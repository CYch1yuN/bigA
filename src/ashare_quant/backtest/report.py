"""回测报告生成：JSON 字典与 Markdown 文本。

- ``generate_json`` 生成可 ``json.dumps`` 的完整报告字典。
- ``generate_markdown`` 生成人类可读的 Markdown 报告。
- ``generate_*_dataframe`` 将流水转为 ``pandas.DataFrame``。

所有 ``Decimal`` 在序列化时转为 ``float``，``date`` 转为 ISO 字符串，
枚举转为其 ``value``。
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any

import pandas as pd

from .config import BacktestConfig
from .metrics import MetricsCalculator
from .models import (
    BacktestResult,
    Fill,
    Order,
    OrderStatus,
    PortfolioSnapshot,
    Position,
    RejectReason,
    Side,
)


class ReportGenerator:
    """回测报告生成器。"""

    #: 默认限制声明，报告始终至少包含这些条目
    DEFAULT_LIMITATIONS: list[str] = [
        "Phase 2 不处理分红、送股、拆并股和配股",
        "前复权价格仅用于信号，未复权价格用于成交",
        "仅支持下一交易日开盘市价撮合",
    ]

    def __init__(self) -> None:
        self._calc = MetricsCalculator()

    # ------------------------------------------------------------------
    # JSON 报告
    # ------------------------------------------------------------------

    def generate_json(
        self, result: BacktestResult, initial_cash: Decimal
    ) -> dict[str, Any]:
        """生成完整的 JSON 报告字典。

        包含：配置摘要、指标、订单流水、成交流水、每日权益、
        期末持仓、限制说明、数据范围、内容哈希、代码提交号。

        Args:
            result: 回测结果。
            initial_cash: 初始资金（Decimal）。

        Returns:
            可直接 ``json.dumps`` 的字典。
        """
        metrics = self._calc.calculate(result, initial_cash)
        report: dict[str, Any] = {
            "config_summary": self._to_jsonable(result.config_summary),
            "metrics": self._to_jsonable(metrics),
            "orders": [self._order_to_dict(o) for o in result.orders],
            "fills": [self._fill_to_dict(f) for f in result.fills],
            "daily_equity": [
                self._snapshot_to_dict(s) for s in result.daily_equity
            ],
            "final_positions": {
                sym: self._position_to_dict(p)
                for sym, p in result.final_positions.items()
            },
            "limitations": self._merge_limitations(result.limitations),
            "data_range": self._to_jsonable(result.data_range),
            "content_hash": result.content_hash,
            "code_commit": result.code_commit,
        }
        return report

    def to_json_string(
        self,
        result: BacktestResult,
        initial_cash: Decimal,
        indent: int = 2,
        ensure_ascii: bool = False,
    ) -> str:
        """便捷方法：直接返回 JSON 字符串。"""
        return json.dumps(
            self.generate_json(result, initial_cash),
            ensure_ascii=ensure_ascii,
            indent=indent,
        )

    # ------------------------------------------------------------------
    # Markdown 报告
    # ------------------------------------------------------------------

    def generate_markdown(
        self, result: BacktestResult, initial_cash: Decimal
    ) -> str:
        """生成 Markdown 报告。

        包含：标题与基本信息、核心指标表格、拒绝原因统计、
        每日权益表、成交流水表、限制声明。
        """
        m = self._calc.calculate(result, initial_cash)
        lines: list[str] = []

        lines.append("# A股量化回测报告")
        lines.append("")

        # 一、基本信息
        lines.append("## 一、基本信息")
        lines.append("")
        period = self._format_data_range(result.data_range)
        trading_days = len(result.daily_equity)
        lines.append(f"- 回测期间: {period}")
        lines.append(f"- 交易天数: {trading_days}")
        lines.append(f"- 初始资金: {self._fmt_money(m['initial_equity'])}")
        lines.append(f"- 最终权益: {self._fmt_money(m['final_equity'])}")
        lines.append(f"- 总收益率: {self._fmt_pct(m['total_return'])}")
        if result.content_hash:
            lines.append(f"- 内容哈希: {result.content_hash}")
        if result.code_commit:
            lines.append(f"- 代码提交号: {result.code_commit}")
        lines.append("")

        # 二、核心指标
        lines.append("## 二、核心指标")
        lines.append("")
        lines.append("| 指标 | 值 |")
        lines.append("| --- | --- |")
        lines.append(f"| 初始权益 | {self._fmt_money(m['initial_equity'])} |")
        lines.append(f"| 最终权益 | {self._fmt_money(m['final_equity'])} |")
        lines.append(f"| 总收益率 | {self._fmt_pct(m['total_return'])} |")
        lines.append(
            f"| 年化收益率 | {self._fmt_pct(m['annualized_return'])} |"
        )
        lines.append(f"| 最大回撤 | {self._fmt_pct(m['max_drawdown'])} |")
        lines.append(f"| 交易次数 | {m['total_trades']} |")
        lines.append(f"| 胜率 | {self._fmt_pct(m['win_rate'])} |")
        lines.append(f"| 盈亏比 | {self._fmt_num(m['profit_loss_ratio'])} |")
        lines.append(f"| 换手率 | {self._fmt_pct(m['turnover_rate'])} |")
        lines.append(
            f"| 不可成交订单率 | {self._fmt_pct(m['unfillable_order_rate'])} |"
        )
        lines.append("")

        # 三、拒绝原因统计
        lines.append("## 三、拒绝原因统计")
        lines.append("")
        rrc = m.get("reject_reason_counts") or {}
        if rrc:
            lines.append("| 拒绝原因 | 次数 |")
            lines.append("| --- | --- |")
            for reason, cnt in sorted(
                rrc.items(), key=lambda x: (-x[1], x[0])
            ):
                lines.append(f"| {reason} | {cnt} |")
        else:
            lines.append("无拒绝/取消记录。")
        lines.append("")

        # 四、每日权益表
        lines.append("## 四、每日权益")
        lines.append("")
        if result.daily_equity:
            lines.append(
                "| 日期 | 现金 | 持仓市值 | 总权益 | 当日盈亏 | 累计盈亏 | 回撤 |"
            )
            lines.append("| --- | --- | --- | --- | --- | --- | --- |")
            for snap in result.daily_equity:
                lines.append(
                    f"| {snap.snapshot_date.isoformat()} "
                    f"| {self._fmt_money(snap.cash)} "
                    f"| {self._fmt_money(snap.position_value)} "
                    f"| {self._fmt_money(snap.total_equity)} "
                    f"| {self._fmt_money(snap.daily_pnl)} "
                    f"| {self._fmt_money(snap.cumulative_pnl)} "
                    f"| {self._fmt_pct(snap.drawdown)} |"
                )
        else:
            lines.append("无每日权益数据。")
        lines.append("")

        # 五、成交流水表
        lines.append("## 五、成交流水")
        lines.append("")
        if result.fills:
            lines.append(
                "| 成交日 | 订单ID | 代码 | 方向 | 数量 | 滑点价 | 佣金 | "
                "印花税 | 过户费 | 总费用 | 现金变化 | 成交额 |"
            )
            lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | "
                "--- | --- | --- | --- |")
            for f in result.fills:
                lines.append(
                    f"| {f.fill_date.isoformat()} "
                    f"| {f.order_id} "
                    f"| {f.symbol} "
                    f"| {self._enum_value(f.side)} "
                    f"| {f.quantity} "
                    f"| {self._fmt_num(f.slippage_price, 4)} "
                    f"| {self._fmt_money(f.commission)} "
                    f"| {self._fmt_money(f.stamp_duty)} "
                    f"| {self._fmt_money(f.transfer_fee)} "
                    f"| {self._fmt_money(f.total_cost)} "
                    f"| {self._fmt_money(f.cash_change)} "
                    f"| {self._fmt_money(f.turnover)} |"
                )
        else:
            lines.append("无成交流水。")
        lines.append("")

        # 六、订单拒绝明细
        rejected_or_cancelled = [
            o for o in result.orders
            if o.status in (OrderStatus.REJECTED, OrderStatus.CANCELLED)
        ]
        lines.append("## 六、订单拒绝/取消明细")
        lines.append("")
        if rejected_or_cancelled:
            lines.append(
                "| 订单ID | 代码 | 方向 | 数量 | 状态 | 拒绝原因 | 拒绝详情 |"
            )
            lines.append("| --- | --- | --- | --- | --- | --- | --- |")
            for o in rejected_or_cancelled:
                lines.append(
                    f"| {o.order_id} "
                    f"| {o.signal.symbol} "
                    f"| {self._enum_value(o.signal.side)} "
                    f"| {o.signal.quantity} "
                    f"| {self._enum_value(o.status)} "
                    f"| {self._enum_value(o.reject_reason) or '-'} "
                    f"| {o.reject_detail or '-'} |"
                )
        else:
            lines.append("无拒绝/取消订单。")
        lines.append("")

        # 七、限制声明
        lines.append("## 七、限制声明")
        lines.append("")
        for item in self._merge_limitations(result.limitations):
            lines.append(f"- {item}")
        lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # DataFrame 生成
    # ------------------------------------------------------------------

    def generate_orders_dataframe(
        self, result: BacktestResult
    ) -> pd.DataFrame:
        """将订单流水转为 DataFrame。"""
        columns = [
            "order_id",
            "signal_date",
            "symbol",
            "side",
            "quantity",
            "reason",
            "planned_fill_date",
            "status",
            "reject_reason",
            "reject_detail",
            "filled",
        ]
        rows: list[dict[str, Any]] = []
        for o in result.orders:
            sig = o.signal
            rows.append(
                {
                    "order_id": o.order_id,
                    "signal_date": sig.signal_date,
                    "symbol": sig.symbol,
                    "side": self._enum_value(sig.side),
                    "quantity": sig.quantity,
                    "reason": sig.reason,
                    "planned_fill_date": o.planned_fill_date,
                    "status": self._enum_value(o.status),
                    "reject_reason": (
                        self._enum_value(o.reject_reason)
                        if o.reject_reason is not None
                        else None
                    ),
                    "reject_detail": o.reject_detail,
                    "filled": o.fill is not None,
                }
            )
        return pd.DataFrame(rows, columns=columns)

    def generate_fills_dataframe(
        self, result: BacktestResult
    ) -> pd.DataFrame:
        """将成交流水转为 DataFrame。"""
        columns = [
            "order_id",
            "fill_date",
            "symbol",
            "side",
            "quantity",
            "raw_open_price",
            "slippage_price",
            "commission",
            "stamp_duty",
            "transfer_fee",
            "total_cost",
            "cash_change",
            "turnover",
        ]
        rows: list[dict[str, Any]] = []
        for f in result.fills:
            rows.append(
                {
                    "order_id": f.order_id,
                    "fill_date": f.fill_date,
                    "symbol": f.symbol,
                    "side": self._enum_value(f.side),
                    "quantity": f.quantity,
                    "raw_open_price": f.raw_open_price,
                    "slippage_price": f.slippage_price,
                    "commission": f.commission,
                    "stamp_duty": f.stamp_duty,
                    "transfer_fee": f.transfer_fee,
                    "total_cost": f.total_cost,
                    "cash_change": f.cash_change,
                    "turnover": f.turnover,
                }
            )
        return pd.DataFrame(rows, columns=columns)

    def generate_equity_dataframe(
        self, result: BacktestResult
    ) -> pd.DataFrame:
        """将每日权益转为 DataFrame。"""
        columns = [
            "snapshot_date",
            "cash",
            "position_value",
            "total_equity",
            "daily_pnl",
            "cumulative_pnl",
            "drawdown",
        ]
        rows: list[dict[str, Any]] = []
        for snap in result.daily_equity:
            rows.append(
                {
                    "snapshot_date": snap.snapshot_date,
                    "cash": snap.cash,
                    "position_value": snap.position_value,
                    "total_equity": snap.total_equity,
                    "daily_pnl": snap.daily_pnl,
                    "cumulative_pnl": snap.cumulative_pnl,
                    "drawdown": snap.drawdown,
                }
            )
        return pd.DataFrame(rows, columns=columns)

    # ------------------------------------------------------------------
    # 序列化与格式化辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _to_jsonable(obj: Any) -> Any:
        """递归将 Decimal/date/Enum 转为 JSON 可序列化类型。"""
        if obj is None:
            return None
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, Enum):
            return obj.value
        if isinstance(obj, date):
            return obj.isoformat()
        if isinstance(obj, dict):
            return {
                str(k): ReportGenerator._to_jsonable(v) for k, v in obj.items()
            }
        if isinstance(obj, (list, tuple)):
            return [ReportGenerator._to_jsonable(v) for v in obj]
        return obj

    @staticmethod
    def _enum_value(value: Any) -> Any:
        """安全获取枚举的 value，非枚举原样返回。"""
        if isinstance(value, Enum):
            return value.value
        return value

    @staticmethod
    def _order_to_dict(order: Order) -> dict[str, Any]:
        sig = order.signal
        return {
            "order_id": order.order_id,
            "signal_date": sig.signal_date.isoformat()
            if sig.signal_date
            else None,
            "symbol": sig.symbol,
            "side": ReportGenerator._enum_value(sig.side),
            "quantity": sig.quantity,
            "reason": sig.reason,
            "planned_fill_date": order.planned_fill_date.isoformat()
            if order.planned_fill_date
            else None,
            "status": ReportGenerator._enum_value(order.status),
            "reject_reason": (
                ReportGenerator._enum_value(order.reject_reason)
                if order.reject_reason is not None
                else None
            ),
            "reject_detail": order.reject_detail,
            "fill": (
                ReportGenerator._fill_to_dict(order.fill)
                if order.fill is not None
                else None
            ),
        }

    @staticmethod
    def _fill_to_dict(fill: Fill) -> dict[str, Any]:
        return {
            "order_id": fill.order_id,
            "fill_date": fill.fill_date.isoformat(),
            "symbol": fill.symbol,
            "side": ReportGenerator._enum_value(fill.side),
            "quantity": fill.quantity,
            "raw_open_price": float(fill.raw_open_price),
            "slippage_price": float(fill.slippage_price),
            "commission": float(fill.commission),
            "stamp_duty": float(fill.stamp_duty),
            "transfer_fee": float(fill.transfer_fee),
            "total_cost": float(fill.total_cost),
            "cash_change": float(fill.cash_change),
            "turnover": float(fill.turnover),
        }

    @staticmethod
    def _snapshot_to_dict(snap: PortfolioSnapshot) -> dict[str, Any]:
        return {
            "snapshot_date": snap.snapshot_date.isoformat(),
            "cash": float(snap.cash),
            "position_value": float(snap.position_value),
            "total_equity": float(snap.total_equity),
            "daily_pnl": float(snap.daily_pnl),
            "cumulative_pnl": float(snap.cumulative_pnl),
            "drawdown": float(snap.drawdown),
        }

    @staticmethod
    def _position_to_dict(pos: Position) -> dict[str, Any]:
        return {
            "symbol": pos.symbol,
            "total_quantity": pos.total_quantity,
            "sellable_quantity": pos.sellable_quantity,
            "frozen_buy_quantity": pos.frozen_buy_quantity,
            "avg_raw_cost": float(pos.avg_raw_cost),
        }

    @classmethod
    def _merge_limitations(cls, limitations: list[str] | None) -> list[str]:
        """合并限制声明，保证至少包含默认条目。"""
        merged: list[str] = list(limitations) if limitations else []
        for item in cls.DEFAULT_LIMITATIONS:
            if item not in merged:
                merged.append(item)
        return merged

    @staticmethod
    def _format_data_range(data_range: Any) -> str:
        """格式化数据范围为可读字符串。"""
        if not data_range:
            return "N/A"
        if isinstance(data_range, dict):
            start = data_range.get("start_date")
            end = data_range.get("end_date")
            if start and end:
                return f"{start} ~ {end}"
            if start:
                return f"{start} ~ N/A"
            # 兜底：展示键值
            parts = [f"{k}={v}" for k, v in data_range.items()]
            return ", ".join(parts) if parts else "N/A"
        return str(data_range)

    # 格式化工具
    @staticmethod
    def _fmt_money(value: Any) -> str:
        if value is None:
            return "N/A"
        return f"{float(value):,.2f}"

    @staticmethod
    def _fmt_pct(value: Any) -> str:
        if value is None:
            return "N/A"
        return f"{float(value) * 100:.2f}%"

    @staticmethod
    def _fmt_num(value: Any, places: int = 2) -> str:
        if value is None:
            return "N/A"
        return f"{float(value):.{places}f}"


__all__ = ["ReportGenerator"]
