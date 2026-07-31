"""基准比较模块。

持有沪深300、中证全指与现金基准的价格序列，并按测试期首个可用收盘至
期末收盘计算基准总收益。基准不模拟交易费用；缺失基准日期按交易日交集
对齐，禁止前向/后向填充未来值。

中证全指若无法获得，必须抛出 :class:`BenchmarkMissingError` 并停止正式研究，
不得用其他基准替代，也不得用当前状态回填历史。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

# 交易年近似交易日数（与 Phase 3 蒙特卡洛、波动率年化口径一致）
TRADING_DAYS_PER_YEAR = 244


class BenchmarkMissingError(Exception):
    """必需基准缺失异常。

    当沪深300或中证全指等必需基准无法获得、或在测试期内无任何可用收盘价时抛出。
    必须停止正式 Gate 3 研究，不得用其他基准替代，也不得用未来值回填。
    """


@dataclass
class BenchmarkData:
    """基准价格数据。

    持有沪深300与中证全指的收盘价序列；现金基准无价格序列，其收益固定为 0%，
    未投资天数由 :func:`compute_cash_benchmark` 单独统计。

    Attributes:
        trade_dates: 基准交易日历（升序、去重），为各基准序列可用日期的并集。
        hs300_close: 沪深300收盘价字典 ``{trade_date: close}``，仅含有效非缺失值。
        csi_all_close: 中证全指收盘价字典 ``{trade_date: close}``，仅含有效非缺失值。
    """

    trade_dates: list[date] = field(default_factory=list)
    hs300_close: dict[date, float] = field(default_factory=dict)
    csi_all_close: dict[date, float] = field(default_factory=dict)

    @property
    def has_hs300(self) -> bool:
        """是否持有有效的沪深300收盘价序列。"""
        return len(self.hs300_close) > 0

    @property
    def has_csi_all(self) -> bool:
        """是否持有有效的中证全指收盘价序列。"""
        return len(self.csi_all_close) > 0


def load_benchmarks(parquet_path: str) -> BenchmarkData:
    """从 Parquet 文件加载基准数据。

    Parquet schema::

        trade_date      # 交易日
        hs300_close     # 沪深300收盘价
        csi_all_close   # 中证全指收盘价

    缺失值（NaN/None）的行不会进入对应基准序列；中证全指列缺失或全为空时抛出
    :class:`BenchmarkMissingError`。

    Args:
        parquet_path: Parquet 文件路径。

    Returns:
        :class:`BenchmarkData` 实例。

    Raises:
        FileNotFoundError: 文件不存在。
        BenchmarkMissingError: 必需基准（沪深300或中证全指）缺失或全为空。
    """
    path = Path(parquet_path)
    if not path.exists():
        raise FileNotFoundError(f"基准 Parquet 文件不存在: {path}")

    df = pd.read_parquet(path)
    if "trade_date" not in df.columns and df.index.name == "trade_date":
        df = df.reset_index()

    required_cols = {"trade_date", "hs300_close", "csi_all_close"}
    missing_cols = required_cols - set(df.columns)
    if missing_cols:
        raise BenchmarkMissingError(
            f"基准 Parquet 缺少必需列: {sorted(missing_cols)}"
        )

    trade_dates: list[date] = []
    hs300: dict[date, float] = {}
    csi_all: dict[date, float] = {}

    raw_dates = df["trade_date"].tolist()
    raw_hs300 = df["hs300_close"].tolist()
    raw_csi = df["csi_all_close"].tolist()
    for raw_date, raw_hs, raw_csi in zip(raw_dates, raw_hs300, raw_csi):
        d = _to_date(raw_date)
        trade_dates.append(d)
        if _is_valid_price(raw_hs):
            hs300[d] = float(raw_hs)
        if _is_valid_price(raw_csi):
            csi_all[d] = float(raw_csi)

    trade_dates = sorted(set(trade_dates))

    if not hs300:
        raise BenchmarkMissingError(
            "沪深300收盘价序列缺失或全为空，无法进行基准比较"
        )
    if not csi_all:
        raise BenchmarkMissingError(
            "中证全指收盘价序列缺失或全为空，必须停止正式研究，不得替代"
        )

    return BenchmarkData(
        trade_dates=trade_dates,
        hs300_close=hs300,
        csi_all_close=csi_all,
    )


def compute_benchmark_returns(
    benchmark: BenchmarkData, start_date: date, end_date: date
) -> dict[str, float]:
    """计算各基准在 ``[start_date, end_date]`` 的总收益。

    对每个价格基准，取测试期内首个可用收盘价（``>= start_date``）与末个可用
    收盘价（``<= end_date``），按 ``last / first - 1`` 计算总收益。缺失日期按
    交易日交集对齐，不做任何前向/后向填充；不模拟交易费用。现金基准收益固定
    为 0%。

    Args:
        benchmark: 基准数据。
        start_date: 测试期起始日（无需为交易日）。
        end_date: 测试期截止日（无需为交易日）。

    Returns:
        ``{"hs300": float, "csi_all": float, "cash": 0.0}``。

    Raises:
        ValueError: ``start_date`` 晚于 ``end_date``。
        BenchmarkMissingError: 沪深300或中证全指在测试期内无可用收盘价。
    """
    if start_date > end_date:
        raise ValueError(
            f"start_date({start_date}) 晚于 end_date({end_date})"
        )

    hs300_return = _series_total_return(
        benchmark.hs300_close,
        benchmark.trade_dates,
        start_date,
        end_date,
        "沪深300",
    )
    csi_all_return = _series_total_return(
        benchmark.csi_all_close,
        benchmark.trade_dates,
        start_date,
        end_date,
        "中证全指",
    )
    return {"hs300": hs300_return, "csi_all": csi_all_return, "cash": 0.0}


def compute_cash_benchmark(
    daily_equity: list[Any], trading_days: int
) -> dict[str, Any]:
    """现金基准：收益固定为 0%，并统计未投资天数。

    未投资天数为日权益序列中持仓市值为 0 的交易日数（即全程持有现金）。现金
    基准不承担交易费用，亦不产生任何收益，仅作为超额收益的最低基准线。

    Args:
        daily_equity: 日权益快照列表（如 ``PortfolioSnapshot``），需含
            ``position_value`` 属性或 ``"position_value"`` 键。
        trading_days: 对应区间的交易日总数。

    Returns:
        包含以下字段的字典：

        - ``total_return``: 固定为 ``0.0``。
        - ``trading_days``: 传入的交易日总数。
        - ``uninvested_days``: 持仓市值为 0 的交易日数。
        - ``invested_days``: 持有持仓的交易日数。
        - ``cash_ratio``: 未投资天数占比 ``uninvested_days / total``。
    """
    uninvested = 0
    total = 0
    for snap in daily_equity:
        total += 1
        position_value = _get_position_value(snap)
        if position_value is None or position_value <= 0:
            uninvested += 1
    invested = total - uninvested
    cash_ratio = uninvested / total if total > 0 else 0.0
    return {
        "total_return": 0.0,
        "trading_days": trading_days,
        "uninvested_days": uninvested,
        "invested_days": invested,
        "cash_ratio": cash_ratio,
    }


# ----------------------------------------------------------------------
# 内部辅助
# ----------------------------------------------------------------------


def _series_total_return(
    closes: dict[date, float],
    trade_dates: list[date],
    start_date: date,
    end_date: date,
    name: str,
) -> float:
    """计算单条基准价格序列在窗口内的总收益。

    仅使用 ``trade_dates`` 中落在 ``[start_date, end_date]`` 且在 ``closes``
    中存在有效值的日期，取首尾收盘价计算总收益。不做任何填充。
    """
    available = [
        d
        for d in trade_dates
        if start_date <= d <= end_date and d in closes
    ]
    if not available:
        raise BenchmarkMissingError(
            f"{name}在 {start_date} 至 {end_date} 内无可用收盘价，"
            "无法计算基准收益"
        )
    first_close = closes[available[0]]
    last_close = closes[available[-1]]
    if first_close <= 0:
        raise BenchmarkMissingError(
            f"{name}首日收盘价非正({first_close})，无法计算基准收益"
        )
    return last_close / first_close - 1.0


def _is_valid_price(value: Any) -> bool:
    """判断价格值是否有效：非 None、非 NaN/Inf 且为正。"""
    if value is None:
        return False
    try:
        f = float(value)
    except (TypeError, ValueError):
        return False
    if math.isnan(f) or math.isinf(f):
        return False
    return f > 0


def _to_date(value: Any) -> date:
    """将 ``datetime``/``Timestamp``/``date``/``str`` 转换为 :class:`date`。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        return date.fromisoformat(value)
    # 兜底：兼容 pandas Timestamp 等带 .date() 方法的对象
    if hasattr(value, "date") and callable(getattr(value, "date")):
        return value.date()
    raise TypeError(f"无法解析的日期类型: {type(value).__name__}")


def _get_position_value(snap: Any) -> float | None:
    """从日权益快照中取出持仓市值，兼容对象属性与字典。"""
    if hasattr(snap, "position_value"):
        return _to_float(getattr(snap, "position_value"))
    if isinstance(snap, dict) and "position_value" in snap:
        return _to_float(snap["position_value"])
    return None


def _to_float(value: Any) -> float | None:
    """安全转换为 float，NaN/不可转换时返回 None。"""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f):
        return None
    return f


__all__ = [
    "TRADING_DAYS_PER_YEAR",
    "BenchmarkMissingError",
    "BenchmarkData",
    "load_benchmarks",
    "compute_benchmark_returns",
    "compute_cash_benchmark",
]
