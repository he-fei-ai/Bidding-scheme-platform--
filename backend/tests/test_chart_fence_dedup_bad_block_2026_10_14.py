#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
图表围栏「解析侧 / 去重侧」口径一致性回归测试（2026-10-14）。

背景（BUG-FIX 2026-10-14 P0 的核心不变量）：
  图表围栏的判定口径统一收敛到单一权威规划器 `plan_chart_fence_lines`，
  解析侧（`_protect_chart_fences` / `parse_content`）与检测侧
  （`iter_chart_fences` → `export_chart_pipeline._detect_inline_chart_markers`）
  共用同一份状态机，必须**口径一致**：

    - 已闭合（block）围栏：解析侧保护→还原→就地渲染成 chart_marker（计入「已内联」）；
      检测侧 `iter_chart_fences` 产出该类型 → 去重侧计入 → 章末不再补图。
    - 未闭合/病态（bad）围栏：解析侧（`_protect_chart_fences`）直接**删除**、
      不渲染成图；检测侧 `iter_chart_fences` **不产出** → 去重侧**不计入** →
      章末仍可据 DB 绑定补图（不丢图）。

  两侧一致即「凡检测侧识别出的类型，parse_content 必渲染；凡 parse_content
  不渲染的，检测侧必不识别」。一旦分叉：
    - 解析侧删块、检测侧却计入  → 章末误判「已内联」而漏补 → **丢图**。
    - 解析侧渲染、检测侧却不计入 → 章末再补一张 → **重复图**。

  本测试钉住两侧口径一致，防止误把「未闭合围栏」计入去重（导致丢图）或漏计
  「已闭合围栏」（导致重复图）。
"""

import json
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).parent.parent
sys.path.insert(0, str(_BACKEND_DIR))

from app.services.content_cleaning import parse_content, iter_chart_fences
from app.services.export_chart_pipeline import _detect_inline_chart_markers


# ── 夹具 ────────────────────────────────────────────────────────────────
# 已闭合、载荷合法（labor）的 chart-json 围栏：解析侧渲染、检测侧计入。
CLOSED_VALID_CHART_JSON = (
    "引导正文段落。\n\n"
    "```chart-json\n"
    '{"type": "labor", "title": "劳动力配置", "phases": ["基础"], '
    '"categories": ["木工"], "data": [[10]]}\n'
    "```\n"
)

# 未闭合（缺结尾 ```）、载荷合法（labor）的 chart-json 围栏：
# 解析侧删除（不渲染）、检测侧不计入（章末仍可补图，不丢图）。
BAD_VALID_CHART_JSON = (
    "引导正文段落。\n\n"
    "```chart-json\n"
    '{"type": "labor", "title": "劳动力配置", "phases": ["基础"], '
    '"categories": ["木工"], "data": [[10]]}\n'
    # 故意缺少闭合 ``` → 规划器判为 bad（未闭合）
)

# 未闭合且载荷非法（非 JSON）的 chart-json 围栏：解析侧删除、检测侧不计入。
BAD_INVALID_CHART_JSON = (
    "引导正文段落。\n\n"
    "```chart-json\n"
    "这不是合法 JSON {{{\n"
)


def _fence_types(content: str) -> set[str]:
    """检测侧：iter_chart_fences 识别出的有效（marker 非 None）围栏图表类型。"""
    types: set[str] = set()
    for _lang, _body, marker_text in iter_chart_fences(content):
        if marker_text is None:
            continue
        try:
            ft = str(json.loads(marker_text).get("chart_type") or "").lower()
        except (json.JSONDecodeError, TypeError):
            continue
        if ft:
            types.add(ft)
    return types


def _rendered_chart_types(content: str) -> set[str]:
    """解析侧：parse_content 实际渲染出的 chart_marker 图表类型。"""
    types: set[str] = set()
    for b in parse_content(content):
        if b.type != "chart_marker":
            continue
        try:
            ft = str(json.loads(b.text).get("chart_type") or "").lower()
        except (json.JSONDecodeError, TypeError):
            continue
        if ft:
            types.add(ft)
    return types


class TestClosedFenceBothSidesAgree:
    """已闭合围栏：解析侧渲染 + 检测侧计入 → 两侧一致（「已内联」）。"""

    def test_parse_renders_closed_fence(self):
        assert "labor" in _rendered_chart_types(CLOSED_VALID_CHART_JSON)

    def test_iter_yields_closed_fence(self):
        assert "labor" in _fence_types(CLOSED_VALID_CHART_JSON)

    def test_dedup_counts_closed_fence(self):
        all_types, _hints = _detect_inline_chart_markers(CLOSED_VALID_CHART_JSON)
        assert "labor" in all_types


class TestBadFenceBothSidesAgree:
    """未闭合围栏：解析侧删除（不渲染）+ 检测侧不计入 → 两侧一致（「未内联」）。

    关键回归守卫：检测侧**不得**把未闭合围栏计入「已内联」，否则会与解析侧
    （删除该围栏、不渲染）分叉 → 章末误判已内联而漏补 → **丢图**。
    """

    def test_parse_does_not_render_bad_fence(self):
        assert "labor" not in _rendered_chart_types(BAD_VALID_CHART_JSON)

    def test_iter_does_not_yield_bad_fence(self):
        assert "labor" not in _fence_types(BAD_VALID_CHART_JSON)

    def test_dedup_does_not_count_bad_fence(self):
        all_types, _hints = _detect_inline_chart_markers(BAD_VALID_CHART_JSON)
        assert "labor" not in all_types, (
            "未闭合围栏在解析侧被删除、不渲染成图，去重侧不得计入，"
            "否则章末误判已内联而漏补 → 丢图"
        )

    def test_dedup_ignores_bad_fence_invalid_payload(self):
        all_types, _hints = _detect_inline_chart_markers(BAD_INVALID_CHART_JSON)
        assert "labor" not in all_types


class TestSidesNeverDiverge:
    """P0 不变量：对围栏而言，检测侧识别集 ⊆ 解析侧渲染集，且 bad 围栏两侧都为空。"""

    def test_fence_types_subset_of_rendered(self):
        for content in (CLOSED_VALID_CHART_JSON, BAD_VALID_CHART_JSON, BAD_INVALID_CHART_JSON):
            fence_types = _fence_types(content)
            rendered = _rendered_chart_types(content)
            # 检测侧识别出的围栏类型，解析侧必然渲染（否则去重会误判、章末漏补 → 丢图）
            assert fence_types <= rendered, (
                f"检测侧识别 {fence_types} 但解析侧只渲染 {rendered} → 口径分叉"
            )
