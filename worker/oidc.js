import { HttpError } from "./responses.js";
import { base64UrlDecode } from "./auth.js";
import { GITHUB_OWNER, GITHUB_REPOSITORY, GITHUB_WORKFLOW_PATH, GITHUB_REF, positiveInteger } from "./config.js";

// runner 证明「我是本仓库 main 上 scan.yml 的这一次运行」靠 GitHub Actions OIDC：工作流声明
// `id-token: write`，向 GitHub 要一张 audience 等于本 Worker origin 的 JWT，Worker 用 GitHub
// 公开的 JWKS 验签并核对声明。双方之间没有任何共享密钥需要配置、轮换或保持一致。
const ISSUER = "https://token.actions.githubusercontent.com";
const JWKS_URL = `${ISSUER}/.well-known/jwks`;
const JWKS_TTL_MS = 60 * 60 * 1000;
// kid 不认识时最多这么频繁地重取一次 JWKS：GitHub 轮换签名密钥时能跟上，伪造的 kid 也刷不爆上游。
const JWKS_REFRESH_MS = 60 * 1000;
const CLOCK_SKEW_SECONDS = 60;
const MAX_TOKEN_CHARS = 8192;

let jwksCache = { keys: new Map(), fetchedAt: 0 };

function unauthorized() {
  return new HttpError(401, "runner 身份无效", "unauthorized");
}

function decodeJson(segment) {
  try {
    return JSON.parse(new TextDecoder().decode(base64UrlDecode(segment)));
  } catch {
    throw unauthorized();
  }
}

async function loadJwks(force) {
  const now = Date.now();
  const fresh = now - jwksCache.fetchedAt < (force ? JWKS_REFRESH_MS : JWKS_TTL_MS);
  if (fresh) return jwksCache.keys;
  const response = await fetch(JWKS_URL, { signal: AbortSignal.timeout(5_000) }).catch(() => null);
  const payload = response?.ok ? await response.json().catch(() => null) : null;
  if (!Array.isArray(payload?.keys)) {
    if (jwksCache.keys.size) return jwksCache.keys;
    throw new HttpError(502, "取不到 GitHub OIDC 公钥", "oidc_unavailable");
  }
  const keys = new Map();
  for (const jwk of payload.keys) {
    if (jwk?.kty !== "RSA" || typeof jwk.kid !== "string") continue;
    keys.set(jwk.kid, crypto.subtle.importKey(
      "jwk", { kty: "RSA", n: jwk.n, e: jwk.e, alg: "RS256", ext: true },
      { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"],
    ));
  }
  jwksCache = { keys, fetchedAt: now };
  return keys;
}

async function keyFor(kid) {
  let keys = await loadJwks(false);
  if (!keys.has(kid)) keys = await loadJwks(true);
  const key = keys.get(kid);
  if (!key) throw unauthorized();
  return key;
}

/**
 * 校验 `Authorization: Bearer <GitHub OIDC JWT>`，返回这次运行的身份 { run_id, run_attempt }。
 * 只认：本仓库、main 分支、scan.yml、workflow_dispatch，audience 等于本 Worker 的 origin。
 */
export async function verifyRunnerIdentity(request) {
  const header = request.headers.get("Authorization") || "";
  const token = header.startsWith("Bearer ") ? header.slice(7).trim() : "";
  const parts = token.split(".");
  if (!token || token.length > MAX_TOKEN_CHARS || parts.length !== 3) throw unauthorized();
  const [encodedHeader, encodedPayload, encodedSignature] = parts;
  const jose = decodeJson(encodedHeader);
  if (jose?.alg !== "RS256" || typeof jose.kid !== "string") throw unauthorized();
  let valid = false;
  try {
    valid = await crypto.subtle.verify(
      "RSASSA-PKCS1-v1_5",
      await keyFor(jose.kid),
      base64UrlDecode(encodedSignature),
      new TextEncoder().encode(`${encodedHeader}.${encodedPayload}`),
    );
  } catch (error) {
    if (error instanceof HttpError && error.status !== 401) throw error;
    throw unauthorized();
  }
  if (!valid) throw unauthorized();
  const claims = decodeJson(encodedPayload);
  const now = Math.floor(Date.now() / 1000);
  const repository = `${GITHUB_OWNER}/${GITHUB_REPOSITORY}`;
  const audiences = Array.isArray(claims?.aud) ? claims.aud : [claims?.aud];
  const runId = positiveInteger(claims?.run_id);
  const runAttempt = positiveInteger(claims?.run_attempt);
  if (
    claims?.iss !== ISSUER ||
    !audiences.includes(new URL(request.url).origin) ||
    !Number.isSafeInteger(claims.exp) || claims.exp + CLOCK_SKEW_SECONDS <= now ||
    (Number.isSafeInteger(claims.nbf) && claims.nbf - CLOCK_SKEW_SECONDS > now) ||
    claims.repository !== repository ||
    claims.workflow_ref !== `${repository}/${GITHUB_WORKFLOW_PATH}@refs/heads/${GITHUB_REF}` ||
    claims.ref !== `refs/heads/${GITHUB_REF}` ||
    claims.event_name !== "workflow_dispatch" ||
    !runId || !runAttempt
  ) {
    throw unauthorized();
  }
  return { run_id: runId, run_attempt: runAttempt };
}
