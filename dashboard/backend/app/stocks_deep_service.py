"""Phase C: 个股深度数据聚合（全部来自 Phase A Westock cache-export bridge，仅研究展示）。

数据边界（Phase C 修正版）：
- 逐能力 fresh/stale/unavailable（复用 WestockCacheStore + CAPABILITY_MAP TTL）。
- **禁止嵌套结构原样透传**：股东/股本/三表/筹码/技术指标均为强制 schema，
  未知字段丢弃；无可识别字段 → 该能力 unavailable。
- 统一数据限制：文本 400 / 标题 200 / URL 500；各列表硬上限，超限裁剪 + warning。
- Intel 稳定分页：固定 category 顺序、合法日期倒序、非法日期置末尾；排序后 offset/limit。
- URL 安全：scheme 仅 http/https、必须 hostname、丢弃带凭据的 URL。
- 元数据 capability_meta 只含 status/as_of/fetched_at/cache_age_seconds。
- 聚合 as_of 取能力数据最新合法日期，无数据为 null，不用系统时间冒充。
- 技术指标只展示，不写入本地序列。全部只读。
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from .stocks_service import (CuratedStocksService, SYMBOL_RE, _as_finite_float,
                             identity_violation, westock_code_to_symbol)

SCHEMA_VERSION = 1
MAX_INTEL_LIMIT = 50

# ---------------------------------------------------------------------- #
# 统一数据限制
# ---------------------------------------------------------------------- #
MAX_TEXT = 400
MAX_TITLE = 200
MAX_URL = 500
_MAX_ITEMS = {
    "news": 200, "reports": 200, "announcements": 200,
    "block_trade": 100, "lhb": 100, "events": 100, "risk": 100,
}
MAX_SHAREHOLDERS = 20
MAX_CHIP_POINTS = 50
MAX_TECH_POINTS = 250
_INTEL_CATEGORIES = ("news", "reports", "announcements")  # 固定顺序（排序依据）

# ---------------------------------------------------------------------- #
# 受控标量字段白名单（profile/financials 摘要/forecast/分红/回购/两融/资金流/北向/筹码）
# ---------------------------------------------------------------------- #
_PROFILE_FIELDS = (
    ("name", ("name", "company_name", "sec_name")),
    ("industry", ("industry", "industry_name")),
    ("business", ("business", "main_business", "business_scope")),
    ("list_date", ("list_date", "ipo_date")),
    ("registered_capital", ("registered_capital", "reg_capital")),
)
_FINANCIAL_SUMMARY_FIELDS = (
    ("report_date", ("report_date", "report_period", "period")),
    ("revenue", ("revenue", "total_revenue", "operating_income")),
    ("net_profit", ("net_profit", "net_profit_attributable")),
    ("roe", ("roe", "weighted_roe")),
    ("eps", ("eps", "basic_eps")),
)
_FORECAST_FIELDS = (
    ("report_date", ("report_date", "period", "year")),
    ("consensus_eps", ("consensus_eps", "eps_forecast", "eps")),
    ("consensus_revenue", ("consensus_revenue", "revenue_forecast", "revenue")),
    ("rating", ("rating", "rating_summary", "avg_rating")),
    ("target_price", ("target_price", "avg_target_price", "target")),
)
_DIVIDEND_FIELDS = (
    ("plan", ("plan", "dividend_plan", "scheme")),
    ("ex_date", ("ex_date", "ex_dividend_date")),
    ("pay_date", ("pay_date", "payment_date")),
)
_BUYBACK_FIELDS = (
    ("status", ("status", "progress", "state")),
    ("price_range", ("price_range", "price_interval")),
    ("amount", ("amount", "total_amount", "planned_amount")),
)
_MARGIN_FIELDS = (
    ("margin_balance", ("margin_balance", "financing_balance", "margin_balance_rmb")),
    ("margin_change", ("margin_change", "financing_change", "margin_change_rmb")),
    ("short_balance", ("short_balance", "securities_lending_balance", "short_balance_rmb")),
    ("short_change", ("short_change", "securities_lending_change", "short_change_rmb")),
)
_NORTHBOUND_FIELDS = (
    ("holding_shares", ("holding_shares", "north_holding_shares", "shares")),
    ("holding_ratio", ("holding_ratio", "north_holding_ratio", "ratio")),
    ("change", ("change", "north_change", "change_shares")),
)
_FUND_FLOW_FIELDS = (
    ("main", ("main", "main_net_inflow", "main_net", "MainNetFlow")),
    ("super_large", ("super_large", "super_large_net", "xl_net", "JumboNetFlow")),
    ("large", ("large", "large_net", "BlockNetFlow")),
    ("medium", ("medium", "medium_net", "MidNetFlow")),
    ("small", ("small", "small_net", "SmallNetFlow")),
)

# ---------------------------------------------------------------------- #
# 强制嵌套 schema（禁止任意键透传）
# ---------------------------------------------------------------------- #
_SHAREHOLDER_ITEM = ("name", "shares", "ratio", "change")
_SHARE_STRUCTURE_FIELDS = ("total_shares", "float_shares", "restricted_shares")
_SHEET_FIELDS = {
    "income_statement": ("revenue", "cost", "operating_profit", "total_profit", "net_profit"),
    "balance_sheet": ("total_assets", "total_liabilities", "equity", "cash", "accounts_receivable"),
    "cash_flow": ("operating_cash_flow", "investing_cash_flow", "financing_cash_flow", "net_cash_flow"),
}
_CHIP_DIST_FIELDS = ("price", "ratio", "chips")
_TECH_SCHEMA = {
    "ma": ("ma5", "ma10", "ma20", "ma60"),
    "macd": ("dif", "dea", "macd"),
    "kdj": ("k", "d", "j"),
    "boll": ("upper", "mid", "lower"),
    "rsi": ("rsi6", "rsi12", "rsi24"),
}

_INTEL_ITEM_FIELDS = {
    "news": ("title", "summary", "source", "date", "url"),
    "reports": ("title", "org", "rating", "target_price", "date"),
    "announcements": ("title", "ann_type", "date"),
}
_EVENT_TAGS = ("tags", "label", "event_type")
_RISK_FIELDS = (
    ("severity", ("severity", "level")),
    ("title", ("title", "risk_type", "name")),
    ("description", ("description", "summary", "detail")),
)


def _pick(data: dict[str, Any], aliases: tuple[str, ...]) -> Any:
    for alias in aliases:
        if alias in data and data[alias] is not None:
            return data[alias]
    return None


def _norm_text(value: Any, limit: int = MAX_TEXT) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _norm_title(value: Any) -> str | None:
    return _norm_text(value, MAX_TITLE)


def _norm_scalar(value: Any) -> Any:
    """标量：数值→finite float（NaN/Infinity 丢弃）；文本→裁剪；dict/list→None。"""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _norm_text(value)
    if math.isnan(number) or math.isinf(number):
        return None  # NaN/Infinity 丢弃
    return number


def _norm_mapping(data: Any, fields: tuple[tuple[str, tuple[str, ...]], ...]) -> dict[str, Any] | None:
    """受控标量：只输出白名单标量字段；嵌套结构一律丢弃。"""
    if not isinstance(data, dict):
        return None
    out: dict[str, Any] = {}
    for key, aliases in fields:
        raw = _pick(data, aliases)
        if raw is None or isinstance(raw, (dict, list)):
            continue  # 嵌套丢弃
        normalized = _norm_scalar(raw)
        if normalized is not None:
            out[key] = normalized
    return out or None


def _unwrap_fund_flow(data: Any) -> Any:
    """解包 fund_flow 真实嵌套：{"sh600519": {"data": [{...}]}} → 首行 dict。

    仅当外层恰有一个 sh/sz/bj 前缀键且内层含列表字段（data/list/items/records）
    时解包取首行；其余结构原样返回，交由受控标准化判断。
    """
    if isinstance(data, dict) and len(data) == 1:
        key, inner = next(iter(data.items()))
        if isinstance(key, str) and len(key) == 8 and key[:2] in ("sh", "sz", "bj") \
                and key[2:].isdigit() and isinstance(inner, dict):
            for list_key in ("data", "list", "items", "records"):
                rows = inner.get(list_key)
                if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                    return rows[0]
            return inner
    return data


def _secucode_conflict(secu: Any, expected_symbol: str) -> str | None:
    """SecuCode 身份校验（脱敏，不回显原始值）。

    - 纯六位（600519）：与请求六位数字比较；
    - 带市场前缀（sh600519）：经统一转换后完整比较市场（sz600519 对 600519.SH 必冲突）；
    - 非法格式：冲突。
    返回固定 reason 或 None（一致/缺失）。
    """
    if secu is None:
        return None
    if not isinstance(secu, str):
        return "SecuCode 格式非法"
    if re.fullmatch(r"[0-9]{6}", secu):
        if secu != expected_symbol[:6]:
            return "SecuCode 数字部分与请求标的不一致"
        return None
    converted = westock_code_to_symbol(secu)
    if converted is None:
        return "SecuCode 格式非法"
    if converted != expected_symbol:
        return "SecuCode 市场与请求标的不一致"
    return None


def _profile_identity_conflict(data: Any, expected_symbol: str) -> str | None:
    """profile 身份冲突定位（脱敏 reason）。无冲突或非 dict 返回 None。"""
    if not isinstance(data, dict):
        return None
    code = data.get("code")
    if code is None:
        return None
    if identity_violation(expected_symbol, code):
        return "profile code 与请求标的不一致"
    return None


def _fund_flow_identity_conflict(data: Any, expected_symbol: str) -> str | None:
    """fund_flow 身份冲突定位（脱敏 reason）。

    仅当外层唯一键可被统一转换解析且值为 dict 时视为 wrapper：
    - wrapper：外层代码必须与 expected 完整一致；inner.code/inner.symbol、首行 code/symbol
      若存在必须一致；
    其余（含 len!=1 的 dict）视为 flat：顶层 code/symbol/SecuCode 若存在必须一致，
    不得因结构非单键而跳过身份校验；
    不含任何身份字段的兼容 flat 数据 → 无冲突，交给受控字段标准化。
    """
    if isinstance(data, dict) and len(data) == 1:
        key, inner = next(iter(data.items()))
        key_symbol = westock_code_to_symbol(key) if isinstance(key, str) else None
        if key_symbol is not None and isinstance(inner, dict):
            # wrapper 形态
            if key_symbol != expected_symbol:
                return "外层股票代码与请求标的不一致"
            if identity_violation(expected_symbol, inner.get("code")):
                return "内层 code 与请求标的不一致"
            if identity_violation(expected_symbol, inner.get("symbol")):
                return "内层 symbol 与请求标的不一致"
            for list_key in ("data", "list", "items", "records"):
                rows = inner.get(list_key)
                if isinstance(rows, list) and rows and isinstance(rows[0], dict):
                    first = rows[0]
                    if identity_violation(expected_symbol, first.get("code")):
                        return "首行 code 与请求标的不一致"
                    if identity_violation(expected_symbol, first.get("symbol")):
                        return "首行 symbol 与请求标的不一致"
                    conflict = _secucode_conflict(first.get("SecuCode"), expected_symbol)
                    if conflict:
                        return conflict
                    break
            return None
    if isinstance(data, dict):
        # flat 形态：顶层身份字段若存在必须一致
        if identity_violation(expected_symbol, data.get("code")):
            return "code 与请求标的不一致"
        if identity_violation(expected_symbol, data.get("symbol")):
            return "symbol 与请求标的不一致"
        conflict = _secucode_conflict(data.get("SecuCode"), expected_symbol)
        if conflict:
            return conflict
    return None


def _norm_news_identity_items(data: Any, warnings: list[str], expected_symbol: str
                              ) -> tuple[list[dict[str, Any]] | None, str]:
    """news 身份过滤：条目 symbol 存在且与 expected_symbol 不一致 → 丢弃该条。

    返回 (items, reason)：
    - "ok"：有合法条目输出；
    - "identity_all_dropped"：存在合法 dict 条目且全部因 symbol 错配被丢弃（warning 已记录）；
    - "empty"：空数组（调用方补受控 warning）；
    - "structure"：非列表 / 全部非 dict / 无受控字段（调用方补受控 warning）。
    非 dict 条目不计入身份错配数量。symbol 不在输出白名单，天然不进前端。
    """
    rows = data
    if isinstance(data, dict):
        rows = _pick(data, ("items", "list", "records", "data"))
    if not isinstance(rows, list):
        return None, "structure"
    if not rows:
        return None, "empty"
    kept_rows: list[dict[str, Any]] = []
    dropped = 0
    non_dict = 0
    for row in rows:
        if not isinstance(row, dict):
            non_dict += 1
            continue
        sym = row.get("symbol")
        if sym is not None and identity_violation(expected_symbol, sym):
            dropped += 1
            continue
        kept_rows.append(row)
    if dropped:
        warnings.append(f"news 含身份不匹配条目，已丢弃 {dropped} 条")
    if not kept_rows:
        # 全非 dict → 结构错误（非身份问题）；否则为身份全丢弃
        return (None, "structure") if non_dict else (None, "identity_all_dropped")
    # 复用受控输出：category 稳定标签 + URL 安全过滤；symbol 不在白名单，天然不进前端
    items = _norm_intel_items("news", kept_rows, warnings)
    if items is None:
        return None, "structure"
    return items, "ok"


def _valid_date_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if len(text) < 10:
        return None
    try:
        datetime.strptime(text[:10], "%Y-%m-%d")
    except ValueError:
        return None
    return text[:10]


def _safe_url(value: Any) -> str | None:
    """URL parser：scheme http/https、必须 hostname、丢弃带凭据。"""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = urlparse(text)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    if not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    return text[:MAX_URL]


def _trim_list(items: list[dict[str, Any]], capability: str, warnings: list[str], limit: int) -> list[dict[str, Any]]:
    if len(items) > limit:
        warnings.append(f"{capability} 条目超过 {limit} 条上限，已裁剪")
        return items[:limit]
    return items


# ---------------------------------------------------------------------- #
# 强制 schema 标准化
# ---------------------------------------------------------------------- #

def _norm_major_shareholders(data: Any, warnings: list[str]) -> list[dict[str, Any]] | None:
    rows = data
    if isinstance(data, dict):
        rows = _pick(data, ("major_shareholders", "top_shareholders", "shareholders"))
    if not isinstance(rows, list):
        return None
    out: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            return None
        normalized: dict[str, Any] = {}
        for field in _SHAREHOLDER_ITEM:
            if field in item and item[field] is not None:
                number = _as_finite_float(item[field])
                normalized[field] = number if number is not None else _norm_text(item[field])
        if normalized:
            out.append(normalized)
        if len(out) >= MAX_SHAREHOLDERS:
            break
    if len(rows) > MAX_SHAREHOLDERS:
        warnings.append(f"主要股东超过 {MAX_SHAREHOLDERS} 条上限，已裁剪")
    return out or None


def _norm_share_structure(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    raw = _pick(data, ("share_structure", "capital_structure"))
    if not isinstance(raw, dict):
        return None
    out: dict[str, Any] = {}
    for field in _SHARE_STRUCTURE_FIELDS:
        if field in raw and raw[field] is not None:
            out[field] = _as_finite_float(raw[field])
    return out or None


def _norm_financials(data: Any) -> tuple[Any, str | None]:
    """财务：摘要（标量白名单）+ 三张报表（强制字段 schema）。"""
    if not isinstance(data, dict):
        return None, "非对象"
    summary = _norm_mapping(data, _FINANCIAL_SUMMARY_FIELDS)
    out: dict[str, Any] = {}
    if summary:
        out["summary"] = summary
    for sheet, fields in _SHEET_FIELDS.items():
        raw = _pick(data, (sheet, f"{sheet}_summary"))
        if not isinstance(raw, dict):
            continue
        normalized: dict[str, Any] = {}
        for field in fields:
            if field in raw and raw[field] is not None:
                number = _as_finite_float(raw[field])
                if number is not None:
                    normalized[field] = number
        if normalized:
            out[sheet] = normalized
    if not out:
        return None, "缺少受控财务字段"
    return out, None


def _norm_chip(data: Any, warnings: list[str]) -> tuple[dict[str, Any] | None, str | None]:
    if not isinstance(data, dict):
        return None, "非对象"
    concentration = _pick(data, ("concentration", "chip_concentration", "concentration_ratio"))
    out: dict[str, Any] = {}
    if concentration is not None:
        number = _as_finite_float(concentration)
        if number is not None:
            out["concentration"] = number
    dist_raw = _pick(data, ("distribution", "chip_distribution", "price_distribution"))
    if isinstance(dist_raw, list):
        points: list[dict[str, Any]] = []
        for point in dist_raw:
            if not isinstance(point, dict):
                continue
            normalized: dict[str, Any] = {}
            for field in _CHIP_DIST_FIELDS:
                if field in point and point[field] is not None:
                    number = _as_finite_float(point[field])
                    if number is not None:
                        normalized[field] = number
            if normalized:
                points.append(normalized)
            if len(points) >= MAX_CHIP_POINTS:
                break
        if len(dist_raw) > MAX_CHIP_POINTS:
            warnings.append(f"筹码分布点超过 {MAX_CHIP_POINTS} 条上限，已裁剪")
        if points:
            out["distribution"] = points
    if not out:
        return None, "缺少受控筹码字段"
    return out, None


def _norm_technical(data: Any) -> tuple[Any, str | None]:
    """技术指标强制 schema：MA/MACD/KDJ/BOLL/RSI 白名单，所有数值 finite。"""
    if not isinstance(data, dict):
        return None, "非对象"
    out: dict[str, Any] = {}
    for group, fields in _TECH_SCHEMA.items():
        raw = _pick(data, (group, group.upper()))
        if not isinstance(raw, dict):
            # RSI 允许受控标量（rsi 单值）
            if group == "rsi":
                scalar = _as_finite_float(_pick(data, ("rsi", "RSI")))
                if scalar is not None:
                    out["rsi"] = scalar
            continue
        normalized: dict[str, Any] = {}
        for field in fields:
            if field in raw and raw[field] is not None:
                number = _as_finite_float(raw[field])
                if number is not None:
                    normalized[field] = number
        if normalized:
            out[group] = normalized
    date = _pick(data, ("date", "trade_date", "indicator_date"))
    valid = _valid_date_str(date)
    if valid:
        out["date"] = valid
    if not out:
        return None, "缺少受控指标字段"
    return out, None


def _norm_item_list(data: Any, fields: tuple[str, ...], capability: str,
                    warnings: list[str], limit: int) -> list[dict[str, Any]] | None:
    rows = data
    if isinstance(data, dict):
        rows = _pick(data, ("items", "list", "records", "data"))
    if not isinstance(rows, list):
        return None
    out: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            return None
        normalized: dict[str, Any] = {}
        for field in fields:
            if field in item and item[field] is not None:
                raw = item[field]
                number = _as_finite_float(raw)
                if number is not None:
                    normalized[field] = number
                else:
                    text = _norm_text(raw, MAX_TITLE if field in ("title",) else MAX_TEXT)
                    if text is not None:
                        normalized[field] = text
        if normalized:
            out.append(normalized)
    if not out:
        return None  # 无任何受控字段 → 结构不可识别
    return _trim_list(out, capability, warnings, limit)


def _norm_intel_items(capability: str, data: Any, warnings: list[str]) -> list[dict[str, Any]] | None:
    fields = _INTEL_ITEM_FIELDS[capability]
    items = _norm_item_list(data, fields, capability, warnings, _MAX_ITEMS[capability])
    if items is None:
        return None
    cleaned: list[dict[str, Any]] = []
    for item in items:
        item["category"] = capability  # 稳定 category
        url = item.pop("url", None) if "url" in item else None
        if url is not None:
            safe = _safe_url(url)
            if safe is not None:
                item["url"] = safe
            else:
                item.pop("url", None)
        cleaned.append(item)
    return cleaned


def _intel_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    """升序排序：合法日期组(0)在前且按日期倒序（负 ordinal）；非法日期组(1)置末尾。
    同日按 category 固定顺序 + 标题。"""
    date = _valid_date_str(item.get("date"))
    title = _norm_text(item.get("title") or "") or ""
    if date is None:
        return (1, 0, 0, title)
    ordinal = datetime.strptime(date, "%Y-%m-%d").toordinal()
    category_order = {cat: i for i, cat in enumerate(_INTEL_CATEGORIES)}
    order = category_order.get(item.get("category"), len(_INTEL_CATEGORIES))
    return (0, -ordinal, order, title)


def _norm_event_items(data: Any, warnings: list[str]) -> tuple[Any, str | None]:
    rows = data
    if isinstance(data, dict):
        rows = _pick(data, ("events", "items", "list"))
    if not isinstance(rows, list):
        return None, "非列表"
    out: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            return None, "行非对象"
        normalized: dict[str, Any] = {}
        for key in ("date", "type", "title", "summary"):
            if key in item and item[key] is not None:
                limit = MAX_TITLE if key == "title" else MAX_TEXT
                normalized[key] = _norm_text(item[key], limit)
        for tag_key in _EVENT_TAGS:
            if tag_key in item and item[tag_key] is not None:
                raw = item[tag_key]
                tags_raw = raw if isinstance(raw, list) else [str(raw)]
                tags = [t for t in (_norm_text(x, MAX_TEXT) for x in tags_raw) if t]
                if tags:
                    normalized["tags"] = tags
                break
        if "date" in normalized or "title" in normalized:
            out.append(normalized)
    return (_trim_list(out, "events", warnings, _MAX_ITEMS["events"]) or None), None


def _norm_risk_items(data: Any, warnings: list[str]) -> tuple[Any, str | None]:
    rows = data
    if isinstance(data, dict):
        rows = _pick(data, ("risks", "items", "list"))
    if not isinstance(rows, list):
        return None, "非列表"
    out: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            return None, "行非对象"
        normalized: dict[str, Any] = {}
        for key, aliases in _RISK_FIELDS:
            raw = _pick(item, aliases)
            if raw is None:
                continue
            number = _as_finite_float(raw)
            normalized[key] = number if number is not None else _norm_text(raw, MAX_TEXT)
        if normalized:
            out.append(normalized)
    return (_trim_list(out, "risk", warnings, _MAX_ITEMS["risk"]) or None), None


# ---------------------------------------------------------------------- #
# 聚合服务
# ---------------------------------------------------------------------- #

class StocksDeepService:
    def __init__(self, project_root: Any):
        self.curated = CuratedStocksService(project_root)

    def _cap(self, capability: str, symbol: str) -> tuple[dict[str, Any] | None, str]:
        if not SYMBOL_RE.fullmatch(symbol):
            raise ValueError("非法 symbol")
        return self.curated._westock_cache(capability, symbol)

    def _capability_meta(self, capability: str, symbol: str) -> dict[str, Any] | None:
        """公开元数据：status/as_of/fetched_at/cache_age_seconds（不含 tool/路径/异常）。"""
        envelope, status = self._cap(capability, symbol)
        if envelope is None:
            return None
        from .stocks_service import _parse_iso_ts
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

    def _envelope(self, symbol: str, status_map: dict[str, str],
                  data: dict[str, Any], warnings: list[str],
                  meta: dict[str, dict[str, Any] | None]) -> dict[str, Any]:
        statuses = list(status_map.values())
        cache_status = (
            "fresh" if "fresh" in statuses
            else "stale" if "stale" in statuses
            else "unavailable"
        )
        # as_of：取能力数据最新合法日期，不用系统时间冒充
        best_as_of: str | None = None
        for capability, m in meta.items():
            if not m:
                continue
            raw = m.get("as_of")
            valid = _valid_date_str(raw)
            if valid and (best_as_of is None or valid > best_as_of):
                best_as_of = valid
        return {
            "schema_version": SCHEMA_VERSION,
            "symbol": symbol,
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

    def _cap_with_norm(self, capability: str, symbol: str, normalize,
                       warnings: list[str],
                       identity_checker=None) -> tuple[Any, str, dict[str, Any] | None]:
        envelope, status = self._cap(capability, symbol)
        meta = self._capability_meta(capability, symbol)
        if envelope is None or status == "unavailable":
            return None, "unavailable", meta
        raw = envelope.get("data")
        if identity_checker is not None:
            conflict = identity_checker(raw)
            if conflict:
                warnings.append(
                    f"{capability} 缓存身份校验失败（{conflict}），已降级为不可用"
                )
                return None, "unavailable", meta
        normalized, reason = normalize(raw)
        if normalized is None:
            warnings.append(
                f"{capability} 缓存结构无法标准化（{reason or '无可识别字段'}），已降级为不可用"
            )
            return None, "unavailable", meta
        return normalized, status, meta

    # ------------------------------------------------------------------ #
    # 1. fundamentals
    # ------------------------------------------------------------------ #

    def fundamentals(self, symbol: str) -> dict[str, Any]:
        status_map: dict[str, str] = {}
        data: dict[str, Any] = {}
        meta: dict[str, dict[str, Any] | None] = {}
        warnings: list[str] = []

        profile, status, m = self._cap_with_norm(
            "profile", symbol, lambda d: (_norm_mapping(d, _PROFILE_FIELDS), None),
            warnings,
            identity_checker=lambda d: _profile_identity_conflict(d, symbol))
        status_map["profile"] = status
        data["profile"] = profile
        meta["profile"] = m

        financials, status, m = self._cap_with_norm(
            "financials", symbol, _norm_financials, warnings)
        status_map["financials"] = status
        data["financials"] = financials
        meta["financials"] = m

        forecast, status, m = self._cap_with_norm(
            "forecast", symbol, lambda d: (_norm_mapping(d, _FORECAST_FIELDS), None), warnings)
        status_map["forecast"] = status
        data["forecast"] = forecast
        meta["forecast"] = m
        return self._envelope(symbol, status_map, data, warnings, meta)

    # ------------------------------------------------------------------ #
    # 2. ownership
    # ------------------------------------------------------------------ #

    def ownership(self, symbol: str) -> dict[str, Any]:
        status_map: dict[str, str] = {}
        data: dict[str, Any] = {}
        meta: dict[str, dict[str, Any] | None] = {}
        warnings: list[str] = []

        shareholders, status, m = self._cap_with_norm(
            "shareholders", symbol,
            lambda d: (_norm_shareholders(d, warnings), None), warnings)
        status_map["shareholders"] = status
        data["shareholders"] = shareholders
        meta["shareholders"] = m

        dividend, status, m = self._cap_with_norm(
            "dividend", symbol, lambda d: (_norm_mapping(d, _DIVIDEND_FIELDS), None), warnings)
        status_map["dividend"] = status
        data["dividend"] = dividend
        meta["dividend"] = m

        buyback, status, m = self._cap_with_norm(
            "buyback", symbol, lambda d: (_norm_mapping(d, _BUYBACK_FIELDS), None), warnings)
        status_map["buyback"] = status
        data["buyback"] = buyback
        meta["buyback"] = m
        return self._envelope(symbol, status_map, data, warnings, meta)

    # ------------------------------------------------------------------ #
    # 3. funds
    # ------------------------------------------------------------------ #

    def funds(self, symbol: str) -> dict[str, Any]:
        status_map: dict[str, str] = {}
        data: dict[str, Any] = {}
        meta: dict[str, dict[str, Any] | None] = {}
        warnings: list[str] = []

        margin, status, m = self._cap_with_norm(
            "margin", symbol, lambda d: (_norm_mapping(d, _MARGIN_FIELDS), None), warnings)
        status_map["margin"] = status
        data["margin"] = margin
        meta["margin"] = m

        block_trade, status, m = self._cap_with_norm(
            "block_trade", symbol,
            lambda d: (_norm_item_list(d, ("date", "price", "shares", "amount", "discount"),
                                        "block_trade", warnings, _MAX_ITEMS["block_trade"]), None),
            warnings)
        status_map["block_trade"] = status
        data["block_trade"] = block_trade
        meta["block_trade"] = m

        fund_flow, status, m = self._cap_with_norm(
            "fund_flow", symbol,
            lambda d: (_norm_mapping(_unwrap_fund_flow(d), _FUND_FLOW_FIELDS), None),
            warnings,
            identity_checker=lambda d: _fund_flow_identity_conflict(d, symbol))
        status_map["fund_flow"] = status
        data["fund_flow"] = fund_flow
        meta["fund_flow"] = m

        northbound, status, m = self._cap_with_norm(
            "northbound", symbol, lambda d: (_norm_mapping(d, _NORTHBOUND_FIELDS), None), warnings)
        status_map["northbound"] = status
        data["northbound"] = northbound
        meta["northbound"] = m

        lhb, status, m = self._cap_with_norm(
            "lhb", symbol,
            lambda d: (_norm_item_list(d, ("date", "reason", "seat", "net_buy", "buy", "sell"),
                                       "lhb", warnings, _MAX_ITEMS["lhb"]), None),
            warnings)
        status_map["lhb"] = status
        data["lhb"] = lhb
        meta["lhb"] = m

        chip, status, m = self._cap_with_norm(
            "chip_distribution", symbol, lambda d: _norm_chip(d, warnings), warnings)
        status_map["chip_distribution"] = status
        data["chip_distribution"] = chip
        meta["chip_distribution"] = m
        return self._envelope(symbol, status_map, data, warnings, meta)

    # ------------------------------------------------------------------ #
    # 4. intel（稳定排序 + 分页）
    # ------------------------------------------------------------------ #

    def intel(self, symbol: str, category: str | None, limit: int, offset: int) -> dict[str, Any]:
        status_map: dict[str, str] = {}
        data: dict[str, Any] = {}
        meta: dict[str, dict[str, Any] | None] = {}
        warnings: list[str] = []
        if category and category not in _INTEL_CATEGORIES:
            raise ValueError(f"非法 category: {category}")

        targets = [category] if category in _INTEL_CATEGORIES else list(_INTEL_CATEGORIES)
        merged: list[dict[str, Any]] = []
        for capability in targets:
            envelope, status = self._cap(capability, symbol)
            status_map[capability] = status
            meta[capability] = self._capability_meta(capability, symbol)
            if envelope is None or status == "unavailable":
                data[capability] = None
                continue
            if capability == "news":
                items, reason = _norm_news_identity_items(
                    envelope.get("data"), warnings, symbol)
                if items is None:
                    status_map[capability] = "unavailable"
                    data[capability] = None
                    if reason in ("structure", "empty"):
                        warnings.append(f"{capability} 缓存结构无法标准化，已降级为不可用")
                    # reason == "identity_all_dropped" 的 warning 已在函数内记录
                    continue
            else:
                items = _norm_intel_items(capability, envelope.get("data"), warnings)
                if items is None:
                    status_map[capability] = "unavailable"
                    data[capability] = None
                    warnings.append(f"{capability} 缓存结构无法标准化，已降级为不可用")
                    continue
            data[capability] = items
            merged.extend(items)

        merged.sort(key=_intel_sort_key)  # 合法日期倒序在前，非法日期置末尾
        total = len(merged)
        page = merged[offset:offset + limit]
        return self._envelope(symbol, status_map, {**data, "items": page, "total": total}, warnings, meta)

    # ------------------------------------------------------------------ #
    # 5. events
    # ------------------------------------------------------------------ #

    def events(self, symbol: str) -> dict[str, Any]:
        status_map: dict[str, str] = {}
        data: dict[str, Any] = {}
        meta: dict[str, dict[str, Any] | None] = {}
        warnings: list[str] = []

        events, status, m = self._cap_with_norm(
            "events", symbol, lambda d: _norm_event_items(d, warnings), warnings)
        status_map["events"] = status
        data["events"] = events
        meta["events"] = m

        risk, status, m = self._cap_with_norm(
            "risk", symbol, lambda d: _norm_risk_items(d, warnings), warnings)
        status_map["risk"] = status
        data["risk"] = risk
        meta["risk"] = m
        if data.get("risk"):
            warnings.append("风险提示来自 Westock 缓存，仅作研究展示，不替代人工判断")
        return self._envelope(symbol, status_map, data, warnings, meta)

    # ------------------------------------------------------------------ #
    # 6. technical
    # ------------------------------------------------------------------ #

    def technical(self, symbol: str) -> dict[str, Any]:
        envelope, status = self._cap("technical", symbol)
        meta = self._capability_meta("technical", symbol)
        data: dict[str, Any] = {"indicators": None}
        warnings: list[str] = []
        if envelope is not None and status != "unavailable":
            indicators, reason = _norm_technical(envelope.get("data"))
            if indicators is None:
                status = "unavailable"
                warnings.append(f"technical 缓存结构无法标准化（{reason or '未知结构'}）")
            else:
                data["indicators"] = indicators
        status_map = {"technical": status}
        if data["indicators"] is not None:
            data["note"] = "技术指标来自 Westock 缓存，仅作展示；不写入本地 K 线、策略或回测"
        return self._envelope(symbol, status_map, data, warnings, {"technical": meta})


def _norm_shareholders(data: Any, warnings: list[str]) -> dict[str, Any] | None:
    """股东：holder_count 标量 + 股本结构强制 schema + 主要股东强制 schema（≤20）。"""
    if not isinstance(data, dict):
        return None
    out: dict[str, Any] = {}
    holder_count = _pick(data, ("holder_count", "total_holders"))
    if holder_count is not None:
        number = _as_finite_float(holder_count)
        if number is not None:
            out["holder_count"] = number
    change = _pick(data, ("holder_count_change", "holder_change"))
    if change is not None:
        number = _as_finite_float(change)
        if number is not None:
            out["holder_count_change"] = number
    structure = _norm_share_structure(data)
    if structure:
        out["share_structure"] = structure
    shareholders = _norm_major_shareholders(data, warnings)
    if shareholders:
        out["major_shareholders"] = shareholders
    return out or None


def build_stocks_deep_service(project_root: Any) -> StocksDeepService:
    return StocksDeepService(project_root)


__all__ = [
    "SCHEMA_VERSION",
    "StocksDeepService",
    "build_stocks_deep_service",
    "MAX_TEXT",
    "MAX_TITLE",
    "MAX_URL",
    "_INTEL_CATEGORIES",
    "_norm_mapping",
    "_norm_financials",
    "_norm_technical",
    "_norm_chip",
    "_safe_url",
    "_valid_date_str",
]
