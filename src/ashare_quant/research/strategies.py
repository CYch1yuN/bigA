"""Phase 3 双轨策略实现。

稳健轨：周频调仓，趋势+动量+波动率综合得分，最多持有一只股票。
激进轨：日频信号，突破+放量+相对强度入场，多条件退出，最多持有一只股票。

两条轨道换仓信号固定为先 SELL 后 BUY。所有特征仅使用截至信号日收盘的前复权数据，
禁止未来函数。突破和均量窗口不包含信号日（使用 shift(1) 排除当日）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from itertools import groupby
from typing import Any, Optional

import numpy as np
import pandas as pd

from ..backtest.interfaces import Strategy
from ..backtest.models import Signal, Side, StrategyContext
from .features import (
    compute_moving_average,
    compute_momentum,
    compute_volatility,
    compute_breakout,
    compute_volume_ratio,
    compute_relative_strength,
    zscore_cross_sectional,
    compute_trend_score,
    compute_steady_score,
)
from .universe import HistoricalUniverseFilter

__all__ = [
    "SteadyParams",
    "AggressiveParams",
    "SteadyStrategy",
    "AggressiveStrategy",
    "STEADY_PARAM_CANDIDATES",
    "AGGRESSIVE_PARAM_CANDIDATES",
    "STEADY_BASELINE_PARAMS",
    "AGGRESSIVE_BASELINE_PARAMS",
]

# ---------------------------------------------------------------------- #
# 参数候选集合（固定，来自任务书）
# ---------------------------------------------------------------------- #

STEADY_PARAM_CANDIDATES: dict[str, list] = {
    "trend_window": [100, 120, 140],
    "momentum_window": [50, 60, 70],
    "volatility_window": [15, 20, 25],
    "minimum_score": [-0.25, 0.0, 0.25],
}

AGGRESSIVE_PARAM_CANDIDATES: dict[str, list] = {
    "breakout_window": [15, 20, 25],
    "volume_window": [15, 20, 25],
    "volume_ratio": [1.2, 1.5, 1.8],
    "relative_strength_window": [15, 20, 25],
    "exit_low_window": [8, 10, 12],
    "max_holding_days": [15, 20, 25],
}


@dataclass(frozen=True)
class SteadyParams:
    """稳健轨参数。"""

    trend_window: int = 120
    momentum_window: int = 60
    volatility_window: int = 20
    minimum_score: float = 0.0


@dataclass(frozen=True)
class AggressiveParams:
    """激进轨参数。"""

    breakout_window: int = 20
    volume_window: int = 20
    volume_ratio: float = 1.5
    relative_strength_window: int = 20
    exit_low_window: int = 10
    max_holding_days: int = 20


STEADY_BASELINE_PARAMS = SteadyParams()
AGGRESSIVE_BASELINE_PARAMS = AggressiveParams()

# 费用缓冲因子（滑点 10bps + 佣金 ~0.04% + 过户费 0.001% ≈ 0.5%）
_COST_BUFFER: float = 1.005


def _to_date(val: Any) -> Optional[date]:
    """安全转换为 date。"""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    if isinstance(val, pd.Timestamp):
        return val.date()
    try:
        if pd.isna(val):
            return None
    except (TypeError, ValueError):
        pass
    return pd.Timestamp(val).date()


def _safe_float(val: Any) -> float:
    """安全转换为 float。"""
    try:
        result = float(val)
        if math.isnan(result) or math.isinf(result):
            return 0.0
        return result
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------- #
# 稳健轨策略
# ---------------------------------------------------------------------- #


class SteadyStrategy(Strategy):
    """稳健轨策略：周频调仓，综合趋势/动量/波动率得分。

    调仓规则：
    - 每周最后一个有效交易日收盘生成信号，下一交易日开盘成交。
    - 最多持有一只股票。
    - 对通过历史股票池过滤的标的计算特征和横截面 z-score 得分。
    - 得分 = zscore(trend) + zscore(momentum) - zscore(volatility)。
    - 最高得分 > minimum_score 时买入；否则持有现金。
    - 换仓信号顺序固定为先 SELL 后 BUY。

    所有特征仅使用截至信号日收盘的前复权数据，禁止未来函数。
    横截面 z-score 只使用同一信号日通过股票池过滤的股票，有效样本 < 5 时不生成买入信号。

    Args:
        params: 稳健轨参数。
        universe_filter: 历史时点股票池过滤器。
        trading_dates: 交易日列表（用于确定周频调仓日）。
        lot_size: 一手股数，默认 100。
        benchmark_hs300: 沪深300收盘价字典（可选，稳健轨不使用但保持接口一致）。
    """

    def __init__(
        self,
        params: SteadyParams,
        universe_filter: HistoricalUniverseFilter,
        trading_dates: list[date],
        lot_size: int = 100,
    ) -> None:
        self._params = params
        self._universe_filter = universe_filter
        self._trading_dates = sorted(set(trading_dates))
        self._lot_size = lot_size
        self._weekly_dates = self._compute_weekly_dates()

    def on_close(self, context: StrategyContext) -> list[Signal]:
        """收盘后生成交易信号。"""
        dt = context.current_date

        # 仅在每周最后一个交易日调仓
        if dt not in self._weekly_dates:
            return []

        bars = context.bars_up_to_date
        if bars is None or len(bars) == 0:
            return []

        # 获取当前持仓
        current_holding = self._get_current_holding(context.positions)

        # 获取截至当日的所有 symbol
        bars_up_to = bars[bars["trade_date"] <= dt] if "trade_date" in bars.columns else bars
        if len(bars_up_to) == 0:
            return []

        symbols = sorted(bars_up_to["symbol"].unique().tolist())

        # 对每个 symbol 执行股票池过滤
        eligible: list[str] = []
        for sym in symbols:
            decision = self._universe_filter.is_eligible(sym, dt, context)
            if decision.eligible:
                eligible.append(sym)

        if len(eligible) < 5:
            # 有效样本不足 5 个，不生成买入信号
            signals: list[Signal] = []
            if current_holding is not None:
                sell_qty = self._get_sellable_quantity(context.positions, current_holding)
                if sell_qty > 0:
                    signals.append(Signal(
                        signal_date=dt,
                        symbol=current_holding,
                        side=Side.SELL,
                        quantity=sell_qty,
                        reason="稳健轨: 有效样本不足5, 清仓",
                    ))
            return signals

        # 计算特征
        trend_values: dict[str, float] = {}
        momentum_values: dict[str, float] = {}
        volatility_values: dict[str, float] = {}
        close_raw_values: dict[str, float] = {}

        for sym in eligible:
            sym_data = bars_up_to[bars_up_to["symbol"] == sym].sort_values("trade_date")
            if len(sym_data) == 0:
                continue

            close_qfq = sym_data["close_qfq"].astype(float)
            close_raw = _safe_float(sym_data.iloc[-1]["close_raw"])

            # 趋势得分: close_qfq / MA(trend_window) - 1
            ma = compute_moving_average(close_qfq, self._params.trend_window)
            if ma.iloc[-1] is None or pd.isna(ma.iloc[-1]) or ma.iloc[-1] <= 0:
                continue
            trend = float(close_qfq.iloc[-1] / ma.iloc[-1] - 1.0)

            # 基线要求 close_qfq > MA
            if close_qfq.iloc[-1] <= ma.iloc[-1]:
                continue

            # 动量: close_qfq / close_qfq.shift(momentum_window) - 1
            momentum = compute_momentum(close_qfq, self._params.momentum_window)
            if pd.isna(momentum.iloc[-1]):
                continue
            mom_val = float(momentum.iloc[-1])

            # 波动率: 过去 volatility_window 日复权日收益标准差 * sqrt(244)
            daily_returns = close_qfq.pct_change()
            vol = compute_volatility(daily_returns, self._params.volatility_window)
            if pd.isna(vol.iloc[-1]):
                continue
            vol_val = float(vol.iloc[-1])

            trend_values[sym] = trend
            momentum_values[sym] = mom_val
            volatility_values[sym] = vol_val
            close_raw_values[sym] = close_raw

        if len(trend_values) < 5:
            signals = []
            if current_holding is not None:
                sell_qty = self._get_sellable_quantity(context.positions, current_holding)
                if sell_qty > 0:
                    signals.append(Signal(
                        signal_date=dt,
                        symbol=current_holding,
                        side=Side.SELL,
                        quantity=sell_qty,
                        reason="稳健轨: 有效特征样本不足5, 清仓",
                    ))
            return signals

        # 构建横截面 Series
        symbols_valid = sorted(trend_values.keys())
        trend_series = pd.Series(
            [trend_values[s] for s in symbols_valid], index=symbols_valid
        )
        momentum_series = pd.Series(
            [momentum_values[s] for s in symbols_valid], index=symbols_valid
        )
        volatility_series = pd.Series(
            [volatility_values[s] for s in symbols_valid], index=symbols_valid
        )

        # 计算综合得分
        scores = compute_steady_score(trend_series, momentum_series, volatility_series)

        # 找到最高得分
        valid_scores = scores.dropna()
        if len(valid_scores) == 0:
            signals = []
            if current_holding is not None:
                sell_qty = self._get_sellable_quantity(context.positions, current_holding)
                if sell_qty > 0:
                    signals.append(Signal(
                        signal_date=dt,
                        symbol=current_holding,
                        side=Side.SELL,
                        quantity=sell_qty,
                        reason="稳健轨: 无有效得分, 清仓",
                    ))
            return signals

        best_symbol = valid_scores.idxmax()
        best_score = float(valid_scores.max())

        signals = []

        # 如果得分不达标，清仓
        if best_score <= self._params.minimum_score:
            if current_holding is not None:
                sell_qty = self._get_sellable_quantity(context.positions, current_holding)
                if sell_qty > 0:
                    signals.append(Signal(
                        signal_date=dt,
                        symbol=current_holding,
                        side=Side.SELL,
                        quantity=sell_qty,
                        reason=f"稳健轨: 最高得分{best_score:.4f} <= {self._params.minimum_score}, 清仓",
                    ))
            return signals

        # 如果需要换仓
        if current_holding is not None and current_holding != best_symbol:
            sell_qty = self._get_sellable_quantity(context.positions, current_holding)
            if sell_qty > 0:
                signals.append(Signal(
                    signal_date=dt,
                    symbol=current_holding,
                    side=Side.SELL,
                    quantity=sell_qty,
                    reason=f"稳健轨: 换仓 {current_holding} -> {best_symbol}",
                ))

        # 买入信号（仅当不持有该标的或已卖出）
        if current_holding != best_symbol or current_holding is None:
            cash = float(context.portfolio.cash)
            close_raw = close_raw_values.get(best_symbol, 0.0)
            buy_qty = self._calc_buy_quantity(cash, close_raw)
            if buy_qty > 0:
                signals.append(Signal(
                    signal_date=dt,
                    symbol=best_symbol,
                    side=Side.BUY,
                    quantity=buy_qty,
                    reason=f"稳健轨: 得分{best_score:.4f}, 买入{best_symbol}",
                ))

        return signals

    # ------------------------------------------------------------------ #
    # 内部辅助
    # ------------------------------------------------------------------ #

    def _compute_weekly_dates(self) -> set[date]:
        """预计算每周最后一个交易日的集合。"""
        if not self._trading_dates:
            return set()

        sorted_dates = sorted(self._trading_dates)
        weekly_dates: set[date] = set()

        def week_key(d: date) -> tuple[int, int]:
            ic = d.isocalendar()
            return (ic[0], ic[1])

        for _key, group in groupby(sorted_dates, key=week_key):
            dates_in_week = list(group)
            weekly_dates.add(dates_in_week[-1])

        return weekly_dates

    @staticmethod
    def _get_current_holding(
        positions: dict[str, Any]
    ) -> Optional[str]:
        """获取当前持有的股票代码。"""
        for sym, pos in positions.items():
            if hasattr(pos, "total_quantity") and pos.total_quantity > 0:
                return sym
            elif isinstance(pos, dict) and pos.get("total_quantity", 0) > 0:
                return sym
        return None

    @staticmethod
    def _get_sellable_quantity(
        positions: dict[str, Any], symbol: str
    ) -> int:
        """获取可卖数量。"""
        pos = positions.get(symbol)
        if pos is None:
            return 0
        if hasattr(pos, "sellable_quantity"):
            return pos.sellable_quantity
        if isinstance(pos, dict):
            return pos.get("sellable_quantity", 0)
        return 0

    def _calc_buy_quantity(self, cash: float, close_raw: float) -> int:
        """计算买入数量（手数整数倍，留费用缓冲）。"""
        if close_raw <= 0 or cash <= 0:
            return 0
        effective_price = close_raw * _COST_BUFFER
        lots = int(cash // (effective_price * self._lot_size))
        return max(0, lots * self._lot_size)


# ---------------------------------------------------------------------- #
# 激进轨策略
# ---------------------------------------------------------------------- #


class AggressiveStrategy(Strategy):
    """激进轨策略：日频信号，突破+放量+相对强度入场。

    调仓规则：
    - 每个交易日收盘计算，下一交易日开盘成交。
    - 最多持有一只股票，仅用于模拟研究。
    - 入场必须同时满足 20 日突破、放量和相对强度条件。
    - 换仓信号顺序固定为先 SELL 后 BUY。

    入场条件：
    - close_qfq > 前 breakout_window 个交易日最高收盘价（窗口不含信号日）。
    - 当日成交量 / 前 volume_window 日平均成交量 >= volume_ratio（均值不含信号日）。
    - 个股 relative_strength_window 日收益减沪深 300 同期收益 > 0。
    - 多个候选按 突破幅度+相对强度+log(量比) 的横截面 z-score 合计排序。

    退出条件（任一满足）：
    - 收盘价低于前 exit_low_window 日最低收盘价（窗口不含信号日）。
    - 持有满 max_holding_days 个交易日。
    - 标的不再满足历史股票池资格。

    Args:
        params: 激进轨参数。
        universe_filter: 历史时点股票池过滤器。
        trading_dates: 交易日列表。
        lot_size: 一手股数，默认 100。
        benchmark_hs300: 沪深300收盘价字典 {date: float}。
    """

    def __init__(
        self,
        params: AggressiveParams,
        universe_filter: HistoricalUniverseFilter,
        trading_dates: list[date],
        lot_size: int = 100,
        benchmark_hs300: Optional[dict[date, float]] = None,
    ) -> None:
        self._params = params
        self._universe_filter = universe_filter
        self._trading_dates = sorted(set(trading_dates))
        self._trading_dates_index = {
            d: i for i, d in enumerate(self._trading_dates)
        }
        self._lot_size = lot_size
        self._benchmark_hs300 = benchmark_hs300 or {}
        self._entry_date: Optional[date] = None
        self._holding_symbol: Optional[str] = None
        self._holding_days = 0

    def on_close(self, context: StrategyContext) -> list[Signal]:
        """收盘后生成交易信号。"""
        dt = context.current_date
        bars = context.bars_up_to_date
        if bars is None or len(bars) == 0:
            return []

        bars_up_to = bars[bars["trade_date"] <= dt] if "trade_date" in bars.columns else bars
        if len(bars_up_to) == 0:
            return []

        signals: list[Signal] = []

        # 同步内部持仓状态
        current_holding = self._get_current_holding(context.positions)
        if current_holding is None:
            self._holding_symbol = None
            self._entry_date = None
            self._holding_days = 0
        elif current_holding != self._holding_symbol:
            # 引擎状态与内部状态不同步（可能是外部买入），重置
            self._holding_symbol = current_holding
            self._entry_date = dt
            self._holding_days = 0

        # 1. 检查退出条件
        should_exit = False
        exit_reason = ""

        if current_holding is not None:
            self._holding_days += 1

            # 退出条件 1: 持有满 max_holding_days
            if self._holding_days >= self._params.max_holding_days:
                should_exit = True
                exit_reason = f"持有满{self._params.max_holding_days}日"

            # 退出条件 2: 收盘价低于前 exit_low_window 日最低收盘价
            if not should_exit:
                sym_data = bars_up_to[bars_up_to["symbol"] == current_holding].sort_values("trade_date")
                if len(sym_data) > 0:
                    close_qfq = sym_data["close_qfq"].astype(float)
                    prev_low = close_qfq.shift(1).rolling(
                        window=self._params.exit_low_window,
                        min_periods=self._params.exit_low_window,
                    ).min()
                    if len(prev_low) > 0 and not pd.isna(prev_low.iloc[-1]):
                        if close_qfq.iloc[-1] < prev_low.iloc[-1]:
                            should_exit = True
                            exit_reason = f"收盘{close_qfq.iloc[-1]:.2f} < 前{self._params.exit_low_window}日最低{prev_low.iloc[-1]:.2f}"

            # 退出条件 3: 不再满足股票池资格
            if not should_exit:
                decision = self._universe_filter.is_eligible(current_holding, dt, context)
                if not decision.eligible:
                    should_exit = True
                    exit_reason = f"不再满足股票池: {decision.reason}"

            if should_exit:
                sell_qty = self._get_sellable_quantity(context.positions, current_holding)
                if sell_qty > 0:
                    signals.append(Signal(
                        signal_date=dt,
                        symbol=current_holding,
                        side=Side.SELL,
                        quantity=sell_qty,
                        reason=f"激进轨退出: {exit_reason}",
                    ))
                # 重置持仓状态
                self._holding_symbol = None
                self._entry_date = None
                self._holding_days = 0
                current_holding = None

        # 2. 检查入场条件（仅当无持仓时）
        if current_holding is None:
            entry_signal = self._check_entry(dt, bars_up_to, context)
            if entry_signal is not None:
                signals.append(entry_signal)

        return signals

    # ------------------------------------------------------------------ #
    # 入场逻辑
    # ------------------------------------------------------------------ #

    def _check_entry(
        self,
        dt: date,
        bars: pd.DataFrame,
        context: StrategyContext,
    ) -> Optional[Signal]:
        """检查入场条件并返回买入信号。"""
        symbols = sorted(bars["symbol"].unique().tolist())

        # 股票池过滤
        eligible: list[str] = []
        for sym in symbols:
            decision = self._universe_filter.is_eligible(sym, dt, context)
            if decision.eligible:
                eligible.append(sym)

        if len(eligible) < 5:
            return None

        # 计算入场特征
        candidates: list[dict[str, Any]] = []

        for sym in eligible:
            sym_data = bars[bars["symbol"] == sym].sort_values("trade_date")
            if len(sym_data) == 0:
                continue

            close_qfq = sym_data["close_qfq"].astype(float)
            volume = sym_data["volume"].astype(float)
            close_raw = _safe_float(sym_data.iloc[-1]["close_raw"])

            # 突破: close_qfq > 前 breakout_window 日最高收盘价
            breakout_signal = compute_breakout(close_qfq, self._params.breakout_window)
            if len(breakout_signal) == 0 or not breakout_signal.iloc[-1]:
                continue

            # 突破幅度
            prev_max = close_qfq.shift(1).rolling(
                window=self._params.breakout_window,
                min_periods=self._params.breakout_window,
            ).max()
            if pd.isna(prev_max.iloc[-1]) or prev_max.iloc[-1] <= 0:
                continue
            breakout_magnitude = float(close_qfq.iloc[-1] / prev_max.iloc[-1] - 1.0)

            # 量比: 当日成交量 / 前 volume_window 日平均成交量
            vol_ratio = compute_volume_ratio(volume, self._params.volume_window)
            if pd.isna(vol_ratio.iloc[-1]):
                continue
            vr = float(vol_ratio.iloc[-1])
            if vr < self._params.volume_ratio:
                continue

            # 相对强度: 个股 RS 日收益 - 沪深300 同期收益
            # 显式传入 trade_dates，不依赖 DataFrame/Series 索引保存交易日期
            rs = self._compute_relative_strength(
                close_qfq, sym_data["trade_date"], dt
            )
            if rs is None:
                continue
            if rs <= 0:
                continue

            candidates.append({
                "symbol": sym,
                "breakout_magnitude": breakout_magnitude,
                "relative_strength": rs,
                "volume_ratio": vr,
                "log_volume_ratio": math.log(max(vr, 1e-10)),
                "close_raw": close_raw,
            })

        if len(candidates) < 5:
            # 有效候选不足 5 个，z-score 不可靠
            if len(candidates) > 0:
                # 仍然可以选最优，但不做 z-score
                best = max(candidates, key=lambda c: c["breakout_magnitude"] + c["relative_strength"] + c["log_volume_ratio"])
                return self._make_buy_signal(dt, best, bars, context)
            return None

        # 构建横截面 Series
        syms = [c["symbol"] for c in candidates]
        breakout_series = pd.Series([c["breakout_magnitude"] for c in candidates], index=syms)
        rs_series = pd.Series([c["relative_strength"] for c in candidates], index=syms)
        log_vr_series = pd.Series([c["log_volume_ratio"] for c in candidates], index=syms)

        # z-score 合计排序
        z_breakout = zscore_cross_sectional(breakout_series)
        z_rs = zscore_cross_sectional(rs_series)
        log_vr = zscore_cross_sectional(log_vr_series)

        scores = z_breakout + z_rs + log_vr
        valid_scores = scores.dropna()

        if len(valid_scores) == 0:
            # z-score 全部 NaN，用原始值排序
            best = max(candidates, key=lambda c: c["breakout_magnitude"] + c["relative_strength"] + c["log_volume_ratio"])
        else:
            best_sym = valid_scores.idxmax()
            best = next(c for c in candidates if c["symbol"] == best_sym)

        return self._make_buy_signal(dt, best, bars, context)

    def _make_buy_signal(
        self,
        dt: date,
        candidate: dict[str, Any],
        bars: pd.DataFrame,
        context: StrategyContext,
    ) -> Optional[Signal]:
        """生成买入信号。"""
        cash = float(context.portfolio.cash)
        close_raw = candidate["close_raw"]
        buy_qty = self._calc_buy_quantity(cash, close_raw)

        if buy_qty <= 0:
            return None

        sym = candidate["symbol"]
        # 更新内部持仓状态
        self._holding_symbol = sym
        self._entry_date = dt
        self._holding_days = 0

        return Signal(
            signal_date=dt,
            symbol=sym,
            side=Side.BUY,
            quantity=buy_qty,
            reason=(
                f"激进轨入场: 突破{candidate['breakout_magnitude']:.4f}, "
                f"量比{candidate['volume_ratio']:.2f}, "
                f"相对强度{candidate['relative_strength']:.4f}"
            ),
        )

    def _compute_relative_strength(
        self,
        stock_close: pd.Series,
        trade_dates: pd.Series,
        dt: date,
    ) -> Optional[float]:
        """计算个股相对沪深300的相对强度。

        显式从 ``trade_dates`` 取得相对强度起止日期，
        不依赖 DataFrame/Series 索引保存交易日期。
        """
        if not self._benchmark_hs300:
            return None

        window = self._params.relative_strength_window
        if len(stock_close) < window + 1:
            return None
        if len(trade_dates) < window + 1:
            return None

        # 个股收益
        stock_return = float(stock_close.iloc[-1] / stock_close.iloc[-window - 1] - 1.0)

        # 显式从 trade_dates 取得起止日期（不使用 stock_close.index）
        end_date = _to_date(trade_dates.iloc[-1])
        start_date = _to_date(trade_dates.iloc[-window - 1])

        if end_date is None or start_date is None:
            return None

        bench_end = self._benchmark_hs300.get(end_date)
        bench_start = self._benchmark_hs300.get(start_date)

        if bench_end is None or bench_start is None or bench_start <= 0:
            # 尝试找最近的基准日期
            bench_end = self._find_nearest_benchmark(end_date, before=False)
            bench_start = self._find_nearest_benchmark(start_date, before=True)
            if bench_end is None or bench_start is None or bench_start <= 0:
                return None

        bench_return = float(bench_end / bench_start - 1.0)
        return stock_return - bench_return

    def _find_nearest_benchmark(
        self, target: date, before: bool = True
    ) -> Optional[float]:
        """在基准数据中查找最接近目标日期的价格。"""
        if not self._benchmark_hs300:
            return None
        sorted_dates = sorted(self._benchmark_hs300.keys())
        if before:
            candidates = [d for d in sorted_dates if d <= target]
        else:
            candidates = [d for d in sorted_dates if d >= target]
        if not candidates:
            return None
        nearest = candidates[-1] if before else candidates[0]
        return self._benchmark_hs300[nearest]

    # ------------------------------------------------------------------ #
    # 辅助方法
    # ------------------------------------------------------------------ #

    @staticmethod
    def _get_current_holding(positions: dict[str, Any]) -> Optional[str]:
        """获取当前持有的股票代码。"""
        for sym, pos in positions.items():
            if hasattr(pos, "total_quantity") and pos.total_quantity > 0:
                return sym
            elif isinstance(pos, dict) and pos.get("total_quantity", 0) > 0:
                return sym
        return None

    @staticmethod
    def _get_sellable_quantity(positions: dict[str, Any], symbol: str) -> int:
        """获取可卖数量。"""
        pos = positions.get(symbol)
        if pos is None:
            return 0
        if hasattr(pos, "sellable_quantity"):
            return pos.sellable_quantity
        if isinstance(pos, dict):
            return pos.get("sellable_quantity", 0)
        return 0

    def _calc_buy_quantity(self, cash: float, close_raw: float) -> int:
        """计算买入数量。"""
        if close_raw <= 0 or cash <= 0:
            return 0
        effective_price = close_raw * _COST_BUFFER
        lots = int(cash // (effective_price * self._lot_size))
        return max(0, lots * self._lot_size)


def generate_steady_param_combinations() -> list[SteadyParams]:
    """生成稳健轨全部参数候选组合。"""
    combos: list[SteadyParams] = []
    for tw in STEADY_PARAM_CANDIDATES["trend_window"]:
        for mw in STEADY_PARAM_CANDIDATES["momentum_window"]:
            for vw in STEADY_PARAM_CANDIDATES["volatility_window"]:
                for ms in STEADY_PARAM_CANDIDATES["minimum_score"]:
                    combos.append(SteadyParams(
                        trend_window=tw,
                        momentum_window=mw,
                        volatility_window=vw,
                        minimum_score=ms,
                    ))
    return combos


def generate_aggressive_param_combinations() -> list[AggressiveParams]:
    """生成激进轨全部参数候选组合。"""
    combos: list[AggressiveParams] = []
    for bw in AGGRESSIVE_PARAM_CANDIDATES["breakout_window"]:
        for vw in AGGRESSIVE_PARAM_CANDIDATES["volume_window"]:
            for vr in AGGRESSIVE_PARAM_CANDIDATES["volume_ratio"]:
                for rsw in AGGRESSIVE_PARAM_CANDIDATES["relative_strength_window"]:
                    for elw in AGGRESSIVE_PARAM_CANDIDATES["exit_low_window"]:
                        for mhd in AGGRESSIVE_PARAM_CANDIDATES["max_holding_days"]:
                            combos.append(AggressiveParams(
                                breakout_window=bw,
                                volume_window=vw,
                                volume_ratio=vr,
                                relative_strength_window=rsw,
                                exit_low_window=elw,
                                max_holding_days=mhd,
                            ))
    return combos
