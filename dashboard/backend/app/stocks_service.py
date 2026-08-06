"""Phase B: 个股行情与策略联动（只读研究服务）。

数据边界：
- K 线只读本地 curated parquet，按列名读取；raw/qfq 均来自本地 curated 真实字段，
  **Westock 复权永不进入 history**；文件访问限定固定目录。
- Westock quote/minute 复用 Phase A ``WestockCacheStore`` 的完整 schema 校验与
  ``CAPABILITY_MAP`` 的 TTL，输出 fresh/stale/unavailable；stale 可展示但明确标记；
  future / 非法 schema 一律 unavailable；绝不标 realtime。
- 信号/订单/持仓只读既有产物与唯一模拟账本，不创建、不修改。
"""

from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from .westock_bridge import CAPABILITY_MAP, WestockCacheStore

SCHEMA_VERSION = 1
SYMBOL_RE = re.compile(r"^[0-9]{6}\.(SH|SZ|BJ)$")
_WESTOCK_CODE_RE = re.compile(r"(sh|sz|bj)([0-9]{6})")
_RANGE_TRADING_DAYS = {"1m": 21, "3m": 63, "6m": 126, "1y": 252, "3y": 756, "all": None}

# 受控字段白名单：只接受明确命名的字段，未知结构一律 unavailable（不猜字段）
_MINUTE_TIME_FIELDS = ("time", "date_time")
_MINUTE_PRICE_FIELDS = ("price", "close")
_MINUTE_VOLUME_FIELDS = ("volume", "vol")
_QUOTE_PRICE_FIELDS = ("price", "last", "close")
_QUOTE_CHG_PCT_FIELDS = ("change_percent", "chg_pct", "change_pct")
_QUOTE_TIME_FIELDS = ("time", "date", "date_time")


def westock_code_to_symbol(value: Any) -> str | None:
    """Westock code → BigA symbol：sh600519 → 600519.SH。

    严格：仅接受 (sh|sz|bj) 小写前缀 + 6 位数字的完整匹配；
    不 strip、不猜测未知格式；其余（600519 / SH600519 / 带空格等）一律 None。
    """
    if not isinstance(value, str):
        return None
    m = _WESTOCK_CODE_RE.fullmatch(value)
    if not m:
        return None
    return f"{m.group(2)}.{m.group(1).upper()}"


def coerce_identity(value: Any) -> str | None:
    """身份字段值 → 统一 BigA symbol 形式（600519.SH）用于比较。

    接受 Westock code（sh600519）或 BigA symbol（600519.SH）；
    未知格式返回 None（不猜测）。
    """
    if not isinstance(value, str):
        return None
    if SYMBOL_RE.fullmatch(value):
        return value
    return westock_code_to_symbol(value)


def identity_violation(expected_symbol: str, value: Any) -> bool:
    """身份字段冲突检测：value 存在但无法解析或解析后 != expected → True。

    缺失（None）不视为冲突（是否阻断由各能力规则决定）；
    明确提供但格式无法解析 → fail-closed 视为冲突。
    """
    if value is None:
        return False
    coerced = coerce_identity(value)
    if coerced is None:
        return True
    return coerced != expected_symbol


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _as_finite_float(value: Any) -> float | None:
    """安全转 float；非数值 / NaN / Infinity → None（结构化降级）。"""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


_MINUTE_SCAN_LIMIT = 500


def _normalize_minute_legacy(data: Any) -> tuple[list[dict[str, Any]] | None, str]:
    """向后兼容路径（expected_symbol 未给定时的旧结构解析）。

    只接受：list[dict] 或 {"minutes": list[dict]}；每行必须含受控 time 与 price
    字段（volume 可选）。字段缺失、结构未知或数值非法 → (None, 原因)，不猜字段。
    """
    rows: Any = None
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict) and isinstance(data.get("minutes"), list):
        rows = data["minutes"]
    if rows is None:
        return None, "未知数据结构（仅接受数组或 {minutes: [...]}）"
    out: list[dict[str, Any]] = []
    for item in rows:
        if not isinstance(item, dict):
            return None, "行不是对象"
        time_field = next((f for f in _MINUTE_TIME_FIELDS if f in item), None)
        price_field = next((f for f in _MINUTE_PRICE_FIELDS if f in item), None)
        if time_field is None or price_field is None:
            return None, "缺少受控 time/price 字段"
        volume = None
        volume_field = next((f for f in _MINUTE_VOLUME_FIELDS if f in item), None)
        if volume_field is not None:
            volume = _as_finite_float(item[volume_field])
            if volume is None:
                return None, "成交量字段非法"
        price = _as_finite_float(item[price_field])
        if price is None:
            return None, "价格字段非法"
        out.append({"time": str(item[time_field]), "price": price, "volume": volume})
    return out, "ok"


def _parse_minute_line(line: Any) -> dict[str, Any] | None:
    """解析 Westock 分时行：恰好 4 段 "HHMM price volume amount"。

    time 严格 4 位数字且为合法 HHMM；price finite 且 >0；volume finite 且 >=0（手，
    不乘 100，整数输出整数）；amount finite 且 >=0（元）。任一非法 → 整行丢弃。
    """
    if not isinstance(line, str):
        return None
    parts = line.split()
    if len(parts) != 4:
        return None
    raw_time, raw_price, raw_vol, raw_amt = parts
    if len(raw_time) != 4 or not raw_time.isdigit():
        return None
    hour, minute = int(raw_time[:2]), int(raw_time[2:])
    if hour > 23 or minute > 59:
        return None
    price = _as_finite_float(raw_price)
    volume = _as_finite_float(raw_vol)
    amount = _as_finite_float(raw_amt)
    if price is None or price <= 0:
        return None
    if volume is None or volume < 0:
        return None
    if amount is None or amount < 0:
        return None
    volume_out: float | int = int(volume) if volume == int(volume) else volume
    return {"time": f"{hour:02d}:{minute:02d}", "price": price,
            "volume": volume_out, "amount": amount}


def _normalize_minute_westock(data: Any, expected_symbol: str,
                              warnings: list[str]) -> tuple[dict[str, Any] | None, str]:
    """Westock 真实分时结构校准（F2-A）：

    真实结构为严格 wrapper 单键 data.<westock_code>.data：date（YYYYMMDD）+ data（字符串数组）。
    只解析该字符串数组；不解析、不输出 qt / mx_price / 原始字符串。
    统计非法行数、重复 time 数与超限裁剪，warning 固定脱敏（不回显原始行）。
    """
    payload = unwrap_strict_westock_payload(data, expected_symbol)
    if payload is None:
        return None, "外层股票代码与请求标的不一致"
    inner = payload.get("data")
    if not isinstance(inner, dict):
        return None, "缺少 data 节点"
    raw_date = inner.get("date")
    if not isinstance(raw_date, str) or not re.fullmatch(r"[0-9]{8}", raw_date):
        return None, "日期非法"
    try:
        date_out = datetime.strptime(raw_date, "%Y%m%d").strftime("%Y-%m-%d")
    except ValueError:
        return None, "日期非法"
    raw_rows = inner.get("data")
    if not isinstance(raw_rows, list):
        return None, "分时行非列表"
    if len(raw_rows) > _MINUTE_SCAN_LIMIT:
        warnings.append(f"Westock 分时数据超过 {_MINUTE_SCAN_LIMIT} 行扫描上限，已裁剪")
        raw_rows = raw_rows[:_MINUTE_SCAN_LIMIT]
    by_time: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    invalid = 0
    duplicates = 0
    for line in raw_rows:
        row = _parse_minute_line(line)
        if row is None:
            invalid += 1
            continue
        if row["time"] in seen:
            duplicates += 1
        else:
            seen.add(row["time"])
        by_time[row["time"]] = row  # 重复 time 保留输入中最后一个有效行
    if invalid:
        warnings.append(f"Westock 分时数据含 {invalid} 行无法解析，已丢弃")
    if duplicates:
        warnings.append(f"Westock 分时数据含 {duplicates} 个重复时间，保留最后值")
    if not by_time:
        return None, "无可识别分时行"
    rows = [by_time[t] for t in sorted(by_time)]
    return {
        "date": date_out,
        "rows": rows,
        "price_unit": "CNY",
        "volume_unit": "lot",
        "amount_unit": "CNY",
    }, "ok"


def normalize_minute(data: Any, expected_symbol: str | None = None,
                     warnings: list[str] | None = None) -> tuple[Any, str]:
    """受控标准化分时数据（F2-A 校准版）。

    expected_symbol 给定时走 Westock 真实结构路径（wrapper 单键 + data.data 字符串数组），
    绑定请求标的并输出 date/rows/单位元数据；未给定时保留旧结构路径（list[dict] /
    {"minutes": [...]}），供既有调用兼容。
    """
    if expected_symbol is None:
        return _normalize_minute_legacy(data)
    sink = warnings if warnings is not None else []
    return _normalize_minute_westock(data, expected_symbol, sink)


_SYMBOL_PREFIX_RE = re.compile(r"^(sh|sz|bj)[0-9]{6}$")


def _unwrap_symbol_payload(data: Any, expected_symbol: str | None = None) -> Any:
    """解包 Westock 单键嵌套：{"sh600519": {...}} → {...}。

    仅当外层 dict 恰有一个键、键匹配 sh/sz/bj 前缀格式、且值为 dict 时才解包，
    避免误伤扁平结构或非符号键。

    expected_symbol 给定时，外层唯一键必须转换后与之一致；不一致返回 None
    （身份冲突，调用方应降级为 unavailable，绝不展示他股价格）。
    """
    if isinstance(data, dict) and len(data) == 1:
        key, value = next(iter(data.items()))
        if isinstance(key, str) and _SYMBOL_PREFIX_RE.fullmatch(key) and isinstance(value, dict):
            key_symbol = westock_code_to_symbol(key)
            if expected_symbol is not None and key_symbol != expected_symbol:
                return None  # 外层唯一键与请求标的不一致
            return value
    return data


def unwrap_strict_westock_payload(data: Any, expected_symbol: str) -> dict[str, Any] | None:
    """严格 Westock wrapper 解包（minute/technical 校准专用，不改共享 helper 语义）。

    要求全部满足才解包：
    - 顶层 dict 恰好一个键；
    - 该键为合法前缀股票键（sh/sz/bj + 6 位，可转换）；
    - 值为 dict；
    - 外层股票与 expected_symbol 完整一致（市场前缀必须一致）。
    任一不满足返回 None（身份冲突/结构非法 → 调用方降级 unavailable）。
    """
    if not isinstance(data, dict) or len(data) != 1:
        return None
    key, value = next(iter(data.items()))
    if not isinstance(key, str):
        return None
    key_symbol = westock_code_to_symbol(key)
    if key_symbol is None or key_symbol != expected_symbol:
        return None
    if not isinstance(value, dict):
        return None
    return value


def quote_identity_conflict(data: Any, expected_symbol: str) -> str | None:
    """quote 身份冲突定位（脱敏 reason，不回显原始值）。

    无冲突或纯结构问题返回 None；冲突返回固定文案（≤400 字符）。
    """
    payload = data
    if isinstance(data, dict) and len(data) == 1:
        key, inner = next(iter(data.items()))
        if isinstance(key, str) and _SYMBOL_PREFIX_RE.fullmatch(key) and isinstance(inner, dict):
            key_symbol = westock_code_to_symbol(key)
            if key_symbol != expected_symbol:
                return "外层股票代码与请求标的不一致"
            payload = inner
    if isinstance(payload, dict):
        if identity_violation(expected_symbol, payload.get("code")):
            return "code 与请求标的不一致"
        if identity_violation(expected_symbol, payload.get("symbol")):
            return "symbol 与请求标的不一致"
    return None


def normalize_quote(data: Any, expected_symbol: str | None = None) -> dict[str, Any] | None:
    """受控标准化 quote 缓存：身份绑定 + 价格 + 可选涨跌幅 + 数据时间。

    expected_symbol（如 600519.SH）给定时：
    - 单键嵌套的外层唯一键必须转换后与 expected 一致；
    - 内层 code/symbol（若存在）必须与 expected 一致；
    - 任一明确身份字段冲突 → None（unavailable），绝不展示他股价格。
    flat payload 含 code/symbol 时同样校验。未识别字段不输出。
    """
    payload = _unwrap_symbol_payload(data, expected_symbol)
    if payload is None:
        return None  # 外层唯一键与请求标的不一致
    if not isinstance(payload, dict):
        return None
    if expected_symbol is not None:
        if identity_violation(expected_symbol, payload.get("code")) or \
                identity_violation(expected_symbol, payload.get("symbol")):
            return None
    price = next((payload[f] for f in _QUOTE_PRICE_FIELDS if f in payload and payload[f] is not None), None)
    price = _as_finite_float(price)
    if price is None:
        return None
    change_percent = None
    for field in _QUOTE_CHG_PCT_FIELDS:
        if field in payload and payload[field] is not None:
            change_percent = _as_finite_float(payload[field])
            if change_percent is not None:
                break
    ts = next((payload[f] for f in _QUOTE_TIME_FIELDS if f in payload), None)
    return {
        "price": price,
        "change_percent": change_percent,
        "time": str(ts) if ts is not None else None,
    }


class CuratedStocksService:
    """只读本地 curated 行情与运行产物，服务 /api/stocks 系列接口。"""

    def __init__(self, project_root: Path):
        self.root = Path(project_root).resolve()
        self.curated_dir = self.root / "data" / "curated"
        self.westock_store = WestockCacheStore(self.root / "state" / "dashboard" / "westock")
        self.reports_dir = self.root / "reports" / "phase-4" / "daily"
        self.state_dir = self.root / "state" / "automation"
        # symbol(6 位无后缀) -> 证券名称；主数据缺失时为空映射（不阻断列表）
        self._security_names: dict[str, str] = self._load_security_names()

    def _load_security_names(self) -> dict[str, str]:
        """读取 data/metadata/security_master.parquet 的名称映射（best-effort）。"""
        path = self.root / "data" / "metadata" / "security_master.parquet"
        try:
            if not path.exists():
                return {}
            df = pd.read_parquet(path)
            if df.empty or "symbol" not in df.columns or "name" not in df.columns:
                return {}
            mapping: dict[str, str] = {}
            for sym, name in zip(df["symbol"], df["name"]):
                key = str(sym).strip()
                if key and name is not None and str(name).strip():
                    mapping[key] = str(name).strip()
            return mapping
        except Exception:  # noqa: BLE001 - 主数据是可选增强，失败不阻断
            return {}

    # ------------------------------------------------------------------ #
    # curated 读取（限定固定目录；数据最新文件；trade_date 类型标准化）
    # ------------------------------------------------------------------ #

    def _load_curated(self, symbol: str) -> pd.DataFrame | None:
        """扫描全部候选 parquet，选 trade_date.max 最大的（数据最新，而非 mtime 最新）。"""
        if not SYMBOL_RE.fullmatch(symbol):
            raise ValueError("非法 symbol")
        cands = sorted(
            self.curated_dir.glob(f"daily_quotes_{symbol}_*.parquet"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        best: pd.DataFrame | None = None
        best_last: Any = None
        for cand in cands:
            try:
                df = pd.read_parquet(cand)
            except Exception:  # noqa: BLE001 - 损坏文件跳过
                continue
            if df is None or df.empty or "trade_date" not in df.columns:
                continue
            # trade_date 类型标准化后比较（兼容 str/date/Timestamp）
            normalized = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")
            last = normalized.max()
            if best is None or (last is not None and (best_last is None or last > best_last)):
                best = df.assign(_td_norm=normalized)
                best_last = last
        if best is None:
            return None
        return best.sort_values("_td_norm").reset_index(drop=True)

    # ------------------------------------------------------------------ #
    # Westock 缓存：复用 Phase A schema 校验 + TTL → fresh/stale/unavailable
    # ------------------------------------------------------------------ #

    def _westock_cache(self, capability: str, symbol: str) -> tuple[dict[str, Any] | None, str]:
        """返回 (envelope, status)。future / 非法 schema → unavailable；stale 可读但标记 stale。"""
        envelope = self.westock_store.read(capability, symbol)
        if envelope is None:
            return None, "unavailable"
        fetched = _parse_iso_ts(envelope.get("fetched_at"))
        if fetched is None or fetched > _utc_now():
            return None, "unavailable"  # future timestamp 视为异常缓存
        definition = CAPABILITY_MAP.get(capability)
        ttl = definition.ttl_seconds if definition else 0
        age = max(0, int((_utc_now() - fetched).total_seconds()))
        status = "fresh" if age <= ttl else "stale"
        return envelope, status

    # ------------------------------------------------------------------ #
    # 1. 股票列表
    # ------------------------------------------------------------------ #

    def list_stocks(self, query: str | None, limit: int, offset: int) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for path in sorted(self.curated_dir.glob("daily_quotes_*_*.parquet")):
            match = re.match(r"daily_quotes_([0-9]{6}\.(?:SH|SZ|BJ))_", path.name)
            if not match or match.group(1) in seen:
                continue
            symbol = match.group(1)
            seen.add(symbol)
            df = self._load_curated(symbol)
            if df is None or df.empty:
                continue
            latest = str(df["_td_norm"].iloc[-1])
            items.append({
                "symbol": symbol,
                "name": self._security_names.get(symbol[:6]),
                "latest_trade_date": latest,
                "bar_count": int(len(df)),
            })
        if query:
            q = query.strip().upper()
            items = [item for item in items if q in item["symbol"]]
        items = sorted(items, key=lambda item: item["symbol"])
        total = len(items)
        page = items[offset:offset + limit]
        return {
            "schema_version": SCHEMA_VERSION,
            "source": "local-curated",
            "as_of": _utc_now().isoformat(),
            "fetched_at": None,
            "cache_status": "unavailable",
            "is_realtime": False,
            "transport": "local-curated",
            "availability": {"curated": True, "westock": False},
            "data": {"total": total, "offset": offset, "limit": limit, "items": page},
            "warnings": [],
        }

    # ------------------------------------------------------------------ #
    # 2. 历史行情（raw/qfq，仅本地 curated；数值结构化降级）
    # ------------------------------------------------------------------ #

    def history(self, symbol: str, adjustment: str, range_key: str, end: str | None) -> dict[str, Any]:
        warnings: list[str] = []
        df = self._load_curated(symbol)
        if df is None or df.empty:
            return self._history_envelope(symbol, adjustment, range_key, [], warnings,
                                          availability={"curated": False, "qfq": False},
                                          status="unavailable", as_of=None,
                                          message="本地无该标的 curated 历史行情")

        base_cols = {"trade_date", "open_raw", "high_raw", "low_raw", "close_raw", "volume", "amount"}
        missing = base_cols - set(df.columns)
        if missing:
            return self._history_envelope(
                symbol, adjustment, range_key, [], warnings,
                availability={"curated": True, "qfq": False},
                status="unavailable", as_of=str(df["_td_norm"].iloc[-1]),
                message=f"curated 缺字段: {', '.join(sorted(missing))}",
            )

        if adjustment == "qfq":
            qfq_cols = {"open_qfq", "high_qfq", "low_qfq", "close_qfq"}
            if not qfq_cols <= set(df.columns):
                return self._history_envelope(
                    symbol, adjustment, range_key, [], warnings,
                    availability={"curated": True, "qfq": False},
                    status="unavailable", as_of=str(df["_td_norm"].iloc[-1]),
                    message="本地 curated 无 qfq 字段；Westock 复权不进入历史图表",
                )

        sub = df
        if end:
            try:
                end_date = date.fromisoformat(end)
            except ValueError:
                raise ValueError("end 必须是 YYYY-MM-DD")
            sub = sub[sub["_td_norm"] <= end_date.isoformat()]
        if range_key != "all":
            n_days = _RANGE_TRADING_DAYS.get(range_key)
            if n_days is None:
                raise ValueError(f"非法区间: {range_key}")
            sub = sub.tail(n_days)
        if sub.empty:
            return self._history_envelope(
                symbol, adjustment, range_key, [], warnings,
                availability={"curated": True, "qfq": adjustment == "qfq"},
                status="unavailable", as_of=str(df["_td_norm"].iloc[-1]),
                message="区间内无数据")

        o_col, h_col, l_col, c_col = ("open_raw", "high_raw", "low_raw", "close_raw")
        if adjustment == "qfq":
            o_col, h_col, l_col, c_col = ("open_qfq", "high_qfq", "low_qfq", "close_qfq")

        rows: list[dict[str, Any]] = []
        skipped = 0
        for _, row in sub.iterrows():
            o = _as_finite_float(row.get(o_col))
            h = _as_finite_float(row.get(h_col))
            lo = _as_finite_float(row.get(l_col))
            c = _as_finite_float(row.get(c_col))
            volume = _as_finite_float(row.get("volume"))
            amount = _as_finite_float(row.get("amount"))
            if o is None or h is None or lo is None or c is None:
                skipped += 1  # OHLC 任一非法 → 整行结构化跳过（不 500）
                continue
            rows.append({
                "date": str(row["_td_norm"]),
                "open": f"{o:.2f}", "high": f"{h:.2f}",
                "low": f"{lo:.2f}", "close": f"{c:.2f}",
                "volume": int(volume) if volume is not None else None,
                "amount": amount,
            })
        if skipped:
            warnings.append(f"{skipped} 行因 OHLC 非数值/NaN/Infinity 被结构化跳过")

        availability = {"curated": True, "qfq": adjustment == "qfq"}
        return self._history_envelope(
            symbol, adjustment, range_key, rows, warnings,
            availability=availability, status="available",
            as_of=str(df["_td_norm"].iloc[-1]),
            message=f"curated {adjustment} 历史行情 {len(rows)} 行",
        )

    def _history_envelope(self, symbol: str, adjustment: str, range_key: str,
                          rows: list[dict[str, Any]], warnings: list[str],
                          *, availability: dict[str, bool], status: str, as_of: str | None,
                          message: str) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "symbol": symbol,
            "source": "local-curated",
            "as_of": as_of,  # 实际最后交易日，而非系统当天
            "fetched_at": None,
            "cache_status": status,
            "is_realtime": False,
            "transport": "local-curated",
            "availability": availability,
            "adjustment": adjustment,
            "range": range_key,
            "data": {"rows": rows},
            "warnings": warnings,
            "message": message,
        }

    # ------------------------------------------------------------------ #
    # 3. 快照：本地最后交易日 + Westock quote 缓存（受控标准化）
    # ------------------------------------------------------------------ #

    def snapshot(self, symbol: str) -> dict[str, Any]:
        warnings: list[str] = []
        local: dict[str, Any] | None = None
        df = self._load_curated(symbol)
        if df is not None and not df.empty and {"trade_date", "close_raw", "open_raw",
                                                "high_raw", "low_raw", "volume", "amount"} <= set(df.columns):
            last = df.iloc[-1]
            prev = df.iloc[-2] if len(df) >= 2 else None
            close = _as_finite_float(last["close_raw"])
            prev_close = _as_finite_float(prev["close_raw"]) if prev is not None else None
            if close is not None:
                local = {
                    "date": str(last["_td_norm"]),
                    "close": f"{close:.2f}",
                    "open": f"{_as_finite_float(last['open_raw']):.2f}",
                    "high": f"{_as_finite_float(last['high_raw']):.2f}",
                    "low": f"{_as_finite_float(last['low_raw']):.2f}",
                    "volume": int(_as_finite_float(last["volume"])) if _as_finite_float(last["volume"]) is not None else None,
                    "amount": _as_finite_float(last["amount"]),
                    "change": f"{close - prev_close:.2f}" if prev_close is not None else None,
                    "change_percent": f"{(close / prev_close - 1) * 100:.2f}" if prev_close else None,
                }

        envelope, quote_status = self._westock_cache("quote", symbol)
        quote_info: dict[str, Any] | None = None
        if envelope is not None and quote_status in ("fresh", "stale"):
            normalized = normalize_quote(envelope.get("data"), symbol)
            if normalized is None:
                conflict = quote_identity_conflict(envelope.get("data"), symbol)
                if conflict is not None:
                    warnings.append(
                        f"Westock quote 缓存身份校验失败（{conflict}），已降级为不可用"
                    )
                else:
                    warnings.append("Westock quote 缓存字段无法识别，已降级为不可用")
            else:
                quote_info = {
                    "price": normalized["price"],
                    "change_percent": normalized["change_percent"],
                    "time": normalized["time"],
                    "as_of": envelope.get("as_of"),
                    "fetched_at": envelope.get("fetched_at"),
                    "status": quote_status,  # fresh / stale 明确标记
                }
        else:
            warnings.append("无可用 Westock quote 缓存（可选旁路，不阻断本地行情）")

        return {
            "schema_version": SCHEMA_VERSION,
            "symbol": symbol,
            "source": "local-curated+westock-cache",
            "as_of": local["date"] if local else _utc_now().isoformat(),
            "fetched_at": quote_info["fetched_at"] if quote_info else None,
            "cache_status": "available" if local else "unavailable",
            "is_realtime": False,
            "transport": "local-curated+westock-cache",
            "availability": {
                "curated": local is not None,
                "westock_quote": quote_info is not None,
            },
            "data": {"local": local, "westock_quote": quote_info},
            "warnings": warnings,
        }

    # ------------------------------------------------------------------ #
    # 4. 分时：只读 Westock minute 缓存（受控标准化，非实时）
    # ------------------------------------------------------------------ #

    def minute(self, symbol: str) -> dict[str, Any]:
        if not SYMBOL_RE.fullmatch(symbol):
            raise ValueError("非法 symbol")
        envelope, status = self._westock_cache("minute", symbol)
        if envelope is None:
            return {
                "schema_version": SCHEMA_VERSION,
                "symbol": symbol,
                "source": "westock-mcp",
                "as_of": None,
                "fetched_at": None,
                "cache_status": "unavailable",
                "is_realtime": False,
                "transport": "cache_export",
                "availability": {"westock_minute": False},
                "data": None,
                "warnings": ["Westock 分时缓存不存在、过期或非法；不宣称实时"],
            }
        warnings: list[str] = ["Westock 缓存导出，非实时"]
        payload, reason = normalize_minute(envelope.get("data"), symbol, warnings)
        if payload is None:
            return {
                "schema_version": SCHEMA_VERSION,
                "symbol": symbol,
                "source": "westock-mcp",
                "as_of": envelope.get("as_of"),
                "fetched_at": envelope.get("fetched_at"),
                "cache_status": "unavailable",
                "is_realtime": False,
                "transport": "cache_export",
                "availability": {"westock_minute": False},
                "data": None,
                # 保留计数 warning（非法行/重复时间/裁剪），再追加降级文案
                "warnings": warnings + [f"Westock 分时数据无法标准化（{reason}），不展示"],
            }
        if status == "stale":
            warnings.append("Westock 分时缓存已过期（stale），仅作展示")
        return {
            "schema_version": SCHEMA_VERSION,
            "symbol": symbol,
            "source": "westock-mcp",
            "as_of": envelope.get("as_of"),
            "fetched_at": envelope.get("fetched_at"),
            "cache_status": status,  # fresh / stale / unavailable
            "is_realtime": False,
            "transport": "cache_export",
            "availability": {"westock_minute": True},
            "data": payload,
            "warnings": warnings,
        }

    # ------------------------------------------------------------------ #
    # 5. 研究汇总：信号 / 订单 / 持仓（只读既有产物与唯一账本）
    # ------------------------------------------------------------------ #

    def research(self, symbol: str) -> dict[str, Any]:
        if not SYMBOL_RE.fullmatch(symbol):
            raise ValueError("非法 symbol")
        as_of = self._usable_daily_as_of()
        signals: list[dict[str, Any]] = []
        orders: list[dict[str, Any]] = []
        warnings: list[str] = []

        if as_of is None:
            warnings.append("无可用 daily 产物（latest-daily 非 SUCCESS/exit 0 或产物目录缺失），信号/订单为空")
        else:
            rep_dir = self.reports_dir / as_of.isoformat()
            signals, orders = self._read_signals_orders(rep_dir, symbol)
        positions = self._read_positions(symbol)  # 持仓始终从唯一模拟账本读取

        return {
            "schema_version": SCHEMA_VERSION,
            "symbol": symbol,
            "source": "biga-artifacts",
            "as_of": as_of.isoformat() if as_of else None,
            "fetched_at": None,
            "cache_status": "available" if as_of else "unavailable",
            "is_realtime": False,
            "transport": "biga-artifacts",
            "availability": {"artifacts": as_of is not None},
            "data": {
                "as_of": as_of.isoformat() if as_of else None,
                "signals": signals,
                "orders": orders,
                "positions": positions,
            },
            "warnings": warnings,
        }

    def _usable_daily_as_of(self) -> date | None:
        """latest-daily 必须 SUCCESS 且 exit_code=0 且产物目录存在，才作为可用 research 日期。"""
        latest = self.state_dir / "latest-daily.json"
        if not latest.is_file():
            return None
        try:
            value = json.loads(latest.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict):
            return None
        if value.get("state") != "SUCCESS" or value.get("exit_code") != 0:
            return None
        as_of_raw = value.get("as_of_date")
        if not isinstance(as_of_raw, str):
            return None
        try:
            as_of = date.fromisoformat(str(as_of_raw)[:10])
        except ValueError:
            return None
        if not (self.reports_dir / as_of.isoformat()).is_dir():
            return None
        return as_of

    def _read_signals_orders(self, rep_dir: Path, symbol: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        signals: list[dict[str, Any]] = []
        orders: list[dict[str, Any]] = []
        sig_path = rep_dir / "signals.json"
        if sig_path.is_file():
            payload = self._read_json_object(sig_path)
            if payload is not None and isinstance(payload.get("signals"), list):
                for item in payload["signals"]:
                    if not isinstance(item, dict):
                        continue
                    if str(item.get("symbol", "")).upper() == symbol:
                        signals.append({
                            "signal_date": str(item.get("signal_date"))[:10],
                            "symbol": item.get("symbol"),
                            "side": str(item.get("side", "")).upper(),
                            "quantity": item.get("quantity"),
                            "reason": item.get("reason", ""),
                        })
        ord_path = rep_dir / "simulated-orders.json"
        if ord_path.is_file():
            payload = self._read_json_object(ord_path)
            if payload is not None and isinstance(payload.get("orders"), list):
                for item in payload["orders"]:
                    if not isinstance(item, dict):
                        continue
                    if str(item.get("symbol", "")).upper() == symbol:
                        orders.append({
                            "signal_date": str(item.get("signal_date"))[:10] if item.get("signal_date") else None,
                            "fill_date": str(item.get("fill_date"))[:10] if item.get("fill_date") else None,
                            "symbol": item.get("symbol"),
                            "side": str(item.get("side", "")).upper(),
                            "quantity": item.get("quantity"),
                            "status": item.get("status", ""),
                            "fill_price": str(item.get("fill_price")) if item.get("fill_price") is not None else None,
                            "reason": item.get("reason", ""),
                        })
        return signals, orders

    def _read_json_object(self, path: Path) -> dict[str, Any] | None:
        """顶层不是对象或 JSON 损坏 → None（fail-open）。"""
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _read_positions(self, symbol: str) -> list[dict[str, Any]]:
        """持仓始终从唯一模拟账本只读加载（不依赖 daily 产物存在）。"""
        positions: list[dict[str, Any]] = []
        acc_dir = self.state_dir / "accounts"
        if not acc_dir.is_dir():
            return positions
        for path in sorted(acc_dir.glob("*.json")):
            payload = self._read_json_object(path)
            if payload is None:
                continue
            account_id = payload.get("account_id", path.stem)
            raw_positions = payload.get("positions", [])
            if isinstance(raw_positions, dict):
                raw_positions = list(raw_positions.values())
            if not isinstance(raw_positions, list):
                continue
            for pos in raw_positions:
                if not isinstance(pos, dict):
                    continue
                if str(pos.get("symbol", "")).upper() == symbol:
                    positions.append({
                        "account_id": account_id,
                        "symbol": pos.get("symbol"),
                        "total_quantity": pos.get("total_quantity", 0),
                        "sellable_quantity": pos.get("sellable_quantity", 0),
                        "avg_raw_cost": str(pos.get("avg_raw_cost")),
                    })
        return positions


def build_stocks_service(project_root: Path) -> CuratedStocksService:
    return CuratedStocksService(project_root)


__all__ = [
    "SCHEMA_VERSION",
    "SYMBOL_RE",
    "CuratedStocksService",
    "build_stocks_service",
    "normalize_minute",
    "normalize_quote",
]
