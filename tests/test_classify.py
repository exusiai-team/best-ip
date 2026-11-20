from __future__ import annotations

import copy
import json
from datetime import UTC, datetime

import pytest
import yaml

from backend.app.classify import (
    ClassifyInputError,
    build_result,
    dumps,
    fingerprint,
    plan_scan,
)

from .conftest import failed_record, success_record

NOW = datetime(2026, 11, 20, 19, 0, tzinfo=UTC)

SUBSCRIPTION = """
proxies:
  - {name: "🇭🇰 香港 01", type: vless, server: example.com, port: 443,
     uuid: 11111111-2222-3333-4444-555555555555}
  - {name: " 前后有空格 ", type: ss, server: 8.8.8.8, port: 8388, cipher: aes-128-gcm, password: pw}
  - {name: "🇭🇰 香港 01", type: trojan, server: 1.1.1.1, port: 443, password: secret-pass}
  - {name: "剩余流量：100 GB", type: ss, server: 9.9.9.9, port: 1, cipher: aes-128-gcm, password: x}
  - {name: 直连, type: direct}
  - {type: ss, server: 4.4.4.4, port: 1}
  - 不是对象
""".encode()


def test_fingerprint_matches_the_documented_js_algorithm():
    # 期望值用 Node 按接口文档的算法算出：type|server|port|(uuid ?? password ?? "")。
    vless = {
        "type": "vless", "server": "example.com", "port": 443,
        "uuid": "11111111-2222-3333-4444-555555555555", "password": "ignored",
    }
    assert fingerprint(vless) == "e3535beb39c6ec5d"
    ss = {"type": "ss", "server": "8.8.8.8", "port": 8388, "password": "pw"}
    assert fingerprint(ss) == "55303f6a971a8366"
    trojan = {"type": "trojan", "server": "🇭🇰.example", "port": "8443", "password": "密码"}
    assert fingerprint(trojan) == "d006208ee081fe85"


def test_plan_keeps_every_input_node_and_renames_duplicates_only_for_scanning():
    plan = plan_scan(SUBSCRIPTION, now=NOW)
    assert [node.index for node in plan.nodes] == list(range(7))
    assert [node.skip_reason for node in plan.nodes] == [
        None, None, None, "subscription_metadata", "unsupported_type", "invalid_node",
        "invalid_node",
    ]
    assert plan.scan_indexes == [0, 1, 2]
    scanned = yaml.safe_load(plan.scan_yaml)["proxies"]
    assert [proxy["name"] for proxy in scanned] == ["🇭🇰 香港 01", " 前后有空格 ", "🇭🇰 香港 01 #2"]
    assert scanned[2]["password"] == "secret-pass"


def test_plan_rejects_documents_without_proxies():
    with pytest.raises(ClassifyInputError) as error:
        plan_scan(b"rules: []\n")
    assert error.value.reason == "body_not_mihomo_yaml"


def test_plan_caps_scanned_nodes():
    plan = plan_scan(SUBSCRIPTION, max_nodes=2, now=NOW)
    assert plan.scan_indexes == [0, 1]
    assert plan.nodes[2].skip_reason == "too_many_nodes"


def _records(plan):
    records = []
    for position in range(plan.scan_count):
        name = yaml.safe_load(plan.scan_yaml)["proxies"][position]["name"]
        records.append(success_record(position, name=name))
    return records


def _enrich(record, *, ip, ipx=None, ipqs=None, ipure=None, gpt=None, **fields):
    record["exit_ip"] = ip
    record["requests"] = {
        "ipx": {"ok": True, "data": {"ip": ip, **(ipx or {})}},
        "ipqs": {"ok": True, "data": ipqs or {}},
        "ipure": {"ok": True, "data": {"network": ipure or {}}},
    }
    record["gpt_check"] = gpt or []
    record.update(fields)
    return record


def _gpt(chatgpt, openai, openai_code=401):
    return [
        {"name": "chatgpt.com", "status": chatgpt, "status_code": 200},
        {"name": "api.openai.com", "status": openai, "status_code": openai_code},
    ]


def test_build_result_maps_records_back_to_input_order_and_names():
    plan = plan_scan(SUBSCRIPTION, now=NOW)
    records = _records(plan)
    _enrich(
        records[0], ip="203.0.113.7",
        ipx={"usage_type": "residential", "registered_country": "us"},
        ipqs={
            "type": "residential",
            "flags": {
                "proxy": {"yes": 1, "of": 2},
                "vpn": {"yes": 2, "of": 3},
                "tor": {"yes": 0, "of": 0},
            },
        },
        ipure={"usage_type": "business"},
        gpt=_gpt("normal", "slow"),
        country_code="JP", usage_type="residential", is_native=True, asn=4713,
        as_org="NTT Communications", ipx_score=82, is_abuser=False,
    )
    _enrich(records[1], ip="203.0.113.7", gpt=_gpt("failed", "failed", 403), status="partial")
    records[2] = failed_record(2)
    result = build_result(
        plan, records, scan_id="clf-1", label="myclash:abcd1234", profile="classify",
        started_at="2026-11-20T19:17:00Z", finished_at="2026-11-20T19:41:12Z",
    )
    assert result["node_count"] == 7 and result["score_source"] == "ipx.health.score"
    nodes = result["nodes"]
    assert [node["name"] for node in nodes[:3]] == ["🇭🇰 香港 01", " 前后有空格 ", "🇭🇰 香港 01"]
    first = nodes[0]
    assert first == {
        "name": "🇭🇰 香港 01",
        "index": 0,
        "fingerprint": "e3535beb39c6ec5d",
        "status": "ok",
        "reason": None,
        "checked_at": "2026-10-24T00:00:01Z",
        "reused": False,
        "exit_ip": "203.0.113.7",
        "country_code": "JP",
        "reg_country_code": "US",
        "usage_type": "residential",
        "usage_votes": {"ipx": "residential", "ipqs": "residential", "ipure": "business"},
        "native": True,
        "asn": 4713,
        "as_org": "NTT Communications",
        "score": 82,
        "risk": {"proxy": False, "vpn": True, "tor": None, "abuser": False},
        "ai": {"openai": "ok", "claude": "unknown", "gemini": "unknown"},
        "same_exit": [" 前后有空格 "],
    }
    assert nodes[1]["status"] == "ok" and nodes[1]["ai"]["openai"] == "blocked"
    assert nodes[1]["same_exit"] == ["🇭🇰 香港 01"]
    assert nodes[2]["status"] == "failed" and nodes[2]["reason"] == "collect_failed"
    assert nodes[2]["exit_ip"] is None and nodes[2]["usage_votes"] == {}
    assert [node["status"] for node in nodes[3:]] == ["skipped"] * 4
    assert nodes[4]["reason"] == "unsupported_type"
    assert nodes[6]["name"] is None and nodes[6]["fingerprint"] is None
    assert result["counts"] == {"ok": 2, "failed": 1, "skipped": 4, "reused": 0}
    # 每个节点字段齐全（拿不到填 null，不省略），结果里没有任何节点凭据。
    assert all(set(node) == set(first) for node in nodes)
    text = dumps(result).decode()
    for secret in ("11111111-2222-3333-4444-555555555555", "secret-pass", '"pw"', "aes-128-gcm"):
        assert secret not in text


def test_openai_unknown_when_only_the_connection_failed():
    plan = plan_scan(SUBSCRIPTION, now=NOW)
    records = _records(plan)
    _enrich(records[0], ip="203.0.113.7", gpt=[
        {"name": "chatgpt.com", "status": "failed", "status_code": None},
        {"name": "api.openai.com", "status": "failed", "status_code": None},
    ])
    _enrich(records[1], ip="203.0.113.8", gpt=_gpt("restricted", "restricted"))
    result = build_result(
        plan, records, scan_id="s", label="l", profile="classify", started_at="a", finished_at="b"
    )
    assert result["nodes"][0]["ai"]["openai"] == "unknown"
    assert result["nodes"][1]["ai"]["openai"] == "blocked"


def test_openai_blocked_in_restricted_country_even_without_probe_answers():
    plan = plan_scan(SUBSCRIPTION, now=NOW)
    records = _records(plan)
    _enrich(records[0], ip="203.0.113.7", country_code="HK", gpt=[
        {"name": "chatgpt.com", "status": "failed", "status_code": None},
        {"name": "api.openai.com", "status": "failed", "status_code": None},
    ])
    _enrich(records[1], ip="203.0.113.8", country_code="JP", gpt=_gpt("normal", "good"))
    result = build_result(
        plan, records, scan_id="s", label="l", profile="classify", started_at="a", finished_at="b"
    )
    assert [node["ai"]["openai"] for node in result["nodes"][:2]] == ["blocked", "ok"]


def test_reuse_keeps_fresh_ok_results_and_rescans_the_rest():
    first = plan_scan(SUBSCRIPTION, now=NOW)
    records = _records(first)
    for record, ip in zip(records, ("203.0.113.7", "203.0.113.8", "203.0.113.9"), strict=True):
        _enrich(record, ip=ip, ipx_score=70)
    records[0]["finished_at"] = "2026-11-20T10:00:00+00:00"
    records[1]["finished_at"] = "2026-11-19T10:00:00+00:00"  # 超过 20 小时
    previous = build_result(
        first, records, scan_id="old", label="l", profile="classify",
        started_at="a", finished_at="b",
    )
    renamed = SUBSCRIPTION.replace("🇭🇰 香港 01".encode(), "香港 新名字".encode(), 1)
    plan = plan_scan(renamed, previous=json.loads(dumps(previous)), reuse_hours=20, now=NOW)
    assert plan.nodes[0].reused is not None
    assert plan.scan_indexes == [1, 2]
    fresh = [_enrich(success_record(0, name=" 前后有空格 "), ip="203.0.113.7"),
             _enrich(success_record(1, name="🇭🇰 香港 01"), ip="203.0.113.10")]
    result = build_result(
        plan, fresh, scan_id="new", label="l", profile="classify", started_at="a", finished_at="b"
    )
    reused = result["nodes"][0]
    assert reused["name"] == "香港 新名字" and reused["reused"] is True
    assert reused["checked_at"] == "2026-11-20T10:00:00Z" and reused["score"] == 70
    assert reused["same_exit"] == [" 前后有空格 "]
    assert result["counts"]["reused"] == 1


def test_reuse_disabled_and_dialer_targets_are_rescanned():
    document = {
        "proxies": [
            {"name": "front", "type": "ss", "server": "1.1.1.1", "port": 1, "password": "a",
             "dialer-proxy": "relay"},
            {"name": "relay", "type": "ss", "server": "2.2.2.2", "port": 2, "password": "b"},
        ]
    }
    content = yaml.safe_dump(document).encode()
    previous = {"nodes": [{
        "fingerprint": fingerprint(document["proxies"][1]), "status": "ok",
        "checked_at": "2026-11-20T18:00:00Z",
    }]}
    assert plan_scan(content, previous=previous, reuse_hours=0, now=NOW).scan_indexes == [0, 1]
    plan = plan_scan(content, previous=copy.deepcopy(previous), reuse_hours=20, now=NOW)
    assert plan.scan_indexes == [0, 1]


def test_everything_reused_needs_no_scan():
    document = {"proxies": [{"name": "a", "type": "ss", "server": "1.1.1.1", "port": 1}]}
    previous = {"nodes": [{
        "fingerprint": fingerprint(document["proxies"][0]), "status": "ok",
        "checked_at": "2026-11-20T18:00:00Z", "exit_ip": "203.0.113.1",
    }]}
    plan = plan_scan(yaml.safe_dump(document).encode(), previous=previous, reuse_hours=2, now=NOW)
    assert plan.scan_yaml is None and plan.scan_count == 0
    result = build_result(
        plan, [], scan_id="s", label="l", profile="classify", started_at="a", finished_at="b"
    )
    assert result["nodes"][0]["exit_ip"] == "203.0.113.1" and result["nodes"][0]["reused"]
