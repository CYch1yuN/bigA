"""FR-21 覆盖率冲刺（五）：对 automation/cli.py 的高速/确定性入口做离线补测。

仅覆盖无需真实管线运行的表面（register 解析器装配、_as_of/_repo_root、
cmd_verify 安全边界、cmd_install/uninstall --dry-run）。不触达 cmd_daily/weekly/rerun
的真实/合成管线，也不连接任何券商，不降低任何验收门槛。
"""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import pytest

from ashare_quant.automation import cli as cli_mod
from ashare_quant.automation.calendar import CalendarUnavailableError
from ashare_quant.automation.cli import (
    _as_of,
    _repo_root,
    cmd_install,
    cmd_uninstall,
    cmd_verify,
    register,
)


class _Args:
    """简易命名空间，按需附加属性。"""

    config = None

    def __init__(self, **kw: object) -> None:
        for k, v in kw.items():
            setattr(self, k, v)


# =========================================================================== #
# 解析器装配
# =========================================================================== #


class TestRegister:
    def _build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        register(sub)
        return parser

    def test_register_marks_all_subcommands(self) -> None:
        parser = self._build_parser()
        expected = {
            "daily": cli_mod.cmd_daily,
            "weekly": cli_mod.cmd_weekly,
            "status": cli_mod.cmd_status,
            "verify": cli_mod.cmd_verify,
            "rerun": cli_mod.cmd_rerun,
            "install": cli_mod.cmd_install,
            "uninstall": cli_mod.cmd_uninstall,
        }
        for name, fn in expected.items():
            ns = parser.parse_args(["automation", name])
            assert ns.func is fn

    def test_register_parses_flags(self) -> None:
        parser = self._build_parser()
        ns = parser.parse_args(["automation", "daily", "--synthetic", "--dry-run"])
        assert ns.func is cli_mod.cmd_daily
        assert ns.synthetic is True
        assert ns.dry_run is True

        ns = parser.parse_args(
            ["automation", "install", "--dry-run", "--yes", "--force", "--task-prefix", "X"]
        )
        assert ns.func is cli_mod.cmd_install
        assert ns.dry_run is True and ns.yes is True and ns.force is True
        assert ns.task_prefix == "X"

        ns = parser.parse_args(["automation", "rerun", "--task", "weekly"])
        assert ns.func is cli_mod.cmd_rerun
        assert ns.task == "weekly"


# =========================================================================== #
# 路径/日期辅助
# =========================================================================== #


class TestPathHelpers:
    def test_as_of(self) -> None:
        assert _as_of(_Args()) is None
        a = _Args(date="2020-01-02")
        assert _as_of(a) == date(2020, 1, 2)

    def test_repo_root(self) -> None:
        root = _repo_root()
        assert isinstance(root, Path)
        assert root.name == "大A受害者"


# =========================================================================== #
# verify 安全边界（monkeypatch 掉可能联网/慢的日历加载）
# =========================================================================== #


class TestVerify:
    def test_verify_non_synthetic(self, capsys, monkeypatch) -> None:
        class _FakeCal:
            first_date = date(2020, 1, 1)
            last_date = date(2020, 12, 31)

            def __len__(self) -> int:
                return 244

        monkeypatch.setattr(cli_mod, "load_trading_calendar", lambda *a, **k: _FakeCal())

        rc = cmd_verify(_Args())
        assert rc == 0
        out = capsys.readouterr().out
        assert "实盘开关已关闭" in out
        assert "交易日历" in out
        assert "simulation" in out.lower() or "模拟" in out

    def test_verify_uses_ascii_status_flags(self, capsys, monkeypatch) -> None:
        """verify 输出必须使用纯 ASCII 状态标志（[OK]/[WARN]/[FAIL]/[INFO]）。

        GBK 控制台无法编码 ✓/✗/⚠/• 等符号（历史缺陷：UnicodeEncodeError）。
        """
        class _FakeCal:
            first_date = date(2020, 1, 1)
            last_date = date(2020, 12, 31)

            def __len__(self) -> int:
                return 244

        monkeypatch.setattr(cli_mod, "load_trading_calendar", lambda *a, **k: _FakeCal())
        rc = cmd_verify(_Args())
        assert rc == 0
        out = capsys.readouterr().out
        for symbol in ("✓", "✗", "⚠", "•"):
            assert symbol not in out, f"verify 输出不得包含非 ASCII 符号 {symbol!r}"
        assert "[OK] 实盘开关已关闭" in out
        assert "[OK] 交易日历" in out

    def test_verify_fails_when_calendar_unavailable(self, capsys, monkeypatch) -> None:
        """日历缺失 ⇒ verify 必须非零退出（fail-closed），不能只给警告。"""
        def _boom(*a, **k):
            raise CalendarUnavailableError("交易日历文件不存在: test")

        monkeypatch.setattr(cli_mod, "load_trading_calendar", _boom)
        rc = cmd_verify(_Args())
        assert rc == 1, "日历不可用时 verify 必须返回非零退出码"
        out = capsys.readouterr().out
        assert "[FAIL] 交易日历不可用" in out

    def test_verify_gbk_console_no_crash(self, capsys, monkeypatch) -> None:
        """GBK 编码控制台（PYTHONIOENCODING=gbk）下 verify 不得崩溃。"""
        class _FakeCal:
            first_date = date(2020, 1, 1)
            last_date = date(2020, 12, 31)

            def __len__(self) -> int:
                return 244

        monkeypatch.setattr(cli_mod, "load_trading_calendar", lambda *a, **k: _FakeCal())
        # 模拟 GBK 控制台：所有 stdout 字符都必须能被 gbk 编码
        rc = cmd_verify(_Args())
        assert rc == 0
        out = capsys.readouterr().out
        out.encode("gbk")  # 若含 gbk 不可编码字符会抛 UnicodeEncodeError


# =========================================================================== #
# install / uninstall --dry-run（不真正调用 powershell）
# =========================================================================== #


class TestInstallUninstallDryRun:
    def test_install_dry_run(self, capsys) -> None:
        rc = cmd_install(_Args(dry_run=True, yes=False, force=False, task_prefix=None))
        assert rc == 0
        out = capsys.readouterr().out
        assert "将要执行" in out
        assert "未实际执行" in out

    def test_uninstall_dry_run(self, capsys) -> None:
        rc = cmd_uninstall(_Args(dry_run=True, yes=False, task_prefix=None))
        assert rc == 0
        out = capsys.readouterr().out
        assert "将要执行" in out
        assert "未实际执行" in out
