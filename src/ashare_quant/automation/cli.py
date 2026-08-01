"""Phase 4 自动化 CLI 子命令。

子命令::

    ashare-quant automation daily [--date YYYY-MM-DD] [--dry-run] [--synthetic] [--config PATH]
    ashare-quant automation weekly [--date YYYY-MM-DD] [--dry-run] [--synthetic] [--config PATH]
    ashare-quant automation status [--config PATH]
    ashare-quant automation verify [--synthetic] [--config PATH]
    ashare-quant automation rerun --task daily|weekly [--date YYYY-MM-DD] [--synthetic] [--config PATH]
    ashare-quant automation install [--dry-run] [--yes] [--force] [--task-prefix NAME] [--config PATH]
    ashare-quant automation uninstall [--dry-run] [--yes] [--task-prefix NAME] [--config PATH]

安全边界（贯穿所有子命令）：
- 本系统只产出研究信号、模拟订单与模拟账户报告。
- 不连接券商、不接触真实资金；实盘开关被强制关闭。
- ``--synthetic`` 仅用于本机离线验证：注入合成行情与日历，绝不伪造"在线抓取成功"。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

from .calendar import load_trading_calendar
from .config import AutomationConfig, default_automation_config_path, load_automation_config
from .models import TaskType
from .reporting import render_status_markdown
from .scheduler import build_scheduler_plan
from .simulated_account import SimulatedAccountManager, assert_simulation_only
from .state import StateStore
from ..backtest.config import BacktestConfig


# ---------------------------------------------------------------------- #
# 配置与路径
# ---------------------------------------------------------------------- #

def _load_config(args: argparse.Namespace) -> AutomationConfig:
    path = getattr(args, "config", None) or default_automation_config_path()
    return load_automation_config(path)


def _as_of(args: argparse.Namespace, name: str = "date") -> Optional[date]:
    raw = getattr(args, name, None)
    if raw:
        return date.fromisoformat(raw)
    return None


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _build_synthetic_env(config: AutomationConfig) -> dict[str, Any]:
    """构造离线验证环境：合成行情 + 注入数据源 + 合成日历。

    仅用于 ``--synthetic`` 本机验证，绝不冒充真实在线数据。
    """
    import pandas as pd

    repo = _repo_root()
    if str(repo) not in sys.path:
        sys.path.insert(0, str(repo))
    from ..config import load_config as load_app_config  # type: ignore
    from .datasource import InjectedDataSource
    from .calendar import TradingCalendar
    from tests.research_samples import (  # type: ignore
        make_benchmark_data,
        make_historical_status_table,
        make_research_quotes,
    )

    app_cfg = load_app_config(repo / "config" / "default.yaml")
    # 合成样本只覆盖一段区间；离线演示须用 symbols=[]（从数据本身推导股票池），
    # 并缩短回看窗口以落在合成行情范围内——与 _smoke_daily.py 的 DataConfig 一致。
    config.data.symbols = []
    config.data.lookback_days = 200
    start = date(2020, 1, 2)
    n_days = 200
    quotes = make_research_quotes(start=start, n_days=n_days, n_stocks=8)
    status_df = make_historical_status_table(start=start, n_stocks=8)
    benchmark = make_benchmark_data(start=start, n_days=n_days)
    trade_dates = sorted({pd.Timestamp(d).date() for d in quotes["trade_date"]})
    cal = TradingCalendar.from_dates(trade_dates, source="synthetic-calendar")
    source = InjectedDataSource(
        quotes,
        name="synthetic-research-samples",
        synthetic=True,
        security_master=status_df,
        benchmark=benchmark,
    )
    universe_kwargs = {"min_turnover": 0.0, "min_listing_days": 120}
    return {
        "app_cfg": app_cfg,
        "source": source,
        "calendar": cal,
        "trade_dates": trade_dates,
        "universe_kwargs": universe_kwargs,
    }


# ---------------------------------------------------------------------- #
# 子命令实现
# ---------------------------------------------------------------------- #

def cmd_daily(args: argparse.Namespace) -> int:
    from .daily import DailyPipeline, run_daily

    config = _load_config(args)
    as_of = _as_of(args)
    store = StateStore(config.state_dir)
    dry_run = bool(getattr(args, "dry_run", False))

    if getattr(args, "synthetic", False):
        env = _build_synthetic_env(config)
        cal = env["calendar"]
        if as_of is None:
            as_of = env["trade_dates"][-1]
        pipeline = DailyPipeline(
            app_config=env["app_cfg"],
            calendar=cal,
            universe_kwargs=env["universe_kwargs"],
        )
        out = run_daily(
            config,
            as_of_date=as_of,
            data_source=env["source"],
            pipeline=pipeline,
            state_store=store,
            dry_run=dry_run,
        )
    else:
        if as_of is None:
            as_of = date.today()
        out = run_daily(
            config, as_of_date=as_of, state_store=store, dry_run=dry_run
        )

    print(f"daily {as_of.isoformat()}: {out.state.value} (exit={out.exit_code})")
    print(out.record.message)
    return out.exit_code


def cmd_weekly(args: argparse.Namespace) -> int:
    from .weekly import WeeklyPipeline, run_weekly

    config = _load_config(args)
    as_of = _as_of(args)
    store = StateStore(config.state_dir)
    dry_run = bool(getattr(args, "dry_run", False))

    if getattr(args, "synthetic", False):
        env = _build_synthetic_env(config)
        cal = env["calendar"]
        if as_of is None:
            as_of = env["trade_dates"][-1]
        # 注入日历需覆盖 as_of：若 as_of 在日历之外，补上该周六
        if not cal.covers(as_of):
            from .calendar import TradingCalendar

            sat = as_of + timedelta(days=(5 - as_of.weekday()) % 7)
            cal = TradingCalendar.from_dates(
                list(env["trade_dates"]) + [sat], source="synthetic-calendar+asof"
            )
            as_of = sat
        pipeline = WeeklyPipeline(calendar=cal)
        out = run_weekly(
            config,
            as_of_date=as_of,
            pipeline=pipeline,
            state_store=store,
            dry_run=dry_run,
        )
    else:
        if as_of is None:
            as_of = date.today()
        out = run_weekly(
            config, as_of_date=as_of, state_store=store, dry_run=dry_run
        )

    print(f"weekly {as_of.isoformat()}: {out.state.value} (exit={out.exit_code})")
    print(out.record.message)
    return out.exit_code


def cmd_status(args: argparse.Namespace) -> int:
    config = _load_config(args)
    store = StateStore(config.state_dir)
    manager = SimulatedAccountManager(config, BacktestConfig())

    daily = store.load_latest(TaskType.DAILY)
    weekly = store.load_latest(TaskType.WEEKLY)
    accounts = store.list_accounts()

    observation = []
    for acc in accounts:
        try:
            observation.append(manager.observation_progress(acc))
        except Exception:  # pragma: no cover - 防御性
            pass

    markdown = render_status_markdown(
        daily=daily,
        weekly=weekly,
        accounts=accounts,
        observation=observation,
    )
    print(markdown)
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    config = _load_config(args)
    lines: list[str] = []

    # 1) 实盘开关必须关闭
    try:
        assert_simulation_only(config)
        lines.append("✓ 实盘开关已关闭（simulation-only 确认）")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"✗ 实盘开关校验失败: {exc}")
        print("\n".join(lines))
        return 1

    # 2) 账户与资格结论
    for acc in config.accounts:
        lines.append(
            f"✓ 账户 {acc.account_id}: 轨道={acc.track.value} "
            f"资格={acc.eligibility_status.value}"
        )

    # 3) 交易日历（fail-closed）
    if getattr(args, "synthetic", False):
        env = _build_synthetic_env(config)
        cal = env["calendar"]
        lines.append(
            f"✓ 交易日历(合成): {cal.first_date} ~ {cal.last_date} 共 {len(cal)} 天"
        )
    else:
        try:
            cal = load_trading_calendar(
                config, as_of=date.today(), calendar=None
            )
            lines.append(
                f"✓ 交易日历: {cal.first_date} ~ {cal.last_date} 共 {len(cal)} 天"
            )
        except Exception as exc:  # noqa: BLE001
            lines.append(f"⚠ 交易日历不可用（fail-closed）: {exc}")

    # 4) 保留策略与调度
    lines.append(
        f"• 归档: enabled={config.archive.enabled} "
        f"retain={config.archive.retain_days}天 "
        f"max_batches={config.archive.max_batches}"
    )
    lines.append(
        f"• 调度: 每日 {config.scheduler.daily_time} / "
        f"每周 {config.scheduler.weekly_day} {config.scheduler.weekly_time}"
    )
    lines.append(f"• 观察窗口: target={config.observation.target_trading_days} 交易日")

    print("\n".join(lines))
    print(
        "\n边界声明: 本系统仅产出研究信号与模拟账户记录，"
        "未连接券商、未涉及真实资金。"
    )
    return 0


def cmd_rerun(args: argparse.Namespace) -> int:
    from .daily import DailyPipeline, run_daily
    from .weekly import WeeklyPipeline, run_weekly

    config = _load_config(args)
    as_of = _as_of(args)
    store = StateStore(config.state_dir)
    task = getattr(args, "task", "daily")

    if task == "daily":
        if getattr(args, "synthetic", False):
            env = _build_synthetic_env(config)
            cal = env["calendar"]
            if as_of is None:
                as_of = env["trade_dates"][-1]
            pipeline = DailyPipeline(
                app_config=env["app_cfg"],
                calendar=cal,
                universe_kwargs=env["universe_kwargs"],
            )
            out = run_daily(
                config,
                as_of_date=as_of,
                data_source=env["source"],
                pipeline=pipeline,
                state_store=store,
                force_retry=True,
            )
        else:
            if as_of is None:
                as_of = date.today()
            out = run_daily(
                config, as_of_date=as_of, state_store=store, force_retry=True
            )
    else:  # weekly
        if getattr(args, "synthetic", False):
            env = _build_synthetic_env(config)
            cal = env["calendar"]
            if as_of is None:
                as_of = env["trade_dates"][-1]
            if not cal.covers(as_of):
                from .calendar import TradingCalendar

                sat = as_of + timedelta(days=(5 - as_of.weekday()) % 7)
                cal = TradingCalendar.from_dates(
                    list(env["trade_dates"]) + [sat],
                    source="synthetic-calendar+asof",
                )
                as_of = sat
            out = run_weekly(
                config,
                as_of_date=as_of,
                pipeline=WeeklyPipeline(calendar=cal),
                state_store=store,
                force_retry=True,
            )
        else:
            if as_of is None:
                as_of = date.today()
            out = run_weekly(
                config,
                as_of_date=as_of,
                state_store=store,
                force_retry=True,
            )

    print(f"rerun {task} {as_of.isoformat()}: {out.state.value} (exit={out.exit_code})")
    print(out.record.message)
    return out.exit_code


def _run_powershell(script_name: str, extra: list[str], *, yes: bool, dry_run: bool) -> int:
    ps1 = _repo_root() / "scripts" / script_name
    cmd = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(ps1),
    ] + extra
    printable = " ".join(f'"{c}"' if " " in str(c) else str(c) for c in cmd)
    print(f"将要执行: {printable}")
    if dry_run or not yes:
        print("（dry-run / 未确认，未实际执行。使用 --yes 执行）")
        return 0
    proc = subprocess.run(cmd)
    return proc.returncode


def cmd_install(args: argparse.Namespace) -> int:
    extra: list[str] = []
    if getattr(args, "force", False):
        extra.append("-Force")
    if getattr(args, "task_prefix", None):
        extra += ["-TaskPrefix", args.task_prefix]
    return _run_powershell(
        "install_scheduler.ps1",
        extra,
        yes=bool(getattr(args, "yes", False)),
        dry_run=bool(getattr(args, "dry_run", False)),
    )


def cmd_uninstall(args: argparse.Namespace) -> int:
    extra: list[str] = []
    if getattr(args, "task_prefix", None):
        extra += ["-TaskPrefix", args.task_prefix]
    return _run_powershell(
        "uninstall_scheduler.ps1",
        extra,
        yes=bool(getattr(args, "yes", False)),
        dry_run=bool(getattr(args, "dry_run", False)),
    )


# ---------------------------------------------------------------------- #
# 解析器装配
# ---------------------------------------------------------------------- #

def register(subparsers: Any) -> None:
    """把 ``automation`` 子命令组挂到主解析器。"""
    p = subparsers.add_parser(
        "automation",
        help="Phase 4 本机自动化（研究/模拟，非实盘）",
    )
    auto = p.add_subparsers(dest="auto_command", required=True)

    # daily
    p_d = auto.add_parser("daily", help="运行每日自动化管线")
    p_d.add_argument("--date", help="业务日 YYYY-MM-DD（默认今天）")
    p_d.add_argument("--config", help="自动化配置 YAML 路径")
    p_d.add_argument("--dry-run", action="store_true", help="只演练不落盘")
    p_d.add_argument(
        "--synthetic",
        action="store_true",
        help="本机离线验证：注入合成行情与日历（非真实在线数据）",
    )
    p_d.set_defaults(func=cmd_daily)

    # weekly
    p_w = auto.add_parser("weekly", help="运行每周汇总")
    p_w.add_argument("--date", help="业务日 YYYY-MM-DD（默认今天）")
    p_w.add_argument("--config", help="自动化配置 YAML 路径")
    p_w.add_argument("--dry-run", action="store_true", help="只演练不落盘")
    p_w.add_argument(
        "--synthetic",
        action="store_true",
        help="本机离线验证：注入合成行情与日历（非真实在线数据）",
    )
    p_w.set_defaults(func=cmd_weekly)

    # status
    p_s = auto.add_parser("status", help="查看最近运行与模拟账户状态")
    p_s.add_argument("--config", help="自动化配置 YAML 路径")
    p_s.set_defaults(func=cmd_status)

    # verify
    p_v = auto.add_parser("verify", help="校验安全边界与配置可行性")
    p_v.add_argument("--config", help="自动化配置 YAML 路径")
    p_v.add_argument(
        "--synthetic",
        action="store_true",
        help="本机离线验证：注入合成日历以校验日历加载路径",
    )
    p_v.set_defaults(func=cmd_verify)

    # rerun
    p_r = auto.add_parser("rerun", help="强制重跑某次每日/每周运行")
    p_r.add_argument("--task", choices=["daily", "weekly"], default="daily")
    p_r.add_argument("--date", help="业务日 YYYY-MM-DD")
    p_r.add_argument("--config", help="自动化配置 YAML 路径")
    p_r.add_argument(
        "--synthetic",
        action="store_true",
        help="本机离线验证：注入合成行情与日历",
    )
    p_r.set_defaults(func=cmd_rerun)

    # install
    p_i = auto.add_parser("install", help="注册 Windows 任务计划（调用 PowerShell）")
    p_i.add_argument("--config", help="自动化配置 YAML 路径")
    p_i.add_argument("--task-prefix", help="任务名前缀（覆盖配置默认值）")
    p_i.add_argument("--force", action="store_true", help="覆盖式注册（先删后建）")
    p_i.add_argument("--dry-run", action="store_true", help="仅打印将执行的命令")
    p_i.add_argument("--yes", action="store_true", help="确认实际执行（否则只打印）")
    p_i.set_defaults(func=cmd_install)

    # uninstall
    p_u = auto.add_parser("uninstall", help="注销 Windows 任务计划（调用 PowerShell）")
    p_u.add_argument("--config", help="自动化配置 YAML 路径")
    p_u.add_argument("--task-prefix", help="任务名前缀（覆盖配置默认值）")
    p_u.add_argument("--dry-run", action="store_true", help="仅打印将执行的命令")
    p_u.add_argument("--yes", action="store_true", help="确认实际执行（否则只打印）")
    p_u.set_defaults(func=cmd_uninstall)
