"""AKShare 数据源适配器（主数据源）。

akshare SDK 在方法内部惰性导入，使离线测试无需安装 akshare。
实际网络调用集中在 ``_call_*`` 私有方法，便于在测试中 mock。

为同时获得未复权与前复权 OHLC，分别以 ``adjust=""`` 与 ``adjust="qfq"``
抓取并在原始层按日期合并，标准化阶段再映射为规范字段。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pandas as pd

from .base import DataProvider


class AKShareProvider(DataProvider):
    """AKShare 主数据源。

    注意：AKShare 为免费数据源，对退市股票、历史 ST 状态区间与历史复权因子的
    覆盖存在缺口（见 docs/phase-1-limitations.md）。本适配器只返回可获取数据，
    不伪造或静默填充缺失部分。
    """

    @property
    def name(self) -> str:
        return "akshare"

    # ---- 原始 SDK 调用（可 mock） ----
    def _call_daily_hist(
        self, symbol: str, start: str, end: str, adjust: str
    ) -> pd.DataFrame:
        import akshare as ak  # 惰性导入

        return ak.stock_zh_a_hist(
            symbol=symbol,
            period="daily",
            start_date=start,
            end_date=end,
            adjust=adjust,
        )

    def _call_code_name(self) -> pd.DataFrame:
        import akshare as ak

        return ak.stock_info_a_code_name()

    def _call_st_list(self) -> pd.DataFrame:
        import akshare as ak

        return ak.stock_zh_a_st_em()

    def _call_trade_dates(self) -> pd.DataFrame:
        import akshare as ak

        return ak.tool_trade_date_hist_sina()

    # ---- DataProvider 接口 ----
    def fetch_daily_quotes(
        self, symbol: str, start_date: date, end_date: date
    ) -> pd.DataFrame:
        start = start_date.strftime("%Y%m%d")
        end = end_date.strftime("%Y%m%d")
        raw_unadj = self._call_daily_hist(symbol, start, end, "")
        raw_qfq = self._call_daily_hist(symbol, start, end, "qfq")
        if raw_unadj is None or raw_unadj.empty:
            return pd.DataFrame()

        # AKShare 列：日期/开盘/收盘/最高/最低/成交量/成交额/振幅/涨跌幅/涨跌额/换手率
        rename = {
            "日期": "date",
            "开盘": "open",
            "收盘": "close",
            "最高": "high",
            "最低": "low",
            "成交量": "volume",
            "成交额": "amount",
        }
        u = raw_unadj.rename(columns=rename)[
            ["date", "open", "close", "high", "low", "volume", "amount"]
        ].copy()
        u["date"] = pd.to_datetime(u["date"]).dt.date.astype(str)

        merged = u.copy()
        merged.rename(
            columns={
                "open": "__raw_open",
                "close": "__raw_close",
                "high": "__raw_high",
                "low": "__raw_low",
                "volume": "volume",
                "amount": "amount",
            },
            inplace=True,
        )

        if raw_qfq is not None and not raw_qfq.empty:
            q = raw_qfq.rename(columns=rename)[
                ["date", "open", "close", "high", "low"]
            ].copy()
            q["date"] = pd.to_datetime(q["date"]).dt.date.astype(str)
            q.rename(
                columns={
                    "open": "__qfq_open",
                    "close": "__qfq_close",
                    "high": "__qfq_high",
                    "low": "__qfq_low",
                },
                inplace=True,
            )
            merged = merged.merge(q, on="date", how="left")
        else:
            # G1-01: 禁止用 raw 回填 qfq，保留 NaN 让质量检查捕获
            for c in ("__qfq_open", "__qfq_close", "__qfq_high", "__qfq_low"):
                merged[c] = pd.NA

        merged["__source_symbol"] = symbol
        merged["__fetched_at"] = datetime.utcnow()
        return merged

    def fetch_security_master(self) -> pd.DataFrame:
        """抓取证券主数据。

        G1-02 修复：
        - 支持中文列名（代码）与英文列名（code）。
        - ST 接口不可用或 schema 变化时标记 ``__st_status`` 为 ``unknown``，不默认 normal。
        """
        code_name = self._call_code_name()
        code_name = code_name.copy()

        # 确定 code 列（中文或英文）
        code_col = "code" if "code" in code_name.columns else (
            "代码" if "代码" in code_name.columns else code_name.columns[0]
        )

        # 尝试获取 ST 列表
        try:
            st_df = self._call_st_list()
        except Exception:
            # ST 接口异常：标记所有为 unknown
            code_name["__st_status"] = "unknown"
            code_name["__fetched_at"] = datetime.utcnow()
            return code_name

        # 识别 ST 列表中的代码列（支持中文和英文）
        if st_df is not None and not st_df.empty:
            st_code_col = None
            for candidate in ("code", "代码", "symbol", "股票代码"):
                if candidate in st_df.columns:
                    st_code_col = candidate
                    break

            if st_code_col is not None:
                st_set = set(st_df[st_code_col].astype(str).str.strip().str.zfill(6))
                code_name["__st_status"] = (
                    code_name[code_col]
                    .astype(str)
                    .str.strip()
                    .str.zfill(6)
                    .apply(lambda x: "st" if x in st_set else "normal")
                )
            else:
                # schema 变化：无法识别代码列，标记 unknown
                code_name["__st_status"] = "unknown"
        else:
            # ST 列表为空（无 ST 股票或接口返回空）
            code_name["__st_status"] = "normal"

        code_name["__fetched_at"] = datetime.utcnow()
        return code_name

    def fetch_trade_calendar(
        self, start_date: date, end_date: date
    ) -> pd.DataFrame:
        dates = self._call_trade_dates()
        if dates is None or dates.empty:
            return pd.DataFrame({"trade_date": []})
        col = "trade_date" if "trade_date" in dates.columns else dates.columns[0]
        cal = dates[[col]].copy()
        cal["trade_date"] = pd.to_datetime(cal[col]).dt.date
        cal = cal[(cal["trade_date"] >= start_date) & (cal["trade_date"] <= end_date)]
        return cal.reset_index(drop=True)
