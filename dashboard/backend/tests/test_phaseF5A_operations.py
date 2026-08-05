"""F5-A（审查修正版）：Westock 运营只读测试。

覆盖规格十后端矩阵：inventory/matrix 分离、raw 五类文件状态、hash 证据链、
capabilities/symbols/requests 真分页过滤、超 50 请求聚合、failure job/request 分离、
as_of lag 确定性、validator 无副作用、只读哈希证明、API 校验与零泄漏。
"""
import json
import hashlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.westock_bridge import WestockCacheStore
from app.westock_refresh_service import (
    build_refresh_store, canonical_request_hash, RefreshError,
)
from app.westock_operations_service import (
    build_operations_service, _category_from_warning, _shanghai_today,
)

CSRF_COOKIE = "ashare_dash_csrf"


# ---------------------------------------------------------------------- #
# 工具
# ---------------------------------------------------------------------- #
def _now():
    return datetime.now(timezone.utc)


def _mk_curated(tmp_path, *symbols) -> Path:
    d = tmp_path / "data" / "curated"
    d.mkdir(parents=True, exist_ok=True)
    for sym in symbols:
        (d / f"daily_quotes_{sym}_2026.parquet").write_bytes(b"x")
    return d


def _tech_payload(code: str = "sh600519") -> dict:
    return {"sh600519": {
        "code": code, "date": "2026-08-05", "closePrice": 1300.0,
        "ma": {"MA_5": 1.0, "MA_10": 1.0, "MA_20": 1.0, "MA_60": 1.0},
        "macd": {"DIF": 1.0, "DEA": 1.0, "MACD": 1.0},
        "kdj": {"KDJ_K": 1.0, "KDJ_D": 1.0, "KDJ_J": 1.0},
        "rsi": {"RSI_6": 1.0, "RSI_12": 1.0, "RSI_24": 1.0},
        "boll": {"BOLL_UPPER": 1.0, "BOLL_MID": 1.0, "BOLL_LOWER": 1.0},
    }}


def _write_raw(base: Path, capability: str, scope: str, data, fetched_at, as_of=None):
    p = base / capability / f"{scope}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    env = {
        "schema_version": 1, "capability": capability, "scope": scope,
        "tool": {
            "quote": "data_quote", "technical": "data_technical", "news": "data_news",
            "margin": "data_fund_margin", "reports": "data_report", "lhb": "data_lhb",
            "filter": "tool_filter", "profile": "data_profile", "minute": "data_minute",
            "market_overview": "data_market_overview",
        }.get(capability, "data_quote"),
        "transport": "cache_export", "source": "westock-mcp",
        "fetched_at": fetched_at, "cached_at": fetched_at, "as_of": as_of,
        "data": data, "warnings": [],
    }
    p.write_text(json.dumps(env, ensure_ascii=False), encoding="utf-8")


def _data_hash(data) -> str:
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True)
                          .encode("utf-8")).hexdigest()


def _store(tmp_path, *symbols):
    curated = _mk_curated(tmp_path, *symbols)
    return build_refresh_store(tmp_path), WestockCacheStore(tmp_path / "state/dashboard/westock"), curated


def _svc(tmp_path, *symbols):
    store, cache, curated = _store(tmp_path, *symbols)
    return build_operations_service(tmp_path, cache, store), store, cache


def _mk_request(store, session: str, complete: str | None = "ok",
                preset: str = "overview", content_hash: str | None = None):
    rid = store.create_request(body={"target": "market", "preset": preset},
                               session_id=session)["request_id"]
    store.claim(rid, "a" * 64)
    jobs = store._read_request_file(rid)["jobs"]
    for j in jobs:
        if complete == "ok":
            store.complete_job(rid, j["job_id"], "ok", export_info={
                "fetched_at": _now().isoformat(), "cache_status": "fresh",
                "data_as_of": "2026-08-05", "content_hash": content_hash or ("b" * 64)})
        elif complete == "failed":
            store.complete_job(rid, j["job_id"], "failed", warning="上游无数据")
    if complete:
        store.finish(rid)
    return rid


def _mk_request_file(tmp_path, rid_hex, created_at: str, session="s"):
    """手写合法 pending 请求文件（聚合全量测试用，不限流）。"""
    store = build_refresh_store(tmp_path)
    target = {"kind": "market", "preset": "overview"}
    request = {
        "schema_version": 2, "request_id": rid_hex, "created_at": created_at,
        "updated_at": created_at, "status": "pending", "target": target,
        "jobs": [
            {"job_id": f"{i:032x}", "capability": cap, "scope": "global",
             "status": "pending", "summary_only": False}
            for i, cap in enumerate(("market_overview", "change_distribution", "hot_ranking"))
        ],
        "request_hash": canonical_request_hash(target),
        "session_fingerprint": hashlib.sha256(session.encode()).hexdigest(),
        "attempts": 0, "worker_id": None, "claimed_at": None, "started_at": None,
        "finished_at": None,
        "expires_at": (datetime.fromisoformat(created_at) + timedelta(hours=24)).isoformat(),
        "warnings": [], "status_detail": None,
    }
    p = store.requests_dir / f"{rid_hex}.json"
    p.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")


def _dir_hashes(root: Path) -> dict[str, str]:
    out = {}
    if not root.exists():
        return out
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


@pytest.fixture()
def ops_client(tmp_path, config_factory, fake_executor):
    from fastapi.testclient import TestClient
    from app.main import create_app
    app = create_app(config_factory(project_root=str(tmp_path)),
                     enable_static=False, executor=fake_executor)
    with TestClient(app, base_url="https://127.0.0.1") as c:
        resp = c.post("/api/auth/login", json={"username": "admin", "password": "secret123"})
        assert resp.status_code == 200, resp.text
        yield c


# ---------------------------------------------------------------------- #
# 一、矩阵：空缓存 + 本地 curated → expected>0 且 unavailable 正确
# ---------------------------------------------------------------------- #
def test_matrix_empty_cache_with_curated(tmp_path):
    svc, store, cache = _svc(tmp_path, "600519.SH", "000001.SZ")
    s = svc.summary()
    from app.westock_refresh_service import _GLOBAL_SHOW_CAPS, _STOCK_CAPS
    expected = 2 * len(_STOCK_CAPS) + len(_GLOBAL_SHOW_CAPS)
    assert s["physical_cache_count"] == 0
    assert s["expected_cell_count"] == expected
    assert s["availability"]["available"] == 0
    assert s["availability"]["unavailable"] == expected
    # 恒等式：fresh+stale+future+invalid+unavailable == expected
    f = s["freshness"]
    assert (f["fresh"] + f["stale"] + f["future_timestamp"] + f["invalid_timestamp"]
            + f["unavailable"]) == expected
    assert s["valid_coverage"] == 0.0
    # 缺失 cell 均产生受控记录（limit 放宽避免默认 50 截断）
    entries = svc.caches({"limit": "200"})["items"]
    assert len(entries) == expected
    assert all(e["availability"] == "unavailable" for e in entries)
    assert all(e["integrity"]["valid"] is False for e in entries)
    assert all(e["fetched_at"] is None and e["as_of"] is None and e["age_seconds"] is None
               for e in entries)


# ---------------------------------------------------------------------- #
# 二、summary-only 无缓存也进矩阵且不含 minute/technical
# ---------------------------------------------------------------------- #
def test_summary_only_cells_without_cache(tmp_path):
    svc, store, cache = _svc(tmp_path, "600519.SH")
    store.create_request(body={"target": "stock", "preset": "full_research",
                               "symbols": ["601318.SH"], "allow_summary_only": True},
                         session_id="s1")
    cells = svc._coverage_cells(list(svc._iter_requests()))
    assert ("quote", "601318.SH") in cells
    assert ("minute", "601318.SH") not in cells
    assert ("technical", "601318.SH") not in cells
    entries = {e["capability"]: e for e in svc.caches({})["items"]
               if e["scope"] == "601318.SH"}
    assert entries["quote"]["availability"] == "unavailable"
    assert entries["quote"]["summary_only"] is True


# ---------------------------------------------------------------------- #
# 三、hash 证据链：verified / unverified / mismatch / 不泄露
# ---------------------------------------------------------------------- #
def test_hash_evidence_chain(tmp_path):
    svc, store, cache = _svc(tmp_path, "600519.SH")
    base = tmp_path / "state/dashboard/westock"
    tech = _tech_payload()
    _write_raw(base, "technical", "600519.SH", tech, _now().isoformat())
    # 无 job 证据 → unverified
    e = next(x for x in svc.caches({"limit": "200"})["items"] if x["capability"] == "technical")
    assert e["integrity"]["hash_status"] == "unverified"
    assert e["integrity"]["hash_verified"] is False
    # 匹配 job → verified（job content_hash == 缓存 data hash）
    h = _data_hash(tech)
    rid = store.create_request(body={"target": "stock", "preset": "market_data",
                                     "symbols": ["600519.SH"]}, session_id="s2")["request_id"]
    store.claim(rid, "a" * 64)
    for job in store._read_request_file(rid)["jobs"]:
        store.complete_job(rid, job["job_id"], "ok", export_info={
            "fetched_at": _now().isoformat(), "cache_status": "fresh",
            "data_as_of": "2026-08-05", "content_hash": h})
    store.finish(rid)
    e2 = next(x for x in svc.caches({"limit": "200"})["items"] if x["capability"] == "technical")
    assert e2["integrity"]["hash_status"] == "verified"
    assert e2["integrity"]["hash_verified"] is True
    # 不匹配 job → mismatch（并进入异常统计）
    rid2 = store.create_request(body={"target": "stock", "preset": "market_data",
                                      "symbols": ["600519.SH"]}, session_id="s3")["request_id"]
    store.claim(rid2, "a" * 64)
    for job in store._read_request_file(rid2)["jobs"]:
        store.complete_job(rid2, job["job_id"], "ok", export_info={
            "fetched_at": _now().isoformat(), "cache_status": "fresh",
            "data_as_of": "2026-08-05", "content_hash": "c" * 64})
    store.finish(rid2)
    svc2, _, _ = _svc(tmp_path, "600519.SH")
    e3 = next(x for x in svc2.caches({"limit": "200"})["items"] if x["capability"] == "technical")
    assert e3["integrity"]["hash_status"] == "mismatch"
    assert e3["integrity"]["hash_verified"] is False
    assert svc2.summary()["integrity"]["hash_mismatch"] == 1


def test_no_content_hash_leak(ops_client):
    r = ops_client.get("/api/connections/westock/operations/caches?limit=5")
    assert r.status_code == 200
    raw = json.dumps(r.json())
    assert "content_hash" not in raw


# ---------------------------------------------------------------------- #
# 四、raw 五类文件状态
# ---------------------------------------------------------------------- #
def test_raw_file_states_matrix(tmp_path):
    svc, store, cache = _svc(tmp_path, "600519.SH")
    base = tmp_path / "state/dashboard/westock"
    now = _now()
    _write_raw(base, "quote", "600519.SH", {"sh600519": {"p": 1}},
               (now - timedelta(seconds=10)).isoformat())       # valid → fresh
    _write_raw(base, "margin", "600519.SH", {}, (now - timedelta(hours=1)).isoformat())  # stale
    _write_raw(base, "news", "600519.SH", {}, (now + timedelta(hours=1)).isoformat())    # future
    _write_raw(base, "reports", "600519.SH", {}, "not-a-timestamp")                      # invalid_timestamp
    p = base / "lhb/global.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{broken", encoding="utf-8")                                            # invalid_json
    p2 = base / "profile/600519.SH.json"
    p2.parent.mkdir(parents=True, exist_ok=True)
    env = json.loads((base / "quote/600519.SH.json").read_text(encoding="utf-8"))
    env["transport"] = "evil"
    p2.write_text(json.dumps(env), encoding="utf-8")                                      # invalid_envelope
    states = {e["capability"]: e["file_state"] for e in svc.caches({})["items"]
              if e["file_state"] != "missing"}
    assert states["quote"] == "valid" and states["margin"] == "valid"
    assert states["news"] == "future_timestamp"
    assert states["reports"] == "invalid_timestamp"
    assert states["lhb"] == "invalid_json"
    assert states["profile"] == "invalid_envelope"
    f = svc.summary()["freshness"]
    assert f["fresh"] >= 1 and f["stale"] >= 1
    assert f["future_timestamp"] == 1 and f["invalid_timestamp"] == 1
    assert f["unavailable"] >= 2


# ---------------------------------------------------------------------- #
# 五、capabilities/symbols/requests 分页过滤
# ---------------------------------------------------------------------- #
def test_capabilities_pagination_filter(tmp_path):
    svc, store, cache = _svc(tmp_path, "600519.SH", "000001.SZ")
    _write_raw(tmp_path / "state/dashboard/westock", "quote", "600519.SH",
               {"sh600519": {"p": 1}}, _now().isoformat())
    r = svc.capabilities({"capability": "quote"})
    assert r["total"] == 1 and len(r["items"]) == 1
    assert r["items"][0]["capability"] == "quote"
    r2 = svc.capabilities({"limit": "5", "offset": "0"})
    assert r2["total"] > 1 and len(r2["items"]) == min(5, r2["total"])
    r3 = svc.capabilities({"limit": "5", "offset": "999"})
    assert r3["items"] == []
    # API 越界
    assert ops_400(svc, "capabilities", {"limit": "0"})
    assert ops_400(svc, "capabilities", {"limit": "201"})
    assert ops_400(svc, "capabilities", {"capability": "nope"})


def ops_400(svc, kind, params) -> bool:
    try:
        getattr(svc, kind)(params)
        return False
    except RefreshError as exc:
        return exc.status_code == 400


def test_symbols_pagination_filter(tmp_path):
    svc, store, cache = _svc(tmp_path, "600519.SH", "000001.SZ")
    _write_raw(tmp_path / "state/dashboard/westock", "quote", "600519.SH",
               {"sh600519": {"p": 1}}, _now().isoformat())
    r = svc.symbols({"symbol": "600519.SH"})
    assert r["total"] == 1 and r["items"][0]["symbol"] == "600519.SH"
    r2 = svc.symbols({"limit": "1", "offset": "0"})
    assert len(r2["items"]) == 1 and r2["total"] == 2
    assert ops_400(svc, "symbols", {"symbol": "abc"})
    assert ops_400(svc, "symbols", {"offset": "-1"})


def test_requests_sort_pagination_and_aggregate_over_50(tmp_path):
    svc, store, cache = _svc(tmp_path)
    # 55 个合法请求（手写文件，绕开限流）
    for i in range(55):
        rid = f"{i:032x}"
        created = (datetime(2026, 8, 6, 0, 0, 0, tzinfo=timezone.utc)
                   - timedelta(minutes=55 - i)).isoformat()
        _mk_request_file(tmp_path, rid, created, session=f"s{i}")
    r = svc.requests({"limit": "10", "offset": "0"})
    assert r["total"] == 55
    # created_at 倒序：最新在前
    times = [x["created_at"] for x in r["items"]]
    assert times == sorted(times, reverse=True)
    assert r["items"][0]["request_id"] == f"{54:032x}"
    # aggregate 全量（不受 limit=50 截断）
    agg = svc.request_aggregate()
    assert agg["total"] == 55
    assert agg["status_counts"]["pending"] == 55


def test_summary_failures_reject_query_params(ops_client):
    r = ops_client.get("/api/connections/westock/operations/summary?x=1")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_request"
    r = ops_client.get("/api/connections/westock/operations/failures?x=1")
    assert r.status_code == 400


# ---------------------------------------------------------------------- #
# 六、时间：latest_ok/fail 来自 job recorded_at；duration 优先 started→finished
# ---------------------------------------------------------------------- #
def test_latest_job_times_and_duration(tmp_path):
    svc, store, cache = _svc(tmp_path, "600519.SH")
    base = tmp_path / "state/dashboard/westock"
    _write_raw(base, "quote", "600519.SH", {"sh600519": {"p": 1}},
               (datetime(2026, 8, 1).replace(tzinfo=timezone.utc)).isoformat())
    rid = store.create_request(body={"target": "stock", "preset": "quote_only",
                                     "symbols": ["600519.SH"]}, session_id="s1")["request_id"]
    store.claim(rid, "a" * 64)
    job = next(j for j in store._read_request_file(rid)["jobs"] if j["capability"] == "quote")
    store.complete_job(rid, job["job_id"], "ok", export_info={
        "fetched_at": _now().isoformat(), "cache_status": "fresh",
        "data_as_of": "2026-08-05", "content_hash": "b" * 64})
    store.finish(rid)
    cap = svc.capabilities({"capability": "quote"})["items"][0]
    # latest_ok_at 来自 job recorded_at（≈现在），不是缓存 fetched_at（08-01）
    assert cap["latest_ok_at"] and cap["latest_ok_at"] > "2026-08-05"
    # duration 优先 started_at→finished_at
    rows = svc.requests({})["items"]
    assert rows[0]["duration_seconds"] is not None


# ---------------------------------------------------------------------- #
# 七、failure job/request 分离
# ---------------------------------------------------------------------- #
def test_failure_job_request_separation(tmp_path):
    svc, store, cache = _svc(tmp_path)
    rid = store.create_request(body={"target": "market", "preset": "overview"},
                               session_id="s1")["request_id"]
    store.claim(rid, "a" * 64)
    jobs = store._read_request_file(rid)["jobs"]
    assert len(jobs) == 3
    for j in jobs:
        store.complete_job(rid, j["job_id"], "failed", warning="上游无数据")
    # 请求级 worker_timeout（模拟 status_detail）
    store.finish(rid)
    p = store.requests_dir / f"{rid}.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    data["status_detail"] = "worker_timeout"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    f = svc.failures()
    assert f["failed_job_count"] == 3
    assert f["job_failure_categories"]["upstream_empty"] == 3  # job 分类按自身 warning
    assert f["failed_request_count"] == 1
    assert f["request_failure_categories"]["worker_timeout"] == 1  # timeout 仅在请求级计 1 次
    assert f["job_failure_categories"]["worker_timeout"] == 0  # 不产生含义不明的 4 次 timeout


# ---------------------------------------------------------------------- #
# 八、as_of lag 确定性
# ---------------------------------------------------------------------- #
def test_as_of_lag_deterministic(tmp_path):
    svc, store, cache = _svc(tmp_path, "600519.SH")
    base = tmp_path / "state/dashboard/westock"
    now = _now().isoformat()
    # 同 capability 两个 scope 不同 as_of（写入顺序故意反序，验证 max 稳定）
    _write_raw(base, "quote", "600519.SH", {"sh600519": {"p": 1}}, now, as_of="2026-08-01")
    _write_raw(base, "quote", "000001.SZ", {"sz000001": {"p": 1}}, now, as_of="2026-08-05")
    lag1 = svc.summary()["as_of_lag"]
    # 反转写入顺序（结果应相同）
    base2 = tmp_path / "state/dashboard/westock2"
    svc2, _, _ = _svc2(tmp_path, base2)
    lag2 = svc2.summary()["as_of_lag"]
    assert lag1["per_capability"]["quote"]["as_of"] == "2026-08-05"
    assert lag2["per_capability"]["quote"]["as_of"] == "2026-08-05"
    assert lag1["per_capability"]["quote"]["lag_days"] == lag2["per_capability"]["quote"]["lag_days"]
    # 缺日期 → unknown
    assert lag1["unknown_count"] >= 0


def _svc2(tmp_path, base2):
    store2 = build_refresh_store(tmp_path)
    cache2 = WestockCacheStore(base2)
    base = tmp_path / "state/dashboard/westock2"
    now = _now().isoformat()
    _write_raw(base, "quote", "000001.SZ", {"sz000001": {"p": 1}}, now, as_of="2026-08-05")
    _write_raw(base, "quote", "600519.SH", {"sh600519": {"p": 1}}, now, as_of="2026-08-01")
    return build_operations_service(tmp_path, cache2, store2), store2, cache2


# ---------------------------------------------------------------------- #
# 九、只读 + validator 无副作用
# ---------------------------------------------------------------------- #
def test_readonly_no_side_effect(tmp_path):
    svc, store, cache = _svc(tmp_path, "600519.SH")
    base = tmp_path / "state/dashboard/westock"
    tech = _tech_payload()
    _write_raw(base, "technical", "600519.SH", tech, _now().isoformat())
    before_files = set(p for p in (tmp_path / "state").rglob("*") if p.is_file())
    before_hashes = _dir_hashes(tmp_path / "state")
    data_before = (base / "technical" / "600519.SH.json").read_bytes()
    r1 = json.dumps(svc.caches({})["items"], sort_keys=True)
    svc.summary()
    svc.capabilities({})
    svc.symbols({})
    svc.failures()
    r2 = json.dumps(svc.caches({})["items"], sort_keys=True)
    assert r1 == r2  # 连续调用一致（deep copy 无副作用）
    after_files = set(p for p in (tmp_path / "state").rglob("*") if p.is_file())
    assert before_files == after_files  # 不创建/删除任何文件
    assert _dir_hashes(tmp_path / "state") == before_hashes  # 文件内容不变
    assert (base / "technical" / "600519.SH.json").read_bytes() == data_before


def test_readonly_api_hash_proof(ops_client, tmp_path):
    store, cache, curated = _store(tmp_path, "600519.SH")
    _write_raw(tmp_path / "state/dashboard/westock", "quote", "600519.SH",
               {"sh600519": {"p": 1}}, _now().isoformat())
    before = _dir_hashes(tmp_path / "state") | _dir_hashes(tmp_path / "data")
    for path in ("summary", "caches?limit=5", "capabilities", "symbols",
                 "requests?limit=5", "failures"):
        r = ops_client.get(f"/api/connections/westock/operations/{path}")
        assert r.status_code == 200, path
    after = _dir_hashes(tmp_path / "state") | _dir_hashes(tmp_path / "data")
    assert before == after


# ---------------------------------------------------------------------- #
# 十、过滤校验 / 401 / 泄漏
# ---------------------------------------------------------------------- #
def test_filter_validation(ops_client):
    for q in ("limit=0", "limit=201", "offset=-1", "symbol=abc",
              "capability=nope", "scope_type=foo", "freshness=zzz"):
        r = ops_client.get(f"/api/connections/westock/operations/caches?{q}")
        assert r.status_code == 400, q
    r = ops_client.get("/api/connections/westock/operations/caches?hacked=1")
    assert r.status_code == 400


def test_unauthorized_401():
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
        for path in ("/api/connections/westock/operations/summary",
                     "/api/connections/westock/operations/caches",
                     "/api/connections/westock/operations/capabilities",
                     "/api/connections/westock/operations/symbols",
                     "/api/connections/westock/operations/requests",
                     "/api/connections/westock/operations/failures"):
            assert c.get(path).status_code == 401, path


def test_no_leakage(ops_client):
    for path in ("/api/connections/westock/operations/summary",
                 "/api/connections/westock/operations/caches?limit=5",
                 "/api/connections/westock/operations/requests?limit=5",
                 "/api/connections/westock/operations/failures"):
        r = ops_client.get(path)
        assert r.status_code == 200
        raw = json.dumps(r.json())
        for bad in ("token", "secret", "password", "cookie", "Authorization",
                    "worker_id", "session_fingerprint", "content_hash",
                    "C:\\", "state/dashboard", "Traceback"):
            assert bad.lower() not in raw.lower(), (path, bad)


# ---------------------------------------------------------------------- #
# 十一、计数恒等式 + failure warning 映射
# ---------------------------------------------------------------------- #
def test_count_identity(tmp_path):
    svc, store, cache = _svc(tmp_path, "600519.SH")
    base = tmp_path / "state/dashboard/westock"
    now = _now()
    _write_raw(base, "quote", "600519.SH", {"sh600519": {"p": 1}},
               (now - timedelta(seconds=10)).isoformat())
    _write_raw(base, "margin", "600519.SH", {}, (now - timedelta(hours=1)).isoformat())
    s = svc.summary()
    f = s["freshness"]
    assert (f["fresh"] + f["stale"] + f["future_timestamp"] + f["invalid_timestamp"]
            + f["unavailable"]) == s["expected_cell_count"]
    a = s["availability"]
    assert a["available"] + a["unavailable"] == s["expected_cell_count"]
    c = s["consumer_status"]
    assert c["usable"] + c["unusable"] + c["not_validated"] == s["expected_cell_count"]
    raw = json.dumps(s)
    assert "NaN" not in raw and "Infinity" not in raw


def test_failure_warning_mapping():
    assert _category_from_warning("上游无数据") == "upstream_empty"
    assert _category_from_warning("worker_timeout 超时") == "worker_timeout"
    assert _category_from_warning("consumer_validation_failed") == "consumer_validation_failed"
    assert _category_from_warning("上游限频") == "upstream_rate_limited"
    assert _category_from_warning("未导出") == "export_failed"
    assert _category_from_warning("不支持") == "unsupported"
    assert _category_from_warning("身份不一致") == "identity_mismatch"
    assert _category_from_warning("随便一句话") == "unknown"
    assert _category_from_warning("") == "unknown"


# ====================================================================== #
# 第二轮定点修正：验收矩阵
# ====================================================================== #
def _mk_screener_request_file(tmp_path, rid_hex, created_at: str, q_scope: str,
                              capability: str = "filter", session: str = "s"):
    """手写合法 pending screener 请求（q_<64hex> scope；不依赖 screener_store）。"""
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


def _mk_completed_request_file(tmp_path, rid_hex, created_at: str,
                               status: str = "completed", session: str = "s"):
    """手写合法终态 market 请求文件（3 ok jobs；可选配 receipt）。"""
    store = build_refresh_store(tmp_path)
    target = {"kind": "market", "preset": "overview"}
    request = {
        "schema_version": 2, "request_id": rid_hex, "created_at": created_at,
        "updated_at": created_at, "status": status, "target": target,
        "jobs": [
            {"job_id": f"{i:032x}", "capability": cap, "scope": "global",
             "status": "ok", "summary_only": False,
             "fetched_at": created_at, "cache_status": "fresh",
             "data_as_of": "2026-08-05", "content_hash": "b" * 64,
             "recorded_at": created_at}
            for i, cap in enumerate(("market_overview", "change_distribution", "hot_ranking"))
        ],
        "request_hash": canonical_request_hash(target),
        "session_fingerprint": hashlib.sha256(session.encode()).hexdigest(),
        "attempts": 0, "worker_id": "a" * 64, "claimed_at": created_at,
        "started_at": created_at, "finished_at": created_at,
        "expires_at": (datetime.fromisoformat(created_at) + timedelta(hours=24)).isoformat(),
        "warnings": [], "status_detail": "3 项能力全部完成",
    }
    p = store.requests_dir / f"{rid_hex}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")


def _mk_receipt(tmp_path, rid_hex, status: str = "completed",
                job_statuses: tuple = ("ok", "ok", "ok")):
    """手写合法 receipt（job 状态可自定义，用于 mismatch 构造）。"""
    store = build_refresh_store(tmp_path)
    target = {"kind": "market", "preset": "overview"}
    caps = ("market_overview", "change_distribution", "hot_ranking")
    jobs = [
        {"job_id": f"{i:032x}", "capability": caps[i], "scope": "global",
         "status": s, "summary_only": False,
         "fetched_at": "2026-08-06T00:01:00+00:00", "cache_status": "fresh",
         "data_as_of": "2026-08-05", "content_hash": "b" * 64,
         "recorded_at": "2026-08-06T00:02:00+00:00"}
        for i, s in enumerate(job_statuses)
    ]
    receipt = {
        "schema_version": 2, "request_id": rid_hex, "target": target, "jobs": jobs,
        "created_at": "2026-08-06T00:00:00+00:00",
        "started_at": "2026-08-06T00:01:00+00:00",
        "finished_at": "2026-08-06T00:02:00+00:00",
        "status": status, "status_detail": "ok", "warnings": [],
    }
    p = store.receipts_dir / f"{rid_hex}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")


# --- 1. capabilities/symbols 真实 API 平铺 envelope ---
def test_api_envelope_flat_pagination(ops_client, tmp_path):
    _mk_curated(tmp_path, "600519.SH")
    _write_raw(tmp_path / "state/dashboard/westock", "quote", "600519.SH",
               {"sh600519": {"p": 1}}, _now().isoformat())
    for path in ("capabilities", "symbols"):
        r = ops_client.get(f"/api/connections/westock/operations/{path}")
        assert r.status_code == 200, path
        d = r.json()["data"]
        assert set(d) == {"total", "limit", "offset", "items"}, path
        assert isinstance(d["items"], list), path
        # 不得双重嵌套：data.items 不是 {total, limit, offset, items}
        assert not (d["items"] and isinstance(d["items"][0], dict)
                    and "items" in d["items"][0] and "total" in d["items"][0])
    d = ops_client.get("/api/connections/westock/operations/capabilities").json()["data"]
    assert d["total"] >= 1 and len(d["items"]) == d["total"]
    d2 = ops_client.get("/api/connections/westock/operations/symbols").json()["data"]
    assert d2["total"] >= 1 and d2["items"][0]["symbol"] == "600519.SH"


# --- 2. future/invalid timestamp：不可用、不校验、不算年龄、hash unverified ---
def test_future_invalid_not_available(tmp_path):
    svc, store, cache = _svc(tmp_path, "600519.SH")
    base = tmp_path / "state/dashboard/westock"
    now = _now()
    # future：fetched_at/cached_at 均在未来
    _write_raw(base, "news", "600519.SH", {}, (now + timedelta(hours=1)).isoformat())
    # invalid_timestamp：fetched_at 非法（data 是坏 quote，若被 validator 会 unusable）
    _write_raw(base, "quote", "600519.SH", {"bad": "shape"},
               "not-a-timestamp")
    entries = {e["capability"]: e for e in svc.caches({"limit": "200"})["items"]}
    for cap in ("news", "quote"):
        e = entries[cap]
        assert e["availability"] == "unavailable", cap
        assert e["integrity"]["valid"] is False, cap
        assert e["integrity"]["hash_status"] == "unverified", cap
        assert e["consumer_status"] == "not_validated", cap  # validator 未被调用
        assert e["age_seconds"] is None and e["expires_at"] is None, cap
    assert entries["news"]["file_state"] == "future_timestamp"
    assert entries["quote"]["file_state"] == "invalid_timestamp"
    s = svc.summary()
    assert s["availability"]["available"] == 0
    assert s["freshness"]["future_timestamp"] == 1
    assert s["freshness"]["invalid_timestamp"] == 1


# --- 3. cached_at 非法 → invalid_timestamp ---
def test_invalid_cached_at(tmp_path):
    svc, store, cache = _svc(tmp_path, "600519.SH")
    base = tmp_path / "state/dashboard/westock"
    env = _env_dict("quote", "600519.SH", {"sh600519": {"p": 1}},
                    _now().isoformat())
    env["cached_at"] = "not-a-time"
    p = base / "quote/600519.SH.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(env, ensure_ascii=False), encoding="utf-8")
    e = next(x for x in svc.caches({"limit": "200"})["items"] if x["capability"] == "quote")
    assert e["file_state"] == "invalid_timestamp"
    assert e["availability"] == "unavailable"
    assert e["integrity"]["valid"] is False


def _env_dict(capability, scope, data, fetched_at, as_of=None):
    return {
        "schema_version": 1, "capability": capability, "scope": scope,
        "tool": {
            "quote": "data_quote", "technical": "data_technical", "news": "data_news",
            "margin": "data_fund_margin", "reports": "data_report", "lhb": "data_lhb",
            "filter": "tool_filter", "profile": "data_profile", "minute": "data_minute",
            "market_overview": "data_market_overview",
        }.get(capability, "data_quote"),
        "transport": "cache_export", "source": "westock-mcp",
        "fetched_at": fetched_at, "cached_at": fetched_at, "as_of": as_of,
        "data": data, "warnings": [],
    }


# --- 4. unexpected 物理文件不破坏 coverage 恒等式 ---
def test_unexpected_physical_does_not_break_identity(tmp_path):
    from app.westock_refresh_service import _GLOBAL_SHOW_CAPS, _STOCK_CAPS
    svc, store, cache = _svc(tmp_path, "600519.SH")
    base = tmp_path / "state/dashboard/westock"
    now = _now()
    _write_raw(base, "quote", "600519.SH", {"sh600519": {"p": 1}},
               (now - timedelta(seconds=10)).isoformat())
    # unexpected：合法 capability 目录下的意外 scope 文件（合法 envelope）
    _write_raw(base, "quote", "weird_scope", {"x": 1},
               (now - timedelta(seconds=5)).isoformat())
    # unexpected + invalid physical：坏 JSON
    (base / "quote/also_weird.json").write_text("{broken", encoding="utf-8")
    s = svc.summary()
    expected = len(_STOCK_CAPS) + len(_GLOBAL_SHOW_CAPS)
    assert s["expected_cell_count"] == expected          # 预期矩阵不变
    assert s["physical_cache_count"] == 3                # 1 正常 + 2 意外
    assert s["unexpected_physical_count"] == 2
    assert s["invalid_physical_count"] == 1
    assert s["total_cells"] == expected                  # 恒等式分母 = expected
    f = s["freshness"]
    assert (f["fresh"] + f["stale"] + f["future_timestamp"] + f["invalid_timestamp"]
            + f["unavailable"]) == expected
    a = s["availability"]
    assert a["available"] + a["unavailable"] == expected
    # caches 双口径
    c = svc.caches({"limit": "200"})
    assert c["coverage_total"] == expected
    assert c["inventory_total"] == 3
    assert c["unexpected_physical_count"] == 2
    assert sum(1 for x in c["items"] if x["in_expected_matrix"]) == expected
    assert sum(1 for x in c["items"] if not x["in_expected_matrix"]) == 2


# --- 5. hash 证据顺序：old ok + newer failed → verified；newer ok 为准 ---
def _run_stock_jobs(store, preset, symbols, result, content_hash=None, session="s"):
    rid = store.create_request(body={"target": "stock", "preset": preset,
                                     "symbols": symbols},
                               session_id=session)["request_id"]
    store.claim(rid, "a" * 64)
    for j in store._read_request_file(rid)["jobs"]:
        if result == "ok":
            store.complete_job(rid, j["job_id"], "ok", export_info={
                "fetched_at": _now().isoformat(), "cache_status": "fresh",
                "data_as_of": "2026-08-05", "content_hash": content_hash})
        else:
            store.complete_job(rid, j["job_id"], "failed", warning="上游无数据")
    store.finish(rid)
    return rid


def test_hash_evidence_order_old_ok_newer_failed(tmp_path):
    svc, store, cache = _svc(tmp_path, "600519.SH")
    base = tmp_path / "state/dashboard/westock"
    tech = _tech_payload()
    _write_raw(base, "technical", "600519.SH", tech, _now().isoformat())
    h = _data_hash(tech)
    # 1) old ok + newer failed：缓存匹配 old ok → verified；最近刷新状态仍 failed
    _run_stock_jobs(store, "market_data", ["600519.SH"], "ok", h, session="s1")
    _run_stock_jobs(store, "market_data", ["600519.SH"], "failed", session="s2")
    e = next(x for x in svc.caches({"limit": "200"})["items"]
             if x["capability"] == "technical")
    assert e["integrity"]["hash_status"] == "verified"
    assert e["integrity"]["hash_verified"] is True
    assert e["last_refresh_status"] == "failed"
    # 2) newer ok 不同 hash → 以 newer ok 证据为准 → mismatch
    _run_stock_jobs(store, "market_data", ["600519.SH"], "ok", "c" * 64, session="s3")
    e2 = next(x for x in svc.caches({"limit": "200"})["items"]
              if x["capability"] == "technical")
    assert e2["integrity"]["hash_status"] == "mismatch"
    # 3) 只有 failed → unverified
    root3 = tmp_path / "proj3"
    store3 = build_refresh_store(root3)
    cache3 = WestockCacheStore(root3 / "state/dashboard/westock")
    _mk_curated(root3, "600519.SH")
    _run_stock_jobs(store3, "market_data", ["600519.SH"], "failed", session="s1")
    _write_raw(root3 / "state/dashboard/westock", "technical", "600519.SH",
               _tech_payload(), _now().isoformat())
    svc3 = build_operations_service(root3, cache3, store3)
    e3 = next(x for x in svc3.caches({"limit": "200"})["items"]
              if x["capability"] == "technical")
    assert e3["integrity"]["hash_status"] == "unverified"
    assert e3["last_refresh_status"] == "failed"


def test_success_rate_all_terminal_job_events(tmp_path):
    svc, store, cache = _svc(tmp_path, "600519.SH")
    for i, result in enumerate(("ok", "ok", "failed")):
        _run_stock_jobs(store, "quote_only", ["600519.SH"], result,
                        content_hash="b" * 64, session=f"s{i}")
    row = svc.capabilities({"capability": "quote"})["items"][0]
    # 2 ok + 1 failed = 3 终态 job 事件 → 2/3（不按 cell 最后状态）
    assert row["success_rate"] == 0.6667
    assert row["latest_ok_at"] is not None and row["latest_fail_at"] is not None


# --- 6. 缺失 q scope 进入矩阵（请求存在、q 缓存不存在） ---
def test_missing_q_scope_enters_matrix(tmp_path):
    from app.westock_operations_service import _short_scope
    svc, store, cache = _svc(tmp_path, "600519.SH")
    q = "q_" + "d" * 64
    _mk_screener_request_file(
        tmp_path, f"{9:032x}", "2026-08-06T00:00:00+00:00", q, capability="filter")
    cells = svc._coverage_cells(list(svc._iter_requests()))
    assert ("filter", q) in cells
    assert cells[("filter", q)] == "query"
    entries = {e["scope"]: e for e in svc.caches({"limit": "200"})["items"]}
    short = _short_scope(q)
    assert entries[short]["file_state"] == "missing"        # 无缓存文件
    assert entries[short]["availability"] == "unavailable"  # 产生 unavailable cell
    assert entries[short]["scope_type"] == "query"
    assert entries[short]["in_expected_matrix"] is True
    # q scope API 只公开 short_scope，不泄露完整 64hex
    raw = json.dumps(svc.caches({"limit": "200"}))
    assert q not in raw
    # 不接受任意 scope：非法 q 前缀不进入矩阵
    assert all(scope != "q_not_hex" for (_, scope) in cells)
    s = svc.summary()
    assert s["availability"]["unavailable"] >= 1


# --- 7. receipt 审计链：valid/missing/invalid/mismatch ---
def test_receipt_audit_chain(tmp_path):
    svc, store, cache = _svc(tmp_path)
    # valid：finish() 正常写入 receipt
    rid1 = store.create_request(body={"target": "market", "preset": "overview"},
                                session_id="s1")["request_id"]
    store.claim(rid1, "a" * 64)
    for j in store._read_request_file(rid1)["jobs"]:
        store.complete_job(rid1, j["job_id"], "ok", export_info={
            "fetched_at": _now().isoformat(), "cache_status": "fresh",
            "data_as_of": "2026-08-05", "content_hash": "b" * 64})
    store.finish(rid1)
    # missing：终态请求无 receipt 文件
    rid2 = f"{2:032x}"
    _mk_completed_request_file(tmp_path, rid2, "2026-08-06T00:00:00+00:00")
    # invalid：receipt 文件存在但内容非法
    rid3 = f"{3:032x}"
    _mk_completed_request_file(tmp_path, rid3, "2026-08-06T01:00:00+00:00")
    (store.receipts_dir / f"{rid3}.json").write_text("{broken", encoding="utf-8")
    # mismatch：receipt 与请求终态不一致
    rid4 = f"{4:032x}"
    _mk_completed_request_file(tmp_path, rid4, "2026-08-06T02:00:00+00:00")
    _mk_receipt(tmp_path, rid4, status="failed")
    rows = {r["request_id"]: r for r in svc.requests({"limit": "200"})["items"]}
    assert rows[rid1]["receipt_status"] == "valid"
    assert rows[rid2]["receipt_status"] == "missing"
    assert rows[rid3]["receipt_status"] == "invalid"
    assert rows[rid4]["receipt_status"] == "mismatch"
    agg = svc.request_aggregate()
    assert agg["receipt_status_counts"]["valid"] == 1
    assert agg["receipt_status_counts"]["missing"] == 1
    assert agg["receipt_status_counts"]["invalid"] == 1
    assert agg["receipt_status_counts"]["mismatch"] == 1
    f = svc.failures()
    assert f["receipt_audit_issues"] == {"missing": 1, "invalid": 1, "mismatch": 1}
    assert f["receipt_audit_issue_count"] == 3


# --- 8. 非法实际日期（2026-02-30）不 500，计 unknown ---
def test_invalid_real_date_not_500(tmp_path):
    svc, store, cache = _svc(tmp_path, "600519.SH")
    base = tmp_path / "state/dashboard/westock"
    _write_raw(base, "quote", "600519.SH", {"sh600519": {"p": 1}},
               _now().isoformat(), as_of="2026-02-30")  # 日历非法
    lag = svc.summary()["as_of_lag"]  # 不得抛错
    assert lag["per_capability"]["quote"]["as_of"] is None
    assert lag["unknown_count"] >= 1
    assert "current_date" in lag and "current_business_date" not in lag


# --- 9. 上海日期边界（Asia/Shanghai current_date + lag） ---
def test_shanghai_date_boundary(tmp_path, monkeypatch):
    import app.westock_operations_service as ops_mod
    monkeypatch.setattr(ops_mod, "_shanghai_today",
                        lambda: datetime(2026, 8, 6).date())
    svc, store, cache = _svc(tmp_path, "600519.SH")
    base = tmp_path / "state/dashboard/westock"
    now = _now().isoformat()
    # 同 capability 最新 as_of 胜出（写入顺序反序验证枚举顺序无关）
    _write_raw(base, "quote", "000001.SZ", {"sz000001": {"p": 1}}, now, as_of="2026-08-05")
    _write_raw(base, "quote", "600519.SH", {"sh600519": {"p": 1}}, now, as_of="2026-08-06")
    lag = svc.summary()["as_of_lag"]
    assert lag["current_date"] == "2026-08-06"           # 上海日期
    assert lag["per_capability"]["quote"]["as_of"] == "2026-08-06"
    assert lag["per_capability"]["quote"]["lag_days"] == 0
    # 边界：as_of 为昨日 → lag 1 天
    base2 = tmp_path / "state/dashboard/westock2"
    cache2 = WestockCacheStore(base2)
    _write_raw(base2, "quote", "600519.SH", {"sh600519": {"p": 1}}, now, as_of="2026-08-05")
    store2 = build_refresh_store(tmp_path)
    svc2 = build_operations_service(tmp_path, cache2, store2)
    lag2 = svc2.summary()["as_of_lag"]
    assert lag2["current_date"] == "2026-08-06"
    assert lag2["per_capability"]["quote"]["lag_days"] == 1


# --- 10. 55+ 股票 summary 不被默认分页截断 ---
def test_summary_symbols_not_truncated_55_plus(tmp_path):
    from app.westock_refresh_service import _GLOBAL_SHOW_CAPS, _STOCK_CAPS
    symbols = [f"{i:06d}.SH" for i in range(1, 56)]
    svc, store, cache = _svc(tmp_path, *symbols)
    s = svc.summary()
    assert len(s["symbols"]) == 55                        # 默认 50 分页不截断
    assert s["expected_cell_count"] == 55 * len(_STOCK_CAPS) + len(_GLOBAL_SHOW_CAPS)
    # 能力全量（矩阵内能力集合 = stock caps ∪ 全局展示 caps）
    assert len(s["capabilities"]) == len(set(_STOCK_CAPS) | set(_GLOBAL_SHOW_CAPS))
    assert s["total_cells"] == s["expected_cell_count"]
    # API 页面仍分页（不冲突）
    r = svc.symbols({"limit": "10", "offset": "0"})
    assert r["total"] == 55 and len(r["items"]) == 10


# --- 11. 连续调用一致 + 全目录 SHA-256 不变（含 receipt 场景） ---
def test_readonly_with_receipts_hash_proof(tmp_path):
    svc, store, cache = _svc(tmp_path, "600519.SH")
    base = tmp_path / "state/dashboard/westock"
    tech = _tech_payload()
    _write_raw(base, "technical", "600519.SH", tech, _now().isoformat())
    h = _data_hash(tech)
    _run_stock_jobs(store, "market_data", ["600519.SH"], "ok", h, session="s1")
    before = _dir_hashes(tmp_path / "state")
    r1 = json.dumps(svc.caches({"limit": "200"})["items"], sort_keys=True)
    s1 = json.dumps(svc.summary(), sort_keys=True)
    r2 = json.dumps(svc.caches({"limit": "200"})["items"], sort_keys=True)
    s2 = json.dumps(svc.summary(), sort_keys=True)
    assert r1 == r2 and s1 == s2                          # 连续调用一致
    assert _dir_hashes(tmp_path / "state") == before      # 全目录 SHA-256 不变
    e = next(x for x in svc.caches({"limit": "200"})["items"]
             if x["capability"] == "technical")
    assert e["integrity"]["hash_status"] == "verified"    # receipt valid → verified
    assert e["integrity"]["hash_verified"] is True



# ====================================================================== #
# 第三轮边界修正：验收矩阵
# ====================================================================== #
def _mk_matching_receipt(tmp_path, rid_hex, request):
    """从 request dict 构造与 finish._build_receipt 完全一致的合法 receipt。"""
    store = build_refresh_store(tmp_path)
    receipt = {
        "schema_version": 2,
        "request_id": request["request_id"],
        "target": request["target"],
        "jobs": [
            {"job_id": j["job_id"], "capability": j["capability"], "scope": j["scope"],
             "status": j["status"], "fetched_at": j.get("fetched_at"),
             "cache_status": j.get("cache_status"), "data_as_of": j.get("data_as_of"),
             "content_hash": j.get("content_hash"), "warning": j.get("warning")}
            for j in request.get("jobs") or []
        ],
        "created_at": request["created_at"],
        "started_at": request["started_at"],
        "finished_at": request["finished_at"],
        "status": request["status"],
        "status_detail": request["status_detail"],
        "warnings": list(request.get("warnings") or [])[-10:],
    }
    p = store.receipts_dir / f"{rid_hex}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
    return receipt


# --- 一、receipt 逐字段投影篡改矩阵（7 项） ---
def test_receipt_tamper_matrix(tmp_path):
    svc, store, cache = _svc(tmp_path)

    def setup(rid_hex, created, mutate):
        _mk_completed_request_file(tmp_path, rid_hex, created)
        req = json.loads((store.requests_dir / f"{rid_hex}.json").read_text(encoding="utf-8"))
        receipt = _mk_matching_receipt(tmp_path, rid_hex, req)
        mutate(receipt)
        (store.receipts_dir / f"{rid_hex}.json").write_text(
            json.dumps(receipt, ensure_ascii=False), encoding="utf-8")

    # 1) 同 counts、不同 job_id
    setup(f"{1:032x}", "2026-08-06T00:00:00+00:00",
          lambda r: r["jobs"][0].update(job_id="f" * 32))
    # 2) 同 counts、不同 capability
    setup(f"{2:032x}", "2026-08-06T01:00:00+00:00",
          lambda r: r["jobs"][1].update(capability="sector"))
    # 3) 同 counts、不同 scope
    setup(f"{3:032x}", "2026-08-06T02:00:00+00:00",
          lambda r: r["jobs"][2].update(scope="600519.SH"))
    # 4) target 不同
    setup(f"{4:032x}", "2026-08-06T03:00:00+00:00",
          lambda r: r.update(target={"kind": "market", "preset": "structure"}))
    # 5) content_hash 不同
    setup(f"{5:032x}", "2026-08-06T04:00:00+00:00",
          lambda r: r["jobs"][0].update(content_hash="c" * 64))
    # 6) finished_at 不同
    setup(f"{6:032x}", "2026-08-06T05:00:00+00:00",
          lambda r: r.update(finished_at="2026-08-06T09:00:00+00:00"))
    # 7) warnings/status_detail 不同
    setup(f"{7:032x}", "2026-08-06T06:00:00+00:00",
          lambda r: r.update(status_detail="tampered", warnings=["evil"]))
    rows = {r["request_id"]: r for r in svc.requests({"limit": "200"})["items"]}
    for i in range(1, 8):
        rid = f"{i:032x}"
        assert rows[rid]["receipt_status"] == "mismatch", rid
    assert svc.failures()["receipt_audit_issues"]["mismatch"] == 7


# --- 二、receipt-required：cancelled/expired 不计回执审计 ---
def test_receipt_not_required_cancelled_expired(tmp_path):
    svc, store, cache = _svc(tmp_path)
    _mk_completed_request_file(tmp_path, f"{1:032x}", "2026-08-06T00:00:00+00:00",
                               status="cancelled")
    _mk_completed_request_file(tmp_path, f"{2:032x}", "2026-08-06T01:00:00+00:00",
                               status="expired")
    rows = {r["request_id"]: r for r in svc.requests({"limit": "200"})["items"]}
    assert rows[f"{1:032x}"]["receipt_status"] == "not_applicable"
    assert rows[f"{2:032x}"]["receipt_status"] == "not_applicable"
    f = svc.failures()
    assert f["receipt_audit_issues"] == {"missing": 0, "invalid": 0, "mismatch": 0}
    assert f["receipt_audit_issue_count"] == 0
    agg = svc.request_aggregate()
    assert agg["receipt_status_counts"]["not_applicable"] == 2
    # 请求级失败分类仍正确
    assert f["request_failure_categories"]["cancelled"] == 1
    assert f["request_failure_categories"]["expired"] == 1


# --- 三、hash verified 依赖精确匹配的 valid receipt ---
def test_hash_verified_requires_exact_receipt(tmp_path):
    svc, store, cache = _svc(tmp_path, "600519.SH")
    base = tmp_path / "state/dashboard/westock"
    tech = _tech_payload()
    _write_raw(base, "technical", "600519.SH", tech, _now().isoformat())
    h = _data_hash(tech)
    # 合法 flow：finish 生成精确 receipt → verified
    _run_stock_jobs(store, "market_data", ["600519.SH"], "ok", h, session="s1")
    e = next(x for x in svc.caches({"limit": "200"})["items"]
             if x["capability"] == "technical")
    assert e["integrity"]["hash_status"] == "verified"
    # 篡改 evidence job 的 content_hash（job counts 相同）→ receipt mismatch → 不 verified
    rid2 = _run_stock_jobs(store, "market_data", ["600519.SH"], "ok", h, session="s2")
    rpath = store.receipts_dir / f"{rid2}.json"
    rec = json.loads(rpath.read_text(encoding="utf-8"))
    for j in rec["jobs"]:
        if j["capability"] == "technical":
            j["content_hash"] = "e" * 64
    rpath.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
    e2 = next(x for x in svc.caches({"limit": "200"})["items"]
              if x["capability"] == "technical")
    assert e2["integrity"]["hash_status"] == "unverified"  # 证据 receipt 非精确 valid
    assert e2["integrity"]["hash_verified"] is False
    # target 篡改 → 不 verified
    rid3 = _run_stock_jobs(store, "market_data", ["600519.SH"], "ok", h, session="s3")
    rpath3 = store.receipts_dir / f"{rid3}.json"
    rec3 = json.loads(rpath3.read_text(encoding="utf-8"))
    rec3["target"] = {"kind": "market", "preset": "overview"}
    rpath3.write_text(json.dumps(rec3, ensure_ascii=False), encoding="utf-8")
    e3 = next(x for x in svc.caches({"limit": "200"})["items"]
              if x["capability"] == "technical")
    assert e3["integrity"]["hash_status"] == "unverified"


def test_hash_pending_evidence_processing(tmp_path):
    svc, store, cache = _svc(tmp_path, "600519.SH")
    base = tmp_path / "state/dashboard/westock"
    tech = _tech_payload()
    _write_raw(base, "technical", "600519.SH", tech, _now().isoformat())
    h = _data_hash(tech)
    rid = store.create_request(body={"target": "stock", "preset": "market_data",
                                     "symbols": ["600519.SH"]},
                               session_id="s1")["request_id"]
    store.claim(rid, "a" * 64)
    for j in store._read_request_file(rid)["jobs"]:
        store.complete_job(rid, j["job_id"], "ok", export_info={
            "fetched_at": _now().isoformat(), "cache_status": "fresh",
            "data_as_of": "2026-08-05", "content_hash": h})
    # 不 finish：请求仍 processing → 只能 pending_evidence
    e = next(x for x in svc.caches({"limit": "200"})["items"]
             if x["capability"] == "technical")
    assert e["integrity"]["hash_status"] == "pending_evidence"
    assert e["integrity"]["hash_verified"] is False


# --- 四、raw 分类优先级：结构校验先于时间分类；时间/身份字段不回显 ---
def test_raw_structure_before_timestamp(tmp_path):
    svc, store, cache = _svc(tmp_path, "600519.SH")
    base = tmp_path / "state/dashboard/westock"
    now = _now()
    # evil transport + future timestamp → invalid_envelope（结构优先于时间）
    env = _env_dict("quote", "600519.SH", {"sh600519": {"p": 1}},
                              (now + timedelta(hours=1)).isoformat())
    env["transport"] = "evil"
    p = base / "quote/600519.SH.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(env, ensure_ascii=False), encoding="utf-8")
    e = next(x for x in svc.caches({"limit": "200"})["items"]
             if x["capability"] == "quote")
    assert e["file_state"] == "invalid_envelope"
    assert e["availability"] == "unavailable"
    # 身份字段注入：fetched_at 含路径/token 原样 → invalid_timestamp 且不回显
    env2 = _env_dict("news", "600519.SH", {}, "C:\\secret\\token-abc")
    p2 = base / "news/600519.SH.json"
    p2.parent.mkdir(parents=True, exist_ok=True)
    p2.write_text(json.dumps(env2, ensure_ascii=False), encoding="utf-8")
    raw = json.dumps(svc.caches({"limit": "200"}))
    assert "C:\\secret" not in raw and "token-abc" not in raw
    e2 = next(x for x in svc.caches({"limit": "200"})["items"]
              if x["capability"] == "news")
    assert e2["file_state"] == "invalid_timestamp"
    assert e2["fetched_at"] is None and e2["cached_at"] is None
    assert e2["as_of"] is None and e2["age_seconds"] is None
    assert e2["expires_at"] is None
    assert e2["consumer_status"] == "not_validated"
    assert e2["integrity"]["valid"] is False
    assert e2["integrity"]["hash_status"] == "unverified"
    # future 时间戳文件也不回显原始时间字符串
    env3 = _env_dict("margin", "600519.SH", {},
                               (now + timedelta(hours=2)).isoformat())
    p3 = base / "margin/600519.SH.json"
    p3.parent.mkdir(parents=True, exist_ok=True)
    p3.write_text(json.dumps(env3, ensure_ascii=False), encoding="utf-8")
    e3 = next(x for x in svc.caches({"limit": "200"})["items"]
              if x["capability"] == "margin")
    assert e3["file_state"] == "future_timestamp"
    assert e3["fetched_at"] is None and e3["cached_at"] is None
    assert e3["as_of"] is None


# --- 五、warning/status_detail 脱敏注入 ---
def test_warning_status_detail_redaction(tmp_path):
    svc, store, cache = _svc(tmp_path)
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
    raw_req = json.dumps(svc.requests({"limit": "200"}))
    raw_sum = json.dumps(svc.summary())
    raw_fail = json.dumps(svc.failures())
    for raw in (raw_req, raw_sum, raw_fail):
        for bad in ("Authorization", "Bearer", "C:\\secret", "token-xxx",
                    "Traceback", "SECRET_URL", "evil.example"):
            assert bad not in raw, (bad, raw[:200])
    row = {r["request_id"]: r for r in svc.requests({"limit": "200"})["items"]}[rid]
    assert row["warning_count"] == 4
    assert row["warning_categories"].get("unknown", 0) == 4  # 注入文本不匹配受控分类
    assert row["status_detail_code"] == "completed_all"
    assert "warnings" not in row and "status_detail" not in row


# --- 六、恶意文件名 scope 脱敏 ---
def test_malicious_scope_filename_redacted(tmp_path):
    svc, store, cache = _svc(tmp_path, "600519.SH")
    base = tmp_path / "state/dashboard/westock"
    p = base / "quote/secret_token_xxx.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}", encoding="utf-8")
    raw = json.dumps(svc.caches({"limit": "200"}))
    assert "secret_token_xxx" not in raw
    e = next(x for x in svc.caches({"limit": "200"})["items"]
             if not x["in_expected_matrix"])
    assert e["capability"] == "quote"
    assert e["scope"] == "invalid_scope"
    assert e["short_scope"] == "非法 Scope"
    assert e["scope_id"] == "u1"
    assert e["file_state"] in ("invalid_envelope", "invalid_json")


# --- 七、physical inventory 覆盖未知 capability 目录与 root JSON ---
def test_unknown_capability_and_root_json(tmp_path):
    from app.westock_refresh_service import _GLOBAL_SHOW_CAPS, _STOCK_CAPS
    svc, store, cache = _svc(tmp_path, "600519.SH")
    base = tmp_path / "state/dashboard/westock"
    now = _now()
    _write_raw(base, "quote", "600519.SH", {"sh600519": {"p": 1}},
               (now - timedelta(seconds=10)).isoformat())
    (base / "not_a_capability").mkdir(parents=True, exist_ok=True)
    (base / "not_a_capability/foo.json").write_text(json.dumps({"x": 1}), encoding="utf-8")
    (base / "not_a_capability/bar.json").write_text("{broken", encoding="utf-8")
    (base / "root_level.json").write_text("{}", encoding="utf-8")
    s = svc.summary()
    expected = len(_STOCK_CAPS) + len(_GLOBAL_SHOW_CAPS)
    assert s["expected_cell_count"] == expected          # 未知不入矩阵
    assert s["physical_cache_count"] == 4                # 1 正常 + 3 未知/root
    assert s["unexpected_physical_count"] == 3
    assert s["invalid_physical_count"] == 3
    assert s["total_cells"] == expected
    raw = json.dumps(svc.caches({"limit": "200"}))
    for bad in ("not_a_capability", "foo", "bar", "root_level"):
        assert bad not in raw
    items = svc.caches({"limit": "200"})["items"]
    u = [x for x in items if not x["in_expected_matrix"]]
    assert len(u) == 3
    assert all(x["capability"] == "unknown" for x in u)
    assert all(x["scope"] == "invalid_scope" for x in u)
    assert {x["scope_id"] for x in u} == {"u1", "u2", "u3"}
    assert all(x["file_state"] in ("invalid_envelope", "invalid_json") for x in u)


# --- 八、caches 过滤分页：返回过滤后 total ---
def test_caches_filtered_total_pagination(tmp_path):
    from app.westock_refresh_service import _GLOBAL_SHOW_CAPS, _STOCK_CAPS
    svc, store, cache = _svc(tmp_path, "600519.SH")
    base = tmp_path / "state/dashboard/westock"
    now = _now()
    _write_raw(base, "quote", "600519.SH", {"sh600519": {"p": 1}},
               (now - timedelta(seconds=10)).isoformat())
    (base / "quote/evil_scope.json").write_text("{broken", encoding="utf-8")
    expected = len(_STOCK_CAPS) + len(_GLOBAL_SHOW_CAPS)
    r = svc.caches({})
    assert r["total"] == expected + 1
    assert r["coverage_total"] == expected
    assert r["inventory_total"] == 2
    assert r["unexpected_physical_count"] == 1
    # 过滤后 total 正确（不能用 coverage+unexpected 推断）
    r2 = svc.caches({"capability": "quote"})
    assert r2["total"] == 2
    assert r2["coverage_total"] == expected  # 未过滤矩阵数不变
    r3 = svc.caches({"freshness": "fresh"})
    assert r3["total"] == 1
    r4 = svc.caches({"scope_type": "invalid"})
    assert r4["total"] == 1 and r4["items"][0]["scope_id"] == "u1"


# --- 九、跨时区排序：按解析绝对时间 ---
def test_cross_timezone_sort(tmp_path):
    from app.westock_operations_service import _ts_sort_key
    svc, store, cache = _svc(tmp_path)
    # 12:00+08:00 = 04:00 UTC；05:00+00:00 = 05:00 UTC（真实更新，必须排前）
    _mk_request_file(tmp_path, f"{1:032x}", "2026-08-06T12:00:00+08:00", session="s1")
    _mk_request_file(tmp_path, f"{2:032x}", "2026-08-06T05:00:00+00:00", session="s2")
    rows = svc.requests({"limit": "10"})["items"]
    assert rows[0]["request_id"] == f"{2:032x}"
    assert rows[1]["request_id"] == f"{1:032x}"
    # 排序键：05:00+00:00 绝对时间更大
    assert _ts_sort_key("2026-08-06T05:00:00+00:00") > _ts_sort_key("2026-08-06T12:00:00+08:00")


# --- 十、orphan / 非法 receipt 审计计数 ---
def test_orphan_and_invalid_receipts(tmp_path):
    svc, store, cache = _svc(tmp_path)
    orphan_rid = "e" * 32
    _mk_receipt(tmp_path, orphan_rid, status="completed")
    bad_rid = "f" * 32
    (store.receipts_dir / f"{bad_rid}.json").write_text("{broken", encoding="utf-8")
    rid1 = store.create_request(body={"target": "market", "preset": "overview"},
                                session_id="s1")["request_id"]
    store.claim(rid1, "a" * 64)
    for j in store._read_request_file(rid1)["jobs"]:
        store.complete_job(rid1, j["job_id"], "ok", export_info={
            "fetched_at": _now().isoformat(), "cache_status": "fresh",
            "data_as_of": "2026-08-05", "content_hash": "b" * 64})
    store.finish(rid1)
    f = svc.failures()
    assert f["orphan_receipt_count"] == 1
    assert f["invalid_receipt_file_count"] == 1
    assert f["receipt_audit_issue_count"] == 0  # 正常请求回执 valid
    raw = json.dumps(f) + json.dumps(svc.summary())
    assert orphan_rid not in raw and bad_rid not in raw  # 不公开 id/文件名
    assert svc.summary()["failures"]["orphan_receipt_count"] == 1


def test_request_history_redacts_query_scope_and_prioritizes_failure_detail(tmp_path):
    svc, store, cache = _svc(tmp_path)
    q_scope = "q_" + "a" * 64
    rid = "d" * 32
    _mk_screener_request_file(tmp_path, rid, _now().isoformat(), q_scope)
    row = svc.requests({"limit": "10"})["items"][0]
    assert row["symbols"] == "q_aaaaa…aaaa"
    assert q_scope not in json.dumps(row)

    # 具体失败原因必须优先于通用 failed 状态，否则历史页会丢失运维原因。
    assert svc._status_detail_code({"status": "failed", "status_detail": "worker_timeout"}) == "worker_timeout"
    assert svc._status_detail_code({"status": "failed", "status_detail": "receipt_write_failed"}) == "receipt_failed"


def test_oversized_refresh_files_are_not_parsed(tmp_path):
    from app.westock_refresh_service import MAX_RECEIPT_BYTES, MAX_REQUEST_BYTES

    svc, store, cache = _svc(tmp_path)
    request_id = "a" * 32
    receipt_id = "b" * 32
    store.requests_dir.mkdir(parents=True, exist_ok=True)
    store.receipts_dir.mkdir(parents=True, exist_ok=True)
    (store.requests_dir / f"{request_id}.json").write_bytes(b" " * (MAX_REQUEST_BYTES + 1))
    (store.receipts_dir / f"{receipt_id}.json").write_bytes(b" " * (MAX_RECEIPT_BYTES + 1))

    assert svc.requests({"limit": "10"})["total"] == 0
    failures = svc.failures()
    assert failures["invalid_receipt_file_count"] == 1
    assert request_id not in json.dumps(svc.summary())
    assert receipt_id not in json.dumps(failures)
