"""2026-10-15 生成大纲模块第五轮加固回归：泛化词干 × 维度词组合治理。

症状来源（用户反馈，本轮复现）：「三级标题大量是占位式泛化名称（如『专项6』
『总体实施策划（2）』『总体实施策划（3）』），且存在编号冲突后缀」。

第四轮（B1~B5）拦住了两个原始家族，但本轮用
backend/scripts/diag_l3_leaks.py 在生产形态的多章骨架上复跑，发现**同一症状
换了三副新面孔**，且全部逃过既有判定：

  D1 泛化词干 × 维度词组合漏判
     「总体实施策划专项方案」「总体实施策划补充措施」「总体实施策划实施方案」
     「总体方案施工工艺」「实施策划质量控制」「实施策划记录归档」
     —— `_GENERIC_ONLY_TITLES`/`_L3_L2LIKE_TITLES` 只按「恰好等于」比对，
        加一个维度词就脱网，于是 2026-10-15 的「消歧产物脏复检」形同虚设。
     修复：`_is_generic_stem_only_title`（形态 A 词干+单维度词 / 形态 B 连续
     堆叠词干）+ `_is_dirty_l3_title` 第 8 条规则。

  D2 `_title_entity_fragments` 未过滤泛化段 + `_parent_tokens` 碎片前缀
     章名本身泛化（「总体方案与实施策划」）时，「总体方案」「实施策划」被当成
     本章专属实体与维度词拼接；`_parent_tokens` 的剩余片段分支还会把父级裁成
     「方案与实」这类无意义 2 字碎片。
     修复：`_is_generic_entity_fragment` 过滤（宽口径，仅用于实体过滤）+
     `_disambiguate_title` 前缀完整性校验。

  D3 `repair_l3_generic_titles` 空标题永久泄漏 + 配额泄漏
     `if not l3_title: continue` 静默跳过空标题节点（比泛化标题更严重的缺陷
     无人修复）；且 `warnings[:max_repair]` 先切片再 `continue`，被跳过的条目
     仍消耗配额 → 靠后位置的脏标题永远进不了任务队列。
     修复：空标题走规则兜底直修；配额改为按可执行任务计。

  C2 新配置（默认开启，均可关闭回退历史行为）：
     OUTLINE_L3_GENERIC_STEM_GUARD_ENABLED / OUTLINE_L3_REPAIR_QUOTA_BY_TASK。

运行：
  cd backend && python -m pytest tests/test_l3_generic_stem_guard_2026_10_15.py -q -p no:cacheprovider
"""

import asyncio
import re

import pytest

from app.config import (
    OUTLINE_L3_GENERIC_STEM_GUARD_ENABLED,
    OUTLINE_L3_MAX_PARENT_REUSE,
    OUTLINE_L3_REPAIR_MAX,
    OUTLINE_L3_REPAIR_QUOTA_BY_TASK,
)
from app.services.ai import outline_common
from app.services.ai.outline_common import (
    _cap_cross_chapter_repetition,
    _cleanup_placeholder_titles,
    _cn_ordinal,
    _disambiguate_title,
    _ensure_min_children,
    _is_dirty_l3_title,
    _is_dirty_l3_title_deep,
    _is_generic_entity_fragment,
    _is_generic_stem_only_title,
    _title_core,
    _title_entity_fragments,
    _uniquify_outline_titles,
)
from app.services.ai.workflows_outline_utils import repair_l3_generic_titles
from app.services.ai.workflows_outline_v51 import _auto_fix_issues, _quick_local_check

# 生产项目里真实存在的两类章标题：泛化章名 / 专业章名
CHAPTER_GENERIC = "总体方案与实施策划"
CHAPTER_RES = "资源配置与协调保障"
CHAPTER_QUALITY = "工程质量保证措施"
CHAPTER_HANG = "悬挑花篮架与盘扣支撑体系方案"



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
                        {"title": t, "level": 3, "children": []}
                        for t in l3_by_l2.get(l2, [])
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


def _multi_chapter_tree(n_chapters: int, l3_count: int) -> list[dict]:
    """生产形态：多个章共享同一批泛化 L2/L3 骨架（清洗前状态）。"""
    return [
        {
            "title": f"第{i}章 {CHAPTER_GENERIC}",
            "level": 1,
            "children": [
                {
                    "title": l2,
                    "level": 2,
                    "children": [
                        {"title": "总体实施策划", "level": 3, "children": []}
                        for _ in range(l3_count)
                    ],
                }
                for l2 in ("总体方案与实施策划", "分阶段推进计划")
            ],
        }
        for i in range(1, n_chapters + 1)
    ]


# ===========================================================================
# C2：新配置项默认值与向后兼容开关
# ===========================================================================
class TestConfigBackwardCompat:
    def test_defaults_are_enabled(self):
        assert OUTLINE_L3_GENERIC_STEM_GUARD_ENABLED is True
        assert OUTLINE_L3_REPAIR_QUOTA_BY_TASK is True
        # 第四轮护栏保持默认开启，不与本轮冲突；2026-10-27 收紧至 2
        assert OUTLINE_L3_MAX_PARENT_REUSE == 2
        assert OUTLINE_L3_REPAIR_MAX >= 1

    @pytest.mark.parametrize("dirty_case", [
        "总体实施策划专项方案",
        "总体实施策划补充措施",
        "总体方案施工工艺",
        "实施策划质量控制",
        "总体方案总体实施策划专项方案",
    ])
    def test_guard_disabled_falls_back_to_legacy(self, dirty_case, monkeypatch):
        """开关关闭 = 完全回退历史行为：泛化词干组合规则不再生效。"""
        monkeypatch.setattr(
            "app.config.OUTLINE_L3_GENERIC_STEM_GUARD_ENABLED", False
        )
        assert _is_generic_stem_only_title(dirty_case) is False
        assert _is_dirty_l3_title_deep(dirty_case) is False

    def test_guard_disabled_keeps_legacy_rules_intact(self, monkeypatch):
        """回退历史行为只关闭新规则，历史规则（专项N / 裸编号后缀）不受影响。"""
        monkeypatch.setattr(
            "app.config.OUTLINE_L3_GENERIC_STEM_GUARD_ENABLED", False
        )
        assert _is_dirty_l3_title_deep("专项6") is True
        assert _is_dirty_l3_title_deep("总体实施策划") is True
        assert _is_dirty_l3_title_deep("总体实施策划（2）") is True


def _l3_tree(titles: list[str]) -> list[dict]:
    return _tree("第1章 总体施工部署", ["总体方案与实施策划"],
                 {"总体方案与实施策划": titles})


# ===========================================================================
# D1：泛化词干 × 维度词组合判定器
# ===========================================================================
class TestGenericStemDetector:
    @pytest.mark.parametrize("title", [
        # 形态 A：泛化词干 + 单个泛化维度词
        "总体实施策划专项方案",
        "总体实施策划补充措施",
        "总体实施策划实施方案",
        "总体实施策划保障措施",
        "总体方案施工工艺",
        "实施策划质量控制",
        "实施策划记录归档",
        "总体方案检测试验",
        "实施策划环境保护",
        "工程质量保证检测试验",
        "总体方案与实施策划质量控制",
    ])
    def test_hits_generic_stem_times_dimension(self, title):
        assert _is_generic_stem_only_title(title) is True
        assert _is_dirty_l3_title(title) is True
        assert _is_dirty_l3_title_deep(title) is True

    @pytest.mark.parametrize("title", [
        # 形态 B：连续堆叠多个泛化词干
        "总体方案总体实施策划专项方案",
        "总体方案总体实施策划补充措施",
    ])
    def test_hits_stacked_generic_stems(self, title):
        assert _is_generic_stem_only_title(title) is True
        assert _is_dirty_l3_title_deep(title) is True

    @pytest.mark.parametrize("title", [
        # 含项目实体的合法标题 —— 绝不能误伤
        "混凝土施工工艺",
        "样板区质量控制",
        "见证取样要点",
        "旁站监理细则",
        "装配式验收标准",
        "机电管线安全防护",
        "工序交接流程5",
        "第一章施工部署",
        "第一章 施工部署",
        "专项方案编制与评审",
        "施工工艺流程与要点",
        "工程质量保证与创优计划",
        "总体技术路线与施工部署",
        "总体平面布置",
    ])
    def test_no_false_positive_on_concrete_titles(self, title):
        assert _is_generic_stem_only_title(title) is False, title
        assert _is_dirty_l3_title(title) is False, title

    def test_chinese_ordinal_disambiguated_titles_are_exempt(self):
        """「X之N」是消歧兜底产物，判脏会与其构成互斥循环 → 刻意豁免。"""
        assert _is_generic_stem_only_title("总体实施策划之一") is False
        assert _is_generic_stem_only_title("总体实施策划之三") is False
        assert _is_dirty_l3_title_deep("总体实施策划之三") is False

    @pytest.mark.parametrize("title", ["", None, "  "])
    def test_empty_and_non_str_input_is_safe(self, title):
        assert _is_generic_stem_only_title(title) is False

    @pytest.mark.parametrize("title", [
        "总体实施策划(2)",
        "总体实施策划（2）",
        "总体实施策划专项6",
        "总体实施策划(六)",
    ])
    def test_history_variants_still_dirty(self, title):
        assert _is_dirty_l3_title_deep(title) is True



# ===========================================================================
# D2：本章实体抽取过滤泛化段 + 消歧前缀完整性
# ===========================================================================
class TestTitleEntityFragmentsFilter:
    def test_generic_chapter_yields_no_entity(self):
        """泛化章名不得产出任何「实体」—— 否则拼出『总体方案施工工艺』。"""
        assert _title_entity_fragments(CHAPTER_GENERIC) == ()
        assert _title_entity_fragments(CHAPTER_RES) == ()
        assert _title_entity_fragments(CHAPTER_QUALITY) == ()
        assert _title_entity_fragments("第3章 总体方案与实施策划") == ()

    def test_professional_chapter_keeps_entities(self):
        """专业章名必须继续产出本章实体词（第四轮 B2 的能力不能被回退）。"""
        frags = _title_entity_fragments(CHAPTER_HANG)
        assert "悬挑花篮架" in frags
        assert "盘扣支撑体系" in frags
        assert all(not _is_generic_entity_fragment(f) for f in frags), frags

    @pytest.mark.parametrize("frag", [
        "总体方案", "实施策划", "资源配置", "协调保障", "总体实施策划",
        "分阶段推进", "阶段推进", "推进计划", "施工组织", "工程质量保证",
    ])
    def test_generic_fragments_are_filtered(self, frag):
        assert _is_generic_entity_fragment(frag) is True

    @pytest.mark.parametrize("frag", [
        "悬挑花篮架", "盘扣支撑体系", "试验检验", "计量管理",
        "装配式", "机电管线", "桩基", "装配式验收",
    ])
    def test_professional_fragments_pass(self, frag):
        assert _is_generic_entity_fragment(frag) is False

    def test_generic_guard_disabled_restores_entities(self, monkeypatch):
        """关闭守卫后回退历史行为：泛化章名重新产出泛化片段。"""
        monkeypatch.setattr(
            "app.config.OUTLINE_L3_GENERIC_STEM_GUARD_ENABLED", False
        )
        assert _title_entity_fragments(CHAPTER_GENERIC) != ()
        assert _is_generic_entity_fragment("总体方案") is False


class TestDisambiguateGenericSubject:
    def test_generic_subject_does_not_get_generic_dimension(self):
        """泛化主体不再追加维度词（旧产物：总体实施策划专项方案）。"""
        seen: set[str] = set()
        for _ in range(6):
            t = _disambiguate_title("总体实施策划", seen, parent_title=CHAPTER_GENERIC)
            assert not _is_dirty_l3_title_deep(t), t
            seen.add(_title_core(t))

    def test_generic_subject_uses_semantic_pool(self):
        """泛化主体改走主体专属语义池，产出「总体施工技术交底」这类可读标题。"""
        seen: set[str] = set()
        for _ in range(6):
            t = _disambiguate_title("总体实施策划", seen, parent_title=CHAPTER_GENERIC)
            assert not _is_dirty_l3_title_deep(t), t
            # 不退化成「总体实施策划之N」纯序数后缀
            assert not t.startswith("总体实施策划之"), t
            seen.add(_title_core(t))

    def test_generic_subject_pool_exhausts_to_ordinal(self):
        """语义池耗尽后才退回中文序数（唯一性优先于语义密度）。"""
        seen: set[str] = set()
        titles = []
        for _ in range(40):
            t = _disambiguate_title("总体实施策划", seen, parent_title=CHAPTER_GENERIC)
            titles.append(t)
            seen.add(_title_core(t))
        assert len(set(titles)) == 40, "40 个重复主体必须全部消歧唯一"

    def test_chinese_ordinal_of_generic_stem_is_exempt(self):
        """「X之N」序数消歧产物必须被泛化判定豁免。

        否则 `_disambiguate_title` 的分支 3 会产出被判定为脏的标题，
        与 `_uniquify_outline_titles` 的消歧产物脏复检构成互斥死循环。
        """
        for t in ("总体实施策划之二", "总体方案之三", "分阶段推进计划之四"):
            assert _is_generic_stem_only_title(t) is False, t
            assert _is_dirty_l3_title_deep(t) is False, t

    def test_parent_fragment_prefix_is_not_truncated(self):
        """「总体方案与实施策划」不得被裁成「方案与实」这类碎片前缀。"""
        seen: set[str] = set()
        for _ in range(6):
            t = _disambiguate_title("总体实施策划", seen, parent_title=CHAPTER_GENERIC)
            assert not re.match(r"^(方案与|与实)", t), t
            seen.add(_title_core(t))

    def test_concrete_subject_still_uses_dimension_branch(self):
        """专业主体的消歧仍然走维度词分支（保留既有行为，不降级质量）。"""
        t = _disambiguate_title(
            "基坑支护施工工艺", set(), parent_title="地下室与基坑支护"
        )
        assert not _is_dirty_l3_title_deep(t), t
        assert "基坑支护施工工艺" in t, t

    def test_uniquify_only_renames_duplicates_policy(self):
        """政策约束：uniquify 只改名「重复」的兄弟，同级唯一标题保持不动。

        同级唯一的泛化标题（如「总体实施策划」）由 `_cleanup_placeholder_titles`
        负责改写 —— 生产链路顺序恒为「清洗 → 去重」，见 TestProdChainEndToEnd。
        """
        tr = _tree("第1章 总体方案与实施策划", ["总体方案与实施策划"],
                   {"总体方案与实施策划": ["地下室施工工艺", "样板区质量控制"]})
        before = [c["title"] for c in tr[0]["children"][0]["children"]]
        _uniquify_outline_titles(tr)
        after = [c["title"] for c in tr[0]["children"][0]["children"]]
        assert before == after, "同级唯一的具体标题不得被改动"

    def test_uniquify_dedupes_dirty_duplicates_without_dirty_output(self):
        """重复的泛化标题被改名后，产物不得是脏标题。"""
        tr = _tree("第1章 总体方案与实施策划", ["总体方案与实施策划"],
                   {"总体方案与实施策划": ["总体实施策划"] * 5})
        _uniquify_outline_titles(tr)
        titles = [c["title"] for c in tr[0]["children"][0]["children"]]
        assert len(set(titles)) == 5, titles
        # 被改名（重复项）的产物一律不是脏标题
        assert not [t for t in titles[1:] if _is_dirty_l3_title_deep(t)], titles

    def test_cleanup_then_uniquify_yields_no_dirty_titles(self):
        """生产链路顺序（清洗 → 去重）下，最终标题具体、唯一、无编号冲突。"""
        tr = _multi_chapter_tree(6, 5)
        _cleanup_placeholder_titles(tr)
        _uniquify_outline_titles(tr)
        l3 = [t for lv, t in _iter_titles(tr) if lv == 3]
        assert all(t.strip() for t in l3), [t for t in l3 if not t.strip()]
        assert not [t for t in l3 if _is_dirty_l3_title_deep(t)], l3
        for ch in tr:
            for l2 in ch["children"]:
                t3 = [c["title"] for c in l2["children"]]
                assert len(set(t3)) == len(t3), t3



class _EchoProvider:
    """AI 复读占位标题的探针：强制走规则兜底路径（不依赖真实 AI 服务）。"""

    async def chat(self, messages, **kwargs):
        return "专项7"


async def _repair(tree: list[dict], max_repair: int = 24) -> list[dict]:
    return await repair_l3_generic_titles(
        _EchoProvider(), tree, "", "", "", "", "", max_repair=max_repair
    )


# ===========================================================================
# D3：修复配额按可执行任务计 + 空标题不再跳过
# ===========================================================================
class TestRepairQuotaAndEmptyTitle:
    def test_empty_titles_are_repaired_not_skipped(self):
        """空标题（比泛化更严重的缺陷）必须被修复，不得静默跳过。"""
        tr = _l3_tree(["", "", "总体实施策划（2）", "专项6", "总体实施策划(2)"])
        out = asyncio.run(_repair(tr, max_repair=10))
        titles = [c["title"] for c in out[0]["children"][0]["children"]]
        assert all(t.strip() for t in titles), f"仍存在空标题: {titles}"
        assert not [t for t in titles if _is_dirty_l3_title_deep(t)], titles

    def test_quota_counts_only_executable_tasks(self, monkeypatch):
        """空标题不占配额：配额只约束需要修复逻辑的任务。"""
        monkeypatch.setattr("app.config.OUTLINE_L3_REPAIR_QUOTA_BY_TASK", True)
        tr = _l3_tree(["", "", "总体实施策划（2）", "专项6"])
        out = asyncio.run(_repair(tr, max_repair=3))
        titles = [c["title"] for c in out[0]["children"][0]["children"]]
        # 变更（2026-10-26 空标题配额豁免）：2 个空标题走纯规则兜底、不占配额；
        # 2 个可执行任务 ≤ 配额 3 → 本轮到第 4 个节点仍能被修复。
        assert all(t.strip() for t in titles), titles
        assert not [t for t in titles if _is_dirty_l3_title_deep(t)], titles

        # 配额对「可执行任务」仍然生效：4 个泛化标题 + 配额 2 → 仅前 2 个修复
        tr2 = _l3_tree(["专项1", "专项2", "专项3", "专项4"])
        out2 = asyncio.run(_repair(tr2, max_repair=2))
        titles2 = [c["title"] for c in out2[0]["children"][0]["children"]]
        assert len([t for t in titles2 if _is_dirty_l3_title_deep(t)]) == 2, titles2

    def test_quota_backwards_compat_flag(self, monkeypatch):
        """关闭新口径 = 回退历史「按告警序号截断」，但空标题仍必须修复。"""
        monkeypatch.setattr("app.config.OUTLINE_L3_REPAIR_QUOTA_BY_TASK", False)
        tr = _l3_tree(["", "", "总体实施策划（2）", "专项6"])
        out = asyncio.run(_repair(tr, max_repair=3))
        titles = [c["title"] for c in out[0]["children"][0]["children"]]
        # 2026-10-26：空标题配额豁免对两种口径都生效（否则空标题节点会在
        # normalize_outline_items 落库时被删除，造成目录缺节）
        assert titles[0].strip() and titles[1].strip(), titles
        assert not _is_dirty_l3_title_deep(titles[2]), titles
        # 历史截断口径保留：第 4 个（下标 3）超出 max_repair=3 的告警序号窗口
        assert titles[3] == "专项6", titles

    def test_all_dirty_are_eventually_covered(self):
        """大目录（> max_repair）经多轮修复后不再残留脏标题。"""
        tr = _l3_tree(["总体实施策划"] * 8 + ["专项6"] * 4)
        out = tr
        for _ in range(3):
            out = asyncio.run(_repair(out, max_repair=5))
        titles = [c["title"] for c in out[0]["children"][0]["children"]]
        assert not [t for t in titles if _is_dirty_l3_title_deep(t)], titles

    def test_empty_title_fallback_uses_parent_semantics(self):
        """空标题的兜底标题应来自父级语义，且不带任何编号后缀。"""
        tr = _l3_tree(["", ""])
        out = asyncio.run(_repair(tr, max_repair=5))
        titles = [c["title"] for c in out[0]["children"][0]["children"]]
        for t in titles:
            assert t.strip(), t
            assert not re.search(r"[（(]\s*\d+\s*[）)]", t), t
            assert "专项" not in t, t


# ===========================================================================
# 端到端：生产形态多章骨架（对应用户反馈的症状）
# ===========================================================================
class TestProdChainEndToEnd:
    def test_no_placeholder_generic_l3_across_chapters(self):
        """6 章 × 2 L2 × 5 L3 的泛化骨架，经完整治理链后不得残留占位泛化标题。"""
        from collections import Counter

        tr = _multi_chapter_tree(6, 5)
        _cleanup_placeholder_titles(tr)
        _uniquify_outline_titles(tr)
        tr, _n = _cap_cross_chapter_repetition(tr, OUTLINE_L3_MAX_PARENT_REUSE)
        _cleanup_placeholder_titles(tr)
        l3 = [t for lv, t in _iter_titles(tr) if lv == 3]
        assert l3, "应存在三级标题"
        # 1) 无空标题
        assert all(t.strip() for t in l3), [t for t in l3 if not t.strip()]
        # 2) 无脏标题（占位 / 泛化词干×维度词）
        dirty = [t for t in l3 if _is_dirty_l3_title_deep(t)]
        assert not dirty, dirty[:10]
        # 3) 无「（N）」式编号冲突后缀
        assert not [t for t in l3 if re.search(r"[（(]\s*\d+\s*[）)]", t)], l3
        # 4) 跨章成套复制被护栏收口
        counts = Counter(_title_core(t) for t in l3)
        assert all(v <= OUTLINE_L3_MAX_PARENT_REUSE for v in counts.values()), (
            counts.most_common(5)
        )

    def test_chain_is_idempotent(self):
        """重复执行治理链不得二次改名（幂等）。"""
        tr = _multi_chapter_tree(4, 4)
        _cleanup_placeholder_titles(tr)
        _uniquify_outline_titles(tr)
        first = [t for lv, t in _iter_titles(tr) if lv >= 2]
        _cleanup_placeholder_titles(tr)
        _uniquify_outline_titles(tr)
        second = [t for lv, t in _iter_titles(tr) if lv >= 2]
        assert first == second

    def test_ensure_min_children_fills_generic_skeleton_concretely(self):
        """L3 骨架为空时补齐结果必须是具体标题，不得是泛化词干×维度词。"""
        tr = _tree("第1章 工程质量保证措施", ["总体方案与实施策划"], {})
        tr[0]["children"][0]["children"] = [
            {"title": "", "level": 3, "children": []} for _ in range(5)
        ]
        _ensure_min_children(tr, min_level2=2, min_level3=5)
        l3 = [c["title"] for c in tr[0]["children"][0]["children"]]
        assert len(l3) >= 5
        assert all(t.strip() for t in l3), l3
        assert not [t for t in l3 if _is_dirty_l3_title_deep(t)], l3
        assert len(set(l3)) == len(l3), l3



# ===========================================================================
# D8：_quick_local_check 脏标题兜底检测 + _auto_fix_issues 一并修复
# ===========================================================================
class TestQuickLocalCheckAndAutofix:
    @staticmethod
    def _tree_with_ids():
        return [
            {
                "title": "第1章 总体方案与实施策划",
                "id": "1",
                "level": 1,
                "children": [
                    {
                        "title": "总体方案与实施策划",
                        "id": "1.1",
                        "level": 2,
                        "children": [
                            {"title": "总体实施策划（2）", "id": "1.1.1", "level": 3,
                             "children": []},
                            {"title": "", "id": "1.1.2", "level": 3, "children": []},
                        ],
                    }
                ],
            }
        ]

    def test_local_check_reports_both_dirty_and_empty(self):
        """脏标题（同级唯一、非空）与空标题必须都被检出。"""
        issues = _quick_local_check(self._tree_with_ids())
        assert any(i.startswith("占位标题: 1/1.1/1.1.1") for i in issues), issues
        assert any(i.startswith("空标题: 1/1.1/1.1.2") for i in issues), issues

    def test_auto_fix_repairs_dirty_and_empty_titles(self):
        """两类缺陷都得到具体标题，且不产生新的空壳标题。"""
        tr = self._tree_with_ids()
        tr = _auto_fix_issues(tr, _quick_local_check(tr))
        titles = [c["title"] for c in tr[0]["children"][0]["children"]]
        assert len(titles) == 2
        for t in titles:
            assert t.strip(), t
            assert not _is_dirty_l3_title_deep(t), t

    def test_l1_l2_titles_are_not_flagged_as_placeholder(self):
        """一/二级是用户的正式章节名，泛化不得判为占位（否则会破坏目录结构）。"""
        tr = self._tree_with_ids()
        issues = _quick_local_check(tr)
        assert not [i for i in issues if i.startswith("占位标题: 1/")
                    and i.split("/")[1] != "1.1"], issues
        assert not [i for i in issues if i.startswith("占位标题: 1 ")], issues


# ===========================================================================
# D6：中文序数与「之101」形态收口
# ===========================================================================


class TestCnOrdinalPureChinese:
    """`_cn_ordinal` 全区间必须输出纯中文。

    2026-10-15 加固背景：旧实现在 n>=100 时直接 `return str(n)`，而三处消歧
    兜底的起点恰好都落在 100 以上（`_disambiguate_title` 收口写死 101、
    `_uniquify_outline_titles` 与 `build_chapter7_framework` 的防御式循环均从
    `_guard = 101` 起步）。于是「总体实施策划」这类同级重名条目较多时会产出
    「总体实施策划之101」—— 阿拉伯数字混进中文标题，且形态与「（2）」裸编号
    同类，等于兜底函数产出了自家定义的脏格式。
    """

    EXPECTED = {
        1: "一", 2: "二", 10: "十", 11: "十一", 19: "十九", 20: "二十",
        99: "九十九", 100: "一百", 101: "一百零一", 110: "一百一十",
        123: "一百二十三", 200: "二百", 999: "九百九十九", 1000: "一千",
        1001: "一千零一", 1010: "一千零一十", 1100: "一千一百", 2000: "二千",
        2005: "二千零五", 2300: "二千三百", 9999: "九千九百九十九",
    }

    @pytest.mark.parametrize("n,expected", sorted(EXPECTED.items()))
    def test_canonical_forms(self, n, expected):
        assert _cn_ordinal(n) == expected, f"{n} -> {_cn_ordinal(n)}"

    def test_no_arabic_or_latin_in_full_range(self):
        """1~9999 全区间不得出现任何阿拉伯数字或拉丁字母。"""
        for n in range(1, 10000):
            got = _cn_ordinal(n)
            assert not re.search(r"[0-9A-Za-z]", got), f"{n} -> {got}"

    def test_full_range_is_unique(self):
        """每个序号的中文形态互不相同（否则无法承担消歧职责）。"""
        forms = {_cn_ordinal(n) for n in range(1, 10000)}
        assert len(forms) == 9999

    @pytest.mark.parametrize("bad", [0, -3, "abc", None, 1.7])
    def test_invalid_input_never_raises(self, bad):
        """非法输入必须降级为「一」而非抛异常。"""
        assert _cn_ordinal(bad) == "一"

    def test_out_of_range_falls_back_to_arabic(self):
        """> 9999 仍回落阿拉伯数字（同级不可能这么多，仅保留可见性）。"""
        assert _cn_ordinal(10000) == "10000"


class TestNoArabicOrdinalSuffix:
    """同级重名条目很多时，消歧产物仍不得出现「之101」这类阿拉伯数字形态。

    这是用户报告「编号冲突后缀」的直接上游：不是生成侧产出的，而是**兜底侧**
    产出的 —— 语义池/维度词池耗尽后的最后一层。
    """

    ARABIC_ORDINAL = re.compile(r"之[0-9]")

    @staticmethod
    def _sibling_tree(titles: list[str]) -> list[dict]:
        return [
            {
                "id": "1",
                "title": "总体施工部署",
                "children": [
                    {
                        "id": "1.1",
                        "title": CHAPTER_GENERIC,
                        "children": [
                            {"id": f"1.1.{i + 1}", "title": t, "children": []}
                            for i, t in enumerate(titles)
                        ],
                    }
                ],
            }
        ]

    @pytest.mark.parametrize("count", [2, 5, 20, 100, 101, 150, 420])
    def test_uniquify_never_emits_arabic_ordinal(self, count):
        tr = self._sibling_tree([CHAPTER_GENERIC] * count)
        tr = _uniquify_outline_titles(tr)
        got = [c["title"] for c in tr[0]["children"][0]["children"]]
        assert len(set(got)) == len(got), got[-5:]
        # 首个原始标题按政策保持不动，不计入产物质量
        renamed = got[1:]
        assert not [t for t in renamed if self.ARABIC_ORDINAL.search(t)], got
        assert not [t for t in renamed if _is_dirty_l3_title_deep(t)], got
        # 产物必须是可读的具体标题，而不是纯序数后缀
        assert not [t for t in renamed if t.startswith("总体实施策划之")], got

    @pytest.mark.parametrize("count", [2, 5, 20, 100, 101, 150, 420])
    def test_uniquify_ordinal_is_pure_chinese(self, count):
        """即便真的走到中文序数兜底，也必须是纯中文序数。"""
        tr = self._sibling_tree(["工序交接流程"] * count)
        tr = _uniquify_outline_titles(tr)
        got = [c["title"] for c in tr[0]["children"][0]["children"]]
        ordinals = [t for t in got if "之" in t]
        for t in ordinals:
            assert not self.ARABIC_ORDINAL.search(t), t

    def test_disambiguate_exhausts_pool_without_arabic(self):
        """连续 400 次消歧（远超维度词池容量）不得退化成阿拉伯数字序数。"""
        used: set[str] = set()
        out: list[str] = []
        for _ in range(400):
            cand = _disambiguate_title(CHAPTER_GENERIC, used, parent_title="")
            used.add(_title_core(cand))
            out.append(cand)
        assert len(set(out)) == 400, "400 次消歧必须全部唯一"
        assert not [t for t in out if self.ARABIC_ORDINAL.search(t)], out[-5:]
        assert not [t for t in out if _is_dirty_l3_title_deep(t)], out[-5:]

