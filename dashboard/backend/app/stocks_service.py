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
_RANGE_TRADING_DAYS = {"1m": 21, "3m": 63, "6m": 126, "1y": 252, "3y": 756, "all": None}

# 受控字段白名单：只接受明确命名的字段，未知结构一律 unavailable（不猜字段）
_MINUTE_TIME_FIELDS = ("time", "date_time")
_MINUTE_PRICE_FIELDS = ("price", "close")
_MINUTE_VOLUME_FIELDS = ("volume", "vol")
_QUOTE_PRICE_FIELDS = ("price", "last", "close")
_QUOTE_CHG_PCT_FIELDS = ("change_percent", "chg_pct", "change_pct")
_QUOTE_TIME_FIELDS = ("time", "date", "date_time")


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


def normalize_minute(data: Any) -> tuple[list[dict[str, Any]] | None, str]:
    """受控标准化分时数据。

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


def normalize_quote(data: Any) -> dict[str, Any] | None:
    """受控标准化 quote 缓存：价格 + 可选涨跌幅 + 数据时间。未识别字段不输出。"""
    if not isinstance(data, dict):
        return None
    price = next((data[f] for f in _QUOTE_PRICE_FIELDS if f in data and data[f] is not None), None)
    price = _as_finite_float(price)
    if price is None:
        return None
    change_percent = None
    for field in _QUOTE_CHG_PCT_FIELDS:
        if field in data and data[field] is not None:
            change_percent = _as_finite_float(data[field])
            if change_percent is not None:
                break
    ts = next((data[f] for f in _QUOTE_TIME_FIELDS if f in data), None)
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
            normalized = normalize_quote(envelope.get("data"))
            if normalized is None:
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
                "source": "westock-cache",
                "as_of": None,
                "fetched_at": None,
                "cache_status": "unavailable",
                "is_realtime": False,
                "transport": "westock-cache",
                "availability": {"westock_minute": False},
                "data": None,
                "warnings": ["Westock 分时缓存不存在、过期或非法；不宣称实时"],
            }
        rows, reason = normalize_minute(envelope.get("data"))
        if rows is None:
            return {
                "schema_version": SCHEMA_VERSION,
                "symbol": symbol,
                "source": "westock-cache",
                "as_of": envelope.get("as_of"),
                "fetched_at": envelope.get("fetched_at"),
                "cache_status": "unavailable",
                "is_realtime": False,
                "transport": "westock-cache",
                "availability": {"westock_minute": False},
                "data": None,
                "warnings": [f"Westock 分时数据无法标准化（{reason}），不展示"],
            }
        warnings = ["Westock 缓存导出，非实时"]
        if status == "stale":
            warnings.append("Westock 分时缓存已过期（stale），仅作展示")
        return {
            "schema_version": SCHEMA_VERSION,
            "symbol": symbol,
            "source": "westock-cache",
            "as_of": envelope.get("as_of"),
            "fetched_at": envelope.get("fetched_at"),
            "cache_status": status,  # fresh / stale / unavailable
            "is_realtime": False,
            "transport": "westock-cache",
            "availability": {"westock_minute": True},
            "data": {"rows": rows},
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
