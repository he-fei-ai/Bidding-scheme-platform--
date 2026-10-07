"""API Key认证中间件

为需要API Key认证的端点提供身份验证。
通过 X-API-Key 请求头传递密钥。
"""

import logging

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from ..services.api_key_auth import ApiKeyService


logger = logging.getLogger(__name__)


class ApiKeyAuthMiddleware(BaseHTTPMiddleware):
    """API Key认证中间件

    使用方法：
    1. 添加到main.py的middleware列表
    2. 在需要认证的路由上添加依赖项
    """

    # 不需要认证的路由白名单
    PUBLIC_ROUTES = [
        "/api/docs",
        "/api/openapi.json",
        "/api/keys",  # API Key管理接口本身
    ]

    async def dispatch(self, request: Request, call_next):
        """处理请求认证"""

        # 检查是否是公开路由
        path = request.url.path
        if any(path.startswith(public) for public in self.PUBLIC_ROUTES):
            return await call_next(request)

        # 获取API Key
        api_key = request.headers.get("X-API-Key")
        if not api_key:
            return JSONResponse(
                status_code=401,
                content={"detail": "缺少API Key", "message": "请在请求头中添加 X-API-Key 字段"},
            )

        # 验证API Key
        key_info = ApiKeyService.validate_api_key(api_key)
        if not key_info:
            logger.warning("无效的API Key: %s...", api_key[:8])
            return JSONResponse(
                status_code=401,
                content={"detail": "无效的API Key", "message": "请检查API Key是否正确且未过期"},
            )

        # 将用户信息添加到request.state
        request.state.api_key_info = key_info

        # 继续处理请求
        response = await call_next(request)

        # 添加速率限制头
        response.headers["X-RateLimit-Limit"] = str(key_info["rate_limit"])
        response.headers["X-RateLimit-Remaining"] = str(max(0, key_info["rate_limit"] - 1))

        return response


def require_api_key(request: Request):
    """依赖项：验证请求包含有效的API Key

    使用示例：
    @router.get("/projects", dependencies=[Depends(require_api_key)])
    async def list_projects():
        ...
    """
    if not hasattr(request.state, "api_key_info"):
        raise HTTPException(status_code=401, detail="请先通过中间件认证")

    return request.state.api_key_info


def get_current_api_key_info(request: Request) -> dict:
    """获取当前API Key的信息

    Args:
        request: FastAPI Request对象

    Returns:
        API Key信息字典
    """
    if not hasattr(request.state, "api_key_info"):
        raise HTTPException(status_code=401, detail="未通过API Key认证")

    return request.state.api_key_info
