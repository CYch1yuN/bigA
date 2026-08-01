"""Phase 4 数据源抽象：可注入、可离线、**不可伪造**。

诚实性契约
----------
本模块最重要的约束不是性能，而是**不撒谎**：

- ``MarketDataBundle.online`` 只有在真的完成了一次线上抓取时才为 ``True``。
- 从本地 Parquet 读取的数据一律标记 ``online=False``、``source="local-parquet"``，
  并在 ``notes`` 中写明"离线本地数据，非线上抓取"。
- 合成样本一律标记 ``synthetic=True``，报告中必须原样透出。
- 数据源不可用时抛 ``DataUnavailableError``，由管线判为
  ``SKIPPED_DATA_UNAVAILABLE``，**绝不**退化成"复用昨日数据"再声称成功。

为什么要这样
------------
自动化系统最危险的失效模式不是崩溃，而是"安静地成功"——
拿着三天前的陈旧数据生成今天的信号，并在报告里写"运行成功"。
本模块用类型和标记把这条路堵死。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional, Protocol, Sequence

import pandas as pd

from .models import AutomationError

__all__ = [
    "DataUnavailableError",
    "MarketDataBundle",
    "MarketDataSource",
    "LocalParquetDataSource",
    "InjectedDataSource",
    "UnavailableDataSource",
    "QUOTE_COLUMNS",
]


#: 管线所需的最小行情列集合（与 Phase 1 curated schema 对齐）。
QUOTE_COLUMNS: tuple[str, ...] = (
    "symbol",
    "trade_date",
    "open_raw",
    "high_raw",
    "low_raw",
    "close_raw",
    "open_qfq",
    "high_qfq",
    "low_qfq",
    "close_qfq",
    "volume",
    "amount",
    "is_suspended",
    "is_tradable",
)


class DataUnavailableError(AutomationError):
    """数据源不可用（网络受限、文件缺失、覆盖不足等）。

    抛出该异常意味着**本次运行没有可信输入**，管线必须跳过而不是硬撑。
    """


# ---------------------------------------------------------------------- #
# 数据包
# ---------------------------------------------------------------------- #


@dataclass
class MarketDataBundle:
    """一次数据加载的完整产出与出处说明。"""

    quotes: pd.DataFrame
    source: str
    online: bool = False
    synthetic: bool = False
    security_master: Optional[pd.DataFrame] = None
    benchmark: Optional[pd.DataFrame] = None
    calendar_df: Optional[pd.DataFrame] = None
    loaded_at: Optional[datetime] = None
    notes: list[str] = field(default_factory=list)

    # -------------------------------------------------------------- #
    @property
    def is_empty(self) -> bool:
        return self.quotes is None or len(self.quotes) == 0

    @property
    def symbols(self) -> list[str]:
        if self.is_empty or "symbol" not in self.quotes.columns:
            return []
        return sorted(str(s) for s in self.quotes["symbol"].dropna().unique())

    @property
    def date_range(self) -> tuple[Optional[date], Optional[date]]:
        if self.is_empty or "trade_date" not in self.quotes.columns:
            return (None, None)
        col = pd.to_datetime(self.quotes["trade_date"], errors="coerce").dropna()
        if col.empty:
            return (None, None)
        return (col.min().date(), col.max().date())

    def latest_date(self) -> Optional[date]:
        return self.date_range[1]

    def covers(self, target: date) -> bool:
        """行情是否覆盖到指定业务日。"""
        latest = self.latest_date()
        return latest is not None and latest >= target

    def provenance(self) -> dict[str, Any]:
        """出处摘要——报告与日志必须原样透出，不得美化。"""
        start, end = self.date_range
        return {
            "source": self.source,
            "online": bool(self.online),
            "synthetic": bool(self.synthetic),
            "rows": 0 if self.is_empty else int(len(self.quotes)),
            "symbols": len(self.symbols),
            "date_start": start.isoformat() if start else None,
            "date_end": end.isoformat() if end else None,
            "loaded_at": self.loaded_at.isoformat() if self.loaded_at else None,
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------- #
# 协议
# ---------------------------------------------------------------------- #


class MarketDataSource(Protocol):
    """行情数据源协议：管线只依赖它，便于离线注入。"""

    name: str

    def load(
        self,
        *,
        symbols: Sequence[str],
        start: date,
        end: date,
        as_of: date,
    ) -> MarketDataBundle:
        """加载 ``[start, end]`` 区间行情。

        Raises:
            DataUnavailableError: 数据不可用。
        """
        ...


# ---------------------------------------------------------------------- #
# 工具
# ---------------------------------------------------------------------- #


def _normalize_quotes(df: pd.DataFrame) -> pd.DataFrame:
    """规范列类型：``trade_date`` 转 ``date``，布尔列补默认值。"""
    out = df.copy()
    if "trade_date" in out.columns:
        out["trade_date"] = pd.to_datetime(out["trade_date"], errors="coerce").dt.date
    if "is_suspended" not in out.columns:
        out["is_suspended"] = False
    if "is_tradable" not in out.columns:
        out["is_tradable"] = True
    out["is_suspended"] = out["is_suspended"].fillna(False).astype(bool)
    out["is_tradable"] = out["is_tradable"].fillna(True).astype(bool)
    sort_cols = [c for c in ("symbol", "trade_date") if c in out.columns]
    if sort_cols:
        out = out.sort_values(sort_cols).reset_index(drop=True)
    return out


def _check_columns(df: pd.DataFrame, where: str) -> None:
    missing = [c for c in QUOTE_COLUMNS if c not in df.columns]
    if missing:
        raise DataUnavailableError(f"{where}: 行情缺少必需列 {missing}")


def _slice(
    df: pd.DataFrame, *, symbols: Sequence[str], start: date, end: date
) -> pd.DataFrame:
    out = df
    if symbols and "symbol" in out.columns:
        wanted = set(symbols)
        out = out[out["symbol"].isin(wanted)]
    if "trade_date" in out.columns:
        out = out[(out["trade_date"] >= start) & (out["trade_date"] <= end)]
    return out.reset_index(drop=True)


# ---------------------------------------------------------------------- #
# 实现：本地 Parquet（默认，离线诚实）
# ---------------------------------------------------------------------- #


class LocalParquetDataSource:
    """从本地 curated Parquet 读取行情。

    该数据源**永远**标记 ``online=False``。它适合两种场景：

    1. 上游抓取任务已把当日数据写进 curated 层，本任务只做消费。
    2. 网络受限环境下的离线复现。

    如果本地数据没有覆盖到目标业务日，直接抛 ``DataUnavailableError``，
    而不是把陈旧数据当新数据用。
    """

    name = "local-parquet"

    def __init__(
        self,
        data_dir: Path | str,
        *,
        quotes_glob: str = "curated/*.parquet",
        security_master_glob: str = "curated/*master*.parquet",
        benchmark_glob: str = "curated/*benchmark*.parquet",
        max_staleness_days: Optional[int] = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.quotes_glob = quotes_glob
        self.security_master_glob = security_master_glob
        self.benchmark_glob = benchmark_glob
        self.max_staleness_days = max_staleness_days

    # -------------------------------------------------------------- #
    def _read_many(self, pattern: str) -> Optional[pd.DataFrame]:
        paths = sorted(self.data_dir.glob(pattern))
        if not paths:
            return None
        frames: list[pd.DataFrame] = []
        for p in paths:
            try:
                frames.append(pd.read_parquet(p))
            except Exception as exc:  # noqa: BLE001 - 单文件损坏不应炸掉全部
                raise DataUnavailableError(f"读取 {p} 失败: {exc}") from exc
        if not frames:
            return None
        return pd.concat(frames, ignore_index=True)

    def load(
        self,
        *,
        symbols: Sequence[str],
        start: date,
        end: date,
        as_of: date,
    ) -> MarketDataBundle:
        if not self.data_dir.exists():
            raise DataUnavailableError(f"数据目录不存在: {self.data_dir}")

        raw = self._read_many(self.quotes_glob)
        if raw is None or raw.empty:
            raise DataUnavailableError(
                f"本地 curated 层没有可用行情（{self.data_dir / self.quotes_glob}）"
            )
        _check_columns(raw, "local-parquet")
        quotes = _slice(_normalize_quotes(raw), symbols=symbols, start=start, end=end)
        if quotes.empty:
            raise DataUnavailableError(
                f"本地行情不覆盖请求区间 {start}~{end}（标的 {list(symbols)[:5]}...）"
            )

        master = None
        try:
            master_raw = self._read_many(self.security_master_glob)
            if master_raw is not None and not master_raw.empty:
                master = master_raw
        except DataUnavailableError:
            master = None

        benchmark = None
        try:
            bench_raw = self._read_many(self.benchmark_glob)
            if bench_raw is not None and not bench_raw.empty:
                benchmark = _normalize_quotes(bench_raw)
        except DataUnavailableError:
            benchmark = None

        bundle = MarketDataBundle(
            quotes=quotes,
            source=self.name,
            online=False,
            synthetic=False,
            security_master=master,
            benchmark=benchmark,
            loaded_at=datetime.now(),
            notes=[
                "离线本地数据，非线上抓取",
                f"数据目录: {self.data_dir}",
            ],
        )

        latest = bundle.latest_date()
        if latest is None:
            raise DataUnavailableError("本地行情无有效交易日")
        if self.max_staleness_days is not None:
            lag = (as_of - latest).days
            if lag > self.max_staleness_days:
                raise DataUnavailableError(
                    f"本地行情过期：最新 {latest}，业务日 {as_of}，"
                    f"滞后 {lag} 天 > 阈值 {self.max_staleness_days} 天"
                )
        return bundle


# ---------------------------------------------------------------------- #
# 实现：注入式（测试与离线复现）
# ---------------------------------------------------------------------- #


class InjectedDataSource:
    """直接注入 DataFrame 的数据源，用于离线测试与合成样本。

    ``synthetic`` 默认为 ``True``——合成数据必须自报家门。
    """

    def __init__(
        self,
        quotes: pd.DataFrame,
        *,
        name: str = "injected",
        synthetic: bool = True,
        security_master: Optional[pd.DataFrame] = None,
        benchmark: Optional[pd.DataFrame] = None,
        notes: Optional[Sequence[str]] = None,
        enforce_coverage: bool = True,
    ) -> None:
        self.name = name
        self._quotes = _normalize_quotes(quotes)
        _check_columns(self._quotes, name)
        self.synthetic = synthetic
        self.security_master = security_master
        self.benchmark = _normalize_quotes(benchmark) if benchmark is not None else None
        self.notes = list(notes or [])
        self.enforce_coverage = enforce_coverage

    def load(
        self,
        *,
        symbols: Sequence[str],
        start: date,
        end: date,
        as_of: date,
    ) -> MarketDataBundle:
        quotes = _slice(self._quotes, symbols=symbols, start=start, end=end)
        if quotes.empty:
            raise DataUnavailableError(
                f"注入数据不覆盖请求区间 {start}~{end}"
            )
        notes = list(self.notes)
        if self.synthetic:
            notes.insert(0, "合成样本数据，仅用于离线测试，不代表真实市场")
        else:
            notes.insert(0, "注入式数据源（非线上抓取）")
        bundle = MarketDataBundle(
            quotes=quotes,
            source=self.name,
            online=False,
            synthetic=self.synthetic,
            security_master=self.security_master,
            benchmark=self.benchmark,
            loaded_at=datetime.now(),
            notes=notes,
        )
        if self.enforce_coverage and not bundle.covers(as_of):
            raise DataUnavailableError(
                f"注入数据未覆盖业务日 {as_of}（最新 {bundle.latest_date()}）"
            )
        return bundle


# ---------------------------------------------------------------------- #
# 实现：显式不可用
# ---------------------------------------------------------------------- #


class UnavailableDataSource:
    """永远不可用的数据源。

    用于两种场景：网络受限环境的默认占位，以及测试
    ``SKIPPED_DATA_UNAVAILABLE`` 分支。
    """

    name = "unavailable"

    def __init__(self, reason: str = "未配置可用数据源（网络受限或缺少上游任务）") -> None:
        self.reason = reason

    def load(
        self,
        *,
        symbols: Sequence[str],
        start: date,
        end: date,
        as_of: date,
    ) -> MarketDataBundle:
        raise DataUnavailableError(self.reason)


# ---------------------------------------------------------------------- #
# 工厂
# ---------------------------------------------------------------------- #


def build_default_data_source(
    data_dir: Path | str, *, max_staleness_days: Optional[int] = None
) -> MarketDataSource:
    """构造默认数据源：本地 curated Parquet（离线、不联网）。"""
    return LocalParquetDataSource(data_dir, max_staleness_days=max_staleness_days)


def lookback_start(end: date, lookback_days: int) -> date:
    """按自然日回看，宽松取数后再由日历裁剪。"""
    return end - timedelta(days=max(int(lookback_days), 1))
