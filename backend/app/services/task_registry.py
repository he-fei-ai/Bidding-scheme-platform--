"""后台任务注册表

管理运行中的后台任务，使其在 SSE 连接断开后继续执行。
支持多客户端订阅同一任务的进度更新。

核心能力：
1. 任务与 SSE 解耦：任务在后台独立运行，SSE 仅是进度通知渠道
2. 断线重连：新的 SSE 连接可订阅正在运行的任务，获取当前进度
3. 任务状态持久化：支持跨请求查询任务状态
4. 订阅管理：多个 SSE 可同时订阅同一任务
"""

import asyncio
from dataclasses import dataclass, field
from enum import Enum
import logging
import time
from typing import Optional


logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class TaskInfo:
    """任务信息"""

    task_id: str
    project_id: int
    task_type: str  # "outline" / "content" / "export" / "file_parse"
    status: TaskStatus = TaskStatus.PENDING
    progress: float = 0.0  # 0-100
    current_step: int = 0
    total_steps: int = 0
    message: str = ""
    started_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    error_message: str = ""
    # 最后一个事件快照，用于重连时发送当前状态
    last_event: dict | None = None
    # 订阅者集合（SSE 队列引用）
    _subscribers: list[asyncio.Queue] = field(default_factory=list)
    # 后台任务引用
    _task: asyncio.Task | None = None
    # 锁保护并发修改
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class TaskRegistry:
    """后台任务注册表（单例）

    使用方式：
        registry = TaskRegistry()

        # 启动任务
        task_info = registry.create_task("outline", project_id)
        task_info._task = asyncio.create_task(run_task())

        # 订阅进度（SSE 连接）
        queue = asyncio.Queue()
        registry.subscribe(task_id, queue)
        # 从队列读取事件...

        # 取消订阅（SSE 断开）
        registry.unsubscribe(task_id, queue)

        # 查询任务状态
        state = registry.get_task_state(task_id)

        # 完成任务
        registry.complete_task(task_id, status="completed", result={...})
    """

    _instance: Optional["TaskRegistry"] = None

    def __new__(cls) -> "TaskRegistry":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        self._tasks: dict[str, TaskInfo] = {}
        self._project_index: dict[int, dict[str, str]] = {}  # project_id -> {task_type -> task_id}
        self._global_lock = asyncio.Lock()
        # BUG-FIX 2026-09-16（低）：延迟清理的 fire-and-forget 任务需要强引用，
        # 否则可能被 GC 中途回收（CPython 已知陷阱），任务记录泄漏在内存里
        self._cleanup_tasks: set[asyncio.Task] = set()

    async def create_task(
        self,
        task_type: str,
        project_id: int,
        task_id: str | None = None,
        total_steps: int = 0,
    ) -> TaskInfo:
        """创建或获取任务

        如果同一项目+类型已有运行中任务，返回现有任务（用于重连）。

        Args:
            task_type: 任务类型 ("outline"/"content"/"export")
            project_id: 项目 ID
            task_id: 可选，指定任务 ID
            total_steps: 总步骤数

        Returns:
            TaskInfo 实例
        """
        async with self._global_lock:
            # 检查是否已有运行中的任务（支持重连）
            if project_id in self._project_index:
                existing_id = self._project_index[project_id].get(task_type)
                if existing_id and existing_id in self._tasks:
                    existing = self._tasks[existing_id]
                    if existing.status in (
                        TaskStatus.RUNNING,
                        TaskStatus.PENDING,
                        TaskStatus.PAUSED,
                    ):
                        logger.info(
                            "[TaskRegistry] 重连到已有任务: %s project=%d type=%s",
                            existing_id,
                            project_id,
                            task_type,
                        )
                        return existing

            # 创建新任务
            import uuid

            tid = task_id or str(uuid.uuid4())
            task = TaskInfo(
                task_id=tid,
                project_id=project_id,
                task_type=task_type,
                total_steps=total_steps,
                status=TaskStatus.PENDING,
            )
            self._tasks[tid] = task
            self._project_index.setdefault(project_id, {})[task_type] = tid
            logger.info(
                "[TaskRegistry] 创建任务: %s project=%d type=%s", tid, project_id, task_type
            )
            return task

    async def subscribe(self, task_id: str, queue: asyncio.Queue) -> TaskInfo:
        """订阅任务进度

        新的 SSE 连接调用此方法，将队列加入订阅者列表。
        订阅后会立即发送当前状态快照，让新连接同步进度。

        Args:
            task_id: 任务 ID
            queue: SSE 事件队列

        Returns:
            TaskInfo 当前状态
        """
        async with self._global_lock:
            task = self._tasks.get(task_id)
            if not task:
                raise KeyError(f"任务不存在: {task_id}")
            task._subscribers.append(queue)
            logger.info("[TaskRegistry] 订阅任务: %s 订阅者数=%d", task_id, len(task._subscribers))
            return task

    async def unsubscribe(self, task_id: str, queue: asyncio.Queue) -> None:
        """取消订阅（SSE 断开时调用）"""
        async with self._global_lock:
            task = self._tasks.get(task_id)
            if task and queue in task._subscribers:
                task._subscribers.remove(queue)
                logger.info(
                    "[TaskRegistry] 取消订阅: %s 订阅者数=%d", task_id, len(task._subscribers)
                )

    async def broadcast_event(self, task_id: str, event: dict) -> None:
        """广播事件到所有订阅者

        Args:
            task_id: 任务 ID
            event: 事件数据
        """
        async with self._global_lock:
            task = self._tasks.get(task_id)
            if not task:
                return
            # 更新最后事件快照
            task.last_event = event
            # 广播到所有订阅队列（非阻塞，单个订阅者处理失败不影响其他）
            dead_queues = []
            for q in task._subscribers:
                try:
                    q.put_nowait(event)
                except Exception:
                    dead_queues.append(q)
            # 清理死掉的队列
            for q in dead_queues:
                task._subscribers.remove(q)

    async def update_progress(
        self,
        task_id: str,
        progress: float | None = None,
        step: int | None = None,
        message: str | None = None,
        status: TaskStatus | None = None,
    ) -> None:
        """更新任务进度

        Args:
            task_id: 任务 ID
            progress: 进度百分比 (0-100)
            step: 当前步骤
            message: 状态消息
            status: 任务状态
        """
        async with self._global_lock:
            task = self._tasks.get(task_id)
            if not task:
                return
            if progress is not None:
                task.progress = progress
            if step is not None:
                task.current_step = step
            if message is not None:
                task.message = message
            if status is not None:
                task.status = status

    async def complete_task(
        self,
        task_id: str,
        status: TaskStatus = TaskStatus.COMPLETED,
        result: dict | None = None,
        error_message: str = "",
    ) -> None:
        """完成任务

        Args:
            task_id: 任务 ID
            status: 最终状态
            result: 结果数据
            error_message: 错误信息
        """
        async with self._global_lock:
            task = self._tasks.get(task_id)
            if not task:
                return
            task.status = status
            task.completed_at = time.time()
            task.error_message = error_message
            if status == TaskStatus.COMPLETED:
                task.progress = 100.0
            logger.info("[TaskRegistry] 任务完成: %s status=%s", task_id, status.value)

        # 发送完成事件
        event = {
            "status": status.value,
            "task_id": task_id,
            "message": task.message,
        }
        if result:
            event["result"] = result
        if error_message:
            event["error"] = error_message
        await self.broadcast_event(task_id, event)

        # 清理任务（延迟清理，给订阅者时间接收完成事件）
        async def _cleanup():
            await asyncio.sleep(30)
            async with self._global_lock:
                if task_id in self._tasks:
                    del self._tasks[task_id]
                    # 清理项目索引
                    for pid, types in list(self._project_index.items()):
                        for ttype, tid in list(types.items()):
                            if tid == task_id:
                                del types[ttype]
                        if not types:
                            del self._project_index[pid]

        _cleanup_task = asyncio.create_task(_cleanup())
        self._cleanup_tasks.add(_cleanup_task)
        _cleanup_task.add_done_callback(self._cleanup_tasks.discard)

    async def get_task_state(self, task_id: str) -> dict | None:
        """获取任务当前状态（用于重连时发送快照）"""
        async with self._global_lock:
            task = self._tasks.get(task_id)
            if not task:
                return None
            return {
                "task_id": task.task_id,
                "project_id": task.project_id,
                "task_type": task.task_type,
                "status": task.status.value,
                "progress": task.progress,
                "current_step": task.current_step,
                "total_steps": task.total_steps,
                "message": task.message,
                "last_event": task.last_event,
            }

    def get_project_tasks(self, project_id: int) -> dict[str, "TaskInfo"]:
        """获取指定项目的所有任务（同步方法，用于状态查询接口）

        Args:
            project_id: 项目 ID

        Returns:
            task_id -> TaskInfo 的映射
        """
        result = {}
        types_map = self._project_index.get(project_id, {})
        for task_type, task_id in types_map.items():
            task = self._tasks.get(task_id)
            if task:
                result[task_id] = task
        return result

    async def get_active_tasks(self, project_id: int | None = None) -> list[dict]:
        """获取活跃任务列表

        修复：避免在持有锁时调用 get_task_state（会再次获取锁导致死锁）。
        改为在锁内收集任务ID，锁外逐个查询状态。

        Args:
            project_id: 可选，筛选指定项目的任务

        Returns:
            任务状态列表
        """
        # 锁内：只收集任务ID列表
        task_ids = []
        async with self._global_lock:
            for task_id, task in self._tasks.items():
                if task.status in (TaskStatus.RUNNING, TaskStatus.PENDING, TaskStatus.PAUSED):
                    if project_id is None or task.project_id == project_id:
                        task_ids.append(task_id)

        # 锁外：逐个获取状态（get_task_state 会获取锁，但此时无嵌套问题）
        tasks = []
        for tid in task_ids:
            state = await self.get_task_state(tid)
            if state:
                tasks.append(state)
        return tasks

    async def cancel_task(self, task_id: str, reason: str = "用户取消") -> None:
        """取消任务"""
        async with self._global_lock:
            task = self._tasks.get(task_id)
            if not task:
                return
            if task._task and not task._task.done():
                task._task.cancel()
            task.status = TaskStatus.CANCELLED
            task.completed_at = time.time()
            task.error_message = reason

        await self.broadcast_event(
            task_id,
            {
                "status": "cancelled",
                "message": reason,
            },
        )

        # 清理
        # BUG-FIX 2026-09-18（低）：原实现只删 `_tasks`，**漏删 `_project_index`**
        # —— 索引里永远留着 `project_id -> {task_type: 已删除的 task_id}`。
        # 后果：`get_project_tasks()` 每次都会按索引去 `_tasks` 里查一个不存在的 id
        # 并静默跳过（表现为"刚取消的任务在列表里消失了，但项目索引一直膨胀"）；
        # 且 `complete_task` 的延迟清理靠比对索引值来定位条目，残留值会让该清理
        # 分支多跑一轮无意义的全表扫描。这里与 `_tasks` 同步清理。
        async with self._global_lock:
            if task_id in self._tasks:
                del self._tasks[task_id]
            for _pid, types in list(self._project_index.items()):
                for _ttype, _tid in list(types.items()):
                    if _tid == task_id:
                        del types[_ttype]
                if not types:
                    del self._project_index[_pid]


# 全局单例
task_registry = TaskRegistry()
