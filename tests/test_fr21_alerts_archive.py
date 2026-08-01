"""FR-21 覆盖率冲刺（三）：``alerts`` 与 ``archive`` 模块补测（离线）。"""
from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from ashare_quant.automation.alerts import AlertManager, build_failure_markdown
from ashare_quant.automation.archive import (
    ArchivedItem,
    ArchiveResult,
    archive_results,
    directory_stats,
    move_directory,
    safe_remove_tree,
)
from ashare_quant.automation.models import RunRecord, RunState, StepResult, StepStatus, TaskType


# ---- fake config 工厂 --------------------------------------------------- #


class _AlertsCfg:
    enabled = True
    failure_marker_filename = "LAST_FAILURE.marker"
    latest_failure_md = "latest-failure.md"
    latest_failure_json = "latest-failure.json"
    windows_event_log = False
    toast = False
    webhook_env_var = ""
    webhook_timeout_seconds = 5
    event_log_source = "AShareQuant"


class _LoggingCfg:
    redact_keys = {"token", "password"}


class _AlertConfig:
    def __init__(self, base: Path) -> None:
        self.state_dir = base / "state"
        self.reports_dir = base / "reports"
        self.alerts = _AlertsCfg()
        self.logging = _LoggingCfg()


class _FakeLogger:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def info(self, event: str, message: str, **kw: object) -> None:
        self.calls.append(("info", event))

    def warning(self, event: str, message: str, **kw: object) -> None:
        self.calls.append(("warning", event))


def _rec(state: RunState, *, steps=(), message: str = "") -> RunRecord:
    return RunRecord(
        run_id="r1", task_type=TaskType.DAILY, as_of_date=date(2020, 1, 2),
        state=state, message=message,
        steps=list(steps),
    )


# =========================================================================== #
# alerts
# =========================================================================== #


class TestBuildFailureMarkdown:
    def test_basic(self) -> None:
        md = build_failure_markdown(_rec(RunState.FAILED, message="boom"))
        assert "FAILED" in md
        assert "boom" in md
        assert "建议动作" in md

    def test_with_failed_steps_and_extra(self) -> None:
        steps = [StepResult(name="x", status=StepStatus.FAILED, error="oops|err")]
        rec = _rec(RunState.BLOCKED_LOCKED, steps=steps, message="locked")
        md = build_failure_markdown(rec, extra={"token": "secret"})
        assert "oops\\|err" in md  # 管道符转义
        assert "secret" in md

    def test_no_failed_steps(self) -> None:
        md = build_failure_markdown(_rec(RunState.SKIPPED_DATA_UNAVAILABLE))
        assert "无显式失败步骤" in md
        assert "建议动作" in md


class TestAlertManager:
    def test_success_clears_marker(self, tmp_path) -> None:
        cfg = _AlertConfig(tmp_path)
        mgr = AlertManager(cfg)
        # 先造一个标记
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        (mgr.marker_path).write_text("{}", encoding="utf-8")
        res = mgr.handle(_rec(RunState.SUCCESS))
        assert res["alerted"] is False
        assert res["cleared_marker"] is True
        assert not mgr.has_pending_failure()

    def test_non_trading_day_no_alert(self, tmp_path) -> None:
        mgr = AlertManager(_AlertConfig(tmp_path))
        res = mgr.handle(_rec(RunState.SKIPPED_NON_TRADING_DAY))
        assert res["alerted"] is False
        assert res["reason"] == "non_trading_day"

    def test_failure_raises_alert(self, tmp_path) -> None:
        cfg = _AlertConfig(tmp_path)
        logger = _FakeLogger()
        mgr = AlertManager(cfg, logger=logger)
        res = mgr.handle(_rec(RunState.FAILED, message="down"))
        assert res["alerted"] is True
        assert "marker_file" in res["channels"]
        assert "failure_report" in res["channels"]
        assert mgr.has_pending_failure()
        marker = mgr.read_marker()
        assert marker["state"] == "FAILED"
        assert cfg.state_dir.joinpath("LAST_FAILURE.marker").exists()
        assert cfg.reports_dir.joinpath("latest-failure.md").exists()
        assert logger.calls  # _log_warn / _log_info 都覆盖

    def test_alerts_disabled(self, tmp_path) -> None:
        cfg = _AlertConfig(tmp_path)
        cfg.alerts.enabled = False
        mgr = AlertManager(cfg)
        res = mgr.handle(_rec(RunState.FAILED))
        assert res["alerted"] is False
        assert res["reason"] == "alerts_disabled"

    def test_clear_no_marker(self, tmp_path) -> None:
        mgr = AlertManager(_AlertConfig(tmp_path))
        assert mgr.clear() is False
        assert mgr.read_marker() is None

    def test_read_marker_corrupt(self, tmp_path) -> None:
        cfg = _AlertConfig(tmp_path)
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        (cfg.state_dir / cfg.alerts.failure_marker_filename).write_text(
            "{bad", encoding="utf-8")
        assert AlertManager(cfg).read_marker() is None

    def test_event_log_and_toast_degraded_on_failure(self, tmp_path) -> None:
        # 本机为 Windows（os.name == "nt"），通道会真正尝试调用
        # eventcreate / powershell。这里桩掉 subprocess，模拟外部工具失败，
        # 验证告警主流程不崩溃、且失败通道不计入 channels。
        cfg = _AlertConfig(tmp_path)
        cfg.alerts.windows_event_log = True
        cfg.alerts.toast = True
        mgr = AlertManager(cfg)
        fake = subprocess.CompletedProcess(args=["x"], returncode=1, stdout="", stderr="")
        with patch("ashare_quant.automation.alerts.subprocess.run", return_value=fake):
            res = mgr.handle(_rec(RunState.FAILED))
        assert res["alerted"] is True
        assert "windows_event_log" not in res["channels"]
        assert "toast" not in res["channels"]

    def test_event_log_and_toast_success(self, tmp_path) -> None:
        # 外部工具调用成功（returncode==0）时，两个可选通道应被记入 channels。
        cfg = _AlertConfig(tmp_path)
        cfg.alerts.windows_event_log = True
        cfg.alerts.toast = True
        mgr = AlertManager(cfg)
        fake = subprocess.CompletedProcess(args=["x"], returncode=0, stdout="", stderr="")
        with patch("ashare_quant.automation.alerts.subprocess.run", return_value=fake):
            res = mgr.handle(_rec(RunState.FAILED))
        assert "windows_event_log" in res["channels"]
        assert "toast" in res["channels"]

    def test_webhook_from_env(self, tmp_path) -> None:
        cfg = _AlertConfig(tmp_path)
        cfg.alerts.webhook_env_var = "MY_WEBHOOK"
        mgr = AlertManager(cfg, env={"MY_WEBHOOK": "http://127.0.0.1:9/nope"})
        res = mgr.handle(_rec(RunState.FAILED))
        # 端口 9 必然连接失败 -> 通道记为 webhook_failed，主流程不崩溃
        assert "webhook_failed" in res["channels"]


# =========================================================================== #
# archive
# =========================================================================== #


def _tree(root: Path, name: str, files: dict[str, str]) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    for fn, content in files.items():
        p = d / fn
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return d


class TestArchiveHelpers:
    def test_directory_stats(self, tmp_path) -> None:
        d = _tree(tmp_path / "d", "x", {"a.txt": "hi", "b.txt": "hello"})
        files, size = directory_stats(d)
        assert files == 2
        assert size == len("hi") + len("hello")
        assert directory_stats(tmp_path / "missing") == (0, 0)

    def test_assert_inside_blocks_escape(self, tmp_path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        with pytest.raises(ValueError):
            safe_remove_tree(outside, root=root)

    def test_safe_remove_tree(self, tmp_path) -> None:
        root = tmp_path / "root"
        tree = _tree(root, "sub", {"a.txt": "x", "nested/b.txt": "yy"})
        removed = safe_remove_tree(tree, root=root)
        assert removed == 2
        assert not tree.exists()
        # 不存在的目标返回 0
        assert safe_remove_tree(root / "gone", root=root) == 0

    def test_move_directory(self, tmp_path) -> None:
        src = _tree(tmp_path, "src", {"f.txt": "data"})
        dst = tmp_path / "dst"
        move_directory(src, dst)
        assert dst.joinpath("f.txt").read_text(encoding="utf-8") == "data"
        assert not src.exists()
        # 目标已存在 -> 先清旧的再搬
        src2 = _tree(tmp_path, "src2", {"g.txt": "more"})
        move_directory(src2, dst)  # dst 已存在
        assert dst.joinpath("g.txt").exists()
        assert not dst.joinpath("f.txt").exists()

    def test_parsers(self) -> None:
        from ashare_quant.automation.archive import (
            _parse_daily_bucket,
            _parse_weekly_bucket,
        )
        assert _parse_daily_bucket("2020-01-02") == date(2020, 1, 2)
        assert _parse_daily_bucket("not-a-date") is None
        assert _parse_weekly_bucket("2020-W02") == date.fromisocalendar(2020, 2, 7)
        assert _parse_weekly_bucket("junk") is None

    def test_archived_item_and_result(self, tmp_path) -> None:
        item = ArchivedItem(kind="daily", bucket="2020-01-02", source="s", target="t",
                            files=3, bytes=99)
        d = item.to_dict()
        assert d["files"] == 3
        res = ArchiveResult(enabled=True, retain_days=10, max_batches=2)
        res.archived.append(item)
        res.pruned_batches.append("old")
        res.removed_files = 5
        assert res.archived_count == 1
        assert res.archived_bytes == 99
        assert res.to_dict()["archived_count"] == 1
        assert "归档开关" in res.summary()


class TestArchiveResults:
    def _cfg(self, base: Path, *, enabled: bool = True, retain_days: int = 30,
             max_batches: int = 2) -> object:
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

    def test_disabled(self, tmp_path) -> None:
        cfg = self._cfg(tmp_path, enabled=False)
        res = archive_results(cfg, as_of_date=date(2020, 7, 1))  # type: ignore[arg-type]
        assert res.enabled is False
        assert "跳过归档" in res.reason

    def test_dry_run_and_archive(self, tmp_path) -> None:
        cfg = self._cfg(tmp_path, retain_days=30, max_batches=2)
        reports = cfg.reports_dir  # type: ignore[attr-defined]
        (reports / "daily").mkdir(parents=True)
        _tree(reports / "daily", "2020-05-01", {"r.json": "old"})
        _tree(reports / "daily", "2020-06-15", {"r.json": "recent"})
        # 当前业务日目录受保护
        _tree(reports / "daily", "2020-07-01", {"r.json": "current"})

        # dry-run：不落地
        res = archive_results(cfg, as_of_date=date(2020, 7, 1), dry_run=True)  # type: ignore[arg-type]
        assert res.dry_run is True
        assert res.archived_count == 1  # 仅 2020-05-01 过期
        assert not (cfg.archive_dir / res.batch / "daily" / "2020-05-01").exists()  # type: ignore[attr-defined]

        # 真实运行：会落地
        res2 = archive_results(cfg, as_of_date=date(2020, 7, 1))  # type: ignore[arg-type]
        assert (cfg.archive_dir / res2.batch / "daily" / "2020-05-01").exists()  # type: ignore[attr-defined]
        assert "2020-07-01" not in [a.bucket for a in res2.archived]

    def test_pruning(self, tmp_path) -> None:
        cfg = self._cfg(tmp_path, retain_days=30, max_batches=2)
        arch = cfg.archive_dir  # type: ignore[attr-defined]
        # 预置 3 个批次目录（超过 max_batches=2）
        for b in ("2020-W01", "2020-W02", "2020-W03"):
            _tree(arch, b, {"f.txt": "x"})
        reports = cfg.reports_dir  # type: ignore[attr-defined]
        (reports / "daily").mkdir(parents=True)
        _tree(reports / "daily", "2020-05-01", {"r.json": "old"})

        res = archive_results(cfg, as_of_date=date(2020, 7, 1))  # type: ignore[arg-type]
        assert res.pruned_batches  # 清理了最老批次
        # 保留数不超过 max_batches（含新增批次）
        remaining = sorted(p.name for p in arch.iterdir() if p.is_dir())
        assert len(remaining) <= 2 + 1
