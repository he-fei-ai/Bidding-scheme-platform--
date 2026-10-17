# -*- coding: utf-8 -*-
"""P2 前端智能编排 单元测试（2026-10-17）。"""

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_BACKEND_DIR))

import pytest

from app.services.ai.chart_orchestrator import (
    analyze_chart_needs,
    generate_chart_payload,
    suggest_insertion_position,
    batch_analyze_sections,
    generate_all_chart_payloads,
    _generate_local_architecture,
    _generate_local_labor,
    _generate_local_comparison,
    _generate_local_layout,
    _generate_local_timeline,
)


class TestChartNeedsAnalysis:
    def test_organization_section_detected(self):
        result = analyze_chart_needs(
            section_title="项目组织架构",
            content="本项目设置项目经理部，下设技术负责人、安全员、质检员等岗位。"
        )
        assert result["needed"] is True
        assert result["chart_type"] == "architecture"

    def test_flowchart_section_detected(self):
        result = analyze_chart_needs(
            section_title="施工工艺流程",
            content="施工流程包括放线、开挖、施工、验收、交付等步骤。"
        )
        assert result["needed"] is True
        assert result["chart_type"] in ("flowchart", "gantt")

    def test_text_only_section_rejected(self):
        result = analyze_chart_needs(
            section_title="项目背景与建设意义",
            content="本项目位于XX市，建设规模为XX平方米，总工期XX个月。"
        )
        assert result["needed"] is False
        assert result["chart_type"] == "none"

    def test_empty_content_returns_empty(self):
        result = analyze_chart_needs(section_title="", content="")
        assert result["needed"] is False

    def test_labor_section_detected(self):
        result = analyze_chart_needs(
            section_title="劳动力配置计划",
            content="项目共需管理人员5人、技术人员10人、劳务人员30人。"
                    "分为准备、施工、验收三个阶段配置劳动力。"
        )
        assert result["needed"] is True
        assert result["chart_type"] in ("labor", "flowchart", "gantt")

    def test_timeline_section_detected(self):
        result = analyze_chart_needs(
            section_title="关键里程碑时间线",
            content="项目关键节点包括开工、基础完成、主体封顶、竣工验收。"
        )
        assert result["needed"] is True
        assert result["chart_type"] in ("timeline", "gantt")


class TestChartPayloadGeneration:
    def test_architecture_payload_generated(self):
        payload = generate_chart_payload(
            section_title="组织架构",
            content="项目经理下设技术负责人、安全员、质检员。",
            chart_type="architecture",
            title_hint="项目组织架构图",
        )
        assert payload is not None
        assert payload["type"] == "architecture"
        assert "tree" in payload

    def test_labor_payload_generated(self):
        payload = generate_chart_payload(
            section_title="劳动力配置",
            content="分准备、施工、验收三阶段配置劳动力。",
            chart_type="labor",
            title_hint="劳动力配置计划",
        )
        assert payload is not None
        assert payload["type"] == "labor"
        assert "phases" in payload
        assert "data" in payload

    def test_comparison_payload_generated(self):
        payload = generate_chart_payload(
            section_title="方案比选",
            content="方案一、方案二、方案三进行对比分析。",
            chart_type="comparison",
            title_hint="方案对比图",
        )
        assert payload is not None
        assert payload["type"] == "comparison"
        assert "rows" in payload
        assert len(payload["rows"]) >= 2

    def test_layout_payload_generated(self):
        payload = generate_chart_payload(
            section_title="总平面布置",
            content="场地分为办公区、生产区、材料堆场等区域。",
            chart_type="layout",
            title_hint="总平面布置图",
        )
        assert payload is not None
        assert payload["type"] == "layout"
        assert "zones" in payload

    def test_timeline_payload_generated(self):
        payload = generate_chart_payload(
            section_title="里程碑时间线",
            content="关键节点包括开工、主体封顶、竣工验收。",
            chart_type="timeline",
            title_hint="里程碑时间线",
        )
        assert payload is not None
        assert payload["type"] == "timeline"
        assert "milestones" in payload
        assert len(payload["milestones"]) >= 2

    def test_none_type_returns_none(self):
        payload = generate_chart_payload(section_title="任意", content="任意", chart_type="none")
        assert payload is None

    def test_empty_chart_type_returns_none(self):
        payload = generate_chart_payload(section_title="任意", content="任意", chart_type="")
        assert payload is None


class TestInsertionPosition:
    def test_flowchart_position_after_keyword(self):
        content = "第一段：项目概况。\n\n第二段：施工工艺流程包括放线、开挖。\n\n第三段：质量保证。"
        position = suggest_insertion_position(content, "flowchart")
        assert position == 1

    def test_empty_content_returns_negative(self):
        position = suggest_insertion_position("", "flowchart")
        assert position == -1

    def test_single_paragraph_returns_zero(self):
        position = suggest_insertion_position("唯一段落内容。", "flowchart")
        assert position in (0, 1)

    def test_no_keyword_match_returns_early_position(self):
        content = "段落一\n段落二\n段落三"
        position = suggest_insertion_position(content, "flowchart")
        assert position <= 1


class TestBatchOrchestration:
    def test_batch_analyze_sections(self):
        sections = [
            {"id": "sec1", "title": "项目组织架构", "content": "项目经理下设各部门。"},
            {"id": "sec2", "title": "项目背景", "content": "本项目位于XX市。"},
            {"id": "sec3", "title": "施工工艺流程", "content": "施工步骤包括放线、开挖。"},
        ]
        results = batch_analyze_sections(sections)
        assert len(results) == 3
        assert results[0]["needed"] is True
        assert results[1]["needed"] is False
        assert results[2]["needed"] is True

    def test_generate_all_payloads(self):
        sections = [
            {"id": "sec1", "title": "组织架构", "content": "项目经理下设各部门。",
             "needed": True, "chart_type": "architecture", "title_hint": "组织架构图"},
            {"id": "sec2", "title": "背景", "content": "项目背景。",
             "needed": False, "chart_type": "none", "title_hint": ""},
        ]
        payloads = generate_all_chart_payloads(sections)
        assert len(payloads) == 1
        assert payloads[0]["section_id"] == "sec1"
        assert payloads[0]["chart_type"] == "architecture"

    def test_empty_sections_returns_empty(self):
        results = batch_analyze_sections([])
        assert results == []


class TestLocalGeneration:
    def test_local_architecture_valid(self):
        payload = _generate_local_architecture("测试架构")
        assert payload["type"] == "architecture"
        assert payload["tree"]["label"] == "项目组织"
        assert len(payload["tree"]["children"]) == 4

    def test_local_labor_valid(self):
        payload = _generate_local_labor("测试劳动力")
        assert payload["type"] == "labor"
        assert len(payload["phases"]) == 3
        assert len(payload["categories"]) == 3
        assert len(payload["data"]) == 3

    def test_local_comparison_valid(self):
        payload = _generate_local_comparison("测试对比")
        assert payload["type"] == "comparison"
        assert len(payload["rows"]) == 3
        assert all(r["value"] > 0 for r in payload["rows"])

    def test_local_layout_valid(self):
        payload = _generate_local_layout("测试布局")
        assert payload["type"] == "layout"
        assert len(payload["zones"]) == 3
        for zone in payload["zones"]:
            assert 0 <= zone["x"] <= 100
            assert 0 <= zone["y"] <= 100
            assert zone["w"] > 0
            assert zone["h"] > 0

    def test_local_timeline_valid(self):
        payload = _generate_local_timeline("测试时间线")
        assert payload["type"] == "timeline"
        assert len(payload["milestones"]) == 4
        dates = [m["date"] for m in payload["milestones"]]
        assert dates == sorted(dates)