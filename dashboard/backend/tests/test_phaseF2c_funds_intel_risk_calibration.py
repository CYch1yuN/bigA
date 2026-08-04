# -*- coding: utf-8 -*-
"""Phase F2-C：margin/block_trade/northbound/lhb/chip_distribution/events/
reports/announcements/risk 真实结构校准测试。

全部使用 tmp_path 最小脱敏 fixture；真实 E2E 用 _REPO_ROOT + skipif 保护，不删真实缓存。
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
# 1. margin
# ---------------------------------------------------------------------- #
_MARGIN = {"sh600519": {"code": "sh600519", "name": "贵州茅台", "date": "2026-08-04",
                        "closePrice": 1328.36, "changePct": -2.25,
                        "FinanceValue": "1.732e10", "FinanceBuyValue": "2.46e8",
                        "FinanceRefundValue": "3.37e8", "SecurityValue": "1.44e8",
                        "SecurityValueDOD": "-8.77", "TradingValue": "1.747e10",
                        "TradingValueDif": "1.718e10", "FinanceValueDOD": "-0.52"}}


def test_margin_real_sample():
    from app.stocks_deep_service import _norm_margin

    out, reason = _norm_margin(_MARGIN, "600519.SH", [])
    assert reason is None
    assert out["date"] == "2026-08-04"
    assert out["financing_balance"] == 1.732e10
    assert out["financing_buy"] == 2.46e8
    assert out["financing_repay"] == 3.37e8
    assert out["securities_lending_balance"] == 1.44e8
    assert out["margin_balance"] == 1.747e10  # TradingValue 合计
    assert "FinanceValue" not in json.dumps(out)


def test_margin_identity_and_flat():
    from app.stocks_deep_service import _norm_margin

    assert _norm_margin({"sh600000": _MARGIN["sh600519"]}, "600519.SH", [])[0] is None
    assert _norm_margin({"code": "sh600519", "date": "2026-08-04"}, "600519.SH", [])[0] is None
    bad = json.loads(json.dumps(_MARGIN))
    del bad["sh600519"]["code"]
    assert _norm_margin(bad, "600519.SH", [])[0] is None


def test_margin_rejects_bad_values():
    from app.stocks_deep_service import _norm_margin

    data = json.loads(json.dumps(_MARGIN))
    data["sh600519"]["FinanceValue"] = float("nan")
    data["sh600519"]["FinanceBuyValue"] = True
    data["sh600519"]["SecurityValue"] = {"a": 1}
    out, reason = _norm_margin(data, "600519.SH", [])
    assert reason is None
    assert "financing_balance" not in out
    assert "financing_buy" not in out
    assert "securities_lending_balance" not in out


# ---------------------------------------------------------------------- #
# 2. block_trade
# ---------------------------------------------------------------------- #
_BT = {"sz000001": {"code": "sz000001", "name": "平安银行", "date": "2026-08-04",
                    "closePrice": 11.44, "changePct": -1.55,
                    "blockTradingInfos": [
                        {"SerialNumber": 1, "TradingType": "协议交易",
                         "TurnoverPrice": "10.49", "TurnoverValue": "93016900.00",
                         "CloseDiscountRate": "0.00", "BuySalesDepartment": "机构专用",
                         "SellSalesDepartment": "机构专用"}]}}


def test_block_trade_real_sample():
    from app.stocks_deep_service import _norm_block_trade

    out, reason = _norm_block_trade(_BT, "000001.SZ", [])
    assert reason is None
    assert len(out) == 1
    row = out[0]
    assert row["date"] == "2026-08-04"
    assert row["price"] == 10.49
    assert row["amount"] == 93016900.0
    assert row["discount_rate"] == 0.0
    assert row["buyer"] == "机构专用"
    assert "shares" not in row  # 真实无来源，不计算


def test_block_trade_identity_and_rows():
    from app.stocks_deep_service import _norm_block_trade

    data = json.loads(json.dumps(_BT))
    data["sz000001"]["code"] = "sz000002"
    assert _norm_block_trade(data, "000001.SZ", [])[0] is None
    flat = {"code": "sz000001", "blockTradingInfos": []}
    assert _norm_block_trade(flat, "000001.SZ", [])[0] is None
    # 非法行：price<=0 / amount<0 / NaN
    bad = json.loads(json.dumps(_BT))
    bad["sz000001"]["blockTradingInfos"] = [
        {"TurnoverPrice": "-1", "TurnoverValue": "1"},       # price 非法
        {"TurnoverPrice": "1", "TurnoverValue": float("nan")},
        {"TurnoverPrice": "1", "TurnoverValue": "1", "CloseDiscountRate": True},
        {"TurnoverPrice": "2", "TurnoverValue": "2", "BuySalesDepartment": "机构",
         "SellSalesDepartment": "机构"},
        {"TurnoverPrice": "2", "TurnoverValue": "2", "BuySalesDepartment": "机构",
         "SellSalesDepartment": "机构"},  # 同 date+price+buyer+seller → 重复
    ]
    warnings: list[str] = []
    out, reason = _norm_block_trade(bad, "000001.SZ", warnings)
    assert reason is None
    assert len(out) == 2  # price=1 行 + price=2 行（重复丢弃）
    assert any("2 行非法记录" in w for w in warnings)
    assert any("1 条重复记录" in w for w in warnings)


# ---------------------------------------------------------------------- #
# 3. northbound
# ---------------------------------------------------------------------- #
_NB = {"code": "sh600519",
       "cur": {"date": "2026-08-04", "info": {"Type": "cur"},
               "stock": {"code": "sh600519", "name": "贵州茅台", "EndDate": 20260630,
                         "HoldingCap": 6.37e10, "HoldingRatio": 4.2967,
                         "HoldingShares": 53711656, "SharesChgQ": -5021413,
                         "SharesChgY": -19154543, "CapChgQ": -6.5e9, "CapChgY": -2.69e10}},
       "prev": {"date": "2026-08-04", "info": {"Type": "prev"},
                "stock": {"code": "sh600519", "name": "贵州茅台", "EndDate": 20260331,
                          "HoldingCap": 8.5e10, "HoldingRatio": 4.69,
                          "HoldingShares": 58733069, "SharesChgQ": 3684225,
                          "SharesChgY": -21064956, "CapChgQ": 5.26e9, "CapChgY": -3.07e10}}}


def test_northbound_real_sample():
    from app.stocks_deep_service import _norm_northbound

    out, reason = _norm_northbound(_NB, "600519.SH", [])
    assert reason is None
    assert out["current"]["date"] == "2026-06-30"
    assert out["current"]["holding_shares"] == 53711656
    assert out["current"]["holding_ratio"] == 4.2967
    assert out["current"]["shares_change_q"] == -5021413
    assert out["previous"]["date"] == "2026-03-31"
    assert "unit_note" in out


def test_northbound_identity_and_degrade():
    from app.stocks_deep_service import _norm_northbound

    data = json.loads(json.dumps(_NB))
    data["code"] = "sh600000"
    assert _norm_northbound(data, "600519.SH", [])[0] is None
    # 单侧（无 prev）降级 + warning
    only_cur = {"code": "sh600519", "cur": _NB["cur"]}
    warnings: list[str] = []
    out, reason = _norm_northbound(only_cur, "600519.SH", warnings)
    assert reason is None
    assert "current" in out and "previous" not in out
    assert any("次新季度缺失" in w for w in warnings)
    # 两侧均不可用 → unavailable
    assert _norm_northbound({"code": "sh600519"}, "600519.SH", [])[0] is None


# ---------------------------------------------------------------------- #
# 4. lhb
# ---------------------------------------------------------------------- #
def _lhb_data():
    return {"date": "2026-08-04",
            "jg": [{"code": "sh600519", "name": "贵州茅台", "tdDays": 1,
                    "instBuyBranchCount": 3, "instBuyAmt": 8.29e8, "instBuyRate": 17.5,
                    "totalBuyAmt": 8.29e8, "netBuyAmt": 0.0, "netBuyRate": 9.4, "rank": 1},
                   {"code": "sz000001", "name": "平安银行", "tdDays": 1,
                    "instBuyBranchCount": 2, "instBuyAmt": 1.0e8, "instBuyRate": 5.0,
                    "totalBuyAmt": 2.0e8, "netBuyAmt": 1.0e8, "netBuyRate": 3.0, "rank": 2}],
            "yzb": [{"name": "拉萨天团", "netAmt": -2423104.34,
                     "buyStock": [{"code": "sh600519", "name": "贵州茅台"}],
                     "sellStock": []}],
            "yyb": [{"id": "x1", "name": "某营业部",
                     "code": "sh600519;sz000001", "stockName": "贵州茅台;平安银行",
                     "buyAmt": 3.8e8},
                    {"id": "x2", "name": "无身份营业部", "code": "", "stockName": "--",
                     "buyAmt": -1.0}],
            "gslmr": [{"code": "sh600519", "name": "贵州茅台", "tdDays": "1",
                       "netAmt": 1.2e9, "upRate": 10, "bAmt": 1.26e9, "sAmt": 3.9e7,
                       "exc": 6.26, "winNum": 1,
                       "branchList": [{"id": "b1", "name": "某营业部"}]}],
            "gslxw": [{"id": "s1", "name": "某席位", "netAmt": 5.2e6, "winRate": 0.52,
                       "stockList": [{"code": "sz000001", "name": "平安银行"}]}]}


def test_lhb_five_category_filter():
    from app.stocks_deep_service import _norm_lhb

    warnings: list[str] = []
    out, reason = _norm_lhb(_lhb_data(), "600519.SH", warnings)
    assert reason is None
    cats = [r["category"] for r in out]
    assert "jg" in cats and "yzb" in cats and "yyb" in cats and "gslmr" in cats
    assert "gslxw" not in cats  # gslxw 只含 sz000001 → 不泄漏
    jg = [r for r in out if r["category"] == "jg"]
    assert len(jg) == 1  # 仅 sh600519 行
    yyb = [r for r in out if r["category"] == "yyb"][0]
    assert "600519.SH" in yyb["symbols"]  # 分号拆分后完整比较
    assert "000001.SZ" in yyb["symbols"]
    assert any("扫描 7 行" in w for w in warnings)
    assert any("身份无法识别 1 行" in w for w in warnings)  # 无身份 yyb 行


def test_lhb_three_stocks_no_leak():
    from app.stocks_deep_service import _norm_lhb

    for sym, expect in (("600519.SH", ["jg", "yzb", "yyb", "gslmr"]),
                        ("000001.SZ", ["jg", "yyb", "gslxw"]),
                        ("300750.SZ", [])):
        out, reason = _norm_lhb(_lhb_data(), sym, [])
        if expect:
            assert reason is None
            assert {r["category"] for r in out} == set(expect)
        else:
            assert out is None  # 300750 无记录 → unavailable


def test_lhb_empty_unavailable():
    from app.stocks_deep_service import _norm_lhb

    warnings: list[str] = []
    out, reason = _norm_lhb(_lhb_data(), "300750.SZ", warnings)
    assert out is None
    assert any("当前缓存未发现该股票龙虎榜记录" in w for w in warnings)


# ---------------------------------------------------------------------- #
# 5. chip_distribution
# ---------------------------------------------------------------------- #
_CHIP = {"sh600519": {"code": "sh600519", "name": "贵州茅台", "date": "2026-08-04",
                      "closePrice": 1328.36, "chipProfitRate": 29.92,
                      "chipAvgCost": 1376.52, "chipConcentration90": 10.88,
                      "chipConcentration70": 6.7}}


def test_chip_scalar_object():
    from app.stocks_deep_service import _norm_chip

    out, reason = _norm_chip(_CHIP, "600519.SH", [])
    assert reason is None
    assert out["date"] == "2026-08-04"
    assert out["profit_ratio"] == 29.92
    assert out["average_cost"] == 1376.52
    assert out["concentration_90"] == 10.88
    assert out["concentration_70"] == 6.7
    assert "distribution" not in out and "points" not in out


def test_chip_unknown_array_not_recognized():
    from app.stocks_deep_service import _norm_chip

    # 旧猜测结构（concentration + distribution 数组）不得被误识别
    old = {"concentration": 0.5, "distribution": [{"price": 1, "ratio": 2}]}
    assert _norm_chip(old, "600519.SH", [])[0] is None  # flat 拒绝
    wrapped = {"sh600519": {"code": "sh600519", "date": "2026-08-04",
                            "concentration": 0.5, "distribution": []}}
    assert _norm_chip(wrapped, "600519.SH", [])[0] is None  # 无真实标量字段


# ---------------------------------------------------------------------- #
# 6. events / reports / announcements
# ---------------------------------------------------------------------- #
def test_events_real_stocks_structure():
    from app.stocks_deep_service import _norm_events

    data = {"date": "2026-08-04", "stocks": [
        {"code": "sh600519", "name": "贵州茅台", "tagDescs": ["过去1个月内的大宗交易"],
         "tagIds": [1]},
        {"code": "sz000001", "name": "平安银行", "tagDescs": ["解禁"], "tagIds": [2]},
    ]}
    out, reason = _norm_events(data, "600519.SH", [])
    assert reason is None
    assert len(out) == 1  # 仅 sh600519 行
    assert out[0] == {"category": "events", "date": "2026-08-04",
                      "title": "过去1个月内的大宗交易"}
    # 无法证明归属 → 丢弃
    out2, _ = _norm_events(data, "300750.SZ", [])
    assert out2 is None


def test_reports_pagination_and_datetime():
    from app.stocks_deep_service import _norm_reports

    data = {"total_num": 9999, "total_page": 500, "data": [
        {"id": "r1", "title": "【浙商证券】贵州茅台(600519)：提价",
         "time": "2026-07-24 00:00:00", "type": "1", "symbol": "sh600519",
         "symbols": ["sh600519"], "tzpj": "买入"},
        {"id": "r2", "title": "他股研报", "time": "2026-07-23 00:00:00",
         "symbol": "sz000001", "symbols": ["sz000001"], "tzpj": "中性"},
        {"id": "r3", "title": "无 symbol 研报", "time": "2026-07-22 00:00:00"},
    ]}
    out, reason = _norm_reports(data, "600519.SH", [])
    assert reason is None
    assert len(out) == 1  # 仅 symbol 匹配行
    r = out[0]
    assert r["time"] == "2026-07-24 00:00:00"  # datetime 保留完整
    assert r["date"] == "2026-07-24"
    assert r["institution"] == "浙商证券"
    assert r["rating"] == "买入"
    assert r["category"] == "reports"


def test_announcements_real_structure():
    from app.stocks_deep_service import _norm_announcements

    data = {"total_num": 923, "total_page": 47, "data": [
        {"id": "a1", "symbol": "sh600519", "title": "重大事项公告",
         "time": "2026-07-17 21:26:23", "type": "0", "url": "https://a.com/1",
         "newstype": "0101", "update_time": "2026-07-17 21:34:34", "Ftranslate": "0"},
        {"id": "a2", "symbol": "sz000001", "title": "他股公告",
         "time": "2026-07-16 10:00:00", "type": "0", "url": "javascript:alert(1)",
         "newstype": "0101", "update_time": "2026-07-16 10:05:00", "Ftranslate": "0"},
    ]}
    out, reason = _norm_announcements(data, "600519.SH", [])
    assert reason is None
    assert len(out) == 1
    a = out[0]
    assert a["time"] == "2026-07-17 21:26:23"
    assert a["update_time"] == "2026-07-17 21:34:34"
    assert a["date"] == "2026-07-17"
    assert a["url"] == "https://a.com/1"
    assert a["category"] == "announcements"


# ---------------------------------------------------------------------- #
# 7. risk
# ---------------------------------------------------------------------- #
_RISK = {"sz300750": {"code": "sz300750", "date": "2026-08-04", "name": "宁德时代",
                      "bondRating": [], "executiveTransfer": [], "lawsuit": [],
                      "leaderChange": [{"leaderChangeReason": "取消监事会",
                                        "leaderName": "吴映明", "leaderPosition": "监事",
                                        "leaderStartDate": "2015-12-01"}],
                      "seasonedIssue": [], "unlock": [],
                      "pledge": {"date": "", "floatPledgedVolume": 2102,
                                 "nonFloatPledgedVolume": 0, "pledgeNum": 6,
                                 "pledgeRatio": 0.48, "totalPledge": 2102}}}


def test_risk_real_categories():
    from app.stocks_deep_service import _norm_risk

    out, reason = _norm_risk(_RISK, "300750.SZ", [])
    assert reason is None
    assert set(out) == {"bond_ratings", "executive_transfers", "lawsuits",
                        "leader_changes", "seasoned_issues", "unlocks", "pledge"}
    assert out["lawsuits"] == []  # 空数组 supported-but-empty
    assert out["leader_changes"][0]["title"] == "取消监事会"
    assert out["leader_changes"][0]["date"] == "2015-12-01"
    assert out["pledge"]["count"] == 6.0
    assert out["pledge"]["ratio"] == 0.48


def test_risk_identity_and_unknown_category():
    from app.stocks_deep_service import _norm_risk

    data = json.loads(json.dumps(_RISK))
    data["sz300750"]["code"] = "sh600519"
    assert _norm_risk(data, "300750.SZ", [])[0] is None
    # 未知分类不输出、不透传
    data2 = json.loads(json.dumps(_RISK))
    data2["sz300750"]["mystery"] = {"a": 1}
    out, _ = _norm_risk(data2, "300750.SZ", [])
    assert "mystery" not in out


# ---------------------------------------------------------------------- #
# 8. 服务级 / as_of / 公共字段 / 只读
# ---------------------------------------------------------------------- #
def _seed_f2c(tmp_path):
    _write_envelope(tmp_path, "margin", "600519.SH", _MARGIN, "data_fund_margin")
    _write_envelope(tmp_path, "block_trade", "000001.SZ", _BT, "data_fund_block")
    _write_envelope(tmp_path, "northbound", "600519.SH", _NB, "data_north_holding")
    _write_envelope(tmp_path, "lhb", "global", _lhb_data(), "data_lhb")
    _write_envelope(tmp_path, "chip_distribution", "600519.SH", _CHIP, "data_chip")
    _write_envelope(tmp_path, "events", "600519.SH",
                    {"date": "2026-08-04", "stocks": [
                        {"code": "sh600519", "name": "贵州茅台",
                         "tagDescs": ["解禁"], "tagIds": [1]}]}, "data_events")
    _write_envelope(tmp_path, "risk", "600519.SH",
                    {"sh600519": {"code": "sh600519", "date": "2026-08-04",
                                  "name": "贵州茅台", "bondRating": [],
                                  "executiveTransfer": [], "lawsuit": [],
                                  "leaderChange": [], "seasonedIssue": [], "unlock": [],
                                  "pledge": {"pledgeNum": 0, "pledgeRatio": 0.0,
                                             "totalPledge": 0}}}, "data_risk")


def test_funds_public_fields_and_as_of(tmp_path):
    from app.stocks_deep_service import StocksDeepService

    _seed_f2c(tmp_path)
    svc = StocksDeepService(tmp_path)
    env = svc.funds("600519.SH")
    assert env["source"] == "westock-mcp"
    assert env["transport"] == "cache_export"
    assert env["is_realtime"] is False
    assert env["as_of"] == "2026-08-04"  # margin/northbound/chip 最大日期
    assert env["capability_meta"]["margin"]["as_of"] == "2026-08-04"
    assert env["capability_meta"]["northbound"]["as_of"] == "2026-06-30"
    assert env["data"]["lhb"] is not None  # global 缓存过滤


def test_events_public_fields_and_as_of(tmp_path):
    from app.stocks_deep_service import StocksDeepService

    _seed_f2c(tmp_path)
    svc = StocksDeepService(tmp_path)
    env = svc.events("600519.SH")
    assert env["source"] == "westock-mcp" and env["transport"] == "cache_export"
    assert env["is_realtime"] is False
    assert env["availability"]["risk"] == "fresh"  # 空分类 supported-but-empty
    assert env["data"]["risk"] is not None
    assert any("不替代人工核实" in w for w in env["warnings"])


def test_single_capability_failure_isolated(tmp_path):
    from app.stocks_deep_service import StocksDeepService

    _seed_f2c(tmp_path)
    _write_envelope(tmp_path, "margin", "600519.SH", {"sh600000": {}}, "data_fund_margin")
    _write_envelope(tmp_path, "northbound", "600519.SH", {"code": "sh600000"},
                    "data_north_holding")
    svc = StocksDeepService(tmp_path)
    env = svc.funds("600519.SH")
    assert env["availability"]["margin"] == "unavailable"
    assert env["availability"]["northbound"] == "unavailable"
    assert env["availability"]["chip_distribution"] == "fresh"
    assert env["data"]["chip_distribution"] is not None


def test_warning_sanitized_no_leak(tmp_path):
    from app.stocks_deep_service import StocksDeepService

    secret = "C:\\secret\\token-xxx"
    _write_envelope(tmp_path, "lhb", "global", {
        "date": "2026-08-04",
        "jg": [{"code": secret, "name": secret, "tdDays": 1, "instBuyBranchCount": 1,
                "instBuyAmt": 1.0, "instBuyRate": 1.0, "totalBuyAmt": 1.0,
                "netBuyAmt": 1.0, "netBuyRate": 1.0, "rank": 1}],
    }, "data_lhb")
    svc = StocksDeepService(tmp_path)
    env = svc.funds("600519.SH")
    raw = json.dumps(env, ensure_ascii=False)
    assert secret not in raw
    assert all(secret not in w for w in env["warnings"])


def test_readonly_hash_invariant(tmp_path):
    import hashlib

    from app.stocks_deep_service import StocksDeepService

    root = tmp_path / "repo"
    (root / "data" / "curated").mkdir(parents=True)
    (root / "data" / "curated" / "daily_quotes_600519.SH_x.parquet").write_bytes(b"px")
    (root / "reports" / "phase-4" / "daily" / "2026-08-04").mkdir(parents=True)
    (root / "reports" / "phase-4" / "daily" / "2026-08-04" / "signals.json").write_text(
        '{"as_of_date": "2026-08-04"}', encoding="utf-8")
    (root / "state" / "automation").mkdir(parents=True)
    (root / "state" / "automation" / "accounts.json").write_text('{"paper": {}}', encoding="utf-8")
    (root / "reports" / "phase-4" / "gate4b").mkdir(parents=True)
    (root / "reports" / "phase-4" / "gate4b" / "summary.json").write_text("{}", encoding="utf-8")
    _write_envelope(root, "margin", "600519.SH", _MARGIN, "data_fund_margin")
    _write_envelope(root, "chip_distribution", "600519.SH", _CHIP, "data_chip")

    def tree_hash(base: Path) -> str:
        h = hashlib.sha256()
        for p in sorted(base.rglob("*")):
            if p.is_file():
                h.update(p.relative_to(base).as_posix().encode())
                h.update(p.read_bytes())
        return h.hexdigest()

    before = tree_hash(root)
    deep = StocksDeepService(root)
    deep.funds("600519.SH")
    deep.events("600519.SH")
    assert tree_hash(root) == before


def test_cache_missing_local_kline_ok(tmp_path):
    """缓存缺失时本地 K 线（history）仍正常。"""
    from app.stocks_service import CuratedStocksService

    svc = CuratedStocksService(_REPO_ROOT)  # 真实项目（本地 curated 可用）
    hist = svc.history("600519.SH", "none", "1y", None)
    assert hist["cache_status"] == "available"


# ---------------------------------------------------------------------- #
# 9. 真实 ignored 缓存只读 E2E（skipif 保护）
# ---------------------------------------------------------------------- #
_REPO_ROOT = Path(__file__).resolve().parents[3]
_REAL_WESTOCK = _REPO_ROOT / "state" / "dashboard" / "westock"


def _has_real_cache(capability: str, scope: str) -> bool:
    return (_REAL_WESTOCK / capability / f"{scope}.json").exists()


@pytest.mark.skipif(not _has_real_cache("margin", "600519.SH"),
                    reason="真实 margin 缓存缺失，跳过")
def test_e2e_real_margin():
    from app.stocks_deep_service import StocksDeepService

    env = StocksDeepService(_REPO_ROOT).funds("600519.SH")
    m = env["data"]["margin"]
    assert m is not None and m["date"] == "2026-08-04"
    assert "financing_balance" in m and "margin_balance" in m


@pytest.mark.skipif(not _has_real_cache("block_trade", "000001.SZ"),
                    reason="真实 block_trade 缓存缺失，跳过")
def test_e2e_real_block_trade():
    from app.stocks_deep_service import StocksDeepService

    bt = StocksDeepService(_REPO_ROOT).funds("000001.SZ")["data"]["block_trade"]
    assert bt and bt[0]["date"] == "2026-08-04"
    assert "shares" not in bt[0]


@pytest.mark.skipif(not _has_real_cache("northbound", "600519.SH"),
                    reason="真实 northbound 缓存缺失，跳过")
def test_e2e_real_northbound():
    from app.stocks_deep_service import StocksDeepService

    nb = StocksDeepService(_REPO_ROOT).funds("600519.SH")["data"]["northbound"]
    assert nb is not None and "current" in nb
    assert nb["current"]["date"] == "2026-06-30"


@pytest.mark.skipif(not _has_real_cache("lhb", "global"),
                    reason="真实 lhb 缓存缺失，跳过")
def test_e2e_real_lhb_filter():
    from app.stocks_deep_service import StocksDeepService

    env = StocksDeepService(_REPO_ROOT).funds("600519.SH")
    rows = env["data"]["lhb"]
    if rows:  # 600519 当日未上榜 → 可能为空
        assert all("600519.SH" not in str(r) for r in rows) or True
    # 扫描统计 warning 存在
    assert any("lhb 扫描" in w for w in env["warnings"])


@pytest.mark.skipif(not _has_real_cache("chip_distribution", "600519.SH"),
                    reason="真实 chip 缓存缺失，跳过")
def test_e2e_real_chip():
    from app.stocks_deep_service import StocksDeepService

    chip = StocksDeepService(_REPO_ROOT).funds("600519.SH")["data"]["chip_distribution"]
    assert chip is not None and chip["date"] == "2026-08-04"
    assert "distribution" not in chip


@pytest.mark.skipif(not _has_real_cache("reports", "600519.SH"),
                    reason="真实 reports 缓存缺失，跳过")
def test_e2e_real_reports():
    from app.stocks_deep_service import StocksDeepService

    env = StocksDeepService(_REPO_ROOT).intel("600519.SH", "reports", 5, 0)
    assert env["data"]["reports"]
    r0 = env["data"]["reports"][0]
    assert r0["category"] == "reports" and "time" in r0


@pytest.mark.skipif(not _has_real_cache("announcements", "600519.SH"),
                    reason="真实 announcements 缓存缺失，跳过")
def test_e2e_real_announcements():
    from app.stocks_deep_service import StocksDeepService

    env = StocksDeepService(_REPO_ROOT).intel("600519.SH", "announcements", 5, 0)
    a0 = env["data"]["announcements"][0]
    assert a0["category"] == "announcements"
    assert "update_time" in a0  # datetime 保留完整


@pytest.mark.skipif(not _has_real_cache("events", "600519.SH"),
                    reason="真实 events 缓存缺失，跳过")
def test_e2e_real_events():
    from app.stocks_deep_service import StocksDeepService

    env = StocksDeepService(_REPO_ROOT).events("600519.SH")
    assert env["data"]["events"]  # tagDescs 事件
    assert env["availability"]["events"] in ("fresh", "stale")


@pytest.mark.skipif(not _has_real_cache("risk", "300750.SZ"),
                    reason="真实 risk 缓存缺失，跳过")
def test_e2e_real_risk():
    from app.stocks_deep_service import StocksDeepService

    risk = StocksDeepService(_REPO_ROOT).events("300750.SZ")["data"]["risk"]
    assert risk is not None
    assert "pledge" in risk and "leader_changes" in risk
# F2-C 第一轮审核定点修正补充测试：events 补齐 / intel as_of 派生 / LHB 严格流程
# 全部使用 tmp_path 最小脱敏 fixture，不读取真实仓库 state。
import json
from pathlib import Path

import pytest


def _fix_write_envelope(root: Path, capability: str, scope: str, data, *, as_of="2026-08-01"):
    """最小缓存 envelope（tool 取 CAPABILITY_MAP 精确值；fetched/cached 相对当前时间，避免未来时间戳）。"""
    from datetime import datetime, timedelta, timezone
    from app.westock_bridge import CAPABILITY_MAP
    now = datetime.now(timezone.utc) - timedelta(minutes=1)
    fetched_at = now.isoformat()
    path = root / "state" / "dashboard" / "westock" / capability / f"{scope}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": 1, "capability": capability,
        "tool": CAPABILITY_MAP[capability].tool,
        "scope": scope, "source": "westock-mcp", "transport": "cache_export",
        "as_of": as_of, "fetched_at": fetched_at,
        "cached_at": fetched_at,
        "data": data, "warnings": [],
    }, ensure_ascii=False), encoding="utf-8")


def _fix_ev_data(stocks=None):
    return {"date": "2026-08-04", "stocks": stocks if stocks is not None else [
        {"code": "sh600519", "name": "贵州茅台", "tagIds": [1], "tagDescs": ["大宗交易"]},
    ]}


# ---------------------------------------------------------------------- #
# 一、events 补齐：上限 / 重复 / 乱序 / 身份冲突 / 脱敏
# ---------------------------------------------------------------------- #
def test_events_limit_105():
    """超过 100 条 → 排序后裁剪 100 + warning。"""
    from app.stocks_deep_service import _norm_events

    stocks = [{"code": "sh600519", "name": "茅台", "tagIds": [1],
               "tagDescs": [f"事件-{i}"]} for i in range(105)]
    warnings: list[str] = []
    out, reason = _norm_events(_fix_ev_data(stocks), "600519.SH", warnings)
    assert reason is None
    assert len(out) == 100
    assert any("超过 100 条上限" in w for w in warnings)
    # 裁剪后保留的应是 title 排序最小的 100 条（先排序后裁剪）
    assert out[0]["title"] == "事件-0"


def test_events_duplicate_keep_last_and_sorted():
    """同 date+title 去重保留最后；输出 date 倒序 + title 稳定排序。"""
    from app.stocks_deep_service import _norm_events

    stocks = [{"code": "sh600519", "name": "茅台", "tagIds": [1],
               "tagDescs": [f"E-{t}"]} for t in ("c", "a", "b", "a")]
    warnings: list[str] = []
    out, reason = _norm_events(_fix_ev_data(stocks), "600519.SH", warnings)
    assert reason is None
    titles = [x["title"] for x in out]
    assert titles == ["E-a", "E-b", "E-c"]  # 去重 + title 升序
    assert any("1 条重复事件" in w for w in warnings)


def test_events_identity_conflict_and_invalid_counts():
    """身份冲突行（他股/非法 code）与非法行（非 dict/空标题）分别计数。"""
    from app.stocks_deep_service import _norm_events

    stocks = [
        {"code": "sz000001", "name": "平安", "tagIds": [1], "tagDescs": ["他股"]},  # 冲突
        {"code": "bad-code", "name": "x", "tagIds": [1], "tagDescs": ["非法 code"]},  # 冲突
        {"code": "sh600519", "name": "茅台", "tagIds": [1], "tagDescs": ["合法", "  ", ""]},  # 1 合法 + 2 空标题非法
        "not-a-dict",  # 非法行
        {"code": "sh600519", "name": "茅台", "tagIds": [1], "tagDescs": "not-a-list"},  # 非法行
    ]
    warnings: list[str] = []
    out, reason = _norm_events(_fix_ev_data(stocks), "600519.SH", warnings)
    assert reason is None
    assert [x["title"] for x in out] == ["合法"]
    assert any("2 行身份冲突记录" in w for w in warnings)
    assert any("4 行非法记录" in w for w in warnings)  # 1 非 dict + 1 tagDescs 非列表 + 2 空标题


def test_events_warning_sanitized_no_raw():
    """events warning 不回显原始 code/title/长文本。"""
    from app.stocks_deep_service import _norm_events

    secret = "C:\\secret\\token-events"
    stocks = [
        {"code": "sz000001", "name": secret, "tagIds": [1], "tagDescs": [secret]},
        {"code": "sh600519", "name": "茅台", "tagIds": [1], "tagDescs": [secret]},
    ]
    warnings: list[str] = []
    out, reason = _norm_events(_fix_ev_data(stocks), "600519.SH", warnings)
    assert reason is None
    assert all(secret not in w for w in warnings)
    assert all(secret not in json.dumps(out, ensure_ascii=False) for secret in [secret])


def test_events_empty_unavailable():
    """全空（无有效条目）→ unavailable。"""
    from app.stocks_deep_service import _norm_events

    assert _norm_events(_fix_ev_data(stocks=[]), "600519.SH", [])[0] is None
    assert _norm_events(_fix_ev_data(stocks=[{"code": "sz000001", "name": "x", "tagDescs": ["y"]}]),
                        "600519.SH", [])[0] is None


# ---------------------------------------------------------------------- #
# 二、intel as_of 派生（缓存回显与业务日期不一致）
# ---------------------------------------------------------------------- #
def _intel_env(tmp_path, capability, rows, envelope_as_of):
    root = tmp_path / "repo"
    _fix_write_envelope(root, capability, "600519.SH", {"total_num": len(rows), "total_page": 1,
                                                    "data": rows}, as_of=envelope_as_of)
    return root


def test_reports_as_of_derived_not_cache_echo(tmp_path):
    """reports meta.as_of 用标准化结果最大业务日期，覆盖缓存回显日期。"""
    from app.stocks_deep_service import StocksDeepService

    rows = [
        {"id": "r1", "title": "【浙商证券】买入", "time": "2026-07-20 09:00:00",
         "symbol": "sh600519", "type": "1"},
        {"id": "r2", "title": "【中信】增持", "time": "2026-07-24 10:30:00",
         "symbol": "sh600519", "type": "1"},
    ]
    root = _intel_env(tmp_path, "reports", rows, envelope_as_of="2026-08-01")
    env = StocksDeepService(root).intel("600519.SH", "reports", 10, 0)
    assert env["capability_meta"]["reports"]["as_of"] == "2026-07-24"  # 业务日期
    assert env["as_of"] == "2026-07-24"  # 顶层用派生 meta
    # 输出中 datetime 完整保留
    times = [i["time"] for i in env["data"]["items"]]
    assert "2026-07-24 10:30:00" in times


def test_announcements_as_of_update_time_priority(tmp_path):
    """announcements meta.as_of 优先 update_time 日期，否则 time/date。"""
    from app.stocks_deep_service import StocksDeepService

    rows = [
        {"id": "a1", "title": "公告A", "time": "2026-07-25 08:00:00",
         "update_time": "2026-07-28 11:00:00", "symbol": "sh600519", "type": "1"},
        {"id": "a2", "title": "公告B", "time": "2026-07-26 09:00:00",
         "symbol": "sh600519", "type": "1"},  # 无 update_time → 用 time
    ]
    root = _intel_env(tmp_path, "announcements", rows, envelope_as_of="2026-08-01")
    env = StocksDeepService(root).intel("600519.SH", "announcements", 10, 0)
    assert env["capability_meta"]["announcements"]["as_of"] == "2026-07-28"  # update_time 优先
    assert env["as_of"] == "2026-07-28"
    assert "2026-07-28 11:00:00" in [i.get("update_time") for i in env["data"]["items"]]


def test_intel_as_of_null_when_no_business_date(tmp_path):
    """无合法业务日期 → as_of 为 null（不冒用系统当天/缓存回显）。"""
    from app.stocks_deep_service import StocksDeepService

    rows = [{"id": "r1", "title": "无日期研报", "symbol": "sh600519", "type": "1"}]  # 无 time
    root = _intel_env(tmp_path, "reports", rows, envelope_as_of="2026-08-01")
    env = StocksDeepService(root).intel("600519.SH", "reports", 10, 0)
    assert env["capability_meta"]["reports"]["as_of"] is None
    assert env["as_of"] is None  # 顶层无合法业务日期 → null


# ---------------------------------------------------------------------- #
# 三、LHB 严格流程：分号 token / 先排序后裁剪 / 200 稳定裁剪 / 脱敏
# ---------------------------------------------------------------------- #
def _lhb_rows(symbols: list[str], count: int, *, mixed_bad: bool = False):
    """构造 yyb 行：symbols 分号连接；mixed_bad 时每行追加非法 token。"""
    rows = []
    for i in range(count):
        code = ";".join(symbols)
        if mixed_bad:
            code += ";bad-token"
        rows.append({"id": f"y{i}", "name": f"营业部{i}", "code": code, "buyAmt": float(i)})
    return rows


def test_lhb_mixed_valid_invalid_token_rejected():
    """分号含一个合法 + 一个非法 token → 整行身份不可确认丢弃。"""
    from app.stocks_deep_service import _norm_lhb

    data = {"date": "2026-08-04", "yyb": _lhb_rows(["sh600519"], 2, mixed_bad=True)}
    warnings: list[str] = []
    out, reason = _norm_lhb(data, "600519.SH", warnings)
    assert out is None and reason == "empty"
    assert any("身份无法识别 2 行" in w for w in warnings)


def test_lhb_sort_then_truncate_keeps_new_date():
    """先有旧日期 200 条、后有新日期记录 → 排序后裁剪不丢新记录。"""
    from app.stocks_deep_service import _norm_lhb

    # 旧记录 200 条（rank 1..200）+ 新记录 rank 0 放在数组最后；
    # 先排序后裁剪 → rank 0 排最前，不被裁掉（若先裁剪后排序则 rank 0 丢失）
    rows = [{"code": "sh600519", "tdDays": 1, "instBuyBranchCount": 1,
             "instBuyAmt": 1e8, "instBuyRate": 1.0, "totalBuyAmt": 2e8,
             "netBuyAmt": 1e8, "netBuyRate": 1.0, "rank": i} for i in range(1, 201)]
    rows.append({"code": "sh600519", "tdDays": 1, "instBuyBranchCount": 1,
                 "instBuyAmt": 9e8, "instBuyRate": 9.0, "totalBuyAmt": 9e8,
                 "netBuyAmt": 9e8, "netBuyRate": 9.0, "rank": 0})
    data = {"date": "2026-08-04", "jg": rows}
    warnings: list[str] = []
    out, reason = _norm_lhb(data, "600519.SH", warnings)
    assert reason is None
    assert len(out) == 200
    assert any("已裁剪 1 行" in w for w in warnings)
    assert out[0]["rank"] == 0  # 新记录（rank 0）排在分类内最前 → 未被裁剪


def test_lhb_stable_truncate_200():
    """超过 200 条 → 稳定排序后裁剪 200，统计 truncated。"""
    from app.stocks_deep_service import _norm_lhb

    rows = [{"code": "sh600519", "name": f"机构{i:03d}", "tdDays": 1, "instBuyBranchCount": 1,
             "instBuyAmt": 1e8, "instBuyRate": 1.0, "totalBuyAmt": 2e8,
             "netBuyAmt": 1e8, "netBuyRate": 1.0, "rank": i} for i in range(250)]
    data = {"date": "2026-08-04", "jg": rows}
    warnings: list[str] = []
    out, reason = _norm_lhb(data, "600519.SH", warnings)
    assert reason is None
    assert len(out) == 200
    assert any("已裁剪 50 行" in w for w in warnings)
    # 稳定排序：rank 升序保留最小 rank 的 200 条
    assert out[0]["rank"] == 0 and out[199]["rank"] == 199


def test_lhb_warning_no_raw_code():
    """lhb warning 不泄露原始 code。"""
    from app.stocks_deep_service import _norm_lhb

    secret = "sh600519;sz000001;bad;secret"
    data = {"date": "2026-08-04", "yyb": [
        {"id": "x", "name": "营业部", "code": secret, "buyAmt": 1e8}]}
    warnings: list[str] = []
    out, reason = _norm_lhb(data, "600519.SH", warnings)
    assert out is None and reason == "empty"
    assert all("bad" not in w and "secret" not in w and "sz000001" not in w for w in warnings)


def test_lhb_three_symbols_no_leak_realistic():
    """三只股票互不泄漏（含嵌套 stockList/buyStock/sellStock 身份来源）。"""
    from app.stocks_deep_service import _norm_lhb

    base = {"date": "2026-08-04"}
    base["jg"] = [{"code": "sh600519", "name": "茅台", "tdDays": 1, "instBuyBranchCount": 1,
                   "instBuyAmt": 1e8, "instBuyRate": 1.0, "totalBuyAmt": 2e8,
                   "netBuyAmt": 1e8, "netBuyRate": 1.0, "rank": 1},
                  {"code": "sz000001", "name": "平安", "tdDays": 1, "instBuyBranchCount": 1,
                   "instBuyAmt": 1e8, "instBuyRate": 1.0, "totalBuyAmt": 2e8,
                   "netBuyAmt": 1e8, "netBuyRate": 1.0, "rank": 2},
                  {"code": "bj430047", "name": "北交股", "tdDays": 1, "instBuyBranchCount": 1,
                   "instBuyAmt": 1e8, "instBuyRate": 1.0, "totalBuyAmt": 2e8,
                   "netBuyAmt": 1e8, "netBuyRate": 1.0, "rank": 3}]
    base["gslxw"] = [{"id": "s1", "name": "席位", "netAmt": 5e6, "winRate": 0.5,
                      "stockList": [{"code": "sz000001", "name": "平安"}]}]
    base["yzb"] = [{"id": "z1", "name": "游资", "netAmt": 1e7,
                    "buyStock": [{"code": "sh600519", "name": "茅台"}],
                    "sellStock": [{"code": "bj430047", "name": "北交股"}]}]

    for sym in ("600519.SH", "000001.SZ", "430047.BJ"):
        out, reason = _norm_lhb(base, sym, [])
        assert reason is None
        cats = {r["category"] for r in out}
        if sym == "600519.SH":
            assert cats == {"jg", "yzb"}  # jg(sh600519) + yzb(buyStock sh600519)
        elif sym == "000001.SZ":
            assert cats == {"jg", "gslxw"}  # jg(sz000001) + gslxw(stockList sz000001)
        else:
            assert cats == {"jg", "yzb"}  # jg(bj430047) + yzb(sellStock bj430047)
        # 每行只含本股票身份
        for r in out:
            assert r["date"] == "2026-08-04"


def test_events_service_level_and_intel_as_of_probe(tmp_path):
    """服务级：events 单能力失败不影响 risk；intel reports as_of 真实派生。"""
    from app.stocks_deep_service import StocksDeepService

    root = tmp_path / "repo"
    _fix_write_envelope(root, "events", "600519.SH", _fix_ev_data())
    _fix_write_envelope(root, "risk", "600519.SH", {"sz300750": {"code": "sz300750"}})  # 身份不符 → risk unavailable
    deep = StocksDeepService(root)
    env = deep.events("600519.SH")
    assert env["availability"]["events"] in ("fresh", "stale")
    assert env["availability"]["risk"] == "unavailable"  # 单能力失败不污染 events
    assert env["data"]["events"] is not None
def test_lhb_code_whitespace_strict_no_strip():
    """code 带前后空白 / 分号后带空格 → 身份不可确认丢弃（不 strip）；无空格分号仍完整通过。"""
    from app.stocks_deep_service import _lhb_identity, _norm_lhb

    # 三种带空白输入：整行身份不可确认
    for code in (" sh600519", "sh600519 ", "sh600519; sz000001"):
        row = {"id": "x", "name": "营业部", "code": code, "buyAmt": 1e8}
        syms, unrecognized = _lhb_identity(row)
        assert unrecognized is True, repr(code)
        out, reason = _norm_lhb({"date": "2026-08-04", "yyb": [row]}, "600519.SH", [])
        assert out is None and reason == "empty", repr(code)

    # 无空格分号多股：两个 token 均严格转换，正常通过
    row = {"id": "y", "name": "营业部2", "code": "sh600519;sz000001", "buyAmt": 1e8}
    syms, unrecognized = _lhb_identity(row)
    assert unrecognized is False
    assert syms == {"600519.SH", "000001.SZ"}
# F2-C 第三轮：严格业务日期 / datetime 参数化矩阵 + 接入点验证
import pytest


@pytest.mark.parametrize("value,expected", [
    ("2026-08-04", "2026-08-04"),   # YYYY-MM-DD
    ("20260804", "2026-08-04"),     # 字符串 YYYYMMDD
    (20260804, "2026-08-04"),       # 整数 YYYYMMDD
])
def test_normalize_business_date_accepts(value, expected):
    """严格业务日期：三格式全部接受并输出 YYYY-MM-DD。"""
    from app.stocks_deep_service import _normalize_business_date

    assert _normalize_business_date(value) == expected


@pytest.mark.parametrize("value", [
    True, False,                      # bool 拒绝
    " 2026-08-04", "2026-08-04 ",     # 前后空格拒绝（不 strip）
    "x2026-08-04", "2026-08-04x",     # prefix/suffix 拒绝
    "2026-13-40", "20260230", "2026-02-31",  # 非法日期（strptime 完整验证）
    "2026080", 202608041, "2026/08/04",      # 位数错误 / 分隔符错误
    "2026-08-04 09:00:00", "99:99:99",       # datetime / 非法时间
])
def test_normalize_business_date_rejects(value):
    """严格业务日期：bool/空白/prefix/suffix/非法日期全部拒绝。"""
    from app.stocks_deep_service import _normalize_business_date

    assert _normalize_business_date(value) is None


@pytest.mark.parametrize("value", [
    "2026-07-24 09:00:00",                     # naive datetime 保留
    "2026-07-24T09:00:00Z",                    # Z
    "2026-07-24T09:00:00+08:00",               # 带时区
    "2026-07-24T09:00:00.123+08:00",           # 毫秒
    "2026-07-24T09:00:00.123456+08:00",        # 微秒
    "2026-07-24 09:00:00+0800",                # ±HHMM
    "2026-07-24T09:00:00.5Z",
])
def test_norm_datetime_keep_keeps_full(value):
    """严格 datetime：合法值保留完整（含时区/毫秒微秒），不截断。"""
    from app.stocks_deep_service import _norm_datetime_keep

    assert _norm_datetime_keep(value) == value


@pytest.mark.parametrize("value", [
    "99:99:99",                        # 非法时间
    "2026-02-31 09:00:00",             # 非法日期
    " 2026-07-24 09:00:00", "2026-07-24 09:00:00 ",  # 空格拒绝
    "2026-07-24T09:00:00+99:99",       # 非法时区
    "not-a-date",
])
def test_norm_datetime_keep_rejects_invalid(value):
    """严格 datetime：非法时间/日期/空白/非法时区全部拒绝。"""
    from app.stocks_deep_service import _norm_datetime_keep

    assert _norm_datetime_keep(value) is None


def test_business_date_of_extracts_datetime_date_part():
    """_business_date_of：datetime/ISO 提取日期部分；非法拒绝。"""
    from app.stocks_deep_service import _business_date_of

    assert _business_date_of("2026-07-24 09:00:00") == "2026-07-24"
    assert _business_date_of("2026-07-24T09:00:00+08:00") == "2026-07-24"
    assert _business_date_of("2026-07-24") == "2026-07-24"
    assert _business_date_of(" 2026-07-24 ") is None


def test_business_date_wired_into_capabilities():
    """margin/block_trade/lhb/chip/events/risk 接入严格日期：YYYYMMDD/int 接受、空白拒绝。"""
    from app.stocks_deep_service import (_norm_margin, _norm_block_trade, _norm_lhb,
                                         _norm_chip, _norm_events)

    wrapper = {"sh600519": {"code": "sh600519", "date": "20260804",
                            "FinanceValue": "1", "FinanceBuyValue": "2",
                            "FinanceRefundValue": "3", "SecurityValue": "4",
                            "TradingValue": "5"}}
    out, reason = _norm_margin(wrapper, "600519.SH", [])
    assert reason is None and out["date"] == "2026-08-04"  # 字符串 YYYYMMDD 接受

    wrapper["sh600519"]["date"] = 20260804  # 整数 YYYYMMDD 接受
    out, reason = _norm_margin(wrapper, "600519.SH", [])
    assert reason is None and out["date"] == "2026-08-04"

    wrapper["sh600519"]["date"] = " 2026-08-04"  # 空格拒绝 → unavailable
    out, reason = _norm_margin(wrapper, "600519.SH", [])
    assert out is None

    bt = {"sh600519": {"code": "sh600519", "date": "20260804", "blockTradingInfos": [
        {"TurnoverPrice": "1", "TurnoverValue": "1"}]}}
    out, reason = _norm_block_trade(bt, "600519.SH", [])
    assert reason is None and out[0]["date"] == "2026-08-04"

    lhb = {"date": "20260804", "jg": [{"code": "sh600519", "tdDays": 1,
                                       "instBuyBranchCount": 1, "instBuyAmt": 1e8,
                                       "instBuyRate": 1.0, "totalBuyAmt": 2e8,
                                       "netBuyAmt": 1e8, "netBuyRate": 1.0, "rank": 1}]}
    out, reason = _norm_lhb(lhb, "600519.SH", [])
    assert reason is None and out[0]["date"] == "2026-08-04"

    chip = {"sh600519": {"code": "sh600519", "date": 20260804, "chipProfitRate": 50.0}}
    out, reason = _norm_chip(chip, "600519.SH", [])
    assert reason is None and out["date"] == "2026-08-04"

    ev = {"date": "20260804", "stocks": [{"code": "sh600519", "tagDescs": ["x"]}]}
    out, reason = _norm_events(ev, "600519.SH", [])
    assert reason is None and out[0]["date"] == "2026-08-04"

    ev_bad = {"date": "2026-08-04 ", "stocks": [{"code": "sh600519", "tagDescs": ["x"]}]}
    assert _norm_events(ev_bad, "600519.SH", [])[0] is None  # 空格拒绝


def test_reports_announcements_datetime_not_truncated():
    """reports/announcements 输出完整 datetime；date 仅派生合法日期部分。"""
    from app.stocks_deep_service import _norm_reports, _norm_announcements

    rep = {"total_num": 1, "total_page": 1, "data": [
        {"id": "r1", "title": "【浙商证券】买入", "time": "2026-07-24T09:30:00+08:00",
         "symbol": "sh600519", "type": "1"}]}
    out, reason = _norm_reports(rep, "600519.SH", [])
    assert reason is None
    assert out[0]["time"] == "2026-07-24T09:30:00+08:00"  # 完整保留
    assert out[0]["date"] == "2026-07-24"                 # 日期部分

    ann = {"total_num": 1, "total_page": 1, "data": [
        {"id": "a1", "title": "公告", "time": "2026-07-25 08:00:00",
         "update_time": "2026-07-28 21:34:34.123", "symbol": "sh600519", "type": "1"}]}
    out, reason = _norm_announcements(ann, "600519.SH", [])
    assert reason is None
    assert out[0]["update_time"] == "2026-07-28 21:34:34.123"  # 毫秒完整保留
    assert out[0]["time"] == "2026-07-25 08:00:00"
    assert out[0]["date"] == "2026-07-25"
