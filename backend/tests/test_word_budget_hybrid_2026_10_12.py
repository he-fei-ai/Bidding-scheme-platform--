"""方案三混合口径（2026-10-12）字数预算 get_section_word_budget 专项测试。

预估总字数只按「每个 L3」累加：
  - 叶子 L3（无下级）：固定 L3 自身正文 300（不受四档下拉影响）；
  - 含下级的 L3：四档 target 作为子树总字数；其 L4+ 下级不单独计入（避免重复计数）；
  - 覆盖优先级：单节点 target_words 精确值 > 单节点 word_count_tier 档位 > 上述规则；
  - L1/L2 不生成正文，不计入。

同时校验：level3_default 返回叶子 L3 自身正文目标（300），不再是四档虚高值；
新增 l3_own_* / leaf_l3_count / subtree_l3_count 字段。

运行：cd backend && pytest tests/test_word_budget_hybrid_2026_10_12.py -v
"""
from __future__ import annotations

import uuid

from app.db import get_db
from app.services.ai.writing_length_config import (
    get_default_tier_config,
    get_l3_own_target,
)
from app.services.sections import get_section_word_budget


def _new_project() -> int:
    name = f"字数预算-{uuid.uuid4().hex[:10]}"
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO projects (name, industry) VALUES (?, '通用')", (name,)
        )
        return int(cur.lastrowid)


def _mk(
    title,
    level,
    *,
    ref=None,
    parent_ref=None,
    target_words=None,
    word_count_tier=None,
    sort_order=1,
) -> dict:
    return {
        "title": title,
        "level": level,
        "ref": ref,
        "parent_ref": parent_ref,
        "target_words": target_words,
        "word_count_tier": word_count_tier,
        "sort_order": sort_order,
    }


def _insert(pid: int, rows: list[dict]) -> None:
    ref_to_id: dict[str, int] = {}
    with get_db() as conn:
        for r in rows:
            parent_id = (
                ref_to_id.get(r["parent_ref"]) if r.get("parent_ref") else None
            )
            conn.execute(
                "INSERT INTO sections (project_id, parent_id, title, level, sort_order,"
                " section_type, status, content, target_words, word_count_tier, word_count)"
                " VALUES (?, ?, ?, ?, ?, 'text', 'pending', '', ?, ?, 0)",
                (
                    pid,
                    parent_id,
                    r["title"],
                    r["level"],
                    r["sort_order"],
                    r["target_words"],
                    r["word_count_tier"],
                ),
            )
            if r.get("ref"):
                # 自增 id 无需预知，按插入顺序回填
                cur = conn.execute("SELECT last_insert_rowid()")
                ref_to_id[r["ref"]] = int(cur.fetchone()[0])


def _tier_target() -> int:
    return int(get_default_tier_config().get("target_words", 1200))


def test_all_leaf_l3_counts_own_each():
    """全叶子 L3：预估 = 叶子数 × 300，与四档无关。"""
    pid = _new_project()
    _insert(pid, [
        _mk("第一章", 1, ref="l1"),
        _mk("1.1", 2, parent_ref="l1", ref="l2"),
        _mk("1.1.1 A", 3, parent_ref="l2"),
        _mk("1.1.2 B", 3, parent_ref="l2"),
        _mk("1.1.3 C", 3, parent_ref="l2"),
    ])
    budget = get_section_word_budget(pid)
    own = get_l3_own_target()
    assert budget["level3_count"] == 3
    assert budget["leaf_l3_count"] == 3
    assert budget["subtree_l3_count"] == 0
    assert budget["total_estimate"] == 3 * own
    # level3_default 改为叶子自身正文目标（300），非四档 1200
    assert budget["level3_default"] == own
    assert (budget["l3_own_lo"], budget["l3_own_hi"]) == (240, 360)


def test_leaf_l3_ignores_global_tier():
    """叶子 L3 预估按 300，而非全局四档 target（修复前虚高约 4 倍）。"""
    pid = _new_project()
    _insert(pid, [
        _mk("第一章", 1, ref="l1"),
        _mk("1.1", 2, parent_ref="l1", ref="l2"),
        _mk("1.1.1 A", 3, parent_ref="l2"),
    ])
    budget = get_section_word_budget(pid)
    assert budget["total_estimate"] == get_l3_own_target()
    tier = _tier_target()
    if tier != get_l3_own_target():
        assert budget["total_estimate"] != tier


def test_l3_with_children_counts_subtree_once():
    """含下级的 L3 记一次档位（子树总字数）；其 L4+ 下级不单独累加。"""
    pid = _new_project()
    _insert(pid, [
        _mk("第一章", 1, ref="l1"),
        _mk("1.1", 2, parent_ref="l1", ref="l2"),
        _mk("1.1.1 有下级", 3, parent_ref="l2", ref="l3"),
        _mk("1.1.1.1 子", 4, parent_ref="l3"),
        _mk("1.1.2 叶子", 3, parent_ref="l2"),
    ])
    budget = get_section_word_budget(pid)
    own = get_l3_own_target()
    # 子树 L3 = 四档 target；叶子 L3 = 300；L4 不重复计入
    assert budget["total_estimate"] == _tier_target() + own
    assert budget["subtree_l3_count"] == 1
    assert budget["leaf_l3_count"] == 1


def test_node_target_words_override():
    """单节点 target_words 精确覆盖最高优先级。"""
    pid = _new_project()
    _insert(pid, [
        _mk("第一章", 1, ref="l1"),
        _mk("1.1", 2, parent_ref="l1", ref="l2"),
        _mk("1.1.1 自定义999", 3, parent_ref="l2", target_words=999),
    ])
    budget = get_section_word_budget(pid)
    assert budget["total_estimate"] == 999
    assert budget["has_custom_targets"] == 1


def test_node_word_count_tier_override():
    """叶子 L3 显式设置 word_count_tier → 按档位目标（覆盖固定 300）。"""
    pid = _new_project()
    _insert(pid, [
        _mk("第一章", 1, ref="l1"),
        _mk("1.1", 2, parent_ref="l1", ref="l2"),
        _mk("1.1.1 详细档", 3, parent_ref="l2", word_count_tier="detailed"),
    ])
    budget = get_section_word_budget(pid)
    assert budget["total_estimate"] == 2000
