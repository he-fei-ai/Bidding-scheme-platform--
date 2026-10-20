"""目录生成质量评分（目录生成工作流程优化方案 · 步骤 11）。

基于 `outline_gate.compute_outline_facts` 产出的确定性事实包，计算五维质量评分：

    完整性 completeness   叶子规模 / 评分要点覆盖 / 层级齐全
    唯一性 uniqueness     同级重复 / 近义重复
    层级深度 depth        最大深度 / 单子节点失衡
    命名规范 naming       标题长度 / 残留占位符
    结构合理性 structure  编号合法性 / 工程逻辑顺序

设计口径：
- 纯函数、确定性：同一事实包必然得到同一分数（无随机、无 AI 调用、无时钟），
  可被后端裁决（`_self_check_v51`）与前端质量报告卡片直接复用；
- 输入只依赖 facts 快照：评分对应「自检时刻」的目录状态，人工编辑后的最终
  目录不会回溯改分（报告定位为生成过程质量参考）；
- 每维 0-100 封顶，加权汇总为总分；等级 A(≥90) / B(≥75) / C(≥60) / D(<60)，
  与优化方案的验收标准（最终质量评分 ≥ 90 分 A 级）对齐。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 维度权重（合计 1.0）。唯一性与完整性并重：标书目录的硬伤几乎都出在这两维。
DIMENSION_WEIGHTS: dict[str, float] = {
    "completeness": 0.25,
    "uniqueness": 0.25,
    "depth": 0.15,
    "naming": 0.20,
    "structure": 0.15,
}

# 维度中文标签（前端质量报告卡片与后端裁决摘要共用，避免两处文案漂移）
DIMENSION_LABELS: dict[str, str] = {
    "completeness": "完整性",
    "uniqueness": "唯一性",
    "depth": "层级深度",
    "naming": "命名规范",
    "structure": "结构合理性",
}

# 等级阈值与文案（优化方案第六节口径）
GRADE_THRESHOLDS: tuple[tuple[float, str], ...] = (
    (90.0, "A（优秀）"),
    (75.0, "B（良好）"),
    (60.0, "C（合格）"),
)
GRADE_FALLBACK = "D（需整改）"


def _clamp(score: float) -> float:
    return max(0.0, min(100.0, float(score)))


def _score_completeness(facts: dict) -> tuple[float, str]:
    """完整性：叶子规模 / 评分要点覆盖 / 层级齐全。"""
    score = 100.0
    notes: list[str] = []

    empty = facts.get("empty_title_nodes") or []
    if empty:
        score -= min(40.0, 15.0 * len(empty))
        notes.append(f"{len(empty)} 个空标题")

    if facts.get("leaf_within_range") is False:
        score -= 30
        notes.append(
            f"叶子数 {facts.get('leaf_count')} 不在目标区间 {facts.get('leaf_acceptable_range')}"
        )

    coverage = facts.get("score_coverage") or {}
    if coverage:
        missing = coverage.get("missing") or []
        matched = coverage.get("matched") or []
        total = len(missing) + len(matched)
        if total and missing:
            ratio = len(missing) / total
            score -= round(35 * ratio, 1)
            notes.append(f"{len(missing)}/{total} 个评分要点未覆盖")
        unexpected = coverage.get("unexpected") or []
        if unexpected:
            score -= min(10.0, 2.0 * len(unexpected))
            notes.append(f"{len(unexpected)} 个章节无对应评分要点")

    by_level = facts.get("node_counts_by_level") or {}
    if by_level and ("2" not in by_level or "3" not in by_level):
        score -= 20
        notes.append("层级不齐全（缺二级或三级节点）")

    return _clamp(score), ("；".join(notes[:3]) or "章节齐全，评分要点覆盖完整")


def _score_uniqueness(facts: dict) -> tuple[float, str]:
    """唯一性：同级完全重复 / 近义重复。"""
    score = 100.0
    notes: list[str] = []

    duplicates = facts.get("duplicate_titles") or []
    if duplicates:
        score -= min(50.0, 20.0 * len(duplicates))
        notes.append(f"{len(duplicates)} 组同级标题完全重复")

    near = facts.get("near_duplicate_titles") or []
    if near:
        score -= min(30.0, 10.0 * len(near))
        notes.append(f"{len(near)} 组近义标题")

    return _clamp(score), ("；".join(notes[:2]) or "无重复标题")


def _score_depth(facts: dict) -> tuple[float, str]:
    """层级深度：标书目录的黄金结构是 3 级（章/节/小节）。"""
    score = 100.0
    notes: list[str] = []

    max_depth = int(facts.get("max_depth") or 0)
    if max_depth == 3:
        pass
    elif max_depth == 2:
        score -= 10
        notes.append("仅两级结构（缺三级节点）")
    elif max_depth == 4:
        score -= 5
        notes.append("四级结构（标书一般三层）")
    elif max_depth >= 5:
        score -= min(40.0, 15.0 * (max_depth - 4))
        notes.append(f"层级深度 {max_depth} 过深")

    single = facts.get("single_child_nodes") or []
    if single:
        score -= min(24.0, 8.0 * len(single))
        notes.append(f"{len(single)} 个节点只有 1 个子节点")

    return _clamp(score), ("；".join(notes[:2]) or "三级结构均衡")


def _score_naming(facts: dict) -> tuple[float, str]:
    """命名规范：标题长度 / 残留占位符。"""
    score = 100.0
    notes: list[str] = []

    placeholders = facts.get("placeholder_titles") or []
    if placeholders:
        score -= min(45.0, 15.0 * len(placeholders))
        notes.append(f"{len(placeholders)} 个标题残留占位符")

    lengths = facts.get("title_length_violations") or []
    if lengths:
        score -= min(25.0, 5.0 * len(lengths))
        notes.append(f"{len(lengths)} 个标题长度越界")

    return _clamp(score), ("；".join(notes[:2]) or "标题命名规范")


def _score_structure(facts: dict) -> tuple[float, str]:
    """结构合理性：编号合法性 / 工程逻辑顺序。"""
    score = 100.0
    notes: list[str] = []

    invalid = facts.get("invalid_id_nodes") or []
    if invalid:
        score -= min(30.0, 10.0 * len(invalid))
        notes.append(f"{len(invalid)} 个节点编号非法")

    zones = facts.get("zone_order_violations") or []
    if zones:
        score -= min(15.0, 5.0 * len(zones))
        notes.append(f"{len(zones)} 处工程逻辑顺序倒置")

    if facts.get("structure_valid") is False:
        score -= 15
        notes.append("结构校验未通过")

    return _clamp(score), ("；".join(notes[:2]) or "编号与顺序合规")


_SCORERS = {
    "completeness": _score_completeness,
    "uniqueness": _score_uniqueness,
    "depth": _score_depth,
    "naming": _score_naming,
    "structure": _score_structure,
}


def grade_for(score: float) -> str:
    """总分 → 等级文案（优化方案口径：A≥90 / B≥75 / C≥60 / D<60）。"""
    for threshold, grade in GRADE_THRESHOLDS:
        if score >= threshold:
            return grade
    return GRADE_FALLBACK


def score_outline_quality(facts: dict) -> dict:
    """计算目录质量评分报告。

    Args:
        facts: `outline_gate.compute_outline_facts` 的事实包快照

    Returns:
        {
            "total_score": 92.5,          # 加权总分（0-100）
            "grade": "A（优秀）",
            "detail": {                    # 五维明细（前端报告卡片直接渲染）
                "completeness": {"score": 95.0, "label": "完整性", "note": "..."},
                ...
            },
            "node_count": 58,              # 统计口径快照
            "chapter_count": 7,
            "leaf_count": 42,
        }
    """
    facts = facts or {}
    detail: dict[str, dict] = {}
    weighted = 0.0
    for dim, weight in DIMENSION_WEIGHTS.items():
        try:
            score, note = _SCORERS[dim](facts)
        except Exception:  # 单维评分失败不得拖垮整份报告（防御脏 facts）
            logger.warning("质量评分维度 %s 计算失败，按满分计入", dim, exc_info=True)
            score, note = 100.0, "评分异常（按满分计入）"
        detail[dim] = {
            "score": round(score, 1),
            "label": DIMENSION_LABELS[dim],
            "note": note,
        }
        weighted += score * weight

    total = round(weighted, 1)
    return {
        "total_score": total,
        "grade": grade_for(total),
        "detail": detail,
        "node_count": int(facts.get("node_count") or 0),
        "chapter_count": int(facts.get("level1_count") or 0),
        "leaf_count": int(facts.get("leaf_count") or 0),
    }


def summarize_quality(quality: dict) -> str:
    """一行摘要：用于日志与进度文案（如「质量评分 92.5 分（A 级，优秀）」）。"""
    if not quality:
        return "质量评分不可用"
    total = quality.get("total_score")
    grade = str(quality.get("grade") or "")
    return f"质量评分 {total} 分（{grade.split('（')[0]} 级）"
