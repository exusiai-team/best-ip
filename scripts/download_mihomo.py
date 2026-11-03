from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

REPOSITORY = "MetaCubeX/mihomo"
ROOT_DIR = Path(__file__).resolve().parents[1]


def platform_asset() -> tuple[str, str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if machine in {"amd64", "x86_64"}:
        architecture = "amd64"
    elif machine in {"arm64", "aarch64"}:
        architecture = "arm64"
    else:
        raise RuntimeError(f"暂不支持的 CPU 架构：{machine}")
    if system == "windows":
        return f"mihomo-windows-{architecture}-v", ".zip", "mihomo.exe"
    if system == "linux":
        return f"mihomo-linux-{architecture}-v", ".gz", "mihomo"
    if system == "darwin":
        return f"mihomo-darwin-{architecture}-v", ".gz", "mihomo"
    raise RuntimeError(f"暂不支持的系统：{system}")


def github_json(url: str) -> dict[str, Any]:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "best-ip-installer",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    # Actions 里带上 GITHUB_TOKEN：匿名请求按 IP 限流，共享 runner 上取 latest 容易撞 403/429，
    # 而 latest 没有直链可回退。
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def select_asset(release: dict[str, Any]) -> dict[str, Any]:
    prefix, suffix, _ = platform_asset()
    candidates = [
        asset
        for asset in release.get("assets", [])
        if asset.get("name", "").startswith(prefix) and asset.get("name", "").endswith(suffix)
    ]
    standard_candidates = [
        asset
        for asset in candidates
        if "-" not in asset["name"][len(prefix) : -len(suffix)]
    ]
    if len(standard_candidates) != 1:
        names = ", ".join(asset.get("name", "") for asset in standard_candidates) or "无"
        raise RuntimeError(f"无法唯一确定标准 Mihomo 资产，候选：{names}")
    return standard_candidates[0]


def download(url: str, destination: Path) -> None:
    request = urllib.request.Request(url, headers={"User-Agent": "best-ip-installer"})
    with urllib.request.urlopen(request, timeout=120) as response, destination.open("wb") as output:
        shutil.copyfileobj(response, output)


def resolve_release(tag: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    release_url = (
        f"https://api.github.com/repos/{REPOSITORY}/releases/tags/{tag}"
        if tag
        else f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
    )
    try:
        release = github_json(release_url)
    except urllib.error.HTTPError as error:
        if error.code not in {403, 429} or not tag or not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
            raise
        prefix, suffix, _ = platform_asset()
        name = f"{prefix}{tag.removeprefix('v')}{suffix}"
        print(f"GitHub API 返回 {error.code}，改用固定版本 {tag} 的官方 Release 直链。")
        return {"tag_name": tag}, {
            "name": name,
            "browser_download_url": (
                f"https://github.com/{REPOSITORY}/releases/download/{tag}/{name}"
            ),
        }
    return release, select_asset(release)


def verify_digest(path: Path, digest: str | None) -> str:
    with path.open("rb") as archive:
        actual = hashlib.file_digest(archive, "sha256").hexdigest()
    if digest:
        expected = digest.removeprefix("sha256:").lower()
        if actual != expected:
            raise RuntimeError(f"SHA-256 校验失败：期望 {expected}，实际 {actual}")
    return actual


def extract(archive: Path, destination: Path) -> None:
    _, suffix, executable_name = platform_asset()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if suffix == ".zip":
        with zipfile.ZipFile(archive) as bundle:
            members = [name for name in bundle.namelist() if name.lower().endswith(".exe")]
            if len(members) != 1:
                raise RuntimeError("Mihomo ZIP 中未找到唯一可执行文件")
            with bundle.open(members[0]) as source, destination.open("wb") as output:
                shutil.copyfileobj(source, output)
    else:
        with gzip.open(archive, "rb") as source, destination.open("wb") as output:
            shutil.copyfileobj(source, output)
    if executable_name != "mihomo.exe":
        destination.chmod(destination.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def cached_archive(cache_dir: Path | None, digest: str | None) -> Path | None:
    if cache_dir is None or not digest:
        return None
    _, suffix, _ = platform_asset()
    normalized = digest.removeprefix("sha256:").lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise RuntimeError("Mihomo 归档 SHA-256 格式无效")
    return cache_dir.resolve() / f"{normalized}{suffix}"


def main() -> None:
    parser = argparse.ArgumentParser(description="下载并校验 Mihomo 核心")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT_DIR / "runtime" / "mihomo",
        help="核心输出目录",
    )
    parser.add_argument(
        "--tag",
        default=os.getenv("BEST_IP_MIHOMO_TAG"),
        help="指定 release tag；未提供时使用 latest",
    )
    parser.add_argument(
        "--archive-sha256",
        default=os.getenv("BEST_IP_MIHOMO_ARCHIVE_SHA256"),
        help="可选：额外核对的归档 SHA-256；未提供时用 GitHub release 给出的资产摘要",
    )
    parser.add_argument(
        "--archive-cache-dir",
        type=Path,
        help="可选的已校验归档缓存目录；缓存命中时跳过 GitHub release 请求和下载",
    )
    args = parser.parse_args()

    _, _, executable_name = platform_asset()
    destination = args.output_dir.resolve() / executable_name
    args.output_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cached_archive(args.archive_cache_dir, args.archive_sha256)
    if cache_path is not None and cache_path.is_file():
        digest = verify_digest(cache_path, args.archive_sha256)
        extract(cache_path, destination)
        (args.output_dir / "version.txt").write_text(
            f"{args.tag or 'cached'}\nsha256:{digest}\n{cache_path.name}\n",
            encoding="utf-8",
        )
        print(f"使用已校验缓存：{cache_path}")
        print(f"已安装：{destination}")
        return

    release, asset = resolve_release(args.tag)

    file_descriptor, temporary_name = tempfile.mkstemp(suffix=Path(asset["name"]).suffix)
    os.close(file_descriptor)
    archive = Path(temporary_name)
    try:
        print(f"下载 {asset['name']} ({release['tag_name']})")
        download(asset["browser_download_url"], archive)
        expected_digest = args.archive_sha256 or asset.get("digest")
        if not expected_digest:
            if os.getenv("GITHUB_ACTIONS") == "true":
                # 生产扫描会把订阅交给这个二进制，没有可信摘要就不装。
                raise RuntimeError("GitHub release 没有给出资产摘要，拒绝安装未校验的 Mihomo")
            print("未提供可信 SHA-256；本次仅通过官方 HTTPS 下载，输出摘要不代表真实性校验。")
        digest = verify_digest(archive, expected_digest)
        if cache_path is not None:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary_cache = cache_path.with_name(
                f".{cache_path.name}.{os.getpid()}.tmp"
            )
            try:
                shutil.copyfile(archive, temporary_cache)
                os.replace(temporary_cache, cache_path)
            finally:
                temporary_cache.unlink(missing_ok=True)
        extract(archive, destination)
    finally:
        archive.unlink(missing_ok=True)

    (args.output_dir / "version.txt").write_text(
        f"{release['tag_name']}\nsha256:{digest}\n{asset['name']}\n", encoding="utf-8"
    )
    print(f"已安装：{destination}")
    print(f"SHA-256：{digest}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, zipfile.BadZipFile) as error:
        raise SystemExit(f"Mihomo 安装失败：{error}") from None
