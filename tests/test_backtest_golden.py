"""回测引擎黄金标准测试（Golden Standard）。

一个 10 个交易日、2 只股票的确定性场景，其中每一个信号、订单、成交、
费用、现金、持仓与权益数值均经手工核算并断言。引擎在滑点取整、费用量化、
T+1 解冻、持仓估值或现金记账上的任何漂移都会令下列断言失败。

确定性数据（价格取整、便于手算）
--------------------------------
两只合成股票，覆盖 10 个交易日（2024-01-02 .. 2024-01-15）：

* ``000001``：每日 open_raw = close_raw = 10.0，high = 10.10，low = 9.90
* ``000002``：每日 open_raw = close_raw =  5.0，high =  5.05，low =  4.95

价格恒定且为整数，使手工核算精确无歧义。

配置（默认费率）
----------------
initial_cash = 10000.0，lot_size = 100，
commission rate = 0.0003（最低 5.0），stamp_duty = 0.001（仅卖出），
transfer_fee = 0.00001（双向），slippage bps = 10，tick = 0.01。

信号计划（手工核算）
--------------------
信号 1 — 2024-01-02 收盘 -> BUY 100 ``000001``（次日 2024-01-03 成交）：
    slippage    = round_up_to_tick(10.0 * 1.001, 0.01) = 10.01
    turnover    = 10.01 * 100           = 1001.00
    commission  = max(1001 * 0.0003, 5) = 5.00
    stamp_duty  (买入)                  = 0.00
    transfer    = 1001 * 0.00001         = 0.01
    total_cost  = 5.00 + 0.00 + 0.01     = 5.01
    cash_change = -(1001.00 + 5.01)      = -1006.01
    cash after  = 10000.00 - 1006.01     = 8993.99
    position    = 100 @ close 10.0       = 1000.00  -> equity 9993.99

信号 2 — 2024-01-04 收盘 -> SELL 100 ``000001``（次日 2024-01-05 成交）：
    slippage    = round_down_to_tick(10.0 * 0.999, 0.01) = 9.99
    turnover    = 9.99 * 100            = 999.00
    commission  = max(999 * 0.0003, 5)   = 5.00
    stamp_duty  = 999 * 0.001           = 1.00
    transfer    = 999 * 0.00001         = 0.01
    total_cost  = 5.00 + 1.00 + 0.01    = 6.01
    cash_change = 999.00 - 6.01         = 992.99
    cash after  = 8993.99 + 992.99      = 9986.98
    position    = 0                     -> equity 9986.98

一次完整买卖盈亏 = 992.99 - 1006.01 = -13.02（一笔亏损交易）。

T+1 时间线：买入于 2024-01-03 成交并冻结，2024-01-04 开盘解冻，
故 2024-01-04 收盘发出的卖出信号在 2024-01-05 可卖。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from ashare_quant.backtest.config import BacktestConfig
from ashare_quant.backtest.engine import BacktestEngine
from ashare_quant.backtest.interfaces import Strategy
from ashare_quant.backtest.models import (
    BacktestResult,
    OrderStatus,
    Side,
    Signal,
    StrategyContext,
)
from tests.backtest_samples import make_bar, make_trade_dates


# --------------------------------------------------------------------------- #
# 脚本化策略：按收盘日回放预设信号。
# --------------------------------------------------------------------------- #
class ScriptedStrategy(Strategy):
    """按收盘日回放固定信号清单的策略。"""

    def __init__(self, signals_by_date: dict[date, list[Signal]]) -> None:
        self._signals_by_date = {d: list(sigs) for d, sigs in signals_by_date.items()}

    def on_close(self, context: StrategyContext) -> list[Signal]:
        return list(self._signals_by_date.get(context.current_date, []))


# --------------------------------------------------------------------------- #
# 确定性恒定价格行情（10 个交易日、2 只股票）。
# --------------------------------------------------------------------------- #
def build_flat_quotes() -> pd.DataFrame:
    """构造 10 个交易日、2 只股票且价格为整数取整的合成行情。"""
    dates = make_trade_dates(date(2024, 1, 2), 10)
    rows: list[dict] = []
    for dt in dates:
        rows.append(
            make_bar(
                symbol="000001",
                dt=dt,
                open_price=10.0,
                high=10.10,
                low=9.90,
                close=10.0,
            )
        )
        rows.append(
            make_bar(
                symbol="000002",
                dt=dt,
                open_price=5.0,
                high=5.05,
                low=4.95,
                close=5.0,
            )
        )
    df = pd.DataFrame(rows)
    return df.sort_values(["trade_date", "symbol"]).reset_index(drop=True)


# --------------------------------------------------------------------------- #
# 黄金标准测试
# --------------------------------------------------------------------------- #
class TestGoldenStandard:
    """10 日 2 股黄金场景：手工核算每一个数值并断言。"""

    # --- 固件 ------------------------------------------------------------- #
    @pytest.fixture(scope="class")
    def quotes(self) -> pd.DataFrame:
        return build_flat_quotes()

    @pytest.fixture(scope="class")
    def backtest_config(self) -> BacktestConfig:
        # initial_cash 提升至 10000.0，保证买入（≈1006）有足够现金。
        return BacktestConfig(initial_cash=10000.0)

    @pytest.fixture(scope="class")
    def strategy(self) -> ScriptedStrategy:
        return ScriptedStrategy(
            {
                date(2024, 1, 2): [
                    Signal(
                        signal_date=date(2024, 1, 2),
                        symbol="000001",
                        side=Side.BUY,
                        quantity=100,
                        reason="golden buy 100 @ flat 10.0",
                    ),
                ],
                date(2024, 1, 4): [
                    Signal(
                        signal_date=date(2024, 1, 4),
                        symbol="000001",
                        side=Side.SELL,
                        quantity=100,
                        reason="golden sell 100 @ flat 10.0",
                    ),
                ],
            }
        )

    @pytest.fixture(scope="class")
    def result(
        self,
        quotes: pd.DataFrame,
        backtest_config: BacktestConfig,
        strategy: ScriptedStrategy,
    ) -> BacktestResult:
        engine = BacktestEngine()
        # 回测窗口 [2024-01-02, 2024-01-12] 覆盖两次成交
        # （2024-01-03 买入、2024-01-05 卖出）；10 日数据集延伸至 2024-01-15。
        return engine.run(
            data=quotes,
            strategy=strategy,
            start_date=date(2024, 1, 2),
            end_date=date(2024, 1, 12),
            initial_cash=backtest_config.initial_cash,
            config=backtest_config,
        )

    # --- 数据自检 --------------------------------------------------------- #
    def test_data_setup(self, quotes: pd.DataFrame):
        """两只股票、各 10 个交易日、价格为恒定整数取整值。"""
        assert set(quotes["symbol"].unique()) == {"000001", "000002"}
        assert quotes["trade_date"].nunique() == 10
        assert len(quotes) == 20
        s1 = quotes[quotes["symbol"] == "000001"]
        assert (s1["open_raw"] == 10.0).all()
        assert (s1["close_raw"] == 10.0).all()
        s2 = quotes[quotes["symbol"] == "000002"]
        assert (s2["open_raw"] == 5.0).all()
        assert (s2["close_raw"] == 5.0).all()

    # --- 1. 成交笔数 ------------------------------------------------------ #
    def test_exactly_two_fills(self, result: BacktestResult):
        assert len(result.fills) == 2

    # --- 2. 订单数量与状态 ------------------------------------------------ #
    def test_orders_count_and_status(self, result: BacktestResult):
        assert len(result.orders) == 2
        for order in result.orders:
            assert order.status is OrderStatus.FILLED
            assert order.fill is not None
        # 每笔成交均关联回某个已成交订单
        order_ids = {o.order_id for o in result.orders}
        for fill in result.fills:
            assert fill.order_id in order_ids

    # --- 3. 买入成交（信号 1）-------------------------------------------- #
    def test_buy_fill_hand_computed(self, result: BacktestResult):
        fill = next(f for f in result.fills if f.fill_date == date(2024, 1, 3))
        assert fill.fill_date == date(2024, 1, 3)
        assert fill.symbol == "000001"
        assert fill.side is Side.BUY
        assert fill.quantity == 100
        # 原始开盘价量化到 4 位
        assert fill.raw_open_price == Decimal("10.0000")
        # round_up_to_tick(10.0 * 1.001, 0.01) = 10.01
        assert fill.slippage_price == Decimal("10.01")
        assert fill.turnover == Decimal("1001.00")
        assert fill.commission == Decimal("5.00")
        assert fill.stamp_duty == Decimal("0.00")
        assert fill.transfer_fee == Decimal("0.01")
        assert fill.total_cost == Decimal("5.01")
        assert fill.cash_change == Decimal("-1006.01")

    # --- 4. 卖出成交（信号 2）-------------------------------------------- #
    def test_sell_fill_hand_computed(self, result: BacktestResult):
        fill = next(f for f in result.fills if f.fill_date == date(2024, 1, 5))
        assert fill.fill_date == date(2024, 1, 5)
        assert fill.symbol == "000001"
        assert fill.side is Side.SELL
        assert fill.quantity == 100
        assert fill.raw_open_price == Decimal("10.0000")
        # round_down_to_tick(10.0 * 0.999, 0.01) = 9.99
        assert fill.slippage_price == Decimal("9.99")
        assert fill.turnover == Decimal("999.00")
        assert fill.commission == Decimal("5.00")
        assert fill.stamp_duty == Decimal("1.00")
        assert fill.transfer_fee == Decimal("0.01")
        assert fill.total_cost == Decimal("6.01")
        assert fill.cash_change == Decimal("992.99")

    # --- 5. 每日权益恒等式 ------------------------------------------------ #
    def test_daily_equity_identity(self, result: BacktestResult):
        for snap in result.daily_equity:
            assert abs(snap.total_equity - (snap.cash + snap.position_value)) <= Decimal("0.01")

    # --- 6. 快照窗口 ------------------------------------------------------ #
    def test_snapshot_window(self, result: BacktestResult):
        # [2024-01-02, 2024-01-12] 共 9 个交易日
        assert len(result.daily_equity) == 9
        dates = [s.snapshot_date for s in result.daily_equity]
        assert dates[0] == date(2024, 1, 2)
        assert dates[-1] == date(2024, 1, 12)
        assert date(2024, 1, 3) in dates  # 买入成交日
        assert date(2024, 1, 5) in dates  # 卖出成交日

    # --- 7. 买入后现金 ---------------------------------------------------- #
    def test_cash_after_buy_fill(self, result: BacktestResult):
        snap = next(s for s in result.daily_equity if s.snapshot_date == date(2024, 1, 3))
        assert snap.cash == Decimal("8993.99")
        # 100 股按 close_raw 10.0 估值
        assert snap.position_value == Decimal("1000.00")
        assert snap.total_equity == Decimal("9993.99")

    # --- 8. 卖出后现金 ---------------------------------------------------- #
    def test_cash_after_sell_fill(self, result: BacktestResult):
        snap = next(s for s in result.daily_equity if s.snapshot_date == date(2024, 1, 5))
        assert snap.cash == Decimal("9986.98")
        assert snap.position_value == Decimal("0")
        assert snap.total_equity == Decimal("9986.98")

    # --- 9. 无剩余持仓 ---------------------------------------------------- #
    def test_no_remaining_positions(self, result: BacktestResult):
        for pos in result.final_positions.values():
            assert pos.total_quantity == 0
            assert pos.sellable_quantity == 0
            assert pos.frozen_buy_quantity == 0
        # 交易标的已完全平仓
        if "000001" in result.final_positions:
            assert result.final_positions["000001"].total_quantity == 0

    # --- 10. 期末权益 ----------------------------------------------------- #
    def test_final_equity(self, result: BacktestResult):
        final = result.daily_equity[-1]
        assert final.cash == Decimal("9986.98")
        assert final.position_value == Decimal("0")
        assert final.total_equity == Decimal("9986.98")

    # --- 11. 内容哈希 ----------------------------------------------------- #
    def test_content_hash(self, result: BacktestResult):
        assert result.content_hash is not None
        assert result.content_hash != ""

    # --- 12. 指标可独立复核 ---------------------------------------------- #
    def test_metrics_recomputed(self, result: BacktestResult):
        # 总交易笔数等于成交笔数
        assert result.metrics["total_trades"] == 2
        assert result.metrics["total_trades"] == len(result.fills)
        # 从成交流水手工汇总成交额
        total_turnover = sum((f.turnover for f in result.fills), Decimal("0"))
        assert total_turnover == Decimal("1001.00") + Decimal("999.00")
        assert total_turnover == Decimal("2000.00")
        # 指标暴露的初始/期末权益
        assert result.metrics["initial_equity"] == Decimal("10000.0")
        assert result.metrics["final_equity"] == Decimal("9986.98")

    # --- 附：一次完整买卖盈亏与胜率指标 --------------------------------- #
    def test_round_trip_pnl_and_win_metrics(self, result: BacktestResult):
        buy = next(f for f in result.fills if f.side is Side.BUY)
        sell = next(f for f in result.fills if f.side is Side.SELL)
        # 买入总成本（含费）= -buy.cash_change = 1006.01
        # 卖出净收入（扣费后）= sell.cash_change = 992.99
        pnl = sell.cash_change - (-buy.cash_change)
        assert pnl == Decimal("-13.02")
        # 一次卖出、零次盈利 -> 胜率 0；一次亏损 -> 盈亏比 0
        assert result.metrics["win_rate"] == Decimal("0")
        assert result.metrics["profit_loss_ratio"] == Decimal("0")
