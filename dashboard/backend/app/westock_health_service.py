"""F5-B：Westock 运营告警、健康评估与受控维护建议（完全只读）。

输入仅来自 F5-A `WestockOperationsService` 的受控聚合结果（_scan 上下文、
_coverage_entries/_inventory_entries/_capability_rows/_symbol_rows/failures/
request_aggregate 等），禁止另写缓存扫描器或复制状态判定表达式。

硬边界（只读）：
- 不自动调用 MCP / 不创建刷新请求 / 不修改删除缓存 / 不写告警确认忽略趋势快照
- 不修改 request/receipt/curated/signals/orders/accounts/daily/Gate4B
- 不安装计划任务、不启动交易能力
- 建议只预填 F3 刷新表单（前端行为），本服务绝不直接创建 request

健康状态模型：
- critical / degraded / attention / healthy / not_observed
- 五维独立：integrity / consumer / freshness / coverage / refresh_workflow
- 总体取五维最高严重度；无缓存且无刷新历史 → not_observed

告警规则：固定 19 条（critical 3 / high 5 / medium 7 / low 4），
由规则函数从受控聚合结果生成，alert_id 确定性
（category + **完整内部身份**（完整 request_id / 完整 q scope）SHA-256 前 8 位）。

去重原则：同一失败事实只产生一条告警——worker 超时只进 recent_worker_timeout；
recent_refresh_failure 排除 worker_timeout/unsupported/upstream_empty/skipped；
不支持与上游为空只进 low 级 unsupported_or_empty。

时间口径：所有"最近 N 小时"按**事件实际发生时间**（job.recorded_at /
请求终态 finished_at|updated_at）以 aware datetime 绝对时间比较，
不使用 created_at 近似；无合法事件时间的记录不进入活动告警。

维护建议：仅由告警固定映射生成（9 个 recommendation_code），
绝不直接创建请求；receipt/consumer/schema 问题不伪装成普通刷新可修复。
stock 预填一律给出 capabilities（注册表能力、去重、≤20）+ allow_summary_only，
不下发 preset；screener 因无 result_id 一律 can_prefill_refresh=false。

只读趋势：7/30 天按 Asia/Shanghai 自然日从 request/job/receipt 历史派生，
**按事件实际发生日归档**（发起量→created_at、终态与回执→finished_at、
job 与 worker 超时→job.recorded_at、平均耗时→完成日），
不写快照、不声称是缓存 freshness 历史；无数据日补零、除零返回 null。

单次扫描：一次 API 响应只建立一个只读 snapshot（ctx），由入口方法创建后
下传给 alerts/recommendations/trends，禁止在同一响应内重复扫描。

禁止返回：完整 q scope、content_hash、request 完整 ID、worker_id/session
fingerprint、路径/文件名、原始 warning/status_detail、MCP tool 名、token、
URL、命令或异常堆栈。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta
from typing import Any

from .westock_bridge import CAPABILITY_MAP, _utc_now
from .stocks_service import SYMBOL_RE, _parse_iso_ts
from .westock_refresh_service import (
    Q_SCOPE_RE,
    RefreshError,
    _MARKET_PRESETS,
    _STOCK_CAPS,
)
from .westock_operations_service import (
    WestockOperationsService,
    _SHANGHAI_TZ,
    _category_from_warning,
    _safe_div,
    _ts_sort_key,
)

SCHEMA_VERSION = 1
SOURCE = "westock-mcp"

SEVERITIES = ("critical", "high", "medium", "low")
_SEVERITY_WEIGHT = {"critical": 4, "high": 3, "medium": 2, "low": 1}

HEALTH_STATUSES = ("critical", "degraded", "attention", "healthy", "not_observed")
DIMENSIONS = ("integrity", "consumer", "freshness", "coverage", "refresh_workflow")

ALERT_CATEGORIES = (
    "hash_mismatch", "consumer_unusable", "receipt_mismatch",
    "invalid_cache_file", "future_timestamp", "receipt_invalid",
    "recent_worker_timeout", "recent_refresh_failure",
    "receipt_missing", "orphan_receipt", "invalid_receipt_file", "stale_cache",
    "missing_expected_cache", "low_valid_coverage", "capability_unavailable",
    "hash_unverified", "pending_evidence", "partial_refresh", "unsupported_or_empty",
)

RECOMMENDATION_CODES = (
    "refresh_invalid_cache", "refresh_hash_mismatch", "refresh_stale_capability",
    "refresh_missing_coverage", "retry_recent_failure", "inspect_receipt_chain",
    "inspect_consumer_schema", "rerun_screener_export", "no_action_required",
)

# 研究工作台关键能力（固定白名单）
KEY_CAPABILITIES = frozenset((
    "quote", "profile", "financials", "news", "fund_flow", "technical",
    "market_overview",
))

# 各健康维度关注的告警分类
_DIMENSION_CATEGORIES: dict[str, frozenset[str]] = {
    "integrity": frozenset((
        "hash_mismatch", "consumer_unusable", "receipt_mismatch",
        "invalid_cache_file", "future_timestamp", "receipt_invalid",
        "receipt_missing", "orphan_receipt", "invalid_receipt_file",
        "hash_unverified", "pending_evidence")),
    "consumer": frozenset((
        "consumer_unusable", "unsupported_or_empty", "invalid_cache_file")),
    "freshness": frozenset((
        "future_timestamp", "stale_cache", "missing_expected_cache")),
    "coverage": frozenset((
        "low_valid_coverage", "capability_unavailable", "missing_expected_cache")),
    "refresh_workflow": frozenset((
        "receipt_mismatch", "receipt_invalid", "receipt_missing",
        "recent_worker_timeout", "recent_refresh_failure", "partial_refresh",
        "orphan_receipt", "invalid_receipt_file")),
}

# 告警 → 建议代码（固定映射）
_ALERT_TO_RECOMMENDATION: dict[str, str] = {
    "hash_mismatch": "refresh_hash_mismatch",
    "consumer_unusable": "inspect_consumer_schema",
    "receipt_mismatch": "inspect_receipt_chain",
    "invalid_cache_file": "refresh_invalid_cache",
    "future_timestamp": "refresh_invalid_cache",
    "receipt_invalid": "inspect_receipt_chain",
    "recent_worker_timeout": "retry_recent_failure",
    "recent_refresh_failure": "retry_recent_failure",
    "receipt_missing": "inspect_receipt_chain",
    "orphan_receipt": "inspect_receipt_chain",
    "invalid_receipt_file": "inspect_receipt_chain",
    "stale_cache": "refresh_stale_capability",
    "missing_expected_cache": "refresh_missing_coverage",
    "low_valid_coverage": "refresh_missing_coverage",
    "capability_unavailable": "refresh_stale_capability",
    "hash_unverified": "no_action_required",
    "pending_evidence": "no_action_required",
    "partial_refresh": "retry_recent_failure",
    "unsupported_or_empty": "no_action_required",
}

# 全局能力 → 预填 market preset（固定映射；不提供任意 scope 输入）
_GLOBAL_PRESET_BY_CAP: dict[str, str] = {}
for _preset, _caps in _MARKET_PRESETS.items():
    for _c in _caps:
        _GLOBAL_PRESET_BY_CAP.setdefault(_c, _preset)


# 活动告警时间窗（小时）——recent 规则一律按"事件发生时间"判定
RECENT_WINDOW_HOURS = 24
# partial 请求也必须限定窗口，避免数月前的 partial 永久停留在活动告警区
PARTIAL_WINDOW_HOURS = 24 * 7

# 不计入"最近刷新失败"的失败分类：worker 超时另有专门规则；
# 不支持 / 上游为空 / 跳过 属于低级 unsupported_or_empty，不重复成 high 告警。
_NON_FAILURE_CATEGORIES = frozenset((
    "worker_timeout", "unsupported", "upstream_empty", "skipped",
))


def _alert_id(category: str, key: str) -> str:
    """确定性告警 ID：category + 完整内部身份键的哈希前 8 位。

    key 使用完整内部身份（完整 request_id / 完整 q scope / 真实目录名），
    经 SHA-256 后只输出前 8 位十六进制，原始身份绝不出现在响应中。
    """
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
    return f"{category}-{digest}"


def _public_capability(capability: str | None) -> str | None:
    """对外 capability 必须来自注册表，否则一律 null（不泄漏未知目录名）。"""
    if capability and capability in CAPABILITY_MAP:
        return capability
    return None


class WestockHealthService:
    """只读健康评估/告警/建议/趋势服务（复用 F5-A 聚合结果）。"""

    def __init__(self, ops: WestockOperationsService):
        self.ops = ops

    # ------------------------------------------------------------------ #
    # 基础聚合（复用 F5-A，不另写扫描器）
    # ------------------------------------------------------------------ #
    def _ctx(self) -> dict[str, Any]:
        """建立一次只读扫描快照；同一次 API 响应内只允许调用一次。"""
        return self.ops._scan()

    def _now_utc(self) -> datetime:
        """唯一时钟源（可注入）：recent 窗口与趋势自然日都由它派生。"""
        return _utc_now()

    def _shanghai_today(self) -> Any:
        """Asia/Shanghai 当前自然日，从同一个可注入时钟派生。"""
        return self._now_utc().astimezone(_SHANGHAI_TZ).date()

    # ------------------------------------------------------------------ #
    # 内部身份配对（完整 capability/scope 仅用于哈希与去重，绝不输出）
    # ------------------------------------------------------------------ #
    def _coverage_pairs(self, ctx: dict[str, Any]) -> list[tuple[tuple[str, str], dict[str, Any]]]:
        """(完整内部键, 脱敏 entry) —— 与 _coverage_entries 的 sorted(cells) 顺序严格对应。"""
        keys = sorted(ctx["cells"])
        entries = self.ops._coverage_entries(ctx)
        if len(keys) != len(entries):  # 防御：顺序契约被破坏时退回不带完整身份
            return [(("", e.get("scope") or ""), e) for e in entries]
        return list(zip(keys, entries))

    def _inventory_pairs(self, ctx: dict[str, Any]) -> list[tuple[tuple[str, str], dict[str, Any]]]:
        """(完整内部键, 脱敏 entry) —— 与 _inventory_entries 的 sorted(...) 顺序严格对应。"""
        keys = sorted(ctx["inventory_state"])
        entries = self.ops._inventory_entries(ctx)
        if len(keys) != len(entries):
            return [(("", e.get("scope") or ""), e) for e in entries]
        return list(zip(keys, entries))

    # ------------------------------------------------------------------ #
    # 告警规则
    # ------------------------------------------------------------------ #
    def _cell_alerts(self, category: str, severity: str,
                     pairs: list[tuple[tuple[str, str], dict[str, Any]]],
                     message_tpl: str, title: str,
                     ctx: dict[str, Any]) -> list[dict[str, Any]]:
        """cell 级告警：按 category + 完整 capability + 完整 scope 去重（哈希后输出）。"""
        refresh_map = ctx["refresh_map"]
        out: list[dict[str, Any]] = []
        for (full_cap, full_scope), e in pairs:
            key = f"{category}|{full_cap}|{full_scope}"
            code = _ALERT_TO_RECOMMENDATION[category]
            # 筛选查询（q scope）无法用 stock/market preset 刷新：任何"刷新类"建议
            # 一律降级为"通过原筛选结果重新导出"（requires_workbuddy=true，不可预填）
            if e.get("scope_type") == "query" and code.startswith("refresh_"):
                code = "rerun_screener_export"
            # 可信 job 证据时间：refresh lookup 的最近终态 job recorded_at
            stamp = None
            rec = refresh_map.get((full_cap, full_scope))
            if rec and rec.get("latest_status"):
                stamp = rec["latest_status"].get("recorded_at")
            out.append({
                "alert_id": _alert_id(category, key),
                "severity": severity,
                "category": category,
                "title": title,
                "message": message_tpl,
                "capability": _public_capability(e.get("capability")),
                "symbol": e["scope"] if e.get("scope_type") == "stock" else None,
                "short_scope": e.get("short_scope") or None,
                "affected_count": 1,
                "first_observed_at": stamp,
                "last_observed_at": stamp,
                "evidence": {"cell_count": 1, "file_state": e.get("file_state"),
                             "hash_status": e["integrity"].get("hash_status"),
                             "consumer_status": e.get("consumer_status")},
                "recommendation_code": code,
                "is_actionable": True,
            })
        return out

    def _aggregate_alerts(self, category: str, severity: str, count: int,
                          title: str, message: str, *, capability: str | None = None,
                          evidence: dict[str, Any] | None = None,
                          first_at: str | None = None, last_at: str | None = None,
                          short_scope: str | None = None,
                          scope_class: str | None = None) -> dict[str, Any]:
        """聚合告警。

        scope_class 用于区分同一能力下**修复路径不同**的作用域类别：
        "query"（筛选查询缓存）只能通过原筛选结果重新导出，不能用 stock/market
        preset 刷新，因此单独成行并降级为 rerun_screener_export。
        """
        key = f"{category}|{capability or ''}|{scope_class or ''}"
        code = _ALERT_TO_RECOMMENDATION[category]
        if scope_class == "query" and code.startswith("refresh_"):
            code = "rerun_screener_export"
        return {
            "alert_id": _alert_id(category, key),
            "severity": severity,
            "category": category,
            "title": title,
            "message": message,
            "capability": capability,
            "symbol": None,
            "short_scope": short_scope,
            "affected_count": count,
            "first_observed_at": first_at,
            "last_observed_at": last_at,
            "evidence": evidence or {"count": count},
            "recommendation_code": code,
            "is_actionable": code != "no_action_required",
        }

    def _request_alerts(self, category: str, severity: str, requests: list[dict[str, Any]],
                        title: str, message: str, *, matcher,
                        evidence_fn, event_time_fn=None,
                        within_hours: int | None = None) -> list[dict[str, Any]]:
        """request 级告警：按 category + 完整 request_id 去重（哈希后输出）。

        event_time_fn 返回"事实发生时间"（job.recorded_at / 请求终态时间），
        within_hours 存在时按该事件时间做绝对时间窗过滤：无合法事件时间不进窗口。
        """
        out: list[dict[str, Any]] = []
        for req in requests:
            if not matcher(req):
                continue
            event_at = event_time_fn(req) if event_time_fn is not None else None
            if within_hours is not None and not self._is_recent(event_at, within_hours):
                continue
            rid = req["request_id"]
            key = f"{category}|{rid}"  # 完整内部身份，输出只保留哈希后的 alert_id
            out.append({
                "alert_id": _alert_id(category, key),
                "severity": severity,
                "category": category,
                "title": title,
                "message": message,
                "capability": None,
                "symbol": None,
                "short_scope": None,
                "affected_count": 1,
                "first_observed_at": req.get("created_at"),
                "last_observed_at": (event_at or req.get("finished_at")
                                     or req.get("updated_at")),
                "evidence": evidence_fn(req),
                "recommendation_code": _ALERT_TO_RECOMMENDATION[category],
                "is_actionable": True,
            })
        return out

    def _is_recent(self, stamp: Any, hours: int) -> bool:
        """按 aware datetime 绝对时间比较；无法解析的时间一律不算最近。"""
        dt = _parse_iso_ts(stamp)
        if dt is None:
            return False
        return dt >= self._now_utc() - timedelta(hours=hours)

    @staticmethod
    def _latest_stamp(stamps: list[Any]) -> str | None:
        """取一组时间戳中可解析的最大者；全部非法返回 None。"""
        valid = [s for s in stamps if _parse_iso_ts(s) is not None]
        if not valid:
            return None
        return max(valid, key=_ts_sort_key)

    # ------------------------------------------------------------------ #
    # 规则实现
    # ------------------------------------------------------------------ #
    def _rule_hash_mismatch(self, cov_pairs, ctx) -> list[dict[str, Any]]:
        cells = [p for p in cov_pairs if p[1]["integrity"]["hash_status"] == "mismatch"]
        return self._cell_alerts(
            "hash_mismatch", "critical", cells,
            "预期单元的缓存数据与可信导出证据的哈希不一致，需人工核对数据完整性。",
            "缓存哈希不一致", ctx)

    def _rule_consumer_unusable(self, cov_pairs, ctx) -> list[dict[str, Any]]:
        cells = [p for p in cov_pairs if p[1]["consumer_status"] == "unusable"
                 and p[1]["integrity"]["valid"]]
        return self._cell_alerts(
            "consumer_unusable", "critical", cells,
            "缓存信封合法但正式消费者校验无法消费该数据，研究链路可能展示异常。",
            "消费者校验不可用", ctx)

    def _rule_receipt_mismatch(self, requests, receipt_status_by_request) -> list[dict[str, Any]]:
        def matcher(req):
            return receipt_status_by_request.get(req["request_id"]) == "mismatch"

        def evidence_fn(req):
            return {"receipt_status": "mismatch",
                    "job_count": len(req.get("jobs") or [])}
        return self._request_alerts(
            "receipt_mismatch", "critical", requests,
            "回执与请求精确投影不一致", "回执内容与请求记录逐字段不一致，审计链断裂，需人工核查。",
            matcher=matcher, evidence_fn=evidence_fn)

    def _rule_invalid_cache_file(self, inv_pairs, ctx) -> list[dict[str, Any]]:
        cells = [p for p in inv_pairs
                 if p[1]["file_state"] in ("invalid_json", "invalid_envelope",
                                           "invalid_timestamp")]
        return self._cell_alerts(
            "invalid_cache_file", "high", cells,
            "存在损坏或非法的缓存文件，该单元不可用，建议重新刷新。",
            "缓存文件非法", ctx)

    def _rule_future_timestamp(self, cov_pairs, inv_pairs, ctx) -> list[dict[str, Any]]:
        """未来时间戳：预期矩阵与物理 inventory 都要检查。

        意外物理文件（矩阵外、甚至非法 scope）时间在未来同样产生受控 high 告警；
        输出只有脱敏 capability/short_scope，非法 scope 不外泄。
        """
        cells = [p for p in list(cov_pairs) + list(inv_pairs)
                 if p[1]["freshness_status"] == "future_timestamp"]
        return self._cell_alerts(
            "future_timestamp", "high", cells,
            "缓存文件时间戳指向未来，时钟或数据源异常，该单元不可用。",
            "未来时间戳", ctx)

    def _rule_receipt_invalid(self, requests, receipt_status_by_request) -> list[dict[str, Any]]:
        def matcher(req):
            return receipt_status_by_request.get(req["request_id"]) == "invalid"

        def evidence_fn(req):
            return {"receipt_status": "invalid"}
        return self._request_alerts(
            "receipt_invalid", "high", requests,
            "回执文件非法", "存在回执文件但内容无法通过校验，审计链不可信。",
            matcher=matcher, evidence_fn=evidence_fn)

    # ---------------- 失败事实的分类与事件时间（项二/项三口径） ---------------- #
    @staticmethod
    def _job_failure_category(job: dict[str, Any]) -> str | None:
        """终态失败 job 的受控分类；非失败返回 None。"""
        if job.get("status") == "skipped":
            return "skipped"
        if job.get("status") != "failed":
            return None
        return _category_from_warning(job.get("warning") or "")

    @classmethod
    def _worker_timeout_jobs(cls, req: dict[str, Any]) -> list[dict[str, Any]]:
        return [j for j in req.get("jobs") or []
                if cls._job_failure_category(j) == "worker_timeout"]

    @classmethod
    def _is_worker_timeout_request(cls, req: dict[str, Any]) -> bool:
        """该请求的失败事实是否属于 worker 超时（request 级或 job 级）。"""
        return (req.get("status_detail") == "worker_timeout"
                or bool(cls._worker_timeout_jobs(req)))

    def _worker_timeout_event_at(self, req: dict[str, Any]) -> str | None:
        """worker 超时事件时间：job.recorded_at 优先，request 级用 finished_at/updated_at。"""
        stamps: list[Any] = [j.get("recorded_at") for j in self._worker_timeout_jobs(req)]
        if req.get("status_detail") == "worker_timeout":
            stamps.append(req.get("finished_at") or req.get("updated_at"))
        return self._latest_stamp(stamps)

    @classmethod
    def _plain_failed_jobs(cls, req: dict[str, Any]) -> list[dict[str, Any]]:
        """普通刷新失败 job：排除 worker 超时 / 不支持 / 上游为空 / 跳过。"""
        return [j for j in req.get("jobs") or []
                if (cls._job_failure_category(j) or "") not in _NON_FAILURE_CATEGORIES
                and cls._job_failure_category(j) is not None]

    def _rule_recent_worker_timeout(self, requests) -> list[dict[str, Any]]:
        def matcher(req):
            return self._is_worker_timeout_request(req)

        def evidence_fn(req):
            return {"worker_timeout_jobs": len(self._worker_timeout_jobs(req)) or 1}
        return self._request_alerts(
            "recent_worker_timeout", "high", requests,
            "最近出现 Worker 超时", "最近 24 小时内刷新任务出现 Worker 超时，刷新闭环可能受阻。",
            matcher=matcher, evidence_fn=evidence_fn,
            event_time_fn=self._worker_timeout_event_at,
            within_hours=RECENT_WINDOW_HOURS)

    def _rule_recent_refresh_failure(self, requests) -> list[dict[str, Any]]:
        """普通刷新失败：同一失败事实不与 worker 超时重复告警。"""
        def matcher(req):
            if self._is_worker_timeout_request(req):
                return False  # 同一失败事实已由 recent_worker_timeout 覆盖
            return bool(self._plain_failed_jobs(req))

        def evidence_fn(req):
            return {"failed_jobs": len(self._plain_failed_jobs(req))}

        def event_time_fn(req):
            return self._latest_stamp(
                [j.get("recorded_at") for j in self._plain_failed_jobs(req)])
        return self._request_alerts(
            "recent_refresh_failure", "high", requests,
            "最近刷新失败", "最近 24 小时内存在失败任务（排除 Worker 超时/不支持/上游为空/跳过），建议重试。",
            matcher=matcher, evidence_fn=evidence_fn,
            event_time_fn=event_time_fn, within_hours=RECENT_WINDOW_HOURS)

    def _rule_receipt_missing(self, requests, receipt_status_by_request) -> list[dict[str, Any]]:
        def matcher(req):
            return receipt_status_by_request.get(req["request_id"]) == "missing"

        def evidence_fn(req):
            return {"receipt_status": "missing"}
        return self._request_alerts(
            "receipt_missing", "medium", requests,
            "回执缺失", "已完成/部分完成请求缺少回执，审计链不完整。",
            matcher=matcher, evidence_fn=evidence_fn)

    def _rule_orphan_receipt(self, ctx) -> list[dict[str, Any]]:
        n = ctx.get("orphan_receipt_count", 0)
        if n <= 0:
            return []
        return [self._aggregate_alerts(
            "orphan_receipt", "medium", n, "孤立回执",
            "存在无对应合法请求的回执文件，需人工核查来源。",
            evidence={"count": n})]

    def _rule_invalid_receipt_file(self, ctx) -> list[dict[str, Any]]:
        n = ctx.get("invalid_receipt_file_count", 0)
        if n <= 0:
            return []
        return [self._aggregate_alerts(
            "invalid_receipt_file", "medium", n, "非法回执文件",
            "存在内容无法通过校验的回执文件，审计链不可信。",
            evidence={"count": n})]

    @staticmethod
    def _scope_class(entry: dict[str, Any]) -> str | None:
        """修复路径分类：query（原筛选重导出） vs 其余（preset 刷新）。"""
        return "query" if entry.get("scope_type") == "query" else None

    def _group_by_cap_scope(self, coverage, predicate):
        """按 (capability, scope_class) 分组：修复路径不同的单元不得混为一条告警。"""
        groups: dict[tuple[str, str | None], dict[str, Any]] = {}
        for e in coverage:
            if not predicate(e):
                continue
            k = (e["capability"], self._scope_class(e))
            b = groups.setdefault(k, {"count": 0, "symbols": set(), "query": 0})
            b["count"] += 1
            if e.get("scope_type") == "stock":
                b["symbols"].add(e["scope"])
            elif e.get("scope_type") == "query":
                b["query"] += 1
        return sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] or ""))

    def _rule_stale_cache(self, coverage) -> list[dict[str, Any]]:
        out = []
        for (cap, sclass), b in self._group_by_cap_scope(
                coverage, lambda e: e["freshness_status"] == "stale"):
            msg = ("该筛选查询缓存已超过 TTL，需通过原筛选结果重新导出。"
                   if sclass == "query"
                   else "该能力存在超过 TTL 的缓存单元，建议刷新。")
            out.append(self._aggregate_alerts(
                "stale_cache", "medium", b["count"], "缓存已过期", msg,
                capability=cap, scope_class=sclass,
                evidence={"count": b["count"], "capability": cap,
                          "symbols": sorted(b["symbols"])[:20],
                          "query_count": b["query"]}))
        return out

    def _rule_missing_expected_cache(self, coverage) -> list[dict[str, Any]]:
        out = []
        for (cap, sclass), b in self._group_by_cap_scope(
                coverage, lambda e: e["file_state"] == "missing"):
            msg = ("筛选查询缓存文件缺失，需通过原筛选结果重新导出。"
                   if sclass == "query"
                   else "预期覆盖单元缺少物理缓存文件，建议补齐刷新。")
            out.append(self._aggregate_alerts(
                "missing_expected_cache", "medium", b["count"], "预期缓存缺失", msg,
                capability=cap, scope_class=sclass,
                evidence={"count": b["count"], "capability": cap,
                          "symbols": sorted(b["symbols"])[:20],
                          "query_count": b["query"]}))
        return out

    def _rule_low_valid_coverage(self, ctx, coverage) -> list[dict[str, Any]]:
        expected = len(ctx["cells"])
        if expected <= 0:
            return []
        usable_fresh_stale = sum(1 for e in coverage
                                 if e["consumer_status"] == "usable"
                                 and e["freshness_status"] in ("fresh", "stale"))
        ratio = _safe_div(usable_fresh_stale, expected)
        if ratio is None or ratio >= 0.50:
            return []
        return [self._aggregate_alerts(
            "low_valid_coverage", "medium", expected,
            "有效覆盖率偏低", "有效且可用覆盖低于 50%，研究能力覆盖不足。",
            evidence={"valid_coverage": ratio, "expected_cells": expected})]

    @staticmethod
    def _cell_is_usable(entry: dict[str, Any]) -> bool:
        """单元是否"当前可用"：消费者显式判定不可用即不可用；
        其余以信封合法且时效为 fresh/stale 为准（global/query 无消费者校验器）。"""
        if entry.get("consumer_status") == "unusable":
            return False
        return bool(entry["integrity"].get("valid")) and \
            entry.get("freshness_status") in ("fresh", "stale")

    def _rule_capability_unavailable(self, coverage) -> list[dict[str, Any]]:
        """按 coverage 全部作用域聚合：expected_count>0 且 usable_count==0 即告警。

        不区分 stock/global/query —— market_overview 这类 global-only 能力缺失
        同样必须产生告警。evidence 只含计数与合法 symbol，完整 q scope 绝不输出。
        """
        stats: dict[str, dict[str, Any]] = {}
        for e in coverage:
            cap = e["capability"]
            if cap not in CAPABILITY_MAP:
                continue  # 未知能力不进入能力级告警（另有 invalid_cache_file）
            b = stats.setdefault(cap, {"expected": 0, "usable": 0, "symbols": set(),
                                       "global": 0, "query": 0})
            b["expected"] += 1
            if self._cell_is_usable(e):
                b["usable"] += 1
            st = e.get("scope_type")
            if st == "stock":
                b["symbols"].add(e["scope"])
            elif st == "query":
                b["query"] += 1
            elif st in ("global", "market"):
                b["global"] += 1
        out = []
        for cap, b in sorted(stats.items()):
            if b["expected"] <= 0 or b["usable"] > 0:
                continue
            # 该能力的预期单元全部是筛选查询 → 修复路径只有"原筛选重新导出"
            sclass = "query" if b["query"] == b["expected"] else None
            out.append(self._aggregate_alerts(
                "capability_unavailable", "medium", b["expected"],
                "能力完全不可用", "该能力存在预期单元但当前无一可用，研究功能缺失。",
                capability=cap, scope_class=sclass,
                evidence={"scope_count": b["expected"], "usable_count": 0,
                          "symbols": sorted(b["symbols"])[:20],
                          "global_count": b["global"], "query_count": b["query"]}))
        return out

    def _rule_hash_unverified(self, coverage) -> list[dict[str, Any]]:
        by_cap: dict[str, int] = {}
        for e in coverage:
            if e["integrity"]["hash_status"] == "unverified":
                by_cap[e["capability"]] = by_cap.get(e["capability"], 0) + 1
        out = []
        for cap, n in sorted(by_cap.items()):
            out.append(self._aggregate_alerts(
                "hash_unverified", "low", n, "哈希未验证",
                "存在缺少可信导出证据的缓存单元（聚合告警，不逐单元提示）。",
                capability=cap, evidence={"count": n, "capability": cap}))
        return out

    def _rule_pending_evidence(self, coverage) -> list[dict[str, Any]]:
        by_cap: dict[str, int] = {}
        for e in coverage:
            if e["integrity"]["hash_status"] == "pending_evidence":
                by_cap[e["capability"]] = by_cap.get(e["capability"], 0) + 1
        out = []
        for cap, n in sorted(by_cap.items()):
            out.append(self._aggregate_alerts(
                "pending_evidence", "low", n, "证据待定",
                "存在刷新处理中产生的匹配证据，待请求终态后确认。",
                capability=cap, evidence={"count": n, "capability": cap}))
        return out

    def _rule_partial_refresh(self, requests) -> list[dict[str, Any]]:
        """partial 作为活动告警同样限定最近窗口，数月前的 partial 不再永久显示。"""
        def matcher(req):
            return req.get("status") == "partial"

        def evidence_fn(req):
            return {"status": "partial",
                    "job_count": len(req.get("jobs") or [])}

        def event_time_fn(req):
            return self._latest_stamp([req.get("finished_at"), req.get("updated_at")])
        return self._request_alerts(
            "partial_refresh", "low", requests,
            "部分刷新完成", "最近 7 天内存在仅部分能力刷新成功的请求，建议补齐未完成部分。",
            matcher=matcher, evidence_fn=evidence_fn,
            event_time_fn=event_time_fn, within_hours=PARTIAL_WINDOW_HOURS)

    def _rule_unsupported_or_empty(self, ctx) -> list[dict[str, Any]]:
        by_cap: dict[str, int] = {}
        for req in ctx["requests"]:
            for j in req.get("jobs") or []:
                if j.get("status") == "skipped":
                    by_cap[j["capability"]] = by_cap.get(j["capability"], 0) + 1
                elif j.get("status") == "failed":
                    cat = _category_from_warning(j.get("warning") or "")
                    if cat in ("unsupported", "upstream_empty"):
                        by_cap[j["capability"]] = by_cap.get(j["capability"], 0) + 1
        out = []
        for cap, n in sorted(by_cap.items()):
            out.append(self._aggregate_alerts(
                "unsupported_or_empty", "low", n, "不支持或上游为空",
                "任务因能力不支持或上游无数据而未导出，与系统故障区分展示。",
                capability=cap, evidence={"count": n, "capability": cap}))
        return out

    # ------------------------------------------------------------------ #
    # 公开：告警
    # ------------------------------------------------------------------ #
    def alerts(self, ctx: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """生成告警。ctx 可由调用方注入，保证一次 API 响应只做一次只读扫描。"""
        if ctx is None:
            ctx = self._ctx()
        cov_pairs = self._coverage_pairs(ctx)
        inv_pairs = self._inventory_pairs(ctx)
        coverage = [e for _, e in cov_pairs]
        requests = ctx["requests"]
        receipt_status_by_request = ctx["receipt_status_by_request"]

        rules: list[list[dict[str, Any]]] = [
            self._rule_hash_mismatch(cov_pairs, ctx),
            self._rule_consumer_unusable(cov_pairs, ctx),
            self._rule_receipt_mismatch(requests, receipt_status_by_request),
            self._rule_invalid_cache_file(inv_pairs, ctx),
            self._rule_future_timestamp(cov_pairs, inv_pairs, ctx),
            self._rule_receipt_invalid(requests, receipt_status_by_request),
            self._rule_recent_worker_timeout(requests),
            self._rule_recent_refresh_failure(requests),
            self._rule_receipt_missing(requests, receipt_status_by_request),
            self._rule_orphan_receipt(ctx),
            self._rule_invalid_receipt_file(ctx),
            self._rule_stale_cache(coverage),
            self._rule_missing_expected_cache(coverage),
            self._rule_low_valid_coverage(ctx, coverage),
            self._rule_capability_unavailable(coverage),
            self._rule_hash_unverified(coverage),
            self._rule_pending_evidence(coverage),
            self._rule_partial_refresh(requests),
            self._rule_unsupported_or_empty(ctx),
        ]
        flat: list[dict[str, Any]] = []
        for rule_out in rules:
            flat.extend(rule_out)

        # 去重：同一 alert_id 只保留一条（规则内部已按去重键分组）
        seen: dict[str, dict[str, Any]] = {}
        for a in flat:
            if a["alert_id"] not in seen:
                seen[a["alert_id"]] = a
        # 排序：severity 权重 → last_observed_at 倒序（None 最后）→ alert_id
        def sort_key(a: dict[str, Any]):
            dt = _parse_iso_ts(a.get("last_observed_at"))
            ts_val = dt.timestamp() if dt else -1e18
            return (-_SEVERITY_WEIGHT[a["severity"]], -ts_val, a["alert_id"])
        return sorted(seen.values(), key=sort_key)

    # ------------------------------------------------------------------ #
    # 公开：健康状态
    # ------------------------------------------------------------------ #
    def health(self) -> dict[str, Any]:
        ctx = self._ctx()  # 本次响应唯一的只读 snapshot
        alerts = self.alerts(ctx)
        has_cache = len(ctx["inventory_state"]) > 0
        has_history = len(ctx["requests"]) > 0
        if not has_cache and not has_history:
            return {
                "overall_status": "not_observed",
                "observed": False,
                "note": "没有任何物理缓存和刷新历史。",
                "dimensions": {
                    dim: {"status": "not_observed",
                          "explanation": "无缓存且无刷新历史。"}
                    for dim in DIMENSIONS
                },
                "alert_summary": {s: 0 for s in SEVERITIES},
            }

        dims: dict[str, dict[str, Any]] = {}
        for dim in DIMENSIONS:
            cats = _DIMENSION_CATEGORIES[dim]
            relevant = [a for a in alerts if a["category"] in cats]
            worst = max((_SEVERITY_WEIGHT[a["severity"]] for a in relevant),
                        default=0)
            if worst >= 4:
                status = "critical"
            elif worst == 3:
                status = "degraded"
            elif worst == 2:
                status = "attention"
            else:
                status = "healthy"
            dims[dim] = {
                "status": status,
                "explanation": _DIMENSION_EXPLAIN[dim],
                "alert_categories": sorted({a["category"] for a in relevant}),
                "alert_count": len(relevant),
            }
        overall = max(dims.values(), key=lambda d: _STATUS_WEIGHT[d["status"]])["status"]
        return {
            "overall_status": overall,
            "observed": True,
            "note": "Westock 异常不影响本地 curated、回测与模拟账本。",
            "dimensions": dims,
            "alert_summary": {s: sum(1 for a in alerts if a["severity"] == s)
                              for s in SEVERITIES},
        }

    # ------------------------------------------------------------------ #
    # 公开：维护建议
    # ------------------------------------------------------------------ #
    def recommendations(self, ctx: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        if ctx is None:
            ctx = self._ctx()  # 本次响应唯一的只读 snapshot
        alerts = self.alerts(ctx)
        local_symbols = ctx.get("local") or set()
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for a in alerts:
            code = a["recommendation_code"]
            key = f"{code}|{a.get('capability') or ''}|{a.get('short_scope') or ''}|{a.get('symbol') or ''}"
            if key in seen:
                continue
            seen.add(key)
            rec = self._recommendation_from_alert(a, code, local_symbols)
            if rec is not None:
                out.append(rec)
        # 排序：priority 权重 → affected_count 倒序 → recommendation_id
        out.sort(key=lambda r: (-_SEVERITY_WEIGHT[r["priority"]],
                                -(r.get("affected_count") or 0),
                                r["recommendation_id"]))
        return out

    def _recommendation_from_alert(self, alert: dict[str, Any], code: str,
                                   local_symbols: Any = ()) -> dict[str, Any] | None:
        """由告警派生受控预填载荷。

        stock 预填一律走 capabilities 模式（不发 preset），能力来自注册表、去重、
        最多 20；market 走固定 preset；screener 因本服务无法得到 result_id，
        一律 can_prefill_refresh=False + requires_workbuddy=True。
        """
        rec_title, reason = _RECOMMENDATION_META[code]
        can_prefill = code in _PREFILLABLE_CODES
        target_kind: str | None = None
        preset: str | None = None
        symbols: list[str] = []
        capabilities: list[str] = []
        short_scope: str | None = None
        requires_workbuddy = code == "rerun_screener_export"

        cap = _public_capability(alert.get("capability"))
        if cap:
            capabilities = [cap]
        ev_syms = (alert.get("evidence") or {}).get("symbols") or []
        sym = alert.get("symbol")
        if sym and SYMBOL_RE.fullmatch(sym):
            symbols = [sym]
        else:
            symbols = [s for s in ev_syms if SYMBOL_RE.fullmatch(s)]
        symbols = sorted(dict.fromkeys(symbols))[:20]
        ss = alert.get("short_scope")
        if ss and Q_SCOPE_RE.fullmatch(ss) or (ss and ss.startswith("q_")):
            short_scope = ss  # 已是缩写
        elif ss:
            short_scope = ss

        # 非本地股票必须 summary-only；全部本地则 false
        allow_summary_only = any(s not in local_symbols for s in symbols)

        if code == "rerun_screener_export":
            target_kind = "screener"
            short_scope = short_scope or "原筛选结果"  # 不提供完整 q scope
            requires_workbuddy = True
            # 本服务拿不到 screener result_id，绝不用空 result_id 冒充预填
            can_prefill = False
            capabilities = []
            symbols = []
            allow_summary_only = False
        elif symbols:
            target_kind = "stock"
            preset = None
            # stock 刷新只接受个股能力；非个股能力不下发（避免 F3 校验必然失败）
            capabilities = [c for c in capabilities if c in _STOCK_CAPS]
            capabilities = sorted(dict.fromkeys(capabilities))[:20]
            if not capabilities:
                can_prefill = False
        elif cap and _GLOBAL_PRESET_BY_CAP.get(cap):
            target_kind = "market"
            preset = _GLOBAL_PRESET_BY_CAP[cap]
            # market 刷新只接受 preset：绝不下发混合模式载荷（capabilities 会被
            # F3 静默忽略，留着只会诱导前端提交出歧义 body）
            capabilities = []
            allow_summary_only = False
        else:
            target_kind = None

        rid = f"{code}-{hashlib.sha256(alert['alert_id'].encode('utf-8')).hexdigest()[:8]}"
        return {
            "recommendation_id": rid,
            "code": code,
            "priority": alert["severity"],
            "title": rec_title,
            "reason": reason,
            "affected_count": alert["affected_count"],
            "target_kind": target_kind,
            "preset": preset,
            "symbols": symbols,
            "capabilities": capabilities,
            "allow_summary_only": allow_summary_only,
            "short_scope": short_scope,
            "can_prefill_refresh": can_prefill and target_kind is not None,
            "requires_workbuddy": requires_workbuddy,
            "warnings": [],
        }

    # ------------------------------------------------------------------ #
    # 公开：只读趋势
    # ------------------------------------------------------------------ #
    def trends(self, window_days: int, ctx: dict[str, Any] | None = None) -> dict[str, Any]:
        """按实际事件日期（Asia/Shanghai 自然日）聚合的只读趋势。

        口径：
        - requests_total → request.created_at（请求"发起"日）
        - status_counts（终态）→ request.finished_at（缺失时退化 updated_at）
        - job_counts → 每个 job 自己的 recorded_at
        - worker_timeout_count → job.recorded_at；request 级超时用 finished_at/updated_at
        - receipt_issue_count → request.finished_at
        - average_duration_seconds → 归到 finished_at 所在日（完成日均耗时）
        """
        if window_days not in (7, 30):
            raise RefreshError("invalid_filter", "window_days 仅支持 7 或 30", 400)
        if ctx is None:
            ctx = self._ctx()  # 本次响应唯一的只读 snapshot
        today = self._shanghai_today()
        start = today - timedelta(days=window_days - 1)
        receipt_status_by_request = ctx["receipt_status_by_request"]

        def empty_day() -> dict[str, Any]:
            return {
                "date": "", "requests_total": 0,
                "status_counts": {"completed": 0, "partial": 0, "failed": 0,
                                  "cancelled": 0, "expired": 0},
                "job_counts": {"ok": 0, "partial": 0, "failed": 0, "skipped": 0},
                "worker_timeout_count": 0, "receipt_issue_count": 0,
                "success_rate": None, "average_duration_seconds": None,
            }

        buckets: dict[str, dict[str, Any]] = {}
        for i in range(window_days):
            day = start + timedelta(days=i)
            d = empty_day()
            d["date"] = day.isoformat()
            buckets[day.isoformat()] = d

        def bucket_of(stamp: Any) -> dict[str, Any] | None:
            """按事件时间落到 Asia/Shanghai 自然日桶；窗口外或非法时间返回 None。"""
            dt = _parse_iso_ts(stamp)
            if dt is None:
                return None
            return buckets.get(dt.astimezone(_SHANGHAI_TZ).date().isoformat())

        for req in ctx["requests"]:
            # 1) 发起量：按创建时间
            b_created = bucket_of(req.get("created_at"))
            if b_created is not None:
                b_created["requests_total"] += 1

            status = req.get("status")
            terminal_at = req.get("finished_at") or req.get("updated_at")
            # 2) 终态分布：按完成时间；pending/processing 不属于终态统计口径
            if status in ("completed", "partial", "failed", "cancelled", "expired"):
                b_fin = bucket_of(terminal_at)
                if b_fin is not None:
                    b_fin["status_counts"][status] += 1
                # 5) 回执问题：按请求完成时间
                rstatus = receipt_status_by_request.get(req["request_id"])
                if rstatus in ("missing", "invalid", "mismatch") and b_fin is not None:
                    b_fin["receipt_issue_count"] += 1
                # 6) 平均耗时：归到完成日
                dur = self.ops._request_row(req, ctx).get("duration_seconds")
                if dur is not None and b_fin is not None:
                    b_fin.setdefault("_dur", []).append(dur)

            # 3) job 分布：每个 job 用自己的 recorded_at
            wt_job_seen = False
            for j in req.get("jobs") or []:
                js = j.get("status")
                b_job = bucket_of(j.get("recorded_at"))
                if b_job is None:
                    continue
                if js in b_job["job_counts"]:
                    b_job["job_counts"][js] += 1
                # 4) worker 超时：job 级按 recorded_at
                if self._job_failure_category(j) == "worker_timeout":
                    b_job["worker_timeout_count"] += 1
                    wt_job_seen = True
            # 4b) request 级超时且无对应 job 事件：按 finished_at/updated_at 计一次
            if not wt_job_seen and req.get("status_detail") == "worker_timeout":
                b_wt = bucket_of(terminal_at)
                if b_wt is not None:
                    b_wt["worker_timeout_count"] += 1

        daily = []
        for iso in sorted(buckets):
            b = buckets[iso]
            ok = b["job_counts"]["ok"]
            partial = b["job_counts"]["partial"]
            failed = b["job_counts"]["failed"]
            skipped = b["job_counts"]["skipped"]
            b["success_rate"] = _safe_div(ok + partial, ok + partial + failed + skipped)
            durs = b.pop("_dur", [])
            b["average_duration_seconds"] = _safe_div(sum(durs), len(durs)) if durs else None
            daily.append(b)

        return {
            "window_days": window_days,
            "start_date": start.isoformat(),
            "end_date": today.isoformat(),
            "timezone": "Asia/Shanghai",
            "note": ("按事件实际发生日聚合：发起量用创建时间，终态分布/回执问题用完成时间，"
                     "任务分布与 Worker 超时用各任务记录时间；"
                     "average_duration_seconds 为该日完成请求的平均耗时（归到完成日，非创建日）。"),
            "daily": daily,
        }


    # ------------------------------------------------------------------ #
    # 认证 API：过滤与分页
    # ------------------------------------------------------------------ #
    @staticmethod
    def _pagination(params: dict[str, str], out: dict[str, Any]) -> None:
        limit_raw = params.get("limit")
        if limit_raw is not None:
            try:
                limit = int(limit_raw)
            except (TypeError, ValueError):
                raise RefreshError("invalid_filter", "limit 必须是整数", 400)
            if not (1 <= limit <= 200):
                raise RefreshError("invalid_filter", "limit 必须在 1–200", 400)
            out["limit"] = limit
        offset_raw = params.get("offset")
        if offset_raw is not None:
            try:
                offset = int(offset_raw)
            except (TypeError, ValueError):
                raise RefreshError("invalid_filter", "offset 必须是非负整数", 400)
            if offset < 0:
                raise RefreshError("invalid_filter", "offset 必须 ≥0", 400)
            out["offset"] = offset

    def alerts_api(self, filters: dict[str, str]) -> dict[str, Any]:
        allowed = {"severity", "category", "capability", "symbol", "limit", "offset"}
        unknown = set(filters) - allowed
        if unknown:
            raise RefreshError("invalid_request", f"未知查询参数: {sorted(unknown)[0]}", 400)
        params: dict[str, Any] = {"limit": 50, "offset": 0}
        self._pagination(filters, params)
        if filters.get("severity") is not None:
            if filters["severity"] not in SEVERITIES:
                raise RefreshError("invalid_filter", "severity 参数不合法", 400)
            params["severity"] = filters["severity"]
        if filters.get("category") is not None:
            if filters["category"] not in ALERT_CATEGORIES:
                raise RefreshError("invalid_filter", "category 参数不合法", 400)
            params["category"] = filters["category"]
        if filters.get("capability") is not None:
            if filters["capability"] not in CAPABILITY_MAP:
                raise RefreshError("invalid_filter", "capability 必须来自注册表", 400)
            params["capability"] = filters["capability"]
        if filters.get("symbol") is not None:
            if not SYMBOL_RE.fullmatch(filters["symbol"]):
                raise RefreshError("invalid_filter", "symbol 必须严格匹配 600519.SH", 400)
            params["symbol"] = filters["symbol"]
        items = self.alerts(self._ctx())  # 单次扫描
        if params.get("severity"):
            items = [a for a in items if a["severity"] == params["severity"]]
        if params.get("category"):
            items = [a for a in items if a["category"] == params["category"]]
        if params.get("capability"):
            items = [a for a in items if a.get("capability") == params["capability"]]
        if params.get("symbol"):
            items = [a for a in items if a.get("symbol") == params["symbol"]]
        total = len(items)
        start = params["offset"]
        end = start + params["limit"]
        return {"total": total, "limit": params["limit"], "offset": params["offset"],
                "items": items[start:end]}

    def recommendations_api(self, filters: dict[str, str]) -> dict[str, Any]:
        allowed = {"priority", "code", "target_kind", "limit", "offset"}
        unknown = set(filters) - allowed
        if unknown:
            raise RefreshError("invalid_request", f"未知查询参数: {sorted(unknown)[0]}", 400)
        params: dict[str, Any] = {"limit": 50, "offset": 0}
        self._pagination(filters, params)
        if filters.get("priority") is not None:
            if filters["priority"] not in SEVERITIES:
                raise RefreshError("invalid_filter", "priority 参数不合法", 400)
            params["priority"] = filters["priority"]
        if filters.get("code") is not None:
            if filters["code"] not in RECOMMENDATION_CODES:
                raise RefreshError("invalid_filter", "code 参数不合法", 400)
            params["code"] = filters["code"]
        if filters.get("target_kind") is not None:
            if filters["target_kind"] not in ("stock", "market", "screener", None):
                raise RefreshError("invalid_filter", "target_kind 参数不合法", 400)
            params["target_kind"] = filters["target_kind"]
        items = self.recommendations(self._ctx())  # 单次扫描
        if params.get("priority"):
            items = [r for r in items if r["priority"] == params["priority"]]
        if params.get("code"):
            items = [r for r in items if r["code"] == params["code"]]
        if params.get("target_kind"):
            items = [r for r in items if r["target_kind"] == params["target_kind"]]
        total = len(items)
        start = params["offset"]
        end = start + params["limit"]
        return {"total": total, "limit": params["limit"], "offset": params["offset"],
                "items": items[start:end]}

    def trends_api(self, filters: dict[str, str]) -> dict[str, Any]:
        unknown = set(filters) - {"window_days"}
        if unknown:
            raise RefreshError("invalid_request", f"未知查询参数: {sorted(unknown)[0]}", 400)
        window = filters.get("window_days", "7")
        if window not in ("7", "30"):
            raise RefreshError("invalid_filter", "window_days 仅支持 7 或 30", 400)
        return self.trends(int(window), self._ctx())  # 单次扫描


_STATUS_WEIGHT = {"critical": 4, "degraded": 3, "attention": 2, "healthy": 1,
                  "not_observed": 0}
_DIMENSION_EXPLAIN = {
    "integrity": "缓存文件、哈希证据与回执审计的完整性。",
    "consumer": "正式消费者校验能否消费缓存数据。",
    "freshness": "缓存时间戳合法性与 TTL 时效。",
    "coverage": "预期覆盖矩阵的可用覆盖程度。",
    "refresh_workflow": "刷新请求/回执闭环是否健康。",
}
_PREFILLABLE_CODES = frozenset((
    "refresh_invalid_cache", "refresh_hash_mismatch", "refresh_stale_capability",
    "refresh_missing_coverage", "retry_recent_failure", "rerun_screener_export",
))
_RECOMMENDATION_META: dict[str, tuple[str, str]] = {
    "refresh_invalid_cache": ("刷新非法缓存", "检测到非法或时间戳异常缓存文件，建议重新导出覆盖。"),
    "refresh_hash_mismatch": ("刷新并核对哈希", "缓存与导出证据哈希不一致，建议重新导出并核对。"),
    "refresh_stale_capability": ("刷新过期能力", "存在超过 TTL 的缓存单元，建议按能力刷新。"),
    "refresh_missing_coverage": ("补齐缺失覆盖", "预期覆盖单元缺少物理缓存或有效覆盖偏低，建议补齐。"),
    "retry_recent_failure": ("重试最近失败任务", "最近存在失败或超时任务，建议重试对应刷新。"),
    "inspect_receipt_chain": ("核查回执审计链", "回执缺失/非法/不一致，需人工核查 request/receipt 记录。"),
    "inspect_consumer_schema": ("核查消费者数据模式", "消费者校验无法消费缓存，需核查数据模式与来源。"),
    "rerun_screener_export": ("重新导出筛选结果", "筛选结果缓存缺失或不可用，建议通过原筛选重新导出。"),
    "no_action_required": ("无需操作", "当前状态属于观察/待确认，无需立即操作。"),
}


def build_health_service(ops: WestockOperationsService) -> WestockHealthService:
    return WestockHealthService(ops)
