# -*- coding: utf-8 -*-
"""westock_adapter 单测：volume 手->股换算、列名归一、fail-open、fetcher 构造。

背景：2026-08-03 实测确认 westock data_kline 的 volume 单位为「手」(100股)，
若不换算会与本地 curated(股) 产生约 99% 虚假偏差——此测试固化该换算。
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from ashare_quant.validators.westock_adapter import (
    VOLUME_LOT_SHARES,
    build_fetcher_from_kline,
    kline_nodes_to_df,
)

NODE = {
    "date": "2026-07-31",
    "open": 1330.03,
    "last": 1350.6,
    "high": 1355.72,
    "low": 1325.77,
    "volume": 55128,      # 手
    "amount": 7373460000,
}


class TestKlineNodesToDf:
    def test_volume_lot_to_shares(self) -> None:
        """volume 手 -> 股（x100），不得保留原始手数值。"""
        df = kline_nodes_to_df([NODE])
        assert df is not None
        assert df.loc[0, "volume"] == pytest.approx(55128 * VOLUME_LOT_SHARES)

    def test_last_renamed_to_close(self) -> None:
        df = kline_nodes_to_df([NODE])
        assert "close" in df.columns
        assert "last" not in df.columns
        assert df.loc[0, "close"] == 1350.6

    def test_empty_nodes_returns_none(self) -> None:
        assert kline_nodes_to_df(None) is None
        assert kline_nodes_to_df([]) is None

    def test_no_99pct_fake_volume_deviation(self) -> None:
        """核心回归：换算后与 curated 的 volume 偏差应远小于 99%。"""
        df = kline_nodes_to_df([NODE])
        curated_shares = 5512752.0
        dev = abs(df.loc[0, "volume"] - curated_shares) / curated_shares
        assert dev < 0.01  # 仅取整差异，绝非 99%


class TestBuildFetcher:
    def test_fetcher_returns_converted_df(self) -> None:
        f = build_fetcher_from_kline({"600519.SH": NODE})
        out = f("600519.SH", date(2026, 7, 31), date(2026, 7, 31))
        assert out is not None
        assert out.loc[0, "volume"] == 55128 * VOLUME_LOT_SHARES
        assert out.loc[0, "close"] == 1350.6

    def test_fetcher_missing_symbol_returns_none(self) -> None:
        f = build_fetcher_from_kline({"600519.SH": NODE})
        assert f("000001.SZ", date(2026, 7, 31), date(2026, 7, 31)) is None

    def test_fetcher_empty_map(self) -> None:
        f = build_fetcher_from_kline({})
        assert f("600519.SH", date(2026, 7, 31), date(2026, 7, 31)) is None
