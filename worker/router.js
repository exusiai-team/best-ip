import { HttpError, json, secureResponse } from "./responses.js";
import { assertWorkerConfigured } from "./config.js";
import { authenticate, assertSameOrigin } from "./auth.js";
import { createScan, scanState, cancelScan, resolveScanRun } from "./scans.js";
import { downloadArtifact } from "./artifacts.js";
import { latestScanState } from "./latest.js";
import { loginRoute, loginRedirect } from "./login.js";
import { SUBSCRIPTION_RELAY_PATH, subscriptionRelay } from "./relay.js";
import { SCAN_PROGRESS_PATH, scanProgressFeed, scanProgressReport } from "./progress.js";
import { SCAN_CLAIM_PATH, claimSubscription } from "./runner.js";

export async function api(request, env, auth) {
  const url = new URL(request.url);
  if (url.pathname === "/api/health" && request.method === "GET") {
    assertWorkerConfigured(env);
    return json({ status: "ok", mode: "github-actions-gateway", authentication: auth.method }, 200, { "Cache-Control": "no-store" });
  }
  if (url.pathname === "/api/scans" && request.method === "POST") return createScan(request, env);
  // 必须排在下面那条 /api/scans/<id> 正则之前：否则 "latest" 会被当成一个 request_id 去验扫描 token。
  if (url.pathname === "/api/scans/latest" && request.method === "GET") return latestScanState(env);
  const match = url.pathname.match(/^\/api\/scans\/([^/]+)(\/artifact|\/progress)?$/u);
  if (!match) throw new HttpError(404, "API 路径不存在", "not_found");
  let requestId;
  try {
    requestId = decodeURIComponent(match[1]);
  } catch {
    throw new HttpError(400, "request_id 无效", "invalid_request");
  }
  // 逐节点的实时读数：只读 Durable Object，不要求 scan token（见 worker/progress.js 的 scanProgressFeed）。
  if (match[2] === "/progress" && request.method === "GET") return scanProgressFeed(request, env, requestId);
  if (match[2] === "/artifact" && request.method === "GET") {
    const run = await resolveScanRun(request, env, requestId);
    return downloadArtifact(env, run, requestId);
  }
  if (!match[2] && request.method === "GET") return scanState(request, env, requestId);
  if (!match[2] && request.method === "DELETE") return cancelScan(request, env, requestId);
  throw new HttpError(405, "请求方法不支持", "method_not_allowed");
}

export async function fetchHandler(request, env, ctx) {
  const url = new URL(request.url);
  // Server-to-server endpoint: it runs before the site password gate and never
  // takes the browser same-origin check, because the caller is an Actions runner.
  if (url.pathname === SUBSCRIPTION_RELAY_PATH) {
    return secureResponse(await subscriptionRelay(request, env), { noStore: true });
  }
  // 同上：runner 凭 GitHub Actions OIDC 领取这次扫描的订阅地址与 runner 令牌。
  if (url.pathname === SCAN_CLAIM_PATH) {
    return secureResponse(await claimSubscription(request, env), { noStore: true });
  }
  // 同上：runner 回报扫描进度（计数与逐节点阶段，不含出口 IP 与评分），凭同一张 runner 令牌。
  if (url.pathname === SCAN_PROGRESS_PATH) {
    return secureResponse(await scanProgressReport(request, env), { noStore: true });
  }
  const login = await loginRoute(request, env);
  if (login) return secureResponse(login, { noStore: true });
  let auth;
  try {
    auth = await authenticate(request, env, ctx);
  } catch (error) {
    if (error.status === 401 && request.method === "GET" &&
        !url.pathname.startsWith("/api/") && request.headers.get("Accept")?.includes("text/html")) {
      return secureResponse(loginRedirect(), { noStore: true });
    }
    throw error;
  }
  if (url.pathname.startsWith("/api/")) {
    assertSameOrigin(request);
    return secureResponse(await api(request, env, auth), { noStore: true });
  }
  if (url.pathname === "/site-config.js") {
    // 这个文件是「这次部署怎么跑」的标记，不是「扫描能力齐不齐」的检查：扫描 secrets 缺失时也要照发。
    // 否则页面读不到配置就退回示例数据，线上会安静地显示一屏合成 IP——一个配置疏漏装成了设计示例。
    // 能力检查交给 /api/* 的 assertWorkerConfigured：那里缺配置就该明确 503。
    return secureResponse(
      new Response(
        `window.BEST_IP_CONFIG = Object.freeze(${JSON.stringify({ mode: "gateway" })});`,
        { headers: { "Content-Type": "application/javascript; charset=utf-8", "Cache-Control": "no-store" } },
      ),
      { noStore: true },
    );
  }
  if (!env.ASSETS?.fetch) throw new HttpError(503, "Worker 静态资源尚未绑定", "worker_not_configured");
  return secureResponse(await env.ASSETS.fetch(request), { noStore: true });
}
