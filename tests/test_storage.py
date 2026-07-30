"""存储模块测试：Parquet、DuckDB、SHA-256。"""
from __future__ import annotations

import pandas as pd

from ashare_quant.constants import DAILY_QUOTE_FIELDS
from ashare_quant.samples import make_normal_raw
from ashare_quant.standardize import Standardizer
from ashare_quant.storage import Storage, file_sha256


def test_write_read_daily_quotes_parquet(storage, standardizer):
    curated = standardizer.standardize_daily_quotes(make_normal_raw("000001", 5), "akshare")
    path = storage.write_daily_quotes_parquet(curated, "test.parquet")
    assert path.exists()
    df = storage.read_parquet(path)
    assert len(df) == 5
    assert set(DAILY_QUOTE_FIELDS).issubset(set(df.columns))


def test_file_sha256_deterministic(tmp_path):
    p = tmp_path / "a.bin"
    p.write_bytes(b"hello")
    h1 = file_sha256(p)
    h2 = file_sha256(p)
    assert h1 == h2
    assert len(h1) == 64


def test_file_sha256_changes(tmp_path):
    p1 = tmp_path / "a.bin"
    p2 = tmp_path / "b.bin"
    p1.write_bytes(b"hello")
    p2.write_bytes(b"world")
    assert file_sha256(p1) != file_sha256(p2)


def test_duckdb_query_parquet(storage, standardizer):
    curated = standardizer.standardize_daily_quotes(make_normal_raw("000001", 10), "akshare")
    path = storage.write_daily_quotes_parquet(curated, "dq.parquet")
    df = storage.query_parquet(str(path), "SELECT COUNT(*) AS n FROM t")
    assert int(df["n"].iloc[0]) == 10


def test_layer_directories_created(tmp_path):
    s = Storage(tmp_path / "d")
    assert s.raw_dir.exists()
    assert s.curated_dir.exists()
    assert s.metadata_dir.exists()


def test_generic_parquet_raw_layer(storage):
    df = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})
    path = storage.write_generic_parquet(df, "raw.parquet", layer="raw")
    assert path.exists()
    out = storage.read_parquet(path)
    assert len(out) == 2
