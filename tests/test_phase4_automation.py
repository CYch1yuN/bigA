"""Phase 4 本机自动化系统单元测试与集成测试。

所有测试均使用 ``tmp_path`` 作为配置基准目录，生成的 state/reports/logs 只写在
pytest 临时目录，绝不污染仓库。离线验证统一使用 ``tests.research_samples`` 的
合成行情 + 注入数据源（与 _smoke_daily.py / _smoke_weekly.py 一致），绝不伪造
"在线抓取成功"。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

# 确保 tests 包可导入（与研究样本同目录）
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from ashare_quant.automation.archive import ArchiveResult, _assert_inside, archive_results
from ashare_quant.automation.calendar import TradingCalendar
from ashare_quant.automation.config import (
    AccountConfig,
    AutomationConfig,
    DataConfig,
    LoggingConfig,
    PathsConfig,
    default_automation_config_path,
    load_automation_config,
)
from ashare_quant.automation.idempotency import build_fingerprint
from ashare_quant.automation.locking import RunLock
from ashare_quant.automation.models import (
    CalendarUnavailableError,
    EligibilityStatus,
    LockInfo,
    NotEligibleError,
    RunState,
    StrategyTrack,
    TaskType,
    TRACK_ELIGIBILITY,
)
from ashare_quant.automation.reporting import iso_week_key, result_paths
from ashare_quant.automation.runner import (
    AutomationRunner,
    NonTradingDay,
    map_exception_state,
)
from ashare_quant.automation.scheduler import build_scheduler_plan
from ashare_quant.automation.simulated_account import (
    SimulatedAccountManager,
    assert_simulation_only,
)
from ashare_quant.automation.state import StateStore
from ashare_quant.backtest.config import BacktestConfig
from ashare_quant.automation.weekly import WEEKLY_STEPS, WeeklyPipeline, run_weekly
from ashare_quant.config import load_config
from tests.research_samples import (
    make_benchmark_data,
    make_historical_status_table,
    make_research_quotes,
)


# ---------------------------------------------------------------------- #
# 合成环境构造
# ---------------------------------------------------------------------- #

def _synthetic(base_dir: Path):
    """构造离线验证环境（合成行情 + 注入数据源 + 合成日历）。"""
    app_cfg = load_config(_ROOT / "config" / "default.yaml")
    start = date(2020, 1, 2)
    n_days = 200
    quotes = make_research_quotes(start=start, n_days=n_days, n_stocks=8)
    status_df = make_historical_status_table(start=start, n_stocks=8)
    bench = make_benchmark_data(start=start, n_days=n_days)
    trade_dates = sorted({pd.Timestamp(d).date() for d in quotes["trade_date"]})
    cal = TradingCalendar.from_dates(trade_dates, source="synthetic-calendar")
    from ashare_quant.automation.datasource import InjectedDataSource

    source = InjectedDataSource(
        quotes,
        name="synthetic-research-samples",
        synthetic=True,
        security_master=status_df,
        benchmark=bench,
    )
    # 离线演示：symbols=[]（从数据推导股票池），回看窗口缩短到合成区间之内
    config = AutomationConfig(
        paths=PathsConfig(
            data_dir="data",
            state_dir="state",
            reports_dir="reports",
            logs_dir="logs",
            archive_dir="reports/archive",
        ),
        data=DataConfig(symbols=[], lookback_days=200),
        logging=LoggingConfig(console=False),
        accounts=[
            AccountConfig(
                account_id="paper-steady",
                track=StrategyTrack.STEADY,
                initial_cash=1000.0,
                eligibility_status=EligibilityStatus.NOT_ELIGIBLE_FOR_LIVE_TRADING,
            ),
            AccountConfig(
                account_id="paper-aggressive",
                track=StrategyTrack.AGGRESSIVE,
                initial_cash=1000.0,
                eligibility_status=EligibilityStatus.SIMULATION_ONLY,
            ),
        ],
    ).with_base_dir(base_dir)
    universe_kwargs = {"min_turnover": 0.0, "min_listing_days": 120}
    return config, source, cal, trade_dates, app_cfg, universe_kwargs


# ---------------------------------------------------------------------- #
# 模型与配置
# ---------------------------------------------------------------------- #

def test_track_eligibility_mapping():
    assert TRACK_ELIGIBILITY[StrategyTrack.STEADY] is EligibilityStatus.NOT_ELIGIBLE_FOR_LIVE_TRADING
    assert TRACK_ELIGIBILITY[StrategyTrack.AGGRESSIVE] is EligibilityStatus.SIMULATION_ONLY


def test_account_eligibility_validation_rejects_wrong_status():
    # 稳健轨若配置成 SIMULATION_ONLY 必须在加载期被拒绝
    with pytest.raises(Exception):
        AccountConfig(
            account_id="x",
            track=StrategyTrack.STEADY,
            initial_cash=1000.0,
            eligibility_status=EligibilityStatus.SIMULATION_ONLY,
        )


def test_run_state_has_nine_states():
    assert len(list(RunState)) == 9


def test_not_eligible_error_is_raisable():
    with pytest.raises(NotEligibleError):
        raise NotEligibleError("资格闸门触发")


def test_load_default_automation_config():
    cfg = load_automation_config()
    assert cfg.live_trading.enabled is False
    assert cfg.archive.enabled is True
    assert len(cfg.accounts) == 2


def test_config_hash_stable():
    a = load_automation_config()
    b = load_automation_config()
    from ashare_quant.automation.idempotency import compute_config_hash

    assert compute_config_hash(a) == compute_config_hash(b)


# ---------------------------------------------------------------------- #
# 交易日历（fail-closed）
# ---------------------------------------------------------------------- #

def test_calendar_sorted_unique():
    cal = TradingCalendar.from_dates(
        [date(2020, 1, 3), date(2020, 1, 1), date(2020, 1, 1)]
    )
    assert cal.dates == (date(2020, 1, 1), date(2020, 1, 3))


def test_calendar_trading_days_between_inclusive():
    cal = TradingCalendar.from_dates(
        [date(2020, 1, 1), date(2020, 1, 2), date(2020, 1, 3)]
    )
    assert cal.trading_days_between(date(2020, 1, 1), date(2020, 1, 3)) == [
        date(2020, 1, 1),
        date(2020, 1, 2),
        date(2020, 1, 3),
    ]


def test_calendar_covers():
    cal = TradingCalendar.from_dates([date(2020, 1, 1), date(2020, 1, 2)])
    assert cal.covers(date(2020, 1, 1)) is True
    assert cal.covers(date(2020, 1, 5)) is False


def test_calendar_empty_raises():
    with pytest.raises(CalendarUnavailableError):
        TradingCalendar.from_dates([])


def test_calendar_fresh_failure_when_stale():
    cal = TradingCalendar.from_dates([date(2020, 1, 1), date(2020, 1, 2)])
    with pytest.raises(CalendarUnavailableError):
        cal.assert_fresh(date(2021, 1, 1), max_staleness_days=30)


# ---------------------------------------------------------------------- #
# 跨进程锁
# ---------------------------------------------------------------------- #

def test_lock_acquire_release(tmp_path):
    lock = RunLock(
        tmp_path / "automation.lock",
        task_type=TaskType.DAILY,
        as_of_date=date(2020, 1, 1),
        stale_after_seconds=21600,
    )
    acq = lock.try_acquire()
    assert acq.acquired is True
    assert lock.held is True
    assert lock.release() is True
    assert lock.held is False


def test_lock_exclusive_blocks_second_holder(tmp_path):
    lock = RunLock(
        tmp_path / "automation.lock",
        task_type=TaskType.DAILY,
        as_of_date=date(2020, 1, 1),
        stale_after_seconds=21600,
    )
    lock.try_acquire()
    other = RunLock(
        tmp_path / "automation.lock",
        task_type=TaskType.DAILY,
        as_of_date=date(2020, 1, 1),
        stale_after_seconds=21600,
    )
    res = other.try_acquire()
    assert res.acquired is False
    lock.release()


def test_lock_stale_takeover(tmp_path):
    lock_path = tmp_path / "automation.lock"
    old = LockInfo(
        pid=999999,
        hostname="host",
        task_type=TaskType.DAILY,
        as_of_date=date(2020, 1, 1),
        started_at=datetime(2000, 1, 1),
        code_commit="x",
        run_id="y",
    )
    lock_path.write_text(json.dumps(old.to_dict()), encoding="utf-8")
    lock = RunLock(
        lock_path,
        task_type=TaskType.DAILY,
        as_of_date=date(2020, 1, 1),
        stale_after_seconds=21600,
        alive_fn=lambda pid: False,  # 持锁进程已消失
    )
    res = lock.try_acquire()
    assert res.acquired is True
    assert res.stole_stale is True


def test_lock_release_confirms_file_removed(tmp_path):
    """release() 必须确认锁文件真的消失，而不是"调用过 unlink"就算数。"""
    lock_path = tmp_path / "automation.lock"
    lock = RunLock(
        lock_path,
        task_type=TaskType.DAILY,
        as_of_date=date(2020, 1, 1),
        stale_after_seconds=21600,
        run_id="run-a",
    )
    lock.try_acquire()
    assert lock_path.exists()
    assert lock.release() is True
    assert not lock_path.exists()
    assert lock.release_error is None


def test_lock_release_retries_transient_unlink_failure(tmp_path, monkeypatch):
    """Windows 上杀软/索引服务可能短暂持有句柄；release() 应有界重试而非静默失败。"""
    lock_path = tmp_path / "automation.lock"
    lock = RunLock(
        lock_path,
        task_type=TaskType.DAILY,
        as_of_date=date(2020, 1, 1),
        stale_after_seconds=21600,
        run_id="run-a",
    )
    lock.try_acquire()

    real_unlink = Path.unlink
    calls = {"n": 0}

    def flaky_unlink(self, *args, **kwargs):
        if self == lock_path:
            calls["n"] += 1
            if calls["n"] <= 2:  # 前两次模拟句柄被占用
                raise PermissionError("被其他进程占用")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    assert lock.release() is True
    assert calls["n"] >= 3
    assert not lock_path.exists()
    assert lock.release_error is None


def test_lock_release_failure_is_surfaced(tmp_path, monkeypatch):
    """删除持续失败时必须返回 False 并记录原因，不允许静默吞掉。"""
    lock_path = tmp_path / "automation.lock"
    lock = RunLock(
        lock_path,
        task_type=TaskType.DAILY,
        as_of_date=date(2020, 1, 1),
        stale_after_seconds=21600,
        run_id="run-a",
    )
    lock.try_acquire()

    def always_fail(self, *args, **kwargs):
        raise PermissionError("句柄长期被占用")

    monkeypatch.setattr(Path, "unlink", always_fail)
    assert lock.release() is False
    assert isinstance(lock.release_error, OSError)


def test_lock_orphan_from_same_process_is_taken_over(tmp_path):
    """本进程遗留的孤儿锁必须可接管，否则一次删除失败会永久阻断自己。

    探活对"自己的 pid"永远返回存活，如果不在探活前拦下，残留锁会把同一台机器上
    后续所有运行都判成 BLOCKED_LOCKED（正是真实跑批中观测到的幽灵锁现象）。
    """
    lock_path = tmp_path / "automation.lock"
    orphan = LockInfo(
        pid=os.getpid(),  # 就是当前进程
        hostname=socket.gethostname(),
        task_type=TaskType.DAILY,
        as_of_date=date(2020, 1, 1),
        started_at=datetime.now().replace(microsecond=0),
        code_commit="x",
        run_id="previous-run",
    )
    lock_path.write_text(json.dumps(orphan.to_dict()), encoding="utf-8")

    lock = RunLock(
        lock_path,
        task_type=TaskType.DAILY,
        as_of_date=date(2020, 1, 2),
        stale_after_seconds=21600,
        run_id="current-run",
        alive_fn=lambda pid: True,  # 自己的进程当然存活
    )
    res = lock.try_acquire()
    assert res.acquired is True
    assert res.stole_stale is True
    assert "孤儿锁" in res.reason


def test_lock_same_process_same_run_id_still_blocks(tmp_path):
    """孤儿锁豁免只对"不同 run_id"生效，同一 run_id 的活跃锁仍必须阻断。"""
    lock_path = tmp_path / "automation.lock"
    first = RunLock(
        lock_path,
        task_type=TaskType.DAILY,
        as_of_date=date(2020, 1, 1),
        stale_after_seconds=21600,
        run_id="same-run",
    )
    first.try_acquire()
    second = RunLock(
        lock_path,
        task_type=TaskType.DAILY,
        as_of_date=date(2020, 1, 1),
        stale_after_seconds=21600,
        run_id="same-run",
        alive_fn=lambda pid: True,
    )
    res = second.try_acquire()
    assert res.acquired is False
    first.release()


# ---------------------------------------------------------------------- #
# 状态仓库
# ---------------------------------------------------------------------- #

def test_state_store_account_roundtrip(tmp_path):
    config, source, cal, trade_dates, app_cfg, uk = _synthetic(tmp_path)
    store = StateStore(config.state_dir)
    mgr = SimulatedAccountManager(config, BacktestConfig())
    acc = mgr.create_account(config.accounts[0])
    store.save_account(acc)
    loaded = store.load_account("paper-steady")
    assert loaded is not None
    assert loaded.account_id == "paper-steady"
    assert loaded.eligibility_status is EligibilityStatus.NOT_ELIGIBLE_FOR_LIVE_TRADING


# ---------------------------------------------------------------------- #
# 幂等指纹
# ---------------------------------------------------------------------- #

def test_fingerprint_deterministic():
    config = load_automation_config()
    f1 = build_fingerprint(
        config, task_type=TaskType.DAILY, as_of_date=date(2020, 1, 1)
    )
    f2 = build_fingerprint(
        config, task_type=TaskType.DAILY, as_of_date=date(2020, 1, 1)
    )
    assert f1.run_id == f2.run_id


# ---------------------------------------------------------------------- #
# 运行编排器：异常 -> 终态映射
# ---------------------------------------------------------------------- #

def test_runner_non_trading_day(tmp_path):
    from ashare_quant.automation.runner import AutomationRunner, NonTradingDay

    config, *_ = _synthetic(tmp_path)
    runner = AutomationRunner(config, task_type=TaskType.DAILY)

    def pipeline(ctx):
        raise NonTradingDay("不是交易日")

    out = runner.run(pipeline, as_of_date=date(2020, 1, 1))
    assert out.state is RunState.SKIPPED_NON_TRADING_DAY
    assert out.exit_code == 0


def test_runner_not_eligible(tmp_path):
    config, *_ = _synthetic(tmp_path)
    runner = AutomationRunner(config, task_type=TaskType.DAILY)

    def pipeline(ctx):
        raise NotEligibleError("资格未通过")

    out = runner.run(pipeline, as_of_date=date(2020, 1, 5))
    assert out.state is RunState.BLOCKED_NOT_ELIGIBLE
    assert out.exit_code == 5


# ---------------------------------------------------------------------- #
# 每日管线（合成）
# ---------------------------------------------------------------------- #

def test_daily_pipeline_success(tmp_path):
    from ashare_quant.automation.daily import DailyPipeline, run_daily

    config, source, cal, trade_dates, app_cfg, uk = _synthetic(tmp_path)
    store = StateStore(config.state_dir)
    d = trade_dates[-1]
    out = run_daily(
        config,
        as_of_date=d,
        data_source=source,
        pipeline=DailyPipeline(app_config=app_cfg, calendar=cal, universe_kwargs=uk),
        state_store=store,
    )
    assert out.state is RunState.SUCCESS


def test_daily_idempotent_reuse(tmp_path):
    from ashare_quant.automation.daily import DailyPipeline, run_daily

    config, source, cal, trade_dates, app_cfg, uk = _synthetic(tmp_path)
    store = StateStore(config.state_dir)
    d = trade_dates[-1]
    pipe = DailyPipeline(app_config=app_cfg, calendar=cal, universe_kwargs=uk)
    out1 = run_daily(
        config, as_of_date=d, data_source=source, pipeline=pipe, state_store=store
    )
    out2 = run_daily(
        config, as_of_date=d, data_source=source, pipeline=pipe, state_store=store
    )
    assert out1.state is RunState.SUCCESS
    assert out2.reused is True


def test_daily_skips_non_trading_day(tmp_path):
    """日历覆盖范围内、但不是交易日 -> SKIPPED_NON_TRADING_DAY（退出码 0）。"""
    from ashare_quant.automation.daily import DailyPipeline, run_daily

    config, source, cal, trade_dates, app_cfg, uk = _synthetic(tmp_path)
    store = StateStore(config.state_dir)

    # 在日历覆盖区间内找一个不属于交易日集合的日期（合成行情按工作日生成，周末即缺口）
    trade_set = set(trade_dates)
    nd = None
    probe = trade_dates[10]
    while probe < trade_dates[-1]:
        probe = probe + timedelta(days=1)
        if probe not in trade_set:
            nd = probe
            break
    assert nd is not None, "合成日历中未找到非交易日缺口"
    assert cal.covers(nd), "所选日期必须落在日历覆盖范围内"

    out = run_daily(
        config,
        as_of_date=nd,
        data_source=source,
        pipeline=DailyPipeline(app_config=app_cfg, calendar=cal, universe_kwargs=uk),
        state_store=store,
    )
    assert out.state is RunState.SKIPPED_NON_TRADING_DAY
    assert out.exit_code == 0


def test_daily_fails_closed_when_date_outside_calendar(tmp_path):
    """日历覆盖范围之外的日期属于「未知」而非「非交易日」-> fail-closed 判 FAILED。

    这是 Phase 4 的核心安全语义：绝不按工作日推断交易日。
    """
    from ashare_quant.automation.daily import DailyPipeline, run_daily

    config, source, cal, trade_dates, app_cfg, uk = _synthetic(tmp_path)
    store = StateStore(config.state_dir)
    narrow = TradingCalendar.from_dates([trade_dates[-1]], source="narrow")
    outside = date(2019, 6, 1)  # 远早于 narrow 日历首日
    assert not narrow.covers(outside)

    out = run_daily(
        config,
        as_of_date=outside,
        data_source=source,
        pipeline=DailyPipeline(app_config=app_cfg, calendar=narrow, universe_kwargs=uk),
        state_store=store,
    )
    assert out.state is RunState.FAILED
    cal_step = {s.name: s for s in out.record.steps}["calendar"]
    assert "超出交易日历覆盖范围" in (cal_step.error or "")


# ---------------------------------------------------------------------- #
# 每周管线（合成）
# ---------------------------------------------------------------------- #

def _run_window_and_saturday(config, source, cal, trade_dates, app_cfg, uk, store, n=10):
    from ashare_quant.automation.daily import DailyPipeline, run_daily

    window = trade_dates[-n:]
    for d in window:
        run_daily(
            config,
            as_of_date=d,
            data_source=source,
            pipeline=DailyPipeline(app_config=app_cfg, calendar=cal, universe_kwargs=uk),
            state_store=store,
        )
    anchor = window[5]
    as_of_sat = anchor + timedelta(days=(5 - anchor.weekday()) % 7)
    cal2 = TradingCalendar.from_dates(
        list(trade_dates) + [as_of_sat], source="syn+asof"
    )
    return as_of_sat, cal2


def test_weekly_pipeline_full_and_readonly(tmp_path):
    config, source, cal, trade_dates, app_cfg, uk = _synthetic(tmp_path)
    store = StateStore(config.state_dir)
    as_of_sat, cal2 = _run_window_and_saturday(
        config, source, cal, trade_dates, app_cfg, uk, store
    )

    obs_before = {
        a.account_id: store.load_account(a.account_id).observation_days
        for a in config.accounts
    }
    out = run_weekly(
        config,
        as_of_date=as_of_sat,
        pipeline=WeeklyPipeline(calendar=cal2),
        state_store=store,
    )
    assert out.state is RunState.SUCCESS
    step_names = [s.name for s in out.record.steps]
    assert all(n in step_names for n in WEEKLY_STEPS)
    # 账户状态只读：observation_days 不被周报改写
    obs_after = {
        a.account_id: store.load_account(a.account_id).observation_days
        for a in config.accounts
    }
    assert obs_before == obs_after


def test_weekly_gap_detected(tmp_path):
    config, source, cal, trade_dates, app_cfg, uk = _synthetic(tmp_path)
    store = StateStore(config.state_dir)
    as_of_sat, cal2 = _run_window_and_saturday(
        config, source, cal, trade_dates, app_cfg, uk, store
    )
    window = trade_dates[-10:]
    gap_day = window[3]
    # 删除该日运行记录与报告目录，模拟"漏跑"
    store.run_path(TaskType.DAILY, gap_day).unlink(missing_ok=True)
    rep = config.reports_dir / "daily" / gap_day.isoformat()
    if rep.exists():
        shutil.rmtree(rep)

    out = run_weekly(
        config,
        as_of_date=as_of_sat,
        pipeline=WeeklyPipeline(calendar=cal2),
        state_store=store,
        force_retry=True,
    )
    # 缺口是"发现"不是"故障"：周报仍 SUCCESS
    assert out.state is RunState.SUCCESS
    cov = next(s for s in out.record.steps if s.name == "coverage_audit")
    assert gap_day.isoformat() in (cov.detail.get("missing_days") or [])
    # 缺口章节显式列出缺失交易日
    md = result_paths(
        config, task_type=TaskType.WEEKLY, as_of_date=as_of_sat
    ).report_md
    assert gap_day.isoformat() in md.read_text(encoding="utf-8")


# ---------------------------------------------------------------------- #
# 归档（三保险）
# ---------------------------------------------------------------------- #

def test_archive_moves_old_dir(tmp_path):
    config, *_ = _synthetic(tmp_path)
    old_day = date(2019, 1, 7)
    old_dir = config.reports_dir / "daily" / old_day.isoformat()
    old_dir.mkdir(parents=True)
    (old_dir / "x.json").write_text("{}")
    res = archive_results(config, as_of_date=date(2020, 10, 1), batch_key="2020-W42")
    assert res.archived_count >= 1
    assert not old_dir.exists()
    assert (
        config.archive_dir / "2020-W42" / "daily" / old_day.isoformat()
    ).exists()


def test_archive_skips_current_week(tmp_path):
    config, *_ = _synthetic(tmp_path)
    old = config.reports_dir / "daily" / "2019-01-07"
    old.mkdir(parents=True)
    (old / "x.json").write_text("{}")
    cur = config.reports_dir / "daily" / "2020-10-01"
    cur.mkdir(parents=True)
    (cur / "x.json").write_text("{}")
    res = archive_results(
        config,
        as_of_date=date(2020, 10, 1),
        batch_key="2020-W41",
        protect_buckets={"2020-10-01"},
    )
    assert res.archived_count == 1  # 旧的搬走
    assert not old.exists()
    assert cur.exists()  # 当前批次受保护


def test_archive_dry_run_no_op(tmp_path):
    config, *_ = _synthetic(tmp_path)
    old = config.reports_dir / "daily" / "2019-01-07"
    old.mkdir(parents=True)
    (old / "x.json").write_text("{}")
    res = archive_results(
        config, as_of_date=date(2020, 10, 1), batch_key="2020-W41", dry_run=True
    )
    assert res.archived_count >= 1
    assert old.exists()  # dry-run 不移动


def test_archive_assert_inside_blocks_escape(tmp_path):
    root = tmp_path / "archive"
    root.mkdir()
    with pytest.raises(ValueError):
        _assert_inside(tmp_path / "outside.txt", root)


# ---------------------------------------------------------------------- #
# 调度计划生成（可单测，不依赖 Windows）
# ---------------------------------------------------------------------- #

def test_scheduler_plan_two_tasks():
    config = load_automation_config()
    plan = build_scheduler_plan(config)
    assert len(plan.tasks) == 2
    names = plan.task_names()
    assert any(n.endswith("-Daily") for n in names)
    assert any(n.endswith("-Weekly") for n in names)


def test_scheduler_plan_commands():
    config = load_automation_config()
    plan = build_scheduler_plan(config)
    cmds = plan.schtasks_commands()
    assert any("/SC DAILY" in c for c in cmds)
    assert any("/SC WEEKLY" in c for c in cmds)
    cmds_f = plan.schtasks_commands(force=True)
    assert any("/Delete" in c for c in cmds_f)

    # DAILY 命令不得携带 /D（schtasks 对 /SC DAILY 不接受 /D，会报
    # "Invalid syntax. Value expected for '/D'" 导致任务创建失败——这是
    # install_scheduler.ps1 历史 bug 的语义锁定）。
    daily_cmd = next(c for c in cmds if "/SC DAILY" in c)
    assert "/D " not in daily_cmd, f"DAILY 任务命令不得包含 /D: {daily_cmd}"
    weekly_cmd = next(c for c in cmds if "/SC WEEKLY" in c)
    assert "/D SAT" in weekly_cmd


# ---------------------------------------------------------------------- #
# 模拟账户与观察窗口
# ---------------------------------------------------------------------- #

def test_observation_progress_advances(tmp_path):
    config, source, cal, trade_dates, app_cfg, uk = _synthetic(tmp_path)
    from ashare_quant.automation.daily import DailyPipeline, run_daily

    store = StateStore(config.state_dir)
    mgr = SimulatedAccountManager(config, BacktestConfig())
    acc = store.load_account("paper-steady")
    before = mgr.observation_progress(acc)["observed_trading_days"] if acc else 0
    for d in trade_dates[-3:]:
        run_daily(
            config,
            as_of_date=d,
            data_source=source,
            pipeline=DailyPipeline(app_config=app_cfg, calendar=cal, universe_kwargs=uk),
            state_store=store,
        )
    acc = store.load_account("paper-steady")
    after = mgr.observation_progress(acc)["observed_trading_days"]
    assert after >= before + 1


def test_simulation_only_guard_passes_when_disabled():
    config = load_automation_config()
    # live_trading.enabled=False 应通过（返回 None 或不抛异常）
    assert assert_simulation_only(config) is None


# ---------------------------------------------------------------------- #
# CLI（只读子命令）
# ---------------------------------------------------------------------- #

def test_cli_verify_synthetic(capsys):
    from ashare_quant.cli import main

    rc = main(["automation", "verify", "--synthetic"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "实盘开关已关闭" in out


def test_cli_status_readonly(capsys):
    from ashare_quant.cli import main

    rc = main(["automation", "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "自动化系统状态" in out


# ---------------------------------------------------------------------- #
# Gate 4B 正式观察 trigger 区分（manual vs scheduled）
# ---------------------------------------------------------------------- #

def test_run_record_default_trigger_is_manual():
    """RunRecord 默认 trigger=manual；旧记录（缺字段）加载也视为 manual。"""
    from ashare_quant.automation.models import RunRecord

    rec = RunRecord(run_id="x", task_type=TaskType.DAILY, as_of_date=date(2026, 8, 3))
    assert rec.trigger == "manual"
    legacy = RunRecord.from_dict(
        {
            "run_id": "y",
            "task_type": "daily",
            "as_of_date": "2026-08-03",
            "state": "SUCCESS",
        }
    )
    assert legacy.trigger == "manual"


def test_run_daily_persists_trigger(tmp_path):
    """run_daily 透传 trigger 到运行记录（scheduled 落盘可被 Gate 4B 识别）。"""
    config, source, cal, trade_dates, app_cfg, uk = _synthetic(tmp_path)
    from ashare_quant.automation.daily import DailyPipeline, run_daily

    store = StateStore(config.state_dir)
    d = trade_dates[-1]
    run_daily(
        config,
        as_of_date=d,
        data_source=source,
        pipeline=DailyPipeline(app_config=app_cfg, calendar=cal, universe_kwargs=uk),
        state_store=store,
        trigger="scheduled",
    )
    rec = store.load_latest(TaskType.DAILY)
    assert rec is not None
    assert rec.trigger == "scheduled"


def test_gate4b_observation_ignores_manual_runs(tmp_path):
    """正式观察只统计 scheduled：手工首跑（默认 manual）不得计入 60 日。"""
    config, source, cal, trade_dates, app_cfg, uk = _synthetic(tmp_path)
    from ashare_quant.automation.daily import DailyPipeline, run_daily

    store = StateStore(config.state_dir)
    d = trade_dates[-1]
    # 手工运行：不带 --trigger，默认 manual
    run_daily(
        config,
        as_of_date=d,
        data_source=source,
        pipeline=DailyPipeline(app_config=app_cfg, calendar=cal, universe_kwargs=uk),
        state_store=store,
    )
    scheduled = [
        r for r in store.list_runs(TaskType.DAILY)
        if getattr(r, "trigger", "manual") == "scheduled"
    ]
    assert scheduled == []
    assert store.load_latest(TaskType.DAILY).trigger == "manual"


# ---------------------------------------------------------------------- #
# Gate 4B tracker 直接调用（真实 _track_real，不复制过滤表达式）
# ---------------------------------------------------------------------- #

def _track_real_run(tmp_path, *, trigger, online, synthetic, write_data_update):
    """构造一日运行 + data-update.json 证明，直接调用 _track_real 返回 summary。"""
    _scripts = Path(__file__).resolve().parents[1] / "scripts"
    if str(_scripts) not in sys.path:
        sys.path.insert(0, str(_scripts))
    import gate4b_observation as g4b

    config, source, cal, trade_dates, app_cfg, uk = _synthetic(tmp_path)
    from ashare_quant.automation.daily import DailyPipeline, run_daily

    store = StateStore(config.state_dir)
    d = trade_dates[-1]
    run_daily(
        config,
        as_of_date=d,
        data_source=source,
        pipeline=DailyPipeline(app_config=app_cfg, calendar=cal, universe_kwargs=uk),
        state_store=store,
        trigger=trigger,
    )
    rep_dir = config.reports_dir / "daily" / d.isoformat()
    du = rep_dir / "data-update.json"
    if write_data_update:
        payload = {"as_of": d.isoformat(), "row_count": 10, "symbols_succeeded": 1}
        if online is not None:
            payload["online"] = online
        if synthetic is not None:
            payload["synthetic"] = synthetic
        du.write_text(json.dumps(payload), encoding="utf-8")
    elif du.exists():
        du.unlink()
    # 真实调用 tracker（calendar 注入合成日历，避免文件日历依赖）
    return g4b._track_real(config, calendar=cal)


def test_track_real_manual_success_not_counted(tmp_path):
    """manual + 真实在线证明：不计入（trigger 过滤），保持 0/60。"""
    s = _track_real_run(tmp_path, trigger="manual", online=True, synthetic=False, write_data_update=True)
    assert s["observation_progress"] == 0
    assert s["real_success_trading_days"] == 0


def test_track_real_scheduled_online_true_counts(tmp_path):
    """scheduled + online=true + synthetic=false：计入 1/60。"""
    s = _track_real_run(tmp_path, trigger="scheduled", online=True, synthetic=False, write_data_update=True)
    assert s["observation_progress"] == 1
    assert s["real_success_trading_days"] == 1
    assert s["violations"] == []


def test_track_real_scheduled_synthetic_rejected(tmp_path):
    """scheduled + synthetic=true：不计入，violations 明确合成原因。"""
    s = _track_real_run(tmp_path, trigger="scheduled", online=True, synthetic=True, write_data_update=True)
    assert s["observation_progress"] == 0
    assert any("合成数据不得计入" in v for v in s["violations"])


def test_track_real_scheduled_offline_rejected(tmp_path):
    """scheduled + online=false：不计入。"""
    s = _track_real_run(tmp_path, trigger="scheduled", online=False, synthetic=False, write_data_update=True)
    assert s["observation_progress"] == 0
    assert any("非在线数据" in v for v in s["violations"])


def test_track_real_missing_online_synthetic_fail_closed(tmp_path):
    """scheduled + 来源字段缺失：fail-closed 不计入，不得猜测。"""
    s = _track_real_run(tmp_path, trigger="scheduled", online=None, synthetic=None, write_data_update=True)
    assert s["observation_progress"] == 0
    assert any("online/synthetic 字段缺失" in v for v in s["violations"])


def test_track_real_missing_data_update_fail_closed(tmp_path):
    """scheduled + SUCCESS 但 data-update.json 缺失：fail-closed 不计入。"""
    s = _track_real_run(tmp_path, trigger="scheduled", online=True, synthetic=False, write_data_update=False)
    assert s["observation_progress"] == 0
    assert any("data-update.json 不存在" in v for v in s["violations"])
