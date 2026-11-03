/* ============================================================================
   行渲染 + 列模型 —— 一个节点 = 宽表的一行；每一列自带「怎么排、怎么筛」
   ----------------------------------------------------------------------------
   COLUMNS 是**唯一**的列真值：表头文案、colgroup 宽度、窄屏卡片的 data-label、表头点击
   排序用的取值、逐列筛选用的取值与控件种类，全部由它生成。三处各写一份必然漂移。

   列的三种语义（sort / filter 字段）：
   - text：按 zh-CN 排序规则比较；筛选是「包含」，空格分隔多个词须同时命中，`a|b` 任一命中，
           `!词` 排除（app/table.js 的 textMatcher）；
   - enum：取值是固定的几个词（住宅 / 机房 / 未知），排序按语义次序，筛选用下拉；
   - num ：分数。-1（地区受限 / 无数据）与缺分**不参与**数值比较，永远沉底；筛选支持
           `80`（≥80）、`>80`、`<=60`、`60-90`、`=-1`（app/table.js 的 numericMatcher）。

   IPure 六项场景评分拆成六个真列：原先是一列里摆六个 chip，只能整体看、不能按某一项排序
   或筛选。现在「按流媒体分排序」「游戏 ≥ 80」与按总分一样是一次点击。

   CSP 约束（生产是 style-src 'self'）：分数色带只能等元素进了 DOM 之后用 CSSOM
   赋值，不能拼进标记的 style 属性里。见 applyScoreStyles。
   ========================================================================== */

import { SCENARIO_LABELS } from "./data.js";
import { comparableScore, ipureScoreInlineStyle } from "./score-color.js";

const STATUS_LABELS = { success: "完整", partial: "部分", failed: "失败" };
const SOURCE_LABELS = { ipure: "IPure", coffee: "Coffee" };

/* ------------------------------------------------------------- 取值函数 --- */
// IPure usageType → 接入列的词。住宅、移动是「真人上网」的出口，风控最友好，着绿色。
const USAGE_LABELS = {
  residential: "住宅", mobile: "移动", business: "商业", hosting: "机房", education: "教育", government: "政府",
};
const KIND_TONES = { 住宅: "good", 移动: "good", 机房: "warn" };

function kindOf(result) {
  if (!result.exit_ip) return null;
  if (USAGE_LABELS[result.usage_type]) return USAGE_LABELS[result.usage_type];
  if (result.is_residential === true) return "住宅";
  if (result.is_residential === false) return "机房";
  return "未知";
}

function nativeOf(result) {
  if (!result.exit_ip) return null;
  if (result.is_native === true) return "原生";
  if (result.is_native === false) return "广播";
  return "未知";
}

/** IPure 总分：无值或空串一律归一成 null，避免药丸里出现空数字。 */
function ipureTotalOf(result) {
  const value = result.score;
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/**
 * 场景分的「显示值」：连上了的行六项永远齐，没测到按 -1 显示（与「地区受限」共用中性灰）。
 * 没有出口 IP 的行根本没有评分对象，返回 undefined，由渲染器换成一句「无场景评分」。
 */
function scenarioShown(result, key) {
  if (!result.exit_ip) return undefined;
  const raw = result.ipure_scores && typeof result.ipure_scores === "object" ? result.ipure_scores[key] : null;
  return raw === null || raw === undefined ? -1 : raw;
}

function formatAsn(asn) {
  const value = Number(asn);
  return Number.isFinite(value) && value > 0 ? `AS${value}` : "";
}

/** 把几段文字拼成一个可检索串；缺项跳过。 */
function joined(...parts) {
  return parts.filter((part) => part !== null && part !== undefined && part !== "").join(" ");
}

// 排序值约定：null = 沉底（不论升降序）；数字直接相减；字符串走 zh-CN 排序规则。
// 筛选值约定：text 列给一个可检索串；enum 列给那个词；num 列给显示出来的那个数（含 -1）。
export const COLUMNS = [
  { key: "rank", label: "#", width: "40px" },
  {
    key: "ident", label: "节点", width: "260px", sort: "text", filter: "text",
    placeholder: "名称/协议",
    sortValue: (r) => r.node || null,
    filterValue: (r) => joined(r.node, r.type, STATUS_LABELS[r.status]),
  },
  {
    key: "ip", label: "出口 IP", width: "164px", sort: "ip", filter: "text",
    placeholder: "IP 或前缀",
    sortValue: (r) => r.exit_ip || null,
    filterValue: (r) => r.exit_ip || "",
  },
  // 归属地列只写国家：真实数据里城市常与国家同名（Hong Kong Hong Kong），一列里写两遍不增信息。
  // 城市、地区、国家代码仍然参与这一列的筛选（输入 JP、Tokyo 都能命中），只是不占列宽。
  {
    key: "geo", label: "国家和地区", width: "104px", sort: "text", filter: "text",
    placeholder: "国家/城市", suggest: (r) => r.country,
    sortValue: (r) => (r.exit_ip ? r.country : null) || null,
    filterValue: (r) => (r.exit_ip ? joined(r.country, r.country_code, r.region, r.city) : ""),
  },
  {
    key: "isp", label: "服务商 / ISP", width: "172px", sort: "text", filter: "text",
    placeholder: "名称/AS 号", suggest: (r) => r.isp,
    sortValue: (r) => (r.exit_ip ? r.isp : null) || null,
    filterValue: (r) => joined(r.isp, r.as_org, formatAsn(r.asn)),
  },
  {
    key: "kind", label: "接入", width: "64px", sort: "enum", filter: "enum",
    options: ["住宅", "移动", "商业", "机房", "教育", "政府", "未知"],
    sortValue: (r) => ({ 住宅: 0, 移动: 1, 商业: 2, 教育: 3, 政府: 4, 机房: 5, 未知: 6 })[kindOf(r)] ?? null,
    filterValue: kindOf,
  },
  {
    key: "native", label: "原生性", width: "66px", sort: "enum", filter: "enum",
    options: ["原生", "广播", "未知"],
    sortValue: (r) => ({ 原生: 0, 广播: 1, 未知: 2 })[nativeOf(r)] ?? null,
    filterValue: nativeOf,
  },
  {
    key: "coffee", label: "Coffee", width: "72px", align: "center", sort: "num", filter: "num",
    title: "Coffee 信任评分",
    sortValue: (r) => (Number.isFinite(r.coffee_score) ? r.coffee_score : null),
    filterValue: (r) => (Number.isFinite(r.coffee_score) ? r.coffee_score : null),
  },
  {
    key: "ipure", label: "IPure", width: "68px", align: "center", sort: "num", filter: "num",
    title: "IPure 纯净度总分",
    // IPure 总分的 -1 是「该地区受限」哨兵，不参与数值比较；null 表示上游没给分。
    sortValue: (r) => comparableScore(ipureTotalOf(r)),
    filterValue: ipureTotalOf,
  },
  ...SCENARIO_LABELS.map(([scenario, label], index) => ({
    key: `scn-${scenario}`, scenario, label, width: "54px", align: "center", sort: "num", filter: "num",
    title: `IPure 场景评分 · ${label}`,
    group: index === 0 ? "first" : "rest",
    sortValue: (r) => comparableScore(scenarioShown(r, scenario) ?? null),
    filterValue: (r) => scenarioShown(r, scenario) ?? null,
  })),
];

export const COLUMN_BY_KEY = new Map(COLUMNS.map((column) => [column.key, column]));
const SCENARIO_COLUMNS = COLUMNS.filter((column) => column.scenario);

// 失败节点缺的是同一组字段（出口 IP / 地区 / 服务商 / 接入 / 原生性），
// 用一个 colspan 单元格说明原因，比摆五个「—」更省地方也更诚实。
const ABSENT_KEYS = ["ip", "geo", "isp", "kind", "native"];

function labelOf(key) {
  return COLUMN_BY_KEY.get(key)?.title ?? COLUMN_BY_KEY.get(key)?.label ?? key;
}

/* -------------------------------------------------------------- 表头 --- */
// col-<key> 是 colgroup 与 thead 共用的命名契约。
export function createColGroup(document) {
  const group = document.createDocumentFragment();
  COLUMNS.forEach((column) => {
    const col = document.createElement("col");
    col.className = `col-${column.key}`;
    if (column.width) col.style.width = column.width;
    group.append(col);
  });
  return group;
}

/**
 * 表头两行：第一行是可点击的列名（点一下排序、再点反向、第三下取消；按住 Shift 叠加次级排序），
 * 第二行是逐列筛选控件。控件由这里按 COLUMNS 生成，index.html 的 <thead> 保持为空
 * （scripts/check_frontend_contracts.mjs 的契约 b）。
 */
export function createHead(document) {
  const fragment = document.createDocumentFragment();

  const head = el(document, "tr", "head-row");
  COLUMNS.forEach((column) => {
    const th = el(document, "th", `col-${column.key}`);
    th.scope = "col";
    if (column.align) th.dataset.align = column.align;
    if (column.group) th.dataset.group = column.group;
    if (column.title) th.title = column.title;
    if (!column.sort) {
      th.textContent = column.label;
      head.append(th);
      return;
    }
    th.setAttribute("aria-sort", "none");
    const button = el(document, "button", "th-sort");
    button.type = "button";
    button.dataset.sortKey = column.key;
    button.title = `${column.title || column.label}：点击排序，Shift+点击叠加排序`;
    const label = el(document, "span", "th-label");
    label.textContent = column.label;
    const indicator = el(document, "span", "sort-ind");
    indicator.setAttribute("aria-hidden", "true");
    const order = el(document, "span", "sort-ord");
    order.setAttribute("aria-hidden", "true");
    button.append(label, indicator, order);
    th.append(button);
    head.append(th);
  });

  const filters = el(document, "tr", "filter-row");
  COLUMNS.forEach((column) => {
    const th = el(document, "th", `col-${column.key}`);
    if (column.group) th.dataset.group = column.group;
    if (!column.filter) {
      th.setAttribute("aria-hidden", "true");
      filters.append(th);
      return;
    }
    const id = `filter-${column.key}`;
    const label = el(document, "label", "filter-lbl");
    label.htmlFor = id;
    label.textContent = column.title || column.label;
    let control;
    if (column.filter === "enum") {
      control = el(document, "select", "filter-input");
      [["全部", ""], ...column.options.map((option) => [option, option])].forEach(([text, value]) => {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = text;
        control.append(option);
      });
    } else {
      control = el(document, "input", "filter-input");
      control.type = "search";
      control.autocomplete = "off";
      control.spellcheck = false;
      if (column.filter === "num") {
        control.inputMode = "text";
        control.placeholder = "≥";
        control.title = "分数筛选：80 即 ≥80；也可写 >80、<=60、60-90、=-1";
      } else {
        control.placeholder = column.placeholder || "筛选";
        control.title = "包含即命中；空格分隔需同时命中，a|b 任一命中，!词 排除";
        if (column.suggest) control.setAttribute("list", `suggest-${column.key}`);
      }
    }
    control.id = id;
    control.dataset.filterKey = column.key;
    th.append(label, control);
    filters.append(th);
  });

  fragment.append(head, filters);
  return fragment;
}

/** 把当前排序画回表头：aria-sort、箭头、多列排序时的序号。表头不重建，输入框焦点不丢。 */
export function syncHeadSort(thead, sorts) {
  thead.querySelectorAll(".head-row th").forEach((th) => {
    const button = th.querySelector(".th-sort");
    if (!button) return;
    const position = sorts.findIndex((sort) => sort.key === button.dataset.sortKey);
    const sort = position >= 0 ? sorts[position] : null;
    th.setAttribute("aria-sort", sort ? (sort.dir === "asc" ? "ascending" : "descending") : "none");
    th.dataset.sorted = sort ? sort.dir : "";
    button.querySelector(".sort-ind").textContent = sort ? (sort.dir === "asc" ? "↑" : "↓") : "";
    button.querySelector(".sort-ord").textContent = sort && sorts.length > 1 ? String(position + 1) : "";
  });
}

/* ------------------------------------------------------------- 小工具 --- */
/** 左侧导轨与状态药丸共用的语义：good=住宅+原生，mixed=可用但有折损，bad=失败。 */
export function verdictOf(result) {
  if (result.status === "failed") return "bad";
  if (result.status === "partial") return "mixed";
  const human = result.is_residential === true || result.usage_type === "mobile";
  return human && result.is_native === true ? "good" : "mixed";
}

function sourceNote(source) {
  return SOURCE_LABELS[source] ? `（来源：${SOURCE_LABELS[source]}）` : "";
}

function coffeeBand(score) {
  if (!Number.isFinite(score)) return "na";
  if (score >= 75) return "good";
  if (score >= 45) return "warn";
  return "bad";
}

function el(document, tag, className) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  return node;
}

function chip(document, label, tone) {
  const node = el(document, "span", `chip chip-${tone}`);
  node.textContent = label;
  return node;
}

function cell(document, key, className) {
  const td = el(document, "td", className);
  td.dataset.label = labelOf(key);
  return td;
}

/** 一键筛选：点单元格里的值，就把这一列的筛选设成它（再点一次取消）。由 main.js 事件委托处理。 */
function quickFilter(node, key, value) {
  if (!value) return node;
  node.dataset.quickKey = key;
  node.dataset.quickValue = value;
  node.classList.add("quick");
  return node;
}

/* --------------------------------------------------------------- 单元格 --- */
function rankCell(document, position) {
  const td = cell(document, "rank", "rank");
  td.textContent = String(position);
  td.setAttribute("aria-label", `第 ${position} 位`);
  return td;
}

function identCell(document, result) {
  const td = cell(document, "ident", "ident");
  const name = el(document, "span", "node");
  name.textContent = result.node || "未命名节点";
  td.append(name);

  const sub = el(document, "span", "ident-sub");
  if (result.type) {
    const proto = quickFilter(el(document, "span", "proto"), "ident", result.type);
    proto.textContent = result.type;
    proto.title = `只看 ${result.type} 节点`;
    sub.append(proto);
  }
  if (result.status === "partial") sub.append(chip(document, STATUS_LABELS.partial, "warn"));
  else if (result.status === "failed") sub.append(chip(document, STATUS_LABELS.failed, "bad"));
  if (sub.childNodes.length) td.append(sub);

  td.title = result.status && result.status !== "success"
    ? `${result.node || "未命名节点"}（${STATUS_LABELS[result.status]}）`
    : result.node || "未命名节点";
  name.title = td.title;
  return td;
}

function ipCell(document, result) {
  const td = cell(document, "ip", "cell-ip");
  const code = el(document, "code", "ip");
  code.textContent = result.exit_ip;
  code.title = `出口 IP${result.sources?.ip === "ipure" ? "（IPure 已确认）" : sourceNote(result.sources?.ip)}`;

  const copy = el(document, "button", "copy-btn");
  copy.type = "button";
  copy.dataset.copy = result.exit_ip;
  copy.title = `复制出口 IP ${result.exit_ip}`;
  copy.setAttribute("aria-label", `复制出口 IP ${result.exit_ip}`);
  copy.append(copyIcon(document), Object.assign(el(document, "span", "copy-lbl"), { textContent: "复制" }));

  td.append(code, copy);
  return td;
}

function geoCell(document, result) {
  const td = cell(document, "geo", "cell-geo");
  const text = result.country || "未知";
  const value = quickFilter(el(document, "span", "geo"), "geo", result.country);
  value.textContent = text;
  td.append(value);
  // 列宽固定、超出省略：裁掉的字与城市只能靠 title 读回来。
  const detail = [result.country, result.region, result.city].filter((part, index, all) => part && all.indexOf(part) === index);
  td.title = `${detail.join(" · ") || "归属地未知"}${result.country_code ? ` [${result.country_code}]` : ""}${sourceNote(result.sources?.geo)}`;
  return td;
}

function ispCell(document, result) {
  const td = cell(document, "isp", "cell-isp");
  const isp = quickFilter(el(document, "span", "isp"), "isp", result.isp);
  isp.textContent = result.isp || "服务商未知";
  isp.title = `${result.isp || "服务商未知"}${result.as_org && result.as_org !== result.isp ? ` · ${result.as_org}` : ""}${sourceNote(result.sources?.isp)}`;
  td.append(isp);

  const asnText = formatAsn(result.asn);
  if (asnText) {
    const sub = quickFilter(el(document, "span", "isp-sub"), "isp", asnText);
    sub.textContent = asnText;
    td.append(sub);
  }
  return td;
}

function kindCell(document, result) {
  const td = cell(document, "kind", "cell-kind");
  const value = kindOf(result);
  const chipEl = quickFilter(chip(document, value, KIND_TONES[value] || "neutral"), "kind", value);
  chipEl.title = `${value}${sourceNote(result.sources?.kind)}`;
  td.append(chipEl);
  return td;
}

function nativeCell(document, result) {
  const td = cell(document, "native", "cell-native");
  const value = nativeOf(result);
  const tone = value === "原生" ? "good" : value === "广播" ? "warn" : "neutral";
  const chipEl = quickFilter(chip(document, value, tone), "native", value);
  // 三态 chip 只有三个词，而 native_status 是上游原话（如「广播 IP (AE)」「任播服务」）。
  const status = typeof result.native_status === "string" ? result.native_status.trim() : "";
  chipEl.title = `${status || value}${sourceNote(result.sources?.native)}`;
  td.append(chipEl);
  return td;
}

/** 分数药丸：Coffee 走三档，IPure 总分走色带（色带由 CSSOM 后置写入）。 */
function scorePill(document, { kind, value, note }) {
  const pill = el(document, "span", "score");
  if (kind === "ipure") {
    const scoreStyle = ipureScoreInlineStyle(value);
    const hasBand = scoreStyle !== "";
    pill.dataset.band = hasBand ? "band" : "na";
    if (hasBand) {
      pill.dataset.ipureScore = String(value);
      pill.dataset.ipureStyle = scoreStyle;
    }
    pill.textContent = value === null || value === undefined ? "—" : String(value);
    if (value === -1) pill.title = "该地区受限";
    else if (hasBand) pill.title = `IPure 纯净度总分 ${value}`;
    else pill.title = note || "IPure 未返回纯净度总分";
    return pill;
  }
  pill.dataset.band = coffeeBand(value);
  pill.textContent = Number.isFinite(value) ? String(value) : "—";
  pill.title = Number.isFinite(value) ? `Coffee 评分 ${value}` : "暂无 Coffee 评分";
  return pill;
}

function coffeeCell(document, result) {
  const td = cell(document, "coffee", "cell-coffee");
  td.dataset.align = "center";
  td.append(scorePill(document, {
    kind: "coffee",
    value: Number.isFinite(result.coffee_score) ? result.coffee_score : null,
  }));
  return td;
}

function ipureCell(document, result) {
  const td = cell(document, "ipure", "cell-ipure");
  td.dataset.align = "center";
  td.append(scorePill(document, { kind: "ipure", value: ipureTotalOf(result), note: result.score_note }));
  return td;
}

/**
 * 一个场景一格。两种 -1 成因不同：上游没返回值是「无数据」，上游明确回 -1 才是受限；
 * 差别放在 title 里说，格子里只出现一个数字。
 */
function scenarioCell(document, result, column) {
  const td = cell(document, column.key, "cell-scn");
  td.dataset.align = "center";
  if (column.group) td.dataset.group = column.group;
  const raw = result.ipure_scores && typeof result.ipure_scores === "object" ? result.ipure_scores[column.scenario] : null;
  const value = scenarioShown(result, column.scenario);
  const item = el(document, "span", "scn");
  item.dataset.ipureScore = String(value);
  if (raw === null || raw === undefined) item.title = `${column.label}：无数据（显示为 -1）`;
  else if (value === -1) item.title = `${column.label}：该地区受限`;
  else item.title = `${column.label} 场景评分 ${value}`;
  const lbl = el(document, "span", "scn-lbl");
  lbl.textContent = column.label;
  const num = el(document, "span", "scn-num");
  num.textContent = String(value);
  item.append(lbl, num);
  td.append(item);
  return td;
}

/** 没连上的行：六个场景格合成一格，写一句话，比摆六个 -1 更直白。 */
function scenarioNoneCell(document, result) {
  const td = el(document, "td", "cell-scn-none");
  td.dataset.label = "IPure 场景评分";
  td.colSpan = SCENARIO_COLUMNS.length;
  td.dataset.group = "first";
  td.textContent = result.status === "failed" ? "无场景评分（未连接成功）" : "无场景评分";
  return td;
}

/** 失败行不留白：把原因和卡住的步骤写进本该显示网络信息的跨列单元格。 */
function absentCell(document, result) {
  const td = el(document, "td", "cell-absent");
  td.dataset.label = "失败原因";
  td.colSpan = ABSENT_KEYS.length;

  const line = el(document, "span", "failure");
  line.append(warnIcon(document), document.createTextNode(result.error || "连接失败"));
  if (result.error_step) {
    const step = el(document, "span", "failure-step");
    step.textContent = `（${result.error_step}）`;
    line.append(step);
  }
  td.append(line);

  if (result.isp) {
    const configured = el(document, "span", "failure-isp");
    configured.textContent = `配置服务商：${result.isp}${formatAsn(result.asn) ? ` · ${formatAsn(result.asn)}` : ""}`;
    td.append(configured);
  }
  return td;
}

/* ----------------------------------------------------------- 行装配 --- */
export function createRow(result, { position, tier }, document) {
  const row = el(document, "tr", "row");
  row.dataset.status = result.status || "failed";
  row.dataset.verdict = verdictOf(result);
  if (tier) row.dataset.tier = "top";

  row.append(rankCell(document, position), identCell(document, result));

  if (result.exit_ip) {
    row.append(
      ipCell(document, result),
      geoCell(document, result),
      ispCell(document, result),
      kindCell(document, result),
      nativeCell(document, result),
    );
  } else {
    row.append(absentCell(document, result));
  }

  row.append(coffeeCell(document, result), ipureCell(document, result));
  if (result.exit_ip) SCENARIO_COLUMNS.forEach((column) => row.append(scenarioCell(document, result, column)));
  else row.append(scenarioNoneCell(document, result));
  return row;
}

export function applyScoreStyles(root) {
  // 顺序契约：赋值必须发生在元素进了 DOM 之后 —— 生产 CSP 是 style-src 'self'，标记里的
  // style 属性会被拦掉，CSSOM 赋值不算内联样式。
  root.querySelectorAll("[data-ipure-score]").forEach((element) => {
    const precomputed = element.dataset.ipureStyle;
    element.style.cssText = precomputed ?? ipureScoreInlineStyle(element.dataset.ipureScore);
  });
}

/* --------------------------------------------------------------- 图标 --- */
function svgIcon(document, size, strokeWidth, children) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  for (const [name, value] of Object.entries({
    viewBox: "0 0 24 24", width: size, height: size, fill: "none", stroke: "currentColor",
    "stroke-width": strokeWidth, "stroke-linecap": "round", "aria-hidden": "true",
  })) svg.setAttribute(name, value);
  children.forEach(([tag, attrs]) => {
    const child = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (const [name, value] of Object.entries(attrs)) child.setAttribute(name, value);
    svg.append(child);
  });
  return svg;
}

function copyIcon(document) {
  return svgIcon(document, "11", "2.4", [
    ["rect", { x: "9", y: "9", width: "12", height: "12", rx: "2.5" }],
    ["path", { d: "M5 15V5a2 2 0 0 1 2-2h10" }],
  ]);
}

function warnIcon(document) {
  return svgIcon(document, "13", "2.2", [
    ["circle", { cx: "12", cy: "12", r: "9" }],
    ["path", { d: "M12 8v5M12 16.5h.01" }],
  ]);
}

export { STATUS_LABELS };
