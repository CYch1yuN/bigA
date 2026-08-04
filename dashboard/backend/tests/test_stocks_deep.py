"""Phase C: 个股深度数据聚合 API 测试（全部 tmp_path 隔离，不触碰真实缓存）。

核心证明：
- 逐能力 fresh/stale/unavailable，单能力失败不影响整体
- 受控标准化：只输出白名单字段；未知结构 → unavailable + warning
- intel 分页 / category 过滤 / 链接协议过滤（http/https 仅保留）
- 风险提示标明 Westock 来源
- 技术指标只展示，不写入本地序列
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.main import create_app
from app.westock_bridge import CAPABILITY_MAP


def _write_cache(root: Path, capability: str, symbol: str, data, *,
                 fetched_at: str | None = None, tool: str | None = None,
                 corrupt: bool = False) -> Path:
    path = root / "state" / "dashboard" / "westock" / capability / f"{symbol}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if corrupt:
        path.write_text("{ 损坏", encoding="utf-8")
        return path
    payload = {
        "schema_version": 1,
        "capability": capability,
        "tool": tool or CAPABILITY_MAP[capability].tool,  # 使用注册表真实 tool 名
        "scope": symbol,
        "source": "westock-mcp",
        "transport": "cache_export",
        "as_of": "2026-07-31",
        "fetched_at": fetched_at or datetime.now(timezone.utc).isoformat(),
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "data": data,
        "warnings": [],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _make_app(root: Path, config_factory):
    return create_app(config_factory(project_root=root), enable_static=False)


def _auth_get(app, url: str):
    from fastapi.testclient import TestClient

    with TestClient(app, base_url="https://127.0.0.1") as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "secret123"})
        return client.get(url)


def _seed_full(root: Path, symbol: str = "600519.SH", *, age_hours: float | None = None) -> None:
    fetched = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat() if age_hours else None
    caches = {
        "profile": {"name": "贵州茅台", "industry": "白酒", "business": "茅台酒生产销售",
                    "list_date": "2001-08-27", "registered_capital": "12.56亿"},
        "financials": {"code": 0, "msg": "success", "data": {"sh600519": {
            "balance": [{"SecuCode": "sh600519", "EndDate": "2026-06-30",
                         "TotalLiability": "4.0e11", "TotalShareholderEquity": "2.5e11",
                         "CashEquivalents": "1.2e11", "BillAccReceivable": "3.2e10",
                         "InfoPublDate": "2026-08-20 00:00:00 +0800 CST"}],
            "cashflow": [{"SecuCode": "sh600519", "EndDate": "2026-06-30",
                          "NetOperateCashFlow": "5.5e10", "NetInvestCashFlow": "-1.2e10",
                          "NetFinanceCashFlow": "-2.5e9",
                          "InfoPublDate": "2026-08-20 00:00:00 +0800 CST"}],
            "income": [{"SecuCode": "sh600519", "EndDate": "2026-06-30",
                        "OperatingRevenue": "8.7e10", "OperatingCost": "1.2e10",
                        "OperatingProfit": "6.0e10", "TotalProfit": "6.1e10",
                        "NPParentCompanyOwners": "4.1e10", "BasicEPS": "32.6",
                        "InfoPublDate": "2026-08-20 00:00:00 +0800 CST"}],
        }}},
        "forecast": {"code": "sh600519", "name": "贵州茅台", "targetPrice": 2100.0,
                     "forecasts": [{"year": 2026, "eps": 68.0, "revenue": 1.8e11,
                                    "netProfit": 8.5e10, "pe": 20.0, "pb": 5.0, "ps": 9.0,
                                    "revenueYoy": 5.0, "netProfitYoy": 4.0,
                                    "institutionCnt": 12}]},
        "shareholders": {"sh600519": {"code": "sh600519", "date": "2026-06-30",
                                       "name": "贵州茅台",
                                       "top10Shareholders": [
                                           {"no": 1, "name": "贵州国资", "holdShares": 680000000,
                                            "holdPct": 54.0, "holdChange": 0}],
                                       "top10FloatShareholders": [
                                           {"no": 1, "name": "贵州国资", "holdShares": 680000000,
                                            "holdPct": 54.0, "holdChange": 0}]}},
        "dividend": {"code": "sh600519", "start": "2025-08-04", "end": "2026-08-04",
                     "plans": [{"cashDiviRMB": "30.00", "dividendFlag": "是",
                                "dividendPlan": "10派300元", "dividendType": "有分红",
                                "exDiviDate": "20260710", "procedure": "方案实施",
                                "proposalSn": 1, "reportEndDate": "20251231",
                                "rightRegDate": "20260709",
                                "totalCashDiviComRMB": "1.5e10",
                                "bonusShareRatio": "", "tranAddShareRatio": ""}]},
        "buyback": {"status": "进行中", "price_range": "1500-1800", "amount": 1.5e10},
        "margin": {"margin_balance": 1.2e10, "margin_change": 5e7,
                   "short_balance": 3e8, "short_change": -1e7},
        "block_trade": [{"date": "2026-07-30", "price": 1350.0, "shares": 100000,
                         "amount": 1.35e8, "discount": -0.5}],
        "fund_flow": {"main": -2.1e8, "super_large": -1.5e8, "large": -6e7,
                      "medium": 4e7, "small": 1.7e8},
        "northbound": {"holding_shares": 8.5e7, "holding_ratio": 6.8, "change": -2e6},
        "lhb": [{"date": "2026-07-29", "reason": "日涨幅偏离值达7%", "seat": "机构专用",
                 "net_buy": 3.2e8, "buy": 5e8, "sell": 1.8e8}],
        "chip_distribution": {"concentration": 0.62,
                              "distribution": [{"price": 1300, "ratio": 10.0}]},
        "news": [{"title": "茅台发布半年报", "summary": "营收增长15%", "source": "上证报",
                  "date": "2026-08-01", "url": "https://example.com/news/1"}],
        "reports": [{"title": "维持买入评级", "org": "中信证券", "rating": "买入",
                     "target_price": 2200.0, "date": "2026-07-31"}],
        "announcements": [{"title": "2026年中期分红公告", "ann_type": "分红", "date": "2026-07-30"}],
        "events": [{"date": "2026-08-20", "type": "解禁", "title": "限售股解禁",
                    "summary": "占总股本0.5%", "tags": ["解禁"]}],
        "risk": [{"severity": "中", "title": "白酒需求波动", "description": "行业景气度下行"}],
        "technical": {"sh600519": {
            "code": "sh600519", "name": "贵州茅台", "date": "2026-07-31", "closePrice": 1360.0,
            "ma": {"MA_5": 1360.0, "MA_10": 1350.0, "MA_20": 1340.0, "MA_60": 1300.0},
            "macd": {"DIF": 2.3, "DEA": 1.8, "MACD": 0.5},
            "kdj": {"KDJ_K": 60.0, "KDJ_D": 55.0, "KDJ_J": 70.0},
            "rsi": {"RSI_6": 50.0, "RSI_12": 55.0, "RSI_24": 58.0},
            "boll": {"BOLL_UPPER": 1400.0, "BOLL_MID": 1350.0, "BOLL_LOWER": 1300.0},
        }},
    }
    for capability, data in caches.items():
        _write_cache(root, capability, symbol, data, fetched_at=fetched)


# ---------------------------------------------------------------------- #
# 基础：统一 envelope / 认证 / 非法 symbol
# ---------------------------------------------------------------------- #

def test_deep_requires_auth(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_full(root)
    app = _make_app(root, config_factory)
    from fastapi.testclient import TestClient
    with TestClient(app, base_url="https://127.0.0.1") as client:
        assert client.get("/api/stocks/600519.SH/fundamentals").status_code == 401


def test_deep_rejects_bad_symbol(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_full(root)
    app = _make_app(root, config_factory)
    for path in ("/api/stocks/../etc/passwd/fundamentals", "/api/stocks/600519/fundamentals"):
        r = _auth_get(app, path)
        assert r.status_code in (400, 404)


def test_empty_cache_unavailable_with_per_capability_status(tmp_path, config_factory):
    root = tmp_path / "repo"
    app = _make_app(root, config_factory)
    r = _auth_get(app, "/api/stocks/600519.SH/fundamentals")
    body = r.json()
    assert body["cache_status"] == "unavailable"
    assert body["availability"] == {"profile": "unavailable", "financials": "unavailable", "forecast": "unavailable"}
    assert body["is_realtime"] is False
    assert body["transport"] == "cache_export"
    assert all(body["data"][key] is None for key in ("profile", "financials", "forecast"))


# ---------------------------------------------------------------------- #
# 6 个聚合 API
# ---------------------------------------------------------------------- #

def test_fundamentals_aggregates_profile_financials_forecast(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_full(root)
    app = _make_app(root, config_factory)
    body = _auth_get(app, "/api/stocks/600519.SH/fundamentals").json()
    assert body["cache_status"] == "fresh"
    assert body["availability"]["profile"] == "fresh"
    assert body["data"]["profile"]["name"] == "贵州茅台"
    assert body["data"]["profile"]["industry"] == "白酒"
    fin = body["data"]["financials"]
    assert fin["periods"][0]["report_date"] == "2026-06-30"
    assert fin["summary"]["revenue"] == pytest.approx(8.7e10)
    assert fin["summary"]["net_profit"] == pytest.approx(4.1e10)
    assert fin["income_statement"]["revenue"] == pytest.approx(8.7e10)
    assert fin["balance_sheet"]["cash"] == pytest.approx(1.2e11)
    assert "unit_note" in fin
    fc = body["data"]["forecast"]
    assert fc["report_date"] == "2026"
    assert fc["consensus_eps"] == pytest.approx(68.0)
    assert fc["target_price"] == pytest.approx(2100.0)
    assert fc["forecasts"][0]["net_profit"] == pytest.approx(8.5e10)


def test_ownership_aggregates_shareholders_dividend_buyback(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_full(root)
    app = _make_app(root, config_factory)
    body = _auth_get(app, "/api/stocks/600519.SH/ownership").json()
    sh = body["data"]["shareholders"]
    assert sh["date"] == "2026-06-30"
    assert sh["major_shareholders"][0] == {
        "rank": 1, "name": "贵州国资", "shares": 680000000, "ratio": 54.0, "change": 0.0,
    }
    assert sh["float_shareholders"][0]["name"] == "贵州国资"
    assert "holder_count" not in sh and "share_structure" not in sh
    dv = body["data"]["dividend"]
    assert dv["plans"][0]["plan"] == "10派300元"
    assert dv["plan"] == "10派300元"  # 兼容字段
    assert dv["ex_date"] == "2026-07-10"
    assert "pay_date" not in dv  # 不伪造
    assert body["data"]["buyback"]["status"] == "进行中"


def test_funds_aggregates_six_capabilities(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_full(root)
    app = _make_app(root, config_factory)
    body = _auth_get(app, "/api/stocks/600519.SH/funds").json()
    assert body["cache_status"] == "fresh"
    assert body["data"]["margin"]["margin_balance"] == 1.2e10
    assert "net_buy" not in body["data"]["block_trade"][0]  # 非受控字段不输出
    assert body["data"]["block_trade"][0]["amount"] == 1.35e8
    assert body["data"]["fund_flow"]["main"] == -2.1e8
    assert body["data"]["northbound"]["holding_ratio"] == 6.8
    assert body["data"]["lhb"][0]["net_buy"] == 3.2e8
    assert body["data"]["chip_distribution"]["concentration"] == 0.62


def test_intel_pagination_category_and_url_filter(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_full(root)
    # 加一条非 http 链接的新闻（应被丢弃）
    _write_cache(root, "news", "600519.SH", [
        {"title": "好新闻", "summary": "s", "source": "src", "date": "2026-08-01", "url": "https://a.com/1"},
        {"title": "坏协议新闻", "summary": "s2", "source": "src", "date": "2026-08-01", "url": "javascript:alert(1)"},
    ])
    app = _make_app(root, config_factory)
    body = _auth_get(app, "/api/stocks/600519.SH/intel?limit=2&offset=0").json()
    assert body["cache_status"] == "fresh"
    assert body["data"]["total"] >= 3  # news2 + reports1 + announcements1（坏链接被丢但总数含条目）
    # 分页：limit=1 只返回 1 条
    body2 = _auth_get(app, "/api/stocks/600519.SH/intel?limit=1&offset=0").json()
    assert len(body2["data"]["items"]) == 1
    # 坏协议链接被过滤：无 url 为 javascript: 的条目
    for item in body["data"]["items"]:
        assert not (item.get("url") or "").startswith("javascript:")
    # category 过滤
    news = _auth_get(app, "/api/stocks/600519.SH/intel?category=news&limit=50&offset=0").json()
    assert all(item["category"] == "news" for item in news["data"]["items"])
    # 非法 category / limit
    assert _auth_get(app, "/api/stocks/600519.SH/intel?category=bad").status_code == 400
    assert _auth_get(app, "/api/stocks/600519.SH/intel?limit=0").status_code == 400
    assert _auth_get(app, "/api/stocks/600519.SH/intel?limit=51").status_code == 400
    assert _auth_get(app, "/api/stocks/600519.SH/intel?offset=-1").status_code == 400


def test_events_includes_risk_source_warning(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_full(root)
    app = _make_app(root, config_factory)
    body = _auth_get(app, "/api/stocks/600519.SH/events").json()
    assert body["data"]["events"][0]["tags"] == ["解禁"]
    assert body["data"]["risk"][0]["severity"] == "中"
    assert any("不替代人工判断" in w for w in body["warnings"])


def test_technical_controlled_indicators_and_note(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_full(root)
    app = _make_app(root, config_factory)
    body = _auth_get(app, "/api/stocks/600519.SH/technical").json()
    assert body["data"]["indicators"]["rsi"]["rsi12"] == 55.0
    assert body["data"]["indicators"]["ma"]["ma5"] == 1360.0
    assert "BigA 策略与回测使用本地 curated 数据独立计算" in body["data"]["note"]
    assert body["availability"]["technical"] == "fresh"


# ---------------------------------------------------------------------- #
# 降级：stale / 单能力失败 / 未知结构 / 损坏
# ---------------------------------------------------------------------- #

def test_stale_capability_marked_stale_not_fresh(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_full(root, age_hours=26)  # profile/financials/forecast TTL 24h → 26h 为 stale
    app = _make_app(root, config_factory)
    body = _auth_get(app, "/api/stocks/600519.SH/fundamentals").json()
    assert body["cache_status"] == "stale"
    assert body["availability"]["profile"] == "stale"
    assert body["is_realtime"] is False


def test_single_capability_failure_does_not_break_api(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_full(root)
    _write_cache(root, "forecast", "600519.SH", {"unknown_shape": 1})  # 结构未知
    app = _make_app(root, config_factory)
    body = _auth_get(app, "/api/stocks/600519.SH/fundamentals").json()
    assert body["availability"]["profile"] == "fresh"
    assert body["availability"]["forecast"] == "unavailable"
    assert body["data"]["forecast"] is None
    assert body["data"]["profile"]["name"] == "贵州茅台"  # 其他能力不受影响
    assert any("forecast" in w for w in body["warnings"])


def test_corrupt_cache_fails_open(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_full(root)
    _write_cache(root, "news", "600519.SH", {"x": 1}, corrupt=True)
    app = _make_app(root, config_factory)
    r = _auth_get(app, "/api/stocks/600519.SH/intel?limit=50&offset=0")
    body = r.json()
    assert r.status_code == 200
    assert body["availability"]["news"] == "unavailable"
    assert body["availability"]["reports"] == "fresh"
    assert str(root) not in r.text  # 不泄露路径


def test_unknown_schema_degrades_with_warning(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_full(root)
    _write_cache(root, "lhb", "600519.SH", [{"odd_field": 1}])  # 缺受控字段
    app = _make_app(root, config_factory)
    body = _auth_get(app, "/api/stocks/600519.SH/funds").json()
    assert body["availability"]["lhb"] == "unavailable"
    assert body["data"]["lhb"] is None
    assert any("lhb" in w for w in body["warnings"])
