"""2026-10-15 全局事实正文一致性增强回归。

覆盖本轮（对齐易标/参考软件）引入的三个增强点：

  A. 正文生成后 AI 一致性审计显式注入**结构化全局事实**作为裁决基准
     （对齐 `contentGenerationTask.cjs::buildConsistencyAuditMessages`
     的「Step04 全局事实变量」）；不传 global_facts 时行为与历史逐字一致。
  B. `/facts/generate` 提取提示词随「目录输入」开关注入**九大章节选题标准**
     （P2-1，对齐易标「先按招标+目录确定大项」+「提取内容项按章节归类」）；
     默认关=不注入该块，提示词与历史逐字一致。
  C. 正文生成**事实门槛**开关 `FACTS_CONTENT_GATE_ENABLED`（默认关）：
     开启后无全局事实时拒绝生成，对齐易标「请先完成全局事实设定，再生成正文」。

设计原则：不依赖真实 AI（可编程假 provider）；只测行为不测实现。
"""

from __future__ import annotations

import asyncio
import json

from app.db import get_db, init_db
from app.services.ai import workflows_expand as wf

FACTS_FIXTURE = [
    {"key": "totalDuration", "label": "总工期", "value": "90 日历天", "category": "schedule"},
    {"key": "qualityWarranty", "label": "质保期", "value": "3 年", "category": "commitment"},
]


def _new_project(name: str) -> int:
    init_db()
    with get_db() as conn:
        conn.execute("INSERT INTO projects (name, industry) VALUES (?, ?)", (name, "通用"))
        return conn.execute("SELECT id FROM projects ORDER BY id DESC LIMIT 1").fetchone()["id"]


class _AuditFactsProvider:
    """捕获审计 prompt 并按「无冲突」应答的假 provider。"""

    def __init__(self) -> None:
        self.audit_prompts: list[str] = []
        self.repair_calls = 0

    async def chat(self, messages, temperature=0.0, cancel_event=None, **kwargs):
        system = str(getattr(messages[0], "content", ""))
        user = str(getattr(messages[1], "content", ""))
        if "擅长发现正文中的矛盾" in system:
            self.audit_prompts.append(user)
            return json.dumps({"has_conflicts": False, "conflicts": []}, ensure_ascii=False)
        self.repair_calls += 1
        return ""


# ==================== A. 审计注入结构化全局事实 ====================


def test_format_global_facts_for_audit():
    text = wf._format_global_facts_for_audit(FACTS_FIXTURE)
    assert "总工期" in text and "90 日历天" in text
    assert "质保期" in text and "3 年" in text
    assert wf._format_global_facts_for_audit([]) == ""
    assert wf._format_global_facts_for_audit(None) == ""
    # 已格式化文本原样透传（str 输入）
    assert wf._format_global_facts_for_audit("## 全局事实\n- 总工期：90 天") == "## 全局事实\n- 总工期：90 天"


def test_consistency_audit_injects_global_facts_block():
    """开启全局事实输入后，审计 prompt 必须显式出现【全局事实变量】与结构化值。"""
    provider = _AuditFactsProvider()
    pid = _new_project("审计注入事实测试")
    results = [{"id": "1", "status": "completed", "content": "本项目总工期为 90 日历天。"}]
    sections = {"1": {"title": "第一章"}}

    out = asyncio.run(
        wf.consistency_audit_content(
            pid,
            provider,
            results,
            sections,
            "项目摘要",
            "招标上下文",
            None,
            global_facts=FACTS_FIXTURE,
            facts_policy="fabricate",
        )
    )
    assert out is results
    assert provider.audit_prompts, "审计必须至少执行一次"
    prompt = provider.audit_prompts[0]
    assert "【全局事实变量" in prompt
    assert "总工期" in prompt and "90 日历天" in prompt
    assert "质保期" in prompt and "3 年" in prompt
    # 审计只修冲突、不重写全文：facts 输入不触发「缺失报告」类描述混淆
    assert "不要报告缺失" in prompt


def test_consistency_audit_without_facts_unchanged():
    """旧调用方不传 global_facts（位置参数）时审计 prompt 不含【全局事实变量】块。"""
    provider = _AuditFactsProvider()
    pid = _new_project("审计无事实测试")
    results = [{"id": "1", "status": "completed", "content": "正文内容。" * 60}]
    sections = {"1": {"title": "第一章"}}

    out = asyncio.run(
        wf.consistency_audit_content(pid, provider, results, sections, "项目摘要", "BT")
    )
    assert out is results
    assert "【全局事实变量" not in provider.audit_prompts[0]


def test_consistency_audit_adds_completeness_rule_for_placeholder():
    """placeholder 策略下审计 prompt 追加【缺值口径】，防止把【待填写】误报冲突。"""
    provider = _AuditFactsProvider()
    pid = _new_project("审计占位策略测试")
    results = [{"id": "1", "status": "completed", "content": "项目经理：【待填写】。"}]
    sections = {"1": {"title": "第一章"}}

    asyncio.run(
        wf.consistency_audit_content(
            pid,
            provider,
            results,
            sections,
            "项目摘要",
            "BT",
            None,
            global_facts=FACTS_FIXTURE,
            facts_policy="placeholder",
        )
    )
    prompt = provider.audit_prompts[0]
    assert "【缺值口径】" in prompt
    assert "【待填写" in prompt
# ==================== B. /facts/generate 注入九大章节选题标准 ====================


def _patch_facts_ai(monkeypatch, ai_payload, captured: dict):
    from app.routers import facts as facts_router
    from app.services.ai import provider_factory, workflows

    monkeypatch.setattr(
        facts_router, "get_facts_file_text", lambda _pid: "招标文件正文：工期要求 90 日历天。"
    )
    monkeypatch.setattr(provider_factory, "get_ai_config", lambda: object())
    monkeypatch.setattr(provider_factory, "create_provider", lambda _cfg: object())

    async def _fake_collect(_provider, _messages, **_kwargs):
        captured["user_prompt"] = str(getattr(_messages[1], "content", ""))
        return ai_payload

    monkeypatch.setattr(workflows, "collect_json_response", _fake_collect)


def test_generate_facts_injects_chapter_scope_when_outline_enabled(monkeypatch):
    """开启目录输入（FACTS_OUTLINE_INPUT_ENABLED=1）时，提取提示词含九大章节选题标准。"""
    from app.models import FactsGenerateRequest
    from app.routers import facts as facts_router

    monkeypatch.setattr(facts_router, "outline_input_enabled", lambda: True)
    monkeypatch.setattr(facts_router, "segmented_extraction_enabled", lambda: False)
    # chapter_scope 默认随 outline_input 启用；此处显式钉住开启，避免环境变量干扰
    monkeypatch.setattr(facts_router, "chapter_scope_enabled", lambda: True)
    captured: dict = {}
    _patch_facts_ai(
        monkeypatch,
        {"projectName": "示范工程", "scheduleDuration": "90 日历天"},
        captured,
    )
    pid = _new_project("章节选题注入测试")
    # 项目已有目录行时，目录输入块才会出现（与 P1-2 同一前提）
    with get_db() as conn:
        conn.execute(
            "INSERT INTO sections (project_id, title, level, sort_order, section_type, status) "
            "VALUES (?, ?, 1, 1, 'leaf', 'pending')",
            (pid, "施工进度计划及保证措施"),
        )

    result = asyncio.run(
        facts_router.generate_facts(pid, FactsGenerateRequest(missing_value_policy="fabricate"))
    )
    assert result["items"]
    prompt = captured["user_prompt"]
    assert "选题范围参考" in prompt
    assert "工程概况与编制依据" in prompt
    assert "专项施工方案（危大工程）" in prompt
    assert "应急预案与风险控制" in prompt
    # 目录输入块也存在（两开关同开）
    assert "技术方案目录（事实大项覆盖范围参考" in prompt


def test_generate_facts_omits_chapter_scope_by_default(monkeypatch):
    """默认（开关关）时提取提示词不含九大章节选题块，提示词与历史逐字一致。"""
    from app.models import FactsGenerateRequest
    from app.routers import facts as facts_router

    monkeypatch.setattr(facts_router, "outline_input_enabled", lambda: False)
    monkeypatch.setattr(facts_router, "segmented_extraction_enabled", lambda: False)
    monkeypatch.setattr(facts_router, "chapter_scope_enabled", lambda: False)
    captured: dict = {}
    _patch_facts_ai(monkeypatch, {"projectName": "示范工程"}, captured)
    pid = _new_project("章节选题默认关闭测试")

    result = asyncio.run(
        facts_router.generate_facts(pid, FactsGenerateRequest(missing_value_policy="fabricate"))
    )
    assert result["items"]
    assert "选题范围参考" not in captured["user_prompt"]
    assert "技术方案目录（事实大项覆盖范围参考" not in captured["user_prompt"]


# ==================== C. 正文生成事实门槛开关 ====================


def test_content_facts_gate_switch_default_off(monkeypatch):
    from app.services import facts_service as fs

    monkeypatch.delenv("FACTS_CONTENT_GATE_ENABLED", raising=False)
    assert fs.content_facts_gate_enabled() is False
    monkeypatch.setenv("FACTS_CONTENT_GATE_ENABLED", "1")
    assert fs.content_facts_gate_enabled() is True
    monkeypatch.setenv("FACTS_CONTENT_GATE_ENABLED", "0")
    assert fs.content_facts_gate_enabled() is False
# ==================== D. 知识库补充（P2-2）与章节范围开关增强 ====================


def test_chapter_scope_disjoint_switch(monkeypatch):
    """章节选题块可独立开启：目录输入关、FACTS_CHAPTER_SCOPE_ENABLED=1 时仍生效。"""
    from app.services import facts_service as fs

    monkeypatch.setattr(fs, "outline_input_enabled", lambda: False)
    monkeypatch.setenv("FACTS_CHAPTER_SCOPE_ENABLED", "1")
    assert fs.chapter_scope_enabled() is True
    monkeypatch.setenv("FACTS_CHAPTER_SCOPE_ENABLED", "0")
    assert fs.chapter_scope_enabled() is False


def test_kb_patch_switch_default_off(monkeypatch):
    from app.services import facts_service as fs

    monkeypatch.delenv("FACTS_KNOWLEDGE_PATCH_ENABLED", raising=False)
    assert fs.kb_patch_enabled() is False
    monkeypatch.setenv("FACTS_KNOWLEDGE_PATCH_ENABLED", "1")
    assert fs.kb_patch_enabled() is True


def test_merge_kb_patch_items_base_priority_and_placeholder_yield():
    """知识库补充与基线合并后：同 key 基线优先、基线占位让位实值、缺失项追加。"""
    from app.services.facts_service import merge_kb_patch_items, normalize_fact_items

    base = [
        {"key": "scheduleDuration", "label": "建设工期", "value": "90 日历天", "category": "schedule"},
        {"key": "projectManager", "label": "项目经理", "value": "【待填写】", "category": "personnel"},
    ]
    patch = [
        {"key": "scheduleDuration", "label": "建设工期", "value": "120 日历天"},
        {"key": "projectManager", "label": "项目经理", "value": "张三"},
        {"key": "warrantyPeriod", "label": "质保期", "value": "3 年"},
    ]
    merged = merge_kb_patch_items(base, patch)
    # 基线优先 + 占位让位实值 + 缺失追加 的统一去重语义由 normalize_fact_items 落地
    normalized = normalize_fact_items(merged, fallback_text="", policy="fabricate")
    by_key = {i["key"]: i for i in normalized}
    # 同 key 冲突：基线（招标）优先，不被知识库覆盖
    assert by_key["scheduleDuration"]["value"] == "90 日历天"
    # 基线占位 → 知识库实值替换
    assert by_key["projectManager"]["value"] == "张三"
    # 基线缺失 → 追加且标记知识库来源
    assert by_key["warrantyPeriod"]["value"] == "3 年"
    assert by_key["warrantyPeriod"]["source"] == "knowledge"


def test_generate_facts_applies_knowledge_patch(monkeypatch):
    """开启 FACTS_KNOWLEDGE_PATCH_ENABLED 后，知识库条目二次提取并入最终事实清单。"""
    from app.models import FactsGenerateRequest
    from app.routers import facts as facts_router
    from app.services import knowledge as kb_svc
    from app.services.ai import provider_factory, workflows

    monkeypatch.setattr(
        facts_router, "get_facts_file_text", lambda _pid: "招标文件正文：工期要求 90 日历天。"
    )
    monkeypatch.setattr(facts_router, "kb_patch_enabled", lambda: True)
    monkeypatch.setattr(facts_router, "segmented_extraction_enabled", lambda: False)
    monkeypatch.setattr(facts_router, "outline_input_enabled", lambda: False)

    # 知识库服务桩：返回一条含质保承诺的条目
    monkeypatch.setattr(
        kb_svc,
        "get_relevant_items",
        lambda **kw: [
            {
                "title": "历史项目售后承诺",
                "summary": "质保期 3 年",
                "content": "本项目免费质保期为 3 年，故障 24 小时响应。",
            }
        ],
    )

    calls = {"n": 0}

    async def _fake_collect(_provider, _messages, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:  # 第一次：基线提取
            return {"projectName": "示范工程", "scheduleDuration": "90 日历天"}
        # 第二次：知识库补充提取
        return {"warrantyPeriod": "3 年"}

    monkeypatch.setattr(provider_factory, "get_ai_config", lambda: object())
    monkeypatch.setattr(provider_factory, "create_provider", lambda _cfg: object())
    monkeypatch.setattr(workflows, "collect_json_response", _fake_collect)

    pid = _new_project("知识库补充测试")
    result = asyncio.run(
        facts_router.generate_facts(pid, FactsGenerateRequest(missing_value_policy="fabricate"))
    )
    assert calls["n"] == 2, "基线 + 知识库各一次提取"
    items = {i["key"]: i for i in result["items"]}
    assert items["warrantyPeriod"]["value"] == "3 年"
    assert items["warrantyPeriod"]["source"] == "knowledge"
    assert result["knowledge_patches_applied"] >= 1
    # 基线事实不受知识库干扰
    assert items["scheduleDuration"]["value"] == "90 日历天"


# ==================== E. 审计超长节分块全量送审（单元级） ====================


def test_split_section_into_chunks_paragraph_boundaries():
    from app.services.ai import workflows_expand as wf

    # 普通节不切
    assert wf._split_section_into_chunks("短短内容", 10) == ["短短内容"]
    # 多段落按行切，每块不超上限
    text = "\n".join("第%d段，内容内容内容内容。" % i for i in range(1, 21))
    chunks = wf._split_section_into_chunks(text, 40)
    assert len(chunks) > 1
    assert all(len(c) <= 40 for c in chunks)
    assert "".join(chunks).replace("，", "，")  # 内容无损（块间只断开换行/硬切点）
    # 连续长行按字符硬切
    hard = wf._split_section_into_chunks("甲" * 100, 30)
    assert all(len(c) <= 30 for c in hard)
    assert sum(len(c) for c in hard) == 100


def test_audit_section_render_chars_counts_full_content_for_oversize():
    from app.services.ai import workflows_expand as wf

    # 普通节：等于正文长度（行为不变）
    assert wf._audit_section_render_chars("abc", 10) == 3
    # 超长节：按全量正文 + 块标识别开销计重（> 截断长度）
    large = "甲" * 500
    chars = wf._audit_section_render_chars(large, 50)
    assert chars > 500
    assert chars <= 500 + 20 * 10  # 上界内