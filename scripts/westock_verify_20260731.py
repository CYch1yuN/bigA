# -*- coding: utf-8 -*-
"""westock 严格旁路核验脚本（参数化）。

用法：
    python scripts/westock_verify_20260731.py                          # 默认复现 2026-07-31 8 标的
    python scripts/westock_verify_20260731.py --date 2026-07-31
    python scripts/westock_verify_20260731.py --raw-json <path> --output <path>
    python scripts/westock_verify_20260731.py --symbols 600519.SH,000001.SZ

边界：
- westock data_kline 未复权（raw），无 qfq/hfq/adjustment_factor
- 阈值 close 2% / volume 10% / amount 10%；fail-open
- 只读 curated，不修改主链数据；volume 手->股换算由 westock_adapter 固化
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]  # scripts/ 的上一级 = 仓库根
sys.path.insert(0, str(ROOT / "src"))

from ashare_quant.validators import WestockValidator  # noqa: E402
from ashare_quant.validators.westock_adapter import build_fetcher_from_kline  # noqa: E402

DEFAULT_SYMBOLS = [
    "600000.SH", "600036.SH", "600519.SH",
    "000001.SZ", "000002.SZ", "000651.SZ", "002415.SZ", "300750.SZ",
]
CLOSE_TOL = 0.02
VOLUME_TOL = 0.10
AMOUNT_TOL = 0.10


def default_raw_json(target: date) -> Path:
    return ROOT / "data" / "raw" / f"westock_{target:%Y%m%d}.json"


def default_output(target: date) -> Path:
    return ROOT / "reports" / "phase-1" / "validation" / f"westock_{target:%Y%m%d}_verify.json"


def load_primary(symbol: str, target: date) -> pd.DataFrame:
    """读 curated parquet 取目标日行（只读）。

    按 mtime 从新到旧扫描全部候选文件，选择**第一个实际包含目标日期**的；
    若所有候选都不含目标日期，抛出明确错误并列出已检查文件。
    """
    cands = sorted(
        (ROOT / "data" / "curated").glob(f"daily_quotes_{symbol}_*.parquet"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not cands:
        raise FileNotFoundError(
            f"curated 无 {symbol} 的 parquet 文件（已检查 data/curated/）"
        )
    for cand in cands:
        df = pd.read_parquet(cand)
        sub = df[df["trade_date"].astype(str) == target.isoformat()]
        if not sub.empty:
            return sub.copy()
    checked = ", ".join(p.name for p in cands)
    raise FileNotFoundError(
        f"curated 所有候选均不含 {target.isoformat()}：{symbol}（已检查: {checked}）"
    )


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


def fmt_pct(v: float | None) -> str:
    return f"{v:.4%}" if v is not None else "-"


def main() -> None:
    ap = argparse.ArgumentParser(description="westock 旁路核验")
    ap.add_argument("--date", type=lambda s: date.fromisoformat(s), default=date(2026, 7, 31))
    ap.add_argument("--raw-json", type=Path, default=None)
    ap.add_argument("--output", type=Path, default=None)
    ap.add_argument("--symbols", type=str, default=None, help="逗号分隔标的，默认 8 标的")
    args = ap.parse_args()

    target: date = args.date
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] if args.symbols else DEFAULT_SYMBOLS
    raw_json = args.raw_json or default_raw_json(target)
    out_json = args.output or default_output(target)

    # 保护 canonical 报告：--symbols 子集且未显式 --output 时拒绝执行，
    # 防止子集结果误覆盖正式 8 标的报告。
    if args.symbols and args.output is None:
        print(
            "[FAIL] --symbols 子集核验必须显式提供 --output，"
            f"避免覆盖 canonical 报告 {out_json.name}（默认输出仅用于完整标的集）",
            file=sys.stderr,
        )
        sys.exit(2)

    if not raw_json.is_file():
        print(f"[FAIL] 缺少原始响应: {raw_json}（请先用 westock data_kline 拉取并留档）", file=sys.stderr)
        sys.exit(2)

    raw = json.loads(raw_json.read_text(encoding="utf-8"))
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

    for sym in symbols:
        try:
            primary = load_primary(sym, target)
        except FileNotFoundError as exc:
            per_symbol[sym] = {"status": "no_local_data", "message": str(exc)}
            anomalies.append(sym)
            continue
        if primary.empty:
            per_symbol[sym] = {"status": "no_local_data", "message": f"curated 无 {target.isoformat()} 行"}
            anomalies.append(sym)
            continue
        w_df = fetcher(sym, target, target)
        result = validator.validate(primary, sym, target, target, fetch=fetcher)

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
        "title": f"westock 严格旁路核验：{target.isoformat()} vs BaoStock curated",
        "verified_at": datetime.now().astimezone().isoformat(),
        "validator": "ashare_quant.validators.WestockValidator + westock_adapter",
        "date": target.isoformat(),
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
            "由 src/ashare_quant/validators/westock_adapter.py 统一换算为股 "
            "(source_unit=lot, normalized_unit=share, multiplier=100)；"
            f"原始响应见 {raw_json.name}（Git 忽略）"
        ),
        "per_symbol": per_symbol,
        "summary": {
            "total": len(symbols),
            "anomalous": anomalies,
            "max_deviation": {
                "close": max_dev_close,
                "volume": max_dev_volume,
                "amount": max_dev_amount,
            },
            "conclusion": (
                f"共 {len(symbols)} 标的；异常标的 {len(anomalies)} 个（{anomalies or '无'}）。"
                f"最大偏差 close={fmt_pct(max_dev_close)} / volume={fmt_pct(max_dev_volume)} / amount={fmt_pct(max_dev_amount)}"
            ),
        },
    }

    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"== westock 旁路核验 {target.isoformat()} ==")
    for sym in symbols:
        e = per_symbol[sym]
        md = e.get("max_dev", {})
        print(
            f"{sym}: status={e.get('status')} issues={len(e.get('issues', []))} "
            f"| close_dev={fmt_pct(md.get('close'))} "
            f"vol_dev={fmt_pct(md.get('volume'))} "
            f"amt_dev={fmt_pct(md.get('amount'))}"
        )
    print(f"异常标的: {anomalies or '无'}")
    print(f"总体: {report['summary']['conclusion']}")
    print(f"报告: {out_json}")


if __name__ == "__main__":
    main()
