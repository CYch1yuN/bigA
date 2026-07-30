"""端到端离线 CLI fetch 测试。

通过 main([...]) 调用真实 CLI 接线，mock 提供器层，
验证落盘文件、manifest 内容和 SHA-256 哈希一致性。

不访问公网；不直接测试 FetchManager 内部方法。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from ashare_quant.cli import main
from ashare_quant.constants import SOURCE_AKSHARE, SOURCE_BAOSTOCK
from ashare_quant.providers import DataProvider
from ashare_quant.storage import file_sha256


# ---- Mock 提供器（与 test_fetcher 相同设计） ----

class _MockProvider(DataProvider):
    """可控 mock 提供器。"""

    def __init__(self, name: str, behaviors: list):
        self._name = name
        self._behaviors = list(behaviors)
        self._call_count = 0

    @property
    def name(self) -> str:
        return self._name

    def fetch_daily_quotes(self, symbol, start_date, end_date):
        return self._next()

    def fetch_security_master(self):
        return self._next()

    def fetch_trade_calendar(self, start_date, end_date):
        return self._next()

    def _next(self):
        if self._call_count >= len(self._behaviors):
            raise RuntimeError(f"{self._name}: 行为序列耗尽")
        behavior = self._behaviors[self._call_count]
        self._call_count += 1
        if isinstance(behavior, Exception):
            raise behavior
        return behavior


def _mock_df(n: int = 5) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": [f"2024-01-{i:02d}" for i in range(1, n + 1)],
            "open": [10.0] * n,
            "close": [10.5] * n,
        }
    )


def _fail_list(n: int, prefix: str = "E") -> list:
    return [RuntimeError(f"{prefix}{i}") for i in range(1, n + 1)]


def _make_config(tmp_path: Path, primary: str = "akshare", max_retries: int = 3) -> Path:
    """生成测试用 YAML 配置文件。"""
    cfg = tmp_path / "test_config.yaml"
    cfg.write_text(
        f"""
project:
  name: "test"
  phase: "phase-1-data"

paths:
  data_dir: "data"
  reports_dir: "reports/phase-1"

schema:
  daily_quote_version: "1.0.0"
  security_master_version: "1.0.0"

providers:
  primary: "{primary}"
  fallback: "baostock"
  max_retries: {max_retries}
  request_interval_seconds: 0.0

quality:
  duplicate_primary_key: critical

manifest:
  content_hash_exclude_fields:
    - "fetched_at"
""",
        encoding="utf-8",
    )
    return cfg


def _patch_fetcher_providers(ak_provider, bs_provider):
    """Patch FetchManager._get_provider 以注入 mock 提供器。"""
    def _get(self, source):
        if source == SOURCE_AKSHARE:
            return ak_provider
        if source == SOURCE_BAOSTOCK:
            return bs_provider
        raise ValueError(f"未知: {source}")
    return patch("ashare_quant.fetcher.FetchManager._get_provider", _get)


# ============================================================
# 1. 重试后成功：验证 raw 文件 + manifest + SHA-256
# ============================================================

class TestCLIRetrySuccess:
    def test_retry_success_manifest_sha256(self, tmp_path):
        """重试后成功：manifest 的 SHA-256 等于 CLI 写出的 raw 文件哈希。"""
        cfg_path = _make_config(tmp_path, max_retries=2)
        data_dir = str(tmp_path / "data")

        ak = _MockProvider(SOURCE_AKSHARE, [RuntimeError("失败"), _mock_df(3)])
        bs = _MockProvider(SOURCE_BAOSTOCK, [_mock_df(3)])

        with _patch_fetcher_providers(ak, bs):
            exit_code = main([
                "fetch",
                "--symbol", "000001",
                "--start", "2024-01-01",
                "--end", "2024-01-10",
                "--config", str(cfg_path),
                "--data-dir", data_dir,
            ])

        assert exit_code == 0

        # 找到 raw parquet
        raw_dir = Path(data_dir) / "raw"
        raw_files = list(raw_dir.glob("*.parquet"))
        assert len(raw_files) == 1
        raw_file = raw_files[0]

        # 找到 manifest
        meta_dir = Path(data_dir) / "metadata"
        manifest_files = list(meta_dir.glob("*.manifest.json"))
        assert len(manifest_files) == 1
        manifest = json.loads(manifest_files[0].read_text(encoding="utf-8"))

        assert manifest["success"] is True
        assert manifest["final_source"] == SOURCE_AKSHARE
        assert manifest["row_count"] == 3
        assert manifest["total_attempts"] == 2

        # 关键：manifest 中的 SHA-256 等于实际 raw 文件哈希
        actual_hash = file_sha256(raw_file)
        assert manifest["file"]["sha256"] == actual_hash
        assert manifest["file"]["path"] == str(raw_file)


# ============================================================
# 2. 主源失败回退成功
# ============================================================

class TestCLIFallbackSuccess:
    def test_fallback_success(self, tmp_path):
        """主源全部失败后回退到 BaoStock 成功。"""
        cfg_path = _make_config(tmp_path, max_retries=1)
        data_dir = str(tmp_path / "data")

        # max_retries=1 => 2 次总尝试 per source
        ak = _MockProvider(SOURCE_AKSHARE, _fail_list(2, "AK"))
        bs = _MockProvider(SOURCE_BAOSTOCK, [_mock_df(5)])

        with _patch_fetcher_providers(ak, bs):
            exit_code = main([
                "fetch",
                "--symbol", "600000",
                "--start", "2024-01-01",
                "--end", "2024-01-10",
                "--config", str(cfg_path),
                "--data-dir", data_dir,
            ])

        assert exit_code == 0

        meta_dir = Path(data_dir) / "metadata"
        manifest_files = list(meta_dir.glob("*.manifest.json"))
        assert len(manifest_files) == 1
        manifest = json.loads(manifest_files[0].read_text(encoding="utf-8"))

        assert manifest["success"] is True
        assert manifest["final_source"] == SOURCE_BAOSTOCK
        # 2 AK + 1 BS = 3
        assert manifest["total_attempts"] == 3

        # raw 文件应以 baostock 开头
        raw_dir = Path(data_dir) / "raw"
        raw_files = list(raw_dir.glob("*.parquet"))
        assert len(raw_files) == 1
        assert raw_files[0].name.startswith("baostock_")


# ============================================================
# 3. 双源失败并生成失败 manifest
# ============================================================

class TestCLIBothFail:
    def test_both_fail_generates_failure_manifest(self, tmp_path):
        """双源全部失败时也生成 manifest，file=null，success=false。"""
        cfg_path = _make_config(tmp_path, max_retries=1)
        data_dir = str(tmp_path / "data")

        ak = _MockProvider(SOURCE_AKSHARE, _fail_list(2, "AK"))
        bs = _MockProvider(SOURCE_BAOSTOCK, _fail_list(2, "BS"))

        with _patch_fetcher_providers(ak, bs):
            exit_code = main([
                "fetch",
                "--symbol", "000001",
                "--start", "2024-01-01",
                "--end", "2024-01-10",
                "--config", str(cfg_path),
                "--data-dir", data_dir,
            ])

        assert exit_code == 1

        # 不应有 raw parquet
        raw_dir = Path(data_dir) / "raw"
        raw_files = list(raw_dir.glob("*.parquet"))
        assert len(raw_files) == 0

        # 应有失败 manifest
        meta_dir = Path(data_dir) / "metadata"
        manifest_files = list(meta_dir.glob("*.manifest.json"))
        assert len(manifest_files) == 1
        manifest = json.loads(manifest_files[0].read_text(encoding="utf-8"))

        assert manifest["success"] is False
        assert manifest["file"] is None
        assert manifest["final_source"] is None
        assert manifest["row_count"] == 0
        assert "error" in manifest
        # 2 AK + 2 BS = 4
        assert manifest["total_attempts"] == 4
        assert len(manifest["attempts"]) == 4
        assert all(not a["success"] for a in manifest["attempts"])


# ============================================================
# 4. 合法空数据成功 manifest
# ============================================================

class TestCLIEmptySuccess:
    def test_empty_data_success_manifest(self, tmp_path):
        """提供器返回空 DataFrame 时视为成功，生成 manifest。"""
        cfg_path = _make_config(tmp_path, max_retries=1)
        data_dir = str(tmp_path / "data")

        ak = _MockProvider(SOURCE_AKSHARE, [pd.DataFrame()])

        with _patch_fetcher_providers(ak, _MockProvider(SOURCE_BAOSTOCK, [])):
            exit_code = main([
                "fetch",
                "--symbol", "000001",
                "--start", "2024-01-01",
                "--end", "2024-01-10",
                "--config", str(cfg_path),
                "--data-dir", data_dir,
            ])

        assert exit_code == 0

        meta_dir = Path(data_dir) / "metadata"
        manifest_files = list(meta_dir.glob("*.manifest.json"))
        assert len(manifest_files) == 1
        manifest = json.loads(manifest_files[0].read_text(encoding="utf-8"))

        assert manifest["success"] is True
        assert manifest["row_count"] == 0
        assert manifest["final_source"] == SOURCE_AKSHARE
        assert manifest["total_attempts"] == 1


# ============================================================
# 5. --no-fallback 禁用回退
# ============================================================

class TestCLINoFallback:
    def test_no_fallback_stops_at_primary(self, tmp_path):
        """--no-fallback 时主源失败不回退，直接返回失败。"""
        cfg_path = _make_config(tmp_path, max_retries=1)
        data_dir = str(tmp_path / "data")

        ak = _MockProvider(SOURCE_AKSHARE, _fail_list(2, "AK"))
        bs = _MockProvider(SOURCE_BAOSTOCK, [_mock_df(3)])

        with _patch_fetcher_providers(ak, bs):
            exit_code = main([
                "fetch",
                "--symbol", "000001",
                "--start", "2024-01-01",
                "--end", "2024-01-10",
                "--config", str(cfg_path),
                "--data-dir", data_dir,
                "--no-fallback",
            ])

        assert exit_code == 1

        meta_dir = Path(data_dir) / "metadata"
        manifest_files = list(meta_dir.glob("*.manifest.json"))
        assert len(manifest_files) == 1
        manifest = json.loads(manifest_files[0].read_text(encoding="utf-8"))

        assert manifest["success"] is False
        # 只有 AKShare 尝试，没有 BaoStock
        sources_used = {a["source"] for a in manifest["attempts"]}
        assert sources_used == {SOURCE_AKSHARE}
        assert manifest["total_attempts"] == 2


# ============================================================
# 6. FR-01: YAML primary 被采用
# ============================================================

class TestCLICustomPrimary:
    def test_yaml_primary_baostock_used(self, tmp_path):
        """YAML primary=baostock 且不传 --source 时应使用 baostock。"""
        cfg_path = _make_config(tmp_path, primary="baostock", max_retries=1)
        data_dir = str(tmp_path / "data")

        ak = _MockProvider(SOURCE_AKSHARE, [_mock_df(3)])
        bs = _MockProvider(SOURCE_BAOSTOCK, [_mock_df(4)])

        with _patch_fetcher_providers(ak, bs):
            exit_code = main([
                "fetch",
                "--symbol", "000001",
                "--start", "2024-01-01",
                "--end", "2024-01-10",
                "--config", str(cfg_path),
                "--data-dir", data_dir,
            ])

        assert exit_code == 0

        meta_dir = Path(data_dir) / "metadata"
        manifest_files = list(meta_dir.glob("*.manifest.json"))
        assert len(manifest_files) == 1
        manifest = json.loads(manifest_files[0].read_text(encoding="utf-8"))

        # 最终数据源应为 baostock（YAML primary），而非 akshare
        assert manifest["final_source"] == SOURCE_BAOSTOCK
        assert manifest["success"] is True
        assert manifest["total_attempts"] == 1

        # raw 文件应以 baostock 开头
        raw_dir = Path(data_dir) / "raw"
        raw_files = list(raw_dir.glob("*.parquet"))
        assert len(raw_files) == 1
        assert raw_files[0].name.startswith("baostock_")
