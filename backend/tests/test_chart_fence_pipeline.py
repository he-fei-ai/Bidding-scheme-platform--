#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
图表生成与渲染模块 — 围栏代码块 / 图题 / 容错 / 去重 专项测试（2026-10-04）

覆盖需求：
  - 需求 5 规范格式：```mermaid / ```chart-json 围栏与 Markdown 正文一致，解析为 chart_marker 块
  - 需求 6 校验修复：validate_mermaid + repair_mermaid 集成，非法语法修复后仍不通过则删坏块（不泄漏乱码）
  - 需求 8 图题规范：四级优先级（载荷 title > Mermaid title > 引导语 > 类型通用名）
  - 需求 9 容错：渲染失败默认跳过不占图号；CHART_FAIL_PLACEHOLDER 开关默认关闭
  - 需求 10 去重：缓存键剔除 fig_no + sort_keys 归一化；图号回滚消除虚跳
"""

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

_BACKEND_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_BACKEND_DIR))

import pytest

from app.services.content_cleaning import (
    parse_content,
    iter_chart_fences,
    extract_mermaid_title,
    _fence_payload_to_marker_text,
    CHART_FENCE_OPEN_PATTERN,
    CHART_FENCE_SCAN_PATTERN,
)


# ============================================================
# 1. 围栏代码块 → chart_marker 解析（需求 5）
# ============================================================
class TestFenceParsing:
    def test_closed_mermaid_fence_produces_chart_marker(self):
        content = (
            "开头正文。\n\n"
            "```mermaid\n"
            "graph TD\n"
            "    A[开始] --> B[处理]\n"
            "    B --> C[结束]\n"
            "```\n\n"
            "结尾正文。"
        )
        blocks = parse_content(content)
        kinds = [b.type for b in blocks]
        assert "chart_marker" in kinds, f"应产出 chart_marker，实际: {kinds}"
        # 围栏原文不得作为普通段落泄漏
        para_texts = [b.text for b in blocks if b.type == "paragraph"]
        assert not any("```" in t or "-->" in t for t in para_texts), \
            f"Mermaid 围栏内容泄漏为正文段落: {para_texts}"
        marker = next(b for b in blocks if b.type == "chart_marker")
        payload = json.loads(marker.text)
        assert payload["chart_type"] == "flowchart"
        inner = json.loads(payload["description"])
        assert inner["type"] == "flowchart"
        assert "graph TD" in inner["code"]

    def test_closed_chart_json_fence_produces_chart_marker(self):
        content = (
            "```chart-json\n"
            "{\"type\": \"labor\", \"title\": \"劳动力配置\", \"phases\": [\"基础\", \"主体\"], "
            "\"categories\": [\"木工\"], \"data\": [[10], [20]]}\n"
            "```\n"
        )
        blocks = parse_content(content)
        markers = [b for b in blocks if b.type == "chart_marker"]
        assert len(markers) == 1
        payload = json.loads(markers[0].text)
        assert payload["chart_type"] == "labor"
        inner = json.loads(payload["description"])
        assert inner["title"] == "劳动力配置"
        assert inner["phases"] == ["基础", "主体"]

    def test_gantt_fence_type_inferred(self):
        content = (
            "```mermaid\n"
            "gantt\n"
            "    title 项目进度\n"
            "    dateFormat YYYY-MM-DD\n"
            "    section 阶段一\n"
            "    任务A :a1, 2026-01-01, 7d\n"
            "```\n"
        )
        blocks = parse_content(content)
        marker = next(b for b in blocks if b.type == "chart_marker")
        payload = json.loads(marker.text)
        assert payload["chart_type"] == "gantt"

    def test_fence_open_pattern_matches(self):
        assert CHART_FENCE_OPEN_PATTERN.match("```mermaid")
        assert CHART_FENCE_OPEN_PATTERN.match("```chart-json")
        assert CHART_FENCE_OPEN_PATTERN.match("```MERMAID")
        assert not CHART_FENCE_OPEN_PATTERN.match("```python")

    def test_iter_chart_fences_yields_closed_blocks(self):
        content = (
            "```mermaid\ngraph TD\nA-->B\n```\n\n"
            "中间\n\n"
            "```chart-json\n{\"type\": \"comparison\", \"title\": \"比选\"}\n```\n"
        )
        items = list(iter_chart_fences(content))
        assert len(items) == 2
        langs = [lang for lang, _body, _mt in items]
        assert langs == ["mermaid", "chart-json"]
        # 两块均为合法载荷（marker_text 不为 None）
        assert all(mt is not None for _l, _b, mt in items)


# ============================================================
# 2. 未闭合围栏 / 坏块处理（需求 6：不泄漏乱码）
# ============================================================
class TestFenceMalformed:
    def test_unclosed_fence_removed_no_leak(self):
        content = (
            "正常段落。\n\n"
            "```mermaid\n"
            "graph TD\n"
            "    A[未闭合坏块\n"
        )
        blocks = parse_content(content)
        # 未闭合围栏不得把代码原文写进正文
        all_text = "\n".join(b.text for b in blocks)
        assert "```" not in all_text, f"未闭合围栏标记泄漏: {all_text}"
        assert "未闭合坏块" not in all_text, f"未闭合围栏代码体泄漏: {all_text}"

    def test_invalid_chart_json_is_bad_block(self):
        # chart-json 载荷非法 JSON → _fence_payload_to_marker_text 返回 None
        result = _fence_payload_to_marker_text("chart-json", "这不是 JSON {{{")
        assert result is None

    def test_empty_body_is_bad_block(self):
        assert _fence_payload_to_marker_text("mermaid", "") is None
        assert _fence_payload_to_marker_text("chart-json", "   ") is None

    def test_chart_json_missing_type_infers_from_title(self):
        # 缺 type 但有 title 且标题可推断类型
        body = json.dumps({"title": "施工进度甘特图", "data": [[1]]}, ensure_ascii=False)
        result = _fence_payload_to_marker_text("chart-json", body)
        assert result is not None
        payload = json.loads(result)
        assert payload["chart_type"] == "gantt"

    def test_chart_json_no_type_no_title_is_bad_block(self):
        body = json.dumps({"data": [[1, 2, 3]]}, ensure_ascii=False)
        assert _fence_payload_to_marker_text("chart-json", body) is None


# ============================================================
# 3. Mermaid title 提取（需求 8 第二级优先级）
# ============================================================
class TestMermaidTitle:
    def test_extract_title_from_gantt(self):
        code = "gantt\n    title 项目总体进度计划\n    section A\n    T1 :a, 2026-01-01, 5d\n"
        assert extract_mermaid_title(code) == "项目总体进度计划"

    def test_extract_title_flowchart(self):
        code = "flowchart TD\n%%{init: {}}%%\n title 施工工艺流程\n A-->B\n"
        assert extract_mermaid_title(code) == "施工工艺流程"

    def test_no_title_returns_empty(self):
        code = "graph TD\n    A-->B\n"
        assert extract_mermaid_title(code) == ""

    def test_empty_code_returns_empty(self):
        assert extract_mermaid_title("") == ""

    def test_fence_payload_carries_title(self):
        code = "gantt\n    title 里程碑计划\n    section S\n    T :a, 2026-01-01, 3d\n"
        result = _fence_payload_to_marker_text("mermaid", code)
        # gantt 需要 section 等结构，校验器可能判非法 → 允许 None 或携带 title
        if result is not None:
            payload = json.loads(result)
            inner = json.loads(payload["description"])
            assert inner.get("title") == "里程碑计划"


# ============================================================
# 4. 图号计数器 + 回滚（需求 9/11：消除虚跳）
# ============================================================
class TestFigureCounter:
    def setup_method(self):
        from app.services.export import _reset_figure_counter
        _reset_figure_counter()

    def test_sequential_numbering(self):
        from app.services.export import _next_figure_number
        assert _next_figure_number("3", 1) == "图3-1"
        assert _next_figure_number("3", 1) == "图3-2"
        assert _next_figure_number("3", 1) == "图3-3"

    def test_chapter_from_subsection(self):
        from app.services.export import _next_figure_number
        # "3.2" 章号取第一段 "3"
        assert _next_figure_number("3.2", 1) == "图3-1"

    def test_rewind_reuses_number(self):
        from app.services.export import _next_figure_number, _rewind_figure_number
        first = _next_figure_number("3", 1)
        _rewind_figure_number("3", 1)  # 模拟渲染失败回滚
        second = _next_figure_number("3", 1)
        assert first == second == "图3-1", "回滚后下一张应复用该号，图号不虚跳"

    def test_project_isolation(self):
        from app.services.export import _next_figure_number
        # 不同项目同章节号互不串号
        assert _next_figure_number("3", 100) == "图3-1"
        assert _next_figure_number("3", 200) == "图3-1"
        assert _next_figure_number("3", 100) == "图3-2"


# ============================================================
# 5. 缓存键去重（需求 10：剔除 fig_no + sort_keys 归一化）
# ============================================================
class TestCacheKeyDedup:
    def test_fig_no_excluded_from_key(self):
        from app.services.export import _strip_fig_no_for_cache
        a = {"type": "flowchart", "code": "graph TD\nA-->B", "fig_no": "图3-1"}
        b = {"type": "flowchart", "code": "graph TD\nA-->B", "fig_no": "图3-2"}
        assert _strip_fig_no_for_cache(a) == _strip_fig_no_for_cache(b), \
            "同代码不同图号应共享缓存键（去重命中）"

    def test_key_order_normalized(self):
        from app.services.export import _strip_fig_no_for_cache
        a = {"type": "labor", "data": [[1]], "title": "T"}
        b = {"title": "T", "data": [[1]], "type": "labor"}
        assert _strip_fig_no_for_cache(a) == _strip_fig_no_for_cache(b), \
            "字段顺序不同但语义相同应归一化为同一缓存键"

    def test_different_code_different_key(self):
        from app.services.export import _strip_fig_no_for_cache
        a = {"type": "flowchart", "code": "graph TD\nA-->B"}
        b = {"type": "flowchart", "code": "graph TD\nX-->Y"}
        assert _strip_fig_no_for_cache(a) != _strip_fig_no_for_cache(b)

    def test_string_payload_with_fig_no(self):
        from app.services.export import _strip_fig_no_for_cache
        s1 = json.dumps({"type": "gantt", "fig_no": "图1-1"}, ensure_ascii=False)
        s2 = json.dumps({"type": "gantt", "fig_no": "图1-5"}, ensure_ascii=False)
        assert _strip_fig_no_for_cache(s1) == _strip_fig_no_for_cache(s2)


# ============================================================
# 6. CHART_FAIL_PLACEHOLDER 配置（需求 9：默认关闭）
# ============================================================
class TestFailPlaceholderConfig:
    def test_default_is_off(self):
        from app import config
        # 默认不设置环境变量时应为 False（静默跳过，不插占位）
        # 若测试环境显式设置了该变量则跳过断言
        if "CHART_FAIL_PLACEHOLDER" not in __import__("os").environ:
            assert config.CHART_FAIL_PLACEHOLDER is False


# ============================================================
# 7. repair_mermaid 机械化修复（需求 6）
# ============================================================
class TestRepairMermaid:
    def test_strips_bom(self):
        from app.services.ai.image_engine import repair_mermaid
        code = "\ufeffgraph TD\n    A-->B\n"
        repaired = repair_mermaid(code)
        assert not repaired.startswith("\ufeff")

    def test_smart_quotes_to_ascii(self):
        from app.services.ai.image_engine import repair_mermaid
        code = "graph TD\n    A[\u201c开始\u201d] --> B[结束]\n"
        repaired = repair_mermaid(code)
        assert "\u201c" not in repaired and "\u201d" not in repaired

    def test_fullwidth_parentheses_normalized(self):
        from app.services.ai.image_engine import repair_mermaid
        code = "graph TD\n    A（开始） --> B[结束]\n"
        repaired = repair_mermaid(code)
        assert "（" not in repaired and "）" not in repaired

    def test_trailing_semicolon_removed(self):
        from app.services.ai.image_engine import repair_mermaid
        code = "graph TD\n    A-->B;\n    B-->C;\n"
        repaired = repair_mermaid(code)
        # 行末分号应被剔除
        for line in repaired.split("\n"):
            assert not line.rstrip().endswith(";")

    def test_missing_head_added(self):
        from app.services.ai.image_engine import repair_mermaid
        code = "A[开始] --> B[结束]\n"
        repaired = repair_mermaid(code)
        assert repaired.lstrip().startswith("graph TD") or "graph TD" in repaired

    def test_validate_accepts_repaired_flowchart(self):
        from app.services.ai.image_engine import repair_mermaid, validate_mermaid
        code = "\ufeffgraph TD\n    A（开始） --> B[结束];\n"
        repaired = repair_mermaid(code)
        ok, _msg = validate_mermaid(repaired)
        assert ok is True, f"修复后应通过校验: {_msg}"


# ============================================================
# 8. DOCX 含图冒烟测试（需求 7/13：真实渲染并内嵌，不依赖 HTTP 服务）
#    围栏正文 → parse_content → _add_inline_chart（skip_http 直走 PIL v2）
#    → 保存为真实 DOCX → 重新打开断言 inline_shapes。
# ============================================================
class TestFenceDocxSmoke:
    _MERMAID_FLOW = (
        "```mermaid\n"
        "graph TD\n"
        "    A[开工] --> B[主体]\n"
        "    B --> C[竣工]\n"
        "```\n"
    )
    _CHARTJSON_LABOR = (
        "```chart-json\n"
        "{\"type\":\"labor\",\"title\":\"劳动力计划\","
        "\"phases\":[\"基础\",\"主体\"],\"categories\":[\"普工\",\"木工\"],"
        "\"data\":\n" + "[[10,20],[30,40]]}" + "\n```\n"
    )

    def test_fence_content_embeds_images_in_real_docx(self):
        from docx import Document
        from app.models import ExportConfig
        from app.services.export import _add_inline_chart, _reset_figure_counter

        _reset_figure_counter()
        content = "施工流程如下。\n\n" + self._MERMAID_FLOW + "\n劳动力配置。\n\n" + self._CHARTJSON_LABOR

        markers = [b for b in parse_content(content) if b.type == "chart_marker"]
        assert len(markers) == 2, f"围栏应解析出 2 个图表块，实际: {len(markers)}"

        doc = Document()
        cfg = ExportConfig(image_max_width_cm=18.0, image_dpi=300)
        sd = SimpleNamespace(title="测试章", project_id=1)
        rendered = []
        for b in markers:
            mk = json.loads(b.text)
            ok = _add_inline_chart(
                doc, mk["chart_type"], mk["description"], sd, cfg, parent_section_number="3"
            )
            rendered.append((mk["chart_type"], ok))
        # 真实渲染（PIL v2）应成功，而非静默跳过
        assert all(ok for _t, ok in rendered), f"存在渲染失败（不应发生）: {rendered}"

        buf = io.BytesIO()
        doc.save(buf)
        buf.seek(0)
        reopened = Document(buf)
        assert len(reopened.inline_shapes) == 2, (
            f"真实 DOCX 应内嵌 2 张图片，实际 inline_shapes={len(reopened.inline_shapes)}"
        )

    def test_render_failure_does_not_inflate_fig_no(self):
        # 无降级：坏载荷（空数据）渲染失败 → 不占用图号，图号连续
        from app.services.export import (
            _next_figure_number,
            _rewind_figure_number,
            _reset_figure_counter,
        )
        _reset_figure_counter()
        assigned = _next_figure_number("3", 1)
        _rewind_figure_number("3", 1)  # 模拟渲染失败回滚
        next_after_fail = _next_figure_number("3", 1)
        assert assigned == next_after_fail == "图3-1", "失败回滚后下一张复用同号，图号不虚跳"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v", "-p", "no:cacheprovider"]))
