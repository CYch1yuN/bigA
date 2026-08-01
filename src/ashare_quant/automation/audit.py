"""FR-23 审计产物：把一次运行固化为可审计、可复算、确定性的产物集。

每次成功运行，除既有 ``run.json`` / ``daily-report.md`` / 数据 JSON 之外，
再落盘以下审计产物（固定列序、稳定排序、字节确定性）::

    run-summary.json      运行摘要（终态/退出码/哈希/计数）
    run-report.md         人读报告的规范名副本（与 daily-report.md 同内容）
    manifest.json         全部输入/输出文件清单 + SHA-256 + 来源哈希
    signals.parquet       研究信号（固定列序）
    orders.parquet        模拟订单（含 order_id / run_id）
    fills.parquet         成交（仅 FILLED，与 orders 经 order_id 关联）
    account-snapshot.json 账户快照（账户 + 权益 + 观察窗口）
    equity.parquet        账户逐日权益曲线
    quality-summary.json  数据质量闸门摘要

确定性保证：
- Parquet 列序固定、行序稳定排序、金额一律以字符串（Decimal 原文）保存；
- ``order_id`` 由订单唯一键确定性派生（双跑一致，orders/fills 关联键）；
- ``output_hash`` 由全部产物文件的 SHA-256 排序拼接再哈希得到（与时间无关）；
- ``manifest.generated_at`` 是唯一允许随运行时间变化的字段。
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import pandas as pd

from .config import AutomationConfig
from .models import (
    RunRecord,
    SimulatedAccountState,
    SimulatedOrderRecord,
    TaskType,
)
from .reporting import ResultPaths, write_json_artifact
from .state import atomic_write_text

__all__ = [
    "SCHEMA_VERSION",
    "SIGNAL_COLUMNS",
    "ORDER_COLUMNS",
    "FILL_COLUMNS",
    "EQUITY_COLUMNS",
    "sha256_bytes",
    "sha256_file",
    "order_id_for",
    "write_parquet",
    "build_run_summary",
    "write_manifest",
    "verify_manifest",
    "write_audit_artifacts",
]

SCHEMA_VERSION = 1

#: 固定列顺序（Parquet 列序稳定，保证双跑字节一致）。
SIGNAL_COLUMNS = [
    "run_id", "as_of_date", "track", "symbol", "side", "quantity",
    "signal_date", "signal_hash", "reason", "simulated",
]
ORDER_COLUMNS = [
    "order_id", "run_id", "account_id", "strategy_track", "signal_date",
    "fill_date", "symbol", "side", "quantity", "signal_hash", "status",
    "reject_reason", "reason", "fill_price", "raw_open_price", "commission",
    "stamp_duty", "transfer_fee", "total_cost", "cash_change", "turnover",
    "simulated",
]
FILL_COLUMNS = [
    "order_id", "run_id", "account_id", "strategy_track", "symbol", "side",
    "quantity", "fill_price", "commission", "stamp_duty", "transfer_fee",
    "total_cost", "cash_change", "fill_date",
]
EQUITY_COLUMNS = [
    "run_id", "as_of_date", "account_id", "cash", "position_value",
    "total_equity", "positions", "observation_days", "eligibility_status",
]


# ---------------------------------------------------------------------- #
# 哈希与原子写
# ---------------------------------------------------------------------- #


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(Path(path).read_bytes())


def atomic_write_bytes(path: Path, data: bytes) -> Path:
    """字节原子写：先写临时文件再 ``os.replace``。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, p)
    return p


def write_parquet(df: pd.DataFrame, path: Path) -> Path:
    """固定内容写 Parquet（原子替换，避免半成品被读到）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    df.to_parquet(tmp, engine="pyarrow", index=False)
    os.replace(tmp, p)
    return p


# ---------------------------------------------------------------------- #
# 订单与成交的关联键
# ---------------------------------------------------------------------- #


def order_id_for(order: Any) -> str:
    """订单确定性子标识（orders 与 fills 经它关联，双跑一致）。

    同一逻辑订单（同一账户 + 信号日 + 标的 + 方向 + 轨道 + 信号哈希）
    无论以对象还是字典出现，都派生出同一个 ``order_id``。
    """
    if isinstance(order, SimulatedOrderRecord):
        key = order.unique_key
    else:
        d = dict(order)
        parts = [
            str(d.get("account_id", "")),
            str(d.get("signal_date", "")),
            str(d.get("symbol", "")),
            str(d.get("side", "")),
            str(d.get("strategy_track", "")),
            str(d.get("signal_hash", "")),
        ]
        key = "|".join(parts)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


# ---------------------------------------------------------------------- #
# 摘要 / 清单
# ---------------------------------------------------------------------- #


def _counts(
    *,
    signals: int,
    orders: Sequence[Any],
    accounts: Sequence[SimulatedAccountState],
    observation: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    filled = sum(
        1
        for o in orders
        if (o.status if isinstance(o, SimulatedOrderRecord) else o.get("status"))
        == "FILLED"
    )
    rejected = sum(
        1
        for o in orders
        if (o.status if isinstance(o, SimulatedOrderRecord) else o.get("status"))
        == "REJECTED"
    )
    duplicates = sum(
        1
        for o in orders
        if (o.status if isinstance(o, SimulatedOrderRecord) else o.get("status"))
        == "DUPLICATE"
    )
    return {
        "signals": int(signals),
        "orders": len(list(orders)),
        "filled": filled,
        "rejected": rejected,
        "duplicates": duplicates,
        "accounts": len(list(accounts)),
        "observation_days": {
            a.account_id: int(a.observation_days) for a in accounts
        },
        "observation": list(observation),
    }


def build_run_summary(
    *,
    record: RunRecord,
    output_hash: str,
    signals: int,
    orders: Sequence[Any],
    accounts: Sequence[SimulatedAccountState],
    observation: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """运行摘要（独立于 run.json 的紧凑审计视图）。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": record.run_id,
        "task_type": record.task_type.value,
        "as_of_date": record.as_of_date.isoformat(),
        "state": record.state.value,
        "exit_code": record.exit_code,
        "code_commit": record.code_commit,
        "config_hash": record.config_hash,
        "input_hash": record.input_hash,
        "output_hash": output_hash,
        "started_at": (
            record.started_at.isoformat(timespec="seconds")
            if record.started_at
            else None
        ),
        "finished_at": (
            record.finished_at.isoformat(timespec="seconds")
            if record.finished_at
            else None
        ),
        "duration_seconds": record.duration_seconds,
        "attempt": int(record.attempt),
        "message": record.message,
        "counts": _counts(
            signals=signals,
            orders=orders,
            accounts=accounts,
            observation=observation,
        ),
        "simulated": True,
        "live_trading": False,
    }


def _manifest_files(
    run_dir: Path, *, extra_files: Iterable[Path] = ()
) -> list[Path]:
    """run 目录下全部产物（排除 manifest 自身）+ 额外文件，稳定排序。"""
    files = [p for p in run_dir.rglob("*") if p.is_file()]
    files.extend(Path(f) for f in extra_files if Path(f).is_file())
    files = sorted(set(files), key=lambda p: str(p).replace("\\", "/"))
    return files


def build_manifest(
    *,
    record: RunRecord,
    config: AutomationConfig,
    run_dir: Path,
    output_hash: str,
    extra_files: Iterable[Path] = (),
    generated_at: Optional[str] = None,
) -> dict[str, Any]:
    """产物清单：全部输入/输出文件 + SHA-256 + 来源哈希。"""
    files = _manifest_files(run_dir, extra_files=extra_files)
    entries = []
    for p in files:
        try:
            rel = str(Path(p).resolve().relative_to(config.base_dir)).replace(
                "\\", "/"
            )
        except ValueError:
            rel = str(p).replace("\\", "/")
        entries.append(
            {"path": rel, "sha256": sha256_file(p), "bytes": Path(p).stat().st_size}
        )
    entries.sort(key=lambda e: e["path"])
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": record.run_id,
        "task_type": record.task_type.value,
        "as_of_date": record.as_of_date.isoformat(),
        "code_commit": record.code_commit,
        "config_hash": record.config_hash,
        "input_hash": record.input_hash,
        "output_hash": output_hash,
        "generated_at": generated_at,
        "count": len(entries),
        "files": entries,
    }


def output_hash_for(entries: list[dict[str, Any]]) -> str:
    """由全部产物哈希排序拼接再哈希得到（与时间无关、确定性）。"""
    payload = "".join(e["sha256"] for e in sorted(entries, key=lambda e: e["path"]))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def verify_manifest(manifest_path: Path, *, config: AutomationConfig) -> dict[str, Any]:
    """重新校验 manifest：逐文件重算 SHA-256 并比对；不一致即抛错。"""
    import json

    manifest_path = Path(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        p = config.base_dir / entry["path"]
        if not p.exists():
            raise AssertionError(f"manifest 列出的文件不存在: {entry['path']}")
        actual = sha256_file(p)
        if actual != entry["sha256"]:
            raise AssertionError(
                f"manifest 校验失败: {entry['path']} sha256={actual} != {entry['sha256']}"
            )
    return manifest


# ---------------------------------------------------------------------- #
# 主入口
# ---------------------------------------------------------------------- #


def _as_signal_dict(s: Any) -> dict[str, Any]:
    if isinstance(s, dict):
        return dict(s)
    to_dict = getattr(s, "to_dict", None)
    if callable(to_dict):
        return dict(to_dict())
    return {}


def _as_order_dict(o: Any) -> dict[str, Any]:
    if isinstance(o, SimulatedOrderRecord):
        d = o.to_dict()
        d["order_id"] = o.order_id
        return d
    d = dict(o)
    d["order_id"] = order_id_for(d)
    return d


def _signals_frame(
    signals: Sequence[Any], *, run_id: str, as_of_date: str
) -> pd.DataFrame:
    rows = []
    for s in signals:
        d = _as_signal_dict(s)
        rows.append(
            {
                "run_id": run_id,
                "as_of_date": as_of_date,
                "track": str(d.get("track", d.get("strategy_track", ""))),
                "symbol": str(d.get("symbol", "")),
                "side": str(d.get("side", "")),
                "quantity": int(d.get("quantity", 0) or 0),
                "signal_date": str(d.get("signal_date", "")),
                "signal_hash": str(d.get("signal_hash", "")),
                "reason": str(d.get("reason", "")),
                "simulated": bool(d.get("simulated", True)),
            }
        )
    df = pd.DataFrame(rows, columns=SIGNAL_COLUMNS)
    if not df.empty:
        df = df.sort_values(
            ["track", "signal_date", "symbol", "side", "quantity"],
            kind="stable",
        ).reset_index(drop=True)
    return df


def _orders_frame(orders: Sequence[Any], *, run_id: str) -> pd.DataFrame:
    rows = []
    for o in orders:
        d = _as_order_dict(o)
        rows.append(
            {
                "order_id": str(d.get("order_id", "")),
                "run_id": run_id,
                "account_id": str(d.get("account_id", "")),
                "strategy_track": str(d.get("strategy_track", "")),
                "signal_date": str(d.get("signal_date", "")),
                "fill_date": (
                    str(d["fill_date"]) if d.get("fill_date") else None
                ),
                "symbol": str(d.get("symbol", "")),
                "side": str(d.get("side", "")),
                "quantity": int(d.get("quantity", 0) or 0),
                "signal_hash": str(d.get("signal_hash", "")),
                "status": str(d.get("status", "")),
                "reject_reason": (
                    str(d["reject_reason"]) if d.get("reject_reason") else None
                ),
                "reason": str(d.get("reason", "")),
                "fill_price": (
                    str(d["fill_price"]) if d.get("fill_price") is not None else None
                ),
                "raw_open_price": (
                    str(d["raw_open_price"])
                    if d.get("raw_open_price") is not None
                    else None
                ),
                "commission": str(d.get("commission", "0")),
                "stamp_duty": str(d.get("stamp_duty", "0")),
                "transfer_fee": str(d.get("transfer_fee", "0")),
                "total_cost": str(d.get("total_cost", "0")),
                "cash_change": str(d.get("cash_change", "0")),
                "turnover": str(d.get("turnover", "0")),
                "simulated": bool(d.get("simulated", True)),
            }
        )
    df = pd.DataFrame(rows, columns=ORDER_COLUMNS)
    if not df.empty:
        df = df.sort_values(
            ["account_id", "signal_date", "symbol", "side", "order_id"],
            kind="stable",
        ).reset_index(drop=True)
    return df


def _fills_frame(orders: Sequence[Any], *, run_id: str) -> pd.DataFrame:
    rows = []
    for o in orders:
        d = _as_order_dict(o)
        if str(d.get("status", "")) != "FILLED":
            continue
        rows.append(
            {
                "order_id": str(d.get("order_id", "")),
                "run_id": run_id,
                "account_id": str(d.get("account_id", "")),
                "strategy_track": str(d.get("strategy_track", "")),
                "symbol": str(d.get("symbol", "")),
                "side": str(d.get("side", "")),
                "quantity": int(d.get("quantity", 0) or 0),
                "fill_price": (
                    str(d["fill_price"]) if d.get("fill_price") is not None else None
                ),
                "commission": str(d.get("commission", "0")),
                "stamp_duty": str(d.get("stamp_duty", "0")),
                "transfer_fee": str(d.get("transfer_fee", "0")),
                "total_cost": str(d.get("total_cost", "0")),
                "cash_change": str(d.get("cash_change", "0")),
                "fill_date": (
                    str(d["fill_date"]) if d.get("fill_date") else None
                ),
            }
        )
    df = pd.DataFrame(rows, columns=FILL_COLUMNS)
    if not df.empty:
        df = df.sort_values(
            ["account_id", "fill_date", "order_id"], kind="stable"
        ).reset_index(drop=True)
    return df


def _equity_frame(
    accounts: Sequence[SimulatedAccountState],
    *,
    equity: dict[str, Any],
    run_id: str,
    as_of_date: str,
) -> pd.DataFrame:
    rows = []
    for a in accounts:
        hist = {str(h.get("date", "")): h for h in (a.history or [])}
        eq = dict(equity.get(a.account_id, {}) or {})
        days = sorted(set(hist) | {as_of_date})
        for d in days:
            h = hist.get(d) or {}
            if d == as_of_date:
                cash = str(eq.get("cash", a.cash))
                pos_value = str(eq.get("position_value", h.get("position_value", "")))
                total = str(eq.get("total_equity", h.get("total_equity", "")))
                positions = int(eq.get("positions", h.get("positions", 0)))
            else:
                cash = str(h.get("cash", ""))
                pos_value = str(h.get("position_value", ""))
                total = str(h.get("total_equity", ""))
                positions = int(h.get("positions", 0) or 0)
            rows.append(
                {
                    "run_id": run_id,
                    "as_of_date": d,
                    "account_id": a.account_id,
                    "cash": cash,
                    "position_value": pos_value,
                    "total_equity": total,
                    "positions": positions,
                    "observation_days": int(a.observation_days),
                    "eligibility_status": a.eligibility_status.value,
                }
            )
    df = pd.DataFrame(rows, columns=EQUITY_COLUMNS)
    if not df.empty:
        df = df.sort_values(
            ["account_id", "as_of_date"], kind="stable"
        ).reset_index(drop=True)
    return df


def _quality_summary(quality: Optional[dict[str, Any]], *, task_type: TaskType) -> dict[str, Any]:
    if quality is None:
        return {
            "schema_version": SCHEMA_VERSION,
            "task_type": task_type.value,
            "note": "本任务不单独执行数据质量闸门；质量结果由每日运行各自记录",
            "summary": {"critical": 0, "warning": 0, "rows_checked": 0},
            "has_critical": False,
            "issues_count": 0,
        }
    summary = dict(quality.get("summary") or {})
    return {
        "schema_version": SCHEMA_VERSION,
        "task_type": task_type.value,
        "summary": {
            "critical": int(summary.get("critical", 0) or 0),
            "warning": int(summary.get("warning", 0) or 0),
            "rows_checked": int(summary.get("rows_checked", 0) or 0),
        },
        "has_critical": bool(quality.get("has_critical", False)),
        "issues_count": len(quality.get("issues") or []),
    }


def write_audit_artifacts(
    *,
    record: RunRecord,
    config: AutomationConfig,
    task_type: TaskType,
    paths: ResultPaths,
    markdown: str,
    accounts: Sequence[SimulatedAccountState],
    orders: Sequence[Any],
    signals: Sequence[Any],
    quality: Optional[dict[str, Any]],
    equity: dict[str, Any],
    observation: Sequence[dict[str, Any]],
    extra_files: Iterable[Path] = (),
    generated_at: str,
) -> list[Path]:
    """写 FR-23 审计数据产物（不含 run.json / manifest，二者由调用方编排）。

    返回写入的产物路径列表（供 ``ctx.add_artifact`` 登记）。
    """
    paths.root.mkdir(parents=True, exist_ok=True)
    run_id = record.run_id
    as_of = record.as_of_date.isoformat()
    written: list[Path] = []

    # 1) 规范名报告副本（与 daily/weekly-report.md 同内容）
    run_report = paths.root / "run-report.md"
    atomic_write_text(run_report, markdown)
    written.append(run_report)

    # 2) Parquet（固定列序 + 稳定排序）
    written.append(
        write_parquet(_signals_frame(signals, run_id=run_id, as_of_date=as_of), paths.root / "signals.parquet")
    )
    written.append(
        write_parquet(_orders_frame(orders, run_id=run_id), paths.root / "orders.parquet")
    )
    written.append(
        write_parquet(_fills_frame(orders, run_id=run_id), paths.root / "fills.parquet")
    )
    written.append(
        write_parquet(
            _equity_frame(accounts, equity=equity, run_id=run_id, as_of_date=as_of),
            paths.root / "equity.parquet",
        )
    )

    # 3) 账户快照 / 质量摘要（清单与 output_hash 需要它们）
    account_snapshot = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "task_type": task_type.value,
        "as_of_date": as_of,
        "generated_at": generated_at,
        "equity": equity,
        "observation": list(observation),
        "accounts": [a.to_dict() for a in accounts],
    }
    written.append(
        write_json_artifact(paths.root / "account-snapshot.json", account_snapshot)
    )
    written.append(
        write_json_artifact(
            paths.root / "quality-summary.json",
            _quality_summary(quality, task_type=task_type),
        )
    )

    # 4) 运行摘要（output_hash 需要全部文件已存在）
    files = _manifest_files(paths.root, extra_files=extra_files)
    entries = [
        {"path": _rel_of(config, p), "sha256": sha256_file(p), "bytes": p.stat().st_size}
        for p in files
    ]
    output_hash = output_hash_for(entries)
    written.append(
        write_json_artifact(
            paths.root / "run-summary.json",
            build_run_summary(
                record=record,
                output_hash=output_hash,
                signals=len(list(signals)),
                orders=orders,
                accounts=accounts,
                observation=observation,
            ),
        )
    )

    return written


def _rel_of(config: AutomationConfig, p: Path) -> str:
    try:
        return str(Path(p).resolve().relative_to(config.base_dir)).replace("\\", "/")
    except ValueError:
        return str(p).replace("\\", "/")


def write_manifest(
    *,
    record: RunRecord,
    config: AutomationConfig,
    run_dir: Path,
    extra_files: Iterable[Path] = (),
    generated_at: str,
) -> Path:
    """写 manifest.json：对 run 目录内全部产物（含 run.json / 报告）逐文件 SHA-256。

    必须在 ``run.json`` 与全部产物落盘**之后**调用，保证清单包含它们。
    """
    files = _manifest_files(run_dir, extra_files=extra_files)
    entries = [
        {"path": _rel_of(config, p), "sha256": sha256_file(p), "bytes": p.stat().st_size}
        for p in files
    ]
    manifest = build_manifest(
        record=record,
        config=config,
        run_dir=run_dir,
        output_hash=output_hash_for(entries),
        extra_files=extra_files,
        generated_at=generated_at,
    )
    path = run_dir / "manifest.json"
    write_json_artifact(path, manifest)
    return path
