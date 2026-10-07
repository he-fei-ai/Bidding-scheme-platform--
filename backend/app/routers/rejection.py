"""废标检查路由

提供 6 个 API 端点：
- POST /api/rejection/start                启动检查任务
- GET  /api/rejection/status/{task_id}     查询任务状态
- GET  /api/rejection/result/{task_id}     获取完整结果
- POST /api/rejection/mark-false-positive  标记误报
- POST /api/rejection/retry-dimension      重试某一维度
- GET  /api/rejection/export-report/{task_id}  导出报告
"""
import asyncio
import functools
import logging
from pathlib import Path
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from ..config import is_downloadable_path
from ..models import ApplySpellingFixRequest, BatchMarkFalsePositiveRequest, BatchTaskDeleteRequest, ExtractItemsRequest, MarkFalsePositiveRequest, RejectionStartRequest, ReportScheduleRequest, RetryDimensionRequest
from ..security.auth import get_current_user
from ..services import rejection
from ..services import task_lock as _task_lock
from ..services.ai.provider_factory import create_provider, get_ai_config
from .project_access import enforce_project_access, enforce_task_access
router = APIRouter(prefix='/rejection', tags=['rejection'])
logger = logging.getLogger(__name__)

#: 废标检查任务类型（登记在 task_lock.TASK_GROUPS 的 `rejection-check` 组）
_CHECK_TASK_TYPE = 'rejection-check-run'

#: 报告导出格式 → (扩展名, MIME)
_REPORT_FORMATS: dict[str, tuple[str, str]] = {
    'docx': ('.docx', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
    'xlsx': ('.xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
}


async def _acquire_check_lock(project_id: int) -> tuple[str, str]:
    """抢「废标项检查」组锁。冲突时抛 HTTPException(409)。

    返回 `(lock_key, owner_id)`，供后台任务在 finally 里释放。
    """
    owner_id = _task_lock.make_owner_id(_CHECK_TASK_TYPE, project_id)
    try:
        record = await asyncio.to_thread(
            functools.partial(
                _task_lock.acquire,
                _CHECK_TASK_TYPE,
                project_id=project_id,
                owner_id=owner_id,
                note=f'废标检查 project={project_id}',
            )
        )
    except _task_lock.TaskLockConflict as e:
        logger.warning('[废标检查] 组锁互斥拦截 project=%s: %s', project_id, e)
        raise HTTPException(status_code=409, detail=str(e)) from None
    return record.get('lock_key', ''), owner_id


async def _release_check_lock(lock_key: str, owner_id: str, *, why: str) -> None:
    """释放组锁。失败只告警 —— TTL（30 分钟）会兜底，不该因为放锁失败
    把用户的操作也搞失败。"""
    if not lock_key or not owner_id:
        return
    try:
        await asyncio.to_thread(_task_lock.release, lock_key, owner_id)
    except Exception as e:
        logger.warning('[废标检查] 释放组锁失败（TTL 兜底）%s key=%s: %s', why, lock_key, e)


async def _guard_schedule_access(schedule_id: int, user) -> None:
    """调度→任务→项目链式反查鉴权；调度不存在抛 404。"""
    owner_task_id = await asyncio.to_thread(rejection.get_schedule_task_id, schedule_id)
    if owner_task_id is None:
        raise HTTPException(status_code=404, detail='调度任务不存在')
    await enforce_task_access('rejection_checks', owner_task_id, user)

@router.post('/start')
async def start_check(req: RejectionStartRequest, bg: BackgroundTasks, user=Depends(get_current_user)):
    """启动废标检查任务"""
    if not req.bid_documents:
        raise HTTPException(status_code=400, detail='请至少上传一份投标文件')
    # ENHANCE 2026-09-29（P1 IDOR 收口）：挂项目的检查会消耗该项目 AI 额度
    # 并读写其正文，须先验项目属权（与 sections 同口径）；纯文本模式无项目，跳过。
    if req.project_id:
        await enforce_project_access(req.project_id, user)
    check_items = [c.model_dump() for c in req.check_items]
    if not check_items and 'rejection' in req.enabled_dimensions and req.tender_text:
        try:
            ai_config = get_ai_config()
            provider = create_provider(ai_config)
            check_items = await rejection.extract_rejection_items(provider, req.tender_text)
        except Exception:
            check_items = rejection._default_rejection_items()
    bid_docs = [b.model_dump() for b in req.bid_documents]

    # BUG-FIX 2026-09-15（高）：先抢组锁再建任务。
    # 顺序不能反 —— 先建任务再抢锁，抢锁失败时已经落了一行 rejection_tasks 脏数据。
    lock_key, owner_id = await _acquire_check_lock(req.project_id)
    try:
        task_id = await asyncio.to_thread(rejection.create_task, project_id=req.project_id, tender_text=req.tender_text, bid_documents=bid_docs, check_items=check_items, enabled_dimensions=req.enabled_dimensions)
    except Exception:
        # 建任务失败就别把锁留着，否则该项目 30 分钟内都发不起检查
        await _release_check_lock(lock_key, owner_id, why='建任务失败回滚')
        raise
    bg.add_task(_run_safely, task_id, lock_key, owner_id)
    return {'task_id': task_id, 'status': 'pending', 'check_items': check_items}

async def _run_safely(task_id: int, lock_key: str='', owner_id: str='', dimension: str | None=None):
    """安全运行检查任务，捕获异常；**无论如何都释放组锁**。

    锁必须在 finally 释放：本函数由 BackgroundTasks 调度，此时 HTTP 响应早已返回，
    在请求处理函数里释放锁等于「任务还没跑锁就没了」，互斥形同虚设。
    """
    try:
        await rejection.run_rejection_check(task_id, dimension=dimension)
    except Exception as e:
        import traceback
        traceback.print_exc()
        await asyncio.to_thread(rejection.update_task_status, task_id, 'failed', error=str(e))
    finally:
        await _release_check_lock(lock_key, owner_id, why=f'任务收尾 task_id={task_id}')

@router.get('/status/{task_id}')
async def get_status(task_id: int, user=Depends(get_current_user)):
    """查询任务状态"""
    # ENHANCE 2026-09-29（P1 IDOR 收口）：任务归属项目鉴权，下同
    await enforce_task_access('rejection_checks', task_id, user)
    # PERF（2026-10-06）：`rejection.get_task` 是同步 DB 读（`get_db()` 写锁连接），
    # 本端点被前端高频轮询。此前在 async def 里同步直调 → 抢全局写锁并阻塞
    # 事件循环。移入线程池（与本模块 run_rejection_check 等重操作口径一致）。
    task = await asyncio.to_thread(rejection.get_task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail='任务不存在')
    return {'id': task['id'], 'status': task['status'], 'progress': task['progress'], 'progress_message': task['progress_message'], 'error': task.get('error', '')}

@router.get('/result/{task_id}')
async def get_result(task_id: int, user=Depends(get_current_user)):
    """获取完整检查结果"""
    await enforce_task_access('rejection_checks', task_id, user)
    task = await asyncio.to_thread(rejection.get_task, task_id)
    if not task:
        raise HTTPException(status_code=404, detail='任务不存在')
    return task

@router.post('/mark-false-positive')
async def mark_false_positive(req: MarkFalsePositiveRequest, task_id: int, user=Depends(get_current_user)):
    """标记/取消标记误报"""
    try:
        await enforce_task_access('rejection_checks', task_id, user)
        fps = await asyncio.to_thread(rejection.mark_false_positive, task_id, req.item_id, req.is_false_positive)
        return {'task_id': task_id, 'false_positives': fps}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

@router.post('/retry-dimension')
async def retry_dimension(req: RetryDimensionRequest, task_id: int, bg: BackgroundTasks, user=Depends(get_current_user)):
    """重试某一维度"""
    await enforce_task_access('rejection_checks', task_id, user)
    task = rejection.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail='任务不存在')
    # BUG-FIX 2026-09-18（P2）：原不校验任务终态——检查还在 running 时也能
    # 发起单维度重试，同一任务两条流水线并发写结果，状态互踩。
    if task['status'] not in ('completed', 'failed'):
        raise HTTPException(
            status_code=409,
            detail=f'任务当前状态为 {task["status"]}，仅检查完成或失败后才可重试单维度',
        )
    if req.dimension not in task['enabled_dimensions']:
        raise HTTPException(status_code=400, detail=f'维度 {req.dimension} 未在当前任务的启用列表中')
    # 重试单维度同样是「跑一轮 AI」，也要参与组互斥（否则能在整轮检查跑着时插进来）
    lock_key, owner_id = await _acquire_check_lock(task['project_id'])
    bg.add_task(_run_safely, task_id, lock_key, owner_id, req.dimension)
    return {'task_id': task_id, 'status': 'running', 'retry_dimension': req.dimension}

@router.get('/export-report/{task_id}')
async def export_report(task_id: int, fmt: str=Query('docx', alias='format'), user=Depends(get_current_user)):
    """导出检查报告。

    ENHANCE 2026-09-15：新增 `format=xlsx`，导出「检查概览/废标项/错别字/逻辑问题」
    四 Sheet 工作簿（移植自易标 `checkResultExportService.cjs`）。
    Word 适合阅读归档，Excel 适合按风险等级筛选复核与整改台账流转。

    BUG-FIX 2026-09-15：报告目录（`data/rejection_reports`）不在 `data/projects`
    之下，原守卫写死只允许 `PROJECTS_DATA_DIR`，导致该端点**恒定 403**；
    现改用 `config.is_downloadable_path()` 统一白名单。
    """
    fmt = (fmt or 'docx').strip().lower()
    if fmt not in _REPORT_FORMATS:
        raise HTTPException(status_code=400, detail='不支持的导出格式，仅支持 docx / xlsx')
    suffix, media_type = _REPORT_FORMATS[fmt]
    # ENHANCE 2026-09-29（P1 IDOR 收口）：报告含项目正文，下载前先验归属
    await enforce_task_access('rejection_checks', task_id, user)
    try:
        # BUG-FIX 2026-09-18（P2）：报告生成是同步重操作（读任务结果 +
        # 渲染 DOCX/XLSX 写盘），同步直调阻塞事件循环；与 duplicate.py
        # 同型端点已修，此处对齐 to_thread。
        if fmt == 'docx':
            file_path = await asyncio.to_thread(rejection.export_report, task_id)
        else:
            file_path = await asyncio.to_thread(rejection.export_report_excel, task_id)
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

@router.post('/apply-spelling')
async def apply_spelling(req: ApplySpellingFixRequest, user=Depends(get_current_user)):
    """一键应用错别字修正（ENHANCE 2026-09-15）。

    参考易标：错别字检查会核验出精确位置，目的就是支持一键替换；
    本平台此前只做到"检测 + 导出"，位置信息白白浪费。
    只对**已通过原文核验**的条目生效，未核验的一律拒绝。
    """
    try:
        # ENHANCE 2026-09-29（P1 IDOR 收口）：本接口会回写项目正文，必须验属权
        await enforce_task_access('rejection_checks', req.task_id, user)
        return await asyncio.to_thread(
            rejection.apply_spelling_fixes,
            req.task_id,
            req.item_ids,
            writeback_project=req.writeback_project,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        logger.error('错别字一键修正失败: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'错别字修正失败: {str(e)[:200]}') from e


@router.get('/tasks')
async def list_tasks(project_id: int | None=None, limit: int=20, user=Depends(get_current_user)):
    """列出任务"""
    try:
        # ENHANCE 2026-09-29（P1 IDOR 收口）：按项目过滤时先验该项目的访问权
        if project_id:
            await enforce_project_access(project_id, user)
        tasks = await asyncio.to_thread(rejection.list_tasks, project_id, limit)
        return tasks
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.get('/project-tasks/{project_id}')
async def get_project_tasks(project_id: int, limit: int=100, user=Depends(get_current_user)):
    """获取项目的废标检查任务列表"""
    try:
        await enforce_project_access(project_id, user)
        tasks = await asyncio.to_thread(rejection.get_tasks_by_project, project_id, limit)
        return {'total': len(tasks), 'items': tasks}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.post('/batch/delete')
async def batch_delete_tasks(req: BatchTaskDeleteRequest, user=Depends(get_current_user)):
    """批量删除任务"""
    try:
        # ENHANCE 2026-09-29（P1 IDOR 收口）：任一任务归属无权项目则整批拒绝，
        # 不做部分删除（语义与 403 一致，避免"删了一半"的中间态）
        for tid in req.task_ids:
            await enforce_task_access('rejection_checks', tid, user)
        result = await asyncio.to_thread(rejection.delete_tasks, req.task_ids)
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.post('/batch/mark-false-positive')
async def batch_mark_false_positive(req: BatchMarkFalsePositiveRequest, user=Depends(get_current_user)):
    """批量标记所有项目为误报"""
    try:
        await enforce_task_access('rejection_checks', req.task_id, user)
        task = rejection.get_task(req.task_id)
        if not task:
            raise HTTPException(status_code=404, detail='任务不存在')
        fps = await asyncio.to_thread(rejection.mark_all_false_positive, req.task_id, req.item_ids)
        return {'task_id': req.task_id, 'false_positives': fps}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.get('/knowledge-suggestions/{project_id}')
async def get_knowledge_suggestions(project_id: int, user=Depends(get_current_user)):
    """从知识库获取检查项建议（与知识库模块的数据连通）

    BUG-FIX 2026-09-29（跨模块断链）：前端 RejectionCheckPage 以
    `res.status === "success"` 判定是否有建议，而后端只回
    `{'suggestions': ...}` 没有 status —— 知识库建议链路从未真正接通，
    「加载知识库建议」按钮恒不出现。现补齐 status 字段。
    """
    try:
        await enforce_project_access(project_id, user)
        from ..services.knowledge import search_items as kb_search_items
        result = kb_search_items(keyword='废标', limit=10)
        return {'status': 'success', 'suggestions': result['items'][:5]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.post('/extract-items', dependencies=[Depends(get_current_user)])
async def extract_items(req: ExtractItemsRequest):
    """从招标文本提取废标条款（前端可单独调用）"""
    if not req.tender_text.strip():
        raise HTTPException(status_code=400, detail='招标文本不能为空')
    try:
        ai_config = get_ai_config()
        provider = create_provider(ai_config)
        items = await rejection.extract_rejection_items(provider, req.tender_text)
        return {'items': items}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'提取失败: {e}') from e

@router.get('/import-from-project/{project_id}')
async def import_from_project(project_id: int, user=Depends(get_current_user)):
    """从项目导入招标原文和投标方案正文"""
    try:
        # ENHANCE 2026-09-29（P1 IDOR 收口）：本接口直接读取项目正文
        await enforce_project_access(project_id, user)
        return await rejection.import_from_project(project_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'导入失败: {e}') from e

@router.post('/schedule-report')
async def schedule_report(req: ReportScheduleRequest, user=Depends(get_current_user)):
    """创建报告定时调度任务"""
    try:
        await enforce_task_access('rejection_checks', req.task_id, user)
        task = rejection.get_task(req.task_id)
        if not task:
            raise HTTPException(status_code=404, detail='任务不存在')
        schedule_id = await asyncio.to_thread(rejection.create_report_schedule, req.task_id, req.schedule_type, req.schedule_time, req.enabled)
        return {'status': 'success', 'schedule_id': schedule_id, 'message': '调度已创建'}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.get('/schedules/{task_id}')
async def get_schedules(task_id: int, user=Depends(get_current_user)):
    """获取某任务的定时调度列表"""
    try:
        await enforce_task_access('rejection_checks', task_id, user)
        schedules = await asyncio.to_thread(rejection.get_report_schedules_by_task, task_id)
        return {'total': len(schedules), 'items': schedules}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.patch('/schedule/{schedule_id}/enable')
async def enable_schedule(schedule_id: int, enabled: bool, user=Depends(get_current_user)):
    """启用/禁用定时调度任务"""
    try:
        # ENHANCE 2026-09-29（P1 IDOR 收口）：调度挂在任务→项目上，经链反查鉴权
        await _guard_schedule_access(schedule_id, user)
        if not await asyncio.to_thread(rejection.update_report_schedule_status, schedule_id, enabled):
            raise HTTPException(status_code=404, detail='调度任务不存在')
        return {'status': 'success', 'enabled': enabled}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.delete('/schedule/{schedule_id}')
async def delete_schedule(schedule_id: int, user=Depends(get_current_user)):
    """删除定时调度任务"""
    try:
        await _guard_schedule_access(schedule_id, user)
        if not await asyncio.to_thread(rejection.delete_report_schedule, schedule_id):
            raise HTTPException(status_code=404, detail='调度任务不存在')
        return {'status': 'success', 'message': '调度已删除'}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

def _require_admin(user) -> None:
    """定时调度属**跨项目**资源，其调度列表仅管理员可见。

    BUG-FIX 2026-10-06（安全 P1·越权）：`GET /rejection/all-schedules` 原先只挂了
    router 级的 `get_current_user` —— 任何登录用户都能列出**全部项目**的废标报告
    调度（项目 ID、调度类型、调度时间、启用状态），等于开放一个跨项目的情报面。
    docstring 虽写「管理员可用」，但代码从未校验角色，属于注释与实现不一致的
    典型越权。现与 `routers/api_keys.py::_require_admin` 同口径补角色门禁。
    """
    if user is None or getattr(user, 'role', '') != 'admin':
        raise HTTPException(status_code=403, detail='仅管理员可查看全部废标报告调度')


@router.get('/all-schedules', dependencies=[Depends(get_current_user)])
async def list_all_schedules(limit: int=100, user=Depends(get_current_user)):
    """列出所有定时调度任务（仅管理员）"""
    # BUG-FIX 2026-10-06（安全 P1·越权）：补角色门禁，详见 _require_admin。
    _require_admin(user)
    try:
        schedules = await asyncio.to_thread(rejection.get_all_report_schedules, limit)
        return {'total': len(schedules), 'items': schedules}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
