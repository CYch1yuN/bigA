"""Phase F3 worker CLI（第二轮）：基于 jobs 的受控缓存导出。

本脚本只推进队列状态与写入缓存，绝不直接调用 MCP，也不宣称自动刷新。

子命令：
    list [--status ...] [--limit N] [--offset N]
    claim <request_id>
    export <request_id> --job <job_id> --input <受控导出元数据.json>
    complete-job <request_id> --job <job_id> --result ok|partial|failed [--export-info <摘要.json>] [--warning ...]
    finish <request_id>

受控导出元数据（export 输入）：
    {"schema_version": 2, "capability": "quote", "scope": "600519.SH",
     "ok": true, "fetched_at": "ISO", "as_of": "YYYY-MM-DD", "data": {}}

安全约束：
- capability/scope 必须与 job 完全一致（防 quote 响应被作为 profile 写入）
- ok=false / 非法 schema / 时间 / data 类型 → 拒绝
- 写入后重新 read 校验；计算缓存内容 SHA-256；返回 fetched_at/cache_status/data_as_of/content_hash
- 原子写：导出失败不破坏旧缓存
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard" / "backend"))

from app.westock_bridge import WestockCacheStore
from app.westock_refresh_service import (
    EXPORT_RESULTS,
    MAX_REQUEST_BYTES,
    RefreshError,
    build_refresh_store,
    worker_fingerprint,
)

MAX_EXPORT_BYTES = 5 * 1024 * 1024  # 导出输入 ≤5 MiB

VALID_STATUSES = ("pending", "processing", "completed", "partial", "failed",
                  "cancelled", "expired")


def _store() -> Any:
    return build_refresh_store(Path.cwd())


def cmd_list(args: argparse.Namespace) -> int:
    store = _store()
    # worker 内部读取：全部请求（不走 session 公共视图）
    all_items = store.list_internal(args.status)
    limit = args.limit or 50
    offset = args.offset or 0
    page = all_items[offset:offset + limit]
    for item in page:
        target = item["target"]
        desc = f"{target['kind']}/{item.get('request_id', '')[:8]}"
        if target["kind"] == "stock":
            desc += " " + ",".join(target.get("symbols") or [])
        elif target["kind"] == "screener":
            desc += " " + (target.get("cache_scope") or "")[:12]
        print(f"{item['request_id']}  {item['status']:10s}  {desc}  jobs={len(item['jobs'])}")
    print(f"共 {len(all_items)} 个请求（显示 {len(page)}）")
    return 0


def cmd_claim(args: argparse.Namespace) -> int:
    try:
        item = _store().claim(args.request_id, worker_fingerprint())
    except RefreshError as exc:
        print(f"[FAIL] {exc.message}", file=sys.stderr)
        return 1
    if item is None:
        print(f"[FAIL] 请求不存在: {args.request_id}", file=sys.stderr)
        return 1
    print(f"[OK] claimed {item['request_id']} status={item['status']} jobs={len(item['jobs'])}")
    for job in item["jobs"]:
        print(f"  job {job['job_id']}  {job['capability']:16s} scope={job['scope']} "
              f"status={job['status']}" + (" summary_only" if job.get("summary_only") else ""))
    print("请在 WorkBuddy 会话内调用 westock MCP 获取数据，构造受控导出元数据（见 --help），"
          "然后使用 export 写入缓存。")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    store = _store()
    try:
        size = args.input.stat().st_size
    except OSError:
        print("[FAIL] 无法读取输入文件", file=sys.stderr)
        return 1
    if size > MAX_EXPORT_BYTES:
        print(f"[FAIL] 输入文件超过 {MAX_EXPORT_BYTES // (1024 * 1024)} MiB 上限", file=sys.stderr)
        return 1
    try:
        export = json.loads(args.input.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        print("[FAIL] 无法读取输入 JSON", file=sys.stderr)
        return 1
    cache = WestockCacheStore(Path.cwd() / "state" / "dashboard" / "westock")
    try:
        info = store.export_job(args.request_id, args.job, export, cache)
    except RefreshError as exc:
        print(f"[FAIL] {exc.message}", file=sys.stderr)
        return 1
    if info is None:
        print(f"[FAIL] 请求不存在: {args.request_id}", file=sys.stderr)
        return 1
    # 输出摘要供 complete-job 使用（也可用 --input 复用）
    summary = {"job": args.job, **info}
    print(f"[OK] cached job={args.job} status={info['cache_status']} "
          f"fetched_at={info['fetched_at']}")
    print(f"[OK] data_as_of={info['data_as_of']} content_hash={info['content_hash'][:16]}…")
    summary_path = args.input.with_suffix(args.input.suffix + ".summary.json")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    print(f"[OK] 摘要已写入 {summary_path}")
    return 0


def cmd_complete_job(args: argparse.Namespace) -> int:
    store = _store()
    export_info = None
    if args.export_info:
        try:
            raw = json.loads(Path(args.export_info).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            print("[FAIL] 无法读取 export-info 摘要", file=sys.stderr)
            return 1
        # export_info 顶层键精确白名单（剥离 job 等附加键）
        export_info = {k: raw[k] for k in ("fetched_at", "cache_status",
                                           "data_as_of", "content_hash") if k in raw}
    try:
        item = store.complete_job(args.request_id, args.job, args.result,
                                  export_info=export_info, warning=args.warning,
                                  cache_store=WestockCacheStore(
                                      Path.cwd() / "state" / "dashboard" / "westock"))
    except RefreshError as exc:
        print(f"[FAIL] {exc.message}", file=sys.stderr)
        return 1
    if item is None:
        print(f"[FAIL] 请求不存在: {args.request_id}", file=sys.stderr)
        return 1
    print(f"[OK] completed job={args.job} result={args.result}")
    return 0


def cmd_finish(args: argparse.Namespace) -> int:
    try:
        item = _store().finish(args.request_id)
    except RefreshError as exc:
        print(f"[FAIL] {exc.message}", file=sys.stderr)
        return 1
    if item is None:
        print(f"[FAIL] 请求不存在: {args.request_id}", file=sys.stderr)
        return 1
    print(f"[OK] finished {item['request_id']} status={item['status']} "
          f"detail={item['status_detail']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wstock-refresh-request",
        description="Phase F3 刷新请求 worker CLI（不调用 MCP，不自动刷新）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="列出刷新请求")
    p_list.add_argument("--status", choices=VALID_STATUSES)
    p_list.add_argument("--limit", type=int, default=50)
    p_list.add_argument("--offset", type=int, default=0)

    p_claim = sub.add_parser("claim", help="认领请求（pending→processing，输出全部 jobs）")
    p_claim.add_argument("request_id")

    p_export = sub.add_parser("export", help="导出单个 job（受控导出元数据）")
    p_export.add_argument("request_id")
    p_export.add_argument("--job", required=True)
    p_export.add_argument("--input", required=True, type=Path)

    p_complete = sub.add_parser("complete-job", help="记录单个 job 完成（幂等）")
    p_complete.add_argument("request_id")
    p_complete.add_argument("--job", required=True)
    p_complete.add_argument("--result", required=True, choices=EXPORT_RESULTS)
    p_complete.add_argument("--export-info")
    p_complete.add_argument("--warning")

    p_finish = sub.add_parser("finish", help="聚合完成（completed/partial/failed）")
    p_finish.add_argument("request_id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = {
        "list": cmd_list,
        "claim": cmd_claim,
        "export": cmd_export,
        "complete-job": cmd_complete_job,
        "finish": cmd_finish,
    }.get(args.command)
    if handler is None:
        return 2
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
