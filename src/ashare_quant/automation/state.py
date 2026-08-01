"""运行状态仓库：原子写入 + 崩溃安全。

落盘布局::

    <state_dir>/
        runs/daily/2026-07-31.json          每个业务日一份运行记录
        runs/weekly/2026-07-31.json
        latest-daily.json                    最近一次每日运行（软链语义的副本）
        latest-weekly.json
        accounts/paper-steady.json           模拟账户状态
        accounts/paper-aggressive.json
        pending-signals.json                 待成交研究信号（收盘生成，次日开盘撮合）
        automation.lock                      运行锁

崩溃安全约定：

1. 所有写入都是"写临时文件 + fsync + ``os.replace``"，读者永远看不到半截文件。
2. ``RUNNING`` **不是终态**。进程崩溃后记录会停留在 ``RUNNING``，
   下一次运行通过 ``recover_interrupted()`` 显式判定为 ``FAILED``，
   绝不会被当成 ``SUCCESS``。
3. 终态记录默认不允许被非终态覆盖，防止乱序写入回退状态。
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional

from .models import (
    RunRecord,
    RunState,
    SimulatedAccountState,
    StepResult,
    StepStatus,
    TERMINAL_STATES,
    TaskType,
)

__all__ = [
    "atomic_write_text",
    "atomic_write_json",
    "StateStore",
]


def atomic_write_text(path: str | Path, content: str, *, encoding: str = "utf-8") -> Path:
    """原子写入文本文件。

    先写同目录下的临时文件并 fsync，再 ``os.replace`` 覆盖目标，
    保证读者要么看到旧内容、要么看到完整新内容。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, target)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return target


def atomic_write_json(path: str | Path, payload: Any, *, indent: int = 2) -> Path:
    """原子写入 JSON 文件（UTF-8，保留中文）。"""
    text = json.dumps(payload, ensure_ascii=False, indent=indent, sort_keys=False)
    return atomic_write_text(path, text + "\n")


class StateStore:
    """运行状态与模拟账户状态仓库。"""

    def __init__(self, state_dir: str | Path) -> None:
        self.state_dir = Path(state_dir)

    # -- 路径 ---------------------------------------------------------- #

    def runs_dir(self, task_type: TaskType) -> Path:
        return self.state_dir / "runs" / task_type.value

    def run_path(self, task_type: TaskType, as_of_date: date) -> Path:
        return self.runs_dir(task_type) / f"{as_of_date.isoformat()}.json"

    def latest_path(self, task_type: TaskType) -> Path:
        return self.state_dir / f"latest-{task_type.value}.json"

    def accounts_dir(self) -> Path:
        return self.state_dir / "accounts"

    def account_path(self, account_id: str) -> Path:
        return self.accounts_dir() / f"{account_id}.json"

    def ensure_dirs(self) -> None:
        """创建所需目录。"""
        for task in TaskType:
            self.runs_dir(task).mkdir(parents=True, exist_ok=True)
        self.accounts_dir().mkdir(parents=True, exist_ok=True)

    # -- 运行记录 ------------------------------------------------------ #

    def save_run(self, record: RunRecord, *, update_latest: bool = True) -> Path:
        """原子保存运行记录。"""
        path = self.run_path(record.task_type, record.as_of_date)
        atomic_write_json(path, record.to_dict())
        if update_latest:
            atomic_write_json(self.latest_path(record.task_type), record.to_dict())
        return path

    def load_run(
        self, task_type: TaskType, as_of_date: date
    ) -> Optional[RunRecord]:
        """读取指定业务日的运行记录。"""
        path = self.run_path(task_type, as_of_date)
        return self._load_record(path)

    def load_latest(self, task_type: TaskType) -> Optional[RunRecord]:
        """读取最近一次运行记录。"""
        return self._load_record(self.latest_path(task_type))

    def _load_record(self, path: Path) -> Optional[RunRecord]:
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        try:
            return RunRecord.from_dict(raw)
        except (KeyError, ValueError):
            return None

    def list_runs(
        self,
        task_type: TaskType,
        *,
        limit: Optional[int] = None,
        states: Optional[Iterable[RunState]] = None,
    ) -> list[RunRecord]:
        """按业务日倒序列出运行记录。"""
        directory = self.runs_dir(task_type)
        if not directory.exists():
            return []
        wanted = set(states) if states is not None else None
        records: list[RunRecord] = []
        for path in sorted(directory.glob("*.json"), reverse=True):
            rec = self._load_record(path)
            if rec is None:
                continue
            if wanted is not None and rec.state not in wanted:
                continue
            records.append(rec)
            if limit is not None and len(records) >= limit:
                break
        return records

    def find_by_run_id(self, run_id: str) -> Optional[RunRecord]:
        """按 run_id 查找运行记录。"""
        for task in TaskType:
            for rec in self.list_runs(task):
                if rec.run_id == run_id:
                    return rec
        return None

    def save_run_guarded(self, record: RunRecord) -> tuple[Path, bool]:
        """带回退保护的保存。

        若磁盘上已有同业务日的**终态**记录，而本次要写入的是非终态，
        则拒绝覆盖（返回 ``(path, False)``），避免状态回退。
        """
        existing = self.load_run(record.task_type, record.as_of_date)
        if (
            existing is not None
            and existing.state in TERMINAL_STATES
            and record.state not in TERMINAL_STATES
            and existing.run_id == record.run_id
        ):
            return self.run_path(record.task_type, record.as_of_date), False
        return self.save_run(record), True

    # -- 崩溃恢复 ------------------------------------------------------- #

    def recover_interrupted(
        self,
        task_type: TaskType,
        *,
        now: Optional[datetime] = None,
        max_running_seconds: int = 21600,
    ) -> list[RunRecord]:
        """将超时仍处于 ``RUNNING`` 的记录显式判定为 ``FAILED``。

        这是崩溃安全的关键一环：进程被强杀 / 断电后，记录停在 ``RUNNING``；
        本方法把它改写为 ``FAILED`` 并追加恢复步骤，**绝不**改写为 ``SUCCESS``。

        Args:
            task_type: 任务类型。
            now: 当前时间（测试可注入）。
            max_running_seconds: 超过该时长仍在 RUNNING 视为中断。

        Returns:
            被判定为失败的记录列表。
        """
        current = now or datetime.now()
        deadline = timedelta(seconds=max_running_seconds)
        recovered: list[RunRecord] = []
        for rec in self.list_runs(task_type, states=[RunState.RUNNING]):
            started = rec.started_at or current
            if current - started < deadline:
                continue
            rec.state = RunState.FAILED
            rec.finished_at = current
            rec.message = (
                f"检测到中断运行：记录在 {started.isoformat(timespec='seconds')} "
                f"进入 RUNNING 后未写入终态，判定为 FAILED（不视为成功）"
            )
            rec.add_step(
                StepResult(
                    name="crash_recovery",
                    status=StepStatus.FAILED,
                    started_at=started,
                    finished_at=current,
                    detail={
                        "reason": "interrupted_run_detected",
                        "max_running_seconds": max_running_seconds,
                    },
                    error="运行进程异常终止，未写入终态",
                )
            )
            self.save_run(rec, update_latest=False)
            latest = self.load_latest(task_type)
            if latest is not None and latest.run_id == rec.run_id:
                atomic_write_json(self.latest_path(task_type), rec.to_dict())
            recovered.append(rec)
        return recovered

    # -- 待成交信号 ------------------------------------------------------ #
    #
    # 策略在收盘后生成信号，最早在**下一交易日开盘**成交。运行当日并不存在
    # 下一交易日的行情，因此信号必须落盘等待，而不是就地假装成交。
    # 这个文件就是那段"隔夜等待"的唯一凭证。

    def pending_signals_path(self) -> Path:
        return self.state_dir / "pending-signals.json"

    def save_pending_signals(self, payload: dict[str, Any]) -> Path:
        """原子保存待成交信号。"""
        return atomic_write_json(self.pending_signals_path(), payload)

    def load_pending_signals(self) -> Optional[dict[str, Any]]:
        """读取待成交信号；文件缺失或损坏时返回 ``None``。"""
        path = self.pending_signals_path()
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return raw if isinstance(raw, dict) else None

    def clear_pending_signals(self) -> None:
        """清空待成交信号（写入空载荷而非删除，保留可审计痕迹）。"""
        atomic_write_json(
            self.pending_signals_path(),
            {"schema_version": 1, "signal_date": None, "signals": []},
        )

    # -- 模拟账户 ------------------------------------------------------- #

    def save_account(self, account: SimulatedAccountState) -> Path:
        """原子保存模拟账户状态。"""
        path = self.account_path(account.account_id)
        atomic_write_json(path, account.to_dict())
        return path

    def load_account(self, account_id: str) -> Optional[SimulatedAccountState]:
        """读取模拟账户状态。"""
        path = self.account_path(account_id)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        try:
            return SimulatedAccountState.from_dict(raw)
        except (KeyError, ValueError):
            return None

    def list_accounts(self) -> list[SimulatedAccountState]:
        """列出全部模拟账户状态。"""
        directory = self.accounts_dir()
        if not directory.exists():
            return []
        result: list[SimulatedAccountState] = []
        for path in sorted(directory.glob("*.json")):
            acc = self.load_account(path.stem)
            if acc is not None:
                result.append(acc)
        return result
