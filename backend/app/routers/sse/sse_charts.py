"""sse_handlers 拆分模块 — sse_charts.py"""

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
from ...services.ai.workflows import generate_outline as _run_outline_workflow
from ...services.ai.workflows import generate_sections as _run_sections
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
from ..task_control import _cancel_events, _cancel_events_lock, _cancel_tasks

from .sse_shared import logger
from .sse_utils import _resolve_chart_field

async def _auto_generate_charts_for_section(

    project_id: int,

    section_data: dict,

    provider: Any,

    writing_length: str,

    *,

    scoring_standard: str = "",

    industry_profile: str = "",

) -> dict | None:

    """自动为章节生成配图（流程图/甘特图/架构图等）— V7.0 批量编排增强版



    Args:

        project_id: 项目 ID

        section_data: 章节结果字典，包含 id, title, content 等字段

        provider: AI 提供商实例

        writing_length: 写作长度

        scoring_standard: 评分标准文本（可选，用于编排判断）

        industry_profile: 行业背景（可选，用于编排判断）

    Returns:

        dict | None: 生成的图表数据，无需生成时返回 None

    """

    section_id = section_data.get("id")

    content = section_data.get("content", "") or ""

    title = section_data.get("title", "")



    if not content or not section_id:

        return None



    from ...services.ai.workflows import extract_section_charts



    # PERF-FIX-2026-09-08: sync DB → asyncio.to_thread，避免 EventLoop 阻塞

    def _fetch_summary_sync(_pid: int) -> str:

        # BUG-FIX 2026-09-14（高）：原实现直接使用未定义的 `_conn`（本函数内从未
        # 打开连接），必然抛 NameError，而 except 又静默吞掉返回 ""。
        # 后果：批量配图编排的「项目概述」恒为空，AI 建图 / 编排丢失项目上下文。
        # 修复：显式打开只读连接；异常改为 warning 日志，避免静默失败。
        try:

            with get_db(readonly=True) as _conn:

                _row = _conn.execute(

                    "SELECT summary_json FROM analysis WHERE project_id = ?", (_pid,),

                ).fetchone()

            return (_row["summary_json"] or "") if _row else ""

        except Exception as _e:

            logger.warning("[配图] 读取项目概述失败 project=%s: %s", _pid, _e)

            return ""



    summary = await asyncio.to_thread(_fetch_summary_sync, project_id)



    # PERF-FIX-2026-09-08: save helper（to_thread 包装 sync update_section）

    async def _save_section_field(_sid: int, _field: str, _json: str) -> None:

        try:

            await asyncio.to_thread(

                update_section, _sid,

                SectionUpdate(**{_field: _json}),

            )

        except Exception as _e:

            logger.warning(

                "保存内联图表失败 section=%d field=%s: %s", _sid, _field, _e,

            )



    # ===== 阶段一：提取正文内联标记（真实数据 / 配图位置提示，二者必须区分）=====
    # BUG-FIX 2026-09-16（高）：原实现把 extract_section_charts 的所有返回值一律当成
    # 图表数据、并无条件 `return charts[0]`，存在三个叠加缺陷：
    #   1. 纯文本占位符（`<<CHART:{施工进度甘特图}>>`，注释记载占 AI 实际输出 96.7%）
    #      被当成数据落库 → DB 字段里是没有 nodes/tasks 的空壳载荷；
    #   2. `chart.get("data")` 取的是嵌套字段，而绝大多数标记的数据**平铺**在顶层
    #      （type/nodes/tasks...）→ 真实的内联图表数据从未落库；
    #   3. 无论命中哪种，函数都会 return → 阶段二/三（批量编排 + 分类型生成）被整体跳过。
    # 结果：正文标注了配图位置、DB 里是无效载荷、真实配图也没生成，导出时渲染失败且
    # 章节末尾也不会补图 → 静默丢图。
    # BUG-FIX 2026-09-18（P1）：extract_section_charts 内含 _try_repair_json 的
    # 最坏十万次 json.loads（重 CPU），同函数内 DB 读写均已 to_thread，唯独这里
    # 同步直调会阻塞事件循环，拖垮同进程全部并发请求。改投线程池执行。
    charts = await asyncio.to_thread(
        extract_section_charts, content, section_title=title, summary=summary
    )

    # 1) 真实数据条目 → 落库（供正文预览 / DOCX 导出就地渲染）
    real_charts: list[dict] = []
    for chart in charts:
        if chart.get("_hint"):
            continue  # 提示条目不含数据 → 交给编排管线生成，绝不落库
        chart_type = str(chart.get("type") or "").lower()
        chart_field = _resolve_chart_field(chart_type)
        if not chart_field:
            continue
        payload = {k: v for k, v in chart.items() if not k.startswith("_")}
        # 兼容历史「嵌套 {type, data} 结构」（见 extract_section_charts 文档）
        _inner = payload.get("data")
        if isinstance(_inner, dict) and len(payload) <= 3:
            _merged = dict(_inner)
            _merged.setdefault("type", chart_type)
            payload = _merged
        if not payload:
            continue
        await _save_section_field(section_id, chart_field, json.dumps(payload, ensure_ascii=False))
        real_charts.append({"type": chart_type, "data": payload})
        logger.info("内联图表数据已落库: section=%d type=%s", section_id, chart_type)

    # 2) 配图位置提示条目 → 转为编排阶段的强制意图（不落库，避免伪造数据）
    chart_hints: dict[str, str] = {}
    for chart in charts:
        if not chart.get("_hint"):
            continue
        _hint_type = str(chart.get("type") or "").lower()
        if _hint_type:
            chart_hints.setdefault(
                _hint_type, str(chart.get("title") or chart.get("chart_title") or "")
            )
    if chart_hints:
        logger.info("正文配图位置提示: section=%d hints=%s", section_id, sorted(chart_hints))

    if real_charts:
        # 正文已内联真实图表数据 → 这些类型不再走编排生成（避免覆盖 / 重复出图）
        #
        # BUG-FIX 2026-09-18（中）：原实现是无条件 `return real_charts[0]`，
        # 直接跳过整个编排阶段。当同一章节**既有**真实图表数据、**又有**其他类型的
        # 配图提示（`<<CHART:甘特图>>` 这类无数据的 hint）时，这些提示被静默丢弃 ——
        # 正文里明确标注的图永远不出现，用户无从察觉。
        # 现在只把「已有真实数据的类型」从编排意图中剔除，其余提示继续走编排。
        _real_types = {c.get("type") for c in real_charts if c.get("type")}
        chart_hints = {k: v for k, v in chart_hints.items() if k not in _real_types}
        if not chart_hints:
            logger.info(
                "章节已有真实图表数据且无其他配图提示，跳过编排: section=%d types=%s",
                section_id,
                sorted(_real_types),
            )
            return real_charts[0]
        logger.info(
            "章节已有真实图表数据 %s，仍需为提示类型 %s 走编排生成: section=%d",
            sorted(_real_types),
            sorted(chart_hints),
            section_id,
        )



    # ===== 阶段二：批量编排 — 单次 AI 调用判断所有图表类型 =====

    try:

        from ...services.ai.image_engine import generate_all_chart_arrangements



        arrangements = await generate_all_chart_arrangements(

            provider,

            section_title=title,

            section_content=content,

            project_overview=summary,

            scoring_standard=scoring_standard,

            industry_profile=industry_profile,

            # 正文 `<<CHART:类型>>` 标注的配图意图 → 编排阶段强制 needed=true
            chart_hints=chart_hints,

        )

    except Exception as e:

        logger.warning("批量图表编排失败 section=%d: %s", section_id, e)

        return None



    if not isinstance(arrangements, dict):

        return None



    # ===== 阶段三：生成所有需要的图表（并行）=====

    generated_charts: list[dict] = []



    async def _generate_and_save(chart_type: str, arrange: dict) -> dict | None:

        """生成单个图表并保存到数据库"""

        if not arrange or not arrange.get("needed"):

            return None

        # 图题：优先用正文标记里给出的标题（`<<CHART:{施工进度甘特图}>>`），
        # 其次用章节标题 —— 保证 DOCX 图题与正文语义一致。
        _chart_title = str(arrange.get("hintTitle") or title or "").strip()



        try:

            from ...services.ai.image_engine import (

                generate_architecture_tree,

                generate_comparison_mermaid,

                generate_labor_data,

                generate_layout_mermaid,

                generate_mermaid_flowchart,

                generate_mermaid_gantt,

                generate_timeline_mermaid,

            )



            chart_field = _resolve_chart_field(chart_type)

            if not chart_field:

                return None



            # 根据类型调用对应的生成函数

            if chart_type == "flowchart":

                mermaid_code = await generate_mermaid_flowchart(

                    provider,

                    section_title=title,

                    section_content=content,

                    scoring_standard=scoring_standard,

                    industry_profile=industry_profile,

                    project_overview=summary,

                    arrange_result=arrange,

                )

                if mermaid_code:

                    chart_data = {"type": "flowchart", "code": mermaid_code, "title": _chart_title}

                    await _save_section_field(

                        section_id,

                        chart_field,

                        json.dumps(chart_data, ensure_ascii=False),

                    )

                    generated_charts.append({"type": chart_type, "data": chart_data})

                    logger.info("批量编排: 流程图生成成功 section=%d", section_id)

                    return {"type": chart_type, "data": chart_data}



            elif chart_type == "gantt":

                mermaid_code = await generate_mermaid_gantt(

                    provider,

                    section_title=title,

                    section_content=content,

                    scoring_standard=scoring_standard,

                    industry_profile=industry_profile,

                    summary=summary,

                    arrange_result=arrange,

                )

                if mermaid_code:

                    chart_data = {"type": "gantt", "code": mermaid_code, "title": _chart_title}

                    await _save_section_field(

                        section_id,

                        chart_field,

                        json.dumps(chart_data, ensure_ascii=False),

                    )

                    generated_charts.append({"type": chart_type, "data": chart_data})

                    logger.info("批量编排: 甘特图生成成功 section=%d", section_id)

                    return {"type": chart_type, "data": chart_data}



            elif chart_type == "architecture":

                arch_tree = await generate_architecture_tree(

                    provider,

                    section_title=title,

                    section_content=content,

                    project_overview=summary,

                    arrange_result=arrange,

                    writing_length=writing_length,

                    industry_profile=industry_profile,

                )

                if arch_tree and arch_tree.get("id"):

                    chart_data = {"type": "architecture", "tree": arch_tree, "title": _chart_title}

                    await _save_section_field(

                        section_id,

                        chart_field,

                        json.dumps(chart_data, ensure_ascii=False),

                    )

                    generated_charts.append({"type": chart_type, "data": chart_data})

                    logger.info("批量编排: 架构图生成成功 section=%d", section_id)

                    return {"type": chart_type, "data": chart_data}



            elif chart_type == "labor":

                labor_data = await generate_labor_data(

                    provider,

                    section_title=title,

                    section_content=content,

                    project_overview=summary,

                    arrange_result=arrange,

                    writing_length=writing_length,

                    industry_profile=industry_profile,

                )

                if labor_data and labor_data.get("phases"):

                    chart_data = {"type": "labor", **labor_data, "title": _chart_title}

                    await _save_section_field(

                        section_id,

                        chart_field,

                        json.dumps(chart_data, ensure_ascii=False),

                    )

                    generated_charts.append({"type": chart_type, "data": chart_data})

                    logger.info("批量编排: 劳动力图生成成功 section=%d", section_id)

                    return {"type": chart_type, "data": chart_data}



            elif chart_type == "comparison":

                mermaid_code = await generate_comparison_mermaid(

                    provider,

                    section_title=title,

                    section_content=content,

                    project_overview=summary,

                    arrange_result=arrange,

                )

                if mermaid_code:

                    chart_data = {"type": "comparison", "code": mermaid_code, "title": _chart_title}

                    await _save_section_field(

                        section_id,

                        chart_field,

                        json.dumps(chart_data, ensure_ascii=False),

                    )

                    generated_charts.append({"type": chart_type, "data": chart_data})

                    logger.info("批量编排: 对比图生成成功 section=%d", section_id)

                    return {"type": chart_type, "data": chart_data}



            elif chart_type == "layout":

                mermaid_code = await generate_layout_mermaid(

                    provider,

                    section_title=title,

                    section_content=content,

                    project_overview=summary,

                    arrange_result=arrange,

                )

                if mermaid_code:

                    chart_data = {"type": "layout", "code": mermaid_code, "title": _chart_title}

                    await _save_section_field(

                        section_id,

                        chart_field,

                        json.dumps(chart_data, ensure_ascii=False),

                    )

                    generated_charts.append({"type": chart_type, "data": chart_data})

                    logger.info("批量编排: 布局图生成成功 section=%d", section_id)

                    return {"type": chart_type, "data": chart_data}



            elif chart_type == "timeline":

                mermaid_code = await generate_timeline_mermaid(

                    provider,

                    section_title=title,

                    section_content=content,

                    project_overview=summary,

                    arrange_result=arrange,

                )

                if mermaid_code:

                    chart_data = {"type": "timeline", "code": mermaid_code, "title": _chart_title}

                    await _save_section_field(

                        section_id,

                        chart_field,

                        json.dumps(chart_data, ensure_ascii=False),

                    )

                    generated_charts.append({"type": chart_type, "data": chart_data})

                    logger.info("批量编排: 时间线图生成成功 section=%d", section_id)

                    return {"type": chart_type, "data": chart_data}



        except Exception as e:

            logger.warning(

                "批量编排: 图表生成失败 section=%d type=%s: %s", section_id, chart_type, e

            )

            return None



        return None



    # 并行生成所有需要的图表

    needed_charts = {

        k: v for k, v in arrangements.items() if isinstance(v, dict) and v.get("needed")

    }

    if needed_charts:

        tasks = [_generate_and_save(ct, arr) for ct, arr in needed_charts.items()]

        await asyncio.gather(*tasks, return_exceptions=True)



    if generated_charts:

        logger.info(

            "批量编排配图完成: section=%d, 图表数=%d, 类型=%s",

            section_id,

            len(generated_charts),

            [c["type"] for c in generated_charts],

        )

        return generated_charts[0]

    # BUG-FIX 2026-09-18：走到这里说明「有真实图表数据 + 有提示类型」，
    # 但编排未能产出任何新图。此时仍应回报已落库的真实图表，而不是 None。
    if real_charts:

        return real_charts[0]

    return None
