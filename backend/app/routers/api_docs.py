"""API文档路由

提供API文档的查看和导出功能。
"""
import logging
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from ..services.api_documentation import export_api_document
logger = logging.getLogger(__name__)
router = APIRouter(prefix='/docs', tags=['api_docs'])

@router.get('')
async def view_api_document(format: str='markdown'):
    """查看API文档

    Args:
        format: 文档格式 (markdown, html, openapi)

    Returns:
        API文档内容
    """
    try:
        result = export_api_document(format=format)
        return Response(content=result['content'], media_type=result['content_type'])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error('生成API文档失败: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'生成文档失败: {e!s}') from e

@router.get('/download')
async def download_api_document(format: str='markdown', filename: str=None):
    """下载API文档

    Args:
        format: 文档格式 (markdown, html, openapi)
        filename: 自定义文件名

    Returns:
        文件下载
    """
    try:
        result = export_api_document(format=format)
        if not filename:
            ext_map = {'markdown': '.md', 'html': '.html', 'openapi': '.json'}
            filename = f"api_documentation{ext_map.get(format, '.txt')}"
        content_disposition = f'attachment; filename="{filename}"'
        return Response(content=result['content'], media_type=result['content_type'], headers={'Content-Disposition': content_disposition})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.error('下载API文档失败: %s', e, exc_info=True)
        raise HTTPException(status_code=500, detail=f'下载文档失败: {e!s}') from e

@router.get('/summary')
async def get_api_summary():
    """获取API概览信息

    Returns:
        API端点汇总信息
    """
    from ..services.api_documentation import API_ENDPOINTS
    summary = {'total_endpoints': sum((len(g['endpoints']) for g in API_ENDPOINTS)), 'groups': [{'name': group['description'], 'path': group['path'], 'methods': group['methods'], 'endpoint_count': len(group['endpoints'])} for group in API_ENDPOINTS]}
    return {'status': 'ok', 'summary': summary}
