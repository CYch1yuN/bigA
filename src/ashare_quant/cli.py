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
import shutil
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Optional

import pandas as pd

from . import __version__
from .config import default_config_path, load_config
from .constants import SOURCE_AKSHARE, SOURCE_BAOSTOCK
from .manifest import build_manifest, write_manifest, get_code_commit
from .quality import QualityChecker
from .reports import (
    generate_coverage_report,
    generate_quality_reports,
    generate_reproducibility_report,
)
from .samples import make_normal_raw, make_trade_calendar
from .standardize import Standardizer, content_hash
from .storage import Storage, file_sha256


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
    """抓取指定股票和日期范围（需数据源 SDK）。

    使用 FetchManager 实现重试与自动回退；无论成功或失败都生成包含
    尝试记录、最终数据源和文件哈希的原始抓取清单。
    """
    config = load_config(args.config) if args.config else load_config(default_config_path())
    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    from .fetcher import FetchManager, build_fetch_manifest

    manager = FetchManager(config)
    result = manager.fetch_daily_quotes(
        symbol=args.symbol,
        start_date=start,
        end_date=end,
        source=args.source,
        allow_fallback=not args.no_fallback,
    )

    storage = Storage(args.data_dir)

    if result.success:
        source_tag = result.final_source or config.providers.primary
        fname = f"{source_tag}_{args.symbol}_{args.start}_{args.end}.parquet"
        path = storage.write_generic_parquet(result.data, fname, layer="raw")
        file_hash = file_sha256(path)
    else:
        path = None
        file_hash = None

    # 无论成功或失败都生成原始抓取清单
    source_tag = result.final_source or args.source or config.providers.primary
    manifest = build_fetch_manifest(
        symbol=args.symbol,
        start_date=args.start,
        end_date=args.end,
        result=result,
        file_path=path,
        file_hash=file_hash,
        config=config,
        schema_version=config.schema_versions.daily_quote_version,
        code_commit=get_code_commit(),
    )
    manifest_name = f"{source_tag}_{args.symbol}_{args.start}_{args.end}.manifest.json"
    manifest_path = storage.write_generic_json(manifest, manifest_name, layer="metadata")

    if not result.success:
        print(f"抓取失败: {result.error}", file=sys.stderr)
        for a in result.attempts:
            status = "成功" if a.success else "失败"
            print(
                f"  尝试 {a.attempt_number}: {a.source} -> {status}"
                + (f" ({a.error})" if a.error else ""),
                file=sys.stderr,
            )
        print(f"失败抓取清单: {manifest_path}", file=sys.stderr)
        return 1

    print(f"已抓取 {len(result.data)} 行原始数据 -> {path}")
    print(f"最终数据源: {result.final_source}")
    print(f"尝试记录: {len(result.attempts)} 次")
    print(f"原始抓取清单: {manifest_path}")
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


def cmd_backtest(args: argparse.Namespace) -> int:
    """运行回测（Phase 2）。

    读取 curated 日行情、配置和信号，运行事件驱动回测，
    输出 JSON 结果、Markdown 报告、订单/成交/权益 Parquet。
    """
    import json as json_mod

    from .backtest.config import BacktestConfig, load_backtest_config, default_backtest_config_path
    from .backtest.engine import BacktestEngine
    from .backtest.strategies import ScriptedStrategy, NoOpStrategy
    from .backtest.report import ReportGenerator

    # 加载配置
    if args.config:
        bt_config = load_backtest_config(args.config)
    else:
        bt_config = load_backtest_config(default_backtest_config_path())

    # 加载行情数据
    quotes = pd.read_parquet(args.quotes)

    # 加载策略
    if args.strategy == "scripted":
        if not args.signals:
            print("错误: scripted 策略需要 --signals 参数", file=sys.stderr)
            return 1
        strategy = ScriptedStrategy.from_json(args.signals)
    elif args.strategy == "noop":
        strategy = NoOpStrategy()
    else:
        print(f"错误: 未知策略 {args.strategy}", file=sys.stderr)
        return 1

    # 确定回测区间
    start = date.fromisoformat(args.start) if args.start else quotes["trade_date"].min()
    end = date.fromisoformat(args.end) if args.end else quotes["trade_date"].max()
    if not isinstance(start, date):
        start = pd.Timestamp(start).date()
    if not isinstance(end, date):
        end = pd.Timestamp(end).date()

    # 运行回测
    engine = BacktestEngine()
    result = engine.run(
        data=quotes,
        strategy=strategy,
        start_date=start,
        end_date=end,
        initial_cash=bt_config.initial_cash,
        config=bt_config,
    )

    # 设置代码提交号（运行元数据，不参与 content_hash）
    result.code_commit = get_code_commit()

    # 验证账务恒等式
    for snap in result.daily_equity:
        expected = snap.cash + snap.position_value
        if abs(expected - snap.total_equity) > Decimal("0.01"):
            print(
                f"错误: 账务不平等 {snap.snapshot_date}: "
                f"现金({snap.cash}) + 持仓({snap.position_value}) != 权益({snap.total_equity})",
                file=sys.stderr,
            )
            return 1

    # 生成报告
    report_gen = ReportGenerator()
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # JSON 结果
    json_result = report_gen.generate_json(result, Decimal(str(bt_config.initial_cash)))
    json_path = output_dir / "backtest-result.json"
    with json_path.open("w", encoding="utf-8") as f:
        json_mod.dump(json_result, f, ensure_ascii=False, indent=2, default=str)

    # Markdown 报告
    md_report = report_gen.generate_markdown(result, Decimal(str(bt_config.initial_cash)))
    md_path = output_dir / "backtest-report.md"
    md_path.write_text(md_report, encoding="utf-8")

    # Parquet 文件
    orders_df = report_gen.generate_orders_dataframe(result)
    orders_df.to_parquet(output_dir / "orders.parquet", index=False)

    fills_df = report_gen.generate_fills_dataframe(result)
    fills_df.to_parquet(output_dir / "fills.parquet", index=False)

    equity_df = report_gen.generate_equity_dataframe(result)
    equity_df.to_parquet(output_dir / "equity.parquet", index=False)

    # 输出摘要
    m = result.metrics
    print(f"回测完成。报告目录: {output_dir}")
    print(f"  交易日: {m.get('trading_days', len(result.daily_equity))}")
    print(f"  初始权益: {m.get('initial_equity')}")
    print(f"  最终权益: {m.get('final_equity')}")
    print(f"  总收益率: {m.get('total_return')}")
    print(f"  最大回撤: {m.get('max_drawdown')}")
    print(f"  交易次数: {m.get('total_trades')}")
    print(f"  订单数: {len(result.orders)}")
    print(f"  成交数: {len(result.fills)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ashare-quant",
        description=f"A 股双轨量化研究系统 v{__version__}",
    )
    sub = p.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init-config", help="查看或初始化配置")
    p_init.add_argument("--output", help="输出配置文件路径")
    p_init.set_defaults(func=cmd_init_config)

    p_fetch = sub.add_parser("fetch", help="抓取指定股票和日期范围")
    p_fetch.add_argument("--symbol", required=True)
    p_fetch.add_argument("--start", required=True, help="YYYY-MM-DD")
    p_fetch.add_argument("--end", required=True, help="YYYY-MM-DD")
    p_fetch.add_argument(
        "--source",
        default=None,
        choices=[SOURCE_AKSHARE, SOURCE_BAOSTOCK],
        help="指定数据源；省略时使用 YAML 中的 primary",
    )
    p_fetch.add_argument("--data-dir", default="data")
    p_fetch.add_argument("--config", help="配置文件路径")
    p_fetch.add_argument("--no-fallback", action="store_true", help="禁用主源失败后自动回退备用源")
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

    p_bt = sub.add_parser("backtest", help="运行回测（Phase 2）")
    p_bt.add_argument("--quotes", required=True, help="curated 日行情 Parquet 路径")
    p_bt.add_argument("--config", help="回测配置 YAML 路径")
    p_bt.add_argument("--strategy", default="scripted", help="策略类型 (scripted/noop)")
    p_bt.add_argument("--signals", help="信号 JSON 文件路径（scripted 策略必需）")
    p_bt.add_argument("--start", help="回测起始日 YYYY-MM-DD")
    p_bt.add_argument("--end", help="回测结束日 YYYY-MM-DD")
    p_bt.add_argument("--output", default="reports/phase-2", help="报告输出目录")
    p_bt.set_defaults(func=cmd_backtest)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
