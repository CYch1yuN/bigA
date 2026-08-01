"""Phase 3 策略研究包。

核心模块：
- features: 仅使用截至信号日数据的特征计算
- universe: 历史时点（point-in-time）股票池过滤
- strategies: 稳健轨、激进轨策略
- benchmarks: 沪深300、中证全指、现金基准
- walk_forward: 滚动训练/验证/测试切分
- stress: 费用、滑点、参数扰动、市场阶段
- monte_carlo: 激进轨概率分析
- analysis: 汇总指标和资格判定
- report: JSON、Markdown、Parquet 报告
"""
from __future__ import annotations

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
from .universe import HistoricalUniverseFilter, HistoricalStatusTable
from .strategies import (
    SteadyParams,
    AggressiveParams,
    SteadyStrategy,
    AggressiveStrategy,
    STEADY_PARAM_CANDIDATES,
    AGGRESSIVE_PARAM_CANDIDATES,
    STEADY_BASELINE_PARAMS,
    AGGRESSIVE_BASELINE_PARAMS,
    generate_steady_param_combinations,
    generate_aggressive_param_combinations,
)
from .benchmarks import (
    BenchmarkData,
    BenchmarkMissingError,
    load_benchmarks,
    compute_benchmark_returns,
    compute_cash_benchmark,
)
from .walk_forward import (
    Fold,
    WalkForwardConfig,
    WalkForwardSplitter,
)
from .stress import (
    StressScenario,
    StressResult,
    ParameterPerturbationResult,
    MarketRegime,
    MarketRegimeResult,
    create_fee_stress_configs,
    extract_stress_result,
    summarize_parameter_perturbation,
    classify_market_regimes,
    summarize_regime_performance,
)
from .monte_carlo import (
    MonteCarloConfig,
    MonteCarloResult,
    run_monte_carlo,
)
from .analysis import (
    TrackType,
    FoldResult,
    TrackResult,
    ResearchResult,
    EligibilityStatus,
    EligibilityCheck,
    compute_metrics_from_equity,
    concatenate_oos_equity,
    select_steady_params,
    select_aggressive_params,
    run_walk_forward,
    check_steady_eligibility,
    ResearchRunner,
)
from .report import (
    ResearchReportGenerator,
    compute_config_hash,
    compute_data_hash,
)

__all__ = [
    # features
    "compute_moving_average",
    "compute_momentum",
    "compute_volatility",
    "compute_breakout",
    "compute_volume_ratio",
    "compute_relative_strength",
    "zscore_cross_sectional",
    "compute_trend_score",
    "compute_steady_score",
    # universe
    "HistoricalUniverseFilter",
    "HistoricalStatusTable",
    # strategies
    "SteadyParams",
    "AggressiveParams",
    "SteadyStrategy",
    "AggressiveStrategy",
    "STEADY_PARAM_CANDIDATES",
    "AGGRESSIVE_PARAM_CANDIDATES",
    "STEADY_BASELINE_PARAMS",
    "AGGRESSIVE_BASELINE_PARAMS",
    "generate_steady_param_combinations",
    "generate_aggressive_param_combinations",
    # benchmarks
    "BenchmarkData",
    "BenchmarkMissingError",
    "load_benchmarks",
    "compute_benchmark_returns",
    "compute_cash_benchmark",
    # walk_forward
    "Fold",
    "WalkForwardConfig",
    "WalkForwardSplitter",
    # stress
    "StressScenario",
    "StressResult",
    "ParameterPerturbationResult",
    "MarketRegime",
    "MarketRegimeResult",
    "create_fee_stress_configs",
    "extract_stress_result",
    "summarize_parameter_perturbation",
    "classify_market_regimes",
    "summarize_regime_performance",
    # monte_carlo
    "MonteCarloConfig",
    "MonteCarloResult",
    "run_monte_carlo",
    # analysis
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
    # report
    "ResearchReportGenerator",
    "compute_config_hash",
    "compute_data_hash",
]
