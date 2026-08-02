"""数据提供器测试：通过 mock/子类化，禁止访问公网。

验证网络获取与标准化解耦：提供器只返回原始数据，标准化独立完成。
所有测试离线运行，不安装 akshare/baostock。
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from ashare_quant.constants import DAILY_QUOTE_FIELDS
from ashare_quant.providers import AKShareProvider, BaoStockProvider, DataProvider
from ashare_quant.providers.akshare_provider import _to_ak_code
from ashare_quant.providers.baostock_provider import _to_bs_code
from ashare_quant.standardize import Standardizer


# ---- AKShare mock ----
def _akshare_hist_df(n: int = 5) -> pd.DataFrame:
    """构造 AKShare 原生列名 DataFrame。"""
    rows = []
    base = date(2024, 1, 2)
    from datetime import timedelta

    p = 10.0
    cur = base
    count = 0
    while count < n:
        if cur.weekday() < 5:
            rows.append(
                {
                    "日期": cur.strftime("%Y-%m-%d"),
                    "开盘": p * 0.99,
                    "收盘": p,
                    "最高": p * 1.01,
                    "最低": p * 0.98,
                    "成交量": 100000,
                    "成交额": 1000000.0,
                    "振幅": 1.0,
                    "涨跌幅": 0.1,
                    "涨跌额": 0.01,
                    "换手率": 0.5,
                }
            )
            count += 1
        cur += timedelta(days=1)
        p *= 1.001
    return pd.DataFrame(rows)


class MockAKShareProvider(AKShareProvider):
    def __init__(self, unadj: pd.DataFrame, qfq: pd.DataFrame):
        self._unadj = unadj
        self._qfq = qfq

    def _call_daily_hist(self, symbol, start, end, adjust):
        return self._qfq.copy() if adjust == "qfq" else self._unadj.copy()

    def _call_code_name(self):
        return pd.DataFrame({"code": ["000001"], "name": ["平安银行"]})

    def _call_st_list(self):
        return pd.DataFrame(columns=["code", "name"])

    def _call_trade_dates(self):
        return pd.DataFrame({"trade_date": pd.date_range("2024-01-02", periods=10)})


def test_akshare_provider_is_dataprovider():
    p = AKShareProvider()
    assert isinstance(p, DataProvider)
    assert p.name == "akshare"


@pytest.mark.parametrize(
    ("symbol", "expected"),
    [
        ("600000", "600000"),
        ("600000.SH", "600000"),
        ("000001.SZ", "000001"),
        ("sh.600519", "600519"),
    ],
)
def test_to_ak_code(symbol, expected):
    assert _to_ak_code(symbol) == expected


def test_akshare_fetch_returns_intermediate_columns():
    unadj = _akshare_hist_df(5)
    qfq = _akshare_hist_df(5)
    # qfq 价格略不同以验证复权因子
    qfq["收盘"] = qfq["收盘"] * 1.1
    provider = MockAKShareProvider(unadj, qfq)
    raw = provider.fetch_daily_quotes("000001", date(2024, 1, 2), date(2024, 1, 10))
    assert not raw.empty
    for c in ("__raw_open", "__raw_close", "__qfq_close", "volume", "amount", "__source_symbol"):
        assert c in raw.columns


def test_akshare_fetch_then_standardize():
    unadj = _akshare_hist_df(5)
    qfq = _akshare_hist_df(5)
    qfq["收盘"] = qfq["收盘"] * 1.1
    provider = MockAKShareProvider(unadj, qfq)
    raw = provider.fetch_daily_quotes("000001", date(2024, 1, 2), date(2024, 1, 10))
    curated = Standardizer().standardize_daily_quotes(raw, "akshare")
    assert list(curated.columns) == DAILY_QUOTE_FIELDS
    assert (curated["source"] == "akshare").all()
    # 复权因子 = qfq_close / raw_close = 1.1
    assert abs(curated["adjustment_factor"].iloc[0] - 1.1) < 1e-6


def test_akshare_fetch_empty():
    provider = MockAKShareProvider(pd.DataFrame(), pd.DataFrame())
    raw = provider.fetch_daily_quotes("000001", date(2024, 1, 2), date(2024, 1, 10))
    assert raw.empty


def test_akshare_security_master():
    provider = MockAKShareProvider(_akshare_hist_df(2), _akshare_hist_df(2))
    sm_raw = provider.fetch_security_master()
    assert "__st_status" in sm_raw.columns


# ---- BaoStock mock ----
def _baostock_hist_df(n: int = 5) -> pd.DataFrame:
    rows = []
    base = date(2024, 1, 2)
    from datetime import timedelta

    p = 10.0
    cur = base
    count = 0
    while count < n:
        if cur.weekday() < 5:
            rows.append(
                {
                    "date": cur.strftime("%Y-%m-%d"),
                    "open": str(p * 0.99),
                    "high": str(p * 1.01),
                    "low": str(p * 0.98),
                    "close": str(p),
                    "volume": "100000",
                    "amount": "1000000",
                }
            )
            count += 1
        cur += timedelta(days=1)
        p *= 1.001
    return pd.DataFrame(rows)


class MockBaoStockProvider(BaoStockProvider):
    def __init__(self, raw_df: pd.DataFrame, qfq_df: pd.DataFrame):
        self._raw = raw_df
        self._qfq = qfq_df

    def _login(self):
        return None

    def _logout(self):
        return None

    def _call_daily_hist(self, bs_code, start, end, adjustflag):
        return self._qfq.copy() if adjustflag == "2" else self._raw.copy()


def test_baostock_provider_name():
    assert BaoStockProvider().name == "baostock"


def test_to_bs_code():
    assert _to_bs_code("600000") == "sh.600000"
    assert _to_bs_code("000001") == "sz.000001"
    assert _to_bs_code("688989") == "sh.688989"
    assert _to_bs_code("600000.SH") == "sh.600000"
    assert _to_bs_code("000001.SZ") == "sz.000001"
    assert _to_bs_code("sh.600519") == "sh.600519"


@pytest.mark.parametrize(
    "bad",
    ["12345", "ABCDEF", "", "600000.SHH", "sh.60051", "60000 0", None],
)
def test_to_ak_code_rejects_invalid(bad):
    """非法代码必须明确报错，不能静默猜测。"""
    with pytest.raises(ValueError):
        _to_ak_code(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "bad",
    ["12345", "ABCDEF", "", "600000.SHH", "sh.60051", "60000 0", None],
)
def test_to_bs_code_rejects_invalid(bad):
    """非法代码必须明确报错，不能静默猜测。"""
    with pytest.raises(ValueError):
        _to_bs_code(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("bj", ["bj.430047", "430047.BJ", "920001.BJ", "bj.920001"])
def test_to_bs_code_rejects_bj_market(bj):
    """BaoStock 对北京交易所未经验证：bj./.BJ 必须明确拒绝，禁止静默生成 bj.XXXXXX。"""
    with pytest.raises(ValueError, match="北京交易所"):
        _to_bs_code(bj)


@pytest.mark.parametrize("bj", ["bj.430047", "430047.BJ"])
def test_to_ak_code_keeps_bj_independent(bj):
    """AKShare 对 BJ 的支持独立于 BaoStock：AKShare 转换仍接受北京代码。"""
    assert _to_ak_code(bj) == "430047"


def test_providers_normalize_standard_symbols_before_sdk_call():
    class RecordingAK(MockAKShareProvider):
        calls = []

        def _call_daily_hist(self, symbol, start, end, adjust):
            self.calls.append(symbol)
            return super()._call_daily_hist(symbol, start, end, adjust)

    class RecordingBS(MockBaoStockProvider):
        calls = []

        def _call_daily_hist(self, bs_code, start, end, adjustflag):
            self.calls.append(bs_code)
            return super()._call_daily_hist(bs_code, start, end, adjustflag)

    ak = RecordingAK(_akshare_hist_df(1), _akshare_hist_df(1))
    bs = RecordingBS(_baostock_hist_df(1), _baostock_hist_df(1))
    ak.fetch_daily_quotes("600000.SH", date(2024, 1, 2), date(2024, 1, 2))
    bs.fetch_daily_quotes("000001.SZ", date(2024, 1, 2), date(2024, 1, 2))
    assert ak.calls == ["600000", "600000"]
    assert bs.calls == ["sz.000001", "sz.000001"]


def test_baostock_fetch_then_standardize():
    raw = _baostock_hist_df(5)
    qfq = _baostock_hist_df(5)
    qfq["close"] = str(11.0)  # qfq 收盘不同
    provider = MockBaoStockProvider(raw, qfq)
    out = provider.fetch_daily_quotes("000001", date(2024, 1, 2), date(2024, 1, 10))
    assert not out.empty
    curated = Standardizer().standardize_daily_quotes(out, "baostock")
    assert (curated["source"] == "baostock").all()
    assert len(curated) == 5


def test_providers_lazy_import_no_sdk_needed():
    """实例化提供器不需要安装 akshare/baostock。"""
    ak = AKShareProvider()
    bs = BaoStockProvider()
    assert ak is not None
    assert bs is not None
