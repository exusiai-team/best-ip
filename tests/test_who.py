from __future__ import annotations

import json
from pathlib import Path

import yaml

from backend.app.mihomo import MihomoProcess
from backend.app.sources import who

PROXY = "http://127.0.0.1:7890"


def _answer(monkeypatch, status, payload):
    async def fake_get(proxy_url, address):
        return status, json.dumps(payload).encode()

    monkeypatch.setattr(who, "_get_via_address", fake_get)


async def test_family_probe_returns_the_echo_of_that_family(monkeypatch):
    _answer(monkeypatch, 200, {"ip": "2001:db8::7", "cf": {"country": "TW", "asn": 3462}})
    result = await who.request_family(PROXY, "2001:db8:ffff::10", timeout_seconds=1)
    assert result["ok"] is True
    assert result["via_mihomo"] is True
    assert result["data"]["ip"] == "2001:db8::7"
    assert result["data"]["country_code"] == "TW"


async def test_family_probe_rejects_an_echo_of_the_other_family(monkeypatch):
    # 节点按 SNI 重新解析了域名、改走 IPv4：这不是它的 IPv6 出口。
    _answer(monkeypatch, 200, {"ip": "203.0.113.7"})
    result = await who.request_family(PROXY, "2001:db8:ffff::10", timeout_seconds=1)
    assert result["ok"] is False
    assert result["data"] is None
    assert result["error_type"] == "FamilyMismatch"
    assert result["skipped"] is True


async def test_family_probe_connection_failure_is_skipped(monkeypatch):
    async def refused(proxy_url, address):
        raise ConnectionResetError

    monkeypatch.setattr(who, "_get_via_address", refused)
    result = await who.request_family(PROXY, "192.0.2.10", timeout_seconds=1)
    assert result["ok"] is False
    assert result["skipped"] is True
    assert "IPv4" in result["error"]


def test_mihomo_routes_only_the_probe_addresses_through_the_node(tmp_path: Path):
    process = MihomoProcess(
        tmp_path / "mihomo",
        tmp_path / "work",
        [{"name": "node", "type": "ss", "server": "198.51.100.1", "port": 1}],
        family_probe_addresses=["192.0.2.10", "2001:db8:ffff::10"],
    )
    config = yaml.safe_load(process._write_config().read_text(encoding="utf-8"))
    rules = config["rules"]
    assert "IP-CIDR,192.0.2.10/32,BEST-IP,no-resolve" in rules
    assert "IP-CIDR6,2001:db8:ffff::10/128,BEST-IP,no-resolve" in rules
    assert rules[-1] == "MATCH,REJECT"
