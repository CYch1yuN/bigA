"""westock（腾讯自选股）旁路核验器。

定位：**校验支路，不进入回测主链**。westock 只负责对已入库的主源数据
（AKShare/BaoStock）做交叉差异检测，以及交易日历辅助校验。

红线约束（与 design 决策一致）：
1. 绝不生成 ``open_qfq`` / ``close_qfq`` / ``adjustment_factor`` 等前复权字段；
2. 绝不用未复权价格冒充前复权价格；
3. 不参与正式回测，不替换主数据源；
4. 数据源（MCP）不可用时 **fail-open**：记录 unavailable 状态并返回，
   绝不允许阻断主数据更新流程或导致调用方抛异常。

留档设计：每次校验记录请求参数、响应摘要与内容 SHA-256 哈希，便于在
连接器升级后判断复权/字段问题何时修复（无需回放历史请求）。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable

import pandas as pd

# 状态枚举
AVAILABLE = "available"  # 校验正常完成
UNAVAILABLE = "unavailable"  # 数据源不可用（fail-open，不阻断主流程）
NO_DATA = "no_data"  # 数据源可用但无匹配数据

# 默认阈值（与 config/default.yaml 中 westock 规则一致；实际值以配置为准）
DEFAULT_CLOSE_TOLERANCE = 0.02  # 收盘价相对差异 2%
DEFAULT_VOLUME_TOLERANCE = 0.10  # 成交量相对差异 10%
DEFAULT_AMOUNT_TOLERANCE = 0.10  # 成交额相对差异 10%

# westock 原生列 → 校验器内部标准列
_CLOSE_ALIASES = ("close", "last", "close_raw")
_VOLUME_ALIASES = ("volume",)
_AMOUNT_ALIASES = ("amount",)

# 生成 UTC 时间戳
def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class ValidationResult:
    """一次校验的完整结果与留档信息。"""

    status: str = AVAILABLE
    checked_at: str = field(default_factory=_utcnow_iso)
    symbol: str = ""
    start_date: str = ""
    end_date: str = ""
    # 请求参数（原样保留，用于追溯）
    request_params: dict[str, Any] = field(default_factory=dict)
    # 响应摘要（行数、日期范围、字段列表等，不含原始行数据）
    response_summary: dict[str, Any] = field(default_factory=dict)
    # 主源与 westock 数据的确定性内容哈希
    primary_content_hash: str = ""
    westock_content_hash: str = ""
    # 差异条目
    issues: list[dict[str, Any]] = field(default_factory=list)
    # 交易日历差异（仅在日历校验时填充）
    calendar_diffs: list[dict[str, Any]] = field(default_factory=list)
    # 连续异常天数（用于升级处理）
    consecutive_anomaly_days: int = 0
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "checked_at": self.checked_at,
            "symbol": self.symbol,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "request_params": self.request_params,
            "response_summary": self.response_summary,
            "primary_content_hash": self.primary_content_hash,
            "westock_content_hash": self.westock_content_hash,
            "issues": self.issues,
            "calendar_diffs": self.calendar_diffs,
            "consecutive_anomaly_days": self.consecutive_anomaly_days,
            "message": self.message,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent, default=str)


class WestockValidator:
    """westock 旁路核验器。

    ``fetcher`` 为可注入的数据获取回调：``fetcher(symbol, start, end) -> DataFrame``
    或 ``(symbol, start, end) -> None``（不可用时返回 None 而非抛异常）。
    生产环境由 WorkBuddy westock-mcp 提供；测试环境注入 mock。
    """

    def __init__(
        self,
        close_tolerance: float = DEFAULT_CLOSE_TOLERANCE,
        volume_tolerance: float = DEFAULT_VOLUME_TOLERANCE,
        amount_tolerance: float = DEFAULT_AMOUNT_TOLERANCE,
        fetcher: Callable[[str, date, date], pd.DataFrame | None] | None = None,
    ) -> None:
        self.close_tolerance = close_tolerance
        self.volume_tolerance = volume_tolerance
        self.amount_tolerance = amount_tolerance
        self.fetcher = fetcher

    # ---- 公共入口 ----

    def validate(
        self,
        primary_df: pd.DataFrame,
        symbol: str,
        start: date,
        end: date,
        *,
        fetch: Callable[[str, date, date], pd.DataFrame | None] | None = None,
    ) -> ValidationResult:
        """对主源数据执行一次 westock 交叉核验。

        fail-open：fetch 返回 None / 抛异常 / 返回空数据均不抛错，
        以 status=unavailable 或 no_data 结果返回。
        """
        result = ValidationResult(
            symbol=symbol,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            request_params={
                "symbol": symbol,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "source": "westock",
                "adjust": "raw",  # 只核验未复权，绝不核验/生成前复权
                "note": "fq=qfq 与 raw 逐字节相同，westock 暂不作为复权源（2026-08-02 实测）",
            },
        )

        fetcher = fetch or self.fetcher
        if fetcher is None:
            result.status = UNAVAILABLE
            result.message = "未配置 westock fetcher（MCP 不可用或未注入）"
            return result

        # fail-open：任何异常都降级为 unavailable，不阻断主流程
        try:
            w_df = fetcher(symbol, start, end)
        except Exception as exc:  # noqa: BLE001 - 旁路校验必须 fail-open
            result.status = UNAVAILABLE
            result.message = f"westock 获取失败（fail-open）: {type(exc).__name__}: {exc}"
            return result

        if w_df is None or w_df.empty:
            result.status = NO_DATA
            result.message = "westock 返回空数据"
            return result

        # 留档：响应摘要 + 内容哈希
        result.response_summary = self._summarize(w_df)
        result.westock_content_hash = self._frame_hash(w_df)
        result.primary_content_hash = self._frame_hash(primary_df)

        # 交叉核验
        result.issues = self._cross_check(primary_df, w_df, symbol)
        result.status = AVAILABLE
        if not result.issues:
            result.message = "westock 交叉核验通过：无超阈值差异"
        else:
            result.message = f"westock 交叉核验发现 {len(result.issues)} 项超阈值差异"
        return result

    def validate_calendar(
        self,
        primary_calendar: pd.DataFrame,
        westock_calendar: pd.DataFrame,
        *,
        fetch_calendar: Callable[[date, date], pd.DataFrame | None] | None = None,
    ) -> ValidationResult:
        """交易日历辅助校验（不阻断主流程）。"""
        result = ValidationResult(
            request_params={"check": "trade_calendar", "source": "westock"}
        )
        if fetch_calendar is not None:
            try:
                wc = fetch_calendar(
                    date(2000, 1, 1), date(2100, 12, 31)
                )
            except Exception as exc:  # noqa: BLE001
                result.status = UNAVAILABLE
                result.message = f"westock 日历获取失败（fail-open）: {type(exc).__name__}: {exc}"
                return result
            if wc is None or wc.empty:
                result.status = NO_DATA
                result.message = "westock 日历为空"
                return result
            westock_calendar = wc

        if westock_calendar is None or westock_calendar.empty:
            result.status = NO_DATA
            result.message = "westock 日历为空"
            return result

        result.westock_content_hash = self._frame_hash(westock_calendar)
        result.primary_content_hash = self._frame_hash(primary_calendar)
        result.calendar_diffs = self._calendar_diff(primary_calendar, westock_calendar)
        result.status = AVAILABLE
        if not result.calendar_diffs:
            result.message = "交易日历一致"
        else:
            result.message = f"交易日历存在 {len(result.calendar_diffs)} 处不一致"
        return result

    # ---- 内部：交叉核验 ----

    def _cross_check(
        self, primary: pd.DataFrame, westock: pd.DataFrame, symbol: str
    ) -> list[dict[str, Any]]:
        """收盘价/成交量/成交额差异检测。

        返回超阈值条目；主源字段缺失时跳过对应维度。
        """
        issues: list[dict[str, Any]] = []

        p = primary.copy()
        w = westock.copy()

        # 归一化 westock 列
        w = self._rename_westock_columns(w)
        # 主源列名统一：close_raw → close，便于与 westock 同名列 merge 加后缀
        p = p.rename(columns={"close_raw": "close"})
        # 统一日期列为 trade_date
        p = self._ensure_date_col(p)
        w = self._ensure_date_col(w)

        merged = p.merge(
            w[["trade_date", "close", "volume", "amount"]],
            on="trade_date",
            how="inner",
            suffixes=("_primary", "_westock"),
        )
        if merged.empty:
            return [
                {
                    "check": "cross_source",
                    "severity": "warning",
                    "symbol": symbol,
                    "trade_date": "",
                    "description": "主源与 westock 无重叠交易日，无法核验",
                }
            ]

        for _, row in merged.iterrows():
            tdate = str(row["trade_date"])
            # 收盘价
            c_primary = self._to_float(row.get("close_primary"))
            c_westock = self._to_float(row.get("close_westock"))
            if c_primary is not None and c_westock is not None and c_primary != 0:
                rel = abs(c_primary - c_westock) / abs(c_primary)
                if rel > self.close_tolerance:
                    issues.append(
                        {
                            "check": "cross_source_close",
                            "severity": "warning",
                            "symbol": symbol,
                            "trade_date": tdate,
                            "description": f"收盘价相对差异 {rel:.4f} 超阈值 {self.close_tolerance}",
                            "details": {
                                "close_primary": c_primary,
                                "close_westock": c_westock,
                                "relative_diff": round(rel, 6),
                            },
                        }
                    )
            # 成交量
            v_primary = self._to_float(row.get("volume_primary"))
            v_westock = self._to_float(row.get("volume_westock"))
            if v_primary is not None and v_westock is not None and v_primary != 0:
                rel = abs(v_primary - v_westock) / abs(v_primary)
                if rel > self.volume_tolerance:
                    issues.append(
                        {
                            "check": "cross_source_volume",
                            "severity": "warning",
                            "symbol": symbol,
                            "trade_date": tdate,
                            "description": f"成交量相对差异 {rel:.4f} 超阈值 {self.volume_tolerance}",
                            "details": {
                                "volume_primary": v_primary,
                                "volume_westock": v_westock,
                                "relative_diff": round(rel, 6),
                            },
                        }
                    )
            # 成交额
            a_primary = self._to_float(row.get("amount_primary"))
            a_westock = self._to_float(row.get("amount_westock"))
            if a_primary is not None and a_westock is not None and a_primary != 0:
                rel = abs(a_primary - a_westock) / abs(a_primary)
                if rel > self.amount_tolerance:
                    issues.append(
                        {
                            "check": "cross_source_amount",
                            "severity": "warning",
                            "symbol": symbol,
                            "trade_date": tdate,
                            "description": f"成交额相对差异 {rel:.4f} 超阈值 {self.amount_tolerance}",
                            "details": {
                                "amount_primary": a_primary,
                                "amount_westock": a_westock,
                                "relative_diff": round(rel, 6),
                            },
                        }
                    )
        return issues

    def _calendar_diff(
        self, primary: pd.DataFrame, westock: pd.DataFrame
    ) -> list[dict[str, Any]]:
        """交易日历差异：仅报告主源有而 westock 没有（或反之）的交易日。"""
        p = self._ensure_date_col(primary)
        w = self._ensure_date_col(westock)
        p_dates = set(p["trade_date"].astype(str))
        w_dates = set(w["trade_date"].astype(str))
        diffs: list[dict[str, Any]] = []
        for d in sorted(p_dates - w_dates):
            diffs.append({"trade_date": d, "in_primary": True, "in_westock": False})
        for d in sorted(w_dates - p_dates):
            diffs.append({"trade_date": d, "in_primary": False, "in_westock": True})
        return diffs

    # ---- 内部：工具 ----

    @staticmethod
    def _rename_westock_columns(df: pd.DataFrame) -> pd.DataFrame:
        """将 westock 原生列（close/last/volume/amount）归一为内部列名。"""
        rename_map: dict[str, str] = {}
        for col in df.columns:
            cl = str(col).lower()
            if cl in _CLOSE_ALIASES and "close" not in rename_map:
                rename_map[col] = "close"
            elif cl in _VOLUME_ALIASES and "volume" not in rename_map:
                rename_map[col] = "volume"
            elif cl in _AMOUNT_ALIASES and "amount" not in rename_map:
                rename_map[col] = "amount"
        return df.rename(columns=rename_map)

    @staticmethod
    def _ensure_date_col(df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        if "trade_date" not in df.columns and "date" in df.columns:
            df["trade_date"] = df["date"]
        # 统一为字符串日期，避免 datetime64 与 object 混排 merge 失败
        if "trade_date" in df.columns:
            df["trade_date"] = df["trade_date"].astype(str)
        return df

    @staticmethod
    def _to_float(v: Any) -> float | None:
        try:
            f = float(v)
            return f
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _frame_hash(df: pd.DataFrame) -> str:
        """DataFrame 确定性 SHA-256（排除非确定字段）。"""
        df = df.copy()
        exclude = {"checked_at", "fetched_at", "observed_at"}
        cols = [c for c in df.columns if c not in exclude]
        sub = df[cols].sort_values(by=cols if cols else [0], kind="mergesort").reset_index(
            drop=True
        )
        buf = __import__("io").StringIO()
        sub.to_csv(buf, index=False, date_format="%Y-%m-%d", lineterminator="\n")
        return hashlib.sha256(buf.getvalue().encode("utf-8")).hexdigest()

    @staticmethod
    def _summarize(df: pd.DataFrame) -> dict[str, Any]:
        """响应摘要。

        日期必须按列名读取（date/trade_date），禁止依赖列顺序——否则当
        westock 返回首列为 code 时会把代码当日期（如 date_min="sh600519"）。
        无合法日期列时 date_min/date_max 返回 null 并记录 summary_error，
        绝不拿其他列冒充日期。
        """
        summary: dict[str, Any] = {
            "rows": int(len(df)),
            "columns": [str(c) for c in df.columns],
            "date_min": None,
            "date_max": None,
        }
        if df.empty:
            return summary
        lowered = {str(c).lower(): str(c) for c in df.columns}
        date_col = None
        for cand in ("trade_date", "date"):
            if cand in lowered:
                date_col = lowered[cand]
                break
        if date_col is None:
            summary["summary_error"] = "缺少 date/trade_date 列，未生成日期范围"
            return summary
        try:
            summary["date_min"] = str(df[date_col].min())
            summary["date_max"] = str(df[date_col].max())
        except Exception as exc:  # noqa: BLE001 - 摘要失败不阻断主校验
            summary["summary_error"] = f"日期列解析失败: {type(exc).__name__}"
        return summary


__all__ = ["WestockValidator", "ValidationResult", "AVAILABLE", "UNAVAILABLE", "NO_DATA"]
