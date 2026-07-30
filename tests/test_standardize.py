"""标准化与内容哈希测试。"""
from __future__ import annotations

import pandas as pd

from ashare_quant.constants import (
    DAILY_QUOTE_FIELDS,
    SIGNAL_PRICE_FIELDS,
    TRADEABLE_PRICE_FIELDS,
    TRADEABLE_VOLUME_FIELDS,
)
from ashare_quant.samples import make_normal_raw
from ashare_quant.standardize import Standardizer, content_hash


def test_standardize_normal_columns(standardizer):
    raw = make_normal_raw("000001", n_days=10)
    curated = standardizer.standardize_daily_quotes(raw, "akshare")
    assert list(curated.columns) == DAILY_QUOTE_FIELDS
    assert len(curated) == 10


def test_standardize_sorted_by_primary_key(standardizer):
    raw = make_normal_raw("000001", n_days=10)
    curated = standardizer.standardize_daily_quotes(raw, "akshare")
    dates = curated["trade_date"].tolist()
    assert dates == sorted(dates)


def test_adjustment_factor_computed(standardizer):
    raw = make_normal_raw("000001", n_days=5)
    curated = standardizer.standardize_daily_quotes(raw, "akshare")
    # qfq_ratio=1.0 -> factor == 1.0
    assert (curated["adjustment_factor"] == 1.0).all()


def test_suspended_detected_from_zero_volume(standardizer):
    from ashare_quant.samples import make_suspended_raw

    raw = make_suspended_raw("000004")
    curated = standardizer.standardize_daily_quotes(raw, "akshare")
    suspended = curated[curated["is_suspended"]]
    assert len(suspended) == 1
    assert suspended["is_tradable"].iloc[0] == False  # noqa: E712
    assert suspended["volume"].iloc[0] == 0


def test_signal_vs_tradable_separation(standardizer):
    """复权信号列与未复权成交列在 schema 中明确分离。"""
    raw = make_normal_raw("000001", n_days=3)
    curated = standardizer.standardize_daily_quotes(raw, "akshare")
    cols = set(curated.columns)
    # 信号列（前复权）
    assert set(SIGNAL_PRICE_FIELDS).issubset(cols)
    # 未复权成交列
    assert set(TRADEABLE_PRICE_FIELDS).issubset(cols)
    assert set(TRADEABLE_VOLUME_FIELDS).issubset(cols)
    # 两组无交集
    assert set(SIGNAL_PRICE_FIELDS).isdisjoint(set(TRADEABLE_PRICE_FIELDS))
    # adjustment_factor 属于信号侧
    assert "adjustment_factor" in cols


def test_content_hash_excludes_fetched_at(standardizer):
    raw = make_normal_raw("000001", n_days=5)
    c1 = standardizer.standardize_daily_quotes(raw, "akshare")
    # 改变 fetched_at 后哈希应不变
    c2 = c1.copy()
    c2["fetched_at"] = pd.Timestamp("2099-12-31")
    h1 = content_hash(c1)
    h2 = content_hash(c2)
    assert h1 == h2


def test_content_hash_changes_with_data(standardizer):
    raw = make_normal_raw("000001", n_days=5)
    c1 = standardizer.standardize_daily_quotes(raw, "akshare")
    c2 = c1.copy()
    c2.loc[0, "close_raw"] = c2.loc[0, "close_raw"] * 2
    assert content_hash(c1) != content_hash(c2)


def test_standardize_empty(standardizer):
    curated = standardizer.standardize_daily_quotes(pd.DataFrame(), "akshare")
    assert curated.empty
    assert list(curated.columns) == DAILY_QUOTE_FIELDS


def test_standardize_security_master_st_intervals(standardizer):
    """证券主数据 schema 必须能保留 ST 状态区间历史（避免幸存者偏差）。"""
    from ashare_quant.constants import SECURITY_MASTER_FIELDS
    from ashare_quant.samples import make_st_interval_master

    sm = make_st_interval_master()
    # 该样本已是规范 schema，验证字段与多区间保留
    assert set(SECURITY_MASTER_FIELDS).issubset(set(sm.columns))
    statuses = sm["st_status"].tolist()
    assert "st" in statuses
    assert "normal" in statuses
    # 多行区间（normal -> st -> normal 摘帽）
    assert len(sm) == 3
    assert sm["status_valid_from"].is_monotonic_increasing


def test_standardize_security_master_delisted(standardizer):
    """BaoStock 原始主数据标准化后保留退市日期。"""
    from ashare_quant.samples import make_baostock_master_raw

    sm = standardizer.standardize_security_master(make_baostock_master_raw(), "baostock")
    delisted = sm[sm["st_status"] == "delisted"]
    assert not delisted.empty
    assert delisted["delist_date"].notna().any()
    assert delisted["list_date"].notna().all()
