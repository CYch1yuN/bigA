"""FR-21 覆盖率冲刺（二）：``state`` 与 ``locking`` 模块补测。

目标：把这两个模块从 ~70% 提到接近 100%，支撑 automation 包整体 ≥90%。
全部离线、不联网、不接触券商。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from ashare_quant.automation.locking import (
    LockAcquisition,
    LockHeldError,
    RunLock,
    process_alive,
    read_lock,
)
from ashare_quant.automation.models import (
    EligibilityStatus,
    LockInfo,
    RunRecord,
    RunState,
    SimulatedAccountState,
    StepResult,
    StepStatus,
    StrategyTrack,
    TaskType,
)
from ashare_quant.automation.state import (
    StateStore,
    atomic_write_json,
    atomic_write_text,
)


def _rec(run_id: str, state: RunState, as_of=date(2020, 1, 2),
         started: datetime | None = None, attempt: int = 1) -> RunRecord:
    return RunRecord(
        run_id=run_id, task_type=TaskType.DAILY, as_of_date=as_of, state=state,
        started_at=started or datetime(2020, 1, 2, 9, 0), attempt=attempt,
    )


# =========================================================================== #
# state
# =========================================================================== #


class TestAtomicWrites:
    def test_text_and_json(self, tmp_path) -> None:
        p = tmp_path / "a" / "x.json"
        atomic_write_text(p, "hello")
        assert p.read_text(encoding="utf-8") == "hello"
        atomic_write_json(tmp_path / "b.json", {"k": "中文"})
        assert tmp_path.joinpath("b.json").read_text(encoding="utf-8").find("中文") >= 0


class TestStateStoreRuns:
    def test_save_and_load(self, tmp_path) -> None:
        s = StateStore(tmp_path)
        s.ensure_dirs()
        rec = _rec("r1", RunState.SUCCESS)
        s.save_run(rec)
        assert s.load_run(TaskType.DAILY, date(2020, 1, 2)).run_id == "r1"
        assert s.load_latest(TaskType.DAILY).run_id == "r1"

    def test_load_missing(self, tmp_path) -> None:
        s = StateStore(tmp_path)
        assert s.load_run(TaskType.DAILY, date(2020, 1, 2)) is None
        assert s.load_latest(TaskType.DAILY) is None

    def test_load_corrupt(self, tmp_path) -> None:
        s = StateStore(tmp_path)
        s.ensure_dirs()
        p = s.run_path(TaskType.DAILY, date(2020, 1, 2))
        p.write_text("not json {", encoding="utf-8")
        assert s.load_run(TaskType.DAILY, date(2020, 1, 2)) is None
        # 无效 RunRecord
        p.write_text('{"run_id": "x"}', encoding="utf-8")
        assert s.load_run(TaskType.DAILY, date(2020, 1, 2)) is None

    def test_list_runs(self, tmp_path) -> None:
        s = StateStore(tmp_path)
        s.ensure_dirs()
        assert s.list_runs(TaskType.DAILY) == []  # 目录不存在
        for d, rid in ((date(2020, 1, 1), "a"), (date(2020, 1, 3), "b"),
                       (date(2020, 1, 2), "c")):
            s.save_run(_rec(rid, RunState.SUCCESS, as_of=d))
        recs = s.list_runs(TaskType.DAILY)
        assert [r.run_id for r in recs] == ["b", "c", "a"]  # 倒序
        # 按状态过滤 + 限制
        only = s.list_runs(TaskType.DAILY, states=[RunState.FAILED])
        assert only == []
        lim = s.list_runs(TaskType.DAILY, limit=2)
        assert len(lim) == 2

    def test_find_by_run_id(self, tmp_path) -> None:
        s = StateStore(tmp_path)
        s.ensure_dirs()
        s.save_run(_rec("findme", RunState.SUCCESS, as_of=date(2020, 1, 2)))
        assert s.find_by_run_id("findme").run_id == "findme"
        assert s.find_by_run_id("missing") is None

    def test_save_run_guarded(self, tmp_path) -> None:
        s = StateStore(tmp_path)
        s.ensure_dirs()
        terminal = _rec("same", RunState.SUCCESS, as_of=date(2020, 1, 2))
        s.save_run(terminal)
        # 同 run_id 用非终态覆盖 -> 拒绝
        non_terminal = _rec("same", RunState.RUNNING, as_of=date(2020, 1, 2))
        path, ok = s.save_run_guarded(non_terminal)
        assert ok is False
        # 不同 run_id -> 允许
        other = _rec("other", RunState.RUNNING, as_of=date(2020, 1, 2))
        _, ok2 = s.save_run_guarded(other)
        assert ok2 is True


class TestStateStoreRecovery:
    def test_recover_interrupted(self, tmp_path) -> None:
        s = StateStore(tmp_path)
        s.ensure_dirs()
        now = datetime(2020, 1, 2, 12, 0)
        # 注意：save_run 按业务日落盘，old/fresh 必须用不同 as_of_date 才不会互相覆盖
        old = _rec("old", RunState.RUNNING, as_of=date(2020, 1, 1),
                   started=datetime(2020, 1, 1, 0, 0))
        fresh = _rec("fresh", RunState.RUNNING, as_of=date(2020, 1, 2),
                     started=now - timedelta(seconds=10))
        s.save_run(old)
        s.save_run(fresh)
        recovered = s.recover_interrupted(TaskType.DAILY, now=now, max_running_seconds=21600)
        ids = {r.run_id for r in recovered}
        assert "old" in ids
        assert "fresh" not in ids  # 未超时，跳过
        reloaded = s.load_run(TaskType.DAILY, date(2020, 1, 1))
        assert reloaded.state == RunState.FAILED

    def test_recover_updates_latest(self, tmp_path) -> None:
        s = StateStore(tmp_path)
        s.ensure_dirs()
        now = datetime(2020, 1, 2, 12, 0)
        rec = _rec("only", RunState.RUNNING, as_of=date(2020, 1, 2),
                   started=datetime(2020, 1, 1, 0, 0))
        s.save_run(rec)
        s.recover_interrupted(TaskType.DAILY, now=now)
        assert s.load_latest(TaskType.DAILY).state == RunState.FAILED


class TestStateStorePendingSignals:
    def test_save_load_clear(self, tmp_path) -> None:
        s = StateStore(tmp_path)
        assert s.load_pending_signals() is None
        s.save_pending_signals({"signals": [1, 2]})
        assert s.load_pending_signals()["signals"] == [1, 2]
        s.clear_pending_signals()
        cleared = s.load_pending_signals()
        assert cleared["signals"] == []

    def test_load_corrupt(self, tmp_path) -> None:
        s = StateStore(tmp_path)
        s.pending_signals_path().write_text("{broken", encoding="utf-8")
        assert s.load_pending_signals() is None


class TestStateStoreAccounts:
    def _acc(self, aid: str) -> SimulatedAccountState:
        return SimulatedAccountState(
            account_id=aid, strategy_track=StrategyTrack.STEADY,
            eligibility_status=EligibilityStatus.SIMULATION_ONLY,
        )

    def test_save_load(self, tmp_path) -> None:
        s = StateStore(tmp_path)
        s.ensure_dirs()
        acc = self._acc("paper-steady")
        s.save_account(acc)
        got = s.load_account("paper-steady")
        assert got is not None and got.account_id == "paper-steady"

    def test_load_missing(self, tmp_path) -> None:
        s = StateStore(tmp_path)
        assert s.load_account("nope") is None
        assert s.list_accounts() == []

    def test_load_corrupt(self, tmp_path) -> None:
        s = StateStore(tmp_path)
        s.ensure_dirs()
        p = s.account_path("bad")
        p.write_text("not json", encoding="utf-8")
        assert s.load_account("bad") is None
        p.write_text('{"account_id": "bad"}', encoding="utf-8")
        assert s.load_account("bad") is None

    def test_list_accounts(self, tmp_path) -> None:
        s = StateStore(tmp_path)
        s.ensure_dirs()
        s.save_account(self._acc("paper-steady"))
        s.save_account(self._acc("paper-aggressive"))
        ids = {a.account_id for a in s.list_accounts()}
        assert ids == {"paper-steady", "paper-aggressive"}


# =========================================================================== #
# locking
# =========================================================================== #


class TestReadLock:
    def test_missing(self, tmp_path) -> None:
        assert read_lock(tmp_path / "no.lock") is None

    def test_corrupt_json(self, tmp_path) -> None:
        p = tmp_path / "l.lock"
        p.write_text("}{", encoding="utf-8")
        assert read_lock(p) is None

    def test_invalid_info(self, tmp_path) -> None:
        p = tmp_path / "l.lock"
        p.write_text('{"pid": "abc"}', encoding="utf-8")  # 缺字段
        assert read_lock(p) is None


class TestProcessAlive:
    def test_none_or_negative(self) -> None:
        assert process_alive(None) is False
        assert process_alive(0) is False
        assert process_alive(-5) is False


def _lockinfo(pid: int, started: datetime, run_id: str = "holder") -> LockInfo:
    return LockInfo(
        pid=pid, hostname="host-a", task_type=TaskType.DAILY,
        as_of_date=date(2020, 1, 2), started_at=started, run_id=run_id,
    )


class TestRunLock:
    def _make(self, tmp_path, *, alive_fn=lambda pid: True,
              now=datetime(2020, 1, 2, 9, 0), run_id="cur", **kw) -> RunLock:
        return RunLock(
            tmp_path / "run.lock", task_type=TaskType.DAILY,
            as_of_date=date(2020, 1, 2), stale_after_seconds=21600,
            run_id=run_id, alive_fn=alive_fn, now_fn=lambda: now, **kw,
        )

    def test_try_acquire_fresh(self, tmp_path) -> None:
        lock = self._make(tmp_path)
        res = lock.try_acquire()
        assert res.acquired is True
        assert lock.held is True

    def test_try_acquire_already_held(self, tmp_path) -> None:
        lock = self._make(tmp_path)
        lock.try_acquire()
        res = lock.try_acquire()
        assert res.acquired is True

    def test_acquire_held(self, tmp_path) -> None:
        lock = self._make(tmp_path)
        info = lock.acquire()
        assert info.pid is not None

    def test_acquire_held_error(self, tmp_path) -> None:
        import socket
        host = socket.gethostname()
        lock = self._make(tmp_path, alive_fn=lambda pid: True, hostname=host)
        # 先放一个活跃 holder（同主机 + 进程存活 -> 不可接管）
        holder = _lockinfo(999, datetime(2020, 1, 2, 8, 0))
        holder.hostname = host
        import json
        (tmp_path / "run.lock").write_text(
            json.dumps(holder.to_dict()), encoding="utf-8")
        with pytest.raises(LockHeldError):
            lock.acquire()

    def test_stale_steal(self, tmp_path) -> None:
        # holder 进程已死 -> 可接管
        holder = _lockinfo(999, datetime(2020, 1, 1, 9, 0))
        import json
        (tmp_path / "run.lock").write_text(
            json.dumps(holder.to_dict()), encoding="utf-8")
        lock = self._make(tmp_path, alive_fn=lambda pid: False,
                          now=datetime(2020, 1, 2, 9, 0), run_id="cur")
        res = lock.try_acquire()
        assert res.acquired is True
        assert res.stole_stale is True

    def test_not_stale(self, tmp_path) -> None:
        holder = _lockinfo(999, datetime(2020, 1, 2, 8, 0))
        import json
        (tmp_path / "run.lock").write_text(
            json.dumps(holder.to_dict()), encoding="utf-8")
        lock = self._make(tmp_path, alive_fn=lambda pid: True,
                          now=datetime(2020, 1, 2, 9, 0))
        res = lock.try_acquire()
        assert res.acquired is False

    def test_corrupt_lock_cleaned(self, tmp_path) -> None:
        (tmp_path / "run.lock").write_text("garbage", encoding="utf-8")
        lock = self._make(tmp_path)
        res = lock.try_acquire()
        assert res.acquired is True
        assert res.stole_stale is True

    def test_preempted(self, tmp_path) -> None:
        holder = _lockinfo(999, datetime(2020, 1, 1, 9, 0))
        import json
        (tmp_path / "run.lock").write_text(
            json.dumps(holder.to_dict()), encoding="utf-8")
        lock = self._make(tmp_path, alive_fn=lambda pid: False)
        # 模拟抢先：_write_exclusive 永远失败
        lock._write_exclusive = lambda info: False  # type: ignore[assignment]
        res = lock.try_acquire()
        assert res.acquired is False

    def test_allow_steal_active_branch(self, tmp_path) -> None:
        holder = _lockinfo(999, datetime(2020, 1, 2, 8, 0))
        import json
        (tmp_path / "run.lock").write_text(
            json.dumps(holder.to_dict()), encoding="utf-8")
        lock = self._make(tmp_path, alive_fn=lambda pid: True,
                          allow_steal_active=True)
        # allow_steal_active 路径仍走删除后重建（此处进程仍"存活"，删除应失败 -> False）
        # 覆盖该分支代码即可
        res = lock.try_acquire()
        assert res.acquired is False

    def test_release_not_held(self, tmp_path) -> None:
        lock = self._make(tmp_path)
        assert lock.release() is False

    def test_release_normal_and_gone(self, tmp_path) -> None:
        lock = self._make(tmp_path)
        lock.try_acquire()
        assert lock.release() is True  # 正常释放
        # _held 已置 False，再次释放走 not-held 分支
        assert lock.release() is False

    def test_release_file_gone_while_held(self, tmp_path) -> None:
        lock = self._make(tmp_path)
        lock.try_acquire()
        # 持锁期间文件被外部删除 -> 释放仍判成功
        lock.lock_path.unlink(missing_ok=True)
        assert lock.release() is True

    def test_release_stolen_by_other(self, tmp_path) -> None:
        lock = self._make(tmp_path, run_id="cur")
        lock.try_acquire()
        # 外部把锁改成别人的
        other = _lockinfo(12345, datetime(2020, 1, 2, 9, 0), run_id="other")
        import json
        (tmp_path / "run.lock").write_text(
            json.dumps(other.to_dict()), encoding="utf-8")
        assert lock.release() is False

    def test_context_manager(self, tmp_path) -> None:
        lock = self._make(tmp_path)
        with lock:
            assert lock.held is True
        assert lock.held is False
