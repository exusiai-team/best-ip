"""把一次扫描的节点记录压成 MyClash 用的分类结果（`/api/v1/classify` 与本地 CLI 共用）。

结果格式见 docs/CLASSIFY_API.md。这里只做三件事：

1. `plan_scan`：读输入 YAML，逐条定下每个节点是「要测」「跳过」还是「复用上次结果」，
   给要测的节点生成一份扫描用 YAML。重名节点在扫描 YAML 里加后缀区分（Mihomo 要求名字唯一），
   结果仍按输入下标逐条回到原名，不合并。
2. `build_result`：把扫描记录（按扫描 YAML 的顺序）和跳过 / 复用的条目合成结果，节点顺序与输入一致。
3. `main`：本地 CLI，`uv run python -m backend.app.classify --input sub.yaml --output result.json`。

结果里只有名字、下标、指纹和测出来的标签，不含任何节点配置字段（uuid、password、私钥……）。
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import dataclasses
import hashlib
import json
import math
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from .scan.errors import live_reason
from .sources.gpt_checks import GPT_RESTRICTED_COUNTRIES
from .sources.ipure import IPURE_USAGE_TYPES
from .subscription import is_subscription_metadata

SCHEMA_VERSION = 1
PROFILES = ("classify", "full")
# 分数取 ipx.al 健康分（0..100，越高越干净）。
SCORE_SOURCE = "ipx.health.score"
AI_SERVICES = ("openai", "claude", "gemini")
MAX_REUSE_HOURS = 168
# Mihomo 里不是「出站代理」的类型：测不出落地 IP，直接跳过。
UNSUPPORTED_TYPES = frozenset({"direct", "reject", "reject-drop", "pass", "compatible", "dns"})
_COUNTRY_CODE = re.compile(r"[A-Z]{2}")

# 失败记录的白名单原因文案（scan/errors.py 的 live_reason）→ 结果里的 reason 码。
_FAILURE_REASONS = {
    "Mihomo 节点配置不受支持": "unsupported_type",
    "节点服务器域名无法解析": "dns_failed",
    "节点 REALITY 认证失败": "auth_failed",
    "连接节点服务器超时": "timeout",
    "节点服务器拒绝连接": "connection_refused",
    "节点 TLS 握手失败": "tls_failed",
    "节点服务器重置连接": "connection_reset",
    "节点服务器提前断开连接": "connection_reset",
    "节点服务器网络不可达": "network_unreachable",
    "Mihomo 节点连接失败": "connect_failed",
    "节点传输连接失败": "connect_failed",
    "Mihomo selector 未确认所选节点": "proxy_error",
}


class ClassifyInputError(ValueError):
    """输入 YAML 不能用。reason 是固定原因码，可以进日志。"""

    def __init__(self, message: str, *, reason: str) -> None:
        self.reason = reason
        super().__init__(message)


def _js_string(value: Any) -> str:
    """与 JS 的 `String(value)` 一致的字符串化（缺失按空串），MyClash 用同一套规则算指纹。"""

    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer() and math.isfinite(value):
        return str(int(value))
    return str(value)


def fingerprint(proxy: dict[str, Any]) -> str:
    """sha256(type|server|port|(uuid ?? password ?? "")) 的十六进制前 16 位。

    取输入原值，不解析域名。
    """

    secret = proxy.get("uuid")
    if secret is None:
        secret = proxy.get("password")
    material = "|".join(
        _js_string(part)
        for part in (proxy.get("type"), proxy.get("server"), proxy.get("port"), secret)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def iso_now() -> str:
    return _iso(datetime.now(UTC))


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _normalize_time(value: Any) -> str | None:
    moment = _parse_time(value)
    return _iso(moment) if moment else None


@dataclass(slots=True)
class InputNode:
    index: int
    name: str | None
    fingerprint: str | None
    proxy: dict[str, Any] | None
    skip_reason: str | None = None
    reused: dict[str, Any] | None = None


@dataclass(slots=True)
class Plan:
    nodes: list[InputNode]
    input_sha256: str
    # 扫描 YAML 里第 i 个节点对应的输入下标。
    scan_indexes: list[int] = field(default_factory=list)
    scan_yaml: bytes | None = None

    @property
    def scan_count(self) -> int:
        return len(self.scan_indexes)


def load_proxies(content: bytes) -> list[Any]:
    try:
        document = yaml.safe_load(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ClassifyInputError("订阅不是有效的 UTF-8 YAML", reason="body_not_utf8_yaml") from exc
    if not isinstance(document, dict) or not isinstance(document.get("proxies"), list):
        raise ClassifyInputError(
            "订阅必须是顶部含 proxies 列表的 YAML", reason="body_not_mihomo_yaml"
        )
    if not document["proxies"]:
        raise ClassifyInputError("订阅中没有节点", reason="body_no_proxies")
    return document["proxies"]


def _reusable(previous: Any, reuse_hours: float, now: datetime) -> dict[str, dict[str, Any]]:
    """上次结果里还在复用期内、状态为 ok 的节点，按指纹索引（同指纹取最近测的那条）。"""

    if (
        reuse_hours <= 0
        or not isinstance(previous, dict)
        or not isinstance(previous.get("nodes"), list)
    ):
        return {}
    cutoff = now - timedelta(hours=reuse_hours)
    reusable: dict[str, tuple[datetime, dict[str, Any]]] = {}
    for entry in previous["nodes"]:
        if not isinstance(entry, dict) or entry.get("status") != "ok":
            continue
        key = entry.get("fingerprint")
        checked = _parse_time(entry.get("checked_at"))
        if (
            not isinstance(key, str)
            or checked is None
            or checked < cutoff
            or checked > now + timedelta(minutes=5)
        ):
            continue
        if key not in reusable or checked > reusable[key][0]:
            reusable[key] = (checked, entry)
    return {key: entry for key, (_, entry) in reusable.items()}


def plan_scan(
    content: bytes,
    *,
    previous: Any = None,
    reuse_hours: float = 0,
    max_nodes: int = 1000,
    now: datetime | None = None,
) -> Plan:
    proxies = load_proxies(content)
    moment = now or datetime.now(UTC)
    reusable = _reusable(previous, reuse_hours, moment)
    nodes: list[InputNode] = []
    for index, proxy in enumerate(proxies):
        if not isinstance(proxy, dict):
            nodes.append(InputNode(index, None, None, None, "invalid_node"))
            continue
        raw_name = proxy.get("name")
        name = (
            raw_name
            if isinstance(raw_name, str)
            else (
                None
                if raw_name is None or isinstance(raw_name, (dict, list))
                else _js_string(raw_name)
            )
        )
        node = InputNode(index, name, fingerprint(proxy), proxy)
        proxy_type = proxy.get("type")
        if name is not None and is_subscription_metadata({"name": name}):
            node.skip_reason = "subscription_metadata"
        elif (
            not name
            or not name.strip()
            or not isinstance(proxy_type, str)
            or not proxy_type.strip()
        ):
            node.skip_reason = "invalid_node"
        elif proxy_type.strip().lower() in UNSUPPORTED_TYPES:
            node.skip_reason = "unsupported_type"
        elif node.fingerprint in reusable:
            node.reused = reusable[node.fingerprint]
        nodes.append(node)

    scanned = [node for node in nodes if node.skip_reason is None and node.reused is None]
    # 被要测的节点当作 dialer-proxy 的节点也得在扫描 YAML 里，复用了也要再测一次。
    dialers = {node.proxy.get("dialer-proxy") for node in scanned if node.proxy}
    for node in nodes:
        if node.reused is not None and node.name in dialers:
            node.reused = None
    scanned = [node for node in nodes if node.skip_reason is None and node.reused is None]
    for node in scanned[max_nodes:]:
        node.skip_reason = "too_many_nodes"
    scanned = scanned[:max_nodes]

    plan = Plan(nodes, hashlib.sha256(content).hexdigest())
    if not scanned:
        return plan
    taken = {node.name for node in nodes if node.name}
    seen: set[str] = set()
    documents = []
    for node in scanned:
        assert node.proxy is not None and node.name is not None
        scan_name = node.name
        if scan_name in seen:
            suffix = 2
            while f"{node.name} #{suffix}" in taken or f"{node.name} #{suffix}" in seen:
                suffix += 1
            scan_name = f"{node.name} #{suffix}"
        seen.add(scan_name)
        documents.append({**node.proxy, "name": scan_name})
        plan.scan_indexes.append(node.index)
    plan.scan_yaml = yaml.safe_dump(
        {"proxies": documents}, allow_unicode=True, sort_keys=False
    ).encode("utf-8")
    return plan


def _data(requests: dict[str, Any], name: str) -> dict[str, Any]:
    result = requests.get(name)
    if (
        not isinstance(result, dict)
        or not result.get("ok")
        or not isinstance(result.get("data"), dict)
    ):
        return {}
    return result["data"]


def _country(value: Any) -> str | None:
    text = value.strip().upper() if isinstance(value, str) else ""
    return text if _COUNTRY_CODE.fullmatch(text) else None


def _usage(value: Any) -> str | None:
    return value if isinstance(value, str) and value in IPURE_USAGE_TYPES else None


def _flag(flags: Any, key: str) -> bool | None:
    """1ms IPQS 的多源标记：过半数来源说是才算是；没有来源报过就是 null。"""

    item = flags.get(key) if isinstance(flags, dict) else None
    if not isinstance(item, dict):
        return None
    yes, total = item.get("yes"), item.get("of")
    if not isinstance(yes, int) or not isinstance(total, int) or total <= 0:
        return None
    return yes * 2 > total


def _risk(record: dict[str, Any], ipqs: dict[str, Any]) -> dict[str, bool | None]:
    flags = ipqs.get("flags")
    risk: dict[str, bool | None] = {}
    for key in ("proxy", "vpn", "tor", "abuser"):
        value = _flag(flags, key)
        if value is None:
            fallback = record.get(f"is_{key}")
            value = fallback if isinstance(fallback, bool) else None
        risk[key] = value
    return risk


def _openai(record: dict[str, Any]) -> str:
    """GPT 探测（chatgpt.com 与 api.openai.com）→ ok / blocked / unknown。

    两个都连通且不在受限地区才算 ok；地区受限，或上游明确回了拒绝的 HTTP 状态码
    （如 403）算 blocked；
    只是连不上（超时、握手失败）说明不了 OpenAI 的态度，记 unknown。
    """

    # 记录里的 gpt_check 只按 Coffee lookup 的国家判受限；这里再按 ipx 优先的落地国家补判一次。
    if _country(record.get("country_code")) in GPT_RESTRICTED_COUNTRIES:
        return "blocked"
    probes = {
        item.get("name"): item for item in record.get("gpt_check") or [] if isinstance(item, dict)
    }
    states = [probes.get(name) for name in ("chatgpt.com", "api.openai.com")]
    if any(state is None for state in states):
        return "unknown"
    statuses = [state.get("status") for state in states]
    if "restricted" in statuses:
        return "blocked"
    if all(status in ("normal", "good", "slow") for status in statuses):
        return "ok"
    if any(
        state.get("status") == "failed" and isinstance(state.get("status_code"), int)
        for state in states
    ):
        return "blocked"
    return "unknown"


def _failure_reason(record: dict[str, Any]) -> str:
    text = live_reason(record.get("transport_error") or record.get("error"))
    if text in _FAILURE_REASONS:
        return _FAILURE_REASONS[text]
    if text.startswith("Mihomo selector 切换失败"):
        return "proxy_error"
    if text.startswith("节点采集失败"):
        return "collect_failed"
    return "no_exit_ip"


def _empty_entry(
    node: InputNode, status: str, reason: str | None, checked_at: str | None
) -> dict[str, Any]:
    return {
        "name": node.name,
        "index": node.index,
        "fingerprint": node.fingerprint,
        "status": status,
        "reason": reason,
        "checked_at": checked_at,
        "reused": False,
        "exit_ip": None,
        "country_code": None,
        "reg_country_code": None,
        "usage_type": None,
        "usage_votes": {},
        "native": None,
        "asn": None,
        "as_org": None,
        "score": None,
        "risk": {"proxy": None, "vpn": None, "tor": None, "abuser": None},
        "ai": dict.fromkeys(AI_SERVICES, "unknown"),
        "same_exit": [],
    }


def node_from_record(node: InputNode, record: dict[str, Any]) -> dict[str, Any]:
    checked_at = _normalize_time(record.get("finished_at"))
    if record.get("status") not in ("success", "partial") or not record.get("exit_ip"):
        return _empty_entry(node, "failed", _failure_reason(record), checked_at)
    requests = record.get("requests") if isinstance(record.get("requests"), dict) else {}
    exit_ip = record["exit_ip"]
    ipx = _data(requests, "ipx")
    ipx = ipx if ipx.get("ip") == exit_ip else {}
    ipqs = _data(requests, "ipqs")
    network = _data(requests, "ipure").get("network")
    network = network if isinstance(network, dict) else {}
    votes = {
        name: value
        for name, value in (
            ("ipx", _usage(ipx.get("usage_type"))),
            ("ipqs", _usage(ipqs.get("type"))),
            ("ipure", _usage(network.get("usage_type"))),
        )
        if value is not None
    }
    reg_country = next(
        (
            code
            for code in (
                _country(ipx.get("registered_country")),
                _country(ipqs.get("registered_country")),
                _country(network.get("registered_country")),
            )
            if code
        ),
        None,
    )
    asn = record.get("asn")
    score = record.get("ipx_score")
    entry = _empty_entry(node, "ok", None, checked_at)
    entry.update(
        exit_ip=exit_ip,
        country_code=_country(record.get("country_code")),
        reg_country_code=reg_country,
        usage_type=_usage(record.get("usage_type")),
        usage_votes=votes,
        native=record.get("is_native") if isinstance(record.get("is_native"), bool) else None,
        asn=asn if isinstance(asn, int) and not isinstance(asn, bool) and asn > 0 else None,
        as_org=(record.get("as_org") or None) if isinstance(record.get("as_org"), str) else None,
        score=score
        if isinstance(score, int) and not isinstance(score, bool) and 0 <= score <= 100
        else None,
        risk=_risk(record, ipqs),
    )
    entry["ai"]["openai"] = _openai(record)
    return entry


def _reused_entry(node: InputNode) -> dict[str, Any]:
    assert node.reused is not None
    entry = _empty_entry(node, "ok", None, None)
    for key in entry:
        if key in node.reused and key not in ("name", "index", "fingerprint", "same_exit"):
            entry[key] = copy.deepcopy(node.reused[key])
    entry["status"] = "ok"
    entry["reason"] = None
    entry["reused"] = True
    return entry


def build_result(
    plan: Plan,
    records: list[dict[str, Any]],
    *,
    scan_id: str,
    label: str,
    profile: str,
    started_at: str,
    finished_at: str,
) -> dict[str, Any]:
    """records：扫描 YAML 顺序的节点记录（与 plan.scan_indexes 一一对应）。"""

    if len(records) != plan.scan_count:
        raise ValueError("扫描记录数与计划不一致")
    by_index = dict(zip(plan.scan_indexes, records, strict=True))
    nodes: list[dict[str, Any]] = []
    for node in plan.nodes:
        if node.skip_reason is not None:
            nodes.append(_empty_entry(node, "skipped", node.skip_reason, None))
        elif node.reused is not None:
            nodes.append(_reused_entry(node))
        else:
            nodes.append(node_from_record(node, by_index[node.index]))

    exits: dict[str, list[dict[str, Any]]] = {}
    for entry in nodes:
        if entry["status"] == "ok" and entry["exit_ip"]:
            exits.setdefault(entry["exit_ip"], []).append(entry)
    for group in exits.values():
        for entry in group:
            entry["same_exit"] = [other["name"] for other in group if other is not entry]

    counts = {
        status: sum(1 for entry in nodes if entry["status"] == status)
        for status in ("ok", "failed", "skipped")
    }
    counts["reused"] = sum(1 for entry in nodes if entry["reused"])
    return {
        "schema_version": SCHEMA_VERSION,
        "scan_id": scan_id,
        "label": label,
        "profile": profile,
        "started_at": started_at,
        "finished_at": finished_at,
        "input_sha256": plan.input_sha256,
        "node_count": len(nodes),
        "score_source": SCORE_SOURCE,
        "counts": counts,
        "nodes": nodes,
    }


def dumps(result: dict[str, Any]) -> bytes:
    return (
        json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")


# ---- 本地 CLI ---------------------------------------------------------------


async def scan_plan(plan: Plan, app_settings: Any, *, job_id: str) -> list[dict[str, Any]]:
    """用共享扫描核心测计划里的节点，返回扫描 YAML 顺序的节点记录。"""

    from .results.store import ResultStore
    from .scan.jobs import ScanJobManager
    from .subscription import SubscriptionError

    if plan.scan_yaml is None:
        return []
    content = plan.scan_yaml
    source = "https://classify.invalid/subscription"

    async def loader(url: str, *, max_bytes: int, timeout_seconds: float) -> bytes:
        if url != source or len(content) > max_bytes:
            raise SubscriptionError("订阅快照无效")
        return content

    owned = Path(tempfile.mkdtemp(prefix="best-ip-classify-")).resolve()
    manager = ScanJobManager(
        app_settings, ResultStore(owned / "results"), owned / "jobs", download=loader
    )
    try:
        manager.create(
            source, subscription_sha256=hashlib.sha256(content).hexdigest(), request_id=job_id
        )
        job = await manager.wait(job_id)
        if job.get("status") != "completed":
            failure = manager.get_error(job_id)
            raise RuntimeError(f"扫描未完成：{failure.code if failure else job.get('status')}")
        return list(manager.export(job_id)["results"])
    finally:
        await manager.shutdown()
        shutil.rmtree(owned, ignore_errors=True)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ClassifyInputError("复用文件不是有效 JSON", reason="reuse_invalid") from exc


async def run_cli(args: argparse.Namespace) -> dict[str, Any]:
    from .config import settings

    content = args.input.read_bytes()
    app_settings = dataclasses.replace(settings, scan_profile=args.profile)
    previous = _read_json(args.reuse) if args.reuse else None
    plan = plan_scan(
        content,
        previous=previous,
        reuse_hours=args.reuse_hours if args.reuse else 0,
        max_nodes=app_settings.max_nodes,
    )
    started_at = iso_now()
    scan_id = f"local-{hashlib.sha256(content).hexdigest()[:12]}"
    records = await scan_plan(plan, app_settings, job_id=scan_id)
    return build_result(
        plan,
        records,
        scan_id=scan_id,
        label=args.label,
        profile=args.profile,
        started_at=started_at,
        finished_at=iso_now(),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="扫描 Clash YAML 订阅，输出 MyClash 用的分类结果 JSON"
    )
    parser.add_argument("--input", required=True, type=Path, help="Clash YAML（顶层含 proxies）")
    parser.add_argument("--output", required=True, type=Path, help="结果 JSON 路径")
    parser.add_argument("--profile", choices=PROFILES, default="classify")
    parser.add_argument(
        "--reuse", type=Path, help="上一次的结果 JSON：指纹不变且在复用期内的节点不再测"
    )
    parser.add_argument("--reuse-hours", type=float, default=20, help="复用期（小时），默认 20")
    parser.add_argument("--label", default="local", help="写进结果的 label")
    args = parser.parse_args(argv)
    if not 0 <= args.reuse_hours <= MAX_REUSE_HOURS:
        parser.error(f"--reuse-hours 必须在 0..{MAX_REUSE_HOURS}")
    try:
        result = asyncio.run(run_cli(args))
    except ClassifyInputError as exc:
        print(f"{exc.reason}: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # 本地工具：原因写清楚即可
        print(f"classify_failed: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(dumps(result))
    counts = result["counts"]
    print(
        f"classify_completed: nodes={result['node_count']} ok={counts['ok']} "
        f"failed={counts['failed']} skipped={counts['skipped']} reused={counts['reused']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
