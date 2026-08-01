"""Phase 3 研究模块测试数据构建器。

提供确定性合成行情、历史状态表、基准数据，覆盖以下场景：
- 牛市、熊市、高波动
- 停牌、涨跌停
- ST、退市、上市不足 120 日
- 无法购买一手（价格过高）
- 基准缺失日期

所有数据为合成数据，非真实行情。
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Optional

import numpy as np
import pandas as pd

from .backtest_samples import make_trade_dates


# ---------------------------------------------------------------------- #
# 交易日历
# ---------------------------------------------------------------------- #


def make_trade_dates_range(start: date, end: date) -> list[date]:
    """生成 [start, end] 内的交易日（排除周末）。"""
    dates: list[date] = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            dates.append(cur)
        cur += timedelta(days=1)
    return dates


# ---------------------------------------------------------------------- #
# 单股票行情生成
# ---------------------------------------------------------------------- #


def _make_bar(
    symbol: str,
    dt: date,
    open_price: float,
    high: float,
    low: float,
    close: float,
    volume: float,
    amount: Optional[float] = None,
    qfq_ratio: float = 1.0,
    is_suspended: bool = False,
    is_tradable: bool = True,
    adjustment_factor: float = 1.0,
) -> dict:
    """构造一行 curated 日行情数据。"""
    amt = amount if amount is not None else volume * close
    return {
        "symbol": symbol,
        "trade_date": dt,
        "open_raw": open_price,
        "high_raw": high,
        "low_raw": low,
        "close_raw": close,
        "volume": volume,
        "amount": amt,
        "open_qfq": open_price * qfq_ratio,
        "high_qfq": high * qfq_ratio,
        "low_qfq": low * qfq_ratio,
        "close_qfq": close * qfq_ratio,
        "adjustment_factor": adjustment_factor,
        "is_suspended": is_suspended,
        "is_tradable": is_tradable,
        "source": "test",
        "fetched_at": pd.Timestamp("2024-01-01 09:00:00"),
    }


def make_stock_quotes(
    symbol: str,
    dates: list[date],
    base_price: float = 5.0,
    daily_return: float = 0.001,
    volume: float = 200000,
    qfq_ratio: float = 1.0,
    suspended_days: Optional[set[date]] = None,
    limit_up_days: Optional[set[date]] = None,
    limit_down_days: Optional[set[date]] = None,
    price_override: Optional[dict[date, float]] = None,
    volume_override: Optional[dict[date, float]] = None,
    start_offset: int = 0,
    is_tradable: bool = True,
) -> pd.DataFrame:
    """生成单只股票的日行情数据。

    Args:
        symbol: 股票代码。
        dates: 交易日列表。
        base_price: 基础价格。
        daily_return: 每日收益率。
        volume: 基础成交量。
        qfq_ratio: 前复权比例。
        suspended_days: 停牌日期集合。
        limit_up_days: 涨停日期集合。
        limit_down_days: 跌停日期集合。
        price_override: 特定日期价格覆盖 {date: close_price}。
        volume_override: 特定日期成交量覆盖 {date: volume}。
        start_offset: 起始偏移（跳过前 N 个交易日，模拟上市较晚）。
        is_tradable: 是否可交易。
    """
    suspended = suspended_days or set()
    limit_up = limit_up_days or set()
    limit_down = limit_down_days or set()
    overrides = price_override or {}
    vol_overrides = volume_override or {}

    rows: list[dict] = []
    price = base_price

    for i, dt in enumerate(dates):
        if i < start_offset:
            continue

        is_susp = dt in suspended
        vol = vol_overrides.get(dt, volume)

        if i > start_offset:
            if dt in overrides:
                price = overrides[dt]
            elif dt in limit_up:
                price = round(price * 1.10, 2)
            elif dt in limit_down:
                price = round(price * 0.90, 2)
            else:
                price *= (1 + daily_return)

        if is_susp:
            if rows:
                prev_close = rows[-1]["close_raw"]
                row = _make_bar(
                    symbol=symbol, dt=dt,
                    open_price=prev_close, high=prev_close,
                    low=prev_close, close=prev_close,
                    volume=0, amount=0, qfq_ratio=qfq_ratio,
                    is_suspended=True, is_tradable=False,
                )
            else:
                row = _make_bar(
                    symbol=symbol, dt=dt,
                    open_price=price, high=price,
                    low=price, close=price,
                    volume=0, amount=0, qfq_ratio=qfq_ratio,
                    is_suspended=True, is_tradable=False,
                )
        else:
            high = price * 1.01
            low = price * 0.99
            row = _make_bar(
                symbol=symbol, dt=dt,
                open_price=price * 0.998,
                high=high, low=low, close=price,
                volume=vol, qfq_ratio=qfq_ratio,
                is_suspended=False, is_tradable=is_tradable,
            )

        rows.append(row)

    return pd.DataFrame(rows)


# ---------------------------------------------------------------------- #
# 多股票合成行情
# ---------------------------------------------------------------------- #


def make_research_quotes(
    start: date = date(2020, 1, 2),
    n_days: int = 200,
    n_stocks: int = 8,
) -> pd.DataFrame:
    """生成多股票合成行情，覆盖牛市、熊市、高波动等场景。

    股票设计：
    - 000001: 稳定上涨（牛市）
    - 000002: 稳定下跌（熊市）
    - 000003: 高波动
    - 000004: 正常波动，中途停牌
    - 000005: 价格过高（>10元，1000元买不了一手）
    - 000006: 上市较晚（start_offset > 0）
    - 000007: 中途涨停
    - 000008: 正常股票（基准对照）

    Args:
        start: 起始日期。
        n_days: 交易日天数。
        n_stocks: 股票数量（最多 8）。
    """
    dates = make_trade_dates(start, n_days)
    dfs: list[pd.DataFrame] = []

    # 000001: 稳定上涨（牛市）
    dfs.append(make_stock_quotes(
        "000001", dates, base_price=5.0, daily_return=0.003, volume=300000,
    ))

    if n_stocks >= 2:
        # 000002: 稳定下跌（熊市）
        dfs.append(make_stock_quotes(
            "000002", dates, base_price=5.0, daily_return=-0.002, volume=300000,
        ))

    if n_stocks >= 3:
        # 000003: 高波动
        np.random.seed(42)
        high_vol_returns = np.random.choice(
            [-0.04, -0.02, 0.02, 0.04], size=n_days, p=[0.2, 0.3, 0.3, 0.2],
        )
        prices = [5.0]
        for r in high_vol_returns[1:]:
            prices.append(prices[-1] * (1 + r))
        price_override = {dates[i]: prices[i] for i in range(n_days)}
        dfs.append(make_stock_quotes(
            "000003", dates, base_price=5.0, daily_return=0.0, volume=300000,
            price_override=price_override,
        ))

    if n_stocks >= 4:
        # 000004: 正常波动，中途停牌 2 天
        mid = n_days // 2
        suspended = {dates[mid], dates[mid + 1]}
        dfs.append(make_stock_quotes(
            "000004", dates, base_price=5.0, daily_return=0.001, volume=300000,
            suspended_days=suspended,
        ))

    if n_stocks >= 5:
        # 000005: 价格过高（>10元，1000元买不了一手）
        dfs.append(make_stock_quotes(
            "000005", dates, base_price=15.0, daily_return=0.001, volume=300000,
        ))

    if n_stocks >= 6:
        # 000006: 上市较晚（跳过前 30 个交易日）
        dfs.append(make_stock_quotes(
            "000006", dates, base_price=5.0, daily_return=0.002, volume=300000,
            start_offset=30,
        ))

    if n_stocks >= 7:
        # 000007: 中途涨停
        limit_up_day = {dates[n_days // 3]}
        dfs.append(make_stock_quotes(
            "000007", dates, base_price=5.0, daily_return=0.001, volume=300000,
            limit_up_days=limit_up_day,
        ))

    if n_stocks >= 8:
        # 000008: 正常股票
        dfs.append(make_stock_quotes(
            "000008", dates, base_price=5.0, daily_return=0.002, volume=300000,
        ))

    return pd.concat(dfs, ignore_index=True).sort_values(
        ["trade_date", "symbol"]
    ).reset_index(drop=True)


# ---------------------------------------------------------------------- #
# 历史状态表
# ---------------------------------------------------------------------- #


def make_historical_status_table(
    start: date = date(2020, 1, 2),
    n_stocks: int = 8,
) -> pd.DataFrame:
    """生成历史状态表（有效区间 schema）。

    股票状态设计：
    - 000001 ~ 000005: 上市日期为 2019-01-02，正常状态
    - 000006: 上市日期较晚（模拟上市不足 120 日场景）
    - 000007: 中途变为 ST
    - 000008: 正常状态

    Args:
        start: 数据起始日期。
        n_stocks: 股票数量。
    """
    records: list[dict] = []

    # 正常股票（上市日期较早）
    for i in range(1, min(n_stocks, 5) + 1):
        sym = f"{i:06d}"
        records.append({
            "symbol": sym,
            "list_date": date(2019, 1, 2),
            "delist_date": None,
            "st_status": "normal",
            "status_valid_from": date(2019, 1, 2),
            "status_valid_to": None,
        })

    # 000006: 上市较晚
    if n_stocks >= 6:
        # 上市日期设为 start + 30 个交易日之后
        late_list = start + timedelta(days=45)
        records.append({
            "symbol": "000006",
            "list_date": late_list,
            "delist_date": None,
            "st_status": "normal",
            "status_valid_from": late_list,
            "status_valid_to": None,
        })

    # 000007: 中途变为 ST
    if n_stocks >= 7:
        # 初始正常
        records.append({
            "symbol": "000007",
            "list_date": date(2019, 1, 2),
            "delist_date": None,
            "st_status": "normal",
            "status_valid_from": date(2019, 1, 2),
            "status_valid_to": start + timedelta(days=90),
        })
        # 之后变为 ST
        records.append({
            "symbol": "000007",
            "list_date": date(2019, 1, 2),
            "delist_date": None,
            "st_status": "st",
            "status_valid_from": start + timedelta(days=90),
            "status_valid_to": None,
        })

    # 000008: 正常
    if n_stocks >= 8:
        records.append({
            "symbol": "000008",
            "list_date": date(2019, 1, 2),
            "delist_date": None,
            "st_status": "normal",
            "status_valid_from": date(2019, 1, 2),
            "status_valid_to": None,
        })

    return pd.DataFrame(records)


def make_status_table_with_delisting(
    start: date = date(2020, 1, 2),
) -> pd.DataFrame:
    """生成包含退市股票的状态表。"""
    df = make_historical_status_table(start=start, n_stocks=8)
    # 添加一只退市股票
    delist_date = start + timedelta(days=60)
    new_row = pd.DataFrame([{
        "symbol": "000009",
        "list_date": date(2019, 1, 2),
        "delist_date": delist_date,
        "st_status": "delisting",
        "status_valid_from": delist_date - timedelta(days=30),
        "status_valid_to": delist_date,
    }])
    # 退市前正常
    pre_row = pd.DataFrame([{
        "symbol": "000009",
        "list_date": date(2019, 1, 2),
        "delist_date": delist_date,
        "st_status": "normal",
        "status_valid_from": date(2019, 1, 2),
        "status_valid_to": delist_date - timedelta(days=30),
    }])
    return pd.concat([df, pre_row, new_row], ignore_index=True)


# ---------------------------------------------------------------------- #
# 基准数据
# ---------------------------------------------------------------------- #


def make_benchmark_data(
    start: date = date(2020, 1, 2),
    n_days: int = 200,
    missing_dates: Optional[set[date]] = None,
    hs300_return: float = 0.001,
    csi_all_return: float = 0.0008,
) -> pd.DataFrame:
    """生成基准数据 Parquet 格式的 DataFrame。

    包含沪深300和中证全指收盘价，可选缺失日期。

    Args:
        start: 起始日期。
        n_days: 交易日天数。
        missing_dates: 基准缺失日期集合（模拟基准数据不完整）。
        hs300_return: 沪深300日收益率。
        csi_all_return: 中证全指日收益率。
    """
    dates = make_trade_dates(start, n_days)
    missing = missing_dates or set()

    hs300_prices: list[float] = []
    csi_prices: list[float] = []
    hs300_base = 3000.0
    csi_base = 5000.0

    for dt in dates:
        if dt in missing:
            hs300_prices.append(float("nan"))
            csi_prices.append(float("nan"))
        else:
            hs300_base *= (1 + hs300_return)
            csi_base *= (1 + csi_all_return)
            hs300_prices.append(round(hs300_base, 2))
            csi_prices.append(round(csi_base, 2))

    return pd.DataFrame({
        "trade_date": dates,
        "hs300_close": hs300_prices,
        "csi_all_close": csi_prices,
    })


def make_benchmark_dict(
    start: date = date(2020, 1, 2),
    n_days: int = 200,
    hs300_return: float = 0.001,
    csi_all_return: float = 0.0008,
) -> dict:
    """生成基准数据字典格式（用于直接构造 BenchmarkData）。"""
    from ashare_quant.research.benchmarks import BenchmarkData

    dates = make_trade_dates(start, n_days)
    hs300: dict[date, float] = {}
    csi_all: dict[date, float] = {}
    hs300_base = 3000.0
    csi_base = 5000.0

    for dt in dates:
        hs300_base *= (1 + hs300_return)
        csi_base *= (1 + csi_all_return)
        hs300[dt] = round(hs300_base, 2)
        csi_all[dt] = round(csi_base, 2)

    return BenchmarkData(
        trade_dates=sorted(set(dates)),
        hs300_close=hs300,
        csi_all_close=csi_all,
    )


# ---------------------------------------------------------------------- #
# 小规模 walk-forward 测试数据
# ---------------------------------------------------------------------- #


def make_walk_forward_test_data(
    n_years: int = 3,
    n_stocks: int = 8,
) -> tuple[pd.DataFrame, pd.DataFrame, "BenchmarkData"]:
    """生成足够覆盖多个滚动折的测试数据。

    使用日历年切分路径，生成 n_years 个完整日历年的数据。

    Args:
        n_years: 日历年数（至少 5 以满足 min_total_years）。
        n_stocks: 股票数量。

    Returns:
        (quotes, status_table_df, benchmark_data)
    """
    start = date(2019, 1, 2)
    end = date(2019 + n_years - 1, 12, 31)
    dates = make_trade_dates_range(start, end)

    quotes = make_research_quotes(
        start=start, n_days=len(dates), n_stocks=n_stocks,
    )
    status_df = make_historical_status_table(start=start, n_stocks=n_stocks)
    benchmark = make_benchmark_dict(
        start=start, n_days=len(dates),
    )

    return quotes, status_df, benchmark


# ---------------------------------------------------------------------- #
# 辅助：构造完整研究环境
# ---------------------------------------------------------------------- #


def make_test_research_env(
    n_days: int = 200,
    n_stocks: int = 8,
) -> dict:
    """构造一个完整的测试环境字典。

    Returns:
        包含 quotes, status_table, universe_filter, benchmark, bt_config,
        walk_forward_config, monte_carlo_config 的字典。
    """
    from ashare_quant.backtest.config import BacktestConfig
    from ashare_quant.research.benchmarks import BenchmarkData
    from ashare_quant.research.monte_carlo import MonteCarloConfig
    from ashare_quant.research.universe import (
        HistoricalStatusTable,
        HistoricalUniverseFilter,
    )
    from ashare_quant.research.walk_forward import WalkForwardConfig

    start = date(2020, 1, 2)
    quotes = make_research_quotes(start=start, n_days=n_days, n_stocks=n_stocks)
    status_df = make_historical_status_table(start=start, n_stocks=n_stocks)
    status_table = HistoricalStatusTable(records=status_df)
    benchmark = make_benchmark_dict(start=start, n_days=n_days)

    universe_filter = HistoricalUniverseFilter(
        status_table=status_table,
        quotes=quotes,
        min_listing_days=120,
        min_valid_days=15,
        valid_days_window=20,
        min_turnover=20_000_000.0,
        turnover_window=20,
        lot_size=100,
        available_cash=1000.0,
    )

    bt_config = BacktestConfig()
    wf_config = WalkForwardConfig()
    mc_config = MonteCarloConfig(n_paths=100, random_seed=42)

    return {
        "quotes": quotes,
        "status_table": status_table,
        "status_df": status_df,
        "universe_filter": universe_filter,
        "benchmark": benchmark,
        "bt_config": bt_config,
        "walk_forward_config": wf_config,
        "monte_carlo_config": mc_config,
        "start": start,
        "n_days": n_days,
        "n_stocks": n_stocks,
    }


__all__ = [
    "make_trade_dates_range",
    "make_stock_quotes",
    "make_research_quotes",
    "make_historical_status_table",
    "make_status_table_with_delisting",
    "make_benchmark_data",
    "make_benchmark_dict",
    "make_walk_forward_test_data",
    "make_test_research_env",
]
