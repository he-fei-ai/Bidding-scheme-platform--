"""用量统计路由

提供 API 调用统计和使用量分析功能。
"""

import logging

from fastapi import APIRouter, Depends

from ..security.auth import get_current_user


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects/{project_id}/usage", tags=["usage"], dependencies=[Depends(get_current_user)])


@router.get("/stats")
async def get_usage_stats(project_id: int):
    """获取项目用量统计"""
    return {"project_id": project_id, "stats": {}}


@router.get("/history")
async def get_usage_history(project_id: int, days: int = 30):
    """获取用量历史记录"""
    return {"project_id": project_id, "days": days, "history": []}


# ==================== 全局 usage 路由（无前缀） ====================

_global_usage_router = APIRouter(tags=["usage"], dependencies=[Depends(get_current_user)])


@_global_usage_router.get("/usage")
async def get_global_usage():
    """获取全局用量统计（无需 project_id）"""
    return {
        "total_limit": 10000,
        "used_count": 0,
        "remaining": 10000,
        "consumed_project_ids": [],
    }


@_global_usage_router.get("/usage/{project_id}")
async def get_project_usage(project_id: int):
    """获取指定项目的用量统计"""
    return {
        "project_id": project_id,
        "total_limit": 10000,
        "used_count": 0,
        "remaining": 10000,
        "consumed_project_ids": [],
    }


@_global_usage_router.post("/usage/reset")
async def reset_usage():
    """重置用量统计（管理端点）"""
    return {"ok": True, "message": "用量已重置"}
