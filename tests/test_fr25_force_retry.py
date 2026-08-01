"""FR-25：``--force-retry`` 适用状态限制的回归测试。

规则一句话：**force-retry 只对"确实需要重试"的终态放行。**

允许集合固定为 ``FAILED`` / ``SKIPPED_DATA_UNAVAILABLE`` / ``BLOCKED_DATA_QUALITY``；
其余终态（尤其 ``SUCCESS``）一律拒绝，且拒绝时模拟账户、观察窗口与模拟订单
必须**逐字节不变**——这是审计可信度的底线，不是风格偏好。

判定顺序也被显式钉死：适用性检查发生在指纹比较**之前**，
改配置或换 commit 不能成为绕过闸门的后门。
"""
from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from ashare_quant.automation.models import (
    FORCE_RETRY_ALLOWED_STATES,
    FORCE_RETRY_REJECT_REASON,
    RunRecord,
    RunState,
    TaskType,
    force_retry_allowed,
)
from ashare_quant.automation.runner import AutomationRunner
from ashare_quant.automation.state import StateStore
from tests.test_phase4_automation import _synthetic


# ---------------------------------------------------------------------- #
# 允许集合本身
# ---------------------------------------------------------------------- #

def test_force_retry_allowed_states_is_exactly_three():
    """允许集合必须精确等于三个可重试终态，多一个少一个都算放宽。"""
    assert FORCE_RETRY_ALLOWED_STATES == frozenset(
        {
            RunState.FAILED,
            RunState.SKIPPED_DATA_UNAVAILABLE,
            RunState.BLOCKED_DATA_QUALITY,
        }
    )


@pytest.mark.parametrize(
    "state",
    [
        RunState.FAILED,
        RunState.SKIPPED_DATA_UNAVAILABLE,
        RunState.BLOCKED_DATA_QUALITY,
    ],
)
def test_force_retry_allowed_true(state):
    assert force_retry_allowed(state) is True


@pytest.mark.parametrize(
    "state",
    [
        RunState.SUCCESS,
        RunState.SKIPPED_NON_TRADING_DAY,
        RunState.BLOCKED_LOCKED,
        RunState.BLOCKED_NOT_ELIGIBLE,
        RunState.PENDING,
        RunState.RUNNING,
    ],
)
def test_force_retry_allowed_false(state):
    assert force_retry_allowed(state) is False


def test_success_is_never_force_retryable():
    """单独钉死最危险的一条：SUCCESS 永远不可强制重跑。"""
    assert RunState.SUCCESS not in FORCE_RETRY_ALLOWED_STATES


# ---------------------------------------------------------------------- #
# 编排器层：状态矩阵
# ---------------------------------------------------------------------- #

class _SentinelPipeline:
    """一旦被调用就说明闸门失守。"""

    def __init__(self) -> None:
        self.called = False

    def __call__(self, ctx) -> None:  # pragma: no cover - 期望永不执行
        self.called = True
        raise AssertionError("管线不应被执行：force-retry 应已在闸门处被拒绝")


def _runner(config, store) -> AutomationRunner:
    return AutomationRunner(
        config,
        task_type=TaskType.DAILY,
        state_store=store,
        now_fn=lambda: datetime(2020, 10, 2, 18, 30, 0),
    )


def _seed_record(runner: AutomationRunner, store: StateStore, *, as_of, state, attempt=1):
    """按给定终态写入一条既有运行记录，run_id 与当前指纹一致。"""
    store.ensure_dirs()
    fp = runner.build_fingerprint(as_of)
    record = RunRecord(
        run_id=fp.run_id,
        task_type=TaskType.DAILY,
        as_of_date=as_of,
        state=state,
        code_commit=fp.code_commit,
        config_hash=fp.config_hash,
        input_hash=fp.input_hash,
        started_at=datetime(2020, 10, 2, 18, 30, 0),
        finished_at=datetime(2020, 10, 2, 18, 31, 0),
        attempt=attempt,
        message=f"seed {state.value}",
    )
    store.save_run(record)
    return record


@pytest.mark.parametrize(
    "state",
    [
        RunState.SUCCESS,
        RunState.SKIPPED_NON_TRADING_DAY,
        RunState.BLOCKED_LOCKED,
        RunState.BLOCKED_NOT_ELIGIBLE,
    ],
)
def test_force_retry_rejected_for_disallowed_states(tmp_path, state):
    """不在允许集合内的终态：force-retry 被拒绝，管线一步都不执行。"""
    config, *_ = _synthetic(tmp_path)
    store = StateStore(config.state_dir)
    runner = _runner(config, store)
    as_of = date(2020, 10, 2)
    seeded = _seed_record(runner, store, as_of=as_of, state=state, attempt=3)

    pipeline = _SentinelPipeline()
    out = runner.run(pipeline, as_of_date=as_of, force_retry=True)

    assert pipeline.called is False
    assert out.force_retry_rejected is True
    assert out.reject_reason == FORCE_RETRY_REJECT_REASON
    assert out.state is state
    assert out.exit_code == seeded.exit_code
    assert out.reused is True
    # 既有记录未被改写：attempt 不递增、run_id 不变
    after = store.load_run(TaskType.DAILY, as_of)
    assert after is not None
    assert after.attempt == 3
    assert after.run_id == seeded.run_id
    assert after.state is state


@pytest.mark.parametrize(
    "state",
    [
        RunState.FAILED,
        RunState.SKIPPED_DATA_UNAVAILABLE,
        RunState.BLOCKED_DATA_QUALITY,
    ],
)
def test_force_retry_allowed_states_actually_rerun(tmp_path, state):
    """允许集合内的终态：force-retry 真正重跑，且 attempt 递增。"""
    config, *_ = _synthetic(tmp_path)
    store = StateStore(config.state_dir)
    runner = _runner(config, store)
    as_of = date(2020, 10, 2)
    _seed_record(runner, store, as_of=as_of, state=state, attempt=3)

    executed: list[str] = []

    def pipeline(ctx) -> None:
        executed.append("ran")
        with ctx.step("noop"):
            pass

    out = runner.run(pipeline, as_of_date=as_of, force_retry=True)

    assert executed == ["ran"]
    assert out.force_retry_rejected is False
    assert out.reject_reason is None
    assert out.state is RunState.SUCCESS
    assert out.record.attempt == 4, "force-retry 必须让 attempt 递增"


def test_non_force_rerun_of_failed_keeps_attempt(tmp_path):
    """同指纹 + 非 force：FAILED 仍可重跑，但 attempt 不递增。"""
    config, *_ = _synthetic(tmp_path)
    store = StateStore(config.state_dir)
    runner = _runner(config, store)
    as_of = date(2020, 10, 2)
    _seed_record(runner, store, as_of=as_of, state=RunState.FAILED, attempt=2)

    def pipeline(ctx) -> None:
        with ctx.step("noop"):
            pass

    out = runner.run(pipeline, as_of_date=as_of, force_retry=False)
    assert out.state is RunState.SUCCESS
    assert out.record.attempt == 2


# ---------------------------------------------------------------------- #
# 指纹变化不得成为后门
# ---------------------------------------------------------------------- #

def test_fingerprint_change_cannot_bypass_force_retry_gate(tmp_path):
    """改配置让指纹变化，仍然拒绝对 SUCCESS 的 force-retry。"""
    config, *_ = _synthetic(tmp_path)
    store = StateStore(config.state_dir)
    runner = _runner(config, store)
    as_of = date(2020, 10, 2)
    seeded = _seed_record(runner, store, as_of=as_of, state=RunState.SUCCESS, attempt=1)

    # 制造指纹变化：额外输入参与哈希
    pipeline = _SentinelPipeline()
    out = runner.run(
        pipeline,
        as_of_date=as_of,
        force_retry=True,
        extra_inputs={"tweak": "changed-after-success"},
    )

    new_fp = runner.build_fingerprint(
        as_of, extra_inputs={"tweak": "changed-after-success"}
    )
    assert new_fp.run_id != seeded.run_id, "前置条件：指纹确实变了"
    assert pipeline.called is False
    assert out.force_retry_rejected is True
    assert out.reject_reason == FORCE_RETRY_REJECT_REASON
    assert store.load_run(TaskType.DAILY, as_of).run_id == seeded.run_id


def test_fingerprint_change_alone_does_not_rerun_success(tmp_path):
    """非 force + 指纹变化：SUCCESS 记录仍复用，不重算不二次记账。"""
    config, *_ = _synthetic(tmp_path)
    store = StateStore(config.state_dir)
    runner = _runner(config, store)
    as_of = date(2020, 10, 2)
    seeded = _seed_record(runner, store, as_of=as_of, state=RunState.SUCCESS, attempt=1)

    pipeline = _SentinelPipeline()
    out = runner.run(
        pipeline,
        as_of_date=as_of,
        force_retry=False,
        extra_inputs={"tweak": "changed-after-success"},
    )

    assert pipeline.called is False
    assert out.reused is True
    assert out.force_retry_rejected is False
    assert out.reject_reason == "idempotent_reuse_fingerprint_changed"
    assert store.load_run(TaskType.DAILY, as_of).run_id == seeded.run_id


# ---------------------------------------------------------------------- #
# 端到端：拒绝后账户 / 观察窗口 / 订单原样不动
# ---------------------------------------------------------------------- #

def _account_fingerprint(config, store: StateStore) -> dict:
    snap = {}
    for acc in config.accounts:
        state = store.load_account(acc.account_id)
        assert state is not None
        snap[acc.account_id] = state.to_dict()
    return snap


def test_force_retry_on_success_leaves_accounts_untouched(tmp_path):
    """真实跑一天 -> SUCCESS -> force-retry 被拒 -> 账户三件套逐字段不变。"""
    from ashare_quant.automation.daily import DailyPipeline, run_daily

    config, source, cal, trade_dates, app_cfg, uk = _synthetic(tmp_path)
    store = StateStore(config.state_dir)
    day = trade_dates[-1]

    first = run_daily(
        config,
        as_of_date=day,
        data_source=source,
        pipeline=DailyPipeline(app_config=app_cfg, calendar=cal, universe_kwargs=uk),
        state_store=store,
    )
    assert first.state is RunState.SUCCESS

    before_accounts = _account_fingerprint(config, store)
    before_run = store.load_run(TaskType.DAILY, day).to_dict()
    orders_path = config.reports_dir / "daily" / day.isoformat() / "simulated-orders.json"
    before_orders = (
        orders_path.read_text(encoding="utf-8") if orders_path.exists() else None
    )

    second = run_daily(
        config,
        as_of_date=day,
        data_source=source,
        pipeline=DailyPipeline(app_config=app_cfg, calendar=cal, universe_kwargs=uk),
        state_store=store,
        force_retry=True,
    )

    assert second.force_retry_rejected is True
    assert second.reject_reason == FORCE_RETRY_REJECT_REASON
    assert second.state is RunState.SUCCESS
    assert second.exit_code == 0

    # 账户：现金、持仓、观察窗口、历史逐字段不变
    after_accounts = _account_fingerprint(config, store)
    assert after_accounts == before_accounts
    for acc_id, snap in after_accounts.items():
        assert snap["observation_days"] == before_accounts[acc_id]["observation_days"]
        assert snap["cash"] == before_accounts[acc_id]["cash"]

    # 运行记录：attempt 不递增
    after_run = store.load_run(TaskType.DAILY, day).to_dict()
    assert after_run == before_run

    # 模拟订单产物：内容不变
    after_orders = (
        orders_path.read_text(encoding="utf-8") if orders_path.exists() else None
    )
    assert after_orders == before_orders


def test_outcome_to_dict_exposes_rejection(tmp_path):
    """拒绝结论必须出现在结构化输出里，便于审计脚本抓取。"""
    config, *_ = _synthetic(tmp_path)
    store = StateStore(config.state_dir)
    runner = _runner(config, store)
    as_of = date(2020, 10, 2)
    _seed_record(runner, store, as_of=as_of, state=RunState.SUCCESS)

    out = runner.run(_SentinelPipeline(), as_of_date=as_of, force_retry=True)
    payload = json.loads(json.dumps(out.to_dict(), ensure_ascii=False, default=str))
    assert payload["force_retry_rejected"] is True
    assert payload["reject_reason"] == FORCE_RETRY_REJECT_REASON
    assert payload["alert"]["reason"] == FORCE_RETRY_REJECT_REASON
