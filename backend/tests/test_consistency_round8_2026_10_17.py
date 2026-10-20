"""一致性校核模块 —— 第八轮（2026-10-17）回归测试。

聚焦本轮实施/加固的行为，每条一个独立用例，钉死不变量：

| 用例组 | 主题 | 现象 / 修复 |
|---|---|---|
| A | `/summary` 摘要响应剥离 `conflicts_json` 巨型载荷（T7-1 技术债闭环） | `get_audit_summary` 此前 `SELECT *` 把整行（含数十 KB 的 `conflicts_json`）随响应下发，而前端只消费 `audit_time` 与存在性。现物化后立即剔除该键，降低带宽、行为不变 |
| B | 剥离后轻量字段仍完整 | 剔除 `conflicts_json` 不得误伤 `audit_time / total_sections / facts_extracted / conflicts_found / status / source_kind` 等摘要卡片真正依赖的字段 |

原则：只测可观察行为（HTTP 响应字段、指标快照），不依赖真实 AI/网络。
"""
from __future__ import annotations

import pytest

from app.db import get_db, init_db
from app.security.auth import User
from app.services.consistency_auditor import ConsistencyAuditor
from app.services.consistency_metrics import (
    consistency_metrics,
    record_autofix,
)


OWNER = 3600
API = "/api/v1"


def _new_project(name: str) -> int:
    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO projects (name, industry, created_by) VALUES (?, ?, ?)",
            (name, "通用", OWNER),
        )
        return conn.execute("SELECT id FROM projects ORDER BY id DESC LIMIT 1").fetchone()["id"]


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
# A. /summary 摘要响应剥离 conflicts_json（T7-1 技术债闭环）
# ============================================================================


class TestSummaryStripsHeavyPayload:
    """第八轮·T7-1：摘要响应里的 latest_audit 不再携带 conflicts_json。

    `consistency_audit_log.conflicts_json` 在大项目下可达数十 KB，而前端
    `AuditSummaryCards` 只消费 `latest_audit.audit_time` 与「是否确有审计记录」，
    该列纯属浪费带宽。后端在物化后立即剔除。
    """

    def test_latest_audit_has_no_conflicts_json(self):
        pid = _new_project("R8-summary-strip")
        # 直接灌一条带超大 conflicts_json 的审计历史，模拟大项目真实形态
        big_blob = "x" * 4096
        with get_db() as conn:
            conn.execute(
                "INSERT INTO consistency_audit_log "
                "(project_id, total_sections, facts_extracted, conflicts_found, "
                " conflicts_json, audit_time, status, source_kind) "
                "VALUES (?, 5, 9, 2, ?, '2026-10-17 12:00:00', 'completed', 'rule')",
                (pid, big_blob),
            )
        resp = _client().get(f"{API}/consistency/summary/{pid}")
        assert resp.status_code == 200, resp.text
        summary = resp.json()["summary"]
        assert summary["latest_audit"] is not None
        assert "conflicts_json" not in summary["latest_audit"], (
            f"摘要不应下发 conflicts_json，实际 keys={list(summary['latest_audit'].keys())}"
        )

    def test_latest_audit_is_null_when_no_audit(self):
        """无审计记录时 latest_audit 仍为 None（FIX-4 行为不被破坏）。"""
        pid = _new_project("R8-summary-none")
        resp = _client().get(f"{API}/consistency/summary/{pid}")
        assert resp.status_code == 200, resp.text
        summary = resp.json()["summary"]
        assert summary["latest_audit"] is None
        assert summary["audit_source"] == "rule"  # 默认回落，但前端据此不渲染来源标签


# ============================================================================
# B. 剔除后轻量字段仍完整（不得误伤摘要卡片依赖的字段）
# ============================================================================


class TestSummaryKeepsLightweightFields:
    """剔除 conflicts_json 不得误伤其它被前端消费的轻量字段。"""

    def test_required_light_fields_present(self):
        pid = _new_project("R8-summary-keep")
        with get_db() as conn:
            conn.execute(
                "INSERT INTO consistency_audit_log "
                "(project_id, total_sections, facts_extracted, conflicts_found, "
                " conflicts_json, audit_time, status, source_kind) "
                "VALUES (?, 7, 12, 3, '[]', '2026-10-17 13:00:00', 'completed', 'ai')",
                (pid,),
            )
        resp = _client().get(f"{API}/consistency/summary/{pid}")
        assert resp.status_code == 200, resp.text
        latest = resp.json()["summary"]["latest_audit"]
        for key in ("audit_time", "total_sections", "facts_extracted", "conflicts_found", "status", "source_kind"):
            assert key in latest, f"摘要 latest_audit 缺失关键字段 {key}（keys={list(latest.keys())}）"
        assert latest["audit_time"] == "2026-10-17 13:00:00"
        assert latest["total_sections"] == 7
        assert latest["conflicts_found"] == 3
        assert latest["source_kind"] == "ai"


# ============================================================================
# C. 运维监控埋点（第八轮·T7-3）：审计耗时/冲突数、autofix 成败与改写量
# ============================================================================


class TestAuditAutofixMetrics:
    """第八轮·T7-3：关键运维指标埋点（耗时/冲突数/改写量/成败计数）。

    说明：autofix 的「真正落文改写」依赖章节正文文本匹配，属 `apply_autofix`
    自身逻辑（已有专测，不在本类重复）。本类只验证**路由层指标埋点是否被正确触发**，
    故用 monkeypatch 把 `apply_autofix` 替换成确定性返回值，分别覆盖
    成功 / 无改动 / 抛异常三条分支，避免依赖真实 DB 文本匹配导致环境相关抖动。
    """

    def test_audit_records_duration_and_conflict_count(self):
        consistency_metrics.reset()
        pid = _new_project("R8-metrics-audit")
        resp = _client().post(f"{API}/consistency/audit/{pid}")
        assert resp.status_code == 200, resp.text
        snap = consistency_metrics.snapshot_with_defaults()
        # 耗时与检出冲突数被累计记录（失败审计 conflicts_detected 兜底为 0）
        assert snap["audit_duration_ms_total"] >= 0
        assert snap["conflicts_detected_total"] >= 0
        # 至少一次审计成功或失败被计入
        assert snap["audit_runs"] + snap["audit_failed"] >= 1

    def test_autofix_records_applied_and_edits(self, monkeypatch):
        consistency_metrics.reset()
        monkeypatch.setattr(
            ConsistencyAuditor,
            "apply_autofix",
            lambda self, *a, **k: {
                "success": True,
                "total_replacements": 2,
                "changed_sections": 1,
            },
        )
        pid = _new_project("R8-metrics-autofix")
        resp = _client().post(
            f"{API}/consistency/conflicts/conflict_dummy/autofix",
            params={"project_id": pid, "new_value": "500"},
        )
        assert resp.status_code == 200, resp.text
        snap = consistency_metrics.snapshot_with_defaults()
        assert snap["autofix_applied_total"] >= 1
        assert snap["autofix_edits_total"] >= 2

    def test_autofix_records_no_change(self, monkeypatch):
        consistency_metrics.reset()
        monkeypatch.setattr(
            ConsistencyAuditor,
            "apply_autofix",
            lambda self, *a, **k: {"success": False, "changed_sections": 0},
        )
        pid = _new_project("R8-metrics-autofix-nc")
        resp = _client().post(
            f"{API}/consistency/conflicts/conflict_dummy/autofix",
            params={"project_id": pid, "new_value": "500"},
        )
        assert resp.status_code == 200, resp.text
        assert consistency_metrics.snapshot_with_defaults()["autofix_no_change_total"] >= 1

    def test_autofix_records_failed(self, monkeypatch):
        consistency_metrics.reset()

        def _boom(self, *a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(ConsistencyAuditor, "apply_autofix", _boom)
        pid = _new_project("R8-metrics-autofix-fail")
        resp = _client().post(
            f"{API}/consistency/conflicts/conflict_dummy/autofix",
            params={"project_id": pid, "new_value": "500"},
        )
        assert resp.status_code == 500, resp.text
        assert consistency_metrics.snapshot_with_defaults()["autofix_failed_total"] >= 1

    def test_record_autofix_helper_branches(self):
        """record_autofix 封装对成功/无改动/失败/改写量四分支计数正确。"""
        consistency_metrics.reset()
        record_autofix(applied=True, edits=3)
        record_autofix(no_change=True)
        record_autofix(failed=True)
        snap = consistency_metrics.snapshot()
        assert snap["autofix_applied_total"] == 1
        assert snap["autofix_no_change_total"] == 1
        assert snap["autofix_failed_total"] == 1
        assert snap["autofix_edits_total"] == 3
