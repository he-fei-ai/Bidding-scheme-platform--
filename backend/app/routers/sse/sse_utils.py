"""sse_handlers 拆分模块 — sse_utils.py"""

import asyncio
import hashlib
import json
import logging
import time
from typing import Any
from fastapi import HTTPException, Query
from starlette.responses import StreamingResponse
from ...db import get_db
from ...services.ai.provider_factory import get_ai_config
from ...models import SectionUpdate
from ...services.ai.industry_profiles import get_industry_profile
from ...services.ai.writing_length_config import (
    get_l3_own_target,
    get_l3_own_range,
    get_tier_config,
    get_default_tier_config,
)
from ...services.ai.workflows import generate_outline as _run_outline_workflow
from ...services.ai.workflows import generate_sections as _run_sections
from ...services.content_field_contract import parse_extraction_fields_record
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
from ...services.task_registry import TaskStatus, task_registry
from ...services.tender_context import extract_tender_context_async
from ...services.v51_state import (

    clear_v51_state_from_db,

    create_v51_state,

    load_v51_state_from_db,

    remove_v51_state,

    save_v51_state_to_db,

)
from ...services.facts_service import (

    PLACEHOLDER_VALUE_PREFIX,

    facts_for_generation,

    is_placeholder_value,

    parse_stored_items,

)
from ..task_control import _cancel_events, _cancel_events_lock, _cancel_tasks

from .sse_shared import logger

# 全局事实占位值前缀（与 app.routers.facts 的 placeholder 策略同源）。
# ENHANCE 2026-09-29（facts-ns）：正本已上收到 `services/facts_service.py`，
# 这里只保留一个 re-export 以兼容既有 `from ...sse_utils import _PLACEHOLDER_VALUE_PREFIX`。
_PLACEHOLDER_VALUE_PREFIX = PLACEHOLDER_VALUE_PREFIX

def _parse_global_facts_items(raw: str | None) -> list:

    """解析 global_facts_json 为事实列表（扁平或分组），异常返回空列表

    BUG-FIX 2026-09-18（P1）：
    1) 原解析失败静默返回空列表——正文在“事实丢失”状态下照常生成，
       用户无从感知，现补 warning 日志；
    2) placeholder 策略下未填事实以【待填写】xxx 入库，此前会直接注入
       正文 prompt，AI 吃掉占位串后把“【待填写】”写进标书正文；现过滤掉
       value 含【待填写】的条目（只污染事实源，不污染成品）。

    ENHANCE 2026-09-29（facts-ns）：解析 + 占位过滤的口径整体委派
    `facts_service.parse_stored_items` / `is_placeholder_value`，与
    facts 路由 GET/audit、Celery `tasks/__init__`、`consistency_repair` 同一
    正本，不再生效不一致的多份副本（以前只在这里过滤占位，
    Celery 路径直接 json.loads 不过滤）。本函数只多保留一句 drop 日志。
    """

    items = parse_stored_items(raw)

    # BUG-FIX 2026-10-05（去重）：「解析 + 剔除占位」这一对动作在
    # `facts_service.facts_for_generation` 里已有正本实现（Celery 侧共用），
    # 此前这里自己写了一遍 list 推导式，属于同一规则的第二份副本——一旦
    # 占位判定口径变化（如新增占位前缀）两边就会再次漂移。改为直接委派，
    # 只额外保留一句 drop 日志（需先取原始条数才能算过滤掉几条）。
    kept = facts_for_generation(raw)

    dropped = len(items) - len(kept)

    if dropped:

        logger.info("全局事实注入前过滤掉 %d 条【待填写】占位项", dropped)

    return kept

def _get_path_and_ancestors(

    sid: int,

    sections_by_id: dict,

    children_map: dict,

) -> tuple[str, list[str]]:

    """获取章节的路径编号和祖先标题列表



    Args:

        sid: 章节 ID

        sections_by_id: 章节 ID → 章节数据映射

        children_map: 父 ID → 子章节列表映射



    Returns:

        tuple: (路径编号如 "1.2.3", 祖先标题列表)

    """

    titles = []

    path_parts = []

    current = sections_by_id.get(sid)

    chain = []

    # BUG-FIX 2026-09-18（P1）：防御性断环 —— 历史脏数据/并发写入可能已造成
    # parent_id 环（PATCH 入口已在服务层拦截），沿链向上若 revisit 同一节点
    # 必须跳出，否则 while 永不终止 → 正文生成整任务卡死+内存膨胀。
    _seen_ids: set = set()

    while current:

        if current.get("id") in _seen_ids:

            logger.warning("检测到章节 parent_id 循环引用，已在 id=%s 处截断链", current.get("id"))

            break

        _seen_ids.add(current.get("id"))

        chain.append(current)

        pid = current["parent_id"]

        current = sections_by_id.get(pid) if pid else None

    chain.reverse()

    for node in chain:

        titles.append(node["title"])

        siblings = children_map.get(node["parent_id"], [])

        for i, s in enumerate(siblings):

            if s["id"] == node["id"]:

                path_parts.append(str(i + 1))

                break

    return ".".join(path_parts), titles

def _resolve_tier_label(word_count_tier: str | None, writing_length: str | None) -> str | None:
    """解析档位中文标签（简短/标准/详细/超长），用于提示词与前端展示。

    优先级：单节点 word_count_tier > 全局 writing_length > 默认档位。
    """
    key = word_count_tier or writing_length
    if not key:
        cfg = get_default_tier_config()
        return cfg.get("label") if cfg else None
    cfg = get_tier_config(key)
    return cfg.get("label") if cfg else None


def _get_effective_target_words(
    section: dict,
    level2_default: int = 3500,
    writing_length: str | None = None,
) -> int | None:
    """计算章节的有效目标字数（2026-10-08 版：字数控制下沉到三级目录 L3）。

    策略：
      - **只对三级目录（L3）作为字数控制单位**：L3 是叶子生成中最常见的层级，
        其目标字数由档位（brief/standard/detailed/long）决定，四档标准
        800/1200/2000/2500，允许 ±10% 浮动。
      - **L1 / L2 不参与字数控制**：它们是不生成正文的父节点，`target_words`
        字段仅作为统计/展示用途（其累计字数由子树所有 L3 累加得出）。
      - **优先级**：单节点 `target_words` 精确覆盖 > 单节点 `word_count_tier`
        > 全局 `writing_length` 档位 > 默认档位（standard=1200）。
      - **L4+ 叶子**：若目录深度 >3，其目标字数按父 L3 档位同步（避免深层
        叶子脱离档位控制）。当前系统默认 L3 即为最深叶子，此兜底较少触发。

    Args:
        section: 章节字典（含 level / target_words / word_count_tier 等）
        level2_default: 历史兼容参数，L2 已不再使用该值；仅保留以免旧调用报错
        writing_length: 全局档位 key（brief/standard/detailed/long，兼容 short/extra_long）

    Returns:
        int | None: 目标字数；对 L1/L2 返回 None（不参与控制）
    """
    level = section.get("level", 3) or 3

    # L1 / L2 是父节点，不生成正文 -> 不参与字数控制
    if level < 3:
        return None

    # 三级目录（L3）：字数控制核心
    if level == 3:
        # 1) 单节点精确覆盖（用户显式设置的 target_words）> 最高优先级
        tw = section.get("target_words")
        if isinstance(tw, int) and tw > 0:
            return tw
        # 2) L3 自身正文（方案三 2026-10-09）：300 字左右 ±20%（240~360 字）
        #    四档 800/1200/2000/2500 作为「L3 子树总字数」口径，仅在 L3
        #    存在下级目录章节（L4+）时参与校验；无下级的 L3 以自身正文
        #    300 ± 20% 为唯一测量口径，生成层与校验层必须一致。
        return get_l3_own_target()

    # L4+ 深层叶子：跟随其父 L3 的档位（简化实现：直接用当前全局档位）
    # 实际系统中 L4+ 极少出现；此处保持与 L3 相同分档策略
    tw = section.get("target_words")
    if isinstance(tw, int) and tw > 0:
        return tw
    tier_key = section.get("word_count_tier")
    if tier_key:
        cfg = get_tier_config(tier_key)
        if cfg:
            return int(cfg["target_words"])
    if writing_length:
        cfg = get_tier_config(writing_length)
        if cfg:
            return int(cfg["target_words"])
    default_cfg = get_default_tier_config()
    return int(default_cfg.get("target_words", 1200))


def _resolve_chart_field(chart_type: str) -> str | None:

    """将图表类型映射到数据库字段名"""

    _MAPPING = {

        "flowchart": "flowchart_json",

        "gantt": "gantt_json",

        "architecture": "architecture_json",

        "labor": "labor_json",

        "comparison": "comparison_json",

        "layout": "layout_json",

        "timeline": "timeline_json",

    }

    return _MAPPING.get(chart_type)

async def _try_get_provider(

    queue: asyncio.Queue | None = None,

) -> tuple:

    """获取 AI 提供商实例，支持自动降级到兜底提供商



    Args:

        queue: 若提供，将推送降级状态事件到队列



    Returns:

        tuple: (provider, used_fb, actual_concurrency)

            - provider: AI 提供商实例

            - used_fb: 使用的兜底提供商名称（None 表示未降级）

            - actual_concurrency: 实际并发数



    Raises:

        Exception: AI 配置异常时抛出

    """

    ai_config = get_ai_config()

    from ...services.ai.provider_factory import get_working_provider



    provider, used_fb = await get_working_provider(ai_config)



    if used_fb:

        logger.info("主配置不可用，已降级到兜底提供商: %s", used_fb)

        if queue:

            await queue.put(

                {

                    "status": "fallback",

                    "provider": used_fb,

                    "message": f"主配置不可用，已自动切换到 {used_fb}",

                }

            )



    actual_concurrency = int(ai_config.get("concurrency", 3))

    return provider, used_fb, actual_concurrency

async def _try_get_provider_with_retry(

    queue: asyncio.Queue | None = None,

    error_msg: str = "",

) -> tuple:

    """获取 AI 提供商（含重试降级），用于异常恢复场景



    当主提供商发生 AI/JSON/超时类错误时，自动切换到兜底提供商重试。



    Args:

        queue: 若提供，将推送状态事件到队列

        error_msg: 原始错误消息（用于判断是否需要降级）



    Returns:

        tuple: (provider, used_fb)

    """

    # BUG-C-05 修复：增强降级触发条件

    # 原逻辑仅匹配特定中文关键词，导致很多 AI 错误（如连接超时、SSL 错误等）

    # 无法触发降级。现在任何 AI 调用错误都尝试降级到兜底提供商。

    ai_config = get_ai_config()

    from ...services.ai.provider_factory import get_working_provider



    logger.warning("AI 调用失败，尝试降级到兜底提供商: %s", error_msg)

    if queue:

        await queue.put(

            {

                "status": "fallback",

                "message": f"主提供商不可用，自动切换到兜底提供商重试... ({error_msg[:50]})",

            }

        )

    provider, used_fb = await get_working_provider(ai_config)

    if used_fb:

        logger.info("自动降级到兜底提供商: %s", used_fb)

    return provider, used_fb

async def _fetch_project_data_async(project_id: int) -> dict | None:

    """获取项目完整数据（章节、元数据、行业配置、招标上下文等）



    优先使用 Redis 缓存，缓存未命中时从数据库查询。

    所有数据库操作通过 asyncio.to_thread 包装，不阻塞事件循环。



    Args:

        project_id: 项目 ID



    Returns:

        dict | None: 项目数据字典，包含：

            - summary: 项目摘要

            - scoring_points: 评分点列表

            - industry_profile: 行业配置

            - tender_context: 招标上下文

            - sections_by_id: 章节 ID → 章节数据映射

            - children_map: 父 ID → 子章节列表映射

            - all_rows: 所有章节行数据

            - level2_default_estimated: 二级章节默认字数

        返回 None 表示数据不可用（如未完成摘要识别）

    """

    # 先从 Redis 缓存中获取章节数据

    sections_cache = CacheService.get_project_sections(project_id)



    # 缓存可能因 sqlite Row 对象经 json.dumps(default=str) 序列化而被污染成字符串列表，

    # 这里做结构校验，无效则回退数据库全量查询，避免迭代时 string indices 崩溃

    _cache_valid = (

        isinstance(sections_cache, list)

        and sections_cache

        and all(isinstance(item, dict) for item in sections_cache)

    )

    if _cache_valid:

        all_rows = sections_cache

        logger.info("使用 Redis 缓存获取章节数据，项目 %d", project_id)



        # 缓存命中时仍需从数据库查询元数据

        def _fetch_metadata():

            with get_db() as conn:

                row = conn.execute(

                    "SELECT summary_json, scoring_points_json, global_facts_json, construction_method_json, extraction_fields_json FROM analysis WHERE project_id = ?",

                    (project_id,),

                ).fetchone()

                if not row:

                    return None, None

                project_row = conn.execute(

                    "SELECT industry FROM projects WHERE id = ?", (project_id,)

                ).fetchone()

                return row, project_row



        meta_result = await asyncio.to_thread(_fetch_metadata)

        if meta_result[0] is None:

            return None

        row, project_row = meta_result

    else:

        # 从数据库查询完整数据

        def _fetch_all():

            with get_db() as conn:

                row = conn.execute(

                    "SELECT summary_json, scoring_points_json, global_facts_json, construction_method_json, extraction_fields_json FROM analysis WHERE project_id = ?",

                    (project_id,),

                ).fetchone()

                if not row:

                    return None



                project_row = conn.execute(

                    "SELECT industry FROM projects WHERE id = ?", (project_id,)

                ).fetchone()



                all_rows = conn.execute(

                    "SELECT id, parent_id, title, level, sort_order, section_type, scoring_point_id, status, content, target_words FROM sections WHERE project_id = ? ORDER BY sort_order, id",

                    (project_id,),

                ).fetchall()

                return row, project_row, all_rows



        db_result = await asyncio.to_thread(_fetch_all)

        if db_result is None:

            return None



        row, project_row, all_rows = db_result

        # 将查询到的章节数据存入缓存前，规范化为 dict 列表，

        # 避免 sqlite Row 对象经 json.dumps(default=str) 序列化被污染成字符串，导致缓存读取时崩溃

        if all_rows:

            all_rows = [dict(r) for r in all_rows]

            CacheService.set_project_sections(project_id, all_rows)



    summary = row["summary_json"]

    scoring_points_str = row["scoring_points_json"]

    try:

        scoring_points = json.loads(scoring_points_str) if scoring_points_str else []

    except json.JSONDecodeError:

        scoring_points = []



    industry = project_row["industry"] if project_row else ""

    industry_profile = get_industry_profile(industry)



    try:

        full_text = await latest_extracted_text(project_id)

        tender_context = await extract_tender_context_async(full_text) if full_text else ""

    except Exception as _tc_e:

        # BUG-FIX 2026-09-18（中）：原为裸 `except Exception: tender_context = ""`，
        # 招标文本读取/解析异常被完全吞掉。目录生成拿不到招标上下文却照常出目录，
        # 用户无从得知「目录为何与招标文件脱节」。至少留下可排查的日志。
        logger.warning("提取招标上下文失败，将不带招标文件上下文继续: project=%d, err=%s", project_id, _tc_e)

        tender_context = ""



    sections_by_id = {r["id"]: dict(r) for r in all_rows}

    children_map: dict[int | None, list[dict]] = {}

    for r in all_rows:

        pid = r["parent_id"]

        children_map.setdefault(pid, []).append(dict(r))



    # 构建返回值字典

    result = {

        "summary": summary,

        "scoring_points": scoring_points,

        "industry_profile": industry_profile,

        "tender_context": tender_context,

        "sections_by_id": sections_by_id,

        "children_map": children_map,

        "all_rows": all_rows,

        "global_facts_items": _parse_global_facts_items(row["global_facts_json"]),

        # ENHANCE 2026-10-05（P-9）：招标文件已提取的「工程作法/施工方案」原文。
        # 正文生成据此区分「承接既有方案扩写」与「无方案从头拟定」两种流程。
        "construction_method": row["construction_method_json"] if row["construction_method_json"] else "",

        # ENHANCE 2026-10-07：行业名，供字段契约做「章节归属随行业模板」的词表匹配
        "industry": project_row["industry"] if project_row and "industry" in project_row else "",

        # ENHANCE 2026-10-07（第三轮）：结构化提取项目字段表（解析模块落库产物）
        # → 经 build_field_contract 的 extraction_fields 显式直供通道消费；
        # 列缺失/旧格式（None）时契约模块自动回落运行时启发式派生。
        "extraction_fields": (parse_extraction_fields_record(row["extraction_fields_json"]) or {}).get("fields"),

    }



    # 数据完整性验证

    _required_fields = ["summary", "scoring_points", "industry_profile"]

    for _field in _required_fields:

        if not result.get(_field):

            logger.warning("项目数据缺失字段: %s, project=%d", _field, project_id)

            if _field == "summary":

                result["summary"] = "（项目摘要未生成，请检查摘要识别步骤）"

            if _field == "scoring_points":

                # BUG-FIX 2026-09-18（中）：此处原写 `result["scoring_points"] = "[]"`
                # —— 字符串，而本函数正常路径（:571 json.loads）与所有下游消费者
                # （sse_content 的 scoring_points_list 判断、正文生成的
                # build_chapter_content_messages）都按 **list** 处理。类型契约被
                # 破坏后，下游 `isinstance(..., list)` 判定为假 → 评分点被整体丢弃，
                # 正文生成时评分要点约束静默失效。统一为空列表。
                result["scoring_points"] = []



    return result

def _build_leaf_dicts(

    project_id: int,

    sections_by_id: dict,

    children_map: dict,

    root_section_id: int | None = None,

    regenerate_ids: str = "",

    regenerate_missing: bool = False,

    level2_default: int = 3500,

    writing_length: str | None = None,

) -> list[dict]:

    """构建需要生成的叶子章节字典列表



    根据参数确定需要生成的叶子章节，并为每个章节构建完整的

    结构化数据（路径、祖先、父章节列表、同级章节列表等）。



    Args:

        project_id: 项目 ID

        sections_by_id: 章节 ID → 章节数据映射

        children_map: 父 ID → 子章节列表映射

        root_section_id: 根章节 ID（仅生成该子树）

        regenerate_ids: 指定重新生成的章节 ID 列表（逗号分隔）

        regenerate_missing: 是否仅重新生成缺失/失败的章节

        level2_default: 二级章节默认目标字数



    Returns:

        list[dict]: 叶子章节字典列表，每个字典包含：

            - id, title, section_type, scoring_point_id

            - path: 路径编号（如 "1.2.3"）

            - ancestors: 祖先标题列表

            - level: 实际层级

            - target_words: 目标字数

            - parent_chapters: 父章节信息列表

            - sibling_chapters: 同级章节信息列表



    Raises:

        ValueError: 指定的章节不存在

    """

    # 确定所有叶子节点

    all_sections = list(sections_by_id.values())

    parent_ids = {s["parent_id"] for s in all_sections if s["parent_id"] is not None}

    all_leaves = [s for s in all_sections if s["id"] not in parent_ids and s["level"] >= 1]



    # BUG-F-14 修复: 所有筛选分支统一使用"内容+状态"双重判断

    # 原逻辑只看 status 不看 content，导致：

    #   - failed 但有部分内容的章节被直接覆盖（应续写/补足）

    #   - cancelled 状态被意外选中（用户主动取消的应跳过）

    #   - completed 但 content 很短的被跳过（质量差应重生成）

    MIN_CONTENT_LEN = 50  # 低于此字数视为"无有效内容"



    def _has_valid_content(sec: dict) -> bool:

        """判断章节是否有有效内容（>MIN_CONTENT_LEN字）"""

        content = sec.get("content") or ""

        return len(content.strip()) > MIN_CONTENT_LEN



    def _build_select(leaves_subset: list) -> list:

        """对叶子子集做最终筛选：跳过 cancelled"""

        # cancelled = 用户主动取消，默认跳过（除非显式 regenerate_ids 指定）

        return [

            l for l in leaves_subset

            if l.get("status") != "cancelled"

        ]



    # 根据参数过滤叶子节点

    if regenerate_ids:

        # 显式指定章节 ID → 不管什么状态都选中（尊重用户意图）

        try:

            regen_ids = set(int(x) for x in regenerate_ids.split(",") if x.strip())

            leaves = [l for l in all_leaves if l["id"] in regen_ids]

        except ValueError:

            logger.warning("[正文生成] regenerate_ids 解析失败，忽略: %s", regenerate_ids)

            leaves = []

    elif root_section_id is not None:

        # 子树模式

        if root_section_id not in sections_by_id:

            raise ValueError("指定的章节不存在")

        subtree_leaf_ids = set(get_subtree_leaf_ids(project_id, root_section_id))

        leaves = [l for l in all_leaves if l["id"] in subtree_leaf_ids]

        if regenerate_missing:

            # 补全模式：放开 cancelled，让用户能重新生成已取消章节

            leaves = [

                l for l in leaves

                if l["status"] in ("pending", "failed", "cancelled") or not _has_valid_content(l)

            ]

        else:

            # 非补全模式：尊重用户取消，跳过 cancelled

            leaves = _build_select(leaves)

    elif regenerate_missing:

        # ============ 补全生成（核心改造） ============

        # 选中条件（OR）：

        #   1. status 是 pending / failed / cancelled（明确未完成或用户曾取消）

        #   2. 有内容但内容不足 MIN_CONTENT_LEN（空壳章节）

        #   3. status 是 active 但无有效内容（状态与内容不一致）

        # 跳过：仅 completed 且内容够的（真正完成的）

        #   注意：补全模式要包含 cancelled——用户点"补全生成"即表示重新生成曾取消的章节

        leaves = [

            l for l in all_leaves

            if (

                l.get("status") in ("pending", "failed", "cancelled")

                or not _has_valid_content(l)

            )

        ]

    else:

        # ============ 生成全部（默认模式） ============

        # 选中条件（OR）：

        #   1. status 不是 completed（含 failed/pending/active）

        #   2. 有内容但内容不足（completed 但质量差也要重生成）

        # 跳过：

        #   - cancelled（用户主动取消的）

        #   - completed 且有足够内容的（真正完成的）

        leaves = [

            l for l in all_leaves

            if l.get("status") != "cancelled"  # 跳过用户主动取消的

            and (

                l["status"] != "completed"

                or not _has_valid_content(l)  # completed 但没内容也重生成

            )

        ]



    # BUG-F-14: 筛选结果统计日志

    if regenerate_ids:

        # BUG-FIX 2026-09-16（中）：这里的 `int()` 没有任何 try 保护。
        # 形如 "abc,5" 的混合输入在上方的解析块里确实会走进 except 把 leaves 置空，
        # 但业务流程并不依赖 leaves 非空 —— 一旦 leaves 非空而这里再解析一次，
        # ValueError 会直接穿透 `_build_leaf_dicts`，SSE 500 断流。
        try:

            _count = len(set(int(x) for x in regenerate_ids.split(",") if x.strip()))

        except ValueError:

            _count = len(leaves)

        _mode = f"regenerate_ids({_count})"

    elif regenerate_missing:

        _mode = "补全生成(regenerate_missing)"

    elif root_section_id is not None:

        _mode = f"子树生成(root={root_section_id}, missing={regenerate_missing})"

    else:

        _mode = "生成全部(默认)"



    _status_counts = {}

    for _l in leaves:

        _s = _l.get("status", "?")

        _status_counts[_s] = _status_counts.get(_s, 0) + 1

    logger.info(

        "[章节筛选] 模式=%s 选中=%d  状态分布=%s",

        _mode, len(leaves), str(_status_counts),

    )



    if not leaves:

        return []



    leaf_dicts = []

    for leaf in leaves:

        path, ancestors = _get_path_and_ancestors(leaf["id"], sections_by_id, children_map)

        effective_target = _get_effective_target_words(leaf, level2_default, writing_length)



        # 构建结构化的父章节、同级章节列表

        parent_chapters: list[dict] = []

        sibling_chapters: list[dict] = []

        try:

            # 追溯父链

            cur = sections_by_id.get(leaf["id"])

            chain = []

            while cur:

                chain.append(cur)

                pid = cur.get("parent_id")

                cur = sections_by_id.get(pid) if pid else None

            # chain 顺序：当前 → ... → 根；倒序后为 根 → ... → 父级（不含当前）

            chain.reverse()

            for node in chain[:-1]:

                parent_chapters.append(

                    {

                        "id": str(node.get("id", "")),

                        "title": node.get("title", ""),

                        "level": node.get("level", 1),

                        "description": node.get("description", "") or "",

                    }

                )

            # 同级：同一 parent_id 的其他节点（不含当前）

            pid_of_leaf = leaf.get("parent_id")

            if pid_of_leaf is not None:

                for sib in children_map.get(pid_of_leaf, []):

                    if sib.get("id") == leaf["id"]:

                        continue

                    sibling_chapters.append(

                        {

                            "id": str(sib.get("id", "")),

                            "title": sib.get("title", ""),

                            "level": sib.get("level", 2),

                            "description": sib.get("description", "") or "",

                        }

                        )

        except Exception as _e:
            logger.debug("[章节筛选] 父/同级构建失败: %s", _e)
            # 容错：父/同级构建失败不阻塞生成

            pass



        leaf_dicts.append(

            {

                "id": leaf["id"],

                "title": leaf["title"],

                # BUG-FIX 2026-09-16（P1 死代码）：原先漏了 parent_id/status。
                # `workflows_sections.generate_sections` 正是靠这两个字段构建
                # 「同级章节 / 上级章节」映射（P0-TEXT-FLOW 防车轱辘话、承上启下），
                # 缺 parent_id 时 `if _pid and _pid in _cld_map` 永假 →
                # 两张映射表恒为空 → 注入 prompt 的上下文一直是空的，
                # 「避免与同级章节重复」这条铁律从未真正下发给 AI。

                "parent_id": leaf.get("parent_id"),

                "status": leaf.get("status", ""),

                "section_type": leaf["section_type"],

                "scoring_point_id": leaf["scoring_point_id"],

                "path": path,

                "ancestors": ancestors,

                "level": leaf.get("level", 3),

                "target_words": effective_target,

                # 2026-10-08 新增：字数档位信息（用于提示词注入与前端展示）
                "word_count_tier": leaf.get("word_count_tier"),
                "tier_label": _resolve_tier_label(leaf.get("word_count_tier"), writing_length),

                # L3 自身正文（三级目录）：用于生成与校验的独立标识
                # L3 自身正文不含下级目录章节；只有存在 L4+ 子目录时
                # 四档口径（子树总字数）才参与校验。
                "is_l3_own": leaf.get("level") == 3,
                # L3 下级目录章节（仅当存在 L4+ 时才有真实子目录）
                "child_chapters": [
                    {
                        "id": str(c.get("id")),
                        "title": c.get("title"),
                        "level": c.get("level", 4),
                        "description": c.get("description") or "",
                        "status": c.get("status", ""),
                        "section_type": c.get("section_type"),
                        "content": c.get("content") or "",
                    }
                    for c in children_map.get(leaf["id"], [])
                ],

                "parent_chapters": parent_chapters,

                "sibling_chapters": sibling_chapters,

            }

        )



    # BUG-CONCURRENCY-ORDER: 按目录树 path 自然排序，确保生成顺序严格按目录树前序遍历

    # path 格式为 "1.2.3"（兄弟在 children 列表中的位置索引），

    # 解析为 [1, 2, 3] 整数元组做排序，解决 "1.10" < "1.2" 字符串排序问题

    def _path_sort_key(d: dict) -> tuple:

        parts = d.get("path", "0")

        try:

            return tuple(int(p) for p in parts.split("."))

        except ValueError:

            return (99999,)



    leaf_dicts.sort(key=_path_sort_key)

    logger.info(

        "[章节排序] 已按目录树 path 排序，首条=%s, 末条=%s",

        leaf_dicts[0].get("path") if leaf_dicts else "-",

        leaf_dicts[-1].get("path") if leaf_dicts else "-",

    )



    return leaf_dicts
