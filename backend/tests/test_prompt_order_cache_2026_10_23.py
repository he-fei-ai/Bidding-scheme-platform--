"""标书智能体（4）——提示词顺序优化与缓存命中：引入回归测试（2026-10-23）。

外部引用标准（冲突时以其工作机制/工作流程/标准为准）：
《标书智能体（4）——提示词顺序优化，让缓存命中，输入成本直降10倍》
（FB208/yibiao-simple）。四条核心原则：
  1. system 只放稳定规则（不放本次请求特有差异）；
  2. 最大、最稳定的上下文尽量前置；
  3. 任务差异尽量放最后；
  4. 同一份数据的组织格式保持稳定（不塞时间戳/request_id/随机数）。

覆盖范围（七类高频任务 + 可观测性）：
  A. prompt_order 通用装配器（顺序/格式稳定/拆分/指纹）；
  B. cache_metrics 按任务类型计量 + 命中率告警 + provider 用量采集；
  C. 正文生成 sibling_context 跨叶子字节级一致（generate_sections 集成）；
  D. 目录生成 L1/L2 分层消息栈 + 开关回退；
  E. 正文编排分层 + 稳定前缀一致；
  F. 图表生成 system 稳定规则 + 失效 AI 链路修复 + 缓存记账；
  G. 审核预检分层（质量评估 + 目录审核）；
  H. 配置项默认行为（默认开启、可独立回退、向后兼容）。
"""

import asyncio
import json
import logging

import pytest

import app.config as app_config
from app.services.ai.cache_metrics import (
    record_prompt_cache,
    record_provider_cache_usage,
    reset,
    snapshot,
)
from app.services.ai.prompt_order import (
    build_cache_friendly_dict_messages,
    build_cache_friendly_messages,
    merge_single_user,
    split_rendered_template,
    stable_prefix_fingerprint,
    stable_prefix_text,
)
from app.services.ai.providers.base import AIMessage


def _contents(messages) -> list[str]:
    return [str(getattr(m, "content", "") or "") for m in messages]


# ══════════════════════════════════════════════════════════════════════
# A. prompt_order 通用装配器
# ══════════════════════════════════════════════════════════════════════
class TestPromptOrderBuilder:

    def test_system_first_stable_middle_task_last(self):
        msgs = build_cache_friendly_messages(
            "稳定规则",
            ["项目概述（大且稳定）", "评分要求（稳定）"],
            "当前章节任务（易变）",
        )
        assert [m.role for m in msgs] == ["system", "user", "user", "user"]
        assert msgs[0].content == "稳定规则"
        assert msgs[1].content == "项目概述（大且稳定）"
        assert msgs[2].content == "评分要求（稳定）"
        assert msgs[3].content == "当前章节任务（易变）"

    def test_empty_blocks_and_task_skipped(self):
        """空块/纯空白块/None 不产生消息；只剩 system。"""
        msgs = build_cache_friendly_messages("规则", ["", "   ", None], "")
        assert [m.role for m in msgs] == ["system"]
        assert msgs[0].content == "规则"
        # 有空块 + 有任务块时，空块跳过、任务块保留在最后
        msgs2 = build_cache_friendly_messages("规则", ["", "有效块"], "任务")
        assert [m.role for m in msgs2] == ["system", "user", "user"]
        assert msgs2[1].content == "有效块"
        assert msgs2[2].content == "任务"

    def test_all_empty_yields_single_system(self):
        msgs = build_cache_friendly_messages("", [], "")
        assert len(msgs) == 1 and msgs[0].role == "system"

    def test_normalize_crlf_and_trailing_ws(self):
        """原则 4：同一份数据渲染结果字节级稳定（CRLF/行尾空白统一）。"""
        msgs = build_cache_friendly_messages(
            "规则  ",
            ["第一行\r\n第二行   \n\n第三行\t"],
            "",
        )
        assert msgs[0].content == "规则"
        assert "\r" not in msgs[1].content
        assert "第二行   " not in msgs[1].content
        assert msgs[1].content == "第一行\n第二行\n\n第三行"

    def test_normalize_can_be_disabled(self):
        msgs = build_cache_friendly_messages("规则", ["a\r\nb"], "", normalize=False)
        assert msgs[1].content == "a\r\nb"

    def test_dict_variant_matches(self):
        dict_msgs = build_cache_friendly_dict_messages("规则", ["块"], "任务")
        obj_msgs = build_cache_friendly_messages("规则", ["块"], "任务")
        assert [d["role"] for d in dict_msgs] == [m.role for m in obj_msgs]
        assert [d["content"] for d in dict_msgs] == [m.content for m in obj_msgs]

    def test_stable_prefix_fingerprint(self):
        """稳定前缀指纹：任务差异变化不动指纹；稳定块变化即变。"""
        base = ["项目概述", "评分要求"]
        m1 = build_cache_friendly_messages("规则", base, "任务A")
        m2 = build_cache_friendly_messages("规则", base, "任务B")
        m3 = build_cache_friendly_messages("规则", ["项目概述", "评分要求X"], "任务A")
        fp1 = stable_prefix_fingerprint(m1)
        assert fp1 == stable_prefix_fingerprint(m2)
        assert fp1 != stable_prefix_fingerprint(m3)
        # 指纹不含任务差异文本本身
        assert "任务A" not in stable_prefix_text(m1)

    def test_split_rendered_template(self):
        before, after = split_rendered_template("稳定A@@M@@稳定B", "@@M@@")
        assert before == "稳定A" and after == "稳定B"
        # 无哨兵时整体视为稳定段
        assert split_rendered_template("全文", "@@M@@") == ("全文", "")

    def test_merge_single_user(self):
        assert merge_single_user(["a", "", "b"]) == "a\nb"


# ══════════════════════════════════════════════════════════════════════
# B. cache_metrics 任务类型计量与告警
# ══════════════════════════════════════════════════════════════════════
class TestCacheMetricsTaskTypes:

    def setup_method(self):
        reset()

    def test_task_type_aggregation(self):
        record_prompt_cache(
            {"prompt_tokens": 1000, "completion_tokens": 100,
             "prompt_tokens_details": {"cached_tokens": 600}},
            task_type="section_content", section_id="1.1",
        )
        record_prompt_cache(
            {"prompt_tokens": 2000, "completion_tokens": 100,
             "prompt_tokens_details": {"cached_tokens": 0}},
            task_type="section_content", section_id="1.2",
        )
        snap = snapshot()
        tt = snap["task_types"]["section_content"]
        assert tt["requests"] == 2
        assert tt["prompt_tokens"] == 3000
        assert tt["cached_tokens"] == 600
        assert tt["hit_rate"] == 20.0
        # 既有全局口径不回归
        assert snap["openai_prompt_cache"]["requests"] == 2
        assert snap["openai_prompt_cache"]["cached_tokens"] == 600

    def test_anthropic_style_task_stats(self):
        record_prompt_cache(
            {"cache_creation_input_tokens": 1000, "cache_read_input_tokens": 9000,
             "input_tokens": 500, "output_tokens": 50},
            task_type="outline_l1",
        )
        tt = snapshot()["task_types"]["outline_l1"]
        # prompt = input + creation + read
        assert tt["prompt_tokens"] == 10500
        assert tt["cached_tokens"] == 9000
        assert tt["hit_rate"] == pytest.approx(85.7, abs=0.1)

    def test_unknown_task_type_isolated(self):
        record_prompt_cache({"prompt_tokens": 10}, task_type="unknown")
        assert "unknown" in snapshot()["task_types"]

    def test_reset_clears_task_stats(self):
        record_prompt_cache({"prompt_tokens": 10}, task_type="x")
        reset()
        assert snapshot()["task_types"] == {}

    def test_alert_fires_when_below_threshold(self, monkeypatch, caplog):
        monkeypatch.setattr(app_config, "AI_CACHE_HIT_RATE_ALERT_THRESHOLD", 0.3)
        monkeypatch.setattr(app_config, "AI_CACHE_HIT_RATE_MIN_SAMPLES", 5)
        monkeypatch.setattr(app_config, "AI_CACHE_ALERT_THROTTLE_SECONDS", 600)
        with caplog.at_level(logging.WARNING):
            for _ in range(5):
                record_prompt_cache(
                    {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 0}},
                    task_type="section_content",
                )
            assert any("缓存告警" in r.message and "section_content" in r.message
                       for r in caplog.records)
            # 节流：同类型不重复告警
            caplog.clear()
            for _ in range(5):
                record_prompt_cache(
                    {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 0}},
                    task_type="section_content",
                )
            assert not any("缓存告警" in r.message for r in caplog.records)

    def test_alert_disabled_by_default(self, caplog):
        with caplog.at_level(logging.WARNING):
            for _ in range(30):
                record_prompt_cache(
                    {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 0}},
                    task_type="section_content",
                )
            assert not any("缓存告警" in r.message for r in caplog.records)

    def test_no_alert_below_min_samples(self, monkeypatch, caplog):
        monkeypatch.setattr(app_config, "AI_CACHE_HIT_RATE_ALERT_THRESHOLD", 0.9)
        monkeypatch.setattr(app_config, "AI_CACHE_HIT_RATE_MIN_SAMPLES", 10)
        with caplog.at_level(logging.WARNING):
            record_prompt_cache(
                {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 0}},
                task_type="section_content",
            )
            assert not any("缓存告警" in r.message for r in caplog.records)

    def test_record_provider_cache_usage(self):
        class _FakeProvider:
            model = "fake-model"

            @property
            def last_usage(self):
                return {"prompt_tokens": 800,
                        "prompt_tokens_details": {"cached_tokens": 700}}

        record_provider_cache_usage(_FakeProvider(), task_type="chart_analysis")
        tt = snapshot()["task_types"]["chart_analysis"]
        assert tt["requests"] == 1 and tt["cached_tokens"] == 700

    def test_provider_without_last_usage_is_noop(self):
        class _NoUsage:
            pass

        record_provider_cache_usage(_NoUsage(), task_type="x")
        assert snapshot()["task_types"] == {}


# ══════════════════════════════════════════════════════════════════════
# C. 正文生成 sibling_context 跨叶子一致（generate_sections 集成）
# ══════════════════════════════════════════════════════════════════════
class _CapturingProvider:
    """捕获消息栈并返回固定正文的假 provider。"""

    def __init__(self):
        self.captured: list[list] = []
        self.model = "fake"

    async def chat(self, messages, **kwargs):
        self.captured.append(list(messages))
        return "这是章节正文，工期180天，内容足够长以便通过各类校验逻辑。" * 3


def _sibling_message_of(messages) -> str:
    return next(
        str(m.content) for m in messages
        if str(getattr(m, "content", "")).startswith("同级章节")
    )


class TestSiblingContextCacheIntegration:

    def test_sibling_context_identical_across_sibling_leaves(self):
        """同父章节两个叶子的 sibling_context 必须字节级一致（缓存前提）。"""
        from app.services.ai.workflows_sections import generate_sections

        provider = _CapturingProvider()
        # 父 1 下两个叶子：2.1 / 2.2（互为同级）
        leaves = [
            {"id": "2.1", "title": "施工工艺A", "level": 3, "parent_id": "2",
             "path": ["1", "2", "2.1"]},
            {"id": "2.2", "title": "施工工艺B", "level": 3, "parent_id": "2",
             "path": ["1", "2", "2.2"]},
        ]
        sections_by_id = {
            "1": {"id": "1", "title": "第一章", "parent_id": None},
            "2": {"id": "2", "title": "施工方案及技术措施", "parent_id": "1"},
            "2.1": leaves[0],
            "2.2": leaves[1],
        }
        children_map = {"1": [{"id": "2", "title": "施工方案及技术措施"}],
                        "2": leaves}

        async def _run():
            return await generate_sections(
                provider, leaves, context={"summary": "某项目", "use_cache": False},
                concurrency=1, generation_mode="batch",
                sections_by_id=sections_by_id, children_map=children_map,
            )

        asyncio.run(_run())
        assert len(provider.captured) == 2
        sib_a = _sibling_message_of(provider.captured[0])
        sib_b = _sibling_message_of(provider.captured[1])
        assert sib_a == sib_b, "同父章节 sibling_context 不一致 → 前缀缓存只能部分命中"
        # 含当前章节（文档（4）改进：完整兄弟列表）
        assert "施工工艺A" in sib_a and "施工工艺B" in sib_a
        assert "请避免与除当前章节外的其他章节内容重复" in sib_a

    def test_sibling_list_capped_identically(self, monkeypatch):
        """超容量时降描述保留全部标题（审计7扩容，2026-10-27）。

        旧口径（超容量只取前 N 个）导致第 N+1 个及之后的章节不在自己的
        同级列表里（自定位/兄弟感知缺失）。新口径：超容量时整表降描述
        仅保留标题、扩至 CONTENT_SIBLING_TITLES_MAX_ITEMS——同父章节仍
        共享同一列表（前缀缓存命中前提不变），且当前章节始终在列。"""
        from app.services.ai.workflows_sections import generate_sections

        monkeypatch.setattr(app_config, "CONTENT_SIBLING_MAX_ITEMS", 2)
        provider = _CapturingProvider()
        kids = [
            {"id": f"2.{i}", "title": f"工艺{i}", "description": f"描述{i}",
             "level": 3, "parent_id": "2", "path": ["1", "2", f"2.{i}"]}
            for i in range(1, 5)
        ]
        sections_by_id = {
            "1": {"id": "1", "title": "第一章", "parent_id": None},
            "2": {"id": "2", "title": "施工方案及技术措施", "parent_id": "1"},
            **{k["id"]: k for k in kids},
        }
        children_map = {"1": [{"id": "2", "title": "施工方案及技术措施"}], "2": kids}

        async def _run():
            return await generate_sections(
                provider, kids, context={"summary": "某项目", "use_cache": False},
                concurrency=1, generation_mode="batch",
                sections_by_id=sections_by_id, children_map=children_map,
            )

        asyncio.run(_run())
        sibs = [_sibling_message_of(c) for c in provider.captured]
        assert len(set(sibs)) == 1, "同父章节 sibling_context 不一致 → 前缀缓存只能部分命中"
        # 全部标题在列（降描述扩容），当前章节不缺位
        assert all(f"工艺{i}" in sibs[0] for i in range(1, 5))
        # 描述已剥离（标题-only 扩容的 token 代价控制）
        assert "描述3" not in sibs[0] and "描述4" not in sibs[0]

    def test_sibling_titles_expansion_capped_by_titles_cap(self, monkeypatch):
        """降描述扩容受 CONTENT_SIBLING_TITLES_MAX_ITEMS 硬上限约束
        （防病态超长目录 token 爆炸）。"""
        from app.services.ai.workflows_sections import generate_sections

        monkeypatch.setattr(app_config, "CONTENT_SIBLING_MAX_ITEMS", 2)
        monkeypatch.setattr(app_config, "CONTENT_SIBLING_TITLES_MAX_ITEMS", 3)
        provider = _CapturingProvider()
        kids = [
            {"id": f"2.{i}", "title": f"工艺{i}", "description": f"描述{i}",
             "level": 3, "parent_id": "2", "path": ["1", "2", f"2.{i}"]}
            for i in range(1, 6)
        ]
        sections_by_id = {
            "1": {"id": "1", "title": "第一章", "parent_id": None},
            "2": {"id": "2", "title": "施工方案及技术措施", "parent_id": "1"},
            **{k["id"]: k for k in kids},
        }
        children_map = {"1": [{"id": "2", "title": "施工方案及技术措施"}], "2": kids}

        async def _run():
            return await generate_sections(
                provider, kids, context={"summary": "某项目", "use_cache": False},
                concurrency=1, generation_mode="batch",
                sections_by_id=sections_by_id, children_map=children_map,
            )

        asyncio.run(_run())
        sibs = [_sibling_message_of(c) for c in provider.captured]
        assert len(set(sibs)) == 1
        assert all(f"工艺{i}" in sibs[0] for i in (1, 2, 3))
        assert "工艺4" not in sibs[0] and "工艺5" not in sibs[0]


# ══════════════════════════════════════════════════════════════════════
# D. 目录生成分层消息栈
# ══════════════════════════════════════════════════════════════════════
class TestOutlineLayeredMessages:

    @staticmethod
    def _sp_text():
        return json.dumps(
            [{"id": "SP1", "title": "施工方案", "weight": 10, "standard": "方案可行"}],
            ensure_ascii=False,
        )

    def _patch_collect(self, monkeypatch, captured):
        from app.services.ai import workflows_outline_v51 as wo

        async def _fake_collect(provider, messages, **kwargs):
            captured.append(list(messages))
            return [{"id": "1", "title": "施工方案及技术措施", "description": "d"}]

        monkeypatch.setattr(wo, "collect_json_response", _fake_collect)

    def test_l1_messages_layered(self, monkeypatch):
        from app.services.ai import workflows_outline_v51 as wo

        captured: list[list] = []
        self._patch_collect(monkeypatch, captured)
        provider = _CapturingProvider()
        monkeypatch.setattr(wo, "_assign_ids", lambda x: x)
        monkeypatch.setattr(wo, "_validate_level1", lambda x: None)
        monkeypatch.setattr(wo, "_check_scoring_coverage", lambda *a, **k: None)
        monkeypatch.setattr(wo, "_apply_l1_chapter_cap", lambda x: (x, False))

        async def _run():
            return await wo._generate_level1_v51(
                provider, "项目摘要：某项目", self._sp_text(), "",
                global_facts=[{"label": "工期", "value": "180天"}],
                tender_context="招标文件原文……", industry_profile="房建",
            )

        asyncio.run(_run())
        msgs = captured[0]
        roles = [m.role for m in msgs]
        assert roles[0] == "system"
        assert roles.count("user") >= 4, "稳定上下文应拆成多条 user 消息"
        texts = _contents(msgs)
        # 稳定上下文前置：概述/评分/参考/事实/招标原文各自成消息
        assert any("<overview>" in t for t in texts[1:-1])
        assert any("<requirements>" in t for t in texts[1:-1])
        assert any("全局事实变量" in t for t in texts[1:-1])
        # 任务差异最后：生成指令与章节数要求在最后一条
        assert "请严格按评分要求生成一级目录" in texts[-1]
        assert "评分项总数: 1" in texts[-1]

    def test_l1_retry_correction_only_touches_task_message(self, monkeypatch):
        """重试修正只追加到任务差异消息，稳定块跨重试逐字节一致。"""
        from app.services.ai import workflows_outline_v51 as wo

        captured: list[list] = []
        state = {"n": 0}

        async def _fake_collect(provider, messages, **kwargs):
            captured.append(list(messages))
            state["n"] += 1
            if state["n"] == 1:
                return {"result": "bad"}  # 触发 ValueError → 重试
            return [{"id": "1", "title": "施工方案及技术措施", "description": "d"}]

        monkeypatch.setattr(wo, "collect_json_response", _fake_collect)
        monkeypatch.setattr(wo, "_assign_ids", lambda x: x)
        monkeypatch.setattr(wo, "_validate_level1", lambda x: None)
        monkeypatch.setattr(wo, "_check_scoring_coverage", lambda *a, **k: None)
        monkeypatch.setattr(wo, "_apply_l1_chapter_cap", lambda x: (x, False))
        provider = _CapturingProvider()

        async def _run():
            return await wo._generate_level1_v51(
                provider, "项目摘要", self._sp_text(), "",
            )

        asyncio.run(_run())
        assert len(captured) >= 2
        first, second = captured[0], captured[1]
        # 稳定消息（除最后一条任务消息）逐字节一致
        assert _contents(first)[:-1] == _contents(second)[:-1]
        # 修正段只进任务消息
        assert "必须修正" in _contents(second)[-1]

    def test_layers_disabled_falls_back_to_single_user(self, monkeypatch):
        from app.services.ai import workflows_outline_v51 as wo

        monkeypatch.setattr(app_config, "OUTLINE_PROMPT_LAYERS_ENABLED", False)
        captured: list[list] = []
        self._patch_collect(monkeypatch, captured)
        provider = _CapturingProvider()
        monkeypatch.setattr(wo, "_assign_ids", lambda x: x)
        monkeypatch.setattr(wo, "_validate_level1", lambda x: None)
        monkeypatch.setattr(wo, "_check_scoring_coverage", lambda *a, **k: None)
        monkeypatch.setattr(wo, "_apply_l1_chapter_cap", lambda x: (x, False))

        async def _run():
            return await wo._generate_level1_v51(
                provider, "项目摘要", self._sp_text(), "",
            )

        asyncio.run(_run())
        msgs = captured[0]
        assert [m.role for m in msgs] == ["system", "user"]
        # 合并后的单条 user 仍包含全部内容（概述 + 任务说明）
        assert "<overview>" in msgs[1].content
        assert "请严格按评分要求生成一级目录" in msgs[1].content

    def test_outline_cache_usage_recorded(self, monkeypatch):
        """目录生成按任务类型记账（cache_task=outline_l1）。"""
        from app.services.ai import workflows_outline_v51 as wo

        reset()
        self._patch_collect(monkeypatch, [])

        class _Provider(_CapturingProvider):
            @property
            def last_usage(self):
                return {"prompt_tokens": 500,
                        "prompt_tokens_details": {"cached_tokens": 400}}

        monkeypatch.setattr(wo, "_assign_ids", lambda x: x)
        monkeypatch.setattr(wo, "_validate_level1", lambda x: None)
        monkeypatch.setattr(wo, "_check_scoring_coverage", lambda *a, **k: None)
        monkeypatch.setattr(wo, "_apply_l1_chapter_cap", lambda x: (x, False))

        async def _run():
            return await wo._generate_level1_v51(
                _Provider(), "项目摘要", self._sp_text(), "",
            )

        asyncio.run(_run())
        tt = snapshot()["task_types"].get("outline_l1")
        assert tt and tt["requests"] == 1 and tt["cached_tokens"] == 400


# ══════════════════════════════════════════════════════════════════════
# E. 正文编排分层
# ══════════════════════════════════════════════════════════════════════
class TestOrchestrationLayeredMessages:

    @staticmethod
    def _leaf(sid, title):
        return {"id": sid, "title": title, "level": 3, "parent_id": "2",
                "description": "desc", "path": ["1", "2", sid], "ancestors": []}

    def test_stable_prefix_identical_across_leaves(self, monkeypatch):
        from app.services.ai.workflows_orchestration import _orch_one_section

        captured: list[list] = []

        async def _fake_chat(messages, **kwargs):
            captured.append(list(messages))
            return '[{"section_id":"x","recommended_type":"text","confidence":0.9}]'

        class _P:
            def chat(self, messages, **kwargs):
                return _fake_chat(messages, **kwargs)

        provider = _P()
        leaves = [self._leaf("2.1", "工艺A"), self._leaf("2.2", "工艺B")]
        for leaf in leaves:
            asyncio.run(_orch_one_section(
                provider, leaf, "编排系统规则", "项目概述内容", "评分标准内容",
            ))
        assert len(captured) == 2
        a, b = captured
        # 除最后一条任务消息外，稳定前缀逐字节一致
        assert _contents(a)[:-1] == _contents(b)[:-1]
        # 任务差异在最后且只含当前叶子
        assert "2.1" in _contents(a)[-1] and "工艺A" in _contents(a)[-1]
        assert "2.2" in _contents(b)[-1] and "工艺B" in _contents(b)[-1]
        # 稳定块（项目概述/评分标准）在前部的独立消息里
        joined = "\n".join(_contents(a)[:-1])
        assert "项目概述内容" in joined and "评分标准内容" in joined

    def test_layers_disabled_single_user(self, monkeypatch):
        from app.services.ai.workflows_orchestration import _orch_one_section

        monkeypatch.setattr(app_config, "ORCHESTRATION_PROMPT_LAYERS_ENABLED", False)
        captured: list[list] = []

        async def _fake_chat(messages, **kwargs):
            captured.append(list(messages))
            return "[]"

        class _P:
            def chat(self, messages, **kwargs):
                return _fake_chat(messages, **kwargs)

        asyncio.run(_orch_one_section(
            _P(), self._leaf("2.1", "工艺A"), "规则", "概述", "评分",
        ))
        assert [m.role for m in captured[0]] == ["system", "user"]


# ══════════════════════════════════════════════════════════════════════
# F. 图表生成
# ══════════════════════════════════════════════════════════════════════
class TestChartLayeredMessages:

    def test_analysis_messages_layered_and_ai_path_works(self):
        """图表分析：system 稳定规则 + 项目信息 + 任务差异；AI 链路真正生效。"""
        from app.services.ai.chart_orchestrator import analyze_chart_needs

        captured: list[list] = []

        class _P:
            model = "fake"

            async def chat(self, messages, **kwargs):
                captured.append(list(messages))
                return json.dumps(
                    {"needed": True, "chart_type": "flowchart", "confidence": 0.9,
                     "reason": "流程章节", "title_hint": "工艺流程图",
                     "insert_after": "第一段"},
                    ensure_ascii=False,
                )

            @property
            def last_usage(self):
                return {"prompt_tokens": 300,
                        "prompt_tokens_details": {"cached_tokens": 250}}

        reset()
        result = analyze_chart_needs(
            section_title="施工工艺流程", content="施工流程包括放线、开挖、验收。",
            project_summary="某房建项目", ai_client=_P(),
        )
        assert result["source"] == "ai", "AI 增强链路应真正生效（修复前恒为 local）"
        assert result["chart_type"] == "flowchart"
        msgs = captured[0]
        assert [m.role for m in msgs] == ["system", "user", "user"]
        assert "图表类型标准" in msgs[0].content
        assert "项目信息" in msgs[1].content
        assert "施工工艺流程" in msgs[2].content
        # 缓存记账
        tt = snapshot()["task_types"].get("chart_analysis")
        assert tt and tt["cached_tokens"] == 250

    def test_dict_config_resolves_to_provider(self, monkeypatch):
        """dict 配置（历史 routers 传参形态）经工厂解析为 provider，不再静默死亡。"""
        from app.services.ai import chart_orchestrator as co

        class _Provider:
            async def chat(self, messages, **kwargs):
                return ""

        provider = _Provider()
        monkeypatch.setattr(
            "app.services.ai.provider_factory.create_provider_with_fallback",
            lambda config=None: (provider, "fake"),
        )
        assert co._resolve_chart_provider({"model": "x"}) is provider
        # 已有 .chat 的对象直接使用，不再绕工厂
        assert co._resolve_chart_provider(provider) is provider
        assert co._resolve_chart_provider(None) is None

    def test_no_provider_raises_and_falls_back_local(self):
        from app.services.ai.chart_orchestrator import analyze_chart_needs

        result = analyze_chart_needs(
            section_title="施工工艺流程", content="放线、开挖、验收。", ai_client=None,
        )
        assert result["source"] == "local"


# ══════════════════════════════════════════════════════════════════════
# G. 审核预检分层
# ══════════════════════════════════════════════════════════════════════
class TestQualityLayeredMessages:

    def test_quality_eval_stable_prefix_identical(self, monkeypatch):
        from app.services.ai.workflows_quality import ContentQualityEvaluator

        captured: list[list] = []

        class _P:
            async def chat(self, messages, **kwargs):
                captured.append(list(messages))
                return json.dumps({"structure_score": 80, "professional_score": 80,
                                   "relevance_score": 80, "issues": [], "suggestions": []})

        ev = ContentQualityEvaluator(_P())
        for title, content in (("章节甲", "甲内容" * 50), ("章节乙", "乙内容" * 50)):
            asyncio.run(ev._ai_evaluate_dimensions(
                content, title, 90.0, 95.0,
            ))
        a, b = captured
        assert _contents(a)[:-1] == _contents(b)[:-1], "稳定评估规则应跨章节一致"
        # 任务差异（标题+正文）在最后
        assert "章节甲" in _contents(a)[-1] and "甲内容" in _contents(a)[-1]
        assert "章节乙" in _contents(b)[-1]
        # 稳定规则（评估维度/输出格式）在独立消息
        joined = "\n".join(_contents(a)[:-1])
        assert "评估维度" in joined and "输出格式" in joined

    def test_quality_eval_layers_disabled(self, monkeypatch):
        from app.services.ai.workflows_quality import ContentQualityEvaluator

        monkeypatch.setattr(app_config, "QUALITY_PROMPT_LAYERS_ENABLED", False)
        captured: list[list] = []

        class _P:
            async def chat(self, messages, **kwargs):
                captured.append(list(messages))
                return "{}"

        asyncio.run(ContentQualityEvaluator(_P())._ai_evaluate_dimensions(
            "内容", "标题", 90.0, 95.0,
        ))
        assert [m.role for m in captured[0]] == ["system", "user"]
        assert "标题" in captured[0][1].content and "内容" in captured[0][1].content

    def test_outline_review_messages_split(self):
        from app.services.ai.workflows_quality import _build_outline_review_messages

        msgs = _build_outline_review_messages(
            '[{"id":"SP1","title":"施工方案","standard":"可行"}]',
            json.dumps([{"id": "1", "title": "第一章"}], ensure_ascii=False),
            summary="项目概述", industry_profile="房建",
        )
        roles = [m.role for m in msgs]
        assert roles[0] == "system"
        assert roles.count("user") >= 3
        texts = _contents(msgs)
        # 稳定规则（审核维度/输出格式）在前部消息；待审核目录在最后
        assert any("审核维度" in t for t in texts[1:-1])
        assert "待审核" in texts[-1] and "第一章" in texts[-1]
        # 换一份目录，稳定前缀不变
        msgs2 = _build_outline_review_messages(
            '[{"id":"SP1","title":"施工方案","standard":"可行"}]',
            json.dumps([{"id": "1", "title": "另一章"}], ensure_ascii=False),
            summary="项目概述", industry_profile="房建",
        )
        assert _contents(msgs)[:-1] == _contents(msgs2)[:-1]

    def test_outline_review_placeholders_actually_substituted(self):
        """BUG-FIX 2026-10-23（P1）：单花括号占位符不得原样发给模型。

        注册表默认模板用 {SCORING_POINTS}/{OUTLINE}/{SUMMARY} 风格，而
        _fill_template 只匹配 __X__ 风格 → 旧实现让 AI「盲审」（字面量占位符）。
        """
        from app.services.ai.workflows_quality import _build_outline_review_texts

        sp = json.dumps(
            [{"id": "SP1", "title": "施工方案", "standard": "可行"}],
            ensure_ascii=False,
        )
        _sys_t, user_t = _build_outline_review_texts(
            sp,
            json.dumps([{"id": "1", "title": "第一章"}], ensure_ascii=False),
            "项目概述",
            "房建",
        )
        for literal in ("{SCORING_POINTS}", "{OUTLINE}", "{SUMMARY}",
                        "__SCORING_POINTS__", "__OUTLINE__", "__SUMMARY__"):
            assert literal not in user_t, f"占位符 {literal} 未替换（盲审缺陷回归）"
        # 真实数据必须进入提示词
        assert "施工方案" in user_t and "第一章" in user_t and "项目概述" in user_t


# ══════════════════════════════════════════════════════════════════════
# H. 配置项默认行为
# ══════════════════════════════════════════════════════════════════════
class TestConfigDefaults:

    def test_cache_observability_defaults(self):
        assert app_config.AI_CACHE_METRICS_ENABLED is True
        assert app_config.AI_CACHE_LOG_ENABLED is True
        # 告警默认关闭（与历史行为一致，生产可显式开启）
        assert app_config.AI_CACHE_HIT_RATE_ALERT_THRESHOLD == 0.0
        assert app_config.AI_CACHE_HIT_RATE_MIN_SAMPLES == 20
        assert app_config.AI_CACHE_ALERT_THROTTLE_SECONDS == 600

    def test_layer_switches_default_on_and_sibling_cap(self):
        assert app_config.OUTLINE_PROMPT_LAYERS_ENABLED is True
        assert app_config.ORCHESTRATION_PROMPT_LAYERS_ENABLED is True
        assert app_config.CHART_PROMPT_LAYERS_ENABLED is True
        assert app_config.QUALITY_PROMPT_LAYERS_ENABLED is True
        assert app_config.CONTENT_SIBLING_MAX_ITEMS == 8
