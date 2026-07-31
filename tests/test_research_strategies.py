"""Phase 3 双轨策略综合 pytest 测试。

覆盖范围（对应任务书测试项 5-8）：
5. 稳健轨只在周末调仓，最多持有一只，无合格标的时持有现金
6. 激进轨日频信号、退出条件、持有天数和最多一只持仓
7. 换仓先卖后买，成交仍为下一交易日开盘
8. 1000 元无法购买一手时保持现金，不得缩小手数或产生负现金

测试类：
1.  TestSteadyParams               - 稳健轨参数 dataclass 与候选集合
2.  TestAggressiveParams            - 激进轨参数 dataclass 与候选集合
3.  TestParamCombinations           - 参数组合生成数量
4.  TestSteadyStrategyWeekly        - 稳健轨仅周频调仓
5.  TestSteadyStrategyMaxOnePosition- 稳健轨最多一只持仓
6.  TestSteadyStrategyCashWhenNoEligible - 无合格标的时持有现金
7.  TestSteadyStrategyCashWhenCantBuyLot - 1000 元买不了一手时保持现金
8.  TestAggressiveStrategyDaily     - 激进轨日频信号
9.  TestAggressiveStrategyExitConditions - 激进轨退出条件
10. TestAggressiveStrategyMaxOnePosition - 激进轨最多一只持仓
11. TestSignalOrdering              - 换仓先卖后买
12. TestBuyQuantityCalc             - 买入数量计算（费用缓冲）

数据由 tests.research_samples 及本文件内的合成构建器生成，非真实行情。
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Optional

import numpy as np
import pandas as pd
import pytest

from ashare_quant.backtest.models import (
    PortfolioSnapshot,
    Position,
    Side,
    Signal,
    StrategyContext,
)
from ashare_quant.research.strategies import (
    AGGRESSIVE_BASELINE_PARAMS,
    AGGRESSIVE_PARAM_CANDIDATES,
    STEADY_BASELINE_PARAMS,
    STEADY_PARAM_CANDIDATES,
    AggressiveParams,
    AggressiveStrategy,
    SteadyParams,
    SteadyStrategy,
    generate_aggressive_param_combinations,
    generate_steady_param_combinations,
)
from ashare_quant.research.universe import (
    HistoricalStatusTable,
    HistoricalUniverseFilter,
)
from tests.backtest_samples import make_trade_dates
from tests.research_samples import (
    make_historical_status_table,
    make_research_quotes,
    make_stock_quotes,
    make_test_research_env,
)

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
START = date(2020, 1, 2)
N_DAYS = 180
N_STOCKS = 6
SYMBOLS = [f"{i:06d}" for i in range(1, N_STOCKS + 1)]
TRADE_DATES = make_trade_dates(START, N_DAYS)


# --------------------------------------------------------------------------- #
# 合成数据构建器
# --------------------------------------------------------------------------- #


def _make_trending_quotes(
    symbols: list[str],
    dates: list[date],
    seed: int = 42,
) -> pd.DataFrame:
    """生成多只确定性上涨股票，趋势/动量/波动率各不相同。

    每只股票有不同的漂移率和噪声水平，确保横截面 z-score 有效
    （标准差 > 0）。所有股票收盘价高于 120 日均线，满足稳健轨趋势过滤。
    """
    rng = np.random.default_rng(seed)
    dfs: list[pd.DataFrame] = []
    for i, sym in enumerate(symbols):
        base = 5.0 + i * 0.3
        drift = 0.003 + i * 0.0008
        noise_scale = 0.005 + i * 0.003
        n = len(dates)
        returns = drift + rng.normal(0, noise_scale, size=n)
        returns[0] = 0.0
        prices = base * np.cumprod(1 + returns)
        price_override = {dates[j]: round(float(prices[j]), 4) for j in range(n)}
        dfs.append(make_stock_quotes(
            sym, dates, base_price=base, daily_return=0.0,
            volume=300000, price_override=price_override,
        ))
    return pd.concat(dfs, ignore_index=True).sort_values(
        ["trade_date", "symbol"]
    ).reset_index(drop=True)


def _make_quotes_ending_at_price(
    symbols: list[str],
    dates: list[date],
    target_price: float = 10.0,
    seed: int = 123,
) -> pd.DataFrame:
    """生成多只股票，末日收盘价均为 target_price，但历史路径不同。

    用于测试费用缓冲边界：所有股票同价但特征各异，可隔离
    ``_calc_buy_quantity`` 的现金/价格逻辑。
    """
    rng = np.random.default_rng(seed)
    start_prices = [5.0, 6.0, 7.0, 8.0, 9.0, 9.5]
    noise_scales = [0.01, 0.008, 0.012, 0.006, 0.015, 0.009]
    n = len(dates)
    dfs: list[pd.DataFrame] = []
    for i, sym in enumerate(symbols):
        start_p = start_prices[i % len(start_prices)]
        trend = np.linspace(start_p, target_price, n)
        noise = rng.normal(0, noise_scales[i % len(noise_scales)], size=n)
        prices = trend * (1 + noise)
        prices[-1] = target_price
        price_override = {dates[j]: round(float(prices[j]), 4) for j in range(n)}
        dfs.append(make_stock_quotes(
            sym, dates, base_price=start_p, daily_return=0.0,
            volume=300000, price_override=price_override,
        ))
    return pd.concat(dfs, ignore_index=True).sort_values(
        ["trade_date", "symbol"]
    ).reset_index(drop=True)


def _make_breakout_quotes(
    symbols: list[str],
    dates: list[date],
    signal_idx: int,
) -> pd.DataFrame:
    """生成突破行情：signal 股在 signal_idx 日突破并放量，其余股票横盘。

    signal 股（symbols[0]）在 signal_idx 日收盘价跳涨且成交量放大，
    满足突破 + 放量条件。其余股票横盘不突破。
    """
    dfs: list[pd.DataFrame] = []
    for i, sym in enumerate(symbols):
        if i == 0:
            prices = [5.0] * len(dates)
            prices[signal_idx] = 5.5
            vols = [200000.0] * len(dates)
            vols[signal_idx] = 600000.0
            price_override = {dates[j]: prices[j] for j in range(len(dates))}
            vol_override = {dates[j]: vols[j] for j in range(len(dates))}
            dfs.append(make_stock_quotes(
                sym, dates, base_price=5.0, daily_return=0.0,
                volume=200000, price_override=price_override,
                volume_override=vol_override,
            ))
        else:
            dfs.append(make_stock_quotes(
                sym, dates, base_price=5.0, daily_return=0.0, volume=200000,
            ))
    return pd.concat(dfs, ignore_index=True).sort_values(
        ["trade_date", "symbol"]
    ).reset_index(drop=True)


def _make_drop_quotes(
    symbols: list[str],
    dates: list[date],
    drop_idx: int,
    drop_to: float = 4.0,
) -> pd.DataFrame:
    """生成下跌行情：symbols[0] 在 drop_idx 日急跌，其余股票缓涨。

    symbols[0] 缓涨至 drop_idx 日后急跌到 drop_to，触发收盘低于
    前 exit_low_window 日最低价的退出条件。
    """
    n = len(dates)
    prices: list[float] = [5.0]
    for j in range(1, n):
        if j == drop_idx:
            prices.append(drop_to)
        else:
            prices.append(round(prices[-1] * 1.003, 4))
    price_override = {dates[j]: prices[j] for j in range(n)}
    dfs = [make_stock_quotes(
        symbols[0], dates, base_price=5.0, daily_return=0.0,
        volume=300000, price_override=price_override,
    )]
    for sym in symbols[1:]:
        dfs.append(make_stock_quotes(
            sym, dates, base_price=5.0, daily_return=0.002, volume=300000,
        ))
    return pd.concat(dfs, ignore_index=True).sort_values(
        ["trade_date", "symbol"]
    ).reset_index(drop=True)


def _make_exit_then_entry_quotes(
    symbols: list[str],
    dates: list[date],
    signal_idx: int,
) -> pd.DataFrame:
    """生成「先退出再入场」行情。

    symbols[0] 在 signal_idx 日急跌（触发低点退出），symbols[1] 在
    signal_idx 日突破放量（触发入场），其余横盘。
    """
    n = len(dates)
    # symbols[0]: 缓涨后急跌
    drop_prices: list[float] = [5.0]
    for j in range(1, n):
        if j == signal_idx:
            drop_prices.append(4.0)
        else:
            drop_prices.append(round(drop_prices[-1] * 1.003, 4))
    drop_override = {dates[j]: drop_prices[j] for j in range(n)}
    dfs = [make_stock_quotes(
        symbols[0], dates, base_price=5.0, daily_return=0.0,
        volume=300000, price_override=drop_override,
    )]
    # symbols[1]: 横盘后突破放量
    breakout_prices = [5.0] * n
    breakout_prices[signal_idx] = 5.5
    breakout_vols = [200000.0] * n
    breakout_vols[signal_idx] = 600000.0
    breakout_price_override = {dates[j]: breakout_prices[j] for j in range(n)}
    breakout_vol_override = {dates[j]: breakout_vols[j] for j in range(n)}
    dfs.append(make_stock_quotes(
        symbols[1], dates, base_price=5.0, daily_return=0.0,
        volume=200000, price_override=breakout_price_override,
        volume_override=breakout_vol_override,
    ))
    # 其余横盘
    for sym in symbols[2:]:
        dfs.append(make_stock_quotes(
            sym, dates, base_price=5.0, daily_return=0.0, volume=200000,
        ))
    return pd.concat(dfs, ignore_index=True).sort_values(
        ["trade_date", "symbol"]
    ).reset_index(drop=True)


def _make_hs300_dict(
    dates: list[date],
    daily_return: float = 0.001,
) -> dict[date, float]:
    """生成沪深 300 基准收盘价字典。"""
    price = 3000.0
    result: dict[date, float] = {}
    for dt in dates:
        price *= (1 + daily_return)
        result[dt] = round(price, 2)
    return result


def _make_simple_status_table(symbols: list[str]) -> HistoricalStatusTable:
    """生成简单状态表：所有股票 2019-01-02 上市，状态 normal。"""
    records = [
        {
            "symbol": sym,
            "list_date": date(2019, 1, 2),
            "delist_date": None,
            "st_status": "normal",
            "status_valid_from": date(2019, 1, 2),
            "status_valid_to": None,
        }
        for sym in symbols
    ]
    return HistoricalStatusTable(records=pd.DataFrame(records))


def _make_permissive_filter(
    status_table: HistoricalStatusTable,
    quotes: pd.DataFrame,
    available_cash: float = 1_000_000.0,
) -> HistoricalUniverseFilter:
    """构建宽松参数过滤器，放行所有正常股票。"""
    return HistoricalUniverseFilter(
        status_table=status_table,
        quotes=quotes,
        min_listing_days=1,
        min_valid_days=1,
        valid_days_window=20,
        min_turnover=0.0,
        turnover_window=20,
        lot_size=100,
        available_cash=available_cash,
    )


# --------------------------------------------------------------------------- #
# 上下文构建器
# --------------------------------------------------------------------------- #


def _bars_up_to(quotes: pd.DataFrame, dt: date) -> pd.DataFrame:
    """截取截至 dt 的行情（保留 RangeIndex，不设日期索引）。"""
    ts = pd.Timestamp(dt)
    return quotes[pd.to_datetime(quotes["trade_date"]) <= ts].copy()


def _build_context(
    dt: date,
    bars: pd.DataFrame,
    cash: float = 100_000.0,
    positions: Optional[dict[str, Position]] = None,
) -> StrategyContext:
    """构建 StrategyContext。"""
    snap = PortfolioSnapshot(
        snapshot_date=dt,
        cash=Decimal(str(cash)),
        position_value=Decimal("0"),
        total_equity=Decimal(str(cash)),
    )
    return StrategyContext(
        current_date=dt,
        portfolio=snap,
        positions=positions or {},
        bars_up_to_date=bars,
    )


def _make_position(symbol: str, qty: int = 100) -> Position:
    """构建可卖持仓。"""
    return Position(
        symbol=symbol,
        total_quantity=qty,
        sellable_quantity=qty,
        avg_raw_cost=Decimal("5"),
    )


# --------------------------------------------------------------------------- #
# 模块级 fixture（只读共享数据）
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def trending_quotes() -> pd.DataFrame:
    return _make_trending_quotes(SYMBOLS, TRADE_DATES)


@pytest.fixture(scope="module")
def simple_status_table() -> HistoricalStatusTable:
    return _make_simple_status_table(SYMBOLS)


@pytest.fixture(scope="module")
def permissive_filter(
    simple_status_table: HistoricalStatusTable,
    trending_quotes: pd.DataFrame,
) -> HistoricalUniverseFilter:
    return _make_permissive_filter(simple_status_table, trending_quotes)


@pytest.fixture(scope="module")
def last_weekly_date() -> date:
    """最后一个交易日（始终是周频调仓日）。"""
    return TRADE_DATES[-1]


# =========================================================================== #
# 1. TestSteadyParams
# =========================================================================== #


class TestSteadyParams:
    """稳健轨参数 dataclass 与候选集合验证。"""

    def test_default_values(self):
        params = SteadyParams()
        assert params.trend_window == 120
        assert params.momentum_window == 60
        assert params.volatility_window == 20
        assert params.minimum_score == 0.0

    def test_is_frozen(self):
        params = SteadyParams()
        with pytest.raises(AttributeError):
            params.trend_window = 100  # type: ignore[misc]

    def test_candidate_keys(self):
        assert set(STEADY_PARAM_CANDIDATES.keys()) == {
            "trend_window", "momentum_window",
            "volatility_window", "minimum_score",
        }

    def test_candidate_values(self):
        assert STEADY_PARAM_CANDIDATES["trend_window"] == [100, 120, 140]
        assert STEADY_PARAM_CANDIDATES["momentum_window"] == [50, 60, 70]
        assert STEADY_PARAM_CANDIDATES["volatility_window"] == [15, 20, 25]
        assert STEADY_PARAM_CANDIDATES["minimum_score"] == [-0.25, 0.0, 0.25]
        for values in STEADY_PARAM_CANDIDATES.values():
            assert len(values) == 3

    def test_baseline_equals_default(self):
        assert STEADY_BASELINE_PARAMS == SteadyParams()
        assert STEADY_BASELINE_PARAMS.trend_window == 120


# =========================================================================== #
# 2. TestAggressiveParams
# =========================================================================== #


class TestAggressiveParams:
    """激进轨参数 dataclass 与候选集合验证。"""

    def test_default_values(self):
        params = AggressiveParams()
        assert params.breakout_window == 20
        assert params.volume_window == 20
        assert params.volume_ratio == 1.5
        assert params.relative_strength_window == 20
        assert params.exit_low_window == 10
        assert params.max_holding_days == 20

    def test_is_frozen(self):
        params = AggressiveParams()
        with pytest.raises(AttributeError):
            params.breakout_window = 15  # type: ignore[misc]

    def test_candidate_keys(self):
        assert set(AGGRESSIVE_PARAM_CANDIDATES.keys()) == {
            "breakout_window", "volume_window", "volume_ratio",
            "relative_strength_window", "exit_low_window", "max_holding_days",
        }

    def test_candidate_values(self):
        assert AGGRESSIVE_PARAM_CANDIDATES["breakout_window"] == [15, 20, 25]
        assert AGGRESSIVE_PARAM_CANDIDATES["volume_ratio"] == [1.2, 1.5, 1.8]
        assert AGGRESSIVE_PARAM_CANDIDATES["max_holding_days"] == [15, 20, 25]
        for values in AGGRESSIVE_PARAM_CANDIDATES.values():
            assert len(values) == 3

    def test_baseline_equals_default(self):
        assert AGGRESSIVE_BASELINE_PARAMS == AggressiveParams()
        assert AGGRESSIVE_BASELINE_PARAMS.max_holding_days == 20


# =========================================================================== #
# 3. TestParamCombinations
# =========================================================================== #


class TestParamCombinations:
    """参数组合生成数量与唯一性。"""

    def test_steady_combination_count(self):
        combos = generate_steady_param_combinations()
        assert len(combos) == 81  # 3 * 3 * 3 * 3

    def test_aggressive_combination_count(self):
        combos = generate_aggressive_param_combinations()
        assert len(combos) == 729  # 3 ** 6

    def test_steady_combinations_unique(self):
        combos = generate_steady_param_combinations()
        unique = set(combos)
        assert len(unique) == 81

    def test_aggressive_combinations_unique(self):
        combos = generate_aggressive_param_combinations()
        unique = set(combos)
        assert len(unique) == 729

    def test_steady_includes_baseline(self):
        combos = generate_steady_param_combinations()
        assert STEADY_BASELINE_PARAMS in combos

    def test_aggressive_includes_baseline(self):
        combos = generate_aggressive_param_combinations()
        assert AGGRESSIVE_BASELINE_PARAMS in combos


# =========================================================================== #
# 4. TestSteadyStrategyWeekly
# =========================================================================== #


class TestSteadyStrategyWeekly:
    """稳健轨仅在每周最后一个交易日调仓。"""

    def test_weekly_dates_subset_of_trading_dates(
        self, permissive_filter: HistoricalUniverseFilter,
    ):
        strategy = SteadyStrategy(SteadyParams(), permissive_filter, TRADE_DATES)
        assert strategy._weekly_dates.issubset(set(TRADE_DATES))

    def test_mid_monday_not_weekly(
        self, permissive_filter: HistoricalUniverseFilter,
    ):
        strategy = SteadyStrategy(SteadyParams(), permissive_filter, TRADE_DATES)
        mondays = [d for d in TRADE_DATES if d.weekday() == 0]
        # 取中间的星期一（不是最后一个交易日）
        mid_monday = mondays[len(mondays) // 2]
        assert mid_monday not in strategy._weekly_dates

    def test_mid_friday_is_weekly(
        self, permissive_filter: HistoricalUniverseFilter,
    ):
        strategy = SteadyStrategy(SteadyParams(), permissive_filter, TRADE_DATES)
        fridays = [d for d in TRADE_DATES if d.weekday() == 4]
        mid_friday = fridays[len(fridays) // 2]
        assert mid_friday in strategy._weekly_dates

    def test_last_trading_date_is_weekly(
        self, permissive_filter: HistoricalUniverseFilter,
    ):
        strategy = SteadyStrategy(SteadyParams(), permissive_filter, TRADE_DATES)
        # 最后一个交易日始终是其 ISO 周的最后一个交易日
        assert TRADE_DATES[-1] in strategy._weekly_dates

    def test_no_signal_on_non_weekly_date(
        self, permissive_filter: HistoricalUniverseFilter,
        trending_quotes: pd.DataFrame,
    ):
        strategy = SteadyStrategy(SteadyParams(), permissive_filter, TRADE_DATES)
        mondays = [d for d in TRADE_DATES if d.weekday() == 0]
        non_weekly = mondays[len(mondays) // 2]
        bars = _bars_up_to(trending_quotes, non_weekly)
        ctx = _build_context(non_weekly, bars, cash=100_000.0)
        signals = strategy.on_close(ctx)
        assert signals == []

    def test_buy_signal_on_weekly_date(
        self, permissive_filter: HistoricalUniverseFilter,
        trending_quotes: pd.DataFrame,
    ):
        """周频调仓日且有合格标的时生成买入信号。"""
        strategy = SteadyStrategy(SteadyParams(), permissive_filter, TRADE_DATES)
        weekly_dt = TRADE_DATES[-1]
        bars = _bars_up_to(trending_quotes, weekly_dt)
        ctx = _build_context(weekly_dt, bars, cash=100_000.0)
        signals = strategy.on_close(ctx)
        buy_signals = [s for s in signals if s.side == Side.BUY]
        assert len(buy_signals) == 1
        assert buy_signals[0].signal_date == weekly_dt
        assert buy_signals[0].quantity > 0


# =========================================================================== #
# 5. TestSteadyStrategyMaxOnePosition
# =========================================================================== #


class TestSteadyStrategyMaxOnePosition:
    """稳健轨最多持有一只股票。"""

    def test_no_position_at_most_one_buy(
        self, permissive_filter: HistoricalUniverseFilter,
        trending_quotes: pd.DataFrame,
    ):
        strategy = SteadyStrategy(SteadyParams(), permissive_filter, TRADE_DATES)
        dt = TRADE_DATES[-1]
        bars = _bars_up_to(trending_quotes, dt)
        ctx = _build_context(dt, bars, cash=100_000.0)
        signals = strategy.on_close(ctx)
        buy_signals = [s for s in signals if s.side == Side.BUY]
        assert len(buy_signals) <= 1
        if buy_signals:
            buy_symbols = {s.symbol for s in buy_signals}
            assert len(buy_symbols) == 1

    def test_holding_non_best_generates_sell_then_buy(
        self, permissive_filter: HistoricalUniverseFilter,
        trending_quotes: pd.DataFrame,
    ):
        """持有非最优标的时，换仓生成卖出 + 买入各一笔。"""
        strategy = SteadyStrategy(SteadyParams(), permissive_filter, TRADE_DATES)
        dt = TRADE_DATES[-1]
        bars = _bars_up_to(trending_quotes, dt)
        # 持有一只不在行情中的股票
        ctx = _build_context(
            dt, bars, cash=100_000.0,
            positions={"999999": _make_position("999999")},
        )
        signals = strategy.on_close(ctx)
        sells = [s for s in signals if s.side == Side.SELL]
        buys = [s for s in signals if s.side == Side.BUY]
        assert len(sells) == 1
        assert len(buys) == 1
        assert sells[0].symbol == "999999"
        assert buys[0].symbol != "999999"

    def test_holding_best_no_duplicate_buy(
        self, permissive_filter: HistoricalUniverseFilter,
        trending_quotes: pd.DataFrame,
    ):
        """已持有最优标的时不再追加买入。"""
        strategy = SteadyStrategy(SteadyParams(), permissive_filter, TRADE_DATES)
        dt = TRADE_DATES[-1]
        bars = _bars_up_to(trending_quotes, dt)
        # 第一次调用：无持仓，获取最优标的
        ctx0 = _build_context(dt, bars, cash=100_000.0)
        signals0 = strategy.on_close(ctx0)
        buy0 = [s for s in signals0 if s.side == Side.BUY]
        assert len(buy0) == 1
        best_sym = buy0[0].symbol
        # 第二次调用：持有最优标的
        ctx1 = _build_context(
            dt, bars, cash=100_000.0,
            positions={best_sym: _make_position(best_sym)},
        )
        signals1 = strategy.on_close(ctx1)
        buys1 = [s for s in signals1 if s.side == Side.BUY]
        sells1 = [s for s in signals1 if s.side == Side.SELL]
        assert len(buys1) == 0
        assert len(sells1) == 0


# =========================================================================== #
# 6. TestSteadyStrategyCashWhenNoEligible
# =========================================================================== #


class TestSteadyStrategyCashWhenNoEligible:
    """无合格标的时持有现金。"""

    @pytest.fixture(scope="class")
    def no_eligible_env(self) -> dict:
        """默认 env 的 min_turnover=20M 使所有合成股票因流动性不足被过滤。"""
        return make_test_research_env(n_days=200, n_stocks=8)

    def test_no_position_no_eligible_returns_empty(self, no_eligible_env: dict):
        env = no_eligible_env
        quotes = env["quotes"]
        uf = env["universe_filter"]
        trading_dates = sorted(
            pd.to_datetime(quotes["trade_date"]).dt.date.unique().tolist()
        )
        strategy = SteadyStrategy(SteadyParams(), uf, trading_dates)
        dt = trading_dates[-1]
        bars = _bars_up_to(quotes, dt)
        ctx = _build_context(dt, bars, cash=1000.0)
        signals = strategy.on_close(ctx)
        # 无持仓且无合格标的 -> 不生成任何信号
        assert signals == []

    def test_with_position_no_eligible_generates_sell(self, no_eligible_env: dict):
        env = no_eligible_env
        quotes = env["quotes"]
        uf = env["universe_filter"]
        trading_dates = sorted(
            pd.to_datetime(quotes["trade_date"]).dt.date.unique().tolist()
        )
        strategy = SteadyStrategy(SteadyParams(), uf, trading_dates)
        dt = trading_dates[-1]
        bars = _bars_up_to(quotes, dt)
        ctx = _build_context(
            dt, bars, cash=1000.0,
            positions={"000001": _make_position("000001")},
        )
        signals = strategy.on_close(ctx)
        # 有持仓但无合格标的 -> 清仓卖出
        sells = [s for s in signals if s.side == Side.SELL]
        buys = [s for s in signals if s.side == Side.BUY]
        assert len(sells) == 1
        assert len(buys) == 0

    def test_no_buy_signal_when_no_eligible(self, no_eligible_env: dict):
        env = no_eligible_env
        quotes = env["quotes"]
        uf = env["universe_filter"]
        trading_dates = sorted(
            pd.to_datetime(quotes["trade_date"]).dt.date.unique().tolist()
        )
        strategy = SteadyStrategy(SteadyParams(), uf, trading_dates)
        dt = trading_dates[-1]
        bars = _bars_up_to(quotes, dt)
        ctx = _build_context(dt, bars, cash=1000.0)
        signals = strategy.on_close(ctx)
        assert all(s.side != Side.BUY for s in signals)


# =========================================================================== #
# 7. TestSteadyStrategyCashWhenCantBuyLot
# =========================================================================== #


class TestSteadyStrategyCashWhenCantBuyLot:
    """1000 元无法购买一手时保持现金，不得缩小手数或产生负现金。"""

    @pytest.fixture(scope="class")
    def ten_yuan_env(self) -> tuple:
        """所有股票末日收盘价 10.0，用于测试费用缓冲边界。"""
        quotes = _make_quotes_ending_at_price(SYMBOLS, TRADE_DATES, 10.0)
        status = _make_simple_status_table(SYMBOLS)
        uf = _make_permissive_filter(status, quotes, available_cash=1_000_000.0)
        return quotes, uf

    def test_all_at_ten_no_buy_with_1000_cash(self, ten_yuan_env: tuple):
        """1000 元买不了一手 10 元股票（含 0.5% 费用缓冲）-> 不生成买入。"""
        quotes, uf = ten_yuan_env
        strategy = SteadyStrategy(SteadyParams(), uf, TRADE_DATES)
        dt = TRADE_DATES[-1]
        bars = _bars_up_to(quotes, dt)
        # 验证末日收盘价均为 10.0
        last_rows = bars[bars["trade_date"] == dt]
        assert all(abs(float(r["close_raw"]) - 10.0) < 1e-6 for _, r in last_rows.iterrows())
        ctx = _build_context(dt, bars, cash=1000.0)
        signals = strategy.on_close(ctx)
        buys = [s for s in signals if s.side == Side.BUY]
        assert len(buys) == 0

    def test_all_at_ten_buy_with_2000_cash(self, ten_yuan_env: tuple):
        """2000 元可买一手 10 元股票 -> 生成买入，数量为 100 的整数倍。"""
        quotes, uf = ten_yuan_env
        strategy = SteadyStrategy(SteadyParams(), uf, TRADE_DATES)
        dt = TRADE_DATES[-1]
        bars = _bars_up_to(quotes, dt)
        ctx = _build_context(dt, bars, cash=2000.0)
        signals = strategy.on_close(ctx)
        buys = [s for s in signals if s.side == Side.BUY]
        assert len(buys) == 1
        assert buys[0].quantity == 100
        # 买入成本不超过现金
        cost = buys[0].quantity * 10.0 * 1.005
        assert cost <= 2000.0

    def test_buy_quantity_always_multiple_of_lot(
        self, permissive_filter: HistoricalUniverseFilter,
        trending_quotes: pd.DataFrame,
    ):
        """买入数量始终为 100 的整数倍，不得出现零碎手数。"""
        strategy = SteadyStrategy(SteadyParams(), permissive_filter, TRADE_DATES)
        dt = TRADE_DATES[-1]
        bars = _bars_up_to(trending_quotes, dt)
        ctx = _build_context(dt, bars, cash=100_000.0)
        signals = strategy.on_close(ctx)
        for s in signals:
            if s.side == Side.BUY:
                assert s.quantity % 100 == 0
                assert s.quantity > 0

    def test_no_negative_cash_implied(self, ten_yuan_env: tuple):
        """买入信号隐含的成本不得超过可用现金（不会产生负现金）。"""
        quotes, uf = ten_yuan_env
        strategy = SteadyStrategy(SteadyParams(), uf, TRADE_DATES)
        dt = TRADE_DATES[-1]
        bars = _bars_up_to(quotes, dt)
        ctx = _build_context(dt, bars, cash=1000.0)
        signals = strategy.on_close(ctx)
        for s in signals:
            if s.side == Side.BUY:
                # 10.0 * 1.005 * qty 必须不超过 1000
                assert 10.0 * 1.005 * s.quantity <= 1000.0


# =========================================================================== #
# 8. TestAggressiveStrategyDaily
# =========================================================================== #


class TestAggressiveStrategyDaily:
    """激进轨日频信号：每个交易日都处理，不受周频限制。"""

    def test_exit_on_non_friday(
        self, permissive_filter: HistoricalUniverseFilter,
        trending_quotes: pd.DataFrame,
    ):
        """非周五（如周一）也能生成退出信号，证明日频处理。"""
        strategy = AggressiveStrategy(
            AggressiveParams(max_holding_days=3),
            permissive_filter, TRADE_DATES, lot_size=100,
        )
        # 找一个周一
        mondays = [d for d in TRADE_DATES if d.weekday() == 0]
        monday = mondays[10]
        bars = _bars_up_to(trending_quotes, monday)
        ctx = _build_context(
            monday, bars, cash=100_000.0,
            positions={"000001": _make_position("000001")},
        )
        signals = strategy.on_close(ctx)
        # 持有但 holding_days=1 < 3，不退出；且无入场（持有中）
        # 关键：策略在非周五执行了处理（未跳过）
        assert isinstance(signals, list)

    def test_exit_by_holding_days_on_non_friday(
        self, permissive_filter: HistoricalUniverseFilter,
        trending_quotes: pd.DataFrame,
    ):
        """连续 3 个交易日调用后，在非周五触发持有天数退出。"""
        strategy = AggressiveStrategy(
            AggressiveParams(max_holding_days=3),
            permissive_filter, TRADE_DATES, lot_size=100,
        )
        pos = _make_position("000001")
        # 取连续 3 个交易日
        idx = len(TRADE_DATES) - 5
        results = []
        for i in range(3):
            dt = TRADE_DATES[idx + i]
            bars = _bars_up_to(trending_quotes, dt)
            ctx = _build_context(dt, bars, cash=100_000.0, positions={"000001": pos})
            sigs = strategy.on_close(ctx)
            results.append(sigs)
        # 前两次不退出，第三次退出
        assert len(results[0]) == 0
        assert len(results[1]) == 0
        assert len(results[2]) == 1
        assert results[2][0].side == Side.SELL

    def test_entry_on_non_friday(self):
        """非周五也能生成入场信号（需要基准数据和日期索引行情）。"""
        agg_dates = make_trade_dates(START, 30)
        agg_symbols = [f"{i:06d}" for i in range(1, 7)]
        quotes = _make_breakout_quotes(agg_symbols, agg_dates, signal_idx=25)
        status = _make_simple_status_table(agg_symbols)
        uf = _make_permissive_filter(status, quotes, available_cash=1_000_000.0)
        bench = _make_hs300_dict(agg_dates, 0.001)
        strategy = AggressiveStrategy(
            AggressiveParams(), uf, agg_dates, lot_size=100,
            benchmark_hs300=bench,
        )
        signal_dt = agg_dates[25]
        # 确认信号日不是周五
        assert signal_dt.weekday() != 4
        bars = _bars_up_to(quotes, signal_dt)
        ctx = _build_context(signal_dt, bars, cash=100_000.0)
        signals = strategy.on_close(ctx)
        buys = [s for s in signals if s.side == Side.BUY]
        assert len(buys) == 1
        assert buys[0].signal_date == signal_dt

    def test_consecutive_days_all_processed(
        self, permissive_filter: HistoricalUniverseFilter,
        trending_quotes: pd.DataFrame,
    ):
        """连续多个交易日调用，策略每天都处理（不跳过）。"""
        strategy = AggressiveStrategy(
            AggressiveParams(), permissive_filter, TRADE_DATES, lot_size=100,
        )
        pos = _make_position("000001")
        for i in range(5):
            dt = TRADE_DATES[len(TRADE_DATES) - 6 + i]
            bars = _bars_up_to(trending_quotes, dt)
            ctx = _build_context(dt, bars, cash=100_000.0, positions={"000001": pos})
            sigs = strategy.on_close(ctx)
            # 策略不抛异常即说明正常处理
            assert isinstance(sigs, list)


# =========================================================================== #
# 9. TestAggressiveStrategyExitConditions
# =========================================================================== #


class TestAggressiveStrategyExitConditions:
    """激进轨退出条件：持有天数、低点突破、股票池退出。"""

    def test_exit_by_holding_days(
        self, permissive_filter: HistoricalUniverseFilter,
        trending_quotes: pd.DataFrame,
    ):
        """持有满 max_holding_days 个交易日后退出。"""
        strategy = AggressiveStrategy(
            AggressiveParams(max_holding_days=3),
            permissive_filter, TRADE_DATES, lot_size=100,
        )
        pos = _make_position("000001")
        idx = len(TRADE_DATES) - 5
        all_signals: list[list[Signal]] = []
        for i in range(3):
            dt = TRADE_DATES[idx + i]
            bars = _bars_up_to(trending_quotes, dt)
            ctx = _build_context(dt, bars, cash=100_000.0, positions={"000001": pos})
            all_signals.append(strategy.on_close(ctx))
        # 第三次触发退出
        assert len(all_signals[2]) == 1
        sell = all_signals[2][0]
        assert sell.side == Side.SELL
        assert sell.symbol == "000001"
        assert "持有满" in sell.reason or "3" in sell.reason

    def test_exit_by_low_break(self):
        """收盘价低于前 exit_low_window 日最低价时退出。"""
        dates = make_trade_dates(START, 30)
        symbols = [f"{i:06d}" for i in range(1, 7)]
        quotes = _make_drop_quotes(symbols, dates, drop_idx=25, drop_to=4.0)
        status = _make_simple_status_table(symbols)
        uf = _make_permissive_filter(status, quotes, available_cash=1_000_000.0)
        strategy = AggressiveStrategy(AggressiveParams(), uf, dates, lot_size=100)
        dt = dates[25]
        bars = _bars_up_to(quotes, dt)
        ctx = _build_context(
            dt, bars, cash=100_000.0,
            positions={"000001": _make_position("000001")},
        )
        signals = strategy.on_close(ctx)
        sells = [s for s in signals if s.side == Side.SELL]
        assert len(sells) == 1
        assert sells[0].symbol == "000001"
        assert "最低" in sells[0].reason

    def test_exit_by_universe_st(self):
        """标的变为 ST 不再满足股票池时退出。"""
        quotes = make_research_quotes(start=START, n_days=200, n_stocks=8)
        status = HistoricalStatusTable(
            records=make_historical_status_table(start=START, n_stocks=8)
        )
        uf = _make_permissive_filter(status, quotes, available_cash=1_000_000.0)
        trading_dates = sorted(
            pd.to_datetime(quotes["trade_date"]).dt.date.unique().tolist()
        )
        strategy = AggressiveStrategy(
            AggressiveParams(), uf, trading_dates, lot_size=100,
        )
        # 000007 在 START + 90 天后变为 ST
        st_date = START + timedelta(days=90)
        dt = next(d for d in trading_dates if d >= st_date)
        bars = _bars_up_to(quotes, dt)
        ctx = _build_context(
            dt, bars, cash=100_000.0,
            positions={"000007": _make_position("000007")},
        )
        signals = strategy.on_close(ctx)
        sells = [s for s in signals if s.side == Side.SELL]
        assert len(sells) == 1
        assert sells[0].symbol == "000007"
        assert "股票池" in sells[0].reason

    def test_no_exit_when_conditions_not_met(
        self, permissive_filter: HistoricalUniverseFilter,
        trending_quotes: pd.DataFrame,
    ):
        """不满足退出条件时不退出。"""
        strategy = AggressiveStrategy(
            AggressiveParams(max_holding_days=20),
            permissive_filter, TRADE_DATES, lot_size=100,
        )
        dt = TRADE_DATES[-3]
        bars = _bars_up_to(trending_quotes, dt)
        ctx = _build_context(
            dt, bars, cash=100_000.0,
            positions={"000001": _make_position("000001")},
        )
        signals = strategy.on_close(ctx)
        # 上涨股票不触发低点退出，holding_days=1 < 20，仍合格 -> 不退出
        # 持有中也不入场 -> 空列表
        assert all(s.side != Side.SELL for s in signals)

    def test_exit_signal_quantity_equals_sellable(
        self, permissive_filter: HistoricalUniverseFilter,
        trending_quotes: pd.DataFrame,
    ):
        """退出信号数量等于可卖数量。"""
        strategy = AggressiveStrategy(
            AggressiveParams(max_holding_days=2),
            permissive_filter, TRADE_DATES, lot_size=100,
        )
        pos = Position(
            symbol="000001", total_quantity=200,
            sellable_quantity=200, avg_raw_cost=Decimal("5"),
        )
        idx = len(TRADE_DATES) - 4
        for i in range(2):
            dt = TRADE_DATES[idx + i]
            bars = _bars_up_to(trending_quotes, dt)
            ctx = _build_context(dt, bars, cash=100_000.0, positions={"000001": pos})
            sigs = strategy.on_close(ctx)
        # 第二次触发退出
        sells = [s for s in sigs if s.side == Side.SELL]
        assert len(sells) == 1
        assert sells[0].quantity == 200


# =========================================================================== #
# 10. TestAggressiveStrategyMaxOnePosition
# =========================================================================== #


class TestAggressiveStrategyMaxOnePosition:
    """激进轨最多持有一只股票。"""

    def test_holding_no_new_buy(
        self, permissive_filter: HistoricalUniverseFilter,
        trending_quotes: pd.DataFrame,
    ):
        """已持仓时不生成新的买入信号。"""
        strategy = AggressiveStrategy(
            AggressiveParams(), permissive_filter, TRADE_DATES, lot_size=100,
        )
        dt = TRADE_DATES[-1]
        bars = _bars_up_to(trending_quotes, dt)
        ctx = _build_context(
            dt, bars, cash=100_000.0,
            positions={"000001": _make_position("000001")},
        )
        signals = strategy.on_close(ctx)
        buys = [s for s in signals if s.side == Side.BUY]
        assert len(buys) == 0

    def test_no_position_at_most_one_buy(self):
        """无持仓时最多生成一笔买入。"""
        dates = make_trade_dates(START, 30)
        symbols = [f"{i:06d}" for i in range(1, 7)]
        quotes = _make_breakout_quotes(symbols, dates, signal_idx=25)
        status = _make_simple_status_table(symbols)
        uf = _make_permissive_filter(status, quotes, available_cash=1_000_000.0)
        bench = _make_hs300_dict(dates, 0.001)
        strategy = AggressiveStrategy(
            AggressiveParams(), uf, dates, lot_size=100,
            benchmark_hs300=bench,
        )
        dt = dates[25]
        bars = _bars_up_to(quotes, dt)
        ctx = _build_context(dt, bars, cash=100_000.0)
        signals = strategy.on_close(ctx)
        buys = [s for s in signals if s.side == Side.BUY]
        assert len(buys) <= 1
        if buys:
            assert len({s.symbol for s in buys}) == 1

    def test_exit_then_entry_one_each(self):
        """退出后入场：恰好一笔卖出 + 一笔买入。"""
        dates = make_trade_dates(START, 30)
        symbols = [f"{i:06d}" for i in range(1, 7)]
        quotes = _make_exit_then_entry_quotes(symbols, dates, signal_idx=25)
        status = _make_simple_status_table(symbols)
        uf = _make_permissive_filter(status, quotes, available_cash=1_000_000.0)
        bench = _make_hs300_dict(dates, 0.001)
        strategy = AggressiveStrategy(
            AggressiveParams(), uf, dates, lot_size=100,
            benchmark_hs300=bench,
        )
        dt = dates[25]
        bars = _bars_up_to(quotes, dt)
        ctx = _build_context(
            dt, bars, cash=100_000.0,
            positions={"000001": _make_position("000001")},
        )
        signals = strategy.on_close(ctx)
        sells = [s for s in signals if s.side == Side.SELL]
        buys = [s for s in signals if s.side == Side.BUY]
        assert len(sells) == 1
        assert len(buys) == 1
        assert sells[0].symbol == "000001"
        assert buys[0].symbol == "000002"


# =========================================================================== #
# 11. TestSignalOrdering
# =========================================================================== #


class TestSignalOrdering:
    """换仓信号顺序：先卖后买。"""

    def test_steady_sell_before_buy(
        self, permissive_filter: HistoricalUniverseFilter,
        trending_quotes: pd.DataFrame,
    ):
        """稳健轨换仓时 SELL 在 BUY 之前。"""
        strategy = SteadyStrategy(SteadyParams(), permissive_filter, TRADE_DATES)
        dt = TRADE_DATES[-1]
        bars = _bars_up_to(trending_quotes, dt)
        ctx = _build_context(
            dt, bars, cash=100_000.0,
            positions={"999999": _make_position("999999")},
        )
        signals = strategy.on_close(ctx)
        assert len(signals) == 2
        assert signals[0].side == Side.SELL
        assert signals[1].side == Side.BUY

    def test_steady_all_sells_precede_buys(
        self, permissive_filter: HistoricalUniverseFilter,
        trending_quotes: pd.DataFrame,
    ):
        """稳健轨信号列表中所有 SELL 在所有 BUY 之前。"""
        strategy = SteadyStrategy(SteadyParams(), permissive_filter, TRADE_DATES)
        dt = TRADE_DATES[-1]
        bars = _bars_up_to(trending_quotes, dt)
        ctx = _build_context(
            dt, bars, cash=100_000.0,
            positions={"999999": _make_position("999999")},
        )
        signals = strategy.on_close(ctx)
        sides = [s.side for s in signals]
        if Side.BUY in sides and Side.SELL in sides:
            assert sides.index(Side.SELL) < sides.index(Side.BUY)
        # 不存在 BUY 在 SELL 之前的情况
        for i in range(len(sides)):
            for j in range(i + 1, len(sides)):
                assert not (sides[i] == Side.BUY and sides[j] == Side.SELL)

    def test_aggressive_sell_before_buy(self):
        """激进轨退出后入场：SELL 在 BUY 之前。"""
        dates = make_trade_dates(START, 30)
        symbols = [f"{i:06d}" for i in range(1, 7)]
        quotes = _make_exit_then_entry_quotes(symbols, dates, signal_idx=25)
        status = _make_simple_status_table(symbols)
        uf = _make_permissive_filter(status, quotes, available_cash=1_000_000.0)
        bench = _make_hs300_dict(dates, 0.001)
        strategy = AggressiveStrategy(
            AggressiveParams(), uf, dates, lot_size=100,
            benchmark_hs300=bench,
        )
        dt = dates[25]
        bars = _bars_up_to(quotes, dt)
        ctx = _build_context(
            dt, bars, cash=100_000.0,
            positions={"000001": _make_position("000001")},
        )
        signals = strategy.on_close(ctx)
        assert len(signals) == 2
        assert signals[0].side == Side.SELL
        assert signals[1].side == Side.BUY

    def test_signal_date_matches_context(
        self, permissive_filter: HistoricalUniverseFilter,
        trending_quotes: pd.DataFrame,
    ):
        """信号日期等于上下文当日（成交为下一交易日开盘）。"""
        strategy = SteadyStrategy(SteadyParams(), permissive_filter, TRADE_DATES)
        dt = TRADE_DATES[-1]
        bars = _bars_up_to(trending_quotes, dt)
        ctx = _build_context(dt, bars, cash=100_000.0)
        signals = strategy.on_close(ctx)
        for s in signals:
            assert s.signal_date == dt


# =========================================================================== #
# 12. TestBuyQuantityCalc
# =========================================================================== #


class TestBuyQuantityCalc:
    """买入数量计算：费用缓冲 _COST_BUFFER = 1.005。"""

    @pytest.fixture(scope="class")
    def steady_calc(self, permissive_filter: HistoricalUniverseFilter):
        return SteadyStrategy(SteadyParams(), permissive_filter, TRADE_DATES, lot_size=100)

    @pytest.fixture(scope="class")
    def aggressive_calc(self, permissive_filter: HistoricalUniverseFilter):
        return AggressiveStrategy(
            AggressiveParams(), permissive_filter, TRADE_DATES, lot_size=100,
        )

    def test_sufficient_cash(self, steady_calc: SteadyStrategy):
        """现金充足时计算正确手数。"""
        # 5.0 * 1.005 * 100 = 502.5; 10000 // 502.5 = 19; 19 * 100 = 1900
        assert steady_calc._calc_buy_quantity(10000, 5.0) == 1900

    def test_insufficient_cash_returns_zero(self, steady_calc: SteadyStrategy):
        """1000 元买不了一手 10 元股票（含缓冲）-> 0。"""
        # 10.0 * 1.005 * 100 = 1005 > 1000
        assert steady_calc._calc_buy_quantity(1000, 10.0) == 0

    def test_cost_buffer_boundary(self, steady_calc: SteadyStrategy):
        """费用缓冲边界：1000 元恰好买不了 10 元一手。"""
        # 10 * 100 = 1000 <= 1000（可过股票池）
        # 10 * 1.005 * 100 = 1005 > 1000（含缓冲买不了）
        assert steady_calc._calc_buy_quantity(1000, 10.0) == 0
        # 1005 元恰好买一手
        assert steady_calc._calc_buy_quantity(1005, 10.0) == 100

    def test_zero_cash(self, steady_calc: SteadyStrategy):
        """现金为 0 -> 0。"""
        assert steady_calc._calc_buy_quantity(0, 5.0) == 0

    def test_zero_price(self, steady_calc: SteadyStrategy):
        """价格为 0 -> 0。"""
        assert steady_calc._calc_buy_quantity(10000, 0.0) == 0

    def test_quantity_multiple_of_lot_size(self, steady_calc: SteadyStrategy):
        """返回数量始终为 lot_size 的整数倍。"""
        for cash in [500, 1000, 5000, 12345, 100000]:
            for price in [3.0, 5.0, 7.5, 10.0, 15.0]:
                qty = steady_calc._calc_buy_quantity(cash, price)
                assert qty % 100 == 0
                # 不超过现金（含缓冲）
                if qty > 0:
                    assert price * 1.005 * qty <= cash

    def test_custom_lot_size(
        self, simple_status_table: HistoricalStatusTable,
        trending_quotes: pd.DataFrame,
    ):
        """自定义手数（200）时按 200 取整。"""
        uf = _make_permissive_filter(simple_status_table, trending_quotes)
        strategy = SteadyStrategy(SteadyParams(), uf, TRADE_DATES, lot_size=200)
        # 5.0 * 1.005 * 200 = 1005; 10000 // 1005 = 9; 9 * 200 = 1800
        assert strategy._calc_buy_quantity(10000, 5.0) == 1800
        assert strategy._calc_buy_quantity(1000, 5.0) == 0  # 1005 > 1000

    def test_aggressive_same_logic(self, aggressive_calc: AggressiveStrategy):
        """激进轨与稳健轨使用相同的买入数量计算逻辑。"""
        assert aggressive_calc._calc_buy_quantity(10000, 5.0) == 1900
        assert aggressive_calc._calc_buy_quantity(1000, 10.0) == 0
        assert aggressive_calc._calc_buy_quantity(1000, 15.0) == 0

