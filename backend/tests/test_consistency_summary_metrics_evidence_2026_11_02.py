"""一致性校核 —— summary 来源对称计数、metrics 起点、severity 判定依据（2026-11-02）。

覆盖方案《方案_一致性校核模块深度审计与修复_2026-11-02》三项：

TD-9（/summary 统计口径不对称）：`unresolved` 侧早已按来源拆分
（`unresolved_rule_conflicts` / `unresolved_ai_findings`），而 `resolved` 侧
只有合计数 —— 前端「已解决」卡片无法区分来源，两侧口径不对称。
现补 `resolved_rule_conflicts`，恒等式：
  unresolved_rule + unresolved_ai == unresolved_conflicts
  resolved_rule + resolved_ai     == resolved_conflicts

G-3（/metrics 累计值无起点）：计数器是进程内累计、重启归零，但没有起点时间，
累计值无法解读；多进程部署下还各自为政。现纯新增 `since` + `notes`。

G-1（严重度判定黑盒）：`assess_severity` 只回 high/medium/low，用户无法知道
"为什么是 high"。现补 `severity_rationale`（判定依据：命中的分支、阈值、
取值数），`Conflict.evidence` 携带实例数/命中章节，/audit 响应以
`fact_evidence` 字段纯新增下发。

L5（2026-11-03 遗留项加固）：判定依据落库（`evidence_json` 列），
`GET /conflicts` 回放同样下发 `fact_evidence` —— 重新进入页面后依据浮层可用。
"""
from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from app.db import get_db, init_db
from app.security.auth import User, get_current_user
from app.services import consistency_rules as rules
from app.services.consistency_auditor import ConsistencyAuditor


OWNER = 3520


def _new_project(name: str = "判定依据测试") -> int:
    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO projects (name, industry, created_by) VALUES (?, ?, ?)",
            (name, "通用", OWNER),
        )
        return conn.execute("SELECT id FROM projects ORDER BY id DESC LIMIT 1").fetchone()["id"]


def _seed_section(project_id: int, *, title: str, content: str, sort_order: int = 0) -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO sections (project_id, title, level, sort_order, content, status, section_type) "
            "VALUES (?, ?, 1, ?, ?, 'completed', 'normal')",
            (project_id, title, sort_order, content),
        )
        return cur.lastrowid


def _seed_conflict(
    project_id: int,
    *,
    conflict_id: str,
    values_json: str = '["45","50"]',
    severity: str = "medium",
    resolved: int = 0,
) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO consistency_conflicts "
            "(project_id, conflict_id, fact_name, values_json, sources_json, "
            " severity, description, resolved, created_at) "
            "VALUES (?, ?, '工期', ?, '[]', ?, '测试冲突', ?, '2026-11-02 10:00:00')",
            (project_id, conflict_id, values_json, severity, resolved),
        )


def _client():
    from app.main import app

    app.dependency_overrides[get_current_user] = lambda: User(OWNER, "u", "user")
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
# TD-9 · /summary 已解决侧来源对称计数
# ============================================================================


class TestSummarySourceSymmetry:
    def test_resolved_rule_conflicts_identity(self):
        """四个来源计数满足两条恒等式，且与注入数据精确一致。"""
        pid = _new_project("TD9-对称计数")
        _seed_conflict(pid, conflict_id=f"t9_rule_open_{pid}", resolved=0)
        _seed_conflict(pid, conflict_id=f"t9_rule_done_{pid}", resolved=1)
        _seed_conflict(pid, conflict_id=f"AI-t9_ai_open_{pid}", values_json="[]", resolved=0)
        _seed_conflict(pid, conflict_id=f"AI-t9_ai_done_{pid}", values_json="[]", resolved=1)

        body = _client().get(f"/api/v1/consistency/summary/{pid}").json()
        s = body["summary"]

        assert s["unresolved_rule_conflicts"] == 1
        assert s["unresolved_ai_findings"] == 1
        # TD-9 新增字段：已解决侧同样按来源拆分
        assert s["resolved_rule_conflicts"] == 1
        assert s["resolved_ai_findings"] == 1
        # 两侧恒等式
        assert (
            s["unresolved_rule_conflicts"] + s["unresolved_ai_findings"]
            == s["unresolved_conflicts"]
        )
        assert (
            s["resolved_rule_conflicts"] + s["resolved_ai_findings"]
            == s["resolved_conflicts"]
        )
        # 既有字段口径不动（向后兼容钉）
        assert s["unresolved_conflicts"] == 2
        assert s["resolved_conflicts"] == 2

    def test_null_resolved_counted_as_open_side(self):
        """resolved=NULL 行归入未解决侧计数（COALESCE 口径与 TD-1 同源）。"""
        pid = _new_project("TD9-NULL计数")
        with get_db() as conn:
            conn.execute(
                "INSERT INTO consistency_conflicts "
                "(project_id, conflict_id, fact_name, values_json, sources_json, "
                " severity, description, resolved, created_at) "
                "VALUES (?, ?, '工期', '[\"45\",\"50\"]', '[]', 'high', 'NULL行', "
                " NULL, '2026-11-02 10:00:00')",
                (pid, f"t9_null_{pid}"),
            )

        s = _client().get(f"/api/v1/consistency/summary/{pid}").json()["summary"]
        assert s["unresolved_rule_conflicts"] == 1
        assert s["resolved_rule_conflicts"] == 0
        assert s["unresolved_conflicts"] == 1


# ============================================================================
# G-3 · /metrics 增加计数起点与解读说明
# ============================================================================


class TestMetricsSince:
    def test_metrics_expose_since_and_notes(self):
        body = _client().get("/api/v1/consistency/metrics").json()
        assert body["status"] == "ok"
        # 既有 metrics 键保持存在（纯新增字段，向后兼容）
        assert isinstance(body["metrics"], dict)
        assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", body["since"])
        assert "进程重启归零" in body["notes"]
        assert "consistency_audit_log" in body["notes"]

    def test_process_started_at_format(self):
        from app.services.consistency_metrics import process_started_at

        value = process_started_at()
        assert re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", value)


# ============================================================================
# G-1 · severity_rationale 判定依据与 assess_severity 单一实现体
# ============================================================================


class TestSeverityRationale:
    def test_critical_fact_high_with_reason(self):
        ev = rules.severity_rationale("工期", ["45", "50"], section_count=3)
        assert ev["severity"] == "high"
        assert ev["critical_fact"] is True
        assert ev["reason"]
        assert "阈值" in ev or "关键" in ev["reason"]
        assert ev["thresholds"]["high_distinct_gt"] > 0

    def test_delegates_consistent_with_assess_severity(self):
        """assess_severity 与 severity_rationale 是同一实现体的两个出口，
        任何取值组合下严重度必须一致（单一事实来源钉）。"""
        cases = [
            ("工期", ["1", "2"], 5),
            ("金额", ["100", "200", "300", "400"], 5),
            ("质量标准", ["A", "B"], 1),
            ("random_fact", ["x", "y", "z", "w"], 30),
        ]
        for name, values, sc in cases:
            direct = rules.assess_severity(name, values, section_count=sc)
            via_ev = rules.severity_rationale(name, values, section_count=sc)
            assert direct == via_ev["severity"], (name, values, sc)
            assert via_ev["distinct_count"] == len(values)

    def test_many_distinct_values_hits_threshold_branch(self):
        ev = rules.severity_rationale("某设施配置", [str(i) for i in range(6)], section_count=4)
        assert ev["severity"] == "high"
        assert ev["distinct_count"] == 6

    def test_single_section_two_values_is_low(self):
        ev = rules.severity_rationale("某岗位人数", ["20", "21"], section_count=1)
        assert ev["severity"] == "low"
        assert "单点" in ev["reason"] or ev["section_count"] == 1

    def test_default_branch_is_medium(self):
        ev = rules.severity_rationale("暂设金额", ["100", "200"], section_count=8)
        assert ev["severity"] == "medium"
        assert ev["critical_fact"] is False


# ============================================================================
# G-1 · detect_conflicts 填充 evidence，/audit 响应下发 fact_evidence
# ============================================================================


class TestAuditEvidence:
    def _seed_conflicting_project(self, name: str) -> int:
        pid = _new_project(name)
        _seed_section(pid, title="一、总体部署", content="本项目建设工期：45 天。", sort_order=0)
        _seed_section(pid, title="二、施工进度计划", content="项目总工期为 50 天。", sort_order=1)
        return pid

    def test_detect_conflicts_fills_evidence(self):
        pid = self._seed_conflicting_project("G1-detect")
        conflicts = ConsistencyAuditor().detect_conflicts(pid)
        target = next(c for c in conflicts if c.fact_name == "工期")
        assert target.evidence is not None
        assert target.evidence["severity"] == target.severity
        assert target.evidence["instance_count"] >= 2
        assert len(target.evidence["section_ids"]) >= 1
        assert target.evidence["reason"]
        assert "thresholds" in target.evidence

    def test_audit_route_serializes_fact_evidence(self):
        pid = self._seed_conflicting_project("G1-路由")
        resp = _client().post(f"/api/v1/consistency/audit/{pid}")
        assert resp.status_code == 200
        conflicts = resp.json()["conflicts"]
        target = next(c for c in conflicts if c["fact_name"] == "工期")
        ev = target["fact_evidence"]
        assert ev["severity"] == target["severity"]
        assert isinstance(ev["reason"], str) and ev["reason"]
        assert isinstance(ev["instance_count"], int)
        # json 可序列化（响应真的过 JSON 编码，dict 里不能藏 dataclass/Row）
        json.dumps(ev)


# ============================================================================
# L5 · 判定依据落库：GET /conflicts 回放下发 fact_evidence（2026-11-03）
# ============================================================================


class TestEvidencePersistence:
    def _seed_conflicting_project(self, name: str) -> int:
        pid = _new_project(name)
        _seed_section(pid, title="一、总体部署", content="本项目建设工期：45 天。", sort_order=0)
        _seed_section(pid, title="二、施工进度计划", content="项目总工期为 50 天。", sort_order=1)
        return pid

    def test_audit_persists_evidence_and_replay_serves_it(self):
        """审计落库后，GET /conflicts 回放与执行响应下发同一份 fact_evidence。"""
        pid = self._seed_conflicting_project("L5-回放")
        audit_resp = _client().post(f"/api/v1/consistency/audit/{pid}")
        assert audit_resp.status_code == 200
        served = {
            c["conflict_id"]: c["fact_evidence"]
            for c in audit_resp.json()["conflicts"]
            if c["fact_name"] == "工期"
        }
        assert served and all(ev is not None for ev in served.values())

        replay = _client().get(f"/api/v1/consistency/conflicts/{pid}").json()["conflicts"]
        replay_target = next(c for c in replay if c["fact_name"] == "工期")
        # 同一 conflict_id、同一份依据（字段级一致，非仅非空）
        assert replay_target["conflict_id"] in served
        assert replay_target["fact_evidence"] == served[replay_target["conflict_id"]]
        # 原始 JSON 列不外泄（下发的键是 fact_evidence）
        assert "evidence_json" not in replay_target

    def test_replay_ai_row_has_no_evidence(self):
        """AI 发现行（AI- 前缀）evidence_json 落库为 NULL → 回放 fact_evidence=None。"""
        pid = _new_project("L5-AI行")
        _seed_conflict(pid, conflict_id=f"AI-l5_{pid}", values_json="[]")
        row = _client().get(f"/api/v1/consistency/conflicts/{pid}").json()["conflicts"][0]
        assert row["source_kind"] == "ai"
        assert row["fact_evidence"] is None

    def test_replay_corrupt_evidence_json_is_none(self):
        """evidence_json 脏数据：回放降级为 None，不炸接口。"""
        pid = _new_project("L5-脏JSON")
        _seed_conflict(pid, conflict_id=f"l5_bad_{pid}")
        with get_db() as conn:
            conn.execute(
                "UPDATE consistency_conflicts SET evidence_json = '{not-json' "
                "WHERE project_id = ?",
                (pid,),
            )
        row = _client().get(f"/api/v1/consistency/conflicts/{pid}").json()["conflicts"][0]
        assert row["fact_evidence"] is None

    def test_migration_adds_column_idempotent(self):
        """老库（无 evidence_json 列）补列幂等，可直接调用迁移函数验证。"""
        from app.db import _migrate_consistency_conflicts_evidence

        with get_db() as conn:
            before = {r["name"] for r in conn.execute("PRAGMA table_info(consistency_conflicts)")}
            _migrate_consistency_conflicts_evidence(conn)
            _migrate_consistency_conflicts_evidence(conn)  # 幂等：重复调用不报错
            after = {r["name"] for r in conn.execute("PRAGMA table_info(consistency_conflicts)")}
        # init_db 已含新库 DDL；无论 before 是否已有列，after 必有且唯一
        assert "evidence_json" in after
        assert list(after).count("evidence_json") == 1
        assert before <= after
