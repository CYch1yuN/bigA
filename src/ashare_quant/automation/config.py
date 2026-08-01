"""Phase 4 自动化配置：所有路径、阈值、时间窗口集中于此，禁止代码硬编码。

使用 Pydantic 校验，保证：

- 时间字符串格式合法（``HH:MM``）。
- 阈值非负且落在合理区间。
- 账户配置的资格结论必须与 Phase 3 结论一致（见 ``TRACK_ELIGIBILITY``），
  任何试图放宽的配置在加载阶段就会被拒绝。
- ``live_trading.enabled`` 只能为 ``false``；置为 ``true`` 时配置仍可加载，
  但运行期会被资格闸门拦截为 ``BLOCKED_NOT_ELIGIBLE``（fail-closed，便于测试覆盖）。
"""
from __future__ import annotations

import json
import re
from datetime import time as dtime
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)

from .models import EligibilityStatus, StrategyTrack, TRACK_ELIGIBILITY

__all__ = [
    "PathsConfig",
    "CalendarConfig",
    "DataConfig",
    "QualityGateConfig",
    "AccountConfig",
    "ObservationConfig",
    "LockConfig",
    "LoggingConfig",
    "AlertsConfig",
    "ArchiveConfig",
    "SchedulerConfig",
    "LiveTradingConfig",
    "AutomationConfig",
    "load_automation_config",
    "default_automation_config_path",
    "repo_root",
    "parse_hhmm",
]

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

_WEEKDAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")


def parse_hhmm(value: str) -> dtime:
    """将 ``HH:MM`` 字符串解析为 ``datetime.time``。"""
    match = _HHMM_RE.match(value.strip())
    if match is None:
        raise ValueError(f"时间格式必须为 HH:MM（24 小时制），收到: {value!r}")
    return dtime(hour=int(match.group(1)), minute=int(match.group(2)))


def repo_root() -> Path:
    """返回仓库根目录（基于本文件位置推导）。"""
    return Path(__file__).resolve().parent.parent.parent.parent


# ---------------------------------------------------------------------- #
# 子配置
# ---------------------------------------------------------------------- #


class PathsConfig(BaseModel):
    """路径配置（均为相对仓库根目录的相对路径）。"""

    data_dir: str = "data"
    state_dir: str = "state/automation"
    reports_dir: str = "reports/phase-4"
    logs_dir: str = "logs/automation"
    archive_dir: str = "reports/phase-4/archive"


class CalendarConfig(BaseModel):
    """交易日历配置（fail-closed）。"""

    calendar_path: str = "data/metadata/trade_calendar.parquet"
    date_column: str = "trade_date"
    is_open_column: str = "is_open"
    # 日历最后一个交易日距离 as_of 超过该天数则视为过期，直接失败
    max_staleness_days: int = Field(default=30, ge=1, le=365)
    # 为 True 时日历缺失即 fail-closed；禁止改为 False 后按工作日推断
    require_calendar: bool = True


class DataConfig(BaseModel):
    """数据更新配置。"""

    symbols: list[str] = Field(default_factory=list)
    benchmark_symbols: list[str] = Field(default_factory=list)
    # 每次增量更新回看的自然日数量（覆盖长假与补数据）
    lookback_days: int = Field(default=400, ge=30, le=3650)
    # 数据源预计就绪时间（交易日当天）
    ready_time: str = "18:30"
    max_retries: int = Field(default=3, ge=0, le=10)
    retry_interval_seconds: float = Field(default=1.0, ge=0.0, le=60.0)
    # 数据源不可用时是否允许"跳过"而非"失败"
    allow_skip_when_unavailable: bool = True

    @field_validator("ready_time")
    @classmethod
    def _check_ready_time(cls, v: str) -> str:
        parse_hhmm(v)
        return v

    @property
    def ready_time_parsed(self) -> dtime:
        return parse_hhmm(self.ready_time)


class QualityGateConfig(BaseModel):
    """数据质量闸门配置。"""

    block_on_critical: bool = True
    # warning 数量超过该阈值也阻断（0 表示不限制）
    max_warning: int = Field(default=0, ge=0)
    # 是否允许在质量未通过时复用上一交易日数据（必须为 False）
    allow_stale_fallback: bool = False

    @field_validator("block_on_critical")
    @classmethod
    def _must_block(cls, v: bool) -> bool:
        if not v:
            raise ValueError(
                "quality.block_on_critical 不允许关闭：critical 问题必须阻断运行"
            )
        return v

    @field_validator("allow_stale_fallback")
    @classmethod
    def _no_stale(cls, v: bool) -> bool:
        if v:
            raise ValueError(
                "quality.allow_stale_fallback 不允许开启：禁止静默复用昨日数据"
            )
        return v


class AccountConfig(BaseModel):
    """模拟账户配置。"""

    account_id: str
    track: StrategyTrack
    initial_cash: float = Field(default=1000.0, gt=0)
    eligibility_status: EligibilityStatus

    @model_validator(mode="after")
    def _check_eligibility(self) -> "AccountConfig":
        expected = TRACK_ELIGIBILITY[self.track]
        if self.eligibility_status is not expected:
            raise ValueError(
                f"账户 {self.account_id} 的资格结论必须为 {expected.value}"
                f"（Phase 3 复审结论），不得配置为 {self.eligibility_status.value}"
            )
        return self


class ObservationConfig(BaseModel):
    """观察窗口配置。"""

    target_trading_days: int = Field(default=60, ge=1, le=500)
    # 周度报告最少需要多少个交易日样本才输出统计
    min_days_for_stats: int = Field(default=5, ge=1, le=100)


class LockConfig(BaseModel):
    """跨进程锁配置。"""

    lock_filename: str = "automation.lock"
    # 超过该秒数且持锁进程已不存在，视为陈旧锁
    stale_after_seconds: int = Field(default=21600, ge=60, le=604800)
    # 是否允许在持锁进程仍存活时抢锁（必须为 False）
    allow_steal_active: bool = False

    @field_validator("allow_steal_active")
    @classmethod
    def _no_steal(cls, v: bool) -> bool:
        if v:
            raise ValueError("lock.allow_steal_active 不允许开启：活跃锁不可被覆盖")
        return v


class LoggingConfig(BaseModel):
    """结构化日志配置。"""

    level: str = "INFO"
    filename_pattern: str = "automation-{date}.jsonl"
    console: bool = True
    redact_keys: list[str] = Field(
        default_factory=lambda: [
            "api_key",
            "apikey",
            "authorization",
            "auth",
            "token",
            "access_token",
            "refresh_token",
            "password",
            "passwd",
            "secret",
            "cookie",
            "session",
            "credential",
            "private_key",
        ]
    )

    @field_validator("level")
    @classmethod
    def _check_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"logging.level 必须是 {sorted(allowed)} 之一")
        return upper


class AlertsConfig(BaseModel):
    """本机告警配置。"""

    enabled: bool = True
    failure_marker_filename: str = "LAST_FAILURE.marker"
    latest_failure_md: str = "latest-failure.md"
    latest_failure_json: str = "latest-failure.json"
    windows_event_log: bool = False
    event_log_source: str = "AShareQuantAutomation"
    toast: bool = False
    # 只能通过环境变量提供 webhook，配置文件中禁止出现明文 URL
    webhook_env_var: str = "ASHARE_AUTOMATION_WEBHOOK_URL"
    webhook_timeout_seconds: float = Field(default=5.0, ge=0.5, le=60.0)

    @field_validator("webhook_env_var")
    @classmethod
    def _no_inline_url(cls, v: str) -> str:
        if "://" in v:
            raise ValueError(
                "alerts.webhook_env_var 必须是环境变量名，禁止在配置文件中写入 URL"
            )
        return v


class ArchiveConfig(BaseModel):
    """归档与保留配置。"""

    enabled: bool = True
    retain_days: int = Field(default=180, ge=1, le=3650)
    # 每周归档一次；保留最近 N 个归档批次
    max_batches: int = Field(default=52, ge=1, le=520)


class SchedulerConfig(BaseModel):
    """Windows 任务计划配置。"""

    task_name_prefix: str = "AShareQuantAutomation"
    daily_time: str = "18:40"
    weekly_day: str = "SAT"
    weekly_time: str = "09:00"
    run_level: str = "LIMITED"
    working_directory: str = "."

    @field_validator("daily_time", "weekly_time")
    @classmethod
    def _check_times(cls, v: str) -> str:
        parse_hhmm(v)
        return v

    @field_validator("weekly_day")
    @classmethod
    def _check_day(cls, v: str) -> str:
        upper = v.strip().upper()
        if upper not in _WEEKDAYS:
            raise ValueError(f"scheduler.weekly_day 必须是 {list(_WEEKDAYS)} 之一")
        return upper

    @field_validator("run_level")
    @classmethod
    def _check_level(cls, v: str) -> str:
        upper = v.strip().upper()
        if upper not in {"LIMITED", "HIGHEST"}:
            raise ValueError("scheduler.run_level 必须是 LIMITED 或 HIGHEST")
        return upper


class LiveTradingConfig(BaseModel):
    """实盘开关（永久关闭）。

    该节点存在的唯一目的，是让"有人试图打开实盘"这件事**可被检测、可被阻断、
    可被测试覆盖**，而不是被静默忽略。
    """

    enabled: bool = False
    broker_endpoint: Optional[str] = None

    @field_validator("broker_endpoint")
    @classmethod
    def _no_broker(cls, v: Optional[str]) -> Optional[str]:
        if v:
            raise ValueError(
                "live_trading.broker_endpoint 必须为空：Phase 4 禁止连接任何券商接口"
            )
        return v


# ---------------------------------------------------------------------- #
# 总配置
# ---------------------------------------------------------------------- #


class AutomationConfig(BaseModel):
    """Phase 4 自动化总配置。"""

    model_config = ConfigDict(populate_by_name=True)

    schema_version: int = Field(default=1, ge=1)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    calendar: CalendarConfig = Field(default_factory=CalendarConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    quality: QualityGateConfig = Field(default_factory=QualityGateConfig)
    accounts: list[AccountConfig] = Field(default_factory=list)
    observation: ObservationConfig = Field(default_factory=ObservationConfig)
    lock: LockConfig = Field(default_factory=LockConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    archive: ArchiveConfig = Field(default_factory=ArchiveConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    live_trading: LiveTradingConfig = Field(default_factory=LiveTradingConfig)

    # 运行期注入的基准目录（不参与哈希）
    _base_dir: Path = PrivateAttr(default_factory=lambda: repo_root())

    @model_validator(mode="after")
    def _check_accounts(self) -> "AutomationConfig":
        ids = [a.account_id for a in self.accounts]
        if len(ids) != len(set(ids)):
            raise ValueError("accounts.account_id 必须唯一")
        return self

    # -- 路径解析 ------------------------------------------------------ #

    def with_base_dir(self, base_dir: str | Path) -> "AutomationConfig":
        """返回绑定了基准目录的配置副本。"""
        clone = self.model_copy(deep=True)
        clone._base_dir = Path(base_dir).resolve()
        return clone

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    def _resolve(self, relative: str) -> Path:
        p = Path(relative)
        return p if p.is_absolute() else (self._base_dir / p)

    @property
    def data_dir(self) -> Path:
        return self._resolve(self.paths.data_dir)

    @property
    def state_dir(self) -> Path:
        return self._resolve(self.paths.state_dir)

    @property
    def reports_dir(self) -> Path:
        return self._resolve(self.paths.reports_dir)

    @property
    def logs_dir(self) -> Path:
        return self._resolve(self.paths.logs_dir)

    @property
    def archive_dir(self) -> Path:
        return self._resolve(self.paths.archive_dir)

    @property
    def calendar_path(self) -> Path:
        return self._resolve(self.calendar.calendar_path)

    @property
    def lock_path(self) -> Path:
        return self.state_dir / self.lock.lock_filename

    def account(self, account_id: str) -> AccountConfig:
        """按 ID 获取账户配置。"""
        for acc in self.accounts:
            if acc.account_id == account_id:
                return acc
        raise KeyError(f"未知账户: {account_id}")

    def account_for_track(self, track: StrategyTrack) -> Optional[AccountConfig]:
        """按轨道获取账户配置。"""
        for acc in self.accounts:
            if acc.track is track:
                return acc
        return None

    # -- 摘要与哈希 ---------------------------------------------------- #

    def to_summary(self) -> dict[str, Any]:
        """可序列化的配置摘要（用于 run 报告与 config_hash）。"""
        data = self.model_dump(mode="json")
        # 路径仅保留相对形式，保证不同机器上的 config_hash 一致
        return data

    def config_hash_payload(self) -> str:
        """生成用于计算 config_hash 的规范化 JSON 字符串。"""
        return json.dumps(self.to_summary(), sort_keys=True, ensure_ascii=False)


# ---------------------------------------------------------------------- #
# 加载器
# ---------------------------------------------------------------------- #


def default_automation_config_path() -> Path:
    """返回仓库内默认自动化配置路径。"""
    return repo_root() / "config" / "automation.default.yaml"


def load_automation_config(
    path: str | Path | None = None,
    *,
    base_dir: str | Path | None = None,
) -> AutomationConfig:
    """从 YAML 加载自动化配置。

    Args:
        path: 配置文件路径；为 None 时使用仓库默认配置。
        base_dir: 相对路径解析基准目录；为 None 时使用仓库根目录。

    Returns:
        已绑定基准目录的 ``AutomationConfig``。

    Raises:
        FileNotFoundError: 配置文件不存在。
    """
    cfg_path = Path(path) if path is not None else default_automation_config_path()
    if not cfg_path.exists():
        raise FileNotFoundError(f"自动化配置文件不存在: {cfg_path}")
    with cfg_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    config = AutomationConfig.model_validate(raw)
    root = Path(base_dir) if base_dir is not None else repo_root()
    return config.with_base_dir(root)
