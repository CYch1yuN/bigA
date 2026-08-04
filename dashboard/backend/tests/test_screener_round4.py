"""Phase E Codex 第四轮最终定点修正测试。

覆盖：
- screener 专用严格日期时间校验（_strict_date / _strict_aware_datetime）接受/拒绝矩阵
- 严格校验应用于 result.as_of/generated_at/source_fetched_at、saved.created_at/updated_at、
  candidate.added_at（naive 时间戳拒绝，禁止自动补 UTC）
- 两代并发公开流程：不手工篡改内部 map；第一代完全消费后自动清理；
  第二代独立运行；缓存读取恰好两次；owner 错误路径跨代不残留
"""
from __future__ import annotations

import json
import threading
import time as _time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

from app.screener_service import (
    ScreenerError,
    ScreenerService,
    ScreenerStore,
    _strict_aware_datetime,
    _strict_date,
    _validate_query,
    canonical_query_hash,
)
from app.westock_bridge import CAPABILITY_MAP


def _write_scope_cache(root: Path, capability: str, scope: str, data) -> Path:
    path = root / "state" / "dashboard" / "westock" / capability / f"{scope}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    fetched = datetime.now(timezone.utc).isoformat()
    payload = {
        "schema_version": 1, "capability": capability, "tool": CAPABILITY_MAP[capability].tool,
        "scope": scope, "source": "westock-mcp", "transport": "cache_export",
        "as_of": "2026-07-31", "fetched_at": fetched,
        "cached_at": fetched, "data": data, "warnings": [],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


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
     "reason": "业绩超预期", "matched_labels": ["high_dividend"],
     "factor_values": {"value": 80.0}},
    {"symbol": "000001.SZ", "name": "平安银行", "score": 60.0, "rank": 2,
     "price": 11.0, "change_percent": 0.5, "industry": "银行",
     "reason": "股息率较高", "matched_labels": [], "factor_values": {"value": 60.0}},
]


def _qhash() -> str:
    return canonical_query_hash(_validate_query(BASE_QUERY))


def _valid_payload(root: Path, **overrides) -> dict:
    payload = {
        "schema_version": 1, "mode": "condition", "source": "westock-mcp",
        "as_of": "2026-07-31", "source_fetched_at": "2026-08-03T04:00:00+00:00",
        "generated_at": "2026-08-03T12:00:00+00:00", "cache_status": "fresh",
        "is_realtime": False, "transport": "cache_export",
        "availability": {"filter": "fresh"},
        "query": _validate_query(BASE_QUERY),
        "data": {"items": [{"symbol": "600519.SH", "name": "贵州茅台", "score": 90.0,
                            "rank": 1, "local_history_available": True}], "total": 1},
        "warnings": ["ok"], "cache_scope": _qhash(),
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------- #
# 一、严格日期时间校验接受/拒绝矩阵
# ---------------------------------------------------------------------- #

@pytest.mark.parametrize("value", [
    "2026-08-04-secret", "prefix-2026-08-04", "2026-8-4", "2026-02-30",
    "", 123, [], {"x": 1}, None,
])
def test_strict_date_rejects(value):
    assert _strict_date(value) is None


@pytest.mark.parametrize("value", ["2026-08-04", "2000-01-01", "2024-12-31"])
def test_strict_date_accepts(value):
    assert _strict_date(value) == value


@pytest.mark.parametrize("value", [
    "2026-08-04", "2026-08-04T12:00:00",           # 纯日期 / naive 无时区
    "2026-08-04 12:00:00",                          # naive 空格分隔
    "2026-08-04T12:00:00.123",                      # naive 带毫秒
    "", 123, [], {"x": 1}, None,
])
def test_strict_aware_datetime_rejects(value):
    assert _strict_aware_datetime(value) is None


@pytest.mark.parametrize("value", [
    "2026-08-04T12:00:00+08:00", "2026-08-04T04:00:00Z",
    "2026-08-04T04:00:00+00:00", "2026-08-04T04:00:00.123456+00:00",
])
def test_strict_aware_datetime_accepts(value):
    assert _strict_aware_datetime(value) is not None


def test_strict_datetime_applied_to_state_files(tmp_path):
    """严格校验应用于 result/saved/candidate：naive 时间戳与宽松日期全部拒绝。"""
    root = tmp_path / "repo"
    store = ScreenerStore(root)
    store.ensure_dirs()

    # result：as_of 宽松（带后缀）拒绝；generated_at naive 拒绝；fetched naive 拒绝
    probes = {
        "1" * 32: _valid_payload(root, as_of="2026-08-04-secret"),
        "2" * 32: _valid_payload(root, as_of="prefix-2026-08-04"),
        "3" * 32: _valid_payload(root, as_of="2026-8-4"),
        "4" * 32: _valid_payload(root, as_of="2026-02-30"),
        "5" * 32: _valid_payload(root, as_of=""),
        "6" * 32: _valid_payload(root, generated_at="2026-08-04T12:00:00"),        # naive
        "7" * 32: _valid_payload(root, generated_at="2026-08-04 12:00:00"),        # naive
        "8" * 32: _valid_payload(root, generated_at=""),
        "9" * 32: _valid_payload(root, source_fetched_at="2026-08-04T12:00:00"),   # naive
        "a" * 32: _valid_payload(root, source_fetched_at="2026-08-04 12:00:00"),   # naive
        "b" * 32: _valid_payload(root, source_fetched_at=""),
    }
    for fid, payload in probes.items():
        (store.results_dir / f"{fid}.json").write_text(json.dumps(payload), encoding="utf-8")
    for fid in probes:
        assert store.read_result(fid) is None, f"应拒绝 {fid}"

    # saved：created_at/updated_at naive 拒绝
    q = _validate_query(BASE_QUERY)
    bad_saved = {"schema_version": 1, "items": [{
        "id": "f" * 32, "name": "x",
        "created_at": "2026-08-04T12:00:00",  # naive
        "updated_at": "2026-08-04T12:00:00+00:00", "query": q}]}
    store.saved_path.write_text(json.dumps(bad_saved), encoding="utf-8")
    assert store.load_saved() == []
    good_saved = {"schema_version": 1, "items": [{
        "id": "f" * 32, "name": "x",
        "created_at": "2026-08-04T12:00:00+00:00",
        "updated_at": "2026-08-04T12:00:00+00:00", "query": q}]}
    store.saved_path.write_text(json.dumps(good_saved), encoding="utf-8")
    assert len(store.load_saved()) == 1

    # candidate：added_at naive 拒绝
    bad_cand = {"schema_version": 1, "items": [{
        "symbol": "600519.SH", "name": "贵州茅台", "source_result_id": "0" * 32,
        "note": "", "added_at": "2026-08-04T12:00:00", "local_history_available": True}]}
    store.candidates_path.write_text(json.dumps(bad_cand), encoding="utf-8")
    assert store.load_candidates() == []
    good_cand = {"schema_version": 1, "items": [{
        "symbol": "600519.SH", "name": "贵州茅台", "source_result_id": "0" * 32,
        "note": "", "added_at": "2026-08-04T04:00:00Z", "local_history_available": True}]}
    store.candidates_path.write_text(json.dumps(good_cand), encoding="utf-8")
    assert len(store.load_candidates()) == 1


# ---------------------------------------------------------------------- #
# 二、两代并发公开流程（不手工篡改内部 map）
# ---------------------------------------------------------------------- #

class _CountingCache:
    def __init__(self, store):
        self.store = store
        self.calls = 0
        self.lock = threading.Lock()

    def read(self, capability, scope):
        with self.lock:
            if capability == "filter":  # 只统计选股能力读取（index/sector 为 universe 解析读取）
                self.calls += 1
        return self.store.read(capability, scope)


def _run_threads(svc, session_key: str, query: dict, n: int):
    """启动 n 个线程同一 session 同一 query；返回 (结果列表, 错误列表)。"""
    results: list = []
    errors: list = []
    lock = threading.Lock()
    barrier = threading.Barrier(n)

    def worker():
        barrier.wait()
        try:
            body = svc.run(session_key, dict(query))
            with lock:
                results.append(body)
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return results, errors


def test_two_generation_public_flow_success(tmp_path):
    """第一代 owner+4 waiter 正常完成 → map 自动清理 → 第二代 owner+4 waiter 独立完成；
    两代 result_id 不同、代内相同；缓存读取恰好 2 次；最终无残留。"""
    root = tmp_path / "repo"
    _seed_curated(root)
    _write_scope_cache(root, "filter", _qhash(), {"items": ROWS})
    svc = ScreenerService(root)
    original = svc.curated.westock_store
    counting = _CountingCache(original)
    svc.curated.westock_store = counting  # type: ignore[assignment]

    # 第一代：同一 session 5 线程
    gen1_results, gen1_errors = _run_threads(svc, "g1", BASE_QUERY, 5)
    assert not gen1_errors
    assert len(gen1_results) == 5
    gen1_ids = {r["result_id"] for r in gen1_results}
    assert len(gen1_ids) == 1
    # 第一代全部消费后 map 自动清理
    with svc._lock:
        assert svc._in_flight == {}

    # 立即第二代：同一 query
    gen2_results, gen2_errors = _run_threads(svc, "g2", BASE_QUERY, 5)
    assert not gen2_errors
    assert len(gen2_results) == 5
    gen2_ids = {r["result_id"] for r in gen2_results}
    assert len(gen2_ids) == 1
    # 两代 result_id 不同（独立运行），代内相同
    assert gen1_ids != gen2_ids
    assert gen1_results[0]["result_id"] != gen2_results[0]["result_id"]
    # 缓存读取恰好两次（每代 owner 一次）
    assert counting.calls == 2
    # 最终无残留
    with svc._lock:
        assert svc._in_flight == {}


def test_two_generation_public_flow_error_cleanup(tmp_path):
    """第一代 owner 错误（invalid_universe）传播给 waiter；第二代同 query 修复后成功；
    error 不跨代残留。"""
    root = tmp_path / "repo"
    _seed_curated(root)
    q = {**BASE_QUERY, "universe": {"type": "index", "value": "000001.SH"}}
    qhash = canonical_query_hash(_validate_query(q))
    _write_scope_cache(root, "filter", qhash, {"items": ROWS})  # filter 缓存存在但 index 缓存缺失
    svc = ScreenerService(root)
    original = svc.curated.westock_store
    counting = _CountingCache(original)
    svc.curated.westock_store = counting  # type: ignore[assignment]

    # 第一代：owner+2 waiter 全部收到 invalid_universe
    gen1_results, gen1_errors = _run_threads(svc, "e1", q, 3)
    assert gen1_results == []
    assert len(gen1_errors) == 3
    assert all(isinstance(e, ScreenerError) and e.code == "invalid_universe" for e in gen1_errors)
    with svc._lock:
        assert svc._in_flight == {}

    # 补 index 缓存 → 第二代同 query 成功
    _write_scope_cache(root, "index", "global", {
        "indexes": [{"code": "000001.SH", "constituents": [{"symbol": "600519.SH"}, {"symbol": "000001.SZ"}]}],
    })
    gen2_results, gen2_errors = _run_threads(svc, "e2", q, 3)
    assert not gen2_errors
    assert len(gen2_results) == 3
    assert len({r["result_id"] for r in gen2_results}) == 1
    assert gen2_results[0]["cache_status"] == "fresh"
    # 缓存读取恰好两次（两代各自 owner 读 filter 一次）
    assert counting.calls == 2
    with svc._lock:
        assert svc._in_flight == {}
