"""AShareBrokerSimulator 的综合 pytest 测试。

覆盖范围：
- ``check_rejection``：缺失行情、无效价格、停牌/不可交易、手数、T+1/持仓不足、涨跌停
- ``execute``：买入/卖出费用手工核算、费率置零、滑点方向、大额佣金超过最低值、拒绝时返回 None
- 涨跌停价计算（``_calc_limit_prices``）与板块比例选择（``_select_limit_ratio``）
- tick 取整辅助方法（``_round_up_to_tick`` / ``_round_down_to_tick``）

所有金额断言使用 ``Decimal`` 精确比较，行情数据由 ``tests.backtest_samples`` 合成。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from ashare_quant.backtest.broker import AShareBrokerSimulator
from ashare_quant.backtest.config import (
    BacktestConfig,
    CommissionConfig,
    LimitConfig,
    SlippageConfig,
    StampDutyConfig,
    TransferFeeConfig,
)
from ashare_quant.backtest.models import *  # noqa: F401,F403  —— 按规范要求使用 star import
from ashare_quant.backtest.models import to_decimal  # 显式便于类型提示
from tests.backtest_samples import *  # noqa: F401,F403  —— 按规范要求使用 star import
from tests.backtest_samples import make_bar, make_limit_down_bar, make_limit_up_bar


# --------------------------------------------------------------------------- #
# 辅助构建函数
# --------------------------------------------------------------------------- #
DEFAULT_SYMBOL = "000001"
DEFAULT_DATE = date(2024, 1, 3)


def build_config(
    *,
    initial_cash: float = 1000.0,
    lot_size: int = 100,
    commission_rate: float = 0.0003,
    commission_minimum: float = 5.0,
    stamp_duty_rate: float = 0.001,
    transfer_fee_rate: float = 0.00001,
    slippage_bps: float = 10.0,
    slippage_tick: float = 0.01,
    main_ratio: float = 0.10,
    star_ratio: float = 0.20,
    szse_ratio: float = 0.10,
    bjse_ratio: float = 0.30,
    limit_tick: float = 0.01,
) -> BacktestConfig:
    """构建 BacktestConfig，允许覆盖任意费率/阈值（默认即规范默认值）。"""
    return BacktestConfig(
        initial_cash=initial_cash,
        lot_size=lot_size,
        commission=CommissionConfig(rate=commission_rate, minimum=commission_minimum),
        stamp_duty=StampDutyConfig(rate=stamp_duty_rate),
        transfer_fee=TransferFeeConfig(rate=transfer_fee_rate),
        slippage=SlippageConfig(bps=slippage_bps, tick_size=slippage_tick),
        limit=LimitConfig(
            main_ratio=main_ratio,
            star_ratio=star_ratio,
            szse_ratio=szse_ratio,
            bjse_ratio=bjse_ratio,
            tick_size=limit_tick,
        ),
    )


def bar_from_dict(d: dict, prev_close_raw: float | None = None) -> BarData:
    """将 ``make_bar`` 系列函数返回的 dict 转为 ``BarData``，可附带 ``prev_close_raw``。"""
    return BarData(
        symbol=d["symbol"],
        trade_date=d["trade_date"],
        open_raw=to_decimal(d["open_raw"]),
        high_raw=to_decimal(d["high_raw"]),
        low_raw=to_decimal(d["low_raw"]),
        close_raw=to_decimal(d["close_raw"]),
        open_qfq=to_decimal(d["open_qfq"]),
        high_qfq=to_decimal(d["high_qfq"]),
        low_qfq=to_decimal(d["low_qfq"]),
        close_qfq=to_decimal(d["close_qfq"]),
        volume=d.get("volume", 0.0),
        amount=d.get("amount", 0.0),
        is_suspended=d.get("is_suspended", False),
        is_tradable=d.get("is_tradable", True),
        prev_close_raw=to_decimal(prev_close_raw) if prev_close_raw is not None else None,
    )


def build_bar(
    *,
    symbol: str = DEFAULT_SYMBOL,
    dt: date = DEFAULT_DATE,
    open_price: float = 10.0,
    prev_close_raw: float | None = None,
    is_suspended: bool = False,
    is_tradable: bool = True,
) -> BarData:
    """构建一个常规 ``BarData``（开盘价可复用前收以跳过涨跌停校验）。"""
    d = make_bar(
        symbol=symbol,
        dt=dt,
        open_price=open_price,
        is_suspended=is_suspended,
        is_tradable=is_tradable,
    )
    return bar_from_dict(d, prev_close_raw=prev_close_raw)


def build_limit_up_bar(
    *,
    symbol: str = DEFAULT_SYMBOL,
    dt: date = DEFAULT_DATE,
    prev_close: float = 10.0,
    ratio: float = 0.10,
) -> BarData:
    """构建涨停 ``BarData``（开盘价=涨停价），并设置 ``prev_close_raw``。"""
    d = make_limit_up_bar(symbol=symbol, dt=dt, prev_close=prev_close, ratio=ratio)
    return bar_from_dict(d, prev_close_raw=prev_close)


def build_limit_down_bar(
    *,
    symbol: str = DEFAULT_SYMBOL,
    dt: date = DEFAULT_DATE,
    prev_close: float = 10.0,
    ratio: float = 0.10,
) -> BarData:
    """构建跌停 ``BarData``（开盘价=跌停价），并设置 ``prev_close_raw``。"""
    d = make_limit_down_bar(symbol=symbol, dt=dt, prev_close=prev_close, ratio=ratio)
    return bar_from_dict(d, prev_close_raw=prev_close)


def build_signal(
    *,
    symbol: str = DEFAULT_SYMBOL,
    side: Side = Side.BUY,
    quantity: int = 100,
    signal_date: date = date(2024, 1, 2),
    reason: str = "test",
) -> Signal:
    return Signal(signal_date=signal_date, symbol=symbol, side=side, quantity=quantity, reason=reason)


def build_order(
    *,
    symbol: str = DEFAULT_SYMBOL,
    side: Side = Side.BUY,
    quantity: int = 100,
    planned_fill_date: date = DEFAULT_DATE,
) -> Order:
    sig = build_signal(symbol=symbol, side=side, quantity=quantity)
    return Order(signal=sig, planned_fill_date=planned_fill_date)


def build_position(
    *,
    symbol: str = DEFAULT_SYMBOL,
    total: int = 0,
    sellable: int = 0,
    frozen: int = 0,
    avg_cost: Decimal = Decimal("0"),
) -> Position:
    return Position(
        symbol=symbol,
        total_quantity=total,
        sellable_quantity=sellable,
        frozen_buy_quantity=frozen,
        avg_raw_cost=avg_cost,
    )


def build_portfolio(
    *,
    cash: Decimal = Decimal("1000.0"),
    position_value: Decimal = Decimal("0"),
    snapshot_date: date = DEFAULT_DATE,
) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        snapshot_date=snapshot_date,
        cash=cash,
        position_value=position_value,
        total_equity=cash + position_value,
    )


@pytest.fixture
def broker() -> AShareBrokerSimulator:
    """无状态成交模拟器实例。"""
    return AShareBrokerSimulator()


@pytest.fixture
def config() -> BacktestConfig:
    """默认回测配置（initial_cash=1000、lot_size=100、万三佣金最低5元、千一印花税、
    万零点一过户费、10bps 滑点、tick=0.01）。"""
    return build_config()


# --------------------------------------------------------------------------- #
# check_rejection
# --------------------------------------------------------------------------- #
class TestCheckRejection:
    """订单拒绝校验：按 check_rejection 内部检查顺序逐项覆盖。"""

    def test_missing_bar_rejected(self, broker, config):
        order = build_order(side=Side.BUY, quantity=100)
        decision = broker.check_rejection(order, None, build_portfolio(), config, {})
        assert decision is not None
        assert decision.approved is False
        assert decision.reject_reason is RejectReason.MISSING_BAR

    def test_invalid_price_zero_rejected(self, broker, config):
        bar = build_bar(open_price=0.0)
        order = build_order(side=Side.BUY, quantity=100)
        decision = broker.check_rejection(order, bar, build_portfolio(), config, {})
        assert decision is not None
        assert decision.reject_reason is RejectReason.INVALID_PRICE

    def test_invalid_price_negative_rejected(self, broker, config):
        bar = build_bar(open_price=-1.0)
        order = build_order(side=Side.BUY, quantity=100)
        decision = broker.check_rejection(order, bar, build_portfolio(), config, {})
        assert decision is not None
        assert decision.reject_reason is RejectReason.INVALID_PRICE

    def test_suspended_rejected(self, broker, config):
        bar = build_bar(open_price=10.0, is_suspended=True)
        order = build_order(side=Side.BUY, quantity=100)
        decision = broker.check_rejection(order, bar, build_portfolio(), config, {})
        assert decision is not None
        assert decision.reject_reason is RejectReason.SUSPENDED

    def test_not_tradable_rejected(self, broker, config):
        bar = build_bar(open_price=10.0, is_tradable=False)
        order = build_order(side=Side.BUY, quantity=100)
        decision = broker.check_rejection(order, bar, build_portfolio(), config, {})
        assert decision is not None
        assert decision.reject_reason is RejectReason.SUSPENDED

    @pytest.mark.parametrize("quantity", [99, 150])
    def test_lot_size_violation_rejected(self, broker, config, quantity):
        bar = build_bar(open_price=10.0)
        order = build_order(side=Side.BUY, quantity=quantity)
        decision = broker.check_rejection(order, bar, build_portfolio(), config, {})
        assert decision is not None
        assert decision.reject_reason is RejectReason.LOT_SIZE

    @pytest.mark.parametrize("quantity", [100, 200])
    def test_lot_size_valid_passes(self, broker, config, quantity):
        bar = build_bar(open_price=10.0)
        order = build_order(side=Side.BUY, quantity=quantity)
        decision = broker.check_rejection(order, bar, build_portfolio(), config, {})
        assert decision is None

    def test_sell_no_position_rejected(self, broker, config):
        bar = build_bar(open_price=10.0)
        order = build_order(side=Side.SELL, quantity=100)
        decision = broker.check_rejection(order, bar, build_portfolio(), config, {})
        assert decision is not None
        assert decision.reject_reason is RejectReason.INSUFFICIENT_POSITION

    def test_sell_t_plus_one_rejected(self, broker, config):
        # 有持仓但可卖为 0（当日买入受 T+1 限制）
        bar = build_bar(open_price=10.0)
        order = build_order(side=Side.SELL, quantity=100)
        positions = {DEFAULT_SYMBOL: build_position(total=100, sellable=0)}
        decision = broker.check_rejection(order, bar, build_portfolio(), config, positions)
        assert decision is not None
        assert decision.reject_reason is RejectReason.T_PLUS_ONE

    def test_sell_insufficient_position_rejected(self, broker, config):
        # 可卖数量小于订单数量
        bar = build_bar(open_price=10.0)
        order = build_order(side=Side.SELL, quantity=100)
        positions = {DEFAULT_SYMBOL: build_position(total=100, sellable=50)}
        decision = broker.check_rejection(order, bar, build_portfolio(), config, positions)
        assert decision is not None
        assert decision.reject_reason is RejectReason.INSUFFICIENT_POSITION

    def test_sell_sufficient_position_passes(self, broker, config):
        bar = build_bar(open_price=10.0, prev_close_raw=10.0)
        order = build_order(side=Side.SELL, quantity=100)
        positions = {DEFAULT_SYMBOL: build_position(total=100, sellable=100)}
        decision = broker.check_rejection(order, bar, build_portfolio(), config, positions)
        assert decision is None

    def test_buy_limit_up_rejected(self, broker, config):
        # make_limit_up_bar(prev_close=10.0, ratio=0.10) -> open=11.00 = 涨停价
        bar = build_limit_up_bar(prev_close=10.0, ratio=0.10)
        order = build_order(side=Side.BUY, quantity=100)
        decision = broker.check_rejection(order, bar, build_portfolio(), config, {})
        assert decision is not None
        assert decision.reject_reason is RejectReason.LIMIT_UP

    def test_sell_limit_down_rejected(self, broker, config):
        # 跌停价拒绝卖出（需先通过持仓校验）
        bar = build_limit_down_bar(prev_close=10.0, ratio=0.10)
        order = build_order(side=Side.SELL, quantity=100)
        positions = {DEFAULT_SYMBOL: build_position(total=100, sellable=100)}
        decision = broker.check_rejection(order, bar, build_portfolio(), config, positions)
        assert decision is not None
        assert decision.reject_reason is RejectReason.LIMIT_DOWN

    def test_buy_at_limit_down_passes(self, broker, config):
        # 跌停允许买入（保守撮合：仅涨停拒买、跌停拒卖）
        bar = build_limit_down_bar(prev_close=10.0, ratio=0.10)
        order = build_order(side=Side.BUY, quantity=100)
        decision = broker.check_rejection(order, bar, build_portfolio(), config, {})
        assert decision is None

    def test_sell_at_limit_up_passes(self, broker, config):
        # 涨停允许卖出
        bar = build_limit_up_bar(prev_close=10.0, ratio=0.10)
        order = build_order(side=Side.SELL, quantity=100)
        positions = {DEFAULT_SYMBOL: build_position(total=100, sellable=100)}
        decision = broker.check_rejection(order, bar, build_portfolio(), config, positions)
        assert decision is None

    def test_no_prev_close_skips_limit_check(self, broker, config):
        # 缺少 prev_close_raw 时跳过涨跌停校验，正常订单不被拒绝（返回 None）
        bar = build_bar(open_price=10.0, prev_close_raw=None)
        order = build_order(side=Side.BUY, quantity=100)
        decision = broker.check_rejection(order, bar, build_portfolio(), config, {})
        assert decision is None


# --------------------------------------------------------------------------- #
# execute
# --------------------------------------------------------------------------- #
class TestExecute:
    """成交撮合：手工核算买卖费用、滑点方向、费率置零、大额佣金、拒绝返回 None。"""

    def test_execute_buy_calculation(self, broker, config):
        # BUY 100 @ open_raw=10.0, bps=10, tick=0.01
        bar = build_bar(open_price=10.0, prev_close_raw=10.0)
        order = build_order(side=Side.BUY, quantity=100)
        fill = broker.execute(order, bar, build_portfolio(), config, {})

        assert fill is not None
        assert fill.side is Side.BUY
        assert fill.symbol == DEFAULT_SYMBOL
        assert fill.quantity == 100
        assert fill.fill_date == DEFAULT_DATE
        assert fill.order_id == order.order_id

        # 滑点后价格 = round_up_to_tick(10.0 * 1.001, 0.01) = 10.01
        assert fill.slippage_price == Decimal("10.01")
        assert fill.raw_open_price == Decimal("10.0000")
        # 成交额 = 10.01 * 100 = 1001.00
        assert fill.turnover == Decimal("1001.00")
        # 佣金 = max(1001 * 0.0003, 5) = 5.00（最低佣金生效）
        assert fill.commission == Decimal("5.00")
        # 买入无印花税
        assert fill.stamp_duty == Decimal("0.00")
        # 过户费 = 1001 * 0.00001 = 0.01
        assert fill.transfer_fee == Decimal("0.01")
        # 总费用 = 5.00 + 0 + 0.01 = 5.01
        assert fill.total_cost == Decimal("5.01")
        # 现金变化 = -(1001.00 + 5.01) = -1006.01
        assert fill.cash_change == Decimal("-1006.01")

    def test_execute_sell_calculation(self, broker, config):
        # SELL 100 @ open_raw=10.0, bps=10, tick=0.01
        bar = build_bar(open_price=10.0, prev_close_raw=10.0)
        order = build_order(side=Side.SELL, quantity=100)
        positions = {DEFAULT_SYMBOL: build_position(total=100, sellable=100)}
        fill = broker.execute(order, bar, build_portfolio(), config, positions)

        assert fill is not None
        assert fill.side is Side.SELL
        # 滑点后价格 = round_down_to_tick(10.0 * 0.999, 0.01) = 9.99
        assert fill.slippage_price == Decimal("9.99")
        assert fill.raw_open_price == Decimal("10.0000")
        # 成交额 = 9.99 * 100 = 999.00
        assert fill.turnover == Decimal("999.00")
        # 佣金 = max(999 * 0.0003, 5) = 5.00
        assert fill.commission == Decimal("5.00")
        # 印花税 = 999 * 0.001 = 1.00（仅卖出）
        assert fill.stamp_duty == Decimal("1.00")
        # 过户费 = 999 * 0.00001 = 0.01
        assert fill.transfer_fee == Decimal("0.01")
        # 总费用 = 5.00 + 1.00 + 0.01 = 6.01
        assert fill.total_cost == Decimal("6.01")
        # 现金变化 = 999.00 - 6.01 = 992.99
        assert fill.cash_change == Decimal("992.99")

    def test_execute_zero_fees(self, broker):
        # 费率全部置 0：佣金 rate=0/minimum=0、印花税=0、过户费=0
        zero_config = build_config(
            commission_rate=0.0,
            commission_minimum=0.0,
            stamp_duty_rate=0.0,
            transfer_fee_rate=0.0,
        )
        bar = build_bar(open_price=10.0, prev_close_raw=10.0)

        # BUY：现金变化 = -成交额
        buy_fill = broker.execute(
            build_order(side=Side.BUY, quantity=100), bar, build_portfolio(), zero_config, {}
        )
        assert buy_fill is not None
        assert buy_fill.commission == Decimal("0.00")
        assert buy_fill.stamp_duty == Decimal("0.00")
        assert buy_fill.transfer_fee == Decimal("0.00")
        assert buy_fill.total_cost == Decimal("0.00")
        assert buy_fill.cash_change == -buy_fill.turnover
        assert buy_fill.cash_change == Decimal("-1001.00")

        # SELL：现金变化 = +成交额
        positions = {DEFAULT_SYMBOL: build_position(total=100, sellable=100)}
        sell_fill = broker.execute(
            build_order(side=Side.SELL, quantity=100), bar, build_portfolio(), zero_config, positions
        )
        assert sell_fill is not None
        assert sell_fill.commission == Decimal("0.00")
        assert sell_fill.stamp_duty == Decimal("0.00")
        assert sell_fill.transfer_fee == Decimal("0.00")
        assert sell_fill.total_cost == Decimal("0.00")
        assert sell_fill.cash_change == sell_fill.turnover
        assert sell_fill.cash_change == Decimal("999.00")

    def test_slippage_direction_buy(self, broker, config):
        # 买入滑点向不利方向：滑点价 > 原始开盘价
        bar = build_bar(open_price=10.0, prev_close_raw=10.0)
        fill = broker.execute(
            build_order(side=Side.BUY, quantity=100), bar, build_portfolio(), config, {}
        )
        assert fill is not None
        assert fill.slippage_price > fill.raw_open_price

    def test_slippage_direction_sell(self, broker, config):
        # 卖出滑点向不利方向：滑点价 < 原始开盘价
        bar = build_bar(open_price=10.0, prev_close_raw=10.0)
        positions = {DEFAULT_SYMBOL: build_position(total=100, sellable=100)}
        fill = broker.execute(
            build_order(side=Side.SELL, quantity=100), bar, build_portfolio(), config, positions
        )
        assert fill is not None
        assert fill.slippage_price < fill.raw_open_price

    def test_large_turnover_commission_exceeds_minimum(self, broker, config):
        # 10000 股 @ 10.0 -> 成交额 ~100100，佣金 = 100100*0.0003 = 30.03 > 5.00
        bar = build_bar(open_price=10.0, prev_close_raw=10.0)
        fill = broker.execute(
            build_order(side=Side.BUY, quantity=10000), bar, build_portfolio(), config, {}
        )
        assert fill is not None
        assert fill.turnover == Decimal("100100.00")
        assert fill.commission == Decimal("30.03")

    def test_small_turnover_uses_minimum_commission(self, broker, config):
        # 1000 股 @ 10.0 -> 成交额 ~10010，佣金 = 10010*0.0003 = 3.003 < 5.00 -> 取最低 5.00
        bar = build_bar(open_price=10.0, prev_close_raw=10.0)
        fill = broker.execute(
            build_order(side=Side.BUY, quantity=1000), bar, build_portfolio(), config, {}
        )
        assert fill is not None
        assert fill.turnover == Decimal("10010.00")
        assert fill.commission == Decimal("5.00")

    def test_execute_returns_none_when_suspended(self, broker, config):
        bar = build_bar(open_price=10.0, is_suspended=True)
        fill = broker.execute(
            build_order(side=Side.BUY, quantity=100), bar, build_portfolio(), config, {}
        )
        assert fill is None

    def test_execute_returns_none_when_lot_size_violation(self, broker, config):
        bar = build_bar(open_price=10.0)
        fill = broker.execute(
            build_order(side=Side.BUY, quantity=99), bar, build_portfolio(), config, {}
        )
        assert fill is None

    def test_execute_returns_none_when_limit_up_buy(self, broker, config):
        bar = build_limit_up_bar(prev_close=10.0, ratio=0.10)
        fill = broker.execute(
            build_order(side=Side.BUY, quantity=100), bar, build_portfolio(), config, {}
        )
        assert fill is None

    def test_execute_returns_none_when_sell_no_position(self, broker, config):
        bar = build_bar(open_price=10.0, prev_close_raw=10.0)
        fill = broker.execute(
            build_order(side=Side.SELL, quantity=100), bar, build_portfolio(), config, {}
        )
        assert fill is None


# --------------------------------------------------------------------------- #
# 涨跌停价计算
# --------------------------------------------------------------------------- #
class TestLimitPrices:
    """``_calc_limit_prices``：按板块比例计算涨停/跌停价并按 tick 取整。"""

    def test_calc_limit_prices_main_board(self, broker):
        # 60xxxx 主板 10%：prev_close=10 -> (11.00, 9.00)
        up, down = broker._calc_limit_prices(Decimal("10.0"), 0.10, 0.01)
        assert up == Decimal("11.00")
        assert down == Decimal("9.00")

    def test_calc_limit_prices_star(self, broker):
        # 688xxx 科创板 20%：prev_close=10 -> (12.00, 8.00)
        up, down = broker._calc_limit_prices(Decimal("10.0"), 0.20, 0.01)
        assert up == Decimal("12.00")
        assert down == Decimal("8.00")

    def test_calc_limit_prices_szse(self, broker):
        # 00xxxx 深市主板 10%：prev_close=10 -> (11.00, 9.00)
        up, down = broker._calc_limit_prices(Decimal("10.0"), 0.10, 0.01)
        assert up == Decimal("11.00")
        assert down == Decimal("9.00")

    def test_calc_limit_prices_bjse(self, broker):
        # 430xxx 北交所 30%：prev_close=10 -> (13.00, 7.00)
        up, down = broker._calc_limit_prices(Decimal("10.0"), 0.30, 0.01)
        assert up == Decimal("13.00")
        assert down == Decimal("7.00")

    def test_calc_limit_prices_rounds_up_and_down_to_tick(self, broker):
        # prev_close=10.005, 10%：up_raw=11.0055 -> 向上取整 11.01；down_raw=9.0045 -> 向下取整 9.00
        up, down = broker._calc_limit_prices(Decimal("10.005"), 0.10, 0.01)
        assert up == Decimal("11.01")
        assert down == Decimal("9.00")


# --------------------------------------------------------------------------- #
# 板块涨跌停比例选择
# --------------------------------------------------------------------------- #
class TestSelectLimitRatio:
    """``_select_limit_ratio``：按代码前缀识别板块并返回对应比例。"""

    @pytest.mark.parametrize(
        "symbol,expected",
        [
            ("600000", 0.10),  # 沪市主板 60xxxx
            ("688001", 0.20),  # 科创板 688xxx
            ("000001", 0.10),  # 深市主板 00xxxx
            ("300001", 0.10),  # 创业板 30xxxx（深市比例）
            ("430001", 0.30),  # 北交所 430xxx
            ("830001", 0.30),  # 北交所 83xxxx
            ("870001", 0.30),  # 北交所 87xxxx
            ("920001", 0.30),  # 北交所 920xxx
            ("999999", 0.10),  # 未知代码 -> 默认主板比例
        ],
    )
    def test_select_limit_ratio(self, config, symbol, expected):
        ratio = AShareBrokerSimulator._select_limit_ratio(symbol, config)
        assert ratio == pytest.approx(expected)


# --------------------------------------------------------------------------- #
# tick 取整辅助方法
# --------------------------------------------------------------------------- #
class TestTickRounding:
    """``_round_up_to_tick`` / ``_round_down_to_tick``：向不利方向取整到 tick 整数倍。"""

    def test_round_up_to_tick_exact(self, broker):
        assert broker._round_up_to_tick(Decimal("10.01"), 0.01) == Decimal("10.01")

    def test_round_up_to_tick_fractional_up(self, broker):
        # 10.005 / 0.01 = 1000.5 -> CEILING 1001 -> 10.01
        assert broker._round_up_to_tick(Decimal("10.005"), 0.01) == Decimal("10.01")

    def test_round_up_to_tick_above_tick(self, broker):
        # 10.014 / 0.01 = 1001.4 -> CEILING 1002 -> 10.02
        assert broker._round_up_to_tick(Decimal("10.014"), 0.01) == Decimal("10.02")

    def test_round_up_to_tick_whole_number(self, broker):
        assert broker._round_up_to_tick(Decimal("10.0"), 0.01) == Decimal("10.00")

    def test_round_down_to_tick_exact(self, broker):
        assert broker._round_down_to_tick(Decimal("9.99"), 0.01) == Decimal("9.99")

    def test_round_down_to_tick_fractional_down(self, broker):
        # 9.995 / 0.01 = 999.5 -> FLOOR 999 -> 9.99
        assert broker._round_down_to_tick(Decimal("9.995"), 0.01) == Decimal("9.99")

    def test_round_down_to_tick_above_tick(self, broker):
        # 10.014 / 0.01 = 1001.4 -> FLOOR 1001 -> 10.01
        assert broker._round_down_to_tick(Decimal("10.014"), 0.01) == Decimal("10.01")

    def test_round_up_to_tick_zero_tick_returns_unchanged(self, broker):
        price = Decimal("10.005")
        assert broker._round_up_to_tick(price, 0) == price

    def test_round_down_to_tick_zero_tick_returns_unchanged(self, broker):
        price = Decimal("9.995")
        assert broker._round_down_to_tick(price, 0) == price

    def test_round_up_to_tick_negative_tick_returns_unchanged(self, broker):
        price = Decimal("10.005")
        assert broker._round_up_to_tick(price, -0.01) == price

    def test_round_down_to_tick_negative_tick_returns_unchanged(self, broker):
        price = Decimal("9.995")
        assert broker._round_down_to_tick(price, -0.01) == price
