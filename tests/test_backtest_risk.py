"""DefaultRiskManager 的综合 pytest 测试。

覆盖范围（按任务规约校验顺序）：
1. 信号基本字段（signal_date / symbol / quantity）
2. 手数（lot_size 整数倍）
3. 缺失行情（bar=None）
4. 单标的持仓限制
5. 最大持仓市值比例
6. 现金充足性（买入）
7. 可卖数量（卖出 T+1 / 持仓不足）
8. 停牌 / 不可交易
9. 涨跌停（涨停拒买、跌停拒卖）
10. 无效价格
11. 全部校验通过

所有金额断言使用 ``Decimal`` 精确比较，行情数据由 ``tests.backtest_samples`` 合成。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

import pytest

from ashare_quant.backtest.config import (
    BacktestConfig,
    CommissionConfig,
    LimitConfig,
    RiskConfig,
    SlippageConfig,
    StampDutyConfig,
    TransferFeeConfig,
    UniverseConfig,
)
from ashare_quant.backtest.models import *  # noqa: F401,F403  —— 按规范要求使用 star import
from ashare_quant.backtest.models import to_decimal  # 显式便于类型提示
from ashare_quant.backtest.risk import DefaultRiskManager
from tests.backtest_samples import *  # noqa: F401,F403  —— 按规范要求使用 star import
from tests.backtest_samples import make_bar, make_limit_down_bar, make_limit_up_bar


# --------------------------------------------------------------------------- #
# 辅助构建函数
# --------------------------------------------------------------------------- #
DEFAULT_SYMBOL = "000001"
DEFAULT_DATE = date(2024, 1, 3)
DEFAULT_SIGNAL_DATE = date(2024, 1, 2)


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
    enable_single_position_limit: bool = True,
    max_position_value_ratio: float = 1.0,
    min_lot_value: float = 1000.0,
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
        risk=RiskConfig(
            enable_single_position_limit=enable_single_position_limit,
            max_position_value_ratio=max_position_value_ratio,
        ),
        universe=UniverseConfig(min_lot_value=min_lot_value),
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
    """构建一个常规 ``BarData``（默认 prev_close_raw=None 以跳过涨跌停校验）。"""
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
    signal_date: Optional[date] = DEFAULT_SIGNAL_DATE,
    reason: str = "test",
) -> Signal:
    """构建交易信号。

    ``signal_date`` 允许为 ``None`` 以测试基本字段校验（``Signal.__post_init__``
    不校验 signal_date，故可直接构造）。
    """
    return Signal(
        signal_date=signal_date,
        symbol=symbol,
        side=side,
        quantity=quantity,
        reason=reason,
    )


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
    cash: Decimal = Decimal("100000"),
    position_value: Decimal = Decimal("0"),
    snapshot_date: date = DEFAULT_DATE,
) -> PortfolioSnapshot:
    """构建组合快照，``total_equity`` 自动取 ``cash + position_value``。"""
    return PortfolioSnapshot(
        snapshot_date=snapshot_date,
        cash=cash,
        position_value=position_value,
        total_equity=cash + position_value,
    )


@pytest.fixture
def risk_manager() -> DefaultRiskManager:
    """无状态风控管理器实例。"""
    return DefaultRiskManager()


@pytest.fixture
def config() -> BacktestConfig:
    """默认回测配置（万三佣金最低5元、千一印花税、lot_size=100、单持仓限制开启、
    max_position_value_ratio=1.0）。"""
    return build_config()


# --------------------------------------------------------------------------- #
# 1. 信号基本字段校验
# --------------------------------------------------------------------------- #
class TestBasicFieldValidation:
    """信号基本字段：signal_date / symbol / quantity。"""

    def test_none_signal_date_rejected(self, risk_manager, config):
        # signal_date=None：Signal 构造不校验该字段，直接传入
        sig = build_signal(signal_date=None)
        decision = risk_manager.validate(sig, build_portfolio(), build_bar(), config, {})
        assert decision.approved is False
        assert decision.reject_reason is None
        assert "signal_date" in decision.reason

    def test_empty_symbol_rejected(self, risk_manager, config):
        # symbol=""：Signal.__post_init__ 会拒绝空 symbol，故构造合法信号后改写字段
        sig = build_signal()
        sig.symbol = ""
        decision = risk_manager.validate(sig, build_portfolio(), build_bar(), config, {})
        assert decision.approved is False
        assert decision.reject_reason is None
        assert "symbol" in decision.reason

    def test_whitespace_symbol_rejected(self, risk_manager, config):
        sig = build_signal()
        sig.symbol = "   "
        decision = risk_manager.validate(sig, build_portfolio(), build_bar(), config, {})
        assert decision.approved is False
        assert decision.reject_reason is None
        assert "symbol" in decision.reason

    @pytest.mark.parametrize("quantity", [0, -100])
    def test_non_positive_quantity_rejected(self, risk_manager, config, quantity):
        # quantity<=0：构造合法信号后改写为非正值
        sig = build_signal()
        sig.quantity = quantity
        decision = risk_manager.validate(sig, build_portfolio(), build_bar(), config, {})
        assert decision.approved is False
        assert decision.reject_reason is None
        assert "quantity" in decision.reason


# --------------------------------------------------------------------------- #
# 2. 手数校验
# --------------------------------------------------------------------------- #
class TestLotSize:
    """数量必须为 lot_size 的整数倍。"""

    @pytest.mark.parametrize("quantity", [99, 150])
    def test_lot_size_violation_rejected(self, risk_manager, config, quantity):
        # 99 与 150 均非 100 的整数倍
        sig = build_signal(quantity=quantity)
        decision = risk_manager.validate(sig, build_portfolio(), build_bar(), config, {})
        assert decision.approved is False
        assert decision.reject_reason is RejectReason.LOT_SIZE

    @pytest.mark.parametrize("quantity", [100, 200])
    def test_lot_size_valid_passes(self, risk_manager, quantity):
        # 100、200 为 100 的整数倍；提供充足现金并关闭单持仓限制以隔离手数检查
        cfg = build_config(enable_single_position_limit=False)
        sig = build_signal(quantity=quantity)
        decision = risk_manager.validate(
            sig, build_portfolio(), build_bar(), cfg, {}
        )
        assert decision.approved is True


# --------------------------------------------------------------------------- #
# 3. 缺失行情（bar=None）
# --------------------------------------------------------------------------- #
class TestMissingBar:
    """bar=None 时返回 MISSING_BAR（仅当 basic 与 lot_size 通过后）。"""

    def test_buy_missing_bar_rejected(self, risk_manager, config):
        sig = build_signal(side=Side.BUY, quantity=100)
        decision = risk_manager.validate(sig, build_portfolio(), None, config, {})
        assert decision.approved is False
        assert decision.reject_reason is RejectReason.MISSING_BAR

    def test_sell_missing_bar_rejected(self, risk_manager, config):
        sig = build_signal(side=Side.SELL, quantity=100)
        decision = risk_manager.validate(sig, build_portfolio(), None, config, {})
        assert decision.approved is False
        assert decision.reject_reason is RejectReason.MISSING_BAR


# --------------------------------------------------------------------------- #
# 4. 单标的持仓限制
# --------------------------------------------------------------------------- #
class TestSinglePositionLimit:
    """买入时若已持有其他标的则拒绝（开关开启时）。"""

    def test_buying_different_symbol_rejected(self, risk_manager, config):
        # 默认 enable_single_position_limit=True，已持有 000002 再买 000001
        positions = {
            "000002": build_position(symbol="000002", total=100, sellable=100)
        }
        sig = build_signal(symbol="000001", side=Side.BUY, quantity=100)
        decision = risk_manager.validate(
            sig, build_portfolio(), build_bar(), config, positions
        )
        assert decision.approved is False
        assert decision.reject_reason is None
        assert "单持仓限制" in decision.reason

    def test_single_position_limit_disabled_passes(self, risk_manager):
        # 关闭单持仓限制后，持有 000002 仍可买入 000001
        cfg = build_config(enable_single_position_limit=False)
        positions = {
            "000002": build_position(symbol="000002", total=100, sellable=100)
        }
        sig = build_signal(symbol="000001", side=Side.BUY, quantity=100)
        decision = risk_manager.validate(
            sig, build_portfolio(), build_bar(), cfg, positions
        )
        assert decision.approved is True

    def test_buying_same_symbol_passes(self, risk_manager, config):
        # 买入已持仓的同一标的不触发单持仓限制
        positions = {
            DEFAULT_SYMBOL: build_position(symbol=DEFAULT_SYMBOL, total=100, sellable=100)
        }
        sig = build_signal(symbol=DEFAULT_SYMBOL, side=Side.BUY, quantity=100)
        decision = risk_manager.validate(
            sig, build_portfolio(), build_bar(), config, positions
        )
        assert decision.approved is True


# --------------------------------------------------------------------------- #
# 5. 最大持仓市值比例
# --------------------------------------------------------------------------- #
class TestPositionRatioLimit:
    """买入后持仓市值占总权益比例不得超过阈值。"""

    def test_ratio_exceeded_rejected(self, risk_manager):
        # max_position_value_ratio=0.5：买入 1000 元 / 权益 1500 = 66.67% > 50%
        cfg = build_config(
            enable_single_position_limit=False,
            max_position_value_ratio=0.5,
        )
        portfolio = build_portfolio(
            cash=Decimal("1500"), position_value=Decimal("0")
        )
        sig = build_signal(side=Side.BUY, quantity=100)
        decision = risk_manager.validate(
            sig, portfolio, build_bar(), cfg, {}
        )
        assert decision.approved is False
        assert decision.reject_reason is RejectReason.INSUFFICIENT_CASH
        assert "持仓比例超限" in decision.reason

    def test_default_ratio_passes(self, risk_manager, config):
        # 默认 max_position_value_ratio=1.0：1000/2000=50% <= 100%
        portfolio = build_portfolio(
            cash=Decimal("2000"), position_value=Decimal("0")
        )
        sig = build_signal(side=Side.BUY, quantity=100)
        decision = risk_manager.validate(
            sig, portfolio, build_bar(), config, {}
        )
        assert decision.approved is True


# --------------------------------------------------------------------------- #
# 6. 现金充足性（买入）
# --------------------------------------------------------------------------- #
class TestCashSufficiency:
    """买入现金充足性：成交额(按 open_raw 估算) + 佣金(应用最低值)。"""

    def test_insufficient_cash_rejected(self, risk_manager, config):
        # 100 股 @ open_raw=10.0 -> 需 1000 + 5(最低佣金) = 1005；现金 500 不足
        # 注：为隔离现金检查，total_equity 设为 2000 使持仓比例(50%)通过
        portfolio = build_portfolio(
            cash=Decimal("500"), position_value=Decimal("1500")
        )
        sig = build_signal(side=Side.BUY, quantity=100)
        decision = risk_manager.validate(
            sig, portfolio, build_bar(open_price=10.0), config, {}
        )
        assert decision.approved is False
        assert decision.reject_reason is RejectReason.INSUFFICIENT_CASH
        assert "现金不足" in decision.reason

    def test_sufficient_cash_passes(self, risk_manager, config):
        # 现金 2000 >= 1005，通过
        portfolio = build_portfolio(
            cash=Decimal("2000"), position_value=Decimal("0")
        )
        sig = build_signal(side=Side.BUY, quantity=100)
        decision = risk_manager.validate(
            sig, portfolio, build_bar(open_price=10.0), config, {}
        )
        assert decision.approved is True


# --------------------------------------------------------------------------- #
# 7. 可卖数量（卖出）
# --------------------------------------------------------------------------- #
class TestSellableQuantity:
    """卖出可卖数量检查：T+1 冻结与持仓不足。"""

    def test_no_position_rejected(self, risk_manager, config):
        sig = build_signal(side=Side.SELL, quantity=100)
        decision = risk_manager.validate(
            sig, build_portfolio(), build_bar(), config, {}
        )
        assert decision.approved is False
        assert decision.reject_reason is RejectReason.INSUFFICIENT_POSITION

    def test_t_plus_one_rejected(self, risk_manager, config):
        # 总持仓 100 但可卖 0（当日买入受 T+1 限制）
        positions = {DEFAULT_SYMBOL: build_position(total=100, sellable=0)}
        sig = build_signal(side=Side.SELL, quantity=100)
        decision = risk_manager.validate(
            sig, build_portfolio(), build_bar(), config, positions
        )
        assert decision.approved is False
        assert decision.reject_reason is RejectReason.T_PLUS_ONE

    def test_sell_more_than_sellable_rejected(self, risk_manager, config):
        # 可卖 100，卖出 200：总持仓 100 < 200 -> 持仓不足
        positions = {DEFAULT_SYMBOL: build_position(total=100, sellable=100)}
        sig = build_signal(side=Side.SELL, quantity=200)
        decision = risk_manager.validate(
            sig, build_portfolio(), build_bar(), config, positions
        )
        assert decision.approved is False
        assert decision.reject_reason is RejectReason.INSUFFICIENT_POSITION

    def test_sell_exact_sellable_passes(self, risk_manager, config):
        # 可卖 100，卖出 100，通过
        positions = {DEFAULT_SYMBOL: build_position(total=100, sellable=100)}
        sig = build_signal(side=Side.SELL, quantity=100)
        decision = risk_manager.validate(
            sig, build_portfolio(), build_bar(), config, positions
        )
        assert decision.approved is True


# --------------------------------------------------------------------------- #
# 8. 停牌 / 不可交易
# --------------------------------------------------------------------------- #
class TestSuspended:
    """停牌或不可交易时拒绝。"""

    def test_suspended_rejected(self, risk_manager):
        # BUY 路径需先通过现金/比例检查，故提供充足现金并关闭单持仓限制
        cfg = build_config(enable_single_position_limit=False)
        sig = build_signal(side=Side.BUY, quantity=100)
        bar = build_bar(open_price=10.0, is_suspended=True, prev_close_raw=None)
        decision = risk_manager.validate(sig, build_portfolio(), bar, cfg, {})
        assert decision.approved is False
        assert decision.reject_reason is RejectReason.SUSPENDED

    def test_not_tradable_rejected(self, risk_manager):
        cfg = build_config(enable_single_position_limit=False)
        sig = build_signal(side=Side.BUY, quantity=100)
        bar = build_bar(
            open_price=10.0, is_suspended=False, is_tradable=False, prev_close_raw=None
        )
        decision = risk_manager.validate(sig, build_portfolio(), bar, cfg, {})
        assert decision.approved is False
        assert decision.reject_reason is RejectReason.SUSPENDED


# --------------------------------------------------------------------------- #
# 9. 涨跌停
# --------------------------------------------------------------------------- #
class TestLimitUpDown:
    """涨跌停校验：涨停拒绝买入、跌停拒绝卖出；涨停允许卖出、跌停允许买入。"""

    def test_buy_at_limit_up_rejected(self, risk_manager):
        # make_limit_up_bar(prev_close=10.0, ratio=0.10) -> open=11.00 = 涨停价
        cfg = build_config(enable_single_position_limit=False)
        sig = build_signal(side=Side.BUY, quantity=100)
        bar = build_limit_up_bar(prev_close=10.0, ratio=0.10)
        decision = risk_manager.validate(sig, build_portfolio(), bar, cfg, {})
        assert decision.approved is False
        assert decision.reject_reason is RejectReason.LIMIT_UP

    def test_sell_at_limit_down_rejected(self, risk_manager, config):
        # 跌停价拒绝卖出（需先通过持仓校验）
        positions = {DEFAULT_SYMBOL: build_position(total=100, sellable=100)}
        sig = build_signal(side=Side.SELL, quantity=100)
        bar = build_limit_down_bar(prev_close=10.0, ratio=0.10)
        decision = risk_manager.validate(
            sig, build_portfolio(), bar, config, positions
        )
        assert decision.approved is False
        assert decision.reject_reason is RejectReason.LIMIT_DOWN

    def test_sell_at_limit_up_passes(self, risk_manager, config):
        # 涨停允许卖出
        positions = {DEFAULT_SYMBOL: build_position(total=100, sellable=100)}
        sig = build_signal(side=Side.SELL, quantity=100)
        bar = build_limit_up_bar(prev_close=10.0, ratio=0.10)
        decision = risk_manager.validate(
            sig, build_portfolio(), bar, config, positions
        )
        assert decision.approved is True

    def test_buy_at_limit_down_passes(self, risk_manager):
        # 跌停允许买入（保守撮合：仅涨停拒买、跌停拒卖）
        cfg = build_config(enable_single_position_limit=False)
        sig = build_signal(side=Side.BUY, quantity=100)
        bar = build_limit_down_bar(prev_close=10.0, ratio=0.10)
        decision = risk_manager.validate(sig, build_portfolio(), bar, cfg, {})
        assert decision.approved is True

    def test_no_prev_close_skips_limit_check(self, risk_manager):
        # 缺少 prev_close_raw 时跳过涨跌停校验，正常订单通过
        cfg = build_config(enable_single_position_limit=False)
        sig = build_signal(side=Side.BUY, quantity=100)
        bar = build_bar(open_price=10.0, prev_close_raw=None)
        decision = risk_manager.validate(sig, build_portfolio(), bar, cfg, {})
        assert decision.approved is True


# --------------------------------------------------------------------------- #
# 10. 无效价格
# --------------------------------------------------------------------------- #
class TestInvalidPrice:
    """open_raw <= 0 时返回 INVALID_PRICE。"""

    @pytest.mark.parametrize("open_price", [0.0, -1.0])
    def test_invalid_price_rejected(self, risk_manager, open_price):
        # prev_close_raw=None 跳过涨跌停校验，使无效价格检查得以触发
        cfg = build_config(enable_single_position_limit=False)
        sig = build_signal(side=Side.BUY, quantity=100)
        bar = build_bar(open_price=open_price, prev_close_raw=None)
        decision = risk_manager.validate(sig, build_portfolio(), bar, cfg, {})
        assert decision.approved is False
        assert decision.reject_reason is RejectReason.INVALID_PRICE


# --------------------------------------------------------------------------- #
# 11. 全部校验通过
# --------------------------------------------------------------------------- #
class TestAllChecksPass:
    """合法买入信号、现金充足时通过全部风控。"""

    def test_valid_buy_approved(self, risk_manager, config):
        # BUY 100 @ open_raw=10.0, prev_close=10.0（非涨跌停），现金 2000 >= 1005
        portfolio = build_portfolio(
            cash=Decimal("2000"), position_value=Decimal("0")
        )
        sig = build_signal(side=Side.BUY, quantity=100)
        bar = build_bar(open_price=10.0, prev_close_raw=10.0)
        decision = risk_manager.validate(sig, portfolio, bar, config, {})
        assert decision.approved is True
        assert "通过风控" in decision.reason
