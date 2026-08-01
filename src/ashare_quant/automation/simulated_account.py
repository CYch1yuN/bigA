"""Phase 4 模拟账户：把研究信号落到**纸面账户**上，永远不接触真实资金。

设计边界（不可绕过）
--------------------
1. 本模块产出的一切都是 **模拟订单 / 模拟持仓 / 模拟权益**。
   ``SimulatedOrderRecord.simulated`` 恒为 ``True``，
   ``SimulatedAccountState.live_trading`` 恒为 ``False``。
2. 账户的资格结论由 ``TRACK_ELIGIBILITY`` 硬编码决定：
   稳健轨 ``NOT_ELIGIBLE_FOR_LIVE_TRADING``、激进轨 ``SIMULATION_ONLY``。
   任何试图写入其它值的账户状态在加载时即被拒绝。
3. 若配置中出现 ``live_trading.enabled=true`` 或非空券商端点，
   直接抛 ``NotEligibleError``，不做任何撮合。
4. 订单记录刻意不含券商下单所需字段（账户号、交易单元、委托类型码），
   无法被直接导入任何交易终端。

记账口径
--------
完全复用 Phase 2：

- 撮合：``AShareBrokerSimulator``（次日开盘价 + 滑点 + 涨跌停 + T+1 + 手数 + 费用）。
- 风控：``DefaultRiskManager``（信号级校验，先于撮合）。
- 持仓更新：与 ``BacktestEngine._update_position`` 逐行一致
  （买入进冻结、加权成本按滑点价累计、卖出扣可卖、清仓归零）。
- 资金：全程 ``Decimal``，禁止 float 参与。

幂等性
------
每条信号计算 ``signal_hash``，再与账户/日期/标的/方向/轨道组成
``order_unique_key``。凡是**已决定**的键（成交或拒单）都写入
``processed_order_keys``，同一业务日重跑不会重复扣款、重复建仓。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Iterable, Optional

from ..backtest.broker import AShareBrokerSimulator
from ..backtest.config import BacktestConfig
from ..backtest.models import (
    BarData,
    Fill,
    Order,
    OrderStatus,
    PortfolioSnapshot,
    Position,
    Side,
    Signal,
    quantize_money,
    quantize_price,
    to_decimal,
)
from ..backtest.risk import DefaultRiskManager
from .config import AccountConfig, AutomationConfig
from .models import (
    TRACK_ELIGIBILITY,
    EligibilityStatus,
    NotEligibleError,
    SimulatedAccountState,
    SimulatedOrderRecord,
    SimulatedPosition,
    StrategyTrack,
    order_unique_key,
    signal_hash,
)

__all__ = [
    "ORDER_STATUS_FILLED",
    "ORDER_STATUS_REJECTED",
    "ORDER_STATUS_DUPLICATE",
    "AccountUpdateResult",
    "SimulatedAccountManager",
    "assert_simulation_only",
    "position_view",
    "SIMULATION_DISCLAIMER",
]


ORDER_STATUS_FILLED = "FILLED"
ORDER_STATUS_REJECTED = "REJECTED"
ORDER_STATUS_DUPLICATE = "SKIPPED_DUPLICATE"

#: 报告与日志中反复出现的免责声明，任何账户产物都必须带上。
SIMULATION_DISCLAIMER = (
    "本账户为研究用模拟账户，所有订单与持仓均为纸面记录，"
    "不构成投资建议，也不构成任何实盘交易授权。"
)


# ---------------------------------------------------------------------- #
# 安全闸门
# ---------------------------------------------------------------------- #


def assert_simulation_only(config: AutomationConfig) -> None:
    """确认当前配置处于纯模拟模式，否则拒绝运行。

    Args:
        config: 自动化配置。

    Raises:
        NotEligibleError: 配置试图开启实盘或配置了券商端点。
    """
    live = config.live_trading
    if getattr(live, "enabled", False):
        raise NotEligibleError(
            "live_trading.enabled=true 被拒绝：Phase 4 仅允许模拟账户，"
            "Phase 3 结论未授权任何实盘交易"
        )
    endpoint = (getattr(live, "broker_endpoint", "") or "").strip()
    if endpoint:
        raise NotEligibleError(
            "检测到券商端点配置，已拒绝运行：本系统禁止连接任何券商或真实账户"
        )


def _assert_account_integrity(state: SimulatedAccountState) -> None:
    """校验账户状态未被手工篡改成"可实盘"。"""
    expected = TRACK_ELIGIBILITY[state.strategy_track]
    if state.eligibility_status is not expected:
        raise NotEligibleError(
            f"账户 {state.account_id} 资格标记异常："
            f"期望 {expected.value}，实际 {state.eligibility_status.value}"
        )


# ---------------------------------------------------------------------- #
# 持仓视图转换
# ---------------------------------------------------------------------- #


def _to_position(sp: SimulatedPosition) -> Position:
    """模拟持仓 -> Phase 2 持仓（供风控与撮合读取）。"""
    return Position(
        symbol=sp.symbol,
        total_quantity=int(sp.total_quantity),
        sellable_quantity=int(sp.sellable_quantity),
        frozen_buy_quantity=int(sp.frozen_buy_quantity),
        avg_raw_cost=to_decimal(sp.avg_raw_cost),
    )


def _from_position(p: Position) -> SimulatedPosition:
    """Phase 2 持仓 -> 模拟持仓（供落盘）。"""
    return SimulatedPosition(
        symbol=p.symbol,
        total_quantity=int(p.total_quantity),
        sellable_quantity=int(p.sellable_quantity),
        frozen_buy_quantity=int(p.frozen_buy_quantity),
        avg_raw_cost=quantize_price(to_decimal(p.avg_raw_cost)),
    )


def position_view(state: SimulatedAccountState) -> dict[str, Position]:
    """把模拟持仓转换为 Phase 2 ``Position`` 视图。

    策略上下文与风控读取的都是这个视图，保证"策略看到的持仓"与
    "账本记的持仓"永远是同一份事实。
    """
    return {sym: _to_position(sp) for sym, sp in state.positions.items()}


#: 内部旧名保留，避免既有调用点大改。
_position_view = position_view


def _apply_position_view(
    state: SimulatedAccountState, positions: dict[str, Position]
) -> None:
    """把 Phase 2 持仓视图写回账户状态，顺带清理空仓。"""
    rebuilt: dict[str, SimulatedPosition] = {}
    for sym, p in positions.items():
        if p.total_quantity <= 0:
            continue
        rebuilt[sym] = _from_position(p)
    state.positions = rebuilt


def _update_position(positions: dict[str, Position], fill: Fill) -> None:
    """按成交更新持仓，逐行对齐 ``BacktestEngine._update_position``。"""
    symbol = fill.symbol
    if symbol not in positions:
        positions[symbol] = Position(symbol=symbol)

    pos = positions[symbol]
    qty = int(fill.quantity)
    price = to_decimal(fill.slippage_price)

    if fill.side == Side.BUY:
        old_total_cost = to_decimal(pos.avg_raw_cost) * Decimal(pos.total_quantity)
        new_total_cost = old_total_cost + price * Decimal(qty)
        pos.total_quantity += qty
        pos.frozen_buy_quantity += qty  # T+1：当日买入冻结
        if pos.total_quantity > 0:
            pos.avg_raw_cost = new_total_cost / Decimal(pos.total_quantity)
    elif fill.side == Side.SELL:
        pos.total_quantity -= qty
        pos.sellable_quantity -= qty
        if pos.total_quantity <= 0:
            pos.total_quantity = 0
            pos.sellable_quantity = 0
            pos.frozen_buy_quantity = 0
            pos.avg_raw_cost = Decimal("0")


# ---------------------------------------------------------------------- #
# 更新结果
# ---------------------------------------------------------------------- #


@dataclass
class AccountUpdateResult:
    """一次账户推进的完整结果（可直接序列化进报告）。"""

    account_id: str
    strategy_track: StrategyTrack
    eligibility_status: EligibilityStatus
    as_of_date: date
    run_id: str
    orders: list[SimulatedOrderRecord] = field(default_factory=list)
    cash_before: Decimal = Decimal("0")
    cash_after: Decimal = Decimal("0")
    position_value: Decimal = Decimal("0")
    total_equity: Decimal = Decimal("0")
    observation_days: int = 0
    rolled_over: bool = False
    simulated: bool = True
    live_trading: bool = False
    disclaimer: str = SIMULATION_DISCLAIMER

    @property
    def filled(self) -> list[SimulatedOrderRecord]:
        return [o for o in self.orders if o.status == ORDER_STATUS_FILLED]

    @property
    def rejected(self) -> list[SimulatedOrderRecord]:
        return [o for o in self.orders if o.status == ORDER_STATUS_REJECTED]

    @property
    def duplicates(self) -> list[SimulatedOrderRecord]:
        return [o for o in self.orders if o.status == ORDER_STATUS_DUPLICATE]

    def counts(self) -> dict[str, int]:
        return {
            "total": len(self.orders),
            "filled": len(self.filled),
            "rejected": len(self.rejected),
            "skipped_duplicate": len(self.duplicates),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "strategy_track": self.strategy_track.value,
            "eligibility_status": self.eligibility_status.value,
            "as_of_date": self.as_of_date.isoformat(),
            "run_id": self.run_id,
            "counts": self.counts(),
            "orders": [o.to_dict() for o in self.orders],
            "cash_before": str(quantize_money(self.cash_before)),
            "cash_after": str(quantize_money(self.cash_after)),
            "position_value": str(quantize_money(self.position_value)),
            "total_equity": str(quantize_money(self.total_equity)),
            "observation_days": int(self.observation_days),
            "rolled_over": bool(self.rolled_over),
            "simulated": True,
            "live_trading": False,
            "disclaimer": self.disclaimer,
        }


# ---------------------------------------------------------------------- #
# 账户管理器
# ---------------------------------------------------------------------- #


class SimulatedAccountManager:
    """模拟账户管理器：创建、推进、估值、快照。

    该类**不**负责持久化——落盘交给 ``StateStore``，
    这样单元测试可以在纯内存中推进账户。
    """

    def __init__(
        self,
        config: AutomationConfig,
        backtest_config: BacktestConfig,
        *,
        broker: Optional[AShareBrokerSimulator] = None,
        risk_manager: Optional[DefaultRiskManager] = None,
    ) -> None:
        assert_simulation_only(config)
        self.config = config
        self.backtest_config = backtest_config
        self.broker = broker or AShareBrokerSimulator()
        self.risk = risk_manager or DefaultRiskManager()

    # ------------------------------------------------------------------ #
    # 账户生命周期
    # ------------------------------------------------------------------ #
    def create_account(self, account_cfg: AccountConfig) -> SimulatedAccountState:
        """按配置创建全新模拟账户。"""
        cash = quantize_money(to_decimal(account_cfg.initial_cash))
        state = SimulatedAccountState(
            account_id=account_cfg.account_id,
            strategy_track=account_cfg.track,
            eligibility_status=TRACK_ELIGIBILITY[account_cfg.track],
            initial_cash=cash,
            cash=cash,
        )
        _assert_account_integrity(state)
        return state

    def ensure_account(
        self,
        account_cfg: AccountConfig,
        existing: Optional[SimulatedAccountState] = None,
    ) -> SimulatedAccountState:
        """返回可用账户：已有则校验后复用，否则新建。"""
        if existing is None:
            return self.create_account(account_cfg)
        _assert_account_integrity(existing)
        if existing.strategy_track is not account_cfg.track:
            raise NotEligibleError(
                f"账户 {existing.account_id} 轨道不一致："
                f"配置 {account_cfg.track.value}，状态 {existing.strategy_track.value}"
            )
        return existing

    # ------------------------------------------------------------------ #
    # T+1 换日
    # ------------------------------------------------------------------ #
    @staticmethod
    def rollover(state: SimulatedAccountState, business_date: date) -> bool:
        """跨交易日解冻 T+1 持仓。

        仅当业务日**严格晚于**账户当前日期时执行，保证同日重跑不会
        把当天买入的股票误解冻。

        Returns:
            是否真的执行了解冻。
        """
        if state.as_of_date is not None and business_date <= state.as_of_date:
            return False
        for pos in state.positions.values():
            if pos.frozen_buy_quantity > 0:
                pos.sellable_quantity += pos.frozen_buy_quantity
                pos.frozen_buy_quantity = 0
        return True

    # ------------------------------------------------------------------ #
    # 估值
    # ------------------------------------------------------------------ #
    @staticmethod
    def close_prices(bars: dict[str, BarData]) -> dict[str, Decimal]:
        """从行情提取未复权收盘价（估值口径与 Phase 2 一致）。"""
        return {sym: to_decimal(bar.close_raw) for sym, bar in bars.items()}

    def snapshot(
        self,
        state: SimulatedAccountState,
        snapshot_date: date,
        prices: dict[str, Decimal],
    ) -> PortfolioSnapshot:
        """构造 Phase 2 组合快照（供风控与撮合读取）。"""
        cash = to_decimal(state.cash)
        pos_value = state.position_value(prices)
        return PortfolioSnapshot(
            snapshot_date=snapshot_date,
            cash=quantize_money(cash),
            position_value=quantize_money(pos_value),
            total_equity=quantize_money(cash + pos_value),
        )

    # ------------------------------------------------------------------ #
    # 核心：推进一个业务日
    # ------------------------------------------------------------------ #
    def apply_signals(
        self,
        state: SimulatedAccountState,
        *,
        signals: Iterable[Signal],
        bars: dict[str, BarData],
        fill_date: date,
        run_id: str,
        count_observation_day: bool = True,
    ) -> AccountUpdateResult:
        """把研究信号推进到模拟账户。

        执行顺序：资格闸门 -> T+1 换日 -> 逐信号(幂等检查 -> 风控 -> 撮合 -> 落账)
        -> 估值 -> 权益快照。

        Args:
            state: 账户状态（原地更新）。
            signals: 待处理研究信号（通常来自前一交易日收盘后）。
            bars: 成交日行情，键为标的代码。
            fill_date: 撮合日（信号日的下一交易日）。
            run_id: 触发本次更新的运行标识。
            count_observation_day: 是否计入观察窗口交易日计数。

        Returns:
            本次推进的结果快照。

        Raises:
            NotEligibleError: 账户资格标记被篡改。
        """
        assert_simulation_only(self.config)
        _assert_account_integrity(state)

        cash_before = to_decimal(state.cash)
        rolled = self.rollover(state, fill_date)

        positions = _position_view(state)
        processed = set(state.processed_order_keys)
        prices = self.close_prices(bars)
        records: list[SimulatedOrderRecord] = []

        for sig in self._ordered(signals):
            sig_hash = signal_hash(
                symbol=sig.symbol,
                side=sig.side.value if isinstance(sig.side, Side) else str(sig.side),
                quantity=int(sig.quantity),
                reason=sig.reason or "",
                signal_date=sig.signal_date,
                strategy_track=state.strategy_track.value,
            )
            side_str = sig.side.value if isinstance(sig.side, Side) else str(sig.side)
            key = order_unique_key(
                account_id=state.account_id,
                signal_date=sig.signal_date,
                symbol=sig.symbol,
                side=side_str,
                strategy_track=state.strategy_track.value,
                sig_hash=sig_hash,
            )

            base = dict(
                account_id=state.account_id,
                strategy_track=state.strategy_track,
                signal_date=sig.signal_date,
                symbol=sig.symbol,
                side=side_str,
                quantity=int(sig.quantity),
                signal_hash=sig_hash,
                reason=sig.reason or "",
                eligibility_status=state.eligibility_status,
            )

            # 1. 幂等：同一唯一键只处理一次
            if key in processed:
                records.append(
                    SimulatedOrderRecord(
                        fill_date=None,
                        status=ORDER_STATUS_DUPLICATE,
                        reject_reason="DUPLICATE_ORDER_KEY",
                        **base,
                    )
                )
                continue

            bar = bars.get(sig.symbol)
            portfolio = self.snapshot(state, fill_date, prices)

            # 2. 风控（信号级）
            decision = self.risk.validate(
                sig, portfolio, bar, self.backtest_config, positions
            )
            if not decision.approved:
                records.append(
                    SimulatedOrderRecord(
                        fill_date=None,
                        status=ORDER_STATUS_REJECTED,
                        reject_reason=(
                            decision.reject_reason.value
                            if decision.reject_reason is not None
                            else "RISK_REJECTED"
                        ),
                        **{**base, "reason": decision.reason or base["reason"]},
                    )
                )
                processed.add(key)
                continue

            # 3. 撮合（次日开盘价）
            order = Order(
                signal=sig,
                planned_fill_date=fill_date,
                order_id=f"{state.account_id}:{sig_hash}",
                status=OrderStatus.PENDING,
            )
            broker_reject = self.broker.check_rejection(
                order, bar, portfolio, self.backtest_config, positions
            )
            if broker_reject is not None and not broker_reject.approved:
                records.append(
                    SimulatedOrderRecord(
                        fill_date=None,
                        status=ORDER_STATUS_REJECTED,
                        reject_reason=(
                            broker_reject.reject_reason.value
                            if broker_reject.reject_reason is not None
                            else "BROKER_REJECTED"
                        ),
                        **{**base, "reason": broker_reject.reason or base["reason"]},
                    )
                )
                processed.add(key)
                continue

            assert bar is not None  # check_rejection 已保证 bar 存在
            fill = self.broker.execute(
                order, bar, portfolio, self.backtest_config, positions
            )
            if fill is None:
                records.append(
                    SimulatedOrderRecord(
                        fill_date=None,
                        status=ORDER_STATUS_REJECTED,
                        reject_reason="BROKER_REJECTED",
                        **{
                            **base,
                            "reason": "撮合阶段被拒（现金保护或最终校验未通过）",
                        },
                    )
                )
                processed.add(key)
                continue

            # 4. 落账：现金 + 持仓
            state.cash = quantize_money(to_decimal(state.cash) + fill.cash_change)
            _update_position(positions, fill)
            prices.setdefault(fill.symbol, to_decimal(bar.close_raw))

            records.append(
                SimulatedOrderRecord(
                    fill_date=fill.fill_date,
                    status=ORDER_STATUS_FILLED,
                    fill_price=quantize_price(to_decimal(fill.slippage_price)),
                    raw_open_price=quantize_price(to_decimal(fill.raw_open_price)),
                    commission=quantize_money(to_decimal(fill.commission)),
                    stamp_duty=quantize_money(to_decimal(fill.stamp_duty)),
                    transfer_fee=quantize_money(to_decimal(fill.transfer_fee)),
                    total_cost=quantize_money(to_decimal(fill.total_cost)),
                    cash_change=quantize_money(to_decimal(fill.cash_change)),
                    turnover=quantize_money(to_decimal(fill.turnover)),
                    audit_flags=list(fill.audit_flags),
                    **base,
                )
            )
            processed.add(key)

        # 5. 回写持仓与幂等键
        _apply_position_view(state, positions)
        state.processed_order_keys = sorted(processed)

        # 6. 估值与快照
        result = self.mark_to_market(
            state,
            as_of_date=fill_date,
            prices=prices,
            run_id=run_id,
            count_observation_day=count_observation_day,
        )
        result.orders = records
        result.cash_before = quantize_money(cash_before)
        result.rolled_over = rolled
        return result

    # ------------------------------------------------------------------ #
    # 估值与权益快照
    # ------------------------------------------------------------------ #
    def mark_to_market(
        self,
        state: SimulatedAccountState,
        *,
        as_of_date: date,
        prices: dict[str, Decimal],
        run_id: str,
        count_observation_day: bool = True,
    ) -> AccountUpdateResult:
        """按收盘价估值并追加权益快照（按日期幂等）。

        同一业务日重复调用只会覆盖当日快照，不会重复计入观察天数。
        """
        _assert_account_integrity(state)

        cash = quantize_money(to_decimal(state.cash))
        pos_value = quantize_money(state.position_value(prices))
        equity = quantize_money(cash + pos_value)

        entry = {
            "date": as_of_date.isoformat(),
            "cash": str(cash),
            "position_value": str(pos_value),
            "total_equity": str(equity),
            "run_id": run_id,
            "positions": len(state.positions),
        }

        existing_idx = next(
            (i for i, h in enumerate(state.history) if h.get("date") == entry["date"]),
            None,
        )
        if existing_idx is None:
            state.history.append(entry)
            state.history.sort(key=lambda h: str(h.get("date", "")))
            if count_observation_day:
                state.observation_days += 1
        else:
            state.history[existing_idx] = entry

        state.as_of_date = as_of_date
        state.last_run_id = run_id

        return AccountUpdateResult(
            account_id=state.account_id,
            strategy_track=state.strategy_track,
            eligibility_status=state.eligibility_status,
            as_of_date=as_of_date,
            run_id=run_id,
            cash_before=cash,
            cash_after=cash,
            position_value=pos_value,
            total_equity=equity,
            observation_days=state.observation_days,
        )

    # ------------------------------------------------------------------ #
    # 观察窗口
    # ------------------------------------------------------------------ #
    def observation_progress(self, state: SimulatedAccountState) -> dict[str, Any]:
        """观察窗口进度（供每周报告使用）。"""
        target = int(self.config.observation.target_trading_days)
        done = int(state.observation_days)
        remaining = max(target - done, 0)
        return {
            "account_id": state.account_id,
            "strategy_track": state.strategy_track.value,
            "eligibility_status": state.eligibility_status.value,
            "target_trading_days": target,
            "observed_trading_days": done,
            "remaining_trading_days": remaining,
            "completed": done >= target,
            "progress_pct": round(min(done / target, 1.0) * 100, 2) if target else 0.0,
            "live_trading": False,
            "disclaimer": SIMULATION_DISCLAIMER,
        }

    def equity_curve(self, state: SimulatedAccountState) -> list[dict[str, Any]]:
        """返回按日期排序的权益曲线副本。"""
        return sorted(
            (dict(h) for h in state.history), key=lambda h: str(h.get("date", ""))
        )

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _ordered(signals: Iterable[Signal]) -> list[Signal]:
        """确定性排序：先卖后买（释放现金），再按代码排序。

        避免同一批信号因输入顺序不同导致撮合结果漂移。
        """

        def sort_key(s: Signal) -> tuple[int, str, int]:
            side = s.side.value if isinstance(s.side, Side) else str(s.side)
            return (0 if side == Side.SELL.value else 1, s.symbol, -int(s.quantity))

        return sorted(signals, key=sort_key)
