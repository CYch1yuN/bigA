"""合成样本构建器：为离线测试与 CLI 示例提供确定性合成数据。

样本覆盖：正常数据、重复记录、缺失交易日、停牌、退市、ST 区间、
OHLC 错误、负成交量、异常价格跳变、双源冲突。
所有样本为合成数据，非真实行情，不涉及凭据。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

import pandas as pd


def make_trade_calendar(start: date, end: date) -> pd.DataFrame:
    """生成合成交易日历（排除周末）。"""
    days = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:  # 周一至周五
            days.append(cur)
        cur += timedelta(days=1)
    return pd.DataFrame({"trade_date": days})


def _base_raw_row(
    symbol: str,
    d: date,
    o: float,
    h: float,
    low: float,
    c: float,
    vol: float,
    amt: float,
    qfq_ratio: float = 1.0,
    fetched_at: Optional[datetime] = None,
) -> dict:
    """构造一行原始中间格式数据。"""
    return {
        "date": d.isoformat(),
        "__source_symbol": symbol,
        "__raw_open": o,
        "__raw_high": h,
        "__raw_low": low,
        "__raw_close": c,
        "volume": vol,
        "amount": amt,
        "__qfq_open": o * qfq_ratio,
        "__qfq_high": h * qfq_ratio,
        "__qfq_low": low * qfq_ratio,
        "__qfq_close": c * qfq_ratio,
        "__fetched_at": fetched_at or datetime(2024, 1, 1, 9, 0, 0),
    }


def make_normal_raw(symbol: str = "000001", n_days: int = 30) -> pd.DataFrame:
    """生成 n_days 天正常原始日行情（含轻微上涨趋势）。"""
    rows = []
    base = date(2024, 1, 2)
    cal = make_trade_calendar(base, base + timedelta(days=n_days * 2))
    cal_dates = cal["trade_date"].tolist()[:n_days]
    price = 10.0
    for d in cal_dates:
        price *= 1.001
        rows.append(
            _base_raw_row(symbol, d, price * 0.99, price * 1.01, price * 0.98, price, 100000, 1000000)
        )
    return pd.DataFrame(rows)


def make_duplicate_raw(symbol: str = "000002") -> pd.DataFrame:
    """含一条主键重复记录的样本。"""
    df = make_normal_raw(symbol, n_days=10)
    dup = df.iloc[[3]].copy()
    return pd.concat([df, dup], ignore_index=True)


def make_missing_day_raw(symbol: str = "000003") -> pd.DataFrame:
    """删除中间一个交易日的样本（用于缺失交易日检查）。"""
    df = make_normal_raw(symbol, n_days=15)
    return df.drop(index=7).reset_index(drop=True)


def make_suspended_raw(symbol: str = "000004") -> pd.DataFrame:
    """含停牌日（成交量为 0）的样本。"""
    df = make_normal_raw(symbol, n_days=12)
    df.loc[5, "volume"] = 0
    df.loc[5, "amount"] = 0
    df.loc[5, "__raw_open"] = df.loc[4, "__raw_close"]
    df.loc[5, "__raw_high"] = df.loc[4, "__raw_close"]
    df.loc[5, "__raw_low"] = df.loc[4, "__raw_close"]
    df.loc[5, "__raw_close"] = df.loc[4, "__raw_close"]
    for c in ("__qfq_open", "__qfq_high", "__qfq_low", "__qfq_close"):
        df.loc[5, c] = df.loc[4, c]
    return df


def make_ohlc_error_raw(symbol: str = "000005") -> pd.DataFrame:
    """含 OHLC 关系错误的样本（high < low）。"""
    df = make_normal_raw(symbol, n_days=10)
    df.loc[4, "__raw_high"] = 5.0
    df.loc[4, "__raw_low"] = 50.0
    df.loc[4, "__qfq_high"] = 5.0
    df.loc[4, "__qfq_low"] = 50.0
    return df


def make_negative_volume_raw(symbol: str = "000006") -> pd.DataFrame:
    """含负成交量的样本。"""
    df = make_normal_raw(symbol, n_days=10)
    df.loc[3, "volume"] = -1000
    return df


def make_price_jump_raw(symbol: str = "000007") -> pd.DataFrame:
    """含异常价格跳变（>20%）的样本。"""
    df = make_normal_raw(symbol, n_days=12)
    df.loc[6, "__raw_close"] = df.loc[5, "__raw_close"] * 1.5
    df.loc[6, "__qfq_close"] = df.loc[5, "__qfq_close"] * 1.5
    return df


def make_cross_source_conflict_raw(symbol: str = "000008") -> pd.DataFrame:
    """用于跨源冲突的样本（收盘价与基准差异大）。"""
    df = make_normal_raw(symbol, n_days=10)
    # 与 normal 样本相比，收盘价差异 > 2%
    df["__raw_close"] = df["__raw_close"] * 1.10
    df["__qfq_close"] = df["__qfq_close"] * 1.10
    return df


def make_delisted_master(symbol: str = "000009") -> pd.DataFrame:
    """证券主数据：含一只退市股票（退市日早于上市日的错误样例可选）。"""
    observed = datetime(2024, 1, 1, 9, 0, 0)
    return pd.DataFrame(
        [
            {
                "symbol": symbol,
                "name": "退市示例",
                "list_date": date(2015, 6, 1),
                "delist_date": date(2020, 5, 29),
                "board": "main",
                "st_status": "delisted",
                "status_valid_from": date(2015, 6, 1),
                "status_valid_to": date(2020, 5, 29),
                "observed_at": observed,
            },
            {
                "symbol": "000001",
                "name": "正常股",
                "list_date": date(2010, 1, 1),
                "delist_date": None,
                "board": "main",
                "st_status": "normal",
                "status_valid_from": date(2010, 1, 1),
                "status_valid_to": None,
                "observed_at": observed,
            },
        ]
    )


def make_st_interval_master(symbol: str = "000010") -> pd.DataFrame:
    """证券主数据：含 ST 状态区间历史（多行不同区间）。"""
    observed = datetime(2024, 1, 1, 9, 0, 0)
    return pd.DataFrame(
        [
            {
                "symbol": symbol,
                "name": "ST示例",
                "list_date": date(2012, 3, 1),
                "delist_date": None,
                "board": "main",
                "st_status": "normal",
                "status_valid_from": date(2012, 3, 1),
                "status_valid_to": date(2018, 4, 30),
                "observed_at": observed,
            },
            {
                "symbol": symbol,
                "name": "ST示例",
                "list_date": date(2012, 3, 1),
                "delist_date": None,
                "board": "main",
                "st_status": "st",
                "status_valid_from": date(2018, 5, 1),
                "status_valid_to": date(2020, 6, 30),
                "observed_at": observed,
            },
            {
                "symbol": symbol,
                "name": "示例(摘帽)",
                "list_date": date(2012, 3, 1),
                "delist_date": None,
                "board": "main",
                "st_status": "normal",
                "status_valid_from": date(2020, 7, 1),
                "status_valid_to": None,
                "observed_at": observed,
            },
        ]
    )


def make_status_contradiction_master() -> pd.DataFrame:
    """含状态矛盾（退市日早于上市日）的证券主数据。"""
    return pd.DataFrame(
        [
            {
                "symbol": "000099",
                "name": "矛盾股",
                "list_date": date(2020, 1, 1),
                "delist_date": date(2019, 1, 1),  # 早于上市日
                "board": "main",
                "st_status": "delisted",
                "status_valid_from": date(2020, 1, 1),
                "status_valid_to": date(2019, 1, 1),
                "observed_at": datetime(2024, 1, 1, 9, 0, 0),
            }
        ]
    )


def make_baostock_master_raw() -> pd.DataFrame:
    """BaoStock query_stock_basic 原生格式样本，含一只退市股票。

    列：code, code_name, ipoDate, ouDate, type, status。
    """
    return pd.DataFrame(
        [
            {
                "code": "sh.600001",
                "code_name": "退市示例",
                "ipoDate": "2015-06-01",
                "ouDate": "2020-05-29",
                "type": "1",
                "status": "0",  # 退市
            },
            {
                "code": "sz.000001",
                "code_name": "正常股",
                "ipoDate": "2010-01-01",
                "ouDate": "",
                "type": "1",
                "status": "1",  # 正常
            },
        ]
    )


__all__ = [
    "make_trade_calendar",
    "make_normal_raw",
    "make_duplicate_raw",
    "make_missing_day_raw",
    "make_suspended_raw",
    "make_ohlc_error_raw",
    "make_negative_volume_raw",
    "make_price_jump_raw",
    "make_cross_source_conflict_raw",
    "make_delisted_master",
    "make_st_interval_master",
    "make_status_contradiction_master",
    "make_baostock_master_raw",
]
