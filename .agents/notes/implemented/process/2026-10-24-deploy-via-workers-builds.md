# Agent Note: 发布改走 Cloudflare Workers Builds，删除 `worker.yml`

Status: implemented

## Problem

发布原来走 `.github/workflows/worker.yml`（[CI 成功后自动发布](2026-09-21-worker-autodeploy-on-ci-success.md) + [只走远程](2026-09-24-deploy-only-through-remote.md)）。这条路要在 GitHub 上存两份 Cloudflare 长期凭据：`CLOUDFLARE_API_TOKEN`、`CLOUDFLARE_ACCOUNT_ID`。部署后自证还要再存一份 `SITE_PASSWORD`。仓库所有者已经在 Cloudflare 控制台用 OAuth 把 Worker 连上了本仓库（Workers Builds），想把这些部署用的 secret 删掉。两条发布路径同时存在会重复发布，所以 `worker.yml` 也必须删。

另外，`scripts/deploy_guard.mjs` 原来只认 `GITHUB_ACTIONS=true`。在 Workers Builds 里跑 `npm run deploy` 一定会以退出码 2 失败。

## Decision

- 删除 `.github/workflows/worker.yml`。发布入口只剩 Workers Builds：生产分支 `main`，构建命令 `npm ci`，部署命令 `npm run deploy`。
- `deploy_guard.mjs` 的判据换成 Workers Builds 注入的变量，四道断言的形状不变：
  1. `WORKERS_CI !== "1"` → `exit 2`（本地照旧拒绝）；
  2. `WORKERS_CI_BRANCH !== main` → `exit 3`（非生产分支应走预览命令，不该跑 `npm run deploy`）；
  3. `HEAD !== WORKERS_CI_COMMIT_SHA` → `exit 4`；
  4. assets 目录逐字节等于 commit 的 blob → 否则 `exit 1`。
  变量名取自 Cloudflare 文档的「Workers Builds default variables」（`CI`、`WORKERS_CI`、`WORKERS_CI_BUILD_UUID`、`WORKERS_CI_COMMIT_SHA`、`WORKERS_CI_BRANCH`）。
  守卫**不再**放行 `GITHUB_ACTIONS`：发布入口只有一个，任何 workflow 都不该再能发布。
- README 部署章节、`docs/CONTRACTS.md` 的发布描述和环境变量表同步更新。README 顺带改正两处与代码不符的地方：`IPURE_CONFIG_YAML` 实际是必填（`scan.yml` 里 `test -n` 并断言有 cookie）；Worker secret 清单补上 `SUBSCRIPTION_RELAY_TOKEN`。

## Alternatives considered

### Why not 让 Workers Builds 用默认部署命令 `npx wrangler deploy`，把守卫删掉？

最强理由：零配置，也少一个脚本。Workers Builds 在 Linux 上干净检出，CRLF 事故的前提（本机 `core.autocrlf=true`）不存在。不选的原因：守卫的第 4 条断言（工作区字节 == blob）在发布**前**就能拦下未跟踪文件、软链之类的问题，不止 CRLF 一种；而且守卫还让本地 `npm run deploy` 继续跑不通。换个判据就能保住这些，成本只是三行环境变量名。

### Why not 保留 `worker.yml`，只把凭据换成 OIDC 之类的短期凭据？

最强理由：保住「CI 绿了才发布」和自动的部署后自证，这是 Workers Builds 给不了的两件事。不选的原因：这样要么凭据还放在 GitHub 上，要么得另外搭一套凭据交换流程，都不符合所有者「删掉部署 secret」的目标。所有者明确选择让 Cloudflare 侧通过 OAuth 拉代码。

## Consequences

- 收益：GitHub 上与部署相关的 secret 从 3 个降到 0 个。`CLOUDFLARE_API_TOKEN`、`CLOUDFLARE_ACCOUNT_ID` 可以删；`SITE_PASSWORD` 只剩 `verify-scan.yml` 在用。
- 代价：**失去 CI 门禁**。Workers Builds 不等 GitHub 上的 `CI` 工作流结果，push 到 `main` 就发布。缓解办法：合并前看 PR 上的 CI；也可以在分支保护里把 `CI / test` 设为必过检查。
- 代价：**失去自动的部署后自证**。`verify:deploy` 改成发布后手动在本地跑。
- 代价：部署命令必须手动填成 `npm run deploy`。填成默认的 `npx wrangler deploy` 会静默绕过守卫，这一点只能靠 README 提醒。
- 事实：`scan.yml`、`verify-scan.yml`、Worker 运行时 secrets 和 Durable Object 绑定都不受影响。`wrangler deploy` 不会覆盖控制台里的运行时 secret。

## Testing

- 守卫逐条实测（scratchpad 里另造一个 git 仓库，内含 `wrangler.jsonc` + 1 个资产）：无 `WORKERS_CI` → `exit 2`；`WORKERS_CI=1 WORKERS_CI_BRANCH=main WORKERS_CI_COMMIT_SHA=<HEAD>` → `exit 0`；分支 `feat` → `exit 3`；SHA 不符 → `exit 4`；改一个字节不提交 → `exit 1`。
- 门禁：`npm run check`、`npm run dry-run`、`uv run ruff check .`。
