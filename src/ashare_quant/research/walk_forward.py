"""滚动训练/验证/测试切分模块。

固定研究协议：

- 训练期 = 连续 ``train_years`` 个交易年。
- 验证期 = 训练期最后 ``validation_months`` 个月，仅用于候选参数选择；验证期
  之前的数据用于特征/参数拟合。
- 样本外测试期 = 紧随训练期之后的 ``test_years`` 个交易年。
- 步进 = ``step_years`` 个交易年。

各测试期不得重叠，所有测试折必须完整。参数只在对应训练/验证数据上选择，选定
后冻结并运行该折测试期；最终汇总只拼接各折样本外权益，不得用全样本重新挑选
参数覆盖样本外结果。

交易年优先按日历年边界（1月1日至12月31日）切分；当数据不足以构成完整日历年
时，按 244 交易日近似一个交易年。
"""
from __future__ import annotations

import calendar as _calendar
from dataclasses import dataclass
from datetime import date

# 交易年近似交易日数
TRADING_DAYS_PER_YEAR = 244


@dataclass
class Fold:
    """单个滚动折的日期边界。

    Attributes:
        fold_id: 折序号（从 0 开始）。
        train_start: 训练期起始日（含）。
        train_end: 训练期截止日（含）。
        validation_start: 验证期起始日（含），为训练期最后一段。
        validation_end: 验证期截止日（含），等于训练期截止日。
        test_start: 测试期起始日（含）。
        test_end: 测试期截止日（含）。
    """

    fold_id: int
    train_start: date
    train_end: date
    validation_start: date
    validation_end: date
    test_start: date
    test_end: date


@dataclass
class WalkForwardConfig:
    """滚动切分配置。

    Attributes:
        train_years: 训练期长度（交易年）。
        validation_months: 验证期长度（月），取训练期最后一段。
        test_years: 测试期长度（交易年）。
        step_years: 步进长度（交易年），必须不小于 ``test_years`` 以保证测试期不重叠。
        min_total_years: 正式研究所需的最小总交易年数。
    """

    train_years: int = 3
    validation_months: int = 6
    test_years: int = 1
    step_years: int = 1
    min_total_years: int = 5

    def __post_init__(self) -> None:
        if self.train_years < 1:
            raise ValueError("train_years 必须 >= 1")
        if self.test_years < 1:
            raise ValueError("test_years 必须 >= 1")
        if self.step_years < self.test_years:
            raise ValueError(
                f"step_years({self.step_years}) 必须 >= test_years({self.test_years})，"
                "否则测试期重叠"
            )
        if self.validation_months < 1:
            raise ValueError("validation_months 必须 >= 1")
        if self.validation_months > self.train_years * 12:
            raise ValueError(
                f"validation_months({self.validation_months}) 超过训练期长度"
                f"({self.train_years * 12} 个月)"
            )
        if self.min_total_years < 1:
            raise ValueError("min_total_years 必须 >= 1")


class WalkForwardSplitter:
    """滚动训练/验证/测试切分器。

    使用方法::

        config = WalkForwardConfig()
        splitter = WalkForwardSplitter(config)
        if splitter.is_insufficient_sample(trading_dates):
            # 标记 INSUFFICIENT_RESEARCH_SAMPLE，可运行小样本演示
            ...
        folds = splitter.split(trading_dates)
        for fold in folds:
            train_dates = splitter.get_train_dates(trading_dates, fold)
            val_dates = splitter.get_validation_dates(trading_dates, fold)
            test_dates = splitter.get_test_dates(trading_dates, fold)

    切分严格按日期完成，再在各折训练/验证数据上选择参数；禁止先在全样本拟合
    标准化器或选择参数。
    """

    def __init__(self, config: WalkForwardConfig | None = None) -> None:
        self.config = config if config is not None else WalkForwardConfig()

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def is_insufficient_sample(self, trading_dates: list[date]) -> bool:
        """判断样本是否不足 ``min_total_years`` 个交易年。

        优先按完整日历年计数；无法确定完整日历年时，按 244 交易日/年近似。

        Args:
            trading_dates: 交易日列表（无需排序）。

        Returns:
            样本不足时返回 ``True``。
        """
        sorted_dates = self._sorted_unique(trading_dates)
        if not sorted_dates:
            return True
        complete_years = self._complete_calendar_years(sorted_dates)
        if complete_years:
            return len(complete_years) < self.config.min_total_years
        return len(sorted_dates) < self.config.min_total_years * TRADING_DAYS_PER_YEAR

    def split(self, trading_dates: list[date]) -> list[Fold]:
        """生成非重叠的滚动折。

        优先按日历年边界切分；当完整日历年不足以构成一个折时，回退到 244 交易日
        块近似。仅保留测试期完整的折，各折测试期互不重叠。

        Args:
            trading_dates: 交易日列表（无需排序）。

        Returns:
            ``Fold`` 列表，按 ``fold_id`` 升序。
        """
        sorted_dates = self._sorted_unique(trading_dates)
        if not sorted_dates:
            return []
        complete_years = self._complete_calendar_years(sorted_dates)
        if len(complete_years) >= self.config.train_years + self.config.test_years:
            return self._split_by_calendar_years(complete_years)
        # 完整日历年不足，回退到 244 交易日块近似
        return self._split_by_trading_day_blocks(sorted_dates)

    def get_train_dates(self, dates: list[date], fold: Fold) -> list[date]:
        """筛选训练期内的交易日（排除验证期）。

        验证期是训练期的最后一段，单独用于候选参数选择，不参与特征/参数拟合。

        Args:
            dates: 交易日列表。
            fold: 目标折。

        Returns:
            训练期内、但不在验证期内的交易日（升序）。
        """
        return sorted(
            d
            for d in dates
            if fold.train_start <= d <= fold.train_end
            and not (fold.validation_start <= d <= fold.validation_end)
        )

    def get_validation_dates(self, dates: list[date], fold: Fold) -> list[date]:
        """筛选验证期内的交易日。

        Args:
            dates: 交易日列表。
            fold: 目标折。

        Returns:
            验证期内的交易日（升序）。
        """
        return sorted(
            d for d in dates if fold.validation_start <= d <= fold.validation_end
        )

    def get_test_dates(self, dates: list[date], fold: Fold) -> list[date]:
        """筛选测试期内的交易日。

        Args:
            dates: 交易日列表。
            fold: 目标折。

        Returns:
            测试期内的交易日（升序）。
        """
        return sorted(d for d in dates if fold.test_start <= d <= fold.test_end)

    # ------------------------------------------------------------------
    # 内部：日历年切分（主路径）
    # ------------------------------------------------------------------

    def _split_by_calendar_years(self, complete_years: list[int]) -> list[Fold]:
        """按日历年边界生成折叠。

        训练取连续 ``train_years`` 个完整日历年，测试取紧随其后的
        ``test_years`` 个完整日历年；步进 ``step_years`` 个日历年。由于
        ``step_years >= test_years``，各折测试期对应不同日历年，互不重叠。
        """
        train_n = self.config.train_years
        test_n = self.config.test_years
        step = self.config.step_years
        folds: list[Fold] = []
        fold_id = 0
        i = 0
        while i + train_n + test_n <= len(complete_years):
            train_years = complete_years[i : i + train_n]
            test_years = complete_years[i + train_n : i + train_n + test_n]
            first_train_year = train_years[0]
            last_train_year = train_years[-1]
            first_test_year = test_years[0]
            last_test_year = test_years[-1]

            train_start = date(first_train_year, 1, 1)
            train_end = date(last_train_year, 12, 31)
            validation_start = self._validation_start(last_train_year, train_start)
            validation_end = train_end
            test_start = date(first_test_year, 1, 1)
            test_end = date(last_test_year, 12, 31)

            folds.append(
                Fold(
                    fold_id=fold_id,
                    train_start=train_start,
                    train_end=train_end,
                    validation_start=validation_start,
                    validation_end=validation_end,
                    test_start=test_start,
                    test_end=test_end,
                )
            )
            fold_id += 1
            i += step
        return folds

    def _validation_start(self, last_train_year: int, train_start: date) -> date:
        """验证期起始日 = 训练期最后 ``validation_months`` 个月的月首。"""
        dec_first = date(last_train_year, 12, 1)
        start = _add_months(dec_first, -(self.config.validation_months - 1))
        if start < train_start:
            # 配置异常时钳制到训练期起点，避免验证期越界
            start = train_start
        return start

    def _complete_calendar_years(self, sorted_dates: list[date]) -> list[int]:
        """识别完整日历年。

        判定标准：该年首 个交易日落在 Q1（1-3 月）、末个交易日落在 Q4（10-12 月），
        确保年度覆盖完整，排除数据首尾的不完整年份。
        """
        by_year: dict[int, list[date]] = {}
        for d in sorted_dates:
            by_year.setdefault(d.year, []).append(d)
        complete: list[int] = []
        for y in sorted(by_year):
            dates_y = by_year[y]
            first = dates_y[0]
            last = dates_y[-1]
            if first.month <= 3 and last.month >= 10:
                complete.append(y)
        return complete

    # ------------------------------------------------------------------
    # 内部：244 交易日块切分（回退路径）
    # ------------------------------------------------------------------

    def _split_by_trading_day_blocks(self, sorted_dates: list[date]) -> list[Fold]:
        """按 244 交易日/年块生成折叠（日历年边界不可用时的回退）。

        训练 = ``train_years * 244`` 个交易日，验证 = 训练期末段按月数占比换算
        的交易日数，测试 = 紧随其后的 ``test_years * 244`` 个交易日。由于
        ``step_years >= test_years``，各折测试期相邻不重叠。
        """
        n = len(sorted_dates)
        days_per_year = TRADING_DAYS_PER_YEAR
        train_days = self.config.train_years * days_per_year
        test_days = self.config.test_years * days_per_year
        step_days = self.config.step_years * days_per_year
        val_days = max(
            1,
            int(round(self.config.validation_months / 12 * train_days)),
        )

        folds: list[Fold] = []
        fold_id = 0
        start = 0
        while True:
            train_s = start
            train_e = start + train_days - 1
            test_s = train_e + 1
            test_e = test_s + test_days - 1
            if test_e >= n:
                # 测试期不完整，停止生成
                break
            val_s = max(train_s, train_e - val_days + 1)
            folds.append(
                Fold(
                    fold_id=fold_id,
                    train_start=sorted_dates[train_s],
                    train_end=sorted_dates[train_e],
                    validation_start=sorted_dates[val_s],
                    validation_end=sorted_dates[train_e],
                    test_start=sorted_dates[test_s],
                    test_end=sorted_dates[test_e],
                )
            )
            fold_id += 1
            start += step_days
        return folds

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _sorted_unique(trading_dates: list[date]) -> list[date]:
        """升序去重。"""
        return sorted(set(trading_dates))


def _add_months(d: date, months: int) -> date:
    """日期加减月数，结果日截断到目标月份最后一天。"""
    total = d.year * 12 + (d.month - 1) + months
    year = total // 12
    month = total % 12 + 1
    day = min(d.day, _last_day_of_month(year, month))
    return date(year, month, day)


def _last_day_of_month(year: int, month: int) -> int:
    """返回某年某月的最后一天。"""
    return _calendar.monthrange(year, month)[1]


__all__ = [
    "TRADING_DAYS_PER_YEAR",
    "Fold",
    "WalkForwardConfig",
    "WalkForwardSplitter",
]
