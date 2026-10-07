"""sse_handlers 拆分模块 — sse_outline.py"""

import asyncio
import functools
import json
import time
from fastapi import HTTPException
from starlette.responses import StreamingResponse
from ...services.ai.workflows import generate_outline as _run_outline_workflow
from ...services import task_lock as _task_lock
from ...services import task_snapshot
from ...services.task_registry import TaskStatus, task_registry
from ...services.v51_state import (

    clear_v51_state_from_db,

    create_v51_state,

    load_v51_state_from_db,

    remove_v51_state,

    save_v51_state_to_db,

    set_v51_active_run,

)
from ..task_control import _cancel_events_lock, _cancel_tasks

from .sse_db_ops import _extract_project_context_with_retry, _save_and_finalize_outline
from .sse_shared import SSE_TOTAL_TIMEOUT_DEFAULT, _NotifyQueue, logger
from .sse_utils import _try_get_provider, _try_get_provider_with_retry

# ---------------------------------------------------------------------------
# ENHANCE 2026-09-15（Task 9）：目录生成的任务组锁 / 任务快照接线
# ---------------------------------------------------------------------------

#: 心跳间隔。锁 TTL 是 1800 秒（task_lock.DEFAULT_TTL_SECONDS），
#: 快照心跳超时阈值是 300 秒（task_snapshot.DEFAULT_STALE_SECONDS），
#: 60 秒一次对两者都有足够余量。
HEARTBEAT_INTERVAL_SECONDS = 60


def _make_lock_owner(project_id: int, task_id: str) -> str:
    """任务组锁的持有者标识。与任务快照共用同一串，便于诊断时把「锁」和「任务」串起来。"""
    return f"outline-generation:{project_id}:{task_id}"


async def _force_release_lock(lock_ctx: dict, project_id: int) -> None:
    """兜底释放 lock_ctx 里登记的任务组锁，并清空凭据避免重复释放。

    BUG-FIX（P1 锁泄漏）：抢锁成功后到 `_run_outline_task` 真正启动之间存在
    多个可取消点（登记任务快照等 `await asyncio.to_thread`）。若用户在这一窗口
    断开 SSE，协程被取消，而释放锁的代码只写在 `_run_outline_task` 的 finally 里
    —— 该方法从未启动，它的 finally 也不会执行，锁会白占 30 分钟 TTL，
    期间该项目所有目录/正文生成都被 TASK_LOCK_CONFLICT 拒绝。
    """
    key = (lock_ctx or {}).get("lock_key")
    owner = (lock_ctx or {}).get("owner_id")
    if not key or not owner:
        return
    lock_ctx["lock_key"] = ""
    try:
        await asyncio.shield(
            asyncio.to_thread(functools.partial(_task_lock.release, key, owner))
        )
        logger.warning(
            "[目录生成] 兜底释放任务组锁 project=%d key=%s", project_id, key
        )
    except Exception as _rel_e:
        logger.warning(
            "[目录生成] 兜底释放任务组锁失败 project=%d: %s", project_id, _rel_e
        )


async def _snapshot_progress(
    lock_ctx,
    *,
    phase=None,
    message=None,
    current_step=None,
    progress=None,
) -> None:
    """把阶段/进度写进任务快照。失败只告警，**绝不**影响生成主流程。"""
    tid = (lock_ctx or {}).get("snapshot_task_id")
    if not tid:
        return
    try:
        await asyncio.to_thread(
            functools.partial(
                task_snapshot.update_progress,
                tid,
                phase=phase,
                message=message,
                current_step=current_step,
                progress=progress,
            )
        )
    except Exception as _e:
        logger.warning("[目录生成] 写入任务快照失败（忽略）task_id=%s: %s", tid, _e)


async def _outline_heartbeat_loop(project_id: int, lock_ctx) -> None:
    """独立心跳：同时续约任务组锁与任务快照。

    ENHANCE 2026-09-15（Task 9）：目录生成单个阶段（L2 并发填充、AI 自检）
    可能十几分钟不产生任何事件。若只靠事件回调顺带续约，锁会在 30 分钟 TTL
    到期后被回收、快照会被对账成 interrupted —— 明明在跑的任务被判成中断。
    独立定时协程与事件流解耦，才能保证心跳稳定。
    """
    while True:
        try:
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            return
        try:
            lk = (lock_ctx or {}).get("lock_key")
            if lk:
                ok = await asyncio.to_thread(
                    _task_lock.heartbeat, lk, lock_ctx.get("owner_id", "")
                )
                if not ok:
                    logger.warning(
                        "[目录生成] 任务组锁续约失败（已被回收或抢占）project=%d", project_id
                    )
            sid = (lock_ctx or {}).get("snapshot_task_id")
            if sid:
                await asyncio.to_thread(task_snapshot.heartbeat, sid)
        except asyncio.CancelledError:
            return
        except Exception as _hb_e:
            logger.warning(
                "[目录生成] 心跳异常（忽略，不中断生成）project=%d: %s", project_id, _hb_e
            )


async def _outline_main_loop(

    project_id,

    task_info,

    sse_queue,

    outline_queue,

    _queue_has_event,

    outline_finished_ref,

    outline_task,

    client_disconnected_ref,

):

    """主循环 async generator：从队列读取事件并推送给客户端



    BUG-O-01 修复：

    - 使用可配置的SSE超时（SSE_TIMEOUT_OUTLINE）替代硬编码值

    - 添加 keep_alive 数据事件（而非仅SSE注释行），便于前端追踪连接健康状态

    - 缩短心跳间隔到8秒，确保代理/负载均衡器不会主动断开空闲连接

    """

    sse_queue_ref = sse_queue

    client_disconnected_ref[0] = False

    # BUG-O-01 修复：使用配置值作为SSE超时（本地常量）

    SSE_TOTAL_TIMEOUT = SSE_TOTAL_TIMEOUT_DEFAULT

    HEARTBEAT_INTERVAL = 8  # 心跳间隔8秒

    sse_start_time = time.time()



    try:

        # 注意：连接事件已由 generate_outline_sse_impl (L1426) 在 subscribe 后推送，

        # 此处不再重复发送，避免前端收到两条 status=connected 事件。



        while not outline_finished_ref[0]:

            elapsed_total = time.time() - sse_start_time

            if elapsed_total >= SSE_TOTAL_TIMEOUT:

                logger.warning(
                    "SSE 总超时 %ds, project=%d",
                    SSE_TOTAL_TIMEOUT,
                    project_id,
                )

                try:

                    yield f"data: {json.dumps({'status': 'error', 'message': f'生成超过{SSE_TOTAL_TIMEOUT // 60}分钟，已自动终止'}, ensure_ascii=False)}\n\n"

                except Exception as _e:

                    logger.debug("[SSE] 静默捕获异常: %s", _e)

                outline_finished_ref[0] = True

                break



            try:

                await asyncio.wait_for(_queue_has_event.wait(), timeout=HEARTBEAT_INTERVAL)

            except asyncio.CancelledError:

                # Python 3.11+ CancelledError is BaseException, not Exception.

                # Frontend closes old EventSource → ASGI throws CancelledError from wait_for.

                # Mark client_disconnected but DO NOT cancel outline_task — let it run on

                # (frontend can reconnect and resume via task_registry).

                logger.info("[目录生成] wait_for CancelledError (frontend closed SSE) project=%d", project_id)

                client_disconnected_ref[0] = True

                break

            except TimeoutError:

                # BUG-OUTLINE-10: send heartbeat event

                try:

                    heartbeat_event = {

                        "status": "generating",

                        "phase": "heartbeat",

                        "message": "AI 正在生成中，请稍候...",

                        "timestamp": time.time(),

                    }

                    yield f"data: {json.dumps(heartbeat_event, ensure_ascii=False)}\n\n"

                except (asyncio.CancelledError, Exception):

                    # SSE push failed = client disconnected. Only mark disconnected, don't cancel task.

                    client_disconnected_ref[0] = True

                    break

                continue



            _queue_has_event.clear()

            while not outline_queue.empty():

                try:

                    event = outline_queue.get_nowait()

                except asyncio.QueueEmpty:

                    break

                logger.info("[目录生成] 收到事件: %s project=%d", event.get("status"), project_id)

                try:

                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

                except (asyncio.CancelledError, Exception):

                    # SSE event push failed = client disconnected. Don't cancel outline_task.

                    client_disconnected_ref[0] = True

                    break

                if event.get("status") in ("completed", "error", "stopped"):

                    logger.info(

                        "[目录生成] 收到终止事件: %s project=%d", event.get("status"), project_id

                    )

                    outline_finished_ref[0] = True

                    break

            else:

                if not outline_finished_ref[0]:

                    # BUG-FIX 2026-09-16（中）：lost-wakeup 防护。原实现在排空后无条件
                    # clear()：若生产者恰好在「empty() 判定通过」与「clear()」之间完成
                    # put()+set()，通知标志被清掉、事件滞留队列，而外层 8s 超时分支只发
                    # 心跳不查队列 → review_needed 等「发完即停」的关键事件永久丢失。
                    # 改为：仅当队列确认为空才 clear，clear 后立刻复查一次，
                    # 若发现新事件则 continue 重新进入等待（事件已 set，立即返回）。
                    if outline_queue.empty():

                        _queue_has_event.clear()

                        if not outline_queue.empty():

                            continue

    finally:

        logger.info(

            "[目录生成] 主循环退出 project=%d, outline_finished=%s client_disconnected=%s",

            project_id,

            outline_finished_ref[0],

            client_disconnected_ref[0],

        )

        await task_registry.unsubscribe(task_info.task_id, sse_queue_ref)



        if not outline_finished_ref[0] and client_disconnected_ref[0]:

            # 客户端断开，保留任务在后台运行

            logger.info(

                "[目录生成] 客户端断开，保留任务在后台运行 project=%d task_id=%s",

                project_id,

                task_info.task_id,

            )

            async with _cancel_events_lock:

                tasks_list = _cancel_tasks.get(project_id)

                if tasks_list and outline_task in tasks_list:

                    tasks_list.remove(outline_task)

                # BUG-FIX 2026-09-16（中）：stop_generation 会先一步 del 整个键，
                # 此时 get 返回 None，原 `if not tasks_list: del` 抛 KeyError
                # 打穿 async generator 的 finally。改用 pop 带默认值安全清理。
                if tasks_list is not None and not tasks_list:

                    _cancel_tasks.pop(project_id, None)

        elif outline_finished_ref[0]:

            # BUG-OUTLINE-RACE 修复：生成已完成（收到终止事件），先等待后台任务清理完成再决定是否取消。

            # 之前立即取消可能导致 _run_outline_task 的 finally 清理（V5.1状态清理等）被中断。

            try:

                await asyncio.wait_for(outline_task, timeout=5)

                logger.info("[目录生成] 后台任务正常完成 project=%d", project_id)

            except Exception:

                logger.warning("[目录生成] 后台任务 5s 内未完成，强制取消 project=%d", project_id)

                outline_task.cancel()

                try:

                    await outline_task

                except (asyncio.CancelledError, Exception):

                    pass


            async with _cancel_events_lock:

                tasks_list = _cancel_tasks.get(project_id)

                if tasks_list and outline_task in tasks_list:

                    tasks_list.remove(outline_task)

                # BUG-FIX 2026-09-16（中）：stop_generation 会先一步 del 整个键，
                # 此时 get 返回 None，原 `if not tasks_list: del` 抛 KeyError
                # 打穿 async generator 的 finally。改用 pop 带默认值安全清理。
                if tasks_list is not None and not tasks_list:

                    _cancel_tasks.pop(project_id, None)

        else:

            # 异常情况：未完成但客户端未断开（SSE 超时等），清理任务

            logger.warning("[目录生成] 异常退出：未完成且客户端未断开 project=%d", project_id)

            async with _cancel_events_lock:

                tasks_list = _cancel_tasks.get(project_id)

                if tasks_list and outline_task in tasks_list:

                    tasks_list.remove(outline_task)

                # BUG-FIX 2026-09-16（中）：stop_generation 会先一步 del 整个键，
                # 此时 get 返回 None，原 `if not tasks_list: del` 抛 KeyError
                # 打穿 async generator 的 finally。改用 pop 带默认值安全清理。
                if tasks_list is not None and not tasks_list:

                    _cancel_tasks.pop(project_id, None)

            if not outline_task.done():

                outline_task.cancel()

                try:

                    await outline_task

                except (asyncio.CancelledError, Exception):

                    pass

            state = await task_registry.get_task_state(task_info.task_id)

            if state and state["status"] not in ("completed", "failed", "cancelled"):

                await task_registry.complete_task(

                    task_info.task_id,

                    status=TaskStatus.CANCELLED,

                    error_message="任务被取消",

                )

async def generate_outline_sse_impl(

    project_id: int,

    outline_mode: str = "auto",

):

    """SSE 流式生成目录（EventSource 只支持 GET）



    2026-08-21 增强：任务与 SSE 解耦，支持后台运行和断线重连。

    - 任务在 task_registry 中独立执行，不受 SSE 连接状态影响

    - SSE 断开时任务继续运行，重新连接可订阅进度



    2026-08-21 修复：全局异常保护，确保始终返回 SSE 流

    """



    try:

        task_type = "outline_v51"

        task_info = await task_registry.create_task(

            task_type=task_type,

            project_id=project_id,

            total_steps=8,

        )

    except Exception as _e:

        logger.error("[目录生成] 创建任务失败 project=%d: %s", project_id, str(_e), exc_info=True)



        # 关键修复：始终返回 SSE 流（而非 JSON），否则 EventSource 会 ERR_ABORTED

        # BUG-FIX-06: 将异常信息提前绑定到局部变量，避免 except 块退出后 e 被清理

        _error_msg = f"服务初始化失败: {str(_e)[:100]}"



        async def _task_create_error_stream():

            yield f"data: {json.dumps({'status': 'error', 'message': _error_msg}, ensure_ascii=False)}\n\n"



        return StreamingResponse(

            _task_create_error_stream(),

            media_type="text/event-stream; charset=utf-8",

            headers={

                "Cache-Control": "no-cache, no-transform",

                "Connection": "keep-alive",

                "X-Accel-Buffering": "no",

                "Content-Encoding": "none",

            },

        )



    # BUG-FIX 2026-09-15（高）：原判据是「started_at 比现在早 0.1 秒」，
    # 对**两次请求间隔小于 0.1 秒的并发入口**完全失效 —— 双击「生成目录」时，
    # 第二个请求拿到的是刚创建 50 毫秒的同一个任务，被判成「不是重连」，
    # 于是又起一个 _run_outline_task：同项目两个生成协程并发跑，双份 AI 调用、
    # 两次 _clear_and_save_outline 的 DELETE+INSERT 竞态、v51_state 互相覆盖。
    # 改为看「该任务是否已经挂了协程」这一确定性事实。
    is_reconnection = getattr(task_info, "_task", None) is not None



    async def event_generator():

        sse_queue: asyncio.Queue = asyncio.Queue()

        await task_registry.subscribe(task_info.task_id, sse_queue)



        state = await task_registry.get_task_state(task_info.task_id)

        init_event = {"status": "connected", "task_id": task_info.task_id}

        if state:

            init_event["task_state"] = state

        yield f"data: {json.dumps(init_event, ensure_ascii=False)}\n\n"

        logger.info(

            "[目录生成] SSE 连接已建立 project=%d reconnection=%s", project_id, is_reconnection

        )



        if state and state["status"] in ("completed", "failed", "cancelled"):

            yield f"data: {json.dumps({'status': state['status'], 'message': task_info.message}, ensure_ascii=False)}\n\n"

            await task_registry.unsubscribe(task_info.task_id, sse_queue)

            return



        if is_reconnection and state and state.get("last_event"):

            yield f"data: {json.dumps(state['last_event'], ensure_ascii=False)}\n\n"



        outline_queue: asyncio.Queue = _NotifyQueue()

        outline_finished = [False]

        _queue_has_event = outline_queue.get_notify_event()



        async def _qput(evt):

            await outline_queue.put(evt)

            await task_registry.broadcast_event(task_info.task_id, evt)

            if "outline_step" in evt:

                await task_registry.update_progress(

                    task_info.task_id,

                    step=evt["outline_step"],

                    message=evt.get("message", ""),

                    status=TaskStatus.RUNNING,

                )

            elif evt.get("status") in ("completed", "error", "stopped"):

                pass



        client_disconnected = [False]

        # ENHANCE 2026-09-15（Task 9）：任务组锁与任务快照的凭据容器。
        # 在 event_generator 里抢到锁 / 登记快照后填充；_run_outline_task 与
        # 心跳协程共享同一份凭据，收尾时按它释放锁、写快照终态。
        lock_ctx: dict = {"lock_key": "", "owner_id": "", "snapshot_task_id": ""}

        async def _run_outline_task():

            v51_task_finished = False

            outline_cancelled = False

            logger.info("[目录生成] 后台任务已启动 project=%d", project_id)

            summary = ""

            scoring_points = ""

            industry_profile = ""

            reference_outline = ""

            tender_context = ""

            project_scope = ""

            global_facts_items: list = []

            v51_state = None

            # BUG-FIX 2026-09-14（高）：这三个名字只在 try 内部（约 627 / 697 / 774 行）
            # 才绑定。主流程若在绑定之前抛异常（如 _qput 失败、DB 查询异常），
            # 下方的"降级重跑"兜底分支（约 950 行）引用它们会抛 NameError，
            # 使**最后一道恢复链路整体失效**，用户只能看到生成失败。
            # 修复：进入 try 前预置安全默认值。
            project_info = None

            _outline_progress_callback = None

            _phase_callback = None

            scoring_points_list = []

            # BUG-FIX 2026-10-04（P1）：boq_json_str 同样只在 try 内（约 756 行）绑定，
            # 但下方降级重跑分支（约 1111 行）无条件引用 —— 若在绑定前抛异常，
            # 兜底恢复链路会因 NameError 再次整体失效（与 2026-09-14 修复同源）。
            boq_json_str = ""

            # BUG 修复：保存引用，供 finally 块持久化到 DB 使用

            v51_state_ref = [None]

            # ENHANCE 2026-09-15（Task 9）：独立心跳协程。与事件流解耦，
            # 保证「锁续约 + 快照心跳」在长时间无事件的阶段也照常进行。
            # BUG-FIX 2026-09-16（低）：create 与进入 try 之间被取消的话，
            # finally 不会执行、心跳协程以 60s 周期永久空转。把 create 挪进 try 内，
            # 由统一 finally 回收。
            hb_task = None
            try:
                if lock_ctx.get("lock_key") or lock_ctx.get("snapshot_task_id"):
                    hb_task = asyncio.create_task(_outline_heartbeat_loop(project_id, lock_ctx))

                try:

                    provider, used_fb, _ = await _try_get_provider(outline_queue)

                except Exception:

                    logger.exception(
                        "[目录生成] 获取 AI Provider 失败 project=%d", project_id
                    )
                    await _qput({"status": "error", "message": "AI 配置异常，请检查设置"})

                    return



                logger.info("[目录生成] 开始提取项目数据 project=%d", project_id)

                await _qput(

                    {"status": "generating", "phase": "context", "message": "正在提取项目数据..."}

                )



                try:

                    logger.info("[目录生成] 开始查询项目数据 project=%d", project_id)

                    await _qput(

                        {

                            "status": "generating",

                            "phase": "outline_phase",

                            "outline_step": 1,

                            "message": "开始提取技术评分大类",

                        }

                    )

                    context = await _extract_project_context_with_retry(project_id)

                    summary = context["summary"]

                    scoring_points = context["scoring_points"]

                    scoring_points_list = context["scoring_points_list"]

                    industry_profile = context["industry_profile"]

                    reference_outline = context["reference_outline"]

                    tender_context = context["tender_context"]

                    project_scope = context["project_scope"]

                    project_info = context["project_info"]

                    global_facts_items = context.get("global_facts_items") or []

                    # BUG-FIX 2026-09-18（P1）：清单工程量接线到目录生成

                    boq_json_str = context.get("boq_json") or ""

                    await _qput(

                        {

                            "status": "generating",

                            "phase": "outline_phase",

                            # BUG-FIX 2026-09-18（P1）：提取完大类是在「构建一级目录」，
                            # 属于步骤1；原发 step2（前端语义=一级校验）造成错位。
                            "outline_step": 1,

                            "message": "技术评分大类提取完成，构建一级目录",

                        }

                    )

                except HTTPException as e:

                    logger.error("[目录生成] HTTPException: %s", e.detail)

                    await _qput({"status": "error", "message": e.detail})

                    return

                except Exception as e:

                    import traceback



                    logger.error(

                        "[目录生成] _extract_project_context 异常 project=%d: %s\n%s",

                        project_id,

                        e,

                        traceback.format_exc(),

                    )

                    await _qput({"status": "error", "message": f"数据库查询失败: {str(e)[:80]}"})

                    return



                await _qput(

                    {

                        "status": "generating",

                        "phase": "outline",

                        "message": "AI 正在生成目录结构，请稍候...",

                    }

                )

                logger.info("[目录生成] 开始 AI 生成目录 project=%d", project_id)



                async def _outline_progress_callback(

                    completed: int, total: int, current_title: str

                ):

                    await _qput(

                        {

                            "status": "generating",

                            "phase": "children",

                            "progress": {

                                "level1": total,

                                "completed": completed,

                                "total": total,

                            },

                            "message": f"正在生成第 {completed}/{total} 章：{current_title}",

                        }

                    )



                # BUG-FIX-ASYNC-DB：同步 sqlite 调用放入线程池，避免写锁竞争时阻塞事件循环
                v51_state = await asyncio.to_thread(load_v51_state_from_db, project_id)

                if v51_state is None:

                    v51_state = create_v51_state(project_id)

                    await _qput(

                        {

                            "status": "generating",

                            "phase": "v51",

                            "message": "已创建审核任务状态，等待生成完成...",

                        }

                    )

                else:

                    await _qput(

                        {

                            "status": "generating",

                            "phase": "v51",

                            "message": f"已恢复 V5.1 任务，当前阶段: {v51_state.current_phase}",

                        }

                    )



                # BUG 修复：将 v51_state 暴露给 finally 块和兜底分支

                v51_state_ref[0] = v51_state

                # BUG-FIX 2026-09-15（高）：标记「该项目有 V5.1 任务在跑」。
                # 不加这个标记的话，别处（例如 GET /tasks/v51/recovery）调用
                # load_v51_state_from_db 会**用 DB 行重建并覆盖**这个对象 ——
                # 覆盖后 cancel_v51_task / resume_v51_task 改的是新对象，
                # 而正在跑的工作流检查的是旧对象，「停止生成」静默失效。
                set_v51_active_run(project_id, True)



                async def _phase_callback(phase: str, message: str):

                    # BUG-OUTLINE-08 修复：每个阶段分配唯一步骤号，避免进度回跳

                    # 与前端 OUTLINE_STEPS_V51 保持一致（8步，1-based）

                    # BUG-FIX-26 修复：原 step 0 导致前端 outline_step-1=-1 数组越界

                    # 且进度百分比 (0-1)/8*100=-12.5% 为负值，改为 1-based 索引。

                    step_map = {

                        # BUG-FIX 2026-09-18（P1）：原映射整体错位一格——后端把
                        # 「准备」占 step1，一级生成从 step2 起；而前端
                        # OUTLINE_STEPS_V51 八步是 1=生成L1、2=校验L1、3=生成L2、
                        # 4=校验L2、5=生成L3、6=AI自检、7=最终审核、8=保存。
                        # 后果：步骤条永远显示上一个阶段的名字（用户看到「校验」
                        # 时 AI 其实在生成 L3），完成态判定也跟着错位。
                        "V5.1准备": 1,  # 准备阶段（归入步骤1：即将生成一级目录）

                        "V5.1一级生成": 1,  # 步骤1：AI生成一级目录

                        "V5.1一级校验": 2,  # 步骤2：一级目录校验

                        "V5.1二级生成": 3,  # 步骤3：生成二级目录

                        "V5.1二级校验": 4,  # 步骤4：二级目录校验

                        "V5.1三级生成": 5,  # 步骤5：生成三级目录

                        "V5.1标题质量": 5,  # 标题质量后处理（与三级生成同阶段，仅更新消息）

                        "V5.1技术增强": 5,  # 技术增强（与三级生成同阶段，仅更新消息）

                        "V5.1自检": 6,  # 步骤6：AI自检

                        "V5.1最终审核通过": 7,  # 步骤7：最终审核通过

                        "V5.1保存": 8,  # 步骤8：保存到数据库

                        "V5.1完成": 8,  # 生成完成

                    }

                    step = step_map.get(phase, 0)

                    await _qput(

                        {

                            "status": "generating",

                            "phase": "outline_phase",

                            "outline_step": step,

                            "message": message,

                        }

                    )

                    # ENHANCE 2026-09-15（Task 9）：阶段推进同步写入任务快照。
                    # 即使进程被强杀（finally 不执行），重启后也能从快照读出
                    # 「上次中断在哪个阶段、进度多少」，而不是一片空白。
                    await _snapshot_progress(
                        lock_ctx,
                        phase=phase,
                        message=message,
                        current_step=step or None,
                        progress=round(step / 8.0 * 100, 1) if step else None,
                    )



                outline = await _run_outline_workflow(

                    provider,

                    summary,

                    scoring_points,

                    industry_profile,

                    tender_context,

                    reference_outline=reference_outline,

                    project_scope=project_scope,

                    project_info=project_info,

                    global_facts=global_facts_items,

                    boq_text=boq_json_str,

                    progress_callback=_outline_progress_callback,

                    phase_callback=_phase_callback,

                    v51_state=v51_state,

                    outline_queue=outline_queue,

                    auto_confirm=True,

                    outline_mode=outline_mode,

                    # 遗留 L1 修复（2026-10-04）：审核事件经 _qput 进广播链，
                    # 断线重连客户端也能收到 review_needed/auto_confirm。
                    event_emitter=_qput,

                )



                if not outline:

                    await _qput({"status": "error", "message": "目录生成失败：AI 返回空结果"})

                    return



                if not await _save_and_finalize_outline(project_id, outline, outline_queue, emit=_qput):

                    return

                v51_task_finished = True

            except asyncio.CancelledError:

                logger.info("[目录生成] 任务被取消 project=%d", project_id)

                outline_cancelled = True

                await _qput({"status": "stopped", "message": "生成已停止"})

            except Exception as e:

                import traceback



                logger.error("[目录生成] 异常 project=%d: %s", project_id, str(e))

                error_msg = str(e)

                if any(

                    kw in error_msg

                    for kw in (

                        "AI调用",

                        "AI 调用",

                        "JSON 生成",

                        "重试",

                        "超时",

                        "目录",

                        "评分项",

                        "数量",

                    )

                ):

                    try:

                        provider, used_fb = await _try_get_provider_with_retry(

                            outline_queue, error_msg

                        )

                        if used_fb:

                            outline = await _run_outline_workflow(

                                provider,

                                summary,

                                scoring_points,

                                industry_profile,

                                tender_context,

                                reference_outline=reference_outline,

                                project_scope=project_scope,

                                project_info=project_info,

                                global_facts=global_facts_items,

                                boq_text=boq_json_str,

                                progress_callback=_outline_progress_callback,

                                phase_callback=_phase_callback,

                                v51_state=v51_state,

                                outline_queue=outline_queue,

                                auto_confirm=True,

                                # BUG-FIX 2026-10-04：兜底重跑与此前主流程保持同一
                                # outline_mode（直出/分步判别口径一致），否则降级
                                # 提供商会丢掉用户显式选择的生成模式。
                                outline_mode=outline_mode,

                                # 遗留 L1 修复：与主流程同走 _qput 广播链
                                event_emitter=_qput,

                            )

                            if outline:

                                if not await _save_and_finalize_outline(

                                    project_id, outline, outline_queue, is_fallback=True, emit=_qput

                                ):

                                    return

                                v51_task_finished = True

                                return

                    except Exception as fb_e:

                        logger.error("[目录生成] 兜底提供商也失败: %s", fb_e)

                suggest = (

                    "请检查AI配置或稍后重试"

                    if "超时" not in error_msg

                    else "主提供商响应超时，请在设置页面配置 DeepSeek/SiliconFlow/通义千问 API Key 后重试"

                )

                await _qput({"status": "error", "message": f"生成失败: {error_msg} | {suggest}"})

            finally:

                # BUG 修复（断点续生）：将 v51_state 中收集到的上下文信息持久化到 DB。

                # 原缺陷：save_v51_state_to_db 在全库无任何调用方，v51_tasks 表形同虚设——

                # 服务重启后无法恢复中断的目录生成任务。

                #

                # 触发时机：

                #   - 生成中途异常/被取消：state 含 summary/scoring_points/tender_context 等，

                #     下次进入时 load_v51_state_from_db 可恢复上下文，避免重复提取项目数据。

                #   - 正常完成（v51_task_finished=True）：先持久化一次作为"已完成快照"，

                #     再在下方清除。持久化用于审计/后续可能的编辑回传，clear 保证表整洁。

                if v51_state_ref[0] is not None:

                    _st = v51_state_ref[0]

                    _st.summary = summary or _st.summary

                    _st.scoring_points = scoring_points or _st.scoring_points

                    _st.industry_profile = industry_profile or _st.industry_profile

                    _st.tender_context = tender_context or _st.tender_context

                    _st.project_scope = project_scope or _st.project_scope

                    _st.project_info = project_info or _st.project_info

                    _outline_result = locals().get("outline")

                    # BUG-FIX 断点续生：持久化完整目录树，使 /v51-pending-task 与服务
                    # 重启后能恢复已生成的目录（此前 full_outline 始终为 None，
                    # final_outline_json 恒为 []，断点续生名不副实）。
                    _st.full_outline = _outline_result

                    if _st.level1_outline is None and _outline_result and len(_outline_result) > 0:

                        # 兼容：工作流返回值赋给 outline，不直接写入 state.level1_outline；

                        # 此处尝试从 v51_state.edited_outline（若 L1 审核有编辑）回写

                        _st.level1_outline = _st.edited_outline or _outline_result

                    try:

                        await asyncio.to_thread(save_v51_state_to_db, _st)

                    except Exception as _db_e:

                        logger.warning(

                            "[目录生成] 持久化 V51 状态失败 project=%d: %s", project_id, _db_e

                        )



                if v51_state is not None and v51_task_finished:

                    try:

                        await asyncio.to_thread(clear_v51_state_from_db, project_id)

                    except Exception as _e:

                        logger.warning(

                            "[目录生成] 清理 V51 数据库状态失败 project=%d: %s", project_id, _e

                        )

                    try:

                        remove_v51_state(project_id)

                    except Exception as _e:

                        logger.warning(

                            "[目录生成] 清理 V51 内存状态失败 project=%d: %s", project_id, _e

                        )

                elif v51_state is not None:

                    logger.info(

                        "[目录生成] 任务未完成（可能断连），保留 V5.1 DB 状态用于断点续生 project=%d",

                        project_id,

                    )

                logger.info("[目录生成] 后台任务结束 project=%d", project_id)

                outline_finished[0] = True

                # BUG 修复：正常结束路径必须将 task_registry 任务标记为终态。

                # 原缺陷：只有"异常退出"分支调用 complete_task，正常完成/失败后

                # TaskInfo 永远 RUNNING —— /tasks/status 持续误报"有运行中任务"

                # （前端 RecoveryBanner 误报可恢复）、内存泄漏，且下次 create_task

                # 会"重连"到僵尸任务触发重复生成。

                try:

                    if v51_task_finished:

                        await task_registry.complete_task(

                            task_info.task_id, status=TaskStatus.COMPLETED

                        )

                    elif outline_cancelled:

                        await task_registry.complete_task(

                            task_info.task_id,

                            status=TaskStatus.CANCELLED,

                            error_message="任务被取消",

                        )

                    else:

                        await task_registry.complete_task(

                            task_info.task_id,

                            status=TaskStatus.FAILED,

                            error_message="目录生成失败",

                        )

                except Exception as _reg_e:

                    logger.warning("[目录生成] 标记任务终态失败 project=%d: %s", project_id, _reg_e)

                # ===== ENHANCE 2026-09-15（Task 9）：收尾心跳 / 快照 / 锁 =====
                if hb_task is not None and not hb_task.done():
                    hb_task.cancel()
                    try:
                        await hb_task
                    except (asyncio.CancelledError, Exception):
                        pass

                # 解除「活跃运行」标记，之后 load_v51_state_from_db 才能正常
                # 从 DB 重建状态（否则会一直返回这份内存对象）。
                set_v51_active_run(project_id, False)

                # 快照终态：把「这次跑到哪一步、什么结局」写下来，
                # 否则行会一直停在 running，直到心跳超时被对账成 interrupted。
                if lock_ctx.get("snapshot_task_id"):
                    if v51_task_finished:
                        _snap_status = task_snapshot.STATUS_COMPLETED
                    elif outline_cancelled:
                        _snap_status = task_snapshot.STATUS_CANCELLED
                    else:
                        _snap_status = task_snapshot.STATUS_FAILED
                    try:
                        await asyncio.shield(
                            asyncio.to_thread(
                                functools.partial(
                                    task_snapshot.finish,
                                    lock_ctx["snapshot_task_id"],
                                    _snap_status,
                                    error_message="" if v51_task_finished else "目录生成未正常结束",
                                )
                            )
                        )
                    except Exception as _snap_e:
                        logger.warning("[目录生成] 写任务快照终态失败 project=%d: %s", project_id, _snap_e)

                # 释放任务组锁。用 shield：收尾阶段若被再次取消，释放动作仍会在
                # 后台跑完，不让锁白占 30 分钟 TTL（否则用户会看到「正在执行正文生成」）。
                if lock_ctx.get("lock_key"):
                    try:
                        await asyncio.shield(
                            asyncio.to_thread(
                                functools.partial(
                                    _task_lock.release,
                                    lock_ctx["lock_key"],
                                    lock_ctx["owner_id"],
                                )
                            )
                        )
                    except Exception as _rel_e:
                        logger.warning("[目录生成] 释放任务组锁失败 project=%d: %s", project_id, _rel_e)

        # BUG 修复：重连防重复生成。原实现对 RUNNING 中的已有任务也会无条件再起一个

        # _run_outline_task —— 同项目两个并发生成协程，双双执行

        # _clear_and_save_outline 的 DELETE+INSERT（DB 竞态），且覆盖 V51 内存状态。

        # 现在重连时若后台任务仍在运行，则复用它并从 task_registry 广播队列消费事件。

        existing_task = getattr(task_info, "_task", None)

        if is_reconnection and existing_task is not None and not existing_task.done():

            logger.info(

                "[目录生成] 重连复用运行中的后台任务 project=%d task_id=%s",

                project_id,

                task_info.task_id,

            )

            try:

                while True:

                    if existing_task.done():

                        # 后台任务已结束：排干残余广播事件后退出

                        while not sse_queue.empty():

                            try:

                                event = sse_queue.get_nowait()

                            except asyncio.QueueEmpty:

                                break

                            try:

                                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

                            except (asyncio.CancelledError, Exception):

                                break

                            if event.get("status") in ("completed", "error", "stopped"):

                                break

                        break

                    try:

                        event = await asyncio.wait_for(sse_queue.get(), timeout=8)

                    except asyncio.CancelledError:

                        # Python 3.11+ CancelledError skips `except TimeoutError`.

                        # Frontend closed SSE connection. Unsubscribe & exit cleanly;

                        # parent _run_outline_task continues running in background.

                        logger.info("[目录生成] 重连路径 wait_for CancelledError (frontend closed) project=%d", project_id)

                        break

                    except TimeoutError:

                        try:

                            heartbeat_event = {

                                "status": "generating",

                                "phase": "heartbeat",

                                "message": "AI 正在生成中，请稍候...",

                                "timestamp": time.time(),

                            }

                            yield f"data: {json.dumps(heartbeat_event, ensure_ascii=False)}\n\n"

                        except (asyncio.CancelledError, Exception):

                            break

                        continue

                    try:

                        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

                    except (asyncio.CancelledError, Exception):

                        break

                    if event.get("status") in ("completed", "error", "stopped"):

                        break

            finally:

                await task_registry.unsubscribe(task_info.task_id, sse_queue)

            return



        # BUG-FIX-42 修复：重连时后台任务已不存在（existing_task 为 None 或已

        # done）但任务状态仍为 RUNNING —— 说明后台协程异常退出未正确标记终态。

        # 原代码无条件落到下方 asyncio.create_task 再起一个新协程，新协程自建

        # v51_state 与旧任务的内存状态完全脱节，导致 L1/L2 已完成的进度丢失、

        # 双重 DELETE+INSERT 竞态。现在改为：标记任务失败并告知前端需手动重启，

        # 而非静默重跑。

        if is_reconnection and (existing_task is None or existing_task.done()):

            logger.warning(

                "[目录生成] 重连时后台任务已丢失 project=%d task_id=%s "

                "existing_task=%s done=%s —— 拒绝静默重跑，标记失败",

                project_id,

                task_info.task_id,

                existing_task is not None,

                existing_task.done() if existing_task is not None else "N/A",

            )

            try:

                await task_registry.complete_task(

                    task_info.task_id,

                    status=TaskStatus.FAILED,

                    error_message="后台生成任务已丢失，请重新发起目录生成",

                )

            except Exception as _reg_e:

                logger.warning("[目录生成] 标记丢失任务失败 project=%d: %s", project_id, _reg_e)

            try:

                yield f"data: {json.dumps({'status': 'error', 'message': '后台生成任务已丢失，请重新发起目录生成'}, ensure_ascii=False)}\n\n"

            except Exception as _e:

                logger.debug("[SSE] 静默捕获异常: %s", _e)

            await task_registry.unsubscribe(task_info.task_id, sse_queue)

            return

        # ===== ENHANCE 2026-09-15（Task 9）：抢任务组锁 + 登记任务快照 =====
        # 「技术方案」组互斥入口：正文生成（content-generation）与本任务同组，
        # 两者不能同时跑。Task 8 只把锁接进了正文生成，目录生成这一侧一直是
        # 裸奔状态 —— 双击「生成目录」会起两个并发生成协程（双份 AI 调用 +
        # 两次 _clear_and_save_outline 的 DELETE+INSERT 竞态）。
        lock_ctx["owner_id"] = _make_lock_owner(project_id, task_info.task_id)

        # 若上次目录生成留下未确认的中断快照，先如实告知用户（**不阻断**）。
        # 参考实现 recoverInterruptedOutlineGenerationTask 是直接拒绝启动、
        # 让用户手动重来；本软件没有可续跑的检查点（中断就是丢了），
        # 再拦一道只会让用户多点一次，所以选择「告知 + 继续」。
        try:
            _interrupted = await asyncio.to_thread(
                task_snapshot.list_interruptions, project_id, task_type="outline_v51"
            )
        except Exception as _int_e:
            _interrupted = []
            logger.warning("[目录生成] 查询中断快照失败（忽略）project=%d: %s", project_id, _int_e)
        if _interrupted:
            _int_msg = task_snapshot.interruption_summary(_interrupted[0])
            logger.warning("[目录生成] %s project=%d，本次将重新开始生成", _int_msg, project_id)
            await _qput(
                {
                    "status": "generating",
                    "phase": "recovery",
                    "outline_step": 1,
                    "message": f"{_int_msg}（本次已重新开始生成）",
                }
            )

        # 抢锁在独立线程执行；客户端若在抢锁瞬间断开 SSE，await 会被取消，
        # 而锁已在 worker 线程里落地（锁行已写 sqlite）。此时 lock_ctx 尚未来得及登记
        # lock_key，_run_outline_task 的 finally 也不会执行 → 锁白占 30 分钟 TTL。
        # 用 _lock_holder 让线程把 key 写进共享容器，即便 awaiter 被取消也能兜底释放。
        _lock_holder = {"key": ""}

        def _do_acquire():
            rec = _task_lock.acquire(
                "outline-generation",
                project_id=project_id,
                owner_id=lock_ctx["owner_id"],
                note=f"outline task_id={task_info.task_id}",
            )
            _lock_holder["key"] = rec.get("lock_key", "")
            return rec

        try:
            _lock_record = await asyncio.to_thread(_do_acquire)
            lock_ctx["lock_key"] = _lock_holder["key"]
        except _task_lock.TaskLockConflict as _lock_e:
            logger.warning("[目录生成] 任务组互斥拦截 project=%d: %s", project_id, _lock_e)
            try:
                await task_registry.complete_task(
                    task_info.task_id, status=TaskStatus.CANCELLED, error_message=str(_lock_e)
                )
            except Exception as _reg_e:
                logger.warning("[目录生成] 标记互斥拦截任务失败 project=%d: %s", project_id, _reg_e)
            await task_registry.unsubscribe(task_info.task_id, sse_queue)
            yield f"data: {json.dumps({'status': 'error', 'code': 'TASK_LOCK_CONFLICT', 'message': str(_lock_e)}, ensure_ascii=False)}\n\n"
            return
        except BaseException:
            # 抢到锁但 awaiter 被取消（客户端在抢锁瞬间断开）：锁已落地但 key 未登记，
            # 必须兜底释放，否则白占 30 分钟 TTL 期间该项目所有目录/正文生成都会被互斥拦截。
            _lk = _lock_holder["key"]
            if _lk:
                lock_ctx["lock_key"] = _lk
                await _force_release_lock(lock_ctx, project_id)
            raise

        try:
            try:
                _snap = await asyncio.to_thread(
                    functools.partial(
                        task_snapshot.register,
                        project_id,
                        "outline_v51",
                        task_id=task_info.task_id,
                        total_steps=8,
                        phase="准备",
                        message="目录生成已启动",
                        owner_id=lock_ctx["owner_id"],
                    )
                )
                lock_ctx["snapshot_task_id"] = _snap.get("task_id", "") or task_info.task_id
            except Exception as _snap_e:
                # 快照只是可观测性，失败不能挡住生成
                logger.warning("[目录生成] 登记任务快照失败（不影响生成）project=%d: %s", project_id, _snap_e)

            outline_task = asyncio.create_task(_run_outline_task())
        except BaseException:
            # 抢到锁但后台任务未能启动（取消/异常）：_run_outline_task 的 finally
            # 不会执行，必须在这里兜底释放，否则锁白占 30 分钟 TTL。
            await _force_release_lock(lock_ctx, project_id)
            raise

        async with _cancel_events_lock:

            _cancel_tasks.setdefault(project_id, []).append(outline_task)



        await task_registry.update_progress(

            task_info.task_id,

            status=TaskStatus.RUNNING,

            message="目录生成中...",

        )

        task_info._task = outline_task



        try:

            async for chunk in _outline_main_loop(

                project_id,

                task_info,

                sse_queue,

                outline_queue,

                _queue_has_event,

                outline_finished,

                outline_task,

                client_disconnected,

            ):

                yield chunk

        except asyncio.CancelledError:

            # Python 3.11+ CancelledError skips `except Exception`.

            # Re-raise so ASGI can close StreamingResponse cleanly.

            logger.info("[目录生成] SSE 主循环 CancelledError (frontend disconnect) project=%d", project_id)

            raise

        except Exception as _sse_e:

            logger.error(

                "[目录生成] SSE 主循环异常 project=%d: %s", project_id, _sse_e, exc_info=True

            )

            try:

                yield f"data: {json.dumps({'status': 'error', 'message': f'流异常: {str(_sse_e)[:100]}'}, ensure_ascii=False)}\n\n"

            except Exception as _e:

                logger.debug("[SSE] 静默捕获异常: %s", _e)



    return StreamingResponse(

        event_generator(),

        media_type="text/event-stream; charset=utf-8",

        headers={

            "Cache-Control": "no-cache, no-transform",

            "Connection": "keep-alive",

            "X-Accel-Buffering": "no",

            "Content-Encoding": "none",

        },

    )
