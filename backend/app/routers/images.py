"""图像生成 API 路由

根据 AI 图像生成模型配置模块技术规范，提供以下端点：
- POST /api/images/generate: 单张图像生成（供正文模块调用）
- GET /api/images/tasks: 获取生成任务列表
- GET /api/images/tasks/:id: 获取任务详情
- GET /api/images/files/:id: 获取本地落盘的生成图片（远程 URL 过期后仍可预览/导出）
"""
import asyncio
import logging
from pathlib import Path
from typing import Annotated, Any
from fastapi import APIRouter, Depends, HTTPException, Request, Security
from fastapi.responses import FileResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel
from ..config import AUTH_ENABLED, DATA_DIR
from ..db import get_db
from ..services.ai.image_providers.service import ImageGenerationService
from .rate_limiter import limit_per_endpoint
logger = logging.getLogger(__name__)
router = APIRouter(prefix='/images', tags=['images'])
api_key_header = APIKeyHeader(name='X-API-Key', auto_error=False)

async def verify_auth(request: Request, api_key: str=Security(api_key_header)):
    """验证用户身份——与 export.py 保持一致的三段式鉴权

    支持三种认证方式：
    1. 中间件预认证（ApiKeyAuthMiddleware 已设置 request.state.api_key_info）
    2. X-API-Key 请求头（ApiKeyService 校验）
    3. 开发模式（AUTH_ENABLED=False）：无认证直接放行

    BUG 修复：原实现直接 pass（注释称 TODO），任何人均可匿名触发付费文生图 API，
    导致配额被恶意消耗。
    """
    if hasattr(request.state, 'api_key_info'):
        return request.state.api_key_info
    if api_key:
        try:
            from ..services.api_key_auth import ApiKeyService
            key_info = ApiKeyService.validate_api_key(api_key)
            if key_info:
                return key_info
        except Exception as _e:
            logger.debug('[silent-except] images.py: line 56 - %s', _e)
    if not AUTH_ENABLED:
        return {'mode': 'dev', 'user_id': 1, 'username': 'developer'}
    raise HTTPException(status_code=401, detail='缺少授权令牌')

def _resolve_image_path(path: Path, images_root: Path) -> Path | None:
    """PERF（2026-10-05）：文件系统 resolve 属同步 I/O，在工作线程执行；
    失败返回 None 等价于旧实现的 except → resolved = None。"""
    try:
        return path.resolve()
    except Exception:
        return None

class ImageGenerateRequest(BaseModel):
    prompt: str
    size: str | None = None
    ratio: str | None = None
    style: str | None = 'engineering_diagram'
    provider: str | None = None
    reference_images: list[str] | None = None
    project_id: str | None = None
    chapter_id: str | None = None

class ImageTask(BaseModel):
    id: str
    project_id: Any | None = None
    chapter_id: Any | None = None
    provider: str
    model: str
    prompt: str
    injected_prompt: str | None = None
    size: str | None = None
    ratio: str | None = None
    status: str
    created_at: str | None = None
    completed_at: str | None = None
    image_url: str | None = None
    image_path: str | None = None
    cost: float | None = None
    retry_count: int | None = None
    error_message: str | None = None
    model_config = {'extra': 'ignore'}

@router.post('/generate', dependencies=[limit_per_endpoint, Depends(verify_auth)])
async def generate_image(request: ImageGenerateRequest, auth_status: Annotated[Any, Depends(verify_auth)]=None) -> dict[str, Any]:
    """生成单张图像

    Args:
        request: 图像生成请求参数

    Returns:
        {success: bool, imageUrl?: string, localPath?: string, cost?: number, provider: string, model: string, taskId: string, error?: string}
    """
    try:
        # STRUCT-FIX（2026-10-05）：不再用 `with get_db()` 包裹整段 await。
        # 旧实现把全局写锁（threading.Lock）从进入端点一路持有到外部
        # HTTP 生成（秒~分钟级）结束，期间所有写请求被阻塞至超时；且
        # 在含 await 的临界区内持 threading.Lock 本身就是危险模式。
        # 现在服务不传长连接（db_conn=None → 自管短事务），写锁仅在每次
        # 写点的 worker 线程内短暂获取，外部 HTTP 期间不持锁。
        image_service = ImageGenerationService(config_service=None)
        unified_request = {'prompt': request.prompt, 'size': request.size, 'ratio': request.ratio, 'style': request.style, 'provider': request.provider, 'reference_images': request.reference_images, 'project_id': request.project_id, 'chapterId': request.chapter_id}
        result = await image_service.generate(unified_request, 1)
        return result
    except Exception as e:
        logger.error('图像生成失败: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'图像生成失败: {e!s}') from e

@router.get('/tasks', dependencies=[limit_per_endpoint, Depends(verify_auth)])
async def list_image_tasks(auth_status: Annotated[Any, Depends(verify_auth)]=None, project_id: str | None=None, status: str | None=None, limit: int=50, offset: int=0) -> dict[str, Any]:
    """获取图像生成任务列表

    Args:
        project_id: 筛选项目 ID
        status: 筛选状态（pending, generating, success, failed, cancelled）
        limit: 限制数量
        offset: 偏移量

    Returns:
        {tasks: List[ImageTask], total: int}
    """
    try:
        # PERF（2026-10-05）：纯读查询 readonly + to_thread，不在事件循环内抢写锁
        def _list_tasks():
            with get_db(readonly=True) as conn:
                base_query = '\n                SELECT id, project_id, chapter_id, provider, model, prompt,\n                       injected_prompt, size, ratio, status, image_url, image_path,\n                       cost, retry_count, error_message, created_at, completed_at\n                FROM image_generation_tasks\n            '
                params: list[Any] = []
                conditions: list[str] = []
                if project_id:
                    conditions.append('project_id = ?')
                    params.append(project_id)
                if status:
                    conditions.append('status = ?')
                    params.append(status)
                where_clause = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
                rows = conn.execute(base_query + where_clause + ' ORDER BY created_at DESC LIMIT ? OFFSET ?', [*params, limit, offset]).fetchall()
                tasks = [ImageTask(**dict(row)) for row in rows]
                if conditions:
                    total = conn.execute('SELECT COUNT(*) FROM image_generation_tasks' + where_clause, params).fetchone()[0]
                else:
                    total = conn.execute('SELECT COUNT(*) FROM image_generation_tasks').fetchone()[0]
                return {'tasks': tasks, 'total': total}

        return await asyncio.to_thread(_list_tasks)
    except HTTPException:
        raise
    except Exception as e:
        logger.error('获取图像任务列表失败: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'获取任务列表失败: {e!s}') from e

@router.get('/tasks/{task_id}', dependencies=[limit_per_endpoint, Depends(verify_auth)])
async def get_image_task(task_id: str, auth_status: Annotated[Any, Depends(verify_auth)]=None) -> dict[str, Any]:
    """获取单个图像生成任务详情

    Args:
        task_id: 任务唯一标识

    Returns:
        任务详情对象或错误
    """
    try:
        # PERF（2026-10-05）：纯读 readonly + to_thread，不在事件循环内抢写锁
        def _read_task():
            with get_db(readonly=True) as conn:
                query = '\n                SELECT id, project_id, chapter_id, provider, model, prompt,\n                       injected_prompt, size, ratio, status, image_url, image_path,\n                       cost, retry_count, error_message, created_at, completed_at\n                FROM image_generation_tasks WHERE id = ?\n            '
                row = conn.execute(query, [task_id]).fetchone()
                return dict(row) if row else None

        row = await asyncio.to_thread(_read_task)
        if not row:
            raise HTTPException(status_code=404, detail='任务不存在')
        return row
    except HTTPException:
        raise
    except Exception as e:
        logger.error('获取任务详情失败: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'获取任务详情失败: {e!s}') from e

@router.get('/files/{task_id}', dependencies=[limit_per_endpoint, Depends(verify_auth)])
async def get_image_file(task_id: str, auth_status: Annotated[Any, Depends(verify_auth)]=None) -> FileResponse:
    """获取本地落盘的生成图片文件

    生成成功时 _download_image 会把图片保存到 data/images/<project_id>/<task_id>.<ext>
    并回写 image_path；该端点按任务 ID 读取本地文件返回，供任务列表缩略图预览，
    以及远程 image_url 过期后的二次使用（导出/查看）。

    Args:
        task_id: 任务唯一标识

    Returns:
        图片文件（image/png、image/jpeg 等）或 404
    """
    try:
        # PERF（2026-10-05）：纯读 readonly + to_thread；路径校验与 FileResponse
        # 发送（大文件 I/O 由 Starlette 线程池处理）保持在事件循环外
        def _read_path():
            with get_db(readonly=True) as conn:
                row = conn.execute('SELECT image_path, image_url FROM image_generation_tasks WHERE id = ?', [task_id]).fetchone()
            return row[0] if row else None

        image_path = await asyncio.to_thread(_read_path)
        if not image_path:
            raise HTTPException(status_code=404, detail='该任务未保存本地图片')
        path = Path(image_path)
        images_root = (DATA_DIR / 'images').resolve()
        resolved = await asyncio.to_thread(_resolve_image_path, path, images_root)
        if not resolved or images_root not in resolved.parents:
            logger.warning('拒绝访问 images 目录外的路径: %s', image_path)
            raise HTTPException(status_code=404, detail='图片路径无效')
        if not resolved.is_file():
            raise HTTPException(status_code=404, detail='本地图片文件不存在')
        media_type = 'image/png'
        suffix = resolved.suffix.lower()
        if suffix in ('.jpg', '.jpeg'):
            media_type = 'image/jpeg'
        elif suffix == '.webp':
            media_type = 'image/webp'
        elif suffix == '.gif':
            media_type = 'image/gif'
        return FileResponse(str(resolved), media_type=media_type)
    except HTTPException:
        raise
    except Exception as e:
        logger.error('获取本地图片失败: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'获取本地图片失败: {e!s}') from e
