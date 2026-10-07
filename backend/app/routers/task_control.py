# Auto-generated Task Control - do not edit manually

from __future__ import annotations

import asyncio
import logging


logger = logging.getLogger(__name__)

_cancel_events: dict[int, list[asyncio.Event]] = {}
_cancel_tasks: dict[int, list[asyncio.Task]] = {}
_cancel_events_lock = asyncio.Lock()


# stop_generation
async def stop_generation(project_id: int, mark_failed: bool = True):
    """停止当前正在进行的正文生成

    执行步骤：
    1. 发送取消信号给所有活跃的 SSE 连接
    2. 取消正在运行的 asyncio.Task
    3. 更新数据库中所有章节状态为 failed（仅当 mark_failed=True 时执行）
    4. 清理内存中的 cancel_events 和 cancel_tasks
    5. 关键修复（P1-7）：通过 generation_manager.finalize_task 清理内存 task，
       并将状态修正为 cancelled（GenerationTask 状态机允许值）：
       running / paused / completed / cancelled / failed

    Args:
        project_id: 项目 ID
        mark_failed: 是否将章节状态标记为 failed。
            pause_generation 调用时应传 False，避免章节被改为 failed 后
            无法再被 WHERE status='generating' 匹配到。
    """
    from ..db import get_db  # BUG O6 修复：添加对 get_db 的导入，避免 NameError
    from ..services.generation_manager import generation_manager

    # 第1+2步：设置 cancel_event + 取消 asyncio.Task
    # 关键修复：合并到一次加锁，避免两次单独加锁之间的 TOCTOU 竞态
    tasks_to_wait: list[asyncio.Task] = []
    async with _cancel_events_lock:
        # 设置 cancel_event，通知生成逻辑停止
        events = _cancel_events.get(project_id)
        if events:
            events_copy = list(events)
            for ev in events_copy:
                ev.set()
            # 清理 cancel_events
            del _cancel_events[project_id]

        # 取消正在运行的 asyncio.Task
        tasks = _cancel_tasks.get(project_id)
        if tasks:
            tasks_to_wait = list(tasks)  # 在 del 之前捕获引用
            for t in tasks:
                if not t.done():
                    t.cancel()
                    logger.info("已发送取消信号到任务: %s", t)
            # 清理 cancel_tasks
            del _cancel_tasks[project_id]

    # 等待被取消的任务实际完成（替代固定的 asyncio.sleep(0.5)）
    if tasks_to_wait:
        await asyncio.gather(*tasks_to_wait, return_exceptions=True)

    # 第3步：更新数据库中所有章节状态为 failed（因为生成被中断）
    # 关键修复：仅当 mark_failed=True 时执行；pause_generation 调用时传 False，
    # 保留章节为 generating 状态，以便后续 pause_generation 的 UPDATE WHERE status='generating' 能匹配到
    if mark_failed:
        try:
            # BUG-FIX 2026-09-16（中低）：同步 UPDATE 移出事件循环线程。
            # 停止/暂停是高频操作路径，WAL 写锁竞争时（如杀毒扫描锁库）
            # 原实现在事件循环线程上同步等锁，会冻结所有并发 SSE 流。
            def _mark_sections_failed() -> None:
                with get_db() as conn:
                    conn.execute(
                        "UPDATE sections SET status = 'failed', updated_at = datetime('now', 'localtime') WHERE project_id = ? AND status = 'generating'",
                        (project_id,),
                    )

            await asyncio.to_thread(_mark_sections_failed)
        except Exception as e:
            logger.error("更新章节状态失败: %s", e)

    # 第4步：更新 GenerationManager 状态并清理内存 task
    # 关键修复（P1-7）：
    # - 原代码设置 task.status = "stopped"，但 GenerationTask 状态机不允许该值
    #   （只允许 running/paused/completed/cancelled/failed），导致
    #   cleanup_completed_tasks 永远无法清理该记录，has_active_task 永远返回 True
    # - 原代码访问 task.stopped_at 字段，但 GenerationTask 没有这个属性，会抛 AttributeError
    # - 原代码没有从 generation_manager._tasks 中删除 task，导致内存泄漏
    # 正确做法：直接调用 finalize_task 走统一收尾逻辑
    try:
        # 关键修复：使用 cancelled 状态（GenerationTask 状态机允许值之一）
        # pause_generation 传 mark_failed=False 时不清理，由 pause_task 处理
        if mark_failed:
            await generation_manager.finalize_task(
                project_id, task_status="cancelled", error_message="用户主动停止生成"
            )
        else:
            # pause 路径下，stop_generation 已经被 pause_generation 包装
            # 这里只需要确保 _cancel_events/_cancel_tasks 已清理，不再触碰 generation_manager
            pass
    except Exception as e:
        logger.warning("清理 generation_manager 任务失败: %s", e)

    return {"status": "ok", "message": "生成已停止"}


# pause_generation
async def pause_generation(project_id: int):
    """暂停当前正在进行的正文生成

    与 stop_generation 的区别：
    - stop_generation: 完全终止，章节状态改为 failed，任务状态改为 cancelled
    - pause_generation: 暂停，章节保留 generating 状态，任务状态改为 paused，
      后续可通过 resume_generation 恢复断点续生

    实现策略：调用 stop_generation(mark_failed=False) 停止 SSE 流和 AI 调用，
    然后通过 generation_manager.pause_task() 将任务标记为 paused。
    """
    from ..services.generation_manager import generation_manager

    # ENHANCE 2026-09-16（P0）：顺序必须是「先标记 paused，再停 SSE」。
    # 反序（旧实现）存在必现竞态：stop_generation 会取消 run_generation 任务，
    # SSE 生成器的 finally 随即 finalize_task —— 把任务从 generation_manager._tasks
    # 删除并释放组锁；等 pause_task 执行时任务已不存在 → 返回 False →
    # 接口回 {"status":"error","message":"没有正在进行的生成任务"}，
    # 而前端无条件提示「已暂停」并显示「恢复」按钮，恢复时又因任务不存在而失败。
    paused = await generation_manager.pause_task(project_id)

    # 停止 SSE 流与正在飞的 AI 调用（不把章节标记为 failed，保留 generating 现场）
    await stop_generation(project_id, mark_failed=False)

    if not paused:
        logger.warning("暂停生成任务失败: project=%d（可能没有运行中的任务）", project_id)
        return {"status": "error", "message": "没有正在进行的生成任务"}

    return {"status": "ok", "message": "生成已暂停", "action": "paused"}
