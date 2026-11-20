// runner 侧：把 /api/v1/classify 的分类结果（或失败码）交回 Worker 的 /api/scan-result（worker/service.js）。
//
// 凭领取订阅时拿到的 runner 令牌（scripts/claim_subscription.mjs 写进的文件），与进度回报同一张。
// 用法（scan.yml 里）：
//   WORKER_URL=https://… node scripts/publish_classify.mjs --token-file F --result "$RUNNER_TEMP/classify.json"
//   WORKER_URL=https://… node scripts/publish_classify.mjs --token-file F --failure-file "$RUNNER_TEMP/scan-failure"
// 失败文件不存在或内容不合规时报 scan_failed。只在连不上 / 5xx 时重试。

import fs from "node:fs";

function argument(name) {
  const index = process.argv.indexOf(name);
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : null;
}

function failureCode(path) {
  try {
    const code = fs.readFileSync(path, "utf8").trim();
    return /^[a-z0-9_]{1,48}$/u.test(code) ? code : "scan_failed";
  } catch {
    return "scan_failed";
  }
}

async function post(workerUrl, token, body) {
  for (let attempt = 1; ; attempt += 1) {
    let response = null;
    try {
      response = await fetch(`${workerUrl}/api/scan-result`, {
        method: "POST",
        headers: { "X-Best-IP-Relay-Token": token, "Content-Type": "application/json" },
        body,
        redirect: "error",
        signal: AbortSignal.timeout(60_000),
      });
    } catch {
      response = null;
    }
    if (response && response.status < 500) {
      if (response.ok) return;
      const payload = await response.json().catch(() => null);
      throw new Error(`交回结果失败（HTTP ${response.status} ${payload?.error || ""}）`.trim());
    }
    if (attempt >= 3) throw new Error(`交回结果失败（${response ? `HTTP ${response.status}` : "连不上 Worker"}）`);
    await new Promise((resolve) => setTimeout(resolve, attempt * 3_000));
  }
}

try {
  const workerUrl = String(process.env.WORKER_URL || "").replace(/\/+$/u, "");
  if (!/^https:\/\/[A-Za-z0-9.-]+$/u.test(workerUrl)) throw new Error("worker_url 无效");
  const tokenFile = argument("--token-file");
  if (!tokenFile) throw new Error("缺少参数 --token-file");
  const token = fs.readFileSync(tokenFile, "utf8").trim();
  const resultFile = argument("--result");
  const failureFile = argument("--failure-file");
  if (resultFile) {
    await post(workerUrl, token, fs.readFileSync(resultFile, "utf8"));
    console.log("classify_published: 分类结果已交回 Worker");
  } else if (failureFile) {
    const code = failureCode(failureFile);
    await post(workerUrl, token, JSON.stringify({ status: "failed", error: code }));
    console.log(`classify_failure_published: ${code}`);
  } else {
    throw new Error("需要 --result 或 --failure-file");
  }
} catch (error) {
  process.stderr.write(`${error?.message || "交回结果失败"}\n`);
  process.exitCode = 1;
}
