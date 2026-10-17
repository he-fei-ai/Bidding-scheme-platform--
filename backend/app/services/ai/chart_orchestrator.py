"""图表智能编排服务（P2 前端智能编排）

职责：
  1. AI 智能判断章节是否需要图表
  2. AI 生成 Mermaid/JSON 载荷
  3. AI 推荐图表插入位置
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from ..export_chart_pipeline import (
    _CHART_TYPE_MAP,
    _CHART_FALLBACK_KEYWORDS,
    _infer_chart_type_from_text,
)

logger = logging.getLogger(__name__)


# ============================================================
# 核心编排函数
# ============================================================


def analyze_chart_needs(
    section_title: str,
    content: str,
    project_summary: str = "",
    ai_client: Any = None,
) -> dict[str, Any]:
    """AI 智能判断章节是否需要图表。"""
    result: dict[str, Any] = {
        "needed": False,
        "chart_type": "none",
        "confidence": 0.0,
        "reason": "",
        "title_hint": "",
        "insert_after": "",
        "source": "local",
    }

    if not section_title and not content:
        return result

    # 本地推断（快速路径）
    local_result = _local_chart_inference(section_title, content)
    result.update(local_result)

    # AI 增强（可选，失败时回退本地）
    if ai_client is not None:
        try:
            ai_result = _ai_chart_analysis(
                section_title, content, project_summary, ai_client
            )
            if ai_result.get("needed") and ai_result.get("confidence", 0) > 0.7:
                result.update(ai_result)
                result["source"] = "ai"
        except Exception as e:
            logger.debug("AI 图表分析失败，回退本地推断: %s", e)

    return result


def generate_chart_payload(
    section_title: str,
    content: str,
    chart_type: str,
    title_hint: str = "",
    ai_client: Any = None,
) -> dict | None:
    """AI 智能生成图表载荷。"""
    if not chart_type or chart_type == "none":
        return None

    # 本地生成（快速路径）
    local_payload = _local_payload_generation(section_title, content, chart_type, title_hint)
    if local_payload:
        return local_payload

    # AI 生成（可选）
    if ai_client is not None:
        try:
            ai_payload = _ai_payload_generation(
                section_title, content, chart_type, title_hint, ai_client
            )
            if ai_payload:
                return ai_payload
        except Exception as e:
            logger.debug("AI 载荷生成失败，回退本地生成: %s", e)

    return local_payload


def suggest_insertion_position(
    content: str,
    chart_type: str,
    section_title: str = "",
) -> int:
    """AI 推荐图表插入位置。"""
    if not content:
        return -1

    paragraphs = [p.strip() for p in content.split("\n") if p.strip()]
    if not paragraphs:
        return -1

    keywords = _CHART_FALLBACK_KEYWORDS.get(chart_type, [])
    best_index = -1
    best_score = 0

    for i, para in enumerate(paragraphs):
        score = sum(1 for kw in keywords if kw in para)
        if score > best_score:
            best_score = score
            best_index = i

    if best_index < 0:
        best_index = min(1, len(paragraphs) - 1)

    return best_index


# ============================================================
# 本地推断与生成
# ============================================================


def _local_chart_inference(section_title: str, content: str) -> dict[str, Any]:
    """本地图表需求推断（无 AI 依赖，基于关键词匹配）。"""
    combined = f"{section_title} {content}"[:500]
    chart_type = _infer_chart_type_from_text(combined)

    keywords = _CHART_FALLBACK_KEYWORDS.get(chart_type, [])
    score = sum(1 for kw in keywords if kw in combined)
    confidence = min(0.9, 0.5 + score * 0.1) if score > 0 else 0.0

    needed = score >= 2

    return {
        "needed": needed,
        "chart_type": chart_type if needed else "none",
        "confidence": confidence,
        "reason": f"关键词命中 {score} 个" if needed else "关键词不足",
        "title_hint": section_title[:20] if section_title else "",
        "insert_after": _paragraphs_to_hint(content, chart_type),
        "source": "local",
    }


def _local_payload_generation(
    section_title: str, content: str, chart_type: str, title_hint: str = ""
) -> dict | None:
    """本地图表载荷生成（基于模板+关键词提取）。"""
    title = title_hint or section_title or _CHART_TYPE_MAP.get(chart_type, ("", "图表"))[1]

    # 结构化 JSON 载荷（优先本地生成）
    if chart_type == "architecture":
        return _generate_local_architecture(title)
    elif chart_type == "labor":
        return _generate_local_labor(title)
    elif chart_type == "comparison":
        return _generate_local_comparison(title)
    elif chart_type == "layout":
        return _generate_local_layout(title)
    elif chart_type == "timeline":
        return _generate_local_timeline(title)

    # Mermaid 类型（flowchart/gantt）走本地模板
    from ..export_chart_pipeline import _generate_local_mermaid_chart
    mermaid = _generate_local_mermaid_chart(chart_type, title, content[:200])
    if mermaid:
        return mermaid

    return None


def _generate_local_architecture(title: str) -> dict:
    """本地生成组织架构树。"""
    return {
        "type": "architecture",
        "title": title,
        "tree": {
            "label": "项目组织",
            "children": [
                {"label": "项目经理部"},
                {"label": "技术负责人"},
                {"label": "安全员"},
                {"label": "质检员"},
            ],
        },
    }


def _generate_local_labor(title: str) -> dict:
    """本地生成劳动力配置矩阵。"""
    return {
        "type": "labor",
        "title": title,
        "phases": ["准备", "施工", "验收"],
        "categories": ["管理", "技术", "劳务"],
        "data": [
            [3, 2, 5],
            [5, 10, 30],
            [2, 3, 5],
        ],
    }


def _generate_local_comparison(title: str) -> dict:
    """本地生成对比图数据。"""
    return {
        "type": "comparison",
        "title": title,
        "rows": [
            {"label": "方案一", "value": 40},
            {"label": "方案二", "value": 35},
            {"label": "方案三", "value": 25},
        ],
    }


def _generate_local_layout(title: str) -> dict:
    """本地生成总平面布置图。"""
    return {
        "type": "layout",
        "title": title,
        "zones": [
            {"name": "办公区", "x": 0, "y": 0, "w": 30, "h": 40},
            {"name": "生产区", "x": 30, "y": 0, "w": 50, "h": 60},
            {"name": "材料堆场", "x": 0, "y": 40, "w": 30, "h": 60},
        ],
    }


def _generate_local_timeline(title: str) -> dict:
    """本地生成时间线。"""
    return {
        "type": "timeline",
        "title": title,
        "milestones": [
            {"date": "2025-01-01", "title": "开工"},
            {"date": "2025-03-01", "title": "基础完成"},
            {"date": "2025-06-01", "title": "主体封顶"},
            {"date": "2025-09-01", "title": "竣工验收"},
        ],
    }


def _paragraphs_to_hint(content: str, chart_type: str) -> str:
    """提取建议插入位置的段落提示。"""
    if not content:
        return ""
    paragraphs = [p.strip() for p in content.split("\n") if p.strip()]
    if not paragraphs:
        return ""
    keywords = _CHART_FALLBACK_KEYWORDS.get(chart_type, [])
    for para in paragraphs:
        if any(kw in para for kw in keywords):
            return para[:40]
    return paragraphs[0][:40]


# ============================================================
# AI 增强函数
# ============================================================


def _ai_chart_analysis(
    section_title: str, content: str, project_summary: str, ai_client: Any
) -> dict[str, Any]:
    """AI 图表需求分析（调用 LLM）。"""
    prompt = f"""判断以下章节是否需要图表，返回JSON:
{{"needed":true/false,"chart_type":"architecture|flowchart|gantt|comparison|layout|labor|timeline|none",
"confidence":0-1,"reason":"原因","title_hint":"标题","insert_after":"段落"}}

标题：{section_title}
内容：{content[:300]}
项目：{project_summary[:200]}"""

    response = ai_client.chat_completion(
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=500,
    )

    try:
        text = response.choices[0].message.content.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        result = json.loads(text)
        result["needed"] = bool(result.get("needed", False))
        result["confidence"] = float(result.get("confidence", 0))
        result["chart_type"] = str(result.get("chart_type", "none")).lower()
        result["reason"] = str(result.get("reason", ""))[:100]
        result["title_hint"] = str(result.get("title_hint", ""))[:30]
        result["insert_after"] = str(result.get("insert_after", ""))[:100]
        return result
    except (json.JSONDecodeError, KeyError, IndexError, ValueError) as e:
        logger.debug("AI 响应解析失败: %s", e)
        return {"needed": False, "confidence": 0.0}


def _ai_payload_generation(
    section_title: str, content: str, chart_type: str, title_hint: str, ai_client: Any
) -> dict | None:
    """AI 图表载荷生成（调用 LLM）。"""
    prompt = f"""生成{chart_type}类型图表载荷，标题：{title_hint or section_title}
内容：{content[:500]}
仅返回纯载荷（Mermaid代码或JSON），无解释文字。"""

    response = ai_client.chat_completion(
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=1000,
    )

    try:
        text = response.choices[0].message.content.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:mermaid|json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)

        if chart_type in ("flowchart", "gantt"):
            if text and len(text) > 10:
                return {"type": chart_type, "code": text, "title": title_hint or section_title}
            return None

        result = json.loads(text)
        if isinstance(result, dict) and "type" in result:
            return result
        return None
    except (json.JSONDecodeError, KeyError, IndexError, ValueError) as e:
        logger.debug("AI 载荷生成失败: %s", e)
        return None


# ============================================================
# 批量智能编排接口
# ============================================================


def batch_analyze_sections(
    sections: list[dict[str, str]],
    project_summary: str = "",
    ai_client: Any = None,
) -> list[dict[str, Any]]:
    """批量智能分析章节图表需求。"""
    results = []
    for i, section in enumerate(sections):
        title = section.get("title", "")
        content = section.get("content", "")
        section_id = section.get("id", f"section_{i}")

        result = analyze_chart_needs(
            section_title=title,
            content=content,
            project_summary=project_summary,
            ai_client=ai_client,
        )
        result["section_id"] = section_id
        results.append(result)

    return results


def generate_all_chart_payloads(
    sections: list[dict[str, Any]],
    ai_client: Any = None,
) -> list[dict[str, Any]]:
    """为所有需要图表的章节生成载荷。"""
    payloads = []
    for section in sections:
        if not section.get("needed", False):
            continue

        chart_type = section.get("chart_type", "none")
        section_id = section.get("section_id", "") or section.get("id", "")
        title_hint = section.get("title_hint", "")
        content = section.get("content", "")

        payload = generate_chart_payload(
            section_title=section.get("title", ""),
            content=content,
            chart_type=chart_type,
            title_hint=title_hint,
            ai_client=ai_client,
        )

        if payload:
            payloads.append({
                "section_id": section_id,
                "chart_type": chart_type,
                "payload": payload,
            })

    return payloads


# ============================================================
# 导出接口
# ============================================================

__all__ = [
    "analyze_chart_needs",
    "generate_chart_payload",
    "suggest_insertion_position",
    "batch_analyze_sections",
    "generate_all_chart_payloads",
    "_generate_local_architecture",
    "_generate_local_labor",
    "_generate_local_comparison",
    "_generate_local_layout",
    "_generate_local_timeline",
]