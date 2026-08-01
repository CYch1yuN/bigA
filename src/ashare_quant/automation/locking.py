"""跨进程运行锁。

保证同一时刻只有一个自动化任务在跑，避免任务计划重复触发、手动重跑与
计划任务撞车导致的状态覆盖与重复下单。

关键实现细节：

- 使用 ``os.open(..., O_CREAT | O_EXCL)`` 原子创建锁文件，避免 TOCTOU 竞态。
- 锁文件内容记录 ``pid / hostname / task_type / as_of_date / started_at /
  code_commit / run_id``，便于人工排查是谁在持锁。
- **活跃锁不可被覆盖**：持锁进程仍存活时，新进程一律拿不到锁。
- 陈旧锁检测：持锁进程已消失（或跨主机且超过阈值）时才允许接管。

Windows 安全提示：``os.kill(pid, 0)`` 在 Windows 上会调用 ``TerminateProcess``
**真的杀掉目标进程**，绝不能用于探活。本模块在 Windows 上改用
``OpenProcess`` + ``GetExitCodeProcess``。
"""
from __future__ import annotations

import json
import os
import socket
import time
from datetime import date, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Callable, Optional

from .models import LockHeldError, LockInfo, TaskType

__all__ = [
    "process_alive",
    "read_lock",
    "RunLock",
    "LockAcquisition",
]

_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# 释放锁时的有界重试：Windows 上杀毒软件 / 搜索索引服务可能在文件刚写完时
# 短暂持有句柄，导致 unlink 抛 PermissionError 或删除延迟可见。若把失败静默
# 吞掉，残留的锁文件会在下一次运行时变成"幽灵锁"，把自己挡在门外。
_RELEASE_MAX_ATTEMPTS = 5
_RELEASE_RETRY_SECONDS = 0.05


def _process_alive_windows(pid: int) -> bool:
    """Windows 探活：OpenProcess + GetExitCodeProcess（只读，绝不终止进程）。"""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = kernel32.OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION, False, wintypes.DWORD(pid)
    )
    if not handle:
        # 打不开句柄：进程不存在，或权限不足。权限不足时保守视为存活。
        err = ctypes.get_last_error()
        ERROR_INVALID_PARAMETER = 87
        return err != ERROR_INVALID_PARAMETER
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True  # 查询失败，保守视为存活
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _process_alive_posix(pid: int) -> bool:
    """POSIX 探活：signal 0 不会真正发送信号。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但无权限
    except OSError:
        return True
    return True


def process_alive(pid: int) -> bool:
    """判断进程是否存活（跨平台，只读探测）。"""
    if pid is None or pid <= 0:
        return False
    if os.name == "nt":
        try:
            return _process_alive_windows(pid)
        except Exception:  # noqa: BLE001 - 探活失败时保守视为存活
            return True
    return _process_alive_posix(pid)


def read_lock(lock_path: str | Path) -> Optional[LockInfo]:
    """读取锁文件内容。

    Returns:
        锁信息；文件不存在或内容损坏时返回 None（损坏锁视为可接管）。
    """
    p = Path(lock_path)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    try:
        return LockInfo.from_dict(raw)
    except (KeyError, ValueError):
        return None


class LockAcquisition:
    """加锁结果描述（用于日志与运行报告）。"""

    __slots__ = ("acquired", "reason", "holder", "stole_stale")

    def __init__(
        self,
        acquired: bool,
        *,
        reason: str = "",
        holder: Optional[LockInfo] = None,
        stole_stale: bool = False,
    ) -> None:
        self.acquired = acquired
        self.reason = reason
        self.holder = holder
        self.stole_stale = stole_stale

    def to_dict(self) -> dict[str, Any]:
        return {
            "acquired": self.acquired,
            "reason": self.reason,
            "stole_stale": self.stole_stale,
            "holder": self.holder.to_dict() if self.holder else None,
        }


class RunLock:
    """自动化运行的跨进程文件锁。

    典型用法::

        lock = RunLock(path, task_type=TaskType.DAILY, as_of_date=d,
                       stale_after_seconds=21600)
        with lock:            # 拿不到锁抛 LockHeldError
            ...               # 临界区

    也可以用 ``try_acquire()`` 获得非异常式结果，交由调用方决定
    是写入 ``BLOCKED_LOCKED`` 还是重试。
    """

    def __init__(
        self,
        lock_path: str | Path,
        *,
        task_type: TaskType,
        as_of_date: date,
        stale_after_seconds: int = 21600,
        code_commit: str = "no-git",
        run_id: str = "",
        allow_steal_active: bool = False,
        now_fn: Callable[[], datetime] = datetime.now,
        alive_fn: Callable[[int], bool] = process_alive,
        hostname: Optional[str] = None,
    ) -> None:
        self.lock_path = Path(lock_path)
        self.task_type = task_type
        self.as_of_date = as_of_date
        self.stale_after_seconds = stale_after_seconds
        self.code_commit = code_commit
        self.run_id = run_id
        self.allow_steal_active = allow_steal_active
        self._now = now_fn
        self._alive = alive_fn
        self._hostname = hostname or socket.gethostname()
        self._held = False
        self._info: Optional[LockInfo] = None
        # 最近一次 release() 失败的原因（成功时为 None），供上层日志与告警使用
        self.release_error: Optional[OSError] = None

    # -- 状态 ---------------------------------------------------------- #

    @property
    def held(self) -> bool:
        """当前实例是否持有锁。"""
        return self._held

    @property
    def info(self) -> Optional[LockInfo]:
        """本实例写入的锁信息。"""
        return self._info

    # -- 陈旧判定 ------------------------------------------------------ #

    def is_stale(self, holder: LockInfo) -> tuple[bool, str]:
        """判断现有锁是否陈旧。

        Returns:
            ``(是否陈旧, 判定理由)``。
        """
        age = (self._now() - holder.started_at).total_seconds()
        same_host = holder.hostname == self._hostname
        if same_host:
            # 本进程自己遗留的孤儿锁：同主机 + 同 pid，但持锁的不是当前 RunLock
            # 实例（run_id 不同）。AutomationRunner 在单进程内严格串行执行，
            # 因此这种锁一定是上一次运行释放失败留下的残留，而非并发运行。
            # 探活会因为"进程就是我们自己"而永远返回 True，必须在探活前拦下，
            # 否则一次删除失败会让后续所有运行被自己永久阻断。
            if holder.pid == os.getpid() and holder.run_id != self.run_id:
                return True, (
                    f"锁由当前进程 pid={holder.pid} 遗留"
                    f"（run_id={holder.run_id or 'n/a'}，已持锁 {age:.0f} 秒），"
                    "判定为本进程孤儿锁"
                )
            if self._alive(holder.pid):
                return False, (
                    f"持锁进程 pid={holder.pid} 仍在运行"
                    f"（已持锁 {age:.0f} 秒）"
                )
            return True, (
                f"持锁进程 pid={holder.pid} 已不存在，判定为陈旧锁"
                f"（已持锁 {age:.0f} 秒）"
            )
        # 跨主机无法探活，只能依赖时间阈值
        if age > self.stale_after_seconds:
            return True, (
                f"锁来自其他主机 {holder.hostname}，已超过 "
                f"{self.stale_after_seconds} 秒阈值（{age:.0f} 秒），判定为陈旧锁"
            )
        return False, (
            f"锁来自其他主机 {holder.hostname}，尚未超过陈旧阈值"
            f"（{age:.0f}/{self.stale_after_seconds} 秒）"
        )

    # -- 加锁 / 解锁 ---------------------------------------------------- #

    def _payload(self) -> LockInfo:
        # 锁文件以秒级精度序列化；此处同步截断微秒，
        # 保证 release() 回读比对时不会因精度差异误判为"锁已被接管"。
        return LockInfo(
            pid=os.getpid(),
            hostname=self._hostname,
            task_type=self.task_type,
            as_of_date=self.as_of_date,
            started_at=self._now().replace(microsecond=0),
            code_commit=self.code_commit,
            run_id=self.run_id,
        )

    def _write_exclusive(self, info: LockInfo) -> bool:
        """原子创建锁文件；已存在返回 False。"""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            fd = os.open(self.lock_path, flags)
        except FileExistsError:
            return False
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(info.to_dict(), fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
        except Exception:
            # 写入失败必须清理半成品锁，否则会永久阻塞后续运行
            try:
                self.lock_path.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - 清理失败极少见
                pass
            raise
        return True

    def try_acquire(self) -> LockAcquisition:
        """尝试加锁，不抛异常。"""
        if self._held:
            return LockAcquisition(True, reason="本实例已持有锁", holder=self._info)

        info = self._payload()
        if self._write_exclusive(info):
            self._held = True
            self._info = info
            return LockAcquisition(True, reason="成功获取锁", holder=info)

        holder = read_lock(self.lock_path)
        if holder is None:
            # 锁文件损坏或恰好被释放：清理后重试一次
            try:
                self.lock_path.unlink(missing_ok=True)
            except OSError:
                pass
            if self._write_exclusive(info):
                self._held = True
                self._info = info
                return LockAcquisition(
                    True, reason="清理损坏锁文件后获取锁", holder=info, stole_stale=True
                )
            holder = read_lock(self.lock_path)
            return LockAcquisition(
                False,
                reason="锁文件损坏且无法接管",
                holder=holder,
            )

        stale, why = self.is_stale(holder)
        if not stale:
            return LockAcquisition(False, reason=why, holder=holder)
        if self.allow_steal_active:  # pragma: no cover - 配置层已禁止
            pass
        # 接管陈旧锁：先删后建，仍走 O_EXCL 保证并发安全
        try:
            self.lock_path.unlink(missing_ok=True)
        except OSError as exc:
            return LockAcquisition(
                False, reason=f"陈旧锁删除失败: {exc}", holder=holder
            )
        if self._write_exclusive(info):
            self._held = True
            self._info = info
            return LockAcquisition(
                True, reason=f"接管陈旧锁（{why}）", holder=info, stole_stale=True
            )
        return LockAcquisition(
            False,
            reason="接管陈旧锁时被其他进程抢先",
            holder=read_lock(self.lock_path),
        )

    def acquire(self) -> LockInfo:
        """加锁；失败抛 ``LockHeldError``。"""
        result = self.try_acquire()
        if not result.acquired:
            holder = result.holder
            detail = ""
            if holder is not None:
                detail = (
                    f"（持锁方: pid={holder.pid} host={holder.hostname} "
                    f"task={holder.task_type.value} "
                    f"as_of={holder.as_of_date.isoformat()} "
                    f"run_id={holder.run_id or 'n/a'}）"
                )
            raise LockHeldError(f"{result.reason}{detail}")
        assert self._info is not None
        return self._info

    def release(self) -> bool:
        """释放锁；仅当锁确为本实例持有时才删除。

        Returns:
            是否实际删除了锁文件。
        """
        if not self._held:
            return False
        current = read_lock(self.lock_path)
        self._held = False
        if current is None:
            # 锁文件已消失或内容损坏：只要磁盘上不再有残留就算释放成功，
            # 否则把损坏的残留一并清掉，避免变成永久阻塞后续运行的幽灵锁。
            if not self.lock_path.exists():
                return True
            return self._unlink_confirmed()
        mine = self._info
        if mine is not None and (
            current.pid != mine.pid
            or current.hostname != mine.hostname
            or current.started_at != mine.started_at
        ):
            # 锁已被别人接管，不能误删他人的锁
            return False
        return self._unlink_confirmed()

    def _unlink_confirmed(self) -> bool:
        """删除锁文件并回读确认生效（有界重试）。

        Windows 上刚写完的文件可能被杀软 / 索引服务短暂持有句柄，``unlink``
        会抛 ``PermissionError``，或删除虽已提交但短时间内仍可见。直接把异常
        吞掉会留下残留锁文件，使同一台机器上的下一次运行被误判为"另一实例
        正在运行"而阻断（BLOCKED_LOCKED）。这里做有界重试并确认结果。
        """
        last_error: Optional[OSError] = None
        for attempt in range(_RELEASE_MAX_ATTEMPTS):
            try:
                self.lock_path.unlink(missing_ok=True)
            except OSError as exc:
                last_error = exc
            if not self.lock_path.exists():
                self.release_error = None
                return True
            time.sleep(_RELEASE_RETRY_SECONDS * (attempt + 1))
        self.release_error = last_error or OSError(
            f"锁文件删除后仍然存在: {self.lock_path}"
        )
        return False

    # -- 上下文管理 ----------------------------------------------------- #

    def __enter__(self) -> "RunLock":
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: Optional[type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.release()
