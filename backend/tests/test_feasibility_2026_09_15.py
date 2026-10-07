# -*- coding: utf-8 -*-
"""可行性研究报告模块回归测试（ENHANCE 2026-09-15，移植自易标 feasibility-report）。

只测确定性逻辑（存储/归一化/下游失效/事实保护/导出），不调 AI。
注意：可研是**单工作区**模块，用例之间必须先 clear_workspace() 隔离。
"""

import pytest

from app.config import FEASIBILITY_REPORTS_DIR, is_downloadable_path
from app.services import feasibility as store
from app.services.feasibility_prompts import (
    ANALYSIS_FIELDS,
    OUTLINE_TEMPLATES,
    analysis_to_markdown,
    format_project_info,
    strip_title_prefix,
)
from app.services.feasibility_tasks import protect_facts


@pytest.fixture(autouse=True)
def _clean_workspace():
    store.clear_workspace()
    yield
    store.clear_workspace()


# ======================= 工作区默认状态 =======================


class TestWorkspaceDefaults:
    def test_default_report(self):
        r = store.load_report()
        assert r["step"] == "materials"
        assert r["outlineTemplate"] == "government"
        assert r["targetWords"] == 30000
        assert r["projectInfo"]["projectType"] == "government"
        assert r["sourceFiles"] == []
        assert r["outlineData"] is None

    def test_normalize_target_words(self):
        assert store.normalize_target_words(None) == 30000
        assert store.normalize_target_words(500) == 30000  # 低于 1000 视为无效
        assert store.normalize_target_words(50000) == 50000


# ======================= 项目信息与资料 =======================


class TestProjectInfoAndSources:
    def test_project_name_required(self):
        with pytest.raises(ValueError, match="项目名称"):
            store.save_project_info({"projectName": ""})

    def test_invalid_project_type_falls_back(self):
        r = store.save_project_info({"projectName": "测试项目", "projectType": "hack"})
        assert r["projectInfo"]["projectType"] == "government"

    def test_add_and_remove_source(self):
        store.save_project_info({"projectName": "测试项目"})
        r = store.add_source("资料A", "新建厂房2万平方米。")
        assert len(r["sourceFiles"]) == 1
        assert r["sourceFiles"][0]["fileName"] == "资料A"
        assert "新建厂房" in store.read_combined_source_markdown()
        r = store.remove_source(r["sourceFiles"][0]["id"])
        assert r["sourceFiles"] == []
        with pytest.raises(ValueError, match="未找到"):
            store.remove_source("nope")

    def test_empty_source_rejected(self):
        with pytest.raises(ValueError, match="内容为空"):
            store.add_source("空资料", "   ")


# ======================= 目录树归一化 =======================


class TestOutlineNormalization:
    def test_strip_numbering_and_assign_ids(self):
        raw = [
            {"title": "1. 概述", "description": "d"},
            {"title": "第2章 风险管控方案", "description": "d"},
        ]
        tree = store.assign_outline_ids(store.normalize_outline_tree(raw))
        assert tree[0]["id"] == "1" and tree[0]["title"] == "概述"
        assert tree[1]["id"] == "2" and tree[1]["title"] == "风险管控方案"

    def test_max_three_levels(self):
        raw = [
            {
                "title": "一",
                "description": "d",
                "children": [
                    {"title": "1", "description": "d", "children": [{"title": "a", "description": "d"}]},
                ],
            }
        ]
        tree = store.assign_outline_ids(store.normalize_outline_tree(raw))
        assert tree[0]["children"][0]["children"][0]["id"] == "1.1.1"
        # 第四层被裁掉
        raw4 = [{"title": "一", "description": "d", "children": [
            {"title": "1", "description": "d", "children": [
                {"title": "a", "description": "d", "children": [{"title": "too deep", "description": "d"}]}
            ]}
        ]}]
        tree4 = store.normalize_outline_tree(raw4)
        leaf = tree4[0]["children"][0]["children"][0]
        assert "children" not in leaf

    def test_empty_nodes_dropped(self):
        raw = [{"title": "", "description": ""}, {"title": "有效", "description": ""}]
        tree = store.normalize_outline_tree(raw)
        assert [t["title"] for t in tree] == ["有效"]

    def test_root_cap(self):
        raw = [{"title": f"章{i}", "description": "d"} for i in range(30)]
        assert len(store.normalize_outline_tree(raw)) == 16


# ======================= 下游失效 =======================


class TestDownstreamInvalidation:
    def _build_full_state(self):
        store.save_project_info({"projectName": "测试项目"})
        store.add_source("资料", "内容")
        store.update_report_state(
            {"analysisMarkdown": "## 项目概况\n\n分析结果", "step": "outline"}
        )
        outline = store.assign_outline_ids(
            store.normalize_outline_tree(
                [{"title": "概述", "description": "d", "children": [{"title": "项目概况", "description": "d"}]}]
            )
        )
        store.update_report_state(
            {
                "outlineData": {"outline": outline, "project_name": "测试项目"},
                "step": "parameters",
                "feasibility-analysis": None,
            }
        )
        store.update_report_state(
            {"keyParametersMarkdown": "## 关键参数", "step": "content",
             "feasibility-outline": None}
        )
        # 写一点正文
        store.save_chapter_content("1.1", "这是正文内容。")

    def test_new_source_clears_analysis_and_outline(self):
        self._build_full_state()
        assert "分析结果" in store.load_report()["analysisMarkdown"]
        store.add_source("新资料", "新内容")
        r = store.load_report()
        assert r["analysisMarkdown"] == ""
        assert r["outlineData"] is None
        assert r["keyParametersMarkdown"] == ""

    def test_save_outline_clears_parameters(self):
        self._build_full_state()
        r = store.load_report()
        assert r["keyParametersMarkdown"] == "## 关键参数"
        store.save_outline(r["outlineData"], reason="edit")
        r = store.load_report()
        assert r["keyParametersMarkdown"] == ""

    def test_save_outline_sort_keeps_downstream(self):
        self._build_full_state()
        r = store.load_report()
        store.save_outline(r["outlineData"], reason="sort")
        assert store.load_report()["keyParametersMarkdown"] == "## 关键参数"

    def test_save_parameters_clears_content(self):
        self._build_full_state()
        store.save_key_parameters("## 新口径")
        leaves = store.collect_leaves(store.load_report()["outlineData"]["outline"])
        assert all(not leaf.get("content") for leaf in leaves)


# ======================= 任务状态 =======================


class TestTaskState:
    def test_save_and_load_task(self):
        store.save_task_state(
            "feasibility-analysis",
            {"task_id": "t1", "status": "running", "progress": 42, "stats": {"phase": "generating"}},
        )
        r = store.load_report()
        assert r["feasibility-analysis"]["progress"] == 42
        assert r["feasibility-analysis"]["stats"] == {"phase": "generating"}

    def test_none_deletes_task(self):
        store.save_task_state("feasibility-outline", {"task_id": "t2", "status": "running"})
        store.save_task_state("feasibility-outline", None)
        assert store.load_report().get("feasibility-outline") is None

    def test_unknown_task_type_ignored(self):
        store.save_task_state("feasibility-hack", {"task_id": "x", "status": "running"})
        assert store.load_report().get("feasibility-hack") is None


# ======================= 事实保护（润色阶段） =======================


class TestProtectFacts:
    def test_missing_quantity_keeps_original(self):
        original = "总投资500万元，工期12个月。"
        rewritten = "总投资约500万元左右。"  # 丢了「12个月」
        assert protect_facts(original, rewritten) == original

    def test_missing_marker_keeps_original(self):
        original = "总投资【待补充】，单位为建设单位。"
        rewritten = "总投资由建设单位确定。"
        assert protect_facts(original, rewritten) == original

    def test_preserving_facts_accepts_rewrite(self):
        original = "总投资500万元，工期【待补充】。"
        rewritten = "项目总投资500万元，具体工期【待补充】后确定。"
        assert protect_facts(original, rewritten) == rewritten

    def test_empty_rewrite_keeps_original(self):
        assert protect_facts("原文内容", "  ") == "原文内容"


# ======================= DOCX 导出 =======================


class TestExportDocx:
    def test_export_requires_outline(self):
        with pytest.raises(ValueError, match="目录"):
            store.export_docx()

    def test_export_writes_file_in_whitelisted_dir(self):
        store.save_project_info({"projectName": "导出测试项目"})
        outline = store.assign_outline_ids(
            store.normalize_outline_tree(
                [{"title": "概述", "description": "d", "children": [{"title": "项目概况", "description": "d"}]}]
            )
        )
        store.update_report_state({"outlineData": {"outline": outline}, "keyParametersMarkdown": "## 参数"})
        store.save_chapter_content("1.1", "第一章正文内容。")
        path = store.export_docx()
        p = __import__("pathlib").Path(path).resolve()
        assert p.exists() and p.suffix == ".docx"
        assert p.is_relative_to(FEASIBILITY_REPORTS_DIR.resolve())
        assert is_downloadable_path(p)
        assert "导出测试项目" in p.name


# ======================= 提示词与模板 =======================


class TestPromptsAndTemplates:
    def test_seven_templates_each_have_consistent_shape(self):
        assert len(OUTLINE_TEMPLATES) == 7
        for tid, tpl in OUTLINE_TEMPLATES.items():
            assert tpl["chapters"], tid
            assert len(tpl["suggested_children"]) == len(tpl["chapters"]), tid

    def test_format_project_info_placeholders(self):
        text = format_project_info({"projectName": "X"})
        assert "项目名称：X" in text
        assert "总投资：【待补充】" in text

    def test_analysis_markdown_covers_all_fields(self):
        md = analysis_to_markdown({})
        for _key, title in ANALYSIS_FIELDS:
            assert f"## {title}" in md
        assert "【待补充】" in md

    def test_strip_title_prefix_variants(self):
        assert strip_title_prefix("1. 概述") == "概述"
        assert strip_title_prefix("第2章 风险") == "风险"
        assert strip_title_prefix("2.3 投资估算") == "投资估算"
        assert strip_title_prefix("概述") == "概述"


# ======================= 章节编辑 =======================


class TestChapterEdit:
    def test_save_chapter_requires_existing_node(self):
        with pytest.raises(ValueError, match="未找到"):
            store.save_chapter_content("9.9.9", "内容")

    def test_save_chapter_content(self):
        store.save_project_info({"projectName": "测试项目"})
        outline = store.assign_outline_ids(
            store.normalize_outline_tree([{"title": "概述", "description": "d"}])
        )
        store.update_report_state({"outlineData": {"outline": outline}})
        store.save_chapter_content("1", "修改后的正文")
        tree = store.load_report()["outlineData"]["outline"]
        assert tree[0]["content"] == "修改后的正文"
