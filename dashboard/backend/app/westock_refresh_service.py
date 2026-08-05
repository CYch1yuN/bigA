"""Phase F3 刷新服务（第二轮严格模型）：可审计、可追踪、受控的 Westock 缓存刷新请求队列。

架构边界（诚实语义）：
- Dashboard 进程只创建 / 查询 / 取消刷新请求，绝不直接调用 MCP；
- WorkBuddy worker（scripts/westock_refresh_request.py）claim 后调用 MCP，
  经受控导出元数据写入缓存（export_job），再 complete_job / finish；
- 请求按 jobs 组织（每项 capability/scope/status，不保存 tool）；
- 状态机：pending → processing → completed | partial | failed；
          pending → cancelled（仅 pending）；pending → expired（24h）；
          processing → failed（2h worker_timeout，status_detail=worker_timeout）；
- 所有权：API 仅能看见/操作当前 session 创建的请求（session_fingerprint 校验，非所有者 404）；
          worker 接口使用内部读取，不走 session 公共视图；
- 去重先于限流；相同未完成请求不消耗新配额；
- 原始 session ID 不落盘；所有写原子（同目录唯一 tmp + flush + fsync + replace）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .westock_bridge import CAPABILITY_MAP
from .stocks_service import SYMBOL_RE
from .screener_service import ScreenerStore, _MODE_CAPABILITY, canonical_query_hash

SCHEMA_VERSION = 2
MAX_REQUEST_BYTES = 256 * 1024    # request ≤256 KiB
MAX_RECEIPT_BYTES = 512 * 1024    # receipt ≤512 KiB
MAX_INDEX_BYTES = 2 * 1024 * 1024  # index ≤2 MiB
REQUEST_ID_RE = re.compile(r"[0-9a-f]{32}")
Q_SCOPE_RE = re.compile(r"q_[0-9a-f]{64}")
MAX_SYMBOLS = 20
PENDING_TTL = timedelta(hours=24)
WORKER_TIMEOUT = timedelta(hours=2)
PRUNE_MAX_AGE_DAYS = 30

# summary-only 硬边界：依赖本地历史/复权的任务一律禁止
_SUMMARY_BLOCKED_CAPS = frozenset(("minute", "technical"))

# 限流（新请求才消耗配额；去重不消耗）
GLOBAL_ACTIVE_MAX = 10
SESSION_ACTIVE_MAX = 2
SESSION_PER_MINUTE_MAX = 5

STATUSES = ("pending", "processing", "completed", "partial", "failed", "cancelled", "expired")
JOB_STATUSES = ("pending", "processing", "ok", "partial", "failed", "skipped")
EXPORT_RESULTS = ("ok", "partial", "failed")

# target 枚举
TARGET_STOCK = "stock"
TARGET_MARKET = "market"
TARGET_SCREENER = "screener"
TARGETS = (TARGET_STOCK, TARGET_MARKET, TARGET_SCREENER)

# 个股研究能力全集（full_research）
_STOCK_CAPS = (
    "quote", "minute", "technical", "profile", "financials", "forecast",
    "shareholders", "dividend", "buyback", "margin", "block_trade",
    "fund_flow", "northbound", "news", "reports", "announcements",
    "events", "risk", "lhb", "chip_distribution",
)
_STOCK_PRESETS: dict[str, tuple[str, ...]] = {
    "quote_only": ("quote",),
    "basic": ("quote", "profile", "news", "fund_flow"),
    "market_data": ("quote", "minute", "technical"),
    "fundamentals": ("profile", "financials", "forecast"),
    "ownership": ("shareholders", "dividend", "buyback"),
    "funds": ("margin", "block_trade", "fund_flow", "northbound", "lhb", "chip_distribution"),
    "intel": ("news", "reports", "announcements", "events", "risk"),
    "full_research": _STOCK_CAPS,
}
_MARKET_PRESETS: dict[str, tuple[str, ...]] = {
    "overview": ("market_overview", "change_distribution", "hot_ranking"),
    "structure": ("sector", "index", "industry_chain"),
    "macro": ("macro",),
    # 市场资金：无独立市场资金流能力，使用概览（含资金维度）
    "funds": ("market_overview",),
    "full_market": ("market_overview", "change_distribution", "hot_ranking",
                    "sector", "index", "industry_chain", "macro"),
}

# 递归禁止字段（任意层级；大小写不敏感）
FORBIDDEN_KEYS = (
    "tool", "mcp_tool", "token", "credential", "secret", "path", "filename",
    "command", "script", "code", "expression", "formula", "raw_params",
    "mcp_params", "password", "cookie", "authorization",
)

# 请求体顶层键白名单（API POST / 兼容 refresh）
REQUEST_BODY_KEYS = ("target", "preset", "capabilities", "symbols", "symbol",
                     "result_id", "cache_scope", "allow_summary_only")

# 请求文件顶层键白名单
_REQUEST_FILE_KEYS = (
    "schema_version", "request_id", "created_at", "updated_at", "status",
    "target", "jobs", "request_hash", "session_fingerprint", "attempts",
    "worker_id", "claimed_at", "started_at", "finished_at", "expires_at",
    "warnings", "status_detail",
)
# job 顶层键白名单
_JOB_KEYS = ("job_id", "capability", "scope", "status", "summary_only",
             "fetched_at", "cache_status", "data_as_of", "content_hash",
             "recorded_at", "warning")
# receipt 顶层键白名单
_RECEIPT_KEYS = ("schema_version", "request_id", "target", "jobs", "created_at",
                 "started_at", "finished_at", "status", "status_detail", "warnings")
# index 顶层键白名单
_INDEX_KEYS = ("schema_version", "updated_at", "requests")
_INDEX_ENTRY_KEYS = ("status", "created_at", "request_hash", "session_fingerprint")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse_aware_iso(value: Any) -> datetime | None:
    """严格 aware ISO 时间：必须带时区；非法/naive → None。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def _stock_consumer_validator(capability: str, scope: str):
    """Return a semantic validator for calibrated stock cache capabilities.

    ``None`` means the capability is not a per-stock F2 adapter and keeps the
    existing envelope-only behavior (market/query capabilities).  The
    validator consumes the exact candidate envelope before it can replace the
    live cache.
    """
    if not SYMBOL_RE.fullmatch(scope):
        return None

    def validate(envelope: dict[str, Any]) -> bool:
        payload = envelope.get("data")
        warnings: list[str] = []
        from .stocks_service import normalize_minute, normalize_quote
        from .stocks_deep_service import (
            _BUYBACK_FIELDS, _FUND_FLOW_FIELDS, _PROFILE_FIELDS,
            _fund_flow_identity_conflict, _norm_announcements,
            _norm_block_trade, _norm_chip, _norm_dividend, _norm_events,
            _norm_financials, _norm_forecast, _norm_mapping, _norm_margin,
            _norm_news_identity_items, _norm_northbound, _norm_reports,
            _norm_risk, _norm_shareholders, _norm_technical,
            _profile_identity_conflict, _unwrap_fund_flow,
        )

        if capability == "quote":
            return normalize_quote(payload, scope) is not None
        if capability == "minute":
            normalized, _ = normalize_minute(payload, scope, warnings)
            if normalized is not None:
                envelope["as_of"] = normalized.get("date")
            return normalized is not None
        if capability == "technical":
            normalized, _ = _norm_technical(payload, scope, warnings)
            if normalized is not None:
                envelope["as_of"] = normalized.get("date")
            return normalized is not None
        if capability == "profile":
            valid = (_profile_identity_conflict(payload, scope) is None
                     and _norm_mapping(payload, _PROFILE_FIELDS) is not None)
            if valid:
                envelope["as_of"] = None
            return valid
        if capability == "financials":
            normalized, _ = _norm_financials(payload, scope, warnings)
            if normalized is not None and normalized.get("periods"):
                envelope["as_of"] = normalized["periods"][0].get("report_date")
            return normalized is not None
        if capability == "forecast":
            normalized, _ = _norm_forecast(payload, scope, warnings)
            if normalized is not None:
                envelope["as_of"] = None
            return normalized is not None
        if capability == "shareholders":
            normalized, _ = _norm_shareholders(payload, scope, warnings)
            return normalized is not None
        if capability == "dividend":
            normalized, _ = _norm_dividend(payload, scope, warnings)
            return normalized is not None
        if capability == "buyback":
            return _norm_mapping(payload, _BUYBACK_FIELDS) is not None
        if capability == "margin":
            normalized, _ = _norm_margin(payload, scope, warnings)
            return normalized is not None
        if capability == "block_trade":
            normalized, _ = _norm_block_trade(payload, scope, warnings)
            return normalized is not None
        if capability == "fund_flow":
            return (_fund_flow_identity_conflict(payload, scope) is None
                    and _norm_mapping(_unwrap_fund_flow(payload), _FUND_FLOW_FIELDS) is not None)
        if capability == "northbound":
            normalized, _ = _norm_northbound(payload, scope, warnings)
            if normalized is not None and normalized.get("current"):
                envelope["as_of"] = normalized["current"].get("date")
            return normalized is not None
        if capability == "news":
            normalized, _ = _norm_news_identity_items(payload, warnings, scope)
            return normalized is not None
        if capability == "reports":
            normalized, _ = _norm_reports(payload, scope, warnings)
            return normalized is not None
        if capability == "announcements":
            normalized, _ = _norm_announcements(payload, scope, warnings)
            return normalized is not None
        if capability == "events":
            normalized, _ = _norm_events(payload, scope, warnings)
            return normalized is not None
        if capability == "risk":
            normalized, _ = _norm_risk(payload, scope, warnings)
            return normalized is not None
        if capability == "chip_distribution":
            normalized, _ = _norm_chip(payload, scope, warnings)
            return normalized is not None
        return True

    calibrated = set(_STOCK_CAPS) - {"lhb"}
    return validate if capability in calibrated else None


def session_fingerprint(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def worker_fingerprint() -> str:
    raw = f"{os.uname().nodename if hasattr(os, 'uname') else 'worker'}:{os.getpid()}:{secrets.token_hex(8)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _has_forbidden_key(value: Any) -> bool:
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str) and k.lower() in FORBIDDEN_KEYS:
                return True
            if _has_forbidden_key(v):
                return True
    elif isinstance(value, list):
        for item in value:
            if _has_forbidden_key(item):
                return True
    return False


def _scope_kind(scope: str) -> str:
    if scope == "global":
        return "global"
    if Q_SCOPE_RE.fullmatch(scope):
        return "query"
    if SYMBOL_RE.fullmatch(scope):
        return "stock"
    return "invalid"


def _atomic_write(path: Path, obj: Any) -> None:
    """原子写：同目录唯一 tmp + flush + fsync + replace；失败清理 tmp。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{secrets.token_hex(6)}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, ensure_ascii=False, indent=2))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _load_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


class RefreshError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


# ---------------------------------------------------------------------- #
# 严格 schema 校验（请求 / job / receipt / index）
# ---------------------------------------------------------------------- #
def _valid_warnings(value: Any) -> bool:
    return isinstance(value, list) and all(
        isinstance(w, str) and 0 < len(w) <= 400 for w in value)


def _valid_strict_date(value: Any) -> bool:
    """null 或严格 YYYY-MM-DD。"""
    if value is None:
        return True
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _valid_export_info(value: Any) -> bool:
    """export_info（export_job 受控摘要）严格校验：顶层键精确白名单。"""
    if not isinstance(value, dict):
        return False
    if set(value) != {"fetched_at", "cache_status", "data_as_of", "content_hash"}:
        return False
    if _parse_aware_iso(value.get("fetched_at")) is None:
        return False
    if value.get("cache_status") not in ("fresh", "stale"):
        return False
    content_hash = value.get("content_hash")
    if not isinstance(content_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", content_hash):
        return False
    return _valid_strict_date(value.get("data_as_of"))


def _valid_target(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    kind = value.get("kind")
    if kind not in TARGETS:
        return False
    if kind == TARGET_STOCK:
        symbols = value.get("symbols")
        if not isinstance(symbols, list) or not (1 <= len(symbols) <= MAX_SYMBOLS):
            return False
        if not all(isinstance(s, str) and SYMBOL_RE.fullmatch(s) for s in symbols):
            return False
        if len(set(symbols)) != len(symbols):
            return False
        preset = value.get("preset")
        caps = value.get("capabilities")
        if (preset is None) == (caps is None):  # 二选一
            return False
        if preset is not None and preset not in _STOCK_PRESETS:
            return False
        if caps is not None:
            if not isinstance(caps, list) or not caps:
                return False
            if not all(isinstance(c, str) and c in _STOCK_CAPS for c in caps):
                return False
            if len(set(caps)) != len(caps):
                return False
        if value.get("allow_summary_only") not in (True, False):
            return False
        summary_only = value.get("summary_only_symbols")
        if summary_only is None:
            return False
        # summary_only_symbols 必须是 symbols 的无重复严格子集
        if not isinstance(summary_only, list):
            return False
        if not all(isinstance(s, str) and s in symbols for s in summary_only):
            return False
        if len(set(summary_only)) != len(summary_only):
            return False
        if set(value) - {"kind", "symbols", "preset", "capabilities",
                         "allow_summary_only", "summary_only_symbols"}:
            return False
        return True
    if kind == TARGET_MARKET:
        preset = value.get("preset")
        if preset not in _MARKET_PRESETS:
            return False
        return set(value) == {"kind", "preset"}
    # screener
    result_id = value.get("result_id")
    cache_scope = value.get("cache_scope")
    if not isinstance(result_id, str) or not REQUEST_ID_RE.fullmatch(result_id):
        return False
    if not isinstance(cache_scope, str) or not Q_SCOPE_RE.fullmatch(cache_scope):
        return False
    capability = value.get("capability")
    if capability not in CAPABILITY_MAP:
        return False
    return set(value) == {"kind", "result_id", "cache_scope", "capability"}


def _valid_job(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) - set(_JOB_KEYS):
        return False
    if not isinstance(value.get("job_id"), str) or not REQUEST_ID_RE.fullmatch(value["job_id"]):
        return False
    capability = value.get("capability")
    if capability not in CAPABILITY_MAP:
        return False
    scope = value.get("scope")
    if not isinstance(scope, str) or _scope_kind(scope) == "invalid":
        return False
    status = value.get("status")
    if status not in JOB_STATUSES:
        return False
    if value.get("summary_only") not in (True, False, None):
        return False
    for key in ("fetched_at", "recorded_at"):
        if value.get(key) is not None and _parse_aware_iso(value.get(key)) is None:
            return False
    if not _valid_strict_date(value.get("data_as_of")):
        return False
    if value.get("cache_status") not in (None, "fresh", "stale", "unavailable"):
        return False
    content_hash = value.get("content_hash")
    if content_hash is not None and (not isinstance(content_hash, str)
                                     or not re.fullmatch(r"[0-9a-f]{64}", content_hash)):
        return False
    if value.get("warning") is not None and not (isinstance(value["warning"], str) and len(value["warning"]) <= 400):
        return False
    # 导出证据一致性：ok/partial job 必须有完整导出证据
    if status in ("ok", "partial"):
        if value.get("fetched_at") is None or value.get("cache_status") is None:
            return False
        if value.get("content_hash") is None:
            return False
    return True


def _valid_request_file(value: Any) -> bool:
    """请求文件严格 schema（含跨字段交叉校验）：任意额外字段或篡改 → fail-open。"""
    if not isinstance(value, dict) or set(value) != set(_REQUEST_FILE_KEYS):
        return False
    if value.get("schema_version") != SCHEMA_VERSION:
        return False
    request_id = value.get("request_id")
    if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
        return False
    status = value.get("status")
    if status not in STATUSES:
        return False
    if _parse_aware_iso(value.get("created_at")) is None:
        return False
    if _parse_aware_iso(value.get("updated_at")) is None:
        return False
    if value.get("expires_at") is not None and _parse_aware_iso(value.get("expires_at")) is None:
        return False
    for key in ("claimed_at", "started_at", "finished_at"):
        if value.get(key) is not None and _parse_aware_iso(value.get(key)) is None:
            return False
    target = value.get("target")
    if not _valid_target(target):
        return False
    jobs = value.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        return False
    if not all(_valid_job(j) for j in jobs):
        return False
    if len({j["job_id"] for j in jobs}) != len(jobs):
        return False
    request_hash = value.get("request_hash")
    if not isinstance(request_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", request_hash):
        return False
    # request_hash 必须等于 canonical target hash（交叉校验）
    if request_hash != canonical_request_hash(target):
        return False
    session_fp = value.get("session_fingerprint")
    if not isinstance(session_fp, str) or not re.fullmatch(r"[0-9a-f]{64}", session_fp):
        return False
    worker_id = value.get("worker_id")
    if worker_id is not None and (not isinstance(worker_id, str)
                                  or not re.fullmatch(r"[0-9a-f]{64}", worker_id)):
        return False
    if not isinstance(value.get("attempts"), int) or value["attempts"] < 0:
        return False
    for key in ("status_detail",):
        if value.get(key) is not None and not isinstance(value.get(key), str):
            return False
    if not _valid_warnings(value.get("warnings")):
        return False
    # 状态字段一致性
    if status == "pending":
        if value.get("claimed_at") is not None or value.get("worker_id") is not None:
            return False
    elif status == "processing":
        if value.get("claimed_at") is None or value.get("started_at") is None:
            return False
    elif status in ("completed", "partial", "failed", "cancelled", "expired"):
        if value.get("finished_at") is None:
            return False
    # jobs 与 target 交叉一致性
    if not _jobs_match_target(target, jobs):
        return False
    return True


def _jobs_match_target(target: dict[str, Any], jobs: list[dict[str, Any]]) -> bool:
    """jobs 与 target 交叉一致性（任意篡改 fail-open）。

    - stock: job 组合必须精确等于 symbols×capabilities（summary_only 过滤后），LHB 仅 global 且 ≤1
    - market: jobs 必须精确等于 preset caps × global
    - screener: 单 job 且 capability/scope 与 target 完全一致
    """
    kind = target.get("kind")
    if kind == TARGET_STOCK:
        caps = set(target.get("capabilities") or _STOCK_PRESETS[target["preset"]])
        summary_only = set(target.get("summary_only_symbols") or [])
        expected_pairs: set[tuple[str, str]] = set()
        symbol_summary: dict[str, bool] = {}
        for symbol in target["symbols"]:
            eff = caps - (_SUMMARY_BLOCKED_CAPS if symbol in summary_only else set())
            symbol_summary[symbol] = symbol in summary_only
            for cap in eff:
                expected_pairs.add((cap, "global" if cap == "lhb" else symbol))
        job_pairs = {(j["capability"], j["scope"]) for j in jobs}
        if job_pairs != expected_pairs:
            return False
        if len(jobs) != len(expected_pairs):  # 多余/重复 job 拒绝
            return False
        lhb_count = sum(1 for j in jobs if j["capability"] == "lhb")
        if lhb_count > 1:
            return False
        for job in jobs:
            if job["capability"] == "lhb":
                if job["scope"] != "global" or job.get("summary_only", False) is not False:
                    return False
            else:
                symbol = job["scope"]
                if symbol not in symbol_summary:
                    return False
                if job.get("summary_only", False) is not symbol_summary[symbol]:
                    return False
        return True
    if kind == TARGET_MARKET:
        caps = _MARKET_PRESETS[target["preset"]]
        expected = {(c, "global") for c in caps}
        return {(j["capability"], j["scope"]) for j in jobs} == expected
    # screener
    if len(jobs) != 1:
        return False
    job = jobs[0]
    return (job["capability"] == target["capability"]
            and job["scope"] == target["cache_scope"])


def _valid_receipt(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != set(_RECEIPT_KEYS):
        return False
    if value.get("schema_version") != SCHEMA_VERSION:
        return False
    request_id = value.get("request_id")
    if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
        return False
    status = value.get("status")
    if status not in STATUSES:
        return False
    if status not in ("completed", "partial", "failed"):
        return False  # receipt 只属于聚合终态
    if _parse_aware_iso(value.get("created_at")) is None:
        return False
    if value.get("started_at") is not None and _parse_aware_iso(value.get("started_at")) is None:
        return False
    if _parse_aware_iso(value.get("finished_at")) is None:
        return False
    jobs = value.get("jobs")
    if not isinstance(jobs, list) or not jobs:
        return False
    if not all(_valid_job(j) for j in jobs):
        return False
    # 终态 receipt 的 job 不得存在 pending/processing
    if any(j.get("status") in ("pending", "processing") for j in jobs):
        return False
    if not _valid_target(value.get("target")):
        return False
    if not _valid_warnings(value.get("warnings")):
        return False
    return True


def _valid_index(value: Any) -> bool:
    if not isinstance(value, dict) or set(value) != set(_INDEX_KEYS):
        return False
    if value.get("schema_version") != SCHEMA_VERSION:
        return False
    if _parse_aware_iso(value.get("updated_at")) is None:
        return False
    requests = value.get("requests")
    if not isinstance(requests, dict):
        return False
    for rid, entry in requests.items():
        if not isinstance(rid, str) or not REQUEST_ID_RE.fullmatch(rid):
            return False
        if not isinstance(entry, dict) or set(entry) != set(_INDEX_ENTRY_KEYS):
            return False
        if entry.get("status") not in STATUSES:
            return False
        if _parse_aware_iso(entry.get("created_at")) is None:
            return False
        if not isinstance(entry.get("request_hash"), str) or len(entry["request_hash"]) != 64:
            return False
        if not isinstance(entry.get("session_fingerprint"), str) or len(entry["session_fingerprint"]) != 64:
            return False
    return True


# ---------------------------------------------------------------------- #
# 请求哈希（canonical）
# ---------------------------------------------------------------------- #
def canonical_request_hash(target: dict[str, Any]) -> str:
    canonical = json.dumps(target, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------- #
# 存储
# ---------------------------------------------------------------------- #
class RefreshStore:
    def __init__(self, root: Path, curated_dir: Path | None = None):
        self.root = Path(root)
        self.requests_dir = self.root / "requests"
        self.receipts_dir = self.root / "receipts"
        self.index_path = self.root / "index.json"
        self.curated_dir = Path(curated_dir) if curated_dir else None
        self._lock = threading.Lock()
        self.requests_dir.mkdir(parents=True, exist_ok=True)
        self.receipts_dir.mkdir(parents=True, exist_ok=True)

    # ---------------- 内部读取 ----------------
    def _request_path(self, request_id: str) -> Path:
        return self.requests_dir / f"{request_id}.json"

    def _receipt_path(self, request_id: str) -> Path:
        return self.receipts_dir / f"{request_id}.json"

    def _read_request_file(self, request_id: str) -> dict[str, Any] | None:
        if not REQUEST_ID_RE.fullmatch(request_id or ""):
            return None
        data = _load_json(self._request_path(request_id))
        if not _valid_request_file(data):
            return None  # 篡改/损坏 fail-open
        if data.get("request_id") != request_id:
            return None
        return data

    def local_symbols(self) -> set[str]:
        """从 curated 真实提取本地股票集合（daily_quotes_<SYMBOL>_*.parquet）。"""
        out: set[str] = set()
        if self.curated_dir is None or not self.curated_dir.is_dir():
            return out
        for path in self.curated_dir.glob("daily_quotes_*_*.parquet"):
            match = re.match(r"daily_quotes_([0-9]{6}\.(?:SH|SZ|BJ))_", path.name)
            if match:
                out.add(match.group(1))
        return out

    # ---------------- 创建 ----------------
    def create_request(self, *, body: dict[str, Any], session_id: str) -> dict[str, Any]:
        """严格创建（并发原子）：校验+canonicalize（锁外）→ 锁内去重/限流/生成/写入。"""
        target, kind = self._build_target(body)
        req_hash = canonical_request_hash(target)
        fingerprint = session_fingerprint(session_id)
        with self._lock:
            # 去重按 session 隔离：仅相同 session 的相同未完成请求可去重
            existing = self._find_incomplete(req_hash, fingerprint)
            if existing is not None:
                return {**self._public_request(existing), "deduplicated": True}
            # 新请求才限流
            self._rate_check(fingerprint)
            request_id = self._new_request_id()
            jobs, job_warnings = self._build_jobs(target, kind)
            if not jobs:
                raise RefreshError("empty_jobs", "请求不产生任何任务（能力全部被 summary 边界禁止）", 400)
            now = _utc_now()
            request: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "request_id": request_id,
                "created_at": _iso(now),
                "updated_at": _iso(now),
                "status": "pending",
                "target": target,
                "jobs": jobs,
                "request_hash": req_hash,
                "session_fingerprint": fingerprint,
                "attempts": 0,
                "worker_id": None,
                "claimed_at": None,
                "started_at": None,
                "finished_at": None,
                "expires_at": _iso(now + PENDING_TTL),
                "warnings": job_warnings,
                "status_detail": None,
            }
            if len(json.dumps(request, ensure_ascii=False).encode("utf-8")) > MAX_REQUEST_BYTES:
                raise RefreshError("request_too_large", "刷新请求超过大小限制", 400)
            _atomic_write(self._request_path(request_id), request)
            self._update_index(request_id, request)
        return self._public_request(request)

    def _build_target(self, body: dict[str, Any]) -> tuple[dict[str, Any], str]:
        """从请求体构造 target 对象（严格校验 + canonicalize）。"""
        extra = set(body) - set(REQUEST_BODY_KEYS)
        if extra:
            raise RefreshError("invalid_request", f"请求包含未知字段: {sorted(extra)[0]}", 400)
        kind = body.get("target")
        if kind not in TARGETS:
            raise RefreshError("invalid_target", "target 必须是 stock/market/screener", 400)
        if kind == TARGET_STOCK:
            symbols = body.get("symbols")
            if symbols is None:
                single = body.get("symbol")
                symbols = [single] if single is not None else None
            if not isinstance(symbols, list) or not (1 <= len(symbols) <= MAX_SYMBOLS):
                raise RefreshError("invalid_symbols", f"symbols 必须是 1–{MAX_SYMBOLS} 个严格 symbol", 400)
            if any(not isinstance(s, str) or not SYMBOL_RE.fullmatch(s) for s in symbols):
                raise RefreshError("invalid_symbols", "symbol 必须严格匹配 600519.SH 格式（不 strip/upper）", 400)
            symbols = list(dict.fromkeys(symbols))
            if len(symbols) > MAX_SYMBOLS:
                raise RefreshError("invalid_symbols", "symbols 超过 20 个", 400)
            preset = body.get("preset")
            caps = body.get("capabilities")
            if (preset is None) == (caps is None):
                raise RefreshError("invalid_request", "preset 与 capabilities 必须二选一", 400)
            target: dict[str, Any] = {"kind": "stock", "symbols": symbols}
            if preset is not None:
                if preset not in _STOCK_PRESETS:
                    raise RefreshError("invalid_preset", "未知 stock preset", 400)
                target["preset"] = preset
            else:
                if not isinstance(caps, list) or not caps:
                    raise RefreshError("invalid_request", "capabilities 必须是非空数组", 400)
                unknown = [c for c in caps if c not in _STOCK_CAPS]
                if unknown:
                    raise RefreshError("invalid_capability", f"未知能力: {unknown[0]}", 400)
                target["capabilities"] = list(dict.fromkeys(caps))
            allow_summary = body.get("allow_summary_only")
            if allow_summary is not None and not isinstance(allow_summary, bool):
                raise RefreshError("invalid_request", "allow_summary_only 必须是布尔", 400)
            target["allow_summary_only"] = bool(allow_summary)
            # 非本地股票 → summary_only
            local = self.local_symbols()
            if not local:
                raise RefreshError("invalid_request", "本地股票集合不可用（curated 缺失）", 400)
            non_local = [s for s in symbols if s not in local]
            if non_local and not target["allow_summary_only"]:
                raise RefreshError(
                    "invalid_symbols",
                    f"非本地股票 {non_local[0]} 需要 allow_summary_only=true", 400)
            target["summary_only_symbols"] = non_local
            return target, TARGET_STOCK
        if kind == TARGET_MARKET:
            preset = body.get("preset")
            if preset not in _MARKET_PRESETS:
                raise RefreshError("invalid_preset", "未知 market preset", 400)
            return {"kind": "market", "preset": preset}, TARGET_MARKET
        # screener：只接受 result_id（cache_scope 可选；提供则必须与存储复算一致，
        # 缺省由后端权威复算——前端不允许手输 scope）
        result_id = body.get("result_id")
        cache_scope = body.get("cache_scope")
        if not isinstance(result_id, str) or not REQUEST_ID_RE.fullmatch(result_id or ""):
            raise RefreshError("invalid_result_id", "result_id 必须是 32 位小写 hex", 400)
        if cache_scope is not None and (not isinstance(cache_scope, str)
                                        or not Q_SCOPE_RE.fullmatch(cache_scope)):
            raise RefreshError("invalid_cache_scope", "cache_scope 必须是 q_<64hex>", 400)
        if self.screener_store is None:
            raise RefreshError("invalid_request", "筛选结果存储不可用", 400)
        snapshot = self.screener_store.read_result(result_id)
        if snapshot is None:
            raise RefreshError("result_not_found", "筛选结果快照不存在或非法", 404)
        recomputed = canonical_query_hash(snapshot["query"])
        if cache_scope is not None and recomputed != cache_scope:
            raise RefreshError("invalid_cache_scope", "cache_scope 与存储 query 不一致", 400)
        capability = _MODE_CAPABILITY[snapshot["mode"]]
        return {"kind": "screener", "result_id": result_id,
                "cache_scope": recomputed, "capability": capability}, TARGET_SCREENER

    def _build_jobs(self, target: dict[str, Any], kind: str) -> tuple[list[dict[str, Any]], list[str]]:
        """生成 jobs；summary_only_symbols 过滤依赖本地历史/复权的能力。

        返回 (jobs, warnings)；过滤事实写入 warnings（不影响 canonical hash）。
        """
        jobs: list[dict[str, Any]] = []
        warnings: list[str] = []
        if kind == TARGET_STOCK:
            caps: tuple[str, ...] = tuple(target.get("capabilities") or _STOCK_PRESETS[target["preset"]])
            summary_only = set(target.get("summary_only_symbols") or [])
            for symbol in target["symbols"]:
                is_summary = symbol in summary_only
                symbol_caps = [c for c in caps
                               if not (is_summary and c in _SUMMARY_BLOCKED_CAPS)]
                if is_summary:
                    blocked = [c for c in caps if c in _SUMMARY_BLOCKED_CAPS]
                    if blocked:
                        warnings.append(
                            f"{symbol} 为 summary-only，跳过依赖本地历史/复权的能力: "
                            + ",".join(blocked))
                for cap in symbol_caps:
                    scope = "global" if cap == "lhb" else symbol
                    jobs.append({
                        "job_id": secrets.token_hex(16),
                        "capability": cap,
                        "scope": scope,
                        "status": "pending",
                        # LHB 为全局能力，不随个股 summary_only 标记
                        "summary_only": False if cap == "lhb" else is_summary,
                    })
            # LHB global 同批去重（scope=global 的 lhb 只保留一个）
            dedup: dict[tuple[str, str], dict[str, Any]] = {}
            for job in jobs:
                key = (job["capability"], job["scope"])
                if key in dedup:
                    continue
                dedup[key] = job
            jobs = list(dedup.values())
        elif kind == TARGET_MARKET:
            for cap in _MARKET_PRESETS[target["preset"]]:
                jobs.append({
                    "job_id": secrets.token_hex(16),
                    "capability": cap,
                    "scope": "global",
                    "status": "pending",
                    "summary_only": False,
                })
        else:  # screener：单 job，scope 必须 q_<64hex>
            jobs.append({
                "job_id": secrets.token_hex(16),
                "capability": target["capability"],
                "scope": target["cache_scope"],
                "status": "pending",
                "summary_only": False,
            })
        return jobs, warnings

    def _new_request_id(self) -> str:
        for _ in range(5):
            request_id = secrets.token_hex(16)
            if not self._request_path(request_id).exists():
                return request_id
        raise RefreshError("id_collision", "无法生成唯一请求标识", 409)

    def _find_incomplete(self, req_hash: str, session_fingerprint: str) -> dict[str, Any] | None:
        """按 session 隔离查重：仅相同 session 的相同未完成请求可去重。"""
        for path in self.requests_dir.glob("*.json"):
            data = _load_json(path)
            if not _valid_request_file(data):
                continue
            if data.get("request_hash") != req_hash:
                continue
            if data.get("session_fingerprint") != session_fingerprint:
                continue  # 不同 session 不得去重
            if data.get("status") in ("pending", "processing"):
                return data
        return None

    def _rate_check(self, fingerprint: str) -> None:
        now = _utc_now()
        global_active = 0
        session_active = 0
        recent_session = 0
        for path in self.requests_dir.glob("*.json"):
            data = _load_json(path)
            if not _valid_request_file(data):
                continue
            created = _parse_aware_iso(data.get("created_at"))
            if data.get("status") in ("pending", "processing"):
                global_active += 1
                if data.get("session_fingerprint") == fingerprint:
                    session_active += 1
            if data.get("session_fingerprint") == fingerprint and created is not None:
                if now - created <= timedelta(minutes=1):
                    recent_session += 1
        if session_active >= SESSION_ACTIVE_MAX:
            raise RefreshError("refresh_rate_limited",
                               f"每会话最多 {SESSION_ACTIVE_MAX} 个未完成请求", 429)
        if global_active >= GLOBAL_ACTIVE_MAX:
            raise RefreshError("refresh_rate_limited",
                               f"全局最多 {GLOBAL_ACTIVE_MAX} 个未完成请求", 429)
        if recent_session >= SESSION_PER_MINUTE_MAX:
            raise RefreshError("refresh_rate_limited",
                               f"每会话每分钟最多 {SESSION_PER_MINUTE_MAX} 个新请求", 429)

    def _update_index(self, request_id: str, request: dict[str, Any]) -> None:
        index = _load_json(self.index_path)
        if not _valid_index(index):
            index = {"schema_version": SCHEMA_VERSION, "requests": {}}
        index["updated_at"] = _iso(_utc_now())
        index["requests"][request_id] = {
            "status": request.get("status"),
            "created_at": request.get("created_at"),
            "request_hash": request.get("request_hash"),
            "session_fingerprint": request.get("session_fingerprint"),
        }
        payload = json.dumps(index, ensure_ascii=False).encode("utf-8")
        if len(payload) > MAX_INDEX_BYTES:
            raise RefreshError("index_too_large", "索引超过大小限制", 409)
        _atomic_write(self.index_path, index)

    # ---------------- 会话视图（API） ----------------
    def _public_request(self, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "request_id": data.get("request_id"),
            "status": data.get("status"),
            "target": data.get("target"),
            "jobs": [
                {"job_id": j["job_id"], "capability": j["capability"], "scope": j["scope"],
                 "status": j["status"], "summary_only": j.get("summary_only", False),
                 "fetched_at": j.get("fetched_at"), "cache_status": j.get("cache_status"),
                 "data_as_of": j.get("data_as_of"), "content_hash": j.get("content_hash"),
                 "warning": j.get("warning")}
                for j in data.get("jobs") or []
            ],
            "created_at": data.get("created_at"),
            "claimed_at": data.get("claimed_at"),
            "started_at": data.get("started_at"),
            "finished_at": data.get("finished_at"),
            "expires_at": data.get("expires_at"),
            "attempts": data.get("attempts", 0),
            "warnings": list(data.get("warnings") or []),
            "status_detail": data.get("status_detail"),
        }

    def list_for_session(self, session_id: str, *, status: str | None = None,
                         limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """仅当前 session 的请求；limit 1–50、offset≥0、返回 total。"""
        self._expire_stale()
        fingerprint = session_fingerprint(session_id)
        items: list[dict[str, Any]] = []
        for path in self.requests_dir.glob("*.json"):
            data = _load_json(path)
            if not _valid_request_file(data):
                continue
            if data.get("session_fingerprint") != fingerprint:
                continue  # 非所有者不可见
            if status and data.get("status") != status:
                continue
            items.append(self._public_request(data))
        items.sort(key=lambda r: (r["created_at"] or ""), reverse=True)
        total = len(items)
        page = items[offset:offset + limit]
        return {"items": page, "total": total}

    def get_for_session(self, request_id: str, session_id: str) -> dict[str, Any] | None:
        """详情：非所有者返回 None（调用方映射 404，避免枚举）。"""
        self._expire_stale()
        data = self._read_request_file(request_id)
        if data is None:
            return None
        if data.get("session_fingerprint") != session_fingerprint(session_id):
            return None
        return self._public_request(data)

    def cancel_for_session(self, request_id: str, session_id: str) -> dict[str, Any] | None:
        """仅 pending 可取消；非所有者 404。"""
        with self._lock:
            data = self._read_request_file(request_id)
            if data is None:
                return None
            if data.get("session_fingerprint") != session_fingerprint(session_id):
                return None
            if data.get("status") != "pending":
                raise RefreshError("refresh_not_cancellable", "仅待处理请求可取消", 409)
            self._transition(self._request_path(request_id), data, "cancelled",
                             warning="用户取消", detail="cancelled_by_user")
            return self._public_request(data)

    def _transition(self, path: Path, data: dict[str, Any], new_status: str, *,
                    warning: str | None = None, detail: str | None = None,
                    attempts: int | None = None, worker_id: str | None = None,
                    claimed_at: str | None = None, started_at: str | None = None) -> None:
        data["status"] = new_status
        data["updated_at"] = _iso(_utc_now())
        data["status_detail"] = detail or data.get("status_detail")
        if warning:
            warnings = list(data.get("warnings") or [])
            warnings.append(warning)
            data["warnings"] = warnings[-20:]
        if attempts is not None:
            data["attempts"] = attempts
        if worker_id is not None:
            data["worker_id"] = worker_id
        if claimed_at is not None:
            data["claimed_at"] = claimed_at
        if started_at is not None:
            data["started_at"] = started_at
        if new_status in ("completed", "partial", "failed", "cancelled", "expired"):
            data["finished_at"] = data.get("finished_at") or _iso(_utc_now())
        if not _valid_request_file(data):  # 防篡改污染
            return
        _atomic_write(path, data)
        self._update_index(data["request_id"], data)

    def _expire_stale(self, now: datetime | None = None) -> None:
        now = now or _utc_now()
        for path in self.requests_dir.glob("*.json"):
            data = _load_json(path)
            if not _valid_request_file(data):
                continue
            status = data.get("status")
            expires = _parse_aware_iso(data.get("expires_at"))
            if status == "pending" and expires is not None and now > expires:
                self._transition(path, data, "expired",
                                 warning="请求超过 24 小时未处理，已过期", detail="pending_expired")
            elif status == "processing":
                claimed = _parse_aware_iso(data.get("claimed_at"))
                if claimed is not None and now - claimed > WORKER_TIMEOUT:
                    self._transition(path, data, "failed",
                                     warning="处理超时（2 小时 worker_timeout），已失败",
                                     detail="worker_timeout")

    # ---------------- worker 接口（内部，不走 session 视图） ----------------
    def list_internal(self, status: str | None = None) -> list[dict[str, Any]]:
        """worker 内部读取：全部请求（不走 session 公共视图）。"""
        self._expire_stale()
        items: list[dict[str, Any]] = []
        for path in self.requests_dir.glob("*.json"):
            data = _load_json(path)
            if not _valid_request_file(data):
                continue
            if status and data.get("status") != status:
                continue
            items.append(self._public_request(data))
        items.sort(key=lambda r: (r["created_at"] or ""), reverse=True)
        return items

    def claim(self, request_id: str, worker_id: str) -> dict[str, Any] | None:
        with self._lock:
            data = self._read_request_file(request_id)
            if data is None:
                return None
            if data.get("status") != "pending":
                raise RefreshError("request_conflict", "请求非待处理状态，无法认领", 409)
            attempts = int(data.get("attempts", 0)) + 1
            now = _iso(_utc_now())
            for job in data["jobs"]:
                if job.get("status") == "pending":
                    job["status"] = "processing"
            self._transition(self._request_path(request_id), data, "processing",
                             attempts=attempts, worker_id=worker_id,
                             claimed_at=now, started_at=now)
            return self._public_request(data)

    def export_job(self, request_id: str, job_id: str, export: dict[str, Any],
                   cache_store: Any) -> dict[str, Any] | None:
        """受控导出：capability/scope 必须与 job 完全一致；写后 read 校验 + SHA-256。"""
        data = self._read_request_file(request_id)
        if data is None:
            return None
        if data.get("status") != "processing":
            raise RefreshError("request_conflict", "请求未在处理中，无法导出", 409)
        job = next((j for j in data["jobs"] if j["job_id"] == job_id), None)
        if job is None:
            raise RefreshError("job_not_found", "job 不存在", 404)
        # 受控导出元数据严格校验
        if not isinstance(export, dict):
            raise RefreshError("invalid_export", "导出必须是对象", 400)
        if set(export) != {"schema_version", "capability", "scope", "ok",
                           "fetched_at", "as_of", "data"}:
            raise RefreshError("invalid_export", "导出元数据顶层键必须精确", 400)
        if export.get("schema_version") != SCHEMA_VERSION:
            raise RefreshError("invalid_export", "导出 schema_version 不匹配", 400)
        if export.get("ok") is not True:
            raise RefreshError("invalid_export", "ok 必须为 true", 400)
        if export.get("capability") != job["capability"]:
            raise RefreshError("invalid_export", "capability 与 job 不一致", 400)
        if export.get("scope") != job["scope"]:
            raise RefreshError("invalid_export", "scope 与 job 不一致", 400)
        fetched_at = _parse_aware_iso(export.get("fetched_at"))
        if fetched_at is None or fetched_at > _utc_now():
            raise RefreshError("invalid_export", "fetched_at 必须为合法过去时间", 400)
        as_of = export.get("as_of")
        if as_of is not None and (not isinstance(as_of, str) or len(as_of) < 10):
            raise RefreshError("invalid_export", "as_of 非法", 400)
        data_payload = export.get("data")
        if not isinstance(data_payload, (dict, list)):
            raise RefreshError("invalid_export", "data 必须是对象或数组", 400)
        # 候选缓存先经正式消费者标准化；通过后才原子晋升。
        validator = _stock_consumer_validator(job["capability"], job["scope"])
        try:
            cache_store.write_validated_export(
                job["capability"], data_payload, scope=job["scope"],
                as_of=as_of, fetched_at=fetched_at.isoformat(),
                validator=validator)
        except (ValueError, OSError) as exc:
            raise RefreshError(
                "consumer_validation_failed",
                "导出内容无法通过受控标准化校验，旧缓存未受影响", 400) from exc
        # 写后重新 read 校验
        env = cache_store.read(job["capability"], job["scope"])
        if env is None:
            raise RefreshError("export_failed", "导出后缓存校验失败", 400)
        from .stocks_service import _parse_iso_ts as _pts
        fetched2 = _pts(env.get("fetched_at"))
        ttl = CAPABILITY_MAP[job["capability"]].ttl_seconds
        age = 0
        if fetched2 is not None:
            age = max(0, int((_utc_now() - fetched2).total_seconds()))
        cache_status = "fresh" if age <= ttl else "stale"
        content_hash = hashlib.sha256(
            json.dumps(env.get("data"), ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return {
            "fetched_at": env.get("fetched_at"),
            "cache_status": cache_status,
            "data_as_of": env.get("as_of"),
            "content_hash": content_hash,
        }

    def complete_job(self, request_id: str, job_id: str, result: str,
                     export_info: dict[str, Any] | None = None,
                     warning: str | None = None,
                     cache_store: Any = None) -> dict[str, Any] | None:
        """记录 job 完成（导出证据绑定）。

        - ok/partial 必须提供合法 export_info（fetched_at aware / cache_status / content_hash 64hex / data_as_of）
        - failed 不得携带 export_info，且必须提供 1–400 字 warning
        - 传 cache_store 时重读缓存比对 content_hash（防止手写 export_info 冒充）
        - 幂等（同内容 OK）/冲突
        """
        if result not in EXPORT_RESULTS:
            raise RefreshError("invalid_result", "result 必须是 ok/partial/failed", 400)
        if result in ("ok", "partial"):
            if export_info is None or not _valid_export_info(export_info):
                raise RefreshError("invalid_export_info",
                                   "ok/partial 必须提供合法 export_info（受控摘要）", 400)
            if result == "partial" and not (isinstance(warning, str) and 1 <= len(warning) <= 400):
                raise RefreshError("invalid_warning", "partial 必须提供 1–400 字 warning 说明部分缺失", 400)
        else:  # failed
            if export_info is not None:
                raise RefreshError("invalid_export_info", "failed 不得携带 export_info", 400)
            if not (isinstance(warning, str) and 1 <= len(warning) <= 400):
                raise RefreshError("invalid_warning", "failed 必须提供 1–400 字 warning", 400)
        with self._lock:
            data = self._read_request_file(request_id)
            if data is None:
                return None
            if data.get("status") != "processing":
                raise RefreshError("request_conflict", "请求未在处理中，无法记录完成", 409)
            job = next((j for j in data["jobs"] if j["job_id"] == job_id), None)
            if job is None:
                raise RefreshError("job_not_found", "job 不存在", 404)
            # 导出证据核验：重读缓存比对 content_hash（防手写摘要冒充）
            if cache_store is not None and result in ("ok", "partial"):
                env = cache_store.read(job["capability"], job["scope"])
                if env is None:
                    raise RefreshError("export_verification_failed",
                                       "缓存不存在，无法验证导出证据", 400)
                actual = hashlib.sha256(
                    json.dumps(env.get("data"), ensure_ascii=False, sort_keys=True).encode("utf-8")
                ).hexdigest()
                if actual != export_info["content_hash"]:
                    raise RefreshError("export_verification_failed",
                                       "缓存内容哈希与 export_info 不一致", 409)
            prev = job.get("status")
            if prev in ("ok", "partial", "failed"):
                # 幂等：同结果且同内容哈希 → 原样；不同内容 → 冲突
                same_hash = True
                if export_info is not None and job.get("content_hash") is not None:
                    same_hash = job.get("content_hash") == export_info.get("content_hash")
                if prev == result and same_hash:
                    return self._public_request(data)
                raise RefreshError("request_conflict", "job 已完成且内容不一致（幂等冲突）", 409)
            now = _iso(_utc_now())
            job["status"] = result
            if export_info:
                job["fetched_at"] = export_info.get("fetched_at")
                job["cache_status"] = export_info.get("cache_status")
                job["data_as_of"] = export_info.get("data_as_of")
                job["content_hash"] = export_info.get("content_hash")
            if warning:
                job["warning"] = warning
            job["recorded_at"] = now
            data["updated_at"] = now
            if not _valid_request_file(data):
                raise RefreshError("invalid_state", "状态写入后校验失败", 409)
            _atomic_write(self._request_path(request_id), data)
            return self._public_request(data)

    def finish(self, request_id: str) -> dict[str, Any] | None:
        """聚合终态 jobs；先验证并写入 receipt，成功后 request 才进入终态。

        - 存在 pending/processing job → 409 jobs_incomplete（request/receipt 均不变）
        - 聚合：全 ok → completed；含 ok/partial 且非全 ok → partial；全 failed/skipped → failed
        - receipt 超限/写入失败 → 409 receipt_write_failed（request 保持 processing，无 completed 无 receipt）
        """
        with self._lock:
            data = self._read_request_file(request_id)
            if data is None:
                return None
            if data.get("status") != "processing":
                raise RefreshError("request_conflict", "仅处理中的请求可 finish", 409)
            job_statuses = [j.get("status") for j in data["jobs"]]
            non_final = [s for s in job_statuses if s not in ("ok", "partial", "failed", "skipped")]
            if non_final:
                raise RefreshError("jobs_incomplete", "存在未完成 job，无法 finish", 409)
            total = len(job_statuses)
            ok_count = sum(1 for s in job_statuses if s == "ok")
            ok_or_partial = sum(1 for s in job_statuses if s in ("ok", "partial"))
            if ok_count == total:
                status = "completed"
                detail = f"{total} 项能力全部完成"
            elif ok_or_partial > 0:
                status = "partial"
                detail = f"{ok_or_partial}/{total} 项能力完成"
            else:
                status = "failed"
                detail = "无能力成功导出"
            # 先构建终态 request（内存）并写 receipt；成功后才落盘 request
            now = _iso(_utc_now())
            data["status"] = status
            data["status_detail"] = detail
            data["updated_at"] = now
            data["finished_at"] = data.get("finished_at") or now
            if not _valid_request_file(data):
                raise RefreshError("invalid_state", "终态校验失败", 409)
            receipt = self._build_receipt(data)
            payload = json.dumps(receipt, ensure_ascii=False).encode("utf-8")
            if len(payload) > MAX_RECEIPT_BYTES:
                raise RefreshError("receipt_write_failed", "回执超过大小上限，请求保持 processing", 409)
            try:
                _atomic_write(self._receipt_path(request_id), receipt)
            except OSError:
                raise RefreshError("receipt_write_failed", "回执写入失败，请求保持 processing", 409)
            _atomic_write(self._request_path(request_id), data)
            self._update_index(request_id, data)
            return self._public_request(data)

    def _build_receipt(self, data: dict[str, Any]) -> dict[str, Any]:
        """回执白名单：不含 data/raw/path/tool/token/堆栈；job 均为终态。"""
        receipt: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "request_id": data.get("request_id"),
            "target": data.get("target"),
            "jobs": [
                {"job_id": j["job_id"], "capability": j["capability"], "scope": j["scope"],
                 "status": j["status"], "fetched_at": j.get("fetched_at"),
                 "cache_status": j.get("cache_status"), "data_as_of": j.get("data_as_of"),
                 "content_hash": j.get("content_hash"), "warning": j.get("warning")}
                for j in data.get("jobs") or []
            ],
            "created_at": data.get("created_at"),
            "started_at": data.get("started_at"),
            "finished_at": data.get("finished_at"),
            "status": data.get("status"),
            "status_detail": data.get("status_detail"),
            "warnings": list(data.get("warnings") or [])[-10:],
        }
        return receipt

    def prune(self, max_age_days: int = PRUNE_MAX_AGE_DAYS) -> int:
        cutoff = _utc_now() - timedelta(days=max_age_days)
        removed = 0
        for path in list(self.requests_dir.glob("*.json")) + list(self.receipts_dir.glob("*.json")):
            data = _load_json(path)
            if not isinstance(data, dict):
                continue
            created = _parse_aware_iso(data.get("created_at"))
            if created is None or created >= cutoff:
                continue
            if data.get("status") in ("completed", "partial", "failed", "cancelled", "expired"):
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed


def build_refresh_store(project_root: Path) -> RefreshStore:
    root = Path(project_root)
    store = RefreshStore(root / "state" / "dashboard" / "westock-refresh",
                         curated_dir=root / "data" / "curated")
    store.screener_store = ScreenerStore(root)
    return store


# ---------------------------------------------------------------------- #
# coverage 索引（第二轮）：逐股票 curated 提取 + 完整矩阵 + query 严格
# ---------------------------------------------------------------------- #
COVERAGE_FILTER_KEYS = ("capability", "scope", "status")
COVERAGE_SCOPES = ("stock", "global", "query")
COVERAGE_STATUSES = ("fresh", "stale", "unavailable")

# global 展示集合：缺失能力也明确 unavailable，不悄悄缺键
_GLOBAL_SHOW_CAPS = (
    "lhb", "market_overview", "change_distribution", "hot_ranking",
    "sector", "index", "industry_chain", "macro",
    "filter", "strategy_select", "factor_ranking", "label_select",
)


class CoverageScanner:
    """扫描 state/dashboard/westock/<cap>/<scope>.json，全部经 WestockCacheStore.read() 校验。

    - 从 curated 文件真实提取每个 symbol；每股票独立 local_history_available
    - 股票能力完整矩阵（个股研究能力 × 本地股票），缓存缺失 → unavailable
    - global 能力独立；query scope 只接受 ^q_[0-9a-f]{64}$
    - 损坏/future → unavailable + 安全 warning
    - latest_export 用 cached_at（或成功 receipt 时间）
    - query 参数严格白名单，未知键拒绝；capability 必须 ∈ CAPABILITY_MAP
    """

    def __init__(self, cache: Any, curated_dir: Path, refresh_root: Path | None = None):
        self.cache = cache
        self.curated_dir = Path(curated_dir)
        self.refresh_root = Path(refresh_root) if refresh_root else None

    def _iter_cache_files(self):
        base = Path(self.cache.root) if hasattr(self.cache, "root") else None
        if base is None or not base.is_dir():
            return
        for cap_dir in sorted(base.iterdir()):
            if not cap_dir.is_dir():
                continue
            capability = cap_dir.name
            if capability not in CAPABILITY_MAP:
                continue
            for file in sorted(cap_dir.glob("*.json")):
                yield capability, file.stem

    def scan(self, filters: dict[str, str] | None = None) -> dict[str, Any]:
        """先构建完整矩阵，再应用 capability/scope/status 过滤，最后从返回集合计数。

        计数口径：fresh/stale/unavailable 之和 == 返回的矩阵单元格 + global 单元格 + query 单元格。
        global 展示集合（lhb + 市场/选股能力）缺失能力明确 unavailable，不悄悄缺键。
        """
        filters = filters or {}
        unknown = set(filters) - set(COVERAGE_FILTER_KEYS)
        if unknown:
            raise RefreshError("invalid_filter", "未知筛选参数: " + sorted(unknown)[0], 400)
        if "scope" in filters and filters["scope"] not in COVERAGE_SCOPES:
            raise RefreshError("invalid_filter", "scope 筛选必须是 stock/global/query", 400)
        if "status" in filters and filters["status"] not in COVERAGE_STATUSES:
            raise RefreshError("invalid_filter", "status 筛选必须是 fresh/stale/unavailable", 400)
        if "capability" in filters and filters["capability"] not in CAPABILITY_MAP:
            raise RefreshError("invalid_filter", "capability 必须属于能力白名单", 400)

        from .stocks_service import _parse_iso_ts as _pts
        local_symbols = set()
        for path in self.curated_dir.glob("daily_quotes_*_*.parquet"):
            m = re.match(r"daily_quotes_([0-9]{6}\.(?:SH|SZ|BJ))_", path.name)
            if m:
                local_symbols.add(m.group(1))

        stock_caps = tuple(c for c in CAPABILITY_MAP if c in _STOCK_CAPS or c == "lhb")
        # 完整矩阵（未过滤）
        matrix: dict[str, dict[str, str]] = {sym: {} for sym in sorted(local_symbols)}
        global_caps: dict[str, str] = {}
        query_counts: dict[str, int] = {}
        warnings: list[str] = []
        latest_export: str | None = None
        seen: set[tuple[str, str]] = set()

        for capability, scope in self._iter_cache_files():
            env = self.cache.read(capability, scope)
            if env is None:
                status = "unavailable"
                warnings.append(capability + "/" + scope + " 缓存损坏或非法，已降级为不可用")
            else:
                fetched = _pts(env.get("cached_at")) or _pts(env.get("fetched_at"))
                ttl = CAPABILITY_MAP[capability].ttl_seconds
                age = 0
                if fetched is not None:
                    age = max(0, int((_utc_now() - fetched).total_seconds()))
                status = "fresh" if age <= ttl else "stale"
                stamp = env.get("cached_at") or env.get("fetched_at")
                if stamp and (latest_export is None or stamp > latest_export):
                    latest_export = stamp
            seen.add((capability, scope))
            if scope == "global":
                global_caps[capability] = status
            elif SYMBOL_RE.fullmatch(scope):
                if scope in matrix:
                    matrix[scope][capability] = status
            elif Q_SCOPE_RE.fullmatch(scope):
                query_counts[capability] = query_counts.get(capability, 0) + 1

        # 股票缺失能力 → unavailable（完整矩阵）
        for sym in matrix:
            for cap in stock_caps:
                matrix[sym].setdefault(cap, "unavailable")
        # global 展示集合缺失 → unavailable（不悄悄缺键）
        for cap in _GLOBAL_SHOW_CAPS:
            global_caps.setdefault(cap, "unavailable")

        per_symbol_history: dict[str, bool] = {
            sym: any(p.name.startswith("daily_quotes_" + sym + "_")
                     for p in self.curated_dir.glob("daily_quotes_" + sym + "_*.parquet"))
            for sym in matrix
        }

        # 应用过滤（capability/scope/status）
        want_cap = filters.get("capability")
        want_scope = filters.get("scope")
        want_status = filters.get("status")
        f_matrix: dict[str, dict[str, str]] = {}
        for sym, caps_map in matrix.items():
            if want_scope == "global":
                continue
            row: dict[str, str] = {}
            for cap, s in caps_map.items():
                if want_cap and cap != want_cap:
                    continue
                if want_status and s != want_status:
                    continue
                row[cap] = s
            if row:
                f_matrix[sym] = row
        f_global: dict[str, str] = {}
        if want_scope in (None, "global"):
            for cap, s in global_caps.items():
                if want_cap and cap != want_cap:
                    continue
                if want_status and s != want_status:
                    continue
                f_global[cap] = s
        f_query: dict[str, int] = {}
        if want_scope in (None, "query"):
            for cap, n in query_counts.items():
                if want_cap and cap != want_cap:
                    continue
                f_query[cap] = n

        # 从返回集合计数（矩阵单元格 + global + query 单元格）
        cells = []
        for row in f_matrix.values():
            cells.extend(row.values())
        cells.extend(f_global.values())
        cells.extend(["fresh"] * sum(f_query.values()))
        fresh_count = cells.count("fresh")
        stale_count = cells.count("stale")
        unavailable_count = cells.count("unavailable")

        return {
            "schema_version": SCHEMA_VERSION,
            "capability_total": len(CAPABILITY_MAP),
            "discovered_capabilities": sorted({cap for cap, _ in seen}),
            "fresh_count": fresh_count,
            "stale_count": stale_count,
            "unavailable_count": unavailable_count,
            "stock_matrix": f_matrix,
            "stock_local_history": per_symbol_history,
            "global_capabilities": f_global,
            "query_scope_counts": f_query,
            "latest_export_at": latest_export,
            "local_history_available": bool(local_symbols),
            "warnings": warnings[-20:],
        }


def build_coverage_scanner(project_root: Path, cache: Any) -> CoverageScanner:
    root = Path(project_root)
    return CoverageScanner(
        cache,
        root / "data" / "curated",
        refresh_root=root / "state" / "dashboard" / "westock-refresh",
    )
