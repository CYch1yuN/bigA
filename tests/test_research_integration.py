"""Integration tests for the Phase 3 ResearchRunner class.

Covers ResearchRunner and run_walk_forward in ``src/ashare_quant/research/analysis.py``
(lines 706-1304) that are not exercised by the existing unit tests in
``test_research_analysis.py``.

All tests are deterministic and use small synthetic data to keep the total
runtime under 120 seconds.  Module-scoped fixtures ensure the expensive
walk-forward pipeline (including 81 + 729 parameter-perturbation backtests)
runs only once per fixture.

Performance strategy
--------------------
The ResearchRunner calls ``BacktestEngine.run()`` ~2 442 times (parameter
selection on validation data, test runs, stress tests, parameter
perturbation).  Each real engine call iterates over ~244 trading days and
creates two DataFrame copies per day (one in the engine main-loop
``data[data["trade_date"] <= dt]``, one in the strategy's
``bars[bars["trade_date"] <= dt]``).  That is over one million DataFrame
copies, which far exceeds the 120-second timeout.

Because the test data has fewer than 5 eligible stocks (the strategy
threshold), strategies never generate buy signals and the real engine
would also produce flat equity (no trades).  We therefore replace
``BacktestEngine.run`` with a fast mock that returns a valid
``BacktestResult`` with flat daily equity and pre-computed metrics,
skipping the expensive per-day processing entirely.

All ResearchRunner code paths are still fully exercised: parameter
selection, stress tests, parameter perturbation, Monte Carlo, eligibility
checks, benchmark comparison, market-regime analysis, etc.
"""
from __future__ import annotations

import bisect
from datetime import date, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from ashare_quant.backtest.config import BacktestConfig
from ashare_quant.backtest.engine import BacktestEngine
from ashare_quant.backtest.metrics import MetricsCalculator
from ashare_quant.backtest.models import (
    BacktestResult,
    EligibilityDecision,
    PortfolioSnapshot,
    to_decimal,
)
from ashare_quant.research.analysis import (
    EligibilityCheck,
    EligibilityStatus,
    FoldResult,
    ResearchResult,
    ResearchRunner,
    TrackResult,
    TrackType,
    run_walk_forward,
)
from ashare_quant.research.benchmarks import BenchmarkData
from ashare_quant.research.monte_carlo import MonteCarloConfig, MonteCarloResult
from ashare_quant.research.stress import (
    ParameterPerturbationResult,
    StressResult,
)
from ashare_quant.research.universe import (
    HistoricalStatusTable,
    HistoricalUniverseFilter,
)
from ashare_quant.research.walk_forward import (
    Fold,
    WalkForwardConfig,
    WalkForwardSplitter,
)
from tests.research_samples import (
    make_benchmark_dict,
    make_historical_status_table,
    make_research_quotes,
    make_trade_dates_range,
)


# --------------------------------------------------------------------------- #
# Fast engine mock
# --------------------------------------------------------------------------- #
# Replaces BacktestEngine.run with a version that returns a valid
# BacktestResult (flat equity, no trades) without running the expensive
# per-day main loop.  See module docstring for rationale.

_dates_cache: dict[int, list[date]] = {}


def _get_sorted_dates(data: pd.DataFrame) -> list[date]:
    """Return sorted unique ``trade_date`` values, cached by ``id(data)``.

    The ResearchRunner passes the same ``quotes`` DataFrame to every
    backtest, so the cache hits after the first call, making subsequent
    lookups O(1).
    """
    key = id(data)
    if key in _dates_cache:
        return _dates_cache[key]
    if data.empty or "trade_date" not in data.columns:
        result: list[date] = []
    else:
        result = sorted(
            d if isinstance(d, date) else pd.Timestamp(d).date()
            for d in data["trade_date"].unique()
        )
    _dates_cache[key] = result
    return result


def _fast_engine_run(
    self: BacktestEngine,
    data: pd.DataFrame,
    strategy: object,
    start_date: date,
    end_date: date,
    initial_cash: float,
    config: BacktestConfig,
    universe_filter: object | None = None,
    risk_manager: object | None = None,
    broker: object | None = None,
) -> BacktestResult:
    """Fast mock of ``BacktestEngine.run`` returning flat equity (no trades).

    Since test data has fewer than 5 eligible stocks, strategies never
    generate buy signals.  This mock returns the same result the real
    engine would produce (flat equity at ``initial_cash``, empty
    orders/fills) but without the expensive per-day DataFrame
    boolean-indexing copies.

    A sorted-date cache and :func:`bisect` keep date-range extraction
    O(log n) per call.
    """
    all_dates = _get_sorted_dates(data)
    lo = bisect.bisect_left(all_dates, start_date)
    hi = bisect.bisect_right(all_dates, end_date)
    dates_in_range = all_dates[lo:hi]

    cash_dec = to_decimal(initial_cash)
    zero_dec = to_decimal("0")
    daily_equity = [
        PortfolioSnapshot(
            snapshot_date=dt,
            cash=cash_dec,
            position_value=zero_dec,
            total_equity=cash_dec,
            daily_pnl=zero_dec,
            cumulative_pnl=zero_dec,
            drawdown=zero_dec,
        )
        for dt in dates_in_range
    ]

    symbols: list[str] = (
        sorted(data["symbol"].unique().tolist())
        if not data.empty and "symbol" in data.columns
        else []
    )

    result = BacktestResult(
        config_summary=config.to_summary(),
        orders=[],
        fills=[],
        daily_equity=daily_equity,
        final_positions={},
        limitations=["Fast mock engine: no trades executed"],
        data_range={
            "start_date": str(start_date),
            "end_date": str(end_date),
            "trading_days": len(dates_in_range),
            "symbols": symbols,
        },
    )

    # Pre-compute metrics (the real engine does this at the end of run()).
    result.metrics = MetricsCalculator().calculate(result, to_decimal(initial_cash))

    return result


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True, scope="module")
def _patch_engine_run():
    """Patch ``BacktestEngine.run`` with the fast mock for this module only.

    The patch is applied via a module-scoped autouse fixture so it does
    not leak into other test modules.
    """
    original_run = BacktestEngine.run
    BacktestEngine.run = _fast_engine_run
    yield
    BacktestEngine.run = original_run


def _make_snapshot(
    d: date, total_equity: float, cash: float | None = None,
    position_value: float | None = None,
) -> PortfolioSnapshot:
    """Create a PortfolioSnapshot for _extract_daily_returns tests."""
    te = to_decimal(total_equity)
    c = to_decimal(cash) if cash is not None else te
    pv = to_decimal(position_value) if position_value is not None else to_decimal("0")
    return PortfolioSnapshot(
        snapshot_date=d, cash=c, position_value=pv, total_equity=te,
    )


def _build_env(
    start: date,
    end: date,
    wf_config: WalkForwardConfig,
    mc_config: MonteCarloConfig | None = None,
    n_stocks: int = 8,
) -> dict:
    """Build a full research environment dictionary.

    Shared by the module-scoped fixtures below to avoid code duplication.
    Uses 8 stocks (matching the user's spec).  Since the fast engine mock
    does not run the real engine, the number of stocks does not affect
    performance.
    """
    mc_config = mc_config or MonteCarloConfig(n_paths=10, random_seed=42)
    bt_config = BacktestConfig(initial_cash=1000.0)

    dates = make_trade_dates_range(start, end)
    quotes = make_research_quotes(start=start, n_days=len(dates), n_stocks=n_stocks)
    status_df = make_historical_status_table(start=start, n_stocks=n_stocks)
    status_table = HistoricalStatusTable(records=status_df)
    benchmark = make_benchmark_dict(start=start, n_days=len(dates))

    # Synthetic stocks have volume=300000 and price ~5, giving daily amount
    # ~1,500,000.  Lower the threshold from 20M to 1M so stocks are eligible.
    universe_filter = HistoricalUniverseFilter(
        status_table=status_table,
        quotes=quotes,
        min_listing_days=120,
        min_valid_days=15,
        valid_days_window=20,
        min_turnover=1_000_000.0,
        turnover_window=20,
        lot_size=100,
        available_cash=1000.0,
    )

    return {
        "quotes": quotes,
        "trading_dates": dates,
        "bt_config": bt_config,
        "benchmark": benchmark,
        "universe_filter": universe_filter,
        "wf_config": wf_config,
        "mc_config": mc_config,
    }


# --------------------------------------------------------------------------- #
# Module-scoped fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def research_env() -> dict:
    """Create the primary research test environment (module-scoped).

    3 calendar years (2019-2021), 8 stocks, small walk-forward config that
    produces 2 folds (fold 0: train=2019, test=2020; fold 1: train=2020,
    test=2021).
    """
    wf_config = WalkForwardConfig(
        train_years=1,
        validation_months=3,
        test_years=1,
        step_years=1,
        min_total_years=2,
    )
    mc_config = MonteCarloConfig(
        n_paths=10, random_seed=42, path_length=50, block_length=3,
    )
    return _build_env(
        start=date(2019, 1, 2),
        end=date(2021, 12, 31),
        wf_config=wf_config,
        mc_config=mc_config,
    )


@pytest.fixture(scope="module")
def research_result(research_env: dict) -> ResearchResult:
    """Run the full ResearchRunner pipeline once (module-scoped).

    This is the primary integration fixture.  The pipeline includes parameter
    selection (81 steady + 729 aggressive candidates per fold), stress tests,
    and parameter perturbation, so it is expensive with the real engine.
    With the fast mock, it completes in under 1 second.
    """
    env = research_env
    runner = ResearchRunner(
        bt_config=env["bt_config"],
        benchmark=env["benchmark"],
        universe_filter=env["universe_filter"],
        walk_forward_config=env["wf_config"],
        monte_carlo_config=env["mc_config"],
    )
    return runner.run(env["quotes"], env["trading_dates"], initial_cash=1000.0)


@pytest.fixture(scope="module")
def insufficient_short_result() -> ResearchResult:
    """Run with 1 year of data and min_total_years=5 (module-scoped, fast).

    Produces 0 folds because 1 complete calendar year < train_years +
    test_years = 2, so the 244-day block fallback also cannot create a fold.
    The sample is flagged as insufficient.
    """
    wf_config = WalkForwardConfig(
        train_years=1,
        validation_months=3,
        test_years=1,
        step_years=1,
        min_total_years=5,
    )
    env = _build_env(
        start=date(2019, 1, 2),
        end=date(2019, 12, 31),
        wf_config=wf_config,
    )
    runner = ResearchRunner(
        bt_config=env["bt_config"],
        benchmark=env["benchmark"],
        universe_filter=env["universe_filter"],
        walk_forward_config=env["wf_config"],
        monte_carlo_config=env["mc_config"],
    )
    return runner.run(env["quotes"], env["trading_dates"], initial_cash=1000.0)


# --------------------------------------------------------------------------- #
# 1. TestResearchRunnerInit
# --------------------------------------------------------------------------- #


class TestResearchRunnerInit:
    """Test the ResearchRunner constructor with various configurations."""

    def test_init_with_explicit_configs(self, research_env: dict):
        """Constructor should store all provided configs."""
        env = research_env
        runner = ResearchRunner(
            bt_config=env["bt_config"],
            benchmark=env["benchmark"],
            universe_filter=env["universe_filter"],
            walk_forward_config=env["wf_config"],
            monte_carlo_config=env["mc_config"],
        )
        assert runner._bt_config is env["bt_config"]
        assert runner._benchmark is env["benchmark"]
        assert runner._universe_filter is env["universe_filter"]
        assert runner._wf_config is env["wf_config"]
        assert runner._mc_config is env["mc_config"]

    def test_init_with_default_walk_forward_config(self, research_env: dict):
        """When walk_forward_config is None, a default WalkForwardConfig is used."""
        env = research_env
        runner = ResearchRunner(
            bt_config=env["bt_config"],
            benchmark=env["benchmark"],
            universe_filter=env["universe_filter"],
        )
        assert isinstance(runner._wf_config, WalkForwardConfig)
        # Default values from WalkForwardConfig
        assert runner._wf_config.train_years == 3
        assert runner._wf_config.test_years == 1
        assert runner._wf_config.min_total_years == 5

    def test_init_with_default_monte_carlo_config(self, research_env: dict):
        """When monte_carlo_config is None, a default MonteCarloConfig is used."""
        env = research_env
        runner = ResearchRunner(
            bt_config=env["bt_config"],
            benchmark=env["benchmark"],
            universe_filter=env["universe_filter"],
        )
        assert isinstance(runner._mc_config, MonteCarloConfig)
        assert runner._mc_config.n_paths == 10_000

    def test_init_creates_engine_and_calculator(self, research_env: dict):
        """Constructor should create a BacktestEngine and MetricsCalculator."""
        from ashare_quant.backtest.engine import BacktestEngine
        from ashare_quant.backtest.metrics import MetricsCalculator

        env = research_env
        runner = ResearchRunner(
            bt_config=env["bt_config"],
            benchmark=env["benchmark"],
            universe_filter=env["universe_filter"],
        )
        assert isinstance(runner._engine, BacktestEngine)
        assert isinstance(runner._calc, MetricsCalculator)


# --------------------------------------------------------------------------- #
# 2. TestResearchRunnerRun
# --------------------------------------------------------------------------- #


class TestResearchRunnerRun:
    """Full end-to-end run with a small config (uses module-scoped fixture)."""

    def test_returns_research_result(self, research_result: ResearchResult):
        """run() should return a ResearchResult instance."""
        assert isinstance(research_result, ResearchResult)

    def test_has_steady_and_aggressive_tracks(self, research_result: ResearchResult):
        """Result must contain both steady and aggressive TrackResult objects."""
        assert isinstance(research_result.steady, TrackResult)
        assert isinstance(research_result.aggressive, TrackResult)
        assert research_result.steady.track_type == TrackType.STEADY
        assert research_result.aggressive.track_type == TrackType.AGGRESSIVE

    def test_folds_are_generated(self, research_result: ResearchResult):
        """Two folds should be produced for 3 calendar years with train=1, test=1."""
        assert len(research_result.folds) == 2
        for i, fold in enumerate(research_result.folds):
            assert isinstance(fold, Fold)
            assert fold.fold_id == i

    def test_fold_test_periods_do_not_overlap(self, research_result: ResearchResult):
        """Test periods of different folds must not overlap."""
        folds = research_result.folds
        assert len(folds) >= 2
        for i in range(len(folds) - 1):
            assert folds[i].test_end < folds[i + 1].test_start

    def test_fold_dates_match_calendar_years(self, research_result: ResearchResult):
        """Fold 0: train=2019, test=2020; Fold 1: train=2020, test=2021."""
        f0, f1 = research_result.folds
        assert f0.train_start == date(2019, 1, 1)
        assert f0.train_end == date(2019, 12, 31)
        assert f0.test_start == date(2020, 1, 1)
        assert f0.test_end == date(2020, 12, 31)
        assert f1.train_start == date(2020, 1, 1)
        assert f1.train_end == date(2020, 12, 31)
        assert f1.test_start == date(2021, 1, 1)
        assert f1.test_end == date(2021, 12, 31)

    def test_steady_fold_results_populated(self, research_result: ResearchResult):
        """Each steady fold should have selected params and a selection reason."""
        steady = research_result.steady
        assert len(steady.folds) == 2
        for fr in steady.folds:
            assert isinstance(fr, FoldResult)
            assert isinstance(fr.selected_params, dict)
            assert len(fr.selected_params) > 0
            assert isinstance(fr.selection_reason, str)
            assert len(fr.selection_reason) > 0

    def test_steady_selected_params_have_expected_keys(self, research_result: ResearchResult):
        """Steady params should contain trend_window, momentum_window, etc."""
        for fr in research_result.steady.folds:
            keys = set(fr.selected_params.keys())
            assert "trend_window" in keys
            assert "momentum_window" in keys
            assert "volatility_window" in keys
            assert "minimum_score" in keys

    def test_aggressive_fold_results_populated(self, research_result: ResearchResult):
        """Each aggressive fold should have selected params and a selection reason."""
        aggressive = research_result.aggressive
        assert len(aggressive.folds) == 2
        for fr in aggressive.folds:
            assert isinstance(fr, FoldResult)
            assert isinstance(fr.selected_params, dict)
            assert len(fr.selected_params) > 0
            assert isinstance(fr.selection_reason, str)
            assert len(fr.selection_reason) > 0

    def test_aggressive_selected_params_have_expected_keys(self, research_result: ResearchResult):
        """Aggressive params should contain breakout_window, volume_window, etc."""
        for fr in research_result.aggressive.folds:
            keys = set(fr.selected_params.keys())
            assert "breakout_window" in keys
            assert "volume_window" in keys
            assert "volume_ratio" in keys
            assert "relative_strength_window" in keys
            assert "exit_low_window" in keys
            assert "max_holding_days" in keys

    def test_oos_equity_is_list_of_snapshots(self, research_result: ResearchResult):
        """oos_equity should be a list of PortfolioSnapshot (may be empty)."""
        for track in [research_result.steady, research_result.aggressive]:
            assert isinstance(track.oos_equity, list)
            for snap in track.oos_equity:
                assert isinstance(snap, PortfolioSnapshot)

    def test_oos_metrics_populated(self, research_result: ResearchResult):
        """oos_metrics should contain expected keys for both tracks."""
        expected_keys = {"total_return", "annualized_return", "max_drawdown",
                         "sharpe", "calmar", "trading_days"}
        for track in [research_result.steady, research_result.aggressive]:
            assert isinstance(track.oos_metrics, dict)
            assert expected_keys.issubset(set(track.oos_metrics.keys()))

    def test_eligibility_set_for_both_tracks(self, research_result: ResearchResult):
        """Both tracks should have an EligibilityCheck."""
        assert research_result.steady.eligibility is not None
        assert isinstance(research_result.steady.eligibility, EligibilityCheck)
        assert research_result.aggressive.eligibility is not None
        assert isinstance(research_result.aggressive.eligibility, EligibilityCheck)

    def test_aggressive_always_simulation_only(self, research_result: ResearchResult):
        """Aggressive track eligibility must always be SIMULATION_ONLY."""
        assert (
            research_result.aggressive.eligibility.status
            == EligibilityStatus.SIMULATION_ONLY
        )

    def test_steady_eligibility_is_paper_or_not_eligible(self, research_result: ResearchResult):
        """Steady track should be ELIGIBLE_FOR_PAPER_OBSERVATION or NOT_ELIGIBLE."""
        status = research_result.steady.eligibility.status
        assert status in (
            EligibilityStatus.ELIGIBLE_FOR_PAPER_OBSERVATION,
            EligibilityStatus.NOT_ELIGIBLE_FOR_LIVE_TRADING,
        )
        # Must never be SIMULATION_ONLY (that is aggressive-only)
        assert status != EligibilityStatus.SIMULATION_ONLY

    def test_steady_eligibility_has_six_conditions(self, research_result: ResearchResult):
        """Steady eligibility check should contain 6 conditions."""
        assert len(research_result.steady.eligibility.conditions) == 6

    def test_monte_carlo_populated_for_aggressive(self, research_result: ResearchResult):
        """Aggressive track should have Monte Carlo result if oos_equity exists."""
        aggressive = research_result.aggressive
        if aggressive.oos_equity:
            assert aggressive.monte_carlo is not None
            assert isinstance(aggressive.monte_carlo, MonteCarloResult)
            assert aggressive.monte_carlo.n_paths == 10
            assert aggressive.monte_carlo.random_seed == 42

    def test_monte_carlo_not_set_for_steady(self, research_result: ResearchResult):
        """Steady track should never have Monte Carlo."""
        assert research_result.steady.monte_carlo is None

    def test_limitations_present(self, research_result: ResearchResult):
        """Limitations list should contain at least 5 entries."""
        assert isinstance(research_result.limitations, list)
        assert len(research_result.limitations) >= 5
        # Check key limitation messages
        all_text = " ".join(research_result.limitations)
        assert "模拟研究" in all_text
        assert "SIMULATION_ONLY" in all_text

    def test_insufficient_sample_false(self, research_result: ResearchResult):
        """With 3 years and min_total_years=2, sample should be sufficient."""
        assert research_result.insufficient_sample is False

    def test_config_hash_data_hash_code_commit_are_none(self, research_result: ResearchResult):
        """Runner does not set hashes; they should be None."""
        assert research_result.config_hash is None
        assert research_result.data_hash is None
        assert research_result.code_commit is None

    def test_benchmark_comparison_populated(self, research_result: ResearchResult):
        """If oos_equity exists, benchmark_comparison should be populated."""
        for track in [research_result.steady, research_result.aggressive]:
            if track.oos_equity:
                assert isinstance(track.benchmark_comparison, dict)
                assert len(track.benchmark_comparison) > 0
                assert "hs300_return" in track.benchmark_comparison

    def test_fold_benchmark_returns_populated(self, research_result: ResearchResult):
        """Each fold result should have benchmark_returns."""
        for track in [research_result.steady, research_result.aggressive]:
            for fr in track.folds:
                assert isinstance(fr.benchmark_returns, dict)
                assert len(fr.benchmark_returns) > 0

    def test_market_regime_populated(self, research_result: ResearchResult):
        """If oos_equity exists, market_regime should be populated."""
        for track in [research_result.steady, research_result.aggressive]:
            if track.oos_equity:
                assert track.market_regime is not None

    def test_oos_equity_no_duplicate_dates(self, research_result: ResearchResult):
        """OOS equity should have no duplicate dates."""
        for track in [research_result.steady, research_result.aggressive]:
            dates = [s.snapshot_date for s in track.oos_equity]
            assert len(dates) == len(set(dates)), "Duplicate dates in oos_equity"

    def test_oos_equity_dates_within_test_periods(self, research_result: ResearchResult):
        """OOS equity dates should fall within the test periods."""
        for track in [research_result.steady, research_result.aggressive]:
            test_ranges = [(fr.fold.test_start, fr.fold.test_end) for fr in track.folds]
            for snap in track.oos_equity:
                in_range = any(s <= snap.snapshot_date <= e for s, e in test_ranges)
                assert in_range, f"Date {snap.snapshot_date} not in any test period"


# --------------------------------------------------------------------------- #
# 3. TestRunWalkForward
# --------------------------------------------------------------------------- #


class TestRunWalkForward:
    """Test the run_walk_forward convenience function.

    Uses a fast insufficient-sample config to avoid running the full
    pipeline a second time.  The function itself is a thin wrapper around
    ``ResearchRunner.__init__`` + ``ResearchRunner.run``, so this still
    exercises all its code paths.
    """

    @pytest.fixture(scope="class")
    def wf_result(self, research_env: dict) -> ResearchResult:
        """Run run_walk_forward with insufficient sample (fast, 0 folds)."""
        env = research_env
        wf_config = WalkForwardConfig(
            train_years=1, validation_months=3, test_years=1,
            step_years=1, min_total_years=5,
        )
        return run_walk_forward(
            quotes=env["quotes"],
            trading_dates=env["trading_dates"][:50],
            bt_config=env["bt_config"],
            benchmark=env["benchmark"],
            universe_filter=env["universe_filter"],
            walk_forward_config=wf_config,
            monte_carlo_config=env["mc_config"],
            initial_cash=1000.0,
        )

    def test_returns_research_result(self, wf_result: ResearchResult):
        """run_walk_forward should return a ResearchResult."""
        assert isinstance(wf_result, ResearchResult)

    def test_has_both_tracks(self, wf_result: ResearchResult):
        """Result should have steady and aggressive tracks."""
        assert wf_result.steady.track_type == TrackType.STEADY
        assert wf_result.aggressive.track_type == TrackType.AGGRESSIVE

    def test_aggressive_always_simulation_only(self, wf_result: ResearchResult):
        """Aggressive track should be SIMULATION_ONLY."""
        assert (
            wf_result.aggressive.eligibility.status
            == EligibilityStatus.SIMULATION_ONLY
        )

    def test_limitations_present(self, wf_result: ResearchResult):
        """Limitations should be populated."""
        assert len(wf_result.limitations) >= 5

    def test_same_result_as_runner(self, research_result: ResearchResult):
        """run_walk_forward produces same type as ResearchRunner.run."""
        # Both should produce ResearchResult with the same track types
        assert isinstance(research_result, ResearchResult)
        assert research_result.steady.track_type == TrackType.STEADY
        assert research_result.aggressive.track_type == TrackType.AGGRESSIVE


# --------------------------------------------------------------------------- #
# 4. TestInsufficientSample
# --------------------------------------------------------------------------- #


class TestInsufficientSample:
    """Test behaviour when the sample is insufficient (min_total_years not met)."""

    def test_insufficient_flag_true(self, insufficient_short_result: ResearchResult):
        """insufficient_sample should be True."""
        assert insufficient_short_result.insufficient_sample is True

    def test_steady_eligibility_insufficient(self, insufficient_short_result: ResearchResult):
        """Steady eligibility should be INSUFFICIENT_RESEARCH_SAMPLE."""
        assert (
            insufficient_short_result.steady.eligibility.status
            == EligibilityStatus.INSUFFICIENT_RESEARCH_SAMPLE
        )

    def test_aggressive_still_simulation_only(self, insufficient_short_result: ResearchResult):
        """Aggressive track should still be SIMULATION_ONLY even when insufficient."""
        assert (
            insufficient_short_result.aggressive.eligibility.status
            == EligibilityStatus.SIMULATION_ONLY
        )

    def test_limitations_include_insufficient_message(
        self, insufficient_short_result: ResearchResult
    ):
        """First limitation should mention insufficient sample."""
        assert len(insufficient_short_result.limitations) > 0
        assert "样本不足" in insufficient_short_result.limitations[0]

    def test_no_folds_when_data_too_short(self, insufficient_short_result: ResearchResult):
        """With 1 year of data, no folds should be generated."""
        assert len(insufficient_short_result.folds) == 0

    def test_empty_tracks_when_no_folds(self, insufficient_short_result: ResearchResult):
        """Both tracks should have empty folds list."""
        assert len(insufficient_short_result.steady.folds) == 0
        assert len(insufficient_short_result.aggressive.folds) == 0

    def test_no_stress_results_when_no_folds(
        self, insufficient_short_result: ResearchResult
    ):
        """No stress results should be produced without folds."""
        assert len(insufficient_short_result.steady.stress_results) == 0
        assert len(insufficient_short_result.aggressive.stress_results) == 0

    def test_no_parameter_perturbation_when_no_folds(
        self, insufficient_short_result: ResearchResult
    ):
        """No parameter perturbation should be produced without folds."""
        assert insufficient_short_result.steady.parameter_perturbation is None
        assert insufficient_short_result.aggressive.parameter_perturbation is None

    def test_no_monte_carlo_when_no_oos_equity(
        self, insufficient_short_result: ResearchResult
    ):
        """No Monte Carlo should be produced without OOS equity."""
        assert insufficient_short_result.aggressive.monte_carlo is None

    def test_steady_eligibility_has_failure_reason(
        self, insufficient_short_result: ResearchResult
    ):
        """Steady eligibility should have a failure reason."""
        assert len(insufficient_short_result.steady.eligibility.failure_reasons) > 0
        assert any("样本不足" in r for r in
                    insufficient_short_result.steady.eligibility.failure_reasons)

    def test_insufficient_detection_with_folds(self, research_env: dict):
        """With 3 years and min_total_years=5, sample is insufficient but folds exist."""
        env = research_env
        wf_config = WalkForwardConfig(
            train_years=1, validation_months=3, test_years=1,
            step_years=1, min_total_years=5,
        )
        splitter = WalkForwardSplitter(wf_config)
        assert splitter.is_insufficient_sample(env["trading_dates"]) is True
        folds = splitter.split(env["trading_dates"])
        # 3 complete calendar years -> 2 folds even though insufficient
        assert len(folds) == 2


# --------------------------------------------------------------------------- #
# 5. TestRunnerStressTests
# --------------------------------------------------------------------------- #


class TestRunnerStressTests:
    """Verify stress test results have 4 scenarios."""

    def test_steady_has_four_stress_scenarios(self, research_result: ResearchResult):
        """Steady track should have 4 stress test results."""
        assert len(research_result.steady.stress_results) == 4

    def test_aggressive_has_four_stress_scenarios(self, research_result: ResearchResult):
        """Aggressive track should have 4 stress test results."""
        assert len(research_result.aggressive.stress_results) == 4

    def test_steady_scenario_names(self, research_result: ResearchResult):
        """Steady stress scenarios should be baseline, fee_2x, slippage_2x, combined_2x."""
        names = {sr.scenario.name for sr in research_result.steady.stress_results}
        assert names == {"baseline", "fee_2x", "slippage_2x", "combined_2x"}

    def test_aggressive_scenario_names(self, research_result: ResearchResult):
        """Aggressive stress scenarios should be baseline, fee_2x, slippage_2x, combined_2x."""
        names = {sr.scenario.name for sr in research_result.aggressive.stress_results}
        assert names == {"baseline", "fee_2x", "slippage_2x", "combined_2x"}

    def test_stress_results_are_stress_result_type(self, research_result: ResearchResult):
        """Each stress result should be a StressResult instance."""
        for sr in research_result.steady.stress_results:
            assert isinstance(sr, StressResult)
        for sr in research_result.aggressive.stress_results:
            assert isinstance(sr, StressResult)

    def test_stress_results_have_metrics(self, research_result: ResearchResult):
        """Each stress result should have numeric metrics."""
        for sr in research_result.steady.stress_results + \
                    research_result.aggressive.stress_results:
            assert isinstance(sr.total_return, float)
            assert isinstance(sr.annualized_return, float)
            assert isinstance(sr.max_drawdown, float)
            assert isinstance(sr.total_trades, int)

    def test_combined_2x_present_for_eligibility(self, research_result: ResearchResult):
        """combined_2x scenario must exist for eligibility checking."""
        steady_combined = [
            sr for sr in research_result.steady.stress_results
            if sr.scenario.name == "combined_2x"
        ]
        assert len(steady_combined) == 1


# --------------------------------------------------------------------------- #
# 6. TestRunnerParameterPerturbation
# --------------------------------------------------------------------------- #


class TestRunnerParameterPerturbation:
    """Verify parameter perturbation results."""

    def test_steady_perturbation_not_none(self, research_result: ResearchResult):
        """Steady track should have parameter perturbation results."""
        assert research_result.steady.parameter_perturbation is not None
        assert isinstance(
            research_result.steady.parameter_perturbation,
            ParameterPerturbationResult,
        )

    def test_aggressive_perturbation_not_none(self, research_result: ResearchResult):
        """Aggressive track should have parameter perturbation results."""
        assert research_result.aggressive.parameter_perturbation is not None
        assert isinstance(
            research_result.aggressive.parameter_perturbation,
            ParameterPerturbationResult,
        )

    def test_steady_total_combinations_is_81(self, research_result: ResearchResult):
        """Steady has 3^4 = 81 parameter combinations."""
        pp = research_result.steady.parameter_perturbation
        assert pp.total_combinations == 81

    def test_aggressive_total_combinations_is_729(self, research_result: ResearchResult):
        """Aggressive has 3^6 = 729 parameter combinations."""
        pp = research_result.aggressive.parameter_perturbation
        assert pp.total_combinations == 729

    def test_steady_per_combination_populated(self, research_result: ResearchResult):
        """Steady perturbation should have per_combination list with 81 entries."""
        pp = research_result.steady.parameter_perturbation
        assert len(pp.per_combination) == 81
        for entry in pp.per_combination:
            assert "param_key" in entry
            assert "total_return" in entry
            assert "max_drawdown" in entry
            assert "turnover_rate" in entry

    def test_aggressive_per_combination_populated(self, research_result: ResearchResult):
        """Aggressive perturbation should have per_combination list with 729 entries."""
        pp = research_result.aggressive.parameter_perturbation
        assert len(pp.per_combination) == 729

    def test_steady_positive_return_count_valid(self, research_result: ResearchResult):
        """positive_return_count should be between 0 and total_combinations."""
        pp = research_result.steady.parameter_perturbation
        assert 0 <= pp.positive_return_count <= pp.total_combinations
        assert 0.0 <= pp.positive_return_ratio <= 1.0

    def test_aggressive_positive_return_count_valid(self, research_result: ResearchResult):
        """positive_return_count should be between 0 and total_combinations."""
        pp = research_result.aggressive.parameter_perturbation
        assert 0 <= pp.positive_return_count <= pp.total_combinations
        assert 0.0 <= pp.positive_return_ratio <= 1.0

    def test_steady_return_median_is_float(self, research_result: ResearchResult):
        """return_median should be a float."""
        pp = research_result.steady.parameter_perturbation
        assert isinstance(pp.return_median, float)

    def test_aggressive_return_median_is_float(self, research_result: ResearchResult):
        """return_median should be a float."""
        pp = research_result.aggressive.parameter_perturbation
        assert isinstance(pp.return_median, float)

    def test_steady_baseline_return_populated(self, research_result: ResearchResult):
        """Baseline return should be a float (may be 0.0 if baseline not found)."""
        pp = research_result.steady.parameter_perturbation
        assert isinstance(pp.baseline_return, float)

    def test_aggressive_baseline_return_populated(self, research_result: ResearchResult):
        """Baseline return should be a float."""
        pp = research_result.aggressive.parameter_perturbation
        assert isinstance(pp.baseline_return, float)


# --------------------------------------------------------------------------- #
# 7. TestRunnerEdgeCases
# --------------------------------------------------------------------------- #


class TestRunnerEdgeCases:
    """Edge cases: empty folds, no test dates, 244-day block fallback."""

    def test_empty_trading_dates(self, research_env: dict):
        """Empty trading_dates should produce 0 folds and insufficient sample."""
        env = research_env
        runner = ResearchRunner(
            bt_config=env["bt_config"],
            benchmark=env["benchmark"],
            universe_filter=env["universe_filter"],
            walk_forward_config=env["wf_config"],
            monte_carlo_config=env["mc_config"],
        )
        result = runner.run(env["quotes"], [], initial_cash=1000.0)

        assert isinstance(result, ResearchResult)
        assert result.insufficient_sample is True
        assert len(result.folds) == 0
        assert len(result.steady.folds) == 0
        assert len(result.aggressive.folds) == 0
        assert len(result.steady.oos_equity) == 0
        assert len(result.aggressive.oos_equity) == 0
        assert result.steady.stress_results == []
        assert result.aggressive.stress_results == []
        assert result.steady.parameter_perturbation is None
        assert result.aggressive.parameter_perturbation is None
        assert result.aggressive.monte_carlo is None

    def test_empty_trading_dates_eligibility(self, research_env: dict):
        """With empty dates, steady should be INSUFFICIENT_RESEARCH_SAMPLE."""
        env = research_env
        runner = ResearchRunner(
            bt_config=env["bt_config"],
            benchmark=env["benchmark"],
            universe_filter=env["universe_filter"],
            walk_forward_config=env["wf_config"],
            monte_carlo_config=env["mc_config"],
        )
        result = runner.run(env["quotes"], [], initial_cash=1000.0)
        assert (
            result.steady.eligibility.status
            == EligibilityStatus.INSUFFICIENT_RESEARCH_SAMPLE
        )
        assert (
            result.aggressive.eligibility.status
            == EligibilityStatus.SIMULATION_ONLY
        )

    def test_empty_trading_dates_limitations(self, research_env: dict):
        """With empty dates, limitations should include insufficient message."""
        env = research_env
        runner = ResearchRunner(
            bt_config=env["bt_config"],
            benchmark=env["benchmark"],
            universe_filter=env["universe_filter"],
            walk_forward_config=env["wf_config"],
            monte_carlo_config=env["mc_config"],
        )
        result = runner.run(env["quotes"], [], initial_cash=1000.0)
        assert any("样本不足" in lim for lim in result.limitations)

    def test_single_date_input(self, research_env: dict):
        """A single trading date should produce 0 folds."""
        env = research_env
        runner = ResearchRunner(
            bt_config=env["bt_config"],
            benchmark=env["benchmark"],
            universe_filter=env["universe_filter"],
            walk_forward_config=env["wf_config"],
            monte_carlo_config=env["mc_config"],
        )
        result = runner.run(env["quotes"], [date(2020, 1, 2)], initial_cash=1000.0)
        assert len(result.folds) == 0
        assert result.insufficient_sample is True

    def test_244_day_block_fallback_splitter(self):
        """When data lacks complete calendar years, splitter uses 244-day blocks."""
        wf_config = WalkForwardConfig(
            train_years=1, validation_months=3, test_years=1,
            step_years=1, min_total_years=2,
        )
        # Start mid-year so no complete calendar year exists
        start = date(2019, 6, 3)
        end = date(2021, 6, 30)
        dates = make_trade_dates_range(start, end)

        splitter = WalkForwardSplitter(wf_config)
        folds = splitter.split(dates)
        # ~522 trading days; 244 + 244 = 488 <= 522, so 1 fold
        assert len(folds) == 1
        fold = folds[0]
        # Verify train and test dates are non-empty
        train_dates = splitter.get_train_dates(dates, fold)
        test_dates = splitter.get_test_dates(dates, fold)
        val_dates = splitter.get_validation_dates(dates, fold)
        assert len(train_dates) > 0
        assert len(test_dates) > 0
        assert len(val_dates) > 0
        # Test period should not overlap with train
        assert fold.test_start > fold.train_end


# --------------------------------------------------------------------------- #
# 8. TestExtractDailyReturns
# --------------------------------------------------------------------------- #


class TestExtractDailyReturns:
    """Test the _extract_daily_returns static method."""

    def test_empty_list(self):
        """Empty equity list should return empty returns."""
        assert ResearchRunner._extract_daily_returns([]) == []

    def test_single_snapshot(self):
        """A single snapshot should return empty (need >= 2 for a return)."""
        snap = _make_snapshot(date(2024, 1, 1), 1000.0)
        assert ResearchRunner._extract_daily_returns([snap]) == []

    def test_two_snapshots_positive_return(self):
        """Two snapshots with increasing equity should give a positive return."""
        snaps = [
            _make_snapshot(date(2024, 1, 1), 1000.0),
            _make_snapshot(date(2024, 1, 2), 1100.0),
        ]
        returns = ResearchRunner._extract_daily_returns(snaps)
        assert len(returns) == 1
        assert returns[0] == pytest.approx(0.1)

    def test_two_snapshots_negative_return(self):
        """Two snapshots with decreasing equity should give a negative return."""
        snaps = [
            _make_snapshot(date(2024, 1, 1), 1000.0),
            _make_snapshot(date(2024, 1, 2), 900.0),
        ]
        returns = ResearchRunner._extract_daily_returns(snaps)
        assert len(returns) == 1
        assert returns[0] == pytest.approx(-0.1)

    def test_multiple_snapshots(self):
        """Multiple snapshots should produce n-1 returns."""
        snaps = [
            _make_snapshot(date(2024, 1, 1), 1000.0),
            _make_snapshot(date(2024, 1, 2), 1100.0),
            _make_snapshot(date(2024, 1, 3), 990.0),
            _make_snapshot(date(2024, 1, 4), 1089.0),
        ]
        returns = ResearchRunner._extract_daily_returns(snaps)
        assert len(returns) == 3
        assert returns[0] == pytest.approx(0.1)
        assert returns[1] == pytest.approx(-0.1, abs=1e-6)
        assert returns[2] == pytest.approx(0.1, abs=1e-6)

    def test_zero_previous_equity(self):
        """When previous equity is 0, return should be skipped (division by zero guard)."""
        snaps = [
            _make_snapshot(date(2024, 1, 1), 0.0),
            _make_snapshot(date(2024, 1, 2), 100.0),
        ]
        returns = ResearchRunner._extract_daily_returns(snaps)
        assert len(returns) == 0

    def test_flat_equity(self):
        """Flat equity should produce all-zero returns."""
        snaps = [
            _make_snapshot(date(2024, 1, 1), 1000.0),
            _make_snapshot(date(2024, 1, 2), 1000.0),
            _make_snapshot(date(2024, 1, 3), 1000.0),
        ]
        returns = ResearchRunner._extract_daily_returns(snaps)
        assert len(returns) == 2
        assert all(r == 0.0 for r in returns)

    def test_returns_are_floats(self):
        """All returned values should be floats."""
        snaps = [
            _make_snapshot(date(2024, 1, 1), 1000.0),
            _make_snapshot(date(2024, 1, 2), 1050.0),
        ]
        returns = ResearchRunner._extract_daily_returns(snaps)
        assert all(isinstance(r, float) for r in returns)

    def test_used_in_monte_carlo_pipeline(self, research_result: ResearchResult):
        """_extract_daily_returns is used internally for Monte Carlo; verify integration."""
        aggressive = research_result.aggressive
        if aggressive.oos_equity and len(aggressive.oos_equity) >= 2:
            returns = ResearchRunner._extract_daily_returns(aggressive.oos_equity)
            assert len(returns) == len(aggressive.oos_equity) - 1
            # Monte Carlo should have been populated
            assert aggressive.monte_carlo is not None
            assert aggressive.monte_carlo.n_oos_days == len(returns)
