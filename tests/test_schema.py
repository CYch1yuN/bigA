"""Schema 定义测试。"""
from __future__ import annotations

from ashare_quant import schema as schema_mod
from ashare_quant.constants import (
    DAILY_QUOTE_FIELDS,
    DAILY_QUOTE_SCHEMA_VERSION,
    SECURITY_MASTER_FIELDS,
    SECURITY_MASTER_SCHEMA_VERSION,
    SIGNAL_PRICE_FIELDS,
    TRADEABLE_PRICE_FIELDS,
)


def test_daily_quote_required_fields():
    required = {
        "symbol", "trade_date", "open_raw", "high_raw", "low_raw", "close_raw",
        "volume", "amount", "open_qfq", "high_qfq", "low_qfq", "close_qfq",
        "adjustment_factor", "is_suspended", "is_tradable", "source", "fetched_at",
    }
    assert required.issubset(set(DAILY_QUOTE_FIELDS))


def test_security_master_required_fields():
    required = {
        "symbol", "name", "list_date", "delist_date", "board",
        "st_status", "status_valid_from", "status_valid_to",
    }
    assert required.issubset(set(SECURITY_MASTER_FIELDS))


def test_schema_versions_defined():
    assert DAILY_QUOTE_SCHEMA_VERSION
    assert SECURITY_MASTER_SCHEMA_VERSION
    assert schema_mod.SCHEMA_VERSIONS["daily_quote"] == DAILY_QUOTE_SCHEMA_VERSION


def test_signal_and_tradable_disjoint():
    assert set(SIGNAL_PRICE_FIELDS).isdisjoint(set(TRADEABLE_PRICE_FIELDS))


def test_arrow_schemas():
    dq = schema_mod.daily_quote_arrow_schema()
    assert "close_qfq" in dq.names
    assert "close_raw" in dq.names
    sm = schema_mod.security_master_arrow_schema()
    assert "st_status" in sm.names


def test_validate_columns_missing():
    import pytest

    with pytest.raises(ValueError):
        schema_mod.validate_dataframe_columns(["symbol"], DAILY_QUOTE_FIELDS)
