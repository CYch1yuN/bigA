"""确定性脚本策略：从预加载信号列表生成订单，用于回测器验证。

不实现任何真实交易逻辑，仅用于测试回测基础设施。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Optional

from .interfaces import Strategy
from .models import Signal, Side, StrategyContext


class ScriptedStrategy(Strategy):
    """脚本策略：从预加载的信号列表中返回当日信号。

    信号来源可以是 JSON 文件或直接传入的信号列表。
    JSON 格式：[{"signal_date": "2024-01-02", "symbol": "000001", "side": "BUY", "quantity": 100, "reason": "..."}]
    """

    def __init__(self, signals: Optional[list[Signal]] = None) -> None:
        self._signals_by_date: dict[date, list[Signal]] = {}
        if signals:
            for s in signals:
                self._signals_by_date.setdefault(s.signal_date, []).append(s)

    @classmethod
    def from_json(cls, path: str | Path) -> "ScriptedStrategy":
        """从 JSON 文件加载信号。"""
        path = Path(path)
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        signals = []
        for item in raw:
            signals.append(
                Signal(
                    signal_date=date.fromisoformat(item["signal_date"]),
                    symbol=item["symbol"],
                    side=Side(item["side"]),
                    quantity=int(item["quantity"]),
                    reason=item.get("reason", ""),
                )
            )
        return cls(signals)

    @classmethod
    def from_list(cls, signals: list[Signal]) -> "ScriptedStrategy":
        """从信号列表创建。"""
        return cls(signals)

    def on_close(self, context: StrategyContext) -> list[Signal]:
        """返回当日预加载的信号。"""
        return self._signals_by_date.get(context.current_date, [])


class NoOpStrategy(Strategy):
    """空策略：不生成任何信号，用于测试零交易场景。"""

    def on_close(self, context: StrategyContext) -> list[Signal]:
        return []


class BuyAndHoldStrategy(Strategy):
    """买入持有策略：第一天买入指定 symbol，之后不操作。

    仅用于测试，不作为真实策略。
    """

    def __init__(self, symbol: str, quantity: int = 100) -> None:
        self._symbol = symbol
        self._quantity = quantity
        self._bought = False

    def on_close(self, context: StrategyContext) -> list[Signal]:
        if not self._bought:
            self._bought = True
            return [
                Signal(
                    signal_date=context.current_date,
                    symbol=self._symbol,
                    side=Side.BUY,
                    quantity=self._quantity,
                    reason="买入持有",
                )
            ]
        return []


__all__ = ["ScriptedStrategy", "NoOpStrategy", "BuyAndHoldStrategy"]
