# 设计决策记录

每条只记**现在生效的决定**和一句理由。被推翻的旧方案不再展开，细节和取证过程在 git 历史里（`.agents/notes/` 在 2026-10-24 收成本文件，旧笔记可用 `git log -- .agents/notes` 找回）。

## 扫描核心

- **每次尝试一个全新 Mihomo 进程，独立工作目录**，结束（含被取消）时确认停止并删除目录，否则以 `MihomoStopError` 收口。理由：节点之间、尝试之间互不污染；清理不确认就不发布产物。
- **重试：第 1 次失败后，余下尝试并行跑，谁先成功用谁**，其余取消。连不上的节点从约 18 秒降到约 11 秒；`attempt_count` 只数失败的加胜出的那一次。
- **节点并发默认 `min(16, max(8, CPU))`**，workflow 里固定 16。交错 A/B：同一窗口 50.9s → 30.4s，可评分节点数不变。并发不影响成功率，坏的网络窗口才影响。
- **IPure 宿主直连兜底超时 5 秒**（原 20 秒）；退避上限 2 秒。
- **DNS 引导出口只用全局 IPv4 字面量节点**，按 `(节点序号 + 尝试次数)` 轮换。GitHub runner 没有 IPv6 出口，选中 IPv6 节点会把整次尝试的 DNS 打掉，误报成「域名无法解析」。
- **Mihomo 开 IPv6**：成功率不变，但失败文案能区分「网络不可达」（环境缺 IPv6 出口）和「域名无法解析」。
- **Mihomo 就绪 = 控制端口应答且 selector 组已注册**。控制端口先接客、tunnel 后填充，只看 `/version` 会 404 打掉健康节点。
- **扫描日志一行 `scan_timing`**（只有数字）：产物 1 天后过期，事后靠这一行定位耗时在哪一相。调节点侧参数前先看它。

## 数据与评分

- **IPure 以官方 `/docs/api` 为准**：`GET /api/lookup?ip=`，不带凭据，六个场景（ai / social / streaming / gaming / ecommerce / email）。
- **评分取值域 `0..100 ∪ {-1} ∪ {null}`**：`-1` 表示上游标了地区受限，由后端写入，前端不推导，显示成中性灰的 `-1`；`score` 必须等于 `ipure_scores.total`。
- **归属信息 IPure 优先、按组回落 Coffee**；`network_source` 记录每组来源，`usage_type` 取 IPure 的六档。`exit_ip` 仍由 Coffee trace 发现（查 IPure 需要先有地址）。
- **色带复刻 ipure.dev 的五段渐变**，锚点按 Coffee 的亮度带重新取色，保证对比度过 AA。

## 结果与校验

- **节点记录规则只在 runner 上，一处**（`backend/app/results/validation.py`）：写入时逐条、finalize 时整体、发布前 `build_artifact` 再整体一次。浏览器读取器只核对完整性：运行身份、`result_sha256`、计数、manifest 与记录一致。原先两端各一套 schema，每加一个字段都要两边同步。
- **产物里不许出现订阅凭据**：订阅 URL、其中的 token、订阅里的密码一律做禁止值扫描，命中就拒绝发布；实时进度里的节点名命中时换成「节点 N」。
- **artifact 字节由浏览器直取**，Worker 只换签名地址（`*.blob.core.windows.net`）。Cloudflare 出口到这族主机实测大量 522。

## 运行与通路

- **抓订阅：直连优先，Worker 中继只在 `http_status_403` / `direct_timeout` / `direct_unreachable` 时回退**。同一台订阅主机可能只拒 Azure，也可能只拒 Cloudflare，写死任何一侧都会把另一侧打成 403。逐跳公网校验只在 runner 做。
- **实时进度：runner 约 0.75 秒报一次 → Worker 的 Durable Object → 页面每秒读一次**。只有计数、阶段和节点名，没有出口 IP 与评分。结果只认终态 artifact。
- **扫描状态轮询只在「运行中但读不到 runner 进度」时才取 Actions job 明细**（页面拿步骤名兜底推断阶段）。
- **页面内发起扫描，订阅地址不进 GitHub**：页面把地址交给 Worker，暂存在 Durable Object（15 分钟，领取即删），workflow 输入只有 `request_id` 与 `worker_url`。原先的 RSA-OAEP + AES-GCM 信封要在两边各配一份密钥与指纹，而 Worker 的订阅中继本来就看得到明文，加密只挡住了 GitHub 一侧；现在 GitHub 一侧根本拿不到它。
- **runner 身份用 GitHub Actions OIDC，不用共享密钥**：Worker 用 GitHub 的 JWKS 验签并核对仓库、`main`、`scan.yml`、`workflow_dispatch` 与 audience；领取时发一张只对这次运行有效的随机 runner 令牌给中继与进度回报用。installation ID 自动查询，HMAC 密钥从 App 私钥派生。整个系统只剩 4 项配置（`SITE_PASSWORD`、`GITHUB_APP_ID`、`GITHUB_APP_PRIVATE_KEY`、Actions 的 `IPURE_CONFIG_YAML`）。

## 前端

- **生产 CSP 只认 CSSOM**：标记里不写 `style=`，由 JS 在节点入 DOM 之后写 `element.style`；不写内联 `<script>`；出网只走 `app/net.js`（统一超时）。这几条由 `scripts/check_frontend_contracts.mjs` 机器检查。
- **单视图宽表**：点列名排序、逐列筛选，筛选状态写进地址栏 hash；窄屏（≤900px）是流式卡片，只有这一个断点。
- **失败对象只有一个工厂** `app/failure.js`，两条网关通路共用状态码文案。
- **离线快照**：`.html` 内联 `copy.js`，`.mhtml` 不内联（零 JS，靠 `user-select: all` 复制）；导出前先 flush 排队中的渲染，避免读到上一帧；标题只在写 `<title>` 时转义一次。

## 发布与流程

- **发布只走 Cloudflare Workers Builds**（OAuth 连仓库）：`main` 上执行 `npx wrangler@latest deploy`（不锁 wrangler 版本，所有者选择自动跟进最新）。`npm run deploy` 经 `deploy_guard.mjs` 只用来拦本机发起的发布；全仓 LF 由 `.gitattributes` 保证。Workers Builds 不等 GitHub CI，合并前要看 CI 是否通过。
- **CI**：`push` 只看 `main`，PR 由 `pull_request` 覆盖；跑 Ruff、pytest、`npm run check`、`wrangler deploy --dry-run`。
- **测试用替身，不联网**：替身 Mihomo 与采集器覆盖重试、并行与清理，加上 CLI 端到端和产物跨语言读回。真实订阅的实测直接在线上页面发起一次扫描（原 `verify-scan.yml` 依赖信封加密，已删除）。（2026-09-21 曾决定不维护测试；2026-10-24 按所有者要求恢复。）
- **不锁依赖版本**（所有者选择自动跟进最新）：不提交 `package-lock.json` / `uv.lock`，`wrangler` 写 `latest`，生产部署用 `npx wrangler@latest deploy`；`scan.yml` 的 Python 环境按天缓存，Mihomo 每次取最新 release，在 Actions 里没有 GitHub 资产摘要就拒装。Action 用浮动大版本标签（`astral-sh/setup-uv` 从 v8 起不发浮动标签，只能写完整版本）。代价：上游出不兼容的版本时会直接失败，届时再临时指定版本。
- **非生产分支跑 `wrangler preview`**，`wrangler.jsonc` 里放空的 `previews` 块（新版 wrangler 要求）。预览不继承绑定与密钥，只用来证明能构建、能上传。
- **Actions runner 固定 `ubuntu-24.04`**。
- **旧前端 `frontend/` 已删除**；要回退用 `git revert`。
