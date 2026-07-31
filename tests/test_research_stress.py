"""Phase 3 压力测试模块 ``research.stress`` 的综合 pytest 测试。

覆盖范围（对应必需测试项）：

12. 费用、滑点、联合压力使用 Phase 2 Broker 真实复跑
    —— ``TestFeeStressBrokerReplay`` 通过真实的 ``AShareBrokerSimulator`` 逐场景
    复跑买卖，验证佣金/印花税/过户费/滑点的真实成本变化，且最低佣金不乘倍数。
13. 市场阶段阈值只来自训练期，不能使用全样本分位数
    —— ``TestMarketRegimeThresholds`` 验证波动率阈值（median / p75）仅由训练期
    确定：改变测试集不影响分类，改变训练集才会改变分类。

其余测试类覆盖：

1. ``create_fee_stress_configs``：4 个场景、费率/滑点翻倍、最低佣金不变、深拷贝隔离。
2. ``extract_stress_result``：从 ``BacktestResult.metrics`` 提取指标，缺失/None 兜底。
3. ``summarize_parameter_perturbation``：分位数、正收益比例、基线查找、空输入。
4. ``classify_market_regimes``：牛/熊/高波动分类、MA120、波动率阈值、重叠标签。
5. ``summarize_regime_performance``：各阶段收益/回撤/交易数/现金占比。
7. 边界场景：空输入、缺失基准、数据不足、neutral 阶段等。

所有测试为确定性合成数据，非真实行情。
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from ashare_quant.backtest.broker import AShareBrokerSimulator
from ashare_quant.backtest.config import BacktestConfig
from ashare_quant.backtest.models import (
    BacktestResult,
    BarData,
    Fill,
    Order,
    PortfolioSnapshot,
    Position,
    Signal,
    Side,
    to_decimal,
)
from ashare_quant.research.benchmarks import BenchmarkData, TRADING_DAYS_PER_YEAR
from ashare_quant.research.stress import (
    MarketRegime,
    MarketRegimeResult,
    ParameterPerturbationResult,
    StressResult,
    StressScenario,
    classify_market_regimes,
    create_fee_stress_configs,
    extract_stress_result,
    summarize_parameter_perturbation,
    summarize_regime_performance,
)
from tests.research_samples import make_trade_dates


# ---------------------------------------------------------------------- #
# 通用 fixture
# ---------------------------------------------------------------------- #


@pytest.fixture
def base_config() -> BacktestConfig:
    """默认回测配置（万三佣金最低5元、千一印花税、万零点一过户费、10bps 滑点）。"""
    return BacktestConfig()


# ---------------------------------------------------------------------- #
# 辅助：构造可控的 HS300 基准（用于市场阶段测试）
# ---------------------------------------------------------------------- #


def _build_regime_benchmark(
    segments: list[tuple[int, float, float]],
    start: date = date(2019, 1, 2),
) -> tuple[BenchmarkData, list[date]]:
    """从分段构造 HS300 基准。

    每段 ``(n_days, drift, amplitude)``：收益交替为 ``drift+amplitude`` 与
    ``drift-amplitude``，均值为 ``drift``，20 日实现波动率幅度为 ``amplitude``
    （vol = amplitude * sqrt(244)，与 drift 无关）。由此可精确控制每段的波动率，
    且通过 drift 正/负控制 close 相对 MA120 的位置（牛/熊）。

    Returns:
        ``(BenchmarkData, trade_dates)``，trade_dates 为排除周末的交易日列表。
    """
    returns: list[float] = []
    for n_days, drift, amp in segments:
        for i in range(n_days):
            returns.append(drift + amp if i % 2 == 0 else drift - amp)

    dates = make_trade_dates(start, len(returns))
    price = 3000.0
    hs300: dict[date, float] = {}
    csi_all: dict[date, float] = {}
    for dt, r in zip(dates, returns):
        price *= 1.0 + r
        hs300[dt] = round(price, 6)
        csi_all[dt] = round(price, 6)

    benchmark = BenchmarkData(
        trade_dates=list(dates),
        hs300_close=hs300,
        csi_all_close=csi_all,
    )
    return benchmark, dates


def _compute_realized_vol(benchmark: BenchmarkData):
    """复刻 stress.classify_market_regimes 内部的 MA120 与 20 日年化波动率计算。

    Returns:
        ``(hs300_series, ma120, realized_vol)``，均为按日期索引的 pd.Series。
    """
    all_dates = sorted(benchmark.trade_dates)
    s = pd.Series(
        [benchmark.hs300_close.get(d, np.nan) for d in all_dates],
        index=all_dates,
    ).dropna()
    ma120 = s.rolling(window=120, min_periods=120).mean()
    daily_returns = s.pct_change()
    realized_vol = (
        daily_returns.rolling(window=20, min_periods=20).std()
        * math.sqrt(TRADING_DAYS_PER_YEAR)
    )
    return s, ma120, realized_vol


def _vol_at(vol: pd.Series, d: date) -> float:
    """安全取某日波动率（NaN/不存在返回 nan）。"""
    if d in vol.index and not pd.isna(vol[d]):
        return float(vol[d])
    return float("nan")


# ---------------------------------------------------------------------- #
# 辅助：Phase 2 Broker 真实复跑所需的行情/订单/组合构造
# ---------------------------------------------------------------------- #


def _make_bar(
    open_price: float,
    prev_close: float,
    dt: date = date(2024, 1, 3),
    symbol: str = "000001",
) -> BarData:
    """构造常规 BarData，并设置 prev_close_raw 以启用涨跌停校验。"""
    return BarData(
        symbol=symbol,
        trade_date=dt,
        open_raw=to_decimal(open_price),
        high_raw=to_decimal(open_price * 1.01),
        low_raw=to_decimal(open_price * 0.99),
        close_raw=to_decimal(open_price),
        open_qfq=to_decimal(open_price),
        high_qfq=to_decimal(open_price * 1.01),
        low_qfq=to_decimal(open_price * 0.99),
        close_qfq=to_decimal(open_price),
        volume=100000.0,
        amount=open_price * 100000.0,
        is_suspended=False,
        is_tradable=True,
        prev_close_raw=to_decimal(prev_close),
    )


def _make_order(
    side: Side,
    quantity: int = 100,
    fill_date: date = date(2024, 1, 3),
    symbol: str = "000001",
) -> Order:
    return Order(
        signal=Signal(
            signal_date=fill_date - timedelta(days=1),
            symbol=symbol,
            side=side,
            quantity=quantity,
            reason="stress-test",
        ),
        planned_fill_date=fill_date,
    )


def _make_portfolio(cash: float, dt: date = date(2024, 1, 3)) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        snapshot_date=dt,
        cash=to_decimal(cash),
        position_value=Decimal("0"),
        total_equity=to_decimal(cash),
    )


def _make_position(total: int, sellable: int, symbol: str = "000001") -> Position:
    return Position(
        symbol=symbol,
        total_quantity=total,
        sellable_quantity=sellable,
    )


def _configs_by_name(base: BacktestConfig) -> dict[str, BacktestConfig]:
    """将 create_fee_stress_configs 结果转为 {场景名: 配置}。"""
    return {s.name: cfg for s, cfg in create_fee_stress_configs(base)}


# ====================================================================== #
# 1. create_fee_stress_configs
# ====================================================================== #


class TestCreateFeeStressConfigs:
    """费用/滑点压力场景配置生成。"""

    def test_creates_four_scenarios_with_expected_names(self, base_config):
        configs = create_fee_stress_configs(base_config)
        names = [s.name for s, _ in configs]
        assert names == ["baseline", "fee_2x", "slippage_2x", "combined_2x"]
        # 场景倍数正确
        by_name = {s.name: s for s, _ in configs}
        assert by_name["baseline"].fee_multiplier == 1.0
        assert by_name["baseline"].slippage_multiplier == 1.0
        assert by_name["fee_2x"].fee_multiplier == 2.0
        assert by_name["fee_2x"].slippage_multiplier == 1.0
        assert by_name["slippage_2x"].fee_multiplier == 1.0
        assert by_name["slippage_2x"].slippage_multiplier == 2.0
        assert by_name["combined_2x"].fee_multiplier == 2.0
        assert by_name["combined_2x"].slippage_multiplier == 2.0

    def test_baseline_config_equals_base_config(self, base_config):
        configs = create_fee_stress_configs(base_config)
        _, baseline_cfg = configs[0]
        assert baseline_cfg.commission.rate == base_config.commission.rate
        assert baseline_cfg.commission.minimum == base_config.commission.minimum
        assert baseline_cfg.stamp_duty.rate == base_config.stamp_duty.rate
        assert baseline_cfg.transfer_fee.rate == base_config.transfer_fee.rate
        assert baseline_cfg.slippage.bps == base_config.slippage.bps

    def test_fee_2x_doubles_rates_but_not_minimum(self, base_config):
        cfgs = _configs_by_name(base_config)
        fee_2x = cfgs["fee_2x"]
        assert fee_2x.commission.rate == pytest.approx(base_config.commission.rate * 2.0)
        assert fee_2x.stamp_duty.rate == pytest.approx(base_config.stamp_duty.rate * 2.0)
        assert fee_2x.transfer_fee.rate == pytest.approx(
            base_config.transfer_fee.rate * 2.0
        )
        # 最低佣金不翻倍，保持 Phase 2 规则
        assert fee_2x.commission.minimum == base_config.commission.minimum
        # 滑点不变
        assert fee_2x.slippage.bps == base_config.slippage.bps

    def test_slippage_2x_doubles_bps_but_not_tick(self, base_config):
        cfgs = _configs_by_name(base_config)
        slip_2x = cfgs["slippage_2x"]
        assert slip_2x.slippage.bps == pytest.approx(base_config.slippage.bps * 2.0)
        # tick_size 不变
        assert slip_2x.slippage.tick_size == base_config.slippage.tick_size
        # 费率不变
        assert slip_2x.commission.rate == base_config.commission.rate
        assert slip_2x.stamp_duty.rate == base_config.stamp_duty.rate
        assert slip_2x.transfer_fee.rate == base_config.transfer_fee.rate

    def test_combined_2x_doubles_both_fee_and_slippage(self, base_config):
        cfgs = _configs_by_name(base_config)
        combined = cfgs["combined_2x"]
        assert combined.commission.rate == pytest.approx(
            base_config.commission.rate * 2.0
        )
        assert combined.stamp_duty.rate == pytest.approx(
            base_config.stamp_duty.rate * 2.0
        )
        assert combined.transfer_fee.rate == pytest.approx(
            base_config.transfer_fee.rate * 2.0
        )
        assert combined.slippage.bps == pytest.approx(base_config.slippage.bps * 2.0)
        # 最低佣金仍不翻倍
        assert combined.commission.minimum == base_config.commission.minimum

    def test_base_config_not_mutated(self, base_config):
        """生成压力配置后，原始 base_config 不应被修改（深拷贝隔离）。"""
        original_commission_rate = base_config.commission.rate
        original_bps = base_config.slippage.bps
        create_fee_stress_configs(base_config)
        assert base_config.commission.rate == original_commission_rate
        assert base_config.slippage.bps == original_bps
        # 各场景配置之间也应相互独立
        cfgs = _configs_by_name(base_config)
        cfgs["fee_2x"].commission.rate = 0.999
        assert cfgs["combined_2x"].commission.rate != 0.999


# ====================================================================== #
# 2. extract_stress_result
# ====================================================================== #


class TestExtractStressResult:
    """从 BacktestResult 提取压力测试指标。"""

    def test_extract_all_metrics(self):
        scenario = StressScenario("baseline", 1.0, 1.0)
        metrics = {
            "total_return": Decimal("0.15"),
            "annualized_return": 0.30,
            "max_drawdown": 0.10,
            "sharpe": 1.2,
            "calmar": 3.0,
            "win_rate": 0.6,
            "turnover_rate": 0.5,
            "total_trades": 12,
        }
        result = BacktestResult(config_summary={}, metrics=metrics)
        sr = extract_stress_result(scenario, result, initial_cash=1000.0)
        assert sr.scenario is scenario
        assert sr.total_return == pytest.approx(0.15)
        assert sr.annualized_return == pytest.approx(0.30)
        assert sr.max_drawdown == pytest.approx(0.10)
        assert sr.sharpe == pytest.approx(1.2)
        assert sr.calmar == pytest.approx(3.0)
        assert sr.win_rate == pytest.approx(0.6)
        assert sr.turnover_rate == pytest.approx(0.5)
        assert sr.total_trades == 12

    def test_extract_missing_metrics_default_to_zero(self):
        scenario = StressScenario("baseline", 1.0, 1.0)
        result = BacktestResult(config_summary={}, metrics={"total_return": 0.05})
        sr = extract_stress_result(scenario, result, initial_cash=1000.0)
        assert sr.total_return == pytest.approx(0.05)
        assert sr.annualized_return == 0.0
        assert sr.max_drawdown == 0.0
        assert sr.sharpe == 0.0
        assert sr.calmar == 0.0
        assert sr.win_rate == 0.0
        assert sr.turnover_rate == 0.0
        assert sr.total_trades == 0

    def test_extract_none_metrics_default_to_zero(self):
        scenario = StressScenario("baseline", 1.0, 1.0)
        # None 值应被 `or 0.0` 兜底为 0
        metrics = {
            "total_return": None,
            "annualized_return": None,
            "max_drawdown": None,
            "sharpe": None,
            "calmar": None,
            "win_rate": None,
            "turnover_rate": None,
            "total_trades": None,
        }
        result = BacktestResult(config_summary={}, metrics=metrics)
        sr = extract_stress_result(scenario, result, initial_cash=1000.0)
        assert sr.total_return == 0.0
        assert sr.sharpe == 0.0
        assert sr.total_trades == 0
        assert isinstance(sr.total_trades, int)

    def test_extract_empty_metrics_and_scenario_preserved(self):
        scenario = StressScenario("fee_2x", 2.0, 1.0, description="双倍费用")
        result = BacktestResult(config_summary={}, metrics={})
        sr = extract_stress_result(scenario, result, initial_cash=1000.0)
        assert sr.scenario.name == "fee_2x"
        assert sr.scenario.description == "双倍费用"
        assert sr.total_return == 0.0
        assert sr.total_trades == 0
        assert isinstance(sr.total_trades, int)


# ====================================================================== #
# 3. summarize_parameter_perturbation
# ====================================================================== #


class TestParameterPerturbation:
    """参数扰动汇总：分位数、正收益比例、基线查找。"""

    @staticmethod
    def _sample_results() -> list[dict]:
        return [
            {"param_key": "a", "total_return": 0.10, "max_drawdown": 0.05,
             "turnover_rate": 0.50, "annualized_return": 0.20},
            {"param_key": "b", "total_return": -0.05, "max_drawdown": 0.08,
             "turnover_rate": 0.60, "annualized_return": -0.10},
            {"param_key": "baseline", "total_return": 0.20, "max_drawdown": 0.04,
             "turnover_rate": 0.40, "annualized_return": 0.40},
            {"param_key": "c", "total_return": 0.30, "max_drawdown": 0.12,
             "turnover_rate": 0.70, "annualized_return": 0.50},
        ]

    def test_basic_stats_and_percentiles(self):
        results = self._sample_results()
        summary = summarize_parameter_perturbation(results, baseline_key="baseline")
        returns = np.array([0.10, -0.05, 0.20, 0.30])
        drawdowns = np.array([0.05, 0.08, 0.04, 0.12])
        turnovers = np.array([0.50, 0.60, 0.40, 0.70])
        assert summary.total_combinations == 4
        assert summary.return_median == pytest.approx(float(np.median(returns)))
        assert summary.return_p10 == pytest.approx(float(np.percentile(returns, 10)))
        assert summary.return_p90 == pytest.approx(float(np.percentile(returns, 90)))
        assert summary.max_drawdown_median == pytest.approx(float(np.median(drawdowns)))
        assert summary.max_drawdown_p90 == pytest.approx(
            float(np.percentile(drawdowns, 90))
        )
        assert summary.turnover_median == pytest.approx(float(np.median(turnovers)))
        assert summary.turnover_p10 == pytest.approx(float(np.percentile(turnovers, 10)))

    def test_positive_return_count_and_ratio(self):
        results = self._sample_results()
        summary = summarize_parameter_perturbation(results, baseline_key="baseline")
        # 0.10, 0.20, 0.30 > 0；-0.05 不计
        assert summary.positive_return_count == 3
        assert summary.positive_return_ratio == pytest.approx(3 / 4)

    def test_baseline_lookup(self):
        results = self._sample_results()
        summary = summarize_parameter_perturbation(results, baseline_key="baseline")
        assert summary.baseline_return == pytest.approx(0.20)
        assert summary.baseline_max_drawdown == pytest.approx(0.04)
        assert summary.baseline_turnover == pytest.approx(0.40)

    def test_missing_baseline_defaults_to_zero(self):
        results = self._sample_results()
        summary = summarize_parameter_perturbation(results, baseline_key="nonexistent")
        assert summary.baseline_return == 0.0
        assert summary.baseline_max_drawdown == 0.0
        assert summary.baseline_turnover == 0.0
        # 其它统计仍正常计算
        assert summary.total_combinations == 4
        assert summary.positive_return_count == 3

    def test_empty_results_returns_all_zeros(self):
        summary = summarize_parameter_perturbation([], baseline_key="baseline")
        assert summary.total_combinations == 0
        assert summary.positive_return_count == 0
        assert summary.positive_return_ratio == 0.0
        assert summary.return_median == 0.0
        assert summary.return_p10 == 0.0
        assert summary.return_p90 == 0.0
        assert summary.baseline_return == 0.0
        assert summary.per_combination == []

    def test_per_combination_preserves_input(self):
        results = self._sample_results()
        summary = summarize_parameter_perturbation(results, baseline_key="baseline")
        assert summary.per_combination == results
        # 返回的是同一列表内容
        assert summary.per_combination[2]["param_key"] == "baseline"


# ====================================================================== #
# 4. classify_market_regimes
# ====================================================================== #


class TestClassifyMarketRegimes:
    """市场阶段分类：牛/熊/高波动、MA120、波动率阈值。"""

    def test_no_hs300_returns_empty(self):
        bd = BenchmarkData(
            trade_dates=[date(2020, 1, 2)],
            hs300_close={},
            csi_all_close={date(2020, 1, 2): 100.0},
        )
        regimes = classify_market_regimes(bd, [date(2020, 1, 2)], [date(2020, 1, 2)])
        assert regimes == []

    def test_insufficient_data_under_120_days_returns_empty(self):
        benchmark, dates = _build_regime_benchmark([(100, 0.001, 0.002)])
        regimes = classify_market_regimes(benchmark, dates[:50], dates[50:100])
        assert regimes == []

    def test_bull_classification(self):
        # 训练期高波动 + 测试期低波动上涨 -> close > MA120 且 vol < train_median -> bull
        # 使用两段式基准避免 amp=0.0 时浮点噪声导致 vol_val 与 vol_median 不可靠比较
        benchmark, dates = _build_regime_benchmark([
            (160, 0.002, 0.003),   # 高波动上涨（训练期）
            (80, 0.002, 0.001),    # 低波动上涨（测试期）
        ])
        train_dates = dates[120:160]
        test_dates = dates[180:240]   # 低波动段深处，20日窗口完全落段
        regimes = classify_market_regimes(benchmark, train_dates, test_dates)
        assert len(regimes) == len(test_dates)
        for r in regimes:
            assert r.is_bull is True
            assert r.is_bear is False
            assert r.is_high_volatility is False
            assert r.regime == "bull"

    def test_bear_classification(self):
        # 训练期高波动 + 测试期低波动下跌 -> close < MA120 -> bear（非高波动）
        # 使用两段式基准避免 amp=0.0 时浮点噪声导致 vol > p75 误判高波动
        benchmark, dates = _build_regime_benchmark([
            (160, -0.001, 0.003),   # 高波动下跌（训练期）
            (80, -0.001, 0.001),    # 低波动下跌（测试期）
        ])
        train_dates = dates[120:160]
        test_dates = dates[180:240]   # 低波动段深处，20日窗口完全落段
        regimes = classify_market_regimes(benchmark, train_dates, test_dates)
        assert len(regimes) == len(test_dates)
        for r in regimes:
            assert r.is_bear is True
            assert r.is_bull is False
            assert r.is_high_volatility is False
            assert r.regime == "bear"

    def test_high_volatility_classification(self):
        # 低波动训练期 + 高波动上涨测试期 -> high_volatility（vol > 训练期 p75）
        benchmark, dates = _build_regime_benchmark([
            (200, 0.005, 0.002),   # 低波动、强上涨（训练期）
            (80, 0.005, 0.03),     # 高波动、强上涨（测试期）
        ])
        train_dates = dates[120:180]
        test_dates = dates[240:280]   # 高波动段深处（窗口完全落在该段）
        s, ma120, vol = _compute_realized_vol(benchmark)
        train_vols = [_vol_at(vol, d) for d in train_dates]
        train_p75 = float(np.percentile(train_vols, 75))
        regimes = classify_market_regimes(benchmark, train_dates, test_dates)
        assert len(regimes) > 0
        for r in regimes:
            assert r.is_high_volatility is True
            assert r.realized_vol_20 > train_p75
            assert "high_volatility" in r.regime

    def test_bear_and_high_volatility_overlap_and_string(self):
        # 低波动训练期 + 高波动下跌测试期 -> bear + high_volatility（可重叠）
        benchmark, dates = _build_regime_benchmark([
            (200, 0.001, 0.002),   # 低波动（训练期）
            (80, -0.02, 0.03),     # 高波动、强下跌（测试期）
        ])
        train_dates = dates[120:180]
        test_dates = dates[250:280]
        regimes = classify_market_regimes(benchmark, train_dates, test_dates)
        assert len(regimes) > 0
        for r in regimes:
            assert r.is_bear is True
            assert r.is_high_volatility is True
            assert r.regime == "bear+high_volatility"
            assert "+" in r.regime


# ====================================================================== #
# 5. TestMarketRegimeThresholds（测试项 13：阈值只来自训练期）
# ====================================================================== #


class TestMarketRegimeThresholds:
    """市场阶段阈值只来自训练期，不能使用全样本分位数。

    基准设计（三段，索引基于交易日）：
      - segment1（0-199）：低波动 amp=0.002 -> vol ≈ 0.031
      - segment2（200-259）：高波动 amp=0.03 -> vol ≈ 0.468
      - segment3（260-299）：中等波动 amp=0.012 -> vol ≈ 0.187

    训练期取自 segment1（低波动），因此 train_p75 很低；而全样本（含高波动测试期）
    的 p75 被高波动段抬高。中等波动测试日 vol 落在 (train_p75, full_p75) 之间，
    其分类结果即可揭示阈值来源。
    """

    SEGMENTS = [(200, 0.001, 0.002), (60, 0.001, 0.03), (40, 0.001, 0.012)]

    @pytest.fixture(scope="class")
    def regime_benchmark(self):
        return _build_regime_benchmark(self.SEGMENTS)

    def test_thresholds_from_train_period_only(self, regime_benchmark):
        """中波动测试日 vol 在 (train_p75, full_p75) 之间：训练期阈值判为高波动，
        全样本阈值则不会——实际判为高波动，证明阈值来自训练期。"""
        benchmark, dates = regime_benchmark
        train_dates = dates[120:180]      # segment1，低波动
        test_dates = dates[200:300]        # 含高/中波动段
        s, ma120, vol = _compute_realized_vol(benchmark)

        train_vols = [_vol_at(vol, d) for d in train_dates]
        test_vols = [_vol_at(vol, d) for d in test_dates]
        train_p75 = float(np.percentile(train_vols, 75))
        full_p75 = float(np.percentile(train_vols + test_vols, 75))
        # 训练期阈值（低）显著低于全样本阈值（高）
        assert train_p75 < full_p75

        # 取 segment3 深处一个中波动日，其 vol 落在两阈值之间
        d_mid = dates[290]
        vol_mid = _vol_at(vol, d_mid)
        assert train_p75 < vol_mid < full_p75, (train_p75, vol_mid, full_p75)

        regimes = classify_market_regimes(benchmark, train_dates, test_dates)
        r_mid = next(r for r in regimes if r.date == d_mid)
        # 训练期阈值：vol_mid > train_p75 -> 高波动
        assert r_mid.is_high_volatility is True
        # 若误用全样本阈值：vol_mid < full_p75 -> 不会判为高波动
        # 因此 True 结果证明阈值取自训练期

    def test_classification_independent_of_test_dates(self, regime_benchmark):
        """固定训练期时，同一测试日的分类不随测试集变化（因为阈值仅来自训练期）。

        若误用全样本阈值：含高波动日的测试集会抬高 p75，使该日由高波动变为非高波动；
        含低波动日的测试集则不会。两者结果不同即说明用了全样本。这里两者相同，证明
        阈值来自训练期。
        """
        benchmark, dates = regime_benchmark
        train_dates = dates[120:180]
        d_mid = dates[290]                 # 中波动日
        s, ma120, vol = _compute_realized_vol(benchmark)
        vol_mid = _vol_at(vol, d_mid)
        train_p75 = float(np.percentile([_vol_at(vol, d) for d in train_dates], 75))
        assert vol_mid > train_p75         # 训练期阈值下判为高波动

        # 测试集 A：含高波动段（会抬高全样本 p75）
        test_a = [d_mid] + dates[220:260]
        # 测试集 B：含低波动段（不会抬高全样本 p75）
        test_b = [d_mid] + dates[180:200]

        regimes_a = classify_market_regimes(benchmark, train_dates, test_a)
        regimes_b = classify_market_regimes(benchmark, train_dates, test_b)
        ra = next(r for r in regimes_a if r.date == d_mid)
        rb = next(r for r in regimes_b if r.date == d_mid)

        # 训练期阈值下，两集合中 d_mid 分类一致（均为高波动）
        assert ra.is_high_volatility is True
        assert rb.is_high_volatility is True
        assert ra.is_high_volatility == rb.is_high_volatility

        # 进一步证明：测试集 A 的全样本 p75 高于 vol_mid（即误用全样本会判非高波动）
        full_vols_a = [_vol_at(vol, d) for d in (train_dates + test_a)]
        full_p75_a = float(np.percentile(full_vols_a, 75))
        assert full_p75_a > vol_mid

    def test_changing_train_changes_threshold(self, regime_benchmark):
        """改变训练期会改变阈值，从而改变同一测试日的分类。"""
        benchmark, dates = regime_benchmark
        d_mid = dates[290]                 # 中波动日
        test_dates = [d_mid]
        s, ma120, vol = _compute_realized_vol(benchmark)
        vol_mid = _vol_at(vol, d_mid)

        # 训练期 A：低波动段 -> 低 p75 -> d_mid 为高波动
        train_a = dates[120:180]
        # 训练期 B：高波动段 -> 高 p75 -> d_mid 不为高波动
        train_b = dates[220:260]

        p75_a = float(np.percentile([_vol_at(vol, d) for d in train_a], 75))
        p75_b = float(np.percentile([_vol_at(vol, d) for d in train_b], 75))
        assert p75_a < vol_mid < p75_b

        regimes_a = classify_market_regimes(benchmark, train_a, test_dates)
        regimes_b = classify_market_regimes(benchmark, train_b, test_dates)
        ra = next(r for r in regimes_a if r.date == d_mid)
        rb = next(r for r in regimes_b if r.date == d_mid)
        assert ra.is_high_volatility is True
        assert rb.is_high_volatility is False

    def test_train_dates_without_realized_vol_returns_empty(self, regime_benchmark):
        """训练期落在 20 日波动率窗口未定义区间（前 20 日）时，无阈值可用 -> 返回空。"""
        benchmark, dates = regime_benchmark
        # 前 20 个交易日的 20 日波动率为 NaN -> train_vol_values 为空
        train_dates = dates[0:10]
        test_dates = dates[130:140]
        regimes = classify_market_regimes(benchmark, train_dates, test_dates)
        assert regimes == []

    def test_high_vol_threshold_is_train_p75_strict(self, regime_benchmark):
        """高波动测试日均满足 vol > 训练期 p75（严格大于）。"""
        benchmark, dates = regime_benchmark
        train_dates = dates[120:180]
        test_dates = dates[220:260]        # 高波动段深处
        s, ma120, vol = _compute_realized_vol(benchmark)
        train_p75 = float(
            np.percentile([_vol_at(vol, d) for d in train_dates], 75)
        )
        regimes = classify_market_regimes(benchmark, train_dates, test_dates)
        assert len(regimes) > 0
        for r in regimes:
            assert r.is_high_volatility is True
            assert r.realized_vol_20 > train_p75


# ====================================================================== #
# 6. summarize_regime_performance
# ====================================================================== #


class TestSummarizeRegimePerformance:
    """各市场阶段策略表现汇总。"""

    @staticmethod
    def _snap(dt: date, equity: float, position_value: float) -> PortfolioSnapshot:
        return PortfolioSnapshot(
            snapshot_date=dt,
            cash=to_decimal(equity - position_value),
            position_value=to_decimal(position_value),
            total_equity=to_decimal(equity),
        )

    @staticmethod
    def _regime(dt: date, bull=False, bear=False, hv=False) -> MarketRegime:
        labels = []
        if bull:
            labels.append("bull")
        if bear:
            labels.append("bear")
        if hv:
            labels.append("high_volatility")
        return MarketRegime(
            date=dt,
            regime="+".join(labels) if labels else "neutral",
            hs300_close=3000.0,
            hs300_ma120=2900.0,
            realized_vol_20=0.1,
            is_bull=bull,
            is_bear=bear,
            is_high_volatility=hv,
        )

    def test_empty_regimes_returns_default(self):
        result = summarize_regime_performance(
            [], [self._snap(date(2024, 1, 2), 1000, 0)], []
        )
        assert isinstance(result, MarketRegimeResult)
        assert result.regimes == []
        assert result.bull_return == 0.0
        assert result.bull_trades == 0

    def test_empty_daily_equity_returns_default(self):
        regimes = [self._regime(date(2024, 1, 2), bull=True)]
        result = summarize_regime_performance(regimes, [], [])
        assert result.bull_return == 0.0
        assert result.bull_trades == 0
        assert result.bull_cash_ratio == 0.0
        # regimes 仍被保留
        assert result.regimes == regimes

    def test_bull_stats_and_cash_ratio(self):
        d1, d2, d3 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)
        regimes = [
            self._regime(d1, bull=True),
            self._regime(d2, bear=True),
            self._regime(d3, bull=True),
        ]
        equity = [
            self._snap(d1, 1000, 500),   # 持仓
            self._snap(d2, 1100, 0),     # 全现金
            self._snap(d3, 1200, 700),   # 持仓
        ]
        fills = [
            {"fill_date": d1},
            {"fill_date": d1},
            {"fill_date": d3},
        ]
        result = summarize_regime_performance(regimes, equity, fills)
        # 牛市日 d1, d3：权益 [1000, 1200]，收益 0.2，交易 2+1=3，现金占比 0
        assert result.bull_return == pytest.approx(0.2)
        assert result.bull_trades == 3
        assert result.bull_cash_ratio == pytest.approx(0.0)
        # 熊市日 d2：权益 [1100]，收益 0，交易 0，现金占比 1.0
        assert result.bear_return == pytest.approx(0.0)
        assert result.bear_trades == 0
        assert result.bear_cash_ratio == pytest.approx(1.0)
        # 高波动：无 -> 0
        assert result.high_vol_return == 0.0
        assert result.high_vol_trades == 0

    def test_high_volatility_overlap_counted_separately(self):
        d1, d2 = date(2024, 1, 2), date(2024, 1, 3)
        regimes = [
            self._regime(d1, bull=True, hv=True),
            self._regime(d2, bear=True, hv=True),
        ]
        equity = [self._snap(d1, 1000, 500), self._snap(d2, 900, 400)]
        fills = [{"fill_date": d1}, {"fill_date": d2}]
        result = summarize_regime_performance(regimes, equity, fills)
        # 高波动日 d1, d2：权益 [1000, 900]，收益 -0.1，交易 2
        assert result.high_vol_return == pytest.approx(-0.1)
        assert result.high_vol_trades == 2
        # 高波动与牛/熊分别统计：牛仅 d1、熊仅 d2，单日收益均为 0
        assert result.bull_return == pytest.approx(0.0)
        assert result.bear_return == pytest.approx(0.0)

    def test_max_drawdown_calculation(self):
        d1, d2, d3 = date(2024, 1, 2), date(2024, 1, 3), date(2024, 1, 4)
        regimes = [
            self._regime(d1, bull=True),
            self._regime(d2, bull=True),
            self._regime(d3, bull=True),
        ]
        equity = [
            self._snap(d1, 1000, 500),
            self._snap(d2, 1200, 600),
            self._snap(d3, 900, 400),
        ]
        result = summarize_regime_performance(regimes, equity, [])
        # 权益 [1000, 1200, 900]，峰值 1200，回撤 (1200-900)/1200 = 0.25
        assert result.bull_max_drawdown == pytest.approx(0.25)
        assert result.bull_return == pytest.approx(900 / 1000 - 1)


# ====================================================================== #
# 7. 边界场景
# ====================================================================== #


class TestEdgeCases:
    """边界与异常输入。"""

    def test_empty_perturbation_results(self):
        summary = summarize_parameter_perturbation([], baseline_key="x")
        assert summary.total_combinations == 0
        assert summary.positive_return_ratio == 0.0
        assert summary.baseline_return == 0.0
        assert summary.per_combination == []

    def test_missing_hs300_returns_empty_regimes(self):
        bd = BenchmarkData(
            trade_dates=[date(2020, 1, 2)],
            hs300_close={},
            csi_all_close={date(2020, 1, 2): 100.0},
        )
        assert classify_market_regimes(bd, [], []) == []
        assert classify_market_regimes(bd, [date(2020, 1, 2)], [date(2020, 1, 2)]) == []

    def test_insufficient_benchmark_data_returns_empty(self):
        benchmark, dates = _build_regime_benchmark([(110, 0.001, 0.002)])
        regimes = classify_market_regimes(benchmark, dates[:50], dates[50:110])
        assert regimes == []

    def test_empty_test_dates_returns_empty_regimes(self):
        benchmark, dates = _build_regime_benchmark([(200, 0.002, 0.0)])
        regimes = classify_market_regimes(benchmark, dates[120:160], [])
        assert regimes == []

    def test_test_dates_not_in_benchmark_are_skipped(self):
        benchmark, dates = _build_regime_benchmark([(200, 0.002, 0.0)])
        train_dates = dates[120:140]
        fake = date(2099, 12, 31)   # 不在基准日历内
        test_dates = [dates[160], fake]
        regimes = classify_market_regimes(benchmark, train_dates, test_dates)
        assert len(regimes) == 1
        assert regimes[0].date == dates[160]

    def test_neutral_regime_when_no_label_applies(self):
        """close > MA120 且 median < vol <= p75 时为 neutral（无牛/熊/高波动）。"""
        # 训练期跨三种波动率幅度，使 median < p75 存在区间
        benchmark, dates = _build_regime_benchmark([
            (160, 0.005, 0.002),   # 0-159 amp 0.002
            (60, 0.005, 0.006),    # 160-219 amp 0.006
            (60, 0.005, 0.010),    # 220-279 amp 0.010
            (60, 0.005, 0.007),    # 280-339 amp 0.007（中波动）
        ])
        # 取各段窗口完全落段内的干净训练日，避开段边界的混合窗口
        train_dates = dates[120:160] + dates[179:220] + dates[239:280]
        test_dates = dates[300:340]   # amp 0.007 段深处
        s, ma120, vol = _compute_realized_vol(benchmark)
        train_vols = [_vol_at(vol, d) for d in train_dates]
        median = float(np.median(train_vols))
        p75 = float(np.percentile(train_vols, 75))
        assert median < p75           # 训练期波动率分布存在跨度

        regimes = classify_market_regimes(benchmark, train_dates, test_dates)
        neutral_found = False
        for r in regimes:
            if r.hs300_close > r.hs300_ma120 and median < r.realized_vol_20 <= p75:
                assert r.is_bull is False
                assert r.is_bear is False
                assert r.is_high_volatility is False
                assert r.regime == "neutral"
                neutral_found = True
        assert neutral_found, "应至少存在一个 neutral 阶段日"


# ====================================================================== #
# 8. TestFeeStressBrokerReplay（测试项 12：Phase 2 Broker 真实复跑）
# ====================================================================== #


class TestFeeStressBrokerReplay:
    """费用/滑点/联合压力通过 Phase 2 Broker（AShareBrokerSimulator）真实复跑。

    不对最终收益乘折扣，而是用真实成交模拟器逐场景撮合买卖，验证费用与滑点的
    真实成本变化，且最低佣金按 Phase 2 规则保持不变（不乘倍数）。
    """

    @staticmethod
    def _buy_fill(config: BacktestConfig) -> Fill:
        broker = AShareBrokerSimulator()
        order = _make_order(Side.BUY, quantity=100)
        bar = _make_bar(open_price=100.0, prev_close=95.0)
        portfolio = _make_portfolio(cash=20000.0)
        return broker.execute(order, bar, portfolio, config, {})

    @staticmethod
    def _sell_fill(config: BacktestConfig) -> Fill:
        broker = AShareBrokerSimulator()
        order = _make_order(Side.SELL, quantity=100)
        bar = _make_bar(open_price=100.0, prev_close=105.0)
        portfolio = _make_portfolio(cash=20000.0)
        positions = {"000001": _make_position(total=100, sellable=100)}
        return broker.execute(order, bar, portfolio, config, positions)

    def test_all_scenarios_replay_via_real_broker(self, base_config):
        cfgs = _configs_by_name(base_config)
        for name in ["baseline", "fee_2x", "slippage_2x", "combined_2x"]:
            buy = self._buy_fill(cfgs[name])
            sell = self._sell_fill(cfgs[name])
            assert buy is not None, f"{name} 买入未成交"
            assert sell is not None, f"{name} 卖出未成交"
            assert buy.side == Side.BUY
            assert sell.side == Side.SELL
            assert buy.quantity == 100
            assert sell.quantity == 100

    def test_fee_2x_doubles_sell_stamp_duty_and_transfer(self, base_config):
        cfgs = _configs_by_name(base_config)
        base_sell = self._sell_fill(cfgs["baseline"])
        fee2x_sell = self._sell_fill(cfgs["fee_2x"])
        # 印花税、过户费精确翻倍
        assert float(fee2x_sell.stamp_duty) == pytest.approx(
            2 * float(base_sell.stamp_duty)
        )
        assert float(fee2x_sell.transfer_fee) == pytest.approx(
            2 * float(base_sell.transfer_fee)
        )
        # 基线佣金命中最低值（成交额*费率 < 最低佣金）
        base_rate_based = float(base_sell.turnover) * base_config.commission.rate
        assert base_rate_based < 5.0
        assert float(base_sell.commission) == pytest.approx(5.0)
        # fee_2x 翻倍费率后超过最低值，按费率计（=2×基线费率计费），而非 2×最低佣金
        fee2x_rate_based = float(fee2x_sell.turnover) * (base_config.commission.rate * 2)
        assert fee2x_rate_based > 5.0
        assert float(fee2x_sell.commission) == pytest.approx(fee2x_rate_based, rel=1e-3)
        # 即 fee_2x 佣金不等于 2×基线佣金（基线用了最低值 5，2×为 10，实际约为 5.99）
        assert float(fee2x_sell.commission) < 2 * float(base_sell.commission)

    def test_fee_2x_doubles_buy_transfer_and_minimum_rule(self, base_config):
        cfgs = _configs_by_name(base_config)
        base_buy = self._buy_fill(cfgs["baseline"])
        fee2x_buy = self._buy_fill(cfgs["fee_2x"])
        # 买入过户费翻倍
        assert float(fee2x_buy.transfer_fee) == pytest.approx(
            2 * float(base_buy.transfer_fee)
        )
        # 买入无印花税
        assert float(base_buy.stamp_duty) == 0.0
        assert float(fee2x_buy.stamp_duty) == 0.0
        # 基线佣金命中最低值
        assert float(base_buy.commission) == pytest.approx(5.0)
        # fee_2x 翻倍费率后按费率计
        fee2x_rate_based = float(fee2x_buy.turnover) * (base_config.commission.rate * 2)
        assert float(fee2x_buy.commission) == pytest.approx(fee2x_rate_based, rel=1e-3)

    def test_slippage_2x_worsens_price_via_broker(self, base_config):
        cfgs = _configs_by_name(base_config)
        base_buy = self._buy_fill(cfgs["baseline"])
        slip2x_buy = self._buy_fill(cfgs["slippage_2x"])
        # 买入：滑点翻倍 -> 成交价更高（更不利）
        assert float(slip2x_buy.slippage_price) > float(base_buy.slippage_price)
        assert float(base_buy.slippage_price) == pytest.approx(100.10)
        assert float(slip2x_buy.slippage_price) == pytest.approx(100.20)
        # 滑点翻倍不改变费用
        assert float(slip2x_buy.commission) == pytest.approx(float(base_buy.commission))
        assert float(slip2x_buy.transfer_fee) == pytest.approx(
            float(base_buy.transfer_fee)
        )

        base_sell = self._sell_fill(cfgs["baseline"])
        slip2x_sell = self._sell_fill(cfgs["slippage_2x"])
        # 卖出：滑点翻倍 -> 成交价更低（更不利）
        assert float(slip2x_sell.slippage_price) < float(base_sell.slippage_price)
        assert float(base_sell.slippage_price) == pytest.approx(99.90)
        assert float(slip2x_sell.slippage_price) == pytest.approx(99.80)

    def test_combined_2x_has_both_fee_and_slippage_effects(self, base_config):
        cfgs = _configs_by_name(base_config)
        combined_buy = self._buy_fill(cfgs["combined_2x"])
        slip2x_buy = self._buy_fill(cfgs["slippage_2x"])
        fee2x_buy = self._buy_fill(cfgs["fee_2x"])
        base_buy = self._buy_fill(cfgs["baseline"])
        # 滑点与 slippage_2x 一致（翻倍）
        assert float(combined_buy.slippage_price) == pytest.approx(
            float(slip2x_buy.slippage_price)
        )
        assert float(combined_buy.slippage_price) == pytest.approx(100.20)
        # 过户费与 fee_2x 一致（翻倍）
        assert float(combined_buy.transfer_fee) == pytest.approx(
            float(fee2x_buy.transfer_fee)
        )
        assert float(combined_buy.transfer_fee) == pytest.approx(
            2 * float(base_buy.transfer_fee)
        )
        # 印花税买入仍为 0
        assert float(combined_buy.stamp_duty) == 0.0

        combined_sell = self._sell_fill(cfgs["combined_2x"])
        base_sell = self._sell_fill(cfgs["baseline"])
        fee2x_sell = self._sell_fill(cfgs["fee_2x"])
        # 卖出滑点翻倍
        assert float(combined_sell.slippage_price) == pytest.approx(99.80)
        # 印花税：费率翻倍但成交价也因滑点翻倍而降低，故不等于 2×基线印花税
        # 正确验证方式：印花税 = 成交额 × 翻倍费率
        expected_stamp = float(combined_sell.turnover) * (
            base_config.stamp_duty.rate * 2
        )
        assert float(combined_sell.stamp_duty) == pytest.approx(expected_stamp)
        # 与 fee_2x（同费率但基线滑点）对比：combined 的印花税更低（因滑点使成交价更低）
        assert float(combined_sell.stamp_duty) < float(fee2x_sell.stamp_duty)
        # 但仍大于基线印花税（费率翻倍的效应大于成交价微降）
        assert float(combined_sell.stamp_duty) > float(base_sell.stamp_duty)

    def test_minimum_commission_not_doubled_via_broker(self, base_config):
        """小成交额下佣金命中最低值：fee_2x 与基线佣金相同（最低值未翻倍）。"""
        cfgs = _configs_by_name(base_config)
        broker = AShareBrokerSimulator()
        order = _make_order(Side.BUY, quantity=100)
        bar = _make_bar(open_price=5.0, prev_close=4.6)   # 小成交额
        portfolio = _make_portfolio(cash=2000.0)

        base_fill = broker.execute(order, bar, portfolio, cfgs["baseline"], {})
        fee2x_fill = broker.execute(order, bar, portfolio, cfgs["fee_2x"], {})
        assert base_fill is not None
        assert fee2x_fill is not None
        # 两者佣金均命中最低值 5.0（若最低值翻倍为 10，fee_2x 应为 10）
        assert float(base_fill.commission) == pytest.approx(5.0)
        assert float(fee2x_fill.commission) == pytest.approx(5.0)
        # 配置层面：最低佣金确实未被修改
        assert cfgs["fee_2x"].commission.minimum == cfgs["baseline"].commission.minimum
        assert cfgs["fee_2x"].commission.minimum == 5.0
        # 但费率确已翻倍
        assert cfgs["fee_2x"].commission.rate == pytest.approx(
            2 * cfgs["baseline"].commission.rate
        )
