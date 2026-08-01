"""Gate 4B 真实观察跟踪器（scripts/gate4b_observation._track_real）回归测试。

复审暴露的阻断性缺陷：
    (d - prev).days == 1 按**自然日**判断连续性——周五到周一相差 3 天，
    每周都会重置计数，真实 A 股观察窗口几乎永远无法达到 60 天。

本测试锁定修复后的语义：
1. 用交易日历生成预期交易日序列（周末/节假日由日历跳过，不重置计数）；
2. 自启动日起逐日复核：记录 SUCCESS / exit 0、产物齐全、manifest 可复算、
   无重复订单、账务恒等式 cash + position_value == total_equity、现金非负；
3. 缺失日 / 失败日 / 重复订单 / 恒等式违规 / 负现金 / manifest 篡改
   任一发生即中断连续计数；
4. 只有连续 60 个预期交易日全部满足验收条件才输出 60/60；
5. 交易日历不可用 → fail-closed（不产出虚假 0/60）；
6. 日历覆盖不足 60 个交易日 → 如实标记 calendar_coverage，不能假装达标。
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

from ashare_quant.automation.audit import build_manifest  # noqa: E402
from ashare_quant.automation.calendar import TradingCalendar  # noqa: E402
from ashare_quant.automation.config import (  # noqa: E402
    AccountConfig,
    AutomationConfig,
    DataConfig,
    LoggingConfig,
    PathsConfig,
)
from ashare_quant.automation.models import (  # noqa: E402
    EligibilityStatus,
    RunRecord,
    RunState,
    StrategyTrack,
    TaskType,
)
from ashare_quant.automation.state import StateStore  # noqa: E402

import gate4b_observation as g4b  # noqa: E402


# ---------------------------------------------------------------------- #
# 合成环境
# ---------------------------------------------------------------------- #

def _config(base: Path) -> AutomationConfig:
    return AutomationConfig(
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


def _weekday_calendar(n_days: int, *, start: date | None = None) -> TradingCalendar:
    """生成只含周一至周五的合成交易日历（A 股常态），共 n_days 个交易日。"""
    if start is None:
        start = date(2020, 1, 6)  # 周一
    days: list[date] = []
    d = start
    while len(days) < n_days:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return TradingCalendar.from_dates(days, source="test-weekdays")


def _seed_day(
    config: AutomationConfig,
    day: date,
    *,
    state: RunState = RunState.SUCCESS,
    orders: list[dict] | None = None,
    tamper_after_manifest: bool = False,
    identity_broken: bool = False,
    negative_cash: bool = False,
) -> None:
    """构造某交易日的最小化真实产物：运行记录 + 报告目录 + 可复算 manifest。"""
    store = StateStore(config.state_dir)
    run_id = f"run-{day.isoformat()}"
    record = RunRecord(
        run_id=run_id,
        task_type=TaskType.DAILY,
        as_of_date=day,
        state=state,
        code_commit="test-commit",
        config_hash="cfg-hash",
        input_hash="in-hash",
    )
    store.save_run(record, update_latest=False)

    rep = config.reports_dir / "daily" / day.isoformat()
    rep.mkdir(parents=True, exist_ok=True)

    # signals.json（空信号即可）
    (rep / "signals.json").write_text(
        json.dumps(
            {"as_of_date": day.isoformat(), "run_id": run_id, "signals": []},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # simulated-orders.json（默认无订单）
    (rep / "simulated-orders.json").write_text(
        json.dumps(
            {
                "as_of_date": day.isoformat(),
                "run_id": run_id,
                "simulated": True,
                "orders": orders or [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # accounts.json：cash / position_value / total_equity 以字符串存（同真实产物）
    def _acct(account_id: str, cash: str, pos: str, eq: str) -> dict:
        return {
            "schema_version": 1,
            "account_id": account_id,
            "strategy_track": "steady",
            "cash": cash,
            "positions": [],
        }, {"cash": cash, "position_value": pos, "total_equity": eq, "positions": 0}

    steady, steady_eq = _acct(
        "paper-steady", "-1.00" if negative_cash else "1000.00", "0.00", "999.00" if identity_broken else "1000.00"
    )
    aggr, aggr_eq = _acct("paper-aggressive", "1000.00", "0.00", "1000.00")
    (rep / "accounts.json").write_text(
        json.dumps(
            {
                "as_of_date": day.isoformat(),
                "run_id": run_id,
                "accounts": [steady, aggr],
                "equity": {"paper-steady": steady_eq, "paper-aggressive": aggr_eq},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # run.json
    (rep / "run.json").write_text(
        json.dumps(record.to_dict(), ensure_ascii=False),
        encoding="utf-8",
    )

    # manifest.json：由真实 build_manifest 生成，哈希与文件一致（可复算）
    manifest = build_manifest(
        record=record,
        config=config,
        run_dir=rep,
        output_hash="out-hash-test",
    )
    (rep / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )

    # 可选：生成 manifest 之后再篡改文件 → verify_manifest 必须失败
    if tamper_after_manifest:
        (rep / "signals.json").write_text(
            '{"as_of_date": "2020-01-01", "signals": [{"tampered": true}]}',
            encoding="utf-8",
        )


def _seed_ok_window(
    config: AutomationConfig,
    calendar: TradingCalendar,
    n_days: int,
    *,
    start: date | None = None,
    mutate: dict[int, callable] | None = None,
) -> list[date]:
    """种下从 start（或日历首日）起连续 n 个交易日；mutate[day_index] 可改某日。"""
    first = start or calendar.dates[0]
    days = list(calendar.dates[: calendar.dates.index(first) + n_days])
    for i, d in enumerate(days):
        fn = (mutate or {}).get(i)
        if fn is not None:
            fn(config, d)
        else:
            _seed_day(config, d)
    return days


# ---------------------------------------------------------------------- #
# 测试
# ---------------------------------------------------------------------- #

def test_weekend_gap_does_not_reset_count(tmp_path: Path) -> None:
    """周五→周一（自然日间隔 3 天）不得重置计数：60 个工作日全部通过 → 60/60。"""
    config = _config(tmp_path)
    cal = _weekday_calendar(60)  # 2020-01-06(周一) 起连续 60 个工作日（跨 12 周）
    _seed_ok_window(config, cal, 60)
    summary = g4b._track_real(config, calendar=cal)
    assert summary["observation_progress"] == 60
    assert summary["consecutive_trading_days"] == 60
    assert summary["real_success_trading_days"] == 60
    assert summary["start_date"] == cal.dates[0].isoformat()
    assert summary["violations"] == []
    assert summary.get("calendar_coverage") is False


def test_older_weekday_skip_counts_as_consecutive(tmp_path: Path) -> None:
    """若首日不是周一，序列仍从该交易日算起，周末不打断。"""
    config = _config(tmp_path)
    cal = _weekday_calendar(60, start=date(2020, 1, 9))  # 周四起
    _seed_ok_window(config, cal, 60)
    summary = g4b._track_real(config, calendar=cal)
    assert summary["observation_progress"] == 60
    assert summary["start_date"] == "2020-01-09"


def test_missing_day_breaks_streak(tmp_path: Path) -> None:
    """预期交易日缺失（无运行记录）→ 连续计数在缺失日中断。"""
    config = _config(tmp_path)
    cal = _weekday_calendar(60)
    days = list(cal.dates[:60])
    for i, d in enumerate(days):
        if i == 29:  # 第 30 个交易日缺失
            continue
        _seed_day(config, d)
    summary = g4b._track_real(config, calendar=cal)
    assert summary["observation_progress"] == 29
    assert summary["consecutive_trading_days"] == 29
    assert any("运行记录缺失" in v for v in summary["violations"])


def test_failed_day_breaks_streak(tmp_path: Path) -> None:
    """某日运行记录非 SUCCESS → 中断。"""
    config = _config(tmp_path)
    cal = _weekday_calendar(60)

    def _fail(cfg, d):
        _seed_day(cfg, d, state=RunState.FAILED)

    _seed_ok_window(config, cal, 60, mutate={40: _fail})
    summary = g4b._track_real(config, calendar=cal)
    assert summary["observation_progress"] == 40
    assert any("非 SUCCESS" in v for v in summary["violations"])


def test_duplicate_order_breaks_streak(tmp_path: Path) -> None:
    """某日出现重复订单（unique_key / order_id 冲突）→ 中断。"""
    config = _config(tmp_path)
    cal = _weekday_calendar(60)
    dup = [
        {
            "order_id": "dup-order-id",
            "unique_key": "dup-unique-key",
            "status": "FILLED",
        },
        {
            "order_id": "dup-order-id",
            "unique_key": "dup-unique-key",
            "status": "FILLED",
        },
    ]

    def _dup(cfg, d):
        _seed_day(cfg, d, orders=dup)

    _seed_ok_window(config, cal, 60, mutate={10: _dup})
    summary = g4b._track_real(config, calendar=cal)
    assert summary["observation_progress"] == 10
    assert any("重复订单" in v for v in summary["violations"])


def test_identity_violation_breaks_streak(tmp_path: Path) -> None:
    """某日账务恒等式 cash + position_value != total_equity → 中断。"""
    config = _config(tmp_path)
    cal = _weekday_calendar(60)
    _seed_ok_window(config, cal, 60, mutate={15: lambda c, d: _seed_day(c, d, identity_broken=True)})
    summary = g4b._track_real(config, calendar=cal)
    assert summary["observation_progress"] == 15
    assert any("账务恒等式违规" in v for v in summary["violations"])


def test_negative_cash_breaks_streak(tmp_path: Path) -> None:
    """某日现金为负 → 中断。"""
    config = _config(tmp_path)
    cal = _weekday_calendar(60)
    _seed_ok_window(config, cal, 60, mutate={20: lambda c, d: _seed_day(c, d, negative_cash=True)})
    summary = g4b._track_real(config, calendar=cal)
    assert summary["observation_progress"] == 20
    assert any("负现金" in v for v in summary["violations"])


def test_manifest_tamper_breaks_streak(tmp_path: Path) -> None:
    """某日产物被篡改（manifest 哈希不可复算）→ 中断。"""
    config = _config(tmp_path)
    cal = _weekday_calendar(60)
    _seed_ok_window(config, cal, 60, mutate={25: lambda c, d: _seed_day(c, d, tamper_after_manifest=True)})
    summary = g4b._track_real(config, calendar=cal)
    assert summary["observation_progress"] == 25
    assert any("manifest 校验失败" in v for v in summary["violations"])


def test_no_records_not_started(tmp_path: Path) -> None:
    """无任何运行记录 → NOT STARTED（0/60），无违规。"""
    config = _config(tmp_path)
    cal = _weekday_calendar(60)
    summary = g4b._track_real(config, calendar=cal)
    assert summary["observation_progress"] == 0
    assert summary["real_success_trading_days"] == 0
    assert summary["violations"] == []


def test_calendar_unavailable_fail_closed(tmp_path: Path) -> None:
    """交易日历不可用 → fail-closed，产出 calendar_error 且进度为 0。"""
    config = _config(tmp_path)
    # 不注入日历、配置指向不存在的 parquet → load_trading_calendar 抛 CalendarUnavailableError
    summary = g4b._track_real(config, calendar=None)
    assert summary["observation_progress"] == 0
    assert "calendar_error" in summary
    assert "交易日历" in summary["calendar_error"]


def test_calendar_coverage_insufficient(tmp_path: Path) -> None:
    """日历覆盖不足 60 个交易日 → 如实标记 calendar_coverage，不假装达标。"""
    config = _config(tmp_path)
    cal = _weekday_calendar(40)  # 只有 40 个交易日
    _seed_ok_window(config, cal, 40)
    summary = g4b._track_real(config, calendar=cal)
    assert summary["observation_progress"] == 40
    assert summary["consecutive_trading_days"] == 40
    assert summary.get("calendar_coverage") is True
    assert summary["observation_progress"] < 60


def test_first_record_holiday_skipped_normalizes_start(tmp_path: Path) -> None:
    """最早运行记录是节假日（非交易日）产生的 SKIPPED 记录 → 起点归一化到下一交易日。

    若直接以该非交易日为第一个预期交易日，会因「非 SUCCESS」永久停在 0。
    """
    config = _config(tmp_path)
    cal = _weekday_calendar(60, start=date(2020, 1, 6))  # 周一起
    # 节假日（日历外的某天，如 2020-01-04 周六前的法定假）产生 SKIPPED 记录
    holiday = date(2020, 1, 3)  # 周五，不在合成日历内（模拟节假日）
    _seed_day(config, holiday, state=RunState.SKIPPED_NON_TRADING_DAY)
    # 之后 60 个工作日全部 SUCCESS
    _seed_ok_window(config, cal, 60, start=cal.dates[0])
    summary = g4b._track_real(config, calendar=cal)
    # 起点归一化到第一个交易日 2020-01-06，60 个工作日全部通过 → 60/60
    assert summary["start_date"] == "2020-01-06"
    assert summary["observation_progress"] == 60
    assert summary["consecutive_trading_days"] == 60
    assert summary["violations"] == []


def test_skipped_holiday_only_no_success_records(tmp_path: Path) -> None:
    """只有节假日 SKIPPED 记录、没有 SUCCESS 交易日 → 进度 0，且非交易日记录不判违规。"""
    config = _config(tmp_path)
    cal = _weekday_calendar(60, start=date(2020, 1, 6))
    holiday = date(2020, 1, 3)
    _seed_day(config, holiday, state=RunState.SKIPPED_NON_TRADING_DAY)
    summary = g4b._track_real(config, calendar=cal)
    assert summary["observation_progress"] == 0
    assert summary["real_success_trading_days"] == 0
    assert summary["start_date"] == "2020-01-06"  # 已归一化到下一交易日
    # 归一化后首个交易日（2020-01-06）无运行记录 → 运行记录缺失（违规中断），
    # 但节假日 SKIPPED 记录本身不是「非 SUCCESS」违规（它不在预期序列里）
    assert not any("非 SUCCESS" in v for v in summary["violations"])
    assert any("运行记录缺失" in v for v in summary["violations"])


def test_unique_key_and_order_id_use_independent_sets(tmp_path: Path) -> None:
    """unique_key 与 order_id 用独立集合：跨字段偶然同值不得误报为重复订单。"""
    config = _config(tmp_path)
    cal = _weekday_calendar(60)
    # 订单 A：unique_key=uk-a / order_id=oid-a
    # 订单 B：unique_key=oid-a（与 A 的 order_id 同值）/ order_id=uk-b
    # 若共用一个集合，会误判 A.order_id 与 B.unique_key 冲突 → 误报重复订单。
    orders = [
        {"order_id": "oid-a", "unique_key": "uk-a", "status": "FILLED"},
        {"order_id": "uk-b", "unique_key": "oid-a", "status": "FILLED"},
    ]

    def _two_orders(cfg, d):
        _seed_day(cfg, d, orders=orders)

    _seed_ok_window(config, cal, 60, mutate={5: _two_orders})
    summary = g4b._track_real(config, calendar=cal)
    # 无真正重复：unique_key 集合 {uk-a, oid-a} 与 order_id 集合 {oid-a, uk-b} 均唯一
    assert summary["observation_progress"] == 60
    assert summary["violations"] == []


def test_render_md_track_status_text(tmp_path: Path) -> None:
    """渲染的 Markdown 必须如实反映进度与复核口径。"""
    config = _config(tmp_path)
    cal = _weekday_calendar(60)
    _seed_ok_window(config, cal, 60)
    summary = g4b._track_real(config, calendar=cal)
    md = g4b._render_md(summary)
    assert "达标（60/60）" in md
    assert "按交易日历逐日复核" in md
