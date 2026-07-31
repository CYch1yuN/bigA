"""FR-04 回归测试：资金检查与 Broker 实际扣款公式一致性。

验证修复后的统一成本计算确保：
- 风控预检与实际成交使用完全相同的成本公式
- 买入后现金不可能为负
- 边界案例（1004.50 元）精确拒单
- 现金刚好等于实际总成本时允许成交
- 比实际总成本少 0.01 元时拒绝
- 非零滑点、非零过户费、最低佣金同时存在
- 自定义高滑点配置不得导致负现金
- Broker 最终现金保护作为最后一道防线
- 拒单审计字段 reject_detail 正确记录
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from ashare_quant.backtest.broker import AShareBrokerSimulator
from ashare_quant.backtest.config import BacktestConfig
from ashare_quant.backtest.cost import compute_buy_cost, compute_sell_cost
from ashare_quant.backtest.engine import BacktestEngine
from ashare_quant.backtest.models import (
    BarData,
    Order,
    OrderStatus,
    PortfolioSnapshot,
    Position,
    RejectReason,
    Side,
    Signal,
    to_decimal,
)
from ashare_quant.backtest.risk import DefaultRiskManager
from tests.backtest_samples import make_bar, make_quotes, make_trade_dates


# ------------------------------------------------------------------ #
# 辅助
# ------------------------------------------------------------------ #
def _make_bar_data(open_raw: float = 9.99, prev_close: float = 9.90) -> BarData:
    """构造默认行情 BarData 对象（直接调用 broker/risk 时使用）。"""
    raw = to_decimal(open_raw)
    return BarData(
        symbol="000001",
        trade_date=date(2024, 1, 3),
        open_raw=raw,
        high_raw=to_decimal(open_raw * 1.01),
        low_raw=to_decimal(open_raw * 0.99),
        close_raw=raw,
        open_qfq=raw,
        high_qfq=to_decimal(open_raw * 1.01),
        low_qfq=to_decimal(open_raw * 0.99),
        close_qfq=raw,
        volume=100000,
        amount=100000 * open_raw,
        is_suspended=False,
        is_tradable=True,
        prev_close_raw=to_decimal(prev_close),
    )


def _make_exact_quotes(
    symbol: str = "000001",
    start: date = date(2024, 1, 2),
    n_days: int = 5,
    open_raw: float = 9.99,
    close_price: float | None = None,
) -> pd.DataFrame:
    """生成 open_raw 恒定（无 daily_return 偏移）的行情 DataFrame。

    与 ``make_quotes`` 不同，本函数每天的开盘价均为 ``open_raw``，
    不乘以 0.998 系数，确保边界测试中成本计算精确可控。

    Args:
        open_raw: 每日开盘价（未复权）。
        close_price: 每日收盘价；为 None 时与 open_raw 相同。
            UniverseFilter 使用 close_raw 判断一手金额，当 close_price
            与 open_raw 不同时需显式指定，确保过滤通过。
    """
    close_p = close_price if close_price is not None else open_raw
    dates = make_trade_dates(start, n_days)
    rows = []
    for dt in dates:
        row = make_bar(
            symbol=symbol,
            dt=dt,
            open_price=open_raw,
            high=max(open_raw, close_p) * 1.01,
            low=min(open_raw, close_p) * 0.99,
            close=close_p,
        )
        rows.append(row)
    return pd.DataFrame(rows)


def _make_order(symbol="000001", side=Side.BUY, quantity=100):
    """构造默认订单。"""
    return Order(
        signal=Signal(date(2024, 1, 2), symbol, side, quantity, "test"),
        planned_fill_date=date(2024, 1, 3),
    )


def _make_snapshot(cash):
    """构造组合快照。"""
    cash_dec = to_decimal(cash) if not isinstance(cash, Decimal) else cash
    return PortfolioSnapshot(
        snapshot_date=date(2024, 1, 3),
        cash=cash_dec,
        position_value=Decimal("0"),
        total_equity=cash_dec,
    )


def _run_backtest(quotes, signals, config):
    """运行回测并返回结果。"""
    engine = BacktestEngine()
    from ashare_quant.backtest.strategies import ScriptedStrategy
    dates = make_trade_dates(date(2024, 1, 2), 5)
    return engine.run(
        data=quotes,
        strategy=ScriptedStrategy(signals),
        start_date=dates[0],
        end_date=dates[-1],
        initial_cash=config.initial_cash,
        config=config,
    )


# ------------------------------------------------------------------ #
# 1. 1004.50 边界案例：必须拒单
# ------------------------------------------------------------------ #
class TestBoundary1004Half:
    """FR-04 核心案例：initial_cash=1004.50, open_raw=9.99, qty=100。

    默认配置下精确成本：
      - slippage_price = ceil_to_tick(9.99 * 1.001, 0.01) = 10.00
      - turnover = 10.00 * 100 = 1000.00
      - commission = max(1000 * 0.0003, 5.0) = 5.00
      - transfer_fee = 1000 * 0.00001 = 0.01
      - total_cost = 5.01
      - required_cash = 1000.00 + 5.01 = 1005.01
      - 1004.50 < 1005.01 → 必须拒单
    """

    def test_rejected_not_filled(self):
        """1004.50 元买入 100 股 @9.99 必须被拒绝，fills 为空。"""
        quotes = _make_exact_quotes("000001", date(2024, 1, 2), 5, open_raw=9.99, close_price=10.00)
        d = make_trade_dates(date(2024, 1, 2), 5)
        signals = [Signal(d[0], "000001", Side.BUY, 100, "boundary")]
        config = BacktestConfig(initial_cash=1004.50)
        result = _run_backtest(quotes, signals, config)

        assert len(result.fills) == 0, "1004.50 案例必须拒单，不应有成交"
        assert result.orders[0].status == OrderStatus.REJECTED
        assert result.orders[0].reject_reason == RejectReason.INSUFFICIENT_CASH

    def test_cash_never_negative(self):
        """所有 daily_equity.cash >= 0。"""
        quotes = _make_exact_quotes("000001", date(2024, 1, 2), 5, open_raw=9.99, close_price=10.00)
        d = make_trade_dates(date(2024, 1, 2), 5)
        signals = [Signal(d[0], "000001", Side.BUY, 100, "boundary")]
        config = BacktestConfig(initial_cash=1004.50)
        result = _run_backtest(quotes, signals, config)

        for snap in result.daily_equity:
            assert snap.cash >= Decimal("0"), f"现金不能为负: {snap.cash}"

    def test_reject_detail_recorded(self):
        """拒单订单必须记录 reject_detail。"""
        quotes = _make_exact_quotes("000001", date(2024, 1, 2), 5, open_raw=9.99, close_price=10.00)
        d = make_trade_dates(date(2024, 1, 2), 5)
        signals = [Signal(d[0], "000001", Side.BUY, 100, "boundary")]
        config = BacktestConfig(initial_cash=1004.50)
        result = _run_backtest(quotes, signals, config)

        order = result.orders[0]
        assert order.reject_detail is not None
        assert "现金不足" in order.reject_detail or "INSUFFICIENT" in order.reject_detail.upper()

    def test_reproduce_original_bug_values(self):
        """精确复现原始 Bug 报告中的数值，验证修复后拒单。

        original_cash=1004.50, open_raw=9.99, qty=100
        修复前: cash_change=-1005.01, 现金=-0.51（负数！）
        修复后: 必须拒单，cash_change 不存在
        """
        config = BacktestConfig(initial_cash=1004.50)
        open_raw = to_decimal(9.99)
        cost = compute_buy_cost(open_raw, 100, config)

        # 验证精确成本与原始 Bug 一致
        assert cost.cash_change == Decimal("-1005.01"), (
            f"cash_change 应为 -1005.01, 实际 {cost.cash_change}"
        )
        required = cost.turnover + cost.total_cost
        assert required == Decimal("1005.01"), (
            f"required_cash 应为 1005.01, 实际 {required}"
        )

        # 1004.50 < 1005.01 → 风控必须拒单
        quotes = _make_exact_quotes("000001", date(2024, 1, 2), 5, open_raw=9.99, close_price=10.00)
        d = make_trade_dates(date(2024, 1, 2), 5)
        signals = [Signal(d[0], "000001", Side.BUY, 100, "reproduce")]
        result = _run_backtest(quotes, signals, config)

        assert len(result.fills) == 0
        assert result.orders[0].status == OrderStatus.REJECTED
        assert result.orders[0].reject_reason == RejectReason.INSUFFICIENT_CASH


# ------------------------------------------------------------------ #
# 2. 统一成本计算验证
# ------------------------------------------------------------------ #
class TestUnifiedCostCalculation:
    """验证风控与 Broker 使用同一成本函数。"""

    def test_risk_and_broker_use_same_cost(self):
        """风控 _check_cash 和 broker execute 的成本必须完全一致。"""
        config = BacktestConfig(initial_cash=10000)
        open_raw = to_decimal(9.99)
        quantity = 100

        # 统一成本函数
        cost = compute_buy_cost(open_raw, quantity, config)

        # 风控用同一函数计算 required_cash
        required_cash = cost.turnover + cost.total_cost

        # Broker 实际扣款
        broker = AShareBrokerSimulator()
        bar = _make_bar_data(9.99)
        order = _make_order()
        snapshot = _make_snapshot(10000)
        fill = broker.execute(order, bar, snapshot, config, {})

        assert fill is not None
        actual_required = -(fill.cash_change)
        assert required_cash == actual_required, (
            f"风控估算 {required_cash} != Broker 实际 {actual_required}"
        )

    def test_cost_includes_slippage_transfer_fee_min_commission(self):
        """成本必须包含滑点、过户费和最低佣金。"""
        config = BacktestConfig(initial_cash=10000)
        open_raw = to_decimal(9.99)
        cost = compute_buy_cost(open_raw, 100, config)

        # 滑点后价格 > open_raw（买入向上取整）
        assert cost.slippage_price > open_raw

        # 佣金 = 最低佣金 5.00（成交额约 999.9 * 0.0003 ≈ 0.30 < 5）
        assert cost.commission == Decimal("5.00")

        # 过户费 > 0
        assert cost.transfer_fee > Decimal("0")

        # 现金变化为负
        assert cost.cash_change < Decimal("0")

    def test_sell_cost_includes_stamp_duty(self):
        """卖出成本必须包含印花税。"""
        config = BacktestConfig(initial_cash=10000)
        open_raw = to_decimal(10.0)
        cost = compute_sell_cost(open_raw, 100, config)

        # 印花税 > 0
        assert cost.stamp_duty > Decimal("0")

        # 现金变化为正
        assert cost.cash_change > Decimal("0")


# ------------------------------------------------------------------ #
# 3. 现金刚好等于总成本时允许成交
# ------------------------------------------------------------------ #
class TestExactCashBoundary:
    """现金刚好等于实际总成本时应该成交。"""

    def test_exact_cash_allows_fill(self):
        """现金 = 滑点后成交额 + 总费用 时允许成交。"""
        config = BacktestConfig(initial_cash=10000)
        open_raw = to_decimal(9.99)
        cost = compute_buy_cost(open_raw, 100, config)
        exact_cash = cost.turnover + cost.total_cost

        # 用精确现金构造快照
        bar = _make_bar_data(9.99)
        order = _make_order()
        snapshot = PortfolioSnapshot(
            snapshot_date=date(2024, 1, 3),
            cash=exact_cash,
            position_value=Decimal("0"),
            total_equity=exact_cash,
        )

        broker = AShareBrokerSimulator()
        fill = broker.execute(order, bar, snapshot, config, {})
        assert fill is not None, "现金刚好等于总成本时应该成交"

    def test_one_cent_less_rejects(self):
        """比实际总成本少 0.01 元时应该拒绝。"""
        config = BacktestConfig(initial_cash=10000)
        open_raw = to_decimal(9.99)
        cost = compute_buy_cost(open_raw, 100, config)
        short_cash = cost.turnover + cost.total_cost - Decimal("0.01")

        bar = _make_bar_data(9.99)
        order = _make_order()
        snapshot = PortfolioSnapshot(
            snapshot_date=date(2024, 1, 3),
            cash=short_cash,
            position_value=Decimal("0"),
            total_equity=short_cash,
        )

        broker = AShareBrokerSimulator()
        fill = broker.execute(order, bar, snapshot, config, {})
        assert fill is None, "比总成本少 0.01 元时应该拒绝"


# ------------------------------------------------------------------ #
# 4. 非零滑点 + 非零过户费 + 最低佣金同时存在
# ------------------------------------------------------------------ #
class TestAllFeesPresent:
    """验证滑点、过户费、最低佣金同时存在时不会导致负现金。"""

    def test_high_slippage_no_negative_cash(self):
        """自定义高滑点配置不得导致负现金。"""
        from ashare_quant.backtest.config import SlippageConfig

        config = BacktestConfig(
            initial_cash=1004.50,
            slippage=SlippageConfig(bps=50.0, tick_size=0.01),
        )
        quotes = _make_exact_quotes("000001", date(2024, 1, 2), 5, open_raw=9.99, close_price=10.00)
        d = make_trade_dates(date(2024, 1, 2), 5)
        signals = [Signal(d[0], "000001", Side.BUY, 100, "high slippage")]
        result = _run_backtest(quotes, signals, config)

        # 高滑点更不可能买得起，必须拒单
        assert len(result.fills) == 0
        for snap in result.daily_equity:
            assert snap.cash >= Decimal("0")

    def test_default_config_all_fees_nonzero(self):
        """默认配置下滑点、过户费、最低佣金均非零。"""
        config = BacktestConfig()
        assert config.slippage.bps > 0
        assert config.transfer_fee.rate > 0
        assert config.commission.minimum > 0

    def test_zero_fees_no_negative_cash(self):
        """费率全设为 0 时也不应导致负现金。"""
        from ashare_quant.backtest.config import (
            CommissionConfig,
            StampDutyConfig,
            TransferFeeConfig,
            SlippageConfig,
        )

        config = BacktestConfig(
            initial_cash=999.00,  # 刚好买 100 股 @9.99（零费率）
            commission=CommissionConfig(rate=0.0, minimum=0.0),
            stamp_duty=StampDutyConfig(rate=0.0),
            transfer_fee=TransferFeeConfig(rate=0.0),
            slippage=SlippageConfig(bps=0.0, tick_size=0.01),
        )
        # close_price=10.00 使一手金额=1000>=min_lot_value，UniverseFilter 通过
        quotes = _make_exact_quotes("000001", date(2024, 1, 2), 5, open_raw=9.99, close_price=10.00)
        d = make_trade_dates(date(2024, 1, 2), 5)
        signals = [Signal(d[0], "000001", Side.BUY, 100, "zero fees")]
        result = _run_backtest(quotes, signals, config)

        # 零费率下 999 元买 100 股 @9.99 应该成交
        # slippage_price = ceil_to_tick(9.99, 0.01) = 9.99
        # turnover = 999.00, required = 999.00
        assert len(result.fills) == 1
        for snap in result.daily_equity:
            assert snap.cash >= Decimal("0")


# ------------------------------------------------------------------ #
# 5. Broker 最终现金保护
# ------------------------------------------------------------------ #
class TestBrokerCashProtection:
    """验证 Broker 作为最后一道防线的现金保护。"""

    def test_broker_rejects_when_cash_insufficient(self):
        """即使风控通过，Broker 也应在现金不足时拒单。"""
        config = BacktestConfig(initial_cash=10000)
        open_raw = to_decimal(9.99)
        cost = compute_buy_cost(open_raw, 100, config)
        short_cash = cost.turnover + cost.total_cost - Decimal("1.00")

        bar = _make_bar_data(9.99)
        order = _make_order()
        snapshot = PortfolioSnapshot(
            snapshot_date=date(2024, 1, 3),
            cash=short_cash,
            position_value=Decimal("0"),
            total_equity=short_cash,
        )

        broker = AShareBrokerSimulator()
        fill = broker.execute(order, bar, snapshot, config, {})
        assert fill is None, "Broker 最终现金保护应拒单"

    def test_broker_cash_protection_via_engine(self):
        """通过引擎验证 Broker 保护生效。"""
        quotes = _make_exact_quotes("000001", date(2024, 1, 2), 5, open_raw=9.99, close_price=10.00)
        d = make_trade_dates(date(2024, 1, 2), 5)
        signals = [Signal(d[0], "000001", Side.BUY, 100, "broker protection")]
        config = BacktestConfig(initial_cash=1004.50)
        result = _run_backtest(quotes, signals, config)

        assert len(result.fills) == 0
        for snap in result.daily_equity:
            assert snap.cash >= Decimal("0"), "现金不能为负"


# ------------------------------------------------------------------ #
# 6. 拒单审计字段
# ------------------------------------------------------------------ #
class TestRejectDetailAudit:
    """验证 Order.reject_detail 字段在所有拒单/取消场景中正确记录。"""

    def test_risk_reject_has_detail(self):
        """风控拒单有 reject_detail。"""
        quotes = _make_exact_quotes("000001", date(2024, 1, 2), 5, open_raw=9.99, close_price=10.00)
        d = make_trade_dates(date(2024, 1, 2), 5)
        signals = [Signal(d[0], "000001", Side.BUY, 100, "risk reject")]
        config = BacktestConfig(initial_cash=1004.50)
        result = _run_backtest(quotes, signals, config)

        order = result.orders[0]
        assert order.status == OrderStatus.REJECTED
        assert order.reject_detail is not None
        assert len(order.reject_detail) > 0

    def test_cancelled_has_detail(self):
        """期末取消的订单有 reject_detail。"""
        quotes = make_quotes("000001", date(2024, 1, 2), 3, base_price=10.0)
        d = make_trade_dates(date(2024, 1, 2), 3)
        # 最后一天发出信号，没有下一交易日
        signals = [Signal(d[2], "000001", Side.BUY, 100, "last day")]
        config = BacktestConfig(initial_cash=10000)
        result = _run_backtest(quotes, signals, config)

        cancelled_orders = [o for o in result.orders if o.status == OrderStatus.CANCELLED]
        assert len(cancelled_orders) >= 1
        for o in cancelled_orders:
            assert o.reject_detail is not None
            assert "取消" in o.reject_detail or "cancel" in o.reject_detail.lower()

    def test_reject_detail_in_json_report(self):
        """reject_detail 出现在 JSON 报告中。"""
        from ashare_quant.backtest.report import ReportGenerator

        quotes = _make_exact_quotes("000001", date(2024, 1, 2), 5, open_raw=9.99, close_price=10.00)
        d = make_trade_dates(date(2024, 1, 2), 5)
        signals = [Signal(d[0], "000001", Side.BUY, 100, "json detail")]
        config = BacktestConfig(initial_cash=1004.50)
        result = _run_backtest(quotes, signals, config)

        report_gen = ReportGenerator()
        json_report = report_gen.generate_json(result, to_decimal(1004.50))

        order_dict = json_report["orders"][0]
        assert "reject_detail" in order_dict
        assert order_dict["reject_detail"] is not None

    def test_reject_detail_in_orders_dataframe(self):
        """reject_detail 出现在订单 DataFrame 中。"""
        from ashare_quant.backtest.report import ReportGenerator

        quotes = _make_exact_quotes("000001", date(2024, 1, 2), 5, open_raw=9.99, close_price=10.00)
        d = make_trade_dates(date(2024, 1, 2), 5)
        signals = [Signal(d[0], "000001", Side.BUY, 100, "df detail")]
        config = BacktestConfig(initial_cash=1004.50)
        result = _run_backtest(quotes, signals, config)

        report_gen = ReportGenerator()
        df = report_gen.generate_orders_dataframe(result)

        assert "reject_detail" in df.columns
        row = df.iloc[0]
        assert row["reject_detail"] is not None

    def test_reject_detail_in_markdown_report(self):
        """reject_detail 出现在 Markdown 报告的拒绝明细表中。"""
        from ashare_quant.backtest.report import ReportGenerator

        quotes = _make_exact_quotes("000001", date(2024, 1, 2), 5, open_raw=9.99, close_price=10.00)
        d = make_trade_dates(date(2024, 1, 2), 5)
        signals = [Signal(d[0], "000001", Side.BUY, 100, "md detail")]
        config = BacktestConfig(initial_cash=1004.50)
        result = _run_backtest(quotes, signals, config)

        report_gen = ReportGenerator()
        md = report_gen.generate_markdown(result, to_decimal(1004.50))

        assert "拒绝详情" in md or "reject_detail" in md
        assert "现金不足" in md


# ------------------------------------------------------------------ #
# 7. 风控与 Broker 一致性——端到端验证
# ------------------------------------------------------------------ #
class TestEndToEndConsistency:
    """端到端验证：风控通过则 Broker 必定能成交，且现金不为负。"""

    def test_risk_pass_broker_fills(self):
        """风控通过 -> Broker 必定成交 -> 现金 >= 0。"""
        config = BacktestConfig(initial_cash=10000)
        open_raw = to_decimal(9.99)
        cost = compute_buy_cost(open_raw, 100, config)
        exact_cash = cost.turnover + cost.total_cost

        # 风控校验
        rm = DefaultRiskManager()
        signal = Signal(date(2024, 1, 2), "000001", Side.BUY, 100, "consistency")
        bar = _make_bar_data(9.99)
        snapshot = PortfolioSnapshot(
            snapshot_date=date(2024, 1, 3),
            cash=exact_cash,
            position_value=Decimal("0"),
            total_equity=exact_cash,
        )
        decision = rm.validate(signal, snapshot, bar, config, {})
        assert decision.approved, "风控应通过"

        # Broker 成交
        broker = AShareBrokerSimulator()
        order = _make_order()
        fill = broker.execute(order, bar, snapshot, config, {})
        assert fill is not None, "Broker 应成交"

        # 成交后现金 = 精确现金 + cash_change >= 0
        post_cash = exact_cash + fill.cash_change
        assert post_cash >= Decimal("0"), f"成交后现金不能为负: {post_cash}"

    def test_various_prices_no_negative_cash(self):
        """多个价格点验证现金不为负。"""
        config = BacktestConfig(initial_cash=5000)
        prices = [5.0, 8.0, 10.0, 15.0, 20.0, 30.0]

        for price in prices:
            quotes = _make_exact_quotes(
                "000001", date(2024, 1, 2), 5, open_raw=price
            )
            d = make_trade_dates(date(2024, 1, 2), 5)
            signals = [Signal(d[0], "000001", Side.BUY, 100, f"price={price}")]
            result = _run_backtest(quotes, signals, config)

            for snap in result.daily_equity:
                assert snap.cash >= Decimal("0"), (
                    f"price={price}: 现金为负 {snap.cash}"
                )
