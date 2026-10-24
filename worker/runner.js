import { HttpError, json } from "./responses.js";
import { REQUEST_ID_PATTERN, isRecord } from "./config.js";
import { base64UrlEncode } from "./auth.js";
import { verifyRunnerIdentity } from "./oidc.js";
import { constantTimeEqual } from "./relay.js";

// 订阅地址怎么到 runner 手里：页面把它交给 Worker，Worker 暂存进这次 request_id 的 Durable Object
// （15 分钟过期），只把 request_id 和本站 origin 放进 workflow 输入——订阅地址从不进入 GitHub。
// runner 凭 GitHub Actions OIDC（worker/oidc.js）来领一次，领完即删；同时领到一张只对这次运行有效
// 的随机 runner 令牌，之后的订阅中继与进度回报都凭它。没有任何需要两边配置一致的共享密钥。
export const SCAN_CLAIM_PATH = "/api/scan-claim";
export const RUNNER_TOKEN_HEADER = "X-Best-IP-Relay-Token";
const MAX_CLAIM_CHARS = 1024;
const MAX_RUNNER_TOKEN_CHARS = 256;

export function scanStub(env, requestId) {
  const namespace = env.SCAN_PROGRESS;
  if (!namespace?.idFromName || !namespace?.get) return null;
  return namespace.get(namespace.idFromName(requestId));
}

function requireStub(env, requestId) {
  const stub = scanStub(env, requestId);
  if (!stub) throw new HttpError(503, "扫描状态存储（SCAN_PROGRESS）尚未绑定", "worker_not_configured");
  return stub;
}

async function sha256Hex(value) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

/** 发起扫描时暂存订阅地址；同一个 request_id 已有一份时拒绝覆盖。 */
export async function stashSubscription(env, requestId, url) {
  const response = await requireStub(env, requestId).fetch("https://scan-state/subscription", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ url }),
  });
  if (response.status === 409) throw new HttpError(409, "request_id 已被使用", "request_id_conflict");
  if (!response.ok) throw new HttpError(502, "订阅地址没有暂存成功", "scan_state_unavailable");
}

/** workflow 没派发出去时把暂存的订阅地址删掉，不留到过期。尽力而为。 */
export async function dropSubscription(env, requestId) {
  await scanStub(env, requestId)?.fetch("https://scan-state/subscription", { method: "DELETE" }).catch(() => {});
}

/** POST /api/scan-claim：runner 凭 OIDC 领取订阅地址与 runner 令牌，每个 request_id 只能领一次。 */
export async function claimSubscription(request, env) {
  try {
    if (request.method !== "POST") throw new HttpError(405, "请求方法不支持", "method_not_allowed");
    const identity = await verifyRunnerIdentity(request);
    let body;
    try {
      const raw = await request.text();
      if (raw.length > MAX_CLAIM_CHARS) throw new Error("size");
      body = JSON.parse(raw);
    } catch {
      throw new HttpError(400, "领取请求无效", "invalid_request");
    }
    const requestId = isRecord(body) && typeof body.request_id === "string" ? body.request_id : "";
    if (!REQUEST_ID_PATTERN.test(requestId)) throw new HttpError(400, "request_id 无效", "invalid_request");
    const token = `${requestId}.${base64UrlEncode(crypto.getRandomValues(new Uint8Array(32)))}`;
    const response = await requireStub(env, requestId).fetch("https://scan-state/claim", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...identity, token_sha256: await sha256Hex(token) }),
    });
    if (response.status === 410) throw new HttpError(410, "订阅地址已被领取或已过期", "subscription_gone");
    const stored = response.ok ? await response.json().catch(() => null) : null;
    if (typeof stored?.url !== "string") throw new HttpError(502, "订阅地址读取失败", "scan_state_unavailable");
    return json({ subscription_url: stored.url, runner_token: token }, 200, { "Cache-Control": "no-store" });
  } catch (error) {
    if (error instanceof HttpError) return json({ error: error.code }, error.status, { "Cache-Control": "no-store" });
    throw error;
  }
}

/** 订阅中继与进度回报的门：runner 令牌必须是这次 request_id 领取时发的那一张。返回运行身份。 */
export async function assertRunnerToken(request, env) {
  const token = request.headers.get(RUNNER_TOKEN_HEADER) || "";
  const separator = token.lastIndexOf(".");
  const requestId = separator > 0 ? token.slice(0, separator) : "";
  if (token.length > MAX_RUNNER_TOKEN_CHARS || !REQUEST_ID_PATTERN.test(requestId)) {
    throw new HttpError(401, "runner 令牌无效", "unauthorized");
  }
  const stub = scanStub(env, requestId);
  if (!stub) throw new HttpError(503, "扫描状态存储（SCAN_PROGRESS）尚未绑定", "worker_not_configured");
  const response = await stub.fetch("https://scan-state/runner", { method: "GET" });
  const runner = response.ok ? await response.json().catch(() => null) : null;
  if (!isRecord(runner) || typeof runner.token_sha256 !== "string" ||
      !constantTimeEqual(await sha256Hex(token), runner.token_sha256)) {
    throw new HttpError(401, "runner 令牌无效", "unauthorized");
  }
  return { request_id: requestId, run_id: runner.run_id, run_attempt: runner.run_attempt };
}
