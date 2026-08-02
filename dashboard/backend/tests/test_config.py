"""配置 fail-closed 与 HTTPS 启动校验测试。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.config import ConfigError, DashboardConfig, load_config
from app.run import build_ssl_args, main


class TestFailClosed:
    def test_missing_username(self, monkeypatch):
        monkeypatch.delenv("ASHARE_DASHBOARD_USERNAME", raising=False)
        monkeypatch.setenv("ASHARE_DASHBOARD_PASSWORD_HASH", "hash")
        monkeypatch.setenv("ASHARE_DASHBOARD_SESSION_SECRET", "s" * 40)
        with pytest.raises(ConfigError):
            load_config()

    def test_missing_password_hash(self, monkeypatch):
        monkeypatch.setenv("ASHARE_DASHBOARD_USERNAME", "admin")
        monkeypatch.delenv("ASHARE_DASHBOARD_PASSWORD_HASH", raising=False)
        monkeypatch.setenv("ASHARE_DASHBOARD_SESSION_SECRET", "s" * 40)
        with pytest.raises(ConfigError):
            load_config()

    def test_missing_session_secret(self, monkeypatch):
        monkeypatch.setenv("ASHARE_DASHBOARD_USERNAME", "admin")
        monkeypatch.setenv("ASHARE_DASHBOARD_PASSWORD_HASH", "hash")
        monkeypatch.delenv("ASHARE_DASHBOARD_SESSION_SECRET", raising=False)
        with pytest.raises(ConfigError):
            load_config()

    def test_short_session_secret(self, monkeypatch):
        monkeypatch.setenv("ASHARE_DASHBOARD_USERNAME", "admin")
        monkeypatch.setenv("ASHARE_DASHBOARD_PASSWORD_HASH", "hash")
        monkeypatch.setenv("ASHARE_DASHBOARD_SESSION_SECRET", "short")
        with pytest.raises(ConfigError):
            load_config()

    def test_empty_username_rejected(self, monkeypatch):
        monkeypatch.setenv("ASHARE_DASHBOARD_USERNAME", "   ")
        monkeypatch.setenv("ASHARE_DASHBOARD_PASSWORD_HASH", "hash")
        monkeypatch.setenv("ASHARE_DASHBOARD_SESSION_SECRET", "s" * 40)
        with pytest.raises(ConfigError):
            load_config()

    def test_default_port(self, monkeypatch):
        monkeypatch.setenv("ASHARE_DASHBOARD_USERNAME", "admin")
        monkeypatch.setenv("ASHARE_DASHBOARD_PASSWORD_HASH", "hash")
        monkeypatch.setenv("ASHARE_DASHBOARD_SESSION_SECRET", "s" * 40)
        cfg = load_config()
        assert cfg.port == 8765
        assert cfg.host == "127.0.0.1"
        assert cfg.lan_mode is False

    def test_custom_port_env(self, monkeypatch):
        monkeypatch.setenv("ASHARE_DASHBOARD_PORT", "9001")
        monkeypatch.setenv("ASHARE_DASHBOARD_USERNAME", "admin")
        monkeypatch.setenv("ASHARE_DASHBOARD_PASSWORD_HASH", "hash")
        monkeypatch.setenv("ASHARE_DASHBOARD_SESSION_SECRET", "s" * 40)
        assert load_config().port == 9001


class TestLanHttps:
    def test_lan_without_cert_rejected(self, tmp_path):
        cfg = DashboardConfig(
            username="admin",
            password_hash="h",
            session_secret="s" * 40,
            host="0.0.0.0",
            cert_file=None,
            key_file=None,
        )
        with pytest.raises(ConfigError):
            cfg.validate()

    def test_lan_with_cert_but_no_key_rejected(self, tmp_path):
        cert = tmp_path / "cert.pem"
        cert.write_text("x")
        cfg = DashboardConfig(
            username="admin",
            password_hash="h",
            session_secret="s" * 40,
            host="0.0.0.0",
            cert_file=cert,
            key_file=None,
        )
        with pytest.raises(ConfigError):
            cfg.validate()

    def test_lan_with_both_ok(self, tmp_path):
        cert = tmp_path / "cert.pem"
        key = tmp_path / "key.pem"
        cert.write_text("x")
        key.write_text("y")
        cfg = DashboardConfig(
            username="admin",
            password_hash="h",
            session_secret="s" * 40,
            host="0.0.0.0",
            cert_file=cert,
            key_file=key,
        )
        assert cfg.lan_mode is True
        cfg.validate()  # 不抛

    def test_lan_cert_file_missing(self, tmp_path):
        cfg = DashboardConfig(
            username="admin",
            password_hash="h",
            session_secret="s" * 40,
            host="192.168.1.10",
            cert_file=tmp_path / "nope.pem",
            key_file=tmp_path / "nope.pem",
        )
        with pytest.raises(ConfigError):
            cfg.validate()

    def test_build_ssl_args_pair(self, tmp_path):
        cert = tmp_path / "c.pem"
        key = tmp_path / "k.pem"
        cert.write_text("x")
        key.write_text("y")
        ssl = build_ssl_args(cert, key)
        assert ssl == {"certfile": str(cert), "keyfile": str(key)}

    def test_build_ssl_args_none(self):
        assert build_ssl_args(None, None) is None

    def test_build_ssl_args_missing_one(self, tmp_path):
        cert = tmp_path / "c.pem"
        cert.write_text("x")
        with pytest.raises(ConfigError):
            build_ssl_args(cert, None)

    def test_run_main_lan_no_https_exits_2(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ASHARE_DASHBOARD_USERNAME", "admin")
        monkeypatch.setenv("ASHARE_DASHBOARD_PASSWORD_HASH", "h")
        monkeypatch.setenv("ASHARE_DASHBOARD_SESSION_SECRET", "s" * 40)
        monkeypatch.setenv("ASHARE_DASHBOARD_HOST", "0.0.0.0")
        monkeypatch.delenv("ASHARE_DASHBOARD_CERT_FILE", raising=False)
        monkeypatch.delenv("ASHARE_DASHBOARD_KEY_FILE", raising=False)
        assert main(["--host", "0.0.0.0"]) == 2

    def test_run_main_loopback_http_ok(self, monkeypatch, tmp_path):
        """127.0.0.1 开发模式允许 HTTP：uvicorn.run 会被调用（mock 掉）。"""
        import app.run as run_mod

        called = {}
        monkeypatch.setenv("ASHARE_DASHBOARD_USERNAME", "admin")
        monkeypatch.setenv("ASHARE_DASHBOARD_PASSWORD_HASH", "h")
        monkeypatch.setenv("ASHARE_DASHBOARD_SESSION_SECRET", "s" * 40)
        monkeypatch.setenv("ASHARE_DASHBOARD_HOST", "127.0.0.1")

        def fake_run(*args, **kwargs):
            called.update(kwargs)

        monkeypatch.setattr(run_mod.uvicorn, "run", fake_run)
        assert run_mod.main([]) == 0
        assert called["ssl_certfile"] is None
        assert called["host"] == "127.0.0.1"

    def test_run_main_lan_debug_rejected(self, monkeypatch, tmp_path):
        cert = tmp_path / "c.pem"
        key = tmp_path / "k.pem"
        cert.write_text("x")
        key.write_text("y")
        monkeypatch.setenv("ASHARE_DASHBOARD_USERNAME", "admin")
        monkeypatch.setenv("ASHARE_DASHBOARD_PASSWORD_HASH", "h")
        monkeypatch.setenv("ASHARE_DASHBOARD_SESSION_SECRET", "s" * 40)
        monkeypatch.setenv("ASHARE_DASHBOARD_HOST", "0.0.0.0")
        monkeypatch.setenv("ASHARE_DASHBOARD_CERT_FILE", str(cert))
        monkeypatch.setenv("ASHARE_DASHBOARD_KEY_FILE", str(key))
        monkeypatch.setenv("ASHARE_DASHBOARD_DEBUG", "1")
        assert main(["--host", "0.0.0.0"]) == 2

    def test_run_main_lan_https_ok(self, monkeypatch, tmp_path):
        import app.run as run_mod

        cert = tmp_path / "c.pem"
        key = tmp_path / "k.pem"
        cert.write_text("x")
        key.write_text("y")
        monkeypatch.setenv("ASHARE_DASHBOARD_USERNAME", "admin")
        monkeypatch.setenv("ASHARE_DASHBOARD_PASSWORD_HASH", "h")
        monkeypatch.setenv("ASHARE_DASHBOARD_SESSION_SECRET", "s" * 40)
        monkeypatch.setenv("ASHARE_DASHBOARD_HOST", "0.0.0.0")
        monkeypatch.setenv("ASHARE_DASHBOARD_CERT_FILE", str(cert))
        monkeypatch.setenv("ASHARE_DASHBOARD_KEY_FILE", str(key))

        called = {}

        def fake_run(*args, **kwargs):
            called.update(kwargs)

        monkeypatch.setattr(run_mod.uvicorn, "run", fake_run)
        assert run_mod.main([]) == 0
        assert called["ssl_certfile"] == str(cert)
        assert called["ssl_keyfile"] == str(key)


class TestCors:
    def test_cors_wildcard_rejected(self, monkeypatch, config_factory):
        monkeypatch.setenv("ASHARE_DASHBOARD_CORS_ORIGINS", "*")
        from app.config import load_config
        from app.main import create_app

        with pytest.raises(ConfigError):
            create_app(config_factory(), enable_static=False)
