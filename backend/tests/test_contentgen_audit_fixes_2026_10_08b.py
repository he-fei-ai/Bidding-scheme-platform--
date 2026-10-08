# -*- coding: utf-8 -*-
"""正文生成模块第二轮深度审计修复验证（2026-10-08）。

覆盖修复项：
  FIX-A  _apply_outside_chart_fences 哨兵奇偶切分颠倒 → 多图章节隔段漏洗
  FIX-G  clean_generated_content 未剥离 UTF-8 BOM → 首标题解析失败
  FIX-F  嵌套列表缩进层级拍平 + "----" 水平线误判为列表项
  FIX-C1 extract_section_charts：{"type":"gantt"} 空壳 JSON 被当真实数据落库
         → 配图编排被整体跳过，图永不生成
  FIX-C2 多行图表标记聚合上限 40 → 200（超长折行 JSON 不再泄漏为正文乱码）
  FIX-C3 扩写/统计字数口径剔除图表围栏载荷（mermaid 代码体不计入正文字数）
  FIX-R1 rate_gate 冷却窗口只睡一次且封顶 30s → Retry-After: 60 实际只等 30s
  FIX-R2 openai_compatible：429 重试不再吃掉通用（5xx/超时）重试预算
  FIX-R3 openai_compatible：每请求覆盖超时（client 复用后长文超时不再失效）
  FIX-R4 openai_compatible：return_meta 缺 usage 时不再返回上一请求的陈旧 token 数
"""
import asyncio
import contextlib
import time
from types import SimpleNamespace

import pytest

from app.services.content_cleaning import (
    clean_generated_content,
    parse_content,
)
from app.services.ai.workflows_sections import extract_section_charts
from app.services.ai.workflows_expand import _words_excluding_chart_payload
from app.utils import count_words as _count_words


MERMAID_FENCE = "```mermaid\ngraph TD\nA[**关键路径**] --> B\n```\n"


class TestFenceParityAndCleaning:
    """FIX-A：多图章节隔段文本漏洗。"""

    def test_two_chart_fences_middle_text_still_cleaned(self):
        content = (
            "开头**加粗**段\n"
            + MERMAID_FENCE
            + "中间**加粗**段\\n字面量\n"
            + MERMAID_FENCE
            + "结尾段\n"
        )
        cleaned = clean_generated_content(content)
        # 两个图表之间的普通文本必须被清洗（旧实现整段跳过）
        assert "中间**加粗**" not in cleaned
        assert "中间加粗段" in cleaned
        # 字面量 \n 必须还原（旧实现在奇数片段被跳过）
        assert "\\n" not in cleaned
        # 首尾普通文本照常清洗
        assert "开头加粗段" in cleaned
        assert "结尾段" in cleaned
        # 图表载荷零污染：mermaid 源码原样保留
        assert "graph TD" in cleaned
        assert "A[**关键路径**] --> B" in cleaned
        # 两个围栏块都在
        assert cleaned.count("```mermaid") == 2

    def test_three_chart_fences_all_gaps_cleaned(self):
        content = (
            "A**x**\n" + MERMAID_FENCE + "B**x**\n" + MERMAID_FENCE
            + "C**x**\n" + MERMAID_FENCE + "D**x**\n"
        )
        cleaned = clean_generated_content(content)
        for seg in ("Ax", "Bx", "Cx", "Dx"):
            assert seg in cleaned
        assert "**" not in cleaned.replace("A[**关键路径**]", "")

    def test_bom_stripped_and_first_heading_kept(self):
        cleaned = clean_generated_content("\ufeff1、工程概况\n本工程规模如下。\n")
        assert not cleaned.startswith("\ufeff")
        blocks = parse_content(cleaned)
        assert blocks and blocks[0].type == "heading3"
        assert blocks[0].text == "工程概况"


class TestListNesting:
    """FIX-F：嵌套列表层级保真 + 水平线不误判。"""

    def test_nested_bullet_keeps_indent_level(self):
        blocks = parse_content("- 一级项\n　　- 二级项\n正文段落\n")
        kinds = [(b.type, getattr(b, "level", 0)) for b in blocks]
        assert kinds[0] == ("list_bullet", 0)
        assert kinds[1] == ("list_bullet", 2)

    def test_halfwidth_indent_two_spaces_one_level(self):
        blocks = parse_content("- 一级\n  - 二级\n")
        assert blocks[1].type == "list_bullet"
        assert blocks[1].level == 1

    def test_horizontal_rule_not_a_list_item(self):
        blocks = parse_content("----\n正文\n")
        assert all(b.type != "list_bullet" for b in blocks)
        assert any(b.text == "----" for b in blocks)


class TestChartShellPayload:
    """FIX-C1：type 空壳 JSON 归一为提示条目。"""

    def test_type_only_marker_becomes_hint(self):
        charts = extract_section_charts('<<CHART:{"type":"gantt","title":"进度计划"}>>')
        assert len(charts) == 1
        entry = charts[0]
        assert entry.get("_hint") is True
        assert entry.get("type") == "gantt"

    def test_marker_with_real_data_stays_data(self):
        raw = (
            '<<CHART:{"type":"gantt","title":"进度","tasks":'
            '[{"name":"a","start":"2026-01-01","end":"2026-01-02"},'
            '{"name":"b","start":"2026-01-02","end":"2026-01-03"}]}>>'
        )
        charts = extract_section_charts(raw)
        assert len(charts) == 1
        assert not charts[0].get("_hint")
        assert len(charts[0]["tasks"]) == 2

    def test_loose_dict_shell_becomes_hint(self):
        # 裸 key 宽松形态：{type: gantt, title: 进度}（Strategy B 修复后解析成功）
        charts = extract_section_charts("<<CHART:{type: gantt, title: 进度}>>")
        assert len(charts) == 1
        assert charts[0].get("_hint") is True
        assert charts[0].get("type") == "gantt"

    def test_multiline_marker_aggregation_limit_raised(self):
        # FIX-C2：折行 JSON 标记 60 行（旧上限 40 会截断 → JSON 体泄漏为正文乱码）
        payload = {"type": "gantt", "title": "总进度", "tasks": [
            {"name": f"任务{i}", "start": "2026-01-01", "end": "2026-01-02"}
            for i in range(20)
        ]}
        folded = json.dumps(payload, ensure_ascii=False, indent=1)
        assert len(folded.split("\n")) > 40
        content = "<<CHART:" + folded + ">>\n正文\n"
        blocks = parse_content(content)
        chart_blocks = [b for b in blocks if b.type == "chart_marker"]
        assert chart_blocks, "折行 JSON 标记应被聚合并解析为 chart_marker 块"
        assert not any("任务" in (b.text or "") and b.type == "paragraph" for b in blocks), \
            "JSON 体不得作为正文段落泄漏"


import json  # noqa: E402  （置于用例组之后以免干扰顶部注释阅读）


class TestExpandWordCount:
    """FIX-C3：字数口径剔除图表围栏载荷。"""

    def test_chart_payload_excluded_from_count(self):
        content = (
            "正文段落一。施工准备。\n"
            "```mermaid\ngraph TD\nA --> B\nC --> D\n```\n"
            "正文段落二。验收。"
        )
        assert _words_excluding_chart_payload(content) < _count_words(content)

    def test_plain_text_unchanged(self):
        text = "普通正文，没有图表。"
        assert _words_excluding_chart_payload(text) == _count_words(text)

    def test_empty_and_none_safe(self):
        assert _words_excluding_chart_payload("") == 0


class TestRateGateCooldown:
    """FIX-R1：冷却窗口分片睡满（Retry-After > 30s 不被截断）。"""

    def test_cooldown_waits_in_chunks_until_finished(self, monkeypatch):
        import app.services.ai.rate_gate as rg

        gate = rg.rate_gate
        st = gate._state("http://cooldown-test-fullwait", 60.0, 2)
        calls: list[float] = []
        orig_cooldown = st.cooldown_until

        async def fake_sleep(seconds, ev):
            calls.append(round(seconds, 3))
            st.cooldown_until -= seconds

        monkeypatch.setattr(rg, "_interruptible_sleep", fake_sleep)
        st.cooldown_until = time.monotonic() + 75.0

        async def run():
            async with gate.slot("http://cooldown-test-fullwait", rpm=60.0, concurrency=2):
                pass

        asyncio.run(run())
        try:
            # 每片 ≤ _MAX_SLEEP，分片数固定为 3，且总等待覆盖完整冷却窗口（±浮点漂移）
            assert len(calls) == 3
            assert all(c <= 30.0 for c in calls)
            assert sum(calls) >= 74.9
        finally:
            st.cooldown_until = orig_cooldown


class _FakeResp:
    def __init__(self, status_code, data=None):
        self.status_code = status_code
        self._data = data or {}
        self.text = ""
        self.headers = {}

    def json(self):
        return self._data

    def raise_for_status(self):
        return None


def _ok_resp():
    return _FakeResp(200, {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    })


class TestOpenAIRetryBudget:
    """FIX-R2/R3/R4：429 独立预算、每请求超时、陈旧 usage。"""

    def _make_provider(self, responses, seen_timeouts):
        import app.services.ai.providers.openai_compatible as oc

        provider = oc.OpenAICompatibleProvider(
            api_key="k", base_url="https://budget-test/v1", max_retries=2
        )
        it = iter(responses)

        @contextlib.asynccontextmanager
        async def fake_get_client(timeout, headers):
            seen_timeouts.append(timeout)

            class _Client:
                async def post(self, url, json=None, timeout=None):
                    seen_timeouts.append(timeout)
                    return next(it)

            yield _Client()

        object.__setattr__(provider, "_get_client", fake_get_client)

        @contextlib.asynccontextmanager
        async def no_slot(*a, **k):
            yield None

        monkey_gate = SimpleNamespace(
            slot=no_slot,
            on_rate_limited=lambda *a, **k: 0.0,
            on_success=lambda *a, **k: None,
        )
        return oc, provider, monkey_gate

    def test_429_does_not_eat_generic_retry_budget(self, monkeypatch):
        import app.services.ai.providers.openai_compatible as oc

        seen_timeouts: list = []
        responses = [
            _FakeResp(429), _FakeResp(429),   # 两次限流
            _FakeResp(500),                    # 限流后首个 5xx：仍应有重试
            _ok_resp(),
        ]
        oc2, provider, gate = self._make_provider(responses, seen_timeouts)

        async def no_sleep(ev, wait, jitter=True):
            return None

        monkeypatch.setattr(oc2, "_cancellable_sleep", no_sleep)
        monkeypatch.setattr(oc2, "_rate_gate", gate)

        result = asyncio.run(
            provider.chat([SimpleNamespace(role="user", content="hi")], max_retries=2)
        )
        # 旧实现：attempt 被 429 推进到 2 → 500 时零重试直接 raise
        assert result == "ok"

    def test_per_request_timeout_passed(self, monkeypatch):
        import app.services.ai.providers.openai_compatible as oc

        seen_timeouts: list = []
        responses = [_ok_resp()]
        oc2, provider, gate = self._make_provider(responses, seen_timeouts)

        async def no_sleep(ev, wait, jitter=True):
            return None

        monkeypatch.setattr(oc2, "_cancellable_sleep", no_sleep)
        monkeypatch.setattr(oc2, "_rate_gate", gate)

        asyncio.run(
            provider.chat(
                [SimpleNamespace(role="user", content="hi")],
                max_retries=1,
                max_tokens=16000,  # → effective_timeout ≥ 180
            )
        )
        # post 必须携带本次计算的 per-request 超时（读超时 ≥180s），而非仅依赖
        # 复用 client 的旧配置
        post_timeouts = [
            t for t in seen_timeouts
            if t is not None and float(getattr(t, "read", 0)) >= 180
        ]
        assert post_timeouts, f"per-request timeout 未透传: {seen_timeouts}"

    def test_return_meta_without_usage_returns_zeroes(self, monkeypatch):
        import app.services.ai.providers.openai_compatible as oc

        seen_timeouts: list = []
        responses = [_FakeResp(200, {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        })]
        oc2, provider, gate = self._make_provider(responses, seen_timeouts)

        async def no_sleep(ev, wait, jitter=True):
            return None

        monkeypatch.setattr(oc2, "_cancellable_sleep", no_sleep)
        monkeypatch.setattr(oc2, "_rate_gate", gate)
        # 预置上一请求的陈旧 usage
        provider._last_usage = {"prompt_tokens": 99, "completion_tokens": 99, "total_tokens": 198}

        meta = asyncio.run(
            provider.chat(
                [SimpleNamespace(role="user", content="hi")],
                max_retries=1,
                return_meta=True,
            )
        )
        assert meta["content"] == "ok"
        assert meta["usage"] == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
