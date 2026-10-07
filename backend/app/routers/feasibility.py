# -*- coding: utf-8 -*-
"""可行性研究报告路由（ENHANCE 2026-09-15，移植自易标 feasibility-report 模块）。

单工作区六步工作流：项目资料 → 资料分析 → 报告目录 → 关键参数 → 正文生成 → 导出。
四个 AI 任务（analysis/outline/parameters/content）共用 `feasibility-report` 任务组锁
（同组互斥：并发跑两个任务会同时读写同一份大纲与参数）。

与废标/查重路由的差异：没有「多任务列表」，`GET /feasibility` 一次返回整个工作区状态
（含全部任务进度），前端轮询该端点即可。
"""

import asyncio
import functools
import logging
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import FileResponse

from ..config import is_downloadable_path
from ..models import (
    FeasibilityChapterSaveRequest,
    FeasibilityContentRunRequest,
    FeasibilityMarkdownSaveRequest,
    FeasibilityNodeKnowledgeRequest,
    FeasibilityOutlineConfigRequest,
    FeasibilityOutlineSaveRequest,
    FeasibilityReferenceDocumentsRequest,
    FeasibilitySaveProjectInfoRequest,
    FeasibilitySourceAddRequest,
)
from ..security.auth import get_current_user
from ..services import feasibility as store
from ..services import feasibility_knowledge as knowledge_material
from ..services import feasibility_tasks
from ..services import task_lock as _task_lock
from ..services.ai.provider_factory import create_provider, get_ai_config

router = APIRouter(prefix="/feasibility", tags=["feasibility"])
logger = logging.getLogger(__name__)


# ===== 任务组锁 =====


async def _acquire_group_lock(task_type: str) -> tuple[str, str]:
    """抢「可研报告」组锁。冲突时抛 HTTPException(409)。

    返回 `(lock_key, owner_id)`，由后台任务在 finally 里释放。
    """
    owner_id = _task_lock.make_owner_id(task_type, None)  # 可研是全局单工作区，锁不区分项目
    try:
        record = await asyncio.to_thread(
            functools.partial(
                _task_lock.acquire,
                task_type,
                project_id=None,
                owner_id=owner_id,
                note="feasibility-report",
            )
        )
    except _task_lock.TaskLockConflict as e:
        raise HTTPException(status_code=409, detail=f"可研报告任务正在运行中，请稍后再试：{e}") from e
    if not record.get("acquired"):
        raise HTTPException(status_code=409, detail="可研报告任务正在运行中，请稍后再试")
    return record.get("lock_key", ""), owner_id


async def _release_group_lock(lock_key: str, owner_id: str, why: str = "") -> None:
    if not lock_key:
        return
    try:
        await asyncio.to_thread(_task_lock.release, lock_key, owner_id)
    except Exception:
        logger.warning("[可研报告] 释放任务锁失败 lock_key=%s: %s", lock_key, why, exc_info=True)


async def _run_task(task_type: str, runner, lock_key: str, owner_id: str, provider) -> None:
    """后台任务外壳（协程）：捕获一切异常写回任务行，无论如何释放组锁。"""
    try:
        await runner(provider)
    except Exception as e:
        logger.error("[可研报告] 任务异常 type=%s: %s", task_type, e, exc_info=True)
        store.save_task_state(
            task_type,
            {"status": "error", "progress": 100, "error": str(e)[:500]},
        )
    finally:
        await _release_group_lock(lock_key, owner_id, why=f"任务收尾 type={task_type}")


def _make_provider():
    """创建 AI Provider；未配置时给 400 明确提示。"""
    return create_provider(get_ai_config())


# ===== 工作区状态 =====


@router.get("")
async def get_report():
    """获取可研工作区完整状态（步骤/项目信息/资料/分析/目录/参数/任务进度）。"""
    return store.load_report()


@router.get("/outline-templates")
async def get_outline_templates():
    """全部大纲模板（前端下拉选择用）。"""
    from ..services.feasibility_prompts import OUTLINE_TEMPLATES

    return {
        template_id: {"label": tpl["label"], "chapters": tpl["chapters"]}
        for template_id, tpl in OUTLINE_TEMPLATES.items()
    }


# ===== 步骤 1：项目资料 =====


@router.put("/project-info")
async def save_project_info(req: FeasibilitySaveProjectInfoRequest):
    """保存项目基础参数；默认使下游分析/目录/参数/正文全部失效。"""
    try:
        return store.save_project_info(req.project_info.model_dump(), clear_downstream=req.clear_downstream)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/sources")
async def add_source(req: FeasibilitySourceAddRequest):
    """新增一份资料文件（文本/markdown 内容直接入库）。"""
    try:
        return store.add_source(req.file_name, req.content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.delete("/sources/{source_id}")
async def remove_source(source_id: str):
    try:
        return store.remove_source(source_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.delete("")
async def clear_workspace():
    """重置可研工作区（清除全部资料/分析/目录/参数/正文/任务）。"""
    return store.clear_workspace()


# ===== 步骤 2：资料分析 =====


@router.post("/analysis/run")
async def run_analysis(bg: BackgroundTasks):
    """AI 资料分析：分段提取九板块事实 → 合并 → Markdown（后台任务）。"""
    lock_key, owner_id = await _acquire_group_lock("feasibility-analysis")
    provider = _make_provider()
    bg.add_task(
        _run_task,
        "feasibility-analysis",
        feasibility_tasks.run_analysis_task,
        lock_key,
        owner_id,
        provider,
    )
    # BUG-FIX 2026-09-18（低）：原返回里 `task_id` 是**硬编码的空字符串**。
    # 可研是全局单工作区，任务状态走 `GET /feasibility` 一次返回（无按 id 查询的
    # 端点），前端若拿这个空 task_id 去轮询/展示，只会得到"任务启动成功但查不到
    # 任何进度"。与其余三个 run 端点保持一致：只回 task_type，不再塞假字段。
    return {"status": "running", "task_type": "feasibility-analysis"}


@router.put("/analysis")
async def save_analysis(req: FeasibilityMarkdownSaveRequest):
    """人工编辑并保存资料分析（使下游目录/参数/正文失效）。"""
    return store.save_analysis(req.markdown)


# ===== 步骤 3：报告目录 =====


@router.put("/outline-config")
async def save_outline_config(req: FeasibilityOutlineConfigRequest):
    """保存大纲模板、目标字数与参考知识库文档（不触发下游失效，仅口径）。"""
    return store.save_outline_config(
        req.outline_template, req.target_words, req.reference_document_ids
    )


@router.post("/outline/run")
async def run_outline(bg: BackgroundTasks):
    """AI 生成三级目录（后台任务）。"""
    lock_key, owner_id = await _acquire_group_lock("feasibility-outline")
    provider = _make_provider()
    bg.add_task(
        _run_task,
        "feasibility-outline",
        feasibility_tasks.run_outline_task,
        lock_key,
        owner_id,
        provider,
    )
    return {"status": "running", "task_type": "feasibility-outline"}


@router.put("/outline")
async def save_outline(req: FeasibilityOutlineSaveRequest):
    """人工编辑保存目录树（树形编辑器）。

    - `id_map` / `affected_node_ids` 用于重新编号后搬运正文与精确选材；
    - 非 sort 原因会清空关键参数与正文任务；
    - 有后台任务运行时返回 409（防人工改动被 checkpoint 覆盖）。
    """
    try:
        return store.save_outline(
            req.outline_data, req.reason, id_map=req.id_map, affected_node_ids=req.affected_node_ids
        )
    except store.TaskRunningError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


# ===== 参考知识库（可研选材） =====


@router.get("/knowledge-documents")
async def list_knowledge_documents():
    """可选作参考资料的业务知识库文档（含条目数/素材字数）。"""
    return {"documents": await asyncio.to_thread(knowledge_material.list_reference_catalog)}


@router.put("/reference-documents")
async def save_reference_documents(req: FeasibilityReferenceDocumentsRequest):
    """保存参考知识库文档选择（不影响目录，仅影响后续任务素材）。"""
    return store.save_reference_documents(req.document_ids)


@router.get("/chapters/{node_id}/knowledge")
async def get_chapter_knowledge(node_id: str):
    """章节选材概览：当前生效素材 + 相关度候选（供前端勾选覆盖）。"""
    report = store.load_report()
    outline_data = report.get("outlineData") or {}
    node = knowledge_material.find_outline_node(outline_data.get("outline") or [], node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="未找到该章节")
    items = await asyncio.to_thread(
        knowledge_material.load_reference_items, report.get("referenceDocumentIds")
    )
    overview = knowledge_material.chapter_knowledge_overview(node, items)
    overview["referenceDocumentCount"] = len(report.get("referenceDocumentIds") or [])
    overview["itemPoolSize"] = len(items)
    return overview


@router.put("/chapters/{node_id}/knowledge")
async def save_chapter_knowledge(node_id: str, req: FeasibilityNodeKnowledgeRequest):
    """保存章节精确指定的知识条目（空列表 = 恢复自动匹配）。"""
    try:
        return store.save_node_knowledge_items(node_id, req.item_ids)
    except store.TaskRunningError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


# ===== 步骤 4：关键参数 =====


@router.post("/parameters/run")
async def run_parameters(bg: BackgroundTasks):
    """AI 生成关键参数与编制口径（后台任务）。"""
    lock_key, owner_id = await _acquire_group_lock("feasibility-parameters")
    provider = _make_provider()
    bg.add_task(
        _run_task,
        "feasibility-parameters",
        feasibility_tasks.run_parameters_task,
        lock_key,
        owner_id,
        provider,
    )
    return {"status": "running", "task_type": "feasibility-parameters"}


@router.put("/parameters")
async def save_parameters(req: FeasibilityMarkdownSaveRequest):
    """人工编辑保存关键参数（清空正文）。"""
    try:
        return store.save_key_parameters(req.markdown)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


# ===== 步骤 5：正文生成 =====


@router.post("/content/run")
async def run_content(req: FeasibilityContentRunRequest, bg: BackgroundTasks):
    """AI 正文生成：逐叶撰写 + 自然化审校（后台任务，支持续跑与跳过审校）。"""
    lock_key, owner_id = await _acquire_group_lock("feasibility-content")
    provider = _make_provider()
    bg.add_task(
        _run_task,
        "feasibility-content",
        lambda p: feasibility_tasks.run_content_task(
            p, only_missing=req.only_missing, skip_polish=req.skip_polish
        ),
        lock_key,
        owner_id,
        provider,
    )
    return {"status": "running", "task_type": "feasibility-content"}


@router.put("/chapters/{node_id}")
async def save_chapter(node_id: str, req: FeasibilityChapterSaveRequest):
    """人工编辑保存单章正文（后台任务运行时拒绝，防被 checkpoint 覆盖）。"""
    try:
        return store.save_chapter_content(node_id, req.content)
    except store.TaskRunningError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


# ===== 导出 =====


@router.get("/export-report")
async def export_report():
    """导出可研报告 DOCX（封面信息 + 关键参数 + 目录树正文）。"""
    try:
        file_path = await asyncio.to_thread(store.export_docx)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except ImportError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    p = Path(file_path).resolve()
    if not p.exists():
        raise HTTPException(status_code=500, detail="报告生成失败")
    if not is_downloadable_path(p):
        raise HTTPException(status_code=403, detail="非法文件路径访问")
    return FileResponse(
        path=str(p),
        filename=p.name,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
