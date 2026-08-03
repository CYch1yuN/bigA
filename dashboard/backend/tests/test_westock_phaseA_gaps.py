"""Phase A 补齐项测试：ok=false / 失败写入 / future timestamp / 损坏缓存 /
schema 校验 / 刷新上限去重 / 龙虎榜注册 / 5MiB 上限。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.main import create_app
from app.westock_bridge import (
    CAPABILITIES,
    CAPABILITY_MAP,
    MAX_REFRESH_CAPABILITIES,
    WestockBridge,
    WestockCacheStore,
)

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "westock_cache_export.py"


def test_lhb_capability_registered_with_15min_ttl():
    """龙虎榜必须单独注册，TTL 15 分钟（900 秒），只读。"""
    lhb = CAPABILITY_MAP.get("lhb")
    assert lhb is not None
    assert lhb.tool == "data_lhb"
    assert lhb.ttl_seconds == 900
    assert lhb.read_only is True


def test_capabilities_have_no_write_operations():
    """注册表不得包含模拟交易/自选股写/提醒写。"""
    tools = {item.tool for item in CAPABILITIES}
    forbidden = {
        "portfolio_paper_trade",
        "portfolio_paper_cancel",
        "portfolio_watchlist_add",
        "portfolio_watchlist_remove",
        "portfolio_group_add",
        "portfolio_group_rename",
        "portfolio_group_sort",
        "portfolio_tips_set",
        "portfolio_watchlist_move",
        "portfolio_watchlist_note",
        "portfolio_watchlist_pin",
        "portfolio_watchlist_unpin",
        "portfolio_watchlist_batch_add",
        "portfolio_watchlist_sort",
    }
    assert not (tools & forbidden)
    assert all(item.read_only for item in CAPABILITIES)


def test_cache_export_rejects_ok_false():
    """ok=false 的 Westock 响应必须拒绝写入缓存（exit 2）。"""
    tmp = Path(tempfile.mkdtemp(prefix="wse_okfalse_"))
    src = tmp / "bad.json"
    src.write_text(json.dumps({"ok": False, "data": {"x": 1}}), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "--capability", "quote", "--input", str(src)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 2
    assert "ok=false" in proc.stderr


def test_cache_export_rejects_oversized_input():
    """输入文件超过 5 MiB 必须拒绝。"""
    tmp = Path(tempfile.mkdtemp(prefix="wse_size_"))
    src = tmp / "big.json"
    src.write_text(json.dumps({"data": {"pad": "x" * (5 * 1024 * 1024 + 1)}}), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT), "--capability", "quote", "--input", str(src)],
        capture_output=True, text=True,
    )
    assert proc.returncode == 2
    assert "MiB 上限" in proc.stderr


def test_failed_write_does_not_destroy_previous_cache(tmp_path: Path):
    store = WestockCacheStore(tmp_path / "westock")
    store.write_export("quote", {"last": 1350.6}, scope="sh600519")
    before = store.read("quote", "sh600519")
    with pytest.raises(ValueError):
        store.write_export("quote", {"bad": 1}, scope="sh600519/../escape")
    after = store.read("quote", "sh600519")
    assert after is not None
    assert after["data"]["last"] == 1350.6
    assert before == after


def test_future_timestamp_is_not_fresh(tmp_path: Path):
    store = WestockCacheStore(tmp_path / "westock")
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    store.write_export("quote", {"last": 1}, fetched_at=future)
    status = WestockBridge(store).connection_status()
    quote = next(item for item in status["data"]["capabilities"] if item["name"] == "quote")
    assert quote["status"] != "fresh"
    assert quote["status"] == "stale"
    assert quote["cache_age_seconds"] is None  # 不表现为"刚刚缓存"
    assert any("未来时间戳" in w for w in status["warnings"])
    assert any("晚于本地时钟" in w for w in status["warnings"])


def test_os_replace_failure_preserves_old_cache(tmp_path: Path, monkeypatch):
    """os.replace 失败：抛异常、旧缓存完全不变、无 .tmp 残留。"""
    store = WestockCacheStore(tmp_path / "westock")
    store.write_export("quote", {"last": 1350.6}, scope="sh600519")
    before_bytes = (tmp_path / "westock" / "quote" / "sh600519.json").read_bytes()

    import app.westock_bridge as bridge_mod

    def boom(src, dst):
        raise OSError("模拟 os.replace 失败")

    monkeypatch.setattr(bridge_mod.os, "replace", boom)
    with pytest.raises(OSError, match="模拟 os.replace 失败"):
        store.write_export("quote", {"last": 999.0}, scope="sh600519")

    after_bytes = (tmp_path / "westock" / "quote" / "sh600519.json").read_bytes()
    assert after_bytes == before_bytes  # 旧缓存逐字节不变
    assert list((tmp_path / "westock" / "quote").glob("*.tmp")) == []  # 无残留临时文件


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e.update({"tool": "data_quote_hacked"}),
        lambda e: e.update({"transport": "direct_mcp"}),
        lambda e: e.update({"source": ""}),
        lambda e: e.update({"fetched_at": "not-a-date"}),
        lambda e: e.update({"cached_at": "garbage"}),
        lambda e: e.pop("data"),
        lambda e: e.update({"warnings": "not-a-list"}),
    ],
)
def test_schema_validation_fails_open(tmp_path: Path, mutate):
    """篡改 envelope 任一字段 → read 返回 None（fail-open），不抛 500。"""
    store = WestockCacheStore(tmp_path / "westock")
    store.write_export("quote", {"last": 1}, scope="sh600519")
    path = tmp_path / "westock" / "quote" / "sh600519.json"
    envelope = json.loads(path.read_text(encoding="utf-8"))
    mutate(envelope)
    path.write_text(json.dumps(envelope), encoding="utf-8")
    assert store.read("quote", "sh600519") is None


def test_corrupt_cache_fails_open(tmp_path: Path, config_factory):
    """缓存损坏 → read None → unavailable；API 不抛 500。"""
    cache_dir = tmp_path / "westock"
    cap_dir = cache_dir / "quote"
    cap_dir.mkdir(parents=True)
    (cap_dir / "sh600519.json").write_text("{ 不是合法 JSON", encoding="utf-8")
    store = WestockCacheStore(cache_dir)
    assert store.read("quote", "sh600519") is None
    status = WestockBridge(store).connection_status()
    assert status["data"]["unavailable_count"] == len(CAPABILITIES)

    root = tmp_path / "repo"
    (root / "state/dashboard/westock/quote").mkdir(parents=True, exist_ok=True)
    (root / "state/dashboard/westock/quote/sh600519.json").write_text(
        "{ 损坏", encoding="utf-8"
    )
    app = create_app(config_factory(project_root=root), enable_static=False)
    from fastapi.testclient import TestClient

    with TestClient(app, base_url="https://127.0.0.1") as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "secret123"})
        response = client.get("/api/connections/westock")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_read_rejects_wrong_schema_version(tmp_path: Path):
    store = WestockCacheStore(tmp_path / "westock")
    store.write_export("quote", {"last": 1}, scope="sh600519")
    path = tmp_path / "westock" / "quote" / "sh600519.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["schema_version"] = 999
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert store.read("quote", "sh600519") is None


def test_refresh_dedupes_and_enforces_limit(tmp_path: Path):
    bridge = WestockBridge(WestockCacheStore(tmp_path / "w"))
    result = bridge.request_refresh(["quote", "quote", "profile", "quote"])
    assert result["requested"] == ["quote", "profile"]
    with pytest.raises(ValueError):
        bridge.request_refresh(["quote"] * (MAX_REFRESH_CAPABILITIES + 1))
