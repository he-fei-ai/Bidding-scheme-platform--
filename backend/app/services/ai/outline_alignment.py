"""outline_alignment.py — 目录一一对应模式（aligned）。

ENHANCE 2026-10-12（P0-2）：移植自 OpenBidKit 易标「aligned」模式。

平台既有目录生成完全依赖 AI 生成 L1 一级目录，无法保证与评分项一一对应。
本模块提供对齐工具：从技术评分要求提取评分大类 → 程序构造 L1 → 锁死顺序
→ 模型只生成 L2/L3 → 最后校验 L1 与评分大类一致。

STATUS 2026-10-13（L6 · 死代码评估结论，**有意保留不删**）：
本模块当前**无生产调用方**（`generate_outline_v51` 尚未接入 aligned 形态判别），
由 `tests/test_2026_10_12_enhance_modules.py` 成体系覆盖（评分提取 / L1 构造 /
一致性校验）。作为「已实现未接线」的能力保留：将来接入目录生成主链时可直接启用，
无需重新实现。接线前置条件：L1 由程序构造的章节同样须过
`outline_common._is_dirty_l3_title` 口径的标题校验（2026-10-13 加固）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


_SCORING_HEADER_RE = re.compile(
    r"^\s*(?:(?:一|二|三|四|五|六|七|八|九|十|[0-9]+)[、．\.]\s*)?"
    r"([\u4e00-\u9fff\-／/·]+?)"
    r"\s*"
    r"(?:（\s*([\d\.]+)\s*(?:分|%)?\s*）|"
    r"\[\s*([\d\.]+)\s*(?:分|%)?\s*\]|"
    r"\(\s*([\d\.]+)\s*分\s*\)|"
    r"([\d\.]+)\s*(?:分|%))",
    re.IGNORECASE,
)


@dataclass
class ScoringItem:
    """评分项（从评分要求解析而来）。"""
    id: str
    title: str
    weight: float
    description: str = ""
    source_text: str = ""


@dataclass
class AlignmentResult:
    """对齐结果。"""
    success: bool
    scoring_items: List[ScoringItem] = field(default_factory=list)
    aligned_l1: List[Dict[str, Any]] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)


def parse_scoring_items(text: Optional[str]) -> List[ScoringItem]:
    """从评分要求文本中解析评分项。"""
    if not text or not isinstance(text, str):
        return []

    items: List[ScoringItem] = []
    current_item: Optional[ScoringItem] = None

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            if current_item:
                current_item.description += "\n"
            continue

        m = _SCORING_HEADER_RE.match(stripped)
        if m:
            title = (m.group(1) or "").strip()
            weight_str = m.group(2) or m.group(3) or m.group(4) or m.group(5)
            if title and weight_str:
                if current_item:
                    current_item.description = current_item.description.strip()
                current_item = ScoringItem(
                    id=f"R{len(items) + 1}",
                    title=title,
                    weight=float(weight_str),
                    source_text=stripped,
                )
                items.append(current_item)
        elif current_item:
            current_item.description += (
                "\n" if current_item.description else ""
            ) + stripped

    if current_item:
        current_item.description = current_item.description.strip()

    return items


def build_aligned_l1(items: List[ScoringItem]) -> List[Dict[str, Any]]:
    """从评分项列表构造锁死顺序的 L1 目录。"""
    result: List[Dict[str, Any]] = []
    for index, item in enumerate(items, 1):
        result.append({
            "id": str(index),
            "title": item.title,
            "description": item.description or item.title,
            "source_requirement_id": item.id,
            "source_requirement_title": item.title,
            "source_requirement_weight": item.weight,
            "children": [],
            "locked": True,
        })
    return result


def validate_alignment(
    outline: List[Dict[str, Any]],
    scoring_items: List[ScoringItem],
) -> List[str]:
    """校验目录 L1 是否与评分项一一对应。"""
    suggestions: List[str] = []
    if not outline:
        suggestions.append("目录为空，无法与评分项对齐")
        return suggestions
    if not scoring_items:
        suggestions.append("评分项为空，跳过对齐校验")
        return suggestions

    if len(outline) != len(scoring_items):
        suggestions.append(
            f"L1 数量不一致：目录有 {len(outline)} 个，评分项有 {len(scoring_items)} 个"
        )

    for i, (l1, item) in enumerate(zip(outline, scoring_items)):
        title = str(l1.get("title") or "").strip()
        expected = item.title.strip()
        if title != expected:
            suggestions.append(
                f"L1 第 {i+1} 项标题不匹配：目录「{title}」≠ 评分项「{expected}」"
            )
        if l1.get("source_requirement_id") and l1.get("source_requirement_id") != item.id:
            suggestions.append(
                f"L1 第 {i+1} 项 source_requirement_id 不匹配："
                f"目录 {l1.get('source_requirement_id')} ≠ 评分项 {item.id}"
            )

    return suggestions


def align_outline_to_scoring(
    outline: List[Dict[str, Any]],
    scoring_text: Optional[str],
    *,
    strict: bool = False,
) -> AlignmentResult:
    """主入口：把目录 L1 对齐到评分项。

    Args:
        outline: 现有目录（会被就地修改 L1）。
        scoring_text: 技术评分要求文本。
        strict: True 时校验失败抛出异常；False 时返回 suggestions。
    """
    if not scoring_text or not scoring_text.strip():
        return AlignmentResult(
            success=False,
            suggestions=["评分要求文本为空，无法对齐"],
        )

    scoring_items = parse_scoring_items(scoring_text)
    if not scoring_items:
        return AlignmentResult(
            success=False,
            suggestions=["未能从评分要求中解析出评分项"],
        )

    aligned_l1 = build_aligned_l1(scoring_items)
    suggestions = validate_alignment(outline, scoring_items)

    if strict and suggestions:
        raise ValueError("目录对齐校验失败：" + "; ".join(suggestions))

    return AlignmentResult(
        success=not suggestions,
        scoring_items=scoring_items,
        aligned_l1=aligned_l1,
        suggestions=suggestions,
    )


def merge_aligned_l1_with_children(
    aligned_l1: List[Dict[str, Any]],
    existing_outline: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """把对齐的 L1 与现有 L2/L3 子节点合并。

    策略：如果 existing_outline 的 L1 与 aligned_l1 标题匹配，保留其 children；
    否则使用空 children（等待 AI 生成）。
    """
    result: List[Dict[str, Any]] = []
    existing_by_title = {
        str(item.get("title") or "").strip(): item
        for item in (existing_outline or [])
    }

    for l1 in aligned_l1:
        new_l1 = dict(l1)
        existing = existing_by_title.get(l1["title"])
        if existing and isinstance(existing.get("children"), list):
            new_l1["children"] = list(existing["children"])
        else:
            new_l1["children"] = []
        result.append(new_l1)

    return result

