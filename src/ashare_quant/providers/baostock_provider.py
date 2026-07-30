"""BaoStock 数据源适配器（备用数据源）。

baostock SDK 在方法内部惰性导入。BaoStock 对退市股票与历史数据有较好覆盖，
但对 ST 状态区间同样不提供完整历史。实际调用集中在 ``_call_*`` 方法，便于 mock。

为同时获得未复权与前复权 OHLC，分别以 adjustflag="3"（不复权）与 "2"（前复权）
抓取并在原始层按日期合并，输出与 AKShare 一致的中间列名 ``__raw_*`` / ``__qfq_*``。

G1-04 修复：校验登录及每次查询的 error_code，失败时抛出异常。
G1-01 修复：qfq 缺失时不回填 raw，保留 NaN。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pandas as pd

from .base import DataProvider


class BaoStockError(Exception):
    """BaoStock 请求异常。"""


def _to_bs_code(symbol: str) -> str:
    """将 6 位代码转为 BaoStock 格式 sh./sz.。"""
    s = str(symbol).strip().zfill(6)
    if s.startswith(("60", "68", "90", "11", "13")):
        return f"sh.{s}"
    return f"sz.{s}"


class BaoStockProvider(DataProvider):
    """BaoStock 备用数据源。"""

    @property
    def name(self) -> str:
        return "baostock"

    # ---- 原始 SDK 调用（可 mock） ----
    def _login(self) -> Any:
        import baostock as bs

        return bs.login()

    def _logout(self) -> Any:
        import baostock as bs

        return bs.logout()

    def _check_login_result(self, result: Any) -> None:
        """校验登录结果，失败时抛出异常。"""
        if result is None:
            return
        error_code = getattr(result, "error_code", "0")
        error_msg = getattr(result, "error_msg", "")
        if str(error_code) != "0":
            raise BaoStockError(
                f"BaoStock 登录失败: error_code={error_code}, error_msg={error_msg}"
            )

    def _call_daily_hist(
        self, bs_code: str, start: str, end: str, adjustflag: str
    ) -> pd.DataFrame:
        import baostock as bs

        fields = "date,open,high,low,close,volume,amount"
        rs = bs.query_history_k_data_plus(
            bs_code,
            fields,
            start_date=start,
            end_date=end,
            frequency="d",
            adjustflag=adjustflag,
        )
        # G1-04: 校验查询错误码
        if rs.error_code != "0":
            raise BaoStockError(
                f"BaoStock 日线查询失败: code={bs_code}, "
                f"error_code={rs.error_code}, error_msg={rs.error_msg}"
            )
        rows: list[list] = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        return pd.DataFrame(rows, columns=fields.split(","))

    def _call_stock_basic(self) -> pd.DataFrame:
        import baostock as bs

        rs = bs.query_stock_basic()
        if rs.error_code != "0":
            raise BaoStockError(
                f"BaoStock 证券主数据查询失败: "
                f"error_code={rs.error_code}, error_msg={rs.error_msg}"
            )
        rows: list[list] = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        return pd.DataFrame(rows, columns=rs.fields)

    def _call_trade_dates(self, start: str, end: str) -> pd.DataFrame:
        import baostock as bs

        rs = bs.query_trade_dates(start_date=start, end_date=end)
        if rs.error_code != "0":
            raise BaoStockError(
                f"BaoStock 交易日历查询失败: "
                f"error_code={rs.error_code}, error_msg={rs.error_msg}"
            )
        rows: list[list] = []
        while rs.error_code == "0" and rs.next():
            rows.append(rs.get_row_data())
        return pd.DataFrame(rows, columns=rs.fields)

    # ---- DataProvider 接口 ----
    def fetch_daily_quotes(
        self, symbol: str, start_date: date, end_date: date
    ) -> pd.DataFrame:
        login_result = self._login()
        self._check_login_result(login_result)
        try:
            bs_code = _to_bs_code(symbol)
            start = start_date.strftime("%Y-%m-%d")
            end = end_date.strftime("%Y-%m-%d")
            raw_df = self._call_daily_hist(bs_code, start, end, "3")  # 未复权
            qfq_df = self._call_daily_hist(bs_code, start, end, "2")  # 前复权
        finally:
            self._logout()
        if raw_df is None or raw_df.empty:
            return pd.DataFrame()

        raw_df = raw_df.copy()
        raw_df.rename(
            columns={
                "open": "__raw_open",
                "high": "__raw_high",
                "low": "__raw_low",
                "close": "__raw_close",
            },
            inplace=True,
        )
        if qfq_df is not None and not qfq_df.empty:
            qfq_df = qfq_df.copy()
            qfq_df.rename(
                columns={
                    "open": "__qfq_open",
                    "high": "__qfq_high",
                    "low": "__qfq_low",
                    "close": "__qfq_close",
                },
                inplace=True,
            )
            qfq_cols = ["date", "__qfq_open", "__qfq_high", "__qfq_low", "__qfq_close"]
            raw_df = raw_df.merge(qfq_df[qfq_cols], on="date", how="left")
        else:
            # G1-01: 禁止用 raw 回填 qfq，保留 NaN
            for c in ("__qfq_open", "__qfq_high", "__qfq_low", "__qfq_close"):
                raw_df[c] = pd.NA

        raw_df["__source_symbol"] = symbol
        raw_df["__fetched_at"] = datetime.utcnow()
        return raw_df

    def fetch_security_master(self) -> pd.DataFrame:
        login_result = self._login()
        self._check_login_result(login_result)
        try:
            basic = self._call_stock_basic()
        finally:
            self._logout()
        if basic is None or basic.empty:
            return pd.DataFrame()
        basic = basic.copy()
        basic["__fetched_at"] = datetime.utcnow()
        return basic

    def fetch_trade_calendar(
        self, start_date: date, end_date: date
    ) -> pd.DataFrame:
        login_result = self._login()
        self._check_login_result(login_result)
        try:
            cal = self._call_trade_dates(
                start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d")
            )
        finally:
            self._logout()
        if cal is None or cal.empty:
            return pd.DataFrame({"trade_date": []})
        cal = cal.copy()
        if "is_trading_day" in cal.columns:
            cal = cal[cal["is_trading_day"] == "1"]
        col = "calendar_date" if "calendar_date" in cal.columns else cal.columns[0]
        cal["trade_date"] = pd.to_datetime(cal[col]).dt.date
        return cal[["trade_date"]].reset_index(drop=True)
