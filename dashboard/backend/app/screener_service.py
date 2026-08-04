"""Phase E: 选股中心（Westock cache-export 只读研究工作台）— Codex 第一轮修正版。

核心修正：
1. 查询真实性：canonical query SHA-256 → query-specific scope（q_<hash>），
   只读精确匹配缓存；找不到 → unavailable + 明确 warning，禁止用全局缓存冒充。
2. Universe 严格隔离：local=curated 集合、index/sector/industry_chain 精确提取，
   交叉泄漏测试证明；local_history_available 一律由 curated 计算。
3. 受控请求：mode 真正互斥；forbidden key 递归检查；condition 逐字段类型 schema；
   精确错误码 invalid_mode/invalid_universe/invalid_condition/invalid_strategy/
   invalid_factor/invalid_label（forbidden 仍 invalid_request）。
4. 结果：matched_*/factor_values 固定 schema；同 symbol 去重保留排序最优；
   缺失值 asc/desc 均排末尾；禁 NaN/Infinity/嵌套透传。
5. 并发去重：query_hash 级 Event 同步——owner 执行，等待者阻塞取同一结果，
   owner 失败传播一致错误并清理。
6. 存储：2MiB result 上限；saved/candidates 文件大小上限；result_id 碰撞重生；
   read 完整 schema 校验 fail-open；os.replace 失败保留旧文件并清理临时文件。
7. saved/candidates：name 从结果行派生；note 拒绝控制字符/script/超长；
   ID 严格 32 位小写 hex；saved 加载后完整重校验。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .stocks_deep_service import MAX_TEXT, _norm_text
from .stocks_service import (CuratedStocksService, SYMBOL_RE, _as_finite_float,
                             _parse_iso_ts, westock_code_to_symbol)

SCHEMA_VERSION = 1
MAX_RESULTS = 500
MAX_RESULT_FILE_BYTES = 2 * 1024 * 1024  # 2MiB
MAX_STATE_FILE_BYTES = 2 * 1024 * 1024  # saved/candidates 文件上限
MAX_SAVED = 100
MAX_CANDIDATES = 500
MAX_RESULT_ROWS = 200
MAX_CONDITIONS = 20
MAX_LIMIT = 200
MAX_BODY_BYTES = 64 * 1024
RATE_LIMIT_PER_SECOND = 2
_ID_RE = re.compile(r"^[0-9a-f]{32}$")

_MODES = ("condition", "strategy", "factor", "label")
_FORBIDDEN_FIELDS = (
    "capability", "tool", "expression", "formula", "script", "code",
    "command", "path", "filename", "raw_params", "mcp_params",
)
# condition 逐字段类型 schema：numeric 数值 / boolean 布尔 / enum 枚举
# boll_position 无稳定口径 → 从可选字段移除（不能猜测），in 仅 enum 字段可用。
_NUMERIC_FIELDS = (
    "price", "change_percent", "volume", "amount", "turnover_rate", "volume_ratio",
    "market_cap", "float_market_cap", "pe", "pb", "ps", "dividend_yield", "roe",
    "revenue_growth", "profit_growth", "debt_ratio", "operating_cash_flow",
    "rsi", "kdj_k", "main_fund_flow", "northbound_change", "margin_change",
)
_BOOLEAN_FIELDS = ("ma5_above_ma20",)
_MACD_SIGNAL_ENUM = ("golden_cross", "death_cross", "bullish", "bearish", "neutral")
_ENUM_FIELDS: dict[str, tuple[str, ...]] = {"macd_signal": _MACD_SIGNAL_ENUM}
_CONDITION_FIELDS = _NUMERIC_FIELDS + _BOOLEAN_FIELDS + tuple(_ENUM_FIELDS)
_NUMERIC_OPERATORS = ("gt", "gte", "lt", "lte", "eq", "between")
_BOOLEAN_OPERATORS = ("eq",)
_STRATEGY_NAMES = (
    "ma_breakout", "macd_golden_cross", "rsi_oversold", "boll_breakout",
    "volume_breakout", "trend_strength", "value_quality", "growth_quality",
)
_STRATEGY_PARAMS: dict[str, tuple[str, ...]] = {
    "ma_breakout": ("lookback_days",),
    "macd_golden_cross": ("lookback_days",),
    "rsi_oversold": ("rsi_limit",),
    "boll_breakout": ("lookback_days",),
    "volume_breakout": ("min_volume_ratio", "lookback_days"),
    "trend_strength": ("lookback_days", "threshold"),
    "value_quality": ("threshold",),
    "growth_quality": ("threshold",),
}
_FACTOR_NAMES = ("value", "quality", "growth", "momentum", "volatility",
                 "liquidity", "size", "dividend", "composite")
_FACTOR_WEIGHT_KEYS = ("value", "quality", "growth", "momentum", "volatility",
                       "liquidity", "size", "dividend")
_LABEL_VALUES = (
    "high_dividend", "low_valuation", "institutional_focus", "northbound_heavy",
    "active_fund_flow", "earnings_growth", "buyback", "dividend_plan",
    "risk_warning", "hot_stock", "sector_leader",
)
_LABEL_MATCH = ("any", "all")
_UNIVERSE_TYPES = ("local", "index", "sector", "industry_chain")
_SORT_FIELDS = ("score", "rank", "price", "change_percent", "symbol")
_SORT_DIRECTIONS = ("asc", "desc")
_RESULT_FIELDS = (
    "symbol", "name", "score", "rank", "price", "change_percent", "industry",
    "sector", "reason", "matched_conditions", "matched_labels", "factor_values",
    "local_history_available",
)
_NUMERIC_RESULT_FIELDS = ("score", "rank", "price", "change_percent")
_MODE_CAPABILITY = {
    "condition": "filter",
    "strategy": "strategy_select",
    "factor": "factor_ranking",
    "label": "label_select",
}
_MISSING_CACHE_WARNING = "当前没有与该筛选条件精确匹配的 Westock 缓存导出，未执行实时查询。"


class ScreenerError(Exception):
    def __init__(self, code: str, message: str, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


_STRICT_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _strict_date(value: Any) -> str | None:
    """screener 专用严格日期：必须为字符串且严格匹配 ^YYYY-MM-DD$，strptime 完整解析。
    拒绝任何前缀/后缀/宽松格式（如 2026-08-04-secret、2026-8-4、2026-02-30）。
    不使用宽松的 _valid_date_str（会接受 2026-08-04-secret）。"""
    if not isinstance(value, str):
        return None
    if not _STRICT_DATE_RE.fullmatch(value):
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    return value


def _strict_aware_datetime(value: Any):
    """screener 专用严格带时区时间：允许 ISO 8601 与末尾 Z；解析后 tzinfo 与 utcoffset()
    必须非 None。禁止给 naive datetime 自动补 UTC（与 _parse_iso_ts 的宽松行为区分）。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _reject_forbidden_recursive(value: Any, path: str = "") -> None:
    """递归检查所有 dict/list 层级的 forbidden 键。"""
    if isinstance(value, dict):
        for key, child in value.items():
            if key in _FORBIDDEN_FIELDS:
                raise ScreenerError("invalid_request", f"禁止字段: {path + key}")
            _reject_forbidden_recursive(child, f"{path}{key}.")
    elif isinstance(value, list):
        for i, child in enumerate(value):
            _reject_forbidden_recursive(child, f"{path}[{i}].")


def _validate_finite(value: Any, name: str) -> float:
    number = _as_finite_float(value)
    if number is None:
        raise ScreenerError("invalid_condition", f"{name} 必须是有限数值")
    return number


def _validate_condition(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ScreenerError("invalid_condition", "condition 必须为对象")
    field = item.get("field")
    if field not in _CONDITION_FIELDS:
        raise ScreenerError("invalid_condition", f"非法 condition.field: {field}")
    operator = item.get("operator")
    value = item.get("value")
    if field in _BOOLEAN_FIELDS:
        if operator not in _BOOLEAN_OPERATORS:
            raise ScreenerError("invalid_condition", f"布尔字段 {field} 仅允许 eq")
        if not isinstance(value, bool):
            raise ScreenerError("invalid_condition", f"布尔字段 {field} 必须为布尔值")
        return {"field": field, "operator": "eq", "value": bool(value)}
    if field in _ENUM_FIELDS:
        enum = _ENUM_FIELDS[field]
        if operator not in ("eq", "in"):
            raise ScreenerError("invalid_condition", f"枚举字段 {field} 仅允许 eq/in")
        if operator == "eq":
            if value not in enum:
                raise ScreenerError("invalid_condition", f"{field} 必须为受控枚举值")
            return {"field": field, "operator": "eq", "value": str(value)}
        # in：仅枚举字段可用；值必须为枚举子集、非空、不重复、≤20
        if not isinstance(value, list) or not value or len(value) > 20:
            raise ScreenerError("invalid_condition", f"{field} in 必须为 1–20 个枚举值")
        if len(set(value)) != len(value):
            raise ScreenerError("invalid_condition", f"{field} in 不允许重复")
        for v in value:
            if v not in enum:
                raise ScreenerError("invalid_condition", f"{field} in 值必须为受控枚举")
        return {"field": field, "operator": "in", "value": [str(v) for v in value]}
    # 数值字段
    if operator not in _NUMERIC_OPERATORS:
        raise ScreenerError("invalid_condition", f"数值字段 {field} 不允许操作符 {operator}")
    if operator == "between":
        if not isinstance(value, list) or len(value) != 2:
            raise ScreenerError("invalid_condition", "between 必须恰好 2 个 finite 数值")
        lo = _validate_finite(value[0], "between[0]")
        hi = _validate_finite(value[1], "between[1]")
        if lo > hi:
            raise ScreenerError("invalid_condition", "between 前值必须 ≤ 后值")
        return {"field": field, "operator": "between", "value": [lo, hi]}
    number = _validate_finite(value, "condition.value")
    return {"field": field, "operator": operator, "value": number}


def _validate_strategy(body: dict[str, Any]) -> dict[str, Any]:
    strategy = body.get("strategy")
    if not isinstance(strategy, dict):
        raise ScreenerError("invalid_strategy", "strategy 必须为对象")
    name = strategy.get("name")
    if name not in _STRATEGY_NAMES:
        raise ScreenerError("invalid_strategy", f"非法 strategy.name: {name}")
    allowed = _STRATEGY_PARAMS[name]
    out: dict[str, Any] = {"name": name}
    for key, value in strategy.items():
        if key == "name":
            continue
        if key not in allowed:
            raise ScreenerError("invalid_strategy", f"strategy {name} 不允许参数: {key}")
        if key == "lookback_days":
            n = _validate_finite(value, "lookback_days")
            if n < 1 or n > 250 or int(n) != n:
                raise ScreenerError("invalid_strategy", "lookback_days 必须为 1–250 整数")
            out[key] = int(n)
        elif key == "threshold":
            n = _validate_finite(value, "threshold")
            if n < -100 or n > 100:
                raise ScreenerError("invalid_strategy", "threshold 必须为 -100–100")
            out[key] = n
        elif key == "min_volume_ratio":
            n = _validate_finite(value, "min_volume_ratio")
            if n < 0 or n > 100:
                raise ScreenerError("invalid_strategy", "min_volume_ratio 必须为 0–100")
            out[key] = n
        elif key == "rsi_limit":
            n = _validate_finite(value, "rsi_limit")
            if n < 0 or n > 100:
                raise ScreenerError("invalid_strategy", "rsi_limit 必须为 0–100")
            out[key] = n
    return out


def _validate_factor(body: dict[str, Any]) -> dict[str, Any]:
    factor = body.get("factor")
    if not isinstance(factor, dict):
        raise ScreenerError("invalid_factor", "factor 必须为对象")
    name = factor.get("name")
    if name not in _FACTOR_NAMES:
        raise ScreenerError("invalid_factor", f"非法 factor.name: {name}")
    out: dict[str, Any] = {"name": name}
    for key, value in factor.items():
        if key == "name":
            continue
        if key == "top_n":
            n = _validate_finite(value, "top_n")
            if n < 1 or n > 200 or int(n) != n:
                raise ScreenerError("invalid_factor", "top_n 必须为 1–200 整数")
            out[key] = int(n)
        elif key == "ascending":
            if not isinstance(value, bool):
                raise ScreenerError("invalid_factor", "ascending 必须为布尔")
            out[key] = value
        elif key == "weights":
            if name != "composite":
                raise ScreenerError("invalid_factor", "weights 仅 composite 允许")
            if not isinstance(value, dict) or not value:
                raise ScreenerError("invalid_factor", "weights 必须为非空对象")
            total = 0.0
            weights: dict[str, float] = {}
            for wkey, wval in value.items():
                if wkey not in _FACTOR_WEIGHT_KEYS:
                    raise ScreenerError("invalid_factor", f"非法 weights 键: {wkey}")
                n = _validate_finite(wval, f"weights.{wkey}")
                if n < 0:
                    raise ScreenerError("invalid_factor", "weights 必须非负")
                weights[wkey] = n
                total += n
            if total <= 0:
                raise ScreenerError("invalid_factor", "weights 总和必须 > 0")
            out[key] = weights
        else:
            raise ScreenerError("invalid_factor", f"factor 不允许参数: {key}")
    return out


def _validate_labels(body: dict[str, Any]) -> dict[str, Any]:
    labels = body.get("labels")
    if not isinstance(labels, dict):
        raise ScreenerError("invalid_label", "labels 必须为对象")
    values = labels.get("values")
    if not isinstance(values, list) or not values or len(values) > 10:
        raise ScreenerError("invalid_label", "labels.values 最多 10 个且非空")
    if len(set(values)) != len(values):
        raise ScreenerError("invalid_label", "labels.values 不允许重复")
    for v in values:
        if v not in _LABEL_VALUES:
            raise ScreenerError("invalid_label", f"非法 label: {v}")
    match = labels.get("match", "any")
    if match not in _LABEL_MATCH:
        raise ScreenerError("invalid_label", "labels.match 仅 any/all")
    return {"values": list(values), "match": match}


def _validate_sort(body: dict[str, Any]) -> dict[str, str]:
    sort = body.get("sort")
    if sort is None:
        return {"field": "score", "direction": "desc"}
    if not isinstance(sort, dict):
        raise ScreenerError("invalid_request", "sort 必须为对象")
    field = sort.get("field", "score")
    direction = sort.get("direction", "desc")
    if field not in _SORT_FIELDS:
        raise ScreenerError("invalid_request", f"非法 sort.field: {field}")
    if direction not in _SORT_DIRECTIONS:
        raise ScreenerError("invalid_request", f"非法 sort.direction: {direction}")
    return {"field": field, "direction": direction}


def _validate_query(body: Any) -> dict[str, Any]:
    """完整校验 run query（不执行）；saved 保存时复用。"""
    _reject_forbidden_recursive(body)
    if not isinstance(body, dict):
        raise ScreenerError("invalid_request", "请求必须为对象")
    mode = body.get("mode")
    if mode not in _MODES:
        raise ScreenerError("invalid_mode", f"mode 必须为 {'/'.join(_MODES)}")

    # mode 真正互斥：非当前模式对应字段只要不是 null 或不存在就拒绝（空数组/空对象也算携带）
    _MODE_FIELDS: dict[str, str] = {
        "condition": "conditions", "strategy": "strategy", "factor": "factor", "label": "labels",
    }
    for other_mode, field in _MODE_FIELDS.items():
        if mode != other_mode and field in body and body.get(field) is not None:
            raise ScreenerError("invalid_mode", f"mode={mode} 不允许 {field}")

    universe = body.get("universe") or {}
    if not isinstance(universe, dict):
        raise ScreenerError("invalid_universe", "universe 必须为对象")
    universe_type = universe.get("type", "local")
    if universe_type not in _UNIVERSE_TYPES:
        raise ScreenerError("invalid_universe", f"非法 universe.type: {universe_type}")
    universe_value = universe.get("value")
    if universe_type == "local" and universe_value is not None:
        raise ScreenerError("invalid_universe", "local universe 的 value 必须为 null")
    if universe_type in ("index", "sector", "industry_chain") and not isinstance(universe_value, str):
        raise ScreenerError("invalid_universe", f"{universe_type} universe 需要受控代码 value")

    conditions: list[dict[str, Any]] = []
    strategy: dict[str, Any] | None = None
    factor: dict[str, Any] | None = None
    labels: dict[str, Any] | None = None
    if mode == "condition":
        raw = body.get("conditions") or []
        if not isinstance(raw, list) or len(raw) > MAX_CONDITIONS:
            raise ScreenerError("invalid_condition", f"conditions 最多 {MAX_CONDITIONS} 条")
        for item in raw:
            conditions.append(_validate_condition(item))
    elif mode == "strategy":
        strategy = _validate_strategy(body)
    elif mode == "factor":
        factor = _validate_factor(body)
    elif mode == "label":
        labels = _validate_labels(body)

    sort = _validate_sort(body)
    limit_raw = body.get("limit", 50)
    limit = _as_finite_float(limit_raw)
    if limit is None or limit < 1 or limit > MAX_LIMIT or int(limit) != limit:
        raise ScreenerError("invalid_request", f"limit 必须为 1–{MAX_LIMIT}")
    return {
        "mode": mode,
        "universe": {"type": universe_type, "value": universe_value},
        "conditions": conditions,
        "strategy": strategy,
        "factor": factor,
        "labels": labels,
        "sort": sort,
        "limit": int(limit),
    }


def canonical_query_hash(query: dict[str, Any]) -> str:
    """稳定序列化 + SHA-256 → query-specific scope（q_<64hex>）。"""
    canonical = json.dumps(query, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "q_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------- #
# 状态存储
# ---------------------------------------------------------------------- #

class ScreenerStore:
    def __init__(self, project_root: Any):
        self.dir = Path(project_root) / "state" / "dashboard" / "screener"
        self.results_dir = self.dir / "results"
        self.saved_path = self.dir / "saved-filters.json"
        self.candidates_path = self.dir / "candidates.json"

    def ensure_dirs(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)

    def _atomic_write(self, path: Path, payload: Any) -> None:
        """临时文件 + fsync + os.replace 原子写；replace 失败保留旧文件并清理临时文件。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".tmp.", suffix=".json", dir=path.parent)
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except Exception:
            if tmp.exists():
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            raise

    def _read_json(self, path: Path) -> Any:
        try:
            raw = path.read_text(encoding="utf-8")
            if len(raw.encode("utf-8")) > MAX_STATE_FILE_BYTES:
                return None  # 超限 fail-open
            return json.loads(raw)
        except Exception:
            return None

    # ---- results ----
    def write_result(self, result_id: str, payload: Any) -> None:
        self.ensure_dirs()
        if not _ID_RE.fullmatch(result_id):
            raise ScreenerError("invalid_result_id", "非法 result_id")
        path = self.results_dir / f"{result_id}.json"
        if path.exists():
            raise ScreenerError("result_collision", "result_id 冲突，请重试", status_code=409)
        blob = json.dumps(payload, ensure_ascii=False)
        if len(blob.encode("utf-8")) > MAX_RESULT_FILE_BYTES:
            raise ScreenerError("result_too_large", f"结果超过 {MAX_RESULT_FILE_BYTES // 1024 // 1024}MiB 上限", status_code=413)
        self._atomic_write(path, payload)
        files = sorted(self.results_dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for old in files[:-MAX_RESULTS]:
            try:
                old.unlink()
            except OSError:
                pass

    def read_result(self, result_id: str) -> dict[str, Any] | None:
        if not _ID_RE.fullmatch(result_id):
            return None
        payload = self._read_json(self.results_dir / f"{result_id}.json")
        return payload if _valid_result_payload(payload, result_id) else None

    # ---- saved ----
    def load_saved(self) -> list[dict[str, Any]]:
        data = self._read_json(self.saved_path)
        if not isinstance(data, dict) or set(data) != {"schema_version", "items"}:
            return []
        if data.get("schema_version") != SCHEMA_VERSION:
            return []
        items = data.get("items")
        if not isinstance(items, list):
            return []
        valid = []
        for item in items:
            if _valid_saved_item(item):
                valid.append(item)
        return valid

    def save_saved(self, items: list[dict[str, Any]]) -> None:
        self.ensure_dirs()
        blob = json.dumps({"schema_version": SCHEMA_VERSION, "items": items}, ensure_ascii=False)
        if len(blob.encode("utf-8")) > MAX_STATE_FILE_BYTES:
            raise ScreenerError("state_too_large", "保存文件超过大小上限", status_code=413)
        self._atomic_write(self.saved_path, {"schema_version": SCHEMA_VERSION, "items": items})

    # ---- candidates ----
    def load_candidates(self) -> list[dict[str, Any]]:
        data = self._read_json(self.candidates_path)
        if not isinstance(data, dict) or set(data) != {"schema_version", "items"}:
            return []
        if data.get("schema_version") != SCHEMA_VERSION:
            return []
        items = data.get("items")
        if not isinstance(items, list):
            return []
        valid = []
        for item in items:
            if _valid_candidate_item(item):
                valid.append(item)
        return valid

    def save_candidates(self, items: list[dict[str, Any]]) -> None:
        self.ensure_dirs()
        blob = json.dumps({"schema_version": SCHEMA_VERSION, "items": items}, ensure_ascii=False)
        if len(blob.encode("utf-8")) > MAX_STATE_FILE_BYTES:
            raise ScreenerError("state_too_large", "候选文件超过大小上限", status_code=413)
        self._atomic_write(self.candidates_path, {"schema_version": SCHEMA_VERSION, "items": items})


def _valid_result_row(item: Any) -> bool:
    """结果行严格校验（与 _norm_row 等价）：白名单字段、symbol 合法、
    数值 finite、嵌套对象一律拒绝。"""
    if not isinstance(item, dict):
        return False
    if any(k not in _RESULT_FIELDS for k in item):
        return False
    symbol = item.get("symbol")
    if not isinstance(symbol, str) or not SYMBOL_RE.fullmatch(symbol):
        return False
    for f in _NUMERIC_RESULT_FIELDS:
        if f in item:
            v = item[f]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                return False
    if "reason" in item and (not isinstance(item["reason"], str) or len(item["reason"]) > 400):
        return False
    if "matched_conditions" in item:
        mc = item["matched_conditions"]
        if not isinstance(mc, list) or len(mc) > 20:
            return False
        if not all(isinstance(x, str) and 0 < len(x) <= 200 for x in mc):
            return False
    if "matched_labels" in item:
        ml = item["matched_labels"]
        if not isinstance(ml, list) or len(ml) > 10 or len(set(ml)) != len(ml):
            return False
        if not all(x in _LABEL_VALUES for x in ml):
            return False
    if "factor_values" in item:
        fv = item["factor_values"]
        if not isinstance(fv, dict) or not fv:
            return False
        if any(k not in _FACTOR_WEIGHT_KEYS and k not in _FACTOR_NAMES for k in fv):
            return False
        for v in fv.values():
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                return False
    for f in ("name", "industry", "sector"):
        if f in item and (not isinstance(item[f], str) or not item[f] or len(item[f]) > MAX_TEXT):
            return False
    if "local_history_available" in item and not isinstance(item["local_history_available"], bool):
        return False
    return True


def _valid_result_payload(payload: Any, expected_id: str | None = None) -> bool:
    """result 快照严格 schema：精确顶层字段集合、ID 与文件名一致且 32 位小写 hex、
    query 重新通过完整校验且与存储内容一致、items 严格校验、固定 source/transport/is_realtime/
    cache_status/warnings/cache_scope。合法 JSON 但任意字段或错误类型 → fail-open 丢弃。"""
    if not isinstance(payload, dict):
        return False
    allowed = {
        "schema_version", "result_id", "mode", "source", "as_of", "source_fetched_at",
        "generated_at", "cache_status", "is_realtime", "transport", "availability",
        "query", "data", "warnings", "cache_scope",
    }
    if set(payload) != allowed:
        return False
    if payload.get("schema_version") != SCHEMA_VERSION:
        return False
    result_id = payload.get("result_id")
    if not isinstance(result_id, str) or not _ID_RE.fullmatch(result_id):
        return False
    if expected_id is not None and result_id != expected_id:
        return False
    mode = payload.get("mode")
    if mode not in _MODES:
        return False
    if payload.get("source") != "westock-mcp":
        return False
    if payload.get("transport") != "cache_export":
        return False
    if payload.get("is_realtime") is not False:
        return False
    cache_status = payload.get("cache_status")
    if cache_status not in ("fresh", "stale", "unavailable"):
        return False
    query = payload.get("query")
    if not isinstance(query, dict):
        return False
    if query.get("mode") != mode:  # mode 必须等于 query.mode
        return False
    try:
        validated = _validate_query(query)
    except ScreenerError:
        return False
    if validated != query:  # 标准化结果必须等于存储内容
        return False
    # as_of：仅 null 或严格 YYYY-MM-DD（_strict_date，拒绝后缀/前缀/宽松格式）
    as_of = payload.get("as_of")
    if as_of is not None and _strict_date(as_of) is None:
        return False
    # generated_at 必须为合法带时区 ISO 时间（_strict_aware_datetime，禁 naive 补 UTC）
    generated = payload.get("generated_at")
    if _strict_aware_datetime(generated) is None:
        return False
    # source_fetched_at：仅 null 或合法带时区 ISO 时间
    source_fetched_at = payload.get("source_fetched_at")
    if source_fetched_at is not None and _strict_aware_datetime(source_fetched_at) is None:
        return False
    # fresh/stale 必须有抓取时间；unavailable 允许 null
    if cache_status in ("fresh", "stale") and source_fetched_at is None:
        return False
    data = payload.get("data")
    if not isinstance(data, dict) or set(data) != {"items", "total"}:
        return False
    items = data.get("items")
    if not isinstance(items, list) or data.get("total") != len(items):
        return False
    if len(items) > MAX_RESULT_ROWS:
        return False
    if not all(_valid_result_row(i) for i in items):
        return False
    warnings = payload.get("warnings")
    if not isinstance(warnings, list) or not all(isinstance(w, str) and 0 < len(w) <= 400 for w in warnings):
        return False
    availability = payload.get("availability")
    # availability 必须恰好只有当前 mode 对应 capability 一个键，且值等于 cache_status
    capability = _MODE_CAPABILITY.get(mode)
    if not isinstance(availability, dict) or set(availability) != {capability}:
        return False
    if availability.get(capability) != cache_status:
        return False
    scope = payload.get("cache_scope")
    if not isinstance(scope, str) or not re.fullmatch(r"q_[0-9a-f]{64}", scope):
        return False
    if scope != canonical_query_hash(query):  # cache_scope 必须与存储 query 一致
        return False
    return True


def _valid_saved_item(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    if set(item) != {"id", "name", "query", "created_at", "updated_at"}:
        return False
    if not isinstance(item.get("id"), str) or not _ID_RE.fullmatch(item.get("id") or ""):
        return False
    name = item.get("name")
    if not isinstance(name, str) or not (1 <= len(name) <= 80):
        return False
    if any(ord(ch) < 32 for ch in name) or "script" in name.lower():
        return False
    for ts in ("created_at", "updated_at"):
        value = item.get(ts)
        if _strict_aware_datetime(value) is None:  # 必须严格带时区 ISO，禁 naive 补 UTC
            return False
    query = item.get("query")
    if not isinstance(query, dict):
        return False
    try:
        validated = _validate_query(query)
    except ScreenerError:
        return False
    return validated == query


def _valid_candidate_item(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    if set(item) != {"symbol", "name", "source_result_id", "note", "added_at", "local_history_available"}:
        return False
    symbol = item.get("symbol")
    if not isinstance(symbol, str) or not SYMBOL_RE.fullmatch(symbol):
        return False
    rid = item.get("source_result_id")
    if not isinstance(rid, str) or not _ID_RE.fullmatch(rid):
        return False
    name = item.get("name")
    if not isinstance(name, str) or not (1 <= len(name) <= MAX_TEXT):
        return False
    if any(ord(ch) < 32 for ch in name) or "script" in name.lower():
        return False
    note = item.get("note")
    if not isinstance(note, str) or len(note) > 400:
        return False
    if any(ord(ch) < 32 for ch in note) or "script" in note.lower():
        return False
    added_at = item.get("added_at")
    if _strict_aware_datetime(added_at) is None:  # 必须严格带时区 ISO，禁 naive 补 UTC
        return False
    if not isinstance(item.get("local_history_available"), bool):
        return False
    return True


# ---------------------------------------------------------------------- #
# 服务
# ---------------------------------------------------------------------- #

class _Flight:
    """单一代际的查询在途对象：waiter 持有具体 Flight 引用（不通过 qhash 查找另一代状态），
    清理 map 时必须确认身份（self._in_flight.get(qhash) is flight）。"""

    __slots__ = ("event", "waiter_count", "result", "error", "completed")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.waiter_count = 0
        self.result: dict[str, Any] | None = None
        self.error: ScreenerError | None = None
        self.completed = False


class ScreenerService:
    def __init__(self, project_root: Any, clock: Callable[[], float] | None = None):
        self.curated = CuratedStocksService(project_root)
        self.store = ScreenerStore(project_root)
        self._clock = clock or time.monotonic
        self._run_times: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        # query_hash → Flight（owner 执行，等待者阻塞取同一结果；完成后不永久驻留）
        self._in_flight: dict[str, _Flight] = {}

    def _check_rate_limit(self, session_key: str) -> None:
        now = self._clock()
        with self._lock:
            times = [t for t in self._run_times.get(session_key, []) if now - t < 1.0]
            if len(times) >= RATE_LIMIT_PER_SECOND:
                raise ScreenerError("rate_limited", "请求过于频繁，请稍后再试", status_code=429)
            times.append(now)
            self._run_times[session_key] = times

    # ---- run（并发去重：owner/waiter，Flight 代际隔离） ----
    def run(self, session_key: str, body: Any) -> dict[str, Any]:
        query = _validate_query(body)
        capability = _MODE_CAPABILITY[query["mode"]]
        qhash = canonical_query_hash(query)

        # 相同 canonical query 的在途请求先参与去重（waiter 不限流）
        with self._lock:
            flight = self._in_flight.get(qhash)
            if flight is None:
                flight = _Flight()
                self._in_flight[qhash] = flight
                owner = True
            else:
                owner = False
                flight.waiter_count += 1

        if not owner:
            # waiter：持有具体 Flight 引用，等待 owner 完成；不重复执行、不计入执行限流
            if not flight.event.wait(timeout=30):
                with self._lock:
                    self._release_waiter(qhash, flight)
                raise ScreenerError("concurrent_error", "并发选股超时", status_code=500)
            with self._lock:
                if flight.result is not None:
                    payload = flight.result
                    self._release_waiter(qhash, flight)
                    return payload
                if flight.error is not None:
                    error = flight.error
                    self._release_waiter(qhash, flight)
                    raise error
            raise ScreenerError("concurrent_error", "并发选股超时", status_code=500)

        # owner：只有 owner 计入执行限流；waiter 不重复占用执行配额。
        # 限流检查必须在 try 内：抛错时 finally 清理事件并唤醒等待者。
        try:
            self._check_rate_limit(session_key)
            payload = self._do_run(query, capability)
            with self._lock:
                flight.result = payload  # 先存，再唤醒等待者
        except Exception as exc:
            error = exc if isinstance(exc, ScreenerError) else ScreenerError("internal_error", str(exc), 500)
            with self._lock:
                flight.error = error
            raise error
        finally:
            with self._lock:
                flight.completed = True
                if flight.waiter_count <= 0:
                    if self._in_flight.get(qhash) is flight:
                        self._in_flight.pop(qhash, None)
                flight.event.set()
        return payload

    def _release_waiter(self, qhash: str, flight: _Flight) -> None:
        """waiter 消费/超时后释放所属 Flight 的槽位；仅当该 Flight 仍登记在 map 中才清理，
        绝不影响其他代际（get(qhash) is flight 身份确认）。"""
        flight.waiter_count -= 1
        if flight.waiter_count <= 0 and flight.completed:
            if self._in_flight.get(qhash) is flight:
                self._in_flight.pop(qhash, None)

    def _do_run(self, query: dict[str, Any], capability: str) -> dict[str, Any]:
        """执行选股并原子保存结果快照。fresh/stale/unavailable 三种结果都必须落盘，
        POST /api/screener/run 返回的 result_id 必须能立即被 GET 读取。"""
        qhash = canonical_query_hash(query)
        envelope, status = self._cap(capability, qhash)  # 只读精确匹配缓存
        warnings = list(envelope.get("warnings") or []) if envelope else []
        if envelope is None or status == "unavailable":
            warnings.append(_MISSING_CACHE_WARNING)
            return self._persist_result({
                "schema_version": SCHEMA_VERSION,
                "mode": query["mode"],
                "source": "westock-mcp",
                "as_of": None,
                "source_fetched_at": None,
                "generated_at": _now_iso(),
                "cache_status": "unavailable",
                "is_realtime": False,
                "transport": "cache_export",
                "availability": {capability: "unavailable"},
                "query": query,
                "data": {"items": [], "total": 0},
                "warnings": warnings,
                "cache_scope": qhash,
            })
        local_available = self._local_symbol_set()
        raw_rows = self._extract_rows(envelope.get("data"))
        if raw_rows is None:
            warnings.append(f"{capability} 缓存结构无法标准化，已降级为不可用")
            return self._persist_result({
                "schema_version": SCHEMA_VERSION,
                "mode": query["mode"],
                "source": "westock-mcp",
                "as_of": envelope.get("as_of"),
                "source_fetched_at": envelope.get("fetched_at"),
                "generated_at": _now_iso(),
                "cache_status": "unavailable",
                "is_realtime": False,
                "transport": "cache_export",
                "availability": {capability: "unavailable"},
                "query": query,
                "data": {"items": [], "total": 0},
                "warnings": warnings,
                "cache_scope": qhash,
            })
        universe_symbols = self._resolve_universe(query["universe"])  # invalid_universe 即抛
        rows: list[dict[str, Any]] = []
        for raw in raw_rows:
            row = self._norm_row(self._remap_westock_row(raw), local_available)
            if row is None:
                continue
            if universe_symbols is not None and row.get("symbol") not in universe_symbols:
                continue
            rows.append(row)
        rows = self._dedupe_and_sort(rows, query["sort"])
        rows = rows[:query["limit"]]
        return self._persist_result({
            "schema_version": SCHEMA_VERSION,
            "mode": query["mode"],
            "source": "westock-mcp",
            "as_of": envelope.get("as_of"),
            "source_fetched_at": envelope.get("fetched_at"),
            "generated_at": _now_iso(),
            "cache_status": status,
            "is_realtime": False,
            "transport": "cache_export",
            "availability": {capability: status},
            "query": query,
            "data": {"items": rows, "total": len(rows)},
            "warnings": warnings,
            "cache_scope": qhash,
        })

    def _persist_result(self, payload: dict[str, Any]) -> dict[str, Any]:
        """原子保存结果快照。每次生成新 ID 都同步更新 payload["result_id"]；
        确认写入成功后才 return；达到最大重试次数仍碰撞必须抛 result_collision。"""
        for _ in range(3):
            result_id = secrets.token_hex(16)
            payload["result_id"] = result_id
            try:
                self.store.write_result(result_id, payload)
                return payload
            except ScreenerError as exc:
                if exc.code != "result_collision":
                    raise
        raise ScreenerError("result_collision", "结果 ID 冲突，请重试", status_code=409)

    def _cap(self, capability: str, scope: str):
        """只读指定 scope 缓存；全局/其他 scope 一律视为不匹配。"""
        envelope = self.curated.westock_store.read(capability, scope)
        if envelope is None:
            return None, "unavailable"
        fetched = _parse_iso_ts(envelope.get("fetched_at"))
        if fetched is None or fetched > datetime.now(timezone.utc):
            return None, "unavailable"
        definition = _capability_def(capability)
        age = max(0, int((datetime.now(timezone.utc) - fetched).total_seconds()))
        status = "fresh" if definition and age <= definition.ttl_seconds else "stale"
        return envelope, status

    def _resolve_universe(self, universe: dict[str, Any]) -> set[str] | None:
        ut = universe["type"]
        uv = universe.get("value")
        if ut == "local":
            return self._local_symbol_set()  # 严格解析为 curated 集合
        if ut == "index":
            if not isinstance(uv, str) or not re.fullmatch(r"[0-9]{6}\.(SH|SZ|BJ)", uv):
                raise ScreenerError("invalid_universe", "非法指数代码")
            envelope, _ = self._cap("index", "global")
            symbols = self._index_constituents(envelope, uv)
            if symbols is None:
                raise ScreenerError("invalid_universe", "未找到该指数的成分股缓存")
            return symbols
        if ut == "sector":
            if not isinstance(uv, str) or not uv:
                raise ScreenerError("invalid_universe", "非法板块代码")
            envelope, _ = self._cap("sector", "global")
            symbols = self._sector_members(envelope, uv)
            if symbols is None:
                raise ScreenerError("invalid_universe", "未找到该板块成员缓存")
            return symbols
        if ut == "industry_chain":
            if not isinstance(uv, str) or not uv:
                raise ScreenerError("invalid_universe", "非法产业链代码")
            envelope, _ = self._cap("industry_chain", "global")
            symbols = self._chain_symbols(envelope, uv)
            if symbols is None:
                raise ScreenerError("invalid_universe", "未找到该产业链成员缓存")
            return symbols
        raise ScreenerError("invalid_universe", "非法 universe.type")

    def _index_constituents(self, envelope, code: str) -> set[str] | None:
        if envelope is None:
            return None
        raw = envelope.get("data")
        if not isinstance(raw, dict):
            return None
        symbols: set[str] = set()
        found = False
        raw_code = raw.get("index_code") or raw.get("code")
        if isinstance(raw_code, str) and raw_code == code:
            found = True
            cons = raw.get("constituents") or raw.get("stocks") or raw.get("members")
            if isinstance(cons, list):
                for c in cons:
                    sym = _norm_symbol_text(c.get("symbol") if isinstance(c, dict) else None)
                    if sym:
                        symbols.add(sym)
        if not found and isinstance(raw.get("indexes"), list):
            for entry in raw["indexes"]:
                if isinstance(entry, dict) and entry.get("code") == code:
                    found = True
                    cons = entry.get("constituents") or entry.get("stocks") or entry.get("members")
                    if isinstance(cons, list):
                        for c in cons:
                            sym = _norm_symbol_text(c.get("symbol") if isinstance(c, dict) else None)
                            if sym:
                                symbols.add(sym)
                    break
        if not found and isinstance(raw.get("constituents_by_index"), dict):
            cons = raw["constituents_by_index"].get(code)
            if isinstance(cons, list):
                found = True
                for c in cons:
                    sym = _norm_symbol_text(c.get("symbol") if isinstance(c, dict) else None)
                    if sym:
                        symbols.add(sym)
        return symbols if found else None

    def _sector_members(self, envelope, code_or_name: str) -> set[str] | None:
        """板块成员：仅当缓存能证明成员关系才返回；否则 None（不可证明）。"""
        if envelope is None:
            return None
        raw = envelope.get("data")
        rows = raw if isinstance(raw, list) else (raw.get("sectors") if isinstance(raw, dict) else None)
        if not isinstance(rows, list):
            return None
        matched = None
        for r in rows:
            if isinstance(r, dict) and (r.get("code") == code_or_name or r.get("name") == code_or_name):
                matched = r
                break
        if matched is None:
            return None
        symbols: set[str] = set()
        leader = _norm_symbol_text(matched.get("leader_symbol"))
        if leader:
            symbols.add(leader)
        members_raw = matched.get("members") or matched.get("stocks") or matched.get("constituents")
        if isinstance(members_raw, list):
            for m in members_raw:
                sym = _norm_symbol_text(m.get("symbol") if isinstance(m, dict) else m)
                if sym:
                    symbols.add(sym)
        if not symbols:
            return None  # 无法证明成员关系
        return symbols

    def _chain_symbols(self, envelope, code_or_name: str) -> set[str] | None:
        if envelope is None:
            return None
        raw = envelope.get("data")
        chains = raw if isinstance(raw, list) else (raw.get("chains") if isinstance(raw, dict) else None)
        if not isinstance(chains, list):
            return None
        matched = next((c for c in chains if isinstance(c, dict) and (c.get("code") == code_or_name or c.get("name") == code_or_name)), None)
        if matched is None:
            return None
        symbols: set[str] = set()
        for stage in ("upstream", "midstream", "downstream"):
            nodes = matched.get(stage)
            if isinstance(nodes, list):
                for node in nodes:
                    if isinstance(node, dict):
                        for sym in node.get("related_symbols") or []:
                            clean = _norm_symbol_text(sym)
                            if clean:
                                symbols.add(clean)
        if not symbols:
            return None  # 无法证明成员关系
        return symbols

    def _extract_rows(self, data: Any) -> list[dict[str, Any]] | None:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("items", "list", "results", "stocks", "data", "records"):
                value = data.get(key)
                if isinstance(value, list):
                    return value
        return None

    @staticmethod
    def _remap_westock_row(raw: Any) -> dict[str, Any] | None:
        """适用于受控 Westock 选股结果行（filter/strategy/factor/label 共用）的最小字段映射：

        - symbol 缺失：仅由合法 code 补（sh600519 → 600519.SH）；code 缺失/非法 → 无 symbol，后续丢弃；
        - symbol 存在：symbol 必须严格合法；code 不存在 → 保留；
          code 存在但非法、或合法但与 symbol 不一致 → 整行丢弃（不静默选择任一方）；
          code 合法且一致 → 保留 symbol，移除冗余 code；
        - ChangePCT → change_percent；ClosePrice → price（字符串数值由 _norm_row 转 float）。
        未知字段保持原样，交由 _norm_row 白名单过滤，不猜字段。
        """
        if not isinstance(raw, dict):
            return {}
        out = dict(raw)
        code = out.get("code")
        symbol = out.get("symbol")
        code_symbol = westock_code_to_symbol(code) if isinstance(code, str) else None

        if symbol is None:
            # symbol 缺失：仅由合法 code 补齐；code 缺失/非法 → 保持无 symbol（_norm_row 丢弃）
            if code_symbol is not None:
                out["symbol"] = code_symbol
                out.pop("code", None)
        else:
            # symbol 存在：symbol 必须严格合法
            if not isinstance(symbol, str) or not SYMBOL_RE.fullmatch(symbol):
                return None
            if "code" in out:
                # code 存在即必须有效且与 symbol 一致，否则整行丢弃
                if code_symbol is None or code_symbol != symbol:
                    return None
                out.pop("code", None)

        for src, dst in (("ChangePCT", "change_percent"), ("ClosePrice", "price")):
            if src in out and dst not in out:
                out[dst] = out.pop(src)
        return out

    def _norm_row(self, raw: Any, local_available: set[str]) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        out: dict[str, Any] = {}
        symbol: str | None = None
        for field in _RESULT_FIELDS:
            if field not in raw or raw[field] is None:
                continue
            value = raw[field]
            if field == "symbol":
                symbol = _norm_symbol_text(value)
                if symbol is not None:
                    out["symbol"] = symbol
                continue
            if field in _NUMERIC_RESULT_FIELDS:
                number = _as_finite_float(value)
                if number is not None:
                    out[field] = number
                continue
            if field == "reason":
                text = _norm_text(value, 400)
                if text:
                    out[field] = text
                continue
            if field == "matched_conditions":
                cleaned = [c for c in value if isinstance(c, str) and len(c) <= 200]
                if cleaned:
                    out[field] = cleaned
                continue
            if field == "matched_labels":
                cleaned = [c for c in value if c in _LABEL_VALUES]
                if cleaned:
                    out[field] = cleaned
                continue
            if field == "factor_values":
                if isinstance(value, dict):
                    cleaned: dict[str, float] = {}
                    for k, v in value.items():
                        if k in _FACTOR_WEIGHT_KEYS or k in _FACTOR_NAMES:
                            n = _as_finite_float(v)
                            if n is not None:
                                cleaned[k] = n
                    if cleaned:
                        out[field] = cleaned
                continue
            if isinstance(value, (dict, list)):
                continue  # 嵌套结构丢弃，不转文本
            text = _norm_text(value, MAX_TEXT)
            if text is not None:
                out[field] = text
        if symbol is None:
            return None
        out["local_history_available"] = symbol in local_available  # 不信 Westock
        return out

    def _dedupe_and_sort(self, rows: list[dict[str, Any]], sort: dict[str, str]) -> list[dict[str, Any]]:
        """去重（同 symbol 保留排序最优）+ 缺失值 asc/desc 均排末尾。"""
        field = sort["field"]
        direction = sort["direction"]

        def sort_key(row: dict[str, Any]):
            v = row.get(field)
            if field == "symbol":
                return (1, "") if v is None else (0, str(v))
            if v is None:
                return (1, 0)  # 缺失永远最后
            return (0, v)

        if direction == "desc" and field != "symbol":
            def desc_key(row: dict[str, Any]):
                group, v = sort_key(row)
                if group == 1:
                    return (1, 0)
                if isinstance(v, (int, float)):
                    return (0, -v)
                return (0, v)
            rows.sort(key=desc_key)
        else:
            rows.sort(key=sort_key)
        # 去重：保留排序最优
        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for row in rows:
            symbol = row.get("symbol")
            if symbol in seen:
                continue
            seen.add(symbol)
            deduped.append(row)
        return deduped

    def _local_symbol_set(self) -> set[str]:
        symbols: set[str] = set()
        for path in self.curated.curated_dir.glob("daily_quotes_*_*.parquet"):
            m = re.match(r"daily_quotes_([0-9]{6}\.(?:SH|SZ|BJ))_", path.name)
            if m:
                symbols.add(m.group(1))
        return symbols

    # ---- results ----
    def get_result(self, result_id: str) -> dict[str, Any]:
        payload = self.store.read_result(result_id)
        if payload is None:
            raise ScreenerError("result_not_found", "结果不存在", status_code=404)
        return payload

    # ---- saved ----
    def list_saved(self) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "items": self.store.load_saved()}

    def save_filter(self, body: Any) -> dict[str, Any]:
        _reject_forbidden_recursive(body)
        if not isinstance(body, dict):
            raise ScreenerError("invalid_request", "请求必须为对象")
        name = body.get("name")
        if not isinstance(name, str) or not (1 <= len(name) <= 80):
            raise ScreenerError("invalid_request", "name 必须为 1–80 字符")
        if any(ord(ch) < 32 for ch in name) or "script" in name.lower():
            raise ScreenerError("invalid_request", "name 含控制字符或非法内容")
        query = _validate_query(body.get("query"))  # 保存前完整重新校验
        items = self.store.load_saved()
        saved_id = secrets.token_hex(16)
        now = _now_iso()
        items.append({
            "id": saved_id, "name": name, "query": query,
            "created_at": now, "updated_at": now,
        })
        if len(items) > MAX_SAVED:
            items = items[-MAX_SAVED:]
        self.store.save_saved(items)
        return {"saved_id": saved_id, "name": name}

    def delete_saved(self, saved_id: str) -> dict[str, Any]:
        if not _ID_RE.fullmatch(saved_id):
            raise ScreenerError("invalid_request", "非法 saved_id")
        items = self.store.load_saved()
        remaining = [i for i in items if i.get("id") != saved_id]
        if len(remaining) == len(items):
            raise ScreenerError("saved_not_found", "保存条件不存在", status_code=404)
        self.store.save_saved(remaining)
        return {"deleted": saved_id}

    # ---- candidates ----
    def list_candidates(self) -> dict[str, Any]:
        items = self.store.load_candidates()
        return {
            "schema_version": SCHEMA_VERSION,
            "items": items,
            "note": "研究候选列表仅用于人工研究整理，不会生成 BigA 信号、订单或持仓，也不会同步到 Westock 自选股。",
        }

    def add_candidate(self, body: Any) -> dict[str, Any]:
        _reject_forbidden_recursive(body)
        if not isinstance(body, dict):
            raise ScreenerError("invalid_request", "请求必须为对象")
        symbol = _norm_symbol_text(body.get("symbol"))
        if symbol is None:
            raise ScreenerError("invalid_request", "非法 symbol")
        source_result_id = body.get("source_result_id")
        if not isinstance(source_result_id, str) or not _ID_RE.fullmatch(source_result_id):
            raise ScreenerError("invalid_request", "非法 source_result_id")
        result = self.store.read_result(source_result_id)
        if result is None:
            raise ScreenerError("result_not_found", "来源结果不存在", status_code=404)
        items_in_result = (result.get("data") or {}).get("items") or []
        row = next((i for i in items_in_result if i.get("symbol") == symbol), None)
        if row is None:
            raise ScreenerError("invalid_request", "symbol 必须属于来源结果")
        note = body.get("note")
        if note is None:
            note = ""
        if not isinstance(note, str):
            raise ScreenerError("invalid_request", "note 必须为字符串")
        if len(note) > 400:
            raise ScreenerError("invalid_request", "note 最多 400 字符")
        if any(ord(ch) < 32 for ch in note) or "script" in note.lower():
            raise ScreenerError("invalid_request", "note 含控制字符或非法内容")
        candidates = self.store.load_candidates()
        existing = next((c for c in candidates if c.get("symbol") == symbol), None)
        entry = {
            "symbol": symbol,
            "name": _norm_text(row.get("name"), MAX_TEXT) or symbol,  # 由结果行派生，不信 body；缺名回退代码
            "source_result_id": source_result_id,
            "note": note,
            "added_at": existing.get("added_at") if existing else _now_iso(),
            "local_history_available": symbol in self._local_symbol_set(),
        }
        if existing:
            candidates = [entry if c.get("symbol") == symbol else c for c in candidates]
        else:
            candidates.append(entry)
        if len(candidates) > MAX_CANDIDATES:
            candidates = candidates[-MAX_CANDIDATES:]
        self.store.save_candidates(candidates)  # 只修改 candidates.json
        return {"symbol": symbol, "added": not existing}

    def delete_candidate(self, symbol: str) -> dict[str, Any]:
        clean = _norm_symbol_text(symbol)
        if clean is None:
            raise ScreenerError("invalid_request", "非法 symbol")
        candidates = self.store.load_candidates()
        remaining = [c for c in candidates if c.get("symbol") != clean]
        if len(remaining) == len(candidates):
            raise ScreenerError("candidate_not_found", "候选不存在", status_code=404)
        self.store.save_candidates(remaining)
        return {"deleted": clean}


def _norm_symbol_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip().upper()
    return text if SYMBOL_RE.fullmatch(text) else None


def _capability_def(name: str):
    from .westock_bridge import CAPABILITY_MAP
    return CAPABILITY_MAP.get(name)


def build_screener_service(project_root: Any, clock: Callable[[], float] | None = None) -> ScreenerService:
    return ScreenerService(project_root, clock=clock)


__all__ = [
    "SCHEMA_VERSION",
    "ScreenerError",
    "ScreenerService",
    "build_screener_service",
    "canonical_query_hash",
    "MAX_BODY_BYTES",
    "RATE_LIMIT_PER_SECOND",
    "_MISSING_CACHE_WARNING",
]
