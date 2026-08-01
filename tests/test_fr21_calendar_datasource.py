"""FR-21 覆盖率冲刺（一）：``calendar`` 与 ``datasource`` 模块补测。

目标：把这两个模块从 ~63%/67% 提到接近 100%，支撑 automation 包整体 ≥90%。
全部离线、不联网、不接触券商。
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta

import pandas as pd
import pytest

from ashare_quant.automation.calendar import (
    CalendarUnavailableError,
    TradingCalendar,
    load_trading_calendar,
    normalize_date,
)
from ashare_quant.automation.datasource import (
    DataUnavailableError,
    InjectedDataSource,
    LocalParquetDataSource,
    MarketDataBundle,
    QUOTE_COLUMNS,
    UnavailableDataSource,
    build_default_data_source,
    lookback_start,
    normalize_quotes,
    require_quote_columns,
)


def _dates(*spec: str) -> list[date]:
    return [date.fromisoformat(s) for s in spec]


def _cal() -> TradingCalendar:
    return TradingCalendar.from_dates(
        _dates("2020-01-02", "2020-01-03", "2020-01-06", "2020-01-07", "2020-01-08")
    )


# =========================================================================== #
# calendar
# =========================================================================== #


class TestNormalizeDate:
    def test_datetime(self) -> None:
        assert normalize_date(datetime(2020, 1, 2, 9, 30)) == date(2020, 1, 2)

    def test_date(self) -> None:
        assert normalize_date(date(2020, 1, 2)) == date(2020, 1, 2)

    def test_pd_timestamp(self) -> None:
        assert normalize_date(pd.Timestamp("2020-01-02")) == date(2020, 1, 2)

    def test_str(self) -> None:
        assert normalize_date("  2020-01-02  ") == date(2020, 1, 2)

    def test_fallback(self) -> None:
        assert normalize_date("20200102") == date(2020, 1, 2)


class TestTradingCalendarBasics:
    def test_len_and_dates(self) -> None:
        cal = _cal()
        assert len(cal) == 5
        assert cal.dates == tuple(cal._dates)
        assert cal.first_date == date(2020, 1, 2)
        assert cal.last_date == date(2020, 1, 8)

    def test_contains(self) -> None:
        cal = _cal()
        assert date(2020, 1, 3) in cal
        assert date(2020, 1, 4) not in cal
        assert "2020-01-06" in cal

    def test_covers(self) -> None:
        cal = _cal()
        assert cal.covers(date(2020, 1, 5)) is True
        assert cal.covers(date(2020, 1, 1)) is False
        assert cal.covers(date(2020, 1, 9)) is False

    def test_empty_raises(self) -> None:
        with pytest.raises(CalendarUnavailableError):
            TradingCalendar.from_dates([])

    def test_summary(self) -> None:
        cal = TradingCalendar(_dates("2020-01-02"), source="mem",
                              generated_at=datetime(2020, 1, 1, 0, 0))
        s = cal.summary()
        assert s["source"] == "mem"
        assert s["trading_day_count"] == 1
        assert s["generated_at"] is not None


class TestTradingCalendarQueries:
    def test_is_trading_day_and_coverage_error(self) -> None:
        cal = _cal()
        assert cal.is_trading_day("2020-01-06") is True
        assert cal.is_trading_day("2020-01-04") is False
        with pytest.raises(CalendarUnavailableError):
            cal.is_trading_day("1999-01-01")

    def test_previous_trading_day(self) -> None:
        cal = _cal()
        # 向后回溯（d > last_date）
        assert cal.previous_trading_day(date(2020, 2, 1)) == date(2020, 1, 8)
        assert cal.previous_trading_day(date(2020, 1, 6)) == date(2020, 1, 3)
        assert cal.previous_trading_day(date(2020, 1, 6), inclusive=True) == date(2020, 1, 6)
        with pytest.raises(CalendarUnavailableError):
            cal.previous_trading_day(date(2020, 1, 1))

    def test_next_trading_day(self) -> None:
        cal = _cal()
        # 向前回溯（d < first_date）
        assert cal.next_trading_day(date(2019, 12, 1)) == date(2020, 1, 2)
        assert cal.next_trading_day(date(2020, 1, 3)) == date(2020, 1, 6)
        assert cal.next_trading_day(date(2020, 1, 3), inclusive=True) == date(2020, 1, 3)
        with pytest.raises(CalendarUnavailableError):
            cal.next_trading_day(date(2021, 1, 1))

    def test_data_expected_ready_time(self) -> None:
        cal = _cal()
        dt = cal.data_expected_ready_time(date(2020, 1, 2), dtime(18, 30))
        assert dt == datetime(2020, 1, 2, 18, 30)

    def test_latest_completed_trading_day(self) -> None:
        cal = _cal()
        # today > last_date -> 错误
        with pytest.raises(CalendarUnavailableError):
            cal.latest_completed_trading_day(datetime(2021, 1, 1, 12, 0), dtime(18, 30))
        # 当日且未到就绪时间 -> 退一个交易日
        out = cal.latest_completed_trading_day(
            datetime(2020, 1, 6, 9, 0), dtime(18, 30)
        )
        assert out == date(2020, 1, 3)
        # 当日且已到就绪时间
        out2 = cal.latest_completed_trading_day(
            datetime(2020, 1, 6, 20, 0), dtime(18, 30)
        )
        assert out2 == date(2020, 1, 6)

    def test_trading_days_between_and_count(self) -> None:
        cal = _cal()
        assert cal.trading_days_between(date(2020, 1, 9), date(2020, 1, 1)) == []
        days = cal.trading_days_between(date(2020, 1, 2), date(2020, 1, 6))
        assert days == _dates("2020-01-02", "2020-01-03", "2020-01-06")
        assert cal.count_trading_days(date(2020, 1, 2), date(2020, 1, 8)) == 5

    def test_shift(self) -> None:
        cal = _cal()
        assert cal.shift(date(2020, 1, 3), 1) == date(2020, 1, 6)
        assert cal.shift(date(2020, 1, 3), -1) == date(2020, 1, 2)
        with pytest.raises(CalendarUnavailableError):
            cal.shift(date(2020, 1, 4), 1)  # 非交易日
        with pytest.raises(CalendarUnavailableError):
            cal.shift(date(2020, 1, 2), -1)  # 越界


class TestCalendarFreshnessAndLoad:
    def test_assert_fresh_ok(self) -> None:
        cal = _cal()
        cal.assert_fresh(date(2020, 1, 8), max_staleness_days=5)
        cal.assert_fresh(date(2020, 1, 6), max_staleness_days=5)

    def test_assert_fresh_stale(self) -> None:
        cal = _cal()
        with pytest.raises(CalendarUnavailableError):
            cal.assert_fresh(date(2020, 2, 1), max_staleness_days=5)

    def test_from_dataframe(self) -> None:
        df = pd.DataFrame({"trade_date": _dates("2020-01-02", "2020-01-03"),
                           "is_open": [True, False]})
        cal = TradingCalendar.from_dataframe(df)
        assert len(cal) == 1
        # 缺失日期列
        with pytest.raises(CalendarUnavailableError):
            TradingCalendar.from_dataframe(pd.DataFrame({"x": [1]}))
        # 空 / None
        with pytest.raises(CalendarUnavailableError):
            TradingCalendar.from_dataframe(pd.DataFrame())
        with pytest.raises(CalendarUnavailableError):
            TradingCalendar.from_dataframe(None)

    def test_from_parquet_errors(self, tmp_path) -> None:
        # 文件不存在
        with pytest.raises(CalendarUnavailableError):
            TradingCalendar.from_parquet(tmp_path / "nope.parquet")
        # 读取失败（损坏文件）
        bad = tmp_path / "bad.parquet"
        bad.write_text("not a parquet", encoding="utf-8")
        with pytest.raises(CalendarUnavailableError):
            TradingCalendar.from_parquet(bad)

    def test_from_parquet_ok(self, tmp_path) -> None:
        df = pd.DataFrame({"trade_date": _dates("2020-01-02", "2020-01-03"),
                           "is_open": [True, True]})
        p = tmp_path / "cal.parquet"
        df.to_parquet(p, index=False)
        cal = TradingCalendar.from_parquet(p)
        assert len(cal) == 2

    def test_load_trading_calendar_require_false(self) -> None:
        class _Cal:
            require_calendar = False
            date_column = "trade_date"
            is_open_column = "is_open"
            max_staleness_days = 5

        class _Cfg:
            calendar = _Cal()
            calendar_path = "x"

        with pytest.raises(CalendarUnavailableError):
            load_trading_calendar(_Cfg())

    def test_load_trading_calendar_injected_freshness(self) -> None:
        class _Cal:
            require_calendar = True
            date_column = "trade_date"
            is_open_column = "is_open"
            max_staleness_days = 5

        class _Cfg:
            calendar = _Cal()
            calendar_path = "x"

        cal = _cal()
        # 未过期：通过
        out = load_trading_calendar(_Cfg(), calendar=cal, as_of=date(2020, 1, 8))
        assert out is cal
        # 过期：失败
        with pytest.raises(CalendarUnavailableError):
            load_trading_calendar(_Cfg(), calendar=cal, as_of=date(2021, 1, 1))


# =========================================================================== #
# datasource
# =========================================================================== #


def _quote_rows() -> pd.DataFrame:
    """构造满足 QUOTE_COLUMNS 的最小行情。"""
    rows = []
    for sym in ("000001", "000002"):
        for d in ("2020-01-02", "2020-01-03"):
            rows.append({
                "symbol": sym,
                "trade_date": date.fromisoformat(d),
                "open_raw": 10.0, "high_raw": 11.0, "low_raw": 9.0, "close_raw": 10.5,
                "open_qfq": 10.0, "high_qfq": 11.0, "low_qfq": 9.0, "close_qfq": 10.5,
                "volume": 1000.0, "amount": 10500.0,
                "is_suspended": False, "is_tradable": True,
            })
    return pd.DataFrame(rows)


class TestMarketDataBundle:
    def test_empty_symbols_and_range(self) -> None:
        b = MarketDataBundle(quotes=pd.DataFrame(), source="x")
        assert b.is_empty is True
        assert b.symbols == []
        assert b.date_range == (None, None)
        assert b.latest_date() is None
        assert b.covers(date(2020, 1, 1)) is False
        prov = b.provenance()
        assert prov["rows"] == 0
        assert prov["source"] == "x"

    def test_symbols_and_range(self) -> None:
        b = MarketDataBundle(quotes=_quote_rows(), source="x")
        assert set(b.symbols) == {"000001", "000002"}
        start, end = b.date_range
        assert start == date(2020, 1, 2)
        assert end == date(2020, 1, 3)
        assert b.covers(date(2020, 1, 3)) is True


class TestDataSourceHelpers:
    def test_check_columns_raises(self) -> None:
        df = pd.DataFrame({"symbol": ["000001"]})
        with pytest.raises(DataUnavailableError):
            require_quote_columns(df, "unit")

    def test_normalize_and_slice(self) -> None:
        df = _quote_rows()
        out = normalize_quotes(df)
        assert "is_suspended" in out.columns
        # 切片
        from ashare_quant.automation.datasource import _slice
        sub = _slice(out, symbols=["000001"], start=date(2020, 1, 2), end=date(2020, 1, 2))
        assert set(sub["symbol"]) == {"000001"}
        assert len(sub) == 1

    def test_lookback_start(self) -> None:
        assert lookback_start(date(2020, 1, 11), 10) == date(2020, 1, 1)
        assert lookback_start(date(2020, 1, 5), 0) == date(2020, 1, 4)


class TestLocalParquetDataSource:
    def test_load_ok(self, tmp_path) -> None:
        (tmp_path / "curated").mkdir()
        _quote_rows().to_parquet(tmp_path / "curated" / "quotes.parquet", index=False)
        ds = LocalParquetDataSource(tmp_path)
        b = ds.load(
            symbols=["000001", "000002"],
            start=date(2020, 1, 2), end=date(2020, 1, 3), as_of=date(2020, 1, 3),
        )
        assert b.online is False
        assert b.source == "local-parquet"
        assert not b.synthetic
        assert "离线本地数据" in b.notes[0]

    def test_missing_dir(self, tmp_path) -> None:
        ds = LocalParquetDataSource(tmp_path / "nope")
        with pytest.raises(DataUnavailableError):
            ds.load(symbols=[], start=date(2020, 1, 2), end=date(2020, 1, 3),
                    as_of=date(2020, 1, 3))

    def test_empty_curated(self, tmp_path) -> None:
        (tmp_path / "curated").mkdir()
        with pytest.raises(DataUnavailableError):
            LocalParquetDataSource(tmp_path).load(
                symbols=[], start=date(2020, 1, 2), end=date(2020, 1, 3),
                as_of=date(2020, 1, 3))

    def test_missing_columns(self, tmp_path) -> None:
        (tmp_path / "curated").mkdir()
        pd.DataFrame({"symbol": ["000001"]}).to_parquet(
            tmp_path / "curated" / "q.parquet", index=False)
        with pytest.raises(DataUnavailableError):
            LocalParquetDataSource(tmp_path).load(
                symbols=[], start=date(2020, 1, 2), end=date(2020, 1, 3),
                as_of=date(2020, 1, 3))

    def test_stale(self, tmp_path) -> None:
        (tmp_path / "curated").mkdir()
        _quote_rows().to_parquet(tmp_path / "curated" / "quotes.parquet", index=False)
        ds = LocalParquetDataSource(tmp_path, max_staleness_days=1)
        with pytest.raises(DataUnavailableError):
            ds.load(symbols=["000001"], start=date(2020, 1, 2), end=date(2020, 1, 3),
                    as_of=date(2020, 2, 1))

    def test_master_and_benchmark_globs(self, tmp_path) -> None:
        (tmp_path / "curated").mkdir()
        _quote_rows().to_parquet(tmp_path / "curated" / "quotes.parquet", index=False)
        # master / benchmark 文件缺失 -> 不应致命
        ds = LocalParquetDataSource(
            tmp_path,
            security_master_glob="curated/*master*.parquet",
            benchmark_glob="curated/*bench*.parquet",
        )
        b = ds.load(symbols=["000001"], start=date(2020, 1, 2), end=date(2020, 1, 3),
                    as_of=date(2020, 1, 3))
        assert b.security_master is None
        assert b.benchmark is None


class TestInjectedDataSource:
    def test_synthetic_notes(self) -> None:
        ds = InjectedDataSource(_quote_rows())
        b = ds.load(symbols=["000001"], start=date(2020, 1, 2), end=date(2020, 1, 3),
                    as_of=date(2020, 1, 3))
        assert b.synthetic is True
        assert b.online is False
        assert "合成样本" in b.notes[0]

    def test_non_synthetic_notes(self) -> None:
        ds = InjectedDataSource(_quote_rows(), synthetic=False, name="inj")
        b = ds.load(symbols=["000001"], start=date(2020, 1, 2), end=date(2020, 1, 3),
                    as_of=date(2020, 1, 3))
        assert "注入式数据源" in b.notes[0]

    def test_empty_slice_raises(self) -> None:
        ds = InjectedDataSource(_quote_rows())
        with pytest.raises(DataUnavailableError):
            ds.load(symbols=["000001"], start=date(1999, 1, 1), end=date(1999, 1, 2),
                    as_of=date(1999, 1, 1))

    def test_coverage_enforce_off(self) -> None:
        ds = InjectedDataSource(_quote_rows(), enforce_coverage=False)
        # 业务日超出最新行情但关闭覆盖检查 -> 仍返回
        b = ds.load(symbols=["000001"], start=date(2020, 1, 2), end=date(2020, 1, 3),
                    as_of=date(2021, 1, 1))
        assert b is not None


class TestUnavailableAndFactory:
    def test_unavailable_raises(self) -> None:
        ds = UnavailableDataSource(reason="x")
        with pytest.raises(DataUnavailableError):
            ds.load(symbols=[], start=date(2020, 1, 2), end=date(2020, 1, 3),
                    as_of=date(2020, 1, 3))

    def test_factory(self, tmp_path) -> None:
        ds = build_default_data_source(tmp_path)
        assert isinstance(ds, LocalParquetDataSource)
        assert ds.max_staleness_days is None
