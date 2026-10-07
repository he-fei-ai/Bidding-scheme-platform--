"""跨模块统一的项目属权校验助手

ENHANCE 2026-09-29（P1 IDOR 收口）：
sections / facts / content_version / tasks 路由早已逐端点调用
`require_project_permission`（sections.py），但审核与预检（rejection /
duplicate / consistency / word_count）及导出（export）五条路由仍以
「已登录」为唯一门槛 —— 任意登录用户可读取、改写、导出他人项目数据
（consistency autofix 甚至能直接改写别的项目正文）。

本模块把口径统一到与 sections 完全相同的判定实现（直接复用，不另起炉灶），
并兼容导出路由的三种身份来源：
- security.auth.User 对象（JWT 经 get_current_user 注入）
- dict {'id', ...}（JwtAuthMiddleware 的 request.state.current_user）
- API Key 信息 dict（无用户 id → 与 sections 对「无创建者记录」的处理一致，放行）
"""
import logging
from typing import Any

from fastapi import HTTPException

logger = logging.getLogger(__name__)


async def enforce_project_access(project_id: int, identity: Any) -> None:
    """校验当前身份对 project_id 的访问权；无权抛 403、项目不存在抛 404。

    判定逻辑完全委托 `sections.require_project_permission`（单一事实来源，
    避免两套实现漂移）：
    - projects 表无 created_by 列 → 向后兼容放行（仅一次 WARNING）
    - created_by 为 NULL（历史项目）→ 放行
    - created_by 与当前用户 id 不符 → 403
    """
    # 惰性导入：routers 包在 main 启动期才完整装配，模块级导入 sections
    # 会牵出其大量服务依赖，放大循环导入风险。
    from .sections import require_project_permission

    user = identity
    if isinstance(identity, dict):
        from ..security.auth import User

        uid = identity.get('id') or identity.get('user_id')
        # API Key 身份（key_id/permissions，无用户 id）：保持既有开放语义，
        # require_project_permission 对无 id 身份同样放行。
        if uid is None:
            return
        user = User(user_id=int(uid), username=str(identity.get('username') or identity.get('name') or ''), role=str(identity.get('role') or 'user'))
    await require_project_permission(project_id, user)


def _project_id_of_task(table: str, task_id: int) -> int | None:
    """按任务 ID 反查其归属项目；任务不存在抛 404，无归属项目返回 None。"""
    from ..db import get_db

    with get_db(readonly=True) as conn:
        row = conn.execute(f'SELECT project_id FROM {table} WHERE id = ?', (task_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail='任务不存在')
    return row['project_id']


async def enforce_task_access(table: str, task_id: int, identity: Any) -> int | None:
    """校验当前身份对「任务所属项目」的访问权。

    Returns:
        任务的 project_id（可能为 None —— 纯文本模式任务不挂项目，放行）。
    """
    project_id = _project_id_of_task(table, task_id)
    if project_id:
        await enforce_project_access(project_id, identity)
    return project_id
