"""默认风控管理器。

在信号进入撮合前进行逐条风控校验，任一检查不通过即拒绝并返回 :class:`RiskDecision`。
所有金额计算使用 ``Decimal``，禁止二进制浮点直接参与现金与费用计算。

校验顺序（任一不通过即拒绝，编号对应任务规约）：

1. 信号基本字段（signal_date / symbol / quantity > 0）
2. 数量为正整数且为 ``lot_size`` 整数倍 -> ``LOT_SIZE``
3. 单标的持仓开关（买入且已持有其他标的） -> 单持仓限制
4. 最大持仓市值比例（买入） -> ``INSUFFICIENT_CASH``（持仓比例超限）
5. 现金充足性（买入） -> ``INSUFFICIENT_CASH``
6. 可卖数量（卖出） -> ``T_PLUS_ONE`` / ``INSUFFICIENT_POSITION``
7. 停牌 / 不可交易 -> ``SUSPENDED``
8. 涨跌停 -> ``LIMIT_UP`` / ``LIMIT_DOWN``
9. 缺失行情 -> ``MISSING_BAR``
10. 无效价格 -> ``INVALID_PRICE``

当 ``bar is None`` 时仅执行 1、2、9 三项；3-8、10 均依赖行情数据。

注意：
- 第 5 项现金检查使用与 ``BrokerSimulator.execute`` 完全一致的统一成本计算函数
  :func:`~ashare_quant.backtest.cost.compute_buy_cost`，包含滑点后价格、
  最低佣金和过户费，确保风控预检与实际成交不会漂移。
- 涨跌停价计算（板块比例选择、按 tick 取整）镜像 ``broker.py`` 的实现，确保
  风控预检与成交撮合对涨跌停的判定完全一致，避免出现「风控拒绝但撮合本可成交」
  的假拒绝（例如科创板 20% 涨跌停被误按主板 10% 判定）。
"""
from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Optional

from .config import BacktestConfig
from .cost import compute_buy_cost, compute_sell_cost
from .interfaces import RiskManager
from .models import (
    BarData,
    PortfolioSnapshot,
    Position,
    RejectReason,
    RiskDecision,
    Side,
    Signal,
    quantize_money,
    quantize_price,
    to_decimal,
)

__all__ = ["DefaultRiskManager"]


class DefaultRiskManager(RiskManager):
    """默认风控管理器：按任务规约顺序对信号逐条校验。"""

    # ------------------------------------------------------------------ #
    # 接口实现
    # ------------------------------------------------------------------ #
    def validate(
        self,
        signal: Signal,
        portfolio: PortfolioSnapshot,
        bar: Optional[BarData],
        config: BacktestConfig,
        positions: dict[str, Position],
    ) -> RiskDecision:
        """验证信号是否通过风控。

        Args:
            signal: 交易信号。
            portfolio: 当前组合快照。
            bar: 当日行情（可能为 ``None``）。
            config: 回测配置。
            positions: 当前持仓字典。

        Returns:
            风控决策。
        """
        # 1. 信号基本字段（始终执行）
        basic = self._check_basic(signal)
        if basic is not None:
            return basic

        # 2. 手数（始终执行）
        lot = self._check_lot_size(signal, config)
        if lot is not None:
            return lot

        # 9. 缺失行情：bar 为空时无法继续后续检查
        if bar is None:
            return RiskDecision(
                approved=False,
                reject_reason=RejectReason.MISSING_BAR,
                reason=f"{signal.symbol}: 缺失当日行情数据(bar=None)",
            )

        # 以下检查依赖 bar
        if signal.side == Side.BUY:
            # 3. 单标的持仓限制
            single = self._check_single_position(signal, config, positions)
            if single is not None:
                return single
            # 4. 最大持仓市值比例
            ratio = self._check_position_ratio(
                signal, bar, portfolio, config, positions
            )
            if ratio is not None:
                return ratio
            # 5. 现金充足性
            cash = self._check_cash(signal, bar, portfolio, config)
            if cash is not None:
                return cash
        else:
            # 6. 可卖数量
            sellable = self._check_sellable(signal, positions)
            if sellable is not None:
                return sellable

        # 7. 停牌 / 不可交易
        if bar.is_suspended or not bar.is_tradable:
            return RiskDecision(
                approved=False,
                reject_reason=RejectReason.SUSPENDED,
                reason=(
                    f"{signal.symbol}: 停牌或不可交易"
                    f"(is_suspended={bar.is_suspended}, "
                    f"is_tradable={bar.is_tradable})"
                ),
            )

        # 8. 涨跌停
        limit = self._check_limit(signal, bar, config)
        if limit is not None:
            return limit

        # 10. 无效价格
        open_raw = to_decimal(bar.open_raw)
        if open_raw <= 0:
            return RiskDecision(
                approved=False,
                reject_reason=RejectReason.INVALID_PRICE,
                reason=f"{signal.symbol}: 无效开盘价(open_raw={open_raw})",
            )

        return RiskDecision(approved=True, reason=f"{signal.symbol}: 通过风控")

    # ------------------------------------------------------------------ #
    # 逐项检查
    # ------------------------------------------------------------------ #
    @staticmethod
    def _check_basic(signal: Signal) -> Optional[RiskDecision]:
        """1. 信号基本字段校验。"""
        if signal.signal_date is None:
            return RiskDecision(approved=False, reason="无效信号: signal_date 为空")
        if not signal.symbol or not signal.symbol.strip():
            return RiskDecision(approved=False, reason="无效信号: symbol 为空")
        if signal.quantity <= 0:
            return RiskDecision(
                approved=False,
                reason=f"无效信号: quantity={signal.quantity} 非正",
            )
        return None

    @staticmethod
    def _check_lot_size(
        signal: Signal, config: BacktestConfig
    ) -> Optional[RiskDecision]:
        """2. 数量必须为正整数且为 ``lot_size`` 的整数倍。"""
        lot_size = config.lot_size
        qty = signal.quantity
        if lot_size > 0 and (qty <= 0 or qty % lot_size != 0):
            return RiskDecision(
                approved=False,
                reject_reason=RejectReason.LOT_SIZE,
                reason=f"{signal.symbol}: 数量 {qty} 不是手数 {lot_size} 的整数倍",
            )
        return None

    @staticmethod
    def _check_single_position(
        signal: Signal,
        config: BacktestConfig,
        positions: dict[str, Position],
    ) -> Optional[RiskDecision]:
        """3. 单标的持仓开关：买入时若已持有其他标的则拒绝。"""
        if not config.risk.enable_single_position_limit:
            return None
        for sym, pos in positions.items():
            if sym != signal.symbol and pos.total_quantity > 0:
                return RiskDecision(
                    approved=False,
                    reject_reason=None,
                    reason=(
                        f"单持仓限制: 已持有 {sym}，不可再买入 {signal.symbol}"
                    ),
                )
        return None

    @staticmethod
    def _check_position_ratio(
        signal: Signal,
        bar: BarData,
        portfolio: PortfolioSnapshot,
        config: BacktestConfig,
        positions: dict[str, Position],
    ) -> Optional[RiskDecision]:
        """4. 买入后该 symbol 持仓市值占总权益比例不得超过阈值。"""
        max_ratio = to_decimal(config.risk.max_position_value_ratio)
        position = positions.get(signal.symbol)
        current_qty = position.total_quantity if position else 0
        post_buy_qty = current_qty + signal.quantity
        price = to_decimal(bar.open_raw)
        post_buy_value = to_decimal(post_buy_qty) * price
        total_equity = to_decimal(portfolio.total_equity)
        if total_equity <= 0:
            return RiskDecision(
                approved=False,
                reject_reason=RejectReason.INSUFFICIENT_CASH,
                reason=(
                    f"{signal.symbol}: 持仓比例超限: 总权益 {total_equity} 非正"
                ),
            )
        ratio = post_buy_value / total_equity
        if ratio > max_ratio:
            return RiskDecision(
                approved=False,
                reject_reason=RejectReason.INSUFFICIENT_CASH,
                reason=(
                    f"{signal.symbol}: 持仓比例超限: "
                    f"{ratio * 100:.2f}% > {max_ratio * 100:.2f}%"
                ),
            )
        return None

    @staticmethod
    def _check_cash(
        signal: Signal,
        bar: BarData,
        portfolio: PortfolioSnapshot,
        config: BacktestConfig,
    ) -> Optional[RiskDecision]:
        """5. 买入现金充足性：使用与 Broker 完全一致的成本计算。

        调用统一函数 ``compute_buy_cost`` 计算滑点后价格、成交额、佣金、
        过户费等全部费用，确保风控预检与实际成交不会漂移。
        """
        open_raw = to_decimal(bar.open_raw)
        cost = compute_buy_cost(open_raw, signal.quantity, config)
        required_cash = cost.turnover + cost.total_cost
        cash = to_decimal(portfolio.cash)
        if cash < required_cash:
            return RiskDecision(
                approved=False,
                reject_reason=RejectReason.INSUFFICIENT_CASH,
                reason=(
                    f"{signal.symbol}: 现金不足: 需要 {required_cash:.2f}, "
                    f"现有 {cash:.2f}"
                ),
            )
        return None

    @staticmethod
    def _check_sellable(
        signal: Signal, positions: dict[str, Position]
    ) -> Optional[RiskDecision]:
        """6. 卖出可卖数量检查：不足时区分 T+1 冻结与持仓不足。"""
        position = positions.get(signal.symbol)
        total_qty = position.total_quantity if position else 0
        sellable_qty = position.sellable_quantity if position else 0
        if sellable_qty < signal.quantity:
            if total_qty >= signal.quantity:
                # 总持仓足够但可卖不足：受 T+1 冻结
                return RiskDecision(
                    approved=False,
                    reject_reason=RejectReason.T_PLUS_ONE,
                    reason=(
                        f"{signal.symbol}: T+1 冻结: 可卖 {sellable_qty} < "
                        f"请求 {signal.quantity}"
                    ),
                )
            return RiskDecision(
                approved=False,
                reject_reason=RejectReason.INSUFFICIENT_POSITION,
                reason=(
                    f"{signal.symbol}: 持仓不足: 总持仓 {total_qty} < "
                    f"请求 {signal.quantity}"
                ),
            )
        return None

    # ------------------------------------------------------------------ #
    # 涨跌停（与 broker.py 保持一致）
    # ------------------------------------------------------------------ #
    def _check_limit(
        self, signal: Signal, bar: BarData, config: BacktestConfig
    ) -> Optional[RiskDecision]:
        """8. 涨跌停校验：涨停拒绝买入、跌停拒绝卖出；无 prev_close 时跳过。"""
        prev_close_raw = bar.prev_close_raw
        if prev_close_raw is None:
            # 无前收盘，无法计算涨跌停价，跳过
            return None
        prev_close = to_decimal(prev_close_raw)
        if prev_close <= 0:
            return None
        ratio = self._select_limit_ratio(signal.symbol, config)
        limit_up, limit_down = self._calc_limit_prices(
            prev_close, ratio, config.limit.tick_size
        )
        open_price = quantize_price(to_decimal(bar.open_raw))
        # 保守判定：涨停拒绝买入、跌停拒绝卖出；涨停允许卖出、跌停允许买入
        if signal.side == Side.BUY and open_price >= limit_up:
            return RiskDecision(
                approved=False,
                reject_reason=RejectReason.LIMIT_UP,
                reason=(
                    f"{signal.symbol}: 涨停无法买入: 开盘 {open_price} >= "
                    f"涨停价 {limit_up}"
                ),
            )
        if signal.side == Side.SELL and open_price <= limit_down:
            return RiskDecision(
                approved=False,
                reject_reason=RejectReason.LIMIT_DOWN,
                reason=(
                    f"{signal.symbol}: 跌停无法卖出: 开盘 {open_price} <= "
                    f"跌停价 {limit_down}"
                ),
            )
        return None

    def _calc_limit_prices(
        self, prev_close: Decimal, ratio: float, tick_size: float
    ) -> tuple[Decimal, Decimal]:
        """计算涨停价（向上取整到 tick）与跌停价（向下取整到 tick）。"""
        ratio_dec = to_decimal(ratio)
        one = Decimal("1")
        limit_up = self._round_up_to_tick(
            prev_close * (one + ratio_dec), tick_size
        )
        limit_down = self._round_down_to_tick(
            prev_close * (one - ratio_dec), tick_size
        )
        return limit_up, limit_down

    @staticmethod
    def _select_limit_ratio(symbol: str, config: BacktestConfig) -> float:
        """根据代码前缀选择板块涨跌停比例（镜像 ``AShareBrokerSimulator``）。

        - ``688xxx`` 科创板 -> ``star_ratio`` (默认 20%)
        - ``430xxx``/``83x``/``87x``/``920xxx`` 北交所 -> ``bjse_ratio`` (默认 30%)
        - ``00xxxx``/``30xxxx`` 深市主板/创业板 -> ``szse_ratio`` (默认 10%)
        - ``60xxxx`` 沪市主板 -> ``main_ratio`` (默认 10%)
        - 其它 -> ``main_ratio``

        .. note::
            ST/*ST 个股的 5% 涨跌停需依据股票名称判断，``BarData`` 暂未提供名称字段，
            故 ST 个股仍按其所属板块比例计算。
        """
        code = symbol.strip()
        if code.startswith("688"):
            return config.limit.star_ratio
        if code.startswith(("430", "83", "87", "920")):
            return config.limit.bjse_ratio
        if code.startswith(("00", "30")):
            return config.limit.szse_ratio
        if code.startswith("60"):
            return config.limit.main_ratio
        return config.limit.main_ratio

    @staticmethod
    def _round_up_to_tick(price: Decimal, tick: float) -> Decimal:
        """将价格向上取整到 ``tick`` 的整数倍。"""
        if tick <= 0:
            return price
        tick_dec = to_decimal(tick)
        quotient = (price / tick_dec).quantize(Decimal("1"), rounding=ROUND_CEILING)
        return quotient * tick_dec

    @staticmethod
    def _round_down_to_tick(price: Decimal, tick: float) -> Decimal:
        """将价格向下取整到 ``tick`` 的整数倍。"""
        if tick <= 0:
            return price
        tick_dec = to_decimal(tick)
        quotient = (price / tick_dec).quantize(Decimal("1"), rounding=ROUND_FLOOR)
        return quotient * tick_dec
