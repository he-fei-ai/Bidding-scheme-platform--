"""text_edit.py — 精确文本替换（唯一命中）。

ENHANCE 2026-10-12（P1-3）：移植自 OpenBidKit 易标 `electron/utils/textEdit.cjs`。

平台既有 `apply_autofix` 会把待统一值在**全文所有章节**里的所有出现位置一律替换，
事实取值多为「30」「1」这类极短串，会把「钢筋直径 30 mm」误替换为工期天数。

本模块提供严格唯一命中语义：
  - old_text 在 text 中必须**唯一出现**，否则返回错误
  - 避免追加到末尾造成静默污染
  - 支持批量修改（多组 old/new）并逐条校验

参考易标 `applyTextEdit` 设计：
  - 未命中或多命中都返回错误，不静默
  - 调用方拿到错误后可回退到 AI 修复路径
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class EditResult:
    """单次替换结果。"""
    success: bool
    new_text: str
    matches: int
    error: Optional[str] = None
    index: Optional[int] = None


def apply_text_edit(
    text: str,
    old_text: str,
    new_text: str,
) -> EditResult:
    """在 text 中把 old_text 替换为 new_text，要求唯一命中。

    Args:
        text: 原文本。
        old_text: 待替换的原文片段。
        new_text: 新文本（可为空字符串表示删除）。

    Returns:
        EditResult 包含 success / new_text / matches / error。

    规则：
      - matches == 0: 失败，"not found"
      - matches == 1: 成功
      - matches > 1: 失败，"ambiguous"
      - old_text == new_text: 失败，"no-op"
      - old_text 为空: 失败，"empty old_text"
    """
    if not isinstance(text, str):
        return EditResult(False, text or "", 0, "text not str", None)
    if not isinstance(old_text, str) or not old_text:
        return EditResult(False, text, 0, "empty old_text", None)
    if new_text is None:
        return EditResult(False, text, 0, "new_text None", None)
    if old_text == new_text:
        return EditResult(False, text, 1, "no-op", None)

    # 精确计数
    matches = text.count(old_text)
    if matches == 0:
        return EditResult(False, text, 0, "not found", None)
    if matches > 1:
        return EditResult(
            False, text, matches,
            f"ambiguous: {matches} matches; provide more context or use a wider snippet",
            None,
        )

    # 唯一命中，执行替换
    idx = text.index(old_text)
    result = text[:idx] + new_text + text[idx + len(old_text):]
    return EditResult(True, result, 1, None, idx)


def apply_text_edits(
    text: str,
    edits: List[Dict[str, str]],
    *,
    stop_on_error: bool = True,
) -> Tuple[str, List[EditResult]]:
    """批量应用多组文本修改。

    Args:
        text: 原文本。
        edits: 每组修改是 dict，含 'old_text' 和 'new_text'。
        stop_on_error: True 时遇到第一条失败即停止；False 时继续应用其他修改。

    Returns:
        (最终文本, 每条修改的结果列表)。
    """
    if not isinstance(text, str):
        return text or "", [EditResult(False, text or "", 0, "text not str", None)]
    if not edits:
        return text, []

    results: List[EditResult] = []
    current = text
    for i, edit in enumerate(edits):
        old = edit.get("old_text") if isinstance(edit, dict) else None
        new = edit.get("new_text") if isinstance(edit, dict) else None
        result = apply_text_edit(current, old or "", new or "")
        results.append(result)
        if result.success:
            current = result.new_text
        elif stop_on_error:
            break
    return current, results


def find_all_occurrences(text: str, target: str) -> List[int]:
    """找出 target 在 text 中的所有出现位置。"""
    if not text or not target:
        return []
    positions: List[int] = []
    start = 0
    while True:
        idx = text.find(target, start)
        if idx < 0:
            break
        positions.append(idx)
        start = idx + len(target)
    return positions


def replace_first(text: str, old_text: str, new_text: str) -> str:
    """只替换第一个匹配，不校验唯一性。用于已知位置的定向修改。"""
    idx = text.find(old_text)
    if idx < 0:
        return text
    return text[:idx] + new_text + text[idx + len(old_text):]


def replace_range(text: str, start: int, end: int, new_text: str) -> str:
    """按范围替换。用于已知精确位置的修改（避免字符串匹配歧义）。"""
    if not isinstance(text, str):
        return text or ""
    if start < 0 or end > len(text) or start > end:
        return text
    return text[:start] + new_text + text[end:]
