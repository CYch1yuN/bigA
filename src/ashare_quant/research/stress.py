"""压力测试模块：费用、滑点、参数扰动和市场阶段分析。

每条轨道、每个样本外折至少运行：

- **费用与滑点**：基线、佣金/印花税/过户费 2 倍、滑点 2 倍、联合 2 倍。
- **参数扰动**：全部固定候选集合的样本外结果分布；基线相邻组合。
- **市场阶段**：按沪深 300 的 MA120 和 20 日年化波动率划分牛/熊/高波动。

最低佣金仍按 Phase 2 规则执行，不能简单把最终收益乘折扣。
市场阶段阈值只能由对应训练期确定，禁止使用全样本分位数。
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
from ..backtest.models import BacktestResult, PortfolioSnapshot
from .benchmarks import BenchmarkData, TRADING_DAYS_PER_YEAR

__all__ = [
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
]


# ---------------------------------------------------------------------- #
# 费用与滑点压力测试
# ---------------------------------------------------------------------- #


@dataclass
class StressScenario:
    """单个压力测试场景。

    Attributes:
        name: 场景名称（如 ``"baseline"``、``"fee_2x"``）。
        fee_multiplier: 费用倍数（1.0 = 基线，2.0 = 双倍）。
        slippage_multiplier: 滑点倍数。
        description: 人类可读描述。
    """

    name: str
    fee_multiplier: float
    slippage_multiplier: float
    description: str = ""


@dataclass
class StressResult:
    """单个压力场景的结果。

    Attributes:
        scenario: 场景定义。
        total_return: 总收益率。
        annualized_return: 年化收益率。
        max_drawdown: 最大回撤。
        sharpe: Sharpe 比率（无风险收益 0）。
        calmar: Calmar 比率。
        win_rate: 胜率。
        turnover_rate: 换手率。
        total_trades: 交易次数。
    """

    scenario: StressScenario
    total_return: float
    annualized_return: float
    max_drawdown: float
    sharpe: float
    calmar: float
    win_rate: float
    turnover_rate: float
    total_trades: int


def create_fee_stress_configs(
    base_config: BacktestConfig,
) -> list[tuple[StressScenario, BacktestConfig]]:
    """创建费用与滑点压力测试配置列表。

    生成 4 个场景：
    1. baseline：基线费用和滑点。
    2. fee_2x：佣金、印花税、过户费均为基线 2 倍。
    3. slippage_2x：滑点为基线 2 倍。
    4. combined_2x：费用 2 倍且滑点 2 倍的联合压力。

    最低佣金仍按 Phase 2 规则执行（不乘倍数，保持 minimum 不变）。

    Args:
        base_config: 基线回测配置。

    Returns:
        ``(StressScenario, BacktestConfig)`` 列表。
    """
    scenarios: list[tuple[StressScenario, BacktestConfig]] = []

    # 1. 基线
    scenarios.append((
        StressScenario(
            name="baseline",
            fee_multiplier=1.0,
            slippage_multiplier=1.0,
            description="基线费用和滑点",
        ),
        base_config.model_copy(deep=True),
    ))

    # 2. 费用 2 倍（佣金率、印花税率、过户费率翻倍，最低佣金不变）
    fee_2x = base_config.model_copy(deep=True)
    fee_2x.commission.rate = base_config.commission.rate * 2.0
    # 最低佣金不翻倍，保持 Phase 2 规则
    fee_2x.stamp_duty.rate = base_config.stamp_duty.rate * 2.0
    fee_2x.transfer_fee.rate = base_config.transfer_fee.rate * 2.0
    scenarios.append((
        StressScenario(
            name="fee_2x",
            fee_multiplier=2.0,
            slippage_multiplier=1.0,
            description="佣金、印花税、过户费均为基线 2 倍",
        ),
        fee_2x,
    ))

    # 3. 滑点 2 倍
    slip_2x = base_config.model_copy(deep=True)
    slip_2x.slippage.bps = base_config.slippage.bps * 2.0
    scenarios.append((
        StressScenario(
            name="slippage_2x",
            fee_multiplier=1.0,
            slippage_multiplier=2.0,
            description="滑点为基线 2 倍",
        ),
        slip_2x,
    ))

    # 4. 联合 2 倍
    combined = base_config.model_copy(deep=True)
    combined.commission.rate = base_config.commission.rate * 2.0
    combined.stamp_duty.rate = base_config.stamp_duty.rate * 2.0
    combined.transfer_fee.rate = base_config.transfer_fee.rate * 2.0
    combined.slippage.bps = base_config.slippage.bps * 2.0
    scenarios.append((
        StressScenario(
            name="combined_2x",
            fee_multiplier=2.0,
            slippage_multiplier=2.0,
            description="费用 2 倍且滑点 2 倍的联合压力",
        ),
        combined,
    ))

    return scenarios


def extract_stress_result(
    scenario: StressScenario,
    result: BacktestResult,
    initial_cash: float,
) -> StressResult:
    """从回测结果中提取压力测试指标。

    Args:
        scenario: 压力场景定义。
        result: 回测结果。
        initial_cash: 初始资金。

    Returns:
        :class:`StressResult` 实例。
    """
    m = result.metrics
    return StressResult(
        scenario=scenario,
        total_return=float(m.get("total_return", 0.0) or 0.0),
        annualized_return=float(m.get("annualized_return", 0.0) or 0.0),
        max_drawdown=float(m.get("max_drawdown", 0.0) or 0.0),
        sharpe=float(m.get("sharpe", 0.0) or 0.0),
        calmar=float(m.get("calmar", 0.0) or 0.0),
        win_rate=float(m.get("win_rate", 0.0) or 0.0),
        turnover_rate=float(m.get("turnover_rate", 0.0) or 0.0),
        total_trades=int(m.get("total_trades", 0) or 0),
    )


# ---------------------------------------------------------------------- #
# 参数扰动
# ---------------------------------------------------------------------- #


@dataclass
class ParameterPerturbationResult:
    """参数扰动汇总结果。

    Attributes:
        total_combinations: 总参数组合数。
        positive_return_count: 正收益组合数。
        positive_return_ratio: 正收益组合比例。
        return_median: 收益中位数。
        return_p10: 收益 10 分位数。
        return_p90: 收益 90 分位数。
        max_drawdown_median: 最大回撤中位数。
        max_drawdown_p10: 最大回撤 10 分位数。
        max_drawdown_p90: 最大回撤 90 分位数。
        turnover_median: 换手率中位数。
        turnover_p10: 换手率 10 分位数。
        turnover_p90: 换手率 90 分位数。
        baseline_return: 基线参数的收益。
        baseline_max_drawdown: 基线参数的最大回撤。
        baseline_turnover: 基线参数的换手率。
        per_combination: 每个组合的详细结果列表。
    """

    total_combinations: int
    positive_return_count: int
    positive_return_ratio: float
    return_median: float
    return_p10: float
    return_p90: float
    max_drawdown_median: float
    max_drawdown_p10: float
    max_drawdown_p90: float
    turnover_median: float
    turnover_p10: float
    turnover_p90: float
    baseline_return: float
    baseline_max_drawdown: float
    baseline_turnover: float
    per_combination: list[dict[str, Any]] = field(default_factory=list)


def summarize_parameter_perturbation(
    results: list[dict[str, Any]],
    baseline_key: str,
) -> ParameterPerturbationResult:
    """汇总参数扰动结果。

    报告全部固定候选集合的样本外结果分布，包括收益、最大回撤和换手率的
    中位数、10/90 分位数及正收益组合比例。

    Args:
        results: 每个参数组合的结果字典列表，每个字典须含：
            - ``param_key``: 参数标识字符串。
            - ``total_return``: 总收益率。
            - ``max_drawdown``: 最大回撤。
            - ``turnover_rate``: 换手率。
            - ``annualized_return``: 年化收益率。
        baseline_key: 基线参数的 ``param_key`` 值。

    Returns:
        :class:`ParameterPerturbationResult` 实例。
    """
    if not results:
        return ParameterPerturbationResult(
            total_combinations=0,
            positive_return_count=0,
            positive_return_ratio=0.0,
            return_median=0.0,
            return_p10=0.0,
            return_p90=0.0,
            max_drawdown_median=0.0,
            max_drawdown_p10=0.0,
            max_drawdown_p90=0.0,
            turnover_median=0.0,
            turnover_p10=0.0,
            turnover_p90=0.0,
            baseline_return=0.0,
            baseline_max_drawdown=0.0,
            baseline_turnover=0.0,
            per_combination=[],
        )

    returns = np.array([r.get("total_return", 0.0) for r in results], dtype=float)
    drawdowns = np.array([r.get("max_drawdown", 0.0) for r in results], dtype=float)
    turnovers = np.array([r.get("turnover_rate", 0.0) for r in results], dtype=float)

    positive_count = int(np.sum(returns > 0))
    total = len(results)

    # 查找基线结果
    baseline_result = next(
        (r for r in results if r.get("param_key") == baseline_key), None
    )

    return ParameterPerturbationResult(
        total_combinations=total,
        positive_return_count=positive_count,
        positive_return_ratio=positive_count / total if total > 0 else 0.0,
        return_median=float(np.median(returns)),
        return_p10=float(np.percentile(returns, 10)),
        return_p90=float(np.percentile(returns, 90)),
        max_drawdown_median=float(np.median(drawdowns)),
        max_drawdown_p10=float(np.percentile(drawdowns, 10)),
        max_drawdown_p90=float(np.percentile(drawdowns, 90)),
        turnover_median=float(np.median(turnovers)),
        turnover_p10=float(np.percentile(turnovers, 10)),
        turnover_p90=float(np.percentile(turnovers, 90)),
        baseline_return=float(baseline_result.get("total_return", 0.0)) if baseline_result else 0.0,
        baseline_max_drawdown=float(baseline_result.get("max_drawdown", 0.0)) if baseline_result else 0.0,
        baseline_turnover=float(baseline_result.get("turnover_rate", 0.0)) if baseline_result else 0.0,
        per_combination=list(results),
    )


# ---------------------------------------------------------------------- #
# 市场阶段分析
# ---------------------------------------------------------------------- #


@dataclass
class MarketRegime:
    """市场阶段分类结果。

    高波动可与牛/熊重叠。

    Attributes:
        date: 交易日。
        regime: 阶段标签（``"bull"``、``"bear"``、``"high_volatility"`` 或
            组合如 ``"bull+high_volatility"``）。
        hs300_close: 沪深300收盘价。
        hs300_ma120: 沪深300的120日均线值。
        realized_vol_20: 20日年化实现波动率。
        is_bull: 是否为牛市。
        is_bear: 是否为熊市。
        is_high_volatility: 是否为高波动。
    """

    date: date
    regime: str
    hs300_close: float
    hs300_ma120: float
    realized_vol_20: float
    is_bull: bool
    is_bear: bool
    is_high_volatility: bool


@dataclass
class MarketRegimeResult:
    """市场阶段策略表现汇总。

    Attributes:
        regimes: 每日阶段分类列表。
        bull_return: 牛市期间策略总收益。
        bull_max_drawdown: 牛市期间最大回撤。
        bull_trades: 牛市期间交易次数。
        bull_cash_ratio: 牛市期间现金占比。
        bear_return: 熊市期间策略总收益。
        bear_max_drawdown: 熊市期间最大回撤。
        bear_trades: 熊市期间交易次数。
        bear_cash_ratio: 熊市期间现金占比。
        high_vol_return: 高波动期间策略总收益。
        high_vol_max_drawdown: 高波动期间最大回撤。
        high_vol_trades: 高波动期间交易次数。
        high_vol_cash_ratio: 高波动期间现金占比。
    """

    regimes: list[MarketRegime] = field(default_factory=list)
    bull_return: float = 0.0
    bull_max_drawdown: float = 0.0
    bull_trades: int = 0
    bull_cash_ratio: float = 0.0
    bear_return: float = 0.0
    bear_max_drawdown: float = 0.0
    bear_trades: int = 0
    bear_cash_ratio: float = 0.0
    high_vol_return: float = 0.0
    high_vol_max_drawdown: float = 0.0
    high_vol_trades: int = 0
    high_vol_cash_ratio: float = 0.0


def classify_market_regimes(
    benchmark: BenchmarkData,
    train_dates: list[date],
    test_dates: list[date],
) -> list[MarketRegime]:
    """按沪深300的MA120和20日年化波动率划分市场阶段。

    使用沪深300的120日均线和20日实现波动率，按每个测试日当时可见数据划分：

    - 牛市：指数收盘高于 MA120，且 20 日年化波动率不高于训练期中位数。
    - 熊市：指数收盘低于 MA120。
    - 高波动：20 日年化波动率高于训练期 75 分位数。

    高波动可与牛/熊重叠。阶段阈值只能由对应训练期确定。

    Args:
        benchmark: 基准数据。
        train_dates: 训练期交易日列表（用于确定阈值）。
        test_dates: 测试期交易日列表（需要分类的日期）。

    Returns:
        测试期每日的 :class:`MarketRegime` 列表。
    """
    if not benchmark.has_hs300:
        return []

    # 构建沪深300收盘价序列
    all_dates = sorted(benchmark.trade_dates)
    hs300_series = pd.Series(
        [benchmark.hs300_close.get(d, np.nan) for d in all_dates],
        index=all_dates,
    ).dropna()

    if len(hs300_series) < 120:
        return []

    # 计算 MA120
    ma120 = hs300_series.rolling(window=120, min_periods=120).mean()

    # 计算 20 日年化实现波动率
    daily_returns = hs300_series.pct_change()
    realized_vol = daily_returns.rolling(window=20, min_periods=20).std() * math.sqrt(
        TRADING_DAYS_PER_YEAR
    )

    # 从训练期数据确定阈值
    train_vol_values: list[float] = []
    for d in train_dates:
        if d in realized_vol.index and not pd.isna(realized_vol[d]):
            train_vol_values.append(float(realized_vol[d]))

    if not train_vol_values:
        return []

    vol_median = float(np.median(train_vol_values))
    vol_p75 = float(np.percentile(train_vol_values, 75))

    # 对测试期每日分类
    regimes: list[MarketRegime] = []
    for d in test_dates:
        if d not in hs300_series.index:
            continue
        close = float(hs300_series[d])
        ma_val = float(ma120[d]) if d in ma120.index and not pd.isna(ma120[d]) else 0.0
        vol_val = float(realized_vol[d]) if d in realized_vol.index and not pd.isna(realized_vol[d]) else 0.0

        is_bull = ma_val > 0 and close > ma_val and vol_val <= vol_median
        is_bear = ma_val > 0 and close < ma_val
        is_high_vol = vol_val > vol_p75

        # 构建阶段标签
        labels: list[str] = []
        if is_bull:
            labels.append("bull")
        if is_bear:
            labels.append("bear")
        if is_high_vol:
            labels.append("high_volatility")
        regime_str = "+".join(labels) if labels else "neutral"

        regimes.append(MarketRegime(
            date=d,
            regime=regime_str,
            hs300_close=close,
            hs300_ma120=ma_val,
            realized_vol_20=vol_val,
            is_bull=is_bull,
            is_bear=is_bear,
            is_high_volatility=is_high_vol,
        ))

    return regimes


def summarize_regime_performance(
    regimes: list[MarketRegime],
    daily_equity: list[PortfolioSnapshot],
    fills: list[Any],
) -> MarketRegimeResult:
    """汇总各市场阶段的策略表现。

    Args:
        regimes: 每日市场阶段分类列表。
        daily_equity: 日权益快照列表。
        fills: 成交流水列表。

    Returns:
        :class:`MarketRegimeResult` 实例。
    """
    if not regimes or not daily_equity:
        return MarketRegimeResult(regimes=regimes)

    # 构建日期到权益的映射
    equity_by_date: dict[date, PortfolioSnapshot] = {}
    for snap in daily_equity:
        equity_by_date[snap.snapshot_date] = snap

    # 构建日期到成交数的映射
    fills_by_date: dict[date, int] = {}
    for f in fills:
        d = f.fill_date if hasattr(f, "fill_date") else f.get("fill_date")
        if d is not None:
            fills_by_date[d] = fills_by_date.get(d, 0) + 1

    def _regime_stats(
        regime_filter: callable,
    ) -> tuple[float, float, int, float]:
        """计算单个阶段的统计量。"""
        matching_dates = [r.date for r in regimes if regime_filter(r)]
        if not matching_dates:
            return 0.0, 0.0, 0, 0.0

        # 收集权益序列
        equities: list[float] = []
        cash_days = 0
        total_days = 0
        trades = 0

        for d in matching_dates:
            snap = equity_by_date.get(d)
            if snap is None:
                continue
            total_days += 1
            equities.append(float(snap.total_equity))
            if float(snap.position_value) <= 0:
                cash_days += 1
            trades += fills_by_date.get(d, 0)

        if not equities:
            return 0.0, 0.0, 0, 0.0

        # 总收益
        total_return = equities[-1] / equities[0] - 1.0 if equities[0] > 0 else 0.0

        # 最大回撤
        peak = equities[0]
        max_dd = 0.0
        for val in equities:
            if val > peak:
                peak = val
            dd = (peak - val) / peak if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd

        cash_ratio = cash_days / total_days if total_days > 0 else 0.0

        return total_return, max_dd, trades, cash_ratio

    bull_ret, bull_dd, bull_trades, bull_cash = _regime_stats(lambda r: r.is_bull)
    bear_ret, bear_dd, bear_trades, bear_cash = _regime_stats(lambda r: r.is_bear)
    hv_ret, hv_dd, hv_trades, hv_cash = _regime_stats(lambda r: r.is_high_volatility)

    return MarketRegimeResult(
        regimes=regimes,
        bull_return=bull_ret,
        bull_max_drawdown=bull_dd,
        bull_trades=bull_trades,
        bull_cash_ratio=bull_cash,
        bear_return=bear_ret,
        bear_max_drawdown=bear_dd,
        bear_trades=bear_trades,
        bear_cash_ratio=bear_cash,
        high_vol_return=hv_ret,
        high_vol_max_drawdown=hv_dd,
        high_vol_trades=hv_trades,
        high_vol_cash_ratio=hv_cash,
    )
