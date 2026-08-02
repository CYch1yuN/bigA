"""Dashboard 启动入口。

启动校验：
- LAN 模式必须同时提供 cert 和 key，否则拒绝启动
- 仅监听 127.0.0.1 时允许开发模式 HTTP
- 生产环境禁止 debug 和自动 reload
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import uvicorn

from .config import ConfigError, load_config


def build_ssl_args(cert_file: Path | None, key_file: Path | None) -> dict | None:
    """构造 uvicorn ssl 参数；仅 LAN 模式需要。"""
    if cert_file is None and key_file is None:
        return None
    if cert_file is None or key_file is None:
        raise ConfigError("HTTPS 必须同时提供 cert 与 key")
    if not Path(cert_file).is_file() or not Path(key_file).is_file():
        raise ConfigError("证书或私钥文件不存在")
    return {"certfile": str(cert_file), "keyfile": str(key_file)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="大A量化研究控制台")
    parser.add_argument("--host", default=None, help="监听地址（默认取 ASHARE_DASHBOARD_HOST）")
    parser.add_argument("--port", type=int, default=None, help="监听端口（默认 8765）")
    parser.add_argument("--debug", action="store_true", help="开发模式（仅 127.0.0.1 允许，且必须关闭）")
    args = parser.parse_args(argv)

    try:
        cfg = load_config()
    except ConfigError as exc:
        print(f"[dashboard] 配置错误，拒绝启动: {exc}", file=sys.stderr)
        return 2

    if args.host:
        cfg = _replace(cfg, host=args.host)
    if args.port:
        cfg = _replace(cfg, port=args.port)

    debug = bool(os.environ.get("ASHARE_DASHBOARD_DEBUG")) or args.debug
    if debug and cfg.lan_mode:
        print("[dashboard] 生产环境禁止 debug 模式（LAN 监听）", file=sys.stderr)
        return 2

    if cfg.lan_mode:
        ssl = build_ssl_args(cfg.cert_file, cfg.key_file)
        if ssl is None:
            print(
                f"[dashboard] LAN 模式（host={cfg.host}）必须提供 ASHARE_DASHBOARD_CERT_FILE 与 "
                "ASHARE_DASHBOARD_KEY_FILE，否则拒绝启动",
                file=sys.stderr,
            )
            return 2
    else:
        ssl = None

    uvicorn.run(
        "ashare_dashboard.app.main:create_app",
        factory=True,
        host=cfg.host,
        port=cfg.port,
        ssl_certfile=ssl["certfile"] if ssl else None,
        ssl_keyfile=ssl["keyfile"] if ssl else None,
        reload=bool(debug) and not cfg.lan_mode,
        debug=debug,
        log_level="info",
    )
    return 0


def _replace(cfg, **kwargs):
    from dataclasses import replace

    return replace(cfg, **kwargs)


if __name__ == "__main__":
    sys.exit(main())
