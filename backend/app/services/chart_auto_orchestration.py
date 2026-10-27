"""图表自动编排闭环服务（2026-10-17，需求 1/3/4 端到端接线）

把 ``chart_orchestrator`` 的「分析 → 载荷生成 → 插入位置建议」接成端到端闭环：

    分析(analyze_chart_needs) → 生成载荷(generate_chart_payload)
      → 统一闸门校验修复(validate_chart_for_ai_pipeline，内含 repair_mermaid)
      → 围栏代码块构建（与正文 Markdown 完全一致的 ```mermaid / ```chart-json）
      → 最佳位置插入正文（suggest_insertion_position）
      → 落库（update_section）

设计原则：
  * 图表全部自动生成，不设人工配图/生图入口（人工逐类生成端点保留但不再是主链路）。
  * 正文已有图表围栏时**跳过插入**（去重防重入），避免同一章节出现双图/图号虚跳。
  * 校验失败不写坏块：先走 repair_mermaid 修复，仍失败则整体跳过（容错降级，
    不占图号——导出侧对不存在的围栏天然无感）。
  * 正文为空无法插入时，降级把载荷持久化到章节 chart 字段（*_json），
    导出侧按 chart 字段路径照常渲染。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..models import SectionUpdate
from .ai.chart_orchestrator import (
    analyze_chart_needs,
    generate_chart_payload,
    suggest_insertion_position,
)
from .sections import get_section, update_section

logger = logging.getLogger(__name__)

#: 图表类型 → 章节 chart 字段（与 routers/sse/sse_utils._resolve_chart_field 同口径；
#: 服务层不能反向 import 路由层，这里维护唯一服务层正本，路由侧后续收敛到此）
CHART_FIELD_MAP: dict[str, str] = {
    "flowchart": "flowchart_json",
    "gantt": "gantt_json",
    "architecture": "architecture_json",
    "labor": "labor_json",
    "comparison": "comparison_json",
    "layout": "layout_json",
    "timeline": "timeline_json",
}

#: 图表类型通用名（图题第 4 优先级兜底，与 _CHART_TYPE_MAP 中文名一致）
_CHART_TYPE_LABELS: dict[str, str] = {
    "flowchart": "流程图",
    "gantt": "甘特图",
    "architecture": "架构图",
    "labor": "劳动力配置图",
    "comparison": "对比图",
    "layout": "平面布置图",
    "timeline": "里程碑时间线",
}

# Mermaid 代码型图表集合（围栏语言判定：mermaid vs chart-json）——已废弃：
# 围栏语言最终按**载荷形态**（是否含 code 字段）判定，见 build_chart_fence。

#: 本地模板数据披露括注（A3 建议 1·显式披露）。挂在 source="local" 载荷的
#: Mermaid 引导语上，前端打「模板数据」标签 + DOCX 正文双重可见；
#: 导出图题链路 _clean_lead_in_title 会剥离该括注，保证图题干净。
_LOCAL_TEMPLATE_DISCLAIMER = "（模板示例数据，请按项目实际核定）"


# ============================================================
# 纯函数：围栏构建 / 去重检测 / 位置插入（可独立单元测试）
# ============================================================


def build_chart_fence(chart_type: str, payload: dict[str, Any]) -> tuple[str, str]:
    """把图表载荷转换为与正文 Markdown 完全一致的围栏代码块。

    围栏语言按**载荷形态**判定（与 SSE 生成侧口径一致）：
      - 载荷带非空 ``code`` 字段 → Mermaid 源码 → `` ```mermaid `` 围栏；
      - 其余（tree/phases/rows/zones/milestones 等结构化数据）→
        `` ```chart-json `` 围栏。
    同一 chart_type 两种形态都可能出现（本地模板产 JSON、AI 产 Mermaid），
    按类型硬判会在混源时写错围栏语言。

    Returns:
        (lead_in, fence)：
        - lead_in: 引导语段落（图题第 3 优先级来源）。mermaid 围栏载荷本身
          不携带 title，靠引导语把图名带进导出图题链路；chart-json 载荷
          内嵌 title 字段（图题第 1 优先级），引导语返回空串避免冗余。
        - fence: ```` ```mermaid ```` / ```` ```chart-json ```` 围栏代码块文本。
    """
    title = str(payload.get("title") or "").strip()
    code = str(payload.get("code") or "").strip()
    if code:
        fence = "```mermaid\n" + code + "\n```"
        base = title or _CHART_TYPE_LABELS.get((chart_type or "").lower(), "图表")
        # A3 建议 1（显式披露）：本地模板载荷的 Mermaid 图无内嵌 title，
        # 披露括注挂引导语（DOCX 正文可见）；图题链路经 _clean_lead_in_title
        # 剥离括注，导出图题保持干净。AI 载荷与 chart-json 型不加括注——
        # chart-json 的披露责任在前端「模板数据」标签（source 键随 JSON
        # 序列化进围栏，前端可读），塞进 title 会污染导出图题。
        if str(payload.get("source") or "") == "local":
            base += _LOCAL_TEMPLATE_DISCLAIMER
        lead_in = f"下图为{base}："
        return lead_in, fence
    # 结构化 JSON 载荷：title 内嵌于载荷（图题第 1 优先级）
    fence = "```chart-json\n" + json.dumps(payload, ensure_ascii=False) + "\n```"
    return "", fence


def content_has_chart_fence(content: str) -> bool:
    """正文是否已包含图表围栏（去重防重入）。

    与 content_cleaning.CHART_FENCE_OPEN_PATTERN 同口径（```
    / ~~~ + mermaid / chart[-_]?json 变体），只判存在性，不解析。
    """
    if not content:
        return False
    from .content_cleaning import CHART_FENCE_OPEN_PATTERN

    for line in content.split("\n"):
        if CHART_FENCE_OPEN_PATTERN.match(line.strip()):
            return True
    return False


def insert_fence_at_paragraph(
    content: str,
    fence: str,
    lead_in: str,
    paragraph_index: int,
) -> str:
    """把围栏块插入到正文的第 ``paragraph_index`` 个非空段之后。

    位置来源为 suggest_insertion_position（关键词打分），保证「图文逻辑连贯」：
    流程图插在工艺流程段后、甘特图插在进度计划段后。空正文时直接以
    「引导语 + 围栏」作为正文主体。
    """
    lines = content.split("\n")
    block = (lead_in + "\n\n" + fence) if lead_in else fence
    block_lines = block.split("\n")
    non_empty = [k for k, ln in enumerate(lines) if ln.strip()]
    if not non_empty:
        return block
    idx = max(0, min(paragraph_index, len(non_empty) - 1))
    line_no = non_empty[idx]
    new_lines = lines[: line_no + 1] + ["", *block_lines] + lines[line_no + 1 :]
    return "\n".join(new_lines)


# ============================================================
# 闭环编排（含 DB 读写；DB 依赖通过模块级名字注入，便于单测 monkeypatch）
# ============================================================


def auto_orchestrate_section(
    project_id: int,
    section_id: int,
    *,
    insert_into_content: bool = True,
    ai_client: Any = None,
    _get_section=None,
    _update_section=None,
) -> dict[str, Any]:
    """对单个章节执行自动图表编排闭环。

    Args:
        project_id: 项目 ID（用于日志与权限上下文）
        section_id: 章节 ID
        insert_into_content: True 时把图表围栏写入正文最佳位置（默认）；
            False 或正文不可写时降级持久化 chart 字段。
        ai_client: 可选 AI 客户端（None 走本地推断/模板生成，快速路径）
        _get_section/_update_section: 测试注入点（默认用 services.sections 实现）

    Returns:
        结果字典（永远可 JSON 序列化，含 analysis/inserted/skipped_reason 等）
    """
    get_sec = _get_section or get_section
    upd_sec = _update_section or update_section

    result: dict[str, Any] = {
        "ok": True,
        "section_id": section_id,
        "analysis": None,
        "inserted": False,
        "persisted_chart_field": "",
        "skipped_reason": "",
        "validation_errors": [],
        "insert_paragraph_index": -1,
        "payload": None,
        "fence": "",
    }

    section = get_sec(section_id)
    if not section:
        result["ok"] = False
        result["skipped_reason"] = "section_not_found"
        return result

    title = section.get("title", "") or ""
    content = section.get("content", "") or ""

    # ① 分析：AI/本地判断是否需要配图与图表类型
    analysis = analyze_chart_needs(
        section_title=title, content=content, ai_client=ai_client
    )
    analysis["insert_paragraph_index"] = (
        suggest_insertion_position(content, analysis.get("chart_type", "none"))
        if analysis.get("needed")
        else -1
    )
    result["analysis"] = analysis
    result["insert_paragraph_index"] = analysis["insert_paragraph_index"]

    if not analysis.get("needed"):
        result["skipped_reason"] = "not_needed"
        return result

    chart_type = analysis.get("chart_type", "none")
    title_hint = analysis.get("title_hint", "") or title

    # ② 生成载荷（本地模板快速路径，可选 AI 增强）
    payload = generate_chart_payload(
        section_title=title,
        content=content,
        chart_type=chart_type,
        title_hint=title_hint,
        ai_client=ai_client,
    )
    if not payload:
        result["skipped_reason"] = "payload_generation_failed"
        logger.info(
            "[自动编排] 载荷生成失败，跳过 project=%d section=%d type=%s",
            project_id, section_id, chart_type,
        )
        return result

    # ③ 统一闸门校验 + 修复（repair_mermaid 内聚于此）
    from .ai.image_engine import validate_chart_for_ai_pipeline

    try:
        ok, errors, fixed_payload, _mutated, _ctx = validate_chart_for_ai_pipeline(
            chart_type, payload, verbose=True, section_title=title
        )
    except Exception as e:  # 校验器异常不得炸穿编排
        ok, errors, fixed_payload = False, [f"validator_exception: {e}"], payload
    if not ok:
        # 容错降级：坏块不写入正文（不占图号，导出侧无感）
        result["skipped_reason"] = "validation_failed"
        result["validation_errors"] = list(errors)[:5]
        logger.info(
            "[自动编排] 校验未通过，跳过 project=%d section=%d type=%s errors=%s",
            project_id, section_id, chart_type, result["validation_errors"],
        )
        return result

    # ④ 插入正文（去重防重入）或降级持久化 chart 字段
    result["payload"] = fixed_payload
    if insert_into_content and content and not content_has_chart_fence(content):
        lead_in, fence = build_chart_fence(chart_type, fixed_payload)
        # RACE-GUARD 2026-10-27（需求 11·并发竞态）：自「读取正文」到「写回」
        # 之间存在窗口，其他编排请求/生成任务可能已写入围栏或改写正文。
        # 插入前重读最新正文：已含围栏 → 跳过（防同章双图/图号虚跳）；
        # 正文已变 → 以最新正文重算插入位置，保证图文连贯。
        try:
            _latest = get_sec(section_id) or {}
            _latest_content = _latest.get("content", "") or ""
        except Exception:
            _latest_content = content
        if _latest_content and content_has_chart_fence(_latest_content):
            result["skipped_reason"] = "chart_fence_exists"
            return result
        if _latest_content != content:
            content = _latest_content
            analysis["insert_paragraph_index"] = suggest_insertion_position(
                content, chart_type
            )
            result["insert_paragraph_index"] = analysis["insert_paragraph_index"]
        new_content = insert_fence_at_paragraph(
            content, fence, lead_in, analysis["insert_paragraph_index"]
        )
        upd_sec(section_id, SectionUpdate(content=new_content))
        result["inserted"] = True
        result["fence"] = fence
        logger.info(
            "[自动编排] 已插入图表围栏 project=%d section=%d type=%s 段落=%d",
            project_id, section_id, chart_type, analysis["insert_paragraph_index"],
        )
        return result

    if content and content_has_chart_fence(content):
        result["skipped_reason"] = "chart_fence_exists"
        return result

    # 正文为空：降级把载荷写入章节 chart 字段（导出按 chart 字段路径渲染）
    field = CHART_FIELD_MAP.get(chart_type)
    if field:
        upd_sec(
            section_id,
            SectionUpdate(**{field: json.dumps(fixed_payload, ensure_ascii=False)}),
        )
        result["persisted_chart_field"] = field
        result["skipped_reason"] = "content_empty_fallback_chart_field"
        logger.info(
            "[自动编排] 正文为空，已持久化 chart 字段 project=%d section=%d field=%s",
            project_id, section_id, field,
        )
    else:
        result["skipped_reason"] = "unsupported_chart_type"

    return result
