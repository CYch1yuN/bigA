"""默认股票池过滤器。

Phase 2 仅实现基础过滤：

1. 上市/退市区间：``context.bars_up_to_date`` 中该 symbol 无任何数据 -> 不可交易
2. ST 状态：Phase 2 不实现详细 ST 过滤，暂以「当日是否存在该 symbol 的记录」
   作为代理判断（无当日记录即不可交易）
3. 停牌：当日 ``is_suspended``（并兜底 ``is_tradable``）
4. 有效价格：当日 ``close_raw > 0``
5. 一手最低购买金额：``close_raw * lot_size >= universe.min_lot_value``

120 日上市期、流动性与趋势过滤留待 Phase 3。

数据来源为 ``StrategyContext.bars_up_to_date``（curated 日行情 DataFrame，
列名遵循 ``ashare_quant.constants.DAILY_QUOTE_FIELDS``：``symbol``、``trade_date``、
``close_raw``、``is_suspended``、``is_tradable`` 等）。
"""
from __future__ import annotations

from datetime import date
from typing import Optional

import pandas as pd

from .config import BacktestConfig
from .interfaces import UniverseFilter
from .models import EligibilityDecision, StrategyContext, to_decimal

__all__ = ["DefaultUniverseFilter"]


class DefaultUniverseFilter(UniverseFilter):
    """默认股票池过滤器（Phase 2 基础实现）。

    Args:
        config: 回测配置，用于读取 ``universe.min_lot_value`` 与 ``lot_size``。
            为 ``None`` 时使用默认 :class:`BacktestConfig`。
    """

    def __init__(self, config: Optional[BacktestConfig] = None) -> None:
        self._config = config if config is not None else BacktestConfig()

    def is_eligible(
        self, symbol: str, dt: date, context: StrategyContext
    ) -> EligibilityDecision:
        """判断某股票在某日是否可交易。

        Args:
            symbol: 股票代码。
            dt: 交易日。
            context: 策略上下文。

        Returns:
            过滤决策。
        """
        df = context.bars_up_to_date

        # 1. 上市/退市区间：该 symbol 是否有任何行情数据
        if df is None or len(df) == 0:
            return EligibilityDecision(False, "无行情数据，无法判断可交易性")
        sym_df = df[df["symbol"] == symbol]
        if sym_df.empty:
            return EligibilityDecision(
                False, f"{symbol}: 上市/退市区间外（无该标的任何行情数据）"
            )

        # 2. 当日记录存在性（Phase 2 ST 代理：无当日记录即不可交易）
        row = self._select_day(sym_df, dt)
        if row is None:
            return EligibilityDecision(
                False,
                f"{symbol}: {dt} 当日无行情记录（停牌无数据或上市/退市区间外）",
            )

        # 3. 停牌 / 不可交易
        is_suspended = bool(row.get("is_suspended", False))
        is_tradable = bool(row.get("is_tradable", True))
        if is_suspended or not is_tradable:
            return EligibilityDecision(
                False,
                f"{symbol}: {dt} 停牌或不可交易"
                f"(is_suspended={is_suspended}, is_tradable={is_tradable})",
            )

        # 4. 有效价格
        close_raw = to_decimal(row.get("close_raw", 0))
        if not (close_raw > 0):
            return EligibilityDecision(
                False, f"{symbol}: {dt} 无效收盘价(close_raw={close_raw})"
            )

        # 5. 一手最低购买金额：close_raw * lot_size >= min_lot_value
        lot_size = self._config.lot_size
        min_lot_value = to_decimal(self._config.universe.min_lot_value)
        lot_value = close_raw * to_decimal(lot_size)
        if lot_value < min_lot_value:
            return EligibilityDecision(
                False,
                f"{symbol}: {dt} 一手金额 {lot_value:.2f} < 最低 {min_lot_value:.2f}",
            )

        return EligibilityDecision(True, "")

    # ------------------------------------------------------------------ #
    # 辅助方法
    # ------------------------------------------------------------------ #
    @staticmethod
    def _select_day(
        sym_df: pd.DataFrame, dt: date
    ) -> Optional[pd.Series]:
        """从 symbol 子集中选取指定日期的首行；不存在返回 ``None``。

        兼容 ``trade_date`` 列为 ``date`` 对象、``datetime64`` 或字符串的情况。
        """
        td = sym_df["trade_date"]
        if pd.api.types.is_datetime64_any_dtype(td):
            day_mask = td.dt.date == dt
        elif pd.api.types.is_object_dtype(td):
            day_mask = td == dt
        else:
            day_mask = pd.to_datetime(td).dt.date == dt
        matched = sym_df[day_mask]
        if matched.empty:
            return None
        return matched.iloc[0]
