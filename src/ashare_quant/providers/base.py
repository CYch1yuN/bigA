"""统一 DataProvider 抽象基类。

设计原则：提供器只负责从网络获取**原始**数据（保留数据源原生字段与格式），
不负责标准化、校验或存储。标准化与质量检查在独立模块完成，从而：
1. 网络逻辑与清洗逻辑解耦；
2. 单元测试可通过 mock/子类化提供器离线运行，无需访问公网。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

import pandas as pd


class DataProvider(ABC):
    """数据源提供器抽象。

    所有方法返回 ``pandas.DataFrame``，列名为数据源原生字段（未标准化）。
    标准化由 :mod:`ashare_quant.standardize` 完成。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """数据源标识，如 ``akshare`` / ``baostock``。"""

    @abstractmethod
    def fetch_daily_quotes(
        self, symbol: str, start_date: date, end_date: date
    ) -> pd.DataFrame:
        """抓取指定股票在 [start_date, end_date] 区间的日行情原始数据。

        返回的 DataFrame 至少包含数据源原生的 OHLC、成交量、成交额、复权信息
        与日期字段；具体列名由各提供器决定，标准化阶段统一映射。
        """

    @abstractmethod
    def fetch_security_master(self) -> pd.DataFrame:
        """抓取证券主数据（含上市/退市/ST 状态历史）。

        如数据源无法完整提供退市或 ST 历史区间，返回数据应保留可获取部分，
        并在 limitations 中标记缺口；不得伪造或静默填充。
        """

    @abstractmethod
    def fetch_trade_calendar(
        self, start_date: date, end_date: date
    ) -> pd.DataFrame:
        """抓取交易日历，返回含 ``trade_date`` 列的 DataFrame。"""

    # ---- 可选：复权方式 ----
    def default_adjustment(self) -> str:
        """默认复权方式标识，默认 ``qfq``（前复权）。"""
        return "qfq"
