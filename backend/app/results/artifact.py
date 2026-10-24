from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import yaml

from .validation import (
    ResultStoreError,
    encode_json,
    node_filename,
    summary,
    validate_manifest,
)


def build_manifest(
    job_id: str, job: dict[str, Any], records: list[dict[str, Any]], node_bytes: list[bytes]
) -> dict[str, Any]:
    """只负责汇总；所有校验集中在末尾的 validate_manifest（与发布前 build_artifact 同一道门）。"""

    expected_total = job.get("total")
    entries = []
    counts = {"success": 0, "partial": 0, "failed": 0, "complete": 0}
    for index, record in enumerate(records):
        status = record.get("status")
        if status in counts:
            counts[status] += 1
        if (record.get("completeness") or {}).get("complete") is True:
            counts["complete"] += 1
        raw = node_bytes[index]
        entries.append(
            {
                "index": index,
                "file": node_filename(index),
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "summary": summary(record),
            }
        )

    manifest = {
        "schema_version": 1,
        "job_id": job_id,
        "status": "completed",
        "created_at": job.get("created_at"),
        "finished_at": job.get("finished_at"),
        "total": expected_total,
        "skipped": job.get("skipped", 0),
        "completed": expected_total,
        "all_records_present": True,
        "complete": True,
        "execution_mode": job.get("execution_mode", "sequential"),
        "counts": counts,
        "records": entries,
    }
    validate_manifest(job_id, manifest, records, node_bytes)
    return manifest


def build_export(
    job_id: str, job: dict[str, Any], manifest: dict[str, Any], results: list[dict[str, Any]]
) -> dict[str, Any]:
    total = manifest["total"]
    counts = manifest["counts"]
    return {
        "id": job_id,
        "status": "completed",
        "message": job.get("message"),
        "created_at": manifest.get("created_at"),
        "finished_at": manifest.get("finished_at"),
        "total": total,
        "skipped": manifest.get("skipped", 0),
        "completed": total,
        "success_count": counts.get("success"),
        "partial_count": counts.get("partial"),
        "failed_count": counts.get("failed"),
        "current_node": None,
        "manifest_ready": True,
        "cleanup_confirmed": job.get("cleanup_confirmed") is True,
        "execution_mode": manifest.get("execution_mode"),
        "error": None,
        "results": results,
        "manifest": manifest,
    }


_CREDENTIAL_KEYS = {
    "auth",
    "auth-str",
    "authorization",
    "cookie",
    "obfs-password",
    "password",
    "pre-shared-key",
    "preshared-key",
    "private-key",
    "private-key-passphrase",
    "psk",
    "secret",
    "token",
    "username",
    "uuid",
}


def credential_values(content: bytes) -> set[str]:
    try:
        document = yaml.safe_load(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, yaml.YAMLError):
        return set()

    values: set[str] = set()

    def visit(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child_value in value.items():
                normalized_key = str(child_key).strip().lower().replace("_", "-")
                visit(child_value, normalized_key)
        elif isinstance(value, list):
            for child in value:
                visit(child, key)
        elif key in _CREDENTIAL_KEYS:
            text = str(value).strip()
            if text:
                values.add(text)

    visit(document)
    return values


def contains_forbidden_value(value: Any, forbidden_values: set[str]) -> bool:
    if isinstance(value, dict):
        return any(contains_forbidden_value(child, forbidden_values) for child in value.values())
    if isinstance(value, list):
        return any(contains_forbidden_value(child, forbidden_values) for child in value)
    if isinstance(value, bool) or value is None:
        return False
    text = str(value)
    return any(
        text == forbidden or (len(forbidden) >= 6 and forbidden in text)
        for forbidden in forbidden_values
    )


_STATIC_SUBSCRIPTION_PATH_SEGMENTS = {
    "api",
    "client",
    "clients",
    "clash",
    "config",
    "configs",
    "download",
    "feed",
    "feeds",
    "link",
    "links",
    "mihomo",
    "profile",
    "profiles",
    "subscribe",
    "subscription",
    "subscriptions",
    "yaml",
    "yml",
}


_STATIC_SUBSCRIPTION_QUERY_VALUES = {
    "auto",
    "base64",
    "clash",
    "false",
    "mihomo",
    "plain",
    "true",
    "yaml",
    "yml",
}


def _looks_like_subscription_query_token(value: str) -> bool:
    return (
        len(value) >= 6
        and not any(character.isspace() for character in value)
        and value.casefold() not in _STATIC_SUBSCRIPTION_QUERY_VALUES
    )


def _looks_like_subscription_path_token(value: str) -> bool:
    return (
        len(value) >= 6
        and not any(character.isspace() for character in value)
        and value.casefold() not in _STATIC_SUBSCRIPTION_PATH_SEGMENTS
    )


def subscription_url_values(url: str) -> set[str]:
    parsed = urlsplit(url)
    values: set[str] = set()
    for key, query_values in parse_qs(
        parsed.query,
        keep_blank_values=False,
    ).items():
        normalized_key = key.strip().lower().replace("_", "-")
        sensitive_key = any(
            marker in normalized_key
            for marker in ("auth", "key", "pass", "secret", "token", "uuid")
        )
        for query_value in query_values:
            value = query_value.strip()
            if value and (sensitive_key or _looks_like_subscription_query_token(value)):
                values.add(value)
    values.update(
        segment
        for segment in (unquote(item).strip() for item in parsed.path.split("/"))
        if _looks_like_subscription_path_token(segment)
    )
    return values


_DENY_KEYS = {
    "authorization",
    "auth_str",
    "cookie",
    "password",
    "passphrase",
    "private_key",
    "secret",
    "subscription_url",
    "token",
    "uuid",
}
_DENY_QUERY_KEYS = {"access_token", "auth", "key", "password", "secret", "token"}
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _walk_forbidden(value: Any, forbidden: set[str], path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).lower().replace("-", "_")
            if normalized in _DENY_KEYS:
                raise ResultStoreError(f"artifact 包含禁止字段：{path}.{key}")
            _walk_forbidden(child, forbidden, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _walk_forbidden(child, forbidden, f"{path}[{index}]")
        return
    if isinstance(value, str):
        try:
            parsed = urlsplit(value)
        except ValueError as exc:
            raise ResultStoreError("artifact URL 无效") from exc
        if parsed.username is not None or parsed.password is not None:
            raise ResultStoreError(f"artifact 包含带凭据 URL：{path}")
        query = parse_qs(parsed.query, keep_blank_values=True)
        if any(key.lower() in _DENY_QUERY_KEYS and values for key, values in query.items()):
            raise ResultStoreError(f"artifact 包含带凭据 URL：{path}")
        if contains_forbidden_value(value, forbidden):
            raise ResultStoreError(f"artifact 包含订阅凭据：{path}")


def build_artifact(
    exported: dict[str, Any],
    request_id: str,
    run_id: int,
    run_attempt: int,
    forbidden_values: set[str],
) -> tuple[bytes, bytes]:
    if not isinstance(request_id, str) or not _REQUEST_ID_RE.fullmatch(request_id):
        raise ResultStoreError("任务 ID 无效")
    if any(type(value) is not int or value < 1 for value in (run_id, run_attempt)):
        raise ResultStoreError("运行身份无效")
    manifest = exported.get("manifest")
    records = exported.get("results")
    if not isinstance(manifest, dict) or not isinstance(records, list):
        raise ResultStoreError("导出结果结构无效")
    validate_manifest(request_id, manifest, records, [encode_json(record) for record in records])
    counts = manifest["counts"]
    if (
        exported.get("id") != request_id
        or exported.get("status") != "completed"
        or exported.get("cleanup_confirmed") is not True
        or exported.get("manifest_ready") is not True
        or exported.get("total") != manifest["total"]
        or exported.get("completed") != manifest["completed"]
        or any(
            exported.get(f"{status}_count") != counts[status]
            for status in ("success", "partial", "failed")
        )
    ):
        raise ResultStoreError("导出结果完成事实或清理状态无效")
    _walk_forbidden(exported, forbidden_values)
    result_bytes = encode_json(exported)
    if b"subscription_url" in result_bytes or contains_forbidden_value(exported, forbidden_values):
        raise ResultStoreError("结果包含订阅凭据")
    status = {
        "schema_version": 1,
        "sanitized": True,
        "request_id": request_id,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "status": "completed",
        "usable": counts["failed"] == 0 and counts["partial"] == 0,
        "total": exported["total"],
        "completed": exported["completed"],
        "counts": counts,
        "result_sha256": hashlib.sha256(result_bytes).hexdigest(),
    }
    return result_bytes, encode_json(status)
