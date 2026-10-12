"""测试 2026-10-12 引入的新模块：outline_numbering / outline_alignment / prompt_layering / text_edit / hazard_projects。

ENHANCE 2026-10-12：本文件验证 P0/P1/E 引入项的所有公开 API。

运行：
    cd backend
    python -m pytest tests/test_2026_10_12_enhance_modules.py -v
"""

from __future__ import annotations

import pytest

from app.services.ai.outline_numbering import (
    NumberingTemplate,
    default_numbering_template,
    render_outline_number,
    renumber_outline_with_template,
    to_alpha,
    to_chinese,
    to_circled,
    to_roman,
)
from app.services.ai.outline_alignment import (
    AlignmentResult,
    ScoringItem,
    align_outline_to_scoring,
    build_aligned_l1,
    merge_aligned_l1_with_children,
    parse_scoring_items,
    validate_alignment,
)
from app.services.ai.prompt_layering import (
    LAYER_CURRENT_SECTION,
    LAYER_GLOBAL_FACTS,
    LAYER_PROJECT_OVERVIEW,
    LAYER_SYSTEM,
    build_chapter_content_messages,
    build_layered_messages,
    estimate_layer_stability,
    normalize_readable_text,
)
from app.services.ai.hazard_projects import (
    HAZARD_PROJECTS,
    HazardProject,
    HazardScanResult,
    build_hazard_outline_sections,
    get_hazard_by_code,
    list_hazard_projects,
    scan_hazard_projects,
)
from app.utils.text_edit import (
    EditResult,
    apply_text_edit,
    apply_text_edits,
    find_all_occurrences,
    replace_first,
    replace_range,
)


# ==================================================================
# outline_numbering
# ==================================================================


class TestOutlineNumbering:
    """P0-1：目录编号模板化引擎测试。"""

    def test_to_chinese_basic(self):
        assert to_chinese(1) == "一"
        assert to_chinese(10) == "十"
        assert to_chinese(11) == "十一"
        assert to_chinese(20) == "二十"
        assert to_chinese(100) == "一百"
        assert to_chinese(1000) == "一千"

    def test_to_chinese_zeros(self):
        assert to_chinese(1001) == "一千零一"
        assert to_chinese(2000) == "二千"
        assert to_chinese(1101) == "一千一百零一"

    def test_to_chinese_out_of_range(self):
        assert to_chinese(0) == "0"
        assert to_chinese(10000) == "10000"
        assert to_chinese(-1) == "-1"

    def test_to_roman(self):
        assert to_roman(1) == "I"
        assert to_roman(4) == "IV"
        assert to_roman(9) == "IX"
        assert to_roman(14) == "XIV"
        assert to_roman(40) == "XL"
        assert to_roman(90) == "XC"
        assert to_roman(400) == "CD"
        assert to_roman(900) == "CM"
        assert to_roman(1994) == "MCMXCIV"

    def test_to_circled(self):
        assert to_circled(1) == "①"
        assert to_circled(3) == "③"
        assert to_circled(20) == "⑳"
        assert to_circled(21) == "21"

    def test_to_alpha(self):
        assert to_alpha(1) == "A"
        assert to_alpha(3) == "C"
        assert to_alpha(26) == "Z"
        assert to_alpha(27) == "27"

    def test_default_template_renders_path(self):
        t = default_numbering_template()
        assert t.render_path([1]) == "第1章"
        assert t.render_path([1, 2]) == "1.2"
        assert t.render_path([1, 2, 3]) == "1.2.3"
        assert t.render_path([2, 5, 1]) == "2.5.1"

    def test_custom_template(self):
        tpl = '{"1": "第{zh}章", "2": "{num:1}.{num:2}", "3": "（{zh}）"}'
        t = NumberingTemplate.from_json(tpl)
        assert t.render_path([1]) == "第一章"
        assert t.render_path([2]) == "第二章"
        assert t.render_path([1, 2, 3]) == "（三）"

    def test_template_with_circled_alpha(self):
        tpl = '{"1": "{alpha}、", "2": "{circled}{num:2} ", "3": "{num:1}.{num:2}.{num:3}"}'
        t = NumberingTemplate.from_json(tpl)
        assert t.render_path([2]) == "B、"
        assert t.render_path([1, 3]) == "③3 "

    def test_template_with_roman(self):
        tpl = '{"1": "{roman}章", "2": "{num:1}.{num:2}"}'
        t = NumberingTemplate.from_json(tpl)
        assert t.render_path([4]) == "IV章"
        assert t.render_path([9]) == "IX章"

    def test_template_from_json_fallback_on_error(self):
        t = NumberingTemplate.from_json("not a json")
        assert t.render_path([1, 2]) == "1.2"

    def test_renumber_tree_preserves_ids(self):
        tree = [
            {"id": "1", "title": "A", "children": [
                {"id": "1.1", "title": "A1", "children": [
                    {"id": "1.1.1", "title": "A11"}
                ]}
            ]}
        ]
        new_tree, changed = renumber_outline_with_template(tree, default_numbering_template())
        assert new_tree[0]["id"] == "1"
        assert new_tree[0]["children"][0]["id"] == "1.1"
        assert new_tree[0]["number"] == "第1章"
        assert new_tree[0]["children"][0]["number"] == "1.1"
        assert new_tree[0]["children"][0]["children"][0]["number"] == "1.1.1"
        assert new_tree[0]["level"] == 1
        assert new_tree[0]["children"][0]["level"] == 2
        assert new_tree[0]["children"][0]["children"][0]["level"] == 3

    def test_render_outline_number_helper(self):
        assert render_outline_number([1]) == "第1章"
        assert render_outline_number([1], '{"1": "第{zh}章"}') == "第一章"

    def test_level_placeholder(self):
        tpl = '{"1": "{level:1}", "2": "{level:1}.{level:2}"}'
        t = NumberingTemplate.from_json(tpl)
        assert t.render_path([2]) == "2"
        assert t.render_path([1, 3]) == "1.3"

    def test_missing_path_segment_falls_back(self):
        tpl = '{"1": "{num:5}", "2": "{num:1}.{num:5}"}'
        t = NumberingTemplate.from_json(tpl)
        assert t.render_path([3]) == "3"


# ==================================================================
# outline_alignment
# ==================================================================


class TestOutlineAlignment:
    def test_parse_scoring_items_chinese(self):
        text = """
一、技术方案（30 分）
    施工组织设计合理。
二、项目管理（20 分）
    项目管理体系完善。
三、质量与安全管理（30 分）
    质量与安全管理体系健全。
四、其他（20 分）
    其他加分项。
"""
        items = parse_scoring_items(text)
        assert len(items) == 4
        assert items[0].id == "R1"
        assert items[0].title == "技术方案"
        assert items[0].weight == 30.0
        assert items[1].title == "项目管理"
        assert items[2].title == "质量与安全管理"
        assert items[3].title == "其他"

    def test_parse_scoring_items_numbered(self):
        text = """
1. 施工组织设计（15 分）
2. 进度计划（10 分）
3. 质量保证（5 分）
"""
        items = parse_scoring_items(text)
        assert len(items) == 3
        assert items[0].weight == 15.0
        assert items[1].weight == 10.0
        assert items[2].weight == 5.0

    def test_parse_empty_returns_empty(self):
        assert parse_scoring_items("") == []
        assert parse_scoring_items(None) == []

    def test_build_aligned_l1(self):
        items = [
            ScoringItem(id="R1", title="技术方案", weight=30.0),
            ScoringItem(id="R2", title="项目管理", weight=20.0),
        ]
        l1 = build_aligned_l1(items)
        assert len(l1) == 2
        assert l1[0]["id"] == "1"
        assert l1[0]["title"] == "技术方案"
        assert l1[0]["source_requirement_id"] == "R1"
        assert l1[0]["locked"] is True
        assert l1[1]["source_requirement_weight"] == 20.0

    def test_validate_alignment_passes_when_matching(self):
        outline = [
            {"title": "技术方案", "source_requirement_id": "R1"},
            {"title": "项目管理", "source_requirement_id": "R2"},
        ]
        items = [
            ScoringItem(id="R1", title="技术方案", weight=30.0),
            ScoringItem(id="R2", title="项目管理", weight=20.0),
        ]
        assert validate_alignment(outline, items) == []

    def test_validate_alignment_fails_on_mismatch(self):
        outline = [
            {"title": "错误的标题", "source_requirement_id": "R1"},
            {"title": "另一个错误", "source_requirement_id": "R2"},
        ]
        items = [
            ScoringItem(id="R1", title="技术方案", weight=30.0),
            ScoringItem(id="R2", title="项目管理", weight=20.0),
        ]
        suggestions = validate_alignment(outline, items)
        assert len(suggestions) >= 2

    def test_validate_alignment_fails_on_count_mismatch(self):
        outline = [{"title": "技术方案", "source_requirement_id": "R1"}]
        items = [
            ScoringItem(id="R1", title="技术方案", weight=30.0),
            ScoringItem(id="R2", title="项目管理", weight=20.0),
        ]
        suggestions = validate_alignment(outline, items)
        assert any("L1 数量不一致" in s for s in suggestions)

    def test_align_outline_to_scoring_empty_input(self):
        result = align_outline_to_scoring([], "")
        assert result.success is False
        assert any("为空" in s for s in result.suggestions)

    def test_align_outline_to_scoring_strict_raises(self):
        outline = [{"title": "错误"}]
        text = "一、正确（10 分）"
        with pytest.raises(ValueError):
            align_outline_to_scoring(outline, text, strict=True)

    def test_merge_aligned_l1_with_children_preserves(self):
        aligned = [
            {"id": "1", "title": "技术方案", "source_requirement_id": "R1"},
        ]
        existing = [
            {
                "id": "1", "title": "技术方案",
                "children": [{"id": "1.1", "title": "施工组织设计"}]
            },
        ]
        merged = merge_aligned_l1_with_children(aligned, existing)
        assert merged[0]["title"] == "技术方案"
        assert merged[0]["source_requirement_id"] == "R1"
        assert merged[0]["children"][0]["title"] == "施工组织设计"



# ==================================================================
# prompt_layering
# ==================================================================


class TestPromptLayering:
    def test_normalize_readable_text(self):
        assert normalize_readable_text("a\r\nb\r\nc") == "a\nb\nc"
        assert normalize_readable_text("a\r b") == "a\n b"  # \r becomes \n, leading space kept
        assert normalize_readable_text("a   \nb   \n") == "a\nb"
        assert normalize_readable_text("  hello  ") == "hello"
        assert normalize_readable_text("") == ""
        assert normalize_readable_text(None) == ""

    def test_build_layered_messages_full(self):
        context = {
            LAYER_SYSTEM: "You are an expert.",
            LAYER_PROJECT_OVERVIEW: "Project: X",
            LAYER_GLOBAL_FACTS: "Duration: 90 days",
            LAYER_CURRENT_SECTION: "Write section 1.1",
        }
        messages = build_layered_messages(context)
        assert len(messages) == 4
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "You are an expert."
        assert messages[1]["role"] == "user"
        assert "Project: X" in messages[1]["content"]
        assert messages[2]["content"] == "Duration: 90 days"
        assert messages[3]["content"] == "Write section 1.1"

    def test_build_layered_messages_skips_empty(self):
        context = {
            LAYER_SYSTEM: "sys",
            LAYER_PROJECT_OVERVIEW: "",
            LAYER_GLOBAL_FACTS: None,
            LAYER_CURRENT_SECTION: "current",
        }
        messages = build_layered_messages(context)
        assert len(messages) == 2

    def test_build_layered_messages_with_labels(self):
        context = {
            LAYER_SYSTEM: "sys",
            LAYER_PROJECT_OVERVIEW: "proj",
        }
        messages = build_layered_messages(context, include_labels=True)
        assert messages[1]["content"].startswith("[项目概述]")

    def test_build_chapter_content_messages_order(self):
        messages = build_chapter_content_messages(
            system_rules="RULES",
            project_overview="OVERVIEW",
            global_facts="FACTS",
            reference_material="REF",
            plan_decision="PLAN",
            parent_chapters="PARENT",
            sibling_chapters="SIBLING",
            current_section="CURRENT",
        )
        assert len(messages) == 8
        contents = [m["content"] for m in messages]
        assert contents[0] == "RULES"
        assert contents[1] == "OVERVIEW"
        assert contents[2] == "FACTS"
        assert contents[3] == "REF"
        assert contents[4] == "PLAN"
        assert contents[5] == "PARENT"
        assert contents[6] == "SIBLING"
        assert contents[7] == "CURRENT"

    def test_estimate_layer_stability(self):
        context = {
            LAYER_SYSTEM: "sys",
            LAYER_PROJECT_OVERVIEW: "ov",
            LAYER_GLOBAL_FACTS: "gf",
            LAYER_CURRENT_SECTION: "cur",
        }
        stability = estimate_layer_stability(context)
        assert stability[LAYER_SYSTEM] == "stable"
        assert stability[LAYER_PROJECT_OVERVIEW] == "stable"
        assert stability[LAYER_GLOBAL_FACTS] == "stable"
        assert stability[LAYER_CURRENT_SECTION] == "volatile"


# ==================================================================
# text_edit
# ==================================================================


class TestTextEdit:
    def test_apply_text_edit_success(self):
        result = apply_text_edit("hello world", "world", "there")
        assert result.success is True
        assert result.new_text == "hello there"
        assert result.matches == 1
        assert result.error is None

    def test_apply_text_edit_not_found(self):
        result = apply_text_edit("hello", "world", "x")
        assert result.success is False
        assert result.matches == 0
        assert result.error == "not found"

    def test_apply_text_edit_ambiguous(self):
        result = apply_text_edit("aa aa", "aa", "bb")
        assert result.success is False
        assert result.matches == 2
        assert "ambiguous" in result.error

    def test_apply_text_edit_noop(self):
        result = apply_text_edit("hello", "hello", "hello")
        assert result.success is False
        assert result.error == "no-op"

    def test_apply_text_edit_empty_old(self):
        result = apply_text_edit("hello", "", "x")
        assert result.success is False
        assert result.error == "empty old_text"

    def test_apply_text_edit_delete(self):
        result = apply_text_edit("hello", "llo", "")
        assert result.success is True
        assert result.new_text == "he"

    def test_apply_text_edits_multiple(self):
        text = "hello world, how are you"
        edits = [
            {"old_text": "hello", "new_text": "hi"},
            {"old_text": "world", "new_text": "planet"},
        ]
        final, results = apply_text_edits(text, edits)
        assert final == "hi planet, how are you"
        assert all(r.success for r in results)

    def test_apply_text_edits_stop_on_error(self):
        text = "hello world"
        edits = [
            {"old_text": "foo", "new_text": "bar"},
            {"old_text": "world", "new_text": "x"},
        ]
        final, results = apply_text_edits(text, edits, stop_on_error=True)
        assert final == "hello world"
        assert len(results) == 1

    def test_find_all_occurrences(self):
        positions = find_all_occurrences("aa aa aa", "aa")
        assert positions == [0, 3, 6]

    def test_replace_first(self):
        result = replace_first("aa aa", "aa", "bb")
        assert result == "bb aa"

    def test_replace_range(self):
        text = "hello world"
        # Replace "world" (indices 6-10) with "there"
        result = replace_range(text, 6, 11, "there")
        assert result == "hello there"
        # Replace entire string
        result2 = replace_range(text, 0, 11, "bye")
        assert result2 == "bye"

    def test_replace_range_invalid_bounds(self):
        text = "hello"
        assert replace_range(text, 0, 100, "x") == "hello"
        assert replace_range(text, 5, 3, "x") == "hello"



# ==================================================================
# hazard_projects
# ==================================================================


class TestHazardProjects:
    def test_hazard_projects_count(self):
        assert len(HAZARD_PROJECTS) == 6

    def test_all_hazards_have_required_sections(self):
        for hp in HAZARD_PROJECTS:
            assert hp.code
            assert hp.name
            assert len(hp.keywords) >= 5
            assert len(hp.required_sections) >= 5
            assert len(hp.required_tables) >= 2
            assert len(hp.required_charts) >= 2

    def test_scan_hazard_projects_empty(self):
        result = scan_hazard_projects("")
        assert result.detected == []
        assert result.scores == {}

    def test_scan_hazard_projects_detects_deep_excavation(self):
        text = "本项目包含深基坑工程，基坑支护采用排桩加内支撑方案，基坑深度 8 米。"
        result = scan_hazard_projects(text)
        assert len(result.detected) >= 1
        assert result.detected[0].code == "deep_excavation"
        assert result.scores.get("deep_excavation", 0) > 0

    def test_scan_hazard_projects_detects_multiple(self):
        text = """
本项目包含深基坑工程和高大模板工程。
脚手架采用盘扣式脚手架，起重吊装使用塔吊。
"""
        result = scan_hazard_projects(text)
        codes = {hp.code for hp in result.detected}
        assert "deep_excavation" in codes
        assert "high_formwork" in codes
        assert "scaffolding" in codes
        assert "lifting" in codes

    def test_scan_hazard_projects_ordering_by_score(self):
        text = """
深基坑工程是重点，包含基坑支护、排桩、内支撑等多种技术。
"""
        result = scan_hazard_projects(text)
        assert len(result.detected) >= 1
        # deep_excavation 应该有最高分数
        assert result.detected[0].code == "deep_excavation"

    def test_build_hazard_outline_sections(self):
        hp = get_hazard_by_code("deep_excavation")
        assert hp is not None
        sections = build_hazard_outline_sections([hp])
        assert len(sections) == 1
        assert sections[0]["title"] == "深基坑工程专项施工方案"
        assert sections[0]["hazard_code"] == "deep_excavation"
        assert len(sections[0]["children"]) == len(hp.required_sections)
        for child in sections[0]["children"]:
            assert "id" in child
            assert "title" in child

    def test_get_hazard_by_code(self):
        hp = get_hazard_by_code("lifting")
        assert hp is not None
        assert hp.name == "起重吊装工程"
        assert get_hazard_by_code("not_exist") is None

    def test_list_hazard_projects(self):
        listing = list_hazard_projects()
        assert len(listing) == 6
        assert all("code" in item and "name" in item for item in listing)

    def test_hazard_reference_standards_valid(self):
        for hp in HAZARD_PROJECTS:
            for std in hp.reference_standards:
                # 每个规范编号应该包含字母+数字+破折号+年份
                assert any(c.isdigit() for c in std)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

