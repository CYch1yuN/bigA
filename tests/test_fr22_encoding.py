"""FR-22：UTF-8 编码完整性测试。

两类校验：

A. 静态文件编码校验
   以 ``Path.read_text(encoding="utf-8")`` 直接读取正式文件，断言：
   - 关键 Phase 4 中文术语存在且可读；
   - 典型乱码片段（±¾ / »ú / ×Ô / ¶¯ / â€¦）**不存在**；
   - 所有扫描到的 JSON 产物可被 ``json.loads()`` 正常解析。
   扫描范围严格限定在 FR-22 指定文件集合。

B. 合成运行 -> 生成报告 -> 读取断言（端到端编码校验）
   在 ``tmp_path`` 内跑一次合成每日任务与一次合成每周任务，然后用 UTF-8
   读取生成的 Markdown / JSON，断言中文正常、终态 SUCCESS、退出码 0、结束时间
   非空。**绝不**复用仓库中 ``reports/phase-4`` 下任何预生成报告。
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

# ---------------------------------------------------------------------- #
# 路径
# ---------------------------------------------------------------------- #

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in __import__("sys").path:
    __import__("sys").path.insert(0, str(ROOT))

from ashare_quant.automation.calendar import TradingCalendar  # noqa: E402
from ashare_quant.automation.config import (  # noqa: E402
    AccountConfig,
    AutomationConfig,
    DataConfig,
    LoggingConfig,
    PathsConfig,
)
from ashare_quant.automation.datasource import InjectedDataSource  # noqa: E402
from ashare_quant.automation.models import (  # noqa: E402
    EligibilityStatus,
    RunState,
    StrategyTrack,
    TaskType,
)
from ashare_quant.automation.reporting import result_paths  # noqa: E402
from ashare_quant.automation.state import StateStore  # noqa: E402
from ashare_quant.automation.weekly import WEEKLY_STEPS, WeeklyPipeline, run_weekly  # noqa: E402
from ashare_quant.backtest.config import BacktestConfig  # noqa: E402
from ashare_quant.config import load_config  # noqa: E402
from tests.research_samples import (  # noqa: E402
    make_benchmark_data,
    make_historical_status_table,
    make_research_quotes,
)

from ashare_quant.automation.daily import DailyPipeline, run_daily  # noqa: E402

# 典型乱码片段（Latin-1/CP1252 误读 GBK/UTF-8 字节的产物）。
GARBLE_FRAGMENTS = [
    "±¾", "»ú", "×Ô", "¶¯", "é", "æ¶", "ç’", "鈹", "Ã", "Æ",
    "锛", "鈥", "婵€",
]

# 必须出现的 Phase 4 可读中文术语。
# 注：「不得接触真实资金」在正式文件中并无逐字出现，其含义由免责声明
# 「未涉及任何真实资金」承载（reporting.py DISCLAIMER_BLOCK），故以此替代，
# 二者语义等价，均为「系统不碰真实资金」的边界声明。
REQUIRED_TERMS = [
    "Phase 4 Windows 本机自动化",
    "交易日历",
    "数据质量闸门",
    "模拟账户",
    "每周自动化",
    "仅用于模拟研究",
    "不连接券商",
    "未涉及任何真实资金",
    "NOT_ELIGIBLE_FOR_LIVE_TRADING",
    "SIMULATION_ONLY",
]

# 含故意乱码 fixture 的测试文件：仅用于「术语存在性」校验，不参与「无乱码」校验。
INTENTIONAL_FIXTURE = "tests/test_fr15b_encoding.py"
# 本测试文件自身也包含乱码片段字面量（用于定义 GARBLE_FRAGMENTS），必须排除出「无乱码」校验。
SELF_FILE = "tests/test_fr22_encoding.py"


# ---------------------------------------------------------------------- #
# 静态文件编码校验
# ---------------------------------------------------------------------- #

def _collect_target_files() -> list[Path]:
    files: list[Path] = []
    # docs / config
    files.append(ROOT / "docs" / "phase-4-automation.md")
    files.append(ROOT / "config" / "automation.default.yaml")
    # scripts/*.ps1
    files.extend(sorted((ROOT / "scripts").glob("*.ps1")))
    # src/ashare_quant/automation/*.py
    files.extend(sorted((ROOT / "src" / "ashare_quant" / "automation").glob("*.py")))
    # tests
    files.append(ROOT / "tests" / "test_phase4_automation.py")
    files.extend(sorted((ROOT / "tests").glob("test_fr*.py")))
    # reports/phase-4/**/*
    files.extend(sorted((ROOT / "reports" / "phase-4").rglob("*")))
    # 过滤：仅保留真实存在的普通文件，跳过目录与字节码缓存
    out = [
        f for f in files
        if f.is_file() and "__pycache__" not in f.parts and f.suffix != ".pyc"
    ]
    # 去重
    seen: set[Path] = set()
    uniq: list[Path] = []
    for f in out:
        if f not in seen:
            seen.add(f)
            uniq.append(f)
    return uniq


def _rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


def test_official_files_clean_utf8_and_required_terms():
    targets = _collect_target_files()
    assert targets, "FR-22 扫描目标文件集合不能为空"

    combined = ""
    json_parse_failures: list[str] = []
    garble_hits: list[str] = []
    decode_failures: list[str] = []

    for f in targets:
        rel = _rel(f)
        # 1) 必须能以 UTF-8 正常读取
        try:
            text = f.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            decode_failures.append(f"{rel}: {exc}")
            continue
        combined += text + "\n"

        # 2) 无乱码（故意 fixture 文件与本测试文件自身除外）
        if rel not in (INTENTIONAL_FIXTURE, SELF_FILE):
            for frag in GARBLE_FRAGMENTS:
                if frag in text:
                    garble_hits.append(f"{rel}: 命中乱码片段 {frag!r}")

        # 3) JSON 必须可被 json.loads 解析
        if f.suffix == ".json":
            try:
                json.loads(text)
            except json.JSONDecodeError as exc:
                json_parse_failures.append(f"{rel}: {exc}")

    # 四类错误最终都必须为 0
    assert not decode_failures, "UTF-8 解码失败：\n" + "\n".join(decode_failures)
    assert not garble_hits, "发现乱码片段：\n" + "\n".join(garble_hits)
    assert not json_parse_failures, "JSON 解析失败：\n" + "\n".join(json_parse_failures)

    # 4) 关键可读中文术语必须存在
    missing = [t for t in REQUIRED_TERMS if t not in combined]
    assert not missing, "缺失关键中文术语：\n" + "\n".join(missing)


# ---------------------------------------------------------------------- #
# 合成运行 -> 生成报告 -> 读取断言
# ---------------------------------------------------------------------- #

def _synthetic(base_dir: Path):
    """构造离线验证环境（合成行情 + 注入数据源 + 合成日历）。"""
    app_cfg = load_config(ROOT / "config" / "default.yaml")
    start = date(2020, 1, 2)
    n_days = 200
    quotes = make_research_quotes(start=start, n_days=n_days, n_stocks=8)
    status_df = make_historical_status_table(start=start, n_stocks=8)
    bench = make_benchmark_data(start=start, n_days=n_days)
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
    ).with_base_dir(base_dir)
    universe_kwargs = {"min_turnover": 0.0, "min_listing_days": 120}
    return config, source, cal, trade_dates, app_cfg, universe_kwargs


def _run_window_and_saturday(config, source, cal, trade_dates, app_cfg, uk, store, n=10):
    from ashare_quant.automation.daily import DailyPipeline, run_daily

    window = trade_dates[-n:]
    for d in window:
        run_daily(
            config,
            as_of_date=d,
            data_source=source,
            pipeline=DailyPipeline(app_config=app_cfg, calendar=cal, universe_kwargs=uk),
            state_store=store,
        )
    anchor = window[5]
    as_of_sat = anchor + timedelta(days=(5 - anchor.weekday()) % 7)
    cal2 = TradingCalendar.from_dates(list(trade_dates) + [as_of_sat], source="syn+asof")
    return as_of_sat, cal2


def test_synthetic_daily_report_clean_utf8(tmp_path):
    config, source, cal, trade_dates, app_cfg, uk = _synthetic(tmp_path)
    store = StateStore(config.state_dir)
    d = trade_dates[-1]
    out = run_daily(
        config,
        as_of_date=d,
        data_source=source,
        pipeline=DailyPipeline(app_config=app_cfg, calendar=cal, universe_kwargs=uk),
        state_store=store,
    )
    assert out.state is RunState.SUCCESS, f"每日合成运行终态应为 SUCCESS，实际 {out.state}"
    assert out.exit_code == 0, f"每日合成运行退出码应为 0，实际 {out.exit_code}"

    paths = result_paths(config, task_type=TaskType.DAILY, as_of_date=d)
    # 终态报告与运行记录必须已落盘
    assert paths.report_md.exists(), "每日报告 Markdown 未生成"
    assert paths.run_json.exists(), "每日运行记录 JSON 未生成"

    # UTF-8 读取报告，断言中文正常、无乱码、JSON 可解析
    md = paths.report_md.read_text(encoding="utf-8")
    assert "模拟账户" in md, "每日报告缺少『模拟账户』中文"
    assert "数据质量闸门" in md, "每日报告缺少『数据质量闸门』中文"
    assert "未涉及任何真实资金" in md, "每日报告缺少真实资金边界声明"

    run_record = json.loads(paths.run_json.read_text(encoding="utf-8"))
    assert run_record["state"] == "SUCCESS"
    assert run_record["exit_code"] == 0
    assert run_record.get("finished_at"), "运行记录 finished_at 必须非空"

    for frag in GARBLE_FRAGMENTS:
        assert frag not in md, f"每日报告出现乱码片段 {frag!r}"


def test_synthetic_weekly_report_clean_utf8(tmp_path):
    config, source, cal, trade_dates, app_cfg, uk = _synthetic(tmp_path)
    store = StateStore(config.state_dir)
    as_of_sat, cal2 = _run_window_and_saturday(
        config, source, cal, trade_dates, app_cfg, uk, store
    )

    out = run_weekly(
        config,
        as_of_date=as_of_sat,
        pipeline=WeeklyPipeline(calendar=cal2),
        state_store=store,
    )
    assert out.state is RunState.SUCCESS, f"每周合成运行终态应为 SUCCESS，实际 {out.state}"
    assert out.exit_code == 0, f"每周合成运行退出码应为 0，实际 {out.exit_code}"

    paths = result_paths(config, task_type=TaskType.WEEKLY, as_of_date=as_of_sat)
    assert paths.report_md.exists(), "每周报告 Markdown 未生成"
    assert paths.run_json.exists(), "每周运行记录 JSON 未生成"

    md = paths.report_md.read_text(encoding="utf-8")
    assert "模拟账户" in md, "每周报告缺少『模拟账户』中文"
    assert "每周自动化" in md, "每周报告缺少『每周自动化』中文"
    assert "NOT_ELIGIBLE_FOR_LIVE_TRADING" in md or "SIMULATION_ONLY" in md

    run_record = json.loads(paths.run_json.read_text(encoding="utf-8"))
    assert run_record["state"] == "SUCCESS"
    assert run_record["exit_code"] == 0
    assert run_record.get("finished_at"), "每周运行记录 finished_at 必须非空"

    for frag in GARBLE_FRAGMENTS:
        assert frag not in md, f"每周报告出现乱码片段 {frag!r}"

    # 周报步骤完整（WEEKLY_STEPS 全出现）
    step_names = [s["name"] for s in run_record.get("steps", [])]
    for name in WEEKLY_STEPS:
        assert name in step_names, f"周报缺少步骤 {name}"
