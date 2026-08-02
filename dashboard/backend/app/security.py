"""安全原语：Argon2 密码、会话管理、CSRF、登录限流、一次性确认令牌、密码存储。

设计要点（UI-G1 Security）：
- 密码哈希用 Argon2id 验证
- 会话 Cookie：Secure、HttpOnly、SameSite=Strict、Path=/，服务端记录过期时间
- 登录成功后轮换 Session 标识
- Logout 立即失效（服务端删除）
- 密码修改后所有旧会话失效
- 登录失败按 IP 限流，达到阈值临时锁定
- 一次性确认令牌：短期有效、仅能使用一次、绑定用户+动作+会话
- 明文密码永不落盘；密码哈希原子写入 Git 忽略的 state/dashboard/auth.json
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from .config import SESSION_TTL_SECONDS, DashboardConfig
from .errors import DashboardError

# 允许的动作白名单（UI-G2 固定枚举；与 /api/jobs 作业类型一致）
ALLOWED_ACTIONS = (
    "verify",
    "daily",
    "weekly",
    "rerun",
    "backfill",
)

# 永久禁止的动作/命令描述
FORBIDDEN_ACTIONS = ("install", "uninstall", "synthetic", "gate4b_track")


class AuthStore:
    """state/dashboard/auth.json 的原子读写（Git 忽略目录）。"""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def write(self, payload: dict[str, Any]) -> None:
        """原子写入：先写临时文件再 os.replace。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with self._lock:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)

    def update_password(self, username: str, password_hash: str) -> None:
        payload = {
            "version": 1,
            "username": username,
            "password_hash": password_hash,
            "updated_at": int(time.time()),
        }
        self.write(payload)


@dataclass
class Session:
    """服务端会话记录。"""

    sid: str
    username: str
    csrf_token: str
    created_at: float
    expires_at: float

    @property
    def expired(self) -> bool:
        return time.time() >= self.expires_at


class SecurityManager:
    """持有密码哈希、会话表、限流表、确认令牌表。"""

    def __init__(self, config: DashboardConfig):
        self.config = config
        self.hasher = PasswordHasher()
        # 密码哈希：优先 auth.json，否则回退环境变量初始化
        self.auth_store = AuthStore(config.auth_file) if config.auth_file else None
        self._username: str | None = None
        self._password_hash: str | None = None
        self._sessions: dict[str, Session] = {}
        self._login_failures: dict[str, list[float]] = {}  # ip -> 失败时间戳列表
        self._confirm_tokens: dict[str, dict[str, Any]] = {}  # token -> payload
        self._lock = threading.Lock()
        self._init_password()

    # ---- 密码 ----

    def _init_password(self) -> None:
        stored = self.auth_store.read() if self.auth_store else {}
        if stored.get("password_hash") and stored.get("username"):
            self._username = stored["username"]
            self._password_hash = stored["password_hash"]
            return
        # 首次启动：从环境变量哈希初始化（服务端已保证 fail-closed 非空）
        self._username = self.config.username
        self._password_hash = self.config.password_hash
        if self.auth_store is not None:
            self.auth_store.update_password(self._username, self._password_hash)

    @property
    def username(self) -> str:
        return self._username or ""

    def verify_password(self, password: str) -> bool:
        """Argon2 验证；哈希非法视为不通过。"""
        if not self._password_hash:
            return False
        try:
            return self.hasher.verify(self._password_hash, password)
        except (VerifyMismatchError, InvalidHashError):
            return False

    def change_password(self, old_password: str, new_password: str) -> None:
        """验证旧密码后写入新哈希，并令所有旧会话失效。"""
        if not self.verify_password(old_password):
            raise DashboardError("auth_old_password", "旧密码不正确", status_code=403)
        if len(new_password) < 8:
            raise DashboardError("auth_weak_password", "新密码至少 8 个字符", status_code=400)
        new_hash = self.hasher.hash(new_password)
        if self.auth_store is not None:
            self.auth_store.update_password(self._username or "", new_hash)
        self._password_hash = new_hash
        # 所有旧会话失效
        with self._lock:
            self._sessions.clear()
            self._confirm_tokens.clear()

    # ---- 会话 ----

    def create_session(self, username: str) -> Session:
        """创建新会话（登录成功后轮换 Session 标识）。"""
        sid = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        now = time.time()
        session = Session(
            sid=sid,
            username=username,
            csrf_token=csrf,
            created_at=now,
            expires_at=now + SESSION_TTL_SECONDS,
        )
        with self._lock:
            self._sessions[sid] = session
        return session

    def get_session(self, sid: str | None) -> Session | None:
        if not sid:
            return None
        with self._lock:
            session = self._sessions.get(sid)
            if session is None:
                return None
            if session.expired:
                self._sessions.pop(sid, None)
                return None
            return session

    def delete_session(self, sid: str | None) -> None:
        with self._lock:
            self._sessions.pop(sid, None)

    def invalidate_all_sessions(self) -> None:
        with self._lock:
            self._sessions.clear()

    # ---- 登录限流 ----

    def _prune_failures(self, ip: str) -> None:
        window = self.config.login_lock_seconds
        now = time.time()
        ts = self._failures(ip)
        keep = [t for t in ts if now - t < window]
        if keep:
            self._login_failures[ip] = keep
        else:
            self._login_failures.pop(ip, None)

    def _failures(self, ip: str) -> list[float]:
        return self._login_failures.get(ip, [])

    def is_locked(self, ip: str) -> bool:
        with self._lock:
            self._prune_failures(ip)
            return len(self._failures(ip)) >= self.config.login_max_failures

    def record_failure(self, ip: str) -> int:
        with self._lock:
            now = time.time()
            self._prune_failures(ip)
            self._login_failures.setdefault(ip, []).append(now)
            return len(self._failures(ip))

    def clear_failures(self, ip: str) -> None:
        with self._lock:
            self._login_failures.pop(ip, None)

    # ---- 一次性确认令牌 ----

    def issue_confirm_token(
        self,
        username: str,
        action: str,
        sid: str,
        ttl: int = 60,
        params: dict[str, Any] | None = None,
    ) -> str:
        """签发一次性确认令牌：绑定用户、动作、会话与**具体参数**，短期有效。

        ``params`` 记录该动作的具体参数（如日期/区间），消费时须一致，
        防止"确认了一个日期、实际执行了另一个日期"的替换攻击。
        """
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._confirm_tokens[token] = {
                "username": username,
                "action": action,
                "sid": sid,
                "params": dict(params or {}),
                "expires_at": time.time() + ttl,
            }
        return token

    def consume_confirm_token(
        self,
        token: str,
        username: str,
        action: str,
        sid: str,
        params: dict[str, Any] | None = None,
    ) -> bool:
        """校验并消费令牌；过期、重复使用、绑定不匹配或**参数不一致**均拒绝。"""
        with self._lock:
            payload = self._confirm_tokens.pop(token, None)
            if payload is None:
                return False
            if time.time() > payload["expires_at"]:
                return False
            bound_params = payload.get("params") or {}
            if params is not None and bound_params != dict(params):
                return False
            return (
                payload["username"] == username
                and payload["action"] == action
                and payload["sid"] == sid
            )

    # ---- CSRF ----

    def validate_csrf(self, session: Session, provided_token: str | None) -> bool:
        """Double-submit：请求头中的 X-CSRF-Token 必须与会话绑定的令牌一致。"""
        if not provided_token:
            return False
        return hmac.compare_digest(session.csrf_token, provided_token)

    def hash_secret(self, value: str) -> str:
        """会话 Secret 派生哈希（用于给返回值打标记，不落日志）。"""
        return hashlib.sha256(
            (self.config.session_secret + value).encode("utf-8")
        ).hexdigest()[:16]
