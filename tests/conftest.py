"""pytest 公共 fixture。"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from ashare_quant.config import default_config_path, load_config
from ashare_quant.samples import make_trade_calendar
from ashare_quant.standardize import Standardizer
from ashare_quant.storage import Storage


@pytest.fixture(scope="session")
def config():
    return load_config(default_config_path())


@pytest.fixture
def standardizer():
    return Standardizer()


@pytest.fixture
def storage(tmp_path):
    return Storage(tmp_path / "data")


@pytest.fixture
def trade_calendar():
    from datetime import date, timedelta

    return make_trade_calendar(date(2024, 1, 2), date(2024, 3, 15))
