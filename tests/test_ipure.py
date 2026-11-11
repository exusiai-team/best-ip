from __future__ import annotations

import httpx

from backend.app.sources.ipure import IPURE_NOT_INDEXED, _response_error


def _error(status: int, payload: dict) -> tuple[str, str, bool]:
    return _response_error(httpx.Response(status, json=payload), payload)


def test_unindexed_ip_is_reported_as_not_indexed():
    # 实测（2026-11-11）：未收录 IP 走匿名接口返回 401 invalid_api_key。
    message, error_type, skipped = _error(
        401, {"code": "invalid_api_key", "reportUrl": "https://ipure.dev/ip/203.0.113.7"}
    )
    assert (message, error_type, skipped) == ("IPure 未收录该 IP", IPURE_NOT_INDEXED, True)


def test_other_401_stays_an_http_error():
    message, error_type, skipped = _error(401, {"code": "something_else"})
    assert (message, error_type, skipped) == ("HTTP 401", "HTTPStatusError", False)
