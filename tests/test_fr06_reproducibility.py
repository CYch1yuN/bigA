"""FR-06 严格回归测试：确定性订单 ID 与可复现性。

验证：
1. 相同输入运行两次，所有 order_id 完全一致
2. fills 的 order_id 完全一致
3. generate_json 两次结果深度相等
4. Markdown 两次完全相等
5. orders DataFrame 完全相等
6. fills DataFrame 完全相等
7. daily equity DataFrame 完全相等
8. content_hash 完全一致
9. 改变任一经济结果时 content_hash 必须变化
10. 两条完全相同的重复信号 ID 唯一且重复运行稳定
11. 不同订单不能发生 ID 冲突
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from typing import Optional

import pandas as pd
import pytest

from ashare_quant.backtest.config import BacktestConfig
from ashare_quant.backtest.engine import BacktestEngine
from ashare_quant.backtest.models import (
    OrderStatus,
    Side,
    Signal,
    to_decimal,
)
from ashare_quant.backtest.report import ReportGenerator
from ashare_quant.backtest.strategies import ScriptedStrategy
from tests.backtest_samples import make_quotes, make_trade_dates


# ------------------------------------------------------------------ #
# 辅助函数
# ------------------------------------------------------------------ #
def _run(
    data: Optional[pd.DataFrame] = None,
    signals: Optional[list[Signal]] = None,
    config: Optional[BacktestConfig] = None,
):
    cfg = config or BacktestConfig(initial_cash=100000)
    df = data if data is not None else make_quotes("000001", date(2024, 1, 2), 10, base_price=10.0)
    sigs = signals if signals is not None else [Signal(date(2024, 1, 2), "000001", Side.BUY, 100)]
    dates = make_trade_dates(date(2024, 1, 2), 10)
    engine = BacktestEngine()
    return engine.run(
        data=df,
        strategy=ScriptedStrategy(sigs),
        start_date=dates[0],
        end_date=dates[-1],
        initial_cash=cfg.initial_cash,
        config=cfg,
    )


# ------------------------------------------------------------------ #
# 1-2. 相同输入运行两次，order_id 和 fill order_id 完全一致
# ------------------------------------------------------------------ #
class TestDeterministicOrderIds:
    """相同输入运行两次，所有 order_id 和 fill order_id 完全一致。"""

    def test_order_ids_identical_across_runs(self):
        quotes = make_quotes("000001", date(2024, 1, 2), 10, base_price=10.0)
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [
            Signal(d[0], "000001", Side.BUY, 100, "buy"),
            Signal(d[3], "000001", Side.SELL, 100, "sell"),
        ]
        result1 = _run(quotes, signals)
        result2 = _run(quotes, signals)

        ids1 = [o.order_id for o in result1.orders]
        ids2 = [o.order_id for o in result2.orders]
        assert ids1 == ids2, (
            f"order_id 不一致:\n  run1={ids1}\n  run2={ids2}"
        )

    def test_fill_order_ids_identical_across_runs(self):
        quotes = make_quotes("000001", date(2024, 1, 2), 10, base_price=10.0)
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [
            Signal(d[0], "000001", Side.BUY, 100),
            Signal(d[3], "000001", Side.SELL, 100),
        ]
        result1 = _run(quotes, signals)
        result2 = _run(quotes, signals)

        fill_ids1 = [f.order_id for f in result1.fills]
        fill_ids2 = [f.order_id for f in result2.fills]
        assert fill_ids1 == fill_ids2, (
            f"fill order_id 不一致:\n  run1={fill_ids1}\n  run2={fill_ids2}"
        )

    def test_fill_order_id_matches_order_order_id(self):
        """Fill.order_id 必须与对应 Order.order_id 一致。"""
        quotes = make_quotes("000001", date(2024, 1, 2), 10, base_price=10.0)
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [
            Signal(d[0], "000001", Side.BUY, 100),
            Signal(d[3], "000001", Side.SELL, 100),
        ]
        result = _run(quotes, signals)

        order_ids = {o.order_id for o in result.orders}
        for fill in result.fills:
            assert fill.order_id in order_ids, (
                f"fill.order_id={fill.order_id} 不在 order_ids={order_ids} 中"
            )


# ------------------------------------------------------------------ #
# 3-4. JSON 和 Markdown 两次结果完全相等
# ------------------------------------------------------------------ #
class TestReportReproducibility:
    """JSON 和 Markdown 报告两次完全相等。"""

    def test_json_deep_equal(self):
        quotes = make_quotes("000001", date(2024, 1, 2), 10, base_price=10.0)
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [
            Signal(d[0], "000001", Side.BUY, 100),
            Signal(d[3], "000001", Side.SELL, 100),
        ]
        result1 = _run(quotes, signals)
        result2 = _run(quotes, signals)

        report_gen = ReportGenerator()
        json1 = report_gen.generate_json(result1, to_decimal(100000))
        json2 = report_gen.generate_json(result2, to_decimal(100000))

        assert json1 == json2, "两次 JSON 报告不一致"

    def test_markdown_equal(self):
        quotes = make_quotes("000001", date(2024, 1, 2), 10, base_price=10.0)
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [
            Signal(d[0], "000001", Side.BUY, 100),
            Signal(d[3], "000001", Side.SELL, 100),
        ]
        result1 = _run(quotes, signals)
        result2 = _run(quotes, signals)

        report_gen = ReportGenerator()
        md1 = report_gen.generate_markdown(result1, to_decimal(100000))
        md2 = report_gen.generate_markdown(result2, to_decimal(100000))

        assert md1 == md2, "两次 Markdown 报告不一致"


# ------------------------------------------------------------------ #
# 5-7. DataFrame 两次完全相等
# ------------------------------------------------------------------ #
class TestDataFrameReproducibility:
    """orders、fills、daily equity DataFrame 两次完全相等。"""

    def test_orders_dataframe_equal(self):
        quotes = make_quotes("000001", date(2024, 1, 2), 10, base_price=10.0)
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [
            Signal(d[0], "000001", Side.BUY, 100),
            Signal(d[3], "000001", Side.SELL, 100),
        ]
        result1 = _run(quotes, signals)
        result2 = _run(quotes, signals)

        report_gen = ReportGenerator()
        df1 = report_gen.generate_orders_dataframe(result1)
        df2 = report_gen.generate_orders_dataframe(result2)

        pd.testing.assert_frame_equal(df1, df2)

    def test_fills_dataframe_equal(self):
        quotes = make_quotes("000001", date(2024, 1, 2), 10, base_price=10.0)
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [
            Signal(d[0], "000001", Side.BUY, 100),
            Signal(d[3], "000001", Side.SELL, 100),
        ]
        result1 = _run(quotes, signals)
        result2 = _run(quotes, signals)

        report_gen = ReportGenerator()
        df1 = report_gen.generate_fills_dataframe(result1)
        df2 = report_gen.generate_fills_dataframe(result2)

        pd.testing.assert_frame_equal(df1, df2)

    def test_equity_dataframe_equal(self):
        quotes = make_quotes("000001", date(2024, 1, 2), 10, base_price=10.0)
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [
            Signal(d[0], "000001", Side.BUY, 100),
            Signal(d[3], "000001", Side.SELL, 100),
        ]
        result1 = _run(quotes, signals)
        result2 = _run(quotes, signals)

        report_gen = ReportGenerator()
        df1 = report_gen.generate_equity_dataframe(result1)
        df2 = report_gen.generate_equity_dataframe(result2)

        pd.testing.assert_frame_equal(df1, df2)


# ------------------------------------------------------------------ #
# 8. content_hash 完全一致
# ------------------------------------------------------------------ #
class TestContentHashReproducibility:
    """content_hash 两次完全一致。"""

    def test_content_hash_identical(self):
        quotes = make_quotes("000001", date(2024, 1, 2), 10, base_price=10.0)
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [
            Signal(d[0], "000001", Side.BUY, 100),
            Signal(d[3], "000001", Side.SELL, 100),
        ]
        result1 = _run(quotes, signals)
        result2 = _run(quotes, signals)

        assert result1.content_hash == result2.content_hash
        assert result1.content_hash is not None
        assert len(result1.content_hash) == 64  # SHA-256 hex


# ------------------------------------------------------------------ #
# 9. 改变任一经济结果时 content_hash 必须变化
# ------------------------------------------------------------------ #
class TestContentHashSensitivity:
    """改变任一经济结果时 content_hash 必须变化。"""

    def test_different_initial_cash_changes_hash(self):
        quotes = make_quotes("000001", date(2024, 1, 2), 10, base_price=10.0)
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [Signal(d[0], "000001", Side.BUY, 100)]

        result1 = _run(quotes, signals, BacktestConfig(initial_cash=100000))
        result2 = _run(quotes, signals, BacktestConfig(initial_cash=200000))

        assert result1.content_hash != result2.content_hash, (
            "不同初始资金应产生不同 content_hash"
        )

    def test_different_signals_change_hash(self):
        quotes = make_quotes("000001", date(2024, 1, 2), 10, base_price=10.0)
        d = make_trade_dates(date(2024, 1, 2), 10)

        result1 = _run(quotes, [Signal(d[0], "000001", Side.BUY, 100)])
        result2 = _run(quotes, [Signal(d[0], "000001", Side.BUY, 200)])

        assert result1.content_hash != result2.content_hash, (
            "不同数量信号应产生不同 content_hash"
        )

    def test_different_data_changes_hash(self):
        quotes1 = make_quotes("000001", date(2024, 1, 2), 10, base_price=10.0)
        quotes2 = make_quotes("000001", date(2024, 1, 2), 10, base_price=20.0)
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [Signal(d[0], "000001", Side.BUY, 100)]

        result1 = _run(quotes1, signals)
        result2 = _run(quotes2, signals)

        assert result1.content_hash != result2.content_hash, (
            "不同行情数据应产生不同 content_hash"
        )

    def test_rejected_order_detail_changes_hash(self):
        """拒单详情不同时 content_hash 必须不同。"""
        quotes = make_quotes("000001", date(2024, 1, 2), 10, base_price=10.0)
        d = make_trade_dates(date(2024, 1, 2), 10)

        # 正常买入
        result1 = _run(quotes, [Signal(d[0], "000001", Side.BUY, 100)])

        # 买入不存在的 symbol（会被 UniverseFilter 拒绝）
        result2 = _run(quotes, [Signal(d[0], "999999", Side.BUY, 100)])

        assert result1.content_hash != result2.content_hash


# ------------------------------------------------------------------ #
# 10. 两条完全相同的重复信号 ID 唯一且重复运行稳定
# ------------------------------------------------------------------ #
class TestDuplicateSignalIds:
    """两条完全相同的重复信号获得不同但稳定的 ID。"""

    def test_duplicate_signals_get_unique_ids(self):
        """同一信号日、同一 symbol、同一 side、同一 quantity 的两条信号
        必须获得不同的 order_id。"""
        quotes = make_quotes("000001", date(2024, 1, 2), 10, base_price=10.0)
        d = make_trade_dates(date(2024, 1, 2), 10)
        # 两条完全相同的信号
        signals = [
            Signal(d[0], "000001", Side.BUY, 100, "dup1"),
            Signal(d[0], "000001", Side.BUY, 100, "dup2"),
        ]
        result = _run(quotes, signals)

        ids = [o.order_id for o in result.orders]
        assert len(ids) == 2
        assert len(set(ids)) == 2, f"两条重复信号 ID 必须唯一: {ids}"

    def test_duplicate_signals_stable_across_runs(self):
        """重复信号的 ID 在多次运行中保持稳定。"""
        quotes = make_quotes("000001", date(2024, 1, 2), 10, base_price=10.0)
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [
            Signal(d[0], "000001", Side.BUY, 100, "dup1"),
            Signal(d[0], "000001", Side.BUY, 100, "dup2"),
        ]
        result1 = _run(quotes, signals)
        result2 = _run(quotes, signals)

        ids1 = [o.order_id for o in result1.orders]
        ids2 = [o.order_id for o in result2.orders]
        assert ids1 == ids2, "重复信号 ID 跨运行不稳定"


# ------------------------------------------------------------------ #
# 11. 不同订单不能发生 ID 冲突
# ------------------------------------------------------------------ #
class TestNoIdCollision:
    """不同订单不能发生 ID 冲突。"""

    def test_multiple_different_orders_no_collision(self):
        """多个不同订单的 ID 全部唯一。"""
        quotes = make_quotes("000001", date(2024, 1, 2), 10, base_price=10.0)
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [
            Signal(d[0], "000001", Side.BUY, 100, "buy1"),
            Signal(d[1], "000001", Side.BUY, 200, "buy2"),
            Signal(d[2], "000001", Side.SELL, 100, "sell1"),
            Signal(d[3], "000001", Side.BUY, 100, "buy3"),
            Signal(d[4], "000001", Side.SELL, 200, "sell2"),
        ]
        result = _run(quotes, signals)

        ids = [o.order_id for o in result.orders]
        assert len(ids) == len(signals)
        assert len(set(ids)) == len(ids), f"存在 ID 冲突: {ids}"

    def test_order_id_format(self):
        """订单 ID 格式为 16 位十六进制字符串（SHA-256 截断）。"""
        quotes = make_quotes("000001", date(2024, 1, 2), 10, base_price=10.0)
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [Signal(d[0], "000001", Side.BUY, 100)]
        result = _run(quotes, signals)

        import re
        pattern = re.compile(r"^[0-9a-f]{16}$")
        for o in result.orders:
            assert pattern.match(o.order_id), (
                f"order_id 格式不符: {o.order_id}"
            )


# ------------------------------------------------------------------ #
# 12. JSON 字符串完全一致（含 order_id 字段）
# ------------------------------------------------------------------ #
class TestJsonStringEquality:
    """完整 JSON 字符串两次完全一致，不忽略 order_id。"""

    def test_json_string_identical(self):
        quotes = make_quotes("000001", date(2024, 1, 2), 10, base_price=10.0)
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [
            Signal(d[0], "000001", Side.BUY, 100),
            Signal(d[3], "000001", Side.SELL, 100),
        ]
        result1 = _run(quotes, signals)
        result2 = _run(quotes, signals)

        report_gen = ReportGenerator()
        str1 = report_gen.to_json_string(result1, to_decimal(100000))
        str2 = report_gen.to_json_string(result2, to_decimal(100000))

        assert str1 == str2, "JSON 字符串不一致"

    def test_json_contains_order_ids(self):
        """JSON 中必须包含 order_id 字段，不能删除。"""
        quotes = make_quotes("000001", date(2024, 1, 2), 10, base_price=10.0)
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [Signal(d[0], "000001", Side.BUY, 100)]
        result = _run(quotes, signals)

        report_gen = ReportGenerator()
        report = report_gen.generate_json(result, to_decimal(100000))

        for order in report["orders"]:
            assert "order_id" in order
            assert order["order_id"] != ""

        for fill in report["fills"]:
            assert "order_id" in fill
            assert fill["order_id"] != ""
