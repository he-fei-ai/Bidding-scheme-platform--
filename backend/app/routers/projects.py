import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException

from ..models import ProjectCreate, ProjectList, ProjectUpdate
from ..security.auth import get_current_user
from ..services import projects as project_service
from .project_access import enforce_project_access

logger = logging.getLogger(__name__)
router = APIRouter(
    prefix='/projects',
    tags=['projects'],
    dependencies=[Depends(get_current_user)],
)

@router.get('', response_model=ProjectList)
async def list_projects():
    try:
        items = project_service.list_projects()
        return ProjectList(total=len(items), items=items)
    except Exception as e:
        logger.error('列出项目失败: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'服务器内部错误: {e}') from e

@router.post('', response_model=dict, status_code=201)
async def create_project(data: ProjectCreate, current_user=Depends(get_current_user)):
    try:
        # BUG-FIX 2026-09-18（P1）：创建时落盘 created_by，多用户隔离链路补齐
        # （此前 projects.created_by 有读无写，权限检查因列缺失而整体跳过）。
        uid = getattr(current_user, 'id', None)
        project = project_service.create_project(data, created_by=uid)
        return {'id': project.id}
    except Exception as e:
        logger.error('创建项目失败: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'服务器内部错误: {e}') from e

@router.get('/{project_id}', response_model=dict)
async def get_project(project_id: int, current_user: Annotated[object, Depends(get_current_user)]=None):
    """获取项目详情"""
    # ENHANCE 2026-09-29（P1 IDOR 收口·补齐链首）：按 id 的读/改/删接入
    # 与 sections 同口径的属权校验；列表/创建维持全局工作区语义不变
    # （历史单用户部署的存量项目 created_by 为 NULL，本就放行）。
    try:
        await enforce_project_access(project_id, current_user)
        project = project_service.get_project(project_id)
        if not project:
            raise HTTPException(status_code=404, detail='项目不存在')
        return project.model_dump()
    except HTTPException:
        raise
    except Exception as e:
        logger.error('获取项目失败 project_id=%s: %s', project_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'服务器内部错误: {e}') from e

@router.patch('/{project_id}', response_model=dict)
async def update_project(project_id: int, data: ProjectUpdate, current_user: Annotated[object, Depends(get_current_user)]=None):
    try:
        await enforce_project_access(project_id, current_user)
        project = project_service.update_project(project_id, data)
        if not project:
            raise HTTPException(status_code=404, detail='项目不存在')
        return project.model_dump()
    except HTTPException:
        raise
    except Exception as e:
        logger.error('更新项目失败 project_id=%s: %s', project_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'服务器内部错误: {e}') from e

@router.delete('/{project_id}', status_code=204)
async def delete_project(project_id: int, current_user: Annotated[object, Depends(get_current_user)]=None):
    try:
        await enforce_project_access(project_id, current_user)
        if not project_service.delete_project(project_id):
            raise HTTPException(status_code=404, detail='项目不存在')
    except HTTPException:
        raise
    except Exception as e:
        logger.error('删除项目失败 project_id=%s: %s', project_id, e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'服务器内部错误: {e}') from e
