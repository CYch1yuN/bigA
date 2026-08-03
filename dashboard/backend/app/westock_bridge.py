"""Westock MCP bridge for the research dashboard.

The WorkBuddy connector currently owns the MCP authorization context.  The
dashboard therefore consumes versioned, atomic cache exports and never claims
that it has a direct connection when it does not.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
MAX_REFRESH_CAPABILITIES = 100
_CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_SCOPE_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


@dataclass(frozen=True)
class CapabilityDefinition:
    name: str
    tool: str
    ttl_seconds: int
    group: str
    read_only: bool = True


CAPABILITIES: tuple[CapabilityDefinition, ...] = (
    CapabilityDefinition("quote", "data_quote", 60, "行情"),
    CapabilityDefinition("minute", "data_minute", 60, "行情"),
    CapabilityDefinition("technical", "data_technical", 300, "行情"),
    CapabilityDefinition("profile", "data_profile", 86400, "基本面"),
    CapabilityDefinition("financials", "data_finance", 86400, "基本面"),
    CapabilityDefinition("forecast", "data_consensus", 86400, "基本面"),
    CapabilityDefinition("shareholders", "data_shareholder", 86400, "基本面"),
    CapabilityDefinition("dividend", "data_dividend", 86400, "基本面"),
    CapabilityDefinition("buyback", "data_buyback", 86400, "基本面"),
    CapabilityDefinition("margin", "data_fund_margin", 300, "资金"),
    CapabilityDefinition("block_trade", "data_fund_block", 900, "资金"),
    CapabilityDefinition("fund_flow", "data_fund_flow", 60, "资金"),
    CapabilityDefinition("northbound", "data_north_holding", 300, "资金"),
    CapabilityDefinition("news", "data_news", 900, "资讯事件"),
    CapabilityDefinition("reports", "data_report", 900, "资讯事件"),
    CapabilityDefinition("announcements", "data_notice", 900, "资讯事件"),
    CapabilityDefinition("events", "data_events", 900, "资讯事件"),
    CapabilityDefinition("risk", "data_risk", 900, "资讯事件"),
    CapabilityDefinition("lhb", "data_lhb", 900, "资讯事件"),
    CapabilityDefinition("chip_distribution", "data_chip", 300, "资金"),
    CapabilityDefinition("market_overview", "data_market_overview", 300, "市场"),
    CapabilityDefinition("change_distribution", "data_changedist", 300, "市场"),
    CapabilityDefinition("hot_ranking", "data_hot", 300, "市场"),
    CapabilityDefinition("sector", "data_sector", 21600, "市场"),
    CapabilityDefinition("index", "data_index", 21600, "市场"),
    CapabilityDefinition("industry_chain", "data_industry_chain", 21600, "市场"),
    CapabilityDefinition("macro", "data_macro", 21600, "市场"),
    CapabilityDefinition("filter", "tool_filter", 300, "选股"),
    CapabilityDefinition("strategy_select", "tool_strategy", 300, "选股"),
    CapabilityDefinition("factor_ranking", "tool_ranking", 300, "选股"),
    CapabilityDefinition("label_select", "tool_label", 300, "选股"),
    CapabilityDefinition("watchlist", "portfolio_watchlist", 300, "自选股"),
)
CAPABILITY_MAP = {item.name: item for item in CAPABILITIES}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _validate_envelope(value: Any, capability: str, scope: str) -> bool:
    """严格校验缓存 envelope；非法即返回 False（fail-open，read 返回 None）。"""
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
    if _parse_time(value.get("fetched_at")) is None or _parse_time(value.get("cached_at")) is None:
        return False
    if "data" not in value:
        return False
    if not isinstance(value.get("warnings"), list):
        return False
    return True


class WestockCacheStore:
    """Versioned cache files under the fixed dashboard state directory."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def _path(self, capability: str, scope: str) -> Path:
        if capability not in CAPABILITY_MAP:
            raise ValueError(f"unsupported capability: {capability}")
        if not _SCOPE_RE.fullmatch(scope):
            raise ValueError("invalid cache scope")
        return self.root / capability / f"{scope}.json"

    def write_export(
        self,
        capability: str,
        data: Any,
        *,
        scope: str = "global",
        source: str = "westock-mcp",
        as_of: str | None = None,
        fetched_at: str | None = None,
    ) -> dict[str, Any]:
        definition = CAPABILITY_MAP.get(capability)
        if definition is None or not _CAPABILITY_RE.fullmatch(capability):
            raise ValueError("unsupported capability")
        now = _utc_now().isoformat()
        envelope = {
            "schema_version": SCHEMA_VERSION,
            "capability": capability,
            "tool": definition.tool,
            "scope": scope,
            "source": source,
            "transport": "cache_export",
            "as_of": as_of,
            "fetched_at": fetched_at or now,
            "cached_at": now,
            "data": data,
            "warnings": [],
        }
        path = self._path(capability, scope)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{scope}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(envelope, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        finally:
            # 临时文件清理失败不得覆盖原始写入异常；正常写入后 os.replace
            # 已移走临时文件，此处 exists 为 False，不会残留 .tmp。
            try:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
            except OSError:
                pass
        return envelope

    def read(self, capability: str, scope: str = "global") -> dict[str, Any] | None:
        path = self._path(capability, scope)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return value if _validate_envelope(value, capability, scope) else None

    def latest_for(self, capability: str) -> dict[str, Any] | None:
        directory = self.root / capability
        if not directory.is_dir():
            return None
        candidates = sorted(directory.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in candidates:
            item = self.read(capability, path.stem)
            if item is not None:
                return item
        return None


class WestockBridge:
    """Read-only dashboard facade for the WorkBuddy-owned connector."""

    def __init__(self, cache: WestockCacheStore):
        self.cache = cache

    def connection_status(self) -> dict[str, Any]:
        now = _utc_now()
        capabilities: list[dict[str, Any]] = []
        fresh_count = stale_count = 0
        latest_success: datetime | None = None
        warnings = [
            "当前 Dashboard 未配置可用的 MCP 直连授权，使用缓存导出桥。",
            "Westock qfq/hfq 曾与 raw 返回一致，禁止作为本地复权和回测数据源。",
        ]
        for definition in CAPABILITIES:
            cached = self.cache.latest_for(definition.name)
            fetched = _parse_time(cached.get("fetched_at")) if cached else None
            future = fetched is not None and fetched > now
            age = max(0, int((now - fetched).total_seconds())) if fetched else None
            if future:
                # 未来时间戳：不判 fresh、不报告为"刚刚缓存"
                age = None
            if cached is None:
                status = "unavailable"
            elif future:
                status = "stale"
                stale_count += 1
                warnings.append(
                    f"能力 {definition.name} 的 fetched_at 晚于本地时钟（未来时间戳），"
                    "已按异常缓存处理，不标记为 fresh"
                )
            elif age is not None and age <= definition.ttl_seconds:
                status = "fresh"
                fresh_count += 1
            else:
                status = "stale"
                stale_count += 1
            if fetched and not future and (latest_success is None or fetched > latest_success):
                latest_success = fetched
            capabilities.append({
                **asdict(definition),
                "status": status,
                "cache_age_seconds": age,
                "last_success_at": fetched.isoformat() if fetched else None,
                "last_error_at": None,
                "response_ms": None,
                "circuit_state": "closed" if cached else "not_observed",
            })
        # connected 仅表示 MCP transport 是否真实连接；cache_export 模式恒为 False。
        # 缓存可用性由 cache_available / cache_status 单独表达，两者语义不混淆。
        cache_available = fresh_count > 0
        return {
            "ok": True,
            "schema_version": SCHEMA_VERSION,
            "source": "westock-mcp",
            "as_of": now.isoformat(),
            "fetched_at": latest_success.isoformat() if latest_success else None,
            "cache_status": "fresh" if fresh_count else "stale" if stale_count else "unavailable",
            "is_realtime": False,
            "transport": "cache_export",
            "availability": {
                "connected": False,
                "direct_mcp": False,
                "cache_export": True,
                "cache_available": cache_available,
                "manual_refresh": False,
            },
            "data": {
                "connected": False,
                "cache_available": cache_available,
                "capability_count": len(CAPABILITIES),
                "fresh_count": fresh_count,
                "stale_count": stale_count,
                "unavailable_count": len(CAPABILITIES) - fresh_count - stale_count,
                "capabilities": capabilities,
                "rate_limit": {"state": "inactive", "reason": "cache-export transport"},
                "circuit_breaker": {"state": "inactive", "reason": "cache-export transport"},
            },
            "warnings": warnings,
        }

    def request_refresh(self, capabilities: Iterable[str] | None = None) -> dict[str, Any]:
        requested = list(capabilities or [])
        # 数量上限（原始长度，防超大请求）→ 去重（保序）→ 白名单校验
        if len(requested) > MAX_REFRESH_CAPABILITIES:
            raise ValueError(
                f"refresh 能力数量超上限（{len(requested)} > {MAX_REFRESH_CAPABILITIES}）"
            )
        requested = list(dict.fromkeys(requested))
        invalid = [name for name in requested if name not in CAPABILITY_MAP]
        if invalid:
            raise ValueError(f"unsupported capabilities: {', '.join(invalid)}")
        return {
            "ok": True,
            "accepted": False,
            "transport": "cache_export",
            "requested": requested,
            "message": "当前授权仅存在于 WorkBuddy 会话；请由 WorkBuddy 导出缓存后刷新页面。",
        }


def build_westock_bridge(project_root: Path) -> WestockBridge:
    return WestockBridge(WestockCacheStore(Path(project_root) / "state" / "dashboard" / "westock"))


__all__ = [
    "CAPABILITIES",
    "CAPABILITY_MAP",
    "SCHEMA_VERSION",
    "CapabilityDefinition",
    "WestockBridge",
    "WestockCacheStore",
    "build_westock_bridge",
]
