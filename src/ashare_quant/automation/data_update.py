"""FR-20 真实数据更新流程：可注入，但不可伪造。

这个模块是 Phase 4 自动化与 Phase 1 数据基建之间的**承重梁**。
在它出现之前，``market_data`` 步骤只会去 curated 层"捡现成的"——
数据是谁抓的、什么时候抓的、抓失败了会怎样，全部是别人的事。
FR-20 要求把这段真空补上：让自动化流水线自己拥有一条
**能抓、会重试、会回退、留得下痕迹**的数据链路。

复用而非重造
------------
本模块**一行抓取逻辑都没有自己写**，全部复用 Phase 1 已通过 Gate 的组件：

======================  ====================================================
组件                     职责
======================  ====================================================
:class:`FetchManager`   重试 + 主源(AKShare) 失败自动回退备源(BaoStock)
:class:`DataProvider`   数据源适配（真实实现 / 测试注入实现）
:class:`Standardizer`   原始 -> curated 规范 schema
:class:`Storage`        raw / curated / metadata 三层 Parquet 落盘
``build_manifest``      数据版本清单（含 SHA-256、配置摘要、代码提交号）
``file_sha256``         逐文件内容指纹
:class:`QualityChecker` 质量闸门（由 ``daily.py`` 在下一步调用）
======================  ====================================================

每日链路
--------
``更新 -> 校验 -> 落盘 -> manifest/SHA256 -> 质量闸门 -> 信号 -> 模拟账户``

前四段在本模块内完成，后三段由 :mod:`ashare_quant.automation.daily` 承接。
校验刻意排在落盘 curated **之前**：schema 不合格的数据连进 curated 层的
资格都没有，免得下游误当成"已治理"数据消费。

失败语义
--------
============================  =========================================
情形                            结果
============================  =========================================
主源失败、备源成功                继续（``fallback_used=True`` 留痕）
部分标的失败、至少一个成功         继续（失败标的进 ``outcomes`` 明细）
全部标的双源失败                  抛 ``DataUnavailableError``
schema 校验不通过（严格模式）      抛 ``DataUpdateFailedError`` -> FAILED
============================  =========================================

``DataUnavailableError`` 由 runner 映射为 ``SKIPPED_DATA_UNAVAILABLE``；
若配置 ``data.allow_skip_when_unavailable=false``，``daily.py`` 会把它
升格为 ``DataUpdateFailedError`` -> ``FAILED``。

诚实性契约（与 :mod:`.datasource` 一脉相承）
-------------------------------------------
注入 ``provider_factory`` 或 ``fetch_manager`` 后，``online`` **默认转 False**、
``synthetic`` **默认转 True**。也就是说：测试无法在不显式声明的前提下
让报告写出"线上抓取"四个字。注入点是为了离线跑通与线上完全相同的
重试/回退/清单代码路径，不是给生产留一条伪造数据源的后门。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Optional, Protocol, Sequence

import pandas as pd

from ..config import AppConfig
from ..constants import (
    DAILY_QUOTE_SCHEMA_VERSION,
    LAYER_CURATED,
    LAYER_METADATA,
    LAYER_RAW,
    SECURITY_MASTER_SCHEMA_VERSION,
)
from ..fetcher import FetchManager, FetchResult, build_fetch_manifest
from ..manifest import build_manifest, get_code_commit, write_manifest
from ..providers import DataProvider
from ..standardize import Standardizer, content_hash
from ..storage import Storage, file_sha256
from .datasource import (
    QUOTE_COLUMNS,
    DataUnavailableError,
    DataUpdateFailedError,
    MarketDataBundle,
    normalize_quotes,
)

__all__ = [
    "SymbolUpdateOutcome",
    "DataUpdateResult",
    "DataUpdater",
    "FetchManagerDataUpdater",
    "AutoUpdatingDataSource",
    "build_updating_data_source",
]


# ---------------------------------------------------------------------- #
# 结果对象
# ---------------------------------------------------------------------- #


@dataclass
class SymbolUpdateOutcome:
    """单个标的的更新结果——审计级明细，成功失败一视同仁地留痕。"""

    symbol: str
    success: bool
    final_source: Optional[str] = None
    row_count: int = 0
    attempts: list[dict[str, Any]] = field(default_factory=list)
    fallback_used: bool = False
    raw_path: Optional[str] = None
    raw_sha256: Optional[str] = None
    curated_path: Optional[str] = None
    curated_sha256: Optional[str] = None
    manifest_path: Optional[str] = None
    content_hash: Optional[str] = None
    error: Optional[str] = None

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    @property
    def sources_tried(self) -> list[str]:
        """按首次出现顺序去重的尝试源列表。"""
        seen: list[str] = []
        for a in self.attempts:
            src = str(a.get("source", ""))
            if src and src not in seen:
                seen.append(src)
        return seen

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "success": self.success,
            "final_source": self.final_source,
            "row_count": int(self.row_count),
            "attempt_count": self.attempt_count,
            "sources_tried": self.sources_tried,
            "attempts": list(self.attempts),
            "fallback_used": bool(self.fallback_used),
            "raw_path": self.raw_path,
            "raw_sha256": self.raw_sha256,
            "curated_path": self.curated_path,
            "curated_sha256": self.curated_sha256,
            "manifest_path": self.manifest_path,
            "content_hash": self.content_hash,
            "error": self.error,
        }


@dataclass
class DataUpdateResult:
    """一次数据更新的完整产出与出处说明。"""

    updater: str
    as_of: date
    start: date
    end: date
    quotes: pd.DataFrame
    online: bool
    synthetic: bool = False
    outcomes: list[SymbolUpdateOutcome] = field(default_factory=list)
    security_master: Optional[pd.DataFrame] = None
    benchmark: Optional[pd.DataFrame] = None
    notes: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    @property
    def succeeded_symbols(self) -> list[str]:
        return [o.symbol for o in self.outcomes if o.success]

    @property
    def failed_symbols(self) -> list[str]:
        return [o.symbol for o in self.outcomes if not o.success]

    @property
    def fallback_symbols(self) -> list[str]:
        return [o.symbol for o in self.outcomes if o.fallback_used]

    @property
    def any_success(self) -> bool:
        return any(o.success for o in self.outcomes)

    @property
    def all_failed(self) -> bool:
        return bool(self.outcomes) and not self.any_success

    @property
    def row_count(self) -> int:
        return 0 if self.quotes is None else int(len(self.quotes))

    @property
    def manifest_paths(self) -> list[str]:
        return [o.manifest_path for o in self.outcomes if o.manifest_path]

    def to_dict(self) -> dict[str, Any]:
        """审计摘要——报告与日志原样透出，不得美化。"""
        return {
            "updater": self.updater,
            "as_of": self.as_of.isoformat(),
            "request_start": self.start.isoformat(),
            "request_end": self.end.isoformat(),
            "online": bool(self.online),
            "synthetic": bool(self.synthetic),
            "row_count": self.row_count,
            "symbols_requested": len(self.outcomes),
            "symbols_succeeded": len(self.succeeded_symbols),
            "symbols_failed": len(self.failed_symbols),
            "failed_symbols": self.failed_symbols,
            "fallback_symbols": self.fallback_symbols,
            "security_master_rows": (
                0 if self.security_master is None else int(len(self.security_master))
            ),
            "benchmark_rows": (
                0 if self.benchmark is None else int(len(self.benchmark))
            ),
            "manifests": self.manifest_paths,
            "outcomes": [o.to_dict() for o in self.outcomes],
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------- #
# 协议
# ---------------------------------------------------------------------- #


class DataUpdater(Protocol):
    """数据更新器协议：管线只依赖它，便于离线注入。"""

    name: str

    def update(
        self,
        *,
        symbols: Sequence[str],
        start: date,
        end: date,
        as_of: date,
    ) -> DataUpdateResult:
        """抓取并落盘 ``[start, end]`` 区间数据。

        Raises:
            DataUnavailableError: 全部标的双源失败。
            DataUpdateFailedError: schema 校验不通过（严格模式）。
        """
        ...


# ---------------------------------------------------------------------- #
# 正式实现
# ---------------------------------------------------------------------- #


class FetchManagerDataUpdater:
    """正式数据更新器：FetchManager -> Standardizer -> Storage -> Manifest。

    生产环境下 ``provider_factory`` 留空，:class:`FetchManager` 自行构造
    真实的 :class:`AKShareProvider`（主）与 :class:`BaoStockProvider`（备）。
    测试环境注入 fake provider，走**完全相同**的代码路径，但出处标记
    自动降级为 ``online=False / synthetic=True``。
    """

    name = "fetch-manager"

    def __init__(
        self,
        app_config: AppConfig,
        *,
        data_dir: Path | str | None = None,
        storage: Optional[Storage] = None,
        provider_factory: Optional[Callable[[str], DataProvider]] = None,
        fetch_manager: Optional[FetchManager] = None,
        benchmark_symbols: Sequence[str] = (),
        update_security_master: bool = True,
        write_manifests: bool = True,
        strict_schema: bool = True,
        online: Optional[bool] = None,
        synthetic: Optional[bool] = None,
        code_commit: Optional[str] = None,
    ) -> None:
        """
        Args:
            app_config: Phase 1 应用配置（主备源、重试、schema 版本、清单规则）。
            data_dir: 落盘根目录；``storage`` 未提供时据此构造 :class:`Storage`。
            storage: 直接注入的存储器（优先于 ``data_dir``）。
            provider_factory: ``name -> DataProvider`` 工厂，仅用于离线测试。
            fetch_manager: 直接注入的抓取管理器（优先于 ``provider_factory``）。
            benchmark_symbols: 基准标的，单独成表，best-effort。
            update_security_master: 是否顺带更新证券主数据（best-effort）。
            write_manifests: 是否写数据版本清单（审计默认开启）。
            strict_schema: curated 校验不通过时是否直接判 ``FAILED``。
            online: 出处标记覆盖；``None`` 时按"是否被注入"自动判定。
            synthetic: 合成标记覆盖；``None`` 时按"是否被注入"自动判定。
            code_commit: 代码提交号；``None`` 时调用 ``get_code_commit()``。
        """
        self._app_config = app_config
        if storage is not None:
            self._storage = storage
        else:
            root = data_dir if data_dir is not None else app_config.paths.data_dir
            self._storage = Storage(root)

        injected = fetch_manager is not None or provider_factory is not None
        self._injected = injected
        self._fetch = fetch_manager or FetchManager(
            app_config, provider_factory=provider_factory
        )
        self._standardizer = Standardizer()
        self._benchmark_symbols = [str(s) for s in benchmark_symbols]
        self._update_security_master = bool(update_security_master)
        self._write_manifests = bool(write_manifests)
        self._strict_schema = bool(strict_schema)

        # 诚实性默认值：被注入即视为非线上、合成，除非调用方显式声明。
        self.online = (not injected) if online is None else bool(online)
        self.synthetic = injected if synthetic is None else bool(synthetic)
        self._provenance_overridden = injected and (
            online is not None or synthetic is not None
        )
        self._code_commit = code_commit if code_commit is not None else get_code_commit()

    # ------------------------------------------------------------------ #
    # 公开入口
    # ------------------------------------------------------------------ #

    def update(
        self,
        *,
        symbols: Sequence[str],
        start: date,
        end: date,
        as_of: date,
    ) -> DataUpdateResult:
        wanted = [str(s) for s in symbols]
        if not wanted:
            raise DataUnavailableError(
                "数据更新器未收到任何标的；请检查 data.symbols 配置"
            )

        outcomes: list[SymbolUpdateOutcome] = []
        frames: list[pd.DataFrame] = []
        for symbol in wanted:
            outcome, curated = self._update_symbol(symbol, start, end)
            outcomes.append(outcome)
            if curated is not None and not curated.empty:
                frames.append(curated)

        if not frames:
            detail = "; ".join(
                f"{o.symbol}: {o.error or '未知错误'}" for o in outcomes
            )
            raise DataUnavailableError(
                f"全部 {len(wanted)} 个标的在主源({self._fetch.primary_name})与"
                f"备用源({self._fetch.fallback_name})上均抓取失败；"
                f"本次运行没有可信输入。明细: {detail}"
            )

        quotes = normalize_quotes(pd.concat(frames, ignore_index=True))

        notes: list[str] = [f"数据更新器: {self.name}"]
        if self._injected:
            notes.append(
                "抓取提供器由调用方注入（离线测试路径），"
                "重试/回退/清单逻辑与线上一致"
            )
        if self._provenance_overridden:
            notes.append("出处标记由调用方显式声明，未使用自动判定值")
        failed = [o.symbol for o in outcomes if not o.success]
        if failed:
            notes.append(f"部分标的抓取失败（已跳过，未伪造）: {failed}")
        fallback = [o.symbol for o in outcomes if o.fallback_used]
        if fallback:
            notes.append(
                f"主源失败后由备用源 {self._fetch.fallback_name} 接管: {fallback}"
            )

        master = self._maybe_update_security_master(notes)
        benchmark = self._maybe_update_benchmark(start, end, notes)

        return DataUpdateResult(
            updater=self.name,
            as_of=as_of,
            start=start,
            end=end,
            quotes=quotes,
            online=self.online,
            synthetic=self.synthetic,
            outcomes=outcomes,
            security_master=master,
            benchmark=benchmark,
            notes=notes,
        )

    # ------------------------------------------------------------------ #
    # 单标的链路：抓取 -> 落 raw -> 标准化 -> 校验 -> 落 curated -> 清单
    # ------------------------------------------------------------------ #

    def _update_symbol(
        self, symbol: str, start: date, end: date
    ) -> tuple[SymbolUpdateOutcome, Optional[pd.DataFrame]]:
        result: FetchResult = self._fetch.fetch_daily_quotes(
            symbol=symbol,
            start_date=start,
            end_date=end,
            allow_fallback=True,
        )
        attempts = result.attempt_log
        source = result.final_source
        fallback_used = bool(source and source != self._fetch.primary_name)

        if not result.success:
            # 失败也要留痕：写一份 file 为 null 的抓取清单，证明"我们确实试过"。
            self._write_fetch_manifest(symbol, start, end, result, None, None)
            return (
                SymbolUpdateOutcome(
                    symbol=symbol,
                    success=False,
                    attempts=attempts,
                    error=result.error or "主源与备用源均抓取失败",
                ),
                None,
            )

        assert source is not None  # success 时 final_source 必然有值

        # 1) 原始层落盘：未经加工的证据先固定下来
        raw_name = f"{source}_{symbol}_{start.isoformat()}_{end.isoformat()}.parquet"
        raw_path = self._storage.write_generic_parquet(
            result.data, raw_name, layer=LAYER_RAW
        )
        raw_hash = file_sha256(raw_path)

        # 2) 标准化
        curated = self._standardizer.standardize_daily_quotes(result.data, source)

        # 3) 校验（先于 curated 落盘：不合格的数据没有进治理层的资格）
        problem = self._validate_curated(curated, symbol, source)
        if problem is not None:
            if self._strict_schema:
                raise DataUpdateFailedError(
                    f"标的 {symbol} 标准化后未通过 schema 校验：{problem}；"
                    f"strict_schema=true，按 fail-closed 判为失败而非静默跳过"
                )
            return (
                SymbolUpdateOutcome(
                    symbol=symbol,
                    success=False,
                    final_source=source,
                    attempts=attempts,
                    fallback_used=fallback_used,
                    raw_path=str(raw_path),
                    raw_sha256=raw_hash,
                    error=problem,
                ),
                None,
            )

        # 4) curated 层落盘 + SHA-256
        curated_name = (
            f"daily_quotes_{symbol}_{start.isoformat()}_{end.isoformat()}.parquet"
        )
        curated_path = self._storage.write_daily_quotes_parquet(
            curated, curated_name, layer=LAYER_CURATED
        )
        curated_hash = file_sha256(curated_path)
        chash = content_hash(
            curated, self._app_config.manifest.content_hash_exclude_fields
        )

        # 5) 数据版本清单
        manifest_path = self._write_data_manifest(
            symbol=symbol,
            source=source,
            start=start,
            end=end,
            curated=curated,
            raw_path=raw_path,
            curated_path=curated_path,
            content_hash_value=chash,
            result=result,
        )
        self._write_fetch_manifest(symbol, start, end, result, raw_path, raw_hash)

        return (
            SymbolUpdateOutcome(
                symbol=symbol,
                success=True,
                final_source=source,
                row_count=int(len(curated)),
                attempts=attempts,
                fallback_used=fallback_used,
                raw_path=str(raw_path),
                raw_sha256=raw_hash,
                curated_path=str(curated_path),
                curated_sha256=curated_hash,
                manifest_path=str(manifest_path) if manifest_path else None,
                content_hash=chash,
            ),
            curated,
        )

    # ------------------------------------------------------------------ #
    def _validate_curated(
        self, curated: pd.DataFrame, symbol: str, source: str
    ) -> Optional[str]:
        """curated 数据校验；通过返回 ``None``，否则返回问题描述。"""
        if curated is None or curated.empty:
            return f"标准化后为空表（来源 {source}）"
        missing = [c for c in QUOTE_COLUMNS if c not in curated.columns]
        if missing:
            return f"缺少必需列 {missing}（来源 {source}）"
        if curated["close_qfq"].isna().all():
            return f"close_qfq 全为空（来源 {source}）"
        if curated["trade_date"].isna().any():
            return f"存在空 trade_date（来源 {source}）"
        dup = curated.duplicated(subset=["symbol", "trade_date"]).sum()
        if dup:
            return f"存在 {int(dup)} 条主键重复记录（来源 {source}）"
        return None

    # ------------------------------------------------------------------ #
    def _write_data_manifest(
        self,
        *,
        symbol: str,
        source: str,
        start: date,
        end: date,
        curated: pd.DataFrame,
        raw_path: Path,
        curated_path: Path,
        content_hash_value: str,
        result: FetchResult,
    ) -> Optional[Path]:
        if not self._write_manifests:
            return None
        manifest = build_manifest(
            source=source,
            symbol=symbol,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            row_count=int(len(curated)),
            files={"raw": raw_path, "curated": curated_path},
            config=self._app_config,
            schema_version=DAILY_QUOTE_SCHEMA_VERSION,
            content_hash_value=content_hash_value,
            code_commit=self._code_commit,
        )
        # 把重试/回退轨迹并入数据清单，避免审计时要对着两份文件拼故事。
        manifest["fetch"] = {
            "final_source": result.final_source,
            "primary": self._fetch.primary_name,
            "fallback": self._fetch.fallback_name,
            "fallback_used": bool(
                result.final_source and result.final_source != self._fetch.primary_name
            ),
            "total_attempts": len(result.attempts),
            "attempts": result.attempt_log,
        }
        manifest["provenance"] = {
            "online": bool(self.online),
            "synthetic": bool(self.synthetic),
            "provider_injected": bool(self._injected),
        }
        name = f"manifest_{symbol}_{start.isoformat()}_{end.isoformat()}.json"
        return write_manifest(manifest, self._storage.metadata_dir / name)

    def _write_fetch_manifest(
        self,
        symbol: str,
        start: date,
        end: date,
        result: FetchResult,
        raw_path: Optional[Path],
        raw_hash: Optional[str],
    ) -> Optional[Path]:
        """写原始抓取清单（成功与失败都写，失败时 file 为 null）。"""
        if not self._write_manifests:
            return None
        payload = build_fetch_manifest(
            symbol=symbol,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            result=result,
            file_path=raw_path,
            file_hash=raw_hash,
            config=self._app_config,
            schema_version=DAILY_QUOTE_SCHEMA_VERSION,
            code_commit=self._code_commit,
        )
        name = f"fetch_{symbol}_{start.isoformat()}_{end.isoformat()}.json"
        return write_manifest(payload, self._storage.metadata_dir / name)

    # ------------------------------------------------------------------ #
    def _maybe_update_security_master(
        self, notes: list[str]
    ) -> Optional[pd.DataFrame]:
        """更新证券主数据（best-effort：失败只记一笔，不拖垮当日运行）。"""
        if not self._update_security_master:
            return None
        try:
            result = self._fetch.fetch_security_master(allow_fallback=True)
        except Exception as exc:  # noqa: BLE001 - 主数据是可选增强项
            notes.append(f"证券主数据更新异常（已跳过）: {exc}")
            return None
        if not result.success or result.data is None or result.data.empty:
            notes.append(
                f"证券主数据更新失败（已跳过，质量检查将缺少主数据维度）: "
                f"{result.error or '空结果'}"
            )
            return None
        try:
            master = self._standardizer.standardize_security_master(
                result.data, result.final_source or self._fetch.primary_name
            )
        except Exception as exc:  # noqa: BLE001
            notes.append(f"证券主数据标准化失败（已跳过）: {exc}")
            return None
        if master is None or master.empty:
            notes.append("证券主数据标准化后为空（已跳过）")
            return None
        try:
            path = self._storage.write_security_master_parquet(
                master, "security_master.parquet", layer=LAYER_METADATA
            )
            if self._write_manifests:
                write_manifest(
                    {
                        "source": result.final_source,
                        "row_count": int(len(master)),
                        "file": {"path": str(path), "sha256": file_sha256(path)},
                        "schema_version": SECURITY_MASTER_SCHEMA_VERSION,
                        "code_commit": self._code_commit,
                        "attempts": result.attempt_log,
                    },
                    self._storage.metadata_dir / "manifest_security_master.json",
                )
        except Exception as exc:  # noqa: BLE001 - 落盘失败不应丢掉已抓到的数据
            notes.append(f"证券主数据落盘失败（内存数据仍可用）: {exc}")
        return master

    def _maybe_update_benchmark(
        self, start: date, end: date, notes: list[str]
    ) -> Optional[pd.DataFrame]:
        """更新基准行情（best-effort）。"""
        if not self._benchmark_symbols:
            return None
        frames: list[pd.DataFrame] = []
        failed: list[str] = []
        for symbol in self._benchmark_symbols:
            try:
                outcome, curated = self._update_symbol(symbol, start, end)
            except DataUpdateFailedError as exc:
                failed.append(f"{symbol}({exc})")
                continue
            if outcome.success and curated is not None and not curated.empty:
                frames.append(curated)
            else:
                failed.append(symbol)
        if failed:
            notes.append(f"基准行情部分失败（已跳过）: {failed}")
        if not frames:
            return None
        return normalize_quotes(pd.concat(frames, ignore_index=True))


# ---------------------------------------------------------------------- #
# 管线适配器
# ---------------------------------------------------------------------- #


class AutoUpdatingDataSource:
    """把 :class:`DataUpdater` 接到 Phase 4 管线的 ``market_data`` 步骤。

    管线只认 :class:`MarketDataSource` 协议；本类负责在 ``load()`` 里
    先跑一遍数据更新，再把结果包装成 :class:`MarketDataBundle`。
    与 :class:`LocalParquetDataSource` 的分工是清晰的：

    - ``LocalParquetDataSource``：**永远离线**，只消费别人抓好的数据。
    - ``AutoUpdatingDataSource``：自己抓，出处标记随更新器如实透出。
    """

    def __init__(
        self,
        updater: DataUpdater,
        *,
        enforce_coverage: bool = True,
    ) -> None:
        self._updater = updater
        self.name = f"auto-update:{getattr(updater, 'name', 'unknown')}"
        self.enforce_coverage = bool(enforce_coverage)
        #: 最近一次更新结果，供 ``daily.py`` 写入审计产物。
        self.last_result: Optional[DataUpdateResult] = None

    def load(
        self,
        *,
        symbols: Sequence[str],
        start: date,
        end: date,
        as_of: date,
    ) -> MarketDataBundle:
        result = self._updater.update(
            symbols=symbols, start=start, end=end, as_of=as_of
        )
        self.last_result = result

        if result.quotes is None or result.quotes.empty:
            raise DataUnavailableError(
                f"数据更新器 {result.updater} 返回空行情（区间 {start}~{end}）"
            )

        notes = list(result.notes)
        notes.append(
            f"成功 {len(result.succeeded_symbols)} / 请求 {len(result.outcomes)} 个标的"
        )
        if result.manifest_paths:
            notes.append(f"已生成 {len(result.manifest_paths)} 份数据版本清单")

        bundle = MarketDataBundle(
            quotes=normalize_quotes(result.quotes),
            source=self.name,
            online=result.online,
            synthetic=result.synthetic,
            security_master=result.security_master,
            benchmark=result.benchmark,
            loaded_at=datetime.now(),
            notes=notes,
        )

        if self.enforce_coverage and not bundle.covers(as_of):
            raise DataUnavailableError(
                f"更新后的行情仍未覆盖业务日 {as_of}"
                f"（最新 {bundle.latest_date()}）；"
                f"数据源尚未就绪，本次跳过而非使用陈旧数据"
            )
        return bundle


def build_updating_data_source(
    app_config: AppConfig,
    *,
    data_dir: Path | str | None = None,
    provider_factory: Optional[Callable[[str], DataProvider]] = None,
    benchmark_symbols: Sequence[str] = (),
    enforce_coverage: bool = True,
    **updater_kwargs: Any,
) -> AutoUpdatingDataSource:
    """便捷工厂：构造"抓取 + 落盘 + 清单"一体的数据源。"""
    updater = FetchManagerDataUpdater(
        app_config,
        data_dir=data_dir,
        provider_factory=provider_factory,
        benchmark_symbols=benchmark_symbols,
        **updater_kwargs,
    )
    return AutoUpdatingDataSource(updater, enforce_coverage=enforce_coverage)
