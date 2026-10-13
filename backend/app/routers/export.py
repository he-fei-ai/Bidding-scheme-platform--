import asyncio
import functools
import json
import logging
from pathlib import Path
import queue
import threading
import time
from typing import Any
import uuid
from collections import OrderedDict
from fastapi import APIRouter, Body, Depends, HTTPException, Request, Security
from fastapi.security import APIKeyHeader
from starlette.responses import FileResponse, StreamingResponse
from ..config import PROJECTS_DATA_DIR
from ..db import get_db
from ..models import ExportConfig
from ..services import task_lock as _task_lock
from ..services.export import build_docx, check_document, get_latest_export, list_exports, record_failed_export
from ..services.projects import update_step
from .project_access import enforce_project_access
from .rate_limiter import limit_per_endpoint
logger = logging.getLogger(__name__)
api_key_header = APIKeyHeader(name='X-API-Key', auto_error=False)
#: 导出任务类型（登记在 task_lock.TASK_GROUPS 的 `export` 组）
_EXPORT_TASK_TYPE = 'export'
#: BUG-FIX 2026-09-16（高 孤儿任务）：`asyncio.create_task` 返回的句柄必须被强引用，
#: 否则可能在 GC 时被回收（此时 `Task was destroyed but it is pending`）——
#: 构建跑到一半消失，队列再无生产者，前端 SSE 一直空转直到超时。
_BUILD_TASKS: set = set()

# ---- ENHANCE 2026-10-04（T3）：SSE 事件 journal（断线续传）----
#: 流终态后 journal 保留时长（10 分钟）与总量上限（50 条流），防内存无界增长
_STREAM_JOURNAL_TTL_SECONDS = 600
_STREAM_JOURNAL_MAX = 50


class _StreamJournal:
    """单条导出流的事件账本：所有投递给 SSE 流的事件带单调 seq 追加。

    线程安全：事件循环协程与断线后的 drain 线程都通过 list.append 追加
    （CPython 原子）；terminal/finished_at 为单写者标志赋值，读者最多
    多轮询一次，无正确性影响。
    """

    __slots__ = ("stream_id", "project_id", "events", "terminal", "created_at",
                 "finished_at", "progress_queue", "drain_started")

    def __init__(self, stream_id: str, project_id: int, q: "queue.Queue"):
        self.stream_id = stream_id
        self.project_id = project_id
        self.events: list[tuple[int, dict]] = []  # (seq 从 1 起, 事件 dict)
        self.terminal = False
        self.created_at = time.time()
        self.finished_at: float | None = None
        self.progress_queue = q
        self.drain_started = False

    def append(self, item: dict) -> int:
        seq = len(self.events) + 1
        self.events.append((seq, item))
        return seq

    def mark_terminal(self) -> None:
        self.terminal = True
        self.finished_at = time.time()


_STREAM_JOURNALS: "OrderedDict[str, _StreamJournal]" = OrderedDict()
_STREAM_JOURNAL_LOCK = threading.Lock()


def _register_stream_journal(journal: _StreamJournal) -> None:
    with _STREAM_JOURNAL_LOCK:
        _STREAM_JOURNALS[journal.stream_id] = journal
        # 淘汰过期与超限条目（调用方持锁，内部不再取锁）
        # BUG-FIX 2026-10-13（P1·活动流被误淘汰）：原判据
        # `(j.finished_at or j.created_at) + TTL < now` 对**未终态**的 journal
        # 用 created_at 判过期 —— 大型标书构建超过 10 分钟仍未结束时，下一次
        # 注册新流就会把它当过期条目删掉 → 客户端 resume 404 → 降级走同步导出
        # → 撞上那条仍在持有的导出组锁 → 409，用户被「导出冲突」拦住。
        # 现只淘汰**已终态且封存超 TTL** 的 journal；活动流仅受容量上限约束
        # （下方 while 分支仍是最早注册优先淘汰的硬闸，防内存无界增长）。
        now = time.time()
        expired = [
            sid for sid, j in _STREAM_JOURNALS.items()
            if j.finished_at is not None
            and j.finished_at + _STREAM_JOURNAL_TTL_SECONDS < now
        ]
        for sid in expired:
            _STREAM_JOURNALS.pop(sid, None)
        # MODULARIZATION 2026-10-12（遗留加固）：超量淘汰补充日志，便于生产观察
        # 高频导出是否触发 journal 容量上限。
        while len(_STREAM_JOURNALS) > _STREAM_JOURNAL_MAX:
            evicted_key, evicted_journal = _STREAM_JOURNALS.popitem(last=False)
            logger.info("SSE journal 超量淘汰流 %s（容量上限 %d）", evicted_key, _STREAM_JOURNAL_MAX)


def _drain_queue_to_journal(journal: _StreamJournal) -> None:
    """断线后接管：后台线程把 progress_queue 继续排入 journal，直到哨兵/超时。

    构建不因断线中断（原实现即如此，_run_build 独立跑完）；断线只是换了
    一个消费者 —— 没有这里的 drain，队列会在 _run_build 结束后囤积全部
    事件再无人读取，续传端点永远看不到终态。
    """

    def _loop() -> None:
        while True:
            try:
                item = journal.progress_queue.get(timeout=3600)
            except queue.Empty:  # 1 小时无事件：视为生产者已死，封存 journal
                journal.mark_terminal()
                break
            if item is None:  # 哨兵：_run_build 已结束
                journal.mark_terminal()
                break
            journal.append(item)

    t = threading.Thread(target=_loop, name=f"sse-journal-drain:{journal.stream_id}", daemon=True)
    t.start()


def _sse_frame(seq: int | None, event_type: str, payload: dict) -> str:
    """拼 SSE 帧；seq 非空时携 id: 行（与前端 last_event_id 续传语义对齐）。"""
    id_line = f"id: {seq}\n" if seq is not None else ""
    return f"{id_line}event: {event_type}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


async def _acquire_export_lock(project_id: int) -> tuple[str, str]:
    """抢「文档导出」组锁。冲突时抛 HTTPException(409)。

    BUG-FIX 2026-09-15（高）：原实现是**进程内** `asyncio.Lock` 字典
    （`_export_locks`），多 worker 部署下每个进程各有一把，等于没有互斥 ——
    两个请求可以同时 `build_docx` 写同一个输出文件，把文件写坏。
    现改为 Task 8 的 sqlite 行级组锁（跨进程有效），
    `TASK_GROUPS` 里登记的 `export` 组终于有了调用方。

    Returns:
        `(lock_key, owner_id)`，调用方**必须**在 finally 里释放。
    """
    owner_id = _task_lock.make_owner_id(_EXPORT_TASK_TYPE, project_id)
    try:
        record = await asyncio.to_thread(
            functools.partial(
                _task_lock.acquire,
                _EXPORT_TASK_TYPE,
                project_id=project_id,
                owner_id=owner_id,
                note=f'文档导出 project={project_id}',
            )
        )
    except _task_lock.TaskLockConflict as e:
        logger.warning('[文档导出] 组锁互斥拦截 project=%s: %s', project_id, e)
        raise HTTPException(status_code=409, detail=str(e)) from None
    return record.get('lock_key', ''), owner_id


async def _release_export_lock(lock_key: str, owner_id: str, *, why: str) -> None:
    """释放导出组锁。失败只告警 —— TTL（30 分钟）兜底。"""
    if not lock_key or not owner_id:
        return
    try:
        await asyncio.to_thread(_task_lock.release, lock_key, owner_id)
    except Exception as e:
        logger.warning('[文档导出] 释放组锁失败（TTL 兜底）%s key=%s: %s', why, lock_key, e)


def _record_export_failure(project_id: int, config: ExportConfig, error: str) -> None:
    """ENHANCE 2026-10-04（T2）：失败导出写入历史（status='failed'）。

    同步/SSE/Celery 三条链路的失败分支统一走这里；落库自身再出错
    只告警，不得把「记录失败」升级成「二次异常掩盖真实失败原因」。
    """
    try:
        record_failed_export(project_id, error, config)
    except Exception as e:  # pragma: no cover - record 内部已兜底
        logger.warning('[文档导出] 写入失败导出历史异常 project=%s: %s', project_id, e)

async def verify_auth(request: Request, api_key: str=Security(api_key_header)):
    """验证用户对项目有访问权限

    支持四种认证方式：
    1. API Key（通过 X-API-Key 请求头）
    2. 中间件预认证（ApiKeyAuthMiddleware 已设置 request.state.api_key_info）
    3. JWT 中间件预认证（JwtAuthMiddleware 已设置 request.state.current_user）
    4. 开发模式（AUTH_ENABLED=False）：无认证直接放行
    """
    from ..config import AUTH_ENABLED
    if hasattr(request.state, 'api_key_info'):
        return request.state.api_key_info
    if hasattr(request.state, 'current_user'):
        return request.state.current_user
    if api_key:
        try:
            from ..services.api_key_auth import ApiKeyService
            key_info = ApiKeyService.validate_api_key(api_key)
            if key_info:
                return key_info
        except Exception as _e:
            logger.debug('[silent-except] export.py: line 70 - %s', _e)
    if not AUTH_ENABLED:
        return {'mode': 'dev', 'user_id': 1, 'username': 'developer'}
    raise HTTPException(status_code=401, detail='缺少授权令牌')
router = APIRouter(prefix='/projects/{project_id}/export', tags=['export'])

@router.get('/docx')
async def get_docx_hint(project_id: int):
    """GET 请求 POST 接口时返回提示"""
    raise HTTPException(status_code=405, detail='请使用 POST 请求调用此接口')

@router.get('/docx-stream')
async def get_docx_stream_hint(project_id: int):
    """GET 请求 POST 接口时返回提示"""
    raise HTTPException(status_code=405, detail='请使用 POST 请求调用此接口')

@router.get('/check', dependencies=[limit_per_endpoint])
async def export_check(project_id: int, strict: bool=False, identity=Depends(verify_auth)):
    """文档完整性检查（V1.0 增强：支持 strict 参数控制严格模式）

    BUG-FIX-01 修复：添加 verify_auth 认证依赖。
    原实现缺少认证，导致未授权用户可通过 /check 端点获取文档完整性信息
    （空章节列表、未覆盖评分点、警告/错误等），属于信息泄露。

    ENHANCE 2026-09-29（P1 IDOR 收口）：verify_auth 只验「是否登录」，
    不验「项目归属」；任意登录用户可传他人 project_id 获取其文档结构。
    现接入与 sections 同口径的 enforce_project_access。
    """
    await enforce_project_access(project_id, identity)
    result = await asyncio.to_thread(check_document, project_id, strict_mode=strict)
    return result

@router.post('/docx', dependencies=[limit_per_endpoint])
async def export_docx(project_id: int, config: ExportConfig=Body(default_factory=ExportConfig), identity=Depends(verify_auth)):
    """生成并下载 DOCX（同步阻塞式）"""
    if config is None:
        config = ExportConfig()
    # ENHANCE 2026-09-29（P1 IDOR 收口）：导出会读取项目全部正文，先验属权
    await enforce_project_access(project_id, identity)
    # BUG-FIX 2026-09-18（中）：原实现调用 `check_document(project_id)` 未传
    # strict_mode，而 SSE 分支（下方 /docx-stream）传的是 `config.strict_mode`。
    # 后果：用户在同步导出接口勾选的「严格模式」完全不生效 —— 严格模式下才
    # 会判定的空章节/评分点覆盖缺口被静默放行，导出结果与用户预期不符。
    check = await asyncio.to_thread(check_document, project_id, strict_mode=config.strict_mode)
    if check.total_sections == 0:
        raise HTTPException(status_code=400, detail='文档为空，无法导出')
    # BUG-FIX 2026-09-18（P0）：判据不得用 `len(empty_sections) == total_sections`。
    # check_document 已把父章节排除在 empty_sections 之外（父章节无正文是设计如此），
    # 一旦文档里存在父章节，该等式**恒为假** —— 全空文档不再被拦住，照样导出一份
    # 0 字的 DOCX。改用与「正文挂在哪类节点」解耦的判据：总字数为 0 即为空。
    if check.total_sections > 0 and not (check.total_chars or 0):
        raise HTTPException(status_code=400, detail='所有章节内容均为空，请先生成章节内容后再导出')
    lock_key, owner_id = await _acquire_export_lock(project_id)
    # ENHANCE 2026-10-04（T4）：后台心跳续约，消除「构建超 30 分钟 → 组锁 TTL
    # 到期被 reclaim_stale 回收 → 同项目第二个导出携新锁并发 build」窗口。
    _stop_keepalive = _task_lock.start_keepalive(lock_key, owner_id)
    try:
        output_path, export_id = await asyncio.to_thread(build_docx, project_id, config=config)
        filename = Path(output_path).name
        file_size = Path(output_path).stat().st_size
        # PERF 2026-09-18（P2）：同步 DB 写移入线程池（_run_build 内的两处本就在
        # 线程里执行，不受影响；仅此处直调会阻塞事件循环）
        await asyncio.to_thread(update_step, project_id, 6)
        # BUG-FIX 2026-09-18（低）：原实现是「死代码」——
        #   try: str(actual_path.relative_to(base_dir))
        #   except ValueError: Path(output_path).name
        # 异常分支既没有 return 也没有赋值，越界路径不会被拦截，只是白算一次。
        # 改为真正校验：导出产物必须落在项目数据目录内，否则拒绝返回。
        base_dir = PROJECTS_DATA_DIR.resolve()
        actual_path = Path(output_path).resolve()
        try:
            actual_path.relative_to(base_dir)
        except ValueError:
            logger.error('导出产物路径越界，拒绝返回: %s', actual_path)
            raise HTTPException(status_code=500, detail='导出产物路径异常，已拒绝下载')
        return {'filename': filename, 'file_path': f'/api/v1/projects/{project_id}/export/download/{export_id}' if export_id else None, 'download_url': f'/api/v1/projects/{project_id}/export/download/{export_id}' if export_id else None, 'file_size': file_size, 'export_id': export_id, 'check': check.model_dump()}
    except FileNotFoundError as e:
        logger.error('导出文件丢失: %s', e)
        _record_export_failure(project_id, config, f'导出文件生成后丢失: {e}')
        raise HTTPException(status_code=500, detail=f'导出文件生成后丢失: {e}') from e
    except PermissionError as e:
        logger.error('导出权限错误: %s', e)
        _record_export_failure(project_id, config, f'文件系统权限错误: {e}')
        raise HTTPException(status_code=500, detail=f'文件系统权限错误: {e}') from e
    except Exception as e:
        logger.exception('DOCX 导出失败')
        _record_export_failure(project_id, config, f'{type(e).__name__}: {str(e)[:200]}')
        raise HTTPException(status_code=500, detail=f'导出失败: {type(e).__name__}: {str(e)[:200]}') from e
    finally:
        _stop_keepalive()
        await _release_export_lock(lock_key, owner_id, why=f'同步导出 project={project_id}')

@router.post('/html', dependencies=[limit_per_endpoint])
async def export_html(project_id: int, config: ExportConfig=Body(default_factory=ExportConfig), identity=Depends(verify_auth)):
    """ENHANCE 2026-10-12：HTML 导出格式（新增第二交付通道）。

    复用同一套章节树 + parse_content 内容组装链，渲染端换成简洁 HTML。
    图表标记按「无降级」原则输出占位符 div（不伪造 Mermaid 图，真实图表需
    DOCX/PDF 格式）。支持 LibreOffice headless 自动转换 HTML → PDF。

    与 DOCX/PDF 并立，不替代任何既有端点；前端可在导出配置页选择格式。
    """
    if config is None:
        config = ExportConfig()
    await enforce_project_access(project_id, identity)
    # BUG-FIX 2026-10-13（P1·空文档口径不一致）：DOCX 与 SSE 分支都对空文档
    # 返回 400，唯独 HTML 分支直接放行 → 产出只有封面没有正文的空壳文件。
    check = await asyncio.to_thread(check_document, project_id, strict_mode=config.strict_mode)
    if check.total_sections == 0:
        raise HTTPException(status_code=400, detail='文档为空，无法导出')
    # BUG-FIX 2026-10-13（P1·无互斥双写）：HTML 产物与 DOCX 同落 exports/ 目录，
    # 但未抢导出组锁 → 两个 HTML 请求（或 HTML 与 DOCX 并发）各自生成产物，
    # 同名时互相截断。抢同一把组锁后，冲突方得到 409 而不是半截文件。
    lock_key, owner_id = await _acquire_export_lock(project_id)
    _stop_keepalive = _task_lock.start_keepalive(lock_key, owner_id)
    try:
        from ..services.html_export import build_html

        path, _fingerprint = await asyncio.to_thread(build_html, project_id, config)

        _p = Path(path)
        if not _p.exists():
            raise HTTPException(status_code=500, detail="HTML 导出产物落盘失败")
        return FileResponse(path=str(_p), filename=_p.name, media_type="text/html; charset=utf-8")
    except Exception as e:
        # HTTPException 直接上抛，保持 400/409/500 语义不被「导出失败」包装
        if isinstance(e, HTTPException):
            raise
        logger.exception('HTML 导出失败')
        _record_export_failure(project_id, config, f'{type(e).__name__}: {str(e)[:200]}')
        raise HTTPException(status_code=500, detail=f'导出失败: {type(e).__name__}: {str(e)[:200]}') from e
    finally:
        _stop_keepalive()
        await _release_export_lock(lock_key, owner_id, why=f'HTML 导出 project={project_id}')

@router.post('/docx-stream', dependencies=[limit_per_endpoint])
async def export_docx_stream(request: Request, project_id: int, config: ExportConfig=Body(default_factory=ExportConfig), identity=Depends(verify_auth)):
    """SSE 流式导出：实时推送构建进度

    事件格式：
    - {"event": "progress", "step": "...", "detail": "...", "progress": 0-1}
    - {"event": "completed", "filename": "...", "file_path": "<下载URL>", "relative_path": "<磁盘相对路径>", "download_url": "...", "file_size": ...}
    - {"event": "error", "detail": "..."}
    """
    if config is None:
        config = ExportConfig()
    # ENHANCE 2026-09-29（P1 IDOR 收口）：SSE 流式导出同样先验项目属权
    await enforce_project_access(project_id, identity)
    t0 = time.time()
    check = await asyncio.to_thread(check_document, project_id, strict_mode=config.strict_mode)
    t1 = time.time()
    logger.info('SSE 导出 check_document 耗时: %.3f 秒 (project_id=%d, strict_mode=%s)', t1 - t0, project_id, config.strict_mode)
    if check.total_sections == 0:
        raise HTTPException(status_code=400, detail='文档为空，无法导出')
    # BUG-FIX 2026-09-18（P0）：判据不得用 `len(empty_sections) == total_sections`。
    # check_document 已把父章节排除在 empty_sections 之外（父章节无正文是设计如此），
    # 一旦文档里存在父章节，该等式**恒为假** —— 全空文档不再被拦住，照样导出一份
    # 0 字的 DOCX。改用与「正文挂在哪类节点」解耦的判据：总字数为 0 即为空。
    if check.total_sections > 0 and not (check.total_chars or 0):
        raise HTTPException(status_code=400, detail='所有章节内容均为空，请先生成章节内容后再导出')
    # BUG-FIX 2026-09-15（高）：锁必须在构建启动**之前**抢到。
    # 原实现先 `create_task(_run_build)` 再在 SSE 生成器里拿锁，而
    # `asyncio.to_thread` 会立即把 `_run_build` 提交到线程池 —— build_docx
    # 全程跑在锁外面，两个并发请求能同时写同一个输出文件。
    lock_key, owner_id = await _acquire_export_lock(project_id)
    # ENHANCE 2026-10-04（T4）：SSE 路径同样心跳续约（stop 在 _run_build finally
    # 释放锁前调用）；客户端断线不中断构建，续约亦不中断，语义一致。
    _stop_keepalive = _task_lock.start_keepalive(lock_key, owner_id)
    # T6 闭环说明（2026-10-04）：队列故意保持无界 —— 有界化后 put_nowait 在
    # 高压下会真实丢事件（包括 completed/error 终态），比囤积内存更坏；
    # 事件量有天然上界（章节数×进度点），断线场景由 T3 的 journal drain
    # 持续消费，不存在无人读取的无界增长。下方 queue.Full 分支保留作防御。
    progress_queue: queue.Queue = queue.Queue()

    def _on_progress(step: str, detail: str, progress: float):
        try:
            progress_queue.put_nowait({'event': 'progress', 'step': step, 'detail': detail, 'progress': round(progress, 4)})
        except queue.Full:
            logger.warning('SSE 导出进度队列已满，丢弃进度事件: step=%s, progress=%.2f', step, progress)
        except Exception as _e:
            logger.debug('[silent-except] export.py: line 247 - %s', _e)

    def _emit_log(level: str, content: str):
        """V1.0 增强：发送 log 事件到 SSE 流"""
        try:
            progress_queue.put_nowait({'event': 'log', 'level': level, 'content': content, 'timestamp': time.time()})
        except queue.Full:
            pass

    def _run_build():
        """后台构建线程：执行 build_docx 并把进度/结果投递给 SSE 流

        关键修复（P0-2）：
        1. 失败时也调用 update_step(6) 进行步骤同步，确保前端步骤条不会卡住
        2. 显式捕获并记录异常类型，便于前端错误面板归类
        3. finally 中放入哨兵 None，保证 SSE 流一定结束
        4. 失败时通过 progress_queue 推 error 事件（不抛 HTTPException，
           因为 StreamingResponse 已经发出 200 + SSE 流，不能改响应头）
        """
        output_path: str | None = None
        error_detail: str | None = None
        try:
            _emit_log('info', '开始构建 DOCX 文档...')
            output_path, export_id = build_docx(project_id, config=config, on_progress=_on_progress)
            filename = Path(output_path).name
            file_size = Path(output_path).stat().st_size
            _emit_log('info', f'文档构建完成: {filename} ({file_size} bytes)')
            update_step(project_id, 6)
            try:
                check_summary = check
                check_result = {'passed': check_summary.passed, 'total_sections': check_summary.total_sections, 'total_chars': check_summary.total_chars, 'empty_sections': len(check_summary.empty_sections), 'uncovered_scoring_points': len(check_summary.uncovered_scoring_points), 'warnings': check_summary.warnings[:5] if check_summary.warnings else [], 'errors': check_summary.errors[:5] if check_summary.errors else []}
            except Exception as e:
                logger.warning('SSE 导出完成后获取 check 摘要失败: %s', e)
                check_result = None
            download_url = f'/api/v1/projects/{project_id}/export/download/{export_id}' if export_id else None
            if export_id is None:
                # BUG-FIX 2026-09-18（P2）：_record_export 失败时 export_id=None，
                # 原流程照常报 completed，前端下载链接为空且用户不知原因。
                # 现显式携带告警，前端可提示“导出历史中不可再查”。
                _emit_log('warn', '导出记录未能写入数据库：本次文件可直接下载，但不会出现在导出历史中')
            # BUG-FIX 2026-10-05（低·漏改路径）：此处是同步导出路径
            # BUG-FIX 2026-09-18（低）「死代码」修复的**并行副本**——当时只改了
            # POST /docx（见上方 relative_to 分支），漏掉了 SSE 流式路径。原实现
            # `except ValueError: rel_path = Path(output_path).name` 会把「产物落在
            # 项目数据目录之外」这一越界情况静默降级成裸文件名写进 completed 事件，
            # 越界路径不会被拦截，只是白算一次（同步路径已明确判定为需拒绝返回）。
            # 现改为真正校验：越界即视为构建失败，走下方 error 分支（线程内抛错，
            # 由 except 统一 emit error 事件并回滚步骤），与同步路径同一口径。
            base_dir = PROJECTS_DATA_DIR.resolve()
            actual_path = Path(output_path).resolve()
            try:
                rel_path = str(actual_path.relative_to(base_dir))
            except ValueError:
                logger.error('导出产物路径越界，拒绝返回: %s', actual_path)
                raise ValueError(f'导出产物路径异常，已拒绝下载: {actual_path}')
            # ENHANCE 2026-10-04（P2 跨模块数据传递统一）：同步 POST /docx 响应的
            # file_path 早已收敛为下载 URL 口径，而 SSE completed 事件的 file_path
            # 仍是相对 PROJECTS_DATA_DIR 的磁盘路径，同一字段名两种语义。
            # 现对齐为 URL 口径（与同步响应/前端类型声明一致），并新增
            # relative_path 保留原始磁盘相对路径供运维排查与历史消费者兼容；
            # download_url 字段不变。前端下载链路由 export_id/latest 拼接，
            # 不依赖此字段拼接路径，改动无破坏性。
            progress_queue.put_nowait({'event': 'completed', 'filename': filename, 'file_path': download_url, 'relative_path': rel_path, 'download_url': download_url, 'file_size': file_size, 'export_id': export_id, 'record_failed': export_id is None, 'check': check_result})
        except FileNotFoundError as e:
            error_detail = f'文件丢失: {e}'
            _emit_log('error', f'文件丢失: {e}')
            logger.error('SSE 导出文件丢失: %s', e)
        except PermissionError as e:
            error_detail = f'文件系统权限错误: {e}'
            _emit_log('error', f'文件系统权限错误: {e}')
            logger.error('SSE 导出权限错误: %s', e)
        except Exception as e:
            error_detail = f'{type(e).__name__}: {str(e)[:200]}'
            _emit_log('error', f'导出失败: {error_detail}')
            logger.exception('SSE 导出失败')
        finally:
            if error_detail is not None:
                # ENHANCE 2026-10-04（T2）：SSE 失败同样写入导出历史（与同步/Celery 同口径）
                _record_export_failure(project_id, config, error_detail)
                try:
                    update_step(project_id, 5)
                except Exception:
                    logger.warning('导出失败时同步步骤失败')
                progress_queue.put_nowait({'event': 'error', 'detail': error_detail})
            progress_queue.put_nowait(None)
            # 释放导出组锁：本函数跑在线程池里，`task_lock.release` 是同步的，直接调。
            # 放在 finally 是因为「构建失败」也必须放锁，否则该项目 30 分钟内导不出文档。
            _stop_keepalive()  # ENHANCE 2026-10-04（T4）：先停续约再释放，避免交错
            if lock_key and owner_id:
                try:
                    _task_lock.release(lock_key, owner_id)
                except Exception as _rel_e:
                    logger.warning('[文档导出] 释放组锁失败（TTL 兜底）key=%s: %s', lock_key, _rel_e)
    progress_queue.put_nowait({'event': 'start', 'project_id': project_id, 'message': '开始导出文档...', 'total_tasks': 6, 'estimated_time': '约3-5分钟'})
    # ENHANCE 2026-10-04（T3）：本流的事件账本；stream_id 随 connected 事件透出，
    # 前端断线后携 last_event_id 重连 GET /docx-stream/resume/{stream_id}。
    stream_id = uuid.uuid4().hex
    journal = _StreamJournal(stream_id, project_id, progress_queue)
    _register_stream_journal(journal)

    async def event_stream():
        # 组锁已在路由层抢到（跨进程 sqlite 行级锁），构建必须在这里才启动 ——
        # 早于此处启动就会跑在锁外面（原实现就是这么坏的）。
        # 锁由 `_run_build` 的 finally 释放。
        _build_task = asyncio.create_task(asyncio.to_thread(_run_build))
        _BUILD_TASKS.add(_build_task)
        _build_task.add_done_callback(_BUILD_TASKS.discard)
        t_start = time.time()
        connected_payload = {'event': 'connected', 'project_id': project_id, 'stream_id': stream_id}
        yield _sse_frame(journal.append(connected_payload), 'connected', connected_payload)
        logger.info('SSE 导出第一个事件发送耗时: %.3f 秒 (project_id=%d)', time.time() - t_start, project_id)
        last_heartbeat = time.time()
        ping_interval = 15
        first_data_sent = False
        try:
            while True:
                if await request.is_disconnected():
                    logger.info('SSE 导出流客户端已断开 (project_id=%d)', project_id)
                    # ENHANCE 2026-10-04（T3）：断线不再吞事件 —— 后台线程接管
                    # 排队列进 journal，客户端可通过续传端点接回剩余事件。
                    if not journal.drain_started:
                        journal.drain_started = True
                        _drain_queue_to_journal(journal)
                    return
                batch: list = []
                try:
                    while len(batch) < 100:
                        batch.append(progress_queue.get_nowait())
                except queue.Empty:
                    pass
                if not batch:
                    elapsed_since_start = time.time() - last_heartbeat
                    if elapsed_since_start >= ping_interval:
                        # keep_alive 是瞬时心跳，不进 journal（无续传价值）
                        yield _sse_frame(None, 'keep_alive', {'event': 'keep_alive', 'timestamp': time.time()})
                        last_heartbeat = time.time()
                    await asyncio.sleep(0.05)
                    continue
                for item in batch:
                    if item is None:
                        journal.mark_terminal()
                        return
                    event_type = item.get('event')
                    if event_type in ('start', 'progress', 'log', 'completed', 'error') and (not first_data_sent):
                        first_data_sent = True
                        last_heartbeat = time.time()
                    yield _sse_frame(journal.append(item), event_type, item)
                    if event_type in ('completed', 'error'):
                        journal.mark_terminal()
                        return
        except Exception as e:
            logger.exception('SSE 导出流异常: %s', e)
            # BUG-FIX 2026-09-18（P2）：原仅记日志不产出任何事件，流直接终止 ——
            # 前端收不到终态信号，只能空转到超时（进度条卡死）。
            # 现补发 error 事件，让前端立刻感知失败并展示原因。
            # ENHANCE 2026-10-04（T3）：error 事件同样进 journal，续传端可看到终态。
            try:
                _err_payload = {'event': 'error', 'detail': f'导出流异常: {type(e).__name__}: {str(e)[:200]}'}
                yield _sse_frame(journal.append(_err_payload), 'error', _err_payload)
                journal.mark_terminal()
            except Exception:
                pass

    return StreamingResponse(event_stream(), media_type='text/event-stream; charset=utf-8', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no', 'Connection': 'keep-alive'})


@router.get('/docx-stream/resume/{stream_id}', dependencies=[limit_per_endpoint])
async def export_docx_stream_resume(request: Request, project_id: int, stream_id: str, last_event_id: int = 0, identity=Depends(verify_auth)):
    """ENHANCE 2026-10-04（T3）：SSE 导出流断线续传。

    客户端断线不中断构建（_run_build 继续跑到终态，事件由 drain 线程落入
    journal）；携 last_event_id 重连本端点，回放其后的事件直到终态。

    作用域与兼容：journal 仅在本进程内有效 —— 多 worker 部署需会话亲和；
    过期/不存在/跨 worker 命中不到时返回 404，前端按既有链路降级
    （导出历史 / latest 兜底），不丢数据。keep_alive 不进 journal，
    因此 id 序列只覆盖有业务价值的事件。
    """
    await enforce_project_access(project_id, identity)
    # FIX 2026-10-11（E-10·P1·锁一致性）：旧实现直接 `_STREAM_JOURNALS.get(stream_id)`
    # 未持锁，与 `_register_stream_journal` 的写操作存在理论竞态（虽 CPython GIL
    # 保证 dict 原子性，但代码一致性缺失，多解释器如 PyPy/Jython 下不安全）。
    # 现统一持锁读取，与 `_register_stream_journal` / 淘汰逻辑同口径。
    with _STREAM_JOURNAL_LOCK:
        journal = _STREAM_JOURNALS.get(stream_id)
    if journal is None or journal.project_id != project_id:
        raise HTTPException(status_code=404, detail='导出流不存在或已过期，请在导出历史中查看结果')

    async def _resume_stream():
        last_sent = max(int(last_event_id or 0), 0)
        deadline = time.time() + 1800  # 30 分钟上限，与导出组锁 TTL 口径一致
        while True:
            # 快照读取：list 迭代中并发 append 不会移动已读元素（CPython）
            batch = [(seq, item) for seq, item in journal.events if seq > last_sent]
            for seq, item in batch:
                event_type = item.get('event', 'message')
                yield _sse_frame(seq, event_type, item)
                last_sent = seq
                if event_type in ('completed', 'error'):
                    return
            if await request.is_disconnected():
                return
            if journal.terminal:
                return
            if time.time() > deadline:
                yield _sse_frame(None, 'error', {'event': 'error', 'detail': '导出流续传等待超时，请在导出历史中查看结果'})
                return
            await asyncio.sleep(0.1)

    return StreamingResponse(_resume_stream(), media_type='text/event-stream; charset=utf-8', headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no', 'Connection': 'keep-alive'})

@router.get('/download/{export_id}', dependencies=[limit_per_endpoint])
async def download_export(project_id: int, export_id: int, identity=Depends(verify_auth)):
    """下载指定导出文件，带权限验证和路径安全检查"""
    # ENHANCE 2026-09-29（P1 IDOR 收口）：下方 SQL 已用 project_id 限定 export_id
    # 归属（跨项目取不到行），但 project_id 本身仍未验属权——需先验调用者有权访问该项目
    await enforce_project_access(project_id, identity)

    # PERF（2026-10-05）：纯读 readonly + to_thread（含 resolve/exists 文件 I/O），
    # 不再在事件循环内抢全局写锁
    def _locate_file():
        with get_db(readonly=True) as conn:
            row = conn.execute('SELECT file_path, filename, status FROM exports WHERE id = ? AND project_id = ?', (export_id, project_id)).fetchone()
        if not row:
            return ('not_found', '导出记录不存在')
        # ENHANCE 2026-10-04（T2）：失败记录（file_path=''）不得进入下载链路
        if (row['status'] or 'completed') != 'completed':
            return ('not_found', '该导出记录为失败记录，无可下载文件')
        file_path = row['file_path']
        base_dir = PROJECTS_DATA_DIR.resolve()
        actual_path = Path(file_path).resolve()
        if not actual_path.is_relative_to(base_dir):
            return ('forbidden', '非法文件路径访问')
        if not actual_path.exists():
            return ('not_found', '文件已被删除')
        if actual_path.suffix.lower() not in {'.docx', '.dotx'}:
            return ('bad_type', '文件类型不支持')
        return ('ok', str(actual_path), row['filename'])

    result = await asyncio.to_thread(_locate_file)
    if result[0] == 'not_found':
        raise HTTPException(status_code=404, detail=result[1])
    if result[0] == 'forbidden':
        raise HTTPException(status_code=403, detail=result[1])
    if result[0] == 'bad_type':
        raise HTTPException(status_code=400, detail=result[1])
    return FileResponse(path=result[1], filename=result[2], media_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document')

@router.get('/latest', dependencies=[limit_per_endpoint])
async def download_latest(project_id: int, identity=Depends(verify_auth)):
    """下载最新导出文件"""
    # ENHANCE 2026-09-29（P1 IDOR 收口）：先验项目属权再取导出文件
    await enforce_project_access(project_id, identity)

    # PERF（2026-10-06）：本端点是 PERF 2026-10-05 迁移的**漏网之鱼**——
    # 同批的 download_export / export_pdf 都已改为 readonly + to_thread，
    # 而这里仍是 `get_latest_export()`（写锁 get_db）+ `Path.resolve()/exists()`
    # 文件 I/O 裸跑在事件循环里：①抢占全局写锁，与导出事务争用；
    # ②前端下载按钮点击后，整个 loop 被阻塞，其它 SSE/请求一并卡住。
    # 现与兄弟端点同口径：整段（含 DB 读与文件 I/O）移入线程池 + 只读连接。
    def _locate_latest():
        file_path = get_latest_export(project_id, readonly=True)
        if not file_path:
            return ('not_found', None)
        path_obj = Path(file_path).resolve()
        base_dir = PROJECTS_DATA_DIR.resolve()
        if not path_obj.is_relative_to(base_dir):
            return ('forbidden', None)
        if not path_obj.exists():
            return ('gone', None)
        # ENHANCE 2026-10-04（P2 口径对齐）：与 download_export 同口径限制后缀，
        # 避免 exports 表历史脏行（指向任意文件）时本端点被当作任意文件读取入口。
        if path_obj.suffix.lower() not in {'.docx', '.dotx'}:
            return ('bad_type', None)
        return ('ok', str(path_obj))

    kind, located = await asyncio.to_thread(_locate_latest)
    if kind == 'not_found':
        raise HTTPException(status_code=404, detail='暂无导出记录')
    if kind == 'forbidden':
        raise HTTPException(status_code=403, detail='非法文件路径访问')
    if kind == 'gone':
        raise HTTPException(status_code=404, detail='文件已被删除')
    if kind == 'bad_type':
        raise HTTPException(status_code=400, detail='文件类型不支持')
    return FileResponse(path=located, filename=Path(located).name, media_type='application/vnd.openxmlformats-officedocument.wordprocessingml.document')

@router.get('/pdf/{export_id}', dependencies=[limit_per_endpoint])
async def export_pdf(project_id: int, export_id: int, identity=Depends(verify_auth)):
    """ENHANCE 2026-10-04（T1）：把既有 DOCX 导出产物按需转换为 PDF 下载。

    - DOCX 是唯一事实源：仅对 completed 记录的 .docx/.dotx 产物调 LibreOffice
      headless 转换；同名 .pdf 已存在且不早于源文件时直接复用。
    - 未安装 LibreOffice → 501 中文提示（安装或设 PDF_SOFFICE_PATH）；
      DOCX 导出链路完全不受影响，向后兼容。
    - 转换在子进程，移入线程池避免阻塞事件循环；产物路径同样受
      PROJECTS_DATA_DIR 越界校验约束。
    """
    await enforce_project_access(project_id, identity)

    # BUGFIX（2026-10-05）：base_dir 需在外层作用域可见，供 _locate_docx 越界校验与
    # 转换后 PDF 产物路径校验共用（此前被误移入闭包导致 line 640 NameError）
    base_dir = PROJECTS_DATA_DIR.resolve()

    # PERF（2026-10-05）：纯读 readonly + to_thread（含 resolve/exists 文件 I/O），
    # 不再在事件循环内裸抢全局写锁
    def _locate_docx():
        with get_db(readonly=True) as conn:
            row = conn.execute('SELECT file_path, filename, status FROM exports WHERE id = ? AND project_id = ?', (export_id, project_id)).fetchone()
            row = dict(row) if row else None
        if not row or (row['status'] or 'completed') != 'completed':
            return ('not_found', None)
        actual_path = Path(row['file_path']).resolve()
        if not actual_path.is_relative_to(base_dir):
            return ('forbidden', None)
        if actual_path.suffix.lower() not in {'.docx', '.dotx'}:
            return ('bad_type', None)
        if not actual_path.exists():
            return ('gone', None)
        return ('ok', actual_path)

    kind, actual_path = await asyncio.to_thread(_locate_docx)
    if kind == 'not_found':
        raise HTTPException(status_code=404, detail='导出记录不存在或为失败记录')
    if kind == 'forbidden':
        raise HTTPException(status_code=403, detail='非法文件路径访问')
    if kind == 'bad_type':
        raise HTTPException(status_code=400, detail='仅支持由 Word 文档转换 PDF')
    if kind == 'gone':
        raise HTTPException(status_code=404, detail='源文件已被删除，请重新导出 DOCX')
    from ..services.pdf_export import PdfUnavailable, convert_docx_to_pdf
    try:
        pdf_path = await asyncio.to_thread(convert_docx_to_pdf, actual_path)
    except PdfUnavailable as e:
        raise HTTPException(status_code=501, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception('PDF 转换失败 project=%s export=%s', project_id, export_id)
        _record_export_failure(project_id, ExportConfig(), f'PDF 转换失败: {type(e).__name__}: {str(e)[:150]}')
        raise HTTPException(status_code=500, detail=f'PDF 转换失败: {str(e)[:200]}') from e
    if not pdf_path.resolve().is_relative_to(base_dir):
        logger.error('PDF 产物路径越界，拒绝返回: %s', pdf_path)
        raise HTTPException(status_code=500, detail='导出产物路径异常，已拒绝下载')
    return FileResponse(path=str(pdf_path), filename=pdf_path.name, media_type='application/pdf')


@router.get('/history', dependencies=[limit_per_endpoint])
async def export_history(project_id: int, identity=Depends(verify_auth)):
    """导出历史记录"""
    # ENHANCE 2026-09-29（P1 IDOR 收口）：导出历史含文件名/时间/尺寸等项目侧信息
    await enforce_project_access(project_id, identity)
    # PERF（2026-10-06）：与 download_latest 同批修复。`list_exports` 是同步函数，
    # 内部 `get_db()` 取写锁连接，且对每一行做 `Path(fp).resolve()` 文件 I/O
    # —— 历史条目多时是「DB 写锁 + N 次 resolve」裸跑在事件循环里。
    # 整段移入线程池并改走只读连接（服务层 readonly 关键字，默认值保持旧口径）。
    return await asyncio.to_thread(list_exports, project_id, readonly=True)


# ==================== 全局导出预设路由（无 project 前缀） ====================

_presets_router = APIRouter(prefix='/export', tags=['export_presets'])


@_presets_router.get('/presets')
async def list_export_presets(identity=Depends(verify_auth)):
    """列出全部导出格式预设（版式 + 主题）。

    ENHANCE 2026-09-14：新增端点，移植自易标 `EXPORT_LAYOUT_PRESETS` /
    `EXPORT_THEME_PRESETS`。预设为全局静态数据、不含任何项目敏感信息，
    因此不需要鉴权；前端据此渲染「一键套用」选择器，开放 API 调用方也可
    只传 `{"preset_id": "formal-binding", "theme_id": "blue"}` 直接得到成品排版。

    SECURITY 2026-10-12（GAP-T04·遗留加固）：原端点无鉴权暴露。虽然预设不含
    敏感数据，但信息暴露面本身属于安全加固范畴——任何匿名请求都能枚举出
    系统已安装的版式/主题预设（可用于探测系统功能边界）。现与同模块其他端点
    统一应用 `Depends(verify_auth)`，前端 `getExportPresets()` 已带 auth header，
    向后完全兼容。
    """
    from ..services.export_presets import list_presets

    return list_presets()
