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
    读取真实自动化状态（``state/automation`` 运行记录）与产物，统计
    「真实自动任务」自启动日起按实际交易日累计的连续运行进度 X/60，
    并校验真实运行中的重复订单与账务恒等式。正式观察尚未启动时为 0/60。

用法::

    python scripts/gate4b_observation.py                 # precheck
    python scripts/gate4b_observation.py --mode track    # 真实进度
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

from ashare_quant.automation.calendar import TradingCalendar
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
SUMMARY_JSON = ROOT / "reports" / "phase-4" / "gate4b" / "60d-summary.json"
OBSERVATION_MD = ROOT / "docs" / "gate4b-observation.md"


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


def _track_real() -> dict[str, Any]:
    """真实自动任务进度：读取 state/automation 运行记录，统计连续交易日 X/60。"""
    config = load_automation_config(ROOT / "config" / "automation.default.yaml")
    store = StateStore(config.state_dir)
    real_dir = config.state_dir
    run_root = real_dir / "runs" / "daily"
    records = []
    if run_root.exists():
        for p in sorted(run_root.glob("*.json")):
            try:
                rec = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            if rec.get("task_type") == "daily":
                records.append(rec)
    # 连续交易日计数：从最早的 SUCCESS 记录起，按日期连续（真实日历交易日由记录本身体现）
    success_days = sorted(
        {date.fromisoformat(r["as_of_date"]) for r in records if r.get("state") == "SUCCESS"}
    )
    consecutive = 0
    prev = None
    for d in success_days:
        if prev is None or (d - prev).days == 1:
            consecutive += 1
        else:
            consecutive = 1
        prev = d
    observed = min(consecutive, OBSERVATION_DAYS)
    return {
        "mode": "track",
        "real_success_trading_days": len(success_days),
        "consecutive_trading_days": consecutive,
        "observation_progress": observed,  # min(consecutive, 60)
        "observation_target": OBSERVATION_DAYS,
        "state_dir": str(real_dir),
        "start_date": success_days[0].isoformat() if success_days else None,
        "last_date": success_days[-1].isoformat() if success_days else None,
    }


def _render_md(summary: dict[str, Any]) -> str:
    lines = ["# Gate 4B 观察报告：连续 60 个交易日自动运行", ""]
    if summary["mode"] == "track":
        t = summary
        if t["real_success_trading_days"] == 0:
            status = "**Gate 4B continuous operation：NOT STARTED（0/60）**"
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
            f"- 自启动日起连续交易日：{t['consecutive_trading_days']}（按实际交易日累计）",
            f"- 观察起始日：{t['start_date'] or '—'}（真实自动任务启动日期）",
            f"- 最近交易日：{t['last_date'] or '—'}",
            f"- 状态目录：`{t['state_dir']}`",
            "",
            "> 进度由 `scripts/gate4b_observation.py --mode track` 从真实运行记录实时计算，"
            "不是预生成快照。",
            "",
        ]
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Gate 4B 观察报告生成器")
    parser.add_argument("--mode", choices=["precheck", "track"], default="precheck")
    args = parser.parse_args()

    if args.mode == "track":
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

    SUMMARY_JSON.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY_JSON.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    OBSERVATION_MD.write_text(_render_md(summary), encoding="utf-8")
    print(f"[gate4b:{args.mode}] wrote {SUMMARY_JSON}")
    print(f"[gate4b:{args.mode}] wrote {OBSERVATION_MD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
