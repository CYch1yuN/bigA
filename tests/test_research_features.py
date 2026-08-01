"""Phase 3 研究特征模块 (ashare_quant.research.features) 的综合测试。

覆盖范围：
1. 移动平均 (compute_moving_average) —— 手算小样本、min_periods=window 行为
2. 动量 (compute_momentum) —— 手算小样本
3. 波动率 (compute_volatility) —— 年化因子 sqrt(244) 验证
4. 突破 (compute_breakout) —— 窗口排除信号日验证
5. 量比 (compute_volume_ratio) —— 均值排除信号日验证
6. 相对强度 (compute_relative_strength) —— 手算小样本、索引内连接对齐
7. 横截面 z-score (zscore_cross_sectional) —— 样本数阈值 (<5 全 NaN)、总体标准差 (ddof=0)
8. 趋势得分 (compute_trend_score) —— 手算小样本
9. 稳健轨得分 (compute_steady_score) —— z(trend)+z(momentum)-z(volatility) 公式验证
10. 无未来数据泄漏 —— 修改未来值不影响历史值
11. window <= 0 抛出 ValueError

所有手算期望值均经过独立推导，与 pandas 实现行为一致。
浮动比较使用 pytest.approx；布尔比较直接断言。
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from ashare_quant.research.features import (
    compute_breakout,
    compute_momentum,
    compute_moving_average,
    compute_relative_strength,
    compute_steady_score,
    compute_trend_score,
    compute_volatility,
    compute_volume_ratio,
    zscore_cross_sectional,
)


# ====================================================================== #
# 辅助函数
# ====================================================================== #


def _is_nan(value) -> bool:
    """判断单个标量是否为 NaN（兼容 float('nan') / np.nan / None）。"""
    return bool(pd.isna(value))


# ====================================================================== #
# compute_moving_average
# ====================================================================== #


class TestComputeMovingAverage:
    """移动平均 (SMA) 测试。

    实现: series.rolling(window=window, min_periods=window).mean()
    前 window-1 个值为 NaN，不产生部分窗口结果。
    """

    def test_hand_computed_small_sample(self):
        """手算验证：series=[1,2,3,4,5], window=3。

        rolling(3, min_periods=3).mean():
        - index 0: NaN（仅 1 个值，不足 3）
        - index 1: NaN（仅 2 个值，不足 3）
        - index 2: (1+2+3)/3 = 2.0
        - index 3: (2+3+4)/3 = 3.0
        - index 4: (3+4+5)/3 = 4.0
        """
        series = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        result = compute_moving_average(series, window=3)
        assert _is_nan(result.iloc[0])
        assert _is_nan(result.iloc[1])
        assert result.iloc[2] == pytest.approx(2.0)
        assert result.iloc[3] == pytest.approx(3.0)
        assert result.iloc[4] == pytest.approx(4.0)

    def test_min_periods_equals_window(self):
        """min_periods=window：前 window-1 个值必须为 NaN。"""
        series = pd.Series([10.0, 20.0, 30.0])
        result = compute_moving_average(series, window=3)
        assert _is_nan(result.iloc[0])
        assert _is_nan(result.iloc[1])
        # index 2: (10+20+30)/3 = 20.0
        assert result.iloc[2] == pytest.approx(20.0)

    def test_window_one_equals_original(self):
        """window=1 时 MA 等于原序列。"""
        series = pd.Series([1.0, 2.0, 3.0])
        result = compute_moving_average(series, window=1)
        assert result.iloc[0] == pytest.approx(1.0)
        assert result.iloc[1] == pytest.approx(2.0)
        assert result.iloc[2] == pytest.approx(3.0)

    def test_preserves_index(self):
        """结果索引与输入一致。"""
        idx = pd.date_range("2024-01-01", periods=5, freq="D")
        series = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0], index=idx)
        result = compute_moving_average(series, window=2)
        assert result.index.equals(idx)

    def test_no_future_data(self):
        """修改最后一个值不影响前 n-1 个 MA 值（因果性）。"""
        series = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        result_a = compute_moving_average(series, window=3)

        series_modified = series.copy()
        series_modified.iloc[-1] = 999.0
        result_b = compute_moving_average(series_modified, window=3)

        # 前 4 个值不应改变（MA[4] 才用到 index 4）
        pd.testing.assert_series_equal(
            result_a.iloc[:4], result_b.iloc[:4], check_names=False
        )
        # 最后一个值应改变
        assert result_b.iloc[4] != pytest.approx(result_a.iloc[4])

    def test_invalid_window_zero(self):
        """window=0 抛出 ValueError。"""
        with pytest.raises(ValueError):
            compute_moving_average(pd.Series([1.0]), window=0)

    def test_invalid_window_negative(self):
        """window=-1 抛出 ValueError。"""
        with pytest.raises(ValueError):
            compute_moving_average(pd.Series([1.0]), window=-1)


# ====================================================================== #
# compute_momentum
# ====================================================================== #


class TestComputeMomentum:
    """动量测试。

    实现: close / close.shift(window) - 1
    前 window 个值为 NaN（shift 产生），不使用未来数据。
    """

    def test_hand_computed_small_sample(self):
        """手算验证：close=[10, 11, 12], window=1。

        momentum = close / close.shift(1) - 1:
        - index 0: 10 / NaN - 1 = NaN
        - index 1: 11 / 10 - 1 = 0.1
        - index 2: 12 / 11 - 1 = 1/11 ≈ 0.090909...
        """
        close = pd.Series([10.0, 11.0, 12.0])
        result = compute_momentum(close, window=1)
        assert _is_nan(result.iloc[0])
        assert result.iloc[1] == pytest.approx(0.1)
        assert result.iloc[2] == pytest.approx(1.0 / 11.0, rel=1e-9)

    def test_window_two(self):
        """window=2：前 2 个值为 NaN。"""
        close = pd.Series([10.0, 12.0, 15.0, 18.0])
        result = compute_momentum(close, window=2)
        assert _is_nan(result.iloc[0])
        assert _is_nan(result.iloc[1])
        # 15 / 10 - 1 = 0.5
        assert result.iloc[2] == pytest.approx(0.5)
        # 18 / 12 - 1 = 0.5
        assert result.iloc[3] == pytest.approx(0.5)

    def test_negative_momentum(self):
        """下跌时动量为负。"""
        close = pd.Series([10.0, 9.0, 8.0])
        result = compute_momentum(close, window=1)
        assert _is_nan(result.iloc[0])
        # 9/10 - 1 = -0.1
        assert result.iloc[1] == pytest.approx(-0.1)
        # 8/9 - 1 ≈ -0.1111
        assert result.iloc[2] == pytest.approx(-1.0 / 9.0, rel=1e-9)

    def test_no_future_data(self):
        """修改最后一个值不影响前 n-1 个动量值。"""
        close = pd.Series([10.0, 11.0, 12.0, 13.0, 14.0])
        result_a = compute_momentum(close, window=2)

        close_modified = close.copy()
        close_modified.iloc[-1] = 999.0
        result_b = compute_momentum(close_modified, window=2)

        pd.testing.assert_series_equal(
            result_a.iloc[:4], result_b.iloc[:4], check_names=False
        )

    def test_invalid_window_zero(self):
        """window=0 抛出 ValueError。"""
        with pytest.raises(ValueError):
            compute_momentum(pd.Series([1.0]), window=0)

    def test_invalid_window_negative(self):
        """window=-1 抛出 ValueError。"""
        with pytest.raises(ValueError):
            compute_momentum(pd.Series([1.0]), window=-1)


# ====================================================================== #
# compute_volatility
# ====================================================================== #


class TestComputeVolatility:
    """年化波动率测试。

    实现: returns.rolling(window, min_periods=window).std() * sqrt(244)
    使用样本标准差 (ddof=1，pandas rolling.std 默认) 年化至 244 个交易日。
    """

    def test_annualization_factor_sqrt_244(self):
        """验证年化因子 sqrt(244)。

        returns=[0.1, 0.2, 0.3], window=2:
        - rolling std (ddof=1, min_periods=2):
          - index 0: NaN
          - index 1: std([0.1, 0.2])
                    = sqrt(((0.1-0.15)^2 + (0.2-0.15)^2) / (2-1))
                    = sqrt((0.0025 + 0.0025) / 1) = sqrt(0.005)
          - index 2: std([0.2, 0.3]) = sqrt(0.005)  (同理)
        - 年化: sqrt(0.005) * sqrt(244) = sqrt(0.005 * 244) = sqrt(1.22)
        """
        returns = pd.Series([0.1, 0.2, 0.3])
        result = compute_volatility(returns, window=2)
        assert _is_nan(result.iloc[0])

        # sqrt(0.005) * sqrt(244) = sqrt(0.005 * 244) = sqrt(1.22)
        expected = math.sqrt(1.22)
        assert result.iloc[1] == pytest.approx(expected, rel=1e-9)
        assert result.iloc[2] == pytest.approx(expected, rel=1e-9)

    def test_annualization_factor_explicit(self):
        """显式验证：波动率 = rolling_std * sqrt(244)，而非 rolling_std * sqrt(252)。"""
        returns = pd.Series([0.01, -0.01, 0.02, -0.02, 0.01])
        window = 3
        result = compute_volatility(returns, window=window)

        # 手动计算 rolling std（pandas 默认 ddof=1）
        rolling_std = returns.rolling(window=window, min_periods=window).std()
        expected = rolling_std * math.sqrt(244)

        # 验证与 sqrt(244) 年化一致
        for i in range(len(returns)):
            if _is_nan(rolling_std.iloc[i]):
                assert _is_nan(result.iloc[i])
            else:
                assert result.iloc[i] == pytest.approx(expected.iloc[i], rel=1e-9)

        # 验证不等于 sqrt(252) 年化（排除美股惯例）
        wrong_expected = rolling_std * math.sqrt(252)
        last_idx = len(returns) - 1
        assert result.iloc[last_idx] != pytest.approx(wrong_expected.iloc[last_idx], rel=1e-3)

    def test_min_periods_equals_window(self):
        """min_periods=window：前 window-1 个值为 NaN。"""
        returns = pd.Series([0.01, 0.02, 0.03])
        result = compute_volatility(returns, window=3)
        assert _is_nan(result.iloc[0])
        assert _is_nan(result.iloc[1])
        # index 2 有值
        assert not _is_nan(result.iloc[2])

    def test_no_future_data(self):
        """修改最后一个收益率不影响前 n-1 个波动率值。"""
        returns = pd.Series([0.01, 0.02, 0.03, 0.04, 0.05])
        result_a = compute_volatility(returns, window=3)

        returns_modified = returns.copy()
        returns_modified.iloc[-1] = 0.99
        result_b = compute_volatility(returns_modified, window=3)

        pd.testing.assert_series_equal(
            result_a.iloc[:4], result_b.iloc[:4], check_names=False
        )

    def test_invalid_window_zero(self):
        """window=0 抛出 ValueError。"""
        with pytest.raises(ValueError):
            compute_volatility(pd.Series([0.01]), window=0)

    def test_invalid_window_negative(self):
        """window=-1 抛出 ValueError。"""
        with pytest.raises(ValueError):
            compute_volatility(pd.Series([0.01]), window=-1)


# ====================================================================== #
# compute_breakout
# ====================================================================== #


class TestComputeBreakout:
    """突破信号测试。

    实现: close > close.shift(1).rolling(window, min_periods=window).max()
    窗口不包含信号日（shift(1) 排除当日），历史不足时为 False。
    """

    def test_hand_computed_small_sample(self):
        """手算验证：close=[1,2,3,4,5], window=2。

        prev_max = close.shift(1).rolling(2, min_periods=2).max():
        - close.shift(1) = [NaN, 1, 2, 3, 4]
        - rolling(2).max():
          - index 0: NaN
          - index 1: NaN（仅 1 个非 NaN 值，不足 2）
          - index 2: max(1, 2) = 2
          - index 3: max(2, 3) = 3
          - index 4: max(3, 4) = 4
        - breakout = close > prev_max:
          - index 0: 1 > NaN → False
          - index 1: 2 > NaN → False
          - index 2: 3 > 2 → True  ← 第 3 天突破前 2 天最高价
          - index 3: 4 > 3 → True
          - index 4: 5 > 4 → True
        """
        close = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        result = compute_breakout(close, window=2)
        assert not result.iloc[0]
        assert not result.iloc[1]
        assert result.iloc[2]
        assert result.iloc[3]
        assert result.iloc[4]

    def test_signal_day_excluded_from_window(self):
        """信号日收盘价不参与窗口最大值计算。

        close=[1, 2, 3], window=2:
        - 正确（排除信号日）: prev_max = max(close[0], close[1]) = max(1, 2) = 2
          breakout = 3 > 2 = True
        - 若错误包含信号日: prev_max = max(1, 2, 3) = 3, 3 > 3 = False

        结果为 True 证明信号日被排除。
        """
        close = pd.Series([1.0, 2.0, 3.0])
        result = compute_breakout(close, window=2)
        assert result.iloc[2]

    def test_no_breakout_when_not_exceeding(self):
        """未超过前 window 日最高价时不触发突破。"""
        # 持续下跌，无突破
        close = pd.Series([5.0, 4.0, 3.0, 2.0, 1.0])
        result = compute_breakout(close, window=2)
        assert not result.any()

    def test_no_breakout_when_equal_to_max(self):
        """收盘价等于前 window 日最高价时不触发突破（严格大于）。"""
        # close=[1, 3, 3], window=2
        # prev_max[2] = max(1, 3) = 3, 3 > 3 = False
        close = pd.Series([1.0, 3.0, 3.0])
        result = compute_breakout(close, window=2)
        assert not result.iloc[2]

    def test_insufficient_history_returns_false(self):
        """历史不足时 prev_max 为 NaN，比较结果为 False。"""
        close = pd.Series([1.0, 2.0, 3.0])
        result = compute_breakout(close, window=5)
        # 窗口 5 > 数据长度 3，全部 False
        assert not result.any()

    def test_result_is_bool_series(self):
        """结果为布尔类型 Series（NaN 比较产生 False 而非 NaN）。"""
        close = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        result = compute_breakout(close, window=2)
        assert result.dtype == bool

    def test_no_future_data(self):
        """修改最后一个值不影响前 n-1 个突破信号。"""
        close = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        result_a = compute_breakout(close, window=2)

        close_modified = close.copy()
        close_modified.iloc[-1] = 0.5  # 从 5 改为 0.5
        result_b = compute_breakout(close_modified, window=2)

        pd.testing.assert_series_equal(
            result_a.iloc[:4], result_b.iloc[:4], check_names=False
        )
        # 最后一个信号应改变：5>4=True vs 0.5>4=False
        assert result_a.iloc[4] != result_b.iloc[4]

    def test_invalid_window_zero(self):
        """window=0 抛出 ValueError。"""
        with pytest.raises(ValueError):
            compute_breakout(pd.Series([1.0]), window=0)

    def test_invalid_window_negative(self):
        """window=-1 抛出 ValueError。"""
        with pytest.raises(ValueError):
            compute_breakout(pd.Series([1.0]), window=-1)


# ====================================================================== #
# compute_volume_ratio
# ====================================================================== #


class TestComputeVolumeRatio:
    """量比测试。

    实现: volume / volume.shift(1).rolling(window, min_periods=window).mean()
    均值窗口不包含信号日（shift(1) 排除当日），历史不足时为 NaN。
    """

    def test_hand_computed_small_sample(self):
        """手算验证：volume=[100, 200, 300, 400, 500], window=2。

        prev_mean = volume.shift(1).rolling(2, min_periods=2).mean():
        - volume.shift(1) = [NaN, 100, 200, 300, 400]
        - rolling(2).mean():
          - index 0: NaN
          - index 1: NaN（仅 1 个非 NaN 值）
          - index 2: mean(100, 200) = 150
          - index 3: mean(200, 300) = 250
          - index 4: mean(300, 400) = 350
        - ratio = volume / prev_mean:
          - index 0: NaN
          - index 1: NaN
          - index 2: 300 / 150 = 2.0
          - index 3: 400 / 250 = 1.6
          - index 4: 500 / 350 ≈ 1.428571
        """
        volume = pd.Series([100.0, 200.0, 300.0, 400.0, 500.0])
        result = compute_volume_ratio(volume, window=2)
        assert _is_nan(result.iloc[0])
        assert _is_nan(result.iloc[1])
        assert result.iloc[2] == pytest.approx(2.0)
        assert result.iloc[3] == pytest.approx(1.6)
        assert result.iloc[4] == pytest.approx(500.0 / 350.0, rel=1e-9)

    def test_signal_day_excluded_from_mean(self):
        """信号日成交量不参与均值计算。

        volume=[100, 200, 300], window=2:
        - 正确（排除信号日）: prev_mean = mean(100, 200) = 150
          ratio = 300 / 150 = 2.0
        - 若错误包含信号日: prev_mean = mean(200, 300) = 250
          ratio = 300 / 250 = 1.2

        2.0 ≠ 1.2 证明信号日被排除。
        """
        volume = pd.Series([100.0, 200.0, 300.0])
        result = compute_volume_ratio(volume, window=2)
        assert result.iloc[2] == pytest.approx(2.0)
        # 确保不是错误包含信号日时的 1.2
        assert result.iloc[2] != pytest.approx(1.2)

    def test_insufficient_history_returns_nan(self):
        """历史不足时返回 NaN（与 breakout 的 False 不同）。"""
        volume = pd.Series([100.0, 200.0])
        result = compute_volume_ratio(volume, window=5)
        assert _is_nan(result.iloc[0])
        assert _is_nan(result.iloc[1])

    def test_no_future_data(self):
        """修改最后一个值不影响前 n-1 个量比值。"""
        volume = pd.Series([100.0, 200.0, 300.0, 400.0, 500.0])
        result_a = compute_volume_ratio(volume, window=2)

        volume_modified = volume.copy()
        volume_modified.iloc[-1] = 99999.0
        result_b = compute_volume_ratio(volume_modified, window=2)

        pd.testing.assert_series_equal(
            result_a.iloc[:4], result_b.iloc[:4], check_names=False
        )

    def test_invalid_window_zero(self):
        """window=0 抛出 ValueError。"""
        with pytest.raises(ValueError):
            compute_volume_ratio(pd.Series([1.0]), window=0)

    def test_invalid_window_negative(self):
        """window=-1 抛出 ValueError。"""
        with pytest.raises(ValueError):
            compute_volume_ratio(pd.Series([1.0]), window=-1)


# ====================================================================== #
# compute_relative_strength
# ====================================================================== #


class TestComputeRelativeStrength:
    """相对强度测试。

    实现: stock_return - bench_return
    其中 return = close / close.shift(window) - 1
    两序列通过 inner join 对齐，前 window 个值为 NaN。
    """

    def test_hand_computed_small_sample(self):
        """手算验证：

        stock_close  = [10, 11, 12, 13]
        benchmark    = [100, 110, 121, 133.1]  (基准每日涨 10%)
        window = 1

        stock_return = stock / stock.shift(1) - 1:
        - index 0: NaN
        - index 1: 11/10 - 1 = 0.1
        - index 2: 12/11 - 1 = 1/11 ≈ 0.090909
        - index 3: 13/12 - 1 = 1/12 ≈ 0.083333

        bench_return = bench / bench.shift(1) - 1:
        - index 0: NaN
        - index 1: 0.1
        - index 2: 0.1
        - index 3: 0.1

        relative_strength = stock_return - bench_return:
        - index 0: NaN
        - index 1: 0.1 - 0.1 = 0.0
        - index 2: 1/11 - 0.1 ≈ -0.009091
        - index 3: 1/12 - 0.1 ≈ -0.016667
        """
        stock_close = pd.Series([10.0, 11.0, 12.0, 13.0])
        benchmark_close = pd.Series([100.0, 110.0, 121.0, 133.1])
        result = compute_relative_strength(stock_close, benchmark_close, window=1)
        assert _is_nan(result.iloc[0])
        assert result.iloc[1] == pytest.approx(0.0)
        assert result.iloc[2] == pytest.approx(1.0 / 11.0 - 0.1, rel=1e-9)
        assert result.iloc[3] == pytest.approx(1.0 / 12.0 - 0.1, rel=1e-9)

    def test_zero_relative_strength_when_same_return(self):
        """个股与基准同期收益相同时相对强度为 0。"""
        stock_close = pd.Series([10.0, 11.0, 12.1])
        benchmark_close = pd.Series([100.0, 110.0, 121.0])
        result = compute_relative_strength(stock_close, benchmark_close, window=2)
        # 两者 window=2 收益均为 21%
        assert _is_nan(result.iloc[0])
        assert _is_nan(result.iloc[1])
        assert result.iloc[2] == pytest.approx(0.0, abs=1e-9)

    def test_index_alignment_inner_join(self):
        """不同索引的序列通过 inner join 对齐。"""
        idx_stock = pd.Index(["a", "b", "c", "d"])
        idx_bench = pd.Index(["b", "c", "d", "e"])
        stock_close = pd.Series([10.0, 11.0, 12.0, 13.0], index=idx_stock)
        benchmark_close = pd.Series([100.0, 110.0, 121.0, 133.1], index=idx_bench)
        result = compute_relative_strength(stock_close, benchmark_close, window=1)
        # inner join 后索引为 ["b", "c", "d"]
        assert list(result.index) == ["b", "c", "d"]

    def test_no_future_data(self):
        """修改最后一个值不影响前 n-1 个相对强度值。"""
        stock_close = pd.Series([10.0, 11.0, 12.0, 13.0, 14.0])
        benchmark_close = pd.Series([100.0, 110.0, 121.0, 133.1, 146.41])
        result_a = compute_relative_strength(stock_close, benchmark_close, window=1)

        stock_modified = stock_close.copy()
        stock_modified.iloc[-1] = 999.0
        result_b = compute_relative_strength(
            stock_modified, benchmark_close, window=1
        )
        pd.testing.assert_series_equal(
            result_a.iloc[:4], result_b.iloc[:4], check_names=False
        )

    def test_invalid_window_zero(self):
        """window=0 抛出 ValueError。"""
        with pytest.raises(ValueError):
            compute_relative_strength(
                pd.Series([1.0]), pd.Series([1.0]), window=0
            )

    def test_invalid_window_negative(self):
        """window=-1 抛出 ValueError。"""
        with pytest.raises(ValueError):
            compute_relative_strength(
                pd.Series([1.0]), pd.Series([1.0]), window=-1
            )


# ====================================================================== #
# zscore_cross_sectional
# ====================================================================== #


class TestZscoreCrossSectional:
    """横截面 z-score 测试。

    实现: (values - mean) / std(ddof=0)
    有效样本 < 5 时返回全 NaN；标准差为 0 时返回全 NaN。
    """

    def test_hand_computed_five_samples(self):
        """手算验证：values=[1,2,3,4,5]，5 个有效样本 >= 5。

        - mean = (1+2+3+4+5)/5 = 3
        - 总体方差 (ddof=0):
          var = ((1-3)^2 + (2-3)^2 + 0 + (4-3)^2 + (5-3)^2) / 5
              = (4+1+0+1+4) / 5 = 10/5 = 2
        - 总体标准差: std = sqrt(2) ≈ 1.414214
        - z-scores = (values - 3) / sqrt(2):
          = [-2/sqrt(2), -1/sqrt(2), 0, 1/sqrt(2), 2/sqrt(2)]
          = [-sqrt(2), -1/sqrt(2), 0, 1/sqrt(2), sqrt(2)]
        """
        values = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        result = zscore_cross_sectional(values)
        sqrt2 = math.sqrt(2.0)
        assert result.iloc[0] == pytest.approx(-sqrt2, rel=1e-9)
        assert result.iloc[1] == pytest.approx(-1.0 / sqrt2, rel=1e-9)
        assert result.iloc[2] == pytest.approx(0.0, abs=1e-12)
        assert result.iloc[3] == pytest.approx(1.0 / sqrt2, rel=1e-9)
        assert result.iloc[4] == pytest.approx(sqrt2, rel=1e-9)

    def test_uses_population_std_ddof_zero(self):
        """验证使用总体标准差 (ddof=0) 而非样本标准差 (ddof=1)。

        values=[1,2,3,4,5]:
        - ddof=0: std = sqrt(2) ≈ 1.414214
        - ddof=1: std = sqrt(2.5) ≈ 1.581139
        z-score[0] = (1-3) / std:
        - ddof=0: -2/1.414214 ≈ -1.414214
        - ddof=1: -2/1.581139 ≈ -1.264911
        """
        values = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        result = zscore_cross_sectional(values)
        std_ddof0 = values.std(ddof=0)
        expected = (values - values.mean()) / std_ddof0
        pd.testing.assert_series_equal(result, expected, check_names=False)

    def test_less_than_five_samples_returns_all_nan(self):
        """有效样本 < 5 时返回全 NaN。

        values=[1,2,3,4]（4 个有效样本 < 5）
        """
        values = pd.Series([1.0, 2.0, 3.0, 4.0])
        result = zscore_cross_sectional(values)
        assert result.isna().all()

    def test_four_valid_with_nan_returns_all_nan(self):
        """含 NaN 时有效样本 < 5 返回全 NaN。

        values=[1,2,3,4,NaN,NaN]（4 个有效样本 < 5）
        """
        values = pd.Series([1.0, 2.0, 3.0, 4.0, np.nan, np.nan])
        result = zscore_cross_sectional(values)
        assert result.isna().all()

    def test_five_valid_with_extra_nan(self):
        """5 个有效样本 + 额外 NaN：有效样本 >= 5，NaN 位置保持 NaN。"""
        values = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, np.nan])
        result = zscore_cross_sectional(values)
        sqrt2 = math.sqrt(2.0)
        assert result.iloc[0] == pytest.approx(-sqrt2, rel=1e-9)
        assert result.iloc[1] == pytest.approx(-1.0 / sqrt2, rel=1e-9)
        assert result.iloc[2] == pytest.approx(0.0, abs=1e-12)
        assert result.iloc[3] == pytest.approx(1.0 / sqrt2, rel=1e-9)
        assert result.iloc[4] == pytest.approx(sqrt2, rel=1e-9)
        assert _is_nan(result.iloc[5])

    def test_zero_std_returns_all_nan(self):
        """标准差为 0（所有值相同）时返回全 NaN。"""
        values = pd.Series([5.0, 5.0, 5.0, 5.0, 5.0])
        result = zscore_cross_sectional(values)
        assert result.isna().all()

    def test_preserves_index(self):
        """结果索引与输入一致。"""
        idx = pd.Index(["s1", "s2", "s3", "s4", "s5"])
        values = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0], index=idx)
        result = zscore_cross_sectional(values)
        assert result.index.equals(idx)

    def test_mean_zero_std_one_after_zscore(self):
        """z-score 后均值为 0、标准差为 1（>=5 个样本时）。"""
        values = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        result = zscore_cross_sectional(values)
        valid = result.dropna()
        assert valid.mean() == pytest.approx(0.0, abs=1e-12)
        assert valid.std(ddof=0) == pytest.approx(1.0, rel=1e-9)


# ====================================================================== #
# compute_trend_score
# ====================================================================== #


class TestComputeTrendScore:
    """趋势得分测试。

    实现: close / compute_moving_average(close, ma_window) - 1
    MA 历史不足时对应位置为 NaN。
    """

    def test_hand_computed_small_sample(self):
        """手算验证：close=[1,2,3,4,5], ma_window=3。

        MA(3) = [NaN, NaN, 2.0, 3.0, 4.0]
        trend = close / MA - 1:
        - index 0: NaN
        - index 1: NaN
        - index 2: 3/2 - 1 = 0.5
        - index 3: 4/3 - 1 ≈ 0.333333
        - index 4: 5/4 - 1 = 0.25
        """
        close = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        result = compute_trend_score(close, ma_window=3)
        assert _is_nan(result.iloc[0])
        assert _is_nan(result.iloc[1])
        assert result.iloc[2] == pytest.approx(0.5)
        assert result.iloc[3] == pytest.approx(1.0 / 3.0, rel=1e-9)
        assert result.iloc[4] == pytest.approx(0.25)

    def test_above_ma_positive_trend(self):
        """收盘价高于均线时趋势得分为正。"""
        close = pd.Series([10.0, 12.0, 15.0])
        result = compute_trend_score(close, ma_window=2)
        # MA(2) = [NaN, 11, 13.5]
        assert _is_nan(result.iloc[0])
        assert result.iloc[1] > 0
        assert result.iloc[2] > 0

    def test_below_ma_negative_trend(self):
        """收盘价低于均线时趋势得分为负。"""
        close = pd.Series([10.0, 8.0, 6.0])
        result = compute_trend_score(close, ma_window=2)
        # MA(2) = [NaN, 9, 7]
        assert _is_nan(result.iloc[0])
        assert result.iloc[1] < 0
        assert result.iloc[2] < 0

    def test_equals_close_over_ma_minus_one(self):
        """趋势得分等于 close / MA - 1。"""
        close = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        ma_window = 3
        result = compute_trend_score(close, ma_window)
        ma = compute_moving_average(close, ma_window)
        expected = close / ma - 1.0
        pd.testing.assert_series_equal(result, expected, check_names=False)

    def test_no_future_data(self):
        """修改最后一个值不影响前 n-1 个趋势得分。"""
        close = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
        result_a = compute_trend_score(close, ma_window=3)

        close_modified = close.copy()
        close_modified.iloc[-1] = 999.0
        result_b = compute_trend_score(close_modified, ma_window=3)

        pd.testing.assert_series_equal(
            result_a.iloc[:4], result_b.iloc[:4], check_names=False
        )

    def test_invalid_window_zero(self):
        """ma_window=0 抛出 ValueError。"""
        with pytest.raises(ValueError):
            compute_trend_score(pd.Series([1.0]), ma_window=0)

    def test_invalid_window_negative(self):
        """ma_window=-1 抛出 ValueError。"""
        with pytest.raises(ValueError):
            compute_trend_score(pd.Series([1.0]), ma_window=-1)


# ====================================================================== #
# compute_steady_score
# ====================================================================== #


class TestComputeSteadyScore:
    """稳健轨综合得分测试。

    实现: zscore(trend) + zscore(momentum) - zscore(volatility)
    三个特征分别做横截面 z-score 后线性组合。
    """

    def test_formula_verification(self):
        """验证公式: z(trend) + z(momentum) - z(volatility)。

        构造 5 只股票的横截面数据：
        - trend      = [0.01, 0.02, 0.03, 0.04, 0.05]  (mean=0.03)
        - momentum   = [0.05, 0.04, 0.03, 0.02, 0.01]  (mean=0.03)
        - volatility = [0.10, 0.20, 0.30, 0.40, 0.50]  (mean=0.30)

        三个序列的间距相同，z-score 后：
        - z_trend      = [-sqrt(2), -1/sqrt(2), 0, 1/sqrt(2), sqrt(2)]
        - z_momentum   = [ sqrt(2),  1/sqrt(2), 0, -1/sqrt(2), -sqrt(2)]
        - z_volatility = [-sqrt(2), -1/sqrt(2), 0, 1/sqrt(2), sqrt(2)]

        steady = z_trend + z_momentum - z_volatility:
        - index 0: -sqrt(2)+sqrt(2)-(-sqrt(2)) = sqrt(2)
        - index 1: -1/sqrt(2)+1/sqrt(2)-(-1/sqrt(2)) = 1/sqrt(2)
        - index 2: 0+0-0 = 0
        - index 3: 1/sqrt(2)+(-1/sqrt(2))-1/sqrt(2) = -1/sqrt(2)
        - index 4: sqrt(2)+(-sqrt(2))-sqrt(2) = -sqrt(2)
        """
        trend = pd.Series([0.01, 0.02, 0.03, 0.04, 0.05])
        momentum = pd.Series([0.05, 0.04, 0.03, 0.02, 0.01])
        volatility = pd.Series([0.10, 0.20, 0.30, 0.40, 0.50])
        result = compute_steady_score(trend, momentum, volatility)
        sqrt2 = math.sqrt(2.0)
        assert result.iloc[0] == pytest.approx(sqrt2, rel=1e-9)
        assert result.iloc[1] == pytest.approx(1.0 / sqrt2, rel=1e-9)
        assert result.iloc[2] == pytest.approx(0.0, abs=1e-12)
        assert result.iloc[3] == pytest.approx(-1.0 / sqrt2, rel=1e-9)
        assert result.iloc[4] == pytest.approx(-sqrt2, rel=1e-9)

    def test_matches_manual_zscore_combination(self):
        """与手动 z-score 组合结果一致。"""
        trend = pd.Series([0.01, 0.02, 0.03, 0.04, 0.05])
        momentum = pd.Series([0.05, 0.04, 0.03, 0.02, 0.01])
        volatility = pd.Series([0.10, 0.20, 0.30, 0.40, 0.50])
        result = compute_steady_score(trend, momentum, volatility)

        z_trend = zscore_cross_sectional(trend)
        z_momentum = zscore_cross_sectional(momentum)
        z_volatility = zscore_cross_sectional(volatility)
        expected = z_trend + z_momentum - z_volatility
        pd.testing.assert_series_equal(result, expected, check_names=False)

    def test_insufficient_samples_returns_nan(self):
        """任一特征有效样本 < 5 时对应得分为 NaN。"""
        # 仅 4 只股票 → 三个 z-score 均为全 NaN
        trend = pd.Series([0.01, 0.02, 0.03, 0.04])
        momentum = pd.Series([0.05, 0.04, 0.03, 0.02])
        volatility = pd.Series([0.10, 0.20, 0.30, 0.40])
        result = compute_steady_score(trend, momentum, volatility)
        assert result.isna().all()

    def test_zero_std_in_one_feature_returns_nan(self):
        """某一特征标准差为 0 时对应得分为 NaN。"""
        trend = pd.Series([0.01, 0.02, 0.03, 0.04, 0.05])
        momentum = pd.Series([0.03, 0.03, 0.03, 0.03, 0.03])  # std=0
        volatility = pd.Series([0.10, 0.20, 0.30, 0.40, 0.50])
        result = compute_steady_score(trend, momentum, volatility)
        # z_momentum 全 NaN → steady 全 NaN
        assert result.isna().all()

    def test_preserves_index(self):
        """结果索引与输入一致。"""
        idx = pd.Index(["A", "B", "C", "D", "E"])
        trend = pd.Series([0.01, 0.02, 0.03, 0.04, 0.05], index=idx)
        momentum = pd.Series([0.05, 0.04, 0.03, 0.02, 0.01], index=idx)
        volatility = pd.Series([0.10, 0.20, 0.30, 0.40, 0.50], index=idx)
        result = compute_steady_score(trend, momentum, volatility)
        assert result.index.equals(idx)
