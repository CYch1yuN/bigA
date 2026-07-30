"""抓取可靠性管理器：重试、自动回退与尝试记录。

按 YAML 配置的 ``max_retries`` 对主数据源重试；主源重试耗尽后自动回退到备用源。
每次尝试记录数据源、尝试序号、成功与否、错误信息与行数，供原始抓取清单审计。

G1 非阻断修复（Gate 1 第二次复审保留项）：
- fetch 使用 max_retries
- AKShare 失败后自动回退 BaoStock
- 抓取后自动生成包含尝试记录的 manifest
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Optional

import pandas as pd

from .config import AppConfig
from .constants import SOURCE_AKSHARE, SOURCE_BAOSTOCK
from .providers import AKShareProvider, BaoStockProvider, DataProvider


@dataclass
class FetchAttempt:
    """单次抓取尝试记录。"""

    source: str
    attempt_number: int
    success: bool
    row_count: int
    error: Optional[str] = None
    duration_seconds: float = 0.0


@dataclass
class FetchResult:
    """抓取结果：包含最终数据、实际使用的数据源和全部尝试记录。"""

    success: bool
    data: pd.DataFrame
    final_source: Optional[str]
    attempts: list[FetchAttempt] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def attempt_log(self) -> list[dict[str, Any]]:
        """尝试记录序列化为字典列表（用于 manifest）。"""
        return [
            {
                "source": a.source,
                "attempt_number": a.attempt_number,
                "success": a.success,
                "row_count": a.row_count,
                "error": a.error,
                "duration_seconds": round(a.duration_seconds, 3),
            }
            for a in self.attempts
        ]


class FetchManager:
    """抓取可靠性管理器。

    按 config.providers.max_retries 对指定数据源重试；
    主源重试耗尽后自动回退到备用源（可禁用）。
    """

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.max_retries: int = config.providers.max_retries
        self.request_interval: float = config.providers.request_interval_seconds
        self.primary_name: str = config.providers.primary
        self.fallback_name: str = config.providers.fallback

    def _get_provider(self, source: str) -> DataProvider:
        """根据名称获取提供器实例。"""
        if source == SOURCE_AKSHARE:
            return AKShareProvider()
        if source == SOURCE_BAOSTOCK:
            return BaoStockProvider()
        raise ValueError(f"未知数据源: {source}")

    def _try_with_retries(
        self,
        source: str,
        fetch_fn: Callable[[DataProvider], pd.DataFrame],
    ) -> tuple[bool, pd.DataFrame, list[FetchAttempt]]:
        """对单个数据源执行 1 + max_retries 次尝试（首次请求 + max_retries 次重试）。

        返回 (success, data, attempts)。
        """
        provider = self._get_provider(source)
        attempts: list[FetchAttempt] = []
        total_attempts = 1 + self.max_retries

        for attempt_num in range(1, total_attempts + 1):
            start_time = time.monotonic()
            try:
                data = fetch_fn(provider)
                duration = time.monotonic() - start_time
                row_count = int(len(data)) if data is not None else 0
                attempt = FetchAttempt(
                    source=source,
                    attempt_number=attempt_num,
                    success=True,
                    row_count=row_count,
                    duration_seconds=duration,
                )
                attempts.append(attempt)
                return True, data, attempts
            except Exception as e:
                duration = time.monotonic() - start_time
                attempt = FetchAttempt(
                    source=source,
                    attempt_number=attempt_num,
                    success=False,
                    row_count=0,
                    error=str(e),
                    duration_seconds=duration,
                )
                attempts.append(attempt)
                # 非最后一次尝试时等待间隔
                if attempt_num < total_attempts:
                    time.sleep(self.request_interval)

        return False, pd.DataFrame(), attempts

    def fetch_daily_quotes(
        self,
        symbol: str,
        start_date: date,
        end_date: date,
        source: Optional[str] = None,
        allow_fallback: bool = True,
    ) -> FetchResult:
        """抓取日行情，支持重试与自动回退。

        参数:
            symbol: 股票代码。
            start_date: 起始日期。
            end_date: 结束日期。
            source: 指定数据源（覆盖配置中的主源）；为 None 时使用配置主源。
            allow_fallback: 主源失败后是否自动回退到备用源。
        """
        primary = source or self.primary_name
        sources_to_try = [primary]
        if allow_fallback and primary != self.fallback_name:
            sources_to_try.append(self.fallback_name)

        all_attempts: list[FetchAttempt] = []

        def _fetch_fn(p: DataProvider) -> pd.DataFrame:
            return p.fetch_daily_quotes(symbol, start_date, end_date)

        for src in sources_to_try:
            success, data, attempts = self._try_with_retries(src, _fetch_fn)
            all_attempts.extend(attempts)
            if success:
                return FetchResult(
                    success=True,
                    data=data,
                    final_source=src,
                    attempts=all_attempts,
                )

        return FetchResult(
            success=False,
            data=pd.DataFrame(),
            final_source=None,
            attempts=all_attempts,
            error=f"所有数据源尝试失败: {sources_to_try}",
        )

    def fetch_security_master(
        self,
        source: Optional[str] = None,
        allow_fallback: bool = True,
    ) -> FetchResult:
        """抓取证券主数据，支持重试与自动回退。"""
        primary = source or self.primary_name
        sources_to_try = [primary]
        if allow_fallback and primary != self.fallback_name:
            sources_to_try.append(self.fallback_name)

        all_attempts: list[FetchAttempt] = []

        def _fetch_fn(p: DataProvider) -> pd.DataFrame:
            return p.fetch_security_master()

        for src in sources_to_try:
            success, data, attempts = self._try_with_retries(src, _fetch_fn)
            all_attempts.extend(attempts)
            if success:
                return FetchResult(
                    success=True,
                    data=data,
                    final_source=src,
                    attempts=all_attempts,
                )

        return FetchResult(
            success=False,
            data=pd.DataFrame(),
            final_source=None,
            attempts=all_attempts,
            error=f"所有数据源尝试失败: {sources_to_try}",
        )


def build_fetch_manifest(
    symbol: str,
    start_date: str,
    end_date: str,
    result: FetchResult,
    file_path: Any,
    file_hash: Optional[str],
    config: AppConfig,
    schema_version: str,
    code_commit: str,
) -> dict[str, Any]:
    """构建原始抓取清单。

    记录：请求范围、最终数据源、尝试记录（含重试与回退）、文件 SHA-256、
    配置摘要与源码提交号。失败时 file 为 null，file_hash 为 None。
    """
    from datetime import datetime, timezone

    file_info: Optional[dict[str, Any]] = None
    if file_path is not None and file_hash is not None:
        file_info = {
            "path": str(file_path),
            "sha256": file_hash,
        }

    manifest: dict[str, Any] = {
        "request": {
            "symbol": symbol,
            "start_date": start_date,
            "end_date": end_date,
        },
        "final_source": result.final_source,
        "success": result.success,
        "row_count": int(len(result.data)) if result.success else 0,
        "attempts": result.attempt_log,
        "total_attempts": len(result.attempts),
        "file": file_info,
        "config_summary": {
            "providers": {
                "primary": config.providers.primary,
                "fallback": config.providers.fallback,
                "max_retries": config.providers.max_retries,
                "request_interval_seconds": config.providers.request_interval_seconds,
            },
            "schema": {
                "daily_quote_version": config.schema_versions.daily_quote_version,
                "security_master_version": config.schema_versions.security_master_version,
            },
        },
        "code_commit": code_commit,
        "schema_version": schema_version,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    if result.error:
        manifest["error"] = result.error
    return manifest


__all__ = [
    "FetchAttempt",
    "FetchResult",
    "FetchManager",
    "build_fetch_manifest",
]
