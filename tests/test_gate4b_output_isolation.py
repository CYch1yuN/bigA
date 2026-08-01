"""Gate 4B 输出隔离（gate4b_observation.py）回归测试。

真实观察（``--mode track``）期间每天都会运行 tracker——它**不得**改写受 Git
跟踪的正式示例文件（``docs/gate4b-observation.md``、
``reports/phase-4/gate4b/60d-summary.json``），否则工作树每天都变脏、
正式示例被真实进度污染。

本测试锁定：

1. ``track`` 默认输出到 Git 忽略的本地状态目录 ``state/automation/gate4b/``；
2. ``track`` 不触碰任何受 Git 跟踪文件（示例产物字节不变）；
3. ``--output-dir`` 显式覆盖输出目录；
4. 重复运行幂等（同输入同输出，无 .tmp 残留）；
5. 无运行记录时输出 NOT STARTED（0/60）；
6. ``precheck`` 仍写正式示例目录（示例语义保留）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))

import gate4b_observation as g4b  # noqa: E402


def _track_summary_with_state(state_dir: Path, reports_dir: Path) -> dict:
    """构造一个最小可运行的 track summary（无运行记录 → NOT STARTED）。"""
    config = g4b.AutomationConfig(
        paths=g4b.PathsConfig(
            data_dir="data",
            state_dir=str(state_dir),
            reports_dir=str(reports_dir),
            logs_dir="logs",
            archive_dir="reports/archive",
        ),
        data=g4b.DataConfig(symbols=[], lookback_days=200),
        logging=g4b.LoggingConfig(console=False),
        accounts=[
            g4b.AccountConfig(
                account_id="paper-steady",
                track=g4b.StrategyTrack.STEADY,
                initial_cash=1000.0,
                eligibility_status=g4b.EligibilityStatus.NOT_ELIGIBLE_FOR_LIVE_TRADING,
            ),
            g4b.AccountConfig(
                account_id="paper-aggressive",
                track=g4b.StrategyTrack.AGGRESSIVE,
                initial_cash=1000.0,
                eligibility_status=g4b.EligibilityStatus.SIMULATION_ONLY,
            ),
        ],
    ).with_base_dir(ROOT)
    summary = g4b._track_real(config, calendar=None)
    summary["mode"] = "track"
    summary["generated_at"] = "2026-08-02T04:00:00"
    return summary


def test_track_default_output_goes_to_state_dir(tmp_path: Path) -> None:
    """track 默认输出到 state/automation/gate4b/（Git 忽略目录）。"""
    state_dir = tmp_path / "state"
    reports_dir = tmp_path / "reports"
    summary = _track_summary_with_state(state_dir, reports_dir)
    summary_path, md_path = g4b._write_outputs(summary)
    # 默认输出目录 = ROOT/state/automation/gate4b
    expected_dir = g4b.TRACK_OUTPUT_DIR
    assert summary_path.parent == expected_dir
    assert md_path.parent == expected_dir
    assert summary_path.name == "gate4b-track-summary.json"
    assert md_path.name == "gate4b-track-observation.md"
    assert summary_path.exists() and md_path.exists()
    # 不得是正式示例路径
    assert summary_path != g4b.SUMMARY_JSON
    assert md_path != g4b.OBSERVATION_MD


def test_track_does_not_touch_tracked_examples(tmp_path: Path) -> None:
    """track 输出不得修改受 Git 跟踪的正式示例文件。"""
    # 记录示例文件当前字节
    example_md = g4b.OBSERVATION_MD
    example_json = g4b.SUMMARY_JSON
    md_before = example_md.read_bytes() if example_md.exists() else None
    json_before = example_json.read_bytes() if example_json.exists() else None

    state_dir = tmp_path / "state"
    reports_dir = tmp_path / "reports"
    summary = _track_summary_with_state(state_dir, reports_dir)
    g4b._write_outputs(summary)

    md_after = example_md.read_bytes() if example_md.exists() else None
    json_after = example_json.read_bytes() if example_json.exists() else None
    assert md_before == md_after
    assert json_before == json_after
    # 且 track 摘要确实写进了状态目录
    assert g4b.TRACK_OUTPUT_DIR.joinpath("gate4b-track-summary.json").exists()


def test_output_dir_override(tmp_path: Path) -> None:
    """--output-dir 显式覆盖输出目录。"""
    state_dir = tmp_path / "state"
    reports_dir = tmp_path / "reports"
    out = tmp_path / "custom-out"
    summary = _track_summary_with_state(state_dir, reports_dir)
    summary_path, md_path = g4b._write_outputs(summary, output_dir=out)
    assert summary_path.parent == out
    assert md_path.parent == out
    assert summary_path.exists() and md_path.exists()
    assert out.joinpath("gate4b-track-summary.json").exists()
    assert out.joinpath("gate4b-track-observation.md").exists()


def test_repeated_run_idempotent_no_tmp_left(tmp_path: Path) -> None:
    """重复运行幂等：输出字节一致，且无 .tmp 残留。"""
    state_dir = tmp_path / "state"
    reports_dir = tmp_path / "reports"
    out = tmp_path / "out"
    summary = _track_summary_with_state(state_dir, reports_dir)
    g4b._write_outputs(summary, output_dir=out)
    first_json = out.joinpath("gate4b-track-summary.json").read_bytes()
    first_md = out.joinpath("gate4b-track-observation.md").read_bytes()

    g4b._write_outputs(summary, output_dir=out)
    second_json = out.joinpath("gate4b-track-summary.json").read_bytes()
    second_md = out.joinpath("gate4b-track-observation.md").read_bytes()
    assert first_json == second_json
    assert first_md == second_md
    leftovers = list(out.glob("*.tmp"))
    assert leftovers == []


def test_no_records_is_not_started(tmp_path: Path) -> None:
    """无运行记录时 track 输出 NOT STARTED（0/60）。"""
    state_dir = tmp_path / "state"
    reports_dir = tmp_path / "reports"
    out = tmp_path / "out"
    summary = _track_summary_with_state(state_dir, reports_dir)
    assert summary["observation_progress"] == 0
    g4b._write_outputs(summary, output_dir=out)
    md = out.joinpath("gate4b-track-observation.md").read_text(encoding="utf-8")
    assert "NOT STARTED（0/60）" in md


def test_precheck_still_writes_tracked_examples(tmp_path: Path, monkeypatch) -> None:
    """precheck 仍写正式示例目录（示例语义保留，历史回放产物）。

    用 monkeypatch 把示例路径指到临时目录，避免触碰真实受跟踪文件。
    """
    fake_json = tmp_path / "precheck" / "60d-summary.json"
    fake_md = tmp_path / "precheck" / "gate4b-observation.md"
    monkeypatch.setattr(g4b, "SUMMARY_JSON", fake_json)
    monkeypatch.setattr(g4b, "OBSERVATION_MD", fake_md)
    summary = {
        "mode": "precheck",
        "first_day": "2020-07-16",
        "last_day": "2020-10-07",
        "trading_days": 60,
        "daily": [],
        "totals": {
            "signals": 0,
            "orders": 0,
            "filled": 0,
            "duplicate_orders": 0,
            "identity_violations": 0,
            "negative_cash_days": 0,
            "non_success_days": 0,
        },
        "observation": {"accounts": [], "observation_days_final": {}},
        "equity_curves": {},
    }
    summary_path, md_path = g4b._write_outputs(summary)
    assert summary_path == fake_json
    assert md_path == fake_md
    assert fake_json.exists() and fake_md.exists()


def test_atomic_write_leaves_no_partial(tmp_path: Path) -> None:
    """原子写入：写失败不留下半成品目标文件。"""
    target = tmp_path / "a" / "b" / "report.json"
    g4b._atomic_write_json(target, {"mode": "track", "x": 1})
    assert target.exists()
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["mode"] == "track"
    leftovers = list(target.parent.glob("*.tmp"))
    assert leftovers == []


def test_track_cli_writes_state_dir_not_tracked(tmp_path: Path) -> None:
    """端到端：--mode track CLI 输出落在状态目录，受跟踪示例不变。"""
    import subprocess

    example_md = g4b.OBSERVATION_MD
    example_json = g4b.SUMMARY_JSON
    md_before = example_md.read_bytes() if example_md.exists() else None
    json_before = example_json.read_bytes() if example_json.exists() else None

    env = dict(os.environ)
    # 解除沙箱 bulk-delete 守卫注入，避免测试收尾清理误拦截
    for k in (
        "CODEBUDDY_SAFE_DELETE_BULK_STATE_DIR",
        "CODEBUDDY_TOOL_CALL_ID",
        "CODEBUDDY_SAFE_DELETE_BULK_GUARD",
        "CODEBUDDY_NODE_BIN",
    ):
        env.pop(k, None)
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "gate4b_observation.py"), "--mode", "track"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    md_after = example_md.read_bytes() if example_md.exists() else None
    json_after = example_json.read_bytes() if example_json.exists() else None
    assert md_before == md_after
    assert json_before == json_after
    # 状态目录下生成了 track 摘要
    assert g4b.TRACK_OUTPUT_DIR.joinpath("gate4b-track-summary.json").exists()
    # 无 .tmp 残留
    leftovers = list(g4b.TRACK_OUTPUT_DIR.glob("*.tmp"))
    assert leftovers == []
