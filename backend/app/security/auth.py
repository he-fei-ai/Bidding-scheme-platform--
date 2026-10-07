"""
Authentication module.

Provides stub and real implementations for the security system.
"""

import logging

from fastapi import Request


logger = logging.getLogger(__name__)


class User:
    """User representation"""

    def __init__(self, user_id: int, username: str, role: str = "user"):
        self.id = user_id
        self.username = username
        self.role = role

    def __repr__(self):
        return f"User(id={self.id}, username={self.username!r}, role={self.role!r})"


def get_current_user(request: Request) -> User | None:
    """
    优先从真实 JWT 解析用户；AUTH_ENABLED=False 且无 token 时返回 mock admin 兜底。

    生产环境（AUTH_ENABLED=True）下不吞没异常，让认证失败传播到 FastAPI 错误处理器。
    """
    from ..config import AUTH_ENABLED

    from fastapi import HTTPException

    try:
        from ..services.auth_service import get_current_user_dependency

        # request 可能来自 FastAPI 注入，也可能是测试传入的 None
        if request is None:
            if not AUTH_ENABLED:
                return User(user_id=1, username="admin", role="admin")
            return None
        info = get_current_user_dependency(request)
        return User(user_id=info["id"], username=info["username"], role=info["role"])
    except HTTPException as exc:
        # BUG-FIX（P0 认证绕过）：此前对**任何**异常都降级为 mock admin，
        # 包括「携带了无效/过期 token」的 401 —— 与 auth_service 里
        # 「存在任何无效 Bearer token 一律抛 401」的约定直接冲突，
        # 结果是拿一个随便编的 token 也能拿到 admin 身份。
        # 认证类失败必须原样传播，其余异常才按开发模式兜底。
        if exc.status_code in (401, 403):
            raise
        if not AUTH_ENABLED:
            return User(user_id=1, username="admin", role="admin")
        raise
    except Exception:
        if not AUTH_ENABLED:
            # 开发模式：降级为 mock admin，避免阻塞启动和调试
            return User(user_id=1, username="admin", role="admin")
        # 生产模式：不吞没异常，让认证失败传播
        raise


def check_project_access(
    project_id: int, current_user: User, required_role: str = "viewer"
) -> bool:
    """
    Check if current user has access to the specified project.

    Admin users always have access. For regular users, checks database
    for project membership or ownership.

    Args:
        project_id: The project to check access for.
        current_user: The authenticated user.
        required_role: Minimum role required (viewer, editor, admin).

    Returns:
        True if the user has access, False otherwise.
    """
    # Admin bypass
    if current_user.role == "admin":
        return True

    # BUG-FIX 2026-09-15 (高危)：此前只要项目存在就放行，普通用户可横向
    # 读取/修改任意项目。当前数据库尚未建立 owner/membership 关系，
    # 因此宁可 fail-closed；待成员模型落地后再按 required_role 查询授权。
    logger.warning(
        "拒绝未实现成员关系的项目访问 project_id=%s user_id=%s role=%s required_role=%s",
        project_id,
        current_user.id,
        current_user.role,
        required_role,
    )
    return False
