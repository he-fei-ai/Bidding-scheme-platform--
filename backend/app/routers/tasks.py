"""统一任务状态与恢复 API

提供跨页面/跨会话的任务恢复能力：
1. 统一查询项目所有后台任务状态（目录生成、正文生成、文件解析）
2. 自动检测异常中断并恢复
3. 支持前端页面加载时自动恢复任务

功能：
- GET /projects/{id}/tasks/status — 统一任务状态查询
- GET /projects/{id}/tasks/v51/recovery — V5.1 目录生成恢复
- GET /projects/{id}/tasks/content/recovery — 正文生成恢复
- GET /projects/{id}/tasks/files/status — 文件解析状态
- GET /projects/{id}/tasks/registry — TaskRegistry 任务状态（新增：后台运行任务查询）
"""
from datetime import datetime
import asyncio
import logging
from typing import Annotated, Any
from fastapi import APIRouter, Depends, HTTPException, Query
from ..db import get_db
from ..security.auth import get_current_user
from ..services import task_snapshot
from ..services.generation_manager import generation_manager
from ..services.task_registry import task_registry
from ..services.v51_state import get_v51_state, load_v51_state_from_db
logger = logging.getLogger(__name__)
router = APIRouter(prefix='/projects/{project_id}/tasks', tags=['tasks'])

_projects_has_created_by = None
_projects_created_by_warned = False

# ENHANCE 2026-09-15（Task 9）：中断快照 → 前端 recovery_options 的 type 取值。
# 与「真能续跑」的 v51_outline / content_generation 区分开，前端据此改按钮文案
# （中断没有检查点，只能重新生成；写成「恢复」会误导用户以为进度还在）。
_INTERRUPTED_OPTION_TYPES = {
    'outline_v51': 'outline_interrupted',
    'content_generation': 'content_interrupted',
}

def _load_project_owner_row_tasks(project_id: int):
    """PERF（2026-10-05）：权限门同步 DB 读取段，供 `asyncio.to_thread` 派发。

    与 routers/sections.py 同口径：只读走 `get_db(readonly=True)` 跳写锁，
    避免在事件循环线程内做同步 sqlite I/O（连接重试 sleep/写锁争用会卡死 loop）。
    """
    global _projects_has_created_by
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
    return dict(row) if row else None


async def _ensure_project_access(project_id: int, current_user: Any):
    """验证用户是否有权限访问指定项目（与 sections.require_project_permission 逻辑一致）"""
    global _projects_created_by_warned
    row = await asyncio.to_thread(_load_project_owner_row_tasks, project_id)
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
        row_dict = dict(row)
        if uid and row_dict.get('created_by') is not None and (row_dict['created_by'] != uid):
            raise HTTPException(status_code=403, detail='无权访问该项目')


@router.get('/status')
async def get_all_tasks_status(project_id: int, current_user: Annotated[Any, Depends(get_current_user)]=None):
    """统一查询项目所有后台任务状态

    返回当前项目的所有后台任务状态，包括：
    - v51_outline: V5.1 目录生成任务
    - content_generation: 正文生成任务
    - file_parsing: 文件解析任务

    前端页面加载时调用此接口，自动检测是否有可恢复的任务。
    """
    await _ensure_project_access(project_id, current_user)
    result = {'project_id': project_id, 'has_pending_tasks': False, 'tasks': {}, 'recovery_available': False, 'recovery_options': []}
    v51_state = get_v51_state(project_id)
    if v51_state is None:
        try:
            v51_state = load_v51_state_from_db(project_id)
        except Exception as _e:
            logger.debug('[silent-except] tasks.py: line 64 - %s', _e)
    if v51_state and (not v51_state.cancelled):
        result['tasks']['v51_outline'] = {'status': 'active', 'phase': v51_state.current_phase, 'has_edited_outline': v51_state.edited_outline is not None, 'cancelled': v51_state.cancelled}
        result['has_pending_tasks'] = True
        result['recovery_options'].append({'type': 'v51_outline', 'label': 'V5.1 目录生成', 'phase': v51_state.current_phase, 'description': f'目录生成进行到 {v51_state.current_phase} 阶段，可继续生成'})
    else:
        try:
            # BUG-FIX 2026-09-16（高）：这是被前端轮询的高频**纯读**接口，
            # 原来用非 readonly 的 get_db() 会去抢全局写锁 _db_write_lock
            # （8s 超时）。正文生成高峰写锁被持久化线程反复占有时，
            # 轮询会在事件循环线程上阻塞最长 8s，冻结全部并发请求。
            # 纯 SELECT 一律走 readonly 连接，不参与写锁竞争。
            # PERF（2026-10-06）：readonly 解决了写锁争用，但 DB 往返本身仍跑在
            # 事件循环里（下方 `_get_file_parsing_status` 早已用 to_thread，本处是
            # 同函数内的漏网之鱼）。现统一为「嵌套同步闭包 + to_thread」，
            # 与同函数内既有写法保持一致。
            def _read_v51_task_row():
                with get_db(readonly=True) as conn:
                    return conn.execute('SELECT project_id, phase, current_phase, cancelled, expired_at\n                       FROM v51_tasks\n                       WHERE project_id = ? AND cancelled = 0', (project_id,)).fetchone()

            row = await asyncio.to_thread(_read_v51_task_row)
            if row:
                expired_at = row['expired_at']
                if expired_at:
                    try:
                        exp_dt = datetime.strptime(expired_at, '%Y-%m-%d %H:%M:%S')
                        if datetime.now() <= exp_dt:
                            result['tasks']['v51_outline'] = {'status': 'recoverable', 'phase': row['current_phase'], 'cancelled': bool(row['cancelled'])}
                            result['recovery_available'] = True
                            result['recovery_options'].append({'type': 'v51_outline', 'label': 'V5.1 目录生成', 'phase': row['current_phase'], 'description': f"目录生成在 {row['current_phase']} 阶段中断，可恢复"})
                    except ValueError:
                        pass
        except Exception as e:
            logger.warning('查询 V5.1 任务状态失败: %s', e)
    try:
        content_progress = await generation_manager.get_project_progress(project_id)
        if content_progress.get('has_active_task'):
            result['tasks']['content_generation'] = {'status': 'active', 'task_status': content_progress.get('task_status'), 'total_sections': content_progress.get('total_sections'), 'completed_count': content_progress.get('completed_count'), 'failed_count': content_progress.get('failed_count')}
            result['has_pending_tasks'] = True
            result['recovery_options'].append({'type': 'content_generation', 'label': '正文生成', 'task_status': content_progress.get('task_status'), 'description': f"正文生成进度: {content_progress.get('completed_count', 0)}/{content_progress.get('total_sections', 0)}"})
        elif content_progress.get('task_status') == 'paused':
            result['tasks']['content_generation'] = {'status': 'paused', 'total_sections': content_progress.get('total_sections'), 'completed_count': content_progress.get('completed_count')}
            result['recovery_available'] = True
            result['recovery_options'].append({'type': 'content_generation', 'label': '正文生成', 'task_status': 'paused', 'description': f"正文生成已暂停 ({content_progress.get('completed_count', 0)}/{content_progress.get('total_sections', 0)})"})
        elif content_progress.get('auto_corrected'):
            result['tasks']['content_generation'] = {'status': 'corrected', 'message': '检测到异常中断的生成任务，已自动标记为失败'}
    except Exception as e:
        logger.warning('查询正文生成状态失败: %s', e)
    try:
        # BUG-FIX 2026-09-16（中）：同步 DB 查询移出事件循环线程
        file_status = await asyncio.to_thread(_get_file_parsing_status, project_id)
        if file_status:
            result['tasks']['file_parsing'] = file_status
            if file_status.get('status') in ('processing', 'queued'):
                result['has_pending_tasks'] = True
    except Exception as e:
        logger.warning('查询文件解析状态失败: %s', e)
    try:
        registry_tasks = task_registry.get_project_tasks(project_id)
        for task_id, task_info in registry_tasks.items():
            if task_info.status in ('pending', 'running', 'paused'):
                task_key = f'registry_{task_info.task_type}'
                result['tasks'][task_key] = {'status': task_info.status, 'progress': task_info.progress, 'message': task_info.message, 'task_id': task_info.task_id}
                result['has_pending_tasks'] = True
                result['recovery_options'].append({'type': 'task_registry', 'task_type': task_info.task_type, 'task_id': task_info.task_id, 'label': f'{task_info.task_type} 任务', 'progress': task_info.progress, 'description': task_info.message or '任务进行中'})
    except Exception as e:
        logger.warning('查询 TaskRegistry 状态失败: %s', e)
    # ENHANCE 2026-09-15（Task 9）：后台任务快照的中断对账结果。
    # 进程被强杀时 `finally` 不执行，目录生成这类长任务不会有任何收尾痕迹；
    # 靠 background_tasks 的心跳判定「这个任务其实已经死了」，在这里如实告知用户。
    # 注意：这里是**纯读**（list_interruptions 不写库），不能因为用户刷页面就改库。
    try:
        for _item in task_snapshot.list_interruptions(project_id):
            _ttype = _item.get('task_type') or 'unknown'
            _summary = task_snapshot.interruption_summary(_item)
            result['tasks'][f'interrupted_{_ttype}'] = {
                'status': 'interrupted',
                'phase': _item.get('phase'),
                'progress': _item.get('progress'),
                'task_type': _ttype,
                'heartbeat_age_seconds': _item.get('heartbeat_age_seconds'),
                'message': _summary,
            }
            result['recovery_options'].append({
                'type': _INTERRUPTED_OPTION_TYPES.get(_ttype, f'{_ttype}_interrupted'),
                'label': _item.get('task_type_label') or _ttype,
                'phase': _item.get('phase'),
                'progress': _item.get('progress'),
                'description': _summary,
            })
            result['recovery_available'] = True
    except Exception as e:
        logger.warning('查询后台任务中断快照失败: %s', e)
    result['has_pending_tasks'] = len(result.get('recovery_options', [])) > 0 or result.get('has_pending_tasks', False)
    return result

@router.get('/v51/recovery')
async def get_v51_recovery_info(project_id: int, current_user: Annotated[Any, Depends(get_current_user)]=None):
    """获取 V5.1 目录生成恢复信息

    返回可恢复的 V5.1 任务状态和上下文，前端可据此显示恢复选项。
    """
    await _ensure_project_access(project_id, current_user)
    try:
        state = load_v51_state_from_db(project_id)
        if not state:
            return {'recoverable': False, 'message': '没有可恢复的 V5.1 任务'}
        return {'recoverable': True, 'project_id': project_id, 'phase': state.current_phase, 'cancelled': state.cancelled, 'has_edited_outline': state.edited_outline is not None, 'level1_count': len(state.level1_outline or []), 'summary': state.summary[:200] if state.summary else '', 'message': f'V5.1 目录生成在 {state.current_phase} 阶段，可继续'}
    except Exception as e:
        logger.error('获取 V5.1 恢复信息失败: %s', e)
        return {'recoverable': False, 'message': f'查询失败: {e}'}

@router.get('/content/recovery')
async def get_content_recovery_info(project_id: int, current_user: Annotated[Any, Depends(get_current_user)]=None):
    """获取正文生成恢复信息"""
    await _ensure_project_access(project_id, current_user)
    try:
        progress = await generation_manager.get_project_progress(project_id)
        if not progress.get('has_active_task') and progress.get('task_status') != 'paused':
            return {'recoverable': False, 'message': '没有可恢复的正文生成任务'}
        pending_sections = await generation_manager.get_pending_sections(project_id)
        return {'recoverable': True, 'project_id': project_id, 'task_status': progress.get('task_status', 'unknown'), 'total_sections': progress.get('total_sections', 0), 'completed_count': progress.get('completed_count', 0), 'failed_count': progress.get('failed_count', 0), 'pending_count': len(pending_sections), 'pending_sections': pending_sections[:20], 'message': f"正文生成进度: {progress.get('completed_count', 0)}/{progress.get('total_sections', 0)}，待恢复 {len(pending_sections)} 个章节"}
    except Exception as e:
        logger.error('获取正文生成恢复信息失败: %s', e)
        return {'recoverable': False, 'message': f'查询失败: {e}'}

@router.get('/content/pending-sections')
async def get_content_pending_sections(project_id: int, offset: int=Query(0, ge=0), limit: int=Query(50, ge=1, le=200), current_user: Annotated[Any, Depends(get_current_user)]=None):
    """获取正文生成中待恢复的章节列表（分页）"""
    await _ensure_project_access(project_id, current_user)
    try:
        pending = await generation_manager.get_pending_sections(project_id)
        page = pending[offset:offset + limit]
        return {'total': len(pending), 'offset': offset, 'limit': limit, 'items': page}
    except Exception as e:
        logger.error('获取待恢复章节列表失败: %s', e)
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.get('/locks')
async def get_task_locks(project_id: int, current_user: Annotated[Any, Depends(get_current_user)]=None):
    """查询任务组互斥锁状态（诊断用）

    ENHANCE 2026-09-15：任务组锁下沉到 sqlite 后，必须有地方能看到
    「谁占着哪个组的锁、还有多久过期」。否则用户只会收到
    「当前技术方案正在执行“正文生成”」这种提示，却无法自查原因，
    也无法判断是「真的有任务在跑」还是「上次崩溃留下的僵尸锁」。
    """
    await _ensure_project_access(project_id, current_user)
    from datetime import datetime as _dt

    from ..services import task_lock
    now = _dt.now()
    items = []
    for lk in task_lock.list_locks(active_only=True):
        if lk.get('project_id') != project_id:
            continue
        try:
            remaining = int((_dt.strptime(lk['expires_at'], '%Y-%m-%d %H:%M:%S') - now).total_seconds())
        except Exception:
            remaining = None
        items.append({
            'lock_key': lk.get('lock_key'),
            'group_name': lk.get('group_name'),
            'group_label': task_lock.GROUP_LABELS.get(lk.get('group_name'), lk.get('group_name')),
            'task_type': lk.get('task_type'),
            'task_label': task_lock.TASK_LABELS.get(lk.get('task_type'), lk.get('task_type')),
            'owner_id': lk.get('owner_id'),
            'acquired_at': lk.get('acquired_at'),
            'heartbeat_at': lk.get('heartbeat_at'),
            'expires_at': lk.get('expires_at'),
            'remaining_seconds': remaining,
            'stale': remaining is not None and remaining <= 0,
            'note': lk.get('note', ''),
        })
    return {'project_id': project_id, 'total': len(items), 'locks': items}

@router.post('/locks/reclaim')
async def reclaim_task_locks(project_id: int, current_user: Annotated[Any, Depends(get_current_user)]=None):
    """回收已超时的任务组锁（运维兜底手段）

    ENHANCE 2026-09-15：正常路径下超时锁会在下次 acquire / 应用启动时自动回收，
    这里提供手动入口，用于「确认持有者进程已死、但不想等 TTL 也不想重启服务」的场景。
    只回收 expires_at 已过的锁，不会动正在心跳的锁。
    """
    await _ensure_project_access(project_id, current_user)
    from ..services import task_lock
    reclaimed = task_lock.reclaim_stale()
    return {'status': 'ok', 'reclaimed': reclaimed, 'message': f'已回收 {reclaimed} 个超时锁'}

@router.get('/interrupted')
async def get_interrupted_tasks(project_id: int, current_user: Annotated[Any, Depends(get_current_user)]=None):
    """查询被判定为「中断」的后台任务快照（纯读，不写库）

    ENHANCE 2026-09-15（Task 9）：对标易标 `recoverInterruptedOutlineGenerationTask`
    —— 把「上次没跑完的任务」如实呈现给用户，而不是让它永远挂在「运行中」。

    判定依据是**心跳超时**（默认 300 秒无心跳），不是「本进程内存里没有」，
    因为本软件是 FastAPI + Celery 多进程部署，内存状态跨进程不可信。
    """
    await _ensure_project_access(project_id, current_user)
    items = task_snapshot.list_interruptions(project_id)
    return {
        'project_id': project_id,
        'total': len(items),
        'items': [
            {
                'task_id': it.get('task_id'),
                'task_type': it.get('task_type'),
                'task_type_label': it.get('task_type_label'),
                'phase': it.get('phase'),
                'progress': it.get('progress'),
                'message': it.get('message'),
                'heartbeat_age_seconds': it.get('heartbeat_age_seconds'),
                'started_at': it.get('started_at'),
                'interrupted_reason': it.get('interrupted_reason'),
                'summary': task_snapshot.interruption_summary(it),
            }
            for it in items
        ],
    }

@router.post('/interrupted/ack')
async def ack_interrupted_tasks(project_id: int, task_type: str | None=Query(None), current_user: Annotated[Any, Depends(get_current_user)]=None):
    """确认已知晓中断提示（避免横幅一直挂着）

    ENHANCE 2026-09-15（Task 9）：用户看到提示后点掉/重新发起时调用。
    只标记 `acknowledged=1`，不改状态、不删行 —— 中断轨迹要留作审计。
    """
    await _ensure_project_access(project_id, current_user)
    count = task_snapshot.acknowledge(project_id, task_type=task_type)
    return {'status': 'ok', 'acknowledged': count, 'message': f'已确认 {count} 条中断提示'}

@router.get('/files/status')
async def get_file_parsing_status(project_id: int, current_user: Annotated[Any, Depends(get_current_user)]=None):
    """获取文件解析状态"""
    await _ensure_project_access(project_id, current_user)
    return _get_file_parsing_status(project_id) or {'status': 'idle', 'message': '没有文件解析任务'}

def _get_file_parsing_status(project_id: int) -> dict | None:
    """获取文件解析任务状态（项目级聚合）

    ENHANCE 2026-10-04（P3）：旧实现 `ORDER BY started_at DESC LIMIT 1` 只返回最近
    单文件的一行，批量上传时无法反映整体进度（多个并发任务同秒写入、started_at
    精度到秒，取哪一条也不确定）。现按项目聚合：
      - processing：当前仍在解析的任务数（同项目并发上传的主要信号）；
      - recent_* ：近 5 分钟窗口内的成功/失败计数（避免把历史所有上传累加）；
      - latest：保留旧结构（最近一条任务行），向后兼容已有调用方。
    """
    try:
        # 纯读查询走 readonly 连接，避免抢全局写锁
        with get_db(readonly=True) as conn:
            agg = conn.execute(
                "SELECT "
                "SUM(CASE WHEN status = 'processing' THEN 1 ELSE 0 END) AS processing, "
                "SUM(CASE WHEN status = 'completed' AND updated_at >= datetime('now', 'localtime', '-5 minutes') THEN 1 ELSE 0 END) AS recent_completed, "
                "SUM(CASE WHEN status = 'failed' AND updated_at >= datetime('now', 'localtime', '-5 minutes') THEN 1 ELSE 0 END) AS recent_failed "
                "FROM file_parse_tasks WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            total = conn.execute(
                "SELECT COUNT(*) AS c FROM file_parse_tasks WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            row = conn.execute(
                'SELECT status, filename, file_type, progress, total_files,\n                          completed_files, error_message, started_at, updated_at\n                   FROM file_parse_tasks\n                   WHERE project_id = ?\n                   ORDER BY id DESC LIMIT 1',
                (project_id,),
            ).fetchone()

        total_count = (total["c"] if total else 0) or 0
        if not total_count:
            return None

        processing = (agg["processing"] if agg else 0) or 0
        recent_completed = (agg["recent_completed"] if agg else 0) or 0
        recent_failed = (agg["recent_failed"] if agg else 0) or 0
        if processing:
            overall = 'processing'
        elif recent_failed:
            overall = 'completed_with_errors' if recent_completed else 'failed'
        else:
            overall = 'completed'
        return {
            'status': overall,
            'processing': processing,
            'recent_completed': recent_completed,
            'recent_failed': recent_failed,
            'total_tasks': total_count,
            'latest': dict(row) if row else None,
        }
    except Exception as e:
        # BUG-FIX 2026-09-16（低）：裸 except 把 DB 故障伪装成「没有解析任务」，
        # 前端误显示 idle；至少留痕日志。
        logger.warning('查询文件解析任务状态失败: %s', e)
        return None
