"""回归测试：引入项 C（2026-10-16 全局事实全流程引入 —— 生成收尾审计冲突结构化）。

对齐易标 `buildConsistencyAuditMessages`（conflicts 含
section_id/fact_title/evidence/reason/severity）：

  C. CONSISTENCY_AUDIT_EVIDENCE_ENABLED（默认关）
     - 默认关：审计提示词输出结构与发现字段逐字保持现状（仅 section_id/issue/suggestion）；
     - 开启：审计提示词要求 evidence/fact_title/severity；
     - evidence 作为补丁 old_text 原文锚点进入 conflicts_text（配合引入项 B 补丁模式）；
     - 发现落库尊重 AI severity（合法 high/medium/low），非法/缺失回落默认；
       evidence 非空并入描述，为空时描述逐字不变。

设计原则：只测可观察行为（提示词文本、DB 落库结果），不依赖真实 AI/网络。
"""

from __future__ import annotations

import asyncio
import json

from app.db import get_db, init_db


def _new_project(name: str) -> int:
    init_db()
    with get_db() as conn:
        conn.execute("INSERT INTO projects (name, industry) VALUES (?, ?)", (name, "通用"))
        return conn.execute("SELECT id FROM projects ORDER BY id DESC LIMIT 1").fetchone()["id"]


class _PromptCapturingProvider:
    """审计/补丁双形态假 provider，并记录送审的审计 user 提示词。"""

    def __init__(
        self,
        conflicts: list[dict],
        *,
        patch_responses: list[str] | None = None,
        repair_text: str | None = None,
    ) -> None:
        self.conflicts = conflicts
        self.patch_responses = list(patch_responses or [])
        self.repair_text = repair_text
        self.audit_prompts: list[str] = []
        self.patch_prompts: list[str] = []
        self.rewrite_calls = 0

    async def chat(self, messages, temperature=0.0, cancel_event=None, **kwargs):
        system = str(getattr(messages[0], "content", ""))
        user = str(getattr(messages[1], "content", ""))
        if "擅长发现正文中的矛盾" in system:
            self.audit_prompts.append(user)
            return json.dumps(
                {"has_conflicts": bool(self.conflicts), "conflicts": self.conflicts},
                ensure_ascii=False,
            )
        if "待修复小节正文（带行号" in user:
            self.patch_prompts.append(user)
            if not self.patch_responses:
                return json.dumps({"patches": []}, ensure_ascii=False)
            return self.patch_responses.pop(0)
        self.rewrite_calls += 1
        return self.repair_text or ""


def _patch_json(section_id: str, old_text: str, new_text: str) -> str:
    return json.dumps(
        {
            "patches": [
                {
                    "section_id": section_id,
                    "old_text": old_text,
                    "new_text": new_text,
                    "reason": "统一为全局事实口径",
                }
            ],
            "unresolved": [],
        },
        ensure_ascii=False,
    )


# ==================== 开关默认值与解析 ====================


def test_audit_evidence_switch_defaults_off(monkeypatch):
    from app.services import facts_service

    monkeypatch.delenv("CONSISTENCY_AUDIT_EVIDENCE_ENABLED", raising=False)
    assert facts_service.consistency_audit_evidence_enabled() is False
    monkeypatch.setenv("CONSISTENCY_AUDIT_EVIDENCE_ENABLED", "1")
    assert facts_service.consistency_audit_evidence_enabled() is True
    monkeypatch.setenv("CONSISTENCY_AUDIT_EVIDENCE_ENABLED", "0")
    assert facts_service.consistency_audit_evidence_enabled() is False


# ==================== 默认关：审计提示词逐字保持现状 ====================


def test_default_off_audit_prompt_unchanged(monkeypatch):
    from app.services.ai import workflows_expand as wf

    monkeypatch.delenv("CONSISTENCY_AUDIT_EVIDENCE_ENABLED", raising=False)
    monkeypatch.delenv("CONSISTENCY_PATCH_REPAIR_ENABLED", raising=False)
    monkeypatch.setattr(wf, "AUDIT_GROUP_CHAR_LIMIT", 2000)
    monkeypatch.setattr(wf, "AUDIT_SECTION_CHAR_LIMIT", 2000)

    results = [{"id": 1, "status": "completed", "content": "项目总工期为450天。" + "甲" * 120}]
    provider = _PromptCapturingProvider(
        [{"section_id": "1", "issue": "工期不一致", "suggestion": "统一为 420 天"}],
        repair_text="项目总工期为420天。" + "甲" * 120,
    )
    pid = _new_project("审计证据-默认关")
    asyncio.run(
        wf.consistency_audit_content(pid, provider, results, {1: {"title": "第一章"}}, "事实", "", None)
    )

    prompt = provider.audit_prompts[0]
    # 默认关：输出结构仍只含 section_id/issue/suggestion，不含 evidence/fact_title/severity
    assert '"section_id": "章节ID", "issue": "问题描述", "suggestion": "修复建议"' in prompt
    assert "evidence" not in prompt
    assert "fact_title" not in prompt
    assert "冲突定位要求" not in prompt


# ==================== 开启：审计提示词要求结构化冲突 ====================


def test_enabled_audit_prompt_requires_structured_conflict(monkeypatch):
    from app.services.ai import workflows_expand as wf

    monkeypatch.setenv("CONSISTENCY_AUDIT_EVIDENCE_ENABLED", "1")
    monkeypatch.delenv("CONSISTENCY_PATCH_REPAIR_ENABLED", raising=False)
    monkeypatch.setattr(wf, "AUDIT_GROUP_CHAR_LIMIT", 2000)
    monkeypatch.setattr(wf, "AUDIT_SECTION_CHAR_LIMIT", 2000)

    results = [{"id": 1, "status": "completed", "content": "项目总工期为450天。" + "甲" * 120}]
    provider = _PromptCapturingProvider(
        [{"section_id": "1", "issue": "工期不一致", "suggestion": "统一为 420 天"}],
        repair_text="项目总工期为420天。" + "甲" * 120,
    )
    pid = _new_project("审计证据-开启提示词")
    asyncio.run(
        wf.consistency_audit_content(pid, provider, results, {1: {"title": "第一章"}}, "事实", "", None)
    )

    prompt = provider.audit_prompts[0]
    assert "evidence" in prompt
    assert "fact_title" in prompt
    assert "severity" in prompt
    assert "冲突定位要求" in prompt
    assert "逐字原文摘录" in prompt


# ==================== evidence 作为补丁 old_text 原文锚点 ====================


def test_evidence_threaded_into_patch_conflicts_text(monkeypatch):
    from app.services.ai import workflows_expand as wf

    monkeypatch.setenv("CONSISTENCY_AUDIT_EVIDENCE_ENABLED", "1")
    monkeypatch.setenv("CONSISTENCY_PATCH_REPAIR_ENABLED", "1")
    monkeypatch.delenv("CONSISTENCY_REPAIR_MAX_ATTEMPTS", raising=False)
    monkeypatch.setattr(wf, "AUDIT_GROUP_CHAR_LIMIT", 2000)
    monkeypatch.setattr(wf, "AUDIT_SECTION_CHAR_LIMIT", 2000)

    original = (
        "第一节文字保持不变。\n"
        "项目总工期为450天，满足招标人要求。\n"
        + "乙" * 200
    )
    results = [{"id": 1, "status": "completed", "content": original}]
    provider = _PromptCapturingProvider(
        [
            {
                "section_id": "1",
                "fact_title": "项目总工期",
                "evidence": "项目总工期为450天",
                "issue": "工期与全局事实不一致",
                "suggestion": "统一为 420 天",
                "severity": "high",
            }
        ],
        patch_responses=[_patch_json("1", "项目总工期为450天", "项目总工期为420天")],
    )
    pid = _new_project("审计证据-补丁锚点")
    asyncio.run(
        wf.consistency_audit_content(pid, provider, results, {1: {"title": "第一章"}}, "事实", "", None)
    )

    assert provider.patch_prompts, "补丁模式必须经 generate_patches 请求补丁"
    patch_prompt = provider.patch_prompts[0]
    # evidence 原文锚点应随 conflicts_text 进入补丁修复请求
    assert "冲突原文锚点" in patch_prompt
    assert "项目总工期为450天" in patch_prompt
    assert "项目总工期为420天" in results[0]["content"]


def test_no_evidence_anchor_when_absent(monkeypatch):
    """模型未返回 evidence 时，补丁请求不应出现空锚点块（向后安全）。"""
    from app.services.ai import workflows_expand as wf

    monkeypatch.setenv("CONSISTENCY_AUDIT_EVIDENCE_ENABLED", "1")
    monkeypatch.setenv("CONSISTENCY_PATCH_REPAIR_ENABLED", "1")
    monkeypatch.delenv("CONSISTENCY_REPAIR_MAX_ATTEMPTS", raising=False)
    monkeypatch.setattr(wf, "AUDIT_GROUP_CHAR_LIMIT", 2000)
    monkeypatch.setattr(wf, "AUDIT_SECTION_CHAR_LIMIT", 2000)

    results = [{"id": 1, "status": "completed", "content": "工期为450天。" + "丙" * 200}]
    provider = _PromptCapturingProvider(
        [{"section_id": "1", "issue": "工期不一致", "suggestion": "统一为 420 天"}],
        patch_responses=[_patch_json("1", "工期为450天", "工期为420天")],
    )
    pid = _new_project("审计证据-无锚点")
    asyncio.run(
        wf.consistency_audit_content(pid, provider, results, {1: {"title": "第一章"}}, "事实", "", None)
    )

    assert provider.patch_prompts
    assert "冲突原文锚点" not in provider.patch_prompts[0]


# ==================== 落库：尊重 AI severity + evidence 并入描述 ====================


def _conflict_row(conflict_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM consistency_conflicts WHERE conflict_id = ?", (conflict_id,)
        ).fetchone()
    return dict(row) if row else None


def _row_for_project(pid: int) -> dict | None:
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM consistency_conflicts WHERE project_id = ? AND conflict_id LIKE 'AI-%' "
            "ORDER BY id DESC LIMIT 1",
            (pid,),
        ).fetchone()
    return dict(row) if row else None


def test_persist_honors_ai_severity_and_evidence():
    from app.services import consistency_rules as rules
    from app.services.ai import workflows_expand as wf

    assert rules.SEVERITY_DEFAULT != "high"
    pid = _new_project("审计证据-落库severity")
    findings = [
        {
            "section_id": "1",
            "issue": "工期与事实不一致",
            "suggestion": "统一为 420 天",
            "evidence": "项目总工期为450天",
            "fact_title": "项目总工期",
            "severity": "high",
            "repaired": False,
        }
    ]
    wf._persist_ai_audit_findings(pid, 1, findings)
    row = _row_for_project(pid)
    assert row is not None
    assert row["severity"] == "high"
    assert "项目总工期为450天" in row["description"]
    # 引入项 C 加固：fact_title 随描述落库，前端可直接定位被违反的全局事实
    assert "【相背事实：项目总工期】" in row["description"]


def test_persist_invalid_severity_falls_back_and_no_evidence_keeps_description():
    from app.services.ai import workflows_expand as wf

    pid = _new_project("审计证据-落库回退")
    findings = [
        {
            "section_id": "1",
            "issue": "工期不一致",
            "suggestion": "统一为 420 天",
            "severity": "urgent",  # 非法值
            "repaired": False,
        }
    ]
    wf._persist_ai_audit_findings(pid, 1, findings)
    row = _row_for_project(pid)
    assert row is not None
    assert row["severity"] == "medium"  # 回落默认
    # 无 evidence/fact_title：描述逐字保持既有口径
    assert row["description"] == "工期不一致（修复建议：统一为 420 天）"


# ==================== 加固：evidence 锚点/描述长度封顶 ====================


def test_evidence_anchor_truncated_in_patch_prompt(monkeypatch):
    """超长 evidence 进入补丁 conflicts_text 时被截断，不撑爆补丁请求。"""
    from app.services.ai import workflows_expand as wf

    monkeypatch.setenv("CONSISTENCY_AUDIT_EVIDENCE_ENABLED", "1")
    monkeypatch.setenv("CONSISTENCY_PATCH_REPAIR_ENABLED", "1")
    monkeypatch.delenv("CONSISTENCY_REPAIR_MAX_ATTEMPTS", raising=False)
    monkeypatch.setattr(wf, "AUDIT_GROUP_CHAR_LIMIT", 2000)
    monkeypatch.setattr(wf, "AUDIT_SECTION_CHAR_LIMIT", 40000)

    long_evidence = "工" * (wf._AUDIT_EVIDENCE_ANCHOR_MAX + 500)
    results = [{"id": 1, "status": "completed", "content": "工期为450天。" + "丙" * 200}]
    provider = _PromptCapturingProvider(
        [
            {
                "section_id": "1",
                "evidence": long_evidence,
                "issue": "工期不一致",
                "suggestion": "统一为 420 天",
            }
        ],
        patch_responses=[_patch_json("1", "工期为450天", "工期为420天")],
    )
    pid = _new_project("审计证据-锚点截断")
    asyncio.run(
        wf.consistency_audit_content(pid, provider, results, {1: {"title": "第一章"}}, "事实", "", None)
    )

    assert provider.patch_prompts
    anchor_block = provider.patch_prompts[0]
    assert "冲突原文锚点" in anchor_block
    # 锚点中的 evidence 被封顶到 _AUDIT_EVIDENCE_ANCHOR_MAX，未出现全长尾段
    assert long_evidence not in anchor_block
    assert "工" * wf._AUDIT_EVIDENCE_ANCHOR_MAX in anchor_block


def test_persist_evidence_truncated_in_description():
    """超长 evidence 落库描述同样被封顶，不污染 DB 字段。"""
    from app.services.ai import workflows_expand as wf

    pid = _new_project("审计证据-描述截断")
    long_evidence = "设" * (wf._AUDIT_EVIDENCE_ANCHOR_MAX + 300)
    findings = [
        {
            "section_id": "1",
            "issue": "型号不一致",
            "suggestion": "统一型号",
            "evidence": long_evidence,
            "repaired": False,
        }
    ]
    wf._persist_ai_audit_findings(pid, 1, findings)
    row = _row_for_project(pid)
    assert row is not None
    assert long_evidence not in row["description"]
    assert "设" * wf._AUDIT_EVIDENCE_ANCHOR_MAX in row["description"]
