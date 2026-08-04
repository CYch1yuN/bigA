"""Phase E Codex 第一轮修正测试：query-specific scope 隔离 / universe 交叉泄漏 /
mode 互斥 / 嵌套 forbidden / per-field schema / 去重与 missing-last / 真实线程并发 /
存储完整性 / candidate 名称派生 / 只读哈希。

全部 tmp_path 隔离；缓存按 canonical query hash 写入对应 scope。
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from app.main import create_app
from app.screener_service import ScreenerError, ScreenerService, ScreenerStore, canonical_query_hash
from app.westock_bridge import CAPABILITY_MAP


def _write_scope_cache(root: Path, capability: str, scope: str, data, *,
                       as_of: str = "2026-07-31", corrupt: bool = False) -> Path:
    path = root / "state" / "dashboard" / "westock" / capability / f"{scope}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    if corrupt:
        path.write_text("{ 损坏", encoding="utf-8")
        return path
    payload = {
        "schema_version": 1,
        "capability": capability,
        "tool": CAPABILITY_MAP[capability].tool,
        "scope": scope,
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


def _seed_query_cache(root: Path, capability: str, query: dict, rows: list) -> Path:
    from app.screener_service import _validate_query
    validated = _validate_query(query)
    return _write_scope_cache(root, capability, canonical_query_hash(validated), {"items": rows})


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


def _client(app):
    from fastapi.testclient import TestClient
    client = TestClient(app, base_url="https://127.0.0.1")
    client.post("/api/auth/login", json={"username": "admin", "password": "secret123"})
    return client


def _headers(client) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies.get("ashare_dash_csrf") or ""}


def _svc(root, clock=None):
    return ScreenerService(root, clock=clock)


def _expect_code(svc, query, code):
    from app.screener_service import _validate_query
    with pytest.raises(ScreenerError) as ei:
        _validate_query(query)
    assert ei.value.code == code


class _FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds: float):
        self.t += seconds


BASE_QUERY = {
    "mode": "condition",
    "universe": {"type": "local", "value": None},
    "conditions": [],
    "sort": {"field": "score", "direction": "desc"},
    "limit": 50,
}

ROWS = [
    {"symbol": "600519.SH", "name": "贵州茅台", "score": 90.0, "rank": 1,
     "price": 1350.0, "change_percent": 3.2, "industry": "白酒",
     "reason": "业绩超预期，估值合理",
     "matched_labels": ["high_dividend"], "factor_values": {"value": 80.0}},
    {"symbol": "000001.SZ", "name": "平安银行", "score": 60.0, "rank": 2,
     "price": 11.0, "change_percent": 0.5, "industry": "银行",
     "reason": "股息率较高", "matched_labels": [], "factor_values": {"value": 60.0}},
    {"symbol": "999999.SZ", "name": "无本地股", "score": 30.0, "rank": 3,
     "price": 5.0, "change_percent": -1.0, "industry": "未知",
     "reason": "x", "matched_labels": [], "factor_values": {"value": 30.0}},
]


# ---------------------------------------------------------------------- #
# 一、查询与缓存真实性（query-specific scope）
# ---------------------------------------------------------------------- #

def test_query_cache_isolation(tmp_path, config_factory):
    """Query A 有精确缓存，Query B 无 → B unavailable + 明确 warning；A 不被 B 使用。"""
    root = tmp_path / "repo"
    _seed_curated(root)
    query_a = {**BASE_QUERY, "conditions": [{"field": "price", "operator": "gt", "value": 100}]}
    query_b = {**BASE_QUERY, "conditions": [{"field": "price", "operator": "lt", "value": 100}]}
    _seed_query_cache(root, "filter", query_a, ROWS)
    svc = _svc(root)

    a = svc.run("s", query_a)
    assert a["cache_status"] == "fresh"
    assert a["data"]["total"] == 2  # local universe → curated 过滤（999999.SZ 不在 curated）

    b = svc.run("s", query_b)
    assert b["cache_status"] == "unavailable"
    assert b["data"]["items"] == []
    assert any("当前没有与该筛选条件精确匹配的 Westock 缓存导出" in w for w in b["warnings"])


def test_missing_precise_cache_warning(tmp_path, config_factory):
    """全局缓存绝不冒充精确匹配：即使存在 global 缓存也不返回。"""
    root = tmp_path / "repo"
    _seed_curated(root)
    _write_scope_cache(root, "filter", "global", {"items": ROWS})  # global 缓存存在
    svc = _svc(root)
    body = svc.run("s", dict(BASE_QUERY))
    assert body["cache_status"] == "unavailable"
    assert body["data"]["items"] == []
    assert any("精确匹配" in w for w in body["warnings"])


def test_query_hash_stable_and_deterministic(tmp_path):
    q1 = dict(BASE_QUERY)
    q2 = {**BASE_QUERY, "conditions": []}
    assert canonical_query_hash(q1) == canonical_query_hash(q2)
    assert canonical_query_hash(q1) != canonical_query_hash({**BASE_QUERY, "limit": 100})
    assert canonical_query_hash(q1).startswith("q_") and len(canonical_query_hash(q1)) == 66


# ---------------------------------------------------------------------- #
# 二、universe 严格隔离（交叉泄漏）
# ---------------------------------------------------------------------- #

def _seed_universe_caches(root: Path) -> None:
    _seed_curated(root)
    _seed_query_cache(root, "filter", dict(BASE_QUERY), ROWS)
    # 每个 universe 查询的 query-specific 缓存（universe 参与 hash）
    for uq in (
        {**BASE_QUERY, "universe": {"type": "index", "value": "000001.SH"}},
        {**BASE_QUERY, "universe": {"type": "index", "value": "399001.SZ"}},
        {**BASE_QUERY, "universe": {"type": "index", "value": "688001.SH"}},
        {**BASE_QUERY, "universe": {"type": "sector", "value": "BK01"}},
        {**BASE_QUERY, "universe": {"type": "sector", "value": "BK99"}},
        {**BASE_QUERY, "universe": {"type": "industry_chain", "value": "IC01"}},
        {**BASE_QUERY, "universe": {"type": "industry_chain", "value": "IC99"}},
    ):
        _seed_query_cache(root, "filter", uq, ROWS)
    _write_scope_cache(root, "index", "global", {
        "indexes": [
            {"code": "000001.SH", "constituents": [{"symbol": "600519.SH"}, {"symbol": "999999.SZ"}]},
            {"code": "399001.SZ", "constituents": [{"symbol": "000001.SZ"}]},
        ],
    })
    _write_scope_cache(root, "sector", "global", [
        {"code": "BK01", "name": "白酒", "leader_symbol": "600519.SH",
         "members": [{"symbol": "600519.SH"}, {"symbol": "999999.SZ"}]},
        {"code": "BK02", "name": "银行", "leader_symbol": "000001.SZ",
         "members": [{"symbol": "000001.SZ"}]},
    ])
    _write_scope_cache(root, "industry_chain", "global", [
        {"code": "IC01", "name": "白酒产业链",
         "upstream": [{"code": "N1", "name": "高粱", "related_symbols": ["000001.SZ"]}],
         "midstream": [{"code": "N2", "name": "酿造", "related_symbols": ["600519.SH"]}],
         "downstream": []},
    ])


def test_universe_local_curated_filter(tmp_path, config_factory):
    """local 严格解析为 curated 集合。"""
    root = tmp_path / "repo"
    _seed_universe_caches(root)
    svc = _svc(root)
    body = svc.run("s", dict(BASE_QUERY))
    symbols = {i["symbol"] for i in body["data"]["items"]}
    assert symbols == {"600519.SH", "000001.SZ"}  # 999999.SZ 不在 curated → 过滤
    assert "999999.SZ" not in symbols


def test_universe_index_no_cross_leak(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_universe_caches(root)
    clock = _FakeClock()
    svc = ScreenerService(root, clock=clock)
    a = svc.run("s", {**BASE_QUERY, "universe": {"type": "index", "value": "000001.SH"}})
    clock.advance(1.0)
    b = svc.run("s", {**BASE_QUERY, "universe": {"type": "index", "value": "399001.SZ"}})
    symbols_a = {i["symbol"] for i in a["data"]["items"]}
    symbols_b = {i["symbol"] for i in b["data"]["items"]}
    assert symbols_a == {"600519.SH", "999999.SZ"}  # 只含 A 指数成分
    assert symbols_b == {"000001.SZ"}
    assert "000001.SZ" not in symbols_a and "600519.SH" not in symbols_b
    # 找不到指数 → invalid_universe
    clock.advance(1.0)
    with pytest.raises(ScreenerError) as ei:
        svc.run("s", {**BASE_QUERY, "universe": {"type": "index", "value": "688001.SH"}})
    assert ei.value.code == "invalid_universe"


def test_universe_sector_members_proven(tmp_path, config_factory):
    """sector 按成员精确过滤；无法证明成员关系 → invalid_universe。"""
    root = tmp_path / "repo"
    _seed_universe_caches(root)
    svc = _svc(root)
    body = svc.run("s", {**BASE_QUERY, "universe": {"type": "sector", "value": "BK01"}})
    symbols = {i["symbol"] for i in body["data"]["items"]}
    assert symbols == {"600519.SH", "999999.SZ"}
    # 无成员缓存的板块 → invalid_universe
    _write_scope_cache(root, "sector", "global", [
        {"code": "BK99", "name": "无成员", "leader_symbol": None},
    ])
    with pytest.raises(ScreenerError) as ei:
        svc.run("s", {**BASE_QUERY, "universe": {"type": "sector", "value": "BK99"}})
    assert ei.value.code == "invalid_universe"


def test_universe_industry_chain_proven(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_universe_caches(root)
    svc = _svc(root)
    body = svc.run("s", {**BASE_QUERY, "universe": {"type": "industry_chain", "value": "IC01"}})
    symbols = {i["symbol"] for i in body["data"]["items"]}
    assert symbols == {"600519.SH", "000001.SZ"}
    with pytest.raises(ScreenerError) as ei:
        svc.run("s", {**BASE_QUERY, "universe": {"type": "industry_chain", "value": "IC99"}})
    assert ei.value.code == "invalid_universe"


# ---------------------------------------------------------------------- #
# 三、受控请求模型
# ---------------------------------------------------------------------- #

def test_mode_mutually_exclusive(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_curated(root)
    svc = _svc(root)
    _expect_code(svc, {**BASE_QUERY, "mode": "condition", "strategy": {"name": "ma_breakout"}}, "invalid_mode")
    _expect_code(svc, {**BASE_QUERY, "mode": "strategy", "conditions": [{"field": "price", "operator": "gt", "value": 1}]}, "invalid_mode")
    _expect_code(svc, {**BASE_QUERY, "mode": "factor", "labels": {"values": ["high_dividend"], "match": "any"}}, "invalid_mode")
    _expect_code(svc, {**BASE_QUERY, "mode": "label", "factor": {"name": "value"}}, "invalid_mode")


def test_nested_forbidden_keys(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_curated(root)
    svc = _svc(root)
    attacks = [
        {**BASE_QUERY, "strategy": {"name": "ma_breakout", "script": "alert(1)"}},
        {**BASE_QUERY, "universe": {"type": "local", "value": None, "path": "/etc"}},
        {**BASE_QUERY, "conditions": [{"field": "price", "operator": "gt", "value": 1, "tool": "x"}]},
        {**BASE_QUERY, "mode": "factor", "factor": {"name": "value", "expression": "1+1"}},
        {"mode": "condition", "universe": {"type": "local", "value": None},
         "conditions": [{"field": "price", "operator": "gt", "value": 1}],
         "nested": {"deep": {"command": "rm -rf"}}},
        {**BASE_QUERY, "code": "evil"},
        {**BASE_QUERY, "mcp_params": {"x": 1}},
    ]
    from app.screener_service import _validate_query
    for attack in attacks:
        with pytest.raises(ScreenerError) as ei:
            _validate_query(attack)
        assert ei.value.code == "invalid_request", attack


def test_per_field_operator_type(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_curated(root)
    svc = _svc(root)
    _expect_code(svc, {**BASE_QUERY, "conditions": [{"field": "ma5_above_ma20", "operator": "gt", "value": 1}]}, "invalid_condition")
    _expect_code(svc, {**BASE_QUERY, "conditions": [{"field": "ma5_above_ma20", "operator": "eq", "value": "yes"}]}, "invalid_condition")
    _expect_code(svc, {**BASE_QUERY, "conditions": [{"field": "price", "operator": "in", "value": [1, 2]}]}, "invalid_condition")
    _expect_code(svc, {**BASE_QUERY, "conditions": [{"field": "price", "operator": "between", "value": [5, 1]}]}, "invalid_condition")
    _expect_code(svc, {**BASE_QUERY, "conditions": [{"field": "price", "operator": "between", "value": [1]}]}, "invalid_condition")
    _expect_code(svc, {**BASE_QUERY, "conditions": [{"field": "price", "operator": "gt", "value": float("nan")}]}, "invalid_condition")
    _expect_code(svc, {**BASE_QUERY, "conditions": [{"field": "hacked", "operator": "gt", "value": 1}]}, "invalid_condition")
    _expect_code(svc, {**BASE_QUERY, "conditions": [{"field": "price", "operator": "approx", "value": 1}]}, "invalid_condition")
    # 合法布尔 / between / 数值
    _seed_query_cache(root, "filter", {**BASE_QUERY, "conditions": [{"field": "ma5_above_ma20", "operator": "eq", "value": True}]}, ROWS)
    svc2 = _svc(root)
    assert svc2.run("s", {**BASE_QUERY, "conditions": [{"field": "ma5_above_ma20", "operator": "eq", "value": True}]})["cache_status"] == "fresh"
    _seed_query_cache(root, "filter", {**BASE_QUERY, "conditions": [{"field": "price", "operator": "between", "value": [1, 100]}]}, ROWS)
    assert svc2.run("s", {**BASE_QUERY, "conditions": [{"field": "price", "operator": "between", "value": [1, 100]}]})["cache_status"] == "fresh"


def test_precise_error_codes(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_curated(root)
    svc = _svc(root)
    _expect_code(svc, {**BASE_QUERY, "mode": "bad"}, "invalid_mode")
    _expect_code(svc, {**BASE_QUERY, "universe": {"type": "nope", "value": None}}, "invalid_universe")
    _expect_code(svc, {**BASE_QUERY, "conditions": [{"field": "x", "operator": "gt", "value": 1}]}, "invalid_condition")
    # strategy/factor/label 模式查询不含 conditions 字段（mode 互斥）
    _expect_code(svc, {"mode": "strategy", "universe": {"type": "local", "value": None},
                       "strategy": {"name": "x"}, "sort": {"field": "score", "direction": "desc"}, "limit": 50},
                 "invalid_strategy")
    _expect_code(svc, {"mode": "factor", "universe": {"type": "local", "value": None},
                       "factor": {"name": "x"}, "sort": {"field": "score", "direction": "desc"}, "limit": 50},
                 "invalid_factor")
    _expect_code(svc, {"mode": "label", "universe": {"type": "local", "value": None},
                       "labels": {"values": ["x"]}, "sort": {"field": "score", "direction": "desc"}, "limit": 50},
                 "invalid_label")


# ---------------------------------------------------------------------- #
# 四、结果标准化与排序
# ---------------------------------------------------------------------- #

def test_dedupe_and_missing_last(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_curated(root)
    # 补 999999.SZ curated，使 local universe 保留三只（专注验证去重与 missing-last）
    curated = root / "data" / "curated"
    extra = curated / "daily_quotes_999999.SZ_2026-07-01_2026-07-31.parquet"
    if not extra.exists():
        df = pd.DataFrame({
            "symbol": ["999999.SZ"] * 2, "trade_date": ["2026-07-31"] * 2,
            "open_raw": [1.0] * 2, "high_raw": [2.0] * 2, "low_raw": [0.5] * 2,
            "close_raw": [1.5] * 2, "open_qfq": [1.0] * 2, "high_qfq": [2.0] * 2,
            "low_qfq": [0.5] * 2, "close_qfq": [1.5] * 2, "volume": [1] * 2, "amount": [1e4] * 2,
        })
        df.to_parquet(extra)
    rows = [
        {"symbol": "600519.SH", "name": "重复A", "score": 10.0, "rank": 1},
        {"symbol": "600519.SH", "name": "重复B", "score": 99.0, "rank": 2},  # 同 symbol 保留排序最优
        {"symbol": "000001.SZ", "name": "无分", "score": None, "rank": 3},
        {"symbol": "999999.SZ", "name": "低分", "score": 5.0, "rank": 4},
    ]
    _seed_query_cache(root, "filter", dict(BASE_QUERY), rows)
    svc = _svc(root)

    _seed_query_cache(root, "filter", {**BASE_QUERY, "sort": {"field": "score", "direction": "asc"}}, rows)
    desc = svc.run("s", dict(BASE_QUERY))  # score desc
    items = desc["data"]["items"]
    symbols = [i["symbol"] for i in items]
    assert symbols == ["600519.SH", "999999.SZ", "000001.SZ"]  # 99 分保留、缺失最后
    assert items[0]["name"] == "重复B"  # 排序最优保留

    asc = svc.run("s", {**BASE_QUERY, "sort": {"field": "score", "direction": "asc"}})
    symbols_asc = [i["symbol"] for i in asc["data"]["items"]]
    assert symbols_asc == ["999999.SZ", "600519.SH", "000001.SZ"]  # asc 缺失也最后


def test_result_fixed_schema_no_leak(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_curated(root)
    rows = [
        {"symbol": "600519.SH", "name": "茅台", "score": 90.0,
         "matched_labels": ["high_dividend", "hacked_label"],  # 非白名单丢弃
         "matched_conditions": ["c1", 123, "c2"],  # 非文本丢弃
         "factor_values": {"value": 80.0, "hacked": 1.0, "growth": float("nan")},  # 非法键/NaN 丢弃
         "industry": {"nested": "obj"},  # 嵌套对象 → 文本化？应丢弃
         "price": float("inf")},
    ]
    _seed_query_cache(root, "filter", dict(BASE_QUERY), rows)
    svc = _svc(root)
    body = svc.run("s", dict(BASE_QUERY))
    row = body["data"]["items"][0]
    assert row["matched_labels"] == ["high_dividend"]
    assert row["matched_conditions"] == ["c1", "c2"]
    assert row["factor_values"] == {"value": 80.0}
    assert "price" not in row  # Infinity 丢弃
    assert "industry" not in row  # 嵌套对象不输出
    serialized = json.dumps(body)
    assert "NaN" not in serialized and "Infinity" not in serialized
    assert "hacked_label" not in serialized and "hacked" not in serialized


# ---------------------------------------------------------------------- #
# 五、真实线程并发去重
# ---------------------------------------------------------------------- #

class _CountingCache:
    """计数读取：证明相同查询只执行一次。"""

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


def test_concurrent_dedup_real_threads(tmp_path, config_factory):
    """真实线程阻塞式验证（同一 session、同一 query、5 线程）：全部成功、
    同一 result_id、缓存只读一次、无 429——waiter 不重复占用执行配额。"""
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
            body = svc.run("same-session", dict(BASE_QUERY))  # 同一 session
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
    assert len(results) == 5  # 5 个都成功（无 429）
    ids = {r["result_id"] for r in results}
    assert len(ids) == 1  # 所有等待者同一 result_id
    assert counting.calls <= 1  # 缓存只读一次（owner）


def test_concurrent_different_queries_still_rate_limited(tmp_path, config_factory):
    """不同 query 仍按 session 每秒 2 次限流：第三个不同 query 429。"""
    root = tmp_path / "repo"
    _seed_curated(root)
    clock = _FakeClock()
    svc = ScreenerService(root, clock=clock)
    q1 = {**BASE_QUERY, "conditions": [{"field": "price", "operator": "gt", "value": 1}]}
    q2 = {**BASE_QUERY, "conditions": [{"field": "price", "operator": "gt", "value": 2}]}
    q3 = {**BASE_QUERY, "conditions": [{"field": "price", "operator": "gt", "value": 3}]}
    _seed_query_cache(root, "filter", q1, ROWS)
    _seed_query_cache(root, "filter", q2, ROWS)
    _seed_query_cache(root, "filter", q3, ROWS)
    svc.run("s", q1)
    svc.run("s", q2)
    with pytest.raises(ScreenerError) as ei:
        svc.run("s", q3)
    assert ei.value.code == "rate_limited"


# ---------------------------------------------------------------------- #
# 六、存储完整性
# ---------------------------------------------------------------------- #

def test_result_collision_regenerates(tmp_path, config_factory):
    root = tmp_path / "repo"
    store = ScreenerStore(root)
    store.ensure_dirs()
    store.write_result("0" * 32, {"schema_version": 1, "result_id": "0" * 32, "data": {"items": []}, "query": {"mode": "condition"}})
    with pytest.raises(ScreenerError) as ei:
        store.write_result("0" * 32, {"schema_version": 1, "result_id": "0" * 32, "data": {"items": []}, "query": {"mode": "condition"}})
    assert ei.value.code == "result_collision"


def test_result_file_size_limit(tmp_path, config_factory):
    """真实 >2MiB UTF-8 payload → result_too_large 且无文件落盘。"""
    root = tmp_path / "repo"
    store = ScreenerStore(root)
    store.ensure_dirs()
    huge = {"schema_version": 1, "result_id": "1" * 32, "mode": "condition",
            "source": "westock-mcp", "transport": "cache_export", "is_realtime": False,
            "cache_status": "fresh", "as_of": None, "source_fetched_at": None,
            "generated_at": "2026-07-31T00:00:00+00:00", "availability": {"filter": "fresh"},
            "query": {"mode": "condition", "universe": {"type": "local", "value": None},
                      "conditions": [], "strategy": None, "factor": None, "labels": None,
                      "sort": {"field": "score", "direction": "desc"}, "limit": 50},
            "data": {"items": [{"symbol": "600519.SH", "name": "股" * 700000}], "total": 1},
            "warnings": ["w"], "cache_scope": "q_" + "0" * 64}
    blob = json.dumps(huge, ensure_ascii=False)
    assert len(blob.encode("utf-8")) > 2 * 1024 * 1024  # 确认真实超限
    with pytest.raises(ScreenerError) as ei:
        store.write_result("1" * 32, huge)
    assert ei.value.code == "result_too_large"
    assert not (root / "state" / "dashboard" / "screener" / "results" / ("1" * 32 + ".json")).exists()


def test_tampered_state_fail_open(tmp_path, config_factory):
    root = tmp_path / "repo"
    store = ScreenerStore(root)
    store.ensure_dirs()
    # 合法 JSON 但含错误类型/非法内容/缺失必需字段 → 严格 schema 丢弃
    store.candidates_path.write_text(json.dumps({
        "schema_version": 1,
        "items": [
            {"symbol": "600519.SH", "name": "贵州茅台", "source_result_id": "0" * 32,
             "note": "ok", "added_at": "2026-07-31T00:00:00+00:00", "local_history_available": True},
            {"symbol": "BAD-SYM", "name": "x", "source_result_id": "0" * 32,
             "note": "", "added_at": "2026-07-31T00:00:00+00:00", "local_history_available": False},
            {"symbol": "000001.SZ", "name": "x", "source_result_id": "nothex",
             "note": "", "added_at": "2026-07-31T00:00:00+00:00", "local_history_available": False},
            {"symbol": "000001.SZ", "name": "x", "source_result_id": "0" * 32,
             "note": "<script>alert(1)</script>", "added_at": "2026-07-31T00:00:00+00:00", "local_history_available": False},
            {"symbol": "000001.SZ", "name": "x", "source_result_id": "0" * 32,
             "note": "", "added_at": "not-a-time", "local_history_available": False},
            {"symbol": "000001.SZ", "name": "x", "source_result_id": "0" * 32,
             "note": "", "added_at": "2026-07-31T00:00:00+00:00", "local_history_available": "yes"},
        ],
    }), encoding="utf-8")
    items = store.load_candidates()
    assert len(items) == 1  # 仅完全合法的条目保留
    assert items[0]["symbol"] == "600519.SH"
    # 合法 JSON 但任意额外字段/缺失必需字段的 result → 丢弃
    store.results_dir.joinpath("f" * 32 + ".json").write_text(json.dumps({"schema_version": 1, "hacked": 1}), encoding="utf-8")
    assert store.read_result("f" * 32) is None
    store.results_dir.joinpath("e" * 32 + ".json").write_text(json.dumps({
        "schema_version": 1, "result_id": "e" * 32, "mode": "condition", "source": "westock-mcp",
        "transport": "cache_export", "is_realtime": False, "cache_status": "fresh",
        "as_of": None, "source_fetched_at": None, "generated_at": "2026-07-31T00:00:00+00:00",
        "availability": {"filter": "fresh"},
        "query": {"mode": "condition", "universe": {"type": "local", "value": None}, "conditions": [],
                  "strategy": None, "factor": None, "labels": None,
                  "sort": {"field": "score", "direction": "desc"}, "limit": 50},
        "data": {"items": [], "total": 0}, "warnings": ["w"],
        "cache_scope": "q_" + "0" * 64,
    }), encoding="utf-8")
    assert store.read_result("e" * 32) is None  # cache_scope 与 query hash 不一致 → 拒绝


def test_atomic_replace_failure_keeps_old(tmp_path, config_factory, monkeypatch):
    """monkeypatch os.replace 抛 OSError：旧文件逐字节不变、无 .tmp.* 残留、抛原始异常。"""
    import app.screener_service as svc_mod
    root = tmp_path / "repo"
    store = ScreenerStore(root)
    store.ensure_dirs()
    old_items = [{"symbol": "600519.SH", "name": "贵州茅台", "source_result_id": "0" * 32,
                  "note": "旧", "added_at": "2026-07-31T00:00:00+00:00", "local_history_available": True}]
    store.save_candidates(old_items)
    old_bytes = store.candidates_path.read_bytes()

    def _boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(svc_mod.os, "replace", _boom)
    with pytest.raises(OSError, match="simulated"):
        store.save_candidates([{
            "symbol": "000001.SZ", "name": "平安银行", "source_result_id": "0" * 32,
            "note": "新", "added_at": "2026-07-31T00:00:00+00:00", "local_history_available": False}])
    monkeypatch.undo()

    assert store.candidates_path.read_bytes() == old_bytes  # 旧文件逐字节不变
    assert not list(store.dir.glob(".tmp.*"))  # 无残留临时文件
    # saved 同样覆盖一次原子写失败
    old_saved = {"schema_version": 1, "items": [{
        "id": "a" * 32, "name": "旧条件", "created_at": "2026-07-31T00:00:00+00:00",
        "updated_at": "2026-07-31T00:00:00+00:00", "query": BASE_QUERY}]}
    store.saved_path.write_text(json.dumps(old_saved), encoding="utf-8")
    old_saved_bytes = store.saved_path.read_bytes()
    monkeypatch.setattr(svc_mod.os, "replace", _boom)
    with pytest.raises(OSError, match="simulated"):
        store.save_saved([{
            "id": "b" * 32, "name": "新条件", "created_at": "2026-07-31T00:00:00+00:00",
            "updated_at": "2026-07-31T00:00:00+00:00", "query": BASE_QUERY}])
    monkeypatch.undo()
    assert store.saved_path.read_bytes() == old_saved_bytes
    assert not list(store.dir.glob(".tmp.*"))


# ---------------------------------------------------------------------- #
# 七、saved / candidates
# ---------------------------------------------------------------------- #

def test_candidate_name_derived_from_result(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_curated(root)
    _seed_query_cache(root, "filter", dict(BASE_QUERY), ROWS)
    app = _make_app(root, config_factory)
    client = _client(app)
    run_id = client.post("/api/screener/run", json=BASE_QUERY, headers=_headers(client)).json()["result_id"]
    # 即使 body 传伪造 name，也由结果行派生
    r = client.post("/api/screener/candidates",
                    json={"symbol": "600519.SH", "source_result_id": run_id, "note": "研究关注", "name": "伪造名"},
                    headers=_headers(client))
    assert r.status_code == 200
    listed = client.get("/api/screener/candidates").json()
    assert listed["items"][0]["name"] == "贵州茅台"  # 来自结果行
    # note 安全
    r2 = client.post("/api/screener/candidates",
                     json={"symbol": "000001.SZ", "source_result_id": run_id, "note": "x<script>alert(1)</script>"},
                     headers=_headers(client))
    assert r2.status_code == 400
    r3 = client.post("/api/screener/candidates",
                     json={"symbol": "000001.SZ", "source_result_id": run_id, "note": "a\x01b"},
                     headers=_headers(client))
    assert r3.status_code == 400


def test_saved_filter_roundtrip_and_validation(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_curated(root)
    app = _make_app(root, config_factory)
    client = _client(app)
    r = client.post("/api/screener/saved", json={"name": "我的条件", "query": BASE_QUERY}, headers=_headers(client))
    assert r.status_code == 200
    saved_id = r.json()["saved_id"]
    assert len(saved_id) == 32 and all(c in "0123456789abcdef" for c in saved_id)
    listed = client.get("/api/screener/saved").json()
    assert len(listed["items"]) == 1
    # 篡改 saved 文件（合法 JSON 但非法 query）→ 加载时丢弃
    store = ScreenerStore(root)
    store.save_saved([{"id": saved_id, "name": "好", "query": {**BASE_QUERY, "mode": "hacked"},
                       "created_at": "x", "updated_at": "x"}])
    assert client.get("/api/screener/saved").json()["items"] == []
    # 删除：严格 ID
    assert client.delete("/api/screener/saved/nothex", headers=_headers(client)).status_code == 400


# ---------------------------------------------------------------------- #
# 限流（注入时钟）
# ---------------------------------------------------------------------- #

def test_run_rate_limit_clock_injected(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_curated(root)
    _seed_query_cache(root, "filter", dict(BASE_QUERY), ROWS)
    clock = _FakeClock()
    svc = ScreenerService(root, clock=clock)
    svc.run("session-a", dict(BASE_QUERY))
    svc.run("session-a", dict(BASE_QUERY))
    with pytest.raises(ScreenerError) as ei:
        svc.run("session-a", dict(BASE_QUERY))
    assert ei.value.code == "rate_limited"
    assert ei.value.status_code == 429
    svc.run("session-b", dict(BASE_QUERY))
    clock.advance(1.0)
    svc.run("session-a", dict(BASE_QUERY))


# ---------------------------------------------------------------------- #
# 认证 + CSRF + 只读哈希
# ---------------------------------------------------------------------- #

def test_screener_requires_auth_and_csrf(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_curated(root)
    _seed_query_cache(root, "filter", dict(BASE_QUERY), ROWS)
    app = _make_app(root, config_factory)
    from fastapi.testclient import TestClient
    with TestClient(app, base_url="https://127.0.0.1") as client:
        assert client.get("/api/screener/saved").status_code == 401
        assert client.post("/api/screener/run", json=BASE_QUERY).status_code == 401
        client.post("/api/auth/login", json={"username": "admin", "password": "secret123"})
        assert client.post("/api/screener/run", json=BASE_QUERY).status_code == 403


def test_run_condition_success_api(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_curated(root)
    _seed_query_cache(root, "filter", dict(BASE_QUERY), ROWS)
    app = _make_app(root, config_factory)
    client = _client(app)
    r = client.post("/api/screener/run", json=BASE_QUERY, headers=_headers(client))
    assert r.status_code == 200
    body = r.json()
    assert body["result_id"] and len(body["result_id"]) == 32
    assert body["is_realtime"] is False and body["transport"] == "cache_export"
    assert body["data"]["total"] == 2  # local universe 过滤掉 999999.SZ
    # 结果可读回；快照 query 无禁止字段
    r2 = client.get(f"/api/screener/results/{body['result_id']}")
    assert r2.status_code == 200
    assert "capability" not in json.dumps(r2.json()["query"])


def test_sha256_readonly(tmp_path, config_factory):
    root = tmp_path / "repo"
    _seed_curated(root)
    _seed_query_cache(root, "filter", dict(BASE_QUERY), ROWS)
    rep = root / "reports" / "phase-4" / "daily" / "2026-07-31"
    rep.mkdir(parents=True)
    signals = rep / "signals.json"
    orders = rep / "simulated-orders.json"
    signals.write_text(json.dumps({"signals": []}), encoding="utf-8")
    orders.write_text(json.dumps({"orders": []}), encoding="utf-8")
    acc = root / "state" / "automation" / "accounts"
    acc.mkdir(parents=True)
    account = acc / "paper-steady.json"
    account.write_text(json.dumps({"account_id": "paper-steady"}), encoding="utf-8")
    gate = root / "state" / "gate4b"
    gate.mkdir(parents=True)
    gate_state = gate / "state.json"
    gate_state.write_text(json.dumps({"phase": "observed"}), encoding="utf-8")
    daily_file = rep / "daily.md"
    daily_file.write_text("# daily", encoding="utf-8")
    parquet = sorted((root / "data" / "curated").glob("*.parquet"))[0]

    targets = [signals, orders, account, gate_state, daily_file, parquet]
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in targets}
    app = _make_app(root, config_factory)
    client = _client(app)
    run_id = client.post("/api/screener/run", json=BASE_QUERY, headers=_headers(client)).json()["result_id"]
    client.post("/api/screener/candidates", json={"symbol": "600519.SH", "source_result_id": run_id, "note": "x"}, headers=_headers(client))
    after = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in targets}
    assert before == after
