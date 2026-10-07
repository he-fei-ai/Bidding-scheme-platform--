"""智能问答路由

提供 3 个 API 端点：
- POST /qa/chat                执行一次问答
- GET  /qa/history/{project_id} 获取项目问答历史
- POST /qa/clear               清空项目问答历史
"""
import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from ..models import QaChatRequest
from ..security.auth import get_current_user
from ..services import qa as qa_service
logger = logging.getLogger(__name__)
router = APIRouter(prefix='/qa', tags=['qa'], dependencies=[Depends(get_current_user)])

@router.post('/chat')
async def chat(req: QaChatRequest):
    """执行一次智能问答

    基于项目信息、招标文件文本与已生成章节内容回答用户问题，
    返回答案与信息来源章节。
    """
    try:
        result = await qa_service.qa_chat(project_id=req.project_id, question=req.question, history=req.history)
        result['sources'] = qa_service.format_sources(result.get('sources', []))
        return {'status': 'ok', **result}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error('智能问答失败: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'问答服务调用失败: {str(e)[:200]}') from e

@router.get('/history/{project_id}')
async def get_history(project_id: int):
    """获取指定项目的问答历史"""
    try:
        history = qa_service.get_history(project_id)
        return {'project_id': project_id, 'items': history, 'total': len(history)}
    except Exception as e:
        logger.error('获取问答历史失败: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e

@router.post('/clear')
async def clear_history(project_id: int=Query(..., description='项目ID')):
    """清空指定项目的问答历史"""
    try:
        qa_service.clear_history(project_id)
        return {'status': 'ok', 'project_id': project_id}
    except Exception as e:
        logger.error('清空问答历史失败: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e
