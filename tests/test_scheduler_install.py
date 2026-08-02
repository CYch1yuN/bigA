"""install_scheduler.ps1 实际执行路径回归测试。

PR #10 复审 FAIL 的两个阻断问题：
1. 安装器不检查 schtasks 退出码——Daily 失败、Weekly 成功仍继续，最后还打印
   「已注册任务」，静默产生部分安装；
2. 新测试验证的是 Python 侧 scheduler.py（本已正确），测不到 PowerShell 缺陷。

本测试通过 **假 schtasks 程序**（tests/fixtures/fake_schtasks.cmd）驱动
install_scheduler.ps1 的真实执行路径：
- 记录每次调用的完整参数；
- 可按子串模拟失败（FAKE_SCHTASKS_FAIL）；
- **禁止操作真实任务**（所有 schtasks 调用都被重定向到假程序）。

验证项（对齐复审要求）：
- Daily 创建命令不含 `/D`；Weekly 创建命令含 `/D SAT`；
- 预览（COMMAND:）与实际执行共用同一参数数组（日志即执行参数）；
- 创建返回非零 → 脚本整体非零退出，且**不输出「已注册任务」**；
- 部分安装会被报告（列出已注册任务），绝不打印整体成功；
- 注册后查询验证 Daily/Weekly 均存在。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install_scheduler.ps1"
FAKE = ROOT / "tests" / "fixtures" / "fake_schtasks.cmd"
PWSH = "powershell.exe"


def _run_install(
    tmp_path: Path,
    *,
    fail: str | None = None,
    force: bool = False,
    prefix: str = "AShareQuantAutomation",
) -> subprocess.CompletedProcess[str]:
    log = tmp_path / "schtasks.log"
    state = tmp_path / "fake_state"
    env = dict(os.environ)
    env["FAKE_SCHTASKS_LOG"] = str(log)
    env["FAKE_SCHTASKS_STATE"] = str(state)
    if fail:
        env["FAKE_SCHTASKS_FAIL"] = fail
    cmd = [
        PWSH,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(SCRIPT),
        "-SchtasksExe",
        str(FAKE),
        "-TaskPrefix",
        prefix,
    ]
    if force:
        cmd.append("-Force")
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env=env,
        timeout=180,
        encoding="utf-8",
        errors="replace",
    )
    proc.attrs_log = log  # type: ignore[attr-defined]
    return proc


def _log_lines(proc: subprocess.CompletedProcess[str]) -> list[str]:
    log = proc.attrs_log  # type: ignore[attr-defined]
    if not log.exists():
        return []
    return [ln.strip() for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _create_cmd(lines: list[str], name: str) -> str | None:
    """返回某任务名的 /Create 完整命令（含全部参数）。"""
    for ln in lines:
        if "/Create" in ln and name in ln:
            return ln
    return None


# ---------------------------------------------------------------------- #
# 参数正确性（共用参数数组）
# ---------------------------------------------------------------------- #

def test_daily_create_has_no_D_flag(tmp_path: Path) -> None:
    proc = _run_install(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = _log_lines(proc)
    daily = _create_cmd(lines, "AShareQuantAutomation-Daily")
    assert daily is not None
    assert "/D " not in daily, f"Daily 不得携带 /D: {daily}"
    assert "/SC DAILY" in daily


def test_weekly_create_has_D_SAT(tmp_path: Path) -> None:
    proc = _run_install(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = _log_lines(proc)
    weekly = _create_cmd(lines, "AShareQuantAutomation-Weekly")
    assert weekly is not None
    assert "/D SAT" in weekly, f"Weekly 必须携带 /D SAT: {weekly}"
    assert "/SC WEEKLY" in weekly


def test_create_and_query_use_same_parameter_array(tmp_path: Path) -> None:
    """预览打印与执行日志必须一致：参数只构建一次。"""
    proc = _run_install(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout
    lines = _log_lines(proc)
    daily = _create_cmd(lines, "AShareQuantAutomation-Daily")
    assert daily is not None
    # 预览 COMMAND: 行的关键参数序列必须与日志一致（/Create /TN /SC DAILY 无 /D）。
    # 引号在 cmd echo 与 PowerShell 打印间的转义细节不同，故按 token 序列比较。
    log_tokens = daily.split()
    assert log_tokens[0] == "/Create"
    assert "AShareQuantAutomation-Daily" in log_tokens
    assert "/SC" in log_tokens and "DAILY" in log_tokens
    assert "/D" not in log_tokens
    # 脚本打印的 COMMAND: 包含同样参数序列
    cmd_line = next(ln for ln in out.splitlines() if ln.startswith("COMMAND:") and "Daily" in ln)
    cmd_tokens = cmd_line.replace("COMMAND:", "").split()
    assert "/SC" in cmd_tokens and "DAILY" in cmd_tokens
    assert "/D" not in cmd_tokens
    # 注册后查询验证存在（含 /Query /TN）
    assert any("/Query /TN AShareQuantAutomation-Daily" in ln for ln in lines)
    assert any("/Query /TN AShareQuantAutomation-Weekly" in ln for ln in lines)


# ---------------------------------------------------------------------- #
# 失败处理：退出码检查 + 不打印整体成功 + 部分安装报告
# ---------------------------------------------------------------------- #

def test_create_failure_exits_nonzero_no_success(tmp_path: Path) -> None:
    """/Create 失败（假 schtasks 返回 1）→ 脚本整体非零，且不输出「已注册任务」。"""
    proc = _run_install(tmp_path, fail="/Create")
    assert proc.returncode != 0, "创建失败时安装脚本必须非零退出"
    assert "已注册任务" not in proc.stdout, "失败时不得打印整体成功"
    assert "安装失败" in proc.stdout or "schtasks 执行失败" in proc.stdout


def test_daily_failure_reports_partial_install(tmp_path: Path) -> None:
    """Daily 创建失败 → 脚本非零、不打印成功、报告安装失败。

    fail-fast 语义：Daily 失败后 Weekly 不再尝试（try 块中断），因此
    已注册任务列表为空，「部分安装」提示不出现，但「安装失败」必须出现。
    """
    proc = _run_install(tmp_path, fail="/SC DAILY")
    assert proc.returncode != 0
    assert "已注册任务" not in proc.stdout
    assert "安装失败" in proc.stdout
    assert "schtasks 执行失败" in proc.stdout


def test_weekly_failure_reports_partial_install(tmp_path: Path) -> None:
    """Weekly 创建失败（Daily 已成功）→ 报告部分安装并列出 Daily，整体非零。"""
    proc = _run_install(tmp_path, fail="/SC WEEKLY")
    assert proc.returncode != 0
    assert "已注册任务" not in proc.stdout
    assert "部分安装" in proc.stdout
    assert "AShareQuantAutomation-Daily" in proc.stdout


def test_force_delete_failure_stops_install(tmp_path: Path) -> None:
    """Force 模式下 /Delete 失败 → 脚本非零退出，不继续注册。

    前提：任务已存在（预置假 schtasks 状态文件），Force 才会真正执行 /Delete。
    """
    # 预置两个任务已存在，使 Force 分支实际发出 /Delete
    state = tmp_path / "fake_state"
    state.mkdir(parents=True, exist_ok=True)
    for name in ("AShareQuantAutomation-Daily", "AShareQuantAutomation-Weekly"):
        (state / f"{name}.txt").write_text("created", encoding="utf-8")
    log = tmp_path / "schtasks.log"
    env = dict(os.environ)
    env["FAKE_SCHTASKS_LOG"] = str(log)
    env["FAKE_SCHTASKS_STATE"] = str(state)
    env["FAKE_SCHTASKS_FAIL"] = "/Delete"
    proc = subprocess.run(
        [
            PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(SCRIPT), "-SchtasksExe", str(FAKE),
            "-TaskPrefix", "AShareQuantAutomation", "-Force",
        ],
        capture_output=True, text=True, cwd=str(ROOT), env=env,
        timeout=180, encoding="utf-8", errors="replace",
    )
    assert proc.returncode != 0
    assert "已注册任务" not in proc.stdout
    lines = [ln.strip() for ln in log.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
    # 删除失败后不得出现后续 /Create
    delete_idx = next(i for i, ln in enumerate(lines) if "/Delete" in ln)
    assert not any("/Create" in ln for ln in lines[delete_idx:])


def test_query_missing_task_does_not_crash(tmp_path: Path) -> None:
    """/Query 目标任务不存在（stderr + 非零）→ Task-Exists 返回 false 而非抛错。

    回归锁定：$ErrorActionPreference='Stop' 会把非零外部命令退出码提升为
    NativeCommandError 中断脚本——Force 安装时对不存在的任务做 /Query
    探测必须静默返回「不存在」并继续（真实安装曾因此失败）。
    """
    # 假 schtasks 初始无任何任务：创建前的 /Query 探测均失败（像真实 schtasks），
    # 创建后的验证 /Query 应成功。
    proc = _run_install(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "已注册任务: AShareQuantAutomation-Daily, AShareQuantAutomation-Weekly" in proc.stdout
    # 每个任务应恰好创建一次（Force=false 时不删除）
    lines = _log_lines(proc)
    for name in ("AShareQuantAutomation-Daily", "AShareQuantAutomation-Weekly"):
        creates = [ln for ln in lines if "/Create" in ln and name in ln]
        assert len(creates) == 1, f"{name} 应恰好创建一次: {creates}"


def test_force_with_missing_tasks_skips_delete(tmp_path: Path) -> None:
    """Force 模式下任务不存在 → 跳过删除（不抛 NativeCommandError），正常创建。"""
    proc = _run_install(tmp_path, force=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "已注册任务" in proc.stdout
    assert "任务不存在，跳过删除" in proc.stdout
    lines = _log_lines(proc)
    # 不存在 → 不发出 /Delete（只 /Create）
    assert not any("/Delete" in ln for ln in lines)


# ---------------------------------------------------------------------- #
# 正常路径：注册后验证存在 + 打印成功
# ---------------------------------------------------------------------- #

def test_success_registers_and_verifies_both(tmp_path: Path) -> None:
    proc = _run_install(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "已注册任务: AShareQuantAutomation-Daily, AShareQuantAutomation-Weekly" in proc.stdout
    lines = _log_lines(proc)
    # 每个任务：/Create 恰好一次；/Query 至少一次（注册内验证 + 顶部统一验证）
    for name in ("AShareQuantAutomation-Daily", "AShareQuantAutomation-Weekly"):
        creates = [ln for ln in lines if "/Create" in ln and name in ln]
        queries = [ln for ln in lines if "/Query" in ln and name in ln]
        assert len(creates) == 1, f"{name} 应恰好创建一次: {creates}"
        assert len(queries) >= 1, f"{name} 应至少验证存在一次: {queries}"


def test_whatif_shows_both_commands_and_touches_nothing(tmp_path: Path) -> None:
    """-WhatIf 必须显示 Daily/Weekly 完整命令（审计参数），且零真实 schtasks 调用。

    复审回归点：COMMAND 打印必须发生在 ShouldProcess 之前，使 WhatIf 也能
    看到完整命令（此前打印在 Invoke-Schtasks 内，WhatIf 不调用它 → 无预览）。
    """
    log = tmp_path / "schtasks.log"
    env = dict(os.environ)
    env["FAKE_SCHTASKS_LOG"] = str(log)
    proc = subprocess.run(
        [
            PWSH, "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(SCRIPT), "-SchtasksExe", str(FAKE), "-WhatIf",
        ],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env=env,
        timeout=180,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "(WhatIf)" in proc.stdout

    # 两条完整命令必须出现在 stdout
    cmd_lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("COMMAND:")]
    assert len(cmd_lines) == 2, f"WhatIf 应显示两条 COMMAND: {cmd_lines}"
    daily_cmd = next(ln for ln in cmd_lines if "AShareQuantAutomation-Daily" in ln)
    weekly_cmd = next(ln for ln in cmd_lines if "AShareQuantAutomation-Weekly" in ln)
    # Daily 预览不得含 /D；Weekly 必须含 /D SAT
    assert "/SC DAILY" in daily_cmd
    assert "/D" not in daily_cmd, f"Daily 预览不得含 /D: {daily_cmd}"
    assert "/SC WEEKLY" in weekly_cmd
    assert "/D SAT" in weekly_cmd, f"Weekly 预览必须含 /D SAT: {weekly_cmd}"

    # WhatIf 零真实调用：假 schtasks 日志必须为空
    assert not log.exists() or log.read_text(encoding="utf-8").strip() == ""
