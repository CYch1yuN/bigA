"""FR-19 测试：真实完整周研究步骤（复用 Phase 3 ResearchRunner）。

包含：
1. ``TestRealResearchRunner`` —— **真实（非 mock）** ``ResearchRunner`` 测试：
   不替换 ``BacktestEngine.run``，跑真实的滚动样本外验证，断言真实产物
   （折、样本外权益、压力、参数扰动、蒙特卡洛、市场阶段、资格判定）。
2. ``TestSummarizeResearch`` —— 对真实结果做摘要提取，验证 81-729 / MC-10000 /
   压力 / 制度分析字段被正确 trim 进 ``ResearchSummary``。
3. ``TestCanonicalConfig`` —— 验证正统配方默认即完整 81-729 参数集 + MC-10000。
4. ``TestWeeklyResearchStep`` —— 步骤接线（快速，注入式 loader + 退化 runner）：
   数据缺失优雅跳过、运行异常优雅捕获。
5. ``TestWeeklyPipelineIntegration`` —— 把 weekly_research 步骤接入每周管线，
   无参考数据时降级跳过。

性能说明
--------
真实引擎单次回测约 2 秒，1 折完整研究约 40-50 秒（项目内
``test_research_integration`` 正是因为真实引擎超时 >120s 才 mock）。
因此真实测试用 module-scoped fixture 只跑一次，并搭配极小候选集（每轨 1 组合）
把参数选择 / 扰动的 810 次回测压到 1 次，使测试在 ~1 分钟内稳定完成。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from ashare_quant.automation.weekly_research import (
    ResearchInputs,
    ResearchSummary,
    _resolve_candidates,
    build_research_runner,
    execute_weekly_research,
    run_weekly_research_step,
    summarize_research,
)
from ashare_quant.backtest.config import BacktestConfig
from ashare_quant.research.analysis import ResearchResult, ResearchRunner
from ashare_quant.research.benchmarks import BenchmarkData
from ashare_quant.research.monte_carlo import MonteCarloConfig
from ashare_quant.research.strategies import (
    AGGRESSIVE_PARAM_CANDIDATES,
    STEADY_PARAM_CANDIDATES,
    generate_aggressive_param_combinations,
    generate_steady_param_combinations,
)
from ashare_quant.research.universe import HistoricalStatusTable
from ashare_quant.research.walk_forward import WalkForwardConfig
from tests.research_samples import (
    make_benchmark_data,
    make_historical_status_table,
    make_research_quotes,
    make_trade_dates_range,
)


# --------------------------------------------------------------------------- #
# 数据装配
# --------------------------------------------------------------------------- #


def _build_inputs(start: date, end: date, n_stocks: int = 5) -> ResearchInputs:
    dates = make_trade_dates_range(start, end)
    quotes = make_research_quotes(start=start, n_days=len(dates), n_stocks=n_stocks)
    status_df = make_historical_status_table(start=start, n_stocks=n_stocks)
    bench_df = make_benchmark_data(start=start, n_days=len(dates))
    bench_dates = [pd.Timestamp(d).date() for d in bench_df["trade_date"]]
    bench = BenchmarkData(
        trade_dates=bench_dates,
        hs300_close={
            d: c
            for d, c in zip(bench_dates, bench_df["hs300_close"].tolist())
            if c == c  # 排除 NaN
        },
        csi_all_close={
            d: c
            for d, c in zip(bench_dates, bench_df["csi_all_close"].tolist())
            if c == c
        },
    )
    return ResearchInputs(
        quotes=quotes,
        benchmark=bench,
        status_table=HistoricalStatusTable(records=status_df),
    )


# 极小候选集：每轨 1 组合 -> 参数选择 + 扰动各 1 次引擎调用（真实引擎但极快）
_TINY_STEADY = {
    "trend_window": [20], "momentum_window": [20],
    "volatility_window": [20], "minimum_score": [0.0],
}
_TINY_AGGRESSIVE = {
    "breakout_window": [20], "volume_window": [20], "volume_ratio": [2.0],
    "relative_strength_window": [20], "exit_low_window": [20], "max_holding_days": [20],
}


@pytest.fixture(scope="module")
def real_result() -> ResearchResult:
    """跑一次**真实** ``ResearchRunner``（不 mock 引擎），供多个测试共享。"""
    inputs = _build_inputs(date(2019, 1, 1), date(2020, 12, 31), n_stocks=5)
    mc = MonteCarloConfig(
        n_paths=200, random_seed=42, path_length=120, block_length=5,
        initial_capital=1000.0, ten_x_target=10_000.0,
        loss_50_threshold=500.0, near_zero_threshold=100.0,
    )
    wf = WalkForwardConfig(
        train_years=1, validation_months=3, test_years=1,
        step_years=1, min_total_years=2,
    )
    return execute_weekly_research(
        inputs,
        bt_config=BacktestConfig(initial_cash=1000.0),
        wf_config=wf,
        mc_config=mc,
        steady_candidates=_TINY_STEADY,
        aggressive_candidates=_TINY_AGGRESSIVE,
        initial_cash=1000.0,
    )


# --------------------------------------------------------------------------- #
# 1. 真实（非 mock）ResearchRunner 测试
# --------------------------------------------------------------------------- #


class TestRealResearchRunner:
    """核心验收：真实引擎、真实研究全流程、真实产物。"""

    def test_is_real_research_result(self, real_result: ResearchResult) -> None:
        assert isinstance(real_result, ResearchResult)

    def test_produces_real_folds_and_equity(self, real_result: ResearchResult) -> None:
        # 1 折（2 个完整日历年），样本外权益为真实非空序列
        assert len(real_result.folds) >= 1
        assert real_result.insufficient_sample is False
        assert len(real_result.steady.oos_equity) > 0
        assert len(real_result.aggressive.oos_equity) > 0

    def test_real_stress_and_perturbation(self, real_result: ResearchResult) -> None:
        # 压力测试 4 场景（baseline / fee_2x / slippage_2x / combined_2x）
        assert len(real_result.steady.stress_results) == 4
        assert len(real_result.aggressive.stress_results) == 4
        # 参数扰动：真实计算了分布
        assert real_result.steady.parameter_perturbation is not None
        assert real_result.steady.parameter_perturbation.total_combinations == 1
        assert real_result.aggressive.parameter_perturbation is not None
        assert real_result.aggressive.parameter_perturbation.total_combinations == 1

    def test_real_eligibility(self, real_result: ResearchResult) -> None:
        # 稳健轨：真实资格判定（样本充足 -> NOT_ELIGIBLE，非实盘）
        assert real_result.steady.eligibility is not None
        assert real_result.steady.eligibility.status == "NOT_ELIGIBLE_FOR_LIVE_TRADING"
        # 激进轨：永远 SIMULATION_ONLY
        assert real_result.aggressive.eligibility.status == "SIMULATION_ONLY"

    def test_real_monte_carlo_and_regime(self, real_result: ResearchResult) -> None:
        # 蒙特卡洛（MC-10000 配置风格）真实运行
        mc = real_result.aggressive.monte_carlo
        assert mc is not None
        assert mc.n_paths == 200
        assert 0.0 <= mc.prob_ten_x <= 1.0
        # 市场阶段分析真实运行
        assert real_result.aggressive.market_regime is not None
        assert len(real_result.aggressive.market_regime.regimes) > 0


# --------------------------------------------------------------------------- #
# 2. 摘要提取
# --------------------------------------------------------------------------- #


class TestSummarizeResearch:
    def test_summary_extracts_real_fields(self, real_result: ResearchResult) -> None:
        summary = summarize_research(
            real_result,
            data_hash="abc",
            config_hash="def",
            code_commit="fr19-test",
            candidate_counts={"steady": 1, "aggressive": 1},
        )
        assert isinstance(summary, ResearchSummary)
        assert summary.ran is True
        assert summary.insufficient_sample is False
        assert summary.folds == len(real_result.folds)
        assert summary.steady_eligibility == "NOT_ELIGIBLE_FOR_LIVE_TRADING"
        assert summary.aggressive_eligibility == "SIMULATION_ONLY"
        assert summary.stress["steady"]  # 非空
        assert summary.stress["aggressive"]
        assert summary.monte_carlo["n_paths"] == 200
        assert summary.regime["day_count"] > 0
        assert summary.perturbation["steady"]["total_combinations"] == 1
        assert summary.data_hash == "abc"
        assert summary.config_hash == "def"
        assert summary.code_commit == "fr19-test"
        assert summary.candidate_counts == {"steady": 1, "aggressive": 1}
        # JSON 可序列化
        import json
        json.dumps(summary.to_dict(), ensure_ascii=False)

    def test_summary_serializes_cleanly(self, real_result: ResearchResult) -> None:
        summary = summarize_research(real_result)
        payload = summary.to_dict()
        assert isinstance(payload, dict)
        assert "ran" in payload and "stress" in payload


# --------------------------------------------------------------------------- #
# 3. 正统配方：默认即完整 81-729 参数集 + MC-10000
# --------------------------------------------------------------------------- #


class TestCanonicalConfig:
    def test_param_grid_sizes(self) -> None:
        assert len(generate_steady_param_combinations(None)) == 81
        assert len(generate_aggressive_param_combinations(None)) == 729

    def test_resolve_candidates_defaults(self) -> None:
        _, _, n_steady, n_aggr = _resolve_candidates(None, None)
        assert n_steady == 81
        assert n_aggr == 729

    def test_builder_uses_full_grid_by_default(self) -> None:
        inputs = _build_inputs(date(2019, 1, 1), date(2020, 12, 31), n_stocks=5)
        runner = build_research_runner(inputs)
        assert isinstance(runner, ResearchRunner)
        # 不传候选集时，构建器使用代码内置的完整 81-729 默认网格
        assert runner._steady_candidates is STEADY_PARAM_CANDIDATES
        assert runner._aggressive_candidates is AGGRESSIVE_PARAM_CANDIDATES
        assert (
            len(generate_steady_param_combinations(runner._steady_candidates)) == 81
        )
        assert (
            len(generate_aggressive_param_combinations(runner._aggressive_candidates)) == 729
        )
        # 蒙特卡洛默认 10,000 路径
        assert runner._mc_config.n_paths == 10_000


# --------------------------------------------------------------------------- #
# 4. 步骤接线（快速，注入式）
# --------------------------------------------------------------------------- #


class _FakeStep:
    def __init__(self, name: str) -> None:
        self.name = name
        self.detail: dict = {}


class _StepCM:
    def __init__(self, step: _FakeStep) -> None:
        self._step = step

    def __enter__(self) -> _FakeStep:
        return self._step

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakeCtx:
    def __init__(self) -> None:
        self.scratch: dict = {}
        self.dry_run = False
        self.steps: list[_FakeStep] = []
        self.warnings: list = []
        self.logger = self

    def step(self, name: str) -> _StepCM:
        s = _FakeStep(name)
        self.steps.append(s)
        return _StepCM(s)

    def warning(self, code: str, msg: str, **kw: object) -> None:
        self.warnings.append((code, msg))


class _Loader:
    def __init__(self, inputs: ResearchInputs | None) -> None:
        self._inputs = inputs

    def load(self) -> ResearchInputs | None:
        return self._inputs


def _degenerate_runner(inputs: ResearchInputs, **kwargs: object) -> ResearchResult:
    """强制样本不足 -> 无折 -> 引擎 0 次调用（瞬时、真实对象）。

    注意：必须赋值覆盖（而非 setdefault）——调用方可能已把 wf_config=None
    传入，setdefault 不会替换已有的 None。
    """
    kwargs["wf_config"] = WalkForwardConfig(min_total_years=10)
    kwargs["steady_candidates"] = _TINY_STEADY
    kwargs["aggressive_candidates"] = _TINY_AGGRESSIVE
    return execute_weekly_research(inputs, **kwargs)


class TestWeeklyResearchStep:
    def test_runs_and_records_summary(self, tmp_path: Path) -> None:
        inputs = _build_inputs(date(2019, 1, 1), date(2020, 12, 31), n_stocks=5)
        ctx = _FakeCtx()
        summary = run_weekly_research_step(
            ctx,
            research_dir=tmp_path / "research_data",
            loader=_Loader(inputs),
            runner_factory=_degenerate_runner,
        )
        assert summary is not None
        assert summary.ran is True
        assert summary.insufficient_sample is True  # 退化配置 -> 样本不足
        assert summary.folds == 0
        assert ctx.scratch.get("weekly_research") == summary.to_dict()
        # 步骤已登记
        assert any(s.name == "weekly_research" for s in ctx.steps)
        assert ctx.steps[-1].detail.get("ran") is True

    def test_missing_data_skips_gracefully(self, tmp_path: Path) -> None:
        ctx = _FakeCtx()
        summary = run_weekly_research_step(
            ctx,
            research_dir=tmp_path / "research_data",
            loader=_Loader(None),  # 模拟三件套缺失
            runner_factory=_degenerate_runner,
        )
        assert summary is not None
        assert summary.ran is False
        assert summary.skipped_reason is not None
        assert ctx.scratch.get("weekly_research", {}).get("ran") is False

    def test_runner_error_captured(self, tmp_path: Path) -> None:
        inputs = _build_inputs(date(2019, 1, 1), date(2020, 12, 31), n_stocks=5)

        def _boom(inputs: ResearchInputs, **kwargs: object) -> ResearchResult:
            raise RuntimeError("research blew up")

        ctx = _FakeCtx()
        summary = run_weekly_research_step(
            ctx,
            research_dir=tmp_path / "research_data",
            loader=_Loader(inputs),
            runner_factory=_boom,
        )
        assert summary is not None
        assert summary.ran is False
        assert summary.error is not None
        assert "research blew up" in summary.error
        assert ctx.warnings  # 记录了 warning


# --------------------------------------------------------------------------- #
# 5. 接入每周管线（无参考数据时降级跳过）
# --------------------------------------------------------------------------- #


class TestWeeklyPipelineIntegration:
    def test_step_wired_and_skips_without_data(self, tmp_path: Path) -> None:
        # 复用与 test_phase4_automation 相同的离线配置构造方式
        from ashare_quant.automation.config import (
            AccountConfig,
            AutomationConfig,
            DataConfig,
            LoggingConfig,
            PathsConfig,
        )
        from ashare_quant.automation.models import EligibilityStatus, StrategyTrack
        from ashare_quant.automation.weekly import WeeklyPipeline, run_weekly

        config = AutomationConfig(
            paths=PathsConfig(
                data_dir="data", state_dir="state", reports_dir="reports",
                logs_dir="logs", archive_dir="reports/archive",
            ),
            data=DataConfig(symbols=[], lookback_days=200),
            logging=LoggingConfig(console=False),
            accounts=[
                AccountConfig(
                    account_id="paper-steady", track=StrategyTrack.STEADY,
                    initial_cash=1000.0,
                    eligibility_status=EligibilityStatus.NOT_ELIGIBLE_FOR_LIVE_TRADING,
                ),
                AccountConfig(
                    account_id="paper-aggressive", track=StrategyTrack.AGGRESSIVE,
                    initial_cash=1000.0,
                    eligibility_status=EligibilityStatus.SIMULATION_ONLY,
                ),
            ],
        ).with_base_dir(tmp_path)

        quotes = make_research_quotes(
            start=date(2020, 1, 2), n_days=200, n_stocks=8
        )
        trade_dates = sorted(
            {pd.Timestamp(d).date() for d in quotes["trade_date"]}
        )
        from ashare_quant.automation.calendar import TradingCalendar
        cal = TradingCalendar.from_dates(trade_dates, source="synthetic")

        out = run_weekly(
            config,
            as_of_date=date(2020, 6, 6),  # 一个周六
            pipeline=WeeklyPipeline(calendar=cal),
        )

        step_names = [s.name for s in out.record.steps]
        assert "weekly_research" in step_names
        research_step = next(s for s in out.record.steps if s.name == "weekly_research")
        # 默认 research_dir 不存在 -> 优雅跳过（不致命）
        assert research_step.detail.get("ran") is False
        assert research_step.detail.get("skipped_reason")
