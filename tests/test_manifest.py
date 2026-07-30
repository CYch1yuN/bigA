"""数据版本清单测试。"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from ashare_quant.manifest import build_manifest, get_code_commit, read_manifest, write_manifest
from ashare_quant.samples import make_normal_raw
from ashare_quant.standardize import Standardizer, content_hash


def test_manifest_contains_required_fields(config, tmp_path):
    std = Standardizer()
    curated = std.standardize_daily_quotes(make_normal_raw("000001", 10), "akshare")
    path = tmp_path / "curated.parquet"
    curated.to_parquet(path, index=False)
    h = content_hash(curated)
    manifest = build_manifest(
        source="akshare",
        symbol="000001",
        start_date="2024-01-02",
        end_date="2024-02-15",
        row_count=len(curated),
        files={"curated": path},
        config=config,
        schema_version=config.schema_versions.daily_quote_version,
        content_hash_value=h,
        code_commit="abc123",
    )
    for key in ("source", "symbol", "fetch_range", "row_count", "files",
                "config_summary", "code_commit", "schema_version", "content_hash"):
        assert key in manifest
    assert manifest["files"]["curated"] != "missing"
    assert manifest["code_commit"] == "abc123"
    assert manifest["schema_version"] == config.schema_versions.daily_quote_version


def test_manifest_write_read(config, tmp_path):
    manifest = build_manifest(
        source="akshare", symbol="000001", start_date="2024-01-02",
        end_date="2024-02-15", row_count=0, files={}, config=config,
        schema_version="1.0.0", code_commit="xyz",
    )
    p = write_manifest(manifest, tmp_path / "manifest.json")
    data = read_manifest(p)
    assert data["symbol"] == "000001"
    assert data["code_commit"] == "xyz"


def test_missing_file_hash(config, tmp_path):
    manifest = build_manifest(
        source="akshare", symbol="000001", start_date="2024-01-02",
        end_date="2024-02-15", row_count=0,
        files={"nope": tmp_path / "missing.parquet"}, config=config,
        schema_version="1.0.0", code_commit="c",
    )
    assert manifest["files"]["nope"] == "missing"


def test_get_code_commit_no_git():
    """无 git 时返回 no-git，不抛异常。"""
    commit = get_code_commit()
    assert isinstance(commit, str)
    assert len(commit) > 0
