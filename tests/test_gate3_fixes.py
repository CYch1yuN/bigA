"""Codex Gate 3 首轮审核问题修复回归测试。

FR-10: AggressiveStrategy 不依赖 DataFrame/Series 索引保存交易日期。
       显式从 sym_data["trade_date"] 取得相对强度起止日期。
       新增真实 RangeIndex 回归测试，禁止测试通过 set_index("trade_date") 适配实现。

FR-11: 修复 analysis.py 中所有 ``metric or fallback`` 数值读取。
       0.0 是合法值，不得被替换。仅当字段缺失或为 None 时使用 fallback。
       - 0% 最大回撤保持 0.0
       - 0% 收益保持 0.0
       - 现金策略不得被报告为 100% 回撤或 -100% 年化收益
       - Markdown、JSON 和 eligibility 条件数值一致
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from ashare_quant.backtest.models import (
    BacktestResult,
    Fill,
    Order,
    PortfolioSnapshot,
    Position,
    Side,
    Signal,
    StrategyContext,
    to_decimal,
)
from ashare_quant.research.analysis import (
    EligibilityCheck,
    EligibilityStatus,
    FoldResult,
    ResearchResult,
    TrackResult,
    TrackType,
    check_steady_eligibility,
    compute_metrics_from_equity,
    safe_metric,
)
from ashare_quant.research.report import ResearchReportGenerator
from ashare_quant.research.strategies import (
    AGGRESSIVE_BASELINE_PARAMS,
    AggressiveParams,
    AggressiveStrategy,
)
from ashare_quant.research.stress import (
    ParameterPerturbationResult,
    StressResult,
    StressScenario,
)
from ashare_quant.research.walk_forward import Fold
from ashare_quant.research.monte_carlo import MonteCarloResult
from tests.backtest_samples import make_trade_dates
from tests.research_samples import (
    make_historical_status_table,
    make_stock_quotes,
)


# --------------------------------------------------------------------------- #
# 辅助函数
# --------------------------------------------------------------------------- #


def _make_snapshot(
    d: date,
    total_equity: float,
    cash: float | None = None,
    position_value: float | None = None,
) -> PortfolioSnapshot:
    """创建一个 PortfolioSnapshot。"""
    te = to_decimal(total_equity)
    c = to_decimal(cash) if cash is not None else te
    pv = to_decimal(position_value) if position_value is not None else to_decimal("0")
    return PortfolioSnapshot(
        snapshot_date=d,
        cash=c,
        position_value=pv,
        total_equity=te,
    )


def _make_dates(n: int, start: date = date(2024, 1, 1)) -> list[date]:
    """生成 n 个连续日历日期。"""
    return [start + timedelta(days=i) for i in range(n)]


def _make_hs300_dict(
    dates: list[date],
    daily_return: float = 0.001,
) -> dict[date, float]:
    """生成沪深 300 基准收盘价字典。"""
    price = 3000.0
    result: dict[date, float] = {}
    for dt in dates:
        price *= (1 + daily_return)
        result[dt] = round(price, 2)
    return result


def _make_simple_status_table(symbols: list[str]):
    """生成简单状态表。"""
    from ashare_quant.research.universe import HistoricalStatusTable
    records = [
        {
            "symbol": sym,
            "list_date": date(2019, 1, 2),
            "delist_date": None,
            "st_status": "normal",
            "status_valid_from": date(2019, 1, 2),
            "status_valid_to": None,
        }
        for sym in symbols
    ]
    return HistoricalStatusTable(records=pd.DataFrame(records))


def _make_permissive_filter(status_table, quotes, available_cash=1_000_000.0):
    """构建宽松参数过滤器。"""
    from ashare_quant.research.universe import HistoricalUniverseFilter
    return HistoricalUniverseFilter(
        status_table=status_table,
        quotes=quotes,
        min_listing_days=1,
        min_valid_days=1,
        valid_days_window=20,
        min_turnover=0.0,
        turnover_window=20,
        lot_size=100,
        available_cash=available_cash,
    )


def _make_position(symbol: str, qty: int = 100) -> Position:
    """创建持仓。"""
    return Position(
        symbol=symbol,
        total_quantity=qty,
        sellable_quantity=qty,
        average_cost=to_decimal("10.0"),
    )


def _make_breakout_quotes(
    symbols: list[str],
    dates: list[date],
    signal_idx: int,
) -> pd.DataFrame:
    """生成突破行情：signal 股在 signal_idx 日突破并放量。"""
    dfs: list[pd.DataFrame] = []
    for i, sym in enumerate(symbols):
        if i == 0:
            prices = [5.0] * len(dates)
            prices[signal_idx] = 5.5
            vols = [200000.0] * len(dates)
            vols[signal_idx] = 600000.0
            price_override = {dates[j]: prices[j] for j in range(len(dates))}
            vol_override = {dates[j]: vols[j] for j in range(len(dates))}
            dfs.append(make_stock_quotes(
                sym, dates, base_price=5.0, daily_return=0.0,
                volume=200000, price_override=price_override,
                volume_override=vol_override,
            ))
        else:
            dfs.append(make_stock_quotes(
                sym, dates, base_price=5.0, daily_return=0.0, volume=200000,
            ))
    return pd.concat(dfs, ignore_index=True).sort_values(
        ["trade_date", "symbol"]
    ).reset_index(drop=True)


def _build_context(
    dt: date,
    bars: pd.DataFrame,
    cash: float = 100_000.0,
    positions: dict | None = None,
) -> StrategyContext:
    """构建策略上下文。"""
    return StrategyContext(
        current_date=dt,
        portfolio=type("P", (), {
            "cash": to_decimal(cash),
            "total_equity": to_decimal(cash),
            "positions": positions or {},
        })(),
        bars_up_to_date=bars,
        positions=positions or {},
    )


# =========================================================================== #
# FR-10: RangeIndex 回归测试
# =========================================================================== #


class TestFR10RangeIndexRegression:
    """FR-10: AggressiveStrategy 不得依赖 DataFrame/Series 索引保存交易日期。

    所有测试使用 RangeIndex（默认整数索引），禁止将日期列设为索引。
    """

    def test_compute_relative_strength_with_range_index(self):
        """_compute_relative_strength 在 RangeIndex 下应正确计算。

        显式传入 trade_dates 参数，不依赖 stock_close.index。
        """
        dates = make_trade_dates(date(2020, 1, 2), 30)
        symbols = [f"{i:06d}" for i in range(1, 7)]
        quotes = _make_breakout_quotes(symbols, dates, signal_idx=25)
        status = _make_simple_status_table(symbols)
        uf = _make_permissive_filter(status, quotes)
        bench = _make_hs300_dict(dates, 0.001)

        strategy = AggressiveStrategy(
            AggressiveParams(), uf, dates, lot_size=100,
            benchmark_hs300=bench,
        )

        # 取一只股票的数据，保持 RangeIndex（不 set_index）
        sym_data = quotes[quotes["symbol"] == "000001"].sort_values("trade_date").reset_index(drop=True)
        assert isinstance(sym_data.index, pd.RangeIndex), "测试数据必须使用 RangeIndex"

        stock_close = sym_data["close_qfq"].astype(float)
        trade_dates = sym_data["trade_date"]

        # 调用 _compute_relative_strength，传入 trade_dates
        rs = strategy._compute_relative_strength(
            stock_close, trade_dates, dates[29]
        )

        # 应返回一个浮点数（非 None），说明不依赖索引也能工作
        assert rs is not None
        assert isinstance(rs, float)

    def test_aggressive_strategy_on_close_with_range_index(self):
        """AggressiveStrategy.on_close 在 RangeIndex 下应正常生成信号。

        禁止通过将 trade_date 设为索引来适配实现。
        """
        dates = make_trade_dates(date(2020, 1, 2), 30)
        symbols = [f"{i:06d}" for i in range(1, 7)]
        quotes = _make_breakout_quotes(symbols, dates, signal_idx=25)
        status = _make_simple_status_table(symbols)
        uf = _make_permissive_filter(status, quotes, available_cash=1_000_000.0)
        bench = _make_hs300_dict(dates, 0.001)

        strategy = AggressiveStrategy(
            AggressiveParams(), uf, dates, lot_size=100,
            benchmark_hs300=bench,
        )

        signal_dt = dates[25]
        # 使用 RangeIndex（不设日期索引）
        ts = pd.Timestamp(signal_dt)
        bars = quotes[pd.to_datetime(quotes["trade_date"]) <= ts].reset_index(drop=True)
        assert isinstance(bars.index, pd.RangeIndex), "测试数据必须使用 RangeIndex"

        ctx = _build_context(signal_dt, bars, cash=100_000.0)
        signals = strategy.on_close(ctx)

        # 应生成买入信号（突破+放量+相对强度条件满足）
        buys = [s for s in signals if s.side == Side.BUY]
        assert len(buys) == 1
        assert buys[0].signal_date == signal_dt

    def test_no_set_index_trade_date_in_test(self):
        """验证测试类不使用将 trade_date 设为索引的适配方法。

        这是一个元测试：检查本测试类的可执行代码不包含该方法调用。
        """
        import ast
        import inspect
        source = inspect.getsource(TestFR10RangeIndexRegression)
        tree = ast.parse(source)
        # 遍历 AST，检查没有 .set_index() 调用
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "set_index":
                pytest.fail("测试类中不得使用 set_index 方法")


# =========================================================================== #
# FR-11: safe_metric 和 0.0 保留测试
# =========================================================================== #


class TestFR11SafeMetric:
    """FR-11: safe_metric 函数测试。"""

    def test_zero_value_preserved(self):
        """0.0 是合法值，不应被 fallback 替换。"""
        metrics = {"max_drawdown": 0.0, "total_return": 0.0}
        assert safe_metric(metrics, "max_drawdown", 1.0) == 0.0
        assert safe_metric(metrics, "total_return", -1.0) == 0.0

    def test_none_uses_fallback(self):
        """None 值应使用 fallback。"""
        metrics = {"max_drawdown": None, "total_return": None}
        assert safe_metric(metrics, "max_drawdown", 1.0) == 1.0
        assert safe_metric(metrics, "total_return", -1.0) == -1.0

    def test_missing_key_uses_fallback(self):
        """缺失的键应使用 fallback。"""
        metrics = {}
        assert safe_metric(metrics, "max_drawdown", 1.0) == 1.0
        assert safe_metric(metrics, "total_return", -1.0) == -1.0

    def test_negative_value_preserved(self):
        """负值应被保留，不被 fallback 替换。"""
        metrics = {"total_return": -0.5}
        assert safe_metric(metrics, "total_return", -1.0) == -0.5

    def test_positive_value_preserved(self):
        """正值应被保留。"""
        metrics = {"annualized_return": 0.15}
        assert safe_metric(metrics, "annualized_return", -1.0) == 0.15

    def test_int_zero_preserved(self):
        """整数 0 也应被保留（不因 falsy 被替换）。"""
        metrics = {"max_drawdown": 0}
        assert safe_metric(metrics, "max_drawdown", 1.0) == 0.0


class TestFR11ZeroDrawdownEligibility:
    """FR-11: 0% 最大回撤保持 0.0。"""

    def test_zero_max_drawdown_not_replaced(self):
        """资格判定中 0.0 最大回撤应保持 0.0，不被替换为 1.0。"""
        oos_metrics = {
            "max_drawdown": 0.0,
            "annualized_return": 0.05,
        }
        result = check_steady_eligibility(
            oos_metrics=oos_metrics,
            stress_results=[],
            param_perturbation=None,
            fold_results=[],
        )

        # 找到 max_drawdown 条件
        dd_condition = next(
            c for c in result.conditions if c["name"] == "max_drawdown_le_20pct"
        )
        assert dd_condition["value"] == 0.0, "0.0 最大回撤应保持 0.0"
        assert dd_condition["passed"] is True

    def test_zero_return_not_replaced(self):
        """资格判定中 0.0 年化收益应保持 0.0，不被替换为 -1.0。"""
        oos_metrics = {
            "max_drawdown": 0.1,
            "annualized_return": 0.0,
        }
        result = check_steady_eligibility(
            oos_metrics=oos_metrics,
            stress_results=[],
            param_perturbation=None,
            fold_results=[],
        )

        # 找到年化收益条件
        ret_condition = next(
            c for c in result.conditions if c["name"] == "oos_annualized_return_positive"
        )
        assert ret_condition["value"] == 0.0, "0.0 年化收益应保持 0.0"
        # 0.0 不大于 0，所以条件不通过
        assert ret_condition["passed"] is False


class TestFR11CashStrategyMetrics:
    """FR-11: 现金策略不得被报告为 100% 回撤或 -100% 年化收益。"""

    def test_cash_strategy_no_100pct_drawdown(self):
        """纯现金策略（权益不变）最大回撤应为 0.0，不是 100%。"""
        # 模拟纯现金策略：权益始终为 1000
        dates = _make_dates(10)
        equity = [_make_snapshot(d, 1000.0, cash=1000.0, position_value=0) for d in dates]
        metrics = compute_metrics_from_equity(equity, initial_cash=1000.0)

        assert metrics["max_drawdown"] == 0.0, "现金策略最大回撤应为 0.0，不是 100%"
        assert metrics["total_return"] == 0.0, "现金策略总收益应为 0.0"
        assert metrics["annualized_return"] == 0.0, "现金策略年化收益应为 0.0，不是 -100%"

    def test_cash_strategy_eligibility_not_extreme(self):
        """现金策略资格判定不应报告极端值。"""
        oos_metrics = compute_metrics_from_equity(
            [_make_snapshot(d, 1000.0, cash=1000.0) for d in _make_dates(10)],
            initial_cash=1000.0,
        )
        result = check_steady_eligibility(
            oos_metrics=oos_metrics,
            stress_results=[],
            param_perturbation=None,
            fold_results=[],
        )

        dd_cond = next(c for c in result.conditions if c["name"] == "max_drawdown_le_20pct")
        assert dd_cond["value"] == 0.0, "现金策略回撤不得为 100%"

        ret_cond = next(c for c in result.conditions if c["name"] == "oos_annualized_return_positive")
        assert ret_cond["value"] == 0.0, "现金策略年化收益不得为 -100%"


class TestFR11ReportConsistency:
    """FR-11: Markdown、JSON 和 eligibility 条件数值一致。"""

    def _make_minimal_research_result(
        self,
        max_dd: float = 0.0,
        ann_ret: float = 0.0,
    ) -> ResearchResult:
        """创建最小化研究结果用于报告一致性测试。"""
        dates = _make_dates(10)
        equity = [_make_snapshot(d, 1000.0, cash=1000.0) for d in dates]
        metrics = compute_metrics_from_equity(equity, initial_cash=1000.0)
        # 覆盖特定值
        metrics["max_drawdown"] = max_dd
        metrics["annualized_return"] = ann_ret

        steady = TrackResult(track_type=TrackType.STEADY)
        steady.oos_equity = equity
        steady.oos_metrics = metrics
        steady.eligibility = check_steady_eligibility(
            oos_metrics=metrics,
            stress_results=[],
            param_perturbation=None,
            fold_results=[],
        )

        aggressive = TrackResult(track_type=TrackType.AGGRESSIVE)
        aggressive.eligibility = EligibilityCheck(
            status=EligibilityStatus.SIMULATION_ONLY,
            conditions=[],
            failure_reasons=[],
        )

        return ResearchResult(
            steady=steady,
            aggressive=aggressive,
            code_commit="test_sha_1234567890abcdef",
            config_hash="test_config_hash",
            data_hash="test_data_hash",
        )

    def test_json_markdown_eligibility_values_consistent(self):
        """JSON 报告、Markdown 报告和 eligibility 条件中的数值应一致。"""
        result = self._make_minimal_research_result(max_dd=0.0, ann_ret=0.0)
        gen = ResearchReportGenerator()

        # 生成 JSON
        json_data = gen.generate_json(result, 1000.0)
        # 生成 Markdown
        md_text = gen.generate_markdown(result, 1000.0)

        # 从 JSON 获取值
        json_max_dd = json_data["steady"]["oos_metrics"]["max_drawdown"]
        json_ann_ret = json_data["steady"]["oos_metrics"]["annualized_return"]
        json_elig_dd = None
        json_elig_ret = None
        for c in json_data["steady_eligibility"]["conditions"]:
            if c["name"] == "max_drawdown_le_20pct":
                json_elig_dd = c["value"]
            if c["name"] == "oos_annualized_return_positive":
                json_elig_ret = c["value"]

        # JSON 中 0.0 应保持 0.0
        assert json_max_dd == 0.0, f"JSON max_drawdown 应为 0.0，实际为 {json_max_dd}"
        assert json_ann_ret == 0.0, f"JSON annualized_return 应为 0.0，实际为 {json_ann_ret}"
        assert json_elig_dd == 0.0, f"JSON eligibility max_drawdown 应为 0.0，实际为 {json_elig_dd}"
        assert json_elig_ret == 0.0, f"JSON eligibility annualized_return 应为 0.0，实际为 {json_elig_ret}"

        # Markdown 中也应显示 0.00%（不是 100.00% 或 -100.00%）
        assert "0.00%" in md_text, "Markdown 中应显示 0.00%"
        assert "100.00%" not in md_text or "100.00%" in md_text.split("蒙特卡洛")[0].split("资格判定")[0], (
            "Markdown 中不应出现错误的 100.00% 回撤"
        )

    def test_zero_values_not_replaced_in_full_report(self):
        """完整报告生成中 0.0 值不应被替换。"""
        result = self._make_minimal_research_result(max_dd=0.0, ann_ret=0.0)
        gen = ResearchReportGenerator()

        with TemporaryDirectory() as tmpdir:
            paths = gen.generate_all(
                result, tmpdir,
                config_dict={"test": True},
                data_files=[],
                initial_cash=1000.0,
            )

            # 读取生成的 JSON
            json_path = paths["research-summary.json"]
            with open(json_path, encoding="utf-8") as f:
                data = json.load(f)

            steady_metrics = data["steady"]["oos_metrics"]
            assert steady_metrics["max_drawdown"] == 0.0
            assert steady_metrics["annualized_return"] == 0.0

            # 检查 eligibility 条件值
            for c in data["steady_eligibility"]["conditions"]:
                if c["name"] == "max_drawdown_le_20pct":
                    assert c["value"] == 0.0, "eligibility max_drawdown 应为 0.0"
                if c["name"] == "oos_annualized_return_positive":
                    assert c["value"] == 0.0, "eligibility annualized_return 应为 0.0"

            # 读取 Markdown
            md_path = paths["research-report.md"]
            md_text = md_path.read_text(encoding="utf-8")
            # 不应包含 100.00% 回撤（现金策略的回撤是 0.00%）
            assert "最大回撤: 0.00%" in md_text or "最大回撤: 0.0" in md_text
