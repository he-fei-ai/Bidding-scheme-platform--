"""一致性校核模块 —— 2026-10-15 冲突来源（rule/ai）命名空间回归测试。

覆盖本轮修复的缺陷，每条一个独立用例，钉死行为不变量：

| 用例组 | 缺陷 / 能力 | 现象 |
|---|---|---|
| A | `consistency_conflicts` 两条审计链路（确定性规则 vs AI 语义发现）此前只在 `conflict_id` 前缀里隐式区分，对外接口一个都不暴露 | `GET /consistency/conflicts` 把 AI 发现与规则冲突混排，前端把 100 字 issue 当「事实项」；「自动修复」按钮对 AI 发现可点但必然 no_change |
| B | `GET /consistency/summary` 的 `conflicts_by_severity` 把 AI 发现（恒 medium）混进「中等冲突」卡片 | 卡片数字被虚高，三张分级卡片之和与「未解决冲突」对不上 |
| C | `consistency_audit_log` 无来源列，但最近一次审计可能来自正文生成收尾的 AI 审计 | 用户看到「最近审计时间」分不清是「开始审计」还是 AI 审计 |
| D | `POST /consistency/facts/proposals/{id}/accept` 直接写 `global_facts_json`，但无任何生成中防护 | 与 facts.py `_ensure_no_ai_task_running` 的 BUG-FIX 2026-09-29 同源：正文生成中改事实基准 → 前半旧工期、后半新工期 → 一致性审计反而报矛盾 |

原则：只测可观察行为（HTTP 响应体、规则命名空间函数、DB 落库结果），不依赖真实 AI/网络。
"""
from __future__ import annotations

import pytest

from app.db import get_db, init_db
from app.security.auth import User


OWNER = 3400
STRANGER_ID = 3401
API = "/api/v1"


def _new_project(name: str) -> int:
    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO projects (name, industry, created_by) VALUES (?, ?, ?)",
            (name, "通用", OWNER),
        )
        return conn.execute("SELECT id FROM projects ORDER BY id DESC LIMIT 1").fetchone()["id"]


def _seed_conflict(
    project_id: int,
    *,
    conflict_id: str,
    severity: str,
    resolved: int = 0,
    values: str = '["450","500"]',
    fact_name: str = "工期",
    created_at: str = "2026-10-08 10:00:00",
) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO consistency_conflicts "
            "(project_id, conflict_id, fact_name, values_json, sources_json, "
            " severity, description, resolved, created_at) "
            "VALUES (?, ?, ?, ?, '[\"section_1\"]', ?, '手工构造', ?, ?)",
            (project_id, conflict_id, fact_name, values, severity, resolved, created_at),
        )


def _seed_ai_finding(
    project_id: int,
    *,
    conflict_id: str,
    resolved: int = 0,
    created_at: str = "2026-10-08 10:00:00",
) -> None:
    """按 `_persist_ai_audit_findings` 的真实落库形态插一条 AI 语义发现。"""
    with get_db() as conn:
        conn.execute(
            "INSERT INTO consistency_conflicts "
            "(project_id, conflict_id, fact_name, values_json, sources_json, "
            " severity, description, resolved, resolved_value, resolved_at, created_at) "
            "VALUES (?, ?, ?, '[]', '[\"3\"]', 'medium', 'AI 发现说明', ?, ?, ?, ?)",
            (
                project_id,
                conflict_id,
                "第四章施工现场描述与全局事实不一致（修复建议：统一工期口径）",
                resolved,
                "AI 自动修复" if resolved else None,
                created_at if resolved else None,
                created_at,
            ),
        )


def _seed_audit_log(
    project_id: int,
    *,
    conflicts_json: str,
    facts_extracted: int = 3,
    status: str = "completed",
    audit_time: str = "2026-10-08 10:00:00",
) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO consistency_audit_log "
            "(project_id, total_sections, facts_extracted, conflicts_found, "
            " conflicts_json, audit_time, status) "
            "VALUES (?, 3, ?, 1, ?, ?, ?)",
            (project_id, facts_extracted, conflicts_json, audit_time, status),
        )
def _client(user_id: int = OWNER):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.security.auth import get_current_user

    app.dependency_overrides[get_current_user] = lambda: User(user_id, "u", "user")
    return TestClient(app)


@pytest.fixture(autouse=True)
def _clean_overrides():
    yield
    try:
        from app.main import app

        app.dependency_overrides.clear()
    except Exception:
        pass


# ============================================================================
# A. 冲突来源命名空间（consistency_rules）
# ============================================================================


class TestConflictSourceNamespace:
    def test_is_ai_conflict_id(self):
        from app.services import consistency_rules as rules

        assert rules.is_ai_conflict_id("AI-1-abc123") is True
        assert rules.is_ai_conflict_id("AI-2") is True
        # 防御式判定：None/空串/非 AI 前缀一律按规则冲突处理
        assert rules.is_ai_conflict_id("conflict_工期_2026") is False
        assert rules.is_ai_conflict_id(None) is False
        assert rules.is_ai_conflict_id("") is False

    def test_conflict_source_kind(self):
        from app.services import consistency_rules as rules

        assert rules.conflict_source_kind("AI-1") == rules.CONFLICT_SOURCE_AI == "ai"
        assert rules.conflict_source_kind("conflict_1") == rules.CONFLICT_SOURCE_RULE == "rule"
        assert rules.conflict_source_kind("") == "rule"

    def test_rules_payload_exposes_conflict_source(self):
        """规则快照必须下发冲突来源命名空间 —— 前端「AI 发现」标签的单一事实来源。"""
        from app.services import consistency_rules as rules

        payload = rules.rules_payload()
        assert "conflict_source" in payload
        ns = payload["conflict_source"]
        assert ns["rule"] == "rule"
        assert ns["ai"] == "ai"
        assert ns["ai_id_prefix"] == "AI-"
        assert ns["meta"]["ai"]["label"] == "AI 发现"
        assert ns["meta"]["rule"]["color"] == "red"

    def test_conflict_source_meta_unknown_falls_back_to_rule(self):
        from app.services import consistency_rules as rules

        # 未知值安全降级为规则冲突文案（不能冒充 AI 发现、误导「需人工核对」判断）
        assert rules.conflict_source_meta(None)["label"] == "规则冲突"
        assert rules.conflict_source_meta("nope")["color"] == "red"

    def test_summary_by_severity_rule_prefers_rule_only(self):
        """恒等式：unresolved_rule + unresolved_ai == unresolved；分级卡片只算规则。"""
        from app.services import consistency_rules as rules

        pid = _new_project("A-来源拆分")
        _seed_conflict(pid, conflict_id=f"{pid}_c1", severity="high", resolved=0)
        _seed_conflict(pid, conflict_id=f"{pid}_c2", severity="medium", resolved=0)
        _seed_conflict(pid, conflict_id=f"{pid}_c3", severity="low", resolved=1)
        _seed_ai_finding(pid, conflict_id=f"AI-{pid}-aaa", resolved=0)
        _seed_ai_finding(pid, conflict_id=f"AI-{pid}-bbb", resolved=1)

        summary = _client().get(f"{API}/consistency/summary/{pid}").json()["summary"]
        # AI 发现必须计入未解决/已解决，但**不计入**规则分级卡片
        assert summary["unresolved_conflicts"] == 3
        assert summary["unresolved_rule_conflicts"] == 2
        assert summary["unresolved_ai_findings"] == 1
        assert summary["resolved_ai_findings"] == 1
        assert summary["unresolved_rule_conflicts"] + summary["unresolved_ai_findings"] \
            == summary["unresolved_conflicts"]
        # 老口径 `conflicts_by_severity` 保留原语义（含 AI），新口径只算规则
        assert summary["conflicts_by_severity"] == {"high": 1, "medium": 2}
        assert summary["conflicts_by_severity_rule"] == {"high": 1, "medium": 1}
        for k in summary["conflicts_by_severity_rule"]:
            assert rules.normalize_severity(k) == k
# ============================================================================
# B. GET /consistency/conflicts 逐条暴露来源
# ============================================================================


class TestConflictsEndpointSourceKind:
    def test_mixed_conflicts_carry_source_kind(self):
        pid = _new_project("B-来源标记")
        _seed_conflict(pid, conflict_id=f"{pid}_rule1", severity="high", resolved=0)
        _seed_ai_finding(pid, conflict_id=f"AI-{pid}-find1", resolved=0)

        client = _client()
        body = client.get(f"{API}/consistency/conflicts/{pid}", params={"resolved": "all"}).json()
        assert body["status"] == "ok"
        by_id = {c["conflict_id"]: c for c in body["conflicts"]}
        assert by_id[f"{pid}_rule1"]["source_kind"] == "rule"
        assert by_id[f"AI-{pid}-find1"]["source_kind"] == "ai"
        # 老调用方依赖的所有既有字段仍然存在
        assert "fact_name" in by_id[f"{pid}_rule1"]
        assert "severity" in by_id[f"AI-{pid}-find1"]

    def test_ai_default_open_filter_returns_findings(self):
        """AI 发现默认出现在「待处理」清单里（用户需要跟进），这是既有行为。"""
        pid = _new_project("B-默认含AI")
        _seed_ai_finding(pid, conflict_id=f"AI-{pid}-x", resolved=0)
        body = _client().get(f"{API}/consistency/conflicts/{pid}").json()
        assert body["count"] == 1
        assert body["conflicts"][0]["source_kind"] == "ai"

    def test_fix_suggestion_on_ai_finding_returns_text_fallback(self):
        """AI 发现无结构化取值 → 建议接口回纯文本（target_value 为 null），前端
        「采纳建议并修复」保持禁用，但 AI 给的修复方向可读。"""
        pid = _new_project("B-AI建议")
        _seed_ai_finding(pid, conflict_id=f"AI-{pid}-y", resolved=0)
        client = _client()
        res = client.get(
            f"{API}/consistency/conflicts/AI-{pid}-y/fix-suggestion",
            params={"project_id": pid},
        )
        assert res.status_code == 200, res.text
        body = res.json()
        assert "AI 语义发现" in (body.get("suggestion") or "")
        assert body.get("target_value") is None


# ============================================================================
# C. 最近一次审计来源推断
# ============================================================================


class TestAuditSourceInference:
    def test_latest_ai_audit_inferred_ai(self):
        pid = _new_project("C-AI审计")
        _seed_audit_log(
            pid,
            conflicts_json='[{"conflict_id": "AI-9-abc", "fact_name": "发现", "values": [], "severity": "medium"}]',
            facts_extracted=0,
        )
        summary = _client().get(f"{API}/consistency/summary/{pid}").json()["summary"]
        assert summary["audit_source"] == "ai"

    def test_latest_rule_audit_inferred_rule(self):
        pid = _new_project("C-规则审计")
        _seed_audit_log(
            pid,
            conflicts_json='[{"conflict_id": "conflict_工期_2026", "fact_name": "工期", "values": [450], "severity": "high"}]',
        )
        summary = _client().get(f"{API}/consistency/summary/{pid}").json()["summary"]
        assert summary["audit_source"] == "rule"

    def test_empty_project_defaults_rule(self):
        pid = _new_project("C-空项目")
        summary = _client().get(f"{API}/consistency/summary/{pid}").json()["summary"]
        assert summary["audit_source"] == "rule"


# ============================================================================
# D. 事实回流采纳端点必须过「正文生成中」防护（409）
# ============================================================================


class TestBackflowAcceptGuard:
    def _acquire_lock(self, project_id: int, task_type: str):
        from app.services.task_lock import acquire, release

        rec = acquire(task_type, project_id=project_id, note="test-source-kind")
        assert rec.get("acquired"), f"测试锁获取失败: {rec}"
        return lambda: release(rec["lock_key"], rec["owner_id"])

    def test_accept_rejected_409_when_content_generation_running(self):
        """跨模块守卫：正文生成中采纳事实回流候选 == facts.py POST /facts 同一防护。

        断言 409 而非 500，证明守卫生效（在 `_LockManager` 与任何合并逻辑之前）。
        """
        pid = _new_project("D-正文生成中回流")
        release = self._acquire_lock(pid, "content-generation")
        try:
            client = _client()
            res = client.post(
                f"{API}/consistency/facts/proposals/{pid}/accept",
                json={"proposals": []},
            )
            assert res.status_code == 409, res.text
        finally:
            release()

    def test_accept_no_lock_returns_ok_empty(self):
        """无生成任务时，空 proposals 走通端点（added=0，不落数据）。"""
        pid = _new_project("D-无锁回流")
        res = _client().post(
            f"{API}/consistency/facts/proposals/{pid}/accept",
            json={"proposals": []},
        )
        assert res.status_code == 200, res.text
        assert res.json()["added"] == 0

    def test_accept_foreign_project_403(self):
        """IDOR：非属权项目在守卫之前就被拦截。"""
        pid = _new_project("D-越权回流")
        release = self._acquire_lock(pid, "content-generation")
        try:
            res = _client(user_id=STRANGER_ID).post(
                f"{API}/consistency/facts/proposals/{pid}/accept",
                json={"proposals": []},
            )
            assert res.status_code == 403, res.text
        finally:
            release()