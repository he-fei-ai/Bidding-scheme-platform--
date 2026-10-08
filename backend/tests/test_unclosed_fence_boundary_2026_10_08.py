#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
章节编写模块 — 未闭合普通围栏 × 图表占位符边界 专项回归测试（2026-10-08）

覆盖 BUG-FIX 2026-10-08（P1 图表连带丢失）：
  _strip_unclosed_plain_fences 原实现把「未闭合普通代码块 → 文末」全部删除。
  而 _protect_chart_fences 先行执行后，其后的闭合图表块已变成
  \x00CHARTFENCE{i}\x00 占位符 —— 占位符行不可能属于普通代码块，
  却被「删到文末」分支连带删除，占位符之后的正常正文也全部丢失。

修复语义：
  扫描闭合行时遇到图表占位符行即停 —— 把「围栏起始行 → 占位符前一行」
  视为坏块删除，保留占位符本身及其后内容（图表块 + 正文），并从占位符后
  继续状态机扫描；无占位符边界时维持「删到文末」的既有保守行为。
"""

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_BACKEND_DIR))

import pytest

from app.services.content_cleaning import clean_generated_content, parse_content


class TestUnclosedPlainFenceChartBoundary:
    """未闭合普通围栏 + 其后图表块：图表与后续正文必须保留（CF-01）"""

    def test_unclosed_python_fence_preserves_later_mermaid_chart(self):
        """复现用例：未闭合 ```python + 其后合法 mermaid 块 + 尾部正文。"""
        raw = (
            "本工程采用流水施工方法组织施工。\n\n"
            "```python\n"
            "def calc_schedule():\n"
            "    return total_days\n"
            "本工程进度计划如下所述。\n"
            "```mermaid\n"
            "graph TD\n"
            "  A[开工] --> B[主体施工]\n"
            "  B --> C[竣工验收]\n"
            "```\n\n"
            "竣工验收阶段将组织五方主体进行联合验收。"
        )
        out = clean_generated_content(raw)
        # 1) 首段正文保留
        assert "本工程采用流水施工方法组织施工。" in out
        # 2) 图表块保留（修复前被连坐删除）
        assert "graph TD" in out, "修复后 mermaid 图表块必须保留"
        assert "A[开工] --> B[主体施工]" in out
        # 3) 占位符之后的正文保留
        assert "竣工验收阶段将组织五方主体进行联合验收。" in out
        # 4) 坏块内的 python 代码不泄漏
        assert "def calc_schedule" not in out
        assert "```python" not in out

    def test_boundary_case_yields_chart_marker_block(self):
        """parse_content 视角：边界修复后图表块应产出 chart_marker 而非丢失。"""
        raw = (
            "段落一。\n"
            "```text\n"
            "some config text\n"
            "```mermaid\n"
            "gantt\n"
            "  dateFormat YYYY-MM-DD\n"
            "  title 施工总进度计划\n"
            "  section 主体\n"
            "  主体结构 :a1, 2026-01-01, 30d\n"
            "```\n"
            "段落二。"
        )
        blocks = parse_content(raw)
        kinds = [b.type for b in blocks]
        assert "chart_marker" in kinds, f"图表应产出 chart_marker，实际: {kinds}"
        paras = [b.text for b in blocks if b.type == "paragraph"]
        assert any("段落一" in t for t in paras)
        assert any("段落二" in t for t in paras)
        assert not any("some config text" in t for t in paras), "坏块内容不得泄漏"

    def test_unclosed_tilde_fence_boundary(self):
        """波浪线围栏（~~~python）未闭合 + 其后 mermaid：同样以占位符为界。"""
        raw = (
            "正文甲。\n"
            "~~~python\n"
            "print('hello')\n"
            "```mermaid\n"
            "flowchart LR\n"
            "  X --> Y\n"
            "```\n"
            "正文乙。"
        )
        out = clean_generated_content(raw)
        assert "正文甲。" in out
        assert "flowchart LR" in out, "波浪线坏块后的图表必须保留"
        assert "正文乙。" in out
        assert "print('hello')" not in out

    def test_unclosed_fence_without_chart_boundary_still_truncates_to_eof(self):
        """无占位符边界：维持既有「删到文末」保守行为（不吞图表但代码不泄漏）。"""
        raw = (
            "前文。\n"
            "```python\n"
            "x = 1\n"
            "y = 2\n"
            "后文普通文字。"
        )
        out = clean_generated_content(raw)
        assert "前文。" in out
        assert "x = 1" not in out
        assert "后文普通文字。" not in out  # 既有行为：删到文末

    def test_closed_plain_fence_untouched(self):
        """闭合普通代码块：保留全部行（既有行为回归保护）。"""
        raw = (
            "说明如下。\n"
            "```python\n"
            "print('ok')\n"
            "```\n"
            "结束。"
        )
        out = clean_generated_content(raw)
        assert "print('ok')" in out
        assert "结束。" in out
        # 围栏标记行按既有逻辑剥离
        assert "```python" not in out

    def test_multiple_unclosed_fences_scoped_correctly(self):
        """多个未闭合围栏：各坏块独立定界，中间图表块互不影响。"""
        raw = (
            "开头。\n"
            "```python\n"
            "bad_a = 1\n"
            "```mermaid\n"
            "graph TD\n"
            "  P --> Q\n"
            "```\n"
            "中段。\n"
            "```json\n"
            "{\"k\": 1\n"
            "```mermaid\n"
            "flowchart TD\n"
            "  R --> S\n"
            "```\n"
            "结尾。"
        )
        out = clean_generated_content(raw)
        assert "graph TD" in out
        assert "flowchart TD" in out
        assert "开头。" in out and "中段。" in out and "结尾。" in out
        assert "bad_a = 1" not in out
        assert '{"k": 1' not in out


class TestChartNumberingRegressionGuards:
    """图表编号链路回归保护（导出端约定，防退化）"""

    def test_rewind_figure_number_restores_sequence(self):
        """渲染失败回滚图号后，下一张图复用该号（图号不虚跳）。"""
        from app.services.export import (
            _next_figure_number,
            _rewind_figure_number,
            _reset_figure_counter,
        )
        _reset_figure_counter(project_id=99001)
        try:
            n1 = _next_figure_number("3.2", 99001)
            assert n1 == "图3-1"
            n2 = _next_figure_number("3.2", 99001)
            assert n2 == "图3-2"
            # 模拟渲染失败回滚
            _rewind_figure_number("3.2", 99001)
            n3 = _next_figure_number("3.2", 99001)
            assert n3 == "图3-2", "回滚后下一张图应复用图3-2，不得虚跳"
            # 项目桶隔离
            other = _next_figure_number("3.2", 99002)
            assert other == "图3-1", "不同项目桶互不串号"
        finally:
            _reset_figure_counter(project_id=99001)
            _reset_figure_counter(project_id=99002)
