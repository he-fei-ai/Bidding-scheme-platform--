#!/usr/bin/env python3
"""
速率限制依赖项（FastAPI 适配版）- 使用 Redis 后端

此模块提供 FastAPI 可直接使用的依赖函数，封装了服务层的速率限制逻辑。
"""

import logging

from fastapi import Depends, HTTPException, Request


logger = logging.getLogger(__name__)

# 导入服务层
from ..services.rate_limiting import get_rate_limiter


# 配置：每个端点可覆盖的默认配额
DEFAULT_LIMIT = 100  # 每窗口最大请求数
DEFAULT_WINDOW = 60  # 秒


def get_identifier_from_request(request: Request) -> str:
    """从请求中提取唯一标识符 - 优先 API Key，其次 IP

    实际部署中应结合认证系统获取 user_id 或 api_key_id。
    """
    # 检查是否已通过中间件注入用户信息（需启用 API Key 认证中间件）
    # BUG-FIX（P1）：JWT 中间件注入的是 **dict**（见 main.py 的
    # `request.state.current_user = get_current_user_dependency(request)`），
    # 而这里按对象取 `.id` → 认证一开启，所有挂 limit_per_endpoint 的端点
    # 直接 AttributeError → 500。两种形态都兼容。
    current_user = getattr(getattr(request, "state", None), "current_user", None)
    if current_user:
        uid = (
            current_user.get("id")
            if isinstance(current_user, dict)
            else getattr(current_user, "id", None)
        )
        if uid is not None:
            return f"user_{uid}"

    # 从 header 或查询参数获取 API key
    api_key = request.headers.get("X-API-Key") or request.query_params.get("api_key")
    if api_key:
        # 哈希处理避免暴露完整 key
        import hashlib

        return f"key_{hashlib.md5(api_key.encode()).hexdigest()[:8]}"

    # 回退到 IP 地址
    client_ip = request.client.host or "127.0.0.1"
    return f"ip_{client_ip}"


async def rate_limit_dependency(
    request: Request,
    limit: int | None = None,
    window: int | None = None,
) -> None:
    """
    FastAPI 依赖项：对当前请求进行速率限制检查。
    若超过限制则抛出 HTTP 429 异常。

    用法示例：
        @router.get("/endpoint", dependencies=[Depends(rate_limit_dependency(limit=50, window=60))])
        async def my_endpoint(...):
            ...

    Args:
        request: FastAPI 请求对象
        limit: 此端点的最大请求数（可选，覆盖全局默认值）
        window: 时间窗口秒数（可选，覆盖全局默认值）

    Raises:
        HTTPException: 状态码 429，当请求超过限额时
    """
    limiter = get_rate_limiter()
    identifier = get_identifier_from_request(request)
    local_limit = limit if limit is not None else DEFAULT_LIMIT
    local_window = window if window is not None else DEFAULT_WINDOW

    allowed, remaining = await limiter.allow(identifier, limit=local_limit, window=local_window)
    if not allowed:
        logger.warning("速率限制触发 - 标识符: %s, 剩余: %d", identifier, remaining)
        raise HTTPException(
            status_code=429,
            detail="请求过于频繁，请稍后重试",
            headers={"Retry-After": str(local_window)},
        )

    # （可选）将剩余配额放入响应头供前端查看
    # request.state.rate_limit_remaining = remaining


# 简洁版本，使用全局默认值
async def _common_rate_limit_func(request: Request) -> None:
    """使用全局默认值的速率限制函数"""
    await rate_limit_dependency(request)


common_rate_limit = Depends(_common_rate_limit_func)

# 兼容别名：供 sections.py、export.py 等模块使用
limit_per_endpoint = common_rate_limit
