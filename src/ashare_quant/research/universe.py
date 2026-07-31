"""Phase 3 历史时点（point-in-time）股票池过滤模块。

实现 point-in-time 股票池过滤：每个信号日只能使用当日已存在的信息，
禁止使用当前状态替代历史状态。

过滤规则（按检查顺序）：

1. 排除当日为 ST、*ST、退市整理或已退市的股票（来自 HistoricalStatusTable）。
2. 排除截至当日上市交易不足 120 个交易日的股票。
3. 排除当日停牌、不可交易、无效价格的股票（来自行情数据）。
4. 排除过去 20 个交易日有效交易天数不足 15 天的股票。
5. 排除过去 20 日平均成交额低于阈值的股票（默认 20_000_000 元）。
6. 排除可用现金无法购买一手的股票。

所有 ST、上市/退市状态查询必须指定日期并使用 HistoricalStatusTable 的
历史数据，不得使用当前状态。若数据源不能提供某项历史状态，应停止正式研究
并标记数据缺陷，不得用今天的状态回填历史。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional

import pandas as pd

from ..backtest.interfaces import UniverseFilter
from ..backtest.models import EligibilityDecision, StrategyContext

__all__ = [
    "PointInTimeError",
    "HistoricalStatusTable",
    "HistoricalUniverseFilter",
    "load_historical_status",
]


class PointInTimeError(Exception):
    """尝试使用当前状态而非历史状态时抛出。

    在 point-in-time 过滤中，所有状态查询必须指定日期。若尝试使用当前
    股票列表、当前名称或当前 ST 状态替代历史状态，则抛出此异常。

    典型场景：
    - ``get_status`` 未指定日期 ``dt``。
    - ``is_eligible`` 未指定信号日 ``dt``。
    - ``HistoricalStatusTable`` 未初始化或为 ``None``。
    """


@dataclass
class HistoricalStatusTable:
    """历史时点状态表。

    存储证券主数据，支持按日期做 point-in-time 查询。所有状态查询必须
    指定日期，禁止使用当前状态替代历史状态。

    支持两种 schema：

    1. **有效区间 schema**（证券主数据，推荐）：列包含 ``symbol``、
       ``list_date``、``delist_date``、``st_status``、
       ``status_valid_from``、``status_valid_to``。
       ``status_valid_to`` 为空（NaT/None）表示至今有效。
       查询时查找 ``status_valid_from <= dt`` 且
       ``status_valid_to`` 为空或 ``dt < status_valid_to`` 的记录。

    2. **逐日快照 schema**：列包含 ``symbol``、``date``、``st_status``、
       ``list_date``、``delist_date``。按 ``date == dt`` 精确匹配。

    Attributes:
        records: 证券主数据 DataFrame。至少包含 ``symbol`` 和
            ``st_status`` 列，以及 ``status_valid_from`` 或 ``date`` 列
            之一用于时点定位。
    """

    records: pd.DataFrame

    def __post_init__(self) -> None:
        """校验必需列。"""
        if self.records is None:
            raise PointInTimeError("HistoricalStatusTable.records 不能为 None")
        required = {"symbol", "st_status"}
        missing = required - set(self.records.columns)
        if missing:
            raise ValueError(
                f"HistoricalStatusTable 缺少必需列: {sorted(missing)}"
            )
        # 必须有日期定位列：status_valid_from 或 date
        if (
            "status_valid_from" not in self.records.columns
            and "date" not in self.records.columns
        ):
            raise ValueError(
                "HistoricalStatusTable 必须包含 status_valid_from "
                "或 date 列用于时点定位"
            )

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _to_date(val: Any) -> Optional[date]:
        """将值安全转换为 ``date`` 对象。

        处理 ``date``、``datetime``、``pd.Timestamp``、字符串和 NaN。
        """
        if val is None:
            return None
        if isinstance(val, datetime):
            return val.date()
        if isinstance(val, date):
            return val
        if isinstance(val, pd.Timestamp):
            return val.date()
        try:
            if pd.isna(val):
                return None
        except (TypeError, ValueError):
            pass
        return pd.Timestamp(val).date()

    def _symbol_records(self, symbol: str) -> pd.DataFrame:
        """获取某股票的全部记录。"""
        return self.records[self.records["symbol"] == symbol]

    def _has_range_schema(self) -> bool:
        """是否使用有效区间 schema。"""
        return "status_valid_from" in self.records.columns

    # ------------------------------------------------------------------ #
    # 查询方法
    # ------------------------------------------------------------------ #
    def get_status(self, symbol: str, dt: date) -> Optional[pd.Series]:
        """获取某股票在某日的时点状态记录（point-in-time）。

        - 有效区间 schema：查找 ``status_valid_from <= dt`` 且
          ``status_valid_to`` 为空或 ``dt < status_valid_to`` 的记录。
          若多条匹配，取 ``status_valid_from`` 最晚的一条。
        - 逐日快照 schema：按 ``date == dt`` 精确匹配。

        Args:
            symbol: 股票代码。
            dt: 查询日期，不能为 ``None``。

        Returns:
            匹配的状态记录行（``pd.Series``）；无匹配返回 ``None``。

        Raises:
            PointInTimeError: ``dt`` 为 ``None`` 时抛出。
        """
        if dt is None:
            raise PointInTimeError(
                "get_status 必须指定日期 dt，禁止使用当前状态替代历史状态"
            )
        sym_df = self._symbol_records(symbol)
        if sym_df.empty:
            return None

        if self._has_range_schema():
            ts = pd.Timestamp(dt)
            valid_from = pd.to_datetime(sym_df["status_valid_from"])
            mask = valid_from <= ts
            if "status_valid_to" in sym_df.columns:
                valid_to = pd.to_datetime(sym_df["status_valid_to"])
                mask = mask & (valid_to.isna() | (ts < valid_to))
            matched = sym_df[mask]
            if matched.empty:
                return None
            # 取 status_valid_from 最晚的记录（最近生效的状态）
            return matched.sort_values("status_valid_from").iloc[-1]
        else:
            # 逐日快照 schema
            target = self._to_date(dt)
            for _, row in sym_df.iterrows():
                row_date = self._to_date(row.get("date"))
                if row_date == target:
                    return row
            return None

    def get_listed_date(self, symbol: str) -> Optional[date]:
        """获取股票上市日期。

        从该股票所有记录中取最早的 ``list_date``。

        Args:
            symbol: 股票代码。

        Returns:
            上市日期；无记录或无 ``list_date`` 列返回 ``None``。
        """
        sym_df = self._symbol_records(symbol)
        if sym_df.empty or "list_date" not in sym_df.columns:
            return None
        dates = sym_df["list_date"].dropna()
        if dates.empty:
            return None
        return self._to_date(dates.min())

    def get_delisted_date(self, symbol: str) -> Optional[date]:
        """获取股票退市日期。

        从该股票所有记录中取最晚的 ``delist_date``。

        Args:
            symbol: 股票代码。

        Returns:
            退市日期；未退市或无记录返回 ``None``。
        """
        sym_df = self._symbol_records(symbol)
        if sym_df.empty or "delist_date" not in sym_df.columns:
            return None
        dates = sym_df["delist_date"].dropna()
        if dates.empty:
            return None
        return self._to_date(dates.max())

    def is_st(self, symbol: str, dt: date) -> bool:
        """判断某股票在某日是否为 ST 或 *ST 状态。

        Args:
            symbol: 股票代码。
            dt: 查询日期。

        Returns:
            ``True`` 表示当日为 ST/*ST；无状态记录返回 ``False``。
        """
        record = self.get_status(symbol, dt)
        if record is None:
            return False
        status = str(record.get("st_status", "normal")).lower().strip()
        return status in ("st", "*st")

    def is_delisting(self, symbol: str, dt: date) -> bool:
        """判断某股票在某日是否处于退市整理期。

        退市整理期状态包括 ``delisting``、``退市整理``、``pt``。

        Args:
            symbol: 股票代码。
            dt: 查询日期。

        Returns:
            ``True`` 表示处于退市整理期。
        """
        record = self.get_status(symbol, dt)
        if record is None:
            return False
        status = str(record.get("st_status", "normal")).lower().strip()
        return status in ("delisting", "退市整理", "pt")

    def is_delisted(self, symbol: str, dt: date) -> bool:
        """判断某股票在某日是否已退市。

        若退市日期存在且 ``dt >= 退市日期``，则视为已退市。

        Args:
            symbol: 股票代码。
            dt: 查询日期。

        Returns:
            ``True`` 表示已退市。
        """
        delist_date = self.get_delisted_date(symbol)
        if delist_date is None:
            return False
        return dt >= delist_date

    def get_st_status(self, symbol: str, dt: date) -> str:
        """获取某股票在某日的 ST 状态字符串。

        Args:
            symbol: 股票代码。
            dt: 查询日期。

        Returns:
            ST 状态（如 ``normal``、``st``、``*st``、``pt``）；
            无记录返回 ``unknown``。
        """
        record = self.get_status(symbol, dt)
        if record is None:
            return "unknown"
        return str(record.get("st_status", "unknown")).lower().strip()


def load_historical_status(parquet_path: str) -> HistoricalStatusTable:
    """从 Parquet 文件加载历史状态表。

    Args:
        parquet_path: Parquet 文件路径。

    Returns:
        加载后的 :class:`HistoricalStatusTable`。

    Raises:
        FileNotFoundError: 文件不存在。
        ValueError: 缺少必需列。
    """
    df = pd.read_parquet(parquet_path)
    return HistoricalStatusTable(records=df)


class HistoricalUniverseFilter(UniverseFilter):
    """历史时点股票池过滤器（Phase 3）。

    实现 point-in-time 过滤，所有 ST、上市/退市状态查询使用
    :class:`HistoricalStatusTable` 的历史数据，禁止使用当前状态。

    可用于两种场景：

    1. **引擎内**：通过 ``is_eligible(symbol, dt, context)`` 调用，
       行情数据来自 ``context.bars_up_to_date``，可用现金来自
       ``context.portfolio.cash``。
    2. **引擎外研究**：初始化时传入 ``quotes`` 和 ``available_cash``，
       或通过 ``filter_on_date`` 批量过滤。

    Args:
        status_table: 历史状态表，不能为 ``None``。
        quotes: 全量行情 DataFrame（可选，用于引擎外研究）。
            引擎内使用时通过 ``context.bars_up_to_date`` 传入。
            需包含列：``trade_date``、``symbol``、``close_raw``、
            ``volume``、``amount`` 或 ``turnover``、``is_suspended``、
            ``is_tradable``。
        min_listing_days: 上市最少交易日数，默认 120。
        min_valid_days: 过去 ``valid_days_window`` 日最少有效交易天数，
            默认 15。
        valid_days_window: 有效交易天数回看窗口，默认 20。
        min_turnover: 过去 ``turnover_window`` 日最低平均成交额（元），
            默认 20_000_000。
        turnover_window: 成交额回看窗口，默认 20。
        lot_size: 一手股数，默认 100。
        available_cash: 可用现金（引擎外研究用，引擎内从 context 读取）。

    Raises:
        PointInTimeError: ``status_table`` 为 ``None`` 时抛出。
    """

    def __init__(
        self,
        status_table: HistoricalStatusTable,
        *,
        quotes: Optional[pd.DataFrame] = None,
        min_listing_days: int = 120,
        min_valid_days: int = 15,
        valid_days_window: int = 20,
        min_turnover: float = 20_000_000.0,
        turnover_window: int = 20,
        lot_size: int = 100,
        available_cash: Optional[float] = None,
    ) -> None:
        if status_table is None:
            raise PointInTimeError(
                "status_table 不能为 None，point-in-time 过滤需要历史状态表"
            )
        self._status_table = status_table
        self._quotes = quotes
        self._min_listing_days = min_listing_days
        self._min_valid_days = min_valid_days
        self._valid_days_window = valid_days_window
        self._min_turnover = min_turnover
        self._turnover_window = turnover_window
        self._lot_size = lot_size
        self._available_cash = available_cash

    # ------------------------------------------------------------------ #
    # UniverseFilter 接口实现
    # ------------------------------------------------------------------ #
    def is_eligible(
        self,
        symbol: str,
        dt: date,
        context: Optional[StrategyContext] = None,
    ) -> EligibilityDecision:
        """判断某股票在某日是否可交易（point-in-time）。

        所有状态查询使用 :class:`HistoricalStatusTable` 的历史数据，
        禁止使用当前状态。

        Args:
            symbol: 股票代码。
            dt: 信号日，不能为 ``None``。
            context: 策略上下文（引擎内传入）。为 ``None`` 时使用初始化时
                配置的 ``quotes`` 和 ``available_cash``。

        Returns:
            过滤决策。

        Raises:
            PointInTimeError: ``dt`` 为 ``None`` 时抛出。
        """
        if dt is None:
            raise PointInTimeError(
                "is_eligible 必须指定信号日 dt，禁止使用当前日期"
            )

        # 获取行情数据：优先从 context 获取，回退到初始化配置
        if context is not None and context.bars_up_to_date is not None:
            quotes = context.bars_up_to_date
        else:
            quotes = self._quotes

        # 获取可用现金：优先从 context 获取，回退到初始化配置
        if context is not None:
            cash = float(context.portfolio.cash)
        else:
            cash = self._available_cash

        return self._check_eligibility(symbol, dt, quotes, cash)

    def filter_on_date(
        self,
        dt: date,
        quotes: Optional[pd.DataFrame] = None,
        cash: Optional[float] = None,
    ) -> dict[str, EligibilityDecision]:
        """批量过滤某日所有股票。

        遍历行情数据中截至 ``dt`` 的所有 symbol，逐一执行过滤。

        Args:
            dt: 信号日。
            quotes: 行情 DataFrame（为 ``None`` 时使用初始化配置）。
            cash: 可用现金（为 ``None`` 时使用初始化配置）。

        Returns:
            ``{symbol: EligibilityDecision}`` 字典。
        """
        q = quotes if quotes is not None else self._quotes
        c = cash if cash is not None else self._available_cash
        if q is None or len(q) == 0:
            return {}

        # 获取截至 dt 的所有 symbol
        q_up_to = self._filter_quotes_up_to(q, dt)
        if q_up_to.empty:
            return {}
        symbols = sorted(q_up_to["symbol"].unique().tolist())
        return {
            sym: self._check_eligibility(sym, dt, q, c) for sym in symbols
        }

    # ------------------------------------------------------------------ #
    # 核心过滤逻辑
    # ------------------------------------------------------------------ #
    def _check_eligibility(
        self,
        symbol: str,
        dt: date,
        quotes: Optional[pd.DataFrame],
        cash: Optional[float],
    ) -> EligibilityDecision:
        """执行全部过滤规则，按顺序检查，首个不通过即返回。

        过滤顺序：
        1. 行情数据存在性
        2. ST/*ST/退市整理/已退市（point-in-time 状态表）
        3. 上市交易日数 >= min_listing_days
        4. 当日行情记录存在
        5. 停牌/不可交易/无效价格
        6. 过去 valid_days_window 日有效交易天数 >= min_valid_days
        7. 过去 turnover_window 日平均成交额 >= min_turnover
        8. 可用现金可购买一手
        """
        # 0. 行情数据保护
        if quotes is None or len(quotes) == 0:
            return EligibilityDecision(False, "无行情数据，无法判断可交易性")

        # 1. ST / *ST / 退市整理 / 已退市（point-in-time 状态表查询）
        st_status = self._status_table.get_st_status(symbol, dt)
        if self._status_table.is_st(symbol, dt):
            return EligibilityDecision(
                False, f"{symbol}: {dt} ST/*ST 状态({st_status})"
            )
        if self._status_table.is_delisting(symbol, dt):
            return EligibilityDecision(
                False, f"{symbol}: {dt} 退市整理期({st_status})"
            )
        if self._status_table.is_delisted(symbol, dt):
            return EligibilityDecision(
                False, f"{symbol}: {dt} 已退市"
            )

        # 2. 上市不足 min_listing_days 个交易日
        listed_date = self._status_table.get_listed_date(symbol)
        if listed_date is None:
            return EligibilityDecision(
                False,
                f"{symbol}: 无上市日期记录，无法验证上市时长",
            )
        market_dates = self._get_market_dates(quotes, dt)
        trading_days_since_listing = sum(
            1 for d in market_dates if d >= listed_date
        )
        if trading_days_since_listing < self._min_listing_days:
            return EligibilityDecision(
                False,
                f"{symbol}: 上市交易 {trading_days_since_listing} 日"
                f" < 最低 {self._min_listing_days} 日",
            )

        # 3. 当日行情记录存在
        sym_quotes = self._filter_symbol_quotes(quotes, symbol, dt)
        day_row = self._get_day_row(sym_quotes, dt)
        if day_row is None:
            return EligibilityDecision(
                False,
                f"{symbol}: {dt} 当日无行情记录"
                f"（停牌无数据或上市/退市区间外）",
            )

        # 4. 停牌 / 不可交易 / 无效价格
        is_suspended = bool(day_row.get("is_suspended", False))
        is_tradable = bool(day_row.get("is_tradable", True))
        if is_suspended or not is_tradable:
            return EligibilityDecision(
                False,
                f"{symbol}: {dt} 停牌或不可交易"
                f"(is_suspended={is_suspended}, "
                f"is_tradable={is_tradable})",
            )

        close_raw = self._safe_float(day_row.get("close_raw", 0))
        if not (close_raw > 0):
            return EligibilityDecision(
                False,
                f"{symbol}: {dt} 无效收盘价(close_raw={close_raw})",
            )

        # 5. 过去 valid_days_window 日有效交易天数 >= min_valid_days
        recent_dates = market_dates[-(self._valid_days_window):]
        valid_count = 0
        for d in recent_dates:
            row = self._get_day_row(sym_quotes, d)
            if row is not None and not bool(row.get("is_suspended", False)):
                valid_count += 1
        if valid_count < self._min_valid_days:
            return EligibilityDecision(
                False,
                f"{symbol}: 过去 {self._valid_days_window} 日有效交易 "
                f"{valid_count} 天 < {self._min_valid_days} 天",
            )

        # 6. 过去 turnover_window 日平均成交额 >= min_turnover
        turnover_col = self._get_turnover_column(quotes)
        recent_rows = self._get_recent_rows(
            sym_quotes, recent_dates, self._turnover_window
        )
        if recent_rows.empty:
            return EligibilityDecision(
                False,
                f"{symbol}: 过去 {self._turnover_window} 日无成交额数据",
            )
        avg_turnover = float(recent_rows[turnover_col].mean())
        if avg_turnover < self._min_turnover:
            return EligibilityDecision(
                False,
                f"{symbol}: 过去 {self._turnover_window} 日平均成交额 "
                f"{avg_turnover:.2f} < {self._min_turnover:.2f}",
            )

        # 7. 可用现金无法购买一手
        if cash is not None:
            lot_cost = close_raw * self._lot_size
            if lot_cost > cash:
                return EligibilityDecision(
                    False,
                    f"{symbol}: 一手成本 {lot_cost:.2f}"
                    f" > 可用现金 {cash:.2f}",
                )

        return EligibilityDecision(True, "")

    # ------------------------------------------------------------------ #
    # 辅助方法：日期处理
    # ------------------------------------------------------------------ #
    @staticmethod
    def _to_date(val: Any) -> Optional[date]:
        """将值安全转换为 ``date`` 对象。"""
        if val is None:
            return None
        if isinstance(val, datetime):
            return val.date()
        if isinstance(val, date):
            return val
        if isinstance(val, pd.Timestamp):
            return val.date()
        try:
            if pd.isna(val):
                return None
        except (TypeError, ValueError):
            pass
        return pd.Timestamp(val).date()

    @staticmethod
    def _safe_float(val: Any) -> float:
        """安全转换为 float，处理 None/NaN/字符串。"""
        try:
            result = float(val)
            if pd.isna(result):
                return 0.0
            return result
        except (TypeError, ValueError):
            return 0.0

    def _get_market_dates(
        self, quotes: pd.DataFrame, dt: date
    ) -> list[date]:
        """获取截至 ``dt`` 的所有交易日（去重、排序）。

        从全量行情（所有 symbol）中提取唯一交易日，确保使用市场日历
        而非单只股票的交易记录。
        """
        dt_series = pd.to_datetime(quotes["trade_date"])
        ts = pd.Timestamp(dt)
        mask = dt_series <= ts
        unique = dt_series[mask].dt.date.unique()
        return sorted(unique)

    def _filter_quotes_up_to(
        self, quotes: pd.DataFrame, dt: date
    ) -> pd.DataFrame:
        """过滤截至 ``dt`` 的行情（所有 symbol）。"""
        dt_series = pd.to_datetime(quotes["trade_date"])
        ts = pd.Timestamp(dt)
        mask = dt_series <= ts
        return quotes[mask].copy()

    def _filter_symbol_quotes(
        self, quotes: pd.DataFrame, symbol: str, dt: date
    ) -> pd.DataFrame:
        """过滤某股票截至 ``dt`` 的行情。"""
        sym_df = quotes[quotes["symbol"] == symbol]
        if sym_df.empty:
            return sym_df
        dt_series = pd.to_datetime(sym_df["trade_date"])
        ts = pd.Timestamp(dt)
        mask = dt_series <= ts
        return sym_df[mask]

    def _get_day_row(
        self, sym_quotes: pd.DataFrame, dt: date
    ) -> Optional[pd.Series]:
        """从股票行情中获取指定日期的行；不存在返回 ``None``。"""
        if sym_quotes.empty:
            return None
        dt_series = pd.to_datetime(sym_quotes["trade_date"])
        ts = pd.Timestamp(dt)
        mask = dt_series == ts
        matched = sym_quotes[mask]
        if matched.empty:
            return None
        return matched.iloc[0]

    def _get_recent_rows(
        self,
        sym_quotes: pd.DataFrame,
        market_dates: list[date],
        window: int,
    ) -> pd.DataFrame:
        """获取过去 ``window`` 个交易日的行情行。

        Args:
            sym_quotes: 某股票截至信号日的行情。
            market_dates: 市场交易日列表（已排序）。
            window: 回看天数。
        """
        recent = market_dates[-(window):]
        if not recent or sym_quotes.empty:
            return pd.DataFrame()
        recent_ts = {pd.Timestamp(d) for d in recent}
        dt_series = pd.to_datetime(sym_quotes["trade_date"])
        mask = dt_series.isin(recent_ts)
        return sym_quotes[mask]

    @staticmethod
    def _get_turnover_column(df: pd.DataFrame) -> str:
        """获取成交额列名（兼容 ``amount`` 和 ``turnover``）。

        Raises:
            ValueError: 两个列名都不存在时抛出。
        """
        if "amount" in df.columns:
            return "amount"
        if "turnover" in df.columns:
            return "turnover"
        raise ValueError("行情数据缺少成交额列（amount 或 turnover）")
