"""事件驱动回测引擎。

日线事件顺序：
1. 开盘：解冻 T+1 持仓 → 处理昨日挂单 → 按未复权开盘价成交 → 更新现金/持仓
2. 收盘：估值 → 策略读取截至当日收盘数据 → 生成下一交易日订单

防未来函数：策略只能读取截至当日收盘的数据，信号最早下一交易日开盘成交。
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import date
from decimal import Decimal
from typing import Any, Optional

import numpy as np
import pandas as pd

from .broker import AShareBrokerSimulator
from .config import BacktestConfig
from .interfaces import BacktestEngine as BacktestEngineABC
from .interfaces import BrokerSimulator, RiskManager, Strategy, UniverseFilter
from .metrics import MetricsCalculator
from .models import (
    BacktestResult,
    BarData,
    Order,
    OrderStatus,
    PortfolioSnapshot,
    Position,
    Signal,
    StrategyContext,
    Side,
    to_decimal,
)
from .report import ReportGenerator
from .risk import DefaultRiskManager
from .universe import DefaultUniverseFilter

logger = logging.getLogger(__name__)

__all__ = ["BacktestEngine"]


class BacktestEngine(BacktestEngineABC):
    """事件驱动回测引擎。"""

    def __init__(self) -> None:
        self._metrics_calc = MetricsCalculator()
        self._report_gen = ReportGenerator()

    def run(
        self,
        data: pd.DataFrame,
        strategy: Strategy,
        start_date: date,
        end_date: date,
        initial_cash: float,
        config: BacktestConfig,
        universe_filter: Optional[UniverseFilter] = None,
        risk_manager: Optional[RiskManager] = None,
        broker: Optional[BrokerSimulator] = None,
    ) -> BacktestResult:
        """运行回测。

        Args:
            data: curated 日行情 DataFrame，须含 DAILY_QUOTE_FIELDS。
            strategy: 交易策略。
            start_date: 回测起始日。
            end_date: 回测结束日。
            initial_cash: 初始资金。
            config: 回测配置。
            universe_filter: 股票池过滤器（None 使用默认）。
            risk_manager: 风控管理器（None 使用默认）。
            broker: 成交模拟器（None 使用默认）。

        Returns:
            回测结果。
        """
        # 0. 数据校验与排序
        data = self._validate_and_sort(data)

        # 1. 初始化组件
        uf = universe_filter or DefaultUniverseFilter(config)
        rm = risk_manager or DefaultRiskManager()
        br = broker or AShareBrokerSimulator()

        # 2. 初始化状态
        cash = to_decimal(initial_cash)
        positions: dict[str, Position] = {}
        orders: list[Order] = []
        fills: list = []
        daily_equity: list[PortfolioSnapshot] = []
        pending_orders: list[Order] = []

        # 3. 获取回测区间内的交易日列表
        trading_dates = self._get_trading_dates(data, start_date, end_date)
        if not trading_dates:
            # 无交易日，生成空结果
            return self._build_empty_result(config, initial_cash, start_date, end_date)

        # 4. 预构建 symbol -> {date -> row} 的索引
        symbol_date_index = self._build_symbol_date_index(data)

        # 5. 跟踪历史最高权益（用于回撤计算）
        peak_equity = cash

        # 6. 主循环：逐日处理
        for i, dt in enumerate(trading_dates):
            # --- 6a. 开盘：解冻 T+1 ---
            for pos in positions.values():
                if pos.frozen_buy_quantity > 0:
                    pos.sellable_quantity += pos.frozen_buy_quantity
                    pos.frozen_buy_quantity = 0

            # --- 6b. 开盘：处理挂单 ---
            still_pending: list[Order] = []
            for order in pending_orders:
                if order.planned_fill_date != dt:
                    # 非今日成交的订单保留
                    still_pending.append(order)
                    continue

                bar = self._get_bar(symbol_date_index, order.signal.symbol, dt)
                snapshot = self._make_snapshot(
                    dt, cash, positions, symbol_date_index, peak_equity
                )

                # 先风控校验（现金充足性、持仓比例等）
                risk_decision = rm.validate(
                    order.signal, snapshot, bar, config, positions,
                )
                if not risk_decision.approved:
                    order.status = OrderStatus.REJECTED
                    order.reject_reason = risk_decision.reject_reason
                    logger.debug(
                        "订单 %s 风控拒绝: %s (%s)",
                        order.order_id, risk_decision.reject_reason, risk_decision.reason,
                    )
                    continue

                # 再检查撮合拒绝（涨跌停、停牌等）
                rejection = br.check_rejection(
                    order, bar, snapshot, config, positions,
                )
                if rejection is not None:
                    order.status = OrderStatus.REJECTED
                    order.reject_reason = rejection.reject_reason
                    logger.debug(
                        "订单 %s 被拒绝: %s (%s)",
                        order.order_id, rejection.reject_reason, rejection.reason,
                    )
                    continue

                # 执行成交
                fill = br.execute(
                    order, bar, snapshot, config, positions,
                )
                if fill is not None:
                    order.status = OrderStatus.FILLED
                    order.fill = fill
                    fills.append(fill)
                    # 更新现金
                    cash += fill.cash_change
                    # 更新持仓
                    self._update_position(positions, fill)
                else:
                    order.status = OrderStatus.REJECTED
                    order.reject_reason = None
                    logger.warning("订单 %s execute 返回 None 但未拒绝", order.order_id)

            pending_orders = still_pending

            # --- 6c. 收盘：估值 ---
            position_value = self._calc_position_value(positions, symbol_date_index, dt)
            total_equity = cash + position_value
            prev_equity = daily_equity[-1].total_equity if daily_equity else to_decimal(initial_cash)
            daily_pnl = total_equity - prev_equity
            cumulative_pnl = total_equity - to_decimal(initial_cash)
            if total_equity > peak_equity:
                peak_equity = total_equity
            drawdown = (peak_equity - total_equity) / peak_equity if peak_equity > 0 else Decimal("0")

            snapshot = PortfolioSnapshot(
                snapshot_date=dt,
                cash=cash,
                position_value=position_value,
                total_equity=total_equity,
                daily_pnl=daily_pnl,
                cumulative_pnl=cumulative_pnl,
                drawdown=drawdown,
            )
            daily_equity.append(snapshot)

            # --- 6d. 策略收盘 ---
            bars_up_to_date = data[data["trade_date"] <= dt]
            context = StrategyContext(
                current_date=dt,
                portfolio=snapshot,
                positions=dict(positions),
                bars_up_to_date=bars_up_to_date,
            )

            signals = strategy.on_close(context)

            # --- 6e. 信号转订单 ---
            next_date = trading_dates[i + 1] if i + 1 < len(trading_dates) else None
            for signal in signals:
                if next_date is None:
                    # 最后一个交易日，信号无法在下一交易日成交
                    order = Order(
                        signal=signal,
                        planned_fill_date=dt,
                        status=OrderStatus.CANCELLED,
                    )
                    orders.append(order)
                else:
                    order = Order(
                        signal=signal,
                        planned_fill_date=next_date,
                    )
                    orders.append(order)
                    pending_orders.append(order)

        # --- 7. 期末：取消剩余挂单 ---
        for order in pending_orders:
            if order.status == OrderStatus.PENDING:
                order.status = OrderStatus.CANCELLED

        # --- 8. 计算指标 ---
        result = BacktestResult(
            config_summary=config.to_summary(),
            orders=orders,
            fills=fills,
            daily_equity=daily_equity,
            final_positions=dict(positions),
            limitations=[
                "Phase 2 不处理分红、送股、拆并股和配股",
                "前复权价格仅用于信号，未复权价格用于成交",
                "仅支持下一交易日开盘市价撮合",
            ],
            data_range={
                "start_date": str(start_date),
                "end_date": str(end_date),
                "trading_days": len(trading_dates),
                "symbols": sorted(data["symbol"].unique().tolist()) if not data.empty else [],
            },
        )

        metrics = self._metrics_calc.calculate(result, to_decimal(initial_cash))
        result.metrics = metrics
        result.content_hash = self._compute_hash(result)

        return result

    # ------------------------------------------------------------------ #
    # 数据校验
    # ------------------------------------------------------------------ #
    def _validate_and_sort(self, data: pd.DataFrame) -> pd.DataFrame:
        """校验输入数据并按 symbol, trade_date 稳定排序。"""
        if data.empty:
            return data

        required = [
            "symbol", "trade_date",
            "open_raw", "high_raw", "low_raw", "close_raw",
            "volume", "amount",
            "open_qfq", "high_qfq", "low_qfq", "close_qfq",
            "adjustment_factor",
            "is_suspended", "is_tradable",
        ]
        missing = [c for c in required if c not in data.columns]
        if missing:
            raise ValueError(f"缺少必需字段: {missing}")

        # 重复键检查
        dup = data.duplicated(subset=["symbol", "trade_date"], keep=False)
        if dup.any():
            raise ValueError(
                f"存在重复 (symbol, trade_date) 键: "
                f"{data[dup][['symbol', 'trade_date']].head(10).to_dict('records')}"
            )

        # 非有限价格检查
        price_cols = [
            "open_raw", "high_raw", "low_raw", "close_raw",
            "open_qfq", "high_qfq", "low_qfq", "close_qfq",
        ]
        for col in price_cols:
            vals = pd.to_numeric(data[col], errors="coerce")
            bad = vals.isna() | ~np.isfinite(vals)
            if bad.any():
                raise ValueError(f"字段 {col} 包含 NaN 或非有限值")

        # 排序
        data = data.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
        return data

    def _get_trading_dates(
        self, data: pd.DataFrame, start_date: date, end_date: date
    ) -> list[date]:
        """获取回测区间内的交易日列表（去重、排序）。"""
        if data.empty:
            return []
        dates = data["trade_date"].unique()
        # 兼容 date 对象和 pandas Timestamp
        result = []
        for d in dates:
            if isinstance(d, date) and not isinstance(d, type(None)):
                dt = d if isinstance(d, date) else pd.Timestamp(d).date()
            else:
                dt = pd.Timestamp(d).date()
            if start_date <= dt <= end_date:
                result.append(dt)
        return sorted(set(result))

    def _build_symbol_date_index(
        self, data: pd.DataFrame
    ) -> dict[str, dict[date, pd.Series]]:
        """构建 symbol -> {trade_date -> row} 索引。"""
        index: dict[str, dict[date, pd.Series]] = {}
        for _, row in data.iterrows():
            sym = str(row["symbol"])
            dt = row["trade_date"]
            if not isinstance(dt, date):
                dt = pd.Timestamp(dt).date()
            if sym not in index:
                index[sym] = {}
            index[sym][dt] = row
        return index

    def _get_bar(
        self,
        index: dict[str, dict[date, pd.Series]],
        symbol: str,
        dt: date,
    ) -> Optional[BarData]:
        """从索引中获取某 symbol 在 dt 的 BarData。"""
        sym_data = index.get(symbol)
        if sym_data is None:
            return None
        row = sym_data.get(dt)
        if row is None:
            return None

        # 查找前收盘价
        prev_close = None
        sorted_dates = sorted(sym_data.keys())
        idx = sorted_dates.index(dt) if dt in sorted_dates else -1
        if idx > 0:
            prev_row = sym_data[sorted_dates[idx - 1]]
            prev_close = to_decimal(prev_row["close_raw"])

        return BarData(
            symbol=symbol,
            trade_date=dt,
            open_raw=to_decimal(row["open_raw"]),
            high_raw=to_decimal(row["high_raw"]),
            low_raw=to_decimal(row["low_raw"]),
            close_raw=to_decimal(row["close_raw"]),
            open_qfq=to_decimal(row["open_qfq"]),
            high_qfq=to_decimal(row["high_qfq"]),
            low_qfq=to_decimal(row["low_qfq"]),
            close_qfq=to_decimal(row["close_qfq"]),
            volume=float(row.get("volume", 0)),
            amount=float(row.get("amount", 0)),
            is_suspended=bool(row.get("is_suspended", False)),
            is_tradable=bool(row.get("is_tradable", True)),
            prev_close_raw=prev_close,
        )

    def _make_snapshot(
        self,
        dt: date,
        cash: Decimal,
        positions: dict[str, Position],
        index: dict[str, dict[date, pd.Series]],
        peak: Decimal,
    ) -> PortfolioSnapshot:
        """构建当前组合快照。"""
        pos_value = self._calc_position_value(positions, index, dt)
        total = cash + pos_value
        return PortfolioSnapshot(
            snapshot_date=dt,
            cash=cash,
            position_value=pos_value,
            total_equity=total,
        )

    def _calc_position_value(
        self,
        positions: dict[str, Position],
        index: dict[str, dict[date, pd.Series]],
        dt: date,
    ) -> Decimal:
        """计算持仓市值（按未复权收盘价）。"""
        value = Decimal("0")
        for symbol, pos in positions.items():
            if pos.total_quantity <= 0:
                continue
            sym_data = index.get(symbol)
            if sym_data is None:
                continue
            row = sym_data.get(dt)
            if row is None:
                continue
            close = to_decimal(row["close_raw"])
            value += close * Decimal(pos.total_quantity)
        return value

    def _update_position(self, positions: dict[str, Position], fill) -> None:
        """根据成交记录更新持仓。"""
        symbol = fill.symbol
        if symbol not in positions:
            positions[symbol] = Position(symbol=symbol)

        pos = positions[symbol]
        qty = fill.quantity
        price = fill.slippage_price

        if fill.side == Side.BUY:
            old_total_cost = pos.avg_raw_cost * Decimal(pos.total_quantity)
            new_total_cost = old_total_cost + price * Decimal(qty)
            pos.total_quantity += qty
            pos.frozen_buy_quantity += qty  # T+1: 当日买入冻结
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

    def _build_empty_result(
        self,
        config: BacktestConfig,
        initial_cash: float,
        start_date: date,
        end_date: date,
    ) -> BacktestResult:
        """构建空回测结果。"""
        cash = to_decimal(initial_cash)
        snapshot = PortfolioSnapshot(
            snapshot_date=end_date,
            cash=cash,
            position_value=Decimal("0"),
            total_equity=cash,
        )
        result = BacktestResult(
            config_summary=config.to_summary(),
            daily_equity=[snapshot],
            final_positions={},
            limitations=[
                "Phase 2 不处理分红、送股、拆并股和配股",
                "前复权价格仅用于信号，未复权价格用于成交",
                "仅支持下一交易日开盘市价撮合",
                "回测区间内无交易日",
            ],
            data_range={
                "start_date": str(start_date),
                "end_date": str(end_date),
                "trading_days": 0,
                "symbols": [],
            },
        )
        metrics = self._metrics_calc.calculate(result, cash)
        result.metrics = metrics
        return result

    def _compute_hash(self, result: BacktestResult) -> str:
        """计算结果内容哈希（排除非确定字段）。"""
        try:
            # 只对确定性内容计算哈希
            hashable = {
                "config_summary": result.config_summary,
                "order_count": len(result.orders),
                "fill_count": len(result.fills),
                "trading_days": len(result.daily_equity),
                "final_equity": str(result.daily_equity[-1].total_equity) if result.daily_equity else None,
            }
            content = json.dumps(hashable, ensure_ascii=False, sort_keys=True, default=str)
            return hashlib.sha256(content.encode("utf-8")).hexdigest()
        except Exception:
            return ""
