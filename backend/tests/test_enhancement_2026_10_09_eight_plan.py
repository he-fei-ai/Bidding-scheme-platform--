"""E-8：八项增强优化（E-1~E-7）的守护测试。

覆盖：
- E-3 缓存度量：OpenAI 兼容链路的 cached_tokens 独立口径，Anthropic 口径不回归
- E-2 编排 knowledge 通道：素材目录稳定格式、白名单过滤、编排远端接收、正文注入
- E-4 全篇骨架：前缀匹配注入、正文消息稳定性（无时间戳/随机数 → 缓存前缀可复用）
- E-1 Agent 修复：JSON 解析、宿主 action 执行安全模型（唯一命中/越界/多命中拒绝）
- E-7 图表校验统一入口 + AI 生图成本拦截
- E-6 废标应答覆盖率的纯函数部分
"""

import asyncio

import pytest


# ============================================================
# E-3：缓存成本度量闭环
# ============================================================


def test_cache_metrics_openai_cached_tokens():
    """OpenAI 兼容链路（prompt_tokens_details.cached_tokens）命中率独立可测。"""
    from app.services.ai import cache_metrics

    cache_metrics.reset()
    cache_metrics.record_prompt_cache({
        "prompt_tokens": 1000,
        "completion_tokens": 200,
        "prompt_tokens_details": {"cached_tokens": 600},
    })
    snap = cache_metrics.snapshot()
    oc = snap["openai_prompt_cache"]
    assert oc["prompt_tokens"] == 1000
    assert oc["cached_tokens"] == 600
    assert oc["hit_rate"] == 60.0
    assert oc["requests"] == 1
    # Anthropic 口径不得被 OpenAI 链路污染
    assert snap["prompt_cache"]["requests_with_cache"] == 0
    assert snap["prompt_cache"]["hit_rate"] == 0.0


def test_cache_metrics_anthropic_unchanged():
    """Anthropic 口径保持不变（cache_read / cache_creation 分开累计）。"""
    from app.services.ai import cache_metrics

    cache_metrics.reset()
    cache_metrics.record_prompt_cache({
        "cache_creation_input_tokens": 5000,
        "cache_read_input_tokens": 20000,
        "input_tokens": 25000,
        "output_tokens": 100,
    })
    snap = cache_metrics.snapshot()
    assert snap["prompt_cache"]["cache_read_input_tokens"] == 20000
    assert snap["prompt_cache"]["cache_creation_input_tokens"] == 5000
    assert snap["prompt_cache"]["hit_rate"] == 80.0
    assert snap["prompt_cache"]["total_cached_tokens"] == 25000


def test_cache_metrics_no_usage_noop():
    """无用量信息不得计数（防止稀释命中率）。"""
    from app.services.ai import cache_metrics

    cache_metrics.reset()
    cache_metrics.record_prompt_cache(None)
    cache_metrics.record_prompt_cache({})
    snap = cache_metrics.snapshot()
    assert snap["prompt_cache"]["requests_with_cache"] == 0
    assert snap["openai_prompt_cache"]["requests"] == 0


# ============================================================
# E-2：编排 knowledge 通道
# ============================================================


def test_knowledge_catalog_text_stable_and_sorted():
    """素材目录文本按 id 升序、格式固定（同项目多次请求前缀一致）。"""
    from app.services.ai.workflows_orchestration import _build_knowledge_catalog_text

    text = _build_knowledge_catalog_text([
        {"id": 5, "title": "B", "summary": "bb"},
        {"id": 1, "title": "A", "summary": "aa"},
        {"id": 2, "title": "C", "summary": "cc"},
    ])
    assert text == "K1: A — aa\nK2: C — cc\nK5: B — bb"
    assert _build_knowledge_catalog_text([]) == ""
    assert _build_knowledge_catalog_text(None) == ""
    assert _build_knowledge_catalog_text([{"id": "x", "title": "bad"}]) == ""


def test_knowledge_id_extraction_whitelist():
    """AI 编造 / 越权 ID 一律丢弃；去重保序；上限 3 条。"""
    from app.services.ai.workflows_orchestration import _extract_knowledge_ids

    plan = {"knowledge": {"item_ids": [1, 999, 2, 1, 3, 4]}}
    out = _extract_knowledge_ids(plan, {1, 2, 3})
    assert out == [1, 2, 3]
    assert _extract_knowledge_ids(plan, None) == [1, 999, 2, 3][:3]
    assert _extract_knowledge_ids({"knowledge": {}}, {1}) == []
    assert _extract_knowledge_ids({"knowledge": {"item_ids": ["bad", 2.0]}}, {2}) == [2]


def test_orchestrate_knowledge_pipeline():
    """端到端（无 DB）：编排接收素材目录 → AI 选 ID → 白名单过滤 → 写回 plan；
    编排失败降级 knowledge 置空。"""
    from app.services.ai.workflows_orchestration import (
        _fallback_plan,
        _normalize_plan,
        orchestrate_content,
    )

    assert _fallback_plan({"id": "1"}).get("plan", {}).get("knowledge") == {"item_ids": []}

    norm = _normalize_plan(
        {"section_id": "1", "title": "t",
         "plan": {"table": {"needed": False}, "mermaid": {"needed": False},
                  "image": {"needed": False}, "knowledge": {"item_ids": [7, 8]}}},
        {"id": "1"},
        allowed_knowledge_ids={7},
    )
    assert norm["plan"]["knowledge"]["item_ids"] == [7]

    class _Stub:
        last_messages = None

        async def chat(self, messages, **kwargs):
            self.last_messages = messages
            return (
                '[{"section_id": "1.1.1", "title": "混凝土施工方案", "recommended_type": "text",'
                ' "confidence": 0.5, "reason": "",'
                ' "plan": {"table": {"needed": false}, "mermaid": {"needed": false, "priority": 0,'
                ' "reason": ""}, "image": {"needed": false, "priority": 0, "reason": ""},'
                ' "knowledge": {"item_ids": [1, 999]}}}]'
            )

    stub = _Stub()
    leaves = [{
        "id": "1.1.1", "title": "混凝土施工方案", "parent_id": "1.1", "level": 3,
        "ancestors": [{"id": "1", "title": "施工方案"}, {"id": "1.1", "title": "主体结构"}],
    }]
    catalog = [{"id": 1, "title": "混凝土配合比", "summary": "配合比设计要点"},
               {"id": 2, "title": "钢筋连接", "summary": "套筒连接规范"}]
    res = asyncio.run(orchestrate_content(
        stub, leaves, scoring_standards="", project_overview="某项目",
        concurrency=1, knowledge_catalog=catalog,
    ))
    assert isinstance(res, list) and len(res) == 1
    # 999 不在白名单 → 丢弃；只剩 1
    assert res[0]["plan"]["knowledge"]["item_ids"] == [1]
    assert res[0]["illustration_type"] == "none"
    # 素材目录文本进入提示词
    joined = "\n".join(str(m.content) for m in stub.last_messages)
    assert "K1: 混凝土配合比 — 配合比设计要点" in joined
    assert "K2: 钢筋连接 — 套筒连接规范" in joined
# ============================================================
# E-4：全篇骨架
# ============================================================


def test_skeleton_select_prefix_match():
    from app.services.ai.workflows_orchestration import select_skeleton_for_section

    sk = {"1": "方案定位", "1.1": "主体结构承接", "1.2": "装饰装修承接"}
    text_113 = select_skeleton_for_section(sk, "1.1.3")
    assert "方案定位" in text_113
    assert "主体结构承接" in text_113
    assert "装饰装修承接" not in text_113
    assert select_skeleton_for_section(sk, "") == ""
    assert select_skeleton_for_section(sk, "2.1") == ""
    assert select_skeleton_for_section({}, "1.1") == ""
    # 省略号截断护栏
    long_sk = {"1": "x" * 600}
    assert len(select_skeleton_for_section(long_sk, "1.5", max_chars=50)) <= 51


def test_skeleton_collect_nodes():
    from app.services.ai.workflows_orchestration import _collect_skeleton_nodes

    nodes = _collect_skeleton_nodes([
        {"id": "1.1.1", "ancestors": [{"id": "1", "title": "施工方案"}, {"id": "1.1", "title": "主体"}]},
        {"id": "2.1.2", "ancestors": [{"id": "2", "title": "进度计划"}]},
    ])
    assert nodes == {"1": "施工方案", "1.1": "主体", "2": "进度计划"}
    # 兜底：无 ancestors 时按 id 前缀推导 L1/L2
    assert _collect_skeleton_nodes([{"id": "3.4.5"}]) == {"3": "", "3.4": ""}
    assert _collect_skeleton_nodes([{"id": "6.7"}]) == {"6": ""}
    assert _collect_skeleton_nodes(None) == {}


class _SkelStub:
    def __init__(self, payload):
        self.payload = payload

    async def chat(self, messages, **kwargs):
        return self.payload


def test_generate_full_skeleton_success_and_fallback():
    from app.services.ai.workflows_orchestration import generate_full_skeleton

    leaves = [{"id": "1.1.1", "ancestors": [{"id": "1", "title": "施工方案"}, {"id": "1.1", "title": "主体"}]}]
    sk = asyncio.run(generate_full_skeleton(
        _SkelStub('{"skeleton": {"1": "第一章定位句", "1.1": "主体承接句"}}'),
        leaves, project_overview="P",
    ))
    assert sk.get("1") == "第一章定位句"
    assert sk.get("1.1") == "主体承接句"

    assert asyncio.run(generate_full_skeleton(_SkelStub("不是JSON"), leaves)) == {}
    assert asyncio.run(generate_full_skeleton(_SkelStub('{"skeleton": []}'), leaves)) == {}
    assert asyncio.run(generate_full_skeleton(_SkelStub('{"other": 1}'), leaves)) == {}
    assert asyncio.run(generate_full_skeleton(_SkelStub("x"), [])) == {}


def test_build_chapter_messages_skeleton_and_knowledge_stable():
    """知识素材 + 全篇骨架注入正文消息，且同输入两轮消息逐字一致
    （无时间戳/随机串 → 不破坏缓存前缀）。"""
    from app.services.ai.workflows_sections import build_chapter_content_messages

    chapter = {
        "id": "1.1.1", "title": "主体结构施工方案", "summary": "主体施工安排",
        "level": 3,
        "knowledge_items": [
            {"id": 1, "title": "混凝土配合比", "content": "配合比需按 JGJ55 设计"},
        ],
        "full_skeleton": {"1": "方案定位", "1.1": "主体承接"},
    }
    m1 = build_chapter_content_messages(
        chapter, facts=None, scoring_context="", project_scope="",
        sibling_chapters=None, parent_chapters=None,
    )
    m2 = build_chapter_content_messages(
        chapter, facts=None, scoring_context="", project_scope="",
        sibling_chapters=None, parent_chapters=None,
    )
    parts1 = [str(x.content) for x in m1]
    assert parts1 == [str(x.content) for x in m2]
    joined = "\n".join(parts1)
    assert "知识库素材" in joined
    assert "混凝土配合比" in joined
    assert "全篇叙事骨架" in joined
    assert "主体承接" in joined
    # 稳定性：不允许出现注入时间戳/随机 token（缓存前缀杀手）
    import re

    for part in parts1:
        assert re.search(r"\b20\d\d-\d\d-\d\d \d\d:\d\d(?::\d\d)?\b", part) is None
def test_orchestrate_wrapper_forwards_knowledge_catalog(monkeypatch):
    """sse_content 经 workflows_sections 包装层调用编排 —— 包装层必须透传
    knowledge_catalog，否则 sse 侧的 E-2 通道会被静默掐断（TypeError 被外层
    except 吞掉，编排降级为纯文字而不报错）。"""
    from app.services.ai import workflows_sections

    captured = {}

    async def _fake_orch(*args, **kwargs):
        captured["kwargs"] = kwargs
        return []

    monkeypatch.setattr(workflows_sections, "_orch_impl", _fake_orch)
    asyncio.run(workflows_sections.orchestrate_content(
        object(), [], project_overview="P", knowledge_catalog=[{"id": 1, "title": "A", "summary": "S"}],
    ))
    assert captured["kwargs"].get("knowledge_catalog") == [{"id": 1, "title": "A", "summary": "S"}]
# ============================================================
# E-1：Agent 一致性修复（宿主安全模型）
# ============================================================


def test_agent_parse_and_feedback():
    from app.services import agent_repair

    assert agent_repair._parse_agent_json('```json\n{"done": true}\n```') == {"done": True}
    assert agent_repair._parse_agent_json('说明 {"done": true} 结束') == {"done": True}
    assert agent_repair._parse_agent_json("no json") is None
    assert agent_repair._parse_agent_json("{broken") is None
    fb = agent_repair._format_feedback([
        {"action": "replace", "ok": True, "detail": "已替换"},
        {"action": "replace", "ok": False, "detail": "不唯一"},
    ])
    assert "[REPLACE] 成功: 已替换" in fb
    assert "[REPLACE] 拒绝: 不唯一" in fb
    assert agent_repair._format_feedback([]) == "（本轮暂无操作记录）"


def test_agent_exec_action_safety():
    """宿主安全模型：唯一命中才替换；越界/多命中/未命中一律拒绝。"""
    from app.services import agent_repair

    ctx = {"sections": [{"id": 1, "title": "实施", "content": "工期90天\n质量控制措施"}]}
    ok = agent_repair._exec_action(
        {"action": "replace", "section_id": "1", "old_text": "90天", "new_text": "120天", "reason": "口径统一"}, ctx
    )
    assert ok["ok"] is True
    assert ctx["sections"][0]["content"] == "工期120天\n质量控制措施"

    # 未命中
    miss = agent_repair._exec_action({"action": "replace", "section_id": "1", "old_text": "不存在", "new_text": "x"}, ctx)
    assert miss["ok"] is False
    # 越界 ID
    oob = agent_repair._exec_action({"action": "replace", "section_id": "999", "old_text": "a", "new_text": "b"}, ctx)
    assert oob["ok"] is False
    # 多命中：同一文本两处
    multi_ctx = {"sections": [{"id": 2, "title": "x", "content": "同段文本\n同段文本"}]}
    multi = agent_repair._exec_action({"action": "replace", "section_id": "2", "old_text": "同段文本", "new_text": "改"}, multi_ctx)
    assert multi["ok"] is False
    # 相同 old/new 拒绝
    same = agent_repair._exec_action({"action": "replace", "section_id": "1", "old_text": "工期", "new_text": "工期"}, ctx)
    assert same["ok"] is False
    # search / read
    s = agent_repair._exec_action({"action": "search", "keyword": "工期"}, ctx)
    assert s["ok"] is True and "命中" in s["detail"]
    r = agent_repair._exec_action({"action": "read", "section_id": "1", "start_line": 1, "end_line": 2}, ctx)
    assert r["ok"] is True and "工期120天" in r["detail"]
    # 未知 action
    unknown = agent_repair._exec_action({"action": "hack", "section_id": "1"}, ctx)
    assert unknown["ok"] is False


# ============================================================
# E-7：图表校验统一入口 + 生图成本拦截
# ============================================================


def test_validate_chart_payload_dispatch():
    from app.services.ai.image_engine import validate_chart_payload

    ok, errs = validate_chart_payload("unknown_type", {})
    assert ok is True and errs == []
    r = validate_chart_payload("layout", {})
    assert isinstance(r, tuple) and len(r) == 2
    assert isinstance(r[0], bool) and isinstance(r[1], list)


def test_resolve_image_budget_clamp(monkeypatch):
    from app.services.ai import image_engine
    from app.services.ai import provider_factory

    # resolve_image_budget 内部是懒加载 `from .provider_factory import get_ai_config`，
    # 因此 patch 目标是 provider_factory 模块的函数
    monkeypatch.setattr(provider_factory, "get_ai_config", lambda: {"is_image_enabled": False})
    assert image_engine.resolve_image_budget(6, 1) == 0

    monkeypatch.setattr(provider_factory, "get_ai_config", lambda: {"max_images_per_project": 2})
    assert image_engine.resolve_image_budget(6, 1) == 2

    monkeypatch.setattr(provider_factory, "get_ai_config", lambda: {})
    assert image_engine.resolve_image_budget(6, 1) == 6
    assert image_engine.resolve_image_budget(0) == 0
    assert image_engine.resolve_image_budget(None) == 0
    assert image_engine.resolve_image_budget("x") == 0


# ============================================================
# E-6：废标条款应答覆盖率（纯函数部分）
# ============================================================


def test_rejection_coverage_helpers():
    from app.services.rejection_coverage import _char_bigrams, _extract_clauses

    assert "工期" in _char_bigrams("工期90天")
    assert _char_bigrams("") == set()
    clauses = _extract_clauses({"items": [{"id": "a", "name": "工期", "clause": "工期违约"}]})
    assert clauses and clauses[0]["text"] == "工期违约"
    clauses2 = _extract_clauses([{"name": "x", "standard": "须响应废标条款"}])
    assert clauses2 and "须响应" in clauses2[0]["text"]
    assert _extract_clauses({"foo": "bar"}) == []
    assert _extract_clauses(None) == []