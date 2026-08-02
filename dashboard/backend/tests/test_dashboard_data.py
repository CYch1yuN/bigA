"""UI-G2 只读数据 API 测试。"""

from __future__ import annotations

import json
from pathlib import Path

from app.main import create_app


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_snapshot_requires_auth(client):
    resp = client.get("/api/dashboard/snapshot")
    assert resp.status_code == 401


def test_snapshot_reads_fixed_project_artifacts(tmp_path, config_factory):
    root = tmp_path / "repo"
    _write(root / "state/automation/latest-daily.json", {
        "run_id": "daily-1", "state": "SUCCESS", "as_of_date": "2026-08-01"
    })
    _write(root / "state/automation/gate4b/gate4b-track-summary.json", {
        "observation_progress": 12, "observation_target": 60, "violations": []
    })
    daily = root / "reports/phase-4/daily/2026-08-01"
    _write(daily / "accounts.json", {"accounts": [{"account_id": "paper-steady"}]})
    _write(daily / "signals.json", {"signals": [{"symbol": "000001", "side": "BUY"}]})
    _write(daily / "simulated-orders.json", {"orders": []})
    _write(daily / "quality-summary.json", {"summary": {"critical": 0, "warning": 1}})
    _write(root / "state/automation/runs/daily/2026-08-01.json", {
        "run_id": "daily-1", "state": "SUCCESS", "task_type": "daily"
    })

    app = create_app(config_factory(project_root=root), enable_static=False)
    from fastapi.testclient import TestClient
    with TestClient(app, base_url="https://127.0.0.1") as client:
        assert client.post("/api/auth/login", json={"username": "admin", "password": "secret123"}).status_code == 200
        resp = client.get("/api/dashboard/snapshot")

    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "research_only"
    assert body["live_trading"] is False
    assert body["artifact_date"] == "2026-08-01"
    assert body["latest_run"]["state"] == "SUCCESS"
    assert body["gate4b"]["observation_progress"] == 12
    assert body["accounts"]["accounts"][0]["account_id"] == "paper-steady"
    assert body["signals"]["signals"][0]["symbol"] == "000001"
    assert len(body["run_history"]) == 1


def test_snapshot_missing_files_returns_availability(tmp_path, config_factory):
    app = create_app(config_factory(project_root=tmp_path / "empty"), enable_static=False)
    from fastapi.testclient import TestClient
    with TestClient(app, base_url="https://127.0.0.1") as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "secret123"})
        body = client.get("/api/dashboard/snapshot").json()
    assert body["availability"]["accounts"] is False
    assert body["accounts"] is None
    assert body["run_history"] == []
