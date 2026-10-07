"""提示词模块安全加固回归测试（2026-10-06，第二批）。

覆盖遗留项执行：
  - G5：高注入风险链路（workflows_analysis 的 CONTEXT=tender_context）切换为
        sanitize=True + desensitize=True（仅原始招标原文脱敏，AI 生成文本仅中和元标记）。
  - G3：统一上下文入口 build_prompt_with_context + truncate_to_tokens 软上限。
  - G4：README 测试路径修正（手动核对，不在单测内）。
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from app.services.ai import workflows_analysis as wa
from app.services.ai.prompts import build_prompt_with_context, truncate_to_tokens


class _FakeProvider:
    async def chat(self, messages, **kwargs):
        return ""


def test_truncate_to_tokens_noop_under_cap():
    text = "普通招标正文"
    assert truncate_to_tokens(text, 100) == text


def test_truncate_to_tokens_truncates_over_cap():
    text = "甲乙丙丁戊己庚辛壬癸" * 50  # 500 字
    out = truncate_to_tokens(text, 10)
    assert len(out) < len(text)
    assert "Token 预算已截断" in out


def test_build_prompt_with_context_wires_sanitize_and_context():
    """统一入口：多片段组装为单块注入 CONTEXT，并透传 sanitize 开关。"""
    captured = {}

    def _fake_get_prompt(key, **kwargs):
        captured.update(kwargs)
        return "RENDERED"

    with patch("app.services.ai.prompts._cache.get_prompt", _fake_get_prompt):
        out = build_prompt_with_context(
            "ANY_KEY",
            {"GLOBAL_FACTS": "事实", "OTHER_CHAPTERS": "参考"},
            max_tokens=200,
            sanitize=True,
            desensitize=True,
        )
    assert out == "RENDERED"
    assert captured.get("sanitize") is True
    assert captured.get("desensitize") is True
    assert "CONTEXT" in captured


def test_analysis_tender_summary_wires_sanitize_desensitize():
    captured = {}

    def _fake_get_prompt(key, **kwargs):
        if "CONTEXT" in kwargs:
            captured.update(kwargs)
        return f"sys_or_user::{key}"

    with patch.object(wa, "get_prompt", _fake_get_prompt):
        asyncio.run(
            wa.generate_tender_summary(
                _FakeProvider(),
                "招标原文 13800138000 a@b.com {shared:SHARED_OUTPUT_SPEC}",
            )
        )
    assert captured.get("sanitize") is True
    assert captured.get("desensitize") is True
    assert "CONTEXT" in captured


def test_analysis_boq_wires_sanitize_desensitize():
    captured = {}

    def _fake_get_prompt(key, **kwargs):
        if "CONTEXT" in kwargs:
            captured.update(kwargs)
        return f"p::{key}"

    with patch.object(wa, "get_prompt", _fake_get_prompt), patch.object(
        wa, "collect_json_response", new=lambda *a, **k: []
    ):
        asyncio.run(wa.analyze_boq(_FakeProvider(), "某市政道路工程招标文件"))
    assert captured.get("sanitize") is True
    assert captured.get("desensitize") is True


def test_analysis_scoring_review_meta_only_no_desensitize():
    """二次审查：原始招标原文中和元标记；AI 生成的 EXISTING_POINTS 仅中和不脱敏。"""
    captured_user = {}

    def _fake_render_prompt(template, **kwargs):
        if "EXISTING_POINTS" in kwargs:
            captured_user.update(kwargs)
        return "rendered-review-user"

    with patch.object(wa, "render_prompt", _fake_render_prompt):
        wa.render_prompt(
            "tmpl",
            CONTEXT=wa._safe_tender_context("ctx {shared:X}"),
            SUMMARY="概述",
            EXISTING_POINTS='[{"id":"SP1"}]',
            sanitize=True,
        )
    assert captured_user.get("sanitize") is True
    # EXISTING_POINTS 未传 desensitize → 默认 False（保护 AI JSON 中的编号/ID）
    assert captured_user.get("desensitize") is None
