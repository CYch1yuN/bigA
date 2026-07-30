"""Schema 定义：规范字段类型、Pydantic 校验模型与 Arrow schema。

复权信号列与未复权成交列在类型与文档中明确分离。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

import pyarrow as pa
from pydantic import BaseModel, field_validator

from .constants import (
    DAILY_QUOTE_FIELDS,
    DAILY_QUOTE_SCHEMA_VERSION,
    SECURITY_MASTER_FIELDS,
    SECURITY_MASTER_SCHEMA_VERSION,
)


class DailyQuoteRecord(BaseModel):
    """单条日行情规范记录。

    未复权列（*_raw、volume、amount）用于成交模拟；
    前复权列（*_qfq、adjustment_factor）用于信号生成。
    """

    symbol: str
    trade_date: date
    open_raw: float
    high_raw: float
    low_raw: float
    close_raw: float
    volume: float
    amount: float
    open_qfq: float
    high_qfq: float
    low_qfq: float
    close_qfq: float
    adjustment_factor: float
    is_suspended: bool
    is_tradable: bool
    source: str
    fetched_at: Optional[datetime] = None

    model_config = {"extra": "forbid"}

    @field_validator("symbol")
    @classmethod
    def _symbol_nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("symbol 不能为空")
        return v.strip()

    @field_validator("trade_date")
    @classmethod
    def _trade_date(cls, v: date) -> date:
        if v is None:
            raise ValueError("trade_date 不能为空")
        return v


class SecurityMasterRecord(BaseModel):
    """证券主数据记录，覆盖上市/退市/ST 状态区间历史。"""

    symbol: str
    name: str
    list_date: Optional[date] = None
    delist_date: Optional[date] = None
    board: str
    st_status: str
    status_valid_from: date
    status_valid_to: Optional[date] = None

    model_config = {"extra": "forbid"}

    @field_validator("symbol", "name", "board", "st_status")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("字段不能为空")
        return v.strip()


def daily_quote_arrow_schema() -> pa.Schema:
    """日行情 Parquet Arrow schema。"""
    return pa.schema(
        [
            ("symbol", pa.string()),
            ("trade_date", pa.date32()),
            ("open_raw", pa.float64()),
            ("high_raw", pa.float64()),
            ("low_raw", pa.float64()),
            ("close_raw", pa.float64()),
            ("volume", pa.float64()),
            ("amount", pa.float64()),
            ("open_qfq", pa.float64()),
            ("high_qfq", pa.float64()),
            ("low_qfq", pa.float64()),
            ("close_qfq", pa.float64()),
            ("adjustment_factor", pa.float64()),
            ("is_suspended", pa.bool_()),
            ("is_tradable", pa.bool_()),
            ("source", pa.string()),
            ("fetched_at", pa.timestamp("us")),
        ]
    )


def security_master_arrow_schema() -> pa.Schema:
    """证券主数据 Parquet Arrow schema。"""
    return pa.schema(
        [
            ("symbol", pa.string()),
            ("name", pa.string()),
            ("list_date", pa.date32()),
            ("delist_date", pa.date32()),
            ("board", pa.string()),
            ("st_status", pa.string()),
            ("status_valid_from", pa.date32()),
            ("status_valid_to", pa.date32()),
        ]
    )


def validate_dataframe_columns(columns: list[str], expected: list[str]) -> None:
    """校验 DataFrame 列是否包含全部期望字段。"""
    missing = [c for c in expected if c not in columns]
    if missing:
        raise ValueError(f"缺少必需字段: {missing}")


SCHEMA_VERSIONS = {
    "daily_quote": DAILY_QUOTE_SCHEMA_VERSION,
    "security_master": SECURITY_MASTER_SCHEMA_VERSION,
}

__all__ = [
    "DailyQuoteRecord",
    "SecurityMasterRecord",
    "daily_quote_arrow_schema",
    "security_master_arrow_schema",
    "validate_dataframe_columns",
    "SCHEMA_VERSIONS",
    "DAILY_QUOTE_FIELDS",
    "SECURITY_MASTER_FIELDS",
]
