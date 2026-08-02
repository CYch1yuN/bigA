# -*- coding: utf-8 -*-
"""westock 严格旁路核验：BigA 2026-07-31 真实首跑 vs BaoStock curated。

- 数据：westock data_kline 未复权日线（raw，无 qfq/hfq/adjustment_factor）
- 主源：data/curated/daily_quotes_*.parquet 的 2026-07-31 行（BaoStock）
- 阈值：close 2% / volume 10% / amount 10%
- fail-open：westock 不可用只记 unavailable，不改主流程
- 输出：reports/phase-1/validation/westock_20260731_verify.json + 控制台摘要
- 只读 curated，不修改任何主链数据
- volume 手->股换算由 westock_adapter 固化（勿在脚本内重复实现）
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]  # scripts/ 的上一级 = 仓库根
sys.path.insert(0, str(ROOT / "src"))

from ashare_quant.validators import WestockValidator  # noqa: E402
from ashare_quant.validators.westock_adapter import build_fetcher_from_kline  # noqa: E402

RAW_JSON = ROOT / "data" / "raw" / "westock_20260731.json"
OUT_JSON = ROOT / "reports" / "phase-1" / "validation" / "westock_20260731_verify.json"
CURATED_DIR = ROOT / "data" / "curated"

SYMBOLS = [
    "600000.SH", "600036.SH", "600519.SH",
    "000001.SZ", "000002.SZ", "000651.SZ", "002415.SZ", "300750.SZ",
]
TARGET_DATE = date(2026, 7, 31)

CLOSE_TOL = 0.02
VOLUME_TOL = 0.10
AMOUNT_TOL = 0.10


def load_primary(symbol: str) -> pd.DataFrame:
    """读 curated parquet 取目标日行（只读）。"""
    p = CURATED_DIR / f"daily_quotes_{symbol}_2025-06-26_2026-07-31.parquet"
    df = pd.read_parquet(p)
    df = df[df["trade_date"].astype(str) == TARGET_DATE.isoformat()]
    return df.copy()


def max_rel_dev(primary: pd.DataFrame, w_df: pd.DataFrame, col_p: str, col_w: str) -> float | None:
    """按日期对齐计算最大相对偏差 |a-b|/max(|a|,|b|,eps)。"""
    if primary.empty or w_df is None or w_df.empty:
        return None
    p = primary.copy()
    p["_d"] = p["trade_date"].astype(str)
    w = w_df.copy()
    w["_d"] = w["date"].astype(str)
    m = p.merge(w, on="_d", suffixes=("_p", "_w"))
    if m.empty:
        return None
    a_col = col_p if col_p in m.columns else f"{col_p}_p"
    b_col = col_w if col_w in m.columns else f"{col_w}_w"
    denom = m[[a_col, b_col]].abs().max(axis=1).replace(0, 1e-12)
    dev = (m[a_col] - m[b_col]).abs() / denom
    return float(dev.max())


def main() -> None:
    raw = json.loads(RAW_JSON.read_text(encoding="utf-8"))
    fetcher = build_fetcher_from_kline(raw["symbols"])
    validator = WestockValidator(
        close_tolerance=CLOSE_TOL,
        volume_tolerance=VOLUME_TOL,
        amount_tolerance=AMOUNT_TOL,
    )

    per_symbol: dict[str, dict] = {}
    anomalies: list[str] = []
    max_dev_close = 0.0
    max_dev_volume = 0.0
    max_dev_amount = 0.0

    for sym in SYMBOLS:
        primary = load_primary(sym)
        if primary.empty:
            per_symbol[sym] = {"status": "no_local_data", "message": "curated 无 2026-07-31 行"}
            anomalies.append(sym)
            continue
        w_df = fetcher(sym, TARGET_DATE, TARGET_DATE)
        result = validator.validate(primary, sym, TARGET_DATE, TARGET_DATE, fetch=fetcher)

        d_close = max_rel_dev(primary, w_df, "close_raw", "close")
        d_vol = max_rel_dev(primary, w_df, "volume", "volume")
        d_amt = max_rel_dev(primary, w_df, "amount", "amount")
        max_dev_close = max(max_dev_close, d_close or 0.0)
        max_dev_volume = max(max_dev_volume, d_vol or 0.0)
        max_dev_amount = max(max_dev_amount, d_amt or 0.0)

        entry = result.to_dict()
        entry["max_dev"] = {"close": d_close, "volume": d_vol, "amount": d_amt}
        entry["primary_row"] = {
            "close_raw": float(primary.iloc[0]["close_raw"]),
            "volume": float(primary.iloc[0]["volume"]),
            "amount": float(primary.iloc[0]["amount"]),
        }
        entry["westock_row"] = {
            "close": float(w_df.iloc[0]["close"]) if w_df is not None and not w_df.empty else None,
            "volume_shares": float(w_df.iloc[0]["volume"]) if w_df is not None and not w_df.empty else None,
            "amount": float(w_df.iloc[0]["amount"]) if w_df is not None and not w_df.empty else None,
        }
        per_symbol[sym] = entry
        if result.issues:
            anomalies.append(sym)

    report = {
        "title": "westock 严格旁路核验：2026-07-31 真实首跑 vs BaoStock curated",
        "verified_at": pd.Timestamp.now(tz="Asia/Shanghai").isoformat(),
        "validator": "ashare_quant.validators.WestockValidator + westock_adapter",
        "date": TARGET_DATE.isoformat(),
        "thresholds": {
            "close_relative": CLOSE_TOL,
            "volume_relative": VOLUME_TOL,
            "amount_relative": AMOUNT_TOL,
        },
        "boundary": {
            "adjust": "raw only（未复权）",
            "no_qfq_hfq_adjustment": True,
            "no_factor_columns": True,
            "read_only_curated": True,
            "not_in_backtest_or_daily_mainline": True,
            "fail_open": True,
        },
        "volume_unit_note": (
            "westock data_kline volume 原始单位为手(100股)；"
            "由 src/ashare_quant/validators/westock_adapter.py 统一换算为股，"
            "原始响应见 data/raw/westock_20260731.json（Git 忽略）"
        ),
        "per_symbol": per_symbol,
        "summary": {
            "total": len(SYMBOLS),
            "anomalous": anomalies,
            "max_deviation": {
                "close": max_dev_close,
                "volume": max_dev_volume,
                "amount": max_dev_amount,
            },
            "conclusion": (
                f"共 {len(SYMBOLS)} 标的；异常标的 {len(anomalies)} 个（{anomalies or '无'}）。"
                f"最大偏差 close={max_dev_close:.4%} / volume={max_dev_volume:.4%} / amount={max_dev_amount:.4%}"
            ),
        },
    }

    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"== westock 旁路核验 {TARGET_DATE.isoformat()} ==")
    for sym in SYMBOLS:
        e = per_symbol[sym]
        md = e.get("max_dev", {})
        dc = md.get("close")
        dv = md.get("volume")
        da = md.get("amount")
        print(
            f"{sym}: status={e.get('status')} issues={len(e.get('issues', []))} "
            f"| close_dev={dc and f'{dc:.4%}'} "
            f"vol_dev={dv and f'{dv:.4%}'} "
            f"amt_dev={da and f'{da:.4%}'}"
        )
    print(f"异常标的: {anomalies or '无'}")
    print(f"总体: {report['summary']['conclusion']}")
    print(f"报告: {OUT_JSON}")


if __name__ == "__main__":
    main()
