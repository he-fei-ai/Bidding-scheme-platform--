# -*- coding: utf-8 -*-
"""图表生成与渲染模块深度增强与缺陷修复测试（2026-10-27）

覆盖（对应报告《图表生成与渲染模块深度探索与增强修复 2026-10-27》）：
  A. F1  AI 优先载荷生成（generate_chart_payload：AI 优先 / 无 AI 本地 / AI 异常回退）
  B. F2  渲染端 Mermaid 语法闸门（非法跳过不渲染 / 可修复代码修复后渲染+缓存键重建）
  C. F3  兜底分支图题与去重键同口径（间接：_clean_lead_in_title + 图题口径注释路径）
  D. F5  P3-2 自适应批量接线（_preload_chart_images_parallel 默认线程数）
  E. F6  引导语图名清理（_clean_lead_in_title 框架词剥离 / 空结果回退）
  F. F7  自动编排并发竞态护栏（插入前重读正文防双图）
  G. F8  gantt 兜底死变量清理（回归：仍产出合法 gantt 载荷）
"""

import base64
import json
import sys
import types
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import pytest

import app.services.export as export_mod
from app.models import SectionUpdate
from app.services.ai.chart_orchestrator import (
    _ai_payload_generation,
    generate_chart_payload,
)
from app.services.chart_auto_orchestration import (
    auto_orchestrate_section,
)
from app.services.content_cleaning import parse_content, _extract_fence_caption_hint
from app.services.export_chart_pipeline import (
    _adaptive_batch_size,
    _clean_lead_in_title,
    _generate_local_mermaid_chart,
)


# ============================================================
# E. F6 引导语图名清理
# ============================================================


class TestCleanLeadInTitle:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("下图为施工进度计划：", "施工进度计划"),
            ("下图为项目组织架构", "项目组织架构"),
            ("如下图为质量管理流程。", "质量管理流程"),
            ("请见下图劳动力投入：", "劳动力投入"),
            ("施工进度计划安排如下图。", None),  # 前缀型引导不在剥离范围 → 保留原文（仅去尾标点）
            ("正常引导语：", "正常引导语"),  # 无框架词只去尾冒号
        ],
    )
    def test_frame_words_stripped(self, raw, expected):
        got = _clean_lead_in_title(raw)
        if expected is None:
            assert got == "施工进度计划安排如下图"
        else:
            assert got == expected

    def test_pure_frame_falls_back_to_raw(self):
        # 整句就是框架词，剥离后为空 → 回退原文，绝不产出空图名
        assert _clean_lead_in_title("如下图所示：") == "如下图所示："

    def test_empty(self):
        assert _clean_lead_in_title("") == ""
        assert _clean_lead_in_title(None) == ""  # type: ignore[arg-type]

    def test_caption_hint_across_blank_line(self):
        """F9（2026-10-27）：隔空行的**图引导语**（含框架词）→ 跨空行捕获；
        普通隔行段落（无框架词）→ 仍不采信（维持旧契约）。"""
        # 含框架词的隔行引导语 → 捕获
        lines = ["下图为施工总体流程：", "", "```mermaid", "graph TD", "    A-->B", "```"]
        assert _extract_fence_caption_hint(lines, 2) == "下图为施工总体流程："
        lines_frame2 = ["如下图所示：", "", "```mermaid", "graph TD", "    A-->B", "```"]
        assert _extract_fence_caption_hint(lines_frame2, 2) == "如下图所示："
        # 普通隔行段落（无框架词）→ 不采信（test_hint_absent_after_blank_line 契约）
        lines_plain = ["上文段落没有句号结尾的隔行内容。", "", "```mermaid", "graph TD", "    A-->B", "```"]
        assert _extract_fence_caption_hint(lines_plain, 2) == ""
        # 紧邻行有引导语 → 优取紧邻行（旧行为不变）
        lines3 = ["远处引导语。", "", "紧邻引导语。", "```mermaid", "graph TD", "    A-->B", "```"]
        assert _extract_fence_caption_hint(lines3, 3) == "紧邻引导语。"

    def test_caption_hint_flows_into_marker_via_parse(self):
        """空行图引导语经 parse_content 进入 marker.caption_hint（端到端口径）。"""
        content = (
            "1、施工部署\n\n下图为施工总体流程：\n\n"
            "```mermaid\ngraph TD\n    A-->B\n```\n"
        )
        blocks = parse_content(content)
        hints = []
        for b in blocks:
            if b.type == "chart_marker":
                try:
                    hints.append(json.loads(b.text).get("caption_hint", ""))
                except Exception:
                    pass
        assert "下图为施工总体流程：" in hints


# ============================================================
# A. F1 AI 优先载荷生成
# ============================================================


class TestGenerateChartPayloadAIFirst:
    AI_MARKER = {"type": "gantt", "code": "gantt\n    title AI真实进度\n    dateFormat YYYY-MM-DD\n    section 施工\n    基坑开挖 :a1, 2025-03-01, 30d", "title": "AI真实进度"}

    def test_ai_payload_preferred_when_client_provided(self, monkeypatch):
        called = {"n": 0}

        def _fake_ai(*args, **kwargs):
            called["n"] += 1
            return dict(self.AI_MARKER)

        monkeypatch.setattr(
            "app.services.ai.chart_orchestrator._ai_payload_generation", _fake_ai
        )
        got = generate_chart_payload(
            "施工进度计划", "本工程总工期180天，分基础、主体、装修三个阶段。",
            "gantt", "施工进度计划", ai_client=object(),
        )
        assert called["n"] == 1
        # A3 建议 1 落地后：返回载荷在 AI 结果之上追加 source="ai" 来源标记
        assert got == {**self.AI_MARKER, "source": "ai"}

    def test_ai_failure_falls_back_to_local(self, monkeypatch):
        def _boom(*args, **kwargs):
            raise RuntimeError("AI 不可用")

        monkeypatch.setattr(
            "app.services.ai.chart_orchestrator._ai_payload_generation", _boom
        )
        got = generate_chart_payload(
            "施工进度计划", "本工程总工期180天。", "gantt", "施工进度计划",
            ai_client=object(),
        )
        assert got is not None
        assert got.get("type") == "gantt"
        assert "dateFormat" in got.get("code", "")  # 本地兜底载荷

    def test_no_ai_client_uses_local_only(self, monkeypatch):
        def _unexpected(*args, **kwargs):
            raise AssertionError("无 ai_client 时不得调用 AI 载荷生成")

        monkeypatch.setattr(
            "app.services.ai.chart_orchestrator._ai_payload_generation", _unexpected
        )
        got = generate_chart_payload("施工进度计划", "总工期180天。", "gantt", "施工进度计划")
        assert got is not None and got.get("type") == "gantt"

    def test_none_type_returns_none(self):
        assert generate_chart_payload("x", "y", "none") is None
        assert generate_chart_payload("x", "y", "") is None


# ============================================================
# B. F2 渲染端 Mermaid 语法闸门
# ============================================================

_PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


class TestRenderGateMermaidSyntax:
    def _render(self, payload, monkeypatch, render_fn):
        monkeypatch.setattr(export_mod, "render_mermaid_to_bytes", render_fn)
        return export_mod._render_commercial(
            "flowchart",
            payload,
            "图1-1 测试流程",
            preload_only=True,
            http_service_available=False,
        )

    def test_invalid_mermaid_skipped_without_render(self, monkeypatch):
        """修复后仍非法的纯 Mermaid 载荷：直接判失败，不消耗渲染预算。"""
        calls = {"n": 0}

        def _must_not_render(*args, **kwargs):
            calls["n"] += 1
            raise AssertionError("非法语法不应进入渲染引擎")

        ok = self._render(
            {"type": "flowchart", "code": "这是一段毫无图形结构的纯文本"},
            monkeypatch,
            _must_not_render,
        )
        assert ok is False
        assert calls["n"] == 0

    def test_repairable_mermaid_rendered_with_fixed_code(self, monkeypatch):
        """可机械修复的载荷（行末分号）：修复后渲染，且缓存键按修复后代码重建。"""
        import io

        seen = []
        from PIL import Image as _Img

        def _fake_render(payload_str, typ, skip_http=False):
            seen.append(payload_str)
            buf = io.BytesIO()
            _Img.new("RGB", (4, 4), "white").save(buf, format="PNG")
            buf.seek(0)
            return buf

        bad = {"type": "flowchart", "code": "graph TD\n    A[开始] --> B[结束];"}
        ok = self._render(bad, monkeypatch, _fake_render)
        assert ok is True
        assert len(seen) == 1
        assert ";" not in seen[0]  # 渲染输入已是修复后代码

        # 二次渲染（模拟正式阶段）：闸门再次修复 → L1 缓存按修复后键命中 → 不再渲染
        ok2 = self._render(dict(bad), monkeypatch, _fake_render)
        assert ok2 is True
        assert len(seen) == 1, "修复后缓存键未重建会导致重复渲染"

    def test_json_payload_bypasses_gate(self, monkeypatch):
        """JSON 数据载荷（labor 等）不经过 Mermaid 语法闸门。"""
        import io

        from PIL import Image as _Img

        seen = []

        def _fake_render(payload_str, typ, skip_http=False):
            seen.append(payload_str)
            buf = io.BytesIO()
            _Img.new("RGB", (4, 4), "white").save(buf, format="PNG")
            buf.seek(0)
            return buf

        labor = {
            "type": "labor",
            "title": "劳动力配置",
            "phases": ["准备", "施工"],
            "categories": ["管理"],
            "data": [[2, 5]],
        }
        ok = export_mod._render_commercial(
            "labor", labor, "图1-2 劳动力配置",
            preload_only=True, http_service_available=False,
        )
        monkeypatch.undo()
        assert ok is True


# ============================================================
# D. F5 自适应批量接线
# ============================================================


class TestAdaptiveBatchWiring:
    def _detail(self, gantt_json: str):
        return types.SimpleNamespace(
            id=1, content="本工程总工期180天，分阶段组织流水施工。",
            gantt_json=gantt_json,
        )

    def test_default_workers_use_adaptive_batch_size(self, monkeypatch):
        captured = {}
        _real_pool = export_mod.ThreadPoolExecutor

        def _pool(*args, **kwargs):
            captured["max_workers"] = kwargs.get("max_workers", args[0] if args else None)
            return _real_pool(*args, **kwargs)

        monkeypatch.setattr(export_mod, "ThreadPoolExecutor", _pool)
        monkeypatch.setattr(
            export_mod, "_add_inline_chart",
            lambda *a, **k: True,
        )
        gantt = (
            "gantt\n    title 施工进度\n    dateFormat YYYY-MM-DD\n"
            "    section 准备\n    场地平整 :a1, 2025-01-01, 30d\n"
        )
        n = export_mod._preload_chart_images_parallel(
            {1: self._detail(gantt)}, export_mod.ExportConfig(),
            _http_service_available=False,
        )
        assert n == 1
        assert captured["max_workers"] == _adaptive_batch_size(1)  # 1 任务 → 1 线程

    def test_explicit_max_workers_unchanged(self, monkeypatch):
        captured = {}
        _real_pool = export_mod.ThreadPoolExecutor

        def _pool(*args, **kwargs):
            captured["max_workers"] = kwargs.get("max_workers", args[0] if args else None)
            return _real_pool(*args, **kwargs)

        monkeypatch.setattr(export_mod, "ThreadPoolExecutor", _pool)
        monkeypatch.setattr(export_mod, "_add_inline_chart", lambda *a, **k: True)
        gantt = (
            "gantt\n    title 施工进度\n    dateFormat YYYY-MM-DD\n"
            "    section 准备\n    场地平整 :a1, 2025-01-01, 30d\n"
        )
        export_mod._preload_chart_images_parallel(
            {1: self._detail(gantt)}, export_mod.ExportConfig(),
            max_workers=3, _http_service_available=False,
        )
        assert captured["max_workers"] == 3  # 显式传入不被覆盖（向后兼容）

    def test_adaptive_batch_size_thresholds(self):
        import os as _os

        _cpu = _os.cpu_count() or 4
        # 策略：<=10 全速 / <=50 核心数 / >50 半速（下限 2）
        assert _adaptive_batch_size(0) == min(_cpu * 2, 16)
        assert _adaptive_batch_size(1) == min(_cpu * 2, 16)
        assert _adaptive_batch_size(10) == min(_cpu * 2, 16)
        assert _adaptive_batch_size(20) == max(2, _cpu)
        assert _adaptive_batch_size(50) == max(2, _cpu)
        assert _adaptive_batch_size(500) == max(2, _cpu // 2)


# ============================================================
# F. F7 自动编排并发竞态护栏
# ============================================================


class _UpdateRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, section_id, data: SectionUpdate):
        self.calls.append((section_id, data))
        return None


def _make_section(title: str, content: str) -> dict:
    return {"id": 1, "project_id": 10, "title": title, "content": content}


class TestAutoOrchestrateRaceGuard:
    ORIGINAL = "第一段：项目概况说明。\n\n第二段：本项目设置项目经理部，下设技术负责人、安全员、质检员等岗位。"
    RACED = ORIGINAL + "\n\n```mermaid\nflowchart TD\n    A-->B\n```\n"

    def test_reread_detects_concurrent_fence_and_skips(self):
        """读-检-写窗口内他人已插入围栏 → 重读后跳过，不产生同章双图。"""
        calls = {"n": 0}

        def _get_section(sid):
            calls["n"] += 1
            # 第 1 次读：无围栏（编排判定通过）；第 2 次读（竞态护栏）：已含围栏
            return _make_section("项目组织架构", self.RACED if calls["n"] > 1 else self.ORIGINAL)

        rec = _UpdateRecorder()
        result = auto_orchestrate_section(10, 1, _get_section=_get_section, _update_section=rec)
        assert calls["n"] >= 2, "插入前必须重读最新正文"
        assert result.get("skipped_reason") == "chart_fence_exists"
        assert result.get("inserted") is False
        assert rec.calls == [], "竞态输家不得写库"

    def test_reread_recomputes_position_on_content_change(self):
        """正文被并发改写（无围栏）→ 以最新正文重算插入位置后写入。"""
        calls = {"n": 0}
        rewritten = (
            "改写后的第一段：项目概况。\n\n"
            "改写后的第二段：本项目设置项目经理部，下设技术负责人、安全员、质检员等岗位。"
        )

        def _get_section(sid):
            calls["n"] += 1
            return _make_section(
                "项目组织架构", rewritten if calls["n"] > 1 else self.ORIGINAL
            )

        rec = _UpdateRecorder()
        result = auto_orchestrate_section(10, 1, _get_section=_get_section, _update_section=rec)
        assert result.get("inserted") is True
        assert len(rec.calls) == 1
        new_content = rec.calls[0][1].content
        # 写入的是基于**最新正文**的内容：原正文首段不应残留
        assert "第一段：项目概况说明。" not in new_content
        assert "改写后的第一段" in new_content
        assert "```mermaid" in new_content or "```chart-json" in new_content


# ============================================================
# G. F8 gantt 兜底死变量清理（回归）
# ============================================================


class TestLocalGanttRegression:
    def test_local_gantt_payload_valid_shape(self):
        payload = _generate_local_mermaid_chart(
            "gantt", "施工进度计划", "本工程总工期180天，分基础、主体、装修三个阶段。"
        )
        assert payload is not None
        assert payload["type"] == "gantt"
        code = payload["code"]
        assert code.startswith("gantt")
        assert "dateFormat" in code
        # 日期始终 YYYY-MM-DD（不回退到 "2025-010" 类无效日期）
        import re as _re

        for m in _re.findall(r"\d{4}-\d{2}-\d{2}", code):
            assert len(m) == 10
        # 任务行使用 30d 时长表达
        assert "30d" in code
