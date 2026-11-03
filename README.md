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

结果默认采用 Coffee 风格的双列节点卡片，小屏幕自动切换为单列；可切换到表格进行逐列筛选和排序。两种视图使用相同结果，点击节点名称查看完整详情，导入、导出与完整性校验保持不变。

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
- 浏览器使用 AES-256-GCM 加密订阅 URL，再用 `frontend-next/scan-public.pem` 对应的 RSA-OAEP-3072 公钥包裹 AES key；明文 URL 不进入 Worker API、GitHub workflow input 或 artifact。
- Worker 固定调度 `silverwolf-x/best-ip` 的 `main` 分支和 `scan.yml`，不提供通用 GitHub API 代理。
- 每个节点尝试使用独立 Mihomo 进程、端口、连接池和临时目录；默认最多 `min(16, max(8, CPU 核数))` 个节点并发（上限为订阅节点数），每节点最多 3 次尝试。
- Coffee 页面与 trace 并行；确认出口 IP 后，lookup、global ping、portscan、pingcheck、related、IPure 官方 `/api/lookup` 评分及 ChatGPT/Codex 探测按既定依赖并发执行。IPure 没有返回纯净度总分时，节点会如实标记为“部分”。
- 1ms IPQS（https://ipqs.1ms.dog）提供表格里的「类型 / 原生」两列与「1ms 评分」：`GET /api/check?ip={ip}` 的 NDJSON 流只读最终 `done` 事件，拍平成 `ipqs_score` / `ipqs_grade` / `ipqs_type` / `ipqs_native` / `ipqs_report_url`，扣分明细留在 `requests.ipqs.data`；字段契约见 [docs/CONTRACTS.md](docs/CONTRACTS.md)。
- IPure 接入完全按官方 `/docs/api` 契约：`GET /api/lookup?ip={ip}` 无需 API key 或 Cookie。响应解析出 `risk.purity` 纯净度总分、`risk.level` / `risk.label` / `risk.verdict` 档位，以及 `scenarios[]` 的六项场景（`ai`、`social`、`streaming`、`gaming`、`ecommerce`、`email`）评分与档位，另附 `reportUrl`、`source`（fresh / cache / store）与 `stale`。上游档位只用于判断是否写哨兵，**不进入记录**（`ipure_level` / `ipure_verdict` / `ipure_scenario_levels` 已从落盘契约删除，读取时容忍旧 artifact）；前端不为 IPure 分数渲染任何档位或状态文案：六项评分一律显示成带颜色的数字，档位为 `restricted`（地区受限）的项按后端写入的 `-1` 哨兵显示为中性灰的 `-1`，既不参与数值排序也不被分数筛选命中。表格与详情卡里剩下的「受限 / 不可用」文字属于 GPT · Codex 可达性探针（chatgpt.com / api.openai.com 的连通状态），与 IPure 分数无关。
- IPure 每次实际 HTTP 请求都会重新读取 `config/ipure.yml` 的 `headers`，修改后无需重启；该文件只用于覆盖请求头（如 User-Agent），已排除出 Git，示例见 `config/ipure.example.yml`。未配置时使用 JSON / `MyIPChecker/1.0` 默认值。`429`（含 `code=open_rate_limited`）最多尝试 3 次，按 1、2 秒退避（上限 2 秒）并遵守秒数形式的 `Retry-After`，等待不会超出查询时间预算，也不切换出口重试；`403`（`code=verification_required`）不盲目重试，而是把官方给出的 `reportUrl` 一并记录。响应头 `x-open-budget-remaining` 会写入 `proxy_evidence.ipure_budget_remaining`。
- mixed-port 与 controller 只监听 `127.0.0.1`；所有请求固定 `trust_env=False` 并禁用重定向。IPure 不读取也不保存任何凭据，因此不存在凭据回退或验证会话；宿主侧直连查询是记录在案的例外，必须与节点出口证据一起落盘（见 `docs/CONTRACTS.md`）。
- 每个真实节点恰好产生一条 `success`、`partial` 或 `failed` 记录。失败记录的 `exit_ip` 必须为 JSON `null`。
- 节点文件原子写入；只有节点集合、字段、状态、出口 IP、代理证据、文件大小和 SHA-256 全部通过后才生成 manifest。
- Actions 运行期间前端只显示 Job/Step，不伪造节点 `0/0` 进度；节点统计以终态 artifact 为唯一事实源。

## 部署

发布是流水线的一部分，也只能是流水线的一部分：本地 `npm run deploy` 现在会被 `scripts/deploy_guard.mjs` 直接拒绝（退出码 2）。发布只有两条远程路径：

1. **自动**：push 到 `main` → CI（Ruff、`npm run check` 语法检查、`wrangler deploy --dry-run`）成功后，`Deploy Cloudflare Worker` 工作流通过 `workflow_run` 自动发布，且发布的就是 CI 验过的那一个 commit。
2. **手动重发一次**：`gh workflow run worker.yml --ref main`，或在 GitHub 页面上派发该工作流（`workflow_dispatch`）。

为什么本地不许发布：本机检出可能是 CRLF（`core.autocrlf=true`，而仓库 blob 是 LF），也可能带着未提交的改动，`wrangler deploy` 会把这些字节原样发到线上，而它只报「上传成功」，不会说发上去的是不是仓库里的内容——历史上线上就这样跑过与任何 commit 都不一致的资产（`app/api.js` 线上 12327B vs blob 12089B，多出的 238 字节全是 CR）。所以守卫在发布前还要断言三件事：分支是 `main`（`--ref` 派发到别的分支不能上线）、`HEAD` 等于本次运行声明的 commit、以及 assets 目录下每个文件都被 git 跟踪且工作区字节逐字节等于该 commit 的 blob。这三条合起来，才把「发布内容等于某个 commit」从约定变成机械判据——发布后同一工作流还会对线上再做一遍逐字节比对。

本地能做的只有只读检查：

```powershell
npm ci
npm run check         # 逐文件 node --check
npm run dry-run       # wrangler deploy --dry-run，只看打包
npm run verify-notes  # 笔记门禁
$env:SITE_PASSWORD="…"; npm run verify:deploy   # 核对线上内容与当前 commit 是否逐字节一致
```

发布之后同一个工作流会跑 `npm run verify:deploy` 做部署后自证，分两层：无凭据检查（登录页可达、CSP 仍放行签名 blob、静态资源仍被会话门挡住），以及有凭据的逐字节比对（用 `SITE_PASSWORD` 登录后，把 `frontend-next/` 下每个被 git 跟踪的文件与它在该 commit 里的 blob 比对）。`wrangler deploy` 只报「上传成功」，它不说线上提供的是不是这一个 commit 的内容；这一步才是那个静默漂移的门。新资产在边缘生效有个窗口（可能先返回 404，也可能先返回旧字节），这一步在宽限预算内会重取，不会把传播窗口判成漂移。

`.github/workflows/worker.yml` 需要以下 GitHub Actions secrets：

- `CLOUDFLARE_API_TOKEN`
- `CLOUDFLARE_ACCOUNT_ID`
- `SITE_PASSWORD`（部署后自证用；缺失时该步骤直接失败，不会降级成「绿灯但没验」）

Worker 没有第三方运行时 npm 依赖；密码会话和 GitHub App RS256 签名都使用原生 Web Crypto。`package.json` 与锁文件保留模块声明、语法检查命令和 Wrangler 部署工具。

Worker 使用 Static Assets 托管 `frontend-next/`；当前配置不使用 Cloudflare Pages 或 GitHub Pages。

## 平台配置

1. 创建 GitHub App，只授予当前仓库 `Actions: Read and write` 与 `Metadata: Read`，并只安装到 `silverwolf-x/best-ip`。
2. 在 Worker 的 Settings → Variables and Secrets 添加 Secret `SITE_PASSWORD`：使用密码管理器生成至少 16 个字符的随机密码（最多 1024 字符）。无需 Cloudflare Access；如果之前启用了 Worker 域名的 Access 保护，请关闭该保护，并移除覆盖该域名的 Zero Trust Access Application。原来的三个 `ACCESS_*` 变量可以删除。
3. 配置 Worker secrets/variables：

   - `SCAN_KEY_ID`：`frontend-next/scan-public.pem` 的 SPKI DER SHA-256 指纹。
   - `GITHUB_APP_ID`
   - `GITHUB_APP_INSTALLATION_ID`
   - `GITHUB_APP_PRIVATE_KEY`
   - `SCAN_TOKEN_SECRET`
   - `SITE_PASSWORD`

4. 配置 Actions secrets `SCAN_PRIVATE_KEY_PEM`、`IPURE_CONFIG_YAML`，并配置 Actions variable `SCAN_KEY_ID`。`IPURE_CONFIG_YAML` 可选，保存与本地 `config/ipure.yml` 相同的请求头覆盖内容；Actions 扫描前生成该文件，结束时清理，不上传到结果 artifact。IPure 不需要凭据，因此该 Secret 为空也不影响评分。Worker 仅调度扫描，不查询 IPure，也不向前端提供任何第三方请求头。可用 `gh secret set IPURE_CONFIG_YAML --repo silverwolf-x/best-ip < config/ipure.yml` 同步本地配置到云端；修改本地文件不会自动更新 GitHub Secret。

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
