"""Phase 4 自动化数据模型：运行状态机、运行记录、模拟订单与模拟账户。

设计要点：

- 状态机共 9 个状态（1 个初始 + 1 个进行中 + 7 个终态），终态一旦写入不可回退。
- 崩溃语义：``RUNNING`` 不是终态，恢复时必须显式判定为 ``FAILED``，
  绝不能把半成品运行标记为 ``SUCCESS``。
- 所有金额字段使用 ``Decimal``（复用 Phase 2 的 ``to_decimal`` / ``quantize_money``），
  禁止使用 float 参与资金计算。
- 所有数据类均可 ``to_dict()`` / ``from_dict()`` 往返，便于原子 JSON 落盘。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Optional

from ..backtest.models import quantize_money, quantize_price, to_decimal

__all__ = [
    "TaskType",
    "RunState",
    "TERMINAL_STATES",
    "BLOCKED_STATES",
    "SKIPPED_STATES",
    "FORCE_RETRY_ALLOWED_STATES",
    "FORCE_RETRY_REJECT_REASON",
    "force_retry_allowed",
    "EXIT_CODES",
    "exit_code_for",
    "StrategyTrack",
    "EligibilityStatus",
    "TRACK_ELIGIBILITY",
    "StepStatus",
    "StepResult",
    "RunRecord",
    "LockInfo",
    "SimulatedOrderRecord",
    "SimulatedPosition",
    "SimulatedAccountState",
    "AutomationError",
    "CalendarUnavailableError",
    "DataQualityBlockedError",
    "LockHeldError",
    "NotEligibleError",
    "signal_hash",
    "order_unique_key",
]


# ---------------------------------------------------------------------- #
# 异常
# ---------------------------------------------------------------------- #


class AutomationError(Exception):
    """Phase 4 自动化基础异常。"""


class CalendarUnavailableError(AutomationError):
    """交易日历缺失、过期或不覆盖目标日期（fail-closed）。"""


class DataQualityBlockedError(AutomationError):
    """数据质量闸门未通过（存在 critical 问题）。"""


class LockHeldError(AutomationError):
    """已有活跃运行持有锁。"""


class NotEligibleError(AutomationError):
    """检测到试图启用实盘交易，被资格闸门拒绝。"""


# ---------------------------------------------------------------------- #
# 枚举
# ---------------------------------------------------------------------- #


class TaskType(str, Enum):
    """自动化任务类型。"""

    DAILY = "daily"
    WEEKLY = "weekly"


class RunState(str, Enum):
    """运行状态机。

    状态流转（唯一合法路径）::

        PENDING -> RUNNING -> {SUCCESS, FAILED,
                               SKIPPED_NON_TRADING_DAY,
                               SKIPPED_DATA_UNAVAILABLE,
                               BLOCKED_DATA_QUALITY,
                               BLOCKED_LOCKED,
                               BLOCKED_NOT_ELIGIBLE}

    ``BLOCKED_LOCKED`` 可以直接从 ``PENDING`` 进入（未拿到锁，从未真正开始运行）。
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED_NON_TRADING_DAY = "SKIPPED_NON_TRADING_DAY"
    SKIPPED_DATA_UNAVAILABLE = "SKIPPED_DATA_UNAVAILABLE"
    BLOCKED_DATA_QUALITY = "BLOCKED_DATA_QUALITY"
    BLOCKED_LOCKED = "BLOCKED_LOCKED"
    BLOCKED_NOT_ELIGIBLE = "BLOCKED_NOT_ELIGIBLE"


TERMINAL_STATES: frozenset[RunState] = frozenset(
    {
        RunState.SUCCESS,
        RunState.FAILED,
        RunState.SKIPPED_NON_TRADING_DAY,
        RunState.SKIPPED_DATA_UNAVAILABLE,
        RunState.BLOCKED_DATA_QUALITY,
        RunState.BLOCKED_LOCKED,
        RunState.BLOCKED_NOT_ELIGIBLE,
    }
)

BLOCKED_STATES: frozenset[RunState] = frozenset(
    {
        RunState.BLOCKED_DATA_QUALITY,
        RunState.BLOCKED_LOCKED,
        RunState.BLOCKED_NOT_ELIGIBLE,
    }
)

SKIPPED_STATES: frozenset[RunState] = frozenset(
    {
        RunState.SKIPPED_NON_TRADING_DAY,
        RunState.SKIPPED_DATA_UNAVAILABLE,
    }
)

FORCE_RETRY_ALLOWED_STATES: frozenset[RunState] = frozenset(
    {
        RunState.FAILED,
        RunState.SKIPPED_DATA_UNAVAILABLE,
        RunState.BLOCKED_DATA_QUALITY,
    }
)
"""``--force-retry`` 唯一允许作用的既有终态集合（FR-25）。

为什么只有这三个：

- ``FAILED``：运行确实没跑完，重试是正当诉求。
  中断恢复（残留 ``RUNNING``）会被显式判定为 ``FAILED``，因此天然落入本集合。
- ``SKIPPED_DATA_UNAVAILABLE``：数据源当时不可用，补数后重试是正当诉求。
- ``BLOCKED_DATA_QUALITY``：质量闸门当时拦截，修数后重试是正当诉求。

明确排除：

- ``SUCCESS``：该业务日已产生模拟成交、账户变动与观察窗口计数。
  强制重跑会二次改写资金与观察窗口，属于**审计事故**，必须拒绝。
- ``SKIPPED_NON_TRADING_DAY``：不是交易日，重跑不会有任何新结论。
- ``BLOCKED_LOCKED``：另一实例正在跑，正确处置是等待而不是抢跑。
- ``BLOCKED_NOT_ELIGIBLE``：安全边界拒绝，重试不得用于绕过边界。

对被排除的状态，常规（非 force）重跑路径依旧可用——因为它们本就不是
``SUCCESS``，不会命中幂等复用分支。``--force-retry`` 只是不再充当万能钥匙。
"""

FORCE_RETRY_REJECT_REASON: str = "force_retry_not_applicable"
"""force-retry 被拒绝时写入日志与运行结果的稳定原因码。"""


def force_retry_allowed(state: RunState) -> bool:
    """判断某个既有终态是否允许 ``--force-retry``。"""
    return state in FORCE_RETRY_ALLOWED_STATES


EXIT_CODES: dict[RunState, int] = {
    RunState.SUCCESS: 0,
    RunState.SKIPPED_NON_TRADING_DAY: 0,
    RunState.SKIPPED_DATA_UNAVAILABLE: 0,
    RunState.FAILED: 1,
    RunState.BLOCKED_DATA_QUALITY: 3,
    RunState.BLOCKED_LOCKED: 4,
    RunState.BLOCKED_NOT_ELIGIBLE: 5,
    # 非终态出现在退出路径上一律视为异常
    RunState.PENDING: 1,
    RunState.RUNNING: 1,
}


def exit_code_for(state: RunState) -> int:
    """返回状态对应的进程退出码。"""
    return EXIT_CODES.get(state, 1)


class StrategyTrack(str, Enum):
    """策略轨道。"""

    STEADY = "steady"
    AGGRESSIVE = "aggressive"


class EligibilityStatus(str, Enum):
    """Phase 3 研究得出的资格结论（Phase 4 不得放宽）。"""

    NOT_ELIGIBLE_FOR_LIVE_TRADING = "NOT_ELIGIBLE_FOR_LIVE_TRADING"
    SIMULATION_ONLY = "SIMULATION_ONLY"


TRACK_ELIGIBILITY: dict[StrategyTrack, EligibilityStatus] = {
    StrategyTrack.STEADY: EligibilityStatus.NOT_ELIGIBLE_FOR_LIVE_TRADING,
    StrategyTrack.AGGRESSIVE: EligibilityStatus.SIMULATION_ONLY,
}
"""轨道 -> 资格结论的硬编码映射。

这是 Phase 3 复审确认的结论，属于安全边界，禁止通过配置覆盖。
"""


class StepStatus(str, Enum):
    """单个步骤的执行结果。"""

    OK = "OK"
    SKIPPED = "SKIPPED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"


# ---------------------------------------------------------------------- #
# 时间序列化辅助
# ---------------------------------------------------------------------- #


def _dt_to_str(value: Optional[datetime]) -> Optional[str]:
    return None if value is None else value.isoformat(timespec="seconds")


def _str_to_dt(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _d_to_str(value: Optional[date]) -> Optional[str]:
    return None if value is None else value.isoformat()


def _str_to_d(value: Any) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


# ---------------------------------------------------------------------- #
# 运行记录
# ---------------------------------------------------------------------- #


@dataclass
class StepResult:
    """单步执行结果。

    Attributes:
        name: 步骤名称（稳定标识，用于跨运行比对）。
        status: 步骤状态。
        started_at: 开始时间。
        finished_at: 结束时间。
        detail: 结构化细节（禁止写入任何凭据）。
        error: 错误摘要（异常类型 + 消息，不含堆栈中的敏感路径）。
    """

    name: str
    status: StepStatus = StepStatus.OK
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    detail: dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None

    @property
    def duration_seconds(self) -> Optional[float]:
        """步骤耗时（秒）。"""
        if self.started_at is None or self.finished_at is None:
            return None
        return round((self.finished_at - self.started_at).total_seconds(), 6)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "started_at": _dt_to_str(self.started_at),
            "finished_at": _dt_to_str(self.finished_at),
            "duration_seconds": self.duration_seconds,
            "detail": self.detail,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "StepResult":
        return cls(
            name=str(raw["name"]),
            status=StepStatus(raw.get("status", "OK")),
            started_at=_str_to_dt(raw.get("started_at")),
            finished_at=_str_to_dt(raw.get("finished_at")),
            detail=dict(raw.get("detail") or {}),
            error=raw.get("error"),
        )


@dataclass
class RunRecord:
    """一次自动化运行的完整记录。

    Attributes:
        run_id: 确定性运行标识（见 ``idempotency.compute_run_id``）。
        task_type: 任务类型。
        as_of_date: 业务基准日（信号日 / 观察日）。
        state: 当前状态。
        code_commit: 代码提交号。
        config_hash: 配置哈希。
        input_hash: 输入数据哈希。
        started_at / finished_at: 运行时间。
        attempt: 第几次尝试（``rerun --force-retry`` 递增）。
        steps: 步骤结果列表。
        message: 人类可读结论。
        artifacts: 产物相对路径列表。
        schema_version: 记录 schema 版本。
    """

    run_id: str
    task_type: TaskType
    as_of_date: date
    state: RunState = RunState.PENDING
    code_commit: str = "no-git"
    config_hash: str = ""
    input_hash: str = ""
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    attempt: int = 1
    steps: list[StepResult] = field(default_factory=list)
    message: str = ""
    artifacts: list[str] = field(default_factory=list)
    schema_version: int = 1

    # -- 状态操作 ------------------------------------------------------ #

    @property
    def is_terminal(self) -> bool:
        """当前状态是否为终态。"""
        return self.state in TERMINAL_STATES

    @property
    def exit_code(self) -> int:
        """对应的进程退出码。"""
        return exit_code_for(self.state)

    @property
    def duration_seconds(self) -> Optional[float]:
        if self.started_at is None or self.finished_at is None:
            return None
        return round((self.finished_at - self.started_at).total_seconds(), 6)

    def add_step(self, step: StepResult) -> StepResult:
        """追加步骤结果（同名步骤覆盖，保证重跑幂等）。"""
        for idx, existing in enumerate(self.steps):
            if existing.name == step.name:
                self.steps[idx] = step
                return step
        self.steps.append(step)
        return step

    def step(self, name: str) -> Optional[StepResult]:
        """按名称获取步骤结果。"""
        for existing in self.steps:
            if existing.name == name:
                return existing
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "task_type": self.task_type.value,
            "as_of_date": _d_to_str(self.as_of_date),
            "state": self.state.value,
            "exit_code": self.exit_code,
            "code_commit": self.code_commit,
            "config_hash": self.config_hash,
            "input_hash": self.input_hash,
            "started_at": _dt_to_str(self.started_at),
            "finished_at": _dt_to_str(self.finished_at),
            "duration_seconds": self.duration_seconds,
            "attempt": self.attempt,
            "message": self.message,
            "artifacts": list(self.artifacts),
            "steps": [s.to_dict() for s in self.steps],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RunRecord":
        as_of = _str_to_d(raw.get("as_of_date"))
        if as_of is None:
            raise ValueError("RunRecord.as_of_date 不可为空")
        return cls(
            run_id=str(raw["run_id"]),
            task_type=TaskType(raw["task_type"]),
            as_of_date=as_of,
            state=RunState(raw.get("state", "PENDING")),
            code_commit=str(raw.get("code_commit", "no-git")),
            config_hash=str(raw.get("config_hash", "")),
            input_hash=str(raw.get("input_hash", "")),
            started_at=_str_to_dt(raw.get("started_at")),
            finished_at=_str_to_dt(raw.get("finished_at")),
            attempt=int(raw.get("attempt", 1)),
            steps=[StepResult.from_dict(s) for s in raw.get("steps", [])],
            message=str(raw.get("message", "")),
            artifacts=list(raw.get("artifacts", [])),
            schema_version=int(raw.get("schema_version", 1)),
        )


@dataclass
class LockInfo:
    """运行锁内容。"""

    pid: int
    hostname: str
    task_type: TaskType
    as_of_date: date
    started_at: datetime
    code_commit: str = "no-git"
    run_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "hostname": self.hostname,
            "task_type": self.task_type.value,
            "as_of_date": _d_to_str(self.as_of_date),
            "started_at": _dt_to_str(self.started_at),
            "code_commit": self.code_commit,
            "run_id": self.run_id,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "LockInfo":
        started = _str_to_dt(raw.get("started_at"))
        as_of = _str_to_d(raw.get("as_of_date"))
        if started is None or as_of is None:
            raise ValueError("锁文件缺少 started_at / as_of_date")
        return cls(
            pid=int(raw["pid"]),
            hostname=str(raw.get("hostname", "")),
            task_type=TaskType(raw["task_type"]),
            as_of_date=as_of,
            started_at=started,
            code_commit=str(raw.get("code_commit", "no-git")),
            run_id=str(raw.get("run_id", "")),
        )


# ---------------------------------------------------------------------- #
# 模拟订单与模拟账户
# ---------------------------------------------------------------------- #


def signal_hash(
    *,
    symbol: str,
    side: str,
    quantity: int,
    reason: str,
    signal_date: date,
    strategy_track: str,
) -> str:
    """计算研究信号的稳定哈希（用于订单唯一约束）。"""
    payload = "|".join(
        [
            strategy_track,
            signal_date.isoformat(),
            symbol,
            side,
            str(int(quantity)),
            reason,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def order_unique_key(
    *,
    account_id: str,
    signal_date: date,
    symbol: str,
    side: str,
    strategy_track: str,
    sig_hash: str,
) -> str:
    """模拟订单唯一约束键。

    约束元组：``(account_id, signal_date, symbol, side, strategy_track, signal_hash)``。
    同一元组在同一账户下只允许存在一条订单记录，保证重跑不会重复下单。
    """
    return "|".join(
        [account_id, signal_date.isoformat(), symbol, side, strategy_track, sig_hash]
    )


@dataclass
class SimulatedOrderRecord:
    """模拟订单记录（研究用途，非券商可导入格式）。

    刻意不包含券商下单所需字段（如账户号、交易单元、委托类型代码），
    以确保该记录**无法**被直接用于实盘下单。
    """

    account_id: str
    strategy_track: StrategyTrack
    signal_date: date
    fill_date: Optional[date]
    symbol: str
    side: str
    quantity: int
    signal_hash: str
    status: str = "PENDING"
    reject_reason: Optional[str] = None
    reason: str = ""
    fill_price: Optional[Decimal] = None
    raw_open_price: Optional[Decimal] = None
    commission: Decimal = Decimal("0")
    stamp_duty: Decimal = Decimal("0")
    transfer_fee: Decimal = Decimal("0")
    total_cost: Decimal = Decimal("0")
    cash_change: Decimal = Decimal("0")
    turnover: Decimal = Decimal("0")
    audit_flags: list[str] = field(default_factory=list)
    eligibility_status: EligibilityStatus = (
        EligibilityStatus.NOT_ELIGIBLE_FOR_LIVE_TRADING
    )
    simulated: bool = True

    @property
    def unique_key(self) -> str:
        return order_unique_key(
            account_id=self.account_id,
            signal_date=self.signal_date,
            symbol=self.symbol,
            side=self.side,
            strategy_track=self.strategy_track.value,
            sig_hash=self.signal_hash,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "strategy_track": self.strategy_track.value,
            "signal_date": _d_to_str(self.signal_date),
            "fill_date": _d_to_str(self.fill_date),
            "symbol": self.symbol,
            "side": self.side,
            "quantity": int(self.quantity),
            "signal_hash": self.signal_hash,
            "unique_key": self.unique_key,
            "status": self.status,
            "reject_reason": self.reject_reason,
            "reason": self.reason,
            "fill_price": _dec_str(self.fill_price),
            "raw_open_price": _dec_str(self.raw_open_price),
            "commission": _dec_str(self.commission),
            "stamp_duty": _dec_str(self.stamp_duty),
            "transfer_fee": _dec_str(self.transfer_fee),
            "total_cost": _dec_str(self.total_cost),
            "cash_change": _dec_str(self.cash_change),
            "turnover": _dec_str(self.turnover),
            "audit_flags": list(self.audit_flags),
            "eligibility_status": self.eligibility_status.value,
            "simulated": self.simulated,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SimulatedOrderRecord":
        signal_date = _str_to_d(raw.get("signal_date"))
        if signal_date is None:
            raise ValueError("SimulatedOrderRecord.signal_date 不可为空")
        return cls(
            account_id=str(raw["account_id"]),
            strategy_track=StrategyTrack(raw["strategy_track"]),
            signal_date=signal_date,
            fill_date=_str_to_d(raw.get("fill_date")),
            symbol=str(raw["symbol"]),
            side=str(raw["side"]),
            quantity=int(raw.get("quantity", 0)),
            signal_hash=str(raw.get("signal_hash", "")),
            status=str(raw.get("status", "PENDING")),
            reject_reason=raw.get("reject_reason"),
            reason=str(raw.get("reason", "")),
            fill_price=_opt_dec(raw.get("fill_price")),
            raw_open_price=_opt_dec(raw.get("raw_open_price")),
            commission=to_decimal(raw.get("commission", 0)),
            stamp_duty=to_decimal(raw.get("stamp_duty", 0)),
            transfer_fee=to_decimal(raw.get("transfer_fee", 0)),
            total_cost=to_decimal(raw.get("total_cost", 0)),
            cash_change=to_decimal(raw.get("cash_change", 0)),
            turnover=to_decimal(raw.get("turnover", 0)),
            audit_flags=list(raw.get("audit_flags", [])),
            eligibility_status=EligibilityStatus(
                raw.get(
                    "eligibility_status",
                    EligibilityStatus.NOT_ELIGIBLE_FOR_LIVE_TRADING.value,
                )
            ),
            simulated=bool(raw.get("simulated", True)),
        )


@dataclass
class SimulatedPosition:
    """模拟持仓（含 T+1 可卖数量）。"""

    symbol: str
    total_quantity: int = 0
    sellable_quantity: int = 0
    frozen_buy_quantity: int = 0
    avg_raw_cost: Decimal = Decimal("0")

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "total_quantity": int(self.total_quantity),
            "sellable_quantity": int(self.sellable_quantity),
            "frozen_buy_quantity": int(self.frozen_buy_quantity),
            "avg_raw_cost": _dec_str(self.avg_raw_cost),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SimulatedPosition":
        return cls(
            symbol=str(raw["symbol"]),
            total_quantity=int(raw.get("total_quantity", 0)),
            sellable_quantity=int(raw.get("sellable_quantity", 0)),
            frozen_buy_quantity=int(raw.get("frozen_buy_quantity", 0)),
            avg_raw_cost=to_decimal(raw.get("avg_raw_cost", 0)),
        )


@dataclass
class SimulatedAccountState:
    """模拟账户状态（持久化于 state 目录）。

    Attributes:
        account_id: 账户标识（``paper-steady`` / ``paper-aggressive``）。
        strategy_track: 对应策略轨道。
        eligibility_status: 资格结论标记（随账户一起持久化，报告必带）。
        initial_cash: 初始资金。
        cash: 当前现金。
        positions: 当前持仓。
        as_of_date: 账户状态对应的业务日。
        last_run_id: 最近一次更新该账户的运行标识。
        observation_days: 已观察交易日计数。
        history: 每日权益快照（date / cash / position_value / total_equity）。
        processed_order_keys: 已处理订单唯一键集合（幂等）。
    """

    account_id: str
    strategy_track: StrategyTrack
    eligibility_status: EligibilityStatus
    initial_cash: Decimal = Decimal("1000.00")
    cash: Decimal = Decimal("1000.00")
    positions: dict[str, SimulatedPosition] = field(default_factory=dict)
    as_of_date: Optional[date] = None
    last_run_id: str = ""
    observation_days: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)
    processed_order_keys: list[str] = field(default_factory=list)
    schema_version: int = 1

    def position_value(self, prices: dict[str, Decimal]) -> Decimal:
        """按给定收盘价计算持仓市值。"""
        total = Decimal("0")
        for symbol, pos in self.positions.items():
            price = prices.get(symbol)
            if price is None or pos.total_quantity <= 0:
                continue
            total += quantize_money(price * Decimal(pos.total_quantity))
        return quantize_money(total)

    def total_equity(self, prices: dict[str, Decimal]) -> Decimal:
        """总权益 = 现金 + 持仓市值。"""
        return quantize_money(self.cash + self.position_value(prices))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "account_id": self.account_id,
            "strategy_track": self.strategy_track.value,
            "eligibility_status": self.eligibility_status.value,
            "initial_cash": _dec_str(self.initial_cash),
            "cash": _dec_str(self.cash),
            "positions": [p.to_dict() for p in self.positions.values()],
            "as_of_date": _d_to_str(self.as_of_date),
            "last_run_id": self.last_run_id,
            "observation_days": int(self.observation_days),
            "history": list(self.history),
            "processed_order_keys": list(self.processed_order_keys),
            "simulated": True,
            "live_trading": False,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "SimulatedAccountState":
        positions = {
            str(p["symbol"]): SimulatedPosition.from_dict(p)
            for p in raw.get("positions", [])
        }
        return cls(
            account_id=str(raw["account_id"]),
            strategy_track=StrategyTrack(raw["strategy_track"]),
            eligibility_status=EligibilityStatus(raw["eligibility_status"]),
            initial_cash=to_decimal(raw.get("initial_cash", 1000)),
            cash=to_decimal(raw.get("cash", 1000)),
            positions=positions,
            as_of_date=_str_to_d(raw.get("as_of_date")),
            last_run_id=str(raw.get("last_run_id", "")),
            observation_days=int(raw.get("observation_days", 0)),
            history=list(raw.get("history", [])),
            processed_order_keys=list(raw.get("processed_order_keys", [])),
            schema_version=int(raw.get("schema_version", 1)),
        )


# ---------------------------------------------------------------------- #
# Decimal 序列化辅助
# ---------------------------------------------------------------------- #


def _dec_str(value: Optional[Decimal]) -> Optional[str]:
    """Decimal -> 字符串（保留精度，避免 float 往返误差）。"""
    if value is None:
        return None
    return str(value)


def _opt_dec(value: Any) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    return to_decimal(value)


# 供其他模块直接使用 Phase 2 的量化函数
quantize_money = quantize_money  # noqa: PLW0127  (re-export)
quantize_price = quantize_price  # noqa: PLW0127  (re-export)
to_decimal = to_decimal  # noqa: PLW0127  (re-export)
