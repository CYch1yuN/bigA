# -*- coding: utf-8 -*-
"""Phase F2-B：financials / forecast / shareholders / dividend / buyback 真实结构校准测试。

全部使用 tmp_path 最小脱敏 fixture，不读真实仓库 state、不复制完整响应。
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
# 1. financials
# ---------------------------------------------------------------------- #
def _fin_row(sheet: str, end: str = "2026-03-31", secu: str = "sh600519", **extra) -> dict:
    base = {"SecuCode": secu, "EndDate": end,
            "InfoPublDate": "2026-04-25 00:00:00 +0800 CST"}
    if sheet == "income":
        row = {"OperatingRevenue": "5.4e10", "OperatingCost": "5.5e9",
               "OperatingProfit": "3.8e10", "TotalProfit": "3.8e10",
               "NPParentCompanyOwners": "2.7e10", "BasicEPS": "21.76"}
    elif sheet == "balance":
        row = {"TotalLiability": "3.9e10", "TotalShareholderEquity": "2.8e11",
               "CashEquivalents": "4.9e10", "BillAccReceivable": "3.2e7"}
    else:
        row = {"NetOperateCashFlow": "2.7e10", "NetInvestCashFlow": "2.6e10",
               "NetFinanceCashFlow": "-1.0e9"}
    row.update(base)
    row.update(extra)
    return row


def _fin_data(rows_per_sheet: dict | None = None, code: int = 0):
    rps = rows_per_sheet or {}
    return {"code": code, "msg": "success", "data": {"sh600519": {
        "balance": rps.get("balance", [_fin_row("balance")]),
        "cashflow": rps.get("cashflow", [_fin_row("cashflow")]),
        "income": rps.get("income", [_fin_row("income")]),
    }}}


def test_financials_real_sample_merged():
    """双层包装 + 三表合并 periods + 最新一期兼容 + unit_note + InfoPublDate 规范化。"""
    from app.stocks_deep_service import _norm_financials

    data = _fin_data({"income": [_fin_row("income", "2026-03-31"),
                                 _fin_row("income", "2025-12-31")],
                      "balance": [_fin_row("balance", "2026-03-31")],
                      "cashflow": [_fin_row("cashflow", "2026-03-31")]})
    out, reason = _norm_financials(data, "600519.SH", [])
    assert reason is None
    assert len(out["periods"]) == 2
    assert out["periods"][0]["report_date"] == "2026-03-31"  # 倒序
    assert out["periods"][1]["report_date"] == "2025-12-31"
    assert out["periods"][0]["info_published_at"] == "2026-04-25T00:00:00+08:00"
    assert out["periods"][0]["summary"] == {"report_date": "2026-03-31",
                                            "revenue": 5.4e10, "net_profit": 2.7e10,
                                            "eps": 21.76}
    assert out["periods"][0]["income_statement"]["operating_profit"] == 3.8e10
    assert out["periods"][0]["balance_sheet"]["equity"] == 2.8e11
    assert out["periods"][0]["cash_flow"]["operating_cash_flow"] == 2.7e10
    # 兼容输出最新一期
    assert out["summary"]["report_date"] == "2026-03-31"
    assert out["income_statement"] == out["periods"][0]["income_statement"]
    assert out["unit_note"].startswith("财务金额单位按字段语义推断为元")
    # 不输出原始字段
    raw = json.dumps(out, ensure_ascii=False)
    assert "SecuCode" not in raw and "OperatingRevenue" not in raw and "code" not in raw


def test_financials_status_code_must_be_zero():
    """状态 code 必须严格数值 0；非 0/缺失/字符串 → unavailable。"""
    from app.stocks_deep_service import _norm_financials

    assert _norm_financials(_fin_data(code=1), "600519.SH", [])[0] is None
    data = _fin_data()
    del data["code"]
    assert _norm_financials(data, "600519.SH", [])[0] is None
    assert _norm_financials(_fin_data(code="0"), "600519.SH", [])[0] is None


def test_financials_flat_payload_rejected():
    """data.data 非严格 wrapper（flat）→ unavailable。"""
    from app.stocks_deep_service import _norm_financials

    flat = {"code": 0, "data": {"balance": [_fin_row("balance")]}}
    assert _norm_financials(flat, "600519.SH", [])[0] is None


def test_financials_secucode_invalid_rows_dropped():
    """行 SecuCode 缺失/错配/非法 → 行丢弃 + 计数 warning（不回显）。"""
    from app.stocks_deep_service import _norm_financials

    rows = [_fin_row("income", secu="sh600519"),
            _fin_row("income", "2025-09-30", secu="sh600000"),  # 错配
            _fin_row("income", "2025-06-30", secu="bad"),       # 非法
            _fin_row("income", "2025-03-31", secu="")]          # 缺失
    warnings: list[str] = []
    out, reason = _norm_financials(_fin_data({"income": rows}), "600519.SH", warnings)
    assert reason is None
    assert len(out["periods"]) == 1  # 仅 sh600519 行有效
    assert any("3 行身份不匹配或非法" in w for w in warnings)
    assert all("sh600000" not in w for w in warnings)  # 脱敏


def test_financials_thirteen_periods_trimmed():
    """13 个合法报告期（全为合法日期，不含 13/14 月）→ 合并后裁剪到 12 + warning。"""
    from app.stocks_deep_service import _norm_financials

    rows = [_fin_row("income", f"2025-{m:02d}-15") for m in range(1, 13)]  # 2025-01..12
    rows.append(_fin_row("income", "2026-01-15"))  # 第 13 个合法期
    warnings: list[str] = []
    out, reason = _norm_financials(_fin_data({"income": rows}), "600519.SH", warnings)
    assert reason is None
    assert any("合并后超过 12 期上限" in w for w in warnings)
    assert len(out["periods"]) == 12
    dates = [p["report_date"] for p in out["periods"]]
    assert dates == sorted(dates, reverse=True)  # 倒序


def test_financials_duplicate_enddate_keeps_last():
    """扫描上限内重复 EndDate：使用不同 revenue 断言保留最后一条有效记录。"""
    from app.stocks_deep_service import _norm_financials

    rows = [_fin_row("income", "2026-03-31", **{"OperatingRevenue": "1.0e10"}),
            _fin_row("income", "2026-03-31", **{"OperatingRevenue": "2.0e10"}),
            _fin_row("income", "2025-12-31", **{"OperatingRevenue": "3.0e10"})]
    warnings: list[str] = []
    out, reason = _norm_financials(_fin_data({"income": rows}), "600519.SH", warnings)
    assert reason is None
    assert len(out["periods"]) == 2
    assert out["periods"][0]["report_date"] == "2026-03-31"
    assert out["periods"][0]["summary"]["revenue"] == 2.0e10  # 同期保留最后
    assert out["periods"][1]["summary"]["revenue"] == 3.0e10


def test_financials_invalid_dates_and_published():
    """EndDate 非法 → 行丢弃；InfoPublDate 非法 → 仅丢字段不丢行。"""
    from app.stocks_deep_service import _norm_financials

    bad_pub = {"InfoPublDate": "not-a-date"}
    data = _fin_data({
        "income": [_fin_row("income", end="20260331"),            # 非 YYYY-MM-DD → 行丢弃
                   _fin_row("income", end="2026-13-40"),          # 非法日期 → 行丢弃
                   _fin_row("income", end="2026-03-31", **bad_pub),  # InfoPublDate 非法
                   _fin_row("income", "2025-12-31")],
        "balance": [_fin_row("balance", "2026-03-31", **bad_pub),
                    _fin_row("balance", "2025-12-31")],
        "cashflow": [_fin_row("cashflow", "2026-03-31", **bad_pub),
                     _fin_row("cashflow", "2025-12-31")],
    })
    out, reason = _norm_financials(data, "600519.SH", [])
    assert reason is None
    dates = [p["report_date"] for p in out["periods"]]
    assert dates == ["2026-03-31", "2025-12-31"]
    assert "info_published_at" not in out["periods"][0]  # 全部来源非法 → 字段丢弃
    assert out["periods"][1]["info_published_at"] == "2026-04-25T00:00:00+08:00"


def test_financials_no_valid_period_unavailable():
    """无任何有效 period → unavailable。"""
    from app.stocks_deep_service import _norm_financials

    bad = _fin_data({"income": [_fin_row("income", secu="sh600000")],
                     "balance": [_fin_row("balance", secu="sh600000")],
                     "cashflow": [_fin_row("cashflow", secu="sh600000")]})
    assert _norm_financials(bad, "600519.SH", [])[0] is None
    assert _norm_financials(_fin_data({"income": [], "balance": [], "cashflow": []}),
                            "600519.SH", [])[0] is None


# ---------------------------------------------------------------------- #
# 2. forecast
# ---------------------------------------------------------------------- #
def _forecast_data(**extra):
    data = {"code": "sh600519", "name": "贵州茅台", "targetPrice": 1699.03,
            "forecasts": [
                {"year": 2026, "eps": 67.77, "revenue": 1.78e10, "netProfit": 8.47e9,
                 "pe": 20.05, "pb": 5.17, "ps": 9.52, "revenueYoy": 3.7,
                 "netProfitYoy": 2.91, "institutionCnt": 12},
                {"year": 2027, "eps": 71.51, "revenue": 1.87e10, "netProfit": 8.94e9,
                 "pe": 19.0, "pb": 4.07, "ps": 9.09, "revenueYoy": 4.72,
                 "netProfitYoy": 5.52, "institutionCnt": 0},
            ]}
    data.update(extra)
    return data


def test_forecast_real_sample():
    """真实样本：forecasts 映射 + 最小 year 摘要 + target_price。"""
    from app.stocks_deep_service import _norm_forecast

    out, reason = _norm_forecast(_forecast_data(), "600519.SH", [])
    assert reason is None
    assert len(out["forecasts"]) == 2
    assert out["forecasts"][0] == {
        "year": 2026, "eps": 67.77, "revenue": 1.78e10, "net_profit": 8.47e9,
        "pe": 20.05, "pb": 5.17, "ps": 9.52, "revenue_yoy": 3.7,
        "net_profit_yoy": 2.91, "institution_count": 12.0,
    }
    assert out["report_date"] == "2026"  # 最小 year
    assert out["consensus_eps"] == 67.77
    assert out["target_price"] == pytest.approx(1699.03)
    raw = json.dumps(out, ensure_ascii=False)
    assert "netProfit" not in raw and "code" not in raw and "name" not in raw


def test_forecast_identity_required():
    """顶层 code 必须存在合法一致。"""
    from app.stocks_deep_service import _norm_forecast

    assert _norm_forecast(_forecast_data(code="sh600000"), "600519.SH", [])[0] is None
    data = _forecast_data()
    del data["code"]
    assert _norm_forecast(data, "600519.SH", [])[0] is None


def test_forecast_limit_year_dedupe_sort():
    """>30 裁剪；year 非法行丢弃；同 year 保留最后；按 year 升序。"""
    from app.stocks_deep_service import _norm_forecast

    rows = [{"year": 2026, "eps": 1.0},          # 合法（放前面，确保进前 30）
            {"year": 2026, "eps": 2.0}]          # 同 year 覆盖（保留最后）
    rows += [{"year": 2050 + i, "eps": float(i)} for i in range(35)]
    rows += [{"year": 1999, "eps": 1.0},          # year 非法
             {"year": 2101, "eps": 1.0},          # year 非法
             {"year": "2026", "eps": 1.0}]        # 非整数
    warnings: list[str] = []
    out, reason = _norm_forecast(_forecast_data(forecasts=rows), "600519.SH", warnings)
    assert reason is None
    assert any("超过 30 条上限" in w for w in warnings)
    assert len(out["forecasts"]) == 29  # 30 行输入含 1 个重复 year
    years = [f["year"] for f in out["forecasts"]]
    assert years == sorted(years)
    assert all(2000 <= y <= 2100 for y in years)
    # 2026 保留最后（eps=2.0）且在最前（最小 year）
    assert out["forecasts"][0]["year"] == 2026 and out["forecasts"][0]["eps"] == 2.0


def test_forecast_target_price_invalid():
    """targetPrice <=0/NaN → 丢弃 + warning。"""
    from app.stocks_deep_service import _norm_forecast

    warnings: list[str] = []
    out, reason = _norm_forecast(_forecast_data(targetPrice=-5.0), "600519.SH", warnings)
    assert reason is None and "target_price" not in out
    assert any("targetPrice 非法" in w for w in warnings)
    out2, _ = _norm_forecast(_forecast_data(targetPrice=float("nan")), "600519.SH", [])
    assert "target_price" not in out2


def test_forecast_nothing_valid_unavailable():
    """无有效 forecasts 且无合法 target_price → unavailable。"""
    from app.stocks_deep_service import _norm_forecast

    assert _norm_forecast(_forecast_data(forecasts=[{"year": 1999}], targetPrice=None),
                          "600519.SH", [])[0] is None


# ---------------------------------------------------------------------- #
# 3. shareholders
# ---------------------------------------------------------------------- #
def _sh_data(**extra):
    data = {"sh600519": {"code": "sh600519", "date": "2026-03-31", "name": "贵州茅台",
                         "top10Shareholders": [
                             {"no": 1, "name": "股东一", "holdShares": 680000000,
                              "holdPct": 54.0, "holdChange": 0},
                             {"no": 2, "name": "股东二", "holdShares": 58000000,
                              "holdPct": 4.6, "holdChange": 3684225},
                         ],
                         "top10FloatShareholders": [
                             {"no": 1, "name": "股东一", "holdShares": 680000000,
                              "holdPct": 54.0, "holdChange": 0},
                         ]}}
    data["sh600519"].update(extra)
    return data


def test_shareholders_real_sample():
    """真实样本：date + major/float 列表 + 行映射。"""
    from app.stocks_deep_service import _norm_shareholders

    out, reason = _norm_shareholders(_sh_data(), "600519.SH", [])
    assert reason is None
    assert out["date"] == "2026-03-31"
    assert out["major_shareholders"] == [
        {"rank": 1, "name": "股东一", "shares": 680000000.0, "ratio": 54.0, "change": 0.0},
        {"rank": 2, "name": "股东二", "shares": 58000000.0, "ratio": 4.6, "change": 3684225.0},
    ]
    assert len(out["float_shareholders"]) == 1
    raw = json.dumps(out, ensure_ascii=False)
    assert "holder_count" not in raw and "share_structure" not in raw
    assert "code" not in raw and "holdShares" not in raw


def test_shareholders_identity_and_date_required():
    """严格 wrapper + 内层 code + date 严格。"""
    from app.stocks_deep_service import _norm_shareholders

    assert _norm_shareholders({"sh600000": {"code": "sh600000", "date": "2026-03-31"}},
                              "600519.SH", [])[0] is None          # wrapper 错配
    assert _norm_shareholders({"sh600519": {"date": "2026-03-31"}},
                              "600519.SH", [])[0] is None          # 缺 code
    assert _norm_shareholders(_sh_data(date="20260331"),
                              "600519.SH", [])[0] is None          # 日期非法
    assert _norm_shareholders(_sh_data(date="2026-13-40"),
                              "600519.SH", [])[0] is None
    flat = {"code": "sh600519", "date": "2026-03-31"}
    assert _norm_shareholders(flat, "600519.SH", [])[0] is None    # flat 拒绝


def test_shareholders_list_limit_and_invalid_rows():
    """单列表 >10 裁剪；非法行丢弃 + 计数。"""
    from app.stocks_deep_service import _norm_shareholders

    rows = [{"no": i, "name": f"股东{i}", "holdShares": i * 1000,
             "holdPct": float(i), "holdChange": 0} for i in range(1, 26)]
    rows += [{"no": -1, "name": "非法", "holdShares": 1},      # no 非正
             {"no": 2, "name": "", "holdShares": 1},           # name 空
             {"no": 3, "name": "数值坏", "holdShares": "x"},   # 数值非法
             {"no": 4, "name": "结构坏"}]                      # 结构正常但仅 rank/name
    warnings: list[str] = []
    data = _sh_data(top10Shareholders=rows)
    out, reason = _norm_shareholders(data, "600519.SH", warnings)
    assert reason is None
    assert any("超过 10 条上限" in w for w in warnings)
    assert any("3 行非法记录" in w for w in warnings)
    assert len(out["major_shareholders"]) == 10


def test_shareholders_both_empty_unavailable():
    """两列表均无有效行 → unavailable。"""
    from app.stocks_deep_service import _norm_shareholders

    assert _norm_shareholders(_sh_data(top10Shareholders=[], top10FloatShareholders=[]),
                              "600519.SH", [])[0] is None


# ---------------------------------------------------------------------- #
# 4. dividend
# ---------------------------------------------------------------------- #
def _div_data(**extra):
    data = {"code": "sz000001", "start": "2025-08-04", "end": "2026-08-04",
            "plans": [
                {"bonusShareRatio": "", "cashDiviRMB": "3.60", "dividendFlag": "是",
                 "dividendPlan": "10派3.600元", "dividendType": "有分红",
                 "exDiviDate": "20260612", "procedure": "方案实施", "proposalSn": 1,
                 "reportEndDate": "20251231", "rightRegDate": "20260611",
                 "totalCashDiviComRMB": "6986130551.28", "tranAddShareRatio": ""},
                {"bonusShareRatio": "", "cashDiviRMB": "2.36", "dividendFlag": "是",
                 "dividendPlan": "10派2.360元", "dividendType": "有分红",
                 "exDiviDate": "20251015", "procedure": "方案实施", "proposalSn": 2,
                 "reportEndDate": "20250630", "rightRegDate": "20251014",
                 "totalCashDiviComRMB": "4580000000.00", "tranAddShareRatio": ""},
            ]}
    data.update(extra)
    return data


def test_dividend_real_sample():
    """真实样本：plans 映射 + 倒序排序 + 最新计划兼容 + pay_date 不伪造。"""
    from app.stocks_deep_service import _norm_dividend

    out, reason = _norm_dividend(_div_data(), "000001.SZ", [])
    assert reason is None
    assert len(out["plans"]) == 2
    assert out["plans"][0]["report_date"] == "2025-12-31"  # 倒序
    assert out["plans"][0]["plan"] == "10派3.600元"
    assert out["plans"][0]["ex_date"] == "2026-06-12"
    assert out["plans"][0]["registration_date"] == "2026-06-11"
    assert out["plans"][0]["cash_per_10_shares"] == 3.6
    assert out["plans"][0]["total_cash"] == pytest.approx(6986130551.28)
    assert out["plans"][0]["procedure"] == "方案实施"
    assert out["plans"][0]["dividend_flag"] == "是"
    assert out["plans"][0]["proposal_sn"] == 1
    assert out["plans"][1]["report_date"] == "2025-06-30"
    # 兼容字段 + pay_date 不伪造
    assert out["plan"] == "10派3.600元"
    assert out["ex_date"] == "2026-06-12"
    assert out["registration_date"] == "2026-06-11"
    assert "pay_date" not in out
    keys = set(out) | {k for p in out["plans"] for k in p}
    assert not (keys & {"start", "end", "code", "dividendPlan", "exDiviDate",
                        "rightRegDate", "reportEndDate"})


def test_dividend_identity_required():
    """顶层 code 必须存在合法一致。"""
    from app.stocks_deep_service import _norm_dividend

    assert _norm_dividend(_div_data(code="sh600519"), "000001.SZ", [])[0] is None
    data = _div_data()
    del data["code"]
    assert _norm_dividend(data, "sz000001", [])[0] is None


def test_dividend_invalid_rows_and_sort():
    """非法行（日期非法/proposal_sn 非整数）丢弃；排序稳定。"""
    from app.stocks_deep_service import _norm_dividend

    bad = [{"dividendPlan": "坏日期", "reportEndDate": "2025123", "proposalSn": 1},
           {"dividendPlan": "坏sn", "reportEndDate": "20251231", "proposalSn": "x"},
           {"dividendPlan": "坏结构"}]
    warnings: list[str] = []
    out, reason = _norm_dividend(_div_data(plans=_div_data()["plans"] + bad),
                                 "000001.SZ", warnings)
    assert reason is None
    assert any("3 条非法计划" in w for w in warnings)
    assert len(out["plans"]) == 2
    # 同 report_date 按 proposal_sn 稳定排序
    dup = _div_data(plans=[
        {"dividendPlan": "A", "reportEndDate": "20251231", "proposalSn": 2,
         "exDiviDate": "20260101", "rightRegDate": "20251231"},
        {"dividendPlan": "B", "reportEndDate": "20251231", "proposalSn": 1,
         "exDiviDate": "20260101", "rightRegDate": "20251231"},
    ])
    out2, _ = _norm_dividend(dup, "000001.SZ", [])
    assert [p["proposal_sn"] for p in out2["plans"]] == [1, 2]


def test_dividend_all_invalid_unavailable():
    """全部计划非法或空 → unavailable。"""
    from app.stocks_deep_service import _norm_dividend

    assert _norm_dividend(_div_data(plans=[{"reportEndDate": "bad", "proposalSn": 1}]),
                          "000001.SZ", [])[0] is None
    assert _norm_dividend(_div_data(plans=[]), "000001.SZ", [])[0] is None


# ---------------------------------------------------------------------- #
# 5. buyback：supported-but-empty
# ---------------------------------------------------------------------- #
def test_buyback_no_cache_supported_but_empty(tmp_path):
    """无 buyback 缓存 → unavailable + data 为 null + 固定 warning。"""
    from app.stocks_deep_service import StocksDeepService

    svc = StocksDeepService(tmp_path)
    env = svc.ownership("600519.SH")
    assert env["availability"]["buyback"] == "unavailable"
    assert env["data"]["buyback"] is None
    assert any("当前缓存未包含回购记录" in w for w in env["warnings"])


def test_buyback_empty_data_supported_but_empty(tmp_path):
    """空对象/空列表 data → supported-but-empty。"""
    from app.stocks_deep_service import StocksDeepService

    for payload in ({}, {"ok": True}, []):
        _write_envelope(tmp_path, "buyback", "600519.SH", payload, "data_buyback")
        svc = StocksDeepService(tmp_path)
        env = svc.ownership("600519.SH")
        assert env["availability"]["buyback"] == "unavailable"
        assert env["data"]["buyback"] is None
        assert any("当前缓存未包含回购记录" in w for w in env["warnings"])


def test_buyback_whitelist_data(tmp_path):
    """有 data 时仅输出白名单字段。"""
    from app.stocks_deep_service import StocksDeepService

    _write_envelope(tmp_path, "buyback", "600519.SH",
                    {"status": "进行中", "price_range": "1500-1800", "amount": 1.5e10,
                     "hacked": "x"}, "data_buyback")
    svc = StocksDeepService(tmp_path)
    env = svc.ownership("600519.SH")
    assert env["availability"]["buyback"] == "fresh"
    assert env["data"]["buyback"] == {"status": "进行中", "price_range": "1500-1800",
                                      "amount": 1.5e10}
    assert "hacked" not in env["data"]["buyback"]


# ---------------------------------------------------------------------- #
# 6. 服务级：单项失败不影响同聚合 API
# ---------------------------------------------------------------------- #
def test_aggregate_isolated_failures(tmp_path):
    """forecast 坏 → financials 仍可用；buyback 空不影响 shareholders/dividend。"""
    from app.stocks_deep_service import StocksDeepService

    _write_envelope(tmp_path, "financials", "600519.SH",
                    _fin_data(), "data_finance")
    _write_envelope(tmp_path, "forecast", "600519.SH",
                    {"unknown_shape": 1}, "data_consensus")
    _write_envelope(tmp_path, "shareholders", "600519.SH",
                    _sh_data(), "data_shareholder")
    _write_envelope(tmp_path, "dividend", "000001.SZ",
                    _div_data(), "data_dividend")
    svc = StocksDeepService(tmp_path)
    env = svc.fundamentals("600519.SH")
    assert env["availability"]["financials"] == "fresh"
    assert env["availability"]["forecast"] == "unavailable"
    assert env["data"]["financials"] is not None
    oenv = svc.ownership("000001.SZ")
    assert oenv["availability"]["shareholders"] in ("fresh", "unavailable")
    assert oenv["availability"]["dividend"] == "fresh"
    assert oenv["availability"]["buyback"] == "unavailable"
    assert oenv["data"]["dividend"]["plans"]


# ---------------------------------------------------------------------- #
# 7. F2-B 扩充覆盖：financials 边界补齐
# ---------------------------------------------------------------------- #
def test_financials_outer_key_mismatch():
    """data.data 外层 key 错配 → unavailable。"""
    from app.stocks_deep_service import _norm_financials

    data = {"code": 0, "data": {"sh600000": {"income": [_fin_row("income")]}}}
    assert _norm_financials(data, "600519.SH", [])[0] is None


def test_financials_three_periods_merged():
    """三表 3 期合并：各期 summary/income/balance/cash_flow 正确对应。"""
    from app.stocks_deep_service import _norm_financials

    data = _fin_data({"income": [_fin_row("income", "2026-03-31"),
                                 _fin_row("income", "2025-12-31"),
                                 _fin_row("income", "2025-09-30")],
                      "balance": [_fin_row("balance", "2026-03-31"),
                                  _fin_row("balance", "2025-12-31"),
                                  _fin_row("balance", "2025-09-30")],
                      "cashflow": [_fin_row("cashflow", "2026-03-31"),
                                   _fin_row("cashflow", "2025-12-31"),
                                   _fin_row("cashflow", "2025-09-30")]})
    out, reason = _norm_financials(data, "600519.SH", [])
    assert reason is None
    assert len(out["periods"]) == 3
    assert [p["report_date"] for p in out["periods"]] == ["2026-03-31", "2025-12-31",
                                                          "2025-09-30"]  # 倒序
    for p in out["periods"]:
        assert "summary" in p and "income_statement" in p
        assert "balance_sheet" in p and "cash_flow" in p


def test_financials_rejects_bool_nan_inf_dict_list():
    """科目值 bool/NaN/Infinity/dict/list → 丢弃。"""
    from app.stocks_deep_service import _norm_financials

    row = _fin_row("income", **{"OperatingRevenue": True, "OperatingCost": float("nan"),
                                "OperatingProfit": float("inf"), "TotalProfit": {"a": 1},
                                "NPParentCompanyOwners": [1.0], "BasicEPS": "9.9"})
    out, reason = _norm_financials(_fin_data({"income": [row]}), "600519.SH", [])
    assert reason is None
    inc = out["periods"][0]["income_statement"]
    assert "revenue" not in inc and "cost" not in inc
    assert "operating_profit" not in inc and "total_profit" not in inc
    assert "net_profit" not in inc
    assert inc == {"eps": 9.9}  # 仅 BasicEPS 合法


def test_financials_no_fabricated_assets_or_net_flow():
    """不伪造 total_assets / net_cash_flow：源含 TotalAssets/NetCashFlowTTM 也不输出。"""
    from app.stocks_deep_service import _norm_financials

    bal = _fin_row("balance", **{"TotalAssets": "9.9e11"})
    cf = _fin_row("cashflow", **{"NetCashFlowTTM": "1.2e10"})
    out, reason = _norm_financials(_fin_data({"balance": [bal], "cashflow": [cf]}),
                                   "600519.SH", [])
    assert reason is None
    bs = out["periods"][0]["balance_sheet"]
    cfout = out["periods"][0]["cash_flow"]
    assert "total_assets" not in bs
    assert "net_cash_flow" not in cfout
    # 合法白名单仍输出
    assert bs["equity"] == 2.8e11
    assert cfout["operating_cash_flow"] == 2.7e10


def test_financials_bills_and_accounts_receivable():
    """bills_and_accounts_receivable ← BillAccReceivable（不误标为纯应收）。"""
    from app.stocks_deep_service import _norm_financials

    bal = _fin_row("balance", **{"BillAccReceivable": "3.2e7"})
    out, reason = _norm_financials(_fin_data({"balance": [bal]}), "600519.SH", [])
    assert reason is None
    bs = out["periods"][0]["balance_sheet"]
    assert bs["bills_and_accounts_receivable"] == 3.2e7
    assert "accounts_receivable" not in bs  # 不输出纯应收误标


# ---------------------------------------------------------------------- #
# 8. F2-B 扩充覆盖：forecast 边界补齐
# ---------------------------------------------------------------------- #
def test_forecast_code_invalid():
    """顶层 code 非法（无法解析）→ unavailable。"""
    from app.stocks_deep_service import _norm_forecast

    assert _norm_forecast(_forecast_data(code="bad-code"), "600519.SH", [])[0] is None


def test_forecast_unknown_fields_not_forwarded():
    """行内未知字段不透传。"""
    from app.stocks_deep_service import _norm_forecast

    data = _forecast_data(forecasts=[{"year": 2026, "eps": 1.0, "hacked": "x",
                                      "odd": {"deep": 1}}])
    out, reason = _norm_forecast(data, "600519.SH", [])
    assert reason is None
    assert set(out["forecasts"][0]) == {"year", "eps"}
    raw = json.dumps(out, ensure_ascii=False)
    assert "hacked" not in raw and "odd" not in raw


def test_forecast_warning_sanitized():
    """forecast warning 不回显注入值。"""
    from app.stocks_deep_service import _norm_forecast

    secret = "C:\\secret\\token-xxx"
    warnings: list[str] = []
    out, _ = _norm_forecast(_forecast_data(targetPrice=float("nan"),
                                           code=secret), "600519.SH", warnings)
    # code 含 secret 但 identity 校验失败走 reason，不进入 warning 文本
    if out is not None:
        assert all(secret not in w for w in warnings)
    out2, _ = _norm_forecast(_forecast_data(targetPrice=-1.0), "600519.SH", warnings)
    assert all(secret not in w for w in warnings)


# ---------------------------------------------------------------------- #
# 9. F2-B 扩充覆盖：shareholders 边界补齐
# ---------------------------------------------------------------------- #
def test_shareholders_lists_not_cross_contaminated():
    """major/float 两列表内容互不交叉。"""
    from app.stocks_deep_service import _norm_shareholders

    data = _sh_data()
    data["sh600519"]["top10Shareholders"] = [
        {"no": 1, "name": "大股东A", "holdShares": 1, "holdPct": 50.0, "holdChange": 0}]
    data["sh600519"]["top10FloatShareholders"] = [
        {"no": 1, "name": "流通股东B", "holdShares": 2, "holdPct": 10.0, "holdChange": 1}]
    out, reason = _norm_shareholders(data, "600519.SH", [])
    assert reason is None
    assert [r["name"] for r in out["major_shareholders"]] == ["大股东A"]
    assert [r["name"] for r in out["float_shareholders"]] == ["流通股东B"]


def test_shareholders_warning_sanitized():
    """shareholders warning 不回显原始身份值。"""
    from app.stocks_deep_service import _norm_shareholders

    secret = "C:\\secret\\token-xxx"
    warnings: list[str] = []
    data = _sh_data(top10Shareholders=[
        {"no": -1, "name": secret, "holdShares": 1}])
    out, reason = _norm_shareholders(data, "600519.SH", warnings)
    assert reason is None
    assert all(secret not in w for w in warnings)
    assert all(secret not in json.dumps(out, ensure_ascii=False) for _ in [1])


# ---------------------------------------------------------------------- #
# 10. F2-B 扩充覆盖：dividend 边界补齐
# ---------------------------------------------------------------------- #
def test_dividend_limit_100():
    """plans 超过 100 裁剪 + warning。"""
    from app.stocks_deep_service import _norm_dividend

    rows = [{"dividendPlan": f"P{i}",
             "reportEndDate": f"20{20 + i // 12:02d}{i % 12 + 1:02d}15",  # 每月 15 日保证合法
             "proposalSn": i} for i in range(110)]
    warnings: list[str] = []
    out, reason = _norm_dividend(_div_data(plans=rows), "000001.SZ", warnings)
    assert reason is None
    assert any("超过 100 条上限" in w for w in warnings)
    assert len(out["plans"]) == 100


def test_dividend_warning_sanitized():
    """dividend warning 不回显原始值。"""
    from app.stocks_deep_service import _norm_dividend

    secret = "C:\\secret\\token-xxx"
    warnings: list[str] = []
    out, _ = _norm_dividend(_div_data(plans=[{"dividendPlan": secret,
                                              "reportEndDate": "bad", "proposalSn": 1}]),
                            "000001.SZ", warnings)
    assert out is None  # 全部非法 → unavailable
    assert all(secret not in w for w in warnings)


# ---------------------------------------------------------------------- #
# 11. F2-B 扩充覆盖：buyback 语义
# ---------------------------------------------------------------------- #
def test_buyback_unavailable_not_unsupported(tmp_path):
    """buyback 空 → unavailable 但不标记 unsupported、不伪造字段。"""
    from app.stocks_deep_service import StocksDeepService

    _write_envelope(tmp_path, "buyback", "600519.SH", {"ok": True}, "data_buyback")
    svc = StocksDeepService(tmp_path)
    env = svc.ownership("600519.SH")
    assert env["availability"]["buyback"] == "unavailable"
    assert env["data"]["buyback"] is None
    raw = json.dumps(env, ensure_ascii=False)
    assert "unsupported" not in raw
    assert any("当前缓存未包含回购记录" in w for w in env["warnings"])


def test_buyback_empty_does_not_affect_others(tmp_path):
    """buyback 空不影响同聚合 API 的 shareholders/dividend。"""
    from app.stocks_deep_service import StocksDeepService

    _write_envelope(tmp_path, "shareholders", "600519.SH", _sh_data(), "data_shareholder")
    _write_envelope(tmp_path, "dividend", "000001.SZ", _div_data(), "data_dividend")
    _write_envelope(tmp_path, "buyback", "600519.SH", {}, "data_buyback")
    svc = StocksDeepService(tmp_path)
    env = svc.ownership("000001.SZ")
    assert env["availability"]["shareholders"] in ("fresh", "unavailable")
    assert env["availability"]["dividend"] == "fresh"
    assert env["availability"]["buyback"] == "unavailable"
    assert env["data"]["dividend"]["plans"]


# ---------------------------------------------------------------------- #
# 12. 服务级：隔离降级 / 只读 / 公开字段
# ---------------------------------------------------------------------- #
def test_fundamentals_financials_failure_isolated(tmp_path):
    """financials 坏 → profile/forecast 仍可用。"""
    from app.stocks_deep_service import StocksDeepService

    _write_envelope(tmp_path, "profile", "600519.SH",
                    {"name": "贵州茅台"}, "data_profile")
    _write_envelope(tmp_path, "financials", "600519.SH",
                    {"code": 1, "data": {}}, "data_finance")
    _write_envelope(tmp_path, "forecast", "600519.SH",
                    _forecast_data(), "data_consensus")
    svc = StocksDeepService(tmp_path)
    env = svc.fundamentals("600519.SH")
    assert env["availability"]["profile"] == "fresh"
    assert env["availability"]["financials"] == "unavailable"
    assert env["availability"]["forecast"] == "fresh"
    assert env["data"]["financials"] is None
    assert env["data"]["forecast"] is not None


def test_ownership_independent_degradation(tmp_path):
    """ownership 三能力独立降级：shareholders 坏不影响 dividend。"""
    from app.stocks_deep_service import StocksDeepService

    _write_envelope(tmp_path, "shareholders", "600519.SH",
                    {"sh600519": {"code": "sh600000", "date": "2026-03-31"}},
                    "data_shareholder")  # code 错配 → unavailable
    _write_envelope(tmp_path, "dividend", "000001.SZ", _div_data(), "data_dividend")
    svc = StocksDeepService(tmp_path)
    env = svc.ownership("000001.SZ")
    assert env["availability"]["shareholders"] == "unavailable"
    assert env["availability"]["dividend"] == "fresh"
    assert env["data"]["dividend"]["plans"]


def test_deep_readonly_hash_invariant(tmp_path):
    """调用前后 curated/signals/orders/accounts/Gate4B 哈希不变（只读）。"""
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
    _write_envelope(root, "financials", "600519.SH", _fin_data(), "data_finance")
    _write_envelope(root, "forecast", "600519.SH", _forecast_data(), "data_consensus")
    _write_envelope(root, "shareholders", "600519.SH", _sh_data(), "data_shareholder")

    def tree_hash(base: Path) -> str:
        h = hashlib.sha256()
        for p in sorted(base.rglob("*")):
            if p.is_file():
                h.update(p.relative_to(base).as_posix().encode())
                h.update(p.read_bytes())
        return h.hexdigest()

    before = tree_hash(root)
    deep = StocksDeepService(root)
    deep.fundamentals("600519.SH")
    deep.ownership("600519.SH")
    assert tree_hash(root) == before


def test_deep_api_public_fields(tmp_path):
    """fundamentals/ownership 公开字段：source/transport/is_realtime。"""
    from app.stocks_deep_service import StocksDeepService

    _write_envelope(tmp_path, "financials", "600519.SH", _fin_data(), "data_finance")
    _write_envelope(tmp_path, "forecast", "600519.SH", _forecast_data(), "data_consensus")
    _write_envelope(tmp_path, "shareholders", "600519.SH", _sh_data(), "data_shareholder")
    svc = StocksDeepService(tmp_path)
    for method in ("fundamentals", "ownership"):
        env = getattr(svc, method)("600519.SH")
        assert env["source"] == "westock-mcp"
        assert env["transport"] == "cache_export"
        assert env["is_realtime"] is False


# ---------------------------------------------------------------------- #
# 13. 真实 ignored 缓存只读 E2E（skipif 保护，不物理删除缓存）
# ---------------------------------------------------------------------- #
_REPO_ROOT = Path(__file__).resolve().parents[3]  # tests -> backend -> dashboard -> 仓库根
_REAL_WESTOCK = _REPO_ROOT / "state" / "dashboard" / "westock"


def _has_real_cache(capability: str, scope: str) -> bool:
    return (_REAL_WESTOCK / capability / f"{scope}.json").exists()


@pytest.mark.skipif(not _has_real_cache("financials", "600519.SH"),
                    reason="真实 financials 缓存缺失，跳过 E2E")
def test_e2e_real_financials():
    """真实 financials 缓存只读 E2E：periods=3 + 最新一期 summary。"""
    from app.stocks_deep_service import StocksDeepService

    env = StocksDeepService(_REPO_ROOT).fundamentals("600519.SH")
    fin = env["data"]["financials"]
    assert fin is not None
    assert len(fin["periods"]) == 3
    assert [p["report_date"] for p in fin["periods"]] == \
        ["2026-03-31", "2025-12-31", "2025-09-30"]  # 倒序
    assert fin["summary"]["report_date"] == "2026-03-31"
    assert fin["summary"]["eps"] == pytest.approx(21.76)
    assert "unit_note" in fin


@pytest.mark.skipif(not _has_real_cache("forecast", "600519.SH"),
                    reason="真实 forecast 缓存缺失，跳过 E2E")
def test_e2e_real_forecast():
    """真实 forecast 缓存只读 E2E：forecasts=3、年度升序。"""
    from app.stocks_deep_service import StocksDeepService

    fc = StocksDeepService(_REPO_ROOT).fundamentals("600519.SH")["data"]["forecast"]
    assert fc is not None
    assert len(fc["forecasts"]) == 3
    years = [f["year"] for f in fc["forecasts"]]
    assert years == sorted(years)
    assert fc["report_date"] == str(min(years))


@pytest.mark.skipif(not _has_real_cache("shareholders", "600519.SH"),
                    reason="真实 shareholders 缓存缺失，跳过 E2E")
def test_e2e_real_shareholders():
    """真实 shareholders 缓存只读 E2E：两列表各 10。"""
    from app.stocks_deep_service import StocksDeepService

    sh = StocksDeepService(_REPO_ROOT).ownership("600519.SH")["data"]["shareholders"]
    assert sh is not None
    assert len(sh["major_shareholders"]) == 10
    assert len(sh["float_shareholders"]) == 10
    assert all(r["rank"] >= 1 for r in sh["major_shareholders"])


@pytest.mark.skipif(not _has_real_cache("dividend", "000001.SZ"),
                    reason="真实 dividend 缓存缺失，跳过 E2E")
def test_e2e_real_dividend():
    """真实 dividend 缓存只读 E2E：plans=2。"""
    from app.stocks_deep_service import StocksDeepService

    dv = StocksDeepService(_REPO_ROOT).ownership("000001.SZ")["data"]["dividend"]
    assert dv is not None
    assert len(dv["plans"]) == 2
    assert dv["plan"]  # 最新计划兼容字段
    assert "pay_date" not in dv


@pytest.mark.skipif(not _has_real_cache("shareholders", "600519.SH"),
                    reason="真实缓存缺失，跳过 E2E")
def test_e2e_real_buyback_empty_not_breaking():
    """真实 buyback（无缓存）→ unavailable 且不影响 ownership 其他项。"""
    from app.stocks_deep_service import StocksDeepService

    env = StocksDeepService(_REPO_ROOT).ownership("600519.SH")
    assert env["availability"]["buyback"] == "unavailable"
    assert env["data"]["buyback"] is None
    assert any("当前缓存未包含回购记录" in w for w in env["warnings"])
    assert env["availability"]["shareholders"] in ("fresh", "stale")


# ---------------------------------------------------------------------- #
# 14. F2-B 第一轮审核定点修正回归
# ---------------------------------------------------------------------- #
def test_meta_as_of_derived(tmp_path):
    """成功标准化后 capability meta.as_of 派生：financials/shareholders/dividend；
    forecast 与 buyback empty 不设置。"""
    from app.stocks_deep_service import StocksDeepService

    _write_envelope(tmp_path, "financials", "600519.SH", _fin_data(), "data_finance")
    _write_envelope(tmp_path, "forecast", "600519.SH", _forecast_data(), "data_consensus")
    _write_envelope(tmp_path, "shareholders", "600519.SH", _sh_data(), "data_shareholder")
    _write_envelope(tmp_path, "dividend", "000001.SZ", _div_data(), "data_dividend")
    svc = StocksDeepService(tmp_path)

    fenv = svc.fundamentals("600519.SH")
    assert fenv["capability_meta"]["financials"]["as_of"] == "2026-03-31"
    assert fenv["capability_meta"]["forecast"]["as_of"] is None  # year 不作 as_of
    assert fenv["as_of"] == "2026-03-31"  # 聚合顶层取最大合法日期

    oenv = svc.ownership("600519.SH")
    assert oenv["capability_meta"]["shareholders"]["as_of"] == "2026-03-31"
    assert oenv["as_of"] == "2026-03-31"

    dvenv = svc.ownership("000001.SZ")
    assert dvenv["capability_meta"]["dividend"]["as_of"] == "2025-12-31"  # 最新 report_date
    assert dvenv["as_of"] == "2025-12-31"
    # buyback 无缓存 → meta None，不设置 as_of
    assert dvenv["capability_meta"]["buyback"] is None


def test_forecast_year_only_rejected():
    """year-only 行（无任何指标）→ 行丢弃；全部 year-only → unavailable。"""
    from app.stocks_deep_service import _norm_forecast

    out, reason = _norm_forecast({"code": "sh600519",
                                  "forecasts": [{"year": 2026}]}, "600519.SH", [])
    assert out is None and reason == "缺少受控预期字段"
    # 混合：一条 year-only + 一条有效 → 仅有效保留
    data = {"code": "sh600519", "forecasts": [{"year": 2026},
                                              {"year": 2027, "eps": 1.0}]}
    out2, reason2 = _norm_forecast(data, "600519.SH", [])
    assert reason2 is None
    assert [f["year"] for f in out2["forecasts"]] == [2027]


def test_forecast_institution_count_int():
    """institutionCnt 必须非负整数，输出 int（不做 float）。"""
    from app.stocks_deep_service import _norm_forecast

    data = {"code": "sh600519", "forecasts": [
        {"year": 2026, "eps": 1.0, "institutionCnt": 12},
        {"year": 2027, "eps": 2.0, "institutionCnt": -1},   # 负 → 不输出
        {"year": 2028, "eps": 3.0, "institutionCnt": True},  # bool → 不输出
        {"year": 2029, "eps": 4.0, "institutionCnt": 0},     # 0 合法
    ]}
    out, reason = _norm_forecast(data, "600519.SH", [])
    assert reason is None
    by_year = {f["year"]: f for f in out["forecasts"]}
    assert by_year[2026]["institution_count"] == 12
    assert isinstance(by_year[2026]["institution_count"], int)
    assert "institution_count" not in by_year[2027]
    assert "institution_count" not in by_year[2028]
    assert by_year[2029]["institution_count"] == 0


def test_forecast_invalid_duplicate_counts_sanitized():
    """非法行/重复年度计数 warning；脱敏不回显原始内容。"""
    from app.stocks_deep_service import _norm_forecast

    secret = "C:\\secret\\token-xxx"
    warnings: list[str] = []
    data = {"code": "sh600519", "forecasts": [
        {"year": 2026, "eps": 1.0},
        {"year": 2026, "eps": 2.0},          # 重复 year
        {"year": 2027},                      # year-only → 非法
        {"year": secret, "eps": 1.0},        # year 非法
    ]}
    out, reason = _norm_forecast(data, "600519.SH", warnings)
    assert reason is None
    assert len(out["forecasts"]) == 1
    assert any("2 行非法记录" in w for w in warnings)
    assert any("1 个重复年度" in w for w in warnings)
    assert all(secret not in w for w in warnings)


def test_dividend_empty_shell_rejected():
    """空壳计划（仅 report_date/proposal_sn）→ 丢弃并计数；全空壳 → unavailable。"""
    from app.stocks_deep_service import _norm_dividend

    warnings: list[str] = []
    data = {"code": "sz000001", "plans": [
        {"reportEndDate": "20251231", "proposalSn": 1},        # 空壳
        {"reportEndDate": "20250630", "proposalSn": 1,
         "dividendPlan": "10派2元"},                           # 有效
    ]}
    out, reason = _norm_dividend(data, "000001.SZ", warnings)
    assert reason is None
    assert any("1 条非法计划" in w for w in warnings)
    assert len(out["plans"]) == 1
    assert out["plans"][0]["plan"] == "10派2元"
    # 全空壳 → unavailable
    shell = {"code": "sz000001", "plans": [{"reportEndDate": "20251231", "proposalSn": 1}]}
    out2, reason2 = _norm_dividend(shell, "000001.SZ", [])
    assert out2 is None


def test_dividend_warning_no_raw_leak(tmp_path):
    """dividend warning 不泄漏原始计划内容。"""
    from app.stocks_deep_service import StocksDeepService

    secret = "C:\\secret\\token-xxx"
    _write_envelope(tmp_path, "dividend", "000001.SZ", {
        "code": "sz000001", "plans": [
            {"reportEndDate": "bad", "proposalSn": 1, "dividendPlan": secret}]},
        "data_dividend")
    svc = StocksDeepService(tmp_path)
    env = svc.ownership("000001.SZ")
    assert env["availability"]["dividend"] == "unavailable"
    raw = json.dumps(env, ensure_ascii=False)
    assert secret not in raw


@pytest.mark.skipif(not _has_real_cache("financials", "600519.SH"),
                    reason="真实 financials 缓存缺失，跳过")
def test_e2e_real_financials_top_as_of():
    """真实 financials 缓存 API 顶层 as_of=2026-03-31（最新报告期）。"""
    from app.stocks_deep_service import StocksDeepService

    env = StocksDeepService(_REPO_ROOT).fundamentals("600519.SH")
    assert env["as_of"] == "2026-03-31"
    assert env["capability_meta"]["financials"]["as_of"] == "2026-03-31"


@pytest.mark.skipif(not _has_real_cache("shareholders", "600519.SH"),
                    reason="真实 shareholders 缓存缺失，跳过")
def test_e2e_real_shareholders_meta_as_of():
    """真实 shareholders capability_meta.as_of=2026-03-31。"""
    from app.stocks_deep_service import StocksDeepService

    env = StocksDeepService(_REPO_ROOT).ownership("600519.SH")
    assert env["capability_meta"]["shareholders"]["as_of"] == "2026-03-31"


@pytest.mark.skipif(not _has_real_cache("dividend", "000001.SZ"),
                    reason="真实 dividend 缓存缺失，跳过")
def test_e2e_real_dividend_top_as_of():
    """真实 dividend 000001.SZ as_of=最新 report_date（2025-12-31）。"""
    from app.stocks_deep_service import StocksDeepService

    env = StocksDeepService(_REPO_ROOT).ownership("000001.SZ")
    assert env["capability_meta"]["dividend"]["as_of"] == "2025-12-31"
    assert env["as_of"] == "2025-12-31"
