"""数据版本清单生成模块。

记录：数据源、抓取范围、行数、文件 SHA-256、配置摘要、代码提交号、schema 版本。
保证报告可追溯到配置、数据版本与代码版本。
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import AppConfig
from .storage import file_sha256


def get_code_commit() -> str:
    """获取当前 git 提交号；不可用时返回 ``no-git``。"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return "no-git"


def build_manifest(
    source: str,
    symbol: str,
    start_date: str,
    end_date: str,
    row_count: int,
    files: dict[str, str | Path],
    config: AppConfig,
    schema_version: str,
    content_hash_value: str | None = None,
    code_commit: str | None = None,
) -> dict[str, Any]:
    """构建数据版本清单字典。

    参数:
        files: 文件名 -> 文件路径，用于计算 SHA-256。
    """
    file_hashes: dict[str, str] = {}
    for name, path in files.items():
        p = Path(path)
        if p.exists():
            file_hashes[name] = file_sha256(p)
        else:
            file_hashes[name] = "missing"

    manifest: dict[str, Any] = {
        "source": source,
        "symbol": symbol,
        "fetch_range": {"start_date": start_date, "end_date": end_date},
        "row_count": row_count,
        "content_hash": content_hash_value,
        "files": file_hashes,
        "config_summary": {
            "providers": {
                "primary": config.providers.primary,
                "fallback": config.providers.fallback,
            },
            "schema": {
                "daily_quote_version": config.schema_versions.daily_quote_version,
                "security_master_version": config.schema_versions.security_master_version,
            },
            "quality_rules": {
                name: str(config.quality.get(name)) for name in config.quality
            },
        },
        "code_commit": code_commit if code_commit is not None else get_code_commit(),
        "schema_version": schema_version,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    return manifest


def write_manifest(manifest: dict[str, Any], path: str | Path) -> Path:
    """写清单为 JSON 文件。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, default=str)
    return p


def read_manifest(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as f:
        return json.load(f)


__all__ = [
    "build_manifest",
    "write_manifest",
    "read_manifest",
    "get_code_commit",
]
