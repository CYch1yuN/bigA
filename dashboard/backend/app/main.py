"""FastAPI 应用工厂与启动校验（UI-G2 可操作工作台）。

安全要点（继承 UI-G1 并升级为真实执行）：
- 除 health 与 login 外全部要求认证
- 所有写操作要求 CSRF（Double-Submit Cookie + 请求头）
- Host 校验只允许配置中的主机/IP
- 统一错误结构，不泄露堆栈/路径/密钥
- LAN 模式必须提供 cert+key，否则拒绝启动
- 仅 127.0.0.1 允许开发模式 HTTP
- 生产禁止 debug 与自动 reload
- 作业通过固定 argv 白名单调用真实 automation CLI（无 shell、无任意参数）
- 写入型作业串行执行；启动时清理遗留 queued/running 作业为 interrupted
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import (
    DashboardConfig,
    ConfigError,
    default_project_root,
    load_config,
)
from .data_service import build_dashboard_snapshot
from .errors import DashboardError, error_body
from .executors import SubprocessExecutor, validate_date_arg
from .jobs import (
    ALL_JOB_TYPES,
    JobManager,
    JobRecord,
    JobStore,
    calendar_provider_from_parquet,
)
from .security import ALLOWED_ACTIONS, FORBIDDEN_ACTIONS, SecurityManager
from .market_service import MarketService, build_market_service
from .screener_service import MAX_BODY_BYTES, ScreenerError, ScreenerService, build_screener_service
from .stocks_deep_service import _INTEL_CATEGORIES, StocksDeepService, build_stocks_deep_service
from .stocks_service import CuratedStocksService, build_stocks_service
from .westock_bridge import build_westock_bridge
from .westock_operations_service import build_operations_service, operations_envelope
from .westock_health_service import build_health_service
from .westock_refresh_service import (
    RefreshError,
    build_coverage_scanner,
    build_refresh_store,
)

# 会话 Cookie 名称
SESSION_COOKIE = "ashare_dash_session"
CSRF_COOKIE = "ashare_dash_csrf"


def _host_allowed(host_header: str, config: DashboardConfig) -> bool:
    """Host 校验：仅允许配置 host 或本机回环；忽略端口部分。"""
    hostname = host_header.split(":")[0].strip().lower()
    allowed = {config.host.strip().lower(), "127.0.0.1", "::1", "localhost"}
    if hostname in allowed:
        return True
    # 允许 IPv6 全写形式
    try:
        if ipaddress.ip_address(hostname) == ipaddress.ip_address(config.host):
            return True
    except ValueError:
        pass
    return False


async def _parse_json(request: Request, *, max_bytes: int | None = None) -> dict[str, Any]:
    """安全解析 JSON 请求体，失败返回空 dict；max_bytes 超限返回空 dict。"""
    try:
        if max_bytes is not None:
            raw = await request.body()
            if len(raw) > max_bytes:
                return {}
            body = json.loads(raw)
        else:
            body = await request.json()
        if isinstance(body, dict):
            return body
    except Exception:
        pass
    return {}


async def _require_session(request: Request, security: SecurityManager, *, csrf_required: bool):
    """认证依赖：无有效会话返回 401；写操作额外要求 CSRF。"""
    sid = request.cookies.get(SESSION_COOKIE)
    session = security.get_session(sid)
    if session is None:
        raise DashboardError("unauthorized", "未认证或会话已过期", status_code=401)
    if csrf_required:
        provided = request.headers.get("X-CSRF-Token")
        if not security.validate_csrf(session, provided):
            raise DashboardError("csrf_invalid", "CSRF 校验失败", status_code=403)
    return session


def create_app(
    config: DashboardConfig | None = None,
    *,
    enable_static: bool = True,
    executor: Any = None,
    job_manager: Any = None,
) -> FastAPI:
    """创建应用；config 为 None 时从环境变量加载（fail-closed）。

    ``executor`` / ``job_manager`` 可注入（测试用假实现避免真实 CLI；
    生产留空自动构造真实执行器与作业管理器）。
    """
    cfg = config if config is not None else load_config()
    security = SecurityManager(cfg)

    # 真实执行器 + 作业管理（可注入替换）
    root = Path(cfg.project_root) if cfg.project_root else default_project_root()
    if executor is None:
        executor = SubprocessExecutor(cfg, project_root=root)
    if job_manager is None:
        store = JobStore(root / "state" / "dashboard")
        # 交易日历：项目固定目录 data/metadata/trade_calendar.parquet；
        # 缺失时由 JobManager 回退工作日模型（不阻断补跑）。
        calendar_path = root / "data" / "metadata" / "trade_calendar.parquet"
        calendar_provider = (
            calendar_provider_from_parquet(calendar_path) if calendar_path.is_file() else None
        )
        job_manager = JobManager(
            executor,
            store,
            calendar_provider=calendar_provider,
        )

    app = FastAPI(title="大A量化研究控制台", version="0.2.0", docs_url=None, redoc_url=None)

    app.state.config = cfg
    app.state.security = security
    app.state.executor = executor
    app.state.job_manager = job_manager
    app.state.westock_bridge = build_westock_bridge(root)
    app.state.westock_refresh = build_refresh_store(root)
    app.state.westock_coverage = build_coverage_scanner(
        root, app.state.westock_bridge.cache)
    app.state.westock_ops = build_operations_service(
        root, app.state.westock_bridge.cache, app.state.westock_refresh)
    app.state.westock_health = build_health_service(app.state.westock_ops)
    app.state.stocks_service: CuratedStocksService = build_stocks_service(root)
    app.state.stocks_deep: StocksDeepService = build_stocks_deep_service(root)
    app.state.market: MarketService = build_market_service(root)
    app.state.screener: ScreenerService = build_screener_service(root)

    # 启动时把遗留 queued/running 作业标记为 interrupted
    interrupted = job_manager.cleanup_on_startup()
    if interrupted:
        print(f"[dashboard] 已将 {interrupted} 个遗留作业标记为 interrupted", file=__import__("sys").stderr)

    # ---------- CORS 默认关闭；不允许星号 ----------
    cors_origins = _load_cors_origins()
    if cors_origins:
        if "*" in cors_origins:
            raise ConfigError("ASHARE_DASHBOARD_CORS_ORIGINS 不允许包含 *")
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST"],
            allow_headers=["X-CSRF-Token", "Content-Type"],
        )

    # ---------- Host 校验中间件 ----------
    @app.middleware("http")
    async def _host_check(request: Request, call_next):
        host_header = request.headers.get("host", "")
        if not _host_allowed(host_header, cfg):
            return JSONResponse(error_body("bad_host", "主机不被允许"), status_code=403)
        return await call_next(request)

    # ---------- 统一错误结构 ----------
    @app.exception_handler(DashboardError)
    async def _handle_dashboard_error(request: Request, exc: DashboardError) -> JSONResponse:
        return JSONResponse(error_body(exc.code, exc.message), status_code=exc.status_code)

    @app.exception_handler(HTTPException)
    async def _handle_http_exception(request: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse(error_body("request_error", str(exc.detail)), status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        # 统一错误结构；生产环境不泄露堆栈细节
        return JSONResponse(error_body("internal_error", "服务器内部错误"), status_code=500)

    # ---------- 路由 ----------

    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "status": "ok",
            "service": "ashare-dashboard",
            "version": "0.1.0",
        }

    @app.post("/api/auth/login")
    async def login(request: Request) -> JSONResponse:
        ip = _client_ip(request)
        if security.is_locked(ip):
            return JSONResponse(error_body("login_locked", "登录失败次数过多，请稍后再试"), status_code=429)
        body = await _parse_json(request)
        username = body.get("username", "")
        password = body.get("password", "")
        if not isinstance(username, str) or not isinstance(password, str) or not username or not password:
            return JSONResponse(error_body("invalid_request", "请求格式错误"), status_code=400)
        if username != cfg.username or not security.verify_password(password):
            security.record_failure(ip)
            return JSONResponse(error_body("invalid_credentials", "用户名或密码错误"), status_code=401)
        security.clear_failures(ip)
        # 登录成功后轮换 Session 标识
        session = security.create_session(cfg.username)
        resp = JSONResponse({"ok": True, "username": cfg.username})
        secure = request.url.scheme == "https"
        _set_session_cookies(resp, session.sid, session.csrf_token, secure=secure)
        return resp

    @app.post("/api/auth/logout")
    async def logout(request: Request) -> JSONResponse:
        session = await _require_session(request, security, csrf_required=True)
        security.delete_session(session.sid)
        resp = JSONResponse({"ok": True})
        _clear_cookies(resp)
        return resp

    @app.get("/api/auth/session")
    async def get_session(request: Request) -> JSONResponse:
        session = await _require_session(request, security, csrf_required=False)
        return JSONResponse({
            "ok": True,
            "authenticated": True,
            "username": session.username,
            "expires_at": session.expires_at,
        })

    @app.post("/api/auth/change-password")
    async def change_password(request: Request) -> JSONResponse:
        session = await _require_session(request, security, csrf_required=True)
        body = await _parse_json(request)
        old_password = body.get("old_password", "")
        new_password = body.get("new_password", "")
        if not isinstance(old_password, str) or not isinstance(new_password, str):
            return JSONResponse(error_body("invalid_request", "请求格式错误"), status_code=400)
        try:
            security.change_password(old_password, new_password)
        except DashboardError as exc:
            return JSONResponse(error_body(exc.code, exc.message), status_code=exc.status_code)
        security.delete_session(session.sid)
        resp = JSONResponse({"ok": True, "message": "密码已修改，所有会话已失效"})
        _clear_cookies(resp)
        return resp

    @app.get("/api/safety")
    async def safety(request: Request) -> JSONResponse:
        await _require_session(request, security, csrf_required=False)
        return JSONResponse({
            "ok": True,
            "live_trading": False,
            "broker_connected": False,
            "allowed_actions": list(ALLOWED_ACTIONS),
            "forbidden_actions": list(FORBIDDEN_ACTIONS),
            "security_statement": "仅用于研究信号与模拟账户，不连接券商，不涉及真实资金",
        })

    @app.get("/api/dashboard/snapshot")
    async def dashboard_snapshot(request: Request) -> JSONResponse:
        """UI-G2 只读聚合数据；认证后可访问，不接受任意路径参数。"""
        await _require_session(request, security, csrf_required=False)
        root = Path(cfg.project_root) if cfg.project_root else default_project_root()
        return JSONResponse(build_dashboard_snapshot(root))

    @app.get("/api/connections/westock")
    async def westock_connection(request: Request) -> JSONResponse:
        """Return the sanitized Westock capability and cache status."""
        await _require_session(request, security, csrf_required=False)
        return JSONResponse(app.state.westock_bridge.connection_status())

    @app.get("/api/connections/westock/coverage")
    async def westock_coverage(request: Request) -> JSONResponse:
        """Coverage 索引：缓存目录扫描（read 校验）+ 逐股票本地历史。"""
        await _require_session(request, security, csrf_required=False)
        allowed = {k: v for k, v in request.query_params.items()}
        try:
            result = app.state.westock_coverage.scan(allowed)
        except RefreshError as exc:
            return JSONResponse(error_body(exc.code, exc.message),
                                status_code=exc.status_code)
        return JSONResponse(result)

    # ------------------------------------------------------------------ #
    # F5-A 运营只读端点（全部认证；GET 无 CSRF；严格白名单参数）
    # ------------------------------------------------------------------ #
    @app.get("/api/connections/westock/operations/summary")
    async def westock_ops_summary(request: Request) -> JSONResponse:
        """运营总览：缓存质量/有效覆盖率/请求聚合/失败分布/TTL/业务日期滞后。"""
        await _require_session(request, security, csrf_required=False)
        if request.query_params:
            return JSONResponse(error_body("invalid_request", "summary 不支持查询参数"),
                                status_code=400)
        data = app.state.westock_ops.summary()
        return JSONResponse(operations_envelope(data))

    @app.get("/api/connections/westock/operations/caches")
    async def westock_ops_caches(request: Request) -> JSONResponse:
        await _require_session(request, security, csrf_required=False)
        try:
            data = app.state.westock_ops.caches(dict(request.query_params))
        except RefreshError as exc:
            return JSONResponse(error_body(exc.code, exc.message),
                                status_code=exc.status_code)
        return JSONResponse(operations_envelope(data))

    @app.get("/api/connections/westock/operations/capabilities")
    async def westock_ops_capabilities(request: Request) -> JSONResponse:
        await _require_session(request, security, csrf_required=False)
        try:
            data = app.state.westock_ops.capabilities(dict(request.query_params))
        except RefreshError as exc:
            return JSONResponse(error_body(exc.code, exc.message),
                                status_code=exc.status_code)
        # data 已是平铺 {total, limit, offset, items}；不得再嵌套一层 items
        return JSONResponse(operations_envelope(data))

    @app.get("/api/connections/westock/operations/symbols")
    async def westock_ops_symbols(request: Request) -> JSONResponse:
        await _require_session(request, security, csrf_required=False)
        try:
            data = app.state.westock_ops.symbols(dict(request.query_params))
        except RefreshError as exc:
            return JSONResponse(error_body(exc.code, exc.message),
                                status_code=exc.status_code)
        # data 已是平铺 {total, limit, offset, items}；不得再嵌套一层 items
        return JSONResponse(operations_envelope(data))

    @app.get("/api/connections/westock/operations/requests")
    async def westock_ops_requests(request: Request) -> JSONResponse:
        await _require_session(request, security, csrf_required=False)
        try:
            data = app.state.westock_ops.requests(dict(request.query_params))
        except RefreshError as exc:
            return JSONResponse(error_body(exc.code, exc.message),
                                status_code=exc.status_code)
        return JSONResponse(operations_envelope(data))

    @app.get("/api/connections/westock/operations/failures")
    async def westock_ops_failures(request: Request) -> JSONResponse:
        await _require_session(request, security, csrf_required=False)
        if request.query_params:
            return JSONResponse(error_body("invalid_request", "failures 不支持查询参数"),
                                status_code=400)
        try:
            data = app.state.westock_ops.failures()
        except RefreshError as exc:
            return JSONResponse(error_body(exc.code, exc.message),
                                status_code=exc.status_code)
        return JSONResponse(operations_envelope(data))

    # ------------------------------------------------------------------ #
    # F5-B 健康评估/告警/建议/趋势（只读；GET 无 CSRF；严格白名单参数）
    # ------------------------------------------------------------------ #
    @app.get("/api/connections/westock/health")
    async def westock_health(request: Request) -> JSONResponse:
        """健康总览：五维状态 + 总体严重度（不接受任何查询参数）。"""
        await _require_session(request, security, csrf_required=False)
        if request.query_params:
            return JSONResponse(error_body("invalid_request", "health 不支持查询参数"),
                                status_code=400)
        data = app.state.westock_health.health()
        return JSONResponse(operations_envelope(data))

    @app.get("/api/connections/westock/alerts")
    async def westock_alerts(request: Request) -> JSONResponse:
        """活动告警：固定规则生成；支持 severity/category/capability/symbol 过滤与分页。"""
        await _require_session(request, security, csrf_required=False)
        try:
            data = app.state.westock_health.alerts_api(dict(request.query_params))
        except RefreshError as exc:
            return JSONResponse(error_body(exc.code, exc.message),
                                status_code=exc.status_code)
        return JSONResponse(operations_envelope(data))

    @app.get("/api/connections/westock/recommendations")
    async def westock_recommendations(request: Request) -> JSONResponse:
        """维护建议：仅由告警固定映射生成；支持 priority/code/target_kind 过滤与分页。"""
        await _require_session(request, security, csrf_required=False)
        try:
            data = app.state.westock_health.recommendations_api(dict(request.query_params))
        except RefreshError as exc:
            return JSONResponse(error_body(exc.code, exc.message),
                                status_code=exc.status_code)
        return JSONResponse(operations_envelope(data))

    @app.get("/api/connections/westock/trends")
    async def westock_trends(request: Request) -> JSONResponse:
        """刷新趋势：7/30 天按上海自然日聚合（只反映刷新工作流历史）。"""
        await _require_session(request, security, csrf_required=False)
        try:
            data = app.state.westock_health.trends_api(dict(request.query_params))
        except RefreshError as exc:
            return JSONResponse(error_body(exc.code, exc.message),
                                status_code=exc.status_code)
        return JSONResponse(operations_envelope(data))

    @app.get("/api/connections/westock/refresh-requests")
    async def westock_refresh_requests_list(request: Request) -> JSONResponse:
        """当前 session 的刷新请求队列（limit 1–50、offset≥0、total）。"""
        session = await _require_session(request, security, csrf_required=False)
        params = dict(request.query_params)
        unknown = set(params) - {"status", "limit", "offset"}
        if unknown:
            return JSONResponse(error_body("invalid_filter",
                                           f"未知查询参数: {sorted(unknown)[0]}"), status_code=400)
        status = params.get("status")
        if status and status not in ("pending", "processing", "completed",
                                     "partial", "failed", "cancelled", "expired"):
            return JSONResponse(error_body("invalid_filter", "status 筛选不合法"), status_code=400)
        try:
            limit = int(params.get("limit", "50"))
            offset = int(params.get("offset", "0"))
        except ValueError:
            return JSONResponse(error_body("invalid_filter", "limit/offset 必须是整数"), status_code=400)
        if not (1 <= limit <= 50) or offset < 0:
            return JSONResponse(error_body("invalid_filter", "limit 1–50、offset≥0"), status_code=400)
        result = app.state.westock_refresh.list_for_session(
            session.sid, status=status, limit=limit, offset=offset)
        return JSONResponse({"ok": True, "items": result["items"], "total": result["total"]})

    @app.get("/api/connections/westock/refresh-requests/{request_id}")
    async def westock_refresh_request_detail(request: Request, request_id: str) -> JSONResponse:
        """单个刷新请求详情（仅当前 session；非所有者 404）。"""
        session = await _require_session(request, security, csrf_required=False)
        from .westock_refresh_service import REQUEST_ID_RE
        if not REQUEST_ID_RE.fullmatch(request_id):
            return JSONResponse(error_body("invalid_request", "request_id 必须是 32 位小写 hex"),
                                status_code=400)
        item = app.state.westock_refresh.get_for_session(request_id, session.sid)
        if item is None:
            return JSONResponse(error_body("request_not_found", "刷新请求不存在"), status_code=404)
        return JSONResponse(item)

    @app.post("/api/connections/westock/refresh-requests")
    async def westock_refresh_requests_create(request: Request) -> JSONResponse:
        """创建刷新请求（Dashboard 不调用 MCP；等待 WorkBuddy 会话处理）。"""
        session = await _require_session(request, security, csrf_required=True)
        body = await _parse_json(request, max_bytes=64 * 1024)
        if not body:
            return JSONResponse(error_body("invalid_request", "请求体非法或超过 64 KiB"),
                                status_code=400)
        from .westock_refresh_service import _has_forbidden_key
        if _has_forbidden_key(body):
            return JSONResponse(error_body("invalid_request", "请求体包含禁止字段"),
                                status_code=400)
        try:
            item = app.state.westock_refresh.create_request(body=body, session_id=session.sid)
        except RefreshError as exc:
            return JSONResponse(error_body(exc.code, exc.message), status_code=exc.status_code)
        return JSONResponse({"ok": True, **item})

    @app.delete("/api/connections/westock/refresh-requests/{request_id}")
    async def westock_refresh_requests_cancel(request: Request, request_id: str) -> JSONResponse:
        """取消刷新请求（仅 pending；非所有者 404）。"""
        session = await _require_session(request, security, csrf_required=True)
        from .westock_refresh_service import REQUEST_ID_RE
        if not REQUEST_ID_RE.fullmatch(request_id):
            return JSONResponse(error_body("invalid_request", "request_id 必须是 32 位小写 hex"),
                                status_code=400)
        try:
            item = app.state.westock_refresh.cancel_for_session(request_id, session.sid)
        except RefreshError as exc:
            return JSONResponse(error_body(exc.code, exc.message), status_code=exc.status_code)
        if item is None:
            return JSONResponse(error_body("request_not_found", "刷新请求不存在"), status_code=404)
        return JSONResponse({"ok": True, **item})

    @app.post("/api/connections/westock/refresh")
    async def westock_refresh(request: Request) -> JSONResponse:
        """创建刷新请求（兼容入口）：与 /refresh-requests 同一严格模型。"""
        session = await _require_session(request, security, csrf_required=True)
        body = await _parse_json(request, max_bytes=64 * 1024)
        from .westock_refresh_service import _has_forbidden_key
        if not body or _has_forbidden_key(body):
            return JSONResponse(
                error_body("invalid_refresh_request", "无法解析刷新请求"),
                status_code=400,
            )
        try:
            item = app.state.westock_refresh.create_request(body=body, session_id=session.sid)
        except RefreshError as exc:
            return JSONResponse(error_body(exc.code, exc.message), status_code=exc.status_code)
        target = item["target"]
        return JSONResponse({
            "ok": True,
            "accepted": True,
            "transport": "cache_export",
            "is_realtime": False,
            "request_id": item["request_id"],
            "status": item["status"],
            "target": target,
            "jobs": item["jobs"],
            "message": "刷新请求已创建，等待 WorkBuddy 会话处理。",
        })

    # ---------- Phase B：个股行情与策略联动（只读） ----------

    @app.get("/api/stocks")
    async def stocks_list(request: Request) -> JSONResponse:
        await _require_session(request, security, csrf_required=False)
        try:
            query = request.query_params.get("query")
            limit_raw = request.query_params.get("limit", "50")
            offset_raw = request.query_params.get("offset", "0")
            limit = int(limit_raw)
            offset = int(offset_raw)
            if limit < 1 or limit > 100:
                raise ValueError("limit 必须在 1~100")
            if offset < 0:
                raise ValueError("offset 必须 >= 0")
        except ValueError:
            return JSONResponse(error_body("invalid_request", "参数不合法"), status_code=400)
        return JSONResponse(app.state.stocks_service.list_stocks(query, limit, offset))

    @app.get("/api/stocks/{symbol}/history")
    async def stocks_history(symbol: str, request: Request) -> JSONResponse:
        await _require_session(request, security, csrf_required=False)
        adjustment = request.query_params.get("adjustment", "qfq")
        range_key = request.query_params.get("range", "all")
        end = request.query_params.get("end")
        if adjustment not in ("raw", "qfq"):
            return JSONResponse(error_body("invalid_request", "adjustment 必须是 raw 或 qfq"), status_code=400)
        if range_key not in ("1m", "3m", "6m", "1y", "3y", "all"):
            return JSONResponse(error_body("invalid_request", "非法区间"), status_code=400)
        try:
            return JSONResponse(app.state.stocks_service.history(symbol, adjustment, range_key, end))
        except ValueError as exc:
            return JSONResponse(error_body("invalid_symbol", str(exc)), status_code=400)

    @app.get("/api/stocks/{symbol}/snapshot")
    async def stocks_snapshot(symbol: str, request: Request) -> JSONResponse:
        await _require_session(request, security, csrf_required=False)
        try:
            return JSONResponse(app.state.stocks_service.snapshot(symbol))
        except ValueError as exc:
            return JSONResponse(error_body("invalid_symbol", str(exc)), status_code=400)

    @app.get("/api/stocks/{symbol}/minute")
    async def stocks_minute(symbol: str, request: Request) -> JSONResponse:
        await _require_session(request, security, csrf_required=False)
        try:
            return JSONResponse(app.state.stocks_service.minute(symbol))
        except ValueError as exc:
            return JSONResponse(error_body("invalid_symbol", str(exc)), status_code=400)

    @app.get("/api/stocks/{symbol}/research")
    async def stocks_research(symbol: str, request: Request) -> JSONResponse:
        await _require_session(request, security, csrf_required=False)
        try:
            return JSONResponse(app.state.stocks_service.research(symbol))
        except ValueError as exc:
            return JSONResponse(error_body("invalid_symbol", str(exc)), status_code=400)

    # ---------- Phase C：个股深度数据聚合（只读 Westock 缓存） ----------

    @app.get("/api/stocks/{symbol}/fundamentals")
    async def stocks_fundamentals(symbol: str, request: Request) -> JSONResponse:
        await _require_session(request, security, csrf_required=False)
        try:
            return JSONResponse(app.state.stocks_deep.fundamentals(symbol))
        except ValueError as exc:
            return JSONResponse(error_body("invalid_symbol", str(exc)), status_code=400)

    @app.get("/api/stocks/{symbol}/ownership")
    async def stocks_ownership(symbol: str, request: Request) -> JSONResponse:
        await _require_session(request, security, csrf_required=False)
        try:
            return JSONResponse(app.state.stocks_deep.ownership(symbol))
        except ValueError as exc:
            return JSONResponse(error_body("invalid_symbol", str(exc)), status_code=400)

    @app.get("/api/stocks/{symbol}/funds")
    async def stocks_funds(symbol: str, request: Request) -> JSONResponse:
        await _require_session(request, security, csrf_required=False)
        try:
            return JSONResponse(app.state.stocks_deep.funds(symbol))
        except ValueError as exc:
            return JSONResponse(error_body("invalid_symbol", str(exc)), status_code=400)

    @app.get("/api/stocks/{symbol}/intel")
    async def stocks_intel(symbol: str, request: Request) -> JSONResponse:
        await _require_session(request, security, csrf_required=False)
        # 先独立校验 symbol（不得被标记为 invalid_category）
        if not re.fullmatch(r"^[0-9]{6}\.(SH|SZ|BJ)$", symbol):
            return JSONResponse(error_body("invalid_symbol", "非法 symbol"), status_code=400)
        category = request.query_params.get("category")
        if category is not None and category not in _INTEL_CATEGORIES:
            return JSONResponse(error_body("invalid_category", "非法 category"), status_code=400)
        try:
            limit_raw = request.query_params.get("limit", "20")
            offset_raw = request.query_params.get("offset", "0")
            limit = int(limit_raw)
            offset = int(offset_raw)
            if limit < 1 or limit > 50:
                raise ValueError("limit 必须在 1~50")
            if offset < 0:
                raise ValueError("offset 必须 >= 0")
        except ValueError:
            return JSONResponse(error_body("invalid_request", "参数不合法"), status_code=400)
        return JSONResponse(app.state.stocks_deep.intel(symbol, category, limit, offset))

    @app.get("/api/stocks/{symbol}/events")
    async def stocks_events(symbol: str, request: Request) -> JSONResponse:
        await _require_session(request, security, csrf_required=False)
        try:
            return JSONResponse(app.state.stocks_deep.events(symbol))
        except ValueError as exc:
            return JSONResponse(error_body("invalid_symbol", str(exc)), status_code=400)

    @app.get("/api/stocks/{symbol}/technical")
    async def stocks_technical(symbol: str, request: Request) -> JSONResponse:
        await _require_session(request, security, csrf_required=False)
        try:
            return JSONResponse(app.state.stocks_deep.technical(symbol))
        except ValueError as exc:
            return JSONResponse(error_body("invalid_symbol", str(exc)), status_code=400)

    # ---------- Phase D：市场研究中心（只读 Westock 缓存） ----------

    @app.get("/api/market/overview")
    async def market_overview(request: Request) -> JSONResponse:
        await _require_session(request, security, csrf_required=False)
        return JSONResponse(app.state.market.overview())

    @app.get("/api/market/distribution")
    async def market_distribution(request: Request) -> JSONResponse:
        await _require_session(request, security, csrf_required=False)
        return JSONResponse(app.state.market.distribution())

    @app.get("/api/market/hot")
    async def market_hot(request: Request) -> JSONResponse:
        await _require_session(request, security, csrf_required=False)
        return JSONResponse(app.state.market.hot())

    @app.get("/api/market/sectors")
    async def market_sectors(request: Request) -> JSONResponse:
        await _require_session(request, security, csrf_required=False)
        return JSONResponse(app.state.market.sectors())

    @app.get("/api/market/indexes")
    async def market_indexes(request: Request) -> JSONResponse:
        await _require_session(request, security, csrf_required=False)
        return JSONResponse(app.state.market.indexes())

    @app.get("/api/market/indexes/{index_code}/constituents")
    async def market_constituents(index_code: str, request: Request) -> JSONResponse:
        await _require_session(request, security, csrf_required=False)
        try:
            return JSONResponse(app.state.market.constituents(index_code))
        except ValueError as exc:
            return JSONResponse(error_body("invalid_index_code", str(exc)), status_code=400)

    @app.get("/api/market/industry-chain")
    async def market_industry_chain(request: Request) -> JSONResponse:
        await _require_session(request, security, csrf_required=False)
        return JSONResponse(app.state.market.industry_chain())

    @app.get("/api/market/macro")
    async def market_macro(request: Request) -> JSONResponse:
        await _require_session(request, security, csrf_required=False)
        return JSONResponse(app.state.market.macro())

    @app.get("/api/market/calendar")
    async def market_calendar(request: Request) -> JSONResponse:
        await _require_session(request, security, csrf_required=False)
        try:
            start_date = request.query_params.get("start_date")
            end_date = request.query_params.get("end_date")
            category = request.query_params.get("category")
            importance = request.query_params.get("importance")
            limit_raw = request.query_params.get("limit", "50")
            offset_raw = request.query_params.get("offset", "0")
            limit = int(limit_raw)
            offset = int(offset_raw)
            if limit < 1 or limit > 500:
                raise ValueError("limit 必须在 1~500")
            if offset < 0:
                raise ValueError("offset 必须 >= 0")
        except ValueError:
            return JSONResponse(error_body("invalid_request", "参数不合法"), status_code=400)
        try:
            return JSONResponse(app.state.market.calendar(
                start_date, end_date, category, importance, limit, offset))
        except ValueError as exc:
            return JSONResponse(error_body("invalid_calendar_params", str(exc)), status_code=400)

    @app.get("/api/market/funds")
    async def market_funds(request: Request) -> JSONResponse:
        await _require_session(request, security, csrf_required=False)
        return JSONResponse(app.state.market.funds())

    @app.get("/api/market/events")
    async def market_events(request: Request) -> JSONResponse:
        await _require_session(request, security, csrf_required=False)
        return JSONResponse(app.state.market.events())

    # ---------- Phase E：选股中心（只读研究工作台） ----------

    def _screener_error(exc: ScreenerError) -> JSONResponse:
        return JSONResponse(error_body(exc.code, exc.message), status_code=exc.status_code)

    @app.post("/api/screener/run")
    async def screener_run(request: Request) -> JSONResponse:
        session = await _require_session(request, security, csrf_required=True)
        body = await _parse_json(request, max_bytes=MAX_BODY_BYTES)
        try:
            return JSONResponse(app.state.screener.run(session.sid, body))
        except ScreenerError as exc:
            return _screener_error(exc)

    @app.get("/api/screener/results/{result_id}")
    async def screener_result(result_id: str, request: Request) -> JSONResponse:
        await _require_session(request, security, csrf_required=False)
        try:
            return JSONResponse(app.state.screener.get_result(result_id))
        except ScreenerError as exc:
            return _screener_error(exc)

    @app.get("/api/screener/saved")
    async def screener_saved_list(request: Request) -> JSONResponse:
        await _require_session(request, security, csrf_required=False)
        return JSONResponse(app.state.screener.list_saved())

    @app.post("/api/screener/saved")
    async def screener_saved_create(request: Request) -> JSONResponse:
        session = await _require_session(request, security, csrf_required=True)
        body = await _parse_json(request, max_bytes=MAX_BODY_BYTES)
        try:
            return JSONResponse(app.state.screener.save_filter(body))
        except ScreenerError as exc:
            return _screener_error(exc)

    @app.delete("/api/screener/saved/{saved_id}")
    async def screener_saved_delete(saved_id: str, request: Request) -> JSONResponse:
        session = await _require_session(request, security, csrf_required=True)
        try:
            return JSONResponse(app.state.screener.delete_saved(saved_id))
        except ScreenerError as exc:
            return _screener_error(exc)

    @app.get("/api/screener/candidates")
    async def screener_candidates_list(request: Request) -> JSONResponse:
        await _require_session(request, security, csrf_required=False)
        return JSONResponse(app.state.screener.list_candidates())

    @app.post("/api/screener/candidates")
    async def screener_candidates_add(request: Request) -> JSONResponse:
        session = await _require_session(request, security, csrf_required=True)
        body = await _parse_json(request, max_bytes=MAX_BODY_BYTES)
        try:
            return JSONResponse(app.state.screener.add_candidate(body))
        except ScreenerError as exc:
            return _screener_error(exc)

    @app.delete("/api/screener/candidates/{symbol}")
    async def screener_candidates_delete(symbol: str, request: Request) -> JSONResponse:
        session = await _require_session(request, security, csrf_required=True)
        try:
            return JSONResponse(app.state.screener.delete_candidate(symbol))
        except ScreenerError as exc:
            return _screener_error(exc)

    @app.post("/api/actions/prepare")
    async def actions_prepare(request: Request) -> JSONResponse:
        """旧接口收敛：仅允许 verify（只读、无参数）。其余动作走 /api/jobs。"""
        session = await _require_session(request, security, csrf_required=True)
        body = await _parse_json(request)
        action = body.get("action", "")
        if not isinstance(action, str) or not action:
            return JSONResponse(error_body("invalid_request", "请求格式错误"), status_code=400)
        if action != "verify":
            return JSONResponse(
                error_body("action_requires_jobs", "该动作需要参数，请使用 /api/jobs 异步作业接口"),
                status_code=400,
            )
        try:
            executor.validate_action(action)
        except DashboardError as exc:
            return JSONResponse(error_body(exc.code, exc.message), status_code=exc.status_code)
        token = security.issue_confirm_token(session.username, action, session.sid)
        return JSONResponse({
            "ok": True,
            "action": action,
            "confirm_token": token,
            "expires_in": 60,
        })

    @app.post("/api/actions/execute")
    async def actions_execute(request: Request) -> JSONResponse:
        """旧接口收敛：仅允许 verify（无需参数、只读）。

        daily / weekly / rerun / backfill 需要日期等参数，必须走
        /api/jobs（异步作业模型 + 参数绑定的确认令牌），本接口一律拒绝，
        避免绕过参数校验的同步执行路径。
        """
        session = await _require_session(request, security, csrf_required=True)
        body = await _parse_json(request)
        action = body.get("action", "")
        token = body.get("confirm_token", "")
        if not isinstance(action, str) or not isinstance(token, str):
            return JSONResponse(error_body("invalid_request", "请求格式错误"), status_code=400)
        if action != "verify":
            return JSONResponse(
                error_body("action_requires_jobs", "该动作需要参数，请使用 /api/jobs 异步作业接口"),
                status_code=400,
            )
        try:
            executor.validate_action(action)
        except DashboardError as exc:
            return JSONResponse(error_body(exc.code, exc.message), status_code=exc.status_code)
        if not security.consume_confirm_token(token, session.username, action, session.sid):
            return JSONResponse(error_body("invalid_confirm_token", "确认令牌无效、过期或已使用"), status_code=403)
        try:
            result = await executor.execute(action)
        except DashboardError as exc:
            return JSONResponse(error_body(exc.code, exc.message), status_code=exc.status_code)
        return JSONResponse({
            "ok": True,
            "result": result.to_public_dict(),
        })

    # ---------- 作业（Job）路由：异步操作中心 ----------

    @app.get("/api/jobs")
    async def jobs_list(request: Request) -> JSONResponse:
        """近期作业列表（最新在前）。"""
        await _require_session(request, security, csrf_required=False)
        jobs = job_manager.list_jobs()
        return JSONResponse({
            "ok": True,
            "jobs": [j.to_dict() for j in jobs],
        })

    @app.post("/api/jobs/prepare")
    async def jobs_prepare(request: Request) -> JSONResponse:
        """为作业类型签发一次性确认令牌（绑定用户+动作+会话+**具体参数**）。"""
        session = await _require_session(request, security, csrf_required=True)
        body = await _parse_json(request)
        job_type = body.get("job_type", "")
        if not isinstance(job_type, str) or job_type not in ALL_JOB_TYPES:
            return JSONResponse(
                error_body("invalid_job_type", f"作业类型必须为 {list(ALL_JOB_TYPES)}"),
                status_code=400,
            )
        params = _job_params_from_body(body)
        token = security.issue_confirm_token(
            session.username, job_type, session.sid, params=params
        )
        return JSONResponse({
            "ok": True,
            "job_type": job_type,
            "confirm_token": token,
            "expires_in": 60,
        })

    @app.get("/api/jobs/{job_id}")
    async def jobs_get(request: Request, job_id: str) -> JSONResponse:
        """作业详情：状态、进度、逐日结果、安全处理后的日志。"""
        await _require_session(request, security, csrf_required=False)
        try:
            rec = job_manager.get_job(job_id)
        except DashboardError as exc:
            return JSONResponse(error_body(exc.code, exc.message), status_code=exc.status_code)
        return JSONResponse({"ok": True, "job": rec.to_dict()})

    @app.post("/api/jobs")
    async def jobs_create(request: Request) -> JSONResponse:
        """创建作业：verify / daily / weekly / rerun / backfill。

        写入型作业串行执行；返回 queued 记录，前端轮询 /api/jobs/{id}。
        确认令牌必须与创建参数一致（防替换攻击）。
        """
        session = await _require_session(request, security, csrf_required=True)
        body = await _parse_json(request)
        job_type = body.get("job_type", "")
        if not isinstance(job_type, str) or job_type not in ALL_JOB_TYPES:
            return JSONResponse(
                error_body("invalid_job_type", f"作业类型必须为 {list(ALL_JOB_TYPES)}"),
                status_code=400,
            )
        # 消费一次性确认令牌（绑定动作=job_type 且参数必须一致）
        token = body.get("confirm_token", "")
        params = _job_params_from_body(body)
        if not isinstance(token, str) or not security.consume_confirm_token(
            token, session.username, job_type, session.sid, params=params
        ):
            return JSONResponse(error_body("invalid_confirm_token", "确认令牌无效、过期、已使用或参数不匹配"), status_code=403)
        try:
            rec = job_manager.create_job(
                job_type,
                date=body.get("date"),
                task=body.get("task"),
                start_date=body.get("start_date"),
                end_date=body.get("end_date"),
            )
        except DashboardError as exc:
            return JSONResponse(error_body(exc.code, exc.message), status_code=exc.status_code)
        job_manager.enqueue_and_run(rec)
        return JSONResponse({"ok": True, "job": rec.to_dict()})

    # ---------- 静态托管（生产构建后） ----------
    if enable_static:
        _mount_static(app)

    return app


def _dist_dir() -> Path:
    """前端构建产物目录（生产构建后由 FastAPI 托管）。"""
    return Path(__file__).resolve().parents[2] / "frontend" / "dist"


def _mount_static(app: FastAPI) -> None:
    dist_dir = _dist_dir()
    if not dist_dir.is_dir():
        @app.get("/", include_in_schema=False)
        async def _root() -> JSONResponse:
            return JSONResponse(
                {"ok": True, "message": "Dashboard 前端未构建（运行 npm run build 后访问）"}
            )
        return

    assets_dir = dist_dir / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False, response_model=None)
    async def _spa(full_path: str) -> FileResponse | JSONResponse:
        # 路径穿越防护：规范化后必须仍在 dist 内
        candidate = (dist_dir / full_path).resolve()
        if candidate.is_file() and dist_dir in candidate.parents:
            return FileResponse(candidate)
        index = dist_dir / "index.html"
        if index.is_file():
            return FileResponse(index)
        return JSONResponse(error_body("not_found", "页面不存在"), status_code=404)


# ---------- 辅助 ----------

def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _set_session_cookies(resp: JSONResponse, sid: str, csrf: str, secure: bool = True) -> None:
    """会话 Cookie：HttpOnly、SameSite=Strict；HTTPS 下附加 Secure。"""
    resp.set_cookie(
        SESSION_COOKIE, sid,
        httponly=True, secure=secure, samesite="strict", path="/", max_age=8 * 3600,
    )
    resp.set_cookie(
        CSRF_COOKIE, csrf,
        httponly=False, secure=secure, samesite="strict", path="/", max_age=8 * 3600,
    )


def _clear_cookies(resp: JSONResponse) -> None:
    resp.delete_cookie(SESSION_COOKIE, path="/")
    resp.delete_cookie(CSRF_COOKIE, path="/")


def _load_cors_origins() -> list[str]:
    import os

    raw = os.environ.get("ASHARE_DASHBOARD_CORS_ORIGINS", "")
    if not raw.strip():
        return []
    return [o.strip() for o in raw.split(",") if o.strip()]


def _job_params_from_body(body: dict[str, Any]) -> dict[str, str]:
    """从请求体提取作业参数（规范化：去 None/空串，仅保留白名单键）。

    确认令牌绑定这些参数；创建作业时参数不一致则拒绝（防替换攻击）。
    """
    keys = ("date", "task", "start_date", "end_date")
    params: dict[str, str] = {}
    for key in keys:
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            params[key] = value.strip()
    return params
