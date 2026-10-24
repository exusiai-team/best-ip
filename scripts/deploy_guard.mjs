// ============================================================================
// 发布路径门闩：只有 Cloudflare Workers Builds（Git 集成，构建 main）能发布，本地一律拒绝。
// ----------------------------------------------------------------------------
// 为什么需要这个门闩：2026-09-24 那次「本地 npm run deploy」把工作区里 CRLF 的资产
// （本机 core.autocrlf=true，而仓库 blob 是 LF）当成线上内容发了上去，线上因此短暂跑着
// 与任何 commit 都不一致的文件（app/api.js 12327B vs blob 12089B，差的 238 字节就是 CR 个数）。
// `wrangler deploy` 只报「上传成功」，它不会说发上去的是不是仓库里的字节；「发布内容等于某个
// commit」这条契约只有把发布固定在远程构建上才守得住（构建机是 Linux 干净检出，blob 是 LF）。
//
// 这个脚本做四件事：
//   1. 不在 Workers Builds 里（WORKERS_CI !== "1"）直接拒绝（exit 2），并打印真正的发布路径；
//   2. 构建分支（WORKERS_CI_BRANCH）不是 main 时拒绝（exit 3）——非生产分支应走
//      Workers Builds 的非生产分支命令（只出预览版本），不能把别处的代码发上线；
//   3. HEAD 不等于本次构建声明的 commit（WORKERS_CI_COMMIT_SHA）时拒绝（exit 4）；
//   4. 发布之前断言「工作区字节 == 这个 commit 的 blob」：assets 目录下每个文件都必须被 git 跟踪，
//      且逐字节等于 `git show HEAD:<path>`。这把「发布内容与 commit 不一致」从发布后的事后检查提前
//      到发布前。早先的实现只按扩展名挑文本资产、只查有没有 CR 字节，那只是这条性质的代理：会漏掉
//      清单外的扩展名（.webmanifest/.map/.xml…），也会把「blob 本身就含 CR」误判成漂移。直接比 blob
//      才是契约本身。发布后可用 scripts/verify_deploy.mjs 对线上做逐字节比对。
//
// 用法：Workers Builds 的部署命令填 `npm run deploy`（不是默认的 `npx wrangler deploy`，那样会绕过本脚本）。
// 退出码：0 可以发布；1 内容与 blob 不一致；2 不是 Workers Builds；3 分支不是 main；4 HEAD ≠ WORKERS_CI_COMMIT_SHA。
// ============================================================================

import { execFileSync } from "node:child_process";
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";

function stripJsonComments(text) {
  // wrangler.jsonc 是 JSONC：先删块注释，再删整行注释（与 verify_deploy.mjs 同一套读法）。
  return text.replace(/\/\*[\s\S]*?\*\//gu, "").replace(/^[ \t]*\/\/.*$/gmu, "");
}

/** `./frontend-next/` → `frontend-next`；与 verify_deploy.mjs 的归一化保持一致。 */
function normalizeAssetDir(value) {
  return value.trim().replace(/^\.\//u, "").replace(/\/+$/u, "");
}

/** assets.directory → 仓库相对路径。 */
function assetDirectoryOfWrangler(configPath = "wrangler.jsonc") {
  const config = JSON.parse(stripJsonComments(readFileSync(configPath, "utf8")));
  const directory = config?.assets?.directory;
  if (typeof directory !== "string" || !directory.trim()) {
    throw new Error(`${configPath} 里没有 assets.directory，发布前无法确认要检查哪个目录`);
  }
  return normalizeAssetDir(directory);
}

/**
 * assets 目录下的全部条目。软链单独报出来：wrangler 上传时会跟穿它，
 * 内容不可能等于仓库里那个链接的 blob，也没法用 blob 断言。
 */
function walkAssetEntries(directory) {
  const files = [];
  const links = [];
  const walk = (relative) => {
    for (const entry of readdirSync(join(directory, relative), { withFileTypes: true })) {
      const next = relative ? `${relative}/${entry.name}` : entry.name;
      if (entry.isSymbolicLink()) links.push(next);
      else if (entry.isDirectory()) walk(next);
      else if (entry.isFile()) files.push(next);
    }
  };
  walk("");
  return { files: files.sort(), links };
}

/** assets 目录下被 git 跟踪的文件（已去掉目录前缀）。 */
function trackedAssetFiles(directory, commit) {
  return execFileSync("git", ["ls-tree", "-r", "--name-only", commit, "--", directory], { encoding: "utf8" })
    .split("\n").map((line) => line.trim()).filter(Boolean)
    .map((path) => path.slice(directory.length + 1));
}

function commitOfHead() {
  try {
    return execFileSync("git", ["rev-parse", "HEAD"], { encoding: "utf8" }).trim();
  } catch {
    return "unknown";
  }
}

/** blob 的原始字节：绕开本地 checkout 的换行转换（core.autocrlf）。 */
function blobAt(commit, path) {
  return execFileSync("git", ["show", `${commit}:${path}`], { maxBuffer: 64 * 1024 * 1024 });
}

/** 差在哪——最常见的一种是 CRLF：字节数之差正好等于工作区里的 CR 个数。 */
function differenceHint(working, blob) {
  const cr = working.reduce((count, byte) => (byte === 0x0d ? count + 1 : count), 0);
  if (cr > 0 && working.length - blob.length === cr) {
    return `工作区 ${working.length}B、blob ${blob.length}B，多出的 ${cr} 字节全是 CR（CRLF 检出，core.autocrlf=true）`;
  }
  return `工作区 ${working.length}B、blob ${blob.length}B`;
}

if (process.env.WORKERS_CI !== "1") {
  console.error(
    [
      "拒绝本地发布：发布只能走 Cloudflare Workers Builds，本地 npm run deploy 已被禁止。",
      "",
      "真正的发布路径：push（或合并 PR）到 main → Workers Builds 检出该 commit 并执行 npm run deploy；",
      "需要重发一次时，在 Cloudflare 控制台该 Worker 的 Deployments / Builds 里对 main 重新构建。",
      "",
      "为什么：本地部署会把工作区（可能是 CRLF 检出、可能带着未提交改动）的字节直接发到线上，",
      "而 wrangler deploy 只报「上传成功」，不会说发上去的是不是仓库里的内容。本地能做的只读检查：",
      "npm run check、npm run dry-run、npm run verify:deploy（核对线上内容与当前 commit 一致）。",
    ].join("\n"),
  );
  process.exit(2);
}

const branch = process.env.WORKERS_CI_BRANCH || "";
if (branch && branch !== "main") {
  console.error(
    [
      `拒绝发布：本次运行在分支 ${branch} 上，而发布只发生在 main。`,
      "非生产分支应走 Workers Builds 的非生产分支命令（默认 npx wrangler preview，只出预览版本），",
      "不要把它也设成 npm run deploy。要发布：push 或合并到 main。",
    ].join("\n"),
  );
  process.exit(3);
}

const commit = "HEAD";
const head = commitOfHead();
const declared = process.env.WORKERS_CI_COMMIT_SHA || "";
if (declared && head !== "unknown" && head !== declared) {
  console.error(
    [
      `拒绝发布：检出的 HEAD 是 ${head}，而本次构建声明的 commit 是 ${declared}。`,
      "发布的必须是这次构建声明的那一个 commit，检出与声明对不上时不能发布。",
    ].join("\n"),
  );
  process.exit(4);
}

const assetsDir = assetDirectoryOfWrangler();
const { files: workdirFiles, links } = walkAssetEntries(assetsDir);
const trackedFiles = trackedAssetFiles(assetsDir, commit);
const trackedSet = new Set(trackedFiles);

const problems = [];
if (links.length) {
  problems.push(`  - 符号链接 ${links.length} 个（wrangler 会跟穿它们上传）：${links.slice(0, 5).join("、")}`);
}
const untracked = workdirFiles.filter((path) => !trackedSet.has(path));
if (untracked.length) {
  problems.push(`  - 未被 git 跟踪但仍会被上传的文件 ${untracked.length} 个：${untracked.slice(0, 5).join("、")}`);
}
const missing = trackedFiles.filter((path) => !workdirFiles.includes(path));
if (missing.length) {
  problems.push(`  - 被跟踪但工作区里不存在 ${missing.length} 个：${missing.slice(0, 5).join("、")}`);
}

const drifted = [];
let checked = 0;
for (const path of workdirFiles) {
  if (!trackedSet.has(path)) continue;
  const working = readFileSync(join(assetsDir, path));
  const blob = blobAt(commit, `${assetsDir}/${path}`);
  checked += 1;
  if (!working.equals(blob)) drifted.push(`  - ${path}：${differenceHint(working, blob)}`);
}

if (problems.length || drifted.length) {
  console.error(`拒绝发布：${assetsDir}/ 与 commit ${head.slice(0, 7)} 的内容不一致。`);
  for (const problem of problems) console.error(problem);
  if (drifted.length) {
    console.error(`  - ${drifted.length}/${checked} 个文件的工作区字节 ≠ blob：`);
    for (const entry of drifted.slice(0, 10)) console.error(entry);
    if (drifted.length > 10) console.error(`  …另有 ${drifted.length - 10} 个`);
  }
  console.error(
    [
      "",
      "wrangler deploy 上传的是工作区的字节，所以这里不一致就说明发上去的内容不会等于任何 commit。",
      "多半是检出时被换行转换过（core.autocrlf=true）或有未提交/未跟踪的文件——修好工作区再发，别绕过这道门。",
    ].join("\n"),
  );
  process.exit(1);
}

console.log(
  `发布前检查通过：${assetsDir}/ 下 ${checked} 个文件与 commit ${head.slice(0, 7)} 的 blob 逐字节一致（分支 ${branch}）。`,
);
