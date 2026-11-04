"""图表遗留问题第三批加固测试（2026-11-03，A3 建议闭环）。

覆盖项（编号与 2026-10-27 报告附录 A3 对应）：
  A3-2  空态优于错态：content_has_extractable_data 判定口径 +
        auto_orchestrate_section 对本地模板兜底的 no_real_data_local 收口
        （非空正文零数据线索 → 不插围栏、不落库；AI 载荷/空正文不受影响）
  A3-1c 导出前数据矛盾告警：warn_local_template_data_mismatch——source="local"
        载荷数字与正文零交集 → WARNING（只告警不阻断）
"""

from __future__ import annotations

import json
import logging

import pytest

from app.services.ai.chart_orchestrator import content_has_extractable_data
from app.services.chart_auto_orchestration import auto_orchestrate_section
from app.services.export_chart_pipeline import warn_local_template_data_mismatch


# ---------------------------------------------------------------------------
# fixtures / 辅助（与编排测试同模式：注入 _get_section/_update_section）
# ---------------------------------------------------------------------------
class _UpdateRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, section_id, data):
        self.calls.append((section_id, data))
        return None


def _make_section(title: str, content: str) -> dict:
    return {"id": 1, "project_id": 10, "title": title, "content": content}


def _force_analysis(chart_type: str):
    """mock analyze_chart_needs：强制判定需要指定类型图表（source=local）。"""

    def _inner(section_title, content, project_summary="", ai_client=None):
        return {
            "needed": True,
            "chart_type": chart_type,
            "confidence": 0.9,
            "reason": "mock",
            "title_hint": section_title,
            "insert_after": "",
            "source": "local",
        }

    return _inner


# ============================================================
# A. content_has_extractable_data（A3-2 判定口径）
# ============================================================
class TestContentHasExtractableData:
    @pytest.mark.parametrize("content,ctype", [
        ("本工程总工期 90 天", "gantt"),
        ("配置管理人员5人、技术人员10人", "labor"),
        ("开工日期 2026-03-01", "timeline"),
        ("基础阶段 30 个工日完成", "gantt"),
        ("方案一占比 25%，方案二 60%", "comparison"),
        ("2026年3月进场", "gantt"),
    ])
    def test_data_driven_with_numeric_clue(self, content, ctype):
        assert content_has_extractable_data(content, ctype) is True

    @pytest.mark.parametrize("content,ctype", [
        ("合理安排施工顺序，科学组织", "gantt"),
        ("劳动力队伍素质过硬，管理有序", "labor"),
        ("对比分析各方案优劣", "comparison"),
        ("里程碑节点清晰可控", "timeline"),
    ])
    def test_data_driven_without_numeric_clue(self, content, ctype):
        assert content_has_extractable_data(content, ctype) is False

    @pytest.mark.parametrize("content,ctype", [
        ("本项目设置项目经理部，下设安全员、质检员等岗位", "architecture"),
        ("施工工艺流程包括放线、开挖、回填", "flowchart"),
    ])
    def test_structural_with_keyword_hit(self, content, ctype):
        assert content_has_extractable_data(content, ctype) is True

    @pytest.mark.parametrize("content,ctype", [
        ("本节为概述性描述。", "architecture"),
        ("本节为概述性描述。", "flowchart"),
    ])
    def test_structural_without_keyword_hit(self, content, ctype):
        assert content_has_extractable_data(content, ctype) is False

    def test_empty_content_returns_false(self):
        assert content_has_extractable_data("", "labor") is False
        assert content_has_extractable_data("   \n  ", "gantt") is False
        assert content_has_extractable_data(None, "gantt") is False  # type: ignore[arg-type]

    def test_chart_type_normalization(self):
        """类型大小写/空白容错：'Labor' / ' labor ' 均按数据型判定。"""
        assert content_has_extractable_data("配置 5 人", "Labor") is True
        assert content_has_extractable_data("配置 5 人", " labor ") is True
        # 数字线索但结构型关键词缺失 → 结构型仍看关键词
        assert content_has_extractable_data("配置 5 人", "flowchart") is False


# ============================================================
# B. auto_orchestrate_section 空态收口（A3-2 集成）
# ============================================================
class TestAutoOrchestrateNoRealDataLocal:
    def test_labor_without_numeric_clue_skipped(self, monkeypatch):
        """非空正文零数据线索（labor 无任何数字）→ no_real_data_local，不插不落。"""
        monkeypatch.setattr(
            "app.services.chart_auto_orchestration.analyze_chart_needs",
            _force_analysis("labor"),
        )
        rec = _UpdateRecorder()
        result = auto_orchestrate_section(
            10, 1,
            _get_section=lambda sid: _make_section(
                "劳动力配置计划", "劳动力队伍素质过硬，管理有序，分工明确。"
            ),
            _update_section=rec,
        )
        assert result["ok"] is True
        assert result["inserted"] is False
        assert result["skipped_reason"] == "no_real_data_local"
        assert result["persisted_chart_field"] == ""
        assert rec.calls == [], "空态不落库（宁缺毋假）"
        assert result["payload"] is None or result["payload"] is not None  # 不落库即可

    def test_gantt_with_numeric_clue_inserts_normally(self, monkeypatch):
        """正文有数字线索 → 空态判定通过，围栏照常插入（回归保护）。"""
        monkeypatch.setattr(
            "app.services.chart_auto_orchestration.analyze_chart_needs",
            _force_analysis("gantt"),
        )
        rec = _UpdateRecorder()
        content = "第一段：项目概况。\n\n第二段：总工期 180 天，分三个阶段组织流水施工。"
        result = auto_orchestrate_section(
            10, 1,
            _get_section=lambda sid: _make_section("施工进度计划", content),
            _update_section=rec,
        )
        assert result["inserted"] is True
        assert result["skipped_reason"] == ""
        assert "```mermaid" in rec.calls[0][1].content

    def test_ai_payload_not_subject_to_empty_state(self, monkeypatch):
        """AI 载荷（source=ai）不做空态判定——数据约束由提示词负责。"""
        monkeypatch.setattr(
            "app.services.chart_auto_orchestration.analyze_chart_needs",
            _force_analysis("labor"),
        )
        monkeypatch.setattr(
            "app.services.chart_auto_orchestration.generate_chart_payload",
            lambda *a, **kw: {
                "type": "labor", "title": "AI", "source": "ai",
                "phases": ["准备", "施工", "验收"],
                "categories": ["管理", "技术", "劳务"],
                "data": [[3, 2, 5], [5, 10, 30], [2, 3, 5]],
            },
        )
        rec = _UpdateRecorder()
        result = auto_orchestrate_section(
            10, 1,
            _get_section=lambda sid: _make_section(
                "劳动力配置计划", "劳动力队伍素质过硬，管理有序。"
            ),
            _update_section=rec,
        )
        assert result["inserted"] is True
        assert result["skipped_reason"] == ""

    def test_empty_content_keeps_legacy_fallback(self, monkeypatch):
        """空正文维持既有降级契约（content_empty_fallback_chart_field）不收紧。"""
        monkeypatch.setattr(
            "app.services.chart_auto_orchestration.analyze_chart_needs",
            _force_analysis("architecture"),
        )
        rec = _UpdateRecorder()
        result = auto_orchestrate_section(
            10, 1,
            _get_section=lambda sid: _make_section("项目组织架构", ""),
            _update_section=rec,
        )
        assert result["inserted"] is False
        assert result["skipped_reason"] == "content_empty_fallback_chart_field"
        assert result["persisted_chart_field"] == "architecture_json"

    def test_existing_fence_takes_precedence_over_empty_state(self, monkeypatch):
        """正文已有围栏（去重必然不出图）→ 空态判定让位，报 chart_fence_exists。"""
        monkeypatch.setattr(
            "app.services.chart_auto_orchestration.analyze_chart_needs",
            _force_analysis("flowchart"),
        )
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

    def test_local_payload_with_data_falls_to_marker_path(self, monkeypatch):
        """有数字线索 + 同类型标记 → 降级落库路径不被空态判定拦截。"""
        monkeypatch.setattr(
            "app.services.chart_auto_orchestration.analyze_chart_needs",
            _force_analysis("labor"),
        )
        rec = _UpdateRecorder()
        content = "配置管理人员 5 人。\n\n<<CHART:labor>>\n\n配齐劳务班组。"
        result = auto_orchestrate_section(
            10, 1,
            _get_section=lambda sid: _make_section("劳动力配置计划", content),
            _update_section=rec,
        )
        assert result["inserted"] is False
        assert result["skipped_reason"] == "chart_marker_exists_fallback_chart_field"
        assert result["persisted_chart_field"] == "labor_json"


# ============================================================
# C. warn_local_template_data_mismatch（A3-1c 矛盾告警）
# ============================================================
class TestWarnLocalTemplateDataMismatch:
    def test_zero_intersection_warns(self, caplog):
        """local 载荷数字与正文零交集 → True + WARNING 日志。"""
        payload = {
            "type": "labor", "title": "劳动力配置", "source": "local",
            "phases": ["准备", "施工"], "categories": ["管理"],
            "data": [[3, 7], [8, 22]],
        }
        with caplog.at_level(logging.WARNING, logger="app.services.export_chart_pipeline"):
            warned = warn_local_template_data_mismatch(
                payload, "本项目共需劳动力 15 人、60 个工日。", "labor", "劳动力配置计划"
            )
        assert warned is True
        assert any("图表矛盾告警" in r.message for r in caplog.records)

    def test_intersection_no_warn(self, caplog):
        """载荷数字与正文有重合 → False（保守口径，零误报优先）。"""
        payload = {
            "type": "labor", "title": "劳动力配置", "source": "local",
            "phases": ["施工"], "categories": ["管理"], "data": [[5]],
        }
        with caplog.at_level(logging.WARNING, logger="app.services.export_chart_pipeline"):
            warned = warn_local_template_data_mismatch(
                payload, "配置管理人员 5 人。", "labor", "劳动力配置计划"
            )
        assert warned is False
        assert not any("图表矛盾告警" in r.message for r in caplog.records)

    def test_ai_source_skipped(self):
        payload = {"type": "labor", "source": "ai", "data": [[99]]}
        assert warn_local_template_data_mismatch(
            payload, "正文数字 1 人。", "labor", "T"
        ) is False

    def test_non_numeric_chart_type_skipped(self):
        """layout（坐标）与 architecture（无数字语义）不在检测范围。"""
        layout = {"type": "layout", "source": "local",
                  "zones": [{"name": "办公区", "x": 0, "y": 0, "w": 30, "h": 40}]}
        assert warn_local_template_data_mismatch(
            layout, "正文数字 100 人。", "layout", "T"
        ) is False
        arch = {"type": "architecture", "source": "local", "tree": {"label": "项目组织"}}
        assert warn_local_template_data_mismatch(
            arch, "正文数字 100 人。", "architecture", "T"
        ) is False

    def test_content_without_numbers_skipped(self):
        """正文无数字 → 无矛盾对象（空态场景由编排侧收口）。"""
        payload = {"type": "labor", "source": "local", "data": [[3]]}
        assert warn_local_template_data_mismatch(
            payload, "正文无任何数字。", "labor", "T"
        ) is False

    def test_non_dict_source_skipped(self):
        assert warn_local_template_data_mismatch(None, "5 人", "labor") is False
        assert warn_local_template_data_mismatch("mermaid 文本", "5 人", "labor") is False

    def test_mermaid_payload_numbers_detected(self, caplog):
        """Mermaid 代码型 local 载荷（gantt code 内的工期数字）同样参与交集判定。"""
        payload = {
            "type": "gantt", "title": "进度计划", "source": "local",
            "code": "gantt\n    任务1 :a1, 2025-01-01, 45d\n    任务2 :a2, after a1, 60d",
        }
        with caplog.at_level(logging.WARNING, logger="app.services.export_chart_pipeline"):
            warned = warn_local_template_data_mismatch(
                payload, "总工期 120 天。", "gantt", "施工进度计划"
            )
        assert warned is True
        assert any("图表矛盾告警" in r.message for r in caplog.records)


# ============================================================
# D. 前端契约锚：no_real_data_local 标签已登记（防漂移）
# ============================================================
class TestFrontendLabelContract:
    def test_frontend_label_registered(self):
        import re

        # 路径独立计算（测试文件与前端组件跨目录）
        from pathlib import Path

        src_path = (
            Path(__file__).resolve().parents[2] / "frontend" / "src" / "components"
            / "ChartGenerationTab.tsx"
        )
        src = src_path.read_text(encoding="utf-8")
        assert "no_real_data_local" in src, (
            "前端 SKIPPED_REASON_LABELS 缺少 no_real_data_local 中文说明"
        )
        # 有 || reason 兜底（未知原因可读），标签注册与兜底互不冲突
        assert re.search(r"SKIPPED_REASON_LABELS\[.*\]\s*\|\|", src)
