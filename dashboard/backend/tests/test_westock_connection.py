"""Westock cache bridge and authenticated connection API tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.main import CSRF_COOKIE, create_app
from app.westock_bridge import CAPABILITIES, WestockCacheStore, build_westock_bridge


def csrf_headers(client) -> dict[str, str]:
    csrf = client.cookies.get(CSRF_COOKIE)
    return {"X-CSRF-Token": csrf} if csrf else {}


def test_connection_requires_auth(client):
    assert client.get("/api/connections/westock").status_code == 401


def test_empty_cache_reports_cache_export(tmp_path: Path):
    status = build_westock_bridge(tmp_path).connection_status()
    assert status["transport"] == "cache_export"
    assert status["is_realtime"] is False
    assert status["availability"] == {
        "connected": False,
        "direct_mcp": False,
        "cache_export": True,
        "cache_available": False,
        "manual_refresh": False,
    }
    assert status["data"]["capability_count"] == len(CAPABILITIES)
    assert status["data"]["unavailable_count"] == len(CAPABILITIES)


def test_cache_export_is_atomic_and_fresh(tmp_path: Path):
    store = WestockCacheStore(tmp_path / "westock")
    store.write_export("quote", {"last": 1350.6}, scope="sh600519", as_of="2026-07-31")
    cached = store.read("quote", "sh600519")
    assert cached is not None
    assert cached["schema_version"] == 1
    assert cached["transport"] == "cache_export"
    assert cached["data"]["last"] == 1350.6
    assert list((tmp_path / "westock" / "quote").glob("*.tmp")) == []
    status = build_westock_bridge(tmp_path.parent).connection_status()
    # build_westock_bridge appends state/dashboard/westock, so use direct bridge below.
    from app.westock_bridge import WestockBridge
    status = WestockBridge(store).connection_status()
    # cache_export 模式：connected 恒 False（MCP 未连接），缓存可用性单独表达
    assert status["availability"]["connected"] is False
    assert status["availability"]["direct_mcp"] is False
    assert status["availability"]["cache_available"] is True
    assert status["data"]["connected"] is False
    assert status["data"]["cache_available"] is True
    quote = next(item for item in status["data"]["capabilities"] if item["name"] == "quote")
    assert quote["status"] == "fresh"


def test_old_cache_is_stale(tmp_path: Path):
    store = WestockCacheStore(tmp_path / "westock")
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    store.write_export("quote", {"last": 1}, fetched_at=old)
    from app.westock_bridge import WestockBridge
    status = WestockBridge(store).connection_status()
    quote = next(item for item in status["data"]["capabilities"] if item["name"] == "quote")
    assert quote["status"] == "stale"
    assert status["cache_status"] == "stale"


@pytest.mark.parametrize("capability,scope", [("unknown", "global"), ("quote", "../escape")])
def test_cache_rejects_unsupported_or_unsafe_paths(tmp_path: Path, capability: str, scope: str):
    store = WestockCacheStore(tmp_path)
    with pytest.raises(ValueError):
        store.write_export(capability, {}, scope=scope)


def test_connection_api_reads_fixed_project_cache(tmp_path: Path, config_factory):
    root = tmp_path / "repo"
    store = WestockCacheStore(root / "state/dashboard/westock")
    # fetched_at 为包含 "401" 数字子串的合法时间戳：验证安全检查不误伤合法时间戳
    store.write_export("profile", {"name": "贵州茅台"}, scope="sh600519",
                       fetched_at="2026-08-03T10:21:22.401649+00:00")
    app = create_app(config_factory(project_root=root), enable_static=False)
    from fastapi.testclient import TestClient
    with TestClient(app, base_url="https://127.0.0.1") as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "secret123"})
        response = client.get("/api/connections/westock")
    assert response.status_code == 200
    body = response.json()
    assert body["transport"] == "cache_export"
    assert body["availability"]["connected"] is False
    assert body["availability"]["cache_available"] is True
    assert body["data"]["cache_available"] is True
    assert "state/dashboard" not in response.text
    # 语义化安全检查（替代原 "401" not in text——会误伤合法时间戳中的 401 数字）：
    # - 公开响应不得暴露 HTTP 401 探测细节（完整短语）
    # - 检查 warnings / message 等公开错误字段
    assert "HTTP 401" not in response.text
    assert not any("401" in str(w) for w in body.get("warnings", []))
    message = body.get("message")
    if message is not None:
        assert "401" not in str(message)
    assert any("MCP 直连授权" in w for w in body["warnings"])  # 授权状态文案保留


def test_refresh_requires_csrf_and_rejects_unknown_capability(auth_client):
    denied = auth_client.post("/api/connections/westock/refresh", json={"capabilities": ["quote"]})
    assert denied.status_code == 403
    invalid = auth_client.post(
        "/api/connections/westock/refresh",
        json={"capabilities": ["paper_trade"]},
        headers=csrf_headers(auth_client),
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "invalid_capability"


def test_refresh_in_cache_mode_is_honest(auth_client):
    response = auth_client.post(
        "/api/connections/westock/refresh",
        json={"capabilities": ["quote", "profile"]},
        headers=csrf_headers(auth_client),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is False
    assert body["transport"] == "cache_export"
    assert "WorkBuddy" in body["message"]
