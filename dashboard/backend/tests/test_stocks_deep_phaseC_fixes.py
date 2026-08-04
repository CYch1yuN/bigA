"""Phase C 修正测试：强制 schema / 限制 / Intel 排序 / URL 安全 / as_of / 错误码 / 只读哈希。

全部 tmp_path 隔离；构造 Westock 缓存样本验证受控标准化行为。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.main import create_app
from app.westock_bridge import CAPABILITY_MAP


def _write_cache(root: Path, capability: str, symbol: str, data, *,
                 fetched_at: str | None = None, as_of: str = "2026-07-31") -> Path:
    path = root / "state" / "dashboard" / "westock" / capability / f"{symbol}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "capability": capability,
        "tool": CAPABILITY_MAP[capability].tool,
        "scope": symbol,
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


def _make_app(root: Path, config_factory):
    return create_app(config_factory(project_root=root), enable_static=False)


def _auth_get(app, url: str):
    from fastapi.testclient import TestClient

    with TestClient(app, base_url="https://127.0.0.1") as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "secret123"})
        return client.get(url)


# ---------------------------------------------------------------------- #
# 强制 schema：嵌套任意键不透传
# ---------------------------------------------------------------------- #

def test_nested_unknown_keys_dropped(tmp_path, config_factory):
    """股东/股本/技术指标的未知嵌套键全部丢弃，仅保留白名单字段。"""
    root = tmp_path / "repo"
    _write_cache(root, "shareholders", "600519.SH", {
        "sh600519": {"code": "sh600519", "date": "2026-06-30", "name": "贵州茅台",
                     "top10Shareholders": [
                         {"no": 1, "name": "股东A", "holdShares": 1000000,
                          "holdPct": 50.0, "holdChange": 0, "hacked": "x",
                          "odd": {"deep": 1}},
                     ],
                     "top10FloatShareholders": []},
    })
    _write_cache(root, "technical", "600519.SH", {
        "sh600519": {
            "code": "sh600519", "date": "2026-07-31",
            "ma": {"MA_5": 1.0, "MA_999": 2.0, "hacked": 3.0},
            "macd": {"DIF": 0.1, "DEA": 0.2, "MACD": 0.3, "extra": 9},
            "mystery_indicator": {"a": 1},
        },
    })
    app = _make_app(root, config_factory)

    own = _auth_get(app, "/api/stocks/600519.SH/ownership").json()
    sh = own["data"]["shareholders"]
    assert sh["major_shareholders"] == [
        {"rank": 1, "name": "股东A", "shares": 1000000, "ratio": 50.0, "change": 0.0},
    ]
    assert "hacked" not in sh["major_shareholders"][0]
    assert "holder_count" not in sh and "share_structure" not in sh  # 不伪造

    tech = _auth_get(app, "/api/stocks/600519.SH/technical").json()
    ind = tech["data"]["indicators"]
    assert ind["ma"] == {"ma5": 1.0}
    assert ind["macd"] == {"dif": 0.1, "dea": 0.2, "macd": 0.3}
    assert "mystery_indicator" not in ind


def test_financial_sheets_whitelist_only(tmp_path, config_factory):
    """三张报表只输出明确字段，任意 key 动态输出被禁止。"""
    root = tmp_path / "repo"
    _write_cache(root, "financials", "600519.SH", {
        "code": 0, "msg": "success", "data": {"sh600519": {
            "balance": [{"SecuCode": "sh600519", "EndDate": "2026-06-30",
                         "CashEquivalents": "1.0", "hacked": 4.0}],
            "cashflow": [{"SecuCode": "sh600519", "EndDate": "2026-06-30",
                          "NetOperateCashFlow": "5.0"}],
            "income": [{"SecuCode": "sh600519", "EndDate": "2026-06-30",
                        "OperatingRevenue": 1.0, "NPParentCompanyOwners": 2.0,
                        "任意键": 99}],
        }},
    })
    app = _make_app(root, config_factory)
    fin = _auth_get(app, "/api/stocks/600519.SH/fundamentals").json()["data"]["financials"]
    assert fin["income_statement"] == {"revenue": 1.0, "net_profit": 2.0}
    assert "任意键" not in fin["income_statement"]
    assert fin["balance_sheet"] == {"cash": 1.0}
    assert "hacked" not in fin["balance_sheet"]
    assert fin["cash_flow"] == {"operating_cash_flow": 5.0}


def test_nan_infinity_dropped(tmp_path, config_factory):
    root = tmp_path / "repo"
    _write_cache(root, "forecast", "600519.SH", {
        "code": "sh600519", "name": "贵州茅台", "targetPrice": float("nan"),
        "forecasts": [{"year": 2026, "eps": float("inf"), "revenue": 1.0}],
    })
    app = _make_app(root, config_factory)
    fc = _auth_get(app, "/api/stocks/600519.SH/fundamentals").json()["data"]["forecast"]
    assert "target_price" not in fc  # NaN 丢弃
    assert "consensus_eps" not in fc  # Infinity 丢弃
    assert "eps" not in fc["forecasts"][0]  # 数值字段 Infinity 不输出
    assert fc["forecasts"][0]["revenue"] == 1.0


# ---------------------------------------------------------------------- #
# 统一限制：裁剪 + warning
# ---------------------------------------------------------------------- #

def test_list_limits_trim_with_warning(tmp_path, config_factory):
    root = tmp_path / "repo"
    news = [{"title": f"新闻{i}", "date": f"2026-07-{i % 28 + 1:02d}"} for i in range(205)]
    _write_cache(root, "news", "600519.SH", news)
    app = _make_app(root, config_factory)
    body = _auth_get(app, "/api/stocks/600519.SH/intel?category=news&limit=50&offset=0").json()
    assert body["data"]["news"] is not None
    assert len(body["data"]["news"]) == 200  # 裁剪到上限
    assert any("裁剪" in w for w in body["warnings"])
    assert body["data"]["total"] == 200  # total 对应裁剪后


def test_long_text_trimmed(tmp_path, config_factory):
    root = tmp_path / "repo"
    long_title = "长" * 500
    long_summary = "摘" * 1000
    _write_cache(root, "news", "600519.SH", [
        {"title": long_title, "summary": long_summary, "date": "2026-07-31", "url": "https://a.com/1"},
    ])
    app = _make_app(root, config_factory)
    item = _auth_get(app, "/api/stocks/600519.SH/intel?category=news&limit=10&offset=0").json()["data"]["items"][0]
    assert len(item["title"]) == 200  # 标题上限 200
    assert len(item["summary"]) == 400  # 文本上限 400


def test_shareholder_and_chip_limits(tmp_path, config_factory):
    root = tmp_path / "repo"
    _write_cache(root, "shareholders", "600519.SH", {
        "sh600519": {"code": "sh600519", "date": "2026-06-30", "name": "贵州茅台",
                     "top10Shareholders": [
                         {"no": i, "name": f"股东{i}", "holdShares": i * 1000,
                          "holdPct": float(i), "holdChange": 0} for i in range(1, 26)],
                     "top10FloatShareholders": []},
    })
    _write_cache(root, "chip_distribution", "600519.SH", {
        "concentration": 0.5,
        "distribution": [{"price": i, "ratio": i} for i in range(60)],
    })
    app = _make_app(root, config_factory)
    own = _auth_get(app, "/api/stocks/600519.SH/ownership").json()
    assert len(own["data"]["shareholders"]["major_shareholders"]) == 10  # 单列表上限 10
    assert any("裁剪" in w for w in own["warnings"])
    funds = _auth_get(app, "/api/stocks/600519.SH/funds").json()
    chip = funds["data"]["chip_distribution"]
    assert len(chip["distribution"]) == 50  # 上限 50
    assert any("裁剪" in w for w in funds["warnings"])


# ---------------------------------------------------------------------- #
# future timestamp / as_of
# ---------------------------------------------------------------------- #

def test_future_timestamp_unavailable(tmp_path, config_factory):
    root = tmp_path / "repo"
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    _write_cache(root, "profile", "600519.SH", {"name": "X"}, fetched_at=future)
    app = _make_app(root, config_factory)
    body = _auth_get(app, "/api/stocks/600519.SH/fundamentals").json()
    assert body["availability"]["profile"] == "unavailable"
    assert body["data"]["profile"] is None


def test_as_of_comes_from_data_not_now(tmp_path, config_factory):
    """聚合 as_of 使用能力数据 as_of（2026-07-31），不是系统当天。"""
    root = tmp_path / "repo"
    _write_cache(root, "profile", "600519.SH", {"name": "贵州茅台"}, as_of="2026-07-25")
    _write_cache(root, "financials", "600519.SH", {"roe": 1.0}, as_of="2026-07-31")
    app = _make_app(root, config_factory)
    body = _auth_get(app, "/api/stocks/600519.SH/fundamentals").json()
    assert body["as_of"] == "2026-07-31"  # 最新合法 as_of
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert body["as_of"] != today


def test_capability_meta_only_public_fields(tmp_path, config_factory):
    root = tmp_path / "repo"
    _write_cache(root, "profile", "600519.SH", {"name": "X"})
    app = _make_app(root, config_factory)
    meta = _auth_get(app, "/api/stocks/600519.SH/fundamentals").json()["capability_meta"]["profile"]
    assert set(meta.keys()) == {"status", "as_of", "fetched_at", "cache_age_seconds"}
    assert meta["status"] == "fresh"
    assert meta["cache_age_seconds"] is not None


# ---------------------------------------------------------------------- #
# Intel 稳定排序 / 分页一致性 / URL 安全
# ---------------------------------------------------------------------- #

def test_intel_stable_sort_invalid_date_last(tmp_path, config_factory):
    root = tmp_path / "repo"
    _write_cache(root, "news", "600519.SH", [
        {"title": "旧闻", "date": "2026-07-01"},
        {"title": "无日期", "date": "not-a-date"},
        {"title": "新闻", "date": "2026-07-31"},
    ])
    _write_cache(root, "reports", "600519.SH", [
        {"title": "研报同日", "date": "2026-07-31"},
    ])
    app = _make_app(root, config_factory)
    items = _auth_get(app, "/api/stocks/600519.SH/intel?limit=50&offset=0").json()["data"]["items"]
    titles = [i["title"] for i in items]
    assert titles == ["新闻", "研报同日", "旧闻", "无日期"]  # 日期倒序；非法日期末尾
    # 同日按 category 固定顺序（news 在 reports 前）


def test_intel_pagination_consistent_across_calls(tmp_path, config_factory):
    root = tmp_path / "repo"
    dates = [f"2026-07-{i % 28 + 1:02d}" for i in range(45)]
    _write_cache(root, "news", "600519.SH", [
        {"title": f"新闻{i:02d}", "date": dates[i]} for i in range(45)
    ])
    app = _make_app(root, config_factory)
    first = _auth_get(app, "/api/stocks/600519.SH/intel?category=news&limit=20&offset=0").json()["data"]["items"]
    second = _auth_get(app, "/api/stocks/600519.SH/intel?category=news&limit=20&offset=20").json()["data"]["items"]
    third = _auth_get(app, "/api/stocks/600519.SH/intel?category=news&limit=20&offset=40").json()["data"]["items"]
    all_items = first + second + third
    all_titles = [i["title"] for i in all_items]
    assert len(all_titles) == 45
    assert len(set(all_titles)) == 45  # 无重复、无遗漏（分页一致）
    assert all(i.get("category") == "news" for i in all_items)


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",   # 非 http 协议
        "file:///etc/passwd",    # file 协议
        "https://user:pass@host.com/x",  # 带凭据
        "https://",              # 无 hostname
        "not-a-url",
    ],
)
def test_intel_unsafe_urls_dropped(tmp_path, config_factory, url):
    root = tmp_path / "repo"
    _write_cache(root, "news", "600519.SH", [
        {"title": "安全新闻", "date": "2026-07-31", "url": url},
    ])
    app = _make_app(root, config_factory)
    items = _auth_get(app, "/api/stocks/600519.SH/intel?category=news&limit=10&offset=0").json()["data"]["items"]
    assert items  # 条目仍存在
    assert "url" not in items[0]  # 不安全 URL 被丢弃


def test_intel_safe_url_kept(tmp_path, config_factory):
    root = tmp_path / "repo"
    _write_cache(root, "news", "600519.SH", [
        {"title": "好新闻", "date": "2026-07-31", "url": "https://example.com/a?b=1"},
    ])
    app = _make_app(root, config_factory)
    item = _auth_get(app, "/api/stocks/600519.SH/intel?category=news&limit=10&offset=0").json()["data"]["items"][0]
    assert item["url"] == "https://example.com/a?b=1"


# ---------------------------------------------------------------------- #
# 错误码语义
# ---------------------------------------------------------------------- #

def test_intel_error_codes(tmp_path, config_factory):
    root = tmp_path / "repo"
    app = _make_app(root, config_factory)
    bad_sym = _auth_get(app, "/api/stocks/600519/intel")  # 无后缀 → 非法 symbol
    assert bad_sym.status_code == 400
    assert bad_sym.json()["error"]["code"] == "invalid_symbol"
    bad_cat = _auth_get(app, "/api/stocks/600519.SH/intel?category=bad")
    assert bad_cat.json()["error"]["code"] == "invalid_category"
    bad_limit = _auth_get(app, "/api/stocks/600519.SH/intel?limit=999")
    assert bad_limit.json()["error"]["code"] == "invalid_request"
    bad_offset = _auth_get(app, "/api/stocks/600519.SH/intel?offset=-1")
    assert bad_offset.json()["error"]["code"] == "invalid_request"


# ---------------------------------------------------------------------- #
# 只读：调用前后哈希不变
# ---------------------------------------------------------------------- #

def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_apis_do_not_modify_artifacts(tmp_path, config_factory):
    root = tmp_path / "repo"
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
    curated = root / "data" / "curated"
    curated.mkdir(parents=True)
    import pandas as pd
    df = pd.DataFrame({
        "symbol": ["600519.SH"] * 2,
        "trade_date": ["2026-07-30", "2026-07-31"],
        "open_raw": [1.0, 2.0], "high_raw": [2.0, 3.0], "low_raw": [0.5, 1.5],
        "close_raw": [1.5, 2.5], "open_qfq": [1.0, 2.0], "high_qfq": [2.0, 3.0],
        "low_qfq": [0.5, 1.5], "close_qfq": [1.5, 2.5],
        "volume": [100, 200], "amount": [1e5, 2e5],
    })
    parquet = curated / "daily_quotes_600519.SH_2026-07-01_2026-07-31.parquet"
    df.to_parquet(parquet)

    before = {str(p): _sha256(p) for p in (signals, orders, account, parquet)}
    app = _make_app(root, config_factory)
    for url in ("/api/stocks/600519.SH/fundamentals", "/api/stocks/600519.SH/ownership",
                "/api/stocks/600519.SH/funds", "/api/stocks/600519.SH/intel?limit=10&offset=0",
                "/api/stocks/600519.SH/events", "/api/stocks/600519.SH/technical",
                "/api/stocks/600519.SH/history?adjustment=raw&range=all"):
        assert _auth_get(app, url).status_code == 200
    after = {str(p): _sha256(p) for p in (signals, orders, account, parquet)}
    assert before == after  # 信号/订单/账户/parquet 哈希全部不变
