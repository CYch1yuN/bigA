"""回测核心数据模型。

所有金额使用 ``Decimal`` 累计，禁止二进制浮点直接参与现金与费用计算。
价格内部至少保留四位精度，报告金额保留两位。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any, Optional


class Side(str, Enum):
    """交易方向。"""

    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    """订单状态。"""

    PENDING = "PENDING"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"


class RejectReason(str, Enum):
    """拒绝原因。"""

    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
    INSUFFICIENT_POSITION = "INSUFFICIENT_POSITION"
    T_PLUS_ONE = "T_PLUS_ONE"
    LOT_SIZE = "LOT_SIZE"
    SUSPENDED = "SUSPENDED"
    LIMIT_UP = "LIMIT_UP"
    LIMIT_DOWN = "LIMIT_DOWN"
    MISSING_BAR = "MISSING_BAR"
    INVALID_PRICE = "INVALID_PRICE"
    UNIVERSE_FILTERED = "UNIVERSE_FILTERED"


@dataclass
class Signal:
    """交易信号：策略在收盘后生成，最早下一交易日开盘成交。

    Attributes:
        signal_date: 信号生成日（收盘后）。
        symbol: 股票代码。
        side: 交易方向。
        quantity: 请求数量（股），必须为正整数。
        reason: 信号理由（人类可读）。
    """

    signal_date: date
    symbol: str
    side: Side
    quantity: int
    reason: str = ""

    def __post_init__(self) -> None:
        if not self.symbol or not self.symbol.strip():
            raise ValueError("symbol 不能为空")
        if self.quantity <= 0:
            raise ValueError(f"quantity 必须为正整数，得到 {self.quantity}")


@dataclass
class Order:
    """订单：由信号生成，等待下一交易日开盘撮合。

    Attributes:
        signal: 关联的交易信号。
        planned_fill_date: 计划成交日（signal_date + 1 交易日）。
        order_id: 全局唯一标识，由 BacktestEngine 确定性生成。
        status: 当前状态。
        reject_reason: 拒绝原因（如有）。
        reject_detail: 拒绝详情（如有）。
        fill: 成交记录（如已成交）。
    """

    signal: Signal
    planned_fill_date: date
    order_id: str = ""
    status: OrderStatus = OrderStatus.PENDING
    reject_reason: Optional[RejectReason] = None
    reject_detail: Optional[str] = None
    fill: Optional[Fill] = None


@dataclass
class Fill:
    """成交记录。

    所有金额使用 Decimal。

    Attributes:
        order_id: 关联订单 ID。
        fill_date: 实际成交日。
        symbol: 股票代码。
        side: 交易方向。
        quantity: 成交数量（股）。
        raw_open_price: 原始开盘价（未复权）。
        slippage_price: 滑点后价格。
        commission: 佣金。
        stamp_duty: 印花税。
        transfer_fee: 过户费。
        total_cost: 总费用（佣金+印花税+过户费）。
        cash_change: 现金变化（买入为负，卖出为正）。
    """

    order_id: str
    fill_date: date
    symbol: str
    side: Side
    quantity: int
    raw_open_price: Decimal
    slippage_price: Decimal
    commission: Decimal
    stamp_duty: Decimal
    transfer_fee: Decimal
    total_cost: Decimal
    cash_change: Decimal

    @property
    def turnover(self) -> Decimal:
        """成交额 = 滑点后价格 × 数量。"""
        return self.slippage_price * Decimal(self.quantity)


@dataclass
class Position:
    """持仓记录。

    Attributes:
        symbol: 股票代码。
        total_quantity: 总持仓数量。
        sellable_quantity: 可卖数量（T+1 后解冻）。
        frozen_buy_quantity: 当日买入冻结数量。
        avg_raw_cost: 平均未复权成本（每股）。
    """

    symbol: str
    total_quantity: int = 0
    sellable_quantity: int = 0
    frozen_buy_quantity: int = 0
    avg_raw_cost: Decimal = field(default_factory=lambda: Decimal("0"))

    @property
    def has_position(self) -> bool:
        return self.total_quantity > 0


@dataclass
class PortfolioSnapshot:
    """每日组合快照。

    Attributes:
        snapshot_date: 快照日期。
        cash: 现金。
        position_value: 持仓市值（按未复权收盘价估值）。
        total_equity: 总权益 = 现金 + 持仓市值。
        daily_pnl: 当日收益。
        cumulative_pnl: 累计收益。
        drawdown: 当前回撤（相对历史最高权益）。
    """

    snapshot_date: date
    cash: Decimal
    position_value: Decimal
    total_equity: Decimal
    daily_pnl: Decimal = field(default_factory=lambda: Decimal("0"))
    cumulative_pnl: Decimal = field(default_factory=lambda: Decimal("0"))
    drawdown: Decimal = field(default_factory=lambda: Decimal("0"))


@dataclass
class EligibilityDecision:
    """股票池过滤决策。

    Attributes:
        eligible: 是否可交易。
        reason: 不可交易原因（如适用）。
    """

    eligible: bool
    reason: str = ""


@dataclass
class RiskDecision:
    """风控决策。

    Attributes:
        approved: 是否通过风控。
        reject_reason: 拒绝原因（如不通过）。
        reason: 人类可读理由。
    """

    approved: bool
    reject_reason: Optional[RejectReason] = None
    reason: str = ""


@dataclass
class BarData:
    """单日行情数据（传递给策略和风控）。

    分离前复权（信号用）和未复权（成交用）列。

    Attributes:
        symbol: 股票代码。
        trade_date: 交易日。
        open_raw: 未复权开盘价。
        high_raw: 未复权最高价。
        low_raw: 未复权最低价。
        close_raw: 未复权收盘价。
        open_qfq: 前复权开盘价。
        high_qfq: 前复权最高价。
        low_qfq: 前复权最低价。
        close_qfq: 前复权收盘价。
        volume: 成交量。
        amount: 成交额。
        is_suspended: 是否停牌。
        is_tradable: 是否可交易。
        prev_close_raw: 前一交易日未复权收盘价（用于涨跌停计算）。
    """

    symbol: str
    trade_date: date
    open_raw: Decimal
    high_raw: Decimal
    low_raw: Decimal
    close_raw: Decimal
    open_qfq: Decimal
    high_qfq: Decimal
    low_qfq: Decimal
    close_qfq: Decimal
    volume: float = 0.0
    amount: float = 0.0
    is_suspended: bool = False
    is_tradable: bool = True
    prev_close_raw: Optional[Decimal] = None


@dataclass
class StrategyContext:
    """策略上下文：传递给 Strategy.on_close。

    策略只能读取截至当日收盘的数据，不可访问未来切片。

    Attributes:
        current_date: 当日日期。
        portfolio: 当前组合快照。
        positions: 当前持仓字典 {symbol: Position}。
        bars: 截至当日的全部行情数据（DataFrame，已排序）。
        bars_up_to_date: 当日及之前的行情 DataFrame。
    """

    current_date: date
    portfolio: PortfolioSnapshot
    positions: dict[str, Position]
    bars_up_to_date: Any  # pd.DataFrame


@dataclass
class BacktestResult:
    """回测结果。

    Attributes:
        config_summary: 配置摘要。
        orders: 全部订单流水。
        fills: 全部成交流水。
        daily_equity: 每日权益快照列表。
        final_positions: 期末持仓字典。
        metrics: 绩效指标字典。
        limitations: 限制说明列表。
        data_range: 数据范围描述。
        content_hash: 内容哈希。
        code_commit: 代码提交号。
    """

    config_summary: dict[str, Any]
    orders: list[Order] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    daily_equity: list[PortfolioSnapshot] = field(default_factory=list)
    final_positions: dict[str, Position] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)
    data_range: dict[str, Any] = field(default_factory=dict)
    content_hash: Optional[str] = None
    code_commit: Optional[str] = None


def to_decimal(value: Any) -> Decimal:
    """安全转换为 Decimal，处理 float/str/Decimal 输入。"""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, int):
        return Decimal(value)
    return Decimal(str(value))


def quantize_price(value: Decimal, places: int = 4) -> Decimal:
    """价格量化到指定小数位（默认4位）。"""
    q = Decimal(10) ** (-places)
    return value.quantize(q)


def quantize_money(value: Decimal) -> Decimal:
    """金额量化到两位小数。"""
    return value.quantize(Decimal("0.01"))


__all__ = [
    "Side",
    "OrderStatus",
    "RejectReason",
    "Signal",
    "Order",
    "Fill",
    "Position",
    "PortfolioSnapshot",
    "EligibilityDecision",
    "RiskDecision",
    "BarData",
    "StrategyContext",
    "BacktestResult",
    "to_decimal",
    "quantize_price",
    "quantize_money",
]
