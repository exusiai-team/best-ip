import { HttpError, GitHubError } from "./responses.js";
import { GITHUB_API, GITHUB_OWNER, GITHUB_REPOSITORY } from "./config.js";

// 调 GitHub API 用一张 fine-grained PAT（Worker Secret `GITHUB_TOKEN`）：只授权本仓库的
// Actions 读写。原先的 GitHub App 要 App ID + 私钥两项配置，还得换 installation token；
// 安装一旦被暂停，扫描就整体 403，而页面上只看得到「无法取得 installation token」。
export function githubToken(env) {
  const token = String(env.GITHUB_TOKEN || "").trim();
  if (!token) throw new HttpError(503, "请配置 GITHUB_TOKEN", "worker_not_configured");
  return token;
}

function githubAuthError(status) {
  return new GitHubError(502, `GITHUB_TOKEN 无效、已过期或没有本仓库 Actions 权限（HTTP ${status}）`, "github_auth_failed");
}

export function githubUrl(path, query = {}) {
  const url = new URL(`${GITHUB_API}${path}`);
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined && value !== null) url.searchParams.set(key, String(value));
  }
  return url;
}

export async function githubJson(env, path, options = {}) {
  const token = githubToken(env);
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
  // PAT 缺权限时 GitHub 回 403（限流也是 403，但会带 x-ratelimit-remaining: 0）。
  if (response.status === 401 ||
      (response.status === 403 && response.headers.get("x-ratelimit-remaining") !== "0")) {
    throw githubAuthError(response.status);
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
// Worker 只保留它独有的能力：用 GitHub token 把那个签名地址换出来。
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

export async function githubArtifactUrl(env, artifactId) {
  const token = githubToken(env);
  const zipUrl = githubUrl(`/repos/${GITHUB_OWNER}/${GITHUB_REPOSITORY}/actions/artifacts/${artifactId}/zip`);
  const headers = {
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "best-ip-cloudflare-worker",
    Authorization: `Bearer ${token}`,
  };
  // 这一跳也可能挂在网络上。必须自己兜住，否则会以 500 `internal_error` 冒出去：
  // 既丢掉跳转轨迹，也绕过下面的 401 判定。
  let manual;
  try {
    manual = await fetch(zipUrl, { headers, redirect: "manual", signal: AbortSignal.timeout(10_000) });
  } catch (error) {
    throw new GitHubError(502, `GitHub artifact 下载地址缺失（manual=${error?.name || error?.message || error}）`, "artifact_location_missing");
  }
  if (manual.status === 401) {
    await dropBody(manual);
    throw githubAuthError(401);
  }
  const { url, trace } = await resolveArtifactUrl(manual, zipUrl, headers);
  if (!url) throw new GitHubError(502, `GitHub artifact 下载地址缺失（${trace.join(" → ")}）`, "artifact_location_missing");
  return url;
}
