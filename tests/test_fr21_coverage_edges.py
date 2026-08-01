"""FR-21 覆盖率冲刺（四）：对易覆盖的边界分支做定向补测（离线、无副作用）。

覆盖目标模块：logging_setup / locking / models / config / simulated_account /
archive / alerts 的未覆盖分支，用于把 ``ashare_quant.automation`` 与总体
覆盖率推过 90% 双门槛。所有用例均离线、不连接券商、不降低任何验收标准。
"""
from __future__ import annotations

import io
import json
import os
import zipfile
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ashare_quant.automation.config import (
    AccountConfig,
    AlertsConfig,
    AutomationConfig,
    LiveTradingConfig,
    LoggingConfig,
    LockConfig,
    QualityGateConfig,
    SchedulerConfig,
    StrategyTrack,
    load_automation_config,
    parse_hhmm,
)
from ashare_quant.automation.logging_setup import AutomationLogger, REDACTED, redact_value
from ashare_quant.automation.locking import LockInfo, RunLock, process_alive
from ashare_quant.automation.models import (
    EligibilityStatus,
    RunRecord,
    SimulatedAccountState,
    SimulatedOrderRecord,
    SimulatedPosition,
    StepResult,
    TaskType,
    _dec_str,
    _opt_dec,
    _str_to_d,
    _str_to_dt,
)
from ashare_quant.automation.simulated_account import (
    ORDER_STATUS_DUPLICATE,
    ORDER_STATUS_FILLED,
    AccountUpdateResult,
    SimulatedAccountManager,
    _assert_account_integrity,
    assert_simulation_only,
)
from ashare_quant.backtest.config import BacktestConfig


# =========================================================================== #
# logging_setup
# =========================================================================== #


class TestLoggingSetupEdges:
    def test_redact_max_depth(self) -> None:
        d: dict = {}
        cur = d
        for _ in range(15):
            nxt: dict = {}
            cur["x"] = nxt
            cur = nxt
        result = redact_value(d, ["k"])
        # 递归超过 12 层后最深层被截断为哨兵值（logging_setup 80-88）
        assert "<max-depth>" in json.dumps(result)

    def test_redact_datetime_and_date(self) -> None:
        assert redact_value(date(2020, 1, 1), []) == "2020-01-01"
        assert redact_value(datetime(2020, 1, 1, 0, 0, 0), []) == "2020-01-01T00:00:00"

    def test_redact_unknown_type(self) -> None:
        class _Weird:
            def __str__(self) -> str:
                return "weird"

        assert redact_value(_Weird(), []) == "weird"

    def test_bind_and_records(self) -> None:
        log = AutomationLogger(level="DEBUG")
        log.bind(run_id="r1", task_type=TaskType.DAILY, as_of_date=date(2020, 1, 1))
        assert log.run_id == "r1"
        assert log.records == []

    def test_emit_below_threshold_returns_none(self) -> None:
        log = AutomationLogger(level="INFO")
        assert log.debug("evt", "msg") is None  # DEBUG(10) < INFO(20)

    def test_emit_to_stream(self) -> None:
        buf = io.StringIO()
        log = AutomationLogger(level="DEBUG", console=False, stream=buf)
        log.info("evt", "hello")
        assert "hello" in buf.getvalue()

    def test_emit_to_console(self, capsys) -> None:
        log = AutomationLogger(level="INFO", console=True)
        log.info("evt", "console-line")  # -> stdout
        log.debug("evt", "skipped-line")  # 低于阈值，不输出
        log.critical("evt", "crit-line")  # >=40 -> stderr
        out = capsys.readouterr()
        assert "console-line" in out.out
        assert "crit-line" in out.err


# =========================================================================== #
# models
# =========================================================================== #


class TestModelsEdges:
    def test_str_to_dt_accepts_datetime(self) -> None:
        now = datetime(2020, 1, 1, 12, 0, 0)
        assert _str_to_dt(now) is now

    def test_str_to_d_accepts_datetime_and_date(self) -> None:
        now = datetime(2020, 1, 1, 12, 0, 0)
        assert _str_to_d(now) == date(2020, 1, 1)
        assert _str_to_d(date(2021, 2, 2)) == date(2021, 2, 2)

    def test_run_record_add_step_overwrite(self) -> None:
        rec = RunRecord(run_id="r", task_type=TaskType.DAILY, as_of_date=date(2020, 1, 1))
        s1 = StepResult(name="x")
        rec.add_step(s1)
        s2 = StepResult(name="x")
        rec.add_step(s2)  # 同名覆盖（376-377）
        assert rec.step("x") is s2
        assert rec.step("missing") is None  # 386

    def test_order_record_from_dict_requires_signal_date(self) -> None:
        with pytest.raises(ValueError):
            SimulatedOrderRecord.from_dict(
                {
                    "signal_date": None,
                    "account_id": "a",
                    "strategy_track": "steady",
                    "symbol": "s",
                    "side": "BUY",
                    "quantity": 1,
                    "signal_hash": "h",
                }
            )

    def test_position_value_skips(self) -> None:
        st = SimulatedAccountState(
            account_id="a",
            strategy_track=StrategyTrack.STEADY,
            eligibility_status=EligibilityStatus.NOT_ELIGIBLE_FOR_LIVE_TRADING,
            positions={"s": SimulatedPosition(symbol="s", total_quantity=10)},
        )
        assert st.position_value({"s": None}) == Decimal("0")
        empty = SimulatedAccountState(
            account_id="b",
            strategy_track=StrategyTrack.STEADY,
            eligibility_status=EligibilityStatus.NOT_ELIGIBLE_FOR_LIVE_TRADING,
            positions={"s": SimulatedPosition(symbol="s", total_quantity=0)},
        )
        assert empty.position_value({"s": Decimal("10")}) == Decimal("0")

    def test_dec_helpers(self) -> None:
        assert _dec_str(None) is None
        assert _opt_dec(None) is None
        assert _opt_dec("") is None
        assert _opt_dec("1.5") == Decimal("1.5")


# =========================================================================== #
# config
# =========================================================================== #


class TestConfigEdges:
    def test_parse_hhmm_bad(self) -> None:
        with pytest.raises(ValueError):
            parse_hhmm("99:99")

    def test_data_ready_time_parsed(self) -> None:
        from ashare_quant.automation.config import DataConfig

        assert DataConfig().ready_time_parsed.hour == 18

    def test_quality_gate_validators(self) -> None:
        with pytest.raises(ValueError):
            QualityGateConfig(block_on_critical=False)
        with pytest.raises(ValueError):
            QualityGateConfig(allow_stale_fallback=True)

    def test_lock_no_steal(self) -> None:
        with pytest.raises(ValueError):
            LockConfig(allow_steal_active=True)

    def test_logging_level_validator(self) -> None:
        with pytest.raises(ValueError):
            LoggingConfig(level="BAD")

    def test_alerts_no_inline_url(self) -> None:
        with pytest.raises(ValueError):
            AlertsConfig(webhook_env_var="http://evil.com/x")

    def test_scheduler_validators(self) -> None:
        with pytest.raises(ValueError):
            SchedulerConfig(weekly_day="FOO")
        with pytest.raises(ValueError):
            SchedulerConfig(run_level="X")

    def test_live_trading_no_broker(self) -> None:
        with pytest.raises(ValueError):
            LiveTradingConfig(broker_endpoint="http://broker")

    def test_duplicate_account_ids_rejected(self) -> None:
        acc = AccountConfig(
            account_id="dup",
            track=StrategyTrack.STEADY,
            eligibility_status=EligibilityStatus.NOT_ELIGIBLE_FOR_LIVE_TRADING,
        )
        with pytest.raises(ValueError):
            AutomationConfig(accounts=[acc, acc])

    def test_account_lookup_and_track(self) -> None:
        cfg = load_automation_config()
        assert cfg.account("paper-steady").account_id == "paper-steady"
        with pytest.raises(KeyError):
            cfg.account("does-not-exist")
        assert cfg.account_for_track(StrategyTrack.STEADY) is not None
        # 仅含稳健轨时，激进轨查询返回 None（406-409）
        cfg2 = AutomationConfig(
            accounts=[
                AccountConfig(
                    account_id="only-steady",
                    track=StrategyTrack.STEADY,
                    eligibility_status=EligibilityStatus.NOT_ELIGIBLE_FOR_LIVE_TRADING,
                )
            ]
        )
        assert cfg2.account_for_track(StrategyTrack.AGGRESSIVE) is None

    def test_load_missing_file(self) -> None:
        with pytest.raises(FileNotFoundError):
            load_automation_config("no-such-config.yaml")


# =========================================================================== #
# locking
# =========================================================================== #


class TestLockingEdges:
    def test_process_alive_bad_pid(self) -> None:
        assert process_alive(0) is False
        assert process_alive(-5) is False

    def test_process_alive_posix_branch(self, monkeypatch) -> None:
        from ashare_quant.automation import locking

        def fake_kill(pid: int, _sig: int) -> None:
            if pid == 999999:
                raise ProcessLookupError()
            return None

        monkeypatch.setattr(locking.os, "name", "posix")
        monkeypatch.setattr(locking.os, "kill", fake_kill)
        assert process_alive(999999) is False
        assert process_alive(os.getpid()) is True

    def test_is_stale_branches(self, tmp_path) -> None:
        from ashare_quant.automation.locking import LockInfo, RunLock

        base = datetime(2020, 1, 1, 0, 0, 0)

        def make(hostname: str, *, now=None, alive=None, run_id: str = ""):
            return RunLock(
                tmp_path / "l.lock",
                task_type=TaskType.DAILY,
                as_of_date=date(2020, 1, 1),
                stale_after_seconds=100,
                now_fn=now or (lambda: base),
                alive_fn=alive or (lambda pid: True),
                hostname=hostname,
                run_id=run_id,
            )

        info = LockInfo(
            pid=12345,
            hostname="HOSTA",
            task_type=TaskType.DAILY,
            as_of_date=date(2020, 1, 1),
            started_at=base,
        )
        # 同主机 + 存活 -> 非陈旧
        assert make("HOSTA", alive=lambda p: True).is_stale(info)[0] is False
        # 同主机 + 进程消失 -> 陈旧
        assert make("HOSTA", alive=lambda p: False).is_stale(info)[0] is True
        # 同主机孤儿锁（pid==自身 且 run_id 不同）
        orphan = LockInfo(
            pid=os.getpid(),
            hostname="HOSTA",
            task_type=TaskType.DAILY,
            as_of_date=date(2020, 1, 1),
            started_at=base,
            run_id="other",
        )
        assert make("HOSTA", run_id="self").is_stale(orphan)[0] is True
        # 跨主机 + 过期 -> 陈旧
        cross = LockInfo(
            pid=999,
            hostname="OTHERHOST",
            task_type=TaskType.DAILY,
            as_of_date=date(2020, 1, 1),
            started_at=base,
        )
        assert (
            make("HOSTA", now=lambda: base + timedelta(seconds=200)).is_stale(cross)[0]
            is True
        )
        # 跨主机 + 未过期 -> 非陈旧
        assert (
            make("HOSTA", now=lambda: base + timedelta(seconds=10)).is_stale(cross)[0]
            is False
        )

    def test_release_corrupt_but_existing(self, tmp_path) -> None:
        from ashare_quant.automation.locking import LockInfo, RunLock

        base = datetime(2020, 1, 1, 0, 0, 0)
        info = LockInfo(
            pid=os.getpid(),
            hostname="HOSTA",
            task_type=TaskType.DAILY,
            as_of_date=date(2020, 1, 1),
            started_at=base,
        )
        lock = RunLock(
            tmp_path / "l.lock",
            task_type=TaskType.DAILY,
            as_of_date=date(2020, 1, 1),
            hostname="HOSTA",
        )
        lock._held = True
        lock._info = info
        # 写入损坏内容：read_lock 返回 None，但文件仍存在 -> 走 _unlink_confirmed
        lock.lock_path.write_text("{bad", encoding="utf-8")
        assert lock.release() is True
        assert lock.info is info  # info property（204）

    def test_release_already_released(self, tmp_path) -> None:
        from ashare_quant.automation.locking import LockInfo, RunLock

        lock = RunLock(
            tmp_path / "l.lock",
            task_type=TaskType.DAILY,
            as_of_date=date(2020, 1, 1),
        )
        assert lock.release() is False  # 未持有 -> False


# =========================================================================== #
# simulated_account
# =========================================================================== #


def _bar(symbol: str, d: date, price: Decimal) -> object:
    from ashare_quant.backtest.models import BarData

    return BarData(
        symbol=symbol,
        trade_date=d,
        open_raw=price,
        high_raw=price,
        low_raw=price,
        close_raw=price,
        open_qfq=price,
        high_qfq=price,
        low_qfq=price,
        close_qfq=price,
    )


class TestSimulatedAccountEdges:
    def test_assert_simulation_only_rejects(self) -> None:
        class _Live:
            enabled = True
            broker_endpoint = ""

        class _Cfg:
            live_trading = _Live()

        with pytest.raises(Exception):
            assert_simulation_only(_Cfg())

        _Live.enabled = False
        _Live.broker_endpoint = "http://broker"
        with pytest.raises(Exception):
            assert_simulation_only(_Cfg())

    def test_integrity_rejects_wrong_status(self) -> None:
        st = SimulatedAccountState(
            account_id="a",
            strategy_track=StrategyTrack.STEADY,
            eligibility_status=EligibilityStatus.SIMULATION_ONLY,  # 稳健轨应为 NOT_ELIGIBLE
        )
        with pytest.raises(Exception):
            _assert_account_integrity(st)

    def test_ensure_account_track_mismatch(self) -> None:
        cfg = load_automation_config()
        mgr = SimulatedAccountManager(cfg, BacktestConfig())
        existing = mgr.create_account(cfg.accounts[0])
        wrong = AccountConfig(
            account_id="other",
            track=StrategyTrack.AGGRESSIVE,
            eligibility_status=EligibilityStatus.SIMULATION_ONLY,
        )
        with pytest.raises(Exception):
            mgr.ensure_account(wrong, existing=existing)

    def test_rollover_noop(self) -> None:
        st = SimulatedAccountState(
            account_id="a",
            strategy_track=StrategyTrack.STEADY,
            eligibility_status=EligibilityStatus.NOT_ELIGIBLE_FOR_LIVE_TRADING,
            as_of_date=date(2020, 1, 5),
        )
        assert SimulatedAccountManager.rollover(st, date(2020, 1, 5)) is False
        assert SimulatedAccountManager.rollover(st, date(2020, 1, 4)) is False

    def test_apply_signals_buy_sell_and_duplicate(self) -> None:
        from ashare_quant.backtest.config import BacktestConfig
        from ashare_quant.backtest.models import Side, Signal

        cfg = load_automation_config()
        mgr = SimulatedAccountManager(cfg, BacktestConfig())
        state = mgr.create_account(cfg.accounts[0])
        sym = "600000"
        price = Decimal("1.0")  # 100×1=100，初始现金充足且为手数 100 的整数倍
        d1 = date(2020, 1, 3)
        d2 = date(2020, 1, 6)
        b1 = _bar(sym, d1, price)
        b2 = _bar(sym, d2, price)

        buy = Signal(signal_date=d1, symbol=sym, side=Side.BUY, quantity=100, reason="t")
        r1 = mgr.apply_signals(state, signals=[buy], bars={sym: b1}, fill_date=d1, run_id="buy")
        assert any(o.status == ORDER_STATUS_FILLED for o in r1.orders)

        sell = Signal(signal_date=d2, symbol=sym, side=Side.SELL, quantity=100, reason="t")
        r2 = mgr.apply_signals(state, signals=[sell], bars={sym: b2}, fill_date=d2, run_id="sell")
        assert any(o.status == ORDER_STATUS_FILLED for o in r2.orders)

        # 重复同一买入信号 -> 幂等去重
        r3 = mgr.apply_signals(state, signals=[buy], bars={sym: b2}, fill_date=d2, run_id="buy2")
        assert r3.orders[0].status == ORDER_STATUS_DUPLICATE

        # counts / to_dict 覆盖
        assert r3.counts()["skipped_duplicate"] >= 1
        assert "account_id" in r3.to_dict()


# =========================================================================== #
# archive
# =========================================================================== #


def _arch_cfg(base: Path, *, enabled: bool = True, retain_days: int = 30, max_batches: int = 2):  # type: ignore[no-untyped-def]
    class _A:
        pass

    a = _A()
    a.enabled = enabled
    a.retain_days = retain_days
    a.max_batches = max_batches

    class _C:
        base_dir = base
        archive_dir = base / "reports" / "phase-4" / "archive"
        reports_dir = base / "reports" / "phase-4"
        archive = a

    return _C()


def _mk_tree(root: Path, name: str, files: dict[str, str]) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    for fn, content in files.items():
        p = d / fn
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return d


class TestArchiveEdges:
    def test_assert_inside_root_equal_raises(self, tmp_path) -> None:
        from ashare_quant.automation.archive import safe_remove_tree

        root = tmp_path / "root"
        root.mkdir()
        with pytest.raises(ValueError):
            safe_remove_tree(root, root=root)  # 拒绝删除归档根自身

    def test_archive_skips_unparseable_dir(self, tmp_path) -> None:
        from ashare_quant.automation.archive import archive_results

        cfg = _arch_cfg(tmp_path)
        reports = cfg.reports_dir
        (reports / "daily").mkdir(parents=True)
        _mk_tree(reports / "daily", "garbage-name", {"r.json": "x"})
        res = archive_results(cfg, as_of_date=date(2020, 7, 1))
        assert any("garbage-name" in s for s in res.skipped)

    def test_pruning_dry_run_counts_files(self, tmp_path) -> None:
        from ashare_quant.automation.archive import archive_results

        cfg = _arch_cfg(tmp_path, retain_days=30, max_batches=2)
        arch = cfg.archive_dir
        for b in ("2020-W01", "2020-W02", "2020-W03"):
            _mk_tree(arch, b, {"f.txt": "x"})
        reports = cfg.reports_dir
        (reports / "daily").mkdir(parents=True, exist_ok=True)
        _mk_tree(reports / "daily", "2020-05-01", {"r.json": "old"})
        res = archive_results(cfg, as_of_date=date(2020, 7, 1), dry_run=True)
        assert res.removed_files > 0  # dry-run 仍统计被清理的文件数


# =========================================================================== #
# alerts
# =========================================================================== #


class _AlertsCfg2:
    enabled = True
    failure_marker_filename = "LAST_FAILURE.marker"
    latest_failure_md = "latest-failure.md"
    latest_failure_json = "latest-failure.json"
    windows_event_log = False
    toast = False
    webhook_env_var = ""
    webhook_timeout_seconds = 5
    event_log_source = "AShareQuant"


class _LoggingCfg2:
    redact_keys = {"token", "password"}


class _AlertConfig2:
    def __init__(self, base: Path) -> None:
        self.state_dir = base / "state"
        self.reports_dir = base / "reports"
        self.alerts = _AlertsCfg2()
        self.logging = _LoggingCfg2()


class _FakeLogger2:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def info(self, event: str, message: str, **kw: object) -> None:
        self.calls.append(("info", event))

    def warning(self, event: str, message: str, **kw: object) -> None:
        self.calls.append(("warning", event))


def _rec(state):  # type: ignore[no-untyped-def]
    from ashare_quant.automation.models import RunRecord, RunState, TaskType

    return RunRecord(
        run_id="r1",
        task_type=TaskType.DAILY,
        as_of_date=date(2020, 1, 2),
        state=state,
        message="boom",
    )


class TestAlertEdges:
    def test_webhook_success(self, tmp_path) -> None:
        from ashare_quant.automation.alerts import AlertManager

        cfg = _AlertConfig2(tmp_path)
        cfg.alerts.webhook_env_var = "MY_WEBHOOK"
        fake = MagicMock()
        fake.status = 200
        fake.__enter__.return_value = fake
        with patch(
            "urllib.request.urlopen", return_value=fake
        ):
            mgr = AlertManager(cfg, env={"MY_WEBHOOK": "http://127.0.0.1/x"})
            res = mgr.handle(_rec(RunState_value("FAILED")))
        assert "webhook" in res["channels"]

    def test_webhook_non_2xx(self, tmp_path) -> None:
        from ashare_quant.automation.alerts import AlertManager

        cfg = _AlertConfig2(tmp_path)
        cfg.alerts.webhook_env_var = "MY_WEBHOOK"
        fake = MagicMock()
        fake.status = 500
        fake.__enter__.return_value = fake
        with patch(
            "urllib.request.urlopen", return_value=fake
        ):
            mgr = AlertManager(cfg, env={"MY_WEBHOOK": "http://127.0.0.1/x"})
            res = mgr.handle(_rec(RunState_value("FAILED")))
        assert "webhook" not in res["channels"]

    def test_toast_exception_logged(self, tmp_path) -> None:
        from ashare_quant.automation.alerts import AlertManager, RunState

        cfg = _AlertConfig2(tmp_path)
        cfg.alerts.toast = True
        logger = _FakeLogger2()
        with patch(
            "ashare_quant.automation.alerts.subprocess.run",
            side_effect=RuntimeError("boom"),
        ):
            mgr = AlertManager(cfg, logger=logger)
            res = mgr.handle(_rec(RunState.FAILED))
        assert "toast" not in res["channels"]
        assert ("warning", "alert_toast_failed") in logger.calls


def RunState_value(name: str):  # type: ignore[no-untyped-def]
    from ashare_quant.automation.models import RunState

    return RunState[name]
