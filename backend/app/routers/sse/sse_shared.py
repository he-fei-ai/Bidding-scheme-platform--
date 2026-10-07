"""sse_handlers 基础设施（logger / SSE 超时常量 / 队列）

本文件仅含跨 SSE 处理器共享的底层定义，不做业务逻辑。
"""
import asyncio
import logging
from typing import Any

# SSE 超时本地常量（秒）—— 目录生成 V5.1 三级流水线需更长时间
SSE_TOTAL_TIMEOUT_DEFAULT = 1800  # 30分钟

logger = logging.getLogger(__name__)

# 保存后台任务引用，防止被 GC 回收
_background_tasks: set = set()


class _NotifyQueue(asyncio.Queue):

    """入队自动唤醒消费者的队列



    背景：目录生成主循环用 asyncio.Event 等待新事件（避免轮询延迟）。

    但 workflows.py 里的 _wait_for_v51_review 等函数只持有 outline_queue 引用，

    直接 outline_queue.put(...) 无法触发 _queue_has_event.set()，

    导致 review_needed 等关键事件滞留队列、主循环只发心跳、前端收不到审核弹窗。

    本类在 put() 时自动 set 关联的 Event，彻底消除该缺陷。

    """



    def __init__(self, *args, **kwargs):

        super().__init__(*args, **kwargs)

        self._notify_event = asyncio.Event()



    def get_notify_event(self) -> asyncio.Event:

        return self._notify_event



    async def put(self, item: Any) -> None:

        await super().put(item)

        self._notify_event.set()
