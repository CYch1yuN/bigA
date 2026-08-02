"""westock 旁路校验器离线测试。

全部使用 mock 数据，不访问 westock-mcp / 公网。
覆盖：
- 正常核验通过（差异低于阈值）
- 收盘价/成交量/成交额超阈值 → warning
- fail-open：fetcher 返回 None / 抛异常 → unavailable，不抛错
- 无重叠数据 → no_data / 无重叠告警
- 日历校验一致与不一致
- 留档：请求参数、响应摘要、内容哈希
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from ashare_quant.config import default_config_path, load_config
from ashare_quant.validators import (
    AVAILABLE,
    NO_DATA,
    UNAVAILABLE,
    ValidationResult,
    WestockValidator,
)


def _primary_df(dates=("2024-01-02", "2024-01-03", "2024-01-04")):
    return pd.DataFrame(
        {
            "symbol": ["600519"] * len(dates),
            "trade_date": pd.to_datetime(dates),
            "open_raw": [1500.0, 1510.0, 1520.0],
            "close_raw": [1505.0, 1515.0, 1525.0],
            "volume": [30000.0, 32000.0, 31000.0],
            "amount": [4.5e9, 4.8e9, 4.7e9],
        }
    )


def _westock_df(dates=("2024-01-02", "2024-01-03", "2024-01-04"), close_offset=0.0):
    """westock 原生列名（close/last/volume/amount）与日期列 date。"""
    closes = [1505.0 + close_offset, 1515.0 + close_offset, 1525.0 + close_offset]
    return pd.DataFrame(
        {
            "date": dates,
            "open": [1500.0, 1510.0, 1520.0],
            "last": closes,
            "high": [1520.0, 1530.0, 1540.0],
            "low": [1490.0, 1500.0, 1510.0],
            "volume": [30000.0, 32000.0, 31000.0],
            "amount": [4.5e9, 4.8e9, 4.7e9],
        }
    )


@pytest.fixture
def validator():
    return WestockValidator()


@pytest.fixture
def config():
    return load_config(default_config_path())


class TestCrossCheck:
    def test_identical_data_passes(self, validator):
        result = validator.validate(_primary_df(), "600519", date(2024, 1, 2), date(2024, 1, 4), fetch=lambda *_: _westock_df())
        assert result.status == AVAILABLE
        assert result.issues == []
        assert "通过" in result.message

    def test_close_diff_exceeds_tolerance(self, validator):
        # 收盘价偏移 5% → 超 2% 阈值
        w = _westock_df(close_offset=1505.0 * 0.05)
        result = validator.validate(_primary_df(), "600519", date(2024, 1, 2), date(2024, 1, 4), fetch=lambda *_: w)
        close_issues = [i for i in result.issues if i["check"] == "cross_source_close"]
        assert len(close_issues) == 3
        assert all(i["severity"] == "warning" for i in close_issues)
        assert close_issues[0]["details"]["relative_diff"] > 0.02

    def test_volume_diff_exceeds_tolerance(self, validator):
        w = _westock_df()
        w["volume"] = w["volume"] * 1.5  # +50% → 超 10%
        result = validator.validate(_primary_df(), "600519", date(2024, 1, 2), date(2024, 1, 4), fetch=lambda *_: w)
        vol_issues = [i for i in result.issues if i["check"] == "cross_source_volume"]
        assert len(vol_issues) == 3

    def test_amount_diff_exceeds_tolerance(self, validator):
        w = _westock_df()
        w["amount"] = w["amount"] * 0.8  # -20% → 超 10%
        result = validator.validate(_primary_df(), "600519", date(2024, 1, 2), date(2024, 1, 4), fetch=lambda *_: w)
        amt_issues = [i for i in result.issues if i["check"] == "cross_source_amount"]
        assert len(amt_issues) == 3

    def test_merged_column_suffixes_correct(self, validator):
        w = _westock_df()
        result = validator.validate(_primary_df(), "600519", date(2024, 1, 2), date(2024, 1, 4), fetch=lambda *_: w)
        assert result.status == AVAILABLE


class TestFailOpen:
    def test_fetcher_returns_none_is_no_data(self, validator):
        result = validator.validate(_primary_df(), "600519", date(2024, 1, 2), date(2024, 1, 4), fetch=lambda *_: None)
        assert result.status == NO_DATA
        assert result.issues == []

    def test_fetcher_returns_empty_df(self, validator):
        result = validator.validate(_primary_df(), "600519", date(2024, 1, 2), date(2024, 1, 4), fetch=lambda *_: pd.DataFrame())
        assert result.status == NO_DATA

    def test_fetcher_raises_does_not_propagate(self, validator):
        def boom(*_):
            raise RuntimeError("MCP 连接器不可用")

        result = validator.validate(_primary_df(), "600519", date(2024, 1, 2), date(2024, 1, 4), fetch=boom)
        assert result.status == UNAVAILABLE
        assert "fail-open" in result.message
        # 关键：不抛异常

    def test_no_fetcher_configured(self):
        v = WestockValidator()  # 未注入 fetcher
        result = v.validate(_primary_df(), "600519", date(2024, 1, 2), date(2024, 1, 4))
        assert result.status == UNAVAILABLE

    def test_no_overlap_dates(self, validator):
        w = _westock_df()
        w["date"] = ["2024-05-01", "2024-05-02", "2024-05-03"]
        result = validator.validate(_primary_df(), "600519", date(2024, 1, 2), date(2024, 1, 4), fetch=lambda *_: w)
        assert result.status == AVAILABLE
        assert any("无重叠交易日" in i["description"] for i in result.issues)


class TestAuditTrail:
    def test_request_params_recorded(self, validator):
        result = validator.validate(_primary_df(), "600519", date(2024, 1, 2), date(2024, 1, 4), fetch=lambda *_: _westock_df())
        assert result.request_params["symbol"] == "600519"
        assert result.request_params["adjust"] == "raw"
        assert "fq=qfq" in result.request_params["note"]  # 复权缺陷留档
        assert result.start_date == "2024-01-02"
        assert result.end_date == "2024-01-04"

    def test_content_hashes_recorded(self, validator):
        primary = _primary_df()
        w = _westock_df()
        result = validator.validate(primary, "600519", date(2024, 1, 2), date(2024, 1, 4), fetch=lambda *_: w)
        assert len(result.primary_content_hash) == 64  # sha256 hex
        assert len(result.westock_content_hash) == 64
        assert result.primary_content_hash != result.westock_content_hash

    def test_response_summary(self, validator):
        result = validator.validate(_primary_df(), "600519", date(2024, 1, 2), date(2024, 1, 4), fetch=lambda *_: _westock_df())
        assert result.response_summary["rows"] == 3
        assert "last" in result.response_summary["columns"]

    def test_to_dict_and_json_roundtrip(self, validator):
        result = validator.validate(_primary_df(), "600519", date(2024, 1, 2), date(2024, 1, 4), fetch=lambda *_: _westock_df())
        d = result.to_dict()
        assert d["status"] == AVAILABLE
        j = result.to_json()
        assert '"status"' in j


class TestCalendar:
    def _cal(self, dates):
        return pd.DataFrame({"trade_date": pd.to_datetime(list(dates))})

    def test_calendar_identical(self, validator):
        r = validator.validate_calendar(
            self._cal(["2024-01-02", "2024-01-03"]),
            self._cal(["2024-01-02", "2024-01-03"]),
        )
        assert r.status == AVAILABLE
        assert r.calendar_diffs == []
        assert "一致" in r.message

    def test_calendar_diff(self, validator):
        r = validator.validate_calendar(
            self._cal(["2024-01-02", "2024-01-03"]),
            self._cal(["2024-01-02", "2024-01-04"]),
        )
        assert r.status == AVAILABLE
        assert len(r.calendar_diffs) == 2

    def test_calendar_fail_open(self, validator):
        def boom(*_):
            raise ConnectionError("MCP 不可用")

        r = validator.validate_calendar(
            self._cal(["2024-01-02"]), pd.DataFrame(), fetch_calendar=boom
        )
        assert r.status == UNAVAILABLE


class TestConfigIntegration:
    def test_providers_validators_from_yaml(self, config):
        assert config.providers.validators == ["westock"]
        assert config.providers.primary == "akshare"
        assert config.providers.fallback == "baostock"

    def test_westock_rule_from_yaml(self, config):
        rule = config.quality_rule("westock_cross_source")
        assert rule.severity == "warning"
        assert rule.params["close_relative_tolerance"] == 0.02
        assert rule.params["volume_relative_tolerance"] == 0.10
        assert rule.params["consecutive_days"] == 3

    def test_validator_matches_config_thresholds(self, config):
        rule = config.quality_rule("westock_cross_source")
        v = WestockValidator(
            close_tolerance=rule.params["close_relative_tolerance"],
            volume_tolerance=rule.params["volume_relative_tolerance"],
            amount_tolerance=rule.params["amount_relative_tolerance"],
        )
        assert v.close_tolerance == 0.02
        assert v.volume_tolerance == 0.10


class TestImports:
    def test_module_exports(self):
        assert callable(WestockValidator)
        assert ValidationResult is not None
        assert AVAILABLE == "available"
        assert UNAVAILABLE == "unavailable"
        assert NO_DATA == "no_data"


# ---------------------------------------------------------------------- #
# response_summary 日期列读取（禁止依赖列顺序）
# ---------------------------------------------------------------------- #

def test_summarize_picks_date_col_when_code_is_first():
    """code 列位于 date 列之前时，仍必须按列名取日期，不得用首列冒充。"""
    df = pd.DataFrame(
        {
            "code": ["sh600519", "sh600519"],
            "date": ["2026-07-30", "2026-07-31"],
            "last": [1361.76, 1350.6],
        }
    )
    s = WestockValidator._summarize(df)
    assert s["date_min"] == "2026-07-30"
    assert s["date_max"] == "2026-07-31"
    assert "summary_error" not in s


def test_summarize_handles_trade_date_alias():
    df = pd.DataFrame({"trade_date": ["2026-07-31"], "close": [1350.6]})
    s = WestockValidator._summarize(df)
    assert s["date_min"] == "2026-07-31"
    assert s["date_max"] == "2026-07-31"


def test_summarize_missing_date_col_does_not_use_symbol():
    """无 date/trade_date 列：date_min/max 必须为 None 并记录 summary_error，
    绝不允许拿 code/symbol 列冒充日期。"""
    df = pd.DataFrame({"code": ["sh600519"], "last": [1350.6]})
    s = WestockValidator._summarize(df)
    assert s["date_min"] is None
    assert s["date_max"] is None
    assert "summary_error" in s
    assert "缺少 date/trade_date 列" in s["summary_error"]


def test_summarize_empty_df():
    s = WestockValidator._summarize(pd.DataFrame())
    assert s["rows"] == 0
    assert s["date_min"] is None and s["date_max"] is None
