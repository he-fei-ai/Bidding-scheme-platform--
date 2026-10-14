# -*- coding: utf-8 -*-
"""遗留问题（第二轮审计 第八节 9 项）加固验证（2026-10-08）。

对应修复：
  L1  rate_gate：新增显式开关 AI_RATE_LIMIT_DISABLED（0/未配置仍 = 跟随默认闸门）
  L2  stream_chat：补齐信号量/限流闸门/每请求超时/取消/空值安全
  L3  图表校验体系统一为 chart_validators 并接入落库前判定链
  L4  _try_repair_json 支持 ASCII 单引号 JSON；扫描正则抗 `->>` 提前截断
  L5  _next_figure_number：章节号缺失时返回空串，不再产出 `图default-1`
  L6  _single_flight_render 产物容器加 TTL + LRU 容量淘汰
  L7  本地质量评分：剔除图表围栏载荷 + marker 维度计入围栏块
  L8  暂停恢复前校验任务组锁存活性（防止双循环并发写）
  L9  章节 system 提示词：删除与注册正本矛盾的兜底文案，改 fail-fast
"""
import asyncio
import contextlib
import time
from types import SimpleNamespace

import pytest


# ============================================================
# L1：RPM 语义冲突 → 显式开关
# ============================================================


class TestRateLimitDisableFlag:
    def test_disabled_flag_forces_unlimited(self, monkeypatch):
        import app.services.ai.rate_gate as rg

        monkeypatch.setenv("AI_RATE_LIMIT_DISABLED", "1")
        gate = rg._RateGate()
        st = gate._state("http://disabled-flag-test", 0, 2)
        assert st.rpm == 0.0  # 0 = 不限流

    def test_unset_flag_keeps_default_throttle(self, monkeypatch):
        """回归保护：显式开关未开时，0/未配置不得变成不限流（防 429 风暴）。"""
        import app.services.ai.rate_gate as rg

        monkeypatch.delenv("AI_RATE_LIMIT_DISABLED", raising=False)
        monkeypatch.setenv("AI_RATE_LIMIT_RPM", "60")
        gate = rg._RateGate()
        st = gate._state("http://keep-throttle-test", 0, 2)
        assert st.rpm == 60.0


# ============================================================
# L2：stream_chat 加固
# ============================================================


class _FakeStreamResponse:
    def __init__(self, lines):
        self._lines = lines

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeStreamClient:
    def __init__(self, lines, seen_timeouts):
        self._lines = lines
        self._seen = seen_timeouts

    def stream(self, method, url, json=None, timeout=None):
        self._seen.append(timeout)
        resp = _FakeStreamResponse(self._lines)

        @contextlib.asynccontextmanager
        async def _cm():
            yield resp

        return _cm()


def _build_stream_provider(lines, seen_timeouts, slot_events):
    import app.services.ai.providers.openai_compatible as oc

    provider = oc.OpenAICompatibleProvider(
        api_key="k", base_url="https://stream-test/v1", max_retries=1
    )
    seen_client: list = []
    client = _FakeStreamClient(lines, seen_timeouts)

    @contextlib.asynccontextmanager
    async def fake_get_client(timeout, headers):
        seen_client.append(timeout)
        yield client

    object.__setattr__(provider, "_get_client", fake_get_client)

    @contextlib.asynccontextmanager
    async def counting_slot(*a, **k):
        slot_events.append("enter")
        try:
            yield None
        finally:
            slot_events.append("exit")

    gate = SimpleNamespace(
        slot=counting_slot,
        on_rate_limited=lambda *a, **k: 0.0,
        on_success=lambda *a, **k: None,
    )
    return oc, provider, gate


class TestStreamChatHardening:
    def test_stream_goes_through_gate_and_tolerates_empty_choices(self, monkeypatch):
        lines = [
            'data: {"choices":[{"delta":{"content":"你好"}}]}',
            "data: {\"choices\":[]}",          # 空 choices：不得 IndexError
            'data: {"choices":[{"delta":null}]}',  # delta 为 null：不得 AttributeError
            "data: [DONE]",
        ]
        seen_timeouts: list = []
        slot_events: list = []
        oc, provider, gate = _build_stream_provider(lines, seen_timeouts, slot_events)
        monkeypatch.setattr(oc, "_rate_gate", gate)

        async def run():
            out = []
            async for chunk in provider.stream_chat(
                [SimpleNamespace(role="user", content="hi")],
                max_tokens=16000,  # → effective_timeout >= 180
            ):
                out.append(chunk)
            return out

        pieces = asyncio.run(run())
        assert pieces == ["你好"]
        # 必须持有并发许可（进入/退出成对）且透传每请求超时
        assert slot_events == ["enter", "exit"]
        assert any(float(getattr(t, "read", 0)) >= 180 for t in seen_timeouts if t is not None)

    def test_stream_cancel_event_raises_cancelled(self, monkeypatch):
        seen_timeouts: list = []
        slot_events: list = []
        oc, provider, gate = _build_stream_provider(
            ['data: {"choices":[{"delta":{"content":"x"}}]}'], seen_timeouts, slot_events
        )
        monkeypatch.setattr(oc, "_rate_gate", gate)

        cancel = asyncio.Event()
        cancel.set()

        async def run():
            async for _ in provider.stream_chat(
                [SimpleNamespace(role="user", content="hi")], cancel_event=cancel
            ):
                pass

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(run())


# ============================================================
# L3：图表校验体系统一
# ============================================================


class TestChartValidationUnified:
    def test_invalid_structure_demoted_to_hint(self):
        from app.services.ai.workflows_sections import extract_section_charts

        # timeline 的 date 不是 YYYY-MM-DD → chart_validators 判非法
        raw = (
            '<<CHART:{"type":"timeline","milestones":'
            '[{"date":"2026-1-1","title":"进场"},{"date":"2026-01-02","title":"验收"}]}>>'
        )
        charts = extract_section_charts(raw)
        assert len(charts) == 1
        assert charts[0].get("_hint") is True, "非法结构必须降级为提示条目"

    def test_unified_validator_accepts_single_task_gantt(self):
        """口径以 chart_validators 为准：gantt ≥1 任务即可（旧 local 口径要求 ≥2）。"""
        from app.services.ai.workflows_sections import extract_section_charts

        raw = (
            '<<CHART:{"type":"gantt","title":"总进度","tasks":'
            '[{"id":"t1","name":"基础施工","start":"2026-01-01","end":"2026-02-01"}]}>>'
        )
        charts = extract_section_charts(raw)
        assert len(charts) == 1
        assert not charts[0].get("_hint")
        assert len(charts[0]["tasks"]) == 1

    def test_validate_chart_data_delegation(self):
        from app.services.ai.workflows_sections import _validate_chart_data

        ok, err = _validate_chart_data(
            "timeline",
            {"milestones": [{"date": "2026-1-1", "title": "a"}, {"date": "2026-01-02", "title": "b"}]},
        )
        assert (ok, bool(err)) == (False, True)
        # 未覆盖类型 → 放行；非 dict → 拒绝
        assert _validate_chart_data("comparison", {"x": 1})[0] is True
        assert _validate_chart_data("flowchart", "not-a-dict")[0] is False


# ============================================================
# L4：JSON 兼容修复
# ============================================================


class TestChartMarkerCompat:
    def test_single_quoted_json_repaired(self):
        from app.services.content_cleaning import _try_repair_json

        obj = _try_repair_json("{'type':'gantt','title':'进度计划'}")
        assert obj is not None and obj.get("type") == "gantt"
        assert obj.get("title") == "进度计划"

    def test_double_quoted_apostrophe_untouched(self):
        from app.services.content_cleaning import _try_repair_json

        # 双引号段内的撇号不得被当作分隔符改写
        raw = '{"type":"flowchart","title":"it\'s ok"}'
        obj = _try_repair_json(raw)
        assert obj is not None
        assert obj.get("title") == "it's ok"

    def test_arrow_sequence_not_truncated(self):
        from app.services.content_cleaning import (
            iter_chart_markers,
            resolve_chart_marker,
        )

        content = '进度\n<<CHART:{"type":"flowchart","code":"A->>B: 提交验收"}>>\n收尾\n'
        markers = list(iter_chart_markers(content))
        assert len(markers) == 1
        _full, prefix, raw = markers[0]
        assert "A->>B" in raw, "描述里的 ->> 不得被当作标记终止符"
        assert "提交验收" in raw
        chart_type, _title = resolve_chart_marker(prefix, raw)
        assert chart_type == "flowchart"

    def test_multiline_marker_still_parsed(self):
        """行尾锚定不得让多行（非行尾终止）标记漏掉。"""
        from app.services.content_cleaning import iter_chart_markers

        content = '正文\n<<CHART:{"type":"gantt",\n"tasks":[{"id":"t1"}]}>> 尾部文本\n'
        markers = list(iter_chart_markers(content))
        assert markers, "多行标记必须仍被识别（宽松形态兜底）"


# ============================================================
# L5：图号
# ============================================================


class TestFigureNumberGuard:
    def test_missing_section_number_yields_no_number(self):
        from app.services.export import _next_figure_number

        assert _next_figure_number("", 90051) == ""
        assert _next_figure_number(None, 90052) == ""
        assert "default" not in _next_figure_number("", 90053)

    def test_normal_numbering_unchanged(self):
        from app.services.export import _next_figure_number

        # 纯章号按章连续递增（生产主路径）
        assert _next_figure_number("3", 90061) == "图3-1"
        assert _next_figure_number("3", 90061) == "图3-2"
        # BUG-FIX 2026-10-14：带点子节号同属第 3 章，与上方共用同一计数器
        # （旧实现会另起新桶重号→图3-1）；现按章连续 → 图3-3。
        assert _next_figure_number("3.2", 90061) == "图3-3"


# ============================================================
# L6：single-flight 淘汰
# ============================================================


class TestInflightResultEviction:
    def test_ttl_and_capacity_eviction(self):
        from app.services import export as E

        saved = dict(E._inflight_results)
        try:
            E._inflight_results.clear()
            now = time.time()
            for i in range(60):
                # 前 5 条为过期条目，其余为新鲜条目（超过容量 50）
                E._inflight_results[f"k{i}"] = (now - 400.0 if i < 5 else now, b"x")
            E._inflight_put("fresh", b"y")
            assert "k0" not in E._inflight_results, "过期条目应被 TTL 淘汰"
            assert len(E._inflight_results) <= E._INFLIGHT_RESULT_MAX_ENTRIES
            assert E._inflight_results["fresh"][1] == b"y"
        finally:
            E._inflight_results.clear()
            E._inflight_results.update(saved)


# ============================================================
# L7：质量评分口径
# ============================================================


class TestLocalQualityMetrics:
    def test_chart_payload_excluded_from_metrics(self):
        from app.services.ai.workflows_quality import score_section_quality_local

        base = "施工准备阶段的组织筹划与技术准备。" * 20
        fence = (
            "```mermaid\ngraph TD\nA[开始] --> B[技术交底]\n"
            "C[复核] --> D[归档]\n```\n"
        )
        s1 = score_section_quality_local(base)
        s2 = score_section_quality_local(base + "\n" + fence)
        assert s2["char_count"] == s1["char_count"], "围栏块（含标记行）不得计入字数"
        assert s2["dimensions"]["markers"] > 0, "围栏块应计入「标记使用」维度"

    def test_marker_dimension_counts_fences(self):
        from app.services.ai.workflows_quality import score_section_quality_local

        with_fence = score_section_quality_local(
            "正文内容。\n```chart-json\n{\"type\":\"gantt\",\"tasks\":[{\"id\":\"t1\"}]}\n```\n"
        )
        assert with_fence["dimensions"]["markers"] > 0


# ============================================================
# L8：暂停恢复锁存活性
# ============================================================


class TestResumeLockLiveness:
    def test_helper_detects_owner_change(self, monkeypatch):
        from app.services import task_lock
        from app.services.generation_manager import _task_group_lock_still_ours

        monkeypatch.setattr(task_lock, "is_lock_live", lambda key, now=None: True)
        monkeypatch.setattr(
            task_lock, "get_active_lock", lambda *a, **k: {"owner_id": "owner-1"}
        )
        assert _task_group_lock_still_ours("k", "owner-1", 1) is True
        assert _task_group_lock_still_ours("k", "someone-else", 1) is False

        monkeypatch.setattr(task_lock, "get_active_lock", lambda *a, **k: None)
        assert _task_group_lock_still_ours("k", "owner-1", 1) is False

        monkeypatch.setattr(
            task_lock, "get_active_lock", lambda *a, **k: {"owner_id": "owner-1"}
        )
        monkeypatch.setattr(task_lock, "is_lock_live", lambda key, now=None: False)
        assert _task_group_lock_still_ours("k", "owner-1", 1) is False

    def test_missing_credentials_refused(self):
        from app.services.generation_manager import _task_group_lock_still_ours

        assert _task_group_lock_still_ours("", "owner-1", 1) is False
        assert _task_group_lock_still_ours("k", "", 1) is False


# ============================================================
# L9：章节 system 提示词 fail-fast
# ============================================================


class TestSectionSystemPromptFailFast:
    def test_registry_value_returned(self, monkeypatch):
        import app.services.ai.workflows_sections as ws

        monkeypatch.setattr(ws, "get_prompt", lambda name: "SYSTEM-REGISTRY")
        assert ws._resolve_section_system_prompt() == "SYSTEM-REGISTRY"

    def test_missing_prompt_raises_instead_of_silent_fallback(self, monkeypatch):
        import app.services.ai.workflows_sections as ws

        monkeypatch.setattr(ws, "get_prompt", lambda name: None)
        with pytest.raises(RuntimeError):
            ws._resolve_section_system_prompt()

    def test_registry_error_raises(self, monkeypatch):
        import app.services.ai.workflows_sections as ws

        def _boom(name):
            raise RuntimeError("registry broken")

        monkeypatch.setattr(ws, "get_prompt", _boom)
        with pytest.raises(RuntimeError):
            ws._resolve_section_system_prompt()
