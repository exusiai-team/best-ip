import { HttpError } from "./responses.js";
import { SCAN_TOKEN_TTL_SECONDS, REQUEST_ID_PATTERN, isRecord } from "./config.js";

const hmacKeyCache = new Map();

export function base64UrlEncode(bytes) {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/u, "");
}

export function base64UrlDecode(value) {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]+$/u.test(value)) {
    throw new HttpError(401, "扫描 token 无效", "invalid_scan_token");
  }
  const normalized = value.replaceAll("-", "+").replaceAll("_", "/");
  const padded = normalized + "=".repeat((4 - normalized.length % 4) % 4);
  try {
    const binary = atob(padded);
    return Uint8Array.from(binary, (character) => character.charCodeAt(0));
  } catch {
    throw new HttpError(401, "扫描 token 无效", "invalid_scan_token");
  }
}

export function text(value) {
  return new TextEncoder().encode(String(value));
}

export async function hmacKey(secret) {
  const normalized = String(secret || "");
  if (!normalized) throw new HttpError(503, "Worker scan token 尚未配置", "worker_not_configured");
  if (!hmacKeyCache.has(normalized)) {
    hmacKeyCache.set(
      normalized,
      crypto.subtle.importKey(
        "raw",
        text(normalized),
        { name: "HMAC", hash: "SHA-256" },
        false,
        ["sign", "verify"],
      ),
    );
  }
  return hmacKeyCache.get(normalized);
}

// 签名密钥不单独配置：从 GitHub App 私钥（Worker 本来就必须有的高熵 secret）按用途派生，
// 用途标签把扫描 token 与登录会话分开。轮换 App 私钥会让已签出的 token 与会话一起失效。
function appSecret(env) {
  const secret = String(env.GITHUB_APP_PRIVATE_KEY || "").trim();
  if (!secret) throw new HttpError(503, "请配置 GITHUB_APP_PRIVATE_KEY", "worker_not_configured");
  return secret;
}

function scanTokenKey(env) {
  return hmacKey(JSON.stringify(["best-ip-scan-token-v2", appSecret(env)]));
}

export async function signScanToken(env, requestId, dispatchedAt) {
  const now = Math.floor(Date.now() / 1000);
  const payload = {
    v: 1,
    request_id: requestId,
    dispatched_at: dispatchedAt,
    expires_at: now + SCAN_TOKEN_TTL_SECONDS,
  };
  const encoded = base64UrlEncode(text(JSON.stringify(payload)));
  const signature = await crypto.subtle.sign("HMAC", await scanTokenKey(env), text(encoded));
  return `${encoded}.${base64UrlEncode(new Uint8Array(signature))}`;
}

export async function verifyScanToken(env, token, requestId) {
  if (typeof token !== "string" || token.length > 4096) {
    throw new HttpError(401, "扫描 token 无效", "invalid_scan_token");
  }
  const parts = token.split(".");
  if (parts.length !== 2) throw new HttpError(401, "扫描 token 无效", "invalid_scan_token");
  const [encoded, encodedSignature] = parts;
  const valid = await crypto.subtle.verify(
    "HMAC",
    await scanTokenKey(env),
    base64UrlDecode(encodedSignature),
    text(encoded),
  );
  if (!valid) throw new HttpError(401, "扫描 token 无效", "invalid_scan_token");
  let payload;
  try {
    payload = JSON.parse(new TextDecoder().decode(base64UrlDecode(encoded)));
  } catch {
    throw new HttpError(401, "扫描 token 无效", "invalid_scan_token");
  }
  const now = Math.floor(Date.now() / 1000);
  if (
    !isRecord(payload) ||
    payload.v !== 1 ||
    payload.request_id !== requestId ||
    !REQUEST_ID_PATTERN.test(requestId) ||
    !Number.isSafeInteger(payload.dispatched_at) ||
    !Number.isSafeInteger(payload.expires_at) ||
    payload.expires_at <= now
  ) {
    throw new HttpError(401, "扫描 token 无效或已过期", "invalid_scan_token");
  }
  return payload;
}

export const SESSION_COOKIE = "__Host-best-ip-session";
export const LOGIN_CSRF_COOKIE = "__Host-best-ip-login-csrf";
export const SESSION_TTL_SECONDS = 12 * 60 * 60;

export function assertLoginConfigured(env) {
  if (typeof env.SITE_PASSWORD !== "string" || env.SITE_PASSWORD.length < 16 || env.SITE_PASSWORD.length > 1024 ||
      !String(env.GITHUB_APP_PRIVATE_KEY || "").trim()) {
    throw new HttpError(503, "请配置至少 16 个字符的 SITE_PASSWORD 和 GITHUB_APP_PRIVATE_KEY", "worker_not_configured");
  }
}

async function sessionKey(env) {
  assertLoginConfigured(env);
  // 与扫描 token 分开派生；改访问密码或轮换 App 私钥都会让已有会话失效。
  return hmacKey(JSON.stringify(["best-ip-session-v2", appSecret(env), env.SITE_PASSWORD]));
}

export async function passwordMatches(env, password) {
  assertLoginConfigured(env);
  const key = await sessionKey(env);
  const signature = await crypto.subtle.sign("HMAC", key, text(env.SITE_PASSWORD));
  return crypto.subtle.verify("HMAC", key, signature, text(password));
}

export async function signSession(env, origin) {
  const expires = Math.floor(Date.now() / 1000) + SESSION_TTL_SECONDS;
  const payload = base64UrlEncode(text(JSON.stringify({ v: 1, origin, expires })));
  const signature = await crypto.subtle.sign("HMAC", await sessionKey(env), text(payload));
  return `${payload}.${base64UrlEncode(new Uint8Array(signature))}`;
}

export async function authenticate(request, env) {
  assertLoginConfigured(env);
  const cookie = (request.headers.get("Cookie") || "").split(";")
    .map((part) => part.trim()).find((part) => part.startsWith(`${SESSION_COOKIE}=`));
  const token = cookie?.slice(SESSION_COOKIE.length + 1) || "";
  try {
    const parts = token.split(".");
    if (token.length > 2048 || parts.length !== 2) throw new Error("invalid");
    const [payload, signature] = parts;
    const valid = await crypto.subtle.verify("HMAC", await sessionKey(env),
      base64UrlDecode(signature), text(payload));
    if (!valid) throw new Error("invalid");
    const data = JSON.parse(new TextDecoder().decode(base64UrlDecode(payload)));
    const now = Math.floor(Date.now() / 1000);
    if (!isRecord(data) || data.v !== 1 || data.origin !== new URL(request.url).origin ||
        !Number.isSafeInteger(data.expires) || data.expires <= now ||
        data.expires > now + SESSION_TTL_SECONDS) throw new Error("expired");
    return { method: "password" };
  } catch {
    throw new HttpError(401, "请使用访问密码登录", "login_required");
  }
}

export function assertSameOrigin(request) {
  if (!["POST", "DELETE"].includes(request.method)) return;
  const origin = request.headers.get("Origin");
  if (origin !== new URL(request.url).origin) {
    throw new HttpError(403, "拒绝跨站状态变更请求", "csrf_rejected");
  }
  const fetchSite = request.headers.get("Sec-Fetch-Site");
  if (fetchSite && !["same-origin", "same-site", "none"].includes(fetchSite)) {
    throw new HttpError(403, "拒绝跨站状态变更请求", "csrf_rejected");
  }
}

