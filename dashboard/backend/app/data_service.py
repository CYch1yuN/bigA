"""UI-G2 只读数据聚合。

只读取仓库内固定的 state/reports 产物，不接受用户提供的文件路径，避免路径穿越。
缺少文件时返回明确的 availability 信息，不伪造业务数据。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _read_json(path: Path, default: Any) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return default


def _latest_dir(parent: Path) -> Path | None:
    if not parent.is_dir():
        return None
    dirs = sorted((p for p in parent.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)
    return dirs[0] if dirs else None


def _latest_daily_artifact(reports_root: Path) -> Path | None:
    return _latest_dir(reports_root / "phase-4" / "daily")


def _run_history(state_root: Path, limit: int = 30) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    base = state_root / "automation" / "runs"
    if not base.is_dir():
        return runs
    files = sorted(base.glob("*/*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in files[:limit]:
        item = _read_json(path, None)
        if isinstance(item, dict):
            runs.append(item)
    return runs


def build_dashboard_snapshot(project_root: Path) -> dict[str, Any]:
    """构建 UI-G2 聚合快照；所有数据均为只读研究/模拟产物。"""
    root = project_root.resolve()
    state_root = root / "state"
    reports_root = root / "reports"
    latest_daily = _latest_daily_artifact(reports_root)

    latest_run = _read_json(state_root / "automation" / "latest-daily.json", None)
    gate4b = _read_json(
        state_root / "automation" / "gate4b" / "gate4b-track-summary.json", None
    )

    def daily_json(name: str, default: Any) -> Any:
        return _read_json(latest_daily / name, default) if latest_daily else default

    accounts = daily_json("accounts.json", None)
    signals = daily_json("signals.json", None)
    orders = daily_json("simulated-orders.json", None)
    quality = daily_json("quality-summary.json", None)
    artifact_run = daily_json("run-summary.json", None)

    source_times = [
        p.stat().st_mtime
        for p in (
            state_root / "automation" / "latest-daily.json",
            state_root / "automation" / "gate4b" / "gate4b-track-summary.json",
            latest_daily / "accounts.json" if latest_daily else None,
            latest_daily / "signals.json" if latest_daily else None,
            latest_daily / "quality-summary.json" if latest_daily else None,
        )
        if p is not None and p.is_file()
    ]
    data_timestamp = (
        datetime.fromtimestamp(max(source_times), tz=timezone.utc).isoformat()
        if source_times
        else None
    )

    return {
        "ok": True,
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_timestamp": data_timestamp,
        "mode": "research_only",
        "live_trading": False,
        "broker_connected": False,
        "availability": {
            "latest_run": latest_run is not None,
            "gate4b": gate4b is not None,
            "accounts": accounts is not None,
            "signals": signals is not None,
            "orders": orders is not None,
            "quality": quality is not None,
        },
        "operations": {
            "available": True,
            "verify": True,
            "daily": True,
            "weekly": True,
            "rerun": True,
            "backfill": True,
            "note": "本地自动化 CLI 真实执行；仅模拟账户，不涉及实盘。",
        },
        "latest_run": latest_run,
        "artifact_run": artifact_run,
        "gate4b": gate4b,
        "accounts": accounts,
        "signals": signals,
        "orders": orders,
        "quality": quality,
        "run_history": _run_history(state_root),
        "artifact_date": latest_daily.name if latest_daily else None,
        "disclaimer": "只读展示研究信号与模拟账户；不连接券商，不涉及真实资金。",
    }
