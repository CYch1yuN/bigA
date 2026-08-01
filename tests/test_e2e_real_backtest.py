# -*- coding: utf-8 -*-
"""真实端到端回测测试：不使用任何 mock，覆盖完整交易链路。

测试链路：信号 -> 次日订单 -> 成交/拒绝 -> 费用 -> 现金 -> 持仓 -> 权益

使用真实组件（均非 mock）：
- ``BacktestEngine.run`` —— 真实事件驱动回测引擎主循环
- ``AShareBrokerSimulator`` —— 真实 A 股成交模拟器（T+1、手数、涨跌停、滑点、费用）
- ``SteadyStrategy`` —— 真实稳健轨策略（周频调仓、趋势+动量+波动率综合得分）
- ``HistoricalUniverseFilter`` —— 真实历史时点股票池过滤器
- ``DefaultRiskManager`` —— 真实默认风控管理器（由引擎内部自动创建）

合成数据设计要点：
- 6 只股票，320 个交易日，起始日 2019-01-02
- 所有股票上市日为 2019-01-02（满足 120 日上市期要求）
- 所有股票呈上升趋势（close_qfq > MA(120)），满足稳健轨趋势过滤
- Stock 000001 先强后弱、Stock 000002 先弱后强，确保换仓产生 SELL+BUY
- 基础成交量 500,000，每 30 天放量至 1,500,000（量比 > 3.0）
- 所有价格控制在 ≤ 8.0 以内，确保 1000 元现金可购买至少一手
"""
from __future__ import annotations

from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Optional

import numpy as np
import pandas as pd
import pytest

from ashare_quant.backtest.broker import AShareBrokerSimulator
from ashare_quant.backtest.config import (
    BacktestConfig,
    CommissionConfig,
    SlippageConfig,
    StampDutyConfig,
    TransferFeeConfig,
)
from ashare_quant.backtest.engine import BacktestEngine
from ashare_quant.backtest.models import (
    BacktestResult,
    Fill,
    Order,
    OrderStatus,
    Side,
)
from ashare_quant.research.strategies import SteadyParams, SteadyStrategy
from ashare_quant.research.universe import (
    HistoricalStatusTable,
    HistoricalUniverseFilter,
)
from tests.backtest_samples import make_trade_dates
from tests.research_samples import make_stock_quotes


# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
START = date(2019, 1, 2)
N_DAYS = 320
SYMBOLS = [f"{i:06d}" for i in range(1, 7)]
INITIAL_CASH = 1000.0


# --------------------------------------------------------------------------- #
# 合成数据构建器
# --------------------------------------------------------------------------- #


def _make_e2e_quotes() -> pd.DataFrame:
    """构建端到端测试合成行情数据。

    6 只股票的行情设计：
    - 000001: base=3.0，前半段漂移 0.0025（强趋势），后半段 0.0005（趋缓）
    - 000002: base=2.0，前半段漂移 0.0005（弱趋势），后半段 0.0050（急剧加速）
    - 000003: base=4.0，全程漂移 0.0015（稳定）
    - 000004: base=5.0，全程漂移 0.0012（稳定）
    - 000005: base=3.5，全程漂移 0.0018（稳定）
    - 000006: base=4.5，全程漂移 0.0010（稳定）

    000001 先强后弱、000002 先弱后强，确保稳健轨策略在不同时期选择不同标的，
    从而产生换仓信号（SELL 旧持仓 + BUY 新标的）。

    所有价格在 320 个交易日内保持在 2.0 ~ 7.5 区间，远低于 9.95 元
    （1000 元现金购买一手的上限），确保策略能实际买入。

    成交量基础为 500,000，每 30 个交易日注入一次 3 倍放量（量比 > 1.5），
    满足激进轨策略的放量条件。
    """
    dates = make_trade_dates(START, N_DAYS)
    rng = np.random.default_rng(42)
    n = len(dates)
    dfs: list[pd.DataFrame] = []

    # (symbol, base_price, drift_phase1, drift_phase2, switch_day, noise_scale)
    configs = [
        ("000001", 3.0, 0.0025, 0.0005, 180, 0.003),
        ("000002", 2.0, 0.0005, 0.0050, 180, 0.003),
        ("000003", 4.0, 0.0015, 0.0015, 180, 0.003),
        ("000004", 5.0, 0.0012, 0.0012, 180, 0.003),
        ("000005", 3.5, 0.0018, 0.0018, 180, 0.004),
        ("000006", 4.5, 0.0010, 0.0010, 180, 0.003),
    ]

    for sym, base, drift1, drift2, switch_day, noise_scale in configs:
        prices = [base]
        for i in range(1, n):
            drift = drift1 if i < switch_day else drift2
            ret = drift + rng.normal(0, noise_scale)
            prices.append(max(0.5, prices[-1] * (1 + ret)))
        price_override = {dates[j]: round(float(prices[j]), 4) for j in range(n)}

        # 基础成交量 + 周期性放量（量比 > 1.5）
        base_vol = 500_000.0
        vols = [base_vol] * n
        for spike_idx in range(150, n, 30):
            vols[spike_idx] = base_vol * 3.0
        vol_override = {dates[j]: vols[j] for j in range(n)}

        dfs.append(make_stock_quotes(
            sym, dates, base_price=base, daily_return=0.0,
            volume=int(base_vol), price_override=price_override,
            volume_override=vol_override,
        ))

    return pd.concat(dfs, ignore_index=True).sort_values(
        ["trade_date", "symbol"]
    ).reset_index(drop=True)


def _make_e2e_status_table() -> HistoricalStatusTable:
    """构建历史状态表：所有股票 2019-01-02 上市，状态 normal。"""
    records = [
        {
            "symbol": sym,
            "list_date": date(2019, 1, 2),
            "delist_date": None,
            "st_status": "normal",
            "status_valid_from": date(2019, 1, 2),
            "status_valid_to": None,
        }
        for sym in SYMBOLS
    ]
    return HistoricalStatusTable(records=pd.DataFrame(records))


def _make_e2e_config() -> BacktestConfig:
    """构建回测配置。

    费率参数：
    - initial_cash: 1000.0
    - lot_size: 100
    - commission rate: 0.0003（万三），minimum: 5.0 元
    - stamp_duty rate: 0.001（千一，仅卖出）
    - transfer_fee rate: 0.00001（万零点一，双向）
    - slippage bps: 10.0（0.1%）
    """
    return BacktestConfig(
        initial_cash=INITIAL_CASH,
        lot_size=100,
        commission=CommissionConfig(rate=0.0003, minimum=5.0),
        stamp_duty=StampDutyConfig(rate=0.001),
        transfer_fee=TransferFeeConfig(rate=0.00001),
        slippage=SlippageConfig(bps=10.0),
    )


def _make_e2e_universe_filter(
    status_table: HistoricalStatusTable,
    quotes: pd.DataFrame,
) -> HistoricalUniverseFilter:
    """构建历史时点股票池过滤器。

    使用宽松参数确保合成数据通过过滤（过滤器本身为真实实现，非 mock）：
    - min_listing_days=120：与稳健轨 trend_window=120 一致
    - min_valid_days=1：合成数据无停牌，放宽有效天数要求
    - min_turnover=0.0：合成数据成交额为合成值，不强制流动性阈值
    """
    return HistoricalUniverseFilter(
        status_table=status_table,
        quotes=quotes,
        min_listing_days=120,
        min_valid_days=1,
        valid_days_window=20,
        min_turnover=0.0,
        turnover_window=20,
        lot_size=100,
        available_cash=INITIAL_CASH,
    )


# --------------------------------------------------------------------------- #
# 辅助：费用重算（用于断言验证）
# --------------------------------------------------------------------------- #


def _expected_buy_slippage_price(
    open_raw: Decimal, bps: float, tick_size: float
) -> Decimal:
    """重算买入滑点后价格：open_raw * (1 + bps/10000)，向上取整到 tick。"""
    bps_dec = Decimal(str(bps))
    factor = Decimal("1") + bps_dec / Decimal("10000")
    slip_raw = open_raw * factor
    if tick_size > 0:
        tick_dec = Decimal(str(tick_size))
        q = (slip_raw / tick_dec).quantize(Decimal("1"), rounding=ROUND_CEILING)
        return (q * tick_dec).quantize(Decimal("0.0001"))
    return slip_raw.quantize(Decimal("0.0001"))


def _expected_sell_slippage_price(
    open_raw: Decimal, bps: float, tick_size: float
) -> Decimal:
    """重算卖出滑点后价格：open_raw * (1 - bps/10000)，向下取整到 tick。"""
    bps_dec = Decimal(str(bps))
    factor = Decimal("1") - bps_dec / Decimal("10000")
    slip_raw = open_raw * factor
    if tick_size > 0:
        tick_dec = Decimal(str(tick_size))
        q = (slip_raw / tick_dec).quantize(Decimal("1"), rounding=ROUND_FLOOR)
        return (q * tick_dec).quantize(Decimal("0.0001"))
    return slip_raw.quantize(Decimal("0.0001"))


def _expected_commission(turnover: Decimal, rate: float, minimum: float) -> Decimal:
    """重算佣金：max(turnover * rate, minimum)，保留两位小数。"""
    comm = turnover * Decimal(str(rate))
    if comm < Decimal(str(minimum)):
        comm = Decimal(str(minimum))
    return comm.quantize(Decimal("0.01"))


def _expected_stamp_duty(turnover: Decimal, rate: float) -> Decimal:
    """重算印花税：turnover * rate，保留两位小数。"""
    return (turnover * Decimal(str(rate))).quantize(Decimal("0.01"))


def _expected_transfer_fee(turnover: Decimal, rate: float) -> Decimal:
    """重算过户费：turnover * rate，保留两位小数。"""
    return (turnover * Decimal(str(rate))).quantize(Decimal("0.01"))


# --------------------------------------------------------------------------- #
# 模块级 fixture：运行一次完整回测，所有测试共享结果
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def e2e_quotes() -> pd.DataFrame:
    """合成行情数据。"""
    return _make_e2e_quotes()


@pytest.fixture(scope="module")
def e2e_status_table() -> HistoricalStatusTable:
    """历史状态表。"""
    return _make_e2e_status_table()


@pytest.fixture(scope="module")
def e2e_config() -> BacktestConfig:
    """回测配置。"""
    return _make_e2e_config()


@pytest.fixture(scope="module")
def e2e_universe_filter(
    e2e_status_table: HistoricalStatusTable,
    e2e_quotes: pd.DataFrame,
) -> HistoricalUniverseFilter:
    """历史时点股票池过滤器。"""
    return _make_e2e_universe_filter(e2e_status_table, e2e_quotes)


@pytest.fixture(scope="module")
def e2e_result(
    e2e_quotes: pd.DataFrame,
    e2e_config: BacktestConfig,
    e2e_universe_filter: HistoricalUniverseFilter,
) -> BacktestResult:
    """运行完整端到端回测，返回真实回测结果。

    使用真实 BacktestEngine.run、真实 AShareBrokerSimulator、真实 SteadyStrategy，
    不使用任何 mock。覆盖完整链路：信号 -> 次日订单 -> 成交 -> 费用 -> 现金 -> 持仓 -> 权益。
    """
    dates = make_trade_dates(START, N_DAYS)
    strategy = SteadyStrategy(
        SteadyParams(), e2e_universe_filter, dates, lot_size=100,
    )
    engine = BacktestEngine()
    # 显式传入真实 AShareBrokerSimulator（也可省略由引擎自动创建，效果相同）
    broker = AShareBrokerSimulator()
    return engine.run(
        data=e2e_quotes,
        strategy=strategy,
        start_date=dates[0],
        end_date=dates[-1],
        initial_cash=INITIAL_CASH,
        config=e2e_config,
        universe_filter=e2e_universe_filter,
        broker=broker,
    )


@pytest.fixture(scope="module")
def e2e_buy_fills(e2e_result: BacktestResult) -> list[Fill]:
    """所有 BUY 成交记录。"""
    return [f for f in e2e_result.fills if f.side == Side.BUY]


@pytest.fixture(scope="module")
def e2e_sell_fills(e2e_result: BacktestResult) -> list[Fill]:
    """所有 SELL 成交记录。"""
    return [f for f in e2e_result.fills if f.side == Side.SELL]


# =========================================================================== #
# 1. 稳健轨策略产生真实交易
# =========================================================================== #


def test_real_e2e_steady_strategy_produces_trades(e2e_result: BacktestResult):
    """验证真实稳健轨策略通过真实引擎和 Broker 产生了交易。

    断言：
    - 订单列表非空
    - 成交列表非空
    - 至少存在一笔 BUY 成交
    - 至少存在一笔 SELL 成交
    - 成交日严格晚于信号日（防未来函数：D 收盘信号 -> D+1 开盘成交）
    """
    # 订单列表非空
    assert len(e2e_result.orders) > 0, "回测应产生至少一个订单"

    # 成交列表非空
    assert len(e2e_result.fills) > 0, "回测应产生至少一笔成交"

    # 分类成交记录
    buy_fills = [f for f in e2e_result.fills if f.side == Side.BUY]
    sell_fills = [f for f in e2e_result.fills if f.side == Side.SELL]

    # 至少一笔 BUY 和一笔 SELL
    assert len(buy_fills) > 0, "应至少存在一笔 BUY 成交"
    assert len(sell_fills) > 0, "应至少存在一笔 SELL 成交"

    # 防未来函数：每笔成交的成交日严格晚于对应订单的信号日
    order_by_id = {o.order_id: o for o in e2e_result.orders}
    for fill in e2e_result.fills:
        order = order_by_id.get(fill.order_id)
        assert order is not None, f"成交 {fill.order_id} 找不到对应订单"
        assert fill.fill_date > order.signal.signal_date, (
            f"成交日 {fill.fill_date} 应晚于信号日 {order.signal.signal_date}（防未来函数）"
        )

    # 至少有一笔 FILLED 状态的订单
    filled_orders = [
        o for o in e2e_result.orders if o.status == OrderStatus.FILLED
    ]
    assert len(filled_orders) > 0, "应至少有一笔状态为 FILLED 的订单"


# =========================================================================== #
# 2. BUY 成交验证：费用与现金
# =========================================================================== #


def test_real_e2e_buy_fill_verifies_fees_and_cash(
    e2e_buy_fills: list[Fill],
    e2e_config: BacktestConfig,
):
    """验证 BUY 成交的费用计算和现金变化。

    断言（针对每笔 BUY 成交）：
    - cash_change 为负数（买入消耗现金）
    - commission > 0（佣金为正）
    - slippage_price > raw_open_price（买入滑点使价格上升）
    - stamp_duty == 0（买入不收印花税）
    - transfer_fee > 0（过户费为正，双向收取）
    - total_cost == commission + stamp_duty + transfer_fee（费用分解一致）
    - cash_change == -(turnover + total_cost)（买入现金变化公式）
    - 重算的佣金、滑点价格与成交记录一致
    """
    assert len(e2e_buy_fills) > 0, "需要至少一笔 BUY 成交"

    bps = e2e_config.slippage.bps
    tick = e2e_config.slippage.tick_size
    comm_rate = e2e_config.commission.rate
    comm_min = e2e_config.commission.minimum
    transfer_rate = e2e_config.transfer_fee.rate

    for fill in e2e_buy_fills:
        # cash_change 为负
        assert fill.cash_change < 0, (
            f"BUY {fill.symbol} {fill.fill_date}: cash_change={fill.cash_change} "
            f"应为负数"
        )

        # commission > 0
        assert fill.commission > 0, (
            f"BUY {fill.symbol} {fill.fill_date}: commission={fill.commission} "
            f"应大于 0"
        )

        # slippage_price > raw_open_price
        assert fill.slippage_price > fill.raw_open_price, (
            f"BUY {fill.symbol} {fill.fill_date}: "
            f"slippage_price={fill.slippage_price} 应大于 "
            f"raw_open_price={fill.raw_open_price}"
        )

        # 买入不收印花税
        assert fill.stamp_duty == 0, (
            f"BUY {fill.symbol} {fill.fill_date}: stamp_duty={fill.stamp_duty} "
            f"应为 0（买入不收印花税）"
        )

        # 过户费 >= 0（双向收取，小额交易可能因量化为 0）
        assert fill.transfer_fee >= 0, (
            f"BUY {fill.symbol} {fill.fill_date}: transfer_fee={fill.transfer_fee} "
            f"不应为负"
        )

        # total_cost 分解一致
        expected_total = fill.commission + fill.stamp_duty + fill.transfer_fee
        assert fill.total_cost == expected_total, (
            f"BUY {fill.symbol} {fill.fill_date}: total_cost={fill.total_cost} "
            f"不等于 commission+stamp_duty+transfer_fee={expected_total}"
        )

        # cash_change 公式：-(turnover + total_cost)
        turnover = fill.slippage_price * Decimal(fill.quantity)
        expected_cash_change = (-(turnover + fill.total_cost)).quantize(
            Decimal("0.01")
        )
        assert fill.cash_change == expected_cash_change, (
            f"BUY {fill.symbol} {fill.fill_date}: cash_change={fill.cash_change} "
            f"不等于 -(turnover+total_cost)={expected_cash_change}"
        )

        # 重算滑点价格并比较
        expected_slip = _expected_buy_slippage_price(
            fill.raw_open_price, bps, tick
        )
        assert fill.slippage_price == expected_slip, (
            f"BUY {fill.symbol} {fill.fill_date}: "
            f"slippage_price={fill.slippage_price} "
            f"不等于重算值 {expected_slip}"
        )

        # 重算佣金并比较
        expected_comm = _expected_commission(turnover, comm_rate, comm_min)
        assert fill.commission == expected_comm, (
            f"BUY {fill.symbol} {fill.fill_date}: "
            f"commission={fill.commission} "
            f"不等于重算值 {expected_comm}"
        )

        # 重算过户费并比较
        expected_transfer = _expected_transfer_fee(turnover, transfer_rate)
        assert fill.transfer_fee == expected_transfer, (
            f"BUY {fill.symbol} {fill.fill_date}: "
            f"transfer_fee={fill.transfer_fee} "
            f"不等于重算值 {expected_transfer}"
        )


# =========================================================================== #
# 3. SELL 成交验证：费用与现金
# =========================================================================== #


def test_real_e2e_sell_fill_verifies_fees_and_cash(
    e2e_sell_fills: list[Fill],
    e2e_config: BacktestConfig,
):
    """验证 SELL 成交的费用计算和现金变化。

    断言（针对每笔 SELL 成交）：
    - cash_change 为正数（卖出增加现金）
    - commission > 0（佣金为正）
    - stamp_duty > 0（卖出收取印花税）
    - transfer_fee > 0（过户费为正，双向收取）
    - slippage_price < raw_open_price（卖出滑点使价格下降）
    - total_cost == commission + stamp_duty + transfer_fee
    - cash_change == turnover - total_cost（卖出现金变化公式）
    - 重算的佣金、印花税、滑点价格与成交记录一致
    """
    assert len(e2e_sell_fills) > 0, "需要至少一笔 SELL 成交"

    bps = e2e_config.slippage.bps
    tick = e2e_config.slippage.tick_size
    comm_rate = e2e_config.commission.rate
    comm_min = e2e_config.commission.minimum
    stamp_rate = e2e_config.stamp_duty.rate
    transfer_rate = e2e_config.transfer_fee.rate

    for fill in e2e_sell_fills:
        # cash_change 为正
        assert fill.cash_change > 0, (
            f"SELL {fill.symbol} {fill.fill_date}: cash_change={fill.cash_change} "
            f"应为正数"
        )

        # commission > 0
        assert fill.commission > 0, (
            f"SELL {fill.symbol} {fill.fill_date}: commission={fill.commission} "
            f"应大于 0"
        )

        # stamp_duty > 0（卖出收取印花税）
        assert fill.stamp_duty > 0, (
            f"SELL {fill.symbol} {fill.fill_date}: stamp_duty={fill.stamp_duty} "
            f"应大于 0（卖出收取印花税）"
        )

        # 过户费 >= 0（双向收取，小额交易可能因量化为 0）
        assert fill.transfer_fee >= 0, (
            f"SELL {fill.symbol} {fill.fill_date}: transfer_fee={fill.transfer_fee} "
            f"不应为负"
        )

        # 卖出滑点使价格下降
        assert fill.slippage_price < fill.raw_open_price, (
            f"SELL {fill.symbol} {fill.fill_date}: "
            f"slippage_price={fill.slippage_price} 应小于 "
            f"raw_open_price={fill.raw_open_price}"
        )

        # total_cost 分解一致
        expected_total = fill.commission + fill.stamp_duty + fill.transfer_fee
        assert fill.total_cost == expected_total, (
            f"SELL {fill.symbol} {fill.fill_date}: total_cost={fill.total_cost} "
            f"不等于 commission+stamp_duty+transfer_fee={expected_total}"
        )

        # cash_change 公式：turnover - total_cost
        turnover = fill.slippage_price * Decimal(fill.quantity)
        expected_cash_change = (turnover - fill.total_cost).quantize(
            Decimal("0.01")
        )
        assert fill.cash_change == expected_cash_change, (
            f"SELL {fill.symbol} {fill.fill_date}: cash_change={fill.cash_change} "
            f"不等于 turnover-total_cost={expected_cash_change}"
        )

        # 重算滑点价格
        expected_slip = _expected_sell_slippage_price(
            fill.raw_open_price, bps, tick
        )
        assert fill.slippage_price == expected_slip, (
            f"SELL {fill.symbol} {fill.fill_date}: "
            f"slippage_price={fill.slippage_price} "
            f"不等于重算值 {expected_slip}"
        )

        # 重算佣金
        expected_comm = _expected_commission(turnover, comm_rate, comm_min)
        assert fill.commission == expected_comm, (
            f"SELL {fill.symbol} {fill.fill_date}: "
            f"commission={fill.commission} "
            f"不等于重算值 {expected_comm}"
        )

        # 重算印花税
        expected_stamp = _expected_stamp_duty(turnover, stamp_rate)
        assert fill.stamp_duty == expected_stamp, (
            f"SELL {fill.symbol} {fill.fill_date}: "
            f"stamp_duty={fill.stamp_duty} "
            f"不等于重算值 {expected_stamp}"
        )

        # 重算过户费
        expected_transfer = _expected_transfer_fee(turnover, transfer_rate)
        assert fill.transfer_fee == expected_transfer, (
            f"SELL {fill.symbol} {fill.fill_date}: "
            f"transfer_fee={fill.transfer_fee} "
            f"不等于重算值 {expected_transfer}"
        )


# =========================================================================== #
# 4. 订单-成交关联验证
# =========================================================================== #


def test_real_e2e_order_fill_linkage(e2e_result: BacktestResult):
    """验证订单与成交通过 order_id 正确关联。

    断言：
    - 每笔成交的 order_id 都能在订单列表中找到对应订单
    - 对应订单的 status 为 FILLED
    - 对应订单的 fill 字段指向该成交记录
    - 每笔 FILLED 订单恰好对应一笔成交
    - 订单的 signal 字段与成交的 symbol/side 一致
    """
    orders = e2e_result.orders
    fills = e2e_result.fills

    # 构建 order_id -> order 映射
    order_map: dict[str, Order] = {o.order_id: o for o in orders}

    # 每笔成交都能找到对应订单，且状态为 FILLED
    for fill in fills:
        assert fill.order_id in order_map, (
            f"成交 {fill.order_id} ({fill.symbol} {fill.fill_date}) "
            f"找不到对应订单"
        )
        order = order_map[fill.order_id]
        assert order.status == OrderStatus.FILLED, (
            f"订单 {fill.order_id} 状态应为 FILLED，实际为 {order.status}"
        )
        # 订单的 fill 字段指向该成交
        assert order.fill is not None, (
            f"订单 {fill.order_id} 的 fill 字段不应为 None"
        )
        assert order.fill.order_id == fill.order_id, (
            f"订单 {fill.order_id} 的 fill.order_id 不匹配"
        )
        # 订单信号与成交的 symbol/side 一致
        assert order.signal.symbol == fill.symbol, (
            f"订单 {fill.order_id} signal.symbol={order.signal.symbol} "
            f"与成交 symbol={fill.symbol} 不一致"
        )
        assert order.signal.side == fill.side, (
            f"订单 {fill.order_id} signal.side={order.signal.side} "
            f"与成交 side={fill.side} 不一致"
        )

    # 每笔 FILLED 订单恰好对应一笔成交
    filled_orders = [o for o in orders if o.status == OrderStatus.FILLED]
    assert len(filled_orders) == len(fills), (
        f"FILLED 订单数 ({len(filled_orders)}) 应等于成交数 ({len(fills)})"
    )

    # 所有 FILLED 订单的 order_id 集合等于所有成交的 order_id 集合
    filled_order_ids = {o.order_id for o in filled_orders}
    fill_order_ids = {f.order_id for f in fills}
    assert filled_order_ids == fill_order_ids, (
        "FILLED 订单 ID 集合与成交 order_id 集合不匹配"
    )

    # 验证 planned_fill_date 等于 fill_date（成交发生在计划日）
    for fill in fills:
        order = order_map[fill.order_id]
        assert order.planned_fill_date == fill.fill_date, (
            f"订单 {fill.order_id} planned_fill_date="
            f"{order.planned_fill_date} 与 fill_date={fill.fill_date} 不一致"
        )


# =========================================================================== #
# 5. 持仓更新验证
# =========================================================================== #


def test_real_e2e_position_updates(e2e_result: BacktestResult):
    """验证买入后持仓增加、卖出后持仓减少或归零。

    断言：
    - 按成交时间顺序模拟持仓变化
    - 每笔 BUY 成交后，对应 symbol 的 total_quantity 增加
    - 每笔 SELL 成交后，对应 symbol 的 total_quantity 减少或归零
    - 模拟的期末持仓与引擎报告的 final_positions 一致
    - 期末持仓中每个 symbol 的 total_quantity >= 0
    """
    fills = e2e_result.fills
    assert len(fills) > 0, "需要至少一笔成交来验证持仓更新"

    # 按成交日顺序模拟持仓变化
    # 注意：同一成交日可能有 SELL + BUY，引擎按 pending_orders 顺序处理
    # （策略生成信号顺序为先 SELL 后 BUY，引擎保持该顺序）
    simulated: dict[str, int] = {}
    # 记录每笔成交后的即时持仓快照，用于事后验证"买入/卖出时刻"的持仓变化
    fill_snapshots: list[tuple[Fill, int, int]] = []  # (fill, old_qty, new_qty)

    for fill in fills:
        sym = fill.symbol
        old_qty = simulated.get(sym, 0)

        if fill.side == Side.BUY:
            new_qty = old_qty + fill.quantity
            # 买入后持仓增加
            assert new_qty > old_qty, (
                f"BUY {sym} {fill.fill_date}: 持仓应增加，"
                f"old={old_qty} new={new_qty}"
            )
            simulated[sym] = new_qty
            fill_snapshots.append((fill, old_qty, new_qty))
        elif fill.side == Side.SELL:
            new_qty = old_qty - fill.quantity
            # 卖出后持仓减少或归零
            assert new_qty < old_qty, (
                f"SELL {sym} {fill.fill_date}: 持仓应减少，"
                f"old={old_qty} new={new_qty}"
            )
            assert new_qty >= 0, (
                f"SELL {sym} {fill.fill_date}: 持仓不应为负，"
                f"old={old_qty} new={new_qty}"
            )
            simulated[sym] = new_qty
            fill_snapshots.append((fill, old_qty, new_qty))

    # 模拟的期末持仓与引擎报告的 final_positions 一致
    final_positions = e2e_result.final_positions
    for sym, expected_qty in simulated.items():
        if expected_qty > 0:
            # 有剩余持仓的 symbol 应在 final_positions 中
            assert sym in final_positions, (
                f"期末应持有 {sym}（模拟数量={expected_qty}），"
                f"但 final_positions 中不存在"
            )
            actual_qty = final_positions[sym].total_quantity
            assert actual_qty == expected_qty, (
                f"{sym} 期末持仓数量不匹配: "
                f"引擎报告={actual_qty}, 模拟值={expected_qty}"
            )
        else:
            # 持仓为零的 symbol：可能在 final_positions 中（total_quantity=0）
            # 也可能不在（引擎清理了空持仓），两种情况都接受
            if sym in final_positions:
                assert final_positions[sym].total_quantity == 0, (
                    f"{sym} 模拟期末持仓为 0，但引擎报告 "
                    f"total_quantity={final_positions[sym].total_quantity}"
                )

    # final_positions 中所有持仓数量 >= 0
    for sym, pos in final_positions.items():
        assert pos.total_quantity >= 0, (
            f"{sym} 期末持仓不应为负: {pos.total_quantity}"
        )
        # sellable + frozen 应等于 total
        assert pos.sellable_quantity + pos.frozen_buy_quantity == pos.total_quantity, (
            f"{sym} 持仓分解不一致: sellable={pos.sellable_quantity} "
            f"frozen={pos.frozen_buy_quantity} total={pos.total_quantity}"
        )

    # 使用即时持仓快照验证 BUY 时刻持仓增加
    buy_snapshots = [
        (f, old, new) for f, old, new in fill_snapshots
        if f.side == Side.BUY
    ]
    assert len(buy_snapshots) > 0, "应至少有一笔 BUY 成交快照"
    for fill, old_qty, new_qty in buy_snapshots:
        assert new_qty == old_qty + fill.quantity, (
            f"BUY {fill.symbol} {fill.fill_date}: "
            f"成交后持仓 {new_qty} 应等于 {old_qty}+{fill.quantity}"
        )
        assert new_qty > old_qty, (
            f"BUY {fill.symbol} {fill.fill_date}: "
            f"持仓应增加 old={old_qty} new={new_qty}"
        )

    # 首笔 BUY 应使持仓从 0 变为正
    first_buy_fill, first_buy_old, first_buy_new = buy_snapshots[0]
    assert first_buy_old == 0, (
        f"首笔 BUY {first_buy_fill.symbol} {first_buy_fill.fill_date}: "
        f"买入前持仓应为 0，实际 {first_buy_old}"
    )
    assert first_buy_new == first_buy_fill.quantity, (
        f"首笔 BUY {first_buy_fill.symbol} {first_buy_fill.fill_date}: "
        f"买入后持仓应等于 {first_buy_fill.quantity}，实际 {first_buy_new}"
    )

    # 使用即时持仓快照验证 SELL 时刻持仓减少
    sell_snapshots = [
        (f, old, new) for f, old, new in fill_snapshots
        if f.side == Side.SELL
    ]
    assert len(sell_snapshots) > 0, "应至少有一笔 SELL 成交快照"
    for fill, old_qty, new_qty in sell_snapshots:
        assert new_qty == old_qty - fill.quantity, (
            f"SELL {fill.symbol} {fill.fill_date}: "
            f"成交后持仓 {new_qty} 应等于 {old_qty}-{fill.quantity}"
        )
        assert new_qty < old_qty, (
            f"SELL {fill.symbol} {fill.fill_date}: "
            f"持仓应减少 old={old_qty} new={new_qty}"
        )


# =========================================================================== #
# 6. 权益追踪验证
# =========================================================================== #


def test_real_e2e_equity_tracking(
    e2e_result: BacktestResult,
    e2e_config: BacktestConfig,
):
    """验证每日权益快照的正确性。

    断言：
    - daily_equity 非空且条目数等于回测交易日数
    - 每个快照的 total_equity == cash + position_value（权益恒等式）
    - 首日 total_equity == initial_cash（全现金，无持仓）
    - total_equity 在回测期间发生变化（非恒定）
    - daily_pnl 为当日权益与前日权益之差
    - cumulative_pnl 为当日权益与初始资金之差
    - drawdown 始终在 [0, 1] 区间
    - 买入后现金减少，卖出后现金增加
    """
    daily_equity = e2e_result.daily_equity
    initial_cash = Decimal(str(e2e_config.initial_cash))

    # daily_equity 非空
    assert len(daily_equity) > 0, "daily_equity 不应为空"
    assert len(daily_equity) >= 300, (
        f"daily_equity 应至少有 300 条记录，实际 {len(daily_equity)}"
    )

    # 首日权益 == initial_cash（全现金，无持仓）
    first_snap = daily_equity[0]
    assert first_snap.total_equity == initial_cash, (
        f"首日 total_equity={first_snap.total_equity} "
        f"应等于 initial_cash={initial_cash}"
    )
    assert first_snap.cash == initial_cash, (
        f"首日 cash={first_snap.cash} 应等于 initial_cash={initial_cash}"
    )
    assert first_snap.position_value == 0, (
        f"首日 position_value={first_snap.position_value} 应为 0"
    )

    # 权益恒等式：total_equity == cash + position_value
    for snap in daily_equity:
        assert snap.total_equity == snap.cash + snap.position_value, (
            f"{snap.snapshot_date}: total_equity={snap.total_equity} "
            f"不等于 cash+position_value="
            f"{snap.cash + snap.position_value}"
        )

    # total_equity 在回测期间发生变化
    equities = [s.total_equity for s in daily_equity]
    unique_equities = set(equities)
    assert len(unique_equities) > 1, (
        "total_equity 在回测期间应发生变化，但所有值相同"
    )

    # daily_pnl 为当日权益与前日权益之差
    for i in range(1, len(daily_equity)):
        prev = daily_equity[i - 1]
        curr = daily_equity[i]
        expected_pnl = curr.total_equity - prev.total_equity
        assert curr.daily_pnl == expected_pnl, (
            f"{curr.snapshot_date}: daily_pnl={curr.daily_pnl} "
            f"不等于 total_equity-prev={expected_pnl}"
        )

    # cumulative_pnl 为当日权益与初始资金之差
    for snap in daily_equity:
        expected_cum = snap.total_equity - initial_cash
        assert snap.cumulative_pnl == expected_cum, (
            f"{snap.snapshot_date}: cumulative_pnl={snap.cumulative_pnl} "
            f"不等于 total_equity-initial_cash={expected_cum}"
        )

    # drawdown 在 [0, 1] 区间
    for snap in daily_equity:
        assert Decimal("0") <= snap.drawdown <= Decimal("1"), (
            f"{snap.snapshot_date}: drawdown={snap.drawdown} "
            f"应在 [0, 1] 区间"
        )

    # 买入后现金减少：找到首个非零持仓的快照，现金应小于 initial_cash
    snapshots_with_position = [
        s for s in daily_equity if s.position_value > 0
    ]
    assert len(snapshots_with_position) > 0, (
        "应至少有一个持有头寸的交易日"
    )
    first_pos_snap = snapshots_with_position[0]
    assert first_pos_snap.cash < initial_cash, (
        f"首个持仓日 {first_pos_snap.snapshot_date}: "
        f"cash={first_pos_snap.cash} 应小于 initial_cash={initial_cash}（买入消耗现金）"
    )

    # 卖出后现金增加：找到持仓从非零变为零的快照
    for i in range(1, len(daily_equity)):
        prev = daily_equity[i - 1]
        curr = daily_equity[i]
        if prev.position_value > 0 and curr.position_value == 0:
            # 卖出后现金应增加
            assert curr.cash > prev.cash, (
                f"{curr.snapshot_date}: 卖出后 cash={curr.cash} "
                f"应大于前日 cash={prev.cash}"
            )
            break

    # 期末权益与初始资金的差异应合理（不会因费用导致权益为负）
    final_snap = daily_equity[-1]
    assert final_snap.total_equity > 0, (
        f"期末 total_equity={final_snap.total_equity} 应为正"
    )

    # 日期连续性：每个快照日期严格递增
    for i in range(1, len(daily_equity)):
        assert daily_equity[i].snapshot_date > daily_equity[i - 1].snapshot_date, (
            f"快照日期应严格递增: {daily_equity[i - 1].snapshot_date} "
            f"-> {daily_equity[i].snapshot_date}"
        )
