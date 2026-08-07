"""预测有效性摘要 API 测试（只读，严格 schema 校验 + 脱敏 + 后端派生门槛）。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.main import create_app
from app.prediction_service import EVALUATION_TTL_SECONDS

# 动态时间（相对本地 UTC 时钟；禁止固定日历时间）
_NOW = datetime.now(timezone.utc)
FRESH_EVAL = (_NOW - timedelta(hours=1)).isoformat()
STALE_EVAL = (_NOW - timedelta(seconds=EVALUATION_TTL_SECONDS + 3600)).isoformat()
FUTURE_EVAL = (_NOW + timedelta(minutes=10)).isoformat()


def _base(**overrides) -> dict:
    payload = {
        "model_version": "v1.0",
        "task_name": "未来 5 个交易日上涨 >=2%",
        "horizon_days": 5,
        "target_return": 0.02,
        "accuracy": 0.85,
        "precision": 0.75,
        "recall": 0.60,
        "auc": 0.61,
        "sample_count": 1200,
        "test_start": "2026-01-01",
        "test_end": "2026-06-30",
        "net_return": 0.08,
        "max_drawdown": -0.12,
        "sharpe": 1.2,
        "benchmark_return": 0.03,
        "gate_status": "passed",
        "evaluated_at": FRESH_EVAL,
    }
    payload.update(overrides)
    return payload


def _eval_path(root: Path) -> Path:
    return root / "reports" / "research" / "prediction" / "latest.json"


def _write_eval(root: Path, payload: object) -> Path:
    path = _eval_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _make_app(root: Path, config_factory):
    return create_app(config_factory(project_root=root), enable_static=False)


@pytest.fixture()
def pred_root(tmp_path: Path) -> Path:
    return tmp_path / "repo"


@pytest.fixture()
def auth_client(pred_root, config_factory):
    from fastapi.testclient import TestClient

    app = _make_app(pred_root, config_factory)
    with TestClient(app, base_url="https://127.0.0.1") as client:
        client.post("/api/auth/login", json={"username": "admin", "password": "secret123"})
        yield client


def _tree_fingerprint(root: Path) -> dict[str, str]:
    import hashlib

    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _avail(auth_client) -> str:
    return auth_client.get("/api/research/prediction-summary").json()["availability"]


# ---------------------------------------------------------------------- #
# 认证
# ---------------------------------------------------------------------- #

def test_requires_auth(pred_root, config_factory):
    from fastapi.testclient import TestClient

    app = _make_app(pred_root, config_factory)
    with TestClient(app, base_url="https://127.0.0.1") as client:
        r = client.get("/api/research/prediction-summary")
    assert r.status_code == 401


# ---------------------------------------------------------------------- #
# unavailable 场景
# ---------------------------------------------------------------------- #

def test_missing_file_unavailable(auth_client):
    r = auth_client.get("/api/research/prediction-summary")
    body = r.json()
    assert body["ok"] is True
    assert body["availability"] == "unavailable"
    assert body["cache_status"] == "unavailable"
    assert body["data"] is None
    assert body["transport"] == "local_evaluation"
    assert body["is_realtime"] is False
    assert any("暂无经过严格样本外验证的预测准确率" in w for w in body["warnings"])


def test_corrupt_json_unavailable(auth_client, pred_root):
    _write_eval(pred_root, "{ not valid json !!!")
    body = auth_client.get("/api/research/prediction-summary").json()
    assert body["availability"] == "unavailable"
    assert body["data"] is None


def test_invalid_schema_extra_key(auth_client, pred_root):
    _write_eval(pred_root, dict(_base(), evil="x"))
    assert _avail(auth_client) == "unavailable"


def test_invalid_schema_bad_date(auth_client, pred_root):
    _write_eval(pred_root, dict(_base(), test_end="2026-13-99"))
    assert _avail(auth_client) == "unavailable"


def test_invalid_schema_path_injection(auth_client, pred_root):
    _write_eval(pred_root, dict(_base(), task_name=r"C:\Windows\system32\evil.exe"))
    assert _avail(auth_client) == "unavailable"


def test_accuracy_out_of_range(auth_client, pred_root):
    for acc in (1.5, -0.1):
        _write_eval(pred_root, dict(_base(), accuracy=acc))
        assert _avail(auth_client) == "unavailable", f"accuracy={acc}"


def test_nan_infinity_rejected(auth_client, pred_root):
    for raw in ('{"accuracy": NaN}', '{"accuracy": Infinity}', '{"accuracy": -Infinity}'):
        _write_eval(pred_root, raw)
        body = auth_client.get("/api/research/prediction-summary").json()
        assert body["availability"] == "unavailable"
        assert body["data"] is None


def test_sample_count_below_display_threshold(auth_client, pred_root):
    _write_eval(pred_root, dict(_base(), sample_count=10))
    body = auth_client.get("/api/research/prediction-summary").json()
    assert body["availability"] == "unavailable"
    assert body["data"] is None
    assert any("样本数不足" in w for w in body["warnings"])


# ---------------------------------------------------------------------- #
# 严格 evaluated_at
# ---------------------------------------------------------------------- #

def test_evaluated_at_naive_rejected(auth_client, pred_root):
    naive = (_NOW - timedelta(hours=1)).replace(tzinfo=None).isoformat()
    _write_eval(pred_root, dict(_base(), evaluated_at=naive))
    assert _avail(auth_client) == "unavailable"


def test_evaluated_at_bare_date_rejected(auth_client, pred_root):
    _write_eval(pred_root, dict(_base(), evaluated_at="2026-08-08"))
    assert _avail(auth_client) == "unavailable"


def test_evaluated_at_leading_trailing_space_rejected(auth_client, pred_root):
    _write_eval(pred_root, dict(_base(), evaluated_at=f" {FRESH_EVAL} "))
    assert _avail(auth_client) == "unavailable"


def test_evaluated_at_invalid_tz_rejected(auth_client, pred_root):
    _write_eval(pred_root, dict(_base(), evaluated_at=f"{FRESH_EVAL}+99:00"))
    assert _avail(auth_client) == "unavailable"


def test_evaluated_at_z_accepted(auth_client, pred_root):
    z_ts = (_NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _write_eval(pred_root, dict(_base(), evaluated_at=z_ts))
    body = auth_client.get("/api/research/prediction-summary").json()
    assert body["availability"] == "fresh"
    assert body["data"] is not None


def test_evaluated_at_offset_with_millis_accepted(auth_client, pred_root):
    ms = (_NOW - timedelta(hours=1)).isoformat(timespec="milliseconds")
    _write_eval(pred_root, dict(_base(), evaluated_at=ms))
    body = auth_client.get("/api/research/prediction-summary").json()
    assert body["availability"] == "fresh"


# ---------------------------------------------------------------------- #
# future timestamp
# ---------------------------------------------------------------------- #

def test_future_timestamp_unavailable(auth_client, pred_root):
    _write_eval(pred_root, dict(_base(), evaluated_at=FUTURE_EVAL))
    body = auth_client.get("/api/research/prediction-summary").json()
    assert body["availability"] == "unavailable"
    assert body["cache_status"] == "unavailable"
    assert body["data"] is None
    assert any("评估时间晚于本地时钟" in w for w in body["warnings"])


# ---------------------------------------------------------------------- #
# 日期区间 / 任务参数
# ---------------------------------------------------------------------- #

def test_test_start_after_test_end_rejected(auth_client, pred_root):
    _write_eval(
        pred_root,
        dict(_base(), test_start="2026-08-01", test_end="2026-07-01"),
    )
    assert _avail(auth_client) == "unavailable"


def test_evaluated_at_before_test_end_rejected(auth_client, pred_root):
    _write_eval(
        pred_root,
        dict(_base(), test_end="2026-08-10"),  # evaluated_at(=now) < test_end
    )
    assert _avail(auth_client) == "unavailable"


def test_horizon_days_out_of_range(auth_client, pred_root):
    for h in (0, 251):
        _write_eval(pred_root, dict(_base(), horizon_days=h))
        assert _avail(auth_client) == "unavailable", f"horizon={h}"


def test_target_return_out_of_range(auth_client, pred_root):
    for t in (-2.0, 11.0):
        _write_eval(pred_root, dict(_base(), target_return=t))
        assert _avail(auth_client) == "unavailable", f"target={t}"


# ---------------------------------------------------------------------- #
# gate_status 派生（不信任输入 passed）
# ---------------------------------------------------------------------- #

def test_input_passed_but_metrics_fail_returns_not_passed(auth_client, pred_root):
    # 输入声称 passed，但 accuracy 仅 0.55（<0.80）
    _write_eval(pred_root, dict(_base(), accuracy=0.55, gate_status="passed"))
    body = auth_client.get("/api/research/prediction-summary").json()
    assert body["availability"] == "fresh"
    assert body["data"]["gate_status"] == "not_passed"
    assert body["data"]["gate_version"] == "gate-v1"
    assert any("样本外准确率" in r for r in body["data"]["gate_reasons"])


def test_all_thresholds_met_returns_passed(auth_client, pred_root):
    _write_eval(pred_root, _base())  # acc 0.85 / prec 0.75 / rec 0.60 / sample 1200 / net>bench / mdd -0.12
    body = auth_client.get("/api/research/prediction-summary").json()
    assert body["availability"] == "fresh"
    assert body["data"]["gate_status"] == "passed"
    assert body["data"]["gate_reasons"] == []


def test_sample_count_insufficient_gate(auth_client, pred_root):
    # 30 <= sample < 1000：可展示但 gate=insufficient_data
    _write_eval(pred_root, dict(_base(), sample_count=500))
    body = auth_client.get("/api/research/prediction-summary").json()
    assert body["availability"] == "fresh"
    assert body["data"]["gate_status"] == "insufficient_data"
    assert any("样本数不足" in r for r in body["data"]["gate_reasons"])


def test_ratio_fields_not_scaled_by_backend(auth_client, pred_root):
    _write_eval(pred_root, _base())  # net_return=0.08, max_drawdown=-0.12, benchmark_return=0.03
    body = auth_client.get("/api/research/prediction-summary").json()
    d = body["data"]
    assert d["net_return"] == 0.08
    assert d["max_drawdown"] == -0.12
    assert d["benchmark_return"] == 0.03
    assert d["target_return"] == 0.02


# ---------------------------------------------------------------------- #
# fresh / stale（动态时间）
# ---------------------------------------------------------------------- #

def test_fresh_result(auth_client, pred_root):
    _write_eval(pred_root, _base())
    body = auth_client.get("/api/research/prediction-summary").json()
    assert body["availability"] == "fresh"
    assert body["cache_status"] == "fresh"
    d = body["data"]
    assert d["model_version"] == "v1.0"
    assert d["task_name"] == "未来 5 个交易日上涨 >=2%"
    assert d["horizon_days"] == 5
    assert d["target_return"] == 0.02
    assert d["accuracy"] == 0.85
    assert d["sample_count"] == 1200
    assert d["test_start"] == "2026-01-01"
    assert d["test_end"] == "2026-06-30"
    assert d["gate_status"] == "passed"


def test_stale_result(auth_client, pred_root):
    _write_eval(pred_root, dict(_base(), evaluated_at=STALE_EVAL))
    body = auth_client.get("/api/research/prediction-summary").json()
    assert body["availability"] == "stale"
    assert body["cache_status"] == "stale"
    assert body["data"] is not None
    assert body["data"]["accuracy"] == 0.85


# ---------------------------------------------------------------------- #
# 只读 / 脱敏
# ---------------------------------------------------------------------- #

def test_read_only_no_files_changed(auth_client, pred_root):
    _write_eval(pred_root, _base())
    before = _tree_fingerprint(pred_root)
    auth_client.get("/api/research/prediction-summary")
    after = _tree_fingerprint(pred_root)
    assert before == after


def test_no_leak_of_path_token_stack(auth_client, pred_root):
    _write_eval(pred_root, "{ broken json")
    r = auth_client.get("/api/research/prediction-summary")
    text = r.text
    assert str(pred_root).replace("\\", "/") not in text
    assert "Traceback" not in text
    assert "token" not in text.lower()
    assert "latest.json" not in text
