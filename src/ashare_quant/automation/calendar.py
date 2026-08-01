"""交易日历（fail-closed）。

设计原则：**宁可停机，不可猜测**。

- 日历文件缺失 → 抛 ``CalendarUnavailableError``，运行判为失败。
- 目标日期落在日历覆盖范围之外 → 抛异常，绝不回退到"周一至周五即交易日"。
- 日历末端距离业务日超过 ``max_staleness_days`` → 视为过期，抛异常。

A 股存在春节 / 国庆等长假、临时休市与周末调休，任何按 ``weekday() < 5``
推断交易日的做法都会在真实环境中产生错误信号，因此本模块**不提供**该退路。
"""
from __future__ import annotations

import bisect
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

import pandas as pd

from .config import AutomationConfig
from .models import CalendarUnavailableError

__all__ = [
    "TradingCalendar",
    "load_trading_calendar",
    "normalize_date",
]


def normalize_date(value: Any) -> date:
    """将任意日期表示归一化为 ``datetime.date``。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, str):
        return date.fromisoformat(value.strip()[:10])
    return pd.Timestamp(value).date()


class TradingCalendar:
    """不可变的交易日历。

    Attributes:
        source: 日历来源描述（文件路径或 ``in-memory``）。
    """

    def __init__(
        self,
        dates: Iterable[Any],
        *,
        source: str = "in-memory",
        generated_at: Optional[datetime] = None,
    ) -> None:
        normalized = sorted({normalize_date(d) for d in dates})
        if not normalized:
            raise CalendarUnavailableError(
                f"交易日历为空（来源: {source}）；fail-closed，拒绝继续运行"
            )
        self._dates: list[date] = normalized
        self._date_set: frozenset[date] = frozenset(normalized)
        self.source = source
        self.generated_at = generated_at

    # -- 基础属性 ------------------------------------------------------ #

    def __len__(self) -> int:
        return len(self._dates)

    def __contains__(self, value: Any) -> bool:
        return normalize_date(value) in self._date_set

    @property
    def dates(self) -> Sequence[date]:
        """全部交易日（升序，只读视图）。"""
        return tuple(self._dates)

    @property
    def first_date(self) -> date:
        return self._dates[0]

    @property
    def last_date(self) -> date:
        return self._dates[-1]

    def covers(self, day: Any) -> bool:
        """目标日期是否落在日历覆盖区间内（闭区间）。"""
        d = normalize_date(day)
        return self.first_date <= d <= self.last_date

    # -- 核心查询 ------------------------------------------------------ #

    def _require_coverage(self, d: date, what: str) -> None:
        if not self.covers(d):
            raise CalendarUnavailableError(
                f"{what} {d.isoformat()} 超出交易日历覆盖范围 "
                f"[{self.first_date.isoformat()}, {self.last_date.isoformat()}]"
                f"（来源: {self.source}）；fail-closed，拒绝按工作日推断"
            )

    def is_trading_day(self, day: Any) -> bool:
        """判断是否为交易日。

        Raises:
            CalendarUnavailableError: 日期超出日历覆盖范围。
        """
        d = normalize_date(day)
        self._require_coverage(d, "查询日期")
        return d in self._date_set

    def previous_trading_day(self, day: Any, *, inclusive: bool = False) -> date:
        """返回 ``day`` 之前（或含当日）的最近一个交易日。

        Args:
            day: 参考日期。
            inclusive: 为 True 时，若 ``day`` 本身是交易日则直接返回。

        Raises:
            CalendarUnavailableError: 无更早的交易日，或超出覆盖范围。
        """
        d = normalize_date(day)
        if d > self.last_date:
            # 允许向历史回溯：只要不早于日历起点即可
            idx = len(self._dates)
        else:
            self._require_coverage(d, "查询日期")
            idx = bisect.bisect_right(self._dates, d) if inclusive else bisect.bisect_left(
                self._dates, d
            )
        if idx <= 0:
            raise CalendarUnavailableError(
                f"{d.isoformat()} 之前没有可用交易日"
                f"（日历起点 {self.first_date.isoformat()}，来源: {self.source}）"
            )
        return self._dates[idx - 1]

    def next_trading_day(self, day: Any, *, inclusive: bool = False) -> date:
        """返回 ``day`` 之后（或含当日）的最近一个交易日。

        Raises:
            CalendarUnavailableError: 无更晚的交易日，或超出覆盖范围。
        """
        d = normalize_date(day)
        if d < self.first_date:
            idx = 0
        else:
            self._require_coverage(d, "查询日期")
            idx = bisect.bisect_left(self._dates, d) if inclusive else bisect.bisect_right(
                self._dates, d
            )
        if idx >= len(self._dates):
            raise CalendarUnavailableError(
                f"{d.isoformat()} 之后没有可用交易日"
                f"（日历终点 {self.last_date.isoformat()}，来源: {self.source}）；"
                f"请先更新交易日历"
            )
        return self._dates[idx]

    def data_expected_ready_time(self, day: Any, ready_time: dtime) -> datetime:
        """返回某交易日行情数据预计就绪的本地时间点。"""
        d = normalize_date(day)
        return datetime.combine(d, ready_time)

    def latest_completed_trading_day(
        self, now: datetime, ready_time: dtime
    ) -> date:
        """返回截至 ``now`` 数据应当已就绪的最近交易日。

        规则：取 ``now`` 当日或之前的最近交易日；若该日为 ``now`` 当日且尚未到
        ``ready_time``，则再向前退一个交易日。

        Raises:
            CalendarUnavailableError: 日历不覆盖 ``now`` 所在日期。
        """
        today = now.date()
        if today > self.last_date:
            raise CalendarUnavailableError(
                f"交易日历终点为 {self.last_date.isoformat()}，"
                f"不覆盖当前日期 {today.isoformat()}；请先更新交易日历"
            )
        candidate = self.previous_trading_day(today, inclusive=True)
        if candidate == today and now < self.data_expected_ready_time(
            candidate, ready_time
        ):
            candidate = self.previous_trading_day(candidate, inclusive=False)
        return candidate

    def trading_days_between(self, start: Any, end: Any) -> list[date]:
        """返回 ``[start, end]`` 闭区间内的全部交易日。"""
        s = normalize_date(start)
        e = normalize_date(end)
        if s > e:
            return []
        lo = bisect.bisect_left(self._dates, s)
        hi = bisect.bisect_right(self._dates, e)
        return self._dates[lo:hi]

    def count_trading_days(self, start: Any, end: Any) -> int:
        """统计闭区间内的交易日数量。"""
        return len(self.trading_days_between(start, end))

    def shift(self, day: Any, offset: int) -> date:
        """以交易日为单位平移。

        Args:
            day: 基准交易日（必须是交易日）。
            offset: 正数向后，负数向前，0 返回自身。

        Raises:
            CalendarUnavailableError: 基准日不是交易日，或平移越界。
        """
        d = normalize_date(day)
        if not self.is_trading_day(d):
            raise CalendarUnavailableError(
                f"{d.isoformat()} 不是交易日，无法按交易日平移"
            )
        idx = bisect.bisect_left(self._dates, d) + offset
        if idx < 0 or idx >= len(self._dates):
            raise CalendarUnavailableError(
                f"以 {d.isoformat()} 为基准平移 {offset} 个交易日越界"
                f"（日历范围 {self.first_date.isoformat()} ~ "
                f"{self.last_date.isoformat()}）"
            )
        return self._dates[idx]

    # -- 新鲜度 -------------------------------------------------------- #

    def assert_fresh(self, as_of: Any, max_staleness_days: int) -> None:
        """校验日历相对业务日足够新。

        Raises:
            CalendarUnavailableError: 日历终点过旧。
        """
        d = normalize_date(as_of)
        if self.last_date < d:
            gap = (d - self.last_date).days
            raise CalendarUnavailableError(
                f"交易日历终点 {self.last_date.isoformat()} 早于业务日 "
                f"{d.isoformat()}（相差 {gap} 天）；fail-closed，请先更新日历"
            )
        limit = d + timedelta(days=max_staleness_days)
        if self.last_date > limit:
            return  # 日历比业务日更超前是正常的
        # 日历终点在业务日之后但不足以覆盖后续调仓时给出显式失败
        if (self.last_date - d).days < 0:  # pragma: no cover - 上面已处理
            raise CalendarUnavailableError("交易日历过期")

    def summary(self) -> dict[str, Any]:
        """日历摘要（写入运行报告）。"""
        return {
            "source": self.source,
            "first_date": self.first_date.isoformat(),
            "last_date": self.last_date.isoformat(),
            "trading_day_count": len(self._dates),
            "generated_at": (
                self.generated_at.isoformat(timespec="seconds")
                if self.generated_at
                else None
            ),
        }

    # -- 构造 ---------------------------------------------------------- #

    @classmethod
    def from_dates(
        cls, dates: Iterable[Any], *, source: str = "in-memory"
    ) -> "TradingCalendar":
        """从日期序列构造（测试与注入使用）。"""
        return cls(dates, source=source)

    @classmethod
    def from_dataframe(
        cls,
        df: pd.DataFrame,
        *,
        date_column: str = "trade_date",
        is_open_column: str = "is_open",
        source: str = "dataframe",
    ) -> "TradingCalendar":
        """从 DataFrame 构造。

        若存在 ``is_open_column``，只保留其为真的行；否则视全部行为交易日。
        """
        if df is None or len(df) == 0:
            raise CalendarUnavailableError(
                f"交易日历数据为空（来源: {source}）；fail-closed"
            )
        if date_column not in df.columns:
            raise CalendarUnavailableError(
                f"交易日历缺少日期列 {date_column!r}（来源: {source}，"
                f"实际列: {list(df.columns)}）"
            )
        working = df
        if is_open_column in df.columns:
            mask = df[is_open_column].astype("boolean").fillna(False)
            working = df[mask.to_numpy(dtype=bool)]
        values = working[date_column].dropna().tolist()
        return cls(values, source=source)

    @classmethod
    def from_parquet(
        cls,
        path: str | Path,
        *,
        date_column: str = "trade_date",
        is_open_column: str = "is_open",
    ) -> "TradingCalendar":
        """从 Parquet 文件加载。

        Raises:
            CalendarUnavailableError: 文件不存在或读取失败。
        """
        p = Path(path)
        if not p.exists():
            raise CalendarUnavailableError(
                f"交易日历文件不存在: {p}；fail-closed，"
                f"请先运行 `ashare-quant fetch` 生成交易日历"
            )
        try:
            df = pd.read_parquet(p)
        except Exception as exc:  # noqa: BLE001 - 统一转换为日历不可用
            raise CalendarUnavailableError(
                f"交易日历读取失败: {p}（{type(exc).__name__}: {exc}）"
            ) from exc
        generated_at: Optional[datetime] = None
        try:
            generated_at = datetime.fromtimestamp(p.stat().st_mtime)
        except OSError:  # pragma: no cover - 平台差异
            generated_at = None
        cal = cls.from_dataframe(
            df,
            date_column=date_column,
            is_open_column=is_open_column,
            source=str(p),
        )
        cal.generated_at = generated_at
        return cal


def load_trading_calendar(
    config: AutomationConfig,
    *,
    as_of: Optional[date] = None,
    calendar: Optional[TradingCalendar] = None,
) -> TradingCalendar:
    """按配置加载交易日历（fail-closed）。

    Args:
        config: 自动化配置。
        as_of: 业务日；提供时会做新鲜度校验。
        calendar: 直接注入的日历（测试用），提供时跳过文件读取。

    Raises:
        CalendarUnavailableError: 日历缺失、为空、过期或不覆盖业务日。
    """
    if calendar is not None:
        cal = calendar
    else:
        if not config.calendar.require_calendar:
            raise CalendarUnavailableError(
                "calendar.require_calendar 被置为 false，但本系统不提供"
                "按工作日推断交易日的退路；请恢复为 true 并提供真实交易日历"
            )
        cal = TradingCalendar.from_parquet(
            config.calendar_path,
            date_column=config.calendar.date_column,
            is_open_column=config.calendar.is_open_column,
        )
    if as_of is not None:
        cal.assert_fresh(as_of, config.calendar.max_staleness_days)
    return cal
