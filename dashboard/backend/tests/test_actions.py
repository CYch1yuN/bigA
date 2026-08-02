"""命令安全骨架测试：白名单、确认令牌、全局锁、超时、截断、注入防护。"""

from __future__ import annotations

import pytest

from app.errors import DashboardError
from app.executors import MockExecutor, SubprocessExecutor, _truncate
from app.security import ALLOWED_ACTIONS, FORBIDDEN_ACTIONS, SecurityManager

from .conftest import csrf_headers


def prepare_and_execute(client, action, csrf=None):
    """走完 prepare -> execute 完整流程，返回 (prepare_resp, execute_resp)。"""
    headers = csrf or csrf_headers(client)
    prep = client.post("/api/actions/prepare", json={"action": action}, headers=headers)
    if prep.status_code != 200:
        return prep, None
    token = prep.json()["confirm_token"]
    exec_resp = client.post(
        "/api/actions/execute",
        json={"action": action, "confirm_token": token},
        headers=headers,
    )
    return prep, exec_resp


class TestWhitelist:
    def test_allowed_actions_exact_set(self):
        assert ALLOWED_ACTIONS == (
            "verify",
            "daily",
            "weekly",
            "rerun",
            "backfill",
        )

    def test_forbidden_actions_set(self):
        assert FORBIDDEN_ACTIONS == ("install", "uninstall", "synthetic", "gate4b_track")

    def test_each_allowed_action_has_fixed_argv(self):
        ex = SubprocessExecutor.__new__(SubprocessExecutor)  # 不触发配置加载
        for action in ALLOWED_ACTIONS:
            argv = ex.ACTION_ARGV[action]
            assert isinstance(argv, list)
            assert "ashare_quant" in " ".join(argv)
            assert "shell" not in " ".join(argv).lower()

    def test_disallowed_action_rejected(self, auth_client):
        # 非 verify 动作在旧 /api/actions 接口被拒绝（引导到 /api/jobs）
        prep, _ = prepare_and_execute(auth_client, "schtasks")
        assert prep.status_code == 400
        assert prep.json()["error"]["code"] == "action_requires_jobs"

    def test_forbidden_install_rejected(self, auth_client):
        # install 是永久禁止动作：jobs/prepare 直接拒绝（不在白名单）
        resp = auth_client.post(
            "/api/jobs/prepare", json={"job_type": "install"}, headers=csrf_headers(auth_client)
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "invalid_job_type"

    def test_forbidden_synthetic_rejected(self, auth_client):
        resp = auth_client.post(
            "/api/jobs/prepare", json={"job_type": "synthetic"}, headers=csrf_headers(auth_client)
        )
        assert resp.status_code == 400

    def test_arbitrary_command_rejected(self, auth_client):
        for cmd in ("rm -rf /", "python -c os.system('x')", "daily --date 2024-01-01", "schtasks /create", "bash -c whoami"):
            # 任意命令不是合法作业类型
            resp = auth_client.post(
                "/api/jobs/prepare", json={"job_type": cmd}, headers=csrf_headers(auth_client)
            )
            assert resp.status_code == 400, cmd
        # 旧 actions 接口也只放行 verify
        prep, _ = prepare_and_execute(auth_client, "rm -rf /")
        assert prep.status_code == 400


class TestNoShellAndTruncation:
    def test_no_shell_in_source(self):
        import inspect

        src = inspect.getsource(SubprocessExecutor)
        assert "shell=True" not in src

    def test_truncate_bytes(self):
        long = "x" * 100000
        assert _truncate(long, 100).endswith("...[输出已截断]")

    def test_mock_executor_still_usable_for_tests(self):
        """MockExecutor 仅测试用；生产使用 SubprocessExecutor。"""
        ex = MockExecutor()
        assert ex.ACTION_ARGV["verify"][-1] == "verify"


class TestConfirmToken:
    def test_token_single_use(self, auth_client):
        # 用 jobs/prepare 流程验证令牌一次性
        prep = auth_client.post(
            "/api/jobs/prepare", json={"job_type": "verify"}, headers=csrf_headers(auth_client)
        )
        token = prep.json()["confirm_token"]
        resp1 = auth_client.post(
            "/api/jobs",
            json={"job_type": "verify", "confirm_token": token},
            headers=csrf_headers(auth_client),
        )
        assert resp1.status_code == 200
        # 同一 token 复用必须失败（写入型 consume 已消费）
        # 注意：verify 作业消费后立即复用 token -> 无效
        resp2 = auth_client.post(
            "/api/jobs",
            json={"job_type": "verify", "confirm_token": token},
            headers=csrf_headers(auth_client),
        )
        assert resp2.status_code == 403

    def test_token_expired(self, auth_client, app):
        security: SecurityManager = app.state.security
        prep = auth_client.post(
            "/api/jobs/prepare", json={"job_type": "verify"}, headers=csrf_headers(auth_client)
        )
        token = prep.json()["confirm_token"]
        with security._lock:
            security._confirm_tokens[token]["expires_at"] = 0
        resp = auth_client.post(
            "/api/jobs",
            json={"job_type": "verify", "confirm_token": token},
            headers=csrf_headers(auth_client),
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "invalid_confirm_token"

    def test_token_cross_job(self, auth_client):
        prep = auth_client.post(
            "/api/jobs/prepare", json={"job_type": "verify"}, headers=csrf_headers(auth_client)
        )
        token = prep.json()["confirm_token"]
        resp = auth_client.post(
            "/api/jobs",
            json={"job_type": "weekly", "confirm_token": token},
            headers=csrf_headers(auth_client),
        )
        assert resp.status_code == 403

    def test_token_missing(self, auth_client):
        resp = auth_client.post(
            "/api/jobs", json={"job_type": "verify"}, headers=csrf_headers(auth_client)
        )
        assert resp.status_code == 403

    def test_execute_requires_auth(self, client):
        resp = client.post("/api/jobs", json={"job_type": "verify", "confirm_token": "x"})
        assert resp.status_code == 401

    def test_prepare_requires_auth(self, client):
        resp = client.post("/api/jobs/prepare", json={"job_type": "verify"})
        assert resp.status_code == 401


class TestJobApiValidation:
    def test_invalid_job_type(self, auth_client):
        resp = auth_client.post(
            "/api/jobs/prepare", json={"job_type": "rm_rf"}, headers=csrf_headers(auth_client)
        )
        assert resp.status_code == 400

    def test_daily_requires_date(self, auth_client):
        resp = auth_client.post(
            "/api/jobs",
            json={"job_type": "daily", "confirm_token": "x"},
            headers=csrf_headers(auth_client),
        )
        # 先通过 prepare 拿 token 再验证日期缺失
        prep = auth_client.post(
            "/api/jobs/prepare", json={"job_type": "daily"}, headers=csrf_headers(auth_client)
        )
        token = prep.json()["confirm_token"]
        resp = auth_client.post(
            "/api/jobs",
            json={"job_type": "daily", "confirm_token": token},
            headers=csrf_headers(auth_client),
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "missing_date"

    def test_invalid_date(self, auth_client):
        # prepare 与 create 使用相同参数（令牌绑定参数一致性）
        prep = auth_client.post(
            "/api/jobs/prepare",
            json={"job_type": "daily", "date": "2026-13-99"},
            headers=csrf_headers(auth_client),
        )
        token = prep.json()["confirm_token"]
        resp = auth_client.post(
            "/api/jobs",
            json={"job_type": "daily", "date": "2026-13-99", "confirm_token": token},
            headers=csrf_headers(auth_client),
        )
        assert resp.status_code == 400

    def test_token_param_mismatch_rejected(self, auth_client):
        """确认令牌绑定的参数与创建参数不一致 → 403（防替换攻击）。"""
        prep = auth_client.post(
            "/api/jobs/prepare",
            json={"job_type": "daily", "date": "2026-08-03"},
            headers=csrf_headers(auth_client),
        )
        token = prep.json()["confirm_token"]
        # 创建时换了一个日期 → 参数不匹配
        resp = auth_client.post(
            "/api/jobs",
            json={"job_type": "daily", "date": "2026-08-04", "confirm_token": token},
            headers=csrf_headers(auth_client),
        )
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "invalid_confirm_token"

    def test_backfill_range_too_large(self, auth_client):
        prep = auth_client.post(
            "/api/jobs/prepare",
            json={"job_type": "backfill", "start_date": "2026-01-01", "end_date": "2026-12-31"},
            headers=csrf_headers(auth_client),
        )
        token = prep.json()["confirm_token"]
        resp = auth_client.post(
            "/api/jobs",
            json={
                "job_type": "backfill",
                "start_date": "2026-01-01",
                "end_date": "2026-12-31",
                "confirm_token": token,
            },
            headers=csrf_headers(auth_client),
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "range_too_large"


class TestJobsApi:
    def test_jobs_list_and_get(self, auth_client):
        resp = auth_client.get("/api/jobs")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert isinstance(resp.json()["jobs"], list)

    def test_job_not_found(self, auth_client):
        resp = auth_client.get("/api/jobs/nonexistent-id-12345")
        assert resp.status_code == 404

    def test_jobs_require_auth(self, client):
        resp = client.get("/api/jobs")
        assert resp.status_code == 401
