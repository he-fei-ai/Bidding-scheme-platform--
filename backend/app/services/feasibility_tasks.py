# -*- coding: utf-8 -*-
"""可行性研究报告 AI 任务编排（ENHANCE 2026-09-15，移植自易标 feasibilityReportTasks.cjs）。

四个后台任务，与易标同名职责：
1. `run_analysis_task`      —— 资料分析：分段提取九板块事实 JSON → 合并 → Markdown；
2. `run_outline_task`       —— 报告目录：AI 生成三级目录 JSON → 归一化（剥编号/限层级/分配 id）；
3. `run_parameters_task`    —— 关键参数：全文编制口径（缺失写【待补充】，严禁编造数字）；
4. `run_content_task`       —— 正文生成：逐叶撰写 + 「自然化审校」润色两阶段，
   润色阶段有 **事实保护**（protectFacts）：数量/金额/日期与【待补充】【待确认】标记
   若在润色结果中丢失，则整段保留原文 —— 防 AI 审校把关键事实改丢。

所有任务通过 `save_task_state` 持久化进度（前端轮询 `GET /feasibility` 即可看到），
成功后把产物 checkpoint 进工作区，失败把异常写进任务行。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from typing import Any

from ..utils.json_parser import extract_json_from_text
from . import feasibility as store
from . import feasibility_knowledge as knowledge_material
from .ai.providers.base import AIMessage
from .ai.workflows import collect_json_response, with_timeout
from .feasibility_prompts import (
    analysis_to_markdown,
    build_analysis_merge_system_prompt,
    build_analysis_merge_user_instruction,
    build_analysis_system_prompt,
    build_analysis_user_instruction,
    build_content_system_prompt,
    build_content_writing_rules,
    build_human_writing_system_prompt,
    build_knowledge_background_note,
    build_knowledge_catalog_note,
    build_knowledge_material_note,
    build_outline_system_prompt,
    build_outline_user_instruction,
    build_parameters_system_prompt,
    build_parameters_user_instruction,
    format_project_info,
    render_outline_for_prompt,
)

logger = logging.getLogger(__name__)

#: 资料分析分段字符数（超过则分段提取后合并）
_ANALYSIS_SEGMENT_CHARS = 20000
#: 单次 AI 调用超时（秒）
_CHAT_TIMEOUT = 300
#: 事实保护：数量/金额/日期等受保护 token
_PROTECTED_QUANTITY_RE = re.compile(
    r"\d+(?:\.\d+)?(?:\s*(?:-|～|~|至)\s*\d+(?:\.\d+)?)?\s*"
    r"(?:亿元|万元|元|%|％|年|个月|月|日|天|小时|平方米|平方公里|亩|公里|米|千米|吨|"
    r"千瓦时|千瓦|兆瓦|人|户|家|项|套|台|个|座|栋|层|次)"
)
_PROTECTED_MARKER_RE = re.compile(r"【(?:待补充|待确认)】")


def _new_task_id() -> str:
    return uuid.uuid4().hex[:12]


def _knowledge_catalog_message(report: dict) -> AIMessage | None:
    """参考知识库轻量清单消息（未选择参考文档时返回 None，不污染提示词）。"""
    items = knowledge_material.load_reference_items(report.get("referenceDocumentIds"))
    catalog = knowledge_material.format_lightweight_catalog(items)
    if not catalog:
        return None
    return AIMessage(
        role="user",
        content=f"【知识素材清单】\n{build_knowledge_catalog_note()}\n\n{catalog}",
    )


def _update_task(task_type: str, task_id: str, **fields: Any) -> None:
    store.save_task_state(task_type, {"task_id": task_id, "started_at": fields.pop("started_at", None), **fields})


def _finish_task(task_type: str, task_id: str, checkpoint: dict) -> None:
    """任务成功收尾：写任务状态 + 产物落工作区。"""
    checkpoint = dict(checkpoint)
    partial = {k: v for k, v in checkpoint.items() if k in ("analysisMarkdown", "outlineData", "keyParametersMarkdown", "step")}
    partial[task_type] = None  # 成功后清除该任务行（易标 checkpointTask 同语义）
    store.update_report_state(partial)
    if checkpoint.get("clear_tasks"):
        for t in checkpoint["clear_tasks"]:
            store.save_task_state(t, None)
    # BUG-FIX 2026-09-18（中）：只清**自己这一轮**的任务行。原实现无条件按 type
    # 删除，若期间已有同类型的新任务启动，会把新任务的状态一起抹掉（task_id 形参
    # 此前完全没被用上）。
    store.clear_task_state_if_owner(task_type, task_id)


def _fail_task(task_type: str, task_id: str, error: Exception) -> None:
    logger.warning("[可研报告] %s 任务失败: %s", task_type, error, exc_info=True)
    store.save_task_state(
        task_type,
        {"task_id": task_id, "status": "error", "progress": 100, "error": str(error)[:500]},
    )


# ===== 事实保护（润色阶段） =====


def _count_protected_tokens(content: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for match in _PROTECTED_QUANTITY_RE.findall(content or ""):
        token = re.sub(r"\s+", " ", match).strip()
        if token:
            counts[token] = counts.get(token, 0) + 1
    for match in _PROTECTED_MARKER_RE.findall(content or ""):
        counts[match] = counts.get(match, 0) + 1
    return counts


def protect_facts(original: str, rewritten: str) -> str:
    """润色结果若丢失任何受保护 token（数量/金额/日期/【待补充】标记），保留原文。"""
    rewritten = (rewritten or "").strip()
    if not rewritten:
        return original or ""
    original_counts = _count_protected_tokens(original or "")
    if not original_counts:
        return rewritten
    revised_counts = _count_protected_tokens(rewritten)
    missing = [
        token
        for token, count in original_counts.items()
        if revised_counts.get(token, 0) < count
    ]
    if missing:
        logger.info("[可研报告] 润色结果丢失受保护事实 %s，保留原文", missing[:5])
        return original or ""
    return rewritten


# ===== 任务 1：资料分析 =====


def _split_segments(text: str, limit: int = _ANALYSIS_SEGMENT_CHARS) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    return [text[i : i + limit] for i in range(0, len(text), limit)]


async def run_analysis_task(provider: Any, cancel_event: asyncio.Event | None = None) -> None:
    task_type = "feasibility-analysis"
    task_id = _new_task_id()
    try:
        report = store.load_report()
        if not report["projectInfo"].get("projectName"):
            raise ValueError("请先填写项目名称")
        sources = store.read_combined_source_markdown()
        project_block = format_project_info(report["projectInfo"])
        segments = _split_segments(sources)
        # ENHANCE 2026-09-15：参考知识库（可选）只提供行业背景与术语口径，
        # 明确禁止素材里的金额/规模等被当成本项目事实写入分析结论。
        reference_items = knowledge_material.load_reference_items(report.get("referenceDocumentIds"))
        catalog_block = knowledge_material.format_lightweight_catalog(reference_items)

        async def analyze_segment(content: str, index: int, total: int) -> dict:
            if cancel_event and cancel_event.is_set():
                raise asyncio.CancelledError("资料分析已取消")
            _update_task(
                task_type,
                task_id,
                status="running",
                progress=12 + round((index - 1) / max(total, 1) * 55),
            )
            source_block = content.strip() or "未导入资料文件"
            messages = [
                AIMessage(role="system", content=build_analysis_system_prompt()),
                AIMessage(role="user", content=f"项目基础参数：\n{project_block}\n\n当前资料分段：{index}/{total}\n\n{source_block}"),
            ]
            if catalog_block:
                messages.append(
                    AIMessage(
                        role="user",
                        content=f"【行业背景参考】\n{build_knowledge_background_note()}\n\n{catalog_block}",
                    )
                )
            messages.append(AIMessage(role="user", content=build_analysis_user_instruction(index, total)))
            result = await collect_json_response(provider, messages, temperature=0.3, max_retries=2)
            return result if isinstance(result, dict) else {}

        if len(segments) <= 1:
            payload = await analyze_segment(segments[0] if segments else "", 1, 1)
        else:
            parts = [await analyze_segment(seg, i + 1, len(segments)) for i, seg in enumerate(segments)]
            _update_task(task_type, task_id, status="running", progress=75)
            merge_messages = [
                AIMessage(role="system", content=build_analysis_merge_system_prompt()),
                AIMessage(
                    role="user",
                    content=(
                        f"项目基础参数：\n{project_block}\n\n分段分析结果：\n"
                        f"{json.dumps(parts, ensure_ascii=False, indent=2)}\n\n"
                        + build_analysis_merge_user_instruction()
                    ),
                ),
            ]
            merged = await collect_json_response(provider, merge_messages, temperature=0.2, max_retries=2)
            payload = merged if isinstance(merged, dict) else {}

        analysis_markdown = analysis_to_markdown(payload)
        _finish_task(
            task_type,
            task_id,
            {
                "analysisMarkdown": analysis_markdown,
                "outlineData": None,
                "keyParametersMarkdown": None,
                "step": "outline",
                "clear_tasks": [
                    "feasibility-outline",
                    "feasibility-parameters",
                    "feasibility-content",
                ],
            },
        )
        logger.info("[可研报告] 资料分析完成（%d 段）", max(len(segments), 1))
    except Exception as e:
        if isinstance(e, asyncio.CancelledError):
            store.save_task_state(task_type, None)
            return
        _fail_task(task_type, task_id, e)


# ===== 任务 2：报告目录 =====


async def run_outline_task(provider: Any, cancel_event: asyncio.Event | None = None) -> None:
    task_type = "feasibility-outline"
    task_id = _new_task_id()
    try:
        report = store.load_report()
        if not str(report.get("analysisMarkdown") or "").strip():
            raise ValueError("请先完成资料分析")
        _update_task(task_type, task_id, status="running", progress=15)

        reference_items = knowledge_material.load_reference_items(report.get("referenceDocumentIds"))
        catalog_block = knowledge_material.format_lightweight_catalog(reference_items)
        messages = [
            AIMessage(role="system", content=build_outline_system_prompt()),
            AIMessage(role="user", content=f"项目基础参数：\n{format_project_info(report['projectInfo'])}"),
            AIMessage(role="user", content=f"项目资料分析：\n{report['analysisMarkdown']}"),
        ]
        if catalog_block:
            messages.append(
                AIMessage(
                    role="user",
                    content=f"【知识素材清单】\n{build_knowledge_catalog_note()}\n\n{catalog_block}",
                )
            )
        messages.append(
            AIMessage(
                role="user",
                content=build_outline_user_instruction(
                    report["outlineTemplate"], report["targetWords"], has_knowledge=bool(catalog_block)
                ),
            )
        )
        _update_task(task_type, task_id, status="running", progress=40)
        raw = await with_timeout(
            provider.chat(messages, temperature=0.4, cancel_event=cancel_event),
            timeout=_CHAT_TIMEOUT,
        )
        parsed = extract_json_from_text(raw)
        outline_items = parsed.get("outline") if isinstance(parsed, dict) else parsed
        # AI 推荐的素材编号必须真实存在（防幻觉 id 落库）；未选参考文档时一律不接受
        allowed_ids = [item["id"] for item in reference_items]
        normalized = store.normalize_outline_tree(outline_items, allowed_knowledge_ids=allowed_ids)
        if not normalized:
            raise ValueError("模型未返回可用目录，请重试")
        outline = store.assign_outline_ids(normalized)
        logger.info("[可研报告] 目录生成完成：%d 个一级章节", len(outline))

        _finish_task(
            task_type,
            task_id,
            {
                "outlineData": {
                    "outline": outline,
                    "project_name": report["projectInfo"].get("projectName"),
                    "project_overview": str(report.get("analysisMarkdown") or "")[:400],
                },
                "keyParametersMarkdown": None,
                "step": "parameters",
                "clear_tasks": ["feasibility-parameters", "feasibility-content"],
            },
        )
    except Exception as e:
        if isinstance(e, asyncio.CancelledError):
            store.save_task_state(task_type, None)
            return
        _fail_task(task_type, task_id, e)


# ===== 任务 3：关键参数 =====


async def run_parameters_task(provider: Any, cancel_event: asyncio.Event | None = None) -> None:
    task_type = "feasibility-parameters"
    task_id = _new_task_id()
    try:
        report = store.load_report()
        outline_data = report.get("outlineData")
        if not outline_data or not outline_data.get("outline"):
            raise ValueError("请先生成报告目录")
        _update_task(task_type, task_id, status="running", progress=20)

        messages = [
            AIMessage(role="system", content=build_parameters_system_prompt()),
            AIMessage(
                role="user",
                content=(
                    f"项目基础参数：\n{format_project_info(report['projectInfo'])}\n\n"
                    f"项目资料分析：\n{report['analysisMarkdown']}"
                ),
            ),
            AIMessage(
                role="user",
                content=f"报告目录：\n{chr(10).join(render_outline_for_prompt(outline_data['outline']))}",
            ),
        ]
        catalog_message = _knowledge_catalog_message(report)
        if catalog_message:
            messages.append(catalog_message)
        messages.append(AIMessage(role="user", content=build_parameters_user_instruction()))
        raw = await with_timeout(
            provider.chat(messages, temperature=0.3, cancel_event=cancel_event),
            timeout=_CHAT_TIMEOUT,
        )
        markdown = store.strip_markdown_fence(raw)
        if not markdown:
            raise ValueError("关键参数生成结果为空，请重试")

        # 参数口径变更后旧正文全部失效（清空各叶 content）
        cleared = store.clear_tree_content(outline_data["outline"])
        logger.info("[可研报告] 关键参数生成完成，已清空旧正文")
        _finish_task(
            task_type,
            task_id,
            {
                "keyParametersMarkdown": markdown,
                "outlineData": {**outline_data, "outline": cleared},
                "step": "content",
            },
        )
    except Exception as e:
        if isinstance(e, asyncio.CancelledError):
            store.save_task_state(task_type, None)
            return
        _fail_task(task_type, task_id, e)


# ===== 任务 4：正文生成（撰写 + 自然化审校） =====


def _has_content(item: dict) -> bool:
    return bool(str(item.get("content") or "").strip())


async def _generate_leaf_content(
    provider: Any,
    report: dict,
    leaf: dict,
    target_words: int,
    cancel_event: asyncio.Event | None,
    knowledge_text: str = "",
) -> str:
    chapter_path = " > ".join(leaf.get("trail") or [leaf.get("title", "")])
    messages = [
        AIMessage(role="system", content=build_content_system_prompt()),
        AIMessage(role="user", content=f"项目基础参数：\n{format_project_info(report['projectInfo'])}"),
        AIMessage(role="user", content=f"项目资料分析：\n{report['analysisMarkdown']}"),
        AIMessage(role="user", content=f"全文关键参数与编制口径：\n{report['keyParametersMarkdown']}"),
        AIMessage(
            role="user",
            content=(
                f"当前章节路径：{chapter_path}\n"
                f"章节写作重点：{leaf.get('description') or '围绕章节标题展开充分论证。'}\n"
                f"参考目标字数：约 {target_words} 字。"
            ),
        ),
    ]
    if knowledge_text:
        messages.append(
            AIMessage(
                role="user",
                content=f"{build_knowledge_material_note()}\n\n{knowledge_text}",
            )
        )
    messages.append(AIMessage(role="user", content=build_content_writing_rules()))
    raw = await with_timeout(
        provider.chat(messages, temperature=0.6, cancel_event=cancel_event),
        timeout=_CHAT_TIMEOUT,
    )
    return store.strip_markdown_fence(raw)


async def _polish_leaf_content(
    provider: Any,
    report: dict,
    leaf: dict,
    cancel_event: asyncio.Event | None,
) -> str:
    chapter_path = " > ".join(leaf.get("trail") or [leaf.get("title", "")])
    messages = [
        AIMessage(role="system", content=build_human_writing_system_prompt()),
        AIMessage(role="user", content=f"项目基础参数：\n{format_project_info(report['projectInfo'])}"),
        AIMessage(role="user", content=f"全文关键参数与编制口径：\n{report['keyParametersMarkdown']}"),
        AIMessage(
            role="user",
            content=(
                f"当前章节路径：{chapter_path}\n\n"
                "请只审校下面的已有正文。不得输出章节标题，不得补写资料中不存在的事实。\n\n"
                f"{leaf.get('content') or ''}"
            ),
        ),
    ]
    raw = await with_timeout(
        provider.chat(messages, temperature=0.4, cancel_event=cancel_event),
        timeout=_CHAT_TIMEOUT,
    )
    return store.strip_markdown_fence(raw)


async def run_content_task(
    provider: Any,
    *,
    only_missing: bool = False,
    skip_polish: bool = False,
    cancel_event: asyncio.Event | None = None,
) -> None:
    """正文生成两阶段：逐叶撰写（5%-70%）→ 自然化审校（70%-99%）。

    每完成一叶立即 checkpoint（写节点 content + 任务进度），中断后可用
    `only_missing=True` 续跑未完成章节。
    """
    task_type = "feasibility-content"
    task_id = _new_task_id()
    try:
        report = store.load_report()
        outline_data = report.get("outlineData")
        if not outline_data or not outline_data.get("outline"):
            raise ValueError("请先生成报告目录")
        if not str(report.get("keyParametersMarkdown") or "").strip():
            raise ValueError("请先生成关键参数")

        outline = outline_data["outline"]
        all_leaves = store.collect_leaves(outline)
        targets = [leaf for leaf in all_leaves if (only_missing and not _has_content(leaf)) or not only_missing]
        per_leaf_words = max(600, round(int(report["targetWords"] or 30000) / max(len(all_leaves), 1)))
        # ENHANCE 2026-09-15：参考知识库素材（章节精确指定优先，否则按相关度自动匹配）
        reference_items = knowledge_material.load_reference_items(report.get("referenceDocumentIds"))
        _update_task(task_type, task_id, status="running", progress=5, stats={"phase": "generating"})

        # 阶段一：逐叶撰写
        generated = 0
        for index, leaf in enumerate(targets):
            if cancel_event and cancel_event.is_set():
                store.save_task_state(task_type, None)
                return
            _update_task(
                task_type,
                task_id,
                status="running",
                progress=5 + round(index / max(len(targets), 1) * 65),
                stats={"phase": "generating", "current": leaf.get("title", "")},
            )
            material = knowledge_material.chapter_material_text(leaf, reference_items)
            content = await _generate_leaf_content(
                provider, report, leaf, per_leaf_words, cancel_event, knowledge_text=material
            )
            if content:
                store.write_chapter_content(leaf["id"], content)
                generated += 1

        if skip_polish:
            _finish_task(task_type, task_id, {"step": "content"})
            logger.info("[可研报告] 正文生成完成（%d 叶，跳过审校）", generated)
            return

        # 阶段二：自然化审校（带事实保护）
        _update_task(task_type, task_id, status="running", progress=70, stats={"phase": "human-writing"})
        leaves_now = store.collect_leaves(store.load_report()["outlineData"]["outline"])
        polish_targets = [leaf for leaf in leaves_now if _has_content(leaf)]
        polished = 0
        for index, leaf in enumerate(polish_targets):
            if cancel_event and cancel_event.is_set():
                store.save_task_state(task_type, None)
                return
            _update_task(
                task_type,
                task_id,
                status="running",
                progress=70 + round(index / max(len(polish_targets), 1) * 29),
                stats={"phase": "human-writing", "current": leaf.get("title", "")},
            )
            polished_text = await _polish_leaf_content(provider, report, leaf, cancel_event)
            final_text = protect_facts(leaf.get("content") or "", polished_text)
            store.write_chapter_content(leaf["id"], final_text)
            polished += 1

        _finish_task(task_type, task_id, {"step": "content"})
        logger.info("[可研报告] 正文任务完成：撰写 %d 叶，审校 %d 叶", generated, polished)
    except Exception as e:
        if isinstance(e, asyncio.CancelledError):
            store.save_task_state(task_type, None)
            return
        _fail_task(task_type, task_id, e)
