"""FR-20 真实数据更新流程测试：可注入、可离线、**不可伪造**。

Codex Gate 4A 第一轮审核的判词很不客气：Phase 4 的 ``market_data`` 步骤只会去
curated 层"捡现成的"，整条流水线并不真正拥有数据更新能力——数据是谁抓的、
抓失败了会怎样，全是别人的事。本文件验证补上的那根承重梁
:mod:`ashare_quant.automation.data_update`。

验收覆盖
--------
=====  ==================================================================
编号    场景
=====  ==================================================================
1      主源成功：raw / curated 双层落盘 + SHA-256 + 数据版本清单
2      主源失败、备用源成功：``fallback_used`` 如实留痕，运行继续
3      主备双源全失败：``DataUnavailableError`` -> ``SKIPPED_DATA_UNAVAILABLE``
4      ``allow_skip_when_unavailable=false``：升格 ``FAILED``（退出码 1）
5      部分标的失败：不伪造、不静默，其余标的继续
6      schema 校验不通过（严格模式）：脏数据连 curated 层的门都进不去
7      诚实性契约：注入提供器后 ``online=False`` / ``synthetic=True`` 自动生效
8      审计产物：``data-update.json`` 与 run 记录中的步骤明细
=====  ==================================================================

离线保证
--------
全部测试通过子类化 :class:`DataProvider` 运行——不 import akshare、
不 import baostock、不开 socket。fake 提供器走的是与线上**完全相同**的
``FetchManager`` 重试 / 回退 / 清单代码路径，注入点是为了可测，
不是给生产留一条伪造数据源的后门。
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import pytest

# 确保 tests 包可导入（与研究样本同目录）
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from ashare_quant.automation.calendar import TradingCalendar
from ashare_quant.automation.config import (
    AccountConfig,
    AutomationConfig,
    DataConfig,
    LoggingConfig,
    PathsConfig,
)
from ashare_quant.automation.data_update import (
    AutoUpdatingDataSource,
    DataUpdateResult,
    FetchManagerDataUpdater,
    SymbolUpdateOutcome,
    build_updating_data_source,
)
from ashare_quant.automation.datasource import (
    QUOTE_COLUMNS,
    DataUnavailableError,
    DataUpdateFailedError,
    LocalParquetDataSource,
)
from ashare_quant.automation.models import (
    EligibilityStatus,
    RunState,
    StrategyTrack,
)
from ashare_quant.automation.runner import map_exception_state
from ashare_quant.automation.state import StateStore
from ashare_quant.config import load_config
from ashare_quant.constants import (
    LAYER_CURATED,
    LAYER_METADATA,
    LAYER_RAW,
    SOURCE_AKSHARE,
    SOURCE_BAOSTOCK,
)
from ashare_quant.providers import DataProvider
from ashare_quant.storage import file_sha256
from tests.research_samples import make_research_quotes


# ---------------------------------------------------------------------- #
# 离线 fake 提供器
# ---------------------------------------------------------------------- #


class _FakeProvider(DataProvider):
    """离线 fake 数据提供器：行为完全由构造参数决定。

    它不 import akshare、不 import baostock、不开 socket——"离线测试"
    四个字的字面意思。它唯一的职责是替真实提供器站在
    :class:`FetchManager` 的下游，让重试与回退逻辑跑起来。
    """

    def __init__(
        self,
        name: str,
        *,
        quotes_by_symbol: Optional[dict[str, pd.DataFrame]] = None,
        fail_quotes: int = 0,
        fail_symbols: tuple[str, ...] = (),
        master: Optional[pd.DataFrame] = None,
        fail_master: bool = False,
    ) -> None:
        """
        Args:
            name: 数据源标识（``akshare`` / ``baostock``）。
            quotes_by_symbol: 标的 -> 原始行情 DataFrame。
            fail_quotes: 每个标的的前 N 次抓取抛错；``-1`` 表示永远失败。
            fail_symbols: 这些标的永远抓取失败（模拟个别标的退市/停牌缺数据）。
            master: 证券主数据原始表；``None`` 时抓取主数据失败。
            fail_master: 强制主数据抓取失败。
        """
        self._name = name
        self._quotes = dict(quotes_by_symbol or {})
        self._fail_quotes = fail_quotes
        self._fail_symbols = set(fail_symbols)
        self._master = master
        self._fail_master = fail_master
        #: 逐次调用记录，供测试断言重试与回退次数
        self.quote_calls: list[str] = []
        self.master_calls: int = 0

    @property
    def name(self) -> str:
        return self._name

    def fetch_daily_quotes(
        self, symbol: str, start_date: date, end_date: date
    ) -> pd.DataFrame:
        self.quote_calls.append(symbol)
        nth = sum(1 for s in self.quote_calls if s == symbol)
        if symbol in self._fail_symbols:
            raise RuntimeError(f"{self._name}: {symbol} 无数据（离线 fake，永久失败）")
        if self._fail_quotes < 0 or nth <= self._fail_quotes:
            raise RuntimeError(
                f"{self._name}: 抓取 {symbol} 失败（第 {nth} 次尝试，离线 fake）"
            )
        df = self._quotes.get(symbol)
        if df is None:
            raise RuntimeError(f"{self._name}: 未预置 {symbol} 数据（离线 fake）")
        return df.copy()

    def fetch_security_master(self) -> pd.DataFrame:
        self.master_calls += 1
        if self._fail_master or self._master is None:
            raise RuntimeError(f"{self._name}: 证券主数据不可用（离线 fake）")
        return self._master.copy()

    def fetch_trade_calendar(
        self, start_date: date, end_date: date
    ) -> pd.DataFrame:
        raise RuntimeError(f"{self._name}: 不提供交易日历（离线 fake）")


# ---------------------------------------------------------------------- #
# 样本构造：curated -> 提供器原始格式的反向映射
# ---------------------------------------------------------------------- #

#: curated 规范列 -> :class:`Standardizer` 期望的原始中间列
_CURATED_TO_RAW = {
    "symbol": "__source_symbol",
    "trade_date": "date",
    "open_raw": "__raw_open",
    "high_raw": "__raw_high",
    "low_raw": "__raw_low",
    "close_raw": "__raw_close",
    "open_qfq": "__qfq_open",
    "high_qfq": "__qfq_high",
    "low_qfq": "__qfq_low",
    "close_qfq": "__qfq_close",
    "fetched_at": "__fetched_at",
}


def _curated_to_raw(df: pd.DataFrame) -> pd.DataFrame:
    """把已知合格的 curated 样本反向映射成提供器原始格式。

    这样 fake 提供器吐出的数据经 :class:`Standardizer` 走一圈后，
    形状与 Phase 3 研究样本完全一致——质量闸门不会因为数据长得奇怪而挑刺，
    测试聚焦在"更新流程"本身而不是"样本合不合口味"。
    """
    out = df.rename(columns=_CURATED_TO_RAW)
    keep = list(_CURATED_TO_RAW.values()) + ["volume", "amount"]
    return out[[c for c in keep if c in out.columns]].reset_index(drop=True)


def _sample_pool(n_stocks: int = 8, n_days: int = 200):
    """生成 ``{symbol: 原始行情}`` 与交易日列表。"""
    quotes = make_research_quotes(
        start=date(2020, 1, 2), n_days=n_days, n_stocks=n_stocks
    )
    by_symbol = {
        str(sym): _curated_to_raw(grp)
        for sym, grp in quotes.groupby("symbol", sort=True)
    }
    trade_dates = sorted({pd.Timestamp(d).date() for d in quotes["trade_date"]})
    return by_symbol, trade_dates


def _akshare_master(symbols) -> pd.DataFrame:
    """AKShare ``stock_info_a_code_name`` 风格的原始主数据。"""
    syms = list(symbols)
    return pd.DataFrame(
        {
            "code": syms,
            "name": [f"合成{s}" for s in syms],
            "__st_status": ["normal"] * len(syms),
            "__fetched_at": [pd.Timestamp("2020-01-01 09:00:00")] * len(syms),
        }
    )


# ---------------------------------------------------------------------- #
# 夹具
# ---------------------------------------------------------------------- #


@pytest.fixture
def app_cfg():
    """Phase 1 应用配置；重试放宽到 1 次、间隔归零以加速测试。"""
    cfg = load_config(_ROOT / "config" / "default.yaml")
    cfg.providers.max_retries = 1
    cfg.providers.request_interval_seconds = 0.0
    return cfg


@pytest.fixture
def pool():
    return _sample_pool()


def _providers(
    pool_data: dict[str, pd.DataFrame],
    *,
    primary_kwargs: Optional[dict] = None,
    fallback_kwargs: Optional[dict] = None,
) -> dict[str, _FakeProvider]:
    """构造主备两个 fake 提供器，返回 ``name -> provider`` 映射。"""
    ak = _FakeProvider(SOURCE_AKSHARE, quotes_by_symbol=pool_data, **(primary_kwargs or {}))
    bs = _FakeProvider(
        SOURCE_BAOSTOCK, quotes_by_symbol=pool_data, **(fallback_kwargs or {})
    )
    return {SOURCE_AKSHARE: ak, SOURCE_BAOSTOCK: bs}


def _build_updater(app_config, data_dir: Path, provs: dict, **kwargs):
    """构造注入 fake 提供器的正式更新器。"""
    kwargs.setdefault("update_security_master", False)
    kwargs.setdefault("code_commit", "fr20-test-commit")
    return FetchManagerDataUpdater(
        app_config,
        data_dir=data_dir,
        provider_factory=lambda name: provs[name],
        **kwargs,
    )


def _update(updater, symbols, trade_dates) -> DataUpdateResult:
    return updater.update(
        symbols=symbols,
        start=trade_dates[0],
        end=trade_dates[-1],
        as_of=trade_dates[-1],
    )


# ====================================================================== #
# 1. 主源成功：抓取 -> 落盘 -> 清单
# ====================================================================== #


class TestPrimarySuccess:
    def test_primary_success_returns_curated_quotes(self, app_cfg, pool, tmp_path):
        """主源一次成功：返回 curated 规范列的行情。"""
        by_symbol, trade_dates = pool
        provs = _providers(by_symbol)
        updater = _build_updater(app_cfg, tmp_path / "data", provs)

        result = _update(updater, ["000001", "000002"], trade_dates)

        assert result.updater == "fetch-manager"
        assert result.any_success
        assert result.failed_symbols == []
        assert sorted(result.succeeded_symbols) == ["000001", "000002"]
        for col in QUOTE_COLUMNS:
            assert col in result.quotes.columns, f"curated 行情缺列 {col}"
        assert set(result.quotes["symbol"].unique()) == {"000001", "000002"}

    def test_raw_and_curated_layers_written(self, app_cfg, pool, tmp_path):
        """raw 与 curated 两层都要落盘——原始证据与治理数据缺一不可。"""
        by_symbol, trade_dates = pool
        provs = _providers(by_symbol)
        data_dir = tmp_path / "data"
        updater = _build_updater(app_cfg, data_dir, provs)

        _update(updater, ["000001"], trade_dates)

        raw_files = list((data_dir / LAYER_RAW).glob("*.parquet"))
        curated_files = list((data_dir / LAYER_CURATED).glob("*.parquet"))
        assert len(raw_files) == 1, "原始层应恰好落一个文件"
        assert len(curated_files) == 1, "治理层应恰好落一个文件"
        assert SOURCE_AKSHARE in raw_files[0].name
        assert "daily_quotes_000001" in curated_files[0].name

    def test_sha256_matches_written_files(self, app_cfg, pool, tmp_path):
        """SHA-256 必须与磁盘文件真实一致，不能是随手编的字符串。"""
        by_symbol, trade_dates = pool
        provs = _providers(by_symbol)
        updater = _build_updater(app_cfg, tmp_path / "data", provs)

        result = _update(updater, ["000001"], trade_dates)
        outcome = result.outcomes[0]

        assert outcome.raw_sha256 == file_sha256(outcome.raw_path)
        assert outcome.curated_sha256 == file_sha256(outcome.curated_path)
        assert len(outcome.raw_sha256) == 64
        assert outcome.content_hash and len(outcome.content_hash) == 64

    def test_manifest_written_with_fetch_and_provenance(
        self, app_cfg, pool, tmp_path
    ):
        """数据版本清单要能独立还原"这批数据怎么来的"。"""
        by_symbol, trade_dates = pool
        provs = _providers(by_symbol)
        updater = _build_updater(app_cfg, tmp_path / "data", provs)

        result = _update(updater, ["000001"], trade_dates)
        manifest_path = Path(result.outcomes[0].manifest_path)
        assert manifest_path.exists()

        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert payload["source"] == SOURCE_AKSHARE
        assert payload["symbol"] == "000001"
        assert payload["row_count"] > 0
        assert payload["code_commit"] == "fr20-test-commit"
        # 抓取轨迹与出处标记并入同一份清单，审计不必对着两份文件拼故事
        assert payload["fetch"]["final_source"] == SOURCE_AKSHARE
        assert payload["fetch"]["fallback_used"] is False
        assert payload["fetch"]["total_attempts"] == 1
        assert payload["provenance"]["online"] is False
        assert payload["provenance"]["provider_injected"] is True

    def test_outcome_records_single_attempt_no_fallback(
        self, app_cfg, pool, tmp_path
    ):
        """一次成功就该只有一次尝试；别把成功也记成"重试后成功"。"""
        by_symbol, trade_dates = pool
        provs = _providers(by_symbol)
        updater = _build_updater(app_cfg, tmp_path / "data", provs)

        result = _update(updater, ["000001"], trade_dates)
        outcome = result.outcomes[0]

        assert outcome.attempt_count == 1
        assert outcome.sources_tried == [SOURCE_AKSHARE]
        assert outcome.fallback_used is False
        assert result.fallback_symbols == []


# ====================================================================== #
# 2. 主源失败、备用源接管
# ====================================================================== #


class TestFallback:
    def test_primary_failure_falls_back_to_baostock(self, app_cfg, pool, tmp_path):
        """AKShare 重试耗尽后 BaoStock 接管，运行继续而不是崩掉。"""
        by_symbol, trade_dates = pool
        provs = _providers(by_symbol, primary_kwargs={"fail_quotes": -1})
        updater = _build_updater(app_cfg, tmp_path / "data", provs)

        result = _update(updater, ["000001"], trade_dates)
        outcome = result.outcomes[0]

        assert outcome.success is True
        assert outcome.final_source == SOURCE_BAOSTOCK
        assert outcome.fallback_used is True
        assert outcome.sources_tried == [SOURCE_AKSHARE, SOURCE_BAOSTOCK]
        assert result.fallback_symbols == ["000001"]

    def test_fallback_recorded_in_manifest_and_notes(
        self, app_cfg, pool, tmp_path
    ):
        """回退是重要事实，必须写进清单与运行说明，不能悄悄发生。"""
        by_symbol, trade_dates = pool
        provs = _providers(by_symbol, primary_kwargs={"fail_quotes": -1})
        updater = _build_updater(app_cfg, tmp_path / "data", provs)

        result = _update(updater, ["000001"], trade_dates)
        payload = json.loads(
            Path(result.outcomes[0].manifest_path).read_text(encoding="utf-8")
        )

        assert payload["fetch"]["fallback_used"] is True
        assert payload["fetch"]["primary"] == SOURCE_AKSHARE
        assert payload["fetch"]["fallback"] == SOURCE_BAOSTOCK
        assert any("备用源" in n for n in result.notes)

    def test_retry_count_respects_max_retries(self, app_cfg, pool, tmp_path):
        """max_retries=1 => 主源 2 次（首请求 + 1 重试）后才回退。"""
        by_symbol, trade_dates = pool
        provs = _providers(by_symbol, primary_kwargs={"fail_quotes": -1})
        updater = _build_updater(app_cfg, tmp_path / "data", provs)

        result = _update(updater, ["000001"], trade_dates)
        attempts = result.outcomes[0].attempts
        ak_attempts = [a for a in attempts if a["source"] == SOURCE_AKSHARE]
        bs_attempts = [a for a in attempts if a["source"] == SOURCE_BAOSTOCK]

        assert len(ak_attempts) == 2, "1 + max_retries(1) = 2 次主源尝试"
        assert all(not a["success"] for a in ak_attempts)
        assert len(bs_attempts) == 1
        assert bs_attempts[0]["success"] is True

    def test_partial_retry_then_primary_success(self, app_cfg, pool, tmp_path):
        """主源首次失败、重试成功：不应回退备用源。"""
        by_symbol, trade_dates = pool
        provs = _providers(by_symbol, primary_kwargs={"fail_quotes": 1})
        updater = _build_updater(app_cfg, tmp_path / "data", provs)

        result = _update(updater, ["000001"], trade_dates)
        outcome = result.outcomes[0]

        assert outcome.final_source == SOURCE_AKSHARE
        assert outcome.fallback_used is False
        assert outcome.attempt_count == 2
        assert provs[SOURCE_BAOSTOCK].quote_calls == []


# ====================================================================== #
# 3. 双源失败与部分失败
# ====================================================================== #


class TestDoubleSourceFailure:
    def test_all_symbols_double_failure_raises_data_unavailable(
        self, app_cfg, pool, tmp_path
    ):
        """主备全灭 => DataUnavailableError（本次运行没有可信输入）。"""
        by_symbol, trade_dates = pool
        provs = _providers(
            by_symbol,
            primary_kwargs={"fail_quotes": -1},
            fallback_kwargs={"fail_quotes": -1},
        )
        updater = _build_updater(app_cfg, tmp_path / "data", provs)

        with pytest.raises(DataUnavailableError) as exc:
            _update(updater, ["000001", "000002"], trade_dates)

        msg = str(exc.value)
        assert "全部 2 个标的" in msg
        assert SOURCE_AKSHARE in msg and SOURCE_BAOSTOCK in msg

    def test_failed_fetch_writes_null_file_manifest(self, app_cfg, pool, tmp_path):
        """失败也要留痕：写一份 ``file=null`` 的抓取清单证明"我们确实试过"。"""
        by_symbol, trade_dates = pool
        provs = _providers(
            by_symbol,
            primary_kwargs={"fail_quotes": -1},
            fallback_kwargs={"fail_quotes": -1},
        )
        data_dir = tmp_path / "data"
        updater = _build_updater(app_cfg, data_dir, provs)

        with pytest.raises(DataUnavailableError):
            _update(updater, ["000001"], trade_dates)

        fetch_manifests = list((data_dir / LAYER_METADATA).glob("fetch_000001_*.json"))
        assert len(fetch_manifests) == 1
        payload = json.loads(fetch_manifests[0].read_text(encoding="utf-8"))
        assert payload["success"] is False
        assert payload["file"] is None
        assert payload["final_source"] is None
        assert payload["total_attempts"] == 4, "2 主源 + 2 备用源"

    def test_no_curated_written_on_total_failure(self, app_cfg, pool, tmp_path):
        """全灭时 curated 层必须干干净净——不能留半成品让下游误食。"""
        by_symbol, trade_dates = pool
        provs = _providers(
            by_symbol,
            primary_kwargs={"fail_quotes": -1},
            fallback_kwargs={"fail_quotes": -1},
        )
        data_dir = tmp_path / "data"
        updater = _build_updater(app_cfg, data_dir, provs)

        with pytest.raises(DataUnavailableError):
            _update(updater, ["000001"], trade_dates)

        assert list((data_dir / LAYER_CURATED).glob("*.parquet")) == []

    def test_partial_failure_continues_with_remaining_symbols(
        self, app_cfg, pool, tmp_path
    ):
        """个别标的双源失败，其余标的照常更新——不伪造、不静默、不全盘放弃。"""
        by_symbol, trade_dates = pool
        provs = _providers(
            by_symbol,
            primary_kwargs={"fail_symbols": ("000002",)},
            fallback_kwargs={"fail_symbols": ("000002",)},
        )
        updater = _build_updater(app_cfg, tmp_path / "data", provs)

        result = _update(updater, ["000001", "000002", "000003"], trade_dates)

        assert result.any_success is True
        assert result.all_failed is False
        assert result.failed_symbols == ["000002"]
        assert sorted(result.succeeded_symbols) == ["000001", "000003"]
        assert set(result.quotes["symbol"].unique()) == {"000001", "000003"}
        assert any("部分标的抓取失败" in n for n in result.notes)

    def test_failed_symbol_outcome_carries_error_text(
        self, app_cfg, pool, tmp_path
    ):
        """失败标的的错误原因要能追溯到具体文字，而不是一个空 None。"""
        by_symbol, trade_dates = pool
        provs = _providers(
            by_symbol,
            primary_kwargs={"fail_symbols": ("000002",)},
            fallback_kwargs={"fail_symbols": ("000002",)},
        )
        updater = _build_updater(app_cfg, tmp_path / "data", provs)

        result = _update(updater, ["000001", "000002"], trade_dates)
        failed = [o for o in result.outcomes if not o.success][0]

        assert failed.symbol == "000002"
        assert failed.error and "所有数据源尝试失败" in failed.error
        assert failed.curated_path is None
        assert failed.row_count == 0


# ====================================================================== #
# 4. schema 校验：脏数据不得进入治理层
# ====================================================================== #


class TestSchemaValidation:
    def test_empty_payload_strict_raises_update_failed(
        self, app_cfg, pool, tmp_path
    ):
        """提供器返回空表（"抓取成功但什么都没有"）在严格模式下判失败。"""
        by_symbol, trade_dates = pool
        empty_pool = dict(by_symbol)
        empty_pool["000001"] = by_symbol["000001"].iloc[0:0]
        provs = _providers(empty_pool)
        updater = _build_updater(app_cfg, tmp_path / "data", provs)

        with pytest.raises(DataUpdateFailedError) as exc:
            _update(updater, ["000001"], trade_dates)
        assert "标准化后为空表" in str(exc.value)

    def test_empty_payload_non_strict_marks_symbol_failed(
        self, app_cfg, pool, tmp_path
    ):
        """非严格模式下降级为"该标的失败"，其余标的继续。"""
        by_symbol, trade_dates = pool
        bad_pool = dict(by_symbol)
        bad_pool["000002"] = by_symbol["000002"].iloc[0:0]
        provs = _providers(bad_pool)
        updater = _build_updater(
            app_cfg, tmp_path / "data", provs, strict_schema=False
        )

        result = _update(updater, ["000001", "000002"], trade_dates)

        assert result.failed_symbols == ["000002"]
        assert result.succeeded_symbols == ["000001"]

    def test_duplicate_primary_key_rejected(self, app_cfg, pool, tmp_path):
        """主键重复是典型的上游拼接事故，必须拦在治理层门外。"""
        by_symbol, trade_dates = pool
        dup_pool = dict(by_symbol)
        dup_pool["000001"] = pd.concat(
            [by_symbol["000001"], by_symbol["000001"]], ignore_index=True
        )
        provs = _providers(dup_pool)
        updater = _build_updater(app_cfg, tmp_path / "data", provs)

        with pytest.raises(DataUpdateFailedError) as exc:
            _update(updater, ["000001"], trade_dates)
        assert "主键重复" in str(exc.value)

    def test_all_nan_close_qfq_rejected(self, app_cfg, pool, tmp_path):
        """收盘价全空意味着这批数据毫无用处，不能装作抓到了。"""
        by_symbol, trade_dates = pool
        nan_pool = dict(by_symbol)
        broken = by_symbol["000001"].copy()
        broken["__qfq_close"] = float("nan")
        nan_pool["000001"] = broken
        provs = _providers(nan_pool)
        updater = _build_updater(app_cfg, tmp_path / "data", provs)

        with pytest.raises(DataUpdateFailedError) as exc:
            _update(updater, ["000001"], trade_dates)
        assert "close_qfq 全为空" in str(exc.value)

    def test_raw_kept_but_curated_absent_when_validation_fails(
        self, app_cfg, pool, tmp_path
    ):
        """校验排在 curated 落盘**之前**：原始证据保留，治理层保持洁净。"""
        by_symbol, trade_dates = pool
        dup_pool = dict(by_symbol)
        dup_pool["000001"] = pd.concat(
            [by_symbol["000001"], by_symbol["000001"]], ignore_index=True
        )
        provs = _providers(dup_pool)
        data_dir = tmp_path / "data"
        updater = _build_updater(app_cfg, data_dir, provs)

        with pytest.raises(DataUpdateFailedError):
            _update(updater, ["000001"], trade_dates)

        assert len(list((data_dir / LAYER_RAW).glob("*.parquet"))) == 1
        assert list((data_dir / LAYER_CURATED).glob("*.parquet")) == []


# ====================================================================== #
# 5. 异常语义：不给 fail-closed 开后门
# ====================================================================== #


class TestFailureSemantics:
    def test_update_failed_is_not_subclass_of_data_unavailable(self):
        """继承关系一旦写错，``FAILED`` 会被静悄悄降级成 ``SKIPPED``。

        这是本次修复中最容易被"顺手优化"掉的一行，特此上锁。
        """
        assert not issubclass(DataUpdateFailedError, DataUnavailableError)

    def test_update_failed_maps_to_failed_state(self):
        """``DataUpdateFailedError`` 必须落到 ``FAILED``（退出码 1）。"""
        assert map_exception_state(DataUpdateFailedError("x")) is RunState.FAILED

    def test_data_unavailable_maps_to_skipped_state(self):
        """``DataUnavailableError`` 仍是"今天没数据"，不是事故。"""
        assert (
            map_exception_state(DataUnavailableError("x"))
            is RunState.SKIPPED_DATA_UNAVAILABLE
        )

    def test_empty_symbol_list_raises(self, app_cfg, tmp_path):
        """没有标的可更新时明确报错，不要返回一个空壳"成功"。"""
        provs = _providers({})
        updater = _build_updater(app_cfg, tmp_path / "data", provs)

        with pytest.raises(DataUnavailableError) as exc:
            updater.update(
                symbols=[],
                start=date(2020, 1, 2),
                end=date(2020, 6, 30),
                as_of=date(2020, 6, 30),
            )
        assert "未收到任何标的" in str(exc.value)


# ====================================================================== #
# 6. 诚实性契约
# ====================================================================== #


class TestHonesty:
    def test_injected_provider_forces_offline_synthetic(
        self, app_cfg, pool, tmp_path
    ):
        """注入即降级：``online=False`` / ``synthetic=True`` 自动生效。"""
        by_symbol, trade_dates = pool
        provs = _providers(by_symbol)
        updater = _build_updater(app_cfg, tmp_path / "data", provs)

        assert updater.online is False
        assert updater.synthetic is True

        result = _update(updater, ["000001"], trade_dates)
        assert result.online is False
        assert result.synthetic is True
        assert result.to_dict()["online"] is False
        assert any("注入" in n for n in result.notes)

    def test_explicit_provenance_override_is_disclosed(
        self, app_cfg, pool, tmp_path
    ):
        """允许显式覆盖出处标记，但必须在运行说明里自报家门。"""
        by_symbol, trade_dates = pool
        provs = _providers(by_symbol)
        updater = _build_updater(
            app_cfg, tmp_path / "data", provs, online=True, synthetic=False
        )

        result = _update(updater, ["000001"], trade_dates)

        assert result.online is True
        assert result.synthetic is False
        assert any("显式声明" in n for n in result.notes)

    def test_manifest_provenance_marks_injection(self, app_cfg, pool, tmp_path):
        """即便调用方谎报 online，清单里的 ``provider_injected`` 也拆穿它。"""
        by_symbol, trade_dates = pool
        provs = _providers(by_symbol)
        updater = _build_updater(
            app_cfg, tmp_path / "data", provs, online=True, synthetic=False
        )

        result = _update(updater, ["000001"], trade_dates)
        payload = json.loads(
            Path(result.outcomes[0].manifest_path).read_text(encoding="utf-8")
        )
        assert payload["provenance"]["provider_injected"] is True

    def test_result_to_dict_exposes_failures_verbatim(
        self, app_cfg, pool, tmp_path
    ):
        """审计摘要必须原样透出失败明细，不得美化。"""
        by_symbol, trade_dates = pool
        provs = _providers(
            by_symbol,
            primary_kwargs={"fail_symbols": ("000002",)},
            fallback_kwargs={"fail_symbols": ("000002",)},
        )
        updater = _build_updater(app_cfg, tmp_path / "data", provs)

        payload = _update(updater, ["000001", "000002"], trade_dates).to_dict()

        assert payload["symbols_requested"] == 2
        assert payload["symbols_succeeded"] == 1
        assert payload["symbols_failed"] == 1
        assert payload["failed_symbols"] == ["000002"]
        assert len(payload["outcomes"]) == 2
        assert payload["updater"] == "fetch-manager"
        # 摘要必须能被 JSON 序列化，否则写不进审计产物
        json.dumps(payload, ensure_ascii=False)


# ====================================================================== #
# 7. 主数据与基准（best-effort，不拖垮当日运行）
# ====================================================================== #


class TestMasterAndBenchmark:
    def test_security_master_updated_when_enabled(self, app_cfg, pool, tmp_path):
        """启用后应抓取并标准化证券主数据。"""
        by_symbol, trade_dates = pool
        master = _akshare_master(sorted(by_symbol))
        provs = _providers(by_symbol)
        provs[SOURCE_AKSHARE]._master = master
        updater = _build_updater(
            app_cfg, tmp_path / "data", provs, update_security_master=True
        )

        result = _update(updater, ["000001"], trade_dates)

        assert result.security_master is not None
        assert len(result.security_master) == len(by_symbol)
        assert result.to_dict()["security_master_rows"] == len(by_symbol)

    def test_security_master_failure_is_note_not_fatal(
        self, app_cfg, pool, tmp_path
    ):
        """主数据抓不到只记一笔，绝不因此让整条流水线躺倒。"""
        by_symbol, trade_dates = pool
        provs = _providers(by_symbol)  # 两个提供器都没有 master
        updater = _build_updater(
            app_cfg, tmp_path / "data", provs, update_security_master=True
        )

        result = _update(updater, ["000001"], trade_dates)

        assert result.any_success is True
        assert result.security_master is None
        assert any("证券主数据" in n for n in result.notes)

    def test_benchmark_symbols_updated_separately(self, app_cfg, pool, tmp_path):
        """基准行情单独成表，不混进选股池。"""
        by_symbol, trade_dates = pool
        provs = _providers(by_symbol)
        updater = _build_updater(
            app_cfg, tmp_path / "data", provs, benchmark_symbols=("000008",)
        )

        result = _update(updater, ["000001"], trade_dates)

        assert result.benchmark is not None
        assert set(result.benchmark["symbol"].unique()) == {"000008"}
        assert set(result.quotes["symbol"].unique()) == {"000001"}
        assert result.to_dict()["benchmark_rows"] > 0

    def test_benchmark_failure_noted_not_fatal(self, app_cfg, pool, tmp_path):
        """基准抓取失败同样是 best-effort。"""
        by_symbol, trade_dates = pool
        provs = _providers(
            by_symbol,
            primary_kwargs={"fail_symbols": ("000008",)},
            fallback_kwargs={"fail_symbols": ("000008",)},
        )
        updater = _build_updater(
            app_cfg, tmp_path / "data", provs, benchmark_symbols=("000008",)
        )

        result = _update(updater, ["000001"], trade_dates)

        assert result.benchmark is None
        assert result.any_success is True
        assert any("基准行情" in n for n in result.notes)


# ====================================================================== #
# 8. AutoUpdatingDataSource：接到 Phase 4 管线
# ====================================================================== #


class TestAutoUpdatingDataSource:
    def test_data_source_wraps_bundle_with_provenance(
        self, app_cfg, pool, tmp_path
    ):
        """包装成 MarketDataBundle 后出处标记必须原样传递。"""
        by_symbol, trade_dates = pool
        provs = _providers(by_symbol)
        updater = _build_updater(app_cfg, tmp_path / "data", provs)
        source = AutoUpdatingDataSource(updater)

        bundle = source.load(
            symbols=["000001"],
            start=trade_dates[0],
            end=trade_dates[-1],
            as_of=trade_dates[-1],
        )

        assert source.name == "auto-update:fetch-manager"
        assert bundle.source == "auto-update:fetch-manager"
        assert bundle.online is False
        assert bundle.synthetic is True
        assert bundle.covers(trade_dates[-1])
        prov = bundle.provenance()
        assert prov["online"] is False and prov["synthetic"] is True

    def test_data_source_enforces_coverage(self, app_cfg, pool, tmp_path):
        """更新后仍未覆盖业务日 => 跳过，绝不拿陈旧数据充数。"""
        by_symbol, trade_dates = pool
        provs = _providers(by_symbol)
        updater = _build_updater(app_cfg, tmp_path / "data", provs)
        source = AutoUpdatingDataSource(updater)

        future = trade_dates[-1] + timedelta(days=30)
        with pytest.raises(DataUnavailableError) as exc:
            source.load(
                symbols=["000001"],
                start=trade_dates[0],
                end=future,
                as_of=future,
            )
        assert "未覆盖业务日" in str(exc.value)

    def test_coverage_check_can_be_disabled(self, app_cfg, pool, tmp_path):
        """显式关闭覆盖检查时允许历史回补场景。"""
        by_symbol, trade_dates = pool
        provs = _providers(by_symbol)
        updater = _build_updater(app_cfg, tmp_path / "data", provs)
        source = AutoUpdatingDataSource(updater, enforce_coverage=False)

        future = trade_dates[-1] + timedelta(days=30)
        bundle = source.load(
            symbols=["000001"], start=trade_dates[0], end=future, as_of=future
        )
        assert not bundle.covers(future)
        assert bundle.latest_date() == trade_dates[-1]

    def test_last_result_exposed_for_audit(self, app_cfg, pool, tmp_path):
        """``last_result`` 是 daily.py 写审计产物的唯一入口。"""
        by_symbol, trade_dates = pool
        provs = _providers(by_symbol)
        updater = _build_updater(app_cfg, tmp_path / "data", provs)
        source = AutoUpdatingDataSource(updater)

        assert source.last_result is None
        source.load(
            symbols=["000001", "000003"],
            start=trade_dates[0],
            end=trade_dates[-1],
            as_of=trade_dates[-1],
        )
        assert isinstance(source.last_result, DataUpdateResult)
        assert len(source.last_result.to_dict()["outcomes"]) == 2

    def test_factory_builds_auto_updating_source(self, app_cfg, pool, tmp_path):
        """便捷工厂产出的对象应与手工组装等价。"""
        by_symbol, trade_dates = pool
        provs = _providers(by_symbol)
        source = build_updating_data_source(
            app_cfg,
            data_dir=tmp_path / "data",
            provider_factory=lambda name: provs[name],
            update_security_master=False,
            code_commit="fr20-test-commit",
        )

        assert isinstance(source, AutoUpdatingDataSource)
        bundle = source.load(
            symbols=["000001"],
            start=trade_dates[0],
            end=trade_dates[-1],
            as_of=trade_dates[-1],
        )
        assert bundle.online is False

    def test_local_parquet_source_stays_offline_only(self, tmp_path):
        """``LocalParquetDataSource`` 的定位不变：永远离线，只消费不抓取。"""
        src = LocalParquetDataSource(tmp_path / "nowhere")
        assert src.name == "local-parquet"
        assert not hasattr(src, "last_result")
        with pytest.raises(DataUnavailableError):
            src.load(
                symbols=["000001"],
                start=date(2020, 1, 2),
                end=date(2020, 6, 30),
                as_of=date(2020, 6, 30),
            )


# ====================================================================== #
# 9. 每日管线集成：跳过 / 失败 / 审计产物
# ====================================================================== #


def _automation_config(base_dir: Path, symbols, *, allow_skip: bool = True):
    return AutomationConfig(
        paths=PathsConfig(
            data_dir="data",
            state_dir="state",
            reports_dir="reports",
            logs_dir="logs",
            archive_dir="reports/archive",
        ),
        data=DataConfig(
            symbols=list(symbols),
            lookback_days=400,
            allow_skip_when_unavailable=allow_skip,
        ),
        logging=LoggingConfig(console=False),
        accounts=[
            AccountConfig(
                account_id="paper-steady",
                track=StrategyTrack.STEADY,
                initial_cash=1000.0,
                eligibility_status=EligibilityStatus.NOT_ELIGIBLE_FOR_LIVE_TRADING,
            ),
            AccountConfig(
                account_id="paper-aggressive",
                track=StrategyTrack.AGGRESSIVE,
                initial_cash=1000.0,
                eligibility_status=EligibilityStatus.SIMULATION_ONLY,
            ),
        ],
    ).with_base_dir(base_dir)


def _steps(outcome) -> dict:
    return {s.name: s for s in outcome.record.steps}


class TestDailyIntegration:
    def test_daily_skips_when_double_source_failure(self, app_cfg, pool, tmp_path):
        """双源失败 + 允许跳过 => SKIPPED_DATA_UNAVAILABLE（退出码 0）。"""
        from ashare_quant.automation.daily import DailyPipeline, run_daily

        by_symbol, trade_dates = pool
        provs = _providers(
            by_symbol,
            primary_kwargs={"fail_quotes": -1},
            fallback_kwargs={"fail_quotes": -1},
        )
        config = _automation_config(tmp_path, ["000001"], allow_skip=True)
        updater = _build_updater(app_cfg, config.data_dir, provs)
        source = AutoUpdatingDataSource(updater)
        cal = TradingCalendar.from_dates(trade_dates, source="synthetic-calendar")

        out = run_daily(
            config,
            as_of_date=trade_dates[-1],
            data_source=source,
            pipeline=DailyPipeline(app_config=app_cfg, calendar=cal),
            state_store=StateStore(config.state_dir),
        )

        assert out.state is RunState.SKIPPED_DATA_UNAVAILABLE
        assert out.exit_code == 0
        step = _steps(out)["market_data"]
        assert step.detail.get("allow_skip_when_unavailable") is True

    def test_daily_fails_when_skip_not_allowed(self, app_cfg, pool, tmp_path):
        """双源失败 + 禁止跳过 => FAILED（退出码 1），告警必须响。

        这是 FR-20 激活的死配置：在此之前
        ``data.allow_skip_when_unavailable`` 从未被任何代码消费过。
        """
        from ashare_quant.automation.daily import DailyPipeline, run_daily

        by_symbol, trade_dates = pool
        provs = _providers(
            by_symbol,
            primary_kwargs={"fail_quotes": -1},
            fallback_kwargs={"fail_quotes": -1},
        )
        config = _automation_config(tmp_path, ["000001"], allow_skip=False)
        updater = _build_updater(app_cfg, config.data_dir, provs)
        source = AutoUpdatingDataSource(updater)
        cal = TradingCalendar.from_dates(trade_dates, source="synthetic-calendar")

        out = run_daily(
            config,
            as_of_date=trade_dates[-1],
            data_source=source,
            pipeline=DailyPipeline(app_config=app_cfg, calendar=cal),
            state_store=StateStore(config.state_dir),
        )

        assert out.state is RunState.FAILED
        assert out.exit_code == 1
        step = _steps(out)["market_data"]
        assert step.detail.get("allow_skip_when_unavailable") is False
        assert "allow_skip_when_unavailable=false" in (step.error or "")

    def test_daily_success_writes_data_update_artifact(
        self, app_cfg, pool, tmp_path
    ):
        """跑通全流程：数据更新明细写入 ``data-update.json`` 与步骤详情。"""
        from ashare_quant.automation.daily import DailyPipeline, run_daily

        by_symbol, trade_dates = pool
        symbols = ["000001", "000002", "000003", "000008"]
        provs = _providers(by_symbol)
        config = _automation_config(tmp_path, symbols, allow_skip=True)
        updater = _build_updater(app_cfg, config.data_dir, provs)
        source = AutoUpdatingDataSource(updater)
        cal = TradingCalendar.from_dates(trade_dates, source="synthetic-calendar")

        out = run_daily(
            config,
            as_of_date=trade_dates[-1],
            data_source=source,
            pipeline=DailyPipeline(
                app_config=app_cfg,
                calendar=cal,
                universe_kwargs={"min_turnover": 0.0, "min_listing_days": 120},
            ),
            state_store=StateStore(config.state_dir),
        )

        assert out.state is RunState.SUCCESS, _steps(out)

        # 步骤详情里带摘要，但剔除了冗长的逐标的 outcomes
        detail = _steps(out)["market_data"].detail["data_update"]
        assert detail["updater"] == "fetch-manager"
        assert detail["symbols_succeeded"] == len(symbols)
        assert "outcomes" not in detail

        # 完整明细落在独立审计产物里
        artifacts = list(config.reports_dir.rglob("data-update.json"))
        assert len(artifacts) == 1, "每次运行应恰好产出一份数据更新审计"
        payload = json.loads(artifacts[0].read_text(encoding="utf-8"))
        assert payload["online"] is False
        assert payload["synthetic"] is True
        assert len(payload["outcomes"]) == len(symbols)
        assert all(o["curated_sha256"] for o in payload["outcomes"])
        assert len(payload["manifests"]) == len(symbols)

    def test_daily_without_updater_writes_no_artifact(
        self, app_cfg, pool, tmp_path
    ):
        """本地消费模式（无更新器）不应产出空壳 ``data-update.json``。"""
        from ashare_quant.automation.daily import DailyPipeline, run_daily
        from ashare_quant.automation.datasource import InjectedDataSource

        _, trade_dates = pool
        quotes = make_research_quotes(start=date(2020, 1, 2), n_days=200, n_stocks=8)
        config = _automation_config(tmp_path, [], allow_skip=True)
        source = InjectedDataSource(quotes, name="synthetic", synthetic=True)
        cal = TradingCalendar.from_dates(trade_dates, source="synthetic-calendar")

        out = run_daily(
            config,
            as_of_date=trade_dates[-1],
            data_source=source,
            pipeline=DailyPipeline(
                app_config=app_cfg,
                calendar=cal,
                universe_kwargs={"min_turnover": 0.0, "min_listing_days": 120},
            ),
            state_store=StateStore(config.state_dir),
        )

        assert out.state is RunState.SUCCESS
        assert "data_update" not in _steps(out)["market_data"].detail
        assert list(config.reports_dir.rglob("data-update.json")) == []


# ====================================================================== #
# 10. 数据结构自检
# ====================================================================== #


class TestResultObjects:
    def test_symbol_outcome_dedupes_sources_tried(self):
        """``sources_tried`` 按首次出现去重，别把 4 次重试记成 4 个数据源。"""
        outcome = SymbolUpdateOutcome(
            symbol="000001",
            success=True,
            attempts=[
                {"source": SOURCE_AKSHARE, "attempt_number": 1, "success": False},
                {"source": SOURCE_AKSHARE, "attempt_number": 2, "success": False},
                {"source": SOURCE_BAOSTOCK, "attempt_number": 1, "success": True},
            ],
        )
        assert outcome.attempt_count == 3
        assert outcome.sources_tried == [SOURCE_AKSHARE, SOURCE_BAOSTOCK]

    def test_result_all_failed_flag(self):
        """空 outcomes 不算"全失败"，避免把"没跑"误报成"全灭"。"""
        empty = DataUpdateResult(
            updater="x",
            as_of=date(2020, 6, 30),
            start=date(2020, 1, 2),
            end=date(2020, 6, 30),
            quotes=pd.DataFrame(),
            online=False,
        )
        assert empty.all_failed is False
        assert empty.any_success is False
        assert empty.row_count == 0

        failed = DataUpdateResult(
            updater="x",
            as_of=date(2020, 6, 30),
            start=date(2020, 1, 2),
            end=date(2020, 6, 30),
            quotes=pd.DataFrame(),
            online=False,
            outcomes=[SymbolUpdateOutcome(symbol="000001", success=False)],
        )
        assert failed.all_failed is True
