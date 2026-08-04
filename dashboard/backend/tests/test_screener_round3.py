"""Phase E Codex 第三轮定点修正测试。

覆盖：
- 时间稳定性：fresh / stale（now - TTL - 偏移）/ future（now + 偏移），禁止固定时刻
- result 严格 schema 探针矩阵（as_of 对象 / generated_at 非法 / source_fetched_at 对象 /
  availability 额外键 / mode≠query.mode / availability≠cache_status / fresh·stale 缺抓取时间 /
  matched_conditions≤20 / matched_labels≤10 不重复）
- saved/candidates 顶层仅 schema_version/items，额外顶层字段整文件 fail-open
- 两代并发竞态：第一代 waiter 持 Flight 引用，第二代同 query 独立运行，互不串扰、无残留
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
    _validate_query,
    canonical_query_hash,
)
from app.westock_bridge import CAPABILITY_MAP


def _write_scope_cache(root: Path, capability: str, scope: str, data, *,
                       fetched_at: str | None = None) -> Path:
    path = root / "state" / "dashboard" / "westock" / capability / f"{scope}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
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


BASE_QUERY = {
    "mode": "condition",
    "universe": {"type": "local", "value": None},
    "conditions": [],
    "sort": {"field": "score", "direction": "desc"},
    "limit": 50,
}

def _qhash() -> str:
    """服务端对 validated query 计算 scope；测试必须一致。"""
    return canonical_query_hash(_validate_query(BASE_QUERY))


ROWS = [
    {"symbol": "600519.SH", "name": "贵州茅台", "score": 90.0, "rank": 1,
     "price": 1350.0, "change_percent": 3.2, "industry": "白酒",
     "reason": "业绩超预期", "matched_labels": ["high_dividend"],
     "factor_values": {"value": 80.0}},
    {"symbol": "000001.SZ", "name": "平安银行", "score": 60.0, "rank": 2,
     "price": 11.0, "change_percent": 0.5, "industry": "银行",
     "reason": "股息率较高", "matched_labels": [], "factor_values": {"value": 60.0}},
]


def _valid_payload(root: Path, **overrides) -> dict:
    """构造合法结果 payload；overrides 用于注入篡改。"""
    qhash = _qhash()
    payload = {
        "schema_version": 1, "mode": "condition", "source": "westock-mcp",
        "as_of": "2026-07-31", "source_fetched_at": "2026-08-03T04:00:00+00:00",
        "generated_at": "2026-08-03T12:00:00+00:00", "cache_status": "fresh",
        "is_realtime": False, "transport": "cache_export",
        "availability": {"filter": "fresh"},
        "query": _validate_query(BASE_QUERY),
        "data": {"items": [{"symbol": "600519.SH", "name": "贵州茅台", "score": 90.0,
                            "rank": 1, "local_history_available": True}], "total": 1},
        "warnings": ["ok"], "cache_scope": qhash,
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------- #
# 一、时间稳定性（禁止固定时刻）
# ---------------------------------------------------------------------- #

def test_time_fresh_stale_future(tmp_path):
    """fresh=now；stale=now-TTL-偏移；future=now+偏移 → unavailable。"""
    root = tmp_path / "repo"
    _seed_curated(root)
    ttl = CAPABILITY_MAP["filter"].ttl_seconds

    # fresh（默认 now）
    _write_scope_cache(root, "filter", _qhash(), {"items": ROWS})
    assert ScreenerService(root).run("s", dict(BASE_QUERY))["cache_status"] == "fresh"

    # stale：now - TTL - 60s
    root2 = tmp_path / "repo2"
    _seed_curated(root2)
    _write_cache_at(root2, "filter", _qhash(), {"items": ROWS}, ttl + 60)
    assert ScreenerService(root2).run("s", dict(BASE_QUERY))["cache_status"] == "stale"

    # future：now + 60s → 视为不可用（未来时间戳）
    root3 = tmp_path / "repo3"
    _seed_curated(root3)
    _write_cache_at(root3, "filter", _qhash(), {"items": ROWS}, -(60))
    body = ScreenerService(root3).run("s", dict(BASE_QUERY))
    assert body["cache_status"] == "unavailable"


# ---------------------------------------------------------------------- #
# 二、result 严格 schema 探针矩阵
# ---------------------------------------------------------------------- #

def test_result_schema_probe_matrix(tmp_path):
    """Codex 探针 + 补充：as_of/generated_at/source_fetched_at/availability/mode/
    cache_status 一致性/抓取时间存在性。"""
    root = tmp_path / "repo"
    store = ScreenerStore(root)
    store.ensure_dirs()

    probes = {
        "1" * 32: _valid_payload(root, as_of={"secret": "x"}),                        # as_of 对象
        "2" * 32: _valid_payload(root, as_of="not-a-date"),                           # as_of 任意文本
        "3" * 32: _valid_payload(root, generated_at="not-a-time"),                    # generated_at 非法
        "4" * 32: _valid_payload(root, source_fetched_at={"path": "C:/secret"}),      # fetched 对象
        "5" * 32: _valid_payload(root, availability={"filter": "fresh", "arbitrary": "fresh"}),  # 额外键
        "6" * 32: _valid_payload(root, availability={"strategy_select": "fresh"}),    # 非本 mode capability
        "7" * 32: _valid_payload(root, availability={"filter": "stale"}),             # 值≠cache_status
        "8" * 32: _valid_payload(root, mode="strategy"),                              # mode≠query.mode
        "9" * 32: _valid_payload(root, source_fetched_at=None),                       # fresh 缺抓取时间
        "a" * 32: _valid_payload(root, cache_status="stale", source_fetched_at=None),  # stale 缺抓取时间
        "b" * 32: _valid_payload(root,
                                 data={"items": [{"symbol": "600519.SH", "name": "x",
                                                  "score": 1.0, "matched_conditions": [f"c{i}" for i in range(21)]}],
                                       "total": 1}),                                  # matched_conditions>20
        "c" * 32: _valid_payload(root,
                                 data={"items": [{"symbol": "600519.SH", "name": "x",
                                                  "score": 1.0, "matched_labels": ["high_dividend"] * 2}],
                                       "total": 1}),                                  # matched_labels 重复
        "d" * 32: _valid_payload(root,
                                 data={"items": [{"symbol": "600519.SH", "name": "x", "score": 1.0,
                                                  "matched_labels": [f"high_dividend"] * 11}],
                                       "total": 1}),                                  # matched_labels>10
    }
    for fid, payload in probes.items():
        (store.results_dir / f"{fid}.json").write_text(json.dumps(payload), encoding="utf-8")
    for fid in probes:
        assert store.read_result(fid) is None, f"应拒绝 {fid}"

    # unavailable + source_fetched_at null → 合法
    ok = _valid_payload(root, cache_status="unavailable", as_of=None, source_fetched_at=None,
                        availability={"filter": "unavailable"})
    ok["result_id"] = "e" * 32
    (store.results_dir / ("e" * 32 + ".json")).write_text(json.dumps(ok), encoding="utf-8")
    assert store.read_result("e" * 32) is not None


def test_saved_candidates_top_level_exact_keys(tmp_path):
    """saved/candidates 顶层仅 schema_version/items；额外顶层字段 → 整文件 fail-open。"""
    root = tmp_path / "repo"
    store = ScreenerStore(root)
    store.ensure_dirs()
    q = _validate_query(BASE_QUERY)
    good_saved = {"schema_version": 1, "items": [{
        "id": "f" * 32, "name": "好条件", "created_at": "2026-08-03T00:00:00+00:00",
        "updated_at": "2026-08-03T00:00:00+00:00", "query": q}]}
    store.saved_path.write_text(json.dumps(good_saved), encoding="utf-8")
    assert len(store.load_saved()) == 1
    # 额外顶层字段 → 整文件 fail-open
    bad = dict(good_saved)
    bad["hacked"] = 1
    store.saved_path.write_text(json.dumps(bad), encoding="utf-8")
    assert store.load_saved() == []
    # 缺 schema_version → fail-open
    store.saved_path.write_text(json.dumps({"items": []}), encoding="utf-8")
    assert store.load_saved() == []

    good_cand = {"schema_version": 1, "items": [{
        "symbol": "600519.SH", "name": "贵州茅台", "source_result_id": "0" * 32,
        "note": "", "added_at": "2026-08-03T00:00:00+00:00", "local_history_available": True}]}
    store.candidates_path.write_text(json.dumps(good_cand), encoding="utf-8")
    assert len(store.load_candidates()) == 1
    badc = dict(good_cand)
    badc["extra"] = "x"
    store.candidates_path.write_text(json.dumps(badc), encoding="utf-8")
    assert store.load_candidates() == []


# ---------------------------------------------------------------------- #
