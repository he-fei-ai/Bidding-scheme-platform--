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
    """AI 智能生成图表载荷。

    ENHANCE 2026-10-27（需求 3·AI 优先）：提供 ``ai_client`` 时**优先 AI 生成**——
    AI 载荷基于章节真实内容（真实工序/任务/人数），本地模板只是兜底。旧实现
    本地模板先行且对全部 7 类恒成功，AI 载荷生成是**死代码**，与「AI 自动生成
    Mermaid/JSON 载荷」需求相悖。无 ``ai_client`` 时行为与旧版完全一致（本地
    模板快速路径），零回归。
    """
    if not chart_type or chart_type == "none":
        return None

    # AI 生成（优先路径：载荷反映章节真实内容）
    ai_payload: dict | None = None
    if ai_client is not None:
        try:
            ai_payload = _ai_payload_generation(
                section_title, content, chart_type, title_hint, ai_client
            )
        except Exception as e:
            logger.debug("AI 载荷生成失败，回退本地模板: %s", e)

    if ai_payload:
        # A3 建议 1（显式披露）：打来源标记，前端据 source="local" 打
        # 「模板数据」标签引导用户核定，导出链路据此决定是否挂披露括注。
        ai_payload["source"] = "ai"
        return ai_payload

    # 本地生成（快速路径 / AI 不可用或失败时的兜底）
    local_payload = _local_payload_generation(section_title, content, chart_type, title_hint)
    if local_payload:
        local_payload["source"] = "local"
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

# 标书智能体（4）——提示词顺序优化（缓存命中）：图表分析/载荷生成的稳定规则
# （图表类型标准 + 输出契约 + Mermaid/JSON 语法约束）抽成独立 system 消息，
# 跨调用完全一致 → 稳定前缀可命中服务商缓存；项目信息（稳定）与当前章节
# 任务（易变）拆成独立 user 消息，任务差异放最后。
_CHART_ANALYSIS_SYSTEM = """你是标书图表编排专家，负责判断章节是否需要配图并选择图表类型。

【图表类型标准】
architecture=组织架构/体系图；flowchart=工艺流程/施工流程；gantt=进度计划/工期安排；
comparison=方案比选；layout=总平面/现场布置；labor=劳动力配置；timeline=里程碑时间线；
none=不需要配图（纯文字章节）。

【输出契约】
只返回 JSON 对象，不要任何解释、Markdown 代码围栏：
{"needed":true/false,"chart_type":"上述类型之一","confidence":0-1,"reason":"原因",
 "title_hint":"标题","insert_after":"段落提示"}

【判断规则】
1. needed 仅当图表能显著提升表达时取 true；confidence 表示确信程度（0-1）。
2. chart_type 必须与章节内容特征匹配，不得随意指定。
3. reason 不超过 100 字；title_hint 不超过 30 字。"""

_CHART_PAYLOAD_SYSTEM = """你是标书图表载荷生成专家，负责把章节内容转成图表源码或结构化数据。

【输出契约】
- flowchart/gantt：返回纯 Mermaid 代码（无围栏、无解释）。
- 其他类型：返回纯 JSON 载荷对象（含 type/title 与对应数据字段），无解释文字。

【Mermaid 语法约束】
1. 节点文本使用引号包裹，禁用未转义的特殊字符（括号、引号、分号）。
2. 不使用 mermaid 主题指令、%%init%% 或 HTML 标签。
3. 流程图方向统一 TD；甘特图 dateFormat 统一 YYYY-MM-DD。

【数据约束】
只使用章节内容中真实存在的信息，禁止虚构数据；标题用 title_hint 或章节标题。"""


def _resolve_chart_provider(ai_client: Any) -> Any:
    """把调用方传入的 ai_client 解析为平台 AI provider。

    BUG-FIX 2026-10-23（标书智能体（4）引入时发现）：routers/charts_orchestration.py
    传入的是 ``get_ai_config()`` 返回的**配置 dict**，而旧实现直接调用
    ``ai_client.chat_completion(...)``（OpenAI SDK 形态）——dict 没有该方法，
    AttributeError 被 analyze_chart_needs 的 except 静默吞掉 →「AI 图表分析」
    在生产环境从未真正执行过（永远走本地推断），且平台未安装 openai SDK、
    全仓库无第二处 chat_completion 调用（死代码实证）。

    现统一收敛到平台 provider（provider.chat + AIMessage）：
      - 传入对象已有 .chat → 直接使用（平台 provider / 测试 fake）；
      - dict / None → 经 provider_factory 创建（DB 配置 → 降级链）；
      - 创建失败 → None（调用方按本地推断继续，不阻塞）。
    """
    if ai_client is None:
        return None
    if hasattr(ai_client, "chat"):
        return ai_client
    try:
        from .provider_factory import create_provider_with_fallback

        provider, _name = create_provider_with_fallback(
            ai_client if isinstance(ai_client, dict) else None
        )
        return provider
    except Exception as e:
        logger.debug("图表 AI provider 创建失败，走本地推断: %s", e)
        return None


def _chart_response_text(response: Any) -> str:
    """provider.chat 返回值 → 纯文本（兼容 str / dict / 对象三种形态）。"""
    if response is None:
        return ""
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        return str(response.get("content", "") or "")
    return str(getattr(response, "content", "") or "")


def _run_chat_sync(provider: Any, messages: list, **kwargs: Any) -> Any:
    """在同步函数里驱动异步 provider.chat。

    图表编排链路（analyze_chart_needs / generate_chart_payload /
    batch_analyze_sections ...）是同步函数，且被 async 路由在**运行中的事件
    循环内**调用（routers/charts_orchestration.py），不能直接 asyncio.run。
    策略：当前线程无运行循环 → asyncio.run；有运行循环 → 丢到工作线程跑
    独立 loop（provider 的 httpx 客户端惰性创建，跨 loop 顺序使用；图表调用
    失败有本地推断兜底，风险可控）。
    """
    import asyncio
    import concurrent.futures

    coro = provider.chat(messages, **kwargs)
    if not asyncio.iscoroutine(coro):
        return coro  # 同步 provider（测试 fake 等）直接返回
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _record_chart_cache(provider: Any, task_type: str, section_id: str = "") -> None:
    """图表链路缓存用量记账（标书智能体（4），fail-soft）。"""
    try:
        from .cache_metrics import record_provider_cache_usage

        record_provider_cache_usage(provider, task_type=task_type, section_id=section_id)
    except Exception:  # pragma: no cover - 防御性
        pass


def _ai_chart_analysis(
    section_title: str, content: str, project_summary: str, ai_client: Any
) -> dict[str, Any]:
    """AI 图表需求分析（调用 LLM）。

    标书智能体（4）：system 只放稳定规则（图表类型标准+输出契约），项目信息
    （同项目稳定）与当前章节任务（易变）拆成独立 user 消息、任务放最后 →
    同项目多章节分析时稳定前缀命中缓存。
    """
    provider = _resolve_chart_provider(ai_client)
    if provider is None:
        raise RuntimeError("图表 AI provider 不可用")

    from .prompt_order import build_cache_friendly_messages

    try:
        from app.config import CHART_PROMPT_LAYERS_ENABLED as _LAYERS_ON
    except Exception:  # pragma: no cover - 配置读取兜底
        _LAYERS_ON = True
    task_block = (
        f"标题：{section_title}\n"
        f"内容：{(content or '')[:300]}\n"
        f"项目：{(project_summary or '')[:200]}"
    )
    if _LAYERS_ON:
        _stable = (
            [f"项目信息：{(project_summary or '')[:200]}"]
            if (project_summary or "").strip() else []
        )
        messages = build_cache_friendly_messages(
            _CHART_ANALYSIS_SYSTEM, _stable, task_block
        )
    else:
        messages = [
            {"role": "system", "content": _CHART_ANALYSIS_SYSTEM},
            {"role": "user", "content": task_block},
        ]

    response = _run_chat_sync(provider, messages, temperature=0.3, max_tokens=500)
    _record_chart_cache(provider, "chart_analysis", section_title)
    text = _chart_response_text(response)

    try:
        text = text.strip()
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
    """AI 图表载荷生成（调用 LLM）。

    标书智能体（4）：system 放稳定规则（Mermaid/JSON 语法约束+数据约束），
    当前图表类型与章节内容作为任务差异放最后一条 user 消息。
    """
    provider = _resolve_chart_provider(ai_client)
    if provider is None:
        raise RuntimeError("图表 AI provider 不可用")

    from .prompt_order import build_cache_friendly_messages

    try:
        from app.config import CHART_PROMPT_LAYERS_ENABLED as _LAYERS_ON
    except Exception:  # pragma: no cover - 配置读取兜底
        _LAYERS_ON = True
    task_block = (
        f"生成{chart_type}类型图表载荷，标题：{title_hint or section_title}\n"
        f"内容：{(content or '')[:500]}\n"
        "仅返回纯载荷（Mermaid代码或JSON），无解释文字。"
    )
    if _LAYERS_ON:
        messages = build_cache_friendly_messages(_CHART_PAYLOAD_SYSTEM, [], task_block)
    else:
        messages = [
            {"role": "system", "content": _CHART_PAYLOAD_SYSTEM},
            {"role": "user", "content": task_block},
        ]

    response = _run_chat_sync(provider, messages, temperature=0.2, max_tokens=1000)
    _record_chart_cache(provider, "chart_payload", section_title)
    text = _chart_response_text(response)

    try:
        text = text.strip()
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