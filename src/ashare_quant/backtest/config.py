"""回测配置：所有费率、阈值集中于此，禁止代码硬编码。

使用 Pydantic 校验，确保非负约束和合理范围。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field


class CommissionConfig(BaseModel):
    """佣金配置。"""

    rate: float = Field(default=0.0003, ge=0, le=0.01)  # 万三
    minimum: float = Field(default=5.0, ge=0)  # 最低佣金5元


class StampDutyConfig(BaseModel):
    """印花税配置（仅卖出收取）。"""

    rate: float = Field(default=0.001, ge=0, le=0.01)  # 千一


class TransferFeeConfig(BaseModel):
    """过户费配置（双向收取）。"""

    rate: float = Field(default=0.00001, ge=0, le=0.001)  # 万零点一


class SlippageConfig(BaseModel):
    """滑点配置。"""

    bps: float = Field(default=10.0, ge=0)  # 10bps = 0.1%
    tick_size: float = Field(default=0.01, ge=0)  # 0.01元


class LimitConfig(BaseModel):
    """涨跌停配置。"""

    # 板块涨跌停比例：main=10%, star=20%, szse=10%, bjse=30%
    main_ratio: float = Field(default=0.10, ge=0, le=1.0)
    star_ratio: float = Field(default=0.20, ge=0, le=1.0)
    szse_ratio: float = Field(default=0.10, ge=0, le=1.0)
    bjse_ratio: float = Field(default=0.30, ge=0, le=1.0)
    # ST 股票涨跌停比例
    st_ratio: float = Field(default=0.05, ge=0, le=1.0)
    tick_size: float = Field(default=0.01, ge=0)


class RiskConfig(BaseModel):
    """风控配置。"""

    enable_single_position_limit: bool = True
    max_position_value_ratio: float = Field(default=1.0, ge=0, le=1.0)  # 100%


class UniverseConfig(BaseModel):
    """股票池配置。"""

    min_lot_value: float = Field(default=1000.0, ge=0)  # 100股最低购买金额


class BacktestConfig(BaseModel):
    """回测总配置。"""

    model_config = ConfigDict(populate_by_name=True)

    initial_cash: float = Field(default=1000.0, ge=0)
    lot_size: int = Field(default=100, ge=1)
    commission: CommissionConfig = Field(default_factory=CommissionConfig)
    stamp_duty: StampDutyConfig = Field(default_factory=StampDutyConfig)
    transfer_fee: TransferFeeConfig = Field(default_factory=TransferFeeConfig)
    slippage: SlippageConfig = Field(default_factory=SlippageConfig)
    limit: LimitConfig = Field(default_factory=LimitConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    universe: UniverseConfig = Field(default_factory=UniverseConfig)

    def to_summary(self) -> dict[str, Any]:
        """生成可序列化的配置摘要。"""
        return {
            "initial_cash": self.initial_cash,
            "lot_size": self.lot_size,
            "commission": {
                "rate": self.commission.rate,
                "minimum": self.commission.minimum,
            },
            "stamp_duty": {"rate": self.stamp_duty.rate},
            "transfer_fee": {"rate": self.transfer_fee.rate},
            "slippage": {
                "bps": self.slippage.bps,
                "tick_size": self.slippage.tick_size,
            },
            "limit": {
                "main_ratio": self.limit.main_ratio,
                "star_ratio": self.limit.star_ratio,
                "szse_ratio": self.limit.szse_ratio,
                "bjse_ratio": self.limit.bjse_ratio,
                "st_ratio": self.limit.st_ratio,
                "tick_size": self.limit.tick_size,
            },
            "risk": {
                "enable_single_position_limit": self.risk.enable_single_position_limit,
                "max_position_value_ratio": self.risk.max_position_value_ratio,
            },
            "universe": {
                "min_lot_value": self.universe.min_lot_value,
            },
        }


def load_backtest_config(path: str | Path) -> BacktestConfig:
    """从 YAML 文件加载回测配置。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return BacktestConfig.model_validate(raw)


def default_backtest_config_path() -> Path:
    """返回仓库内默认回测配置路径。"""
    here = Path(__file__).resolve().parent
    return here.parent.parent.parent / "config" / "backtest.default.yaml"


__all__ = [
    "CommissionConfig",
    "StampDutyConfig",
    "TransferFeeConfig",
    "SlippageConfig",
    "LimitConfig",
    "RiskConfig",
    "UniverseConfig",
    "BacktestConfig",
    "load_backtest_config",
    "default_backtest_config_path",
]
