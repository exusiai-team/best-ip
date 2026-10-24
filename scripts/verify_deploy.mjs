// 发布闭环的最后一步：对刚上线的生产站点做自证。
//
// 只做两件事，且都不依赖部署方的自述：
//   1. 无凭据可达性——登录页可达、CSP 仍放行签名 blob、静态资源仍被会话门挡住；
//   2. 有凭据字节比对——用 SITE_PASSWORD 登录后，把**当前 assets 目录**下每一个被 git
//      跟踪的文件与它在该 commit 里的 blob 逐字节比对。
// 第 2 步是真正的漂移门：`wrangler deploy` 只报「上传成功」，它不说线上提供的是不是这一个
// commit 的内容；而历史上确实发生过线上跑旧资产的静默漂移。
//
// assets 目录不写死：从 wrangler.jsonc 的 assets.directory 读（`--assets` 可覆盖）。写死成某一个
// 目录会变成一个绿着的假门禁——部署根换了、校验还在比老目录，每个「线上文件」都 404，而 404 与
// 内容不一致在结论里长得一模一样，只是更难查。
//
// 没有 SITE_PASSWORD 时只跑第 1 步并明确标注「未做字节比对」，不会假装验过。
//
// 用法：
//   node scripts/verify_deploy.mjs --site https://best-ip.silverwolfx.workers.dev
//   SITE_PASSWORD=... node scripts/verify_deploy.mjs --site <url> --commit <sha>
// 退出码：0 全通过；1 有失败项。

import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";

const argv = process.argv.slice(2);
const argOf = (name, fallback = "") => {
  const index = argv.indexOf(`--${name}`);
  return index >= 0 && argv[index + 1] ? argv[index + 1] : fallback;
};

function stripJsonComments(text) {
  // wrangler.jsonc 是 JSONC：先删块注释，再删整行注释。这个配置里没有含 // 的字符串值。
  return text.replace(/\/\*[\s\S]*?\*\//gu, "").replace(/^[ \t]*\/\/.*$/gmu, "");
}

/** `./frontend-next/` → `frontend-next`；与 deploy_guard.mjs 的归一化保持一致。 */
function normalizeAssetDir(value) {
  return value.trim().replace(/^\.\//u, "").replace(/\/+$/u, "");
}

/** assets.directory → 仓库相对路径。 */
function assetDirectoryOfWrangler(configPath = "wrangler.jsonc") {
  const config = JSON.parse(stripJsonComments(readFileSync(configPath, "utf8")));
  const directory = config?.assets?.directory;
  if (typeof directory !== "string" || !directory.trim()) {
    throw new Error(`${configPath} 里没有 assets.directory，无法确定该拿哪个目录当比对基准`);
  }
  return normalizeAssetDir(directory);
}

const SITE = argOf("site", "https://best-ip.silverwolfx.workers.dev").replace(/\/+$/u, "");
const COMMIT = argOf("commit", "HEAD");
const PASSWORD = argOf("password", process.env.SITE_PASSWORD || "");
// --assets 也要归一化：`--assets ./frontend-next` 不归一化时，后面切前缀会把
// `frontend-next/index.html` 切成 `dex.html`，取不到 blob 而崩在 git show 上。
const ASSETS_DIR = normalizeAssetDir(argOf("assets", assetDirectoryOfWrangler()));
const UA = "best-ip-deploy-verify";
const ATTEMPTS = 3;
const TIMEOUT_MS = 20_000;
const BLOB_CSP = "connect-src 'self' https://*.blob.core.windows.net";
const SESSION_COOKIE = "__Host-best-ip-session";
// 新资产在边缘生效有个窗口（见 fetchTrackedAsset 的注释）：内容不等于目标 blob 时继续重取。
// 预算是每段比对各自的窗口（armGrace），所以真漂移最多拖住 2 × 预算，而不是每个文件各等一轮。
// 注意预算只约束宽限等待，不含 request() 自身对 5xx/网络的 3 次重试。
const ASSET_GRACE_WAIT_MS = 5_000;
const ASSET_GRACE_BUDGET_MS = 180_000;

const failures = [];
const results = [];

function record(ok, label, detail = "") {
  results.push({ ok, label, detail });
  if (!ok) failures.push(`${label}${detail ? ` — ${detail}` : ""}`);
  console.log(`${ok ? "OK  " : "FAIL"}  ${label}${detail ? `  (${detail})` : ""}`);
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function request(path, { method = "GET", headers = {}, body } = {}) {
  let lastError = "";
  for (let attempt = 1; attempt <= ATTEMPTS; attempt += 1) {
    try {
      const response = await fetch(SITE + path, {
        method,
        headers: { "User-Agent": UA, ...headers },
        body,
        redirect: "manual",
        signal: AbortSignal.timeout(TIMEOUT_MS),
      });
      // 5xx 是 Cloudflare/上游的瞬时面，重试；4xx 是确定答复，直接返回。
      if (response.status >= 500 && attempt < ATTEMPTS) {
        lastError = `HTTP ${response.status}`;
        await sleep(1500 * attempt);
        continue;
      }
      return response;
    } catch (error) {
      lastError = error?.message || String(error);
      if (attempt < ATTEMPTS) await sleep(1500 * attempt);
    }
  }
  throw new Error(`${method} ${path} 连续 ${ATTEMPTS} 次失败：${lastError}`);
}

function cookiesOf(response) {
  const raw = typeof response.headers.getSetCookie === "function"
    ? response.headers.getSetCookie()
    : [response.headers.get("set-cookie")].filter(Boolean);
  return raw.map((entry) => entry.split(";")[0].trim()).filter(Boolean);
}

function blobAt(path) {
  // blob 原始字节，绕开本地 checkout 的换行转换（core.autocrlf）。
  return execFileSync("git", ["show", `${COMMIT}:${path}`], { maxBuffer: 64 * 1024 * 1024 });
}

/** 当前 assets 目录下被 git 跟踪的全部文件，路径已去掉目录前缀。 */
function trackedAssetFiles() {
  return execFileSync("git", ["ls-files", ASSETS_DIR], { encoding: "utf8" })
    .split("\n").map((line) => line.trim()).filter(Boolean)
    .map((path) => path.slice(ASSETS_DIR.length + 1));
}

/**
 * 未登录可达性探测用的那条静态资源路径：取目录里真实存在的一个文件。
 * 不写死某个具体路径——换了 assets 根之后写死的路径根本不存在，而那条断言测的是「会话门挡不挡」，
 * 不管文件在不在，会一直绿着。
 */
function assetProbePath() {
  const files = trackedAssetFiles();
  return files.find((path) => path !== "index.html" && !path.endsWith(".pem")) || "styles.css";
}

/**
 * 取线上资产时等它「到位」：HTTP 200 且字节等于该 commit 的 blob。
 *
 * Cloudflare 把新版本切到每个边缘有个窗口，而窗口不只表现为 404：2026-09-22 那次发布实测——
 * 版本创建后约 2 秒去看，嵌套目录下的文件全是 404；3 分钟后再看同一个 URL 是 200 且字节正确
 * （`/app/api.js` 12089 B、`/app/main.js` 20001 B）。2026-09-24 22:29:52→22:29:55 那次发布又撞上另一种
 * 中间态：探针文件没走 404 分支（走了就至少等 5 秒，而这一步 3 秒就结束了），逐字节比对却当场判红；
 * 三分钟后原样重跑同一个 run 是 18/18 全绿。两种都不是漂移，但都会把发布判红。所以宽限的判据是
 * 「内容是否已等于目标 blob」，不是「是不是 404」。
 * 等不到也不放弃比对：真正的漂移（发错版本、漏发文件）正是后面要判红的东西。
 *
 * 预算是每段比对各自的窗口（探针一段、逐文件一段），所以真漂移最多拖住 2 × 预算。
 */
let graceDeadline = 0;

/** 为下一段比对开一段宽限窗口。 */
function armGrace() {
  graceDeadline = Date.now() + ASSET_GRACE_BUDGET_MS;
}

/** 还有宽限预算就等一轮再重取；返回 false 表示预算用尽，按最后一次的响应判定。 */
async function waitForGrace(served, why) {
  const leftMs = graceDeadline - Date.now();
  if (leftMs <= 0) return false;
  const waitMs = Math.min(ASSET_GRACE_WAIT_MS, leftMs);
  console.log(
    `      ${served} ${why}：新资产在边缘生效有个窗口，等 ${Math.round(waitMs / 1000)} 秒再取一次（宽限预算剩 ${Math.round(leftMs / 1000)} 秒）。`,
  );
  await sleep(waitMs);
  return true;
}

/** 取一个线上资产并与预期 blob 比对，未到位时在宽限预算内重取。 */
async function fetchTrackedAsset(path, session, expected) {
  const served = path === "index.html" ? "/" : `/${path}`; // 首页只挂在根路径上
  let rounds = 0;
  for (;;) {
    rounds += 1;
    const response = await request(served, { headers: { Cookie: session } });
    if (response.status !== 200) {
      if (await waitForGrace(served, `返回 HTTP ${response.status}`)) continue;
      return { served, status: response.status, live: null, rounds };
    }
    const live = Buffer.from(await response.arrayBuffer());
    if (live.equals(expected)) return { served, status: 200, live, rounds };
    if (!(await waitForGrace(served, `仍是 ${live.length}B（目标 ${expected.length}B）`))) {
      return { served, status: 200, live, rounds };
    }
  }
}

/** 先拿目录里一个真实文件探一次，看这个边缘上的新资产有没有到位（到位或预算用尽为止）。 */
async function waitForAssets(session) {
  const probePath = assetProbePath();
  const expected = blobAt(`${ASSETS_DIR}/${probePath}`);
  armGrace();
  const attempt = await fetchTrackedAsset(probePath, session, expected);
  const settled = attempt.status === 200 && Boolean(attempt.live) && attempt.live.equals(expected);
  return { probePath, status: attempt.status, rounds: attempt.rounds, settled };
}

/**
 * 收尾：汇总 + 设置退出码。
 *
 * 必须由一个函数承担，而且 main() 的每条提前退出路径都要走它：`process.exitCode = 1` 只在这里设置，
 * 之前写成 `return` 的那些分支（CSRF 缺失、登录失败）会把已经记进 failures 的 FAIL 连同退出码一起丢掉——
 * 门禁以 exit 0 收场，而「线上内容与 commit 逐字节一致」这一项根本没跑，正是这道门禁
 * 想避免的「绿灯但没验」。SITE_PASSWORD 轮换或填错时就会走到那条路。
 */
function finish() {
  const passed = results.filter((entry) => entry.ok).length;
  console.log(`\n结果：${passed}/${results.length} 项通过`);
  if (failures.length) {
    console.log("失败项：");
    for (const failure of failures) console.log(`  - ${failure}`);
    process.exitCode = 1;
  }
}

async function main() {
  console.log(`站点：${SITE}`);
  console.log(`比对基准：${COMMIT}（${ASSETS_DIR}/ 下全部被跟踪文件）\n`);

  // ---- 第 1 步：无凭据可达性 ----
  const loginPage = await request("/login");
  const loginHtml = await loginPage.text();
  record(loginPage.status === 200 && /name="password"/u.test(loginHtml), "登录页可达", `HTTP ${loginPage.status}`);

  const csp = loginPage.headers.get("content-security-policy") || "";
  record(csp.includes(BLOB_CSP), "CSP 仍放行签名 blob", csp ? "connect-src 已核对" : "缺少 CSP 头");

  const root = await request("/");
  const rootBody = await root.text();
  let gated = false;
  try {
    gated = root.status === 401 && JSON.parse(rootBody).error === "login_required";
  } catch {
    gated = false;
  }
  record(gated, "未登录访问被会话门挡住", `HTTP ${root.status}`);

  const probePath = assetProbePath();
  const asset = await request(`/${probePath}`);
  record(asset.status === 401, "静态资源未登录不可读", `HTTP ${asset.status}`);

  // ---- 第 2 步：有凭据字节比对 ----
  if (!PASSWORD) {
    console.log("\n未提供 SITE_PASSWORD：跳过字节比对，本次**没有**验证线上内容与 commit 一致。");
  } else {
    const csrf = /name="csrf" value="([A-Za-z0-9_-]{32})"/u.exec(loginHtml)?.[1] || "";
    const csrfCookie = cookiesOf(loginPage).find((entry) => entry.includes("login-csrf")) || "";
    record(Boolean(csrf && csrfCookie), "登录页下发 CSRF 令牌");
    if (!csrf || !csrfCookie) return finish();

    const loginResponse = await request("/login", {
      method: "POST",
      headers: {
        "Content-Type": "application/x-www-form-urlencoded",
        Origin: SITE,
        Cookie: csrfCookie,
      },
      body: new URLSearchParams({ csrf, password: PASSWORD }).toString(),
    });
    const session = cookiesOf(loginResponse).find((entry) => entry.startsWith(`${SESSION_COOKIE}=`)) || "";
    record(loginResponse.status === 303 && Boolean(session), "登录换取会话", `HTTP ${loginResponse.status}`);
    if (!session) return finish();

    const grace = await waitForAssets(session);
    if (!grace.settled) {
      console.log(`      /${grace.probePath} 在宽限预算内没有到位（取了 ${grace.rounds} 次，最后一次 HTTP ${grace.status}）：按真实漂移处理。`);
    } else if (grace.rounds > 1) {
      console.log(`      /${grace.probePath} 在第 ${grace.rounds} 次取到时到位。`);
    }

    // 逐文件比对开自己的一段宽限窗口：探针可能已经把上一段预算耗在某一个边缘的传播上，
    // 而这里每个文件面对的是同一个边缘的同一批资产，不该因为探针等得久而失去重取机会。
    armGrace();
    // /site-config.js 不在这份清单里：它由 worker/router.js 现算（注入 mode 与公钥指纹），
    // 不是仓库里的文件，没有可比对的 blob。
    const files = trackedAssetFiles();
    const mismatches = [];
    for (const path of files) {
      const expected = blobAt(`${ASSETS_DIR}/${path}`);
      const got = await fetchTrackedAsset(path, session, expected);
      if (got.status !== 200) {
        mismatches.push(`${path} -> HTTP ${got.status}`);
        continue;
      }
      if (!got.live.equals(expected)) {
        mismatches.push(`${path} -> ${got.live.length}B sha=${createHash("sha256").update(got.live).digest("hex").slice(0, 12)} ≠ ${expected.length}B sha=${createHash("sha256").update(expected).digest("hex").slice(0, 12)}`);
      }
    }
    record(mismatches.length === 0, `线上内容与 ${COMMIT} 逐字节一致`, mismatches.length === 0 ? `${files.length}/${files.length} 个文件` : mismatches.slice(0, 5).join("; "));
    if (mismatches.length > 5) console.log(`     …另有 ${mismatches.length - 5} 处不一致`);
  }

  finish();
}

await main();
