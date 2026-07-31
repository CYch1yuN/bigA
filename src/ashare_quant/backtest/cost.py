"""统一买卖成本计算。

风控预检与实际成交 **必须** 调用同一函数计算成本，禁止各自维护可能漂移的公式。

所有金额使用 ``Decimal``，价格内部至少 4 位精度，报告金额保留 2 位。
"""
from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import NamedTuple

from .config import BacktestConfig
from .models import Side, quantize_money, quantize_price, to_decimal

__all__ = ["CostBreakdown", "compute_buy_cost", "compute_sell_cost"]


class CostBreakdown(NamedTuple):
    """成本分解结果——风控预检与实际成交共用。

    所有字段已按精度规则量化（价格 4 位、金额 2 位）。
    """

    slippage_price: Decimal
    turnover: Decimal
    commission: Decimal
    stamp_duty: Decimal
    transfer_fee: Decimal
    total_cost: Decimal
    cash_change: Decimal


def _round_up_to_tick(price: Decimal, tick: float) -> Decimal:
    """将价格向上取整到 ``tick`` 的整数倍。"""
    if tick <= 0:
        return price
    tick_dec = to_decimal(tick)
    quotient = (price / tick_dec).quantize(Decimal("1"), rounding=ROUND_CEILING)
    return quotient * tick_dec


def _round_down_to_tick(price: Decimal, tick: float) -> Decimal:
    """将价格向下取整到 ``tick`` 的整数倍。"""
    if tick <= 0:
        return price
    tick_dec = to_decimal(tick)
    quotient = (price / tick_dec).quantize(Decimal("1"), rounding=ROUND_FLOOR)
    return quotient * tick_dec


def compute_buy_cost(
    open_raw: Decimal,
    quantity: int,
    config: BacktestConfig,
) -> CostBreakdown:
    """计算买入侧的精确成本（含滑点、佣金、过户费）。

    买入不收印花税。滑点向上取整到 tick。

    Args:
        open_raw: 未复权开盘价。
        quantity: 买入数量（股）。
        config: 回测配置。

    Returns:
        :class:`CostBreakdown`，``cash_change`` 为负数。
    """
    bps = to_decimal(config.slippage.bps)
    tick = config.slippage.tick_size
    bps_factor = bps / Decimal("10000")

    # 滑点后价格（向上取整到 tick）
    slip_raw = open_raw * (Decimal("1") + bps_factor)
    slippage_price = quantize_price(_round_up_to_tick(slip_raw, tick))

    # 成交额
    turnover = slippage_price * Decimal(quantity)

    # 佣金 = max(成交额 * rate, minimum)
    commission_rate = to_decimal(config.commission.rate)
    commission_min = to_decimal(config.commission.minimum)
    commission = turnover * commission_rate
    if commission < commission_min:
        commission = commission_min
    commission = quantize_money(commission)

    # 印花税：买入 0
    stamp_duty = Decimal("0")

    # 过户费（双向）
    transfer_fee = quantize_money(
        turnover * to_decimal(config.transfer_fee.rate)
    )

    # 总费用
    total_cost = quantize_money(commission + stamp_duty + transfer_fee)

    # 现金变化：买入 = -(成交额 + 总费用)
    cash_change = quantize_money(-(turnover + total_cost))

    return CostBreakdown(
        slippage_price=slippage_price,
        turnover=turnover,
        commission=commission,
        stamp_duty=stamp_duty,
        transfer_fee=transfer_fee,
        total_cost=total_cost,
        cash_change=cash_change,
    )


def compute_sell_cost(
    open_raw: Decimal,
    quantity: int,
    config: BacktestConfig,
) -> CostBreakdown:
    """计算卖出侧的精确成本（含滑点、佣金、印花税、过户费）。

    滑点向下取整到 tick。

    Args:
        open_raw: 未复权开盘价。
        quantity: 卖出数量（股）。
        config: 回测配置。

    Returns:
        :class:`CostBreakdown`，``cash_change`` 为正数。
    """
    bps = to_decimal(config.slippage.bps)
    tick = config.slippage.tick_size
    bps_factor = bps / Decimal("10000")

    # 滑点后价格（向下取整到 tick）
    slip_raw = open_raw * (Decimal("1") - bps_factor)
    slippage_price = quantize_price(_round_down_to_tick(slip_raw, tick))

    # 成交额
    turnover = slippage_price * Decimal(quantity)

    # 佣金
    commission_rate = to_decimal(config.commission.rate)
    commission_min = to_decimal(config.commission.minimum)
    commission = turnover * commission_rate
    if commission < commission_min:
        commission = commission_min
    commission = quantize_money(commission)

    # 印花税：卖出收取
    stamp_duty = quantize_money(
        turnover * to_decimal(config.stamp_duty.rate)
    )

    # 过户费（双向）
    transfer_fee = quantize_money(
        turnover * to_decimal(config.transfer_fee.rate)
    )

    # 总费用
    total_cost = quantize_money(commission + stamp_duty + transfer_fee)

    # 现金变化：卖出 = 成交额 - 总费用
    cash_change = quantize_money(turnover - total_cost)

    return CostBreakdown(
        slippage_price=slippage_price,
        turnover=turnover,
        commission=commission,
        stamp_duty=stamp_duty,
        transfer_fee=transfer_fee,
        total_cost=total_cost,
        cash_change=cash_change,
    )
