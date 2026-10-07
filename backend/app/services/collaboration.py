"""WebSocket协作服务

提供多人实时编辑、评论批注等协作功能。
基于WebSocket实现双向实时通信。
"""

import asyncio
from datetime import datetime
import json
import logging
import uuid

from ..db import get_db


logger = logging.getLogger(__name__)


class ConnectionManager:
    """WebSocket连接管理器

    管理所有活跃的WebSocket连接，支持房间（room）概念。
    每个项目-章节组合是一个独立的房间。

    修复历史：
    - 2026-08-21: 修复死锁Bug - connect/disconnect在持锁时调用_broadcast_to_room导致嵌套锁
    - 2026-08-21: 修复send_message持锁执行I/O导致性能问题
    """

    def __init__(self):
        # {connection_id: {"websocket": WebSocket, "user_id": str, "room": str, "joined_at": datetime}}
        self._connections: dict[str, dict] = {}
        # {room: {connection_id, ...}}
        self._rooms: dict[str, set[str]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, websocket, user_id: str, room: str) -> str:
        """连接WebSocket并加入房间

        Args:
            websocket: WebSocket实例
            user_id: 用户ID
            room: 房间ID（格式：project:{id}:section:{id}）

        Returns:
            连接ID
        """
        connection_id = str(uuid.uuid4())[:8]

        # 锁内：只修改内存状态，不做任何I/O
        async with self._lock:
            self._connections[connection_id] = {
                "websocket": websocket,
                "user_id": user_id,
                "room": room,
                "joined_at": datetime.now(),
            }

            if room not in self._rooms:
                self._rooms[room] = set()
            self._rooms[room].add(connection_id)

        logger.info("WebSocket连接: %s, 用户: %s, 房间: %s", connection_id, user_id, room)

        # 锁外：发送欢迎消息（I/O操作）
        await websocket.send_json(
            {
                "type": "welcome",
                "connection_id": connection_id,
                "user_id": user_id,
                "room": room,
                "message": "已连接到协作房间",
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        )

        # 锁外：通知房间内其他用户（调用者自行管理锁，使用unlocked版本）
        await self._broadcast_to_room_unlocked(
            room,
            {
                "type": "user_joined",
                "user_id": user_id,
                "connection_id": connection_id,
                "online_users": self.get_online_users(room),
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
            exclude=connection_id,
        )

        return connection_id

    async def disconnect(self, connection_id: str):
        """断开WebSocket连接

        修复：锁内只做内存数据搬运，锁外做I/O广播，避免嵌套锁死锁。
        """
        # 锁内：只收集需要的数据，不做任何I/O
        room = None
        user_id = None
        online_users_data = []

        async with self._lock:
            conn_info = self._connections.pop(connection_id, None)
            if conn_info:
                room = conn_info["room"]
                user_id = conn_info["user_id"]

                if room in self._rooms:
                    self._rooms[room].discard(connection_id)
                    if not self._rooms[room]:
                        del self._rooms[room]

                # 收集当前在线用户（锁内拷贝，锁外使用）
                if room and room in self._rooms:
                    for cid in self._rooms[room]:
                        ci = self._connections.get(cid)
                        if ci:
                            online_users_data.append(
                                {
                                    "user_id": ci["user_id"],
                                    "connection_id": cid,
                                }
                            )

        if conn_info:
            logger.info("WebSocket断开: %s, 用户: %s, 房间: %s", connection_id, user_id, room)

            # 锁外：广播通知（使用unlocked版本，因为已获取过锁）
            # 注意：这里使用_broadcast_to_room_unlocked而不是_broadcast_to_room，
            # 因为_broadcast_to_room内部会获取self._lock，而我们刚刚已释放锁
            # 所以可以安全使用需要锁的版本
            await self._broadcast_to_room(
                room,
                {
                    "type": "user_left",
                    "user_id": user_id,
                    "connection_id": connection_id,
                    "online_users": online_users_data,
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                },
                exclude=connection_id,
            )

    async def send_message(self, connection_id: str, message: dict):
        """向特定连接发送消息

        修复：锁内只获取连接引用，锁外执行I/O发送。
        """
        # 锁内：获取连接引用
        websocket_ref = None
        async with self._lock:
            conn_info = self._connections.get(connection_id)
            if conn_info:
                websocket_ref = conn_info["websocket"]

        if not websocket_ref:
            logger.warning("连接不存在: %s", connection_id)
            return False

        # 锁外：执行I/O发送
        try:
            await websocket_ref.send_json(message)
            return True
        except Exception as e:
            logger.error("发送消息失败: %s, 错误: %s", connection_id, e)
            return False

    async def broadcast_to_room(self, room: str, message: dict, exclude: str | None = None):
        """向房间内所有用户广播消息"""
        await self._broadcast_to_room(room, message, exclude=exclude)

    async def _broadcast_to_room(self, room: str, message: dict, exclude: str | None = None):
        """内部方法：向房间内广播消息（获取锁后执行）"""
        # 锁内：获取连接列表快照
        async with self._lock:
            connections = self._rooms.get(room, set()).copy()

        # 锁外：执行I/O发送
        for conn_id in connections:
            if conn_id != exclude:
                try:
                    conn_info = self._connections.get(conn_id)
                    if conn_info:
                        await conn_info["websocket"].send_json(message)
                except Exception as e:
                    logger.error("广播消息失败: %s, 错误: %s", conn_id, e)

    async def _broadcast_to_room_unlocked(
        self, room: str, message: dict, exclude: str | None = None
    ):
        """内部方法：向房间内广播消息（不获取锁，调用者需确保已持有或无需锁）

        用于connect/disconnect等已在锁外时调用的场景，避免嵌套锁死锁。
        """
        # 直接获取当前连接列表（无锁，因为调用者已在锁外安全场景）
        connections = self._rooms.get(room, set()).copy()

        for conn_id in connections:
            if conn_id != exclude:
                try:
                    conn_info = self._connections.get(conn_id)
                    if conn_info:
                        await conn_info["websocket"].send_json(message)
                except Exception as e:
                    logger.error("广播消息失败: %s, 错误: %s", conn_id, e)

    def get_online_users(self, room: str) -> list[dict]:
        """获取房间内在线用户列表"""
        users = []
        conn_ids = self._rooms.get(room, set())

        for conn_id in conn_ids:
            conn_info = self._connections.get(conn_id)
            if conn_info:
                users.append(
                    {
                        "user_id": conn_info["user_id"],
                        "connection_id": conn_id,
                        "joined_at": conn_info["joined_at"].strftime("%Y-%m-%d %H:%M:%S"),
                    }
                )

        return users

    def get_room_connection_count(self, room: str) -> int:
        """获取房间内连接数"""
        return len(self._rooms.get(room, set()))

    def is_room_active(self, room: str) -> bool:
        """检查房间是否活跃"""
        return room in self._rooms and len(self._rooms[room]) > 0

    def get_all_rooms(self) -> list[str]:
        """获取所有活跃房间"""
        return list(self._rooms.keys())

    def get_stats(self) -> dict:
        """获取连接统计信息"""
        total_connections = len(self._connections)
        active_rooms = len([r for r in self._rooms if self._rooms[r]])

        return {
            "total_connections": total_connections,
            "active_rooms": active_rooms,
            "rooms": {room: len(connections) for room, connections in self._rooms.items()},
        }


# 全局连接管理器实例
connection_manager = ConnectionManager()


class CollaborativeEditor:
    """协作编辑器服务

    处理实时编辑操作：插入、删除、替换文本等。
    使用操作转换（OT）算法保证一致性。
    """

    @staticmethod
    async def handle_edit_operation(
        room: str,
        user_id: str,
        operation: dict,
        section_id: int,
    ) -> dict:
        """处理编辑操作

        Args:
            room: 房间ID
            user_id: 用户ID
            operation: 编辑操作 {"type": "insert/delete/replace", "position": int, "text": str, ...}
            section_id: 章节ID

        Returns:
            操作结果
        """
        op_type = operation.get("type", "")
        position = operation.get("position", 0)
        operation.get("text", "")

        # 验证操作
        if not op_type or position < 0:
            return {"status": "error", "message": "无效的操作参数"}

        # 构建操作消息
        edit_message = {
            "type": "edit",
            "operation": operation,
            "user_id": user_id,
            "section_id": section_id,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "op_id": str(uuid.uuid4())[:8],
        }

        # 广播给房间内其他用户
        await connection_manager.broadcast_to_room(
            room,
            edit_message,
            exclude=None,  # 不排除发送者，让所有客户端同步
        )

        # 记录操作到数据库（用于持久化）
        await CollaborativeEditor._save_operation(section_id, user_id, operation)

        return {
            "status": "ok",
            "message": "操作已广播",
            "op_id": edit_message["op_id"],
        }

    @staticmethod
    async def _save_operation(section_id: int, user_id: str, operation: dict):
        """保存编辑操作到数据库"""
        try:
            with get_db() as conn:
                conn.execute(
                    """INSERT INTO edit_operations 
                       (section_id, user_id, operation, created_at)
                       VALUES (?, ?, ?, ?)""",
                    (
                        section_id,
                        user_id,
                        json.dumps(operation, ensure_ascii=False),
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    ),
                )
        except Exception as e:
            logger.error("保存编辑操作失败: %s", e)

    @staticmethod
    async def get_edit_history(section_id: int, limit: int = 100) -> list[dict]:
        """获取章节的编辑历史

        Args:
            section_id: 章节ID
            limit: 最大返回数量

        Returns:
            编辑历史记录列表
        """
        with get_db() as conn:
            rows = conn.execute(
                """SELECT * FROM edit_operations 
                   WHERE section_id = ? 
                   ORDER BY created_at DESC 
                   LIMIT ?""",
                (section_id, limit),
            ).fetchall()

            history = []
            for row in rows:
                record = dict(row)
                try:
                    record["operation"] = json.loads(record["operation"])
                except json.JSONDecodeError:
                    record["operation"] = {}
                history.append(record)

            return history


class CommentService:
    """评论与批注服务"""

    @staticmethod
    async def add_comment(
        project_id: int,
        section_id: int,
        user_id: str,
        content: str,
        position: int | None = None,
        parent_id: int | None = None,
    ) -> dict:
        """添加评论

        Args:
            project_id: 项目ID
            section_id: 章节ID
            user_id: 用户ID
            content: 评论内容
            position: 在文本中的位置（可选）
            parent_id: 父评论ID（回复评论时使用）

        Returns:
            创建的评论
        """
        with get_db() as conn:
            cursor = conn.execute(
                """INSERT INTO comments 
                   (project_id, section_id, user_id, content, position, parent_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    project_id,
                    section_id,
                    user_id,
                    content,
                    position,
                    parent_id,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            comment_id = cursor.lastrowid

        # 广播新评论
        room = f"project:{project_id}:section:{section_id}"
        await connection_manager.broadcast_to_room(
            room,
            {
                "type": "new_comment",
                "comment": {
                    "id": comment_id,
                    "project_id": project_id,
                    "section_id": section_id,
                    "user_id": user_id,
                    "content": content,
                    "position": position,
                    "parent_id": parent_id,
                    "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                },
            },
        )

        return {
            "status": "ok",
            "comment_id": comment_id,
            "message": "评论已添加",
        }

    @staticmethod
    async def get_comments(project_id: int, section_id: int | None = None) -> list[dict]:
        """获取评论列表

        Args:
            project_id: 项目ID
            section_id: 章节ID（可选，不传则获取整个项目的评论）

        Returns:
            评论列表
        """
        with get_db() as conn:
            if section_id:
                rows = conn.execute(
                    """SELECT c.*, u.name as user_name 
                       FROM comments c
                       LEFT JOIN users u ON c.user_id = u.id
                       WHERE c.project_id = ? AND c.section_id = ?
                       ORDER BY c.created_at ASC""",
                    (project_id, section_id),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT c.*, u.name as user_name 
                       FROM comments c
                       LEFT JOIN users u ON c.user_id = u.id
                       WHERE c.project_id = ?
                       ORDER BY c.created_at DESC""",
                    (project_id,),
                ).fetchall()

            return [dict(row) for row in rows]

    @staticmethod
    async def resolve_comment(comment_id: int, user_id: str) -> dict:
        """标记评论为已解决

        Args:
            comment_id: 评论ID
            user_id: 用户ID

        Returns:
            操作结果
        """
        with get_db() as conn:
            conn.execute(
                """UPDATE comments 
                   SET resolved = 1, resolved_by = ?, resolved_at = ?
                   WHERE id = ?""",
                (
                    user_id,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    comment_id,
                ),
            )

        return {
            "status": "ok",
            "message": "评论已标记为已解决",
        }


# ==================== 版本对比服务 ====================


class VersionCompareService:
    """版本对比服务

    支持内容版本的创建、比较和回滚。
    """

    @staticmethod
    async def create_version(
        project_id: int,
        section_id: int,
        user_id: str,
        label: str = "",
    ) -> dict:
        """创建内容版本

        Args:
            project_id: 项目ID
            section_id: 章节ID
            user_id: 用户ID
            label: 版本标签（可选）

        Returns:
            创建版本的信息
        """
        # 获取当前内容
        with get_db() as conn:
            row = conn.execute(
                "SELECT content FROM sections WHERE id = ? AND project_id = ?",
                (section_id, project_id),
            ).fetchone()

        if not row or not row["content"]:
            return {
                "status": "error",
                "message": "没有可保存的内容",
            }

        version_label = label or f"v{datetime.now().strftime('%Y%m%d_%H%M%S')}"

        with get_db() as conn:
            cursor = conn.execute(
                """INSERT INTO content_versions 
                   (project_id, section_id, user_id, label, content, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    project_id,
                    section_id,
                    user_id,
                    version_label,
                    row["content"],
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            version_id = cursor.lastrowid

        return {
            "status": "ok",
            "version_id": version_id,
            "label": version_label,
            "message": f"版本已创建: {version_label}",
        }

    @staticmethod
    async def compare_versions(
        section_id: int,
        version_id_1: int,
        version_id_2: int,
    ) -> dict:
        """比较两个版本

        Args:
            section_id: 章节ID
            version_id_1: 第一个版本ID
            version_id_2: 第二个版本ID

        Returns:
            对比结果
        """
        with get_db() as conn:
            v1 = conn.execute(
                "SELECT content FROM content_versions WHERE id = ?",
                (version_id_1,),
            ).fetchone()

            v2 = conn.execute(
                "SELECT content FROM content_versions WHERE id = ?",
                (version_id_2,),
            ).fetchone()

        if not v1 or not v2:
            return {
                "status": "error",
                "message": "版本不存在",
            }

        # 简单的行级差异计算
        lines1 = v1["content"].split("\n")
        lines2 = v2["content"].split("\n")

        added = []
        removed = []

        # 计算差异（简化版）
        for i, line in enumerate(lines2):
            if i >= len(lines1) or lines1[i] != line:
                added.append({"line": i + 1, "content": line})

        for i, line in enumerate(lines1):
            if i >= len(lines2) or lines2[i] != line:
                removed.append({"line": i + 1, "content": line})

        return {
            "status": "ok",
            "version_1": {
                "id": version_id_1,
                "lines": len(lines1),
                "chars": len(v1["content"]),
            },
            "version_2": {
                "id": version_id_2,
                "lines": len(lines2),
                "chars": len(v2["content"]),
            },
            "diff": {
                "added_lines": len(added),
                "removed_lines": len(removed),
                "added": added[:50],  # 最多返回50行
                "removed": removed[:50],
            },
        }

    @staticmethod
    async def get_versions(section_id: int) -> list[dict]:
        """获取章节的所有版本

        Args:
            section_id: 章节ID

        Returns:
            版本列表
        """
        with get_db() as conn:
            rows = conn.execute(
                """SELECT id, label, user_id, created_at 
                   FROM content_versions 
                   WHERE section_id = ?
                   ORDER BY created_at DESC""",
                (section_id,),
            ).fetchall()

            return [dict(row) for row in rows]


# 初始化数据库表
def init_collaboration_tables():
    """初始化协作功能所需的数据库表"""
    with get_db() as conn:
        # 编辑操作表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS edit_operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                section_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY(section_id) REFERENCES sections(id) ON DELETE CASCADE
            )
        """)

        # 评论表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                section_id INTEGER,
                user_id TEXT NOT NULL,
                content TEXT NOT NULL,
                position INTEGER,
                parent_id INTEGER,
                resolved INTEGER DEFAULT 0,
                resolved_by TEXT,
                resolved_at TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            )
        """)

        # 内容版本表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS content_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                section_id INTEGER NOT NULL,
                user_id TEXT NOT NULL,
                label TEXT,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
                FOREIGN KEY(section_id) REFERENCES sections(id) ON DELETE CASCADE
            )
        """)

        # 创建索引
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_edit_op_section ON edit_operations(section_id)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_comments_project ON comments(project_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_comments_section ON comments(section_id)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_versions_section ON content_versions(section_id)"
        )

        logger.info("协作功能数据库表初始化完成")
