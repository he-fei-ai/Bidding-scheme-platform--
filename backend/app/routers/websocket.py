"""WebSocket协作路由

提供WebSocket连接端点和REST API用于协作功能。
"""

import logging

from fastapi import APIRouter, Depends, Query, Request, WebSocket, WebSocketDisconnect

from ..security.auth import get_current_user
from ..services.collaboration import (
    CollaborativeEditor,
    CommentService,
    VersionCompareService,
    connection_manager,
)


logger = logging.getLogger(__name__)

router = APIRouter(tags=["collaboration"])


# ==================== WebSocket端点 ====================


def _authenticate_websocket(websocket: WebSocket, fallback_user_id: str) -> str:
    """校验 WebSocket 连接身份，返回可信 user_id；校验失败抛 HTTPException(401)。

    BUG-FIX（P1 未授权访问）：该端点此前完全没有鉴权 ——
    1. `BaseHTTPMiddleware` 在 Starlette 下只对 `scope["type"] == "http"` 生效，
       即使 AUTH_ENABLED=true 也拦不住 WebSocket；
    2. `user_id` 直接取自 query，任何人 `?user_id=admin` 即可伪装身份，
       收发任意项目的编辑内容与评论。

    WebSocket 无法设置自定义请求头，因此沿用 SSE 的约定：从 `?token=` 取 JWT。
    """
    from fastapi import HTTPException

    from ..config import AUTH_ENABLED
    from ..services.auth_service import get_current_user_dependency

    if not AUTH_ENABLED:
        return fallback_user_id

    token = websocket.query_params.get("token", "")
    if not token:
        raise HTTPException(status_code=401, detail="缺少授权令牌")

    # 复用 HTTP 依赖的解析逻辑：需要 headers + query_params 两个属性
    class _WsRequest:
        headers = {"Authorization": f"Bearer {token}"}
        query_params = {}

    info = get_current_user_dependency(_WsRequest())
    return str(info.get("username") or info.get("id") or fallback_user_id)


def _trusted_user_id(request: Request, fallback_user_id: str) -> str:
    """REST 端点取**可信** user_id。

    BUG-FIX 2026-09-16（安全 P1 身份冒用）：这些 REST 端点此前直接信任 query 里的
    `user_id`（默认 "anonymous"），任何已登录用户都能 `?user_id=他人` 冒名发评论、
    写版本记录且无法溯源；WebSocket 端点早已用 `_authenticate_websocket` 修过，
    REST 侧却漏了。鉴权关闭时保持旧行为，不影响本地开发与既有用法。
    """
    from ..config import AUTH_ENABLED

    if not AUTH_ENABLED:
        return fallback_user_id

    user = getattr(request.state, "current_user", None)
    if user is None:
        return fallback_user_id

    if isinstance(user, dict):
        return str(user.get("username") or user.get("id") or fallback_user_id)
    return str(getattr(user, "username", "") or getattr(user, "id", "") or fallback_user_id)


@router.websocket("/ws/projects/{project_id}/sections/{section_id}")
async def section_websocket(
    websocket: WebSocket,
    project_id: int,
    section_id: int,
    user_id: str = Query(default="anonymous"),
):
    """章节协作WebSocket端点

    用于实时编辑、评论通知等。

    客户端发送的消息格式：
    - {"type": "edit", "operation": {...}} - 编辑操作
    - {"type": "comment", "content": "..."} - 添加评论
    - {"type": "ping"} - 心跳检测
    - {"type": "cursor", "position": int} - 光标位置同步

    服务端返回的消息格式：
    - {"type": "welcome", ...} - 连接成功
    - {"type": "edit", ...} - 其他用户的编辑操作
    - {"type": "user_joined/left", ...} - 用户加入/离开
    - {"type": "new_comment", ...} - 新评论
    """
    room = f"project:{project_id}:section:{section_id}"

    # 鉴权必须在 accept() 之前完成，否则未授权连接已经建立（可被用来探测房间）
    try:
        user_id = _authenticate_websocket(websocket, user_id)
    except Exception as _auth_e:
        status_code = getattr(_auth_e, "status_code", 401)
        await websocket.close(code=1008, reason=f"unauthorized: {status_code}")
        logger.warning(
            "[ws] 拒绝未授权连接 project=%d section=%d: %s",
            project_id,
            section_id,
            _auth_e,
        )
        return

    await websocket.accept()
    connection_id = await connection_manager.connect(websocket, user_id, room)

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type", "")

            if msg_type == "ping":
                # 心跳响应
                await websocket.send_json(
                    {
                        "type": "pong",
                        "timestamp": __import__("time").time(),
                    }
                )

            elif msg_type == "edit":
                # 处理编辑操作
                result = await CollaborativeEditor.handle_edit_operation(
                    room,
                    user_id,
                    data.get("operation", {}),
                    section_id,
                )
                await websocket.send_json(result)

            elif msg_type == "comment":
                # 添加评论
                result = await CommentService.add_comment(
                    project_id,
                    section_id,
                    user_id,
                    content=data.get("content", ""),
                    position=data.get("position"),
                    parent_id=data.get("parent_id"),
                )
                await websocket.send_json(result)

            elif msg_type == "cursor":
                # 广播光标位置
                await connection_manager.broadcast_to_room(
                    room,
                    {
                        "type": "cursor_update",
                        "user_id": user_id,
                        "connection_id": connection_id,
                        "position": data.get("position", 0),
                        "timestamp": __import__("time").time(),
                    },
                    exclude=connection_id,
                )

    except WebSocketDisconnect:
        logger.info("WebSocket断开: %s", connection_id)
        await connection_manager.disconnect(connection_id)
    except Exception as e:
        logger.error("WebSocket错误: %s, 错误: %s", connection_id, e)
        await connection_manager.disconnect(connection_id)


# ==================== REST API端点 ====================


@router.get("/projects/{project_id}/sections/{section_id}/comments", dependencies=[Depends(get_current_user)])
async def get_comments(
    project_id: int,
    section_id: int,
):
    """获取章节评论列表"""
    comments = await CommentService.get_comments(project_id, section_id)
    return {
        "status": "ok",
        "count": len(comments),
        "comments": comments,
    }


@router.post("/projects/{project_id}/sections/{section_id}/comments", dependencies=[Depends(get_current_user)])
async def add_comment(
    project_id: int,
    section_id: int,
    request: Request,
    content: str = Query(..., min_length=1),
    user_id: str = Query("anonymous"),
    position: int = None,
    parent_id: int = None,
):
    """添加评论"""
    user_id = _trusted_user_id(request, user_id)
    result = await CommentService.add_comment(
        project_id,
        section_id,
        user_id,
        content,
        position,
        parent_id,
    )
    return result


@router.post("/projects/{project_id}/sections/{section_id}/comments/{comment_id}/resolve", dependencies=[Depends(get_current_user)])
async def resolve_comment(
    comment_id: int,
    request: Request,
    user_id: str = Query("anonymous"),
):
    """标记评论为已解决"""
    user_id = _trusted_user_id(request, user_id)
    result = await CommentService.resolve_comment(comment_id, user_id)
    return result


@router.get("/projects/{project_id}/sections/{section_id}/versions", dependencies=[Depends(get_current_user)])
async def get_versions(section_id: int):
    """获取章节版本历史"""
    versions = await VersionCompareService.get_versions(section_id)
    return {
        "status": "ok",
        "count": len(versions),
        "versions": versions,
    }


@router.post("/projects/{project_id}/sections/{section_id}/versions", dependencies=[Depends(get_current_user)])
async def create_version(
    project_id: int,
    section_id: int,
    request: Request,
    user_id: str = Query("anonymous"),
    label: str = Query(""),
):
    """创建内容版本"""
    user_id = _trusted_user_id(request, user_id)
    result = await VersionCompareService.create_version(
        project_id,
        section_id,
        user_id,
        label,
    )
    return result


@router.get("/projects/{project_id}/sections/{section_id}/compare", dependencies=[Depends(get_current_user)])
async def compare_versions(
    section_id: int,
    v1: int = Query(..., alias="v1", description="第一个版本ID"),
    v2: int = Query(..., alias="v2", description="第二个版本ID"),
):
    """比较两个版本"""
    result = await VersionCompareService.compare_versions(section_id, v1, v2)
    return result


@router.get("/ws/stats", dependencies=[Depends(get_current_user)])
async def get_ws_stats():
    """获取WebSocket连接统计"""
    stats = connection_manager.get_stats()
    return {
        "status": "ok",
        "websocket": stats,
    }
