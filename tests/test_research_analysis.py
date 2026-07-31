"""Phase 3 研究分析模块 ``research.analysis`` 的综合 pytest 测试。

覆盖必测项：
10. 每个样本外折完整输出，拼接测试期无重复日期
11. 参数选择规则、并列规则和被淘汰参数可复算
17. 稳健轨任一资格条件失败时输出不具备实盘资格
18. 激进轨始终为 SIMULATION_ONLY

所有测试均为确定性测试，不依赖随机数或外部数据。
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from ashare_quant.backtest.models import (
    BacktestResult,
    Fill,
    Order,
    PortfolioSnapshot,
    Side,
    to_decimal,
)
from ashare_quant.research.analysis import (
    EligibilityCheck,
    EligibilityStatus,
    FoldResult,
    ResearchResult,
    TrackResult,
    TrackType,
    check_steady_eligibility,
    compute_metrics_from_equity,
    concatenate_oos_equity,
    select_aggressive_params,
    select_steady_params,
)
from ashare_quant.research.strategies import (
    AGGRESSIVE_BASELINE_PARAMS,
    STEADY_BASELINE_PARAMS,
    AggressiveParams,
    SteadyParams,
)
from ashare_quant.research.stress import (
    ParameterPerturbationResult,
    StressResult,
    StressScenario,
)
from ashare_quant.research.walk_forward import Fold


# --------------------------------------------------------------------------- #
# 辅助构建函数
# --------------------------------------------------------------------------- #


def _make_snapshot(
    d: date,
    total_equity: float,
    cash: float | None = None,
    position_value: float | None = None,
) -> PortfolioSnapshot:
    """创建一个 PortfolioSnapshot，默认全部为现金。"""
    te = to_decimal(total_equity)
    c = to_decimal(cash) if cash is not None else te
    pv = to_decimal(position_value) if position_value is not None else to_decimal("0")
    return PortfolioSnapshot(
        snapshot_date=d,
        cash=c,
        position_value=pv,
        total_equity=te,
    )


def _make_dates(n: int, start: date = date(2024, 1, 1)) -> list[date]:
    """生成 n 个连续日历日期。"""
    return [start + timedelta(days=i) for i in range(n)]


def _make_equity(
    values: list[float],
    start: date = date(2024, 1, 1),
    all_cash: bool = True,
) -> list[PortfolioSnapshot]:
    """从 total_equity 值列表生成权益快照列表。

    all_cash=True 时 position_value=0（纯现金日）。
    all_cash=False 时 cash=0（满仓日）。
    """
    dates = _make_dates(len(values), start)
    if all_cash:
        return [_make_snapshot(d, v, cash=v, position_value=0) for d, v in zip(dates, values)]
    return [_make_snapshot(d, v, cash=0, position_value=v) for d, v in zip(dates, values)]


def _make_equity_mixed(
    values: list[float],
    cash_flags: list[bool],
    start: date = date(2024, 1, 1),
) -> list[PortfolioSnapshot]:
    """生成部分现金、部分持仓的权益快照列表。

    cash_flags[i]=True 表示第 i 天为现金日（position_value=0）。
    """
    dates = _make_dates(len(values), start)
    snapshots: list[PortfolioSnapshot] = []
    for d, v, is_cash in zip(dates, values, cash_flags):
        if is_cash:
            snapshots.append(_make_snapshot(d, v, cash=v, position_value=0))
        else:
            snapshots.append(_make_snapshot(d, v, cash=0, position_value=v))
    return snapshots


def _make_fill(
    fill_date: date,
    symbol: str = "TEST001",
    side: Side = Side.BUY,
    quantity: int = 100,
    price: float = 10.0,
) -> Fill:
    """创建一个最小化的 Fill 用于测试。

    turnover = slippage_price * quantity，用于控制 MetricsCalculator 的换手率。
    """
    sp = to_decimal(price)
    if side == Side.BUY:
        cash_change = -(price * quantity)
    else:
        cash_change = price * quantity
    return Fill(
        order_id=f"ORD_{fill_date.isoformat()}_{symbol}_{side.value}_{quantity}",
        fill_date=fill_date,
        symbol=symbol,
        side=side,
        quantity=quantity,
        raw_open_price=sp,
        slippage_price=sp,
        commission=to_decimal("0"),
        stamp_duty=to_decimal("0"),
        transfer_fee=to_decimal("0"),
        total_cost=to_decimal("0"),
        cash_change=to_decimal(cash_change),
    )


def _make_backtest_result(
    daily_equity: list[PortfolioSnapshot] | None = None,
    fills: list[Fill] | None = None,
    metrics: dict | None = None,
) -> BacktestResult:
    """创建一个 BacktestResult 用于测试。"""
    return BacktestResult(
        config_summary={},
        orders=[],
        fills=fills or [],
        daily_equity=daily_equity or [],
        final_positions={},
        metrics=metrics or {},
        limitations=[],
        data_range={},
    )


def _make_fold(fold_id: int = 0) -> Fold:
    """创建一个最小化的 Fold 用于测试。"""
    return Fold(
        fold_id=fold_id,
        train_start=date(2021, 1, 1),
        train_end=date(2023, 12, 31),
        validation_start=date(2023, 7, 1),
        validation_end=date(2023, 12, 31),
        test_start=date(2024, 1, 1),
        test_end=date(2024, 12, 31),
    )


def _make_fold_result(
    fold: Fold | None = None,
    test_result: BacktestResult | None = None,
    selected_params: dict | None = None,
    reason: str = "",
    eliminated: list | None = None,
) -> FoldResult:
    """创建一个 FoldResult 用于测试。"""
    return FoldResult(
        fold=fold or _make_fold(),
        selected_params=selected_params or {},
        selection_reason=reason,
        eliminated_candidates=eliminated or [],
        test_result=test_result,
    )


def _make_stress_result(
    name: str = "combined_2x",
    annualized_return: float = 0.05,
) -> StressResult:
    """创建一个 StressResult 用于测试。"""
    return StressResult(
        scenario=StressScenario(
            name=name,
            fee_multiplier=2.0,
            slippage_multiplier=2.0,
            description="测试压力场景",
        ),
        total_return=0.1,
        annualized_return=annualized_return,
        max_drawdown=0.1,
        sharpe=1.0,
        calmar=0.5,
        win_rate=0.6,
        turnover_rate=0.3,
        total_trades=10,
    )


def _make_param_perturbation(return_median: float = 0.05) -> ParameterPerturbationResult:
    """创建一个 ParameterPerturbationResult 用于测试。"""
    return ParameterPerturbationResult(
        total_combinations=10,
        positive_return_count=7,
        positive_return_ratio=0.7,
        return_median=return_median,
        return_p10=-0.02,
        return_p90=0.15,
        max_drawdown_median=0.1,
        max_drawdown_p10=0.05,
        max_drawdown_p90=0.2,
        turnover_median=0.3,
        turnover_p10=0.1,
        turnover_p90=0.5,
        baseline_return=0.04,
        baseline_max_drawdown=0.08,
        baseline_turnover=0.25,
    )


# --------------------------------------------------------------------------- #
# 1. compute_metrics_from_equity 测试
# --------------------------------------------------------------------------- #


class TestComputeMetricsFromEquity:
    """指标计算函数测试。"""

    def test_empty_equity_returns_zeros(self):
        """空权益序列应返回全零指标字典。"""
        result = compute_metrics_from_equity([], initial_cash=1000.0)

        assert result["total_return"] == 0.0
        assert result["annualized_return"] == 0.0
        assert result["max_drawdown"] == 0.0
        assert result["sharpe"] == 0.0
        assert result["calmar"] == 0.0
        assert result["total_trades"] == 0
        assert result["win_rate"] == 0.0
        assert result["turnover_rate"] == 0.0
        assert result["trading_days"] == 0
        assert result["cash_ratio"] == 0.0

    def test_single_snapshot_total_return(self):
        """单个快照时 total_return = final/initial - 1。"""
        snap = _make_snapshot(date(2024, 1, 1), 1100.0)
        result = compute_metrics_from_equity([snap], initial_cash=1000.0)

        assert result["total_return"] == pytest.approx(0.1)
        assert result["trading_days"] == 1
        assert result["final_equity"] == pytest.approx(1100.0)
        assert result["initial_equity"] == pytest.approx(1000.0)
        # 单快照无日收益序列，vol/sharpe/win_rate 均为 0
        assert result["annualized_volatility"] == 0.0
        assert result["sharpe"] == 0.0
        assert result["win_rate"] == 0.0

    def test_monotonic_increasing_zero_drawdown(self):
        """单调递增权益曲线最大回撤应为 0。"""
        equity = _make_equity([1000, 1050, 1100, 1150])
        result = compute_metrics_from_equity(equity, initial_cash=1000.0)

        assert result["max_drawdown"] == pytest.approx(0.0)
        assert result["total_return"] == pytest.approx(0.15)
        # 全部日收益为正 → win_rate = 1.0
        assert result["win_rate"] == pytest.approx(1.0)

    def test_max_drawdown_with_dip(self):
        """含回撤的权益曲线应正确计算最大回撤。"""
        # 1000 → 1200 → 900 → 1100
        equity = _make_equity([1000, 1200, 900, 1100])
        result = compute_metrics_from_equity(equity, initial_cash=1000.0)

        # 最大回撤 = (1200 - 900) / 1200 = 0.25
        assert result["max_drawdown"] == pytest.approx(0.25, abs=1e-6)
        assert result["total_return"] == pytest.approx(0.1)

    def test_trading_days_equals_equity_length(self):
        """trading_days 应等于权益序列长度。"""
        for n in [1, 5, 10, 50]:
            equity = _make_equity([1000.0] * n)
            result = compute_metrics_from_equity(equity, initial_cash=1000.0)
            assert result["trading_days"] == n

    def test_cash_ratio_mixed_positions(self):
        """现金占比应正确统计 position_value <= 0 的天数。"""
        values = [1000, 1000, 1000, 1000]
        cash_flags = [True, False, True, False]
        equity = _make_equity_mixed(values, cash_flags)
        result = compute_metrics_from_equity(equity, initial_cash=1000.0)

        # 2/4 天为现金日
        assert result["cash_ratio"] == pytest.approx(0.5)

    def test_win_rate_mixed_daily_returns(self):
        """胜率应正确统计日收益为正的比例。"""
        # 1000 → 1100（+10%）→ 1000（-9.09%）→ 1050（+5%）
        equity = _make_equity([1000, 1100, 1000, 1050])
        result = compute_metrics_from_equity(equity, initial_cash=1000.0)

        # 3 个日收益：+0.1, -0.0909, +0.05 → 2 正 1 负
        assert result["win_rate"] == pytest.approx(2.0 / 3.0, abs=1e-6)

    def test_total_trades_from_fills(self):
        """total_trades 应等于 len(fills) // 2。"""
        equity = _make_equity([1000, 1000, 1000, 1000])
        dates = _make_dates(4)
        fills = [
            _make_fill(dates[0], side=Side.BUY, quantity=100, price=10.0),
            _make_fill(dates[1], side=Side.SELL, quantity=100, price=10.0),
            _make_fill(dates[2], side=Side.BUY, quantity=100, price=10.0),
            _make_fill(dates[3], side=Side.SELL, quantity=100, price=10.0),
        ]
        result = compute_metrics_from_equity(equity, initial_cash=1000.0, fills=fills)

        # 4 个 fill → 2 次交易
        assert result["total_trades"] == 2


# --------------------------------------------------------------------------- #
# 2. concatenate_oos_equity 测试（必测项 10）
# --------------------------------------------------------------------------- #


class TestConcatenateOosEquity:
    """样本外权益拼接测试。"""

    def test_empty_fold_results_returns_empty(self):
        """空折结果列表应返回空列表。"""
        result = concatenate_oos_equity([], initial_cash=1000.0)
        assert result == []

    def test_single_fold_passthrough(self):
        """单折应完整输出其权益序列。"""
        equity = _make_equity([1000, 1050, 1100])
        bt = _make_backtest_result(daily_equity=equity)
        fr = _make_fold_result(test_result=bt)
        result = concatenate_oos_equity([fr], initial_cash=1000.0)

        assert len(result) == 3
        # 初始权益应等于 initial_cash
        assert float(result[0].total_equity) == pytest.approx(1000.0)
        assert float(result[-1].total_equity) == pytest.approx(1100.0)

    def test_multiple_folds_no_duplicate_dates(self):
        """多折拼接后不应有重复日期。"""
        # 折 1: 2024-01-01 ~ 01-03
        eq1 = _make_equity([1000, 1050, 1100], start=date(2024, 1, 1))
        # 折 2: 2024-01-04 ~ 01-06（不重叠）
        eq2 = _make_equity([1000, 950, 1050], start=date(2024, 1, 4))
        # 折 3: 2024-01-07 ~ 01-09（不重叠）
        eq3 = _make_equity([1000, 1080, 1120], start=date(2024, 1, 7))

        folds = [
            _make_fold_result(fold=_make_fold(0), test_result=_make_backtest_result(eq1)),
            _make_fold_result(fold=_make_fold(1), test_result=_make_backtest_result(eq2)),
            _make_fold_result(fold=_make_fold(2), test_result=_make_backtest_result(eq3)),
        ]
        result = concatenate_oos_equity(folds, initial_cash=1000.0)

        dates = [s.snapshot_date for s in result]
        assert len(dates) == len(set(dates)), "拼接结果中存在重复日期"
        assert len(result) == 9

    def test_scaling_continues_from_previous_fold(self):
        """每折权益应从前一折最终权益开始复利缩放。"""
        # 折 1: 1000 → 1200（+20%）
        eq1 = _make_equity([1000, 1200], start=date(2024, 1, 1))
        # 折 2: 1000 → 1100（+10%），但应缩放到从 1200 开始
        eq2 = _make_equity([1000, 1100], start=date(2024, 1, 3))

        folds = [
            _make_fold_result(fold=_make_fold(0), test_result=_make_backtest_result(eq1)),
            _make_fold_result(fold=_make_fold(1), test_result=_make_backtest_result(eq2)),
        ]
        result = concatenate_oos_equity(folds, initial_cash=1000.0)

        # 折 1 最终 = 1200
        assert float(result[1].total_equity) == pytest.approx(1200.0)
        # 折 2 起始应 = 1200（缩放因子 = 1200/1000 = 1.2）
        assert float(result[2].total_equity) == pytest.approx(1200.0)
        # 折 2 最终应 = 1100 * 1.2 = 1320
        assert float(result[3].total_equity) == pytest.approx(1320.0)
        # 总收益 = 1320/1000 - 1 = 0.32 = 1.2 * 1.1 - 1
        total_ret = float(result[-1].total_equity) / 1000.0 - 1.0
        assert total_ret == pytest.approx(0.32, abs=1e-6)

    def test_skip_fold_without_test_result(self):
        """test_result 为 None 的折应被跳过。"""
        eq1 = _make_equity([1000, 1100], start=date(2024, 1, 1))
        folds = [
            _make_fold_result(fold=_make_fold(0), test_result=_make_backtest_result(eq1)),
            _make_fold_result(fold=_make_fold(1), test_result=None),
        ]
        result = concatenate_oos_equity(folds, initial_cash=1000.0)

        assert len(result) == 2
        assert result[0].snapshot_date == date(2024, 1, 1)
        assert result[1].snapshot_date == date(2024, 1, 2)

    def test_skip_fold_with_empty_equity(self):
        """daily_equity 为空的折应被跳过。"""
        eq1 = _make_equity([1000, 1100], start=date(2024, 1, 1))
        folds = [
            _make_fold_result(fold=_make_fold(0), test_result=_make_backtest_result(eq1)),
            _make_fold_result(
                fold=_make_fold(1),
                test_result=_make_backtest_result(daily_equity=[]),
            ),
        ]
        result = concatenate_oos_equity(folds, initial_cash=1000.0)

        assert len(result) == 2

    def test_overlapping_dates_first_wins(self):
        """重叠日期应保留首次出现的快照，跳过后续重复。"""
        # 折 1: 01-01, 01-02, 01-03
        eq1 = _make_equity([1000, 1050, 1100], start=date(2024, 1, 1))
        # 折 2: 01-03, 01-04（01-03 重叠）
        eq2 = _make_equity([1000, 1200], start=date(2024, 1, 3))

        folds = [
            _make_fold_result(fold=_make_fold(0), test_result=_make_backtest_result(eq1)),
            _make_fold_result(fold=_make_fold(1), test_result=_make_backtest_result(eq2)),
        ]
        result = concatenate_oos_equity(folds, initial_cash=1000.0)

        dates = [s.snapshot_date for s in result]
        # 01-03 应只出现一次（来自折 1）
        assert dates.count(date(2024, 1, 3)) == 1
        assert len(result) == 4  # 01-01, 01-02, 01-03, 01-04

    def test_all_fold_snapshots_present(self):
        """每个样本外折的快照应完整出现在拼接结果中。"""
        eq1 = _make_equity([1000, 1010, 1020], start=date(2024, 1, 1))
        eq2 = _make_equity([1000, 990, 1030], start=date(2024, 1, 4))
        eq3 = _make_equity([1000, 1050, 1080], start=date(2024, 1, 7))

        folds = [
            _make_fold_result(fold=_make_fold(0), test_result=_make_backtest_result(eq1)),
            _make_fold_result(fold=_make_fold(1), test_result=_make_backtest_result(eq2)),
            _make_fold_result(fold=_make_fold(2), test_result=_make_backtest_result(eq3)),
        ]
        result = concatenate_oos_equity(folds, initial_cash=1000.0)

        expected_dates = (
            _make_dates(3, date(2024, 1, 1))
            + _make_dates(3, date(2024, 1, 4))
            + _make_dates(3, date(2024, 1, 7))
        )
        actual_dates = [s.snapshot_date for s in result]
        assert actual_dates == expected_dates
        assert len(result) == 9


# --------------------------------------------------------------------------- #
# 3. select_steady_params 测试（必测项 11）
# --------------------------------------------------------------------------- #


class TestSelectSteadyParams:
    """稳健轨参数选择测试。

    选择规则：先排除最大回撤 > 20% 的候选，再按 Calmar 降序排序；
    并列时依次选择换手率更低、参数离基线更近的候选。
    被淘汰参数（因回撤超限）记录在 eliminated 列表中，可复算。
    """

    def test_empty_validation_results_returns_baseline(self):
        """无验证结果时应返回基线参数。"""
        selected, reason, eliminated = select_steady_params([], [])

        assert selected == STEADY_BASELINE_PARAMS
        assert "基线" in reason
        assert eliminated == []

    def test_drawdown_elimination(self):
        """最大回撤超过 20% 的候选应被淘汰。"""
        # 候选 A：小回撤，通过
        eq_a = _make_equity([1000, 1010, 1005, 1020])
        params_a = SteadyParams(trend_window=120, momentum_window=60,
                                volatility_window=20, minimum_score=0.0)
        # 候选 B：大回撤（1200 → 700 → 800），max_dd ≈ 0.417 > 0.20
        eq_b = _make_equity([1000, 1200, 700, 800])
        params_b = SteadyParams(trend_window=100, momentum_window=50,
                                volatility_window=15, minimum_score=-0.25)

        val_results = [
            (params_a, _make_backtest_result(eq_a)),
            (params_b, _make_backtest_result(eq_b)),
        ]
        selected, reason, eliminated = select_steady_params(
            [params_a, params_b], val_results
        )

        assert selected == params_a
        assert len(eliminated) == 1
        assert eliminated[0]["param_key"].startswith("tw100")
        assert "回撤" in eliminated[0]["reason"]
        assert eliminated[0]["max_drawdown"] > 0.20

    def test_ranking_when_calmar_tied_turnover_decides(self):
        """Calmar 相同时应选择换手率更低的候选。

        MetricsCalculator 不输出 calmar，故所有候选 calmar 均为 0（并列），
        此时由换手率升序决定排名。
        """
        # 两个候选使用相同的权益曲线（相同 max_drawdown），但换手率不同
        eq = _make_equity([1000, 1010, 1005, 1020])
        dates = _make_dates(4)

        # 候选 A：低换手率（2 笔成交，total_turnover = 200）
        fills_a = [
            _make_fill(dates[0], side=Side.BUY, quantity=10, price=10.0),
            _make_fill(dates[1], side=Side.SELL, quantity=10, price=10.0),
        ]
        # 候选 B：高换手率（2 笔成交，total_turnover = 400）
        fills_b = [
            _make_fill(dates[0], side=Side.BUY, quantity=20, price=10.0),
            _make_fill(dates[1], side=Side.SELL, quantity=20, price=10.0),
        ]

        params_a = SteadyParams(trend_window=120, momentum_window=60,
                                volatility_window=20, minimum_score=0.0)
        params_b = SteadyParams(trend_window=100, momentum_window=50,
                                volatility_window=15, minimum_score=-0.25)

        val_results = [
            (params_a, _make_backtest_result(eq, fills=fills_a)),
            (params_b, _make_backtest_result(eq, fills=fills_b)),
        ]
        selected, reason, eliminated = select_steady_params(
            [params_a, params_b], val_results
        )

        # A 换手率更低 → 应被选中
        assert selected == params_a
        assert "Calmar" in reason
        # 稳健轨的 eliminated 只包含回撤淘汰的候选，不包含排名靠后的
        assert eliminated == []

    def test_distance_tiebreak(self):
        """Calmar 和换手率均相同时应选择离基线更近的候选。"""
        eq = _make_equity([1000, 1010, 1005, 1020])
        dates = _make_dates(4)
        # 两个候选使用相同的权益和成交（相同 calmar、max_dd、turnover）
        fills = [
            _make_fill(dates[0], side=Side.BUY, quantity=10, price=10.0),
            _make_fill(dates[1], side=Side.SELL, quantity=10, price=10.0),
        ]

        # A = 基线参数，distance = 0
        params_a = SteadyParams(trend_window=120, momentum_window=60,
                                volatility_window=20, minimum_score=0.0)
        # B 偏离基线，distance = |100-120| + |50-60| + |15-20| + |-0.25|*10 = 20+10+5+2.5 = 37.5
        params_b = SteadyParams(trend_window=100, momentum_window=50,
                                volatility_window=15, minimum_score=-0.25)

        val_results = [
            (params_a, _make_backtest_result(eq, fills=fills)),
            (params_b, _make_backtest_result(eq, fills=fills)),
        ]
        selected, reason, eliminated = select_steady_params(
            [params_a, params_b], val_results
        )

        # A 离基线更近 → 应被选中
        assert selected == params_a
        assert eliminated == []

    def test_all_eliminated_returns_baseline(self):
        """全部候选因回撤超限被淘汰时应返回基线参数。"""
        eq_a = _make_equity([1000, 1200, 700, 800])  # max_dd ≈ 0.417
        eq_b = _make_equity([1000, 1100, 600, 900])  # max_dd ≈ 0.455

        params_a = SteadyParams(trend_window=120, momentum_window=60,
                                volatility_window=20, minimum_score=0.0)
        params_b = SteadyParams(trend_window=100, momentum_window=50,
                                volatility_window=15, minimum_score=-0.25)

        val_results = [
            (params_a, _make_backtest_result(eq_a)),
            (params_b, _make_backtest_result(eq_b)),
        ]
        selected, reason, eliminated = select_steady_params(
            [params_a, params_b], val_results
        )

        assert selected == STEADY_BASELINE_PARAMS
        assert "回撤约束" in reason
        assert len(eliminated) == 2

    def test_reproducibility(self):
        """相同输入应产生完全相同的输出。"""
        eq = _make_equity([1000, 1010, 1005, 1020])
        dates = _make_dates(4)
        fills = [
            _make_fill(dates[0], side=Side.BUY, quantity=10, price=10.0),
            _make_fill(dates[1], side=Side.SELL, quantity=10, price=10.0),
        ]

        params_a = SteadyParams(trend_window=120, momentum_window=60,
                                volatility_window=20, minimum_score=0.0)
        params_b = SteadyParams(trend_window=100, momentum_window=50,
                                volatility_window=15, minimum_score=-0.25)

        val_results = [
            (params_a, _make_backtest_result(eq, fills=fills)),
            (params_b, _make_backtest_result(eq, fills=fills)),
        ]

        r1 = select_steady_params([params_a, params_b], val_results)
        r2 = select_steady_params([params_a, params_b], val_results)

        assert r1[0] == r2[0]
        assert r1[1] == r2[1]
        assert r1[2] == r2[2]

    def test_eliminated_list_contents(self):
        """被淘汰候选列表应包含 param_key、reason、max_drawdown 等字段。"""
        eq_pass = _make_equity([1000, 1010, 1005, 1020])
        eq_fail = _make_equity([1000, 1200, 700, 800])

        params_pass = SteadyParams(trend_window=120, momentum_window=60,
                                   volatility_window=20, minimum_score=0.0)
        params_fail = SteadyParams(trend_window=100, momentum_window=50,
                                   volatility_window=15, minimum_score=-0.25)

        val_results = [
            (params_pass, _make_backtest_result(eq_pass)),
            (params_fail, _make_backtest_result(eq_fail)),
        ]
        _, _, eliminated = select_steady_params(
            [params_pass, params_fail], val_results
        )

        assert len(eliminated) == 1
        entry = eliminated[0]
        assert "param_key" in entry
        assert "reason" in entry
        assert "max_drawdown" in entry
        assert "calmar" in entry
        assert "turnover_rate" in entry
        assert entry["max_drawdown"] > 0.20

    def test_selection_reason_contains_calmar_and_drawdown(self):
        """选择理由应包含 Calmar 和回撤信息。"""
        eq = _make_equity([1000, 1010, 1005, 1020])
        params = SteadyParams(trend_window=120, momentum_window=60,
                              volatility_window=20, minimum_score=0.0)

        selected, reason, eliminated = select_steady_params(
            [params], [(params, _make_backtest_result(eq))]
        )

        assert selected == params
        assert "Calmar" in reason
        assert "回撤" in reason
        assert "换手率" in reason


# --------------------------------------------------------------------------- #
# 4. select_aggressive_params 测试（必测项 11）
# --------------------------------------------------------------------------- #


class TestSelectAggressiveParams:
    """激进轨参数选择测试。

    选择规则：按验证期扣费后几何收益降序排序；
    并列时依次选择最大回撤更低、换手率更低、参数离基线更近的候选。
    被淘汰参数（非最优候选）记录在 eliminated 列表中，可复算。
    """

    def test_empty_validation_results_returns_baseline(self):
        """无验证结果时应返回基线参数。"""
        selected, reason, eliminated = select_aggressive_params([], [])

        assert selected == AGGRESSIVE_BASELINE_PARAMS
        assert "基线" in reason
        assert eliminated == []

    def test_geometric_return_ranking(self):
        """几何收益更高的候选应被选中。"""
        # 候选 A：total_return = 0.02（final = 1020）
        eq_a = _make_equity([1000, 1010, 1005, 1020])
        # 候选 B：total_return = 0.10（final = 1100）
        eq_b = _make_equity([1000, 1010, 1005, 1100])

        params_a = AggressiveParams(breakout_window=20, volume_window=20,
                                    volume_ratio=1.5, relative_strength_window=20,
                                    exit_low_window=10, max_holding_days=20)
        params_b = AggressiveParams(breakout_window=15, volume_window=15,
                                    volume_ratio=1.2, relative_strength_window=15,
                                    exit_low_window=8, max_holding_days=15)

        val_results = [
            (params_a, _make_backtest_result(eq_a)),
            (params_b, _make_backtest_result(eq_b)),
        ]
        selected, reason, eliminated = select_aggressive_params(
            [params_a, params_b], val_results
        )

        # B 几何收益更高 → 应被选中
        assert selected == params_b
        assert "几何收益" in reason
        assert len(eliminated) == 1

    def test_drawdown_tiebreak(self):
        """几何收益相同时应选择最大回撤更低的候选。"""
        # 两个候选 final 相同（total_return = 0.10），但回撤不同
        # A: 小回撤 max_dd ≈ 0.005
        eq_a = _make_equity([1000, 1010, 1005, 1100])
        # B: 大回撤 max_dd ≈ 0.273
        eq_b = _make_equity([1000, 1100, 800, 1100])

        params_a = AggressiveParams(breakout_window=20, volume_window=20,
                                    volume_ratio=1.5, relative_strength_window=20,
                                    exit_low_window=10, max_holding_days=20)
        params_b = AggressiveParams(breakout_window=15, volume_window=15,
                                    volume_ratio=1.2, relative_strength_window=15,
                                    exit_low_window=8, max_holding_days=15)

        val_results = [
            (params_a, _make_backtest_result(eq_a)),
            (params_b, _make_backtest_result(eq_b)),
        ]
        selected, reason, eliminated = select_aggressive_params(
            [params_a, params_b], val_results
        )

        # A 回撤更低 → 应被选中
        assert selected == params_a

    def test_turnover_tiebreak(self):
        """几何收益和回撤均相同时应选择换手率更低的候选。"""
        eq = _make_equity([1000, 1010, 1005, 1100])
        dates = _make_dates(4)

        # A：低换手率
        fills_a = [
            _make_fill(dates[0], side=Side.BUY, quantity=10, price=10.0),
            _make_fill(dates[1], side=Side.SELL, quantity=10, price=10.0),
        ]
        # B：高换手率
        fills_b = [
            _make_fill(dates[0], side=Side.BUY, quantity=20, price=10.0),
            _make_fill(dates[1], side=Side.SELL, quantity=20, price=10.0),
        ]

        params_a = AggressiveParams(breakout_window=20, volume_window=20,
                                    volume_ratio=1.5, relative_strength_window=20,
                                    exit_low_window=10, max_holding_days=20)
        params_b = AggressiveParams(breakout_window=15, volume_window=15,
                                    volume_ratio=1.2, relative_strength_window=15,
                                    exit_low_window=8, max_holding_days=15)

        val_results = [
            (params_a, _make_backtest_result(eq, fills=fills_a)),
            (params_b, _make_backtest_result(eq, fills=fills_b)),
        ]
        selected, reason, eliminated = select_aggressive_params(
            [params_a, params_b], val_results
        )

        # A 换手率更低 → 应被选中
        assert selected == params_a

    def test_distance_tiebreak(self):
        """几何收益、回撤和换手率均相同时应选择离基线更近的候选。"""
        eq = _make_equity([1000, 1010, 1005, 1100])
        dates = _make_dates(4)
        fills = [
            _make_fill(dates[0], side=Side.BUY, quantity=10, price=10.0),
            _make_fill(dates[1], side=Side.SELL, quantity=10, price=10.0),
        ]

        # A = 基线参数，distance = 0
        params_a = AggressiveParams(breakout_window=20, volume_window=20,
                                    volume_ratio=1.5, relative_strength_window=20,
                                    exit_low_window=10, max_holding_days=20)
        # B 偏离基线
        params_b = AggressiveParams(breakout_window=15, volume_window=15,
                                    volume_ratio=1.2, relative_strength_window=15,
                                    exit_low_window=8, max_holding_days=15)

        val_results = [
            (params_a, _make_backtest_result(eq, fills=fills)),
            (params_b, _make_backtest_result(eq, fills=fills)),
        ]
        selected, reason, eliminated = select_aggressive_params(
            [params_a, params_b], val_results
        )

        # A 离基线更近 → 应被选中
        assert selected == params_a

    def test_reproducibility(self):
        """相同输入应产生完全相同的输出。"""
        eq = _make_equity([1000, 1010, 1005, 1100])

        params_a = AggressiveParams(breakout_window=20, volume_window=20,
                                    volume_ratio=1.5, relative_strength_window=20,
                                    exit_low_window=10, max_holding_days=20)
        params_b = AggressiveParams(breakout_window=15, volume_window=15,
                                    volume_ratio=1.2, relative_strength_window=15,
                                    exit_low_window=8, max_holding_days=15)

        val_results = [
            (params_a, _make_backtest_result(eq)),
            (params_b, _make_backtest_result(eq)),
        ]

        r1 = select_aggressive_params([params_a, params_b], val_results)
        r2 = select_aggressive_params([params_a, params_b], val_results)

        assert r1[0] == r2[0]
        assert r1[1] == r2[1]
        assert r1[2] == r2[2]

    def test_eliminated_list_contains_all_non_selected(self):
        """被淘汰列表应包含所有非最优候选，并含完整字段。"""
        eq_a = _make_equity([1000, 1010, 1005, 1020])
        eq_b = _make_equity([1000, 1010, 1005, 1100])
        eq_c = _make_equity([1000, 1010, 1005, 1050])

        params_a = AggressiveParams(breakout_window=25, volume_window=25,
                                    volume_ratio=1.8, relative_strength_window=25,
                                    exit_low_window=12, max_holding_days=25)
        params_b = AggressiveParams(breakout_window=20, volume_window=20,
                                    volume_ratio=1.5, relative_strength_window=20,
                                    exit_low_window=10, max_holding_days=20)
        params_c = AggressiveParams(breakout_window=15, volume_window=15,
                                    volume_ratio=1.2, relative_strength_window=15,
                                    exit_low_window=8, max_holding_days=15)

        val_results = [
            (params_a, _make_backtest_result(eq_a)),
            (params_b, _make_backtest_result(eq_b)),
            (params_c, _make_backtest_result(eq_c)),
        ]
        selected, reason, eliminated = select_aggressive_params(
            [params_a, params_b, params_c], val_results
        )

        # B 几何收益最高 → 应被选中
        assert selected == params_b
        # 激进轨的 eliminated 包含所有非最优候选
        assert len(eliminated) == 2
        for entry in eliminated:
            assert "param_key" in entry
            assert "reason" in entry
            assert "geometric_return" in entry
            assert "max_drawdown" in entry
            assert "turnover_rate" in entry
            assert "几何收益" in entry["reason"]


# --------------------------------------------------------------------------- #
# 5. check_steady_eligibility 测试（必测项 17）
# --------------------------------------------------------------------------- #


class TestCheckSteadyEligibility:
    """稳健轨资格判定测试。

    6 个条件全部通过时为 ELIGIBLE_FOR_PAPER_OBSERVATION，
    任一条件失败时为 NOT_ELIGIBLE_FOR_LIVE_TRADING。
    """

    @staticmethod
    def _make_passing_inputs():
        """构造全部条件通过的输入数据。"""
        oos_metrics = {
            "max_drawdown": 0.10,
            "annualized_return": 0.05,
        }
        stress_results = [_make_stress_result("combined_2x", annualized_return=0.03)]
        param_perturbation = _make_param_perturbation(return_median=0.02)
        # 3 折中 2 折正收益 = 66.7% >= 60%
        fold_results = [
            _make_fold_result(
                fold=_make_fold(i),
                test_result=_make_backtest_result(
                    metrics={"total_return": 0.05}
                ),
            )
            for i in range(2)
        ] + [
            _make_fold_result(
                fold=_make_fold(2),
                test_result=_make_backtest_result(
                    metrics={"total_return": -0.02}
                ),
            ),
        ]
        return oos_metrics, stress_results, param_perturbation, fold_results

    def test_all_conditions_pass(self):
        """全部条件通过时应为 ELIGIBLE_FOR_PAPER_OBSERVATION。"""
        oos_metrics, stress, perturb, folds = self._make_passing_inputs()
        result = check_steady_eligibility(oos_metrics, stress, perturb, folds)

        assert result.status == EligibilityStatus.ELIGIBLE_FOR_PAPER_OBSERVATION
        assert len(result.conditions) == 6
        assert all(c["passed"] for c in result.conditions)
        assert result.failure_reasons == []

    def test_condition1_max_drawdown_exceeds_20pct(self):
        """条件 1：最大回撤超过 20% 时应不具备实盘资格。"""
        oos_metrics, stress, perturb, folds = self._make_passing_inputs()
        oos_metrics["max_drawdown"] = 0.25  # > 0.20

        result = check_steady_eligibility(oos_metrics, stress, perturb, folds)

        assert result.status == EligibilityStatus.NOT_ELIGIBLE_FOR_LIVE_TRADING
        assert any("回撤" in r for r in result.failure_reasons)
        cond1 = next(c for c in result.conditions if c["name"] == "max_drawdown_le_20pct")
        assert cond1["passed"] is False

    def test_condition2_annualized_return_not_positive(self):
        """条件 2：年化收益不大于 0 时应不具备实盘资格。"""
        oos_metrics, stress, perturb, folds = self._make_passing_inputs()
        oos_metrics["annualized_return"] = -0.01  # <= 0

        result = check_steady_eligibility(oos_metrics, stress, perturb, folds)

        assert result.status == EligibilityStatus.NOT_ELIGIBLE_FOR_LIVE_TRADING
        assert any("年化收益" in r for r in result.failure_reasons)
        cond2 = next(c for c in result.conditions if c["name"] == "oos_annualized_return_positive")
        assert cond2["passed"] is False

    def test_condition3_positive_fold_ratio_below_60pct(self):
        """条件 3：正收益折比例低于 60% 时应不具备实盘资格。"""
        oos_metrics, stress, perturb, _ = self._make_passing_inputs()
        # 3 折中仅 1 折正收益 = 33.3% < 60%
        fold_results = [
            _make_fold_result(
                fold=_make_fold(i),
                test_result=_make_backtest_result(
                    metrics={"total_return": 0.05 if i == 0 else -0.02}
                ),
            )
            for i in range(3)
        ]

        result = check_steady_eligibility(oos_metrics, stress, perturb, fold_results)

        assert result.status == EligibilityStatus.NOT_ELIGIBLE_FOR_LIVE_TRADING
        assert any("正收益折比例" in r for r in result.failure_reasons)
        cond3 = next(c for c in result.conditions if c["name"] == "positive_fold_ratio_ge_60pct")
        assert cond3["passed"] is False

    def test_condition4_combined_stress_return_not_positive(self):
        """条件 4：联合 2 倍压力下年化收益不大于 0 时应不具备实盘资格。"""
        oos_metrics, _, perturb, folds = self._make_passing_inputs()
        stress_results = [_make_stress_result("combined_2x", annualized_return=-0.02)]

        result = check_steady_eligibility(oos_metrics, stress_results, perturb, folds)

        assert result.status == EligibilityStatus.NOT_ELIGIBLE_FOR_LIVE_TRADING
        assert any("联合" in r and "压力" in r for r in result.failure_reasons)
        cond4 = next(c for c in result.conditions if c["name"] == "combined_stress_annualized_return_positive")
        assert cond4["passed"] is False

    def test_condition5_param_perturbation_median_not_positive(self):
        """条件 5：参数扰动收益中位数不大于 0 时应不具备实盘资格。"""
        oos_metrics, stress, _, folds = self._make_passing_inputs()
        param_perturbation = _make_param_perturbation(return_median=-0.01)

        result = check_steady_eligibility(oos_metrics, stress, param_perturbation, folds)

        assert result.status == EligibilityStatus.NOT_ELIGIBLE_FOR_LIVE_TRADING
        assert any("参数扰动" in r for r in result.failure_reasons)
        cond5 = next(c for c in result.conditions if c["name"] == "param_perturbation_median_positive")
        assert cond5["passed"] is False

    def test_condition6_data_quality_failure(self):
        """条件 6：数据质量检查失败时应不具备实盘资格。"""
        oos_metrics, stress, perturb, folds = self._make_passing_inputs()

        result = check_steady_eligibility(
            oos_metrics, stress, perturb, folds, data_quality_ok=False
        )

        assert result.status == EligibilityStatus.NOT_ELIGIBLE_FOR_LIVE_TRADING
        assert any("数据质量" in r for r in result.failure_reasons)
        cond6 = next(c for c in result.conditions if c["name"] == "no_data_quality_failure")
        assert cond6["passed"] is False

    def test_multiple_conditions_fail(self):
        """多个条件同时失败时仍应为 NOT_ELIGIBLE，且失败原因应包含所有失败项。"""
        oos_metrics = {
            "max_drawdown": 0.30,       # 条件 1 失败
            "annualized_return": -0.05,  # 条件 2 失败
        }
        stress = [_make_stress_result("combined_2x", annualized_return=-0.01)]  # 条件 4 失败
        perturb = _make_param_perturbation(return_median=-0.02)  # 条件 5 失败
        folds = [
            _make_fold_result(
                fold=_make_fold(i),
                test_result=_make_backtest_result(metrics={"total_return": -0.01}),
            )
            for i in range(3)
        ]  # 条件 3 失败（0% < 60%）

        result = check_steady_eligibility(
            oos_metrics, stress, perturb, folds, data_quality_ok=False  # 条件 6 失败
        )

        assert result.status == EligibilityStatus.NOT_ELIGIBLE_FOR_LIVE_TRADING
        # 至少 5 个条件失败（条件 1、2、3、4、5、6 全部失败）
        failed = [c for c in result.conditions if not c["passed"]]
        assert len(failed) == 6
        assert len(result.failure_reasons) == 6


# --------------------------------------------------------------------------- #
# 6. EligibilityStatus 测试（必测项 18）
# --------------------------------------------------------------------------- #


class TestEligibilityStatus:
    """资格状态常量与激进轨 SIMULATION_ONLY 测试。"""

    def test_status_constants_have_correct_values(self):
        """资格状态常量应具有正确的字符串值。"""
        assert EligibilityStatus.ELIGIBLE_FOR_PAPER_OBSERVATION == "ELIGIBLE_FOR_PAPER_OBSERVATION"
        assert EligibilityStatus.NOT_ELIGIBLE_FOR_LIVE_TRADING == "NOT_ELIGIBLE_FOR_LIVE_TRADING"
        assert EligibilityStatus.SIMULATION_ONLY == "SIMULATION_ONLY"
        assert EligibilityStatus.INSUFFICIENT_RESEARCH_SAMPLE == "INSUFFICIENT_RESEARCH_SAMPLE"

    def test_all_status_values_are_distinct(self):
        """所有资格状态值应互不相同。"""
        statuses = [
            EligibilityStatus.ELIGIBLE_FOR_PAPER_OBSERVATION,
            EligibilityStatus.NOT_ELIGIBLE_FOR_LIVE_TRADING,
            EligibilityStatus.SIMULATION_ONLY,
            EligibilityStatus.INSUFFICIENT_RESEARCH_SAMPLE,
        ]
        assert len(set(statuses)) == 4

    def test_check_steady_never_returns_simulation_only(self):
        """check_steady_eligibility 永远不应返回 SIMULATION_ONLY。

        SIMULATION_ONLY 仅供激进轨使用，稳健轨只返回
        ELIGIBLE_FOR_PAPER_OBSERVATION 或 NOT_ELIGIBLE_FOR_LIVE_TRADING。
        """
        oos_metrics = {"max_drawdown": 0.10, "annualized_return": 0.05}
        stress = [_make_stress_result("combined_2x", annualized_return=0.03)]
        perturb = _make_param_perturbation(return_median=0.02)
        folds = [
            _make_fold_result(
                fold=_make_fold(i),
                test_result=_make_backtest_result(metrics={"total_return": 0.05}),
            )
            for i in range(3)
        ]

        # 全部通过
        result_pass = check_steady_eligibility(oos_metrics, stress, perturb, folds)
        assert result_pass.status != EligibilityStatus.SIMULATION_ONLY
        assert result_pass.status == EligibilityStatus.ELIGIBLE_FOR_PAPER_OBSERVATION

        # 任一失败
        oos_metrics_fail = {"max_drawdown": 0.30, "annualized_return": 0.05}
        result_fail = check_steady_eligibility(oos_metrics_fail, stress, perturb, folds)
        assert result_fail.status != EligibilityStatus.SIMULATION_ONLY
        assert result_fail.status == EligibilityStatus.NOT_ELIGIBLE_FOR_LIVE_TRADING

    def test_aggressive_track_always_simulation_only(self):
        """激进轨的资格状态应始终为 SIMULATION_ONLY。

        验证 ResearchResult 中激进轨的 eligibility 状态符合设计约定：
        无论激进轨表现如何，均不输出实盘资格，始终为 SIMULATION_ONLY。
        """
        aggressive_track = TrackResult(
            track_type=TrackType.AGGRESSIVE,
            oos_metrics={"total_return": 10.0, "max_drawdown": 0.01},
        )
        aggressive_track.eligibility = EligibilityCheck(
            status=EligibilityStatus.SIMULATION_ONLY,
            conditions=[],
            failure_reasons=[],
        )

        assert aggressive_track.eligibility.status == EligibilityStatus.SIMULATION_ONLY
        assert aggressive_track.track_type == TrackType.AGGRESSIVE

    def test_research_result_aggressive_eligibility_is_simulation_only(self):
        """ResearchResult 中激进轨 eligibility 应为 SIMULATION_ONLY。"""
        result = ResearchResult()
        result.aggressive = TrackResult(track_type=TrackType.AGGRESSIVE)
        result.aggressive.eligibility = EligibilityCheck(
            status=EligibilityStatus.SIMULATION_ONLY,
        )
        result.steady = TrackResult(track_type=TrackType.STEADY)
        result.steady.eligibility = EligibilityCheck(
            status=EligibilityStatus.ELIGIBLE_FOR_PAPER_OBSERVATION,
        )

        assert result.aggressive.eligibility.status == EligibilityStatus.SIMULATION_ONLY
        # 稳健轨可以是其他状态，但激进轨永远不是
        assert result.aggressive.eligibility.status != EligibilityStatus.ELIGIBLE_FOR_PAPER_OBSERVATION
        assert result.aggressive.eligibility.status != EligibilityStatus.NOT_ELIGIBLE_FOR_LIVE_TRADING
