"""Phase 4 运行编排器：把锁、状态机、日志、告警缝成一条可靠执行链。

职责边界
--------
``AutomationRunner`` 只管**怎么跑**，不管**跑什么**。
具体业务步骤由 ``daily.py`` / ``weekly.py`` 以回调形式提供。
这样每日与每周两条管线共享同一套崩溃语义、幂等语义与告警语义，
不会各写一份、各错一份。

执行链
------
1. **恢复**：把上次残留的 ``RUNNING`` 记录显式判为 ``FAILED``（绝不判成功）。
2. **指纹**：算出确定性 ``run_id``。
3. **幂等**：同一 ``run_id`` 已有 ``SUCCESS`` 记录且未 ``--force-retry`` 时，
   直接复用，不重复扣款、不重复写报告。
4. **加锁**：拿不到活跃锁 -> ``BLOCKED_LOCKED``（退出码 4）。
5. **执行**：逐步跑管线，异常按类型映射到终态。
6. **收尾**：原子落盘状态 -> 触发/清除告警 -> 释放锁 -> 返回退出码。

异常到终态的映射
----------------
==============================  ==============================
异常                             终态
==============================  ==============================
``NonTradingDay``               ``SKIPPED_NON_TRADING_DAY`` (0)
``DataUnavailableError``        ``SKIPPED_DATA_UNAVAILABLE`` (0)
``DataQualityBlockedError``     ``BLOCKED_DATA_QUALITY`` (3)
``LockHeldError``               ``BLOCKED_LOCKED`` (4)
``NotEligibleError``            ``BLOCKED_NOT_ELIGIBLE`` (5)
``CalendarUnavailableError``    ``FAILED`` (1)  — fail-closed
其它任何异常                      ``FAILED`` (1)
==============================  ==============================

交易日历不可用**不是**跳过而是失败：无法确认今天是不是交易日，
就没有资格宣称"今天没什么可做的"。
"""
from __future__ import annotations

import traceback
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, Sequence

from .alerts import AlertManager
from .config import AutomationConfig
from .datasource import DataUnavailableError, MarketDataSource
from .idempotency import RunFingerprint, build_fingerprint
from .locking import RunLock
from .logging_setup import AutomationLogger, build_logger
from .models import (
    AutomationError,
    CalendarUnavailableError,
    DataQualityBlockedError,
    LockHeldError,
    NotEligibleError,
    RunRecord,
    RunState,
    StepResult,
    StepStatus,
    TaskType,
)
from .state import StateStore

__all__ = [
    "PipelineSignal",
    "NonTradingDay",
    "PipelineContext",
    "RunOutcome",
    "AutomationRunner",
]


# ---------------------------------------------------------------------- #
# 控制流信号
# ---------------------------------------------------------------------- #


class PipelineSignal(AutomationError):
    """管线主动请求以某个终态收尾（非错误）。"""

    def __init__(
        self,
        state: RunState,
        message: str,
        *,
        detail: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.state = state
        self.detail = dict(detail or {})


class NonTradingDay(PipelineSignal):
    """业务日不是交易日，正常跳过。"""

    def __init__(self, message: str, *, detail: Optional[dict[str, Any]] = None) -> None:
        super().__init__(RunState.SKIPPED_NON_TRADING_DAY, message, detail=detail)


#: 异常类型 -> 终态映射（顺序敏感：子类在前）
_EXCEPTION_STATE_MAP: tuple[tuple[type[BaseException], RunState], ...] = (
    (DataUnavailableError, RunState.SKIPPED_DATA_UNAVAILABLE),
    (DataQualityBlockedError, RunState.BLOCKED_DATA_QUALITY),
    (NotEligibleError, RunState.BLOCKED_NOT_ELIGIBLE),
    (LockHeldError, RunState.BLOCKED_LOCKED),
    (CalendarUnavailableError, RunState.FAILED),
)


def map_exception_state(exc: BaseException) -> RunState:
    """把异常映射为终态；未知异常一律 ``FAILED``。"""
    if isinstance(exc, PipelineSignal):
        return exc.state
    for exc_type, state in _EXCEPTION_STATE_MAP:
        if isinstance(exc, exc_type):
            return state
    return RunState.FAILED


_SIGNAL_STEP_STATUS: dict[RunState, StepStatus] = {
    RunState.SKIPPED_NON_TRADING_DAY: StepStatus.SKIPPED,
    RunState.SKIPPED_DATA_UNAVAILABLE: StepStatus.SKIPPED,
    RunState.BLOCKED_DATA_QUALITY: StepStatus.BLOCKED,
    RunState.BLOCKED_LOCKED: StepStatus.BLOCKED,
    RunState.BLOCKED_NOT_ELIGIBLE: StepStatus.BLOCKED,
}


# ---------------------------------------------------------------------- #
# 管线上下文
# ---------------------------------------------------------------------- #


@dataclass
class PipelineContext:
    """传给业务管线的执行上下文。"""

    config: AutomationConfig
    logger: AutomationLogger
    state_store: StateStore
    alerts: AlertManager
    record: RunRecord
    fingerprint: RunFingerprint
    task_type: TaskType
    as_of_date: date
    data_source: Optional[MarketDataSource] = None
    dry_run: bool = False
    now_fn: Callable[[], datetime] = datetime.now
    artifacts: list[Path] = field(default_factory=list)
    scratch: dict[str, Any] = field(default_factory=dict)

    # -------------------------------------------------------------- #
    @property
    def run_id(self) -> str:
        return self.record.run_id

    def add_artifact(self, path: Path) -> Path:
        """登记产物路径（相对仓库根写入运行记录）。"""
        p = Path(path)
        self.artifacts.append(p)
        try:
            rel = p.resolve().relative_to(self.config.base_dir).as_posix()
        except ValueError:
            rel = p.as_posix()
        if rel not in self.record.artifacts:
            self.record.artifacts.append(rel)
        return p

    @contextmanager
    def step(self, name: str) -> Iterator[StepResult]:
        """执行一个命名步骤，自动记录状态、耗时与异常。

        步骤内抛出的 ``PipelineSignal`` 会被标记为 ``SKIPPED`` / ``BLOCKED``
        后原样上抛，交由 ``AutomationRunner`` 决定运行终态。
        """
        started = self.now_fn()
        result = StepResult(name=name, status=StepStatus.OK, started_at=started)
        step_logger = self.logger.step(name)
        step_logger.info("step_start", f"步骤开始: {name}")
        try:
            yield result
        except PipelineSignal as sig:
            result.status = _SIGNAL_STEP_STATUS.get(sig.state, StepStatus.BLOCKED)
            result.error = f"{type(sig).__name__}: {sig}"
            result.detail.update(sig.detail)
            result.finished_at = self.now_fn()
            self.record.add_step(result)
            step_logger.warning(
                "step_signal",
                f"步骤请求以 {sig.state.value} 收尾: {sig}",
                signal_state=sig.state.value,
                signal_detail=sig.detail,
            )
            raise
        except BaseException as exc:  # noqa: BLE001 - 需统一记录后再上抛
            state = map_exception_state(exc)
            result.status = _SIGNAL_STEP_STATUS.get(state, StepStatus.FAILED)
            result.error = f"{type(exc).__name__}: {exc}"
            result.finished_at = self.now_fn()
            self.record.add_step(result)
            step_logger.error(
                "step_error",
                f"步骤异常: {name}",
                exception=exc,
                mapped_state=state.value,
            )
            raise
        else:
            result.finished_at = self.now_fn()
            self.record.add_step(result)
            step_logger.info(
                "step_done",
                f"步骤完成: {name}",
                duration_seconds=result.duration_seconds,
                step_detail=result.detail,
            )

    def skip_non_trading_day(self, message: str, **detail: Any) -> None:
        raise NonTradingDay(message, detail=detail)


# ---------------------------------------------------------------------- #
# 运行结果
# ---------------------------------------------------------------------- #


@dataclass
class RunOutcome:
    """一次运行的对外结果。"""

    record: RunRecord
    exit_code: int
    alert: dict[str, Any] = field(default_factory=dict)
    artifacts: list[Path] = field(default_factory=list)
    reused: bool = False
    lock: dict[str, Any] = field(default_factory=dict)

    @property
    def state(self) -> RunState:
        return self.record.state

    @property
    def succeeded(self) -> bool:
        return self.record.state is RunState.SUCCESS

    def to_dict(self) -> dict[str, Any]:
        return {
            "run": self.record.to_dict(),
            "exit_code": self.exit_code,
            "alert": self.alert,
            "reused": self.reused,
            "lock": self.lock,
        }


# ---------------------------------------------------------------------- #
# 编排器
# ---------------------------------------------------------------------- #

Pipeline = Callable[[PipelineContext], None]


class AutomationRunner:
    """自动化运行编排器。"""

    def __init__(
        self,
        config: AutomationConfig,
        *,
        task_type: TaskType,
        data_source: Optional[MarketDataSource] = None,
        state_store: Optional[StateStore] = None,
        logger: Optional[AutomationLogger] = None,
        alerts: Optional[AlertManager] = None,
        now_fn: Callable[[], datetime] = datetime.now,
    ) -> None:
        self.config = config
        self.task_type = task_type
        self.data_source = data_source
        self.now_fn = now_fn
        self.state_store = state_store or StateStore(config.state_dir)
        self._logger_override = logger
        self._alerts_override = alerts

    # ------------------------------------------------------------------ #
    # 输入指纹
    # ------------------------------------------------------------------ #
    def _input_paths(self) -> list[Path]:
        """参与输入哈希的文件：curated 行情 + 交易日历。"""
        paths: list[Path] = []
        curated = self.config.data_dir / "curated"
        if curated.exists():
            paths.extend(sorted(curated.glob("*.parquet")))
        cal = self.config.calendar_path
        if cal is not None:
            paths.append(Path(cal))
        return paths

    def build_fingerprint(
        self,
        as_of_date: date,
        *,
        extra_inputs: Optional[dict[str, Any]] = None,
    ) -> RunFingerprint:
        extra = {
            "symbols": list(self.config.data.symbols),
            "lookback_days": int(self.config.data.lookback_days),
            "task": self.task_type.value,
        }
        if extra_inputs:
            extra.update(extra_inputs)
        return build_fingerprint(
            self.config,
            task_type=self.task_type,
            as_of_date=as_of_date,
            input_paths=self._input_paths(),
            extra_inputs=extra,
        )

    # ------------------------------------------------------------------ #
    # 主入口
    # ------------------------------------------------------------------ #
    def run(
        self,
        pipeline: Pipeline,
        *,
        as_of_date: date,
        force_retry: bool = False,
        dry_run: bool = False,
        extra_inputs: Optional[dict[str, Any]] = None,
    ) -> RunOutcome:
        """执行一次自动化运行。

        Args:
            pipeline: 业务管线回调，接收 ``PipelineContext``。
            as_of_date: 业务基准日。
            force_retry: 是否忽略已有成功记录强制重跑（attempt 递增）。
            dry_run: 只演练不落盘（账户状态与报告不写入）。
            extra_inputs: 额外参与输入哈希的结构化输入。

        Returns:
            运行结果（含终态、退出码、产物与告警摘要）。
        """
        self.state_store.ensure_dirs()
        self.config.logs_dir.mkdir(parents=True, exist_ok=True)
        self.config.reports_dir.mkdir(parents=True, exist_ok=True)

        # 1) 恢复上次残留的 RUNNING
        recovered = self.state_store.recover_interrupted(
            self.task_type, now=self.now_fn()
        )

        # 2) 指纹
        fingerprint = self.build_fingerprint(as_of_date, extra_inputs=extra_inputs)

        logger = self._logger_override or build_logger(
            self.config,
            run_id=fingerprint.run_id,
            task_type=self.task_type,
            as_of_date=as_of_date,
        )
        alerts = self._alerts_override or AlertManager(self.config, logger=logger)

        if recovered:
            logger.warning(
                "recover_interrupted",
                f"发现 {len(recovered)} 条中断运行，已判定为 FAILED",
                run_ids=[r.run_id for r in recovered],
            )

        # 3) 幂等：已有成功记录直接复用
        existing = self.state_store.load_run(self.task_type, as_of_date)
        attempt = 1
        if existing is not None:
            attempt = int(existing.attempt)
            same_fingerprint = existing.run_id == fingerprint.run_id
            if (
                same_fingerprint
                and existing.state is RunState.SUCCESS
                and not force_retry
            ):
                logger.info(
                    "idempotent_reuse",
                    "该业务日已成功运行，直接复用既有结果",
                    run_id=existing.run_id,
                    as_of_date=as_of_date.isoformat(),
                )
                return RunOutcome(
                    record=existing,
                    exit_code=existing.exit_code,
                    reused=True,
                    alert={"alerted": False, "reason": "idempotent_reuse"},
                )
            if force_retry or not same_fingerprint:
                attempt = attempt + 1

        record = RunRecord(
            run_id=fingerprint.run_id,
            task_type=self.task_type,
            as_of_date=as_of_date,
            state=RunState.PENDING,
            code_commit=fingerprint.code_commit,
            config_hash=fingerprint.config_hash,
            input_hash=fingerprint.input_hash,
            started_at=self.now_fn(),
            attempt=attempt,
        )

        # 4) 加锁
        lock = RunLock(
            self.config.lock_path,
            task_type=self.task_type,
            as_of_date=as_of_date,
            stale_after_seconds=self.config.lock.stale_after_seconds,
            code_commit=fingerprint.code_commit,
            run_id=fingerprint.run_id,
            allow_steal_active=False,
            now_fn=self.now_fn,
        )
        acquisition = lock.try_acquire()
        if not acquisition.acquired:
            record.state = RunState.BLOCKED_LOCKED
            record.finished_at = self.now_fn()
            record.message = f"另一实例正在运行，已阻断：{acquisition.reason}"
            record.add_step(
                StepResult(
                    name="lock",
                    status=StepStatus.BLOCKED,
                    started_at=record.started_at,
                    finished_at=record.finished_at,
                    detail=acquisition.to_dict(),
                    error=acquisition.reason,
                )
            )
            logger.error(
                "lock_blocked", record.message, lock_detail=acquisition.to_dict()
            )
            self.state_store.save_run(record)
            alert = alerts.handle(record, extra={"lock": acquisition.to_dict()})
            return RunOutcome(
                record=record,
                exit_code=record.exit_code,
                alert=alert,
                lock=acquisition.to_dict(),
            )

        if acquisition.stole_stale:
            logger.warning(
                "lock_stale_taken",
                "接管了陈旧锁（原持有进程已不存在或超时）",
                lock_detail=acquisition.to_dict(),
            )

        ctx = PipelineContext(
            config=self.config,
            logger=logger,
            state_store=self.state_store,
            alerts=alerts,
            record=record,
            fingerprint=fingerprint,
            task_type=self.task_type,
            as_of_date=as_of_date,
            data_source=self.data_source,
            dry_run=dry_run,
            now_fn=self.now_fn,
        )
        ctx.record.add_step(
            StepResult(
                name="lock",
                status=StepStatus.OK,
                started_at=record.started_at,
                finished_at=self.now_fn(),
                detail=acquisition.to_dict(),
            )
        )

        # 5) 执行
        record.state = RunState.RUNNING
        self.state_store.save_run(record)
        logger.info(
            "run_start",
            f"{self.task_type.value} 运行开始",
            run_id=record.run_id,
            as_of_date=as_of_date.isoformat(),
            attempt=attempt,
            dry_run=dry_run,
            code_commit=record.code_commit,
        )

        try:
            pipeline(ctx)
        except PipelineSignal as sig:
            record.state = sig.state
            record.message = str(sig)
        except BaseException as exc:  # noqa: BLE001 - 顶层兜底
            record.state = map_exception_state(exc)
            record.message = f"{type(exc).__name__}: {exc}"
            logger.error(
                "run_exception",
                "运行异常终止",
                exception=exc,
                traceback_tail=_tail_traceback(exc),
            )
        else:
            if not record.is_terminal:
                record.state = RunState.SUCCESS
            if not record.message:
                record.message = f"{self.task_type.value} 运行完成"
        finally:
            record.finished_at = self.now_fn()
            released = lock.release()

        # 6) 收尾
        # 终态报告渲染：在运行终态（SUCCESS/SKIPPED/BLOCKED/FAILED）确定之后再
        # 落盘 run.json 与 Markdown 报告，否则报告会显示 "运行中（非终态）"（缺陷 #2）。
        # 仅当业务管线在成功路径上注册了渲染回调时执行；异常/跳过路径不写报告。
        finalize = ctx.scratch.get("_finalize_report")
        if finalize is not None:
            try:
                finalize()
            except Exception as exc:  # noqa: BLE001 - 报告渲染失败不得吞掉运行结果
                logger.error(
                    "report_finalize_failed",
                    "终态报告渲染失败，运行结果记录仍会保存",
                    exception=exc,
                    traceback_tail=_tail_traceback(exc),
                )

        self.state_store.save_run(record)
        if not released and lock.release_error is not None:
            # 锁文件删除失败会让下一次运行被误判为"另一实例正在运行"，
            # 必须显式告警而非静默吞掉（Windows 上多由杀软/索引服务持句柄导致）。
            logger.warning(
                "lock_release_failed",
                f"锁文件释放失败，可能残留: {self.config.lock_path}",
                lock_path=str(self.config.lock_path),
                error=str(lock.release_error),
            )
        logger.info(
            "run_finish",
            f"{self.task_type.value} 运行结束: {record.state.value}",
            state=record.state.value,
            exit_code=record.exit_code,
            artifacts=len(record.artifacts),
            lock_released=released,
        )
        alert = alerts.handle(
            record, extra={"artifacts": list(record.artifacts), "dry_run": dry_run}
        )
        return RunOutcome(
            record=record,
            exit_code=record.exit_code,
            alert=alert,
            artifacts=list(ctx.artifacts),
            lock=acquisition.to_dict(),
        )


def _tail_traceback(exc: BaseException, *, lines: int = 6) -> str:
    """取异常堆栈尾部若干行（避免把全量路径写进日志）。"""
    tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
    joined = "".join(tb).strip().splitlines()
    return "\n".join(joined[-lines:])
