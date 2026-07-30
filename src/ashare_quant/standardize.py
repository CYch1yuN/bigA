"""标准化模块：将数据源原生原始数据映射为规范 schema 的 curated 数据。

设计要点：
1. 确定性：相同输入 + 配置产生相同输出（按主键排序、稳定类型转换）。
2. ``fetched_at`` 等非确定字段保留在输出中，但不参与内容哈希（见 content_hash）。
3. 复权信号列（*_qfq、adjustment_factor）与未复权成交列（*_raw、volume、amount）明确分离。
"""
from __future__ import annotations

import hashlib
import io
from datetime import date, datetime
from typing import Iterable

import numpy as np
import pandas as pd

from .constants import (
    DAILY_QUOTE_FIELDS,
    DAILY_QUOTE_PRIMARY_KEY,
    NONDETERMINISTIC_FIELDS,
    SECURITY_MASTER_FIELDS,
    SECURITY_MASTER_PRIMARY_KEY,
    SOURCE_AKSHARE,
    SOURCE_BAOSTOCK,
)


class Standardizer:
    """原始 -> curated 标准化器。"""

    # 日行情中间列名 -> 规范列名
    _DAILY_MAP = {
        "__source_symbol": "symbol",
        "date": "trade_date",
        "__raw_open": "open_raw",
        "__raw_high": "high_raw",
        "__raw_low": "low_raw",
        "__raw_close": "close_raw",
        "volume": "volume",
        "amount": "amount",
        "__qfq_open": "open_qfq",
        "__qfq_high": "high_qfq",
        "__qfq_low": "low_qfq",
        "__qfq_close": "close_qfq",
        "__fetched_at": "fetched_at",
    }

    def standardize_daily_quotes(
        self, raw_df: pd.DataFrame, source: str
    ) -> pd.DataFrame:
        """将原始日行情标准化为规范 schema。

        参数:
            raw_df: 提供器返回的原始数据（含 ``__raw_*`` / ``__qfq_*`` 中间列）。
            source: 数据源标识 ``akshare`` / ``baostock``。
        """
        if raw_df is None or raw_df.empty:
            return pd.DataFrame(columns=DAILY_QUOTE_FIELDS)

        df = raw_df.copy()
        # 仅保留已知映射列
        present = {k: v for k, v in self._DAILY_MAP.items() if k in df.columns}
        df = df[list(present.keys())].rename(columns=present)

        # 类型转换（确定性）
        df["symbol"] = df["symbol"].astype(str).str.strip().str.zfill(6)
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        for col in (
            "open_raw",
            "high_raw",
            "low_raw",
            "close_raw",
            "volume",
            "amount",
            "open_qfq",
            "high_qfq",
            "low_qfq",
            "close_qfq",
        ):
            df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)
        if "fetched_at" in df.columns:
            df["fetched_at"] = pd.to_datetime(df["fetched_at"], errors="coerce")

        # 复权因子 = 前复权收盘 / 未复权收盘（guard 0）
        df["adjustment_factor"] = _safe_divide(df["close_qfq"], df["close_raw"])

        # 停牌/可交易：成交量为 0 视为停牌（免费源不提供显式停牌标记时的保守推断）
        df["is_suspended"] = df["volume"].fillna(0) == 0
        df["is_tradable"] = ~df["is_suspended"]

        df["source"] = source

        # 确定性排序
        df = df.sort_values(DAILY_QUOTE_PRIMARY_KEY, kind="mergesort").reset_index(
            drop=True
        )
        return df[DAILY_QUOTE_FIELDS]

    def standardize_security_master(
        self, raw_df: pd.DataFrame, source: str
    ) -> pd.DataFrame:
        """将原始证券主数据标准化为规范 schema。

        不同数据源字段差异较大，此处按 source 分派映射。
        无法获取的字段（如退市日、ST 历史区间）置为空，不伪造。
        """
        if raw_df is None or raw_df.empty:
            return pd.DataFrame(columns=SECURITY_MASTER_FIELDS)

        df = raw_df.copy()
        if source == SOURCE_AKSHARE:
            out = self._map_akshare_master(df)
        elif source == SOURCE_BAOSTOCK:
            out = self._map_baostock_master(df)
        else:
            raise ValueError(f"未知数据源: {source}")

        # 类型与排序
        for col in ("list_date", "delist_date", "status_valid_from", "status_valid_to"):
            if col in out.columns:
                out[col] = pd.to_datetime(out[col], errors="coerce").dt.date
        if "observed_at" in out.columns:
            out["observed_at"] = pd.to_datetime(out["observed_at"], errors="coerce")
        out["symbol"] = out["symbol"].astype(str).str.strip().str.zfill(6)
        for col in ("name", "board", "st_status"):
            out[col] = out[col].astype(str).str.strip()
        out = out.sort_values(
            SECURITY_MASTER_PRIMARY_KEY, kind="mergesort"
        ).reset_index(drop=True)
        return out[SECURITY_MASTER_FIELDS]

    def _map_akshare_master(self, df: pd.DataFrame) -> pd.DataFrame:
        """AKShare stock_info_a_code_name 列：code, name, __is_st/__st_status。"""
        out = pd.DataFrame()
        out["symbol"] = df["code"].astype(str).str.zfill(6)
        out["name"] = df.get("name", "")
        out["list_date"] = pd.NaT
        out["delist_date"] = pd.NaT
        out["board"] = out["symbol"].apply(_infer_board)
        # ST 状态：优先使用 __st_status（G1-02 修复），回退到 __is_st
        if "__st_status" in df.columns:
            out["st_status"] = df["__st_status"].astype(str).str.strip()
        else:
            out["st_status"] = df.get("__is_st", False).apply(
                lambda x: "st" if x else "normal"
            )
        # AKShare 仅提供当前快照，不知道 ST 真实生效日，保持空值不伪造。
        out["status_valid_from"] = pd.NaT
        out["status_valid_to"] = pd.NaT
        # observed_at 记录快照观察时间（抓取时间），不是状态生效日。
        if "__fetched_at" in df.columns:
            out["observed_at"] = pd.to_datetime(df["__fetched_at"], errors="coerce")
        else:
            out["observed_at"] = pd.NaT
        return out

    def _map_baostock_master(self, df: pd.DataFrame) -> pd.DataFrame:
        """BaoStock query_stock_basic 列：code, code_name,ipoDate, ouDate, type, status。"""
        out = pd.DataFrame()
        out["symbol"] = df["code"].astype(str).str.replace(
            r"^(sh|sz)\.", "", regex=True
        ).str.zfill(6)
        out["name"] = df.get("code_name", "")
        out["list_date"] = df.get("ipoDate", pd.NaT)
        out["delist_date"] = df.get("ouDate", pd.NaT)
        out["board"] = out["symbol"].apply(_infer_board)
        # status: 1=正常 0=退市
        status = df.get("status", "1")
        out["st_status"] = status.apply(lambda x: "delisted" if str(x) == "0" else "normal")
        out["status_valid_from"] = out["list_date"]
        out["status_valid_to"] = out["delist_date"]
        # observed_at 记录快照观察时间
        if "__fetched_at" in df.columns:
            out["observed_at"] = pd.to_datetime(df["__fetched_at"], errors="coerce")
        else:
            out["observed_at"] = pd.NaT
        return out


def _safe_divide(numer: pd.Series, denom: pd.Series) -> pd.Series:
    """安全除法，分母为 0 或 NaN 时返回 NaN（不掩盖坏数据）。

    分母为 0 时结果为 inf/-inf，统一替换为 NaN，由 required_completeness 检查阻断。
    """
    numer_f = pd.to_numeric(numer, errors="coerce").astype(float)
    denom_f = pd.to_numeric(denom, errors="coerce").astype(float)
    result = numer_f / denom_f
    # 分母为 0 时结果为 inf/-inf，替换为 NaN
    result = result.replace([np.inf, -np.inf], np.nan)
    return result.astype(float)


def _infer_board(symbol: str) -> str:
    """根据代码推断板块。"""
    s = str(symbol).strip().zfill(6)
    if s.startswith(("60", "68")):
        return "star" if s.startswith("68") else "main"
    if s.startswith("30"):
        return "szse"
    if s.startswith(("43", "83", "87", "88")):
        return "bjse"
    return "main"


def content_hash(
    df: pd.DataFrame, exclude_fields: Iterable[str] | None = None
) -> str:
    """计算 DataFrame 内容 SHA-256，排除非确定字段。

    通过将 DataFrame 写为确定性 CSV（固定列顺序、排序、空值规范化）后哈希，
    保证相同内容产生相同哈希。``fetched_at`` 默认被排除。
    """
    exclude = set(exclude_fields or NONDETERMINISTIC_FIELDS)
    cols = [c for c in df.columns if c not in exclude]
    sub = df[cols].copy()
    # 确定性：按列名排序 + 行排序（若存在主键则按主键，否则按全部列）
    sub = sub.sort_values(by=cols if cols else [0], kind="mergesort").reset_index(
        drop=True
    )
    buf = io.StringIO()
    sub.to_csv(buf, index=False, date_format="%Y-%m-%d", lineterminator="\n")
    return hashlib.sha256(buf.getvalue().encode("utf-8")).hexdigest()


__all__ = ["Standardizer", "content_hash"]
