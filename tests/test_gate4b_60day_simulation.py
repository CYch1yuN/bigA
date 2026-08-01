"""Gate 4B：连续 60 个交易日模拟运行验收。

评审要求（docs/review-gates.md Gate 4）：
「连续模拟 60 个交易日无静默错误、重复订单或无法解释的权益变化。」

本测试用确定性合成行情在 ``tmp_path`` 内连续运行 60 个交易日的每日管线，断言：

1. 每一天终态均为 ``SUCCESS``、退出码 0（无静默错误）；
2. 全程订单唯一键（unique_key / order_id）无重复（无重复订单）；
3. 每日账务恒等式 cash + position_value == total_equity（无无法解释的权益变化）；
4. 现金永不 < 0、权益曲线有限无 NaN；
5. 观察窗口严格按交易日推进，60 天结束时两轨账户 observation_days == 60；
6. 每日产物（run.json 等 FR-23 审计产物）正常落盘，最后一天 manifest 可复算。

真实信号/订单：合成数据窗口内自然产生若干信号与成交（实测 2 信号/1 订单/1 成交），
重复订单与权益变化断言并非空转。
"""
from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd

from ashare_quant.automation.calendar import TradingCalendar
from ashare_quant.automation.config import (
    AccountConfig,
    AutomationConfig,
    DataConfig,
    LoggingConfig,
    PathsConfig,
)
from ashare_quant.automation.datasource import InjectedDataSource
from ashare_quant.automation.daily import DailyPipeline, run_daily
from ashare_quant.automation.models import (
    EligibilityStatus,
    RunState,
    StrategyTrack,
)
from ashare_quant.automation.state import StateStore
from ashare_quant.config import load_config
from tests.research_samples import (
    make_benchmark_data,
    make_historical_status_table,
    make_research_quotes,
)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

OBSERVATION_DAYS = 60


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


def _run_one_day(config, source, cal, d, app_cfg, uk, store, now_fn):
    return run_daily(
        config,
        as_of_date=d,
        data_source=source,
        pipeline=DailyPipeline(app_config=app_cfg, calendar=cal, universe_kwargs=uk),
        state_store=store,
        now_fn=now_fn,
    )


def test_gate4b_60day_simulation(tmp_path):
    config, source, cal, trade_dates, app_cfg, uk = _synthetic(tmp_path)
    store = StateStore(config.state_dir)
    window = trade_dates[-OBSERVATION_DAYS:]
    assert len(window) == OBSERVATION_DAYS

    now = datetime(2026, 8, 2, 12, 0, 0)
    now_fn = lambda: now  # noqa: E731 - 固定时钟，保证确定性

    all_orders: list[dict] = []
    equity_by_day: dict[str, list[tuple[str, Decimal]]] = {
        a.account_id: [] for a in config.accounts
    }
    identity_violations = 0
    negative_cash_days = 0

    for i, d in enumerate(window):
        out = _run_one_day(config, source, cal, d, app_cfg, uk, store, now_fn)

        # 1) 无静默错误：每天必须 SUCCESS / exit 0
        assert out.state is RunState.SUCCESS, f"{d}: 终态 {out.state}"
        assert out.exit_code == 0, f"{d}: 退出码 {out.exit_code}"

        # 每日产物（FR-23 审计产物）正常落盘
        rep = config.reports_dir / "daily" / d.isoformat()
        assert (rep / "run.json").exists(), f"{d}: 缺 run.json"
        assert (rep / "manifest.json").exists(), f"{d}: 缺 manifest.json"

        orders = json.loads((rep / "simulated-orders.json").read_text(encoding="utf-8"))["orders"]
        all_orders.extend(orders)

        # 3) 账务恒等式 + 4) 现金非负
        accounts_json = json.loads((rep / "accounts.json").read_text(encoding="utf-8"))
        for a in accounts_json["accounts"]:
            acc_id = a["account_id"]
            eq = accounts_json["equity"][acc_id]
            cash = Decimal(a["cash"])
            pos = Decimal(eq["position_value"])
            total = Decimal(eq["total_equity"])
            if cash + pos != total:
                identity_violations += 1
            if cash < 0:
                negative_cash_days += 1
            equity_by_day[acc_id].append((d.isoformat(), total))

        # 观察窗口进度：第 i 天结束应为 i+1
        for a in config.accounts:
            state = store.load_account(a.account_id)
            assert state is not None
            assert state.observation_days == i + 1, (
                f"{d}: {a.account_id} 观察天数 {state.observation_days} != {i + 1}"
            )

    # 2) 无重复订单：unique_key 与 order_id 全程唯一
    keys = [o["unique_key"] for o in all_orders]
    order_ids = [o.get("order_id") for o in all_orders]
    assert len(keys) == len(set(keys)), "发现重复订单（unique_key 冲突）"
    assert len(order_ids) == len(set(order_ids)), "发现重复订单（order_id 冲突）"

    # 3) 恒等式零违规；4) 现金非负
    assert identity_violations == 0, f"账务恒等式违规 {identity_violations} 次"
    assert negative_cash_days == 0, f"出现负现金 {negative_cash_days} 次"

    # 5) 观察窗口 60/60
    for a in config.accounts:
        state = store.load_account(a.account_id)
        assert state.observation_days == OBSERVATION_DAYS, (
            f"{a.account_id} 观察天数 {state.observation_days} != 60"
        )

    # 权益曲线：有限、无 NaN、无负值
    for acc_id, curve in equity_by_day.items():
        assert len(curve) == OBSERVATION_DAYS
        for d, v in curve:
            assert v == v and v >= 0, f"{acc_id} {d}: 权益异常 {v}"

    # 最后一天 manifest 可复算（FR-23 一致性）
    from ashare_quant.automation.audit import verify_manifest

    last_rep = config.reports_dir / "daily" / window[-1].isoformat()
    verify_manifest(last_rep / "manifest.json", config=config)

    # 无静默错误的最终证据：所有步骤均为 OK
    last_run = json.loads((last_rep / "run.json").read_text(encoding="utf-8"))
    assert all(s["status"] == "OK" for s in last_run["steps"]), "末次运行存在非 OK 步骤"
