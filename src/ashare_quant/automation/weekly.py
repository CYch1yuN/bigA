"""Phase 4 每周自动化管线：8 步复盘，从一周流水汇成一份可审计的账。

流水线全景
----------
::

    1  preflight            资格与安全边界自检（实盘开关必须关闭）
    2  calendar             交易日历 fail-closed 加载 + 本 ISO 周区间求解
    3  collect_runs         收集本周每日运行记录与产物
    4  coverage_audit       跑批覆盖率审计（缺失 / 失败 / 阻断一律显式暴露）
    5  account_review       模拟账户周度复核（权益、收益、回撤）
    6  observation_review   60 交易日观察窗口进度 + 资格闸门复核
    7  archive              按保留策略归档历史结果
    8  artifacts            产物落盘（JSON + Markdown，附边界声明）

三条设计约束
------------
**一、周报对账户状态只读。**
每周任务不做盯市、不撮合、不改账户文件。原因很实际：
``mark_to_market`` 会推进 ``observation_days``，如果周报也调它，
一周就凭空多出一个"观察日"，60 交易日窗口会被悄悄注水。
周报只负责**读**和**说**，不负责**改**。

**二、周六不是交易日，但周报照跑。**
每日管线遇到非交易日会 ``SKIPPED_NON_TRADING_DAY``；每周管线不能照抄——
它的默认执行时间就是周六。只有当**整个 ISO 周一个交易日都没有**
（例如完整的春节休市周）时，才跳过。

**三、缺失的每日跑批是"发现"，不是"故障"。**
周一漏跑了，周报不应该也跟着失败——那只会让人同时丢掉数据和报告。
正确做法是把缺口写进报告最显眼的位置，并打 WARNING 日志。
周报的价值恰恰在于**把这种缺口捞出来**。

边界声明
--------
本管线汇总的是研究信号与**模拟**账户记录。稳健轨结论
``NOT_ELIGIBLE_FOR_LIVE_TRADING``，激进轨结论 ``SIMULATION_ONLY``。
观察窗口跑满 60 个交易日**不等于**取得实盘资格。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from ..backtest.config import BacktestConfig
from .archive import ArchiveResult, archive_results
from .calendar import TradingCalendar, load_trading_calendar
from .config import AutomationConfig
from .models import (
    NotEligibleError,
    RunRecord,
    RunState,
    SimulatedAccountState,
    TRACK_ELIGIBILITY,
    TaskType,
)
from .reporting import (
    iso_week_key,
    render_weekly_markdown,
    result_paths,
    write_json_artifact,
)
from .runner import AutomationRunner, PipelineContext, RunOutcome
from .simulated_account import SimulatedAccountManager, assert_simulation_only
from .state import StateStore
from .weekly_research import run_weekly_research_step

__all__ = [
    "WEEKLY_STEPS",
    "WeeklyPipeline",
    "week_bounds",
    "run_weekly",
]


#: 每周管线的 8 个步骤名（顺序即执行顺序，测试据此断言）。
WEEKLY_STEPS: tuple[str, ...] = (
    "preflight",
    "calendar",
    "collect_runs",
    "coverage_audit",
    "weekly_research",
    "account_review",
    "observation_review",
    "archive",
    "artifacts",
)

#: 被视为"这天没跑成"的终态。
_UNHEALTHY_STATES: frozenset[RunState] = frozenset(
    {
        RunState.FAILED,
        RunState.BLOCKED_DATA_QUALITY,
        RunState.BLOCKED_LOCKED,
        RunState.BLOCKED_NOT_ELIGIBLE,
        RunState.SKIPPED_DATA_UNAVAILABLE,
    }
)


# ---------------------------------------------------------------------- #
# 工具
# ---------------------------------------------------------------------- #


def week_bounds(day: date) -> tuple[date, date]:
    """返回 ``day`` 所在 ISO 周的周一与周日。"""
    monday = day - timedelta(days=day.isoweekday() - 1)
    return (monday, monday + timedelta(days=6))


def _read_json(path: Path) -> Optional[dict[str, Any]]:
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _dec(value: Any, default: str = "0") -> Decimal:
    if value is None or value == "":
        return Decimal(default)
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(default)


def _pct(numerator: Decimal, denominator: Decimal) -> Optional[float]:
    if denominator == 0:
        return None
    return round(float(numerator / denominator) * 100, 4)


def _max_drawdown(curve: Sequence[dict[str, Any]]) -> Optional[float]:
    """按权益曲线计算最大回撤（百分比，负值）。"""
    peak: Optional[Decimal] = None
    worst: Optional[float] = None
    for point in curve:
        equity = _dec(point.get("total_equity"))
        if equity <= 0:
            continue
        if peak is None or equity > peak:
            peak = equity
        if peak and peak > 0:
            drawdown = round(float(equity / peak - 1) * 100, 4)
            if worst is None or drawdown < worst:
                worst = drawdown
    return worst


# ---------------------------------------------------------------------- #
# 管线
# ---------------------------------------------------------------------- #


@dataclass
class WeeklyPipeline:
    """每周管线：作为 ``Pipeline`` 回调交给 ``AutomationRunner`` 执行。

    Args:
        calendar: 注入的交易日历（测试用）；为 None 时按配置 fail-closed 加载。
        backtest_config: 仅用于构造 ``SimulatedAccountManager`` 读取观察窗口配置。
        skip_archive: 显式跳过归档（``--no-archive``）。
    """

    calendar: Optional[TradingCalendar] = None
    backtest_config: BacktestConfig = field(default_factory=BacktestConfig)
    skip_archive: bool = False
    # 每周研究步骤：复用 Phase 3 ResearchRunner 跑真实完整周研究。
    research_dir: Optional[Path] = None
    research_enabled: bool = True

    # ------------------------------------------------------------------ #
    def __call__(self, ctx: PipelineContext) -> None:
        self.execute(ctx)

    # ------------------------------------------------------------------ #
    def execute(self, ctx: PipelineContext) -> None:
        cfg = ctx.config
        as_of = ctx.as_of_date

        # -- 1. 边界自检 ------------------------------------------------ #
        with ctx.step("preflight") as step:
            assert_simulation_only(cfg)
            accounts_cfg = list(cfg.accounts)
            if not accounts_cfg:
                raise ValueError(
                    "配置中没有任何模拟账户；请在 accounts 下声明 "
                    "paper-steady / paper-aggressive"
                )
            step.detail.update(
                {
                    "accounts": [a.account_id for a in accounts_cfg],
                    "live_trading_enabled": False,
                    "broker_connected": False,
                    "mutates_account_state": False,
                    "eligibility": {
                        a.account_id: a.eligibility_status.value for a in accounts_cfg
                    },
                }
            )

        # -- 2. 本周区间 ------------------------------------------------ #
        with ctx.step("calendar") as step:
            cal = load_trading_calendar(cfg, as_of=as_of, calendar=self.calendar)
            ctx.scratch["calendar"] = cal
            week_key = iso_week_key(as_of)
            week_start, week_end = week_bounds(as_of)
            # 只复盘"已经发生过的"交易日：区间右端取业务日与周日的较小者
            review_end = min(week_end, as_of)
            trading_days = list(cal.trading_days_between(week_start, review_end))
            ctx.scratch["week_key"] = week_key
            ctx.scratch["week_start"] = week_start
            ctx.scratch["week_end"] = week_end
            ctx.scratch["trading_days"] = trading_days
            step.detail.update(cal.summary())
            step.detail.update(
                {
                    "week_key": week_key,
                    "week_start": week_start.isoformat(),
                    "week_end": week_end.isoformat(),
                    "review_end": review_end.isoformat(),
                    "trading_days": [d.isoformat() for d in trading_days],
                    "trading_day_count": len(trading_days),
                    "as_of_is_trading_day": (
                        cal.is_trading_day(as_of) if cal.covers(as_of) else None
                    ),
                }
            )
            if not trading_days:
                ctx.skip_non_trading_day(
                    f"{week_key} 整周没有交易日，跳过本次每周汇总",
                    week_key=week_key,
                    week_start=week_start.isoformat(),
                    week_end=week_end.isoformat(),
                )

        # -- 3. 收集每日运行 -------------------------------------------- #
        with ctx.step("collect_runs") as step:
            collected = self._collect_runs(ctx)
            step.detail.update(
                {
                    "expected_trading_days": len(ctx.scratch["trading_days"]),
                    "records_found": len(collected["records"]),
                    "signals": len(collected["signals"]),
                    "orders": len(collected["orders"]),
                    "artifact_dirs": collected["artifact_dirs"],
                }
            )

        # -- 4. 覆盖率审计 ---------------------------------------------- #
        with ctx.step("coverage_audit") as step:
            audit = self._audit_coverage(ctx)
            step.detail.update(audit)
            if not audit["coverage_ok"]:
                ctx.logger.warning(
                    "weekly_coverage_gap",
                    "本周每日跑批存在缺口：缺失 %d 天、异常 %d 天"
                    % (len(audit["missing_days"]), len(audit["unhealthy_days"])),
                    week_key=ctx.scratch["week_key"],
                    missing_days=audit["missing_days"],
                    unhealthy_days=audit["unhealthy_days"],
                )

        # -- 4b. 每周研究（只读研究，复用 Phase 3 ResearchRunner） ------- #
        if self.research_enabled:
            research_dir = self.research_dir or (cfg.base_dir / "research_data")
            run_weekly_research_step(
                ctx,
                research_dir=research_dir,
                code_commit=getattr(cfg, "code_commit", None),
            )

        # -- 5. 账户复核（只读） ----------------------------------------- #
        with ctx.step("account_review") as step:
            review = self._review_accounts(ctx)
            step.detail.update(
                {
                    "accounts": len(review["accounts"]),
                    "uninitialized": review["uninitialized"],
                    "weekly_return_pct": {
                        k: v.get("weekly_return_pct")
                        for k, v in review["performance"].items()
                    },
                    "state_written": False,
                }
            )

        # -- 6. 观察窗口与资格闸门 --------------------------------------- #
        with ctx.step("observation_review") as step:
            gate = self._review_observation(ctx)
            step.detail.update(gate)

        # -- 7. 归档 ----------------------------------------------------- #
        with ctx.step("archive") as step:
            if self.skip_archive:
                result = ArchiveResult(
                    enabled=False,
                    dry_run=ctx.dry_run,
                    batch=ctx.scratch["week_key"],
                    reason="本次运行显式跳过归档（--no-archive）",
                )
            else:
                protect = {d.isoformat() for d in ctx.scratch["trading_days"]}
                result = archive_results(
                    cfg,
                    as_of_date=as_of,
                    batch_key=ctx.scratch["week_key"],
                    dry_run=ctx.dry_run,
                    protect_buckets=protect,
                )
            ctx.scratch["archive"] = result
            step.detail.update(
                {
                    "enabled": result.enabled,
                    "archived_count": result.archived_count,
                    "archived_bytes": result.archived_bytes,
                    "pruned_batches": result.pruned_batches,
                    "removed_files": result.removed_files,
                    "dry_run": ctx.dry_run,
                }
            )

        # -- 8. 数据产物 ------------------------------------------------ #
        # 只写"与终态无关"的数据产物；终态报告延迟到运行编排器设置终态后渲染
        # （缺陷 #2：避免报告里出现 "运行中（非终态）"）。
        with ctx.step("artifacts") as step:
            written = self._write_data_artifacts(ctx)
            step.detail.update(
                {"files": [p.name for p in written], "dry_run": ctx.dry_run}
            )

        ctx.scratch["_finalize_report"] = lambda: self._write_report(ctx)

        ctx.record.message = self._summary_message(ctx)

    # ------------------------------------------------------------------ #
    # 步骤实现
    # ------------------------------------------------------------------ #

    def _collect_runs(self, ctx: PipelineContext) -> dict[str, Any]:
        """读取本周每个交易日的运行记录与产物。"""
        cfg = ctx.config
        trading_days: list[date] = ctx.scratch["trading_days"]

        records: list[RunRecord] = []
        by_day: dict[str, Optional[RunRecord]] = {}
        signals: list[dict[str, Any]] = []
        orders: list[dict[str, Any]] = []
        artifact_dirs = 0

        for day in trading_days:
            rec = ctx.state_store.load_run(TaskType.DAILY, day)
            by_day[day.isoformat()] = rec
            if rec is not None:
                records.append(rec)

            paths = result_paths(cfg, task_type=TaskType.DAILY, as_of_date=day)
            if paths.root.exists():
                artifact_dirs += 1
            sig_payload = _read_json(paths.signals_json)
            if sig_payload:
                for item in sig_payload.get("signals") or []:
                    enriched = dict(item)
                    enriched.setdefault("strategy_track", enriched.get("track"))
                    enriched.setdefault("as_of_date", day.isoformat())
                    signals.append(enriched)
            order_payload = _read_json(paths.orders_json)
            if order_payload:
                for item in order_payload.get("orders") or []:
                    enriched = dict(item)
                    enriched.setdefault("as_of_date", day.isoformat())
                    orders.append(enriched)

        collected = {
            "records": records,
            "by_day": by_day,
            "signals": signals,
            "orders": orders,
            "artifact_dirs": artifact_dirs,
        }
        ctx.scratch["collected"] = collected
        return collected

    def _audit_coverage(self, ctx: PipelineContext) -> dict[str, Any]:
        """核对"应该跑几天"和"实际跑成几天"。"""
        trading_days: list[date] = ctx.scratch["trading_days"]
        by_day: dict[str, Optional[RunRecord]] = ctx.scratch["collected"]["by_day"]

        missing: list[str] = []
        unhealthy: list[dict[str, str]] = []
        succeeded = 0
        skipped_non_trading = 0
        state_counts: dict[str, int] = {}

        for day in trading_days:
            key = day.isoformat()
            rec = by_day.get(key)
            if rec is None:
                missing.append(key)
                continue
            state_counts[rec.state.value] = state_counts.get(rec.state.value, 0) + 1
            if rec.state is RunState.SUCCESS:
                succeeded += 1
            elif rec.state is RunState.SKIPPED_NON_TRADING_DAY:
                # 日历判定与跑批判定不一致，属于需要人看一眼的异常
                skipped_non_trading += 1
                unhealthy.append({"date": key, "state": rec.state.value})
            elif rec.state in _UNHEALTHY_STATES:
                unhealthy.append({"date": key, "state": rec.state.value})

        expected = len(trading_days)
        audit = {
            "expected_trading_days": expected,
            "succeeded_days": succeeded,
            "missing_days": missing,
            "unhealthy_days": unhealthy,
            "state_counts": state_counts,
            "calendar_disagreement_days": skipped_non_trading,
            "coverage_pct": (
                round(succeeded / expected * 100, 2) if expected else 0.0
            ),
            "coverage_ok": (not missing and not unhealthy),
        }
        ctx.scratch["audit"] = audit
        return audit

    def _review_accounts(self, ctx: PipelineContext) -> dict[str, Any]:
        """读取账户状态并计算周度表现。**不写回任何状态。**"""
        cfg = ctx.config
        manager = SimulatedAccountManager(cfg, self.backtest_config)
        ctx.scratch["manager"] = manager

        week_start: date = ctx.scratch["week_start"]
        review_end: date = min(ctx.scratch["week_end"], ctx.as_of_date)

        accounts: list[SimulatedAccountState] = []
        equity: dict[str, Any] = {}
        performance: dict[str, dict[str, Any]] = {}
        uninitialized: list[str] = []

        for acc_cfg in cfg.accounts:
            state = ctx.state_store.load_account(acc_cfg.account_id)
            if state is None:
                # 账户还没被任何每日运行创建过：构造一份仅用于展示的初始状态，
                # 绝不落盘——周报没有创建账户的权力。
                state = manager.create_account(acc_cfg)
                uninitialized.append(acc_cfg.account_id)
            accounts.append(state)

            curve = manager.equity_curve(state)
            in_week = [
                p
                for p in curve
                if week_start.isoformat() <= str(p.get("date", "")) <= review_end.isoformat()
            ]
            before = [
                p for p in curve if str(p.get("date", "")) < week_start.isoformat()
            ]

            last_point = in_week[-1] if in_week else (curve[-1] if curve else None)
            base_point = before[-1] if before else (in_week[0] if in_week else None)

            end_equity = (
                _dec(last_point.get("total_equity"))
                if last_point
                else _dec(state.initial_cash)
            )
            start_equity = (
                _dec(base_point.get("total_equity"))
                if base_point
                else _dec(state.initial_cash)
            )
            initial = _dec(state.initial_cash, "1")

            equity[state.account_id] = {
                "cash": str(last_point.get("cash")) if last_point else str(state.cash),
                "position_value": (
                    str(last_point.get("position_value")) if last_point else "0"
                ),
                "total_equity": str(end_equity),
                "positions": len(state.positions),
                "eligibility_status": state.eligibility_status.value,
                "as_of": (
                    str(last_point.get("date"))
                    if last_point
                    else (state.as_of_date.isoformat() if state.as_of_date else None)
                ),
            }
            performance[state.account_id] = {
                "strategy_track": state.strategy_track.value,
                "eligibility_status": state.eligibility_status.value,
                "week_points": len(in_week),
                "start_equity": str(start_equity),
                "end_equity": str(end_equity),
                "weekly_return_pct": _pct(end_equity - start_equity, start_equity),
                "cumulative_return_pct": _pct(end_equity - initial, initial),
                "max_drawdown_pct": _max_drawdown(curve),
                "observation_days": int(state.observation_days),
                "positions": sorted(state.positions.keys()),
                "simulated": True,
                "live_trading": False,
            }

        review = {
            "accounts": accounts,
            "equity": equity,
            "performance": performance,
            "uninitialized": uninitialized,
        }
        ctx.scratch["review"] = review
        return review

    def _review_observation(self, ctx: PipelineContext) -> dict[str, Any]:
        """观察窗口进度 + 资格闸门复核。

        这是本管线的安全阀：任何一个账户的资格结论如果偏离 Phase 3 的硬编码
        映射，立即 ``NotEligibleError`` 阻断，退出码 5。
        跑满 60 个交易日只会把 ``completed`` 置真，**不会**改写资格结论。
        """
        manager: SimulatedAccountManager = ctx.scratch["manager"]
        accounts: list[SimulatedAccountState] = ctx.scratch["review"]["accounts"]

        observation: list[dict[str, Any]] = []
        completed: list[str] = []
        for state in accounts:
            expected = TRACK_ELIGIBILITY[state.strategy_track]
            if state.eligibility_status is not expected:
                raise NotEligibleError(
                    f"账户 {state.account_id} 的资格结论为 "
                    f"{state.eligibility_status.value}，"
                    f"与 Phase 3 复审结论 {expected.value} 不一致；"
                    f"资格结论属于安全边界，禁止在运行期被改写"
                )
            progress = manager.observation_progress(state)
            observation.append(progress)
            if progress.get("completed"):
                completed.append(state.account_id)

        ctx.scratch["observation"] = observation
        gate = {
            "accounts_reviewed": len(accounts),
            "observation_completed": completed,
            "live_trading_authorized": False,
            "requires_independent_review": True,
            "note": (
                "观察窗口完成仅表示样本量达标，不代表取得实盘资格；"
                "实盘资格需独立复审，且不由本自动化系统作出"
            ),
        }
        ctx.scratch["gate"] = gate
        return gate

    # ------------------------------------------------------------------ #
    # 产物
    # ------------------------------------------------------------------ #

    def _weekly_stats(self, ctx: PipelineContext) -> dict[str, Any]:
        audit: dict[str, Any] = ctx.scratch["audit"]
        collected: dict[str, Any] = ctx.scratch["collected"]
        orders: list[dict[str, Any]] = collected["orders"]
        filled = [o for o in orders if str(o.get("status")) == "FILLED"]
        rejected = [o for o in orders if str(o.get("status")) == "REJECTED"]
        perf: dict[str, dict[str, Any]] = ctx.scratch["review"]["performance"]

        stats: dict[str, Any] = {
            "ISO 周": ctx.scratch["week_key"],
            "应跑交易日": audit["expected_trading_days"],
            "成功跑批": audit["succeeded_days"],
            "缺失跑批": len(audit["missing_days"]),
            "异常跑批": len(audit["unhealthy_days"]),
            "跑批覆盖率": f"{audit['coverage_pct']}%",
            "新增研究信号": len(collected["signals"]),
            "模拟订单记录": len(orders),
            "模拟成交": len(filled),
            "模拟拒单": len(rejected),
        }
        for account_id, item in perf.items():
            stats[f"{account_id} 周收益率"] = (
                "—"
                if item.get("weekly_return_pct") is None
                else f"{item['weekly_return_pct']}%"
            )
            stats[f"{account_id} 最大回撤"] = (
                "—"
                if item.get("max_drawdown_pct") is None
                else f"{item['max_drawdown_pct']}%"
            )
        return stats

    def _write_data_artifacts(self, ctx: PipelineContext) -> list[Path]:
        """写"与终态无关"的数据产物：信号 / 订单 / 账户 / 周度汇总。

        终态报告（run.json + Markdown）由运行编排器在设置终态之后通过
        ``_write_report`` 落盘（缺陷 #2）。
        """
        cfg = ctx.config
        week_key: str = ctx.scratch["week_key"]
        paths = result_paths(cfg, task_type=TaskType.WEEKLY, as_of_date=ctx.as_of_date)
        stats = self._weekly_stats(ctx)
        ctx.scratch["stats"] = stats
        if ctx.dry_run:
            return []
        paths.ensure()

        collected: dict[str, Any] = ctx.scratch["collected"]
        review: dict[str, Any] = ctx.scratch["review"]
        audit: dict[str, Any] = ctx.scratch["audit"]
        archive: ArchiveResult = ctx.scratch["archive"]
        observation: list[dict[str, Any]] = ctx.scratch["observation"]
        accounts: list[SimulatedAccountState] = review["accounts"]
        records: list[RunRecord] = collected["records"]

        written: list[Path] = []

        written.append(
            write_json_artifact(
                paths.signals_json,
                {
                    "week_key": week_key,
                    "as_of_date": ctx.as_of_date.isoformat(),
                    "run_id": ctx.run_id,
                    "simulated": True,
                    "live_trading": False,
                    "note": (
                        "本周研究信号汇总，非投资建议、非交易指令；"
                        "全部信号仅用于推进模拟账户"
                    ),
                    "signals": collected["signals"],
                },
            )
        )
        written.append(
            write_json_artifact(
                paths.orders_json,
                {
                    "week_key": week_key,
                    "as_of_date": ctx.as_of_date.isoformat(),
                    "run_id": ctx.run_id,
                    "simulated": True,
                    "orders": collected["orders"],
                },
            )
        )
        written.append(
            write_json_artifact(
                paths.accounts_json,
                {
                    "week_key": week_key,
                    "as_of_date": ctx.as_of_date.isoformat(),
                    "run_id": ctx.run_id,
                    "state_mutated": False,
                    "equity": review["equity"],
                    "performance": review["performance"],
                    "observation": observation,
                    "accounts": [a.to_dict() for a in accounts],
                },
            )
        )
        written.append(
            write_json_artifact(
                paths.root / "weekly-summary.json",
                {
                    "week_key": week_key,
                    "as_of_date": ctx.as_of_date.isoformat(),
                    "run_id": ctx.run_id,
                    "week_start": ctx.scratch["week_start"].isoformat(),
                    "week_end": ctx.scratch["week_end"].isoformat(),
                    "trading_days": [
                        d.isoformat() for d in ctx.scratch["trading_days"]
                    ],
                    "coverage": audit,
                    "stats": stats,
                    "gate": ctx.scratch["gate"],
                    "archive": archive.to_dict(),
                    "research": ctx.scratch.get("weekly_research"),
                    "daily_runs": [r.to_dict() for r in records],
                },
            )
        )

        for p in written:
            ctx.add_artifact(p)
        return written

    def _write_report(self, ctx: PipelineContext) -> list[Path]:
        """写终态报告：run.json + weekly-report.md + latest-weekly.md。

        由运行编排器在终态确定后调用，确保报告反映真实终态而非 ``RUNNING``。
        """
        cfg = ctx.config
        week_key: str = ctx.scratch["week_key"]
        paths = result_paths(cfg, task_type=TaskType.WEEKLY, as_of_date=ctx.as_of_date)
        if ctx.dry_run:
            return []
        stats = ctx.scratch["stats"]
        collected: dict[str, Any] = ctx.scratch["collected"]
        review: dict[str, Any] = ctx.scratch["review"]
        audit: dict[str, Any] = ctx.scratch["audit"]
        archive: ArchiveResult = ctx.scratch["archive"]
        observation: list[dict[str, Any]] = ctx.scratch["observation"]
        accounts: list[SimulatedAccountState] = review["accounts"]
        records: list[RunRecord] = collected["records"]

        written: list[Path] = []
        written.append(write_json_artifact(paths.run_json, ctx.record.to_dict()))

        markdown = render_weekly_markdown(
            ctx.record,
            week_key=week_key,
            daily_runs=records,
            accounts=accounts,
            equity=review["equity"],
            observation=observation,
            stats=stats,
            archive=archive.summary(),
        )
        markdown = self._append_gap_section(markdown, audit)
        paths.report_md.write_text(markdown, encoding="utf-8")
        written.append(paths.report_md)
        paths.latest_md.parent.mkdir(parents=True, exist_ok=True)
        paths.latest_md.write_text(markdown, encoding="utf-8")
        written.append(paths.latest_md)

        for p in written:
            ctx.add_artifact(p)
        return written

    @staticmethod
    def _append_gap_section(markdown: str, audit: dict[str, Any]) -> str:
        """把跑批缺口追加成一节——缺口必须被人看见，不能只躺在 JSON 里。"""
        lines = ["", "## 跑批缺口", ""]
        if audit["coverage_ok"]:
            lines.append(
                f"本周 {audit['expected_trading_days']} 个交易日全部成功跑批，无缺口。"
            )
            lines.append("")
            return markdown.rstrip() + "\n" + "\n".join(lines)

        lines.append(
            f"**本周跑批存在缺口，覆盖率 {audit['coverage_pct']}%"
            f"（{audit['succeeded_days']}/{audit['expected_trading_days']}）。**"
        )
        lines.append("")
        if audit["missing_days"]:
            lines.append("缺失运行记录的交易日：")
            lines.append("")
            for day in audit["missing_days"]:
                lines.append(f"- `{day}`：无任何运行记录，建议 `automation rerun --date {day}`")
            lines.append("")
        if audit["unhealthy_days"]:
            lines += ["未成功收尾的交易日：", "", "| 业务日 | 终态 |", "| --- | --- |"]
            for item in audit["unhealthy_days"]:
                lines.append(f"| {item['date']} | `{item['state']}` |")
            lines.append("")
        lines.append(
            "> 缺口不会导致每周汇总失败——汇总的职责正是把缺口暴露出来。"
            "请人工确认原因后补跑。"
        )
        lines.append("")
        return markdown.rstrip() + "\n" + "\n".join(lines)

    def _summary_message(self, ctx: PipelineContext) -> str:
        audit: dict[str, Any] = ctx.scratch["audit"]
        collected: dict[str, Any] = ctx.scratch["collected"]
        archive: ArchiveResult = ctx.scratch["archive"]
        observation: list[dict[str, Any]] = ctx.scratch["observation"]
        days = max(
            (int(o.get("observed_trading_days", 0)) for o in observation), default=0
        )
        target = ctx.config.observation.target_trading_days
        filled = sum(
            1 for o in collected["orders"] if str(o.get("status")) == "FILLED"
        )
        gap = ""
        if not audit["coverage_ok"]:
            gap = (
                f"，跑批缺口 {len(audit['missing_days'])} 天"
                f"/异常 {len(audit['unhealthy_days'])} 天（需人工补跑）"
            )
        return (
            f"每周汇总完成：{ctx.scratch['week_key']} 覆盖 "
            f"{audit['succeeded_days']}/{audit['expected_trading_days']} 个交易日，"
            f"模拟成交 {filled} 笔，新增研究信号 {len(collected['signals'])} 条，"
            f"观察窗口 {days}/{target} 交易日，归档 {archive.archived_count} 个目录{gap}；"
            f"全部为模拟账户记录，未连接券商、未涉及真实资金"
        )


# ---------------------------------------------------------------------- #
# 便捷入口
# ---------------------------------------------------------------------- #


def run_weekly(
    config: AutomationConfig,
    *,
    as_of_date: date,
    pipeline: Optional[WeeklyPipeline] = None,
    state_store: Optional[StateStore] = None,
    force_retry: bool = False,
    dry_run: bool = False,
    now_fn: Callable[[], datetime] = datetime.now,
) -> RunOutcome:
    """执行一次每周自动化汇总。

    每周任务**不需要行情数据源**：它只读取每日运行留下的状态与产物。
    这是有意为之——数据源挂了的那一周，恰恰最需要一份周报把缺口说清楚。

    Args:
        config: 自动化配置。
        as_of_date: 业务日（通常是周六）。
        pipeline: 自定义管线（测试可注入日历）。
        state_store: 状态仓库；默认按配置创建。
        force_retry: 忽略已有 SUCCESS 记录强制重跑。
        dry_run: 只跑流程不落盘（不写产物、不动归档）。
        now_fn: 时钟注入。

    Returns:
        ``RunOutcome``，含运行记录、退出码与产物列表。
    """
    runner = AutomationRunner(
        config,
        task_type=TaskType.WEEKLY,
        state_store=state_store,
        now_fn=now_fn,
    )
    return runner.run(
        pipeline or WeeklyPipeline(),
        as_of_date=as_of_date,
        force_retry=force_retry,
        dry_run=dry_run,
    )
