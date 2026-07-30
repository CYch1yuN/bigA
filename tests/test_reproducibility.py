"""可复现性测试：相同输入与配置产生相同 curated 数据与稳定内容哈希。"""
from __future__ import annotations

import pandas as pd

from ashare_quant.manifest import build_manifest
from ashare_quant.samples import make_normal_raw
from ashare_quant.standardize import Standardizer, content_hash


def test_same_input_same_curated(standardizer):
    raw = make_normal_raw("000001", 20)
    c1 = standardizer.standardize_daily_quotes(raw, "akshare")
    c2 = standardizer.standardize_daily_quotes(raw, "akshare")
    pd.testing.assert_frame_equal(c1, c2)


def test_same_input_stable_content_hash(standardizer):
    raw = make_normal_raw("000001", 20)
    c1 = standardizer.standardize_daily_quotes(raw, "akshare")
    c2 = standardizer.standardize_daily_quotes(raw, "akshare")
    assert content_hash(c1) == content_hash(c2)


def test_fetched_at_does_not_break_reproducibility(standardizer):
    """不同 fetched_at 不应改变内容哈希。"""
    raw = make_normal_raw("000001", 10)
    c1 = standardizer.standardize_daily_quotes(raw, "akshare")
    c2 = c1.copy()
    c2["fetched_at"] = pd.Timestamp("2030-01-01 00:00:00")
    assert content_hash(c1) == content_hash(c2)


def test_manifest_content_hash_stable(config, standardizer, tmp_path):
    raw = make_normal_raw("000001", 15)
    curated = standardizer.standardize_daily_quotes(raw, "akshare")
    path = tmp_path / "c.parquet"
    curated.to_parquet(path, index=False)
    h = content_hash(curated)
    m1 = build_manifest(
        source="akshare", symbol="000001", start_date="2024-01-02",
        end_date="2024-02-15", row_count=len(curated), files={"c": path},
        config=config, schema_version="1.0.0", content_hash_value=h, code_commit="x",
    )
    # 重新计算应一致
    h2 = content_hash(curated)
    assert m1["content_hash"] == h2


def test_reproducible_across_two_standardizer_instances():
    raw = make_normal_raw("000001", 12)
    c1 = Standardizer().standardize_daily_quotes(raw, "akshare")
    c2 = Standardizer().standardize_daily_quotes(raw, "akshare")
    assert content_hash(c1) == content_hash(c2)
