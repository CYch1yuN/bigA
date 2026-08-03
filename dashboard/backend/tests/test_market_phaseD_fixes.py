"""Phase D Codex 修正测试：严格 index_code / 成分隔离 / 派生日历 schema / 本地可用性。

全部 tmp_path 隔离；构造 Westock 缓存样本验证受控行为。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from app.main import create_app
from app.westock_bridge import CAPABILITY_MAP


def _write_cache(root: Path, capability: str, data, *, as_of: str = "2026-07-31") -> Path:
    path = root / "state" / "dashboard" / "westock" / capability / "global.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "capability": capability,
        "tool": CAPABILITY_MAP[capability].tool,
        "scope": "global",
        "source": "westock-mcp",
        "transport": "cache_export",
        "as_of": as_of,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
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


def _make_app(root: Path, config_factory):
    return create_app(config_factory(project_root=root), enable_static=False)


def _auth_get(app, url: str):
    from fastapi.testclient import TestClient

    with TestClient(app, base_url="https://127.0.0.1") as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "secret123"})
        return client.get(url)


# ---------------------------------------------------------------------- #
# 一、严格 index_code
# ---------------------------------------------------------------------- #

@pytest.mark.parametrize("bad", [
    "ABC", "000001", "000001.XX", "000001.SH/../", "000001 SH", " 000001.SH ",
    "000001.SHHHH", "000001.SHAAAA", "../etc", "9" * 20,
])
def test_strict_index_code_rejected(tmp_path, config_factory, bad):
    root = tmp_path / "repo"
    _write_cache(root, "index", {"indexes": [{"code": "000001.SH", "name": "上证"}]})
    app = _make_app(root, config_factory)
    r = _auth_get(app, f"/api/market/indexes/{bad}/constituents")
    assert r.status_code in (400, 404)  # 非法 index_code 拒绝
    if r.status_code == 400:
        assert r.json()["error"]["code"] == "invalid_index_code"


def test_valid_index_code_accepted(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_curated(root)
    _write_cache(root, "index", {
        "indexes": [{"code": "000001.SH", "name": "上证指数", "constituents": [
            {"symbol": "600519.SH", "name": "贵州茅台", "weight": 1.0},
        ]}],
    })
    app = _make_app(root, config_factory)
    body = _auth_get(app, "/api/market/indexes/000001.SH/constituents").json()
    assert body["data"]["constituents"][0]["symbol"] == "600519.SH"


# ---------------------------------------------------------------------- #
# 二、成分股按 index_code 隔离（交叉泄漏）
# ---------------------------------------------------------------------- #

def test_constituents_isolated_by_index(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_curated(root)
    _write_cache(root, "index", {
        "indexes": [
            {"code": "000001.SH", "name": "上证指数",
             "constituents": [{"symbol": "600519.SH", "name": "贵州茅台", "weight": 5.2}]},
            {"code": "399001.SZ", "name": "深证成指",
             "constituents": [{"symbol": "000001.SZ", "name": "平安银行", "weight": 1.0}]},
        ],
    })
    app = _make_app(root, config_factory)

    a = _auth_get(app, "/api/market/indexes/000001.SH/constituents").json()
    assert [c["symbol"] for c in a["data"]["constituents"]] == ["600519.SH"]

    b = _auth_get(app, "/api/market/indexes/399001.SZ/constituents").json()
    assert [c["symbol"] for c in b["data"]["constituents"]] == ["000001.SZ"]

    # 请求 C：必须为空 + 明确 warning，不得返回 A/B 成分
    c = _auth_get(app, "/api/market/indexes/688001.SH/constituents").json()
    assert c["data"]["constituents"] == []
    assert any("未找到该指数的成分股缓存" in w for w in c["warnings"])

    # A/B 互不泄漏
    assert all("000001.SZ" not in c["symbol"] for c in a["data"]["constituents"])
    assert all("600519.SH" not in c["symbol"] for c in b["data"]["constituents"])


def test_constituents_controlled_mapping(tmp_path, config_factory):
    """{constituents_by_index: {...}} 只读取精确键。"""
    root = tmp_path / "repo"
    _seed_curated(root)
    _write_cache(root, "index", {
        "constituents_by_index": {
            "000001.SH": [{"symbol": "600519.SH", "name": "贵州茅台"}],
            "399001.SZ": [{"symbol": "000001.SZ", "name": "平安银行"}],
        },
    })
    app = _make_app(root, config_factory)
    a = _auth_get(app, "/api/market/indexes/000001.SH/constituents").json()
    assert [c["symbol"] for c in a["data"]["constituents"]] == ["600519.SH"]
    assert all("未找到" not in w for w in a["warnings"])


# ---------------------------------------------------------------------- #
# 三、派生日历 schema
# ---------------------------------------------------------------------- #

def _calendar_seed(root: Path) -> None:
    _write_cache(root, "events", [
        {"date": "2026-08-01", "time": "09:30", "title": "事件甲", "importance": "high",
         "country": "中国", "actual": 3.2, "forecast": 2.8, "previous": 2.5,
         "url": "https://example.com/e1"},
        {"date": "2026-07-30", "time": "15:00:00", "title": "事件乙", "importance": "low",
         "actual": float("nan"), "forecast": float("inf"), "previous": "好"},
        {"date": "not-a-date", "title": "无日期事件", "importance": "EXTREME"},
    ])
    _write_cache(root, "announcements", [
        {"ann_date": "2026-07-31", "title": "公告丙", "importance": "medium",
         "url": "javascript:alert(1)", "time": "25:99"},
        {"ann_date": "2026-07-29", "title": "公告丁", "url": "https://user:pass@host.com/x"},
    ])


def test_calendar_full_schema(tmp_path, config_factory):
    root = tmp_path / "repo"
    _calendar_seed(root)
    app = _make_app(root, config_factory)
    body = _auth_get(app, "/api/market/calendar?limit=50&offset=0").json()
    items = body["data"]["items"]
    by_title = {i["title"]: i for i in items}

    # 事件甲：完整受控字段
    e1 = by_title["事件甲"]
    assert e1["date"] == "2026-08-01"
    assert e1["time"] == "09:30"
    assert e1["importance"] == "high"
    assert e1["country"] == "中国"
    assert e1["actual"] == 3.2
    assert e1["forecast"] == 2.8
    assert e1["previous"] == 2.5
    assert e1["url"] == "https://example.com/e1"

    # 事件乙：NaN/Infinity 丢弃，previous 为受限标量文本；time HH:MM:SS 接受
    e2 = by_title["事件乙"]
    assert "actual" not in e2
    assert "forecast" not in e2
    assert e2["previous"] == "好"
    assert e2["time"] == "15:00:00"

    # 无日期事件：importance EXTREME 丢弃，日期缺失置末尾
    e3 = by_title["无日期事件"]
    assert "importance" not in e3
    assert "date" not in e3

    # 公告丙：javascript 协议 URL 丢弃 + time "25:99" 非法丢弃 + importance medium 保留
    e5 = by_title["公告丙"]
    assert "url" not in e5
    assert "time" not in e5
    assert e5["importance"] == "medium"

    # 公告丁：带凭据 URL 丢弃
    e6 = by_title["公告丁"]
    assert "url" not in e6

    # 排序：合法日期倒序，非法日期末尾
    dated = [i for i in items if i.get("date")]
    dates = [i["date"] for i in dated]
    assert dates == sorted(dates, reverse=True)
    assert items[-1]["title"] == "无日期事件"


def test_calendar_filtering_real_and_invalid_category(tmp_path, config_factory):
    root = tmp_path / "repo"
    _calendar_seed(root)
    app = _make_app(root, config_factory)

    # importance 过滤真实生效
    high = _auth_get(app, "/api/market/calendar?importance=high&limit=50&offset=0").json()
    assert [i["title"] for i in high["data"]["items"]] == ["事件甲"]
    low = _auth_get(app, "/api/market/calendar?importance=low&limit=50&offset=0").json()
    assert [i["title"] for i in low["data"]["items"]] == ["事件乙"]

    # category 过滤真实生效（白名单 events/announcements）
    ev = _auth_get(app, "/api/market/calendar?category=events&limit=50&offset=0").json()
    assert all(i["category"] == "events" for i in ev["data"]["items"])
    ann = _auth_get(app, "/api/market/calendar?category=announcements&limit=50&offset=0").json()
    assert all(i["category"] == "announcements" for i in ann["data"]["items"])

    # 非法 category → invalid_calendar_params（不得静默空结果）
    r = _auth_get(app, "/api/market/calendar?category=risk")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_calendar_params"


def test_calendar_derived_boundary_warning(tmp_path, config_factory):
    root = tmp_path / "repo"
    _calendar_seed(root)
    app = _make_app(root, config_factory)
    body = _auth_get(app, "/api/market/calendar?limit=50&offset=0").json()
    assert any("并非独立财经日历能力" in w for w in body["warnings"])
    assert any("actual、forecast、previous 仅在来源数据明确提供时展示" in w for w in body["warnings"])


# ---------------------------------------------------------------------- #
# 四、hot/sector 本地可用性来自 curated
# ---------------------------------------------------------------------- #

def test_hot_local_history_from_curated(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_curated(root)
    _write_cache(root, "hot_ranking", {
        "stocks": [
            {"rank": 1, "symbol": "600519.SH", "name": "有本地", "heat": 90, "local_history_available": False},
            {"rank": 2, "symbol": "999999.SZ", "name": "无本地", "heat": 80, "local_history_available": True},
        ],
        "sectors": [
            {"rank": 1, "code": "BK01", "name": "白酒", "heat": 88,
             "leader_symbol": "600519.SH", "leader_name": "茅台", "leader_local_history_available": False},
            {"rank": 2, "code": "BK02", "name": "未知", "heat": 10,
             "leader_symbol": "999999.SZ", "leader_name": "无本地", "leader_local_history_available": True},
        ],
    })
    app = _make_app(root, config_factory)
    hot = _auth_get(app, "/api/market/hot").json()["data"]["hot"]
    by_symbol = {s["symbol"]: s for s in hot["stocks"]}
    # 不信 Westock 返回值：600519.SH curated 有 → True；999999.SZ curated 无 → False
    assert by_symbol["600519.SH"]["local_history_available"] is True
    assert by_symbol["999999.SZ"]["local_history_available"] is False
    sector_by_code = {s["code"]: s for s in hot["sectors"]}
    assert sector_by_code["BK01"]["leader_local_history_available"] is True
    assert sector_by_code["BK02"]["leader_local_history_available"] is False


def test_sectors_leader_local_history_from_curated(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_curated(root)
    _write_cache(root, "sector", [
        {"code": "BK01", "name": "白酒", "sector_type": "concept", "change_percent": 1.0,
         "leader_symbol": "600519.SH", "leader_name": "茅台", "leader_local_history_available": False},
        {"code": "BK02", "name": "未知", "sector_type": "concept", "change_percent": 0.5,
         "leader_symbol": "999999.SZ", "leader_name": "无本地", "leader_local_history_available": True},
    ])
    app = _make_app(root, config_factory)
    sectors = _auth_get(app, "/api/market/sectors").json()["data"]["sectors"]
    by_code = {s["code"]: s for s in sectors}
    assert by_code["BK01"]["leader_local_history_available"] is True
    assert by_code["BK02"]["leader_local_history_available"] is False
