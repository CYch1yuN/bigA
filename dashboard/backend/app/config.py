"""Dashboard 安全配置：环境变量读取与 fail-closed 校验。

约束：
- 禁止默认密码
- 用户名、密码哈希或 Session Secret 缺失时服务 fail-closed
- 密码使用 Argon2 验证
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# 服务默认端口
DEFAULT_PORT = 8765

# 必须存在的环境变量（缺失即 fail-closed）
REQUIRED_ENV_VARS = (
    "ASHARE_DASHBOARD_USERNAME",
    "ASHARE_DASHBOARD_PASSWORD_HASH",
    "ASHARE_DASHBOARD_SESSION_SECRET",
)

# 登录限流默认值
DEFAULT_LOGIN_MAX_FAILURES = 5
DEFAULT_LOGIN_LOCK_SECONDS = 900  # 15 分钟

# 确认令牌有效期（秒）
CONFIRM_TOKEN_TTL_SECONDS = 60

# 会话有效期（秒）
SESSION_TTL_SECONDS = 8 * 3600  # 8 小时

# 操作超时与输出上限
ACTION_TIMEOUT_SECONDS = 30
ACTION_OUTPUT_MAX_BYTES = 65536  # 64 KiB

# 作业（job）超时：verify 短、单日/每周中等、区间补跑长
JOB_TIMEOUT_VERIFY_SECONDS = 60
JOB_TIMEOUT_SINGLE_SECONDS = 600
JOB_TIMEOUT_BACKFILL_SECONDS = 3600
# 区间补跑自然日上限
JOB_MAX_BACKFILL_DAYS = 250


class ConfigError(RuntimeError):
    """安全配置缺失或非法导致服务无法启动。"""


@dataclass(frozen=True)
class DashboardConfig:
    """由环境变量解析出的不可变配置。"""

    username: str
    password_hash: str
    session_secret: str
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    cert_file: Path | None = None
    key_file: Path | None = None
    login_max_failures: int = DEFAULT_LOGIN_MAX_FAILURES
    login_lock_seconds: int = DEFAULT_LOGIN_LOCK_SECONDS
    auth_file: Path | None = None  # state/dashboard/auth.json，Git 忽略
    state_dir: Path | None = None
    project_root: Path | None = None

    @property
    def lan_mode(self) -> bool:
        """是否为 LAN 监听模式（非本机回环地址）。"""
        return self.host not in ("127.0.0.1", "::1", "localhost")

    def validate(self) -> "DashboardConfig":
        """fail-closed：缺关键配置直接抛 ConfigError。"""
        if not self.username:
            raise ConfigError("ASHARE_DASHBOARD_USERNAME 未配置：禁止默认密码，服务拒绝启动")
        if not self.password_hash:
            raise ConfigError("ASHARE_DASHBOARD_PASSWORD_HASH 未配置：服务拒绝启动")
        if not self.session_secret or len(self.session_secret) < 32:
            raise ConfigError("ASHARE_DASHBOARD_SESSION_SECRET 未配置或过短（至少 32 字符）：服务拒绝启动")
        if self.lan_mode:
            if not self.cert_file or not self.key_file:
                raise ConfigError(
                    f"LAN 模式（host={self.host}）必须同时提供 "
                    "ASHARE_DASHBOARD_CERT_FILE 与 ASHARE_DASHBOARD_KEY_FILE，否则拒绝启动"
                )
            if not Path(self.cert_file).is_file() or not Path(self.key_file).is_file():
                raise ConfigError("配置的证书或私钥文件不存在，拒绝启动")
        return self


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} 必须为整数，当前值: {raw!r}") from None


def load_config(environ: dict[str, str] | None = None) -> DashboardConfig:
    """从环境变量加载配置；缺关键项时抛 ConfigError（fail-closed）。"""
    env = os.environ if environ is None else environ

    cfg = DashboardConfig(
        username=env.get("ASHARE_DASHBOARD_USERNAME", "").strip(),
        password_hash=env.get("ASHARE_DASHBOARD_PASSWORD_HASH", "").strip(),
        session_secret=env.get("ASHARE_DASHBOARD_SESSION_SECRET", "").strip(),
        host=env.get("ASHARE_DASHBOARD_HOST", "127.0.0.1").strip() or "127.0.0.1",
        port=_env_int("ASHARE_DASHBOARD_PORT", DEFAULT_PORT),
        cert_file=Path(env["ASHARE_DASHBOARD_CERT_FILE"]) if env.get("ASHARE_DASHBOARD_CERT_FILE") else None,
        key_file=Path(env["ASHARE_DASHBOARD_KEY_FILE"]) if env.get("ASHARE_DASHBOARD_KEY_FILE") else None,
        login_max_failures=_env_int("ASHARE_DASHBOARD_LOGIN_MAX_FAILURES", DEFAULT_LOGIN_MAX_FAILURES),
        login_lock_seconds=_env_int("ASHARE_DASHBOARD_LOGIN_LOCK_SECONDS", DEFAULT_LOGIN_LOCK_SECONDS),
        # 密码哈希持久化：首次启动写入 state/dashboard/auth.json，此后优先于环境变量
        auth_file=default_auth_file(),
    )
    return cfg.validate()


def default_auth_file() -> Path:
    """默认密码存储位置：仓库 state/dashboard/auth.json（Git 忽略）。

    注意：config.py 位于 dashboard/backend/app/ 下，
    parents[3] = 仓库根（与 default_project_root 一致）。
    """
    return Path(__file__).resolve().parents[3] / "state" / "dashboard" / "auth.json"


def default_project_root() -> Path:
    """Dashboard 所属仓库根目录。"""
    return Path(__file__).resolve().parents[3]
