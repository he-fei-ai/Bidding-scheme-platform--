"""outline_alignment.py — 目录一一对应模式（aligned）。

ENHANCE 2026-10-12（P0-2）：移植自 OpenBidKit 易标「aligned」模式。

平台既有目录生成完全依赖 AI 生成 L1 一级目录，无法保证与评分项一一对应。
本模块提供对齐工具：从技术评分要求提取评分大类 → 程序构造 L1 → 锁死顺序
→ 模型只生成 L2/L3 → 最后校验 L1 与评分大类一致。

STATUS 2026-10-20（易标机制引入 P0-1/P0-2）：本模块已接入生产链路——
`config.OUTLINE_ALIGNED_MODE="aligned"` 时，`workflows_outline_v51._generate_level1_v51`
直接采用程序构造的 L1（评分项→锁定章节，模型只生成 L2/L3），并由
`augment_aligned_l1` 回填易标 OutlineItem 扩展字段（content_mode/
source_requirement_id/locked）。默认 `"free"` 保持历史行为不变。
历史评估结论（2026-10-13 L6）：接线前置条件——程序构造的章节同样须过
`outline_common._is_dirty_l3_title` 口径的标题校验，由调用方在构造后统一
走 `normalize_outline_items + outline_gate` 门禁满足。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


logger = logging.getLogger(__name__)


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


#: 易标 OutlineItem.content_mode 的四种取值（对齐参考软件 outline.ts）。
#: 本平台当前只有 ai-generate 有正文生成实现；其余三种为未来模式预留，
#: 落库后由正文生成侧 resolve_content_mode 统一告警并回落，避免「死字段」。
CONTENT_MODE_AI_GENERATE = "ai-generate"
CONTENT_MODE_TEMPLATE_FILL = "template-fill"
CONTENT_MODE_POINT_TO_POINT = "point-to-point"
CONTENT_MODE_OTHER = "other"
CONTENT_MODES = (
    CONTENT_MODE_AI_GENERATE,
    CONTENT_MODE_TEMPLATE_FILL,
    CONTENT_MODE_POINT_TO_POINT,
    CONTENT_MODE_OTHER,
)

#: 正文生成侧已有消费实现的模式（2026-10-20 遗留 5）
IMPLEMENTED_CONTENT_MODES = frozenset({CONTENT_MODE_AI_GENERATE})

#: 历史/外部数据的常见别名（大小写与下划线在 normalize 中先归一）
_CONTENT_MODE_ALIASES = {
    "ai": CONTENT_MODE_AI_GENERATE,
    "template": CONTENT_MODE_TEMPLATE_FILL,
    "point-to-point": CONTENT_MODE_POINT_TO_POINT,
}


def normalize_content_mode(value: Any) -> str:
    """归一 content_mode 取值。

    空值/未知值一律回落 ``ai-generate``（历史默认，保证旧数据零行为变化）；
    已知但暂未实现的模式（template-fill/point-to-point/other）保持原值，
    由消费点显式告警，而不是在落库时被静默改写。
    """
    raw = str(value or "").strip().lower().replace("_", "-")
    if not raw:
        return CONTENT_MODE_AI_GENERATE
    raw = _CONTENT_MODE_ALIASES.get(raw, raw)
    return raw if raw in CONTENT_MODES else CONTENT_MODE_AI_GENERATE


def resolve_content_mode(section: Dict[str, Any] | None) -> str:
    """正文生成侧 content_mode 消费点（2026-10-20 遗留 5）。

    当前生成链路只有 ``ai-generate`` 一种实现。模板填充/点对点模式若
    出现在数据里（历史导入/外部对接），必须显式告警并回落，保证：
    ``content_mode`` 是可被消费的契约字段，而不是只在内存里透出的死数据。
    未来接入新实现时，只需扩展 ``IMPLEMENTED_CONTENT_MODES`` 并在此分流。
    """
    mode = normalize_content_mode((section or {}).get("content_mode"))
    if mode not in IMPLEMENTED_CONTENT_MODES:
        logger.warning(
            "[正文生成] content_mode=%s 暂无消费实现，章节「%s」回落 ai-generate",
            mode,
            str((section or {}).get("title") or "")[:60],
        )
        return CONTENT_MODE_AI_GENERATE
    return mode


def augment_aligned_l1(l1_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """为程序构造的对齐 L1 回填易标 OutlineItem 扩展字段（2026-10-20 P0-2）。

    对齐参考软件 client/src/shared/types/outline.ts 的 OutlineItem 结构：
      - source_requirement_id：目录节点→评分项回溯标识（与 ScoringItem.id 同口径 R{n}）；
      - content_mode：默认 "ai-generate"（易标四种模式 ai-generate/template-fill/
        point-to-point/other 中，本链路生成的章节均为 AI 生成）；
      - locked：标记 L1 由程序构造，下游（审核/重生成）不得改写一级顺序。

    幂等：已有值不覆盖（允许调用方预先定制 content_mode 等字段）；
    向后兼容：旧数据无这些字段时仅新增，不修改既有字段，导出链路不感知。
    """
    for index, node in enumerate(l1_items or [], 1):
        if not isinstance(node, dict):
            continue
        if not node.get("source_requirement_id"):
            node["source_requirement_id"] = f"R{index}"
        if not node.get("source_requirement_title"):
            node["source_requirement_title"] = str(node.get("title") or "")
        if not node.get("content_mode"):
            node["content_mode"] = CONTENT_MODE_AI_GENERATE
        else:
            # 幂等归一：已知别名（ai/template…）收敛到标准值；未知值回落默认，
            # 与正文生成侧 resolve_content_mode 共用同一份取值契约。
            node["content_mode"] = normalize_content_mode(node.get("content_mode"))
        node.setdefault("locked", True)
    # None 入参归一化为 []（与全仓防御口径一致），list 入参原地修改后原样返回
    return l1_items if l1_items is not None else []


# ---------------------------------------------------------------------------
# 建筑工程专项施工方案「九大章节标准」（2026-10-20 易标机制引入适配层，
# 任务规格四.1：参考软件的「评分大类」对齐逻辑适配为本平台的
# 「专项方案类型/主要施工内容」对齐；目录九大章节标准必须保留）。
#
# 适用场景：aligned 模式下无评分项的专项方案编写（高支模/基坑/脚手架等
# 专项方案往往没有评分表，方案名称即唯一需求）——一级目录由程序直接
# 按行业标准构造，模型只生成 L2/L3；章节顺序由 locked 标记保护，
# 不受门禁 zone 重排（见 workflows_outline_v51._l1_locked）。
# ---------------------------------------------------------------------------

#: 九大章节标准（标题, 职责描述）——顺序即行业标准顺序，不得重排
NINE_CHAPTER_STANDARD: List[tuple] = [
    ("工程概况", "专项方案对应分部分项工程介绍、规模参数与项目特征描述"),
    ("编制依据", "编制所依据的法规、规范标准、施工图与专项方案类型相关的设计文件清单"),
    ("施工计划", "进度计划、材料与劳动力等资源配置安排、施工顺序与流水段划分"),
    ("施工工艺技术", "工艺原理、施工工艺流程、操作要点与关键技术参数（含验算）"),
    ("安全保证措施", "风险辨识与分级管控、组织保障、技术措施、监测与检查制度"),
    ("人员配备", "管理架构与作业班组配置、岗位职责、持证要求与培训安排"),
    ("验收要求", "验收标准与依据、过程验收与竣工验收程序、整改闭环要求"),
    ("应急处置措施", "易引发突发事件的危险源辨识、应急组织、响应程序与物资储备"),
    ("计算书与图纸", "稳定性/强度等验算计算书与相关施工图纸、节点大样"),
]


def build_nine_chapter_l1() -> List[Dict[str, Any]]:
    """按建筑工程专项施工方案九大章节标准程序构造一级目录。

    节点字段与 `build_aligned_l1` 同构（易标 OutlineItem 对齐）：
    source_requirement_id 取「S{n}」（S=standard，与评分项 R{n} 命名空间
    区分）；content_mode=ai-generate；locked=True（顺序由标准锁定）。
    """
    result: List[Dict[str, Any]] = []
    for index, (title, description) in enumerate(NINE_CHAPTER_STANDARD, 1):
        result.append({
            "id": str(index),
            "title": title,
            "description": description,
            "source_requirement_id": f"S{index}",
            "source_requirement_title": title,
            "children": [],
            "locked": True,
        })
    return augment_aligned_l1(result)

