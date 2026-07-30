"""FetchManager 重试与回退测试。

覆盖场景（Gate 1 第二次复审要求）：
1. 重试成功（首次失败，后续成功）
2. 重试耗尽（主源全部失败，无回退）
3. 主源失败后回退备用源成功
4. 双源均失败
5. 合法空数据（不抛异常）

所有测试通过子类化 mock 提供器，禁止访问公网。
"""
from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pandas as pd
import pytest

from ashare_quant.config import default_config_path, load_config
from ashare_quant.constants import SOURCE_AKSHARE, SOURCE_BAOSTOCK
from ashare_quant.fetcher import (
    FetchAttempt,
    FetchManager,
    FetchResult,
    build_fetch_manifest,
)
from ashare_quant.providers import AKShareProvider, BaoStockProvider, DataProvider
from ashare_quant.storage import file_sha256


# ---- Mock 提供器 ----

class _MockProvider(DataProvider):
    """可控 mock 提供器：按预设行为序列响应。"""

    def __init__(self, name: str, behaviors: list):
        """behaviors: 每次调用返回 DataFrame 或抛 Exception。"""
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


def _make_mock_df(n: int = 5) -> pd.DataFrame:
    """生成小型测试 DataFrame。"""
    return pd.DataFrame(
        {
            "date": [f"2024-01-{i:02d}" for i in range(1, n + 1)],
            "open": [10.0] * n,
            "close": [10.5] * n,
        }
    )


def _patch_providers(manager: FetchManager, ak_provider, bs_provider):
    """替换 FetchManager 的 _get_provider 方法。"""
    def _get(source):
        if source == SOURCE_AKSHARE:
            return ak_provider
        if source == SOURCE_BAOSTOCK:
            return bs_provider
        raise ValueError(f"未知: {source}")
    manager._get_provider = _get


@pytest.fixture
def config():
    return load_config(default_config_path())


@pytest.fixture
def fast_config(config):
    """将 request_interval 设为 0 以加速重试测试。"""
    config.providers.request_interval_seconds = 0.0
    config.providers.max_retries = 3
    return config


# ============================================================
# 1. 重试成功
# ============================================================

class TestRetrySuccess:
    def test_first_fail_second_success(self, fast_config):
        """主源首次失败、第二次成功，应返回成功数据。"""
        ak = _MockProvider(SOURCE_AKSHARE, [
            RuntimeError("网络超时"),
            _make_mock_df(3),
        ])
        bs = _MockProvider(SOURCE_BAOSTOCK, [_make_mock_df(3)])

        manager = FetchManager(fast_config)
        _patch_providers(manager, ak, bs)

        result = manager.fetch_daily_quotes("000001", date(2024, 1, 1), date(2024, 1, 10))
        assert result.success
        assert result.final_source == SOURCE_AKSHARE
        assert len(result.data) == 3
        # 应有 2 次尝试（1 失败 + 1 成功）
        assert len(result.attempts) == 2
        assert not result.attempts[0].success
        assert result.attempts[0].error == "网络超时"
        assert result.attempts[1].success
        assert result.attempts[1].row_count == 3


# ============================================================
# 2. 重试耗尽（主源全部失败，无回退）
# ============================================================

class TestRetryExhausted:
    def test_all_retries_fail_no_fallback(self, fast_config):
        """主源重试全部失败且禁用回退时，应返回失败。"""
        ak = _MockProvider(SOURCE_AKSHARE, [
            RuntimeError("失败1"),
            RuntimeError("失败2"),
            RuntimeError("失败3"),
        ])
        bs = _MockProvider(SOURCE_BAOSTOCK, [_make_mock_df(3)])

        manager = FetchManager(fast_config)
        _patch_providers(manager, ak, bs)

        result = manager.fetch_daily_quotes(
            "000001", date(2024, 1, 1), date(2024, 1, 10),
            allow_fallback=False,
        )
        assert not result.success
        assert result.final_source is None
        # 3 次尝试全部失败
        assert len(result.attempts) == 3
        assert all(not a.success for a in result.attempts)


# ============================================================
# 3. 主源失败后回退备用源成功
# ============================================================

class TestFallbackSuccess:
    def test_primary_fail_fallback_success(self, fast_config):
        """主源全部失败后回退到备用源成功。"""
        ak = _MockProvider(SOURCE_AKSHARE, [
            RuntimeError("AKShare 不可用"),
            RuntimeError("AKShare 仍不可用"),
            RuntimeError("AKShare 第三次失败"),
        ])
        bs = _MockProvider(SOURCE_BAOSTOCK, [_make_mock_df(7)])

        manager = FetchManager(fast_config)
        _patch_providers(manager, ak, bs)

        result = manager.fetch_daily_quotes("000001", date(2024, 1, 1), date(2024, 1, 10))
        assert result.success
        assert result.final_source == SOURCE_BAOSTOCK
        assert len(result.data) == 7
        # 3 次 AKShare 失败 + 1 次 BaoStock 成功
        assert len(result.attempts) == 4
        ak_attempts = [a for a in result.attempts if a.source == SOURCE_AKSHARE]
        bs_attempts = [a for a in result.attempts if a.source == SOURCE_BAOSTOCK]
        assert len(ak_attempts) == 3
        assert len(bs_attempts) == 1
        assert bs_attempts[0].success


# ============================================================
# 4. 双源均失败
# ============================================================

class TestBothSourcesFail:
    def test_both_sources_all_fail(self, fast_config):
        """主源和备用源全部失败时，应返回失败并包含全部尝试记录。"""
        ak = _MockProvider(SOURCE_AKSHARE, [
            RuntimeError("AK 失败1"),
            RuntimeError("AK 失败2"),
            RuntimeError("AK 失败3"),
        ])
        bs = _MockProvider(SOURCE_BAOSTOCK, [
            RuntimeError("BS 失败1"),
            RuntimeError("BS 失败2"),
            RuntimeError("BS 失败3"),
        ])

        manager = FetchManager(fast_config)
        _patch_providers(manager, ak, bs)

        result = manager.fetch_daily_quotes("000001", date(2024, 1, 1), date(2024, 1, 10))
        assert not result.success
        assert result.final_source is None
        assert result.error is not None
        # 3 + 3 = 6 次尝试
        assert len(result.attempts) == 6
        assert all(not a.success for a in result.attempts)


# ============================================================
# 5. 合法空数据
# ============================================================

class TestLegitimateEmpty:
    def test_empty_result_not_exception(self, fast_config):
        """提供器返回空 DataFrame（合法无数据）不应抛异常，应视为成功。"""
        ak = _MockProvider(SOURCE_AKSHARE, [pd.DataFrame()])

        manager = FetchManager(fast_config)
        _patch_providers(manager, ak, _MockProvider(SOURCE_BAOSTOCK, []))

        result = manager.fetch_daily_quotes("000001", date(2024, 1, 1), date(2024, 1, 10))
        assert result.success
        assert result.final_source == SOURCE_AKSHARE
        assert result.data.empty
        assert len(result.attempts) == 1
        assert result.attempts[0].success
        assert result.attempts[0].row_count == 0


# ============================================================
# 6. Manifest 生成
# ============================================================

class TestFetchManifest:
    def test_manifest_contains_attempt_log(self, fast_config, tmp_path):
        """原始抓取清单应包含尝试记录、最终数据源和文件哈希。"""
        ak = _MockProvider(SOURCE_AKSHARE, [
            RuntimeError("失败"),
            _make_mock_df(5),
        ])

        manager = FetchManager(fast_config)
        _patch_providers(manager, ak, _MockProvider(SOURCE_BAOSTOCK, []))

        result = manager.fetch_daily_quotes("000001", date(2024, 1, 1), date(2024, 1, 10))
        assert result.success

        # 写入临时文件并计算哈希
        test_file = tmp_path / "test_raw.parquet"
        result.data.to_parquet(test_file, index=False)
        file_hash = file_sha256(test_file)

        manifest = build_fetch_manifest(
            symbol="000001",
            start_date="2024-01-01",
            end_date="2024-01-10",
            result=result,
            file_path=test_file,
            file_hash=file_hash,
            config=fast_config,
            schema_version="1.0.0",
            code_commit="abc123",
        )

        assert manifest["final_source"] == SOURCE_AKSHARE
        assert manifest["success"] is True
        assert manifest["row_count"] == 5
        assert manifest["file"]["sha256"] == file_hash
        assert manifest["code_commit"] == "abc123"
        assert manifest["schema_version"] == "1.0.0"
        assert len(manifest["attempts"]) == 2
        assert manifest["attempts"][0]["success"] is False
        assert manifest["attempts"][1]["success"] is True
        assert manifest["attempts"][0]["source"] == SOURCE_AKSHARE
        assert manifest["attempts"][1]["source"] == SOURCE_AKSHARE
        assert "max_retries" in manifest["config_summary"]["providers"]
        assert "primary" in manifest["config_summary"]["providers"]
        assert "fallback" in manifest["config_summary"]["providers"]


# ============================================================
# 7. 指定数据源不回退
# ============================================================

class TestSpecifiedSource:
    def test_specified_source_no_fallback(self, fast_config):
        """显式指定 baostock 时不回退到 akshare。"""
        bs = _MockProvider(SOURCE_BAOSTOCK, [_make_mock_df(4)])
        ak = _MockProvider(SOURCE_AKSHARE, [_make_mock_df(4)])

        manager = FetchManager(fast_config)
        _patch_providers(manager, ak, bs)

        result = manager.fetch_daily_quotes(
            "000001", date(2024, 1, 1), date(2024, 1, 10),
            source=SOURCE_BAOSTOCK,
        )
        assert result.success
        assert result.final_source == SOURCE_BAOSTOCK
        assert len(result.attempts) == 1
