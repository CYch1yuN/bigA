"""Phase B: 个股行情与策略联动后端测试（全部 tmp_path fixture，不触碰真实 curated）。

数据边界核心证明：
- K 线来自本地 curated；qfq 只用 curated 真实 qfq 列（Westock 复权永不进入）
- snapshot/minute 缓存断开时本地 K 线仍可用
- 非本地/非法 symbol（含路径穿越）拒绝
- 缺失 qfq 明确降级；损坏文件 fail-open 无路径/堆栈
- research 只读（调用后产物不变）
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from app.main import create_app
from app.stocks_service import CuratedStocksService

TRADE_DATES = [
    "2026-07-01", "2026-07-02", "2026-07-03", "2026-07-06", "2026-07-07",
    "2026-07-08", "2026-07-09", "2026-07-10", "2026-07-13", "2026-07-14",
    "2026-07-15", "2026-07-16", "2026-07-17", "2026-07-20", "2026-07-21",
    "2026-07-22", "2026-07-23", "2026-07-24", "2026-07-27", "2026-07-28",
    "2026-07-29", "2026-07-30", "2026-07-31",
]


def _write_curated(root: Path, symbol: str = "600519.SH", dates=None) -> Path:
    curated = root / "data" / "curated"
    curated.mkdir(parents=True, exist_ok=True)
    dates = dates or TRADE_DATES
    df = pd.DataFrame({
        "symbol": [symbol] * len(dates),
        "trade_date": dates,
        "open_raw": [100.0 + i for i in range(len(dates))],
        "high_raw": [101.0 + i for i in range(len(dates))],
        "low_raw": [99.0 + i for i in range(len(dates))],
        "close_raw": [100.5 + i for i in range(len(dates))],
        "open_qfq": [90.0 + i for i in range(len(dates))],
        "high_qfq": [91.0 + i for i in range(len(dates))],
        "low_qfq": [89.0 + i for i in range(len(dates))],
        "close_qfq": [90.5 + i for i in range(len(dates))],
        "volume": [1000 * (i + 1) for i in range(len(dates))],
        "amount": [100000.0 * (i + 1) for i in range(len(dates))],
    })
    path = curated / f"daily_quotes_{symbol}_2026-07-01_2026-07-31.parquet"
    df.to_parquet(path)
    return path


def _write_westock_cache(root: Path, capability: str, symbol: str,
                         *, fetched_at: str | None = None, corrupt: bool = False) -> Path:
    path = root / "state" / "dashboard" / "westock" / capability / f"{symbol}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if corrupt:
        path.write_text("{ 损坏", encoding="utf-8")
        return path
    payload = {
        "schema_version": 1,
        "capability": capability,
        "tool": "data_quote" if capability == "quote" else "data_minute",
        "scope": symbol,
        "source": "westock-mcp",
        "transport": "cache_export",
        "as_of": "2026-07-31",
        "fetched_at": fetched_at or datetime.now(timezone.utc).isoformat(),
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "data": {"price": 1350.6, "change_percent": 0.03} if capability == "quote" else {"minutes": []},
        "warnings": [],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_artifacts(root: Path, symbol: str = "600519.SH") -> None:
    rep = root / "reports" / "phase-4" / "daily" / "2026-07-31"
    rep.mkdir(parents=True, exist_ok=True)
    (rep / "signals.json").write_text(json.dumps({
        "as_of_date": "2026-07-31", "run_id": "r1", "simulated": True, "live_trading": False,
        "signals": [{"signal_date": "2026-07-31", "symbol": symbol, "side": "BUY",
                     "quantity": 100, "reason": "测试信号"}],
    }), encoding="utf-8")
    (rep / "simulated-orders.json").write_text(json.dumps({
        "as_of_date": "2026-07-31", "run_id": "r1", "simulated": True,
        "orders": [{"account_id": "paper-steady", "signal_date": "2026-07-31", "fill_date": None,
                    "symbol": symbol, "side": "BUY", "quantity": 100, "status": "PENDING",
                    "fill_price": None, "reason": "测试订单"}],
    }), encoding="utf-8")
    acc = root / "state" / "automation" / "accounts"
    acc.mkdir(parents=True, exist_ok=True)
    (acc / "paper-steady.json").write_text(json.dumps({
        "account_id": "paper-steady", "positions": [
            {"symbol": symbol, "total_quantity": 100, "sellable_quantity": 100,
             "avg_raw_cost": "120.00"},
        ],
    }), encoding="utf-8")
    state = root / "state" / "automation"
    state.mkdir(parents=True, exist_ok=True)
    (state / "latest-daily.json").write_text(json.dumps({
        "as_of_date": "2026-07-31", "state": "SUCCESS", "exit_code": 0,
    }), encoding="utf-8")


def _make_app(root: Path, config_factory):
    return create_app(config_factory(project_root=root), enable_static=False)


@pytest.fixture()
def stocks_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _write_curated(root)
    return root


@pytest.fixture()
def auth_client(stocks_root, config_factory):
    from fastapi.testclient import TestClient

    app = _make_app(stocks_root, config_factory)
    with TestClient(app, base_url="https://127.0.0.1") as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "secret123"})
        yield client


# ---------------------------------------------------------------------- #
# 列表
# ---------------------------------------------------------------------- #

def test_stocks_list_search_pagination(auth_client):
    r = auth_client.get("/api/stocks?limit=10&offset=0")
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "local-curated"
    assert body["is_realtime"] is False
    assert body["data"]["total"] == 1
    assert body["data"]["items"][0]["symbol"] == "600519.SH"
    assert body["data"]["items"][0]["latest_trade_date"] == "2026-07-31"

    search = auth_client.get("/api/stocks?query=000001")
    assert search.json()["data"]["total"] == 0
    # 分页越界
    empty = auth_client.get("/api/stocks?limit=10&offset=50")
    assert empty.json()["data"]["items"] == []
    # 参数校验
    assert auth_client.get("/api/stocks?limit=0").status_code == 400
    assert auth_client.get("/api/stocks?limit=101").status_code == 400
    assert auth_client.get("/api/stocks?offset=-1").status_code == 400


def test_stocks_list_requires_auth(stocks_root, config_factory):
    from fastapi.testclient import TestClient

    app = _make_app(stocks_root, config_factory)
    with TestClient(app, base_url="https://127.0.0.1") as client:
        assert client.get("/api/stocks").status_code == 401


# ---------------------------------------------------------------------- #
# history：raw/qfq / 区间 / 边界
# ---------------------------------------------------------------------- #

def test_history_raw_values_match_parquet(auth_client, stocks_root):
    r = auth_client.get("/api/stocks/600519.SH/history?adjustment=raw&range=all")
    assert r.status_code == 200
    rows = r.json()["data"]["rows"]
    assert len(rows) == len(TRADE_DATES)
    # 升序
    dates = [row["date"] for row in rows]
    assert dates == sorted(dates)
    # 抽样与 parquet 一致
    df = pd.read_parquet(next(stocks_root.joinpath("data/curated").glob("*.parquet")))
    last = rows[-1]
    assert last["date"] == "2026-07-31"
    assert last["close"] == f"{float(df['close_raw'].iloc[-1]):.2f}"
    assert last["open"] == f"{float(df['open_raw'].iloc[-1]):.2f}"
    assert last["volume"] == int(df["volume"].iloc[-1])


def test_history_qfq_uses_curated_qfq_columns(auth_client):
    r = auth_client.get("/api/stocks/600519.SH/history?adjustment=qfq&range=all")
    assert r.status_code == 200
    body = r.json()
    assert body["availability"]["qfq"] is True
    last = body["data"]["rows"][-1]
    assert last["close"] == "112.50"  # close_qfq = 90.5 + 22
    assert last["open"] == "112.00"  # open_qfq = 90.0 + 22


def test_history_range_and_end_filter(auth_client):
    r1 = auth_client.get("/api/stocks/600519.SH/history?adjustment=raw&range=1m")
    assert len(r1.json()["data"]["rows"]) == 21  # 21 个交易日（1 个月）
    r3 = auth_client.get("/api/stocks/600519.SH/history?adjustment=raw&range=3m")
    assert len(r3.json()["data"]["rows"]) == len(TRADE_DATES)  # 数据不足 3 个月，返回全部
    rend = auth_client.get("/api/stocks/600519.SH/history?adjustment=raw&range=all&end=2026-07-15")
    rows = rend.json()["data"]["rows"]
    assert rows[-1]["date"] == "2026-07-15"
    assert rows[0]["date"] == "2026-07-01"


def test_history_rejects_path_traversal(auth_client):
    for bad in ("600519.SH/../secret", "../secret", "600519", "600519.SHX", "abc"):
        assert auth_client.get(f"/api/stocks/{bad}/history").status_code in (400, 404)


def test_history_rejects_bad_params(auth_client):
    assert auth_client.get("/api/stocks/600519.SH/history?adjustment=bad").status_code == 400
    assert auth_client.get("/api/stocks/600519.SH/history?range=9m").status_code == 400
    assert auth_client.get("/api/stocks/600519.SH/history?end=not-a-date").status_code == 400


def test_history_qfq_missing_degrades(tmp_path, config_factory):
    root = tmp_path / "repo"
    _write_curated(root)
    # 移除 qfq 列
    path = next(root.joinpath("data/curated").glob("*.parquet"))
    df = pd.read_parquet(path)
    df.drop(columns=["open_qfq", "high_qfq", "low_qfq", "close_qfq"], inplace=True)
    df.to_parquet(path)

    app = _make_app(root, config_factory)
    from fastapi.testclient import TestClient
    with TestClient(app, base_url="https://127.0.0.1") as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "secret123"})
        r = client.get("/api/stocks/600519.SH/history?adjustment=qfq&range=all")
    body = r.json()
    assert r.status_code == 200
    assert body["availability"]["qfq"] is False
    assert body["data"]["rows"] == []
    assert "qfq" in body["message"].lower()
    # raw 仍可用
    r_raw = auth_client_get(app, "/api/stocks/600519.SH/history?adjustment=raw&range=1m")
    assert r_raw.status_code == 200
    assert len(r_raw.json()["data"]["rows"]) == 21


def auth_client_get(app, url: str):
    from fastapi.testclient import TestClient
    with TestClient(app, base_url="https://127.0.0.1") as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "secret123"})
        return client.get(url)


def test_history_corrupt_file_fails_open(tmp_path, config_factory):
    root = tmp_path / "repo"
    curated = root / "data" / "curated"
    curated.mkdir(parents=True)
    (curated / "daily_quotes_600519.SH_2026-01-01_2026-07-31.parquet").write_bytes(
        b"not a parquet at all")
    app = _make_app(root, config_factory)
    r = auth_client_get(app, "/api/stocks/600519.SH/history?adjustment=raw&range=all")
    body = r.json()
    assert r.status_code == 200
    assert body["cache_status"] == "unavailable"
    assert body["data"]["rows"] == []
    assert str(root) not in r.text  # 不泄露服务端绝对路径
    assert "C:\\" not in body.get("message", "")  # message 不含盘符路径


def test_history_unknown_symbol_degrades(auth_client):
    r = auth_client.get("/api/stocks/999999.SZ/history?adjustment=raw&range=all")
    body = r.json()
    assert r.status_code == 200
    assert body["availability"]["curated"] is False
    assert body["data"]["rows"] == []


# ---------------------------------------------------------------------- #
# snapshot / minute
# ---------------------------------------------------------------------- #

def test_snapshot_local_works_without_westock(auth_client):
    r = auth_client.get("/api/stocks/600519.SH/snapshot")
    body = r.json()
    assert r.status_code == 200
    assert body["availability"]["curated"] is True
    assert body["availability"]["westock_quote"] is False
    assert body["data"]["local"]["date"] == "2026-07-31"
    assert body["data"]["local"]["close"] == "122.50"  # close_raw = 100.5 + 22
    assert body["warnings"]  # 提示无 westock 缓存


def test_snapshot_with_westock_quote_cache(auth_client, stocks_root):
    _write_westock_cache(stocks_root, "quote", "600519.SH")
    r = auth_client.get("/api/stocks/600519.SH/snapshot")
    body = r.json()
    assert body["availability"]["westock_quote"] is True
    quote = body["data"]["westock_quote"]
    assert quote["price"] == 1350.6
    assert quote["change_percent"] == 0.03
    assert quote["status"] == "fresh"  # 刚写入 → fresh
    assert body["transport"] == "local-curated+westock-cache"


def test_snapshot_expired_quote_is_stale_not_fresh(auth_client, stocks_root):
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    _write_westock_cache(stocks_root, "quote", "600519.SH", fetched_at=old)
    body = auth_client.get("/api/stocks/600519.SH/snapshot").json()
    assert body["availability"]["westock_quote"] is True  # stale 仍可展示
    assert body["data"]["westock_quote"]["status"] == "stale"  # 但明确 stale
    assert body["is_realtime"] is False


def test_snapshot_ignores_corrupt_or_future_westock(auth_client, stocks_root):
    _write_westock_cache(stocks_root, "quote", "600519.SH", corrupt=True)
    r = auth_client.get("/api/stocks/600519.SH/snapshot")
    assert r.json()["availability"]["westock_quote"] is False
    assert r.json()["data"]["local"] is not None  # 本地不受影响

    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    _write_westock_cache(stocks_root, "quote", "600519.SH", fetched_at=future)
    r2 = auth_client.get("/api/stocks/600519.SH/snapshot")
    assert r2.json()["availability"]["westock_quote"] is False  # future 视为异常缓存


def test_minute_cache_present_and_missing(auth_client, stocks_root):
    missing = auth_client.get("/api/stocks/600519.SH/minute")
    assert missing.json()["cache_status"] == "unavailable"
    assert missing.json()["is_realtime"] is False

    _write_westock_cache(stocks_root, "minute", "600519.SH")
    present = auth_client.get("/api/stocks/600519.SH/minute")
    body = present.json()
    assert body["cache_status"] == "fresh"
    assert body["availability"]["westock_minute"] is True
    assert body["is_realtime"] is False
    assert any("非实时" in w for w in body["warnings"])
    assert body["data"]["rows"] == []  # 空分钟列表正常标准化


def test_minute_expired_is_stale_not_fresh(auth_client, stocks_root):
    old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    _write_westock_cache(stocks_root, "minute", "600519.SH", fetched_at=old)
    body = auth_client.get("/api/stocks/600519.SH/minute").json()
    assert body["cache_status"] == "stale"  # stale 可展示但明确标记
    assert any("过期" in w for w in body["warnings"])


# ---------------------------------------------------------------------- #
# research
# ---------------------------------------------------------------------- #

def test_research_reads_artifacts_and_is_readonly(tmp_path, config_factory):
    root = tmp_path / "repo"
    _write_curated(root)
    _write_artifacts(root)
    before_signals = (root / "reports" / "phase-4" / "daily" / "2026-07-31" / "signals.json").read_bytes()
    before_orders = (root / "reports" / "phase-4" / "daily" / "2026-07-31" / "simulated-orders.json").read_bytes()

    app = _make_app(root, config_factory)
    r = auth_client_get(app, "/api/stocks/600519.SH/research")
    body = r.json()
    assert body["data"]["as_of"] == "2026-07-31"
    assert len(body["data"]["signals"]) == 1
    assert body["data"]["signals"][0]["side"] == "BUY"
    assert len(body["data"]["orders"]) == 1
    assert body["data"]["orders"][0]["status"] == "PENDING"
    assert len(body["data"]["positions"]) == 1
    assert body["data"]["positions"][0]["total_quantity"] == 100

    # 只读：产物未被修改
    after_signals = (root / "reports" / "phase-4" / "daily" / "2026-07-31" / "signals.json").read_bytes()
    after_orders = (root / "reports" / "phase-4" / "daily" / "2026-07-31" / "simulated-orders.json").read_bytes()
    assert after_signals == before_signals
    assert after_orders == before_orders


def test_research_other_symbol_empty_but_200(auth_client, stocks_root):
    _write_artifacts(stocks_root, "600519.SH")
    r = auth_client.get("/api/stocks/000001.SZ/research")
    body = r.json()
    assert r.status_code == 200
    assert body["data"]["signals"] == []
    assert body["data"]["orders"] == []


def test_research_service_direct_unit(tmp_path):
    """service 级：tmp_path 完全隔离 project_root/reports/state，无真实依赖。"""
    root = tmp_path / "repo"
    svc = CuratedStocksService(root)
    r = svc.research("600519.SH")
    assert r["data"]["signals"] == []
    assert r["data"]["orders"] == []
    assert r["data"]["positions"] == []
    assert r["availability"]["artifacts"] is False
    assert r["warnings"]  # 提示无可用 daily 产物


def test_positions_load_without_daily_artifacts(tmp_path):
    """持仓始终从唯一模拟账本只读加载，不依赖 daily 信号/订单产物存在。"""
    root = tmp_path / "repo"
    acc = root / "state" / "automation" / "accounts"
    acc.mkdir(parents=True)
    (acc / "paper-steady.json").write_text(json.dumps({
        "account_id": "paper-steady",
        "positions": [{"symbol": "600519.SH", "total_quantity": 200,
                       "sellable_quantity": 100, "avg_raw_cost": "1300.00"}],
    }), encoding="utf-8")
    svc = CuratedStocksService(root)
    r = svc.research("600519.SH")
    assert r["data"]["positions"] == [{
        "account_id": "paper-steady", "symbol": "600519.SH",
        "total_quantity": 200, "sellable_quantity": 100, "avg_raw_cost": "1300.00",
    }]
    assert r["availability"]["artifacts"] is False  # 无 daily 产物但持仓仍返回


def test_failed_daily_not_usable_research_date(tmp_path):
    """latest-daily 非 SUCCESS/exit 0 → 不作为可用 research 日期。"""
    root = tmp_path / "repo"
    state = root / "state" / "automation"
    state.mkdir(parents=True)
    (state / "latest-daily.json").write_text(json.dumps({
        "as_of_date": "2026-07-31", "state": "FAILED", "exit_code": 1,
    }), encoding="utf-8")
    svc = CuratedStocksService(root)
    assert svc._usable_daily_as_of() is None
    r = svc.research("600519.SH")
    assert r["data"]["as_of"] is None
    assert r["availability"]["artifacts"] is False


def test_latest_daily_needs_existing_report_dir(tmp_path):
    """latest-daily SUCCESS 但产物目录缺失 → 不作为可用日期。"""
    root = tmp_path / "repo"
    state = root / "state" / "automation"
    state.mkdir(parents=True)
    (state / "latest-daily.json").write_text(json.dumps({
        "as_of_date": "2026-07-31", "state": "SUCCESS", "exit_code": 0,
    }), encoding="utf-8")
    svc = CuratedStocksService(root)
    assert svc._usable_daily_as_of() is None  # reports/phase-4/daily/2026-07-31 不存在


def test_signals_orders_fail_open_on_bad_shape(tmp_path):
    """signals/orders 顶层非对象或字段非数组 → fail-open（空列表，不抛错）。"""
    root = tmp_path / "repo"
    rep = root / "reports" / "phase-4" / "daily" / "2026-07-31"
    rep.mkdir(parents=True)
    (rep / "signals.json").write_text("[1,2,3]", encoding="utf-8")  # 顶层数组
    (rep / "simulated-orders.json").write_text(json.dumps({"orders": "not-a-list"}), encoding="utf-8")
    state = root / "state" / "automation"
    state.mkdir(parents=True)
    (state / "latest-daily.json").write_text(json.dumps({
        "as_of_date": "2026-07-31", "state": "SUCCESS", "exit_code": 0,
    }), encoding="utf-8")
    svc = CuratedStocksService(root)
    r = svc.research("600519.SH")
    assert r["data"]["signals"] == []
    assert r["data"]["orders"] == []
    assert r["availability"]["artifacts"] is True
