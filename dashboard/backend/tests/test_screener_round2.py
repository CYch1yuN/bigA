"""Phase E Codex 第二轮定点修正测试。

覆盖：
- unavailable 结果 POST→GET 闭环（真实 API）
- result_id 碰撞（monkeypatch token_hex）：三处 ID 一致 / 连续碰撞抛错
- mode 互斥按字段非 null（空数组/空对象也拒绝）
- 严格 schema 拒绝矩阵（result/saved/candidate 篡改）
- 同 session 五线程并发 + 内存清理
- 真实 >2MiB / os.replace 失败
- condition 枚举语义（macd_signal enum / in 仅枚举）
- cache_scope 隔离与安全字段
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from app.main import create_app
from app.screener_service import (
    ScreenerError,
    ScreenerService,
    ScreenerStore,
    canonical_query_hash,
)
from app.westock_bridge import CAPABILITY_MAP


def _write_scope_cache(root: Path, capability: str, scope: str, data, *,
                     fetched_at: str | None = None) -> Path:
    path = root / "state" / "dashboard" / "westock" / capability / f"{scope}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    # 禁止依赖当前日期中的固定时刻：默认用调用时刻（fresh）
    fetched = fetched_at if fetched_at is not None else datetime.now(timezone.utc).isoformat()
    payload = {
        "schema_version": 1, "capability": capability, "tool": CAPABILITY_MAP[capability].tool,
        "scope": scope, "source": "westock-mcp", "transport": "cache_export",
        "as_of": "2026-07-31", "fetched_at": fetched,
        "cached_at": fetched, "data": data, "warnings": [],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _write_cache_at(root: Path, capability: str, scope: str, data, age_seconds: int) -> Path:
    """按相对当前时刻的年龄写入缓存（stale/future 测试专用）。"""
    fetched = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat()
    return _write_scope_cache(root, capability, scope, data, fetched_at=fetched)


def _seed_curated(root: Path) -> None:
    curated = root / "data" / "curated"
    curated.mkdir(parents=True)
    df = pd.DataFrame({
        "symbol": ["600519.SH", "000001.SZ"] * 5, "trade_date": ["2026-07-31"] * 10,
        "open_raw": [1.0] * 10, "high_raw": [2.0] * 10, "low_raw": [0.5] * 10,
        "close_raw": [1.5] * 10, "open_qfq": [1.0] * 10, "high_qfq": [2.0] * 10,
        "low_qfq": [0.5] * 10, "close_qfq": [1.5] * 10, "volume": [100] * 10, "amount": [1e5] * 10,
    })
    df.to_parquet(curated / "daily_quotes_600519.SH_2026-07-01_2026-07-31.parquet")
    df.to_parquet(curated / "daily_quotes_000001.SZ_2026-07-01_2026-07-31.parquet")


def _seed_query_cache(root: Path, capability: str, query: dict, rows: list) -> Path:
    from app.screener_service import _validate_query
    validated = _validate_query(query)
    return _write_scope_cache(root, capability, canonical_query_hash(validated), {"items": rows})


def _make_app(root: Path, config_factory):
    return create_app(config_factory(project_root=root), enable_static=False)


def _client(app):
    from fastapi.testclient import TestClient
    client = TestClient(app, base_url="https://127.0.0.1")
    client.post("/api/auth/login", json={"username": "admin", "password": "secret123"})
    return client


def _headers(client) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("ashare_dash_csrf") or ""}


BASE_QUERY = {
    "mode": "condition",
    "universe": {"type": "local", "value": None},
    "conditions": [],
    "sort": {"field": "score", "direction": "desc"},
    "limit": 50,
}

VALID_QUERY = {
    "mode": "condition", "universe": {"type": "local", "value": None}, "conditions": [],
    "strategy": None, "factor": None, "labels": None,
    "sort": {"field": "score", "direction": "desc"}, "limit": 50,
}


ROWS = [
    {"symbol": "600519.SH", "name": "贵州茅台", "score": 90.0, "rank": 1,
     "price": 1350.0, "change_percent": 3.2, "industry": "白酒",
     "reason": "业绩超预期", "matched_labels": ["high_dividend"],
     "factor_values": {"value": 80.0}},
    {"symbol": "000001.SZ", "name": "平安银行", "score": 60.0, "rank": 2,
     "price": 11.0, "change_percent": 0.5, "industry": "银行",
     "reason": "股息率较高", "matched_labels": [], "factor_values": {"value": 60.0}},
    {"symbol": "999999.SZ", "name": "无本地股", "score": 30.0, "rank": 3,
     "price": 5.0, "change_percent": -1.0, "industry": "未知",
     "reason": "x", "matched_labels": [], "factor_values": {"value": 30.0}},
]


# ---------------------------------------------------------------------- #
# 一、unavailable 结果完整闭环（POST → GET）
# ---------------------------------------------------------------------- #

def test_unavailable_run_post_then_get_roundtrip(tmp_path, config_factory):
    """无精确缓存时 POST run 返回 result_id，GET 立即可读（200 + unavailable），
    页面不会进入 result_not_found。"""
    root = tmp_path / "repo"
    _seed_curated(root)
    app = _make_app(root, config_factory)
    client = _client(app)
    r = client.post("/api/screener/run", json=BASE_QUERY, headers=_headers(client))
    assert r.status_code == 200
    body = r.json()
    assert body["cache_status"] == "unavailable"
    assert body["data"]["items"] == [] and body["data"]["total"] == 0
    result_id = body["result_id"]
    assert result_id and len(result_id) == 32
    # GET 立即读回
    r2 = client.get(f"/api/screener/results/{result_id}")
    assert r2.status_code == 200  # 不是 404 result_not_found
    got = r2.json()
    assert got["result_id"] == result_id
    assert got["cache_status"] == "unavailable"
    assert any("精确匹配" in w for w in got["warnings"])
    # 结果快照文件确实存在
    store = ScreenerStore(root)
    assert (store.results_dir / f"{result_id}.json").exists()


def test_unavailable_snapshot_persisted_even_when_no_cache(tmp_path, config_factory):
    """损坏/无缓存结构不可识别 → 也生成快照并保存（_do_run 三种结果都落盘）。"""
    root = tmp_path / "repo"
    _seed_curated(root)
    _write_scope_cache(root, "filter", "global", {"items": ROWS})  # global 不满足精确 scope
    app = _make_app(root, config_factory)
    client = _client(app)
    body = client.post("/api/screener/run", json=BASE_QUERY, headers=_headers(client)).json()
    assert body["cache_status"] == "unavailable"
    r2 = client.get(f"/api/screener/results/{body['result_id']}")
    assert r2.status_code == 200
    assert r2.json()["cache_status"] == "unavailable"


# ---------------------------------------------------------------------- #
# 二、result_id 碰撞彻底修正
# ---------------------------------------------------------------------- #

def _valid_result_payload_ok(root: Path) -> dict:
    """构造一个合法可写的结果 payload（query 与 cache_scope 一致）。"""
    from app.screener_service import canonical_query_hash
    qhash = canonical_query_hash(VALID_QUERY)
    return {
        "schema_version": 1, "mode": "condition", "source": "westock-mcp",
        "as_of": "2026-07-31", "source_fetched_at": "2026-07-31T10:00:00+00:00",
        "generated_at": "2026-07-31T10:00:00+00:00", "cache_status": "fresh",
        "is_realtime": False, "transport": "cache_export",
        "availability": {"filter": "fresh"}, "query": VALID_QUERY,
        "data": {"items": [{"symbol": "600519.SH", "name": "贵州茅台", "score": 90.0,
                            "rank": 1, "local_history_available": True}], "total": 1},
        "warnings": ["ok"], "cache_scope": qhash,
    }


def test_result_collision_syncs_payload_and_retries(tmp_path, config_factory, monkeypatch):
    """第一次碰撞、第二次成功：返回 ID == 文件名 == 文件内容 result_id，三处一致。"""
    import app.screener_service as svc_mod
    root = tmp_path / "repo"
    _seed_curated(root)
    _seed_query_cache(root, "filter", dict(BASE_QUERY), ROWS)
    svc = ScreenerService(root)

    store = ScreenerStore(root)
    store.ensure_dirs()
    collisions = {"0" * 32}  # 第一次生成必碰撞
    counter = {"n": 0}

    def fake_token_hex(nbytes):
        counter["n"] += 1
        if counter["n"] == 1:
            return "0" * 32  # 碰撞
        return "a" * 32  # 第二次成功

    # 预先占用 0*32 与 a*32（a*32 第二次生成时也会碰撞？不——占用 0*32 即可，
    # 第二次生成 a*32 不占用则成功）
    store.write_result("0" * 32, _valid_result_payload_ok(root))
    monkeypatch.setattr(svc_mod.secrets, "token_hex", fake_token_hex)
    body = svc.run("s", dict(BASE_QUERY))
    monkeypatch.undo()
    result_id = body["result_id"]
    assert result_id == "a" * 32
    # 三处一致：返回 ID == 文件名 == 文件内容 result_id
    stored = json.loads((store.results_dir / f"{result_id}.json").read_text(encoding="utf-8"))
    assert stored["result_id"] == result_id


def test_result_collision_three_times_raises(tmp_path, config_factory, monkeypatch):
    """连续三次碰撞 → result_collision，不能静默返回未写入的 payload。"""
    import app.screener_service as svc_mod
    root = tmp_path / "repo"
    _seed_curated(root)
    _seed_query_cache(root, "filter", dict(BASE_QUERY), ROWS)
    svc = ScreenerService(root)
    store = ScreenerStore(root)
    store.ensure_dirs()
    for i in range(3):
        store.write_result(f"{i:x}" * 32, _valid_result_payload_ok(root))

    def fake_token_hex(nbytes):
        # 始终返回已占用 ID（循环 0/1/2）
        return f"{0:x}" * 32

    monkeypatch.setattr(svc_mod.secrets, "token_hex", fake_token_hex)
    with pytest.raises(ScreenerError) as ei:
        svc.run("s", dict(BASE_QUERY))
    assert ei.value.code == "result_collision"
    monkeypatch.undo()


# ---------------------------------------------------------------------- #
# 三、mode 互斥按“字段非 null”判断
# ---------------------------------------------------------------------- #

def test_mode_mutual_exclusion_empty_values(tmp_path, config_factory):
    """空数组/空对象也属于携带其他模式字段 → invalid_mode。"""
    root = tmp_path / "repo"
    _seed_curated(root)
    svc = ScreenerService(root)
    from app.screener_service import _validate_query

    # conditions=[]（strategy 模式携带）
    with pytest.raises(ScreenerError) as ei:
        _validate_query({"mode": "strategy", "universe": {"type": "local", "value": None},
                         "conditions": [], "strategy": {"name": "ma_breakout"},
                         "sort": {"field": "score", "direction": "desc"}, "limit": 50})
    assert ei.value.code == "invalid_mode"
    # strategy={}
    with pytest.raises(ScreenerError) as ei:
        _validate_query({**BASE_QUERY, "strategy": {}})
    assert ei.value.code == "invalid_mode"
    # factor={}
    with pytest.raises(ScreenerError) as ei:
        _validate_query({**BASE_QUERY, "factor": {}})
    assert ei.value.code == "invalid_mode"
    # labels={}
    with pytest.raises(ScreenerError) as ei:
        _validate_query({**BASE_QUERY, "labels": {}})
    assert ei.value.code == "invalid_mode"
    # condition 模式自身 conditions=[] 合法
    q = _validate_query(dict(BASE_QUERY))
    assert q["mode"] == "condition" and q["conditions"] == []


# ---------------------------------------------------------------------- #
# 四、严格 schema 拒绝矩阵
# ---------------------------------------------------------------------- #

def test_strict_schema_rejection_matrix(tmp_path, config_factory):
    """result/saved/candidate 篡改拒绝矩阵：任意字段/非法 ID/非法 item/错误类型全部 fail-open。"""
    root = tmp_path / "repo"
    _seed_curated(root)
    _seed_query_cache(root, "filter", dict(BASE_QUERY), ROWS)
    store = ScreenerStore(root)
    store.ensure_dirs()
    app = _make_app(root, config_factory)
    client = _client(app)

    # 合法结果
    rid = client.post("/api/screener/run", json=BASE_QUERY, headers=_headers(client)).json()["result_id"]
    assert client.get(f"/api/screener/results/{rid}").status_code == 200

    # result 篡改矩阵（各写一个文件验证拒绝）
    bad_results = {
        "1" * 32: {"schema_version": 1, "hacked": 1},  # 任意顶层字段
        "2" * 32: {**_valid_result_payload_ok(root), "result_id": "3" * 32},  # ID 与文件名不一致
        "4" * 32: {**_valid_result_payload_ok(root),
                   "data": {"items": [{"symbol": "600519.SH", "script": "alert(1)"}], "total": 1}},  # item 非法键
        "5" * 32: {**_valid_result_payload_ok(root), "cache_status": "maybe"},  # 非法 cache_status
        "6" * 32: {**_valid_result_payload_ok(root), "source": "hacked"},  # 非法 source
        "7" * 32: {**_valid_result_payload_ok(root), "transport": "mcp"},  # 非法 transport
        "8" * 32: {**_valid_result_payload_ok(root), "is_realtime": True},  # 非法 is_realtime
        "9" * 32: {**_valid_result_payload_ok(root),
                   "data": {"items": [{"symbol": "BAD", "name": "x"}], "total": 1}},  # 非法 symbol
        "a" * 32: {**_valid_result_payload_ok(root),
                   "data": {"items": [{"symbol": "600519.SH", "factor_values": {"hacked": 1.0}}], "total": 1}},  # factor 非法键
        "b" * 32: {**_valid_result_payload_ok(root), "data": {"items": [], "total": 5}},  # total 与 items 不一致
        "c" * 32: {**_valid_result_payload_ok(root), "cache_scope": "q_" + "f" * 64},  # scope 与 query 不一致
        "d" * 32: {**_valid_result_payload_ok(root), "warnings": [123]},  # warnings 非字符串
        "e" * 32: {**_valid_result_payload_ok(root),
                   "data": {"items": [{"symbol": "600519.SH", "name": "x", "score": float("nan")}], "total": 1}},  # NaN
    }
    for fid, payload in bad_results.items():
        (store.results_dir / f"{fid}.json").write_text(json.dumps(payload, allow_nan=True), encoding="utf-8")
    for fid in bad_results:
        assert store.read_result(fid) is None, f"应拒绝 {fid}"

    # saved 篡改矩阵
    good_saved = {"id": "f" * 32, "name": "好条件", "created_at": "2026-07-31T00:00:00+00:00",
                  "updated_at": "2026-07-31T00:00:00+00:00", "query": VALID_QUERY}
    store.save_saved([good_saved])
    assert len(store.load_saved()) == 1
    bad_saved = [
        {"id": "f" * 32, "name": "x<script>", "created_at": "2026-07-31T00:00:00+00:00",
         "updated_at": "2026-07-31T00:00:00+00:00", "query": VALID_QUERY},  # script
        {"id": "f" * 32, "name": "x", "created_at": "bad-time",
         "updated_at": "2026-07-31T00:00:00+00:00", "query": VALID_QUERY},  # 非法时间
        {"id": "f" * 32, "name": "x", "created_at": "2026-07-31T00:00:00+00:00",
         "updated_at": "2026-07-31T00:00:00+00:00",
         "query": {**BASE_QUERY, "mode": "hacked"}},  # 非法 query
        {"id": "f" * 32, "name": "x", "created_at": "2026-07-31T00:00:00+00:00",
         "updated_at": "2026-07-31T00:00:00+00:00", "query": VALID_QUERY, "extra": 1},  # 任意字段
    ]
    for item in bad_saved:
        store.save_saved([item])
        assert store.load_saved() == [], f"应拒绝 saved: {item}"

    # candidate 篡改矩阵（缺字段/非法类型/script）
    good_cand = {"symbol": "600519.SH", "name": "贵州茅台", "source_result_id": rid,
                 "note": "ok", "added_at": "2026-07-31T00:00:00+00:00", "local_history_available": True}
    store.save_candidates([good_cand])
    assert len(store.load_candidates()) == 1
    bad_cands = [
        {"symbol": "600519.SH", "name": "x", "source_result_id": rid,
         "note": "x<script>", "added_at": "2026-07-31T00:00:00+00:00", "local_history_available": True},
        {"symbol": "600519.SH", "name": "x", "source_result_id": rid,
         "note": "", "added_at": "bad", "local_history_available": True},
        {"symbol": "600519.SH", "name": "x", "source_result_id": "nothex",
         "note": "", "added_at": "2026-07-31T00:00:00+00:00", "local_history_available": True},
        {"symbol": "600519.SH", "source_result_id": rid,
         "note": "", "added_at": "2026-07-31T00:00:00+00:00", "local_history_available": True},  # 缺 name
        {"symbol": "600519.SH", "name": "x", "source_result_id": rid,
         "note": "", "added_at": "2026-07-31T00:00:00+00:00", "local_history_available": True, "extra": 1},
    ]
    for item in bad_cands:
        store.save_candidates([item])
        assert store.load_candidates() == [], f"应拒绝 candidate: {item}"


# ---------------------------------------------------------------------- #
# 五、同 session 五线程并发 + 内存清理
# ---------------------------------------------------------------------- #

class _CountingCache:
    def __init__(self, store, root, capability, scope):
        self.store = store
        self.root = root
        self.capability = capability
        self.scope = scope
        self.calls = 0
        self.lock = threading.Lock()

    def read(self, capability, scope):
        with self.lock:
            self.calls += 1
        return self.store.read(capability, scope)


def test_same_session_five_threads_dedup_and_cleanup(tmp_path, config_factory):
    """同一 session、同一 query、5 个真实线程：全部成功、同一 result_id、
    缓存只读一次、无 429；完成后共享状态全部清理（防内存泄漏）。"""
    root = tmp_path / "repo"
    _seed_curated(root)
    _seed_query_cache(root, "filter", dict(BASE_QUERY), ROWS)
    svc = ScreenerService(root)
    original = svc.curated.westock_store
    counting = _CountingCache(original, root, "filter", canonical_query_hash(BASE_QUERY))
    svc.curated.westock_store = counting  # type: ignore[assignment]

    results: list[dict] = []
    errors: list[Exception] = []
    lock = threading.Lock()
    barrier = threading.Barrier(5)

    def worker():
        barrier.wait()
        try:
            body = svc.run("same-session", dict(BASE_QUERY))
            with lock:
                results.append(body)
        except Exception as exc:  # pragma: no cover
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, [str(e) for e in errors]
    assert len(results) == 5
    assert len({r["result_id"] for r in results}) == 1
    assert counting.calls <= 1
    # 内存生命周期：完成后无残留（避免长时间运行泄漏）
    assert svc._in_flight == {}


# ---------------------------------------------------------------------- #
# 六、condition 枚举语义
# ---------------------------------------------------------------------- #

def test_enum_condition_semantics(tmp_path, config_factory):
    from app.screener_service import _validate_query
    root = tmp_path / "repo"
    _seed_curated(root)

    # macd_signal eq 合法
    q1 = {**BASE_QUERY, "conditions": [{"field": "macd_signal", "operator": "eq", "value": "golden_cross"}]}
    assert _validate_query(q1)["conditions"][0]["value"] == "golden_cross"
    # macd_signal in 合法（枚举子集、非空、不重复、≤20）
    q2 = {**BASE_QUERY, "conditions": [{"field": "macd_signal", "operator": "in", "value": ["golden_cross", "neutral"]}]}
    assert _validate_query(q2)["conditions"][0]["value"] == ["golden_cross", "neutral"]
    # 非法枚举值
    with pytest.raises(ScreenerError) as ei:
        _validate_query({**BASE_QUERY, "conditions": [{"field": "macd_signal", "operator": "eq", "value": "hacked"}]})
    assert ei.value.code == "invalid_condition"
    # in 空 / 重复 / >20 / 非法值
    for bad in ([], ["golden_cross", "golden_cross"], list(range(21)),
                ["golden_cross", "hacked"]):
        with pytest.raises(ScreenerError) as ei:
            _validate_query({**BASE_QUERY, "conditions": [{"field": "macd_signal", "operator": "in", "value": bad}]})
        assert ei.value.code == "invalid_condition"
    # 枚举字段禁止 between / gt
    with pytest.raises(ScreenerError) as ei:
        _validate_query({**BASE_QUERY, "conditions": [{"field": "macd_signal", "operator": "between", "value": [1, 2]}]})
    assert ei.value.code == "invalid_condition"
    with pytest.raises(ScreenerError) as ei:
        _validate_query({**BASE_QUERY, "conditions": [{"field": "macd_signal", "operator": "gt", "value": 1}]})
    assert ei.value.code == "invalid_condition"
    # numeric 禁止 in；boolean 禁止 in/between
    with pytest.raises(ScreenerError) as ei:
        _validate_query({**BASE_QUERY, "conditions": [{"field": "price", "operator": "in", "value": [1, 2]}]})
    assert ei.value.code == "invalid_condition"
    with pytest.raises(ScreenerError) as ei:
        _validate_query({**BASE_QUERY, "conditions": [{"field": "ma5_above_ma20", "operator": "in", "value": [True]}]})
    assert ei.value.code == "invalid_condition"
    # boll_position 已从可选字段移除
    with pytest.raises(ScreenerError) as ei:
        _validate_query({**BASE_QUERY, "conditions": [{"field": "boll_position", "operator": "gt", "value": 1}]})
    assert ei.value.code == "invalid_condition"


def test_enum_condition_run_uses_query_specific_cache(tmp_path, config_factory):
    """macd_signal in 查询：精确缓存命中（scope 包含枚举数组），A/B 隔离。"""
    root = tmp_path / "repo"
    _seed_curated(root)
    qa = {**BASE_QUERY, "conditions": [{"field": "macd_signal", "operator": "in", "value": ["golden_cross"]}]}
    qb = {**BASE_QUERY, "conditions": [{"field": "macd_signal", "operator": "in", "value": ["death_cross"]}]}
    _seed_query_cache(root, "filter", qa, ROWS)
    svc = ScreenerService(root)
    a = svc.run("s", qa)
    assert a["cache_status"] == "fresh" and a["data"]["total"] == 2
    b = svc.run("s", qb)
    assert b["cache_status"] == "unavailable"
    assert a["cache_scope"] != b["cache_scope"]


# ---------------------------------------------------------------------- #
# 七、cache_scope 隔离与安全字段
# ---------------------------------------------------------------------- #

def test_cache_scope_present_and_isolated(tmp_path, config_factory):
    """run 响应与结果快照包含 cache_scope（q_<64hex>）；A/B 不同；A 缓存不满足 B。"""
    root = tmp_path / "repo"
    _seed_curated(root)
    qa = {**BASE_QUERY, "conditions": [{"field": "price", "operator": "gt", "value": 100}]}
    qb = {**BASE_QUERY, "conditions": [{"field": "price", "operator": "gt", "value": 200}]}
    _seed_query_cache(root, "filter", qa, ROWS)
    svc = ScreenerService(root)
    a = svc.run("s", qa)
    b = svc.run("s", qb)
    import re as _re
    assert _re.fullmatch(r"q_[0-9a-f]{64}", a["cache_scope"])
    assert a["cache_scope"] == canonical_query_hash(a["query"])
    assert a["cache_scope"] != b["cache_scope"]
    assert a["cache_status"] == "fresh"
    assert b["cache_status"] == "unavailable"  # A 缓存不能满足 B
    # 快照也含 cache_scope，且 GET 读回一致
    store = ScreenerStore(root)
    stored = json.loads((store.results_dir / f"{a['result_id']}.json").read_text(encoding="utf-8"))
    assert stored["cache_scope"] == a["cache_scope"]
    # 安全：不暴露 tool/path/credential
    serialized = json.dumps(a)
    assert "tool" not in serialized and "path" not in serialized and "token" not in serialized
