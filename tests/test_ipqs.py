from __future__ import annotations

import json

import pytest

from backend.app.results.validation import ResultStoreError, validate_node
from backend.app.sources.collector import _network_identity
from backend.app.sources.ipqs import ipqs_fields, ipqs_url, parse_report
from backend.app.sources.who import parse_me

IP = "203.0.113.7"


def _stream(done: dict) -> str:
    events = [
        {"t": "start", "ip": IP, "n": 2},
        {"t": "src", "id": "ipapi", "ok": True, "d": {"cc": "JP"}},
        {"t": "done", **done},
    ]
    return "\n".join(json.dumps(event) for event in events) + "\n"


DONE = {
    "score": 73,
    "grade": "B",
    "parts": [{"k": "p_hosting", "pts": 20, "p": {"n": 2, "of": 2}}, {"k": "p_risk", "pts": 7}],
    "summary": {"type": "hosting", "native": False, "cc": "JP", "regCc": "US", "regFrom": "rir"},
    "at": "2026-11-03T04:31:42.281Z",
}


def test_parse_report_reads_only_the_done_event():
    report = parse_report(_stream(DONE), ipqs_url(IP))
    assert report["score"] == 73
    assert report["grade"] == "B"
    assert report["type"] == "hosting"
    assert report["native"] is False
    assert report["registered_country"] == "US"
    assert [item["key"] for item in report["deductions"]] == ["hosting", "risk"]
    assert report["report_url"] == f"https://ipqs.1ms.dog/zh-cn/?ip={IP}"


@pytest.mark.parametrize(
    "override",
    [{"score": 101}, {"score": 7.5}, {"score": True}, {"score": None}],
)
def test_parse_report_requires_an_integer_score(override):
    assert parse_report(_stream({**DONE, **override}), ipqs_url(IP)) is None


def test_parse_report_drops_unknown_upstream_vocabulary():
    done = {**DONE, "grade": "A+", "summary": {**DONE["summary"], "type": "satellite"}}
    report = parse_report(_stream(done), ipqs_url(IP))
    assert report["grade"] is None
    assert report["type"] is None
    assert report["type_raw"] == "satellite"


def test_ipv6_report_url_is_encoded():
    assert ipqs_url("2001:db8::1") == "https://ipqs.1ms.dog/api/check?ip=2001%3Adb8%3A%3A1"


def test_ipqs_wins_kind_and_native_over_ipure_and_coffee():
    ipqs = parse_report(_stream(DONE), ipqs_url(IP))
    ipure = {"usage_type": "residential", "is_residential": True, "is_native": True}
    coffee = {"is_residential": True, "is_native": True, "location": ""}
    identity = _network_identity(IP, None, ipure, ipqs, coffee)
    assert identity["usage_type"] == "hosting"
    assert identity["is_datacenter"] is True
    assert identity["is_native"] is False
    assert identity["native_status"] == "广播 IP (US)"
    assert identity["network_source"]["kind"] == "ipqs"
    assert identity["network_source"]["native"] == "ipqs"


def test_identity_falls_back_to_ipure_without_ipqs():
    ipure = {"usage_type": "residential", "is_residential": True, "is_native": True}
    identity = _network_identity(IP, None, ipure, None, {"location": ""})
    assert identity["network_source"]["kind"] == "ipure"
    assert identity["network_source"]["native"] == "ipure"


WHO_ME = {
    "ip": IP,
    "cf": {"country": "jp", "region": "Tokyo", "city": "Tokyo", "asn": 16509,
           "asOrganization": "Amazon Data Services Japan", "colo": "NRT"},
    "h": {"user-agent": "x"},
    "ptr": "ec2-203-0-113-7.ap-northeast-1.compute.amazonaws.com",
}
IPURE_NETWORK = {
    "ip": IP, "country": "Japan", "country_code": "JP", "region": "Kanto", "city": "Chiyoda",
    "asn": 16509, "as_name": "Amazon.com, Inc.", "org": "AWS Tokyo",
}


def test_parse_me_keeps_identity_and_drops_echoed_headers():
    report = parse_me(WHO_ME)
    assert report["country_code"] == "JP"
    assert report["asn"] == 16509
    assert report["ptr"].startswith("ec2-")
    assert "h" not in report
    assert parse_me({**WHO_ME, "ip": "not-an-ip"}) is None


def test_who_wins_geo_isp_and_rdns_over_ipqs_and_ipure():
    ipqs = parse_report(_stream({**DONE, "summary": {**DONE["summary"], "asn": 1, "org": "X"}}),
                        ipqs_url(IP))
    identity = _network_identity(IP, parse_me(WHO_ME), IPURE_NETWORK, ipqs, {})
    source = identity["network_source"]
    assert (source["exit_ip"], source["geo"], source["isp"]) == ("who", "who", "who")
    assert identity["country_code"] == "JP"
    assert (identity["region"], identity["city"]) == ("Tokyo", "Tokyo")
    # 国名只有 IPure 给；国家代码一致时借用。同 ASN 时 AS 组织借用 IPure 清洗过的 AS 名。
    assert identity["country"] == "Japan"
    assert identity["location"] == "Japan Tokyo"
    assert identity["asn"] == 16509
    assert identity["isp"] == "Amazon Data Services Japan"
    assert identity["as_org"] == "Amazon.com, Inc."
    assert identity["rdns"] == WHO_ME["ptr"]


def test_who_for_another_ip_is_ignored_and_ipqs_takes_over():
    ipqs = parse_report(
        _stream({**DONE, "summary": {**DONE["summary"], "city": "Osaka", "asn": 2516,
                                     "org": "KDDI", "host": "x.kddi.ne.jp"}}),
        ipqs_url(IP),
    )
    identity = _network_identity(IP, parse_me({**WHO_ME, "ip": "198.51.100.1"}), {}, ipqs, {})
    source = identity["network_source"]
    assert (source["exit_ip"], source["geo"], source["isp"]) == ("coffee", "ipqs", "ipqs")
    assert (identity["country_code"], identity["city"]) == ("JP", "Osaka")
    assert identity["country"] is None
    assert (identity["asn"], identity["isp"], identity["rdns"]) == (2516, "KDDI", "x.kddi.ne.jp")


def test_identity_never_falls_back_to_coffee_for_geo_or_isp():
    coffee = {"location": "Coffee Land", "isp": "Coffee ISP", "asn": 1}
    identity = _network_identity(IP, None, None, None, coffee)
    source = identity["network_source"]
    assert (source["geo"], source["isp"]) == (None, None)
    assert (identity["location"], identity["isp"], identity["asn"]) == ("", "", None)


def _with_who(record: dict) -> dict:
    record["requests"]["who"] = {
        "ok": True, "via_mihomo": True, "data": parse_me({**WHO_ME, "ip": record["exit_ip"]}),
    }
    record["network_source"] = {
        "exit_ip": "who", "geo": "who", "isp": "who", "kind": None, "native": None,
    }
    return record


def test_validator_accepts_who_sourced_identity(make_success):
    record = _with_who(make_success(0))
    validate_node(record["job_id"], 0, record)


def test_validator_rejects_who_source_for_another_ip(make_success):
    record = _with_who(make_success(0))
    record["requests"]["who"]["data"]["ip"] = "198.51.100.1"
    with pytest.raises(ResultStoreError, match="1ms who"):
        validate_node(record["job_id"], 0, record)


def _with_ipqs(record: dict, **fields) -> dict:
    report = parse_report(_stream(DONE), ipqs_url(record["exit_ip"]))
    record["requests"]["ipqs"] = {
        "ok": True, "data": report, "direct_fallback": False, "via_mihomo": True,
    }
    record["proxy_evidence"]["ipqs_via_direct_fallback"] = False
    record.update(ipqs_fields({"data": report}), **fields)
    return record


def test_validator_accepts_a_proxied_ipqs_record(make_success):
    validate_node(make_success(0)["job_id"], 0, _with_ipqs(make_success(0)))


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"ipqs_score": 101}, "评分取值无效"),
        ({"ipqs_score": True}, "评分取值无效"),
        ({"ipqs_grade": "A+"}, "等级取值无效"),
        ({"ipqs_score": None}, "等级取值无效"),
    ],
)
def test_validator_rejects_out_of_domain_ipqs_fields(make_success, fields, message):
    record = _with_ipqs(make_success(0), **fields)
    with pytest.raises(ResultStoreError, match=message):
        validate_node(record["job_id"], 0, record)


def test_validator_rejects_direct_score_presented_as_proxy_evidence(make_success):
    record = _with_ipqs(make_success(0))
    record["proxy_evidence"]["ipqs_via_direct_fallback"] = True
    with pytest.raises(ResultStoreError, match="出口证据无效"):
        validate_node(record["job_id"], 0, record)
