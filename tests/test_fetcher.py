"""FetchManager 重试与回退测试。

覆盖场景（Gate 1 抓取可靠性补丁审核要求）：
1. 重试成功（首次失败，后续成功）
2. 重试耗尽（主源全部失败，无回退）
3. 主源失败后回退备用源成功
4. 双源均失败
5. 合法空数据（不抛异常）
6. Manifest 生成（含 total_attempts 字段）
7. 指定数据源不回退
8. FR-02: max_retries=0 仍执行一次初始请求
9. FR-02: max_retries=2 最多执行三次
10. FR-03: 非法配置加载失败
11. 失败 manifest (file=null, success=false)

所有测试通过子类化 mock 提供器，禁止访问公网。
"""
from __future__ import annotations

from datetime import date

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
from ashare_quant.providers import DataProvider
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
    """将 request_interval 设为 0 以加速重试测试；max_retries=3 即 4 次总尝试。"""
    config.providers.request_interval_seconds = 0.0
    config.providers.max_retries = 3
    return config


def _fail(n: int, prefix: str = "失败") -> list:
    """生成 n 个 RuntimeError 行为。"""
    return [RuntimeError(f"{prefix}{i}") for i in range(1, n + 1)]


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
        """主源重试全部失败且禁用回退时，应返回失败。

        max_retries=3 => 1 + 3 = 4 次总尝试。
        """
        ak = _MockProvider(SOURCE_AKSHARE, _fail(4, "AK"))
        bs = _MockProvider(SOURCE_BAOSTOCK, [_make_mock_df(3)])

        manager = FetchManager(fast_config)
        _patch_providers(manager, ak, bs)

        result = manager.fetch_daily_quotes(
            "000001", date(2024, 1, 1), date(2024, 1, 10),
            allow_fallback=False,
        )
        assert not result.success
        assert result.final_source is None
        # 4 次尝试全部失败
        assert len(result.attempts) == 4
        assert all(not a.success for a in result.attempts)


# ============================================================
# 3. 主源失败后回退备用源成功
# ============================================================

class TestFallbackSuccess:
    def test_primary_fail_fallback_success(self, fast_config):
        """主源全部失败后回退到备用源成功。

        max_retries=3 => AKShare 4 次失败 + BaoStock 1 次成功 = 5 次总尝试。
        """
        ak = _MockProvider(SOURCE_AKSHARE, _fail(4, "AK"))
        bs = _MockProvider(SOURCE_BAOSTOCK, [_make_mock_df(7)])

        manager = FetchManager(fast_config)
        _patch_providers(manager, ak, bs)

        result = manager.fetch_daily_quotes("000001", date(2024, 1, 1), date(2024, 1, 10))
        assert result.success
        assert result.final_source == SOURCE_BAOSTOCK
        assert len(result.data) == 7
        # 4 次 AKShare 失败 + 1 次 BaoStock 成功
        assert len(result.attempts) == 5
        ak_attempts = [a for a in result.attempts if a.source == SOURCE_AKSHARE]
        bs_attempts = [a for a in result.attempts if a.source == SOURCE_BAOSTOCK]
        assert len(ak_attempts) == 4
        assert len(bs_attempts) == 1
        assert bs_attempts[0].success


# ============================================================
# 4. 双源均失败
# ============================================================

class TestBothSourcesFail:
    def test_both_sources_all_fail(self, fast_config):
        """主源和备用源全部失败时，应返回失败并包含全部尝试记录。

        max_retries=3 => 4 + 4 = 8 次总尝试。
        """
        ak = _MockProvider(SOURCE_AKSHARE, _fail(4, "AK"))
        bs = _MockProvider(SOURCE_BAOSTOCK, _fail(4, "BS"))

        manager = FetchManager(fast_config)
        _patch_providers(manager, ak, bs)

        result = manager.fetch_daily_quotes("000001", date(2024, 1, 1), date(2024, 1, 10))
        assert not result.success
        assert result.final_source is None
        assert result.error is not None
        # 4 + 4 = 8 次尝试
        assert len(result.attempts) == 8
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
# 6. Manifest 生成（含 total_attempts）
# ============================================================

class TestFetchManifest:
    def test_success_manifest(self, fast_config, tmp_path):
        """成功 manifest 应包含尝试记录、最终数据源、文件哈希和 total_attempts。"""
        ak = _MockProvider(SOURCE_AKSHARE, [
            RuntimeError("失败"),
            _make_mock_df(5),
        ])

        manager = FetchManager(fast_config)
        _patch_providers(manager, ak, _MockProvider(SOURCE_BAOSTOCK, []))

        result = manager.fetch_daily_quotes("000001", date(2024, 1, 1), date(2024, 1, 10))
        assert result.success

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
        assert manifest["total_attempts"] == 2
        assert len(manifest["attempts"]) == 2
        assert manifest["attempts"][0]["success"] is False
        assert manifest["attempts"][1]["success"] is True
        assert manifest["config_summary"]["providers"]["max_retries"] == 3

    def test_failure_manifest_file_null(self, fast_config):
        """失败 manifest 应 file=null、success=false、包含 error 和全部尝试。"""
        ak = _MockProvider(SOURCE_AKSHARE, _fail(4, "AK"))
        bs = _MockProvider(SOURCE_BAOSTOCK, _fail(4, "BS"))

        manager = FetchManager(fast_config)
        _patch_providers(manager, ak, bs)

        result = manager.fetch_daily_quotes("000001", date(2024, 1, 1), date(2024, 1, 10))
        assert not result.success

        manifest = build_fetch_manifest(
            symbol="000001",
            start_date="2024-01-01",
            end_date="2024-01-10",
            result=result,
            file_path=None,
            file_hash=None,
            config=fast_config,
            schema_version="1.0.0",
            code_commit="abc123",
        )

        assert manifest["success"] is False
        assert manifest["file"] is None
        assert manifest["final_source"] is None
        assert manifest["row_count"] == 0
        assert manifest["total_attempts"] == 8
        assert "error" in manifest
        assert len(manifest["attempts"]) == 8


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


# ============================================================
# 8. FR-02: max_retries=0 仍执行一次初始请求
# ============================================================

class TestMaxRetriesZero:
    def test_zero_retries_still_one_attempt(self, config):
        """max_retries=0 时仍应执行一次初始请求。"""
        config.providers.max_retries = 0
        config.providers.request_interval_seconds = 0.0

        ak = _MockProvider(SOURCE_AKSHARE, [_make_mock_df(2)])
        manager = FetchManager(config)
        _patch_providers(manager, ak, _MockProvider(SOURCE_BAOSTOCK, []))

        result = manager.fetch_daily_quotes("000001", date(2024, 1, 1), date(2024, 1, 10))
        assert result.success
        assert len(result.attempts) == 1

    def test_zero_retries_fail_no_retry(self, config):
        """max_retries=0 时首次失败不重试。"""
        config.providers.max_retries = 0
        config.providers.request_interval_seconds = 0.0

        ak = _MockProvider(SOURCE_AKSHARE, [RuntimeError("唯一一次失败")])
        bs = _MockProvider(SOURCE_BAOSTOCK, [_make_mock_df(2)])
        manager = FetchManager(config)
        _patch_providers(manager, ak, bs)

        result = manager.fetch_daily_quotes("000001", date(2024, 1, 1), date(2024, 1, 10))
        # AK 1 次失败 + BS 1 次成功
        assert result.success
        assert result.final_source == SOURCE_BAOSTOCK
        assert len(result.attempts) == 2


# ============================================================
# 9. FR-02: max_retries=2 最多执行三次
# ============================================================

class TestMaxRetriesTwo:
    def test_max_three_attempts(self, config):
        """max_retries=2 => 最多 3 次尝试。"""
        config.providers.max_retries = 2
        config.providers.request_interval_seconds = 0.0

        ak = _MockProvider(SOURCE_AKSHARE, _fail(3, "AK"))
        bs = _MockProvider(SOURCE_BAOSTOCK, [_make_mock_df(2)])
        manager = FetchManager(config)
        _patch_providers(manager, ak, bs)

        result = manager.fetch_daily_quotes("000001", date(2024, 1, 1), date(2024, 1, 10))
        assert result.success
        assert result.final_source == SOURCE_BAOSTOCK
        # 3 AK + 1 BS = 4
        assert len(result.attempts) == 4
        ak_count = sum(1 for a in result.attempts if a.source == SOURCE_AKSHARE)
        assert ak_count == 3


# ============================================================
# 10. FR-03: 非法配置加载失败
# ============================================================

class TestConfigValidation:
    def test_negative_max_retries_rejected(self, tmp_path):
        """max_retries < 0 应被 Pydantic 拒绝。"""
        yaml_text = """
providers:
  primary: akshare
  fallback: baostock
  max_retries: -1
  request_interval_seconds: 1.0
"""
        cfg_path = tmp_path / "bad.yaml"
        cfg_path.write_text(yaml_text, encoding="utf-8")
        with pytest.raises(Exception):
            load_config(cfg_path)

    def test_negative_interval_rejected(self, tmp_path):
        """request_interval_seconds < 0 应被 Pydantic 拒绝。"""
        yaml_text = """
providers:
  primary: akshare
  fallback: baostock
  max_retries: 3
  request_interval_seconds: -0.5
"""
        cfg_path = tmp_path / "bad.yaml"
        cfg_path.write_text(yaml_text, encoding="utf-8")
        with pytest.raises(Exception):
            load_config(cfg_path)
