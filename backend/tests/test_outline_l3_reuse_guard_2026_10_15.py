"""2026-10-15 生成大纲模块第四轮加固回归。

症状来源（用户反馈）：「三级标题大量是占位式泛化名称，且存在编号冲突后缀」。
排查证明「专项6」「总体实施策划（2）」两个家族早已被 2026-10-09~10-13 三轮清洗
拦住（生产库 809 行章节零命中），但**同一症状在当前代码里换了三副面孔**，
且其中一副是我们自己的清洗器制造的。本轮修复锚点：

  B1 清洗器过度剥离：「悬挑架安装（2）」这类**主体完全具体**的标题只带一个
     冲突后缀，旧实现命中脏判定后整段替换为词池标题（实测变成「技术交底要点」），
     清洗器自己成了泛化标题来源。现按「先剥后缀保主体，主体仍泛化才重写」处理。
  B2 兜底实体跨章错配：`_l3_entity_candidates` 只按语义域从全局词池取实体，
     而 `_DOMAIN_KEYWORDS` 缺悬挑花篮架/盘扣支撑体系/试验计量等专业词，这些章
     被判 generic 后落回 work 池 → 「悬挑花篮架」章下产出「地下室施工工艺」
     （21 章骨架实测错配 70/525）。现从祖级 L1 标题切本章实体并优先使用。
  B3 跨章成套复用无护栏：2026-10-11 把去重作用域从全树改为同级（该政策本身正确，
     它消除的是「删除跨父同名节点」与「（其N）」后缀污染），但**没有任何机制约束
     复用广度** → AI 逐章按同一套骨架填充时，同一批泛化标题被成套复制到几十章
     （生产库实测单标题 25 个父级、三级跨章重复率 97%）。
     新增 `_cap_cross_chapter_repetition` 按「不同父级个数」设上限收口。
  B4 消歧产物无脏复检：`_uniquify_outline_titles` 在清洗**之后**改名，
     `_disambiguate_title` 的维度词分支会产出「总体实施策划专项方案」这类
     仍泛化的标题并直接落库。现补一道统一脏判定 + 语义化重生成。
  B5 `_ensure_min_children` 补建 L3 的两处接线缺陷：传 `set()` 当 used
     （注释写「used 含全局」但代码是空集 → 整批派生标题跨章成套复制），
     且不传 `grandparent_title`（L2 恒为通用维度名，拿不到本章实体词）。
  C1 新配置 OUTLINE_L3_MAX_PARENT_REUSE：默认 3；设为 0 关闭护栏（向后兼容）。

运行：cd backend && python -m pytest tests/test_outline_l3_reuse_guard_2026_10_15.py -q -p no:cacheprovider
"""

import pytest

from app.config import OUTLINE_L3_MAX_PARENT_REUSE
from app.services.ai.outline_common import (
    _cap_cross_chapter_repetition,
    _cleanup_placeholder_titles,
    _is_dirty_l3_title_deep,
    _l3_entity_candidates,
    _title_entity_fragments,
    _uniquify_outline_titles,
    _ensure_min_children,
    _title_core,
)

# 真实生产项目里的一条一级章标题（`_DOMAIN_KEYWORDS` 对其完全无感）
CHAPTER_HANG = "悬挑花篮架与盘扣支撑体系方案"
CHAPTER_TEST = "试验检验与计量管理计划"
L2_DIMS = ["总体方案与实施策划", "分阶段推进计划", "资源配置与协调保障"]


def _tree(chapter_title: str, l2_titles: list[str], l3_by_l2: dict[str, list[str]]):
    return [
        {
            "title": chapter_title,
            "level": 1,
            "children": [
                {
                    "title": l2,
                    "level": 2,
                    "children": [
                        {"title": t, "level": 3, "children": []} for t in l3_by_l2.get(l2, [])
                    ],
                }
                for l2 in l2_titles
            ],
        }
    ]


def _iter_titles(nodes, level=1):
    for n in nodes:
        if not isinstance(n, dict):
            continue
        yield level, n.get("title") or ""
        yield from _iter_titles(n.get("children") or [], level + 1)


# ===========================================================================
# B1：清洗器过度剥离 —— 剥后缀保主体
# ===========================================================================
class TestSuffixStripKeepsBody:
    def test_specific_title_with_collision_suffix_keeps_body(self):
        """「悬挑架安装（2）」只该剥掉后缀，主体是项目具体标题。"""
        tree = _tree(CHAPTER_HANG, ["分阶段推进计划"], {"分阶段推进计划": ["悬挑架安装（2）"]})
        fixed = _cleanup_placeholder_titles(tree)
        result = tree[0]["children"][0]["children"][0]["title"]
        assert fixed == 1
        assert result == "悬挑架安装"
        assert not _is_dirty_l3_title_deep(result)

    def test_halfwidth_suffix_also_stripped(self):
        tree = _tree(CHAPTER_HANG, ["分阶段推进计划"], {"分阶段推进计划": ["花篮式锚固检查(3)"]})
        _cleanup_placeholder_titles(tree)
        assert tree[0]["children"][0]["children"][0]["title"] == "花篮式锚固检查"

    def test_generic_body_is_still_rewritten(self):
        """主体本身泛化（「总体实施策划」）→ 仍必须整体重写为语义化标题。"""
        tree = _tree(CHAPTER_HANG, ["分阶段推进计划"], {"分阶段推进计划": ["总体实施策划（2）"]})
        _cleanup_placeholder_titles(tree)
        result = tree[0]["children"][0]["children"][0]["title"]
        assert not _is_dirty_l3_title_deep(result)
        assert result != "总体实施策划"

    def test_strip_does_not_collide_with_existing_sibling(self):
        """剥后缀若与同父兄弟撞名，必须改走重写而不是产出重复标题。"""
        tree = _tree(
            CHAPTER_HANG,
            ["分阶段推进计划"],
            {"分阶段推进计划": ["悬挑架安装", "悬挑架安装（2）"]},
        )
        _cleanup_placeholder_titles(tree)
        titles = [c["title"] for c in tree[0]["children"][0]["children"]]
        assert len(set(titles)) == 2, titles

    def test_cleanup_is_idempotent_after_strip(self):
        tree = _tree(CHAPTER_HANG, ["分阶段推进计划"], {"分阶段推进计划": ["悬挑架安装（2）"]})
        assert _cleanup_placeholder_titles(tree) == 1
        assert _cleanup_placeholder_titles(tree) == 0


# ===========================================================================
# B2：本章实体词优先 —— 消除跨章实体错配
# ===========================================================================
class TestChapterScopedEntities:
    def test_fragments_extracted_from_chapter_title(self):
        frags = _title_entity_fragments(CHAPTER_HANG)
        assert "悬挑花篮架" in frags
        assert "盘扣支撑体系" in frags

    def test_fragments_strip_chapter_level_noise_suffixes(self):
        frags = _title_entity_fragments(CHAPTER_TEST)
        # 「管理计划」是章名级虚词，实体应是「试验检验」/「计量管理」
        assert "试验检验" in frags
        assert all(not f.endswith("方案") for f in frags)

    def test_fragments_ignores_numbering_prefix(self):
        assert _title_entity_fragments("第三章 悬挑花篮架与盘扣支撑体系方案") == (
            _title_entity_fragments(CHAPTER_HANG)
        )

    def test_chapter_entities_come_before_global_pool(self):
        """域判定失败的章（generic→work 池）也必须先拿到本章实体。"""
        cands = _l3_entity_candidates("总体方案与实施策划", 0, set(), context_title=CHAPTER_HANG)
        assert cands, "候选列表不应为空"
        assert any("悬挑花篮架" in c for c in cands[:6]), cands[:6]

    def test_legacy_pool_entities_not_used_when_chapter_entity_exists(self):
        """同一章的多个候选应落在本章实体上，而不是 work 池的「地下室」等。"""
        cands = _l3_entity_candidates(
            "分阶段推进计划", 0, set(), context_title=CHAPTER_HANG
        )
        chapter_scoped = [c for c in cands[:4] if "悬挑花篮架" in c or "盘扣支撑体系" in c]
        assert len(chapter_scoped) >= 3, cands[:4]

    def test_fallback_without_context_still_never_dirty(self):
        """无祖级上下文（旧调用形态）时行为退化为域池，但不得产出脏标题。"""
        cands = _l3_entity_candidates("总体方案与实施策划", 0, set(), context_title="")
        assert all(not _is_dirty_l3_title_deep(c) for c in cands)


# ===========================================================================
# B3：跨章成套复用护栏
# ===========================================================================
class TestCrossChapterReuseCap:
    GENERIC_SET = ["施工工艺流程与要点", "常见问题与防治措施", "成品保护与验收要求"]

    def _mass_tree(self, chapters: int):
        tree = []
        for i in range(chapters):
            tree.append(
                {
                    "title": f"第{i}章 分项工程{i}号施工方法",
                    "level": 1,
                    "children": [
                        {
                            "title": l2,
                            "level": 2,
                            "children": [
                                {"title": t, "level": 3, "children": []}
                                for t in self.GENERIC_SET
                            ],
                        }
                        for l2 in L2_DIMS
                    ],
                }
            )
        return tree

    def test_mass_repetition_is_capped(self):
        tree, changed = _cap_cross_chapter_repetition(self._mass_tree(10), max_reuse=3)
        assert changed > 0
        # 每个原始泛化标题最多保留 3 个父级
        for t in self.GENERIC_SET:
            occurrences = sum(
                1
                for level, title in _iter_titles(tree)
                if level == 3 and title == t
            )
            assert occurrences <= 3, f"{t} 仍出现 {occurrences} 次"
        assert changed == 10 * len(L2_DIMS) * 3 - sum(
            1 for level, title in _iter_titles(tree)
            if level == 3 and title in self.GENERIC_SET
        )

    def test_rewritten_titles_are_clean_and_chapter_scoped(self):
        tree, changed = _cap_cross_chapter_repetition(self._mass_tree(6), max_reuse=3)
        assert changed > 0
        for level, title in _iter_titles(tree):
            if level == 3:
                assert title and not _is_dirty_l3_title_deep(title), title
                assert "（" not in title and "(" not in title, f"不应产出括号编号后缀: {title}"

    def test_guard_is_idempotent(self):
        tree = self._mass_tree(6)
        _cap_cross_chapter_repetition(tree, max_reuse=3)
        snapshot = [t for lv, t in _iter_titles(tree) if lv == 3]
        again, changed = _cap_cross_chapter_repetition(tree, max_reuse=3)
        assert changed == 0
        assert [t for lv, t in _iter_titles(again) if lv == 3] == snapshot

    def test_legitimate_small_reuse_untouched(self):
        """同一标题出现在 2~3 个章是标书合法形态（2026-10-11 政策），不得误伤。"""
        tree = [
            {
                "title": f"第{i}章 施工方法",
                "level": 1,
                "children": [
                    {
                        "title": "总体方案与实施策划",
                        "level": 2,
                        "children": [{"title": "施工工艺流程与要点", "level": 3, "children": []}],
                    }
                ],
            }
            for i in range(3)
        ]
        _, changed = _cap_cross_chapter_repetition(tree, max_reuse=3)
        assert changed == 0
        assert all(
            t == "施工工艺流程与要点" for lv, t in _iter_titles(tree) if lv == 3
        )

    def test_max_reuse_zero_disables_guard_for_backward_compat(self):
        tree = self._mass_tree(4)
        _, changed = _cap_cross_chapter_repetition(tree, max_reuse=0)
        assert changed == 0

    def test_only_applies_to_level3_and_below(self):
        """一级/二级是用户正式章节名，护栏一律不动。"""
        tree = self._mass_tree(5)
        l1_before = [t for lv, t in _iter_titles(tree) if lv == 1]
        l2_before = [t for lv, t in _iter_titles(tree) if lv == 2]
        _cap_cross_chapter_repetition(tree, max_reuse=3)
        assert [t for lv, t in _iter_titles(tree) if lv == 1] == l1_before
        assert [t for lv, t in _iter_titles(tree) if lv == 2] == l2_before


# ===========================================================================
# B4：消歧产物的脏标题复检
# ===========================================================================
class TestUniquifyDirtyRecheck:
    def test_disambiguated_titles_are_not_dirty(self):
        """同级多个「总体实施策划」：旧实现产出「总体实施策划专项方案」仍泛化。"""
        tree = [
            {
                "title": "项目管理机构与资源配置",
                "level": 1,
                "children": [
                    {
                        "title": "总体实施策划",
                        "level": 2,
                        "children": [
                            {"title": "总体实施策划", "level": 3, "children": []}
                            for _ in range(3)
                        ],
                    }
                ],
            }
        ]
        _uniquify_outline_titles(tree)
        titles = [c["title"] for c in tree[0]["children"][0]["children"]]
        assert len(set(titles)) == 3, titles
        # 去重只负责改名「重复」的那几个，首个占位标题的清洗属 `_cleanup_
        # placeholder_titles` 的职责（终态由 C1 的链路用例整体把关）。这里断言
        # 的是 B4 本身：消歧产物不得仍是泛化标题。
        for t in titles:
            if t != "总体实施策划":
                assert not _is_dirty_l3_title_deep(t), t

    def test_no_ordinal_suffix_produced(self):
        tree = [
            {
                "title": "绿色施工与环保技术措施",
                "level": 1,
                "children": [
                    {
                        "title": "分阶段推进计划",
                        "level": 2,
                        "children": [
                            {"title": "分阶段推进计划", "level": 3, "children": []}
                            for _ in range(4)
                        ],
                    }
                ],
            }
        ]
        _uniquify_outline_titles(tree)
        for t in [c["title"] for c in tree[0]["children"][0]["children"]]:
            assert "（" not in t and "(" not in t, t


# ===========================================================================
# B5：`_ensure_min_children` 的 used / 祖级上下文接线
# ===========================================================================
class TestEnsureMinChildrenWiring:
    def test_new_l3_set_is_not_copied_across_chapters(self):
        """旧实现传 set() 当 used → 不同章新建的 L3 成套同名。"""
        tree = [
            {"title": CHAPTER_HANG, "level": 1, "children": []},
            {"title": CHAPTER_TEST, "level": 1, "children": []},
            {"title": "临时用电与消防系统专项方案", "level": 1, "children": []},
        ]
        out = _ensure_min_children(tree, min_level2=3, min_level3=5)
        l3 = [t for lv, t in _iter_titles(out) if lv == 3]
        assert l3, "应生成三级标题"
        assert len(l3) == len(set(l3)), "补建的三级标题必须全局唯一（跨章不成套复制）"

    def test_new_l3_inherits_chapter_entities(self):
        """不传 grandparent 时 L2 恒为通用维度名 → 拿不到本章实体词。"""
        tree = [{"title": CHAPTER_HANG, "level": 1, "children": []}]
        out = _ensure_min_children(tree, min_level2=3, min_level3=5)
        l3 = [t for lv, t in _iter_titles(out) if lv == 3]
        assert any("悬挑花篮架" in t or "盘扣支撑体系" in t for t in l3), l3

    def test_placeholder_l3_fill_uses_global_used(self):
        """已有空标题占位节点的补全同样不得跨章撞名。"""
        tree = [
            {
                "title": title,
                "level": 1,
                "children": [
                    {
                        "title": l2,
                        "level": 2,
                        "children": [{"title": "", "level": 3, "children": []} for _ in range(3)],
                    }
                    for l2 in L2_DIMS
                ],
            }
            for title in (CHAPTER_HANG, CHAPTER_TEST)
        ]
        out = _ensure_min_children(tree, min_level2=3, min_level3=3)
        l3 = [t for lv, t in _iter_titles(out) if lv == 3]
        assert all(l3), "占位空标题应被补全"
        assert len(l3) == len(set(l3)), l3


# ===========================================================================
# C1：新配置项默认值与向后兼容
# ===========================================================================
class TestNewConfig:
    def test_default_cap_is_three(self):
        """默认 3：合法的小范围复用不受影响，成套复制被收口。"""
        assert OUTLINE_L3_MAX_PARENT_REUSE == 3

    def test_end_to_end_chain_leaves_no_dirty_or_suffixed_titles(self):
        """清洗 → 去重 → 护栏 → 再清洗：终态既无脏标题也无跨章成套复制。"""
        tree = [
            {
                "title": f"第{i}章 悬挑花篮架与盘扣支撑体系方案",
                "level": 1,
                "children": [
                    {
                        "title": l2,
                        "level": 2,
                        "children": [
                            {"title": t, "level": 3, "children": []}
                            for t in ("总体实施策划（2）", "专项6", "施工工艺流程与要点")
                        ],
                    }
                    for l2 in L2_DIMS
                ],
            }
            for i in range(8)
        ]
        _cleanup_placeholder_titles(tree)
        tree = _uniquify_outline_titles(tree)
        tree, _ = _cap_cross_chapter_repetition(tree, OUTLINE_L3_MAX_PARENT_REUSE)
        _cleanup_placeholder_titles(tree)

        l3 = [t for lv, t in _iter_titles(tree) if lv == 3]
        for t in l3:
            assert t and not _is_dirty_l3_title_deep(t), t
            assert "（2）" not in t and "（3）" not in t, t
        from collections import Counter

        reuse = Counter(_title_core(t) for t in l3)
        assert all(v <= OUTLINE_L3_MAX_PARENT_REUSE for v in reuse.values()), reuse.most_common(3)
