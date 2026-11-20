// /api/v1/classify 的离线测试：真实的 Worker 路由与两个 Durable Object 类，存储换成内存版，
// GitHub API 换成替身。覆盖鉴权、幂等、同一令牌只跑一个、内联 YAML 分片暂存与领取、
// runner 交回结果 / 失败、409、latest、复用上下文、GitHub 侧失败发现和 8 天清理。
// 用法：node scripts/check_service_api.mjs（npm run check 会跑）。

import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { fetchHandler } from "../worker/router.js";
import { ScanProgress } from "../worker/progress.js";
import { ClassifyRegistry, classifyContext } from "../worker/service.js";

class MemoryStorage {
  constructor() {
    this.map = new Map();
    this.alarm = null;
  }
  async get(key) {
    if (Array.isArray(key)) return new Map(key.filter((k) => this.map.has(k)).map((k) => [k, structuredClone(this.map.get(k))]));
    return this.map.has(key) ? structuredClone(this.map.get(key)) : undefined;
  }
  async put(key, value) {
    const entries = typeof key === "string" ? [[key, value]] : Object.entries(key);
    for (const [k, v] of entries) this.map.set(k, structuredClone(v));
  }
  async delete(key) {
    for (const k of Array.isArray(key) ? key : [key]) this.map.delete(k);
  }
  async deleteAll() {
    this.map.clear();
  }
  async list({ prefix = "" } = {}) {
    return new Map([...this.map].filter(([k]) => k.startsWith(prefix)).map(([k, v]) => [k, structuredClone(v)]));
  }
  async getAlarm() {
    return this.alarm;
  }
  async setAlarm(time) {
    this.alarm = time;
  }
}

class Namespace {
  constructor(Klass) {
    this.Klass = Klass;
    this.objects = new Map();
  }
  idFromName(name) {
    return name;
  }
  object(id) {
    if (!this.objects.has(id)) this.objects.set(id, new this.Klass({ storage: new MemoryStorage() }));
    return this.objects.get(id);
  }
  get(id) {
    return { fetch: (input, init) => this.object(id).fetch(new Request(input, init)) };
  }
}

const TOKEN = "t".repeat(40);
const ORIGIN = "https://best-ip.example.workers.dev";
const github = { dispatches: [], runs: [] };
globalThis.fetch = async (input, init = {}) => {
  const url = new URL(input instanceof Request ? input.url : String(input));
  if (url.hostname !== "api.github.com") throw new Error(`unexpected fetch ${url}`);
  if (url.pathname.endsWith("/dispatches")) {
    github.dispatches.push(JSON.parse(init.body));
    return new Response(null, { status: 204 });
  }
  if (url.pathname.endsWith("/runs")) return Response.json({ workflow_runs: github.runs });
  throw new Error(`unexpected GitHub call ${url.pathname}`);
};

function environment() {
  return {
    GITHUB_TOKEN: "github-pat-for-tests",
    SITE_PASSWORD: "p".repeat(20),
    BESTIP_SERVICE_TOKEN: TOKEN,
    SCAN_PROGRESS: new Namespace(ScanProgress),
    CLASSIFY_REGISTRY: new Namespace(ClassifyRegistry),
  };
}

async function call(env, method, path, { body, token = TOKEN, headers = {} } = {}) {
  const init = { method, headers: { ...headers } };
  if (token !== null) init.headers.Authorization = `Bearer ${token}`;
  if (body !== undefined) {
    init.body = typeof body === "string" ? body : JSON.stringify(body);
    init.headers["Content-Type"] = "application/json";
  }
  let response;
  try {
    response = await fetchHandler(new Request(`${ORIGIN}${path}`, init), env, {});
  } catch (error) {
    if (!error.status) console.error(error);
    response = Response.json({ error: error.code }, { status: error.status || 500 });
  }
  const text = await response.text();
  let data = null;
  try {
    data = JSON.parse(text);
  } catch {
    data = text;
  }
  return { status: response.status, data, text, headers: response.headers };
}

const sha256 = (value) => createHash("sha256").update(value).digest("hex");

async function claim(env, scanId) {
  const token = `${scanId}.${"r".repeat(43)}`;
  const response = await env.SCAN_PROGRESS.get(scanId).fetch("https://scan-state/claim", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ run_id: 99, run_attempt: 1, token_sha256: sha256(token) }),
  });
  assert.equal(response.status, 200);
  return { token, stored: await response.json() };
}

function resultFor(scanId, names) {
  return {
    schema_version: 1,
    scan_id: scanId,
    label: "spoofed-by-runner",
    profile: "classify",
    started_at: "2026-11-20T19:17:00Z",
    finished_at: "2026-11-20T19:41:12Z",
    input_sha256: "0".repeat(64),
    node_count: names.length,
    nodes: names.map((name, index) => ({ name, index, fingerprint: "0123456789abcdef", status: "ok" })),
  };
}

const checks = [];
async function check(name, body) {
  await body();
  checks.push(name);
  console.log(`PASS ${name}`);
}

const env = environment();
const YAML = `proxies:\n${"  - {name: 节点, type: ss, server: 1.1.1.1, port: 1, password: secret-pw}\n".repeat(1)}# ${"x".repeat(600 * 1024)}\n`;
let first;

await check("未配置服务令牌时 503，令牌错误时 401，网页接口仍要密码会话", async () => {
  const bare = environment();
  delete bare.BESTIP_SERVICE_TOKEN;
  assert.equal((await call(bare, "GET", "/api/v1/health")).status, 503);
  assert.equal((await call(env, "GET", "/api/v1/health", { token: "wrong".repeat(10) })).status, 401);
  assert.equal((await call(env, "GET", "/api/v1/health", { token: null })).status, 401);
  assert.deepEqual((await call(env, "GET", "/api/v1/health")).data, { status: "ok", api: "v1" });
  assert.equal((await call(env, "GET", "/api/scans/latest", { token: null })).status, 401);
});

await check("参数校验：二选一、profile、reuse_hours、label", async () => {
  const base = { request_id: "myclash-abcd1234-20261120", label: "myclash:abcd1234" };
  assert.equal((await call(env, "POST", "/api/v1/classify", { body: base })).status, 400);
  assert.equal((await call(env, "POST", "/api/v1/classify", {
    body: { ...base, subscription_yaml: "a", subscription_url: "https://example.com/x" },
  })).status, 400);
  assert.equal((await call(env, "POST", "/api/v1/classify", {
    body: { ...base, subscription_url: "http://127.0.0.1/sub" },
  })).data.error, "invalid_subscription_url");
  assert.equal((await call(env, "POST", "/api/v1/classify", { body: { ...base, subscription_yaml: "a", profile: "x" } })).status, 400);
  assert.equal((await call(env, "POST", "/api/v1/classify", { body: { ...base, subscription_yaml: "a", reuse_hours: -1 } })).status, 400);
  assert.equal((await call(env, "POST", "/api/v1/classify", { body: { ...base, label: "bad label", subscription_yaml: "a" } })).status, 400);
  assert.equal(github.dispatches.length, 0);
});

await check("创建扫描：202、派发 api 通道、内联 YAML 分片暂存", async () => {
  const response = await call(env, "POST", "/api/v1/classify", {
    body: {
      request_id: "myclash-abcd1234-20261120",
      label: "myclash:abcd1234",
      subscription_yaml: YAML,
      profile: "classify",
      reuse_hours: 20,
    },
  });
  assert.equal(response.status, 202);
  first = response.data.scan_id;
  assert.match(first, /^clf-\d{8}-[0-9a-f]{12}$/u);
  assert.deepEqual(response.data, {
    scan_id: first, status: "queued",
    status_url: `/api/v1/classify/${first}`, result_url: `/api/v1/classify/${first}/result`,
  });
  assert.deepEqual(github.dispatches.at(-1), {
    ref: "main", inputs: { request_id: first, worker_url: ORIGIN, profile: "classify", channel: "api" },
  });
  const storage = env.SCAN_PROGRESS.object(first).state.storage;
  assert.ok((await storage.get("subscription")).yaml_chunks >= 2);
  // 注册表里没有订阅内容。
  assert.ok(!JSON.stringify([...env.CLASSIFY_REGISTRY.object("classify-registry-v1").state.storage.map]).includes("secret-pw"));
});

await check("幂等与单活：同 request_id、或已有扫描在跑时返回同一个 scan_id", async () => {
  const again = await call(env, "POST", "/api/v1/classify", {
    body: { request_id: "myclash-abcd1234-20261120", label: "myclash:abcd1234", subscription_yaml: "proxies: []" },
  });
  assert.equal(again.status, 200);
  assert.equal(again.data.scan_id, first);
  assert.equal(again.data.deduplicated, true);
  const other = await call(env, "POST", "/api/v1/classify", {
    body: { request_id: "myclash-other-20261120", label: "myclash:other", subscription_url: "https://example.com/sub" },
  });
  assert.equal(other.data.scan_id, first);
  assert.equal(github.dispatches.length, 1);
});

await check("状态：queued → 领取后 running；未完成取结果 409", async () => {
  const status = await call(env, "GET", `/api/v1/classify/${first}`);
  assert.equal(status.status, 200);
  assert.equal(status.data.status, "queued");
  assert.deepEqual(status.data.progress, { done: 0, total: null });
  const { stored } = await claim(env, first);
  assert.equal(stored.yaml, YAML);
  assert.deepEqual(stored.classify, { owner: stored.classify.owner, label: "myclash:abcd1234", reuse_hours: 20 });
  assert.equal(await env.SCAN_PROGRESS.object(first).state.storage.get("subscription_yaml:0"), undefined);
  assert.equal((await call(env, "GET", `/api/v1/classify/${first}`)).data.status, "running");
  const early = await call(env, "GET", `/api/v1/classify/${first}/result`);
  assert.equal(early.status, 409);
  assert.equal(early.data.status, "running");
  assert.equal((await call(env, "GET", "/api/v1/classify/latest?label=myclash:abcd1234")).status, 404);
});

await check("runner 交回结果：令牌与结构校验，done 后可取结果与 latest", async () => {
  const token = `${first}.${"r".repeat(43)}`;
  const post = (body, relayToken = token) => call(env, "POST", "/api/scan-result", {
    body, token: null, headers: { "X-Best-IP-Relay-Token": relayToken },
  });
  assert.equal((await post(resultFor(first, ["a"]), `${first}.${"z".repeat(43)}`)).status, 401);
  assert.equal((await post({ ...resultFor(first, ["a"]), node_count: 2 })).status, 400);
  assert.equal((await post(resultFor("clf-other", ["a"]))).status, 400);
  assert.equal((await post(resultFor(first, ["🇭🇰 香港 01", "🇭🇰 香港 01"]))).status, 204);
  assert.equal((await post(resultFor(first, ["late"]))).status, 409);
  const status = await call(env, "GET", `/api/v1/classify/${first}`);
  assert.equal(status.data.status, "done");
  assert.deepEqual(status.data.progress, { done: 2, total: 2 });
  const result = await call(env, "GET", `/api/v1/classify/${first}/result`);
  assert.equal(result.status, 200);
  assert.match(result.headers.get("Content-Type"), /^application\/json/u);
  assert.equal(result.data.label, "myclash:abcd1234");
  assert.deepEqual(result.data.nodes.map((node) => node.name), ["🇭🇰 香港 01", "🇭🇰 香港 01"]);
  const latest = await call(env, "GET", "/api/v1/classify/latest?label=myclash:abcd1234");
  assert.equal(latest.text, result.text);
  assert.equal((await call(env, "GET", `/api/v1/classify/${first}`, { token: "x".repeat(40) })).status, 401);
});

let second;
await check("上一次结束后可以开新扫描；领取时带上复用上下文（上一次结果）", async () => {
  const response = await call(env, "POST", "/api/v1/classify", {
    body: { request_id: "myclash-abcd1234-20261121", label: "myclash:abcd1234", subscription_url: "https://example.com/sub", reuse_hours: 20 },
  });
  assert.equal(response.status, 202);
  second = response.data.scan_id;
  assert.notEqual(second, first);
  const { stored } = await claim(env, second);
  assert.equal(stored.url, "https://example.com/sub");
  const context = await classifyContext(env, stored.classify);
  assert.equal(context.reuse_hours, 20);
  assert.equal(context.previous.scan_id, first);
  assert.equal(context.previous.nodes.length, 2);
});

await check("runner 报失败：状态 failed，latest 仍是上一次 done", async () => {
  const token = `${second}.${"r".repeat(43)}`;
  const response = await call(env, "POST", "/api/scan-result", {
    body: { status: "failed", error: "scan_timeout" }, token: null, headers: { "X-Best-IP-Relay-Token": token },
  });
  assert.equal(response.status, 204);
  const status = await call(env, "GET", `/api/v1/classify/${second}`);
  assert.equal(status.data.status, "failed");
  assert.equal(status.data.error, "scan_timeout");
  assert.equal((await call(env, "GET", `/api/v1/classify/${second}/result`)).status, 409);
  assert.equal((await call(env, "GET", "/api/v1/classify/latest?label=myclash:abcd1234")).data.scan_id, first);
});

await check("GitHub 上运行已结束却没交回结果：读状态时标记 failed", async () => {
  const response = await call(env, "POST", "/api/v1/classify", {
    body: { request_id: "myclash-abcd1234-20261122", label: "myclash:abcd1234", subscription_url: "https://example.com/sub" },
  });
  const third = response.data.scan_id;
  const storage = env.CLASSIFY_REGISTRY.object("classify-registry-v1").state.storage;
  const scan = await storage.get(`scan:${third}`);
  await storage.put(`scan:${third}`, { ...scan, created_at: Date.now() - 5 * 60 * 1000 });
  github.runs = [{ display_title: `Best IP api ${third}`, status: "completed", conclusion: "failure" }];
  const status = await call(env, "GET", `/api/v1/classify/${third}`);
  assert.equal(status.data.status, "failed");
  assert.equal(status.data.error, "run_failure");
  assert.equal((await storage.get(`active:${scan.owner}`)), undefined);
});

await check("8 天清理：过期扫描删掉，label 最近一次 done 的保留", async () => {
  const registry = env.CLASSIFY_REGISTRY.object("classify-registry-v1");
  const storage = registry.state.storage;
  const old = Date.now() - 9 * 24 * 60 * 60 * 1000;
  for (const [key, value] of await storage.list({ prefix: "scan:" })) await storage.put(key, { ...value, created_at: old });
  await registry.alarm();
  assert.ok(await storage.get(`scan:${first}`));
  assert.equal(await storage.get(`scan:${second}`), undefined);
  assert.equal((await call(env, "GET", "/api/v1/classify/latest?label=myclash:abcd1234")).data.scan_id, first);
});

console.log(`\n全部 ${checks.length} 组断言通过`);
