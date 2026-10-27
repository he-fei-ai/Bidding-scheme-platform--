"""批量操作路由

提供批量数据库操作的API接口。
"""
import logging
from fastapi import APIRouter, Depends, HTTPException
from ..security.auth import get_current_user
from ..services.batch_query import batch_optimizer
from .project_access import enforce_project_access
logger = logging.getLogger(__name__)
router = APIRouter(prefix='/batch', tags=['batch_operations'], dependencies=[Depends(get_current_user)])

@router.get('/statistics')
async def get_statistics():
    """获取项目统计数据"""
    try:
        stats = batch_optimizer.get_project_statistics()
        return {'status': 'ok', 'statistics': stats}
    except Exception as e:
        logger.error('获取统计数据失败: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'获取统计失败: {e!s}') from e

@router.post('/projects')
async def batch_upsert_projects(projects: list[dict]):
    """批量插入或更新项目

    Args:
        projects: [{"id": int (可选), "name": str, "description": str, ...}, ...]

    Returns:
        操作的项目ID列表
    """
    try:
        if not projects:
            raise HTTPException(status_code=400, detail='项目列表不能为空')
        operated_ids = batch_optimizer.batch_upsert_projects(projects)
        return {'status': 'ok', 'message': f'成功操作 {len(operated_ids)} 个项目', 'project_ids': operated_ids}
    except HTTPException:
        raise
    except Exception as e:
        logger.error('批量操作项目失败: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'批量操作失败: {e!s}') from e

@router.post('/sections/status')
async def batch_update_section_status(project_id: int, section_updates: list[tuple[int, str]]):
    """批量更新章节状态

    Args:
        project_id: 项目ID
        section_updates: [[section_id, status], ...] 列表

    Returns:
        更新的行数
    """
    try:
        if not section_updates:
            raise HTTPException(status_code=400, detail='章节更新列表不能为空')
        updated_count = batch_optimizer.batch_update_section_status(project_id, section_updates)
        return {'status': 'ok', 'message': f'成功更新 {updated_count} 个章节状态', 'updated_count': updated_count}
    except HTTPException:
        raise
    except Exception as e:
        logger.error('批量更新章节状态失败: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'批量更新失败: {e!s}') from e

@router.get('/files')
async def batch_get_files(project_ids: str, identity=Depends(get_current_user)):
    """批量获取多个项目的文件列表

    Args:
        project_ids: 项目ID列表，逗号分隔（如 "1,2,3"）

    Returns:
        {project_id: [file_list], ...}
    """
    # ENHANCE（本轮跨模块深度探索·IDOR 口径收口）：本端点此前只验登录、不验
    # 项目归属——任意登录用户可传他人 project_id 读取其文件列表，与 files.py
    # 2026-09-29 的属权收口不同口径。现按 ids 逐个 enforce_project_access
    # （无权 403 / 不存在 404），与全仓库同一口径。注：前端无任何调用方
    # （全仓 grep 0 命中），属内部/管理端点，收口无兼容影响。
    try:
        ids = [int(pid.strip()) for pid in project_ids.split(',') if pid.strip()]
        if not ids:
            raise HTTPException(status_code=400, detail='请提供有效的项目ID列表')
        for _pid in ids:
            await enforce_project_access(_pid, identity)
        files_map = batch_optimizer.batch_get_project_files(ids)
        return {'status': 'ok', 'files': files_map}
    except HTTPException:
        raise
    except Exception as e:
        logger.error('批量获取文件失败: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'批量获取失败: {e!s}') from e
