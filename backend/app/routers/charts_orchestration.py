"""图表智能编排 API 路由（P2-集成）

提供前端调用接口：
  POST /api/v1/charts/orchestrate - 智能分析章节图表需求
  POST /api/v1/charts/generate-payloads - 批量生成图表载荷
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..services.ai.chart_orchestrator import (
    analyze_chart_needs,
    batch_analyze_sections,
    generate_all_chart_payloads,
    generate_chart_payload,
)
from ..services.chart_auto_orchestration import auto_orchestrate_section

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/charts", tags=["charts-orchestration"])


# ============================================================
# 请求/响应模型
# ============================================================


class SectionInput(BaseModel):
    """章节输入"""
    id: str = Field(..., description="章节 ID")
    title: str = Field(..., description="章节标题")
    content: str = Field("", description="章节正文")


class OrchestrateRequest(BaseModel):
    """智能编排请求"""
    sections: list[SectionInput] = Field(..., description="章节列表")
    project_summary: str = Field("", description="项目概述")
    use_ai: bool = Field(False, description="是否启用 AI 增强（默认本地推断）")


class OrchestrateResponse(BaseModel):
    """智能编排响应"""
    results: list[dict[str, Any]] = Field(..., description="分析结果列表")
    needed_count: int = Field(0, description="需要图表的章节数")
    total_count: int = Field(0, description="总章节数")


class PayloadRequest(BaseModel):
    """载荷生成请求"""
    sections: list[dict[str, Any]] = Field(..., description="章节分析结果列表")
    use_ai: bool = Field(False, description="是否启用 AI 生成")


class PayloadResponse(BaseModel):
    """载荷生成响应"""
    payloads: list[dict[str, Any]] = Field(..., description="图表载荷列表")
    generated_count: int = Field(0, description="生成数量")


# ============================================================
# API 端点
# ============================================================


@router.post("/orchestrate", response_model=OrchestrateResponse)
async def orchestrate_charts(request: OrchestrateRequest):
    """智能分析章节图表需求（P2 前端智能编排）

    根据章节标题和内容，AI 智能判断是否需要图表，并推断图表类型。
    """
    if not request.sections:
        raise HTTPException(status_code=400, detail="sections 不能为空")

    sections_data = [
        {"id": s.id, "title": s.title, "content": s.content}
        for s in request.sections
    ]

    # 获取 AI 客户端（可选）
    ai_client = None
    if request.use_ai:
        try:
            from ..services.ai.provider_factory import get_ai_config
            ai_client = get_ai_config()
        except Exception as e:
            logger.warning("AI 客户端不可用，回退本地推断: %s", e)

    # 执行智能编排
    results = batch_analyze_sections(
        sections=sections_data,
        project_summary=request.project_summary,
        ai_client=ai_client,
    )

    needed_count = sum(1 for r in results if r.get("needed"))

    return OrchestrateResponse(
        results=results,
        needed_count=needed_count,
        total_count=len(results),
    )


@router.post("/generate-payloads", response_model=PayloadResponse)
async def generate_chart_payloads_api(request: PayloadRequest):
    """批量生成图表载荷（P2 前端智能编排）

    为所有需要图表的章节生成 Mermaid/JSON 载荷。
    """
    if not request.sections:
        raise HTTPException(status_code=400, detail="sections 不能为空")

    # 获取 AI 客户端（可选）
    ai_client = None
    if request.use_ai:
        try:
            from ..services.ai.provider_factory import get_ai_config
            ai_client = get_ai_config()
        except Exception as e:
            logger.warning("AI 客户端不可用，回退本地生成: %s", e)

    # 执行批量生成
    payloads = generate_all_chart_payloads(
        sections=request.sections,
        ai_client=ai_client,
    )

    return PayloadResponse(
        payloads=payloads,
        generated_count=len(payloads),
    )


@router.post("/generate-payload")
async def generate_single_payload(
    section_title: str,
    content: str = "",
    chart_type: str = "architecture",
    title_hint: str = "",
    use_ai: bool = False,
):
    """单个图表载荷生成（P2 前端智能编排）

    为单个章节生成指定类型的图表载荷。
    """
    # 获取 AI 客户端（可选）
    ai_client = None
    if use_ai:
        try:
            from ..services.ai.provider_factory import get_ai_config
            ai_client = get_ai_config()
        except Exception as e:
            logger.warning("AI 客户端不可用，回退本地生成: %s", e)

    payload = generate_chart_payload(
        section_title=section_title,
        content=content,
        chart_type=chart_type,
        title_hint=title_hint,
        ai_client=ai_client,
    )

    if payload is None:
        raise HTTPException(status_code=400, detail=f"无法生成 {chart_type} 类型图表载荷")

    return {"payload": payload}


# ============================================================
# 自动编排闭环（需求 1/3/4：全部自动生成，无人工配图入口）
# ============================================================


class AutoOrchestrateSectionRequest(BaseModel):
    """单章节自动编排请求"""
    project_id: int = Field(..., description="项目 ID")
    section_id: int = Field(..., description="章节 ID")
    insert_into_content: bool = Field(
        True,
        description="是否把图表围栏写入正文最佳位置（默认 True；正文为空时自动降级持久化 chart 字段）",
    )
    use_ai: bool = Field(False, description="是否启用 AI 增强（默认本地推断/模板生成）")


@router.post("/auto-orchestrate-section")
async def auto_orchestrate_section_api(request: AutoOrchestrateSectionRequest):
    """单章节图表自动编排闭环（2026-10-17，需求 1/3/4 端到端接线）。

    流程：分析是否需要配图 → 生成 Mermaid/JSON 载荷 → 统一闸门校验修复
    （repair_mermaid 内聚）→ 按建议段落位置把 ```mermaid / ```chart-json
    围栏写入正文 → 落库。正文已有图表围栏时去重跳过；校验失败时容错跳过
    （不占图号）；正文为空时降级持久化章节 chart 字段。
    """
    ai_client = None
    if request.use_ai:
        try:
            from ..services.ai.provider_factory import get_ai_config
            ai_client = get_ai_config()
        except Exception as e:
            logger.warning("AI 客户端不可用，回退本地编排: %s", e)

    result = auto_orchestrate_section(
        project_id=request.project_id,
        section_id=request.section_id,
        insert_into_content=request.insert_into_content,
        ai_client=ai_client,
    )

    if result.get("skipped_reason") == "section_not_found":
        raise HTTPException(status_code=404, detail=f"章节 {request.section_id} 不存在")

    return result