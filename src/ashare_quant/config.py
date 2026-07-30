"""配置加载：从 YAML 读取并校验为 Pydantic 模型。

质量阈值集中存放在 YAML，禁止散落硬编码。
"""

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

# 严重等级
SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING = "warning"


class QualityRule(BaseModel):
    """单条质量规则：严重等级 + 可选阈值参数。"""

    severity: str = SEVERITY_WARNING
    # 额外阈值参数（如 lookback_days、阈值数值）以字典形式存放，
    # 避免为每条规则定义独立模型。
    params: dict[str, Any] = Field(default_factory=dict)

    @field_validator("severity")
    @classmethod
    def _check_severity(cls, v: str) -> str:
        if v not in (SEVERITY_CRITICAL, SEVERITY_WARNING):
            raise ValueError(f"severity 必须为 critical 或 warning，得到 {v!r}")
        return v


class ManifestConfig(BaseModel):
    content_hash_exclude_fields: list[str] = Field(default_factory=list)


class PathsConfig(BaseModel):
    data_dir: str = "data"
    raw_dir: str = "data/raw"
    curated_dir: str = "data/curated"
    metadata_dir: str = "data/metadata"
    reports_dir: str = "reports/phase-1"


class SchemaConfig(BaseModel):
    daily_quote_version: str = "1.0.0"
    security_master_version: str = "1.0.0"


class ProvidersConfig(BaseModel):
    primary: str = "akshare"
    fallback: str = "baostock"
    # 失败后的重试次数（不含首次请求）；总尝试次数 = 1 + max_retries
    max_retries: int = Field(default=3, ge=0)
    # 重试间隔（秒），不得为负
    request_interval_seconds: float = Field(default=1.0, ge=0)


class AppConfig(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    project: dict[str, str] = Field(default_factory=dict)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    # YAML 键为 ``schema``，属性名避开 BaseModel.schema 冲突
    schema_versions: SchemaConfig = Field(default_factory=SchemaConfig, alias="schema")
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    quality: dict[str, Any] = Field(default_factory=dict)
    manifest: ManifestConfig = Field(default_factory=ManifestConfig)

    def quality_rule(self, name: str) -> QualityRule:
        """按名称获取质量规则。

        支持两种 YAML 写法：
        1. 简写：``duplicate_primary_key: critical``
        2. 详细：``abnormal_volume: {severity: warning, lookback_days: 20, ...}``
        """
        if name not in self.quality:
            raise KeyError(f"质量规则 {name!r} 未在配置中定义")
        entry = self.quality[name]
        if isinstance(entry, str):
            return QualityRule(severity=entry)
        if isinstance(entry, dict):
            severity = entry.get("severity", SEVERITY_WARNING)
            params = {k: v for k, v in entry.items() if k != "severity"}
            return QualityRule(severity=severity, params=params)
        raise TypeError(f"质量规则 {name!r} 格式不支持: {type(entry)}")


def load_config(path: str | Path) -> AppConfig:
    """从 YAML 文件加载并校验配置。"""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return AppConfig.model_validate(raw)


def default_config_path() -> Path:
    """返回仓库内默认配置路径。"""
    here = Path(__file__).resolve().parent
    # src/ashare_quant -> 仓库根 / config / default.yaml
    return here.parent.parent / "config" / "default.yaml"
