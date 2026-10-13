"""2026-10-13 G1/G2 修复回归：AI 一致性审计的落库与生成中防护门。

覆盖缺陷（均为本轮实测确认）：

  | G1 | `consistency_audit_content`（正文生成勾选「全文一致性审计」跑的
        AI 路径）只就地改 `results` 里的 content，**发现从不落库** ——
        `consistency_conflicts` / `consistency_audit_log` 永远只有确定性审计
        （`ConsistencyAuditor`）的写入，前端「一致性检查」页看不到 AI 路径
        查了什么、修了什么                                        |
  | G2 | 同一条链路没有任何「生成中」防护。若目录生成正在全表重建小节，
        审计修复写进去的内容会被静默丢弃；缺的正是 `task_guard.
        reject_if_generation_running` 接入               |

场景约束（防自锁，是本测试钉死的核心契约）：

  AI 审计运行在**正文生成任务收尾段**，锁正是该任务自己持有的 ——
  所以必须用 `allow_content_edit=True`（内容类写入口径），默认
  `allow_content_edit=False` 会把自己 409 挡死；而目录生成持锁时
  （同组互斥，全表重建会丢写入）仍要拦住。被拦时是「跳过审计并原样
  返回」而不是抛 409：SSE 调用方只 `except Exception` 记日志，异常会
  把整个审计静默吞掉。

设计原则：不依赖真实 AI（可编程假 provider），只断言可观察行为
（DB 行 / 内容是否被改写）。
"""

from __future__ import annotations

import asyncio
import json
import re

from app.db import get_db
from app.services import consistency_rules as rules
from app.services.ai import workflows_expand as wf
from app.services.task_lock import acquire, release


# ==================== 公共工具 ====================


def _new_project(name: str) -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO projects (name, industry) VALUES (?, '通用')", (name,)
        )
        return cur.lastrowid


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


class _FakeProvider:
    """按「第 N/M 组」查表返回冲突的审计假 provider。"""

    def __init__(
        self,
        conflicts: dict[int, list[dict]] | None = None,
        *,
        repair_text: str | None = None,
    ) -> None:
        self.conflicts = conflicts or {}
        self.repair_text = repair_text
        self.audit_prompts: list[str] = []

    async def chat(self, messages, temperature=0.0, cancel_event=None, **kwargs):
        system = str(getattr(messages[0], "content", ""))
        user = str(getattr(messages[1], "content", ""))
        if "擅长发现正文中的矛盾" in system:
            self.audit_prompts.append(user)
            index = _group_index_from_prompt(user)
            items = self.conflicts.get(index, [])
            return json.dumps(
                {"has_conflicts": bool(items), "conflicts": items}, ensure_ascii=False
            )
        if self.repair_text is None:
            return _extract_current_body(user)
        return self.repair_text


def _seed_deterministic_conflict(project_id: int) -> str:
    """插一条确定性审计口径的冲突行（`conflict_` 前缀）。"""
    conflict_id = f"conflict_{project_id}_总工期_x"
    with get_db() as conn:
        conn.execute(
            """INSERT INTO consistency_conflicts
               (project_id, conflict_id, fact_name, values_json,
                sources_json, severity, description, resolved)
               VALUES (?, ?, '总工期', '[]', '["1"]', 'medium', '工期不一致', 0)""",
            (project_id, conflict_id),
        )
    return conflict_id


def _ai_conflicts(project_id: int) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM consistency_conflicts "
            "WHERE project_id = ? AND conflict_id LIKE 'AI-%'",
            (project_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def _audit_log_rows(project_id: int) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM consistency_audit_log WHERE project_id = ?",
            (project_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def _run_audit(pid: int, provider, results: list[dict], sections: dict) -> list[dict]:
    return asyncio.run(
        wf.consistency_audit_content(pid, provider, results, sections, "事实", "")
    )


# ==================== G1：AI 审计发现落库 ====================


class TestAiAuditFindingsPersisted:
    def test_findings_written_to_conflicts_table(self):
        """AI 审计的发现必须逐条落 `consistency_conflicts`（AI- 前缀）。"""
        pid = _new_project("G1-落库-conflicts")
        results = [
            {"id": 1, "status": "completed", "content": "工期 300 天"},
            {"id": 2, "status": "completed", "content": "工期 450 天"},
        ]
        sections = {1: {"title": "第一章"}, 2: {"title": "第二章"}}
        provider = _FakeProvider(
            {
                1: [
                    {
                        "section_id": "1",
                        "issue": "工期与事实变量不一致",
                        "suggestion": "统一为 450 天",
                    }
                ]
            },
            repair_text="工期 450 天",
        )

        _run_audit(pid, provider, results, sections)

        rows = _ai_conflicts(pid)
        assert len(rows) == 1, "AI 审计发现必须逐条落库"
        row = rows[0]
        assert row["conflict_id"].startswith("AI-")
        assert row["project_id"] == pid
        assert row["fact_name"] == "工期与事实变量不一致"
        assert row["description"] == "工期与事实变量不一致（修复建议：统一为 450 天）"
        assert json.loads(row["values_json"]) == []
        assert json.loads(row["sources_json"]) == ["1"]
        assert row["severity"] == rules.SEVERITY_DEFAULT == "medium"

    def test_repaired_finding_marked_resolved(self):
        """修复成功（正文已被 AI 改写落库）→ resolved=1 + resolved_value。

        与 apply_autofix「真实落文后才标 resolved」的语义一致：不允许
        在待处理列表里留已修复的假未解决项。
        """
        pid = _new_project("G1-已修复标 resolved")
        results = [{"id": 1, "status": "completed", "content": "甲" * 200}]
        provider = _FakeProvider(
            {1: [{"section_id": "1", "issue": "x", "suggestion": "y"}]},
            repair_text="乙" * 200,
        )

        _run_audit(pid, provider, results, {1: {"title": "第一章"}})

        rows = _ai_conflicts(pid)
        assert len(rows) == 1
        assert rows[0]["resolved"] == 1
        assert rows[0]["resolved_value"] == "AI 自动修复"
        assert rows[0]["resolved_at"]

    def test_unrepaired_finding_left_unresolved(self):
        """无修复建议 / 修复被拒 → resolved=0，留待用户跟进。"""
        pid = _new_project("G1-未修复留 unresolved")
        results = [{"id": 1, "status": "completed", "content": "甲" * 200}]
        sections = {1: {"title": "第一章"}}

        # 有建议但 AI 修复返回过短（疑似截断）→ 拒绝应用
        provider = _FakeProvider(
            {1: [{"section_id": "1", "issue": "工期不一致", "suggestion": "统一为 450 天"}]},
            repair_text="乙" * 10,
        )
        _run_audit(pid, provider, results, sections)
        rows = _ai_conflicts(pid)
        assert len(rows) == 1
        assert rows[0]["resolved"] == 0
        assert rows[0]["resolved_value"] is None
        assert rows[0]["resolved_at"] is None

        # 无修复建议（只报告不修复）
        pid2 = _new_project("G1-无建议留 unresolved")
        provider2 = _FakeProvider(
            {1: [{"section_id": "1", "issue": "疑似矛盾", "suggestion": ""}]}
        )
        _run_audit(pid2, provider2, results, sections)
        rows2 = _ai_conflicts(pid2)
        assert len(rows2) == 1
        assert rows2[0]["resolved"] == 0
        assert "疑似矛盾" in rows2[0]["fact_name"]

    def test_audit_log_row_appended(self):
        """本轮审计摘要必须追加进 `consistency_audit_log`。"""
        pid = _new_project("G1-audit_log")
        results = [
            {"id": 1, "status": "completed", "content": "甲" * 200},
            {"id": 2, "status": "completed", "content": "甲" * 200},
        ]
        sections = {1: {"title": "第一章"}, 2: {"title": "第二章"}}
        provider = _FakeProvider(
            {1: [{"section_id": "1", "issue": "x", "suggestion": "y"}]},
            repair_text="乙" * 200,
        )

        _run_audit(pid, provider, results, sections)

        rows = _audit_log_rows(pid)
        assert len(rows) == 1, "每轮审计追加一行摘要"
        row = rows[0]
        assert row["total_sections"] == 2
        assert row["facts_extracted"] == 0, "AI 路径不做结构化事实抽取，记 0 不编数"
        assert row["conflicts_found"] == 1
        assert row["status"] == rules.AUDIT_STATUS_COMPLETED
        logged = json.loads(row["conflicts_json"])
        assert len(logged) == 1 and logged[0]["conflict_id"].startswith("AI-")

        # 无冲突轮 → 状态走 no_conflicts
        pid2 = _new_project("G1-audit_log-无冲突")
        _run_audit(pid2, _FakeProvider({}), results, sections)
        rows2 = _audit_log_rows(pid2)
        assert len(rows2) == 1
        assert rows2[0]["conflicts_found"] == 0
        assert rows2[0]["status"] == rules.AUDIT_STATUS_NO_CONFLICTS

    def test_second_run_only_replaces_ai_rows(self):
        """重跑审计：只清上轮 AI 发现，绝不碰确定性审计的 conflict_ 行。"""
        pid = _new_project("G1-重跑不误删")
        deterministic_id = _seed_deterministic_conflict(pid)
        results = [{"id": 1, "status": "completed", "content": "甲" * 200}]
        sections = {1: {"title": "第一章"}}

        provider1 = _FakeProvider(
            {1: [{"section_id": "1", "issue": "第一轮发现", "suggestion": "y"}]},
            repair_text="乙" * 200,
        )
        _run_audit(pid, provider1, results, sections)
        first_rows = _ai_conflicts(pid)
        assert len(first_rows) == 1

        provider2 = _FakeProvider({})
        _run_audit(pid, provider2, results, sections)

        with get_db() as conn:
            det = conn.execute(
                "SELECT * FROM consistency_conflicts WHERE conflict_id = ?",
                (deterministic_id,),
            ).fetchone()
        assert det is not None and det["resolved"] == 0, (
            "确定性审计写入的 conflict_ 行绝不能被 AI 审计路径清掉"
        )
        # 第二轮的 AI 行只有「无冲突」状态 —— 上轮 AI 发现已被清空
        assert _ai_conflicts(pid) == []
        assert len(_audit_log_rows(pid)) == 2

    def test_finding_without_section_id_not_persisted(self):
        """LLM 返回缺 section_id 的条目按既有口径跳过，不得凭空造行。"""
        pid = _new_project("G1-无 section_id 跳过")
        results = [{"id": 1, "status": "completed", "content": "甲" * 200}]
        provider = _FakeProvider({1: [{"issue": "全局矛盾"}]})

        _run_audit(pid, provider, results, {1: {"title": "第一章"}})

        assert _ai_conflicts(pid) == []

    def test_persist_failure_does_not_break_audit(self, monkeypatch, caplog):
        """落库异常只记日志——不得吞掉/翻转已完成的审计与修复结果。"""
        pid = _new_project("G1-落库失败兜底")
        results = [{"id": 1, "status": "completed", "content": "甲" * 200}]
        provider = _FakeProvider(
            {1: [{"section_id": "1", "issue": "x", "suggestion": "y"}]},
            repair_text="乙" * 200,
        )

        def _boom(*_a, **_kw):
            raise RuntimeError("模拟落库失败")

        monkeypatch.setattr(wf, "_persist_ai_audit_findings", _boom)

        with caplog.at_level("WARNING"):
            out = _run_audit(pid, provider, results, {1: {"title": "第一章"}})

        # 审计与修复照常返回
        assert out is results
        assert results[0]["content"] == "乙" * 200
        assert any("落库失败" in r.message for r in caplog.records)


# ==================== G2：生成中防护门 ====================


def _hold_lock(project_id: int, task_type: str):
    rec = acquire(task_type, project_id=project_id, note="test-g1g2")
    assert rec.get("acquired"), f"测试锁获取失败: {rec}"
    key, owner = rec["lock_key"], rec["owner_id"]

    def _release() -> None:
        release(key, owner)

    return _release


class TestAiAuditGenerationGuard:
    def test_outline_generation_blocks_audit(self, caplog):
        """目录生成持锁（同组互斥，全表重建会丢写入）→ 跳过审计、原样返回。"""
        pid = _new_project("G2-目录生成拦截")
        release_lock = _hold_lock(pid, "outline-generation")
        try:
            results = [{"id": 1, "status": "completed", "content": "甲" * 200}]
            sections = {1: {"title": "第一章"}}
            provider = _FakeProvider(
                {1: [{"section_id": "1", "issue": "x", "suggestion": "y"}]},
                repair_text="乙" * 200,
            )

            with caplog.at_level("WARNING"):
                out = _run_audit(pid, provider, results, sections)

            # 跳过审计：原文不动、一次 AI 调用都没发生、无落库
            assert out is results
            assert results[0]["content"] == "甲" * 200
            assert provider.audit_prompts == []
            assert _ai_conflicts(pid) == []
            assert _audit_log_rows(pid) == []
            assert any("一致性审计被任务守卫拦截" in r.message for r in caplog.records)
        finally:
            release_lock()

    def test_content_generation_lock_does_not_self_block(self, caplog):
        """防自锁（本契约的核心）：锁是正文生成任务自己持有的。

        内容类写入口径 `allow_content_edit=True` 下审计必须照常跑——
        否则默认口径会把自己的锁判成冲突，审计永远 409 静默跳过，
        「全文一致性审计」选项等于失效。
        """
        pid = _new_project("G2-正文生成不自锁")
        release_lock = _hold_lock(pid, "content-generation")
        try:
            results = [{"id": 1, "status": "completed", "content": "甲" * 200}]
            sections = {1: {"title": "第一章"}}
            provider = _FakeProvider(
                {1: [{"section_id": "1", "issue": "x", "suggestion": "y"}]},
                repair_text="乙" * 200,
            )

            with caplog.at_level("WARNING"):
                out = _run_audit(pid, provider, results, sections)

            assert out is results
            assert results[0]["content"] == "乙" * 200, "正文生成持锁时审计应照常修复"
            assert provider.audit_prompts, "审计应实际执行"
            assert not any(
                "一致性审计被任务守卫拦截" in r.message for r in caplog.records
            ), "allow_content_edit=True 口径下不得出现拦截告警"
            assert len(_ai_conflicts(pid)) == 1
        finally:
            release_lock()

    def test_no_lock_runs_audit(self):
        """无锁放行：审计与修复正常执行。"""
        pid = _new_project("G2-无锁放行")
        results = [{"id": 1, "status": "completed", "content": "甲" * 200}]
        provider = _FakeProvider(
            {1: [{"section_id": "1", "issue": "x", "suggestion": "y"}]},
            repair_text="乙" * 200,
        )

        _run_audit(pid, provider, results, {1: {"title": "第一章"}})

        assert results[0]["content"] == "乙" * 200
        assert len(_ai_conflicts(pid)) == 1

    def test_guard_blocks_before_any_ai_call(self):
        """拦截必须发生在任何 AI 调用之前（不能先花钱审计再被拦）。"""
        pid = _new_project("G2-拦截时机")
        release_lock = _hold_lock(pid, "outline-generation")
        try:
            results = [{"id": 1, "status": "completed", "content": "甲" * 200}]
            provider = _FakeProvider(
                {1: [{"section_id": "1", "issue": "x", "suggestion": "y"}]},
                repair_text="乙" * 200,
            )

            _run_audit(pid, provider, results, {1: {"title": "第一章"}})

            assert provider.audit_prompts == []
            assert results[0]["content"] == "甲" * 200
        finally:
            release_lock()


# ==================== 签名契约 ====================


class TestSignatureContract:
    def test_project_id_is_first_positional_param(self):
        """G1/G2 都依赖 project_id —— 必须显式入参而非靠 results 反推。"""
        import inspect

        params = list(inspect.signature(wf.consistency_audit_content).parameters)
        assert params[0] == "project_id"
        for name in ("provider", "results", "sections_by_id", "summary", "tender_context"):
            assert name in params
        assert "cancel_event" in params

    def test_guard_uses_content_edit_allowance(self):
        """准入口径必须是 allow_content_edit=True —— 允许正文类写法（防自锁）。

        直接读源码断言口径：改成默认 False 会立刻自锁，功能假性失效。
        """
        import inspect

        src = inspect.getsource(wf.consistency_audit_content)
        assert "reject_if_generation_running" in src
        assert "allow_content_edit=True" in src, (
            "一致性审计是内容类写入，必须用 allow_content_edit=True 口径"
        )

    def test_guard_import_path_resolves(self):
        """`from ..task_guard import ...` 必须真实可解析（曾写成
        `..services.task_guard`，从 services/ai/ 出发解析不到）。"""
        import importlib
        import inspect

        # 1) 源码里用的相对导入路径：workflows_expand 的 `..` == app.services
        src = inspect.getsource(wf.consistency_audit_content)
        assert "from ..task_guard import reject_if_generation_running" in src
        # 2) 该相对路径解析出的绝对模块必须真的提供这个符号
        mod = importlib.import_module("app.services.task_guard")
        assert callable(mod.reject_if_generation_running)
