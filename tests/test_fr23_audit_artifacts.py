"""FR-23 审计产物测试。

每次成功运行都必须产出完整、确定、可复算的审计产物：

- ``run-summary.json`` / ``run-report.md`` / ``manifest.json``
- ``signals.parquet`` / ``orders.parquet`` / ``fills.parquet``
- ``account-snapshot.json`` / ``equity.parquet`` / ``quality-summary.json``

并满足：固定 Parquet 列序与稳定排序；订单与成交经 ``order_id`` 关联；
manifest 含全部文件 SHA-256 与 ``code_commit/config_hash/input_hash/output_hash``；
``run.json`` 的 artifacts 列表包含数据产物、报告与 manifest；
``latest`` 全部写完并校验后原子更新、失败运行不指向半成品；
同输入双跑字节一致（JSON/MD/Parquet），``run_id`` 与 ``order_id`` 一致。

所有运行都在 ``tmp_path`` 内完成，绝不触碰仓库预生成报告。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

from ashare_quant.automation.audit import (  # noqa: E402
    EQUITY_COLUMNS,
    FILL_COLUMNS,
    ORDER_COLUMNS,
    SIGNAL_COLUMNS,
    _manifest_files,
    verify_manifest,
)
from ashare_quant.automation.calendar import TradingCalendar  # noqa: E402
from ashare_quant.automation.config import (  # noqa: E402
    AccountConfig,
    AutomationConfig,
    DataConfig,
    LoggingConfig,
    PathsConfig,
)
from ashare_quant.automation.datasource import InjectedDataSource  # noqa: E402
from ashare_quant.automation.models import (  # noqa: E402
    EligibilityStatus,
    RunState,
    StrategyTrack,
    TaskType,
)
from ashare_quant.automation.reporting import result_paths  # noqa: E402
from ashare_quant.automation.state import StateStore  # noqa: E402
from ashare_quant.automation.weekly import WeeklyPipeline, run_weekly  # noqa: E402
from ashare_quant.backtest.config import BacktestConfig  # noqa: E402
from ashare_quant.config import load_config  # noqa: E402
from tests.research_samples import (  # noqa: E402
    make_benchmark_data,
    make_historical_status_table,
    make_research_quotes,
)

from ashare_quant.automation.daily import DailyPipeline, run_daily  # noqa: E402

FIXED_NOW = datetime(2026, 8, 1, 12, 0, 0)
FIXED_NOW_FN = lambda: FIXED_NOW  # noqa: E731 - 注入固定时钟供确定性双跑

# FR-23 必出的 9 项审计产物（在 run 目录内）。
AUDIT_FILES = [
    "run-summary.json",
    "run-report.md",
    "manifest.json",
    "signals.parquet",
    "orders.parquet",
    "fills.parquet",
    "account-snapshot.json",
    "equity.parquet",
    "quality-summary.json",
]


# ---------------------------------------------------------------------- #
# 8) manifest 自引用回归：重跑覆盖时清单不得包含 manifest 自身
# ---------------------------------------------------------------------- #

def test_manifest_files_excludes_self(tmp_path):
    """manifest.json 不得出现在自己的文件清单里（否则重跑后哈希不可复算）。"""
    d = tmp_path / "run"
    d.mkdir()
    (d / "run.json").write_text("{}", encoding="utf-8")
    (d / "daily-report.md").write_text("x", encoding="utf-8")
    # 模拟重跑：磁盘上已存在上一次的 manifest.json
    (d / "manifest.json").write_text("{}", encoding="utf-8")
    names = {p.name for p in _manifest_files(d)}
    assert "manifest.json" not in names
    assert {"run.json", "daily-report.md"} <= names


# ---------------------------------------------------------------------- #
# 合成环境
# ---------------------------------------------------------------------- #

def _synthetic(base_dir: Path):
    app_cfg = load_config(ROOT / "config" / "default.yaml")
    start = date(2020, 1, 2)
    n_days = 200
    quotes = make_research_quotes(start=start, n_days=n_days, n_stocks=8)
    status_df = make_historical_status_table(start=start, n_stocks=8)
    bench = make_benchmark_data(start=start, n_days=n_days)
    trade_dates = sorted({pd.Timestamp(d).date() for d in quotes["trade_date"]})
    cal = TradingCalendar.from_dates(trade_dates, source="synthetic-calendar")
    source = InjectedDataSource(
        quotes,
        name="synthetic-research-samples",
        synthetic=True,
        security_master=status_df,
        benchmark=bench,
        now_fn=FIXED_NOW_FN,
    )
    config = AutomationConfig(
        paths=PathsConfig(
            data_dir="data",
            state_dir="state",
            reports_dir="reports",
            logs_dir="logs",
            archive_dir="reports/archive",
        ),
        data=DataConfig(symbols=[], lookback_days=200),
        logging=LoggingConfig(console=False),
        accounts=[
            AccountConfig(
                account_id="paper-steady",
                track=StrategyTrack.STEADY,
                initial_cash=1000.0,
                eligibility_status=EligibilityStatus.NOT_ELIGIBLE_FOR_LIVE_TRADING,
            ),
            AccountConfig(
                account_id="paper-aggressive",
                track=StrategyTrack.AGGRESSIVE,
                initial_cash=1000.0,
                eligibility_status=EligibilityStatus.SIMULATION_ONLY,
            ),
        ],
    ).with_base_dir(base_dir)
    universe_kwargs = {"min_turnover": 0.0, "min_listing_days": 120}
    return config, source, cal, trade_dates, app_cfg, universe_kwargs


def _run_daily(config, source, cal, d, app_cfg, uk, store, now_fn=FIXED_NOW_FN):
    return run_daily(
        config,
        as_of_date=d,
        data_source=source,
        pipeline=DailyPipeline(app_config=app_cfg, calendar=cal, universe_kwargs=uk),
        state_store=store,
        now_fn=now_fn,
    )


# ---------------------------------------------------------------------- #
# 1) 产物齐全 + run.json artifacts 完整
# ---------------------------------------------------------------------- #

def test_daily_audit_artifact_set(tmp_path):
    config, source, cal, trade_dates, app_cfg, uk = _synthetic(tmp_path)
    store = StateStore(config.state_dir)
    d = trade_dates[-1]
    out = _run_daily(config, source, cal, d, app_cfg, uk, store)
    assert out.state is RunState.SUCCESS

    paths = result_paths(config, task_type=TaskType.DAILY, as_of_date=d)
    for name in AUDIT_FILES:
        assert (paths.root / name).exists(), f"缺少审计产物 {name}"

    # run.json 的 artifacts 必须包含数据产物、报告与 manifest（FR-23）
    run_record = json.loads(paths.run_json.read_text(encoding="utf-8"))
    artifact_names = {Path(a).name for a in run_record["artifacts"]}
    for name in AUDIT_FILES:
        assert name in artifact_names, f"run.json artifacts 缺少 {name}"
    assert "daily-report.md" in artifact_names
    assert "run.json" in artifact_names
    assert "signals.json" in artifact_names  # 原有数据产物仍在

    # run-report.md 与 daily-report.md 同内容
    assert paths.report_md.read_text(encoding="utf-8") == (
        paths.root / "run-report.md"
    ).read_text(encoding="utf-8")


# ---------------------------------------------------------------------- #
# 2) Parquet 固定列序 + 稳定排序
# ---------------------------------------------------------------------- #

def test_parquet_fixed_columns_and_stable_sort(tmp_path):
    config, source, cal, trade_dates, app_cfg, uk = _synthetic(tmp_path)
    store = StateStore(config.state_dir)
    d = trade_dates[-1]
    out = _run_daily(config, source, cal, d, app_cfg, uk, store)
    assert out.state is RunState.SUCCESS

    paths = result_paths(config, task_type=TaskType.DAILY, as_of_date=d)
    signals = pd.read_parquet(paths.root / "signals.parquet")
    orders = pd.read_parquet(paths.root / "orders.parquet")
    fills = pd.read_parquet(paths.root / "fills.parquet")
    equity = pd.read_parquet(paths.root / "equity.parquet")

    # 固定列序
    assert list(signals.columns) == SIGNAL_COLUMNS
    assert list(orders.columns) == ORDER_COLUMNS
    assert list(fills.columns) == FILL_COLUMNS
    assert list(equity.columns) == EQUITY_COLUMNS

    # 稳定排序
    assert _is_sorted(orders, ["account_id", "signal_date", "symbol", "side", "order_id"])
    assert _is_sorted(fills, ["account_id", "fill_date", "order_id"])
    assert _is_sorted(equity, ["account_id", "as_of_date"])

    # 金额以字符串保存（Decimal 原文，不丢精度）
    assert signals["quantity"].dtype.kind in "iu"  # 整数


def _is_sorted(df: pd.DataFrame, cols: list[str]) -> bool:
    if df.empty:
        return True
    key = df[cols].astype(str).agg("|".join, axis=1)
    return key.is_monotonic_increasing


# ---------------------------------------------------------------------- #
# 3) order_id 关联 orders 与 fills
# ---------------------------------------------------------------------- #

def test_order_id_links_orders_and_fills(tmp_path):
    config, source, cal, trade_dates, app_cfg, uk = _synthetic(tmp_path)
    store = StateStore(config.state_dir)
    d = trade_dates[-1]
    out = _run_daily(config, source, cal, d, app_cfg, uk, store)
    assert out.state is RunState.SUCCESS

    paths = result_paths(config, task_type=TaskType.DAILY, as_of_date=d)
    orders = pd.read_parquet(paths.root / "orders.parquet")
    fills = pd.read_parquet(paths.root / "fills.parquet")

    order_ids = set(orders["order_id"])
    fill_ids = set(fills["order_id"])
    # 每条成交都能在订单里找到；每条订单 id 全局一致
    assert fill_ids <= order_ids
    assert len(order_ids) == orders["order_id"].nunique()
    assert len(fill_ids) == fills["order_id"].nunique()

    # 所有 FILLED 订单都进了 fills
    filled_ids = set(orders.loc[orders["status"] == "FILLED", "order_id"])
    assert filled_ids == fill_ids


# ---------------------------------------------------------------------- #
# 4) manifest：SHA-256 + 来源哈希 + 可校验
# ---------------------------------------------------------------------- #

def test_manifest_sha256_and_source_hashes(tmp_path):
    config, source, cal, trade_dates, app_cfg, uk = _synthetic(tmp_path)
    store = StateStore(config.state_dir)
    d = trade_dates[-1]
    out = _run_daily(config, source, cal, d, app_cfg, uk, store)
    assert out.state is RunState.SUCCESS

    paths = result_paths(config, task_type=TaskType.DAILY, as_of_date=d)
    manifest = json.loads((paths.root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["run_id"] == out.record.run_id
    for key in ("code_commit", "config_hash", "input_hash", "output_hash"):
        assert manifest.get(key), f"manifest 缺少 {key}"

    # 逐文件 SHA-256 与磁盘一致
    for entry in manifest["files"]:
        p = config.base_dir / entry["path"]
        assert p.exists(), f"manifest 文件不存在 {entry['path']}"
        actual = _sha256(p)
        assert actual == entry["sha256"], f"哈希不一致 {entry['path']}"

    # run 目录内每个文件（除 manifest 自身）都被列出
    run_files = {
        str(p.resolve().relative_to(config.base_dir)).replace("\\", "/")
        for p in paths.root.rglob("*")
        if p.is_file() and p.name != "manifest.json"
    }
    listed = {e["path"] for e in manifest["files"]}
    assert run_files <= listed
    assert manifest["count"] == len(listed)

    # 独立校验通过
    verified = verify_manifest(paths.root / "manifest.json", config=config)
    assert verified["output_hash"] == manifest["output_hash"]


def _sha256(p: Path) -> str:
    import hashlib

    return hashlib.sha256(p.read_bytes()).hexdigest()


# ---------------------------------------------------------------------- #
# 5) latest：原子更新；失败运行不指向半成品
# ---------------------------------------------------------------------- #

def test_latest_not_updated_on_failed_run(tmp_path):
    config, source, cal, trade_dates, app_cfg, uk = _synthetic(tmp_path)
    store = StateStore(config.state_dir)
    d = trade_dates[-1]

    # 成功运行 -> latest 落盘
    out_ok = _run_daily(config, source, cal, d, app_cfg, uk, store)
    assert out_ok.state is RunState.SUCCESS
    paths = result_paths(config, task_type=TaskType.DAILY, as_of_date=d)
    latest = config.reports_dir / "latest-daily.md"
    assert latest.exists()
    ok_content = latest.read_bytes()

    # 失败运行（日历 fail-closed，日期超出覆盖范围）不得改写 latest
    narrow = TradingCalendar.from_dates([trade_dates[-1]], source="narrow")
    outside = date(2019, 6, 1)
    out_fail = run_daily(
        config,
        as_of_date=outside,
        data_source=source,
        pipeline=DailyPipeline(app_config=app_cfg, calendar=narrow, universe_kwargs=uk),
        state_store=store,
        now_fn=FIXED_NOW_FN,
    )
    assert out_fail.state is RunState.FAILED
    assert latest.read_bytes() == ok_content, "失败运行不得改写 latest 摘要"


# ---------------------------------------------------------------------- #
# 6) 同输入双跑：字节一致 + run_id/order_id 一致
# ---------------------------------------------------------------------- #

def test_deterministic_double_run(tmp_path):
    base1 = tmp_path / "run1"
    base2 = tmp_path / "run2"
    cfg1, src1, cal1, dates1, app1, uk1 = _synthetic(base1)
    cfg2, src2, cal2, dates2, app2, uk2 = _synthetic(base2)
    assert dates1 == dates2

    d = dates1[-1]
    out1 = _run_daily(cfg1, src1, cal1, d, app1, uk1, StateStore(cfg1.state_dir))
    out2 = _run_daily(cfg2, src2, cal2, d, app2, uk2, StateStore(cfg2.state_dir))
    assert out1.state is RunState.SUCCESS and out2.state is RunState.SUCCESS
    assert out1.record.run_id == out2.record.run_id, "run_id 必须一致"

    p1 = result_paths(cfg1, task_type=TaskType.DAILY, as_of_date=d).root
    p2 = result_paths(cfg2, task_type=TaskType.DAILY, as_of_date=d).root
    names = [
        "run.json",
        "run-summary.json",
        "run-report.md",
        "daily-report.md",
        "manifest.json",
        "account-snapshot.json",
        "quality-summary.json",
        "signals.json",
        "simulated-orders.json",
        "accounts.json",
        "quality.json",
        "signals.parquet",
        "orders.parquet",
        "fills.parquet",
        "equity.parquet",
    ]
    for name in names:
        b1 = (p1 / name).read_bytes()
        b2 = (p2 / name).read_bytes()
        assert b1 == b2, f"双跑产物不一致: {name}"

    # order_id 一致
    o1 = pd.read_parquet(p1 / "orders.parquet")
    o2 = pd.read_parquet(p2 / "orders.parquet")
    assert list(o1["order_id"]) == list(o2["order_id"])
    assert o1.equals(o2)


# ---------------------------------------------------------------------- #
# 7) 每周任务：同样产出审计产物 + 研究/汇总入清单
# ---------------------------------------------------------------------- #

def _run_window_and_saturday(config, source, cal, trade_dates, app_cfg, uk, store, n=10):
    window = trade_dates[-n:]
    for d in window:
        _run_daily(config, source, cal, d, app_cfg, uk, store)
    anchor = window[5]
    as_of_sat = anchor + timedelta(days=(5 - anchor.weekday()) % 7)
    cal2 = TradingCalendar.from_dates(list(trade_dates) + [as_of_sat], source="syn+asof")
    return as_of_sat, cal2


def test_weekly_audit_artifacts(tmp_path):
    config, source, cal, trade_dates, app_cfg, uk = _synthetic(tmp_path)
    store = StateStore(config.state_dir)
    as_of_sat, cal2 = _run_window_and_saturday(
        config, source, cal, trade_dates, app_cfg, uk, store
    )

    out = run_weekly(
        config,
        as_of_date=as_of_sat,
        pipeline=WeeklyPipeline(calendar=cal2),
        state_store=store,
        now_fn=FIXED_NOW_FN,
    )
    assert out.state is RunState.SUCCESS

    paths = result_paths(config, task_type=TaskType.WEEKLY, as_of_date=as_of_sat)
    for name in AUDIT_FILES:
        assert (paths.root / name).exists(), f"周任务缺少审计产物 {name}"

    run_record = json.loads(paths.run_json.read_text(encoding="utf-8"))
    artifact_names = {Path(a).name for a in run_record["artifacts"]}
    for name in AUDIT_FILES:
        assert name in artifact_names, f"周 run.json artifacts 缺少 {name}"
    assert "weekly-summary.json" in artifact_names

    # manifest 覆盖周产物
    manifest = json.loads((paths.root / "manifest.json").read_text(encoding="utf-8"))
    listed = {e["path"] for e in manifest["files"]}
    assert any("weekly-summary.json" in p for p in listed)
    assert any("weekly-report.md" in p for p in listed)
    verified = verify_manifest(paths.root / "manifest.json", config=config)
    assert verified["output_hash"] == manifest["output_hash"]
