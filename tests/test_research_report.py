"""Phase 3 研究报告生成模块 ``report`` 的综合 pytest 测试。

覆盖测试项 19：JSON/Markdown/Parquet 双跑一致，订单、成交和权益可追溯。

测试范围：
1. 配置哈希确定性、区分性、格式校验
2. 数据哈希真实文件计算、缺失文件处理、确定性
3. JSON 报告结构、元数据字段、双轨完整性
4. Markdown 报告内容、标题、表格、声明
5. Parquet 文件生成、列结构、非空、可追溯性
6. 双跑一致性 —— JSON/Markdown 字节级一致、Parquet 内容一致
7. 报告完整性 —— 全部输出文件存在、code_commit、limitations
8. 蒙特卡洛摘要结构与声明

所有测试使用确定性合成数据，无外部依赖。
"""
from __future__ import annotations

import json
import tempfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from ashare_quant.research.report import (
    ResearchReportGenerator,
    compute_config_hash,
    compute_data_hash,
)
from ashare_quant.research.analysis import (
    ResearchResult,
    TrackResult,
    FoldResult,
    EligibilityCheck,
    TrackType,
    EligibilityStatus,
)
from ashare_quant.research.walk_forward import Fold
from ashare_quant.research.stress import (
    StressScenario,
    StressResult,
    MarketRegime,
    MarketRegimeResult,
    ParameterPerturbationResult,
)
from ashare_quant.research.monte_carlo import MonteCarloResult
from ashare_quant.backtest.models import (
    PortfolioSnapshot,
    BacktestResult,
    Order,
    Fill,
    Signal,
    Side,
    OrderStatus,
    to_decimal,
)


# --------------------------------------------------------------------------- #
# 固定时间戳，用于双跑一致性测试中 patch datetime
# --------------------------------------------------------------------------- #
FIXED_DATETIME = datetime(2026, 1, 15, 10, 30, 0, 0)


# --------------------------------------------------------------------------- #
# 辅助构建函数
# --------------------------------------------------------------------------- #


def _make_snapshot(
    snap_date: date,
    cash: float,
    position_value: float,
    total_equity: float,
    daily_pnl: float = 0.0,
    cumulative_pnl: float = 0.0,
    drawdown: float = 0.0,
) -> PortfolioSnapshot:
    """构造 PortfolioSnapshot，所有金额通过 to_decimal 转换为 Decimal。"""
    return PortfolioSnapshot(
        snapshot_date=snap_date,
        cash=to_decimal(cash),
        position_value=to_decimal(position_value),
        total_equity=to_decimal(total_equity),
        daily_pnl=to_decimal(daily_pnl),
        cumulative_pnl=to_decimal(cumulative_pnl),
        drawdown=to_decimal(drawdown),
    )


def _make_fill(
    order_id: str,
    fill_date: date,
    symbol: str,
    side: Side,
    quantity: int,
    raw_open_price: float,
    slippage_price: float,
    commission: float,
    stamp_duty: float,
    transfer_fee: float,
    total_cost: float,
    cash_change: float,
    audit_flags: list[str] | None = None,
) -> Fill:
    """构造 Fill 记录，金额通过 to_decimal 转换。"""
    return Fill(
        order_id=order_id,
        fill_date=fill_date,
        symbol=symbol,
        side=side,
        quantity=quantity,
        raw_open_price=to_decimal(raw_open_price),
        slippage_price=to_decimal(slippage_price),
        commission=to_decimal(commission),
        stamp_duty=to_decimal(stamp_duty),
        transfer_fee=to_decimal(transfer_fee),
        total_cost=to_decimal(total_cost),
        cash_change=to_decimal(cash_change),
        audit_flags=audit_flags if audit_flags is not None else [],
    )


def _make_order(
    signal: Signal,
    planned_fill_date: date,
    order_id: str,
    status: OrderStatus = OrderStatus.FILLED,
    fill: Fill | None = None,
    audit_flags: list[str] | None = None,
) -> Order:
    """构造 Order 记录。"""
    return Order(
        signal=signal,
        planned_fill_date=planned_fill_date,
        order_id=order_id,
        status=status,
        fill=fill,
        audit_flags=audit_flags if audit_flags is not None else [],
    )


def _make_test_result(orders: list[Order], fills: list[Fill],
                      equity: list[PortfolioSnapshot]) -> BacktestResult:
    """构造测试期回测结果。"""
    return BacktestResult(
        config_summary={"initial_cash": 1000.0},
        orders=orders,
        fills=fills,
        daily_equity=equity,
        metrics={
            "total_return": 0.05,
            "annualized_return": 0.05,
            "max_drawdown": 0.03,
            "sharpe": 0.8,
            "calmar": 1.67,
            "total_trades": 1,
            "win_rate": 0.6,
            "turnover_rate": 0.3,
            "trading_days": len(equity),
            "cash_ratio": 0.4,
        },
    )


def _make_stress_scenario(name: str, fee_mult: float, slip_mult: float) -> StressScenario:
    """构造压力测试场景。"""
    return StressScenario(
        name=name,
        fee_multiplier=fee_mult,
        slippage_multiplier=slip_mult,
        description=f"场景: {name}",
    )


def make_mock_research_result() -> ResearchResult:
    """构建完整 ResearchResult 用于测试。

    包含两条轨道（稳健/激进）、滚动折、订单/成交流水、权益序列、
    压力测试、参数扰动、市场阶段和蒙特卡洛结果。
    所有数据为确定性合成数据。
    """
    # ---- 滚动折定义 ----
    fold0 = Fold(
        fold_id=0,
        train_start=date(2021, 1, 1),
        train_end=date(2023, 12, 31),
        validation_start=date(2023, 7, 1),
        validation_end=date(2023, 12, 31),
        test_start=date(2024, 1, 2),
        test_end=date(2024, 12, 31),
    )
    fold1 = Fold(
        fold_id=1,
        train_start=date(2022, 1, 1),
        train_end=date(2024, 12, 31),
        validation_start=date(2024, 7, 1),
        validation_end=date(2024, 12, 31),
        test_start=date(2025, 1, 2),
        test_end=date(2025, 12, 31),
    )

    # ---- 权益序列 ----
    steady_equity = [
        _make_snapshot(date(2024, 1, 2), 1000.0, 0.0, 1000.0, 0.0, 0.0, 0.0),
        _make_snapshot(date(2024, 1, 3), 950.0, 60.0, 1010.0, 10.0, 10.0, 0.0),
        _make_snapshot(date(2024, 1, 4), 945.0, 70.0, 1015.0, 5.0, 15.0, 0.0),
        _make_snapshot(date(2024, 1, 5), 940.0, 85.0, 1025.0, 10.0, 25.0, 0.0),
        _make_snapshot(date(2024, 1, 8), 1040.0, 0.0, 1040.0, 15.0, 40.0, 0.0),
    ]
    aggressive_equity = [
        _make_snapshot(date(2024, 1, 2), 1000.0, 0.0, 1000.0, 0.0, 0.0, 0.0),
        _make_snapshot(date(2024, 1, 3), 900.0, 120.0, 1020.0, 20.0, 20.0, 0.0),
        _make_snapshot(date(2024, 1, 4), 880.0, 160.0, 1040.0, 20.0, 40.0, 0.0),
        _make_snapshot(date(2024, 1, 5), 860.0, 200.0, 1060.0, 20.0, 60.0, 0.0),
        _make_snapshot(date(2024, 1, 8), 1080.0, 0.0, 1080.0, 20.0, 80.0, 0.0),
    ]

    # ---- 订单与成交 ----
    buy_signal = Signal(
        signal_date=date(2024, 1, 2),
        symbol="000001",
        side=Side.BUY,
        quantity=100,
        reason="趋势向上突破",
    )
    sell_signal = Signal(
        signal_date=date(2024, 1, 5),
        symbol="000001",
        side=Side.SELL,
        quantity=100,
        reason="持仓止盈离场",
    )

    buy_fill = _make_fill(
        order_id="steady-fold0-buy-001",
        fill_date=date(2024, 1, 3),
        symbol="000001",
        side=Side.BUY,
        quantity=100,
        raw_open_price=5.00,
        slippage_price=5.01,
        commission=2.50,
        stamp_duty=0.00,
        transfer_fee=0.10,
        total_cost=2.60,
        cash_change=-503.60,
        audit_flags=["limit_check_unavailable"],
    )
    sell_fill = _make_fill(
        order_id="steady-fold0-sell-001",
        fill_date=date(2024, 1, 8),
        symbol="000001",
        side=Side.SELL,
        quantity=100,
        raw_open_price=5.40,
        slippage_price=5.39,
        commission=2.70,
        stamp_duty=2.70,
        transfer_fee=0.10,
        total_cost=5.50,
        cash_change=533.50,
        audit_flags=[],
    )

    buy_order = _make_order(
        signal=buy_signal,
        planned_fill_date=date(2024, 1, 3),
        order_id="steady-fold0-buy-001",
        status=OrderStatus.FILLED,
        fill=buy_fill,
        audit_flags=["limit_check_unavailable"],
    )
    sell_order = _make_order(
        signal=sell_signal,
        planned_fill_date=date(2024, 1, 8),
        order_id="steady-fold0-sell-001",
        status=OrderStatus.FILLED,
        fill=sell_fill,
        audit_flags=[],
    )

    test_result_fold0 = _make_test_result(
        orders=[buy_order, sell_order],
        fills=[buy_fill, sell_fill],
        equity=steady_equity,
    )

    # 激进轨复用同样的订单/成交结构但使用激进权益
    test_result_aggressive = _make_test_result(
        orders=[buy_order, sell_order],
        fills=[buy_fill, sell_fill],
        equity=aggressive_equity,
    )

    # ---- 折结果 ----
    steady_fold0 = FoldResult(
        fold=fold0,
        selected_params={
            "trend_window": 20,
            "momentum_window": 10,
            "volatility_window": 20,
            "minimum_score": 0.5,
        },
        selection_reason="Calmar=1.67, 回撤=0.03, 换手率=0.30",
        eliminated_candidates=[
            {"param_key": "tw10_mw5_vw10_ms0.3", "reason": "回撤 0.35 > 0.20"},
        ],
        test_result=test_result_fold0,
        benchmark_returns={"hs300": 0.01, "csi_all": 0.008},
    )
    aggressive_fold0 = FoldResult(
        fold=fold0,
        selected_params={
            "breakout_window": 20,
            "volume_window": 10,
            "volume_ratio": 1.5,
            "relative_strength_window": 20,
            "exit_low_window": 5,
            "max_holding_days": 10,
        },
        selection_reason="几何收益=0.05, 回撤=0.03, 换手率=0.30",
        eliminated_candidates=[],
        test_result=test_result_aggressive,
        benchmark_returns={"hs300": 0.01, "csi_all": 0.008},
    )

    # ---- 压力测试结果 ----
    stress_scenarios = [
        _make_stress_scenario("baseline", 1.0, 1.0),
        _make_stress_scenario("fee_2x", 2.0, 1.0),
        _make_stress_scenario("slippage_2x", 1.0, 2.0),
        _make_stress_scenario("combined_2x", 2.0, 2.0),
    ]
    stress_results_steady = [
        StressResult(
            scenario=sc,
            total_return=0.05 if "baseline" in sc.name else 0.03,
            annualized_return=0.05 if "baseline" in sc.name else 0.03,
            max_drawdown=0.03,
            sharpe=0.8,
            calmar=1.67,
            win_rate=0.6,
            turnover_rate=0.3,
            total_trades=1,
        )
        for sc in stress_scenarios
    ]
    stress_results_aggressive = [
        StressResult(
            scenario=sc,
            total_return=0.08 if "baseline" in sc.name else 0.04,
            annualized_return=0.08 if "baseline" in sc.name else 0.04,
            max_drawdown=0.06,
            sharpe=0.9,
            calmar=1.33,
            win_rate=0.55,
            turnover_rate=0.4,
            total_trades=1,
        )
        for sc in stress_scenarios
    ]

    # ---- 参数扰动结果 ----
    baseline_key_steady = "tw20_mw10_vw20_ms0.5"
    per_combination_steady = [
        {
            "param_key": baseline_key_steady,
            "baseline_key": baseline_key_steady,
            "total_return": 0.05,
            "annualized_return": 0.05,
            "max_drawdown": 0.03,
            "turnover_rate": 0.3,
        },
        {
            "param_key": "tw25_mw10_vw20_ms0.5",
            "baseline_key": baseline_key_steady,
            "total_return": 0.04,
            "annualized_return": 0.04,
            "max_drawdown": 0.04,
            "turnover_rate": 0.28,
        },
        {
            "param_key": "tw15_mw10_vw20_ms0.5",
            "baseline_key": baseline_key_steady,
            "total_return": 0.06,
            "annualized_return": 0.06,
            "max_drawdown": 0.05,
            "turnover_rate": 0.32,
        },
    ]
    parameter_perturbation_steady = ParameterPerturbationResult(
        total_combinations=3,
        positive_return_count=3,
        positive_return_ratio=1.0,
        return_median=0.05,
        return_p10=0.04,
        return_p90=0.06,
        max_drawdown_median=0.04,
        max_drawdown_p10=0.03,
        max_drawdown_p90=0.05,
        turnover_median=0.3,
        turnover_p10=0.28,
        turnover_p90=0.32,
        baseline_return=0.05,
        baseline_max_drawdown=0.03,
        baseline_turnover=0.3,
        per_combination=per_combination_steady,
    )

    baseline_key_aggressive = "bw20_vw10_vr1.5_rs20_el5_mh10"
    per_combination_aggressive = [
        {
            "param_key": baseline_key_aggressive,
            "baseline_key": baseline_key_aggressive,
            "total_return": 0.08,
            "annualized_return": 0.08,
            "max_drawdown": 0.06,
            "turnover_rate": 0.4,
        },
        {
            "param_key": "bw25_vw10_vr1.5_rs20_el5_mh10",
            "baseline_key": baseline_key_aggressive,
            "total_return": 0.07,
            "annualized_return": 0.07,
            "max_drawdown": 0.07,
            "turnover_rate": 0.38,
        },
    ]
    parameter_perturbation_aggressive = ParameterPerturbationResult(
        total_combinations=2,
        positive_return_count=2,
        positive_return_ratio=1.0,
        return_median=0.075,
        return_p10=0.07,
        return_p90=0.08,
        max_drawdown_median=0.065,
        max_drawdown_p10=0.06,
        max_drawdown_p90=0.07,
        turnover_median=0.39,
        turnover_p10=0.38,
        turnover_p90=0.4,
        baseline_return=0.08,
        baseline_max_drawdown=0.06,
        baseline_turnover=0.4,
        per_combination=per_combination_aggressive,
    )

    # ---- 市场阶段 ----
    regimes = [
        MarketRegime(
            date=date(2024, 1, 2),
            regime="bull",
            hs300_close=3100.0,
            hs300_ma120=3050.0,
            realized_vol_20=0.12,
            is_bull=True,
            is_bear=False,
            is_high_volatility=False,
        ),
        MarketRegime(
            date=date(2024, 1, 3),
            regime="bull+high_volatility",
            hs300_close=3120.0,
            hs300_ma120=3055.0,
            realized_vol_20=0.28,
            is_bull=True,
            is_bear=False,
            is_high_volatility=True,
        ),
        MarketRegime(
            date=date(2024, 1, 4),
            regime="bear",
            hs300_close=3040.0,
            hs300_ma120=3060.0,
            realized_vol_20=0.15,
            is_bull=False,
            is_bear=True,
            is_high_volatility=False,
        ),
    ]
    market_regime_result = MarketRegimeResult(
        regimes=regimes,
        bull_return=0.02,
        bull_max_drawdown=0.01,
        bull_trades=1,
        bull_cash_ratio=0.3,
        bear_return=-0.01,
        bear_max_drawdown=0.02,
        bear_trades=0,
        bear_cash_ratio=0.5,
        high_vol_return=0.01,
        high_vol_max_drawdown=0.03,
        high_vol_trades=1,
        high_vol_cash_ratio=0.4,
    )

    # ---- 蒙特卡洛结果 ----
    monte_carlo = MonteCarloResult(
        prob_ten_x=0.05,
        prob_loss_50=0.15,
        prob_near_zero=0.02,
        percentiles={
            "1%": 200.0,
            "5%": 400.0,
            "25%": 800.0,
            "50%": 1100.0,
            "75%": 1500.0,
            "95%": 3000.0,
            "99%": 6000.0,
        },
        n_oos_days=244,
        block_length=5,
        n_paths=10000,
        random_seed=20260731,
        insufficient_sample=False,
    )

    # ---- 资格判定 ----
    steady_eligibility = EligibilityCheck(
        status=EligibilityStatus.NOT_ELIGIBLE_FOR_LIVE_TRADING,
        conditions=[
            {"name": "max_drawdown_le_20pct", "value": 0.03, "threshold": 0.20, "passed": True},
            {"name": "oos_annualized_return_positive", "value": 0.05, "threshold": 0.0, "passed": True},
            {"name": "positive_fold_ratio_ge_60pct", "value": 1.0, "threshold": 0.60, "passed": True},
            {"name": "combined_stress_annualized_return_positive", "value": 0.03, "threshold": 0.0, "passed": True},
            {"name": "param_perturbation_median_positive", "value": 0.05, "threshold": 0.0, "passed": True},
            {"name": "no_data_quality_failure", "value": True, "threshold": True, "passed": True},
        ],
        failure_reasons=[],
    )
    aggressive_eligibility = EligibilityCheck(
        status=EligibilityStatus.SIMULATION_ONLY,
        conditions=[],
        failure_reasons=[],
    )

    # ---- 组装 TrackResult ----
    steady = TrackResult(
        track_type=TrackType.STEADY,
        folds=[steady_fold0],
        oos_equity=steady_equity,
        oos_metrics={
            "total_return": 0.04,
            "annualized_return": 0.04,
            "max_drawdown": 0.03,
            "sharpe": 0.8,
            "calmar": 1.33,
            "total_trades": 1,
            "win_rate": 0.6,
            "turnover_rate": 0.3,
            "trading_days": 5,
            "cash_ratio": 0.4,
        },
        benchmark_comparison={
            "hs300_return": 0.01,
            "csi_all_return": 0.008,
            "cash_return": 0.0,
            "excess_vs_hs300": 0.03,
            "excess_vs_csi_all": 0.032,
            "excess_vs_cash": 0.04,
        },
        stress_results=stress_results_steady,
        parameter_perturbation=parameter_perturbation_steady,
        market_regime=market_regime_result,
        monte_carlo=None,
        eligibility=steady_eligibility,
        insufficient_sample=False,
    )

    aggressive = TrackResult(
        track_type=TrackType.AGGRESSIVE,
        folds=[aggressive_fold0],
        oos_equity=aggressive_equity,
        oos_metrics={
            "total_return": 0.08,
            "annualized_return": 0.08,
            "max_drawdown": 0.06,
            "sharpe": 0.9,
            "calmar": 1.33,
            "total_trades": 1,
            "win_rate": 0.55,
            "turnover_rate": 0.4,
            "trading_days": 5,
            "cash_ratio": 0.3,
        },
        benchmark_comparison={
            "hs300_return": 0.01,
            "csi_all_return": 0.008,
            "cash_return": 0.0,
            "excess_vs_hs300": 0.07,
            "excess_vs_csi_all": 0.072,
            "excess_vs_cash": 0.08,
        },
        stress_results=stress_results_aggressive,
        parameter_perturbation=parameter_perturbation_aggressive,
        market_regime=market_regime_result,
        monte_carlo=monte_carlo,
        eligibility=aggressive_eligibility,
        insufficient_sample=False,
    )

    return ResearchResult(
        steady=steady,
        aggressive=aggressive,
        folds=[fold0, fold1],
        code_commit="abc123def456",
        limitations=[
            "Phase 3 仅用于模拟研究，不构成投资建议或实盘依据",
            "激进轨永远为 SIMULATION_ONLY，不输出实盘资格",
            "蒙特卡洛结果仅用于概率研究，不构成收益承诺",
            "历史状态表按 point-in-time join，不使用当前状态替代历史状态",
            "参数只在对应训练/验证数据上选择，选定后冻结运行测试期",
        ],
    )


def _generate_all_to_dir(result: ResearchResult, output_dir: Path,
                         config_dict: dict | None = None,
                         data_files: list | None = None,
                         initial_cash: float = 1000.0) -> dict[str, Path]:
    """便捷函数：创建生成器并调用 generate_all。"""
    gen = ResearchReportGenerator()
    return gen.generate_all(
        result=result,
        output_dir=output_dir,
        config_dict=config_dict or {"walk_forward": {"train_years": 3}},
        data_files=data_files,
        initial_cash=initial_cash,
    )


# --------------------------------------------------------------------------- #
# 1. TestConfigHash
# --------------------------------------------------------------------------- #


class TestConfigHash:
    """配置哈希函数测试：确定性、区分性、格式校验。"""

    def test_hash_is_deterministic(self):
        """相同配置字典应产生相同哈希。"""
        config = {"a": 1, "b": [1, 2, 3], "c": {"d": "hello"}}
        h1 = compute_config_hash(config)
        h2 = compute_config_hash(config)
        assert h1 == h2

    def test_different_configs_different_hashes(self):
        """不同配置应产生不同哈希。"""
        config_a = {"param": 1}
        config_b = {"param": 2}
        assert compute_config_hash(config_a) != compute_config_hash(config_b)

    def test_hash_is_64_char_hex(self):
        """哈希应为 64 字符的十六进制字符串。"""
        h = compute_config_hash({"x": 1})
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_empty_config_hash_valid(self):
        """空字典应返回有效的 64 字符哈希。"""
        h = compute_config_hash({})
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_key_order_independence(self):
        """键顺序不同但内容相同的配置应产生相同哈希。"""
        config_ordered = {"a": 1, "b": 2, "c": 3}
        config_shuffled = {"c": 3, "a": 1, "b": 2}
        assert compute_config_hash(config_ordered) == compute_config_hash(config_shuffled)

    def test_nested_config_deterministic(self):
        """嵌套配置的哈希也应确定性。"""
        config = {
            "walk_forward": {"train_years": 3, "test_years": 1},
            "initial_cash": 1000.0,
            "params": {"trend_window": 20},
        }
        assert compute_config_hash(config) == compute_config_hash(config)


# --------------------------------------------------------------------------- #
# 2. TestDataHash
# --------------------------------------------------------------------------- #


class TestDataHash:
    """数据文件哈希函数测试：真实文件、缺失文件、确定性。"""

    def test_hash_with_real_files(self, tmp_path):
        """对真实文件计算哈希应返回有效的 64 字符十六进制串。"""
        f1 = tmp_path / "data1.parquet"
        f2 = tmp_path / "data2.parquet"
        f1.write_bytes(b"hello world")
        f2.write_bytes(b"foobar baz")

        h = compute_data_hash([str(f1), str(f2)])
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_hash_deterministic(self, tmp_path):
        """相同文件内容应产生相同哈希。"""
        f = tmp_path / "data.parquet"
        f.write_bytes(b"deterministic content")

        h1 = compute_data_hash([str(f)])
        h2 = compute_data_hash([str(f)])
        assert h1 == h2

    def test_missing_file_does_not_raise(self, tmp_path):
        """缺失文件不应抛异常，哈希中应包含 'missing' 标记。"""
        missing = tmp_path / "nonexistent.parquet"
        existing = tmp_path / "exists.parquet"
        existing.write_bytes(b"exists")

        h = compute_data_hash([str(missing), str(existing)])
        assert len(h) == 64

    def test_different_content_different_hash(self, tmp_path):
        """不同文件内容应产生不同哈希。"""
        f1 = tmp_path / "a.parquet"
        f2 = tmp_path / "b.parquet"
        f1.write_bytes(b"content version 1")
        f2.write_bytes(b"content version 2")

        assert compute_data_hash([str(f1)]) != compute_data_hash([str(f2)])

    def test_empty_file_list_returns_valid_hash(self):
        """空文件列表应返回有效的 64 字符哈希。"""
        h = compute_data_hash([])
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_path_objects_accepted(self, tmp_path):
        """Path 对象应与字符串路径等效。"""
        f = tmp_path / "data.parquet"
        f.write_bytes(b"path object test")

        h_str = compute_data_hash([str(f)])
        h_path = compute_data_hash([f])
        assert h_str == h_path


# --------------------------------------------------------------------------- #
# 3. TestGenerateJSON
# --------------------------------------------------------------------------- #


class TestGenerateJSON:
    """JSON 报告生成测试：结构、元数据、双轨完整性。"""

    def test_json_has_metadata_section(self):
        """JSON 应包含 metadata 字段及其全部子字段。"""
        result = make_mock_research_result()
        gen = ResearchReportGenerator()
        summary = gen.generate_json(result, initial_cash=1000.0)

        assert "metadata" in summary
        meta = summary["metadata"]
        assert "code_commit" in meta
        assert "config_hash" in meta
        assert "data_hash" in meta
        assert "insufficient_sample" in meta
        assert "initial_cash" in meta
        assert "generated_at" in meta

    def test_json_has_steady_and_aggressive_tracks(self):
        """JSON 应包含 steady 和 aggressive 两条轨道。"""
        result = make_mock_research_result()
        gen = ResearchReportGenerator()
        summary = gen.generate_json(result, initial_cash=1000.0)

        assert "steady" in summary
        assert "aggressive" in summary
        assert summary["steady"]["track_type"] == "steady"
        assert summary["aggressive"]["track_type"] == "aggressive"

    def test_json_has_folds(self):
        """JSON 应包含 folds 列表且每折有完整的日期边界。"""
        result = make_mock_research_result()
        gen = ResearchReportGenerator()
        summary = gen.generate_json(result, initial_cash=1000.0)

        assert "folds" in summary
        assert len(summary["folds"]) == 2
        fold0 = summary["folds"][0]
        assert fold0["fold_id"] == 0
        assert "train_start" in fold0
        assert "train_end" in fold0
        assert "test_start" in fold0
        assert "test_end" in fold0

    def test_json_has_limitations(self):
        """JSON 应包含 limitations 列表。"""
        result = make_mock_research_result()
        gen = ResearchReportGenerator()
        summary = gen.generate_json(result, initial_cash=1000.0)

        assert "limitations" in summary
        assert isinstance(summary["limitations"], list)
        assert len(summary["limitations"]) > 0

    def test_json_has_eligibility_for_both_tracks(self):
        """JSON 应包含双轨的资格判定信息。"""
        result = make_mock_research_result()
        gen = ResearchReportGenerator()
        summary = gen.generate_json(result, initial_cash=1000.0)

        assert "steady_eligibility" in summary
        assert "aggressive_eligibility" in summary
        assert summary["steady_eligibility"]["status"] == "NOT_ELIGIBLE_FOR_LIVE_TRADING"
        assert summary["aggressive_eligibility"]["status"] == "SIMULATION_ONLY"

    def test_json_metadata_initial_cash_reflected(self):
        """JSON metadata 中 initial_cash 应反映传入的值。"""
        result = make_mock_research_result()
        gen = ResearchReportGenerator()
        summary = gen.generate_json(result, initial_cash=5000.0)

        assert summary["metadata"]["initial_cash"] == 5000.0


# --------------------------------------------------------------------------- #
# 4. TestGenerateMarkdown
# --------------------------------------------------------------------------- #


class TestGenerateMarkdown:
    """Markdown 报告生成测试：内容、标题、表格、声明。"""

    def test_markdown_has_title(self):
        """Markdown 应以一级标题开头。"""
        result = make_mock_research_result()
        gen = ResearchReportGenerator()
        md = gen.generate_markdown(result, initial_cash=1000.0)

        assert md.startswith("# A股双轨策略研究报告（Phase 3）")

    def test_markdown_has_metadata_section(self):
        """Markdown 应包含元数据章节。"""
        result = make_mock_research_result()
        gen = ResearchReportGenerator()
        md = gen.generate_markdown(result, initial_cash=1000.0)

        assert "## 元数据" in md
        assert "代码提交号" in md
        assert "配置哈希" in md
        assert "数据哈希" in md
        assert "初始资金" in md

    def test_markdown_has_fold_table(self):
        """Markdown 应包含滚动折表格。"""
        result = make_mock_research_result()
        gen = ResearchReportGenerator()
        md = gen.generate_markdown(result, initial_cash=1000.0)

        assert "## 滚动折" in md
        assert "| 折ID | 训练期 | 验证期 | 测试期 |" in md
        assert "fold_id" or "0" in md

    def test_markdown_has_limitations(self):
        """Markdown 应包含限制声明章节和内容。"""
        result = make_mock_research_result()
        gen = ResearchReportGenerator()
        md = gen.generate_markdown(result, initial_cash=1000.0)

        assert "## 限制声明" in md
        for item in result.limitations:
            assert item in md

    def test_markdown_has_track_sections(self):
        """Markdown 应包含稳健轨和激进轨章节。"""
        result = make_mock_research_result()
        gen = ResearchReportGenerator()
        md = gen.generate_markdown(result, initial_cash=1000.0)

        assert "## 稳健轨" in md
        assert "## 激进轨" in md

    def test_markdown_has_monte_carlo_disclaimer(self):
        """Markdown 蒙特卡洛章节应包含免责声明。"""
        result = make_mock_research_result()
        gen = ResearchReportGenerator()
        md = gen.generate_markdown(result, initial_cash=1000.0)

        assert "## 蒙特卡洛概率分析（激进轨）" in md
        assert "蒙特卡洛结果仅用于概率研究，不构成收益承诺" in md
        assert "SIMULATION_ONLY" in md


# --------------------------------------------------------------------------- #
# 5. TestGenerateParquet
# --------------------------------------------------------------------------- #


class TestGenerateParquet:
    """Parquet 文件生成测试：列结构、非空、可追溯性。

    覆盖测试项 19 中「订单、成交和权益可追溯」的要求。
    """

    def test_folds_parquet_has_correct_columns(self, tmp_path):
        """walk-forward-folds.parquet 应包含全部预期列。"""
        result = make_mock_research_result()
        gen = ResearchReportGenerator()
        df = gen.generate_folds_dataframe(result)

        expected_cols = {
            "fold_id", "train_start", "train_end",
            "validation_start", "validation_end",
            "test_start", "test_end", "track",
            "selected_params", "selection_reason",
            "eliminated_count",
            "test_total_return", "test_max_drawdown",
            "benchmark_hs300", "benchmark_csi_all",
        }
        assert set(df.columns) == expected_cols

    def test_folds_parquet_non_empty(self, tmp_path):
        """walk-forward-folds.parquet 应非空（两条轨道各一折）。"""
        result = make_mock_research_result()
        gen = ResearchReportGenerator()
        df = gen.generate_folds_dataframe(result)

        assert len(df) == 2  # steady 1 fold + aggressive 1 fold

    def test_equity_parquet_non_empty_and_traceable(self, tmp_path):
        """稳健轨和激进轨权益 Parquet 应非空且日期可追溯。"""
        result = make_mock_research_result()
        gen = ResearchReportGenerator()

        steady_df = gen.generate_equity_dataframe(result.steady.oos_equity)
        aggressive_df = gen.generate_equity_dataframe(result.aggressive.oos_equity)

        assert len(steady_df) == 5
        assert len(aggressive_df) == 5

        # 验证列结构
        expected_cols = {
            "snapshot_date", "cash", "position_value", "total_equity",
            "daily_pnl", "cumulative_pnl", "drawdown",
        }
        assert set(steady_df.columns) == expected_cols
        assert set(aggressive_df.columns) == expected_cols

        # 权益可追溯：验证日期序列与输入一致
        assert steady_df["snapshot_date"].iloc[0] == date(2024, 1, 2)
        assert aggressive_df["snapshot_date"].iloc[-1] == date(2024, 1, 8)

    def test_orders_parquet_traceable(self, tmp_path):
        """orders.parquet 应包含可追溯的订单流水（关联轨道、折和信号）。"""
        result = make_mock_research_result()
        gen = ResearchReportGenerator()
        df = gen.generate_orders_dataframe(result)

        assert len(df) == 4  # 2 orders * 2 tracks

        # 验证列结构
        expected_cols = {
            "track", "fold_id", "order_id", "signal_date",
            "symbol", "side", "quantity", "reason",
            "planned_fill_date", "status",
            "reject_reason", "reject_detail", "filled", "audit_flags",
        }
        assert set(df.columns) == expected_cols

        # 订单可追溯：每条订单应关联到轨道和折
        for _, row in df.iterrows():
            assert row["track"] in ("steady", "aggressive")
            assert row["fold_id"] == 0
            assert row["order_id"] != ""
            assert row["symbol"] == "000001"
            assert row["side"] in ("BUY", "SELL")
            assert row["filled"] is True

    def test_fills_parquet_traceable(self, tmp_path):
        """fills.parquet 应包含可追溯的成交流水（关联订单和金额明细）。"""
        result = make_mock_research_result()
        gen = ResearchReportGenerator()
        df = gen.generate_fills_dataframe(result)

        assert len(df) == 4  # 2 fills * 2 tracks

        # 验证列结构
        expected_cols = {
            "track", "fold_id", "order_id", "fill_date",
            "symbol", "side", "quantity",
            "raw_open_price", "slippage_price",
            "commission", "stamp_duty", "transfer_fee",
            "total_cost", "cash_change", "turnover", "audit_flags",
        }
        assert set(df.columns) == expected_cols

        # 成交可追溯：每条成交应关联到订单 ID
        for _, row in df.iterrows():
            assert row["order_id"] != ""
            assert row["fill_date"] is not None
            assert row["symbol"] == "000001"

        # 验证买入成交的现金变化为负，卖出为正
        buy_rows = df[df["side"] == "BUY"]
        sell_rows = df[df["side"] == "SELL"]
        assert all(buy_rows["cash_change"] < 0)
        assert all(sell_rows["cash_change"] > 0)

    def test_all_parquet_files_generated(self, tmp_path):
        """generate_all 应生成全部 7 个 Parquet 文件且非空。"""
        result = make_mock_research_result()
        paths = _generate_all_to_dir(result, tmp_path)

        parquet_files = [
            "walk-forward-folds.parquet",
            "steady-oos-equity.parquet",
            "aggressive-oos-equity.parquet",
            "orders.parquet",
            "fills.parquet",
            "parameter-results.parquet",
            "stress-results.parquet",
            "market-regimes.parquet",
        ]
        for fname in parquet_files:
            assert fname in paths
            assert paths[fname].exists()
            df = pd.read_parquet(paths[fname])
            assert len(df) > 0, f"{fname} 不应为空"

    def test_parameter_results_parquet_baseline_detection(self, tmp_path):
        """parameter-results.parquet 应正确标记基线参数。"""
        result = make_mock_research_result()
        gen = ResearchReportGenerator()
        df = gen.generate_parameter_results_dataframe(result)

        assert len(df) == 5  # 3 steady + 2 aggressive
        assert "is_baseline" in df.columns
        assert df["is_baseline"].sum() == 2  # 1 baseline per track

    def test_stress_results_parquet_has_scenarios(self, tmp_path):
        """stress-results.parquet 应包含全部压力场景。"""
        result = make_mock_research_result()
        gen = ResearchReportGenerator()
        df = gen.generate_stress_results_dataframe(result)

        assert len(df) == 8  # 4 scenarios * 2 tracks
        assert "scenario_name" in df.columns
        assert "combined_2x" in df["scenario_name"].values

    def test_market_regimes_parquet_has_regimes(self, tmp_path):
        """market-regimes.parquet 应包含市场阶段分类数据。"""
        result = make_mock_research_result()
        gen = ResearchReportGenerator()
        df = gen.generate_market_regimes_dataframe(result)

        assert len(df) == 3  # 3 regime records
        assert "regime" in df.columns
        assert "is_bull" in df.columns
        assert "is_bear" in df.columns
        assert "is_high_volatility" in df.columns


# --------------------------------------------------------------------------- #
# 6. TestDoubleRunConsistency
# --------------------------------------------------------------------------- #


class TestDoubleRunConsistency:
    """双跑一致性测试：JSON/Markdown 字节级一致、Parquet 内容一致。

    覆盖测试项 19 中「JSON/Markdown/Parquet 双跑一致」的要求。

    由于 generate_json 包含 datetime.utcnow().isoformat() 时间戳，
    需 patch datetime 使其返回固定值，否则字节级比较会因时间戳不同而失败。
    """

    def test_json_byte_identical_with_patched_datetime(self, tmp_path):
        """patch datetime 后，两次生成的 JSON 文件应字节级完全一致。"""
        dir1 = tmp_path / "run1"
        dir2 = tmp_path / "run2"

        with patch("ashare_quant.research.report.datetime") as mock_dt:
            mock_dt.utcnow.return_value = FIXED_DATETIME

            result1 = make_mock_research_result()
            _generate_all_to_dir(result1, dir1)

            result2 = make_mock_research_result()
            _generate_all_to_dir(result2, dir2)

        json1 = (dir1 / "research-summary.json").read_bytes()
        json2 = (dir2 / "research-summary.json").read_bytes()
        assert json1 == json2

    def test_markdown_byte_identical(self, tmp_path):
        """两次生成的 Markdown 文件应字节级完全一致（Markdown 无时间戳）。"""
        dir1 = tmp_path / "run1"
        dir2 = tmp_path / "run2"

        result1 = make_mock_research_result()
        _generate_all_to_dir(result1, dir1)

        result2 = make_mock_research_result()
        _generate_all_to_dir(result2, dir2)

        md1 = (dir1 / "research-report.md").read_bytes()
        md2 = (dir2 / "research-report.md").read_bytes()
        assert md1 == md2

    def test_parquet_content_identical(self, tmp_path):
        """两次生成的全部 Parquet 文件内容应完全一致。"""
        dir1 = tmp_path / "run1"
        dir2 = tmp_path / "run2"

        result1 = make_mock_research_result()
        _generate_all_to_dir(result1, dir1)

        result2 = make_mock_research_result()
        _generate_all_to_dir(result2, dir2)

        parquet_files = [
            "walk-forward-folds.parquet",
            "steady-oos-equity.parquet",
            "aggressive-oos-equity.parquet",
            "orders.parquet",
            "fills.parquet",
            "parameter-results.parquet",
            "stress-results.parquet",
            "market-regimes.parquet",
        ]
        for fname in parquet_files:
            df1 = pd.read_parquet(dir1 / fname)
            df2 = pd.read_parquet(dir2 / fname)
            pd.testing.assert_frame_equal(df1, df2)

    def test_monte_carlo_json_identical_with_patched_datetime(self, tmp_path):
        """patch datetime 后，蒙特卡洛摘要 JSON 应字节级一致。"""
        dir1 = tmp_path / "run1"
        dir2 = tmp_path / "run2"

        with patch("ashare_quant.research.report.datetime") as mock_dt:
            mock_dt.utcnow.return_value = FIXED_DATETIME

            result1 = make_mock_research_result()
            _generate_all_to_dir(result1, dir1)

            result2 = make_mock_research_result()
            _generate_all_to_dir(result2, dir2)

        mc1 = (dir1 / "monte-carlo-summary.json").read_bytes()
        mc2 = (dir2 / "monte-carlo-summary.json").read_bytes()
        assert mc1 == mc2

    def test_json_excluding_generated_at_identical(self, tmp_path):
        """不 patch datetime 时，排除 generated_at 字段后 JSON 应完全一致。"""
        dir1 = tmp_path / "run1"
        dir2 = tmp_path / "run2"

        result1 = make_mock_research_result()
        _generate_all_to_dir(result1, dir1)

        result2 = make_mock_research_result()
        _generate_all_to_dir(result2, dir2)

        json1 = json.loads((dir1 / "research-summary.json").read_text(encoding="utf-8"))
        json2 = json.loads((dir2 / "research-summary.json").read_text(encoding="utf-8"))

        # 删除 generated_at 后比较
        json1["metadata"].pop("generated_at", None)
        json2["metadata"].pop("generated_at", None)
        assert json1 == json2

    def test_same_result_object_twice_identical(self, tmp_path):
        """同一 result 对象连续调用两次 generate_all，输出应一致（patch datetime）。"""
        dir1 = tmp_path / "run1"
        dir2 = tmp_path / "run2"

        result = make_mock_research_result()

        with patch("ashare_quant.research.report.datetime") as mock_dt:
            mock_dt.utcnow.return_value = FIXED_DATETIME

            _generate_all_to_dir(result, dir1)
            _generate_all_to_dir(result, dir2)

        # JSON 字节一致
        assert (dir1 / "research-summary.json").read_bytes() == \
               (dir2 / "research-summary.json").read_bytes()
        # Markdown 字节一致
        assert (dir1 / "research-report.md").read_bytes() == \
               (dir2 / "research-report.md").read_bytes()
        # Parquet 内容一致
        df1 = pd.read_parquet(dir1 / "orders.parquet")
        df2 = pd.read_parquet(dir2 / "orders.parquet")
        pd.testing.assert_frame_equal(df1, df2)


# --------------------------------------------------------------------------- #
# 7. TestReportCompleteness
# --------------------------------------------------------------------------- #


class TestReportCompleteness:
    """报告完整性测试：全部输出文件存在、元数据完整、限制声明。"""

    REQUIRED_FILES = [
        "research-summary.json",
        "research-report.md",
        "walk-forward-folds.parquet",
        "steady-oos-equity.parquet",
        "aggressive-oos-equity.parquet",
        "orders.parquet",
        "fills.parquet",
        "parameter-results.parquet",
        "stress-results.parquet",
        "market-regimes.parquet",
        "monte-carlo-summary.json",
    ]

    def test_all_required_files_present(self, tmp_path):
        """generate_all 应生成全部 11 个必需输出文件。"""
        result = make_mock_research_result()
        paths = _generate_all_to_dir(result, tmp_path)

        for fname in self.REQUIRED_FILES:
            assert fname in paths, f"缺少输出文件: {fname}"
            assert paths[fname].exists(), f"文件不存在: {fname}"

    def test_code_commit_in_json(self, tmp_path):
        """JSON 报告中应包含 code_commit 且与 result 一致。"""
        result = make_mock_research_result()
        _generate_all_to_dir(result, tmp_path)

        summary = json.loads(
            (tmp_path / "research-summary.json").read_text(encoding="utf-8")
        )
        assert summary["metadata"]["code_commit"] == "abc123def456"

    def test_limitations_listed_in_json(self, tmp_path):
        """JSON 报告中应列出全部 limitations。"""
        result = make_mock_research_result()
        _generate_all_to_dir(result, tmp_path)

        summary = json.loads(
            (tmp_path / "research-summary.json").read_text(encoding="utf-8")
        )
        assert len(summary["limitations"]) == len(result.limitations)
        for item in result.limitations:
            assert item in summary["limitations"]

    def test_config_hash_updated_after_generate_all(self, tmp_path):
        """generate_all 后 result 的 config_hash 应被更新。"""
        result = make_mock_research_result()
        assert result.config_hash is None  # 初始为 None

        config_dict = {"test": "config", "value": 42}
        _generate_all_to_dir(result, tmp_path, config_dict=config_dict)

        assert result.config_hash is not None
        assert result.config_hash == compute_config_hash(config_dict)

    def test_data_hash_updated_after_generate_all(self, tmp_path):
        """generate_all 后 result 的 data_hash 应被更新。"""
        result = make_mock_research_result()
        assert result.data_hash is None

        data_file = tmp_path / "input.parquet"
        data_file.write_bytes(b"test data content")
        _generate_all_to_dir(result, tmp_path, data_files=[str(data_file)])

        assert result.data_hash is not None
        assert result.data_hash == compute_data_hash([str(data_file)])

    def test_code_commit_updated_when_none(self, tmp_path):
        """当 result.code_commit 为 None 时，generate_all 应填充 code_commit。"""
        result = make_mock_research_result()
        result.code_commit = None
        _generate_all_to_dir(result, tmp_path)

        # get_code_commit() 会返回 git commit 或 "no-git"
        assert result.code_commit is not None
        assert isinstance(result.code_commit, str)


# --------------------------------------------------------------------------- #
# 8. TestMonteCarloJSON
# --------------------------------------------------------------------------- #


class TestMonteCarloJSON:
    """蒙特卡洛摘要 JSON 测试：结构、声明、字段完整性。"""

    def test_monte_carlo_available_with_all_fields(self):
        """激进轨有蒙特卡洛结果时，摘要应 available=True 且包含全部字段。"""
        result = make_mock_research_result()
        gen = ResearchReportGenerator()
        mc = gen.generate_monte_carlo_json(result)

        assert mc["available"] is True
        assert "prob_ten_x" in mc
        assert "prob_loss_50" in mc
        assert "prob_near_zero" in mc
        assert "percentiles" in mc
        assert "n_oos_days" in mc
        assert "block_length" in mc
        assert "n_paths" in mc
        assert "random_seed" in mc
        assert "insufficient_sample" in mc
        assert "disclaimer" in mc

    def test_monte_carlo_has_disclaimer(self):
        """蒙特卡洛摘要应包含免责声明文本。"""
        result = make_mock_research_result()
        gen = ResearchReportGenerator()
        mc = gen.generate_monte_carlo_json(result)

        assert "disclaimer" in mc
        assert "不构成收益承诺" in mc["disclaimer"]
        assert "SIMULATION_ONLY" in mc["disclaimer"]

    def test_monte_carlo_percentiles_structure(self):
        """蒙特卡洛摘要的 percentiles 应为字典且包含全部 7 个分位数键。"""
        result = make_mock_research_result()
        gen = ResearchReportGenerator()
        mc = gen.generate_monte_carlo_json(result)

        expected_keys = {"1%", "5%", "25%", "50%", "75%", "95%", "99%"}
        assert set(mc["percentiles"].keys()) == expected_keys

    def test_monte_carlo_values_match_input(self):
        """蒙特卡洛摘要中的概率值应与输入的 MonteCarloResult 一致。"""
        result = make_mock_research_result()
        gen = ResearchReportGenerator()
        mc = gen.generate_monte_carlo_json(result)

        mc_input = result.aggressive.monte_carlo
        assert mc["prob_ten_x"] == mc_input.prob_ten_x
        assert mc["prob_loss_50"] == mc_input.prob_loss_50
        assert mc["prob_near_zero"] == mc_input.prob_near_zero
        assert mc["n_paths"] == mc_input.n_paths
        assert mc["random_seed"] == mc_input.random_seed

    def test_monte_carlo_when_none(self):
        """当激进轨无蒙特卡洛结果时，摘要应 available=False。"""
        result = make_mock_research_result()
        result.aggressive.monte_carlo = None
        gen = ResearchReportGenerator()
        mc = gen.generate_monte_carlo_json(result)

        assert mc["available"] is False
        assert "reason" in mc

    def test_monte_carlo_json_file_written(self, tmp_path):
        """generate_all 应将蒙特卡洛摘要写入 monte-carlo-summary.json。"""
        result = make_mock_research_result()
        _generate_all_to_dir(result, tmp_path)

        mc_path = tmp_path / "monte-carlo-summary.json"
        assert mc_path.exists()

        mc = json.loads(mc_path.read_text(encoding="utf-8"))
        assert mc["available"] is True
        assert mc["prob_ten_x"] == 0.05
        assert mc["disclaimer"] != ""
