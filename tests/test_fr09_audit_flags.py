"""FR-09 严格回归测试：缺少前收盘价时的可归档审计标记。

验证：
1. 首个无前收盘日允许完成其他校验（成交/拒单行为不因缺前收而被误改）
2. 审计标记 limit_check_unavailable 存在于 Order 和 Fill
3. JSON/Markdown/Parquet 可见审计标记
4. 有前收数据时标记不存在
5. 审计标记跨运行稳定（确定性）
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

import pandas as pd
import pytest

from ashare_quant.backtest.config import BacktestConfig
from ashare_quant.backtest.engine import BacktestEngine
from ashare_quant.backtest.interfaces import UniverseFilter
from ashare_quant.backtest.models import (
    BacktestResult,
    EligibilityDecision,
    Fill,
    Order,
    OrderStatus,
    Side,
    Signal,
    StrategyContext,
)
from ashare_quant.backtest.report import ReportGenerator
from ashare_quant.backtest.strategies import ScriptedStrategy
from tests.backtest_samples import make_bar, make_quotes, make_trade_dates


# ------------------------------------------------------------------ #
# 测试辅助：宽松股票池过滤器
# ------------------------------------------------------------------ #
class AlwaysEligibleUniverseFilter(UniverseFilter):
    """测试专用：始终允许交易的股票池过滤器。

    用于构造「标的上市首日即成交」场景，绕过默认过滤器的上市区间检查，
    使得缺少 prev_close_raw 的成交场景可被测试。
    """

    def is_eligible(
        self, symbol: str, dt: date, context: StrategyContext
    ) -> EligibilityDecision:
        return EligibilityDecision(True, "")


# ------------------------------------------------------------------ #
# 数据构建
# ------------------------------------------------------------------ #
def _make_data_with_late_symbol() -> pd.DataFrame:
    """构造数据：000001 从 2024-01-02 开始，600000 从 2024-01-03 开始。

    600000 在 2024-01-03（首个数据日）没有 prev_close_raw。
    """
    dates = make_trade_dates(date(2024, 1, 2), 10)
    rows = []
    # 000001: 全部 10 天
    for i, dt in enumerate(dates):
        rows.append(make_bar("000001", dt, open_price=10.0 + i * 0.1))
    # 600000: 从第 2 天开始（2024-01-03），共 9 天
    for i, dt in enumerate(dates[1:]):
        rows.append(make_bar("600000", dt, open_price=20.0 + i * 0.1))
    return pd.DataFrame(rows)


def _make_normal_data() -> pd.DataFrame:
    """构造数据：000001 全部 10 天，所有日均有 prev_close。"""
    return make_quotes("000001", date(2024, 1, 2), 10, base_price=10.0)


def _run(
    data: pd.DataFrame,
    signals: list[Signal],
    universe_filter: Optional[UniverseFilter] = None,
) -> BacktestResult:
    dates = make_trade_dates(date(2024, 1, 2), 10)
    engine = BacktestEngine()
    cfg = BacktestConfig(initial_cash=100000)
    return engine.run(
        data=data,
        strategy=ScriptedStrategy(signals),
        start_date=dates[0],
        end_date=dates[-1],
        initial_cash=cfg.initial_cash,
        config=cfg,
        universe_filter=universe_filter,
    )


def _run_late(data: pd.DataFrame, signals: list[Signal]) -> BacktestResult:
    """使用宽松股票池过滤器运行回测（用于缺少 prev_close 场景）。"""
    return _run(data, signals, universe_filter=AlwaysEligibleUniverseFilter())


# ------------------------------------------------------------------ #
# 1. 无前收盘价时审计标记存在
# ------------------------------------------------------------------ #
class TestLimitCheckUnavailableFlag:
    """缺少 prev_close_raw 时记录 limit_check_unavailable。"""

    def test_fill_has_audit_flag_when_no_prev_close(self):
        """成交日无前收盘价时 Fill 包含 limit_check_unavailable。"""
        data = _make_data_with_late_symbol()
        d = make_trade_dates(date(2024, 1, 2), 10)
        # 信号在 2024-01-02 生成，2024-01-03 成交
        # 600000 在 2024-01-03 是首个数据日，无 prev_close
        signals = [Signal(d[0], "600000", Side.BUY, 100, "test")]
        result = _run_late(data, signals)

        fills_for_600000 = [f for f in result.fills if f.symbol == "600000"]
        assert len(fills_for_600000) > 0, "600000 应有成交记录"
        fill = fills_for_600000[0]
        assert "limit_check_unavailable" in fill.audit_flags, (
            f"Fill audit_flags 应包含 limit_check_unavailable, 实际: {fill.audit_flags}"
        )

    def test_order_has_audit_flag_when_no_prev_close(self):
        """成交日无前收盘价时 Order 包含 limit_check_unavailable。"""
        data = _make_data_with_late_symbol()
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [Signal(d[0], "600000", Side.BUY, 100, "test")]
        result = _run_late(data, signals)

        orders_for_600000 = [
            o for o in result.orders if o.signal.symbol == "600000"
        ]
        assert len(orders_for_600000) > 0
        order = orders_for_600000[0]
        assert "limit_check_unavailable" in order.audit_flags, (
            f"Order audit_flags 应包含 limit_check_unavailable, 实际: {order.audit_flags}"
        )

    def test_fill_behavior_not_changed_by_missing_prev_close(self):
        """缺前收盘价不改变成交行为（仍正常成交）。"""
        data = _make_data_with_late_symbol()
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [Signal(d[0], "600000", Side.BUY, 100, "test")]
        result = _run_late(data, signals)

        # 订单应正常成交
        orders_for_600000 = [
            o for o in result.orders if o.signal.symbol == "600000"
        ]
        assert len(orders_for_600000) == 1
        assert orders_for_600000[0].status == OrderStatus.FILLED, (
            f"订单应正常成交，实际状态: {orders_for_600000[0].status}"
        )
        assert len(result.fills) == 1


# ------------------------------------------------------------------ #
# 2. 有前收盘价时标记不存在
# ------------------------------------------------------------------ #
class TestNoFlagWhenPrevCloseAvailable:
    """有前收盘价时不添加 limit_check_unavailable 标记。"""

    def test_fill_no_audit_flag_when_prev_close_available(self):
        """成交日有前收盘价时 Fill 不包含 limit_check_unavailable。"""
        data = _make_normal_data()
        d = make_trade_dates(date(2024, 1, 2), 10)
        # 信号在 2024-01-02 生成，2024-01-03 成交
        # 000001 在 2024-01-03 有 prev_close（来自 2024-01-02）
        signals = [Signal(d[0], "000001", Side.BUY, 100, "test")]
        result = _run(data, signals)

        fills = [f for f in result.fills if f.symbol == "000001"]
        assert len(fills) > 0
        fill = fills[0]
        assert "limit_check_unavailable" not in fill.audit_flags, (
            f"有前收盘价时不应有 limit_check_unavailable, 实际: {fill.audit_flags}"
        )
        assert fill.audit_flags == [], (
            f"有前收盘价时 audit_flags 应为空, 实际: {fill.audit_flags}"
        )

    def test_order_no_audit_flag_when_prev_close_available(self):
        """成交日有前收盘价时 Order 不包含 limit_check_unavailable。"""
        data = _make_normal_data()
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [Signal(d[0], "000001", Side.BUY, 100, "test")]
        result = _run(data, signals)

        orders = [o for o in result.orders if o.signal.symbol == "000001"]
        assert len(orders) > 0
        order = orders[0]
        assert "limit_check_unavailable" not in order.audit_flags, (
            f"有前收盘价时不应有 limit_check_unavailable, 实际: {order.audit_flags}"
        )


# ------------------------------------------------------------------ #
# 3. JSON/Markdown/Parquet 可见审计标记
# ------------------------------------------------------------------ #
class TestAuditFlagInReports:
    """审计标记出现在 JSON、Markdown 和 Parquet 中。"""

    def test_json_contains_audit_flags(self):
        """JSON 报告包含 audit_flags。"""
        data = _make_data_with_late_symbol()
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [Signal(d[0], "600000", Side.BUY, 100, "test")]
        result = _run_late(data, signals)

        report_gen = ReportGenerator()
        report = report_gen.generate_json(result, Decimal("100000"))

        # 检查 fills
        fills = report.get("fills", [])
        assert len(fills) > 0
        fill = fills[0]
        assert "audit_flags" in fill
        assert "limit_check_unavailable" in fill["audit_flags"]

        # 检查 orders
        orders = report.get("orders", [])
        assert len(orders) > 0
        order = orders[0]
        assert "audit_flags" in order
        assert "limit_check_unavailable" in order["audit_flags"]

    def test_markdown_contains_audit_flag(self):
        """Markdown 报告包含审计标记。"""
        data = _make_data_with_late_symbol()
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [Signal(d[0], "600000", Side.BUY, 100, "test")]
        result = _run_late(data, signals)

        report_gen = ReportGenerator()
        md = report_gen.generate_markdown(result, Decimal("100000"))

        assert "limit_check_unavailable" in md

    def test_orders_parquet_contains_audit_flags(self):
        """orders Parquet 包含 audit_flags 列。"""
        data = _make_data_with_late_symbol()
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [Signal(d[0], "600000", Side.BUY, 100, "test")]
        result = _run_late(data, signals)

        report_gen = ReportGenerator()
        orders_df = report_gen.generate_orders_dataframe(result)

        assert "audit_flags" in orders_df.columns
        # 第一行应包含 limit_check_unavailable
        first_flags = orders_df.iloc[0]["audit_flags"]
        assert "limit_check_unavailable" in first_flags

    def test_fills_parquet_contains_audit_flags(self):
        """fills Parquet 包含 audit_flags 列。"""
        data = _make_data_with_late_symbol()
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [Signal(d[0], "600000", Side.BUY, 100, "test")]
        result = _run_late(data, signals)

        report_gen = ReportGenerator()
        fills_df = report_gen.generate_fills_dataframe(result)

        assert "audit_flags" in fills_df.columns
        assert len(fills_df) > 0
        first_flags = fills_df.iloc[0]["audit_flags"]
        assert "limit_check_unavailable" in first_flags


# ------------------------------------------------------------------ #
# 4. 拒单场景下的审计标记
# ------------------------------------------------------------------ #
class TestAuditFlagOnRejectedOrder:
    """被拒绝的订单也应有审计标记（如果缺前收盘价）。"""

    def test_rejected_order_has_audit_flag(self):
        """被风控拒绝的订单（缺前收盘价）也有审计标记。"""
        data = _make_data_with_late_symbol()
        d = make_trade_dates(date(2024, 1, 2), 10)
        # 构造一个会被拒的信号：买入远超资金的量
        signals = [Signal(d[0], "600000", Side.BUY, 1000000, "test")]
        result = _run_late(data, signals)

        orders_for_600000 = [
            o for o in result.orders if o.signal.symbol == "600000"
        ]
        assert len(orders_for_600000) == 1
        order = orders_for_600000[0]
        # 应被拒绝（现金不足）
        assert order.status in (OrderStatus.REJECTED, OrderStatus.CANCELLED)
        # 即使被拒，也应有审计标记
        assert "limit_check_unavailable" in order.audit_flags


# ------------------------------------------------------------------ #
# 5. 审计标记跨运行稳定（确定性）
# ------------------------------------------------------------------ #
class TestAuditFlagDeterministic:
    """审计标记跨运行稳定。"""

    def test_audit_flags_identical_across_runs(self):
        """相同输入重复运行 audit_flags 一致。"""
        data = _make_data_with_late_symbol()
        d = make_trade_dates(date(2024, 1, 2), 10)
        signals = [Signal(d[0], "600000", Side.BUY, 100, "test")]

        result1 = _run_late(data, signals)
        result2 = _run_late(data, signals)

        flags1 = [list(o.audit_flags) for o in result1.orders]
        flags2 = [list(o.audit_flags) for o in result2.orders]
        assert flags1 == flags2

        fill_flags1 = [list(f.audit_flags) for f in result1.fills]
        fill_flags2 = [list(f.audit_flags) for f in result2.fills]
        assert fill_flags1 == fill_flags2
