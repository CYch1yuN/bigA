"""CLI 测试。"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from ashare_quant.cli import main
from ashare_quant.samples import make_duplicate_raw, make_normal_raw
from ashare_quant.standardize import Standardizer


def test_init_config_stdout(capsys):
    rc = main(["init-config"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "quality" in out


def test_init_config_output_file(tmp_path):
    out = tmp_path / "cfg.yaml"
    rc = main(["init-config", "--output", str(out)])
    assert rc == 0
    assert out.exists()
    assert "abnormal_price_jump" in out.read_text(encoding="utf-8")


def test_standardize_cli(tmp_path):
    std = Standardizer()
    raw = make_normal_raw("000001", 8)
    raw_path = tmp_path / "raw.parquet"
    raw.to_parquet(raw_path, index=False)
    out_path = tmp_path / "curated.parquet"
    rc = main(
        ["standardize", "--input", str(raw_path), "--output", str(out_path),
         "--source", "akshare"]
    )
    assert rc == 0
    assert out_path.exists()
    df = pd.read_parquet(out_path)
    assert len(df) == 8


def test_quality_cli_pass(tmp_path):
    std = Standardizer()
    curated = std.standardize_daily_quotes(make_normal_raw("000001", 20), "akshare")
    curated_path = tmp_path / "c.parquet"
    curated.to_parquet(curated_path, index=False)
    reports_dir = tmp_path / "reports"
    rc = main(
        ["quality", "--input", str(curated_path), "--reports-dir", str(reports_dir)]
    )
    assert rc == 0
    assert (reports_dir / "quality-report.json").exists()
    assert (reports_dir / "quality-report.md").exists()


def test_quality_cli_fail_nonzero(tmp_path):
    """严重质量问题返回非零退出码。"""
    std = Standardizer()
    curated = std.standardize_daily_quotes(make_duplicate_raw("000002"), "akshare")
    curated_path = tmp_path / "c.parquet"
    curated.to_parquet(curated_path, index=False)
    reports_dir = tmp_path / "reports"
    rc = main(
        ["quality", "--input", str(curated_path), "--reports-dir", str(reports_dir)]
    )
    assert rc == 1
    data = json.loads((reports_dir / "quality-report.json").read_text(encoding="utf-8"))
    assert data["exit_code"] == 1
    assert data["has_critical"] is True


def test_manifest_cli(tmp_path):
    std = Standardizer()
    curated = std.standardize_daily_quotes(make_normal_raw("000001", 5), "akshare")
    curated_path = tmp_path / "c.parquet"
    curated.to_parquet(curated_path, index=False)
    out = tmp_path / "manifest.json"
    rc = main(
        ["manifest", "--input", str(curated_path), "--source", "akshare",
         "--symbol", "000001", "--start", "2024-01-02", "--end", "2024-02-15",
         "--files", f"curated={curated_path}", "--output", str(out)]
    )
    assert rc == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["symbol"] == "000001"
    assert data["row_count"] == 5
    assert data["files"]["curated"] != "missing"


def test_run_example_cli(tmp_path):
    reports_dir = tmp_path / "phase1_reports"
    rc = main(
        ["run-example", "--reports-dir", str(reports_dir), "--data-dir", str(tmp_path / "exdata")]
    )
    assert rc == 0
    assert (reports_dir / "quality-report.json").exists()
    assert (reports_dir / "quality-report.md").exists()
    assert (reports_dir / "reproducibility.md").exists()
    assert (reports_dir / "manifest.example.json").exists()
