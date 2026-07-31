"""Phase 3 基准比较模块测试。

测试 ``ashare_quant.research.benchmarks`` 模块，覆盖：

1. 基准日期对齐（禁止前向/后向填充未来值）
2. 现金基准收益恒为 0.0
3. 缺失基准抛出 :class:`BenchmarkMissingError`
4. ``load_benchmarks`` 从 Parquet 文件加载
5. ``compute_benchmark_returns`` 首尾收盘价计算、缺失日期不填充
6. 基准含 NaN 值时被跳过
7. ``start_date > end_date`` 抛出 :class:`ValueError`
8. ``compute_cash_benchmark`` 未投资天数统计

所有基准数据为合成数据，非真实行情。
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from ashare_quant.research.benchmarks import (
    BenchmarkData,
    BenchmarkMissingError,
    compute_benchmark_returns,
    compute_cash_benchmark,
    load_benchmarks,
)
from tests.research_samples import (
    make_benchmark_data,
    make_benchmark_dict,
    make_trade_dates,
)


# ------------------------------------------------------------------ #
# 固定价格的小型基准（用于精确验证首尾收盘价选取逻辑）
# ------------------------------------------------------------------ #
# 交易日历：2020-01-02(周四) ~ 2020-01-08(周三)，跳过周末
_MANUAL_DATES: list[date] = [
    date(2020, 1, 2),   # Thu
    date(2020, 1, 3),   # Fri
    date(2020, 1, 6),   # Mon  （1/4、1/5 为周末缺口）
    date(2020, 1, 7),   # Tue
    date(2020, 1, 8),   # Wed
]
_MANUAL_HS300: dict[date, float] = {
    date(2020, 1, 2): 100.0,
    date(2020, 1, 3): 110.0,
    date(2020, 1, 6): 120.0,
    date(2020, 1, 7): 130.0,
    date(2020, 1, 8): 140.0,
}
_MANUAL_CSI_ALL: dict[date, float] = {
    date(2020, 1, 2): 200.0,
    date(2020, 1, 3): 210.0,
    date(2020, 1, 6): 220.0,
    date(2020, 1, 7): 230.0,
    date(2020, 1, 8): 240.0,
}


@pytest.fixture
def manual_benchmark() -> BenchmarkData:
    """构造一个价格已知的小型基准，用于精确验证首尾收盘价选取。"""
    return BenchmarkData(
        trade_dates=list(_MANUAL_DATES),
        hs300_close=dict(_MANUAL_HS300),
        csi_all_close=dict(_MANUAL_CSI_ALL),
    )


# ================================================================== #
# BenchmarkData 属性
# ================================================================== #


def test_benchmark_data_default_is_empty():
    """空 BenchmarkData 的 has_hs300 / has_csi_all 均为 False。"""
    bd = BenchmarkData()
    assert bd.has_hs300 is False
    assert bd.has_csi_all is False
    assert bd.trade_dates == []
    assert bd.hs300_close == {}
    assert bd.csi_all_close == {}


def test_benchmark_data_properties_reflect_content():
    """持有有效序列时 has_hs300 / has_csi_all 为 True。"""
    d = date(2020, 1, 2)
    bd = BenchmarkData(
        trade_dates=[d],
        hs300_close={d: 100.0},
        csi_all_close={d: 200.0},
    )
    assert bd.has_hs300 is True
    assert bd.has_csi_all is True


def test_benchmark_data_partial_series():
    """仅持有沪深300、缺失中证全指时属性正确反映。"""
    d = date(2020, 1, 2)
    bd = BenchmarkData(trade_dates=[d], hs300_close={d: 100.0}, csi_all_close={})
    assert bd.has_hs300 is True
    assert bd.has_csi_all is False


# ================================================================== #
# 1 & 5. 基准日期对齐：首尾收盘价计算，禁止前向/后向填充
# ================================================================== #


@pytest.mark.parametrize(
    "start_date,end_date,expected_hs300,expected_csi_all",
    [
        # 完整区间：首日=1/2(100/200)，末日=1/8(140/240)
        (date(2020, 1, 1), date(2020, 1, 10), 140 / 100 - 1, 240 / 200 - 1),
        # start 落在缺口(1/4 周六)：首个可用收盘=1/6(120/220)，
        # 禁止后向填充到 1/3(110/210)。
        (date(2020, 1, 4), date(2020, 1, 8), 140 / 120 - 1, 240 / 220 - 1),
        # end 落在缺口(1/5 周日)：末个可用收盘=1/3(110/210)，
        # 禁止前向填充到 1/6(120/220)。
        (date(2020, 1, 2), date(2020, 1, 5), 110 / 100 - 1, 210 / 200 - 1),
        # start==end==交易日(1/6)：首尾相同，收益为 0
        (date(2020, 1, 6), date(2020, 1, 6), 0.0, 0.0),
        # 精确起止交易日 1/3 ~ 1/7
        (date(2020, 1, 3), date(2020, 1, 7), 130 / 110 - 1, 230 / 210 - 1),
        # start 早于所有交易日、end 晚于所有交易日 -> 取首尾
        (date(2019, 12, 1), date(2020, 2, 1), 140 / 100 - 1, 240 / 200 - 1),
    ],
    ids=[
        "full-range",
        "no-backward-fill-start-in-gap",
        "no-forward-fill-end-in-gap",
        "single-trade-day",
        "exact-trade-day-bounds",
        "bounds-beyond-calendar",
    ],
)
def test_benchmark_returns_alignment(
    manual_benchmark, start_date, end_date, expected_hs300, expected_csi_all
):
    """验证首尾收盘价选取：取首个 >=start_date 与末个 <=end_date 的可用收盘。"""
    result = compute_benchmark_returns(manual_benchmark, start_date, end_date)

    assert set(result.keys()) == {"hs300", "csi_all", "cash"}
    assert result["hs300"] == pytest.approx(expected_hs300)
    assert result["csi_all"] == pytest.approx(expected_csi_all)
    assert result["cash"] == 0.0


@pytest.mark.parametrize(
    "start_date,end_date",
    [
        # 窗口完全在首个交易日之前 -> 无可用收盘
        (date(2019, 12, 30), date(2020, 1, 1)),
        # 窗口完全在末个交易日之后 -> 无可用收盘
        (date(2020, 1, 9), date(2020, 1, 10)),
        # start==end 落在缺口内(1/4 周六) -> 不填充到相邻交易日，应抛错
        (date(2020, 1, 4), date(2020, 1, 4)),
        # start==end 落在缺口内(1/5 周日) -> 不填充到相邻交易日，应抛错
        (date(2020, 1, 5), date(2020, 1, 5)),
    ],
    ids=[
        "window-before-first",
        "window-after-last",
        "single-non-trade-day-sat",
        "single-non-trade-day-sun",
    ],
)
def test_benchmark_returns_no_fill_raises(manual_benchmark, start_date, end_date):
    """窗口内无可用收盘价时必须抛 BenchmarkMissingError，禁止填充相邻日期。"""
    with pytest.raises(BenchmarkMissingError):
        compute_benchmark_returns(manual_benchmark, start_date, end_date)


# ================================================================== #
# 2. 现金基准收益恒为 0.0
# ================================================================== #


def test_cash_benchmark_return_always_zero(manual_benchmark):
    """无论窗口如何，compute_benchmark_returns 的 cash 键恒为 0.0。"""
    windows = [
        (date(2020, 1, 2), date(2020, 1, 8)),
        (date(2020, 1, 6), date(2020, 1, 6)),
        (date(2020, 1, 1), date(2020, 1, 10)),
        (date(2020, 1, 3), date(2020, 1, 7)),
    ]
    for start_date, end_date in windows:
        result = compute_benchmark_returns(manual_benchmark, start_date, end_date)
        assert result["cash"] == 0.0
        assert set(result.keys()) == {"hs300", "csi_all", "cash"}


# ================================================================== #
# 7. start_date > end_date 抛出 ValueError
# ================================================================== #


def test_start_date_after_end_date_raises_valueerror(manual_benchmark):
    """start_date 晚于 end_date 时抛 ValueError（优先于基准缺失判断）。"""
    with pytest.raises(ValueError):
        compute_benchmark_returns(
            manual_benchmark, date(2020, 1, 8), date(2020, 1, 2)
        )


def test_start_date_after_end_date_raises_valueerror_even_with_empty_window():
    """即便数据为空，start_date > end_date 仍应抛 ValueError。"""
    empty = BenchmarkData()
    with pytest.raises(ValueError):
        compute_benchmark_returns(empty, date(2020, 1, 10), date(2020, 1, 2))


# ================================================================== #
# 4. load_benchmarks 从 Parquet 文件加载
# ================================================================== #


def test_load_benchmarks_from_parquet(tmp_path):
    """从 Parquet 加载基准数据，字段与价格正确还原。"""
    n_days = 50
    start = date(2020, 1, 2)
    df = make_benchmark_data(
        start=start, n_days=n_days, hs300_return=0.001, csi_all_return=0.0008
    )
    parquet_path = tmp_path / "benchmarks.parquet"
    df.to_parquet(parquet_path)

    benchmark = load_benchmarks(str(parquet_path))

    dates = make_trade_dates(start, n_days)
    assert benchmark.has_hs300 is True
    assert benchmark.has_csi_all is True
    # trade_dates 为去重升序的全部交易日
    assert benchmark.trade_dates == sorted(set(dates))
    # 每个交易日都应有有效收盘价
    for d in dates:
        assert d in benchmark.hs300_close
        assert d in benchmark.csi_all_close
    # 首日价格：沪深300 = 3000 * 1.001，中证全指 = 5000 * 1.0008
    assert benchmark.hs300_close[dates[0]] == pytest.approx(3003.0)
    assert benchmark.csi_all_close[dates[0]] == pytest.approx(5004.0)


def test_load_benchmarks_nonexistent_file_raises(tmp_path):
    """文件不存在时抛 FileNotFoundError。"""
    with pytest.raises(FileNotFoundError):
        load_benchmarks(str(tmp_path / "no_such_file.parquet"))


def test_load_benchmarks_missing_column_raises(tmp_path):
    """Parquet 缺少必需列时抛 BenchmarkMissingError。"""
    df = make_benchmark_data(start=date(2020, 1, 2), n_days=10).drop(
        columns=["csi_all_close"]
    )
    parquet_path = tmp_path / "missing_col.parquet"
    df.to_parquet(parquet_path)

    with pytest.raises(BenchmarkMissingError):
        load_benchmarks(str(parquet_path))


# ================================================================== #
# 3. 缺失基准抛出 BenchmarkMissingError
# ================================================================== #


def test_load_benchmarks_all_nan_hs300_raises(tmp_path):
    """沪深300 全为 NaN 时抛 BenchmarkMissingError。"""
    df = make_benchmark_data(start=date(2020, 1, 2), n_days=10)
    df["hs300_close"] = float("nan")
    parquet_path = tmp_path / "all_nan_hs300.parquet"
    df.to_parquet(parquet_path)

    with pytest.raises(BenchmarkMissingError):
        load_benchmarks(str(parquet_path))


def test_load_benchmarks_all_nan_csi_all_raises(tmp_path):
    """中证全指全为 NaN 时抛 BenchmarkMissingError，不得用其他基准替代。"""
    df = make_benchmark_data(start=date(2020, 1, 2), n_days=10)
    df["csi_all_close"] = float("nan")
    parquet_path = tmp_path / "all_nan_csi.parquet"
    df.to_parquet(parquet_path)

    with pytest.raises(BenchmarkMissingError):
        load_benchmarks(str(parquet_path))


def test_load_benchmarks_empty_columns_raise(tmp_path):
    """两列必需基准均为空时抛 BenchmarkMissingError。"""
    df = pd.DataFrame(
        {
            "trade_date": make_trade_dates(date(2020, 1, 2), 5),
            "hs300_close": [float("nan")] * 5,
            "csi_all_close": [float("nan")] * 5,
        }
    )
    parquet_path = tmp_path / "empty.parquet"
    df.to_parquet(parquet_path)

    with pytest.raises(BenchmarkMissingError):
        load_benchmarks(str(parquet_path))


def test_compute_returns_window_without_available_close_raises(manual_benchmark):
    """测试期内无可用收盘价时，compute_benchmark_returns 抛 BenchmarkMissingError。"""
    with pytest.raises(BenchmarkMissingError):
        compute_benchmark_returns(
            manual_benchmark, date(2020, 1, 9), date(2020, 1, 10)
        )


# ================================================================== #
# 6. 基准含 NaN 值时被跳过（不进入序列、不参与首尾计算）
# ================================================================== #


def test_load_benchmarks_skips_nan_values(tmp_path):
    """NaN 收盘价不进入 hs300_close / csi_all_close，但仍保留在 trade_dates。"""
    start = date(2020, 1, 2)
    dates = make_trade_dates(start, 10)
    missing = {dates[0], dates[5]}  # 首日与中段各缺一天
    df = make_benchmark_data(start=start, n_days=10, missing_dates=missing)
    parquet_path = tmp_path / "with_nan.parquet"
    df.to_parquet(parquet_path)

    benchmark = load_benchmarks(str(parquet_path))

    # 缺失日期不在收盘价序列中
    assert dates[0] not in benchmark.hs300_close
    assert dates[0] not in benchmark.csi_all_close
    assert dates[5] not in benchmark.hs300_close
    assert dates[5] not in benchmark.csi_all_close
    # 缺失日期仍保留在交易日历（并集）
    assert dates[0] in benchmark.trade_dates
    assert dates[5] in benchmark.trade_dates
    # 其余日期均有有效收盘价
    for d in dates:
        if d not in missing:
            assert d in benchmark.hs300_close
            assert d in benchmark.csi_all_close
    assert benchmark.has_hs300 is True
    assert benchmark.has_csi_all is True


def test_compute_returns_skips_nan_at_boundary(tmp_path):
    """窗口起点恰好为 NaN 缺失日时，首个可用收盘为下一个有效交易日。"""
    start = date(2020, 1, 2)
    dates = make_trade_dates(start, 10)
    missing = {dates[0]}  # 首日缺失
    df = make_benchmark_data(start=start, n_days=10, missing_dates=missing)
    parquet_path = tmp_path / "nan_first.parquet"
    df.to_parquet(parquet_path)

    benchmark = load_benchmarks(str(parquet_path))

    # 窗口仅含缺失日 -> 无可用收盘 -> 抛错（禁止填充）
    with pytest.raises(BenchmarkMissingError):
        compute_benchmark_returns(benchmark, dates[0], dates[0])

    # 窗口 [dates[0], dates[1]]：首个可用=dates[1]，末个可用=dates[1] -> 收益 0
    result = compute_benchmark_returns(benchmark, dates[0], dates[1])
    assert result["hs300"] == pytest.approx(0.0)
    assert result["csi_all"] == pytest.approx(0.0)

    # 完整窗口：首个可用=dates[1]，末个可用=dates[-1]
    full = compute_benchmark_returns(benchmark, dates[0], dates[-1])
    expected_hs300 = (
        benchmark.hs300_close[dates[-1]] / benchmark.hs300_close[dates[1]] - 1.0
    )
    expected_csi = (
        benchmark.csi_all_close[dates[-1]] / benchmark.csi_all_close[dates[1]] - 1.0
    )
    assert full["hs300"] == pytest.approx(expected_hs300)
    assert full["csi_all"] == pytest.approx(expected_csi)


# ================================================================== #
# 端到端：基于 make_benchmark_dict 验证收益计算
# ================================================================== #


def test_compute_returns_full_window_matches_formula():
    """完整窗口的总收益等于 (1+r)^(n-1) - 1（首日到末日）。"""
    n_days = 200
    start = date(2020, 1, 2)
    benchmark = make_benchmark_dict(
        start=start, n_days=n_days, hs300_return=0.001, csi_all_return=0.0008
    )
    dates = make_trade_dates(start, n_days)

    result = compute_benchmark_returns(benchmark, dates[0], dates[-1])

    # 首日 close = 3000*(1.001)^1，末日 close = 3000*(1.001)^200
    # 收益 = (1.001)^(200-1) - 1
    assert result["hs300"] == pytest.approx((1.001 ** 199) - 1, rel=1e-4)
    assert result["csi_all"] == pytest.approx((1.0008 ** 199) - 1, rel=1e-4)
    assert result["cash"] == 0.0


def test_compute_returns_alignment_on_generated_data():
    """start_date 落在缺口时，首个可用收盘为下一个交易日，收益对应缩短。"""
    n_days = 200
    start = date(2020, 1, 2)
    benchmark = make_benchmark_dict(
        start=start, n_days=n_days, hs300_return=0.001, csi_all_return=0.0008
    )
    dates = make_trade_dates(start, n_days)
    # dates[0]=1/2(周四), dates[1]=1/3(周五), dates[2]=1/6(周一)
    # start_date=1/4(周六) 落在缺口 -> 首个可用收盘 = dates[2]
    start_date = date(2020, 1, 4)
    end_date = dates[-1]

    result = compute_benchmark_returns(benchmark, start_date, end_date)

    # 从 dates[2] 到 dates[-1]：收益 = (1.001)^(200-3) - 1 = (1.001)^197 - 1
    assert result["hs300"] == pytest.approx((1.001 ** 197) - 1, rel=1e-4)
    assert result["csi_all"] == pytest.approx((1.0008 ** 197) - 1, rel=1e-4)


def test_compute_returns_beyond_calendar_uses_first_and_last():
    """start/end 超出日历范围时，取首尾交易日收盘。"""
    n_days = 60
    start = date(2020, 1, 2)
    benchmark = make_benchmark_dict(
        start=start, n_days=n_days, hs300_return=0.002, csi_all_return=0.001
    )
    dates = make_trade_dates(start, n_days)

    result = compute_benchmark_returns(
        benchmark, date(2019, 12, 1), date(2020, 12, 31)
    )
    expected_hs300 = (
        benchmark.hs300_close[dates[-1]] / benchmark.hs300_close[dates[0]] - 1.0
    )
    expected_csi = (
        benchmark.csi_all_close[dates[-1]] / benchmark.csi_all_close[dates[0]] - 1.0
    )
    assert result["hs300"] == pytest.approx(expected_hs300)
    assert result["csi_all"] == pytest.approx(expected_csi)


# ================================================================== #
# 8. compute_cash_benchmark：未投资天数统计
# ================================================================== #


def test_cash_benchmark_return_is_zero():
    """现金基准总收益固定为 0.0。"""
    snaps = [{"position_value": 0.0}, {"position_value": 100.0}]
    result = compute_cash_benchmark(snaps, trading_days=2)
    assert result["total_return"] == 0.0


def test_cash_benchmark_counts_uninvested_and_invested_days():
    """持仓市值为 0 的交易日计为未投资，>0 计为已投资。"""
    snaps = [
        {"position_value": 0.0},     # 未投资
        {"position_value": 100.0},   # 已投资
        {"position_value": 0.0},     # 未投资
        {"position_value": 50.0},    # 已投资
    ]
    result = compute_cash_benchmark(snaps, trading_days=4)

    assert result["total_return"] == 0.0
    assert result["trading_days"] == 4
    assert result["uninvested_days"] == 2
    assert result["invested_days"] == 2
    assert result["cash_ratio"] == pytest.approx(0.5)
    # invested_days + uninvested_days == total
    assert result["invested_days"] + result["uninvested_days"] == len(snaps)


def test_cash_benchmark_all_uninvested():
    """全部为现金（持仓为 0）时 cash_ratio 为 1.0。"""
    snaps = [{"position_value": 0.0}] * 5
    result = compute_cash_benchmark(snaps, trading_days=5)
    assert result["uninvested_days"] == 5
    assert result["invested_days"] == 0
    assert result["cash_ratio"] == pytest.approx(1.0)
    assert result["total_return"] == 0.0


def test_cash_benchmark_all_invested():
    """全程持仓时 cash_ratio 为 0.0。"""
    snaps = [{"position_value": 100.0}] * 3
    result = compute_cash_benchmark(snaps, trading_days=3)
    assert result["uninvested_days"] == 0
    assert result["invested_days"] == 3
    assert result["cash_ratio"] == pytest.approx(0.0)


def test_cash_benchmark_empty_equity():
    """空权益序列时现金占比为 0.0，且不除零。"""
    result = compute_cash_benchmark([], trading_days=0)
    assert result["total_return"] == 0.0
    assert result["uninvested_days"] == 0
    assert result["invested_days"] == 0
    assert result["cash_ratio"] == 0.0
    assert result["trading_days"] == 0


def test_cash_benchmark_object_snapshots():
    """兼容带 position_value 属性的快照对象。"""
    class _Snap:
        def __init__(self, pv: float) -> None:
            self.position_value = pv

    snaps = [_Snap(0.0), _Snap(200.0), _Snap(0.0)]
    result = compute_cash_benchmark(snaps, trading_days=3)
    assert result["uninvested_days"] == 2
    assert result["invested_days"] == 1
    assert result["cash_ratio"] == pytest.approx(2 / 3)


def test_cash_benchmark_none_and_negative_position_is_uninvested():
    """持仓市值为 None 或非正（含负数）均计为未投资。"""
    snaps = [
        {"position_value": None},     # None -> 未投资
        {"position_value": -10.0},    # 负数 -> 未投资
        {"position_value": 100.0},    # 已投资
    ]
    result = compute_cash_benchmark(snaps, trading_days=3)
    assert result["uninvested_days"] == 2
    assert result["invested_days"] == 1
    assert result["cash_ratio"] == pytest.approx(2 / 3)


def test_cash_benchmark_snapshot_without_position_value():
    """快照不含 position_value 时计为未投资。"""
    snaps = [
        {"other_field": 1},           # 无持仓市值信息 -> 未投资
        {"position_value": 100.0},    # 已投资
    ]
    result = compute_cash_benchmark(snaps, trading_days=2)
    assert result["uninvested_days"] == 1
    assert result["invested_days"] == 1
    assert result["cash_ratio"] == pytest.approx(0.5)


def test_cash_benchmark_result_keys():
    """返回字典包含全部必需字段。"""
    result = compute_cash_benchmark(
        [{"position_value": 0.0}, {"position_value": 1.0}], trading_days=2
    )
    assert set(result.keys()) == {
        "total_return",
        "trading_days",
        "uninvested_days",
        "invested_days",
        "cash_ratio",
    }
