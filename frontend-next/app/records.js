/* ============================================================================
   导出记录 → 行对象 —— 真实数据与渲染器之间唯一的适配层
   ----------------------------------------------------------------------------
   输入是 backend `/api/scans/{job}/export` 的 results[i]（真实产物原样），输出是
   app/data.js 里那种行对象：字段名与 render.js 的列契约一一对应，渲染器不需要知道
   数据来自真实扫描还是合成示例。

   三条最容易写错、也最容易在页面上表现成「谎话」的规则：

   1. 三态布尔（is_residential / is_native）的 null 是「没测到」，**不是「否」**。
      渲染器把 true 显示成「住宅 / 原生」、false 显示成「机房 / 广播」、null 显示成
      中性灰的「未知」。把 null 压成 false，等于把一个没测出归属的节点当面诬告成机房。

   2. ipure_scores 里的 -1 是「该地区受限」的哨兵，**不是低分**：原样保留，交给取色与
      排序逻辑按哨兵处理（见 docs/DECISIONS.md「数据与评分」）。
      null 才是「该项无数据」。两者混起来会把受限地区画成红色垫底。

   3. 一切非字符串脏值都截断成 null：空串、数字、对象都当没有。渲染器对「有值」和
      「无值」走不同分支——isp 传一个空串进去，行里会出现一片空白的服务商，
      而不是「服务商未知」。
   ========================================================================== */

const STATUSES = new Set(["success", "partial", "failed"]);

// 渲染器只读这六项场景；总分走顶层 score（ipure_scores.total 与 score 同源，不再重复携带）。
const SCENARIO_KEYS = ["ai", "social", "streaming", "gaming", "ecommerce", "email"];

// 失败可能卡在这几步上，也只有这几步对读表格的人有意义。其余请求
// （who / global_ping / port_scan / ping_check）是可选的探针，失败不代表流程卡住，
// 所以不参与 error_step 的推断。旧产物里的 page 仍认（新产物不再请求 Coffee 页面）。
const STEP_LABELS = [
  ["page", "Coffee 页面"],
  ["trace", "出口 IP 确认"],
  ["ipx", "ipx 健康分"],
  ["lookup", "Coffee 查询"],
  ["ipqs", "1ms 评分"],
  ["ipure", "IPure 评分"],
];

// IPure 明确答复「未收录这个 IP」（匿名接口不做实时检测）时 requests.ipure 的 error_type。
const IPURE_NOT_INDEXED = "NotIndexed";

/* ------------------------------------------------------------- 值归一化 --- */

function text(value) {
  return typeof value === "string" && value.trim() !== "" ? value.trim() : null;
}

function integer(value) {
  if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
  const number = Number(value);
  return Number.isFinite(number) ? Math.trunc(number) : null;
}

/**
 * IPure 取值的合法域：`-1` 是「该地区受限」哨兵，`0..100` 的**整数**是真实分数，其余当无值。
 * 不能只靠 integer() 的截断：`-1.4` 会被截成 `-1`，于是渲染器把「上游回了个域外值」谎报成
 * 「该地区受限」——那正是这一列语义最重的值；`-0.5` 会被截成 `0`，凭空造出一个 0 分；
 * `-2` / `101` 更糟，会直接显示成「场景评分 -2」。所以先要求整数，再要求落在域内，
 * 不合格的退回 null，走「无数据（显示为 -1）」那条路，不猜。
 */
function ipureValue(value) {
  if (value === null || value === undefined || value === "" || typeof value === "boolean") return null;
  const number = Number(value);
  if (!Number.isInteger(number)) return null;
  if (number === -1) return -1;
  return number >= 0 && number <= 100 ? number : null;
}

/** 三态布尔：只有真的布尔才透传，其余一律 null（含 "true" / 0 / "no" 这类脏值）。 */
function triState(value) {
  return value === true || value === false ? value : null;
}

/**
 * 「住宅 / 机房」是三态：true 住宅、false 机房、null 没测到。
 * is_datacenter 是后端另一个独立键，只回了「是机房」时用它补出 false（机房必然不是住宅），
 * 但绝不拿它去补 true——「不是机房」不等于「是住宅」。
 */
function residentialOf(record) {
  const stated = triState(record?.is_residential);
  if (stated !== null) return stated;
  const datacenter = triState(record?.is_datacenter);
  return datacenter === true ? false : null;
}

function pairOf(source) {
  if (!source || typeof source !== "object" || Array.isArray(source)) return { country: null, city: null };
  return { country: text(source.country), city: text(source.city) };
}

/**
 * location 是后端把 country 与 city 用空格拼出来的展示串，两种形态：
 * - "Hong Kong Hong Kong"：国家与城市同名，整串是同一段重复两次；
 * - "Singapore"：只有国家。
 * 只有这两类能无歧义拆开——「前半 = 后半」时取那一半同时当国家与城市，单词时只知道国家。
 * 其余形态（如 "United States Los Angeles"、"New York"）一律返回 null：光靠空格分不出
 * "New York" 是城市还是国家，猜错比留白更糟（渲染器对空值显示「归属地未知」）。
 */
function pairFromLocation(value) {
  const raw = text(value);
  if (!raw) return { country: null, city: null };
  const words = raw.split(/\s+/u);
  if (words.length === 1) return { country: words[0], city: null };
  if (words.length % 2 === 0) {
    const half = words.length / 2;
    const head = words.slice(0, half).join(" ");
    const tail = words.slice(half).join(" ");
    if (head === tail) return { country: head, city: tail };
  }
  return { country: null, city: null };
}

// 国家代码 → 中文国名。ipx / who / 1ms 共识只给代码，IPure 给的是英文名；统一按代码本地化，
// 整列同一种语言。运行环境没有 Intl.DisplayNames 时退回上游原文。
const REGION_NAMES = (() => {
  try {
    return new Intl.DisplayNames(["zh-CN"], { type: "region" });
  } catch {
    return null;
  }
})();

function regionName(code) {
  if (!code || !REGION_NAMES) return null;
  try {
    const name = REGION_NAMES.of(code);
    return name && name !== code ? name : null;
  } catch {
    return null;
  }
}

/**
 * 国家 / 城市的优先顺序：
 * - 新产物（带 network_source）：collector 已按「ipx → 1ms who → 1ms 共识 → IPure」写好顶层字段，
 *   只认顶层，**不再**回落 Coffee 的归属数据——那正是这次改掉的来源；
 * - 旧产物：归属查询的原始字段 → coffee 页面拿到的同一份归属数据 → 从 location 串派生 → null。
 */
function countryCityOf(record) {
  const top = { country: text(record?.country), city: text(record?.city) };
  if (record?.network_source && typeof record.network_source === "object") return top;
  if (top.country || top.city) return top;

  const fromLookup = pairOf(record?.requests?.lookup?.data);
  if (fromLookup.country || fromLookup.city) return fromLookup;

  // coffee.lookup 有两种落盘形态：直接是归属数据，或包一层 { data }。两种都收。
  const coffeeLookup = record?.coffee?.lookup;
  const fromCoffee = pairOf(coffeeLookup?.data && typeof coffeeLookup.data === "object"
    ? coffeeLookup.data
    : coffeeLookup);
  if (fromCoffee.country || fromCoffee.city) return fromCoffee;

  return pairFromLocation(record?.location);
}

const SOURCES = new Set(["ipx", "who", "ipqs", "ipure", "coffee"]);
const USAGE_TYPES = new Set(["residential", "mobile", "business", "hosting", "education", "government"]);

const IPQS_GRADES = new Set(["A", "B", "C", "D", "F"]);

/**
 * 1ms IPQS 评分：记录顶层只有分数、等级与报告地址，扣分明细在请求证据里，只用来写 tooltip。
 * 分数必须是 0..100 的整数；等级只在有分时才认。
 */
function ipqsOf(record) {
  const raw = record?.ipqs_score;
  const score = Number.isInteger(raw) && raw >= 0 && raw <= 100 ? raw : null;
  const deductions = record?.requests?.ipqs?.data?.deductions;
  return {
    ipqs_score: score,
    ipqs_grade: score !== null && IPQS_GRADES.has(record?.ipqs_grade) ? record.ipqs_grade : null,
    ipqs_report_url: score !== null ? text(record?.ipqs_report_url) : null,
    ipqs_deductions: Array.isArray(deductions)
      ? deductions
        .filter((item) => text(item?.key) && Number.isFinite(item?.points) && item.points > 0)
        .map((item) => ({ key: item.key, points: Math.round(item.points) }))
      : [],
  };
}

const IPX_GRADES = new Set(["极佳", "良好", "一般", "较差", "高风险"]);

/**
 * ipx.al 健康分：同 1ms 的形状。扣分项在请求证据的 items 里（含 0 分的提示项），
 * tooltip 只列真正扣了分的。
 */
function ipxOf(record) {
  const raw = record?.ipx_score;
  const score = Number.isInteger(raw) && raw >= 0 && raw <= 100 ? raw : null;
  const data = record?.requests?.ipx?.data;
  const items = data?.items;
  return {
    ipx_score: score,
    ipx_grade: score !== null && IPX_GRADES.has(record?.ipx_grade) ? record.ipx_grade : null,
    ipx_report_url: score !== null ? text(record?.ipx_report_url) : null,
    ipx_ceiling: score !== null && Number.isInteger(data?.ceiling) ? data.ceiling : null,
    ipx_deductions: Array.isArray(items)
      ? items
        .filter((item) => text(item?.label) && Number.isFinite(item?.points) && item.points < 0)
        .map((item) => ({ label: item.label, points: Math.round(-item.points) }))
      : [],
  };
}

/** 每组取值是谁说的：network_source 只在新产物里有；旧产物一律记为 Coffee（那时只有它）。 */
function sourcesOf(record) {
  const source = record?.network_source;
  if (source && typeof source === "object" && !Array.isArray(source)) {
    const pick = (key) => (SOURCES.has(source[key]) ? source[key] : null);
    return { ip: pick("exit_ip"), geo: pick("geo"), isp: pick("isp"), kind: pick("kind"), native: pick("native") };
  }
  return { ip: null, geo: null, isp: null, kind: null, native: null };
}

/** 国家代码：两到三位字母才算，统一大写；其余一律 null。新产物只认顶层，不回落 Coffee。 */
function countryCodeOf(record) {
  const legacy = !(record?.network_source && typeof record.network_source === "object");
  const raw = text(record?.country_code)
    || (legacy ? text(record?.requests?.lookup?.data?.countryCode) : null)
    || (legacy ? text(record?.requests?.lookup?.data?.country_code) : null);
  return raw && /^[A-Za-z]{2,3}$/u.test(raw) ? raw.toUpperCase() : null;
}

/** 失败卡在哪一步：四步流水线里第一个 ok !== true 的请求。全都没失败或结构缺失 → null。 */
function errorStepOf(record) {
  const requests = record?.requests;
  if (!requests || typeof requests !== "object" || Array.isArray(requests)) return null;
  for (const [key, label] of STEP_LABELS) {
    const entry = requests[key];
    // 记录里没有这个请求（没跑过）不算「卡在这里」，继续看后面的流水线步骤。
    if (!entry || typeof entry !== "object") continue;
    if (entry.ok === true) continue;
    return label;
  }
  return null;
}

/**
 * 六项场景评分。上游给了对象就一律按六项画：缺项（null / 域外值）由渲染器显示 -1，
 * 这样「IPure 场景评分」列在每一行都是固定的六格。只有上游根本没这个键（不是对象）
 * 时才返回 null——渲染器对「没有出口 IP 的行」会用一句「无场景评分」代替六格。
 */
function scenarioScoresOf(source) {
  if (!source || typeof source !== "object" || Array.isArray(source)) return null;
  const scores = {};
  for (const key of SCENARIO_KEYS) scores[key] = ipureValue(source[key]);
  return scores;
}

/**
 * IPure 没分时的说明：上游明说「未收录」就照说；否则只有「部分」才写明原因——
 * 完整却缺分说明上游契约变了，那不该被一句解释掩盖过去。
 */
function scoreNoteOf(record, status, score) {
  if (status === "failed" || score !== null) return null;
  if (record?.requests?.ipure?.error_type === IPURE_NOT_INDEXED) return "IPure 未收录该 IP";
  return status === "partial" ? "IPure 未返回纯净度总分" : null;
}

/* ----------------------------------------------------------------- 映射 --- */

function toRow(record) {
  const status = STATUSES.has(record?.status) ? record.status : "failed";
  const failed = status === "failed";
  const geo = countryCityOf(record);
  const countryCode = failed ? null : countryCodeOf(record);
  const asn = integer(record?.asn);
  const rdns = text(record?.rdns);
  // 总分与六项同一取值域：越界小数不能被截成 -1，否则药丸会把它说成「该地区受限」。
  const score = failed ? null : ipureValue(record?.score);

  return {
    node: text(record?.node),
    type: text(record?.type),
    status,
    // 失败行没有出口 IP：即便记录里残留一个值也不显示，否则「没连上」的行会被伪装成可用节点。
    exit_ip: failed ? null : text(record?.exit_ip),
    country: regionName(countryCode) || geo.country,
    // 上游原文（如 IPure 的 "Japan"）留给 title 与筛选：输入英文国名照样命中。
    country_raw: geo.country,
    country_code: countryCode,
    region: text(record?.region),
    city: geo.city,
    // 服务器名（反向解析 PTR）。"-" 是后端「没有」的占位，不当值显示。
    rdns: rdns && rdns !== "-" ? rdns : null,
    isp: text(record?.isp),
    as_org: text(record?.as_org),
    // 页面在 title 里写明「这一格来自 ipx.al / 1ms who / 1ms IPQS / IPure」，排查数据时不必去翻产物。
    sources: sourcesOf(record),
    asn: asn !== null && asn > 0 ? asn : null,
    company_type: text(record?.company_type),
    // is_datacenter 与 is_residential 是后端各自独立取到的两个键：只回了「是机房」而没回
    // 「不是住宅」时，三态会停在 null，页面就画成「未知」——把上游已经写明的事实丢掉。
    is_residential: residentialOf(record),
    // IPure 的使用类型（住宅 / 移动 / 商业 / 机房 / 教育 / 政府）；旧产物与 unknown 为 null。
    usage_type: failed ? null : (USAGE_TYPES.has(record?.usage_type) ? record.usage_type : null),
    native_status: text(record?.native_status),
    is_native: triState(record?.is_native),
    coffee_score: failed ? null : integer(record?.coffee_score),
    ...(failed ? ipqsOf(null) : ipqsOf(record)),
    ...(failed ? ipxOf(null) : ipxOf(record)),
    score,
    score_note: scoreNoteOf(record, status, score),
    ipure_scores: failed ? null : scenarioScoresOf(record?.ipure_scores),
    // 失败原因对所有状态都照抄：渲染器只在没有出口 IP 的行上展示它，而「部分」的行
    // 也可能恰好缺出口 IP（trace 没成功），那时它就是这行唯一的解释。
    error: text(record?.error) || text(record?.transport_error),
    error_step: errorStepOf(record),
  };
}

/** 按 node_index 排序：导出顺序理论上就是它，但排名列读的就是行序，排一次不留隐患。 */
function inNodeOrder(results) {
  const keyed = results.map((record, index) => ({ record, index, key: integer(record?.node_index) }));
  keyed.sort((left, right) => {
    if (left.key === null && right.key === null) return left.index - right.index;
    if (left.key === null) return 1;
    if (right.key === null) return -1;
    return left.key - right.key || left.index - right.index;
  });
  return keyed.map((entry) => entry.record);
}

/**
 * 把一份导出 payload 映射成行对象数组。
 * @param {{results?: object[]}} exportPayload backend 的导出 payload 或 fetchExport 的返回值
 */
export function toRows(exportPayload) {
  const results = exportPayload?.results;
  if (!Array.isArray(results)) throw new Error("导出结果缺少 results 列表");
  // 元素不是对象时不能默默造行：toRow 会把未知 status 兜成 "failed"，于是 [null] 这种
  // 畸形载荷会渲染成一行「未命名节点 / 连接失败」，看着像这次扫描真的失败了一个节点。
  if (!results.every((item) => item !== null && typeof item === "object" && !Array.isArray(item))) {
    throw new Error("导出结果里有不是对象的记录项");
  }
  return inNodeOrder(results).map(toRow);
}

/* ---------------------------------------------------------- 来源元信息 --- */

function pad(value) {
  return String(value).padStart(2, "0");
}

/**
 * finished_at 是 ISO 串，按本地墙上时间格式化成 "YYYY-MM-DD HH:mm"。
 * 刻意不用 new Date()：那会把「这次快照生成于何时」变成「页面何时被打开」，同一个任务
 * 每刷新一次就换一个时间戳，截图比对和归档都失去意义。
 */
function formatLocalTimestamp(value) {
  const raw = text(value);
  if (!raw) return null;
  const date = new Date(raw);
  if (Number.isNaN(date.getTime())) return null;
  const day = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  return `${day} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/**
 * 与 app/data.js 的 SNAPSHOT_META 同形：页面顶部那句「生成于……」和导出快照的标题都读它。
 * source 带上任务 ID 的前 8 位：页面里同时提到「真实扫描」和「哪一次扫描」才说得清来源，
 * 完整 ID 太长，塞进标题里只会挤掉真正有用的信息。
 */
export function toSnapshotMeta(exportPayload) {
  const jobId = text(exportPayload?.id) || "";
  const generatedAt = formatLocalTimestamp(exportPayload?.finished_at)
    || formatLocalTimestamp(exportPayload?.created_at)
    || "时间未知";
  return {
    generatedAt,
    source: jobId ? `真实扫描 · ${jobId.slice(0, 8)}` : "真实扫描",
  };
}
