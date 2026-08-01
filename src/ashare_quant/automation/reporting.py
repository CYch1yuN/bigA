"""Phase 4 报告渲染：把运行结果写成人能读、机器也能读的产物。

产物布局
--------
::

    reports/phase-4/
      daily/2026-07-31/
        run.json              运行记录（状态机、步骤、指纹）
        signals.json          研究信号（含资格标记）
        simulated-orders.json 模拟订单流水
        accounts.json         模拟账户快照
        quality.json          数据质量报告
        daily-report.md       人读摘要
      weekly/2026-W31/
        ...
      latest-daily.md         最近一次每日摘要（软副本）
      latest-failure.md       最近一次失败报告（由 alerts 写）

不可省略的声明
--------------
每一份 Markdown 产物都必须带 ``DISCLAIMER_BLOCK``。
这不是形式主义——这些文件会被复制、转发、脱离上下文阅读，
声明必须跟着文件走，而不是跟着对话走。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from .config import AutomationConfig
from .models import (
    RunRecord,
    RunState,
    SimulatedAccountState,
    SimulatedOrderRecord,
    TaskType,
)
from .state import atomic_write_json, atomic_write_text

__all__ = [
    "DISCLAIMER_BLOCK",
    "STATE_LABELS",
    "ResultPaths",
    "result_paths",
    "iso_week_key",
    "write_json_artifact",
    "render_daily_markdown",
    "render_weekly_markdown",
    "render_status_markdown",
]


#: 所有 Markdown 产物统一附带的边界声明。
DISCLAIMER_BLOCK = """> **边界声明**
>
> - 本报告由本机自动化研究系统生成，内容为**研究信号**与**模拟账户**记录。
> - 所有订单均为**纸面模拟**，未连接任何券商、未涉及任何真实资金。
> - 稳健轨结论为 `NOT_ELIGIBLE_FOR_LIVE_TRADING`，激进轨结论为 `SIMULATION_ONLY`；
>   两者**均未获得实盘授权**。
> - 本报告不构成投资建议，不构成买卖推荐，不可作为交易决策依据。
"""


#: 状态的人读标签。
STATE_LABELS: dict[RunState, str] = {
    RunState.PENDING: "待运行",
    RunState.RUNNING: "运行中（非终态）",
    RunState.SUCCESS: "成功",
    RunState.FAILED: "失败",
    RunState.SKIPPED_NON_TRADING_DAY: "跳过（非交易日）",
    RunState.SKIPPED_DATA_UNAVAILABLE: "跳过（数据不可用）",
    RunState.BLOCKED_DATA_QUALITY: "阻断（数据质量）",
    RunState.BLOCKED_LOCKED: "阻断（并发锁）",
    RunState.BLOCKED_NOT_ELIGIBLE: "阻断（资格未通过）",
}


# ---------------------------------------------------------------------- #
# 路径
# ---------------------------------------------------------------------- #


def iso_week_key(d: date) -> str:
    """ISO 周标识，如 ``2026-W31``。"""
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


@dataclass(frozen=True)
class ResultPaths:
    """一次运行的结果目录与关键文件路径。"""

    root: Path
    run_json: Path
    report_md: Path
    signals_json: Path
    orders_json: Path
    accounts_json: Path
    quality_json: Path
    latest_md: Path

    def ensure(self) -> "ResultPaths":
        self.root.mkdir(parents=True, exist_ok=True)
        return self


def result_paths(
    config: AutomationConfig, *, task_type: TaskType, as_of_date: date
) -> ResultPaths:
    """按任务类型与业务日推导结果目录。

    每日按日期分目录，每周按 ISO 周分目录——重跑同一天会覆盖同一目录，
    天然幂等，不会堆出一地时间戳垃圾。
    """
    if task_type is TaskType.WEEKLY:
        bucket = iso_week_key(as_of_date)
        sub = "weekly"
        report_name = "weekly-report.md"
        latest = config.reports_dir / "latest-weekly.md"
    else:
        bucket = as_of_date.isoformat()
        sub = "daily"
        report_name = "daily-report.md"
        latest = config.reports_dir / "latest-daily.md"

    root = config.reports_dir / sub / bucket
    return ResultPaths(
        root=root,
        run_json=root / "run.json",
        report_md=root / report_name,
        signals_json=root / "signals.json",
        orders_json=root / "simulated-orders.json",
        accounts_json=root / "accounts.json",
        quality_json=root / "quality.json",
        latest_md=latest,
    )


# ---------------------------------------------------------------------- #
# 序列化
# ---------------------------------------------------------------------- #


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if isinstance(obj, Path):
        return obj.as_posix()
    if hasattr(obj, "value"):  # Enum
        return obj.value
    return str(obj)


def write_json_artifact(path: Path, payload: Any) -> Path:
    """原子写 JSON 产物（Decimal / date 自动转字符串）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default)
    return atomic_write_text(path, text + "\n")


# ---------------------------------------------------------------------- #
# Markdown 片段
# ---------------------------------------------------------------------- #


def _fmt(value: Any, dash: str = "—") -> str:
    if value is None or value == "":
        return dash
    return str(value)


def _run_header(record: RunRecord, title: str) -> list[str]:
    label = STATE_LABELS.get(record.state, record.state.value)
    lines = [
        f"# {title}",
        "",
        DISCLAIMER_BLOCK,
        "",
        "## 运行概览",
        "",
        "| 项目 | 值 |",
        "| --- | --- |",
        f"| 运行标识 | `{record.run_id}` |",
        f"| 任务类型 | {record.task_type.value} |",
        f"| 业务日 | {record.as_of_date.isoformat()} |",
        f"| 终态 | **{label}** (`{record.state.value}`) |",
        f"| 退出码 | {record.exit_code} |",
        f"| 尝试次数 | {record.attempt} |",
        f"| 代码提交 | `{record.code_commit}` |",
        f"| 配置哈希 | `{record.config_hash}` |",
        f"| 输入哈希 | `{record.input_hash}` |",
        f"| 开始时间 | {_fmt(record.started_at)} |",
        f"| 结束时间 | {_fmt(record.finished_at)} |",
        f"| 结论 | {_fmt(record.message)} |",
        "",
    ]
    return lines


def _steps_table(record: RunRecord) -> list[str]:
    lines = ["## 步骤明细", "", "| # | 步骤 | 状态 | 耗时(s) | 说明 |", "| --- | --- | --- | --- | --- |"]
    if not record.steps:
        lines.append("| — | — | — | — | 无步骤记录 |")
    for i, s in enumerate(record.steps, 1):
        note = s.error or _summarize_detail(s.detail)
        lines.append(
            f"| {i} | `{s.name}` | {s.status.value} | "
            f"{_fmt(s.duration_seconds, '—')} | {_fmt(note)} |"
        )
    lines.append("")
    return lines


def _summarize_detail(detail: dict[str, Any], limit: int = 4) -> str:
    if not detail:
        return ""
    parts = []
    for k, v in list(detail.items())[:limit]:
        if isinstance(v, (dict, list)):
            parts.append(f"{k}={len(v)}项")
        else:
            parts.append(f"{k}={v}")
    return ", ".join(parts)


def _data_section(provenance: Optional[dict[str, Any]]) -> list[str]:
    lines = ["## 数据出处", ""]
    if not provenance:
        lines += ["未加载行情数据。", ""]
        return lines
    online = provenance.get("online")
    synthetic = provenance.get("synthetic")
    badge = "线上抓取" if online else "**非线上抓取**"
    if synthetic:
        badge += "、**合成样本**"
    lines += [
        "| 项目 | 值 |",
        "| --- | --- |",
        f"| 数据源 | `{provenance.get('source')}` ({badge}) |",
        f"| 行数 | {provenance.get('rows')} |",
        f"| 标的数 | {provenance.get('symbols')} |",
        f"| 覆盖区间 | {_fmt(provenance.get('date_start'))} ~ {_fmt(provenance.get('date_end'))} |",
        f"| 加载时间 | {_fmt(provenance.get('loaded_at'))} |",
        "",
    ]
    notes = provenance.get("notes") or []
    if notes:
        lines.append("数据源备注：")
        lines.append("")
        for n in notes:
            lines.append(f"- {n}")
        lines.append("")
    return lines


def _quality_section(quality: Optional[dict[str, Any]]) -> list[str]:
    lines = ["## 数据质量闸门", ""]
    if not quality:
        lines += ["未执行质量检查。", ""]
        return lines
    summary = quality.get("summary", {})
    lines += [
        "| 项目 | 值 |",
        "| --- | --- |",
        f"| 严重问题 | **{summary.get('critical', 0)}** |",
        f"| 警告 | {summary.get('warning', 0)} |",
        f"| 检查行数 | {summary.get('rows_checked', '—')} |",
        f"| 是否阻断下游 | {'**是**' if quality.get('has_critical') else '否'} |",
        "",
    ]
    issues = quality.get("issues") or []
    if issues:
        lines += ["前 10 条问题：", "", "| 严重度 | 规则 | 说明 |", "| --- | --- | --- |"]
        for issue in issues[:10]:
            lines.append(
                f"| {issue.get('severity')} | `{issue.get('rule', issue.get('code', ''))}` | "
                f"{_fmt(issue.get('message'))} |"
            )
        lines.append("")
    return lines


def _orders_table(orders: Sequence[SimulatedOrderRecord]) -> list[str]:
    lines = [
        "| 账户 | 轨道 | 标的 | 方向 | 数量 | 状态 | 成交价 | 费用合计 | 现金变动 | 拒因 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    if not orders:
        lines.append("| — | — | — | — | — | — | — | — | — | 本期无模拟订单 |")
    for o in orders:
        lines.append(
            f"| {o.account_id} | {o.strategy_track.value} | `{o.symbol}` | {o.side} | "
            f"{o.quantity} | {o.status} | {_fmt(o.fill_price)} | {_fmt(o.total_cost)} | "
            f"{_fmt(o.cash_change)} | {_fmt(o.reject_reason)} |"
        )
    lines.append("")
    return lines


def _accounts_table(accounts: Sequence[SimulatedAccountState], equity: dict[str, Any]) -> list[str]:
    lines = [
        "| 账户 | 轨道 | 资格结论 | 现金 | 持仓市值 | 总权益 | 持仓数 | 已观察交易日 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for a in accounts:
        e = equity.get(a.account_id, {})
        lines.append(
            f"| {a.account_id} | {a.strategy_track.value} | `{a.eligibility_status.value}` | "
            f"{_fmt(e.get('cash', a.cash))} | {_fmt(e.get('position_value'))} | "
            f"{_fmt(e.get('total_equity'))} | {len(a.positions)} | {a.observation_days} |"
        )
    lines.append("")
    return lines


# ---------------------------------------------------------------------- #
# 每日报告
# ---------------------------------------------------------------------- #


def render_daily_markdown(
    record: RunRecord,
    *,
    provenance: Optional[dict[str, Any]] = None,
    quality: Optional[dict[str, Any]] = None,
    signals: Optional[Sequence[dict[str, Any]]] = None,
    orders: Optional[Sequence[SimulatedOrderRecord]] = None,
    accounts: Optional[Sequence[SimulatedAccountState]] = None,
    equity: Optional[dict[str, Any]] = None,
    observation: Optional[Sequence[dict[str, Any]]] = None,
) -> str:
    """渲染每日运行报告。"""
    lines = _run_header(record, f"每日自动化运行报告 · {record.as_of_date.isoformat()}")
    lines += _steps_table(record)
    lines += _data_section(provenance)
    lines += _quality_section(quality)

    lines += ["## 研究信号", ""]
    sig_list = list(signals or [])
    if not sig_list:
        lines += ["本交易日无新增研究信号（含稳健轨 HOLD_CASH 情形）。", ""]
    else:
        lines += [
            "| 轨道 | 标的 | 方向 | 数量 | 信号日 | 依据 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for s in sig_list:
            lines.append(
                f"| {_fmt(s.get('strategy_track'))} | `{_fmt(s.get('symbol'))}` | "
                f"{_fmt(s.get('side'))} | {_fmt(s.get('quantity'))} | "
                f"{_fmt(s.get('signal_date'))} | {_fmt(s.get('reason'))} |"
            )
        lines.append("")
        lines.append(
            "> 上表为**研究信号**，不是操作建议。信号仅用于推进模拟账户。"
        )
        lines.append("")

    lines += ["## 模拟订单", ""]
    lines += _orders_table(list(orders or []))

    lines += ["## 模拟账户", ""]
    lines += _accounts_table(list(accounts or []), dict(equity or {}))

    if observation:
        lines += ["## 观察窗口进度", "", "| 账户 | 已观察 | 目标 | 剩余 | 进度 |", "| --- | --- | --- | --- | --- |"]
        for o in observation:
            lines.append(
                f"| {o.get('account_id')} | {o.get('observed_trading_days')} | "
                f"{o.get('target_trading_days')} | {o.get('remaining_trading_days')} | "
                f"{o.get('progress_pct')}% |"
            )
        lines.append("")

    if record.artifacts:
        lines += ["## 产物清单", ""]
        for a in record.artifacts:
            lines.append(f"- `{a}`")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------- #
# 每周报告
# ---------------------------------------------------------------------- #


def render_weekly_markdown(
    record: RunRecord,
    *,
    week_key: str,
    daily_runs: Optional[Sequence[RunRecord]] = None,
    accounts: Optional[Sequence[SimulatedAccountState]] = None,
    equity: Optional[dict[str, Any]] = None,
    observation: Optional[Sequence[dict[str, Any]]] = None,
    stats: Optional[dict[str, Any]] = None,
    archive: Optional[dict[str, Any]] = None,
) -> str:
    """渲染每周汇总报告。"""
    lines = _run_header(record, f"每周自动化汇总报告 · {week_key}")
    lines += _steps_table(record)

    lines += ["## 本周每日运行回顾", "", "| 业务日 | 终态 | 退出码 | 尝试 | 结论 |", "| --- | --- | --- | --- | --- |"]
    runs = list(daily_runs or [])
    if not runs:
        lines.append("| — | — | — | — | 本周无每日运行记录 |")
    for r in runs:
        label = STATE_LABELS.get(r.state, r.state.value)
        lines.append(
            f"| {r.as_of_date.isoformat()} | {label} | {r.exit_code} | {r.attempt} | "
            f"{_fmt(r.message)} |"
        )
    lines.append("")

    if stats:
        lines += ["## 本周统计", "", "| 指标 | 值 |", "| --- | --- |"]
        for k, v in stats.items():
            lines.append(f"| {k} | {_fmt(v)} |")
        lines.append("")

    lines += ["## 模拟账户", ""]
    lines += _accounts_table(list(accounts or []), dict(equity or {}))

    if observation:
        lines += [
            "## 60 交易日观察窗口",
            "",
            "| 账户 | 资格结论 | 已观察 | 目标 | 剩余 | 进度 | 是否完成 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for o in observation:
            lines.append(
                f"| {o.get('account_id')} | `{o.get('eligibility_status')}` | "
                f"{o.get('observed_trading_days')} | {o.get('target_trading_days')} | "
                f"{o.get('remaining_trading_days')} | {o.get('progress_pct')}% | "
                f"{'是' if o.get('completed') else '否'} |"
            )
        lines.append("")
        lines.append(
            "> 观察窗口完成**不等于**获得实盘资格。窗口结束后仍需独立复审，"
            "且复审结论不由本自动化系统作出。"
        )
        lines.append("")

    if archive:
        lines += ["## 归档", "", "| 项目 | 值 |", "| --- | --- |"]
        for k, v in archive.items():
            lines.append(f"| {k} | {_fmt(v)} |")
        lines.append("")

    if record.artifacts:
        lines += ["## 产物清单", ""]
        for a in record.artifacts:
            lines.append(f"- `{a}`")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------- #
# 状态摘要
# ---------------------------------------------------------------------- #


def render_status_markdown(
    *,
    daily: Optional[RunRecord],
    weekly: Optional[RunRecord],
    accounts: Sequence[SimulatedAccountState],
    pending_failure: Optional[dict[str, Any]] = None,
    observation: Optional[Sequence[dict[str, Any]]] = None,
) -> str:
    """渲染 ``automation status`` 的人读摘要。"""
    lines = ["# 自动化系统状态", "", DISCLAIMER_BLOCK, "", "## 最近运行", ""]
    lines += ["| 任务 | 业务日 | 终态 | 退出码 | 结束时间 |", "| --- | --- | --- | --- | --- |"]
    for label, rec in (("daily", daily), ("weekly", weekly)):
        if rec is None:
            lines.append(f"| {label} | — | 无记录 | — | — |")
            continue
        state_label = STATE_LABELS.get(rec.state, rec.state.value)
        lines.append(
            f"| {label} | {rec.as_of_date.isoformat()} | {state_label} | "
            f"{rec.exit_code} | {_fmt(rec.finished_at)} |"
        )
    lines.append("")

    lines += ["## 模拟账户", ""]
    lines += _accounts_table(list(accounts), {})

    if observation:
        lines += ["## 观察窗口", "", "| 账户 | 已观察 | 目标 | 进度 |", "| --- | --- | --- | --- |"]
        for o in observation:
            lines.append(
                f"| {o.get('account_id')} | {o.get('observed_trading_days')} | "
                f"{o.get('target_trading_days')} | {o.get('progress_pct')}% |"
            )
        lines.append("")

    lines += ["## 待处理告警", ""]
    if pending_failure:
        lines += [
            f"- 存在未清除的失败标记：`{pending_failure.get('run_id', '未知')}`",
            f"- 终态：`{pending_failure.get('state', '未知')}`",
            f"- 时间：{_fmt(pending_failure.get('created_at'))}",
            "",
        ]
    else:
        lines += ["无。", ""]

    return "\n".join(lines).rstrip() + "\n"
