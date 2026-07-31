"""回测绩效指标计算。

所有金额使用 ``Decimal`` 累计，避免二进制浮点误差。
无法定义的指标返回 ``None``，不抛异常。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from .models import (
    BacktestResult,
    Fill,
    Order,
    OrderStatus,
    PortfolioSnapshot,
    Side,
)


class MetricsCalculator:
    """回测绩效指标计算器。

    使用方法::

        calc = MetricsCalculator()
        metrics = calc.calculate(result, initial_cash)
    """

    def calculate(
        self, result: BacktestResult, initial_cash: Decimal
    ) -> dict[str, Any]:
        """计算全部绩效指标。

        Args:
            result: 回测结果。
            initial_cash: 初始资金（Decimal）。

        Returns:
            指标字典，金额字段保留 Decimal 精度，无法定义的为 None。
        """
        daily_equity: list[PortfolioSnapshot] = result.daily_equity
        fills: list[Fill] = result.fills
        orders: list[Order] = result.orders

        trading_days = len(daily_equity)
        initial_equity: Decimal = initial_cash

        # 1. 初始权益
        # initial_equity 已赋值

        # 2. 最终权益
        final_equity: Decimal | None = (
            daily_equity[-1].total_equity if daily_equity else None
        )

        # 3. 总收益率
        if final_equity is not None and initial_equity != 0:
            total_return: Decimal | None = (
                final_equity - initial_equity
            ) / initial_equity
        else:
            total_return = None

        # 4. 年化收益率
        annualized_return = self._calc_annualized_return(
            initial_equity, final_equity, trading_days
        )

        # 5. 最大回撤
        max_drawdown = self._calc_max_drawdown(daily_equity)

        # 6. 交易次数
        total_trades = len(fills)

        # 7 & 8. 胜率与盈亏比
        win_rate, profit_loss_ratio = self._calc_win_loss(fills)

        # 9. 换手率
        total_turnover = sum((fill.turnover for fill in fills), Decimal("0"))
        if trading_days > 0 and final_equity is not None:
            avg_equity = (initial_equity + final_equity) / Decimal("2")
            if avg_equity != 0:
                turnover_rate: Decimal | None = (
                    total_turnover / avg_equity / Decimal(trading_days)
                )
            else:
                turnover_rate = None
        else:
            turnover_rate = None

        # 10. 不可成交订单率
        total_orders = len(orders)
        if total_orders == 0:
            unfillable_order_rate: Decimal = Decimal("0")
        else:
            unfillable = sum(
                1
                for o in orders
                if o.status in (OrderStatus.REJECTED, OrderStatus.CANCELLED)
            )
            unfillable_order_rate = Decimal(unfillable) / Decimal(total_orders)

        # 11. 拒绝原因计数
        reject_reason_counts = self._calc_reject_reason_counts(orders)

        # 12 / 13 / 14. 每日序列（日期存为 ISO 字符串，便于序列化）
        daily_cash = [
            {"date": snap.snapshot_date.isoformat(), "cash": snap.cash}
            for snap in daily_equity
        ]
        daily_position_value = [
            {
                "date": snap.snapshot_date.isoformat(),
                "position_value": snap.position_value,
            }
            for snap in daily_equity
        ]
        daily_equity_list = [
            {
                "date": snap.snapshot_date.isoformat(),
                "total_equity": snap.total_equity,
            }
            for snap in daily_equity
        ]

        return {
            "initial_equity": initial_equity,
            "final_equity": final_equity,
            "total_return": total_return,
            "annualized_return": annualized_return,
            "max_drawdown": max_drawdown,
            "total_trades": total_trades,
            "win_rate": win_rate,
            "profit_loss_ratio": profit_loss_ratio,
            "turnover_rate": turnover_rate,
            "unfillable_order_rate": unfillable_order_rate,
            "reject_reason_counts": reject_reason_counts,
            "daily_cash": daily_cash,
            "daily_position_value": daily_position_value,
            "daily_equity_list": daily_equity_list,
        }

    # ------------------------------------------------------------------
    # 内部计算辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _calc_annualized_return(
        initial_equity: Decimal,
        final_equity: Decimal | None,
        trading_days: int,
    ) -> Decimal | None:
        """年化收益率 = (final/initial) ** (252/days) - 1。"""
        if trading_days <= 0 or initial_equity == 0 or final_equity is None:
            return None
        growth_ratio = final_equity / initial_equity
        if growth_ratio <= 0:
            # 权益非正时分数幂无定义
            return None
        exponent = 252.0 / trading_days
        # Decimal 不支持分数幂，转 float 计算后回写 Decimal
        return Decimal(str(float(growth_ratio) ** exponent - 1))

    @staticmethod
    def _calc_max_drawdown(
        daily_equity: list[PortfolioSnapshot],
    ) -> Decimal | None:
        """最大回撤：历史最高点到后续最低点的跌幅比例。"""
        if not daily_equity:
            return None
        peak = daily_equity[0].total_equity
        max_dd = Decimal("0")
        for snap in daily_equity:
            equity = snap.total_equity
            if equity > peak:
                peak = equity
            if peak > 0:
                dd = (peak - equity) / peak
                if dd > max_dd:
                    max_dd = dd
        return max_dd

    @staticmethod
    def _calc_win_loss(
        fills: list[Fill],
    ) -> tuple[Decimal | None, Decimal | None]:
        """计算胜率与盈亏比。

        按成交日稳定排序后顺序处理，维护每个 symbol 的移动平均成本基础：
        - 买入：累计 acquisition cost = -cash_change（含费用）。
        - 卖出：盈亏 = 卖出净收入(cash_change) - 卖出数量对应的成本基础。

        Returns:
            (win_rate, profit_loss_ratio)，无卖出或无亏损时对应项为 None。
        """
        # 按成交日稳定排序，同日内保留原始顺序
        ordered = sorted(fills, key=lambda f: f.fill_date)

        cost_basis: dict[str, Decimal] = {}
        holding_qty: dict[str, int] = {}

        total_sells = 0
        wins = 0
        profits: list[Decimal] = []
        losses: list[Decimal] = []

        for fill in ordered:
            sym = fill.symbol
            if fill.side == Side.BUY:
                # cash_change 为负，取反得到含费用的买入总成本
                acquire_cost = -fill.cash_change
                cost_basis[sym] = cost_basis.get(sym, Decimal("0")) + acquire_cost
                holding_qty[sym] = holding_qty.get(sym, 0) + fill.quantity
            elif fill.side == Side.SELL:
                total_sells += 1
                held = holding_qty.get(sym, 0)
                if held > 0:
                    avg_cost = cost_basis.get(sym, Decimal("0")) / Decimal(held)
                    cost_basis_sold = avg_cost * Decimal(fill.quantity)
                    pnl = fill.cash_change - cost_basis_sold
                    # 按比例扣减剩余成本基础
                    cost_basis[sym] = (
                        cost_basis.get(sym, Decimal("0")) - cost_basis_sold
                    )
                    holding_qty[sym] = held - fill.quantity
                else:
                    # 无买入记录，无法确定成本，视为全部收入
                    pnl = fill.cash_change

                if pnl > 0:
                    wins += 1
                    profits.append(pnl)
                elif pnl < 0:
                    losses.append(-pnl)
                # pnl == 0 不计入盈亏统计

        if total_sells == 0:
            return None, None

        win_rate = Decimal(wins) / Decimal(total_sells)

        if not losses:
            profit_loss_ratio: Decimal | None = None
        else:
            avg_profit = (
                sum(profits, Decimal("0")) / Decimal(len(profits))
                if profits
                else Decimal("0")
            )
            avg_loss = sum(losses, Decimal("0")) / Decimal(len(losses))
            profit_loss_ratio = (
                avg_profit / avg_loss if avg_loss != 0 else None
            )

        return win_rate, profit_loss_ratio

    @staticmethod
    def _calc_reject_reason_counts(orders: list[Order]) -> dict[str, int]:
        """统计被拒绝/取消订单的拒绝原因分布。"""
        counts: dict[str, int] = {}
        for o in orders:
            if o.status in (OrderStatus.REJECTED, OrderStatus.CANCELLED):
                if o.reject_reason is None:
                    continue
                reason = (
                    o.reject_reason.value
                    if hasattr(o.reject_reason, "value")
                    else str(o.reject_reason)
                )
                counts[reason] = counts.get(reason, 0) + 1
        return counts


__all__ = ["MetricsCalculator"]
