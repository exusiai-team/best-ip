// runner 侧：凭 GitHub Actions OIDC 向 Worker 领取这次扫描的订阅地址与 runner 令牌（worker/runner.js）。
//
// 订阅地址从不进入 workflow 输入：页面把它交给 Worker 暂存，这里证明「我是 main 上 scan.yml 的这一次
// 运行」之后领一次，领完 Worker 即删。不需要任何 Actions secret——OIDC 令牌由 GitHub 现签，
// audience 写死为本次的 Worker origin，拿去别的站点也用不了。
//
// 用法（scan.yml 里，job 需要 permissions: id-token: write）：
//   WORKER_URL=https://… REQUEST_ID=… node scripts/claim_subscription.mjs \
//     --subscription-file "$RUNNER_TEMP/subscription-url" --token-file "$RUNNER_TEMP/runner-token"
// 两个文件都以 0600 新建；令牌与订阅地址都先 ::add-mask::，之后哪一步意外打印也只会是 ***。

import fs from "node:fs";

function argument(name, { optional = false } = {}) {
  const index = process.argv.indexOf(name);
  if (index < 0 || !process.argv[index + 1]) {
    if (optional) return null;
    throw new Error(`缺少参数 ${name}`);
  }
  return process.argv[index + 1];
}

async function oidcToken(audience) {
  const requestUrl = process.env.ACTIONS_ID_TOKEN_REQUEST_URL;
  const requestToken = process.env.ACTIONS_ID_TOKEN_REQUEST_TOKEN;
  if (!requestUrl || !requestToken) throw new Error("没有 OIDC 环境（job 需要 permissions: id-token: write）");
  const url = new URL(requestUrl);
  url.searchParams.set("audience", audience);
  const response = await fetch(url, {
    headers: { Authorization: `Bearer ${requestToken}` },
    signal: AbortSignal.timeout(15_000),
  });
  const payload = await response.json().catch(() => null);
  if (!response.ok || typeof payload?.value !== "string") throw new Error(`取 OIDC 令牌失败（HTTP ${response.status}）`);
  return payload.value;
}

async function claim(workerUrl, requestId, jwt) {
  // 只在连不上 / 5xx 时重试：401/410 说明身份或订阅本身不对，重试不会变好。
  for (let attempt = 1; ; attempt += 1) {
    let response = null;
    try {
      response = await fetch(`${workerUrl}/api/scan-claim`, {
        method: "POST",
        headers: { Authorization: `Bearer ${jwt}`, "Content-Type": "application/json" },
        body: JSON.stringify({ request_id: requestId }),
        redirect: "error",
        signal: AbortSignal.timeout(20_000),
      });
    } catch {
      response = null;
    }
    if (response && response.status < 500) {
      const payload = await response.json().catch(() => null);
      if (!response.ok) throw new Error(`领取订阅失败（HTTP ${response.status} ${payload?.error || ""}）`.trim());
      return payload;
    }
    if (attempt >= 3) throw new Error(`领取订阅失败（${response ? `HTTP ${response.status}` : "连不上 Worker"}）`);
    await new Promise((resolve) => setTimeout(resolve, attempt * 2_000));
  }
}

try {
  const subscriptionFile = argument("--subscription-file");
  const tokenFile = argument("--token-file");
  const workerUrl = String(process.env.WORKER_URL || "").replace(/\/+$/u, "");
  const requestId = String(process.env.REQUEST_ID || "");
  if (!/^https:\/\/[A-Za-z0-9.-]+$/u.test(workerUrl)) throw new Error("worker_url 无效");
  if (!/^[A-Za-z0-9_-]{1,128}$/u.test(requestId)) throw new Error("request_id 无效");

  // /api/v1/classify 的扫描可能内联 YAML（没有地址），并另带 classify 上下文（label、复用时长、上一次结果）。
  const yamlFile = argument("--yaml-file", { optional: true });
  const classifyFile = argument("--classify-file", { optional: true });

  const payload = await claim(workerUrl, requestId, await oidcToken(workerUrl));
  const url = payload?.subscription_url;
  const yaml = payload?.subscription_yaml;
  const token = payload?.runner_token;
  if (typeof token !== "string" || !/^[A-Za-z0-9_.-]{16,512}$/u.test(token)) throw new Error("Worker 返回的 runner 令牌无效");
  process.stdout.write(`::add-mask::${token}\n`);
  if (typeof yaml === "string") {
    if (!yamlFile || !yaml || Buffer.byteLength(yaml, "utf8") > 5 * 1024 * 1024) throw new Error("Worker 返回的订阅内容无效");
    fs.writeFileSync(yamlFile, yaml, { encoding: "utf8", flag: "wx", mode: 0o600 });
  } else {
    if (typeof url !== "string" || !/^https?:\/\//u.test(url) || url.length > 4096 || /[\s]/u.test(url)) {
      throw new Error("Worker 返回的订阅地址无效");
    }
    process.stdout.write(`::add-mask::${url}\n`);
    fs.writeFileSync(subscriptionFile, `${url}\n`, { encoding: "utf8", flag: "wx", mode: 0o600 });
  }
  fs.writeFileSync(tokenFile, token, { encoding: "utf8", flag: "wx", mode: 0o600 });
  if (payload?.classify && classifyFile) {
    fs.writeFileSync(classifyFile, JSON.stringify(payload.classify), { encoding: "utf8", flag: "wx", mode: 0o600 });
  }
  console.log(`subscription_claimed: 已领取订阅${typeof yaml === "string" ? "内容" : "地址"}与 runner 令牌`);
} catch (error) {
  process.stderr.write(`${error?.message || "领取订阅失败"}\n`);
  process.exitCode = 1;
}
