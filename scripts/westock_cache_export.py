"""Import a normalized WorkBuddy Westock result into Dashboard cache.

This command does not call MCP.  It accepts a JSON file containing the tool's
successful ``data`` payload and writes a versioned cache envelope.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "dashboard" / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.westock_bridge import CAPABILITY_MAP, WestockCacheStore  # noqa: E402

MAX_INPUT_BYTES = 5 * 1024 * 1024  # 5 MiB


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导入 Westock 标准化缓存")
    parser.add_argument("--capability", required=True, choices=sorted(CAPABILITY_MAP))
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--scope", default="global")
    parser.add_argument("--as-of")
    parser.add_argument("--fetched-at")
    args = parser.parse_args(argv)
    try:
        size = args.input.stat().st_size
    except OSError as exc:
        print(f"[FAIL] 无法读取输入文件: {type(exc).__name__}", file=sys.stderr)
        return 2
    if size > MAX_INPUT_BYTES:
        print(
            f"[FAIL] 输入文件超过 {MAX_INPUT_BYTES // (1024 * 1024)} MiB 上限"
            f"（实际 {size} 字节），拒绝写入缓存",
            file=sys.stderr,
        )
        return 2
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        print(f"[FAIL] 无法读取输入 JSON: {type(exc).__name__}", file=sys.stderr)
        return 2
    if isinstance(payload, dict) and payload.get("ok") is False:
        print("[FAIL] Westock 响应 ok=false，拒绝写入缓存", file=sys.stderr)
        return 2
    data = payload.get("data") if isinstance(payload, dict) and "data" in payload else payload
    store = WestockCacheStore(ROOT / "state" / "dashboard" / "westock")
    try:
        store.write_export(
            args.capability,
            data,
            scope=args.scope,
            as_of=args.as_of,
            fetched_at=args.fetched_at,
        )
    except ValueError as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 2
    print(f"[OK] cached capability={args.capability} scope={args.scope}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
