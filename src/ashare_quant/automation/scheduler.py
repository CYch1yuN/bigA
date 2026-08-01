"""Phase 4 Windows 任务计划命令生成。

本模块只负责**生成** ``schtasks`` 命令，不依赖 Windows 即可单测（dry-run 友好）。
真正的注册由 ``scripts/install_scheduler.ps1`` 在本机执行；本模块产出的命令字符串
与 PowerShell 脚本保持语义一致，可作为验证基准。

两条任务：
- ``<prefix>-Daily``    每个交易日盘后（默认 18:30）跑每日管线
- ``<prefix>-Weekly``   每周六（默认 09:00）跑每周汇总

安全边界：动作指向 ``scripts/run_daily.ps1`` / ``run_weekly.ps1``，
最终调用 ``ashare-quant automation daily|weekly``——只产出研究信号与模拟账户报告，
绝不连接券商、绝不触碰真实资金。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .config import AutomationConfig


@dataclass(frozen=True)
class ScheduledTask:
    """一个计划任务的描述。"""

    name: str
    trigger: str  # "DAILY" / "WEEKLY"
    time: str  # HH:MM
    day: Optional[str]  # WEEKLY 的星期（MON..SUN）
    action: str  # 可执行命令（含参数）
    working_directory: str
    run_level: str = "LIMITED"


@dataclass
class SchedulerPlan:
    """一组计划任务及其渲染后的 ``schtasks`` 命令。"""

    tasks: list[ScheduledTask] = field(default_factory=list)

    def schtasks_commands(self, *, force: bool = False) -> list[str]:
        """渲染为可执行的 ``schtasks`` 命令列表。"""
        cmds: list[str] = []
        for t in self.tasks:
            if force:
                cmds.append(f'schtasks /Delete /TN "{t.name}" /F')
            base = [
                "schtasks",
                "/Create",
                f'/TN "{t.name}"',
                f"/SC {t.trigger}",
                f'/ST {t.time}',
                f"/RL {t.run_level}",
                f'/TR "{t.action}"',
                "/F",
            ]
            if t.trigger == "WEEKLY" and t.day:
                base.insert(4, f"/D {t.day}")
            cmds.append(" ".join(base))
        return cmds

    def render(self, *, force: bool = False) -> str:
        return "\n".join(self.schtasks_commands(force=force))

    def task_names(self) -> list[str]:
        return [t.name for t in self.tasks]


def default_repo_root() -> Path:
    """返回仓库根目录（基于本文件位置推导）。"""
    return Path(__file__).resolve().parents[3]


def _python_exe(repo_root: Path) -> Path:
    return repo_root / ".venv" / "Scripts" / "python.exe"


def build_scheduler_plan(
    config: AutomationConfig, *, repo_root: Optional[Path] = None
) -> SchedulerPlan:
    """根据自动化配置生成计划任务方案。"""
    root = Path(repo_root) if repo_root is not None else default_repo_root()
    prefix = config.scheduler.task_name_prefix
    ps = "powershell.exe"
    run_daily = root / "scripts" / "run_daily.ps1"
    run_weekly = root / "scripts" / "run_weekly.ps1"
    daily_action = (
        f'{ps} -NoProfile -ExecutionPolicy Bypass -File "{run_daily}"'
    )
    weekly_action = (
        f'{ps} -NoProfile -ExecutionPolicy Bypass -File "{run_weekly}"'
    )
    tasks = [
        ScheduledTask(
            name=f"{prefix}-Daily",
            trigger="DAILY",
            time=config.scheduler.daily_time,
            day=None,
            action=daily_action,
            working_directory=str(root),
            run_level=config.scheduler.run_level,
        ),
        ScheduledTask(
            name=f"{prefix}-Weekly",
            trigger="WEEKLY",
            time=config.scheduler.weekly_time,
            day=config.scheduler.weekly_day,
            action=weekly_action,
            working_directory=str(root),
            run_level=config.scheduler.run_level,
        ),
    ]
    return SchedulerPlan(tasks=tasks)
