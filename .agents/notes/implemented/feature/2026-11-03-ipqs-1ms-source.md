# Agent Note: 接入 1ms IPQS 多源评分，接管「类型 / 原生性」两列

Status: implemented

## Problem

表格里「接入（住宅 / 机房）」与「原生性」两列读的是 Coffee 单一来源的 `is_residential` / `is_native`。单源判断容易误判：同一个出口在不同库里经常一边说住宅、一边说机房，表格却只给出一个结论，也说不出依据。用户要求换成 https://ipqs.1ms.dog 的判定，并新增一列 1ms 评分。

## Decision

- 新增数据源 `backend/app/sources/ipqs.py`：请求 `GET https://ipqs.1ms.dog/api/check?ip=<出口 IP>`。响应是 NDJSON 流，只读最后的 `done` 事件。`done.score` 是 0..100 的整数，也是唯一硬要求；缺了就记为 `ResponseParseError`。
- 记录顶层新增五个可选字段：`ipqs_score`、`ipqs_grade`（A/B/C/D/F）、`ipqs_type`（residential/mobile/hosting/business/education/government）、`ipqs_native`（三态布尔）、`ipqs_report_url`。上游给出闭合集合以外的值时一律落成 `null`，原值只留在 `requests.ipqs.data.type_raw`。这样上游哪天加了新等级或新类型，扫描也不会因为校验失败整批作废。
- 扣分明细（`deductions`）、注册地 / 归属地、匿名标记、DNSBL 结果都只留在 `requests.ipqs.data`，前端从那里读，用来做 tooltip。
- 扫描用的 Mihomo 配置是域名白名单（`backend/app/mihomo.py` 的 `rules`，未列出的域名一律 `MATCH,REJECT`），`ipqs.1ms.dog` 必须在白名单里。白名单里的主机常量直接 import 自 `sources/http.py`，与请求代码共用同一份。漏掉这一条时，所有 1ms 请求都会被 Mihomo 拒绝，然后全部「成功」地走直连兜底，表面上看不出问题（首轮真实订阅验收就是 23/23 全部直连）。所以兜底成功时也会在 `requests.ipqs.proxy_error` 记下代理路径的失败原因，`verify_real_scan.py` 会打印经代理与走直连的节点数。
- 出网路径与 IPure 同构：先走节点代理，最多两次尝试，429 会尊重 `Retry-After`；遇到瞬时连接错误再改走宿主直连，并记录 `direct_fallback` / `via_mihomo` / `proxy_evidence.ipqs_via_direct_fallback` 三项。两侧校验器都交叉核对这三项。
- 缺 1ms 报告时节点状态为 `partial`（`completeness.checks.ipqs_recorded`）。限流类错误也会写进 `record.error`（`_combined_error` 对 IPQS 与 IPure 一视同仁）。
- 五个字段**不进** manifest summary：summary 改了键，历史任务的 manifest 摘要比对会全部失败。
- `scripts/verify_real_scan.py` 要求每个有出口 IP 的节点都带 1ms 报告，且拍平字段与 `requests.ipqs.data` 逐项一致。

## 分数可信度

1ms 网页另有两个只在浏览器里跑的源（ipinfo.io、ipwho.is）。核对过页面 bundle：这两个源只贡献国家、城市、ASN、主机名、anycast，不贡献类型、匿名标记和风险值，而评分只由这三类输入计算。因此 `done.score` 与网页显示的分数相同。原生性按「多数源国家 == 注册地」判定，这两个源在极端情况下可能改变多数国家；这是唯一可能与网页出现分歧的地方。

## Alternatives considered

### Why not 在后端复刻 1ms 的聚合算法、自己拉十几个源？

最强理由：不依赖第三方站点的可用性，也能把 ipinfo / ipwho 补齐，与网页完全一致。没有这样做，是因为算法与源列表都在对方仓库里持续演进，复刻等于长期追一个别人的私有实现，分数还会悄悄漂移；而核对已证明，缺这两个源不影响评分。

### Why not 只把 `ipqs_type` 塞进旧的 `is_residential` 字段？

最强理由：前端不用改字段名，旧导出也能直接显示。没有这样做，是因为它会把两个来源的判断混成一个字段，事后说不清某个值是谁给的，也违背「每个数字都能追到出处」的契约。

## Consequences

- 收益：类型 / 原生性改由十余个源投票得出，评分附带可读的扣分理由和报告链接，结论可以复核。
- 代价：每个节点多一个约 3–5 秒的外部请求（与 IPure 并发，墙钟基本不增）；1ms 限流或不可用时节点会降为 `partial`。
- Coffee 的 `is_residential` / `is_native` 仍然落盘，只是前端不再展示。
- 前端如何展示见 [两级分组表头的表格重构](2026-11-03-frontend-grouped-table-redesign.md)。
