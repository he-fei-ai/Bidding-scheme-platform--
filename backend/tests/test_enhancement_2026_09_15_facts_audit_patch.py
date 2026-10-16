"""回归测试：全局事实三档策略 / 一致性审计分组并发 / 补丁区间占比保护 /
废标项跨分块缺失状态机 / 错别字一键修正。

ENHANCE 2026-09-15（第七轮）：对照「易标投标工具箱」补完上一轮登记的候选清单。

覆盖范围：
  A. 全局事实缺失值策略（fabricate / omit / placeholder）
  B. 一致性审计：字符口径分组 + 首组串行预热 + 其余组并发
  C. text_patch：区间占比保护（整段重写拒绝）
  D. 废标项：跨分块「疑似缺失」pending → resolved/confirmed 状态机
  E. 错别字：一键修正 + 整文重新锚定位置
  F. 报表与路由接线

设计原则：只测行为不测实现；不依赖真实 AI 与外部网络（用可编程假 provider）。
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest

from app.db import get_db, init_db


# ==================== 公共工具 ====================


def _new_project(name: str) -> int:
    init_db()
    with get_db() as conn:
        conn.execute("INSERT INTO projects (name, industry) VALUES (?, ?)", (name, "通用"))
        return conn.execute("SELECT id FROM projects ORDER BY id DESC LIMIT 1").fetchone()["id"]


def _group_index_from_prompt(prompt: str) -> int:
    match = re.search(r"第\s*(\d+)\s*/\s*(\d+)\s*组", prompt)
    return int(match.group(1)) if match else 0


def _extract_current_body(repair_prompt: str) -> str:
    marker = "【当前正文】\n"
    start = repair_prompt.find(marker)
    if start < 0:
        return ""
    body = repair_prompt[start + len(marker) :]
    return body.split("\n\n请直接输出")[0]


class _FakeAuditProvider:
    """可编程的一致性审计/修复假 provider。

    - 审计调用按提示词里的「第 N/M 组」查表返回冲突；
    - 修复调用返回预设文本（None 表示"原样返回当前正文"）；
    - 记录并发峰值，用于验证首组串行 + 其余并发的调度。
    """

    def __init__(
        self,
        conflicts: dict[int, list[dict]] | None = None,
        *,
        repair_text: str | None = None,
        delay: float = 0.0,
        fail_group: int | None = None,
    ) -> None:
        self.conflicts = conflicts or {}
        self.repair_text = repair_text
        self.delay = delay
        self.fail_group = fail_group
        self.audit_prompts: list[str] = []
        self.repair_calls = 0
        self.active = 0
        self.max_active = 0

    async def chat(self, messages, temperature=0.0, cancel_event=None, **kwargs):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            system = str(getattr(messages[0], "content", ""))
            user = str(getattr(messages[1], "content", ""))
            if "擅长发现正文中的矛盾" in system:
                self.audit_prompts.append(user)
                index = _group_index_from_prompt(user)
                if self.fail_group == index:
                    raise RuntimeError("模拟第 %d 组 AI 失败" % index)
                items = self.conflicts.get(index, [])
                return json.dumps(
                    {"has_conflicts": bool(items), "conflicts": items}, ensure_ascii=False
                )
            self.repair_calls += 1
            if self.repair_text is None:
                return _extract_current_body(user)
            return self.repair_text
        finally:
            self.active -= 1


# ==================== A. 全局事实缺失值策略 ====================


def test_facts_policy_normalization():
    from app.routers import facts as facts_router

    assert facts_router.normalize_facts_policy("omit") == "omit"
    assert facts_router.normalize_facts_policy(" PLACEHOLDER ") == "placeholder"
    assert facts_router.normalize_facts_policy("fabricate") == "fabricate"
    # 非法值静默回退默认档，不抛错（旧前端不传该字段时必须保持历史行为）
    assert facts_router.normalize_facts_policy(None) == "fabricate"
    assert facts_router.normalize_facts_policy("not-a-policy") == "fabricate"


def test_policy_rule_text_differs_per_policy():
    from app.routers import facts as facts_router

    fabricate = facts_router._policy_rule_text("fabricate")
    omit = facts_router._policy_rule_text("omit")
    placeholder = facts_router._policy_rule_text("placeholder")

    assert "可模拟生成" in fabricate
    assert "严禁" in omit and "编造" in omit
    assert "【待填写】" in placeholder
    assert len({fabricate, omit, placeholder}) == 3


def test_schedule_fallback_value_follows_policy():
    from app.routers import facts as facts_router

    assert facts_router._schedule_fallback_value("fabricate") == "满足招标文件工期要求，按合同约定执行"
    assert facts_router._schedule_fallback_value("omit") == "按招标文件要求及合同约定执行"
    assert facts_router._schedule_fallback_value("placeholder") == "【待填写】"
    # 非法策略回退默认档，避免兜底文案为空
    assert facts_router._schedule_fallback_value("bogus") == facts_router._schedule_fallback_value(
        "fabricate"
    )


@pytest.mark.parametrize(
    ("policy", "expected_value", "expect_flag"),
    [
        ("fabricate", "满足招标文件工期要求，按合同约定执行", False),
        ("omit", "按招标文件要求及合同约定执行", False),
        ("placeholder", "【待填写】", True),
    ],
)
def test_normalize_facts_schedule_fallback_per_policy(policy, expected_value, expect_flag):
    from app.routers import facts as facts_router

    items = facts_router._normalize_facts(
        [{"key": "projectName", "label": "项目名称", "value": "某项目", "category": "basic"}],
        fallback_text="本工程无工期信息",
        policy=policy,
    )
    schedule = next(item for item in items if item["key"] == "scheduleDuration")
    assert schedule["value"] == expected_value
    assert bool(schedule.get("needs_manual_input")) is expect_flag


def test_normalize_facts_keeps_existing_schedule_untouched():
    from app.routers import facts as facts_router

    items = facts_router._normalize_facts(
        [{"key": "totalDuration", "label": "总工期", "value": "420 日历天", "category": "schedule"}],
        policy="placeholder",
    )
    assert len(items) == 1
    assert items[0]["value"] == "420 日历天"


def _patch_facts_ai(monkeypatch, ai_payload):
    """把 /facts/generate 依赖的 AI 与文件读取全部替换成假实现。"""
    from app.routers import facts as facts_router
    from app.services.ai import provider_factory, workflows

    monkeypatch.setattr(
        facts_router, "get_facts_file_text", lambda _pid: "招标文件正文：工期要求 450 日历天。"
    )
    monkeypatch.setattr(provider_factory, "get_ai_config", lambda: object())
    monkeypatch.setattr(provider_factory, "create_provider", lambda _cfg: object())

    async def _fake_collect(_provider, _messages, **_kwargs):
        return ai_payload

    monkeypatch.setattr(workflows, "collect_json_response", _fake_collect)


@pytest.mark.parametrize(
    ("policy", "expected_schedule"),
    [
        ("fabricate", "满足招标文件工期要求，按合同约定执行"),
        ("omit", "按招标文件要求及合同约定执行"),
        ("placeholder", "【待填写】"),
    ],
)
def test_generate_facts_persists_policy_and_fallback(monkeypatch, policy, expected_schedule):
    from app.models import FactsGenerateRequest
    from app.routers import facts as facts_router

    project_id = _new_project(f"事实策略-{policy}")
    _patch_facts_ai(
        monkeypatch,
        [{"key": "projectName", "label": "项目名称", "value": "示范工程", "category": "basic"}],
    )

    result = asyncio.run(
        facts_router.generate_facts(project_id, FactsGenerateRequest(missing_value_policy=policy))
    )

    assert result["missing_value_policy"] == policy
    schedule = next(item for item in result["items"] if item["key"] == "scheduleDuration")
    assert schedule["value"] == expected_schedule
    assert result["placeholder_count"] == (1 if policy == "placeholder" else 0)

    # 落库并可回读，避免"刷新后策略又变回默认档"
    stored = asyncio.run(facts_router.get_facts(project_id))
    assert stored["missing_value_policy"] == policy
    assert stored["placeholder_count"] == (1 if policy == "placeholder" else 0)


def test_generate_facts_defaults_to_fabricate_when_body_missing(monkeypatch):
    """旧前端仍以「无请求体」调用该端点，必须保持历史行为。"""
    from app.routers import facts as facts_router

    project_id = _new_project("事实策略-无请求体")
    _patch_facts_ai(monkeypatch, [{"key": "projectName", "label": "项目名称", "value": "X"}])

    result = asyncio.run(facts_router.generate_facts(project_id, None))
    assert result["missing_value_policy"] == "fabricate"


def test_generate_facts_rejects_unknown_policy_by_falling_back(monkeypatch):
    from app.models import FactsGenerateRequest
    from app.routers import facts as facts_router

    project_id = _new_project("事实策略-非法")
    _patch_facts_ai(monkeypatch, [{"key": "projectName", "label": "项目名称", "value": "X"}])

    result = asyncio.run(
        facts_router.generate_facts(project_id, FactsGenerateRequest(missing_value_policy="unknown"))
    )
    assert result["missing_value_policy"] == "fabricate"


def test_get_facts_returns_default_policy_without_row():
    from app.routers import facts as facts_router

    # BUG-FIX 2026-09-18：facts 路由补齐了项目级权限校验
    # （`_ensure_project_access` → `require_project_permission`），与 sections 路由口径一致：
    # 项目不存在时返回 404，而不是静默返回空数据。因此本用例改用真实项目，
    # 保持原意图不变 —— 验证「项目存在但尚无 analysis 行」时返回默认策略。
    project_id = _new_project("事实策略-无 analysis 行")

    result = asyncio.run(facts_router.get_facts(project_id))
    assert result == {
        "items": [],
        "confirmed": False,
        "missing_value_policy": "fabricate",
    }


def test_get_facts_rejects_unknown_project():
    """项目不存在时必须 404（横向越权防护的一部分：先确认项目归属）。"""
    from fastapi import HTTPException

    from app.routers import facts as facts_router

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(facts_router.get_facts(99999999))
    assert exc_info.value.status_code == 404


# ==================== B. 一致性审计分组与并发 ====================


def test_audit_group_budget_matches_prompt_limit():
    """口径必须统一：分组预算就是送审提示词上限（此前 30万字 vs 3万字符）。"""
    from app.services.ai import workflows_expand as wf

    assert wf.AUDIT_GROUP_CHAR_LIMIT < 300000
    assert wf.AUDIT_SECTION_CHAR_LIMIT <= wf.AUDIT_GROUP_CHAR_LIMIT
    assert wf.AUDIT_GROUP_CONCURRENCY >= 1


def test_split_audit_groups_uses_truncated_char_count(monkeypatch):
    from app.services.ai import workflows_expand as wf

    monkeypatch.setattr(wf, "AUDIT_GROUP_CHAR_LIMIT", 100)
    monkeypatch.setattr(wf, "AUDIT_SECTION_CHAR_LIMIT", 40)

    sections = [{"id": i, "content": "甲" * 40} for i in range(1, 10)]
    groups = wf._split_audit_groups(sections)
    assert [len(group) for group in groups] == [2, 2, 2, 2, 1]
    # 组内实际送审字符数不超过预算
    for group in groups:
        total = sum(min(len(item["content"]), 40) for item in group)
        assert total <= 100


def test_split_audit_groups_keeps_oversized_single_section_in_own_group(monkeypatch):
    from app.services.ai import workflows_expand as wf

    monkeypatch.setattr(wf, "AUDIT_GROUP_CHAR_LIMIT", 100)
    monkeypatch.setattr(wf, "AUDIT_SECTION_CHAR_LIMIT", 40)

    groups = wf._split_audit_groups([{"id": 1, "content": "甲" * 5000}])
    assert [len(group) for group in groups] == [1]


def test_split_audit_groups_empty():
    from app.services.ai import workflows_expand as wf

    assert wf._split_audit_groups([]) == []


def test_consistency_audit_first_group_serial_then_concurrent(monkeypatch):
    from app.services.ai import workflows_expand as wf

    monkeypatch.setattr(wf, "AUDIT_GROUP_CHAR_LIMIT", 100)
    monkeypatch.setattr(wf, "AUDIT_SECTION_CHAR_LIMIT", 40)
    monkeypatch.setattr(wf, "AUDIT_GROUP_CONCURRENCY", 2)

    results = [{"id": i, "status": "completed", "content": "甲" * 40} for i in range(1, 10)]
    sections = {i: {"title": f"章节{i}"} for i in range(1, 10)}
    provider = _FakeAuditProvider({}, delay=0.02)
    pid = _new_project("一致性审计并发测试")

    out = asyncio.run(wf.consistency_audit_content(pid, provider, results, sections, "事实", "", None))

    assert out is results, "审计必须原地返回同一列表，调用方依赖它继续落库"
    assert len(provider.audit_prompts) == 5
    # 首组串行：第一条审计调用必然是第 1 组
    assert "第 1/5 组" in provider.audit_prompts[0]
    # 其余组并发（且不超过上限）
    assert provider.max_active >= 2
    assert provider.max_active <= 2


def test_consistency_audit_applies_repair(monkeypatch):
    from app.services.ai import workflows_expand as wf

    monkeypatch.setattr(wf, "AUDIT_GROUP_CHAR_LIMIT", 200)
    monkeypatch.setattr(wf, "AUDIT_SECTION_CHAR_LIMIT", 200)

    results = [
        {"id": 1, "status": "completed", "content": "甲" * 200},
        {"id": 2, "status": "completed", "content": "甲" * 200},
    ]
    sections = {1: {"title": "第一章"}, 2: {"title": "第二章"}}
    provider = _FakeAuditProvider(
        {1: [{"section_id": "2", "issue": "工期不一致", "suggestion": "统一为 450 天"}]},
        repair_text="乙" * 200,
    )
    pid = _new_project("一致性审计修复测试")

    asyncio.run(wf.consistency_audit_content(pid, provider, results, sections, "事实", "", None))

    assert results[0]["content"] == "甲" * 200
    assert results[1]["content"] == "乙" * 200
    assert provider.repair_calls == 1


def test_consistency_audit_rejects_truncated_repair(monkeypatch):
    """修复结果明显短于原文 → 疑似截断，必须拒绝应用（防长章节内容丢失）。"""
    from app.services.ai import workflows_expand as wf

    monkeypatch.setattr(wf, "AUDIT_GROUP_CHAR_LIMIT", 200)
    monkeypatch.setattr(wf, "AUDIT_SECTION_CHAR_LIMIT", 200)

    results = [{"id": 1, "status": "completed", "content": "甲" * 200}]
    sections = {1: {"title": "第一章"}}
    provider = _FakeAuditProvider(
        {1: [{"section_id": "1", "issue": "x", "suggestion": "y"}]},
        repair_text="乙" * 50,
    )
    pid = _new_project("一致性审计截断修复测试")

    asyncio.run(wf.consistency_audit_content(pid, provider, results, sections, "事实", "", None))
    assert results[0]["content"] == "甲" * 200


def test_consistency_audit_isolates_group_failure(monkeypatch):
    from app.services.ai import workflows_expand as wf

    monkeypatch.setattr(wf, "AUDIT_GROUP_CHAR_LIMIT", 200)
    monkeypatch.setattr(wf, "AUDIT_SECTION_CHAR_LIMIT", 200)

    results = [
        {"id": 1, "status": "completed", "content": "甲" * 200},
        {"id": 2, "status": "completed", "content": "甲" * 200},
    ]
    sections = {1: {"title": "第一章"}, 2: {"title": "第二章"}}
    provider = _FakeAuditProvider(
        {1: [{"section_id": "1", "issue": "x", "suggestion": "y"}]},
        repair_text="乙" * 200,
        fail_group=2,
    )
    pid = _new_project("一致性审计组隔离测试")

    # 第 2 组抛错不应影响第 1 组的修复结果
    asyncio.run(wf.consistency_audit_content(pid, provider, results, sections, "事实", "", None))
    assert results[0]["content"] == "乙" * 200
    assert results[1]["content"] == "甲" * 200


def test_consistency_audit_skips_when_cancelled(monkeypatch):
    from app.services.ai import workflows_expand as wf

    cancel = asyncio.Event()
    cancel.set()
    results = [{"id": 1, "status": "completed", "content": "甲" * 200}]
    provider = _FakeAuditProvider({})
    pid = _new_project("一致性审计取消测试")

    out = asyncio.run(
        wf.consistency_audit_content(pid, provider, results, {1: {}}, "事实", "", cancel)
    )
    assert out is results
    assert provider.audit_prompts == []


def test_consistency_audit_logs_oversized_section_truncation(monkeypatch, caplog):
    """超长节不再抽样截断：拆块全量送审并留下可见日志（修复覆盖盲区）。

    ENHANCE 2026-10-15：旧实现只送审前 `AUDIT_SECTION_CHAR_LIMIT` 字符，
    正文尾部永远不被审计。现在分块续送，全部块都进审计 prompt，
    同时保留「超出单节送审上限」痕迹供排障。
    """
    from app.services.ai import workflows_expand as wf

    monkeypatch.setattr(wf, "AUDIT_GROUP_CHAR_LIMIT", 60)
    monkeypatch.setattr(wf, "AUDIT_SECTION_CHAR_LIMIT", 50)

    results = [{"id": 1, "status": "completed", "content": "甲" * 500}]
    provider = _FakeAuditProvider({})
    pid = _new_project("一致性审计超长节截断测试")

    with caplog.at_level("WARNING"):
        asyncio.run(
            wf.consistency_audit_content(pid, provider, results, {1: {"title": "长章"}}, "事实", "", None)
        )

    assert any("超出单节送审上限" in record.message for record in caplog.records)
    assert provider.audit_prompts
    prompt = provider.audit_prompts[0]
    # 500 字符 / 每块 50 = 10 块：最后一块的定位标记出现 = 全量送审
    assert "第 10/10 块" in prompt
    # 500 个“甲”全部送审（旧实现仅 50 个）
    assert prompt.count("甲") == 500


# ==================== C. 补丁区间占比保护 ====================


def test_disproportionate_guard_skips_short_content():
    from app.services import text_patch as tp

    # 短正文整段替换是正常场景，不应误伤
    allowed, detail = tp.is_disproportionate_patch("甲" * 100, "甲" * 100)
    assert allowed is False
    assert detail["content_chars"] == 100


def test_disproportionate_guard_flags_whole_section_rewrite():
    from app.services import text_patch as tp

    content = "甲" * 1000
    allowed, detail = tp.is_disproportionate_patch(content, "甲" * 950)
    assert allowed is True
    assert detail["share"] >= tp.DISPROPORTIONATE_RATIO


def test_disproportionate_guard_allows_local_patch():
    from app.services import text_patch as tp

    content = "甲" * 1000
    allowed, _detail = tp.is_disproportionate_patch(content, "甲" * 200)
    assert allowed is False


def test_apply_exact_patch_rejects_whole_section_rewrite():
    from app.services import text_patch as tp

    content = "甲" * 800
    with pytest.raises(tp.PatchApplyError, match="疑似整段重写"):
        tp.apply_exact_patch(content, {"old_text": "甲" * 780, "new_text": "乙" * 780})


def test_apply_exact_patch_guard_can_be_disabled():
    from app.services import text_patch as tp

    content = "甲" * 800
    result = tp.apply_exact_patch(
        content,
        {"old_text": "甲" * 780, "new_text": "乙" * 780},
        disproportionate_ratio=None,
    )
    assert result == "乙" * 780 + "甲" * 20


def test_apply_patches_isolates_disproportionate_rejection():
    from app.services import text_patch as tp

    content = "甲" * 500 + "本地句子。" + "乙" * 500
    result = tp.apply_patches(
        content,
        [
            {"old_text": content, "new_text": "整段重写"},  # 占比 100% → 拒绝
            {"old_text": "本地句子。", "new_text": "改后句子。"},
        ],
    )
    assert result["applied_count"] == 1
    assert result["rejected_count"] == 1
    assert "疑似整段重写" in result["errors"][0]
    assert "改后句子。" in result["content"]


def test_describe_patch_match_reports_proportion():
    from app.services import text_patch as tp

    detail = tp.describe_patch_match("甲" * 800, {"old_text": "甲" * 780, "new_text": "x"})
    assert detail["disproportionate"] is True
    assert detail["proportion"]["share"] >= 0.9


# ==================== D. 废标项跨分块缺失状态机 ====================


def test_extract_claim_keywords_prefers_quoted_material_names():
    from app.services import rejection

    keywords = rejection.extract_claim_keywords(
        {"description": "未找到「安全生产许可证」的扫描件", "location": "附件清单"},
    )
    assert keywords[0] == "安全生产许可证"


def test_extract_claim_keywords_requires_material_like_tokens():
    from app.services import rejection

    # "本项目" 是停用词、"施工" 不是材料名（无材料类后缀）→ 都抽不出来
    assert rejection.extract_claim_keywords({"description": "本项目施工部分内容缺失"}) == []


def test_extract_claim_keywords_dedupes_and_limits():
    from app.services import rejection

    keywords = rejection.extract_claim_keywords(
        {"description": "缺少 安全生产许可证、安全生产许可证、质量保证书、履约保函"},
    )
    assert keywords == ["安全生产许可证", "质量保证书", "履约保函"]


def test_resolve_missing_claims_marks_resolved_when_full_text_has_clue():
    from app.services import rejection

    full_text = "第一章 资格审查资料\n\n安全生产许可证（证书编号：12345）\n"
    issues = [
        {
            "id": "i1",
            "description": "未找到「安全生产许可证」，疑似材料缺失",
            "evidence_kind": "structural",
        },
    ]
    rejection.resolve_missing_material_claims(issues, full_text)

    assert issues[0]["missing_state"] == rejection.MISSING_STATE_RESOLVED
    assert "疑似分块误判" in issues[0]["missing_state_reason"]
    assert issues[0]["line_hint"].startswith("第 ")
    assert "安全生产许可证" in issues[0]["excerpt"]


def test_resolve_missing_claims_confirms_when_no_clue_anywhere():
    from app.services import rejection

    issues = [
        {
            "id": "i1",
            "description": "未找到「安全生产许可证」",
            "evidence_kind": "structural",
        },
    ]
    rejection.resolve_missing_material_claims(issues, "第一章 施工方案\n\n无相关材料。\n")

    assert issues[0]["missing_state"] == rejection.MISSING_STATE_CONFIRMED
    assert "缺失判断成立" in issues[0]["missing_state_reason"]


def test_resolve_missing_claims_keeps_pending_without_keywords():
    from app.services import rejection

    issues = [{"id": "i1", "description": "部分材料可能缺失", "evidence_kind": "structural"}]
    rejection.resolve_missing_material_claims(issues, "任意正文")

    assert issues[0]["missing_state"] == rejection.MISSING_STATE_PENDING
    # pending 不做任何排除，也不假装定位成功
    assert issues[0].get("line_hint") is None
    assert issues[0].get("excerpt") is None


def test_resolve_missing_claims_ignores_quote_based_issues():
    from app.services import rejection

    issues = [
        {
            "id": "i1",
            "description": "工期与「安全生产许可证」无关",
            "evidence_kind": "quote",
            "validated": True,
        },
    ]
    rejection.resolve_missing_material_claims(issues, "安全生产许可证 已提交")
    assert "missing_state" not in issues[0]


def test_finalize_rejection_issues_wires_missing_state():
    from app.services import rejection

    bid_doc = {"id": 1, "name": "技术标.docx"}
    issues = [
        {
            "id": "i1",
            "description": "未找到「安全生产许可证」",
            "severity": "critical",
            "check_item_id": "r001",
        },
    ]
    text = "第一章\n\n安全生产许可证（有效期至 2027 年）\n"
    finalized = rejection._finalize_rejection_issues(
        issues, bid_doc, [{"id": "r001", "description": "资质"}], text
    )

    assert len(finalized) == 1
    assert finalized[0]["missing_state"] in (
        rejection.MISSING_STATE_RESOLVED,
        rejection.MISSING_STATE_CONFIRMED,
    )


def test_rejection_excel_reports_missing_state_columns():
    from app.services import check_report_excel as excel

    task = {
        "id": 1,
        "status": "completed",
        "results": {
            "rejection": [
                {
                    "id": "i1",
                    "description": "未找到「安全生产许可证」",
                    "severity": "critical",
                    "validated": None,
                    "evidence_kind": "structural",
                    "missing_state": "resolved",
                    "missing_state_reason": "全文其它位置存在该材料的结构性线索",
                },
            ],
            "spelling": [],
            "logic": [],
        },
    }
    sheets = excel.build_rejection_workbook(task)
    detail = sheets[1]
    assert "缺失复核" in detail.headers
    assert "缺失复核说明" in detail.headers
    assert detail.rows[0][10] == "已排除（该材料在其它分块中存在线索）"
    assert any("疑似缺失跨块复核" in note for note in sheets[0].notes)
    assert excel.missing_state_text({"missing_state": "confirmed"}).startswith("缺失成立")


# ==================== E. 错别字一键修正 ====================


def _spelling_task(
    *, doc_text: str, item: dict, status: str = "completed", project_id: int | None = None
) -> int:
    from app.services import rejection

    task_id = rejection.create_task(
        project_id=project_id,
        tender_text="招标文件",
        bid_documents=[{"id": 1, "name": "技术标.docx", "text": doc_text}],
        check_items=[{"id": "r001", "description": "错别字"}],
        enabled_dimensions=["spelling"],
    )
    rejection.update_task_status(
        task_id,
        status,
        results={
            "rejection": [],
            "spelling": [{"bid_doc_id": 1, "bid_doc_name": "技术标.docx", **item}],
            "logic": [],
        },
    )
    return task_id


def test_apply_spelling_fixes_replaces_by_verified_position():
    from app.services import rejection

    doc_text = "甲" * 50 + "实施过成" + "乙" * 50
    task_id = _spelling_task(
        doc_text=doc_text,
        item={
            "id": "sp-001",
            "original": "实施过成",
            "corrected": "实施过程",
            "verified": True,
            "position": 50,
        },
    )

    result = rejection.apply_spelling_fixes(task_id, ["sp-001"])

    assert result["applied_count"] == 1
    assert result["skipped_count"] == 0
    task = rejection.get_task(task_id)
    assert task["bid_documents"][0]["text"] == "甲" * 50 + "实施过程" + "乙" * 50
    assert task["results"]["spelling"][0]["applied"] is True
    assert task["results"]["spelling_fixes"][0]["before"] == "实施过成"


def test_apply_spelling_fixes_relocates_when_position_is_batch_relative():
    """position 来自批次文本时下标无意义，必须重新定位而非盲改。"""
    from app.services import rejection

    doc_text = "甲" * 50 + "实施过成" + "乙" * 50
    task_id = _spelling_task(
        doc_text=doc_text,
        item={
            "id": "sp-001",
            "original": "实施过成",
            "corrected": "实施过程",
            "verified": True,
            "position": 8,
        },
    )

    rejection.apply_spelling_fixes(task_id, ["sp-001"])
    text = rejection.get_task(task_id)["bid_documents"][0]["text"]
    assert text == "甲" * 50 + "实施过程" + "乙" * 50


def test_apply_spelling_fixes_skips_unverified_item():
    from app.services import rejection

    task_id = _spelling_task(
        doc_text="实施过成内容",
        item={
            "id": "sp-001",
            "original": "实施过成",
            "corrected": "实施过程",
            "verified": False,
        },
    )

    result = rejection.apply_spelling_fixes(task_id, ["sp-001"])
    assert result["applied_count"] == 0
    assert "未通过原文核验" in result["skipped"][0]["reason"]
    assert rejection.get_task(task_id)["bid_documents"][0]["text"] == "实施过成内容"


def test_apply_spelling_fixes_skips_when_not_found():
    from app.services import rejection

    task_id = _spelling_task(
        doc_text="完全没有问题的正文",
        item={"id": "sp-001", "original": "错别字串", "corrected": "改正串", "verified": True},
    )

    result = rejection.apply_spelling_fixes(task_id, ["sp-001"])
    assert result["applied_count"] == 0
    assert "未能" in result["skipped"][0]["reason"]


def test_apply_spelling_fixes_skips_identical_text_and_unknown_id():
    from app.services import rejection

    task_id = _spelling_task(
        doc_text="实施过成内容",
        item={"id": "sp-001", "original": "实施过成", "corrected": "实施过成", "verified": True},
    )

    result = rejection.apply_spelling_fixes(task_id, ["sp-001", "not-exist"])
    reasons = [item["reason"] for item in result["skipped"]]
    assert any("相同" in reason for reason in reasons)
    assert any("找不到" in reason for reason in reasons)


def test_apply_spelling_fixes_isolates_batch_failures():
    from app.services import rejection

    doc_text = "甲" * 30 + "实施过成" + "乙" * 30
    task_id = _spelling_task(
        doc_text=doc_text,
        item={
            "id": "sp-001",
            "original": "实施过成",
            "corrected": "实施过程",
            "verified": True,
            "position": 30,
        },
    )
    # 追加一条定位不到的条目，批量时必须互不影响
    task = rejection.get_task(task_id)
    task["results"]["spelling"].append(
        {"id": "sp-002", "original": "不存在的错字", "corrected": "X", "verified": True, "bid_doc_id": 1}
    )
    rejection.update_task_status(task_id, "completed", results=task["results"])

    result = rejection.apply_spelling_fixes(task_id, ["sp-001", "sp-002"])
    assert result["applied_count"] == 1
    assert result["skipped_count"] == 1


def test_apply_spelling_fixes_refuses_running_task():
    from app.services import rejection

    task_id = _spelling_task(
        doc_text="实施过程内容",
        item={"id": "sp-001", "original": "实施过程", "corrected": "实施过程", "verified": True},
        status="running",
    )

    with pytest.raises(ValueError, match="尚未结束"):
        rejection.apply_spelling_fixes(task_id, ["sp-001"])


def test_apply_spelling_fixes_requires_ids_and_existing_task():
    from app.services import rejection

    with pytest.raises(ValueError, match="任务不存在"):
        rejection.apply_spelling_fixes(99999999, ["x"])


def test_apply_spelling_fixes_writeback_project():
    """写回项目正文：偏移必须精确映射到小节，映射不上就跳过。"""
    from app.models import SectionUpdate
    from app.services import rejection
    from app.services.sections import create_section, list_sections, update_section

    project_id = _new_project("错别字写回")
    # SectionCreate 不含 content 字段（正文只能在建节后写入），这里两步构造
    section = create_section(project_id, _section_create("第一章 施工方案"))
    update_section(section.id, SectionUpdate(content="甲" * 20 + "实施过成"))

    # 任务文本按 import_from_project 的拼装方式构造
    sections = list_sections(project_id)
    prefix = f"## {sections[0].title}\n\n"
    doc_text = prefix + sections[0].content

    task_id = _spelling_task(
        doc_text=doc_text,
        project_id=project_id,
        item={
            "id": "sp-001",
            "original": "实施过成",
            "corrected": "实施过程",
            "verified": True,
            "position": len(prefix) + 20,
        },
    )

    result = rejection.apply_spelling_fixes(task_id, ["sp-001"], writeback_project=True)

    assert result["applied_count"] == 1
    assert result["applied"][0]["section_id"] == sections[0].id
    refreshed = list_sections(project_id)[0]
    assert refreshed.content == "甲" * 20 + "实施过程"


def _section_create(title: str):
    from app.models import SectionCreate

    return SectionCreate(title=title, level=1, sort_order=1)


def test_spelling_positions_reanchored_to_whole_document(monkeypatch):
    """批次内偏移必须重新锚定到整文，否则报告行号与一键修正锚点都是错的。"""
    from app.services import rejection

    async def _fake_collect(_provider, _messages, **_kwargs):
        return [{"id": "sp-1", "original": "实施过成", "corrected": "实施过程", "location": "第1段"}]

    monkeypatch.setattr(rejection, "collect_json_response", _fake_collect)
    monkeypatch.setattr(rejection, "get_prompt", lambda _key: "系统提示")

    text = "甲" * 50 + "实施过成" + "乙" * 50
    errors = asyncio.run(rejection.check_spelling_dimension(object(), {"id": 1, "name": "A.docx", "text": text}))

    assert len(errors) == 1
    assert errors[0]["position"] == 50, "position 必须是整文偏移而不是批次偏移"
    assert errors[0]["position_scope"] == "document"
    assert errors[0]["line_hint"].startswith("第 ")


def test_spelling_endpoint_route_exists():
    import inspect

    from app.routers import rejection as rejection_router

    source = inspect.getsource(rejection_router)
    assert "apply_spelling_fixes" in source
    assert "'/apply-spelling'" in source
