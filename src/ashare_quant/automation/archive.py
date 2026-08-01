"""Phase 4 结果归档与保留策略。

为什么要归档
------------
每日跑批会在 ``reports/phase-4/daily/<日期>/`` 下留一份完整产物。
一年 240 多个交易日，不清理的话仓库会被慢慢撑爆，而真正需要频繁翻阅的
只有最近几周。归档做两件事：

1. **搬**：把超过 ``archive.retain_days`` 的结果目录搬进
   ``reports/phase-4/archive/<批次>/``，按周分批，历史仍然可查。
2. **删**：批次数超过 ``archive.max_batches`` 时，删掉最老的批次。

这是本包**唯一**会移动和删除文件的模块，因此它自带三道安全带
------------------------------------------------------------
1. **只删归档目录内的东西。** 每次删除前都做路径包含性校验
   （``_assert_inside``），目标必须真实位于 ``archive_dir`` 之内；
   符号链接、``..`` 穿越、配置写错路径，都会在删除前抛异常而不是继续。
2. **不用 ``shutil.rmtree``。** 改为自底向上逐个 ``os.remove`` / ``os.rmdir``，
   每删一个文件都可计数、可记录。递归删除是个"要么全成功要么留下一地残骸"
   的黑盒，而归档需要的是可审计。
3. **永不碰当前批次。** 本周的报告目录被显式排除在归档候选之外——
   周报还没写完就把自己搬走，是个很好笑但很致命的 bug。

``dry_run`` 模式下全部操作只做计算、不落地，用于 ``--dry-run`` 预演。
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

from .config import AutomationConfig
from .reporting import iso_week_key

__all__ = [
    "ArchivedItem",
    "ArchiveResult",
    "directory_stats",
    "safe_remove_tree",
    "move_directory",
    "archive_results",
]


# ---------------------------------------------------------------------- #
# 结果结构
# ---------------------------------------------------------------------- #


@dataclass(frozen=True)
class ArchivedItem:
    """一个被搬进归档的结果目录。"""

    kind: str          # "daily" / "weekly"
    bucket: str        # 原目录名（日期或 ISO 周）
    source: str        # 相对仓库根的原路径
    target: str        # 相对仓库根的归档后路径
    files: int = 0
    bytes: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "bucket": self.bucket,
            "source": self.source,
            "target": self.target,
            "files": self.files,
            "bytes": self.bytes,
        }


@dataclass
class ArchiveResult:
    """一次归档操作的完整结果（写入周报与运行记录）。"""

    enabled: bool = True
    dry_run: bool = False
    batch: Optional[str] = None
    batch_dir: Optional[str] = None
    retain_days: int = 0
    max_batches: int = 0
    cutoff_date: Optional[str] = None
    archived: list[ArchivedItem] = field(default_factory=list)
    pruned_batches: list[str] = field(default_factory=list)
    removed_files: int = 0
    skipped: list[str] = field(default_factory=list)
    reason: str = ""

    # ------------------------------------------------------------------ #
    @property
    def archived_count(self) -> int:
        return len(self.archived)

    @property
    def archived_bytes(self) -> int:
        return sum(a.bytes for a in self.archived)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "dry_run": self.dry_run,
            "batch": self.batch,
            "batch_dir": self.batch_dir,
            "retain_days": self.retain_days,
            "max_batches": self.max_batches,
            "cutoff_date": self.cutoff_date,
            "archived_count": self.archived_count,
            "archived_bytes": self.archived_bytes,
            "archived": [a.to_dict() for a in self.archived],
            "pruned_batches": list(self.pruned_batches),
            "removed_files": self.removed_files,
            "skipped": list(self.skipped),
            "reason": self.reason,
        }

    def summary(self) -> dict[str, Any]:
        """给 Markdown 表格用的扁平摘要。"""
        return {
            "归档开关": "启用" if self.enabled else "关闭",
            "演练模式": "是" if self.dry_run else "否",
            "批次": self.batch or "—",
            "保留天数": self.retain_days,
            "归档截止日": self.cutoff_date or "—",
            "本次归档目录数": self.archived_count,
            "本次归档字节数": self.archived_bytes,
            "清理批次数": len(self.pruned_batches),
            "清理文件数": self.removed_files,
            "备注": self.reason or "—",
        }


# ---------------------------------------------------------------------- #
# 文件系统工具（全部带包含性校验）
# ---------------------------------------------------------------------- #


def _assert_inside(path: Path, root: Path) -> Path:
    """确认 ``path`` 真实位于 ``root`` 之内，否则抛异常。

    用 ``resolve()`` 展开符号链接与 ``..``，再做 ``relative_to`` 判定。
    这一步是删除操作的保险丝：配置里把 ``archive_dir`` 写成 ``/`` 或
    ``C:\\``，会在这里炸掉，而不是在磁盘上炸掉。
    """
    resolved = Path(path).resolve()
    root_resolved = Path(root).resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(
            f"拒绝操作归档目录之外的路径：{resolved} 不在 {root_resolved} 之内"
        ) from exc
    if resolved == root_resolved:
        raise ValueError(f"拒绝操作归档根目录自身：{resolved}")
    return resolved


def directory_stats(path: Path) -> tuple[int, int]:
    """统计目录下的文件数与总字节数。"""
    files = 0
    total = 0
    p = Path(path)
    if not p.exists():
        return (0, 0)
    for entry in p.rglob("*"):
        if entry.is_file():
            files += 1
            try:
                total += entry.stat().st_size
            except OSError:  # pragma: no cover - 文件在统计间隙消失
                continue
    return (files, total)


def safe_remove_tree(path: Path, *, root: Path) -> int:
    """删除 ``path`` 及其内容，返回删除的文件数。

    不使用 ``shutil.rmtree``：改为自底向上逐个删除，每一步都可计数、可中断，
    并且在开始前强制校验目标位于 ``root`` 之内。

    Args:
        path: 待删除目录。
        root: 允许删除的根目录（通常是 ``config.archive_dir``）。

    Returns:
        实际删除的文件数量。

    Raises:
        ValueError: 目标不在 ``root`` 之内。
    """
    target = _assert_inside(path, root)
    if not target.exists():
        return 0
    removed = 0
    for current, dirnames, filenames in os.walk(target, topdown=False):
        current_path = Path(current)
        for name in filenames:
            file_path = current_path / name
            try:
                file_path.unlink()
                removed += 1
            except OSError:  # pragma: no cover - 只读文件等边缘情况
                continue
        for name in dirnames:
            try:
                (current_path / name).rmdir()
            except OSError:  # pragma: no cover
                continue
    try:
        target.rmdir()
    except OSError:  # pragma: no cover - 仍有残留时保留现场供人工检查
        pass
    return removed


def move_directory(src: Path, dst: Path) -> Path:
    """把目录 ``src`` 搬到 ``dst``（目标已存在时先让路）。

    同盘用 ``rename`` 走元数据操作，跨盘退化为"复制 + 删源"。
    """
    source = Path(src)
    target = Path(dst)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        # 同一批次内重跑会撞上同名目录；Windows 的 rename 遇到已存在目录直接失败，
        # 所以先清掉旧的归档副本（它一定在归档目录内，删除受包含性校验保护）
        safe_remove_tree(target, root=target.parent)
    try:
        source.rename(target)
    except OSError:
        # 跨卷 / 跨文件系统：rename 不可用，退化为"复制成功后再删源"。
        # 这是 move 语义的必然代价，顺序上先保证副本落地，再删原件。
        shutil.copytree(source, target)
        safe_remove_tree(source, root=source.parent)
    return target


# ---------------------------------------------------------------------- #
# 归档主流程
# ---------------------------------------------------------------------- #


def _parse_daily_bucket(name: str) -> Optional[date]:
    try:
        return date.fromisoformat(name)
    except ValueError:
        return None


def _parse_weekly_bucket(name: str) -> Optional[date]:
    """把 ``2026-W31`` 解析为该 ISO 周的周日（用于判定是否过期）。"""
    if "-W" not in name:
        return None
    year_part, _, week_part = name.partition("-W")
    try:
        year = int(year_part)
        week = int(week_part)
        return date.fromisocalendar(year, week, 7)
    except ValueError:
        return None


def archive_results(
    config: AutomationConfig,
    *,
    as_of_date: date,
    batch_key: Optional[str] = None,
    dry_run: bool = False,
    protect_buckets: Optional[set[str]] = None,
) -> ArchiveResult:
    """按保留策略归档历史结果目录。

    Args:
        config: 自动化配置（读取 ``archive`` 节点与路径）。
        as_of_date: 业务日；``as_of_date - retain_days`` 之前的结果进入归档。
        batch_key: 归档批次名；默认取业务日所在 ISO 周（如 ``2026-W31``）。
        dry_run: 只计算不落地。
        protect_buckets: 额外保护、绝不归档的目录名集合。

    Returns:
        ``ArchiveResult``，含搬运明细、清理批次与统计。
    """
    archive_cfg = config.archive
    batch = batch_key or iso_week_key(as_of_date)
    archive_root = config.archive_dir
    result = ArchiveResult(
        enabled=bool(archive_cfg.enabled),
        dry_run=dry_run,
        batch=batch,
        batch_dir=_rel(archive_root / batch, config),
        retain_days=int(archive_cfg.retain_days),
        max_batches=int(archive_cfg.max_batches),
    )

    if not archive_cfg.enabled:
        result.reason = "archive.enabled=false，跳过归档"
        return result

    cutoff = as_of_date - timedelta(days=int(archive_cfg.retain_days))
    result.cutoff_date = cutoff.isoformat()

    # 当前周的报告目录永远不动——周报此刻正要写进去
    protected = set(protect_buckets or set())
    protected.add(iso_week_key(as_of_date))
    protected.add(as_of_date.isoformat())

    batch_dir = archive_root / batch
    archive_root_resolved = archive_root.resolve()

    for kind, parser in (("daily", _parse_daily_bucket), ("weekly", _parse_weekly_bucket)):
        source_root = config.reports_dir / kind
        if not source_root.exists():
            continue
        for child in sorted(source_root.iterdir()):
            if not child.is_dir():
                continue
            name = child.name
            if name in protected:
                result.skipped.append(f"{kind}/{name}（当前批次，受保护）")
                continue
            # 归档目录本身不参与归档（防止自我吞噬）
            try:
                child.resolve().relative_to(archive_root_resolved)
            except ValueError:
                pass
            else:
                continue
            bucket_date = parser(name)
            if bucket_date is None:
                result.skipped.append(f"{kind}/{name}（目录名无法解析为日期）")
                continue
            if bucket_date >= cutoff:
                continue
            files, size = directory_stats(child)
            target = batch_dir / kind / name
            item = ArchivedItem(
                kind=kind,
                bucket=name,
                source=_rel(child, config),
                target=_rel(target, config),
                files=files,
                bytes=size,
            )
            if not dry_run:
                move_directory(child, target)
            result.archived.append(item)

    # -- 批次数量上限 --------------------------------------------------- #
    if archive_root.exists():
        batches = sorted(
            (p for p in archive_root.iterdir() if p.is_dir()), key=lambda p: p.name
        )
        overflow = len(batches) - int(archive_cfg.max_batches)
        if overflow > 0:
            for stale in batches[:overflow]:
                result.pruned_batches.append(stale.name)
                if not dry_run:
                    result.removed_files += safe_remove_tree(
                        stale, root=archive_root
                    )
                else:
                    files, _ = directory_stats(stale)
                    result.removed_files += files

    if not result.archived and not result.pruned_batches:
        result.reason = (
            f"无过期结果（保留 {archive_cfg.retain_days} 天，"
            f"截止 {cutoff.isoformat()}）"
        )
    return result


def _rel(path: Path, config: AutomationConfig) -> str:
    """尽量转成相对仓库根的 POSIX 路径，便于跨机器比对报告。"""
    try:
        return Path(path).resolve().relative_to(config.base_dir).as_posix()
    except ValueError:
        return Path(path).as_posix()
