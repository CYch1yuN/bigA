"""幂等性：确定性 run_id 与输入哈希。

``run_id = f(task_type, as_of_date, code_commit, config_hash, input_hash)``

同样的任务类型、同样的业务日、同样的代码、同样的配置、同样的输入数据，
必然得到同一个 ``run_id``；任何一项变化都会得到新的 ``run_id``。

这保证了：

- 重复执行不会产生重复产物（同 run_id 直接命中已有终态记录）。
- 代码或配置变更后，重跑会被识别为一次**新的**运行，而不是静默复用旧结果。
"""
from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from ..manifest import get_code_commit
from ..storage import file_sha256
from .config import AutomationConfig
from .models import TaskType

__all__ = [
    "compute_config_hash",
    "compute_input_hash",
    "compute_payload_hash",
    "compute_run_id",
    "RunFingerprint",
    "build_fingerprint",
]

_HASH_PREFIX_LEN = 16
"""哈希截断长度：16 个十六进制字符（64 bit），足以避免实践中的碰撞。"""


def _sha256_hex(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_payload_hash(obj: Any) -> str:
    """对任意可 JSON 化对象计算规范化哈希。"""
    payload = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    return _sha256_hex(payload)[:_HASH_PREFIX_LEN]


def compute_config_hash(config: AutomationConfig) -> str:
    """计算配置哈希。

    仅基于配置内容（相对路径形式），不含机器相关的绝对路径，
    因此同一份配置在不同机器上得到相同哈希。
    """
    return _sha256_hex(config.config_hash_payload())[:_HASH_PREFIX_LEN]


def compute_input_hash(
    paths: Iterable[str | Path],
    *,
    base_dir: Optional[str | Path] = None,
    extra: Optional[dict[str, Any]] = None,
) -> str:
    """计算输入数据哈希。

    对每个存在的文件计算 SHA-256 内容哈希，与其相对路径一起排序后再整体哈希。
    不存在的文件以 ``<missing>`` 参与计算——文件缺失同样是一种确定性输入状态。

    Args:
        paths: 输入文件路径集合。
        base_dir: 用于生成稳定相对路径的基准目录。
        extra: 额外参与哈希的结构化输入（如标的列表、日期范围）。

    Returns:
        16 位十六进制哈希。
    """
    root = Path(base_dir).resolve() if base_dir is not None else None
    entries: list[tuple[str, str]] = []
    for raw in paths:
        p = Path(raw)
        if root is not None:
            try:
                key = p.resolve().relative_to(root).as_posix()
            except ValueError:
                key = p.as_posix()
        else:
            key = p.as_posix()
        digest = file_sha256(p) if p.exists() and p.is_file() else "<missing>"
        entries.append((key, digest))
    entries.sort()
    payload: dict[str, Any] = {"files": entries}
    if extra:
        payload["extra"] = extra
    return compute_payload_hash(payload)


def compute_run_id(
    *,
    task_type: TaskType | str,
    as_of_date: date,
    code_commit: str,
    config_hash: str,
    input_hash: str,
) -> str:
    """计算确定性 run_id。

    格式：``{task_type}-{YYYYMMDD}-{16位哈希}``，例如
    ``daily-20260731-3f2a91c4d5e60718``。

    前缀可读，便于在文件名与日志中人工定位；后缀哈希保证唯一性。
    """
    task = task_type.value if isinstance(task_type, TaskType) else str(task_type)
    payload = "|".join(
        [
            task,
            as_of_date.isoformat(),
            code_commit or "no-git",
            config_hash or "",
            input_hash or "",
        ]
    )
    digest = _sha256_hex(payload)[:_HASH_PREFIX_LEN]
    return f"{task}-{as_of_date.strftime('%Y%m%d')}-{digest}"


class RunFingerprint:
    """一次运行的完整指纹。"""

    __slots__ = (
        "task_type",
        "as_of_date",
        "code_commit",
        "config_hash",
        "input_hash",
        "run_id",
        "input_files",
    )

    def __init__(
        self,
        *,
        task_type: TaskType,
        as_of_date: date,
        code_commit: str,
        config_hash: str,
        input_hash: str,
        input_files: Sequence[str] = (),
    ) -> None:
        self.task_type = task_type
        self.as_of_date = as_of_date
        self.code_commit = code_commit
        self.config_hash = config_hash
        self.input_hash = input_hash
        self.input_files = list(input_files)
        self.run_id = compute_run_id(
            task_type=task_type,
            as_of_date=as_of_date,
            code_commit=code_commit,
            config_hash=config_hash,
            input_hash=input_hash,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_type": self.task_type.value,
            "as_of_date": self.as_of_date.isoformat(),
            "code_commit": self.code_commit,
            "config_hash": self.config_hash,
            "input_hash": self.input_hash,
            "input_files": list(self.input_files),
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"RunFingerprint({self.run_id})"


def build_fingerprint(
    config: AutomationConfig,
    *,
    task_type: TaskType,
    as_of_date: date,
    input_paths: Optional[Sequence[str | Path]] = None,
    extra_inputs: Optional[dict[str, Any]] = None,
    code_commit: Optional[str] = None,
) -> RunFingerprint:
    """构建运行指纹。

    Args:
        config: 自动化配置。
        task_type: 任务类型。
        as_of_date: 业务日。
        input_paths: 输入文件（数据 Parquet、交易日历等）。
        extra_inputs: 额外结构化输入（标的清单、回看窗口等）。
        code_commit: 代码提交号；为 None 时自动读取。
    """
    paths = list(input_paths or [])
    commit = code_commit if code_commit is not None else get_code_commit()
    cfg_hash = compute_config_hash(config)
    in_hash = compute_input_hash(paths, base_dir=config.base_dir, extra=extra_inputs)
    rel_files: list[str] = []
    for p in paths:
        path = Path(p)
        try:
            rel_files.append(path.resolve().relative_to(config.base_dir).as_posix())
        except ValueError:
            rel_files.append(path.as_posix())
    rel_files.sort()
    return RunFingerprint(
        task_type=task_type,
        as_of_date=as_of_date,
        code_commit=commit,
        config_hash=cfg_hash,
        input_hash=in_hash,
        input_files=rel_files,
    )
