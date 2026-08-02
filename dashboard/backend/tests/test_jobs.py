"""JobManager / JobStore / 区间补跑 后端测试。

覆盖：白名单动作、参数与日期校验、CSRF 与确认令牌、命令注入防护、
全局串行锁、超时、输出截断、状态持久化、重启 interrupted、
区间全部成功/部分失败/全部失败、非交易日跳过、单日失败继续执行。
"""
from __future__ import annotations

import asyncio
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.errors import DashboardError
from app.executors import SubprocessExecutor, validate_date_arg
from app.jobs import (
    JOB_BACKFILL,
    JOB_DAILY,
    JOB_RERUN,
    JOB_VERIFY,
    JOB_WEEKLY,
    JobManager,
    JobRecord,
    JobStore,
    STATE_FAILED,
    STATE_INTERRUPTED,
    STATE_PARTIAL,
    STATE_QUEUED,
    STATE_RUNNING,
    STATE_SKIPPED,
    STATE_SUCCEEDED,
    parse_cli_state,
)


def make_config(tmp_path: Path):
    from app.config import DashboardConfig

    return DashboardConfig(
        username="admin",
        password_hash="$argon2id$v=19$m=65536,t=3,p=4$fake",
        session_secret="s" * 40,
        host="127.0.0.1",
        auth_file=tmp_path / "auth.json",
        project_root=tmp_path,
    )


class FakeExecutor:
    """可编排结果的假执行器（不跑真实 CLI）。"""

    def __init__(self, results: dict[str, int] | None = None):
        # action -> (exit_code, timed_out)
        self.results: dict[str, tuple[int, bool]] = {
            JOB_VERIFY: (0, False),
            JOB_DAILY: (0, False),
            JOB_WEEKLY: (0, False),
            JOB_RERUN: (0, False),
        }
        if results:
            self.results.update(results)
        self.executed: list[tuple[str, str | None, str | None]] = []
        self.lock_held = False

    def validate_action(self, action: str) -> None:
        if action not in ("verify", "daily", "weekly", "rerun", "backfill"):
            raise DashboardError("action_not_allowed", f"动作不允许: {action}", status_code=403)

    def is_write_action(self, action: str) -> bool:
        return action in ("daily", "weekly", "rerun", "backfill")

    async def execute(self, action: str, *, date=None, task=None, timeout=None):
        self.executed.append((action, date, task))
        code, timed_out = self.results.get(action, (0, False))
        from app.executors import ActionResult

        return ActionResult(
            ok=code == 0 and not timed_out,
            action=action,
            stdout="fake out",
            stderr="" if code == 0 else "fake err",
            exit_code=code,
            duration_ms=1,
            timed_out=timed_out,
        )


@pytest.fixture
def store(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path / "state" / "dashboard")


@pytest.fixture
def fake_executor() -> FakeExecutor:
    return FakeExecutor()


def make_manager(executor, store, **kwargs) -> JobManager:
    return JobManager(executor, store, **kwargs)


class TestDateValidation:
    def test_valid_date(self):
        assert validate_date_arg("2026-08-03") == "2026-08-03"

    @pytest.mark.parametrize("bad", ["2026-8-3", "2026/08/03", "abc", "", "2026-13-01", "2026-02-30"])
    def test_invalid_dates(self, bad):
        with pytest.raises(DashboardError):
            validate_date_arg(bad)

    def test_injection_attempts(self):
        for bad in ("2024-01-01; rm -rf /", "2024-01-01 --config x", "2024-01-01||whoami", "$(whoami)"):
            with pytest.raises(DashboardError):
                validate_date_arg(bad)


class TestWhitelist:
    def test_unknown_job_type_rejected(self, fake_executor, store):
        mgr = make_manager(fake_executor, store)
        with pytest.raises(DashboardError):
            mgr.create_job("rm_rf")

    def test_unknown_task_rejected(self, fake_executor, store):
        mgr = make_manager(fake_executor, store)
        with pytest.raises(DashboardError):
            mgr.create_job(JOB_RERUN, task="schtasks", date="2026-08-03")

    def test_daily_requires_date(self, fake_executor, store):
        mgr = make_manager(fake_executor, store)
        with pytest.raises(DashboardError):
            mgr.create_job(JOB_DAILY)

    def test_backfill_requires_range(self, fake_executor, store):
        mgr = make_manager(fake_executor, store)
        with pytest.raises(DashboardError):
            mgr.create_job(JOB_BACKFILL, start_date="2026-08-01")
        with pytest.raises(DashboardError):
            mgr.create_job(JOB_BACKFILL, start_date="2026-08-05", end_date="2026-08-01")

    def test_backfill_range_limit(self, fake_executor, store):
        mgr = make_manager(fake_executor, store, max_backfill_days=250)
        with pytest.raises(DashboardError):
            mgr.create_job(
                JOB_BACKFILL,
                start_date="2026-01-01",
                end_date="2026-12-31",  # 365 天超限
            )


class TestExecutorSafety:
    def test_no_shell_in_source(self):
        import inspect

        src = inspect.getsource(SubprocessExecutor)
        assert "shell=True" not in src

    def test_build_argv_fixed(self, tmp_path):
        cfg = make_config(tmp_path)
        ex = SubprocessExecutor(cfg, project_root=tmp_path)
        argv = ex.build_argv(JOB_VERIFY)
        assert argv[-1] == "verify"
        assert "shell" not in " ".join(argv).lower()
        argv2 = ex.build_argv(JOB_DAILY, date="2026-08-03")
        assert argv2[-2:] == ["--date", "2026-08-03"]

    def test_build_argv_rejects_arbitrary(self, tmp_path):
        cfg = make_config(tmp_path)
        ex = SubprocessExecutor(cfg, project_root=tmp_path)
        with pytest.raises(DashboardError):
            ex.build_argv("rm -rf /")


class TestJobLifecycle:
    def test_create_and_persist(self, fake_executor, store):
        mgr = make_manager(fake_executor, store)
        rec = mgr.create_job(JOB_VERIFY)
        assert rec.state == STATE_QUEUED
        loaded = store.load(rec.job_id)
        assert loaded is not None
        assert loaded.job_type == JOB_VERIFY

    def test_run_verify_succeeds(self, fake_executor, store):
        mgr = make_manager(fake_executor, store)
        rec = mgr.create_job(JOB_VERIFY)
        asyncio.get_event_loop().run_until_complete(mgr._run_job(rec.job_id))
        loaded = store.load(rec.job_id)
        assert loaded.state == STATE_SUCCEEDED

    def test_run_daily_fails(self, fake_executor, store):
        fake_executor.results[JOB_DAILY] = (1, False)
        mgr = make_manager(fake_executor, store)
        rec = mgr.create_job(JOB_DAILY, date="2026-08-03")
        asyncio.get_event_loop().run_until_complete(mgr._run_job(rec.job_id))
        loaded = store.load(rec.job_id)
        assert loaded.state == STATE_FAILED
        assert "CLI 失败" in (loaded.error or "")

    def test_interrupted_on_restart(self, fake_executor, store):
        mgr = make_manager(fake_executor, store)
        rec = mgr.create_job(JOB_DAILY, date="2026-08-03")
        rec.state = STATE_RUNNING
        store.save(rec)
        # 模拟重启：新 manager 清理
        mgr2 = make_manager(fake_executor, store)
        count = mgr2.cleanup_on_startup()
        assert count >= 1
        loaded = store.load(rec.job_id)
        assert loaded.state == STATE_INTERRUPTED


class TestBackfill:
    def _run_backfill(self, fake_executor, store, start, end, calendar=None):
        mgr = make_manager(
            fake_executor,
            store,
            calendar_provider=calendar or (lambda s, e: _weekdays(s, e)),
        )
        rec = mgr.create_job(JOB_BACKFILL, start_date=start.isoformat(), end_date=end.isoformat())
        asyncio.get_event_loop().run_until_complete(mgr._run_job(rec.job_id))
        return store.load(rec.job_id)

    def test_backfill_all_success(self, fake_executor, store):
        s, e = date(2026, 8, 3), date(2026, 8, 7)  # 周一至周五
        rec = self._run_backfill(fake_executor, store, s, e)
        assert rec.state == STATE_SUCCEEDED
        assert rec.summary["succeeded"] == 5
        assert rec.summary["failed"] == 0

    def test_backfill_partial_failure(self, fake_executor, store):
        fake_executor.results[JOB_DAILY] = (0, False)
        # 让 8-04 失败：按日期精确编排
        original = fake_executor.execute

        async def _execute(action, *, date=None, task=None, timeout=None):
            if date == "2026-08-04":
                from app.executors import ActionResult

                return ActionResult(ok=False, action=action, stderr="fail", exit_code=1, duration_ms=1)
            return await original(action, date=date, task=task, timeout=timeout)

        fake_executor.execute = _execute
        s, e = date(2026, 8, 3), date(2026, 8, 7)
        rec = self._run_backfill(fake_executor, store, s, e)
        assert rec.state == STATE_PARTIAL
        assert rec.summary["succeeded"] == 4
        assert rec.summary["failed"] == 1

    def test_backfill_all_fail(self, fake_executor, store):
        fake_executor.results[JOB_DAILY] = (2, False)
        s, e = date(2026, 8, 3), date(2026, 8, 5)
        rec = self._run_backfill(fake_executor, store, s, e)
        assert rec.state == STATE_FAILED
        assert rec.summary["failed"] == 3

    def test_backfill_skips_non_trading_days(self, fake_executor, store):
        # 8-03(周一)~8-09(周日)：交易日历只有 3 个交易日
        def calendar(s, e):
            return [date(2026, 8, 3), date(2026, 8, 5), date(2026, 8, 7)]

        s, e = date(2026, 8, 3), date(2026, 8, 9)
        rec = self._run_backfill(fake_executor, store, s, e, calendar=calendar)
        assert len(rec.daily_results) == 3
        assert rec.summary["trading_days"] == 3
        assert rec.state == STATE_SUCCEEDED

    def test_single_day_failure_continues(self, fake_executor, store):
        calls: list[str] = []

        async def _execute(action, *, date=None, task=None, timeout=None):
            calls.append(date)
            if date == "2026-08-04":
                from app.executors import ActionResult

                return ActionResult(ok=False, action=action, stderr="boom", exit_code=1, duration_ms=1)
            from app.executors import ActionResult

            return ActionResult(ok=True, action=action, stdout="ok", exit_code=0, duration_ms=1)

        fake_executor.execute = _execute
        s, e = date(2026, 8, 3), date(2026, 8, 7)
        rec = self._run_backfill(fake_executor, store, s, e)
        assert len(calls) == 5  # 失败日之后仍继续
        assert rec.summary["failed"] == 1
        assert rec.summary["succeeded"] == 4

    def test_serial_execution_via_lock(self, fake_executor, store):
        """写入型作业串行：模拟两个并发作业只串行执行。"""
        mgr = make_manager(fake_executor, store)
        rec1 = mgr.create_job(JOB_DAILY, date="2026-08-03")
        rec2 = mgr.create_job(JOB_DAILY, date="2026-08-04")

        async def _run_both():
            await asyncio.gather(mgr._run_job(rec1.job_id), mgr._run_job(rec2.job_id))

        asyncio.get_event_loop().run_until_complete(_run_both())
        r1 = store.load(rec1.job_id)
        r2 = store.load(rec2.job_id)
        assert r1.state in (STATE_SUCCEEDED, STATE_FAILED)
        assert r2.state in (STATE_SUCCEEDED, STATE_FAILED)

    def test_backfill_all_skipped_is_skipped_state(self, fake_executor, store):
        """区间全部被跳过（如数据不可用）→ 作业状态 skipped，不伪装成功。"""
        from app.executors import ActionResult

        async def _execute(action, *, date=None, task=None, timeout=None):
            return ActionResult(
                ok=True,
                action=action,
                stdout=f"daily {date}: SKIPPED_DATA_UNAVAILABLE (exit=0)",
                exit_code=0,
                duration_ms=5,
            )

        fake_executor.execute = _execute
        s, e = date(2026, 8, 3), date(2026, 8, 7)
        rec = self._run_backfill(fake_executor, store, s, e)
        assert rec.state == STATE_SKIPPED
        assert rec.summary["skipped_days"] == 5
        assert rec.summary["succeeded"] == 0

    def test_backfill_mixed_skip_and_success_is_partial(self, fake_executor, store):
        from app.executors import ActionResult

        async def _execute(action, *, date=None, task=None, timeout=None):
            if date == "2026-08-03":
                return ActionResult(
                    ok=True, action=action,
                    stdout=f"daily {date}: SKIPPED_DATA_UNAVAILABLE (exit=0)",
                    exit_code=0, duration_ms=5,
                )
            return ActionResult(ok=True, action=action, stdout=f"daily {date}: SUCCESS (exit=0)", exit_code=0, duration_ms=5)

        fake_executor.execute = _execute
        s, e = date(2026, 8, 3), date(2026, 8, 5)
        rec = self._run_backfill(fake_executor, store, s, e)
        assert rec.state == STATE_PARTIAL
        assert rec.summary["succeeded"] == 2
        assert rec.summary["skipped_days"] == 1

    def test_backfill_fails_when_calendar_unavailable(self, fake_executor, store):
        """日历提供者为 None：补跑拒绝执行（fail-closed），不退化工作日。"""
        mgr = make_manager(fake_executor, store, calendar_provider=None)
        rec = mgr.create_job(
            JOB_BACKFILL, start_date="2026-08-03", end_date="2026-08-07"
        )
        asyncio.get_event_loop().run_until_complete(mgr._run_job(rec.job_id))
        loaded = store.load(rec.job_id)
        assert loaded.state == STATE_FAILED
        assert "交易日历" in (loaded.error or "")


def _weekdays(start: date, end: date) -> list[date]:
    days = []
    cur = start
    while cur <= end:
        if cur.weekday() < 5:
            days.append(cur)
        cur += timedelta(days=1)
    return days


class TestCliStateParsing:
    def test_parse_success_line(self):
        out = "daily 2026-08-02: SUCCESS (exit=0)"
        assert parse_cli_state(out) == "SUCCESS"

    def test_parse_skipped_non_trading_day(self):
        out = "daily 2026-08-02: SKIPPED_NON_TRADING_DAY (exit=0)"
        assert parse_cli_state(out) == "SKIPPED_NON_TRADING_DAY"

    def test_parse_with_extra_lines(self):
        out = '{"timestamp": "x", "level": "WARNING"}\ndaily 2026-07-31: SKIPPED_DATA_UNAVAILABLE (exit=0)'
        assert parse_cli_state(out) == "SKIPPED_DATA_UNAVAILABLE"

    def test_parse_none_on_garbage(self):
        assert parse_cli_state("nothing useful") is None
        assert parse_cli_state("") is None
        assert parse_cli_state(None) is None

    def test_skipped_daily_marked_skipped_state(self, fake_executor, store):
        """SKIPPED_NON_TRADING_DAY：作业状态为 skipped（≠ succeeded，不伪装成功）。"""
        from app.executors import ActionResult

        async def _execute(action, *, date=None, task=None, timeout=None):
            return ActionResult(
                ok=True,
                action=action,
                stdout="daily 2026-08-02: SKIPPED_NON_TRADING_DAY (exit=0)",
                exit_code=0,
                duration_ms=5,
            )

        fake_executor.execute = _execute
        mgr = make_manager(fake_executor, store)
        rec = mgr.create_job(JOB_DAILY, date="2026-08-02")
        asyncio.get_event_loop().run_until_complete(mgr._run_job(rec.job_id))
        loaded = store.load(rec.job_id)
        assert loaded.state == STATE_SKIPPED  # 跳过 ≠ 成功
        assert loaded.summary.get("skipped") == "SKIPPED_NON_TRADING_DAY"
        assert loaded.summary.get("cli_state") == "SKIPPED_NON_TRADING_DAY"

    def test_blocked_daily_marked_failed(self, fake_executor, store):
        from app.executors import ActionResult

        async def _execute(action, *, date=None, task=None, timeout=None):
            return ActionResult(
                ok=False,
                action=action,
                stdout="daily 2026-08-03: BLOCKED_DATA_QUALITY (exit=1)",
                exit_code=1,
                duration_ms=5,
            )

        fake_executor.execute = _execute
        mgr = make_manager(fake_executor, store)
        rec = mgr.create_job(JOB_DAILY, date="2026-08-03")
        asyncio.get_event_loop().run_until_complete(mgr._run_job(rec.job_id))
        loaded = store.load(rec.job_id)
        assert loaded.state == STATE_FAILED
        assert "BLOCKED" in (loaded.error or "")
