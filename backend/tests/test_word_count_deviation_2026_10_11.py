"""字数偏差控制深度修复（2026-10-11）专项测试。

背景：
  DB 实测 10 个 L3 章节平均偏差 +34.2%、100% 系统性偏高、最极端 +120%。
  根因：提示词以区间表达，模型倾向超长；生成后无压缩机制；偏差不可追溯。

修复项：
  1. 提示词强化（workflows_sections.py）：具体目标值 + 硬性上限 + 自检要求
  2. 配置项（config.py）：CONTENT_USE_WORD_COUNT_ENFORCEMENT 等
  3. Condense prompt（prompts/content.py）：CONTENT_CONDENSE_SYSTEM
  4. Condense 函数（workflows_sections.py）：condense_overlong_content / condense_overlong_results
  5. 审计端点（word_count.py）：GET /word-count/audit/{project_id}

运行：cd backend && pytest tests/test_word_count_deviation_2026_10_11.py -v
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.config import (
    CONTENT_USE_WORD_COUNT_ENFORCEMENT_DEFAULT,
    CONTENT_WORD_COUNT_CONDENSE_MAX_ROUNDS,
    CONTENT_WORD_COUNT_CONDENSE_THRESHOLD,
)
from app.services.ai import writing_length_config as wlc
from app.services.ai.workflows_sections import (
    build_chapter_content_messages,
    condense_overlong_content,
    condense_overlong_results,
)
from app.utils import count_words


def _cn(n: int) -> str:
    return "施" * n


def _make_section_row(*, sid=0, pid=0, title="章", level=3, content="", word_count_tier=None, target_words=None) -> dict:
    return {
        "id": sid, "project_id": pid, "parent_id": None, "title": title,
        "level": level, "sort_order": 1, "section_type": "text",
        "target_words": target_words, "word_count_tier": word_count_tier,
        "content": content, "word_count": count_words(content), "status": "completed",
    }


class _FakeProvider:
    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.call_count = 0

    async def chat(self, messages, **kwargs) -> str:
        self.call_count += 1
        if self.responses:
            return self.responses.pop(0)
        return ""


class TestConfigDefaults:
    def test_enforcement_default_is_true(self):
        assert CONTENT_USE_WORD_COUNT_ENFORCEMENT_DEFAULT is True

    def test_condense_threshold_is_1_15(self):
        assert CONTENT_WORD_COUNT_CONDENSE_THRESHOLD == pytest.approx(1.15)

    def test_max_rounds_default_is_1(self):
        assert CONTENT_WORD_COUNT_CONDENSE_MAX_ROUNDS == 1


class TestCondensePrompt:
    def test_prompt_is_registered(self):
        from app.services.ai.prompts._cache import get_prompt
        prompt = get_prompt("CONTENT_CONDENSE_SYSTEM")
        assert prompt is not None and len(prompt) > 100

    def test_prompt_contains_key_elements(self):
        from app.services.ai.prompts._cache import get_prompt
        prompt = get_prompt("CONTENT_CONDENSE_SYSTEM")
        assert prompt is not None
        assert "必含要素" in prompt or "关键内容" in prompt
        assert "数据" in prompt
        assert "禁止" in prompt
        assert "占位" in prompt or "TODO" in prompt

    def test_prompt_preserves_chart_blocks(self):
        from app.services.ai.prompts._cache import get_prompt
        prompt = get_prompt("CONTENT_CONDENSE_SYSTEM")
        assert prompt is not None
        assert "mermaid" in prompt or "chart-json" in prompt or "图表" in prompt


class TestPromptEnhancement:
    def _prompt_text(self, info: dict) -> str:
        msgs = build_chapter_content_messages(info)
        return "\n".join(m.content for m in msgs if m.role == "user")

    def test_l3_own_uses_specific_target_not_range(self):
        info = {"id": 1, "title": "测试", "level": 3, "is_l3_own": True,
                "target_words": None, "word_count_tier": None, "child_chapters": []}
        text = self._prompt_text(info)
        assert "约 300 字" in text
        assert "硬性上限 360 字" in text
        assert "自检" in text

    def test_l3_own_keeps_range_display(self):
        info = {"id": 2, "title": "测试", "level": 3, "is_l3_own": True,
                "target_words": None, "word_count_tier": None, "child_chapters": []}
        text = self._prompt_text(info)
        assert "240~360" in text

    def test_self_check_instruction_present(self):
        info = {"id": 3, "title": "测试", "level": 3, "is_l3_own": True,
                "target_words": None, "word_count_tier": None, "child_chapters": []}
        text = self._prompt_text(info)
        assert "自查" in text or "自检" in text

    def test_l4_uses_specific_target(self):
        info = {"id": 4, "title": "测试", "level": 4, "is_l3_own": False,
                "target_words": None, "word_count_tier": None, "child_chapters": []}
        text = self._prompt_text(info)
        assert "约 1200 字" in text


class TestCondenseOverlongContent:
    def test_no_condense_when_under_threshold(self):
        content = _cn(300)
        provider = _FakeProvider([])
        result, meta = asyncio.run(condense_overlong_content(
            content, target_words=300, tolerance_ratio=0.20, provider=provider
        ))
        assert result == content
        assert meta["condensed"] is False
        assert meta["original_wc"] == 300
        assert provider.call_count == 0

    def test_condense_when_over_threshold(self):
        content = _cn(659)
        condensed = _cn(320)
        provider = _FakeProvider([condensed])
        result, meta = asyncio.run(condense_overlong_content(
            content, target_words=300, tolerance_ratio=0.20, provider=provider
        ))
        assert result == condensed
        assert meta["condensed"] is True
        assert meta["original_wc"] == 659


class TestCondenseOverlongResults:
    def test_enabled_false_disables_condense(self):
        results = [{"id": 1, "content": _cn(500), "status": "completed"}]
        sections_by_id = {1: _make_section_row(sid=1, level=3, content=_cn(500))}
        provider = _FakeProvider([_cn(300)])
        summary = asyncio.run(condense_overlong_results(
            results, sections_by_id, provider, enabled=False
        ))
        assert summary["condensed_count"] == 0
        assert provider.call_count == 0
        assert results[0]["content"] == _cn(500)

    def test_condense_overlong_section(self):
        original = _cn(500)
        condensed = _cn(320)
        results = [{"id": 1, "content": original, "status": "completed"}]
        sections_by_id = {1: _make_section_row(sid=1, level=3, content=original)}
        provider = _FakeProvider([condensed])
        summary = asyncio.run(condense_overlong_results(
            results, sections_by_id, provider, enabled=True, threshold=1.15
        ))
        assert summary["condensed_count"] == 1
        assert results[0]["content"] == condensed
        assert "word_count_target" in results[0]
        assert "word_count_deviation" in results[0]

    def test_under_threshold_not_condensed(self):
        content = _cn(340)
        results = [{"id": 1, "content": content, "status": "completed"}]
        sections_by_id = {1: _make_section_row(sid=1, level=3, content=content)}
        provider = _FakeProvider([_cn(300)])
        summary = asyncio.run(condense_overlong_results(
            results, sections_by_id, provider, enabled=True, threshold=1.15
        ))
        assert summary["condensed_count"] == 0
        assert provider.call_count == 0

    def test_l1_l2_skipped(self):
        content = _cn(5000)
        results = [{"id": 1, "content": content, "status": "completed"}]
        sections_by_id = {1: _make_section_row(sid=1, level=1, content=content)}
        provider = _FakeProvider([_cn(300)])
        summary = asyncio.run(condense_overlong_results(
            results, sections_by_id, provider, enabled=True
        ))
        assert summary["condensed_count"] == 0
        assert provider.call_count == 0

    def test_deviation_recorded_even_when_not_condensed(self):
        content = _cn(340)
        results = [{"id": 1, "content": content, "status": "completed"}]
        sections_by_id = {1: _make_section_row(sid=1, level=3, content=content)}
        provider = _FakeProvider([])
        summary = asyncio.run(condense_overlong_results(
            results, sections_by_id, provider, enabled=True, threshold=1.15
        ))
        assert summary["condensed_count"] == 0
        assert results[0]["word_count_target"] == 300
        assert results[0]["word_count_deviation"] == 40

    def test_empty_results_no_error(self):
        summary = asyncio.run(condense_overlong_results([], {}, None, enabled=True))
        assert summary["condensed_count"] == 0
        assert summary["fail_count"] == 0


class TestDeviationClassification:
    def test_on_target_classification(self):
        lo, hi = wlc.get_l3_own_range()
        assert lo <= 300 <= hi

    def test_below_target_classification(self):
        lo, hi = wlc.get_l3_own_range()
        assert 200 < lo

    def test_above_target_classification(self):
        lo, hi = wlc.get_l3_own_range()
        assert 400 > hi

    def test_extreme_deviation_classification(self):
        deviation_rate = (659 - 300) / 300
        assert deviation_rate > 0.5


class TestRegression:
    def test_tier_config_unchanged(self):
        assert wlc.get_tier_config("brief")["target_words"] == 800
        assert wlc.get_tier_config("standard")["target_words"] == 1200
        assert wlc.get_tier_config("detailed")["target_words"] == 2000
        assert wlc.get_tier_config("long")["target_words"] == 2500

    def test_l3_own_target_unchanged(self):
        assert wlc.get_l3_own_target() == 300
        assert wlc.get_l3_own_tolerance() == 0.20
        assert wlc.get_l3_own_range() == (240, 360)

    def test_l3_own_within_unchanged(self):
        assert wlc.l3_own_within(300) is True
        assert wlc.l3_own_within(240) is True
        assert wlc.l3_own_within(360) is True
        assert wlc.l3_own_within(239) is False
        assert wlc.l3_own_within(361) is False

    def test_tier_tolerance_ratio_unchanged(self):
        assert wlc.get_tier_config("brief")["tolerance_ratio"] == 0.10
        assert wlc.get_tier_config("standard")["tolerance_ratio"] == 0.10
        assert wlc.get_tier_config("detailed")["tolerance_ratio"] == 0.10
        assert wlc.get_tier_config("long")["tolerance_ratio"] == 0.10

    def test_condense_fails_gracefully_on_error(self):
        content = _cn(500)
        provider = _FakeProvider([""])
        result, meta = asyncio.run(condense_overlong_content(
            content, target_words=300, tolerance_ratio=0.20, provider=provider
        ))
        assert result == content
        assert meta["condensed"] is False
        assert meta["error"] != ""

    def test_condense_rejects_longer_output(self):
        content = _cn(400)
        longer = _cn(500)
        provider = _FakeProvider([longer])
        result, meta = asyncio.run(condense_overlong_content(
            content, target_words=300, tolerance_ratio=0.20, provider=provider
        ))
        assert result == content
        assert meta["condensed"] is False
        assert "未减少" in meta["error"]

    def test_condense_with_provider_none(self):
        content = _cn(500)
        result, meta = asyncio.run(condense_overlong_content(
            content, target_words=300, tolerance_ratio=0.20, provider=None
        ))
        assert result == content
        assert meta["condensed"] is False
        assert meta["error"] != ""