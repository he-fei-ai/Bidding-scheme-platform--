#!/usr/bin/env python3
"""
速率限制依赖项（FastAPI 适配版）- 使用 Redis 后端

此模块提供 FastAPI 可直接使用的依赖函数，封装了服务层的速率限制逻辑。
"""

import logging
import os

from fastapi import Depends, HTTPException, Request


logger = logging.getLogger(__name__)

# 导入服务层
from ..services.rate_limiting import get_rate_limiter


# 配置：每个端点可覆盖的默认配额
DEFAULT_LIMIT = 100  # 每窗口最大请求数
DEFAULT_WINDOW = 60  # 秒


def _rate_limit_bypassed_in_tests() -> bool:
    """测试环境下是否跳过 HTTP 限流（2026-10-20 修复全量套件 429 污染）。

    根因（有实录）：``TestClient`` 的所有请求共享同一个标识符
    ``ip_testclient`` —— 无认证注入（``request.state.current_user`` 为空）、
    无 API Key、``client.host`` 对 TestClient 固定为 ``testclient``。
    全量套件单进程跑出数千个 HTTP 请求，远超 100 次/60s 的滑动窗口后，
    后运行的测试文件全部拿到 429：``test_outline_hardening_2026_10_08.py``
    的 8 个用例即因此失败（``_mk_section`` 断言 201 实得 429）。
    单文件运行或 ``-k`` 选择运行时请求量小、不触发，表现为"间歇性失败"，
    早期被误判为"跨文件 DB 污染"。

    为什么在**依赖层**跳过而不是限速器内部：限速器算法本身由
    ``tests/test_sections_integration.py::TestRateLimiting`` 等服务级
    单测直接覆盖（它们调用 ``get_rate_limiter().allow(...``），在依赖层
    跳过不影响这些用例，也不削弱算法正确性验证。

    生产环境无 ``PYTEST_RUNNING`` 标记（conftest 在任何 app 导入前设置），
    行为零变化。需要验证 HTTP 429 行为的测试可 monkeypatch 本函数返回
    False 后按原路径断言。
    """
    return os.environ.get("PYTEST_RUNNING") == "1"


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
    # BUG-FIX 2026-10-20（全量套件 429 污染根因修复）：测试环境跳过限流。
    # 详见 _rate_limit_bypassed_in_tests 的根因注释。放行而非抛错，
    # 保证 TestClient 集成测试在完整跑套件时不被共享标识符的配额耗光。
    if _rate_limit_bypassed_in_tests():
        return

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
