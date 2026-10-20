# -*- coding: utf-8 -*-
"""图表自动编排闭环测试（2026-10-17，需求 1/3/4 端到端接线）

覆盖：
  A. build_chart_fence —— 载荷形态判定围栏语言（mermaid vs chart-json）+ 引导语
  B. content_has_chart_fence —— 围栏变体去重检测（```/~~~、chart_json/chartjson）
  C. insert_fence_at_paragraph —— 关键词段落定位插入 / 空正文 / 越界回退
  D. auto_orchestrate_section —— 闭环编排（需要/不需要/去重/校验失败/空正文降级/章节缺失）
"""

import json
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import pytest

from app.models import SectionUpdate
from app.services.chart_auto_orchestration import (
    CHART_FIELD_MAP,
    auto_orchestrate_section,
    build_chart_fence,
    content_has_chart_fence,
    insert_fence_at_paragraph,
)


# ============================================================
# A. build_chart_fence
# ============================================================

class TestBuildChartFence:
    def test_code_payload_becomes_mermaid_fence(self):
        lead_in, fence = build_chart_fence(
            "flowchart", {"type": "flowchart", "code": "graph TD\n    A-->B", "title": "施工流程"}
        )
        assert fence.startswith("```mermaid\n")
        assert fence.endswith("\n```")
        assert "graph TD" in fence
        assert "下图为施工流程：" == lead_in

    def test_json_payload_becomes_chart_json_fence(self):
        payload = {"type": "labor", "title": "劳动力配置", "phases": ["准备"], "categories": ["管理"], "data": [[1]]}
        lead_in, fence = build_chart_fence("labor", payload)
        assert lead_in == ""
        assert fence.startswith("```chart-json\n")
        assert json.loads(fence.removeprefix("```chart-json\n").removesuffix("\n```")) == payload

    def test_mermaid_without_title_uses_type_label(self):
        lead_in, fence = build_chart_fence("gantt", {"type": "gantt", "code": "gantt\n    title X"})
        assert lead_in == "下图为甘特图："
        assert fence.startswith("```mermaid\n")

    def test_local_json_generators_shape_based(self):
        """本地模板 comparison/layout/timeline 产 JSON 载荷 → chart-json 围栏"""
        for chart_type, key in (("comparison", "rows"), ("layout", "zones"), ("timeline", "milestones")):
            from app.services.ai.chart_orchestrator import _generate_local_comparison, _generate_local_layout, _generate_local_timeline
            payload = {"comparison": _generate_local_comparison, "layout": _generate_local_layout, "timeline": _generate_local_timeline}[chart_type]("测试")
            lead_in, fence = build_chart_fence(chart_type, payload)
            assert fence.startswith("```chart-json\n")
            assert lead_in == ""
            assert key in payload


# ============================================================
# B. content_has_chart_fence
# ============================================================

class TestContentHasChartFence:
    @pytest.mark.parametrize("line", [
        "```mermaid", "```chart-json", "```chart_json", "```chartjson",
        "~~~mermaid", "~~~chart-json", "```mermaid {\"attr\": 1}", "`````chart-json",
    ])
    def test_variants_detected(self, line):
        assert content_has_chart_fence(f"正文段落\n\n{line}\nflowchart TD\nA-->B\n```") is True

    def test_plain_text_no_fence(self):
        assert content_has_chart_fence("这是普通正文，没有图表。\n```python\nprint(1)\n```") is False

    def test_empty_content(self):
        assert content_has_chart_fence("") is False
        assert content_has_chart_fence(None) is False  # type: ignore[arg-type]


# ============================================================
# C. insert_fence_at_paragraph
# ============================================================

class TestInsertFenceAtParagraph:
    FENCE = "```mermaid\nflowchart TD\n    A-->B\n```"

    def test_insert_after_keyword_paragraph(self):
        content = "第一段：项目概况。\n\n第二段：施工工艺流程包括放线、开挖。\n\n第三段：质量保证。"
        # flowchart 关键词在第二段 → 建议位置 1
        from app.services.ai.chart_orchestrator import suggest_insertion_position
        pos = suggest_insertion_position(content, "flowchart")
        new_content = insert_fence_at_paragraph(content, self.FENCE, "下图为流程图：", pos)
        assert new_content.index("第二段") < new_content.index("```mermaid") < new_content.index("第三段")

    def test_empty_content_returns_block_only(self):
        result = insert_fence_at_paragraph("", self.FENCE, "下图为流程图：", 0)
        assert result == "下图为流程图：\n\n" + self.FENCE

    def test_index_out_of_range_clamped(self):
        content = "唯一段落"
        result = insert_fence_at_paragraph(content, self.FENCE, "", 999)
        assert result == content + "\n\n" + self.FENCE

    def test_negative_index_clamped_to_first(self):
        content = "段落一\n\n段落二"
        result = insert_fence_at_paragraph(content, self.FENCE, "", -5)
        assert result.index("段落一") < result.index("```mermaid") < result.index("段落二")


# ============================================================
# D. auto_orchestrate_section（闭环）
# ============================================================

class _UpdateRecorder:
    def __init__(self):
        self.calls: list[tuple[int, SectionUpdate]] = []

    def __call__(self, section_id, data: SectionUpdate):
        self.calls.append((section_id, data))
        return None


def _make_section(title: str, content: str) -> dict:
    return {"id": 1, "project_id": 10, "title": title, "content": content}


class TestAutoOrchestrateSection:
    def test_not_needed_section_skipped(self):
        rec = _UpdateRecorder()
        result = auto_orchestrate_section(
            10, 1,
            _get_section=lambda sid: _make_section("项目背景与建设意义", "本项目位于XX市，建设规模为XX平方米。"),
            _update_section=rec,
        )
        assert result["ok"] is True
        assert result["skipped_reason"] == "not_needed"
        assert result["inserted"] is False
        assert rec.calls == []

    def test_needed_section_fence_inserted_into_content(self):
        rec = _UpdateRecorder()
        content = "第一段：项目概况说明。\n\n第二段：本项目设置项目经理部，下设技术负责人、安全员、质检员等岗位。"
        result = auto_orchestrate_section(
            10, 1,
            _get_section=lambda sid: _make_section("项目组织架构", content),
            _update_section=rec,
        )
        assert result["ok"] is True
        assert result["inserted"] is True
        assert len(rec.calls) == 1
        sid, update = rec.calls[0]
        assert sid == 1
        new_content = update.content
        assert "```mermaid" in new_content or "```chart-json" in new_content
        # 原正文完整保留（正文不被吞）
        for fragment in ("第一段：项目概况说明。", "第二段：本项目设置项目经理部"):
            assert fragment in new_content
        # 图表插在组织架构相关段落附近（插图后位于第二段之后）
        assert new_content.index("第二段") < new_content.index("```")

    def test_existing_fence_dedup_skip(self):
        rec = _UpdateRecorder()
        content = "正文\n\n```mermaid\nflowchart TD\n    A-->B\n```\n"
        result = auto_orchestrate_section(
            10, 1,
            _get_section=lambda sid: _make_section("施工工艺流程", content),
            _update_section=rec,
        )
        assert result["skipped_reason"] == "chart_fence_exists"
        assert result["inserted"] is False
        assert rec.calls == []

    def test_validation_failure_skips_without_write(self, monkeypatch):
        import app.services.ai.image_engine as image_engine

        def _fail(chart_type, chart_data, verbose=False, section_title=None):
            return False, ["mock 校验失败"], chart_data, False, None

        monkeypatch.setattr(image_engine, "validate_chart_for_ai_pipeline", _fail)
        rec = _UpdateRecorder()
        result = auto_orchestrate_section(
            10, 1,
            _get_section=lambda sid: _make_section("劳动力配置计划", "项目共需管理人员5人、技术人员10人、劳务人员30人。分为准备、施工、验收三个阶段配置劳动力。"),
            _update_section=rec,
        )
        assert result["skipped_reason"] == "validation_failed"
        assert result["validation_errors"] == ["mock 校验失败"]
        assert rec.calls == []

    def test_empty_content_with_suggestive_title_falls_back(self):
        """正文为空但标题命中架构关键词（标题推断）→ 降级持久化 chart 字段"""
        rec = _UpdateRecorder()
        result = auto_orchestrate_section(
            10, 1,
            _get_section=lambda sid: _make_section("项目组织架构", ""),
            _update_section=rec,
        )
        assert result["inserted"] is False
        assert result["persisted_chart_field"] == "architecture_json"
        assert result["skipped_reason"] == "content_empty_fallback_chart_field"
        assert len(rec.calls) == 1
        _, update = rec.calls[0]
        payload = json.loads(update.architecture_json)
        assert payload["type"] == "architecture"

    def test_empty_content_with_forced_type_falls_back(self, monkeypatch):
        """正文为空且强制判定需要图（模拟 AI 场景）→ 降级持久化 chart 字段"""
        monkeypatch.setattr(
            "app.services.chart_auto_orchestration.analyze_chart_needs",
            lambda section_title, content, project_summary="", ai_client=None: {
                "needed": True, "chart_type": "labor", "confidence": 0.9,
                "reason": "mock", "title_hint": section_title, "insert_after": "", "source": "mock",
            },
        )
        rec = _UpdateRecorder()
        result = auto_orchestrate_section(
            10, 1,
            _get_section=lambda sid: _make_section("任意章节", ""),
            _update_section=rec,
        )
        assert result["inserted"] is False
        assert result["persisted_chart_field"] == "labor_json"
        assert result["skipped_reason"] == "content_empty_fallback_chart_field"
        assert len(rec.calls) == 1
        _, update = rec.calls[0]
        payload = json.loads(update.labor_json)
        assert payload["type"] == "labor"

    def test_section_not_found(self):
        result = auto_orchestrate_section(
            10, 999, _get_section=lambda sid: None, _update_section=_UpdateRecorder(),
        )
        assert result["ok"] is False
        assert result["skipped_reason"] == "section_not_found"

    def test_chart_field_map_complete(self):
        assert set(CHART_FIELD_MAP) == {
            "flowchart", "gantt", "architecture", "labor", "comparison", "layout", "timeline",
        }
