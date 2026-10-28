"""目录生成模块：占位式泛化标题漏检回归测试（2026-10-27）。

用户复报的具体形态：三级标题大量是占位式泛化名称（"专项6""总体实施策划（2）"
"总体实施策划（3）"），且存在编号冲突后缀（3）、（2）等。

本次探查发现：
  L1. 「总体实施策划二」这类**裸中文序数后缀**（无括号、无「之」前缀）穿过
      `_is_dirty_l3_title_deep` —— `_ORDINAL_SUFFIX_RE` 要求 `[（(]` 开头，
      裸序数没有括号完全穿透。这类标题与「总体实施策划之二」（合法消歧）只差
      一个「之」，语义上是编号冲突而非合法消歧。
  L2. 「总体实施策划（Ⅱ）」这类**罗马数字括号后缀**穿过 —— `_ORDINAL_SUFFIX_RE`
      的字符类 `[0-9一二三四五六七八九十百]+` 不含罗马数字，`（Ⅱ）`/`（Ⅲ）`
      这类拉丁大写序号完全穿透。
  L3. 「总体实施策划第二」/「总体实施策划第三」这类**「第N」无括号后缀**穿过 ——
      `_ORDINAL_SUFFIX_RE` 要求括号包起来，无括号「第N」穿透。
  L4. 单字符编号标题「1」「一」「Ⅱ」等整段就是序号 —— 单字符既不满足
      `_PLACEHOLDER_ORDINAL_ONLY_RE`（要求「内容/项目」等主体词），也不满足
      `_ORDINAL_ITEM_ONLY_RE`（要求「项/条」等量词），完全穿透。

修复目标：把这四种形态都纳入脏判定，且**保留**「之N」合法消歧的豁免（
`_is_generic_stem_only_title` 对「之」开头/含「之」的候选刻意豁免）。

回归测试：见 `tests/test_outline_title_quality_2026_10_27.py`。
"""
from __future__ import annotations

from app.services.ai.outline_common import (
    _is_dirty_l3_title,
    _is_dirty_l3_title_deep,
    _is_dirty_l2_title_deep,
    _cleanup_placeholder_titles,
    _uniquify_outline_titles,
    _is_placeholder_numbered_title,
)


class TestChineseBareOrdinalSuffix:
    """裸中文序数后缀（无括号无「之」）必须判脏。"""

    def test_bare_chinese_ordinal_suffix_flagged(self):
        # 与「总体实施策划之二」（合法消歧）只差一个「之」字
        for t in ["总体实施策划二", "总体实施策划三", "总体实施策划十", "总体实施策划二十"]:
            assert _is_dirty_l3_title_deep(t), f"{t!r} 应为脏标题"
            assert _is_placeholder_numbered_title(t), f"{t!r} 应被编号占位识别"

    def test_zhi_prefix_form_not_dirty(self):
        # 「之N」是消歧产物，必须豁免
        for t in ["总体实施策划之一", "总体实施策划之二", "总体实施策划之十"]:
            assert not _is_dirty_l3_title_deep(t), f"{t!r} 应为合法消歧"


class TestRomanNumeralInBrackets:
    """罗马数字括号后缀（Ⅰ/Ⅱ/Ⅲ/Ⅳ）必须判脏。"""

    def test_roman_numeral_bracketed_flagged(self):
        for t in ["总体实施策划（Ⅱ）", "总体实施策划（Ⅲ）", "总体实施策划（Ⅳ）", "总体实施策划(Ⅱ)"]:
            assert _is_dirty_l3_title_deep(t), f"{t!r} 应为脏标题"


class TestDeiPrefixNoBrackets:
    """「第N」无括号后缀必须判脏。"""

    def test_dei_prefix_no_bracket_flagged(self):
        for t in ["总体实施策划第二", "总体实施策划第三", "总体实施策划第十二"]:
            assert _is_dirty_l3_title_deep(t), f"{t!r} 应为脏标题"


class TestSingleCharOrdinalTitles:
    """单字符编号标题整段就是序号，必须判脏。"""

    def test_single_char_flagged(self):
        for t in ["1", "12", "一", "二", "十", "Ⅱ", "Ⅲ"]:
            assert _is_dirty_l3_title_deep(t), f"{t!r} 应为脏标题"


class TestFullPipelineCatchesLeaks:
    """端到端验证：这些漏检形态必须被完整质量收口链清洗。"""

    def test_cleanup_placeholder_catches_all_forms(self):
        tree = [{"title": "第一章", "children": [
            {"title": "总体实施策划", "children": [
                {"title": "总体实施策划二", "children": []},
                {"title": "总体实施策划三", "children": []},
                {"title": "总体实施策划（Ⅱ）", "children": []},
                {"title": "总体实施策划（Ⅲ）", "children": []},
                {"title": "总体实施策划第二", "children": []},
                {"title": "总体实施策划第三", "children": []},
                {"title": "1", "children": []},
                {"title": "一", "children": []},
                {"title": "Ⅱ", "children": []},
            ]},
        ]}]
        _cleanup_placeholder_titles(tree)
        t2 = _uniquify_outline_titles(tree)
        final = [l3["title"] for l1 in t2 for l2 in l1["children"] for l3 in l2["children"]]
        # 所有形态都必须被改写
        assert final, "目录结构不能空"
        residual = [t for t in final if _is_dirty_l3_title_deep(t)]
        assert residual == [], f"仍有脏标题: {residual}"


class TestLegitFormsNotOverFiltered:
    """合法中文序数/罗马数字形态必须豁免。"""

    def test_zhi_prefix_disambiguation_legit(self):
        # 「之N」是消歧产物，必须豁免
        for t in ["总体实施策划之一", "总体实施策划之二", "总体实施策划之十", "总体实施策划之二十"]:
            assert not _is_dirty_l3_title_deep(t)

    def test_non_generic_bare_ordinal_not_flagged(self):
        # 「质量管理二」这类**非泛化主体**的裸序数不应过度拦截
        # （用户报告的核心症状是泛化标题的编号冲突，不是所有裸序数）
        assert not _is_dirty_l3_title_deep("质量管理二")
        assert not _is_dirty_l3_title_deep("混凝土施工三")

    def test_legit_titles_not_flagged(self):
        assert not _is_dirty_l3_title_deep("质量管理措施")
        assert not _is_dirty_l3_title_deep("桩基技术交底")
        assert not _is_dirty_l3_title_deep("混凝土配合比（C30）")  # 括号含非编号信息
        assert not _is_dirty_l3_title_deep("2026年施工组织计划")  # 年份不判脏

    def test_generic_only_still_flagged(self):
        # 「总体实施策划」无后缀本身是泛化词表成员，必须仍判脏
        assert _is_dirty_l3_title_deep("总体实施策划")


class TestNoBracketItemSuffixLeak:
    """「泛化词干 × 第N项/条」无括号编号冲突（第七轮新发现，用户复报漏网）。

    用户复报「编号冲突后缀（3）（2）」家族里，以下**无括号**「第N项/条」结尾
    形态此前穿透全部脏判定（`_ORDINAL_SUFFIX_RE` 要求 `[（(]` 包裹，
    `_ORDINAL_ITEM_ONLY_RE` 只认整标题即「第N项」）：
      - 「总体实施策划第2项」「总体实施策划第6条」
      - 「专项第6项」「专项方案第2项」
      - 「专项施工方案第6项」「专项施工技术措施第3项」（长度≥7，被
        `_is_l3_title_project_specific` 误判为「具体」而整体漏出）
      - 「总体实施策划之6」（「之N」阿拉伯数字，合法消歧是「之六」）
    """

    def test_generic_stem_item_suffix_flagged(self):
        for t in [
            "总体实施策划第2项", "总体实施策划第6条", "实施要点第3项",
            "专项第6项", "专项方案第2项",
            "专项施工方案第6项", "专项施工技术措施第3项",
        ]:
            assert _is_dirty_l3_title_deep(t), f"{t!r} 应判脏（无括号第N项/条）"
            assert _is_placeholder_numbered_title(t), f"{t!r} 应被编号占位识别"

    def test_zhi_arabic_num_flagged(self):
        # 「之N」合法消歧是中文「之六」；阿拉伯「之6」是编号冲突后缀
        assert _is_dirty_l3_title_deep("总体实施策划之6")
        assert _is_dirty_l3_title_deep("总体实施策划之20")

    def test_specific_base_item_suffix_not_flagged(self):
        # 具体主体 + 第N项/条 是合法条目标题，不得误伤
        assert not _is_dirty_l3_title_deep("混凝土第3项")
        assert not _is_dirty_l3_title_deep("安全技术交底第3项")
        assert not _is_dirty_l3_title_deep("钢筋绑扎验收第2项")
        assert not _is_dirty_l3_title_deep("基坑监测方案第2项")

    def test_l2_detection_same_scope(self):
        # L2 同样适用结构性脏（编号冲突与层级无关）
        assert _is_dirty_l2_title_deep("总体实施策划第2项")

    def test_cleanup_rewrites_leaks(self):
        tree = [{"title": "第一章", "children": [
            {"title": "总体方案与实施策划", "children": [
                {"title": "总体实施策划第2项", "children": []},
                {"title": "专项方案第2项", "children": []},
                {"title": "专项施工方案第6项", "children": []},
                {"title": "总体实施策划之6", "children": []},
                {"title": "混凝土第3项", "children": []},
            ]},
        ]}]
        _cleanup_placeholder_titles(tree)
        t2 = _uniquify_outline_titles(tree)
        final = [l3["title"] for l1 in t2 for l2 in l1["children"] for l3 in l2["children"]]
        residual = [t for t in final if _is_dirty_l3_title_deep(t)]
        assert residual == [], f"仍有脏标题: {residual}"
        # 具体主体「混凝土第3项」剥后缀保留主体
        assert any("混凝土" in t for t in final), f"具体主体丢失: {final}"

    def test_normalization_collapses_item_suffix(self):
        # 去重口径：无括号第N项/条 与 括号式/无后缀 归一化后同 core
        from app.services.ai.outline_common import _title_core
        core_set = {_title_core(t) for t in [
            "总体实施策划", "总体实施策划（2）", "总体实施策划第2项", "总体实施策划第6条",
        ]}
        assert len(core_set) == 1, f"应归一化为同一 core: {core_set}"
