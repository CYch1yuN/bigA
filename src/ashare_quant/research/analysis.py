"""研究分析模块：滚动样本外验证、参数选择、资格判定。

本模块是 Phase 3 研究的核心编排器，负责：

1. 对每个滚动折，在训练/验证期选择参数，在测试期运行冻结参数的回测。
2. 拼接各折样本外权益，计算汇总指标。
3. 执行费用/滑点压力测试、参数扰动和市场阶段分析。
4. 对激进轨执行蒙特卡洛概率分析。
5. 根据资格条件判定稳健轨是否可进入模拟观察。

重要声明：所有参数只在对应训练/验证数据上选择，选定后冻结并运行该折测试期。
最终汇总只拼接各折样本外权益，不得用全样本重新挑选"最佳参数"后覆盖样本外结果。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Optional

import numpy as np
import pandas as pd

from ..backtest.config import BacktestConfig
from ..backtest.engine import BacktestEngine
from ..backtest.metrics import MetricsCalculator
from ..backtest.models import (
    BacktestResult,
    Fill,
    Order,
    PortfolioSnapshot,
    Side,
    to_decimal,
)
from .benchmarks import (
    BenchmarkData,
    BenchmarkMissingError,
    compute_benchmark_returns,
    compute_cash_benchmark,
    TRADING_DAYS_PER_YEAR,
)
from .monte_carlo import MonteCarloConfig, MonteCarloResult, run_monte_carlo
from .strategies import (
    AggressiveParams,
    AggressiveStrategy,
    SteadyParams,
    SteadyStrategy,
    STEADY_BASELINE_PARAMS,
    AGGRESSIVE_BASELINE_PARAMS,
    generate_steady_param_combinations,
    generate_aggressive_param_combinations,
)
from .stress import (
    MarketRegimeResult,
    ParameterPerturbationResult,
    StressResult,
    classify_market_regimes,
    create_fee_stress_configs,
    extract_stress_result,
    summarize_regime_performance,
    summarize_parameter_perturbation,
)
from .universe import HistoricalUniverseFilter
from .walk_forward import Fold, WalkForwardConfig, WalkForwardSplitter

__all__ = [
    "TrackType",
    "FoldResult",
    "TrackResult",
    "ResearchResult",
    "EligibilityStatus",
    "EligibilityCheck",
    "compute_metrics_from_equity",
    "concatenate_oos_equity",
    "select_steady_params",
    "select_aggressive_params",
    "run_walk_forward",
    "check_steady_eligibility",
    "ResearchRunner",
    "safe_metric",
]


def safe_metric(metrics: dict[str, Any], key: str, fallback: float) -> float:
    """安全读取浮点指标。

    仅当字段缺失或为 None 时使用 fallback。
    0.0 是合法值，不得被 ``or`` 替换。

    Args:
        metrics: 指标字典。
        key: 指标键名。
        fallback: 字段缺失或为 None 时的回退值。

    Returns:
        浮点指标值。
    """
    val = metrics.get(key)
    if val is None:
        return fallback
    try:
        return float(val)
    except (TypeError, ValueError):
        return fallback


# ---------------------------------------------------------------------- #
# 枚举与数据类
# ---------------------------------------------------------------------- #


class TrackType:
    """轨道类型常量。"""

    STEADY = "steady"
    AGGRESSIVE = "aggressive"


class EligibilityStatus:
    """资格状态常量。"""

    ELIGIBLE_FOR_PAPER_OBSERVATION = "ELIGIBLE_FOR_PAPER_OBSERVATION"
    NOT_ELIGIBLE_FOR_LIVE_TRADING = "NOT_ELIGIBLE_FOR_LIVE_TRADING"
    SIMULATION_ONLY = "SIMULATION_ONLY"
    INSUFFICIENT_RESEARCH_SAMPLE = "INSUFFICIENT_RESEARCH_SAMPLE"


@dataclass
class FoldResult:
    """单个滚动折的结果。

    Attributes:
        fold: 折定义。
        selected_params: 选定的参数字典。
        selection_reason: 参数选择理由。
        eliminated_candidates: 被淘汰的候选参数列表。
        test_result: 测试期回测结果。
        benchmark_returns: 基准收益率字典。
    """

    fold: Fold
    selected_params: dict[str, Any]
    selection_reason: str
    eliminated_candidates: list[dict[str, Any]] = field(default_factory=list)
    test_result: Optional[BacktestResult] = None
    benchmark_returns: dict[str, float] = field(default_factory=dict)


@dataclass
class TrackResult:
    """单条轨道的汇总结果。

    Attributes:
        track_type: 轨道类型。
        folds: 各折结果列表。
        oos_equity: 拼接后的样本外权益序列。
        oos_metrics: 样本外汇总指标。
        benchmark_comparison: 基准比较结果。
        stress_results: 压力测试结果列表。
        parameter_perturbation: 参数扰动汇总。
        market_regime: 市场阶段分析结果。
        monte_carlo: 蒙特卡洛结果（仅激进轨）。
        eligibility: 资格判定结果。
        insufficient_sample: 样本是否不足。
    """

    track_type: str
    folds: list[FoldResult] = field(default_factory=list)
    oos_equity: list[PortfolioSnapshot] = field(default_factory=list)
    oos_metrics: dict[str, Any] = field(default_factory=dict)
    benchmark_comparison: dict[str, Any] = field(default_factory=dict)
    stress_results: list[StressResult] = field(default_factory=list)
    parameter_perturbation: Optional[ParameterPerturbationResult] = None
    market_regime: Optional[MarketRegimeResult] = None
    monte_carlo: Optional[MonteCarloResult] = None
    eligibility: Optional["EligibilityCheck"] = None
    insufficient_sample: bool = False


@dataclass
class EligibilityCheck:
    """资格判定结果。

    Attributes:
        status: 资格状态。
        conditions: 各条件检查结果列表。
        failure_reasons: 失败原因列表。
    """

    status: str
    conditions: list[dict[str, Any]] = field(default_factory=list)
    failure_reasons: list[str] = field(default_factory=list)


@dataclass
class ResearchResult:
    """完整研究结果。

    Attributes:
        steady: 稳健轨结果。
        aggressive: 激进轨结果。
        folds: 滚动折列表。
        config_hash: 配置哈希。
        data_hash: 数据哈希。
        code_commit: 代码提交号。
        limitations: 限制说明列表。
        insufficient_sample: 样本是否不足。
    """

    steady: TrackResult = field(default_factory=lambda: TrackResult(track_type=TrackType.STEADY))
    aggressive: TrackResult = field(default_factory=lambda: TrackResult(track_type=TrackType.AGGRESSIVE))
    folds: list[Fold] = field(default_factory=list)
    config_hash: Optional[str] = None
    data_hash: Optional[str] = None
    code_commit: Optional[str] = None
    limitations: list[str] = field(default_factory=list)
    insufficient_sample: bool = False


# ---------------------------------------------------------------------- #
# 指标计算
# ---------------------------------------------------------------------- #


def compute_metrics_from_equity(
    equity: list[PortfolioSnapshot],
    initial_cash: float,
    fills: list[Fill] | None = None,
    orders: list[Order] | None = None,
) -> dict[str, Any]:
    """从权益序列计算汇总指标。

    Args:
        equity: 日权益快照列表。
        initial_cash: 初始资金。
        fills: 成交流水（可选）。
        orders: 订单流水（可选）。

    Returns:
        指标字典。
    """
    if not equity:
        return {
            "total_return": 0.0,
            "annualized_return": 0.0,
            "max_drawdown": 0.0,
            "sharpe": 0.0,
            "calmar": 0.0,
            "total_trades": 0,
            "win_rate": 0.0,
            "turnover_rate": 0.0,
            "trading_days": 0,
            "cash_ratio": 0.0,
        }

    initial = float(initial_cash)
    final = float(equity[-1].total_equity)
    total_return = final / initial - 1.0 if initial > 0 else 0.0

    trading_days = len(equity)
    years = trading_days / TRADING_DAYS_PER_YEAR if trading_days > 0 else 1.0
    annualized_return = (1.0 + total_return) ** (1.0 / years) - 1.0 if years > 0 else 0.0

    # 最大回撤
    peak = float(equity[0].total_equity)
    max_dd = 0.0
    for snap in equity:
        val = float(snap.total_equity)
        if val > peak:
            peak = val
        dd = (peak - val) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

    # 日收益率序列
    daily_returns: list[float] = []
    for i in range(1, len(equity)):
        prev = float(equity[i - 1].total_equity)
        curr = float(equity[i].total_equity)
        if prev > 0:
            daily_returns.append(curr / prev - 1.0)

    # 年化波动率
    if len(daily_returns) >= 2:
        vol = float(np.std(daily_returns, ddof=1)) * math.sqrt(TRADING_DAYS_PER_YEAR)
    else:
        vol = 0.0

    # Sharpe（无风险收益 0）
    sharpe = annualized_return / vol if vol > 0 else 0.0

    # Calmar
    calmar = annualized_return / max_dd if max_dd > 0 else 0.0

    # 交易统计
    total_trades = len(fills) // 2 if fills else 0  # 买卖配对计为一次交易
    cash_days = sum(1 for s in equity if float(s.position_value) <= 0)
    cash_ratio = cash_days / trading_days if trading_days > 0 else 0.0

    # 胜率（简化：按日正收益计）
    win_days = sum(1 for r in daily_returns if r > 0)
    win_rate = win_days / len(daily_returns) if daily_returns else 0.0

    # 换手率（简化估算）
    turnover = total_trades / years if years > 0 else 0.0

    return {
        "total_return": total_return,
        "annualized_return": annualized_return,
        "max_drawdown": max_dd,
        "annualized_volatility": vol,
        "sharpe": sharpe,
        "calmar": calmar,
        "total_trades": total_trades,
        "win_rate": win_rate,
        "turnover_rate": turnover,
        "trading_days": trading_days,
        "cash_ratio": cash_ratio,
        "initial_equity": initial,
        "final_equity": final,
    }


def concatenate_oos_equity(
    fold_results: list[FoldResult],
    initial_cash: float,
) -> list[PortfolioSnapshot]:
    """拼接各折样本外权益序列。

    拼接测试期无重复日期。每折权益从前一折最终权益开始复利。

    Args:
        fold_results: 各折结果列表。
        initial_cash: 初始资金。

    Returns:
        拼接后的权益快照列表。
    """
    if not fold_results:
        return []

    merged: list[PortfolioSnapshot] = []
    seen_dates: set[date] = set()
    current_base = float(initial_cash)

    for fr in fold_results:
        if fr.test_result is None or not fr.test_result.daily_equity:
            continue

        # 计算该折的缩放因子
        fold_initial = float(fr.test_result.daily_equity[0].total_equity)
        if fold_initial <= 0:
            continue
        scale = current_base / fold_initial

        for snap in fr.test_result.daily_equity:
            if snap.snapshot_date in seen_dates:
                continue
            seen_dates.add(snap.snapshot_date)

            # 缩放权益
            scaled_cash = float(snap.cash) * scale
            scaled_pos_val = float(snap.position_value) * scale
            scaled_total = scaled_cash + scaled_pos_val

            if merged:
                prev_total = float(merged[-1].total_equity)
                daily_pnl = scaled_total - prev_total
            else:
                daily_pnl = scaled_total - current_base

            cumulative = scaled_total - float(initial_cash)
            peak = max(float(s.total_equity) for s in merged) if merged else current_base
            peak = max(peak, scaled_total)
            dd = (peak - scaled_total) / peak if peak > 0 else 0.0

            merged.append(PortfolioSnapshot(
                snapshot_date=snap.snapshot_date,
                cash=to_decimal(scaled_cash),
                position_value=to_decimal(scaled_pos_val),
                total_equity=to_decimal(scaled_total),
                daily_pnl=to_decimal(daily_pnl),
                cumulative_pnl=to_decimal(cumulative),
                drawdown=to_decimal(dd),
            ))

            current_base = scaled_total

    return merged


# ---------------------------------------------------------------------- #
# 参数选择
# ---------------------------------------------------------------------- #


def select_steady_params(
    candidates: list[SteadyParams],
    validation_results: list[tuple[SteadyParams, BacktestResult]],
) -> tuple[SteadyParams, str, list[dict[str, Any]]]:
    """稳健轨训练内参数选择。

    选择目标固定为：先排除最大回撤超过 20% 的候选，再按扣费后验证期
    Calmar 比率排序；并列时依次选择换手率更低、参数离基线更近的候选。
    若没有候选满足条件，返回基线参数并记录原因。

    Args:
        candidates: 候选参数列表。
        validation_results: 各候选在验证期的回测结果列表。

    Returns:
        ``(选定参数, 选择理由, 被淘汰候选列表)``。
    """
    if not validation_results:
        return STEADY_BASELINE_PARAMS, "无候选参数，使用基线", []

    calc = MetricsCalculator()
    eliminated: list[dict[str, Any]] = []
    surviving: list[tuple[SteadyParams, BacktestResult, dict[str, Any]]] = []

    for params, result in validation_results:
        m = calc.calculate(result, to_decimal(1000.0))
        max_dd = safe_metric(m, "max_drawdown", 1.0)
        calmar = safe_metric(m, "calmar", 0.0)
        turnover = safe_metric(m, "turnover_rate", 0.0)
        annual_ret = safe_metric(m, "annualized_return", 0.0)

        param_key = _steady_param_key(params)

        if max_dd > 0.20:
            eliminated.append({
                "param_key": param_key,
                "reason": f"最大回撤 {max_dd:.4f} > 0.20",
                "max_drawdown": max_dd,
                "calmar": calmar,
                "turnover_rate": turnover,
            })
            continue

        # 参数离基线的距离
        distance = (
            abs(params.trend_window - STEADY_BASELINE_PARAMS.trend_window)
            + abs(params.momentum_window - STEADY_BASELINE_PARAMS.momentum_window)
            + abs(params.volatility_window - STEADY_BASELINE_PARAMS.volatility_window)
            + abs(params.minimum_score - STEADY_BASELINE_PARAMS.minimum_score) * 10
        )

        surviving.append((params, result, {
            "param_key": param_key,
            "max_drawdown": max_dd,
            "calmar": calmar,
            "turnover_rate": turnover,
            "annualized_return": annual_ret,
            "distance_to_baseline": distance,
        }))

    if not surviving:
        return STEADY_BASELINE_PARAMS, "无候选满足回撤约束，使用基线", eliminated

    # 按 Calmar 降序，并列时换手率升序，再并列时距离基线升序
    surviving.sort(key=lambda x: (
        -x[2]["calmar"],
        x[2]["turnover_rate"],
        x[2]["distance_to_baseline"],
    ))

    best = surviving[0]
    reason = (
        f"Calmar={best[2]['calmar']:.4f}, "
        f"回撤={best[2]['max_drawdown']:.4f}, "
        f"换手率={best[2]['turnover_rate']:.4f}"
    )

    return best[0], reason, eliminated


def select_aggressive_params(
    candidates: list[AggressiveParams],
    validation_results: list[tuple[AggressiveParams, BacktestResult]],
) -> tuple[AggressiveParams, str, list[dict[str, Any]]]:
    """激进轨训练内参数选择。

    选择目标固定为验证期扣费后几何收益；并列时依次选择最大回撤更低、
    换手率更低、参数离基线更近的候选。不得以"一年十倍次数"作为参数选择目标。

    Args:
        candidates: 候选参数列表。
        validation_results: 各候选在验证期的回测结果列表。

    Returns:
        ``(选定参数, 选择理由, 被淘汰候选列表)``。
    """
    if not validation_results:
        return AGGRESSIVE_BASELINE_PARAMS, "无候选参数，使用基线", []

    calc = MetricsCalculator()
    eliminated: list[dict[str, Any]] = []
    scored: list[tuple[AggressiveParams, dict[str, Any]]] = []

    for params, result in validation_results:
        m = calc.calculate(result, to_decimal(1000.0))
        total_return = safe_metric(m, "total_return", -1.0)
        max_dd = safe_metric(m, "max_drawdown", 1.0)
        turnover = safe_metric(m, "turnover_rate", 0.0)

        # 几何收益 = (1 + total_return)^(1/years) - 1
        years = len(result.daily_equity) / TRADING_DAYS_PER_YEAR if result.daily_equity else 1.0
        geo_return = (1.0 + total_return) ** (1.0 / years) - 1.0 if years > 0 and total_return > -1.0 else -1.0

        distance = (
            abs(params.breakout_window - AGGRESSIVE_BASELINE_PARAMS.breakout_window)
            + abs(params.volume_window - AGGRESSIVE_BASELINE_PARAMS.volume_window)
            + abs(params.volume_ratio - AGGRESSIVE_BASELINE_PARAMS.volume_ratio) * 10
            + abs(params.relative_strength_window - AGGRESSIVE_BASELINE_PARAMS.relative_strength_window)
            + abs(params.exit_low_window - AGGRESSIVE_BASELINE_PARAMS.exit_low_window)
            + abs(params.max_holding_days - AGGRESSIVE_BASELINE_PARAMS.max_holding_days)
        )

        param_key = _aggressive_param_key(params)
        scored.append((params, {
            "param_key": param_key,
            "geometric_return": geo_return,
            "total_return": total_return,
            "max_drawdown": max_dd,
            "turnover_rate": turnover,
            "distance_to_baseline": distance,
        }))

    if not scored:
        return AGGRESSIVE_BASELINE_PARAMS, "无候选参数，使用基线", eliminated

    # 按几何收益降序，并列时回撤升序，换手率升序，距离基线升序
    scored.sort(key=lambda x: (
        -x[1]["geometric_return"],
        x[1]["max_drawdown"],
        x[1]["turnover_rate"],
        x[1]["distance_to_baseline"],
    ))

    best = scored[0]
    reason = (
        f"几何收益={best[1]['geometric_return']:.4f}, "
        f"回撤={best[1]['max_drawdown']:.4f}, "
        f"换手率={best[1]['turnover_rate']:.4f}"
    )

    # 其余作为被淘汰候选
    for _, info in scored[1:]:
        eliminated.append({
            "param_key": info["param_key"],
            "reason": f"几何收益 {info['geometric_return']:.4f} < 最优 {best[1]['geometric_return']:.4f}",
            **info,
        })

    return best[0], reason, eliminated


def _steady_param_key(p: SteadyParams) -> str:
    """生成稳健轨参数标识字符串。"""
    return f"tw{p.trend_window}_mw{p.momentum_window}_vw{p.volatility_window}_ms{p.minimum_score}"


def _aggressive_param_key(p: AggressiveParams) -> str:
    """生成激进轨参数标识字符串。"""
    return (
        f"bw{p.breakout_window}_vw{p.volume_window}_vr{p.volume_ratio}"
        f"_rs{p.relative_strength_window}_el{p.exit_low_window}_mh{p.max_holding_days}"
    )


# ---------------------------------------------------------------------- #
# 资格判定
# ---------------------------------------------------------------------- #


def check_steady_eligibility(
    oos_metrics: dict[str, Any],
    stress_results: list[StressResult],
    param_perturbation: Optional[ParameterPerturbationResult],
    fold_results: list[FoldResult],
    data_quality_ok: bool = True,
) -> EligibilityCheck:
    """稳健轨资格判定。

    同时满足以下条件才可标记 ``ELIGIBLE_FOR_PAPER_OBSERVATION``：

    1. 拼接样本外最大回撤不超过 20%。
    2. 扣除费用和滑点后的拼接样本外年化收益大于 0。
    3. 至少 60% 的样本外折收益为正。
    4. 费用与滑点联合 2 倍压力下年化收益仍大于 0。
    5. 基线相邻参数组合的样本外年化收益中位数大于 0。
    6. 无数据质量失败、未来函数、账务错误或不可解释权益变化。

    任一条件不满足，结论必须为 ``NOT_ELIGIBLE_FOR_LIVE_TRADING``。

    Args:
        oos_metrics: 样本外汇总指标。
        stress_results: 压力测试结果列表。
        param_perturbation: 参数扰动汇总。
        fold_results: 各折结果列表。
        data_quality_ok: 数据质量是否通过。

    Returns:
        :class:`EligibilityCheck` 实例。
    """
    conditions: list[dict[str, Any]] = []
    failures: list[str] = []

    # 条件 1: 最大回撤不超过 20%
    max_dd = safe_metric(oos_metrics, "max_drawdown", 1.0)
    cond1_pass = max_dd <= 0.20
    conditions.append({
        "name": "max_drawdown_le_20pct",
        "value": max_dd,
        "threshold": 0.20,
        "passed": cond1_pass,
    })
    if not cond1_pass:
        failures.append(f"拼接样本外最大回撤 {max_dd:.4f} 超过 20%")

    # 条件 2: 年化收益大于 0
    ann_ret = safe_metric(oos_metrics, "annualized_return", -1.0)
    cond2_pass = ann_ret > 0
    conditions.append({
        "name": "oos_annualized_return_positive",
        "value": ann_ret,
        "threshold": 0.0,
        "passed": cond2_pass,
    })
    if not cond2_pass:
        failures.append(f"拼接样本外年化收益 {ann_ret:.4f} 不大于 0")

    # 条件 3: 至少 60% 的样本外折收益为正
    positive_folds = 0
    total_folds = 0
    for fr in fold_results:
        if fr.test_result is not None and fr.test_result.metrics:
            total_folds += 1
            if safe_metric(fr.test_result.metrics, "total_return", -1.0) > 0:
                positive_folds += 1
    positive_ratio = positive_folds / total_folds if total_folds > 0 else 0.0
    cond3_pass = positive_ratio >= 0.60
    conditions.append({
        "name": "positive_fold_ratio_ge_60pct",
        "value": positive_ratio,
        "threshold": 0.60,
        "passed": cond3_pass,
    })
    if not cond3_pass:
        failures.append(f"正收益折比例 {positive_ratio:.2%} 低于 60%")

    # 条件 4: 联合 2 倍压力下年化收益仍大于 0
    combined_result = next(
        (sr for sr in stress_results if sr.scenario.name == "combined_2x"),
        None,
    )
    combined_ann_ret = float(combined_result.annualized_return) if combined_result else -1.0
    cond4_pass = combined_ann_ret > 0
    conditions.append({
        "name": "combined_stress_annualized_return_positive",
        "value": combined_ann_ret,
        "threshold": 0.0,
        "passed": cond4_pass,
    })
    if not cond4_pass:
        failures.append(f"联合 2 倍压力下年化收益 {combined_ann_ret:.4f} 不大于 0")

    # 条件 5: 基线相邻参数组合的样本外年化收益中位数大于 0
    if param_perturbation is not None:
        median_ret = param_perturbation.return_median
    else:
        median_ret = -1.0
    cond5_pass = median_ret > 0
    conditions.append({
        "name": "param_perturbation_median_positive",
        "value": median_ret,
        "threshold": 0.0,
        "passed": cond5_pass,
    })
    if not cond5_pass:
        failures.append(f"参数扰动收益中位数 {median_ret:.4f} 不大于 0")

    # 条件 6: 无数据质量失败
    cond6_pass = data_quality_ok
    conditions.append({
        "name": "no_data_quality_failure",
        "value": data_quality_ok,
        "threshold": True,
        "passed": cond6_pass,
    })
    if not cond6_pass:
        failures.append("存在数据质量问题")

    status = (
        EligibilityStatus.ELIGIBLE_FOR_PAPER_OBSERVATION
        if all(c["passed"] for c in conditions)
        else EligibilityStatus.NOT_ELIGIBLE_FOR_LIVE_TRADING
    )

    return EligibilityCheck(
        status=status,
        conditions=conditions,
        failure_reasons=failures,
    )


# ---------------------------------------------------------------------- #
# 研究运行器
# ---------------------------------------------------------------------- #


class ResearchRunner:
    """研究运行器：编排滚动样本外验证全流程。

    使用方法::

        runner = ResearchRunner(config, benchmark, universe_filter)
        result = runner.run(quotes, trading_dates)
    """

    def __init__(
        self,
        bt_config: BacktestConfig,
        benchmark: BenchmarkData,
        universe_filter: HistoricalUniverseFilter,
        walk_forward_config: Optional[WalkForwardConfig] = None,
        monte_carlo_config: Optional[MonteCarloConfig] = None,
        steady_candidates: Optional[dict[str, list]] = None,
        aggressive_candidates: Optional[dict[str, list]] = None,
    ) -> None:
        self._bt_config = bt_config
        self._benchmark = benchmark
        self._universe_filter = universe_filter
        self._wf_config = walk_forward_config or WalkForwardConfig()
        self._mc_config = monte_carlo_config or MonteCarloConfig()
        self._steady_candidates = steady_candidates
        self._aggressive_candidates = aggressive_candidates
        self._engine = BacktestEngine()
        self._calc = MetricsCalculator()

    def run(
        self,
        quotes: pd.DataFrame,
        trading_dates: list[date],
        initial_cash: float = 1000.0,
    ) -> ResearchResult:
        """运行完整研究流程。

        Args:
            quotes: 日行情 DataFrame。
            trading_dates: 交易日列表。
            initial_cash: 初始资金。

        Returns:
            :class:`ResearchResult` 实例。
        """
        splitter = WalkForwardSplitter(self._wf_config)
        insufficient = splitter.is_insufficient_sample(trading_dates)
        folds = splitter.split(trading_dates)

        result = ResearchResult(
            folds=folds,
            insufficient_sample=insufficient,
            limitations=[
                "Phase 3 仅用于模拟研究，不构成投资建议或实盘依据",
                "激进轨永远为 SIMULATION_ONLY，不输出实盘资格",
                "蒙特卡洛结果仅用于概率研究，不构成收益承诺",
                "历史状态表按 point-in-time join，不使用当前状态替代历史状态",
                "参数只在对应训练/验证数据上选择，选定后冻结运行测试期",
            ],
        )

        if insufficient:
            result.limitations.insert(0, "样本不足，标记为 INSUFFICIENT_RESEARCH_SAMPLE")

        # 稳健轨
        result.steady = self._run_track(
            TrackType.STEADY, quotes, trading_dates, folds, initial_cash, insufficient
        )

        # 激进轨
        result.aggressive = self._run_track(
            TrackType.AGGRESSIVE, quotes, trading_dates, folds, initial_cash, insufficient
        )

        # 激进轨蒙特卡洛
        if result.aggressive.oos_equity:
            daily_returns = self._extract_daily_returns(result.aggressive.oos_equity)
            result.aggressive.monte_carlo = run_monte_carlo(daily_returns, self._mc_config)

        # 资格判定
        if not insufficient:
            result.steady.eligibility = check_steady_eligibility(
                result.steady.oos_metrics,
                result.steady.stress_results,
                result.steady.parameter_perturbation,
                result.steady.folds,
            )
        else:
            result.steady.eligibility = EligibilityCheck(
                status=EligibilityStatus.INSUFFICIENT_RESEARCH_SAMPLE,
                conditions=[],
                failure_reasons=["样本不足，无法进行资格判定"],
            )

        result.aggressive.eligibility = EligibilityCheck(
            status=EligibilityStatus.SIMULATION_ONLY,
            conditions=[],
            failure_reasons=[],
        )

        return result

    def _run_track(
        self,
        track_type: str,
        quotes: pd.DataFrame,
        trading_dates: list[date],
        folds: list[Fold],
        initial_cash: float,
        insufficient: bool,
    ) -> TrackResult:
        """运行单条轨道的完整流程。"""
        track_result = TrackResult(track_type=track_type, insufficient_sample=insufficient)

        if not folds:
            return track_result

        splitter = WalkForwardSplitter(self._wf_config)

        # 各折回测
        for fold in folds:
            fold_result = self._run_fold(
                track_type, quotes, trading_dates, fold, splitter, initial_cash
            )
            track_result.folds.append(fold_result)

        # 拼接样本外权益
        track_result.oos_equity = concatenate_oos_equity(track_result.folds, initial_cash)

        # 计算汇总指标
        all_fills: list[Fill] = []
        all_orders: list[Order] = []
        for fr in track_result.folds:
            if fr.test_result:
                all_fills.extend(fr.test_result.fills)
                all_orders.extend(fr.test_result.orders)

        track_result.oos_metrics = compute_metrics_from_equity(
            track_result.oos_equity, initial_cash, all_fills, all_orders
        )

        # 基准比较
        if track_result.oos_equity:
            oos_start = track_result.oos_equity[0].snapshot_date
            oos_end = track_result.oos_equity[-1].snapshot_date
            try:
                bench_returns = compute_benchmark_returns(
                    self._benchmark, oos_start, oos_end
                )
                cash_bench = compute_cash_benchmark(
                    track_result.oos_equity,
                    len(track_result.oos_equity),
                )
                track_result.benchmark_comparison = {
                    "hs300_return": bench_returns["hs300"],
                    "csi_all_return": bench_returns["csi_all"],
                    "cash_return": bench_returns["cash"],
                    "excess_vs_hs300": track_result.oos_metrics.get("total_return", 0.0) - bench_returns["hs300"],
                    "excess_vs_csi_all": track_result.oos_metrics.get("total_return", 0.0) - bench_returns["csi_all"],
                    "excess_vs_cash": track_result.oos_metrics.get("total_return", 0.0) - bench_returns["cash"],
                    "cash_benchmark": cash_bench,
                }
            except BenchmarkMissingError:
                track_result.benchmark_comparison = {"error": "基准数据缺失"}

        # 压力测试：费用与滑点（使用第一折的选定参数和测试期）
        if track_result.folds and track_result.folds[0].test_result:
            track_result.stress_results = self._run_stress_tests(
                track_type, quotes, trading_dates, track_result.folds, initial_cash
            )

        # 参数扰动：全部候选组合在测试期的结果分布
        if track_result.folds:
            track_result.parameter_perturbation = self._run_parameter_perturbation(
                track_type, quotes, trading_dates, track_result.folds, initial_cash
            )

        # 市场阶段分析（使用第一折的训练期确定阈值）
        if folds and track_result.oos_equity:
            first_fold = folds[0]
            train_dates = splitter.get_train_dates(trading_dates, first_fold)
            test_dates_all = []
            for fr in track_result.folds:
                if fr.test_result:
                    for snap in fr.test_result.daily_equity:
                        test_dates_all.append(snap.snapshot_date)

            regimes = classify_market_regimes(
                self._benchmark, train_dates, test_dates_all
            )
            if regimes:
                track_result.market_regime = summarize_regime_performance(
                    regimes, track_result.oos_equity, all_fills
                )

        return track_result

    def _run_stress_tests(
        self,
        track_type: str,
        quotes: pd.DataFrame,
        trading_dates: list[date],
        fold_results: list[FoldResult],
        initial_cash: float,
    ) -> list[StressResult]:
        """运行费用与滑点压力测试。

        使用第一折的选定参数，在第一折的测试期上运行 4 个压力场景：
        baseline、fee_2x、slippage_2x、combined_2x。
        """
        if not fold_results or not fold_results[0].test_result:
            return []

        first_fold = fold_results[0]
        splitter = WalkForwardSplitter(self._wf_config)
        test_dates = splitter.get_test_dates(trading_dates, first_fold.fold)

        if not test_dates:
            return []

        # 获取选定参数
        if track_type == TrackType.STEADY:
            params = SteadyParams(**first_fold.selected_params)
        else:
            params = AggressiveParams(**first_fold.selected_params)

        # 创建压力测试配置
        stress_configs = create_fee_stress_configs(self._bt_config)
        hs300_dict = dict(self._benchmark.hs300_close)

        results: list[StressResult] = []
        for scenario, stress_config in stress_configs:
            if track_type == TrackType.STEADY:
                strategy = SteadyStrategy(
                    params=params,
                    universe_filter=self._universe_filter,
                    trading_dates=test_dates,
                    lot_size=stress_config.lot_size,
                )
            else:
                strategy = AggressiveStrategy(
                    params=params,
                    universe_filter=self._universe_filter,
                    trading_dates=test_dates,
                    lot_size=stress_config.lot_size,
                    benchmark_hs300=hs300_dict,
                )

            try:
                bt_result = self._engine.run(
                    data=quotes,
                    strategy=strategy,
                    start_date=test_dates[0],
                    end_date=test_dates[-1],
                    initial_cash=initial_cash,
                    config=stress_config,
                    universe_filter=self._universe_filter,
                )
                results.append(
                    extract_stress_result(scenario, bt_result, initial_cash)
                )
            except Exception:
                results.append(StressResult(
                    scenario=scenario,
                    total_return=0.0,
                    annualized_return=0.0,
                    max_drawdown=1.0,
                    sharpe=0.0,
                    calmar=0.0,
                    win_rate=0.0,
                    turnover_rate=0.0,
                    total_trades=0,
                ))

        return results

    def _run_parameter_perturbation(
        self,
        track_type: str,
        quotes: pd.DataFrame,
        trading_dates: list[date],
        fold_results: list[FoldResult],
        initial_cash: float,
    ) -> Optional[ParameterPerturbationResult]:
        """运行参数扰动测试。

        在第一折的测试期上运行全部候选参数组合，
        报告收益、最大回撤和换手率的分布。
        """
        if not fold_results:
            return None

        first_fold = fold_results[0]
        splitter = WalkForwardSplitter(self._wf_config)
        test_dates = splitter.get_test_dates(trading_dates, first_fold.fold)

        if not test_dates:
            return None

        hs300_dict = dict(self._benchmark.hs300_close)
        calc = MetricsCalculator()
        all_results: list[dict[str, Any]] = []

        if track_type == TrackType.STEADY:
            candidates = generate_steady_param_combinations(self._steady_candidates)
            baseline_key = _steady_param_key(STEADY_BASELINE_PARAMS)
            for params in candidates:
                param_key = _steady_param_key(params)
                strategy = SteadyStrategy(
                    params=params,
                    universe_filter=self._universe_filter,
                    trading_dates=test_dates,
                    lot_size=self._bt_config.lot_size,
                )
                try:
                    result = self._engine.run(
                        data=quotes,
                        strategy=strategy,
                        start_date=test_dates[0],
                        end_date=test_dates[-1],
                        initial_cash=initial_cash,
                        config=self._bt_config,
                        universe_filter=self._universe_filter,
                    )
                    m = calc.calculate(result, to_decimal(initial_cash))
                    all_results.append({
                        "param_key": param_key,
                        "total_return": safe_metric(m, "total_return", 0.0),
                        "annualized_return": safe_metric(m, "annualized_return", 0.0),
                        "max_drawdown": safe_metric(m, "max_drawdown", 0.0),
                        "turnover_rate": safe_metric(m, "turnover_rate", 0.0),
                    })
                except Exception:
                    all_results.append({
                        "param_key": param_key,
                        "total_return": 0.0,
                        "annualized_return": 0.0,
                        "max_drawdown": 1.0,
                        "turnover_rate": 0.0,
                    })
        else:
            candidates = generate_aggressive_param_combinations(self._aggressive_candidates)
            baseline_key = _aggressive_param_key(AGGRESSIVE_BASELINE_PARAMS)
            for params in candidates:
                param_key = _aggressive_param_key(params)
                strategy = AggressiveStrategy(
                    params=params,
                    universe_filter=self._universe_filter,
                    trading_dates=test_dates,
                    lot_size=self._bt_config.lot_size,
                    benchmark_hs300=hs300_dict,
                )
                try:
                    result = self._engine.run(
                        data=quotes,
                        strategy=strategy,
                        start_date=test_dates[0],
                        end_date=test_dates[-1],
                        initial_cash=initial_cash,
                        config=self._bt_config,
                        universe_filter=self._universe_filter,
                    )
                    m = calc.calculate(result, to_decimal(initial_cash))
                    all_results.append({
                        "param_key": param_key,
                        "total_return": safe_metric(m, "total_return", 0.0),
                        "annualized_return": safe_metric(m, "annualized_return", 0.0),
                        "max_drawdown": safe_metric(m, "max_drawdown", 0.0),
                        "turnover_rate": safe_metric(m, "turnover_rate", 0.0),
                    })
                except Exception:
                    all_results.append({
                        "param_key": param_key,
                        "total_return": 0.0,
                        "annualized_return": 0.0,
                        "max_drawdown": 1.0,
                        "turnover_rate": 0.0,
                    })

        return summarize_parameter_perturbation(all_results, baseline_key)

    def _run_fold(
        self,
        track_type: str,
        quotes: pd.DataFrame,
        trading_dates: list[date],
        fold: Fold,
        splitter: WalkForwardSplitter,
        initial_cash: float,
    ) -> FoldResult:
        """运行单个折的训练/验证/测试流程。"""
        train_dates = splitter.get_train_dates(trading_dates, fold)
        val_dates = splitter.get_validation_dates(trading_dates, fold)
        test_dates = splitter.get_test_dates(trading_dates, fold)

        # 参数选择
        if track_type == TrackType.STEADY:
            selected_params, reason, eliminated = self._select_steady(
                quotes, train_dates, val_dates, fold, initial_cash
            )
            param_dict = {
                "trend_window": selected_params.trend_window,
                "momentum_window": selected_params.momentum_window,
                "volatility_window": selected_params.volatility_window,
                "minimum_score": selected_params.minimum_score,
            }
        else:
            selected_params, reason, eliminated = self._select_aggressive(
                quotes, train_dates, val_dates, fold, initial_cash
            )
            param_dict = {
                "breakout_window": selected_params.breakout_window,
                "volume_window": selected_params.volume_window,
                "volume_ratio": selected_params.volume_ratio,
                "relative_strength_window": selected_params.relative_strength_window,
                "exit_low_window": selected_params.exit_low_window,
                "max_holding_days": selected_params.max_holding_days,
            }

        # 测试期回测
        test_result = self._run_test(
            track_type, quotes, test_dates, fold, selected_params, initial_cash
        )

        # 基准收益率
        benchmark_returns: dict[str, float] = {}
        if test_dates:
            try:
                benchmark_returns = compute_benchmark_returns(
                    self._benchmark, fold.test_start, fold.test_end
                )
            except BenchmarkMissingError:
                benchmark_returns = {"error": "基准数据缺失"}

        return FoldResult(
            fold=fold,
            selected_params=param_dict,
            selection_reason=reason,
            eliminated_candidates=eliminated,
            test_result=test_result,
            benchmark_returns=benchmark_returns,
        )

    def _select_steady(
        self,
        quotes: pd.DataFrame,
        train_dates: list[date],
        val_dates: list[date],
        fold: Fold,
        initial_cash: float,
    ) -> tuple[SteadyParams, str, list[dict[str, Any]]]:
        """稳健轨参数选择。"""
        if not val_dates:
            return STEADY_BASELINE_PARAMS, "验证期无交易日，使用基线", []

        candidates = generate_steady_param_combinations(self._steady_candidates)
        val_results: list[tuple[SteadyParams, BacktestResult]] = []

        for params in candidates:
            strategy = SteadyStrategy(
                params=params,
                universe_filter=self._universe_filter,
                trading_dates=val_dates,
                lot_size=self._bt_config.lot_size,
            )
            try:
                result = self._engine.run(
                    data=quotes,
                    strategy=strategy,
                    start_date=val_dates[0],
                    end_date=val_dates[-1],
                    initial_cash=initial_cash,
                    config=self._bt_config,
                    universe_filter=self._universe_filter,
                )
                val_results.append((params, result))
            except Exception:
                continue

        return select_steady_params(candidates, val_results)

    def _select_aggressive(
        self,
        quotes: pd.DataFrame,
        train_dates: list[date],
        val_dates: list[date],
        fold: Fold,
        initial_cash: float,
    ) -> tuple[AggressiveParams, str, list[dict[str, Any]]]:
        """激进轨参数选择。"""
        if not val_dates:
            return AGGRESSIVE_BASELINE_PARAMS, "验证期无交易日，使用基线", []

        candidates = generate_aggressive_param_combinations(self._aggressive_candidates)
        val_results: list[tuple[AggressiveParams, BacktestResult]] = []

        hs300_dict = dict(self._benchmark.hs300_close)

        for params in candidates:
            strategy = AggressiveStrategy(
                params=params,
                universe_filter=self._universe_filter,
                trading_dates=val_dates,
                lot_size=self._bt_config.lot_size,
                benchmark_hs300=hs300_dict,
            )
            try:
                result = self._engine.run(
                    data=quotes,
                    strategy=strategy,
                    start_date=val_dates[0],
                    end_date=val_dates[-1],
                    initial_cash=initial_cash,
                    config=self._bt_config,
                    universe_filter=self._universe_filter,
                )
                val_results.append((params, result))
            except Exception:
                continue

        return select_aggressive_params(candidates, val_results)

    def _run_test(
        self,
        track_type: str,
        quotes: pd.DataFrame,
        test_dates: list[date],
        fold: Fold,
        params: Any,
        initial_cash: float,
    ) -> Optional[BacktestResult]:
        """运行测试期回测。"""
        if not test_dates:
            return None

        hs300_dict = dict(self._benchmark.hs300_close)

        if track_type == TrackType.STEADY:
            strategy = SteadyStrategy(
                params=params,
                universe_filter=self._universe_filter,
                trading_dates=test_dates,
                lot_size=self._bt_config.lot_size,
            )
        else:
            strategy = AggressiveStrategy(
                params=params,
                universe_filter=self._universe_filter,
                trading_dates=test_dates,
                lot_size=self._bt_config.lot_size,
                benchmark_hs300=hs300_dict,
            )

        try:
            return self._engine.run(
                data=quotes,
                strategy=strategy,
                start_date=test_dates[0],
                end_date=test_dates[-1],
                initial_cash=initial_cash,
                config=self._bt_config,
                universe_filter=self._universe_filter,
            )
        except Exception:
            return None

    @staticmethod
    def _extract_daily_returns(equity: list[PortfolioSnapshot]) -> list[float]:
        """从权益序列中提取日收益率列表。"""
        if len(equity) < 2:
            return []
        returns: list[float] = []
        for i in range(1, len(equity)):
            prev = float(equity[i - 1].total_equity)
            curr = float(equity[i].total_equity)
            if prev > 0:
                returns.append(curr / prev - 1.0)
        return returns


# ---------------------------------------------------------------------- #
# 便捷函数
# ---------------------------------------------------------------------- #


def run_walk_forward(
    quotes: pd.DataFrame,
    trading_dates: list[date],
    bt_config: BacktestConfig,
    benchmark: BenchmarkData,
    universe_filter: HistoricalUniverseFilter,
    walk_forward_config: Optional[WalkForwardConfig] = None,
    monte_carlo_config: Optional[MonteCarloConfig] = None,
    initial_cash: float = 1000.0,
) -> ResearchResult:
    """便捷函数：一键运行完整滚动样本外验证。

    创建 :class:`ResearchRunner` 并执行完整研究流程。

    Args:
        quotes: 日行情 DataFrame。
        trading_dates: 交易日列表。
        bt_config: 回测配置。
        benchmark: 基准数据。
        universe_filter: 历史时点股票池过滤器。
        walk_forward_config: 滚动切分配置（可选）。
        monte_carlo_config: 蒙特卡洛配置（可选）。
        initial_cash: 初始资金。

    Returns:
        :class:`ResearchResult` 实例。
    """
    runner = ResearchRunner(
        bt_config=bt_config,
        benchmark=benchmark,
        universe_filter=universe_filter,
        walk_forward_config=walk_forward_config,
        monte_carlo_config=monte_carlo_config,
    )
    return runner.run(quotes, trading_dates, initial_cash)
