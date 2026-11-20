# 分类接口 `/api/v1/classify`（给 MyClash 的交接文档）

MyClash 每天把合并后的订阅交给 best-ip 实测，best-ip 回传每个节点的落地标签：家宽或机房、是否原生、落地国家、AI 可用性、风险。节点名不改。

```
MyClash GitHub Action（每天）
  → POST /api/v1/classify          （Bearer 服务令牌）
  → GET  /api/v1/classify/{scan_id} 轮询，直到 status = done / failed
  → GET  /api/v1/classify/{scan_id}/result
  → 写 MyClash KV：res:<token>
失败时：GET /api/v1/classify/latest?label=… 取上一次 done 的结果兜底
```

- 生产地址：`https://best-ip.silverwolfx.workers.dev`
- 实现：`worker/service.js`（接口与结果存储）、`backend/app/classify.py`（结果格式、复用、本地 CLI）、`.github/workflows/scan.yml`（`channel=api`）
- 离线测试：`node scripts/check_service_api.mjs`、`uv run pytest tests/test_classify.py`

## 1. 鉴权

- 每个请求带 `Authorization: Bearer <BESTIP_SERVICE_TOKEN>`。不用密码登录 Cookie，也不做同源检查。
- best-ip 这边：令牌放在 Worker 的 secret `BESTIP_SERVICE_TOKEN`，至少 32 个字符。MyClash 那边：放进 Actions secret `BESTIP_SERVICE_TOKEN`。
- 没配令牌时返回 `503 service_not_configured`；令牌不对返回 `401 unauthorized`。
- 用 `GET /api/v1/health` 检查令牌是否可用，正常返回 `{"status":"ok","api":"v1"}`。

## 2. 接口

### 2.1 创建扫描 `POST /api/v1/classify`

```json
{
  "request_id": "myclash-abcd1234-20261120",
  "label": "myclash:abcd1234",
  "subscription_url": "https://<myclash>/sub/<token>",
  "profile": "classify",
  "reuse_hours": 20
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `request_id` | 是 | 幂等键，1..128 个字符，只能用 `A-Z a-z 0-9 . _ : -`。同一个 id 重复提交，返回同一个扫描 |
| `label` | 是 | 区分订阅组，1..64 个字符，字符集同上，供 `latest` 使用 |
| `subscription_url` | 二选一 | Clash YAML 订阅地址，必须是公网 http/https，不能带账号密码。runner 先直连抓取，被拒时改走 Cloudflare 中继 |
| `subscription_yaml` | 二选一 | 内联 YAML 文本，UTF-8 不超过 5 MB。整个请求体上限 16 MB |
| `profile` | 否 | `classify`（轻量档，见第 4 节）或 `full`（完整扫描），默认 `full` |
| `reuse_hours` | 否 | 0..168，默认 0。节点指纹不变、上次结果为 `ok` 且 `checked_at` 在 N 小时内的节点，直接复用上次结果，不重新测 |

返回：

- 新建扫描时返回 `202`：
  ```json
  { "scan_id": "clf-20261120-1a2b3c4d5e6f", "status": "queued",
    "status_url": "/api/v1/classify/clf-20261120-1a2b3c4d5e6f",
    "result_url": "/api/v1/classify/clf-20261120-1a2b3c4d5e6f/result" }
  ```
- `request_id` 已用过，或者同一令牌已有扫描在跑（queued / running）时，返回 `200`。响应体与 2.2 相同，另带 `status_url`、`result_url` 和 `"deduplicated": true`，`scan_id` 指向已有的那个扫描。这时不会新开扫描。
- 参数错误返回 `400 invalid_request` 或 `invalid_subscription_url`；超过大小限制返回 `413 request_too_large`；GitHub 派发失败返回 `502`，这个扫描会记为 `failed`，`error` 为 `dispatch_failed`。

### 2.2 查询状态 `GET /api/v1/classify/{scan_id}`

```json
{
  "scan_id": "clf-20261120-1a2b3c4d5e6f",
  "status": "running",
  "progress": { "done": 120, "total": 300 },
  "error": null,
  "request_id": "myclash-abcd1234-20261120",
  "label": "myclash:abcd1234",
  "profile": "classify",
  "created_at": "2026-11-20T19:17:00.000Z",
  "finished_at": null
}
```

- `status` 取值：`queued`（已派发，runner 还没领取订阅）、`running`、`done`、`failed`。
- `progress` 只统计这次实际要测的节点，不含复用和跳过的。runner 还没回报时，`total` 为 `null`。`done` 之后为 `{node_count, node_count}`。
- `failed` 时 `error` 是固定错误码：
  - runner 回报的：`input_invalid`（订阅抓不到或 YAML 无效）、`environment_not_ready`、`scan_unavailable`（一个可用节点都没有）、`result_invalid`、`scan_timeout`、`cleanup_failed`、`output_failed`、`scan_cancelled`、`scan_failed`；
  - Worker 发现的：`run_failure`、`run_cancelled` 等 `run_<GitHub conclusion>`（运行已结束但没交回结果）、`run_not_found`（派发 20 分钟后 GitHub 上仍找不到这次运行）、`timeout`（超过 3 小时）、`dispatch_failed`。
- 只能查本令牌创建的扫描，否则返回 `404`。

### 2.3 取结果 `GET /api/v1/classify/{scan_id}/result`

- 只有 `status = done` 才能取，返回 `application/json`。客户端带 `Accept-Encoding: gzip` 时，Cloudflare 会自动压缩。
- 还没完成或已失败时返回 `409`：`{"error":"not_ready","scan_id":"…","status":"running"}`。
- 结果保留 8 天。每个 label 最近一次 `done` 的结果会一直保留，直到被更新的结果替换。超过保留期返回 `410 result_expired`。

### 2.4 取某个 label 最近一次结果 `GET /api/v1/classify/latest?label=myclash:abcd1234`

返回本令牌下这个 label 最近一次 `done` 的结果，格式同 2.3。还没有结果时返回 `404`。失败的扫描不会覆盖它。

## 3. 结果格式（`schema_version: 1`）

```json
{
  "schema_version": 1,
  "scan_id": "clf-20261120-1a2b3c4d5e6f",
  "label": "myclash:abcd1234",
  "profile": "classify",
  "started_at": "2026-11-20T19:17:00Z",
  "finished_at": "2026-11-20T19:41:12Z",
  "input_sha256": "…",
  "node_count": 300,
  "score_source": "ipx.health.score",
  "counts": { "ok": 250, "failed": 40, "skipped": 10, "reused": 120 },
  "nodes": [ … ]
}
```

- `input_sha256`：best-ip 实际拿到的订阅字节（URL 抓下来的内容，或内联 YAML 的 UTF-8 字节）的 SHA-256。
- `nodes` 与输入的 `proxies` 一一对应，顺序和数量都相同，`nodes[i].index == i`。重名节点逐条返回，不合并。

### 3.1 节点身份

- `name`：直接取输入 YAML 解析出的字符串，不 trim、不去 emoji、不做 Unicode 规范化。输入项不是对象或没有 name 时为 `null`；name 是数字等标量时，按 JS `String()` 转成字符串。
- `index`：在输入 `proxies` 里的下标，从 0 开始。
- `fingerprint`：`sha256(type + "|" + server + "|" + port + "|" + (uuid ?? password ?? ""))` 的十六进制前 16 位，各字段取输入原值。字段缺失或为 null 时按空串处理，数字按十进制、布尔按 `true`/`false` 转字符串（与 JS `String()` 相同）。输入项不是对象时为 `null`。
  - 参考值：`{type:"ss", server:"8.8.8.8", port:8388, password:"pw"}` → `55303f6a971a8366`。

### 3.2 单个节点

```json
{
  "name": "🇭🇰 香港 01",
  "index": 0,
  "fingerprint": "3f9a1c0d2b7e4a51",
  "status": "ok",
  "reason": null,
  "checked_at": "2026-11-20T19:20:03Z",
  "reused": false,
  "exit_ip": "203.0.113.7",
  "country_code": "JP",
  "reg_country_code": "US",
  "usage_type": "residential",
  "usage_votes": { "ipx": "residential", "ipqs": "residential", "ipure": "business" },
  "native": true,
  "asn": 4713,
  "as_org": "NTT Communications",
  "score": 82,
  "risk": { "proxy": false, "vpn": false, "tor": false, "abuser": false },
  "ai": { "openai": "ok", "claude": "unknown", "gemini": "unknown" },
  "same_exit": ["🇯🇵 日本 03"]
}
```

每个节点都包含全部字段。拿不到的值填 `null`，`usage_votes` 为空对象 `{}`，`same_exit` 为空数组 `[]`，字段不会省略。

| 字段 | 取值与来源 |
|---|---|
| `status` | `ok`：测到了落地 IP（各数据源缺几个也算 ok，缺的字段为 null）。`failed`：测不到落地 IP。`skipped`：没测 |
| `reason` | `failed` 时：`timeout`、`dns_failed`、`connection_refused`、`connection_reset`、`tls_failed`、`auth_failed`、`network_unreachable`、`unsupported_type`（Mihomo 不支持这个配置）、`proxy_error`、`connect_failed`、`collect_failed`、`no_exit_ip`。`skipped` 时：`subscription_metadata`（「剩余流量」「套餐到期」这类信息项）、`unsupported_type`（direct / reject / dns 等非出站类型）、`invalid_node`（不是对象、缺 name 或 type）、`too_many_nodes`（超过 1000 个要测的节点）。`ok` 时为 `null` |
| `checked_at` | 这个节点实测完成的时间（UTC）。复用的节点保留原来的实测时间，所以复用不会无限续期 |
| `reused` | 是否来自 `reuse_hours` 复用 |
| `exit_ip` | 落地 IP。由 Coffee trace 确定，trace 不通时依次退回 ipx / 1ms who 的经代理回显 |
| `country_code` | 落地地理国家，ISO 3166-1 alpha-2 大写。来源顺序：ipx → 1ms who → 1ms IPQS 共识 → IPure |
| `reg_country_code` | IP 注册国家。来源顺序：ipx `native.regCountry` → 1ms IPQS `regCc` → IPure registry |
| `usage_type` | `residential` / `mobile` / `business` / `hosting` / `education` / `government` / `null`。来源顺序：ipx → 1ms IPQS → IPure 中第一个给出值的。`mobile` 不并入 `residential` |
| `usage_votes` | 各数据源各自的判定，拿不到的源不出现。键为 `ipx`、`ipqs`、`ipure` |
| `native` | `true` 原生、`false` 广播、`null`。来源顺序：ipx → 1ms IPQS → IPure → Coffee |
| `asn`、`as_org` | 整数、字符串。来源顺序：ipx → who → 1ms IPQS → IPure |
| `score` | ipx.al 健康分 `health.score`，0..100，越高越干净。顶层 `score_source` 写明来源 |
| `risk` | `proxy` / `vpn` / `tor` / `abuser`：1ms IPQS 多源标记过半数为 `true`，有来源报告但不过半为 `false`；IPQS 没有数据时退回 Coffee lookup 的同名标记；都没有时为 `null` |
| `ai.openai` | `ok`：chatgpt.com 和 api.openai.com 经节点都连通，且落地国家不在受限名单（CN HK MO RU IR KP CU SY）。`blocked`：落地在受限地区，或上游明确返回拒绝状态码（如 403）。`unknown`：只是连接失败、无法判断 |
| `ai.claude`、`ai.gemini` | 目前没有实测，固定为 `unknown`。按约定，AI 组只放 `ok` 的节点 |
| `same_exit` | 本次结果里与本节点落地 IP 相同的其他 `ok` 节点名（包括复用的节点） |

约定：

- 枚举值只会出现上表列出的值。
- 结果里不包含任何节点配置字段（uuid、password、私钥、cipher 等）。测试会校验结果里不出现这些值。
- 以后可能新增字段；改名或改含义时会升级 `schema_version`。

## 4. `classify` 轻量档

- 会做：落地 IP 探测、网络身份（ipx → 1ms IPQS → IPure → Coffee lookup）、原生判定、风险标记、GPT/OpenAI 可达性。
- 不做：全球 Ping、端口扫描、连通检测、双栈第二出口。`related_domains` 本来就只取自 lookup，不另外请求。
- 多个节点落地 IP 相同时，lookup、IPure、1ms IPQS 只查一次，结果共用（按出口 IP 缓存）。
- 某个数据源返回 429 时，该源记为缺失，相应字段为 null，节点不会因此失败。IPure 遇到 429 时最多尝试 3 次（按 1、2 秒退避）。
- 每个节点最多尝试 3 次（第一次失败后，余下两次并行），并发 16。实测数据：本地 44 个节点约 58 秒跑完（28 ok / 13 failed / 3 skipped）。GitHub runner 上还要加约 1 分钟的准备时间。300 个节点按并发推算约 5 分钟；api 通道单次最多测 1000 个节点。

`profile: "full"` 会跑完整扫描（包括上面跳过的检测），结果格式相同。

## 5. 安全

- `subscription_url` / `subscription_yaml` 只暂存在这次扫描的 Durable Object 里，15 分钟内由 runner 凭 GitHub Actions OIDC 领取一次，领取后立即删除。它们不会进入 workflow 输入、日志、结果或注册表。
- 结果注册表只保存调用方标识（令牌 SHA-256 的前 16 位）、`request_id`、`label` 和结果本身，不保存令牌原文。
- runner 用领取订阅时拿到的一次性 runner 令牌回传结果（`POST /api/scan-result`）。结果里的 `scan_id` 和 `label` 以 Worker 登记的为准。

## 6. 本地 CLI（接口不可用时的备选）

输出格式与第 3 节完全相同，需要先安装 uv：

```bash
uv sync --no-dev
uv run python scripts/download_mihomo.py        # 首次运行：下载并校验 Mihomo
uv run python -m backend.app.classify --input sub.yaml --output result.json \
  [--profile classify] [--reuse result-prev.json] [--reuse-hours 20] [--label myclash:abcd1234]
```

- 模块路径是 `backend.app.classify`，不是接口文档里写的 `app.classify`；要从仓库根目录运行。
- `--reuse` 默认复用 20 小时内的结果，可以用 `--reuse-hours` 修改。
- IPure 的请求头从 `config/ipure.yml` 读取（示例见 `config/ipure.example.yml`）。没有这个文件时使用默认请求头，IPure 可能拿不到数据，对应的投票和回落值为 null，其余功能不受影响。

## 7. MyClash Action 参考写法

```bash
set -euo pipefail
BASE=https://best-ip.silverwolfx.workers.dev
AUTH="Authorization: Bearer $BESTIP_SERVICE_TOKEN"
DAY=$(date -u +%Y%m%d)
body=$(jq -n --arg id "myclash-${TOKEN:0:8}-$DAY" --arg label "myclash:${TOKEN:0:8}" \
  --arg url "https://<myclash>/sub/$TOKEN" \
  '{request_id:$id,label:$label,subscription_url:$url,profile:"classify",reuse_hours:20}')
scan=$(curl -fsS -X POST "$BASE/api/v1/classify" -H "$AUTH" -H 'Content-Type: application/json' -d "$body" | jq -r .scan_id)
for _ in $(seq 1 90); do                       # 最多等 45 分钟
  status=$(curl -fsS "$BASE/api/v1/classify/$scan" -H "$AUTH" | jq -r .status)
  [[ $status == done || $status == failed ]] && break
  sleep 30
done
if [[ $status == done ]]; then
  curl -fsS --compressed "$BASE/api/v1/classify/$scan/result" -H "$AUTH" -o result.json
else
  curl -fsS --compressed "$BASE/api/v1/classify/latest?label=myclash:${TOKEN:0:8}" -H "$AUTH" -o result.json
fi
```

## 8. 已知限制

- `ai.claude`、`ai.gemini` 目前固定为 `unknown`，还没有对应的探测。
- best-ip 的扫描 workflow 共用一个并发组：网页扫描和 api 扫描排队执行，不会同时跑。
- 正在运行的扫描不能通过接口取消。超过 3 小时会被判为 `timeout`，之后可以重新提交。
- 输入里 `dialer-proxy` 指向的节点即使在复用期内也会重新测，因为扫描时需要用到它。
