"""统一错误结构与静态托管测试。"""

from __future__ import annotations

from pathlib import Path

from app.errors import DashboardError, error_body, ok_body


class TestErrorBody:
    def test_error_body_structure(self):
        body = error_body("some_code", "some message")
        assert body == {"ok": False, "error": {"code": "some_code", "message": "some message"}}

    def test_ok_body(self):
        assert ok_body() == {"ok": True}
        assert ok_body({"a": 1}) == {"ok": True, "a": 1}

    def test_dashboard_error(self):
        err = DashboardError("code", "msg")
        assert err.status_code == 400
        err2 = DashboardError("code", "msg", status_code=418)
        assert err2.status_code == 418


class TestStaticHosting:
    def test_root_without_dist(self, app, config_factory, monkeypatch):
        """dist 不存在时根路径返回提示 JSON。"""
        import app.main as main_mod
        from fastapi.testclient import TestClient

        # 强制 dist 不存在
        monkeypatch.setattr(main_mod, "_dist_dir", lambda: Path("__nonexistent_dist__"))
        app2 = main_mod.create_app(config_factory(), enable_static=True)
        with TestClient(app2, base_url="https://127.0.0.1") as c:
            resp = c.get("/")
            assert resp.status_code == 200
            body = resp.json()
            assert body["ok"] is True
            assert "未构建" in body["message"]

    def test_spa_with_dist(self, tmp_path, config_factory, monkeypatch):
        """构建 dist 后：SPA 路由回退 index.html，assets 可访问。"""
        from app.main import create_app
        from fastapi.testclient import TestClient

        dist = tmp_path / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text("<html>dashboard</html>", encoding="utf-8")
        (dist / "assets" / "app.js").write_text("//app", encoding="utf-8")

        import app.main as main_mod

        def fake_mount(target_app):
            from fastapi.responses import FileResponse, JSONResponse
            from fastapi.staticfiles import StaticFiles

            dist_dir = dist
            target_app.mount("/assets", StaticFiles(directory=dist_dir / "assets"), name="assets")

            @target_app.get("/{full_path:path}", include_in_schema=False)
            async def _spa(full_path: str):
                candidate = (dist_dir / full_path).resolve()
                if candidate.is_file() and dist_dir in candidate.parents:
                    return FileResponse(candidate)
                index = dist_dir / "index.html"
                if index.is_file():
                    return FileResponse(index)
                return JSONResponse({"ok": False, "error": {"code": "not_found", "message": "页面不存在"}}, status_code=404)

        monkeypatch.setattr(main_mod, "_mount_static", fake_mount)
        app2 = main_mod.create_app(config_factory(), enable_static=True)
        with TestClient(app2, base_url="https://127.0.0.1") as c:
            resp = c.get("/overview")
            assert resp.status_code == 200
            assert "dashboard" in resp.text
            resp_asset = c.get("/assets/app.js")
            assert resp_asset.status_code == 200
            assert resp_asset.text == "//app"

    def test_spa_path_traversal_blocked(self, tmp_path, config_factory, monkeypatch):
        """路径穿越应被拒绝，读不到 dist 外的文件。"""
        from app.main import create_app
        from fastapi.testclient import TestClient

        dist = tmp_path / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text("<html>dashboard</html>", encoding="utf-8")
        secret = tmp_path / "secret.txt"
        secret.write_text("TOP SECRET", encoding="utf-8")

        import app.main as main_mod

        def fake_mount(target_app):
            from fastapi.responses import FileResponse, JSONResponse
            from fastapi.staticfiles import StaticFiles

            dist_dir = dist
            target_app.mount("/assets", StaticFiles(directory=dist_dir / "assets"), name="assets")

            @target_app.get("/{full_path:path}", include_in_schema=False)
            async def _spa(full_path: str):
                candidate = (dist_dir / full_path).resolve()
                if candidate.is_file() and dist_dir in candidate.parents:
                    return FileResponse(candidate)
                index = dist_dir / "index.html"
                if index.is_file():
                    return FileResponse(index)
                return JSONResponse({"ok": False, "error": {"code": "not_found", "message": "页面不存在"}}, status_code=404)

        monkeypatch.setattr(main_mod, "_mount_static", fake_mount)
        app2 = main_mod.create_app(config_factory(), enable_static=True)
        with TestClient(app2, base_url="https://127.0.0.1") as c:
            resp = c.get("/..%2F..%2Fsecret.txt")
            assert "TOP SECRET" not in resp.text
            resp2 = c.get("/%2e%2e/secret.txt")
            assert "TOP SECRET" not in resp2.text
