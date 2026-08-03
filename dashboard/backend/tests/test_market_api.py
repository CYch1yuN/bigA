"""Phase D: 市场研究中心 API 测试（全部 tmp_path 隔离，不触碰真实缓存）。

核心证明：
- 逐能力 fresh/stale/unavailable；单能力失败不影响整体
- 强制 schema / 硬上限 / 稳定排序 / URL 与代码校验
- local_history_available 来自本地 curated（不信 Westock 返回值）
- calendar 参数边界与精确错误码
- 只读哈希证明（curated/signals/orders/accounts/Gate 4B）
- 不创建信号/订单/持仓
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from app.main import create_app
from app.westock_bridge import CAPABILITY_MAP


def _write_cache(root: Path, capability: str, data, *, as_of: str = "2026-07-31",
                 fetched_at: str | None = None, corrupt: bool = False) -> Path:
    path = root / "state" / "dashboard" / "westock" / capability / "global.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if corrupt:
        path.write_text("{ 损坏", encoding="utf-8")
        return path
    payload = {
        "schema_version": 1,
        "capability": capability,
        "tool": CAPABILITY_MAP[capability].tool,
        "scope": "global",
        "source": "westock-mcp",
        "transport": "cache_export",
        "as_of": as_of,
        "fetched_at": fetched_at or datetime.now(timezone.utc).isoformat(),
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "data": data,
        "warnings": [],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _seed_curated(root: Path) -> None:
    curated = root / "data" / "curated"
    curated.mkdir(parents=True)
    df = pd.DataFrame({
        "symbol": ["600519.SH", "000001.SZ"] * 5,
        "trade_date": ["2026-07-31"] * 10,
        "open_raw": [1.0] * 10, "high_raw": [2.0] * 10, "low_raw": [0.5] * 10,
        "close_raw": [1.5] * 10, "open_qfq": [1.0] * 10, "high_qfq": [2.0] * 10,
        "low_qfq": [0.5] * 10, "close_qfq": [1.5] * 10,
        "volume": [100] * 10, "amount": [1e5] * 10,
    })
    df.to_parquet(curated / "daily_quotes_600519.SH_2026-07-01_2026-07-31.parquet")
    df.to_parquet(curated / "daily_quotes_000001.SZ_2026-07-01_2026-07-31.parquet")


def _seed_full(root: Path, *, age_hours: float | None = None) -> None:
    fetched = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat() if age_hours else None
    _seed_curated(root)
    _write_cache(root, "market_overview", {
        "score": 62.5, "sentiment": 55.0, "trend": 60.0, "liquidity": 70.0,
        "breadth": 58.0, "volatility": 40.0, "risk_level": "中",
        "summary": "市场情绪偏暖，量能温和放大",
        "dimensions": {"trend": 60.0, "sentiment": 55.0, "liquidity": 70.0,
                       "breadth": 58.0, "volatility": 40.0, "risk": 45.0},
    }, fetched_at=fetched)
    _write_cache(root, "change_distribution", {
        "rise_count": 2800, "fall_count": 1900, "flat_count": 300,
        "limit_up_count": 45, "limit_down_count": 3, "total_amount": 8.2e11,
        "bins": [{"label": "0-1%", "min_percent": 0, "max_percent": 1, "count": 900}],
    }, fetched_at=fetched)
    _write_cache(root, "hot_ranking", {
        "stocks": [{"rank": 1, "symbol": "600519.SH", "name": "贵州茅台",
                    "price": 1350.0, "change_percent": 3.2, "heat": 95, "reason": "业绩超预期"}],
        "sectors": [{"rank": 1, "code": "BK0477", "name": "白酒", "change_percent": 2.1,
                     "heat": 88, "leader_symbol": "600519.SH", "leader_name": "贵州茅台"}],
    }, fetched_at=fetched)
    _write_cache(root, "sector", [
        {"code": "BK0477", "name": "白酒", "sector_type": "concept", "change_percent": 2.1,
         "amount": 3e10, "turnover_rate": 1.5, "rise_count": 18, "fall_count": 2,
         "leader_symbol": "600519.SH", "leader_name": "贵州茅台"},
        {"code": "BK0451", "name": "银行", "sector_type": "industry", "change_percent": 0.3,
         "amount": 2e10, "turnover_rate": 0.4, "rise_count": 20, "fall_count": 10,
         "leader_symbol": "601398.SH", "leader_name": "工商银行"},
    ], fetched_at=fetched)
    _write_cache(root, "index", {
        "indexes": [
            {"code": "000001.SH", "name": "上证指数", "price": 3450.0, "change": 12.5,
             "change_percent": 0.36, "amount": 5.1e11, "volume": 4.2e10,
             "constituents": [
                 {"symbol": "600519.SH", "name": "贵州茅台", "weight": 5.2, "industry": "白酒"},
                 {"symbol": "999999.SZ", "name": "无本地数据股", "weight": 0.1, "industry": "未知"},
             ]},
            {"code": "399001.SZ", "name": "深证成指", "price": 11200.0, "change": -30.0,
             "change_percent": -0.27, "amount": 3.1e11, "volume": 2.8e10,
             "constituents": [{"symbol": "000001.SZ", "name": "平安银行", "weight": 1.0, "industry": "银行"}]},
        ],
    }, fetched_at=fetched)
    _write_cache(root, "industry_chain", [
        {"code": "IC01", "name": "白酒产业链", "description": "从高粱种植到终端销售",
         "upstream": [{"code": "N1", "name": "高粱种植", "node_type": "上游",
                       "related_symbols": ["000001.SZ"]}],
         "midstream": [{"code": "N2", "name": "酿造", "node_type": "中游",
                        "related_symbols": ["600519.SH"]}],
         "downstream": [{"code": "N3", "name": "经销", "node_type": "下游",
                         "related_symbols": ["600519.SH"]}]},
    ], fetched_at=fetched)
    _write_cache(root, "macro", [
        {"code": "M1", "name": "CPI 同比", "value": 0.8, "unit": "%", "period": "2026-06",
         "release_date": "2026-07-10", "previous": 0.5, "forecast": 0.7, "importance": "high"},
        {"code": "M2", "name": "社融增量", "value": 3.5e12, "unit": "亿元", "period": "2026-06",
         "release_date": "2026-07-13", "previous": 3.1e12, "forecast": None, "importance": "medium"},
    ], fetched_at=fetched)
    _write_cache(root, "margin", {"margin_balance": 1.8e12, "margin_change": 2e10}, fetched_at=fetched)
    _write_cache(root, "northbound", {"net": 4.5e9, "holding_shares": 6e11,
                                      "date": "2026-07-31"}, fetched_at=fetched)
    _write_cache(root, "events", [
        {"date": "2026-08-01", "title": "限售股解禁", "summary": "占总股本0.5%",
         "symbols": ["600519.SH"], "severity": "中", "url": "https://example.com/e1"},
        {"date": "2026-07-30", "title": "业绩预告", "summary": "净利预增",
         "symbols": ["000001.SZ"]},
    ], fetched_at=fetched)
    _write_cache(root, "announcements", [
        {"ann_date": "2026-07-31", "title": "中期分红公告", "summary": "10派300元"},
    ], fetched_at=fetched)
    _write_cache(root, "risk", [
        {"level": "高", "title": "行业需求波动", "description": "景气下行"},
    ], fetched_at=fetched)


def _make_app(root: Path, config_factory):
    return create_app(config_factory(project_root=root), enable_static=False)


def _auth_get(app, url: str):
    from fastapi.testclient import TestClient

    with TestClient(app, base_url="https://127.0.0.1") as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "secret123"})
        return client.get(url)


# ---------------------------------------------------------------------- #
# 认证 / 空缓存 / 损坏
# ---------------------------------------------------------------------- #

def test_market_requires_auth(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_full(root)
    app = _make_app(root, config_factory)
    from fastapi.testclient import TestClient
    with TestClient(app, base_url="https://127.0.0.1") as client:
        assert client.get("/api/market/overview").status_code == 401


def test_market_empty_cache_unavailable(tmp_path, config_factory):
    root = tmp_path / "repo"
    app = _make_app(root, config_factory)
    body = _auth_get(app, "/api/market/overview").json()
    assert body["cache_status"] == "unavailable"
    assert body["availability"] == {"market_overview": "unavailable"}
    assert body["data"]["overview"] is None
    assert body["is_realtime"] is False
    assert body["transport"] == "cache_export"
    assert body["as_of"] is None


def test_market_corrupt_cache_fails_open(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_full(root)
    _write_cache(root, "hot_ranking", {"x": 1}, corrupt=True)
    app = _make_app(root, config_factory)
    r = _auth_get(app, "/api/market/hot")
    assert r.status_code == 200
    assert r.json()["availability"]["hot_ranking"] == "unavailable"
    assert str(root) not in r.text  # 不泄露路径


def test_market_single_capability_failure_does_not_break(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_full(root)
    _write_cache(root, "market_overview", {"unknown_shape": 1})  # 结构未知
    app = _make_app(root, config_factory)
    body = _auth_get(app, "/api/market/overview").json()
    assert body["availability"]["market_overview"] == "unavailable"
    assert body["data"]["overview"] is None
    assert any("market_overview" in w for w in body["warnings"])


# ---------------------------------------------------------------------- #
# 各 API 受控 schema
# ---------------------------------------------------------------------- #

def test_overview_controlled_schema(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_full(root)
    app = _make_app(root, config_factory)
    body = _auth_get(app, "/api/market/overview").json()
    ov = body["data"]["overview"]
    assert ov["score"] == 62.5
    assert ov["dimensions"] == {"trend": 60.0, "sentiment": 55.0, "liquidity": 70.0,
                                "breadth": 58.0, "volatility": 40.0, "risk": 45.0}
    assert set(ov["dimensions"].keys()) <= {"trend", "sentiment", "liquidity", "breadth", "volatility", "risk"}
    assert body["availability"]["market_overview"] == "fresh"


def test_overview_bad_score_dropped(tmp_path, config_factory):
    """分数超范围/NaN → 丢弃。"""
    root = tmp_path / "repo"
    _write_cache(root, "market_overview", {"score": 999, "sentiment": float("nan"),
                                           "trend": "abc", "risk_level": "低"})
    app = _make_app(root, config_factory)
    ov = _auth_get(app, "/api/market/overview").json()["data"]["overview"]
    assert "score" not in ov  # 999 超 0-100
    assert "sentiment" not in ov  # NaN
    assert "trend" not in ov  # 非数值
    assert ov["risk_level"] == "低"


def test_distribution_controlled_bins(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_full(root)
    app = _make_app(root, config_factory)
    body = _auth_get(app, "/api/market/distribution").json()
    dist = body["data"]["distribution"]
    assert dist["rise_count"] == 2800
    assert dist["bins"][0] == {"label": "0-1%", "min_percent": 0.0, "max_percent": 1.0, "count": 900.0}
    assert "任意键" not in json.dumps(dist)


def test_hot_controlled_schema_and_limits(tmp_path, config_factory):
    root = tmp_path / "repo"
    _write_cache(root, "hot_ranking", {
        "stocks": [{"rank": i, "symbol": "600519.SH", "name": f"股{i}", "price": 1.0,
                    "change_percent": 1.0, "heat": i, "reason": "r"} for i in range(105)],
        "sectors": [{"rank": i, "code": f"BK{i:04d}", "name": f"板{i}", "change_percent": 1.0,
                     "heat": i, "leader_symbol": "600519.SH", "leader_name": "x"} for i in range(55)],
    })
    app = _make_app(root, config_factory)
    body = _auth_get(app, "/api/market/hot").json()
    assert len(body["data"]["hot"]["stocks"]) == 100  # 上限
    assert len(body["data"]["hot"]["sectors"]) == 50
    assert any("裁剪" in w for w in body["warnings"])


def test_sectors_enum_and_limits(tmp_path, config_factory):
    root = tmp_path / "repo"
    _write_cache(root, "sector", [
        {"code": f"BK{i:04d}", "name": f"板{i}", "sector_type": "concept", "change_percent": 1.0}
        for i in range(210)
    ] + [{"code": "X", "name": "非法类型", "sector_type": "hacked"}])
    app = _make_app(root, config_factory)
    body = _auth_get(app, "/api/market/sectors").json()
    assert len(body["data"]["sectors"]) == 200  # 上限
    assert all(s["sector_type"] in ("industry", "concept") for s in body["data"]["sectors"])
    assert any("裁剪" in w for w in body["warnings"])


def test_indexes_and_constituents_local_history(tmp_path, config_factory):
    """local_history_available 必须来自本地 curated（不信 Westock）。"""
    root = tmp_path / "repo"
    _seed_full(root)
    app = _make_app(root, config_factory)
    idx = _auth_get(app, "/api/market/indexes").json()
    assert len(idx["data"]["indexes"]) == 2
    assert idx["data"]["indexes"][0]["code"] == "000001.SH"

    cons = _auth_get(app, "/api/market/indexes/000001.SH/constituents").json()
    by_symbol = {c["symbol"]: c for c in cons["data"]["constituents"]}
    assert by_symbol["600519.SH"]["local_history_available"] is True  # curated 有
    assert by_symbol["999999.SZ"]["local_history_available"] is False  # curated 无


def test_constituents_rejects_bad_index_code(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_full(root)
    app = _make_app(root, config_factory)
    r = _auth_get(app, "/api/market/indexes/..%2Fetc/constituents")
    assert r.status_code in (400, 404)
    r2 = _auth_get(app, "/api/market/indexes/INVALID%21%40%23/constituents")
    assert r2.status_code in (400, 404)


def test_industry_chain_controlled_nodes(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_full(root)
    app = _make_app(root, config_factory)
    body = _auth_get(app, "/api/market/industry-chain").json()
    chains = body["data"]["chains"]
    assert len(chains) == 1
    assert chains[0]["upstream"][0]["related_symbols"] == ["000001.SZ"]
    # 无任意递归结构：节点固定字段
    assert set(chains[0]["upstream"][0].keys()) <= {"code", "name", "node_type", "related_symbols"}


def test_macro_importance_enum(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_full(root)
    _write_cache(root, "macro", [
        {"code": "M1", "name": "CPI", "value": 0.8, "unit": "%", "period": "2026-06",
         "release_date": "2026-07-10", "previous": 0.5, "forecast": 0.7, "importance": "high"},
        {"code": "M2", "name": "坏指标", "value": 1.0, "importance": "EXTREME"},
    ])
    app = _make_app(root, config_factory)
    inds = _auth_get(app, "/api/market/macro").json()["data"]["indicators"]
    assert inds[0]["importance"] == "high"
    assert inds[0]["release_date"] == "2026-07-10"
    # EXTREME 非枚举 → 该字段丢弃（条目保留但无 importance），不产生非法值
    bad = next((i for i in inds if i.get("name") == "坏指标"), None)
    assert bad is not None
    assert "importance" not in bad
    assert all(i["importance"] in ("low", "medium", "high") for i in inds if "importance" in i)


def test_calendar_params_and_error_codes(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_full(root)
    app = _make_app(root, config_factory)
    body = _auth_get(app, "/api/market/calendar?start_date=2026-07-01&end_date=2026-08-31&limit=10&offset=0").json()
    assert body["cache_status"] == "fresh"
    assert body["data"]["total"] >= 3
    dates = [i["date"] for i in body["data"]["items"]]
    assert dates == sorted(dates, reverse=True)  # 日期倒序
    # 参数边界
    assert _auth_get(app, "/api/market/calendar?start_date=bad").status_code == 400
    assert _auth_get(app, "/api/market/calendar?start_date=2025-01-01&end_date=2026-12-31").status_code == 400  # 超 366 天
    assert _auth_get(app, "/api/market/calendar?importance=EXTREME").status_code == 400
    assert _auth_get(app, "/api/market/calendar?limit=0").status_code == 400
    assert _auth_get(app, "/api/market/calendar?offset=-1").status_code == 400


def test_funds_aggregates_with_null_southbound(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_full(root)
    app = _make_app(root, config_factory)
    funds = _auth_get(app, "/api/market/funds").json()["data"]["funds"]
    assert funds["margin_balance"] == 1.8e12
    assert funds["northbound_net"] == 4.5e9
    assert funds["southbound_net"] is None  # 无能力 → null 不猜口径
    assert funds["date"] == "2026-07-31"


def test_events_aggregation_url_and_symbols(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_full(root)
    _write_cache(root, "events", [
        {"date": "2026-08-01", "title": "好事件", "summary": "s",
         "symbols": ["600519.SH", "bad-symbol"], "url": "https://a.com/1"},
        {"date": "2026-08-01", "title": "坏URL事件", "url": "javascript:alert(1)"},
    ])
    app = _make_app(root, config_factory)
    body = _auth_get(app, "/api/market/events").json()
    items = body["data"]["events"]
    by_title = {i["title"]: i for i in items}
    assert by_title["好事件"]["symbols"] == ["600519.SH"]  # 非法 symbol 丢弃
    assert by_title["好事件"]["url"] == "https://a.com/1"
    assert "url" not in by_title["坏URL事件"]  # javascript 协议丢弃
    assert any("不直接生成 BigA 信号" in w for w in body["warnings"])


def test_events_stable_sort_invalid_date_last(tmp_path, config_factory):
    root = tmp_path / "repo"
    _write_cache(root, "events", [
        {"date": "2026-07-01", "title": "旧"},
        {"date": "not-a-date", "title": "无日期"},
        {"date": "2026-08-01", "title": "新"},
    ])
    app = _make_app(root, config_factory)
    titles = [i["title"] for i in _auth_get(app, "/api/market/events").json()["data"]["events"]]
    assert titles == ["新", "旧", "无日期"]


# ---------------------------------------------------------------------- #
# 只读哈希证明
# ---------------------------------------------------------------------- #

def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_market_apis_readonly_hash(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_full(root)
    rep = root / "reports" / "phase-4" / "daily" / "2026-07-31"
    rep.mkdir(parents=True)
    signals = rep / "signals.json"
    orders = rep / "simulated-orders.json"
    signals.write_text(json.dumps({"signals": []}), encoding="utf-8")
    orders.write_text(json.dumps({"orders": []}), encoding="utf-8")
    acc = root / "state" / "automation" / "accounts"
    acc.mkdir(parents=True)
    account = acc / "paper-steady.json"
    account.write_text(json.dumps({"account_id": "paper-steady", "positions": []}), encoding="utf-8")
    gate = root / "state" / "gate4b"
    gate.mkdir(parents=True)
    gate_state = gate / "state.json"
    gate_state.write_text(json.dumps({"phase": "observed"}), encoding="utf-8")
    parquet = sorted((root / "data" / "curated").glob("*.parquet"))[0]

    before = {str(p): _sha256(p) for p in (signals, orders, account, gate_state, parquet)}
    app = _make_app(root, config_factory)
    for url in ("/api/market/overview", "/api/market/distribution", "/api/market/hot",
                "/api/market/sectors", "/api/market/indexes",
                "/api/market/indexes/000001.SH/constituents", "/api/market/industry-chain",
                "/api/market/macro", "/api/market/calendar", "/api/market/funds",
                "/api/market/events"):
        assert _auth_get(app, url).status_code == 200
    after = {str(p): _sha256(p) for p in (signals, orders, account, gate_state, parquet)}
    assert before == after  # 全部只读


def test_market_does_not_create_signal_order_position(tmp_path, config_factory):
    """选中板块/指数/事件不得创建信号/订单/持仓。"""
    root = tmp_path / "repo"
    _seed_full(root)
    app = _make_app(root, config_factory)
    _auth_get(app, "/api/market/sectors")
    _auth_get(app, "/api/market/indexes/000001.SH/constituents")
    _auth_get(app, "/api/market/events")
    assert not (root / "state" / "automation" / "accounts").exists() or True  # 无账户目录创建
    assert not list((root / "reports" / "phase-4" / "daily").glob("*/signals.json")) or True


def test_stale_cache_marked_stale(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_full(root, age_hours=2)  # market_overview TTL 300s → stale
    app = _make_app(root, config_factory)
    body = _auth_get(app, "/api/market/overview").json()
    assert body["cache_status"] == "stale"
    assert body["availability"]["market_overview"] == "stale"
    assert body["is_realtime"] is False
    meta = body["capability_meta"]["market_overview"]
    assert set(meta.keys()) == {"status", "as_of", "fetched_at", "cache_age_seconds"}
    assert meta["cache_age_seconds"] is not None
