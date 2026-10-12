"""prompt_layering.py — 正文提示词分层消息栈。

ENHANCE 2026-10-12（P0-3）：移植自 OpenBidKit 易标「提示词顺序优化」方法论。

参考软件实测：OpenRouter + gemini-2.5-flash，第二次请求 `cached_tokens=19442`，
输入成本从 0.0058 降到 0.00067，**降低约 10 倍**。

核心原则（易标官方方法论）：
  1. system 只放稳定规则
  2. 最大最稳定上下文尽量前置
  3. 任务差异尽量放最后
  4. 同一份数据组织格式保持稳定（标题写法、换行、strip 一致，不塞随机时间戳）

消息栈分层（从稳定到变化）：
  Layer 0  system          稳定规则（每次一致）
  Layer 1  project_overview 项目概述（全文稳定）
  Layer 2  global_facts     全局事实变量（相对稳定）
  Layer 3  reference_material 参考正文素材（章节级稳定）
  Layer 4  plan_decision    正文编排决策（相对独立）
  Layer 5  parent_chapters  上级章节（当前任务）
  Layer 6  sibling_chapters 同级章节（当前任务）
  Layer 7  current_section  当前章节 + 字数要求（任务差异，最后）
"""

from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional

SYSTEM_ROLE = "system"
USER_ROLE = "user"

LAYER_SYSTEM = "system"
LAYER_PROJECT_OVERVIEW = "project_overview"
LAYER_GLOBAL_FACTS = "global_facts"
LAYER_REFERENCE_MATERIAL = "reference_material"
LAYER_PLAN_DECISION = "plan_decision"
LAYER_PARENT_CHAPTERS = "parent_chapters"
LAYER_SIBLING_CHAPTERS = "sibling_chapters"
LAYER_CURRENT_SECTION = "current_section"

LAYER_ORDER: List[str] = [
    LAYER_SYSTEM,
    LAYER_PROJECT_OVERVIEW,
    LAYER_GLOBAL_FACTS,
    LAYER_REFERENCE_MATERIAL,
    LAYER_PLAN_DECISION,
    LAYER_PARENT_CHAPTERS,
    LAYER_SIBLING_CHAPTERS,
    LAYER_CURRENT_SECTION,
]

_LAYER_LABELS: Dict[str, str] = {
    LAYER_SYSTEM: "[SYSTEM]",
    LAYER_PROJECT_OVERVIEW: "[项目概述]",
    LAYER_GLOBAL_FACTS: "[全局事实]",
    LAYER_REFERENCE_MATERIAL: "[参考素材]",
    LAYER_PLAN_DECISION: "[正文编排决策]",
    LAYER_PARENT_CHAPTERS: "[上级章节]",
    LAYER_SIBLING_CHAPTERS: "[同级章节]",
    LAYER_CURRENT_SECTION: "[当前章节]",
}

_WHITESPACE_RE = re.compile(r"[ \t]+")
_TRAILING_WS_RE = re.compile(r"[\r\n]+\Z")


def normalize_readable_text(text: Optional[str]) -> str:
    """稳定化字符串，保证同一份数据每次渲染结果一致。

    清理规则（易标方法论）：
      1. 统一换行符：\r\n / \r → \n
      2. 每行末尾空白 trim
      3. 行内连续空白（非空行）保留原样
      4. 空行保留（Markdown 语义需要）
      5. 去掉首尾整体空白
    """
    if not text or not isinstance(text, str):
        return ""
    s = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = s.split("\n")
    lines = [line.rstrip() for line in lines]
    return _TRAILING_WS_RE.sub("", "\n".join(lines)).lstrip()


def build_layered_messages(
    context: Dict[str, Any],
    *,
    include_labels: bool = False,
    skip_empty: bool = True,
    layer_templates: Optional[Dict[str, Callable[[str], str]]] = None,
) -> List[Dict[str, str]]:
    """构建分层消息栈。

    Args:
        context: 上下文数据，key 是层标识，value 是该层内容（字符串或可渲染对象）。
        include_labels: True 时每个用户消息前置层标签（如「[项目概述]」），便于调试。
        skip_empty: True 时跳过空内容层。
        layer_templates: 可选的层模板函数；layer 标识 → Callable(str) → str。
    """
    templates = layer_templates or {}
    messages: List[Dict[str, str]] = []

    for layer in LAYER_ORDER:
        raw = context.get(layer)
        if raw is None:
            continue
        if isinstance(raw, str):
            content = raw
        elif callable(raw):
            try:
                content = str(raw())
            except Exception:
                continue
        else:
            content = str(raw)
        if layer in templates:
            try:
                content = templates[layer](content)
            except Exception:
                pass
        content = normalize_readable_text(content)
        if skip_empty and not content.strip():
            continue

        if layer == LAYER_SYSTEM:
            messages.append({"role": SYSTEM_ROLE, "content": content})
        else:
            prefix = _LAYER_LABELS.get(layer, "") if include_labels else ""
            final_content = (prefix + "\n" + content) if prefix else content
            messages.append({"role": USER_ROLE, "content": final_content})

    if not messages:
        messages.append({"role": SYSTEM_ROLE, "content": ""})
    elif messages[0]["role"] != SYSTEM_ROLE:
        messages.insert(0, {"role": SYSTEM_ROLE, "content": "You are a helpful assistant."})

    return messages


def build_chapter_content_messages(
    *,
    system_rules: str,
    project_overview: Optional[str] = None,
    global_facts: Optional[str] = None,
    reference_material: Optional[str] = None,
    plan_decision: Optional[str] = None,
    parent_chapters: Optional[str] = None,
    sibling_chapters: Optional[str] = None,
    current_section: str,
    include_labels: bool = False,
) -> List[Dict[str, str]]:
    """构建章节正文生成的 8 层消息栈（易标顺序）。"""
    context = {
        LAYER_SYSTEM: system_rules,
        LAYER_PROJECT_OVERVIEW: project_overview,
        LAYER_GLOBAL_FACTS: global_facts,
        LAYER_REFERENCE_MATERIAL: reference_material,
        LAYER_PLAN_DECISION: plan_decision,
        LAYER_PARENT_CHAPTERS: parent_chapters,
        LAYER_SIBLING_CHAPTERS: sibling_chapters,
        LAYER_CURRENT_SECTION: current_section,
    }
    return build_layered_messages(context, include_labels=include_labels)


def estimate_layer_stability(context: Dict[str, Any]) -> Dict[str, str]:
    """估算每层内容的稳定性（用于缓存命中分析）。"""
    STABLE_LAYERS = {LAYER_SYSTEM, LAYER_PROJECT_OVERVIEW, LAYER_GLOBAL_FACTS}
    MEDIUM_LAYERS = {LAYER_REFERENCE_MATERIAL, LAYER_PLAN_DECISION}

    result: Dict[str, str] = {}
    for layer in LAYER_ORDER:
        if layer not in context or not str(context.get(layer) or "").strip():
            result[layer] = "empty"
        elif layer in STABLE_LAYERS:
            result[layer] = "stable"
        elif layer in MEDIUM_LAYERS:
            result[layer] = "medium"
        else:
            result[layer] = "volatile"
    return result

