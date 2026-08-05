"""F5-A：Westock 缓存运营与可观测性（只读）——第三轮边界修正版。

数据来源仅限：
- state/dashboard/westock/**/*.json（物理缓存文件，raw inspection；含未知 capability 目录）
- state/dashboard/westock-refresh/{requests,receipts}/*.json（F3/F4 请求/回执）
- CAPABILITY_MAP（TTL/group/read_only）
- 本地 curated 股票集合（仅标注 local_history_available，不修改 parquet）

第三轮边界修正核心口径：
1. receipt 逐字段绑定 request（正式 receipt projection）：
   - 比较 request_id/target/created_at/started_at/finished_at/status/status_detail/
     warnings(最后10条)/jobs 数量与 job_id 集合/每个 job 的 capability/scope/status/
     fetched_at/cache_status/data_as_of/content_hash/warning；顺序可不同但按 job_id 索引。
   - 任何差异 → mismatch（同 counts 不同 job_id/capability/scope/hash 等均 mismatch）。
2. receipt-required 状态 = completed/partial/failed；pending/processing/cancelled/expired
   → not_applicable（cancelled/expired 不产生 receipt，不计 missing）。
3. hash verified 依赖精确匹配的 valid receipt；evidence 所在请求 receipt 非精确 valid
   → 不 verified；processing 请求 ok job → pending_evidence。
4. raw 分类顺序：先完整 envelope 结构校验（tool/transport/source/warnings/data，
   不含时间值），再分类时间；evil transport + future timestamp → invalid_envelope。
   future/invalid 文件不回传原始 fetched_at/cached_at/as_of（统一 null）、
   不读取/展示 data、不调 validator、不计算 hash。
5. 请求 warning/status_detail 脱敏：只返回 warning_count/warning_categories（固定枚举）
   与 status_detail_code（固定受控摘要）；绝不返回原 warning/status_detail 文本。
6. unexpected/非法 scope 不公开文件名：合法 SYMBOL/global 原样，q 只公开 short_scope，
   非法 scope → 固定 "invalid_scope"+ 服务端序号 scope_id（u1,u2,...）。
7. physical inventory 覆盖未知 capability 目录与 cache root 直接 JSON：
   - 计入 physical/unexpected/invalid physical；不公开未知目录名与文件名
     （capability 固定 "unknown"、scope "invalid_scope"）；不进入 expected coverage；
     symlink/逃逸仍拒绝。
8. caches 返回过滤后 total（分页依据）+ coverage_total/inventory_total/
   unexpected_physical_count/limit/offset/items。
9. 时间排序用 aware datetime 解析后比较（跨时区偏移 ISO 字典序错误）；
   相同时间用稳定 ID 次级排序；非法时间防御排最末。
10. orphan receipt（合法 receipt 无对应合法 request）与非法 receipt 文件
    计入 orphan_receipt_count/invalid_receipt_file_count（只公开计数）。

禁止返回：token/credential/cookie/Authorization、密码/哈希/session secret、
worker_id、session fingerprint、服务端绝对路径、原始 MCP 响应、完整 content_hash、
原始异常堆栈、request 原始 warning/status_detail 文本、意外文件名/未知目录名。
"""
from __future__ import annotations

import copy
import json
import hashlib
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .westock_bridge import CAPABILITY_MAP, _validate_envelope, _utc_now, WestockCacheStore
from .stocks_service import SYMBOL_RE, _parse_iso_ts
from .westock_refresh_service import (
    MAX_RECEIPT_BYTES,
    MAX_REQUEST_BYTES,
    Q_SCOPE_RE,
    RefreshError,
    _GLOBAL_SHOW_CAPS,
    _STOCK_CAPS,
    _SUMMARY_BLOCKED_CAPS,
    _stock_consumer_validator,
    _valid_receipt,
    _valid_request_file,
)

SCHEMA_VERSION = 1
SOURCE = "westock-mcp"
MAX_RAW_BYTES = 4 * 1024 * 1024  # raw inspection 大小上限（4 MiB）

# Asia/Shanghai = UTC+08:00 恒定（中国不实行夏令时），避免 zoneinfo/tzdata 依赖
_SHANGHAI_TZ = timezone(timedelta(hours=8))

FAILURE_CATEGORIES = (
    "upstream_empty", "upstream_rate_limited", "upstream_unavailable",
    "unsupported", "consumer_validation_failed", "identity_mismatch",
    "invalid_envelope", "stale", "future_timestamp", "worker_timeout",
    "export_failed", "receipt_failed", "cancelled", "expired", "unknown",
)

SCOPE_TYPES = ("stock", "global", "market", "query", "invalid")
FRESHNESSES = ("fresh", "stale", "future_timestamp", "invalid_timestamp", "unavailable")
CONSUMER_STATUSES = ("usable", "unusable", "not_validated")
REQUEST_STATUSES = ("pending", "processing", "completed", "partial", "failed",
                    "cancelled", "expired")
FILE_STATES = ("missing", "invalid_json", "invalid_envelope", "invalid_timestamp",
               "future_timestamp", "valid")
HASH_STATUSES = ("verified", "unverified", "mismatch", "pending_evidence")
RECEIPT_STATUSES = ("valid", "missing", "invalid", "mismatch", "not_applicable")
# F3 _valid_receipt 只允许聚合终态 completed/partial/failed；cancelled/expired 不产生回执
_RECEIPT_REQUIRED_STATUSES = frozenset(("completed", "partial", "failed"))

FILTER_KEYS = ("capability", "symbol", "scope_type", "freshness", "consumer_status",
               "failure_category", "request_status", "limit", "offset")

_INVALID_PHYSICAL_STATES = ("invalid_json", "invalid_envelope",
                            "invalid_timestamp", "future_timestamp")
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_HASH_RE = re.compile(r"[0-9a-f]{64}")
# receipt 投影中每个 job 需要逐字段一致的键（与 refresh_service._build_receipt 白名单一致）
_RECEIPT_JOB_FIELDS = ("job_id", "capability", "scope", "status",
                       "fetched_at", "cache_status", "data_as_of",
                       "content_hash", "warning")

_INVALID_SCOPE_MARKER = "invalid_scope"
_UNKNOWN_CAP_MARKER = "unknown"


def _shanghai_today() -> datetime.date:
    """Asia/Shanghai 当前日期（UTC+8 恒定偏移）。"""
    return datetime.now(_SHANGHAI_TZ).date()


def _parse_strict_date(value: Any) -> datetime.date | None:
    """严格真实日历日期：2026-02-30 → None（绝不抛错）。"""
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _ts_sort_key(value: Any) -> float:
    """aware datetime 解析后的绝对时间排序键；非法/缺失排最前（升序）。"""
    dt = _parse_iso_ts(value)
    return dt.timestamp() if dt else -1e18


def _safe_div(numerator: float | int, denominator: float | int) -> float | None:
    """除零守卫：分母为 0 返回 None（不产生 NaN/Infinity）。"""
    if denominator is None or denominator == 0:
        return None
    return round(numerator / denominator, 4)


def _category_from_warning(warning: str) -> str:
    """脱敏 warning → 固定 failure_category（受控映射，不以原始 warning 为键）。"""
    if not warning:
        return "unknown"
    w = warning.lower()
    if "worker_timeout" in w:
        return "worker_timeout"
    if "consumer_validation_failed" in w:
        return "consumer_validation_failed"
    if "限频" in w or "rate" in w:
        return "upstream_rate_limited"
    if "上游无数据" in w or "空" in w or "empty" in w:
        return "upstream_empty"
    if "未导出" in w or "export_failed" in w:
        return "export_failed"
    if "不支持" in w:
        return "unsupported"
    if "身份" in w or "identity" in w:
        return "identity_mismatch"
    if "不可用" in w or "unavailable" in w:
        return "upstream_unavailable"
    return "unknown"


def _scope_type(scope: str, capability: str) -> str:
    if SYMBOL_RE.fullmatch(scope):
        return "stock"
    if Q_SCOPE_RE.fullmatch(scope):
        return "query"
    if scope == "global":
        return "market" if CAPABILITY_MAP[capability].group == "市场" else "global"
    return "invalid"


def _short_scope(scope: str) -> str:
    """q_<64hex> 只展示缩写 q_abcd…efgh。"""
    if Q_SCOPE_RE.fullmatch(scope):
        return f"{scope[:7]}…{scope[-4:]}"
    return scope


def _job_counts(jobs: list[dict[str, Any]]) -> dict[str, int]:
    counts = {"ok": 0, "partial": 0, "failed": 0, "skipped": 0, "pending": 0}
    for j in jobs or []:
        status = j.get("status")
        if status in counts:
            counts[status] += 1
        else:
            counts["pending"] += 1
    return counts


def _validate_envelope_except_time(value: Any, capability: str, scope: str) -> bool:
    """完整 envelope 结构校验（不含 fetched_at/cached_at 时间值分类）。

    等价于 bridge._validate_envelope 但跳过时间解析：tool/transport/source/
    warnings/data/schema/capability/scope 任一被篡改即 False。
    """
    if not isinstance(value, dict):
        return False
    if value.get("schema_version") != SCHEMA_VERSION:
        return False
    if value.get("capability") != capability or value.get("scope") != scope:
        return False
    definition = CAPABILITY_MAP.get(capability)
    if definition is None or value.get("tool") != definition.tool:
        return False
    if value.get("transport") != "cache_export":
        return False
    source = value.get("source")
    if not isinstance(source, str) or not source:
        return False
    if "data" not in value:
        return False
    if not isinstance(value.get("warnings"), list):
        return False
    return True


def _receipt_projection(req: dict[str, Any]) -> dict[str, Any]:
    """与 refresh_service.finish._build_receipt 完全一致的受控投影（用于精确比对）。"""
    return {
        "request_id": req.get("request_id"),
        "target": req.get("target"),
        "jobs": [{f: j.get(f) for f in _RECEIPT_JOB_FIELDS}
                 for j in req.get("jobs") or []],
        "created_at": req.get("created_at"),
        "started_at": req.get("started_at"),
        "finished_at": req.get("finished_at"),
        "status": req.get("status"),
        "status_detail": req.get("status_detail"),
        "warnings": list(req.get("warnings") or [])[-10:],
    }


class WestockOperationsService:
    """只读运营服务（第三轮边界修正版）。"""

    def __init__(self, cache: WestockCacheStore, refresh: Any, curated_dir: Path):
        self.cache = cache
        self.refresh = refresh
        self.curated_dir = Path(curated_dir)

    # ------------------------------------------------------------------ #
    # 基础扫描
    # ------------------------------------------------------------------ #
    def _local_symbols(self) -> set[str]:
        if not self.curated_dir.is_dir():
            return set()
        out: set[str] = set()
        for path in self.curated_dir.glob("daily_quotes_*_*.parquet"):
            m = re.match(r"daily_quotes_([0-9]{6}\.(?:SH|SZ|BJ))_", path.name)
            if m:
                out.add(m.group(1))
        return out

    def _iter_cache_paths(self):
        """扫描 cache root 下全部 JSON（含未知 capability 目录与 root 直接文件）。

        不在此处读取/公开目录名与文件名——识别工作交由 _inspect_raw/_evaluate
        的受控分支（capability 固定 "unknown"、scope 固定 "invalid_scope"）。
        """
        base = Path(self.cache.root)
        if not base.is_dir():
            return
        for file in sorted(base.glob("*.json")):
            yield "__root__", file.stem
        for cap_dir in sorted(base.iterdir()):
            if not cap_dir.is_dir():
                continue
            for file in sorted(cap_dir.glob("*.json")):
                yield cap_dir.name, file.stem

    # ------------------------------------------------------------------ #
    # raw inspection：先结构校验、后时间分类（不泄露原始字符串）
    # ------------------------------------------------------------------ #
    def _inspect_raw(self, capability: str, scope: str) -> str:
        path = Path(self.cache.root) / capability / f"{scope}.json"
        # symlink / 解析后逃离 cache root → 受控 invalid_envelope（不读入）
        try:
            resolved = path.resolve()
        except OSError:
            return "invalid_envelope"
        root_str = os.path.normcase(str(Path(self.cache.root).resolve()))
        res_str = os.path.normcase(str(resolved))
        if not (res_str == root_str or res_str.startswith(root_str + os.sep)):
            return "invalid_envelope"
        try:
            if path.is_symlink():
                return "invalid_envelope"
        except OSError:
            return "invalid_envelope"
        if not path.exists():
            return "missing"
        try:
            size = path.stat().st_size
        except OSError:
            return "invalid_envelope"  # stat 失败 fail-open
        if size > MAX_RAW_BYTES:
            return "invalid_envelope"  # 过大按不可信处理（不读入）
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return "invalid_json"
        if not isinstance(value, dict):
            return "invalid_json"
        # 未知 capability（含 __root__）：结构无从校验，一律受控 invalid_envelope
        if capability not in CAPABILITY_MAP:
            return "invalid_envelope"
        # 1) 完整 envelope 结构校验（不含时间值）：tool/transport/source/warnings/data
        if not _validate_envelope_except_time(value, capability, scope):
            return "invalid_envelope"
        # 2) 时间分类：先非法后 future
        fetched = _parse_iso_ts(value.get("fetched_at"))
        cached = _parse_iso_ts(value.get("cached_at"))
        if fetched is None or cached is None:
            return "invalid_timestamp"
        now = _utc_now()
        if fetched > now or cached > now:
            return "future_timestamp"
        return "valid"

    def _inventory_state(self) -> dict[tuple[str, str], str]:
        return {
            (cap, scope): self._inspect_raw(cap, scope)
            for cap, scope in self._iter_cache_paths()
        }

    # ------------------------------------------------------------------ #
    # 刷新历史 / receipt（内部读取；不返回 content_hash/worker/session/path）
    # ------------------------------------------------------------------ #
    def _iter_requests(self):
        base = Path(self.refresh.requests_dir)
        if not base.is_dir():
            return
        for path in sorted(base.glob("*.json")):
            try:
                if path.is_symlink() or path.stat().st_size > MAX_REQUEST_BYTES:
                    continue
                if path.resolve().parent != base.resolve():
                    continue
            except OSError:
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not _valid_request_file(data):
                continue
            yield data

    def _iter_receipts(self) -> tuple[dict[str, dict[str, Any]], set[str]]:
        """合法 receipt 字典 + 目录中存在的全部文件名（含非法，用于 invalid 判定）。"""
        receipts: dict[str, dict[str, Any]] = {}
        files: set[str] = set()
        base = Path(self.refresh.receipts_dir)
        if not base.is_dir():
            return receipts, files
        for path in sorted(base.glob("*.json")):
            files.add(path.stem)
            try:
                if path.is_symlink() or path.stat().st_size > MAX_RECEIPT_BYTES:
                    continue
                if path.resolve().parent != base.resolve():
                    continue
            except OSError:
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if _valid_receipt(value) and value.get("request_id") == path.stem:
                receipts[path.stem] = value
        return receipts, files

    def _receipt_matches(self, req: dict[str, Any], receipt: dict[str, Any]) -> bool:
        """receipt 与 request 的正式 projection 逐字段精确一致（顺序可不同）。"""
        if receipt.get("request_id") != req.get("request_id"):
            return False
        if receipt.get("status") != req.get("status"):
            return False
        if receipt.get("status_detail") != req.get("status_detail"):
            return False
        if receipt.get("target") != req.get("target"):
            return False
        if receipt.get("created_at") != req.get("created_at"):
            return False
        if receipt.get("started_at") != req.get("started_at"):
            return False
        if receipt.get("finished_at") != req.get("finished_at"):
            return False
        if list(receipt.get("warnings") or [])[-10:] != list(req.get("warnings") or [])[-10:]:
            return False
        rjobs = receipt.get("jobs") or []
        qjobs = req.get("jobs") or []
        if len(rjobs) != len(qjobs):
            return False
        rindex = {j.get("job_id"): j for j in rjobs}
        qindex = {j.get("job_id"): j for j in qjobs}
        if set(rindex) != set(qindex):
            return False
        for jid, rj in rindex.items():
            qj = qindex.get(jid)
            if qj is None:
                return False
            for field in _RECEIPT_JOB_FIELDS:
                if rj.get(field) != qj.get(field):
                    return False
        return True

    def _receipt_status_of(self, req: dict[str, Any], receipts: dict[str, dict[str, Any]],
                           receipt_files: set[str]) -> str:
        status = req.get("status")
        if status not in _RECEIPT_REQUIRED_STATUSES:
            return "not_applicable"  # pending/processing/cancelled/expired 不产生回执
        rid = req.get("request_id")
        receipt = receipts.get(rid)
        if receipt is None:
            return "invalid" if rid in receipt_files else "missing"
        return "valid" if self._receipt_matches(req, receipt) else "mismatch"

    def _refresh_lookup(self, requests: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
        """(capability, scope) → 最近状态 + 导出证据 + 终态 job 事件计数。

        - latest_job_status：最近任意终态 job（ok/partial/failed/skipped）。
        - export_evidence：仅 ok/partial 且 content_hash 合法的最近 job 证据（含 request_id）。
        - success_count/attempt_count：全部终态 job 事件。
        时间比较一律用 aware datetime 解析后比较（跨时区偏移 ISO 字典序不可靠）。
        """
        lookup: dict[tuple[str, str], dict[str, Any]] = {}
        for req in requests:
            for job in req.get("jobs") or []:
                status = job.get("status")
                if status not in ("ok", "partial", "failed", "skipped"):
                    continue
                stamp = job.get("recorded_at") or req.get("finished_at") or req.get("updated_at")
                key = (job["capability"], job["scope"])
                entry = lookup.setdefault(key, {
                    "latest_status": None, "export_evidence": None,
                    "success_count": 0, "attempt_count": 0,
                })
                entry["attempt_count"] += 1
                if status in ("ok", "partial"):
                    entry["success_count"] += 1
                prev = entry["latest_status"]
                if prev is None or _ts_sort_key(stamp) >= _ts_sort_key(prev.get("recorded_at")):
                    category = None
                    if status == "failed":
                        category = _category_from_warning(job.get("warning") or "")
                        if req.get("status_detail") == "worker_timeout":
                            category = "worker_timeout"
                    entry["latest_status"] = {
                        "status": status, "recorded_at": stamp,
                        "warning": job.get("warning"),
                        "failure_category": category,
                    }
                if (status in ("ok", "partial")
                        and isinstance(job.get("content_hash"), str)
                        and _HASH_RE.fullmatch(job["content_hash"])):
                    prev_ev = entry["export_evidence"]
                    if prev_ev is None or _ts_sort_key(stamp) >= _ts_sort_key(prev_ev.get("recorded_at")):
                        entry["export_evidence"] = {
                            "recorded_at": stamp, "content_hash": job["content_hash"],
                            "request_id": req["request_id"], "status": status,
                        }
        return lookup

    def _summary_only_symbols(self, requests: list[dict[str, Any]]) -> set[str]:
        out: set[str] = set()
        for req in requests:
            for s in (req.get("target") or {}).get("summary_only_symbols") or []:
                out.add(s)
        return out

    # ------------------------------------------------------------------ #
    # 预期覆盖矩阵（coverage_cells）
    # ------------------------------------------------------------------ #
    def _coverage_cells(self, requests: list[dict[str, Any]]) -> dict[tuple[str, str], str]:
        """完整预期观察矩阵 cell → scope 语义（stock/global/query）。

        只由本地 curated / summary-only / 合法缓存识别 / global 展示 /
        合法请求 job 的 q_<64hex> 构成；未知 capability 与非法 scope 绝不进入。
        """
        local = self._local_symbols()
        cells: dict[tuple[str, str], str] = {}
        for sym in local:
            for cap in _STOCK_CAPS:
                cells[(cap, sym)] = "stock"
        for sym in self._summary_only_symbols(requests):
            for cap in set(_STOCK_CAPS) - _SUMMARY_BLOCKED_CAPS:
                cells[(cap, sym)] = "stock"
        for cap, scope in self._iter_cache_paths():
            if cap in CAPABILITY_MAP and SYMBOL_RE.fullmatch(scope) and scope not in local:
                for c in set(_STOCK_CAPS) - _SUMMARY_BLOCKED_CAPS:
                    cells[(c, scope)] = "stock"
        for cap in _GLOBAL_SHOW_CAPS:
            cells[(cap, "global")] = "global"
        for req in requests:
            for job in req.get("jobs") or []:
                scope = job.get("scope")
                if isinstance(scope, str) and Q_SCOPE_RE.fullmatch(scope):
                    cells[(job["capability"], scope)] = "query"
        return cells

    # ------------------------------------------------------------------ #
    # 统一评估
    # ------------------------------------------------------------------ #
    def _unknown_entry(self, capability: str, scope: str,
                       state: str, ctx: dict[str, Any]) -> dict[str, Any]:
        """未知 capability / root JSON 的受控记录：不公开目录名与文件名。"""
        return {
            "capability": _UNKNOWN_CAP_MARKER,
            "scope": _INVALID_SCOPE_MARKER,
            "short_scope": "非法 Scope",
            "scope_id": None,  # 由 _inventory_entries 分配稳定序号
            "scope_type": "invalid",
            "group": "未知能力",
            "file_state": "invalid_envelope",
            "in_expected_matrix": False,
            "availability": "unavailable",
            "cache_status": "unavailable",
            "freshness_status": "unavailable",
            "consumer_status": "not_validated",
            "integrity": {"valid": False, "hash_verified": False,
                          "hash_status": "unverified"},
            "age_seconds": None,
            "ttl_seconds": None,
            "expires_at": None,
            "as_of": None,
            "fetched_at": None,
            "cached_at": None,
            "last_refresh_status": "never",
            "failure_category": "invalid_envelope",
            "local_history_available": False,
            "summary_only": False,
        }

    def _evaluate(self, capability: str, scope: str, file_state: str,
                  refresh_map: dict[tuple[str, str], dict[str, Any]],
                  ctx: dict[str, Any], in_matrix: bool) -> dict[str, Any]:
        if capability not in CAPABILITY_MAP:
            return self._unknown_entry(capability, scope, file_state, ctx)
        definition = CAPABILITY_MAP[capability]
        envelope: dict[str, Any] | None = None
        data_hash: str | None = None
        # future/invalid 不读取/展示 data、不回传原始时间字符串
        if file_state == "valid":
            try:
                value = json.loads((Path(self.cache.root) / capability / f"{scope}.json")
                                   .read_text(encoding="utf-8"))
                if isinstance(value, dict):
                    envelope = value
                    data_hash = hashlib.sha256(
                        json.dumps(value["data"], ensure_ascii=False, sort_keys=True)
                        .encode("utf-8")).hexdigest()
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                envelope = None
                file_state = "invalid_json"  # fail-open 降级为受控状态

        available = file_state == "valid"
        freshness: str = "unavailable"
        fetched = None
        if file_state == "valid":
            fetched = _parse_iso_ts(envelope.get("fetched_at")) if envelope else None
            age = (_utc_now() - fetched).total_seconds() if fetched else 0
            freshness = "fresh" if age <= definition.ttl_seconds else "stale"
        elif file_state == "future_timestamp":
            freshness = "future_timestamp"
        elif file_state == "invalid_timestamp":
            freshness = "invalid_timestamp"
        cache_status = "fresh" if freshness == "fresh" else (
            "stale" if freshness == "stale" else "unavailable")

        consumer = "not_validated"
        if available and envelope is not None:
            validator = _stock_consumer_validator(capability, scope)
            if validator is not None:
                try:
                    consumer = "usable" if validator(copy.deepcopy(envelope)) else "unusable"
                except Exception:
                    consumer = "unusable"

        hash_status = "unverified"
        entry = refresh_map.get((capability, scope))
        evidence = (entry or {}).get("export_evidence")
        if data_hash is not None and evidence:
            if evidence["content_hash"] != data_hash:
                hash_status = "mismatch"  # 硬证据矛盾优先
            else:
                rid = evidence["request_id"]
                req_status = (ctx.get("requests_by_id") or {}).get(rid, {}).get("status")
                receipt_status = (ctx.get("receipt_status_by_request") or {}).get(rid)
                # verified 依赖证据所在请求 receipt 精确 valid
                if receipt_status == "valid":
                    hash_status = "verified"
                elif req_status in ("pending", "processing"):
                    hash_status = "pending_evidence"
                else:
                    hash_status = "unverified"

        last = (entry or {}).get("latest_status")
        last_status = (last or {}).get("status") or "never"
        age_seconds = None
        expires_at = None
        if fetched is not None and available:
            age_seconds = max(0, int((_utc_now() - fetched).total_seconds()))
            expires_at = (fetched + timedelta(seconds=definition.ttl_seconds)).isoformat()

        # scope 脱敏：合法 SYMBOL/global 原样；q 只公开 short_scope；其余固定 invalid_scope
        out_scope: str = scope
        out_short: str = _short_scope(scope)
        if Q_SCOPE_RE.fullmatch(scope):
            out_scope = out_short
        elif not (SYMBOL_RE.fullmatch(scope) or scope == "global"):
            out_scope = _INVALID_SCOPE_MARKER
            out_short = "非法 Scope"

        return {
            "capability": capability,
            "scope": out_scope,
            "short_scope": out_short,
            "scope_id": None,  # unexpected 项由 _inventory_entries 分配序号
            "scope_type": _scope_type(scope, capability),
            "group": definition.group,
            "file_state": file_state,
            "in_expected_matrix": in_matrix,
            "availability": "available" if available else "unavailable",
            "cache_status": cache_status,
            "freshness_status": freshness,
            "consumer_status": consumer,
            "integrity": {
                "valid": available,
                "hash_verified": hash_status == "verified",
                "hash_status": hash_status,
            },
            "age_seconds": age_seconds,
            "ttl_seconds": definition.ttl_seconds,
            "expires_at": expires_at,
            "as_of": envelope.get("as_of") if envelope else None,
            "fetched_at": envelope.get("fetched_at") if envelope else None,
            "cached_at": envelope.get("cached_at") if envelope else None,
            "last_refresh_status": last_status,
            "failure_category": self._entry_category(
                file_state, freshness, last, scope),
            "local_history_available": scope in ctx["local"],
            "summary_only": scope in ctx["summary_only"],
        }

    @staticmethod
    def _entry_category(file_state: str, freshness: str,
                        last: dict[str, Any] | None, scope: str) -> str | None:
        if file_state in ("invalid_envelope", "invalid_json"):
            return "invalid_envelope"
        if freshness == "future_timestamp":
            return "future_timestamp"
        if last and last.get("status") == "failed":
            return last.get("failure_category") or "unknown"
        if last and last.get("status") == "skipped":
            return "unsupported"
        if freshness == "stale":
            return "stale"
        return None

    # ------------------------------------------------------------------ #
    # 一次性只读扫描上下文
    # ------------------------------------------------------------------ #
    def _scan(self) -> dict[str, Any]:
        requests = list(self._iter_requests())
        receipts, receipt_files = self._iter_receipts()
        requests_by_id = {r["request_id"]: r for r in requests}
        return {
            "requests": requests,
            "requests_by_id": requests_by_id,
            "receipts": receipts,
            "receipt_files": receipt_files,
            "receipt_status_by_request": {
                rid: self._receipt_status_of(r, receipts, receipt_files)
                for rid, r in requests_by_id.items()
            },
            # orphan：合法 receipt 但无对应合法 request；非法：文件存在但未通过校验
            "orphan_receipt_count": sum(1 for rid in receipts if rid not in requests_by_id),
            "invalid_receipt_file_count": sum(1 for rid in receipt_files if rid not in receipts),
            "refresh_map": self._refresh_lookup(requests),
            "local": self._local_symbols(),
            "summary_only": self._summary_only_symbols(requests),
            "cells": self._coverage_cells(requests),
            "inventory_state": self._inventory_state(),
        }

    # ------------------------------------------------------------------ #
    # coverage / inventory 两套口径
    # ------------------------------------------------------------------ #
    def _coverage_entries(self, ctx: dict[str, Any]) -> list[dict[str, Any]]:
        """只含预期矩阵 cell（缺失 → unavailable 记录），恒等式严格成立。"""
        cells = ctx["cells"]
        inventory_state = ctx["inventory_state"]
        return [
            self._evaluate(cap, scope, inventory_state.get((cap, scope), "missing"),
                           ctx["refresh_map"], ctx, in_matrix=True)
            for cap, scope in sorted(cells)
        ]

    def _inventory_entries(self, ctx: dict[str, Any]) -> list[dict[str, Any]]:
        """只含物理文件（含未知 capability/root JSON/非法 scope，均不公开真实名）。"""
        cells = ctx["cells"]
        entries = [
            self._evaluate(cap, scope, state, ctx["refresh_map"], ctx,
                           in_matrix=(cap, scope) in cells)
            for (cap, scope), state in sorted(ctx["inventory_state"].items())
        ]
        # unexpected 项分配稳定序号（u1, u2, ...），供前端 key 与定位，不泄露文件名
        seq = 0
        for e in entries:
            if not e["in_expected_matrix"]:
                seq += 1
                e["scope_id"] = f"u{seq}"
            else:
                e["scope_id"] = None
        return entries

    def _unexpected_entries(self, ctx: dict[str, Any]) -> list[dict[str, Any]]:
        inventory = self._inventory_entries(ctx)
        return [e for e in inventory if not e["in_expected_matrix"]]

    # ------------------------------------------------------------------ #
    # 指标
    # ------------------------------------------------------------------ #
    def summary(self) -> dict[str, Any]:
        ctx = self._scan()
        coverage = self._coverage_entries(ctx)
        inventory = self._inventory_entries(ctx)
        expected = len(ctx["cells"])
        physical = len(inventory)
        unexpected = sum(1 for e in inventory if not e["in_expected_matrix"])
        invalid_physical = sum(1 for e in inventory
                               if e["file_state"] in _INVALID_PHYSICAL_STATES)
        available = sum(1 for e in coverage if e["availability"] == "available")
        fresh = sum(1 for e in coverage if e["freshness_status"] == "fresh")
        stale = sum(1 for e in coverage if e["freshness_status"] == "stale")
        future_ts = sum(1 for e in coverage if e["freshness_status"] == "future_timestamp")
        invalid_ts = sum(1 for e in coverage if e["freshness_status"] == "invalid_timestamp")
        unavailable = sum(1 for e in coverage if e["freshness_status"] == "unavailable")
        usable = sum(1 for e in coverage if e["consumer_status"] == "usable")
        unusable = sum(1 for e in coverage if e["consumer_status"] == "unusable")
        not_validated = sum(1 for e in coverage if e["consumer_status"] == "not_validated")
        usable_fresh_stale = sum(1 for e in coverage
                                 if e["consumer_status"] == "usable"
                                 and e["freshness_status"] in ("fresh", "stale"))
        hash_mismatch = sum(1 for e in coverage
                            if e["integrity"]["hash_status"] == "mismatch")
        hash_unverified = sum(1 for e in coverage
                              if e["integrity"]["hash_status"] == "unverified")
        pending_evidence = sum(1 for e in coverage
                               if e["integrity"]["hash_status"] == "pending_evidence")
        caps = self._capability_rows(ctx)
        syms = self._symbol_rows(ctx)
        reqs = self.request_aggregate(ctx=ctx)
        failures = self.failures(ctx=ctx)
        ttl = self._ttl_expiring(coverage)
        lag = self._as_of_lag(coverage)
        return {
            "physical_cache_count": physical,
            "expected_cell_count": expected,
            "total_cells": len(coverage),
            "unexpected_physical_count": unexpected,
            "invalid_physical_count": invalid_physical,
            "availability": {"available": available, "unavailable": expected - available},
            "freshness": {"fresh": fresh, "stale": stale,
                          "future_timestamp": future_ts, "invalid_timestamp": invalid_ts,
                          "unavailable": unavailable},
            "consumer_status": {"usable": usable, "unusable": unusable,
                                "not_validated": not_validated},
            "integrity": {"hash_mismatch": hash_mismatch, "hash_unverified": hash_unverified,
                          "pending_evidence": pending_evidence},
            "usable_fresh_stale": usable_fresh_stale,
            "valid_coverage": _safe_div(usable_fresh_stale, expected),
            "capabilities": caps,
            "symbols": syms,
            "requests": reqs,
            "failures": failures,
            "ttl_expiring": ttl,
            "as_of_lag": lag,
        }

    def _ttl_expiring(self, entries: list[dict[str, Any]]) -> dict[str, int]:
        now = _utc_now()
        out = {"within_5min": 0, "within_1h": 0, "expired": 0}
        for e in entries:
            fetched = _parse_iso_ts(e.get("fetched_at"))
            if fetched is None or e["freshness_status"] == "future_timestamp":
                continue
            expires = fetched + timedelta(seconds=e["ttl_seconds"])
            delta = (expires - now).total_seconds()
            if delta <= 0:
                out["expired"] += 1
            elif delta <= 300:
                out["within_5min"] += 1
            elif delta <= 3600:
                out["within_1h"] += 1
        return out

    def _as_of_lag(self, entries: list[dict[str, Any]]) -> dict[str, Any]:
        """每 capability 取最新合法 as_of；非法/缺失日期计 unknown；上海 current_date。"""
        best: dict[str, datetime.date] = {}
        for e in entries:
            day = _parse_strict_date(e.get("as_of"))
            if day is None:
                continue
            cap = e["capability"]
            if cap not in best or day > best[cap]:
                best[cap] = day
        today = _shanghai_today()
        per_cap: dict[str, Any] = {}
        unknown = 0
        for cap in sorted({e["capability"] for e in entries}):
            day = best.get(cap)
            if day is None:
                per_cap[cap] = {"as_of": None, "lag_days": None}
                unknown += 1
            else:
                per_cap[cap] = {"as_of": day.isoformat(),
                                "lag_days": max(0, (today - day).days)}
        return {
            "current_date": today.isoformat(),
            "unknown_count": unknown,
            "per_capability": per_cap,
        }

    # ------------------------------------------------------------------ #
    # 端点数据
    # ------------------------------------------------------------------ #
    def caches(self, filters: dict[str, str]) -> dict[str, Any]:
        """缓存质量：覆盖矩阵 + 意外物理文件；过滤后 total 用于分页。"""
        params = self._validate_filters(filters)
        ctx = self._scan()
        coverage = self._coverage_entries(ctx)
        unexpected = self._unexpected_entries(ctx)
        merged = coverage + unexpected
        merged = self._apply_filters(merged, params)
        total = len(merged)
        start = params["offset"]
        end = start + params["limit"]
        return {
            "total": total,  # 过滤后 merged 总数（翻页依据）
            "coverage_total": len(coverage),
            "inventory_total": len(self._inventory_entries(ctx)),
            "unexpected_physical_count": len(unexpected),
            "limit": params["limit"],
            "offset": params["offset"],
            "items": merged[start:end],
        }

    def capabilities(self, filters: dict[str, str]) -> dict[str, Any]:
        params = self._validate_filters(filters)
        rows = self._capability_rows(self._scan())
        if params.get("capability"):
            rows = [r for r in rows if r["capability"] == params["capability"]]
        total = len(rows)
        start = params["offset"]
        end = start + params["limit"]
        return {"total": total, "limit": params["limit"], "offset": params["offset"],
                "items": rows[start:end]}

    def _capability_rows(self, ctx: dict[str, Any]) -> list[dict[str, Any]]:
        """内部全量聚合（无分页）；success_rate 基于全部终态 job 事件。"""
        entries = self._coverage_entries(ctx)
        by_cap: dict[str, list[dict[str, Any]]] = {}
        for e in entries:
            by_cap.setdefault(e["capability"], []).append(e)
        refresh_map = ctx["refresh_map"]
        rows = []
        for cap in sorted(by_cap):
            r = by_cap[cap]
            definition = CAPABILITY_MAP[cap]
            usable = sum(1 for x in r if x["consumer_status"] == "usable")
            stale = sum(1 for x in r if x["freshness_status"] == "stale")
            unavail = sum(1 for x in r if x["freshness_status"] == "unavailable")
            success_count = sum(e["success_count"] for key, e in refresh_map.items()
                                if key[0] == cap)
            attempt_count = sum(e["attempt_count"] for key, e in refresh_map.items()
                                if key[0] == cap)
            rows.append({
                "capability": cap,
                "name": cap,
                "group": definition.group,
                "read_only": definition.read_only,
                "ttl_seconds": definition.ttl_seconds,
                "scope_count": len(r),
                "usable": usable,
                "stale": stale,
                "unavailable": unavail,
                "latest_ok_at": self._latest_job_time(cap, "ok"),
                "latest_fail_at": self._latest_job_time(cap, "failed"),
                "success_rate": _safe_div(success_count, attempt_count) if attempt_count else None,
            })
        return rows

    def _latest_job_time(self, capability: str, status: str) -> str | None:
        best: str | None = None
        best_key = -1e18
        for req in self._iter_requests():
            for job in req.get("jobs") or []:
                if job.get("capability") != capability or job.get("status") != status:
                    continue
                stamp = job.get("recorded_at") or req.get("finished_at")
                if stamp:
                    key = _ts_sort_key(stamp)
                    if key > best_key:
                        best, best_key = stamp, key
        return best

    def symbols(self, filters: dict[str, str]) -> dict[str, Any]:
        params = self._validate_filters(filters)
        rows = self._symbol_rows(self._scan())
        if params.get("symbol"):
            rows = [r for r in rows if r["symbol"] == params["symbol"]]
        total = len(rows)
        start = params["offset"]
        end = start + params["limit"]
        return {"total": total, "limit": params["limit"], "offset": params["offset"],
                "items": rows[start:end]}

    def _symbol_rows(self, ctx: dict[str, Any]) -> list[dict[str, Any]]:
        """内部全量聚合（无分页）：51+ 股票仍完整计数。"""
        entries = self._coverage_entries(ctx)
        by_sym: dict[str, list[dict[str, Any]]] = {}
        for e in entries:
            if e["scope_type"] == "stock":
                by_sym.setdefault(e["scope"], []).append(e)
        local = ctx["local"]
        rows = []
        for sym in sorted(by_sym):
            r = by_sym[sym]
            expected = (_STOCK_CAPS if sym in local
                        else set(_STOCK_CAPS) - _SUMMARY_BLOCKED_CAPS)
            usable = sum(1 for x in r if x["consumer_status"] == "usable")
            stale = sum(1 for x in r if x["freshness_status"] == "stale")
            unavail = sum(1 for x in r if x["freshness_status"] == "unavailable")
            rows.append({
                "symbol": sym,
                "local_history_available": sym in local,
                "expected_count": len(expected),
                "usable": usable,
                "stale": stale,
                "unavailable": unavail,
            })
        return rows

    def requests(self, filters: dict[str, str]) -> dict[str, Any]:
        """刷新历史：created_at 按解析绝对时间倒序稳定排序后再分页。"""
        params = self._validate_filters(filters)
        ctx = self._scan()
        rows = []
        for req in ctx["requests"]:
            status = req.get("status")
            if params.get("request_status") and status != params["request_status"]:
                continue
            rows.append(self._request_row(req, ctx))
        rows.sort(key=lambda r: (_ts_sort_key(r["created_at"]), r["request_id"]),
                  reverse=True)
        total = len(rows)
        start = params["offset"]
        end = start + params["limit"]
        return {"total": total, "limit": params["limit"], "offset": params["offset"],
                "items": rows[start:end]}

    @staticmethod
    def _status_detail_code(req: dict[str, Any]) -> str:
        """status_detail 固定受控摘要（绝不回传原文本）。"""
        status = req.get("status")
        sd = (req.get("status_detail") or "").lower()
        if "worker_timeout" in sd:
            return "worker_timeout"
        if "receipt" in sd and ("fail" in sd or "写" in sd):
            return "receipt_failed"
        if status == "completed":
            return "completed_all"
        if status == "partial":
            return "partial_success"
        if status == "failed":
            return "failed_none"
        return status or "unknown"

    def _request_row(self, req: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
        rid = req["request_id"]
        jobs = req.get("jobs") or []
        created = _parse_iso_ts(req.get("created_at"))
        started = _parse_iso_ts(req.get("started_at"))
        finished = _parse_iso_ts(req.get("finished_at"))
        duration = None
        if started and finished:
            duration = max(0, int((finished - started).total_seconds()))
        elif created and finished:
            duration = max(0, int((finished - created).total_seconds()))
        tgt = req.get("target") or {}
        # warnings/status_detail 脱敏：只返回计数与固定分类，绝不回传原文本
        warnings = req.get("warnings") or []
        warn_cats: dict[str, int] = {}
        for w in warnings[-10:]:
            c = _category_from_warning(w)
            warn_cats[c] = warn_cats.get(c, 0) + 1
        return {
            "request_id": rid,
            "short_id": rid[:8],
            "status": req.get("status"),
            "receipt_status": (ctx.get("receipt_status_by_request") or {}).get(
                rid, "missing"),
            "target": tgt.get("kind"),
            "preset": tgt.get("preset") or tgt.get("result_id") or None,
            "symbols": tgt.get("symbols") or (
                _short_scope(tgt["cache_scope"])
                if isinstance(tgt.get("cache_scope"), str)
                and Q_SCOPE_RE.fullmatch(tgt["cache_scope"])
                else None
            ),
            "job_counts": _job_counts(jobs),
            "created_at": req.get("created_at"),
            "started_at": req.get("started_at"),
            "finished_at": req.get("finished_at"),
            "duration_seconds": duration,
            "warning_count": len(warnings),
            "warning_categories": dict(sorted(warn_cats.items())),
            "status_detail_code": self._status_detail_code(req),
        }

    def request_aggregate(self, ctx: dict[str, Any] | None = None) -> dict[str, Any]:
        """扫描全部合法请求（不受 limit 限制）；含 receipt_status 汇总。"""
        ctx = ctx or self._scan()
        rows = [self._request_row(req, ctx) for req in ctx["requests"]]
        rows.sort(key=lambda r: (_ts_sort_key(r["created_at"]), r["request_id"]),
                  reverse=True)
        status_counts = {s: 0 for s in REQUEST_STATUSES}
        receipt_counts = {s: 0 for s in RECEIPT_STATUSES}
        job_counts = {"ok": 0, "partial": 0, "failed": 0, "skipped": 0, "pending": 0}
        durations = [r["duration_seconds"] for r in rows if r["duration_seconds"] is not None]
        for r in rows:
            if r["status"] in status_counts:
                status_counts[r["status"]] += 1
            receipt_counts[r["receipt_status"]] = receipt_counts.get(
                r["receipt_status"], 0) + 1
            for k in job_counts:
                job_counts[k] += r["job_counts"].get(k, 0)
        return {
            "total": len(rows),
            "status_counts": status_counts,
            "receipt_status_counts": receipt_counts,
            "job_counts": job_counts,
            "avg_duration_seconds": _safe_div(sum(durations), len(durations)),
            "recent_20": rows[:20],
        }

    def failures(self, ctx: dict[str, Any] | None = None) -> dict[str, Any]:
        """失败统计：job/request 分离；仅 receipt-required 终态进入回执审计；
        orphan/非法回执文件只公开计数。"""
        ctx = ctx or self._scan()
        job_categories: dict[str, int] = {}
        request_categories: dict[str, int] = {}
        receipt_issues = {"missing": 0, "invalid": 0, "mismatch": 0}
        failed_job_count = 0
        failed_request_count = 0
        for req in ctx["requests"]:
            status = req.get("status")
            req_cat = None
            if status == "cancelled":
                req_cat = "cancelled"
            elif status == "expired":
                req_cat = "expired"
            elif req.get("status_detail") == "worker_timeout":
                req_cat = "worker_timeout"
            elif req.get("status_detail") == "receipt_failed":
                req_cat = "receipt_failed"
            elif status == "failed":
                req_cat = "failed"
            if req_cat:
                failed_request_count += 1
                request_categories[req_cat] = request_categories.get(req_cat, 0) + 1
            # 仅 receipt-required 状态（completed/partial/failed）进入回执审计；
            # cancelled/expired 不产生回执，不计 missing
            if status in _RECEIPT_REQUIRED_STATUSES:
                rstatus = (ctx.get("receipt_status_by_request") or {}).get(
                    req["request_id"])
                if rstatus in ("missing", "invalid", "mismatch"):
                    receipt_issues[rstatus] = receipt_issues.get(rstatus, 0) + 1
            for job in req.get("jobs") or []:
                if job.get("status") == "failed":
                    failed_job_count += 1
                    cat = _category_from_warning(job.get("warning") or "")
                    job_categories[cat] = job_categories.get(cat, 0) + 1
                elif job.get("status") == "skipped":
                    job_categories["unsupported"] = job_categories.get("unsupported", 0) + 1
        for cat in FAILURE_CATEGORIES:
            job_categories.setdefault(cat, 0)
            request_categories.setdefault(cat, 0)
        return {
            "job_failure_categories": dict(sorted(job_categories.items())),
            "request_failure_categories": dict(sorted(request_categories.items())),
            "failed_job_count": failed_job_count,
            "failed_request_count": failed_request_count,
            "receipt_audit_issues": dict(sorted(receipt_issues.items())),
            "receipt_audit_issue_count": sum(receipt_issues.values()),
            "orphan_receipt_count": ctx.get("orphan_receipt_count", 0),
            "invalid_receipt_file_count": ctx.get("invalid_receipt_file_count", 0),
        }

    # ------------------------------------------------------------------ #
    # 过滤与校验
    # ------------------------------------------------------------------ #
    def _validate_filters(self, filters: dict[str, str]) -> dict[str, Any]:
        unknown = set(filters) - set(FILTER_KEYS)
        if unknown:
            raise RefreshError("invalid_filter", f"未知筛选参数: {sorted(unknown)[0]}", 400)
        out: dict[str, Any] = {"limit": 50, "offset": 0}
        cap = filters.get("capability")
        if cap is not None:
            if cap not in CAPABILITY_MAP:
                raise RefreshError("invalid_filter", "capability 必须来自注册表", 400)
            out["capability"] = cap
        sym = filters.get("symbol")
        if sym is not None:
            if not SYMBOL_RE.fullmatch(sym):
                raise RefreshError("invalid_filter", "symbol 必须严格匹配 600519.SH", 400)
            out["symbol"] = sym
        scope_type = filters.get("scope_type")
        if scope_type is not None and scope_type not in SCOPE_TYPES:
            raise RefreshError("invalid_filter", "scope_type 必须为 stock/global/market/query/invalid", 400)
        out["scope_type"] = scope_type
        freshness = filters.get("freshness")
        if freshness is not None and freshness not in FRESHNESSES:
            raise RefreshError("invalid_filter", "freshness 参数不合法", 400)
        out["freshness"] = freshness
        cs = filters.get("consumer_status")
        if cs is not None and cs not in CONSUMER_STATUSES:
            raise RefreshError("invalid_filter", "consumer_status 参数不合法", 400)
        out["consumer_status"] = cs
        rs = filters.get("request_status")
        if rs is not None and rs not in REQUEST_STATUSES:
            raise RefreshError("invalid_filter", "request_status 参数不合法", 400)
        out["request_status"] = rs
        fc = filters.get("failure_category")
        if fc is not None and fc not in FAILURE_CATEGORIES:
            raise RefreshError("invalid_filter", "failure_category 参数不合法", 400)
        out["failure_category"] = fc
        limit_raw = filters.get("limit")
        if limit_raw is not None:
            try:
                limit = int(limit_raw)
            except (TypeError, ValueError):
                raise RefreshError("invalid_filter", "limit 必须是整数", 400)
            if not (1 <= limit <= 200):
                raise RefreshError("invalid_filter", "limit 必须在 1–200", 400)
            out["limit"] = limit
        offset_raw = filters.get("offset")
        if offset_raw is not None:
            try:
                offset = int(offset_raw)
            except (TypeError, ValueError):
                raise RefreshError("invalid_filter", "offset 必须是非负整数", 400)
            if offset < 0:
                raise RefreshError("invalid_filter", "offset 必须 ≥0", 400)
            out["offset"] = offset
        return out

    def _apply_filters(self, entries: list[dict[str, Any]],
                       params: dict[str, Any]) -> list[dict[str, Any]]:
        out = entries
        if params.get("capability"):
            out = [e for e in out if e["capability"] == params["capability"]]
        if params.get("symbol"):
            out = [e for e in out if e["scope"] == params["symbol"]]
        if params.get("scope_type"):
            out = [e for e in out if e["scope_type"] == params["scope_type"]]
        if params.get("freshness"):
            out = [e for e in out if e["freshness_status"] == params["freshness"]]
        if params.get("consumer_status"):
            out = [e for e in out if e["consumer_status"] == params["consumer_status"]]
        if params.get("failure_category"):
            out = [e for e in out if e["failure_category"] == params["failure_category"]]
        return out


def build_operations_service(project_root: Path, cache: WestockCacheStore,
                             refresh: Any) -> WestockOperationsService:
    root = Path(project_root)
    return WestockOperationsService(cache, refresh, root / "data" / "curated")


def operations_envelope(data: dict[str, Any], warnings: list[str] | None = None) -> dict[str, Any]:
    """固定响应 envelope：不返回内部文件名/绝对路径/哈希。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE,
        "as_of": _utc_now().date().isoformat(),
        "generated_at": _utc_now().isoformat(),
        "availability": "ok",
        "data": data,
        "warnings": warnings or [],
    }
