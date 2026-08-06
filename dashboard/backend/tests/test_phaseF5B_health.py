"""F5-B：Westock 健康评估/告警/建议/趋势 只读测试。

覆盖规格十后验收矩阵：五维健康与总体严重度、每个 critical/high 规则、
聚合不告警风暴、summary-only 边界、q scope 不泄漏、稳定 ID 与确定性排序、
建议映射与白名单、receipt/consumer 不生成刷新按钮、7/30 趋势上海边界与
跨时区、补零、除零、四 API 认证分页过滤未知参数、注入不泄漏、只读哈希、
固定时钟连续调用一致。
"""
import json
import hashlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

# 复用 F5-A 测试 helpers（tests 目录加入 path）
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.westock_bridge import WestockCacheStore
from app.westock_refresh_service import build_refresh_store, RefreshError
from app.westock_health_service import (
    build_health_service, SEVERITIES, ALERT_CATEGORIES, RECOMMENDATION_CODES,
    _SHANGHAI_TZ,
)
from test_phaseF5A_operations import (
    _svc, _write_raw, _tech_payload, _data_hash, _now,
    _mk_request_file, _run_stock_jobs, _mk_completed_request_file,
    _mk_receipt, _mk_matching_receipt, _env_dict, _mk_curated,
)

CSRF_COOKIE = "ashare_dash_csrf"


def _health(tmp_path, *symbols):
    svc, store, cache = _svc(tmp_path, *symbols)
    return build_health_service(svc), svc, store, cache


def _run_stock_jobs_warn(store, preset, symbols, warning, session="s"):
    """个股刷新：全部 job 以**指定 warning** 失败。

    F5-A 的 `_run_stock_jobs(..., "failed")` 固定写 `warning="上游无数据"`，
    经 `_category_from_warning` → `upstream_empty`，按去重口径**不应**进入
    `recent_refresh_failure`。本 helper 允许构造真实的普通失败（export_failed 等）。
    """
    rid = store.create_request(body={"target": "stock", "preset": preset,
                                     "symbols": symbols},
                               session_id=session)["request_id"]
    store.claim(rid, "a" * 64)
    for j in store._read_request_file(rid)["jobs"]:
        store.complete_job(rid, j["job_id"], "failed", warning=warning)
    store.finish(rid)
    return rid


def _mk_stock_failed_request_file(tmp_path, rid_hex, *, created_at, recorded_at,
                                  finished_at=None, warning="数据未导出",
                                  symbol="600519.SH", capability="quote",
                                  session="s", status="failed",
                                  status_detail="无能力成功导出"):
    """手写终态 stock 请求文件：创建时间与 job 事件时间**可独立控制**。

    用于验证"最近 24 小时"按事件发生时间（job.recorded_at）判定，
    而不是按 created_at/updated_at 近似。
    """
    from app.westock_refresh_service import canonical_request_hash
    store = build_refresh_store(tmp_path)
    finished_at = finished_at or recorded_at
    target = {"kind": "stock", "symbols": [symbol], "preset": "quote_only",
              "allow_summary_only": False, "summary_only_symbols": []}
    request = {
        "schema_version": 2, "request_id": rid_hex, "created_at": created_at,
        "updated_at": finished_at, "status": status, "target": target,
        "jobs": [{"job_id": f"{7:032x}", "capability": capability, "scope": symbol,
                  "status": "failed", "summary_only": False,
                  "recorded_at": recorded_at, "warning": warning}],
        "request_hash": canonical_request_hash(target),
        "session_fingerprint": hashlib.sha256(session.encode()).hexdigest(),
        "attempts": 1, "worker_id": "a" * 64, "claimed_at": created_at,
        "started_at": created_at, "finished_at": finished_at,
        "expires_at": (datetime.fromisoformat(created_at) + timedelta(hours=24)).isoformat(),
        "warnings": [], "status_detail": status_detail,
    }
    p = store.requests_dir / f"{rid_hex}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    return rid_hex


def _dir_hashes(root: Path) -> dict[str, str]:
    out = {}
    if not root.exists():
        return out
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


@pytest.fixture()
def health_client(tmp_path, config_factory, fake_executor):
    from fastapi.testclient import TestClient
    from app.main import create_app
    app = create_app(config_factory(project_root=str(tmp_path)),
                     enable_static=False, executor=fake_executor)
    with TestClient(app, base_url="https://127.0.0.1") as c:
        resp = c.post("/api/auth/login", json={"username": "admin", "password": "secret123"})
        assert resp.status_code == 200, resp.text
        yield c


# ---------------------------------------------------------------------- #
# 1. 五维健康状态与总体严重度提升
# ---------------------------------------------------------------------- #
def test_health_five_dimensions_and_overall(tmp_path):
    hs, svc, store, cache = _health(tmp_path, "600519.SH")
    # 空缓存 + 无请求 → not_observed
    h = hs.health()
    assert h["overall_status"] == "not_observed"
    assert all(d["status"] == "not_observed" for d in h["dimensions"].values())
    assert h["observed"] is False
    # 有缓存 + 合法请求（receipt valid）→ 整体健康
    base = tmp_path / "state/dashboard/westock"
    tech = _tech_payload()
    _write_raw(base, "technical", "600519.SH", tech, _now().isoformat())
    _run_stock_jobs(store, "market_data", ["600519.SH"], "ok", _data_hash(tech), session="s1")
    h2 = hs.health()
    assert h2["observed"] is True
    assert h2["overall_status"] in ("healthy", "attention")  # 部分维度可能 attention
    # 构造 hash_mismatch → integrity critical → 总体 critical
    rid = _run_stock_jobs(store, "market_data", ["600519.SH"], "ok", "c" * 64, session="s2")
    rp = store.receipts_dir / f"{rid}.json"
    rec = json.loads(rp.read_text(encoding="utf-8"))
    for j in rec["jobs"]:
        if j["capability"] == "technical":
            j["content_hash"] = "c" * 64
    rp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    h3 = hs.health()
    assert h3["dimensions"]["integrity"]["status"] == "critical"
    assert h3["overall_status"] == "critical"
    assert h3["alert_summary"]["critical"] >= 1


# ---------------------------------------------------------------------- #
# 2. 每个 critical/high 规则
# ---------------------------------------------------------------------- #
def test_critical_and_high_rules(tmp_path):
    hs, svc, store, cache = _health(tmp_path, "600519.SH")
    base = tmp_path / "state/dashboard/westock"
    now = _now()
    tech = _tech_payload()
    _write_raw(base, "technical", "600519.SH", tech, (now - timedelta(seconds=10)).isoformat())
    # hash_mismatch（critical）
    _run_stock_jobs(store, "market_data", ["600519.SH"], "ok", "c" * 64, session="s1")
    # consumer_unusable（critical）：合法 envelope 但坏 data
    _write_raw(base, "quote", "600519.SH", {"bad": "shape"}, now.isoformat())
    # receipt_mismatch（critical）
    _mk_completed_request_file(tmp_path, f"{2:032x}", "2026-08-06T00:00:00+00:00")
    req = json.loads((store.requests_dir / f"{2:032x}.json").read_text(encoding="utf-8"))
    rec = _mk_matching_receipt(tmp_path, f"{2:032x}", req)
    rec["status_detail"] = "tampered"
    (store.receipts_dir / f"{2:032x}.json").write_text(
        json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    # invalid_cache_file（high）
    p_lhb = base / "lhb/global.json"
    p_lhb.parent.mkdir(parents=True, exist_ok=True)
    p_lhb.write_text("{broken", encoding="utf-8")
    # future_timestamp（high）
    _write_raw(base, "news", "600519.SH", {}, (now + timedelta(hours=1)).isoformat())
    # receipt_invalid（high）
    _mk_completed_request_file(tmp_path, f"{3:032x}", "2026-08-06T01:00:00+00:00")
    (store.receipts_dir / f"{3:032x}.json").write_text("{broken", encoding="utf-8")
    # recent_worker_timeout（high）
    rid4 = store.create_request(body={"target": "market", "preset": "overview"},
                                session_id="s2")["request_id"]
    store.claim(rid4, "a" * 64)
    for j in store._read_request_file(rid4)["jobs"]:
        store.complete_job(rid4, j["job_id"], "failed", warning="worker_timeout 超时")
    store.finish(rid4)
    # recent_refresh_failure（high）：必须是**真实普通失败**（export_failed），
    # 不能用 "上游无数据"（upstream_empty，按去重口径只进 low 级 unsupported_or_empty）
    _run_stock_jobs_warn(store, "quote_only", ["600519.SH"], "数据未导出", session="s3")
    cats = {a["category"] for a in hs.alerts()}
    for c in ("hash_mismatch", "consumer_unusable", "receipt_mismatch",
              "invalid_cache_file", "future_timestamp", "receipt_invalid",
              "recent_worker_timeout", "recent_refresh_failure"):
        assert c in cats, c
    sev = {a["category"]: a["severity"] for a in hs.alerts()}
    assert sev["hash_mismatch"] == "critical"
    assert sev["consumer_unusable"] == "critical"
    assert sev["receipt_mismatch"] == "critical"
    assert sev["invalid_cache_file"] == "high"
    assert sev["future_timestamp"] == "high"
    assert sev["receipt_invalid"] == "high"
    assert sev["recent_worker_timeout"] == "high"
    assert sev["recent_refresh_failure"] == "high"


# ---------------------------------------------------------------------- #
# 3. missing/stale/unverified 聚合不产生告警风暴
# ---------------------------------------------------------------------- #
def test_aggregation_no_alert_storm(tmp_path):
    hs, svc, store, cache = _health(tmp_path, "600519.SH", "000001.SZ", "601398.SH")
    # 三只股票 quote 全部缺失 → 只按 capability 聚合 1 条
    cats = [a["category"] for a in hs.alerts()]
    missing = [a for a in hs.alerts() if a["category"] == "missing_expected_cache"]
    quote_missing = [a for a in missing if a.get("capability") == "quote"]
    assert len(quote_missing) == 1
    assert quote_missing[0]["affected_count"] == 3  # 3 个 missing cell 合并
    # 多个 stale 同 capability → 聚合 1 条
    base = tmp_path / "state/dashboard/westock"
    old = (datetime(2026, 8, 1).replace(tzinfo=timezone.utc)).isoformat()
    for sym in ("600519.SH", "000001.SZ", "601398.SH"):
        _write_raw(base, "quote", sym, {"sh600519": {"p": 1}}, old)
    stale = [a for a in hs.alerts() if a["category"] == "stale_cache"
             and a.get("capability") == "quote"]
    assert len(stale) == 1 and stale[0]["affected_count"] == 3
    # unverified 聚合
    unv = [a for a in hs.alerts() if a["category"] == "hash_unverified"
           and a.get("capability") == "quote"]
    assert len(unv) == 1 and unv[0]["affected_count"] >= 3


# ---------------------------------------------------------------------- #
# 4. summary-only 不因 minute/technical 告警
# ---------------------------------------------------------------------- #
def test_summary_only_no_minute_technical_alerts(tmp_path):
    hs, svc, store, cache = _health(tmp_path, "600519.SH")
    # summary-only 股票 601318.SH（非本地）
    store.create_request(body={"target": "stock", "preset": "full_research",
                               "symbols": ["601318.SH"], "allow_summary_only": True},
                         session_id="s1")
    alerts = hs.alerts()
    for a in alerts:
        if a.get("symbol") == "601318.SH":
            assert a["category"] not in ("missing_expected_cache", "stale_cache",
                                         "capability_unavailable"), a
    # minute/technical 不因 summary-only 产生告警
    assert not any(a.get("capability") in ("minute", "technical")
                   and a.get("symbol") == "601318.SH" for a in alerts)


# ---------------------------------------------------------------------- #
# 5. q scope 不泄漏完整值
# ---------------------------------------------------------------------- #
def test_q_scope_not_leaked(tmp_path):
    from app.westock_operations_service import _short_scope
    hs, svc, store, cache = _health(tmp_path, "600519.SH")
    q = "q_" + "d" * 64
    _mk_request_file_screener(tmp_path, f"{9:032x}", "2026-08-06T00:00:00+00:00", q)
    alerts = hs.alerts()
    raw = json.dumps(alerts) + json.dumps(hs.recommendations()) + json.dumps(hs.health())
    assert q not in raw
    assert ("q_" + "d" * 16) not in raw


def _mk_request_file_screener(tmp_path, rid_hex, created_at: str, q_scope: str,
                              capability: str = "filter", session: str = "s"):
    from app.westock_refresh_service import canonical_request_hash
    store = build_refresh_store(tmp_path)
    target = {"kind": "screener", "result_id": "a" * 32,
              "cache_scope": q_scope, "capability": capability}
    request = {
        "schema_version": 2, "request_id": rid_hex, "created_at": created_at,
        "updated_at": created_at, "status": "pending", "target": target,
        "jobs": [{"job_id": "c" * 32, "capability": capability, "scope": q_scope,
                  "status": "pending", "summary_only": False}],
        "request_hash": canonical_request_hash(target),
        "session_fingerprint": hashlib.sha256(session.encode()).hexdigest(),
        "attempts": 0, "worker_id": None, "claimed_at": None, "started_at": None,
        "finished_at": None,
        "expires_at": (datetime.fromisoformat(created_at) + timedelta(hours=24)).isoformat(),
        "warnings": [], "status_detail": None,
    }
    p = store.requests_dir / f"{rid_hex}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------- #
# 6. 告警稳定 ID 与确定性排序
# ---------------------------------------------------------------------- #
def test_alert_stable_id_and_sort(tmp_path):
    hs, svc, store, cache = _health(tmp_path, "600519.SH")
    base = tmp_path / "state/dashboard/westock"
    _write_raw(base, "quote", "600519.SH", {"bad": "shape"}, _now().isoformat())
    a1 = hs.alerts()
    a2 = hs.alerts()
    assert [x["alert_id"] for x in a1] == [x["alert_id"] for x in a2]
    weights = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    sev_order = [weights[x["severity"]] for x in a1]
    assert sev_order == sorted(sev_order, reverse=True)
    # 连续调用 alerts/health/recommendations 完全一致
    assert json.dumps(a1, sort_keys=True) == json.dumps(a2, sort_keys=True)
    assert json.dumps(hs.health(), sort_keys=True) == json.dumps(hs.health(), sort_keys=True)


# ---------------------------------------------------------------------- #
# 7. 建议严格映射与参数白名单
# ---------------------------------------------------------------------- #
def test_recommendation_mapping_and_validation(tmp_path):
    hs, svc, store, cache = _health(tmp_path, "600519.SH")
    base = tmp_path / "state/dashboard/westock"
    tech = _tech_payload()
    _write_raw(base, "technical", "600519.SH", tech, _now().isoformat())
    _run_stock_jobs(store, "market_data", ["600519.SH"], "ok", "c" * 64, session="s1")
    recs = hs.recommendations()
    codes = {r["code"] for r in recs}
    assert "refresh_hash_mismatch" in codes
    assert "no_action_required" not in {r["code"] for r in recs if r["priority"] == "critical"}
    for r in recs:
        assert r["code"] in RECOMMENDATION_CODES
        assert r["priority"] in SEVERITIES
        assert len(r["symbols"]) <= 20 and len(r["capabilities"]) <= 20
        assert all(s and s == s.strip() for s in r["symbols"])
    # 参数白名单
    def ops_400(kind, params):
        try:
            getattr(hs, kind)(params)
            return False
        except RefreshError as exc:
            return exc.status_code == 400
    assert ops_400("alerts_api", {"severity": "zzz"})
    assert ops_400("alerts_api", {"category": "nope"})
    assert ops_400("alerts_api", {"capability": "nope"})
    assert ops_400("alerts_api", {"symbol": "abc"})
    assert ops_400("alerts_api", {"limit": "0"})
    assert ops_400("alerts_api", {"limit": "201"})
    assert ops_400("alerts_api", {"offset": "-1"})
    assert ops_400("alerts_api", {"hacked": "1"})
    assert ops_400("recommendations_api", {"priority": "zzz"})
    assert ops_400("recommendations_api", {"code": "nope"})
    assert ops_400("recommendations_api", {"target_kind": "zzz"})
    assert ops_400("trends_api", {"window_days": "14"})
    assert ops_400("trends_api", {"window_days": "7", "x": "1"})


# ---------------------------------------------------------------------- #
# 8. receipt/consumer 问题不生成普通刷新按钮
# ---------------------------------------------------------------------- #
def test_receipt_consumer_no_prefill_refresh(tmp_path):
    hs, svc, store, cache = _health(tmp_path, "600519.SH")
    base = tmp_path / "state/dashboard/westock"
    _write_raw(base, "quote", "600519.SH", {"bad": "shape"}, _now().isoformat())
    _mk_completed_request_file(tmp_path, f"{2:032x}", "2026-08-06T00:00:00+00:00")
    req = json.loads((store.requests_dir / f"{2:032x}.json").read_text(encoding="utf-8"))
    rec = _mk_matching_receipt(tmp_path, f"{2:032x}", req)
    rec["status"] = "failed"
    (store.receipts_dir / f"{2:032x}.json").write_text(
        json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    recs = hs.recommendations()
    by_code = {r["code"]: r for r in recs}
    assert by_code["inspect_consumer_schema"]["can_prefill_refresh"] is False
    assert by_code["inspect_receipt_chain"]["can_prefill_refresh"] is False
    assert by_code["inspect_receipt_chain"]["requires_workbuddy"] is False
    # 可预填 ⟺ 建议本身属于刷新类 **且** 有受控 target_kind（stock/market）。
    # 无对应 preset 的 global-only 能力（filter/strategy_select 等）宁可不给按钮，
    # 也绝不下发假预填；inspect/no_action 类建议永远不给刷新按钮。
    from app.westock_health_service import _PREFILLABLE_CODES
    for r in recs:
        expect = (r["code"] in _PREFILLABLE_CODES
                  and r["target_kind"] in ("stock", "market"))
        assert r["can_prefill_refresh"] is expect, r
    # 至少存在一条真实可预填的个股刷新建议
    stock_prefill = [r for r in recs if r["target_kind"] == "stock"
                     and r["can_prefill_refresh"]]
    assert stock_prefill, [(r["code"], r["target_kind"]) for r in recs]
    assert all(r["code"].startswith("refresh_") for r in stock_prefill)


# ---------------------------------------------------------------------- #
# 9. trends：7/30 天、上海日期边界、跨时区
# ---------------------------------------------------------------------- #
def test_trends_windows_and_timezone(tmp_path, monkeypatch):
    import app.westock_health_service as hs_mod
    # 唯一时钟源：趋势的上海自然日由 service clock（_utc_now）派生，
    # 模块内不再重复定义 _shanghai_today
    monkeypatch.setattr(hs_mod, "_utc_now", lambda: datetime(2026, 8, 7, 4, 0, 0, tzinfo=timezone.utc))
    assert not hasattr(hs_mod, "_shanghai_today"), "模块级 _shanghai_today 必须已删除"
    hs, svc, store, cache = _health(tmp_path)
    # 跨时区同日：12:00+08:00（=04:00 UTC）与 05:00+00:00（=05:00 UTC）都是上海 8/6
    _mk_request_file(tmp_path, f"{1:032x}", "2026-08-06T12:00:00+08:00", session="s1")
    _mk_request_file(tmp_path, f"{2:032x}", "2026-08-06T05:00:00+00:00", session="s2")
    # 上海边界：8/6 23:00+00:00 = 8/7 07:00 上海（跨日归次日）
    _mk_request_file(tmp_path, f"{3:032x}", "2026-08-06T23:00:00+00:00", session="s3")
    tr7 = hs.trends(7)
    assert tr7["window_days"] == 7
    assert len(tr7["daily"]) == 7
    assert tr7["start_date"] == "2026-08-01" and tr7["end_date"] == "2026-08-07"
    by_date = {d["date"]: d for d in tr7["daily"]}
    assert by_date["2026-08-06"]["requests_total"] == 2
    assert by_date["2026-08-07"]["requests_total"] == 1  # 上海跨日
    assert by_date["2026-08-05"]["requests_total"] == 0  # 补零
    tr30 = hs.trends(30)
    assert tr30["window_days"] == 30 and len(tr30["daily"]) == 30
    assert tr30["start_date"] == "2026-07-09"


# ---------------------------------------------------------------------- #
# 10. 补零 + 11. 除零
# ---------------------------------------------------------------------- #
def test_trends_zero_fill_and_zero_division(tmp_path):
    hs, svc, store, cache = _health(tmp_path)
    tr = hs.trends(7)
    assert all(d["requests_total"] == 0 for d in tr["daily"])
    assert all(d["success_rate"] is None for d in tr["daily"])
    assert all(d["average_duration_seconds"] is None for d in tr["daily"])
    raw = json.dumps(tr)
    assert "NaN" not in raw and "Infinity" not in raw


# ---------------------------------------------------------------------- #
# 12. 四 API 认证、分页、过滤、未知参数
# ---------------------------------------------------------------------- #
def test_api_auth_pagination_filter_unknown(health_client, tmp_path):
    # 401 未认证（独立未登录客户端）
    from fastapi.testclient import TestClient
    from app.config import DashboardConfig
    from app.main import create_app
    from argon2 import PasswordHasher
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    ph = PasswordHasher().hash("secret123")
    cfg = DashboardConfig(username="admin", password_hash=ph, session_secret="s" * 40,
                          host="127.0.0.1", port=8765,
                          auth_file=tmp / "auth.json", project_root=str(tmp))
    app = create_app(cfg)
    with TestClient(app, base_url="https://127.0.0.1") as c:
        for path in ("/api/connections/westock/health",
                     "/api/connections/westock/alerts",
                     "/api/connections/westock/recommendations",
                     "/api/connections/westock/trends"):
            assert c.get(path).status_code == 401, path
    # health 拒绝任何参数
    r = health_client.get("/api/connections/westock/health?x=1")
    assert r.status_code == 400
    # alerts 过滤 + 分页
    r = health_client.get("/api/connections/westock/alerts?limit=5&offset=0")
    assert r.status_code == 200
    body = r.json()
    assert body["data"]["limit"] == 5 and body["data"]["offset"] == 0
    assert len(body["data"]["items"]) <= 5
    assert "total" in body["data"]
    # 未知参数/非法枚举/越界
    for q in ("severity=zzz", "category=nope", "capability=nope", "symbol=abc",
              "limit=0", "limit=201", "offset=-1", "hacked=1"):
        r = health_client.get(f"/api/connections/westock/alerts?{q}")
        assert r.status_code == 400, q
    r = health_client.get("/api/connections/westock/recommendations?code=nope")
    assert r.status_code == 400
    r = health_client.get("/api/connections/westock/trends?window_days=14")
    assert r.status_code == 400
    r = health_client.get("/api/connections/westock/trends?window_days=30")
    assert r.status_code == 200
    # 过滤生效
    r = health_client.get("/api/connections/westock/alerts?severity=critical")
    assert r.status_code == 200
    assert all(x["severity"] == "critical" for x in r.json()["data"]["items"])


# ---------------------------------------------------------------------- #
# 13. 注入 token/path/URL/Traceback 不泄漏
# ---------------------------------------------------------------------- #
def test_injection_not_leaked(health_client, tmp_path):
    store = build_refresh_store(tmp_path)
    _mk_curated(tmp_path, "600519.SH")
    rid = f"{1:032x}"
    _mk_completed_request_file(tmp_path, rid, "2026-08-06T00:00:00+00:00")
    p = store.requests_dir / f"{rid}.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["warnings"] = ["Authorization: Bearer 123", "C:\\secret\\token-xxx",
                        "Traceback (most recent call last)",
                        "https://evil.example/?token=SECRET_URL"]
    data["status_detail"] = "Traceback: C:\\secret\\token-xxx?key=SECRET_URL"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    _mk_matching_receipt(tmp_path, rid, data)
    _write_raw(tmp_path / "state/dashboard/westock", "quote", "600519.SH",
               {"sh600519": {"p": 1}}, _now().isoformat())
    for path in ("/api/connections/westock/health",
                 "/api/connections/westock/alerts?limit=50",
                 "/api/connections/westock/recommendations?limit=50",
                 "/api/connections/westock/trends?window_days=7"):
        r = health_client.get(path)
        assert r.status_code == 200, path
        raw = json.dumps(r.json())
        for bad in ("Authorization", "Bearer", "C:\\secret", "token-xxx",
                    "Traceback", "SECRET_URL", "evil.example", "content_hash",
                    "worker_id", "session_fingerprint"):
            assert bad.lower() not in raw.lower(), (path, bad)


# ---------------------------------------------------------------------- #
# 14. 只读哈希：API 调用前后 state/data 文件 SHA-256 与文件集合不变
# ---------------------------------------------------------------------- #
def test_readonly_hash_proof(health_client, tmp_path):
    _mk_curated(tmp_path, "600519.SH")
    _write_raw(tmp_path / "state/dashboard/westock", "quote", "600519.SH",
               {"sh600519": {"p": 1}}, _now().isoformat())
    before = _dir_hashes(tmp_path / "state") | _dir_hashes(tmp_path / "data")
    before_files = set(before)
    for path in ("/api/connections/westock/health",
                 "/api/connections/westock/alerts?limit=5",
                 "/api/connections/westock/recommendations?limit=5",
                 "/api/connections/westock/trends?window_days=7",
                 "/api/connections/westock/trends?window_days=30"):
        r = health_client.get(path)
        assert r.status_code == 200, path
    after = _dir_hashes(tmp_path / "state") | _dir_hashes(tmp_path / "data")
    assert set(after) == before_files
    assert after == before


# ---------------------------------------------------------------------- #
# 15. 固定时钟下连续调用完全一致
# ---------------------------------------------------------------------- #
def test_consistent_under_fixed_clock(tmp_path, monkeypatch):
    import app.westock_health_service as hs_mod
    fixed = datetime(2026, 8, 6, 4, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(hs_mod, "_utc_now", lambda: fixed)
    hs, svc, store, cache = _health(tmp_path, "600519.SH")
    base = tmp_path / "state/dashboard/westock"
    _write_raw(base, "quote", "600519.SH", {"sh600519": {"p": 1}},
               (fixed - timedelta(seconds=10)).isoformat())
    _run_stock_jobs(store, "quote_only", ["600519.SH"], "ok", "b" * 64, session="s1")
    r1 = json.dumps(hs.alerts(), sort_keys=True)
    h1 = json.dumps(hs.health(), sort_keys=True)
    t1 = json.dumps(hs.trends(7), sort_keys=True)
    r2 = json.dumps(hs.alerts(), sort_keys=True)
    h2 = json.dumps(hs.health(), sort_keys=True)
    t2 = json.dumps(hs.trends(7), sort_keys=True)
    assert r1 == r2 and h1 == h2 and t1 == t2
    assert _dir_hashes(tmp_path / "state") == _dir_hashes(tmp_path / "state")

# ====================================================================== #
# 第一轮审核整改：逐规则参数化反例（项二~项九）
# ====================================================================== #
_FIXED_NOW = datetime(2026, 8, 6, 4, 0, 0, tzinfo=timezone.utc)  # 上海 2026-08-06 12:00


@pytest.fixture()
def fixed_clock(monkeypatch):
    """钉死健康服务**唯一**时钟源：recent 窗口与趋势自然日都由它派生。"""
    import app.westock_health_service as hs_mod
    monkeypatch.setattr(hs_mod, "_utc_now", lambda: _FIXED_NOW)
    return _FIXED_NOW


def _mk_stock_completed_request_file(tmp_path, rid_hex, *, created_at, started_at,
                                     finished_at, recorded_at, symbol="600519.SH",
                                     capability="quote", session="s"):
    """手写 completed 请求文件：创建日 / 完成日 / job 记录日**可独立控制**。

    用于验证趋势按实际事件日期聚合（发起量归创建日，终态与耗时归完成日，
    job 分布归各自 recorded_at）。
    """
    from app.westock_refresh_service import canonical_request_hash
    store = build_refresh_store(tmp_path)
    target = {"kind": "stock", "symbols": [symbol], "preset": "quote_only",
              "allow_summary_only": False, "summary_only_symbols": []}
    request = {
        "schema_version": 2, "request_id": rid_hex, "created_at": created_at,
        "updated_at": finished_at, "status": "completed", "target": target,
        "jobs": [{"job_id": f"{8:032x}", "capability": capability, "scope": symbol,
                  "status": "ok", "summary_only": False, "recorded_at": recorded_at,
                  "fetched_at": recorded_at, "cache_status": "fresh",
                  "data_as_of": "2026-08-02", "content_hash": "b" * 64}],
        "request_hash": canonical_request_hash(target),
        "session_fingerprint": hashlib.sha256(session.encode()).hexdigest(),
        "attempts": 1, "worker_id": "a" * 64, "claimed_at": started_at,
        "started_at": started_at, "finished_at": finished_at,
        "expires_at": (datetime.fromisoformat(created_at) + timedelta(hours=24)).isoformat(),
        "warnings": [], "status_detail": "全部能力导出成功",
    }
    p = store.requests_dir / f"{rid_hex}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
    return rid_hex


# ---------------------------------------------------------------------- #
# 16. 项二：同一失败事实**绝不**重复告警（逐失败分类参数化反例）
# ---------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "warning,status_detail,n_worker_timeout,n_refresh_failure,n_unsup_empty",
    [
        # worker 超时：只出一条 high recent_worker_timeout，不再出 recent_refresh_failure
        pytest.param("处理超时（2 小时 worker_timeout），已失败", "worker_timeout",
                     1, 0, 0, id="worker_timeout_single_high"),
        # 上游为空：只进 low 级 unsupported_or_empty，不算刷新失败
        pytest.param("上游无数据", "无能力成功导出", 0, 0, 1, id="upstream_empty_low_only"),
        # 能力不支持：同上
        pytest.param("该标的不支持此能力", "无能力成功导出", 0, 0, 1, id="unsupported_low_only"),
        # 真实普通失败（export_failed）：才产生 recent_refresh_failure
        pytest.param("数据未导出", "无能力成功导出", 0, 1, 0, id="export_failed_is_real_failure"),
    ],
)
def test_failure_fact_not_double_alerted(tmp_path, fixed_clock, warning, status_detail,
                                         n_worker_timeout, n_refresh_failure,
                                         n_unsup_empty):
    hs, svc, store, cache = _health(tmp_path, "600519.SH")
    _mk_stock_failed_request_file(
        tmp_path, f"{0x11:032x}",
        created_at=(fixed_clock - timedelta(hours=3)).isoformat(),
        recorded_at=(fixed_clock - timedelta(hours=1)).isoformat(),
        warning=warning, status_detail=status_detail)
    cats = [a["category"] for a in hs.alerts()]
    assert cats.count("recent_worker_timeout") == n_worker_timeout, cats
    assert cats.count("recent_refresh_failure") == n_refresh_failure, cats
    assert cats.count("unsupported_or_empty") == n_unsup_empty, cats
    # 同一失败事实绝不同时产生两条 high
    assert not (cats.count("recent_worker_timeout")
                and cats.count("recent_refresh_failure")), cats


# ---------------------------------------------------------------------- #
# 17. 项三：最近 24 小时按**事件发生时间**，不看 created_at/updated_at
# ---------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "created_hours_ago,recorded_hours_ago,expect_recent",
    [
        # 请求两天前创建，但 job 一小时前才失败 → 属于最近（用 created_at 会漏报）
        pytest.param(48, 1, True, id="old_request_fresh_failure_is_recent"),
        # 请求一小时前创建，但失败事件发生在两天前 → 不属于最近（用 created_at 会误报）
        pytest.param(1, 48, False, id="new_request_old_failure_not_recent"),
        # 窗口内 / 外边界
        pytest.param(72, 23, True, id="event_23h_ago_in_window"),
        pytest.param(72, 25, False, id="event_25h_ago_out_of_window"),
    ],
)
def test_recent_window_uses_event_time(tmp_path, fixed_clock, created_hours_ago,
                                       recorded_hours_ago, expect_recent):
    hs, svc, store, cache = _health(tmp_path, "600519.SH")
    recorded = fixed_clock - timedelta(hours=recorded_hours_ago)
    _mk_stock_failed_request_file(
        tmp_path, f"{0x12:032x}",
        created_at=(fixed_clock - timedelta(hours=created_hours_ago)).isoformat(),
        recorded_at=recorded.isoformat(), finished_at=recorded.isoformat(),
        warning="数据未导出")
    cats = [a["category"] for a in hs.alerts()]
    assert ("recent_refresh_failure" in cats) is expect_recent, cats


def test_recent_window_timezone_equivalent(tmp_path, fixed_clock):
    """同一绝对时刻用 +00:00 与 +08:00 表达，recent 判定与告警集合完全一致。"""
    created = fixed_clock - timedelta(hours=30)
    recorded = fixed_clock - timedelta(hours=2)
    results = []
    for i, tz in enumerate((timezone.utc, _SHANGHAI_TZ)):
        root = tmp_path / f"tz{i}"
        hs, svc, store, cache = _health(root, "600519.SH")
        _mk_stock_failed_request_file(
            root, f"{0x13:032x}",
            created_at=created.astimezone(tz).isoformat(),
            recorded_at=recorded.astimezone(tz).isoformat(),
            finished_at=recorded.astimezone(tz).isoformat(),
            warning="数据未导出")
        results.append(sorted((a["category"], a["alert_id"]) for a in hs.alerts()))
    assert results[0] == results[1]
    assert any(c == "recent_refresh_failure" for c, _ in results[0]), results[0]


# ---------------------------------------------------------------------- #
# 18. 项四：趋势按**实际事件日期**（Asia/Shanghai）跨日聚合矩阵
# ---------------------------------------------------------------------- #
def test_trends_cross_day_event_date_matrix(tmp_path, fixed_clock):
    """8/1 创建、8/2 完成 + job recorded：
    requests_total 落 8/1；completed / job ok / duration / receipt_issue 落 8/2；
    其余日期全部补零。"""
    hs, svc, store, cache = _health(tmp_path, "600519.SH")
    created = "2026-08-01T02:00:00+08:00"      # 上海 8/1
    finished = "2026-08-02T10:00:00+08:00"     # 上海 8/2
    recorded = "2026-08-02T09:30:00+08:00"     # 上海 8/2
    _mk_stock_completed_request_file(
        tmp_path, f"{0x14:032x}", created_at=created, started_at=created,
        finished_at=finished, recorded_at=recorded)

    t = hs.trends(7)
    assert t["start_date"] == "2026-07-31" and t["end_date"] == "2026-08-06"
    by_date = {d["date"]: d for d in t["daily"]}
    assert len(t["daily"]) == 7 and len(by_date) == 7

    d1, d2 = by_date["2026-08-01"], by_date["2026-08-02"]
    # 发起量只在创建日
    assert d1["requests_total"] == 1 and d2["requests_total"] == 0
    # 终态分布只在完成日
    assert d1["status_counts"]["completed"] == 0
    assert d2["status_counts"]["completed"] == 1
    # job 分布按各自 recorded_at
    assert d1["job_counts"]["ok"] == 0 and d2["job_counts"]["ok"] == 1
    # 平均耗时归到完成日（8/1 02:00+08 → 8/2 10:00+08 = 32h）
    assert d1["average_duration_seconds"] is None
    assert d2["average_duration_seconds"] == 32 * 3600
    # 回执问题按完成日（completed 无回执 → missing）
    assert d1["receipt_issue_count"] == 0 and d2["receipt_issue_count"] == 1
    # 成功率：8/1 无 job 事件 → None（除零守卫）；8/2 = 1/1
    assert d1["success_rate"] is None and d2["success_rate"] == 1.0
    # 其余日期严格补零
    for iso in ("2026-07-31", "2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06"):
        z = by_date[iso]
        assert z["requests_total"] == 0
        assert z["status_counts"] == {"completed": 0, "partial": 0, "failed": 0,
                                      "cancelled": 0, "expired": 0}
        assert z["job_counts"] == {"ok": 0, "partial": 0, "failed": 0, "skipped": 0}
        assert z["worker_timeout_count"] == 0 and z["receipt_issue_count"] == 0
        assert z["success_rate"] is None and z["average_duration_seconds"] is None


def test_trends_worker_timeout_uses_job_recorded_at(tmp_path, fixed_clock):
    """worker 超时按 job.recorded_at 落日，不按创建日，且只计一次。"""
    hs, svc, store, cache = _health(tmp_path, "600519.SH")
    _mk_stock_failed_request_file(
        tmp_path, f"{0x15:032x}",
        created_at="2026-08-01T02:00:00+08:00",
        recorded_at="2026-08-03T08:00:00+08:00",
        finished_at="2026-08-03T08:00:00+08:00",
        warning="处理超时（2 小时 worker_timeout），已失败",
        status_detail="worker_timeout")
    by_date = {d["date"]: d for d in hs.trends(7)["daily"]}
    assert by_date["2026-08-01"]["worker_timeout_count"] == 0
    assert by_date["2026-08-03"]["worker_timeout_count"] == 1
    assert sum(d["worker_timeout_count"] for d in by_date.values()) == 1
    assert by_date["2026-08-01"]["requests_total"] == 1  # 发起量仍归创建日
    assert by_date["2026-08-03"]["status_counts"]["failed"] == 1


# ---------------------------------------------------------------------- #
# 19. 项五：capability_unavailable 覆盖**全部作用域**（含 global-only 能力）
# ---------------------------------------------------------------------- #
@pytest.mark.parametrize("capability", ["market_overview", "sector", "macro", "index"])
def test_capability_unavailable_covers_global_only(tmp_path, capability):
    """market_overview 等 global-only 能力无缓存时同样必须告警。"""
    hs, svc, store, cache = _health(tmp_path, "600519.SH")
    alerts = [a for a in hs.alerts() if a["category"] == "capability_unavailable"]
    hit = [a for a in alerts if a["capability"] == capability]
    assert len(hit) == 1, [a["capability"] for a in alerts]
    ev = hit[0]["evidence"]
    assert ev["usable_count"] == 0 and ev["scope_count"] >= 1
    assert ev["global_count"] >= 1          # 该能力确实由 global scope 贡献
    assert ev["symbols"] == []              # global-only 能力无个股 symbol
    assert ev["query_count"] == 0


def test_capability_unavailable_stock_scope_evidence(tmp_path):
    """个股能力缺失：evidence 含合法 symbols（≤20），完整 q scope 绝不输出。"""
    hs, svc, store, cache = _health(tmp_path, "600519.SH")
    q = "q_" + "e" * 64
    _mk_request_file_screener(tmp_path, f"{0x16:032x}",
                              "2026-08-06T00:00:00+00:00", q, capability="filter")
    alerts = [a for a in hs.alerts() if a["category"] == "capability_unavailable"]
    quote = next(a for a in alerts if a["capability"] == "quote")
    assert quote["evidence"]["symbols"] == ["600519.SH"]
    assert len(quote["evidence"]["symbols"]) <= 20
    filt = next(a for a in alerts if a["capability"] == "filter")
    assert filt["evidence"]["query_count"] >= 1
    raw = json.dumps(alerts, ensure_ascii=False)
    assert q not in raw and ("q_" + "e" * 16) not in raw


# ---------------------------------------------------------------------- #
# 20. 项六：unknown capability → capability=null，且不泄漏目录名/文件名
# ---------------------------------------------------------------------- #
def test_unknown_capability_alert_capability_is_null(tmp_path):
    from app.westock_bridge import CAPABILITY_MAP
    hs, svc, store, cache = _health(tmp_path, "600519.SH")
    base = tmp_path / "state/dashboard/westock"
    secret_dir, secret_file = "zz_secret_dir", "zz_secret_file"
    p = base / secret_dir / f"{secret_file}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"anything": 1}), encoding="utf-8")

    alerts = hs.alerts()
    bad = [a for a in alerts if a["category"] == "invalid_cache_file"]
    assert len(bad) == 1
    assert bad[0]["capability"] is None, bad[0]
    assert bad[0]["symbol"] is None
    assert bad[0]["short_scope"] == "非法 Scope"
    # 任何输出都不得出现真实目录名/文件名
    raw = (json.dumps(alerts, ensure_ascii=False)
           + json.dumps(hs.recommendations(), ensure_ascii=False)
           + json.dumps(hs.health(), ensure_ascii=False))
    assert secret_dir not in raw and secret_file not in raw
    # Alert.capability 必为注册表能力或 null
    for a in alerts:
        assert a["capability"] is None or a["capability"] in CAPABILITY_MAP


# ---------------------------------------------------------------------- #
# 21. 项六 b：future_timestamp 必须检查 **inventory**（不只 coverage）
# ---------------------------------------------------------------------- #
def test_future_timestamp_covers_inventory_only_cells(tmp_path):
    """矩阵外的意外物理文件时间在未来 → 仍产生受控 high 告警，且非法 scope 不外泄。"""
    hs, svc, store, cache = _health(tmp_path, "600519.SH")
    base = tmp_path / "state/dashboard/westock"
    future = datetime(2030, 1, 1, tzinfo=timezone.utc).isoformat()
    # (a) 合法 symbol + 非本地 + minute（summary blocked）→ 只在 inventory，不在 coverage
    _write_raw(base, "minute", "000001.SZ", {"sz000001": {"p": 1}}, future)
    # (b) 已知能力 + 非法 scope → 只在 inventory，且 scope 必须脱敏
    weird = "zz_weird_scope_name"
    _write_raw(base, "quote", weird, {"x": 1}, future)

    ctx = svc._scan()
    assert ("minute", "000001.SZ") not in ctx["cells"], "minute 不应进入预期矩阵"
    assert ("quote", weird) not in ctx["cells"], "非法 scope 不应进入预期矩阵"
    assert ("minute", "000001.SZ") in ctx["inventory_state"]
    assert ("quote", weird) in ctx["inventory_state"]

    alerts = hs.alerts()
    fut = [a for a in alerts if a["category"] == "future_timestamp"]
    assert len(fut) == 2, [(a["capability"], a["short_scope"]) for a in fut]
    assert all(a["severity"] == "high" for a in fut)
    assert sorted(a["capability"] for a in fut) == ["minute", "quote"]
    masked = next(a for a in fut if a["capability"] == "quote")
    assert masked["short_scope"] == "非法 Scope" and masked["symbol"] is None
    assert weird not in json.dumps(alerts, ensure_ascii=False)


# ---------------------------------------------------------------------- #
# 22. 项七：告警 ID 用**完整内部身份**生成（缩写碰撞不得导致 ID 碰撞）
# ---------------------------------------------------------------------- #
def test_alert_id_request_prefix_collision(tmp_path, fixed_clock):
    """两个 request_id 前 8 位相同、后续不同 → alert_id 必须不同。"""
    hs, svc, store, cache = _health(tmp_path, "600519.SH")
    rid_a = "abcdef01" + "0" * 23 + "1"
    rid_b = "abcdef01" + "0" * 23 + "2"
    assert rid_a[:8] == rid_b[:8] and rid_a != rid_b and len(rid_a) == 32
    for i, rid in enumerate((rid_a, rid_b)):
        _mk_stock_failed_request_file(
            tmp_path, rid,
            created_at=(fixed_clock - timedelta(hours=3)).isoformat(),
            recorded_at=(fixed_clock - timedelta(hours=1, minutes=i)).isoformat(),
            warning="处理超时（2 小时 worker_timeout），已失败",
            status_detail="worker_timeout", session=f"s{i}")
    wt = [a for a in hs.alerts() if a["category"] == "recent_worker_timeout"]
    assert len(wt) == 2, wt
    assert len({a["alert_id"] for a in wt}) == 2
    raw = json.dumps(wt, ensure_ascii=False)
    assert rid_a not in raw and rid_b not in raw
    assert "request_id" not in raw


def test_alert_id_q_scope_short_collision(tmp_path):
    """两个 q scope 缩写完全相同、完整值不同 → alert_id 必须不同。"""
    from app.westock_operations_service import _short_scope
    hs, svc, store, cache = _health(tmp_path, "600519.SH")
    q1 = "q_" + "a" * 5 + "b" * 55 + "c" * 4
    q2 = "q_" + "a" * 5 + "d" * 55 + "c" * 4
    assert len(q1) == 66 and len(q2) == 66 and q1 != q2
    assert _short_scope(q1) == _short_scope(q2), "构造前提：缩写必须碰撞"
    base = tmp_path / "state/dashboard/westock"
    for q in (q1, q2):
        p = base / "filter" / f"{q}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("{ broken json", encoding="utf-8")
    bad = [a for a in hs.alerts() if a["category"] == "invalid_cache_file"]
    assert len(bad) == 2, bad
    assert len({a["alert_id"] for a in bad}) == 2, "缩写碰撞不得导致 alert_id 碰撞"
    assert {a["short_scope"] for a in bad} == {_short_scope(q1)}
    raw = json.dumps(bad, ensure_ascii=False)
    assert q1 not in raw and q2 not in raw


# ---------------------------------------------------------------------- #
# 23. 项八：同一 API 调用只做一次只读扫描
# ---------------------------------------------------------------------- #
def _count_scans(monkeypatch):
    import app.westock_operations_service as ops_mod
    calls = {"n": 0}
    original = ops_mod.WestockOperationsService._scan

    def counting(self):
        calls["n"] += 1
        return original(self)

    monkeypatch.setattr(ops_mod.WestockOperationsService, "_scan", counting)
    return calls


def test_single_scan_per_public_call(tmp_path, monkeypatch):
    calls = _count_scans(monkeypatch)
    hs, svc, store, cache = _health(tmp_path, "600519.SH")
    _write_raw(tmp_path / "state/dashboard/westock", "quote", "600519.SH",
               {"sh600519": {"p": 1}}, _now().isoformat())
    cases = (
        ("health", lambda: hs.health()),
        ("alerts_api", lambda: hs.alerts_api({"limit": "50"})),
        ("recommendations_api", lambda: hs.recommendations_api({"limit": "50"})),
        ("trends_api", lambda: hs.trends_api({"window_days": "7"})),
    )
    for name, fn in cases:
        calls["n"] = 0
        fn()
        assert calls["n"] == 1, name + " 扫描次数应为 1，实际 " + str(calls["n"])


def test_single_scan_per_http_request(health_client, tmp_path, monkeypatch):
    calls = _count_scans(monkeypatch)
    _mk_curated(tmp_path, "600519.SH")
    _write_raw(tmp_path / "state/dashboard/westock", "quote", "600519.SH",
               {"sh600519": {"p": 1}}, _now().isoformat())
    for path in ("/api/connections/westock/health",
                 "/api/connections/westock/alerts?limit=50",
                 "/api/connections/westock/recommendations?limit=50",
                 "/api/connections/westock/trends?window_days=7"):
        calls["n"] = 0
        r = health_client.get(path)
        assert r.status_code == 200, path
        assert calls["n"] == 1, path + " 扫描次数应为 1，实际 " + str(calls["n"])


# ---------------------------------------------------------------------- #
# 24. 项九：唯一可注入时钟 —— recent 与趋势同源，无重复 _shanghai_today
# ---------------------------------------------------------------------- #
def test_single_injectable_clock_source(tmp_path, monkeypatch):
    import app.westock_health_service as hs_mod
    assert not hasattr(hs_mod, "_shanghai_today"), "模块级 _shanghai_today 必须已删除"
    hs, svc, store, cache = _health(tmp_path, "600519.SH")
    # 同一个 _utc_now 同时决定 recent 判定与趋势自然日
    for now, expect_end, expect_recent in (
        (datetime(2026, 8, 6, 4, 0, 0, tzinfo=timezone.utc), "2026-08-06", True),
        (datetime(2026, 8, 9, 4, 0, 0, tzinfo=timezone.utc), "2026-08-09", False),
    ):
        monkeypatch.setattr(hs_mod, "_utc_now", lambda now=now: now)
        assert hs._shanghai_today().isoformat() == expect_end
        assert hs.trends(7)["end_date"] == expect_end
        assert hs._is_recent("2026-08-06T03:00:00+00:00",
                             hs_mod.RECENT_WINDOW_HOURS) is expect_recent


# ---------------------------------------------------------------------- #
# 25. 项一（后端侧）：建议携带**真实可预填** capabilities，且不带 preset
# ---------------------------------------------------------------------- #
def test_recommendation_prefill_payload_is_real(tmp_path):
    from app.westock_refresh_service import _STOCK_CAPS, SYMBOL_RE
    hs, svc, store, cache = _health(tmp_path, "600519.SH")
    recs = hs.recommendations()
    stock = [r for r in recs if r["target_kind"] == "stock" and r["can_prefill_refresh"]]
    assert stock, [(r["code"], r["target_kind"]) for r in recs]
    for r in stock:
        assert r["preset"] is None, "stock 预填只走 capabilities 模式，不得同时发 preset"
        assert r["capabilities"], r
        assert len(r["capabilities"]) <= 20
        assert r["capabilities"] == sorted(dict.fromkeys(r["capabilities"]))
        assert all(c in _STOCK_CAPS for c in r["capabilities"])
        assert all(SYMBOL_RE.fullmatch(s) for s in r["symbols"])
        assert r["allow_summary_only"] is False  # 本地股票
    market = [x for x in recs if x["target_kind"] == "market"]
    assert market, [(r["code"], r["target_kind"]) for r in recs]
    for r in market:
        assert r["preset"] and not r["capabilities"], r
        assert r["allow_summary_only"] is False
        assert r["symbols"] == []


def test_recommendation_non_local_symbol_allows_summary_only(tmp_path):
    """非本地股票的预填必须 allow_summary_only=True（否则 F3 必然拒绝）。"""
    hs, svc, store, cache = _health(tmp_path, "600519.SH")
    base = tmp_path / "state/dashboard/westock"
    # 000001.SZ 非本地：写入其缓存文件使其进入预期矩阵
    _write_raw(base, "quote", "000001.SZ", {"bad": "shape"}, _now().isoformat())
    recs = hs.recommendations()
    hit = [r for r in recs if r["symbols"] == ["000001.SZ"]]
    assert hit, [r["symbols"] for r in recs]
    assert all(r["allow_summary_only"] is True for r in hit)


def test_screener_recommendation_never_prefills(tmp_path):
    """screener 无 result_id → 不可预填、必须走 WorkBuddy 重新导出。"""
    hs, svc, store, cache = _health(tmp_path, "600519.SH")
    q = "q_" + "f" * 64
    _mk_request_file_screener(tmp_path, f"{0x17:032x}",
                              "2026-08-06T00:00:00+00:00", q, capability="filter")
    recs = hs.recommendations()
    scr = [r for r in recs if r["code"] == "rerun_screener_export"]
    assert scr, [r["code"] for r in recs]
    for r in scr:
        assert r["can_prefill_refresh"] is False
        assert r["requires_workbuddy"] is True
        assert r["capabilities"] == [] and r["symbols"] == []
        assert r["preset"] is None
    assert q not in json.dumps(recs, ensure_ascii=False)
