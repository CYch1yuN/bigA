"""Dashboard 后端测试共享夹具。

全部使用 tmp_path / 内存状态，不访问真实 state、reports、logs。
应用注入假执行器，作业不触发真实 CLI。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# 确保 app 包可导入（无需安装）
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import DashboardConfig  # noqa: E402
from app.main import CSRF_COOKIE, SESSION_COOKIE, create_app  # noqa: E402
from app.security import SecurityManager  # noqa: E402


def make_password_hash(password: str) -> str:
    from argon2 import PasswordHasher

    return PasswordHasher().hash(password)


@pytest.fixture()
def auth_file(tmp_path: Path) -> Path:
    return tmp_path / "state" / "dashboard" / "auth.json"


@pytest.fixture()
def config_factory(auth_file: Path):
    def _make(**overrides) -> DashboardConfig:
        base = dict(
            username="admin",
            password_hash=make_password_hash("secret123"),
            session_secret="s" * 40,
            host="127.0.0.1",
            port=8765,
            auth_file=auth_file,
        )
        base.update(overrides)
        return DashboardConfig(**base)

    return _make


class FakeSubprocessExecutor:
    """假执行器：返回可编排结果，不触发真实 CLI。

    接口与 SubprocessExecutor 对齐（validate_action / execute / to_public_dict）。
    """

    def __init__(self, default_result=None):
        from app.executors import ActionResult

        self.execute_count = 0
        self.default_result = default_result or ActionResult(
            ok=True,
            action="verify",
            stdout="daily 2026-08-03: SUCCESS (exit=0)",
            exit_code=0,
            duration_ms=1,
            mock=True,
        )

    def validate_action(self, action: str) -> None:
        from app.errors import DashboardError
        from app.security import ALLOWED_ACTIONS, FORBIDDEN_ACTIONS

        if action not in ALLOWED_ACTIONS:
            raise DashboardError("action_not_allowed", f"动作不允许: {action}", status_code=403)
        if action in FORBIDDEN_ACTIONS:
            raise DashboardError("action_forbidden", f"动作被永久禁止: {action}", status_code=403)

    def is_busy(self) -> bool:
        return False

    async def execute(self, action, *, date=None, task=None, timeout=None):
        self.execute_count += 1
        from app.executors import ActionResult

        return ActionResult(
            ok=True,
            action=action,
            stdout=(
                "daily 2026-08-03: SUCCESS (exit=0)"
                if action == "daily"
                else "verify" if action == "verify" else f"{action} OK"
            ),
            exit_code=0,
            duration_ms=1,
            mock=True,
        )


@pytest.fixture()
def fake_executor():
    return FakeSubprocessExecutor()


@pytest.fixture()
def app(config_factory, fake_executor):
    """标准测试应用（非静态托管）；注入假执行器避免真实 CLI。"""
    return create_app(
        config_factory(project_root=None),
        enable_static=False,
        executor=fake_executor,
    )


@pytest.fixture()
def client(app):
    from fastapi.testclient import TestClient

    # Secure cookie 仅随 HTTPS 发送，测试以 https base_url 模拟真实浏览器
    with TestClient(app, base_url="https://127.0.0.1") as c:
        yield c


@pytest.fixture()
def auth_client(client):
    """已登录的客户端（附带 session 与 csrf cookie）。"""
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "secret123"})
    assert resp.status_code == 200, resp.text
    return client


def login(client, username="admin", password="secret123"):
    return client.post("/api/auth/login", json={"username": username, "password": password})


def csrf_headers(client) -> dict[str, str]:
    csrf = client.cookies.get(CSRF_COOKIE)
    return {"X-CSRF-Token": csrf} if csrf else {}
