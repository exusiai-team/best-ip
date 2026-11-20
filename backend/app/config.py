from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def validate_local_frontend_origin(value: str | None) -> str | None:
    origin = str(value or "").strip()
    if not origin:
        return None
    parsed = urlsplit(origin)
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("BEST_IP_LOCAL_FRONTEND_ORIGIN 端口无效") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "BEST_IP_LOCAL_FRONTEND_ORIGIN 必须是带端口的 127.0.0.1 HTTP origin"
        )
    return f"http://127.0.0.1:{port}"


SCAN_PROFILES = frozenset({"full", "classify"})

ROOT_DIR = Path(__file__).resolve().parents[2]
RUNTIME_DIR = ROOT_DIR / "runtime"
JOBS_DIR = RUNTIME_DIR / "jobs"
RESULTS_DIR = RUNTIME_DIR / "results"


def _default_mihomo_path() -> Path:
    filename = "mihomo.exe" if platform.system() == "Windows" else "mihomo"
    return RUNTIME_DIR / "mihomo" / filename


@dataclass(frozen=True, slots=True)
class Settings:
    mihomo_path: Path = Path(os.getenv("BEST_IP_MIHOMO_PATH", str(_default_mihomo_path())))
    local_dev: bool = _env_flag("BEST_IP_LOCAL_DEV")
    local_frontend_origin: str | None = validate_local_frontend_origin(
        os.getenv("BEST_IP_LOCAL_FRONTEND_ORIGIN")
    )
    subscription_max_bytes: int = int(os.getenv("BEST_IP_SUBSCRIPTION_MAX_BYTES", 5 * 1024 * 1024))
    max_nodes: int = int(os.getenv("BEST_IP_MAX_NODES", "500"))
    max_parallel_jobs: int = int(os.getenv("BEST_IP_MAX_PARALLEL_JOBS", "2"))
    # 并发节点数默认取 CPU 核数（夹在 8..16）。实测依据：同一条 25 节点订阅在相同时间窗内
    # 交错对照 8 vs 16 并发，16 并发把墙钟从 50.9s/70.9s 压到 30.4s/31.2s（失败节点数相同的
    # 那两轮拿分节点数都是 13），而 8 并发时失败节点只能排在成功节点后面逐个占槽位，尾巴更长。
    max_parallel_nodes: int = int(
        os.getenv(
            "BEST_IP_MAX_PARALLEL_NODES",
            str(min(16, max(8, os.cpu_count() or 8))),
        )
    )
    max_node_attempts: int = int(os.getenv("BEST_IP_MAX_NODE_ATTEMPTS", "3"))
    node_retry_backoff_ms: int = int(os.getenv("BEST_IP_NODE_RETRY_BACKOFF_MS", "500"))
    outbound_interface: str | None = (
        os.getenv("BEST_IP_OUTBOUND_INTERFACE", "").strip() or None
    )
    page_timeout_ms: int = int(os.getenv("BEST_IP_PAGE_TIMEOUT_MS", "45000"))
    subscription_timeout_seconds: float = float(
        os.getenv("BEST_IP_SUBSCRIPTION_TIMEOUT_SECONDS", "30")
    )
    # full：现有完整扫描。classify：给 /api/v1/classify 的轻量档，不测全球 Ping、端口、连通检测，
    # 也不做双栈第二出口；出口 IP、归属、接入、原生、各家分数与 GPT 探测照旧
    # （见 backend/app/classify.py）。
    scan_profile: str = os.getenv("BEST_IP_SCAN_PROFILE", "full").strip().lower() or "full"

    def __post_init__(self) -> None:
        if self.scan_profile not in SCAN_PROFILES:
            raise ValueError("scan_profile 只能是 full 或 classify")
        for name in (
            "subscription_max_bytes",
            "max_nodes",
            "max_parallel_jobs",
            "max_parallel_nodes",
            "max_node_attempts",
            "page_timeout_ms",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} 必须大于 0")
        if self.node_retry_backoff_ms < 0:
            raise ValueError("node_retry_backoff_ms 不能小于 0")
        if self.subscription_timeout_seconds <= 0:
            raise ValueError("subscription_timeout_seconds 必须大于 0")


settings = Settings()

