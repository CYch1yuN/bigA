"""异步作业模型：状态机、本地持久化、串行调度、区间补跑、重启清理。

设计要点（UI-G2 可操作工作台）：
- 作业类型：verify / daily / weekly / rerun / backfill
- 统一状态：queued -> running -> succeeded | partial | failed | interrupted
- 写入型作业严格串行（全局 asyncio 锁 + 持久化 busy 标记）
- 服务重启时把遗留 queued/running 标记为 interrupted
- 状态持久化在项目固定状态目录 ``state/dashboard/jobs/``
- 区间补跑：交易日历筛选、逐日串行、单日失败继续、≤250 自然日、
  succeeded / partial / failed 汇总
- API 不泄露绝对路径 / 环境变量 / 密钥 / 完整堆栈
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .errors import DashboardError
from .executors import ActionResult, SubprocessExecutor

# 状态常量
STATE_QUEUED = "queued"
STATE_RUNNING = "running"
STATE_SUCCEEDED = "succeeded"
STATE_PARTIAL = "partial"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"
STATE_INTERRUPTED = "interrupted"
# 任务被跳过（非交易日/数据不可用等）：不等于成功，也不等于失败，
# 明确区分"产生了数据"与"被跳过"两种截然不同的结果。
STATE_SKIPPED = "skipped"

ALL_STATES = (
    STATE_QUEUED,
    STATE_RUNNING,
    STATE_SUCCEEDED,
    STATE_PARTIAL,
    STATE_FAILED,
    STATE_CANCELLED,
    STATE_INTERRUPTED,
    STATE_SKIPPED,
)

# 作业类型
JOB_VERIFY = "verify"
JOB_DAILY = "daily"
JOB_WEEKLY = "weekly"
JOB_RERUN = "rerun"
JOB_BACKFILL = "backfill"
ALL_JOB_TYPES = (JOB_VERIFY, JOB_DAILY, JOB_WEEKLY, JOB_RERUN, JOB_BACKFILL)

# 区间补跑上限（自然日）
MAX_BACKFILL_DAYS = 250

# 默认超时
TIMEOUT_VERIFY = 60
TIMEOUT_SINGLE = 600
TIMEOUT_BACKFILL = 3600

# 状态文件
JOBS_DIR_NAME = "jobs"
_INDEX_NAME = "index.json"

# 路径/密钥脱敏
_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s\"']+|<PATH>")
_SECRET_RE = re.compile(r"(?i)(password|secret|token|api[_-]?key)\s*[:=]\s*\S+")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _utc_now() -> float:
    return time.time()


def _safe_text(text: str) -> str:
    text = _PATH_RE.sub("<PATH>", text or "")
    text = _SECRET_RE.sub(r"\1=<REDACTED>", text)
    return text[:4000]


@dataclass
class JobRecord:
    """单个作业的持久化记录。"""

    job_id: str
    job_type: str
    state: str = STATE_QUEUED
    created_at: str = field(default_factory=_now_iso)
    started_at: str | None = None
    finished_at: str | None = None
    # 请求参数（白名单内）
    params: dict[str, Any] = field(default_factory=dict)
    # 逐日/子任务结果（backfill 用）
    daily_results: list[dict[str, Any]] = field(default_factory=list)
    # 汇总统计
    summary: dict[str, Any] = field(default_factory=dict)
    # 日志（安全处理后，逐行）
    log: list[str] = field(default_factory=list)
    # 错误原因（安全处理）
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "job_type": self.job_type,
            "state": self.state,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "params": self.params,
            "daily_results": self.daily_results,
            "summary": self.summary,
            "log": self.log[-200:],
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "JobRecord":
        return cls(
            job_id=str(raw.get("job_id", "")),
            job_type=str(raw.get("job_type", "")),
            state=str(raw.get("state", STATE_QUEUED)),
            created_at=str(raw.get("created_at", "")),
            started_at=raw.get("started_at"),
            finished_at=raw.get("finished_at"),
            params=raw.get("params") or {},
            daily_results=raw.get("daily_results") or [],
            summary=raw.get("summary") or {},
            log=raw.get("log") or [],
            error=raw.get("error"),
        )

    def append_log(self, line: str) -> None:
        self.log.append(_safe_text(line))
        if len(self.log) > 500:
            self.log = self.log[-500:]


class JobStore:
    """作业状态持久化：state/dashboard/jobs/。"""

    def __init__(self, base_dir: Path) -> None:
        self.base = Path(base_dir)
        self.jobs_dir = self.base / JOBS_DIR_NAME

    def ensure_dir(self) -> None:
        self.jobs_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, job_id: str) -> Path:
        return self.jobs_dir / f"{job_id}.json"

    def save(self, record: JobRecord) -> None:
        self.ensure_dir()
        tmp = self._path(record.job_id).with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(record.to_dict(), fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self._path(record.job_id))

    def load(self, job_id: str) -> JobRecord | None:
        path = self._path(job_id)
        if not path.is_file():
            return None
        try:
            return JobRecord.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            return None

    def list_recent(self, limit: int = 50) -> list[JobRecord]:
        if not self.jobs_dir.is_dir():
            return []
        records: list[JobRecord] = []
        for path in sorted(self.jobs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            rec = self.load(path.stem)
            if rec is not None:
                records.append(rec)
            if len(records) >= limit:
                break
        return records

    def mark_interrupted_on_startup(self) -> int:
        """服务启动时把遗留 queued/running 标记为 interrupted。"""
        count = 0
        for path in self.jobs_dir.glob("*.json") if self.jobs_dir.is_dir() else []:
            rec = self.load(path.stem)
            if rec is None:
                continue
            if rec.state in (STATE_QUEUED, STATE_RUNNING):
                rec.state = STATE_INTERRUPTED
                rec.finished_at = _now_iso()
                rec.error = "服务重启，未完成的作业被标记为 interrupted"
                rec.append_log("作业因服务重启被标记为 interrupted")
                self.save(rec)
                count += 1
        return count


class JobManager:
    """作业调度器：串行执行、区间补跑、状态持久化、重启清理。"""

    def __init__(
        self,
        executor: SubprocessExecutor,
        store: JobStore,
        *,
        max_backfill_days: int = MAX_BACKFILL_DAYS,
        timeouts: dict[str, float] | None = None,
        calendar_provider: Callable[[date, date], list[date]] | None = None,
    ) -> None:
        self.executor = executor
        self.store = store
        self.max_backfill_days = max_backfill_days
        self.timeouts = timeouts or {
            JOB_VERIFY: TIMEOUT_VERIFY,
            JOB_DAILY: TIMEOUT_SINGLE,
            JOB_WEEKLY: TIMEOUT_SINGLE,
            JOB_RERUN: TIMEOUT_SINGLE,
            JOB_BACKFILL: TIMEOUT_BACKFILL,
        }
        # 交易日提供者：显式注入时使用；None 表示"无可用日历"（补跑 fail-closed）。
        # 注意：不能用 ``or`` 兜底，否则显式 None 会被替换成默认工作日，
        # 导致把法定休市日误当交易日。
        self._calendar_provider = calendar_provider
        # 全局串行锁（写入型作业）
        self._write_lock = asyncio.Lock()
        self._background_tasks: set[asyncio.Task] = set()
        self._startup_cleanup_done = False

    # ---- 启动清理 ----

    def cleanup_on_startup(self) -> int:
        """把遗留 queued/running 标记为 interrupted；返回清理数量。"""
        count = self.store.mark_interrupted_on_startup()
        self._startup_cleanup_done = True
        return count

    # ---- 创建作业 ----

    def create_job(
        self,
        job_type: str,
        *,
        date: str | None = None,
        task: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> JobRecord:
        """创建作业（校验参数），返回 queued 记录。"""
        if job_type not in ALL_JOB_TYPES:
            raise DashboardError("invalid_job_type", f"作业类型不允许: {job_type}", status_code=400)
        params: dict[str, Any] = {}
        if job_type == JOB_DAILY:
            params["date"] = self._require_date(date)
        elif job_type == JOB_WEEKLY:
            params["date"] = self._optional_date(date)
        elif job_type == JOB_RERUN:
            if task not in ("daily", "weekly"):
                raise DashboardError("invalid_task", f"重跑任务必须为 daily 或 weekly，得到 {task!r}", status_code=400)
            params["task"] = task
            params["date"] = self._require_date(date)
        elif job_type == JOB_BACKFILL:
            params["start_date"], params["end_date"] = self._validate_range(start_date, end_date)
        # verify 无参数
        record = JobRecord(
            job_id=self._new_id(),
            job_type=job_type,
            state=STATE_QUEUED,
            params=params,
        )
        record.append_log(f"作业已创建: {job_type}")
        self.store.save(record)
        return record

    def enqueue_and_run(self, record: JobRecord) -> JobRecord:
        """把作业交给后台任务执行（不阻塞请求）。"""
        task = asyncio.create_task(self._run_job(record.job_id))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return record

    # ---- 查询 ----

    def get_job(self, job_id: str) -> JobRecord:
        rec = self.store.load(job_id)
        if rec is None:
            raise DashboardError("job_not_found", f"作业不存在: {job_id}", status_code=404)
        return rec

    def list_jobs(self, limit: int = 50) -> list[JobRecord]:
        return self.store.list_recent(limit)

    # ---- 执行 ----

    async def _run_job(self, job_id: str) -> None:
        rec = self.store.load(job_id)
        if rec is None:
            return
        # 写操作串行；verify 也走同一锁保持简单一致
        async with self._write_lock:
            rec = self.store.load(job_id)
            if rec is None or rec.state != STATE_QUEUED:
                return
            rec.state = STATE_RUNNING
            rec.started_at = _now_iso()
            rec.append_log("作业开始执行")
            self.store.save(rec)
            try:
                if rec.job_type == JOB_BACKFILL:
                    await self._run_backfill(rec)
                else:
                    await self._run_single(rec)
            except asyncio.CancelledError:
                rec.state = STATE_INTERRUPTED
                rec.error = "作业被取消"
                rec.append_log("作业被取消")
                rec.finished_at = _now_iso()
                self.store.save(rec)
                raise
            except Exception as exc:  # noqa: BLE001 - 统一落 failed 状态
                rec.state = STATE_FAILED
                rec.error = _safe_text(f"{type(exc).__name__}: {exc}")
                rec.append_log(f"作业失败: {rec.error}")
                rec.finished_at = _now_iso()
                self.store.save(rec)

    async def _run_single(self, rec: JobRecord) -> None:
        """执行单个 verify/daily/weekly/rerun 作业。"""
        timeout = self.timeouts.get(rec.job_type, TIMEOUT_SINGLE)
        try:
            result = await self.executor.execute(
                rec.job_type,
                date=rec.params.get("date"),
                task=rec.params.get("task"),
                timeout=timeout,
            )
        except DashboardError as exc:
            rec.state = STATE_FAILED
            rec.error = _safe_text(exc.message)
            rec.append_log(f"作业失败: {exc.message}")
            rec.finished_at = _now_iso()
            self.store.save(rec)
            return

        rec.summary = {
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
            "timed_out": result.timed_out,
        }
        rec.append_log(f"CLI 退出码: {result.exit_code}")
        # 解析 CLI 状态：SKIPPED_* 记为 skipped（不伪装成成功），BLOCKED_* 记为失败
        cli_state = parse_cli_state(result.stdout)
        if cli_state:
            rec.summary["cli_state"] = cli_state
            rec.append_log(f"CLI 状态: {cli_state}")
        if result.timed_out:
            rec.state = STATE_FAILED
            rec.error = f"执行超时（上限 {int(timeout)}s）"
        elif cli_state in _SKIPPED_STATES:
            rec.state = STATE_SKIPPED  # 跳过 ≠ 成功
            rec.summary["skipped"] = cli_state
            rec.append_log(f"作业被跳过（{cli_state}），未产生数据，非成功")
        elif cli_state in _BLOCKED_STATES:
            rec.state = STATE_FAILED
            rec.error = f"CLI 阻断（{cli_state}）"
        elif result.ok:
            rec.state = STATE_SUCCEEDED
        else:
            rec.state = STATE_FAILED
            rec.error = f"CLI 失败（退出码 {result.exit_code}）"
        if result.stderr:
            rec.append_log(f"stderr: {result.stderr[:2000]}")
        rec.finished_at = _now_iso()
        self.store.save(rec)

    async def _run_backfill(self, rec: JobRecord) -> None:
        """区间补跑：交易日筛选、逐日串行、单日失败继续、汇总。"""
        start = date.fromisoformat(rec.params["start_date"])
        end = date.fromisoformat(rec.params["end_date"])
        trading_days = self._trading_days(start, end)
        rec.append_log(f"区间 {start} ~ {end} 共筛选出 {len(trading_days)} 个交易日")
        rec.summary = {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "trading_days": len(trading_days),
            "succeeded": 0,
            "failed": 0,
            "skipped_days": 0,
        }
        self.store.save(rec)

        for day in trading_days:
            # 每次迭代前重载，确保最新状态
            rec = self.store.load(rec.job_id) or rec
            day_result: dict[str, Any] = {
                "date": day.isoformat(),
                "state": "skipped",
                "exit_code": None,
                "error": None,
            }
            try:
                result = await self.executor.execute(
                    JOB_DAILY,
                    date=day.isoformat(),
                    timeout=self.timeouts.get(JOB_DAILY, TIMEOUT_SINGLE),
                )
                day_result["exit_code"] = result.exit_code
                cli_state = parse_cli_state(result.stdout)
                if cli_state:
                    day_result["cli_state"] = cli_state
                if result.timed_out:
                    day_result["state"] = "failed"
                    day_result["error"] = "执行超时"
                elif cli_state in _SKIPPED_STATES:
                    day_result["state"] = "skipped"
                    day_result["error"] = None
                elif cli_state in _BLOCKED_STATES:
                    day_result["state"] = "failed"
                    day_result["error"] = f"CLI 阻断（{cli_state}）"
                elif result.ok:
                    day_result["state"] = "succeeded"
                else:
                    day_result["state"] = "failed"
                    day_result["error"] = f"CLI 退出码 {result.exit_code}"
            except DashboardError as exc:
                day_result["state"] = "failed"
                day_result["error"] = _safe_text(exc.message)
            except Exception as exc:  # noqa: BLE001 - 单日失败不中断补跑
                day_result["state"] = "failed"
                day_result["error"] = _safe_text(f"{type(exc).__name__}: {exc}")

            rec.daily_results.append(day_result)
            # summary 键：succeeded / failed / skipped_days（避免与单日 skipped 字符串冲突）
            summary_key = day_result["state"]
            if summary_key == "skipped":
                summary_key = "skipped_days"
            rec.summary[summary_key] = int(rec.summary.get(summary_key, 0)) + 1
            rec.append_log(
                f"{day.isoformat()}: {day_result['state']}"
                + (f" ({day_result['error']})" if day_result["error"] else "")
            )
            self.store.save(rec)

        # 汇总判定（诚实语义）：
        #   succeeded=total            -> succeeded（全部产生数据）
        #   failed=total               -> failed（全部失败）
        #   succeeded=0 且 skipped>0   -> skipped（没有成功也没失败，全被跳过）
        #   其余                       -> partial（部分成功/部分失败/混合跳过）
        s = rec.summary
        succeeded = int(s.get("succeeded", 0))
        failed = int(s.get("failed", 0))
        skipped = int(s.get("skipped_days", 0))
        total = max(1, len(trading_days))
        if failed == 0 and succeeded == total:
            rec.state = STATE_SUCCEEDED
        elif succeeded == 0 and failed > 0 and skipped == 0:
            rec.state = STATE_FAILED
        elif succeeded == 0 and failed == 0 and skipped > 0:
            rec.state = STATE_SKIPPED
        else:
            rec.state = STATE_PARTIAL
        rec.append_log(f"补跑完成: 成功 {succeeded} / 失败 {failed} / 跳过 {skipped}")
        rec.finished_at = _now_iso()
        self.store.save(rec)

    # ---- 参数校验 ----

    @staticmethod
    def _new_id() -> str:
        import uuid

        return uuid.uuid4().hex[:16]

    @staticmethod
    def _require_date(value: str | None) -> str:
        from .executors import validate_date_arg

        if not value:
            raise DashboardError("missing_date", "必须指定业务日期", status_code=400)
        return validate_date_arg(value)

    @staticmethod
    def _optional_date(value: str | None) -> str | None:
        from .executors import validate_date_arg

        if value is None or value == "":
            return None
        return validate_date_arg(value)

    def _validate_range(self, start: str | None, end: str | None) -> tuple[str, str]:
        from .executors import validate_date_arg

        if not start or not end:
            raise DashboardError("invalid_range", "区间补跑必须同时提供开始与结束日期", status_code=400)
        s = validate_date_arg(start)
        e = validate_date_arg(end)
        d0 = date.fromisoformat(s)
        d1 = date.fromisoformat(e)
        if d0 > d1:
            raise DashboardError("invalid_range", "开始日期不能晚于结束日期", status_code=400)
        span = (d1 - d0).days + 1
        if span > self.max_backfill_days:
            raise DashboardError(
                "range_too_large",
                f"区间 {span} 个自然日超过上限 {self.max_backfill_days}",
                status_code=400,
            )
        return s, e

    # ---- 交易日 ----

    @staticmethod
    def _default_weekdays(start: date, end: date) -> list[date]:
        days: list[date] = []
        cur = start
        while cur <= end:
            if cur.weekday() < 5:  # 周一至周五
                days.append(cur)
            cur += timedelta(days=1)
        return days

    def _trading_days(self, start: date, end: date) -> list[date]:
        """取交易日；日历不可用时**不退化**为普通工作日（避免把法定休市
        日当交易日），而是抛 DashboardError 让补跑明确失败（fail-closed）。

        只有显式注入的日历提供者能决定交易日；缺失/异常一律拒绝补跑。
        """
        if self._calendar_provider is None:
            raise DashboardError(
                "calendar_unavailable",
                "交易日历不可用，无法进行区间补跑（拒绝以普通工作日代替）",
                status_code=400,
            )
        try:
            days = self._calendar_provider(start, end)
            if days:
                return sorted(days)
        except Exception as exc:  # noqa: BLE001 - 日历异常同样 fail-closed
            raise DashboardError(
                "calendar_unavailable",
                f"交易日历读取失败，无法进行区间补跑: {_safe_text(str(exc))}",
                status_code=400,
            ) from exc
        raise DashboardError(
            "calendar_empty",
            f"交易日历在 {start}~{end} 区间无交易日",
            status_code=400,
        )


# CLI 状态 → 作业状态映射
_SKIPPED_STATES = ("SKIPPED_NON_TRADING_DAY", "SKIPPED_DATA_UNAVAILABLE")
_BLOCKED_STATES = ("BLOCKED_DATA_QUALITY", "BLOCKED")


def parse_cli_state(stdout: str) -> str | None:
    """从 CLI stdout 解析运行状态（如 ``daily 2026-08-02: SKIPPED_... (exit=0)``）。

    返回状态字符串；无法解析时返回 None（调用方回退 exit_code 判定）。
    """
    if not stdout:
        return None
    for line in stdout.splitlines():
        if " (exit=" in line:
            after_colon = line.split(":", 1)[-1].strip()
            state = after_colon.split(" (exit=")[0].strip()
            if state:
                return state
    return None


def calendar_provider_from_parquet(
    path: Path,
) -> Callable[[date, date], list[date]]:
    """从项目交易日历 parquet 构造交易日提供者。

    只读取 ``trade_date`` / ``is_open`` 两列；文件缺失或解析失败时
    由 JobManager 回退工作日模型（严格旁路：日历不可用不阻断补跑）。
    """

    def _provide(start: date, end: date) -> list[date]:
        import pandas as pd

        df = pd.read_parquet(path)
        if "trade_date" not in df.columns or "is_open" not in df.columns:
            raise ValueError("交易日历缺少 trade_date/is_open 列")
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        mask = (df["trade_date"] >= start) & (df["trade_date"] <= end) & (df["is_open"] == True)  # noqa: E712
        return sorted(df.loc[mask, "trade_date"].tolist())

    return _provide


__all__ = [
    "JobRecord",
    "JobStore",
    "JobManager",
    "STATE_QUEUED",
    "STATE_RUNNING",
    "STATE_SUCCEEDED",
    "STATE_PARTIAL",
    "STATE_FAILED",
    "STATE_CANCELLED",
    "STATE_INTERRUPTED",
    "STATE_SKIPPED",
    "ALL_JOB_TYPES",
    "MAX_BACKFILL_DAYS",
]
