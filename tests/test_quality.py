"""数据质量检查测试：覆盖 10 类检查与严重等级。"""
from __future__ import annotations

import pandas as pd

from ashare_quant.config import SEVERITY_CRITICAL
from ashare_quant.quality import QualityChecker
from ashare_quant.samples import (
    make_cross_source_conflict_raw,
    make_delisted_master,
    make_duplicate_raw,
    make_missing_day_raw,
    make_negative_volume_raw,
    make_normal_raw,
    make_ohlc_error_raw,
    make_price_jump_raw,
    make_status_contradiction_master,
    make_trade_calendar,
)
from ashare_quant.standardize import Standardizer
from datetime import date


def _run(config, raw, source="akshare", sm=None, cal=None, other=None):
    std = Standardizer()
    curated = std.standardize_daily_quotes(raw, source)
    if other is not None:
        other_curated = std.standardize_daily_quotes(other, "baostock")
    else:
        other_curated = None
    checker = QualityChecker(config)
    return checker.run(curated, sm, cal, other_curated)


def test_normal_data_no_critical(config, trade_calendar):
    report = _run(config, make_normal_raw("000001", 30), cal=trade_calendar)
    assert not report.has_critical
    assert report.exit_code == 0


def test_duplicate_primary_key_critical(config, trade_calendar):
    report = _run(config, make_duplicate_raw("000002"), cal=trade_calendar)
    dups = [i for i in report.issues if i.check == "duplicate_primary_key"]
    assert len(dups) > 0
    assert report.has_critical
    assert report.exit_code == 1


def test_missing_trade_day_critical(config):
    cal = make_trade_calendar(date(2024, 1, 2), date(2024, 2, 15))
    report = _run(config, make_missing_day_raw("000003"), cal=cal)
    missing = [i for i in report.issues if i.check == "missing_trade_day"]
    assert len(missing) > 0
    assert report.has_critical


def test_ohlc_error_critical(config, trade_calendar):
    report = _run(config, make_ohlc_error_raw("000005"), cal=trade_calendar)
    ohlc = [i for i in report.issues if i.check == "ohlc_relation"]
    assert len(ohlc) > 0
    assert report.has_critical


def test_negative_volume_critical(config, trade_calendar):
    report = _run(config, make_negative_volume_raw("000006"), cal=trade_calendar)
    neg = [i for i in report.issues if i.check == "negative_volume"]
    assert len(neg) > 0
    assert report.has_critical


def test_abnormal_price_jump_warning(config, trade_calendar):
    report = _run(config, make_price_jump_raw("000007"), cal=trade_calendar)
    jumps = [i for i in report.issues if i.check == "abnormal_price_jump"]
    assert len(jumps) > 0
    # 异常跳变为 warning，不阻止下游
    assert all(i.severity != SEVERITY_CRITICAL for i in jumps)


def test_cross_source_conflict(config, trade_calendar):
    base = make_normal_raw("000008", 10)
    other = make_cross_source_conflict_raw("000008")
    report = _run(config, base, other=other, cal=trade_calendar)
    cross = [i for i in report.issues if i.check == "cross_source"]
    assert len(cross) > 0


def test_status_contradiction_delist_before_list(config, trade_calendar):
    sm = make_status_contradiction_master()
    raw = make_normal_raw("000099", 10)
    report = _run(config, raw, sm=sm, cal=trade_calendar)
    contra = [i for i in report.issues if i.check == "status_contradiction"]
    assert any("退市日早于上市日" in i.description for i in contra)


def test_suspended_with_volume_contradiction(config, trade_calendar):
    """停牌标记为 True 但成交量>0 应触发状态矛盾。"""
    from ashare_quant.samples import make_suspended_raw

    raw = make_suspended_raw("000004")
    std = Standardizer()
    curated = std.standardize_daily_quotes(raw, "akshare")
    # 人为破坏：停牌日设为有成交量
    curated.loc[curated["is_suspended"], "volume"] = 99999
    checker = QualityChecker(config)
    report = checker.run(curated, trade_calendar=trade_calendar)
    contra = [i for i in report.issues if i.check == "status_contradiction"]
    assert len(contra) > 0


def test_report_formats(config, trade_calendar):
    report = _run(config, make_normal_raw("000001", 10), cal=trade_calendar)
    js = report.to_json()
    md = report.to_markdown()
    assert '"summary"' in js
    assert "数据质量检查报告" in md
    assert "退出码" in md


def test_empty_data_critical(config):
    checker = QualityChecker(config)
    report = checker.run(pd.DataFrame())
    assert report.has_critical
    assert report.exit_code == 1


def test_thresholds_from_config_not_hardcoded(config):
    """阈值来自 YAML 配置。"""
    checker = QualityChecker(config)
    summary = checker._config_summary()
    assert "abnormal_price_jump" in summary
    assert "abs_return_threshold" in summary["abnormal_price_jump"]
    assert "cross_source" in summary
    assert "close_relative_tolerance" in summary["cross_source"]
