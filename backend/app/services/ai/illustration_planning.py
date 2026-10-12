"""illustration_planning.py — 配图预编排。

ENHANCE 2026-10-12（P1-1）：移植自 OpenBidKit 易标「pre-plan illustrations」方法论。

在正文生成前，分析章节内容，规划配图位置、类型和数量。
支持 Mermaid 流程图、架构图、表格等多种图表类型。

设计原则：
  1. **默认关闭**：config.PRE_PLAN_ILLUSTRATIONS=false（默认关闭）。
  2. **预编排**：在正文生成前完成配图规划。
  3. **多样化**：支持多种图表类型和布局。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ========== 图表类型定义 ==========

@dataclass
class IllustrationPlan:
    """配图规划。"""
    section_id: str
    section_title: str
    illustrations: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class Illustration:
    """单个配图。"""
    type: str  # "mermaid" | "table" | "image"
    description: str
    code: Optional[str] = None  # Mermaid 代码
    data: Optional[Dict[str, Any]] = None  # 表格数据
    prompt: Optional[str] = None  # 图片生成提示词


# ========== 配图类型检测 ==========

_MERMAID_INDICATORS = [
    "流程", "步骤", "顺序", "阶段", "阶段划分",
    "架构", "结构", "组成", "模块", "层次",
    "关系", "关联", "依赖", "交互", "通信",
    "时间线", "进度", "阶段计划",
]

_TABLE_INDICATORS = [
    "表格", "清单", "列表", "汇总", "对比",
    "参数", "规格", "指标", "数据",
]


def detect_illustration_types(text: str) -> List[str]:
    """检测文本中适合插入的配图类型。"""
    if not text:
        return []

    types = set()

    # 检测 Mermaid 图表
    for indicator in _MERMAID_INDICATORS:
        if indicator in text:
            types.add("mermaid")
            break

    # 检测表格
    for indicator in _TABLE_INDICATORS:
        if indicator in text:
            types.add("table")
            break

    return list(types)


def plan_illustrations(
    section_id: str,
    section_title: str,
    content: str,
    *,
    max_illustrations: int = 3,
) -> IllustrationPlan:
    """为章节规划配图。

    Args:
        section_id: 章节 ID。
        section_title: 章节标题。
        content: 章节内容。
        max_illustrations: 最大配图数量。

    Returns:
        IllustrationPlan 包含规划的配图列表。
    """
    plan = IllustrationPlan(
        section_id=section_id,
        section_title=section_title,
    )

    if not content:
        return plan

    # 检测适合的图表类型
    types = detect_illustration_types(content)

    for i, type in enumerate(types[:max_illustrations]):
        if type == "mermaid":
            plan.illustrations.append(Illustration(
                type="mermaid",
                description=f"根据章节内容生成的流程图或架构图",
            ))
        elif type == "table":
            plan.illustrations.append(Illustration(
                type="table",
                description=f"根据章节内容生成的数据表格",
            ))

    return plan


def plan_outline_illustrations(
    outline: List[Dict[str, Any]],
    *,
    max_per_section: int = 3,
) -> Dict[str, IllustrationPlan]:
    """为整个目录规划配图。

    Args:
        outline: 目录节点列表。
        max_per_section: 每个章节的最大配图数量。

    Returns:
        以章节 ID 为键的配图规划字典。
    """
    plans: Dict[str, IllustrationPlan] = {}

    def walk(nodes: List[Dict[str, Any]], parent_id: str = ""):
        for i, node in enumerate(nodes):
            if not isinstance(node, dict):
                continue

            node_id = node.get("id") or f"{parent_id}.{i}" if parent_id else str(i)
            title = node.get("title", "")
            description = node.get("description", "")
            content = title + " " + description

            plan = plan_illustrations(
                section_id=node_id,
                section_title=title,
                content=content,
                max_illustrations=max_per_section,
            )
            plans[node_id] = plan

            # 递归处理子节点
            children = node.get("children", [])
            if isinstance(children, list) and children:
                walk(children, node_id)

    walk(outline)
    return plans


# ========== 统计与报告 ==========

def count_illustrations(plans: Dict[str, IllustrationPlan]) -> Dict[str, int]:
    """统计配图数量。"""
    stats = {"total": 0, "by_type": {}}

    for plan in plans.values():
        stats["total"] += len(plan.illustrations)
        for illus in plan.illustrations:
            type = illus.get("type", "unknown") if isinstance(illus, dict) else illus.type
            stats["by_type"][type] = stats["by_type"].get(type, 0) + 1

    return stats


def generate_plan_report(plans: Dict[str, IllustrationPlan]) -> str:
    """生成配图规划报告。"""
    lines = ["# 配图规划报告", ""]

    for section_id, plan in plans.items():
        lines.append(f"## {plan.section_title} ({plan.section_id})")
        if plan.illustrations:
            for i, illus in enumerate(plan.illustrations, 1):
                illus_data = illus if isinstance(illus, dict) else {
                    "type": illus.type,
                    "description": illus.description,
                }
                lines.append(f"{i}. [{illus_data.get('type')}] {illus_data.get('description')}")
        else:
            lines.append("- 无需配图")
        lines.append("")

    stats = count_illustrations(plans)
    lines.append(f"## 统计")
    lines.append(f"- 总章节数：{len(plans)}")
    lines.append(f"- 总配图数：{stats['total']}")
    lines.append(f"- 按类型：{stats['by_type']}")

    return "\n".join(lines)
