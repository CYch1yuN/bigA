# -*- coding: utf-8 -*-
"""westock MCP 数据适配器：data_kline 响应 -> 校验器标准输入。

核心职责（2026-08-03 实测固化）：
- ``volume`` 单位换算：westock data_kline 返回的成交量单位为「手」(100 股)，
  本地 curated（BaoStock 等）为「股」。自动 hook 接入前必须在此换算，
  否则会产生约 99% 的虚假成交量偏差。
- 列名归一：``last`` -> ``close``（与 WestockValidator 内部列名一致）。

用法：
    from ashare_quant.validators.westock_adapter import build_fetcher_from_kline
    fetcher = build_fetcher_from_kline(raw["symbols"])   # {symbol: node, ...}
    result = validator.validate(primary, symbol, start, end, fetch=fetcher)
"""
from __future__ import annotations

from typing import Any, Callable

import pandas as pd

# westock 成交量原始单位：手（100 股/手）
VOLUME_LOT_SHARES = 100.0

# 单位换算元数据（适配结果必须带此元数据，可追溯、不可在通用校验内猜单位）
VOLUME_UNIT_ATTR = "volume_unit"
VOLUME_UNIT_META = {
    "source_unit": "lot",
    "normalized_unit": "share",
    "multiplier": VOLUME_LOT_SHARES,
}


def kline_nodes_to_df(nodes: list[dict[str, Any]] | None) -> pd.DataFrame | None:
    """将 MCP data_kline 的 nodes 列表转换为标准 DataFrame。

    - volume：手 -> 股（x100）
    - last -> close（列名归一）
    - 返回 None 表示无数据（fail-open 语义）
    """
    if not nodes:
        return None
    df = pd.DataFrame(nodes)
    if "volume" in df.columns:
        df["volume"] = df["volume"].astype(float) * VOLUME_LOT_SHARES
    if "last" in df.columns and "close" not in df.columns:
        df = df.rename(columns={"last": "close"})
    # 显式单位元数据：换算结果必须可追溯，禁止在通用校验公式内猜单位
    df.attrs[VOLUME_UNIT_ATTR] = dict(VOLUME_UNIT_META)
    return df


def build_fetcher_from_kline(
    kline_by_symbol: dict[str, dict[str, Any]],
) -> Callable[[str, Any, Any], pd.DataFrame | None]:
    """由按标的整理的 data_kline 响应构造 (symbol, start, end) -> DataFrame 的 fetcher。

    ``kline_by_symbol``：{symbol: node}，node 为单日 data_kline 返回节点
    （date/open/last/high/low/volume/amount）。符号键与主源 symbol 一致
    （如 600519.SH）。不可用时返回 None（fail-open，不抛异常）。
    """

    def _fetch(symbol: str, start: Any, end: Any) -> pd.DataFrame | None:
        node = kline_by_symbol.get(symbol)
        if node is None:
            return None
        return kline_nodes_to_df([node])

    return _fetch


__all__ = [
    "VOLUME_LOT_SHARES",
    "VOLUME_UNIT_ATTR",
    "VOLUME_UNIT_META",
    "kline_nodes_to_df",
    "build_fetcher_from_kline",
]
