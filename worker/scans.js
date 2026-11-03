import { GITHUB_OWNER, GITHUB_REPOSITORY, GITHUB_WORKFLOW, GITHUB_WORKFLOW_PATH, GITHUB_REF, MAX_REQUEST_BYTES, MAX_JOB_PAGES, REQUEST_ID_PATTERN, isRecord, positiveInteger, assertWorkerConfigured } from "./config.js";
import { HttpError, json } from "./responses.js";
import { assertSameOrigin, signScanToken, verifyScanToken } from "./auth.js";
import { githubJson } from "./github.js";
import { findArtifact } from "./artifacts.js";
import { readScanProgress } from "./progress.js";
import { assertTargetUrl } from "./relay.js";
import { stashSubscription, dropSubscription } from "./runner.js";

export async function readJsonBody(request) {
  const contentLength = Number(request.headers.get("Content-Length"));
  if (Number.isFinite(contentLength) && contentLength > MAX_REQUEST_BYTES) {
    throw new HttpError(413, "请求体超过安全上限", "request_too_large");
  }
  const bytes = await request.arrayBuffer();
  if (bytes.byteLength > MAX_REQUEST_BYTES) {
    throw new HttpError(413, "请求体超过安全上限", "request_too_large");
  }
  try {
    return JSON.parse(new TextDecoder().decode(bytes));
  } catch {
    throw new HttpError(400, "请求 JSON 无效", "invalid_json");
  }
}

export function validateScanRequest(body) {
  if (!isRecord(body)) throw new HttpError(400, "请求结构无效", "invalid_request");
  const requestId = String(body.request_id || "");
  if (!REQUEST_ID_PATTERN.test(requestId)) throw new HttpError(400, "request_id 无效", "invalid_request");
  let subscriptionUrl;
  try {
    // 与订阅中继同一套公网地址校验；runner 下载时还会逐跳再验一次。
    subscriptionUrl = assertTargetUrl(String(body.subscription_url || "").trim()).href;
  } catch {
    throw new HttpError(400, "订阅地址必须是公开的 http/https 链接，且不能携带账号密码", "invalid_subscription_url");
  }
  return { requestId, subscriptionUrl };
}

export function expectedRunTitle(requestId) {
  return `Best IP scan ${requestId}`;
}

export function exactRun(run, requestId, dispatchedAt) {
  if (!isRecord(run)) return false;
  if (run.path !== GITHUB_WORKFLOW_PATH || run.display_title !== expectedRunTitle(requestId)) return false;
  if (run.event !== "workflow_dispatch" || run.head_branch !== GITHUB_REF) return false;
  const id = positiveInteger(run.id);
  const created = Date.parse(String(run.created_at || ""));
  if (!id || !Number.isFinite(created)) return false;
  return created >= dispatchedAt - 120_000 && created <= Date.now() + 120_000;
}

export async function resolveRun(env, requestId, runId, dispatchedAt) {
  if (runId) {
    const run = await githubJson(env, `/repos/${GITHUB_OWNER}/${GITHUB_REPOSITORY}/actions/runs/${runId}`);
    if (!exactRun(run, requestId, dispatchedAt)) {
      throw new HttpError(409, "GitHub Actions 运行与本次请求无法精确关联", "run_identity_mismatch");
    }
    return run;
  }
  const payload = await githubJson(
    env,
    `/repos/${GITHUB_OWNER}/${GITHUB_REPOSITORY}/actions/workflows/${encodeURIComponent(GITHUB_WORKFLOW)}/runs`,
    { query: { event: "workflow_dispatch", branch: GITHUB_REF, per_page: 20 } },
  );
  const matches = Array.isArray(payload?.workflow_runs)
    ? payload.workflow_runs.filter((run) => exactRun(run, requestId, dispatchedAt))
    : [];
  if (matches.length > 1) throw new HttpError(409, "无法唯一关联本次 GitHub Actions 运行", "run_identity_ambiguous");
  return matches[0] || null;
}

export async function listRunJobs(env, run) {
  const runId = positiveInteger(run?.id);
  const attempt = positiveInteger(run?.run_attempt);
  if (!runId || !attempt) throw new HttpError(502, "GitHub Actions 运行版本无效", "github_run_invalid");
  const jobs = [];
  const seen = new Set();
  let totalCount = null;
  let warning = null;
  for (let page = 1; page <= MAX_JOB_PAGES; page += 1) {
    const payload = await githubJson(
      env,
      `/repos/${GITHUB_OWNER}/${GITHUB_REPOSITORY}/actions/runs/${runId}/attempts/${attempt}/jobs`,
      { query: { per_page: 100, page } },
    );
    const pageJobs = Array.isArray(payload?.jobs) ? payload.jobs : [];
    if (Number.isSafeInteger(Number(payload?.total_count)) && Number(payload.total_count) >= 0) {
      totalCount = Number(payload.total_count);
    }
    for (const job of pageJobs) {
      const jobAttempt = job?.run_attempt == null ? attempt : Number(job.run_attempt);
      const jobId = positiveInteger(job?.id);
      if (jobAttempt !== attempt || !jobId || seen.has(jobId)) continue;
      seen.add(jobId);
      jobs.push(job);
    }
    if (!pageJobs.length || pageJobs.length < 100 || (totalCount !== null && jobs.length >= totalCount)) break;
    if (page === MAX_JOB_PAGES) warning = "Actions job 分页达到安全上限，进度可能不完整";
  }
  return { available: true, jobs, totalCount, warning };
}

export function runStatus(run) {
  const status = String(run?.status || "unknown");
  if (status === "completed") {
    if (run?.conclusion === "success") return "completed";
    if (run?.conclusion === "cancelled") return "cancelled";
    return "failed";
  }
  if (["queued", "waiting", "pending", "requested"].includes(status)) return "queued";
  if (status === "in_progress") return "running";
  return status;
}

export async function readScanContext(request, env, requestId) {
  if (!REQUEST_ID_PATTERN.test(requestId)) throw new HttpError(400, "request_id 无效", "invalid_request");
  const token = request.headers.get("X-Best-IP-Scan-Token");
  const payload = await verifyScanToken(env, token, requestId);
  const url = new URL(request.url);
  const runId = url.searchParams.get("run_id") ? positiveInteger(url.searchParams.get("run_id")) : null;
  const attempt = url.searchParams.get("run_attempt") ? positiveInteger(url.searchParams.get("run_attempt")) : null;
  if (url.searchParams.has("run_id") && !runId) throw new HttpError(400, "run_id 无效", "invalid_request");
  if (url.searchParams.has("run_attempt") && !attempt) throw new HttpError(400, "run_attempt 无效", "invalid_request");
  return { payload, runId, attempt };
}

export function assertRunAttempt(run, expectedAttempt) {
  if (expectedAttempt && positiveInteger(run?.run_attempt) !== expectedAttempt) {
    throw new HttpError(409, "GitHub Actions 运行 attempt 与本次请求不匹配", "run_identity_mismatch");
  }
}

export async function resolveScanRun(request, env, requestId) {
  const context = await readScanContext(request, env, requestId);
  const run = await resolveRun(env, requestId, context.runId, context.payload.dispatched_at);
  if (run) assertRunAttempt(run, context.attempt);
  return run;
}

export async function createScan(request, env) {
  assertSameOrigin(request);
  assertWorkerConfigured(env);
  const body = await readJsonBody(request);
  const { requestId, subscriptionUrl } = validateScanRequest(body);
  // 订阅地址只进 Durable Object，不进 workflow 输入；runner 凭 OIDC 来领（worker/runner.js）。
  await stashSubscription(env, requestId, subscriptionUrl);
  const dispatchedAt = Date.now();
  await githubJson(
    env,
    `/repos/${GITHUB_OWNER}/${GITHUB_REPOSITORY}/actions/workflows/${encodeURIComponent(GITHUB_WORKFLOW)}/dispatches`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        ref: GITHUB_REF,
        inputs: {
          request_id: requestId,
          worker_url: new URL(request.url).origin,
        },
      }),
    },
  ).catch(async (error) => {
    await dropSubscription(env, requestId);
    throw error;
  });
  return json(
    {
      request_id: requestId,
      run_id: null,
      dispatched_at: dispatchedAt,
      scan_token: await signScanToken(env, requestId, dispatchedAt),
    },
    202,
    { "Cache-Control": "no-store" },
  );
}

export async function scanState(request, env, requestId) {
  const run = await resolveScanRun(request, env, requestId);
  if (!run) {
    return json(
      {
        request_id: requestId,
        status: "dispatching",
        run: null,
        jobs: [],
        jobs_available: true,
        jobs_total_count: null,
        jobs_warning: null,
        artifact_ready: false,
        progress: null,
      },
      200,
      { "Cache-Control": "no-store" },
    );
  }
  // 进度读数（Durable Object，几十毫秒）与产物探测并行取；产物探测失败照旧整轮报错。
  // job 明细只在「运行中但读不到 runner 进度」时才去 GitHub 取：页面只拿它的步骤名兜底推断阶段
  // （frontend-next/app/progress.js 的 stageOf），有进度读数时步骤名永远排在后面，没运行时也用不上。
  // 原来每轮都取，是每次轮询里最慢的一路 GitHub 请求。
  const succeeded = run.status === "completed" && run.conclusion === "success";
  const [progress, artifact] = await Promise.all([
    readScanProgress(env, requestId, run),
    succeeded ? findArtifact(env, run, requestId) : null,
  ]);
  const jobsResult = run.status === "in_progress" && !progress
    ? await listRunJobs(env, run).catch((error) => ({
      available: false,
      jobs: [],
      totalCount: null,
      warning: error instanceof HttpError ? error.message : "读取 Actions job 失败",
    }))
    : { available: true, jobs: [], totalCount: null, warning: null };
  const status = run.status === "completed" && run.conclusion === "success" && !artifact
    ? "artifact_pending"
    : runStatus(run);
  return json(
    {
      request_id: requestId,
      status,
      run,
      jobs: jobsResult.jobs,
      jobs_available: jobsResult.available,
      jobs_total_count: jobsResult.totalCount,
      jobs_warning: jobsResult.warning,
      artifact_ready: Boolean(artifact),
      artifact_id: artifact?.id || null,
      artifact_name: artifact?.name || null,
      progress,
    },
    200,
    { "Cache-Control": "no-store" },
  );
}

export async function cancelScan(request, env, requestId) {
  assertSameOrigin(request);
  const run = await resolveScanRun(request, env, requestId);
  if (!run) throw new HttpError(409, "GitHub Actions 运行尚未建立", "run_pending");
  await githubJson(env, `/repos/${GITHUB_OWNER}/${GITHUB_REPOSITORY}/actions/runs/${run.id}/cancel`, { method: "POST" });
  return json({ request_id: requestId, run_id: run.id, status: "cancelled" }, 202, { "Cache-Control": "no-store" });
}
