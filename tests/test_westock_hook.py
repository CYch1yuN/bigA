"""westock 每日挂接（hook）离线测试。

全部使用 mock，不访问 westock-mcp / 公网。覆盖：
- 严格旁路：hook 内部异常 / fetcher 异常一律不冒泡
- 连续异常累计与升级（consecutive_days）
- 报告落盘（reports/validation/westock_<date>.json）
- 状态文件读写（state/validators/westock.json）
- daily pipeline 集成：旁路步骤不影响主流程状态
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from ashare_quant.automation.westock_hook import (
    WestockHookResult,
    WestockValidationHook,
)
from ashare_quant.validators import AVAILABLE, NO_DATA, UNAVAILABLE


def _quotes(symbol="600519"):
    return pd.DataFrame(
        {
            "symbol": [symbol],
            "trade_date": pd.to_datetime(["2026-08-03"]),
            "open_raw": [1500.0],
            "close_raw": [1505.0],
            "volume": [30000.0],
            "amount": [4.5e9],
        }
    )


def _westock_same():
    """与主源一致的 westock 数据。"""
    return pd.DataFrame(
        {
            "date": ["2026-08-03"],
            "last": [1505.0],
            "volume": [30000.0],
            "amount": [4.5e9],
        }
    )


def _westock_diff():
    """收盘价 +5% 的 westock 数据（超 2% 阈值）。"""
    return pd.DataFrame(
        {
            "date": ["2026-08-03"],
            "last": [1505.0 * 1.05],
            "volume": [30000.0],
            "amount": [4.5e9],
        }
    )


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return tmp_path / "state"


@pytest.fixture
def reports_dir(tmp_path: Path) -> Path:
    return tmp_path / "reports"


class TestStrictBypass:
    def test_hook_never_raises_on_fetcher_error(self, state_dir, reports_dir):
        def boom(*_):
            raise RuntimeError("MCP down")

        hook = WestockValidationHook(
            fetch_quotes=boom,
            state_dir=state_dir,
            reports_dir=reports_dir,
        )
        result = hook.run(_quotes(), symbol="600519", start=date(2026, 8, 3), end=date(2026, 8, 3), as_of=date(2026, 8, 3))
        assert isinstance(result, WestockHookResult)
        assert result.status == UNAVAILABLE
        assert "fail-open" in result.message or "unavailable" in result.status

    def test_hook_never_raises_on_no_fetcher(self, state_dir, reports_dir):
        hook = WestockValidationHook(state_dir=state_dir, reports_dir=reports_dir)
        result = hook.run(_quotes(), symbol="600519", start=date(2026, 8, 3), end=date(2026, 8, 3), as_of=date(2026, 8, 3))
        assert result.status in (UNAVAILABLE, NO_DATA)

    def test_hook_never_raises_on_empty_quotes(self, state_dir, reports_dir):
        hook = WestockValidationHook(state_dir=state_dir, reports_dir=reports_dir)
        result = hook.run(pd.DataFrame(), symbol="600519", start=date(2026, 8, 3), end=date(2026, 8, 3), as_of=date(2026, 8, 3))
        assert result.status in (UNAVAILABLE, NO_DATA)

    def test_identical_data_ok(self, state_dir, reports_dir):
        hook = WestockValidationHook(
            fetch_quotes=lambda *_: _westock_same(),
            state_dir=state_dir,
            reports_dir=reports_dir,
        )
        result = hook.run(_quotes(), symbol="600519", start=date(2026, 8, 3), end=date(2026, 8, 3), as_of=date(2026, 8, 3))
        assert result.status == AVAILABLE
        assert result.issues_count == 0
        assert result.escalated is False


class TestConsecutiveEscalation:
    def test_diff_bumps_consecutive(self, state_dir, reports_dir):
        hook = WestockValidationHook(
            fetch_quotes=lambda *_: _westock_diff(),
            state_dir=state_dir,
            reports_dir=reports_dir,
            consecutive_days=3,
        )
        r1 = hook.run(_quotes(), symbol="600519", start=date(2026, 8, 3), end=date(2026, 8, 3), as_of=date(2026, 8, 3))
        assert r1.consecutive_anomaly_days == 1
        r2 = hook.run(_quotes(), symbol="600519", start=date(2026, 8, 4), end=date(2026, 8, 4), as_of=date(2026, 8, 4))
        assert r2.consecutive_anomaly_days == 2
        r3 = hook.run(_quotes(), symbol="600519", start=date(2026, 8, 5), end=date(2026, 8, 5), as_of=date(2026, 8, 5))
        assert r3.consecutive_anomaly_days == 3
        assert r3.escalated is True  # 达到 consecutive_days → 升级

    def test_same_day_no_double_count(self, state_dir, reports_dir):
        hook = WestockValidationHook(
            fetch_quotes=lambda *_: _westock_diff(),
            state_dir=state_dir,
            reports_dir=reports_dir,
            consecutive_days=3,
        )
        hook.run(_quotes(), symbol="600519", start=date(2026, 8, 3), end=date(2026, 8, 3), as_of=date(2026, 8, 3))
        r2 = hook.run(_quotes(), symbol="600519", start=date(2026, 8, 3), end=date(2026, 8, 3), as_of=date(2026, 8, 3))
        assert r2.consecutive_anomaly_days == 1  # 同日不重复累计

    def test_clean_day_resets_consecutive(self, state_dir, reports_dir):
        def alternating(_sym, _s, _e):
            return _westock_same()  # 第一次后全部一致

        hook = WestockValidationHook(
            fetch_quotes=alternating,
            state_dir=state_dir,
            reports_dir=reports_dir,
        )
        # 先产生一天差异
        bad_hook = WestockValidationHook(
            fetch_quotes=lambda *_: _westock_diff(),
            state_dir=state_dir,
            reports_dir=reports_dir,
        )
        bad_hook.run(_quotes(), symbol="600519", start=date(2026, 8, 3), end=date(2026, 8, 3), as_of=date(2026, 8, 3))
        # 再跑一致日 → 清零
        r = hook.run(_quotes(), symbol="600519", start=date(2026, 8, 4), end=date(2026, 8, 4), as_of=date(2026, 8, 4))
        assert r.consecutive_anomaly_days == 0
        assert r.issues_count == 0

    def test_state_file_written(self, state_dir, reports_dir):
        hook = WestockValidationHook(
            fetch_quotes=lambda *_: _westock_diff(),
            state_dir=state_dir,
            reports_dir=reports_dir,
        )
        hook.run(_quotes(), symbol="600519", start=date(2026, 8, 3), end=date(2026, 8, 3), as_of=date(2026, 8, 3))
        state_file = state_dir / "validators" / "westock.json"
        assert state_file.is_file()
        data = json.loads(state_file.read_text(encoding="utf-8"))
        assert data["consecutive"] == 1
        assert data["last_date"] == "2026-08-03"


class TestReportArtifacts:
    def test_report_written(self, state_dir, reports_dir):
        hook = WestockValidationHook(
            fetch_quotes=lambda *_: _westock_diff(),
            state_dir=state_dir,
            reports_dir=reports_dir,
        )
        result = hook.run(_quotes(), symbol="600519", start=date(2026, 8, 3), end=date(2026, 8, 3), as_of=date(2026, 8, 3))
        assert result.report_path is not None
        report_file = Path(result.report_path)
        assert report_file.is_file()
        payload = json.loads(report_file.read_text(encoding="utf-8"))
        assert payload["as_of"] == "2026-08-03"
        assert payload["status"] == AVAILABLE
        assert payload["issues_count"] == 1
        # 留档字段
        assert payload["primary_content_hash"]
        assert payload["westock_content_hash"]
        assert payload["request_params"]["adjust"] == "raw"

    def test_no_reports_dir_skips_report(self, state_dir):
        hook = WestockValidationHook(
            fetch_quotes=lambda *_: _westock_same(),
            state_dir=state_dir,
            reports_dir=None,
        )
        result = hook.run(_quotes(), symbol="600519", start=date(2026, 8, 3), end=date(2026, 8, 3), as_of=date(2026, 8, 3))
        assert result.report_path is None


class TestDailyPipelineIntegration:
    def test_daily_pipeline_accepts_hook(self):
        """DailyPipeline dataclass 能携带 westock_hook 字段且默认 None。"""
        from ashare_quant.automation.daily import DailyPipeline

        p = DailyPipeline()
        assert p.westock_hook is None
        p2 = DailyPipeline(westock_hook=WestockValidationHook())
        assert p2.westock_hook is not None

    def test_validators_config_parses(self):
        from ashare_quant.automation.config import AutomationConfig

        cfg = AutomationConfig(validators={"enabled": ["westock"], "consecutive_days": 5})
        assert cfg.validators.westock_enabled is True
        assert cfg.validators.consecutive_days == 5
        cfg2 = AutomationConfig()
        assert cfg2.validators.westock_enabled is False
