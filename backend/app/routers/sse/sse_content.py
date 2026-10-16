"""sse_handlers 拆分模块 — sse_content.py"""

import asyncio
import hashlib
import json
import logging
import time
from typing import Any
from fastapi import HTTPException, Query
from starlette.responses import StreamingResponse
from ...config import SSE_HEARTBEAT_INTERVAL_SECS, SSE_QUEUE_POLL_INTERVAL_SECS
from ...db import get_db
from ...services.ai.provider_factory import get_ai_config
from ...models import SectionUpdate
from ...services.ai.industry_profiles import get_industry_profile
from ...services.ai.writing_length_config import level2_target, level_target
from ...services.ai.workflows import generate_outline as _run_outline_workflow
from ...services.ai.workflows import generate_sections as _run_sections
from ...services.ai.workflows_sections import _is_rate_limit_error
from ...services.content_field_contract import build_field_contract
from ...services.files import latest_extracted_text
from ...services.generation_manager import generation_manager
from ...services.projects import update_step
from ...services.redis_cache import CacheService
from ...services.sections import (

    _count_words,

    _invalidate_build_tree_cache,

    build_tree,

    get_subtree_leaf_ids,

    update_section,

)
from ...services.task_lock import TaskLockConflict
from ...services.task_registry import TaskStatus, task_registry
from ...services.tender_context import extract_tender_context_async
from ...services.v51_state import (

    clear_v51_state_from_db,

    create_v51_state,

    load_v51_state_from_db,

    remove_v51_state,

    save_v51_state_to_db,

)
from ..task_control import _cancel_events, _cancel_events_lock, _cancel_tasks

from .sse_charts import _auto_generate_charts_for_section
from .sse_db_ops import _mark_sections_generating, _rollback_dirty_sections, _save_section_to_db
from .sse_shared import SSE_TERMINAL_STATUSES, _background_tasks, logger
from .sse_utils import _build_leaf_dicts, _fetch_project_data_async, _try_get_provider

# ENHANCE 2026-09-15：并发硬上限提为模块级常量，供正文生成与兜底扩写共用。
# PERF-P1-B4 注释声称「从 3 提升到 8」，但常量一直是 4 —— 注释与代码不一致；
# 前端并发选择器提供 6，会被这里静默钳制，用户无从得知。现统一为 8。
MAX_CONTENT_CONCURRENCY = 8

# Risk-P3b FIX 2026-10-11：模块级命名常量，避免硬编码散落在函数体内部。
# 整体进度事件推送节流：同 percent 值 2s 内只发一次，避免日志噪声。
_OVERALL_PROGRESS_THROTTLE_SECS = 2.0
# chart_queue 入队有界等待（1s）：QueueFull 后不阻塞 section_complete_callback。
_CHART_QUEUE_PUT_WAIT_TIMEOUT = 1.0

# ENHANCE 2026-10-05（P-11）：正文编排的可配置面。
# 此前这些值全部硬编码在 _adjudicate_illustrations 的签名默认值里
# （max_ai_images=6 / table_ratio="moderate"），用户完全无法控制
# 「图片总数」「表格密度」「是否禁用某类图」——而商务标/技术标对
# 图文比例的诉求差异很大。现接入 Query 参数，与 EasyBid 规格六.5 对齐。
TABLE_RATIO_VALID = ("none", "light", "moderate", "heavy")


def _should_emit_heartbeat(
    now: float,
    last_activity_time: float,
    last_heartbeat_time: float,
    interval_secs: float,
) -> bool:
    """SSE 保活帧判定：空闲超阈 **且** 距上次心跳也超阈。

    P0 修复 2026-09-29：原主循环内联写法只判“空闲 >= 阈值”，而
    last_heartbeat_time 从未参与判断；加上心跳分支的 continue，导致空闲期
    内每轮循环都发心跳且从不消费队列。抽成纯函数便于单测锁定两个判据。

    Args:
        now: 当前时间戳
        last_activity_time: 最后一次**转发真实事件**的时间
        last_heartbeat_time: 最后一次发 keep_alive 的时间（0.0=从未发过）
        interval_secs: 心跳间隔（同时作为空闲阈值与两次心跳的最小间隔）

    Returns:
        bool: 是否应发一条 keep_alive
    """
    if interval_secs <= 0:
        return False
    return (
        (now - last_activity_time) >= interval_secs
        and (now - last_heartbeat_time) >= interval_secs
    )

async def generate_content_impl(

    project_id: int,

    writing_length: str = Query("standard"),

    regenerate_ids: str = Query(""),

    root_section_id: int | None = Query(None),

    regenerate_missing: bool = Query(False),

    use_orchestration: bool = Query(False),

    level2_default: int = Query(3500, ge=1000, le=10000),

    use_word_sampling: bool = Query(False),

    use_fallback_expand: bool = Query(False),

    use_consistency_audit: bool = Query(False),

    concurrency: int | None = Query(None),

    # ENHANCE 2026-10-05（P-11）：正文编排参数配置面
    # BUG-FIX 2026-10-05：必须追加在**末尾**而非插在中间——
    # routers/sections.py::generate_content 以**位置参数**调用本函数
    # （`generate_content_impl(project_id, writing_length, ..., concurrency)`），
    # 若在中间插入新参数会使后续位置全部错位，Query 默认值被当成实参传入，
    # 最终 `int(concurrency)` 收到 Query 对象抛 TypeError。
    orch_max_ai_images: int = Query(6, ge=0, le=50),

    orch_table_ratio: str = Query("moderate"),

    orch_prefer_mermaid: bool = Query(True),

    orch_section_per_segment: int = Query(10, ge=3, le=40),

    # 易标 R9/R10/R11（2026-10-12）：字数双向多轮调整（额度分配 + 六道硬校验）。
    #   None = 服务端判定（config 默认 + 强控自动启用）；true/false = 单次强制。
    # BUG-FIX 2026-10-05 同款约束：必须追加在**签名末尾**，否则
    # routers/sections.py::generate_content 的位置参数调用会整体错位。
    use_word_adjust: bool | None = Query(None),
):

    """SSE 并发生成正文（EventSource 只支持 GET）



    Args:

        project_id: 项目 ID

        writing_length: 写作长度

        regenerate_ids: 重新生成的章节 ID 列表

        root_section_id: 根章节 ID

        regenerate_missing: 是否重新生成缺失内容

        use_orchestration: 是否使用内容编排（先让 AI 判断每个章节的内容类型）

        orch_max_ai_images: 全文 AI 生图硬上限，0 = 禁用 AI 生图
        （对应易标规格 useAiImages=false / maxAiImages=N）
        orch_table_ratio: 表格密度 none/light/moderate/heavy（对应 20%/40%/宽松）
        orch_prefer_mermaid: True = 同章节两者都需时 mermaid 优先；
        False = 两类按 priority 合并竞争同一配额（商务标等偏照片场景）
        orch_section_per_segment: 分段择优的段长（越小图片分布越均匀）

        level2_default: [已废弃 2026-10-08] 字数控制已下沉到三级目录（L3）档位，此参数不再使用

        use_word_sampling: 是否使用字数采样预估（先采样3个小节）

        use_fallback_expand: 是否使用兜底扩写（生成后对字数最少的章节扩写）

        use_consistency_audit: 是否使用一致性审计（生成后检查全文一致性）

        concurrency: 并发数（覆盖AI配置的默认值）

    """



    # SSE 事件生成器

    # 关键修复：所有错误都必须在 SSE 流中返回错误事件，而不是抛出 HTTPException

    # 因为 HTTPException 会返回 JSON 响应，导致 EventSource 触发 onerror

    async def event_generator():

        yield f"data: {json.dumps({'status': 'connected'}, ensure_ascii=False)}\n\n"



        try:

            # 1. 初始化 AI 配置（优先使用保存的配置，不可用时自动降级）

            try:

                provider, used_fb, config_concurrency = await _try_get_provider()

                # BUG-FIX 2026-09-15：局部常量曾是 4，与注释矛盾且静默钳制前端的「并发 6」；
                # 现移到模块级 MAX_CONTENT_CONCURRENCY=8，生成与兜底扩写共用。

                # BUG-FIX 2026-10-07（删除被生产日志证伪的注释）：
                # 此处原注释断言「429 限流问题已在 Agnes 实际测试中验证过 R=60 RPM 充足，
                # 瓶颈是并发不够而非限流」。该结论已被 logs/backend.log 直接推翻 ——
                # 2026-10-07 22:34~22:41 连续 7 分钟、每分钟 70~97 次 HTTP 429、
                # 累计 528 条，章节生成整批失败。真实约束是**厂商侧每分钟请求/token 配额**，
                # 而非本进程的在飞并发数（信号量对此完全无感）。
                # 现引入 services/ai/rate_gate.py：按 RPM 令牌桶主动节流 + 收到 429 时
                # AIMD 自适应收缩并发，两者叠加才会自动收敛到可持续速率。

                raw_concurrency = concurrency if concurrency else config_concurrency

                actual_concurrency = max(1, min(int(raw_concurrency), MAX_CONTENT_CONCURRENCY))

                if actual_concurrency != int(raw_concurrency):

                    logger.warning(

                        "[正文生成] 并发数 %d 超过硬上限 %d，已钳制为 %d",

                        int(raw_concurrency), MAX_CONTENT_CONCURRENCY, actual_concurrency,

                    )

                if used_fb:

                    logger.info("[正文生成] 主配置不可用，已降级到兜底提供商: %s", used_fb)

            except Exception as e:

                yield f"data: {json.dumps({'status': 'error', 'message': f'AI 配置异常: {e!s}'}, ensure_ascii=False)}\n\n"

                return



            # 2. 获取项目数据（使用提取的公共函数）

            try:

                # BUG 修复：生成开始前先失效 Redis 章节缓存。

                # 原缺陷：缓存 TTL 30 分钟且保存路径从不失效，上一次生成完成后的

                # regenerate_missing/断点续生 会读到旧 status='pending'/空 content，

                # 导致已完成章节被重复生成（重复 AI 计费）。

                try:

                    CacheService.invalidate_sections(project_id)

                except Exception as _inv_e:

                    logger.warning("[正文生成] 章节缓存预失效失败（忽略）: %s", _inv_e)

                project_data = await _fetch_project_data_async(project_id)

                if project_data is None:

                    yield f"data: {json.dumps({'status': 'error', 'message': '请先完成摘要和评分点识别'}, ensure_ascii=False)}\n\n"

                    return

                summary = project_data.get("summary", "")

                scoring_points = project_data.get("scoring_points", "")

                scoring_points_list = (

                    json.loads(scoring_points)

                    if isinstance(scoring_points, str)

                    else scoring_points

                )

                if not isinstance(scoring_points_list, list):

                    scoring_points_list = []

                industry_profile = project_data.get("industry_profile", "")

                tender_context = project_data.get("tender_context", "")

                # ENHANCE 2026-10-05（P-9）：招标文件已提取的「工程作法」原文。
                # 有值 → 正文生成走「承接既有方案扩写」；空 → 走「从头拟定方案」。
                construction_method = project_data.get("construction_method", "") or ""

                sections_by_id = project_data.get("sections_by_id", {})

                children_map = project_data.get("children_map", {})

                all_rows = project_data.get("all_rows", [])

            except Exception as e:

                import traceback



                logger.error(

                    "[正文生成] _fetch_project_data_async 失败 project=%d: %s\n%s",

                    project_id,

                    e,

                    traceback.format_exc(),

                )

                yield f"data: {json.dumps({'status': 'error', 'message': f'数据库查询失败: {str(e)[:80]}'}, ensure_ascii=False)}\n\n"

                return



            def _get_section_level(section: dict, sections_by_id: dict) -> int:

                """确定章节的实际层级：叶子节点可能是三级，也可能是二级（无子级时）"""

                level = section.get("level", 3)

                if level == 2:

                    children = [

                        s for s in sections_by_id.values() if s.get("parent_id") == section["id"]

                    ]

                    if not children:

                        return 2

                return level



            # 3. 构建需要生成的叶子章节列表

            try:

                leaf_dicts = _build_leaf_dicts(

                    project_id=project_id,

                    sections_by_id=sections_by_id,

                    children_map=children_map,

                    root_section_id=root_section_id,

                    regenerate_ids=regenerate_ids,

                    regenerate_missing=regenerate_missing,

                    level2_default=level2_default,

                    writing_length=writing_length,

                )

            except ValueError as e:

                yield f"data: {json.dumps({'status': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"

                return



            if not leaf_dicts:

                yield f"data: {json.dumps({'status': 'info', 'message': '所有章节已生成完成，没有需要重新生成的章节'}, ensure_ascii=False)}\n\n"

                return



            # BUG 修复：用户显式指定 regenerate_ids 重新生成时，必须失效对应的

            # 章节内容缓存（TTL 1小时），否则重新生成直接命中缓存返回旧内容，

            # 用户感知"重新生成无效"。root_section_id 子树重生成同理。

            #

            # BUG-FIX 2026-09-16：原实现只在「显式 regenerate_ids / 子树重生成」时失效

            # 缓存，遗漏了另两条会重生成**已有内容章节**的路径：

            #   ① 生成全部（默认模式）—— completed 但正文 <50 字的空壳章节会被重新生成；

            #   ② 补全生成（regenerate_missing）—— 同样包含空壳章节。

            # 这两类章节在上一次生成时已写入缓存（TTL 1 小时），重生成直接命中缓存返回

            # 同样不足 50 字的旧正文，用户感知为「点了重新生成，字数一点没变」。

            # 结论：既然 `_build_leaf_dicts` 已经判定完「本次要生成哪些章节」，

            # 缓存就必须无条件服从该判定 —— 对本次选中的**全部**章节预失效。

            try:

                from ...services.ai.workflows_sections import (

                    _section_content_cache as _sec_cache,

                )



                # BUG-FIX 2026-09-14（中）：缓存键末段是 section_id 而非 title，
                # 传标题做子串匹配恒不命中；改为按 id 精确失效。
                _regen_ids = [ld.get("id") for ld in leaf_dicts]

                if _regen_ids:

                    _invalidated = await _sec_cache.invalidate_by_ids(project_id, _regen_ids)

                    if _invalidated:

                        logger.info(

                            "[正文生成] 重生成缓存预失效: %d 条, project=%d",

                            _invalidated,

                            project_id,

                        )

            except Exception as _cache_e:

                logger.warning("[正文生成] 重生成缓存预失效失败（忽略）: %s", _cache_e)



            # V2.1 增强：从数据库读取全局事实变量（正文字里行间强制引用唯一事实源）

            global_facts: list | None = project_data.get("global_facts_items") or None

            # ENHANCE 2026-10-14（facts-ns P1-3）：缺失值策略随正文生成透传，
            # 供 system 追加 omit/placeholder 缺值口径铁律（开关默认关，
            # 旧 Redis 缓存无此键时按 fabricate 处理，行为不变）。
            facts_policy: str = project_data.get("facts_policy") or "fabricate"

            # ENHANCE 2026-10-15（对齐易标「请先完成全局事实设定，再生成正文」）：
            # 可选事实门槛。开关 FACTS_CONTENT_GATE_ENABLED 默认关=保持现有行为；
            # 开启后没有任何全局事实时拒绝进入正文生成，强制用户先完成事实设定，
            # 避免正文在没有统一口径基准的情况下各章节自由发挥。
            try:
                from ...services.facts_service import content_facts_gate_enabled as _facts_gate

                if _facts_gate() and not global_facts:
                    raise HTTPException(
                        status_code=400,
                        detail="已开启全局事实门槛：请先完成全局事实设定（提取并确认事实变量）后再生成正文。",
                    )
            except HTTPException:
                raise
            except Exception as _gate_e:  # pragma: no cover - 防御性
                logger.warning("[正文生成] 事实门槛开关读取失败，视为未开启: %s", _gate_e)

            # ENHANCE 2026-10-07：构建字段契约（提取项目字段 + 全局数据字典）。
            # 全局数据字典把项目名称/编号/业主/工期/人员数量/设备型号/质保期等
            # 关键一致性字段收敛为唯一权威值，随稳定上下文注入每一章，
            # 正文必须一字不差引用 —— 这是前后章节数据一致的核心机制。
            field_contract = build_field_contract(project_data, global_facts)

            # ENHANCE 2026-10-07：行业名 → 章节归属随行业模板词表匹配
            industry = project_data.get("industry") or ""

            # 字段调用追溯：记录每个章节实际注入了哪些字段（供审计/自检）
            field_trace: list = []

            # R14 硬门槛（易标字数控制 2026-10-12）：项目一旦配置了绝对字数
            # 目标（最少/最多/每节任一 > 0），生成正文前必须持有「与目录版本
            # 配对」的生效快照；未配置（全 0）时保持既有档位体系，不设门槛。
            # 快照由「字数统计」页的固化动作产出，改配置会置空快照（PUT 端点），
            # 从根上杜绝「改了目录/配置但控制链按旧口径跑」的静默脱节。
            from ...services.word_control_runtime import (
                build_section_override,
                load_project_word_control,
            )

            _wc_state = await asyncio.to_thread(load_project_word_control, project_id)

            _wc_gate_error = _wc_state.gate_error()

            if _wc_gate_error:

                yield f"data: {json.dumps({'status': 'error', 'message': _wc_gate_error}, ensure_ascii=False)}\n\n"

                return

            cancel_event = asyncio.Event()

            async with _cancel_events_lock:

                _cancel_events.setdefault(project_id, []).append(cancel_event)



            leaf_ids_for_generation = [ld["id"] for ld in leaf_dicts]

            # R5/R8（易标字数控制 2026-10-12）：强控生效时为每个参与生成的
            # L3+ 章节装配「目标 S / 硬区间 [0.8S,1.2S] / 初稿倒推目标 G」覆盖表；
            # 未配置或未强控时为空表，下游提示词与压缩链回落既有档位逻辑。
            _wc_override = await asyncio.to_thread(
                build_section_override, _wc_state, leaf_dicts
            )

            if _wc_override:

                logger.info(

                    "[字数控制] 强控生效：%d 个章节按快照装配（S=%d, 区间=[%d,%d]）",

                    len(_wc_override),

                    _wc_state.effective_options.section_words,

                    next(iter(_wc_override.values()))["lo"],

                    next(iter(_wc_override.values()))["hi"],

                )

        except Exception as e:

            import traceback



            traceback.print_exc()

            if "cancel_event" in locals():

                async with _cancel_events_lock:

                    evs = _cancel_events.get(project_id)

                    if evs and cancel_event in evs:

                        evs.remove(cancel_event)

                        if not evs:

                            del _cancel_events[project_id]

            yield f"data: {json.dumps({'status': 'error', 'message': f'初始化失败: {e!s}'}, ensure_ascii=False)}\n\n"

            return



        queue: asyncio.Queue = asyncio.Queue()



        saved_count = 0

        failed_count = 0

        cancelled_count = 0

        # BUG-P3-A FIX 2026-10-11：chart_processed 估算错误。
        # 原实现用 saved_count（所有落库成功章节数） - chart_queue.qsize()，
        # 但 chart_queue.put_nowait 在队列满时会 QueueFull 被 skip，
        # 而 failed/cancelled 章节根本不会入队 → chart_processed 永远偏低。
        # 现新增 chart_enqueued_count，只有真正入队成功的章节才 +1。
        #
        # Risk-P3c NOTE 2026-10-11：chart_enqueued_count 是本 SSE 事件生成器
        # 内的 nonlocal 局部变量，生命周期 = 单次 SSE 连接。
        # 多 worker 部署时各 worker 独立计数，无跨 worker 一致性问题——
        # 每个 SSE 连接固定落在一个 worker 上，计数只服务于当前连接的
        # overall_progress 事件推送，无需共享状态。
        chart_enqueued_count = 0

        # BUG-P2b FIX 2026-10-11：整体进度推送节流。
        # 防止每完成一个章节就推一次相同 percent 值 → 日志噪声。
        _last_overall_progress_event_at = 0

        results_lock = asyncio.Lock()



        # 计算目录版本哈希并创建生成任务

        # BUG 修复：build_tree 是同步 DB 查询+树构建，在事件生成器中直接调用

        # 会阻塞事件循环，改用 asyncio.to_thread。

        tree = await asyncio.to_thread(build_tree, project_id)

        # 将 SectionTreeNode 对象转换为字典以便 JSON 序列化

        tree_dict = [

            node.model_dump() if hasattr(node, "model_dump") else dict(node) for node in tree

        ]

        outline_version = hashlib.sha256(

            json.dumps(tree_dict, ensure_ascii=False).encode()

        ).hexdigest()[:16]



        # ENHANCE 2026-09-15（高）：任务组互斥冲突必须转成友好错误。

        # create_task 会先抢「技术方案」组的锁；抢不到说明同项目已有任务在跑。

        # 原实现这里没有任何并发保护，也没有 try —— 重复点击「生成正文」会创建

        # 第二个 generation_tasks 行并覆盖内存任务，两个生成循环并发写同一批

        # sections；而异常若直接穿透生成器，SSE 连接会无提示地断开。

        try:

            generation_id = await generation_manager.create_task(

                project_id,

                outline_version,

                config={

                    "writing_length": writing_length,

                    "concurrency": actual_concurrency,

                },

            )

        except TaskLockConflict as _lock_e:

            logger.warning(

                "[正文生成] 任务组互斥拦截 project=%d: %s", project_id, _lock_e

            )

            # BUG-FIX 2026-09-16（中）：return 会跳过主循环段带 finally 的清理逻辑，
            # 384 行注册进 _cancel_events 的 cancel_event 永不移除 ——
            # 每次互斥冲突泄漏一个死事件，列表随冲突次数无限增长。
            async with _cancel_events_lock:

                evs = _cancel_events.get(project_id)

                if evs and cancel_event in evs:

                    evs.remove(cancel_event)

                    if not evs:

                        _cancel_events.pop(project_id, None)

            yield f"data: {json.dumps({'status': 'error', 'code': 'TASK_LOCK_CONFLICT', 'message': str(_lock_e)}, ensure_ascii=False)}\n\n"

            return



        # 更新任务的 total_sections

        async def _update_total_sections():

            # BUG 修复：原实现在持有 generation_manager._lock 期间同步执行

            # _persist_task（DB 写入），阻塞事件循环且与该类"锁内改内存、

            # 锁外异步持久化"的既有模式不一致；同时绕过 _persist_locks 串行锁，

            # 可能与并发章节进度的 INSERT OR REPLACE 产生旧快照覆盖竞态。

            # 改为：锁内只更新内存值，持久化用与 update_section_progress 相同的

            # 串行锁 + 线程池模式。

            task = None

            async with generation_manager._lock:

                task = generation_manager._tasks.get(project_id)

                if task:

                    task.total_sections = len(leaf_dicts)

            if task:

                persist_lock = generation_manager._persist_locks.setdefault(

                    project_id, asyncio.Lock()

                )

                async with persist_lock:

                    await asyncio.to_thread(generation_manager._persist_task, task)



        await _update_total_sections()



        # === PERF-P0-B1: 配图流水线并行化基础设施 ===

        # 在"正文开始生成"之前先启动配图 worker pool，

        # 让每个章节 DB 保存后立即触发配图（见 section_complete_callback 中的 put_nowait），

        # 与剩余章节的 LLM 生成并行。原实现等 AI 全部结束后才开始配图（~140s 纯空洞）。

        chart_queue: asyncio.Queue = asyncio.Queue(maxsize=50)  # PERF-3 有界化: 防内存爆

        chart_done_event = asyncio.Event()

        chart_semaphore_pipe = asyncio.Semaphore(2)  # 并发配图上限 2（Agnes 限额）



        async def _chart_worker():

            """从 chart_queue 取章节，调配图引擎"""

            while not chart_done_event.is_set() or not chart_queue.empty():

                # PERF-CHART-WORKER-EXIT: check done_event BEFORE queue.get()

                if chart_done_event.is_set() and chart_queue.empty():

                    break

                try:

                    result = await asyncio.wait_for(chart_queue.get(), timeout=2.0)

                except asyncio.TimeoutError:

                    if chart_done_event.is_set() and chart_queue.empty():

                        break

                    continue

                except asyncio.QueueEmpty:

                    continue

                if result is None:

                    break

                async with chart_semaphore_pipe:

                    if cancel_event.is_set():

                        continue

                    try:

                        await _auto_generate_charts_for_section(

                            project_id,

                            result,

                            provider,

                            writing_length,

                            scoring_standard=scoring_points if scoring_points else "",

                            industry_profile=industry_profile,

                        )

                    except Exception as _e:

                        logger.warning("配图生成失败 section_id=%s: %s", result.get("id"), _e)



        # 预启动 worker（数量 = 配图并发上限）

        # PERF-3b: worker 数动态计算（硬编码 4 → 按章节规模自适应）
        # 最少 2（单章也保证配图并行），最多 4（并发配图上限）。
        _chart_worker_count = max(2, min(4, (len(leaf_dicts) + 9) // 10))
        chart_workers = [asyncio.create_task(_chart_worker(), name=f'chart-worker-{i}')
                        for i in range(_chart_worker_count)]

        async def _converge_chart_workers():
            # BUG-FIX 2026-10-08（P1 worker 泄漏）：早退/编排取消路径位于 try@1090
            # 之外，内层 finally（唯一收敛点）不会执行 → 2~4 个 _chart_worker 以
            # 2s 周期无限轮询到进程重启（CPU/FD 泄漏，即 2026-09-14「BUG-FIX 高」
            # 描述的原始症状——该修复只覆盖了 try@1090 内的路径）。幂等，可多处调用。
            try:
                chart_done_event.set()
                for _w in chart_workers:
                    if not _w.done():
                        _w.cancel()
                if chart_workers:
                    await asyncio.wait_for(
                        asyncio.gather(*chart_workers, return_exceptions=True),
                        timeout=2.0,
                    )
            except Exception:
                pass

        # CLEANUP 2026-10-04：删除死变量 chart_success_count/chart_fail_count
        # （全文件仅定义无引用，配图成失统计实际由 worker 内部日志与
        # chart_done_event 汇总承载）。


        async def progress_callback(

            section_id: int,

            title: str,

            status: str,

            progress: int,

            message: str,

            word_count: int = 0,

        ):

            # PERF-PROGRESS-ROBUST 2026-09-08：update_section_progress 内部含

            # DB 持久化（asyncio.gather + run_in_executor），理论上异常已

            # 被 return_exceptions=True 吞掉。但仍加 try/except 兜底——

            # 一旦 update 异常导致 queue.put 不执行，前端就再也收不到

            # "completed" 事件，章节会一直显示"generating" 假卡住。

            try:

                await generation_manager.update_section_progress(

                    project_id, section_id, status, progress

                )

            except Exception as _gm_e:

                logger.warning(

                    "[正文生成] update_section_progress 异常 section=%s status=%s: %s",

                    section_id, status, _gm_e,

                )

            try:

                await queue.put(

                    {

                        "section_id": section_id,

                        "title": title,

                        "status": status,

                        "progress": progress,

                        "message": message,

                        "word_count": word_count,  # BUG-F-05 修复：添加字数信息用于前端显示

                    }

                )

            except Exception as _q_e:

                logger.warning(

                    "[正文生成] progress_callback queue.put 异常 section=%s: %s",

                    section_id, _q_e,

                )



        async def section_complete_callback(result: dict):

            nonlocal saved_count, failed_count, cancelled_count, chart_enqueued_count, _last_overall_progress_event_at

            sid = result.get("id")

            status = result.get("status")

            content = result.get("content", "")

            title = result.get("title", "")



            # BUG-C-03 修复：即使DB保存失败，也要通知前端章节状态，

            # 避免前端显示"generating"卡住且用户不知道需要重试。

            db_save_error = None

            try:

                # PERF-SAVE-NO-SELECT 2026-09-08：传入 project_id 省去内部 SELECT

                await asyncio.to_thread(

                    _save_section_to_db, sid, status, content, project_id,

                )

            except Exception as e:

                logger.error("[正文生成] 保存章节失败 section_id=%s: %s", sid, e)

                # 保存失败时仍按失败处理

                status = "failed"

                result["status"] = status

                result["error"] = f"保存失败: {e}"

                # BUG 修复：兜底把 failed 状态写入 DB，避免章节永远卡在 generating

                try:

                    await asyncio.to_thread(

                        _save_section_to_db, sid, "failed", "", project_id,

                    )

                except Exception as _fallback_save_e:

                    logger.debug("[正文生成] 兜底保存 failed 状态异常 sid=%s: %s", sid, _fallback_save_e)



            # PERF-P0-B1: 章节正文 DB 保存后立即入队配图任务，与剩余章节的 LLM 生成并行

            if status == "completed" and not cancel_event.is_set():

                # BUG-FIX 2026-09-14（中）：chart_queue 是 maxsize=50 的有界队列，
                # 但原实现用 `await chart_queue.put(...)`——队列满时会一直等待，
                # **永远不会抛 QueueFull**，所以下面的"降级同步配图"是死代码；
                # 更糟的是它会把 section_complete_callback 反压卡住，
                # 进而拖住 _gen_one 与整条生成流水线（大项目明显变慢）。
                # 修复：改用 put_nowait + 有界等待（1s）。仍满则丢弃该章节的自动配图，
                # 绝不阻塞 section_complete_callback（配图可事后用图表端点单独补）。
                try:

                    chart_queue.put_nowait(result)
                    chart_enqueued_count += 1  # BUG-P3-A FIX：真正入队才计数

                except asyncio.QueueFull:

                    try:

                        await asyncio.wait_for(chart_queue.put(result), timeout=_CHART_QUEUE_PUT_WAIT_TIMEOUT)
                        chart_enqueued_count += 1  # BUG-P3-A FIX：1s 等待入队也计数

                    except (TimeoutError, asyncio.TimeoutError):

                        logger.warning(
                            "[正文生成] chart_queue 持续满载，跳过该章节自动配图 "
                            "section_id=%s（可事后用图表端点补）", result.get("id"),
                        )

                except Exception as _e:

                    logger.debug("[正文生成] chart_queue.put 异常 section_id=%s: %s", result.get("id"), _e)



            quality = result.get("quality", {})

            quality_score = quality.get("score") if quality else None

            # BUG-FIX 2026-09-16（低）：update_section_progress 若抛异常（如持久化
            # 层故障），下面的章节终态 queue.put 与 saved_count/failed_count 计数
            # 全部跳过 —— 前端该章节永久卡 generating 且计数少记。
            # 与 progress_callback 的保护保持一致：进度登记失败不阻断终态派发。
            try:

                await generation_manager.update_section_progress(

                    project_id,

                    sid,

                    status,

                    # BUG-FIX 2026-10-08（P2 数据不一致）：failed/cancelled 章节原先
                    # 也持久化 progress=100（SSE 事件侧是 0），断点续生/进度面板
                    # 读 DB 时失败章节显示 100% 进度，与 SSE 呈现矛盾。
                    100 if status == "completed" else 0,

                    content=content if status == "completed" else None,

                    quality_score=quality_score,

                    error=result.get("error") if status == "failed" else None,

                )

            except Exception as _prog_e:

                logger.warning(

                    "[正文生成] update_section_progress 异常 section=%s status=%s: %s",

                    sid, status, _prog_e,

                )



            async with results_lock:

                if status == "completed":

                    saved_count += 1

                elif status == "failed":

                    failed_count += 1

                elif status == "cancelled":

                    cancelled_count += 1



            wc = _count_words(content) if content else 0

            # CONVERGE 2026-10-12：单章节事件补字数目标/区间，让前端
            # ProgressPanel 直接显示「目标 N 字（lo~hi）· 实际 M 字 · 偏差 ±X」，
            # 无需二次请求字数统计页。口径全部来自唯一权威解析器 +
            # 强控覆盖表（_wc_override），与校验页 / R12 警告同源。
            _target = _target_lo = _target_hi = _target_source = None
            try:
                _ov = _wc_override.get(sid) if _wc_override else None
                if _ov:
                    _target = int(_ov.get("section_words") or 0)
                    _target_lo = int(_ov.get("lo") or 0)
                    _target_hi = int(_ov.get("hi") or 0)
                    _target_source = "strict"
                else:
                    from ...services.word_target_resolver import (
                        resolve_section_word_target_from_mapping as _resolve_wt,
                    )

                    _sec_for_target = sections_by_id.get(sid) or {}
                    _resolved = _resolve_wt(_sec_for_target, writing_length=writing_length)
                    if _resolved:
                        _target = int(_resolved.target)
                        _target_lo = int(_resolved.lo)
                        _target_hi = int(_resolved.hi)
                        _target_source = _resolved.source
                    else:
                        # 兜底：未命中强控/解析器时使用章节默认目标
                        _sec = sections_by_id.get(sid) or {}
                        _lvl = _sec.get("level", 3)
                        _default_t = 500 if _lvl == 1 else 300
                        _target = _default_t
                        _target_lo = int(max(0, _default_t * 0.8))
                        _target_hi = _default_t
                        _target_source = "fallback_default"
            except Exception as _tgt_e:
                logger.debug("[字数目标] 章节 %s 目标解析失败（忽略）: %s", sid, _tgt_e)
                # 兜底：确保即使出错也有默认值，防止 frontend 显示 NaN
                _target = 300
                _target_lo = 240
                _target_hi = 300
                _target_source = "error_fallback"


            await queue.put(

                {

                    "section_id": sid,

                    "title": title,

                    "status": status,

                    "progress": 100 if status == "completed" else 0,

                    "message": "已自动保存" if status == "completed" else result.get("error", ""),

                    "word_count": wc,

                    "saved": status == "completed",

                    "word_count_target": _target,
                    "word_count_target_lo": _target_lo,
                    "word_count_target_hi": _target_hi,
                    "word_count_target_source": _target_source,

                }

            )

            # BUG-P2b FIX 2026-10-11：整体进度事件推送。
            # 此前前端只能靠单章节事件硬算整体进度，感知延迟 ≈ 下一个章节完成
            # 才能跳一次 → "日志延迟错位"感。
            # 现每有章节终态（success/fail/cancel），推送一条 overall_progress：
            #   {"status":"overall_progress","saved":N,"failed":F,"total":T,"percent":P}
            # 节流：同 percent 值 2s 内不重复推送，避免噪声。
            import time as _time
            # leaf_dicts 是外层 generate_content_impl 的闭包变量，正常情况下恒存在；
            # 但 section_complete_callback 也可能在极端边界（如初始阶段回调）被调用，
            # 加 try/except NameError 兜底，防止死代码被移除后暴露隐患。
            # 使用 leaf_dicts 闭包变量确保 total 计数与章节生成列表一致。
            # 始终在 leaf_dicts 定义后（第 360-378 行）才进入主循环，已通过
            # generate_content_impl 的结构保证 leaf_dicts 恒定；若因极端边界（如
            # 生成早期回调）仍未定义，兜底使用 0 并由 max(_,1) 避免零Division。
            _total = len(leaf_dicts) if isinstance(leaf_dicts, list) else 0
            _done = saved_count + failed_count + cancelled_count
            _percent = round(_done / max(_total, 1) * 100)
            _now = _time.time()
            if _now - _last_overall_progress_event_at >= _OVERALL_PROGRESS_THROTTLE_SECS or _last_overall_progress_event_at == 0:
                _last_overall_progress_event_at = _now
                await queue.put({
                    "status": "overall_progress",
                    "saved": saved_count,
                    "failed": failed_count,
                    "cancelled": cancelled_count,
                    "chart_enqueued": chart_enqueued_count,
                    "chart_processed": chart_enqueued_count - chart_queue.qsize(),  # BUG-P3-A FIX
                    "chart_queue_size": chart_queue.qsize(),
                    "total": _total,
                    "done": _done,
                    "percent": _percent,
                })



        finished = False

        error_sent = False

        stopped = False

        client_disconnected = False



        async def run_generation():

            nonlocal finished, error_sent, stopped, saved_count, failed_count, cancelled_count

            _gen_total_start = time.time()

            logger.info(

                "[性能] 正文生成任务启动 project=%d, 章节数=%d", project_id, len(leaf_dicts)

            )



            # 在开始生成前检查取消事件（关键修复）

            if cancel_event and cancel_event.is_set():

                finished = True

                # BUG-FIX 2026-10-08（P2 状态错标）：早退分支原先只置 finished——
                # SSE 主循环收到 stopped 事件 break 后，finally 走
                # `elif finished: finalize_task("completed")`，把用户主动停止
                # 写成 completed 入库。与下方 CancelledError 分支（置 stopped）
                # 对齐。
                stopped = True

                # BUG-FIX 2026-10-08（P1 worker 泄漏）：本路径位于 try@1090 之外，
                # 内层 finally 不会执行，先收敛配图 worker 再退出。
                await _converge_chart_workers()

                await queue.put({"status": "stopped", "message": "生成已取消"})

                return



            # 内容编排：如果启用，先让 AI 判断每个章节的内容类型

            if use_orchestration:

                try:

                    from ...services.ai.workflows import orchestrate_content as _orchestrate
                    from ...services.ai.workflows_orchestration import (
                        apply_orchestration_to_leaves,
                        generate_full_skeleton,
                    )

                    # ENHANCE 2026-10-09（E-2）：知识素材通道 —— 加载项目可用素材的
                    # 轻量目录（id/标题/摘要），交给编排 AI 按章节挑选（非 RAG：
                    # 不给全文，只给清单，正文阶段按选中 id 再取全文）。
                    _knowledge_catalog = []
                    try:
                        from ...services import knowledge as _kb_svc
                        _kb_items = _kb_svc.get_relevant_items(limit=30)
                        for _it in _kb_items or []:
                            try:
                                _kid = int(_it.get("id"))
                            except (TypeError, ValueError):
                                continue
                            _knowledge_catalog.append({
                                "id": _kid,
                                "title": str(_it.get("title") or "").strip()[:60],
                                "summary": str(_it.get("summary") or "").strip()[:120],
                            })
                    except Exception as _kb_e:
                        logger.warning("知识素材目录加载失败（跳过 knowledge 编排）: %s", _kb_e)

                    # ENHANCE 2026-10-14（facts-ns P0，对齐易标两阶段消费）：
                    # 编排阶段 AI 只看事实标题清单选出 plan.facts.titles，
                    # 生成阶段按标题取完整事实注入。开关默认关=不传清单，
                    # plan.facts.titles 恒为空，行为与今天完全一致。
                    _fact_titles_for_orch: list = []
                    try:
                        from ...services.facts_service import (
                            fact_titles_for_orchestration,
                            orchestrated_select_enabled,
                        )
                        if orchestrated_select_enabled() and global_facts:
                            _fact_titles_for_orch = fact_titles_for_orchestration(global_facts)
                    except Exception as _ft_e:
                        logger.warning("事实标题清单装配失败（跳过 facts 编排）: %s", _ft_e)



                    # BUG 修复：此前直接 json.dumps(build_tree(...))——build_tree 返回

                    # SectionTreeNode（pydantic BaseModel）列表，json.dumps 必抛

                    # TypeError: Object of type SectionTreeNode is not JSON serializable，

                    # 异常被 except 吞掉后仅记 warning，导致内容编排功能自上线以来

                    # 静默失效。此处直接复用外层已转换好的 tree_dict。

                    # 标书智能体（六）完整编排调用：直接传 leaf_dicts（已转为 list[dict]），
                    # 不再序列化为 JSON 字符串——编排实现需要 list 迭代，而非 str。
                    # E-7.4：AI 生图成本拦截 —— 编排入口先做全局配置钳制
                    # （is_image_enabled=False → 0；max_images_per_project 配额 → min）
                    try:
                        from ...services.ai.image_engine import resolve_image_budget

                        _max_ai_images_eff = resolve_image_budget(orch_max_ai_images, project_id)
                        if _max_ai_images_eff != orch_max_ai_images:
                            logger.info(
                                "[正文生成] AI 生图上限被全局配置钳制: %s → %s",
                                orch_max_ai_images, _max_ai_images_eff,
                            )
                    except Exception:
                        _max_ai_images_eff = orch_max_ai_images

                    orchestration_result = await _orchestrate(

                        provider, leaf_dicts,

                        # BUG-FIX 2026-10-04（P2·BUG-D）：scoring_points 本就是 DB 里的
                        # JSON 字符串，旧实现 json.dumps 二次编码会把引号全部转义，
                        # 渲染进提示词后浪费 1000 字截断预算并破坏可读性。
                        # 与正文生成路径（scoring_standards=scoring_points）口径一致。
                        scoring_standards=scoring_points or "",

                        project_overview=summary,

                        children_map=children_map,

                        concurrency=min(2, actual_concurrency),

                        cancel_event=cancel_event,

                        max_ai_images=_max_ai_images_eff,

                        table_ratio=(orch_table_ratio if orch_table_ratio in TABLE_RATIO_VALID
                                     else "moderate"),

                        prefer_mermaid=orch_prefer_mermaid,

                        section_per_segment=orch_section_per_segment,

                        knowledge_catalog=_knowledge_catalog,

                        fact_titles=_fact_titles_for_orch or None,

                    )

                    # 将编排结果合并到 leaf_dicts。

                    # 关键：AI 返回顺序不保证与 leaf_dicts 一致，禁止按数组下标对齐，

                    # 否则会张冠李戴。改为按 section_id（优先）/title（兜底）精确匹配。

                    # 标书智能体（六）：用统一函数合并编排结果
                    # 同时写入 plan(table/mermaid/image 决策) + illustration_type + section_type
                    if isinstance(orchestration_result, list):
                        apply_orchestration_to_leaves(leaf_dicts, orchestration_result)

                        # E-2：按编排选定的 knowledge.item_ids 解析素材正文写回叶子，
                        # 供正文生成注入（build_chapter_content_messages 读 chapter_info）。
                        if _knowledge_catalog:
                            _cat_ids = {it["id"] for it in _knowledge_catalog}
                            for _leaf in leaf_dicts:
                                _kn_ids = ((_leaf.get("plan") or {}).get("knowledge") or {}).get("item_ids") or []
                                _sel = []
                                for _kid in _kn_ids:
                                    try:
                                        _kid_i = int(_kid)
                                    except (TypeError, ValueError):
                                        continue
                                    if _kid_i not in _cat_ids:
                                        continue
                                    _meta = next((it for it in _knowledge_catalog if it["id"] == _kid_i), None)
                                    if _meta is None:
                                        continue
                                    _content_txt = _meta["summary"]
                                    try:
                                        _full = _kb_svc.get_item(_kid_i)
                                        if isinstance(_full, dict) and _full.get("content"):
                                            _content_txt = str(_full["content"])[:800]
                                    except Exception:
                                        pass
                                    _sel.append({"id": _kid_i, "title": _meta["title"], "content": _content_txt})
                                    try:
                                        _kb_svc.record_usage(_kid_i, project_id, _leaf.get("id"), "content_orchestration")
                                    except Exception:
                                        pass
                                if _sel:
                                    _leaf["knowledge_items"] = _sel

                        # E-4：全篇叙事骨架 —— 一次生成、随叶子下发（best-effort，
                        # 失败不阻塞主流程）。骨架内容同项目内稳定，不影响缓存前缀。
                        try:
                            _skeleton = await generate_full_skeleton(
                                provider, leaf_dicts, project_overview=summary,
                                cancel_event=cancel_event,
                            )
                            if _skeleton:
                                for _leaf in leaf_dicts:
                                    _leaf["full_skeleton"] = _skeleton
                        except Exception as _sk_e:
                            logger.warning("全篇骨架生成失败（跳过）: %s", _sk_e)

                except asyncio.CancelledError:

                    # BUG-FIX 2026-10-08（P1 worker 泄漏）：取消落在编排 await 上
                    # （位于 try@1090 之外），内层 finally 不执行——先收敛配图
                    # worker 再继续抛出。
                    await _converge_chart_workers()

                    raise

                except Exception as e:

                    logger.warning("内容编排失败，继续使用默认类型: %s", e)



            try:

                await asyncio.to_thread(_mark_sections_generating, leaf_ids_for_generation)

                await queue.put(

                    {

                        "status": "started",

                        "total": len(leaf_dicts),

                        "section_ids": leaf_ids_for_generation,

                    }

                )



                # 字数采样预估：采样头+中+尾3个小节，预估总字数

                if use_word_sampling and len(leaf_dicts) >= 3:

                    try:

                        from ...services.ai.workflows import (

                            estimate_total_words_from_probe as _estimate,

                        )



                        probe_indices = [0, len(leaf_dicts) // 2, len(leaf_dicts) - 1]

                        probe_targets = [leaf_dicts[i] for i in probe_indices]

                        estimated_words = await _estimate(

                            provider,

                            probe_targets,

                            tender_context,

                            summary,

                            industry_profile,

                            scoring_points_list,

                            writing_length,

                            cancel_event=cancel_event,

                            project_id=project_id,

                            total_leaves=len(leaf_dicts),

                        )

                        total_target = sum(

                            ld.get("target_words") or 300

                            for ld in leaf_dicts

                        )

                        if estimated_words > 0 and estimated_words < total_target * 0.8:

                            logger.info(

                                "字数采样预估 %d 字，目标 %d 字，不足80%，触发补目录",

                                estimated_words,

                                total_target,

                            )

                            await queue.put(

                                {

                                    "status": "word_sampling",

                                    "estimated_words": estimated_words,

                                    "target_words": total_target,

                                    "ratio": estimated_words / total_target

                                    if total_target > 0

                                    else 0,

                                    "message": f"采样预估约 {estimated_words} 字，目标约 {total_target} 字，准备补充目录",

                                }

                            )

                    except Exception as e:

                        logger.warning("字数采样失败: %s", e)



                gen_start_ts = time.time()

                logger.info(

                    "[性能] 开始并发生成 %d 个章节, 并发度=%d, 评分点类型=%s",

                    len(leaf_dicts),

                    actual_concurrency,

                    type(scoring_points_list).__name__,

                )

                # P0-TEXT-FLOW: 传入完整 sections_by_id + children_map，
                # 让 generate_sections 内部为每个叶子节点计算 siblings/parents，
                # 注入 prompt 防车轱辘话、承上启下。
                results = await _run_sections(

                    provider,

                    leaf_dicts,

                    tender_context=tender_context,

                    summary=summary,

                    industry_profile=industry_profile,

                    scoring_standards=scoring_points,

                    writing_length=writing_length,

                    concurrency=actual_concurrency,

                    progress_callback=progress_callback,

                    section_complete_callback=section_complete_callback,

                    cancel_event=cancel_event,

                    global_facts=global_facts,

                    project_id=project_id,

                    sections_by_id=sections_by_id,

                    children_map=children_map,

                    construction_method=construction_method,

                    field_contract=field_contract,

                    field_trace=field_trace,

                    industry=industry,

                    # 易标 R5/R8：强控覆盖表（空表 = 未强控，下游回落档位逻辑）
                    word_control=_wc_override,

                    # ENHANCE 2026-10-14（facts-ns P1-3）：缺值策略透传
                    facts_policy=facts_policy,

                )

                # === BUG-C-04 修复：失败章节指数退避重试 ===
                # 主生成完成后，挑出 failed 章节做最多 2 轮重试（退避 1s→2s）
                # 成功则更新 results/DB/SSE，彻底消除"失败章节静默跳过"
                _MAX_RETRY_ROUNDS = 2
                _RETRY_BACKOFF_BASE = 1.0  # 秒，第二轮翻倍
                # BUG-FIX 2026-10-07（限流风暴）：日志实证 22:34~22:41 连续 7 分钟、
                # 每分钟 70~97 次 HTTP 429，累计 528 条，章节生成全军覆没。
                # 原实现对**所有**失败一律 1s→2s 退避并以全并发重跑：限流窗口
                # 尚未恢复时，这等于在限流之上再叠加一整波冲击，形成正反馈。
                # 现在按失败原因分流：命中限流特征时用分钟级退避并把重试并发减半。
                _RATE_LIMIT_RETRY_BACKOFF_BASE = 20.0
                _failed_section_ids = {
                    r["id"] for r in results if r.get("status") == "failed" and r.get("id")
                }
                _retry_backoff_base = _RETRY_BACKOFF_BASE
                _retry_concurrency = actual_concurrency
                if _failed_section_ids:
                    _throttled = any(
                        _is_rate_limit_error(str(r.get("error") or ""))
                        for r in results
                        if r.get("status") == "failed"
                    )
                    if _throttled:
                        _retry_backoff_base = _RATE_LIMIT_RETRY_BACKOFF_BASE
                        _retry_concurrency = max(1, actual_concurrency // 2)
                        logger.warning(
                            "[正文生成] 失败原因为 API 限流(429)，重试策略切换为分钟级退避 "
                            "(基准 %.0fs) + 并发减半 %d→%d",
                            _retry_backoff_base, actual_concurrency, _retry_concurrency,
                        )
                if _failed_section_ids and not cancel_event.is_set():
                    _retry_sections = [
                        ld for ld in leaf_dicts if ld.get("id") in _failed_section_ids
                    ]
                    if _retry_sections:
                        await queue.put({
                            "status": "retry_round",
                            "round": 0,
                            "total_rounds": _MAX_RETRY_ROUNDS,
                            "retry_count": len(_retry_sections),
                            "message": f"主生成失败 {len(_retry_sections)} 章，开始重试 Round 1/{_MAX_RETRY_ROUNDS}",
                        })
                        for _round in range(_MAX_RETRY_ROUNDS):
                            if cancel_event.is_set():
                                break
                            _backoff = _retry_backoff_base * (2 ** _round)
                            logger.info(
                                "[正文生成] BUG-C-04 重试 Round %d/%d, 退避 %.1fs, 章节数=%d, 并发=%d",
                                _round + 1, _MAX_RETRY_ROUNDS, _backoff, len(_retry_sections),
                                _retry_concurrency,
                            )
                            await asyncio.sleep(_backoff)
                            try:
                                _retry_results = await _run_sections(
                                    provider,
                                    _retry_sections,
                                    tender_context=tender_context,
                                    summary=summary,
                                    industry_profile=industry_profile,
                                    scoring_standards=scoring_points,
                                    writing_length=writing_length,
                                    concurrency=_retry_concurrency,
                                    progress_callback=progress_callback,
                                    section_complete_callback=section_complete_callback,
                                    cancel_event=cancel_event,
                                    global_facts=global_facts,
                                    project_id=project_id,
                                    sections_by_id=sections_by_id,
                                    children_map=children_map,
                                    construction_method=construction_method,
                                    field_contract=field_contract,
                                    field_trace=field_trace,
                                    industry=industry,
                                    word_control=_wc_override,
                                    # ENHANCE 2026-10-14（facts-ns P1-3）：缺值策略透传
                                    facts_policy=facts_policy,
                                )
                                # 更新 results 中对应条目的状态（重试成功的替换掉 failed 条目）
                                _retry_success_ids = {
                                    r["id"] for r in _retry_results if r.get("status") == "completed"
                                }
                                for i, r in enumerate(results):
                                    if r.get("id") in _retry_success_ids:
                                        for rr in _retry_results:
                                            if rr.get("id") == r["id"]:
                                                results[i] = rr
                                                break
                                # 更新 remaining 列表进入下一轮（只重试仍 failed 的）
                                _still_failed = [
                                    r for r in _retry_results if r.get("status") == "failed"
                                ]
                                if not _still_failed:
                                    logger.info("[正文生成] BUG-C-04 重试 Round %d 全部成功", _round + 1)
                                    break
                                _retry_sections = [
                                    ld for ld in _retry_sections
                                    if ld.get("id") in {r["id"] for r in _still_failed}
                                ]
                                if not _retry_sections:
                                    break
                                await queue.put({
                                    "status": "retry_round",
                                    "round": _round + 1,
                                    "total_rounds": _MAX_RETRY_ROUNDS,
                                    "retry_count": len(_retry_sections),
                                    "message": f"重试 Round {_round + 1} 仍失败 {len(_retry_sections)} 章，继续 Round {_round + 2}/{_MAX_RETRY_ROUNDS}",
                                })
                            except Exception as _retry_err:
                                logger.warning("[正文生成] BUG-C-04 重试 Round %d 异常: %s", _round + 1, _retry_err)
                                continue

                # 重新统计计数（重试后 updated）
                saved_count = sum(1 for r in results if r.get("status") == "completed")
                failed_count = sum(1 for r in results if r.get("status") == "failed")

                gen_elapsed = time.time() - gen_start_ts

                logger.info(

                    "[性能] 正文生成完成, 耗时 %.2fs, 成功=%d, 失败=%d, 平均AI响应=%.2fs/节",

                    gen_elapsed,

                    saved_count,

                    failed_count,

                    gen_elapsed / max(saved_count + failed_count, 1),

                )



                if saved_count > 0:

                    await asyncio.to_thread(update_step, project_id, 5)

                # 字数偏差控制（2026-10-11 新增）：对超长章节自动压缩
                # 缺口：DB 实测 10 个 L3 章节平均偏差 +34.2%、100% 系统性偏高。
                # 修复：主生成完成后、兜底扩写之前，对超出 target × threshold
                # 的章节调用 condense_overlong_results 压缩到目标区间。
                # 失败安全：压缩失败/超时/取消均保留原文；异常向上被捕获，不影响主生成。
                try:
                    from ...services.ai.workflows_sections import condense_overlong_results
                    _condense_summary = await condense_overlong_results(
                        results, sections_by_id, provider, writing_length,
                        # 易标 R5：强控生效时按快照 S ±20% 压缩（空表回落档位逻辑）
                        word_control=_wc_override,
                        cancel_event=cancel_event,
                    )
                    if _condense_summary.get("condensed_count", 0) > 0:
                        logger.info(
                            "[字数偏差控制] 压缩完成: %d/%d 章节, 失败=%d",
                            _condense_summary.get("condensed_count", 0),
                            saved_count,
                            _condense_summary.get("fail_count", 0),
                        )
                        # BUG-FIX 2026-10-12（P1 缓存未失效 / 压缩不落库）：
                        # condense_overlong_results 只在内存 results 列表上就地
                        # 改写 content 字段，此前既不写 sections 表也不失效任何
                        # 一层缓存（内存 1h / Redis 30min / build_tree 60s）。
                        # 用户刷新页面后从 DB 读回的仍是压缩前长版正文，前端
                        # word_count_condense 事件里"压缩 N 章节"的提示与页面
                        # 显示矛盾；同一项目其他 60 分钟窗口内的读接口全部命中
                        # 旧缓存。修复：与兜底扩写同构，批量 executemany UPDATE
                        # 后调用统一三层失效入口。condense_overlong_results 仅
                        # 在压缩成功分支写入 `original_content` 字段（见
                        # workflows_sections.py 1385 行），据此筛选压缩章节。
                        # HARDEN 2026-10-12：优先使用 condense_overlong_results
                        # 显式返回的 condensed_ids 契约（精确、无歧义）；若上游
                        # 版本尚未提供该字段，回退到旧的隐式判别（original_content
                        # != content）以维持向后兼容。
                        _condensed_ids = set(
                            _condense_summary.get("condensed_ids") or []
                        )
                        _condense_updates: list[tuple[str, int]] = []
                        _r_indexed = False
                        if _condensed_ids:
                            # 显式契约路径：O(1) 查找、精确落库
                            _by_id = {r.get("id"): r for r in results}
                            for _sid in _condensed_ids:
                                _r = _by_id.get(_sid)
                                if not _r or _r.get("status") != "completed":
                                    continue
                                _new = _r.get("content")
                                if _new is None:
                                    continue
                                _condense_updates.append((_new, _sid))
                            _r_indexed = True
                        if not _r_indexed:
                            # 兼容回退：隐式判别（旧路径）
                            for _r in results:
                                if _r.get("status") != "completed":
                                    continue
                                _orig = _r.get("original_content")
                                _new = _r.get("content")
                                if not _orig or _new is None or _orig == _new:
                                    continue
                                _condense_updates.append((_new, _r["id"]))
                        if _condense_updates:

                            def _batch_update_condensed(updates):
                                from ...db import get_db
                                from ...services.sections import _count_readable_words, _count_words

                                with get_db() as conn:
                                    conn.executemany(
                                        "UPDATE sections SET content = ?, word_count = ?, readable_word_count = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
                                        [
                                            (_c, _count_words(_c), _count_readable_words(_c), _sid)
                                            for _c, _sid in updates
                                        ],
                                    )

                            await asyncio.to_thread(_batch_update_condensed, _condense_updates)
                            try:
                                from ...services.sections import invalidate_content_caches

                                invalidate_content_caches(
                                    project_id, [_sid for _c, _sid in _condense_updates]
                                )
                            except Exception as _inv_e:
                                logger.warning(
                                    "[字数偏差控制] 三层缓存失效失败（忽略）: %s", _inv_e
                                )
                        await queue.put({
                            "status": "word_count_condense",
                            "message": f"字数偏差控制完成：{_condense_summary.get('condensed_count', 0)} 章节压缩",
                            "condensed_count": _condense_summary.get("condensed_count", 0),
                            "fail_count": _condense_summary.get("fail_count", 0),
                            "saved_to_db": len(_condense_updates),
                        })
                except Exception as _we:
                    logger.warning("[字数偏差控制] 整体流程异常（忽略，不影响主生成）: %s", _we)



                # 兜底扩写：对字数最少的章节进行扩写

                if use_fallback_expand and saved_count > 0 and not cancel_event.is_set():

                    try:

                        # BUG-F-13 修复：expand_content_fallback 实际在 workflows_expand 模块

                        # 原代码从 workflows 导入 → ImportError

                        from ...services.ai.workflows_expand import (

                            expand_content_fallback as _expand_fallback,

                        )



                        # 兜底扩写阈值：基于各章节 effective target_words 的 80%，
                        # 确保扩写不会停在远低于章节目标的位置
                        _section_effective_targets = []
                        for _r in results:
                            _sid = _r.get("id")
                            # 易标 R5：强控生效时以快照 S 为基准（S×80% 恰为区间下限），
                            # 保证扩写不会停在强控区间之外。
                            _ov = _wc_override.get(_sid) if _wc_override else None
                            if _ov:
                                _section_effective_targets.append(int(_ov["section_words"]))
                                continue
                            if _sid and _sid in sections_by_id:
                                _sec = sections_by_id[_sid]
                                _lvl = _sec.get("level", 3)
                                if _lvl >= 2:
                                    # 二级目录及以下：与正文生成一致，由写作长度模式控制
                                    _section_effective_targets.append(level_target(writing_length, _lvl, 300))
                                elif _tw := _sec.get("target_words"):
                                    _section_effective_targets.append(_tw)
                                else:
                                    _section_effective_targets.append(500 if _lvl == 1 else 300)

                        if _section_effective_targets:
                            # 所有章节都应至少达到各自 target 的 80%
                            minimum_words = int(max(_section_effective_targets) * 0.80)
                            min_words = int(min(_section_effective_targets) * 0.80)
                            logger.info("兜底扩写阈值按章节 target 动态计算: 最低章节 80%%=%d, 最高章节 80%%=%d",
                                        min_words, minimum_words)
                        else:
                            # 兜底：用写作长度模式下的二级目录目标（中间值）
                            minimum_words = level2_target(writing_length)

                        expand_start = time.time()

                        # 持久化修复（P1 2026-09-16）：先快照扩写前原文。
                        # expand_content_fallback 会就地修改 results 中章节的 content，
                        # 若事后才从 results 取"原文"，取到的已是扩写后内容，
                        # 下方 batch_updates 将恒为空、扩写结果永远落不了库。
                        _results_original_by_id = {
                            r["id"]: r.get("content", "") for r in results
                        }

                        expanded = await _expand_fallback(

                            provider,

                            results,

                            sections_by_id,

                            minimum_words,

                            tender_context,

                            summary,

                            industry_profile,

                            writing_length,

                            cancel_event=cancel_event,

                            # ENHANCE 2026-09-16：把生成流程的并发度与 level2 目标
                            # 透传下去。旧实现漏传 → expand_content_fallback 退回
                            # 默认并发 2 且按 level2_default=3500 分档判阈值，
                            # 与「生成后自动扩写」应有的口径不一致（慢了 N 倍）。
                            concurrency=actual_concurrency,

                            level2_default=level2_default,

                        )

                        expand_elapsed = time.time() - expand_start

                        expanded_count = sum(1 for r in expanded if r.get("status") == "completed")

                        logger.info(

                            "[性能] 兜底扩写完成, 耗时 %.2fs, 扩写章节=%d/%d",

                            expand_elapsed,

                            expanded_count,

                            len(expanded),

                        )

                        # PERF-2 修复：原循环逐条 update_section 产生大量小事务，

                        # 改为收集后一次性批量更新，减少 SQLite WAL 刷盘开销。

                        # PERF 修复：O(n²) 查找改为字典索引（300+ 章节时原实现

                        # 每章节全量扫描 results）

                        _results_content_by_id = _results_original_by_id

                        batch_updates = []

                        for r in expanded:

                            if r.get("status") == "completed" and r.get("content"):

                                original = _results_content_by_id.get(r["id"], "")

                                if len(r.get("content", "")) > len(original):

                                    batch_updates.append((r["content"], r["id"]))

                        if batch_updates:



                            def _batch_update_content(updates):

                                from ...db import get_db

                                from ...services.sections import _count_readable_words, _count_words



                                with get_db() as conn:

                                    conn.executemany(

                                        "UPDATE sections SET content = ?, word_count = ?, readable_word_count = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",

                                        [
                                            (_c, _count_words(_c), _count_readable_words(_c), _sid)
                                            for _c, _sid in updates
                                        ],

                                    )



                            await asyncio.to_thread(_batch_update_content, batch_updates)

                            # BUG-FIX 2026-09-18（P1）：此处裸 SQL 直写正文，与
                            # 一致性 autofix/版本回滚同类旁路，但此前既不更新
                            # word_count 列（上方 SQL 已补），也不失效任何一层正文
                            # 缓存 → 内存缓存(1h)里的扩写前旧正文会回灌后续读。
                            # 统一走三层失效入口。
                            try:

                                from ...services.sections import invalidate_content_caches

                                invalidate_content_caches(

                                    project_id, [_sid for _c, _sid in batch_updates]

                                )

                            except Exception as _inv_e:

                                logger.warning("[兜底扩写] 三层缓存失效失败（忽略）: %s", _inv_e)

                        results = expanded

                        # BUG 修复：扩写后重新统计计数，避免 completed 事件报告的 success 数量不准确

                        saved_count = sum(1 for r in results if r.get("status") == "completed")

                        failed_count = sum(1 for r in results if r.get("status") == "failed")

                        # SSE payload 对称化（2026-10-12）：此前本事件只有
                        # status/message，而 word_count_condense 带 5 个统计字段，
                        # 前端只能用固定文案 toast，用户看不到「扩了几节、落库几节」。
                        await queue.put({
                            "status": "fallback_expand",
                            "message": "兜底扩写完成：{} 节扩写，落库 {} 节".format(
                                expanded_count, len(batch_updates)
                            ),
                            "expanded_count": expanded_count,
                            "deficient_total": len(expanded),
                            "saved_to_db": len(batch_updates),
                        })

                    except Exception as e:

                        logger.warning("兜底扩写失败: %s", e)



# WIRING-2026-10-12：易标 R9/R10/R11 字数双向多轮调整
                # 易标 R9/R10/R11 生产接线（2026-10-12）：
                #   R9  强控小节双向拉回（每节 ≤3 轮，额度 |当前−S|）
                #   R10 全文扩写 / 缩写（按比例分配额度；缩写 ≤3 轮；
                #        扩写连续 3 轮无进展即停）
                #   R11 每次结果六道硬校验（不通过即保留原文）
                # 排在 condense / 兜底扩写之后：先做「一次性压缩」与「兜底补齐」，
                # 再按额度做精调，最后由 R12 统一出警告，语义与易标正文阶段一致。
                # 启用判定：显式参数 > （强控自动启用 且 项目已配置绝对字数目标）
                #         > config 默认值；任一环节异常都只记日志，不影响主生成。
                _wc_adjust_enabled = False
                try:
                    from ...config import (
                        CONTENT_USE_WORD_ADJUSTMENT_DEFAULT,
                        CONTENT_WORD_ADJUST_AUTO_ON_STRICT,
                        CONTENT_WORD_ADJUST_CONCURRENCY,
                        CONTENT_WORD_ADJUST_MAX_ROUNDS,
                    )

                    if use_word_adjust is None:
                        _wc_adjust_enabled = bool(
                            CONTENT_USE_WORD_ADJUSTMENT_DEFAULT
                            or (
                                CONTENT_WORD_ADJUST_AUTO_ON_STRICT
                                and _wc_override
                                and _wc_state.effective_options
                            )
                        )
                    else:
                        _wc_adjust_enabled = bool(use_word_adjust)
                except Exception as _adj_cfg_e:
                    logger.warning("[字数调整] 配置读取失败（跳过该阶段）: %s", _adj_cfg_e)
                    _wc_adjust_enabled = False

                if (
                    _wc_adjust_enabled
                    and saved_count > 0
                    and not cancel_event.is_set()
                ):
                    try:
                        from ...services.ai.workflows_word_adjust import (
                            run_word_adjustment_pipeline,
                        )

                        async def _on_adjust_summary(_stage: str, _summary: dict):
                            await queue.put({
                                "status": "word_adjust_progress",
                                "stage": _stage,
                                "message": (
                                    "字数强控拉回：调整 {}/{} 节，未达标 {} 节".format(
                                        _summary.get("adjusted_count", 0),
                                        _summary.get("checked", 0),
                                        len(_summary.get("unresolved_ids") or []),
                                    )
                                    if _stage == "section"
                                    else "全文{}：轮数 {}，调整 {} 节，总字数 {} → {}".format(
                                        "扩写"
                                        if _summary.get("direction") == "expand"
                                        else "缩写",
                                        _summary.get("rounds", 0),
                                        _summary.get("adjusted_count", 0),
                                        _summary.get("total_before", 0),
                                        _summary.get("total_after", 0),
                                    )
                                ),
                                "direction": _summary.get("direction"),
                                "adjusted_count": _summary.get("adjusted_count", 0),
                                "fail_count": _summary.get("fail_count", 0),
                                "rounds": _summary.get("rounds", 0),
                                "checked": _summary.get("checked", 0),
                                "unresolved_count": len(
                                    _summary.get("unresolved_ids") or []
                                ),
                                "total_before": _summary.get("total_before"),
                                "total_after": _summary.get("total_after"),
                            })

                        _wc_adjust_summary = await run_word_adjustment_pipeline(
                            provider,
                            results,
                            sections_by_id,
                            options=_wc_state.effective_options,
                            word_control=_wc_override,
                            max_rounds=CONTENT_WORD_ADJUST_MAX_ROUNDS,
                            concurrency=min(
                                CONTENT_WORD_ADJUST_CONCURRENCY,
                                max(1, actual_concurrency),
                            ),
                            cancel_event=cancel_event,
                            on_summary=_on_adjust_summary,
                        )
                        # 落库：与 condense / 兜底扩写同构（批量 UPDATE + 三层失效）。
                        # BUG-FIX 2026-10-12 同源教训：调整只改内存 results 而不落库 /
                        # 不失效缓存，会让用户刷新看到调整前正文、AI 压缩成本白花。
                        _adjust_ids = set(_wc_adjust_summary.get("adjusted_ids") or [])
                        _adjust_updates = [
                            (_r.get("content"), _r.get("id"))
                            for _r in results
                            if _r.get("id") in _adjust_ids
                            and _r.get("status") == "completed"
                            and _r.get("content")
                            and _r.get("word_count_adjusted")
                        ]
                        if _adjust_updates:

                            def _batch_update_adjusted(updates):
                                from ...db import get_db
                                from ...services.sections import (
                                    _count_readable_words,
                                    _count_words,
                                )

                                with get_db() as conn:
                                    conn.executemany(
                                        "UPDATE sections SET content = ?, word_count = ?, readable_word_count = ?, updated_at = datetime('now', 'localtime') WHERE id = ?",
                                        [
                                            (
                                                _c,
                                                _count_words(_c),
                                                _count_readable_words(_c),
                                                _sid,
                                            )
                                            for _c, _sid in updates
                                        ],
                                    )

                            await asyncio.to_thread(
                                _batch_update_adjusted, _adjust_updates
                            )
                            try:
                                from ...services.sections import invalidate_content_caches

                                invalidate_content_caches(
                                    project_id, [_sid for _c, _sid in _adjust_updates]
                                )
                            except Exception as _adj_inv_e:
                                logger.warning(
                                    "[字数调整] 三层缓存失效失败（忽略）: %s", _adj_inv_e
                                )
                        _sec_sum = _wc_adjust_summary.get("section") or {}
                        _tot_sum = _wc_adjust_summary.get("total") or {}
                        await queue.put({
                            "status": "word_adjust",
                            "message": "字数多轮调整完成：调整 {} 节（强控拉回 {}，全文{} {} 节）".format(
                                len(_wc_adjust_summary.get("adjusted_ids") or []),
                                _sec_sum.get("adjusted_count", 0),
                                "扩写" if _tot_sum.get("direction") == "expand" else "缩写",
                                _tot_sum.get("adjusted_count", 0),
                            ),
                            "adjusted_count": len(
                                _wc_adjust_summary.get("adjusted_ids") or []
                            ),
                            "saved_to_db": len(_adjust_updates),
                            "section_adjusted": _sec_sum.get("adjusted_count", 0),
                            "section_failed": _sec_sum.get("fail_count", 0),
                            "section_unresolved": len(
                                _sec_sum.get("unresolved_ids") or []
                            ),
                            "total_direction": _tot_sum.get("direction", "none"),
                            "total_rounds": _tot_sum.get("rounds", 0),
                            "total_adjusted": _tot_sum.get("adjusted_count", 0),
                            "total_before": _tot_sum.get("total_before"),
                            "total_after": _tot_sum.get("total_after"),
                        })
                        logger.info(
                            "[字数调整] 完成：调整 %d 节，落库 %d 节",
                            len(_wc_adjust_summary.get("adjusted_ids") or []),
                            len(_adjust_updates),
                        )
                    except Exception as _adj_e:
                        logger.warning(
                            "[字数调整] 整体流程异常（忽略，不影响主生成）: %s", _adj_e
                        )


                # 易标 R12（字数控制 2026-10-12）：生成/压缩/扩写链全部结束后，
                # 逐节按 R1 可读口径重算实际字数，判定强控小节越界与全文偏离，
                # 结构化警告回写 word_control_settings.word_control_warning
                # 并推送 SSE「word_control_warning」事件；不硬裁正文，
                # 由前端弹窗展示「期望区间 vs 实际 + 逐节明细」交人工裁决。
                if saved_count > 0 and not cancel_event.is_set():

                    try:

                        from ...services.word_control_runtime import (
                            finalize_word_control_warning,
                        )

                        _word_control_warning = await asyncio.to_thread(
                            finalize_word_control_warning,
                            _wc_state,
                            results,
                            single_section=len(leaf_ids_for_generation) == 1,
                        )

                        if _word_control_warning:

                            await queue.put({
                                "status": "word_control_warning",
                                "warning": _word_control_warning,
                                "message": _word_control_warning.get("message", ""),
                            })

                    except Exception as _wc_warn_e:

                        logger.warning("[字数控制] 收尾警告计算失败（忽略）: %s", _wc_warn_e)



                # PERF-P0-B1: 等待配图流水线 worker 把队列里剩余章节跑完。

                # 配图已在 section_complete_callback 中与 LLM 并行进行，这里只做收尾。

                #

                # PERF-CHART-DRAIN 2026-09-08：原实现 asyncio.wait(..., timeout=120)

                # 单次阻塞 120s，期间不发任何 progress 事件，前端体感为"卡住"。

                # 改为：分轮轮询（每 5s 一次），每轮发 chart_draining 进度事件，

                # 60s 仍未清空则强制 cancel workers（不阻塞主流程）。

                if saved_count > 0 and not cancel_event.is_set():

                    try:

                        chart_drain_start = time.time()

                        chart_done_event.set()

                        CHART_DRAIN_TOTAL_TIMEOUT = 60  # 总收尾上限（秒）

                        CHART_DRAIN_TICK = 5           # 进度推送间隔（秒）

                        last_tick = 0.0

                        while True:

                            alive_workers = [w for w in chart_workers if not w.done()]

                            if not alive_workers and chart_queue.empty():

                                break

                            elapsed = time.time() - chart_drain_start

                            if elapsed >= CHART_DRAIN_TOTAL_TIMEOUT:

                                # BUG-FIX 2026-09-14（高）：原超时分支只打日志，
                                # 既不 break 也不 cancel worker。若某个配图 worker
                                # 卡在 AI 调用上，alive_workers 永不为空 → while True
                                # 每 5s 空转一次，SSE 流永不结束，前端永久转圈。

                                logger.warning(

                                    "[性能] 配图收尾超时 %ds，强制 cancel %d 个 worker",

                                    CHART_DRAIN_TOTAL_TIMEOUT, len(alive_workers),

                                )

                                for _w in alive_workers:

                                    _w.cancel()

                                break

                            # 每 5s 推一次进度事件，让前端能看到"配图收尾中..."

                            if elapsed - last_tick >= CHART_DRAIN_TICK:

                                last_tick = elapsed

                                try:

                                    await queue.put({

                                        "status": "chart_draining",

                                        "remaining": chart_queue.qsize(),

                                        "elapsed": int(elapsed),

                                        "message": f"配图收尾中，剩余 {chart_queue.qsize()} 章",

                                    })

                                except Exception as _e:

                                    logger.warning("[SSE] 静默捕获异常（BUG修复后应逐步消除）: %s", _e)

                            try:

                                # 短轮询：5s 后再看一眼

                                done, _pending = await asyncio.wait(

                                    alive_workers if alive_workers else chart_workers,

                                    timeout=CHART_DRAIN_TICK,

                                    return_when=asyncio.FIRST_COMPLETED,

                                )

                            except Exception:

                                break

                        chart_elapsed = time.time() - chart_drain_start

                        # 粗略估算：queue 里剩余了多少章节 = (原 completed 数 - 已被 worker 取走)

                        logger.info(

                            "[性能] 配图流水线收尾完成, 耗时 %.2fs, 队列剩余=%d",

                            chart_elapsed, chart_queue.qsize(),

                        )

                    except Exception as e:

                        logger.warning("配图流水线收尾异常: %s", e)

                    finally:

                        # 确保 worker 全部结束，不泄漏

                        for w in chart_workers:

                            if not w.done():

                                w.cancel()

                        # 给 cancel 一点时间收敛，但不阻塞主线

                        try:

                            await asyncio.wait_for(

                                asyncio.gather(*chart_workers, return_exceptions=True),

                                timeout=2.0,

                            )

                        except (TimeoutError, Exception):

                            pass



                    # 重新统计配图结果。
                    # BUG-FIX 2026-10-15（P3 日志口径错位）：原实现用 saved_count
                    # （所有落库成功章节数）充当「总入队」，并由此推导 worker 处理数。
                    # 这与本模块为 BUG-P3-A 引入的 chart_enqueued_count 口径分叉：
                    # 失败/取消章节根本不入队、chart_queue 满载被丢弃的章节也不入队，
                    # 于是「总入队」与「已处理」双双被高估，排错日志误导。

                    _chart_enqueued = chart_enqueued_count
                    remaining = chart_queue.qsize()
                    chart_processed = max(_chart_enqueued - remaining, 0)

                    logger.info(
                        "自动配图流水线完成: 总入队=%d, 已取走(含处理中)≈%d, "
                        "队列剩余=%d, 落库成功章节=%d",
                        _chart_enqueued, chart_processed, remaining, saved_count,
                    )



                # 全文一致性审计：检查正文与项目事实的冲突

                if use_consistency_audit and saved_count > 0 and not cancel_event.is_set():

                    try:

                        from ...services.ai.workflows import consistency_audit_content as _audit



                        # 持久化修复（P1 2026-09-16）：先快照审计前原文。
                        # consistency_audit_content 会就地修改 results 中章节的 content，
                        # 若事后才从 results 取"原文"，取到的已是修复后内容，
                        # 下方 update_section 将恒不触发、审计修复永远落不了库。
                        _audit_original_by_id = {
                            r["id"]: r.get("content", "") for r in results
                        }

                        audited = await _audit(

                            project_id,

                            provider,

                            results,

                            sections_by_id,

                            summary,

                            tender_context,

                            cancel_event=cancel_event,

                            # ENHANCE 2026-10-15（对齐易标 buildConsistencyAuditMessages）：

                            # 审计显式注入结构化全局事实 + 缺失值策略口径，作为一致性裁决基准。

                            global_facts=global_facts,

                            facts_policy=facts_policy,

                        )

                        # PERF 修复：O(n²) 查找改为字典索引

                        _audit_content_by_id = _audit_original_by_id

                        for r in audited:

                            if r.get("content"):

                                original = _audit_content_by_id.get(r["id"], "")

                                if len(r.get("content", "")) > 0 and r.get("content") != original:

                                    # 使用关键字参数捕获当前 r 的值，避免 lambda 闭包问题

                                    r_content = r["content"]

                                    r_id = r["id"]

                                    await asyncio.to_thread(

                                        update_section, r_id, SectionUpdate(content=r_content)

                                    )

                        results = audited

                        # SSE payload 对称化（2026-10-12）：补「审计了几节」统计，
                        # 前端可据此提示覆盖率，而不是黑盒 toast。基准取审计前的
                        # 原文快照 _audit_original_by_id（此处 results 已被整体
                        # 替换为 audited，拿它自比恒为 0）。
                        _audited_count = sum(
                            1
                            for _r in audited
                            if _r.get("id") is not None
                            and _r.get("content")
                            and _r.get("content")
                            != _audit_original_by_id.get(_r.get("id"))
                        )

                        await queue.put(

                            {
                                "status": "consistency_audit",
                                "message": "一致性审计完成：修订 {} 节".format(
                                    _audited_count
                                ),
                                "audited_count": _audited_count,
                            }

                        )

                    except Exception as e:

                        logger.warning("一致性审计失败: %s", e)



                # P2 修复：缓存统计与质量摘要必须在终止事件（completed/stopped）之前

                # 入队——SSE 消费端遇到无 section_id 的终止事件即 break，

                # 旧实现把二者放在终止事件之后，事件永远滞留队列无法送达客户端

                try:

                    from ...services.ai.workflows import _section_content_cache as cache



                    cache_stats = cache.get_stats()

                    await queue.put(

                        {

                            "status": "cache_stats",

                            **cache_stats,

                        }

                    )

                except Exception as e:

                    logger.warning("获取缓存统计失败: %s", e)



                try:

                    quality_scores = [

                        r.get("quality", {}).get("score", 0)

                        for r in results

                        if r.get("status") == "completed" and r.get("quality")

                    ]

                    if quality_scores:

                        avg_quality = sum(quality_scores) / len(quality_scores)

                        min_quality = min(quality_scores)

                        max_quality = max(quality_scores)

                        await queue.put(

                            {

                                "status": "quality_summary",

                                "avg_score": round(avg_quality, 1),

                                "min_score": round(min_quality, 1),

                                "max_score": round(max_quality, 1),

                                "evaluated_count": len(quality_scores),

                            }

                        )

                except Exception as e:

                    logger.warning("获取质量评估摘要失败: %s", e)



                if cancelled_count > 0 and saved_count == 0 and failed_count == 0:

                    stopped = True

                    await queue.put({"status": "stopped", "total": len(results), "success": 0})

                elif cancelled_count > 0:

                    stopped = True

                    await queue.put(

                        {

                            "status": "completed",

                            "total": len(results),

                            "success": saved_count,

                            "cancelled": cancelled_count,

                            "failed": failed_count,

                            "stopped": True,

                        }

                    )

                else:

                    await queue.put(

                        {

                            "status": "completed",

                            "total": len(results),

                            "success": saved_count,

                            "cancelled": 0,

                            "failed": failed_count,

                        }

                    )

            except asyncio.CancelledError:

                logger.info(

                    "[正文生成] 生成任务被取消 project=%d, 已保存=%d", project_id, saved_count

                )

                # BUG-FIX 2026-09-16（中）：用户「停止」被误记为 completed 的竞态。
                # 原实现只往队列发 stopped 事件、不置 stopped 标志：若 SSE 收尾
                # 先于主循环消费该事件执行（客户端断开/停止与收尾重叠），
                # finally 的 `elif finished: finalize_task("completed")` 会把用户
                # 主动停止的任务写成 completed，且与 stop_generation 的
                # finalize_task("cancelled") 形成双收尾竞态。run_generation 已声明
                # `nonlocal stopped`，这里置 True 后外层 finally 必走 cancelled 分支。
                stopped = True

                # 修复：即使捕获到 CancelledError，也要发送停止事件到队列，确保前端知道生成已被取消

                await queue.put(

                    {

                        "status": "stopped",

                        "total": len(leaf_dicts),

                        "success": saved_count,

                        "cancelled": cancelled_count,

                        "failed": failed_count,

                        "message": "生成已被用户停止",

                    }

                )

                # 注意：finished 将在 finally 块中设置为 True

            except Exception as e:

                import traceback



                traceback.print_exc()

                error_sent = True

                await queue.put({"status": "error", "message": str(e)})

            finally:

                finished = True

                # BUG-FIX 2026-09-14（高）：chart_done_event.set() 与 worker 的 cancel
                # 原先只写在 `if saved_count > 0 and not cancel_event.is_set()` 块内。
                # 当用户点停止、客户端断开、或全部章节生成失败时该块整块被跳过，
                # 2~4 个 _chart_worker 会以 2s 为周期无限空转到进程重启（CPU + FD 泄漏）。
                # 修复：在 run_generation 的 finally 里无条件收敛配图 worker。
                try:

                    chart_done_event.set()

                    for _w in chart_workers:

                        if not _w.done():

                            _w.cancel()

                    if chart_workers:

                        await asyncio.wait_for(

                            asyncio.gather(*chart_workers, return_exceptions=True),

                            timeout=2.0,

                        )

                except (TimeoutError, Exception):

                    pass

                _gen_total_elapsed = time.time() - _gen_total_start

                logger.info(

                    "[性能] 正文生成任务结束 project=%d, 总耗时 %.2fs, 成功=%d, 失败=%d, 取消=%d",

                    project_id,

                    _gen_total_elapsed,

                    saved_count,

                    failed_count,

                    cancelled_count,

                )

                # BUG 修复：任务结束后失效 Redis 章节缓存，

                # 确保下一次 _fetch_project_data_async 读取到最新 status/content

                # BUG-FIX 2026-09-18（P1）：升级为三层统一失效——原只失效 Redis，

                # 内存正文缓存(1h TTL)/build_tree 缓存仍可能回灌旧正文。

                try:

                    from ...services.sections import invalidate_content_caches

                    invalidate_content_caches(project_id)

                except Exception as _inv_e:

                    logger.warning("[正文生成] 章节缓存后失效失败（忽略）: %s", _inv_e)

                # BUG 修复：客户端断开后后台任务完成时仍需 finalize_task，

                # 否则 generation_manager._tasks 永远残留，内存泄漏 + 状态死锁

                # ENHANCE 2026-09-16：暂停中的任务同样不能在这里收尾 ——
                # 用户「暂停的同时关掉标签页」会让 client_disconnected=True 与
                # paused 同时成立，此处若 finalize 就会删掉断点续生现场。

                if (
                    client_disconnected
                    and not error_sent
                    and not stopped
                    and not await generation_manager.is_paused(project_id)
                ):

                    try:

                        _final_status = "completed" if saved_count > 0 else "failed"

                        await generation_manager.finalize_task(

                            project_id, task_status=_final_status

                        )

                        logger.info(

                            "[正文生成] 客户端断开但后台任务已完成，已清理 generation_manager project=%d",

                            project_id,

                        )

                    except Exception as _fin_e:

                        logger.warning("[正文生成] 后台任务完成后 finalize_task 失败: %s", _fin_e)

                async with _cancel_events_lock:

                    _evs = _cancel_events.get(project_id)

                    if _evs and cancel_event in _evs:

                        _evs.remove(cancel_event)

                        # BUG-FIX 2026-09-16（低）：空列表也要清键，防止残留

                        if not _evs:

                            _cancel_events.pop(project_id, None)



        task = asyncio.create_task(run_generation(), name=f"content-gen-{project_id}")



        # 注册任务到全局任务列表，以便 stop_generation 可以取消它

        async with _cancel_events_lock:

            _cancel_tasks.setdefault(project_id, []).append(task)



        # P0 修复 2026-09-29：last_heartbeat_time 原先被赋值但**从未被读取**，
        # 导致心跳完全不受节流（详见下方主循环注释）。现作为「两条心跳的最小
        # 间隔」判据参与判断；初始化为 0.0 表示「从未发过心跳」，因此首个心跳
        # 依旧在 AI 静默达阈值时立即发出，保活语义与修复前完全一致。
        last_heartbeat_time = 0.0

        last_activity_time = time.time()  # 最后收到真实活动事件的时间

        # BUG #14 修复：SSE 总超时保护

        # 场景：AI 服务完全不可达时，所有章节都会失败，但若某些章节回调丢失，

        # run_generation 不会进入 finally 关闭 SSE 流，前端会持续看到 heartbeat

        # 解决方案：设置 SSE 总超时，到达后强制发送 completed 事件并退出

        # BUG-FIX-20 修复：原公式 min(900 + n*90, 2700) 对大项目必然误杀——

        # 100+ 章节 ÷ 并发 4 × 60-120s/节（含质量重试/续写）轻松超过 45 分钟上限。

        # 改为按并发分摊：基础 15 分钟 + 每章 150s ÷ 并发数，上限 2 小时。

        # BUG-FIX-52 修复：原 _per_chapter_secs=150 对含质量重试+续写的场景

        # 估计严重不足。单章节最坏情况：3次质量重试×120s + 120s续写 + 120s质量评估

        # = 600s。改为动态计算，考虑质量重试次数。

        #

        # PERF-SSE-TIMEOUT 2026-09-08：硬熔断对 300+ 章节项目仍会误杀。

        # 改为三段式：

        #   - 估算时间（_estimated_secs）：合理预期

        #   - 软告警（_warn_secs = _estimated * 1.5）：仅发 warning 事件，不熔断

        #

        _quality_retries = 2  # max_retries in quality check
# ENHANCE 2026-10-13：质量重试次数对超时估算影响显著。
# 如需微调，根据实际 AI 服务商的 429/质量评估重试经验对本值进行行业化调整。
# 若观测到实际重试频率远超预期，可适当增加本值以降低误触软告警率。

        _per_chapter_secs = 120 * (1 + _quality_retries * 2) + 120  # ~840s worst case

        _estimated_secs = 900 + (len(leaf_dicts) * _per_chapter_secs) / max(actual_concurrency, 1)

        _soft_warn_secs = int(_estimated_secs * 1.5)  # 1.5x 估算时间，到点发 warning

        _hard_timeout_secs = 14400  # 4 小时硬熔断（仅最终保险，正常不会触发）

        SSE_TOTAL_TIMEOUT = _hard_timeout_secs

        sse_start_time = time.time()

        soft_warn_sent = False  # 软告警只发一次

        try:

            while not finished or not queue.empty():

                # BUG-C-01 修复：检查总超时

                # 当超时发生时，不仅关闭SSE流，还要取消后台生成任务，

                # 避免孤儿任务继续消耗AI资源却没人接收结果

                elapsed_total = time.time() - sse_start_time

                # --- 软告警：到估算 1.5x 时仅发一次 warning，不熔断 ---

                if (not soft_warn_sent) and elapsed_total >= _soft_warn_secs:

                    soft_warn_sent = True

                    logger.warning(

                        "[正文生成] 已运行 %.0fs，超过估算 1.5x (%.0fs)，仍继续等待。",

                        elapsed_total, _estimated_secs,

                    )

                    try:

                        await queue.put({

                            "status": "slow_warning",

                            "message": (

                                f"已生成 {saved_count}/{len(leaf_dicts)} 章，"

                                f"运行 {int(elapsed_total)}s 超过预期估算 "

                                f"{int(_estimated_secs)}s，AI 响应较慢但仍在继续。"

                                f"距硬熔断({SSE_TOTAL_TIMEOUT//60}min)还剩 "

                                f"{int((SSE_TOTAL_TIMEOUT - elapsed_total)/60)} min"

                            ),

                            "elapsed": int(elapsed_total),

                            "estimated": int(_estimated_secs),

                            "saved": saved_count,

                            "total": len(leaf_dicts),

                        })

                    except Exception as _e:

                        logger.warning("[SSE] 静默捕获异常（BUG修复后应逐步消除）: %s", _e)

                # --- 硬熔断：4 小时才真正终止，作为"AI 完全卡死"最终保险 ---

                if elapsed_total >= SSE_TOTAL_TIMEOUT:

                    logger.warning(

                        "[正文生成] SSE 硬超时（%ds），强制关闭流并取消后台任务 project=%d",

                        SSE_TOTAL_TIMEOUT,

                        project_id,

                    )

                    error_sent = True

                    # 取消后台生成任务，防止孤儿任务持续消耗资源

                    if not task.done():

                        task.cancel()

                    try:

                        yield f"data: {json.dumps({'status': 'error', 'message': f'生成超过{SSE_TOTAL_TIMEOUT // 3600}小时，已自动终止'}, ensure_ascii=False)}\n\n"

                    except Exception as _e:

                        logger.warning("[SSE] 静默捕获异常（BUG修复后应逐步消除）: %s", _e)

                    break



                now = time.time()



                # BUG-O-01 修复：空闲超阈则发送 keep_alive 数据事件
                # （而非 SSE 注释行），便于前端跟踪连接健康状态

                # P0 修复 2026-09-29（心跳风暴 / 队列饿死）：心跳必须同时受
                # last_heartbeat_time 节流。原实现只看“距上次真实事件的空闲时长”，
                # 而 last_heartbeat_time 写后从未被读 —— 一旦 AI 单章耗时超过
                # 心跳阈值（并发场景的常态），空闲量恒 >= 阈值，循环每轮都走心跳
                # 分支并跳过队列消费，永远不再执行下面的 queue.get。后果链：
                #   ① 队列被饿死，进度/完成事件滞留队列无法转发
                #      → 前端进度条卡死、日志延迟错位；
                #   ② `while not finished or not queue.empty()` 因队列永不排空
                #      而无法退出，SSE 流只能靠 4h 硬熔断作为最终保险；
                #   ③ 每轮循环向客户端灌一条 keep_alive，形成心跳风暴。
                if _should_emit_heartbeat(
                    now, last_activity_time, last_heartbeat_time, SSE_HEARTBEAT_INTERVAL_SECS
                ):

                    try:

                        keep_alive_event = json.dumps(

                            {"event": "keep_alive", "status": "alive", "timestamp": now}

                        )

                        yield f"data: {keep_alive_event}\n\n"

                        last_heartbeat_time = now

                        # BUG #19 修复：心跳不应重置 last_activity_time

                        # 原代码 last_activity_time = now 导致心跳掩盖真实停滞：

                        # 当 AI 服务完全卡死时，心跳每10秒发送一次并重置 last_activity_time，

                        # 使 time_since_activity 永远不超过10秒，无法区分"有真实活动"和"只有心跳"。

                        # 保留 last_activity_time 仅在收到真实事件时更新（第1606行），

                        # 这样可以通过 last_activity_time 判断 AI 是否真正在工作。

                    except (asyncio.CancelledError, Exception):

                        # BUG-AI-04 修复（2026-09-03，已被 BUG-B 修正 2026-10-04）：

                        # 原实现在此 cancel_event.set() 杀死后台续跑，与模块既定契约

                        # 矛盾：finally 块「客户端断开保留任务后台运行」、_drain_queue、

                        # run_generation 的 client_disconnected finalize、前端 detach()

                        # + pollAndReconnect 全部以「断线≠停止」为前提。断线只是传输

                        # 层断开，不是用户主动停止（停止走 stop_generation 显式路径）。

                        client_disconnected = True

                        logger.warning("SSE 心跳推送失败，客户端已断开连接，任务转后台续跑")

                        break
                    # P0 修复 2026-09-29：此处不再 continue。心跳只是附带发送的
                    # 保活帧，绝不能因为它刚发过就跳过本轮的队列消费。删除 continue
                    # 后控制流直接落到下面的 queue.get，队列得以持续排空。

                try:

                    event = await asyncio.wait_for(
                        queue.get(), timeout=SSE_QUEUE_POLL_INTERVAL_SECS
                    )



                    # 客户端已断开连接则不再向其推送（写入会再次触发 ClientDisconnect）

                    if client_disconnected:

                        continue



                    # 更新最后活动时间（有真实事件说明连接正常且服务器在干活）

                    last_activity_time = time.time()



                    try:

                        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

                    except (asyncio.CancelledError, Exception):

                        # SSE 写入失败，说明客户端已断开连接，停止推送。

                        # BUG-B 修正 2026-10-04：不再 cancel_event.set() ——断线应转

                        # 后台续跑（finally 保留 task + _drain_queue 防队列堆积），

                        # 与前端 pollAndReconnect 等待后台任务完成的契约一致。

                        client_disconnected = True

                        logger.warning("SSE 事件推送失败，客户端已断开连接，任务转后台续跑")

                        break

                    if (

                        event.get("status") in SSE_TERMINAL_STATUSES

                        and event.get("section_id") is None

                    ):

                        break

                except asyncio.CancelledError:

                    # Python 3.11+ CancelledError bypasses TimeoutError/Exception.

                    # Frontend closed SSE → ASGI throws CancelledError from wait_for.

                    client_disconnected = True

                    logger.info("[正文生成] wait_for CancelledError (frontend closed SSE) project=%d", project_id)

                    break

                except TimeoutError:

                    # 轮询超时：本轮队列无新事件，回到循环顶部重新做超时/软告警/
                    # 节流心跳判定（心跳由 last_heartbeat_time 控制最小间隔，不会频发）

                    continue

        except asyncio.CancelledError:

            # Re-raise so ASGI can close StreamingResponse cleanly.

            # BUG-FIX 2026-10-04（BUG-B）：此前漏置 client_disconnected，finally 会

            # 误判为「非断线场景」而 task.cancel() 杀死生成任务，破坏后台续跑契约。

            client_disconnected = True

            logger.info("[正文生成] SSE 主循环 CancelledError (frontend disconnect) 转后台续跑 project=%d", project_id)

            raise

        except Exception as e:

            if not error_sent:

                try:

                    yield f"data: {json.dumps({'status': 'error', 'message': f'流异常: {e!s}'}, ensure_ascii=False)}\n\n"

                except (asyncio.CancelledError, Exception):

                    pass

        finally:

            # 2026-08-21 增强：支持后台运行 - 客户端断开时保留任务继续执行

            # BUG #11 修复：从 generation_manager 内存中清理已完成/已中断的任务

            # 场景：SSE 流断开、客户端取消、生成完成/失败后，必须从

            # generation_manager._tasks 中移除 task，否则 has_active_task 永远 True

            #

            # BUG-C-04 修复：finalize_task 优先级调整

            # 原逻辑 error_sent > stopped > finished，导致超时取消时

            # 已完成的章节被错误标记为 failed。修复后：

            #   stopped > finished > error_sent > client_disconnected

            # 因为超时取消属于"有结果的停止"，应优先使用 stopped 状态

            task_was_cancelled = task.cancelled() or (error_sent and (stopped or finished))

            # ENHANCE 2026-09-16（P0 断点续生）：暂停中的任务不能在这里收尾。
            # pause_generation 已先把任务标记为 paused（并置 attached=False，
            # 保留 generation_id 与任务组锁），随后才取消本 SSE。若此处照常
            # finalize_task，会把暂停任务从内存删除并释放组锁 —— 「恢复」按钮
            # 的 resume_task 再也找不到任务，恢复恒失败，用户只能从头重跑。
            _paused_task = await generation_manager.is_paused(project_id)

            try:

                if _paused_task:

                    logger.info(
                        "[正文生成] 任务处于暂停状态，保留断点续生现场 project=%d", project_id
                    )

                elif task_was_cancelled or stopped:

                    await generation_manager.finalize_task(

                        project_id, task_status="cancelled", error_message="生成已停止"

                    )

                elif finished:

                    await generation_manager.finalize_task(project_id, task_status="completed")

                elif error_sent:

                    await generation_manager.finalize_task(

                        project_id, task_status="failed", error_message="生成过程出错"

                    )

                elif client_disconnected:

                    # 2026-08-21 增强：客户端断开时保留任务继续在后台运行

                    # 不标记为 failed，不回滚章节，让任务继续执行

                    # 用户重新连接时可通过 generation-manager 查看进度

                    logger.info("[正文生成] 客户端断开，保留任务在后台运行 project=%d", project_id)

                    # 不调用 finalize_task，保持任务在 generation_manager 中运行

            except Exception as cleanup_err:

                logger.warning("[正文生成] 清理 generation_manager 任务失败: %s", cleanup_err)



            # BUG-C-02 修复：任务取消/错误时，回滚脏状态章节（stuck 在 generating）

            # BUG-FIX 2026-09-16：原实现把回滚调用放在 if/elif 分支**之外**，
            # 于是「客户端断开保留后台续跑」「暂停保留现场」两种场景都会被回滚成
            # pending 并清空 content —— 与紧邻注释声称的语义正好相反。
            # 现在只对「真停止/真失败」回滚。

            if not client_disconnected and not _paused_task:

                try:

                    await asyncio.to_thread(_rollback_dirty_sections, leaf_ids_for_generation)

                except Exception as rb_err:

                    logger.warning("[正文生成] 回滚脏状态失败: %s", rb_err)



            if not task.done():

                # 2026-08-21 增强：客户端断开时保留任务运行

                if not client_disconnected:

                    task.cancel()

                    try:

                        await asyncio.wait_for(task, timeout=30.0)

                    except TimeoutError:

                        logger.warning("[正文生成] 任务等待超时，继续清理")

                    except asyncio.CancelledError:

                        pass

                    except Exception as _e:

                        logger.warning("[SSE] 静默捕获异常（BUG修复后应逐步消除）: %s", _e)

                else:

                    logger.info("[正文生成] 后台模式：保留生成任务继续运行 project=%d", project_id)



                    # 启动后台消费者排空队列，防止内存泄漏

                    async def _drain_queue():

                        _drain_started = asyncio.get_running_loop().time()

                        try:

                            while True:

                                try:

                                    event = await asyncio.wait_for(queue.get(), timeout=30)

                                    # 丢弃事件，仅用于防止队列堆积

                                    if event.get("status") in SSE_TERMINAL_STATUSES:

                                        break

                                except asyncio.CancelledError:

                                    # Python 3.11+ CancelledError bypasses TimeoutError

                                    break

                                except TimeoutError:

                                    # BUG-FIX 2026-10-08（P3）：30s 无事件不再直接退出——
                                    # 单章 AI 长调用即可造成静默窗口，提前退出会让后续
                                    # 事件在无界 queue 滞留至任务结束（与「防堆积」设计
                                    # 意图不符）。终态事件（completed/error/stopped）
                                    # 必达，由其驱动退出；兜底 4h 总时长上限（与 SSE
                                    # 硬熔断一致）。
                                    if asyncio.get_running_loop().time() - _drain_started >= 14400:

                                        break

                                    continue

                        except Exception as _e:

                            logger.warning("[SSE] 静默捕获异常（BUG修复后应逐步消除）: %s", _e)



                    _drain_task = asyncio.create_task(_drain_queue(), name=f"drain-{project_id}")

                    _background_tasks.add(_drain_task)

                    _drain_task.add_done_callback(_background_tasks.discard)



            # 清理任务注册

            async with _cancel_events_lock:

                tasks = _cancel_tasks.get(project_id)

                if tasks and task in tasks:

                    tasks.remove(task)

                    if not tasks:

                        del _cancel_tasks[project_id]



            # Bug 修复：将 _cancel_events 清理逻辑整体放入锁内，

            # 避免「检查-操作」竞态导致 KeyError / IndexError。

            # 关键：_cancel_events 是共享可变结构（dict + list），

            # 必须先获取锁再做「存在性检查 + 修改 + 可能 del」三步操作。

            async with _cancel_events_lock:

                evs = _cancel_events.get(project_id)

                if evs is not None:

                    try:

                        evs.remove(cancel_event)

                    except ValueError:

                        # 事件已被其他协程移除（如 finally 之外的清理路径），

                        # 这是良性竞争，吞掉即可

                        pass

                    if not evs:

                        del _cancel_events[project_id]



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

async def expand_content_impl(

    project_id: int,

    writing_length: str = Query("standard"),

    level2_default: int = Query(3500, ge=1000, le=10000),

    concurrency: int | None = Query(None),

):

    """兜底扩写：对字数不足的章节并发扩写，确保内容充实（SSE流式返回）"""



    # 事件生成器 - 产生 SSE 事件流

    async def event_generator():

        yield f"data: {json.dumps({'status': 'connected'}, ensure_ascii=False)}\n\n"

        # ENHANCE 2026-09-15（高）：兜底扩写加入「正文生成」任务组锁。
        # 原实现没有任何互斥：正文生成进行中仍可触发扩写（多标签页/直接调 API），
        # 两条流水线并发 UPDATE 同一批 sections，扩写结果会覆盖正在生成的正文。
        from ...services import task_lock as _task_lock

        lock_record: dict = {}

        # BUG-FIX 2026-09-16（P1 锁泄漏）：抢锁在 worker 线程里执行，锁一旦落到 sqlite
        # 就已生效，而 `await asyncio.to_thread(...)` 本身是可取消点。客户端在抢锁
        # 瞬间断开 SSE 时协程被取消 → `lock_record` 还来不及赋值 → finally 的释放分支
        # 拿不到 lock_key → 锁白占 30 分钟 TTL，这期间该项目所有正文生成/兜底扩写
        # 都会被 TASK_LOCK_CONFLICT 拒绝。用共享容器把 key 从线程里带出来，
        # 并在 BaseException（含 CancelledError）分支兜底释放 —— 与目录生成侧
        # `sse_outline._force_release_lock` 同机制。
        _lock_holder: dict = {"key": "", "owner": ""}

        def _do_acquire():
            rec = _task_lock.acquire(
                "content-generation",
                project_id=project_id,
                note="fallback-expand",
            )
            _lock_holder["key"] = rec.get("lock_key", "")
            _lock_holder["owner"] = rec.get("owner_id", "")
            return rec

        try:
            lock_record = await asyncio.to_thread(_do_acquire)
        except TaskLockConflict as _lock_e:
            logger.warning("[兜底扩写] 任务组互斥拦截 project=%d: %s", project_id, _lock_e)
            yield f"data: {json.dumps({'status': 'error', 'code': 'TASK_LOCK_CONFLICT', 'message': str(_lock_e)}, ensure_ascii=False)}\n\n"
            return
        except BaseException:
            _lk, _lo = _lock_holder["key"], _lock_holder["owner"]
            if _lk and _lo:
                try:
                    await asyncio.shield(
                        asyncio.to_thread(_task_lock.release, _lk, _lo)
                    )
                    logger.warning(
                        "[兜底扩写] 抢锁途中被取消，已兜底释放任务组锁 project=%d key=%s",
                        project_id, _lk,
                    )
                except Exception:
                    logger.warning(
                        "[兜底扩写] 抢锁途中被取消且兜底释放失败 project=%d key=%s",
                        project_id, _lk, exc_info=True,
                    )
            raise
        if not lock_record.get("acquired"):
            lock_record = {}  # 未登记分组等占位结果，无需释放

        # BUG-FIX 2026-10-08（P1 锁失效）：扩写超时预算可达 4h > 锁 TTL 30min，
        # 且本路径没有 update_section_progress 心跳 → 锁被 reclaim_stale 回收后，
        # 同项目新的「生成正文」create_task 可抢锁成功，与仍在跑的扩写并发
        # UPDATE 同一批 sections（正是上方注释要防的场景）。启动后台续约线程
        # （与导出侧 start_keepalive 同机制），收尾时先 stop 再 release。
        _stop_lock_keepalive = lambda: None  # noqa: E731 默认空操作
        if lock_record.get("acquired"):
            _stop_lock_keepalive = _task_lock.start_keepalive(
                lock_record.get("lock_key", ""), lock_record.get("owner_id", "")
            )

        # 注册取消事件

        cancel_event = asyncio.Event()

        async with _cancel_events_lock:

            _cancel_events.setdefault(project_id, []).append(cancel_event)



        finished = False

        error_sent = False

        # ENHANCE 2026-10-04（遗留问题3）：断线标志——与 generate_content_impl 同语义，
        # 客户端断开 ≠ 用户停止，置位后 finally 保留 expand_task 后台续跑。
        client_disconnected = False

        # ENHANCE 2026-09-16：兜底扩写的工作量预估与软/硬超时容器。
        # `run_expand` 在算出「字数不足章节数 + 实际并发」后写入，SSE 主循环据此动态放宽：
        # 旧实现把 SSE_TOTAL_TIMEOUT 写死 900s（15 分钟），但扩写早已从「每轮只挑全局
        # 最少的 1 章、最多 5 轮」重写为「筛出全部不足章节 + 并发扩写」（见
        # workflows_expand.expand_content_fallback）。300 个不足章节 × 最多 3 轮 × 120s ÷ 并发 8
        # 轻松超过 15 分钟 → 必然被硬超时误杀：前端弹「扩写超时」，
        # 后台 expand_task 随即被 cancel，大量章节永远扩不到目标字数（且用户无从得知）。
        # 现按规模估算并留 1.5x 余量，4 小时封顶，同时在 1.5x 估算点发一次 slow_warning。
        _expand_budget: dict = {"estimated": 0, "timeout": 900, "warned": False}



        # 扩写任务

        async def run_expand():

            nonlocal finished, error_sent

            try:

                # 1. 初始化配置（优先使用保存的配置，不可用时自动降级）

                ai_config = get_ai_config()

                from ...services.ai.provider_factory import get_working_provider



                provider, used_fb = await get_working_provider(ai_config)

                if used_fb:

                    logger.info("[兜底扩写] 主配置不可用，已降级到兜底提供商: %s", used_fb)

                # 兜底扩写平铺阈值（writing_length 映射）；章节级阈值由
                # section_expand_threshold 按各章 target_words×80% 逐章计算
                minimum_words = {
                    "short": 800,
                    "standard": 2000,
                    "detailed": 4000,
                    "extra_long": 6000,
                }.get(writing_length, 2000)



                # 检查取消

                if cancel_event.is_set():

                    finished = True

                    return



                # 2. 获取项目数据

                # BUG 修复：原实现直接在 async 函数中同步执行 DB 查询

                # （sections 全表内容查询，大项目可达数 MB），阻塞事件循环。

                # 与 _fetch_project_data_async 保持一致，改用 to_thread。

                try:



                    def _fetch_expand_data():

                        with get_db() as conn:

                            row = conn.execute(

                                "SELECT summary_json FROM analysis WHERE project_id = ?",

                                (project_id,),

                            ).fetchone()

                            if not row:

                                return None, None, None

                            summary = row["summary_json"]

                            project_row = conn.execute(

                                "SELECT industry FROM projects WHERE id = ?",

                                (project_id,),

                            ).fetchone()

                            all_rows = conn.execute(

                                "SELECT id, parent_id, title, level, target_words, content, status FROM sections WHERE project_id = ? ORDER BY sort_order, id",

                                (project_id,),

                            ).fetchall()

                            return summary, project_row, all_rows



                    summary, project_row, all_rows = await asyncio.to_thread(_fetch_expand_data)

                    if summary is None:

                        await queue.put({"status": "error", "message": "请先完成摘要识别"})

                        error_sent = True

                        finished = True

                        return

                except Exception as e:

                    # BUG-FIX 2026-09-15：这里原来还有一个不可达的 `except Exception: return`，

                    # 且本分支漏置 finished —— run_expand 已退出但主循环靠 error 事件的

                    # 无 section_id 终止判定兜底，语义脆弱。现补齐 finished 并删除死代码。

                    await queue.put({"status": "error", "message": f"数据库查询失败: {e!s}"})

                    error_sent = True

                    finished = True

                    return



                # 检查取消

                if cancel_event.is_set():

                    finished = True

                    return



                industry = (

                    project_row["industry"] if project_row and "industry" in project_row else ""

                )

                industry_profile = get_industry_profile(industry)



                try:

                    full_text = await latest_extracted_text(project_id)

                    tender_context = (

                        await extract_tender_context_async(full_text) if full_text else ""

                    )

                except Exception as _tc_e:
                    # BUG-FIX 2026-10-15（M-2 · 同缺陷不同口径）：与
                    # sse_utils._extract_project_context 的 2026-09-18 修复对齐，
                    # 正文生成链路读不到招标正文时必须留告警。
                    logger.warning(
                        "正文上下文装配: 招标正文/上下文提取失败，将不带招标上下文继续 "
                        "project=%d err=%s",
                        project_id, _tc_e,
                    )
                    tender_context = ""



                # BUG-F-12 修复：sqlite3.Row 没有 .get() 方法，必须先转 dict

                # 原代码直接 all_rows（sqlite3.Row）→ results 列表，

                # 后续 r.get("content") 抛 AttributeError → ERR_ABORTED

                sections_by_id = {r["id"]: dict(r) for r in all_rows}

                results = [

                    {"id": r["id"], "content": r["content"] or "", "status": r["status"] or ""}

                    for r in all_rows

                    if r["content"]  # sqlite3.Row 用下标/键访问

                ]



                if not results:

                    await queue.put(

                        {

                            "status": "completed",

                            "message": "没有需要扩写的章节",

                            "expanded_count": 0,

                        }

                    )

                    finished = True

                    return



                # 检查取消

                if cancel_event.is_set():

                    finished = True

                    return



                # 3. 执行扩写任务

                # ENHANCE 2026-09-15：先统计「字数不足」章节（per-section 阈值），
                # 为 0 时直接完成，避免空跑 AI；扩写期间逐章保存 + 推送进度。
                from ...services.ai.workflows_expand import collect_deficient_sections

                deficient_entries = collect_deficient_sections(
                    results, sections_by_id, minimum_words, level2_default, writing_length=writing_length
                )
                deficient_before = len(deficient_entries)
                if deficient_before == 0:
                    await queue.put(
                        {
                            "status": "completed",
                            "message": "所有已生成章节字数均达标，无需扩写",
                            "expanded_count": 0,
                            "total": 0,
                        }
                    )
                    finished = True
                    return

                actual_concurrency = max(1, min(int(concurrency or 2), MAX_CONTENT_CONCURRENCY))

                # ENHANCE 2026-09-16：按真实工作量估算超时（见 _expand_budget 注释）。
                # 单章最坏 = 3 轮扩写 × 120s；再按并发分摊，基础 5 分钟。
                _per_section_secs = 120 * 3
                _estimated_expand = 300 + (
                    deficient_before * _per_section_secs
                ) / max(actual_concurrency, 1)
                _expand_budget["estimated"] = int(_estimated_expand)
                _expand_budget["timeout"] = int(
                    min(max(_estimated_expand * 1.5, 900), 4 * 3600)
                )
                logger.info(
                    "[扩写] 章节数=%d 并发=%d → 估算耗时 %ds，硬超时 %ds",
                    deficient_before,
                    actual_concurrency,
                    _expand_budget["estimated"],
                    _expand_budget["timeout"],
                )

                await queue.put(
                    {
                        "status": "generating",
                        "phase": "expand",
                        "message": f"开始扩写 {deficient_before} 个字数不足的章节（并发 {actual_concurrency}）...",
                        "total": deficient_before,
                        "concurrency": actual_concurrency,
                        # 让前端/诊断能解释"为什么这次要跑很久"，
                        # 也让"超时随工作量自适应"这件事可被回归测试锚定。
                        "estimated_seconds": _expand_budget["estimated"],
                        "timeout_seconds": _expand_budget["timeout"],
                    }
                )



                # BUG-F-13 修复：expand_content_fallback 实际在 workflows_expand 模块

                from ...services.ai.workflows_expand import expand_content_fallback as _expand

                expanded_count = 0
                _results_by_id = {r["id"]: r for r in results if r.get("id") is not None}

                async def _on_section_done(section_id, title):
                    """单章达标即落库 + 推送进度（ENHANCE 2026-09-15）。

                    旧实现等全部扩写结束才逐条保存——中途停止/断线会丢掉已扩写内容。
                    """
                    nonlocal expanded_count
                    r = _results_by_id.get(section_id)
                    if not r or not r.get("content"):
                        return
                    try:
                        # update_section 为同步 DB 写入，用 to_thread 避免阻塞事件循环
                        await asyncio.to_thread(
                            update_section,
                            r["id"],
                            SectionUpdate(content=r["content"], status="completed"),
                        )
                        expanded_count += 1
                        await queue.put(
                            {
                                "status": "section_completed",
                                "section_id": r["id"],
                                "title": title,
                                "word_count": _count_words(r.get("content") or ""),
                            }
                        )
                    except Exception as e:
                        logger.warning("更新章节 %s 扩写内容失败: %s", section_id, e)

                expanded = await _expand(
                    provider,
                    results,
                    sections_by_id,
                    minimum_words,
                    tender_context,
                    summary,
                    industry_profile,
                    writing_length,
                    cancel_event=cancel_event,
                    concurrency=actual_concurrency,
                    level2_default=level2_default,
                    on_section_done=_on_section_done,
                )



                # 检查取消

                if cancel_event.is_set():

                    finished = True

                    return



                # 4. 统计仍未达标的章节数（阈值口径与扩写一致）

                deficient_after = len(
                    collect_deficient_sections(expanded, sections_by_id, minimum_words, level2_default, writing_length=writing_length)
                )



                # 5. 返回完成事件

                await queue.put(

                    {

                        "status": "completed" if not cancel_event.is_set() else "stopped",

                        "message": (
                            f"扩写完成：字数不足 {deficient_before} → {deficient_after} 章，"
                            f"本次扩写达标 {expanded_count} 章（并发 {actual_concurrency}）"
                        ),

                        "expanded_count": expanded_count,

                        "total": deficient_before,
                        "deficient_after": deficient_after,

                    }

                )

            except asyncio.CancelledError:

                await queue.put({"status": "stopped", "message": "扩写已被停止"})

            except Exception as e:

                error_sent = True

                await queue.put({"status": "error", "message": f"扩写失败: {e!s}"})

            finally:

                finished = True

                async with _cancel_events_lock:

                    _evs = _cancel_events.get(project_id)

                    if _evs and cancel_event in _evs:

                        _evs.remove(cancel_event)

                        # BUG-FIX 2026-09-16（低）：空列表也要清键，防止残留

                        if not _evs:

                            _cancel_events.pop(project_id, None)



        # 创建队列和任务

        queue: asyncio.Queue = asyncio.Queue()

        expand_task = asyncio.create_task(run_expand(), name=f"expand-{project_id}")

        async with _cancel_events_lock:

            _cancel_tasks.setdefault(project_id, []).append(expand_task)



        # SSE 主循环

        try:

            # ENHANCE 2026-09-16：超时由 _expand_budget 动态给出（默认 900s 兜底，
            # run_expand 算出真实工作量后会放宽；见其注释）。
            sse_start_time = time.time()

            while not finished or not queue.empty():

                elapsed = time.time() - sse_start_time

                SSE_TOTAL_TIMEOUT = _expand_budget.get("timeout", 900)

                # 软告警：超过估算 1.5x 只提示一次，不熔断（与正文生成同款语义）
                if (
                    _expand_budget.get("estimated")
                    and not _expand_budget.get("warned")
                    and elapsed >= _expand_budget["estimated"] * 1.5
                ):
                    _expand_budget["warned"] = True
                    logger.warning(
                        "[扩写] 已运行 %.0fs，超过估算 %.0fs 的 1.5x，仍继续等待 project=%d",
                        elapsed,
                        _expand_budget["estimated"],
                        project_id,
                    )
                    try:
                        await queue.put({
                            "status": "slow_warning",
                            "message": (
                                f"扩写耗时 {int(elapsed)}s 已超过预期估算 "
                                f"{_expand_budget['estimated']}s，AI 响应较慢但仍在继续"
                            ),
                            "elapsed": int(elapsed),
                            "estimated": _expand_budget["estimated"],
                        })
                    except Exception as _e:
                        logger.debug("[扩写] 推送 slow_warning 失败: %s", _e)

                if elapsed >= SSE_TOTAL_TIMEOUT:

                    logger.warning("[扩写] SSE 总超时，强制关闭 project=%d", project_id)

                    yield f"data: {json.dumps({'status': 'error', 'message': '扩写超时，已自动终止'}, ensure_ascii=False)}\n\n"

                    break



                try:

                    event = await asyncio.wait_for(queue.get(), timeout=10)

                    try:

                        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

                    except (asyncio.CancelledError, Exception):

                        # BUG-A5 修复（2026-09-03，已被遗留问题3改造 2026-10-04）：
                        # 原实现在此 cancel_event.set() 杀死扩写。现对齐正文生成
                        # 的「断线≠停止」契约：run_expand 已有逐章落库
                        # （_on_section_done），断线后后台续跑把剩余章节扩完，
                        # AI 成本不浪费；用户显式停止仍走 stop_generation。

                        client_disconnected = True

                        logger.info("[扩写] SSE 事件推送失败，客户端已断开，任务转后台续跑 project=%d", project_id)

                        break

                    if (

                        event.get("status") in SSE_TERMINAL_STATUSES

                        and event.get("section_id") is None

                    ):

                        break

                except asyncio.CancelledError:

                    # Python 3.11+ CancelledError skips TimeoutError/Exception.

                    # ENHANCE 2026-10-04（遗留问题3）：断线转后台续跑，不再杀死任务。

                    client_disconnected = True

                    logger.info("[扩写] wait_for CancelledError (frontend closed SSE) 转后台续跑 project=%d", project_id)

                    break

                except TimeoutError:

                    # 10秒无新事件，继续等待

                    continue

        except asyncio.CancelledError:

            # ENHANCE 2026-10-04（遗留问题3）：此前漏置 client_disconnected，finally
            # 会无条件 cancel expand_task，丢剩余扩写。现统一为断线后台续跑。

            client_disconnected = True

            logger.info("[扩写] SSE 主循环 CancelledError (frontend disconnect) 转后台续跑 project=%d", project_id)

            raise

        except Exception as e:

            try:

                yield f"data: {json.dumps({'status': 'error', 'message': f'流异常: {e!s}'}, ensure_ascii=False)}\n\n"

            except (asyncio.CancelledError, Exception):

                pass

        finally:

            # BUG-1 修复：补齐 expand_task 清理 + _cancel_tasks 反注册
            # 原实现只清理了 _cancel_events，遗漏：
            #   1. expand_task 未 done 时需 cancel（否则孤儿任务持续消耗 AI 资源）
            #   2. _cancel_tasks 中仍持有 expand_task 引用（内存泄漏）
            # 对照 generate_content_impl finally 块的完整清理模式补齐。

            # ENHANCE 2026-10-04（遗留问题3）：断线语义对齐正文生成——客户端断开
            # 且非用户显式停止时，保留 expand_task 后台续跑，由后台等待协程在
            # 任务自然完成后执行清理；任务组锁继续持有至扩写结束，避免正文
            # 生成流水线插队并发覆写同一批 sections。stop_generation 仍可遍历
            # _cancel_tasks 强制取消后台任务（因此续跑分支不能提前反注册）。
            # 极端情况（应用关机协程被取消）由锁 TTL 兜底释放。

            async def _cleanup_after_expand():

                # 2) 从 _cancel_tasks 移除注册
                async with _cancel_events_lock:

                    _tasks = _cancel_tasks.get(project_id)

                    if _tasks and expand_task in _tasks:

                        _tasks.remove(expand_task)

                        if not _tasks:

                            del _cancel_tasks[project_id]

                # 3) 清理取消事件记录
                async with _cancel_events_lock:

                    _evs = _cancel_events.get(project_id)

                    if _evs and cancel_event in _evs:

                        _evs.remove(cancel_event)

                        # BUG-FIX 2026-09-16（低）：空列表也要清键，防止残留

                        if not _evs:

                            _cancel_events.pop(project_id, None)

                # 3.5) ENHANCE 2026-09-16：扩写直接 UPDATE 了 sections.content，
                # 必须失效项目章节缓存。BUG-FIX 2026-09-18（P1）：原只失效 Redis，
                # 改用三层统一失效入口（内存正文缓存/build_tree 一并失效）。
                # 逐章 update_section 已做过精确失效，此处为整包兜底。
                try:

                    from ...services.sections import invalidate_content_caches

                    invalidate_content_caches(project_id)

                except Exception as _inv_e:

                    logger.warning("[兜底扩写] 章节缓存失效失败（忽略）: %s", _inv_e)

                # 4) 释放任务组锁（ENHANCE 2026-09-15）
                # BUG-FIX 2026-10-08：先停 keepalive 再释放，避免续约与释放交错
                _stop_lock_keepalive()
                if lock_record.get("lock_key"):

                    try:

                        await asyncio.to_thread(
                            _task_lock.release, lock_record["lock_key"], lock_record.get("owner_id", "")
                        )

                    except Exception:

                        logger.warning("[兜底扩写] 释放任务组锁失败 project=%d", project_id, exc_info=True)

            _keep_running = (

                client_disconnected

                and not cancel_event.is_set()

                and not expand_task.done()

            )

            if _keep_running:

                logger.info("[兜底扩写] 客户端断开，保留扩写任务后台续跑 project=%d", project_id)

                async def _drain_expand_queue():

                    # 防止断线后事件在 queue 无限堆积（与 generate 侧同机制）
                    try:

                        while True:

                            try:

                                await asyncio.wait_for(queue.get(), timeout=30)

                            except asyncio.CancelledError:

                                break

                            except TimeoutError:

                                if expand_task.done():

                                    break

                    except Exception as _e:

                        logger.debug("[扩写] 后台队列排空异常: %s", _e)

                async def _await_expand_then_cleanup():

                    try:

                        # shield：即使本等待协程被取消，也不误伤 expand_task 本体
                        await asyncio.shield(expand_task)

                    except asyncio.CancelledError:

                        # 用户显式停止（stop_generation 遍历 _cancel_tasks 取消）
                        logger.info("[扩写] 后台续跑任务被显式停止 project=%d", project_id)

                    except Exception as _e:

                        logger.warning("[扩写] 后台续跑任务异常: %s", _e)

                    await _cleanup_after_expand()

                _drain_task = asyncio.create_task(_drain_expand_queue(), name=f"expand-drain-{project_id}")

                _background_tasks.add(_drain_task)

                _drain_task.add_done_callback(_background_tasks.discard)

                _bg_task = asyncio.create_task(_await_expand_then_cleanup(), name=f"expand-bg-{project_id}")

                _background_tasks.add(_bg_task)

                _bg_task.add_done_callback(_background_tasks.discard)

            else:

                # 1) cancel expand_task 并等待收敛（最多 30s）——
                # 正常完成/错误/用户显式停止路径，与原 BUG-1 行为一致。
                if not expand_task.done():

                    expand_task.cancel()

                    try:

                        await asyncio.wait_for(expand_task, timeout=30.0)

                    except TimeoutError:

                        logger.warning('[扩写] expand_task 等待超时，继续清理 project=%d', project_id)

                    except asyncio.CancelledError:

                        pass

                    except Exception as _e:

                        logger.debug('[扩写] expand_task 清理异常: %s', _e)

                await _cleanup_after_expand()

    # BUG-FIX 2026-09-15（P0）：expand_content_impl 缺少返回语句！
    # 事件生成器写完后没有 `return StreamingResponse(...)`，整个端点返回 null，
    # SSE 流根本建立不起来，前端必然报「SSE 流异常结束，未收到完成事件」——
    # 兜底扩写按钮从未真正可用。补上与 generate_content_impl 一致的返回。

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
