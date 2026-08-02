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
from .models import FORCE_RETRY_ALLOWED_STATES, TaskType
from .reporting import render_status_markdown
from .scheduler import build_scheduler_plan
from .simulated_account import SimulatedAccountManager, assert_simulation_only
from .state import StateStore
from ..backtest.config import BacktestConfig


def _build_auto_data_source(config: AutomationConfig):
    """构造真实行情数据源（生产路径）。

    使用 :func:`build_updating_data_source`：``provider_factory`` 留空时，
    :class:`FetchManager` 自行构造 AKShare（主）+ BaoStock（备）真实提供器，
    并完成 抓取 -> 标准化 -> 落盘 curated -> manifest/SHA256 全链路。

    数据源不可用 / 抓取失败时由管线按既定语义降级为
    ``SKIPPED_DATA_UNAVAILABLE``（可接受跳过），绝不伪造在线成功。
    """
    from .data_update import build_updating_data_source
    from ..config import default_config_path, load_config

    app_cfg = load_config(default_config_path())
    return build_updating_data_source(
        app_cfg,
        data_dir=config.data_dir,
        benchmark_symbols=list(config.data.benchmark_symbols),
    )


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
    trigger = str(getattr(args, "trigger", "manual"))

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
            trigger=trigger,
        )
    else:
        if as_of is None:
            as_of = date.today()
        # 真实数据源：抓取 + 落盘 + 清单，产出真实行情/信号/模拟账户产物
        data_source = _build_auto_data_source(config)
        out = run_daily(
            config,
            as_of_date=as_of,
            data_source=data_source,
            state_store=store,
            dry_run=dry_run,
            trigger=trigger,
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
        # 每周任务不需要行情数据源：只读取每日运行产物（项目设计）
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


def _check_data_sources(lines: list[str]) -> None:
    """检查 AKShare / BaoStock 是否可导入（工作台真实抓取前置条件）。

    缺失或导入阶段异常标记为 [WARN]（不 fail）：离线测试与核心不依赖
    数据源 SDK，但工作台 daily 将无法产生真实行情产物——界面会如实显示
    skipped。``verify`` 绝不因数据源检查崩溃。
    安装命令：``pip install -e ".[workbench]"``。
    """
    for mod_name in ("akshare", "baostock"):
        try:
            __import__(mod_name)
            lines.append(f"[OK] 数据源 {mod_name} 可导入（工作台可真实抓取）")
        except Exception as exc:  # noqa: BLE001 - 导入阶段任何异常都降级为 WARN
            lines.append(
                f"[WARN] 数据源 {mod_name} 不可用（{type(exc).__name__}）："
                "工作台 daily 无法产生真实行情产物。安装: pip install -e \".[workbench]\""
            )


def cmd_verify(args: argparse.Namespace) -> int:
    """安全边界与配置可行性校验（fail-closed）。

    输出使用纯 ASCII 状态标志（``[OK]``/``[WARN]``/``[FAIL]``），确保
    GBK / UTF-8 等任何 Windows 控制台编码下都不会因符号字符崩溃
    （历史缺陷：``✓`` 等符号在 GBK 控制台触发 ``UnicodeEncodeError``）。

    任一关键项失败（实盘开关、交易日历）必须以非零退出码结束——缺少
    交易日历时 ``verify`` 必须失败，而不是只给警告。
    """
    config = _load_config(args)
    lines: list[str] = []
    has_failure = False

    # 1) 实盘开关必须关闭
    try:
        assert_simulation_only(config)
        lines.append("[OK] 实盘开关已关闭（simulation-only 确认）")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"[FAIL] 实盘开关校验失败: {exc}")
        has_failure = True

    # 2) 账户与资格结论
    for acc in config.accounts:
        lines.append(
            f"[OK] 账户 {acc.account_id}: 轨道={acc.track.value} "
            f"资格={acc.eligibility_status.value}"
        )

    # 3) 交易日历（fail-closed：加载失败 = verify 失败，非零退出）
    if getattr(args, "synthetic", False):
        env = _build_synthetic_env(config)
        cal = env["calendar"]
        lines.append(
            f"[OK] 交易日历(合成): {cal.first_date} ~ {cal.last_date} 共 {len(cal)} 天"
        )
    else:
        try:
            cal = load_trading_calendar(
                config, as_of=date.today(), calendar=None
            )
            lines.append(
                f"[OK] 交易日历: {cal.first_date} ~ {cal.last_date} 共 {len(cal)} 天"
            )
        except Exception as exc:  # noqa: BLE001
            lines.append(f"[FAIL] 交易日历不可用（fail-closed）: {exc}")
            has_failure = True

    # 4) 数据源 SDK 可导入性（工作台真实抓取的前置条件）
    _check_data_sources(lines)

    # 5) 保留策略与调度
    lines.append(
        f"[INFO] 归档: enabled={config.archive.enabled} "
        f"retain={config.archive.retain_days}天 "
        f"max_batches={config.archive.max_batches}"
    )
    lines.append(
        f"[INFO] 调度: 每日 {config.scheduler.daily_time} / "
        f"每周 {config.scheduler.weekly_day} {config.scheduler.weekly_time}"
    )
    lines.append(
        f"[INFO] 观察窗口: target={config.observation.target_trading_days} 交易日"
    )

    print("\n".join(lines))
    print(
        "\n边界声明: 本系统仅产出研究信号与模拟账户记录，"
        "未连接券商、未涉及真实资金。"
    )
    return 1 if has_failure else 0


def cmd_rerun(args: argparse.Namespace) -> int:
    from .daily import DailyPipeline, run_daily
    from .weekly import WeeklyPipeline, run_weekly

    config = _load_config(args)
    as_of = _as_of(args)
    store = StateStore(config.state_dir)
    task = getattr(args, "task", "daily")
    trigger = str(getattr(args, "trigger", "manual"))

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
                trigger=trigger,
            )
        else:
            if as_of is None:
                as_of = date.today()
            # 重跑同样需要真实数据源（重新抓取数据）
            data_source = _build_auto_data_source(config)
            out = run_daily(
                config,
                as_of_date=as_of,
                data_source=data_source,
                state_store=store,
                force_retry=True,
                trigger=trigger,
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

    if out.force_retry_rejected:
        # FR-25：force-retry 只对可重试终态放行，拒绝时必须让用户看懂为什么。
        allowed = "、".join(sorted(s.value for s in FORCE_RETRY_ALLOWED_STATES))
        print(
            f"rerun {task} {as_of.isoformat()}: 已拒绝 "
            f"(既有终态 {out.state.value}，exit={out.exit_code})"
        )
        print(f"可强制重试的终态仅限：{allowed}")
        print("既有运行记录、模拟账户、观察窗口与模拟订单均未改动。")
        return out.exit_code

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
    p_d.add_argument(
        "--trigger",
        choices=("manual", "scheduled"),
        default="manual",
        help="触发来源：计划任务必须传 scheduled（计入 Gate 4B 正式观察）；手工重跑默认 manual",
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
    p_r = auto.add_parser(
        "rerun",
        help=(
            "强制重跑某次每日/每周运行（仅限 FAILED / "
            "SKIPPED_DATA_UNAVAILABLE / BLOCKED_DATA_QUALITY；"
            "已 SUCCESS 的业务日会被拒绝）"
        ),
    )
    p_r.add_argument("--task", choices=["daily", "weekly"], default="daily")
    p_r.add_argument("--date", help="业务日 YYYY-MM-DD")
    p_r.add_argument("--config", help="自动化配置 YAML 路径")
    p_r.add_argument(
        "--synthetic",
        action="store_true",
        help="本机离线验证：注入合成行情与日历",
    )
    p_r.add_argument(
        "--trigger",
        choices=("manual", "scheduled"),
        default="manual",
        help="触发来源：计划任务必须传 scheduled（计入 Gate 4B 正式观察）；手工重跑默认 manual",
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
