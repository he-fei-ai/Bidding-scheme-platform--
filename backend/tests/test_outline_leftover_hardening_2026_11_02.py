"""生成大纲模块遗留问题加固（2026-11-02 第一轮处置）。

覆盖报告《报告_生成大纲模块深度探索与修复_2026-11-02.md》§八 遗留项：
- 遗留 3：L1 纯占位脏名（剥无可剥保持原样）交付前确定性透知——
  `_collect_l1_placeholder_titles` 判定 + `_emit_l1_placeholder_notice` 事件；
- 遗留 4：`export._strip_heading_numbering` 补 ≥4 段点分编号整串剥除
  （"1.2.3.1 标题" 旧实现剥成 "1 标题" 留半截编号）；
- 遗留 5：`_merge_filled_children` 顺序错位检测透知（OUTLINE_L2_ORDER_MISMATCH
  WARNING，纯日志不改合并行为）。

遗留 1（旧 e2e 测试 _user_text 空跑缺陷）在
test_outline_l3_prefix_placeholder_2026_10_17.py 内以反空转断言修正。
"""

from __future__ import annotations

import logging

import pytest

from app.services.ai.workflows_outline_v51 import (
    _collect_l1_placeholder_titles,
    _emit_l1_placeholder_notice,
    _merge_filled_children,
)
from app.services.export import _strip_heading_numbering


# ===========================================================================
# 遗留 3：L1 占位章名终局透知
# ===========================================================================


class TestL1PlaceholderNotice:
    def test_collect_detects_pure_placeholder(self):
        """「专项6」清洗链剥无可剥 → 终局扫描必须命中。"""
        outline = [
            {"title": "专项6", "children": []},
            {"title": "总体施工部署", "children": []},
            "不是字典",
        ]
        bad = _collect_l1_placeholder_titles(outline)
        assert bad == ["专项6"], bad

    def test_collect_legal_titles_zero_hit(self):
        outline = [
            {"title": "总体施工部署"},
            {"title": "质量保证措施和创优计划"},
            {"title": "专项施工方案"},
        ]
        assert _collect_l1_placeholder_titles(outline) == []

    @pytest.mark.asyncio
    async def test_emit_notice_sends_event_on_hit(self):
        events: list[dict] = []

        async def _emitter(evt):
            events.append(evt)

        await _emit_l1_placeholder_notice([{"title": "专项6"}], _emitter, None)
        hits = [e for e in events if e.get("status") == "outline_l1_placeholder"]
        assert hits, events
        assert hits[0]["placeholder_count"] == 1
        assert hits[0]["placeholder_titles"] == ["专项6"]
        assert hits[0]["phase"] == "final"
        # ENHANCE 2026-11-04：error_code 与日志口径一致，供监控聚合
        assert hits[0].get("error_code") == "OUTLINE_L1_PLACEHOLDER_LEAK", hits[0].get("error_code")

    @pytest.mark.asyncio
    async def test_emit_notice_silent_when_clean(self):
        events: list[dict] = []

        async def _emitter(evt):
            events.append(evt)

        await _emit_l1_placeholder_notice([{"title": "总体施工部署"}], _emitter, None)
        assert events == []

    @pytest.mark.asyncio
    async def test_emit_notice_never_raises_on_emitter_failure(self):
        """事件发送抛异常必须被吞掉——透知只是质量提示，不得炸掉交付。"""
        async def _bad_emitter(evt):
            raise RuntimeError("SSE 断了")

        await _emit_l1_placeholder_notice([{"title": "专项6"}], _bad_emitter, None)


# ===========================================================================
# 遗留 4：export 剥号正则 ≥4 段点分防御分支
# ===========================================================================


class TestStripHeadingNumberingFourSegments:
    def test_four_segment_punct_stripped_wholly(self):
        assert _strip_heading_numbering("1.2.3.1、工程质量管控") == "工程质量管控"

    def test_four_segment_space_stripped_wholly(self):
        """旧实现命中 3 段分支剥成「1 施工准备」留半截编号——必须整串剥除。"""
        assert _strip_heading_numbering("1.2.3.1 施工准备") == "施工准备"

    def test_five_segment_stripped(self):
        assert _strip_heading_numbering("1.2.3.4.5、样板引路") == "样板引路"

    def test_existing_forms_regression(self):
        # 1/2/3 段与中文编号形态行为不回退
        assert _strip_heading_numbering("1.2.3、质量目标") == "质量目标"
        assert _strip_heading_numbering("1.2、质量管理") == "质量管理"
        assert _strip_heading_numbering("3、安全生产") == "安全生产"
        assert _strip_heading_numbering("一、编制依据") == "编制依据"
        assert _strip_heading_numbering("第一节、工程概况") == "工程概况"
        assert _strip_heading_numbering("（2）总体实施策划") == "总体实施策划"

    def test_plain_title_untouched(self):
        assert _strip_heading_numbering("混凝土施工工艺") == "混凝土施工工艺"


# ===========================================================================
# 遗留 5：_merge_filled_children 顺序错位检测（纯透知不改行为）
# ===========================================================================


class TestMergeFilledChildrenOrderMismatch:
    def _skel(self, *titles):
        return [
            {"title": t, "description": "", "children": []} for t in titles
        ]

    def test_swapped_titles_emit_warning_behavior_unchanged(self, caplog):
        """骨架与 AI 序列标题对调 → WARNING；合并行为仍按位置覆盖（不变）。"""
        skel = self._skel("分部工程划分", "施工区段安排")
        ai = [
            {"title": "施工区段安排", "description": ""},
            {"title": "分部工程划分", "description": ""},
        ]
        with caplog.at_level(logging.WARNING):
            out = _merge_filled_children(skel, ai)
        # 行为不变：按位置覆盖 AI 标题
        assert [n["title"] for n in out] == ["施工区段安排", "分部工程划分"]
        assert "OUTLINE_L2_ORDER_MISMATCH" in caplog.text

    def test_normal_refinement_no_warning(self, caplog):
        """AI 精化改写（骨架原标题不在 AI 序列中出现）→ 不误报。"""
        skel = self._skel("总体部署", "平面布置")
        ai = [
            {"title": "施工总体部署与任务划分", "description": ""},
            {"title": "施工现场平面布置", "description": ""},
        ]
        with caplog.at_level(logging.WARNING):
            out = _merge_filled_children(skel, ai)
        assert [n["title"] for n in out] == [
            "施工总体部署与任务划分", "施工现场平面布置",
        ]
        assert "OUTLINE_L2_ORDER_MISMATCH" not in caplog.text

    def test_empty_skeleton_titles_no_warning(self, caplog):
        """常规 L2 填充（骨架标题为空待填）→ 无错位信号。"""
        skel = self._skel("", "")
        ai = [
            {"title": "分部工程划分", "description": ""},
            {"title": "施工区段安排", "description": ""},
        ]
        with caplog.at_level(logging.WARNING):
            _merge_filled_children(skel, ai)
        assert "OUTLINE_L2_ORDER_MISMATCH" not in caplog.text
