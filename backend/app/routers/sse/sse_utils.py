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
    """计算章节的有效目标字数（权威口径收敛至 ``word_target_resolver``）。

    CONVERGE 2026-10-12：本函数曾是 4 份同构实现之一（另 3 份在
    ``routers/word_count.py``、``workflows_sections`` 提示词侧、
    ``workflows_expand.section_expand_threshold``，压缩侧还有第 5 份内联解析），
    历史上多次出现「生成按 detailed 写、校验按 standard 判」的分叉故障。
    现全部改为调用唯一权威实现 ``services/word_target_resolver.py``，
    本函数退化为**薄转发**（保留原签名与返回值语义，含 L1/L2 → None）。

    口径（唯一公式，详见该模块 docstring）：
      L1/L2 不参与控制 → 单节点 target_words > 单节点 word_count_tier
      > L3 自身正文 300±20% > 全局 writing_length 档位 > 默认档位。

    Args:
        section: 章节字典（含 level / target_words / word_count_tier 等）
        level2_default: 历史兼容参数，L2 已不再使用该值；仅保留以免旧调用报错
        writing_length: 全局档位 key（brief/standard/detailed/long，兼容 short/extra_long）

    Returns:
        int | None: 目标字数；对 L1/L2 返回 None（不参与控制）
    """
    from ...services.word_target_resolver import (
        resolve_section_word_target_from_mapping,
    )

    resolved = resolve_section_word_target_from_mapping(section, writing_length=writing_length)
    return resolved.target if resolved else None


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

        # BUG-FIX 2026-10-28（P2 数据链断点）：缓存可能由修复前 SQL（未选
        # word_count_tier 列）填充，命中行缺该 key → 消费端恒 None → 单节档位
        # 覆盖在生成侧被静默丢弃。此处对缓存行做列补齐防御（缺列补 None，
        # 与 SELECT 列口径一致），使旧缓存不丢字段、新缓存直接命中。
        for _r in all_rows:
            if "word_count_tier" not in _r:
                _r["word_count_tier"] = None



        # 缓存命中时仍需从数据库查询元数据

        def _fetch_metadata():

            with get_db() as conn:

                row = conn.execute(

                    # ENHANCE 2026-10-14（facts-ns P1-3）：补读 facts_policy，供正文生成
                    # 按缺失值策略追加缺值口径铁律（口径同 routers/ai.py::_sync_get_analysis）。
                    # A-5 2026-10-17：补 confirmed_summary/confirmed_scoring 软闸门
                    "SELECT summary_json, scoring_points_json, global_facts_json, construction_method_json, extraction_fields_json, "
                    "COALESCE(facts_policy, 'fabricate') AS facts_policy, "
                    "confirmed_summary, confirmed_scoring "
                    "FROM analysis WHERE project_id = ?",

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

                    # ENHANCE 2026-10-14（facts-ns P1-3）：补读 facts_policy（同上）。
                    # A-5 2026-10-17：补 confirmed_summary/confirmed_scoring 软闸门
                    "SELECT summary_json, scoring_points_json, global_facts_json, construction_method_json, extraction_fields_json, "
                    "COALESCE(facts_policy, 'fabricate') AS facts_policy, "
                    "confirmed_summary, confirmed_scoring "
                    "FROM analysis WHERE project_id = ?",

                    (project_id,),

                ).fetchone()

                if not row:

                    return None



                project_row = conn.execute(

                    "SELECT industry FROM projects WHERE id = ?", (project_id,)

                ).fetchone()



                all_rows = conn.execute(

                    "SELECT id, parent_id, title, level, sort_order, section_type, scoring_point_id, status, content, target_words, locked, content_mode, word_count_tier FROM sections WHERE project_id = ? ORDER BY sort_order, id",

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

    # A-5 BUG-FIX 2026-10-17（软闸门）：confirmed_summary/confirmed_scoring 读取。
    # 正文生成链路（与目录链路同口径）——数据非空但未确认时 logger.warning +
    # 返回值带 is_confirmed 标志，不硬拦截（行为兼容）但让消费方可观测。
    # 注意：sse_utils 的 row 是原始 sqlite3.Row（无 .get()，与 sse_db_ops 转 dict 不同），
    # 但列名在两条 SELECT 中都显式列出，下标访问安全。
    _confirmed_summary = bool(row["confirmed_summary"])
    _confirmed_scoring = bool(row["confirmed_scoring"])
    _has_summary = bool(summary) and summary not in ("{}", "", "待AI解析")
    _has_scoring = isinstance(scoring_points, list) and len(scoring_points) > 0
    _is_confirmed = _confirmed_summary and _confirmed_scoring
    if _has_summary and not _confirmed_summary:
        logger.warning(
            "[正文生成] summary 非空但未确认 (confirmed_summary=0)，"
            "将基于未确认摘要生成正文 project=%d", project_id,
        )
    if _has_scoring and not _confirmed_scoring:
        logger.warning(
            "[正文生成] scoring_points 非空但未确认 (confirmed_scoring=0)，"
            "将基于未确认评分点生成正文 project=%d", project_id,
        )



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

    # 2026-10-18 ENHANCE（第二轮）：同级顺序不依赖 SQL ORDER BY。
    # `_get_path_and_ancestors` 用「节点在同级列表中的下标 +1」派生编号 path，
    # 旧实现依赖 `ORDER BY sort_order, id` 的 SQL 结果 —— 一旦上游查询改写排序
    # （或改走缓存源），path 编号即错位，正文生成顺序随之错乱。
    # 现显式按唯一权威 sibling_sort_key=(sort_order, id) 排序每个同级桶，
    # 使 path 派生口径与 SQL 层解耦。
    try:
        from ...services.ai.ordered_generation import (
            audit_children_map,
            sibling_sort_key,
        )

        for _bucket in children_map.values():
            _bucket.sort(key=sibling_sort_key)
    except ImportError:  # pragma: no cover - 防御性兜底（等价旧口径）
        for _bucket in children_map.values():
            _bucket.sort(key=lambda s: (s.get("sort_order") or 0, s.get("id") or 0))
        audit_children_map = None

    # 遗留加固（2026-10-19）：桶表自检（无论走哪条排序分支都执行）。
    # path 由「节点在同级桶中的下标 +1」派生，桶表是唯一依据。上面只保证了
    # **本次构建**的桶是有序的；若未来有其它代码路径绕过 `_load_project_data`
    # 直接改写 children_map（增删节点、外部重建桶），path 会静默错位且生成
    # 结果里极难察觉 —— 此处做只读自检并留日志（不阻断生成）。
    if audit_children_map is not None:
        try:
            _cm_audit = audit_children_map(all_rows, children_map)
            if not _cm_audit["ok"]:
                logger.warning(
                    "[children-map] project=%d 桶表自检未通过: %s",
                    project_id,
                    [(i["type"], i["detail"]) for i in _cm_audit["issues"]],
                )
        except Exception as _cm_e:  # pragma: no cover - 自检本身不得阻断生成
            logger.debug("[silent-except] children_map 自检失败: %s", _cm_e)



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

        # ENHANCE 2026-10-14（facts-ns P1-3）：缺失值策略随数据下发，供正文生成
        # 按策略追加缺值口径铁律；旧 Redis 缓存无此键时消费方按 fabricate 处理。
        "facts_policy": (row["facts_policy"] if "facts_policy" in row.keys() else "fabricate") or "fabricate",

        # ENHANCE 2026-10-05（P-9）：招标文件已提取的「工程作法/施工方案」原文。
        # 正文生成据此区分「承接既有方案扩写」与「无方案从头拟定」两种流程。
        "construction_method": row["construction_method_json"] if row["construction_method_json"] else "",

        # ENHANCE 2026-10-07：行业名，供字段契约做「章节归属随行业模板」的词表匹配
        "industry": project_row["industry"] if project_row and "industry" in project_row else "",

        # ENHANCE 2026-10-07（第三轮）：结构化提取项目字段表（解析模块落库产物）
        # → 经 build_field_contract 的 extraction_fields 显式直供通道消费；
        # 列缺失/旧格式（None）时契约模块自动回落运行时启发式派生。
        "extraction_fields": (parse_extraction_fields_record(row["extraction_fields_json"]) or {}).get("fields"),

        # A-5 2026-10-17 软闸门标志（与目录链路同口径）
        "is_confirmed": _is_confirmed,
        "confirmed_summary": _confirmed_summary,
        "confirmed_scoring": _confirmed_scoring,

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



    def _resolve_content_mode(node: dict) -> str | None:
        """沿父链继承最近的 content_mode（2026-10-20 遗留 5）。

        content_mode 当前只写在程序构造的一级目录上（ai-generate），正文
        生成的对象是叶子章节。不继承会导致叶子一律拿到 None，模式契约在
        生成侧名存实亡。子节点显式设置的 content_mode 优先于祖先。
        """
        seen: set = set()
        cur = node
        while cur is not None and cur.get("id") not in seen:
            seen.add(cur.get("id"))
            mode = cur.get("content_mode")
            if mode:
                return str(mode)
            pid = cur.get("parent_id")
            cur = sections_by_id.get(pid) if pid is not None else None
        return None



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

        # ENHANCE 2026-10-21（正文 #5）：子树叶子集合改用**同一份** sections_by_id
        # 内存数据派生，消除原先 get_subtree_leaf_ids 的二次读库，及其与 all_leaves
        # （缓存/略早快照）之间的时序差，保证子树选集与叶子判定严格同源。
        _children: dict[int | None, list[int]] = {}
        for _s in all_sections:
            _children.setdefault(_s["parent_id"], []).append(_s["id"])
        _subtree_ids: set[int] = set()
        _stack = [root_section_id]
        while _stack:
            _nid = _stack.pop()
            if _nid in _subtree_ids:
                continue
            _subtree_ids.add(_nid)
            _stack.extend(_children.get(_nid, []))

        leaves = [l for l in all_leaves if l["id"] in _subtree_ids]

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
                # 2026-10-18 ENHANCE: carry DB sort_order for stable tree ordering
                "sort_order": leaf.get("sort_order"),
                "status": leaf.get("status", ""),

                "section_type": leaf["section_type"],

                "scoring_point_id": leaf["scoring_point_id"],

                # 2026-10-20 遗留 5：目录落库的正文模式随叶子字典下发，
                # 沿父链继承最近的 content_mode（默认 ai-generate），
                # 正文生成入口 resolve_content_mode 消费。
                "content_mode": _resolve_content_mode(leaf),

                "path": path,

                "ancestors": ancestors,

                "level": leaf.get("level", 3),

                "target_words": effective_target,
                # 体系合并 2026-10-20：生成侧契约需要 DB 原始 target_words
                # （``target_words`` 字段存的是解析后的有效目标，供面板/落库展示）；
                # 旧「提示词侧契约 P-22」移除后，单节显式覆盖由统一解析器首项尊重。
                "target_words_raw": leaf.get("target_words"),

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



    # 2026-10-18 ENHANCE（第二轮）：目录树顺序排序 —— 生成顺序的唯一决定点。
    # 排序字段：节点 ID、编号（path）、父节点 ID、兄弟节点序号（sort_order）；
    # 排序规则由目录生成模块写入（sections.sort_order 1 起连续），
    # 正文生成只读不重排，排序结果必须稳定。
    #
    # BUG-FIX 2026-10-18（第二轮）：首轮增强误用 `(sort_order, path)` 作排序键 ——
    # sort_order 是**同父节点下**的兄弟序号，不同父节点下会重复（每个 L2 的第一个
    # 子节 sort_order 都是 1），按它做主键会把两个 L2 子树交错排序
    # （1.1.1, 1.2.1, 1.1.2, 1.2.2），即「跳章乱序」。正确口径：编号 path 是
    # 位置派生编号（逐级拼接 sort_order），按 path 元组升序即严格的深度优先
    # 前序（L1→L7 层级顺序、同级按编号升序）；sort_order 与 id 仅在同 path
    # 前缀下决胜，保证排序稳定。权威实现：ordered_generation.tree_sort_key。
    try:
        from ...services.ai.ordered_generation import (
            sort_sections_by_tree_order,
            verify_tree_order,
        )
        # 先校验输入顺序（告警用），再按目录树顺序重排（只读 sort_order，不改变值）
        _pre_order = verify_tree_order(leaf_dicts)
        if not _pre_order.get("ok", True):
            logger.warning(
                "[section-sort] 叶子输入未按目录树顺序排列（已按树序重排）: %s",
                _pre_order.get("violations", [])[:5],
            )
        leaf_dicts = sort_sections_by_tree_order(leaf_dicts)
    except ImportError:  # pragma: no cover - 防御性兜底（与权威实现等价）
        def _sort_key(d: dict) -> tuple:
            parts = d.get("path", "0")
            try:
                path_tuple = tuple(int(p) for p in parts.split("."))
            except ValueError:
                path_tuple = (99999,)
            so = d.get("sort_order")
            so_val = int(so) if isinstance(so, (int, float)) else 0
            return (path_tuple, so_val, d.get("id") or 0)

        leaf_dicts.sort(key=_sort_key)

    logger.info(
        "[section-sort] 目录树顺序（path 前序）: n=%d, first=%s(sort=%s), last=%s(sort=%s)",
        len(leaf_dicts),
        leaf_dicts[0].get("path") if leaf_dicts else "-",
        leaf_dicts[0].get("sort_order") if leaf_dicts else "-",
        leaf_dicts[-1].get("path") if leaf_dicts else "-",
        leaf_dicts[-1].get("sort_order") if leaf_dicts else "-",
    )

    # 断点续生成：记录「最后成功节点 → 下一节点」定位（只读日志，不改变选集）。
    # regenerate_missing / 主生成续跑都按此口径从下一节点按顺序继续。
    # 口径：对**全部叶子**（按目录树顺序）找第一个未完成节点。
    try:
        from ...services.ai.ordered_generation import find_resume_point

        # 原始行无 path 字段，用权威 path 计算补齐后按目录树顺序排全部叶子
        _all_leaves_with_path = []
        for _s in all_leaves:
            _p, _ = _get_path_and_ancestors(_s["id"], sections_by_id, children_map)
            _all_leaves_with_path.append({**_s, "path": _p})
        _all_leaves_sorted = sorted(_all_leaves_with_path, key=lambda s: (
            tuple(int(p) if p.isdigit() else 99999
                  for p in str(s.get("path") or "0").split(".")),
            int(s.get("sort_order") or 0),
            s.get("id") or 0,
        ))
        _all_leaf_ids = [s["id"] for s in _all_leaves_sorted]
        _done_ids = [
            s["id"] for s in _all_leaves_sorted
            if s.get("status") == "completed" and _has_valid_content(s)
        ]
        _resume = find_resume_point(_all_leaf_ids, _done_ids)
        if not _resume.get("all_completed"):
            logger.info(
                "[section-resume] 断点续生定位: 叶子总数=%d, 已完成=%d, "
                "下一未完成节点 index=%d id=%s",
                len(_all_leaf_ids), len(_done_ids),
                _resume.get("next_index"), _resume.get("next_id"),
            )
    except Exception as _resume_e:  # pragma: no cover - 防御性
        logger.debug("[section-resume] 断点定位失败（忽略）: %s", _resume_e)

    return leaf_dicts
