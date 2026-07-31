"""DefaultUniverseFilter 的综合 pytest 测试。

覆盖范围（按 Phase 2 基础过滤顺序）：
1. 无行情数据（空 DataFrame / None）
2. 上市/退市区间外（symbol 不在数据中）
3. 当日无行情记录（指定日期无数据）
4. 停牌（is_suspended=True）
5. 不可交易（is_tradable=False）
6. 无效收盘价（close_raw <= 0）
7. 一手最低购买金额不足（close_raw * lot_size < min_lot_value）
8. 全部校验通过
9. 自定义 min_lot_value 过滤

数据由 ``tests.backtest_samples`` 合成。
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Optional

import pandas as pd
import pytest

from ashare_quant.backtest.config import BacktestConfig, UniverseConfig
from ashare_quant.backtest.models import PortfolioSnapshot, Position, StrategyContext
from ashare_quant.backtest.universe import DefaultUniverseFilter
from tests.backtest_samples import *  # noqa: F401,F403  —— 按规范要求使用 star import
from tests.backtest_samples import make_bar, make_quotes


# --------------------------------------------------------------------------- #
# 辅助构建函数
# --------------------------------------------------------------------------- #
DEFAULT_SYMBOL = "000001"
DEFAULT_DATE = date(2024, 1, 2)


def build_context(bars_up_to_date: Any) -> StrategyContext:
    """构建 ``StrategyContext``，填充默认组合快照与空持仓。

    Args:
        bars_up_to_date: 当日及之前的行情 DataFrame（可为 ``None`` 或空 DataFrame）。
    """
    snap = PortfolioSnapshot(
        snapshot_date=DEFAULT_DATE,
        cash=Decimal("100000"),
        position_value=Decimal("0"),
        total_equity=Decimal("100000"),
    )
    return StrategyContext(
        current_date=DEFAULT_DATE,
        portfolio=snap,
        positions={},
        bars_up_to_date=bars_up_to_date,
    )


def build_single_day_df(
    *,
    symbol: str = DEFAULT_SYMBOL,
    dt: date = DEFAULT_DATE,
    close: float = 10.0,
    is_suspended: bool = False,
    is_tradable: bool = True,
) -> pd.DataFrame:
    """构造单日单标的行为 DataFrame（基于 ``make_bar``）。

    便于精确控制 ``close_raw`` / ``is_suspended`` / ``is_tradable`` 字段。
    """
    return pd.DataFrame(
        [
            make_bar(
                symbol=symbol,
                dt=dt,
                close=close,
                is_suspended=is_suspended,
                is_tradable=is_tradable,
            )
        ]
    )


@pytest.fixture
def universe_filter() -> DefaultUniverseFilter:
    """默认股票池过滤器（min_lot_value=1000、lot_size=100）。"""
    return DefaultUniverseFilter()


# --------------------------------------------------------------------------- #
# 1. 无行情数据
# --------------------------------------------------------------------------- #
class TestNoData:
    """bars_up_to_date 为空或 None 时不可交易。"""

    def test_empty_dataframe_ineligible(self, universe_filter):
        ctx = build_context(pd.DataFrame())
        decision = universe_filter.is_eligible(DEFAULT_SYMBOL, DEFAULT_DATE, ctx)
        assert decision.eligible is False

    def test_none_bars_ineligible(self, universe_filter):
        ctx = build_context(None)
        decision = universe_filter.is_eligible(DEFAULT_SYMBOL, DEFAULT_DATE, ctx)
        assert decision.eligible is False


# --------------------------------------------------------------------------- #
# 2. 上市/退市区间外（symbol 不在数据中）
# --------------------------------------------------------------------------- #
class TestSymbolNotInData:
    """symbol 在 DataFrame 中无任何记录时不可交易。"""

    def test_symbol_not_in_data_ineligible(self, universe_filter):
        df = make_quotes(symbol="000001", start=date(2024, 1, 2), n_days=5)
        ctx = build_context(df)
        decision = universe_filter.is_eligible("999999", DEFAULT_DATE, ctx)
        assert decision.eligible is False
        assert "上市/退市区间" in decision.reason


# --------------------------------------------------------------------------- #
# 3. 当日无行情记录
# --------------------------------------------------------------------------- #
class TestNoDataForDate:
    """symbol 存在但指定日期无记录时不可交易。"""

    def test_date_not_in_data_ineligible(self, universe_filter):
        # make_quotes 生成 2024-01-02 起 5 个交易日，2024-02-01 不在其中
        df = make_quotes(symbol="000001", start=date(2024, 1, 2), n_days=5)
        ctx = build_context(df)
        decision = universe_filter.is_eligible(
            "000001", date(2024, 2, 1), ctx
        )
        assert decision.eligible is False
        assert "当日无行情记录" in decision.reason


# --------------------------------------------------------------------------- #
# 4. 停牌
# --------------------------------------------------------------------------- #
class TestSuspended:
    """is_suspended=True 时不可交易。"""

    def test_suspended_ineligible(self, universe_filter):
        df = build_single_day_df(
            dt=DEFAULT_DATE, close=10.0, is_suspended=True, is_tradable=True
        )
        ctx = build_context(df)
        decision = universe_filter.is_eligible(DEFAULT_SYMBOL, DEFAULT_DATE, ctx)
        assert decision.eligible is False
        assert "停牌或不可交易" in decision.reason


# --------------------------------------------------------------------------- #
# 5. 不可交易
# --------------------------------------------------------------------------- #
class TestNotTradable:
    """is_tradable=False 时不可交易。"""

    def test_not_tradable_ineligible(self, universe_filter):
        df = build_single_day_df(
            dt=DEFAULT_DATE, close=10.0, is_suspended=False, is_tradable=False
        )
        ctx = build_context(df)
        decision = universe_filter.is_eligible(DEFAULT_SYMBOL, DEFAULT_DATE, ctx)
        assert decision.eligible is False
        assert "停牌或不可交易" in decision.reason


# --------------------------------------------------------------------------- #
# 6. 无效收盘价
# --------------------------------------------------------------------------- #
class TestInvalidPrice:
    """close_raw <= 0 时不可交易（无效价格）。"""

    @pytest.mark.parametrize("close", [0.0, -1.0])
    def test_invalid_close_rejected(self, universe_filter, close):
        df = build_single_day_df(dt=DEFAULT_DATE, close=close)
        ctx = build_context(df)
        decision = universe_filter.is_eligible(DEFAULT_SYMBOL, DEFAULT_DATE, ctx)
        assert decision.eligible is False
        assert "无效收盘价" in decision.reason


# --------------------------------------------------------------------------- #
# 7. 一手最低购买金额不足
# --------------------------------------------------------------------------- #
class TestLotValueBelowMinimum:
    """close_raw * lot_size < min_lot_value 时不可交易。"""

    def test_lot_value_below_minimum_ineligible(self, universe_filter):
        # close=5.0, lot_size=100 -> lot_value=500 < 默认 min_lot_value=1000
        df = build_single_day_df(dt=DEFAULT_DATE, close=5.0)
        ctx = build_context(df)
        decision = universe_filter.is_eligible(DEFAULT_SYMBOL, DEFAULT_DATE, ctx)
        assert decision.eligible is False
        assert "一手金额" in decision.reason


# --------------------------------------------------------------------------- #
# 8. 全部校验通过
# --------------------------------------------------------------------------- #
class TestAllChecksPass:
    """正常股票（有效价格、未停牌、一手金额达标）可交易。"""

    def test_normal_stock_eligible(self, universe_filter):
        # close=10.0, lot_size=100 -> lot_value=1000 >= min_lot_value=1000
        df = build_single_day_df(dt=DEFAULT_DATE, close=10.0)
        ctx = build_context(df)
        decision = universe_filter.is_eligible(DEFAULT_SYMBOL, DEFAULT_DATE, ctx)
        assert decision.eligible is True
        assert decision.reason == ""


# --------------------------------------------------------------------------- #
# 9. 自定义 min_lot_value
# --------------------------------------------------------------------------- #
class TestCustomMinLotValue:
    """提高 min_lot_value 后，原本达标的股票被过滤。"""

    def test_custom_min_lot_value_filters_out(self):
        # min_lot_value=2000：close=10.0 -> lot_value=1000 < 2000 -> 不可交易
        cfg = BacktestConfig(universe=UniverseConfig(min_lot_value=2000.0))
        uf = DefaultUniverseFilter(cfg)
        df = build_single_day_df(dt=DEFAULT_DATE, close=10.0)
        ctx = build_context(df)
        decision = uf.is_eligible(DEFAULT_SYMBOL, DEFAULT_DATE, ctx)
        assert decision.eligible is False
        assert "一手金额" in decision.reason

    def test_default_min_lot_value_allows(self, universe_filter):
        # 默认 min_lot_value=1000：close=10.0 -> lot_value=1000 >= 1000 -> 可交易
        df = build_single_day_df(dt=DEFAULT_DATE, close=10.0)
        ctx = build_context(df)
        decision = universe_filter.is_eligible(DEFAULT_SYMBOL, DEFAULT_DATE, ctx)
        assert decision.eligible is True
