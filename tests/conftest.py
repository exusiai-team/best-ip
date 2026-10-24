from __future__ import annotations

import copy
from typing import Any

import pytest

from backend.app.scan.errors import _failed_node

JOB_ID = "req-test-0001"


def success_record(index: int, *, job_id: str = JOB_ID, name: str | None = None) -> dict[str, Any]:
    """一条能过 validate_node 的成功记录：只放契约要求的字段，取值都是文档网段。"""

    node = name or f"node-{index}"
    return {
        "schema_version": 1,
        "job_id": job_id,
        "node_index": index,
        "node": node,
        "type": "ss",
        "selected_proxy": node,
        "status": "success",
        "error": None,
        "transport_error": None,
        "started_at": "2026-10-24T00:00:00Z",
        "finished_at": "2026-10-24T00:00:01Z",
        "exit_ip": f"203.0.113.{index + 1}",
        "elapsed_ms": 1000 + index,
        "score": 80,
        "ipure_scores": {
            "total": 80,
            "ai": 90,
            "social": -1,
            "streaming": None,
            "gaming": 70,
            "ecommerce": 60,
            "email": 100,
        },
        "proxy_evidence": {
            "transport": "workspace_mihomo_mixed_port",
            "proxy_url": "http://127.0.0.1:17890",
            "mihomo_instance": "instance-1",
            "selected_proxy": node,
            "selection_confirmed": True,
            "target_origin": "https://ip.net.coffee",
            "trust_env": False,
            "ipure_via_direct_fallback": False,
        },
        "completeness": {
            "complete": True,
            "checks": {
                "page_received": True,
                "trace_received": True,
                "exit_ip_valid": True,
                "lookup_received": True,
                "lookup_matches_trace": True,
            },
        },
        "requests": {"ipure": {"direct_fallback": False, "via_mihomo": True}},
        "coffee": {},
    }


def failed_record(index: int, *, job_id: str = JOB_ID) -> dict[str, Any]:
    return _failed_node(job_id, index, f"node-{index}", "ss", RuntimeError("连接被拒绝"))


@pytest.fixture
def make_success():
    return lambda index, **kwargs: copy.deepcopy(success_record(index, **kwargs))


@pytest.fixture
def make_failed():
    return lambda index, **kwargs: failed_record(index, **kwargs)
