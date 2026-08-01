"""Phase 3 特征计算模块。

所有特征仅使用截至信号日收盘的数据，禁止使用未来数据。
滚动窗口在历史不足时返回 NaN，不允许部分窗口计算。
突破和均量窗口不包含信号日。

设计原则：
- 前复权 OHLCV 用于信号和特征计算，未复权数据用于成交。
- 滚动窗口使用 ``min_periods=window`` 确保不产生部分窗口结果。
- 突破和量比的回看窗口使用 ``shift(1)`` 排除信号日。
- 横截面 z-score 仅使用同一信号日通过股票池过滤的股票。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

__all__ = [
    "compute_moving_average",
    "compute_momentum",
    "compute_volatility",
    "compute_breakout",
    "compute_volume_ratio",
    "compute_relative_strength",
    "zscore_cross_sectional",
    "compute_trend_score",
    "compute_steady_score",
]

# 年化交易日数（A 股约 244 个交易日）
_TRADING_DAYS_PER_YEAR: int = 244

# 横截面 z-score 最小有效样本数
_MIN_CROSS_SECTION_SAMPLES: int = 5


def compute_moving_average(series: pd.Series, window: int) -> pd.Series:
    """计算简单移动平均（SMA）。

    使用 ``min_periods=window`` 确保历史不足时不产生部分窗口结果，
    前 ``window - 1`` 个值为 NaN。

    Args:
        series: 输入序列（通常为前复权收盘价）。
        window: 滚动窗口大小，必须为正整数。

    Returns:
        与输入同索引的移动平均序列，前 ``window - 1`` 个值为 NaN。

    Raises:
        ValueError: ``window`` 非正时抛出。
    """
    if window <= 0:
        raise ValueError(f"window 必须为正整数，得到 {window}")
    return series.rolling(window=window, min_periods=window).mean()


def compute_momentum(close: pd.Series, window: int) -> pd.Series:
    """计算动量：``close / close.shift(window) - 1``。

    前 ``window`` 个值为 NaN（``shift`` 产生），不使用未来数据。

    Args:
        close: 收盘价序列。
        window: 回看窗口，必须为正整数。

    Returns:
        动量序列（比率形式，如 0.05 表示上涨 5%）。

    Raises:
        ValueError: ``window`` 非正时抛出。
    """
    if window <= 0:
        raise ValueError(f"window 必须为正整数，得到 {window}")
    return close / close.shift(window) - 1.0


def compute_volatility(returns: pd.Series, window: int) -> pd.Series:
    """计算年化波动率：滚动日收益标准差 * sqrt(244)。

    使用 ``min_periods=window`` 确保历史不足时返回 NaN。

    Args:
        returns: 日收益率序列（如 ``close.pct_change()``）。
        window: 滚动窗口大小，必须为正整数。

    Returns:
        年化波动率序列，历史不足时为 NaN。

    Raises:
        ValueError: ``window`` 非正时抛出。
    """
    if window <= 0:
        raise ValueError(f"window 必须为正整数，得到 {window}")
    rolling_std = returns.rolling(window=window, min_periods=window).std()
    return rolling_std * math.sqrt(_TRADING_DAYS_PER_YEAR)


def compute_breakout(close: pd.Series, window: int) -> pd.Series:
    """计算突破信号：当日收盘价 > 前 ``window`` 个交易日最高收盘价。

    窗口不包含信号日：使用 ``shift(1)`` 将序列后移一日，
    再取 ``window`` 期滚动最大值，确保信号日收盘价不参与比较。

    计算方式::

        prev_max = close.shift(1).rolling(window, min_periods=window).max()
        breakout = close > prev_max

    历史不足时 ``prev_max`` 为 NaN，``close > NaN`` 结果为 False，
    表示无法确认突破。

    Args:
        close: 收盘价序列。
        window: 回看窗口（不包含信号日），必须为正整数。

    Returns:
        布尔序列，``True`` 表示突破。历史不足时为 ``False``。

    Raises:
        ValueError: ``window`` 非正时抛出。
    """
    if window <= 0:
        raise ValueError(f"window 必须为正整数，得到 {window}")
    prev_max = close.shift(1).rolling(window=window, min_periods=window).max()
    return close > prev_max


def compute_volume_ratio(volume: pd.Series, window: int) -> pd.Series:
    """计算量比：当日成交量 / 前 ``window`` 个交易日平均成交量。

    均值窗口不包含信号日：使用 ``shift(1)`` 将序列后移一日，
    再取 ``window`` 期滚动均值。

    计算方式::

        prev_mean = volume.shift(1).rolling(window, min_periods=window).mean()
        ratio = volume / prev_mean

    历史不足时 ``prev_mean`` 为 NaN，结果为 NaN。

    Args:
        volume: 成交量序列。
        window: 均值窗口（不包含信号日），必须为正整数。

    Returns:
        量比序列。历史不足时为 NaN。

    Raises:
        ValueError: ``window`` 非正时抛出。
    """
    if window <= 0:
        raise ValueError(f"window 必须为正整数，得到 {window}")
    prev_mean = volume.shift(1).rolling(window=window, min_periods=window).mean()
    return volume / prev_mean


def compute_relative_strength(
    stock_close: pd.Series,
    benchmark_close: pd.Series,
    window: int,
) -> pd.Series:
    """计算相对强度：个股 ``window`` 日收益 - 基准同期收益。

    两个序列按索引内连接（``join="inner"``）对齐后再计算，避免错位。
    前导 ``window`` 个值为 NaN。

    Args:
        stock_close: 个股收盘价序列。
        benchmark_close: 基准收盘价序列。
        window: 收益计算窗口，必须为正整数。

    Returns:
        相对强度序列（个股超额收益）。

    Raises:
        ValueError: ``window`` 非正时抛出。
    """
    if window <= 0:
        raise ValueError(f"window 必须为正整数，得到 {window}")
    stock_aligned, bench_aligned = stock_close.align(
        benchmark_close, join="inner"
    )
    stock_return = stock_aligned / stock_aligned.shift(window) - 1.0
    bench_return = bench_aligned / bench_aligned.shift(window) - 1.0
    return stock_return - bench_return


def zscore_cross_sectional(values: pd.Series) -> pd.Series:
    """横截面 z-score 标准化。

    对同一信号日多只股票的某特征值做标准化。有效值（非 NaN）少于
    :data:`_MIN_CROSS_SECTION_SAMPLES`（5）个时返回全 NaN，
    防止小样本产生不稳定标准化。

    使用总体标准差（``ddof=0``）。标准差为 0 时返回全 NaN。

    Args:
        values: 横截面数值序列（同一信号日多只股票的某特征值）。

    Returns:
        z-score 序列，均值为 0、标准差为 1。有效值不足 5 个时全为 NaN。
    """
    valid = values.dropna()
    if len(valid) < _MIN_CROSS_SECTION_SAMPLES:
        return pd.Series(np.nan, index=values.index, dtype=float)
    mean = float(valid.mean())
    std = float(valid.std(ddof=0))
    if not (std > 0):
        return pd.Series(np.nan, index=values.index, dtype=float)
    return (values - mean) / std


def compute_trend_score(close: pd.Series, ma_window: int) -> pd.Series:
    """计算趋势得分：``close / MA(ma_window) - 1``。

    MA 历史不足时对应位置为 NaN。

    Args:
        close: 收盘价序列。
        ma_window: 移动平均窗口，必须为正整数。

    Returns:
        趋势得分序列（比率形式，如 0.05 表示收盘价高于均线 5%）。

    Raises:
        ValueError: ``ma_window`` 非正时抛出。
    """
    if ma_window <= 0:
        raise ValueError(f"ma_window 必须为正整数，得到 {ma_window}")
    ma = compute_moving_average(close, ma_window)
    return close / ma - 1.0


def compute_steady_score(
    trend: pd.Series,
    momentum: pd.Series,
    volatility: pd.Series,
) -> pd.Series:
    """计算稳健轨综合得分。

    得分 = ``zscore(trend) + zscore(momentum) - zscore(volatility)``

    三个特征分别做横截面 z-score 后线性组合：
    趋势和动量正向贡献，波动率负向贡献。

    任一特征的 z-score 为 NaN 时（如有效样本不足 5 个或标准差为 0），
    对应股票的得分为 NaN。

    Args:
        trend: 趋势得分（横截面，同一信号日多只股票）。
        momentum: 动量（横截面）。
        volatility: 年化波动率（横截面）。

    Returns:
        稳健轨综合得分序列。
    """
    z_trend = zscore_cross_sectional(trend)
    z_momentum = zscore_cross_sectional(momentum)
    z_volatility = zscore_cross_sectional(volatility)
    return z_trend + z_momentum - z_volatility
