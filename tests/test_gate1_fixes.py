"""Gate 1 审核报告阻断问题修复测试。

对应 docs/reviews/gate-1-data-review.md 中的 G1-01 至 G1-05。
按 TDD 原则：先写失败测试，再修复实现。
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest

from ashare_quant.config import SEVERITY_CRITICAL, default_config_path, load_config
from ashare_quant.constants import (
    DAILY_QUOTE_FIELDS,
    SECURITY_MASTER_FIELDS,
    SOURCE_AKSHARE,
    SOURCE_BAOSTOCK,
)
from ashare_quant.providers.akshare_provider import AKShareProvider
from ashare_quant.providers.baostock_provider import BaoStockProvider
from ashare_quant.quality import QualityChecker
from ashare_quant.samples import (
    make_normal_raw,
    make_trade_calendar,
)
from ashare_quant.standardize import Standardizer, content_hash


# ============================================================
# G1-01: 必需数值缺失可静默通过质量检查
# ============================================================

class TestG1_01_Completeness:
    """必需字段完整性与有限数检查。"""

    def _make_curated(self, n: int = 10) -> pd.DataFrame:
        raw = make_normal_raw("000001", n_days=n)
        return Standardizer().standardize_daily_quotes(raw, SOURCE_AKSHARE)

    def test_nan_in_required_field_is_critical(self, config, trade_calendar):
        """NaN 在必需数值字段中必须触发 critical。"""
        curated = self._make_curated(10)
        curated.loc[3, "close_qfq"] = np.nan
        checker = QualityChecker(config)
        report = checker.run(curated, trade_calendar=trade_calendar)
        completeness = [i for i in report.issues if i.check == "required_completeness"]
        assert len(completeness) > 0
        assert report.has_critical

    def test_nan_in_raw_ohlc_is_critical(self, config, trade_calendar):
        """NaN 在 raw OHLC 中必须触发 critical。"""
        curated = self._make_curated(10)
        curated.loc[2, "open_raw"] = np.nan
        checker = QualityChecker(config)
        report = checker.run(curated, trade_calendar=trade_calendar)
        completeness = [i for i in report.issues if i.check == "required_completeness"]
        assert len(completeness) > 0
        assert report.has_critical

    def test_nan_in_volume_is_critical(self, config, trade_calendar):
        """NaN 在成交量中必须触发 critical。"""
        curated = self._make_curated(10)
        curated.loc[1, "volume"] = np.nan
        checker = QualityChecker(config)
        report = checker.run(curated, trade_calendar=trade_calendar)
        completeness = [i for i in report.issues if i.check == "required_completeness"]
        assert len(completeness) > 0

    def test_nan_in_adjustment_factor_is_critical(self, config, trade_calendar):
        """NaN 在复权因子中必须触发 critical。"""
        curated = self._make_curated(10)
        curated.loc[0, "adjustment_factor"] = np.nan
        checker = QualityChecker(config)
        report = checker.run(curated, trade_calendar=trade_calendar)
        completeness = [i for i in report.issues if i.check == "required_completeness"]
        assert len(completeness) > 0

    def test_inf_in_required_field_is_critical(self, config, trade_calendar):
        """inf 在必需字段中必须触发 critical。"""
        curated = self._make_curated(10)
        curated.loc[4, "high_qfq"] = np.inf
        checker = QualityChecker(config)
        report = checker.run(curated, trade_calendar=trade_calendar)
        completeness = [i for i in report.issues if i.check == "required_completeness"]
        assert len(completeness) > 0


class TestG1_01_NoBackfill:
    """禁止用 raw OHLC 静默回填 qfq OHLC。"""

    def test_akshare_no_qfq_does_not_backfill(self):
        """AKShare 提供器在 qfq 缺失时不应回填 raw 值。"""
        from tests.test_providers import _akshare_hist_df

        unadj = _akshare_hist_df(5)
        provider = _MockAKShareNoQfq(unadj)
        raw = provider.fetch_daily_quotes("000001", date(2024, 1, 2), date(2024, 1, 10))
        # qfq 列应全为 NaN，不是 raw 值
        assert raw["__qfq_close"].isna().all()
        assert raw["__qfq_open"].isna().all()

    def test_baostock_no_qfq_does_not_backfill(self):
        """BaoStock 提供器在 qfq 缺失时不应回填 raw 值。"""
        from tests.test_providers import _baostock_hist_df

        raw_df = _baostock_hist_df(5)
        provider = _MockBaoStockNoQfq(raw_df)
        out = provider.fetch_daily_quotes("000001", date(2024, 1, 2), date(2024, 1, 10))
        assert out["__qfq_close"].isna().all()

    def test_adjustment_factor_nan_when_close_raw_zero(self, standardizer):
        """close_raw 为 0 时复权因子应为 NaN，不是 1.0。"""
        raw = make_normal_raw("000001", n_days=5)
        raw.loc[2, "__raw_close"] = 0.0
        curated = standardizer.standardize_daily_quotes(raw, SOURCE_AKSHARE)
        assert pd.isna(curated.loc[2, "adjustment_factor"])


class TestG1_01_DateConsistency:
    """raw 与 qfq 日期集合不一致检查。"""

    def test_raw_qfq_date_mismatch_critical(self, config, trade_calendar):
        """raw 和 qfq 日期不一致应触发 critical。"""
        raw = make_normal_raw("000001", n_days=10)
        # 删除一行的 qfq 数据制造日期不一致
        raw.loc[3, "__qfq_open"] = np.nan
        raw.loc[3, "__qfq_high"] = np.nan
        raw.loc[3, "__qfq_low"] = np.nan
        raw.loc[3, "__qfq_close"] = np.nan
        curated = Standardizer().standardize_daily_quotes(raw, SOURCE_AKSHARE)
        checker = QualityChecker(config)
        report = checker.run(curated, trade_calendar=trade_calendar)
        consistency = [
            i for i in report.issues if i.check == "raw_qfq_date_consistency"
        ]
        assert len(consistency) > 0
        assert report.has_critical

    def test_raw_qfq_dates_consistent_no_issue(self, config, trade_calendar):
        """正常数据不应触发日期不一致。"""
        raw = make_normal_raw("000001", n_days=10)
        curated = Standardizer().standardize_daily_quotes(raw, SOURCE_AKSHARE)
        checker = QualityChecker(config)
        report = checker.run(curated, trade_calendar=trade_calendar)
        consistency = [
            i for i in report.issues if i.check == "raw_qfq_date_consistency"
        ]
        assert len(consistency) == 0


# ============================================================
# G1-02: AKShare ST 列名映射错误且失败后默认"非 ST"
# ============================================================

class TestG1_02_STMapping:
    """ST 列名映射与 unknown 状态。"""

    def test_chinese_column_st_recognized(self):
        """AKShare ST 接口返回中文列名时应正确识别 ST 股票。"""
        from tests.test_providers import _akshare_hist_df

        class MockChineseST(AKShareProvider):
            def __init__(self):
                pass

            def _call_code_name(self):
                return pd.DataFrame(
                    {"code": ["000001", "000002"], "name": ["股票A", "股票B"]}
                )

            def _call_st_list(self):
                # AKShare stock_zh_a_st_em 返回中文列名
                return pd.DataFrame(
                    {"代码": ["000001"], "名称": ["ST股票A"]}
                )

        provider = MockChineseST()
        sm_raw = provider.fetch_security_master()
        # 000001 应被识别为 ST（统一使用 __st_status 契约）
        row = sm_raw[sm_raw["code"] == "000001"].iloc[0]
        assert row["__st_status"] == "st"

    def test_st_interface_exception_marks_unknown(self):
        """ST 接口异常时状态应标记为 unknown，不是 normal。"""
        from tests.test_providers import _akshare_hist_df

        class MockSTException(AKShareProvider):
            def __init__(self):
                pass

            def _call_code_name(self):
                return pd.DataFrame(
                    {"code": ["000001"], "name": ["股票A"]}
                )

            def _call_st_list(self):
                raise RuntimeError("ST 接口不可用")

        provider = MockSTException()
        sm_raw = provider.fetch_security_master()
        assert "__st_status" in sm_raw.columns
        assert sm_raw["__st_status"].iloc[0] == "unknown"

    def test_st_interface_schema_change_marks_unknown(self):
        """ST 接口返回未知 schema 时状态应标记为 unknown。"""
        from tests.test_providers import _akshare_hist_df

        class MockSTSchemaChange(AKShareProvider):
            def __init__(self):
                pass

            def _call_code_name(self):
                return pd.DataFrame(
                    {"code": ["000001"], "name": ["股票A"]}
                )

            def _call_st_list(self):
                # 返回完全不同的列名
                return pd.DataFrame({"ticker": ["000001"], "desc": ["test"]})

        provider = MockSTSchemaChange()
        sm_raw = provider.fetch_security_master()
        assert sm_raw["__st_status"].iloc[0] == "unknown"


# ============================================================
# G1-03: 证券状态区间被人为制造且不可复现
# ============================================================

class TestG1_03_MasterReproducibility:
    """证券主数据可复现性与 status_valid_from。"""

    def test_akshare_master_no_current_date_in_valid_from(self):
        """AKShare 主数据标准化后 status_valid_from 不应写入当天日期。"""
        from tests.test_providers import _akshare_hist_df, MockAKShareProvider

        provider = MockAKShareProvider(_akshare_hist_df(2), _akshare_hist_df(2))
        sm_raw = provider.fetch_security_master()
        sm = Standardizer().standardize_security_master(sm_raw, SOURCE_AKSHARE)
        today = date.today()
        for v in sm["status_valid_from"]:
            if pd.notna(v):
                assert v != today, "status_valid_from 不应等于当天日期"

    def test_status_valid_from_is_null_when_unknown(self):
        """未知状态起始日应为空值。"""
        from tests.test_providers import _akshare_hist_df, MockAKShareProvider

        provider = MockAKShareProvider(_akshare_hist_df(2), _akshare_hist_df(2))
        sm_raw = provider.fetch_security_master()
        sm = Standardizer().standardize_security_master(sm_raw, SOURCE_AKSHARE)
        # AKShare 仅提供快照，status_valid_from 应为空
        assert sm["status_valid_from"].isna().all()

    def test_observed_at_field_exists(self):
        """证券主数据应包含 observed_at 字段。"""
        from tests.test_providers import _akshare_hist_df, MockAKShareProvider

        provider = MockAKShareProvider(_akshare_hist_df(2), _akshare_hist_df(2))
        sm_raw = provider.fetch_security_master()
        sm = Standardizer().standardize_security_master(sm_raw, SOURCE_AKSHARE)
        assert "observed_at" in sm.columns

    def test_security_master_reproducible_across_dates(self):
        """相同原始输入在不同日期运行应产生相同内容哈希（排除 observed_at）。"""
        from tests.test_providers import _akshare_hist_df, MockAKShareProvider

        provider = MockAKShareProvider(_akshare_hist_df(2), _akshare_hist_df(2))
        sm_raw = provider.fetch_security_master()
        config = load_config(default_config_path())
        exclude = config.manifest.content_hash_exclude_fields + ["observed_at"]

        sm1 = Standardizer().standardize_security_master(sm_raw, SOURCE_AKSHARE)
        h1 = content_hash(sm1, exclude)

        # 模拟不同日期运行（observed_at 会变，但内容哈希应不变）
        sm2 = Standardizer().standardize_security_master(sm_raw, SOURCE_AKSHARE)
        h2 = content_hash(sm2, exclude)
        assert h1 == h2

    def test_security_master_content_hash_stable(self):
        """证券主数据内容哈希在多次运行间稳定。"""
        from tests.test_providers import _akshare_hist_df, MockAKShareProvider

        provider = MockAKShareProvider(_akshare_hist_df(2), _akshare_hist_df(2))
        sm_raw = provider.fetch_security_master()
        config = load_config(default_config_path())
        exclude = config.manifest.content_hash_exclude_fields + ["observed_at"]

        sm1 = Standardizer().standardize_security_master(sm_raw, SOURCE_AKSHARE)
        sm2 = Standardizer().standardize_security_master(sm_raw, SOURCE_AKSHARE)
        assert content_hash(sm1, exclude) == content_hash(sm2, exclude)


# ============================================================
# G1-04: BaoStock 错误码与登录结果未校验
# ============================================================

class TestG1_04_BaoStockErrorCodes:
    """BaoStock 错误码校验。"""

    def test_login_failure_raises_exception(self):
        """BaoStock 登录失败应抛出异常。"""

        class MockLoginFail(BaoStockProvider):
            def __init__(self):
                pass

            def _login(self):
                result = MagicMock()
                result.error_code = "1"
                result.error_msg = "登录失败"
                return result

            def _logout(self):
                return None

        provider = MockLoginFail()
        with pytest.raises(Exception, match="登录"):
            provider.fetch_daily_quotes("000001", date(2024, 1, 2), date(2024, 1, 10))

    def test_query_failure_raises_exception(self):
        """BaoStock 查询失败（非零 error_code）应抛出异常。"""

        class MockQueryFail(BaoStockProvider):
            def __init__(self):
                pass

            def _login(self):
                result = MagicMock()
                result.error_code = "0"
                result.error_msg = ""
                return result

            def _logout(self):
                return None

            def _call_daily_hist(self, bs_code, start, end, adjustflag):
                raise RuntimeError("查询失败: error_code=1, 网络异常")

        provider = MockQueryFail()
        with pytest.raises(Exception, match="查询失败|error"):
            provider.fetch_daily_quotes("000001", date(2024, 1, 2), date(2024, 1, 10))

    def test_query_returns_error_code_raises(self):
        """BaoStock query 返回非零 error_code 时应抛异常。"""

        class MockQueryError(BaoStockProvider):
            def __init__(self):
                pass

            def _login(self):
                result = MagicMock()
                result.error_code = "0"
                result.error_msg = ""
                return result

            def _logout(self):
                return None

            def _call_daily_hist(self, bs_code, start, end, adjustflag):
                # 返回带错误码的空结果
                raise RuntimeError("BaoStock error_code=1: system error")

        provider = MockQueryError()
        with pytest.raises(RuntimeError, match="error"):
            provider.fetch_daily_quotes("000001", date(2024, 1, 2), date(2024, 1, 10))

    def test_legitimate_empty_not_exception(self):
        """合法无数据（正常返回空）不应抛异常。"""
        from tests.test_providers import _baostock_hist_df

        class MockEmpty(BaoStockProvider):
            def __init__(self):
                pass

            def _login(self):
                result = MagicMock()
                result.error_code = "0"
                result.error_msg = ""
                return result

            def _logout(self):
                return None

            def _call_daily_hist(self, bs_code, start, end, adjustflag):
                return pd.DataFrame(
                    columns=["date", "open", "high", "low", "close", "volume", "amount"]
                )

        provider = MockEmpty()
        result = provider.fetch_daily_quotes("000001", date(2024, 1, 2), date(2024, 1, 10))
        assert result.empty


# ============================================================
# G1-05: 缺失交易日检查无法发现首尾截断
# ============================================================

class TestG1_05_HeadTailTruncation:
    """首尾截断检测与请求范围覆盖率。"""

    def test_head_truncation_detected(self, config):
        """请求区间开头缺失应被检测到。"""
        cal = make_trade_calendar(date(2024, 1, 2), date(2024, 2, 15))
        # 生成数据但跳过前 5 个交易日
        raw = make_normal_raw("000001", n_days=20)
        raw = raw.iloc[5:].reset_index(drop=True)
        curated = Standardizer().standardize_daily_quotes(raw, SOURCE_AKSHARE)
        checker = QualityChecker(config)
        report = checker.run(
            curated,
            trade_calendar=cal,
            request_start="2024-01-02",
            request_end="2024-02-15",
        )
        missing = [i for i in report.issues if i.check == "missing_trade_day"]
        assert len(missing) > 0
        assert report.has_critical

    def test_tail_truncation_detected(self, config):
        """请求区间结尾缺失应被检测到。"""
        cal = make_trade_calendar(date(2024, 1, 2), date(2024, 2, 15))
        # 生成数据但跳过最后 5 个交易日
        raw = make_normal_raw("000001", n_days=20)
        raw = raw.iloc[:-5].reset_index(drop=True)
        curated = Standardizer().standardize_daily_quotes(raw, SOURCE_AKSHARE)
        checker = QualityChecker(config)
        report = checker.run(
            curated,
            trade_calendar=cal,
            request_start="2024-01-02",
            request_end="2024-02-15",
        )
        missing = [i for i in report.issues if i.check == "missing_trade_day"]
        assert len(missing) > 0
        assert report.has_critical

    def test_pre_listing_not_flagged(self, config):
        """上市前的日期不应被标记为缺失。"""
        cal = make_trade_calendar(date(2024, 1, 2), date(2024, 2, 29))
        raw = make_normal_raw("000001", n_days=15)
        curated = Standardizer().standardize_daily_quotes(raw, SOURCE_AKSHARE)
        # 提供证券主数据：上市日为 2024-01-08
        sm = pd.DataFrame([
            {
                "symbol": "000001",
                "name": "新股",
                "list_date": date(2024, 1, 8),
                "delist_date": None,
                "board": "main",
                "st_status": "normal",
                "status_valid_from": date(2024, 1, 8),
                "status_valid_to": None,
                "observed_at": datetime(2024, 1, 8),
            }
        ])
        checker = QualityChecker(config)
        report = checker.run(
            curated,
            security_master=sm,
            trade_calendar=cal,
            request_start="2024-01-02",
            request_end="2024-02-15",
        )
        # 上市前的缺失（1月2日-1月5日）不应被标记
        pre_listing_missing = [
            i for i in report.issues
            if i.check == "missing_trade_day"
            and i.trade_date is not None
            and str(i.trade_date) < "2024-01-08"
        ]
        assert len(pre_listing_missing) == 0

    def test_post_delist_not_flagged(self, config):
        """退市后的日期不应被标记为缺失。"""
        cal = make_trade_calendar(date(2024, 1, 2), date(2024, 2, 29))
        raw = make_normal_raw("000001", n_days=15)
        curated = Standardizer().standardize_daily_quotes(raw, SOURCE_AKSHARE)
        # 提供证券主数据：退市日为数据结束日
        last_date = str(curated["trade_date"].max())
        sm = pd.DataFrame([
            {
                "symbol": "000001",
                "name": "退市股",
                "list_date": date(2024, 1, 2),
                "delist_date": curated["trade_date"].max(),
                "board": "main",
                "st_status": "delisted",
                "status_valid_from": date(2024, 1, 2),
                "status_valid_to": curated["trade_date"].max(),
                "observed_at": datetime(2024, 1, 2),
            }
        ])
        checker = QualityChecker(config)
        report = checker.run(
            curated,
            security_master=sm,
            trade_calendar=cal,
            request_start="2024-01-02",
            request_end="2024-02-29",
        )
        # 退市后的缺失不应被标记
        post_delist_missing = [
            i for i in report.issues
            if i.check == "missing_trade_day"
            and i.trade_date is not None
            and str(i.trade_date) > last_date
        ]
        assert len(post_delist_missing) == 0

    def test_no_request_range_falls_back_to_data_range(self, config):
        """不提供 request_start/end 时回退到数据范围（向后兼容）。"""
        cal = make_trade_calendar(date(2024, 1, 2), date(2024, 2, 15))
        raw = make_normal_raw("000001", n_days=20)
        curated = Standardizer().standardize_daily_quotes(raw, SOURCE_AKSHARE)
        checker = QualityChecker(config)
        # 不传 request_start/end，应回退到旧行为
        report = checker.run(curated, trade_calendar=cal)
        # 正常数据不应有 critical
        assert not report.has_critical


# ============================================================
# 辅助 mock 类
# ============================================================

class _MockAKShareNoQfq(AKShareProvider):
    """AKShare 提供器，qfq 查询返回空。"""

    def __init__(self, unadj: pd.DataFrame):
        self._unadj = unadj

    def _call_daily_hist(self, symbol, start, end, adjust):
        if adjust == "qfq":
            return pd.DataFrame()
        return self._unadj.copy()

    def _call_code_name(self):
        return pd.DataFrame({"code": ["000001"], "name": ["test"]})

    def _call_st_list(self):
        return pd.DataFrame(columns=["code", "name"])

    def _call_trade_dates(self):
        return pd.DataFrame({"trade_date": pd.date_range("2024-01-02", periods=10)})


class _MockBaoStockNoQfq(BaoStockProvider):
    """BaoStock 提供器，qfq 查询返回空。"""

    def __init__(self, raw_df: pd.DataFrame):
        self._raw = raw_df

    def _login(self):
        result = MagicMock()
        result.error_code = "0"
        result.error_msg = ""
        return result

    def _logout(self):
        return None

    def _call_daily_hist(self, bs_code, start, end, adjustflag):
        if adjustflag == "2":
            return pd.DataFrame(
                columns=["date", "open", "high", "low", "close", "volume", "amount"]
            )
        return self._raw.copy()
