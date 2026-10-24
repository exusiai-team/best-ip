from __future__ import annotations

import gzip
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import download_mihomo  # noqa: E402

PAYLOAD = gzip.compress(b"#!/bin/sh\necho mihomo\n")


@pytest.fixture
def fake_release(monkeypatch):
    """替身 latest release：资产摘要由测试决定，下载写入固定字节。"""

    prefix, suffix, _ = download_mihomo.platform_asset()

    def install(digest):
        asset = {
            "name": f"{prefix}1.99.0{suffix}",
            "browser_download_url": "https://example.invalid/mihomo",
        }
        if digest is not None:
            asset["digest"] = digest
        monkeypatch.setattr(
            download_mihomo, "resolve_release", lambda tag: ({"tag_name": "v1.99.0"}, asset)
        )
        monkeypatch.setattr(
            download_mihomo, "download", lambda url, destination: destination.write_bytes(PAYLOAD)
        )

    return install


def _run(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["download_mihomo.py", "--output-dir", str(tmp_path)])
    download_mihomo.main()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="替身资产按 linux .gz 构造")
def test_latest_release_is_verified_against_asset_digest(monkeypatch, tmp_path, fake_release):
    fake_release("sha256:" + hashlib.sha256(PAYLOAD).hexdigest())
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    _run(monkeypatch, tmp_path)
    assert (tmp_path / "version.txt").read_text().startswith("v1.99.0\n")


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="替身资产按 linux .gz 构造")
def test_actions_refuses_mihomo_without_digest(monkeypatch, tmp_path, fake_release):
    fake_release(None)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    with pytest.raises(RuntimeError, match="摘要"):
        _run(monkeypatch, tmp_path)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="替身资产按 linux .gz 构造")
def test_digest_mismatch_is_rejected(monkeypatch, tmp_path, fake_release):
    fake_release("sha256:" + "0" * 64)
    with pytest.raises(RuntimeError):
        _run(monkeypatch, tmp_path)


def test_api_requests_carry_github_token(monkeypatch):
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(request, timeout):
        seen.update(request.headers)
        return Response()

    monkeypatch.setattr(download_mihomo.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_example")
    download_mihomo.github_json("https://api.github.com/repos/x/y/releases/latest")
    assert seen["Authorization"] == "Bearer ghs_example"
