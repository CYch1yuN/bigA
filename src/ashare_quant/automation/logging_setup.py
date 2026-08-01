"""结构化 JSONL 日志 + 敏感信息脱敏。

每行一条 JSON 记录，字段固定::

    timestamp       ISO8601 本地时间（秒级）
    level           DEBUG / INFO / WARNING / ERROR / CRITICAL
    run_id          运行标识
    task_type       daily / weekly
    as_of_date      业务日
    step            步骤名（无步骤上下文时为 null）
    event           机器可读事件码（稳定标识，便于 grep 与告警规则）
    message         人类可读描述
    exception_type  异常类型名（无异常时为 null）
    detail          结构化附加字段（已脱敏）

**脱敏是硬约束**：日志中不得出现 API Key、Authorization、环境变量值、
密码、Cookie、Token 等任何凭据。本模块在两个层面拦截：

1. 键名命中 ``redact_keys`` 的字段，值整体替换为 ``***REDACTED***``。
2. 文本内容中形如 ``token=xxx`` / ``Bearer xxx`` / 长串十六进制的片段被掩码。
"""
from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Optional, TextIO

from .config import AutomationConfig, LoggingConfig
from .models import TaskType

__all__ = [
    "REDACTED",
    "redact_value",
    "scrub_text",
    "AutomationLogger",
    "build_logger",
    "LOG_LEVELS",
]

REDACTED = "***REDACTED***"

LOG_LEVELS: dict[str, int] = {
    "DEBUG": 10,
    "INFO": 20,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}

# 形如 key=value / key: value 的凭据片段
_KV_SECRET_RE = re.compile(
    r"(?i)\b(api[_-]?key|apikey|authorization|auth|access[_-]?token|refresh[_-]?token"
    r"|token|password|passwd|pwd|secret|cookie|session[_-]?id|credential"
    r"|private[_-]?key)\b\s*[:=]\s*(\"[^\"]*\"|'[^']*'|\S+)"
)
# Bearer / Basic 认证头
_BEARER_RE = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9\-._~+/=]{8,}")
# 长串疑似密钥（32 位以上的 base64/hex 连续串）
_LONG_TOKEN_RE = re.compile(r"\b[A-Za-z0-9_\-]{40,}\b")
# URL 中的 userinfo
_URL_USERINFO_RE = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@")


def scrub_text(text: str) -> str:
    """对自由文本做凭据掩码。"""
    if not text:
        return text
    out = _URL_USERINFO_RE.sub(rf"\1{REDACTED}@", text)
    # 顺序关键：先处理 `Bearer <token>`，否则 KV 规则会把 "Bearer" 误当成值吃掉，
    # 把真正的 token 留在明文里。
    out = _BEARER_RE.sub(rf"\1 {REDACTED}", out)
    out = _KV_SECRET_RE.sub(rf"\1={REDACTED}", out)
    out = _LONG_TOKEN_RE.sub(REDACTED, out)
    return out


def redact_value(value: Any, redact_keys: Iterable[str], *, _depth: int = 0) -> Any:
    """递归脱敏结构化数据。

    - 键名（小写、去分隔符后）命中 ``redact_keys`` → 整体替换。
    - 字符串值 → 走 ``scrub_text``。
    - 嵌套深度超过 12 层直接截断，避免恶意/异常结构导致栈溢出。
    """
    if _depth > 12:
        return "<max-depth>"
    keys = {k.lower().replace("-", "_") for k in redact_keys}

    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for k, v in value.items():
            key_norm = str(k).lower().replace("-", "_")
            if key_norm in keys or any(part in key_norm for part in keys):
                result[str(k)] = REDACTED
            else:
                result[str(k)] = redact_value(v, redact_keys, _depth=_depth + 1)
        return result
    if isinstance(value, (list, tuple, set)):
        return [redact_value(v, redact_keys, _depth=_depth + 1) for v in value]
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return scrub_text(str(value))


class AutomationLogger:
    """自动化结构化日志器。

    刻意不依赖标准库 ``logging`` 的全局状态，避免多次运行之间 handler 泄漏，
    也便于测试直接注入内存流。
    """

    def __init__(
        self,
        *,
        log_path: Optional[Path] = None,
        level: str = "INFO",
        console: bool = True,
        redact_keys: Optional[Iterable[str]] = None,
        run_id: str = "",
        task_type: Optional[TaskType] = None,
        as_of_date: Optional[date] = None,
        stream: Optional[TextIO] = None,
        now_fn=datetime.now,
    ) -> None:
        self.log_path = Path(log_path) if log_path is not None else None
        self.level = level.upper()
        self._threshold = LOG_LEVELS.get(self.level, 20)
        self.console = console
        self.redact_keys = list(redact_keys or [])
        self.run_id = run_id
        self.task_type = task_type
        self.as_of_date = as_of_date
        self._stream = stream
        self._now = now_fn
        self._step: Optional[str] = None
        self._records: list[dict[str, Any]] = []
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)

    # -- 上下文 -------------------------------------------------------- #

    def bind(
        self,
        *,
        run_id: Optional[str] = None,
        task_type: Optional[TaskType] = None,
        as_of_date: Optional[date] = None,
    ) -> "AutomationLogger":
        """就地绑定运行上下文（返回自身，便于链式调用）。"""
        if run_id is not None:
            self.run_id = run_id
        if task_type is not None:
            self.task_type = task_type
        if as_of_date is not None:
            self.as_of_date = as_of_date
        return self

    def step(self, name: Optional[str]) -> "AutomationLogger":
        """设置当前步骤名。"""
        self._step = name
        return self

    @property
    def records(self) -> list[dict[str, Any]]:
        """本次进程内产生的全部日志记录（测试与运行报告使用）。"""
        return list(self._records)

    # -- 写入 ---------------------------------------------------------- #

    def _emit(
        self,
        level: str,
        event: str,
        message: str,
        *,
        step: Optional[str] = None,
        exception: Optional[BaseException] = None,
        **detail: Any,
    ) -> Optional[dict[str, Any]]:
        if LOG_LEVELS.get(level, 20) < self._threshold:
            return None
        record = {
            "timestamp": self._now().isoformat(timespec="seconds"),
            "level": level,
            "run_id": self.run_id or None,
            "task_type": self.task_type.value if self.task_type else None,
            "as_of_date": self.as_of_date.isoformat() if self.as_of_date else None,
            "step": step if step is not None else self._step,
            "event": event,
            "message": scrub_text(str(message)),
            "exception_type": type(exception).__name__ if exception else None,
            "detail": redact_value(detail, self.redact_keys) if detail else {},
        }
        line = json.dumps(record, ensure_ascii=False)
        self._records.append(record)
        if self.log_path is not None:
            try:
                with self.log_path.open("a", encoding="utf-8", newline="\n") as fh:
                    fh.write(line + "\n")
            except OSError:  # pragma: no cover - 磁盘异常不应中断业务
                pass
        if self._stream is not None:
            self._stream.write(line + "\n")
        elif self.console:
            target = sys.stderr if LOG_LEVELS.get(level, 20) >= 40 else sys.stdout
            target.write(line + "\n")
        return record

    # -- 便捷方法 ------------------------------------------------------- #

    def debug(self, event: str, message: str, **detail: Any) -> None:
        self._emit("DEBUG", event, message, **detail)

    def info(self, event: str, message: str, **detail: Any) -> None:
        self._emit("INFO", event, message, **detail)

    def warning(self, event: str, message: str, **detail: Any) -> None:
        self._emit("WARNING", event, message, **detail)

    def error(
        self,
        event: str,
        message: str,
        *,
        exception: Optional[BaseException] = None,
        **detail: Any,
    ) -> None:
        self._emit("ERROR", event, message, exception=exception, **detail)

    def critical(
        self,
        event: str,
        message: str,
        *,
        exception: Optional[BaseException] = None,
        **detail: Any,
    ) -> None:
        self._emit("CRITICAL", event, message, exception=exception, **detail)


def build_logger(
    config: AutomationConfig,
    *,
    run_id: str = "",
    task_type: Optional[TaskType] = None,
    as_of_date: Optional[date] = None,
    log_date: Optional[date] = None,
    stream: Optional[TextIO] = None,
    console: Optional[bool] = None,
) -> AutomationLogger:
    """按配置构建日志器。

    日志文件名由 ``logging.filename_pattern`` 决定，``{date}`` 占位符
    使用 ``log_date``（默认取 ``as_of_date``，再默认取今天）。
    """
    log_cfg: LoggingConfig = config.logging
    stamp = log_date or as_of_date or date.today()
    filename = log_cfg.filename_pattern.format(date=stamp.isoformat())
    return AutomationLogger(
        log_path=config.logs_dir / filename,
        level=log_cfg.level,
        console=log_cfg.console if console is None else console,
        redact_keys=log_cfg.redact_keys,
        run_id=run_id,
        task_type=task_type,
        as_of_date=as_of_date,
        stream=stream,
    )
