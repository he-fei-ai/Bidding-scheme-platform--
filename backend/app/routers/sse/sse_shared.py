"""sse_handlers 基础设施（logger / SSE 超时常量 / 队列）

本文件仅含跨 SSE 处理器共享的底层定义，不做业务逻辑。
"""
import asyncio
import logging
import os
from typing import Any

# SSE 总超时（秒）—— 目录生成 V5.1 三级流水线对超大型标书可能耗时较长。
#
# 历史值 1800s（30 分钟）对大项目偏紧：后台 _run_outline_task 与 SSE 显示解耦
# （任务在 task_registry 中独立运行，SSE 断开不影响其继续生成），30 分钟时仅仅
# 代表「SSE 显示连接已达上限」，任务本身仍在正常跑，却被前端误报成
# "生成超过30分钟，已自动终止" 的致命错误（BUG：本仓库此前的超时分支会发送
# status=error 并声称已终止，但实际后台任务从未被取消，仍在占用任务组锁并继续
# 生成，用户既看不到进度也拿不到结果）。
#
# 修复策略：
#   1) 放宽默认到 3600s（60 分钟）作为真正的防死循环安全网，并可通过环境变量
#      SSE_TOTAL_TIMEOUT_OUTLINE 调整；
#   2) 超时不再致命终止：sse_outline.py 改为发送非终态 `sse_timeout`，前端据此
#      自动重连并复用运行中的后台任务（task_registry 重连机制），直至任务真正完成。
SSE_TOTAL_TIMEOUT_DEFAULT = int(os.environ.get("SSE_TOTAL_TIMEOUT_OUTLINE", "3600"))  # 60分钟（可配置）

# BUG-FIX 2026-10-09（跨模块协议统一）：SSE 终态集合此前在两处各写一份字面量，
# 且**目录流少了 "cancelled"**：
#   · sse_outline.py  ("completed", "error", "stopped")
#   · sse_content.py  ("completed", "error", "stopped")
# 而前端 OutlineEditorPage.tsx:299 与 SectionEditorPage.tsx:727 都实现了
# `status === "cancelled"` 分支 —— 前端认它、后端不认它：一旦上游推来 cancelled，
# 目录流的消费循环不会 break，连接会一直挂到 1800s 总超时才释放。
# 现收敛为唯一一份定义，两条流共用；新增终态只需改这里。
SSE_TERMINAL_STATUSES: frozenset[str] = frozenset(
    {"completed", "error", "stopped", "cancelled"}
)

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
