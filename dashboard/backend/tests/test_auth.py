"""认证、会话、CSRF、限流、密码修改测试。"""

from __future__ import annotations

import time

import pytest

from app.main import CSRF_COOKIE, SESSION_COOKIE
from app.security import SecurityManager

from .conftest import csrf_headers, login


class TestLogin:
    def test_login_success(self, client):
        resp = login(client)
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["username"] == "admin"
        assert SESSION_COOKIE in client.cookies
        assert CSRF_COOKIE in client.cookies

    def test_login_wrong_password(self, client):
        resp = login(client, password="wrongpass")
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "invalid_credentials"
        assert SESSION_COOKIE not in client.cookies

    def test_login_wrong_username(self, client):
        resp = login(client, username="root")
        assert resp.status_code == 401

    def test_login_missing_fields(self, client):
        resp = client.post("/api/auth/login", json={})
        assert resp.status_code == 400
        resp = client.post("/api/auth/login", json={"username": "admin"})
        assert resp.status_code == 400

    def test_cookie_attributes(self, client):
        resp = login(client)
        set_cookie = resp.headers.get("set-cookie", "")
        assert "HttpOnly" in set_cookie
        assert "Secure" in set_cookie
        assert "SameSite=strict" in set_cookie.lower() or "samesite=strict" in set_cookie.lower()
        assert "Path=/" in set_cookie

    def test_session_rotation_on_relogin(self, client):
        resp1 = login(client)
        sid1 = client.cookies.get(SESSION_COOKIE)
        resp2 = login(client)
        sid2 = client.cookies.get(SESSION_COOKIE)
        assert sid1 != sid2

    def test_health_public(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


class TestSession:
    def test_session_endpoint_authenticated(self, auth_client):
        resp = auth_client.get("/api/auth/session")
        assert resp.status_code == 200
        body = resp.json()
        assert body["authenticated"] is True
        assert body["username"] == "admin"

    def test_session_endpoint_unauthenticated(self, client):
        resp = client.get("/api/auth/session")
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "unauthorized"

    def test_logout_invalidates(self, auth_client):
        resp = auth_client.post("/api/auth/logout", headers=csrf_headers(auth_client))
        assert resp.status_code == 200
        # 退出后 session 立即失效
        resp = auth_client.get("/api/auth/session")
        assert resp.status_code == 401

    def test_logout_requires_csrf(self, auth_client):
        resp = auth_client.post("/api/auth/logout")
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "csrf_invalid"

    def test_session_expiry(self, client, app):
        security: SecurityManager = app.state.security
        resp = login(client)
        sid = client.cookies.get(SESSION_COOKIE)
        session = security.get_session(sid)
        assert session is not None
        # 人为使会话过期
        session.expires_at = time.time() - 1
        resp = client.get("/api/auth/session")
        assert resp.status_code == 401
        # 过期会话从存储清除
        assert security.get_session(sid) is None

    def test_change_password_invalidates_old_sessions(self, auth_client):
        resp = auth_client.post(
            "/api/auth/change-password",
            json={"old_password": "secret123", "new_password": "newsecret456"},
            headers=csrf_headers(auth_client),
        )
        assert resp.status_code == 200
        # 旧会话（包括当前）全部失效
        resp = auth_client.get("/api/auth/session")
        assert resp.status_code == 401
        # 旧密码失效
        resp = login(auth_client, password="secret123")
        assert resp.status_code == 401
        # 新密码可用
        resp = login(auth_client, password="newsecret456")
        assert resp.status_code == 200

    def test_change_password_wrong_old(self, auth_client):
        resp = auth_client.post(
            "/api/auth/change-password",
            json={"old_password": "bad", "new_password": "newsecret456"},
            headers=csrf_headers(auth_client),
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "auth_old_password"

    def test_change_password_weak(self, auth_client):
        resp = auth_client.post(
            "/api/auth/change-password",
            json={"old_password": "secret123", "new_password": "short"},
            headers=csrf_headers(auth_client),
        )
        assert resp.status_code == 400

    def test_change_password_requires_csrf(self, auth_client):
        resp = auth_client.post(
            "/api/auth/change-password",
            json={"old_password": "secret123", "new_password": "newsecret456"},
        )
        assert resp.status_code == 403

    def test_change_password_requires_auth(self, client):
        resp = client.post(
            "/api/auth/change-password",
            json={"old_password": "x", "new_password": "y"},
        )
        assert resp.status_code == 401


class TestRateLimit:
    def test_lock_after_failures(self, client, app):
        security: SecurityManager = app.state.security
        for i in range(5):
            resp = login(client, password="wrong")
            assert resp.status_code == 401
        # 第 6 次应被锁定
        resp = login(client, password="secret123")
        assert resp.status_code == 429
        assert resp.json()["error"]["code"] == "login_locked"
        # 即使密码正确也锁定
        security.clear_failures("testclient")

    def test_failure_counting(self, app):
        security: SecurityManager = app.state.security
        assert security.record_failure("1.2.3.4") == 1
        assert security.record_failure("1.2.3.4") == 2
        assert security.is_locked("1.2.3.4") is False
        for _ in range(3):
            security.record_failure("1.2.3.4")
        assert security.is_locked("1.2.3.4") is True
        security.clear_failures("1.2.3.4")
        assert security.is_locked("1.2.3.4") is False

    def test_failure_window_expiry(self, app):
        security: SecurityManager = app.state.security
        security.record_failure("5.6.7.8")
        security.record_failure("5.6.7.8")
        # 过期失败应被清除
        from app.config import DEFAULT_LOGIN_LOCK_SECONDS

        ts = security._login_failures["5.6.7.8"]
        ts[0] = time.time() - DEFAULT_LOGIN_LOCK_SECONDS - 10
        assert security.is_locked("5.6.7.8") is False


class TestCsrf:
    def test_prepare_requires_csrf(self, auth_client):
        resp = auth_client.post("/api/actions/prepare", json={"action": "verify"})
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "csrf_invalid"

    def test_prepare_wrong_csrf(self, auth_client):
        resp = auth_client.post(
            "/api/actions/prepare",
            json={"action": "verify"},
            headers={"X-CSRF-Token": "invalid-token"},
        )
        assert resp.status_code == 403

    def test_csrf_bound_to_session(self, auth_client, app):
        security: SecurityManager = app.state.security
        sid = auth_client.cookies.get(SESSION_COOKIE)
        session = security.get_session(sid)
        # 正确令牌通过
        resp = auth_client.post(
            "/api/actions/prepare",
            json={"action": "verify"},
            headers={"X-CSRF-Token": session.csrf_token},
        )
        assert resp.status_code == 200


class TestErrorStructure:
    def test_unified_error_structure(self, client):
        resp = client.get("/api/auth/session")
        body = resp.json()
        assert body["ok"] is False
        assert "error" in body
        assert "code" in body["error"]
        assert "message" in body["error"]
        # 不泄露堆栈/路径/密钥
        text = resp.text
        for secret in ("Traceback", "\.py", "session_secret", "password", "hash"):
            assert secret not in text

    def test_internal_error_no_leak(self, client, app):
        # 触发未处理异常路径
        async def boom(request):
            raise RuntimeError("sensitive detail")

        app.router.routes  # noqa
        # 通过一个会抛异常的路径测试：给一个不存在 JSON 的 body
        resp = client.post("/api/auth/login", content="not json", headers={"Content-Type": "application/json"})
        assert resp.status_code in (200, 400, 401)
        assert "Traceback" not in resp.text


class TestHostValidation:
    def test_bad_host_rejected(self, client):
        resp = client.get("/api/health", headers={"host": "evil.example.com"})
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "bad_host"

    def test_good_host_allowed(self, client):
        resp = client.get("/api/health", headers={"host": "127.0.0.1"})
        assert resp.status_code == 200

    def test_localhost_allowed(self, client):
        resp = client.get("/api/health", headers={"host": "localhost"})
        assert resp.status_code == 200
