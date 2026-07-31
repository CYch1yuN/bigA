"""FR-05 端到端测试：BacktestEngine 实际调用 UniverseFilter。

验证：
1. 默认 UniverseFilter 拒绝一手金额不达标的 BUY
2. 过滤后 fills 为空
3. 订单流水保存明确 reject_reason 和 reject_detail
4. 自定义 AlwaysRejectUniverseFilter 能阻止引擎成交
5. 自定义 AlwaysAllowUniverseFilter 会被引擎实际调用
6. UniverseFilter 拒绝时 SELL 仍可退出已有持仓
7. 调用日期只能是信号日，不能读取未来成交日数据
8. JSON、Markdown、orders DataFrame 包含过滤原因
9. 同一输入重复运行结果一致
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

import pandas as pd
import pytest

from ashare_quant.backtest.config import BacktestConfig
from ashare_quant.backtest.engine import BacktestEngine
from ashare_quant.backtest.interfaces import UniverseFilter
from ashare_quant.backtest.models import (
    EligibilityDecision,
    OrderStatus,
    RejectReason,
    Side,
    Signal,
    StrategyContext,
    to_decimal,
)
from ashare_quant.backtest.report import ReportGenerator
from ashare_quant.backtest.strategies import ScriptedStrategy
from tests.backtest_samples import make_quotes, make_trade_dates


# ------------------------------------------------------------------ #
# 自定义 UniverseFilter 实现
# ------------------------------------------------------------------ #
class AlwaysRejectUniverseFilter(UniverseFilter):
    """始终拒绝的股票池过滤器，用于验证引擎实际调用。"""

    def __init__(self) -> None:
        self.call_count: int = 0
        self.called_with: list[tuple[str, date]] = []

    def is_eligible(
        self, symbol: str, dt: date, context: StrategyContext
    ) -> EligibilityDecision:
        self.call_count += 1
        self.called_with.append((symbol, dt))
        return EligibilityDecision(False, f"AlwaysReject: 拒绝 {symbol} @ {dt}")


class AlwaysAllowUniverseFilter(UniverseFilter):
    """始终允许的股票池过滤器，用于验证引擎实际调用。"""

    def __init__(self) -> None:
        self.call_count: int = 0
        self.called_with: list[tuple[str, date]] = []

    def is_eligible(
        self, symbol: str, dt: date, context: StrategyContext
    ) -> EligibilityDecision:
        self.call_count += 1
        self.called_with.append((symbol, dt))
        return EligibilityDecision(True, "")


class BuyOnceThenRejectFilter(UniverseFilter):
    """允许第一次检查通过，之后全部拒绝。

    用于测试 SELL 信号在过滤器拒绝时仍能退出持仓。
    """

    def __init__(self) -> None:
        self.call_count: int = 0
        self.called_with: list[tuple[str, date]] = []

    def is_eligible(
        self, symbol: str, dt: date, context: StrategyContext
    ) -> EligibilityDecision:
        self.call_count += 1
        self.called_with.append((symbol, dt))
        if self.call_count == 1:
            return EligibilityDecision(True, "")
        return EligibilityDecision(
            False, f"BuyOnceThenReject: 第 {self.call_count} 次检查拒绝"
        )


# ------------------------------------------------------------------ #
# 辅助函数
# ------------------------------------------------------------------ #
def _run(
    data: pd.DataFrame,
    signals: list[Signal],
    config: Optional[BacktestConfig] = None,
    universe_filter: Optional[UniverseFilter] = None,
    start: Optional[date] = None,
    end: Optional[date] = None,
):
    cfg = config or BacktestConfig(initial_cash=100000)
    dates = make_trade_dates(date(2024, 1, 2), 10)
    s = start or dates[0]
    e = end or dates[-1]
    engine = BacktestEngine()
    strategy = ScriptedStrategy(signals)
    return engine.run(
        data=data,
        strategy=strategy,
        start_date=s,
        end_date=e,
        initial_cash=cfg.initial_cash,
        config=cfg,
        universe_filter=universe_filter,
    )


def _make_low_lot_quotes(symbol: str = "000001") -> pd.DataFrame:
    """生成 close_raw=9.99 的行情，一手金额 999 < 默认 1000 -> 过滤拒绝。"""
    return make_quotes(symbol, date(2024, 1, 2), 10, base_price=9.99)


def _make_valid_quotes(symbol: str = "000001") -> pd.DataFrame:
    """生成 close_raw=10.0 的行情，一手金额 1000 >= 默认 1000 -> 过滤通过。"""
    return make_quotes(symbol, date(2024, 1, 2), 10, base_price=10.0)


# ------------------------------------------------------------------ #
# 1. 默认 UniverseFilter 拒绝一手金额不达标的 BUY
# ------------------------------------------------------------------ #
class TestDefaultFilterRejectsLowLotValue:
    """默认 UniverseFilter 拒绝一手金额不达标的 BUY 信号。"""

    def test_rejects_low_lot_value_buy(self):
        """close_raw=9.99, lot_value=999 < 1000 -> UNIVERSE_FILTERED。"""
        quotes = _make_low_lot_quotes()
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [Signal(d[0], "000001", Side.BUY, 100, "low lot value")]
        result = _run(quotes, signals, BacktestConfig(initial_cash=100000))

        assert len(result.fills) == 0, "过滤拒绝后不应有成交"
        assert result.orders[0].status == OrderStatus.REJECTED
        assert result.orders[0].reject_reason == RejectReason.UNIVERSE_FILTERED

    def test_fills_empty_after_filter(self):
        """过滤后 fills 列表必须为空。"""
        quotes = _make_low_lot_quotes()
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [Signal(d[0], "000001", Side.BUY, 100)]
        result = _run(quotes, signals, BacktestConfig(initial_cash=100000))
        assert result.fills == []

    def test_reject_reason_and_detail_present(self):
        """订单流水必须保存明确 reject_reason 和 reject_detail。"""
        quotes = _make_low_lot_quotes()
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [Signal(d[0], "000001", Side.BUY, 100, "test")]
        result = _run(quotes, signals, BacktestConfig(initial_cash=100000))

        order = result.orders[0]
        assert order.reject_reason == RejectReason.UNIVERSE_FILTERED
        assert order.reject_detail is not None
        assert "股票池过滤" in order.reject_detail
        assert "一手金额" in order.reject_detail or "999" in order.reject_detail


# ------------------------------------------------------------------ #
# 2. 自定义 AlwaysRejectUniverseFilter 能阻止引擎成交
# ------------------------------------------------------------------ #
class TestAlwaysRejectFilterBlocksFills:
    """自定义 AlwaysRejectUniverseFilter 能阻止引擎成交。"""

    def test_no_fills_with_always_reject(self):
        quotes = _make_valid_quotes()
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [Signal(d[0], "000001", Side.BUY, 100)]
        uf = AlwaysRejectUniverseFilter()
        result = _run(
            quotes, signals, BacktestConfig(initial_cash=100000),
            universe_filter=uf,
        )

        assert len(result.fills) == 0
        assert result.orders[0].status == OrderStatus.REJECTED
        assert result.orders[0].reject_reason == RejectReason.UNIVERSE_FILTERED

    def test_filter_actually_called(self):
        """必须证明引擎实际调用了自定义过滤器。"""
        quotes = _make_valid_quotes()
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [Signal(d[0], "000001", Side.BUY, 100)]
        uf = AlwaysRejectUniverseFilter()
        _run(
            quotes, signals, BacktestConfig(initial_cash=100000),
            universe_filter=uf,
        )

        assert uf.call_count > 0, "引擎必须实际调用 UniverseFilter"
        assert uf.called_with[0][0] == "000001"


# ------------------------------------------------------------------ #
# 3. 自定义 AlwaysAllowUniverseFilter 会被引擎实际调用
# ------------------------------------------------------------------ #
class TestAlwaysAllowFilterCalledByEngine:
    """自定义 AlwaysAllowUniverseFilter 会被引擎实际调用。"""

    def test_allow_filter_called_and_fills(self):
        quotes = _make_valid_quotes()
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [Signal(d[0], "000001", Side.BUY, 100)]
        uf = AlwaysAllowUniverseFilter()
        result = _run(
            quotes, signals, BacktestConfig(initial_cash=100000),
            universe_filter=uf,
        )

        assert uf.call_count > 0, "引擎必须实际调用 AlwaysAllowUniverseFilter"
        assert len(result.fills) == 1, "AlwaysAllow 应允许成交"
        assert result.orders[0].status == OrderStatus.FILLED

    def test_allow_filter_called_with_correct_symbol(self):
        quotes = _make_valid_quotes()
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [Signal(d[0], "000001", Side.BUY, 100)]
        uf = AlwaysAllowUniverseFilter()
        _run(
            quotes, signals, BacktestConfig(initial_cash=100000),
            universe_filter=uf,
        )

        assert uf.called_with[0][0] == "000001"


# ------------------------------------------------------------------ #
# 4. UniverseFilter 拒绝时 SELL 仍可退出已有持仓
# ------------------------------------------------------------------ #
class TestSellNotFiltered:
    """SELL 信号不经过 UniverseFilter，即使过滤器拒绝也能退出持仓。"""

    def test_sell_bypasses_filter(self):
        """BUY 通过过滤后建仓，SELL 即使在过滤器拒绝时也能成交。

        使用 BuyOnceThenRejectFilter：第一次检查（BUY）通过，
        后续检查全部拒绝。SELL 信号不调用过滤器，因此仍能成交。
        """
        quotes = _make_valid_quotes()
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [
            Signal(d[0], "000001", Side.BUY, 100, "买入"),
            Signal(d[1], "000001", Side.SELL, 100, "卖出"),
        ]
        uf = BuyOnceThenRejectFilter()
        result = _run(
            quotes, signals, BacktestConfig(initial_cash=100000),
            universe_filter=uf,
        )

        buy_fills = [f for f in result.fills if f.side == Side.BUY]
        sell_fills = [f for f in result.fills if f.side == Side.SELL]
        assert len(buy_fills) == 1, "BUY 应通过第一次过滤检查并成交"
        assert len(sell_fills) == 1, "SELL 不经过过滤，应成交退出持仓"

    def test_filter_not_called_for_sell(self):
        """SELL 信号不应触发 UniverseFilter 调用。"""
        quotes = _make_valid_quotes()
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [
            Signal(d[0], "000001", Side.BUY, 100),
            Signal(d[1], "000001", Side.SELL, 100),
        ]
        uf = BuyOnceThenRejectFilter()
        _run(
            quotes, signals, BacktestConfig(initial_cash=100000),
            universe_filter=uf,
        )

        # BUY 信号调用 1 次，SELL 信号不调用
        assert uf.call_count == 1, (
            f"SELL 不应调用过滤器，期望 call_count=1，实际 {uf.call_count}"
        )


# ------------------------------------------------------------------ #
# 5. 调用日期只能是信号日，不能读取未来成交日数据
# ------------------------------------------------------------------ #
class TestFilterUsesSignalDate:
    """UniverseFilter 调用时传入的日期必须是信号日，不能是成交日。"""

    def test_filter_called_with_signal_date(self):
        """过滤器收到的日期 = signal.signal_date，≠ fill_date。"""
        quotes = _make_valid_quotes()
        d = make_trade_dates(date(2024, 1, 2), 10)
        signal_date = d[0]
        expected_fill_date = d[1]

        signals = [Signal(signal_date, "000001", Side.BUY, 100)]
        uf = AlwaysAllowUniverseFilter()
        _run(
            quotes, signals, BacktestConfig(initial_cash=100000),
            universe_filter=uf,
        )

        assert uf.call_count == 1
        called_symbol, called_date = uf.called_with[0]
        assert called_date == signal_date, (
            f"过滤器应收到信号日 {signal_date}，实际收到 {called_date}"
        )
        assert called_date != expected_fill_date, (
            "过滤器不应收到成交日（未来数据）"
        )

    def test_filter_date_matches_signal_not_fill(self):
        """多信号场景：每个信号日的过滤器调用日期必须匹配信号日。"""
        quotes = _make_valid_quotes()
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [
            Signal(d[0], "000001", Side.BUY, 100),
            Signal(d[2], "000001", Side.BUY, 100),
        ]
        uf = AlwaysAllowUniverseFilter()
        _run(
            quotes, signals, BacktestConfig(initial_cash=100000),
            universe_filter=uf,
        )

        assert uf.call_count == 2
        assert uf.called_with[0][1] == d[0]
        assert uf.called_with[1][1] == d[2]


# ------------------------------------------------------------------ #
# 6. JSON、Markdown、orders DataFrame 包含过滤原因
# ------------------------------------------------------------------ #
class TestReportsIncludeFilterReason:
    """JSON、Markdown、orders DataFrame 均包含 UNIVERSE_FILTERED 过滤原因。"""

    @pytest.fixture
    def filtered_result(self):
        quotes = _make_low_lot_quotes()
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [Signal(d[0], "000001", Side.BUY, 100, "filtered")]
        return _run(quotes, signals, BacktestConfig(initial_cash=100000))

    def test_json_report_contains_filter_reason(self, filtered_result):
        report_gen = ReportGenerator()
        report = report_gen.generate_json(
            filtered_result, to_decimal(100000)
        )

        orders = report["orders"]
        assert len(orders) == 1
        assert orders[0]["reject_reason"] == "UNIVERSE_FILTERED"
        assert orders[0]["reject_detail"] is not None
        assert "股票池过滤" in orders[0]["reject_detail"]

    def test_markdown_report_contains_filter_reason(self, filtered_result):
        report_gen = ReportGenerator()
        md = report_gen.generate_markdown(
            filtered_result, to_decimal(100000)
        )

        assert "UNIVERSE_FILTERED" in md
        assert "股票池过滤" in md

    def test_orders_dataframe_contains_filter_reason(self, filtered_result):
        report_gen = ReportGenerator()
        df = report_gen.generate_orders_dataframe(filtered_result)

        assert len(df) == 1
        assert df.iloc[0]["reject_reason"] == "UNIVERSE_FILTERED"
        assert df.iloc[0]["reject_detail"] is not None
        assert "股票池过滤" in df.iloc[0]["reject_detail"]


# ------------------------------------------------------------------ #
# 7. 同一输入重复运行结果一致
# ------------------------------------------------------------------ #
class TestReproducibility:
    """同一输入重复运行结果一致。"""

    def test_repeated_run_same_result(self):
        quotes = _make_low_lot_quotes()
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [Signal(d[0], "000001", Side.BUY, 100, "repro")]

        result1 = _run(quotes, signals, BacktestConfig(initial_cash=100000))
        result2 = _run(quotes, signals, BacktestConfig(initial_cash=100000))

        # 订单数和成交数一致
        assert len(result1.orders) == len(result2.orders)
        assert len(result1.fills) == len(result2.fills)

        # 状态和拒绝原因一致
        for o1, o2 in zip(result1.orders, result2.orders):
            assert o1.status == o2.status
            assert o1.reject_reason == o2.reject_reason

        # 最终权益一致
        eq1 = result1.daily_equity[-1].total_equity
        eq2 = result2.daily_equity[-1].total_equity
        assert eq1 == eq2

        # 内容哈希一致
        assert result1.content_hash == result2.content_hash

    def test_repeated_run_with_custom_filter_same_result(self):
        quotes = _make_valid_quotes()
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [Signal(d[0], "000001", Side.BUY, 100)]

        uf1 = AlwaysRejectUniverseFilter()
        uf2 = AlwaysRejectUniverseFilter()
        result1 = _run(
            quotes, signals, BacktestConfig(initial_cash=100000),
            universe_filter=uf1,
        )
        result2 = _run(
            quotes, signals, BacktestConfig(initial_cash=100000),
            universe_filter=uf2,
        )

        assert len(result1.orders) == len(result2.orders)
        assert len(result1.fills) == len(result2.fills)
        assert result1.content_hash == result2.content_hash
        assert uf1.call_count == uf2.call_count


# ------------------------------------------------------------------ #
# 8. 期末取消规则：过滤信号在最后交易日无下一交易日时按期末取消
# ------------------------------------------------------------------ #
class TestFilteredSignalLastDayCancellation:
    """过滤信号在最后交易日无下一交易日时按期末取消规则处理。"""

    def test_last_day_signal_cancelled_not_filtered(self):
        """最后交易日的 BUY 信号：无下一交易日 -> CANCELLED（期末取消）。

        此场景在 UniverseFilter 之前判断，因为 planned_fill_date 不存在。
        """
        quotes = _make_low_lot_quotes()
        d = make_trade_dates(date(2024, 1, 2), 10)
        # 最后一天发出信号
        signals = [Signal(d[-1], "000001", Side.BUY, 100, "last day")]
        result = _run(quotes, signals, BacktestConfig(initial_cash=100000))

        assert result.orders[0].status == OrderStatus.CANCELLED
        assert result.orders[0].reject_detail is not None
        assert "期末取消" in result.orders[0].reject_detail
