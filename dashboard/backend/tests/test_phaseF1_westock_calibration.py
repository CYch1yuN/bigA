# -*- coding: utf-8 -*-
"""Phase F1：Westock 真实缓存最小映射 + scope/真实数据身份绑定测试。

覆盖：
1. stocks_service.normalize_quote 解包 {"sh600519": {...}} 单键嵌套 + expected_symbol 绑定
2. stocks_deep_service fund_flow 解包 + PascalCase 别名 + 身份校验
3. stocks_deep_service profile / news 身份校验
4. screener_service._remap_westock_row（code→symbol、字段别名、symbol/code 冲突处理）

全部使用内联缓存样例 / tmp_path，不读取或修改真实 state。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest


# ---------------------------------------------------------------------- #
# helpers：写合法缓存 envelope（fetched_at=now → fresh）
# ---------------------------------------------------------------------- #
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_envelope(tmp_path: Path, capability: str, scope: str, data, tool: str) -> Path:
    env = {
        "schema_version": 1,
        "capability": capability,
        "tool": tool,
        "scope": scope,
        "source": "westock-mcp",
        "transport": "cache_export",
        "as_of": "2026-08-04",
        "fetched_at": _now_iso(),
        "cached_at": _now_iso(),
        "data": data,
        "warnings": [],
    }
    path = tmp_path / "state" / "dashboard" / "westock" / capability / f"{scope}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(env, ensure_ascii=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------- #
# 1. quote：解包 + expected_symbol 身份绑定
# ---------------------------------------------------------------------- #
def test_normalize_quote_unwraps_symbol_nested():
    """quote 真实缓存：{"sh600519": {...snake_case...}} 单键嵌套必须解包。"""
    from app.stocks_service import normalize_quote

    cache_data = {
        "sh600519": {
            "price": 1338.99, "change_percent": -1.47, "time": "2026-08-04",
            "total_market_cap": 16738.47,
        },
    }
    out = normalize_quote(cache_data, "600519.SH")
    assert out is not None
    assert out["price"] == pytest.approx(1338.99)
    assert out["change_percent"] == pytest.approx(-1.47)
    assert out["time"] == "2026-08-04"


def test_normalize_quote_real_sample_with_identity():
    """真实样本含 code/symbol=sh600519，且与 expected 一致 → 正常输出。"""
    from app.stocks_service import normalize_quote

    cache_data = {
        "sh600519": {
            "code": "sh600519", "symbol": "sh600519",
            "price": 1338.99, "change_percent": -1.47, "time": "2026-08-04",
        },
    }
    out = normalize_quote(cache_data, "600519.SH")
    assert out is not None
    assert out["price"] == pytest.approx(1338.99)


def test_normalize_quote_outer_key_mismatch():
    """quote 外层唯一键错配（sh600000）→ None，绝不展示他股价格。"""
    from app.stocks_service import normalize_quote

    cache_data = {"sh600000": {"price": 10.0, "change_percent": 1.0}}
    assert normalize_quote(cache_data, "600519.SH") is None


def test_normalize_quote_inner_code_mismatch():
    """quote 内层 code 错配 → None。"""
    from app.stocks_service import normalize_quote

    cache_data = {"sh600519": {"code": "sh600000", "price": 10.0}}
    assert normalize_quote(cache_data, "600519.SH") is None


def test_normalize_quote_flat_code_mismatch():
    """flat payload 含 code 错配 → None（flat 也校验身份）。"""
    from app.stocks_service import normalize_quote

    assert normalize_quote({"code": "sh600000", "price": 10.0}, "600519.SH") is None


def test_normalize_quote_flat_identity_ok():
    """flat payload 含 code 匹配 → 正常。"""
    from app.stocks_service import normalize_quote

    out = normalize_quote({"code": "sh600519", "price": 10.5, "change_percent": 1.2}, "600519.SH")
    assert out is not None
    assert out["price"] == pytest.approx(10.5)


def test_quote_identity_conflict_reason():
    """quote_identity_conflict 返回脱敏 reason（不回显原始值）。"""
    from app.stocks_service import quote_identity_conflict

    assert "外层股票代码" in quote_identity_conflict({"sh600000": {"price": 1}}, "600519.SH")
    assert "code" in quote_identity_conflict({"sh600519": {"code": "sh600000"}}, "600519.SH")
    assert "symbol" in quote_identity_conflict({"sh600519": {"symbol": "sz000001"}}, "600519.SH")
    assert quote_identity_conflict({"sh600519": {"price": 1}}, "600519.SH") is None


def test_normalize_quote_unknown_structure_degrades():
    """多键或非符号键嵌套不解包 → 保持降级为 None。"""
    from app.stocks_service import normalize_quote

    assert normalize_quote({"a": {"price": 1.0}, "b": {"price": 2.0}}, "600519.SH") is None
    assert normalize_quote({"foo": {"price": 1.0}}, "600519.SH") is None
    assert normalize_quote({"sh600519": "not-a-dict"}, "600519.SH") is None


# ---------------------------------------------------------------------- #
# 2. fund_flow：解包 + 别名 + 身份绑定
# ---------------------------------------------------------------------- #
def test_unwrap_fund_flow_pascal_aliases():
    """fund_flow 真实缓存：{"sh600519": {"data": [{PascalCase 字符串字段}]}} → 受控 5 字段。"""
    from app.stocks_deep_service import (_FUND_FLOW_FIELDS,
                                         _norm_mapping, _unwrap_fund_flow)

    cache_data = {
        "sh600519": {
            "code": "sh600519",
            "data": [{
                "MainNetFlow": "-309615648", "JumboNetFlow": "-193055289",
                "BlockNetFlow": "-116560359", "MidNetFlow": "309702924",
                "SmallNetFlow": "-87276", "ClosePrice": "1338.55",
                "EndDate": "2026-08-04", "code": "sh600519", "SecuCode": "600519",
                "name": "贵州茅台",
            }],
        },
    }
    out = _norm_mapping(_unwrap_fund_flow(cache_data), _FUND_FLOW_FIELDS)
    assert out is not None
    assert out["main"] == pytest.approx(-309615648)
    assert out["super_large"] == pytest.approx(-193055289)
    assert out["large"] == pytest.approx(-116560359)
    assert out["medium"] == pytest.approx(309702924)
    assert out["small"] == pytest.approx(-87276)


def test_fund_flow_outer_key_mismatch():
    """fund_flow 外层键错配 → 身份冲突。"""
    from app.stocks_deep_service import _fund_flow_identity_conflict

    data = {"sh600000": {"code": "sh600000", "data": [{"code": "sh600000"}]}}
    reason = _fund_flow_identity_conflict(data, "600519.SH")
    assert reason is not None and "外层股票代码" in reason


def test_fund_flow_inner_code_mismatch():
    """fund_flow inner.code 错配 → 身份冲突。"""
    from app.stocks_deep_service import _fund_flow_identity_conflict

    data = {"sh600519": {"code": "sh600000", "data": [{"code": "sh600519"}]}}
    reason = _fund_flow_identity_conflict(data, "600519.SH")
    assert reason is not None and "内层 code" in reason


def test_fund_flow_row_code_mismatch():
    """fund_flow 首行 code 错配 → 身份冲突。"""
    from app.stocks_deep_service import _fund_flow_identity_conflict

    data = {"sh600519": {"code": "sh600519", "data": [{"code": "sh600000"}]}}
    reason = _fund_flow_identity_conflict(data, "600519.SH")
    assert reason is not None and "首行 code" in reason


def test_fund_flow_secucode_mismatch():
    """fund_flow SecuCode 六位数字部分必须等于请求六位数字。"""
    from app.stocks_deep_service import _fund_flow_identity_conflict

    bad = {"sh600519": {"code": "sh600519", "data": [{"code": "sh600519", "SecuCode": "000001"}]}}
    reason = _fund_flow_identity_conflict(bad, "600519.SH")
    assert reason is not None and "SecuCode" in reason
    # 非法格式（非 6 位数字）同样冲突
    bad2 = {"sh600519": {"code": "sh600519", "data": [{"code": "sh600519", "SecuCode": "600519X"}]}}
    assert _fund_flow_identity_conflict(bad2, "600519.SH") is not None
    # 带前缀但数字部分错配 → 冲突
    bad3 = {"sh600519": {"code": "sh600519", "data": [{"code": "sh600519", "SecuCode": "sz000001"}]}}
    assert _fund_flow_identity_conflict(bad3, "600519.SH") is not None


def test_fund_flow_real_sample_identity_ok():
    """fund_flow 真实样本（SecuCode 带前缀 sh600519 或纯 600519）→ 无冲突。"""
    from app.stocks_deep_service import _fund_flow_identity_conflict

    prefixed = {"sh600519": {"code": "sh600519",
                             "data": [{"code": "sh600519", "SecuCode": "sh600519"}]}}
    assert _fund_flow_identity_conflict(prefixed, "600519.SH") is None
    plain = {"sh600519": {"code": "sh600519",
                          "data": [{"code": "sh600519", "SecuCode": "600519"}]}}
    assert _fund_flow_identity_conflict(plain, "600519.SH") is None


def test_unwrap_fund_flow_flat_passthrough():
    """非嵌套结构原样返回，交给受控标准化判断（不应误解包）。"""
    from app.stocks_deep_service import _unwrap_fund_flow

    flat = {"main": 1.0}
    assert _unwrap_fund_flow(flat) is flat
    assert _unwrap_fund_flow("junk") == "junk"
    assert _unwrap_fund_flow(None) is None


# ---------------------------------------------------------------------- #
# 3. profile：code 身份绑定
# ---------------------------------------------------------------------- #
def test_profile_identity_ok_real_sample():
    """profile 真实样本 code=sh600519 → 无冲突。"""
    from app.stocks_deep_service import _profile_identity_conflict

    assert _profile_identity_conflict({"code": "sh600519", "name": "贵州茅台"}, "600519.SH") is None
    # code 缺失 → 无冲突（结构分支兜底）
    assert _profile_identity_conflict({"name": "贵州茅台"}, "600519.SH") is None


def test_profile_code_mismatch():
    """profile code 错配 → 身份冲突。"""
    from app.stocks_deep_service import _profile_identity_conflict

    reason = _profile_identity_conflict({"code": "sh600000", "name": "浦发银行"}, "600519.SH")
    assert reason is not None and "code" in reason


def test_profile_code_mismatch_service_degrades(tmp_path):
    """fundamentals：profile code 错配 → profile 独立 unavailable + warning；
    financials/forecast 等其他能力不受影响。"""
    from app.stocks_deep_service import StocksDeepService

    _write_envelope(tmp_path, "profile", "600519.SH",
                    {"code": "sh600000", "name": "浦发银行", "industry": "银行"},
                    "data_profile")
    svc = StocksDeepService(tmp_path)
    env = svc.fundamentals("600519.SH")
    assert env["data"]["profile"] is None
    assert env["availability"]["profile"] == "unavailable"
    assert any("身份校验失败" in w and "profile" in w for w in env["warnings"])
    # financials 未提供缓存 → 仍为 unavailable 但无身份 warning（不受 profile 影响）
    assert any("身份校验失败" in w and "financials" in w for w in env["warnings"]) is False


# ---------------------------------------------------------------------- #
# 4. news：symbol 身份过滤
# ---------------------------------------------------------------------- #
def test_news_mixed_match_and_mismatch():
    """news 混合：匹配条目保留、错配条目丢弃 + warning、无 symbol 条目保留。"""
    from app.stocks_deep_service import _norm_news_identity_items

    data = {"data": [
        {"id": "1", "symbol": "sh600519", "title": "茅台新闻", "time": "2026-08-04 09:31:09"},
        {"id": "2", "symbol": "sh600000", "title": "浦发新闻", "time": "2026-08-04 09:30:00"},
        {"id": "3", "title": "无 symbol 条目", "time": "2026-08-04 09:29:00"},
    ]}
    warnings: list[str] = []
    items, reason = _norm_news_identity_items(data, warnings, "600519.SH")
    assert reason == "ok"
    assert items is not None and len(items) == 2
    titles = [i["title"] for i in items]
    assert "茅台新闻" in titles and "无 symbol 条目" in titles
    assert "浦发新闻" not in titles
    assert any("身份不匹配" in w for w in warnings)
    # symbol 不输出到前端
    for item in items:
        assert "symbol" not in item


def test_news_all_mismatch_unavailable():
    """news 全部条目身份不匹配 → items None + identity_all_dropped + warning。"""
    from app.stocks_deep_service import _norm_news_identity_items

    data = {"data": [
        {"id": "1", "symbol": "sh600000", "title": "A", "time": "2026-08-04 09:31:09"},
        {"id": "2", "symbol": "sz000001", "title": "B", "time": "2026-08-04 09:30:00"},
    ]}
    warnings: list[str] = []
    items, reason = _norm_news_identity_items(data, warnings, "600519.SH")
    assert items is None
    assert reason == "identity_all_dropped"
    assert any("身份不匹配" in w for w in warnings)


def test_news_structure_unknown():
    """news 结构无法识别 → structure（不产生身份 warning）。"""
    from app.stocks_deep_service import _norm_news_identity_items

    warnings: list[str] = []
    items, reason = _norm_news_identity_items("junk", warnings, "600519.SH")
    assert items is None and reason == "structure"
    assert not any("身份不匹配" in w for w in warnings)


# ---------------------------------------------------------------------- #
# 5. screener：symbol/code 冲突处理 + 字段别名
# ---------------------------------------------------------------------- #
def test_remap_westock_row_symbol_and_aliases(tmp_path):
    """filter 行：code(sh600519)→symbol(600519.SH)；ChangePCT/ClosePrice 别名。"""
    from app.screener_service import ScreenerService

    svc = ScreenerService(tmp_path)
    remapped = svc._remap_westock_row({
        "ChangePCT": "-1.94", "ClosePrice": "1332.55",
        "code": "sh600519", "name": "贵州茅台",
    })
    assert remapped["symbol"] == "600519.SH"
    assert "code" not in remapped
    assert "change_percent" in remapped and "price" in remapped
    # 未知字段保留，交给 _norm_row 白名单过滤
    assert remapped["name"] == "贵州茅台"


def test_remap_westock_row_symbol_code_same(tmp_path):
    """symbol 与 code 同时存在且一致 → 正常（保留 symbol，移除 code）。"""
    from app.screener_service import ScreenerService

    svc = ScreenerService(tmp_path)
    out = svc._remap_westock_row({"symbol": "600519.SH", "code": "sh600519", "name": "贵州茅台"})
    assert out is not None
    assert out["symbol"] == "600519.SH"
    assert "code" not in out


def test_remap_westock_row_symbol_code_conflict_dropped(tmp_path):
    """symbol 与 code 转换后不同 → 整行丢弃（不静默选择任一方）。"""
    from app.screener_service import ScreenerService

    svc = ScreenerService(tmp_path)
    assert svc._remap_westock_row({"symbol": "600519.SH", "code": "sh600000"}) is None
    # symbol 非法格式 + code 合法 → 无法确认一致 → 丢弃
    assert svc._remap_westock_row({"symbol": "not-a-symbol", "code": "sh600519"}) is None


def test_filter_pipeline_extract_remap_norm(tmp_path):
    """filter 完整链路：extract → remap → norm_row，真实缓存样例产出合法行。"""
    from app.screener_service import ScreenerService

    svc = ScreenerService(tmp_path)
    envelope_data = {
        "totalStocks": 4996,
        "stocks": [
            {"ChangePCT": "16.52", "ClosePrice": "1892.26", "code": "sh688808", "name": "联讯仪器"},
            {"ChangePCT": "-1.94", "ClosePrice": "1332.55", "code": "sh600519", "name": "贵州茅台"},
        ],
    }
    raw_rows = svc._extract_rows(envelope_data)
    assert raw_rows is not None and len(raw_rows) == 2
    rows = []
    for raw in raw_rows:
        row = svc._norm_row(svc._remap_westock_row(raw), set())
        if row is not None:
            rows.append(row)
    assert len(rows) == 2
    moutai = next(r for r in rows if r["symbol"] == "600519.SH")
    assert moutai["name"] == "贵州茅台"
    assert moutai["price"] == pytest.approx(1332.55)
    assert moutai["change_percent"] == pytest.approx(-1.94)
    # 本地缓存不存在 → local_history_available 为 False（不信 Westock）
    assert moutai["local_history_available"] is False


def test_filter_real_envelope_do_run_no_leak(tmp_path):
    """filter 真实 envelope 走完整 _do_run：portfolioPath/expression/原始外层字段
    不得进入结果快照；结果行只含受控白名单字段。"""
    from app.screener_service import (ScreenerService, _validate_query,
                                      canonical_query_hash)

    query = {
        "mode": "condition",
        "universe": {"type": "local", "value": None},
        "conditions": [{"field": "price", "operator": "gt", "value": 0}],
        "sort": {"field": "score", "direction": "desc"},
        "limit": 20,
    }
    qhash = canonical_query_hash(_validate_query(query))
    data = {
        "totalStocks": 4996,
        "stockAmountInUniverse": "全市场",
        "portfolioPath": "/ashare/strategy/portfolio/demo",
        "expression": "price > 0",
        "date": "2026-08-04",
        "stocks": [
            {"ChangePCT": "16.52", "ClosePrice": "1892.26", "code": "sh688808", "name": "联讯仪器"},
            {"ChangePCT": "-1.94", "ClosePrice": "1332.55", "code": "sh600519", "name": "贵州茅台"},
        ],
    }
    _write_envelope(tmp_path, "filter", qhash, data, "tool_filter")
    # universe=local 会严格解析为本地 curated 集合：用空 parquet 占位让集合含这两只
    curated = tmp_path / "data" / "curated"
    curated.mkdir(parents=True, exist_ok=True)
    for sym in ("600519.SH", "688808.SH"):
        (curated / f"daily_quotes_{sym}_x.parquet").write_bytes(b"")
    svc = ScreenerService(tmp_path)
    payload = svc._do_run(_validate_query(query), "filter")

    assert payload["cache_status"] == "fresh"
    items = payload["data"]["items"]
    assert len(items) == 2
    # 受控白名单字段
    from app.screener_service import _RESULT_FIELDS
    for item in items:
        assert set(item) <= set(_RESULT_FIELDS)
    # 原始外层字段 / portfolioPath / expression / date 不得泄漏
    forbidden = {"totalStocks", "stockAmountInUniverse", "portfolioPath", "expression", "date", "stocks", "ChangePCT", "ClosePrice", "code"}
    raw = json.dumps(payload, ensure_ascii=False)
    for key in forbidden:
        assert key not in raw, f"结果快照泄漏字段: {key}"
