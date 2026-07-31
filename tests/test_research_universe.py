"""Phase 3 历史时点（point-in-time）股票池过滤器综合 pytest 测试。

覆盖范围：
1. PointInTimeError 守卫：dt=None 或 status_table=None 时必须抛出
2. HistoricalStatusTable 查询：上市日期、退市日期、ST 状态、退市整理期
3. ST 过滤：000007 在 ST 转换前可交易、转换后不可交易
4. 退市过滤：000009 退市整理期和已退市均不可交易
5. 上市不足 120 日过滤：000006 上市较晚，早期不可交易
6. 停牌过滤：000004 停牌日不可交易
7. 流动性过滤：成交额低于阈值不可交易
8. 现金不足过滤：000005 价格过高，可用现金买不了一手
9. filter_on_date 批量过滤
10. load_historical_status 从 Parquet 加载
11. StrategyContext 上下文集成
12. Point-in-time 一致性：同一股票不同日期结果不同

数据由 tests.research_samples 合成，非真实行情。
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pandas as pd
import pytest

from ashare_quant.backtest.config import BacktestConfig
from ashare_quant.backtest.models import (
    EligibilityDecision,
    PortfolioSnapshot,
    Position,
    StrategyContext,
)
from ashare_quant.research.universe import (
    HistoricalStatusTable,
    HistoricalUniverseFilter,
    PointInTimeError,
    load_historical_status,
)
from tests.backtest_samples import make_trade_dates
from tests.research_samples import (
    make_historical_status_table,
    make_research_quotes,
    make_status_table_with_delisting,
    make_test_research_env,
)

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
START = date(2020, 1, 2)
N_DAYS = 200
N_STOCKS = 8

# 000007 ST 转换日：start + 90 天 = 2020-04-01
ST_TRANSITION = START + timedelta(days=90)

# 000009 退市相关日期
DELIST_DATE = START + timedelta(days=60)  # 2020-03-02
DELISTING_START = START + timedelta(days=30)  # 2020-02-01

# 000006 上市日期：start + 45 天 = 2020-02-16（周日）
LATE_LIST_DATE = START + timedelta(days=45)


# --------------------------------------------------------------------------- #
# 辅助函数
# --------------------------------------------------------------------------- #
def get_trading_dates(quotes: pd.DataFrame) -> list[date]:
    """从行情 DataFrame 提取排序后的唯一交易日列表。"""
    return sorted(pd.to_datetime(quotes["trade_date"]).dt.date.unique().tolist())


def first_on_or_after(dates: list[date], target: date) -> date:
    """返回 >= target 的第一个交易日。"""
    for d in dates:
        if d >= target:
            return d
    raise ValueError(f"没有 >= {target} 的交易日")


def last_before(dates: list[date], target: date) -> date:
    """返回 < target 的最后一个交易日。"""
    result = None
    for d in dates:
        if d < target:
            result = d
        else:
            break
    if result is None:
        raise ValueError(f"没有 < {target} 的交易日")
    return result


def make_permissive_filter(
    status_table: HistoricalStatusTable,
    quotes: pd.DataFrame,
    **overrides: Any,
) -> HistoricalUniverseFilter:
    """构建宽松参数过滤器，仅隔离目标过滤规则。

    默认放宽所有限制（min_listing_days=1, min_valid_days=1,
    min_turnover=0, available_cash=1_000_000），通过 overrides
    覆写需要测试的参数。
    """
    defaults: dict[str, Any] = dict(
        min_listing_days=1,
        min_valid_days=1,
        valid_days_window=20,
        min_turnover=0.0,
        turnover_window=20,
        lot_size=100,
        available_cash=1_000_000.0,
    )
    defaults.update(overrides)
    return HistoricalUniverseFilter(
        status_table=status_table,
        quotes=quotes,
        **defaults,
    )


def build_context(
    dt: date,
    quotes: pd.DataFrame,
    cash: float = 1_000_000.0,
) -> StrategyContext:
    """构建 StrategyContext，用于 is_eligible 的 context 参数。"""
    snap = PortfolioSnapshot(
        snapshot_date=dt,
        cash=Decimal(str(cash)),
        position_value=Decimal("0"),
        total_equity=Decimal(str(cash)),
    )
    if len(quotes) > 0:
        ts = pd.Timestamp(dt)
        bars = quotes[pd.to_datetime(quotes["trade_date"]) <= ts].copy()
    else:
        bars = quotes
    return StrategyContext(
        current_date=dt,
        portfolio=snap,
        positions={},
        bars_up_to_date=bars,
    )


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def quotes() -> pd.DataFrame:
    return make_research_quotes(start=START, n_days=N_DAYS, n_stocks=N_STOCKS)


@pytest.fixture(scope="module")
def status_df() -> pd.DataFrame:
    return make_historical_status_table(start=START, n_stocks=N_STOCKS)


@pytest.fixture(scope="module")
def status_table(status_df: pd.DataFrame) -> HistoricalStatusTable:
    return HistoricalStatusTable(records=status_df)


@pytest.fixture(scope="module")
def delisting_status_df() -> pd.DataFrame:
    return make_status_table_with_delisting(start=START)


@pytest.fixture(scope="module")
def delisting_status_table(
    delisting_status_df: pd.DataFrame,
) -> HistoricalStatusTable:
    return HistoricalStatusTable(records=delisting_status_df)


@pytest.fixture(scope="module")
def trading_dates(quotes: pd.DataFrame) -> list[date]:
    return get_trading_dates(quotes)


@pytest.fixture(scope="module")
def suspended_dates_000004(quotes: pd.DataFrame) -> list[date]:
    """000004 的停牌日期列表。"""
    mask = (quotes["symbol"] == "000004") & (quotes["is_suspended"] == True)  # noqa: E712
    return sorted(pd.to_datetime(quotes.loc[mask, "trade_date"]).dt.date.unique().tolist())


@pytest.fixture
def env() -> dict:
    """完整测试研究环境（每个测试独立实例）。"""
    return make_test_research_env(n_days=N_DAYS, n_stocks=N_STOCKS)


# --------------------------------------------------------------------------- #
# 1. PointInTimeError 守卫
# --------------------------------------------------------------------------- #
class TestPointInTimeGuard:
    """dt=None 或 status_table=None 时必须抛出 PointInTimeError。"""

    def test_get_status_none_dt_raises(self, status_table: HistoricalStatusTable):
        with pytest.raises(PointInTimeError):
            status_table.get_status("000001", None)

    def test_is_st_none_dt_raises(self, status_table: HistoricalStatusTable):
        with pytest.raises(PointInTimeError):
            status_table.is_st("000001", None)

    def test_is_delisting_none_dt_raises(self, status_table: HistoricalStatusTable):
        with pytest.raises(PointInTimeError):
            status_table.is_delisting("000001", None)

    def test_get_st_status_none_dt_raises(self, status_table: HistoricalStatusTable):
        with pytest.raises(PointInTimeError):
            status_table.get_st_status("000001", None)

    def test_is_eligible_none_dt_raises(
        self, status_table: HistoricalStatusTable, quotes: pd.DataFrame
    ):
        uf = HistoricalUniverseFilter(status_table=status_table, quotes=quotes)
        with pytest.raises(PointInTimeError):
            uf.is_eligible("000001", None, None)

    def test_is_eligible_none_dt_raises_with_context(
        self, status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        trading_dates: list[date],
    ):
        """即使提供了 context，dt=None 仍必须抛出。"""
        dt = trading_dates[150]
        ctx = build_context(dt, quotes)
        uf = HistoricalUniverseFilter(status_table=status_table, quotes=quotes)
        with pytest.raises(PointInTimeError):
            uf.is_eligible("000001", None, ctx)

    def test_filter_init_none_status_table_raises(self, quotes: pd.DataFrame):
        with pytest.raises(PointInTimeError):
            HistoricalUniverseFilter(status_table=None, quotes=quotes)

    def test_status_table_none_records_raises(self):
        with pytest.raises(PointInTimeError):
            HistoricalStatusTable(records=None)

    def test_status_table_missing_required_columns_raises(self):
        """缺少 symbol / st_status 列时抛出 ValueError。"""
        bad_df = pd.DataFrame({"code": ["000001"], "status": ["normal"]})
        with pytest.raises(ValueError, match="缺少必需列"):
            HistoricalStatusTable(records=bad_df)

    def test_status_table_missing_date_column_raises(self):
        """缺少 status_valid_from 和 date 列时抛出 ValueError。"""
        bad_df = pd.DataFrame({
            "symbol": ["000001"],
            "st_status": ["normal"],
        })
        with pytest.raises(ValueError, match="时点定位"):
            HistoricalStatusTable(records=bad_df)


# --------------------------------------------------------------------------- #
# 2. HistoricalStatusTable 查询方法
# --------------------------------------------------------------------------- #
class TestHistoricalStatusTableQueries:
    """HistoricalStatusTable 的 point-in-time 查询方法。"""

    # --- get_listed_date ---
    def test_get_listed_date_normal_stock(self, status_table: HistoricalStatusTable):
        assert status_table.get_listed_date("000001") == date(2019, 1, 2)

    def test_get_listed_date_late_listing(self, status_table: HistoricalStatusTable):
        assert status_table.get_listed_date("000006") == LATE_LIST_DATE

    def test_get_listed_date_unknown_symbol(self, status_table: HistoricalStatusTable):
        assert status_table.get_listed_date("999999") is None

    # --- get_delisted_date ---
    def test_get_delisted_date_non_delisted(self, status_table: HistoricalStatusTable):
        assert status_table.get_delisted_date("000001") is None

    def test_get_delisted_date_delisted_stock(
        self, delisting_status_table: HistoricalStatusTable
    ):
        assert delisting_status_table.get_delisted_date("000009") == DELIST_DATE

    # --- get_st_status ---
    def test_get_st_status_before_transition(self, status_table: HistoricalStatusTable):
        before = date(2020, 3, 31)
        assert status_table.get_st_status("000007", before) == "normal"

    def test_get_st_status_after_transition(self, status_table: HistoricalStatusTable):
        after = date(2020, 4, 1)
        assert status_table.get_st_status("000007", after) == "st"

    def test_get_st_status_unknown_symbol(self, status_table: HistoricalStatusTable):
        assert status_table.get_st_status("999999", date(2020, 6, 1)) == "unknown"

    def test_get_st_status_normal_stock(self, status_table: HistoricalStatusTable):
        assert status_table.get_st_status("000001", date(2020, 6, 1)) == "normal"

    # --- is_st ---
    def test_is_st_false_before_transition(self, status_table: HistoricalStatusTable):
        assert status_table.is_st("000007", date(2020, 3, 31)) is False

    def test_is_st_true_after_transition(self, status_table: HistoricalStatusTable):
        assert status_table.is_st("000007", date(2020, 4, 1)) is True

    def test_is_st_false_on_transition_boundary(self, status_table: HistoricalStatusTable):
        """ST 记录的 status_valid_from = 转换日，当日即为 ST。"""
        assert status_table.is_st("000007", ST_TRANSITION) is True

    # --- is_delisting ---
    def test_is_delisting_during_period(
        self, delisting_status_table: HistoricalStatusTable
    ):
        mid = date(2020, 2, 14)
        assert delisting_status_table.is_delisting("000009", mid) is True

    def test_is_delisting_before_period(
        self, delisting_status_table: HistoricalStatusTable
    ):
        before = date(2020, 1, 15)
        assert delisting_status_table.is_delisting("000009", before) is False

    def test_is_delisting_after_delist(
        self, delisting_status_table: HistoricalStatusTable
    ):
        """退市日后 get_status 返回 None（区间已结束），is_delisting 为 False。"""
        after = date(2020, 3, 15)
        assert delisting_status_table.is_delisting("000009", after) is False

    # --- is_delisted ---
    def test_is_delisted_after_date(
        self, delisting_status_table: HistoricalStatusTable
    ):
        assert delisting_status_table.is_delisted("000009", DELIST_DATE) is True

    def test_is_delisted_before_date(
        self, delisting_status_table: HistoricalStatusTable
    ):
        before = date(2020, 2, 28)
        assert delisting_status_table.is_delisted("000009", before) is False

    def test_is_delisted_non_delisted_stock(self, status_table: HistoricalStatusTable):
        assert status_table.is_delisted("000001", date(2025, 1, 1)) is False

    # --- get_status ---
    def test_get_status_unknown_symbol_returns_none(
        self, status_table: HistoricalStatusTable
    ):
        assert status_table.get_status("999999", date(2020, 6, 1)) is None

    def test_get_status_returns_latest_match(self, status_table: HistoricalStatusTable):
        """多条匹配时取 status_valid_from 最晚的记录。"""
        record = status_table.get_status("000007", date(2020, 6, 1))
        assert record is not None
        assert record["st_status"] == "st"

    def test_get_status_no_match_before_listing(
        self, status_table: HistoricalStatusTable
    ):
        """000006 在上市前无有效状态记录。"""
        before_listing = date(2019, 6, 1)
        assert status_table.get_status("000006", before_listing) is None


# --------------------------------------------------------------------------- #
# 3. ST 过滤
# --------------------------------------------------------------------------- #
class TestSTFilter:
    """ST 状态过滤：000007 在 ST 转换前可交易、转换后不可交易。"""

    def test_normal_before_st_transition_eligible(
        self, status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        trading_dates: list[date],
    ):
        """000007 在 ST 转换前为正常状态，应可交易。"""
        before = last_before(trading_dates, ST_TRANSITION)
        uf = make_permissive_filter(status_table, quotes)
        decision = uf.is_eligible("000007", before, None)
        assert isinstance(decision, EligibilityDecision)
        assert decision.eligible is True
        assert decision.reason == ""

    def test_st_after_transition_ineligible(
        self, status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        trading_dates: list[date],
    ):
        """000007 在 ST 转换后为 ST 状态，不可交易。"""
        after = first_on_or_after(trading_dates, ST_TRANSITION)
        uf = make_permissive_filter(status_table, quotes)
        decision = uf.is_eligible("000007", after, None)
        assert decision.eligible is False

    def test_st_filter_reason_contains_st(
        self, status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        trading_dates: list[date],
    ):
        """ST 拒绝原因必须包含 ST 标识。"""
        after = first_on_or_after(trading_dates, ST_TRANSITION)
        uf = make_permissive_filter(status_table, quotes)
        decision = uf.is_eligible("000007", after, None)
        assert "ST" in decision.reason

    def test_st_filter_checked_before_listing_days(
        self, status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        trading_dates: list[date],
    ):
        """ST 检查优先于上市天数检查：即使上市不足 120 日，ST 也先拒绝。"""
        after = first_on_or_after(trading_dates, ST_TRANSITION)
        uf = make_permissive_filter(status_table, quotes, min_listing_days=120)
        decision = uf.is_eligible("000007", after, None)
        assert decision.eligible is False
        assert "ST" in decision.reason


# --------------------------------------------------------------------------- #
# 4. 退市过滤
# --------------------------------------------------------------------------- #
class TestDelistingFilter:
    """退市整理期和已退市过滤：000009。"""

    def test_delisting_period_ineligible(
        self, delisting_status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        trading_dates: list[date],
    ):
        """000009 在退市整理期内不可交易。"""
        dt = first_on_or_after(trading_dates, DELISTING_START)
        uf = make_permissive_filter(delisting_status_table, quotes)
        decision = uf.is_eligible("000009", dt, None)
        assert decision.eligible is False

    def test_delisting_reason(
        self, delisting_status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        trading_dates: list[date],
    ):
        """退市整理期拒绝原因必须包含 '退市整理'。"""
        dt = first_on_or_after(trading_dates, DELISTING_START)
        uf = make_permissive_filter(delisting_status_table, quotes)
        decision = uf.is_eligible("000009", dt, None)
        assert "退市整理" in decision.reason

    def test_delisted_ineligible(
        self, delisting_status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        trading_dates: list[date],
    ):
        """000009 在退市日后不可交易。"""
        dt = first_on_or_after(trading_dates, DELIST_DATE)
        uf = make_permissive_filter(delisting_status_table, quotes)
        decision = uf.is_eligible("000009", dt, None)
        assert decision.eligible is False

    def test_delisted_reason(
        self, delisting_status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        trading_dates: list[date],
    ):
        """已退市拒绝原因必须包含 '已退市'。"""
        dt = first_on_or_after(trading_dates, DELIST_DATE)
        uf = make_permissive_filter(delisting_status_table, quotes)
        decision = uf.is_eligible("000009", dt, None)
        assert "已退市" in decision.reason

    def test_normal_stock_not_delisted(
        self, delisting_status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        trading_dates: list[date],
    ):
        """正常股票 000001 不受退市影响，应可交易。"""
        dt = trading_dates[150]
        uf = make_permissive_filter(delisting_status_table, quotes)
        decision = uf.is_eligible("000001", dt, None)
        assert decision.eligible is True


# --------------------------------------------------------------------------- #
# 5. 上市不足 120 日过滤
# --------------------------------------------------------------------------- #
class TestListingDaysFilter:
    """上市不足 120 个交易日过滤：000006 上市较晚。"""

    def test_late_listing_ineligible_before_120_days(
        self, status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        trading_dates: list[date],
    ):
        """000006 上市较晚，在上市不足 120 日时不可交易。"""
        # dates[150]：000006 上市后约 119 个交易日 < 120
        dt = trading_dates[150]
        uf = make_permissive_filter(status_table, quotes, min_listing_days=120)
        decision = uf.is_eligible("000006", dt, None)
        assert decision.eligible is False

    def test_late_listing_eligible_after_120_days(
        self, status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        trading_dates: list[date],
    ):
        """000006 在上市满 120 日后可交易。"""
        # dates[151]：000006 上市后约 120 个交易日 >= 120
        dt = trading_dates[151]
        uf = make_permissive_filter(status_table, quotes, min_listing_days=120)
        decision = uf.is_eligible("000006", dt, None)
        assert decision.eligible is True

    def test_normal_stock_eligible_when_late_listing_not(
        self, status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        trading_dates: list[date],
    ):
        """000001（上市较早）可交易，000006（上市较晚）不可交易。"""
        dt = trading_dates[150]
        uf = make_permissive_filter(status_table, quotes, min_listing_days=120)
        d1 = uf.is_eligible("000001", dt, None)
        d6 = uf.is_eligible("000006", dt, None)
        assert d1.eligible is True
        assert d6.eligible is False

    def test_listing_days_reason(
        self, status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        trading_dates: list[date],
    ):
        """上市天数不足拒绝原因必须包含上市天数信息。"""
        dt = trading_dates[150]
        uf = make_permissive_filter(status_table, quotes, min_listing_days=120)
        decision = uf.is_eligible("000006", dt, None)
        assert decision.eligible is False
        assert "上市" in decision.reason
        assert "120" in decision.reason

    def test_no_listed_date_ineligible(
        self, status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        trading_dates: list[date],
    ):
        """状态表中无上市日期的股票不可交易。"""
        dt = trading_dates[150]
        uf = make_permissive_filter(status_table, quotes)
        decision = uf.is_eligible("999999", dt, None)
        assert decision.eligible is False
        assert "上市日期" in decision.reason


# --------------------------------------------------------------------------- #
# 6. 停牌过滤
# --------------------------------------------------------------------------- #
class TestSuspendedFilter:
    """停牌过滤：000004 中途停牌 2 天。"""

    def test_suspended_day_ineligible(
        self, status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        suspended_dates_000004: list[date],
    ):
        """000004 在停牌日不可交易。"""
        assert len(suspended_dates_000004) == 2, "000004 应有 2 个停牌日"
        dt = suspended_dates_000004[0]
        uf = make_permissive_filter(status_table, quotes)
        decision = uf.is_eligible("000004", dt, None)
        assert decision.eligible is False

    def test_suspended_reason(
        self, status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        suspended_dates_000004: list[date],
    ):
        """停牌拒绝原因必须包含 '停牌' 或 '不可交易'。"""
        dt = suspended_dates_000004[0]
        uf = make_permissive_filter(status_table, quotes)
        decision = uf.is_eligible("000004", dt, None)
        assert "停牌" in decision.reason or "不可交易" in decision.reason

    def test_normal_day_eligible_near_suspension(
        self, status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        suspended_dates_000004: list[date], trading_dates: list[date],
    ):
        """000004 在停牌前的正常交易日可交易。"""
        dt = last_before(trading_dates, suspended_dates_000004[0])
        uf = make_permissive_filter(status_table, quotes)
        decision = uf.is_eligible("000004", dt, None)
        assert decision.eligible is True

    def test_second_suspended_day_ineligible(
        self, status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        suspended_dates_000004: list[date],
    ):
        """000004 第二个停牌日也不可交易。"""
        dt = suspended_dates_000004[1]
        uf = make_permissive_filter(status_table, quotes)
        decision = uf.is_eligible("000004", dt, None)
        assert decision.eligible is False

    def test_valid_days_reduced_by_suspension(
        self, status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        suspended_dates_000004: list[date], trading_dates: list[date],
    ):
        """停牌减少有效交易天数：提高 min_valid_days 可使 000004 不达标。"""
        # 取停牌后第一个交易日
        dt = first_on_or_after(trading_dates, suspended_dates_000004[1] + timedelta(days=1))
        # 最近 20 个交易日中有 2 天停牌 -> 有效 18 天
        # 设置 min_valid_days=19 使其不达标
        uf = make_permissive_filter(
            status_table, quotes, min_valid_days=19, valid_days_window=20,
        )
        decision = uf.is_eligible("000004", dt, None)
        assert decision.eligible is False
        assert "有效交易" in decision.reason


# --------------------------------------------------------------------------- #
# 7. 流动性过滤
# --------------------------------------------------------------------------- #
class TestLiquidityFilter:
    """流动性（成交额）过滤。"""

    def test_low_turnover_ineligible(
        self, status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        trading_dates: list[date],
    ):
        """000001 平均成交额约 235 万 < 300 万阈值 -> 不可交易。"""
        dt = trading_dates[160]
        uf = make_permissive_filter(
            status_table, quotes, min_turnover=3_000_000.0,
        )
        decision = uf.is_eligible("000001", dt, None)
        assert decision.eligible is False

    def test_high_turnover_eligible(
        self, status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        trading_dates: list[date],
    ):
        """000005 平均成交额约 523 万 >= 300 万阈值 -> 可交易。"""
        dt = trading_dates[160]
        uf = make_permissive_filter(
            status_table, quotes, min_turnover=3_000_000.0,
        )
        decision = uf.is_eligible("000005", dt, None)
        assert decision.eligible is True

    def test_liquidity_reason(
        self, status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        trading_dates: list[date],
    ):
        """流动性不足拒绝原因必须包含成交额信息。"""
        dt = trading_dates[160]
        uf = make_permissive_filter(
            status_table, quotes, min_turnover=3_000_000.0,
        )
        decision = uf.is_eligible("000001", dt, None)
        assert decision.eligible is False
        assert "成交额" in decision.reason

    def test_all_filtered_with_default_threshold(self, env: dict):
        """使用默认 env（min_turnover=20M）所有股票均因流动性被过滤。"""
        uf = env["universe_filter"]
        quotes = env["quotes"]
        trading_dates = get_trading_dates(quotes)
        dt = trading_dates[160]
        results = uf.filter_on_date(dt)
        assert len(results) > 0
        # 所有股票都不可交易
        for sym, decision in results.items():
            assert decision.eligible is False, (
                f"{sym} 应被过滤，实际 eligible={decision.eligible}, "
                f"reason={decision.reason}"
            )

    def test_turnover_zero_threshold_allows(
        self, status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        trading_dates: list[date],
    ):
        """min_turnover=0 时流动性检查不构成障碍。"""
        dt = trading_dates[160]
        uf = make_permissive_filter(status_table, quotes, min_turnover=0.0)
        decision = uf.is_eligible("000001", dt, None)
        assert decision.eligible is True


# --------------------------------------------------------------------------- #
# 8. 现金不足过滤
# --------------------------------------------------------------------------- #
class TestCashFilter:
    """可用现金不足购买一手的过滤。"""

    def test_expensive_stock_ineligible_low_cash(
        self, status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        trading_dates: list[date],
    ):
        """000005 价格 15 元，一手 1500 元 > 可用现金 1000 元 -> 不可交易。"""
        dt = trading_dates[160]
        uf = make_permissive_filter(
            status_table, quotes, available_cash=1000.0,
        )
        decision = uf.is_eligible("000005", dt, None)
        assert decision.eligible is False

    def test_cash_reason(
        self, status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        trading_dates: list[date],
    ):
        """现金不足拒绝原因必须包含一手成本或可用现金信息。"""
        dt = trading_dates[160]
        uf = make_permissive_filter(
            status_table, quotes, available_cash=1000.0,
        )
        decision = uf.is_eligible("000005", dt, None)
        assert decision.eligible is False
        assert "一手成本" in decision.reason or "可用现金" in decision.reason

    def test_expensive_stock_eligible_high_cash(
        self, status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        trading_dates: list[date],
    ):
        """000005 价格 15 元，一手 1500 元 <= 可用现金 2000 元 -> 可交易。"""
        dt = trading_dates[160]
        uf = make_permissive_filter(
            status_table, quotes, available_cash=2000.0,
        )
        decision = uf.is_eligible("000005", dt, None)
        assert decision.eligible is True

    def test_normal_stock_eligible_low_cash(
        self, status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        trading_dates: list[date],
    ):
        """000001 价格约 5 元，一手约 500 元 <= 可用现金 1000 元 -> 可交易。"""
        dt = trading_dates[160]
        uf = make_permissive_filter(
            status_table, quotes, available_cash=1000.0,
        )
        decision = uf.is_eligible("000001", dt, None)
        assert decision.eligible is True

    def test_cash_none_skips_check(
        self, status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        trading_dates: list[date],
    ):
        """available_cash=None 时跳过现金检查（引擎内从 context 读取）。"""
        dt = trading_dates[160]
        uf = HistoricalUniverseFilter(
            status_table=status_table,
            quotes=quotes,
            min_listing_days=1,
            min_valid_days=1,
            min_turnover=0.0,
            available_cash=None,
        )
        # 不传 context -> cash=None -> 跳过现金检查
        decision = uf.is_eligible("000005", dt, None)
        assert decision.eligible is True


# --------------------------------------------------------------------------- #
# 9. filter_on_date 批量过滤
# --------------------------------------------------------------------------- #
class TestFilterOnDate:
    """filter_on_date 批量过滤。"""

    def test_returns_all_symbols(
        self, status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        trading_dates: list[date],
    ):
        """filter_on_date 返回截至当日的所有 symbol。"""
        dt = trading_dates[160]
        uf = make_permissive_filter(
            status_table, quotes, min_listing_days=120,
        )
        results = uf.filter_on_date(dt)
        # 截至 dt 的所有 symbol 都在结果中
        q_up_to = quotes[pd.to_datetime(quotes["trade_date"]) <= pd.Timestamp(dt)]
        expected_symbols = set(q_up_to["symbol"].unique())
        assert set(results.keys()) == expected_symbols

    def test_mixed_eligible_ineligible(
        self, status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        trading_dates: list[date],
    ):
        """混合结果：正常股票可交易，ST/上市不足不可交易。"""
        dt = trading_dates[160]
        uf = make_permissive_filter(
            status_table, quotes, min_listing_days=120,
        )
        results = uf.filter_on_date(dt)
        # 000007 为 ST -> 不可交易
        assert results["000007"].eligible is False
        # 000001 正常 -> 可交易
        assert results["000001"].eligible is True
        # 000006 上市 129 日 >= 120 -> 可交易
        assert results["000006"].eligible is True

    def test_all_ineligible_with_default_env(self, env: dict):
        """默认 env 过滤器（min_turnover=20M）所有股票不可交易。"""
        uf = env["universe_filter"]
        quotes = env["quotes"]
        trading_dates = get_trading_dates(quotes)
        dt = trading_dates[160]
        results = uf.filter_on_date(dt)
        assert len(results) == N_STOCKS
        for decision in results.values():
            assert decision.eligible is False

    def test_empty_quotes_returns_empty(self, status_table: HistoricalStatusTable):
        """空行情 -> 空结果。"""
        uf = HistoricalUniverseFilter(
            status_table=status_table,
            quotes=pd.DataFrame(),
        )
        results = uf.filter_on_date(date(2020, 6, 1))
        assert results == {}

    def test_dt_before_data_returns_empty(
        self, status_table: HistoricalStatusTable, quotes: pd.DataFrame,
    ):
        """信号日早于所有行情 -> 空结果。"""
        uf = HistoricalUniverseFilter(
            status_table=status_table, quotes=quotes,
        )
        results = uf.filter_on_date(date(2019, 1, 1))
        assert results == {}

    def test_filter_on_date_consistent_with_is_eligible(
        self, status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        trading_dates: list[date],
    ):
        """filter_on_date 与逐个 is_eligible 结果一致。"""
        dt = trading_dates[160]
        uf = make_permissive_filter(
            status_table, quotes, min_listing_days=120,
        )
        batch = uf.filter_on_date(dt)
        for sym in batch:
            single = uf.is_eligible(sym, dt, None)
            assert batch[sym].eligible == single.eligible, (
                f"{sym}: batch={batch[sym].eligible}, single={single.eligible}"
            )


# --------------------------------------------------------------------------- #
# 10. load_historical_status 从 Parquet 加载
# --------------------------------------------------------------------------- #
class TestLoadHistoricalStatus:
    """load_historical_status 从 Parquet 文件加载。"""

    def test_load_roundtrip(
        self, tmp_path, status_df: pd.DataFrame,
        trading_dates: list[date],
    ):
        """写入 Parquet 再加载，查询结果一致。"""
        parquet_path = tmp_path / "status.parquet"
        status_df.to_parquet(parquet_path, index=False)
        loaded = load_historical_status(str(parquet_path))
        assert isinstance(loaded, HistoricalStatusTable)
        # 验证查询结果与原始一致
        assert loaded.get_listed_date("000001") == date(2019, 1, 2)
        assert loaded.get_listed_date("000006") == LATE_LIST_DATE
        assert loaded.is_st("000007", date(2020, 3, 31)) is False
        assert loaded.is_st("000007", date(2020, 4, 1)) is True
        assert loaded.get_st_status("000001", date(2020, 6, 1)) == "normal"

    def test_load_file_not_found(self):
        """文件不存在时抛出 FileNotFoundError。"""
        with pytest.raises(FileNotFoundError):
            load_historical_status("/nonexistent/path/status.parquet")

    def test_load_delisting_status_roundtrip(
        self, tmp_path, delisting_status_df: pd.DataFrame,
    ):
        """退市状态表 Parquet 往返加载。"""
        parquet_path = tmp_path / "delisting.parquet"
        delisting_status_df.to_parquet(parquet_path, index=False)
        loaded = load_historical_status(str(parquet_path))
        assert loaded.is_delisting("000009", date(2020, 2, 14)) is True
        assert loaded.is_delisted("000009", DELIST_DATE) is True
        assert loaded.get_delisted_date("000009") == DELIST_DATE


# --------------------------------------------------------------------------- #
# 11. StrategyContext 上下文集成
# --------------------------------------------------------------------------- #
class TestStrategyContextIntegration:
    """is_eligible 通过 StrategyContext 获取行情和现金。"""

    def test_context_cash_used(
        self, status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        trading_dates: list[date],
    ):
        """context.portfolio.cash 覆盖初始化的 available_cash。"""
        dt = trading_dates[160]
        # 初始化时 available_cash 很高
        uf = HistoricalUniverseFilter(
            status_table=status_table, quotes=quotes,
            min_listing_days=1, min_valid_days=1, min_turnover=0.0,
            available_cash=1_000_000.0,
        )
        # context 中现金很低
        ctx = build_context(dt, quotes, cash=1000.0)
        decision = uf.is_eligible("000005", dt, ctx)
        # 000005 一手 1500 > 1000 -> 不可交易
        assert decision.eligible is False
        assert "一手成本" in decision.reason or "可用现金" in decision.reason

    def test_context_high_cash_overrides_init(
        self, status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        trading_dates: list[date],
    ):
        """context 高现金覆盖初始化低现金。"""
        dt = trading_dates[160]
        uf = HistoricalUniverseFilter(
            status_table=status_table, quotes=quotes,
            min_listing_days=1, min_valid_days=1, min_turnover=0.0,
            available_cash=100.0,  # 初始化很低
        )
        ctx = build_context(dt, quotes, cash=100_000.0)  # context 很高
        decision = uf.is_eligible("000005", dt, ctx)
        assert decision.eligible is True

    def test_no_context_uses_init_config(
        self, status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        trading_dates: list[date],
    ):
        """context=None 时使用初始化的 quotes 和 available_cash。"""
        dt = trading_dates[160]
        uf = HistoricalUniverseFilter(
            status_table=status_table, quotes=quotes,
            min_listing_days=1, min_valid_days=1, min_turnover=0.0,
            available_cash=1000.0,
        )
        # 不传 context
        decision = uf.is_eligible("000005", dt, None)
        assert decision.eligible is False  # 1500 > 1000

        decision_ok = uf.is_eligible("000001", dt, None)
        assert decision_ok.eligible is True  # ~500 < 1000

    def test_context_quotes_subset(
        self, status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        trading_dates: list[date],
    ):
        """context.bars_up_to_date 截断行情时仍可正确过滤。"""
        dt = trading_dates[160]
        # 只传入截至 dt 的行情子集
        ts = pd.Timestamp(dt)
        subset = quotes[pd.to_datetime(quotes["trade_date"]) <= ts].copy()
        uf = HistoricalUniverseFilter(
            status_table=status_table, quotes=quotes,
            min_listing_days=120, min_valid_days=15, min_turnover=0.0,
            available_cash=1_000_000.0,
        )
        ctx = build_context(dt, subset, cash=1_000_000.0)
        decision = uf.is_eligible("000001", dt, ctx)
        assert decision.eligible is True

    def test_context_none_bars_falls_back(
        self, status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        trading_dates: list[date],
    ):
        """context.bars_up_to_date=None 时回退到初始化 quotes。"""
        dt = trading_dates[160]
        uf = HistoricalUniverseFilter(
            status_table=status_table, quotes=quotes,
            min_listing_days=1, min_valid_days=1, min_turnover=0.0,
            available_cash=1_000_000.0,
        )
        snap = PortfolioSnapshot(
            snapshot_date=dt,
            cash=Decimal("1000000"),
            position_value=Decimal("0"),
            total_equity=Decimal("1000000"),
        )
        ctx = StrategyContext(
            current_date=dt, portfolio=snap, positions={},
            bars_up_to_date=None,
        )
        decision = uf.is_eligible("000001", dt, ctx)
        assert decision.eligible is True


# --------------------------------------------------------------------------- #
# 12. Point-in-time 一致性
# --------------------------------------------------------------------------- #
class TestPointInTimeConsistency:
    """Point-in-time 一致性：同一股票在不同日期结果不同。"""

    def test_st_status_changes_over_time(
        self, status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        trading_dates: list[date],
    ):
        """000007 在 ST 转换前可交易、转换后不可交易。"""
        before = last_before(trading_dates, ST_TRANSITION)
        after = first_on_or_after(trading_dates, ST_TRANSITION)
        uf = make_permissive_filter(status_table, quotes)

        d_before = uf.is_eligible("000007", before, None)
        d_after = uf.is_eligible("000007", after, None)
        assert d_before.eligible is True
        assert d_after.eligible is False
        assert d_before.eligible != d_after.eligible

    def test_st_status_at_exact_transition(
        self, status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        trading_dates: list[date],
    ):
        """ST 转换当日即为 ST 状态。"""
        transition = first_on_or_after(trading_dates, ST_TRANSITION)
        uf = make_permissive_filter(status_table, quotes)
        # 转换当日
        d = uf.is_eligible("000007", transition, None)
        assert d.eligible is False
        assert "ST" in d.reason

    def test_listing_eligibility_changes_over_time(
        self, status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        trading_dates: list[date],
    ):
        """000006 上市天数随时间增长，从不可交易变为可交易。"""
        uf = make_permissive_filter(status_table, quotes, min_listing_days=120)
        early = trading_dates[150]  # 119 日 < 120
        late = trading_dates[151]   # 120 日 >= 120
        d_early = uf.is_eligible("000006", early, None)
        d_late = uf.is_eligible("000006", late, None)
        assert d_early.eligible is False
        assert d_late.eligible is True

    def test_filter_uses_historical_not_current_status(
        self, status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        trading_dates: list[date],
    ):
        """过滤器使用历史状态而非当前状态：000007 当前是 ST，但历史上正常。

        若错误使用当前状态，000007 在所有日期都会被拒绝。
        正确的 point-in-time 行为是：转换日前不拒绝。
        """
        before = last_before(trading_dates, ST_TRANSITION)
        uf = make_permissive_filter(status_table, quotes)
        # 000007 当前（2020-06-01）是 ST，但 before 日期是 normal
        current_status = status_table.get_st_status("000007", date(2020, 6, 1))
        assert current_status == "st"
        # 但在 before 日期，point-in-time 状态是 normal
        historical_status = status_table.get_st_status("000007", before)
        assert historical_status == "normal"
        # 过滤器使用历史状态 -> 可交易
        decision = uf.is_eligible("000007", before, None)
        assert decision.eligible is True

    def test_delisted_stock_normal_before_delisting(
        self, delisting_status_table: HistoricalStatusTable, quotes: pd.DataFrame,
        trading_dates: list[date],
    ):
        """000009 在退市前为正常状态（但有行情缺失），退市整理期不可交易。"""
        before_delisting = date(2020, 1, 15)
        during_delisting = first_on_or_after(trading_dates, DELISTING_START)
        uf = make_permissive_filter(delisting_status_table, quotes)

        # 退市整理期不可交易
        d_during = uf.is_eligible("000009", during_delisting, None)
        assert d_during.eligible is False
        assert "退市整理" in d_during.reason

        # 退市前状态为 normal（但无行情 -> 当日无行情记录）
        d_before = uf.is_eligible("000009", before_delisting, None)
        assert d_before.eligible is False
        assert "无行情记录" in d_before.reason
