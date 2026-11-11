"""数据源注册表：每个经节点访问的上游在这里登记一次，其余各处从这张表派生。

派生出去的地方：Mihomo 域名白名单（`mihomo.py`）、completeness 检查项与「部分」判定
（`collector.py`）、实时进度里「缺了什么」的文案（`scan/errors.py`）、`scan_timing`
的逐源统计（`scripts/run_scan.py`）、同一任务内按出口 IP 复用（`collector.py`）。
原来这些各抄一份，接一个新源要改十几处；漏加白名单时请求会悄悄全走宿主直连，表面看不出来。

请求本身怎么发、怎么解析仍在各自的模块里——那部分每家都不一样，硬塞进一张表只会更难读。
"""

from __future__ import annotations

from dataclasses import dataclass

from .http import COFFEE_HOST, IPQS_HOST, IPURE_HOST, IPX_HOST, WHO_HOST


@dataclass(frozen=True, slots=True)
class Source:
    name: str
    """`record.requests` 里的键。"""
    host: str
    """经节点代理访问的主机，进 Mihomo 白名单。"""
    label: str
    """「部分」节点缺了它时，实时进度里怎么说。"""
    check: str
    """`completeness.checks` 里的键。"""
    by_ip: bool = False
    """按出口 IP 查询（在出口 IP 定下之后才发）。"""
    shareable: bool = False
    """答案只取决于出口 IP、与从哪个节点问无关：同一任务内同一出口 IP 只问一次。"""
    score_column: bool = False
    """页面上有一列分数：有出口 IP 而缺了它，节点算「部分」。"""
    enrichment: bool = False
    """第三方富化源：它被跳过（skipped）时的原因仍并入记录的 error。"""


SOURCES: tuple[Source, ...] = (
    Source("trace", COFFEE_HOST, "出口 IP 探测", "trace_received"),
    Source("who", WHO_HOST, "1ms who", "who_recorded", enrichment=True),
    Source(
        "ipx", IPX_HOST, "ipx 健康分", "ipx_recorded",
        by_ip=True, score_column=True, enrichment=True,
    ),
    Source(
        "lookup", COFFEE_HOST, "Coffee 评分", "lookup_received",
        by_ip=True, shareable=True, score_column=True,
    ),
    Source(
        "ipqs", IPQS_HOST, "1ms 评分", "ipqs_recorded",
        by_ip=True, shareable=True, score_column=True, enrichment=True,
    ),
    Source(
        "ipure", IPURE_HOST, "IPure 评分", "ipure_recorded",
        by_ip=True, shareable=True, score_column=True, enrichment=True,
    ),
    Source(
        "global_ping", COFFEE_HOST, "全球 Ping", "global_ping_recorded",
        by_ip=True, shareable=True,
    ),
    Source(
        "port_scan", COFFEE_HOST, "端口扫描", "port_scan_recorded",
        by_ip=True, shareable=True,
    ),
    Source(
        "ping_check", COFFEE_HOST, "连通检测", "ping_check_recorded",
        by_ip=True, shareable=True,
    ),
)

SOURCE_BY_NAME = {source.name: source for source in SOURCES}

# Mihomo 白名单：出现顺序去重。
PROXIED_HOSTS: tuple[str, ...] = tuple(dict.fromkeys(source.host for source in SOURCES))

SCORE_SOURCES: tuple[Source, ...] = tuple(source for source in SOURCES if source.score_column)

ENRICHMENT_HOSTS = frozenset(source.host for source in SOURCES if source.enrichment)

# completeness.checks 的键 → 实时进度里的文案。
CHECK_LABELS = {source.check: source.label for source in SOURCES}
