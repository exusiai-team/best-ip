from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from ..mihomo import (
    MIHOMO_NOT_READY_MESSAGE,
    MihomoError,
    MihomoNotReadyError,
    MihomoProcess,
    MihomoStopError,
)
from ..results.store import ResultStoreError
from ..subscription import SubscriptionError


@dataclass(frozen=True, slots=True)
class ScanError:
    code: str
    phase: str
    retryable: bool
    message: str


def classify_error(exc: Exception, *, phase: str = "collect") -> ScanError:
    if isinstance(exc, MihomoStopError):
        return ScanError("cleanup_failed", "cleanup", False, "Mihomo 进程或工作目录清理未确认")
    if isinstance(exc, MihomoNotReadyError):
        return ScanError("environment_not_ready", "start", False, MIHOMO_NOT_READY_MESSAGE)
    if isinstance(exc, SubscriptionError):
        return ScanError("subscription_invalid", "prepare", False, "订阅下载或解析失败")
    if isinstance(exc, ResultStoreError):
        return ScanError("result_invalid", phase, False, "扫描结果写入或校验失败")
    if isinstance(exc, MihomoError):
        message = _safe_exception_error(exc)
        if "配置不受支持" in message or "dependency" in str(exc).lower():
            return ScanError("node_configuration", "start", False, message)
        if phase in {"select", "selector"}:
            return ScanError("selector_unconfirmed", "select", True, message)
        return ScanError("node_transport", phase, True, message)
    if phase in {"collect", "collector"}:
        return ScanError("node_collection", "collect", True, _safe_exception_error(exc))
    return ScanError("scan_failed", phase, False, _safe_job_error(exc))


_TRANSPORT_ERROR_TYPES = {
    "CloseError",
    "ConnectError",
    "ConnectTimeout",
    "LocalProtocolError",
    "NetworkError",
    "PoolTimeout",
    "ProtocolError",
    "ProxyError",
    "ReadError",
    "ReadTimeout",
    "RemoteProtocolError",
    "TimeoutException",
    "WriteError",
    "WriteTimeout",
}


def _read_mihomo_error(mihomo: MihomoProcess, offset: int) -> str:
    try:
        return mihomo.read_log_since(offset)
    except (OSError, ValueError):
        return ""


def _attach_mihomo_error(result: dict[str, Any], mihomo: MihomoProcess, offset: int) -> None:
    requests = result.get("requests")
    if not isinstance(requests, dict) or not any(
        isinstance(request, dict) and request.get("error_type") in _TRANSPORT_ERROR_TYPES
        for request in requests.values()
    ):
        return

    message = _read_mihomo_error(mihomo, offset)
    transport_error = _summarize_mihomo_error(message) if message else "节点传输连接失败"
    result["transport_error"] = transport_error
    result["error"] = _merge_error(result.get("error"), transport_error)
    pages = result.get("pages")
    ip_page = pages.get("ip") if isinstance(pages, dict) else None
    if isinstance(ip_page, dict):
        ip_page["error"] = result["error"]


def _failed_node(
    job_id: str,
    index: int,
    node_name: str,
    node_type: str,
    exc: Exception,
    *,
    phase: str | None = None,
    mihomo_error: str = "",
    mihomo: MihomoProcess | None = None,
) -> dict[str, Any]:
    exception_error = _safe_exception_error(exc)
    transport_error = exception_error
    if mihomo_error and exception_error == "Mihomo 节点连接失败":
        transport_error = _summarize_mihomo_error(mihomo_error)
    error = transport_error
    return {
        "schema_version": 1,
        "job_id": job_id,
        "node_index": index,
        "node": node_name,
        "type": node_type,
        "status": "failed",
        "error": error,
        "phase": phase or ("selector" if isinstance(exc, MihomoError) else "collector"),
        "started_at": _now(),
        "finished_at": _now(),
        "exit_ip": None,
        "selected_proxy": None,
        "requested_proxy": node_name,
        "proxy_evidence": {
            "transport": "workspace_mihomo_mixed_port",
            "proxy_url": mihomo.proxy_url if mihomo else None,
            "mihomo_instance": mihomo.instance_id if mihomo else None,
            "selector": MihomoProcess.group_name,
            "requested_proxy": node_name,
            "selection_confirmed": False,
            "target_origin": "https://ip.net.coffee",
            "trust_env": False,
        },
        "requests": {"gpt_check": []},
        "coffee": {"gpt_check": []},
        "completeness": {
            "complete": False,
            "required": {"page": False, "trace": False, "lookup": False},
            "optional": {},
            "missing": ["selector_or_collector"],
        },
        "transport_error": transport_error,
        "cidr": "",
        "rdns": "-",
        "ai_verdict": "",
        "location": "",
        "isp": "",
        "score": None,
        "is_residential": None,
        "is_datacenter": None,
        "is_native": None,
        "native_status": "未知",
        "native_detail": "",
        "is_bogon": False,
        "bogon_status": "未知",
        "bogon_reason": "",
        "rpki_status": "未知",
        "asn_kind": "",
        "asn_kind_display": "未知",
        "abuse_level": "未知",
        "honeypot_status": "未知",
        "traffic_profile": "未知",
        "company_type": "未知",
        "is_vpn": None,
        "is_proxy": None,
        "is_tor": None,
        "is_crawler": None,
        "is_abuser": None,
        "security_status": "检测失败",
        "threat_tags": [],
        "asn": None,
        "as_org": "",
        "global_ping": [],
        "port_scan": None,
        "ping_check": None,
        "gpt_check": [],
        "related_domains": [],
        "elapsed_ms": 0,
    }


def _summarize_mihomo_error(message: str) -> str:
    normalized = message.lower()
    if "parse config error" in normalized or "unsupport proxy type" in normalized:
        return "Mihomo 节点配置不受支持"
    if "dns resolve failed" in normalized or "no such host" in normalized:
        return "节点服务器域名无法解析"
    if "reality authentication failed" in normalized:
        return "节点 REALITY 认证失败"
    if "context deadline exceeded" in normalized or "i/o timeout" in normalized:
        return "连接节点服务器超时"
    if "connection refused" in normalized:
        return "节点服务器拒绝连接"
    if "tls handshake" in normalized:
        return "节点 TLS 握手失败"
    if "connection reset" in normalized:
        return "节点服务器重置连接"
    if "eof" in normalized or "broken pipe" in normalized:
        return "节点服务器提前断开连接"
    if "network is unreachable" in normalized:
        return "节点服务器网络不可达"
    return "Mihomo 节点连接失败"


# 实时进度里每个节点的「为什么」：只放代码自己写出来的那几句分类文案（白名单），
# 不放任何可能夹带主机名、IP 或订阅内容的原文——进度不经过产物那道脱敏与逐字节校验。
_LIVE_REASONS = frozenset(
    {
        "Mihomo 节点配置不受支持",
        "节点服务器域名无法解析",
        "节点 REALITY 认证失败",
        "连接节点服务器超时",
        "节点服务器拒绝连接",
        "节点 TLS 握手失败",
        "节点服务器重置连接",
        "节点服务器提前断开连接",
        "节点服务器网络不可达",
        "Mihomo 节点连接失败",
        "Mihomo selector 未确认所选节点",
        "节点传输连接失败",
    }
)
_LIVE_REASON_PATTERNS = (
    re.compile(r"Mihomo selector 切换失败（HTTP \d{3}）"),
    re.compile(r"节点采集失败（[A-Za-z]{1,40}）"),
)
# completeness.unrecorded_requests 的键 → 「部分」节点缺了哪几项。
_LIVE_MISSING = {
    "ipure_recorded": "IPure 评分",
    "global_ping_recorded": "全球 Ping",
    "port_scan_recorded": "端口扫描",
    "ping_check_recorded": "连通检测",
    "related_recorded": "关联域名",
}


def live_reason(text: object) -> str:
    """一段错误文案 → 白名单里的一句；合并过的多句（「；」分隔）取第一句认得的。"""

    for part in str(text or "").split("；"):
        part = part.strip()
        if part in _LIVE_REASONS or any(
            pattern.fullmatch(part) for pattern in _LIVE_REASON_PATTERNS
        ):
            return part
    return "检测失败"


def live_record_reason(record: dict[str, Any]) -> str | None:
    """终态记录 → 实时视图里的一句原因：失败说卡在哪，部分说缺了什么，完整不说。"""

    status = record.get("status")
    if status == "failed":
        return live_reason(record.get("transport_error") or record.get("error"))
    if status == "partial":
        completeness = record.get("completeness")
        if not isinstance(completeness, dict):
            completeness = {}
        missing = completeness.get("unrecorded_requests")
        labels = [_LIVE_MISSING[key] for key in missing or [] if key in _LIVE_MISSING]
        return f"缺 {'、'.join(labels)}" if labels else "富化数据不完整"
    return None


def _safe_exception_error(exc: Exception) -> str:
    if isinstance(exc, MihomoError):
        message = str(exc)
        prefix = "切换节点失败：HTTP "
        if message.startswith(prefix):
            status_code = message.removeprefix(prefix).strip()
            if status_code.isdigit():
                return f"Mihomo selector 切换失败（HTTP {status_code}）"
        if message.startswith("切换节点后未确认 selector 身份"):
            return "Mihomo selector 未确认所选节点"
        return _summarize_mihomo_error(str(exc))
    return f"节点采集失败（{exc.__class__.__name__}）"


def _merge_error(*messages: str | None) -> str:
    parts = [" ".join(message.split()) for message in messages if message]
    return "；".join(dict.fromkeys(parts))[:2000]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_job_error(exc: Exception) -> str:
    if isinstance(exc, (SubscriptionError, ResultStoreError)):
        return _short_error(exc)
    if isinstance(exc, MihomoNotReadyError):
        return str(exc)
    if isinstance(exc, MihomoError):
        return _summarize_mihomo_error(str(exc))
    return f"扫描任务失败（{exc.__class__.__name__}）"


def _short_error(exc: Exception) -> str:
    return " ".join(str(exc).split())[:1000] or exc.__class__.__name__
