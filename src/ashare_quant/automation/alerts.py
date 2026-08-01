"""本机告警：失败必须"看得见"。

无人值守的自动化最危险的失败模式不是"跑挂了"，而是"跑挂了但没人知道"。
本模块提供四层递进的可见性：

1. **失败标记文件** ``state/automation/LAST_FAILURE.marker``
   —— 存在即代表上一次运行有问题；成功运行会自动清除。
2. **最新失败报告** ``reports/phase-4/latest-failure.md`` 与 ``latest-failure.json``
   —— 人读 + 机读双份，包含 run_id、状态、失败步骤与建议动作。
3. **非零退出码** —— 任务计划程序的"上次运行结果"列会直接变红。
4. **可选通道** —— Windows 事件日志 / toast 通知 / webhook。

安全约束：

- webhook URL **只能**来自环境变量（配置文件里写 URL 会在校验期被拒绝）。
- 所有外发内容都经过脱敏，不含任何凭据。
- 任何告警通道失败都不得让主流程崩溃——告警是"附加价值"，不是"新的故障点"。
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .config import AutomationConfig
from .logging_setup import AutomationLogger, redact_value, scrub_text
from .models import BLOCKED_STATES, RunRecord, RunState, StepStatus
from .state import atomic_write_json, atomic_write_text

__all__ = [
    "AlertManager",
    "build_failure_markdown",
]

_ACTION_HINTS: dict[RunState, str] = {
    RunState.FAILED: (
        "检查 `latest-failure.json` 中的 `failed_steps`，"
        "修复后执行 `ashare-quant automation rerun --as-of <日期> --force-retry`。"
    ),
    RunState.BLOCKED_DATA_QUALITY: (
        "数据质量存在 critical 问题，已阻断信号与模拟下单。"
        "请先修复数据（重新抓取或修正源），再重跑当日任务。"
        "**严禁**为了跑通而放宽质量闸门。"
    ),
    RunState.BLOCKED_LOCKED: (
        "另一个自动化进程正在运行。确认无重复计划任务后，"
        "若确属残留锁，删除 `state/automation/automation.lock` 前请先确认持锁进程已退出。"
    ),
    RunState.BLOCKED_NOT_ELIGIBLE: (
        "检测到试图启用实盘交易。Phase 3 结论为稳健轨 "
        "NOT_ELIGIBLE_FOR_LIVE_TRADING、激进轨 SIMULATION_ONLY，"
        "本系统不提供实盘通路。请将 `live_trading.enabled` 恢复为 false。"
    ),
    RunState.SKIPPED_DATA_UNAVAILABLE: (
        "数据源不可用，当日任务已跳过（未产生任何信号或模拟订单）。"
        "数据恢复后可重跑该业务日。"
    ),
}


def build_failure_markdown(record: RunRecord, *, extra: Optional[dict[str, Any]] = None) -> str:
    """生成人读的失败报告 Markdown。"""
    failed_steps = [
        s for s in record.steps if s.status in (StepStatus.FAILED, StepStatus.BLOCKED)
    ]
    lines: list[str] = []
    lines.append(f"# 自动化运行告警 - {record.state.value}")
    lines.append("")
    lines.append(
        "> 本系统仅产出研究信号与模拟账户结果，**不构成实盘交易授权**，"
        "不连接任何券商接口。"
    )
    lines.append("")
    lines.append("## 运行概要")
    lines.append("")
    lines.append("| 字段 | 值 |")
    lines.append("| --- | --- |")
    lines.append(f"| run_id | `{record.run_id}` |")
    lines.append(f"| 任务类型 | {record.task_type.value} |")
    lines.append(f"| 业务日 | {record.as_of_date.isoformat()} |")
    lines.append(f"| 状态 | **{record.state.value}** |")
    lines.append(f"| 退出码 | {record.exit_code} |")
    lines.append(f"| 尝试次数 | {record.attempt} |")
    lines.append(f"| code_commit | `{record.code_commit}` |")
    lines.append(f"| config_hash | `{record.config_hash}` |")
    lines.append(f"| input_hash | `{record.input_hash}` |")
    started = (
        record.started_at.isoformat(timespec="seconds") if record.started_at else "n/a"
    )
    finished = (
        record.finished_at.isoformat(timespec="seconds")
        if record.finished_at
        else "n/a"
    )
    lines.append(f"| 开始 / 结束 | {started} / {finished} |")
    lines.append(f"| 耗时(秒) | {record.duration_seconds if record.duration_seconds is not None else 'n/a'} |")
    lines.append("")

    if record.message:
        lines.append("## 结论")
        lines.append("")
        lines.append(scrub_text(record.message))
        lines.append("")

    lines.append("## 失败 / 阻断步骤")
    lines.append("")
    if not failed_steps:
        lines.append("_无显式失败步骤（可能在步骤编排之前即告失败）。_")
    else:
        lines.append("| 步骤 | 状态 | 错误 |")
        lines.append("| --- | --- | --- |")
        for s in failed_steps:
            err = scrub_text(s.error or "").replace("|", "\\|") or "-"
            lines.append(f"| {s.name} | {s.status.value} | {err} |")
    lines.append("")

    lines.append("## 全部步骤")
    lines.append("")
    lines.append("| # | 步骤 | 状态 | 耗时(秒) |")
    lines.append("| --- | --- | --- | --- |")
    for idx, s in enumerate(record.steps, start=1):
        dur = s.duration_seconds
        lines.append(
            f"| {idx} | {s.name} | {s.status.value} | "
            f"{dur if dur is not None else '-'} |"
        )
    lines.append("")

    hint = _ACTION_HINTS.get(record.state)
    if hint:
        lines.append("## 建议动作")
        lines.append("")
        lines.append(hint)
        lines.append("")

    if extra:
        lines.append("## 附加信息")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(extra, ensure_ascii=False, indent=2, default=str))
        lines.append("```")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        f"生成时间: {datetime.now().isoformat(timespec='seconds')} · "
        "Phase 4 自动化（模拟运行，无实盘授权）"
    )
    lines.append("")
    return "\n".join(lines)


class AlertManager:
    """本机告警管理器。"""

    def __init__(
        self,
        config: AutomationConfig,
        *,
        logger: Optional[AutomationLogger] = None,
        env: Optional[dict[str, str]] = None,
    ) -> None:
        self.config = config
        self.logger = logger
        self._env = env if env is not None else dict(os.environ)

    # -- 路径 ---------------------------------------------------------- #

    @property
    def marker_path(self) -> Path:
        return self.config.state_dir / self.config.alerts.failure_marker_filename

    @property
    def failure_md_path(self) -> Path:
        return self.config.reports_dir / self.config.alerts.latest_failure_md

    @property
    def failure_json_path(self) -> Path:
        return self.config.reports_dir / self.config.alerts.latest_failure_json

    # -- 主入口 -------------------------------------------------------- #

    def handle(
        self, record: RunRecord, *, extra: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        """根据运行终态决定是否告警。

        Returns:
            告警动作摘要（写入运行报告）。
        """
        if record.state is RunState.SUCCESS:
            cleared = self.clear()
            return {"alerted": False, "cleared_marker": cleared}
        if record.state is RunState.SKIPPED_NON_TRADING_DAY:
            # 非交易日跳过是完全正常的，不告警也不清除历史标记
            return {"alerted": False, "reason": "non_trading_day"}
        return self.raise_alert(record, extra=extra)

    def raise_alert(
        self, record: RunRecord, *, extra: Optional[dict[str, Any]] = None
    ) -> dict[str, Any]:
        """发出告警（写标记 + 落报告 + 可选通道）。"""
        if not self.config.alerts.enabled:
            return {"alerted": False, "reason": "alerts_disabled"}

        safe_extra = (
            redact_value(extra, self.config.logging.redact_keys) if extra else None
        )
        actions: dict[str, Any] = {"alerted": True, "channels": []}

        # 1) 标记文件
        try:
            marker_payload = {
                "run_id": record.run_id,
                "task_type": record.task_type.value,
                "as_of_date": record.as_of_date.isoformat(),
                "state": record.state.value,
                "exit_code": record.exit_code,
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
            atomic_write_json(self.marker_path, marker_payload)
            actions["channels"].append("marker_file")
            actions["marker_path"] = str(self.marker_path)
        except OSError as exc:  # pragma: no cover
            self._log_warn("alert_marker_failed", f"写入失败标记失败: {exc}")

        # 2) 最新失败报告（md + json）
        try:
            atomic_write_text(
                self.failure_md_path, build_failure_markdown(record, extra=safe_extra)
            )
            payload = record.to_dict()
            payload["extra"] = safe_extra
            payload["action_hint"] = _ACTION_HINTS.get(record.state, "")
            payload["eligibility_note"] = (
                "稳健轨 NOT_ELIGIBLE_FOR_LIVE_TRADING；激进轨 SIMULATION_ONLY；"
                "本系统仅产出模拟结果，无实盘授权。"
            )
            atomic_write_json(self.failure_json_path, payload)
            actions["channels"].append("failure_report")
            actions["failure_md"] = str(self.failure_md_path)
            actions["failure_json"] = str(self.failure_json_path)
        except OSError as exc:  # pragma: no cover
            self._log_warn("alert_report_failed", f"写入失败报告失败: {exc}")

        # 3) Windows 事件日志
        if self.config.alerts.windows_event_log:
            if self._write_event_log(record):
                actions["channels"].append("windows_event_log")

        # 4) toast 通知
        if self.config.alerts.toast:
            if self._send_toast(record):
                actions["channels"].append("toast")

        # 5) webhook（仅环境变量）
        webhook = self._webhook_url()
        if webhook:
            ok = self._post_webhook(webhook, record)
            actions["channels"].append("webhook" if ok else "webhook_failed")

        self._log_warn(
            "alert_raised",
            f"运行 {record.run_id} 终态为 {record.state.value}，已发出本机告警",
            state=record.state.value,
            exit_code=record.exit_code,
            channels=actions["channels"],
        )
        return actions

    def clear(self) -> bool:
        """清除失败标记（成功运行时调用）。

        Returns:
            是否实际删除了标记文件。
        """
        path = self.marker_path
        if not path.exists():
            return False
        try:
            path.unlink()
        except OSError:  # pragma: no cover
            return False
        self._log_info("alert_cleared", "运行成功，已清除失败标记")
        return True

    def has_pending_failure(self) -> bool:
        """是否存在未清除的失败标记。"""
        return self.marker_path.exists()

    def read_marker(self) -> Optional[dict[str, Any]]:
        """读取失败标记内容。"""
        path = self.marker_path
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    # -- 可选通道实现 --------------------------------------------------- #

    def _webhook_url(self) -> Optional[str]:
        """从环境变量读取 webhook URL（配置文件中禁止明文）。"""
        var = self.config.alerts.webhook_env_var
        if not var:
            return None
        url = self._env.get(var, "").strip()
        return url or None

    def _post_webhook(self, url: str, record: RunRecord) -> bool:
        """向 webhook 推送简报（失败不影响主流程，且不回显 URL）。"""
        import urllib.error
        import urllib.request

        body = {
            "source": "ashare-quant-automation",
            "simulated_only": True,
            "run_id": record.run_id,
            "task_type": record.task_type.value,
            "as_of_date": record.as_of_date.isoformat(),
            "state": record.state.value,
            "exit_code": record.exit_code,
            "message": scrub_text(record.message)[:500],
        }
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(
                req, timeout=self.config.alerts.webhook_timeout_seconds
            ) as resp:
                ok = 200 <= getattr(resp, "status", 200) < 300
        except Exception as exc:  # noqa: BLE001 - 告警失败不得中断主流程
            # 注意：只记录异常类型，绝不记录 URL 本身
            self._log_warn(
                "alert_webhook_failed",
                f"webhook 推送失败（{type(exc).__name__}）",
                env_var=self.config.alerts.webhook_env_var,
            )
            return False
        if not ok:
            self._log_warn("alert_webhook_failed", "webhook 返回非 2xx")
        return ok

    def _write_event_log(self, record: RunRecord) -> bool:
        """写入 Windows 事件日志（通过 eventcreate，无需额外依赖）。"""
        if os.name != "nt":
            self._log_info(
                "alert_event_log_skipped", "非 Windows 平台，跳过事件日志"
            )
            return False
        msg = (
            f"[{record.state.value}] run_id={record.run_id} "
            f"task={record.task_type.value} as_of={record.as_of_date.isoformat()} "
            f"exit={record.exit_code}"
        )
        cmd = [
            "eventcreate",
            "/T",
            "ERROR",
            "/ID",
            "700",
            "/L",
            "APPLICATION",
            "/SO",
            self.config.alerts.event_log_source,
            "/D",
            msg,
        ]
        try:
            proc = subprocess.run(  # noqa: S603 - 固定参数，无用户输入拼接
                cmd, capture_output=True, text=True, timeout=15, check=False
            )
        except Exception as exc:  # noqa: BLE001
            self._log_warn(
                "alert_event_log_failed", f"事件日志写入失败（{type(exc).__name__}）"
            )
            return False
        if proc.returncode != 0:
            self._log_warn(
                "alert_event_log_failed",
                "事件日志写入失败（可能需要管理员权限）",
                returncode=proc.returncode,
            )
            return False
        return True

    def _send_toast(self, record: RunRecord) -> bool:
        """发送 Windows toast 通知（PowerShell BurntToast 不可用时静默降级）。"""
        if os.name != "nt":
            return False
        title = f"A股量化自动化 {record.state.value}"
        body = (
            f"{record.task_type.value} {record.as_of_date.isoformat()} "
            f"退出码 {record.exit_code}（模拟运行）"
        )
        script = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications,"
            " ContentType = WindowsRuntime] > $null; "
            "$t = [Windows.UI.Notifications.ToastNotificationManager]::"
            "GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
            f"$t.GetElementsByTagName('text')[0].AppendChild($t.CreateTextNode('{title}')) > $null; "
            f"$t.GetElementsByTagName('text')[1].AppendChild($t.CreateTextNode('{body}')) > $null; "
            "[Windows.UI.Notifications.ToastNotificationManager]::"
            "CreateToastNotifier('AShareQuantAutomation').Show("
            "[Windows.UI.Notifications.ToastNotification]::new($t))"
        )
        try:
            proc = subprocess.run(  # noqa: S603
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except Exception as exc:  # noqa: BLE001
            self._log_warn("alert_toast_failed", f"toast 失败（{type(exc).__name__}）")
            return False
        return proc.returncode == 0

    # -- 日志辅助 ------------------------------------------------------- #

    def _log_info(self, event: str, message: str, **detail: Any) -> None:
        if self.logger is not None:
            self.logger.info(event, message, **detail)

    def _log_warn(self, event: str, message: str, **detail: Any) -> None:
        if self.logger is not None:
            self.logger.warning(event, message, **detail)
