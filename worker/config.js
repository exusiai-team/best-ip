import { HttpError } from "./responses.js";

export const GITHUB_API = "https://api.github.com";
// 仓库已从个人账号 silverwolf-x 转到组织 silverwolfxai。GitHub 对旧路径只回转址（3xx），而 githubJson
// 用的是 redirect: "manual"（只认确切的那个仓库），所以这里必须写当前的所有者，不能靠转址兜底。
export const GITHUB_OWNER = "silverwolfxai";
export const GITHUB_REPOSITORY = "best-ip";
export const GITHUB_WORKFLOW = "scan.yml";
export const GITHUB_WORKFLOW_PATH = ".github/workflows/scan.yml";
export const GITHUB_REF = "main";
export const ARTIFACT_PREFIX = "best-ip-result";
export const MAX_REQUEST_BYTES = 70 * 1024;
export const MAX_ARTIFACT_BYTES = 50 * 1024 * 1024;
export const SCAN_TOKEN_TTL_SECONDS = 2 * 60 * 60;
export const ENVELOPE_TTL_SECONDS = 15 * 60;
export const MAX_JOB_PAGES = 10;
export const REQUEST_ID_PATTERN = /^[A-Za-z0-9_-]{1,128}$/u;
export const KEY_ID_PATTERN = /^[a-f0-9]{64}$/iu;
export const APP_ID_PATTERN = /^[0-9]+$/u;
export const INSTALLATION_ID_PATTERN = /^[0-9]+$/u;

export function isRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

export function positiveInteger(value) {
  const number = Number(value);
  return Number.isSafeInteger(number) && number > 0 ? number : null;
}

export function assertWorkerConfigured(env) {
  if (
    !KEY_ID_PATTERN.test(String(env.SCAN_KEY_ID || "").trim()) ||
    !String(env.SCAN_TOKEN_SECRET || "").trim() ||
    !APP_ID_PATTERN.test(String(env.GITHUB_APP_ID || "").trim()) ||
    !INSTALLATION_ID_PATTERN.test(String(env.GITHUB_APP_INSTALLATION_ID || "").trim()) ||
    !String(env.GITHUB_APP_PRIVATE_KEY || "").trim()
  ) {
    throw new HttpError(503, "Worker 扫描 secrets 尚未完整配置", "worker_not_configured");
  }
}
