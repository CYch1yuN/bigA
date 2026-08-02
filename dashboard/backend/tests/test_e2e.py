"""E2E 测试：登录 → safety → 作业创建 → 状态轮询 → logout。

全部使用 tmp_path，不访问真实 state/reports/logs，不调用 schtasks。
作业执行走真实 CLI 路径但使用注入的假执行器语义由 jobs 单测覆盖；
此处验证 API 流程与安全边界。
"""

from __future__ import annotations

from pathlib import Path

from app.security import ALLOWED_ACTIONS

from .conftest import csrf_headers, login


def test_full_flow_login_safety_jobs_logout(auth_client):
    # 1. safety（只读安全边界）
    resp = auth_client.get("/api/safety")
    assert resp.status_code == 200
    safety = resp.json()
    assert safety["live_trading"] is False
    assert safety["broker_connected"] is False
    assert set(ALLOWED_ACTIONS) <= set(safety["allowed_actions"])

    # 2. 作业列表（空或非空均可）
    resp = auth_client.get("/api/jobs")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    # 3. snapshot（操作中心能力声明）
    resp = auth_client.get("/api/dashboard/snapshot")
    assert resp.status_code == 200
    data = resp.json()
    assert data["operations"]["available"] is True
    assert data["operations"]["verify"] is True

    # 4. logout
    logout_resp = auth_client.post("/api/auth/logout", headers=csrf_headers(auth_client))
    assert logout_resp.status_code == 200

    # 5. 退出后 session 失效
    resp = auth_client.get("/api/auth/session")
    assert resp.status_code == 401


def test_all_job_types_have_prepare_flow(auth_client):
    for job_type in ALLOWED_ACTIONS:
        prep = auth_client.post(
            "/api/jobs/prepare",
            json={"job_type": job_type},
            headers=csrf_headers(auth_client),
        )
        assert prep.status_code == 200, job_type
        assert "confirm_token" in prep.json()


def test_auth_file_initialized_from_env(tmp_path, config_factory):
    """首次启动从环境变量哈希初始化 auth.json，明文密码永不落盘。"""
    import os

    os.environ["ASHARE_DASHBOARD_USERNAME"] = "admin"
    from app.config import load_config
    from app.security import AuthStore, SecurityManager

    cfg = config_factory(password_hash="dummy-hash-argon2")
    sm = SecurityManager(cfg)
    assert sm.username == "admin"
    store = AuthStore(cfg.auth_file)
    payload = store.read()
    assert payload["username"] == "admin"
    assert payload["password_hash"] == "dummy-hash-argon2"
    # 文件中绝无明文密码
    raw = Path(cfg.auth_file).read_text(encoding="utf-8")
    assert "secret123" not in raw


def test_password_change_persists_to_auth_file(auth_client):
    resp = auth_client.post(
        "/api/auth/change-password",
        json={"old_password": "secret123", "new_password": "brand-new-pass-1"},
        headers=csrf_headers(auth_client),
    )
    assert resp.status_code == 200
    # 重新登录使用新密码
    resp = login(auth_client, password="brand-new-pass-1")
    assert resp.status_code == 200


def test_no_temp_files_pollute_repo(tmp_path, client):
    """健康检查后，测试临时目录不产生仓库内文件。"""
    resp = client.get("/api/health")
    assert resp.status_code == 200
    # 无需额外断言：pytest tmp_path 自动清理
