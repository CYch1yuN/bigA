"""回测引擎 BacktestEngine 测试。

覆盖任务规格中的必须测试项 #1-#12（防未来函数、手数、资金不足、T+1、停牌、
涨跌停、账务恒等式、ID唯一、可复现、边缘场景、数据校验、期末取消等）。
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

import numpy as np
import pandas as pd
import pytest

from ashare_quant.backtest.config import BacktestConfig
from ashare_quant.backtest.engine import BacktestEngine
from ashare_quant.backtest.models import (
    OrderStatus,
    RejectReason,
    Side,
    Signal,
    to_decimal,
)
from ashare_quant.backtest.strategies import (
    BuyAndHoldStrategy,
    NoOpStrategy,
    ScriptedStrategy,
)
from tests.backtest_samples import (
    make_bar,
    make_limit_down_bar,
    make_limit_up_bar,
    make_quotes,
    make_trade_dates,
    make_two_stock_quotes,
)


# ------------------------------------------------------------------ #
# 辅助函数
# ------------------------------------------------------------------ #
def _dates(start: date = date(2024, 1, 2), n: int = 10) -> list[date]:
    return make_trade_dates(start, n)


def _run(
    data: pd.DataFrame,
    signals: list[Signal],
    config: Optional[BacktestConfig] = None,
    start: Optional[date] = None,
    end: Optional[date] = None,
):
    cfg = config or BacktestConfig()
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
    )


# ------------------------------------------------------------------ #
# 1. 防未来函数：D 收盘信号只能在 D+1 开盘成交
# ------------------------------------------------------------------ #
class TestAntiLookahead:
    def test_signal_fills_next_day(self):
        quotes = make_quotes("000001", date(2024, 1, 2), 10)
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [Signal(d[1], "000001", Side.BUY, 100, "test")]
        result = _run(quotes, signals, BacktestConfig(initial_cash=10000))
        fills = result.fills
        assert len(fills) == 1
        assert fills[0].fill_date == d[2], "成交日应为信号日 D 的下一交易日 D+1"
        assert fills[0].fill_date > signals[0].signal_date

    def test_no_same_day_fill(self):
        quotes = make_quotes("000001", date(2024, 1, 2), 10)
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [Signal(d[0], "000001", Side.BUY, 100, "test")]
        result = _run(quotes, signals, BacktestConfig(initial_cash=10000))
        fills = result.fills
        assert len(fills) == 1
        assert fills[0].fill_date == d[1], "首日信号应在次日成交，而非当日"


# ------------------------------------------------------------------ #
# 2. 手数：100/200 成功，99/150 拒绝
# ------------------------------------------------------------------ #
class TestLotSize:
    def test_100_shares_succeeds(self):
        quotes = make_quotes("000001", date(2024, 1, 2), 10)
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [Signal(d[0], "000001", Side.BUY, 100)]
        result = _run(quotes, signals, BacktestConfig(initial_cash=10000))
        assert len(result.fills) == 1
        assert result.orders[0].status == OrderStatus.FILLED

    def test_200_shares_succeeds(self):
        quotes = make_quotes("000001", date(2024, 1, 2), 10)
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [Signal(d[0], "000001", Side.BUY, 200)]
        result = _run(quotes, signals, BacktestConfig(initial_cash=10000))
        assert len(result.fills) == 1
        assert result.orders[0].status == OrderStatus.FILLED

    def test_99_shares_rejected(self):
        quotes = make_quotes("000001", date(2024, 1, 2), 10)
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [Signal(d[0], "000001", Side.BUY, 99)]
        result = _run(quotes, signals, BacktestConfig(initial_cash=10000))
        assert len(result.fills) == 0
        assert result.orders[0].status == OrderStatus.REJECTED
        assert result.orders[0].reject_reason == RejectReason.LOT_SIZE

    def test_150_shares_rejected(self):
        quotes = make_quotes("000001", date(2024, 1, 2), 10)
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [Signal(d[0], "000001", Side.BUY, 150)]
        result = _run(quotes, signals, BacktestConfig(initial_cash=10000))
        assert len(result.fills) == 0
        assert result.orders[0].status == OrderStatus.REJECTED
        assert result.orders[0].reject_reason == RejectReason.LOT_SIZE


# ------------------------------------------------------------------ #
# 3. 资金不足整单拒绝，现金不为负
# ------------------------------------------------------------------ #
class TestCashInsufficiency:
    def test_insufficient_cash_rejected(self):
        quotes = make_quotes("000001", date(2024, 1, 2), 10)
        d = make_trade_dates(date(2024, 1, 2), 10)
        # 200 股 @~10 元需要 ~2012 元，但只有 1000 元
        signals = [Signal(d[0], "000001", Side.BUY, 200)]
        result = _run(quotes, signals, BacktestConfig(initial_cash=1000))
        assert len(result.fills) == 0
        assert result.orders[0].status == OrderStatus.REJECTED

    def test_cash_not_negative(self):
        quotes = make_quotes("000001", date(2024, 1, 2), 10)
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [Signal(d[0], "000001", Side.BUY, 200)]
        result = _run(quotes, signals, BacktestConfig(initial_cash=1000))
        for snap in result.daily_equity:
            assert snap.cash >= Decimal("0"), "现金不能为负"


# ------------------------------------------------------------------ #
# 4. T+1：买入当日不可卖，次日解冻可卖
# ------------------------------------------------------------------ #
class TestTPlusOne:
    def test_buy_then_sell_next_day(self):
        quotes = make_quotes("000001", date(2024, 1, 2), 10)
        d = make_trade_dates(date(2024, 1, 2), 10)
        # D0 信号买入 -> D1 成交；D1 信号卖出 -> D2 尝试成交
        # 但 D1 成交后 frozen，D2 开盘先解冻，所以 D2 可卖
        signals = [
            Signal(d[0], "000001", Side.BUY, 100, "买入"),
            Signal(d[1], "000001", Side.SELL, 100, "卖出"),
        ]
        result = _run(quotes, signals, BacktestConfig(initial_cash=10000))
        fills = result.fills
        assert len(fills) == 2
        assert fills[0].side == Side.BUY
        assert fills[0].fill_date == d[1]
        assert fills[1].side == Side.SELL
        assert fills[1].fill_date == d[2]

    def test_same_day_sell_after_buy_rejected(self):
        """买入成交日（D1）当日即生成卖出信号，D2 成交时已解冻，应成功。

        但如果策略在 D0 同时发出 BUY 和 SELL 信号：
        - BUY 在 D1 成交，frozen
        - SELL 也在 D1 尝试成交，但此时持仓尚未解冻 -> T_PLUS_ONE 拒绝
        """
        quotes = make_quotes("000001", date(2024, 1, 2), 10)
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [
            Signal(d[0], "000001", Side.BUY, 100, "买入"),
            Signal(d[0], "000001", Side.SELL, 100, "同日卖出"),
        ]
        result = _run(quotes, signals, BacktestConfig(initial_cash=10000))
        # BUY 成功，SELL 被拒（T_PLUS_ONE）
        buy_fills = [f for f in result.fills if f.side == Side.BUY]
        sell_fills = [f for f in result.fills if f.side == Side.SELL]
        assert len(buy_fills) == 1
        assert len(sell_fills) == 0
        # 找到被拒的 SELL 订单
        sell_orders = [
            o for o in result.orders if o.signal.side == Side.SELL
        ]
        assert len(sell_orders) == 1
        assert sell_orders[0].status == OrderStatus.REJECTED
        assert sell_orders[0].reject_reason == RejectReason.T_PLUS_ONE


# ------------------------------------------------------------------ #
# 5. 停牌买卖均拒绝
# ------------------------------------------------------------------ #
class TestSuspended:
    def test_suspended_rejects_buy(self):
        quotes = make_quotes(
            "000001", date(2024, 1, 2), 10, is_suspended_days=[2]
        )
        d = make_trade_dates(date(2024, 1, 2), 10)
        # D1 信号 -> D2 成交（D2 是停牌日）
        signals = [Signal(d[1], "000001", Side.BUY, 100)]
        result = _run(quotes, signals, BacktestConfig(initial_cash=10000))
        assert len(result.fills) == 0
        assert result.orders[0].status == OrderStatus.REJECTED
        assert result.orders[0].reject_reason == RejectReason.SUSPENDED

    def test_suspended_rejects_sell(self):
        """先买入，再在停牌日卖出 -> SUSPENDED 拒绝。"""
        quotes = make_quotes(
            "000001", date(2024, 1, 2), 10, is_suspended_days=[3]
        )
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [
            Signal(d[0], "000001", Side.BUY, 100, "买入"),
            Signal(d[2], "000001", Side.SELL, 100, "停牌日卖出"),
        ]
        result = _run(quotes, signals, BacktestConfig(initial_cash=10000))
        buy_fills = [f for f in result.fills if f.side == Side.BUY]
        sell_fills = [f for f in result.fills if f.side == Side.SELL]
        assert len(buy_fills) == 1
        assert len(sell_fills) == 0
        sell_orders = [
            o for o in result.orders if o.signal.side == Side.SELL
        ]
        assert sell_orders[0].status == OrderStatus.REJECTED
        assert sell_orders[0].reject_reason == RejectReason.SUSPENDED


# ------------------------------------------------------------------ #
# 6. 涨跌停
# ------------------------------------------------------------------ #
class TestLimitUpDown:
    def _make_limit_quotes(self, symbol="000001", prev_close=10.0, ratio=0.10):
        """构造行情：第 3 天开盘价 = 涨停价。"""
        d = make_trade_dates(date(2024, 1, 2), 10)
        rows = []
        for i, dt in enumerate(d):
            if i == 2:
                # 涨停日
                limit_price = round(prev_close * (1 + ratio), 2)
                row = make_bar(
                    symbol, dt,
                    open_price=limit_price,
                    high=limit_price,
                    low=limit_price,
                    close=limit_price,
                )
            elif i == 3:
                # 跌停日
                limit_down = round(prev_close * (1 - ratio), 2)
                row = make_bar(
                    symbol, dt,
                    open_price=limit_down,
                    high=limit_down,
                    low=limit_down,
                    close=limit_down,
                )
            else:
                row = make_bar(symbol, dt, open_price=prev_close, close=prev_close)
            rows.append(row)
        return pd.DataFrame(rows)

    def test_limit_up_rejects_buy(self):
        quotes = self._make_limit_quotes()
        d = make_trade_dates(date(2024, 1, 2), 10)
        # D1 信号 -> D2 成交（涨停日）
        signals = [Signal(d[1], "000001", Side.BUY, 100)]
        result = _run(quotes, signals, BacktestConfig(initial_cash=10000))
        assert len(result.fills) == 0
        assert result.orders[0].reject_reason == RejectReason.LIMIT_UP

    def test_limit_down_rejects_sell(self):
        """先买入，在跌停日卖出 -> LIMIT_DOWN 拒绝。"""
        quotes = self._make_limit_quotes()
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [
            Signal(d[0], "000001", Side.BUY, 100, "买入"),
            Signal(d[2], "000001", Side.SELL, 100, "跌停日卖出"),
        ]
        result = _run(quotes, signals, BacktestConfig(initial_cash=10000))
        sell_orders = [
            o for o in result.orders if o.signal.side == Side.SELL
        ]
        assert len(sell_orders) == 1
        assert sell_orders[0].status == OrderStatus.REJECTED
        assert sell_orders[0].reject_reason == RejectReason.LIMIT_DOWN

    def test_limit_up_allows_sell(self):
        """涨停日卖出应被允许。"""
        quotes = self._make_limit_quotes()
        d = make_trade_dates(date(2024, 1, 2), 10)
        # D0 买入 -> D1 成交；D1 信号卖出 -> D2 涨停日成交（允许卖出）
        signals = [
            Signal(d[0], "000001", Side.BUY, 100),
            Signal(d[1], "000001", Side.SELL, 100),
        ]
        result = _run(quotes, signals, BacktestConfig(initial_cash=10000))
        assert len(result.fills) == 2

    def test_limit_down_allows_buy(self):
        """跌停日买入应被允许。"""
        quotes = self._make_limit_quotes()
        d = make_trade_dates(date(2024, 1, 2), 10)
        # D2 信号 -> D3 跌停日买入（允许买入）
        signals = [Signal(d[2], "000001", Side.BUY, 100)]
        result = _run(quotes, signals, BacktestConfig(initial_cash=10000))
        assert len(result.fills) == 1
        assert result.fills[0].fill_date == d[3]


# ------------------------------------------------------------------ #
# 7. 账务恒等式
# ------------------------------------------------------------------ #
class TestAccountingIdentity:
    def test_daily_equity_identity(self):
        quotes = make_quotes("000001", date(2024, 1, 2), 10)
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [
            Signal(d[0], "000001", Side.BUY, 100),
            Signal(d[5], "000001", Side.SELL, 100),
        ]
        result = _run(quotes, signals, BacktestConfig(initial_cash=10000))
        for snap in result.daily_equity:
            expected = snap.cash + snap.position_value
            diff = abs(expected - snap.total_equity)
            assert diff <= Decimal("0.01"), (
                f"账务不平等 {snap.snapshot_date}: "
                f"cash({snap.cash}) + pos({snap.position_value}) "
                f"!= equity({snap.total_equity}), diff={diff}"
            )

    def test_identity_noop(self):
        quotes = make_quotes("000001", date(2024, 1, 2), 10)
        result = _run(quotes, [], BacktestConfig(initial_cash=10000))
        for snap in result.daily_equity:
            assert snap.total_equity == snap.cash
            assert snap.position_value == Decimal("0")


# ------------------------------------------------------------------ #
# 8. 订单 ID 唯一
# ------------------------------------------------------------------ #
class TestOrderIdUniqueness:
    def test_all_order_ids_unique(self):
        quotes = make_two_stock_quotes("000001", "000002", date(2024, 1, 2), 10)
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [
            Signal(d[0], "000001", Side.BUY, 100),
            Signal(d[0], "000002", Side.BUY, 200),
            Signal(d[3], "000001", Side.SELL, 100),
            Signal(d[3], "000002", Side.SELL, 200),
        ]
        config = BacktestConfig(initial_cash=10000)
        config.risk.enable_single_position_limit = False
        result = _run(quotes, signals, config)
        ids = [o.order_id for o in result.orders]
        assert len(ids) == len(set(ids)), "订单 ID 必须全部唯一"


# ------------------------------------------------------------------ #
# 9. 可复现：相同输入相同结果
# ------------------------------------------------------------------ #
class TestReproducibility:
    def test_same_input_same_hash(self):
        quotes = make_quotes("000001", date(2024, 1, 2), 10)
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [Signal(d[0], "000001", Side.BUY, 100)]
        config = BacktestConfig(initial_cash=10000)

        r1 = _run(quotes, signals, config)
        r2 = _run(quotes, signals, config)

        assert r1.content_hash == r2.content_hash
        assert len(r1.orders) == len(r2.orders)
        assert len(r1.fills) == len(r2.fills)
        assert r1.daily_equity[-1].total_equity == r2.daily_equity[-1].total_equity

    def test_content_hash_not_empty(self):
        quotes = make_quotes("000001", date(2024, 1, 2), 10)
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [Signal(d[0], "000001", Side.BUY, 100)]
        result = _run(quotes, signals, BacktestConfig(initial_cash=10000))
        assert result.content_hash is not None
        assert result.content_hash != ""


# ------------------------------------------------------------------ #
# 10. 边缘场景
# ------------------------------------------------------------------ #
class TestEdgeCases:
    def test_noop_strategy(self):
        """空策略：零交易，最终权益 = 初始现金。"""
        quotes = make_quotes("000001", date(2024, 1, 2), 10)
        d = make_trade_dates(date(2024, 1, 2), 10)
        engine = BacktestEngine()
        result = engine.run(
            data=quotes,
            strategy=NoOpStrategy(),
            start_date=d[0],
            end_date=d[-1],
            initial_cash=10000,
            config=BacktestConfig(initial_cash=10000),
        )
        assert len(result.fills) == 0
        assert len(result.orders) == 0
        final = result.daily_equity[-1]
        assert final.total_equity == Decimal("10000")
        assert final.position_value == Decimal("0")

    def test_single_day_signal_cancelled(self):
        """单交易日：信号无法在下一日成交 -> CANCELLED。"""
        quotes = make_quotes("000001", date(2024, 1, 2), 1)
        d = make_trade_dates(date(2024, 1, 2), 1)
        signals = [Signal(d[0], "000001", Side.BUY, 100)]
        result = _run(
            quotes, signals, BacktestConfig(initial_cash=10000),
            start=d[0], end=d[0],
        )
        assert len(result.orders) == 1
        assert result.orders[0].status == OrderStatus.CANCELLED
        assert len(result.fills) == 0

    def test_all_cash_no_positions(self):
        """全程现金：报告仍生成，持仓市值=0。"""
        quotes = make_quotes("000001", date(2024, 1, 2), 5)
        d = make_trade_dates(date(2024, 1, 2), 5)
        result = _run(quotes, [], BacktestConfig(initial_cash=5000))
        for snap in result.daily_equity:
            assert snap.position_value == Decimal("0")
            assert snap.total_equity == Decimal("5000")

    def test_empty_data_range(self):
        """回测区间内无交易日 -> 空结果。"""
        quotes = make_quotes("000001", date(2024, 1, 2), 5)
        result = _run(
            quotes, [], BacktestConfig(initial_cash=1000),
            start=date(2025, 1, 1), end=date(2025, 1, 31),
        )
        assert len(result.daily_equity) >= 1
        assert len(result.fills) == 0


# ------------------------------------------------------------------ #
# 11. 数据校验
# ------------------------------------------------------------------ #
class TestDataValidation:
    def test_missing_required_columns(self):
        df = pd.DataFrame({"symbol": ["000001"], "trade_date": [date(2024, 1, 2)]})
        with pytest.raises(ValueError, match="缺少必需字段"):
            _run(df, [], BacktestConfig(initial_cash=1000))

    def test_duplicate_keys(self):
        row = make_bar("000001", date(2024, 1, 2))
        df = pd.DataFrame([row, row])
        with pytest.raises(ValueError, match="重复"):
            _run(df, [], BacktestConfig(initial_cash=1000))

    def test_nan_in_price(self):
        quotes = make_quotes("000001", date(2024, 1, 2), 5)
        quotes.loc[0, "open_raw"] = np.nan
        with pytest.raises(ValueError, match="NaN|非有限"):
            _run(quotes, [], BacktestConfig(initial_cash=1000))


# ------------------------------------------------------------------ #
# 12. 期末挂单取消
# ------------------------------------------------------------------ #
class TestEndOfPeriodCancellation:
    def test_last_day_signal_cancelled(self):
        """最后一个交易日信号无法成交 -> CANCELLED。"""
        quotes = make_quotes("000001", date(2024, 1, 2), 5)
        d = make_trade_dates(date(2024, 1, 2), 5)
        signals = [Signal(d[-1], "000001", Side.BUY, 100)]
        result = _run(quotes, signals, BacktestConfig(initial_cash=10000))
        assert len(result.orders) == 1
        assert result.orders[0].status == OrderStatus.CANCELLED


# ------------------------------------------------------------------ #
# 13. 多次买入平均成本、部分卖出、清仓
# ------------------------------------------------------------------ #
class TestAverageCostAndPartialSell:
    def test_multiple_buys_average_cost(self):
        quotes = make_quotes("000001", date(2024, 1, 2), 10)
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [
            Signal(d[0], "000001", Side.BUY, 100, "第一次买入"),
            Signal(d[2], "000001", Side.BUY, 100, "第二次买入"),
        ]
        result = _run(quotes, signals, BacktestConfig(initial_cash=10000))
        assert len(result.fills) == 2
        final_pos = result.final_positions.get("000001")
        assert final_pos is not None
        assert final_pos.total_quantity == 200

    def test_partial_sell_then_clear(self):
        quotes = make_quotes("000001", date(2024, 1, 2), 10)
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [
            Signal(d[0], "000001", Side.BUY, 200, "买入200"),
            Signal(d[2], "000001", Side.SELL, 100, "卖出100"),
            Signal(d[4], "000001", Side.SELL, 100, "清仓"),
        ]
        result = _run(quotes, signals, BacktestConfig(initial_cash=10000))
        assert len(result.fills) == 3
        final_pos = result.final_positions.get("000001")
        # 清仓后 total_quantity 应为 0
        if final_pos:
            assert final_pos.total_quantity == 0


# ------------------------------------------------------------------ #
# 14. 缺失 bar
# ------------------------------------------------------------------ #
class TestMissingBar:
    def test_missing_bar_rejected(self):
        """信号针对一个在成交日没有行情的 symbol -> MISSING_BAR。"""
        quotes = make_quotes("000001", date(2024, 1, 2), 10)
        d = make_trade_dates(date(2024, 1, 2), 10)
        # 买入一个不存在的 symbol
        signals = [Signal(d[0], "999999", Side.BUY, 100)]
        result = _run(quotes, signals, BacktestConfig(initial_cash=10000))
        assert len(result.fills) == 0
        assert result.orders[0].status == OrderStatus.REJECTED
        assert result.orders[0].reject_reason == RejectReason.MISSING_BAR


# ------------------------------------------------------------------ #
# 15. 报告指标可由原始流水独立复算
# ------------------------------------------------------------------ #
class TestMetricsReproducible:
    def test_metrics_from_raw_records(self):
        quotes = make_quotes("000001", date(2024, 1, 2), 10)
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [
            Signal(d[0], "000001", Side.BUY, 100),
            Signal(d[5], "000001", Side.SELL, 100),
        ]
        result = _run(quotes, signals, BacktestConfig(initial_cash=10000))
        m = result.metrics

        # 交易次数 = 成交数
        assert m["total_trades"] == len(result.fills)

        # 初始权益
        assert to_decimal(m["initial_equity"]) == Decimal("10000")

        # 最终权益 = 最后一天快照
        assert to_decimal(m["final_equity"]) == result.daily_equity[-1].total_equity

        # 拒绝原因计数可由 orders 复算
        manual_counts: dict[str, int] = {}
        for o in result.orders:
            if o.status in (OrderStatus.REJECTED, OrderStatus.CANCELLED):
                if o.reject_reason is not None:
                    key = o.reject_reason.value if hasattr(o.reject_reason, "value") else str(o.reject_reason)
                    manual_counts[key] = manual_counts.get(key, 0) + 1
        assert m["reject_reason_counts"] == manual_counts
