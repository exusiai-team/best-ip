from __future__ import annotations

import asyncio

import yaml

from backend.app.mihomo import MihomoProcess
from backend.app.sources.ip_cache import IpCache
from backend.app.sources.registry import PROXIED_HOSTS, SOURCES


def _answer(ok: bool, proxy: str):
    calls: list[str] = []

    async def request():
        calls.append(proxy)
        await asyncio.sleep(0.05)
        return {"ok": ok, "proxy_url": proxy}

    return request, calls


async def test_same_ip_is_asked_once_and_marked_shared():
    cache = IpCache()
    first, first_calls = _answer(True, "http://127.0.0.1:1")
    second, second_calls = _answer(True, "http://127.0.0.1:2")
    owner, waiter = await asyncio.gather(
        cache.fetch("ipqs", "203.0.113.1", first), cache.fetch("ipqs", "203.0.113.1", second)
    )
    assert first_calls == ["http://127.0.0.1:1"] and second_calls == []
    assert "shared" not in owner
    # 复用来的结果保留真正发出请求的节点代理地址。
    assert waiter == {"ok": True, "proxy_url": "http://127.0.0.1:1", "shared": True}
    assert cache.hits == 1


async def test_failed_answer_is_not_reused():
    cache = IpCache()
    failing, _ = _answer(False, "http://127.0.0.1:1")
    working, working_calls = _answer(True, "http://127.0.0.1:2")
    owner, waiter = await asyncio.gather(
        cache.fetch("ipqs", "203.0.113.1", failing), cache.fetch("ipqs", "203.0.113.1", working)
    )
    assert owner["ok"] is False
    assert waiter == {"ok": True, "proxy_url": "http://127.0.0.1:2"}
    assert working_calls == ["http://127.0.0.1:2"]
    # 失败的答案不留在表里：之后再问会重新发。
    again, again_calls = _answer(True, "http://127.0.0.1:3")
    assert (await cache.fetch("ipqs", "203.0.113.1", again))["proxy_url"] == "http://127.0.0.1:3"
    assert again_calls


async def test_cancelled_owner_hands_over_to_the_waiter():
    cache = IpCache()
    slow, _ = _answer(True, "http://127.0.0.1:1")
    other, other_calls = _answer(True, "http://127.0.0.1:2")
    owner = asyncio.create_task(cache.fetch("lookup", "203.0.113.1", slow))
    await asyncio.sleep(0)
    waiter = asyncio.create_task(cache.fetch("lookup", "203.0.113.1", other))
    await asyncio.sleep(0.01)
    owner.cancel()
    assert (await waiter)["proxy_url"] == "http://127.0.0.1:2"
    assert other_calls == ["http://127.0.0.1:2"]


async def test_different_ip_or_source_is_not_shared():
    cache = IpCache()
    a, a_calls = _answer(True, "a")
    b, b_calls = _answer(True, "b")
    c, c_calls = _answer(True, "c")
    await asyncio.gather(
        cache.fetch("ipqs", "203.0.113.1", a),
        cache.fetch("ipqs", "203.0.113.2", b),
        cache.fetch("ipure", "203.0.113.1", c),
    )
    assert (a_calls, b_calls, c_calls) == (["a"], ["b"], ["c"])


def test_caller_specific_sources_are_never_shared():
    shareable = {source.name for source in SOURCES if source.shareable}
    # trace / who / ipx 问的是「这条连接」，换个节点问答案就不同。
    assert not shareable & {"trace", "who", "ipx"}


def test_mihomo_allowlist_covers_every_registered_host(tmp_path):
    proxy = {"name": "node", "type": "ss", "server": "203.0.113.9", "port": 1}
    process = MihomoProcess(tmp_path / "mihomo", tmp_path / "work", [proxy])
    config = yaml.safe_load(process._write_config().read_text(encoding="utf-8"))
    assert set(PROXIED_HOSTS) == {
        "ip.net.coffee", "who.1ms.dog", "ipx.al", "ipqs.1ms.dog", "ipure.dev",
    }
    for host in PROXIED_HOSTS:
        assert f"DOMAIN,{host},{MihomoProcess.group_name}" in config["rules"]
    assert config["rules"][-1] == "MATCH,REJECT"
