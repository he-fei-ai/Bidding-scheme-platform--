# -*- coding: utf-8 -*-
"""正文生成·续写衔接加固回归测试（ENHANCE 2026-10-06）。

覆盖两项增强：
- 接缝去重（_strip_seam_overlap / _auto_continue_on_length）：finish_reason=='length'
  被截断后续写时，模型常把「已写内容末段」整段重发，造成正文重复。续写循环须剥离
  extra 前缀中与 final_content 尾部重叠的重复段，且不破坏「无缝衔接」设计。
- 续写上下文截断（_MAX_CONTINUE_CONTEXT_CHARS）：超长章节逐轮把全部已写内容回灌会撑爆
  上下文窗口；续写只需「最近尾部」即可衔接，故超过上限只取末尾片段。
"""
from __future__ import annotations

import asyncio

import pytest

import app.services.ai.workflows_sections as ws


# ---------------------------------------------------------------------------
# 接缝去重纯函数
# ---------------------------------------------------------------------------

def test_strip_seam_overlap_no_overlap():
    assert ws._strip_seam_overlap("旧正文结束。", "新内容开始。") == "新内容开始。"


def test_strip_seam_overlap_full_overlap():
    # extra 与尾部完全一致 → 整段剥离
    assert ws._strip_seam_overlap("第一段。第二段。", "第二段。") == ""


def test_strip_seam_overlap_partial_seam():
    final = "第一段内容。第二段内容是重点。"
    extra = "第二段内容是重点。第三段新内容。"
    out = ws._strip_seam_overlap(final, extra)
    # 接缝处「第二段内容是重点。」被剥离，仅留新内容
    assert out == "第三段新内容。"
    # 拼接后不应出现该句的重复
    merged = final.rstrip() + out
    assert "第二段内容是重点。第二段内容是重点。" not in merged
    assert "第三段新内容。" in merged


def test_strip_seam_overlap_only_within_tail_window():
    # 重叠段若超出尾部窗口（>_MAX_SEAM_OVERLAP）则不被剥离，避免过度裁剪
    long_final = "X" * 500 + "唯一长句标记"
    extra = "唯一长句标记"  # 仅 6 字，远小于窗口，应被剥离
    assert ws._strip_seam_overlap(long_final, extra) == ""


# ---------------------------------------------------------------------------
# 续写循环：接缝去重端到端
# ---------------------------------------------------------------------------

class _OverlapProvider:
    """返回与已写末段重叠的续写内容，finish_reason 先 length 后 stop。"""

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, **options):
        self.calls += 1
        if options.get("return_meta"):
            finish = "length" if self.calls < 2 else "stop"
            # 第 1 轮续写重发了已写末段「第二段内容是重点。」再接新内容
            return {
                "content": "第二段内容是重点。第三段新内容。",
                "finish_reason": finish,
                "usage": None,
            }
        return "第二段内容是重点。第三段新内容。"


def test_auto_continue_strips_seam_overlap(monkeypatch):
    monkeypatch.setattr(ws, "get_prompt", lambda key, **kw: f"prompt::{key}")
    provider = _OverlapProvider()
    ctx = {"writing_length": "standard", "cancel_event": None}
    final = asyncio.run(
        ws._auto_continue_on_length(
            provider, "第一段内容。第二段内容是重点。", {"title": "章"},
            ctx, {"return_meta": True}, max_rounds=3,
        )
    )
    # 拼接结果不应出现「第二段内容是重点。」连续重复两次
    assert "第二段内容是重点。第二段内容是重点。" not in final
    # 新内容必须保留
    assert "第三段新内容。" in final
    assert "第一段内容。" in final


# ---------------------------------------------------------------------------
# 续写上下文截断端到端
# ---------------------------------------------------------------------------

class _CaptureProvider:
    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, messages, **options):
        self.calls += 1
        if options.get("return_meta"):
            # 仅一轮，直接 stop
            return {"content": "尾部新段。", "finish_reason": "stop", "usage": None}
        return "尾部新段。"


def test_auto_continue_truncates_existing_context(monkeypatch):
    captured = {}

    def _fake_prompt(key, **kw):
        captured.update(kw)
        return "prompt"

    monkeypatch.setattr(ws, "get_prompt", _fake_prompt)

    # 构造超长已写内容（> _MAX_CONTINUE_CONTEXT_CHARS）
    long_text = "甲" * (ws._MAX_CONTINUE_CONTEXT_CHARS + 1500) + "结尾标记段"
    provider = _CaptureProvider()
    ctx = {"writing_length": "standard", "cancel_event": None}
    asyncio.run(
        ws._auto_continue_on_length(
            provider, long_text, {"title": "章"}, ctx,
            {"return_meta": True}, max_rounds=3,
        )
    )
    existing = captured.get("existing_content", "")
    # 注入提示词的已写内容必须是末尾截断片段，而非全文
    assert len(existing) <= ws._MAX_CONTINUE_CONTEXT_CHARS
    assert existing.endswith("结尾标记段")
    assert "甲" * (ws._MAX_CONTINUE_CONTEXT_CHARS + 1) not in existing


def test_auto_continue_no_truncation_for_short_content(monkeypatch):
    captured = {}

    def _fake_prompt(key, **kw):
        captured.update(kw)
        return "prompt"

    monkeypatch.setattr(ws, "get_prompt", _fake_prompt)
    short_text = "短正文内容"
    provider = _CaptureProvider()
    ctx = {"writing_length": "standard", "cancel_event": None}
    asyncio.run(
        ws._auto_continue_on_length(
            provider, short_text, {"title": "章"}, ctx,
            {"return_meta": True}, max_rounds=3,
        )
    )
    assert captured.get("existing_content") == short_text
