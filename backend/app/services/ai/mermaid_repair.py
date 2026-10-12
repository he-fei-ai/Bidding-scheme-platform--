"""mermaid_repair.py — Mermaid 图修复。

ENHANCE 2026-10-12（P1-2）：移植自 OpenBidKit 易标「mermaid_repair_prompt」。

当 Mermaid 图渲染失败时，用专门的修复提示词让 AI 修正 Mermaid 语法错误。
默认修复 3 轮，每轮检查语法并尝试修复。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ========== 修复提示词 ==========

_REPAIR_SYSTEM_PROMPT = """你是一个 Mermaid 图表修复专家。用户给你一段可能语法错误的 Mermaid 代码，你的任务是：

1. 分析错误原因（语法错误、节点定义错误、连线错误、样式错误等）
2. 修复所有语法错误，保持图表的原始意图和结构
3. 保持节点 ID、标签、连线关系不变，只修复语法
4. 只返回修复后的完整 Mermaid 代码，不要包含任何解释、markdown 围栏或注释

常见修复场景：
- 节点定义语法错误（如缺少引号、括号不匹配）
- 连线语法错误（如箭头符号错误、缺少连接符）
- 样式语法错误（如 classDef 语法错误）
- 特殊字符转义问题
- 中文标签需要引号包裹"""


@dataclass
class MermaidRepairAttempt:
    """单次修复尝试。"""
    round: int
    success: bool
    error: Optional[str] = None
    original: str = ""
    repaired: Optional[str] = None


@dataclass
class MermaidRepairResult:
    """修复循环结果。"""
    success: bool
    result: Optional[str] = None
    attempts: List[MermaidRepairAttempt] = field(default_factory=list)
    total_rounds: int = 0


# ========== 语法检查 ==========

_MERMAID_START_RE = re.compile(r"^\s*(?:flowchart|graph|sequenceDiagram|classDiagram|stateDiagram|erDiagram|gantt|pie|journey|timeline|quadrantChart|sankey|xychart|packet|kanban|mindmap|gitGraph|architecture|block)\b", re.IGNORECASE)


def check_mermaid_syntax(code: Optional[str]) -> Optional[str]:
    """检查 Mermaid 语法是否有效。

    Returns:
        错误信息；None 表示语法正确。
    """
    if not code or not isinstance(code, str):
        return "empty or non-string input"

    code = code.strip()

    # 检查是否以有效的 Mermaid 关键字开头
    if not _MERMAID_START_RE.match(code):
        return "invalid mermaid start keyword"

    # 检查括号平衡
    paren_count = 0
    bracket_count = 0
    for char in code:
        if char == '(':
            paren_count += 1
        elif char == ')':
            paren_count -= 1
        elif char == '[':
            bracket_count += 1
        elif char == ']':
            bracket_count -= 1

    if paren_count != 0:
        return f"unbalanced parentheses: {paren_count}"
    if bracket_count != 0:
        return f"unbalanced brackets: {bracket_count}"

    # 检查引号平衡（简单检查）
    quote_count = code.count('"')
    if quote_count % 2 != 0:
        return "unbalanced quotes"

    return None


async def repair_mermaid_with_ai(
    invalid_code: str,
    ai_call: Callable[[List[Dict[str, str]]], Any],
    *,
    max_rounds: Optional[int] = None,
) -> MermaidRepairResult:
    """用 AI 修复 Mermaid 代码。

    Args:
        invalid_code: 包含无效 Mermaid 的原始代码。
        ai_call: AI 调用函数，接收 messages 列表，返回响应内容。
        max_rounds: 最大修复轮数。

    Returns:
        MermaidRepairResult 包含修复结果和尝试历史。
    """
    from ...config import MERMAID_REPAIR_ROUNDS
    if max_rounds is None:
        max_rounds = MERMAID_REPAIR_ROUNDS

    attempts: List[MermaidRepairAttempt] = []
    current_code = invalid_code

    for round_num in range(1, max_rounds + 1):
        # 检查当前代码语法
        syntax_error = check_mermaid_syntax(current_code)
        if syntax_error is None:
            # 语法正确，返回
            return MermaidRepairResult(
                success=True,
                result=current_code,
                attempts=attempts,
                total_rounds=round_num - 1,
            )

        # ENHANCE 2026-10-12：优先从 prompts 注册表取完整版提示词（MERMAID_REPAIR_PROMPT），
        # 硬编码 _REPAIR_SYSTEM_PROMPT 作为 DB 不可用时的最小 fallback。
        from .prompts import get_prompt as _gp
        sys_prompt = _gp("MERMAID_REPAIR_PROMPT") or _REPAIR_SYSTEM_PROMPT
        # 构建修复提示词
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": f"请修复以下 Mermaid 代码：\n\n{current_code}\n\n错误信息：{syntax_error}"},
        ]

        # 调用 AI
        try:
            response = await ai_call(messages)
            repaired_code = str(response).strip()
        except Exception as e:
            attempts.append(MermaidRepairAttempt(
                round=round_num,
                success=False,
                error=f"AI call failed: {e}",
                original=current_code[:200],
            ))
            continue

        # 去除 markdown 围栏
        if repaired_code.startswith("```"):
            lines = repaired_code.split('\n')
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            repaired_code = '\n'.join(lines)

        attempts.append(MermaidRepairAttempt(
            round=round_num,
            success=False,
            error=syntax_error,
            original=current_code[:200],
            repaired=repaired_code[:200],
        ))

        # 用修复后的代码作为下一轮的输入
        current_code = repaired_code

    # 最终检查
    final_error = check_mermaid_syntax(current_code)
    return MermaidRepairResult(
        success=final_error is None,
        result=current_code if final_error is None else None,
        attempts=attempts,
        total_rounds=max_rounds,
    )


def extract_mermaid_blocks(text: Optional[str]) -> List[str]:
    """从文本中提取所有 Mermaid 代码块。"""
    if not text or not isinstance(text, str):
        return []

    blocks = []
    pattern = re.compile(r"```mermaid\s*([\s\S]*?)\s*```", re.IGNORECASE)
    for m in pattern.finditer(text):
        blocks.append(m.group(1).strip())
    return blocks


def replace_mermaid_block(text: str, old_code: str, new_code: str) -> str:
    """替换文本中的 Mermaid 代码块。"""
    if not old_code or not new_code:
        return text

    # 尝试替换整个代码块
    old_pattern = f"```mermaid\n{old_code}\n```"
    new_pattern = f"```mermaid\n{new_code}\n```"
    if old_pattern in text:
        return text.replace(old_pattern, new_pattern)

    # 尝试替换代码内容（不带围栏）
    return text.replace(old_code, new_code)
