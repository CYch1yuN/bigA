"""Phase F3 第二轮聚焦测试：严格请求模型 / 去重限流所有权 / schema 校验原子写 / worker jobs / coverage / API。

全部使用 tmp_path 隔离（project_root=tmp_path），不触碰真实 state。
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.main import CSRF_COOKIE, create_app
from app.westock_bridge import WestockCacheStore
from app.westock_refresh_service import (
    FORBIDDEN_KEYS,
    JOB_STATUSES,
    REQUEST_ID_RE,
    RefreshError,
    RefreshStore,
    _STOCK_PRESETS,
    _MARKET_PRESETS,
    build_refresh_store,
    canonical_request_hash,
    session_fingerprint,
)

NOW = datetime.now(timezone.utc)


def _mk_store(tmp_path: Path, *symbols: str) -> RefreshStore:
    curated = tmp_path / "data" / "curated"
    curated.mkdir(parents=True, exist_ok=True)
    for sym in symbols or ("600519.SH", "000001.SZ"):
        (curated / f"daily_quotes_{sym}_x.parquet").write_bytes(b"x")
    return build_refresh_store(tmp_path)


def _seed_screener_snapshot(tmp_path: Path, result_id: str = "ab" * 16):
    from app.screener_service import ScreenerStore, canonical_query_hash
    s = ScreenerStore(tmp_path)
    query = {"mode": "condition", "universe": {"type": "local", "value": None},
             "conditions": [{"field": "ma5_above_ma20", "operator": "eq", "value": True}],
             "strategy": None, "factor": None, "labels": None,
             "sort": {"field": "score", "direction": "desc"}, "limit": 20}
    scope = canonical_query_hash(query)
    snap = {
        "schema_version": 1, "result_id": result_id, "mode": "condition",
        "source": "westock-mcp", "as_of": "2026-08-04", "source_fetched_at": None,
        "generated_at": "2026-08-05T00:00:00+00:00", "cache_status": "unavailable",
        "is_realtime": False, "transport": "cache_export",
        "availability": {"filter": "unavailable"}, "query": query,
        "data": {"items": [], "total": 0}, "warnings": [], "cache_scope": scope,
    }
    s.write_result(result_id, snap)
    return result_id, scope


# ---------------------------------------------------------------------- #
# 一、严格请求模型
# ---------------------------------------------------------------------- #
def test_multi_symbol_jobs_and_lhb_global_dedup(tmp_path):
    store = _mk_store(tmp_path)
    req = store.create_request(body={"target": "stock", "preset": "funds",
                                     "symbols": ["600519.SH", "000001.SZ"]}, session_id="s1")
    caps = _STOCK_PRESETS["funds"]
    assert len(req["jobs"]) == 2 * len(caps) - 1  # LHB global 去重
    lhb = [j for j in req["jobs"] if j["capability"] == "lhb"]
    assert len(lhb) == 1 and lhb[0]["scope"] == "global"
    assert all(j["scope"] in ("global", "600519.SH", "000001.SZ") for j in req["jobs"])
    assert all(j["capability"] in caps for j in req["jobs"])
    assert "tool" not in json.dumps(req["jobs"])


def test_stock_symbols_strict_no_strip_upper(tmp_path):
    store = _mk_store(tmp_path)
    for bad in ("600519.sh", " 600519.SH", "600519.SH ", "600519", "sh600519"):
        with pytest.raises(RefreshError) as exc:
            store.create_request(body={"target": "stock", "preset": "basic",
                                       "symbols": [bad]}, session_id="s1")
        assert exc.value.code == "invalid_symbols"
    with pytest.raises(RefreshError):
        store.create_request(body={"target": "stock", "preset": "basic",
                                   "symbols": [f"60051{i}.SH" for i in range(21)]},
                             session_id="s1")


def test_non_local_symbol_summary_only(tmp_path):
    store = _mk_store(tmp_path, "600519.SH")
    with pytest.raises(RefreshError) as exc:
        store.create_request(body={"target": "stock", "preset": "basic",
                                   "symbols": ["300750.SZ"]}, session_id="s1")
    assert exc.value.code == "invalid_symbols"
    req = store.create_request(body={"target": "stock", "preset": "basic",
                                     "symbols": ["300750.SZ"], "allow_summary_only": True},
                               session_id="s1")
    assert all(j["summary_only"] is True for j in req["jobs"])


def test_preset_or_capabilities_exclusive(tmp_path):
    store = _mk_store(tmp_path)
    with pytest.raises(RefreshError):
        store.create_request(body={"target": "stock", "preset": "basic",
                                   "capabilities": ["quote"], "symbols": ["600519.SH"]},
                             session_id="s1")
    req = store.create_request(body={"target": "stock", "capabilities": ["quote"],
                                     "symbols": ["600519.SH"]}, session_id="s1")
    assert [j["capability"] for j in req["jobs"]] == ["quote"]
    with pytest.raises(RefreshError) as exc:
        store.create_request(body={"target": "stock", "capabilities": ["nope"],
                                   "symbols": ["600519.SH"]}, session_id="s1")
    assert exc.value.code == "invalid_capability"


@pytest.mark.parametrize("preset,caps", [(k, tuple(v)) for k, v in _STOCK_PRESETS.items()])
def test_all_stock_presets_exact(tmp_path, preset, caps):
    store = _mk_store(tmp_path)
    req = store.create_request(body={"target": "stock", "preset": preset,
                                     "symbols": ["600519.SH"]}, session_id="s1")
    got = tuple(sorted({j["capability"] for j in req["jobs"]}))
    assert got == tuple(sorted(set(caps))), preset


@pytest.mark.parametrize("preset,caps", [(k, tuple(v)) for k, v in _MARKET_PRESETS.items()])
def test_all_market_presets_global(tmp_path, preset, caps):
    store = _mk_store(tmp_path)
    req = store.create_request(body={"target": "market", "preset": preset}, session_id="s1")
    assert all(j["scope"] == "global" for j in req["jobs"])
    assert tuple(sorted({j["capability"] for j in req["jobs"]})) == tuple(sorted(set(caps)))


def test_screener_qhash_recomputed_and_forged_rejected(tmp_path):
    store = _mk_store(tmp_path)
    result_id, scope = _seed_screener_snapshot(tmp_path)
    req = store.create_request(body={"target": "screener", "result_id": result_id,
                                     "cache_scope": scope}, session_id="s1")
    assert req["jobs"][0]["capability"] == "filter"
    assert req["jobs"][0]["scope"] == scope and scope.startswith("q_")
    assert len(scope) == 66  # q_ + 64hex
    with pytest.raises(RefreshError) as exc:
        store.create_request(body={"target": "screener", "result_id": result_id,
                                   "cache_scope": "q_" + "f" * 64}, session_id="s1")
    assert exc.value.code == "invalid_cache_scope"


def test_forbidden_keys_full_matrix(tmp_path):
    store = _mk_store(tmp_path)
    for key in FORBIDDEN_KEYS:
        body = {"target": "stock", "preset": "basic", "symbols": ["600519.SH"], key: "x"}
        with pytest.raises(RefreshError) as exc:
            store.create_request(body=body, session_id="s1")
        assert exc.value.code == "invalid_request", key
    # 嵌套任意层级
    with pytest.raises(RefreshError):
        store.create_request(body={"target": "stock", "preset": "basic",
                                   "symbols": ["600519.SH"],
                                   "nested": {"deep": {"token": "x"}}}, session_id="s1")


def test_request_body_unknown_key_rejected(tmp_path):
    store = _mk_store(tmp_path)
    with pytest.raises(RefreshError) as exc:
        store.create_request(body={"target": "stock", "preset": "basic",
                                   "symbols": ["600519.SH"], "hacked": 1}, session_id="s1")
    assert exc.value.code == "invalid_request"


# ---------------------------------------------------------------------- #
# 二、去重 / 限流 / 所有权
# ---------------------------------------------------------------------- #
def test_dedup_before_rate_limit(tmp_path):
    store = _mk_store(tmp_path)
    r1 = store.create_request(body={"target": "market", "preset": "overview"}, session_id="s1")
    r2 = store.create_request(body={"target": "market", "preset": "overview"}, session_id="s1")
    assert r2["deduplicated"] is True and r2["request_id"] == r1["request_id"]


def test_rate_limits_session_active_2(tmp_path):
    store = _mk_store(tmp_path)
    store.create_request(body={"target": "stock", "preset": "quote_only",
                               "symbols": ["600519.SH"]}, session_id="s1")
    store.create_request(body={"target": "stock", "preset": "basic",
                               "symbols": ["600519.SH"]}, session_id="s1")
    with pytest.raises(RefreshError) as exc:
        store.create_request(body={"target": "market", "preset": "overview"}, session_id="s1")
    assert exc.value.code == "refresh_rate_limited" and exc.value.status_code == 429


def test_rate_limits_per_minute_5(tmp_path, monkeypatch):
    import app.westock_refresh_service as svc
    fixed = datetime(2026, 8, 5, 4, 0, tzinfo=timezone.utc)
    clock = {"t": fixed}
    monkeypatch.setattr(svc, "_utc_now", lambda: clock["t"])
    store = _mk_store(tmp_path)
    info = {"fetched_at": fixed.isoformat(), "cache_status": "fresh",
            "data_as_of": "2026-08-05", "content_hash": "a" * 64}

    def _complete_all_and_finish(rid):
        data = store._read_request_file(rid)
        for j in data["jobs"]:
            store.complete_job(rid, j["job_id"], "ok", export_info=info)
        store.finish(rid)

    # 创建 A、B（活跃 2）→ claim+finish 释放
    for preset in ("quote_only", "basic"):
        rid = store.create_request(body={"target": "stock", "preset": preset,
                                         "symbols": ["600519.SH"]}, session_id="s1")["request_id"]
        store.claim(rid, "a" * 64)
        _complete_all_and_finish(rid)
    # 再创建 C、D、E（活跃保持 ≤2；60s 内计数累计 5）
    for preset in ("market_data", "fundamentals", "ownership"):
        rid = store.create_request(body={"target": "stock", "preset": preset,
                                         "symbols": ["600519.SH"]}, session_id="s1")["request_id"]
        store.claim(rid, "a" * 64)
        _complete_all_and_finish(rid)
    # 第 6 个新请求 → 5/min 429
    with pytest.raises(RefreshError) as exc:
        store.create_request(body={"target": "stock", "preset": "funds",
                                   "symbols": ["600519.SH"]}, session_id="s1")
    assert exc.value.code == "refresh_rate_limited" and exc.value.status_code == 429


def test_rate_limits_global_active_10(tmp_path):
    symbols = [f"60051{i}.SH" for i in range(10)] + ["000001.SZ"]  # 11 个唯一
    store = _mk_store(tmp_path, *symbols)
    # 先创建 10 个活跃（9 个其他 session + 1 个本 session）→ 第 11 个 429
    for i, sym in enumerate(symbols[:9]):
        store.create_request(body={"target": "stock", "preset": "quote_only",
                                   "symbols": [sym]}, session_id=f"other-{i}")
    store.create_request(body={"target": "stock", "preset": "quote_only",
                               "symbols": [symbols[9]]}, session_id="s1")
    with pytest.raises(RefreshError) as exc:
        store.create_request(body={"target": "stock", "preset": "quote_only",
                                   "symbols": [symbols[10]]}, session_id="s1")
    assert exc.value.code == "refresh_rate_limited"


def test_session_ownership_isolated(tmp_path):
    store = _mk_store(tmp_path)
    ra = store.create_request(body={"target": "stock", "preset": "basic",
                                    "symbols": ["600519.SH"]}, session_id="sessA")
    store.create_request(body={"target": "market", "preset": "overview"}, session_id="sessB")
    assert store.get_for_session(ra["request_id"], "sessB") is None
    assert store.get_for_session(ra["request_id"], "sessA") is not None
    assert store.cancel_for_session(ra["request_id"], "sessB") is None  # 非所有者 404 语义
    lst = store.list_for_session("sessA")
    assert lst["total"] == 1 and lst["items"][0]["request_id"] == ra["request_id"]
    # worker 内部读取能看到全部（不走 session 视图）
    internal = store.list_internal()
    assert len(internal) == 2


# ---------------------------------------------------------------------- #
# 三、严格状态文件 / 原子写
# ---------------------------------------------------------------------- #
def test_tamper_matrix_fail_open(tmp_path):
    store = _mk_store(tmp_path)
    rid = store.create_request(body={"target": "stock", "preset": "basic",
                                     "symbols": ["600519.SH"]}, session_id="s1")["request_id"]
    path = store._request_path(rid)
    data = json.loads(path.read_text(encoding="utf-8"))
    # 篡改矩阵：额外键 / 非法状态 / naive 时间 / 类型错误 / 错误 request_id
    tamper_cases = [
        {**data, "extra": 1},
        {**data, "status": "bogus"},
        {**data, "created_at": "2026-08-05T00:00:00"},  # naive
        {**data, "created_at": "not-a-date"},
        {**data, "request_id": "0" * 32},
        {**data, "warnings": "not-a-list"},
        {**data, "warnings": ["x" * 500]},
        {**data, "jobs": data["jobs"][:0]},  # 空 jobs
        {**data, "jobs": [{**data["jobs"][0], "status": "bogus"}]},
        {**data, "session_fingerprint": "short"},
    ]
    for i, tampered in enumerate(tamper_cases):
        path.write_text(json.dumps(tampered), encoding="utf-8")
        assert store.get_for_session(rid, "s1") is None, f"tamper case {i} not fail-open"
    # 恢复合法 → 可读
    path.write_text(json.dumps(data), encoding="utf-8")
    assert store.get_for_session(rid, "s1") is not None


def test_atomic_write_unique_temp_and_fsync(tmp_path, monkeypatch):
    store = _mk_store(tmp_path)
    rid = store.create_request(body={"target": "stock", "preset": "basic",
                                     "symbols": ["600519.SH"]}, session_id="s1")["request_id"]
    path = store._request_path(rid)
    original = path.read_bytes()
    # 每次写使用唯一 tmp；成功后无 tmp 残留
    store.cancel_for_session(rid, "s1")
    assert list(path.parent.glob("*.tmp")) == []
    # 写失败（模拟 os.replace 抛错）→ 临时文件清理 + 旧文件逐字节不变
    store2 = _mk_store(tmp_path.parent / "x2")
    rid2 = store2.create_request(body={"target": "market", "preset": "overview"},
                                 session_id="s1")["request_id"]
    p2 = store2._request_path(rid2)
    before = p2.read_bytes()

    def fake_replace(src, dst):
        raise OSError("boom")

    monkeypatch.setattr(os, "replace", fake_replace)
    with pytest.raises(OSError):
        store2.cancel_for_session(rid2, "s1")
    monkeypatch.undo()
    assert p2.read_bytes() == before  # 旧文件逐字节不变
    assert list(p2.parent.glob("*.tmp")) == []  # 失败清理临时文件


def test_size_limits_request_receipt_index(tmp_path):
    store = _mk_store(tmp_path)
    from app.westock_refresh_service import (MAX_RECEIPT_BYTES, MAX_REQUEST_BYTES)
    # request 文件 ≤256 KiB
    rid = store.create_request(body={"target": "stock", "preset": "basic",
                                     "symbols": ["600519.SH"]}, session_id="s1")["request_id"]
    assert store._request_path(rid).stat().st_size <= MAX_REQUEST_BYTES
    # receipt ≤512 KiB
    store.claim(rid, "a" * 64)
    info = {"fetched_at": NOW.isoformat(), "cache_status": "fresh",
            "data_as_of": "2026-08-05", "content_hash": "a" * 64}
    for j in store._read_request_file(rid)["jobs"]:
        store.complete_job(rid, j["job_id"], "ok", export_info=info)
    store.finish(rid)
    assert store._receipt_path(rid).stat().st_size <= MAX_RECEIPT_BYTES
    # index ≤2 MiB
    assert store.index_path.stat().st_size <= 2 * 1024 * 1024


def test_processing_timeout_failed_worker_timeout(tmp_path):
    store = _mk_store(tmp_path)
    rid = store.create_request(body={"target": "market", "preset": "overview"},
                               session_id="s1")["request_id"]
    store.claim(rid, "a" * 64)
    path = store._request_path(rid)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["claimed_at"] = (NOW - timedelta(hours=3)).isoformat()
    path.write_text(json.dumps(data), encoding="utf-8")
    item = store.get_for_session(rid, "s1")
    assert item["status"] == "failed"
    assert item["status_detail"] == "worker_timeout"
    assert any("worker_timeout" in w for w in item["warnings"])


def test_pending_expired_24h(tmp_path):
    store = _mk_store(tmp_path)
    rid = store.create_request(body={"target": "market", "preset": "overview"},
                               session_id="s1")["request_id"]
    path = store._request_path(rid)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["expires_at"] = (NOW - timedelta(hours=25)).isoformat()
    path.write_text(json.dumps(data), encoding="utf-8")
    assert store.get_for_session(rid, "s1")["status"] == "expired"


def test_cancel_only_pending(tmp_path):
    store = _mk_store(tmp_path)
    rid = store.create_request(body={"target": "market", "preset": "overview"},
                               session_id="s1")["request_id"]
    store.claim(rid, "a" * 64)
    with pytest.raises(RefreshError) as exc:
        store.cancel_for_session(rid, "s1")
    assert exc.value.code == "refresh_not_cancellable" and exc.value.status_code == 409


# ---------------------------------------------------------------------- #
# 四、worker jobs / export / receipt
# ---------------------------------------------------------------------- #
def _claim_and_jobs(store, rid):
    claimed = store.claim(rid, "a" * 64)
    assert all(j["status"] == "processing" for j in claimed["jobs"])
    return claimed


def _export_payload(capability, scope, ok=True, extra=None):
    samples = {
        "quote": {"code": "sh600519", "last": 1350.0},
        "profile": {"code": "sh600519", "name": "贵州茅台"},
        "news": [{"title": "测试资讯", "date": "2026-08-05"}],
        "fund_flow": {"code": "sh600519", "main": 1.0},
    }
    payload = {"schema_version": 2, "capability": capability, "scope": scope,
               "ok": ok, "fetched_at": (NOW - timedelta(minutes=1)).isoformat(),
               "as_of": "2026-08-05", "data": samples.get(capability, {"v": 1})}
    if extra:
        payload.update(extra)
    return payload


def test_export_mismatch_rejected(tmp_path):
    store = _mk_store(tmp_path)
    cache = WestockCacheStore(tmp_path / "state" / "dashboard" / "westock")
    rid = store.create_request(body={"target": "stock", "preset": "basic",
                                     "symbols": ["600519.SH"]}, session_id="s1")["request_id"]
    claimed = _claim_and_jobs(store, rid)
    job_quote = next(j for j in claimed["jobs"] if j["capability"] == "quote")
    job_profile = next(j for j in claimed["jobs"] if j["capability"] == "profile")
    # capability 错配：quote 响应作为 profile job
    with pytest.raises(RefreshError) as exc:
        store.export_job(rid, job_profile["job_id"],
                         _export_payload("quote", "600519.SH"), cache)
    assert exc.value.code == "invalid_export"
    # scope 错配
    with pytest.raises(RefreshError):
        store.export_job(rid, job_quote["job_id"],
                         _export_payload("quote", "000001.SZ"), cache)
    # ok=false
    with pytest.raises(RefreshError):
        store.export_job(rid, job_quote["job_id"],
                         _export_payload("quote", "600519.SH", ok=False), cache)
    # 未知顶层键
    with pytest.raises(RefreshError):
        store.export_job(rid, job_quote["job_id"],
                         _export_payload("quote", "600519.SH", extra={"x": 1}), cache)


def test_consumer_validation_failure_preserves_rich_cache(tmp_path):
    store = _mk_store(tmp_path)
    cache = WestockCacheStore(tmp_path / "state" / "dashboard" / "westock")
    rich = {"sh600519": {"code": "sh600519", "date": "2026-08-05",
                           "ma": {"MA_5": 1.0}, "macd": {"DIF": 1.0},
                           "kdj": {"KDJ_K": 1.0}, "rsi": {"RSI_6": 1.0},
                           "boll": {"BOLL_UPPER": 2.0, "BOLL_MID": 1.0,
                                    "BOLL_LOWER": 0.5}}}
    cache.write_export("technical", rich, scope="600519.SH",
                       as_of="2026-08-05", fetched_at=(NOW - timedelta(minutes=1)).isoformat())
    path = cache._path("technical", "600519.SH")
    before = path.read_bytes()

    rid = store.create_request(body={"target": "stock", "capabilities": ["technical"],
                                     "symbols": ["600519.SH"]}, session_id="s1")["request_id"]
    job = _claim_and_jobs(store, rid)["jobs"][0]
    bad = _export_payload("technical", "600519.SH")  # envelope 合法，消费者不可用
    with pytest.raises(RefreshError) as exc:
        store.export_job(rid, job["job_id"], bad, cache)
    assert exc.value.code == "consumer_validation_failed"
    assert path.read_bytes() == before
    assert not list(path.parent.glob("*.tmp"))

    good = _export_payload("technical", "600519.SH")
    good["as_of"] = "2099-12-31"  # worker 提供值不得冒充业务日期
    good["data"] = rich
    info = store.export_job(rid, job["job_id"], good, cache)
    assert info["data_as_of"] == "2026-08-05"
    assert cache.read("technical", "600519.SH")["as_of"] == "2026-08-05"


@pytest.mark.parametrize(("capability", "bad_data"), [
    ("financials", {"summary": {"eps": 1.0}}),
    ("northbound", {"cur": {"code": "sh600519"}}),
    ("technical", {"sh600519": {"code": "sh600519", "date": "2026-08-05"}}),
])
def test_consumer_validator_rejects_correct_hash_but_unusable_payload(
        tmp_path, capability, bad_data):
    store = _mk_store(tmp_path)
    cache = WestockCacheStore(tmp_path / "state" / "dashboard" / "westock")
    rid = store.create_request(body={"target": "stock", "capabilities": [capability],
                                     "symbols": ["600519.SH"]}, session_id="s1")["request_id"]
    job = _claim_and_jobs(store, rid)["jobs"][0]
    payload = _export_payload(capability, "600519.SH")
    payload["data"] = bad_data
    with pytest.raises(RefreshError) as exc:
        store.export_job(rid, job["job_id"], payload, cache)
    assert exc.value.code == "consumer_validation_failed"
    assert cache.read(capability, "600519.SH") is None


def test_export_content_hash_and_receipt_fields(tmp_path):
    store = _mk_store(tmp_path)
    cache = WestockCacheStore(tmp_path / "state" / "dashboard" / "westock")
    rid = store.create_request(body={"target": "stock", "preset": "basic",
                                     "symbols": ["600519.SH"]}, session_id="s1")["request_id"]
    claimed = _claim_and_jobs(store, rid)
    for job in claimed["jobs"]:
        info = store.export_job(rid, job["job_id"],
                                _export_payload(job["capability"], job["scope"]), cache)
        assert set(info) == {"fetched_at", "cache_status", "data_as_of", "content_hash"}
        assert len(info["content_hash"]) == 64
        store.complete_job(rid, job["job_id"], "ok", export_info=info)
    f = store.finish(rid)
    assert f["status"] == "completed"
    receipt = json.loads((store.receipts_dir / f"{rid}.json").read_text(encoding="utf-8"))
    assert receipt["started_at"] is not None and receipt["finished_at"] is not None
    for j in receipt["jobs"]:
        assert set(j) == {"job_id", "capability", "scope", "status", "fetched_at",
                          "cache_status", "data_as_of", "content_hash", "warning"}
        assert j["cache_status"] and j["content_hash"]
    raw = json.dumps(receipt)
    for bad in ("tool", "token", "password", "C:"):
        assert bad not in raw


def test_repeat_export_complete_idempotent_conflict(tmp_path):
    store = _mk_store(tmp_path)
    cache = WestockCacheStore(tmp_path / "state" / "dashboard" / "westock")
    rid = store.create_request(body={"target": "stock", "preset": "basic",
                                     "symbols": ["600519.SH"]}, session_id="s1")["request_id"]
    claimed = _claim_and_jobs(store, rid)
    job_q = next(j for j in claimed["jobs"] if j["capability"] == "quote")
    info = store.export_job(rid, job_q["job_id"], _export_payload("quote", "600519.SH"), cache)
    store.complete_job(rid, job_q["job_id"], "ok", export_info=info)
    # 幂等：同结果同内容
    store.complete_job(rid, job_q["job_id"], "ok", export_info=info)
    # 冲突：不同结果（failed 带合法 warning）
    with pytest.raises(RefreshError) as exc:
        store.complete_job(rid, job_q["job_id"], "failed", warning="上游无数据")
    assert exc.value.code == "request_conflict"
    # failed 不带 warning → invalid_warning
    with pytest.raises(RefreshError) as exc2:
        store.complete_job(rid, job_q["job_id"], "failed")
    assert exc2.value.code == "invalid_warning"


# ---------------------------------------------------------------------- #
# 五、coverage
# ---------------------------------------------------------------------- #
def _write_cache(cache_root: Path, capability: str, scope: str, *, fetched_at: str):
    path = cache_root / capability / f"{scope}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema_version": 1, "capability": capability, "tool": f"data_{capability}",
        "scope": scope, "source": "westock-mcp", "transport": "cache_export",
        "as_of": "2026-08-04", "fetched_at": fetched_at, "cached_at": fetched_at,
        "data": {}, "warnings": [],
    }), encoding="utf-8")


def test_coverage_per_symbol_and_missing_unavailable(tmp_path):
    from app.westock_refresh_service import build_coverage_scanner
    store = _mk_store(tmp_path, "600519.SH", "000001.SZ")
    cache_root = tmp_path / "state" / "dashboard" / "westock"
    cache = WestockCacheStore(cache_root)
    _write_cache(cache_root, "quote", "600519.SH",
                 fetched_at=(NOW - timedelta(seconds=10)).isoformat())
    scanner = build_coverage_scanner(tmp_path, cache)
    result = scanner.scan()
    assert result["stock_local_history"]["600519.SH"] is True
    assert result["stock_matrix"]["600519.SH"]["quote"] == "fresh"
    # 缺失能力 → unavailable
    assert result["stock_matrix"]["600519.SH"]["profile"] == "unavailable"
    # 000001.SZ 全部 unavailable（无缓存）
    assert result["stock_matrix"]["000001.SZ"]["quote"] == "unavailable"
    # 每股票独立 local_history
    assert result["stock_local_history"]["000001.SZ"] is True


def test_coverage_query_scope_strict_and_filters(tmp_path):
    from app.westock_refresh_service import build_coverage_scanner
    store = _mk_store(tmp_path)
    cache_root = tmp_path / "state" / "dashboard" / "westock"
    cache = WestockCacheStore(cache_root)
    _write_cache(cache_root, "filter", "q_" + "a" * 64, fetched_at=NOW.isoformat())
    # 非法 query scope 文件（非 q_64hex）被忽略
    _write_cache(cache_root, "filter", "query-bogus", fetched_at=NOW.isoformat())
    scanner = build_coverage_scanner(tmp_path, cache)
    result = scanner.scan()
    assert result["query_scope_counts"].get("filter") == 1  # 只有合法 q_ 计入
    with pytest.raises(RefreshError):
        scanner.scan({"bogus": "1"})  # 未知参数拒绝
    with pytest.raises(RefreshError):
        scanner.scan({"capability": "not-a-cap"})
    with pytest.raises(RefreshError):
        scanner.scan({"scope": "bogus"})


# ---------------------------------------------------------------------- #
# 六、API
# ---------------------------------------------------------------------- #
@pytest.fixture()
def f3_client(tmp_path, config_factory, fake_executor):
    from fastapi.testclient import TestClient
    app = create_app(config_factory(project_root=str(tmp_path)),
                     enable_static=False, executor=fake_executor)
    with TestClient(app, base_url="https://127.0.0.1") as c:
        resp = c.post("/api/auth/login", json={"username": "admin", "password": "secret123"})
        assert resp.status_code == 200, resp.text
        yield c


def csrf_headers(client) -> dict[str, str]:
    csrf = client.cookies.get(CSRF_COOKIE)
    return {"X-CSRF-Token": csrf} if csrf else {}


def _local_fixture(tmp_path):
    curated = tmp_path / "data" / "curated"
    curated.mkdir(parents=True, exist_ok=True)
    (curated / "daily_quotes_600519.SH_x.parquet").write_bytes(b"x")


def test_api_create_list_pagination_and_unknown_params(f3_client, tmp_path):
    _local_fixture(tmp_path)
    h = csrf_headers(f3_client)
    created = []
    for preset in ("quote_only", "basic"):
        resp = f3_client.post("/api/connections/westock/refresh-requests",
                              json={"target": "stock", "preset": preset,
                                    "symbols": ["600519.SH"]}, headers=h)
        assert resp.status_code == 200, resp.text
        created.append(resp.json()["request_id"])
    # 分页：limit/offset/total
    page = f3_client.get("/api/connections/westock/refresh-requests?limit=1&offset=0").json()
    assert page["total"] == 2 and len(page["items"]) == 1
    page2 = f3_client.get("/api/connections/westock/refresh-requests?limit=1&offset=1").json()
    assert len(page2["items"]) == 1
    # 未知 query 参数拒绝
    resp = f3_client.get("/api/connections/westock/refresh-requests?bogus=1")
    assert resp.status_code == 400
    # limit 越界
    resp = f3_client.get("/api/connections/westock/refresh-requests?limit=0")
    assert resp.status_code == 400
    resp = f3_client.get("/api/connections/westock/refresh-requests?limit=51")
    assert resp.status_code == 400


def test_api_ownership_404(f3_client, tmp_path):
    _local_fixture(tmp_path)
    h = csrf_headers(f3_client)
    resp = f3_client.post("/api/connections/westock/refresh-requests",
                          json={"target": "stock", "preset": "basic",
                                "symbols": ["600519.SH"]}, headers=h)
    rid = resp.json()["request_id"]
    # 另一 session 登录（同一 app 实例）不能查看/取消 → 404（避免枚举）
    from fastapi.testclient import TestClient
    other = TestClient(f3_client.app, base_url="https://127.0.0.1")
    assert other.post("/api/auth/login", json={"username": "admin",
                                               "password": "secret123"}).status_code == 200
    assert other.get(f"/api/connections/westock/refresh-requests/{rid}").status_code == 404
    assert other.delete(f"/api/connections/westock/refresh-requests/{rid}",
                        headers={"X-CSRF-Token": other.cookies.get(CSRF_COOKIE)}).status_code == 404
    # 未认证 → 401
    assert TestClient(f3_client.app, base_url="https://127.0.0.1").get(
        f"/api/connections/westock/refresh-requests/{rid}").status_code == 401


def test_api_cancel_conflict_and_rate_code(f3_client, tmp_path):
    _local_fixture(tmp_path)
    h = csrf_headers(f3_client)
    resp = f3_client.post("/api/connections/westock/refresh-requests",
                          json={"target": "stock", "preset": "basic",
                                "symbols": ["600519.SH"]}, headers=h)
    rid = resp.json()["request_id"]
    # 取消（pending）OK
    assert f3_client.delete(f"/api/connections/westock/refresh-requests/{rid}",
                            headers=h).status_code == 200
    # 再取消 → 409 refresh_not_cancellable
    resp = f3_client.delete(f"/api/connections/westock/refresh-requests/{rid}", headers=h)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "refresh_not_cancellable"


def test_api_body_whitelist_and_rate_limited(f3_client, tmp_path):
    _local_fixture(tmp_path)
    h = csrf_headers(f3_client)
    # 未知顶层键
    resp = f3_client.post("/api/connections/westock/refresh-requests",
                          json={"target": "stock", "preset": "basic",
                                "symbols": ["600519.SH"], "hacked": 1}, headers=h)
    assert resp.status_code == 400
    # 未知 target
    resp = f3_client.post("/api/connections/westock/refresh-requests",
                          json={"target": "bad", "preset": "basic"}, headers=h)
    assert resp.status_code == 400
    # 429 限流（session 活跃上限触发 refresh_rate_limited）
    for preset in ("quote_only", "basic"):
        r = f3_client.post("/api/connections/westock/refresh-requests",
                           json={"target": "stock", "preset": preset,
                                 "symbols": ["600519.SH"]}, headers=h)
        assert r.status_code == 200
    resp = f3_client.post("/api/connections/westock/refresh-requests",
                          json={"target": "stock", "preset": "funds",
                                "symbols": ["600519.SH"]}, headers=h)
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "refresh_rate_limited"


def test_api_compatible_refresh_same_model(f3_client, tmp_path):
    _local_fixture(tmp_path)
    h = csrf_headers(f3_client)
    resp = f3_client.post("/api/connections/westock/refresh",
                          json={"target": "stock", "preset": "basic",
                                "symbols": ["600519.SH"]}, headers=h)
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] is True and body["transport"] == "cache_export"
    assert body["status"] == "pending" and len(body["jobs"]) == 4
    # 旧式 capabilities 数组 → invalid_refresh_request
    resp = f3_client.post("/api/connections/westock/refresh",
                          json={"capabilities": ["quote"]}, headers=h)
    assert resp.status_code == 400


def test_api_coverage_unknown_param_rejected(f3_client, tmp_path):
    _local_fixture(tmp_path)
    resp = f3_client.get("/api/connections/westock/coverage?bogus=1")
    assert resp.status_code == 400
    ok = f3_client.get("/api/connections/westock/coverage").json()
    assert ok["capability_total"] >= 1
    assert "stock_local_history" in ok


def test_api_list_is_session_scoped(f3_client, tmp_path):
    _local_fixture(tmp_path)
    h = csrf_headers(f3_client)
    f3_client.post("/api/connections/westock/refresh-requests",
                   json={"target": "stock", "preset": "basic", "symbols": ["600519.SH"]},
                   headers=h)
    items = f3_client.get("/api/connections/westock/refresh-requests").json()
    assert items["total"] == 1  # 当前 session 只能看到自己的请求


# ---------------------------------------------------------------------- #
# CLI（jobs 模式）测试：subprocess 隔离运行 scripts/westock_refresh_request.py
# ---------------------------------------------------------------------- #
import subprocess as _subprocess
import sys as _sys
from datetime import datetime, timedelta, timezone as _tz

CLI = Path(__file__).resolve().parents[3] / "scripts" / "westock_refresh_request.py"


def _run_cli(tmp_path: Path, *args: str, expect: int = 0) -> str:
    proc = _subprocess.run([_sys.executable, str(CLI), *args],
                           cwd=str(tmp_path), capture_output=True, text=True, timeout=60)
    assert proc.returncode == expect, f"exit={proc.returncode}\n{proc.stdout}\n{proc.stderr}"
    return proc.stdout


def _seed(tmp_path: Path):
    store = _mk_store(tmp_path)
    rid = store.create_request(body={"target": "stock", "preset": "basic",
                                     "symbols": ["600519.SH"]}, session_id="cli")["request_id"]
    return store, rid


def test_cli_claim_lists_jobs_and_export_flow(tmp_path):
    store, rid = _seed(tmp_path)
    out = _run_cli(tmp_path, "list")
    assert rid in out
    out = _run_cli(tmp_path, "claim", rid)
    assert "claimed" in out and "jobs=" in out
    for cap in ("quote", "profile", "news", "fund_flow"):
        assert cap in out  # claim 输出每个 job 的 capability
    claimed = store.get_for_session(rid, "cli")
    job_ids = {j["job_id"]: j for j in claimed["jobs"]}
    # export 需要受控导出元数据
    job = job_ids["quote" if "quote" in job_ids else list(job_ids)[0]]
    job_id = next(jid for jid, j in job_ids.items() if j["capability"] == "quote")
    raw = tmp_path / "quote.json"
    raw.write_text(json.dumps({
        "schema_version": 2, "capability": "quote", "scope": "600519.SH",
        "ok": True,
        "fetched_at": (datetime.now(_tz.utc) - timedelta(minutes=1)).isoformat(),
        "as_of": "2026-08-05", "data": {"code": "sh600519", "last": 1},
    }), encoding="utf-8")
    out = _run_cli(tmp_path, "export", rid, "--job", job_id, "--input", str(raw))
    assert "content_hash" in out
    summary = tmp_path / "quote.json.summary.json"
    assert summary.exists()
    _run_cli(tmp_path, "complete-job", rid, "--job", job_id, "--result", "ok",
             "--export-info", str(summary))
    # 重复 export 不同内容 → 冲突（幂等要求一致）
    raw2 = tmp_path / "quote2.json"
    raw2.write_text(json.dumps({
        "schema_version": 2, "capability": "quote", "scope": "600519.SH",
        "ok": True,
        "fetched_at": (datetime.now(_tz.utc) - timedelta(minutes=1)).isoformat(),
        "as_of": "2026-08-05", "data": {"code": "sh600519", "last": 999},
    }), encoding="utf-8")
    proc = _subprocess.run([_sys.executable, str(CLI), "export", rid, "--job", job_id,
                            "--input", str(raw2)], cwd=str(tmp_path),
                           capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0  # 导出可重复（写缓存），冲突发生在 complete-job


def test_cli_complete_conflict_and_partial(tmp_path):
    store, rid = _seed(tmp_path)
    _run_cli(tmp_path, "claim", rid)
    claimed = store.get_for_session(rid, "cli")
    quote_job = next(j["job_id"] for j in claimed["jobs"] if j["capability"] == "quote")
    minute = next((j["job_id"] for j in claimed["jobs"] if j["capability"] == "news"), quote_job)
    # quote：先 export 写缓存 → 生成受控摘要 → complete ok（带导出证据）
    raw = tmp_path / "quote_partial.json"
    raw.write_text(json.dumps({
        "schema_version": 2, "capability": "quote", "scope": "600519.SH",
        "ok": True,
        "fetched_at": (datetime.now(_tz.utc) - timedelta(minutes=1)).isoformat(),
        "as_of": "2026-08-05", "data": {"code": "sh600519", "last": 1},
    }), encoding="utf-8")
    _run_cli(tmp_path, "export", rid, "--job", quote_job, "--input", str(raw))
    summary = tmp_path / "quote_partial.json.summary.json"
    assert summary.exists()
    _run_cli(tmp_path, "complete-job", rid, "--job", quote_job, "--result", "ok",
             "--export-info", str(summary))
    _run_cli(tmp_path, "complete-job", rid, "--job", minute, "--result", "failed",
             "--warning", "上游无数据")
    # failed 无 warning → CLI 拒绝
    proc = _subprocess.run([_sys.executable, str(CLI), "complete-job", rid,
                            "--job", minute, "--result", "failed"],
                           cwd=str(tmp_path), capture_output=True, text=True, timeout=60)
    assert proc.returncode == 1 and "warning" in proc.stderr.lower()
    # 其余 job 标 failed（带 warning）→ finish 得 partial
    claimed2 = store.get_for_session(rid, "cli")
    for j in claimed2["jobs"]:
        if j["status"] not in ("ok", "failed"):
            _run_cli(tmp_path, "complete-job", rid, "--job", j["job_id"],
                     "--result", "failed", "--warning", "未导出")
    out = _run_cli(tmp_path, "finish", rid)
    assert "partial" in out
    item = store.get_for_session(rid, "cli")
    assert item["status"] == "partial"


def test_cli_export_mismatch_rejected(tmp_path):
    store, rid = _seed(tmp_path)
    _run_cli(tmp_path, "claim", rid)
    claimed = store.get_for_session(rid, "cli")
    quote_job = next(j["job_id"] for j in claimed["jobs"] if j["capability"] == "quote")
    # capability 错配（profile 元数据给 quote job）
    raw = tmp_path / "bad.json"
    raw.write_text(json.dumps({
        "schema_version": 2, "capability": "profile", "scope": "600519.SH",
        "ok": True,
        "fetched_at": (datetime.now(_tz.utc) - timedelta(minutes=1)).isoformat(),
        "as_of": "2026-08-05", "data": {},
    }), encoding="utf-8")
    proc = _subprocess.run([_sys.executable, str(CLI), "export", rid, "--job", quote_job,
                            "--input", str(raw)], cwd=str(tmp_path),
                           capture_output=True, text=True, timeout=60)
    assert proc.returncode == 1 and "capability 与 job 不一致" in proc.stderr


def test_cli_help_honest():
    proc = _subprocess.run([_sys.executable, str(CLI), "--help"],
                           capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0
    assert "不调用 MCP" in proc.stdout and "不自动刷新" in proc.stdout


# ---------------------------------------------------------------------- #
# F3R3 审核缺口探针：session 去重 / summary 边界 / export 证据 / finish / coverage / receipt 事务
# ---------------------------------------------------------------------- #
import threading


def _complete_all(store, rid, info=None, failed_with_warning=False):
    """完成全部 job（ok，带 export_info；或 failed+warning）。"""
    data = store._read_request_file(rid)
    for j in data["jobs"]:
        if failed_with_warning:
            store.complete_job(rid, j["job_id"], "failed", warning="未导出")
        else:
            store.complete_job(rid, j["job_id"], "ok", export_info=info)


# ---- 1. cross-session duplicate isolation ----
def test_cross_session_duplicate_not_deduped(tmp_path):
    store = _mk_store(tmp_path)
    body = {"target": "stock", "preset": "basic", "symbols": ["600519.SH"]}
    a = store.create_request(body=body, session_id="sessA")
    b = store.create_request(body=body, session_id="sessB")
    assert a["request_id"] != b["request_id"]  # 不得返回 A 的 request_id
    assert b.get("deduplicated") is None
    # A 无法看到 B 的请求
    assert store.get_for_session(a["request_id"], "sessB") is None
    assert store.get_for_session(b["request_id"], "sessA") is None


# ---- 2. same-session 5-thread dedup（barrier）----
def test_same_session_five_thread_dedup(tmp_path):
    store = _mk_store(tmp_path)
    barrier = threading.Barrier(5)
    results: list[dict] = []

    def worker(i):
        barrier.wait()
        results.append(store.create_request(
            body={"target": "market", "preset": "overview"}, session_id="sessC"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    ids = {r["request_id"] for r in results}
    assert len(ids) == 1, ids  # 同 session 同请求并发 → 唯一 request_id
    # 活跃数不超限（2）
    active = [r for r in results if r["status"] in ("pending", "processing")]
    assert len(active) >= 1


def test_cross_session_parallel_distinct_ids(tmp_path):
    store = _mk_store(tmp_path)
    barrier = threading.Barrier(4)
    results: list[dict] = []

    def worker(i):
        barrier.wait()
        results.append(store.create_request(
            body={"target": "stock", "preset": "quote_only", "symbols": ["600519.SH"]},
            session_id=f"sess-{i}"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert len({r["request_id"] for r in results}) == 4  # 各自 request_id


# ---- 3. summary-only 硬边界 ----
def test_summary_only_market_data_keeps_only_quote(tmp_path):
    store = _mk_store(tmp_path, "600519.SH")
    req = store.create_request(body={"target": "stock", "preset": "market_data",
                                     "symbols": ["300750.SZ"], "allow_summary_only": True},
                               session_id="s1")
    caps = {j["capability"] for j in req["jobs"]}
    assert caps == {"quote"}  # market_data=quote/minute/technical → 非本地仅剩 quote
    assert any("summary-only" in w for w in req["warnings"])
    # full_research 不含 minute/technical
    req2 = store.create_request(body={"target": "stock", "preset": "full_research",
                                      "symbols": ["300750.SZ"], "allow_summary_only": True},
                                session_id="s2")
    caps2 = {j["capability"] for j in req2["jobs"]}
    assert "minute" not in caps2 and "technical" not in caps2
    assert "lhb" in caps2  # 摘要能力保留
    lhb = [j for j in req2["jobs"] if j["capability"] == "lhb"]
    assert len(req2["jobs"]) == 18
    assert len(lhb) == 1 and lhb[0]["scope"] == "global"
    assert lhb[0]["summary_only"] is False
    assert store._read_request_file(req2["request_id"]) is not None
    claimed = store.claim(req2["request_id"], "b" * 64)
    assert claimed is not None and len(claimed["jobs"]) == 18
    # 本地股票仍含 minute/technical
    req3 = store.create_request(body={"target": "stock", "preset": "market_data",
                                      "symbols": ["600519.SH"]}, session_id="s3")
    caps3 = {j["capability"] for j in req3["jobs"]}
    assert {"quote", "minute", "technical"} <= caps3


def test_summary_only_mixed_symbols_jobs(tmp_path):
    store = _mk_store(tmp_path, "600519.SH")
    req = store.create_request(body={"target": "stock", "preset": "full_research",
                                     "symbols": ["600519.SH", "300750.SZ"],
                                     "allow_summary_only": True}, session_id="s1")
    local_jobs = [j for j in req["jobs"] if j["scope"] == "600519.SH"]
    summary_jobs = [j for j in req["jobs"] if j["scope"] == "300750.SZ"]
    assert any(j["capability"] == "minute" for j in local_jobs)
    assert not any(j["capability"] in ("minute", "technical") for j in summary_jobs)
    assert all(j["summary_only"] for j in summary_jobs)
    assert not any(j["summary_only"] for j in local_jobs)
    lhb = [j for j in req["jobs"] if j["capability"] == "lhb"]
    assert len(lhb) == 1 and lhb[0]["scope"] == "global"
    assert lhb[0]["summary_only"] is False


def test_summary_only_all_blocked_capabilities_rejected(tmp_path):
    store = _mk_store(tmp_path, "600519.SH")
    with pytest.raises(RefreshError) as exc:
        store.create_request(body={"target": "stock",
                                   "capabilities": ["minute", "technical"],
                                   "symbols": ["300750.SZ"], "allow_summary_only": True},
                             session_id="s1")
    assert exc.value.code == "empty_jobs"


# ---- 4/5/6. export 证据绑定 ----
def test_ok_without_export_info_rejected(tmp_path):
    store = _mk_store(tmp_path)
    cache = WestockCacheStore(tmp_path / "state" / "dashboard" / "westock")
    rid = store.create_request(body={"target": "market", "preset": "overview"},
                               session_id="s1")["request_id"]
    claimed = _claim_and_jobs(store, rid)
    job = claimed["jobs"][0]
    with pytest.raises(RefreshError) as exc:
        store.complete_job(rid, job["job_id"], "ok")
    assert exc.value.code == "invalid_export_info"


def test_partial_without_export_info_rejected(tmp_path):
    store = _mk_store(tmp_path)
    rid = store.create_request(body={"target": "market", "preset": "overview"},
                               session_id="s1")["request_id"]
    claimed = _claim_and_jobs(store, rid)
    job = claimed["jobs"][0]
    with pytest.raises(RefreshError) as exc:
        store.complete_job(rid, job["job_id"], "partial", warning="部分缺失")
    assert exc.value.code == "invalid_export_info"


def test_failed_without_warning_rejected(tmp_path):
    store = _mk_store(tmp_path)
    rid = store.create_request(body={"target": "market", "preset": "overview"},
                               session_id="s1")["request_id"]
    claimed = _claim_and_jobs(store, rid)
    job = claimed["jobs"][0]
    with pytest.raises(RefreshError) as exc:
        store.complete_job(rid, job["job_id"], "failed")
    assert exc.value.code == "invalid_warning"
    # failed 携带 export_info → 拒绝
    with pytest.raises(RefreshError) as exc2:
        store.complete_job(rid, job["job_id"], "failed", warning="w",
                           export_info={"fetched_at": NOW.isoformat(),
                                        "cache_status": "fresh",
                                        "data_as_of": "2026-08-05",
                                        "content_hash": "a" * 64})
    assert exc2.value.code == "invalid_export_info"


def test_export_info_extra_key_rejected(tmp_path):
    store = _mk_store(tmp_path)
    cache = WestockCacheStore(tmp_path / "state" / "dashboard" / "westock")
    rid = store.create_request(body={"target": "market", "preset": "overview"},
                               session_id="s1")["request_id"]
    claimed = _claim_and_jobs(store, rid)
    job = claimed["jobs"][0]
    info = {"fetched_at": NOW.isoformat(), "cache_status": "fresh",
            "data_as_of": "2026-08-05", "content_hash": "a" * 64, "extra": 1}
    with pytest.raises(RefreshError) as exc:
        store.complete_job(rid, job["job_id"], "ok", export_info=info)
    assert exc.value.code == "invalid_export_info"


def test_complete_job_verifies_cached_hash(tmp_path):
    store = _mk_store(tmp_path)
    cache = WestockCacheStore(tmp_path / "state" / "dashboard" / "westock")
    rid = store.create_request(body={"target": "stock", "preset": "basic",
                                     "symbols": ["600519.SH"]}, session_id="s1")["request_id"]
    claimed = _claim_and_jobs(store, rid)
    job_q = next(j for j in claimed["jobs"] if j["capability"] == "quote")
    info = store.export_job(rid, job_q["job_id"], _export_payload("quote", "600519.SH"), cache)
    # 手写错误 hash（与缓存不符）→ 拒绝
    forged = {**info, "content_hash": "b" * 64}
    with pytest.raises(RefreshError) as exc:
        store.complete_job(rid, job_q["job_id"], "ok", export_info=forged, cache_store=cache)
    assert exc.value.code == "export_verification_failed"
    # 真实摘要 → OK
    store.complete_job(rid, job_q["job_id"], "ok", export_info=info, cache_store=cache)


# ---- 7/8/9/10. finish 状态机 ----
def test_finish_with_processing_jobs_rejected(tmp_path):
    store = _mk_store(tmp_path)
    rid = store.create_request(body={"target": "market", "preset": "overview"},
                               session_id="s1")["request_id"]
    _claim_and_jobs(store, rid)
    with pytest.raises(RefreshError) as exc:
        store.finish(rid)
    assert exc.value.code == "jobs_incomplete" and exc.value.status_code == 409
    # request 不变（仍 processing）
    assert store.get_for_session(rid, "s1")["status"] == "processing"


def test_finish_all_partial_aggregates_partial(tmp_path):
    store = _mk_store(tmp_path)
    rid = store.create_request(body={"target": "market", "preset": "overview"},
                               session_id="s1")["request_id"]
    claimed = _claim_and_jobs(store, rid)
    for job in claimed["jobs"]:
        store.complete_job(rid, job["job_id"], "partial", warning="部分缺失",
                           export_info={"fetched_at": NOW.isoformat(),
                                        "cache_status": "stale",
                                        "data_as_of": "2026-08-05",
                                        "content_hash": "a" * 64})
    assert store.finish(rid)["status"] == "partial"


def test_finish_ok_plus_failed_aggregates_partial(tmp_path):
    store = _mk_store(tmp_path)
    rid = store.create_request(body={"target": "stock", "preset": "basic",
                                     "symbols": ["600519.SH"]}, session_id="s1")["request_id"]
    claimed = _claim_and_jobs(store, rid)
    info = {"fetched_at": NOW.isoformat(), "cache_status": "fresh",
            "data_as_of": "2026-08-05", "content_hash": "a" * 64}
    for i, job in enumerate(claimed["jobs"]):
        if i == 0:
            store.complete_job(rid, job["job_id"], "ok", export_info=info)
        else:
            store.complete_job(rid, job["job_id"], "failed", warning="上游无数据")
    f = store.finish(rid)
    assert f["status"] == "partial"
    assert "1/4" in f["status_detail"]


def test_final_receipt_has_no_processing_jobs(tmp_path):
    store = _mk_store(tmp_path)
    rid = store.create_request(body={"target": "market", "preset": "overview"},
                               session_id="s1")["request_id"]
    claimed = _claim_and_jobs(store, rid)
    for job in claimed["jobs"]:
        store.complete_job(rid, job["job_id"], "failed", warning="全部失败")
    store.finish(rid)
    receipt = json.loads((store.receipts_dir / f"{rid}.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "failed"
    assert all(j["status"] not in ("pending", "processing") for j in receipt["jobs"])


# ---- 11/12. coverage 计数口径 ----
def test_coverage_empty_matrix_counts(tmp_path):
    from app.westock_refresh_service import build_coverage_scanner, _STOCK_CAPS
    store = _mk_store(tmp_path, "600519.SH")  # 1 股票，无缓存
    cache = WestockCacheStore(tmp_path / "state" / "dashboard" / "westock")
    scanner = build_coverage_scanner(tmp_path, cache)
    result = scanner.scan()
    # 无缓存 + 1 股票 + 20 stock 能力 → unavailable 至少 20 项矩阵
    assert result["unavailable_count"] >= len(_STOCK_CAPS)
    cells = [v for row in result["stock_matrix"].values() for v in row.values()]
    cells += list(result["global_capabilities"].values())
    assert result["fresh_count"] + result["stale_count"] + result["unavailable_count"] == len(cells)
    assert result["fresh_count"] == 0 and result["stale_count"] == 0


def test_coverage_filters_counts_and_no_fresh_unavailable(tmp_path):
    from app.westock_refresh_service import build_coverage_scanner
    store = _mk_store(tmp_path, "600519.SH")
    cache_root = tmp_path / "state" / "dashboard" / "westock"
    cache = WestockCacheStore(cache_root)
    _write_cache(cache_root, "quote", "600519.SH",
                 fetched_at=(NOW - timedelta(seconds=10)).isoformat())
    scanner = build_coverage_scanner(tmp_path, cache)
    # status=fresh：只含 fresh，不补 unavailable
    fresh = scanner.scan({"status": "fresh"})
    assert all(v == "fresh" for row in fresh["stock_matrix"].values()
               for v in row.values())
    assert fresh["unavailable_count"] == 0
    # capability=quote：矩阵只含 quote
    q = scanner.scan({"capability": "quote"})
    assert all(set(row) == {"quote"} for row in q["stock_matrix"].values())
    # 计数一致
    assert (q["fresh_count"] + q["stale_count"] + q["unavailable_count"]
            == sum(len(r) for r in q["stock_matrix"].values())
            + len(q["global_capabilities"]))


# ---- 13. cross-field tamper 矩阵 ----
def test_cross_field_tamper_matrix(tmp_path):
    store = _mk_store(tmp_path)
    rid = store.create_request(body={"target": "stock", "preset": "basic",
                                     "symbols": ["600519.SH"]}, session_id="s1")["request_id"]
    path = store._request_path(rid)
    data = json.loads(path.read_text(encoding="utf-8"))
    from app.westock_refresh_service import canonical_request_hash
    cases = [
        # summary_only 非子集
        {**data, "target": {**data["target"], "summary_only_symbols": ["000001.SZ"]}},
        # request_hash 篡改
        {**data, "request_hash": "f" * 64},
        # LHB 非 global（构造 lhb job scope=600519.SH）
        {**data, "jobs": [{**data["jobs"][0], "capability": "lhb", "scope": "600519.SH"}]},
        # jobs 与 target 不一致（多余 job）
        {**data, "jobs": data["jobs"] + [dict(data["jobs"][0], job_id="d" * 32)]},
        # processing 无 claimed_at（先改 status 一致性）
        {**data, "status": "processing", "claimed_at": None, "started_at": None},
        # pending 却有 worker_id
        {**data, "worker_id": "a" * 64},
        # ok job 无导出证据
        {**data, "jobs": [{**data["jobs"][0], "status": "ok"}]},
        # worker_id 非 hex
        {**data, "status": "processing", "claimed_at": NOW.isoformat(),
         "started_at": NOW.isoformat(), "worker_id": "z" * 64},
        # data_as_of 非严格日期
        {**data, "jobs": [{**data["jobs"][0], "status": "ok", "fetched_at": NOW.isoformat(),
                           "cache_status": "fresh", "content_hash": "a" * 64,
                           "data_as_of": "2026/08/05"}]},
    ]
    for i, tampered in enumerate(cases):
        path.write_text(json.dumps(tampered), encoding="utf-8")
        assert store.get_for_session(rid, "s1") is None, f"tamper case {i} not fail-open"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert store.get_for_session(rid, "s1") is not None


# ---- 14. receipt 写入失败事务 ----
def test_receipt_write_failure_keeps_request_processing(tmp_path, monkeypatch):
    store = _mk_store(tmp_path)
    rid = store.create_request(body={"target": "market", "preset": "overview"},
                               session_id="s1")["request_id"]
    claimed = _claim_and_jobs(store, rid)
    for job in claimed["jobs"]:
        store.complete_job(rid, job["job_id"], "failed", warning="全部失败")
    request_before = (store._request_path(rid)).read_bytes()
    # 模拟 receipt os.replace 失败（全局替换；仅 receipt 抛错）
    _orig_replace = os.replace

    def fake_replace(src, dst):
        if "receipts" in str(dst):
            raise OSError("receipt write boom")
        return _orig_replace(src, dst)

    monkeypatch.setattr(os, "replace", fake_replace)
    with pytest.raises(RefreshError) as exc:
        store.finish(rid)
    monkeypatch.undo()
    assert exc.value.code == "receipt_write_failed"
    # request 未进入终态（无 completed）；无 receipt 文件
    item = store.get_for_session(rid, "s1")
    assert item["status"] == "processing"
    assert not store._receipt_path(rid).exists()
    assert (store._request_path(rid)).read_bytes() == request_before  # 旧文件逐字节不变


def test_worker_interrupt_resume_without_repeat(tmp_path):
    """真实 CLI/exporter 流程可在中断后续作，已完成缓存不重写。"""
    store = _mk_store(tmp_path)
    rid = store.create_request(body={"target": "stock", "preset": "basic",
                                     "symbols": ["600519.SH"]}, session_id="s1")["request_id"]
    _run_cli(tmp_path, "claim", rid)
    jobs = {j["capability"]: j for j in store._read_request_file(rid)["jobs"]}

    def export_and_complete(capability: str, data: object) -> Path:
        raw = tmp_path / f"{capability}.json"
        raw.write_text(json.dumps({
            "schema_version": 2, "capability": capability, "scope": "600519.SH",
            "ok": True, "fetched_at": (datetime.now(_tz.utc) - timedelta(minutes=1)).isoformat(),
            "as_of": "2026-08-05", "data": data,
        }), encoding="utf-8")
        jid = jobs[capability]["job_id"]
        _run_cli(tmp_path, "export", rid, "--job", jid, "--input", str(raw))
        summary = raw.with_suffix(raw.suffix + ".summary.json")
        _run_cli(tmp_path, "complete-job", rid, "--job", jid, "--result", "ok",
                 "--export-info", str(summary))
        return tmp_path / "state" / "dashboard" / "westock" / capability / "600519.SH.json"

    quote_path = export_and_complete("quote", {"code": "sh600519", "last": 1.0})
    before_hash = hashlib.sha256(quote_path.read_bytes()).hexdigest()
    before_mtime = quote_path.stat().st_mtime_ns

    # 新 worker 只读 list 后直接续作未完成 job；不重新 claim 整个 processing 请求。
    listing = _run_cli(tmp_path, "list", "--status", "processing")
    assert rid in listing
    export_and_complete("profile", {"code": "sh600519", "name": "贵州茅台"})
    for capability in ("news", "fund_flow"):
        jid = jobs[capability]["job_id"]
        _run_cli(tmp_path, "complete-job", rid, "--job", jid, "--result", "failed",
                 "--warning", "上游无数据")

    assert hashlib.sha256(quote_path.read_bytes()).hexdigest() == before_hash
    assert quote_path.stat().st_mtime_ns == before_mtime
    out = _run_cli(tmp_path, "finish", rid)
    assert "partial" in out
    receipt = json.loads(store._receipt_path(rid).read_text(encoding="utf-8"))
    assert all(j["status"] in ("ok", "partial", "failed", "skipped") for j in receipt["jobs"])
