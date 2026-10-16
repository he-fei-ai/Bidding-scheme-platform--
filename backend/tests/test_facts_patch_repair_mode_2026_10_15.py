"""回归测试：引入项 A/B（2026-10-15 全局事实全流程引入报告配套）。

覆盖范围：
  A. CONTENT_SECTION_SYSTEM【事实铁律】对齐易标规则 15/16
     （提供必用对 + 仅用本章提供 + 缺值禁编人员/周期/质保/品牌/型号承诺）；
  B. 生成收尾 AI 审计修复的精确补丁模式
     （CONSISTENCY_PATCH_REPAIR_ENABLED，默认关=整节重写路径逐字不变；
      开启后 old_text/new_text 唯一命中替换、失败原因回喂重试、绝不重写整节）。

设计原则：只测行为不测实现；不依赖真实 AI 与外部网络（用可编程假 provider）。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.db import get_db, init_db


def _new_project(name: str) -> int:
    init_db()
    with get_db() as conn:
        conn.execute("INSERT INTO projects (name, industry) VALUES (?, ?)", (name, "通用"))
        return conn.execute("SELECT id FROM projects ORDER BY id DESC LIMIT 1").fetchone()["id"]


class _AuditProvider:
    """审计 + 修复双形态假 provider。

    - 审计调用（system 含「擅长发现正文中的矛盾」）→ 返回预设冲突；
    - 整节重写修复（user 含「请直接输出修复后的完整正文」）→ 返回 repair_text；
    - 精确补丁修复（user 含「待修复小节正文（带行号」）→ 按脚本依次返回补丁 JSON，
      并把每轮 prompt 记录到 patch_prompts 供断言失败回喂。
    """

    def __init__(
        self,
        conflicts: list[dict],
        *,
        repair_text: str | None = None,
        patch_responses: list[str] | None = None,
    ) -> None:
        self.conflicts = conflicts
        self.repair_text = repair_text
        self.patch_responses = list(patch_responses or [])
        self.audit_calls = 0
        self.rewrite_calls = 0
        self.patch_prompts: list[str] = []

    async def chat(self, messages, temperature=0.0, cancel_event=None, **kwargs):
        system = str(getattr(messages[0], "content", ""))
        user = str(getattr(messages[1], "content", ""))
        if "擅长发现正文中的矛盾" in system:
            self.audit_calls += 1
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


# ==================== A. 正文事实铁律提示词（引入项 A） ====================


def test_content_system_fact_iron_rule_aligned_with_reference_r15_r16():
    """铁律必须同时含「提供必用对」（易标 R15）与「缺值禁编承诺」（易标 R16）两半。"""
    from app.services.ai.prompts.content import CONTENT_SECTION_SYSTEM

    rule = next(
        line for line in CONTENT_SECTION_SYSTEM.splitlines() if line.startswith("【事实铁律】")
    )
    # R15 半：提供的事实必须优先使用、不得前后矛盾
    assert "必须优先使用变量值" in rule
    assert "不得前后矛盾" in rule
    # R16 半：只用本章提供的事实；未提供时不编造影响全文一致性的承诺
    assert "仅使用本章节提供的事实变量" in rule
    assert "不要主动编造具体人员、周期、质保、品牌、型号" in rule
    # 历史约束保留：有事实时一字不差、无数字时定性描述
    assert "一字不差" in rule
    assert "改用定性描述" in rule


# ==================== B. 精确补丁修复模式（引入项 B） ====================


def test_patch_repair_switch_defaults_off(monkeypatch):
    from app.services import facts_service

    monkeypatch.delenv("CONSISTENCY_PATCH_REPAIR_ENABLED", raising=False)
    assert facts_service.consistency_patch_repair_enabled() is False
    monkeypatch.setenv("CONSISTENCY_PATCH_REPAIR_ENABLED", "1")
    assert facts_service.consistency_patch_repair_enabled() is True


def test_default_off_keeps_rewrite_path_untouched(monkeypatch):
    """开关默认关：走整节重写 + 80% 长度保护的既有路径，行为逐字不变。"""
    from app.services.ai import workflows_expand as wf

    monkeypatch.delenv("CONSISTENCY_PATCH_REPAIR_ENABLED", raising=False)
    monkeypatch.setattr(wf, "AUDIT_GROUP_CHAR_LIMIT", 2000)
    monkeypatch.setattr(wf, "AUDIT_SECTION_CHAR_LIMIT", 2000)

    original = "项目总工期为450天。" + "甲" * 120
    results = [{"id": 1, "status": "completed", "content": original}]
    provider = _AuditProvider(
        [{"section_id": "1", "issue": "工期不一致", "suggestion": "统一为 420 天"}],
        repair_text="项目总工期为420天。" + "甲" * 120,
    )
    pid = _new_project("补丁模式-默认关")

    asyncio.run(wf.consistency_audit_content(pid, provider, results, {1: {"title": "第一章"}}, "事实", "", None))

    assert provider.rewrite_calls == 1
    assert provider.patch_prompts == []
    assert results[0]["content"].startswith("项目总工期为420天。")


def test_patch_mode_applies_exact_patch_only(monkeypatch):
    """开关开启：只做 old_text/new_text 唯一命中替换，正文其余部分逐字不动。"""
    from app.services.ai import workflows_expand as wf

    monkeypatch.setenv("CONSISTENCY_PATCH_REPAIR_ENABLED", "1")
    monkeypatch.delenv("CONSISTENCY_REPAIR_MAX_ATTEMPTS", raising=False)
    monkeypatch.setattr(wf, "AUDIT_GROUP_CHAR_LIMIT", 2000)
    monkeypatch.setattr(wf, "AUDIT_SECTION_CHAR_LIMIT", 2000)

    original = (
        "第一节文字保持不变。\n"
        "项目总工期为450天，满足招标人要求。\n"
        "尾节文字同样保持不变，且长度足以让整节重写模式明显短于原文而被长度保护拒绝。"
        + "乙" * 200
    )
    results = [{"id": 1, "status": "completed", "content": original}]
    provider = _AuditProvider(
        [{"section_id": "1", "issue": "工期与事实不一致", "suggestion": "统一为 420 天"}],
        patch_responses=[_patch_json("1", "项目总工期为450天", "项目总工期为420天")],
        repair_text="整节重写不应发生",
    )
    pid = _new_project("补丁模式-唯一命中")

    asyncio.run(wf.consistency_audit_content(pid, provider, results, {1: {"title": "第一章"}}, "事实", "", None))

    assert provider.patch_prompts, "补丁模式必须经 generate_patches 请求补丁"
    assert provider.rewrite_calls == 0, "补丁模式禁止整节重写"
    assert "项目总工期为420天" in results[0]["content"]
    # 除补丁点位外，正文其余部分必须逐字保留（对比旧重写路径：非命中处全丢）
    assert "第一节文字保持不变。" in results[0]["content"]
    assert results[0]["content"].endswith("乙" * 200)

    # 发现已修复并落库（AI- 前缀冲突记 resolved=1）
    with get_db() as conn:
        rows = conn.execute(
            "SELECT resolved FROM consistency_conflicts WHERE project_id = ? AND conflict_id LIKE 'AI-%'",
            (pid,),
        ).fetchall()
    assert rows and rows[0]["resolved"] == 1


def test_patch_mode_rejects_hallucinated_old_text(monkeypatch):
    """old_text 在正文中不存在 → 唯一命中校验拒绝，正文一字不改（幻觉无法污染）。"""
    from app.services.ai import workflows_expand as wf

    monkeypatch.setenv("CONSISTENCY_PATCH_REPAIR_ENABLED", "1")
    monkeypatch.delenv("CONSISTENCY_REPAIR_MAX_ATTEMPTS", raising=False)
    monkeypatch.setattr(wf, "AUDIT_GROUP_CHAR_LIMIT", 2000)
    monkeypatch.setattr(wf, "AUDIT_SECTION_CHAR_LIMIT", 2000)

    original = "项目总工期为450天。" + "甲" * 120
    results = [{"id": 1, "status": "completed", "content": original}]
    provider = _AuditProvider(
        [{"section_id": "1", "issue": "x", "suggestion": "y"}],
        patch_responses=[_patch_json("1", "根本不存在的原文片段QWERTY", "项目总工期为420天")],
    )
    pid = _new_project("补丁模式-幻觉拒绝")

    asyncio.run(wf.consistency_audit_content(pid, provider, results, {1: {"title": "第一章"}}, "事实", "", None))

    assert results[0]["content"] == original, "未命中补丁绝不能改动正文"


def test_patch_mode_failure_feedback_retry(monkeypatch):
    """第一轮补丁被拒 → 失败原因回喂重新生成，第二轮命中后落文（对齐易标重试语义）。"""
    from app.services.ai import workflows_expand as wf

    monkeypatch.setenv("CONSISTENCY_PATCH_REPAIR_ENABLED", "1")
    monkeypatch.setenv("CONSISTENCY_REPAIR_MAX_ATTEMPTS", "2")
    monkeypatch.setattr(wf, "AUDIT_GROUP_CHAR_LIMIT", 2000)
    monkeypatch.setattr(wf, "AUDIT_SECTION_CHAR_LIMIT", 2000)

    original = "项目总工期为450天。" + "甲" * 120
    results = [{"id": 1, "status": "completed", "content": original}]
    provider = _AuditProvider(
        [{"section_id": "1", "issue": "工期不一致", "suggestion": "统一为 420 天"}],
        patch_responses=[
            _patch_json("1", "根本不存在的原文片段QWERTY", "项目总工期为420天"),
            _patch_json("1", "项目总工期为450天", "项目总工期为420天"),
        ],
    )
    pid = _new_project("补丁模式-失败回喂")

    asyncio.run(wf.consistency_audit_content(pid, provider, results, {1: {"title": "第一章"}}, "事实", "", None))

    assert len(provider.patch_prompts) == 2, "第一轮被拒后必须回喂重试一轮"
    assert "上一轮修复补丁未能应用" in provider.patch_prompts[1]
    assert "项目总工期为420天" in results[0]["content"]


def test_patch_mode_drops_out_of_whitelist_section_id(monkeypatch):
    """补丁 section_id 越界（AI 编造章节号）→ 白名单过滤丢弃，正文不动。"""
    from app.services.ai import workflows_expand as wf

    monkeypatch.setenv("CONSISTENCY_PATCH_REPAIR_ENABLED", "1")
    monkeypatch.delenv("CONSISTENCY_REPAIR_MAX_ATTEMPTS", raising=False)
    monkeypatch.setattr(wf, "AUDIT_GROUP_CHAR_LIMIT", 2000)
    monkeypatch.setattr(wf, "AUDIT_SECTION_CHAR_LIMIT", 2000)

    original = "项目总工期为450天。" + "甲" * 120
    results = [{"id": 1, "status": "completed", "content": original}]
    provider = _AuditProvider(
        [{"section_id": "1", "issue": "x", "suggestion": "y"}],
        patch_responses=[_patch_json("999", "项目总工期为450天", "项目总工期为420天")],
    )
    pid = _new_project("补丁模式-越界丢弃")

    asyncio.run(wf.consistency_audit_content(pid, provider, results, {1: {"title": "第一章"}}, "事实", "", None))

    assert results[0]["content"] == original


@pytest.mark.parametrize(
    "env_value,expected",
    [("0", False), ("", False), ("1", True), ("true", True), ("on", True), ("YES", True)],
)
def test_patch_repair_switch_env_parsing(monkeypatch, env_value, expected):
    from app.services import facts_service

    monkeypatch.setenv("CONSISTENCY_PATCH_REPAIR_ENABLED", env_value)
    assert facts_service.consistency_patch_repair_enabled() is expected
