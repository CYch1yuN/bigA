# -*- coding: utf-8 -*-
"""Phase F2-A：minute + technical 真实结构校准测试。

覆盖：
1. stocks_service.normalize_minute（expected_symbol 绑定 + data.data 字符串数组解析）
2. stocks_deep_service._norm_technical（5 组白名单映射 + 双重身份绑定 + date/closePrice 校验）

全部使用 tmp_path / 内联样本，不读取或修改真实 state。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_envelope(tmp_path: Path, capability: str, scope: str, data, tool: str) -> Path:
    env = {
        "schema_version": 1,
        "capability": capability,
        "tool": tool,
        "scope": scope,
        "source": "westock-mcp",
        "transport": "cache_export",
        "as_of": "2026-08-04",
        "fetched_at": _now_iso(),
        "cached_at": _now_iso(),
        "data": data,
        "warnings": [],
    }
    path = tmp_path / "state" / "dashboard" / "westock" / capability / f"{scope}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(env, ensure_ascii=False), encoding="utf-8")
    return path


# ---------------------------------------------------------------------- #
# 1. minute：真实结构解析
# ---------------------------------------------------------------------- #
def _minute_data(rows, date="20260804"):
    return {"sh600519": {"data": {"date": date, "data": rows},
                         "mx_price": {"mx": "", "price": ""},
                         "qt": {"market": [], "sh600519": []}}}


def test_minute_real_sample_parsed():
    """真实样本：外层键匹配 + 字符串数组 → date/rows/单位元数据。"""
    from app.stocks_service import normalize_minute

    data = _minute_data([
        "0930 1350.06 235 31726410.00",
        "0931 1345.02 1225 165111656.00",
    ])
    payload, reason = normalize_minute(data, "600519.SH", [])
    assert reason == "ok"
    assert payload["date"] == "2026-08-04"
    assert payload["price_unit"] == "CNY"
    assert payload["volume_unit"] == "lot"
    assert payload["amount_unit"] == "CNY"
    assert payload["rows"] == [
        {"time": "09:30", "price": 1350.06, "volume": 235, "amount": 31726410.00},
        {"time": "09:31", "price": 1345.02, "volume": 1225, "amount": 165111656.00},
    ]
    # 不解析/不输出 qt、mx_price、原始字符串
    assert "qt" not in payload and "mx_price" not in payload
    for row in payload["rows"]:
        assert set(row) == {"time", "price", "volume", "amount"}


def test_minute_outer_key_mismatch():
    """外层 key 错配 → unavailable。"""
    from app.stocks_service import normalize_minute

    data = {"sh600000": {"data": {"date": "20260804", "data": ["0930 1.0 1 2"]}}}
    payload, reason = normalize_minute(data, "600519.SH", [])
    assert payload is None and reason == "外层股票代码与请求标的不一致"


def test_minute_outer_key_invalid():
    """外层 key 非法（非 sh/sz/bj 前缀）→ unavailable。"""
    from app.stocks_service import normalize_minute

    data = {"foo": {"data": {"date": "20260804", "data": ["0930 1.0 1 2"]}}}
    payload, reason = normalize_minute(data, "600519.SH", [])
    assert payload is None


def test_minute_date_invalid():
    """data.date 非法（非 YYYYMMDD / 不存在日期）→ 整项 unavailable。"""
    from app.stocks_service import normalize_minute

    assert normalize_minute(_minute_data(["0930 1.0 1 2"], "2026080"), "600519.SH", [])[0] is None
    assert normalize_minute(_minute_data(["0930 1.0 1 2"], "20261399"), "600519.SH", [])[0] is None
    assert normalize_minute(_minute_data(["0930 1.0 1 2"], 20260804), "600519.SH", [])[0] is None


def test_minute_row_must_have_exactly_4_fields():
    """行必须恰好 4 段，少/多均丢弃；非字符串丢弃。"""
    from app.stocks_service import normalize_minute

    data = _minute_data([
        "0930 1.0 1",                # 3 段 → 丢弃
        "0930 1.0 1 2 3",            # 5 段 → 丢弃
        12345,                        # 非字符串 → 丢弃
        "0931 2.0 3 4",              # 合法
    ])
    payload, reason = normalize_minute(data, "600519.SH", [])
    assert reason == "ok"
    assert payload["rows"] == [{"time": "09:31", "price": 2.0, "volume": 3, "amount": 4.0}]


def test_minute_time_validation():
    """time 严格 4 位数字、合法 HHMM。"""
    from app.stocks_service import normalize_minute

    data = _minute_data([
        "093X 1.0 1 2",      # 非数字
        "24:00 1.0 1 2",     # 非 HHMM 格式
        "2499 1.0 1 2",      # 小时非法
        "0960 1.0 1 2",      # 分钟非法
        "0930 1.0 1 2",      # 合法
    ])
    payload, reason = normalize_minute(data, "600519.SH", [])
    assert reason == "ok"
    assert payload["rows"] == [{"time": "09:30", "price": 1.0, "volume": 1, "amount": 2.0}]


def test_minute_price_volume_amount_validation():
    """price>0、volume>=0、amount>=0；非法丢弃该行。"""
    from app.stocks_service import normalize_minute

    data = _minute_data([
        "0930 0.0 1 2",       # price 非 >0
        "0931 -1.0 1 2",      # price 负数
        "0932 1.0 -1 2",      # volume 负数
        "0933 1.0 1 -1",      # amount 负数
        "0934 abc 1 2",       # price 垃圾
        "0935 1.0 1 2",       # 合法
    ])
    payload, reason = normalize_minute(data, "600519.SH", [])
    assert reason == "ok"
    assert payload["rows"] == [{"time": "09:35", "price": 1.0, "volume": 1, "amount": 2.0}]


def test_minute_dedupe_last_and_sort():
    """重复 time 保留最后一个有效行；输出按 time 升序。"""
    from app.stocks_service import normalize_minute

    data = _minute_data([
        "1400 5.0 5 50",
        "0930 1.0 1 10",
        "0930 2.0 2 20",   # 重复，覆盖
        "0931 3.0 3 30",
    ])
    payload, reason = normalize_minute(data, "600519.SH", [])
    assert reason == "ok"
    times = [r["time"] for r in payload["rows"]]
    assert times == ["09:30", "09:31", "14:00"]
    assert payload["rows"][0]["price"] == 2.0  # 0930 保留最后


def test_minute_scan_limit_truncates():
    """超过 500 行扫描上限 → 裁剪 + 固定脱敏 warning。"""
    from app.stocks_service import normalize_minute

    rows = [f"{h:02d}{m:02d} {1.0} {1} {2}" for h in range(24) for m in range(60)]
    rows = (rows * 3)[:600]  # 600 行
    warnings: list[str] = []
    payload, reason = normalize_minute(_minute_data(rows), "600519.SH", warnings)
    assert reason == "ok"
    assert len(payload["rows"]) == 500
    assert any("扫描上限" in w and "已裁剪" in w for w in warnings)


def test_minute_all_invalid_unavailable():
    """全空 / 全非法 / 结构未知 → unavailable。"""
    from app.stocks_service import normalize_minute

    assert normalize_minute(_minute_data([]), "600519.SH", [])[0] is None
    assert normalize_minute(_minute_data(["bad line", "also bad"]), "600519.SH", [])[0] is None
    assert normalize_minute({"sh600519": {"qt": {}}}, "600519.SH", [])[0] is None
    assert normalize_minute("not-a-dict", "600519.SH", [])[0] is None


def test_minute_volume_int_vs_float():
    """volume 整数值输出 int，非整数保留 float；amount 恒 float。"""
    from app.stocks_service import normalize_minute

    data = _minute_data(["0930 1.0 100 2.5", "0931 1.0 100.5 3"])
    payload, reason = normalize_minute(data, "600519.SH", [])
    assert reason == "ok"
    row0, row1 = payload["rows"]
    assert row0["volume"] == 100 and isinstance(row0["volume"], int)
    assert row0["amount"] == 2.5 and isinstance(row0["amount"], float)
    assert row1["volume"] == 100.5 and isinstance(row1["volume"], float)
    assert row1["amount"] == 3.0


def test_minute_warning_sanitized():
    """warning 不回显原始行/原始数据。"""
    from app.stocks_service import normalize_minute

    secret = "C:\\secret\\token-xxx"
    data = _minute_data([f"0930 {secret} 1 2", "0931 1.0 1 2"])
    warnings: list[str] = []
    payload, reason = normalize_minute(data, "600519.SH", warnings)
    assert reason == "ok"
    assert secret not in json.dumps(payload, ensure_ascii=False)
    assert all(secret not in w for w in warnings)


# ---------------------------------------------------------------------- #
# 2. minute：服务级整链路
# ---------------------------------------------------------------------- #
def test_minute_service_roundtrip(tmp_path):
    """minute 服务：写真实结构缓存 → minute() → payload 含 date/rows/单位。"""
    from app.stocks_service import CuratedStocksService

    data = {"sh600519": {"data": {"date": "20260804", "data": [
        "0930 1350.06 235 31726410.00", "0931 1345.02 1225 165111656.00"]}}}
    _write_envelope(tmp_path, "minute", "600519.SH", data, "data_minute")
    svc = CuratedStocksService(tmp_path)
    env = svc.minute("600519.SH")
    assert env["cache_status"] == "fresh"
    assert env["availability"]["westock_minute"] is True
    body = env["data"]
    assert body["date"] == "2026-08-04"
    assert len(body["rows"]) == 2
    assert body["rows"][0] == {"time": "09:30", "price": 1350.06, "volume": 235,
                               "amount": 31726410.00}
    assert "qt" not in body and "mx_price" not in body


# ---------------------------------------------------------------------- #
# 3. technical：真实结构标准化
# ---------------------------------------------------------------------- #
def _tech_data(extra=None, code="sh600519", date="2026-08-04"):
    base = {
        "code": code, "name": "贵州茅台", "date": date, "closePrice": 1328.36,
        "ma": {"MA_5": 1344.14, "MA_10": 1322.462, "MA_20": 1281.775, "MA_60": 1255.771,
               "MA_120": 1333.577, "MA_250": 1363.491},
        "macd": {"DIF": 30.871, "DEA": 23.811, "MACD": 14.12},
        "kdj": {"KDJ_K": 77.717, "KDJ_D": 80.633, "KDJ_J": 71.885},
        "rsi": {"RSI_6": 54.912, "RSI_12": 60.105, "RSI_24": 56.762},
        "boll": {"BOLL_UPPER": 1391.671, "BOLL_MID": 1281.775, "BOLL_LOWER": 1171.88},
        "bias": {"BIAS_6": -0.877}, "wr": {"WR_6": 47.393},
        "dmi": {"ADX": 71.991}, "other": {"OBV": -682417},
    }
    if extra:
        base.update(extra)
    return {"sh600519": base}


def test_technical_real_sample_mapped():
    """真实样本：5 组白名单映射正确；bias/wr/dmi/other 与未知字段丢弃。"""
    from app.stocks_deep_service import _norm_technical

    out, reason = _norm_technical(_tech_data(), "600519.SH", [])
    assert reason is None
    assert out["date"] == "2026-08-04"
    assert out["closePrice"] == 1328.36
    assert out["ma"] == {"ma5": 1344.14, "ma10": 1322.462, "ma20": 1281.775, "ma60": 1255.771}
    assert out["macd"] == {"dif": 30.871, "dea": 23.811, "macd": 14.12}
    assert out["kdj"] == {"k": 77.717, "d": 80.633, "j": 71.885}
    assert out["rsi"] == {"rsi6": 54.912, "rsi12": 60.105, "rsi24": 56.762}
    assert out["boll"] == {"upper": 1391.671, "mid": 1281.775, "lower": 1171.88}
    assert set(out) == {"date", "closePrice", "ma", "macd", "kdj", "rsi", "boll"}


def test_technical_outer_key_mismatch():
    """外层 key 错配 → unavailable。"""
    from app.stocks_deep_service import _norm_technical

    data = {"sh600000": {"code": "sh600000", "date": "2026-08-04",
                         "ma": {"MA_5": 1.0}}}
    out, reason = _norm_technical(data, "600519.SH", [])
    assert out is None and reason == "外层股票代码与请求标的不一致"


def test_technical_inner_code_mismatch():
    """内层 code 错配 → unavailable。"""
    from app.stocks_deep_service import _norm_technical

    data = {"sh600519": {"code": "sh600000", "date": "2026-08-04", "ma": {"MA_5": 1.0}}}
    out, reason = _norm_technical(data, "600519.SH", [])
    assert out is None and reason == "code 与请求标的不一致"


def test_technical_date_invalid():
    """date 严格 YYYY-MM-DD；非法 → 整项 unavailable。"""
    from app.stocks_deep_service import _norm_technical

    assert _norm_technical(_tech_data(date="20260804"), "600519.SH", [])[0] is None
    assert _norm_technical(_tech_data(date="2026-13-40"), "600519.SH", [])[0] is None
    assert _norm_technical(_tech_data(date=20260804), "600519.SH", [])[0] is None


def test_technical_rejects_bool_nan_inf():
    """拒绝 bool / NaN / Infinity / dict / list / 垃圾字符串。"""
    from app.stocks_deep_service import _norm_technical

    data = _tech_data({
        "ma": {"MA_5": True, "MA_10": float("nan"), "MA_20": float("inf"),
               "MA_60": "garbage"},
    })
    out, reason = _norm_technical(data, "600519.SH", [])
    assert reason is None
    assert "ma" not in out  # 全部拒绝 → 组空 → 组省略
    # 其他组仍有效
    assert "macd" in out and "kdj" in out


def test_technical_all_groups_invalid():
    """5 组全部无效 → unavailable。"""
    from app.stocks_deep_service import _norm_technical

    data = _tech_data({
        "ma": {"MA_5": "x"}, "macd": {"DIF": True}, "kdj": {"KDJ_K": float("nan")},
        "rsi": {"RSI_6": "x"}, "boll": {"BOLL_UPPER": "x"},
    })
    out, reason = _norm_technical(data, "600519.SH", [])
    assert out is None and reason == "缺少受控指标字段"


def test_technical_partial_group_kept():
    """单组部分字段有效 → 保留有效字段。"""
    from app.stocks_deep_service import _norm_technical

    data = _tech_data({"ma": {"MA_5": 10.0, "MA_10": "bad"}})
    out, reason = _norm_technical(data, "600519.SH", [])
    assert reason is None
    assert out["ma"] == {"ma5": 10.0}


def test_technical_close_price_optional():
    """closePrice 可选：合法保留；非法仅丢弃 + warning；缺失不阻断。"""
    from app.stocks_deep_service import _norm_technical

    warnings: list[str] = []
    bad = _tech_data({"closePrice": -1.0})
    out, _ = _norm_technical(bad, "600519.SH", warnings)
    assert out is not None and "closePrice" not in out
    assert any("closePrice 非法" in w for w in warnings)

    missing = _tech_data(extra=None)
    missing["sh600519"].pop("closePrice")
    out2, _ = _norm_technical(missing, "600519.SH", [])
    assert out2 is not None and "closePrice" not in out2


def test_technical_scalar_not_series():
    """输出为当前时点标量（date + 各组标量），不伪造成时间序列。"""
    from app.stocks_deep_service import _norm_technical

    out, reason = _norm_technical(_tech_data(), "600519.SH", [])
    assert reason is None
    assert isinstance(out["date"], str)
    for group in ("ma", "macd", "kdj", "rsi", "boll"):
        assert all(isinstance(v, (int, float)) for v in out[group].values())
    assert "series" not in out and "points" not in out


def test_technical_service_roundtrip(tmp_path):
    """technical 服务：写真实结构缓存 → technical() → indicators + note + 脱敏。"""
    from app.stocks_deep_service import StocksDeepService

    _write_envelope(tmp_path, "technical", "600519.SH", _tech_data(), "data_technical")
    svc = StocksDeepService(tmp_path)
    env = svc.technical("600519.SH")
    assert env["availability"]["technical"] == "fresh"
    ind = env["data"]["indicators"]
    assert ind["date"] == "2026-08-04"
    assert ind["ma"]["ma5"] == 1344.14
    assert "note" in env["data"]
    assert "BigA 策略与回测使用本地 curated 数据独立计算" in env["data"]["note"]
    raw = json.dumps(env, ensure_ascii=False)
    assert "bias" not in raw and "dmi" not in raw and "other" not in raw


# ---------------------------------------------------------------------- #
# 4. 补充场景：minute 边界补齐
# ---------------------------------------------------------------------- #
def test_minute_data_rows_not_list():
    """data.data 非列表 -> unavailable。"""
    from app.stocks_service import normalize_minute

    data = {"sh600519": {"data": {"date": "20260804", "data": "not-a-list"}}}
    assert normalize_minute(data, "600519.SH", [])[0] is None


def test_minute_nan_infinity_rows_dropped():
    """行内 NaN/Infinity 数值 -> 该行丢弃。"""
    from app.stocks_service import normalize_minute

    data = _minute_data([
        "0930 NaN 1 2",
        "0931 inf 1 2",
        "0932 1.0 -Infinity 2",
        "0933 1.0 1 NaN",
        "0934 5.0 5 50",
    ])
    payload, reason = normalize_minute(data, "600519.SH", [])
    assert reason == "ok"
    assert payload["rows"] == [{"time": "09:34", "price": 5.0, "volume": 5, "amount": 50.0}]


# ---------------------------------------------------------------------- #
# 5. 补充场景：technical 边界补齐
# ---------------------------------------------------------------------- #
def test_technical_outer_key_invalid():
    """外层 key 非法（非 sh/sz/bj 前缀）-> unavailable。"""
    from app.stocks_deep_service import _norm_technical

    data = {"foo": {"code": "sh600519", "date": "2026-08-04", "ma": {"MA_5": 1.0}}}
    out, reason = _norm_technical(data, "600519.SH", [])
    assert out is None


def test_technical_inner_code_invalid():
    """内层 code 非法（无法解析）-> 身份冲突 -> unavailable。"""
    from app.stocks_deep_service import _norm_technical

    data = {"sh600519": {"code": "bad-code", "date": "2026-08-04", "ma": {"MA_5": 1.0}}}
    out, reason = _norm_technical(data, "600519.SH", [])
    assert out is None and reason == "code 与请求标的不一致"


def test_technical_rejects_dict_list_values():
    """组内 dict/list 值 -> 丢弃。"""
    from app.stocks_deep_service import _norm_technical

    data = _tech_data({"ma": {"MA_5": {"a": 1}, "MA_10": [1.0], "MA_20": 20.0}})
    out, reason = _norm_technical(data, "600519.SH", [])
    assert reason is None
    assert out["ma"] == {"ma20": 20.0}


def test_technical_unknown_group_not_output():
    """未知组（mystery_indicator）不输出。"""
    from app.stocks_deep_service import _norm_technical

    data = _tech_data({"mystery_indicator": {"a": 1}})
    out, reason = _norm_technical(data, "600519.SH", [])
    assert reason is None
    assert "mystery_indicator" not in out


def test_technical_unknown_fields_dropped():
    """组内未知字段（MA_999/hacked）丢弃，仅白名单输出。"""
    from app.stocks_deep_service import _norm_technical

    data = _tech_data({"ma": {"MA_5": 1.0, "MA_999": 2.0, "hacked": 3.0}})
    out, reason = _norm_technical(data, "600519.SH", [])
    assert reason is None
    assert out["ma"] == {"ma5": 1.0}


def test_technical_warning_sanitized():
    """technical warning 不回显原始身份值。"""
    from app.stocks_deep_service import _norm_technical

    secret = "C:\\secret\\token-xxx"
    data = {"sh600519": {"code": "sh600519", "date": "2026-08-04",
                         "closePrice": -5.0, "ma": {"MA_5": 1.0}}}
    warnings: list[str] = []
    out, _ = _norm_technical(data, "600519.SH", warnings)
    assert out is not None
    assert all(secret not in w for w in warnings)
    assert secret not in json.dumps(out, ensure_ascii=False)


def test_technical_readonly_no_state_change(tmp_path):
    """调用前后 curated/signals/orders/accounts/Gate4B 文件哈希不变（只读）。"""
    import hashlib

    from app.stocks_deep_service import StocksDeepService

    root = tmp_path / "repo"
    (root / "data" / "curated").mkdir(parents=True)
    (root / "data" / "curated" / "daily_quotes_600519.SH_x.parquet").write_bytes(b"px")
    (root / "reports" / "phase-4" / "daily" / "2026-08-04").mkdir(parents=True)
    (root / "reports" / "phase-4" / "daily" / "2026-08-04" / "signals.json").write_text(
        '{"as_of_date": "2026-08-04"}', encoding="utf-8")
    (root / "state" / "automation").mkdir(parents=True)
    (root / "state" / "automation" / "accounts.json").write_text('{"paper": {}}', encoding="utf-8")
    (root / "reports" / "phase-4" / "gate4b").mkdir(parents=True)
    (root / "reports" / "phase-4" / "gate4b" / "summary.json").write_text("{}", encoding="utf-8")
    _write_envelope(root, "technical", "600519.SH", _tech_data(), "data_technical")

    def tree_hash(base: Path) -> str:
        h = hashlib.sha256()
        for p in sorted(base.rglob("*")):
            if p.is_file():
                h.update(p.relative_to(base).as_posix().encode())
                h.update(p.read_bytes())
        return h.hexdigest()

    before = tree_hash(root)
    deep = StocksDeepService(root)
    env = deep.technical("600519.SH")
    assert env["data"]["indicators"] is not None
    assert tree_hash(root) == before


# ---------------------------------------------------------------------- #
# 6. 真实缓存只读 E2E（state/dashboard/westock 为 ignored 缓存，只读不删）
# ---------------------------------------------------------------------- #
_REPO_ROOT = Path(__file__).resolve().parents[3]  # tests -> backend -> dashboard -> 仓库根
_REAL_WESTOCK = _REPO_ROOT / "state" / "dashboard" / "westock"


def _has_real_cache(capability: str, scope: str) -> bool:
    return (_REAL_WESTOCK / capability / f"{scope}.json").exists()


@pytest.mark.skipif(not _has_real_cache("minute", "600519.SH"),
                    reason="真实 minute 缓存缺失，跳过 E2E")
def test_e2e_real_minute_cache_readonly():
    """真实 minute 缓存只读 E2E：rows>0、日期/time 合法、单位正确、非实时、cache_export。"""
    from app.stocks_service import CuratedStocksService

    svc = CuratedStocksService(_REPO_ROOT)
    env = svc.minute("600519.SH")
    assert env["is_realtime"] is False
    # 直接断言 API 公开字段（不读底层 envelope）
    assert env["source"] == "westock-mcp"
    assert env["transport"] == "cache_export"
    assert env["availability"]["westock_minute"] is True
    body = env["data"]
    assert body is not None
    rows = body["rows"]
    assert len(rows) > 0
    assert body["date"] == "2026-08-04"
    assert body["price_unit"] == "CNY" and body["volume_unit"] == "lot" \
        and body["amount_unit"] == "CNY"
    for row in rows:
        assert set(row) == {"time", "price", "volume", "amount"}
        hh, mm = row["time"].split(":")
        assert 0 <= int(hh) <= 23 and 0 <= int(mm) <= 59


@pytest.mark.skipif(not _has_real_cache("technical", "600519.SH"),
                    reason="真实 technical 缓存缺失，跳过 E2E")
def test_e2e_real_technical_cache_readonly():
    """真实 technical 缓存只读 E2E：至少一组有效、只含 5 组白名单、note 存在。"""
    from app.stocks_deep_service import StocksDeepService

    deep = StocksDeepService(_REPO_ROOT)
    env = deep.technical("600519.SH")
    assert env["availability"]["technical"] in ("fresh", "stale")
    ind = env["data"]["indicators"]
    assert ind is not None
    groups = set(ind) - {"date", "closePrice"}
    assert groups  # 至少一组有效
    assert groups <= {"ma", "macd", "kdj", "rsi", "boll"}
    assert "note" in env["data"]
    assert "BigA 策略与回测使用本地 curated 数据独立计算" in env["data"]["note"]


# ---------------------------------------------------------------------- #
# 7. monkeypatch 隔离缓存验证缺失降级（不物理删除缓存）
# ---------------------------------------------------------------------- #
def test_minute_missing_does_not_affect_history_snapshot(monkeypatch):
    """minute 缺失（monkeypatch read->None）-> minute unavailable；history/snapshot 正常。"""
    from app.stocks_service import CuratedStocksService

    svc = CuratedStocksService(_REPO_ROOT)
    orig_read = svc.westock_store.read

    def fake_read(capability, scope="global"):
        if capability == "minute":
            return None
        return orig_read(capability, scope)

    monkeypatch.setattr(svc.westock_store, "read", fake_read)
    env = svc.minute("600519.SH")
    assert env["cache_status"] == "unavailable"
    assert env["data"] is None
    # 本地 curated 不依赖 minute 缓存
    hist = svc.history("600519.SH", "none", "1y", None)
    assert hist["cache_status"] == "available"
    snap = svc.snapshot("600519.SH")
    assert snap["cache_status"] == "available"


def test_technical_missing_does_not_affect_other_deep(monkeypatch):
    """technical 缺失（monkeypatch read->None）-> technical unavailable；其他深度 API 正常。"""
    from app.stocks_deep_service import StocksDeepService

    deep = StocksDeepService(_REPO_ROOT)
    orig_read = deep.curated.westock_store.read

    def fake_read(capability, scope="global"):
        if capability == "technical":
            return None
        return orig_read(capability, scope)

    monkeypatch.setattr(deep.curated.westock_store, "read", fake_read)
    env = deep.technical("600519.SH")
    assert env["availability"]["technical"] == "unavailable"
    # fundamentals（profile 已校准、真实缓存存在）不受 technical 缺失影响
    fund = deep.fundamentals("600519.SH")
    assert fund["availability"]["profile"] in ("fresh", "stale")
    assert fund["data"]["profile"] is not None


# ---------------------------------------------------------------------- #
# 8. F2-A 第一轮审核定点修正：严格 wrapper / 计数 warning / 公开字段
# ---------------------------------------------------------------------- #
def test_minute_flat_payload_rejected():
    """minute flat payload（无 wrapper 单键）→ 拒绝（严格 wrapper 要求）。"""
    from app.stocks_service import normalize_minute

    flat = {"code": "sh600519", "data": {"date": "20260804",
                                         "data": ["0930 1.0 1 2"]}}
    payload, reason = normalize_minute(flat, "600519.SH", [])
    assert payload is None and reason == "外层股票代码与请求标的不一致"


def test_technical_flat_payload_rejected():
    """technical flat payload（无 wrapper 单键）→ 拒绝。"""
    from app.stocks_deep_service import _norm_technical

    flat = {"code": "sh600519", "date": "2026-08-04", "ma": {"MA_5": 1.0}}
    out, reason = _norm_technical(flat, "600519.SH", [])
    assert out is None and reason == "外层股票代码与请求标的不一致"


def test_technical_wrapper_missing_inner_code():
    """technical wrapper 缺少内层 code → 拒绝（code 必须存在）。"""
    from app.stocks_deep_service import _norm_technical

    data = {"sh600519": {"date": "2026-08-04", "ma": {"MA_5": 1.0}}}
    out, reason = _norm_technical(data, "600519.SH", [])
    assert out is None and reason == "code 与请求标的不一致"


def test_technical_wrapper_valid_code_still_ok():
    """合法 wrapper + 内层 code → 仍通过。"""
    from app.stocks_deep_service import _norm_technical

    data = {"sh600519": {"code": "sh600519", "date": "2026-08-04",
                         "ma": {"MA_5": 1.0}}}
    out, reason = _norm_technical(data, "600519.SH", [])
    assert reason is None and out["ma"] == {"ma5": 1.0}


def test_minute_invalid_and_duplicate_counts():
    """minute 统计非法行数与重复 time 数；warning 固定脱敏不含原始行。"""
    from app.stocks_service import normalize_minute

    data = _minute_data([
        "0930 1.0 1 2",
        "bad line",             # 非法行
        12345,                  # 非法行
        "0930 2.0 3 4",         # 重复 0930（保留最后）
        "0931 3.0 5 6",
    ])
    warnings: list[str] = []
    payload, reason = normalize_minute(data, "600519.SH", warnings)
    assert reason == "ok"
    assert any("2 行无法解析" in w for w in warnings)
    assert any("1 个重复时间" in w for w in warnings)
    # 0930 保留最后一个有效行
    assert payload["rows"][0] == {"time": "09:30", "price": 2.0, "volume": 3, "amount": 4.0}
    # 脱敏：不含原始行内容
    assert all("bad line" not in w for w in warnings)


def test_minute_all_invalid_service_keeps_counts(tmp_path):
    """minute 全部无效时，服务响应 unavailable 且保留计数 warning；公开字段正确。"""
    from app.stocks_service import CuratedStocksService

    data = {"sh600519": {"data": {"date": "20260804",
                                  "data": ["bad", "also bad", 1]}}}
    _write_envelope(tmp_path, "minute", "600519.SH", data, "data_minute")
    svc = CuratedStocksService(tmp_path)
    env = svc.minute("600519.SH")
    assert env["cache_status"] == "unavailable"
    assert env["data"] is None
    assert env["is_realtime"] is False
    assert env["source"] == "westock-mcp"
    assert env["transport"] == "cache_export"
    assert any("3 行无法解析" in w for w in env["warnings"])
    assert any("无法标准化" in w for w in env["warnings"])
