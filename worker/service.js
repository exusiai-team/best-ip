import { HttpError, json } from "./responses.js";
import {
  GITHUB_OWNER,
  GITHUB_REPOSITORY,
  GITHUB_WORKFLOW,
  GITHUB_REF,
  REQUEST_ID_PATTERN,
  isRecord,
  assertWorkerConfigured,
} from "./config.js";
import { githubJson } from "./github.js";
import { assertTargetUrl, constantTimeEqual } from "./relay.js";
import { assertRunnerToken, scanStub, stashSubscription, dropSubscription } from "./runner.js";

// /api/v1/*：给 MyClash 这类机器调用方的分类接口（契约见 docs/CLASSIFY_API.md）。
//
// 鉴权只认 `Authorization: Bearer <BESTIP_SERVICE_TOKEN>`，不走站点密码 Cookie，也不做同源检查。
// 扫描本身还是同一条路：订阅（地址或内联 YAML）暂存进这次 scan_id 的 SCAN_PROGRESS Durable Object，
// workflow 只拿到 scan_id 与本站 origin，runner 凭 OIDC 领一次、领完即删（worker/runner.js）。
// 不同的是结果：runner 扫完把分类结果 JSON 直接交回 Worker（POST /api/scan-result），存进
// ClassifyRegistry（全站一个实例），保留 8 天；每个 label 最近一次 done 的结果一直留到被更新的那次替换。
// 注册表里没有订阅地址、没有订阅内容，也没有令牌本身（只有它的 SHA-256 前缀，用来区分调用方）。
export const SERVICE_PREFIX = "/api/v1/";
export const SCAN_RESULT_PATH = "/api/scan-result";
export const API_RUN_TITLE_PREFIX = "Best IP api ";

const REGISTRY_NAME = "classify-registry-v1";
const SERVICE_TOKEN_MIN_CHARS = 32;
const SERVICE_TOKEN_MAX_CHARS = 512;
const MAX_CREATE_BYTES = 16 * 1024 * 1024;
const MAX_YAML_BYTES = 5 * 1024 * 1024;
const MAX_RESULT_CHARS = 16 * 1024 * 1024;
const MAX_REUSE_HOURS = 168;
const ID_PATTERN = /^[A-Za-z0-9._:-]{1,128}$/u;
const LABEL_PATTERN = /^[A-Za-z0-9._:-]{1,64}$/u;
const PROFILES = ["classify", "full"];
const NODE_STATUSES = ["ok", "failed", "skipped"];
const ERROR_CODE_PATTERN = /^[a-z0-9_]{1,48}$/u;
// 多久没跑完就算超时：排队 + scan.yml 的 30 分钟上限，留足余量。
const ACTIVE_TTL_MS = 3 * 60 * 60 * 1000;
// 派发后这么久还在 GitHub 上找不到这次运行，就当派发丢了。
const RUN_LOOKUP_GRACE_MS = 20 * 60 * 1000;
const GITHUB_CHECK_INTERVAL_MS = 60 * 1000;
const GITHUB_CHECK_MIN_AGE_MS = 90 * 1000;
const RETENTION_MS = 8 * 24 * 60 * 60 * 1000;
const RESULT_CHUNK_CHARS = 512 * 1024;
const CLEANUP_INTERVAL_MS = 24 * 60 * 60 * 1000;

async function sha256Hex(value) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

/** 校验服务令牌，返回调用方标识（令牌 SHA-256 的前 16 位）。 */
export async function authenticateService(request, env) {
  const expected = String(env.BESTIP_SERVICE_TOKEN || "").trim();
  if (expected.length < SERVICE_TOKEN_MIN_CHARS || expected.length > SERVICE_TOKEN_MAX_CHARS) {
    throw new HttpError(503, `请配置至少 ${SERVICE_TOKEN_MIN_CHARS} 个字符的 BESTIP_SERVICE_TOKEN`, "service_not_configured");
  }
  const header = request.headers.get("Authorization") || "";
  const token = header.startsWith("Bearer ") ? header.slice(7).trim() : "";
  if (!token || token.length > SERVICE_TOKEN_MAX_CHARS || !constantTimeEqual(token, expected)) {
    throw new HttpError(401, "服务令牌无效", "unauthorized");
  }
  return (await sha256Hex(expected)).slice(0, 16);
}

function registry(env) {
  const namespace = env.CLASSIFY_REGISTRY;
  if (!namespace?.idFromName || !namespace?.get) {
    throw new HttpError(503, "分类结果存储（CLASSIFY_REGISTRY）尚未绑定", "worker_not_configured");
  }
  return namespace.get(namespace.idFromName(REGISTRY_NAME));
}

async function callRegistry(env, path, body) {
  const response = await registry(env).fetch(`https://classify-registry${path}`, body === undefined
    ? { method: "GET" }
    : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  return response;
}

async function registryJson(env, path, body) {
  const response = await callRegistry(env, path, body);
  if (response.status === 404) return null;
  if (!response.ok) throw new HttpError(502, "分类结果存储不可用", "registry_unavailable");
  return response.json();
}

function invalid(message) {
  return new HttpError(400, message, "invalid_request");
}

async function readCreateBody(request) {
  const declared = Number(request.headers.get("Content-Length"));
  if (Number.isFinite(declared) && declared > MAX_CREATE_BYTES) {
    throw new HttpError(413, "请求体超过安全上限", "request_too_large");
  }
  const bytes = await request.arrayBuffer();
  if (bytes.byteLength > MAX_CREATE_BYTES) throw new HttpError(413, "请求体超过安全上限", "request_too_large");
  try {
    return JSON.parse(new TextDecoder().decode(bytes));
  } catch {
    throw new HttpError(400, "请求 JSON 无效", "invalid_json");
  }
}

export function validateCreate(body) {
  if (!isRecord(body)) throw invalid("请求结构无效");
  const requestId = typeof body.request_id === "string" ? body.request_id : "";
  if (!ID_PATTERN.test(requestId)) throw invalid("request_id 必须是 1..128 个字母、数字或 . _ : -");
  const label = typeof body.label === "string" ? body.label : "";
  if (!LABEL_PATTERN.test(label)) throw invalid("label 必须是 1..64 个字母、数字或 . _ : -");
  const hasUrl = body.subscription_url !== undefined && body.subscription_url !== null;
  const hasYaml = body.subscription_yaml !== undefined && body.subscription_yaml !== null;
  if (hasUrl === hasYaml) throw invalid("subscription_url 与 subscription_yaml 必须且只能给一个");
  let subscription;
  if (hasUrl) {
    try {
      subscription = { url: assertTargetUrl(String(body.subscription_url).trim()).href };
    } catch {
      throw new HttpError(400, "订阅地址必须是公开的 http/https 链接，且不能携带账号密码", "invalid_subscription_url");
    }
  } else {
    if (typeof body.subscription_yaml !== "string" || !body.subscription_yaml.trim()) {
      throw invalid("subscription_yaml 必须是非空字符串");
    }
    if (new TextEncoder().encode(body.subscription_yaml).byteLength > MAX_YAML_BYTES) {
      throw new HttpError(413, "subscription_yaml 超过 5 MB", "request_too_large");
    }
    subscription = { yaml: body.subscription_yaml };
  }
  const profile = body.profile === undefined || body.profile === null ? "full" : body.profile;
  if (!PROFILES.includes(profile)) throw invalid("profile 只能是 classify 或 full");
  const reuseHours = body.reuse_hours === undefined || body.reuse_hours === null ? 0 : body.reuse_hours;
  if (typeof reuseHours !== "number" || !Number.isFinite(reuseHours) || reuseHours < 0 || reuseHours > MAX_REUSE_HOURS) {
    throw invalid(`reuse_hours 必须是 0..${MAX_REUSE_HOURS} 的数字`);
  }
  return { requestId, label, subscription, profile, reuseHours };
}

function newScanId() {
  const day = new Date().toISOString().slice(0, 10).replaceAll("-", "");
  const random = Array.from(crypto.getRandomValues(new Uint8Array(6)), (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `clf-${day}-${random}`;
}

function links(scanId) {
  return {
    status_url: `${SERVICE_PREFIX}classify/${scanId}`,
    result_url: `${SERVICE_PREFIX}classify/${scanId}/result`,
  };
}

function publicStatus(scan, progress = null) {
  return {
    scan_id: scan.scan_id,
    status: scan.status,
    progress: progress || { done: scan.status === "done" ? scan.node_count ?? null : 0, total: scan.node_count ?? null },
    error: scan.error ?? null,
    request_id: scan.request_id,
    label: scan.label,
    profile: scan.profile,
    created_at: new Date(scan.created_at).toISOString(),
    finished_at: scan.finished_at ? new Date(scan.finished_at).toISOString() : null,
  };
}

async function dispatch(env, scan, origin) {
  await githubJson(
    env,
    `/repos/${GITHUB_OWNER}/${GITHUB_REPOSITORY}/actions/workflows/${encodeURIComponent(GITHUB_WORKFLOW)}/dispatches`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ref: GITHUB_REF,
        inputs: { request_id: scan.scan_id, worker_url: origin, profile: scan.profile, channel: "api" },
      }),
    },
  );
}

async function createClassify(request, env, owner) {
  assertWorkerConfigured(env);
  const input = validateCreate(await readCreateBody(request));
  const created = await registryJson(env, "/create", {
    owner,
    scan_id: newScanId(),
    request_id: input.requestId,
    label: input.label,
    profile: input.profile,
    reuse_hours: input.reuseHours,
  });
  if (!created?.scan) throw new HttpError(502, "分类结果存储不可用", "registry_unavailable");
  const { scan } = created;
  if (created.existing) {
    return json({ ...publicStatus(scan), ...links(scan.scan_id), deduplicated: true }, 200, { "Cache-Control": "no-store" });
  }
  try {
    await stashSubscription(env, scan.scan_id, {
      ...input.subscription,
      classify: { owner: scan.owner, label: scan.label, reuse_hours: scan.reuse_hours },
    });
    await dispatch(env, scan, new URL(request.url).origin);
  } catch (error) {
    await dropSubscription(env, scan.scan_id);
    await callRegistry(env, "/finish", { scan_id: scan.scan_id, status: "failed", error: "dispatch_failed" }).catch(() => {});
    throw error;
  }
  return json({ scan_id: scan.scan_id, status: "queued", ...links(scan.scan_id) }, 202, { "Cache-Control": "no-store" });
}

async function readStub(stub, path) {
  try {
    const response = await stub.fetch(`https://scan-state${path}`, { method: "GET" });
    return response.ok ? await response.json() : null;
  } catch {
    return null;
  }
}

async function findApiRun(env, scanId) {
  const payload = await githubJson(
    env,
    `/repos/${GITHUB_OWNER}/${GITHUB_REPOSITORY}/actions/workflows/${encodeURIComponent(GITHUB_WORKFLOW)}/runs`,
    { query: { event: "workflow_dispatch", branch: GITHUB_REF, per_page: 50 } },
  );
  const runs = Array.isArray(payload?.workflow_runs) ? payload.workflow_runs : [];
  return runs.find((run) => run?.display_title === `${API_RUN_TITLE_PREFIX}${scanId}`) || null;
}

/**
 * queued / running 的扫描在读状态时顺手核对一次：runner 领过订阅就算 running；
 * GitHub 上那次运行已经结束却没交回结果就是 failed（runner 自己报失败时更早就落定了）。
 * GitHub 每分钟最多问一次，失败不影响读状态。
 */
async function refreshScan(env, scan) {
  if (scan.status !== "queued" && scan.status !== "running") return { scan, progress: null };
  const now = Date.now();
  const stub = scanStub(env, scan.scan_id);
  const [runner, report] = stub ? await Promise.all([readStub(stub, "/runner"), readStub(stub, "/report")]) : [null, null];
  const progress = isRecord(report) && Number.isSafeInteger(report.total)
    ? { done: report.completed, total: report.total }
    : null;
  let current = scan;
  if (current.status === "queued" && isRecord(runner)) {
    current = (await registryJson(env, "/update", { scan_id: scan.scan_id, status: "running" }))?.scan || current;
  }
  let failure = null;
  if (now - scan.created_at > ACTIVE_TTL_MS) {
    failure = "timeout";
  } else if (now - scan.created_at > GITHUB_CHECK_MIN_AGE_MS && now - (scan.checked_github_at || 0) > GITHUB_CHECK_INTERVAL_MS) {
    await callRegistry(env, "/update", { scan_id: scan.scan_id, checked_github_at: now }).catch(() => {});
    const run = await findApiRun(env, scan.scan_id).catch(() => undefined);
    if (run === null && now - scan.created_at > RUN_LOOKUP_GRACE_MS) failure = "run_not_found";
    if (run?.status === "completed") failure = `run_${String(run.conclusion || "failed").replace(/[^a-z_]/gu, "")}`;
  }
  if (failure) {
    // 注册表只在还没 done 时接受失败：runner 刚交回的结果不会被这里覆盖。
    const finished = await registryJson(env, "/finish", { scan_id: scan.scan_id, status: "failed", error: failure });
    if (finished?.scan) current = finished.scan;
  }
  return { scan: current, progress };
}

async function scanFor(env, owner, scanId) {
  if (!REQUEST_ID_PATTERN.test(scanId)) throw new HttpError(404, "扫描不存在", "not_found");
  const scan = (await registryJson(env, `/scan?id=${encodeURIComponent(scanId)}`))?.scan;
  if (!scan || scan.owner !== owner) throw new HttpError(404, "扫描不存在", "not_found");
  return scan;
}

function resultResponse(text) {
  return new Response(text, {
    status: 200,
    headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" },
  });
}

async function storedResult(env, path) {
  const response = await callRegistry(env, path);
  if (response.status === 404) return null;
  if (!response.ok) throw new HttpError(502, "分类结果存储不可用", "registry_unavailable");
  return response.text();
}

export async function serviceApi(request, env) {
  const url = new URL(request.url);
  const owner = await authenticateService(request, env);
  const path = url.pathname.slice(SERVICE_PREFIX.length);
  if (path === "health" && request.method === "GET") {
    return json({ status: "ok", api: "v1" }, 200, { "Cache-Control": "no-store" });
  }
  if (path === "classify" && request.method === "POST") return createClassify(request, env, owner);
  // 必须排在 classify/{id} 之前：否则 latest 会被当成一个 scan_id。
  if (path === "classify/latest" && request.method === "GET") {
    const label = url.searchParams.get("label") || "";
    if (!LABEL_PATTERN.test(label)) throw invalid("label 无效");
    const text = await storedResult(env, `/latest?owner=${owner}&label=${encodeURIComponent(label)}`);
    if (text === null) throw new HttpError(404, "这个 label 还没有完成的结果", "not_found");
    return resultResponse(text);
  }
  const match = path.match(/^classify\/([^/]+)(\/result)?$/u);
  if (!match || request.method !== "GET") {
    throw new HttpError(match ? 405 : 404, match ? "请求方法不支持" : "API 路径不存在", match ? "method_not_allowed" : "not_found");
  }
  let scanId;
  try {
    scanId = decodeURIComponent(match[1]);
  } catch {
    throw new HttpError(404, "扫描不存在", "not_found");
  }
  const { scan, progress } = await refreshScan(env, await scanFor(env, owner, scanId));
  if (!match[2]) return json(publicStatus(scan, progress), 200, { "Cache-Control": "no-store" });
  if (scan.status !== "done") {
    return json({ error: "not_ready", detail: "扫描还没有完成", scan_id: scan.scan_id, status: scan.status }, 409, { "Cache-Control": "no-store" });
  }
  const text = await storedResult(env, `/result?id=${encodeURIComponent(scan.scan_id)}`);
  if (text === null) throw new HttpError(410, "结果已过保留期", "result_expired");
  return resultResponse(text);
}

/** 领取订阅时给 runner 的 classify 上下文：label、复用时长与这个 label 最近一次的结果（要复用时才给）。 */
export async function classifyContext(env, stored) {
  if (!isRecord(stored)) return null;
  const reuseHours = Number(stored.reuse_hours) || 0;
  let previous = null;
  if (reuseHours > 0) {
    const query = `owner=${encodeURIComponent(stored.owner)}&label=${encodeURIComponent(stored.label)}`;
    const response = await callRegistry(env, `/latest?${query}`).catch(() => null);
    previous = response?.ok ? await response.json().catch(() => null) : null;
  }
  return { label: stored.label, reuse_hours: reuseHours, previous };
}

function validateResult(payload, scanId) {
  if (!isRecord(payload) || payload.schema_version !== 1 || payload.scan_id !== scanId ||
      !Array.isArray(payload.nodes) || payload.node_count !== payload.nodes.length) {
    return false;
  }
  return payload.nodes.every((node, index) => isRecord(node) && node.index === index &&
    NODE_STATUSES.includes(node.status) && (node.name === null || typeof node.name === "string") &&
    !("uuid" in node) && !("password" in node));
}

/** POST /api/scan-result：runner 交回分类结果（或失败码），凭领取订阅时拿到的 runner 令牌。 */
export async function scanResultReport(request, env) {
  try {
    if (request.method !== "POST") throw new HttpError(405, "请求方法不支持", "method_not_allowed");
    const runner = await assertRunnerToken(request, env);
    const declared = Number(request.headers.get("Content-Length"));
    if (Number.isFinite(declared) && declared > MAX_RESULT_CHARS * 4) throw invalid("结果过大");
    const raw = await request.text();
    if (raw.length > MAX_RESULT_CHARS) throw invalid("结果过大");
    let payload;
    try {
      payload = JSON.parse(raw);
    } catch {
      throw invalid("结果 JSON 无效");
    }
    let body;
    if (isRecord(payload) && payload.status === "failed") {
      const code = typeof payload.error === "string" && ERROR_CODE_PATTERN.test(payload.error) ? payload.error : "scan_failed";
      body = { scan_id: runner.request_id, status: "failed", error: code };
    } else {
      if (!validateResult(payload, runner.request_id)) throw invalid("分类结果结构无效");
      body = { scan_id: runner.request_id, status: "done", result: payload };
    }
    const response = await callRegistry(env, "/finish", body);
    if (response.status === 404) throw new HttpError(404, "没有这次分类扫描", "not_found");
    if (response.status === 409) throw new HttpError(409, "这次扫描已经结束", "already_finished");
    if (!response.ok) throw new HttpError(502, "分类结果没有写入", "registry_unavailable");
    return new Response(null, { status: 204, headers: { "Cache-Control": "no-store" } });
  } catch (error) {
    if (error instanceof HttpError) return json({ error: error.code }, error.status, { "Cache-Control": "no-store" });
    throw error;
  }
}

/**
 * 全站一个实例的分类扫描注册表。存的是：
 * - `scan:<id>`：扫描元数据（调用方标识、request_id、label、profile、状态、时间）；
 * - `req:<owner>:<request_id>`：幂等键 → scan_id；
 * - `active:<owner>`：这个调用方正在跑的 scan_id（同一令牌同时只跑一个）；
 * - `result:<id>:<n>`：结果 JSON 分片（单值 ≤ 512K 字符，低于 SQLite 后端 2MB 上限）；
 * - `latest:<owner>:<label>`：最近一次 done 的 scan_id。
 * 每天清一次 8 天前的扫描；仍是某个 label「最近一次」的那份不清。
 */
export class ClassifyRegistry {
  constructor(state) {
    this.state = state;
    this.storage = state.storage;
  }

  async fetch(request) {
    const url = new URL(request.url);
    const body = request.method === "POST" ? await request.json().catch(() => null) : null;
    await this.arm();
    switch (url.pathname) {
      case "/create": return this.create(body);
      case "/update": return this.update(body);
      case "/finish": return this.finish(body);
      case "/scan": return this.scan(url.searchParams.get("id"));
      case "/result": return this.result(url.searchParams.get("id"));
      case "/latest": return this.latest(url.searchParams);
      default: return new Response(null, { status: 404 });
    }
  }

  async create(body) {
    if (!isRecord(body) || !REQUEST_ID_PATTERN.test(String(body.scan_id || ""))) return new Response(null, { status: 400 });
    const now = Date.now();
    const requestKey = `req:${body.owner}:${body.request_id}`;
    const knownId = await this.storage.get(requestKey);
    const known = knownId ? await this.storage.get(`scan:${knownId}`) : null;
    if (known) return Response.json({ scan: known, existing: true });
    const activeKey = `active:${body.owner}`;
    const activeId = await this.storage.get(activeKey);
    const active = activeId ? await this.storage.get(`scan:${activeId}`) : null;
    if (active && ["queued", "running"].includes(active.status) && now - active.created_at < ACTIVE_TTL_MS) {
      return Response.json({ scan: active, existing: true });
    }
    const scan = {
      scan_id: body.scan_id,
      owner: body.owner,
      request_id: body.request_id,
      label: body.label,
      profile: body.profile,
      reuse_hours: body.reuse_hours,
      status: "queued",
      error: null,
      node_count: null,
      created_at: now,
      finished_at: null,
      checked_github_at: 0,
    };
    await this.storage.put({ [`scan:${scan.scan_id}`]: scan, [requestKey]: scan.scan_id, [activeKey]: scan.scan_id });
    return Response.json({ scan, existing: false });
  }

  async update(body) {
    const scan = isRecord(body) ? await this.storage.get(`scan:${body.scan_id}`) : null;
    if (!scan) return new Response(null, { status: 404 });
    if (body.status === "running" && scan.status === "queued") scan.status = "running";
    if (Number.isSafeInteger(body.checked_github_at)) scan.checked_github_at = body.checked_github_at;
    await this.storage.put(`scan:${scan.scan_id}`, scan);
    return Response.json({ scan });
  }

  async finish(body) {
    const scan = isRecord(body) ? await this.storage.get(`scan:${body.scan_id}`) : null;
    if (!scan) return new Response(null, { status: 404 });
    if (scan.status === "done" || scan.status === "failed") return new Response(null, { status: 409 });
    scan.finished_at = Date.now();
    const writes = {};
    if (body.status === "done" && isRecord(body.result)) {
      // label 与 scan_id 以注册表为准：结果里这两项由 Worker 写定，不信 runner。
      const text = JSON.stringify({ ...body.result, scan_id: scan.scan_id, label: scan.label });
      const chunks = Math.ceil(text.length / RESULT_CHUNK_CHARS);
      for (let index = 0; index < chunks; index += 1) {
        writes[`result:${scan.scan_id}:${index}`] = text.slice(index * RESULT_CHUNK_CHARS, (index + 1) * RESULT_CHUNK_CHARS);
      }
      scan.status = "done";
      scan.error = null;
      scan.result_chunks = chunks;
      scan.node_count = body.result.node_count;
      writes[`latest:${scan.owner}:${scan.label}`] = scan.scan_id;
    } else {
      scan.status = "failed";
      scan.error = typeof body.error === "string" && ERROR_CODE_PATTERN.test(body.error) ? body.error : "scan_failed";
    }
    writes[`scan:${scan.scan_id}`] = scan;
    await this.storage.put(writes);
    if (await this.storage.get(`active:${scan.owner}`) === scan.scan_id) await this.storage.delete(`active:${scan.owner}`);
    return Response.json({ scan });
  }

  async scan(id) {
    const scan = id ? await this.storage.get(`scan:${id}`) : null;
    return scan ? Response.json({ scan }) : new Response(null, { status: 404 });
  }

  async readResult(scan) {
    if (!scan || scan.status !== "done" || !Number.isSafeInteger(scan.result_chunks)) return null;
    const keys = Array.from({ length: scan.result_chunks }, (_, index) => `result:${scan.scan_id}:${index}`);
    const parts = await this.storage.get(keys);
    if (keys.some((key) => typeof parts.get(key) !== "string")) return null;
    return keys.map((key) => parts.get(key)).join("");
  }

  async result(id) {
    const text = await this.readResult(id ? await this.storage.get(`scan:${id}`) : null);
    return text === null ? new Response(null, { status: 404 }) : new Response(text, { headers: { "Content-Type": "application/json" } });
  }

  async latest(params) {
    const label = params.get("label") || "";
    const id = await this.storage.get(`latest:${params.get("owner")}:${label}`);
    return this.result(id);
  }

  async arm() {
    if (!(await this.storage.getAlarm())) await this.storage.setAlarm(Date.now() + CLEANUP_INTERVAL_MS);
  }

  async alarm() {
    const now = Date.now();
    const keep = new Set((await this.storage.list({ prefix: "latest" })).values());
    const scans = await this.storage.list({ prefix: "scan:" });
    for (const scan of scans.values()) {
      if (!isRecord(scan) || now - scan.created_at < RETENTION_MS || keep.has(scan.scan_id)) continue;
      const keys = [`scan:${scan.scan_id}`, `req:${scan.owner}:${scan.request_id}`];
      for (let index = 0; index < (scan.result_chunks || 0); index += 1) keys.push(`result:${scan.scan_id}:${index}`);
      await this.storage.delete(keys);
    }
    await this.storage.setAlarm(now + CLEANUP_INTERVAL_MS);
  }
}
