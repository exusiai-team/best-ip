// 发布门闩：只有 Cloudflare Workers Builds 构建 main 时才放行 `npm run deploy`，本地一律拒绝。
//
// 2026-09-24 那次本地发布把 CRLF 工作区的字节发上了线（本机 core.autocrlf=true）。根治靠
// .gitattributes（全仓 eol=lf）与 Workers Builds 的干净检出；这里只保证发布不会从本机发起。
// 退出码：0 放行；2 不在 Workers Builds 里；3 构建分支不是 main。

if (process.env.WORKERS_CI !== "1") {
  console.error(
    [
      "拒绝本地发布：发布只走 Cloudflare Workers Builds（push 或合并到 main 即自动发布）。",
      "需要重发时，在 Cloudflare 控制台该 Worker 的 Deployments / Builds 里对 main 重新构建。",
      "本地只做只读检查：npm run check、npm run dry-run。",
    ].join("\n"),
  );
  process.exit(2);
}

const branch = process.env.WORKERS_CI_BRANCH || "";
if (branch !== "main") {
  console.error(
    `拒绝发布：构建分支是 ${branch || "（未知）"}。非生产分支应使用 Workers Builds 的预览命令，不要设成 npm run deploy。`,
  );
  process.exit(3);
}
