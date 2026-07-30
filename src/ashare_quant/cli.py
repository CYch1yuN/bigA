"""命令行接口。

支持：
- init-config：查看或初始化配置
- fetch：抓取指定股票和日期范围（需数据源 SDK）
- standardize：标准化本地原始数据
- quality：执行质量检查
- manifest：生成数据版本清单
- run-example：运行完整 Phase 1 离线示例
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from . import __version__
from .config import default_config_path, load_config
from .constants import SOURCE_AKSHARE, SOURCE_BAOSTOCK
from .manifest import build_manifest, write_manifest, get_code_commit
from .providers import AKShareProvider, BaoStockProvider
from .quality import QualityChecker
from .reports import (
    generate_coverage_report,
    generate_quality_reports,
    generate_reproducibility_report,
)
from .samples import make_normal_raw, make_trade_calendar
from .standardize import Standardizer, content_hash
from .storage import Storage


def _provider(name: str):
    if name == SOURCE_AKSHARE:
        return AKShareProvider()
    if name == SOURCE_BAOSTOCK:
        return BaoStockProvider()
    raise ValueError(f"未知数据源: {name}")


def cmd_init_config(args: argparse.Namespace) -> int:
    """初始化或查看配置。"""
    src = default_config_path()
    if args.output:
        dst = Path(args.output)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        print(f"配置已写入: {dst}")
    else:
        print(src.read_text(encoding="utf-8"))
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    """抓取指定股票和日期范围（需数据源 SDK）。"""
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    provider = _provider(args.source)
    raw = provider.fetch_daily_quotes(args.symbol, start, end)
    storage = Storage(args.data_dir)
    fname = f"{args.source}_{args.symbol}_{args.start}_{args.end}.parquet"
    path = storage.write_generic_parquet(raw, fname, layer="raw")
    print(f"已抓取 {len(raw)} 行原始数据 -> {path}")
    return 0


def cmd_standardize(args: argparse.Namespace) -> int:
    """标准化本地原始数据。"""
    raw = pd.read_parquet(args.input)
    config = load_config(args.config) if args.config else load_config(default_config_path())
    std = Standardizer()
    curated = std.standardize_daily_quotes(raw, args.source)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    curated.to_parquet(out_path, index=False)
    print(f"已标准化 {len(curated)} 行 -> {out_path}")
    print(f"内容哈希(排除 fetched_at): {content_hash(curated, config.manifest.content_hash_exclude_fields)}")
    return 0


def cmd_quality(args: argparse.Namespace) -> int:
    """执行质量检查；严重问题返回非零退出码。"""
    config = load_config(args.config) if args.config else load_config(default_config_path())
    df = pd.read_parquet(args.input)
    sm = pd.read_parquet(args.security_master) if args.security_master else None
    cal = pd.read_parquet(args.calendar) if args.calendar else None
    other = pd.read_parquet(args.cross_source) if args.cross_source else None

    checker = QualityChecker(config)
    report = checker.run(df, sm, cal, other)

    reports_dir = Path(args.reports_dir or config.paths.reports_dir)
    json_path, md_path = generate_quality_reports(report, reports_dir)
    print(f"JSON 报告: {json_path}")
    print(f"Markdown 报告: {md_path}")
    print(f"严重: {report.counts()['critical']}  警告: {report.counts()['warning']}")
    if report.has_critical:
        print("存在严重质量问题，阻止下游处理。", file=sys.stderr)
        return 1
    return 0


def cmd_manifest(args: argparse.Namespace) -> int:
    """生成数据版本清单。"""
    config = load_config(args.config) if args.config else load_config(default_config_path())
    df = pd.read_parquet(args.input) if args.input else pd.DataFrame()
    files = {}
    for spec in args.files or []:
        name, _, path = spec.partition("=")
        files[name] = path
    manifest = build_manifest(
        source=args.source,
        symbol=args.symbol,
        start_date=args.start,
        end_date=args.end,
        row_count=int(len(df)),
        files=files,
        config=config,
        schema_version=config.schema_versions.daily_quote_version,
        content_hash_value=content_hash(df, config.manifest.content_hash_exclude_fields) if not df.empty else None,
        code_commit=get_code_commit(),
    )
    out_path = Path(args.output)
    write_manifest(manifest, out_path)
    print(f"清单已写入: {out_path}")
    return 0


def cmd_run_example(args: argparse.Namespace) -> int:
    """运行完整 Phase 1 离线示例，生成全部报告。"""
    config = load_config(args.config) if args.config else load_config(default_config_path())
    reports_dir = Path(args.reports_dir or config.paths.reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)

    # 使用临时数据目录（示例不污染正式 data/）
    storage = Storage(args.data_dir or "data/example")
    std = Standardizer()

    # 1. 合成原始数据（确定性）—— 用于复现性与清单
    raw = make_normal_raw(symbol="000001", n_days=30)
    raw_path = storage.write_generic_parquet(raw, "example_raw.parquet", layer="raw")

    # 2. 标准化
    curated = std.standardize_daily_quotes(raw, SOURCE_AKSHARE)
    curated_path = storage.write_daily_quotes_parquet(curated, "example_curated.parquet")

    # 3. 交易日历
    cal = make_trade_calendar(date(2024, 1, 2), date(2024, 3, 15))
    cal_path = storage.write_generic_parquet(cal, "example_calendar.parquet", layer="metadata")

    # 4. 质量检查：在干净数据上注入一个异常价格跳变（warning），演示报告可捕获问题但不阻止下游
    demo_raw = raw.copy()
    mid = len(demo_raw) // 2
    prev_c = float(demo_raw.loc[mid - 1, "__raw_close"])
    # 整日缩放保持 OHLC 关系正确，仅制造 >20% 的日间跳变
    demo_raw.loc[mid, "__raw_open"] = prev_c * 1.40
    demo_raw.loc[mid, "__raw_high"] = prev_c * 1.42
    demo_raw.loc[mid, "__raw_low"] = prev_c * 1.38
    demo_raw.loc[mid, "__raw_close"] = prev_c * 1.41
    prev_q = float(demo_raw.loc[mid - 1, "__qfq_close"])
    demo_raw.loc[mid, "__qfq_open"] = prev_q * 1.40
    demo_raw.loc[mid, "__qfq_high"] = prev_q * 1.42
    demo_raw.loc[mid, "__qfq_low"] = prev_q * 1.38
    demo_raw.loc[mid, "__qfq_close"] = prev_q * 1.41
    demo_curated = std.standardize_daily_quotes(demo_raw, SOURCE_AKSHARE)
    checker = QualityChecker(config)
    report = checker.run(demo_curated, trade_calendar=cal)
    generate_quality_reports(report, reports_dir)

    # 5. 复现性：第二次标准化并比较哈希
    curated2 = std.standardize_daily_quotes(raw, SOURCE_AKSHARE)
    h1 = content_hash(curated, config.manifest.content_hash_exclude_fields)
    h2 = content_hash(curated2, config.manifest.content_hash_exclude_fields)

    # 6. 清单
    manifest = build_manifest(
        source=SOURCE_AKSHARE,
        symbol="000001",
        start_date="2024-01-02",
        end_date="2024-03-15",
        row_count=int(len(curated)),
        files={"raw": raw_path, "curated": curated_path, "calendar": cal_path},
        config=config,
        schema_version=config.schema_versions.daily_quote_version,
        content_hash_value=h1,
        code_commit=get_code_commit(),
    )
    manifest_path = reports_dir / "manifest.example.json"
    write_manifest(manifest, manifest_path)

    # 7. 复现性报告
    generate_reproducibility_report(h1, h2, manifest_path, reports_dir)

    # 8. 覆盖报告（若存在 coverage.json）
    cov_json = reports_dir / "coverage.json"
    if cov_json.exists():
        generate_coverage_report(cov_json, reports_dir)
    else:
        generate_coverage_report(None, reports_dir)

    print(f"示例完成。报告目录: {reports_dir}")
    print(f"  quality-report.json / quality-report.md")
    print(f"  reproducibility.md")
    print(f"  manifest.example.json")
    print(f"内容哈希一致: {h1 == h2}")
    print(f"质量退出码: {report.exit_code}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ashare-quant",
        description=f"A 股双轨量化研究系统 - Phase 1 数据层 v{__version__}",
    )
    sub = p.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init-config", help="查看或初始化配置")
    p_init.add_argument("--output", help="输出配置文件路径")
    p_init.set_defaults(func=cmd_init_config)

    p_fetch = sub.add_parser("fetch", help="抓取指定股票和日期范围")
    p_fetch.add_argument("--symbol", required=True)
    p_fetch.add_argument("--start", required=True, help="YYYY-MM-DD")
    p_fetch.add_argument("--end", required=True, help="YYYY-MM-DD")
    p_fetch.add_argument("--source", default=SOURCE_AKSHARE, choices=[SOURCE_AKSHARE, SOURCE_BAOSTOCK])
    p_fetch.add_argument("--data-dir", default="data")
    p_fetch.set_defaults(func=cmd_fetch)

    p_std = sub.add_parser("standardize", help="标准化本地原始数据")
    p_std.add_argument("--input", required=True, help="原始 Parquet 路径")
    p_std.add_argument("--output", required=True, help="curated Parquet 输出路径")
    p_std.add_argument("--source", required=True, choices=[SOURCE_AKSHARE, SOURCE_BAOSTOCK])
    p_std.add_argument("--config", help="配置文件路径")
    p_std.set_defaults(func=cmd_standardize)

    p_q = sub.add_parser("quality", help="执行质量检查")
    p_q.add_argument("--input", required=True, help="curated Parquet 路径")
    p_q.add_argument("--security-master", help="证券主数据 Parquet")
    p_q.add_argument("--calendar", help="交易日历 Parquet")
    p_q.add_argument("--cross-source", help="另一数据源 curated Parquet（跨源比较）")
    p_q.add_argument("--config", help="配置文件路径")
    p_q.add_argument("--reports-dir", help="报告输出目录")
    p_q.set_defaults(func=cmd_quality)

    p_m = sub.add_parser("manifest", help="生成数据版本清单")
    p_m.add_argument("--input", help="curated Parquet 路径")
    p_m.add_argument("--source", required=True)
    p_m.add_argument("--symbol", required=True)
    p_m.add_argument("--start", required=True)
    p_m.add_argument("--end", required=True)
    p_m.add_argument("--files", nargs="*", help="文件清单 name=path ...")
    p_m.add_argument("--output", required=True)
    p_m.add_argument("--config", help="配置文件路径")
    p_m.set_defaults(func=cmd_manifest)

    p_ex = sub.add_parser("run-example", help="运行完整 Phase 1 离线示例")
    p_ex.add_argument("--config", help="配置文件路径")
    p_ex.add_argument("--reports-dir", help="报告输出目录")
    p_ex.add_argument("--data-dir", help="示例数据目录")
    p_ex.set_defaults(func=cmd_run_example)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
