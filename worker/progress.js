import { HttpError, json } from "./responses.js";
import { REQUEST_ID_PATTERN, isRecord, positiveInteger } from "./config.js";
import { assertRunnerToken, scanStub } from "./runner.js";

// 扫描进行中的实时进度：runner 大约每秒把「每个节点此刻在做什么」和总计数报给 Worker，页面轮询时
// 由 Worker 交回。为什么要这条通道：GitHub 在 job 结束前读不到日志（实测 404），Actions API 只给得出
// 步骤名，于是一次几十秒到几分钟的扫描在页面上只有一句「Run production scan」。
//
// 它只是**过程中的读数**，不是结果：每个节点只有名字、协议类型、阶段、第几次尝试、用时与一句白名单
// 原因（backend/app/scan/errors.py 的 live_reason）；出口 IP、评分与订阅内容一律不在这里，表格仍然只认
// 终态 artifact（逐字节校验之后）。进度写坏了、丢了、Durable Object 不可用，都只会让页面退回「只显示
// 步骤名」，绝不会让扫描或结果本身失败。
export const SCAN_PROGRESS_PATH = "/api/scan-progress";

// 一条报告的上限：1000 个节点 × 每个约 150 字符。超过节点上限时 runner 只报计数。
const MAX_REPORT_CHARS = 256 * 1024;
const MAX_COUNT = 100_000;
export const MAX_REPORT_NODES = 1000;
// 节点的几档：等待 → 启动代理 / 连检测站 / 查 IP 质量（失败后进入「重试」再走一遍）→ 三种终态。
const NODE_STATES = ["wait", "retry", "start", "connect", "lookup", "success", "partial", "failed"];
const FINAL_STATES = ["success", "partial", "failed"];
const NODE_TYPE_PATTERN = /^[a-z0-9-]{0,24}$/u;
const MAX_NODE_MS = 3_600_000;
// 一条进度只在扫描期间有意义：scan.yml 是 timeout-minutes: 30，artifact 只留 1 天。
// 6 小时后整条清掉，存储里不留历史。
const RETENTION_MS = 6 * 60 * 60 * 1000;
// 暂存的订阅地址只等 runner 来领这么久（workflow 排队通常几秒，长的也就一两分钟）。
const SUBSCRIPTION_TTL_MS = 15 * 60 * 1000;
const READ_TIMEOUT_MS = 3_000;
// 阶段只往前走：同一次运行里晚到的旧报告不能把「打包中」打回「扫描中」。
const PHASES = ["subscription", "scanning", "packaging", "done"];

function invalidReport() {
  return new HttpError(400, "扫描进度报告无效", "invalid_request");
}

function count(value) {
  return Number.isSafeInteger(value) && value >= 0 && value <= MAX_COUNT ? value : null;
}

function shortText(value, max) {
  if (typeof value !== "string") return null;
  const text = value.trim();
  // 控制字符一律不收：这些字符串最后会被页面当文本画出来。
  return text.length >= 1 && text.length <= max && !/[\u0000-\u001f\u007f]/u.test(text) ? text : null;
}

/** 一个节点的读数；任何一项不合规返回 null（整条报告随之拒收）。 */
function normalizeNode(raw) {
  if (!isRecord(raw)) return null;
  const name = shortText(raw.n, 80);
  const type = typeof raw.t === "string" && NODE_TYPE_PATTERN.test(raw.t) ? raw.t : null;
  const state = NODE_STATES.includes(raw.s) ? raw.s : null;
  const attempt = Number.isSafeInteger(raw.a) && raw.a >= 0 && raw.a <= 10 ? raw.a : null;
  const elapsed = Number.isSafeInteger(raw.ms) && raw.ms >= 0 && raw.ms <= MAX_NODE_MS ? raw.ms : null;
  const reason = raw.r === null || raw.r === undefined ? null : shortText(raw.r, 60);
  if (name === null || type === null || state === null || attempt === null || elapsed === null) return null;
  if (raw.r !== null && raw.r !== undefined && reason === null) return null;
  return { n: name, t: type, s: state, a: attempt, ms: elapsed, r: reason };
}

/** 节点列表必须与计数自洽：个数等于总数，三种终态的个数分别等于三个计数。 */
function normalizeNodes(raw, counts) {
  if (raw === undefined || raw === null) return null;
  if (!Array.isArray(raw) || raw.length > MAX_REPORT_NODES || raw.length !== counts.total) throw invalidReport();
  const nodes = raw.map(normalizeNode);
  if (nodes.includes(null)) throw invalidReport();
  for (const state of FINAL_STATES) {
    if (nodes.filter((node) => node.s === state).length !== counts[state]) throw invalidReport();
  }
  return nodes;
}

/** 校验并规整 runner 的一条报告；任何一项不合规都整条拒收，不做部分采信。 */
export function normalizeReport(payload) {
  if (!isRecord(payload)) throw invalidReport();
  const requestId = typeof payload.request_id === "string" ? payload.request_id : "";
  const runId = positiveInteger(payload.run_id);
  const runAttempt = positiveInteger(payload.run_attempt);
  const phase = typeof payload.phase === "string" ? payload.phase : "";
  if (!REQUEST_ID_PATTERN.test(requestId) || !runId || !runAttempt || !PHASES.includes(phase)) {
    throw invalidReport();
  }
  const total = count(payload.total);
  const completed = count(payload.completed);
  const success = count(payload.success);
  const partial = count(payload.partial);
  const failed = count(payload.failed);
  if ([total, completed, success, partial, failed].includes(null)) throw invalidReport();
  if (completed > total || success + partial + failed !== completed) throw invalidReport();
  const nodes = normalizeNodes(payload.nodes, { total, success, partial, failed });
  return {
    request_id: requestId,
    run_id: runId,
    run_attempt: runAttempt,
    phase,
    total,
    completed,
    success,
    partial,
    failed,
    nodes,
  };
}

async function readReport(request) {
  const contentLength = Number(request.headers.get("Content-Length"));
  if (Number.isFinite(contentLength) && contentLength > MAX_REPORT_CHARS * 4) throw invalidReport();
  let raw;
  try {
    raw = await request.text();
  } catch {
    throw invalidReport();
  }
  if (raw.length > MAX_REPORT_CHARS) throw invalidReport();
  try {
    return JSON.parse(raw);
  } catch {
    throw invalidReport();
  }
}

/** POST /api/scan-progress：runner → Worker。凭领取订阅时拿到的 runner 令牌，不经过站点密码门。 */
export async function scanProgressReport(request, env) {
  try {
    if (request.method !== "POST") throw new HttpError(405, "请求方法不支持", "method_not_allowed");
    const runner = await assertRunnerToken(request, env);
    const report = normalizeReport(await readReport(request));
    // 令牌只对领取它的那一次运行有效：不能拿它替别的 request_id 或别的 attempt 写进度。
    if (report.request_id !== runner.request_id || report.run_id !== runner.run_id ||
        report.run_attempt !== runner.run_attempt) {
      throw new HttpError(403, "进度报告与 runner 身份不符", "run_identity_mismatch");
    }
    const stored = await scanStub(env, report.request_id).fetch("https://scan-state/report", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(report),
    });
    if (!stored.ok) throw new HttpError(502, "扫描进度没有写入", "progress_unavailable");
    return new Response(null, { status: 204, headers: { "Cache-Control": "no-store" } });
  } catch (error) {
    if (error instanceof HttpError) {
      return json({ error: error.code }, error.status, { "Cache-Control": "no-store" });
    }
    throw error;
  }
}

/**
 * 给页面的进度读数。只交回与这一次运行（run_id + attempt）对得上的那一条：重跑同一个
 * request_id 时，上一次 attempt 的计数不能被当成这一次的。读不到、超时、绑定缺失一律返回 null。
 * withNodes 为 false（状态轮询与 latest 的 active）时只给计数；逐节点的那份走 /progress 端点。
 */
export async function readScanProgress(env, requestId, run, { withNodes = false } = {}) {
  const runId = positiveInteger(run?.id);
  const runAttempt = positiveInteger(run?.run_attempt);
  if (!runId || !runAttempt || !REQUEST_ID_PATTERN.test(String(requestId || ""))) return null;
  const stub = scanStub(env, requestId);
  if (!stub) return null;
  let timer;
  try {
    const response = await Promise.race([
      stub.fetch("https://scan-state/report", { method: "GET" }),
      new Promise((resolve) => { timer = setTimeout(() => resolve(null), READ_TIMEOUT_MS); }),
    ]);
    if (!response?.ok) return null;
    const stored = await response.json();
    if (!isRecord(stored) || stored.run_id !== runId || stored.run_attempt !== runAttempt) return null;
    return {
      phase: stored.phase,
      total: stored.total,
      completed: stored.completed,
      success: stored.success,
      partial: stored.partial,
      failed: stored.failed,
      updated_at: typeof stored.updated_at === "string" ? stored.updated_at : null,
      ...(withNodes ? { nodes: Array.isArray(stored.nodes) ? stored.nodes : null } : {}),
    };
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

/**
 * GET /api/scans/{request_id}/progress?run_id=&run_attempt=：页面约每秒问一次的逐节点读数。
 * 只读 Durable Object、不碰 GitHub API，所以可以问得勤。门是站点会话（与 /api/scans/latest 的
 * active 同一道门、同一份数据），不要求 scan token：刷新后跟进的页面手里没有它。
 */
export async function scanProgressFeed(request, env, requestId) {
  if (!REQUEST_ID_PATTERN.test(requestId)) throw new HttpError(400, "request_id 无效", "invalid_request");
  const url = new URL(request.url);
  const runId = positiveInteger(url.searchParams.get("run_id"));
  const runAttempt = positiveInteger(url.searchParams.get("run_attempt"));
  if (!runId || !runAttempt) throw new HttpError(400, "缺少 run_id 或 run_attempt", "invalid_request");
  const progress = await readScanProgress(env, requestId, { id: runId, run_attempt: runAttempt }, { withNodes: true });
  return json({ request_id: requestId, run_id: runId, run_attempt: runAttempt, progress }, 200, { "Cache-Control": "no-store" });
}

/** 同一次运行里，新报告不能比已存的那条「更早」：阶段不倒退，同阶段完成数不减少。 */
function isStale(stored, incoming) {
  if (!isRecord(stored) || stored.run_id !== incoming.run_id || stored.run_attempt !== incoming.run_attempt) {
    return false;
  }
  const storedRank = PHASES.indexOf(stored.phase);
  const incomingRank = PHASES.indexOf(incoming.phase);
  if (incomingRank !== storedRank) return incomingRank < storedRank;
  return incoming.completed < stored.completed;
}

/**
 * 每个 request_id 一个实例，存这次扫描在 Worker 侧的全部状态：
 * - `subscription`：页面交来、等 runner 领取的订阅地址（15 分钟过期，领取即删）；
 * - `runner`：领取时绑定的运行身份与 runner 令牌的 SHA-256（令牌本身不落盘）；
 * - `report`：最新一条进度报告（含逐节点读数，最大约 150KB，远低于 SQLite 后端单值 2MB 的上限）。
 * 存储里都是 Worker 已经校验过的规整对象（只有本 Worker 能拿到这个 namespace）。
 * 第一次写入起 6 小时整体清空；订阅地址到期由同一个 alarm 提前删掉。
 */
export class ScanProgress {
  constructor(state) {
    this.state = state;
  }

  async fetch(request) {
    const path = new URL(request.url).pathname;
    if (path === "/subscription") return this.subscription(request);
    if (path === "/claim" && request.method === "POST") return this.claim(request);
    if (path === "/runner" && request.method === "GET") {
      return Response.json((await this.state.storage.get("runner")) ?? null);
    }
    if (path !== "/report") return new Response(null, { status: 404 });
    if (request.method === "GET") {
      const stored = await this.state.storage.get("report");
      return Response.json(stored ?? null);
    }
    if (request.method !== "PUT") return new Response(null, { status: 405 });
    let incoming;
    try {
      incoming = normalizeReport(await request.json());
    } catch {
      return new Response(null, { status: 400 });
    }
    const stored = await this.state.storage.get("report");
    if (!isStale(stored, incoming)) {
      await this.state.storage.put("report", { ...incoming, updated_at: new Date().toISOString() });
    }
    await this.arm();
    return new Response(null, { status: 204 });
  }

  async subscription(request) {
    const storage = this.state.storage;
    if (request.method === "DELETE") {
      await storage.delete("subscription");
      return new Response(null, { status: 204 });
    }
    if (request.method !== "PUT") return new Response(null, { status: 405 });
    const body = await request.json().catch(() => null);
    if (typeof body?.url !== "string" || !body.url) return new Response(null, { status: 400 });
    // 一个 request_id 只发起一次：已经暂存过（哪怕已被领走）就不再接受。
    if (await storage.get("created_at")) return new Response(null, { status: 409 });
    await storage.put({ subscription: { url: body.url, expires_at: Date.now() + SUBSCRIPTION_TTL_MS } });
    await this.arm();
    return new Response(null, { status: 204 });
  }

  async claim(request) {
    const storage = this.state.storage;
    const body = await request.json().catch(() => null);
    const pending = await storage.get("subscription");
    if (!isRecord(pending) || pending.expires_at <= Date.now() || await storage.get("runner")) {
      return new Response(null, { status: 410 });
    }
    const runId = positiveInteger(body?.run_id);
    const runAttempt = positiveInteger(body?.run_attempt);
    if (!runId || !runAttempt || !/^[a-f0-9]{64}$/u.test(String(body?.token_sha256 || ""))) {
      return new Response(null, { status: 400 });
    }
    await storage.delete("subscription");
    await storage.put("runner", { run_id: runId, run_attempt: runAttempt, token_sha256: body.token_sha256 });
    return Response.json({ url: pending.url });
  }

  /** 记下第一次写入的时间，并把 alarm 定在最近的一个期限（订阅地址过期 / 整体保留期满）。 */
  async arm() {
    const storage = this.state.storage;
    let createdAt = await storage.get("created_at");
    if (!createdAt) {
      createdAt = Date.now();
      await storage.put("created_at", createdAt);
    }
    const pending = await storage.get("subscription");
    const deadline = Math.min(createdAt + RETENTION_MS, pending?.expires_at ?? Infinity);
    const current = await storage.getAlarm();
    if (!current || current > deadline) await storage.setAlarm(deadline);
  }

  async alarm() {
    const storage = this.state.storage;
    const now = Date.now();
    const createdAt = await storage.get("created_at");
    if (!createdAt || now >= createdAt + RETENTION_MS) {
      await storage.deleteAll();
      return;
    }
    const pending = await storage.get("subscription");
    if (pending && pending.expires_at <= now) await storage.delete("subscription");
    await storage.setAlarm(createdAt + RETENTION_MS);
  }
}
