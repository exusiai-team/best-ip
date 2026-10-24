# Best IP

Cloudflare Worker 托管前端与同源 API，通过 GitHub App 调度固定的 GitHub Actions `scan.yml`。Actions runner 使用独立 Mihomo 工作区并发检测订阅节点，生成经过脱敏和完整性校验的短期 artifact；浏览器只在 artifact 完整通过 ZIP、CRC、SHA-256、运行身份、manifest 和节点记录校验后展示结果。

本仓库只支持这一条生产路径：

```text
浏览器
  └─ 访问密码登录（签名 Cookie）
      └─ Cloudflare Worker + Static Assets
          └─ GitHub App → GitHub Actions scan.yml
              ├─ Mihomo 节点扫描
              └─ sanitized artifact → Worker 换取签名地址 → 浏览器直取字节
```

生产扫描由 `scan.yml` 直接运行 `scripts/run_scan.py`，不启动 FastAPI。FastAPI 仅用于本地 loopback API：独立静态服务提供页面，前后端分离。仓库不提供 Docker、Compose 或 GitHub Pages 模式。

## 本地调试

线上页面（`frontend-next/`）是一张像电子表格一样用的宽表：点列名排序（再点反向、第三下取消，Shift+点击叠加次级排序），表头下一行逐列筛选（文本列「包含即命中」，支持空格多词、`a|b`、`!排除`；分数列支持 `80`（≥80）、`>80`、`<=60`、`60-90`、`=-1`），点单元格里的国家 / 服务商 / AS 号 / 接入 / 原生性 / 协议即可一键筛这个值；IPure 六项场景评分各占一列，可单独排序筛选。筛选与排序写进地址栏 hash，刷新与分享都能还原；窄屏变成卡片，表头变成排序胶囊条与可收起的筛选格。导出的离线快照去掉筛选控件，摘要写明当时的筛选与排序。

本地调试模式复用同一套 Mihomo、扫描器、结果存储和前端，只替换任务传输层；启动器分别运行前端静态服务与 FastAPI API。生产环境仍使用 Cloudflare Worker → GitHub Actions，保留密码登录、密文订阅与 artifact 校验。

安装 [uv](https://docs.astral.sh/uv/) 后，在仓库根目录运行：

```powershell
npm run dev
```

也可以不经过 npm：

```powershell
uv run --no-dev python scripts/dev.py
```

首次启动会自动下载当前固定版本的 Mihomo；前端默认监听 `127.0.0.1:5173`，后端 API 默认监听 `127.0.0.1:8000`，端口占用时各自自动选择空闲端口。准备就绪后浏览器只打开前端地址，前端通过动态的 loopback API Base 调用后端。Windows 下自动关闭后端热重载，因为 Uvicorn 的重载模式使用不支持异步子进程的 SelectorEventLoop，会导致 Mihomo 无法启动；修改后端代码后需重启。其他系统默认启用热重载；需要稳定执行长扫描时使用：

```powershell
npm run dev:no-reload
```

可用 `--port 8080` 更换后端 API 端口、`--frontend-port 5174` 更换前端端口、`--no-open` 禁止自动打开浏览器。手动启动时必须同时设置 `BEST_IP_LOCAL_DEV=1` 与精确的 `BEST_IP_LOCAL_FRONTEND_ORIGIN=http://127.0.0.1:<前端端口>`；不要将任一服务绑定到局域网或公网地址，因为本地模式有意跳过生产 Worker 的密码登录与 scan token，只用于本机调试。

固定正式版本（如 `v1.19.30`）安装遇到 GitHub API `403`/`429` 时，自动改用官方 Release 直链，不依赖第三方镜像。`latest` 与非标准 tag 不猜测下载地址。设置 `BEST_IP_MIHOMO_ARCHIVE_SHA256` 可强制校验归档；回退时若未指定摘要，则仅依赖官方 HTTPS，计算出的 SHA-256 不代表真实性校验。已有核心可通过 `BEST_IP_MIHOMO_PATH` 指定路径，跳过下载。

## 生产行为

- 输入必须是顶部含 `proxies` 的 UTF-8 Mihomo/Clash YAML 公开 HTTP/HTTPS 地址。
- 浏览器使用 AES-256-GCM 加密订阅 URL，再用 `frontend/scan-public.pem` 对应的 RSA-OAEP-3072 公钥包裹 AES key；明文 URL 不进入 Worker API、GitHub workflow input 或 artifact。
- Worker 固定调度 `silverwolfxai/best-ip` 的 `main` 分支和 `scan.yml`，不提供通用 GitHub API 代理。
- 每个节点尝试使用独立 Mihomo 进程、端口、连接池和临时目录；默认最多 `min(16, max(8, CPU 核数))` 个节点并发（上限为订阅节点数），每节点最多 3 次尝试：首次失败后余下的尝试**并行**跑，第一份非失败记录胜出、其余立即取消并确认清理（永久连不上的节点从约 18 秒降到约 11 秒）。
- 出口 IP、国家和地区、服务商 / ISP、ASN、接入（住宅 / 移动 / 商业 / 机房 / 教育 / 政府）与原生性以 IPure `/api/lookup` 报告为准（`geo.countryName/country/region/city`、`asn.asn/org/name`、`usageType`、`nativeType`）：报告里说了的那一组就用它，没说的那一组才回落到 Coffee 的归属查询；每组来源记在节点记录的 `network_source`，页面在对应单元格的 title 里标出「来源：IPure / Coffee」。出口 IP 仍由 trace 发现（IPure 查询必须带上一个 IP），IPure 回显一致时记为由 IPure 确认。
- Coffee 页面与 trace 并行；确认出口 IP 后，lookup、global ping、portscan、pingcheck、related、IPure 官方 `/api/lookup` 评分及 ChatGPT/Codex 探测按既定依赖并发执行。IPure 没有返回纯净度总分时，节点会如实标记为“部分”。
- IPure 接入完全按官方 `/docs/api` 契约：`GET /api/lookup?ip={ip}` 无需 API key 或 Cookie。响应解析出 `risk.purity` 纯净度总分、`risk.level` / `risk.label` / `risk.verdict` 档位，以及 `scenarios[]` 的六项场景（`ai`、`social`、`streaming`、`gaming`、`ecommerce`、`email`）评分与档位，另附 `reportUrl`、`source`（fresh / cache / store）与 `stale`。上游档位只用于判断是否写哨兵，**不进入记录**（`ipure_level` / `ipure_verdict` / `ipure_scenario_levels` 已从落盘契约删除，读取时容忍旧 artifact）；前端不为 IPure 分数渲染任何档位或状态文案：六项评分一律显示成带颜色的数字，档位为 `restricted`（地区受限）的项按后端写入的 `-1` 哨兵显示为中性灰的 `-1`，既不参与数值排序也不被分数筛选命中。表格与详情卡里剩下的「受限 / 不可用」文字属于 GPT · Codex 可达性探针（chatgpt.com / api.openai.com 的连通状态），与 IPure 分数无关。
- IPure 每次实际 HTTP 请求都会重新读取 `config/ipure.yml` 的 `headers`，修改后无需重启；该文件只用于覆盖请求头（如 User-Agent），已排除出 Git，示例见 `config/ipure.example.yml`。未配置时使用 JSON / `MyIPChecker/1.0` 默认值。`429`（含 `code=open_rate_limited`）最多尝试 3 次，按 1、2 秒退避（上限 2 秒）并遵守秒数形式的 `Retry-After`，等待不会超出查询时间预算，也不切换出口重试；`403`（`code=verification_required`）不盲目重试，而是把官方给出的 `reportUrl` 一并记录。响应头 `x-open-budget-remaining` 会写入 `proxy_evidence.ipure_budget_remaining`。
- mixed-port 与 controller 只监听 `127.0.0.1`；所有请求固定 `trust_env=False` 并禁用重定向。IPure 不读取也不保存任何凭据，因此不存在凭据回退或验证会话；宿主侧直连查询是记录在案的例外，必须与节点出口证据一起落盘（见 `docs/CONTRACTS.md`）。
- 每个真实节点恰好产生一条 `success`、`partial` 或 `failed` 记录。失败记录的 `exit_ip` 必须为 JSON `null`。
- 节点文件原子写入；只有节点集合、字段、状态、出口 IP、代理证据、文件大小和 SHA-256 全部通过后才生成 manifest。
- 扫描进度画成三段步骤条（启动 → 扫描节点 → 校验结果；提交、排队、准备环境合成「启动」一格），配每秒走的用时。面板主体是**逐节点格子**：每个节点一格，实时显示等待 / 启动代理 / 连接检测站 / 查询 IP 质量 / 重试，用时每秒往上走，终态给完整 / 部分（缺哪几项）/ 失败（白名单原因）。读数来自 runner 约每秒一次的回报（`POST /api/scan-progress`，存在 Worker 的 `SCAN_PROGRESS` Durable Object 里），页面每秒读一次 `GET /api/scans/{id}/progress`（只读 Durable Object，不耗 GitHub API）。格子里没有出口 IP 与评分；读不到回报时进度条退回不定状态，不伪造数字。完成那一刻面板计数改用终态 artifact，格子只在三种终态个数与 artifact 逐一相同时保留——表格与导出仍以校验过的 artifact 为唯一事实源。
- 刷新页面或换标签页后，若最近一次扫描还在排队/执行，页面会自动跟进它的进度（`/api/scans/latest` 的 `active`），跑完自动换上新结果；跟进只读，不能停止（停止需要发起时内存里的 scan token）。
- 页面轮询首轮 1.5 秒、每次 ×1.5、上限 4 秒；节点检测完（runner 报了 packaging/done）后改为每秒一次，运行一结束就取结果。`scan.yml` 把单任务节点并发设为 16（托管 runner 默认公式只给 8）。
- `scan.yml` 的准备段压到三步：校验输入 + 写 IPure 配置 + 解密订阅一步完成（私钥用完即删）；uv 管理的 Python 3.13、`.venv` 与 Mihomo 压缩包同一个缓存，命中时不装 uv、不跑 `uv sync`，直接 `.venv/bin/python`；`uv.lock` / `pyproject.toml` / Mihomo 版本变化时才走一次装 uv + `uv sync` 并存回缓存。

## 部署

发布走 Cloudflare Workers Builds（Worker 在控制台用 OAuth 连接本仓库），本地 `npm run deploy` 会被 `scripts/deploy_guard.mjs` 直接拒绝（退出码 2）。发布路径：

1. **自动**：push（或合并 PR）到 `main` → Workers Builds 检出该 commit，执行部署命令 `npm run deploy`。
2. **手动重发一次**：在 Cloudflare 控制台该 Worker 的 Deployments / Builds 里对 `main` 重新构建。

Workers Builds 的构建设置（Settings → Build）：

- 生产分支：`main`；构建命令：`npm ci`；部署命令：**`npm run deploy`**。不要用默认的 `npx wrangler deploy`，那样会绕过下面的守卫。
- 非生产分支：用默认的预览命令（只出预览版本，不上线），或直接关掉非生产分支构建。不要设成 `npm run deploy`，守卫会以退出码 3 拒绝。
- Build variables 一个都不需要；运行时密钥只放 Worker 的 Variables and Secrets（见下文「平台配置」），`wrangler deploy` 不会覆盖它们。

注意：Workers Builds 不等 GitHub 上的 `CI` 工作流结果，push 到 `main` 就会构建。所以合并前要看 PR 上的 CI 是绿的；也可以在 GitHub 的分支保护里把 `CI / test` 设成必过检查。

为什么本地不许发布：本机检出可能是 CRLF（`core.autocrlf=true`，而仓库 blob 是 LF），也可能带着未提交的改动，`wrangler deploy` 会把这些字节原样发到线上，而它只报「上传成功」，不会说发上去的是不是仓库里的内容——历史上线上就这样跑过与任何 commit 都不一致的资产（`app/api.js` 线上 12327B vs blob 12089B，多出的 238 字节全是 CR）。所以守卫只在 Workers Builds 里放行（`WORKERS_CI=1`），并在发布前断言三件事：构建分支是 `main`（`WORKERS_CI_BRANCH`）、`HEAD` 等于本次构建声明的 commit（`WORKERS_CI_COMMIT_SHA`）、以及 assets 目录下每个文件都被 git 跟踪且工作区字节逐字节等于该 commit 的 blob。

本地能做的只有只读检查：

```powershell
npm ci
npm run check         # 逐文件 node --check
npm run dry-run       # wrangler deploy --dry-run，只看打包
npm run verify-notes  # 笔记门禁
$env:SITE_PASSWORD="…"; npm run verify:deploy   # 核对线上内容与当前 commit 是否逐字节一致
```

部署后自证改为手动：发布完成后在本地跑 `npm run verify:deploy`（见上），分两层：无凭据检查（登录页可达、CSP 仍放行签名 blob、静态资源仍被会话门挡住），以及有凭据的逐字节比对（用 `SITE_PASSWORD` 登录后，把 `frontend-next/` 下每个被 git 跟踪的文件与它在该 commit 里的 blob 比对）。新资产在边缘生效有个窗口（可能先返回 404，也可能先返回旧字节），这一步在宽限预算内会重取，不会把传播窗口判成漂移。

部署不再需要任何 GitHub Actions secret（原来的 `CLOUDFLARE_API_TOKEN`、`CLOUDFLARE_ACCOUNT_ID` 可以删除）。GitHub 上剩下的 secrets/variables 都是扫描用的，见「平台配置」第 4 条；`SITE_PASSWORD` 只有 `Verify scan flow` 工作流还在用，不用它可以删。

Worker 没有第三方运行时 npm 依赖；密码会话和 GitHub App RS256 签名都使用原生 Web Crypto。`package.json` 与锁文件保留模块声明、语法检查命令和 Wrangler 部署工具。

Worker 使用 Static Assets 托管 `frontend-next/`；当前配置不使用 Cloudflare Pages 或 GitHub Pages。实时进度用一个 SQLite 后端的 Durable Object（`wrangler.jsonc` 的 `SCAN_PROGRESS` 绑定与 `v1` 迁移），首次发布时由迁移自动创建，Workers 免费计划可用，不需要在控制台事先建资源；绑定缺失时页面只是退回「只显示步骤」。

端到端实测走 `Verify scan flow`（`.github/workflows/verify-scan.yml`，人工派发）：先在本机用 `frontend-next/app/scan-crypto.js` 把订阅地址封成信封（明文不进 Actions 输入），再把 `request_id`、`key_id`、`encrypted_subscription_url` 三项交给该工作流。它用 `SITE_PASSWORD` 登录线上站点、经 Worker 发起真实扫描，直接复用前端的 `scan.js` / `gateway.js` / artifact reader 跟进度与逐字节校验，日志只打印聚合计数与耗时。

## 平台配置

1. 创建 GitHub App，只授予当前仓库 `Actions: Read and write` 与 `Metadata: Read`，并只安装到 `silverwolfxai/best-ip`（仓库所有者变了就要在新所有者下重新安装，并更新 `GITHUB_APP_INSTALLATION_ID`）。
2. 在 Worker 的 Settings → Variables and Secrets 添加 Secret `SITE_PASSWORD`：使用密码管理器生成至少 16 个字符的随机密码（最多 1024 字符）。无需 Cloudflare Access；如果之前启用了 Worker 域名的 Access 保护，请关闭该保护，并移除覆盖该域名的 Zero Trust Access Application。原来的三个 `ACCESS_*` 变量可以删除。
3. 配置 Worker secrets/variables：

   - `SCAN_KEY_ID`：`frontend/scan-public.pem` 的 SPKI DER SHA-256 指纹。
   - `GITHUB_APP_ID`
   - `GITHUB_APP_INSTALLATION_ID`
   - `GITHUB_APP_PRIVATE_KEY`
   - `SCAN_TOKEN_SECRET`
   - `SITE_PASSWORD`
   - `SUBSCRIPTION_RELAY_TOKEN`：runner 回调 Worker（订阅中继与 `/api/scan-progress`）的令牌，至少 16 个字符，必须与 Actions secret `SCAN_RELAY_TOKEN` 相同。

4. 配置 Actions secrets `SCAN_PRIVATE_KEY_PEM`、`SCAN_RELAY_TOKEN`、`IPURE_CONFIG_YAML`，并配置 Actions variables `SCAN_KEY_ID`、`SCAN_RELAY_URL`（Worker 的 https 地址）。`IPURE_CONFIG_YAML` 保存与本地 `config/ipure.yml` 相同的请求头覆盖内容，`scan.yml` 要求它非空且带 IPure 验证 cookie，缺失时扫描在准备阶段失败；Actions 扫描前生成该文件，结束时清理，不上传到结果 artifact。Worker 仅调度扫描，不查询 IPure，也不向前端提供任何第三方请求头。可用 `gh secret set IPURE_CONFIG_YAML --repo silverwolfxai/best-ip < config/ipure.yml` 同步本地配置到云端；修改本地文件不会自动更新 GitHub Secret。

打开网站后输入访问密码。登录会话有效期 12 小时，保存在 Secure、HttpOnly、SameSite=Strict 的主机专属 Cookie 中；页面提供退出登录入口。修改 `SITE_PASSWORD` 或 `SCAN_TOKEN_SECRET` 后旧会话失效。退出登录清除当前浏览器 Cookie，其他浏览器的会话不受影响。密码和签名密钥只放 Worker 运行时 Secrets，不放 Build variables 或仓库。

登录接口为 `POST /login`，没有内置分布式尝试次数限制；如需限制在线猜测，可在 Cloudflare 为该路径配置限流规则。缺少登录配置时 Worker 返回 503；扫描配置在调用扫描功能时检查。scan token 有效期两小时，只存在页面内存并通过 `X-Best-IP-Scan-Token` 请求头发送，不进入 URL 或 localStorage。

## API

除登录、退出页面及登录样式外，所有生产请求都需要密码登录会话。浏览器页面导航未登录时跳转 `/login`，API 未登录时返回 401。创建扫描时签发 scan token，不要求预先持有 token；后续查询、下载和取消请求需要内存中的 scan token。状态变更（包括登录和退出）还需要同源 Origin/Fetch-Metadata。

```http
POST /api/scans
GET /api/scans/{request_id}?run_id={run_id}
GET /api/scans/{request_id}/artifact?run_id={run_id}&run_attempt={attempt}
DELETE /api/scans/{request_id}?run_id={run_id}
GET /api/health
```

Worker 会精确核对 request ID、workflow path、事件、分支、run/attempt、创建时间窗和唯一 artifact 名称。取件接口只返回 GitHub 的短期签名地址（`artifact_url`，必须落在 `*.blob.core.windows.net` 上），那 ~90KB 字节由浏览器自己跨域直取；Worker 不下载归档，因为 Cloudflare 出口到这一族 blob 主机在实测中有很大比例会挂死并被边缘改写成 522。地址缺失或不在这一族主机上时按 502 `GitHub artifact 下载地址缺失` 失败。浏览器侧仍然只接受通过 ZIP 目录、CRC、SHA-256、运行身份与 manifest 校验的归档。artifact 只保留 1 天。

## 验证

仓库保留运行源码、部署配置与依赖锁文件；本地工具状态、凭据、Mihomo 二进制和扫描结果不进入 Git。测试套件已在提交 `928c770` 中删除、`/tests/` 同时加入 `.gitignore`，因此本仓库不再有 Python 或 JavaScript 用例：`npm test` 只做语法检查（`node --check`），CI 不再运行 `pytest`。门禁保留的是一条能真跑通的检查 —— Ruff、`npm test`（语法）、`npm run verify-notes` 和 `wrangler deploy --dry-run`。本地启动使用 `--no-dev`，不安装 pytest、Ruff 等测试工具。生产 CLI 用法见 [docs/CLI.md](docs/CLI.md)，接口与结果格式见 [docs/CONTRACTS.md](docs/CONTRACTS.md)。

```powershell
uv sync --dev
uv run ruff check .
npm ci
npm test
npm run verify-notes
npm run dry-run
git diff --check
```

`scan.yml` 的生产运行只安装锁定的运行时依赖，并缓存已固定 SHA-256 的 Mihomo 压缩包；缓存命中后仍重新校验摘要再解压。

真实订阅不会写入仓库，运行前通过环境变量注入，并走生产同一套脚本。

完整本地扫描验收需要先启动 `127.0.0.1:8000` 的 API，并通过 `BEST_IP_TEST_SUBSCRIPTION_URL` 注入真实订阅（不要把 token 写入仓库）。IPure 无需任何凭据。生产路径的闭环用 `uv run --no-dev python scripts/verify_real_scan.py`，它按同一个 `BEST_IP_API_BASE` 校验节点、评分与脱敏约束。

## 配置

| 变量 | 默认值 | 说明 |
|---|---:|---|
| `BEST_IP_MIHOMO_PATH` | `runtime/mihomo/mihomo(.exe)` | Mihomo 核心路径 |
| `BEST_IP_MAX_NODES` | `500` | 单订阅真实节点上限 |
| `BEST_IP_MAX_PARALLEL_JOBS` | `2` | runner 内同时运行的扫描任务数 |
| `BEST_IP_MAX_PARALLEL_NODES` | `min(16, max(8, CPU 核数))` | 单任务并发节点数；上限为订阅节点数 |
| `BEST_IP_MAX_NODE_ATTEMPTS` | `3` | 单节点最大尝试次数 |
| `BEST_IP_NODE_RETRY_BACKOFF_MS` | `500` | 重试基础退避毫秒数 |
| `BEST_IP_PAGE_TIMEOUT_MS` | `45000` | 单次节点采集预算；IPure 查询单独限制为最多 30 秒（直连兜底 5 秒）并与其他请求并发 |
| `BEST_IP_SUBSCRIPTION_MAX_BYTES` | `5242880` | 订阅最大字节数 |
| `BEST_IP_SUBSCRIPTION_TIMEOUT_SECONDS` | `30` | 订阅下载超时 |

## 已知限制

- 只支持顶部含 `proxies` 列表的 Mihomo YAML；不支持 URI 列表或只有远程 `proxy-providers` 的配置。
- 如果全部节点服务器都是域名且没有可用的字面 IP 引导节点，严格 DNS 隔离可能导致节点明确失败，不会回退宿主代理。
- Coffee 接口以及 IPure `/api/lookup` 的字段和限流属于外部服务，改版时必须同步 URL allowlist 与解析器；仓库不再有契约测试兜底，改版只能靠真实订阅扫描发现。
- IPure 对未收录 IP 的免验证查询按来源 IP 每天 5 次、全站每分钟 15 次限流；额度用完返回 `403 verification_required`，此时节点会标记为“部分”并记录官方 `reportUrl`，无法在本次扫描中取得分数。
- `source` 为 `store` 且 `stale` 为真时返回的是历史报告，分数反映当初检测时的状态。
- Cloudflare Worker 不执行扫描本身；扫描延迟仍包含 GitHub Actions 排队、runner 初始化、Mihomo 启动和节点网络耗时。
