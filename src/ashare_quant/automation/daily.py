"""Phase 4 每日自动化管线：10 步流水，从行情落到模拟账本。

流水线全景
----------
::

    1  preflight          资格与安全边界自检（实盘开关必须关闭）
    2  calendar           交易日历 fail-closed 加载 + 交易日判定
    3  market_data        行情获取（出处如实标注：线上/离线/合成）
    4  quality_gate       数据质量闸门（critical > 0 一律阻断）
    5  settle_pending     结算上一交易日挂起的信号（今日开盘撮合）
    6  universe           构建历史时点股票池（point-in-time）
    7  generate_signals   双轨策略生成今日收盘信号
    8  stage_pending      今日信号落盘为待成交（下一交易日开盘）
    9  mark_to_market     账户盯市 + 观察窗口推进 + 状态持久化
    10 artifacts          产物落盘（JSON + Markdown，附边界声明）

隔夜时序（本管线最容易被做错的地方）
------------------------------------
策略在 **D 日收盘后** 生成信号，最早在 **D+1 开盘** 成交。运行 D 日这一刻，
D+1 的行情根本不存在——所以本管线**绝不**就地假装成交，而是：

- 读取 ``pending-signals.json`` 里 **D-1 收盘**生成的信号，用 **D 日开盘价**撮合；
- 把 **D 日收盘**新生成的信号写回 ``pending-signals.json``，等 D+1 再撮合。

这条隔夜链路是"无未来函数"承诺在自动化场景下的具体落地。

交易日历为何可以"看到未来"
--------------------------
策略构造需要完整交易日列表（稳健轨据此判定"每周最后一个交易日"）。
交易日历是交易所**提前公开发布**的日程表，不是价格数据；
知道下周一开市与偷看下周一的收盘价，是两回事。
若只传截至今日的交易日，周三跑批就会把周三误判成"本周最后一个交易日"，
反而制造出错误信号——那才是真正的 bug。

边界声明
--------
本管线产出研究信号与**模拟**订单。稳健轨结论 ``NOT_ELIGIBLE_FOR_LIVE_TRADING``，
激进轨结论 ``SIMULATION_ONLY``，均**未获得实盘授权**。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

import pandas as pd

from ..backtest.config import BacktestConfig
from ..backtest.models import (
    BarData,
    Side,
    Signal,
    StrategyContext,
    to_decimal,
)
from ..config import AppConfig, load_config
from ..quality import QualityChecker, QualityReport
from ..research.strategies import (
    AggressiveParams,
    AggressiveStrategy,
    SteadyParams,
    SteadyStrategy,
)
from ..research.universe import HistoricalStatusTable, HistoricalUniverseFilter
from .calendar import TradingCalendar, load_trading_calendar
from .config import AccountConfig, AutomationConfig
from .datasource import (
    DataUnavailableError,
    DataUpdateFailedError,
    MarketDataBundle,
    MarketDataSource,
    lookback_start,
)
from .models import (
    DataQualityBlockedError,
    RunState,
    SimulatedAccountState,
    SimulatedOrderRecord,
    StrategyTrack,
    TaskType,
    signal_hash,
)
from .reporting import render_daily_markdown, result_paths, write_json_artifact
from .runner import AutomationRunner, PipelineContext, RunOutcome
from .simulated_account import (
    AccountUpdateResult,
    SimulatedAccountManager,
    assert_simulation_only,
    position_view,
)
from .state import StateStore, atomic_write_text
from .audit import write_audit_artifacts, write_manifest, verify_manifest
from .westock_hook import WestockHookResult, WestockValidationHook

__all__ = [
    "DAILY_STEPS",
    "PENDING_SCHEMA_VERSION",
    "StatefulAggressiveStrategy",
    "PendingSignal",
    "DailyPipeline",
    "build_bar_map",
    "run_daily",
]


#: 每日管线的 10 个步骤名（顺序即执行顺序，测试据此断言）。
DAILY_STEPS: tuple[str, ...] = (
    "preflight",
    "calendar",
    "market_data",
    "quality_gate",
    "settle_pending",
    "universe",
    "generate_signals",
    "stage_pending",
    "mark_to_market",
    "artifacts",
)

#: 待成交信号文件的 schema 版本。
PENDING_SCHEMA_VERSION = 1

#: 策略需要的最少行情列。
_REQUIRED_QUOTE_COLUMNS = (
    "symbol",
    "trade_date",
    "open_raw",
    "high_raw",
    "low_raw",
    "close_raw",
    "close_qfq",
)


def _data_update_audit(source: Any) -> Optional[dict[str, Any]]:
    """从数据源中提取本次数据更新的审计摘要（FR-20）。

    只有 :class:`~.data_update.AutoUpdatingDataSource` 会携带
    ``last_result``；其余数据源（本地 Parquet、注入式）返回 ``None``，
    管线照常运行——数据更新器是可选增强，不是硬依赖。
    """
    result = getattr(source, "last_result", None)
    if result is None:
        return None
    to_dict = getattr(result, "to_dict", None)
    if not callable(to_dict):
        return None
    try:
        return dict(to_dict())
    except Exception:  # noqa: BLE001 - 审计摘要失败不应拖垮当日运行
        return None


# ---------------------------------------------------------------------- #
# 待成交信号
# ---------------------------------------------------------------------- #


@dataclass
class PendingSignal:
    """一条等待下一交易日开盘撮合的研究信号。

    刻意**不含**任何券商下单字段（账户号、交易单元、委托类型码），
    这份 JSON 无法被任何交易终端直接导入——这是设计，不是遗漏。
    """

    track: StrategyTrack
    signal_date: date
    symbol: str
    side: str
    quantity: int
    reason: str = ""
    signal_hash: str = ""
    simulated: bool = True

    def __post_init__(self) -> None:
        if not self.signal_hash:
            self.signal_hash = signal_hash(
                symbol=self.symbol,
                side=self.side,
                quantity=int(self.quantity),
                reason=self.reason or "",
                signal_date=self.signal_date,
                strategy_track=self.track.value,
            )

    # ------------------------------------------------------------------ #
    @classmethod
    def from_signal(cls, sig: Signal, track: StrategyTrack) -> "PendingSignal":
        side = sig.side.value if isinstance(sig.side, Side) else str(sig.side)
        return cls(
            track=track,
            signal_date=sig.signal_date,
            symbol=sig.symbol,
            side=side,
            quantity=int(sig.quantity),
            reason=sig.reason or "",
        )

    def to_signal(self) -> Signal:
        return Signal(
            signal_date=self.signal_date,
            symbol=self.symbol,
            side=Side(self.side),
            quantity=int(self.quantity),
            reason=self.reason,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "track": self.track.value,
            "signal_date": self.signal_date.isoformat(),
            "symbol": self.symbol,
            "side": self.side,
            "quantity": int(self.quantity),
            "reason": self.reason,
            "signal_hash": self.signal_hash,
            "simulated": True,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "PendingSignal":
        return cls(
            track=StrategyTrack(raw["track"]),
            signal_date=date.fromisoformat(str(raw["signal_date"])),
            symbol=str(raw["symbol"]),
            side=str(raw["side"]),
            quantity=int(raw["quantity"]),
            reason=str(raw.get("reason", "")),
            signal_hash=str(raw.get("signal_hash", "")),
        )


# ---------------------------------------------------------------------- #
# 可续跑的激进轨策略
# ---------------------------------------------------------------------- #


class StatefulAggressiveStrategy(AggressiveStrategy):
    """在**跨进程日频运行**之间保持持仓计数的激进轨策略。

    Phase 3 的 ``AggressiveStrategy`` 假定自己活在一次连续回测里，
    ``_holding_days`` 靠每日累加。搬到每天起一次新进程的自动化场景下，
    计数器会天天归零，"持有满 N 日"这条退出规则将**永远不会触发**——
    表面跑得好好的，实际上少了一条风控。

    本子类把这份状态导出/导入，让计数在进程之间接得上。
    只做状态搬运，不改任何策略逻辑。
    """

    def export_state(self) -> dict[str, Any]:
        return {
            "holding_symbol": self._holding_symbol,
            "entry_date": self._entry_date.isoformat() if self._entry_date else None,
            "holding_days": int(self._holding_days),
        }

    def restore_state(self, blob: Optional[dict[str, Any]]) -> None:
        if not blob:
            return
        symbol = blob.get("holding_symbol")
        self._holding_symbol = str(symbol) if symbol else None
        raw_entry = blob.get("entry_date")
        self._entry_date = date.fromisoformat(str(raw_entry)) if raw_entry else None
        try:
            self._holding_days = int(blob.get("holding_days", 0) or 0)
        except (TypeError, ValueError):
            self._holding_days = 0


# ---------------------------------------------------------------------- #
# 行情工具
# ---------------------------------------------------------------------- #


def _dec(value: Any, fallback: str = "0") -> Decimal:
    """稳健地转 ``Decimal``；NaN / None 一律退回 fallback。"""
    if value is None:
        return to_decimal(fallback)
    if isinstance(value, float) and math.isnan(value):
        return to_decimal(fallback)
    try:
        return to_decimal(value)
    except Exception:  # noqa: BLE001 - 脏数据不应炸掉整条流水线
        return to_decimal(fallback)


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, float) and math.isnan(value):
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "t"}
    return bool(value)


def build_bar_map(quotes: pd.DataFrame, target_date: date) -> dict[str, BarData]:
    """把 ``target_date`` 当日行情整理为 ``{symbol: BarData}``。

    ``prev_close_raw`` 取同一标的在 ``target_date`` 之前最近一个交易日的
    未复权收盘价——涨跌停判定全靠它，缺了这一列撮合就会失真。
    """
    if quotes is None or len(quotes) == 0:
        return {}
    df = quotes.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    df = df[df["trade_date"] <= target_date].sort_values(["symbol", "trade_date"])

    bars: dict[str, BarData] = {}
    for symbol, group in df.groupby("symbol", sort=True):
        rows = group.to_dict("records")
        if not rows:
            continue
        last = rows[-1]
        if last["trade_date"] != target_date:
            # 当日无该标的行情（停牌未出数据 / 未上市），不构造 bar
            continue
        prev_close = None
        if len(rows) >= 2:
            prev_close = _dec(rows[-2].get("close_raw"))
        close_raw = _dec(last.get("close_raw"))
        bars[str(symbol)] = BarData(
            symbol=str(symbol),
            trade_date=target_date,
            open_raw=_dec(last.get("open_raw"), str(close_raw)),
            high_raw=_dec(last.get("high_raw"), str(close_raw)),
            low_raw=_dec(last.get("low_raw"), str(close_raw)),
            close_raw=close_raw,
            open_qfq=_dec(last.get("open_qfq"), str(close_raw)),
            high_qfq=_dec(last.get("high_qfq"), str(close_raw)),
            low_qfq=_dec(last.get("low_qfq"), str(close_raw)),
            close_qfq=_dec(last.get("close_qfq"), str(close_raw)),
            volume=float(last.get("volume") or 0.0),
            amount=float(last.get("amount") or last.get("turnover") or 0.0),
            is_suspended=_as_bool(last.get("is_suspended"), False),
            is_tradable=_as_bool(last.get("is_tradable"), True),
            prev_close_raw=prev_close,
        )
    return bars


def _benchmark_map(bundle: MarketDataBundle) -> dict[date, float]:
    """把基准行情整理为 ``{date: close}``（激进轨相对强度用）。"""
    df = bundle.benchmark
    if df is None or len(df) == 0:
        return {}
    work = df.copy()
    if "trade_date" not in work.columns:
        return {}
    work["trade_date"] = pd.to_datetime(work["trade_date"]).dt.date
    price_col = next(
        (c for c in ("close_qfq", "close_raw", "close") if c in work.columns), None
    )
    if price_col is None:
        return {}
    work = work.dropna(subset=[price_col])
    return {
        row["trade_date"]: float(row[price_col])
        for _, row in work.iterrows()
    }


def _status_table(bundle: MarketDataBundle) -> HistoricalStatusTable:
    """构造历史时点状态表；缺证券主数据时退化为"全部非 ST"的最小表。

    退化不是掩盖——``security_master_available=False`` 会写进报告出处，
    读者一眼能看出这次运行的股票池过滤强度打了折。
    """
    master = bundle.security_master
    if master is not None and len(master) > 0:
        cols = set(master.columns)
        if {"symbol", "st_status"} <= cols and (
            "status_valid_from" in cols or "date" in cols
        ):
            return HistoricalStatusTable(master.copy())

    symbols = bundle.symbols
    start, _end = bundle.date_range
    fallback = pd.DataFrame(
        {
            "symbol": symbols,
            "st_status": [False] * len(symbols),
            "status_valid_from": [start or date(1990, 1, 1)] * len(symbols),
            "status_valid_to": [None] * len(symbols),
            "list_date": [start or date(1990, 1, 1)] * len(symbols),
            "delist_date": [None] * len(symbols),
        }
    )
    return HistoricalStatusTable(fallback)


# ---------------------------------------------------------------------- #
# 管线
# ---------------------------------------------------------------------- #


@dataclass
class DailyPipeline:
    """每日管线：作为 ``Pipeline`` 回调交给 ``AutomationRunner`` 执行。

    Args:
        backtest_config: 撮合/费用/风控参数（复用 Phase 2 配置）。
        app_config: Phase 1 配置（质量检查阈值）；为 None 时按仓库默认加载。
        calendar: 注入的交易日历（测试用）；为 None 时按配置 fail-closed 加载。
        steady_params / aggressive_params: 双轨策略参数。
        universe_kwargs: 透传给 ``HistoricalUniverseFilter`` 的可选参数。
    """

    backtest_config: BacktestConfig = field(default_factory=BacktestConfig)
    app_config: Optional[AppConfig] = None
    calendar: Optional[TradingCalendar] = None
    steady_params: SteadyParams = field(default_factory=SteadyParams)
    aggressive_params: AggressiveParams = field(default_factory=AggressiveParams)
    universe_kwargs: dict[str, Any] = field(default_factory=dict)
    # 旁路核验 hook（严格旁路：失败只告警，不改变主流程成功状态）
    westock_hook: Optional["WestockValidationHook"] = None

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
                    "eligibility": {
                        a.account_id: a.eligibility_status.value for a in accounts_cfg
                    },
                }
            )

        # -- 2. 交易日历 ------------------------------------------------ #
        with ctx.step("calendar") as step:
            cal = load_trading_calendar(cfg, as_of=as_of, calendar=self.calendar)
            ctx.scratch["calendar"] = cal
            step.detail.update(cal.summary())
            if not cal.is_trading_day(as_of):
                step.detail["is_trading_day"] = False
                ctx.skip_non_trading_day(
                    f"{as_of.isoformat()} 不是交易日，跳过本次每日运行",
                    as_of_date=as_of.isoformat(),
                    calendar_source=cal.source,
                )
            step.detail["is_trading_day"] = True
            prev_day = cal.previous_trading_day(as_of)
            next_day: Optional[date]
            try:
                next_day = cal.next_trading_day(as_of)
            except Exception:  # noqa: BLE001 - 日历尾部越界不应中断当日运行
                next_day = None
            ctx.scratch["prev_trading_day"] = prev_day
            ctx.scratch["next_trading_day"] = next_day
            step.detail["previous_trading_day"] = prev_day.isoformat()
            step.detail["next_trading_day"] = (
                next_day.isoformat() if next_day else None
            )

        # -- 3. 行情获取 ------------------------------------------------ #
        with ctx.step("market_data") as step:
            source = ctx.data_source
            if source is None:
                raise DataUnavailableError(
                    "未提供行情数据源；本机自动化不做隐式联网抓取，"
                    "请显式注入 MarketDataSource 或配置本地 curated 数据"
                )
            start = lookback_start(as_of, cfg.data.lookback_days)
            try:
                bundle = source.load(
                    symbols=list(cfg.data.symbols),
                    start=start,
                    end=as_of,
                    as_of=as_of,
                )
            except DataUnavailableError as exc:
                # FR-20：data.allow_skip_when_unavailable 决定"缺数据"的性质。
                # 为 true 时是可接受的跳过（退出码 0）；为 false 时运维要求
                # 每个交易日都必须有数据，缺数据就是事故，必须落 FAILED（退出码 1），
                # 否则调度器与告警看到 0 会以为一切正常。
                if not cfg.data.allow_skip_when_unavailable:
                    step.detail["allow_skip_when_unavailable"] = False
                    raise DataUpdateFailedError(
                        f"数据不可用且 data.allow_skip_when_unavailable=false，"
                        f"按 fail-closed 判为失败而非跳过：{exc}"
                    ) from exc
                step.detail["allow_skip_when_unavailable"] = True
                raise
            missing = [
                c for c in _REQUIRED_QUOTE_COLUMNS if c not in bundle.quotes.columns
            ]
            if missing:
                raise DataUnavailableError(
                    f"行情缺少必需列 {missing}（来源: {bundle.source}）"
                )
            ctx.scratch["bundle"] = bundle
            update_audit = _data_update_audit(source)
            if update_audit is not None:
                ctx.scratch["data_update"] = update_audit
                step.detail["data_update"] = {
                    k: v for k, v in update_audit.items() if k != "outcomes"
                }
            provenance = bundle.provenance()
            provenance["security_master_available"] = (
                bundle.security_master is not None and len(bundle.security_master) > 0
            )
            provenance["benchmark_available"] = (
                bundle.benchmark is not None and len(bundle.benchmark) > 0
            )
            ctx.scratch["provenance"] = provenance
            step.detail.update(provenance)
            ctx.logger.info(
                "data_provenance",
                "行情出处：online=%s synthetic=%s source=%s"
                % (bundle.online, bundle.synthetic, bundle.source),
                **provenance,
            )

        # -- 4. 质量闸门 ------------------------------------------------ #
        with ctx.step("quality_gate") as step:
            bundle = ctx.scratch["bundle"]
            report = self._run_quality(cfg, bundle)
            ctx.scratch["quality"] = report
            counts = report.counts()
            step.detail.update(counts)
            step.detail["has_critical"] = report.has_critical
            if report.has_critical and cfg.quality.block_on_critical:
                raise DataQualityBlockedError(
                    f"数据质量闸门未通过：critical={counts['critical']}，"
                    f"warning={counts['warning']}；"
                    f"按 fail-closed 约定阻断下游，且**不允许**复用昨日数据"
                )
            max_warn = cfg.quality.max_warning
            if max_warn and counts["warning"] > max_warn:
                raise DataQualityBlockedError(
                    f"warning 数量 {counts['warning']} 超过阈值 {max_warn}，阻断下游"
                )

        # -- 4.5 westock 旁路核验（严格旁路） ---------------------------- #
        # 只做未复权交叉核验与告警报告，任何失败都不改变主流程成功状态。
        if self.westock_hook is not None and cfg.validators.westock_enabled:
            with ctx.step("westock_validation") as step:
                hook_result = self._run_westock_hook(ctx, as_of)
                ctx.scratch["westock_validation"] = hook_result
                step.detail.update(hook_result.to_dict())
        else:
            ctx.logger.info(
                "westock_validation",
                "westock 旁路核验未启用（未注入 hook 或 validators.enabled 不含 westock）",
            )

        # -- 账户与账房先生 --------------------------------------------- #
        manager = SimulatedAccountManager(cfg, self.backtest_config)
        accounts: dict[StrategyTrack, SimulatedAccountState] = {}
        for acc_cfg in cfg.accounts:
            existing = ctx.state_store.load_account(acc_cfg.account_id)
            accounts[acc_cfg.track] = manager.ensure_account(acc_cfg, existing)
        ctx.scratch["accounts"] = accounts
        ctx.scratch["manager"] = manager

        bundle = ctx.scratch["bundle"]
        bars_today = build_bar_map(bundle.quotes, as_of)
        ctx.scratch["bars_today"] = bars_today

        # -- 5. 结算昨日挂起信号 ---------------------------------------- #
        with ctx.step("settle_pending") as step:
            pending, pending_meta = self._load_pending(ctx)
            results: dict[StrategyTrack, AccountUpdateResult] = {}
            order_records: list[SimulatedOrderRecord] = []
            for track, state in accounts.items():
                track_signals = [p.to_signal() for p in pending if p.track is track]
                result = manager.apply_signals(
                    state,
                    signals=track_signals,
                    bars=bars_today,
                    fill_date=as_of,
                    run_id=ctx.run_id,
                    count_observation_day=True,
                )
                results[track] = result
                order_records.extend(result.orders)
            ctx.scratch["settle_results"] = results
            ctx.scratch["orders"] = order_records
            step.detail.update(
                {
                    "pending_signal_date": pending_meta.get("signal_date"),
                    "pending_count": len(pending),
                    "filled": sum(len(r.filled) for r in results.values()),
                    "rejected": sum(len(r.rejected) for r in results.values()),
                    "duplicates": sum(len(r.duplicates) for r in results.values()),
                    "fill_date": as_of.isoformat(),
                }
            )

        # -- 6. 股票池 -------------------------------------------------- #
        with ctx.step("universe") as step:
            status_table = _status_table(bundle)
            ctx.scratch["status_table"] = status_table
            step.detail.update(
                {
                    "symbols": len(bundle.symbols),
                    "security_master_available": bool(
                        bundle.security_master is not None
                        and len(bundle.security_master) > 0
                    ),
                    "filter_kwargs": {
                        k: v for k, v in self.universe_kwargs.items() if k != "quotes"
                    },
                }
            )

        # -- 7. 生成今日信号 -------------------------------------------- #
        with ctx.step("generate_signals") as step:
            new_signals = self._generate_signals(ctx, manager, accounts, bars_today)
            ctx.scratch["new_signals"] = new_signals
            step.detail.update(
                {
                    "signal_date": as_of.isoformat(),
                    "fill_date": (
                        ctx.scratch["next_trading_day"].isoformat()
                        if ctx.scratch.get("next_trading_day")
                        else None
                    ),
                    "count": len(new_signals),
                    "by_track": {
                        t.value: sum(1 for s in new_signals if s.track is t)
                        for t in accounts
                    },
                }
            )

        # -- 8. 落盘为待成交 -------------------------------------------- #
        with ctx.step("stage_pending") as step:
            payload = {
                "schema_version": PENDING_SCHEMA_VERSION,
                "generated_run_id": ctx.run_id,
                "signal_date": as_of.isoformat(),
                "fill_date": (
                    ctx.scratch["next_trading_day"].isoformat()
                    if ctx.scratch.get("next_trading_day")
                    else None
                ),
                "simulated": True,
                "live_trading": False,
                "note": (
                    "研究信号，等待下一交易日开盘模拟撮合；"
                    "不含任何券商下单字段，无法直接导入交易终端"
                ),
                "strategy_state": ctx.scratch.get("strategy_state", {}),
                "signals": [s.to_dict() for s in ctx.scratch["new_signals"]],
            }
            if not ctx.dry_run:
                ctx.state_store.save_pending_signals(payload)
            ctx.scratch["pending_payload"] = payload
            step.detail.update(
                {
                    "written": not ctx.dry_run,
                    "count": len(payload["signals"]),
                    "path": ctx.state_store.pending_signals_path().name,
                }
            )

        # -- 9. 盯市 + 观察窗口 ----------------------------------------- #
        with ctx.step("mark_to_market") as step:
            prices = manager.close_prices(bars_today)
            equity: dict[str, Any] = {}
            observation: list[dict[str, Any]] = []
            for track, state in accounts.items():
                manager.mark_to_market(
                    state,
                    as_of_date=as_of,
                    prices=prices,
                    run_id=ctx.run_id,
                    count_observation_day=True,
                )
                equity[state.account_id] = {
                    "cash": str(state.cash),
                    "position_value": str(state.position_value(prices)),
                    "total_equity": str(state.total_equity(prices)),
                    "positions": len(state.positions),
                    "eligibility_status": state.eligibility_status.value,
                }
                observation.append(manager.observation_progress(state))
                if not ctx.dry_run:
                    ctx.state_store.save_account(state)
            ctx.scratch["equity"] = equity
            ctx.scratch["observation"] = observation
            step.detail.update(
                {
                    "priced_symbols": len(prices),
                    "accounts": len(accounts),
                    "persisted": not ctx.dry_run,
                    "observation": {
                        o.get("account_id", ""): o.get("observed_trading_days")
                        for o in observation
                    },
                }
            )

        # -- 10. 数据产物 ------------------------------------------------ #
        # 只写"与终态无关"的数据产物（信号/订单/账户/质量）。
        # 终态报告（run.json + Markdown）延迟到运行编排器设置终态之后再渲染，
        # 否则报告里会出现 "运行中（非终态）"（缺陷 #2）。
        with ctx.step("artifacts") as step:
            written = self._write_data_artifacts(ctx, accounts)
            step.detail.update(
                {"files": [p.name for p in written], "dry_run": ctx.dry_run}
            )

        ctx.scratch["_finalize_report"] = lambda: self._write_report(ctx, accounts)

        ctx.record.message = self._summary_message(ctx, accounts)

    # ------------------------------------------------------------------ #
    # 步骤实现
    # ------------------------------------------------------------------ #

    def _run_quality(
        self, cfg: AutomationConfig, bundle: MarketDataBundle
    ) -> QualityReport:
        app_cfg = self.app_config
        if app_cfg is None:
            app_cfg = load_config(cfg.base_dir / "config" / "default.yaml")
        checker = QualityChecker(app_cfg)
        return checker.run(
            bundle.quotes,
            security_master=bundle.security_master,
            trade_calendar=bundle.calendar_df,
        )

    def _run_westock_hook(
        self, ctx: PipelineContext, as_of: date
    ) -> "WestockHookResult":
        """执行 westock 旁路核验（严格旁路，绝不阻断主流程）。

        数据源不可用 / 无注入 fetcher / 无匹配数据时一律降级为
        unavailable / no_data 并记录，不影响主流程退出码。
        """
        hook = self.westock_hook
        if hook is None:
            return WestockHookResult(
                status="skipped", message="未注入 westock hook"
            )
        bundle = ctx.scratch.get("bundle")
        if bundle is None or bundle.quotes is None or bundle.quotes.empty:
            return WestockHookResult(
                status="no_data", message="主源行情为空，跳过 westock 核验"
            )
        quotes = bundle.quotes
        # 取第一个 symbol 做核验样本（当前为轻量旁路；全池核验留待正式 provider）
        symbol = quotes["symbol"].dropna().iloc[0]
        start = as_of  # 与每日增量对齐：只核验当日数据
        end = as_of
        calendar_df = getattr(bundle, "calendar_df", None)
        return hook.run(
            quotes,
            symbol=str(symbol),
            start=start,
            end=end,
            as_of=as_of,
            calendar=calendar_df,
        )

    def _load_pending(
        self, ctx: PipelineContext
    ) -> tuple[list[PendingSignal], dict[str, Any]]:
        """读取待成交信号，并丢弃"信号日不早于今日"的条目。

        信号必须**先于**成交日产生。若文件里的 signal_date >= 今日，
        说明是同日重跑残留或时钟错乱——宁可不成交，也不能让今日信号今日成交，
        那等于凭空多出一天的先知。
        """
        raw = ctx.state_store.load_pending_signals()
        if not raw:
            return [], {}
        items = raw.get("signals") or []
        parsed: list[PendingSignal] = []
        dropped = 0
        for item in items:
            try:
                ps = PendingSignal.from_dict(item)
            except (KeyError, ValueError, TypeError):
                dropped += 1
                continue
            if ps.signal_date >= ctx.as_of_date:
                dropped += 1
                continue
            parsed.append(ps)
        if dropped:
            ctx.logger.warning(
                "pending_signals_dropped",
                f"丢弃 {dropped} 条不可用的待成交信号（信号日不早于业务日或格式损坏）",
                dropped=dropped,
                as_of_date=ctx.as_of_date.isoformat(),
            )
        meta = {
            "signal_date": raw.get("signal_date"),
            "generated_run_id": raw.get("generated_run_id"),
            "dropped": dropped,
        }
        ctx.scratch["prev_strategy_state"] = raw.get("strategy_state") or {}
        return parsed, meta

    def _generate_signals(
        self,
        ctx: PipelineContext,
        manager: SimulatedAccountManager,
        accounts: dict[StrategyTrack, SimulatedAccountState],
        bars_today: dict[str, BarData],
    ) -> list[PendingSignal]:
        """运行双轨策略，产出今日收盘信号。"""
        cfg = ctx.config
        bundle: MarketDataBundle = ctx.scratch["bundle"]
        cal: TradingCalendar = ctx.scratch["calendar"]
        as_of = ctx.as_of_date
        status_table: HistoricalStatusTable = ctx.scratch["status_table"]

        quotes = bundle.quotes.copy()
        quotes["trade_date"] = pd.to_datetime(quotes["trade_date"]).dt.date
        quotes = quotes[quotes["trade_date"] <= as_of].sort_values(
            ["symbol", "trade_date"]
        )

        # 完整交易日历（含未来日程）——见模块文档"为何可以看到未来"
        trading_dates = list(cal.dates)
        prices = manager.close_prices(bars_today)
        benchmark = _benchmark_map(bundle)

        prev_state = ctx.scratch.get("prev_strategy_state") or {}
        strategy_state: dict[str, Any] = {}
        collected: list[PendingSignal] = []

        for track, state in accounts.items():
            positions = position_view(state)
            portfolio = manager.snapshot(state, as_of, prices)
            universe = HistoricalUniverseFilter(
                status_table,
                lot_size=self.backtest_config.lot_size,
                **self.universe_kwargs,
            )
            context = StrategyContext(
                current_date=as_of,
                portfolio=portfolio,
                positions=positions,
                bars_up_to_date=quotes,
            )

            if track is StrategyTrack.STEADY:
                strategy: Any = SteadyStrategy(
                    self.steady_params,
                    universe,
                    trading_dates,
                    lot_size=self.backtest_config.lot_size,
                )
                signals = strategy.on_close(context)
            else:
                aggressive = StatefulAggressiveStrategy(
                    self.aggressive_params,
                    universe,
                    trading_dates,
                    lot_size=self.backtest_config.lot_size,
                    benchmark_hs300=benchmark,
                )
                aggressive.restore_state(prev_state.get(track.value))
                signals = aggressive.on_close(context)
                strategy_state[track.value] = aggressive.export_state()

            for sig in signals:
                collected.append(PendingSignal.from_signal(sig, track))
            ctx.logger.info(
                "signals_generated",
                f"{track.value} 轨生成 {len(signals)} 条研究信号（模拟，非交易指令）",
                track=track.value,
                count=len(signals),
                signal_date=as_of.isoformat(),
            )

        ctx.scratch["strategy_state"] = strategy_state
        return collected

    def _write_data_artifacts(
        self,
        ctx: PipelineContext,
        accounts: dict[StrategyTrack, SimulatedAccountState],
    ) -> list[Path]:
        """写"与终态无关"的数据产物：信号 / 订单 / 账户 / 质量。

        终态报告（run.json + Markdown）不在这里写——它由运行编排器在设置
        终态之后通过 ``_write_report`` 落盘，见 ``runner.py``。
        """
        cfg = ctx.config
        paths = result_paths(cfg, task_type=TaskType.DAILY, as_of_date=ctx.as_of_date)
        if ctx.dry_run:
            return []
        paths.ensure()

        quality: QualityReport = ctx.scratch["quality"]
        orders: list[SimulatedOrderRecord] = ctx.scratch.get("orders", [])
        new_signals: list[PendingSignal] = ctx.scratch.get("new_signals", [])
        equity: dict[str, Any] = ctx.scratch.get("equity", {})
        observation: list[dict[str, Any]] = ctx.scratch.get("observation", [])
        account_list = list(accounts.values())

        written: list[Path] = []

        written.append(
            write_json_artifact(
                paths.signals_json,
                {
                    "as_of_date": ctx.as_of_date.isoformat(),
                    "run_id": ctx.run_id,
                    "simulated": True,
                    "live_trading": False,
                    "note": (
                        "研究信号，非投资建议、非交易指令；"
                        "下一交易日开盘由模拟账户撮合"
                    ),
                    "signals": [s.to_dict() for s in new_signals],
                },
            )
        )
        written.append(
            write_json_artifact(
                paths.orders_json,
                {
                    "as_of_date": ctx.as_of_date.isoformat(),
                    "run_id": ctx.run_id,
                    "simulated": True,
                    "orders": [o.to_dict() for o in orders],
                },
            )
        )
        written.append(
            write_json_artifact(
                paths.accounts_json,
                {
                    "as_of_date": ctx.as_of_date.isoformat(),
                    "run_id": ctx.run_id,
                    "equity": equity,
                    "observation": observation,
                    "accounts": [a.to_dict() for a in account_list],
                },
            )
        )
        written.append(write_json_artifact(paths.quality_json, quality.to_dict()))

        for p in written:
            ctx.add_artifact(p)
        return written

    def _write_report(
        self,
        ctx: PipelineContext,
        accounts: dict[StrategyTrack, SimulatedAccountState],
    ) -> list[Path]:
        """写终态报告：run.json + daily-report.md + latest-daily.md。

        由运行编排器在终态确定后调用，确保报告反映真实终态
        （SUCCESS/FAILED/...）而非运行中的 ``RUNNING``（缺陷 #2）。
        """
        cfg = ctx.config
        paths = result_paths(cfg, task_type=TaskType.DAILY, as_of_date=ctx.as_of_date)
        if ctx.dry_run:
            return []
        # 数据产物已创建 root 目录；这里补齐 run.json 与报告。
        quality: Optional[QualityReport] = ctx.scratch.get("quality")
        orders: list[SimulatedOrderRecord] = ctx.scratch.get("orders", [])
        new_signals: list[PendingSignal] = ctx.scratch.get("new_signals", [])
        provenance: dict[str, Any] = ctx.scratch.get("provenance", {})
        equity: dict[str, Any] = ctx.scratch.get("equity", {})
        observation: list[dict[str, Any]] = ctx.scratch.get("observation", [])
        account_list = list(accounts.values())
        generated_at = ctx.now_fn().isoformat(timespec="seconds")

        written: list[Path] = []

        # FR-20：数据更新链路的完整审计明细（含每标的重试/回退/SHA-256/清单）。
        # 仅在真的跑过数据更新器时生成，本地消费模式不产生空壳文件。
        data_update: Optional[dict[str, Any]] = ctx.scratch.get("data_update")
        if data_update:
            written.append(
                write_json_artifact(paths.root / "data-update.json", data_update)
            )

        markdown = render_daily_markdown(
            ctx.record,
            provenance=provenance,
            quality=quality.to_dict() if quality is not None else {},
            signals=[s.to_dict() for s in new_signals],
            orders=orders,
            accounts=account_list,
            equity=equity,
            observation=observation,
        )
        paths.report_md.write_text(markdown, encoding="utf-8")
        written.append(paths.report_md)

        # FR-23：审计产物（Parquet 固定列序 / run-summary / 快照 / 规范名报告）。
        # 不在此写 run.json 与 manifest —— 二者需要在全部产物落盘后编排。
        audit_written = write_audit_artifacts(
            record=ctx.record,
            config=cfg,
            task_type=TaskType.DAILY,
            paths=paths,
            markdown=markdown,
            accounts=account_list,
            orders=orders,
            signals=new_signals,
            quality=quality.to_dict() if quality is not None else {},
            equity=equity,
            observation=observation,
            generated_at=generated_at,
        )
        written.extend(audit_written)

        for p in written:
            ctx.add_artifact(p)

        # run.json 的 artifacts 必须包含数据产物、报告与 manifest（FR-23）。
        try:
            run_json_rel = str(
                paths.run_json.resolve().relative_to(cfg.base_dir)
            ).replace("\\", "/")
        except ValueError:
            run_json_rel = str(paths.run_json).replace("\\", "/")
        if run_json_rel not in ctx.record.artifacts:
            ctx.record.artifacts.append(run_json_rel)
        try:
            manifest_rel = str(
                (paths.root / "manifest.json").resolve().relative_to(cfg.base_dir)
            ).replace("\\", "/")
        except ValueError:
            manifest_rel = str(paths.root / "manifest.json").replace("\\", "/")
        if manifest_rel not in ctx.record.artifacts:
            ctx.record.artifacts.append(manifest_rel)
        written.append(write_json_artifact(paths.run_json, ctx.record.to_dict()))
        ctx.add_artifact(paths.run_json)

        # manifest：在全部产物（含 run.json / 报告）落盘之后写，逐文件 SHA-256。
        manifest_path = write_manifest(
            record=ctx.record,
            config=cfg,
            run_dir=paths.root,
            generated_at=generated_at,
        )
        written.append(manifest_path)
        ctx.add_artifact(manifest_path)

        # latest：全部文件写完并校验后原子更新；失败运行不得指向半成品。
        if ctx.record.state is RunState.SUCCESS:
            verify_manifest(manifest_path, config=cfg)
            paths.latest_md.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(paths.latest_md, markdown)
            written.append(paths.latest_md)
            ctx.add_artifact(paths.latest_md)

        return written

    def _summary_message(
        self,
        ctx: PipelineContext,
        accounts: dict[StrategyTrack, SimulatedAccountState],
    ) -> str:
        orders = ctx.scratch.get("orders", [])
        filled = sum(1 for o in orders if o.status == "FILLED")
        new_signals = ctx.scratch.get("new_signals", [])
        obs = ctx.scratch.get("observation", [])
        days = max((o.get("observed_trading_days", 0) for o in obs), default=0)
        target = ctx.config.observation.target_trading_days
        return (
            f"每日模拟运行完成：撮合 {filled} 笔（共 {len(orders)} 条订单记录），"
            f"新增研究信号 {len(new_signals)} 条，观察窗口 {days}/{target} 交易日；"
            f"全部为模拟账户记录，未连接券商、未涉及真实资金"
        )


# ---------------------------------------------------------------------- #
# 便捷入口
# ---------------------------------------------------------------------- #


def run_daily(
    config: AutomationConfig,
    *,
    as_of_date: date,
    data_source: Optional[MarketDataSource] = None,
    pipeline: Optional[DailyPipeline] = None,
    state_store: Optional[StateStore] = None,
    force_retry: bool = False,
    dry_run: bool = False,
    now_fn: Callable[[], datetime] = datetime.now,
) -> RunOutcome:
    """执行一次每日自动化运行。

    Args:
        config: 自动化配置。
        as_of_date: 业务日（通常是运行当天）。
        data_source: 行情数据源；离线场景请显式注入。
        pipeline: 自定义管线（测试可注入日历与参数）。
        state_store: 状态仓库；默认按配置创建。
        force_retry: 忽略已有 SUCCESS 记录强制重跑。
        dry_run: 只跑流程不落盘（产物与账户状态均不写）。
        now_fn: 时钟注入。

    Returns:
        ``RunOutcome``，含运行记录、退出码与产物列表。
    """
    runner = AutomationRunner(
        config,
        task_type=TaskType.DAILY,
        data_source=data_source,
        state_store=state_store,
        now_fn=now_fn,
    )
    return runner.run(
        pipeline or DailyPipeline(),
        as_of_date=as_of_date,
        force_retry=force_retry,
        dry_run=dry_run,
    )
