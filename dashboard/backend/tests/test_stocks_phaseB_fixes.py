# -*- coding: utf-8 -*-
"""Phase B 修正追加测试：非法 schema / 异常数值 / 分时标准化。"""
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from app.main import create_app

TRADE_DATES = [
    "2026-07-01", "2026-07-02", "2026-07-03", "2026-07-06", "2026-07-07",
    "2026-07-08", "2026-07-09", "2026-07-10", "2026-07-13", "2026-07-14",
    "2026-07-15", "2026-07-16", "2026-07-17", "2026-07-20", "2026-07-21",
    "2026-07-22", "2026-07-23", "2026-07-24", "2026-07-27", "2026-07-28",
    "2026-07-29", "2026-07-30", "2026-07-31",
]


def _write_curated(root: Path) -> None:
    curated = root / "data" / "curated"
    curated.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame({
        "symbol": ["600519.SH"] * len(TRADE_DATES),
        "trade_date": TRADE_DATES,
        "open_raw": [100.0 + i for i in range(len(TRADE_DATES))],
        "high_raw": [101.0 + i for i in range(len(TRADE_DATES))],
        "low_raw": [99.0 + i for i in range(len(TRADE_DATES))],
        "close_raw": [100.5 + i for i in range(len(TRADE_DATES))],
        "open_qfq": [90.0 + i for i in range(len(TRADE_DATES))],
        "high_qfq": [91.0 + i for i in range(len(TRADE_DATES))],
        "low_qfq": [89.0 + i for i in range(len(TRADE_DATES))],
        "close_qfq": [90.5 + i for i in range(len(TRADE_DATES))],
        "volume": [1000 * (i + 1) for i in range(len(TRADE_DATES))],
        "amount": [100000.0 * (i + 1) for i in range(len(TRADE_DATES))],
    })
    df.to_parquet(curated / "daily_quotes_600519.SH_2026-07-01_2026-07-31.parquet")


def _make_app(root: Path, config_factory):
    return create_app(config_factory(project_root=root), enable_static=False)


def _auth_get(app, url: str):
    from fastapi.testclient import TestClient

    with TestClient(app, base_url="https://127.0.0.1") as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "secret123"})
        return client.get(url)


def _write_raw_westock(root: Path, capability: str, symbol: str, payload: dict) -> None:
    path = root / "state" / "dashboard" / "westock" / capability / f"{symbol}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _base_payload(capability: str, symbol: str, data: dict) -> dict:
    return {
        "schema_version": 1,
        "capability": capability,
        "tool": "data_minute" if capability == "minute" else "data_quote",
        "scope": symbol,
        "source": "westock-mcp",
        "transport": "cache_export",
        "as_of": "2026-07-31",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "data": data,
        "warnings": [],
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update({"tool": "data_quote_hacked"}),
        lambda p: p.update({"source": ""}),
        lambda p: p.update({"cached_at": "not-a-time"}),
        lambda p: p.pop("data"),
        lambda p: p.update({"warnings": "not-a-list"}),
        lambda p: p.update({"transport": "direct_mcp"}),
    ],
)
def test_westock_invalid_schema_unavailable(tmp_path, config_factory, mutate):
    """非法 schema（tool/source/cached_at/data/warnings/transport）→ unavailable。"""
    root = tmp_path / "repo"

    _write_curated(root)
    payload = _base_payload("minute", "600519.SH", {"minutes": []})
    mutate(payload)
    _write_raw_westock(root, "minute", "600519.SH", payload)
    app = _make_app(root, config_factory)
    r = _auth_get(app, "/api/stocks/600519.SH/minute")
    body = r.json()
    assert body["cache_status"] == "unavailable"
    assert body["availability"]["westock_minute"] is False
    assert body["is_realtime"] is False


def test_history_nan_infinity_values_do_not_500(tmp_path, config_factory):
    """OHLC 非数值/NaN/Infinity → 行结构化跳过，不 500。"""

    root = tmp_path / "repo"
    curated = root / "data" / "curated"
    curated.mkdir(parents=True)
    df = pd.DataFrame({
        "symbol": ["600519.SH"] * 3,
        "trade_date": ["2026-07-29", "2026-07-30", "2026-07-31"],
        "open_raw": ["100.0", "NaN", "bad"],  # 全字符串列：合法 / NaN / 非数值
        "high_raw": [101.0, 102.0, 103.0],
        "low_raw": [99.0, 100.0, 101.0],
        "close_raw": [100.5, 101.5, 102.5],
        "open_qfq": [100.0, 101.0, 102.0],
        "high_qfq": [101.0, 102.0, 103.0],
        "low_qfq": [99.0, 100.0, 101.0],
        "close_qfq": [100.5, 101.5, 102.5],
        "volume": [1000, 2000, 3000],
        "amount": [1e6, 2e6, 3e6],
    })
    df.to_parquet(curated / "daily_quotes_600519.SH_2026-07-01_2026-07-31.parquet")
    app = _make_app(root, config_factory)
    r = _auth_get(app, "/api/stocks/600519.SH/history?adjustment=raw&range=all")
    body = r.json()
    assert r.status_code == 200
    rows = body["data"]["rows"]
    assert len(rows) == 1  # 仅 07-29 行通过（07-30 NaN、07-31 非数值被跳过）
    assert rows[0]["date"] == "2026-07-29"
    assert any("跳过" in w for w in body["warnings"])


def test_normalize_minute_accepts_controlled_fields():
    from app.stocks_service import normalize_minute

    rows, reason = normalize_minute([
        {"time": "09:30", "price": 100.0, "volume": 100},
        {"date_time": "09:31", "close": 100.5},
    ])
    assert reason == "ok"
    assert rows == [
        {"time": "09:30", "price": 100.0, "volume": 100.0},
        {"time": "09:31", "price": 100.5, "volume": None},
    ]
    rows2, _ = normalize_minute({"minutes": [{"time": "09:32", "price": 101.0}]})
    assert rows2 == [{"time": "09:32", "price": 101.0, "volume": None}]


@pytest.mark.parametrize(
    "data",
    [
        "not-a-list",
        {"unknown": 1},
        [{"foo": 1}],
        [{"time": "09:30", "price": "bad"}],
        [{"time": "09:30", "price": 1.0, "volume": "x"}],
        [{"time": "09:30", "price": float("inf")}],
    ],
)
def test_normalize_minute_unknown_schema_degrades(data):
    from app.stocks_service import normalize_minute

    rows, reason = normalize_minute(data)
    assert rows is None
    assert reason
