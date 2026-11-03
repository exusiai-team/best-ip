import {
  GITHUB_OWNER,
  GITHUB_REPOSITORY,
  GITHUB_WORKFLOW,
  GITHUB_WORKFLOW_PATH,
  GITHUB_REF,
  REQUEST_ID_PATTERN,
  positiveInteger,
  assertWorkerConfigured,
} from "./config.js";
import { json } from "./responses.js";
import { githubJson, githubArtifactUrl } from "./github.js";
import { findArtifact } from "./artifacts.js";
import { expectedRunTitle, runStatus } from "./scans.js";
import { readScanProgress } from "./progress.js";

// 这是新前端的**只读**入口：站点页面没有订阅输入、没有开始按钮，扫描仍然由页面之外发起
// （Actions 的 workflow_dispatch / 脚本），页面只回答「最近一次扫描是什么、结果在哪」。
// 因此这里不发任何 GitHub 写请求，只把已有的那次运行和它的 artifact 签名地址交出去。
//
// 为什么请求者只要带着访问密码登录就够了：站点本身就是「一把密码 = 一份数据」的门，
// 登录后能看到的节点结果，和这里返回的是同一份东西。给扫描用的 scan_token 仍然只属于
// 「发起那次扫描的人」，这条只读路径不碰它，也不放宽它——所以下面的 run 只挑身份字段返回，
// 不再像 GET /api/scans/:id 那样把整个 GitHub run 对象（含触发者、日志地址）吐出去。
const ARTIFACT_PROBES = 5;

// 标题前缀不另写字面量：它必须和 worker/scans.js 的 expectedRunTitle 逐字符一致，
// 否则「列出来的 run」和「页面找得到的 run」会悄悄对不上。
const TITLE_PREFIX = expectedRunTitle("");

function scanRequestId(run) {
  const title = typeof run?.display_title === "string" ? run.display_title : "";
  if (!title.startsWith(TITLE_PREFIX)) return null;
  const requestId = title.slice(TITLE_PREFIX.length).trim();
  return REQUEST_ID_PATTERN.test(requestId) ? requestId : null;
}

function runIdentity(run) {
  return {
    id: positiveInteger(run?.id),
    run_attempt: positiveInteger(run?.run_attempt),
    status: String(run?.status || "unknown"),
    conclusion: run?.conclusion == null ? null : String(run.conclusion),
    created_at: typeof run?.created_at === "string" ? run.created_at : null,
    updated_at: typeof run?.updated_at === "string" ? run.updated_at : null,
  };
}

// 查询参数已经按 workflow/event/branch 过滤过，这里再核一遍：这三个字段只要对不上，
// 被挑中的就是另一次运行，而页面会把另一次运行的结果当成「最近一次扫描」展示——
// 只读路径上最坏的一种错（不是报错，是安静地说错话）。判据对齐 worker/scans.js 的 exactRun。
function isScanRun(run) {
  return run?.path === GITHUB_WORKFLOW_PATH
    && run.event === "workflow_dispatch"
    && run.head_branch === GITHUB_REF
    && scanRequestId(run) !== null;
}
// status 由调用方给：只有「挑到了产物」和「没挑到」两种收尾，但没挑到时要区分的成因有三种
// （从没扫过 / 最新那次还没跑完或跑挂了 / 最新的若干次产物都过期了），硬编码在这里就会把
// 「本站还没跑过扫描」也说成「产物过期」——一句不真的解释比没有解释更坏。
function payloadFor(requestId, run, artifact, status) {
  return {
    request_id: requestId,
    status,
    run: run ? runIdentity(run) : null,
    artifact_ready: Boolean(artifact),
    artifact_id: artifact ? positiveInteger(artifact.id) : null,
    artifact_name: artifact ? String(artifact.name || "") || null : null,
    artifact_url: artifact ? artifact.url : null,
    // 页面只说「这次结果来自哪次扫描」，所以这里给的是运行结束时间；没有运行就没有这个时间。
    scanned_at: run && typeof run.updated_at === "string" ? run.updated_at : null,
  };
}

/**
 * 最新那次运行还在排队或执行时，把它单独交出来：页面据此「跟进」而不是让人反复刷新。
 * 它与下面挑出来的产物是两件事——旧结果照常显示，新扫描在旁边走进度，跑完再换表。
 * 只给身份、状态与计数读数；没有 scan_token 的一方拿不到取消权，也看不到 job 明细。
 */
async function activeScan(env, newest) {
  const status = newest ? runStatus(newest) : null;
  if (status !== "queued" && status !== "running") return null;
  const requestId = scanRequestId(newest);
  return {
    request_id: requestId,
    status,
    run: runIdentity(newest),
    progress: await readScanProgress(env, requestId, newest),
  };
}

/**
 * 最近一次扫描的状态。挑 run 的顺序是「从最新往回」，最多看 ARTIFACT_PROBES 次：
 * scan.yml 的 artifact 只保留 1 天（retention-days: 1），最新那次常常已经过期，
 * 这时往前多找几次比让页面空着更有用；再往前翻既没意义（都过期了）也白花 GitHub 配额。
 */
export async function latestScanState(env) {
  assertWorkerConfigured(env);
  const payload = await githubJson(
    env,
    `/repos/${GITHUB_OWNER}/${GITHUB_REPOSITORY}/actions/workflows/${encodeURIComponent(GITHUB_WORKFLOW)}/runs`,
    { query: { event: "workflow_dispatch", branch: GITHUB_REF, per_page: 20 } },
  );
  const runs = Array.isArray(payload?.workflow_runs) ? payload.workflow_runs : [];
  const scanned = runs.filter(isScanRun);
  const newest = scanned[0] || null;
  // 与产物探测并行：进度读数是一次 Durable Object 往返，不该排在最多 5 次 GitHub 探测后面。
  const activePromise = activeScan(env, newest);
  let probes = 0;
  let probeErrors = 0;
  let firstProbeError = null;
  for (const run of scanned) {
    if (probes >= ARTIFACT_PROBES) break;
    if (runStatus(run) !== "completed") continue;
    probes += 1;
    const requestId = scanRequestId(run);
    try {
      const artifact = await findArtifact(env, run, requestId);
      if (!artifact) continue;
      const ready = { id: artifact.id, name: artifact.name, url: await githubArtifactUrl(env, artifact.id) };
      return json({ ...payloadFor(requestId, run, ready, "completed"), active: await activePromise }, 200, { "Cache-Control": "no-store" });
    } catch (cause) {
      // 单次探测失败（限流 / 5xx / 产物名撞车）不该让整条只读路径塌掉：
      // 更旧的那次产物可能好好的，继续往前找比当场 502 有用。
      probeErrors += 1;
      firstProbeError = firstProbeError || cause;
    }
  }

  // 每一次探测都失败了：这是上游坏了，不是「产物过期」。把真实原因抛出去，
  // 否则页面会把一次 GitHub 故障说成「结果已经不在 GitHub 上了」。
  if (probes > 0 && probeErrors === probes) {
    await activePromise.catch(() => null);
    throw firstProbeError;
  }

  // 一次都没扫过：报 none，而不是假装有结果；最新那次还没跑完或跑挂了就照它的真实状态说。
  const status = newest === null
    ? "none"
    : runStatus(newest) === "completed"
      ? "artifact_expired"
      : runStatus(newest);
  return json(
    { ...payloadFor(newest ? scanRequestId(newest) : null, newest, null, status), active: await activePromise },
    200,
    { "Cache-Control": "no-store" },
  );
}
