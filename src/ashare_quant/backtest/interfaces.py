"""回测抽象接口定义。

所有接口使用 ABC，确保子类必须实现关键方法。
策略接口设计为防未来函数：只能读取截至当日收盘的数据。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Any, Optional

import pandas as pd

from .config import BacktestConfig
from .models import (
    BacktestResult,
    BarData,
    EligibilityDecision,
    Fill,
    Order,
    PortfolioSnapshot,
    Position,
    RiskDecision,
    Signal,
    StrategyContext,
)


class Strategy(ABC):
    """交易策略抽象接口。

    策略在每日收盘后被调用，只能读取截至当日收盘的数据。
    生成的信号最早在下一交易日开盘成交。
    """

    @abstractmethod
    def on_close(self, context: StrategyContext) -> list[Signal]:
        """收盘后生成交易信号。

        Args:
            context: 策略上下文，包含截至当日的行情和持仓信息。

        Returns:
            信号列表，将在下一交易日开盘尝试成交。
        """
        ...


class UniverseFilter(ABC):
    """股票池过滤器抽象接口。"""

    @abstractmethod
    def is_eligible(
        self, symbol: str, dt: date, context: StrategyContext
    ) -> EligibilityDecision:
        """判断某股票在某日是否可交易。

        Args:
            symbol: 股票代码。
            dt: 交易日。
            context: 策略上下文。

        Returns:
            过滤决策。
        """
        ...


class RiskManager(ABC):
    """风控管理器抽象接口。"""

    @abstractmethod
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
            bar: 当日行情（可能为 None）。
            config: 回测配置。
            positions: 当前持仓。

        Returns:
            风控决策。
        """
        ...


class BrokerSimulator(ABC):
    """成交模拟器抽象接口。"""

    @abstractmethod
    def execute(
        self,
        order: Order,
        bar: BarData,
        portfolio: PortfolioSnapshot,
        config: BacktestConfig,
        positions: dict[str, Position],
    ) -> Optional[Fill]:
        """执行订单撮合。

        Args:
            order: 待执行订单。
            bar: 成交日行情。
            portfolio: 当前组合快照。
            config: 回测配置。
            positions: 当前持仓。

        Returns:
            成交记录或 None（拒绝时由调用方设置 reject_reason）。
        """
        ...

    @abstractmethod
    def check_rejection(
        self,
        order: Order,
        bar: Optional[BarData],
        portfolio: PortfolioSnapshot,
        config: BacktestConfig,
        positions: dict[str, Position],
    ) -> Optional[RiskDecision]:
        """检查订单是否应被拒绝（在 execute 之前调用）。

        Returns:
            拒绝决策或 None（通过）。
        """
        ...


class BacktestEngine(ABC):
    """回测引擎抽象接口。"""

    @abstractmethod
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
            data: curated 日行情 DataFrame。
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
        ...


__all__ = [
    "Strategy",
    "UniverseFilter",
    "RiskManager",
    "BrokerSimulator",
    "BacktestEngine",
]
