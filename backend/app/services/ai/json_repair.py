"""json_repair.py — JSON 自动修复循环。

ENHANCE 2026-10-12（P1-4）：移植自 OpenBidKit 易标「json_repair_prompts」方法论。

修复循环最多 N 轮（默认 3 轮），每轮：
  1. 检查 JSON 语法
  2. 如果语法错误，调用修复提示词
  3. 再次检查，循环直到成功或达到轮次上限
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 《标书智能体（5）》§五：校验问题格式化（具体到位置的错误信息供修复消息使用）
from .json_issues import format_issues_from_list  # noqa: E402


# ========== 快速提取 ==========

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)
_JSON_OBJECT_RE = re.compile(r"\{[\s\S]*\}")
_JSON_ARRAY_RE = re.compile(r"\[[\s\S]*\]")


def quick_extract_json(text: Optional[str]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """快速从 AI 输出中提取 JSON。

    Returns:
        (解析结果, 错误信息)。成功时错误为 None。
    """
    if not text or not isinstance(text, str):
        return None, "empty or non-string input"

    # 1. 去除 markdown 代码围栏
    m = _JSON_FENCE_RE.search(text)
    if m:
        text = m.group(1).strip()

    # 2. 直接解析
    try:
        result = json.loads(text)
        return result, None
    except json.JSONDecodeError:
        pass

    # 3. 提取 JSON 对象
    m = _JSON_OBJECT_RE.search(text)
    if m:
        try:
            result = json.loads(m.group(0))
            return result, None
        except json.JSONDecodeError:
            pass

    # 4. 提取 JSON 数组
    m = _JSON_ARRAY_RE.search(text)
    if m:
        try:
            result = json.loads(m.group(0))
            return result, None
        except json.JSONDecodeError:
            pass

    return None, "failed to extract valid JSON"


# ========== 修复提示词 ==========

_REPAIR_SYSTEM_PROMPT = """你是一个 JSON 修复专家。用户给你一段可能包含 JSON 但语法错误的文本，你的任务是：

1. 提取出其中的 JSON 内容（忽略 JSON 之外的任何文字、markdown 标记、代码围栏等）
2. 修复 JSON 语法错误（如缺少引号、括号不匹配、尾随逗号、转义问题等）
3. 保持 JSON 的原始结构和数据不变，只修复语法
4. 只返回修复后的完整 JSON，不要包含任何解释、注释或 markdown 代码围栏"""


@dataclass
class RepairAttempt:
    """单次修复尝试。"""
    round: int
    success: bool
    error: Optional[str] = None
    original: str = ""
    repaired: Optional[str] = None


@dataclass
class RepairResult:
    """修复循环结果。"""
    success: bool
    result: Optional[Dict[str, Any]] = None
    attempts: List[RepairAttempt] = field(default_factory=list)
    total_rounds: int = 0


async def repair_json_with_ai(
    invalid_text: str,
    ai_call: Callable[[List[Dict[str, str]]], Any],
    *,
    max_rounds: int = 3,
    target_description: str = "",
    issues: Optional[List[str]] = None,
) -> RepairResult:
    """用 AI 修复 JSON。

    Args:
        invalid_text: 包含无效 JSON 的原始文本。
        ai_call: AI 调用函数，接收 messages 列表，返回响应内容。
        max_rounds: 最大修复轮数。
        target_description: 目标结果类型（如「完整目录」「一级目录」），
            《标书智能体（5）》§四：修复时须把三部分信息传给模型——
            目标结果类型、当前校验问题、原始内容。缺省时不输出该段。
        issues: 当前校验问题列表（如 validate_outline_json 的返回值）。
            具体到位置的错误信息能显著提高弱模型的修复成功率。

    Returns:
        RepairResult 包含修复结果和尝试历史。
    """
    attempts: List[RepairAttempt] = []
    current_text = invalid_text

    for round_num in range(1, max_rounds + 1):
        # 构建修复提示词
        # ENHANCE 2026-10-12：优先从 prompts 注册表取完整版提示词（JSON_REPAIR_PROMPT），
        # 硬编码 _REPAIR_SYSTEM_PROMPT 作为 DB 不可用时的最小 fallback。
        # ENHANCE 2026-10-23（《标书智能体（5）》§四/§五）：修复消息从单一
        # 「请修复以下 JSON」升级为三部分结构——目标类型 + 校验问题 + 原始内容；
        # 旧调用方不传新参数时行为兼容（仅省略对应段落）。
        from .prompts import get_prompt as _gp
        sys_prompt = _gp("JSON_REPAIR_PROMPT") or _REPAIR_SYSTEM_PROMPT
        _user_parts: List[str] = []
        if target_description:
            _user_parts.append(f"目标类型: {target_description}")
        _issues_text = format_issues_from_list(issues)
        if _issues_text:
            _user_parts.append(f"校验问题:\n{_issues_text}")
        _user_parts.append(f"原始输出:\n{current_text}")
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": "\n\n".join(_user_parts)},
        ]

        # 调用 AI
        try:
            response = await ai_call(messages)
            repaired_text = str(response).strip()
        except Exception as e:
            attempts.append(RepairAttempt(
                round=round_num,
                success=False,
                error=f"AI call failed: {e}",
                original=current_text[:200],
            ))
            continue

        # 尝试解析修复后的 JSON
        result, error = quick_extract_json(repaired_text)
        if result is not None:
            attempts.append(RepairAttempt(
                round=round_num,
                success=True,
                repaired=repaired_text,
            ))
            return RepairResult(
                success=True,
                result=result,
                attempts=attempts,
                total_rounds=round_num,
            )

        attempts.append(RepairAttempt(
            round=round_num,
            success=False,
            error=error,
            original=current_text[:200],
            repaired=repaired_text[:200],
        ))

        # 用修复后的文本作为下一轮的输入
        current_text = repaired_text

    return RepairResult(
        success=False,
        result=None,
        attempts=attempts,
        total_rounds=max_rounds,
    )


async def repair_outline_json(
    invalid_text: str,
    ai_call: Callable[[List[Dict[str, str]]], Any],
    *,
    max_rounds: Optional[int] = None,
    target_description: str = "标书目录 JSON（三级结构，叶子 children 为空数组）",
    issues: Optional[List[str]] = None,
) -> RepairResult:
    """修复目录 JSON（使用 config.OUTLINE_JSON_REPAIR_ROUNDS）。"""
    from ...config import OUTLINE_JSON_REPAIR_ROUNDS
    if max_rounds is None:
        max_rounds = OUTLINE_JSON_REPAIR_ROUNDS
    return await repair_json_with_ai(
        invalid_text,
        ai_call,
        max_rounds=max_rounds,
        target_description=target_description,
        issues=issues,
    )


async def repair_content_json(
    invalid_text: str,
    ai_call: Callable[[List[Dict[str, str]]], Any],
    *,
    max_rounds: Optional[int] = None,
    target_description: str = "正文 JSON（sections 数组）",
    issues: Optional[List[str]] = None,
) -> RepairResult:
    """修复正文 JSON（使用 config.CONTENT_JSON_REPAIR_ROUNDS）。"""
    from ...config import CONTENT_JSON_REPAIR_ROUNDS
    if max_rounds is None:
        max_rounds = CONTENT_JSON_REPAIR_ROUNDS
    return await repair_json_with_ai(
        invalid_text,
        ai_call,
        max_rounds=max_rounds,
        target_description=target_description,
        issues=issues,
    )


def validate_outline_json(data: Any) -> List[str]:
    """校验目录 JSON 结构。

    Returns:
        错误信息列表；空列表表示校验通过。
    """
    errors: List[str] = []

    if not isinstance(data, dict):
        errors.append("根节点必须是对象")
        return errors

    if "outline" not in data and "sections" not in data:
        errors.append("缺少 'outline' 或 'sections' 字段")

    outline = data.get("outline") or data.get("sections") or []
    if not isinstance(outline, list):
        errors.append("'outline' 必须是数组")
        return errors

    for i, node in enumerate(outline):
        if not isinstance(node, dict):
            errors.append(f"节点 {i} 必须是对象")
            continue
        if "id" not in node:
            errors.append(f"节点 {i} 缺少 'id' 字段")
        if "title" not in node:
            errors.append(f"节点 {i} 缺少 'title' 字段")
        if "children" in node and not isinstance(node["children"], list):
            errors.append(f"节点 {i} 的 'children' 必须是数组")

    return errors


def validate_content_json(data: Any) -> List[str]:
    """校验正文 JSON 结构。

    Returns:
        错误信息列表；空列表表示校验通过。
    """
    errors: List[str] = []

    if not isinstance(data, dict):
        errors.append("根节点必须是对象")
        return errors

    if "sections" not in data and "content" not in data:
        errors.append("缺少 'sections' 或 'content' 字段")

    sections = data.get("sections") or []
    if not isinstance(sections, list):
        errors.append("'sections' 必须是数组")
        return errors

    for i, section in enumerate(sections):
        if not isinstance(section, dict):
            errors.append(f"章节 {i} 必须是对象")
            continue
        if "id" not in section:
            errors.append(f"章节 {i} 缺少 'id' 字段")
        if "content" not in section:
            errors.append(f"章节 {i} 缺少 'content' 字段")

    return errors

