"""Phase D: 市场研究中心（全部来自 Phase A Westock cache-export bridge，仅研究展示）。

数据边界（沿用 Phase A–C）：
- 复用 WestockCacheStore + CAPABILITY_MAP TTL → fresh/stale/unavailable。
- 无真实缓存样本依赖：全部受控白名单 schema，未知结构 → unavailable，不猜字段。
- 禁止透传任意嵌套对象/任意键/原始 JSON/MCP tool 名。
- 所有数值 finite（NaN/Infinity 丢弃 + warning）；日期合法解析，非法日期置末。
- 所有列表硬上限 + 裁剪 warning；稳定排序后分页。
- as_of 取能力数据最新合法日期，不用系统时间冒充。
- capability_meta 仅 status/as_of/fetched_at/cache_age_seconds。
- 技术性数据只展示，不写入本地 K 线/策略/回测；不创建信号/订单/持仓。
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

import re

from .stocks_deep_service import (
    MAX_TEXT,
    MAX_TITLE,
    MAX_URL,
    _norm_scalar,
    _norm_text,
    _norm_title,
    _pick,
    _safe_url,
    _trim_list,
    _valid_date_str,
)
from .stocks_service import CuratedStocksService, SYMBOL_RE, _as_finite_float, _parse_iso_ts

SCHEMA_VERSION = 1
MAX_INDEX_CODE_LEN = 12
# 指数代码白名单：六位数字 + .SH/.SZ/.BJ（禁止任意字母数字猜测）
_INDEX_CODE_RE = re.compile(r"^[0-9]{6}\.(SH|SZ|BJ)$")
_CALENDAR_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d(:[0-5]\d)?$")
_CALENDAR_CATEGORIES = ("events", "announcements")  # 派生日历实际聚合的类别

# ---------------------------------------------------------------------- #
# 硬上限
# ---------------------------------------------------------------------- #
MAX_HOT_STOCKS = 100
MAX_HOT_SECTORS = 50
MAX_SECTORS = 200
MAX_INDEXES = 100
MAX_CONSTITUENTS = 1000
MAX_CHAINS = 100
MAX_CHAIN_NODES = 100
MAX_RELATED_SYMBOLS = 100
MAX_MACRO = 200
MAX_CALENDAR = 500
MAX_EVENTS = 300
MAX_BINS = 30
CALENDAR_MAX_SPAN_DAYS = 366

_IMPORTANCE = ("low", "medium", "high")
_SECTOR_TYPES = ("industry", "concept")

# ---------------------------------------------------------------------- #
# 受控字段白名单（强制 schema）
# ---------------------------------------------------------------------- #
_OVERVIEW_FIELDS = (
    ("score", ("score", "market_score")),
    ("sentiment", ("sentiment", "sentiment_score")),
    ("trend", ("trend", "trend_score")),
    ("liquidity", ("liquidity", "liquidity_score")),
    ("breadth", ("breadth", "breadth_score")),
    ("volatility", ("volatility", "volatility_score")),
    ("risk_level", ("risk_level", "risk")),
)
_DIMENSION_FIELDS = ("trend", "sentiment", "liquidity", "breadth", "volatility", "risk")
_DISTRIBUTION_FIELDS = (
    ("rise_count", ("rise_count", "rises", "up_count")),
    ("fall_count", ("fall_count", "falls", "down_count")),
    ("flat_count", ("flat_count", "flats")),
    ("limit_up_count", ("limit_up_count", "limit_ups")),
    ("limit_down_count", ("limit_down_count", "limit_downs")),
    ("total_amount", ("total_amount", "total_turnover", "amount")),
)
_BIN_FIELDS = ("label", "min_percent", "max_percent", "count")
_HOT_STOCK_FIELDS = ("rank", "symbol", "name", "price", "change_percent", "heat", "reason", "local_history_available")
_HOT_SECTOR_FIELDS = ("rank", "code", "name", "change_percent", "heat", "leader_symbol", "leader_name", "leader_local_history_available")
_SECTOR_FIELDS = (
    "code", "name", "sector_type", "change_percent", "amount", "turnover_rate",
    "rise_count", "fall_count", "leader_symbol", "leader_name", "leader_local_history_available",
)
_INDEX_FIELDS = ("code", "name", "price", "change", "change_percent", "amount", "volume")
_CONSTITUENT_FIELDS = ("symbol", "name", "weight", "industry", "local_history_available")
_CHAIN_NODE_FIELDS = ("code", "name", "node_type", "related_symbols")
_MACRO_FIELDS = (
    "code", "name", "value", "unit", "period", "release_date",
    "previous", "forecast", "importance",
)
_EVENT_FIELDS = ("category", "date", "title", "summary", "severity", "symbols", "url")


def _finite_score(value: Any) -> float | None:
    """0–100 有限分数；超出/非法 → None。"""
    number = _as_finite_float(value)
    if number is None:
        return None
    if number < 0 or number > 100:
        return None
    return number


def _norm_index_code(value: Any) -> str | None:
    """严格白名单：六位数字 + .SH/.SZ/.BJ（大写）。ABC/000001/000001.XX/路径/空格/小写/超长全部拒绝。"""
    if not isinstance(value, str):
        return None
    if len(value) > MAX_INDEX_CODE_LEN:
        return None
    return value if _INDEX_CODE_RE.fullmatch(value) else None


def _norm_symbol(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().upper()
    return text if SYMBOL_RE.fullmatch(text) else None


# ---------------------------------------------------------------------- #
# 市场服务
# ---------------------------------------------------------------------- #

class MarketService:
    def __init__(self, project_root: Any):
        self.curated = CuratedStocksService(project_root)

    def _cap(self, capability: str) -> tuple[dict[str, Any] | None, str]:
        envelope = self.curated.westock_store.read(capability, "global")
        if envelope is None:
            return None, "unavailable"
        fetched = _parse_iso_ts(envelope.get("fetched_at"))
        if fetched is None or fetched > datetime.now(timezone.utc):
            return None, "unavailable"
        definition = CAPABILITY_MAP_LOOKUP(capability)
        age = max(0, int((datetime.now(timezone.utc) - fetched).total_seconds()))
        status = "fresh" if definition and age <= definition.ttl_seconds else "stale"
        return envelope, status

    def _meta(self, capability: str) -> dict[str, Any] | None:
        envelope, status = self._cap(capability)
        if envelope is None:
            return None
        fetched = _parse_iso_ts(envelope.get("fetched_at"))
        age = None
        if fetched is not None:
            age = max(0, int((datetime.now(timezone.utc) - fetched).total_seconds()))
        return {
            "status": status,
            "as_of": envelope.get("as_of"),
            "fetched_at": envelope.get("fetched_at"),
            "cache_age_seconds": age,
        }

    def _envelope(self, status_map: dict[str, str], data: dict[str, Any],
                  warnings: list[str], meta: dict[str, dict[str, Any] | None]) -> dict[str, Any]:
        statuses = list(status_map.values())
        cache_status = (
            "fresh" if "fresh" in statuses
            else "stale" if "stale" in statuses
            else "unavailable"
        )
        best_as_of: str | None = None
        for m in meta.values():
            if not m:
                continue
            valid = _valid_date_str(m.get("as_of"))
            if valid and (best_as_of is None or valid > best_as_of):
                best_as_of = valid
        return {
            "schema_version": SCHEMA_VERSION,
            "source": "westock-mcp",
            "as_of": best_as_of,
            "fetched_at": next((m["fetched_at"] for m in meta.values() if m and m.get("fetched_at")), None),
            "cache_status": cache_status,
            "is_realtime": False,
            "transport": "cache_export",
            "availability": status_map,
            "capability_meta": meta,
            "data": data,
            "warnings": warnings,
        }

    def _norm_cap(self, capability: str, normalize, warnings: list[str]):
        envelope, status = self._cap(capability)
        meta = self._meta(capability)
        if envelope is None or status == "unavailable":
            return None, "unavailable", meta
        normalized, reason = normalize(envelope.get("data"))
        if normalized is None:
            warnings.append(f"{capability} 缓存结构无法标准化（{reason or '无可识别字段'}），已降级为不可用")
            return None, "unavailable", meta
        return normalized, status, meta

    # ------------------------------------------------------------------ #
    # 1. overview
    # ------------------------------------------------------------------ #

    def overview(self) -> dict[str, Any]:
        warnings: list[str] = []

        def _norm(data: Any):
            if not isinstance(data, dict):
                return None, "非对象"
            out: dict[str, Any] = {}
            for key, aliases in _OVERVIEW_FIELDS:
                raw = _pick(data, aliases)
                if raw is None or isinstance(raw, (dict, list)):
                    continue
                if key == "risk_level":
                    text = _norm_text(raw, 20)
                    if text is not None:
                        out[key] = text
                    continue
                number = _finite_score(raw)
                if number is not None:
                    out[key] = number
            summary = _pick(data, ("summary", "overview"))
            if summary is not None:
                text = _norm_text(summary)
                if text is not None:
                    out["summary"] = text
            dimensions = _pick(data, ("dimensions", "dimension_scores"))
            if isinstance(dimensions, dict):
                dim_out: dict[str, Any] = {}
                for dim in _DIMENSION_FIELDS:
                    if dim in dimensions and dimensions[dim] is not None:
                        number = _finite_score(dimensions[dim])
                        if number is not None:
                            dim_out[dim] = number
                if dim_out:
                    out["dimensions"] = dim_out
            if not out:
                return None, "缺少受控字段"
            return out, None

        data, status, meta = self._norm_cap("market_overview", _norm, warnings)
        status_map = {"market_overview": status}
        return self._envelope(status_map, {"overview": data}, warnings, {"market_overview": meta})

    # ------------------------------------------------------------------ #
    # 2. distribution
    # ------------------------------------------------------------------ #

    def distribution(self) -> dict[str, Any]:
        warnings: list[str] = []

        def _norm(data: Any):
            if not isinstance(data, dict):
                return None, "非对象"
            out: dict[str, Any] = {}
            for key, aliases in _DISTRIBUTION_FIELDS:
                raw = _pick(data, aliases)
                number = _as_finite_float(raw)
                if number is not None:
                    out[key] = number
            bins_raw = _pick(data, ("bins", "distribution", "ranges"))
            if isinstance(bins_raw, list):
                bins: list[dict[str, Any]] = []
                for item in bins_raw:
                    if not isinstance(item, dict):
                        continue
                    normalized: dict[str, Any] = {}
                    for field in _BIN_FIELDS:
                        if field in item and item[field] is not None:
                            number = _as_finite_float(item[field])
                            normalized[field] = number if number is not None else _norm_text(item[field], MAX_TEXT)
                    if normalized:
                        bins.append(normalized)
                    if len(bins) >= MAX_BINS:
                        break
                if len(bins_raw) > MAX_BINS:
                    warnings.append(f"涨跌分布区间超过 {MAX_BINS} 条上限，已裁剪")
                if bins:
                    out["bins"] = bins
            if not out:
                return None, "缺少受控字段"
            return out, None

        data, status, meta = self._norm_cap("change_distribution", _norm, warnings)
        status_map = {"change_distribution": status}
        return self._envelope(status_map, {"distribution": data}, warnings, {"change_distribution": meta})

    # ------------------------------------------------------------------ #
    # 3. hot
    # ------------------------------------------------------------------ #

    def hot(self) -> dict[str, Any]:
        warnings: list[str] = []

        def _norm(data: Any):
            if not isinstance(data, dict):
                return None, "非对象"
            local_available = self._local_symbol_set()
            out: dict[str, Any] = {}
            stocks_raw = _pick(data, ("stocks", "hot_stocks", "list"))
            if isinstance(stocks_raw, list):
                stocks: list[dict[str, Any]] = []
                for item in stocks_raw:
                    if not isinstance(item, dict):
                        continue
                    normalized: dict[str, Any] = {}
                    for field in _HOT_STOCK_FIELDS:
                        if field in item and item[field] is not None:
                            if field == "symbol":
                                symbol = _norm_symbol(item[field])
                                if symbol is not None:
                                    normalized["symbol"] = symbol
                            else:
                                number = _as_finite_float(item[field])
                                normalized[field] = number if number is not None else _norm_text(item[field], MAX_TEXT)
                    if normalized.get("symbol"):
                        # local_history_available 必须由本地 curated 计算，不信 Westock
                        normalized["local_history_available"] = normalized["symbol"] in local_available
                    if normalized.get("symbol") or normalized.get("name"):
                        stocks.append(normalized)
                    if len(stocks) >= MAX_HOT_STOCKS:
                        break
                if len(stocks_raw) > MAX_HOT_STOCKS:
                    warnings.append(f"热门股票超过 {MAX_HOT_STOCKS} 条上限，已裁剪")
                if stocks:
                    out["stocks"] = stocks
            sectors_raw = _pick(data, ("sectors", "hot_sectors"))
            if isinstance(sectors_raw, list):
                sectors: list[dict[str, Any]] = []
                for item in sectors_raw:
                    if not isinstance(item, dict):
                        continue
                    normalized: dict[str, Any] = {}
                    leader_symbol: str | None = None
                    for field in _HOT_SECTOR_FIELDS:
                        if field not in item or item[field] is None:
                            continue
                        if field == "leader_symbol":
                            leader_symbol = _norm_symbol(item[field])
                            if leader_symbol is not None:
                                normalized["leader_symbol"] = leader_symbol
                            continue
                        number = _as_finite_float(item[field])
                        normalized[field] = number if number is not None else _norm_text(item[field], MAX_TEXT)
                    if leader_symbol is not None:
                        normalized["leader_local_history_available"] = leader_symbol in local_available
                    if normalized:
                        sectors.append(normalized)
                    if len(sectors) >= MAX_HOT_SECTORS:
                        break
                if len(sectors_raw) > MAX_HOT_SECTORS:
                    warnings.append(f"热门板块超过 {MAX_HOT_SECTORS} 条上限，已裁剪")
                if sectors:
                    out["sectors"] = sectors
            if not out:
                return None, "缺少受控字段"
            return out, None

        data, status, meta = self._norm_cap("hot_ranking", _norm, warnings)
        status_map = {"hot_ranking": status}
        return self._envelope(status_map, {"hot": data}, warnings, {"hot_ranking": meta})

    # ------------------------------------------------------------------ #
    # 4. sectors（sector_type 枚举校验）
    # ------------------------------------------------------------------ #

    def sectors(self) -> dict[str, Any]:
        warnings: list[str] = []

        def _norm(data: Any):
            rows = data
            if isinstance(data, dict):
                rows = _pick(data, ("sectors", "items", "list", "data"))
            if not isinstance(rows, list):
                return None, "非列表"
            local_available = self._local_symbol_set()
            out: list[dict[str, Any]] = []
            for item in rows:
                if not isinstance(item, dict):
                    return None, "行非对象"
                normalized: dict[str, Any] = {}
                leader_symbol: str | None = None
                for field in _SECTOR_FIELDS:
                    if field not in item or item[field] is None:
                        continue
                    if field == "sector_type":
                        text = _norm_text(item[field], 20)
                        if text in _SECTOR_TYPES:
                            normalized["sector_type"] = text
                        continue
                    if field == "leader_symbol":
                        leader_symbol = _norm_symbol(item[field])
                        if leader_symbol is not None:
                            normalized["leader_symbol"] = leader_symbol
                        continue
                    number = _as_finite_float(item[field])
                    normalized[field] = number if number is not None else _norm_text(item[field], MAX_TEXT)
                if leader_symbol is not None:
                    normalized["leader_local_history_available"] = leader_symbol in local_available
                if normalized.get("code") or normalized.get("name"):
                    out.append(normalized)
            if not out:
                return None, "无受控板块行"
            return _trim_list(out, "sector", warnings, MAX_SECTORS), None

        data, status, meta = self._norm_cap("sector", _norm, warnings)
        status_map = {"sector": status}
        return self._envelope(status_map, {"sectors": data}, warnings, {"sector": meta})

    # ------------------------------------------------------------------ #
    # 5. indexes + constituents
    # ------------------------------------------------------------------ #

    def indexes(self) -> dict[str, Any]:
        warnings: list[str] = []

        def _norm(data: Any):
            rows = data
            if isinstance(data, dict):
                rows = _pick(data, ("indexes", "indices", "items", "list", "data"))
            if not isinstance(rows, list):
                return None, "非列表"
            out: list[dict[str, Any]] = []
            for item in rows:
                if not isinstance(item, dict):
                    return None, "行非对象"
                normalized: dict[str, Any] = {}
                for field in _INDEX_FIELDS:
                    if field not in item or item[field] is None:
                        continue
                    if field == "code":
                        code = _norm_index_code(item[field])
                        if code is not None:
                            normalized["code"] = code
                        continue
                    number = _as_finite_float(item[field])
                    normalized[field] = number if number is not None else _norm_text(item[field], MAX_TEXT)
                if normalized.get("code"):
                    out.append(normalized)
            if not out:
                return None, "无受控指数行"
            return _trim_list(out, "index", warnings, MAX_INDEXES), None

        data, status, meta = self._norm_cap("index", _norm, warnings)
        status_map = {"index": status}
        return self._envelope(status_map, {"indexes": data}, warnings, {"index": meta})

    def constituents(self, index_code: str) -> dict[str, Any]:
        code = _norm_index_code(index_code)
        if code is None:
            raise ValueError("非法 index_code")
        warnings: list[str] = []
        envelope, status = self._cap("index")
        meta = self._meta("index")
        local_available = self._local_symbol_set()
        rows: list[dict[str, Any]] = []
        found = False
        if envelope is not None and status != "unavailable":
            raw = envelope.get("data")
            constituents_raw: Any = None
            if isinstance(raw, dict):
                # 结构1：单指数 envelope {index_code/code, constituents:[...]}
                raw_code = _norm_index_code(_pick(raw, ("index_code", "code")))
                if raw_code == code:
                    constituents_raw = _pick(raw, ("constituents", "stocks", "members"))
                    found = True
                # 结构2：多指数列表 {indexes:[{code, constituents:[...]}]}
                if not found:
                    indexes_raw = _pick(raw, ("indexes", "indices"))
                    if isinstance(indexes_raw, list):
                        for entry in indexes_raw:
                            if isinstance(entry, dict) and _norm_index_code(entry.get("code")) == code:
                                constituents_raw = _pick(entry, ("constituents", "stocks", "members"))
                                found = True
                                break
                # 结构3：受控映射 {constituents_by_index: {code:[...]}}
                if not found:
                    mapping = _pick(raw, ("constituents_by_index",))
                    if isinstance(mapping, dict) and code in mapping:
                        constituents_raw = mapping[code]
                        found = True
            if found and isinstance(constituents_raw, list):
                for item in constituents_raw:
                    if not isinstance(item, dict):
                        continue
                    symbol = _norm_symbol(_pick(item, ("symbol", "code", "stock_code")))
                    if symbol is None:
                        continue
                    normalized: dict[str, Any] = {
                        "symbol": symbol,
                        "name": _norm_text(_pick(item, ("name", "sec_name")), MAX_TEXT),
                        "weight": _as_finite_float(_pick(item, ("weight", "index_weight"))),
                        "industry": _norm_text(_pick(item, ("industry", "industry_name")), MAX_TEXT),
                        "local_history_available": symbol in local_available,
                    }
                    rows.append(normalized)
                    if len(rows) >= MAX_CONSTITUENTS:
                        break
                if len(constituents_raw) > MAX_CONSTITUENTS:
                    warnings.append(f"成分股超过 {MAX_CONSTITUENTS} 条上限，已裁剪")
            elif not found:
                warnings.append("未找到该指数的成分股缓存")  # 不返回其他指数成分，不得 500
        status_map = {"index": status}
        return self._envelope(
            status_map, {"constituents": rows, "index_code": code}, warnings, {"index": meta})

    def _local_symbol_set(self) -> set[str]:
        symbols: set[str] = set()
        from re import match as _match
        for path in self.curated.curated_dir.glob("daily_quotes_*_*.parquet"):
            m = _match(r"daily_quotes_([0-9]{6}\.(?:SH|SZ|BJ))_", path.name)
            if m:
                symbols.add(m.group(1))
        return symbols

    # ------------------------------------------------------------------ #
    # 6. industry-chain
    # ------------------------------------------------------------------ #

    def industry_chain(self) -> dict[str, Any]:
        warnings: list[str] = []

        def _norm_node(raw: Any) -> dict[str, Any] | None:
            if not isinstance(raw, dict):
                return None
            out: dict[str, Any] = {}
            for field in _CHAIN_NODE_FIELDS:
                if field not in raw or raw[field] is None:
                    continue
                if field == "related_symbols":
                    symbols_raw = raw["related_symbols"]
                    if isinstance(symbols_raw, list):
                        symbols = [s for s in (_norm_symbol(x) for x in symbols_raw) if s][:MAX_RELATED_SYMBOLS]
                        if symbols:
                            out["related_symbols"] = symbols
                    continue
                number = _as_finite_float(raw[field])
                out[field] = number if number is not None else _norm_text(raw[field], MAX_TEXT)
            return out or None

        def _norm(data: Any):
            rows = data
            if isinstance(data, dict):
                rows = _pick(data, ("chains", "industry_chains", "items", "list"))
            if not isinstance(rows, list):
                return None, "非列表"
            out: list[dict[str, Any]] = []
            for item in rows:
                if not isinstance(item, dict):
                    return None, "链非对象"
                chain: dict[str, Any] = {}
                for field in ("code", "name", "description"):
                    if field in item and item[field] is not None:
                        text = _norm_text(item[field], MAX_TEXT)
                        if text is not None:
                            chain[field] = text
                for stage in ("upstream", "midstream", "downstream"):
                    raw = item.get(stage)
                    nodes = raw if isinstance(raw, list) else None
                    if nodes is None:
                        continue
                    stage_nodes: list[dict[str, Any]] = []
                    for node in nodes:
                        normalized = _norm_node(node)
                        if normalized is not None:
                            stage_nodes.append(normalized)
                        if len(stage_nodes) >= MAX_CHAIN_NODES:
                            break
                    if stage_nodes:
                        chain[stage] = stage_nodes
                if chain.get("name") or chain.get("code"):
                    out.append(chain)
                if len(out) >= MAX_CHAINS:
                    break
            if len(rows) > MAX_CHAINS:
                warnings.append(f"产业链超过 {MAX_CHAINS} 条上限，已裁剪")
            if not out:
                return None, "无受控产业链"
            return out, None

        data, status, meta = self._norm_cap("industry_chain", _norm, warnings)
        status_map = {"industry_chain": status}
        return self._envelope(status_map, {"chains": data}, warnings, {"industry_chain": meta})

    # ------------------------------------------------------------------ #
    # 7. macro
    # ------------------------------------------------------------------ #

    def macro(self) -> dict[str, Any]:
        warnings: list[str] = []

        def _norm(data: Any):
            rows = data
            if isinstance(data, dict):
                rows = _pick(data, ("indicators", "macro", "items", "list", "data"))
            if not isinstance(rows, list):
                return None, "非列表"
            out: list[dict[str, Any]] = []
            for item in rows:
                if not isinstance(item, dict):
                    return None, "行非对象"
                normalized: dict[str, Any] = {}
                for field in _MACRO_FIELDS:
                    if field not in item or item[field] is None:
                        continue
                    if field == "importance":
                        text = _norm_text(item[field], 10)
                        if text in _IMPORTANCE:
                            normalized["importance"] = text
                        continue
                    if field == "release_date":
                        valid = _valid_date_str(item[field])
                        if valid:
                            normalized["release_date"] = valid
                        continue
                    number = _as_finite_float(item[field])
                    normalized[field] = number if number is not None else _norm_text(item[field], MAX_TEXT)
                if normalized.get("name") or normalized.get("code"):
                    out.append(normalized)
            if not out:
                return None, "无受控宏观行"
            return _trim_list(out, "macro", warnings, MAX_MACRO), None

        data, status, meta = self._norm_cap("macro", _norm, warnings)
        status_map = {"macro": status}
        return self._envelope(status_map, {"indicators": data}, warnings, {"macro": meta})

    # ------------------------------------------------------------------ #
    # 8. calendar（从 events/announcements 派生；参数边界严格）
    # ------------------------------------------------------------------ #

    def calendar(self, start_date: str | None, end_date: str | None,
                 category: str | None, importance: str | None,
                 limit: int, offset: int) -> dict[str, Any]:
        warnings: list[str] = []
        start = _valid_date_str(start_date) if start_date else None
        end = _valid_date_str(end_date) if end_date else None
        if start_date and start is None:
            raise ValueError("start_date 非法")
        if end_date and end is None:
            raise ValueError("end_date 非法")
        if start and end:
            from datetime import date as _date
            span = (_date.fromisoformat(end) - _date.fromisoformat(start)).days
            if span < 0 or span > CALENDAR_MAX_SPAN_DAYS:
                raise ValueError(f"日期范围不能超过 {CALENDAR_MAX_SPAN_DAYS} 天")
        if importance is not None and importance not in _IMPORTANCE:
            raise ValueError("importance 仅 low/medium/high")
        if category is not None and category not in _CALENDAR_CATEGORIES:
            raise ValueError(f"category 仅 {'/'.join(_CALENDAR_CATEGORIES)}")

        status_map: dict[str, str] = {}
        meta: dict[str, dict[str, Any] | None] = {}
        merged: list[dict[str, Any]] = []
        for capability in _CALENDAR_CATEGORIES:
            envelope, status = self._cap(capability)
            status_map[capability] = status
            meta[capability] = self._meta(capability)
            if envelope is None or status == "unavailable":
                continue
            items = _norm_calendar_rows(capability, envelope.get("data"), warnings)
            if items is None:
                status_map[capability] = "unavailable"
                warnings.append(f"{capability} 缓存结构无法标准化，已降级为不可用")
                continue
            merged.extend(items)

        # 先稳定排序（合法日期倒序、非法日期置末），再过滤和分页
        merged.sort(key=_calendar_sort_key)
        filtered: list[dict[str, Any]] = []
        for item in merged:
            if category and item.get("category") != category:
                continue
            if importance and item.get("importance") != importance:
                continue
            date_str = item.get("date") or ""
            if start and date_str and date_str < start:
                continue
            if end and date_str and date_str > end:
                continue
            filtered.append(item)
        if len(filtered) > MAX_CALENDAR:
            warnings.append(f"财经日历超过 {MAX_CALENDAR} 条上限，已裁剪")
            filtered = filtered[:MAX_CALENDAR]
        total = len(filtered)
        page = filtered[offset:offset + limit]
        warnings.append(
            "财经日历由 Westock 事件与公告缓存派生，并非独立财经日历能力；"
            "actual、forecast、previous 仅在来源数据明确提供时展示。"
        )
        return self._envelope(status_map, {"items": page, "total": total}, warnings, meta)

    # ------------------------------------------------------------------ #
    # 9. funds（margin + northbound；southbound 无能力 → null）
    # ------------------------------------------------------------------ #

    def funds(self) -> dict[str, Any]:
        warnings: list[str] = []
        status_map: dict[str, str] = {}
        meta: dict[str, dict[str, Any] | None] = {}
        data: dict[str, Any] = {
            "margin_balance": None, "margin_change": None,
            "northbound_net": None, "northbound_holding": None,
            "southbound_net": None, "date": None,
        }
        margin, status, m = self._norm_cap("margin", lambda d: _norm_scalar_map(
            d, (("margin_balance", ("margin_balance", "financing_balance")),
                ("margin_change", ("margin_change", "financing_change")))), warnings)
        status_map["margin"] = status
        meta["margin"] = m
        if margin:
            data["margin_balance"] = margin.get("margin_balance")
            data["margin_change"] = margin.get("margin_change")

        north, status, m = self._norm_cap("northbound", lambda d: _norm_scalar_map(
            d, (("northbound_net", ("net", "northbound_net", "net_inflow")),
                ("northbound_holding", ("holding_shares", "north_holding_shares")),
                ("date", ("date", "trade_date")))), warnings)
        status_map["northbound"] = status
        meta["northbound"] = m
        if north:
            data["northbound_net"] = north.get("northbound_net")
            data["northbound_holding"] = north.get("northbound_holding")
            date = north.get("date")
            if date is not None:
                valid = _valid_date_str(date)
                data["date"] = valid if valid else data.get("date")
        # southbound 无对应能力 → 保持 null（不猜口径）
        if data.get("northbound_net") is None and data.get("margin_balance") is None:
            warnings.append("市场资金缓存不可用或结构无法识别")
        return self._envelope(status_map, {"funds": data}, warnings, meta)

    # ------------------------------------------------------------------ #
    # 10. events（聚合 events + announcements + risk）
    # ------------------------------------------------------------------ #

    def events(self) -> dict[str, Any]:
        warnings: list[str] = []
        status_map: dict[str, str] = {}
        meta: dict[str, dict[str, Any] | None] = {}
        merged: list[dict[str, Any]] = []
        for capability in ("events", "announcements", "risk"):
            envelope, status = self._cap(capability)
            status_map[capability] = status
            meta[capability] = self._meta(capability)
            if envelope is None or status == "unavailable":
                continue
            items = _norm_event_rows(capability, envelope.get("data"), warnings)
            if items is None:
                status_map[capability] = "unavailable"
                warnings.append(f"{capability} 缓存结构无法标准化，已降级为不可用")
                continue
            merged.extend(items)
        merged.sort(key=_calendar_sort_key)
        if len(merged) > MAX_EVENTS:
            warnings.append(f"市场事件超过 {MAX_EVENTS} 条上限，已裁剪")
            merged = merged[:MAX_EVENTS]
        if merged:
            warnings.append("市场数据来自 Westock 缓存，仅作研究背景，不直接生成 BigA 信号、订单或持仓，也不修改 Gate 4B")
        return self._envelope(status_map, {"events": merged, "total": len(merged)}, warnings, meta)


# ---------------------------------------------------------------------- #
# 模块级辅助
# ---------------------------------------------------------------------- #

def _norm_scalar_map(data: Any, fields) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(data, dict):
        return None, "非对象"
    out: dict[str, Any] = {}
    for key, aliases in fields:
        raw = _pick(data, aliases)
        if raw is None or isinstance(raw, (dict, list)):
            continue
        number = _as_finite_float(raw)
        if number is not None:
            out[key] = number
        else:
            text = _norm_text(raw, MAX_TEXT)
            if text is not None:
                out[key] = text
    return (out or None), None


def _norm_event_rows(capability: str, data: Any, warnings: list[str]) -> list[dict[str, Any]] | None:
    """受控事件行：category/date/title/summary/severity/symbols/url。"""
    rows = data
    if isinstance(data, dict):
        rows = _pick(data, (capability, "items", "list", "records", "data"))
    if not isinstance(rows, list):
        return None
    out: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            return None
        normalized: dict[str, Any] = {}
        title = _norm_title(_pick(item, ("title", "name", "ann_type")))
        if title is None:
            continue
        normalized["title"] = title
        normalized["category"] = capability  # 稳定分类（不暴露英文 tool 名——用中文前端映射）
        date = _pick(item, ("date", "ann_date", "release_date", "event_date"))
        valid = _valid_date_str(date)
        if valid:
            normalized["date"] = valid
        summary = _norm_text(_pick(item, ("summary", "content", "description", "detail")), MAX_TEXT)
        if summary:
            normalized["summary"] = summary
        severity = _norm_text(_pick(item, ("severity", "level", "risk_level")), 20)
        if severity:
            normalized["severity"] = severity
        symbols_raw = _pick(item, ("symbols", "related_symbols", "stocks"))
        if isinstance(symbols_raw, list):
            symbols = [s for s in (_norm_symbol(x) for x in symbols_raw) if s][:MAX_RELATED_SYMBOLS]
            if symbols:
                normalized["symbols"] = symbols
        url = _safe_url(_pick(item, ("url", "link", "source_url")))
        if url is not None:
            normalized["url"] = url
        out.append(normalized)
    return _trim_list(out, capability, warnings, MAX_EVENTS) or None


def _norm_calendar_rows(capability: str, data: Any, warnings: list[str]) -> list[dict[str, Any]] | None:
    """派生日历受控行（独立于市场事件）：date/time/category/title/importance/country/
    actual/forecast/previous/url；importance 枚举；time 仅 HH:MM(:SS)；数值 finite 或受限标量；
    未知嵌套字段丢弃。"""
    rows = data
    if isinstance(data, dict):
        rows = _pick(data, (capability, "items", "list", "records", "data"))
    if not isinstance(rows, list):
        return None
    out: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            return None
        normalized: dict[str, Any] = {}
        title = _norm_title(_pick(item, ("title", "name", "ann_type")))
        if title is None:
            continue
        normalized["title"] = title
        normalized["category"] = capability
        date = _pick(item, ("date", "ann_date", "release_date", "event_date", "calendar_date"))
        valid = _valid_date_str(date)
        if valid:
            normalized["date"] = valid
        time_raw = _pick(item, ("time", "ann_time", "release_time"))
        if isinstance(time_raw, str):
            t = time_raw.strip()
            if _CALENDAR_TIME_RE.fullmatch(t):
                normalized["time"] = t
        importance = _norm_text(_pick(item, ("importance", "level")), 10)
        if importance in _IMPORTANCE:
            normalized["importance"] = importance
        country = _norm_text(_pick(item, ("country", "region")), 20)
        if country:
            normalized["country"] = country
        for key in ("actual", "forecast", "previous"):
            raw = _pick(item, (key,))
            if raw is None or isinstance(raw, (dict, list)):
                continue
            scalar = _norm_scalar(raw)  # NaN/Infinity 丢弃；非数值转受限文本
            if scalar is not None:
                normalized[key] = scalar
        url = _safe_url(_pick(item, ("url", "link", "source_url")))
        if url is not None:
            normalized["url"] = url
        out.append(normalized)
    return _trim_list(out, capability, warnings, MAX_CALENDAR) or None


def _calendar_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    """合法日期倒序（负 ordinal）；非法日期置末。"""
    date = _valid_date_str(item.get("date"))
    title = item.get("title") or ""
    if date is None:
        return (1, 0, title)
    ordinal = datetime.strptime(date, "%Y-%m-%d").toordinal()
    return (0, -ordinal, title)


def CAPABILITY_MAP_LOOKUP(name: str):
    from .westock_bridge import CAPABILITY_MAP
    return CAPABILITY_MAP.get(name)


def build_market_service(project_root: Any) -> MarketService:
    return MarketService(project_root)


__all__ = [
    "SCHEMA_VERSION",
    "MarketService",
    "build_market_service",
    "MAX_CALENDAR",
    "CALENDAR_MAX_SPAN_DAYS",
]
