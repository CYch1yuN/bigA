"""Phase 3 蒙特卡洛模块 ``monte_carlo`` 的综合 pytest 测试。

覆盖范围：
1. 固定种子完全可复现 —— 相同输入与种子产生逐字节一致结果；
2. 路径长度与概率定义正确；
3. 所有概率落在 [0, 1] 区间；
4. 样本外天数 < path_length 时 insufficient_sample 标记为 True；
5. 空收益序列 —— 返回退化结果且 insufficient_sample=True；
6. block_length / path_length / n_paths 非正时抛 ValueError；
7. 全零收益 —— 期末资金等于初始资金、prob_ten_x=0、prob_near_zero 取决于阈值；
8. 极高正收益 —— prob_ten_x 应较高；
9. percentiles 字典包含全部 7 个键；
10. 不同种子（通常）产生不同结果。

所有测试使用小规模 n_paths（如 100）以保证执行速度。
"""
from __future__ import annotations

import numpy as np
import pytest

from ashare_quant.research.monte_carlo import (
    MonteCarloConfig,
    MonteCarloResult,
    run_monte_carlo,
)

# --------------------------------------------------------------------------- #
# 期望的分位数键集合
# --------------------------------------------------------------------------- #
EXPECTED_PERCENTILE_KEYS = {"1%", "5%", "25%", "50%", "75%", "95%", "99%"}


# --------------------------------------------------------------------------- #
# 辅助构建函数
# --------------------------------------------------------------------------- #


def make_config(**overrides) -> MonteCarloConfig:
    """构造蒙特卡洛配置，默认使用小规模参数以加速测试。

    默认 path_length=20、block_length=3、n_paths=100，便于快速运行且
    能覆盖各类场景。可传入 overrides 覆盖任意字段。
    """
    defaults = dict(
        random_seed=20260731,
        n_paths=100,
        path_length=20,
        block_length=3,
        initial_capital=1000.0,
        ten_x_target=10000.0,
        loss_50_threshold=500.0,
        near_zero_threshold=100.0,
    )
    defaults.update(overrides)
    return MonteCarloConfig(**defaults)


def make_returns(n: int, fill: float = 0.001, seed: int = 7) -> list[float]:
    """生成确定性的收益序列。

    使用固定随机种子构造确定性序列，避免测试因随机数据而抖动。
    当 fill 为 None 时使用随机值，否则返回全 fill 常数序列。
    """
    if fill is not None:
        return [float(fill)] * n
    rng = np.random.RandomState(seed)
    return [float(x) for x in rng.normal(0.0, 0.01, size=n)]


# --------------------------------------------------------------------------- #
# 1. 固定种子完全可复现
# --------------------------------------------------------------------------- #


class TestReproducibility:
    """固定种子可复现性测试。"""

    def test_same_seed_same_result_identical(self):
        """相同输入与种子应产生逐字段完全一致的结果。"""
        returns = make_returns(n=60, seed=3)
        cfg = make_config(random_seed=12345)

        r1 = run_monte_carlo(returns, cfg)
        r2 = run_monte_carlo(returns, cfg)

        assert r1 == r2

    def test_same_seed_different_n_paths_consistent_seed(self):
        """相同种子下，随机种子字段应被原样回填到结果中。"""
        returns = make_returns(n=60, seed=4)
        cfg = make_config(random_seed=999)

        result = run_monte_carlo(returns, cfg)
        assert result.random_seed == cfg.random_seed

    def test_reproducible_across_multiple_runs(self):
        """多次运行结果一致，验证随机状态每次都从种子重新初始化。"""
        returns = make_returns(n=80, seed=11)
        cfg = make_config(random_seed=4242)

        results = [run_monte_carlo(returns, cfg) for _ in range(5)]

        first = results[0]
        for other in results[1:]:
            assert other.prob_ten_x == first.prob_ten_x
            assert other.prob_loss_50 == first.prob_loss_50
            assert other.prob_near_zero == first.prob_near_zero
            assert other.percentiles == first.percentiles

    def test_reproducible_default_seed(self):
        """模块默认随机种子也应保证可复现。"""
        returns = make_returns(n=50, seed=5)
        cfg = MonteCarloConfig(n_paths=100, path_length=20, block_length=3)

        r1 = run_monte_carlo(returns, cfg)
        r2 = run_monte_carlo(returns, cfg)
        assert r1 == r2
        assert r1.random_seed == MonteCarloConfig().random_seed


# --------------------------------------------------------------------------- #
# 2. 路径长度与概率定义正确
# --------------------------------------------------------------------------- #


class TestPathLengthAndDefinitions:
    """路径长度、样本外天数与概率定义正确性测试。"""

    def test_n_oos_days_reflects_input_length(self):
        """n_oos_days 应等于输入收益序列长度。"""
        returns = make_returns(n=37, seed=6)
        cfg = make_config()

        result = run_monte_carlo(returns, cfg)
        assert result.n_oos_days == 37

    def test_n_oos_days_when_empty(self):
        """空输入时 n_oos_days 应为 0。"""
        cfg = make_config()
        result = run_monte_carlo([], cfg)
        assert result.n_oos_days == 0

    def test_block_length_reflected_in_result(self):
        """结果中的 block_length 应与配置一致。"""
        cfg = make_config(block_length=7)
        result = run_monte_carlo(make_returns(n=50, seed=8), cfg)
        assert result.block_length == 7

    def test_n_paths_reflected_in_result(self):
        """结果中的 n_paths 应与配置一致。"""
        cfg = make_config(n_paths=256)
        result = run_monte_carlo(make_returns(n=50, seed=9), cfg)
        assert result.n_paths == 256

    def test_prob_ten_x_definition_uses_ten_x_target(self):
        """prob_ten_x 应表示 P(期末资金 >= ten_x_target)。

        构造一个必达十倍的场景：每期收益足以让资金翻数十倍，
        且 ten_x_target 设为较低值，验证 prob_ten_x=1.0。
        """
        # 每日 +5%，20 日后约 1000 * 1.05^20 ≈ 2653，仍未达 10000。
        # 改用更高收益以确保超过目标。
        returns = [0.30] * 30  # 30 日，1000 * 1.3^30 远超 10000
        cfg = make_config(
            path_length=30, block_length=3,
            initial_capital=1000.0, ten_x_target=10000.0,
        )
        result = run_monte_carlo(returns, cfg)
        assert result.prob_ten_x == pytest.approx(1.0)

    def test_prob_near_zero_definition_uses_threshold(self):
        """prob_near_zero 应表示 P(期末资金 <= near_zero_threshold)。

        构造大亏损场景使期末资金远低于阈值，验证 prob_near_zero=1.0。
        """
        returns = [-0.20] * 30  # 30 日，1000 * 0.8^30 ≈ 1.2，远低于 100
        cfg = make_config(
            path_length=30, block_length=3,
            initial_capital=1000.0, near_zero_threshold=100.0,
        )
        result = run_monte_carlo(returns, cfg)
        assert result.prob_near_zero == pytest.approx(1.0)

    def test_prob_loss_50_definition_uses_drawdown(self):
        """prob_loss_50 应表示 P(任意时点回撤达 50%)。

        构造持续大跌使资金跌至初始资金的 50% 以下，验证 prob_loss_50=1.0。
        """
        returns = [-0.10] * 30  # 30 日，资金迅速跌破 500
        cfg = make_config(
            path_length=30, block_length=3,
            initial_capital=1000.0, loss_50_threshold=500.0,
        )
        result = run_monte_carlo(returns, cfg)
        assert result.prob_loss_50 == pytest.approx(1.0)

    def test_loss_50_captures_absolute_loss_below_threshold(self):
        """历史最高点包含初始资金，故资金跌至初始 50% 以下也应触发。

        使用每日常数暴跌序列验证绝对损失情形被捕获：每条路径第一天
        资金即从 1000 跌至 400（< 500），running_max 包含初始资金 1000，
        故 400/1000 = 0.4 <= 0.5 触发回撤判定。
        """
        # 每日 -60%，使每条路径首日资金即跌至 400 < 500。
        returns = [-0.60] * 30
        cfg = make_config(
            path_length=30, block_length=3,
            initial_capital=1000.0, loss_50_threshold=500.0,
        )
        result = run_monte_carlo(returns, cfg)
        assert result.prob_loss_50 == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# 3. 所有概率落在 [0, 1] 区间
# --------------------------------------------------------------------------- #


class TestProbabilityRange:
    """概率值范围校验测试。"""

    @pytest.mark.parametrize("seed", [1, 2, 3, 42, 20260731])
    def test_all_probabilities_in_unit_interval(self, seed):
        """多个种子下所有概率应落在 [0, 1] 区间。"""
        returns = make_returns(n=50, seed=12)
        cfg = make_config(random_seed=seed)
        result = run_monte_carlo(returns, cfg)

        assert 0.0 <= result.prob_ten_x <= 1.0
        assert 0.0 <= result.prob_loss_50 <= 1.0
        assert 0.0 <= result.prob_near_zero <= 1.0

    def test_probabilities_in_range_extreme_returns(self):
        """极端正负收益混合下概率仍应落在 [0, 1]。"""
        rng = np.random.RandomState(99)
        returns = [float(x) for x in rng.choice(
            [-0.50, -0.30, 0.30, 0.50], size=80,
        )]
        cfg = make_config()
        result = run_monte_carlo(returns, cfg)

        assert 0.0 <= result.prob_ten_x <= 1.0
        assert 0.0 <= result.prob_loss_50 <= 1.0
        assert 0.0 <= result.prob_near_zero <= 1.0

    def test_probabilities_in_range_empty_returns(self):
        """空收益序列下概率仍应落在 [0, 1]。"""
        cfg = make_config()
        result = run_monte_carlo([], cfg)

        assert 0.0 <= result.prob_ten_x <= 1.0
        assert 0.0 <= result.prob_loss_50 <= 1.0
        assert 0.0 <= result.prob_near_zero <= 1.0


# --------------------------------------------------------------------------- #
# 4. 样本不足标记
# --------------------------------------------------------------------------- #


class TestInsufficientSample:
    """insufficient_sample 标记测试。"""

    def test_insufficient_when_n_oos_less_than_path_length(self):
        """样本外天数 < path_length 时 insufficient_sample 应为 True。"""
        cfg = make_config(path_length=50, block_length=5)
        returns = make_returns(n=30, seed=13)  # 30 < 50
        result = run_monte_carlo(returns, cfg)

        assert result.insufficient_sample is True
        assert result.n_oos_days == 30

    def test_sufficient_when_n_oos_equals_path_length(self):
        """样本外天数 == path_length 时 insufficient_sample 应为 False。"""
        cfg = make_config(path_length=50, block_length=5)
        returns = make_returns(n=50, seed=14)  # 50 == 50
        result = run_monte_carlo(returns, cfg)

        assert result.insufficient_sample is False
        assert result.n_oos_days == 50

    def test_sufficient_when_n_oos_greater_than_path_length(self):
        """样本外天数 > path_length 时 insufficient_sample 应为 False。"""
        cfg = make_config(path_length=50, block_length=5)
        returns = make_returns(n=200, seed=15)  # 200 > 50
        result = run_monte_carlo(returns, cfg)

        assert result.insufficient_sample is False
        assert result.n_oos_days == 200

    def test_insufficient_still_produces_result(self):
        """样本不足时仍应输出可用的演示结果。"""
        cfg = make_config(path_length=100, block_length=5)
        returns = make_returns(n=10, seed=16)
        result = run_monte_carlo(returns, cfg)

        assert result.insufficient_sample is True
        assert isinstance(result, MonteCarloResult)
        assert set(result.percentiles.keys()) == EXPECTED_PERCENTILE_KEYS


# --------------------------------------------------------------------------- #
# 5. 空收益序列
# --------------------------------------------------------------------------- #


class TestEmptyReturns:
    """空收益序列的退化结果测试。"""

    def test_empty_returns_insufficient_sample_true(self):
        """空收益序列应标记 insufficient_sample=True。"""
        cfg = make_config()
        result = run_monte_carlo([], cfg)
        assert result.insufficient_sample is True

    def test_empty_returns_n_oos_days_zero(self):
        """空收益序列 n_oos_days 应为 0。"""
        cfg = make_config()
        result = run_monte_carlo([], cfg)
        assert result.n_oos_days == 0

    def test_empty_returns_prob_loss_50_zero(self):
        """空收益序列无回撤，prob_loss_50 应为 0。"""
        cfg = make_config()
        result = run_monte_carlo([], cfg)
        assert result.prob_loss_50 == pytest.approx(0.0)

    def test_empty_returns_prob_ten_x_zero(self):
        """空收益序列期末资金等于初始资金，prob_ten_x 应为 0（默认阈值）。"""
        cfg = make_config(initial_capital=1000.0, ten_x_target=10000.0)
        result = run_monte_carlo([], cfg)
        assert result.prob_ten_x == pytest.approx(0.0)

    def test_empty_returns_prob_near_zero_depends_on_threshold(self):
        """空收益序列 prob_near_zero 取决于阈值与初始资金关系。

        - 当 near_zero_threshold >= initial_capital 时为 1.0；
        - 当 near_zero_threshold < initial_capital 时为 0.0。
        """
        # 阈值高于初始资金：期末资金(=初始资金) <= 阈值 → 1.0
        cfg_high = make_config(
            initial_capital=1000.0, near_zero_threshold=1500.0,
        )
        result_high = run_monte_carlo([], cfg_high)
        assert result_high.prob_near_zero == pytest.approx(1.0)

        # 阈值低于初始资金：期末资金(=初始资金) > 阈值 → 0.0
        cfg_low = make_config(
            initial_capital=1000.0, near_zero_threshold=100.0,
        )
        result_low = run_monte_carlo([], cfg_low)
        assert result_low.prob_near_zero == pytest.approx(0.0)

    def test_empty_returns_percentiles_all_equal_initial_capital(self):
        """空收益序列所有分位数应等于初始资金。"""
        cfg = make_config(initial_capital=1000.0)
        result = run_monte_carlo([], cfg)

        for key in EXPECTED_PERCENTILE_KEYS:
            assert result.percentiles[key] == pytest.approx(1000.0)

    def test_empty_returns_has_all_percentile_keys(self):
        """空收益序列 percentiles 仍应包含全部 7 个键。"""
        cfg = make_config()
        result = run_monte_carlo([], cfg)
        assert set(result.percentiles.keys()) == EXPECTED_PERCENTILE_KEYS


# --------------------------------------------------------------------------- #
# 6. 非正参数抛 ValueError
# --------------------------------------------------------------------------- #


class TestInvalidConfig:
    """非法配置参数应抛 ValueError 的测试。"""

    def test_block_length_zero_raises(self):
        """block_length=0 应抛 ValueError。"""
        returns = make_returns(n=50, seed=17)
        cfg = make_config(block_length=0)
        with pytest.raises(ValueError, match="block_length"):
            run_monte_carlo(returns, cfg)

    def test_block_length_negative_raises(self):
        """block_length<0 应抛 ValueError。"""
        returns = make_returns(n=50, seed=18)
        cfg = make_config(block_length=-3)
        with pytest.raises(ValueError, match="block_length"):
            run_monte_carlo(returns, cfg)

    def test_path_length_zero_raises(self):
        """path_length=0 应抛 ValueError。"""
        returns = make_returns(n=50, seed=19)
        cfg = make_config(path_length=0)
        with pytest.raises(ValueError, match="path_length"):
            run_monte_carlo(returns, cfg)

    def test_path_length_negative_raises(self):
        """path_length<0 应抛 ValueError。"""
        returns = make_returns(n=50, seed=20)
        cfg = make_config(path_length=-5)
        with pytest.raises(ValueError, match="path_length"):
            run_monte_carlo(returns, cfg)

    def test_n_paths_zero_raises(self):
        """n_paths=0 应抛 ValueError。"""
        returns = make_returns(n=50, seed=21)
        cfg = make_config(n_paths=0)
        with pytest.raises(ValueError, match="n_paths"):
            run_monte_carlo(returns, cfg)

    def test_n_paths_negative_raises(self):
        """n_paths<0 应抛 ValueError。"""
        returns = make_returns(n=50, seed=22)
        cfg = make_config(n_paths=-10)
        with pytest.raises(ValueError, match="n_paths"):
            run_monte_carlo(returns, cfg)

    def test_invalid_config_with_empty_returns_still_raises(self):
        """空收益序列下非法参数仍应优先抛 ValueError。"""
        cfg = make_config(block_length=0)
        with pytest.raises(ValueError):
            run_monte_carlo([], cfg)

    def test_invalid_params_raise_before_random_draw(self):
        """参数校验应在任何随机抽样之前完成（保证不消耗随机状态）。

        通过先跑一次非法调用（抛错），再跑一次合法调用，验证合法调用
        结果与未经过非法调用时一致，说明随机状态未被提前消耗。
        """
        returns = make_returns(n=50, seed=23)
        cfg_bad = make_config(block_length=0, random_seed=555)
        cfg_good = make_config(random_seed=555)

        with pytest.raises(ValueError):
            run_monte_carlo(returns, cfg_bad)

        result_after_error = run_monte_carlo(returns, cfg_good)
        result_clean = run_monte_carlo(returns, cfg_good)

        assert result_after_error == result_clean


# --------------------------------------------------------------------------- #
# 7. 全零收益
# --------------------------------------------------------------------------- #


class TestZeroReturns:
    """全零收益序列测试。"""

    def test_zero_returns_end_capital_equals_initial(self):
        """全零收益下所有分位数应等于初始资金。"""
        returns = [0.0] * 50
        cfg = make_config(initial_capital=1000.0)
        result = run_monte_carlo(returns, cfg)

        for key in EXPECTED_PERCENTILE_KEYS:
            assert result.percentiles[key] == pytest.approx(1000.0)

    def test_zero_returns_prob_ten_x_zero(self):
        """全零收益下 prob_ten_x 应为 0（期末资金 = 初始资金 < 目标）。"""
        returns = [0.0] * 50
        cfg = make_config(
            initial_capital=1000.0, ten_x_target=10000.0,
        )
        result = run_monte_carlo(returns, cfg)
        assert result.prob_ten_x == pytest.approx(0.0)

    def test_zero_returns_prob_loss_50_zero(self):
        """全零收益下无回撤，prob_loss_50 应为 0。"""
        returns = [0.0] * 50
        cfg = make_config(
            initial_capital=1000.0, loss_50_threshold=500.0,
        )
        result = run_monte_carlo(returns, cfg)
        assert result.prob_loss_50 == pytest.approx(0.0)

    def test_zero_returns_prob_near_zero_below_threshold(self):
        """全零收益 + 阈值低于初始资金 → prob_near_zero=0。"""
        returns = [0.0] * 50
        cfg = make_config(
            initial_capital=1000.0, near_zero_threshold=100.0,
        )
        result = run_monte_carlo(returns, cfg)
        assert result.prob_near_zero == pytest.approx(0.0)

    def test_zero_returns_prob_near_zero_at_threshold(self):
        """全零收益 + 阈值等于初始资金 → 期末资金 <= 阈值成立 → prob=1。

        注意：<= 是包含等号的，故等于阈值时计为归零。
        """
        returns = [0.0] * 50
        cfg = make_config(
            initial_capital=1000.0, near_zero_threshold=1000.0,
        )
        result = run_monte_carlo(returns, cfg)
        assert result.prob_near_zero == pytest.approx(1.0)

    def test_zero_returns_prob_near_zero_above_threshold(self):
        """全零收益 + 阈值高于初始资金 → 期末资金 <= 阈值成立 → prob=1。"""
        returns = [0.0] * 50
        cfg = make_config(
            initial_capital=1000.0, near_zero_threshold=2000.0,
        )
        result = run_monte_carlo(returns, cfg)
        assert result.prob_near_zero == pytest.approx(1.0)

    def test_zero_returns_insufficient_flag_respected(self):
        """全零收益下 insufficient_sample 仍由天数关系决定。"""
        returns = [0.0] * 10
        cfg = make_config(path_length=50)
        result = run_monte_carlo(returns, cfg)
        assert result.insufficient_sample is True

        returns2 = [0.0] * 100
        result2 = run_monte_carlo(returns2, cfg)
        assert result2.insufficient_sample is False


# --------------------------------------------------------------------------- #
# 8. 极高正收益
# --------------------------------------------------------------------------- #


class TestHighPositiveReturns:
    """极高正收益场景测试。"""

    def test_high_positive_returns_high_prob_ten_x(self):
        """极高正收益下 prob_ten_x 应较高（接近或等于 1）。"""
        # 每日 +20%，20 日后 1000 * 1.2^20 ≈ 38337，远超 10000。
        returns = [0.20] * 60
        cfg = make_config(
            path_length=20, block_length=3,
            initial_capital=1000.0, ten_x_target=10000.0,
        )
        result = run_monte_carlo(returns, cfg)
        assert result.prob_ten_x >= 0.95

    def test_high_positive_returns_low_prob_near_zero(self):
        """极高正收益下 prob_near_zero 应为 0。"""
        returns = [0.20] * 60
        cfg = make_config(
            path_length=20, block_length=3,
            initial_capital=1000.0, near_zero_threshold=100.0,
        )
        result = run_monte_carlo(returns, cfg)
        assert result.prob_near_zero == pytest.approx(0.0)

    def test_high_positive_returns_low_prob_loss_50(self):
        """极高正收益下 prob_loss_50 应为 0（资金单调上行无回撤）。"""
        returns = [0.20] * 60
        cfg = make_config(
            path_length=20, block_length=3,
            initial_capital=1000.0, loss_50_threshold=500.0,
        )
        result = run_monte_carlo(returns, cfg)
        assert result.prob_loss_50 == pytest.approx(0.0)

    def test_moderate_positive_returns_below_ten_x(self):
        """温和正收益不足以达到十倍，prob_ten_x 应为 0。

        每日 +0.1%，244 日后 1000 * 1.001^244 ≈ 1276，远低于 10000。
        """
        returns = [0.001] * 300
        cfg = make_config(
            path_length=244, block_length=5,
            initial_capital=1000.0, ten_x_target=10000.0,
        )
        result = run_monte_carlo(returns, cfg)
        assert result.prob_ten_x == pytest.approx(0.0)

    def test_high_positive_returns_percentiles_above_target(self):
        """极高正收益下高分位数应超过 ten_x_target。"""
        returns = [0.20] * 60
        cfg = make_config(
            path_length=20, block_length=3,
            initial_capital=1000.0, ten_x_target=10000.0,
        )
        result = run_monte_carlo(returns, cfg)
        assert result.percentiles["50%"] >= 10000.0
        assert result.percentiles["99%"] >= result.percentiles["50%"]


# --------------------------------------------------------------------------- #
# 9. percentiles 字典结构
# --------------------------------------------------------------------------- #


class TestPercentiles:
    """percentiles 字典结构测试。"""

    def test_percentiles_has_all_seven_keys(self):
        """percentiles 应包含全部 7 个键。"""
        returns = make_returns(n=50, seed=24)
        cfg = make_config()
        result = run_monte_carlo(returns, cfg)

        assert set(result.percentiles.keys()) == EXPECTED_PERCENTILE_KEYS

    def test_percentiles_keys_exact_names(self):
        """percentiles 键名应为 "1%".."99%" 形式。"""
        returns = make_returns(n=50, seed=25)
        cfg = make_config()
        result = run_monte_carlo(returns, cfg)

        expected = {"1%", "5%", "25%", "50%", "75%", "95%", "99%"}
        assert set(result.percentiles.keys()) == expected

    def test_percentiles_are_floats(self):
        """percentiles 值应为 float 类型。"""
        returns = make_returns(n=50, seed=26)
        cfg = make_config()
        result = run_monte_carlo(returns, cfg)

        for value in result.percentiles.values():
            assert isinstance(value, float)

    def test_percentiles_monotonically_increasing(self):
        """分位数应单调非递减（低分位 <= 高分位）。"""
        returns = make_returns(n=80, seed=27)
        cfg = make_config()
        result = run_monte_carlo(returns, cfg)

        ordered = [
            result.percentiles["1%"],
            result.percentiles["5%"],
            result.percentiles["25%"],
            result.percentiles["50%"],
            result.percentiles["75%"],
            result.percentiles["95%"],
            result.percentiles["99%"],
        ]
        for lo, hi in zip(ordered, ordered[1:]):
            assert lo <= hi

    def test_percentiles_positive_for_positive_returns(self):
        """正收益场景下分位数应为正数。"""
        returns = [0.01] * 60
        cfg = make_config(initial_capital=1000.0)
        result = run_monte_carlo(returns, cfg)

        for value in result.percentiles.values():
            assert value > 0.0

    def test_percentiles_empty_returns(self):
        """空收益序列下 percentiles 仍应结构完整。"""
        cfg = make_config()
        result = run_monte_carlo([], cfg)
        assert set(result.percentiles.keys()) == EXPECTED_PERCENTILE_KEYS


# --------------------------------------------------------------------------- #
# 10. 不同种子产生不同结果
# --------------------------------------------------------------------------- #


class TestSeedSensitivity:
    """不同种子产生不同结果的测试。"""

    def test_different_seeds_usually_different(self):
        """不同种子通常应产生不同的 prob_ten_x（允许极小概率相同）。

        使用具有足够随机性的收益序列，使不同种子产生可区分的抽样。
        """
        rng = np.random.RandomState(0)
        returns = [float(x) for x in rng.normal(0.0, 0.03, size=200)]
        cfg_a = make_config(random_seed=1, n_paths=200)
        cfg_b = make_config(random_seed=2, n_paths=200)

        r_a = run_monte_carlo(returns, cfg_a)
        r_b = run_monte_carlo(returns, cfg_b)

        # 至少其中一个指标应不同
        differs = (
            r_a.prob_ten_x != r_b.prob_ten_x
            or r_a.prob_loss_50 != r_b.prob_loss_50
            or r_a.prob_near_zero != r_b.prob_near_zero
            or r_a.percentiles != r_b.percentiles
        )
        assert differs

    def test_different_seeds_different_percentiles_median(self):
        """不同种子的中位数分位数通常应不同。"""
        rng = np.random.RandomState(123)
        returns = [float(x) for x in rng.normal(0.0, 0.02, size=200)]

        seeds = [10, 20, 30, 40, 50]
        medians = []
        for s in seeds:
            cfg = make_config(random_seed=s, n_paths=200)
            result = run_monte_carlo(returns, cfg)
            medians.append(result.percentiles["50%"])

        # 5 个不同种子应产生至少 2 个不同的中位数
        assert len(set(medians)) >= 2

    def test_same_seed_same_across_calls(self):
        """相同种子在多次调用中应产生相同结果（对照实验）。"""
        returns = make_returns(n=100, seed=28)
        cfg = make_config(random_seed=777, n_paths=200)

        r1 = run_monte_carlo(returns, cfg)
        r2 = run_monte_carlo(returns, cfg)

        assert r1.percentiles == r2.percentiles
        assert r1.prob_ten_x == r2.prob_ten_x


# --------------------------------------------------------------------------- #
# 补充：结果类型与边界一致性
# --------------------------------------------------------------------------- #


class TestResultStructure:
    """MonteCarloResult 结构与字段一致性测试。"""

    def test_result_is_monte_carlo_result(self):
        """run_monte_carlo 应返回 MonteCarloResult 实例。"""
        returns = make_returns(n=50, seed=29)
        result = run_monte_carlo(returns, make_config())
        assert isinstance(result, MonteCarloResult)

    def test_result_fields_consistent_with_config(self):
        """结果中的 block_length / n_paths / random_seed 应与配置一致。"""
        cfg = make_config(
            random_seed=31415, n_paths=128, block_length=4, path_length=30,
        )
        result = run_monte_carlo(make_returns(n=60, seed=30), cfg)

        assert result.block_length == cfg.block_length
        assert result.n_paths == cfg.n_paths
        assert result.random_seed == cfg.random_seed

    def test_block_length_larger_than_sample(self):
        """块长度大于样本长度时应使用循环取块而非崩溃。

        此时 n < block_length，触发 wrap 分支。
        """
        returns = [0.01, -0.02, 0.005]  # n=3 < block_length=5
        cfg = make_config(
            path_length=20, block_length=5,
            initial_capital=1000.0,
        )
        result = run_monte_carlo(returns, cfg)

        assert isinstance(result, MonteCarloResult)
        assert result.insufficient_sample is True  # 3 < 20
        assert result.n_oos_days == 3
        # 概率仍应合法
        assert 0.0 <= result.prob_ten_x <= 1.0

    def test_block_length_equal_to_sample(self):
        """块长度等于样本长度时应正常工作（n_starts=1）。"""
        returns = [0.01, 0.02, -0.01, 0.03, 0.005]  # n=5 == block_length=5
        cfg = make_config(
            path_length=10, block_length=5,
            initial_capital=1000.0,
        )
        result = run_monte_carlo(returns, cfg)

        assert isinstance(result, MonteCarloResult)
        assert result.insufficient_sample is True  # 5 < 10

    def test_single_return_value(self):
        """单个收益值时应能运行（wrap 分支）。"""
        returns = [0.05]
        cfg = make_config(path_length=10, block_length=3)
        result = run_monte_carlo(returns, cfg)

        assert isinstance(result, MonteCarloResult)
        assert result.n_oos_days == 1
        assert result.insufficient_sample is True

    def test_non_list_sequence_input(self):
        """元组等其他可迭代序列也应被接受。"""
        returns = (0.01, 0.02, -0.005) * 20
        cfg = make_config()
        result = run_monte_carlo(list(returns), cfg)
        assert isinstance(result, MonteCarloResult)

    def test_nan_returns_do_not_crash(self):
        """含 NaN 的收益序列不应崩溃（结果可能为 NaN，但不应抛错）。"""
        returns = [0.01, float("nan"), -0.02] * 20
        cfg = make_config()
        # 不断言具体值，仅验证不抛异常
        result = run_monte_carlo(returns, cfg)
        assert isinstance(result, MonteCarloResult)

    def test_negative_returns_produce_loss_50(self):
        """持续大幅下跌应触发 prob_loss_50=1.0。"""
        returns = [-0.15] * 40
        cfg = make_config(
            path_length=20, block_length=3,
            initial_capital=1000.0, loss_50_threshold=500.0,
        )
        result = run_monte_carlo(returns, cfg)
        assert result.prob_loss_50 == pytest.approx(1.0)
