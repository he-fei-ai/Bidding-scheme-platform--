"""标书查重路由

提供 4 个 API 端点：
- POST /api/duplicate/start                启动查重任务
- GET  /api/duplicate/status/{task_id}     查询进度
- GET  /api/duplicate/result/{task_id}     获取完整结果
- GET  /api/duplicate/export-report/{task_id}  导出报告
"""
import asyncio
import functools
import logging
from pathlib import Path
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from ..config import is_downloadable_path
from ..models import DuplicateStartRequest
from ..security.auth import get_current_user
from ..services import duplicate
from ..services import task_lock as _task_lock
from .project_access import enforce_project_access, enforce_task_access
router = APIRouter(prefix='/duplicate', tags=['duplicate'], dependencies=[Depends(get_current_user)])
logger = logging.getLogger(__name__)

#: 标书查重任务类型（登记在 task_lock.TASK_GROUPS 的 `duplicate-check` 组）
_DUP_TASK_TYPE = 'duplicate-analysis'

#: 报告导出格式 → (扩展名, MIME)
_REPORT_FORMATS: dict[str, tuple[str, str]] = {
    'docx': ('.docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
    'xlsx': ('.xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
}


async def _acquire_dup_lock(project_id: int) -> tuple[str, str]:
    """抢「标书查重」组锁。冲突时抛 HTTPException(409)。

    返回 `(lock_key, owner_id)`，供后台任务在 finally 里释放。
    """
    owner_id = _task_lock.make_owner_id(_DUP_TASK_TYPE, project_id)
    try:
        record = await asyncio.to_thread(
            functools.partial(
                _task_lock.acquire,
                _DUP_TASK_TYPE,
                project_id=project_id,
                owner_id=owner_id,
                note=f'标书查重 project={project_id}',
            )
        )
    except _task_lock.TaskLockConflict as e:
        logger.warning('[标书查重] 组锁互斥拦截 project=%s: %s', project_id, e)
        raise HTTPException(status_code=409, detail=str(e)) from None
    return record.get('lock_key', ''), owner_id


async def _release_dup_lock(lock_key: str, owner_id: str, *, why: str) -> None:
    """释放组锁。失败只告警 —— TTL（30 分钟）兜底。"""
    if not lock_key or not owner_id:
        return
    try:
        await asyncio.to_thread(_task_lock.release, lock_key, owner_id)
    except Exception as e:
        logger.warning('[标书查重] 释放组锁失败（TTL 兜底）%s key=%s: %s', why, lock_key, e)


@router.post('/start')
async def start_check(req: DuplicateStartRequest, bg: BackgroundTasks, user=Depends(get_current_user)):
    """启动查重任务"""
    if len(req.file_ids) < 2:
        raise HTTPException(status_code=400, detail='至少需要 2 个文件')
    # ENHANCE 2026-09-29（P1 IDOR 收口）：挂项目任务先验属权，与 sections 同口径
    if req.project_id:
        await enforce_project_access(req.project_id, user)

    # BUG-FIX 2026-09-15（高）：先抢组锁再建任务。
    lock_key, owner_id = await _acquire_dup_lock(req.project_id)
    try:
        # P1：任务登记归属项目，列表按项目隔离
        task_id, is_cached = duplicate.create_task(req.file_ids, req.tender_file_id, req.project_id)
    except Exception:
        await _release_dup_lock(lock_key, owner_id, why='建任务失败回滚')
        raise
    if is_cached:
        # 命中缓存 → 不会起后台任务，锁没有收尾者，必须当场放掉
        await _release_dup_lock(lock_key, owner_id, why='命中缓存')
        return {'task_id': task_id, 'status': 'completed', 'cached': True}
    bg.add_task(_run_safely, task_id, lock_key, owner_id)
    return {'task_id': task_id, 'status': 'pending', 'cached': False}


async def _run_safely(task_id: int, lock_key: str='', owner_id: str=''):
    """安全运行查重任务；**无论如何都释放组锁**（BackgroundTasks 模式下
    请求早已返回，不能在请求处理函数里释放，否则互斥形同虚设）。"""
    try:
        await duplicate.run_duplicate_check(task_id)
    except Exception as e:
        import traceback
        traceback.print_exc()
        duplicate.update_task_status(task_id, 'failed', error=str(e))
    finally:
        await _release_dup_lock(lock_key, owner_id, why=f'任务收尾 task_id={task_id}')

@router.get('/status/{task_id}')
async def get_status(task_id: int, user=Depends(get_current_user)):
    # ENHANCE 2026-09-29（P1 IDOR 收口）：任务归属项目鉴权，下同
    await enforce_task_access('duplicate_checks', task_id, user)
    # PERF（2026-10-06）：`duplicate.get_task` 内部 `get_db()` 取**写锁**连接，
    # 本端点被前端每 2s 轮询。此前在 async def 里同步直调 → 抢全局写锁并阻塞
    # 事件循环（与 rejection.status 同病，当时漏改了 duplicate 这一侧）。
    task = await asyncio.to_thread(duplicate.get_task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail='任务不存在')
    return {'id': task['id'], 'status': task['status'], 'progress': task['progress'], 'progress_message': task['progress_message'], 'error': task.get('error', '')}

@router.get('/result/{task_id}')
async def get_result(task_id: int, user=Depends(get_current_user)):
    await enforce_task_access('duplicate_checks', task_id, user)
    task = await asyncio.to_thread(duplicate.get_task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail='任务不存在')
    return task

@router.get('/export-report/{task_id}')
async def export_report(task_id: int, fmt: str=Query('docx', alias='format'), user=Depends(get_current_user)):
    """导出查重报告。

    ENHANCE 2026-09-15：新增 `format=xlsx`，导出「查重概览/元数据/目录重复/
    文件相似度/重复句子/重复图片」六 Sheet 工作簿（移植自易标
    `checkResultExportService.cjs`），相似度列带百分比格式与自动筛选。

    BUG-FIX 2026-09-15：报告目录不在 `data/projects` 之下，原守卫导致该端点
    **恒定 403**，前端从未成功下载过报告；现统一走 `config.is_downloadable_path()`。
    """
    fmt = (fmt or 'docx').strip().lower()
    if fmt not in _REPORT_FORMATS:
        raise HTTPException(status_code=400, detail='不支持的导出格式，仅支持 docx / xlsx')
    suffix, media_type = _REPORT_FORMATS[fmt]
    # ENHANCE 2026-09-29（P1 IDOR 收口）：报告含标书正文，生成前先验归属
    await enforce_task_access('duplicate_checks', task_id, user)
    try:
        builder = duplicate.export_report if fmt == 'docx' else duplicate.export_report_excel
        file_path = await asyncio.to_thread(builder, task_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ImportError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    p = Path(file_path).resolve()
    if not p.exists():
        raise HTTPException(status_code=500, detail='报告生成失败')
    if not is_downloadable_path(p):
        raise HTTPException(status_code=403, detail='非法文件路径访问')
    if p.suffix.lower() != suffix:
        raise HTTPException(status_code=400, detail='文件类型不支持')
    return FileResponse(path=str(p), filename=p.name, media_type=media_type)

@router.get('/tasks')
async def list_tasks(limit: int=20, project_id: int | None = Query(None, description='仅列出该项目的查重任务'), user=Depends(get_current_user)):
    """列出查重任务（P1：传 project_id 时按项目隔离，不传保留全局视图兼容旧前端）"""
    # ENHANCE 2026-09-29（P1 IDOR 收口）：按项目过滤时先验该项目访问权
    if project_id is not None:
        await enforce_project_access(project_id, user)
    # PERF（2026-10-06）：纯读列表端点。此前在 async def 里同步直调
    # `duplicate.list_tasks()`（内部 `get_db()` 取**写锁**连接），既抢全局写锁
    # 又阻塞事件循环；与本模块其余端点（均已 to_thread）口径不一致。
    # 现移入线程池并改走只读连接。
    return await asyncio.to_thread(duplicate.list_tasks, limit, project_id=project_id, readonly=True)
