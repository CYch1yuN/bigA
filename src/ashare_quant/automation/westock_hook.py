"""westock 旁路核验的每日挂接（hook）。

职责：在每日管线的 ``quality_gate`` 之后，用 westock（腾讯自选股）对
已入库主源数据做未复权交叉核验，并把结果写为独立报告。

严格旁路契约
------------
1. 本模块**永远不抛异常**：内部所有失败（MCP 不可用、数据缺失、类型异常）
   一律 catch 后降级为 ``unavailable`` / ``no_data`` 结果并记录；
2. 校验失败**只产生告警和报告**，绝不改变主流程的成功状态（退出码）；
3. 不生成 ``open_qfq`` / ``close_qfq`` / ``adjustment_factor`` 等复权字段；
4. 连续异常累计写入 ``state/validators/westock.json``，超过
   ``validators.consecutive_days`` 后升级告警（仍不阻断）。

本模块被 :mod:`.daily` 以独立步骤 ``westock_validation`` 调用；
fetcher 由调用方注入（生产为 westock-mcp，测试为 mock）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import pandas as pd

from ..validators.westock_validator import (
    AVAILABLE,
    NO_DATA,
    UNAVAILABLE,
    WestockValidator,
)

# 状态文件名（相对 state_dir）
_WESTOCK_STATE_NAME = "validators/westock.json"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class WestockHookResult:
    """一次旁路核验的 hook 结果（供步骤 detail 与报告使用）。"""

    status: str = "skipped"  # skipped / available / unavailable / no_data
    checked_at: str = field(default_factory=_utcnow_iso)
    issues_count: int = 0
    consecutive_anomaly_days: int = 0
    escalated: bool = False
    report_path: Optional[str] = None
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "checked_at": self.checked_at,
            "issues_count": self.issues_count,
            "consecutive_anomaly_days": self.consecutive_anomaly_days,
            "escalated": self.escalated,
            "report_path": self.report_path,
            "message": self.message,
        }


class WestockValidationHook:
    """westock 旁路核验 hook。

    ``fetch_quotes`` 与 ``fetch_calendar`` 为可注入回调（``(symbol, start, end) ->
    DataFrame | None``）；生产由 westock-mcp 提供，测试注入 mock。
    两者均可缺省：缺省时 hook 直接以 ``unavailable`` 降级，不阻塞主流程。
    """

    def __init__(
        self,
        *,
        validator: Optional[WestockValidator] = None,
        fetch_quotes: Optional[Callable[[str, date, date], pd.DataFrame | None]] = None,
        fetch_calendar: Optional[Callable[[date, date], pd.DataFrame | None]] = None,
        state_dir: Optional[Path] = None,
        reports_dir: Optional[Path] = None,
        consecutive_days: int = 3,
        escalation_severity: str = "warning",
    ) -> None:
        self.validator = validator or WestockValidator()
        self.fetch_quotes = fetch_quotes
        self.fetch_calendar = fetch_calendar
        self.state_dir = Path(state_dir) if state_dir else None
        self.reports_dir = Path(reports_dir) if reports_dir else None
        self.consecutive_days = max(1, int(consecutive_days))
        self.escalation_severity = escalation_severity

    # ---- 主入口 ------------------------------------------------------ #

    def run(
        self,
        quotes: pd.DataFrame,
        *,
        symbol: str,
        start: date,
        end: date,
        as_of: date,
        calendar: Optional[pd.DataFrame] = None,
    ) -> WestockHookResult:
        """执行一次旁路核验。任何异常都不上抛（严格旁路）。"""
        result = WestockHookResult()
        try:
            self._ensure_dirs()
            v_result = self.validator.validate(
                quotes,
                symbol=symbol,
                start=start,
                end=end,
                fetch=self.fetch_quotes,
            )
            result.status = v_result.status
            result.issues_count = len(v_result.issues)

            # 连续异常累计
            consecutive = self._bump_consecutive(as_of, v_result.issues)
            result.consecutive_anomaly_days = consecutive
            result.escalated = bool(v_result.issues) and consecutive >= self.consecutive_days

            # 报告落盘
            report_path = self._write_report(as_of, v_result, result, calendar)
            result.report_path = str(report_path) if report_path else None
            result.message = v_result.message
        except Exception as exc:  # noqa: BLE001 - 严格旁路：绝不抛出
            result.status = UNAVAILABLE
            result.message = f"westock hook 内部异常（fail-open）: {type(exc).__name__}: {exc}"
        return result

    # ---- 状态累计 ---------------------------------------------------- #

    def _state_path(self) -> Optional[Path]:
        if self.state_dir is None:
            return None
        return self.state_dir / _WESTOCK_STATE_NAME

    def _load_state(self) -> dict[str, Any]:
        path = self._state_path()
        if path is None or not path.is_file():
            return {"last_date": None, "consecutive": 0, "history": []}
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"last_date": None, "consecutive": 0, "history": []}

    def _bump_consecutive(self, as_of: date, issues: list[dict[str, Any]]) -> int:
        """更新连续异常计数：有差异则 +1（同日不重复），无差异则清零。"""
        state = self._load_state()
        last_date = state.get("last_date")
        current = int(state.get("consecutive", 0))
        today = as_of.isoformat()

        if last_date == today:
            # 同日重复运行：不重复累计
            pass
        elif issues:
            current += 1
        else:
            current = 0

        state["last_date"] = today
        state["consecutive"] = current
        # 历史保留最近 30 条（含日期与差异数）
        history = state.get("history", [])
        history.append({"date": today, "issues": len(issues), "consecutive": current})
        state["history"] = history[-30:]

        path = self._state_path()
        if path is not None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                with tmp.open("w", encoding="utf-8") as f:
                    json.dump(state, f, ensure_ascii=False, indent=2)
                tmp.replace(path)
            except OSError:
                # 状态写失败不阻断核验（只影响升级判断）
                pass
        return current

    # ---- 报告 -------------------------------------------------------- #

    def _report_path_for(self, as_of: date) -> Optional[Path]:
        if self.reports_dir is None:
            return None
        return self.reports_dir / "validation" / f"westock_{as_of.isoformat()}.json"

    def _write_report(
        self,
        as_of: date,
        v_result: Any,
        hook_result: WestockHookResult,
        calendar: Optional[pd.DataFrame],
    ) -> Optional[Path]:
        path = self._report_path_for(as_of)
        if path is None:
            return None
        payload: dict[str, Any] = {
            "title": f"westock 旁路核验 · {as_of.isoformat()}",
            "checked_at": hook_result.checked_at,
            "as_of": as_of.isoformat(),
            "status": v_result.status,
            "issues_count": len(v_result.issues),
            "consecutive_anomaly_days": hook_result.consecutive_anomaly_days,
            "escalated": hook_result.escalated,
            "request_params": v_result.request_params,
            "response_summary": v_result.response_summary,
            "primary_content_hash": v_result.primary_content_hash,
            "westock_content_hash": v_result.westock_content_hash,
            "issues": v_result.issues,
            "message": v_result.message,
            "calendar_diffs": v_result.calendar_diffs,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        return path

    def _ensure_dirs(self) -> None:
        if self.state_dir is not None:
            (self.state_dir / "validators").mkdir(parents=True, exist_ok=True)
        if self.reports_dir is not None:
            (self.reports_dir / "validation").mkdir(parents=True, exist_ok=True)


__all__ = ["WestockValidationHook", "WestockHookResult"]
