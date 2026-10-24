# Agent Note: CI 去重、删除旧前端、扫描日志加一行聚合耗时

Status: implemented

## Problem

所有者要求清理冗余、加快流程，并找出耗时瓶颈。实测（2026-10 的线上运行）：

| 流程 | 实测 | 构成 |
| --- | --- | --- |
| `scan.yml`（25 节点上下） | 33–87 秒 | 排队 + Set up job 约 5 秒，准备段约 4 秒（缓存命中），**Run production scan 24–70 秒**，上传产物约 2 秒 |
| `ci.yml` | 约 20 秒 | `npm ci` 4 秒，setup-uv 收尾存缓存 3 秒，其余各 1 秒 |

- **扫描的瓶颈在节点检测**，不在 workflow。按[墙钟预算笔记](../architecture/2026-09-21-scan-wall-clock-budget.md)，`collect` 相占节点墙钟 82–96%，长尾来自永久失败节点（约 11 秒）和慢 IPure。采集器内部已经并行，准备段也已经压到个位数秒。问题是 Actions 日志里只有 `subscription_source` 和 `progress_reports` 两行，看不到这次扫描的时间花在哪。产物保留 1 天，过期后 `phase_ms` 也就没了，事后没法分析。
- **CI 每次 PR push 跑两遍**：`on: push` 不限分支，加上 `pull_request`，同一个 commit 跑两次（PR #19 上能看到两个 `test` check）。
- **旧前端 `frontend/`**（21 个文件、224K）已经不再部署，`scripts/dev.py` 也不用它。但 `frontend-next` 里有三处「逐字副本，两边必须同时改」的人工同步约定（`artifact/reader.js`、`scan-crypto.js`、`score-color.js`）。注释自己写着「等旧前端下线、`frontend/` 删除，这里的副本才重新变成唯一实现」。

## Decision

- `ci.yml`：`push` 只看 `main`；加上按 ref 分组的 `concurrency` + `cancel-in-progress`。lint 从「装 Python → 全量 `uv sync --dev` → ruff」合成一步 `uv run --frozen --only-group dev ruff check .`，ruff 版本仍由 `uv.lock` 锁定。
- 删除 `frontend/`。`check_js.mjs` 不再扫这个目录；README 里 `scan-public.pem` 的路径改成 `frontend-next/`（两份 PEM 逐字节相同）；`wrangler.jsonc` 和 `dev.py` 里关于「回退到 `./frontend`」的注释删掉（要回退可以 `git revert`）；三处副本的注释改成「唯一实现」。
- `scripts/run_scan.py`：任务结束后往 stderr 打一行 `scan_timing:`。内容包括订阅下载时长、扫描墙钟、节点数、峰值并发、节点用时的 p50/p90/max、重试和失败的节点数，以及各相合计。只有数字，没有节点名、出口 IP 或订阅内容，与现有日志的脱敏口径一致。

## Alternatives considered

### Why not 继续压 `scan.yml` 的准备段？

最强理由：它是唯一完全由仓库控制的那部分时间。不选的原因：缓存命中时它约 4 秒，占总时长不到 10%；再压只剩合并步骤这类亚秒级的收益，代价是让已经调过的缓存与清理逻辑重新承担风险。

### Why not 直接改节点侧参数（拨号超时、重试次数）？

最强理由：长尾就在那里。不选的原因：现在没有这次扫描的分相数据，改了也证明不了收益。尝试次数还关系到救回节点的能力（见 DNS 引导笔记）。先把 `scan_timing` 打出来，攒几次真实运行，再决定动哪一相。

### Why not 保留 `frontend/` 作为回退？

最强理由：改一行 `assets.directory` 就能回退。不选的原因：它已经过时（不含后来的 IPure 字段与扫描进度功能），回退过去也是坏的；而它的存在逼着三份代码维持「人工同步」的约定。git 历史就是回退通道。

## Consequences

- 收益：每个 PR push 少跑一次 CI；lint 步骤不再安装运行时依赖。
- 收益：每次扫描的 Actions 日志里都有一行 `scan_timing`，产物过期后也能看出瓶颈在哪一相，下一轮调参有据可依。
- 收益：前端只剩一份实现，不再有「逐字副本」要人工同步。
- 代价：`main` 以外的分支不开 PR 就不跑 CI。
- 未做：节点侧参数一律没动，等 `scan_timing` 积累了数据再定。

## Testing

- `_timing_summary` 用替身 job/节点跑过：25 个节点、3 个失败、4 个重试，输出与手算一致；没有节点数据时只输出订阅时长。
- `uv run --frozen --only-group dev ruff check .`（干净 venv）、`npm run check`、`npm run dry-run` 都通过。
