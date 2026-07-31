"""A股成交模拟器。

实现 ``BrokerSimulator`` 接口，模拟 A 股「T+1、手数、涨跌停、佣金/印花税/过户费、滑点」
等真实交易约束的次日开盘价撮合。

核心规则
--------
1. **T+1**：当日买入的股票当日不可卖出，卖出数量受 ``Position.sellable_quantity`` 约束。
2. **手数**：申报数量必须为 ``config.lot_size``（默认 100 股）的整数倍。
3. **涨跌停**：基于 ``prev_close_raw`` 与板块涨跌停比例计算涨停/跌停价，并按 ``tick_size``
   向上/向下取整；保守撮合——开盘价触及涨停价时拒绝买入，触及跌停价时拒绝卖出
   （涨停允许卖出、跌停允许买入）。
4. **滑点**：买入按 ``open_raw * (1 + bps/10000)`` 向上取整到 tick，卖出按
   ``open_raw * (1 - bps/10000)`` 向下取整到 tick（恒向不利方向）。
5. **费用**：佣金 = max(成交额 * rate, minimum)；印花税仅卖出收取；过户费双向收取。
   所有金额使用 ``Decimal`` 累计，禁止二进制浮点直接参与现金与费用计算。

重要约束
--------
- 所有金额使用 ``Decimal``，禁止 float 直接累计。
- 价格内部至少 4 位精度，报告金额保留 2 位。
- 滑点后价格按 ``tick_size`` 向不利方向取整。
- 费率必须允许设为 0（即关闭某项费用）。
"""
from __future__ import annotations

import logging
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Optional

from .config import BacktestConfig
from .cost import compute_buy_cost, compute_sell_cost
from .interfaces import BrokerSimulator
from .models import (
    BarData,
    Fill,
    Order,
    PortfolioSnapshot,
    Position,
    RejectReason,
    RiskDecision,
    Side,
    quantize_money,
    quantize_price,
    to_decimal,
)

logger = logging.getLogger(__name__)

__all__ = ["AShareBrokerSimulator"]


class AShareBrokerSimulator(BrokerSimulator):
    """A股成交模拟器：次日开盘价撮合，含 T+1、手数、涨跌停、滑点与费用。"""

    # ------------------------------------------------------------------ #
    # 接口实现
    # ------------------------------------------------------------------ #
    def check_rejection(
        self,
        order: Order,
        bar: Optional[BarData],
        portfolio: PortfolioSnapshot,
        config: BacktestConfig,
        positions: dict[str, Position],
    ) -> Optional[RiskDecision]:
        """检查订单是否应被拒绝。

        检查顺序（命中即返回拒绝决策，未命中返回 ``None``）：

        1. 缺失 bar            -> ``MISSING_BAR``
        2. 无效价格            -> ``INVALID_PRICE``
        3. 停牌 / 不可交易     -> ``SUSPENDED``
        4. 数量非手数整数倍    -> ``LOT_SIZE``
        5. T+1 / 持仓不足      -> ``T_PLUS_ONE`` / ``INSUFFICIENT_POSITION``
        6. 涨跌停              -> ``LIMIT_UP`` / ``LIMIT_DOWN``
           （缺少 ``prev_close_raw`` 时标记 ``limit_check_unavailable`` 但不拒绝）

        Args:
            order: 待检查订单。
            bar: 成交日行情（可能为 ``None``）。
            portfolio: 当前组合快照。
            config: 回测配置。
            positions: 当前持仓字典。

        Returns:
            拒绝决策或 ``None``（通过）。
        """
        symbol = order.signal.symbol
        side = order.signal.side
        quantity = order.signal.quantity

        # 1. 缺失 bar -----------------------------------------------------
        if bar is None:
            return RiskDecision(
                approved=False,
                reject_reason=RejectReason.MISSING_BAR,
                reason=f"{symbol}: 缺失行情数据(bar=None)，无法撮合",
            )

        # 2. 无效价格 -----------------------------------------------------
        open_raw = to_decimal(bar.open_raw)
        if open_raw <= 0:
            return RiskDecision(
                approved=False,
                reject_reason=RejectReason.INVALID_PRICE,
                reason=f"{symbol}: 开盘价无效(open_raw={open_raw})",
            )

        # 3. 停牌 ---------------------------------------------------------
        if bar.is_suspended or not bar.is_tradable:
            return RiskDecision(
                approved=False,
                reject_reason=RejectReason.SUSPENDED,
                reason=(
                    f"{symbol}: 停牌或不可交易"
                    f"(is_suspended={bar.is_suspended}, is_tradable={bar.is_tradable})"
                ),
            )

        # 4. 手数 ---------------------------------------------------------
        lot_size = config.lot_size
        if lot_size > 0 and quantity % lot_size != 0:
            return RiskDecision(
                approved=False,
                reject_reason=RejectReason.LOT_SIZE,
                reason=f"{symbol}: 数量{quantity}不是手数{lot_size}的整数倍",
            )

        # 5. T+1 / 持仓不足（仅卖出） ------------------------------------
        if side == Side.SELL:
            position = positions.get(symbol)
            sellable = position.sellable_quantity if position else 0
            total = position.total_quantity if position else 0
            if sellable <= 0:
                if total <= 0:
                    return RiskDecision(
                        approved=False,
                        reject_reason=RejectReason.INSUFFICIENT_POSITION,
                        reason=f"{symbol}: 无持仓，不可卖出",
                    )
                return RiskDecision(
                    approved=False,
                    reject_reason=RejectReason.T_PLUS_ONE,
                    reason=f"{symbol}: 持仓{total}股但可卖0股，受T+1限制",
                )
            if sellable < quantity:
                return RiskDecision(
                    approved=False,
                    reject_reason=RejectReason.INSUFFICIENT_POSITION,
                    reason=f"{symbol}: 可卖{sellable}股不足订单{quantity}股",
                )

        # 6. 涨跌停 -------------------------------------------------------
        if bar.prev_close_raw is None:
            # 标记不可用但不拒绝：缺乏前收，无法计算涨跌停价
            logger.warning(
                "%s 涨跌停检查不可用(limit_check_unavailable): 缺少 prev_close_raw，"
                "跳过涨跌停校验",
                symbol,
            )
        else:
            prev_close = to_decimal(bar.prev_close_raw)
            ratio = self._select_limit_ratio(symbol, config)
            limit_up, limit_down = self._calc_limit_prices(
                prev_close, ratio, config.limit.tick_size
            )
            open_price = quantize_price(open_raw)
            # 保守撮合：涨停拒绝买入、跌停拒绝卖出；涨停允许卖出、跌停允许买入
            if side == Side.BUY and open_price >= limit_up:
                return RiskDecision(
                    approved=False,
                    reject_reason=RejectReason.LIMIT_UP,
                    reason=(
                        f"{symbol}: 开盘价{open_price} >= 涨停价{limit_up}，"
                        f"拒绝买入(涨停)"
                    ),
                )
            if side == Side.SELL and open_price <= limit_down:
                return RiskDecision(
                    approved=False,
                    reject_reason=RejectReason.LIMIT_DOWN,
                    reason=(
                        f"{symbol}: 开盘价{open_price} <= 跌停价{limit_down}，"
                        f"拒绝卖出(跌停)"
                    ),
                )

        return None

    def execute(
        self,
        order: Order,
        bar: BarData,
        portfolio: PortfolioSnapshot,
        config: BacktestConfig,
        positions: dict[str, Position],
    ) -> Optional[Fill]:
        """执行订单撮合，返回 ``Fill`` 或 ``None``（拒绝时）。

        步骤：
            1. 调用 ``check_rejection``，若被拒绝返回 ``None``。
            2. 使用统一成本函数计算滑点后价格、成交额、佣金、印花税、过户费、总费用、现金变化。
            3. 最终现金保护：如果真实 cash_change 会使现金 < 0，拒绝成交。
            4. 构造成交记录。

        Args:
            order: 待执行订单。
            bar: 成交日行情。
            portfolio: 当前组合快照。
            config: 回测配置。
            positions: 当前持仓。

        Returns:
            成交记录或 ``None``（被拒绝时）。
        """
        # 1. 风控/合规预检
        decision = self.check_rejection(order, bar, portfolio, config, positions)
        if decision is not None and not decision.approved:
            return None

        symbol = order.signal.symbol
        side = order.signal.side
        quantity = order.signal.quantity

        # 2. 统一成本计算（与风控预检共用同一函数）
        open_raw = to_decimal(bar.open_raw)
        if side == Side.BUY:
            cost = compute_buy_cost(open_raw, quantity, config)
        else:
            cost = compute_sell_cost(open_raw, quantity, config)

        # 3. 最终现金保护：买入时如果现金不足则拒单
        if side == Side.BUY:
            cash = to_decimal(portfolio.cash)
            required = cost.turnover + cost.total_cost
            if cash < required:
                logger.warning(
                    "%s: Broker 最终现金保护拒单: 需要 %.2f, 现金 %.2f",
                    symbol, required, cash,
                )
                return None

        raw_open_price = quantize_price(open_raw)

        # 4. 构造成交记录
        audit_flags: list[str] = []
        if bar.prev_close_raw is None:
            audit_flags.append("limit_check_unavailable")

        return Fill(
            order_id=order.order_id,
            fill_date=bar.trade_date,
            symbol=symbol,
            side=side,
            quantity=quantity,
            raw_open_price=raw_open_price,
            slippage_price=cost.slippage_price,
            commission=cost.commission,
            stamp_duty=cost.stamp_duty,
            transfer_fee=cost.transfer_fee,
            total_cost=cost.total_cost,
            cash_change=cost.cash_change,
            audit_flags=audit_flags,
        )

    # ------------------------------------------------------------------ #
    # 辅助方法
    # ------------------------------------------------------------------ #
    def _calc_limit_prices(
        self,
        prev_close: Decimal,
        ratio: float,
        tick_size: float,
    ) -> tuple[Decimal, Decimal]:
        """计算涨停价与跌停价。

        - 涨停价 = ``prev_close * (1 + ratio)``，向上取整到 ``tick_size``。
        - 跌停价 = ``prev_close * (1 - ratio)``，向下取整到 ``tick_size``。

        Args:
            prev_close: 前一交易日未复权收盘价。
            ratio: 涨跌停比例（如 0.10 表示 10%）。
            tick_size: 取整粒度（A 股通常 0.01 元）。

        Returns:
            ``(涨停价, 跌停价)``，均为 ``Decimal``。
        """
        ratio_dec = to_decimal(ratio)
        one = Decimal("1")
        limit_up_raw = prev_close * (one + ratio_dec)
        limit_down_raw = prev_close * (one - ratio_dec)
        limit_up = self._round_up_to_tick(limit_up_raw, tick_size)
        limit_down = self._round_down_to_tick(limit_down_raw, tick_size)
        return limit_up, limit_down

    @staticmethod
    def _round_up_to_tick(price: Decimal, tick: float) -> Decimal:
        """将价格向上取整到 ``tick`` 的整数倍。

        Args:
            price: 待取整价格。
            tick: 取整粒度（>0；<=0 时原样返回）。

        Returns:
            向上取整后的 ``Decimal`` 价格。
        """
        if tick <= 0:
            return price
        tick_dec = to_decimal(tick)
        quotient = (price / tick_dec).quantize(Decimal("1"), rounding=ROUND_CEILING)
        return quotient * tick_dec

    @staticmethod
    def _round_down_to_tick(price: Decimal, tick: float) -> Decimal:
        """将价格向下取整到 ``tick`` 的整数倍。

        Args:
            price: 待取整价格。
            tick: 取整粒度（>0；<=0 时原样返回）。

        Returns:
            向下取整后的 ``Decimal`` 价格。
        """
        if tick <= 0:
            return price
        tick_dec = to_decimal(tick)
        quotient = (price / tick_dec).quantize(Decimal("1"), rounding=ROUND_FLOOR)
        return quotient * tick_dec

    @staticmethod
    def _select_limit_ratio(symbol: str, config: BacktestConfig) -> float:
        """根据股票代码所属板块选择涨跌停比例。

        板块识别（按代码前缀）：

        - ``688xxx`` 科创板     -> ``star_ratio``  (默认 20%)
        - ``430xxx``/``83x``/``87x``/``920xxx`` 北交所 -> ``bjse_ratio`` (默认 30%)
        - ``00xxxx``/``30xxxx`` 深市主板/创业板 -> ``szse_ratio`` (默认 10%)
        - ``60xxxx`` 沪市主板   -> ``main_ratio``  (默认 10%)
        - 其它（默认）          -> ``main_ratio``

        .. note::
            ST/*ST 股票的 5% 涨跌停需依据股票名称判断，``BarData`` 暂未提供名称字段，
            故 ST 个股仍按其所属板块比例计算（后续可扩展）。

        Args:
            symbol: 股票代码。
            config: 回测配置。

        Returns:
            对应板块的涨跌停比例。
        """
        code = symbol.strip()
        # 科创板 688xxx
        if code.startswith("688"):
            return config.limit.star_ratio
        # 北交所 430xxx / 83xxxx / 87xxxx / 920xxx
        if code.startswith(("430", "83", "87", "920")):
            return config.limit.bjse_ratio
        # 深市主板 00xxxx、创业板 30xxxx
        if code.startswith(("00", "30")):
            return config.limit.szse_ratio
        # 沪市主板 60xxxx
        if code.startswith("60"):
            return config.limit.main_ratio
        # 默认主板比例
        return config.limit.main_ratio
