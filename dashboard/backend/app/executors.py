"""命令安全执行骨架（UI-G2 可操作工作台）。

安全设计（全部继承 UI-G1 并升级为真实执行）：
- 不使用 shell=True：一律 ``subprocess.run(argv, shell=False)``
- 每个动作映射到**固定 argv 数组**；用户只能从白名单选动作与合法参数
- 用户不能提供任意命令、任意参数、任意工作目录或任意文件系统路径
- 写操作沿用登录 + CSRF + 一次性确认令牌 + 动作白名单
- 全局单任务锁：写入型任务串行执行，避免并发修改状态与报告目录
- 超时上限与 stdout/stderr 字节上限
- 错误信息去敏：不泄露绝对路径、环境变量、密钥或完整堆栈

动作 -> CLI 映射（唯一权威来源，与 ``automation cli`` 语义一致）：
    verify         -> ashare-quant automation verify
    daily          -> ashare-quant automation daily --date <date>
    weekly         -> ashare-quant automation weekly --date <date>
    rerun          -> ashare-quant automation rerun --task <task> --date <date>
    backfill       -> 逐日调用 daily（由 JobManager 拆解，不走单条 argv）
"""

from __future__ import annotations

import asyncio
import inspect
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import (
    ACTION_OUTPUT_MAX_BYTES,
    ACTION_TIMEOUT_SECONDS,
    DashboardConfig,
)
from .errors import DashboardError
from .security import ALLOWED_ACTIONS, FORBIDDEN_ACTIONS

# 日期严格校验：YYYY-MM-DD 且为真实日期
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 非交易日跳过时的退出码（automation daily 对非交易日 SKIPPED 以 0 退出）
EXIT_OK = 0
EXIT_GENERIC_ERROR = 1

# 写操作固定超时（秒）：verify 短、daily/weekly/backfill 长
TIMEOUT_VERIFY = 60
TIMEOUT_RUN = 600
TIMEOUT_BACKFILL = 3600


@dataclass
class ActionResult:
    ok: bool
    action: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    duration_ms: int = 0
    mock: bool = False
    timed_out: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    def truncated_stdout(self) -> str:
        return _truncate(self.stdout, ACTION_OUTPUT_MAX_BYTES)

    def truncated_stderr(self) -> str:
        return _truncate(self.stderr, ACTION_OUTPUT_MAX_BYTES)

    def to_public_dict(self) -> dict[str, Any]:
        """对外返回的安全表示：截断输出、无路径、无密钥。"""
        return {
            "action": self.action,
            "ok": self.ok,
            "stdout": self.truncated_stdout(),
            "stderr": self.truncated_stderr(),
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "mock": self.mock,
            "timed_out": self.timed_out,
        }


def _truncate(text: str, limit: int) -> str:
    if len(text.encode("utf-8")) <= limit:
        return text
    head = text.encode("utf-8")[:limit]
    return head.decode("utf-8", errors="replace") + "\n...[输出已截断]"


def _sanitize_output(text: str) -> str:
    """去掉绝对路径与疑似密钥片段，避免日志/接口泄露敏感信息。"""
    text = re.sub(r"[A-Za-z]:\\[^\s\"']+", "<PATH>", text)
    text = re.sub(r"(?i)(password|secret|token|api[_-]?key)\s*[:=]\s*\S+", r"\1=<REDACTED>", text)
    return text


def validate_date_arg(value: str) -> str:
    """校验日期参数：必须为合法 YYYY-MM-DD。失败抛 DashboardError。"""
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise DashboardError(
            "invalid_date", f"日期参数非法: {value!r}（需要 YYYY-MM-DD）", status_code=400
        )
    import datetime

    try:
        datetime.date.fromisoformat(value)
    except ValueError:
        raise DashboardError(
            "invalid_date", f"日期参数非法: {value!r}（不是真实日期）", status_code=400
        )
    return value


class SubprocessExecutor:
    """真实 subprocess 执行器：固定 argv、无 shell、白名单、锁、超时、截断。"""

    # 动作 -> 是否写操作（写操作占用全局串行锁；verify 为只读可并发）
    WRITE_ACTIONS = ("daily", "weekly", "rerun", "backfill")

    # 动作 -> 固定 argv 模板（供审计与测试使用；date/task 为白名单参数占位）
    ACTION_ARGV: dict[str, list[str]] = {
        "verify": ["python", "-m", "ashare_quant.cli", "automation", "verify"],
        "daily": ["python", "-m", "ashare_quant.cli", "automation", "daily", "--date", "<date>"],
        "weekly": ["python", "-m", "ashare_quant.cli", "automation", "weekly", "--date", "<date>"],
        "rerun": ["python", "-m", "ashare_quant.cli", "automation", "rerun", "--task", "<task>", "--date", "<date>"],
        "backfill": ["python", "-m", "ashare_quant.cli", "automation", "daily", "--date", "<date>"],
    }

    def __init__(
        self,
        config: DashboardConfig,
        *,
        python_exe: str | None = None,
        project_root: Path | None = None,
        timeout_verify: float = TIMEOUT_VERIFY,
        timeout_run: float = TIMEOUT_RUN,
        max_output: int = ACTION_OUTPUT_MAX_BYTES,
    ) -> None:
        self.config = config
        # 固定可执行文件：优先项目 venv，回退当前解释器
        self._python_exe = python_exe or self._resolve_python()
        self._project_root = (project_root or default_project_root()).resolve()
        self.timeout_verify = timeout_verify
        self.timeout_run = timeout_run
        self.max_output = max_output
        self._lock = threading.Lock()
        self._busy = False

    # ---- 可执行文件解析 ----

    @staticmethod
    def _resolve_python() -> str:
        """固定使用项目 .venv 解释器；不存在时回退当前解释器。"""
        root = default_project_root()
        venv_candidates = (
            root / ".venv" / "Scripts" / "python.exe",
            root / ".venv" / "bin" / "python",
        )
        for cand in venv_candidates:
            if cand.is_file():
                return str(cand)
        return sys.executable

    # ---- 白名单 ----

    def validate_action(self, action: str) -> None:
        if action not in ALLOWED_ACTIONS:
            raise DashboardError("action_not_allowed", f"动作不允许: {action}", status_code=403)
        if action in FORBIDDEN_ACTIONS:
            raise DashboardError("action_forbidden", f"动作被永久禁止: {action}", status_code=403)

    def is_busy(self) -> bool:
        return self._busy

    def is_write_action(self, action: str) -> bool:
        return action in self.WRITE_ACTIONS

    # ---- argv 构造（固定映射，用户不可控部分） ----

    def build_argv(self, action: str, *, date: str | None = None, task: str | None = None) -> list[str]:
        """按动作构造固定 argv。除白名单动作外不接受任何自定义参数。"""
        self.validate_action(action)
        cli = [self._python_exe, "-m", "ashare_quant.cli", "automation"]
        if action == "verify":
            return cli + ["verify"]
        if action == "daily":
            if not date:
                raise DashboardError("missing_date", "运行每日任务必须指定日期", status_code=400)
            return cli + ["daily", "--date", validate_date_arg(date)]
        if action == "weekly":
            # weekly 支持指定日期；缺省则用今天（由 CLI 决定）
            args = cli + ["weekly"]
            if date:
                args += ["--date", validate_date_arg(date)]
            return args
        if action == "rerun":
            if task not in ("daily", "weekly"):
                raise DashboardError("invalid_task", f"重跑任务必须为 daily 或 weekly，得到 {task!r}", status_code=400)
            if not date:
                raise DashboardError("missing_date", "重跑必须指定业务日期", status_code=400)
            return cli + ["rerun", "--task", task, "--date", validate_date_arg(date)]
        raise DashboardError("action_not_allowed", f"动作不允许: {action}", status_code=403)

    # ---- 执行 ----

    async def execute(
        self,
        action: str,
        *,
        date: str | None = None,
        task: str | None = None,
        timeout: float | None = None,
        dry_run: bool = False,
    ) -> ActionResult:
        """执行白名单动作；写操作先获取全局串行锁。"""
        self.validate_action(action)
        argv = self.build_argv(action, date=date, task=task)
        effective_timeout = timeout or (
            self.timeout_verify if action == "verify" else self.timeout_run
        )
        if dry_run:
            return ActionResult(
                ok=True,
                action=action,
                stdout="[dry-run] 命令 argv: " + " ".join(argv),
                exit_code=0,
                mock=False,
            )

        if self.is_write_action(action):
            if not self._lock.acquire(blocking=False):
                raise DashboardError(
                    "operation_busy", "已有写入任务正在执行，请稍后再试", status_code=409
                )
            self._busy = True
        start = time.monotonic()
        try:
            return await asyncio.to_thread(
                self._run_subprocess, argv, effective_timeout
            )
        finally:
            if self.is_write_action(action):
                self._busy = False
                self._lock.release()

    def _run_subprocess(self, argv: list[str], timeout: float) -> ActionResult:
        """同步 subprocess 调用（在 to_thread 中运行）。"""
        action = " ".join(argv)
        start = time.monotonic()
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                shell=False,
                cwd=str(self._project_root),
                timeout=timeout,
                env=self._safe_env(),
            )
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            err = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            return ActionResult(
                ok=False,
                action=action,
                stdout=_sanitize_output(out),
                stderr=_sanitize_output(err) + "\n...[进程超时被终止]",
                exit_code=124,
                timed_out=True,
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        except FileNotFoundError:
            return ActionResult(
                ok=False,
                action=action,
                stderr="找不到可执行解释器（venv 缺失）",
                exit_code=127,
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        return ActionResult(
            ok=proc.returncode == 0,
            action=action,
            stdout=_sanitize_output(stdout),
            stderr=_sanitize_output(stderr),
            exit_code=proc.returncode,
            duration_ms=int((time.monotonic() - start) * 1000),
        )

    def _safe_env(self) -> dict[str, str]:
        """构造最小化子进程环境：保留 PATH 与必要项，去掉敏感环境变量。"""
        env = dict(os.environ)
        for key in list(env):
            up = key.upper()
            if any(tok in up for tok in ("TOKEN", "SECRET", "PASSWORD", "API_KEY", "SESSION")):
                env.pop(key, None)
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env.setdefault("PYTHONUTF8", "1")
        return env


def default_project_root() -> Path:
    """Dashboard 所属仓库根目录。"""
    return Path(__file__).resolve().parents[3]


class MockExecutor:
    """旧测试兼容的 mock 执行器（仅测试用，生产使用 SubprocessExecutor）。

    保留 ACTION_ARGV 与 execute 骨架以便既有 test_actions 等测试继续运行；
    生产代码不再实例化本类。
    """

    ACTION_ARGV: dict[str, list[str]] = SubprocessExecutor.ACTION_ARGV

    def __init__(
        self,
        timeout: float = ACTION_TIMEOUT_SECONDS,
        max_output: int = ACTION_OUTPUT_MAX_BYTES,
    ) -> None:
        self.timeout = timeout
        self.max_output = max_output
        self._lock = threading.Lock()
        self._busy = False

    def validate_action(self, action: str) -> None:
        if action not in ALLOWED_ACTIONS:
            raise DashboardError("action_not_allowed", f"动作不允许: {action}", status_code=403)
        if action in FORBIDDEN_ACTIONS:
            raise DashboardError("action_forbidden", f"动作被永久禁止: {action}", status_code=403)

    def is_busy(self) -> bool:
        return self._busy

    async def execute(self, action: str, duration: float = 0.05) -> ActionResult:
        """mock 执行：不运行真实命令，返回结构化结果（仅测试）。"""
        self.validate_action(action)
        if not self._lock.acquire(blocking=False):
            raise DashboardError("operation_busy", "已有操作正在执行，请稍后再试", status_code=409)
        self._busy = True
        start = time.monotonic()
        try:
            if duration > self.timeout:
                await asyncio.sleep(min(duration, 0.2))
                raise DashboardError("action_timeout", f"操作超时（上限 {self.timeout}s）", status_code=504)
            await asyncio.sleep(duration)
            return ActionResult(
                ok=True,
                action=action,
                stdout=f"[mock] {action} 已安全执行（测试替身，不运行真实命令）",
                exit_code=0,
                duration_ms=int((time.monotonic() - start) * 1000),
                mock=True,
            )
        finally:
            self._busy = False
            self._lock.release()


__all__ = [
    "ActionResult",
    "SubprocessExecutor",
    "MockExecutor",
    "validate_date_arg",
    "default_project_root",
    "TIMEOUT_VERIFY",
    "TIMEOUT_RUN",
    "TIMEOUT_BACKFILL",
]
