import asyncio
import logging
import logging.handlers
import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
import sys
import time
import traceback

from .logging_context import (
    ContextFilter,
    bind_project,
    bind_section,
    bind_task,
    request_id_var,
    project_id_var,
    section_id_var,
    task_id_var,
)

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.cors import CORSMiddleware


# 加载 .env 文件（支持 backend/.env 和项目根目录 .env），必须在 config 导入前执行
env_path = Path(__file__).resolve().parent.parent / ".env"
if env_path.exists():
    load_dotenv(dotenv_path=env_path, override=True)
else:
    load_dotenv()

from .config import AUTH_ENABLED, HOST, PORT
from .db import init_db
from .middleware.latency_monitor import LatencyBudgetMiddleware
from .middleware.spa_fallback import SPAIndexFallbackMiddleware

# API Key 认证中间件（可选，注释掉以禁用）
# from .middleware.api_key_auth import ApiKeyAuthMiddleware
from .routers import (
    ai,
    ai_config,
    api_docs,
    api_keys,
    auth,
    batch,
    cache,
    charts,
    consistency,
    content_version,
    duplicate,
    export,
    facts,
    feasibility,  # ENHANCE 2026-09-15：可行性研究报告模块
    files,
    i18n,
    images,  # 新增：图像生成路由
    industry_tree,
    knowledge,
    monitor,
    projects,
    prompts,
    qa,  # 新增：智能问答路由
    rejection,
    sections,
    tasks,  # 新增：统一任务恢复路由
    templates,
    usage,
    websocket,
    word_count,  # 新增：字数统计路由
)


logger = logging.getLogger(__name__)

# 配置日志：同时输出到控制台和文件
LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "backend.log"

# 根日志配置：控制台 + 文件（含崩溃追踪）
_root_logger = logging.getLogger()
_root_logger.setLevel(logging.DEBUG)

# 控制台处理器
_console_handler = logging.StreamHandler(sys.stdout)
_console_handler.setLevel(logging.INFO)
_console_formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s [rid=%(request_id)s pid=%(project_id)s sid=%(section_id)s tid=%(task_id)s]: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_console_handler.setFormatter(_console_formatter)
_console_handler.addFilter(ContextFilter())

# 文件处理器（滚动日志，最多10MB x 5个文件）
_file_handler = logging.handlers.RotatingFileHandler(
    LOG_FILE,
    maxBytes=10 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
)
_file_handler.setLevel(logging.DEBUG)
_file_formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s (%(filename)s:%(lineno)d) [rid=%(request_id)s pid=%(project_id)s sid=%(section_id)s tid=%(task_id)s]: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
_file_handler.setFormatter(_file_formatter)
_file_handler.addFilter(ContextFilter())

# 移除已有处理器避免重复
_root_logger.handlers.clear()
_root_logger.addHandler(_console_handler)
_root_logger.addHandler(_file_handler)


# 全局未捕获异常钩子：记录到文件
def _global_exception_handler(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        return
    logger.critical("未捕获的致命异常", exc_info=(exc_type, exc_value, exc_traceback))
    try:
        with open(LOG_DIR / "fatal_crash.log", "a", encoding="utf-8") as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}]\n")
            f.write(f"类型: {exc_type.__name__}\n")
            f.write(f"消息: {exc_value}\n")
            f.write(f"堆栈:\n{''.join(traceback.format_tb(exc_traceback))}\n")
    except Exception as _e:
        logger.debug("[silent-except] main.py: line 121 - %s", _e)


sys.excepthook = _global_exception_handler

# 创建主应用
app = FastAPI(
    title="招投标方案编写平台", version="3.0.0", docs_url="/api/docs", redoc_url="/api/redoc"
)

# 追踪后台任务，防止 GC 导致未 await 的协程警告
_background_tasks: set = set()

# 创建 v1 API 子应用
from fastapi import APIRouter


v1_router = APIRouter(prefix="/api/v1")

app.add_middleware(
    LatencyBudgetMiddleware,
)

# JWT 认证中间件（仅在 AUTH_ENABLED=True 时挂载）
if AUTH_ENABLED:
    from starlette.middleware.base import BaseHTTPMiddleware

    class JwtAuthMiddleware(BaseHTTPMiddleware):
        """对 /api/v1/ 路径做 Bearer Token 校验的轻量中间件。

        白名单路径放行；其余路径若无有效 Authorization: Bearer <token> 则返回 401。
        不对 /api/docs /api/redoc /api/openapi.json 做限制，保留 OpenAPI 可用性。
        不做流式响应吞并：仅在缺少/无效 token 时提前拦截，否则直接 call_next。
        """

        # 认证白名单按“精确路径 + 少量静态资源前缀”匹配，避免
        # /auth/login-evil、/health-secret 等相似路径意外绕过认证。
        PUBLIC_EXACT_PATHS = frozenset(
            {
                "/api/v1/auth/login",
                "/api/v1/health",
                "/api/v1/health/enhanced",
                "/api/docs",
                "/api/redoc",
                "/api/openapi.json",
            }
        )
        PUBLIC_PATHS_PREFIXES = ("/favicon", "/assets", "/static")

        async def dispatch(self, request, call_next):
            path = request.url.path
            # BUG-FIX（P1）：中间件挂在应用级，原实现对 `/`、`/index.html`、
            # `/assets/*` 一律要求 Bearer Token。浏览器导航无法携带
            # Authorization 头 → AUTH_ENABLED=true 时前端直接打不开，
            # 迫使运维长期保持认证关闭。所有业务路由都挂在 /api/v1 下，
            # 因此只对 /api/v1/ 做强制校验，其余（SPA / 静态资源 / 文档）放行。
            if not path.startswith("/api/v1/"):
                return await call_next(request)
            if path in self.PUBLIC_EXACT_PATHS or any(
                path.startswith(p) for p in self.PUBLIC_PATHS_PREFIXES
            ):
                return await call_next(request)

            # BUG-FIX-24 修复：SSE 的 EventSource 不支持自定义头，
            # 允许通过 query 参数 ?token=xxx 作为后备 token 来源。
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                token = auth_header[7:].strip()
            else:
                token = request.query_params.get("token", "")
            if not token:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "缺少授权令牌"},
                )
            try:
                from .services.auth_service import get_current_user_dependency

                # BUG-FIX 2026-09-16（高 事件循环阻塞）：中间件跑在事件循环线程上，
                # 而 get_current_user_dependency 是一次同步文件读（JWT secret）
                # + 一次 sqlite 查询 + 抢全局写锁。AUTH_ENABLED=true 时**每个**请求
                # 都要付这笔开销，高峰期整站 rt 抬升。移线程池执行。
                request.state.current_user = await asyncio.to_thread(
                    get_current_user_dependency, request
                )
            except HTTPException as exc:
                return JSONResponse(
                    status_code=exc.status_code,
                    content={"detail": exc.detail},
                )
            return await call_next(request)

    app.add_middleware(JwtAuthMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        f"http://{HOST}:{PORT}",
        f"http://127.0.0.1:{PORT}",
        f"http://localhost:{PORT}",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:5175",
        "http://localhost:5175",
        *(os.getenv("CORS_ORIGINS", "").split(",") if os.getenv("CORS_ORIGINS") else []),
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 请求级追踪标识中间件（F-P0-1，2026-10-07）：为每个请求生成/透传 request_id，
# 使全链路日志可按 ID 串联。优先使用上游传入的 X-Request-ID（网关/前端可透传）。
from starlette.middleware.base import BaseHTTPMiddleware


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        request_id_var.set(rid)
        request.state.request_id = rid
        # F-P0-1：在中间件层按 URL 绑定 project/section/task ID 到日志上下文。
        # 放在 call_next 之前，确保本次请求全程日志（含路由处理）都带 ID；
        # 用 try 包住，避免绑定异常影响主流程。
        try:
            _bind_context_ids(request)
        except Exception:
            pass
        try:
            response = await call_next(request)
        finally:
            # contextvar 随协程任务结束自然失效，无需手动 reset（避免线程复用串号风险）
            pass
        # 回显 request_id 到响应头，便于网关/前端/客户端按 ID 串联日志与链路
        response.headers["X-Request-ID"] = rid
        return response


app.add_middleware(RequestIDMiddleware)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """全局异常处理器：捕获所有未处理的异常

    - SSE 请求：返回 SSE 格式的错误事件，避免 EventSource 触发 ERR_ABORTED
    - HTTPException: 直接返回原始状态码和详情
    - 其他异常: 记录完整堆栈，返回500错误（不含敏感信息）
    """
    import json
    import traceback as _tb

    from fastapi.responses import StreamingResponse

    logger = logging.getLogger(__name__)
    request_method = request.method
    request_path = request.url.path
    accept_header = request.headers.get("accept", "")

    is_sse_request = "text/event-stream" in accept_header

    # HTTPException：已经是预期的错误
    from fastapi import HTTPException

    if isinstance(exc, HTTPException):
        # 检测 HTTPException 中包装的数据库错误（如端点捕获数据库异常后 raise HTTPException(500, detail="...数据库...")
        # 将此类错误从 500 降级为 503，便于前端识别并友好提示
        exc_detail_lower = str(exc.detail).lower() if exc.detail else ""
        if exc.status_code >= 500 and any(
            kw in exc_detail_lower
            for kw in (
                "database",
                "sqlite",
                "数据库",
                "file is not a database",
                "pool exhausted",
                "connection pool",
                "locked",
                "not a database",
            )
        ):
            logger.warning(
                "HTTP异常(数据库降级): 503 - %s | %s %s", exc.detail, request_method, request_path
            )
            if is_sse_request:
                return StreamingResponse(
                    iter(
                        [
                            f"data: {json.dumps({'status': 'error', 'message': str(exc.detail)}, ensure_ascii=False)}\n\n"
                        ]
                    ),
                    media_type="text/event-stream",
                    status_code=503,
                    headers={
                        "Cache-Control": "no-cache, no-transform",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no",
                    },
                )
            return JSONResponse(
                status_code=503,
                content={"detail": str(exc.detail)},
            )
        logger.warning(
            "HTTP异常: %d - %s | %s %s", exc.status_code, exc.detail, request_method, request_path
        )
        if is_sse_request:
            return StreamingResponse(
                iter(
                    [
                        f"data: {json.dumps({'status': 'error', 'message': str(exc.detail)}, ensure_ascii=False)}\n\n"
                    ]
                ),
                media_type="text/event-stream",
                status_code=exc.status_code,
                headers={
                    "Cache-Control": "no-cache, no-transform",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
        )

    # 区分异常类型，提供更友好的错误信息
    error_type = type(exc).__name__
    error_detail = str(exc)

    # 记录完整堆栈到日志
    tb_str = _tb.format_exc()

    # SSE 请求统一返回 SSE 错误事件
    if is_sse_request:
        logger.error(
            "SSE请求异常 [%s]: %s | %s %s\n%s",
            error_type,
            error_detail,
            request_method,
            request_path,
            tb_str,
        )
        return StreamingResponse(
            iter(
                [
                    f"data: {json.dumps({'status': 'error', 'message': f'服务器内部错误: {error_detail}'}, ensure_ascii=False)}\n\n"
                ]
            ),
            media_type="text/event-stream",
            status_code=500,
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    # 根据异常类型分类处理
    if "authentication" in error_type.lower() or "permission" in error_type.lower():
        logger.error(
            "认证/权限错误: %s | %s %s\n%s", error_detail, request_method, request_path, tb_str
        )
        return JSONResponse(
            status_code=403,
            content={"detail": "权限不足，请重新登录"},
        )

    if "timeout" in error_type.lower() or "Timeout" in error_type:
        logger.error("超时错误: %s | %s %s\n%s", error_detail, request_method, request_path, tb_str)
        return JSONResponse(
            status_code=504,
            content={"detail": "请求超时，请稍后重试"},
        )

    if any(kw in error_type.lower() for kw in ("database", "sqlite")) or any(
        kw in error_detail.lower()
        for kw in (
            "database",
            "pool exhausted",
            "not a database",
            "database is locked",
            "locked",
            "wal",
            "journal",
            "disk i/o",
        )
    ):
        logger.error(
            "数据库错误: %s | %s %s\n%s", error_detail, request_method, request_path, tb_str
        )
        return JSONResponse(
            status_code=503,
            content={"detail": "数据库服务暂时不可用，请稍后重试"},
        )

    # AI相关错误（包括网络连接错误、HTTP 错误、AI 超时/重试耗尽等 AI 调用中常见的异常）
    ai_keywords = (
        "ai",
        "openai",
        "deepseek",
        "api_key",
        "connect",
        "connection",
        "http",
        "request",
    )
    ai_error_signals = (
        "超过最大重试次数",
        "请求超时",
        "api 调用失败",
        "api 调用最终失败",
        "可重试错误",
        "api 配额已耗尽",
        "api key 无效",
        "未授权",
        "ssl",
    )
    is_ai_runtime = isinstance(exc, RuntimeError) and any(
        sig in error_detail for sig in ai_error_signals
    )
    if (
        is_ai_runtime
        or any(kw in error_type.lower() for kw in ai_keywords)
        or any(kw in error_detail.lower() for kw in ai_keywords)
    ):
        logger.error(
            "AI服务错误 [%s]: %s | %s %s\n%s",
            error_type,
            error_detail,
            request_method,
            request_path,
            tb_str,
        )
        return JSONResponse(
            status_code=502,
            content={"detail": f"AI服务调用失败: {error_detail[:200]} | 请检查AI配置或稍后重试"},
        )

    # 默认：未知错误，返回通用错误信息（不暴露详细错误）
    logger.error(
        "未捕获的异常 [%s]: %s\n%s\n请求路径: %s %s",
        error_type,
        error_detail,
        tb_str,
        request_method,
        request_path,
    )

    # 记录到最近错误列表，便于前端调试接口查询
    try:
        from .debug_errors import record_error

        record_error(
            method=request_method,
            path=request_path,
            error_type=error_type,
            error_detail=error_detail,
            traceback=tb_str,
        )
    except Exception as _e:
        logger.debug("[silent-except] main.py: line 440 - %s", _e)

    return JSONResponse(
        status_code=500,
        content={"detail": "服务器内部错误，请稍后重试"},
    )


# 将所有路由挂载到 /api/v1 前缀下
v1_router.include_router(projects.router, prefix="", tags=["projects"])
v1_router.include_router(files.router, prefix="", tags=["files"])
v1_router.include_router(ai.router, prefix="", tags=["ai"])
v1_router.include_router(ai_config.router, prefix="", tags=["ai_config"])
v1_router.include_router(prompts.router, prefix="", tags=["prompts"])
v1_router.include_router(sections.router, prefix="", tags=["sections"])
v1_router.include_router(templates.router, prefix="", tags=["templates"])
v1_router.include_router(export.router, prefix="", tags=["export"])
v1_router.include_router(facts.router, prefix="", tags=["facts"])
v1_router.include_router(rejection.router, prefix="", tags=["rejection"])
v1_router.include_router(duplicate.router, prefix="", tags=["duplicate"])
# ENHANCE 2026-09-15：可行性研究报告（全局单工作区，无 project 前缀）
v1_router.include_router(feasibility.router, prefix="", tags=["feasibility"])
v1_router.include_router(industry_tree.router, prefix="", tags=["industry_tree"])
v1_router.include_router(knowledge.router, prefix="", tags=["knowledge"])
v1_router.include_router(usage.router, prefix="", tags=["usage"])
v1_router.include_router(usage._global_usage_router, prefix="", tags=["usage_global"])
# ENHANCE 2026-09-14：导出格式预设（全局，无 project 前缀）
v1_router.include_router(export._presets_router, prefix="", tags=["export_presets"])
v1_router.include_router(monitor.router, prefix="", tags=["monitor"])
v1_router.include_router(api_keys.router, prefix="", tags=["api_keys"])
v1_router.include_router(api_docs.router, prefix="", tags=["api_docs"])
v1_router.include_router(batch.router, prefix="", tags=["batch_operations"])
v1_router.include_router(cache.router, prefix="", tags=["cache"])
v1_router.include_router(i18n.router, prefix="", tags=["i18n"])
v1_router.include_router(consistency.router, prefix="", tags=["consistency_audit"])
v1_router.include_router(qa.router, prefix="", tags=["qa"])
v1_router.include_router(word_count.router, prefix="", tags=["word_count"])
v1_router.include_router(websocket.router, prefix="", tags=["collaboration"])
v1_router.include_router(images.router, prefix="", tags=["images"])
v1_router.include_router(content_version.router, prefix="", tags=["content_version"])
v1_router.include_router(tasks.router, prefix="", tags=["tasks"])
v1_router.include_router(charts.router, prefix="", tags=["charts"])
v1_router.include_router(auth.router, prefix="/auth", tags=["auth"])

# 调试接口：发射一条带 request_id/project/section/task 的示例日志（F-P0-1 验证用）
# 无需开启 DEBUG：重启后端后直接 curl 即可在 backend.log 中按 rid 查找结构化日志样本。
@app.get("/api/v1/_debug/log", tags=["debug"])
def _debug_log(
    request: Request,
    project_id: str | None = None,
    section_id: str | None = None,
    task_id: str | None = None,
):
    """调试接口：发射一条带结构化 ID 的示例日志。

    用法：
      curl http://localhost:8000/api/v1/_debug/log
      curl "http://localhost:8000/api/v1/_debug/log?project_id=42&section_id=SEC9&task_id=TASK7"
    随后到 backend.log 中按响应里的 request_id 查找，确认日志行含 rid/pid/sid/tid（F-P0-1 闭环）。
    """
    if project_id:
        bind_project(project_id)
    if section_id:
        bind_section(section_id)
    if task_id:
        bind_task(task_id)
    rid = getattr(request.state, "request_id", None) or request_id_var.get()
    # 该日志行会经由 ContextFilter 注入 rid/pid/sid/tid（未绑定时为 '-'）
    logger.info("[_debug_log] 示例业务日志：导出/AI 等后续日志都会继承同一 rid")
    return {
        "request_id": rid,
        "project_id": project_id_var.get(),
        "section_id": section_id_var.get(),
        "task_id": task_id_var.get(),
        "tip": f"到 backend.log 查找 rid={rid} 的日志行，确认结构化链路（F-P0-1）已生效",
    }


# 调试接口：暴露最近错误列表（仅 DEBUG 模式下可用）
from .debug_errors import get_recent_errors


_IS_DEBUG = os.getenv("DEBUG", "0") == "1"

if _IS_DEBUG:

    @app.get("/api/v1/_debug_errors", tags=["debug"])
    def _debug_list_errors(limit: int = 5):
        return get_recent_errors(limit)

    @app.get("/api/v1/_debug_reset_db_pool", tags=["debug"])
    def _debug_reset_db_pool():
        """调试接口：重置数据库连接池（解决 disk I/O error 等连接失效问题）"""
        from .db import get_pool

        pool = get_pool()
        stats_before = pool.get_stats()
        pool.close_all()
        logger.info("数据库连接池已重置，重置前 stats: %s", stats_before)
        return {"status": "ok", "message": "数据库连接池已重置", "stats_before": stats_before}


# 结构化日志：从路由 path/query 参数自动绑定 project_id/section_id/task_id（F-P0-1）
# 这样所有业务日志（含 with_timeout 的 AI 调用耗时）都能按这三类 ID 串联定位。
# 注意：路由级依赖执行时 request.path_params 可能尚未填充，故以 URL 正则解析为主（更稳健）。
_PROJECT_ID_RE = re.compile(r"/projects/(\d+)")
_SECTION_ID_RE = re.compile(r"/sections/(\d+)")
_TASK_ID_RE = re.compile(r"/tasks/(\d+)")


def _bind_context_ids(request: Request):
    path = str(request.url.path)
    q = request.query_params
    mp = _PROJECT_ID_RE.search(path)
    if mp:
        bind_project(mp.group(1))
    ms = _SECTION_ID_RE.search(path)
    if ms:
        bind_section(ms.group(1))
    mt = _TASK_ID_RE.search(path)
    task_val = (mt.group(1) if mt else None) or q.get("task_id")
    if task_val:
        bind_task(task_val)


# 挂载 v1 路由
app.include_router(v1_router)


# ENHANCE 2026-09-29：on_event → lifespan 迁移（下方文件末尾注册）。
# 原 @app.on_event("startup") 已废弃（FastAPI 0.100+），测试日志中每轮出现
# 3+ 处 DeprecationWarning。改造为普通协程 _startup，由 lifespan context
# manager 统一调用；启动逻辑本身（DB 初始化/stuck 章节清理/行业树种子/
# 增强功能/协作表/AI 配置/提示词缓存/任务锁回收/快照对账）逐行保持不变。
async def _startup():
    _start_time = time.monotonic()
    logger.info("应用启动中...")
    init_db()

    # BUG-F-11 修复：启动时清理 stuck generating 状态的章节
    # 场景：服务器重启前正在生成的章节（status='generating'），
    # 重启后永远卡在 generating → regenerate_missing 无法识别
    try:
        from .db import get_db

        with get_db() as _conn:
            _row = _conn.execute(
                "SELECT count(*) FROM sections WHERE status='generating'"
            ).fetchone()
            _stuck = _row[0]
            if _stuck > 0:
                _conn.execute(
                    "UPDATE sections SET status='failed', updated_at=datetime('now','localtime') "
                    "WHERE status='generating'"
                )
                logger.info(
                    "[启动清理] 发现 %d 个卡在 generating 的章节，已重置为 failed",
                    _stuck,
                )
            else:
                logger.info("[启动清理] 无 stuck generating 章节")
    except Exception as _rollback_err:
        logger.warning("[启动清理] stuck generating 清理失败（忽略）: %s", _rollback_err)

    # V5.1 唯一路径防线4（已完成）：workflows_outline_v2v3 已物理删除
    # 所有目录生成统一走 workflows_outline_v51.generate_outline()

    try:
        from .services.industry_tree import seed_industry_tree

        seed_industry_tree()
    except Exception as e:
        logger.warning("行业树种子初始化失败: %s", e)

    # 初始化增强功能（后台任务，不阻塞服务启动）
    # 增强功能标记为"不影响核心功能"，启动失败不应阻止服务启动
    try:
        from .services.enhancement_orchestrator import initialize_all

        async def _init_enhancements():
            try:
                await asyncio.wait_for(initialize_all(), timeout=30.0)
                logger.info("增强功能初始化完成")
            except TimeoutError:
                logger.warning("增强功能初始化超时（30秒），跳过（不影响核心功能）")
            except Exception as e:
                logger.warning("增强功能初始化失败（不影响核心功能）: %s", e)

        _task = asyncio.create_task(_init_enhancements())
        _background_tasks.add(_task)
        _task.add_done_callback(_background_tasks.discard)
        logger.info("增强功能初始化已注册为后台任务")
    except Exception as e:
        logger.warning("增强功能初始化注册失败（不影响核心功能）: %s", e)

    # 初始化协作功能数据库表
    try:
        from .services.collaboration import init_collaboration_tables

        init_collaboration_tables()
        logger.info("协作功能数据库表初始化完成")
    except Exception as e:
        logger.warning("协作功能初始化失败（不影响核心功能）: %s", e)

    # ENHANCE 2026-10-11: repair_stats 启动回填（跨重启保留历史高频规则）
    try:
        from .services.ai.image_engine import restore_repair_stats_from_db as _restore
        _restored = _restore()
        if _restored.get("restored", 0) > 0:
            logger.info("repair_stats 启动回填: 注入 %d 条 mismatch 历史（来自 %d 条快照）",
                        _restored["restored"], _restored["snapshots_used"])
    except Exception as e:
        logger.debug("repair_stats 启动回填跳过（表不存在？）: %s", e)

    # 初始化 AI 配置服务（配置缓存预热 + 历史记录清理定时任务）
    try:
        from .services.ai.ai_config_service import startup as _ai_config_startup

        _ai_config_startup()
        logger.info("AI 配置服务初始化完成")
    except Exception as e:
        logger.warning("AI 配置服务初始化失败（不影响核心功能）: %s", e)

    # V2.1 优化：预热提示词缓存，避免首次请求时触发 DB 查询
    try:
        from .services.ai.prompts import reload_prompt_cache

        reload_prompt_cache()
        logger.info("提示词缓存预热完成")
    except Exception as e:
        logger.warning("提示词缓存预热失败（不影响核心功能）: %s", e)

    # 一致性审计数据库表已收归 `db.init_db()`（上方第 514 行已调用）。
    # BUG-FIX 2026-09-14（中）：此处原先内联 DDL 且**硬编码** `backend/data/bid_assistant.sqlite3`，
    # 绕过 `BID_ASSISTANT_DATA_DIR`；自定义数据目录下表会建错库，功能直接 `no such table`。
    logger.info("一致性审计数据库表由 init_db 统一初始化")

    # ENHANCE 2026-09-15：回收上次进程退出时来不及释放的任务组锁。
    # 进程被 kill / 容器重启时不会有释放机会，锁会一直占着直到 TTL 超时
    # （默认 30 分钟），期间同项目的任务全部被挡。启动时按时间回收一次，
    # 把「进程已死但锁还在」的窗口从 30 分钟压缩到「本次启动」。
    # 只按 expires_at 判定，不看内存状态 —— 因为多进程下别的 worker 可能
    # 正持有锁，而本进程当然不知道。
    try:
        from .services import task_lock as _task_lock

        reclaimed = _task_lock.reclaim_stale()
        logger.info("任务组锁回收完成，清理超时锁 %d 个", reclaimed)
    except Exception as e:
        logger.warning("任务组锁回收失败（不影响核心功能）: %s", e)

    # ENHANCE 2026-09-15（Task 9）：后台任务快照对账。
    # 移植易标 `recoverInterruptedOutlineGenerationTask` 的核心语义 —— 启动时
    # 把「上次进程异常退出留下的、心跳已停的活跃任务」落到明面上，而不是让它
    # 永远挂在「运行中」。参考实现在这种情况下会把任务标成 error 并提示
    # 「上次目录生成未完成，请重新生成目录。」；本软件把对账结果留在
    # `background_tasks` 里，由 GET /tasks/status 的 recovery_options 呈现给前端。
    #
    # 判据是**心跳超时**而不是「进程重启了所以全死了」：多 worker 部署下，
    # 后启动的进程会把先启动进程正在跑的任务全部误杀（Task 8 修过同类缺陷）。
    try:
        from .services import task_snapshot as _task_snapshot

        interrupted = _task_snapshot.reconcile_stale()
        if interrupted:
            logger.warning(
                "后台任务对账完成：%d 个上次未正常结束的任务已标记为中断", len(interrupted)
            )
            for _row in interrupted:
                logger.warning(
                    "  - 项目 %s 的「%s」中断于「%s」阶段（进度 %.0f%%）",
                    _row.get("project_id"),
                    _task_snapshot.TASK_TYPE_LABELS.get(
                        _row.get("task_type", ""), _row.get("task_type")
                    ),
                    _row.get("phase") or "未知",
                    float(_row.get("progress") or 0),
                )
        else:
            logger.info("后台任务对账完成：无中断遗留")
    except Exception as e:
        logger.warning("后台任务对账失败（不影响核心功能）: %s", e)

    # HARDENING 2026-10-11（遗留·审计日志无 retention）：配置审计表 ai_config_audit
    # 会长期累积。启动后注册每日清理后台任务（默认保留 90 天，
    # 环境变量 CONFIG_AUDIT_RETENTION_DAYS 可配）。清理失败不阻断主流程。
    try:
        _audit_retention_days = int(
            os.environ.get("CONFIG_AUDIT_RETENTION_DAYS", "90")
        )
        _audit_retention_days = max(1, _audit_retention_days)

        async def _audit_cleanup_loop():
            while True:
                try:
                    from .services.ai.provider_factory import cleanup_old_config_audit

                    # BUG-FIX 2026-10-14：cleanup_old_config_audit 是同步 SQLite
                    # DELETE，会持全局写锁。此前直接在事件循环里执行——首轮清理在
                    # 应用刚启动即触发，DELETE 期间整个 loop 被冻结、所有并发请求挂
                    # 起。与兄弟 provider_history 清理（ai_config_service._cleanup_loop
                    # 早已 asyncio.to_thread 化）口径对齐：派发工作线程。
                    _deleted = await asyncio.to_thread(
                        cleanup_old_config_audit, retention_days=_audit_retention_days
                    )
                    if _deleted:
                        logger.info("配置审计日志清理完成，删除 %d 条过期记录", _deleted)
                except asyncio.CancelledError:
                    raise
                except Exception as _e:
                    logger.warning("配置审计日志清理异常（忽略）: %s", _e)
                await asyncio.sleep(86400)

        _task = asyncio.create_task(_audit_cleanup_loop())
        _background_tasks.add(_task)
        _task.add_done_callback(_background_tasks.discard)
        logger.info("配置审计日志清理任务已注册（保留 %d 天）", _audit_retention_days)
    except Exception as e:
        logger.warning("配置审计日志清理任务注册失败（不影响核心功能）: %s", e)

    logger.info("应用启动完成，耗时 %.1fs", time.monotonic() - _start_time)


# ENHANCE 2026-09-29：同上，shutdown 逻辑收进 _shutdown 由 lifespan 调用。
async def _shutdown():
    """应用关闭：取消后台定时任务，避免事件循环关闭时残留未回收的 asyncio 任务"""
    try:
        from .services.ai.ai_config_service import shutdown as _ai_config_shutdown

        _ai_config_shutdown()
    except Exception as e:
        logger.warning("AI 配置服务关闭失败: %s", e)

    # HARDENING 2026-10-11：取消所有注册的后台循环任务（含审计日志每日清理），
    # 避免事件循环关闭时留下未 await 的 while-True 协程。
    for _t in list(_background_tasks):
        _t.cancel()
    _background_tasks.clear()


# ENHANCE 2026-09-29（on_event 弃用迁移）：以 lifespan context manager 注册
# 启动/关闭钩子。Starlette 在 __call__ 时才读取 router.lifespan_context，
# 此处覆盖默认 on_event 链不会丢失已有中间件/路由初始化；TestClient 进入
# 上下文、uvicorn 启动均会触发，行为与原 @app.on_event 完全一致。
@asynccontextmanager
async def _lifespan(_app: FastAPI):
    await _startup()
    try:
        yield
    finally:
        await _shutdown()


app.router.lifespan_context = _lifespan


@v1_router.get("/health")
async def health():
    return {"status": "ok"}


@v1_router.get("/health/enhanced")
async def enhanced_health_check():
    """增强版健康检查（包含所有增强服务状态）"""
    try:
        from .services.enhancement_orchestrator import enhancement_orchestrator

        status = await enhancement_orchestrator.health_check()
        return status
    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
        }


frontend_dist = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
if frontend_dist.exists():
    # 先挂载静态文件服务，处理 assets/ 等静态资源
    app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="frontend")
    # SPA fallback 中间件：对未匹配的 SPA 路由（非 /api/、非 /assets/）返回 index.html
    index_html = frontend_dist / "index.html"
    app.add_middleware(SPAIndexFallbackMiddleware, index_html_path=index_html)
