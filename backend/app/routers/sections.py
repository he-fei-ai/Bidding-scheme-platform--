"""项目正文路由 - 含 CRUD、生成、权限验证和速率限制

包含以下端点：
- GET /：获取章节树
- GET /flat：获取章节列表（扁平）
- GET /{section_id}：获取单个章节
- POST /：创建章节
- PATCH /{section_id}：更新章节
- DELETE /{section_id}：删除章节
- DELETE /：清空章节
- POST /{section_id}/move：移动章节
- POST /reorder：重排序
- POST /review：评审大纲
- POST /review-with-autofix：评审并自动修复
- GET /content-persistence-snapshot：内容持久化快照
- GET /generate-sse：生成目录（SSE流）
- GET /generate-content：并发生成正文（SSE流）
- POST /stop-generation：停止生成任务（需权限验证）
- POST /expand-content：兜底扩写（SSE流）

所有端点均启用速率限制，部分端点需权限验证。
"""
import asyncio
import json
import logging
from typing import Annotated, Any
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from starlette.responses import StreamingResponse
from ..config import (
    CONTENT_USE_CONSISTENCY_AUDIT_DEFAULT,
    CONTENT_USE_ORCHESTRATION_DEFAULT,
    CONTENT_USE_WORD_SAMPLING_DEFAULT,
)
from ..db import DatabaseWriteLockTimeout, get_db
from ..models import ReorderRequest, SectionCreate, SectionUpdate
from ..security.auth import get_current_user
from ..services.generation_manager import generation_manager
from ..services.task_lock import TASK_LABELS, get_active_lock, is_lock_live
from ..services.sections import build_tree, clear_project_sections, create_section, delete_section_tree, get_section, get_section_word_budget, list_sections, move_section, reorder_sections, update_section
from ..services.v51_state import cancel_v51_task, clear_v51_state_from_db, get_v51_confirm_version, get_v51_last_reject_reason, get_v51_state, load_v51_state_from_db, resume_v51_task
from .rate_limiter import limit_per_endpoint
from .sse_handlers import expand_content_impl, generate_content_impl, generate_outline_sse_impl
from .task_control import pause_generation, stop_generation
logger = logging.getLogger(__name__)
router = APIRouter(prefix='/projects/{project_id}/sections', tags=['sections'])

# 修复：DDL 表结构不会在运行时变化，'created_by' 列是否存在对整个进程是常量。
# 一次性缓存 + 一次性 WARNING 标记，避免每次 PRAGMA 查询 + 重复 WARNING。
_projects_has_created_by = None
_projects_created_by_warned = False

def _load_project_owner_row(project_id: int):
    """PERF（2026-10-05）：权限门的同步 DB 读取段，供 `asyncio.to_thread` 派发。

    只读查询统一走 `get_db(readonly=True)` 跳过全局写锁；此前在 `async def`
    端点必经路径里直接执行，正文生成高峰期写锁争用（超时上限 60s）会冻结
    整个事件循环，拖死所有并发 SSE 心跳与其他请求（同 09-16 B-W4 口径）。
    """
    global _projects_has_created_by

    # 一次性缓存列存在性（DDL 不会变，进程内只查一次 PRAGMA）
    if _projects_has_created_by is None:
        with get_db(readonly=True) as conn:
            columns = {row['name'] for row in conn.execute('PRAGMA table_info(projects)').fetchall()}
        _projects_has_created_by = 'created_by' in columns

    has_created_by = _projects_has_created_by
    with get_db(readonly=True) as conn:
        if has_created_by:
            row = conn.execute('SELECT id, created_by FROM projects WHERE id = ?', (project_id,)).fetchone()
        else:
            row = conn.execute('SELECT id FROM projects WHERE id = ?', (project_id,)).fetchone()
    # 同步段内即转纯 dict：sqlite3.Row 依附游标语句，跨线程回传前物化最稳妥
    return dict(row) if row else None


async def require_project_permission(project_id: int, current_user: Any):
    """验证用户是否有权限操作指定项目"""
    global _projects_created_by_warned

    row = await asyncio.to_thread(_load_project_owner_row, project_id)
    has_created_by = _projects_has_created_by
    if not row:
        raise HTTPException(status_code=404, detail='项目不存在')
    if not has_created_by:
        if not _projects_created_by_warned:
            _projects_created_by_warned = True
            logger.warning('projects 表无 created_by 列，跳过权限检查（向后兼容模式，仅首次警告）')
        return
    if current_user and hasattr(current_user, 'id') and current_user.id:
        try:
            uid = int(current_user.id)
        except (ValueError, TypeError):
            uid = None
        row_dict = row
        if uid and row_dict.get('created_by') is not None and (row_dict['created_by'] != uid):
            raise HTTPException(status_code=403, detail='无权访问该项目')
        elif uid is not None and row_dict.get('created_by') is None:
            logger.info('项目 %d 无创建者记录，允许访问', project_id)

def _ensure_section_in_project(project_id: int, section_id: int) -> None:
    """BUG-FIX 2026-09-18（P1）：section_id 与 project_id 是两个独立路径参数，
    服务层 update/delete/move 只按 id 定位 —— 用自己的 project_id 配别人的
    section_id 即可跨项目改写/删除任意章节（IDOR）。
    图表类端点早已做该校验，现统一到全部 CRUD 端点。
    """
    section = get_section(section_id)
    if not section:
        raise HTTPException(status_code=404, detail='章节不存在')
    if section.project_id != project_id:
        raise HTTPException(status_code=404, detail='章节不属于该项目')


def _live_group_lock(project_id: int) -> dict | None:
    """读取该项目所在技术方案任务组的存活锁（不存在或已过期返回 None）。

    只看行镇锁不看进程内状态：多 worker 部署下 `TaskRegistry` 是进程内单例，
    跨进程看不见彼此的任务；而锁行 sink 到 sqlite，任何 worker 都能读到同一个
    判定依据（`services/task_lock.py` 的文档已说明这条取舍）。
    """
    lock = get_active_lock('outline-generation', project_id=project_id)
    if not lock:
        return None
    if not is_lock_live(lock.get('lock_key', '')):
        # 持有者进程崩溃留下的超时锁，对调用方等价于无锁
        return None
    return lock


def _reject_if_generation_running(project_id: int, *, allow_content_edit: bool=False) -> None:
    """ENHANCE 2026-09-15：目录编辑的任务运行中防护（返回 409）。

    可研侧早就用 `TaskRunningError` → 409 拦住了「AI 任务运行中改大纲」
    （`services/feasibility.py::assert_no_running_task`），而技术方案侧所有
    章节编辑端点一直是裸奔：SSE 目录生成过程中用户仍可并发增删改章节，
    生成收尾时 `_clear_and_save_outline` 的 `DELETE FROM sections` + 全量
    INSERT 会把人工改动静默覆盖（`routers/sse/sse_db_ops.py`）。

    粒度上有意区分两种任务，避免把正常用法一起挡掉：
    - **目录生成中**：结构性改动（增删改 / 移动 / 重排）与章节内容改写都不允许 ——
      结束时整表会被重建，任何改动都必然丢失；
    - **正文生成中**：只允许继续手写章节内容（`allow_content_edit` 的路由不改结构），
      结构性改动仍拒绝 —— 会让正在跑的章节映射到已经不存在的节点。
    """
    lock = _live_group_lock(project_id)
    if lock is None:
        return
    task_type = lock.get('task_type', '')
    if task_type != 'outline-generation' and allow_content_edit:
        return
    label = TASK_LABELS.get(task_type, task_type or 'AI 生成任务')
    raise HTTPException(
        status_code=409,
        detail=f'当前正在执行「{label}」，目录已锁定，请任务结束后再编辑章节。',
    )


@router.get('')
async def list_sections_route(project_id: int, current_user: Annotated[Any, Depends(get_current_user)]=None):
    """获取章节树"""
    # PERF（2026-10-06）：build_tree 走 DB + 60s 缓存，直调会阻塞事件循环
    return await asyncio.to_thread(build_tree, project_id)

@router.get('/flat')
async def list_sections_flat_route(project_id: int, current_user: Annotated[Any, Depends(get_current_user)]=None):
    """获取章节列表（扁平结构）"""
    # PERF（2026-10-06）：list_sections 对每章完整正文跑 _count_words，
    # 大项目单次数十 MB 级扫描，必须移出事件循环并走只读连接。
    return await asyncio.to_thread(list_sections, project_id, readonly=True)

@router.get('/content-persistence-snapshot', dependencies=[limit_per_endpoint])
async def content_persistence_snapshot_route(project_id: int, current_user: Annotated[Any, Depends(get_current_user)]=None):
    """获取内容持久化快照（仅返回摘要信息，不含完整章节内容）"""
    sections = await asyncio.to_thread(list_sections, project_id, readonly=True)
    total_sections = len(sections)
    count = sum((1 for s in sections if s.content))
    summaries = [{'id': s.id, 'title': s.title, 'level': s.level, 'status': s.status, 'has_content': bool(s.content), 'word_count': s.word_count, 'parent_id': s.parent_id, 'sort_order': s.sort_order} for s in sections]
    return {'total_sections': total_sections, 'count': count, 'snapshot': summaries}

@router.get('/word-budget')
async def word_budget_route(project_id: int, level2_default: int=Query(3500, ge=1000, le=10000), current_user: Annotated[Any, Depends(get_current_user)]=None):
    """计算项目章节字数预算

    BUG O5 修复：原缺失该路由，前端调用此端点时被 FastAPI 错误匹配到
    `/{section_id}` 动态路由，导致 Pydantic 422 错误。
    """
    return get_section_word_budget(project_id, level2_default)

@router.get('/generation-status')
async def generation_status_route(project_id: int, current_user: Annotated[Any, Depends(get_current_user)]=None):
    """获取项目正文生成任务状态（用于前端轮询/恢复判断）

    BUG O5 修复：原缺失该路由，前端调用此端点时被 FastAPI 错误匹配到
    `/{section_id}` 动态路由，导致 Pydantic 422 错误：
    "path.section_id: Input should be a valid integer"。
    修复：在 /{section_id} 之前定义此静态路由。
    """
    progress = await generation_manager.get_project_progress(project_id)
    return progress

@router.post('/resume-generation')
async def resume_generation_route(project_id: int, current_user: Annotated[Any, Depends(get_current_user)]=None):
    """恢复项目的正文生成任务（断点续生）

    BUG O5 修复：原缺失该路由，恢复生成按钮调用 404。
    """
    generation_id = await generation_manager.resume_task(project_id)
    if not generation_id:
        return {'status': 'error', 'message': '无可恢复的暂停任务'}
    return {'status': 'ok', 'message': '已恢复生成', 'generation_id': generation_id, 'action': 'resumed'}

@router.get('/generate-sse', dependencies=[limit_per_endpoint])
async def generate_outline_sse(project_id: int, strategy: str | None=Query(None), outline_mode: str=Query('auto', pattern='^(auto|direct|stepwise)$'), current_user: Annotated[Any, Depends(get_current_user)]=None):
    """SSE 流式生成目录（EventSource 只支持 GET）

    2026-08-23 V4 增强：V5.1 唯一路径防线1 — 入口收口
    任何 legacy strategy 参数被静默丢弃，强制走 V5.1

    契约BUG修复 2026-09-15：`strategy` 原先用 `request.query_params` 手工读取，
    没有在 OpenAPI 契约里显式声明，前端 `generateOutlineSSEUrl` 传的参数对
    自动生成文档/校验器不可见。现改为显式 Query 参数，语义不变。
    """
    legacy_strategy = strategy
    if legacy_strategy and legacy_strategy != 'v51':
        logger.warning('[目录生成] 收到 legacy strategy=%s，已强制降级为 V5.1', legacy_strategy)
    try:
        if current_user:
            await require_project_permission(project_id, current_user)
    except HTTPException as _e:
        _error_detail = _e.detail

        async def error_generator():
            yield f"data: {json.dumps({'status': 'error', 'message': _error_detail}, ensure_ascii=False)}\n\n"
        return StreamingResponse(error_generator(), media_type='text/event-stream; charset=utf-8', headers={'Cache-Control': 'no-cache, no-transform', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no', 'Content-Encoding': 'none'})
    except Exception as _e:
        _error_msg = f'权限验证失败: {_e!s}'

        async def error_generator():
            yield f"data: {json.dumps({'status': 'error', 'message': _error_msg}, ensure_ascii=False)}\n\n"
        return StreamingResponse(error_generator(), media_type='text/event-stream; charset=utf-8', headers={'Cache-Control': 'no-cache, no-transform', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no', 'Content-Encoding': 'none'})
    return await generate_outline_sse_impl(project_id, outline_mode=outline_mode)

@router.get('/generate-content', dependencies=[limit_per_endpoint])
async def generate_content(request: Request, project_id: int, writing_length: str=Query('standard'), regenerate_ids: str=Query(''), root_section_id: int | None=Query(None), regenerate_missing: bool=Query(False), use_orchestration: bool=Query(CONTENT_USE_ORCHESTRATION_DEFAULT), level2_default: int=Query(3500, ge=1000, le=10000), use_word_sampling: bool=Query(CONTENT_USE_WORD_SAMPLING_DEFAULT), use_fallback_expand: bool=Query(False), use_consistency_audit: bool=Query(CONTENT_USE_CONSISTENCY_AUDIT_DEFAULT), concurrency: int=Query(4, ge=1, le=8), orch_max_ai_images: int=Query(6, ge=0, le=50), orch_table_ratio: str=Query('moderate'), orch_prefer_mermaid: bool=Query(True), orch_section_per_segment: int=Query(10, ge=3, le=40), use_word_adjust: bool | None=Query(None), current_user: Annotated[Any, Depends(get_current_user)]=None):
    """SSE 并发生成正文（EventSource 只支持 GET）

    2026-08-21 修复：权限验证和参数验证异常包装在 SSE 流中，确保始终返回 text/event-stream

    ENHANCE 2026-10-06：三项质量门禁（use_orchestration / use_word_sampling /
    use_consistency_audit）的默认值改读 config（env 可覆盖），**默认仍为 False**，
    与改动前逐字一致 —— 运维想全开可用 CONTENT_USE_*_DEFAULT=true 统一打开，
    前端显式传参依旧优先。不在此处静默翻转默认值是因为三项门禁都会显著增加
    AI 调用量与耗时，属产品决策。

    FIX 2026-10-12：补齐 use_word_adjust 的 router 暴露与位置传参。
    易标 R9/R10/R11 已在 generate_content_impl 末位实现该参数（None = 服务端
    按 config 判定，true/false = 单次强制），但本 router 漏了同名 Query 形参、
    调用点也未传 —— 结果是客户端传入的 use_word_adjust 被 FastAPI 静默丢弃，
    永远退回服务端默认。追加在 current_user 之前、且保持与 impl 同序，是
    「位置参数调用不整体错位」的硬约束（见 generate_content_impl 内注释）。
    不传时行为与改动前逐字一致，向后兼容。
    """
    try:
        if current_user:
            await require_project_permission(project_id, current_user)
        valid_lengths = ('short', 'standard', 'detailed', 'extra_long')
        if writing_length not in valid_lengths:
            raise HTTPException(status_code=422, detail=f'writing_length 必须是 {valid_lengths} 之一')
    except HTTPException as _e:
        _error_detail = _e.detail

        async def error_generator():
            yield f"data: {json.dumps({'status': 'error', 'message': _error_detail}, ensure_ascii=False)}\n\n"
        return StreamingResponse(error_generator(), media_type='text/event-stream; charset=utf-8', headers={'Cache-Control': 'no-cache, no-transform', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no', 'Content-Encoding': 'none'})
    except Exception as _e:
        _error_msg = f'验证失败: {_e!s}'

        async def error_generator():
            yield f"data: {json.dumps({'status': 'error', 'message': _error_msg}, ensure_ascii=False)}\n\n"
        return StreamingResponse(error_generator(), media_type='text/event-stream; charset=utf-8', headers={'Cache-Control': 'no-cache, no-transform', 'Connection': 'keep-alive', 'X-Accel-Buffering': 'no', 'Content-Encoding': 'none'})
    return await generate_content_impl(project_id, writing_length, regenerate_ids, root_section_id, regenerate_missing, use_orchestration, level2_default, use_word_sampling, use_fallback_expand, use_consistency_audit, concurrency, orch_max_ai_images, orch_table_ratio, orch_prefer_mermaid, orch_section_per_segment, use_word_adjust)

@router.get('/v51-pending-task')
async def v51_pending_task_route(project_id: int, current_user: Annotated[Any, Depends(get_current_user)]=None):
    """查询项目是否存在未完成的 V5.1 任务（断点续生）

    Returns:
        dict: {
            "has_pending": bool,
            "phase": str | None,   # 当前阶段 level1/level2/final
            "outline": list | None, # 待审核的目录树（仅 level1 阶段有）
            "message": str
        }
    """
    if current_user:
        await require_project_permission(project_id, current_user)
    state = get_v51_state(project_id)
    if state and (not state.cancelled):
        return {'has_pending': True, 'phase': state.current_phase, 'outline': state.full_outline or state.level1_outline, 'message': f'存在进行中的 V5.1 任务，当前阶段: {state.current_phase}'}
    db_state = load_v51_state_from_db(project_id)
    if db_state:
        return {'has_pending': True, 'phase': db_state.current_phase, 'outline': db_state.full_outline or db_state.level1_outline, 'message': f'存在未完成的 V5.1 任务（已持久化），当前阶段: {db_state.current_phase}'}
    return {'has_pending': False, 'phase': None, 'outline': None, 'message': '没有进行中的 V5.1 任务'}

@router.post('/v51-clear-pending')
async def v51_clear_pending_route(project_id: int, current_user: Annotated[Any, Depends(get_current_user)]=None):
    """清除未完成的 V5.1 任务状态（用于用户决定重新开始生成时）"""
    if current_user:
        await require_project_permission(project_id, current_user)
    clear_v51_state_from_db(project_id)
    cancel_v51_task(project_id)
    return {'status': 'ok', 'message': '已清除未完成的 V5.1 任务'}

@router.get('/{section_id}')
async def get_section_route(project_id: int, section_id: int, current_user: Annotated[Any, Depends(get_current_user)]=None):
    """获取单个章节"""
    _ensure_section_in_project(project_id, section_id)
    return get_section(section_id)

@router.post('', dependencies=[limit_per_endpoint], status_code=201)
async def create_section_route(project_id: int, data: SectionCreate, current_user: Annotated[Any, Depends(get_current_user)]=None):
    """创建章节"""
    if current_user:
        await require_project_permission(project_id, current_user)
    _reject_if_generation_running(project_id)
    # BUG-FIX 2026-09-18（P1）：防跨项目注入子章节 —— parent_id 必须属于同一项目
    if data.parent_id is not None:
        _ensure_section_in_project(project_id, data.parent_id)
    return create_section(project_id, data)

@router.patch('/{section_id}')
async def update_section_route(project_id: int, section_id: int, data: SectionUpdate, current_user: Annotated[Any, Depends(get_current_user)]=None):
    """更新章节

    正文生成期间仍允许改写章节内容（用户边生成边润色是正常用法），
    但结构/标题类字段的改动会同时被拒。
    """
    if current_user:
        await require_project_permission(project_id, current_user)
    _reject_if_generation_running(project_id, allow_content_edit=True)
    _ensure_section_in_project(project_id, section_id)
    try:
        section = await asyncio.to_thread(update_section, section_id, data)
    except ValueError as e:
        # BUG-FIX 2026-09-18（P1）：update_section 对 parent_id 环路/跨项目移动
        # 抛 ValueError，这里转 400 返回可读提示，而非 500
        raise HTTPException(status_code=400, detail=str(e))
    if not section:
        raise HTTPException(status_code=404, detail='章节不存在')
    return section

@router.delete('/{section_id}', dependencies=[limit_per_endpoint])
async def delete_section_route(project_id: int, section_id: int, current_user: Annotated[Any, Depends(get_current_user)]=None):
    """删除章节（含子章节）"""
    if current_user:
        await require_project_permission(project_id, current_user)
    _reject_if_generation_running(project_id)
    _ensure_section_in_project(project_id, section_id)
    result = await asyncio.to_thread(delete_section_tree, section_id)
    if result.get('deleted_count', 0) == 0:
        raise HTTPException(status_code=404, detail='章节不存在')
    return {'status': 'deleted'}

@router.delete('')
async def clear_sections_route(project_id: int, current_user: Annotated[Any, Depends(get_current_user)]=None):
    """清空项目所有章节

    BUG-FIX 2026-09-29：补齐项目归属权限校验——同文件其余写端点均调
    require_project_permission，此处此前裸奔，任意登录用户可清空他人项目全部分章。
    """
    if current_user:
        await require_project_permission(project_id, current_user)
    try:
        await stop_generation(project_id)
    except Exception as _e:
        logger.warning('[清空章节] 停止生成任务失败（忽略）: %s', _e)
    cancel_v51_task(project_id)
    clear_project_sections(project_id)
    return {'status': 'cleared'}

@router.post('/{section_id}/move', dependencies=[limit_per_endpoint])
async def move_section_route(project_id: int, section_id: int, direction: str=Query(pattern='^(up|down)$'), current_user: Annotated[Any, Depends(get_current_user)]=None):
    """移动章节（上移/下移）"""
    if current_user:
        await require_project_permission(project_id, current_user)
    _reject_if_generation_running(project_id)
    _ensure_section_in_project(project_id, section_id)
    if not move_section(section_id, direction):
        raise HTTPException(status_code=400, detail='无法移动章节：已在边界位置或章节不存在')
    return await asyncio.to_thread(build_tree, project_id)

@router.patch('/{section_id}/target-words')
async def update_section_target_words_route(project_id: int, section_id: int, target_words: int=Query(default=0, ge=0, le=100000), current_user: Annotated[Any, Depends(get_current_user)]=None):
    """更新章节目标字数

    前端在正文编辑页与字数统计页设置章节目标字数（0 表示恢复默认）。

    Args:
        project_id: 项目ID
        section_id: 章节ID
        target_words: 目标字数（0 或省略 = 恢复默认，使用全局配置）

    Returns:
        dict: {status, section_id, target_words}
    """
    # BUG-FIX 2026-09-18（P1）：原端点既不校验项目归属，也不拦生成中任务；
    # target_words 是字数采样预估/兑底扩写的判定依据，生成中并发改写会
    # 导致扩写判定与最终落库字数不一致。现补齐两道防线。
    if current_user:
        await require_project_permission(project_id, current_user)
    _reject_if_generation_running(project_id)
    _ensure_section_in_project(project_id, section_id)
    section = await asyncio.to_thread(update_section, section_id, SectionUpdate(target_words=None if target_words == 0 else target_words))
    if not section:
        raise HTTPException(status_code=404, detail='章节不存在')
    logger.info('更新章节目标字数: section=%d, target_words=%s', section_id, section.target_words)
    return {'status': 'ok', 'section_id': section_id, 'target_words': section.target_words}

@router.patch('/{section_id}/word-count-tier')
async def update_section_word_count_tier_route(
    project_id: int,
    section_id: int,
    word_count_tier: str = Query(default='', description='档位 key: brief/standard/detailed/long；空串=清除覆盖，跟随全局默认'),
    current_user: Annotated[Any, Depends(get_current_user)] = None,
):
    """更新章节字数档位（三级目录控制）

    2026-10-08 新增：字数控制下沉到三级目录（L3）后，每个 L3 可独立设置
    档位（brief/standard/detailed/long）。传空串表示清除覆盖，跟随全局默认。

    Args:
        project_id: 项目 ID
        section_id: 章节 ID
        word_count_tier: 档位 key（brief/standard/detailed/long），空串=清除覆盖

    Returns:
        dict: {status, section_id, word_count_tier, tier_label, tier_target}
    """
    from ..services.ai.writing_length_config import get_tier_config, get_default_tier_config

    # 校验档位 key
    valid_keys = {'brief', 'standard', 'detailed', 'long', ''}
    if word_count_tier not in valid_keys:
        raise HTTPException(status_code=400, detail=f'无效的档位 key: {word_count_tier}，可选值: {sorted(valid_keys - {""})}')

    if current_user:
        await require_project_permission(project_id, current_user)
    _reject_if_generation_running(project_id)
    _ensure_section_in_project(project_id, section_id)

    # 空串 = 清除覆盖（设为 None）
    tier_value = word_count_tier if word_count_tier else None
    section = await asyncio.to_thread(
        update_section, section_id, SectionUpdate(word_count_tier=tier_value)
    )
    if not section:
        raise HTTPException(status_code=404, detail='章节不存在')

    # 返回解析后的档位信息
    effective_tier = section.word_count_tier or 'standard'
    tier_cfg = get_tier_config(effective_tier) or get_default_tier_config()

    logger.info('更新章节档位: section=%d, word_count_tier=%s', section_id, section.word_count_tier)
    return {
        'status': 'ok',
        'section_id': section_id,
        'word_count_tier': section.word_count_tier,
        'tier_label': tier_cfg.get('label'),
        'tier_target': tier_cfg.get('target_words'),
        'tier_tolerance': tier_cfg.get('tolerance_ratio'),
    }

@router.post('/reorder', dependencies=[limit_per_endpoint])
async def reorder_sections_route(project_id: int, data: ReorderRequest, current_user: Annotated[Any, Depends(get_current_user)]=None):
    """重排序章节

    BUG-FIX 2026-09-29：补齐项目归属权限校验（与其余写端点一致）。
    """
    if current_user:
        await require_project_permission(project_id, current_user)
    _reject_if_generation_running(project_id)
    return reorder_sections(project_id, [g.model_dump() for g in data.groups])

async def _prepare_review(project_id: int) -> tuple:
    """提取评审大纲路由的公共逻辑：初始化 AI 配置、查询评分点、获取目录树"""
    from ..services.ai.provider_factory import create_provider, get_ai_config
    try:
        ai_config = get_ai_config()
        provider = create_provider(ai_config)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'AI 配置异常: {e!s}') from e
    # PERF（2026-10-06）：原实现把「查评分点」与「建目录树」两段同步重操作直接
    # 跑在事件循环里（get_db() 抢全局写锁 + build_tree 全表扫描），会冻结并发请求。
    # 现各自包进同步闭包并派发到线程池；纯读查询改走 readonly 连接。
    # 三条错误分支（AI 配置异常 / 未识别评分点 / 查询失败）与文案保持原样。
    def _read_scoring_points():
        with get_db(readonly=True) as conn:
            return conn.execute(
                'SELECT scoring_points_json FROM analysis WHERE project_id = ?',
                (project_id,),
            ).fetchone()

    try:
        row = await asyncio.to_thread(_read_scoring_points)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'查询评分点失败: {e!s}') from e
    if not row:
        raise HTTPException(status_code=400, detail='请先完成评分点识别')
    scoring_points = row['scoring_points_json'] or '[]'
    try:
        outline_data = await asyncio.to_thread(build_tree, project_id)
        outline = json.dumps([n.model_dump() if hasattr(n, 'model_dump') else dict(n) for n in outline_data], ensure_ascii=False, indent=2)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'获取目录树失败: {e!s}') from e
    return (provider, scoring_points, outline)

@router.post('/review', dependencies=[limit_per_endpoint])
async def review_outline_route(project_id: int, current_user: Annotated[Any, Depends(get_current_user)]=None):
    """评审大纲"""
    if current_user:
        await require_project_permission(project_id, current_user)
    from ..services.ai.workflows import review_outline as _review_outline
    provider, scoring_points, outline = await _prepare_review(project_id)
    try:
        outline_list = json.loads(outline)
    except (TypeError, ValueError):
        outline_list = outline
    result = await _review_outline(provider, outline_list, scoring_points, '', '')
    return {'status': 'completed', 'result': result}

@router.post('/review-with-autofix', dependencies=[limit_per_endpoint])
async def review_outline_with_autofix_route(project_id: int, current_user: Annotated[Any, Depends(get_current_user)]=None):
    """评审大纲并自动修复

    BUG-FIX 2026-09-29（跨模块数据传递断裂）：此前修复成功时只把 AI 原始
    fixed_outline（无数据库 id/sort_order 的裸 JSON）透传前端直接 setTree，
    后续对节点的改名/删除/移动请求全部打在 id=undefined 上链路断裂，且刷新
    后修复结果丢失。现在修复成功后走与目录生成同一落库链 _clear_and_save_outline
    （含旧正文按 (level,title) 快照回填），并额外返回带数据库 ID 的完整目录树。
    """
    if current_user:
        await require_project_permission(project_id, current_user)
    from ..services.ai.workflows import review_outline_with_autofix as _review_outline_with_autofix
    provider, scoring_points, outline = await _prepare_review(project_id)
    result = await _review_outline_with_autofix(provider, scoring_points, outline)
    if isinstance(result, dict) and result.get('repaired') and result.get('fixed_outline'):
        from .sse.sse_db_ops import _clear_and_save_outline

        # BUG-FIX 2026-10-07：日志实证 8 次
        #   app.routers.sections:489 自动修复结果落库失败 project=990308: db locked
        # `_clear_and_save_outline` 是「删除旧目录 + 批量插入 + 正文回填」的长写事务，
        # 需要长时间持有 db.py 的进程级写锁；与之并发的正文生成 SSE 流水线
        # （_save_section_to_db 持续写 sections）会把它挤到 8 秒超时。
        # 这是纯粹的瞬时争用 —— 整个函数在单个事务内执行，失败必然回滚，
        # 因此整体重跑是安全的。这里做有限次数退避重试，避免把争用直接暴露成 500。
        _MAX_AUTOFIX_SAVE_RETRIES = 3
        _save_err: Exception | None = None
        for _attempt in range(1, _MAX_AUTOFIX_SAVE_RETRIES + 1):
            try:
                await asyncio.to_thread(_clear_and_save_outline, project_id, result["fixed_outline"])
                _save_err = None
                break
            except DatabaseWriteLockTimeout as e:
                _save_err = e
                if _attempt >= _MAX_AUTOFIX_SAVE_RETRIES:
                    break
                logger.warning(
                    "自动修复结果落库遇写锁争用，第 %d/%d 次重试 project=%d: %s",
                    _attempt, _MAX_AUTOFIX_SAVE_RETRIES, project_id, e,
                )
                await asyncio.sleep(_attempt * 0.8)
            except Exception as e:
                # BUG-FIX 2026-10-08（容错/500 语义）：此前只捕获
                # DatabaseWriteLockTimeout，其余异常（sqlite3.OperationalError
                # "database is locked" 的原生形态、IntegrityError、OSError 磁盘满、
                # 快照/回填链路内部断言……）全部裸穿出路由：
                #   · TestClient 场景直接 raise → 表现为「测试炸红」；
                #   · 生产场景 Starlette 返回无结构的 500，前端拿不到任何可读原因，
                #     且此时**目录已被 DELETE 清空、正文尚未回填**（_clear_and_save_outline
                #     内部单事务，失败必回滚，故目录仍是旧的）——用户看到的是
                #     「AI 修复成功了，但页面目录没变」，前后端状态分裂且无从排查。
                # 现按「可重试争用」与「不可重试失败」分治：前者 503 提示稍后重试，
                # 后者 500 且 detail 含「保存失败」与原因，前端可提示用户重试/联系管理员。
                _save_err = e
                logger.error(
                    "自动修复结果落库失败（非写锁争用）project=%d attempt=%d: %s",
                    project_id, _attempt, e, exc_info=True,
                )
                break
        if _save_err is not None:
            if isinstance(_save_err, DatabaseWriteLockTimeout):
                logger.error(
                    "自动修复结果落库重试耗尽（写锁争用）project=%d: %s", project_id, _save_err
                )
                raise HTTPException(
                    status_code=503,
                    detail="自动修复结果保存失败：数据库正忙（并发写入），请稍后重试",
                ) from _save_err
            logger.error("自动修复结果落库失败 project=%d: %s", project_id, _save_err, exc_info=True)
            raise HTTPException(
                status_code=500,
                detail=f"自动修复结果保存失败：{_save_err!s}",
            ) from _save_err
        result['tree'] = await asyncio.to_thread(build_tree, project_id)
    return {'status': 'completed', 'result': result}

@router.post('/v51-confirm', dependencies=[limit_per_endpoint])
async def v51_confirm_route(project_id: int, data: dict, current_user: Annotated[Any, Depends(get_current_user)]=None):
    """V5.1 人工审核确认

    接收用户编辑后的目录树，恢复 V5.1 生成任务继续下一阶段。

    Args:
        project_id: 项目 ID
        data: 包含 edited_outline 的请求体
            {
                "edited_outline": [...]  # 用户编辑后的目录树
            }

    Returns:
        dict: {"status": "ok", "phase": "next_phase"}
    """
    if current_user:
        await require_project_permission(project_id, current_user)
    state = get_v51_state(project_id)
    if not state:
        from ..db import get_db_with_retry

        # PERF（2026-10-05）：get_db_with_retry() 无 readonly 会抢全局写锁，
        # 纯读查询派发至工作线程，不在事件循环内久持锁
        def _check_auto_confirmed():
            with get_db_with_retry(readonly=True) as conn:
                row = conn.execute('SELECT current_step FROM projects WHERE id = ?', (project_id,)).fetchone()
            return bool(row and row['current_step'] == 4)

        if await asyncio.to_thread(_check_auto_confirmed):
            return {'status': 'already_completed', 'message': 'V5.1 目录已自动生成完成（自动确认模式）'}
        raise HTTPException(status_code=404, detail='没有进行中的 V5.1 生成任务')
    edited_outline = data.get('edited_outline')
    if edited_outline is None:
        raise HTTPException(status_code=422, detail='缺少 edited_outline 字段')
    expected_version = get_v51_confirm_version(project_id)
    success = resume_v51_task(project_id, edited_outline, expected_version=expected_version)
    if not success:
        # BUG-FIX 2026-10-11（O-7）：按拒绝原因区分 HTTP 状态码，
        # 让用户/前端能给出可操作提示，而不是笼统返回"冲突"。
        #   · version_conflict → 409 Conflict（乐观锁冲突，刷新重试）
        #   · depth_mismatch   → 400 Bad Request（当前 phase 不匹配 outline 深度）
        #   · invalid_outline  → 400 Bad Request（outline 格式非法）
        #   · 其他 / 无原因     → 409（兜底，保持旧行为兼容）
        reason = get_v51_last_reject_reason(project_id)
        current_phase = state.current_phase if state else 'unknown'
        if reason == 'depth_mismatch':
            # 计算 outline 实际深度以在 detail 里给出可操作提示
            from ..services.v51_state import _outline_max_depth as _od, _V51_PHASE_ALLOWED_DEPTH as _rngs
            _rng = _rngs.get(current_phase)
            rng_text = f"[{_rng[0]},{_rng[1]}]" if _rng else "未定义"
            raise HTTPException(
                status_code=400,
                detail=(
                    f'提交的目录深度（{_od(edited_outline)}）不符合当前审核阶段 {current_phase} '
                    f'允许的范围 {rng_text}。请在当前阶段只提交该层级目录，'
                    f'后续层级将由系统自动生成。'
                ),
            )
        if reason == 'invalid_outline':
            raise HTTPException(
                status_code=400,
                detail='edited_outline 必须是列表（[{...}, ...]），而非其他类型',
            )
        raise HTTPException(status_code=409, detail='V5.1 确认冲突：其他标签页或操作已提交了修改，请刷新页面后重试')
    if state.current_phase == 'level1':
        next_phase = 'level2'
    elif state.current_phase == 'level2':
        next_phase = 'level3'
    elif state.current_phase == 'final':
        next_phase = 'done'
    else:
        next_phase = 'unknown'
    return {'status': 'ok', 'phase': state.current_phase, 'next_phase': next_phase}

@router.post('/stop-generation', dependencies=[limit_per_endpoint])
async def stop_generation_route(project_id: int, current_user: Annotated[Any, Depends(get_current_user)]=None):
    """停止指定项目的生成任务（需权限验证）"""
    if current_user:
        await require_project_permission(project_id, current_user)
    cancel_v51_task(project_id)
    result = await stop_generation(project_id)
    return {'status': 'cancelled', 'message': result.get('message', '生成已停止')}

@router.post('/pause-generation', dependencies=[limit_per_endpoint])
async def pause_generation_route(project_id: int, current_user: Annotated[Any, Depends(get_current_user)]=None):
    """暂停指定项目的生成任务（需权限验证）

    与 stop-generation 的区别：
    - stop: 完全终止，章节状态改为 failed，任务状态改为 cancelled
    - pause: 暂停，章节保留 generating 状态，任务状态改为 paused，
      后续可通过 /resume-generation 恢复断点续生
    """
    if current_user:
        await require_project_permission(project_id, current_user)
    result = await pause_generation(project_id)
    return result

@router.post('/expand-content', dependencies=[limit_per_endpoint])
async def expand_content(request: Request, project_id: int, writing_length: str=Query('standard'), level2_default: int=Query(3500, ge=1000, le=10000), concurrency: int | None = Query(None, ge=1, le=8), current_user: Annotated[Any, Depends(get_current_user)]=None):
    """兜底扩写：对字数不足的章节并发扩写，确保内容充实（SSE 流）"""
    if current_user:
        await require_project_permission(project_id, current_user)
    valid_lengths = ('short', 'standard', 'detailed', 'extra_long')
    if writing_length not in valid_lengths:
        raise HTTPException(status_code=422, detail=f'writing_length 必须是 {valid_lengths} 之一')
    return await expand_content_impl(project_id, writing_length, level2_default, concurrency)

@router.post('/{section_id}/generate-illustration')
async def generate_illustration(section_id: int, project_id: int, request: Request, prompt: str='', style: str='engineering_diagram', size: str='16:9', current_user: Annotated[Any, Depends(get_current_user)]=None):
    """为章节生成配图（三步：AI 编排 → AI 生成提示词 → AI 生成图像）"""
    from ..services.ai.image_engine import build_illustration_url, generate_illustration_arrange, generate_illustration_image, generate_illustration_prompt
    if current_user:
        await require_project_permission(project_id, current_user)
    section = get_section(section_id)
    if not section:
        raise HTTPException(status_code=404, detail='章节不存在')
    if section.project_id != project_id:
        raise HTTPException(status_code=404, detail='章节不属于该项目')
    from ..services.ai.provider_factory import create_provider, get_ai_config
    ai_config = get_ai_config()
    if not ai_config or not ai_config.get('api_key'):
        return {'success': False, 'error': '请先配置文本模型 API Key'}
    text_provider = create_provider(ai_config)
    from ..services.ai.image_providers.service import load_image_config
    image_config = load_image_config()
    all_sections = await asyncio.to_thread(list_sections, project_id, readonly=True)
    overview_section = next((s for s in all_sections if s.title == '项目概述'), None)
    industry_profile = (ai_config or {}).get('document_profile', '')
    arrange_result = await generate_illustration_arrange(text_provider, section_title=section.title or '', section_content=section.content or '', project_overview=overview_section.content if overview_section else '', industry_profile=industry_profile)
    if not arrange_result.get('needed', False):
        _prev_url = ''
        if section.illustration_data:
            try:
                _prev_url = json.loads(section.illustration_data).get('image_url', '') or ''
            except Exception:
                _prev_url = ''
                logger.warning('[配图] illustration_data 损坏，无法解析: section_id=%d', section_id)
        return {'success': True, 'imageUrl': _prev_url, 'image_url': _prev_url, 'style': section.illustration_type or 'engineering_diagram', 'prompt': '', 'description': '', 'reason': arrange_result.get('reason', '该章节内容不需要配图'), 'step': 'arrange'}
    prompt_result = await generate_illustration_prompt(text_provider, section_title=section.title or '', section_content=section.content or '', section_path='...', industry_profile=industry_profile)
    generated_prompt = prompt_result.get('prompt', prompt or arrange_result.get('reason', ''))
    generated_style = prompt_result.get('style', style)
    generated_description = prompt_result.get('description', '')
    image_url = None
    _image_key = (image_config or {}).get('image_api_key_enc') or ''
    if image_config and image_config.get('image_enabled', True) and _image_key:
        image_url = await generate_illustration_image(text_provider, prompt=generated_prompt, style=generated_style, section_size=size, provider_name=image_config.get('image_provider', 'agnes'), image_config=image_config)
        _last_step = 'image'
    else:
        _last_step = 'arrange'
        if not _image_key:
            image_url = build_illustration_url(generated_prompt, image_size='landscape_16:9')
        else:
            image_url = None
    if not image_url:
        image_url = build_illustration_url(generated_prompt, image_size='landscape_16:9')
    try:
        from ..db import get_db_with_retry

        # PERF（2026-10-05）：持久化写操作派发至工作线程（写锁在 worker 阻塞）
        def _persist_illustration():
            with get_db_with_retry() as conn:
                conn.execute("UPDATE sections SET illustration_type = ?, illustration_data = ?, updated_at = datetime('now', 'localtime') WHERE id = ?", (generated_style, json.dumps({'prompt': generated_prompt, 'style': generated_style, 'description': generated_description, 'image_url': image_url, 'image_size': size}, ensure_ascii=False), section_id))
                conn.commit()

        await asyncio.to_thread(_persist_illustration)
    except Exception as e:
        logger.warning('持久化配图结果失败: %s', e)
        return {'success': False, 'error': '配图已生成但保存失败，请重试'}
    return {'success': True, 'imageUrl': image_url, 'image_url': image_url, 'style': generated_style, 'prompt': generated_prompt, 'description': generated_description, 'reason': arrange_result.get('reason', ''), 'step': _last_step}

@router.post('/{section_id}/generate-flowchart')
async def generate_flowchart(section_id: int, project_id: int, current_user: Annotated[Any, Depends(get_current_user)]=None):
    """AI 生成流程图（Mermaid）"""
    if current_user:
        await require_project_permission(project_id, current_user)
    section = get_section(section_id)
    if not section:
        raise HTTPException(status_code=404, detail='章节不存在')
    if section.project_id != project_id:
        raise HTTPException(status_code=404, detail='章节不属于该项目')
    from ..services.ai.provider_factory import create_provider, get_ai_config
    ai_config = get_ai_config()
    if not ai_config or not ai_config.get('api_key'):
        raise HTTPException(status_code=503, detail='未配置 AI 模型，请先在管理页面配置')
    provider = create_provider(ai_config)
    if not provider:
        raise HTTPException(status_code=503, detail='无法创建 AI 提供商')
    getattr(section, 'summary', '') or ''
    scoring_standard = getattr(section, 'scoring_standard', '') or ''
    industry_profile = getattr(section, 'industry_profile', '') or ''
    project_overview = getattr(section, 'project_overview', '') or ''
    from ..services.ai.image_engine import generate_mermaid_flowchart, validate_mermaid
    mermaid_code = await generate_mermaid_flowchart(provider, section_title=section.title, section_content=section.content or '', scoring_standard=scoring_standard, industry_profile=industry_profile, project_overview=project_overview)
    valid, _ = validate_mermaid(mermaid_code)
    return {'mermaid_code': mermaid_code, 'valid': valid}

@router.post('/{section_id}/arrange-flowchart')
async def arrange_flowchart(section_id: int, project_id: int, current_user: Annotated[Any, Depends(get_current_user)]=None):
    """AI 编排判断：是否需要流程图"""
    if current_user:
        await require_project_permission(project_id, current_user)
    section = get_section(section_id)
    if not section:
        raise HTTPException(status_code=404, detail='章节不存在')
    if section.project_id != project_id:
        raise HTTPException(status_code=404, detail='章节不属于该项目')
    from ..services.ai.provider_factory import create_provider, get_ai_config
    ai_config = get_ai_config()
    if not ai_config or not ai_config.get('api_key'):
        raise HTTPException(status_code=503, detail='未配置 AI 模型，请先在管理页面配置')
    provider = create_provider(ai_config)
    if not provider:
        raise HTTPException(status_code=503, detail='无法创建 AI 提供商')
    from ..services.ai.image_engine import generate_flowchart_arrange
    result = await generate_flowchart_arrange(provider, section_title=section.title or '', section_content=section.content or '', project_overview=section.project_overview or '', scoring_standard=section.scoring_standard or '', industry_profile=section.industry_profile or '')
    return {'need_arrange': result.get('needed', False), 'reason': result.get('reason', '')}

@router.post('/{section_id}/generate-gantt')
async def generate_gantt(section_id: int, project_id: int, current_user: Annotated[Any, Depends(get_current_user)]=None):
    """AI 生成甘特图（Mermaid）"""
    if current_user:
        await require_project_permission(project_id, current_user)
    section = get_section(section_id)
    if not section:
        raise HTTPException(status_code=404, detail='章节不存在')
    if section.project_id != project_id:
        raise HTTPException(status_code=404, detail='章节不属于该项目')
    from ..services.ai.provider_factory import create_provider, get_ai_config
    ai_config = get_ai_config()
    if not ai_config or not ai_config.get('api_key'):
        raise HTTPException(status_code=503, detail='未配置 AI 模型，请先在管理页面配置')
    provider = create_provider(ai_config)
    if not provider:
        raise HTTPException(status_code=503, detail='无法创建 AI 提供商')
    summary = getattr(section, 'summary', '') or ''
    scoring_standard = getattr(section, 'scoring_standard', '') or ''
    industry_profile = getattr(section, 'industry_profile', '') or ''
    from ..services.ai.image_engine import generate_mermaid_gantt, validate_mermaid
    mermaid_code = await generate_mermaid_gantt(provider, section_title=section.title, section_content=section.content or '', scoring_standard=scoring_standard, industry_profile=industry_profile, summary=summary)
    valid, _ = validate_mermaid(mermaid_code)
    return {'mermaid_code': mermaid_code, 'valid': valid}

@router.post('/{section_id}/arrange-architecture')
async def arrange_architecture(section_id: int, project_id: int, current_user: Annotated[Any, Depends(get_current_user)]=None):
    """AI 编排判断：是否需要架构图"""
    if current_user:
        await require_project_permission(project_id, current_user)
    section = get_section(section_id)
    if not section:
        raise HTTPException(status_code=404, detail='章节不存在')
    if section.project_id != project_id:
        raise HTTPException(status_code=404, detail='章节不属于该项目')
    from ..services.ai.provider_factory import create_provider, get_ai_config
    ai_config = get_ai_config()
    if not ai_config or not ai_config.get('api_key'):
        raise HTTPException(status_code=503, detail='未配置 AI 模型，请先在管理页面配置')
    provider = create_provider(ai_config)
    if not provider:
        raise HTTPException(status_code=503, detail='无法创建 AI 提供商')
    from ..services.ai.image_engine import generate_architecture_arrange
    result = await generate_architecture_arrange(provider, section_title=section.title or '', section_content=section.content or '', project_overview=getattr(section, 'project_overview', '') or '', scoring_standard=getattr(section, 'scoring_standard', '') or '', industry_profile=getattr(section, 'industry_profile', '') or '')
    return {'need_arrange': result.get('needed', False), 'reason': result.get('reason', '')}

@router.post('/{section_id}/generate-architecture')
async def generate_architecture(section_id: int, project_id: int, current_user: Annotated[Any, Depends(get_current_user)]=None):
    """AI 生成架构图"""
    if current_user:
        await require_project_permission(project_id, current_user)
    section = get_section(section_id)
    if not section:
        raise HTTPException(status_code=404, detail='章节不存在')
    if section.project_id != project_id:
        raise HTTPException(status_code=404, detail='章节不属于该项目')
    from ..services.ai.provider_factory import create_provider, get_ai_config
    ai_config = get_ai_config()
    if not ai_config or not ai_config.get('api_key'):
        raise HTTPException(status_code=503, detail='未配置 AI 模型，请先在管理页面配置')
    provider = create_provider(ai_config)
    if not provider:
        raise HTTPException(status_code=503, detail='无法创建 AI 提供商')
    from ..services.ai.image_engine import generate_architecture_arrange, generate_architecture_tree
    arrange_result = await generate_architecture_arrange(provider, section_title=section.title or '', section_content=section.content or '', project_overview=getattr(section, 'project_overview', '') or '', scoring_standard=getattr(section, 'scoring_standard', '') or '', industry_profile=getattr(section, 'industry_profile', '') or '')
    tree = await generate_architecture_tree(provider, section_title=section.title or '', section_content=section.content or '', project_overview=section.project_overview or '', arrange_result=arrange_result)
    return {'tree': tree}

@router.post('/{section_id}/arrange-labor')
async def arrange_labor(section_id: int, project_id: int, current_user: Annotated[Any, Depends(get_current_user)]=None):
    """AI 编排判断：是否需要劳动力图"""
    if current_user:
        await require_project_permission(project_id, current_user)
    section = get_section(section_id)
    if not section:
        raise HTTPException(status_code=404, detail='章节不存在')
    if section.project_id != project_id:
        raise HTTPException(status_code=404, detail='章节不属于该项目')
    from ..services.ai.provider_factory import create_provider, get_ai_config
    ai_config = get_ai_config()
    if not ai_config or not ai_config.get('api_key'):
        raise HTTPException(status_code=503, detail='未配置 AI 模型，请先在管理页面配置')
    provider = create_provider(ai_config)
    if not provider:
        raise HTTPException(status_code=503, detail='无法创建 AI 提供商')
    from ..services.ai.image_engine import generate_labor_arrange
    result = await generate_labor_arrange(provider, section_title=section.title or '', section_content=section.content or '', project_overview=getattr(section, 'project_overview', '') or '', scoring_standard=getattr(section, 'scoring_standard', '') or '', industry_profile=getattr(section, 'industry_profile', '') or '')
    return {'need_arrange': result.get('needed', False), 'reason': result.get('reason', '')}

@router.post('/{section_id}/generate-labor')
async def generate_labor(section_id: int, project_id: int, writing_length: str='standard', current_user: Annotated[Any, Depends(get_current_user)]=None):
    """AI 生成劳动力图数据"""
    if current_user:
        await require_project_permission(project_id, current_user)
    section = get_section(section_id)
    if not section:
        raise HTTPException(status_code=404, detail='章节不存在')
    if section.project_id != project_id:
        raise HTTPException(status_code=404, detail='章节不属于该项目')
    from ..services.ai.provider_factory import create_provider, get_ai_config
    ai_config = get_ai_config()
    if not ai_config or not ai_config.get('api_key'):
        raise HTTPException(status_code=503, detail='未配置 AI 模型，请先在管理页面配置')
    provider = create_provider(ai_config)
    if not provider:
        raise HTTPException(status_code=503, detail='无法创建 AI 提供商')
    from ..services.ai.image_engine import generate_labor_arrange, generate_labor_data
    arrange_result = await generate_labor_arrange(provider, section_title=section.title or '', section_content=section.content or '', project_overview=getattr(section, 'project_overview', '') or '', scoring_standard=getattr(section, 'scoring_standard', '') or '', industry_profile=getattr(section, 'industry_profile', '') or '')
    labor_data = await generate_labor_data(provider, section_title=section.title or '', section_content=section.content or '', project_overview=getattr(section, 'project_overview', '') or '', arrange_result=arrange_result, writing_length=writing_length, industry_profile=getattr(section, 'industry_profile', '') or '')
    return {'labor_data': labor_data, 'valid': True}

@router.post('/{section_id}/arrange-comparison')
async def arrange_comparison(section_id: int, project_id: int, current_user: Annotated[Any, Depends(get_current_user)]=None):
    """AI 编排判断：是否需要对比图"""
    if current_user:
        await require_project_permission(project_id, current_user)
    section = get_section(section_id)
    if not section:
        raise HTTPException(status_code=404, detail='章节不存在')
    if section.project_id != project_id:
        raise HTTPException(status_code=404, detail='章节不属于该项目')
    from ..services.ai.provider_factory import create_provider, get_ai_config
    ai_config = get_ai_config()
    if not ai_config or not ai_config.get('api_key'):
        raise HTTPException(status_code=503, detail='未配置 AI 模型，请先在管理页面配置')
    provider = create_provider(ai_config)
    if not provider:
        raise HTTPException(status_code=503, detail='无法创建 AI 提供商')
    from ..services.ai.image_engine import generate_comparison_arrange
    result = await generate_comparison_arrange(provider, section_title=section.title or '', section_content=section.content or '', project_overview=getattr(section, 'project_overview', '') or '', scoring_standard=getattr(section, 'scoring_standard', '') or '', industry_profile=getattr(section, 'industry_profile', '') or '')
    return {'need_arrange': result.get('needed', False), 'reason': result.get('reason', '')}

@router.post('/{section_id}/generate-comparison')
async def generate_comparison(section_id: int, project_id: int, current_user: Annotated[Any, Depends(get_current_user)]=None):
    """AI 生成对比图（Mermaid Pie/xychart）"""
    if current_user:
        await require_project_permission(project_id, current_user)
    section = get_section(section_id)
    if not section:
        raise HTTPException(status_code=404, detail='章节不存在')
    if section.project_id != project_id:
        raise HTTPException(status_code=404, detail='章节不属于该项目')
    from ..services.ai.provider_factory import create_provider, get_ai_config
    ai_config = get_ai_config()
    if not ai_config or not ai_config.get('api_key'):
        raise HTTPException(status_code=503, detail='未配置 AI 模型，请先在管理页面配置')
    provider = create_provider(ai_config)
    if not provider:
        raise HTTPException(status_code=503, detail='无法创建 AI 提供商')
    from ..services.ai.image_engine import generate_comparison_arrange, generate_comparison_mermaid, validate_mermaid
    arrange_result = await generate_comparison_arrange(provider, section_title=section.title or '', section_content=section.content or '', project_overview=getattr(section, 'project_overview', '') or '', scoring_standard=getattr(section, 'scoring_standard', '') or '', industry_profile=getattr(section, 'industry_profile', '') or '')
    mermaid_code = await generate_comparison_mermaid(provider, section_title=section.title or '', section_content=section.content or '', project_overview=section.project_overview or '', arrange_result=arrange_result)
    valid, _ = validate_mermaid(mermaid_code)
    return {'mermaid_code': mermaid_code, 'valid': valid}
