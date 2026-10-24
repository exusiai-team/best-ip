/* ============================================================================
   生产网关通路 —— 从本站自己的 Cloudflare Worker 读取「最近一次扫描」的结果
   ----------------------------------------------------------------------------
   这里读「最近一次扫描」。页面自己发起的那次扫描走 app/scan.js，但「拿签名地址、直连 blob、
   逐字节校验」这三步两条路共用本文件导出的 readArtifactFrom —— 一份实现，一处错误口径。
   所以这里做三件事，一件都不多：
     1. GET /api/scans/latest —— 问 Worker「最近一次扫描是哪一次、产物在不在」；
     2. 拿它给的签名地址直连 blob 取回 ZIP 字节（Worker 不搬字节，见
        docs/DECISIONS.md「结果与校验」）；
     3. 交给 app/artifact/reader.js 做完整性校验（身份、SHA-256、CRC、manifest 交叉核对），
        再把 result.json 原样返回——它的形状和本机 loopback 的 ?job= 导出完全一样，
        于是 records.js / render.js 一行都不用改。

   为什么错误文案分得这么细：这条路上「没扫过」「产物过期」「还没跑完」「跑挂了」
   「产物坏了」「浏览器拦了跨源」在页面上都表现为「没有数据」。一句笼统的
   「加载失败」会让人以为是自己网断了，于是反复刷新——所以每种成因都留一句真话，
   并且连标题也分开：标题说「是哪种情况」，正文说「接下来会怎样」。
   ========================================================================== */

import { readArtifact } from "./artifact/reader.js";
import { failure, gatewayStatusText } from "./failure.js";
import { fetchWithTimeout, isTimeoutError } from "./net.js";
import { normalizeProgress } from "./progress.js";

const LATEST_PATH = "/api/scans/latest";
const LATEST_TIMEOUT_MS = 30_000;
const ARTIFACT_TIMEOUT_MS = 30_000;
const MAX_ARTIFACT_BYTES = 50 * 1024 * 1024;
// 传输截断是唯一值得重来的失败：换一张新的签名地址再取一次通常就好了（旧前端同样只对这种情况重取）。
const ARTIFACT_ATTEMPTS = 3;
// 与 worker/github.js 的 ARTIFACT_HOST_SUFFIX 一致：产物地址只认 GitHub 自己的 blob 主机。
// Worker 已经筛过一遍，这里是第二遍——一个被改写过的响应不该把浏览器带去别的主机。
const ARTIFACT_HOST_SUFFIX = ".blob.core.windows.net";

// 上游（Worker）状态 → 人话：标题 + 一句「接下来会怎样」。键名与 worker/latest.js 的
// status 取值一一对应；认不得的状态走兜底那条，宁可不解释也不编原因。
const STATUS_REPORT = {
  none: {
    title: "本站还没有跑过扫描",
    text: "这个页面上就能发起一次：把订阅地址粘到顶部那个输入框，点「开始扫描」，跑完之后这里会自动填上结果。",
  },
  artifact_expired: {
    title: "最近一次扫描的产物已经过期",
    text: "GitHub 上的扫描产物只保留 1 天，过期后只剩这条记录：结果要在扫完的当天来看。",
  },
  running: {
    title: "最近一次扫描还在进行",
    text: "结果还没生成：顶部会跟着这次扫描走进度，跑完后这里自动填上结果，不用刷新。",
  },
  queued: {
    title: "最近一次扫描还在排队",
    text: "结果还没生成：顶部会跟着这次扫描走进度，跑完后这里自动填上结果，不用刷新。",
  },
  failed: {
    title: "最近一次扫描没有成功",
    text: "最近一次扫描没有成功，没有可读的结果。",
  },
  cancelled: {
    title: "最近一次扫描被取消了",
    text: "最近一次扫描被取消了，没有可读的结果。",
  },
};

const UNKNOWN_STATUS = { title: "没有读到真实扫描结果", text: "网关没有说明原因。" };

// 失败对象与状态码文案来自 app/failure.js：原先本文件的 error() 与 scan.js 的 failure()
// 同形（都挂 code、可选 title），只有 error() 多接收一个 cause。见该文件头部。

function reasonOf(cause) {
  return cause && cause.name ? cause.name : "未知错误";
}

function statusReport(status) {
  if (typeof status !== "string" || !status) return UNKNOWN_STATUS;
  return STATUS_REPORT[status] || {
    title: "没有读到真实扫描结果",
    text: `网关返回了无法处理的状态（${status}）。`,
  };
}

/**
 * 签名产物地址只允许 https + GitHub 产物主机；不符合就抛错而不是硬取。
 * 空值也算错：artifact_ready 为 true 却没有地址，是网关自相矛盾的响应。
 */
export function artifactUrlOf(raw) {
  let parsed;
  try {
    parsed = new URL(String(raw || ""));
  } catch {
    throw failure("artifact_url_invalid", "网关返回的产物地址无效");
  }
  if (parsed.protocol !== "https:" || !parsed.hostname.endsWith(ARTIFACT_HOST_SUFFIX)) {
    throw failure("artifact_url_invalid", `网关返回的产物地址不是 GitHub 产物主机：${parsed.hostname || "(空)"}`);
  }
  return parsed.toString();
}

/**
 * 最新那次还在排队/执行的扫描（worker/latest.js 的 active）。形状不对就当没有：它只用来
 * 「跟进」，少了它页面照旧显示已有结果，不值得为它报错。
 */
function activeOf(raw) {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  if (raw.status !== "queued" && raw.status !== "running") return null;
  if (typeof raw.request_id !== "string" || !raw.request_id) return null;
  const startedAt = Date.parse(String(raw.run?.created_at || ""));
  const runId = Number.isSafeInteger(raw.run?.id) && raw.run.id > 0 ? raw.run.id : 0;
  const runAttempt = Number.isSafeInteger(raw.run?.run_attempt) && raw.run.run_attempt > 0 ? raw.run.run_attempt : 0;
  return {
    requestId: raw.request_id,
    status: raw.status,
    startedAt: Number.isFinite(startedAt) ? startedAt : 0,
    // 逐节点读数（scan.js 的 pollProgress）要靠 run 身份去问；跟进的页面手里只有这一份。
    runId,
    runAttempt,
    progress: normalizeProgress(raw.progress),
  };
}

// 401/403/503 三句与 scan.js 共用 app/failure.js 的表（原先两边各抄一份，只差一两个词）；
// 这里的说法是「读取」而不是「请求」，兜底句保持本文件原来的「本站的扫描网关返回 HTTP N」。
function httpTextFor(status) {
  return gatewayStatusText(status, {
    sessionTail: "",
    action: "读取",
    detail: "",
    fallback: (value) => `本站的扫描网关返回 HTTP ${value}`,
  });
}

// 没有超时的话，Worker 侧卡在 GitHub API 上时这个 promise 永不 settle：页面会一直停在
// 「正在读取」，既没有报错也没有重试入口。本文件这两次出网都走 app/net.js 的
// fetchWithTimeout()，超时信号与它的 release 都在那里收口。
/** 取回并校验 GET /api/scans/latest 的响应；形状不对就当网关坏了，不猜。 */
async function fetchLatestState(fetchImpl) {
  let response;
  try {
    response = await fetchWithTimeout(fetchImpl, LATEST_PATH, {
      method: "GET",
      // 同源 + 带会话 cookie：这条路径的门就是站点的访问密码。
      credentials: "same-origin",
      headers: { Accept: "application/json" },
      cache: "no-store",
    }, LATEST_TIMEOUT_MS);
  } catch (cause) {
    if (isTimeoutError(cause)) {
      throw failure("gateway_timeout", `本站的扫描网关 ${LATEST_TIMEOUT_MS / 1000} 秒内没有响应`, { cause });
    }
    throw failure("gateway_unreachable", `读不到本站的扫描网关（${reasonOf(cause)}）`, { cause });
  }
  if (!response.ok) throw failure("gateway_http", httpTextFor(response.status));

  let payload;
  try {
    payload = await response.json();
  } catch (cause) {
    throw failure("gateway_json", "扫描网关返回的不是 JSON", { cause });
  }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw failure("gateway_shape", "扫描网关返回的结构无效");
  }
  const active = activeOf(payload.active);
  if (payload.artifact_ready !== true) {
    // 没有产物时只信 status 那句话；此时 run/artifact 字段一律不读，避免用半个身份拼出一行数据。
    const report = statusReport(payload.status);
    const error = failure(typeof payload.status === "string" && payload.status ? payload.status : "gateway_status",
      report.text, { title: report.title });
    error.active = active;
    throw error;
  }
  const { request_id: requestId, run } = payload;
  const runId = run?.id;
  const runAttempt = run?.run_attempt;
  if (typeof requestId !== "string" || !requestId
    || !Number.isInteger(runId) || runId < 1
    || !Number.isInteger(runAttempt) || runAttempt < 1) {
    throw failure("gateway_shape", "扫描网关说产物就绪，却没给全扫描身份（request_id / run_id / run_attempt）");
  }
  return { requestId, runId, runAttempt, url: artifactUrlOf(payload.artifact_url), active };
}

/**
 * 跟进用的轻量一问：只读 /api/scans/latest 的 JSON，不取字节。返回进行中的那次（没有就是 null）
 * 与当前可读产物属于哪一次扫描——后者变了，才值得去取一次新字节。
 */
export async function pollLatestActive({ fetchImpl = globalThis.fetch } = {}) {
  if (typeof fetchImpl !== "function") throw failure("no_fetch", "当前环境没有可用的 fetch");
  try {
    const state = await fetchLatestState(fetchImpl);
    return { active: state.active, readyRequestId: state.requestId };
  } catch (error) {
    // 没有产物（还没扫过 / 过期 / 最新那次在跑）不是这里的失败：照样把 active 交回去。
    if (error && Object.hasOwn(error, "active")) return { active: error.active, readyRequestId: null };
    throw error;
  }
}

/** 直连签名地址取字节。CORS 直连是刻意的：Worker 出口到 blob 主机不可靠（见 github.js 注释）。 */
async function downloadArtifact(url, fetchImpl) {
  let response;
  try {
    response = await fetchWithTimeout(fetchImpl, url, {
      method: "GET",
      // 不带凭据、不带自定义头：Azure 侧回 Access-Control-Allow-Origin: *，一旦带上
      // cookie 或自定义头就会变成预检请求，反而取不到字节。
      credentials: "omit",
      cache: "no-store",
    }, ARTIFACT_TIMEOUT_MS);
  } catch (cause) {
    if (isTimeoutError(cause)) {
      throw failure("artifact_timeout", `产物地址 ${ARTIFACT_TIMEOUT_MS / 1000} 秒内没有返回字节`, { cause });
    }
    throw failure("artifact_blocked", `浏览器没能取到产物字节（${reasonOf(cause)}）：产物主机拒绝了这次跨源请求`, { cause });
  }
  if (!response.ok) throw failure("artifact_http", `产物地址返回 HTTP ${response.status}`);

  let buffer;
  try {
    buffer = await response.arrayBuffer();
  } catch (cause) {
    throw failure("artifact_truncated", `产物字节没有读完（${reasonOf(cause)}）`, { cause });
  }
  if (buffer.byteLength === 0) throw failure("artifact_truncated", "产物地址返回了空响应（0 字节）");
  if (buffer.byteLength > MAX_ARTIFACT_BYTES) {
    throw failure("artifact_too_large", `产物 ${buffer.byteLength} 字节，超过 ${MAX_ARTIFACT_BYTES} 字节上限`);
  }
  return buffer;
}

/**
 * 取一份产物：每轮都重新问一次「这次要取哪个地址、以及它是哪一次运行的一部分」。
 * 为什么不是先问一次地址再重试下载：签名地址有有效期，重试必须用新的一张，否则第二次
 * 必然还是拿同一份坏字节。为什么只有「字节没读完」值得重来：取不到（跨源被拦 / 404 /
 * 超时）重试一次也不会变好，只会让用户多等一轮才看到那句真话。
 *
 * 「最近一次扫描」（本文件）与「页面上刚发起的那次扫描」（app/scan.js）共用这一段：
 * 两条路的差别只在 rounds() 怎么问出地址，取字节、校验、报错口径都只有一份。
 *
 * @param {() => Promise<{url:string, requestId:string, runId:number, runAttempt:number}>} rounds
 * @returns {Promise<object>} 校验通过的 result.json
 */
export async function readArtifactFrom(rounds, { fetchImpl = globalThis.fetch } = {}) {
  if (typeof fetchImpl !== "function") throw failure("no_fetch", "当前环境没有可用的 fetch");
  for (let attempt = 1; attempt <= ARTIFACT_ATTEMPTS; attempt += 1) {
    // 身份与地址必须取自同一轮响应：拿上一轮的身份去校验这一轮取回的字节，等于把
    // 「地址换了一批内容」这种漂移判成「产物被改动过」。
    const round = await rounds();
    let bytes;
    try {
      bytes = await downloadArtifact(round.url, fetchImpl);
    } catch (cause) {
      if (cause?.code !== "artifact_truncated") throw cause;
      if (attempt === ARTIFACT_ATTEMPTS) {
        throw failure("artifact_truncated", `产物字节连续 ${ARTIFACT_ATTEMPTS} 次都没有读完`, { cause });
      }
      // 字节没读完、且还有重试机会：丢掉这一份，下一轮重新问地址再取一次。
      continue;
    }
    try {
      return (await readArtifact(bytes, round)).result;
    } catch (cause) {
      // 读者层自己也会解出「没读完」这一种（短 body / 找不到结束目录，reader.js 给的 code 就是
      // artifact_truncated）：它和传输截断同因同解，必须一起重试，否则一次半截响应会被说成
      // 「产物被改动过」，而且没有第二发。
      if (cause?.code === "artifact_truncated") {
        if (attempt === ARTIFACT_ATTEMPTS) {
          throw failure("artifact_truncated", `产物字节连续 ${ARTIFACT_ATTEMPTS} 次都没有读完`, { cause });
        }
        continue;
      }
      // 校验失败不是「没有数据」：产物要么被改动过，要么和这次运行对不上，必须说出来。
      // 这里不重试：字节已经完整到手，内容不对是确定的结论。
      throw failure("artifact_invalid", `扫描产物校验失败：${cause.message}`, { cause });
    }
  }
  // 循环只可能 return 或 throw；走到这里说明上面的分支被改坏了——宁可报「没读完」，
  // 也不要静默返回 undefined 让调用方以为数据到手。
  throw failure("artifact_truncated", `产物字节连续 ${ARTIFACT_ATTEMPTS} 次都没有读完`);
}

/**
 * 读一次「最近一次扫描」，返回与 ?job= 本地导出同形的 payload。
 * 任何一步不成立都抛错（带 code 与可选 title），由 main.js 决定显示哪一种空状态。
 *
 * @returns {Promise<{payload: {id: string, status: string, results: object[], manifest: object},
 *   active: {requestId: string, status: string, startedAt: number, progress: object|null} | null}>}
 */
export async function fetchLatestScan({ fetchImpl = globalThis.fetch } = {}) {
  // 每轮都重新问一次 /api/scans/latest：签名地址与运行身份都取自同一轮响应。
  // 进行中的那次扫描（active）跟着最后一轮响应走：成功时挂在返回值上，失败时挂在错误上。
  let active = null;
  try {
    const payload = await readArtifactFrom(async () => {
      const state = await fetchLatestState(fetchImpl);
      active = state.active;
      return state;
    }, { fetchImpl });
    return { payload, active };
  } catch (error) {
    if (error && typeof error === "object" && !Object.hasOwn(error, "active")) error.active = active;
    throw error;
  }
}
