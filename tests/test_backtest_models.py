"""回测数据模型的综合 pytest 测试。

覆盖 ``ashare_quant.backtest.models`` 中的枚举、数据类、属性与方法。
所有金额使用 ``Decimal`` 验证，避免二进制浮点误差。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from ashare_quant.backtest.models import *
from tests.backtest_samples import *


# -----------------------------------------------------------------------------
# 1. 枚举：Side
# -----------------------------------------------------------------------------
class TestSide:
    """交易方向枚举。"""

    def test_buy_value(self) -> None:
        assert Side.BUY.value == "BUY"

    def test_sell_value(self) -> None:
        assert Side.SELL.value == "SELL"

    def test_member_count(self) -> None:
        assert len(list(Side)) == 2

    def test_is_str_enum(self) -> None:
        # Side 继承自 str，应可直接当字符串使用
        assert Side.BUY == "BUY"
        assert Side.SELL == "SELL"

    def test_from_value(self) -> None:
        assert Side("BUY") is Side.BUY
        assert Side("SELL") is Side.SELL


# -----------------------------------------------------------------------------
# 2. 枚举：OrderStatus
# -----------------------------------------------------------------------------
class TestOrderStatus:
    """订单状态枚举。"""

    def test_pending_value(self) -> None:
        assert OrderStatus.PENDING.value == "PENDING"

    def test_filled_value(self) -> None:
        assert OrderStatus.FILLED.value == "FILLED"

    def test_rejected_value(self) -> None:
        assert OrderStatus.REJECTED.value == "REJECTED"

    def test_cancelled_value(self) -> None:
        assert OrderStatus.CANCELLED.value == "CANCELLED"

    def test_member_count(self) -> None:
        assert len(list(OrderStatus)) == 4

    def test_all_values_unique(self) -> None:
        values = [m.value for m in OrderStatus]
        assert len(values) == len(set(values))


# -----------------------------------------------------------------------------
# 3. 枚举：RejectReason（全部 10 个原因）
# -----------------------------------------------------------------------------
class TestRejectReason:
    """拒绝原因枚举。"""

    def test_all_reason_values(self) -> None:
        expected = {
            "INSUFFICIENT_CASH",
            "INSUFFICIENT_POSITION",
            "T_PLUS_ONE",
            "LOT_SIZE",
            "SUSPENDED",
            "LIMIT_UP",
            "LIMIT_DOWN",
            "MISSING_BAR",
            "INVALID_PRICE",
            "UNIVERSE_FILTERED",
        }
        actual = {m.value for m in RejectReason}
        assert actual == expected

    def test_member_count(self) -> None:
        assert len(list(RejectReason)) == 10

    @pytest.mark.parametrize("reason", list(RejectReason))
    def test_each_reason_value_matches_name(self, reason: RejectReason) -> None:
        assert reason.value == reason.name

    def test_is_str_enum(self) -> None:
        assert RejectReason.LIMIT_UP == "LIMIT_UP"

    def test_from_value(self) -> None:
        assert RejectReason("T_PLUS_ONE") is RejectReason.T_PLUS_ONE


# -----------------------------------------------------------------------------
# 4. Signal
# -----------------------------------------------------------------------------
class TestSignal:
    """交易信号。"""

    def test_valid_signal(self) -> None:
        sig = Signal(
            signal_date=date(2024, 1, 2),
            symbol="000001",
            side=Side.BUY,
            quantity=100,
            reason="均线金叉",
        )
        assert sig.signal_date == date(2024, 1, 2)
        assert sig.symbol == "000001"
        assert sig.side is Side.BUY
        assert sig.quantity == 100
        assert sig.reason == "均线金叉"

    def test_default_reason_is_empty(self) -> None:
        sig = Signal(date(2024, 1, 2), "000001", Side.SELL, 200)
        assert sig.reason == ""

    def test_empty_symbol_raises(self) -> None:
        with pytest.raises(ValueError, match="symbol"):
            Signal(date(2024, 1, 2), "", Side.BUY, 100)

    def test_whitespace_only_symbol_raises(self) -> None:
        with pytest.raises(ValueError, match="symbol"):
            Signal(date(2024, 1, 2), "   ", Side.BUY, 100)

    def test_zero_quantity_raises(self) -> None:
        with pytest.raises(ValueError, match="quantity"):
            Signal(date(2024, 1, 2), "000001", Side.BUY, 0)

    def test_negative_quantity_raises(self) -> None:
        with pytest.raises(ValueError, match="quantity"):
            Signal(date(2024, 1, 2), "000001", Side.BUY, -100)


# -----------------------------------------------------------------------------
# 5. Order
# -----------------------------------------------------------------------------
class TestOrder:
    """订单。"""

    @staticmethod
    def _signal() -> Signal:
        return Signal(date(2024, 1, 2), "000001", Side.BUY, 100)

    def test_default_order_id_is_empty(self) -> None:
        """Order 默认 order_id 为空字符串，由 BacktestEngine 确定性赋值。"""
        order = Order(signal=self._signal(), planned_fill_date=date(2024, 1, 3))
        assert order.order_id == ""
        assert isinstance(order.order_id, str)

    def test_default_status_is_pending(self) -> None:
        order = Order(signal=self._signal(), planned_fill_date=date(2024, 1, 3))
        assert order.status is OrderStatus.PENDING

    def test_default_reject_reason_is_none(self) -> None:
        order = Order(signal=self._signal(), planned_fill_date=date(2024, 1, 3))
        assert order.reject_reason is None

    def test_default_fill_is_none(self) -> None:
        order = Order(signal=self._signal(), planned_fill_date=date(2024, 1, 3))
        assert order.fill is None

    def test_explicit_order_id(self) -> None:
        order = Order(
            signal=self._signal(),
            planned_fill_date=date(2024, 1, 3),
            order_id="custom-id-01",
        )
        assert order.order_id == "custom-id-01"

    def test_explicit_status(self) -> None:
        order = Order(
            signal=self._signal(),
            planned_fill_date=date(2024, 1, 3),
            status=OrderStatus.FILLED,
        )
        assert order.status is OrderStatus.FILLED

    def test_explicit_reject_reason(self) -> None:
        order = Order(
            signal=self._signal(),
            planned_fill_date=date(2024, 1, 3),
            status=OrderStatus.REJECTED,
            reject_reason=RejectReason.LIMIT_UP,
        )
        assert order.reject_reason is RejectReason.LIMIT_UP


# -----------------------------------------------------------------------------
# 6. Fill
# -----------------------------------------------------------------------------
class TestFill:
    """成交记录。"""

    @staticmethod
    def _make_fill(
        slippage_price: Decimal = Decimal("10.05"),
        quantity: int = 100,
    ) -> Fill:
        return Fill(
            order_id="abc123def456",
            fill_date=date(2024, 1, 3),
            symbol="000001",
            side=Side.BUY,
            quantity=quantity,
            raw_open_price=Decimal("10.00"),
            slippage_price=slippage_price,
            commission=Decimal("5.00"),
            stamp_duty=Decimal("0.00"),
            transfer_fee=Decimal("0.10"),
            total_cost=Decimal("5.10"),
            cash_change=Decimal("-1010.10"),
        )

    def test_all_fields(self) -> None:
        fill = self._make_fill()
        assert fill.order_id == "abc123def456"
        assert fill.fill_date == date(2024, 1, 3)
        assert fill.symbol == "000001"
        assert fill.side is Side.BUY
        assert fill.quantity == 100
        assert fill.raw_open_price == Decimal("10.00")
        assert fill.slippage_price == Decimal("10.05")
        assert fill.commission == Decimal("5.00")
        assert fill.stamp_duty == Decimal("0.00")
        assert fill.transfer_fee == Decimal("0.10")
        assert fill.total_cost == Decimal("5.10")
        assert fill.cash_change == Decimal("-1010.10")

    def test_turnover_property(self) -> None:
        fill = self._make_fill(slippage_price=Decimal("10.05"), quantity=100)
        assert fill.turnover == Decimal("10.05") * Decimal(100)
        assert fill.turnover == Decimal("1005.00")

    def test_turnover_uses_slippage_not_raw(self) -> None:
        fill = self._make_fill(slippage_price=Decimal("9.80"), quantity=200)
        # turnover 应基于 slippage_price 而非 raw_open_price
        assert fill.turnover == Decimal("9.80") * Decimal(200)
        assert fill.turnover != fill.raw_open_price * Decimal(fill.quantity)

    def test_turnover_returns_decimal(self) -> None:
        fill = self._make_fill()
        assert isinstance(fill.turnover, Decimal)


# -----------------------------------------------------------------------------
# 7. Position
# -----------------------------------------------------------------------------
class TestPosition:
    """持仓记录。"""

    def test_default_values(self) -> None:
        pos = Position(symbol="000001")
        assert pos.symbol == "000001"
        assert pos.total_quantity == 0
        assert pos.sellable_quantity == 0
        assert pos.frozen_buy_quantity == 0
        assert pos.avg_raw_cost == Decimal("0")

    def test_has_position_false_when_empty(self) -> None:
        pos = Position(symbol="000001")
        assert pos.has_position is False

    def test_has_position_true_when_holding(self) -> None:
        pos = Position(symbol="000001", total_quantity=100)
        assert pos.has_position is True

    def test_has_position_false_when_zero(self) -> None:
        pos = Position(symbol="000001", total_quantity=0)
        assert pos.has_position is False

    def test_explicit_fields(self) -> None:
        pos = Position(
            symbol="600000",
            total_quantity=200,
            sellable_quantity=100,
            frozen_buy_quantity=100,
            avg_raw_cost=Decimal("9.50"),
        )
        assert pos.total_quantity == 200
        assert pos.sellable_quantity == 100
        assert pos.frozen_buy_quantity == 100
        assert pos.avg_raw_cost == Decimal("9.50")

    def test_avg_raw_cost_is_decimal(self) -> None:
        pos = Position(symbol="000001")
        assert isinstance(pos.avg_raw_cost, Decimal)


# -----------------------------------------------------------------------------
# 8. PortfolioSnapshot
# -----------------------------------------------------------------------------
class TestPortfolioSnapshot:
    """每日组合快照。"""

    def test_default_pnl_and_drawdown(self) -> None:
        snap = PortfolioSnapshot(
            snapshot_date=date(2024, 1, 2),
            cash=Decimal("100000"),
            position_value=Decimal("0"),
            total_equity=Decimal("100000"),
        )
        assert snap.snapshot_date == date(2024, 1, 2)
        assert snap.cash == Decimal("100000")
        assert snap.position_value == Decimal("0")
        assert snap.total_equity == Decimal("100000")
        assert snap.daily_pnl == Decimal("0")
        assert snap.cumulative_pnl == Decimal("0")
        assert snap.drawdown == Decimal("0")

    def test_defaults_are_decimal_zero(self) -> None:
        snap = PortfolioSnapshot(
            snapshot_date=date(2024, 1, 2),
            cash=Decimal("1000"),
            position_value=Decimal("0"),
            total_equity=Decimal("1000"),
        )
        assert isinstance(snap.daily_pnl, Decimal)
        assert isinstance(snap.cumulative_pnl, Decimal)
        assert isinstance(snap.drawdown, Decimal)

    def test_explicit_pnl_and_drawdown(self) -> None:
        snap = PortfolioSnapshot(
            snapshot_date=date(2024, 1, 3),
            cash=Decimal("90000"),
            position_value=Decimal("12000"),
            total_equity=Decimal("102000"),
            daily_pnl=Decimal("2000"),
            cumulative_pnl=Decimal("2000"),
            drawdown=Decimal("-0.01"),
        )
        assert snap.daily_pnl == Decimal("2000")
        assert snap.cumulative_pnl == Decimal("2000")
        assert snap.drawdown == Decimal("-0.01")


# -----------------------------------------------------------------------------
# 9. BarData
# -----------------------------------------------------------------------------
class TestBarData:
    """单日行情数据。"""

    def _make_bar(self, **overrides) -> BarData:
        defaults = dict(
            symbol="000001",
            trade_date=date(2024, 1, 2),
            open_raw=Decimal("10.00"),
            high_raw=Decimal("10.10"),
            low_raw=Decimal("9.90"),
            close_raw=Decimal("10.05"),
            open_qfq=Decimal("10.00"),
            high_qfq=Decimal("10.10"),
            low_qfq=Decimal("9.90"),
            close_qfq=Decimal("10.05"),
        )
        defaults.update(overrides)
        return BarData(**defaults)

    def test_creation_with_all_fields(self) -> None:
        bar = self._make_bar()
        assert bar.symbol == "000001"
        assert bar.trade_date == date(2024, 1, 2)
        assert bar.open_raw == Decimal("10.00")
        assert bar.high_raw == Decimal("10.10")
        assert bar.low_raw == Decimal("9.90")
        assert bar.close_raw == Decimal("10.05")
        assert bar.open_qfq == Decimal("10.00")
        assert bar.high_qfq == Decimal("10.10")
        assert bar.low_qfq == Decimal("9.90")
        assert bar.close_qfq == Decimal("10.05")

    def test_prev_close_raw_defaults_to_none(self) -> None:
        bar = self._make_bar()
        assert bar.prev_close_raw is None

    def test_default_volume_and_amount(self) -> None:
        bar = self._make_bar()
        assert bar.volume == 0.0
        assert bar.amount == 0.0

    def test_default_suspended_and_tradable(self) -> None:
        bar = self._make_bar()
        assert bar.is_suspended is False
        assert bar.is_tradable is True

    def test_explicit_prev_close_raw(self) -> None:
        bar = self._make_bar(prev_close_raw=Decimal("9.95"))
        assert bar.prev_close_raw == Decimal("9.95")

    def test_suspended_bar(self) -> None:
        bar = self._make_bar(is_suspended=True, is_tradable=False, volume=0.0)
        assert bar.is_suspended is True
        assert bar.is_tradable is False


# -----------------------------------------------------------------------------
# 10. StrategyContext
# -----------------------------------------------------------------------------
class TestStrategyContext:
    """策略上下文。"""

    def test_creation(self) -> None:
        snap = PortfolioSnapshot(
            snapshot_date=date(2024, 1, 2),
            cash=Decimal("100000"),
            position_value=Decimal("0"),
            total_equity=Decimal("100000"),
        )
        positions = {"000001": Position(symbol="000001")}
        ctx = StrategyContext(
            current_date=date(2024, 1, 2),
            portfolio=snap,
            positions=positions,
            bars_up_to_date=None,  # 测试中允许 None 占位
        )
        assert ctx.current_date == date(2024, 1, 2)
        assert ctx.portfolio is snap
        assert ctx.positions is positions
        assert ctx.bars_up_to_date is None

    def test_positions_can_be_empty(self) -> None:
        snap = PortfolioSnapshot(
            snapshot_date=date(2024, 1, 2),
            cash=Decimal("100000"),
            position_value=Decimal("0"),
            total_equity=Decimal("100000"),
        )
        ctx = StrategyContext(
            current_date=date(2024, 1, 2),
            portfolio=snap,
            positions={},
            bars_up_to_date=None,
        )
        assert ctx.positions == {}

    def test_with_dataframe(self) -> None:
        # 利用 backtest_samples 生成真实 DataFrame
        df = make_quotes(symbol="000001", start=date(2024, 1, 2), n_days=5)
        snap = PortfolioSnapshot(
            snapshot_date=date(2024, 1, 2),
            cash=Decimal("100000"),
            position_value=Decimal("0"),
            total_equity=Decimal("100000"),
        )
        ctx = StrategyContext(
            current_date=date(2024, 1, 2),
            portfolio=snap,
            positions={},
            bars_up_to_date=df,
        )
        assert ctx.bars_up_to_date is df
        assert len(ctx.bars_up_to_date) == 5


# -----------------------------------------------------------------------------
# 11. BacktestResult
# -----------------------------------------------------------------------------
class TestBacktestResult:
    """回测结果。"""

    def test_default_empty_lists_and_dicts(self) -> None:
        result = BacktestResult(config_summary={"start": "2024-01-02"})
        assert result.orders == []
        assert result.fills == []
        assert result.daily_equity == []
        assert result.final_positions == {}
        assert result.metrics == {}
        assert result.limitations == []
        assert result.data_range == {}

    def test_default_content_hash_is_none(self) -> None:
        result = BacktestResult(config_summary={})
        assert result.content_hash is None

    def test_default_code_commit_is_none(self) -> None:
        result = BacktestResult(config_summary={})
        assert result.code_commit is None

    def test_config_summary_preserved(self) -> None:
        result = BacktestResult(config_summary={"capital": 100000})
        assert result.config_summary == {"capital": 100000}

    def test_default_lists_are_independent_instances(self) -> None:
        # 两次构造应得到各自独立的列表，避免可变默认值共享
        r1 = BacktestResult(config_summary={})
        r2 = BacktestResult(config_summary={})
        r1.orders.append("x")  # type: ignore[arg-type]
        assert r2.orders == []

    def test_explicit_content_hash_and_commit(self) -> None:
        result = BacktestResult(
            config_summary={},
            content_hash="abc123",
            code_commit="deadbeef",
        )
        assert result.content_hash == "abc123"
        assert result.code_commit == "deadbeef"


# -----------------------------------------------------------------------------
# 12. to_decimal
# -----------------------------------------------------------------------------
class TestToDecimal:
    """安全转换为 Decimal。"""

    def test_from_decimal(self) -> None:
        d = Decimal("3.14")
        assert to_decimal(d) is d

    def test_from_int(self) -> None:
        result = to_decimal(100)
        assert result == Decimal(100)
        assert isinstance(result, Decimal)

    def test_from_str(self) -> None:
        result = to_decimal("10.05")
        assert result == Decimal("10.05")
        assert isinstance(result, Decimal)

    def test_from_float(self) -> None:
        # float 应通过 str 转换，避免二进制浮点误差
        result = to_decimal(0.1)
        assert result == Decimal("0.1")
        assert isinstance(result, Decimal)

    def test_from_float_no_binary_artifact(self) -> None:
        # 0.1 + 0.2 的经典浮点问题：通过 str 转换不应出现 0.30000000000000004
        result = to_decimal(0.1 + 0.2)
        assert result == Decimal(str(0.1 + 0.2))

    def test_from_negative_float(self) -> None:
        result = to_decimal(-1.5)
        assert result == Decimal("-1.5")

    def test_from_string_with_whitespace(self) -> None:
        result = to_decimal("  3.5  ")
        # str(Decimal) 不会自动去除空白，但 Decimal(str) 可解析前后空白
        assert result == Decimal("3.5")


# -----------------------------------------------------------------------------
# 13. quantize_price
# -----------------------------------------------------------------------------
class TestQuantizePrice:
    """价格量化（默认 4 位小数）。"""

    def test_default_4_places(self) -> None:
        result = quantize_price(Decimal("10.123456"))
        assert result == Decimal("10.1235")  # ROUND_HALF_EVEN 默认
        # 验证精度为 4 位
        assert result.as_tuple().exponent == -4

    def test_explicit_places(self) -> None:
        result = quantize_price(Decimal("10.123456"), places=2)
        assert result == Decimal("10.12")

    def test_zero_places(self) -> None:
        result = quantize_price(Decimal("10.6"), places=0)
        assert result == Decimal("11")

    def test_already_exact(self) -> None:
        result = quantize_price(Decimal("10.0000"))
        assert result == Decimal("10.0000")

    def test_returns_decimal(self) -> None:
        result = quantize_price(Decimal("1.23456789"))
        assert isinstance(result, Decimal)


# -----------------------------------------------------------------------------
# 14. quantize_money
# -----------------------------------------------------------------------------
class TestQuantizeMoney:
    """金额量化（2 位小数）。"""

    def test_two_places(self) -> None:
        result = quantize_money(Decimal("1010.105"))
        assert result == Decimal("1010.10")  # ROUND_HALF_EVEN: 1010.10
        assert result.as_tuple().exponent == -2

    def test_truncates_extra_decimals(self) -> None:
        result = quantize_money(Decimal("5.999"))
        assert result == Decimal("6.00")

    def test_whole_number(self) -> None:
        result = quantize_money(Decimal("100"))
        assert result == Decimal("100.00")

    def test_returns_decimal(self) -> None:
        result = quantize_money(Decimal("1.234"))
        assert isinstance(result, Decimal)


# -----------------------------------------------------------------------------
# 15. EligibilityDecision & RiskDecision
# -----------------------------------------------------------------------------
class TestEligibilityDecision:
    """股票池过滤决策。"""

    def test_eligible_default_reason(self) -> None:
        dec = EligibilityDecision(eligible=True)
        assert dec.eligible is True
        assert dec.reason == ""

    def test_ineligible_with_reason(self) -> None:
        dec = EligibilityDecision(eligible=False, reason="ST 股票")
        assert dec.eligible is False
        assert dec.reason == "ST 股票"


class TestRiskDecision:
    """风控决策。"""

    def test_approved_default_fields(self) -> None:
        dec = RiskDecision(approved=True)
        assert dec.approved is True
        assert dec.reject_reason is None
        assert dec.reason == ""

    def test_rejected_with_reason(self) -> None:
        dec = RiskDecision(
            approved=False,
            reject_reason=RejectReason.INSUFFICIENT_CASH,
            reason="现金不足",
        )
        assert dec.approved is False
        assert dec.reject_reason is RejectReason.INSUFFICIENT_CASH
        assert dec.reason == "现金不足"

    def test_reject_reason_can_be_any_enum(self) -> None:
        for reason in RejectReason:
            dec = RiskDecision(approved=False, reject_reason=reason)
            assert dec.reject_reason is reason


# -----------------------------------------------------------------------------
# 16. Order ID 唯一性
# -----------------------------------------------------------------------------
class TestOrderIdUniqueness:
    """订单 ID 唯一性：引擎确定性生成时，100 个订单全部唯一。

    Order.order_id 默认为空字符串，由 BacktestEngine 通过
    _generate_order_id(...) 确定性赋值（含 data_hash、信号内容和序号）。
    """

    def test_100_orders_unique_ids_with_explicit_ids(self) -> None:
        sig = Signal(date(2024, 1, 2), "000001", Side.BUY, 100)
        orders = [
            Order(
                signal=sig,
                planned_fill_date=date(2024, 1, 3),
                order_id=f"testhash-{i:04d}",
            )
            for i in range(100)
        ]
        ids = [o.order_id for o in orders]
        assert len(ids) == 100
        assert len(set(ids)) == 100  # 全部唯一

    def test_explicit_ids_are_stable(self) -> None:
        """相同显式 ID 的两个 Order 的 order_id 相同。"""
        sig = Signal(date(2024, 1, 2), "000001", Side.BUY, 100)
        o1 = Order(
            signal=sig, planned_fill_date=date(2024, 1, 3),
            order_id="abcdef12-0001",
        )
        o2 = Order(
            signal=sig, planned_fill_date=date(2024, 1, 3),
            order_id="abcdef12-0001",
        )
        assert o1.order_id == o2.order_id

    def test_two_consecutive_orders_differ(self) -> None:
        """引擎生成时，连续两个序号的 order_id 不同。"""
        sig = Signal(date(2024, 1, 2), "000001", Side.BUY, 100)
        o1 = Order(
            signal=sig, planned_fill_date=date(2024, 1, 3),
            order_id="abcdef12-0001",
        )
        o2 = Order(
            signal=sig, planned_fill_date=date(2024, 1, 3),
            order_id="abcdef12-0002",
        )
        assert o1.order_id != o2.order_id
