from __future__ import annotations

import asyncio

import pytest

from backend.app.results.validation import ResultStoreError, validate_node
from backend.app.sources import ipx as ipx_source
from backend.app.sources.http import IPX_SELF_URL, _validate_ipx_url
from backend.app.sources.ipx import ipx_fields, ipx_report_url, parse_report

IP = "203.0.113.7"

REPORT = {
    "ip": IP,
    "health": {
        "score": 47,
        "grade": "较差",
        "ceiling": 55,
        "ipType": "hosting",
        "items": [
            {"label": "多源标记为代理", "pts": -8, "code": "proxy_multi"},
            {"label": "未发现 Tor 出口", "pts": 0, "code": "no_tor"},
            {"label": "bad", "pts": True},
        ],
        "model": "v3.6-2026-11-06",
        "coverage": {"low": False},
    },
    "native": {"verdict": "broadcast", "regCountry": "US"},
    "type": {"verdict": "hosting"},
    "generatedAt": "2026-11-11T07:45:37.631Z",
}


def test_parse_report_keeps_score_grade_and_real_items():
    report = parse_report(REPORT)
    assert (report["ip"], report["score"], report["grade"], report["ceiling"]) == (
        IP, 47, "较差", 55,
    )
    assert report["native"] is False
    assert [item["points"] for item in report["items"]] == [-8, 0]
    assert report["report_url"] == f"https://ipx.al/{IP}"


@pytest.mark.parametrize(
    "health",
    [{"score": 101}, {"score": 7.5}, {"score": True}, {}],
)
def test_parse_report_requires_an_integer_score(health):
    assert parse_report({**REPORT, "health": health}) is None


def test_parse_report_requires_a_valid_ip():
    assert parse_report({**REPORT, "ip": "nope"}) is None


def test_unknown_grade_becomes_null():
    report = parse_report({**REPORT, "health": {**REPORT["health"], "grade": "S"}})
    assert report["grade"] is None
    assert report["grade_raw"] == "S"


def test_url_allowlist():
    _validate_ipx_url(IPX_SELF_URL)
    _validate_ipx_url(ipx_report_url("2001:db8::1"))
    for bad in (
        "https://ipx.al/api/report/not-an-ip",
        "https://ipx.al/api/asn/13335",
        "https://ipx.al/json?x=1",
        "http://ipx.al/json",
        "https://evil.example/api/report/1.1.1.1",
    ):
        with pytest.raises(ValueError):
            _validate_ipx_url(bad)


def _proxied(ip: str) -> dict:
    return ipx_source._result(
        "http://127.0.0.1:7890", IPX_SELF_URL, 0.0,
        status_code=200, data=parse_report({**REPORT, "ip": ip}),
    )


def test_matching_proxied_report_is_kept(monkeypatch):
    async def never(*_args):
        raise AssertionError("不应走直连兜底")

    monkeypatch.setattr(ipx_source, "_direct", never)
    proxied = _proxied(IP)
    assert asyncio.run(ipx_source.for_exit_ip("http://127.0.0.1:7890", proxied, IP)) is proxied


def test_mismatched_ip_falls_back_to_report_by_ip(monkeypatch):
    calls = []

    async def direct(proxy_url, url):
        calls.append(url)
        return ipx_source._result(proxy_url, url, 0.0, status_code=200,
                                  data=parse_report(REPORT), direct_fallback=True)

    monkeypatch.setattr(ipx_source, "_direct", direct)
    result = asyncio.run(
        ipx_source.for_exit_ip("http://127.0.0.1:7890", _proxied("198.51.100.1"), IP)
    )
    assert calls == [ipx_report_url(IP)]
    assert result["direct_fallback"] is True
    assert result["via_mihomo"] is False
    assert result["proxy_error"] == "IPMismatch"


def test_mismatch_without_fallback_is_not_ok(monkeypatch):
    async def direct(*_args):
        return None

    monkeypatch.setattr(ipx_source, "_direct", direct)
    result = asyncio.run(
        ipx_source.for_exit_ip("http://127.0.0.1:7890", _proxied("198.51.100.1"), IP)
    )
    assert result["ok"] is False
    assert result["data"] is None
    assert ipx_fields(result)["ipx_score"] is None


def _with_ipx(record: dict, *, direct: bool = False, ip: str | None = None, **fields) -> dict:
    exit_ip = record["exit_ip"]
    report = parse_report({**REPORT, "ip": ip or exit_ip})
    record["requests"]["ipx"] = {
        "ok": True,
        "data": report,
        "direct_fallback": direct,
        "via_mihomo": not direct,
        "url": ipx_report_url(exit_ip) if direct else IPX_SELF_URL,
    }
    record["proxy_evidence"]["ipx_via_direct_fallback"] = direct
    record.update(ipx_fields({"data": report}), **fields)
    return record


@pytest.mark.parametrize("direct", [False, True])
def test_validator_accepts_proxied_and_fallback_records(make_success, direct):
    record = _with_ipx(make_success(0), direct=direct)
    validate_node(record["job_id"], 0, record)


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"ipx_score": 101}, "评分取值无效"),
        ({"ipx_grade": "A"}, "等级取值无效"),
        ({"ipx_score": None}, "等级取值无效"),
        ({"ipx_score": 46}, "不对应"),
    ],
)
def test_validator_rejects_out_of_domain_fields(make_success, fields, message):
    record = _with_ipx(make_success(0), **fields)
    with pytest.raises(ResultStoreError, match=message):
        validate_node(record["job_id"], 0, record)


def test_validator_rejects_a_report_for_another_ip(make_success):
    record = _with_ipx(make_success(0), ip="198.51.100.1")
    with pytest.raises(ResultStoreError, match="不对应"):
        validate_node(record["job_id"], 0, record)


def test_validator_rejects_direct_score_presented_as_proxy_evidence(make_success):
    record = _with_ipx(make_success(0), direct=True)
    record["proxy_evidence"]["ipx_via_direct_fallback"] = False
    with pytest.raises(ResultStoreError, match="出口证据无效"):
        validate_node(record["job_id"], 0, record)
