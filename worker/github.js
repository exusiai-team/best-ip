import { base64UrlEncode, text } from "./auth.js";
import { HttpError, GitHubError } from "./responses.js";
import { GITHUB_API, GITHUB_OWNER, GITHUB_REPOSITORY, APP_ID_PATTERN, INSTALLATION_ID_PATTERN, isRecord } from "./config.js";

let installationTokenCache = null;

export async function appJwt(env) {
  const appId = String(env.GITHUB_APP_ID || "").trim();
  const privateKey = normalizePrivateKey(String(env.GITHUB_APP_PRIVATE_KEY || "").replaceAll("\\n", "\n"));
  if (!APP_ID_PATTERN.test(appId) || !privateKey) {
    throw new HttpError(503, "GitHub App 尚未配置", "worker_not_configured");
  }
  let key;
  try {
    key = await crypto.subtle.importKey("pkcs8", pemBytes(privateKey, "PRIVATE KEY"),
      { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["sign"]);
  } catch {
    throw new HttpError(503, "GitHub App 私钥无效", "worker_not_configured");
  }
  const now = Math.floor(Date.now() / 1000);
  const header = base64UrlEncode(text(JSON.stringify({ alg: "RS256", typ: "JWT" })));
  const payload = base64UrlEncode(text(JSON.stringify({ iat: now - 60, exp: now + 540, iss: appId })));
  const unsigned = `${header}.${payload}`;
  const signature = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", key, text(unsigned));
  return `${unsigned}.${base64UrlEncode(new Uint8Array(signature))}`;
}

export function derLength(length) {
  if (length < 128) return Uint8Array.of(length);
  const bytes = [];
  let value = length;
  while (value > 0) {
    bytes.unshift(value & 0xff);
    value >>>= 8;
  }
  return Uint8Array.from([0x80 | bytes.length, ...bytes]);
}

export function derField(tag, value) {
  const length = derLength(value.length);
  const result = new Uint8Array(1 + length.length + value.length);
  result[0] = tag;
  result.set(length, 1);
  result.set(value, 1 + length.length);
  return result;
}

export function joinBytes(...parts) {
  const result = new Uint8Array(parts.reduce((total, part) => total + part.length, 0));
  let offset = 0;
  for (const part of parts) {
    result.set(part, offset);
    offset += part.length;
  }
  return result;
}

export function pemBytes(pem, label) {
  const body = pem
    .replace(`-----BEGIN ${label}-----`, "")
    .replace(`-----END ${label}-----`, "")
    .replace(/\s+/gu, "");
  if (!body) return null;
  try {
    const binary = atob(body);
    return Uint8Array.from(binary, (character) => character.charCodeAt(0));
  } catch {
    return null;
  }
}

export function normalizePrivateKey(pem) {
  if (pem.includes("-----BEGIN PRIVATE KEY-----")) return pem;
  if (!pem.includes("-----BEGIN RSA PRIVATE KEY-----")) return pem;
  const pkcs1 = pemBytes(pem, "RSA PRIVATE KEY");
  if (!pkcs1) return pem;
  const algorithm = derField(0x30, joinBytes(
    derField(0x06, Uint8Array.from([0x2a, 0x86, 0x48, 0x86, 0xf7, 0x0d, 0x01, 0x01, 0x01])),
    derField(0x05, new Uint8Array()),
  ));
  const privateKeyInfo = derField(0x30, joinBytes(
    derField(0x02, Uint8Array.of(0)),
    algorithm,
    derField(0x04, pkcs1),
  ));
  let encoded = "";
  for (const byte of privateKeyInfo) encoded += String.fromCharCode(byte);
  const body = btoa(encoded).match(/.{1,64}/gu)?.join("\n") || "";
  return `-----BEGIN PRIVATE KEY-----\n${body}\n-----END PRIVATE KEY-----\n`;
}

export async function installationToken(env, forceRefresh = false) {
  const installationId = String(env.GITHUB_APP_INSTALLATION_ID || "").trim();
  if (!INSTALLATION_ID_PATTERN.test(installationId)) {
    throw new HttpError(503, "GitHub App installation 尚未配置", "worker_not_configured");
  }
  const now = Date.now();
  if (!forceRefresh && installationTokenCache && installationTokenCache.expiresAt - now > 5 * 60 * 1000) {
    return installationTokenCache.token;
  }
  const response = await fetch(`${GITHUB_API}/app/installations/${installationId}/access_tokens`, {
    method: "POST",
    headers: {
      Accept: "application/vnd.github+json",
      "Content-Type": "application/json",
      "X-GitHub-Api-Version": "2022-11-28",
      Authorization: `Bearer ${await appJwt(env)}`,
      "User-Agent": "best-ip-cloudflare-worker",
    },
    body: JSON.stringify({ repositories: [GITHUB_REPOSITORY] }),
  });
  if (!response.ok) throw new HttpError(502, "无法取得 GitHub App installation token", "github_auth_failed");
  const payload = await response.json().catch(() => null);
  if (!isRecord(payload) || typeof payload.token !== "string") {
    throw new HttpError(502, "GitHub App token 返回格式无效", "github_auth_failed");
  }
  const expiresAt = Date.parse(String(payload.expires_at || ""));
  installationTokenCache = {
    token: payload.token,
    expiresAt: Number.isFinite(expiresAt) ? expiresAt : now + 50 * 60 * 1000,
  };
  return payload.token;
}

export function githubUrl(path, query = {}) {
  const url = new URL(`${GITHUB_API}${path}`);
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null) url.searchParams.set(key, String(value));
  }
  return url;
}

export async function githubJson(env, path, options = {}, retry = true) {
  const token = await installationToken(env);
  const response = await fetch(githubUrl(path, options.query), {
    method: options.method || "GET",
    headers: {
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "best-ip-cloudflare-worker",
      Authorization: `Bearer ${token}`,
      ...(options.headers || {}),
    },
    body: options.body,
    redirect: "manual",
  });
  if (response.status === 401 && retry) {
    installationTokenCache = null;
    return githubJson(env, path, options, false);
  }
  if (response.status === 204) return null;
  const payload = await response.json().catch(() => null);
  if (!response.ok) {
    const status = response.status === 403 ? 502 : response.status === 404 ? 404 : 502;
    throw new GitHubError(status, status === 404 ? "GitHub 资源不存在" : `GitHub API 请求失败（HTTP ${response.status}）`);
  }
  return payload;
}

// 取字节这一跳不再由 Worker 承担。Cloudflare 出口到 GitHub artifact 的 blob
// （*.blob.core.windows.net）实测约 40%~60% 的请求会挂死到超时、被边缘改写成 522，
// 而同一个签名地址由浏览器直连是健康的（Azure 侧回 Access-Control-Allow-Origin: *）。
// Worker 只保留它独有的能力：用 GitHub App token 把那个签名地址换出来。
const ARTIFACT_HOST_SUFFIX = ".blob.core.windows.net";

function describeHop(label, response) {
  const opaque = response.type === "opaqueredirect" ? "(opaque)" : "";
  return `${label}=${response.status}${opaque}`;
}

function artifactDownloadUrl(value) {
  if (typeof value !== "string" || !value) return null;
  let url;
  try {
    url = new URL(value);
  } catch {
    return null;
  }
  // 只把 GitHub 自己的 artifact blob 交给浏览器，别的一律不认。
  if (url.protocol !== "https:" || !url.hostname.endsWith(ARTIFACT_HOST_SUFFIX)) return null;
  return url.toString();
}

// cancel 在已经出错或已被消费的流上会 reject；这里只关心「别继续挂着连接」，不关心结果。
async function dropBody(response) {
  if (!response?.body) return;
  await response.body.cancel().catch(() => {});
}

// redirect: "manual" 是首选：只读 302 头，不搬字节。
// 但 manual 的语义各运行时并不一致，所以两条退路都留着，并立刻丢掉 body：
// 已经给出 200（运行时自己跟了重定向）就用 response.url；什么都不给就退回默认重定向反推。
async function resolveArtifactUrl(manual, zipUrl, headers) {
  const trace = [describeHop("manual", manual)];
  const fromLocation = manual.status >= 300 && manual.status < 400
    ? artifactDownloadUrl(manual.headers.get("Location"))
    : null;
  if (fromLocation) {
    await dropBody(manual);
    return { url: fromLocation, trace };
  }
  const inline = manual.ok ? artifactDownloadUrl(manual.url) : null;
  if (inline) {
    await dropBody(manual);
    return { url: inline, trace };
  }
  const followed = await fetch(zipUrl, { headers, redirect: "follow", signal: AbortSignal.timeout(10_000) })
    .catch(() => null);
  await dropBody(manual);
  if (!followed) {
    trace.push("follow=timeout");
    return { url: null, trace };
  }
  trace.push(describeHop("follow", followed));
  const landed = artifactDownloadUrl(followed.url);
  await dropBody(followed);
  return { url: landed, trace };
}

export async function githubArtifactUrl(env, artifactId, retry = true) {
  const token = await installationToken(env);
  const zipUrl = githubUrl(`/repos/${GITHUB_OWNER}/${GITHUB_REPOSITORY}/actions/artifacts/${artifactId}/zip`);
  const headers = {
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "best-ip-cloudflare-worker",
    Authorization: `Bearer ${token}`,
  };
  // 这一跳也可能挂在网络上。必须自己兜住，否则会以 500 `internal_error` 冒出去：
  // 既丢掉跳转轨迹，也绕过下面的 401 重试。
  let manual;
  try {
    manual = await fetch(zipUrl, { headers, redirect: "manual", signal: AbortSignal.timeout(10_000) });
  } catch (error) {
    throw new GitHubError(502, `GitHub artifact 下载地址缺失（manual=${error?.name || error?.message || error}）`, "artifact_location_missing");
  }
  if (manual.status === 401 && retry) {
    installationTokenCache = null;
    await dropBody(manual);
    return githubArtifactUrl(env, artifactId, false);
  }
  const { url, trace } = await resolveArtifactUrl(manual, zipUrl, headers);
  if (!url) throw new GitHubError(502, `GitHub artifact 下载地址缺失（${trace.join(" → ")}）`, "artifact_location_missing");
  return url;
}
