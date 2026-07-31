"""回测测试数据构建器：为回测器测试提供确定性合成行情数据。

所有数据为合成数据，非真实行情。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

import pandas as pd


def make_trade_dates(start: date, n_days: int) -> list[date]:
    """生成 n_days 个交易日（排除周末）。"""
    dates = []
    cur = start
    while len(dates) < n_days:
        if cur.weekday() < 5:
            dates.append(cur)
        cur += timedelta(days=1)
    return dates


def make_bar(
    symbol: str,
    dt: date,
    open_price: float = 10.0,
    high: Optional[float] = None,
    low: Optional[float] = None,
    close: Optional[float] = None,
    volume: float = 100000,
    amount: Optional[float] = None,
    qfq_ratio: float = 1.0,
    is_suspended: bool = False,
    is_tradable: bool = True,
    adjustment_factor: float = 1.0,
) -> dict:
    """构造一行 curated 日行情数据。"""
    h = high if high is not None else open_price * 1.01
    lo = low if low is not None else open_price * 0.99
    c = close if close is not None else open_price
    amt = amount if amount is not None else volume * c
    return {
        "symbol": symbol,
        "trade_date": dt,
        "open_raw": open_price,
        "high_raw": h,
        "low_raw": lo,
        "close_raw": c,
        "volume": volume,
        "amount": amt,
        "open_qfq": open_price * qfq_ratio,
        "high_qfq": h * qfq_ratio,
        "low_qfq": lo * qfq_ratio,
        "close_qfq": c * qfq_ratio,
        "adjustment_factor": adjustment_factor,
        "is_suspended": is_suspended,
        "is_tradable": is_tradable,
        "source": "test",
        "fetched_at": pd.Timestamp("2024-01-01 09:00:00"),
    }


def make_quotes(
    symbol: str = "000001",
    start: date = date(2024, 1, 2),
    n_days: int = 10,
    base_price: float = 10.0,
    daily_return: float = 0.001,
    is_suspended_days: Optional[list[int]] = None,
    is_tradable: bool = True,
) -> pd.DataFrame:
    """生成 n_days 天日行情数据。

    Args:
        symbol: 股票代码。
        start: 起始日期。
        n_days: 交易日天数。
        base_price: 基础价格。
        daily_return: 每日收益率。
        is_suspended_days: 停牌日索引列表（0-based）。
        is_tradable: 是否可交易。
    """
    dates = make_trade_dates(start, n_days)
    suspended = is_suspended_days or []
    rows = []
    price = base_price
    for i, dt in enumerate(dates):
        if i > 0:
            price *= (1 + daily_return)
        is_susp = i in suspended
        row = make_bar(
            symbol=symbol,
            dt=dt,
            open_price=price * 0.998,
            high=price * 1.01,
            low=price * 0.99,
            close=price,
            is_suspended=is_susp,
            is_tradable=is_tradable and not is_susp,
        )
        if is_susp:
            row["volume"] = 0
            row["amount"] = 0
            row["open_raw"] = rows[-1]["close_raw"]
            row["high_raw"] = rows[-1]["close_raw"]
            row["low_raw"] = rows[-1]["close_raw"]
            row["close_raw"] = rows[-1]["close_raw"]
            for c in ("open_qfq", "high_qfq", "low_qfq", "close_qfq"):
                row[c] = rows[-1][c]
        rows.append(row)
    return pd.DataFrame(rows)


def make_two_stock_quotes(
    symbol1: str = "000001",
    symbol2: str = "000002",
    start: date = date(2024, 1, 2),
    n_days: int = 10,
    price1: float = 10.0,
    price2: float = 5.0,
) -> pd.DataFrame:
    """生成两只股票的日行情数据。"""
    df1 = make_quotes(symbol1, start, n_days, price1)
    df2 = make_quotes(symbol2, start, n_days, price2)
    return pd.concat([df1, df2], ignore_index=True).sort_values(["trade_date", "symbol"]).reset_index(drop=True)


def make_limit_up_bar(
    symbol: str = "000001",
    dt: date = date(2024, 1, 3),
    prev_close: float = 10.0,
    ratio: float = 0.10,
) -> dict:
    """构造涨停日行情（开盘价 = 涨停价）。"""
    limit_price = round(prev_close * (1 + ratio), 2)
    return make_bar(
        symbol=symbol,
        dt=dt,
        open_price=limit_price,
        high=limit_price,
        low=limit_price,
        close=limit_price,
    )


def make_limit_down_bar(
    symbol: str = "000001",
    dt: date = date(2024, 1, 3),
    prev_close: float = 10.0,
    ratio: float = 0.10,
) -> dict:
    """构造跌停日行情（开盘价 = 跌停价）。"""
    limit_price = round(prev_close * (1 - ratio), 2)
    return make_bar(
        symbol=symbol,
        dt=dt,
        open_price=limit_price,
        high=limit_price,
        low=limit_price,
        close=limit_price,
    )


__all__ = [
    "make_trade_dates",
    "make_bar",
    "make_quotes",
    "make_two_stock_quotes",
    "make_limit_up_bar",
    "make_limit_down_bar",
]
