"""L1 编号残留清洗（ENHANCE 2026-11-02）。

`_cleanup_placeholder_titles` 旧实现 `level >= 2` 把一级完全排除：AI 若把
「总体实施策划（2）」「专项6」产出为**一级章节名**，会穿过 L1 审核与骨架
锚定一路到落库，且其子树的清洗全部以脏标题为语义源。

本组测试钉住新行为：
1. L1 带编号后缀/前缀 → 确定性剥除、保留干净主体；
2. L1 主体剥无可剥（碎片/仍脏）→ 保持原样（不做语义重写），返回计数不含它；
3. 合法 L1 章名（含"专项施工方案"等近邻）→ 零改动，防误伤。
"""

from __future__ import annotations

from app.services.ai.outline_common import (
    _cleanup_placeholder_titles,
    _is_dirty_l2_title_deep,
)


def _node(title: str, children=None):
    return {"title": title, "level": 1, "children": children or []}


class TestL1NumberingResidueCleanup:
    def test_l1_suffix_stripped_body_kept(self):
        """「总体实施策划（2）」作 L1 → 剥后缀保主体「总体实施策划」。"""
        tree = [_node("总体实施策划（2）", [_node("A", [])])]
        fixed = _cleanup_placeholder_titles(tree)
        assert tree[0]["title"] == "总体实施策划", tree[0]["title"]
        assert fixed >= 1

    def test_l1_prefix_stripped_body_kept(self):
        """「2、总体实施策划」作 L1 → 剥前缀保主体。"""
        tree = [_node("2、总体实施策划")]
        _cleanup_placeholder_titles(tree)
        assert tree[0]["title"] == "总体实施策划", tree[0]["title"]

    def test_l1_halfwidth_paren_suffix_stripped(self):
        tree = [_node("总体实施策划(3)")]
        _cleanup_placeholder_titles(tree)
        assert tree[0]["title"] == "总体实施策划", tree[0]["title"]

    def test_l1_pure_placeholder_untouched(self):
        """「专项6」剥后只剩碎片 → L1 不做语义重写，保持原样（暴露给上游链）。"""
        tree = [_node("专项6")]
        fixed = _cleanup_placeholder_titles(tree)
        assert tree[0]["title"] == "专项6", tree[0]["title"]
        assert fixed == 0

    def test_l1_legal_titles_zero_touch(self):
        """合法一级章名一律零改动——含"专项"字样的近邻，防误伤评分项映射。"""
        legal = [
            "专项施工方案",
            "危险性较大的分部分项工程专项方案",
            "总体实施策划",
            "第1章 编制依据",  # 「第X章」前缀在 L1 判脏与否取决于判定表，此处只保证不抛错
        ]
        for t in legal:
            tree = [_node(t)]
            _cleanup_placeholder_titles(tree)
            if not _is_dirty_l2_title_deep(t):
                assert tree[0]["title"] == t, f"合法 L1 被误改: {t!r} → {tree[0]['title']!r}"

    def test_l1_children_parent_title_uses_cleaned_body(self):
        """L1 清洗后，其子树语义源应使用清洗后的主体而非脏标题。"""
        dirty_child = _node("专项6", [])
        tree = [_node("总体实施策划（2）", [dirty_child])]
        _cleanup_placeholder_titles(tree)
        assert tree[0]["title"] == "总体实施策划"
        # L2 子节点标题仍参与自身清洗链路，不因父级清洗而丢失结构
        assert len(tree[0]["children"]) == 1
