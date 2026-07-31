"""激进轨样本外收益的蒙特卡洛概率分析。

对激进轨拼接后的样本外日收益执行 moving-block bootstrap 蒙特卡洛分析，
估计一年（244 个交易日）内：

- 期末资金达到或超过十倍目标（10000 元）的概率；
- 任意时点回撤或资金损失达到 50% 的概率；
- 期末资金近似归零（不高于 100 元）的概率；
- 期末资金的 1%/5%/25%/50%/75%/95%/99% 分位数。

重要声明：本模块仅用于概率研究与方法论演示，不构成任何收益承诺、
投资建议或实盘依据。激进轨永远为 ``SIMULATION_ONLY``。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class MonteCarloConfig:
    """蒙特卡洛分析配置。

    所有阈值、窗口、随机种子与路径参数集中于此，禁止在算法代码中硬编码。

    Attributes:
        random_seed: 随机种子，保证相同输入下结果完全可复现。
        n_paths: 模拟路径数量。
        path_length: 每条路径的交易日长度（默认一年 244 日）。
        block_length: moving-block bootstrap 的块长度（日）。
        initial_capital: 每条路径的初始资金。
        ten_x_target: 十倍目标资金，期末资金达到或超过该值计为“十倍”。
        loss_50_threshold: 50% 损失/回撤阈值，取初始资金的 50%（默认 500）。
        near_zero_threshold: 近似归零阈值，期末资金不高于该值计为“归零”。
    """

    random_seed: int = 20260731
    n_paths: int = 10_000
    path_length: int = 244
    block_length: int = 5
    initial_capital: float = 1000.0
    ten_x_target: float = 10000.0
    loss_50_threshold: float = 500.0
    near_zero_threshold: float = 100.0


@dataclass
class MonteCarloResult:
    """蒙特卡洛分析结果。

    Attributes:
        prob_ten_x: P(期末资金 >= ten_x_target)。
        prob_loss_50: P(任意时点回撤或损失达到 50%)。
        prob_near_zero: P(期末资金 <= near_zero_threshold)。
        percentiles: 期末资金分位数，键为 "1%".."99%"。
        n_oos_days: 实际使用的样本外天数。
        block_length: 块长度。
        n_paths: 路径数量。
        random_seed: 随机种子。
        insufficient_sample: 样本外天数不足 path_length 时为 True。
    """

    prob_ten_x: float
    prob_loss_50: float
    prob_near_zero: float
    percentiles: dict[str, float]
    n_oos_days: int
    block_length: int
    n_paths: int
    random_seed: int
    insufficient_sample: bool


# 期末资金分位数列表（%）。
_PERCENTILES: tuple[int, ...] = (1, 5, 25, 50, 75, 95, 99)


def _clip01(value: float) -> float:
    """将概率值限制在 [0, 1] 区间。"""
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def _compute_percentiles(end_capitals: np.ndarray) -> dict[str, float]:
    """计算期末资金分位数。

    Args:
        end_capitals: 各路径期末资金的一维数组。

    Returns:
        键为 "1%".."99%"、值为对应分位数的字典。
    """
    values = np.asarray(end_capitals, dtype=float)
    return {f"{p}%": float(np.percentile(values, p)) for p in _PERCENTILES}


def run_monte_carlo(
    daily_returns: list[float], config: MonteCarloConfig
) -> MonteCarloResult:
    """对样本外日收益执行 moving-block bootstrap 蒙特卡洛分析。

    使用长度为 ``config.block_length`` 的移动块有放回抽样，构造
    ``config.n_paths`` 条长度为 ``config.path_length`` 的收益路径。
    每条路径从 ``config.initial_capital`` 起逐日复利
    ``capital_t = capital_{t-1} * (1 + return_t)``，再统计：

    - 十倍概率：期末资金 >= ``ten_x_target``；
    - 50% 损失/回撤概率：任意时点 ``capital / running_max_capital``
      <= ``loss_50_threshold / initial_capital``（即相对历史最高点回撤
      达到 50%；由于历史最高点始终不低于初始资金，该条件严格包含
      “资金跌至初始资金 50% 以下”的情形，等价于“回撤或绝对损失
      达到 50%”的并集）；
    - 近似归零概率：期末资金 <= ``near_zero_threshold``；
    - 期末资金分位数。

    当样本外天数少于 ``path_length`` 时仍可输出演示结果，但会标记
    ``insufficient_sample=True``，不得据此作概率结论。

    可复现性：全部随机数由 ``numpy.random.RandomState(config.random_seed)``
    一次性生成，相同输入与种子下结果逐字节一致。

    Args:
        daily_returns: 激进轨拼接后的样本外日收益序列。
        config: 蒙特卡洛配置。

    Returns:
        蒙特卡洛分析结果。

    Raises:
        ValueError: 当 ``block_length``、``path_length`` 或 ``n_paths`` 非正时。
    """
    if config.block_length < 1:
        raise ValueError(
            f"block_length 必须为正整数，得到 {config.block_length}"
        )
    if config.path_length < 1:
        raise ValueError(
            f"path_length 必须为正整数，得到 {config.path_length}"
        )
    if config.n_paths < 1:
        raise ValueError(f"n_paths 必须为正整数，得到 {config.n_paths}")

    block_length = config.block_length
    path_length = config.path_length
    n_paths = config.n_paths
    initial_capital = float(config.initial_capital)

    returns = np.asarray(daily_returns, dtype=float).ravel()
    n = int(returns.shape[0])
    insufficient_sample = n < path_length

    # 回撤/损失比例阈值（默认 500 / 1000 = 0.5）。
    loss_ratio_threshold = float(config.loss_50_threshold) / initial_capital

    rng = np.random.RandomState(config.random_seed)

    # 样本为空：无法抽样，所有路径保持初始资金，返回退化结果。
    if n == 0:
        end_capitals = np.full(n_paths, initial_capital, dtype=float)
        return MonteCarloResult(
            prob_ten_x=_clip01(float(np.mean(end_capitals >= config.ten_x_target))),
            prob_loss_50=0.0,  # 无收益序列不存在回撤
            prob_near_zero=_clip01(
                float(np.mean(end_capitals <= config.near_zero_threshold))
            ),
            percentiles=_compute_percentiles(end_capitals),
            n_oos_days=0,
            block_length=block_length,
            n_paths=n_paths,
            random_seed=config.random_seed,
            insufficient_sample=True,
        )

    # 每条路径需要的块数（向上取整），拼接后截断到 path_length。
    n_blocks = (path_length + block_length - 1) // block_length

    # 有效块起始索引范围。
    if n >= block_length:
        # 标准 moving-block：块不跨越序列末端，起始索引 0..n-block_length。
        n_starts = n - block_length + 1
        wrap = False
    else:
        # 样本不足一个块：循环（cycle through）后取块，起始索引 0..n-1。
        n_starts = n
        wrap = True

    # 一次性抽取所有路径、所有块的起始索引，保证完全可复现。
    starts = rng.randint(0, n_starts, size=(n_paths, n_blocks))
    offsets = np.arange(block_length)
    if wrap:
        idx = (starts[:, :, None] + offsets[None, None, :]) % n
    else:
        idx = starts[:, :, None] + offsets[None, None, :]

    # 拼接各块收益并截断到 path_length。
    # sampled 形状：(n_paths, n_blocks * block_length) -> (n_paths, path_length)
    sampled = returns[idx].reshape(n_paths, n_blocks * block_length)
    path_returns = sampled[:, :path_length]

    # 逐日复利：capital_t = capital_{t-1} * (1 + return_t)。
    capitals = initial_capital * np.cumprod(1.0 + path_returns, axis=1)
    end_capitals = capitals[:, -1]

    # 十倍概率：期末资金 >= ten_x_target。
    prob_ten_x = _clip01(float(np.mean(end_capitals >= config.ten_x_target)))

    # 近似归零概率：期末资金 <= near_zero_threshold。
    prob_near_zero = _clip01(
        float(np.mean(end_capitals <= config.near_zero_threshold))
    )

    # 任意时点回撤/损失达到 50%：capital / running_max <= loss_ratio_threshold。
    # 历史最高点包含初始资金，确保“跌至初始资金 50%”也被捕获。
    running_max = np.maximum.accumulate(capitals, axis=1)
    running_max = np.maximum(running_max, initial_capital)
    # 防御除零：initial_capital > 0 时 running_max 恒为正。
    safe_max = np.where(running_max == 0.0, 1.0, running_max)
    drawdown_ratio = capitals / safe_max
    prob_loss_50 = _clip01(
        float(np.mean(np.any(drawdown_ratio <= loss_ratio_threshold, axis=1)))
    )

    return MonteCarloResult(
        prob_ten_x=prob_ten_x,
        prob_loss_50=prob_loss_50,
        prob_near_zero=prob_near_zero,
        percentiles=_compute_percentiles(end_capitals),
        n_oos_days=n,
        block_length=block_length,
        n_paths=n_paths,
        random_seed=config.random_seed,
        insufficient_sample=insufficient_sample,
    )


__all__ = [
    "MonteCarloConfig",
    "MonteCarloResult",
    "run_monte_carlo",
]
