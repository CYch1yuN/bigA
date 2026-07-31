"""Phase 3 滚动训练/验证/测试切分模块 ``walk_forward`` 的综合 pytest 测试。

覆盖范围（对应 Phase 3 验收测试项 9：训练、验证、测试日期严格分离；测试期数据
变化不能改变训练期参数）：

1. 日历年切分：数据覆盖完整日历年时按年边界切分；
2. 244 交易日块回退：日历年不可用时按 244 交易日/年近似切分；
3. 各折测试期互不重叠（step_years >= test_years 保证）；
4. 验证期为训练期最后 N 个月；
5. get_train_dates 排除验证期日期；
6. get_validation_dates 仅返回验证期日期；
7. get_test_dates 仅返回测试期日期；
8. 训练/验证/测试日期严格分离（无交集，且时间上严格递进）；
9. is_insufficient_sample 在样本不足 min_total_years 时返回 True；
10. 非法配置抛出 ValueError；
11. 空日期列表返回空折列表；
12. 折按 fold_id 升序排列。

核心守卫（测试项 9）：切分严格按日期完成，参数只在对应训练/验证数据上选择。
本测试通过证明“测试期数据的变化不会改变某一折的训练/验证日期集”来锁定该
协议——一旦折边界固定，训练期可用的数据对测试期数据完全不变，因此测试期
数据变化无法回流影响训练期参数。
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from ashare_quant.research.walk_forward import (
    TRADING_DAYS_PER_YEAR,
    Fold,
    WalkForwardConfig,
    WalkForwardSplitter,
)
from tests.backtest_samples import make_trade_dates
from tests.research_samples import make_trade_dates_range

# --------------------------------------------------------------------------- #
# 常量与测试数据构建器
# --------------------------------------------------------------------------- #

# 交易年近似交易日数（与模块保持一致，断言时直接引用）。
_EXPECTED_TRADING_DAYS_PER_YEAR = 244

# 244 交易日块回退路径使用的配置：缩小训练/测试年数，便于用少量数据生成多折。
BLOCK_CONFIG = WalkForwardConfig(
    train_years=1,
    validation_months=3,
    test_years=1,
    step_years=1,
    min_total_years=2,
)


def _make_no_complete_year_dates(start_year: int, end_year: int) -> list[date]:
    """生成 start_year..end_year 每年 4-12 月的交易日列表。

    每年首个交易日落在 4 月（month > 3），因此 ``_complete_calendar_years``
    判定这些年均非完整日历年，从而强制切分器走 244 交易日块回退路径。
    各年日期按年升序拼接，整体已升序。
    """
    dates: list[date] = []
    for y in range(start_year, end_year + 1):
        dates.extend(make_trade_dates_range(date(y, 4, 1), date(y, 12, 31)))
    return dates


def _build_scenario(name: str) -> tuple[list[date], WalkForwardConfig, WalkForwardSplitter, list[Fold]]:
    """构建一个切分场景，返回 (dates, config, splitter, folds)。

    - ``calendar``：5 个完整日历年 + 默认配置 -> 走日历年切分路径，2 折。
    - ``block``：6 年 4-12 月不完整日历年 + BLOCK_CONFIG -> 走 244 交易日块路径，3 折。
    """
    if name == "calendar":
        dates = make_trade_dates_range(date(2019, 1, 2), date(2023, 12, 31))
        config = WalkForwardConfig()
    elif name == "block":
        dates = _make_no_complete_year_dates(2020, 2025)
        config = BLOCK_CONFIG
    else:  # pragma: no cover - 防御性分支
        raise ValueError(f"未知场景: {name}")
    splitter = WalkForwardSplitter(config)
    folds = splitter.split(dates)
    return dates, config, splitter, folds


def _first_missing_date_in(start: date, end: date, existing: list[date]) -> date | None:
    """返回 [start, end] 内首个不在 existing 中的日期（用于模拟测试期数据新增）。"""
    have = set(existing)
    cur = start
    while cur <= end:
        if cur not in have:
            return cur
        cur += timedelta(days=1)
    return None


# --------------------------------------------------------------------------- #
# 1. 配置校验与默认值
# --------------------------------------------------------------------------- #


class TestWalkForwardConfig:
    """``WalkForwardConfig`` 的默认值与非法参数校验。"""

    def test_default_config_values(self):
        """默认配置应与协议约定一致。"""
        cfg = WalkForwardConfig()
        assert cfg.train_years == 3
        assert cfg.validation_months == 6
        assert cfg.test_years == 1
        assert cfg.step_years == 1
        assert cfg.min_total_years == 5

    @pytest.mark.parametrize(
        "train_years, test_years",
        [(0, 1), (1, 0), (-1, 1), (1, -2)],
    )
    def test_non_positive_train_or_test_years_raise(self, train_years, test_years):
        """train_years / test_years 非正时必须抛 ValueError。"""
        with pytest.raises(ValueError):
            WalkForwardConfig(train_years=train_years, test_years=test_years)

    @pytest.mark.parametrize(
        "test_years, step_years",
        [(2, 1), (3, 2), (1, 0)],
    )
    def test_step_less_than_test_years_raises(self, test_years, step_years):
        """step_years < test_years 会导致测试期重叠，必须抛 ValueError。"""
        with pytest.raises(ValueError):
            WalkForwardConfig(test_years=test_years, step_years=step_years)

    @pytest.mark.parametrize(
        "train_years, validation_months",
        [(1, 0), (1, 13), (2, 25), (1, -1)],
    )
    def test_invalid_validation_months_raise(self, train_years, validation_months):
        """validation_months < 1 或超过训练期长度时必须抛 ValueError。"""
        with pytest.raises(ValueError):
            WalkForwardConfig(
                train_years=train_years, validation_months=validation_months,
            )

    @pytest.mark.parametrize("min_total_years", [0, -1, -5])
    def test_invalid_min_total_years_raises(self, min_total_years):
        """min_total_years 非正时必须抛 ValueError。"""
        with pytest.raises(ValueError):
            WalkForwardConfig(min_total_years=min_total_years)

    @pytest.mark.parametrize(
        "train_years, validation_months, test_years, step_years, min_total_years",
        [
            (2, 6, 1, 1, 4),
            (1, 12, 1, 1, 1),  # validation_months 恰好等于训练期长度（边界合法）
            (3, 6, 2, 2, 5),   # step_years == test_years（边界合法）
            (5, 1, 1, 3, 7),   # step_years > test_years（合法，留间隔）
        ],
    )
    def test_valid_boundary_configs(
        self, train_years, validation_months, test_years, step_years, min_total_years,
    ):
        """合法配置（含边界）应成功构造，不抛异常。"""
        cfg = WalkForwardConfig(
            train_years=train_years,
            validation_months=validation_months,
            test_years=test_years,
            step_years=step_years,
            min_total_years=min_total_years,
        )
        assert cfg.train_years == train_years
        assert cfg.step_years >= cfg.test_years


# --------------------------------------------------------------------------- #
# 2. 样本不足检测
# --------------------------------------------------------------------------- #


class TestInsufficientSample:
    """``is_insufficient_sample`` 在日历年路径与 244 交易日块路径下的判定。"""

    def test_empty_dates_insufficient(self):
        """空日期列表必然样本不足。"""
        splitter = WalkForwardSplitter()
        assert splitter.is_insufficient_sample([]) is True

    def test_few_complete_calendar_years_insufficient(self):
        """完整日历年数 < min_total_years 时判定为不足（日历年路径）。"""
        splitter = WalkForwardSplitter()  # min_total_years=5
        # 3 个完整日历年
        dates3 = make_trade_dates_range(date(2019, 1, 2), date(2021, 12, 31))
        assert splitter.is_insufficient_sample(dates3) is True
        # 4 个完整日历年仍 < 5
        dates4 = make_trade_dates_range(date(2019, 1, 2), date(2022, 12, 31))
        assert splitter.is_insufficient_sample(dates4) is True

    def test_enough_complete_calendar_years_sufficient(self):
        """完整日历年数 >= min_total_years 时判定为充足（日历年路径）。"""
        splitter = WalkForwardSplitter()  # min_total_years=5
        # 恰好 5 个完整日历年
        dates5 = make_trade_dates_range(date(2019, 1, 2), date(2023, 12, 31))
        assert splitter.is_insufficient_sample(dates5) is False
        # 6 个完整日历年
        dates6 = make_trade_dates_range(date(2019, 1, 2), date(2024, 12, 31))
        assert splitter.is_insufficient_sample(dates6) is False

    def test_few_trading_days_insufficient(self):
        """无完整日历年且交易日数 < min_total_years*244 时判定为不足（回退路径）。"""
        splitter = WalkForwardSplitter()  # 阈值 = 5 * 244 = 1220
        # 2020 年 6 月起 65 个交易日，全部落在 2020 年且首个交易日为 6 月（非完整日历年）
        dates = make_trade_dates(date(2020, 6, 1), 65)
        assert len(dates) == 65
        assert len(dates) < 1220
        assert splitter.is_insufficient_sample(dates) is True

    def test_enough_trading_days_sufficient(self):
        """无完整日历年但交易日数 >= min_total_years*244 时判定为充足（回退路径）。"""
        # 自定义 min_total_years=2 -> 阈值 = 2 * 244 = 488
        splitter = WalkForwardSplitter(
            WalkForwardConfig(min_total_years=2)
        )
        # 4 年 4-12 月，无完整日历年，约 760+ 个交易日
        dates = _make_no_complete_year_dates(2020, 2023)
        assert len(dates) >= 488
        assert splitter.is_insufficient_sample(dates) is False

    def test_custom_min_total_years_boundary(self):
        """自定义 min_total_years 的边界：恰好等于 -> 充足；少一 -> 不足。"""
        cfg_at = WalkForwardConfig(min_total_years=4)
        cfg_below = WalkForwardConfig(min_total_years=5)
        dates = make_trade_dates_range(date(2019, 1, 2), date(2022, 12, 31))
        # 4 个完整日历年
        assert WalkForwardSplitter(cfg_at).is_insufficient_sample(dates) is False
        assert WalkForwardSplitter(cfg_below).is_insufficient_sample(dates) is True


# --------------------------------------------------------------------------- #
# 3. 日历年切分
# --------------------------------------------------------------------------- #


class TestCalendarYearSplit:
    """完整日历年可用时按年边界切分。"""

    def test_split_uses_calendar_year_boundaries(self):
        """训练/测试期起止应落在 1 月 1 日 / 12 月 31 日年边界上。"""
        dates = make_trade_dates_range(date(2019, 1, 2), date(2023, 12, 31))
        splitter = WalkForwardSplitter()
        folds = splitter.split(dates)
        assert len(folds) == 2

        # 折 0：训练 2019-2021，测试 2022
        f0 = folds[0]
        assert f0.train_start == date(2019, 1, 1)
        assert f0.train_end == date(2021, 12, 31)
        assert f0.test_start == date(2022, 1, 1)
        assert f0.test_end == date(2022, 12, 31)

        # 折 1：训练 2020-2022，测试 2023
        f1 = folds[1]
        assert f1.train_start == date(2020, 1, 1)
        assert f1.train_end == date(2022, 12, 31)
        assert f1.test_start == date(2023, 1, 1)
        assert f1.test_end == date(2023, 12, 31)

    def test_fold_ids_sorted_and_sequential_from_zero(self):
        """fold_id 从 0 起递增，折列表按 fold_id 升序。"""
        dates = make_trade_dates_range(date(2019, 1, 2), date(2026, 12, 31))
        splitter = WalkForwardSplitter()
        folds = splitter.split(dates)
        assert [f.fold_id for f in folds] == list(range(len(folds)))
        assert folds == sorted(folds, key=lambda f: f.fold_id)

    def test_train_end_equals_validation_end(self):
        """验证期截止日等于训练期截止日（验证期是训练期最后一段）。"""
        dates = make_trade_dates_range(date(2019, 1, 2), date(2023, 12, 31))
        splitter = WalkForwardSplitter()
        for fold in splitter.split(dates):
            assert fold.validation_end == fold.train_end

    def test_validation_is_last_six_months_of_training(self):
        """默认 6 个月验证期 = 训练期最后一年 7-12 月。"""
        dates = make_trade_dates_range(date(2019, 1, 2), date(2023, 12, 31))
        splitter = WalkForwardSplitter()
        folds = splitter.split(dates)
        f0 = folds[0]
        # 训练期最后一年为 2021，验证期应为 2021-07-01 ~ 2021-12-31
        assert f0.validation_start == date(2021, 7, 1)
        assert f0.validation_end == date(2021, 12, 31)
        assert f0.train_start < f0.validation_start <= f0.train_end
        # 验证期为训练期最后 6 个日历月
        assert f0.validation_start == date(f0.train_end.year, 7, 1)

    def test_exact_boundary_yields_single_fold(self):
        """恰好 train_years+test_years 个完整日历年时只生成 1 折。"""
        # 4 个完整日历年 = train(3) + test(1)
        dates = make_trade_dates_range(date(2019, 1, 2), date(2022, 12, 31))
        splitter = WalkForwardSplitter()
        folds = splitter.split(dates)
        assert len(folds) == 1
        assert folds[0].train_start == date(2019, 1, 1)
        assert folds[0].test_end == date(2022, 12, 31)

    def test_test_period_strictly_after_training(self):
        """测试期起始 = 训练期截止次年 1 月 1 日，严格晚于训练期。"""
        dates = make_trade_dates_range(date(2019, 1, 2), date(2023, 12, 31))
        splitter = WalkForwardSplitter()
        for fold in splitter.split(dates):
            assert fold.test_start > fold.train_end
            assert fold.test_start == fold.train_end + timedelta(days=1)


# --------------------------------------------------------------------------- #
# 4. 244 交易日块切分（回退路径）
# --------------------------------------------------------------------------- #


class TestTradingDayBlockSplit:
    """日历年不可用时的 244 交易日块回退切分。"""

    def test_fallback_produces_folds_when_no_complete_years(self):
        """无完整日历年时回退到 244 交易日块，仍能生成多折。"""
        dates = _make_no_complete_year_dates(2020, 2025)
        splitter = WalkForwardSplitter(BLOCK_CONFIG)
        folds = splitter.split(dates)
        assert len(folds) >= 2  # 期望 3 折
        assert [f.fold_id for f in folds] == list(range(len(folds)))

    def test_train_block_has_244_days_per_year(self):
        """训练期（含验证）交易日数 = train_years * 244。"""
        dates = _make_no_complete_year_dates(2020, 2025)
        splitter = WalkForwardSplitter(BLOCK_CONFIG)
        fold = splitter.split(dates)[0]
        train_dates = splitter.get_train_dates(dates, fold)
        val_dates = splitter.get_validation_dates(dates, fold)
        expected = BLOCK_CONFIG.train_years * _EXPECTED_TRADING_DAYS_PER_YEAR
        assert len(train_dates) + len(val_dates) == expected

    def test_test_block_has_244_days_per_year(self):
        """测试期交易日数 = test_years * 244。"""
        dates = _make_no_complete_year_dates(2020, 2025)
        splitter = WalkForwardSplitter(BLOCK_CONFIG)
        fold = splitter.split(dates)[0]
        test_dates = splitter.get_test_dates(dates, fold)
        expected = BLOCK_CONFIG.test_years * _EXPECTED_TRADING_DAYS_PER_YEAR
        assert len(test_dates) == expected

    def test_validation_block_size_matches_month_ratio(self):
        """验证期交易日数 = round(validation_months / 12 * train_days)。"""
        dates = _make_no_complete_year_dates(2020, 2025)
        splitter = WalkForwardSplitter(BLOCK_CONFIG)
        fold = splitter.split(dates)[0]
        val_dates = splitter.get_validation_dates(dates, fold)
        train_days = BLOCK_CONFIG.train_years * _EXPECTED_TRADING_DAYS_PER_YEAR
        expected_val = max(1, int(round(BLOCK_CONFIG.validation_months / 12 * train_days)))
        assert len(val_dates) == expected_val
        # BLOCK_CONFIG: 3/12 * 244 = 61
        assert expected_val == 61

    def test_test_start_immediately_follows_train_end_in_trading_list(self):
        """测试期首个交易日是训练期截止日之后紧邻的下一个交易日。"""
        dates = _make_no_complete_year_dates(2020, 2025)
        splitter = WalkForwardSplitter(BLOCK_CONFIG)
        fold = splitter.split(dates)[0]
        assert fold.test_start > fold.train_end
        # 训练截止日与测试起始日之间不存在任何列表内交易日
        between = [d for d in dates if fold.train_end < d < fold.test_start]
        assert between == []


# --------------------------------------------------------------------------- #
# 5. 训练/验证/测试日期严格分离（含测试项 9 核心守卫）
# --------------------------------------------------------------------------- #


class TestDateSeparation:
    """训练、验证、测试日期严格分离；测试期数据变化不能改变训练期参数。"""

    @pytest.mark.parametrize("scenario_name", ["calendar", "block"])
    def test_train_validation_test_pairwise_disjoint(self, scenario_name):
        """每折训练/验证/测试三组日期两两无交集。"""
        dates, _config, splitter, folds = _build_scenario(scenario_name)
        assert folds, "场景应至少生成一折"
        for fold in folds:
            train_set = set(splitter.get_train_dates(dates, fold))
            val_set = set(splitter.get_validation_dates(dates, fold))
            test_set = set(splitter.get_test_dates(dates, fold))
            assert train_set.isdisjoint(val_set)
            assert train_set.isdisjoint(test_set)
            assert val_set.isdisjoint(test_set)

    @pytest.mark.parametrize("scenario_name", ["calendar", "block"])
    def test_strict_chronological_order(self, scenario_name):
        """三组日期在时间上严格递进：max(train) < min(val) < max(val) < min(test)。"""
        dates, _config, splitter, folds = _build_scenario(scenario_name)
        fold = folds[0]
        train_dates = splitter.get_train_dates(dates, fold)
        val_dates = splitter.get_validation_dates(dates, fold)
        test_dates = splitter.get_test_dates(dates, fold)
        assert train_dates and val_dates and test_dates
        assert max(train_dates) < min(val_dates)
        assert max(val_dates) < min(test_dates)
        assert max(train_dates) < min(test_dates)

    @pytest.mark.parametrize("scenario_name", ["calendar", "block"])
    def test_test_data_changes_do_not_alter_train_dates(self, scenario_name):
        """测试项 9 核心：测试期数据变化不能改变训练期参数。

        一旦折边界固定，训练/验证日期集由训练期边界决定，与测试期数据无关。
        分别通过“删除全部测试期日期”与“在测试期新增日期”两种变化验证训练/
        验证日期集保持不变——即测试期数据无法回流影响训练期可用数据。
        """
        dates, _config, splitter, folds = _build_scenario(scenario_name)
        fold = folds[0]
        train_before = splitter.get_train_dates(dates, fold)
        val_before = splitter.get_validation_dates(dates, fold)
        test_before = splitter.get_test_dates(dates, fold)
        assert test_before  # 确保测试期非空

        # 变化一：删除全部测试期日期
        no_test = [d for d in dates if not (fold.test_start <= d <= fold.test_end)]
        assert splitter.get_train_dates(no_test, fold) == train_before
        assert splitter.get_validation_dates(no_test, fold) == val_before

        # 变化二：在测试期日历区间内新增一个原本不存在的日期
        extra = _first_missing_date_in(fold.test_start, fold.test_end, dates)
        assert extra is not None, "测试期日历区间内应存在可新增的日期"
        extended = sorted(set(dates) | {extra})
        assert splitter.get_train_dates(extended, fold) == train_before
        assert splitter.get_validation_dates(extended, fold) == val_before
        # 新增日期应落入测试期（证明变化确实发生在测试期，而非训练期）
        assert fold.test_start <= extra <= fold.test_end

    @pytest.mark.parametrize("scenario_name", ["calendar", "block"])
    def test_get_train_dates_excludes_validation_period(self, scenario_name):
        """get_train_dates 返回的训练日期不包含任何验证期日期。"""
        dates, _config, splitter, folds = _build_scenario(scenario_name)
        fold = folds[0]
        train_dates = splitter.get_train_dates(dates, fold)
        for d in train_dates:
            assert not (fold.validation_start <= d <= fold.validation_end)
            assert fold.train_start <= d <= fold.train_end

    @pytest.mark.parametrize("scenario_name", ["calendar", "block"])
    def test_get_validation_and_test_dates_within_their_windows(self, scenario_name):
        """get_validation_dates / get_test_dates 仅返回各自窗口内的日期。"""
        dates, _config, splitter, folds = _build_scenario(scenario_name)
        fold = folds[0]
        for d in splitter.get_validation_dates(dates, fold):
            assert fold.validation_start <= d <= fold.validation_end
        for d in splitter.get_test_dates(dates, fold):
            assert fold.test_start <= d <= fold.test_end


# --------------------------------------------------------------------------- #
# 6. 各折测试期不重叠
# --------------------------------------------------------------------------- #


class TestNoOverlap:
    """step_years >= test_years 保证各折测试期互不重叠。"""

    @pytest.mark.parametrize("scenario_name", ["calendar", "block"])
    def test_test_periods_pairwise_disjoint(self, scenario_name):
        """所有折的测试期日期集合两两无交集。"""
        dates, _config, splitter, folds = _build_scenario(scenario_name)
        assert len(folds) >= 2
        test_sets = [set(splitter.get_test_dates(dates, f)) for f in folds]
        for i in range(len(test_sets)):
            for j in range(i + 1, len(test_sets)):
                assert test_sets[i].isdisjoint(test_sets[j]), (
                    f"折 {i} 与折 {j} 测试期重叠"
                )

    @pytest.mark.parametrize("scenario_name", ["calendar", "block"])
    def test_test_periods_strictly_increasing(self, scenario_name):
        """各折测试期起止时间随 fold_id 严格递增。"""
        dates, _config, splitter, folds = _build_scenario(scenario_name)
        assert len(folds) >= 2
        for prev, cur in zip(folds, folds[1:]):
            assert cur.test_start > prev.test_end
            assert cur.test_end > prev.test_end
            assert cur.test_start >= prev.test_start

    def test_step_equals_test_years_adjacent_no_gap_no_overlap(self):
        """step_years == test_years：日历年路径下测试期相邻、无间隔、无重叠。"""
        cfg = WalkForwardConfig(train_years=2, test_years=1, step_years=1,
                                validation_months=6, min_total_years=4)
        dates = make_trade_dates_range(date(2019, 1, 2), date(2024, 12, 31))
        splitter = WalkForwardSplitter(cfg)
        folds = splitter.split(dates)
        assert len(folds) >= 2
        test_sets = [set(splitter.get_test_dates(dates, f)) for f in folds]
        for i in range(len(folds) - 1):
            # 无重叠
            assert test_sets[i].isdisjoint(test_sets[i + 1])
            # 无间隔：下一折测试起始 = 当前折测试截止 + 1 天
            assert folds[i + 1].test_start == folds[i].test_end + timedelta(days=1)

    def test_step_greater_than_test_years_creates_gap_no_overlap(self):
        """step_years > test_years：测试期之间留有间隔但仍不重叠。"""
        cfg = WalkForwardConfig(train_years=2, test_years=1, step_years=2,
                                validation_months=6, min_total_years=4)
        dates = make_trade_dates_range(date(2019, 1, 2), date(2026, 12, 31))
        splitter = WalkForwardSplitter(cfg)
        folds = splitter.split(dates)
        assert len(folds) >= 2
        test_sets = [set(splitter.get_test_dates(dates, f)) for f in folds]
        for i in range(len(folds) - 1):
            assert test_sets[i].isdisjoint(test_sets[i + 1])
            # 留有间隔：下一折测试起始严格晚于当前折测试截止 + 1 天
            assert folds[i + 1].test_start > folds[i].test_end + timedelta(days=1)


# --------------------------------------------------------------------------- #
# 7. 验证期是训练期最后一段
# --------------------------------------------------------------------------- #


class TestValidationPeriod:
    """验证期定位为训练期最后 N 个月。"""

    @pytest.mark.parametrize("scenario_name", ["calendar", "block"])
    def test_validation_end_equals_train_end(self, scenario_name):
        """验证期截止日 == 训练期截止日。"""
        dates, _config, splitter, folds = _build_scenario(scenario_name)
        for fold in folds:
            assert fold.validation_end == fold.train_end

    @pytest.mark.parametrize("scenario_name", ["calendar", "block"])
    def test_validation_start_within_training(self, scenario_name):
        """验证期起始日落在 [train_start, train_end] 内。"""
        dates, _config, splitter, folds = _build_scenario(scenario_name)
        for fold in folds:
            assert fold.train_start <= fold.validation_start <= fold.train_end

    def test_validation_is_last_six_calendar_months(self):
        """日历年路径：6 个月验证期 = 训练期最后一年的 7-12 月。"""
        dates = make_trade_dates_range(date(2019, 1, 2), date(2023, 12, 31))
        splitter = WalkForwardSplitter()
        folds = splitter.split(dates)
        for fold in folds:
            last_train_year = fold.train_end.year
            assert fold.validation_start == date(last_train_year, 7, 1)
            assert fold.validation_end == date(last_train_year, 12, 31)

    @pytest.mark.parametrize("scenario_name", ["calendar", "block"])
    def test_validation_dates_subset_of_training_window(self, scenario_name):
        """所有验证期日期都落在训练期日历窗口内。"""
        dates, _config, splitter, folds = _build_scenario(scenario_name)
        for fold in folds:
            for d in splitter.get_validation_dates(dates, fold):
                assert fold.train_start <= d <= fold.train_end

    @pytest.mark.parametrize("scenario_name", ["calendar", "block"])
    def test_train_and_validation_partition_training_window(self, scenario_name):
        """训练日期 ∪ 验证日期 = 训练窗口内的全部交易日，且二者不相交。"""
        dates, _config, splitter, folds = _build_scenario(scenario_name)
        fold = folds[0]
        train_set = set(splitter.get_train_dates(dates, fold))
        val_set = set(splitter.get_validation_dates(dates, fold))
        in_window = {d for d in dates if fold.train_start <= d <= fold.train_end}
        assert train_set.isdisjoint(val_set)
        assert train_set | val_set == in_window


# --------------------------------------------------------------------------- #
# 8. 边界情况
# --------------------------------------------------------------------------- #


class TestEdgeCases:
    """空日期、单日历年、精确边界、未排序/重复日期等边界情况。"""

    def test_empty_dates_returns_empty_folds(self):
        """空日期列表应返回空折列表。"""
        splitter = WalkForwardSplitter()
        assert splitter.split([]) == []

    def test_empty_dates_is_insufficient(self):
        """空日期列表应判定为样本不足。"""
        splitter = WalkForwardSplitter()
        assert splitter.is_insufficient_sample([]) is True

    def test_single_calendar_year_yields_no_fold(self):
        """仅 1 个完整日历年无法构成 train+test，split 返回空。"""
        dates = make_trade_dates_range(date(2020, 1, 2), date(2020, 12, 31))
        splitter = WalkForwardSplitter()
        # 完整日历年数 1 < train(3)+test(1)=4 -> 回退路径；交易日数 < 976 -> 无完整折
        assert splitter.split(dates) == []

    def test_single_calendar_year_insufficient(self):
        """仅 1 个完整日历年 < min_total_years(5) -> 样本不足。"""
        dates = make_trade_dates_range(date(2020, 1, 2), date(2020, 12, 31))
        splitter = WalkForwardSplitter()
        assert splitter.is_insufficient_sample(dates) is True

    def test_min_total_years_exact_boundary(self):
        """完整日历年数恰好 == min_total_years 时为充足，少一则不足。"""
        dates5 = make_trade_dates_range(date(2019, 1, 2), date(2023, 12, 31))
        # 恰好 5 个完整日历年
        assert WalkForwardSplitter(
            WalkForwardConfig(min_total_years=5)
        ).is_insufficient_sample(dates5) is False
        assert WalkForwardSplitter(
            WalkForwardConfig(min_total_years=6)
        ).is_insufficient_sample(dates5) is True

    def test_unsorted_and_duplicate_dates_handled(self):
        """传入未排序、含重复的日期列表应与排序去重后结果一致。"""
        dates = make_trade_dates_range(date(2019, 1, 2), date(2023, 12, 31))
        splitter = WalkForwardSplitter()
        clean_folds = splitter.split(dates)

        # 构造未排序 + 含重复的输入（确定性，无随机）
        messy = list(reversed(dates)) + dates[:5] + [dates[0], dates[-1]]
        messy_folds = splitter.split(messy)

        assert len(messy_folds) == len(clean_folds)
        for cf, mf in zip(clean_folds, messy_folds):
            assert cf == mf
