"""A3 建议 1（显式披露 + 可编辑）后端落地测试 — 2026-10-27

覆盖三处修改：
1. ``generate_chart_payload`` 载荷来源标记（source="ai"/"local"）；
2. ``build_chart_fence`` 对 source="local" 的 Mermaid 引导语挂披露括注
   （chart-json 型不加括注，source 键随 JSON 进围栏供前端打标签）；
3. ``_clean_lead_in_title`` 剥离披露括注，保证导出图题干净。
"""

from __future__ import annotations

import json

import pytest

from app.services.ai import chart_orchestrator
from app.services.chart_auto_orchestration import (
    _LOCAL_TEMPLATE_DISCLAIMER,
    build_chart_fence,
)
from app.services.export_chart_pipeline import _clean_lead_in_title


# ============================================================
# 1. 载荷来源标记（generate_chart_payload）
# ============================================================


class TestPayloadSourceMarker:
    CONTENT = "本项目施工流程包括施工准备、基层处理、面层安装三个阶段。"

    def test_local_no_ai_client(self):
        """无 ai_client 纯本地路径 → source="local"。"""
        payload = chart_orchestrator.generate_chart_payload(
            "施工工艺", self.CONTENT, "architecture"
        )
        assert payload is not None
        assert payload["source"] == "local"

    def test_local_mermaid_type(self):
        """flowchart 本地 Mermaid 模板路径 → source="local"。"""
        payload = chart_orchestrator.generate_chart_payload(
            "施工流程", self.CONTENT, "flowchart"
        )
        assert payload is not None
        assert payload["source"] == "local"
        assert payload.get("code")

    def test_ai_success_marks_ai(self, monkeypatch):
        """AI 载荷成功 → source="ai"。"""
        def fake_ai(section_title, content, chart_type, title_hint, ai_client):
            return {"title": "施工流程图", "code": "graph TD\nA[准备] --> B[施工]"}

        monkeypatch.setattr(chart_orchestrator, "_ai_payload_generation", fake_ai)
        payload = chart_orchestrator.generate_chart_payload(
            "施工流程", self.CONTENT, "flowchart", ai_client=object()
        )
        assert payload["source"] == "ai"
        assert "模板示例" not in json.dumps(payload, ensure_ascii=False)

    def test_ai_failure_falls_back_local(self, monkeypatch):
        """AI 抛异常回退本地 → source="local"。"""
        def boom(*args, **kwargs):
            raise RuntimeError("AI 不可用")

        monkeypatch.setattr(chart_orchestrator, "_ai_payload_generation", boom)
        payload = chart_orchestrator.generate_chart_payload(
            "施工工艺", self.CONTENT, "architecture", ai_client=object()
        )
        assert payload is not None
        assert payload["source"] == "local"

    def test_ai_empty_falls_back_local(self, monkeypatch):
        """AI 返回空（拒绝生成）回退本地 → source="local"。"""
        monkeypatch.setattr(
            chart_orchestrator, "_ai_payload_generation", lambda *a, **k: None
        )
        payload = chart_orchestrator.generate_chart_payload(
            "施工工艺", self.CONTENT, "architecture", ai_client=object()
        )
        assert payload is not None
        assert payload["source"] == "local"


# ============================================================
# 2. 围栏构建披露（build_chart_fence）
# ============================================================


class TestBuildChartFenceDisclosure:
    def test_local_mermaid_has_disclaimer(self):
        lead_in, fence = build_chart_fence(
            "flowchart", {"code": "graph TD\nA-->B", "source": "local"}
        )
        assert _LOCAL_TEMPLATE_DISCLAIMER in lead_in
        assert lead_in.startswith("下图为")
        fence.startswith("```mermaid")

    def test_ai_mermaid_no_disclaimer(self):
        lead_in, _ = build_chart_fence(
            "flowchart", {"code": "graph TD\nA-->B", "source": "ai"}
        )
        assert _LOCAL_TEMPLATE_DISCLAIMER not in lead_in

    def test_missing_source_no_disclaimer(self):
        """无 source 键（旧数据/手工载荷）不加括注——向后兼容。"""
        lead_in, _ = build_chart_fence("flowchart", {"code": "graph TD\nA-->B"})
        assert _LOCAL_TEMPLATE_DISCLAIMER not in lead_in

    def test_chart_json_keeps_source_in_fence(self):
        """chart-json 型：lead_in 为空，source 键随 JSON 序列化进围栏（前端打标签依据）。"""
        payload = {
            "type": "architecture",
            "title": "项目组织架构",
            "tree": {"label": "项目组织", "children": [{"label": "项目经理部"}]},
            "source": "local",
        }
        lead_in, fence = build_chart_fence("architecture", payload)
        assert lead_in == ""
        assert fence.startswith("```chart-json")
        assert json.loads(fence.removeprefix("```chart-json\n").removesuffix("\n```"))[
            "source"
        ] == "local"

    def test_disclaimer_constant_used_verbatim(self):
        """生成端括注与常量逐字一致（清理端正则按该形态剥离）。"""
        lead_in, _ = build_chart_fence(
            "flowchart", {"code": "graph TD\nA-->B", "source": "local"}
        )
        assert lead_in.endswith(f"{_LOCAL_TEMPLATE_DISCLAIMER}：")


# ============================================================
# 3. 图题清理（_clean_lead_in_title）
# ============================================================


class TestCleanLeadInDisclosure:
    def test_strip_disclaimer(self):
        assert (
            _clean_lead_in_title("下图为施工流程（模板示例数据，请按项目实际核定）：")
            == "施工流程"
        )

    def test_strip_disclaimer_short_variant(self):
        assert _clean_lead_in_title("下图为劳动力配置（模板示例数据）：") == "劳动力配置"

    def test_normal_lead_in_unaffected(self):
        """无括注的原引导语行为不变（防回归）。"""
        assert _clean_lead_in_title("下图为施工流程：") == "施工流程"
        # 整句为框架词+标点：剥空后回退原文（既有设计，绝不产出空图名）
        assert _clean_lead_in_title("如下图所示：") == "如下图所示："

    def test_end_to_end_fence_to_title(self):
        """端到端：build_chart_fence 披露引导语 → 图题干净。"""
        lead_in, _ = build_chart_fence(
            "flowchart", {"code": "graph TD\nA-->B", "source": "local"}
        )
        assert _clean_lead_in_title(lead_in) == "流程图"

    def test_disclaimer_alone_falls_back(self):
        """剥离后为空回退原文，绝不产出空图名。"""
        raw = "（模板示例数据，请按项目实际核定）"
        assert _clean_lead_in_title(raw) == raw


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
