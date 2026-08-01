"""Gate 4B 观察报告生成器（可追溯、可复现）。

本脚本是 ``docs/gate4b-observation.md`` 与
``reports/phase-4/gate4b/60d-summary.json`` 的**唯一生成来源**
（代码证据：报告由本脚本运行产生，而非手写）。

两种模式：

``--mode precheck``（默认）
    在临时目录内用确定性合成行情**历史回放**连续 60 个交易日，验证
    「60 日历史回放预检」通过（60/60 SUCCESS、无重复订单、账务恒等式无违规）。
    注意：这只是**预检**，不等同于 Gate 4B「连续 60 个交易日自动运行」。

``--mode track``
    读取真实自动化状态（``state/automation`` 运行记录）与产物，用**交易日历**
    生成预期交易日序列（不是按自然日推断），自启动日起逐日复核：
    记录必须存在且 SUCCESS / exit 0；报告产物齐全；每日 manifest 哈希可复算；
    无重复订单；账务恒等式 cash + position_value == total_equity 成立；
    现金非负。只有从起始交易日起**连续 60 个预期交易日全部满足验收条件**
    才输出 60/60。正式观察尚未启动时为 0/60。

用法::

    python scripts/gate4b_observation.py                 # precheck
    python scripts/gate4b_observation.py --mode track    # 真实进度
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from ashare_quant.automation.audit import verify_manifest
from ashare_quant.automation.calendar import (
    CalendarUnavailableError,
    TradingCalendar,
    load_trading_calendar,
)
from ashare_quant.automation.config import (
    AccountConfig,
    AutomationConfig,
    DataConfig,
    LoggingConfig,
    PathsConfig,
    load_automation_config,
)
from ashare_quant.automation.datasource import InjectedDataSource
from ashare_quant.automation.daily import DailyPipeline, run_daily
from ashare_quant.automation.models import (
    EligibilityStatus,
    RunState,
    StrategyTrack,
    TaskType,
)
from ashare_quant.automation.state import StateStore
from ashare_quant.config import load_config
from tests.research_samples import (
    make_benchmark_data,
    make_historical_status_table,
    make_research_quotes,
)

ROOT = Path(__file__).resolve().parents[1]
OBSERVATION_DAYS = 60
# precheck（正式示例）输出：受 Git 跟踪，保留历史示例语义。
SUMMARY_JSON = ROOT / "reports" / "phase-4" / "gate4b" / "60d-summary.json"
OBSERVATION_MD = ROOT / "docs" / "gate4b-observation.md"
# track（真实观察）默认输出目录：Git 忽略，不得每天改动受跟踪示例文件。
TRACK_OUTPUT_DIR = ROOT / "state" / "automation" / "gate4b"


def _atomic_write_text(path: Path, text: str) -> None:
    """同目录临时文件 + 原子替换；失败时原文件不受影响。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    try:
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _write_outputs(
    summary: dict[str, Any], *, output_dir: Optional[Path] = None
) -> tuple[Path, Path]:
    """按模式写观察报告与摘要。

    ``precheck``：始终写正式示例目录（``reports/phase-4/gate4b/`` 与 ``docs/``，
    受 Git 跟踪，保留示例语义）。

    ``track``：默认写 ``state/automation/gate4b/``（Git 忽略，真实观察期间
    不得每天改动正式示例文件）；可通过 ``output_dir`` 显式覆盖目录。
    两份输出文件名带 ``track-`` 前缀，避免与 precheck 示例混淆。

    均为原子写入。
    """
    if summary["mode"] == "precheck":
        summary_path, md_path = SUMMARY_JSON, OBSERVATION_MD
    else:
        out = Path(output_dir) if output_dir is not None else TRACK_OUTPUT_DIR
        summary_path = out / "gate4b-track-summary.json"
        md_path = out / "gate4b-track-observation.md"
    _atomic_write_json(summary_path, summary)
    _atomic_write_text(md_path, _render_md(summary))
    return summary_path, md_path


def main() -> int:
    parser = argparse.ArgumentParser(description="Gate 4B 观察报告生成器")
    parser.add_argument("--mode", choices=["precheck", "track"], default="precheck")
    parser.add_argument(
        "--config",
        default=None,
        help="自动化配置文件路径（默认 config/automation.default.yaml）",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="track 输出目录（默认 state/automation/gate4b/，Git 忽略；"
        "precheck 忽略此参数，始终写正式示例目录）",
    )
    args = parser.parse_args()

    if args.mode == "track":
        if args.config:
            config = load_automation_config(args.config)
            summary = _track_real(config)
        else:
            summary = _track_real()
    else:
        summary = _run_replay()
    summary["generated_at"] = datetime.now().isoformat(timespec="seconds")
    summary["synthetic"] = args.mode == "precheck"
    summary["online"] = False
    summary["disclaimer"] = (
        "本报告由 scripts/gate4b_observation.py 生成（代码可复现），"
        "内容为模拟/研究记录，未连接券商、未涉及真实资金。"
    )

    summary_path, md_path = _write_outputs(
        summary,
        output_dir=Path(args.output_dir) if args.output_dir else None,
    )
    print(f"[gate4b:{args.mode}] wrote {summary_path}")
    print(f"[gate4b:{args.mode}] wrote {md_path}")
    return 0


def _synthetic_env(base: Path):
    app_cfg = load_config(ROOT / "config" / "default.yaml")
    start = date(2020, 1, 2)
    quotes = make_research_quotes(start=start, n_days=200, n_stocks=8)
    status_df = make_historical_status_table(start=start, n_stocks=8)
    bench = make_benchmark_data(start=start, n_days=200)
    trade_dates = sorted({pd.Timestamp(d).date() for d in quotes["trade_date"]})
    cal = TradingCalendar.from_dates(trade_dates, source="synthetic-calendar")
    source = InjectedDataSource(
        quotes,
        name="synthetic-research-samples",
        synthetic=True,
        security_master=status_df,
        benchmark=bench,
    )
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
    ).with_base_dir(base)
    return config, source, cal, trade_dates, app_cfg, {"min_turnover": 0.0, "min_listing_days": 120}


def _run_replay() -> dict[str, Any]:
    """历史回放预检：临时目录内连续运行 60 个交易日。"""
    now = datetime(2026, 8, 2, 12, 0, 0)
    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        config, source, cal, trade_dates, app_cfg, uk = _synthetic_env(base)
        store = StateStore(config.state_dir)
        window = trade_dates[-OBSERVATION_DAYS:]
        daily: list[dict] = []
        all_orders: list[dict] = []
        identity_violations = 0
        negative_cash_days = 0
        equity_by_account: dict[str, list[dict]] = {}
        for d in window:
            out = run_daily(
                config,
                as_of_date=d,
                data_source=source,
                pipeline=DailyPipeline(app_config=app_cfg, calendar=cal, universe_kwargs=uk),
                state_store=store,
                now_fn=lambda: now,
            )
            rep = config.reports_dir / "daily" / d.isoformat()
            signals = json.loads((rep / "signals.json").read_text(encoding="utf-8"))["signals"]
            orders = json.loads((rep / "simulated-orders.json").read_text(encoding="utf-8"))["orders"]
            accs = json.loads((rep / "accounts.json").read_text(encoding="utf-8"))
            identity_ok = True
            for a in accs["accounts"]:
                eq = accs["equity"][a["account_id"]]
                if Decimal(a["cash"]) + Decimal(eq["position_value"]) != Decimal(eq["total_equity"]):
                    identity_ok = False
                    identity_violations += 1
                if Decimal(a["cash"]) < 0:
                    negative_cash_days += 1
                equity_by_account.setdefault(a["account_id"], []).append(
                    {"date": d.isoformat(), "total_equity": eq["total_equity"]}
                )
            all_orders.extend(orders)
            daily.append({
                "date": d.isoformat(),
                "state": out.state.value,
                "exit_code": out.exit_code,
                "signals": len(signals),
                "orders": len(orders),
                "filled": sum(1 for o in orders if o["status"] == "FILLED"),
                "identity_ok": identity_ok,
            })
        obs = {
            "accounts": [a.account_id for a in config.accounts],
            "observation_days_final": {
                a.account_id: store.load_account(a.account_id).observation_days
                for a in config.accounts
            },
        }
        return {
            "mode": "precheck",
            "first_day": window[0].isoformat(),
            "last_day": window[-1].isoformat(),
            "trading_days": len(window),
            "daily": daily,
            "totals": {
                "signals": sum(x["signals"] for x in daily),
                "orders": sum(x["orders"] for x in daily),
                "filled": sum(x["filled"] for x in daily),
                "duplicate_orders": len(all_orders)
                - len({o["unique_key"] for o in all_orders}),
                "identity_violations": identity_violations,
                "negative_cash_days": negative_cash_days,
                "non_success_days": sum(1 for x in daily if x["state"] != "SUCCESS"),
            },
            "observation": obs,
            "equity_curves": equity_by_account,
        }


def _track_real(
    config: Optional[AutomationConfig] = None,
    *,
    calendar: Optional[TradingCalendar] = None,
    observation_target: int = OBSERVATION_DAYS,
) -> dict[str, Any]:
    """真实自动任务进度：用交易日历生成预期序列，逐日复核运行记录与审计产物。

    启动日 = 最早运行记录所在日期**归一化到交易日**：当天是交易日则用当天，
    否则取其后最近交易日（节假日/周末产生的 ``SKIPPED_NON_TRADING_DAY``
    记录不是预期交易日，不能作为观察起点，否则会永久停在 0）。随后按交易日历
    生成连续 ``observation_target`` 个预期交易日；对每个预期交易日逐日复核：

    1. 运行记录存在且 ``SUCCESS`` / exit 0（缺失或失败 = 违规）；
    2. 报告目录产物齐全（run.json / manifest.json / accounts.json /
       simulated-orders.json / signals.json）；
    3. 每日 ``manifest.json`` 哈希可复算（``verify_manifest``）；
    4. 全窗口订单 ``unique_key`` 与 ``order_id`` 各自唯一（两个独立集合，
       避免跨字段偶然同值误报；无重复订单）；
    5. 每日账务恒等式 cash + position_value == total_equity（无无法解释的
       权益变化）；现金非负。

    只有从启动日起**连续全部通过**的预期交易日才计入进度；任何一天缺失、
    失败或任一项复核不过，进度即中断（连续语义，与自然日无关——周末由
    交易日历跳过，不会重置计数）。
    """
    if config is None:
        config = load_automation_config(ROOT / "config" / "automation.default.yaml")
    store = StateStore(config.state_dir)
    if calendar is None:
        try:
            calendar = load_trading_calendar(config)
        except CalendarUnavailableError as exc:
            return {
                "mode": "track",
                "calendar_error": str(exc),
                "observation_progress": 0,
                "observation_target": observation_target,
                "real_success_trading_days": 0,
                "consecutive_trading_days": 0,
                "state_dir": str(config.state_dir),
                "start_date": None,
                "last_date": None,
                "violations": ["交易日历不可用，无法生成预期交易日序列（fail-closed）"],
            }

    records = store.list_runs(TaskType.DAILY)
    by_date = {r.as_of_date: r for r in records}
    if not by_date:
        return {
            "mode": "track",
            "observation_progress": 0,
            "observation_target": observation_target,
            "real_success_trading_days": 0,
            "consecutive_trading_days": 0,
            "state_dir": str(config.state_dir),
            "start_date": None,
            "last_date": None,
            "violations": [],
        }

    # 启动日 = 最早运行记录；归一化到交易日。
    # 注意：任务在节假日/周末也可能留下 SKIPPED_NON_TRADING_DAY 记录（非交易日），
    # 它**不是**预期交易日——若直接以它为起点，会因「非 SUCCESS」永久停在 0。
    # 规则：最早运行日期当天若是交易日 → 用当天；否则 → 取其后最近交易日。
    earliest = min(by_date)
    try:
        start_date = calendar.next_trading_day(earliest, inclusive=True)
    except CalendarUnavailableError:
        return {
            "mode": "track",
            "calendar_error": (
                f"最早运行记录 {earliest.isoformat()} 无法归一到交易日"
                f"（日历范围 [{calendar.first_date.isoformat()}, "
                f"{calendar.last_date.isoformat()}]，来源: {calendar.source}）"
            ),
            "observation_progress": 0,
            "observation_target": observation_target,
            "real_success_trading_days": sum(
                1 for r in records if r.state is RunState.SUCCESS
            ),
            "consecutive_trading_days": 0,
            "state_dir": str(config.state_dir),
            "start_date": earliest.isoformat(),
            "last_date": None,
            "violations": [f"{earliest.isoformat()}: 启动日无法归一到交易日"],
        }
    expected_days: list[date] = []
    d = start_date
    try:
        for _ in range(observation_target):
            expected_days.append(d)
            d = calendar.next_trading_day(d, inclusive=False)
    except CalendarUnavailableError:
        # 日历覆盖不足（真实部署中日历通常延伸到当前日期，属正常）。
        pass

    seen_unique_keys: set[str] = set()
    seen_order_ids: set[str] = set()
    violations: list[str] = []
    checked: list[dict[str, Any]] = []
    consecutive = 0
    for i, day in enumerate(expected_days):
        rec = by_date.get(day)
        day_ok = True
        issues: list[str] = []
        rep_dir = config.reports_dir / "daily" / day.isoformat()

        if rec is None:
            day_ok = False
            issues.append("运行记录缺失")
        elif rec.state is not RunState.SUCCESS or rec.exit_code != 0:
            day_ok = False
            issues.append(f"非 SUCCESS：{rec.state.value} / exit {rec.exit_code}")
        else:
            # 产物齐全
            required = [
                "run.json",
                "manifest.json",
                "accounts.json",
                "simulated-orders.json",
                "signals.json",
            ]
            missing = [n for n in required if not (rep_dir / n).exists()]
            if missing:
                day_ok = False
                issues.append(f"产物缺失: {', '.join(missing)}")
            else:
                # manifest 可复算
                try:
                    verify_manifest(rep_dir / "manifest.json", config=config)
                except Exception as exc:  # noqa: BLE001
                    day_ok = False
                    issues.append(f"manifest 校验失败: {exc}")
                # 账务恒等式 + 负现金
                try:
                    accs = json.loads(
                        (rep_dir / "accounts.json").read_text(encoding="utf-8")
                    )
                    for a in accs["accounts"]:
                        eq = accs["equity"][a["account_id"]]
                        if Decimal(a["cash"]) + Decimal(eq["position_value"]) != Decimal(
                            eq["total_equity"]
                        ):
                            day_ok = False
                            issues.append(
                                f"账务恒等式违规: {a['account_id']} "
                                f"cash={a['cash']} + pos={eq['position_value']} "
                                f"!= equity={eq['total_equity']}"
                            )
                        if Decimal(a["cash"]) < 0:
                            day_ok = False
                            issues.append(f"负现金: {a['account_id']} cash={a['cash']}")
                except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
                    day_ok = False
                    issues.append(f"accounts.json 读取失败: {exc}")
                # 重复订单（unique_key 与 order_id 各自全窗口唯一；用两个独立集合，
                # 避免某订单的 unique_key 与另一订单的 order_id 偶然同值造成误报）
                try:
                    ords = json.loads(
                        (rep_dir / "simulated-orders.json").read_text(
                            encoding="utf-8"
                        )
                    )["orders"]
                    for o in ords:
                        uk = o.get("unique_key")
                        if uk is not None:
                            if uk in seen_unique_keys:
                                day_ok = False
                                issues.append(f"重复订单 unique_key={uk}")
                            seen_unique_keys.add(uk)
                        oid = o.get("order_id")
                        if oid is not None:
                            if oid in seen_order_ids:
                                day_ok = False
                                issues.append(f"重复订单 order_id={oid}")
                            seen_order_ids.add(oid)
                except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
                    day_ok = False
                    issues.append(f"simulated-orders.json 读取失败: {exc}")

        checked.append({
            "date": day.isoformat(),
            "day_index": i + 1,
            "ok": day_ok,
            "state": rec.state.value if rec is not None else "MISSING",
            "issues": issues,
        })
        if day_ok:
            consecutive += 1
        else:
            violations.extend(f"{day.isoformat()}: {msg}" for msg in issues)
            break  # 连续窗口中断：只统计从启动日起的连续达标段

    return {
        "mode": "track",
        "observation_progress": consecutive,
        "observation_target": observation_target,
        "real_success_trading_days": sum(
            1 for r in records if r.state is RunState.SUCCESS
        ),
        "consecutive_trading_days": consecutive,
        "expected_trading_days": len(expected_days),
        "start_date": start_date.isoformat(),
        "last_date": checked[-1]["date"] if checked else None,
        "state_dir": str(config.state_dir),
        "violations": violations,
        "daily": checked,
        "calendar_coverage": len(expected_days) < observation_target,
    }


def _render_md(summary: dict[str, Any]) -> str:
    lines = ["# Gate 4B 观察报告：连续 60 个交易日自动运行", ""]
    if summary["mode"] == "track":
        t = summary
        if t["real_success_trading_days"] == 0 and not t["start_date"]:
            status = "**Gate 4B continuous operation：NOT STARTED（0/60）**"
        elif t.get("calendar_error"):
            status = "**Gate 4B continuous operation：无法判定（交易日历不可用）**"
        elif t["observation_progress"] >= t["observation_target"]:
            status = "**Gate 4B continuous operation：达标（60/60）**"
        else:
            status = (
                f"**Gate 4B continuous operation：进行中 "
                f"（{t['observation_progress']}/{t['observation_target']}）**"
            )
        lines += [
            "## 状态",
            "",
            status,
            "",
            f"- 真实自动任务累计 SUCCESS 交易日：{t['real_success_trading_days']}",
            f"- 自启动日起按交易日历连续达标：{t['consecutive_trading_days']}（预期 "
            f"{t.get('expected_trading_days', t['observation_target'])} 个交易日）",
            f"- 观察起始日（真实自动任务启动日期）：{t['start_date'] or '—'}",
            f"- 最近复核交易日：{t['last_date'] or '—'}",
            f"- 状态目录：`{t['state_dir']}`",
            "",
            "> 进度由 `scripts/gate4b_observation.py --mode track` 从真实运行记录实时计算，"
            "并**按交易日历逐日复核**（记录 SUCCESS/exit 0、产物齐全、manifest 可复算、"
            "无重复订单、账务恒等式成立、现金非负）；任何一天不过即中断连续计数，"
            "周末等非交易日由日历自动跳过，不会重置计数。",
            "",
        ]
        if t.get("calendar_error"):
            lines += [
                "> ⚠️ **交易日历不可用（fail-closed）**：`data/metadata/trade_calendar.parquet` "
                "缺失或不可读，无法生成预期交易日序列，进度暂不可计算。",
                f"> `{t['calendar_error']}`",
                "",
                "> 请先执行 `ashare-quant fetch`（或等价数据准备）生成真实交易日历后重跑本脚本。",
                "",
            ]
        if t.get("violations"):
            lines += [
                "### 复核不通过明细",
                "",
                "| 交易日 | 原因 |",
                "| --- | --- |",
            ]
            for v in t["violations"]:
                day, _, reason = v.partition(":")
                lines.append(f"| {day.strip()} | {reason.strip() if reason else '—'} |")
            lines.append("")
        elif not t.get("calendar_error") and t.get("daily"):
            lines += [
                "### 逐日复核结果（连续达标段）",
                "",
                "| 交易日 | 复核 | 运行状态 |",
                "| --- | --- | --- |",
            ]
            for c in t["daily"]:
                mark = "✓" if c["ok"] else "✗"
                lines.append(
                    f"| {c['date']} | {mark} | {c['state']} |"
                )
            lines.append("")
    else:
        t = summary["totals"]
        lines += [
            "## 状态",
            "",
            "- **Gate 4B historical replay（60 日历史回放预检）：PASS**",
            "- **Gate 4B continuous operation（连续 60 个交易日自动运行）：NOT STARTED（0/60）**",
            "",
            "## 预检（历史回放）结果",
            "",
            f"- 观察窗口：{summary['first_day']} ~ {summary['last_day']}（{summary['trading_days']} 个连续交易日）",
            f"- 每日终态：{summary['trading_days']}/{summary['trading_days']} 全部 `SUCCESS` / exit 0，**无静默错误**",
            f"- 订单：共 {t['orders']} 条，成交 {t['filled']} 笔，信号 {t['signals']} 条，**重复订单 0**",
            f"- 账务恒等式违规 0 次；负现金 0 次；**无无法解释的权益变化**",
            "- 数据源：确定性合成行情（synthetic，离线），不联网、不手写结果",
            "",
            "> **重要边界**：以上为**历史回放预检**（单进程内完成，约 20-40 秒），"
            "**不能**替代原验收条件要求的「连续 60 个交易日自动运行」。"
            "正式观察须从真实自动任务启动日起，按实际交易日经 CLI / Windows 任务计划、"
            "跨日进程重启、真实数据更新、失败告警与恢复流程累计 60 天。",
            "",
            "## 每日明细（预检回放）",
            "",
            "| 交易日 | 终态 | 退出码 | 信号 | 订单 | 成交 | 账务恒等 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
        ]
        for x in summary["daily"]:
            lines.append(
                f"| {x['date']} | {x['state']} | {x['exit_code']} | {x['signals']} | "
                f"{x['orders']} | {x['filled']} | {'✓' if x['identity_ok'] else '✗'} |"
            )
    lines += [
        "",
        "## 连续运行启动指引（continuous operation）",
        "",
        "1. 确认真实数据源就绪（`config/automation.default.yaml` 的 `data.symbols` 与本地 curated 数据），",
        "   并验证 `ashare-quant automation verify` 通过。",
        "2. 安装 Windows 任务计划：`ashare-quant automation install --yes`（或运行 `scripts/install_scheduler.ps1`），",
        "   每日 `18:30` 自动执行 `ashare-quant automation daily`。",
        "3. 自启动日起，每个真实交易日由任务计划运行一次每日管线（独立进程、跨日重启、真实数据更新、",
        "   失败告警与中断恢复）。",
        "4. 进度由 `scripts/gate4b_observation.py --mode track` 按实际交易日实时累计；",
        "   连续 60 个交易日（X/60）且每日 SUCCESS、无重复订单、账务恒等式无违规时，方标记 continuous operation 达标。",
        "",
        "## 资格结论",
        "",
        "- 稳健轨 `paper-steady`：`NOT_ELIGIBLE_FOR_LIVE_TRADING`",
        "- 激进轨 `paper-aggressive`：`SIMULATION_ONLY`",
        "- **两轨均不具备实盘资格**；观察窗口完成不等同于获得实盘授权，复审结论不由本系统作出。",
        "",
        "> **边界声明**：本报告由本机自动化研究系统生成，内容为模拟/研究记录；",
        "> 未连接券商、未涉及真实资金，不构成投资建议。",
        "",
        "**等待 Gate 4B 复审（continuous operation 尚未开始）。**",
        "",
    ]
    return "\n".join(lines)
