# -*- coding: utf-8 -*-
"""Phase F1 第二轮边界修正测试：screener code 有效性、fund_flow flat 形态、
news 身份全丢弃 vs 结构错误、warning 脱敏（不回显原始身份值）。

全部使用 tmp_path / 内联样本，不读取或修改真实 state。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest


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
# 1. screener：存在 code 就必须有效且一致
# ---------------------------------------------------------------------- #
def test_remap_westock_row_valid_symbol_invalid_code_dropped(tmp_path):
    """symbol 合法 + code 存在但非法 → 整行丢弃（存在 code 就必须有效）。"""
    from app.screener_service import ScreenerService

    svc = ScreenerService(tmp_path)
    assert svc._remap_westock_row({"symbol": "600519.SH", "code": "bad-code"}) is None
    assert svc._remap_westock_row({"symbol": "600519.SH", "code": "sh6005"}) is None
    assert svc._remap_westock_row({"symbol": "600519.SH", "code": 123456}) is None


def test_remap_westock_row_symbol_valid_no_code(tmp_path):
    """symbol 合法 + code 不存在 → 保留（不丢弃）。"""
    from app.screener_service import ScreenerService

    svc = ScreenerService(tmp_path)
    out = svc._remap_westock_row({"symbol": "600519.SH", "name": "贵州茅台"})
    assert out is not None and out["symbol"] == "600519.SH"


def test_remap_westock_row_symbol_invalid_always_dropped(tmp_path):
    """symbol 非法 → 无论 code 如何都整行丢弃。"""
    from app.screener_service import ScreenerService

    svc = ScreenerService(tmp_path)
    assert svc._remap_westock_row({"symbol": "not-a-symbol", "code": "sh600519"}) is None
    assert svc._remap_westock_row({"symbol": "not-a-symbol"}) is None


def test_remap_westock_row_symbol_code_same_kept(tmp_path):
    """symbol 与 code 一致 → 保留 symbol 移除 code。"""
    from app.screener_service import ScreenerService

    svc = ScreenerService(tmp_path)
    out = svc._remap_westock_row({"symbol": "600519.SH", "code": "sh600519"})
    assert out is not None and out["symbol"] == "600519.SH" and "code" not in out


def test_remap_westock_row_symbol_code_diff_dropped(tmp_path):
    """symbol 与 code 转换后不同 → 整行丢弃。"""
    from app.screener_service import ScreenerService

    svc = ScreenerService(tmp_path)
    assert svc._remap_westock_row({"symbol": "600519.SH", "code": "sh600000"}) is None


# ---------------------------------------------------------------------- #
# 2. fund_flow：wrapper 与 flat 都要校验身份
# ---------------------------------------------------------------------- #
def test_fund_flow_flat_wrong_code_conflict():
    """fund_flow flat：顶层 code 错配 → 冲突（不因非 wrapper 跳过）。"""
    from app.stocks_deep_service import _fund_flow_identity_conflict

    reason = _fund_flow_identity_conflict({"code": "sh600000", "MainNetFlow": "-100"},
                                          "600519.SH")
    assert reason is not None and "code" in reason
    # 多键 flat 同样校验
    multi = {"a": 1, "code": "sh600000", "MainNetFlow": "-100"}
    assert _fund_flow_identity_conflict(multi, "600519.SH") is not None


def test_fund_flow_flat_correct_code_pass():
    """fund_flow flat：顶层 code 匹配 → 无冲突（交给受控字段标准化）。"""
    from app.stocks_deep_service import _fund_flow_identity_conflict

    flat = {"code": "sh600519", "MainNetFlow": "-309615648", "ClosePrice": "1338.55"}
    assert _fund_flow_identity_conflict(flat, "600519.SH") is None
    # 不含任何身份字段的兼容 flat → 无冲突
    assert _fund_flow_identity_conflict({"MainNetFlow": "-100"}, "600519.SH") is None


def test_fund_flow_wrapper_inner_symbol_mismatch():
    """wrapper inner.symbol 错配 → 冲突。"""
    from app.stocks_deep_service import _fund_flow_identity_conflict

    data = {"sh600519": {"code": "sh600519", "symbol": "sh600000",
                         "data": [{"code": "sh600519"}]}}
    reason = _fund_flow_identity_conflict(data, "600519.SH")
    assert reason is not None and "symbol" in reason


def test_fund_flow_row_symbol_mismatch():
    """wrapper 首行 symbol 错配 → 冲突。"""
    from app.stocks_deep_service import _fund_flow_identity_conflict

    data = {"sh600519": {"code": "sh600519",
                         "data": [{"code": "sh600519", "symbol": "sz000001"}]}}
    reason = _fund_flow_identity_conflict(data, "600519.SH")
    assert reason is not None and "symbol" in reason


def test_fund_flow_secucode_pass_variants():
    """SecuCode：纯六位 600519 与带前缀 sh600519 均通过。"""
    from app.stocks_deep_service import _fund_flow_identity_conflict

    plain = {"sh600519": {"code": "sh600519",
                          "data": [{"code": "sh600519", "SecuCode": "600519"}]}}
    assert _fund_flow_identity_conflict(plain, "600519.SH") is None
    prefixed = {"sh600519": {"code": "sh600519",
                             "data": [{"code": "sh600519", "SecuCode": "sh600519"}]}}
    assert _fund_flow_identity_conflict(prefixed, "600519.SH") is None


def test_fund_flow_secucode_market_mismatch():
    """SecuCode 带前缀但市场不同（sz600519 对 600519.SH）→ 必须冲突。"""
    from app.stocks_deep_service import _fund_flow_identity_conflict

    data = {"sh600519": {"code": "sh600519",
                         "data": [{"code": "sh600519", "SecuCode": "sz600519"}]}}
    reason = _fund_flow_identity_conflict(data, "600519.SH")
    assert reason is not None and "SecuCode" in reason


def test_fund_flow_secucode_malformed_conflict():
    """SecuCode 非法格式（非六位/非字符串）→ 冲突。"""
    from app.stocks_deep_service import _fund_flow_identity_conflict

    for bad in ("600519X", "sh60051", "SH600519", 123456, {"a": 1}):
        data = {"sh600519": {"code": "sh600519",
                             "data": [{"code": "sh600519", "SecuCode": bad}]}}
        assert _fund_flow_identity_conflict(data, "600519.SH") is not None, f"{bad!r}"


def test_fund_flow_secucode_digit_mismatch():
    """SecuCode 数字部分不同（000001）→ 冲突。"""
    from app.stocks_deep_service import _fund_flow_identity_conflict

    data = {"sh600519": {"code": "sh600519",
                         "data": [{"code": "sh600519", "SecuCode": "000001"}]}}
    reason = _fund_flow_identity_conflict(data, "600519.SH")
    assert reason is not None and "SecuCode" in reason


# ---------------------------------------------------------------------- #
# 3. news：身份全丢弃 vs 结构错误
# ---------------------------------------------------------------------- #
def test_news_all_non_dict_structure():
    """news 全部非 dict 条目 → structure（非身份问题），不产生身份 warning。"""
    from app.stocks_deep_service import _norm_news_identity_items

    warnings: list[str] = []
    items, reason = _norm_news_identity_items({"data": [1, 2, "x"]}, warnings, "600519.SH")
    assert items is None and reason == "structure"
    assert not any("身份不匹配" in w for w in warnings)


def test_news_empty_array_empty():
    """news 空数组 → empty。"""
    from app.stocks_deep_service import _norm_news_identity_items

    warnings: list[str] = []
    items, reason = _norm_news_identity_items({"data": []}, warnings, "600519.SH")
    assert items is None and reason == "empty"
    assert not any("身份不匹配" in w for w in warnings)


def test_news_mixed_non_dict_match_mismatch():
    """混合：非 dict 不计身份错配；匹配条目保留；错配条目计数并丢弃。"""
    from app.stocks_deep_service import _norm_news_identity_items

    data = {"data": [
        "junk",
        {"id": "1", "symbol": "sh600519", "title": "匹配", "time": "2026-08-04 09:31:09"},
        {"id": "2", "symbol": "sh600000", "title": "错配", "time": "2026-08-04 09:30:00"},
        {"id": "3", "title": "无 symbol", "time": "2026-08-04 09:29:00"},
    ]}
    warnings: list[str] = []
    items, reason = _norm_news_identity_items(data, warnings, "600519.SH")
    assert reason == "ok"
    assert items is not None and len(items) == 2
    titles = [i["title"] for i in items]
    assert "匹配" in titles and "无 symbol" in titles and "错配" not in titles
    assert any("已丢弃 1 条" in w for w in warnings)


def test_news_all_mismatch_identity_dropped():
    """合法 dict 条目全部因 symbol 错配被丢弃 → identity_all_dropped。"""
    from app.stocks_deep_service import _norm_news_identity_items

    data = {"data": [
        {"id": "1", "symbol": "sh600000", "title": "A", "time": "2026-08-04 09:31:09"},
        {"id": "2", "symbol": "sz000001", "title": "B", "time": "2026-08-04 09:30:00"},
    ]}
    warnings: list[str] = []
    items, reason = _norm_news_identity_items(data, warnings, "600519.SH")
    assert items is None and reason == "identity_all_dropped"
    assert any("身份不匹配" in w for w in warnings)


# ---------------------------------------------------------------------- #
# 4. warning 脱敏：原始身份值不进入 warnings/响应
# ---------------------------------------------------------------------- #
LEAK_PATH = r"C:\secret\token-xxx"
LEAK_LONG = "X" * 600
LEAK_URL = "https://evil.example/leak?id=1"


def test_quote_identity_value_not_leaked(tmp_path):
    """quote 身份冲突时，注入值（路径/超长/URL）不得出现在 warnings/响应。"""
    from app.stocks_service import CuratedStocksService

    for leaked in (LEAK_PATH, LEAK_LONG, LEAK_URL):
        _write_envelope(tmp_path, "quote", "600519.SH",
                        {"sh600519": {"code": leaked, "price": 10.0}},
                        "data_quote")
        svc = CuratedStocksService(tmp_path)
        env = svc.snapshot("600519.SH")
        raw = json.dumps(env, ensure_ascii=False)
        assert leaked not in raw, f"quote warning 泄漏: {leaked[:20]}"
        assert any("身份校验失败" in w for w in env["warnings"])


def test_profile_identity_value_not_leaked(tmp_path):
    """profile 身份冲突时，注入值不得出现在 warnings/响应。"""
    from app.stocks_deep_service import StocksDeepService

    for leaked in (LEAK_PATH, LEAK_LONG, LEAK_URL):
        _write_envelope(tmp_path, "profile", "600519.SH",
                        {"code": leaked, "name": "错的公司"}, "data_profile")
        svc = StocksDeepService(tmp_path)
        env = svc.fundamentals("600519.SH")
        raw = json.dumps(env, ensure_ascii=False)
        assert leaked not in raw, f"profile warning 泄漏: {leaked[:20]}"
        assert env["data"]["profile"] is None
        assert any("身份校验失败" in w for w in env["warnings"])


def test_fund_flow_identity_value_not_leaked(tmp_path):
    """fund_flow 身份冲突时，注入值不得出现在 warnings/响应。"""
    from app.stocks_deep_service import StocksDeepService

    for leaked in (LEAK_PATH, LEAK_LONG, LEAK_URL):
        _write_envelope(tmp_path, "fund_flow", "600519.SH",
                        {"sh600519": {"code": "sh600519",
                                      "data": [{"code": "sh600519", "SecuCode": leaked}]}},
                        "data_fund_flow")
        svc = StocksDeepService(tmp_path)
        env = svc.funds("600519.SH")
        raw = json.dumps(env, ensure_ascii=False)
        assert leaked not in raw, f"fund_flow warning 泄漏: {leaked[:20]}"
        assert env["data"]["fund_flow"] is None
        assert any("身份校验失败" in w for w in env["warnings"])


def test_news_identity_value_not_leaked(tmp_path):
    """news 身份错配条目丢弃时，注入值不得出现在 warnings/响应。"""
    from app.stocks_deep_service import StocksDeepService

    _write_envelope(tmp_path, "news", "600519.SH",
                    {"data": [
                        {"id": "1", "symbol": LEAK_PATH, "title": "A",
                         "time": "2026-08-04 09:31:09"},
                        {"id": "2", "symbol": "sh600519", "title": "B",
                         "time": "2026-08-04 09:30:00"},
                    ]},
                    "data_news")
    svc = StocksDeepService(tmp_path)
    env = svc.intel("600519.SH", "news", 20, 0)
    raw = json.dumps(env, ensure_ascii=False)
    assert LEAK_PATH not in raw, "news warning 泄漏"
    assert any("身份不匹配" in w for w in env["warnings"])


def test_warnings_length_limited():
    """身份 warning 每条 ≤400 字符（固定文案天然满足）。"""
    from app.stocks_service import quote_identity_conflict
    from app.stocks_deep_service import (_fund_flow_identity_conflict,
                                         _profile_identity_conflict)

    reasons = [
        quote_identity_conflict({"sh600000": {"price": 1}}, "600519.SH"),
        _profile_identity_conflict({"code": "sh600000"}, "600519.SH"),
        _fund_flow_identity_conflict({"sh600000": {"data": [{}]}}, "600519.SH"),
        _fund_flow_identity_conflict(
            {"sh600519": {"data": [{"SecuCode": "600519X"}]}}, "600519.SH"),
    ]
    for reason in reasons:
        assert reason is not None and len(reason) <= 400
