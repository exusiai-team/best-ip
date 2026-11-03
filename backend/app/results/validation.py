from __future__ import annotations

import hashlib
import ipaddress
import json
from typing import Any
from urllib.parse import urlsplit

from ..sources.ipure import IPURE_RESTRICTED_SCORE, IPURE_SCENARIOS, IPURE_USAGE_TYPES

_SUMMARY_KEYS = (
    "node_index",
    "node",
    "type",
    "selected_proxy",
    "status",
    "error",
    "transport_error",
    "attempt_count",
    "retry_count",
    "attempt_errors",
    "exit_ip",
    "cidr",
    "rdns",
    "ai_verdict",
    "location",
    "isp",
    "score",
    "ipure_scores",
    "ipure_report_url",
    "is_residential",
    "is_datacenter",
    "is_native",
    "native_status",
    "native_detail",
    "is_bogon",
    "bogon_status",
    "bogon_reason",
    "rpki_status",
    "asn_kind",
    "asn_kind_display",
    "abuse_level",
    "honeypot_status",
    "traffic_profile",
    "company_type",
    "is_vpn",
    "is_proxy",
    "is_tor",
    "is_crawler",
    "is_abuser",
    "security_status",
    "threat_tags",
    "asn",
    "as_org",
    "global_ping",
    "port_scan",
    "ping_check",
    "gpt_check",
    "related_domains",
    "elapsed_ms",
    "started_at",
    "finished_at",
    "completeness",
    "proxy_evidence",
)


_NODE_FIELDS = {
    "schema_version",
    "job_id",
    "node_index",
    "node",
    "type",
    "selected_proxy",
    "requested_proxy",
    "coffee_page_url",
    "status",
    "error",
    "phase",
    "transport_error",
    "attempt_count",
    "retry_count",
    "attempt_errors",
    "started_at",
    "finished_at",
    "exit_ip",
    "cidr",
    "rdns",
    "ai_verdict",
    "location",
    "country",
    "country_code",
    "region",
    "city",
    "usage_type",
    "network_source",
    "isp",
    "score",
    "coffee_score",
    "ipure_scores",
    "ipure_report_url",
    "is_residential",
    "is_datacenter",
    "is_native",
    "native_status",
    "native_detail",
    "is_bogon",
    "bogon_status",
    "bogon_reason",
    "rpki_status",
    "asn_kind",
    "asn_kind_display",
    "abuse_level",
    "honeypot_status",
    "traffic_profile",
    "company_type",
    "is_vpn",
    "is_proxy",
    "is_tor",
    "is_crawler",
    "is_abuser",
    "security_status",
    "threat_tags",
    "asn",
    "as_org",
    "global_ping",
    "port_scan",
    "ping_check",
    "gpt_check",
    "related_domains",
    "elapsed_ms",
    "proxy_evidence",
    "completeness",
    "requests",
    "coffee",
    "pages",
}

# 旧版本写过这三个档位字段；落盘契约已收窄，读取时忽略，历史结果仍要能打开。
_RETIRED_NODE_FIELDS = frozenset(
    {"ipure_level", "ipure_verdict", "ipure_scenario_levels"}
)
_NODE_REQUIRED_FIELDS = {
    "schema_version",
    "job_id",
    "node_index",
    "node",
    "type",
    "selected_proxy",
    "status",
    "error",
    "transport_error",
    "started_at",
    "finished_at",
    "exit_ip",
    "elapsed_ms",
    "proxy_evidence",
    "completeness",
    "requests",
    "coffee",
}


class ResultStoreError(RuntimeError):
    pass


def validate_node(job_id: str, index: int, record: dict[str, Any]) -> None:
    if not isinstance(record, dict):
        raise ResultStoreError("节点记录结构无效")
    if index < 0:
        raise ResultStoreError("节点索引不能为负数")
    unknown_fields = set(record) - _NODE_FIELDS - _RETIRED_NODE_FIELDS
    missing_fields = _NODE_REQUIRED_FIELDS - set(record)
    if unknown_fields or missing_fields:
        raise ResultStoreError("节点记录字段集合无效")
    if (
        record.get("schema_version") != 1
        or record.get("job_id") != job_id
        or record.get("node_index") != index
        or not isinstance(record.get("node"), str)
        or not record.get("node")
        or not isinstance(record.get("type"), str)
        or not record.get("type")
        or not isinstance(record.get("elapsed_ms"), int)
        or record.get("elapsed_ms") < 0
    ):
        raise ResultStoreError("节点记录结构或身份无效")
    for field in ("proxy_evidence", "completeness", "requests", "coffee"):
        if not isinstance(record.get(field), dict):
            raise ResultStoreError(f"节点记录的 {field} 无效")
    validate_network_identity(record)

    status = record.get("status")
    attempt_count = record.get("attempt_count")
    retry_count = record.get("retry_count")
    attempt_errors = record.get("attempt_errors")
    if any(value is not None for value in (attempt_count, retry_count, attempt_errors)):
        evidence = record["proxy_evidence"]
        expected_error_count = attempt_count if status == "failed" else retry_count
        if (
            not isinstance(attempt_count, int)
            or isinstance(attempt_count, bool)
            or attempt_count < 1
            or not isinstance(retry_count, int)
            or isinstance(retry_count, bool)
            or retry_count != attempt_count - 1
            or not isinstance(attempt_errors, list)
            or len(attempt_errors) != expected_error_count
            or not all(isinstance(error, str) and bool(error.strip()) for error in attempt_errors)
            or evidence.get("fresh_mihomo_per_attempt") is not True
            or not isinstance(evidence.get("max_attempts"), int)
            or evidence.get("max_attempts") < attempt_count
        ):
            raise ResultStoreError("节点重试证据无效")
    if status in {"success", "partial"}:
        evidence = record["proxy_evidence"]
        proxy_url = evidence.get("proxy_url")
        try:
            parsed_proxy = urlsplit(proxy_url)
            proxy_port = parsed_proxy.port
        except (TypeError, ValueError):
            parsed_proxy = None
            proxy_port = None
        checks = record["completeness"].get("checks")
        if (
            record.get("selected_proxy") != record.get("node")
            or evidence.get("selection_confirmed") is not True
            or evidence.get("selected_proxy") != record.get("node")
            or evidence.get("transport") != "workspace_mihomo_mixed_port"
            or evidence.get("target_origin") != "https://ip.net.coffee"
            or not isinstance(evidence.get("mihomo_instance"), str)
            or not evidence.get("mihomo_instance")
            or parsed_proxy is None
            or parsed_proxy.scheme != "http"
            or parsed_proxy.hostname != "127.0.0.1"
            or proxy_port is None
            or parsed_proxy.path not in {"", "/"}
            or parsed_proxy.query
            or parsed_proxy.fragment
            or parsed_proxy.username
            or parsed_proxy.password
            or evidence.get("trust_env") is not False
            or not isinstance(checks, dict)
            or not all(
                checks.get(key) is True
                for key in (
                    "page_received",
                    "trace_received",
                    "exit_ip_valid",
                    "lookup_received",
                    "lookup_matches_trace",
                )
            )
        ):
            raise ResultStoreError("成功节点记录的采集或代理证据无效")
        validate_ipure_scores(record)
        validate_ipure_evidence(record["proxy_evidence"], record["requests"])
        validate_exit_ip(record.get("exit_ip"))
    elif status == "failed":
        if record.get("exit_ip") is not None:
            raise ResultStoreError("失败节点不能保存伪造的出口 IP")
        if not str(record.get("error") or "").strip():
            raise ResultStoreError("失败节点缺少错误原因")
    else:
        raise ResultStoreError("节点记录状态无效")


def summary(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record.get(key) for key in _SUMMARY_KEYS}


def validate_ipure_scores(record: dict[str, Any]) -> None:
    """Keep the IPure score map shape stable so every node exposes the same six scenarios.

    Allowed values are 0..100, the -1 sentinel (upstream marked it region-restricted)
    and null (upstream published no score). Anything else — including -2 and 101 —
    is rejected. `score` is checked on its own too, so deleting `ipure_scores` cannot
    smuggle an out-of-scale total past the record contract.
    """

    scores = record.get("ipure_scores")
    total = record.get("score")
    if scores is None:
        if total is not None:
            raise ResultStoreError("节点 IPure 评分字段缺失")
        return
    if not isinstance(scores, dict) or set(scores) != {"total", *IPURE_SCENARIOS}:
        raise ResultStoreError("节点 IPure 评分字段集合无效")
    for value in scores.values():
        if value is None:
            continue
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or (value != IPURE_RESTRICTED_SCORE and not 0 <= value <= 100)
        ):
            raise ResultStoreError("节点 IPure 评分取值无效")
    if total != scores.get("total"):
        raise ResultStoreError("节点总分与 IPure 纯净度不一致")


def validate_ipure_evidence(evidence: dict[str, Any], requests: dict[str, Any]) -> None:
    """A score may come from the workspace proxy or, as a recorded exception, from the host."""

    ipure = requests.get("ipure") if isinstance(requests, dict) else None
    if not isinstance(ipure, dict):
        return
    direct = ipure.get("direct_fallback")
    if not isinstance(direct, bool):
        raise ResultStoreError("节点 IPure 直连标记无效")
    if evidence.get("ipure_via_direct_fallback") is not direct:
        raise ResultStoreError("节点 IPure 直连证据与请求记录不一致")
    if ipure.get("via_mihomo") is not (not direct):
        raise ResultStoreError("节点 IPure 出口证据无效")


_NETWORK_SOURCE_KEYS = ("exit_ip", "geo", "isp", "kind", "native")
_NETWORK_SOURCES = {"ipure", "coffee", None}


def validate_network_identity(record: dict[str, Any]) -> None:
    """IPure-first identity fields are optional (old artifacts lack them) but typed when present."""

    for field in ("country", "country_code", "region", "city"):
        value = record.get(field)
        if value is not None and not isinstance(value, str):
            raise ResultStoreError(f"节点记录的 {field} 无效")
    if record.get("usage_type") not in (None, *IPURE_USAGE_TYPES):
        raise ResultStoreError("节点记录的 usage_type 无效")
    source = record.get("network_source")
    if source is None:
        return
    if (
        not isinstance(source, dict)
        or set(source) != set(_NETWORK_SOURCE_KEYS)
        or any(source[key] not in _NETWORK_SOURCES for key in _NETWORK_SOURCE_KEYS)
    ):
        raise ResultStoreError("节点记录的 network_source 无效")


def validate_exit_ip(value: Any) -> None:
    try:
        ipaddress.ip_address(value)
    except (TypeError, ValueError) as exc:
        raise ResultStoreError("成功节点缺少合法出口 IP") from exc


def node_filename(index: int) -> str:
    return f"{index:04d}.json"


def encode_json(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + chr(10)).encode(
        "utf-8"
    )


def validate_manifest(
    job_id: str,
    manifest: dict[str, Any],
    node_records: list[dict[str, Any]],
    node_bytes: list[bytes],
) -> None:
    total = manifest.get("total")
    records = manifest.get("records")
    if (
        manifest.get("schema_version") != 1
        or manifest.get("job_id") != job_id
        or manifest.get("status") != "completed"
        or manifest.get("complete") is not True
        or manifest.get("all_records_present") is not True
        or manifest.get("completed") != total
        or not isinstance(total, int)
        or total < 1
        or not isinstance(records, list)
        or len(records) != total
    ):
        raise ResultStoreError("manifest 结构或任务绑定无效")
    if len(node_records) != total or len(node_bytes) != total:
        raise ResultStoreError("manifest 节点数量不一致")
    counts = {"success": 0, "partial": 0, "failed": 0, "complete": 0}
    for expected_index, entry in enumerate(records):
        if not isinstance(entry, dict):
            raise ResultStoreError("manifest 节点条目无效")
        filename = node_filename(expected_index)
        if entry.get("index") != expected_index or entry.get("file") != filename:
            raise ResultStoreError("manifest 节点索引或文件名不连续")
        raw = node_bytes[expected_index]
        digest = hashlib.sha256(raw).hexdigest()
        if entry.get("bytes") != len(raw) or entry.get("sha256") != digest:
            raise ResultStoreError(f"节点 {expected_index} hash/size 校验失败")
        record = node_records[expected_index]
        validate_node(job_id, expected_index, record)
        if entry.get("summary") != summary(record):
            raise ResultStoreError(f"节点 {expected_index} 摘要与文件不一致")
        status = record.get("status")
        if status not in {"success", "partial", "failed"}:
            raise ResultStoreError(f"节点 {expected_index} 状态无效")
        counts[status] += 1
        if (record.get("completeness") or {}).get("complete") is True:
            counts["complete"] += 1
        if status == "failed":
            if record.get("exit_ip") is not None:
                raise ResultStoreError(f"失败节点 {expected_index} 含有出口 IP")
        else:
            validate_exit_ip(record.get("exit_ip"))
    if manifest.get("counts") != counts:
        raise ResultStoreError("manifest 计数与节点文件不一致")
