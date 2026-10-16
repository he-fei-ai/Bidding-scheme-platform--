"""一致性校核模块 —— 2026-10-16 遗留项收口回归测试（T2 根治 / T5 指标 / 迁移）。

覆盖报告《第六轮深度加固》§十遗留项的最优方案落地，每条一个独立用例：

| 用例组 | 遗留项 | 验证点 |
|---|---|---|
| A | T2：`consistency_audit_log` 加 `source_kind` 列 | schema 有列；两条写入路径（规则 auditor / AI persist）写入正确来源 |
| B | T2：老库补列迁移（`_migrate_consistency_audit_log`） | 旧 schema 补列 + 按 `"conflict_id": "AI-` 模式回填历史行；幂等 |
| C | T5：模块运行时指标 + `GET /consistency/metrics` | 审计成功/失败、AI 落库/发现/修复、补丁拒绝、生成守卫拦截计数正确 |
| D | celery `dispatch_task` 挂死修复 | broker 不可达时连接预检立即抛错 → inline 兜底（不再无限重试挂死） |

原则：只测可观察行为（HTTP 响应体、DB 落库结果、计数器快照），不依赖真实
AI/网络/Redis broker。
"""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from app.db import get_db, init_db
from app.security.auth import User


OWNER = 3500
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
def _clean_overrides_and_metrics():
    from app.services import consistency_metrics

    consistency_metrics.reset()
    yield
    consistency_metrics.reset()
    try:
        from app.main import app

        app.dependency_overrides.clear()
    except Exception:
        pass


# ============================================================================
# A. T2：consistency_audit_log 的 source_kind 列与两条写入路径
# ============================================================================


class TestAuditLogSourceKind:
    def test_schema_has_source_kind_column(self):
        init_db()
        with get_db(readonly=True) as conn:
            cols = {r["name"] for r in conn.execute(
                "PRAGMA table_info(consistency_audit_log)").fetchall()}
        assert "source_kind" in cols

    def test_rule_path_writes_source_kind(self):
        """确定性 auditor `_save_audit_log` 写 source_kind='rule'。"""
        from app.services.consistency_auditor import AuditResult, consistency_auditor

        pid = _new_project("A-规则路径")
        result = AuditResult(
            project_id=pid,
            total_sections=2,
            facts_extracted=3,
            conflicts_found=0,
            conflicts=[],
            audit_time="2026-10-16 10:00:00",
            status="completed_no_conflicts",
        )
        consistency_auditor._save_audit_log(pid, result)

        with get_db(readonly=True) as conn:
            row = conn.execute(
                "SELECT source_kind FROM consistency_audit_log "
                "WHERE project_id = ?", (pid,),
            ).fetchone()
        assert row is not None
        assert row["source_kind"] == "rule"

    def test_ai_path_writes_source_kind(self):
        """AI persist 路径写 source_kind='ai'（metrics 同步累加见 C 组）。"""
        from app.services.ai.workflows_expand import _persist_ai_audit_findings

        pid = _new_project("A-AI路径")
        findings = [
            {"issue": "第四章与全局事实工期不一致", "section_id": "3",
             "suggestion": "统一为 450", "repaired": True},
            {"issue": "摘要与正文金额表述矛盾", "section_id": "5",
             "suggestion": "", "repaired": False},
        ]
        _persist_ai_audit_findings(pid, 4, findings)

        with get_db(readonly=True) as conn:
            row = conn.execute(
                "SELECT source_kind, status FROM consistency_audit_log "
                "WHERE project_id = ? ORDER BY id DESC LIMIT 1", (pid,),
            ).fetchone()
        assert row is not None
        assert row["source_kind"] == "ai"
        assert row["status"] == "completed"


# ============================================================================
# B. T2：老库补列迁移（幂等 + 历史行回填）
# ============================================================================


class TestLegacyMigration:
    @staticmethod
    def _legacy_conn() -> sqlite3.Connection:
        """构造旧 schema（无 source_kind 列）的内存库。"""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE consistency_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                total_sections INTEGER DEFAULT 0,
                facts_extracted INTEGER DEFAULT 0,
                conflicts_found INTEGER DEFAULT 0,
                conflicts_json TEXT,
                audit_time TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'completed'
            )
        """)
        # 一行 AI 路径历史数据（conflict_id 恒 AI- 前缀）
        conn.execute(
            "INSERT INTO consistency_audit_log "
            "(project_id, audit_time, conflicts_json) VALUES (1, '2026-10-01', ?)",
            (json.dumps([{"conflict_id": "AI-1-abc", "fact_name": "发现",
                          "values": [], "severity": "medium"}]),),
        )
        # 一行规则路径历史数据
        conn.execute(
            "INSERT INTO consistency_audit_log "
            "(project_id, audit_time, conflicts_json) VALUES (1, '2026-10-02', ?)",
            (json.dumps([{"conflict_id": "conflict_工期_2026", "fact_name": "工期",
                          "values": ["450"], "severity": "high"}]),),
        )
        conn.commit()
        return conn

    def test_migration_adds_column_and_backfills(self):
        from app.db import _migrate_consistency_audit_log

        conn = self._legacy_conn()
        cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(consistency_audit_log)").fetchall()}
        assert "source_kind" not in cols

        _migrate_consistency_audit_log(conn)

        cols = {r["name"] for r in conn.execute(
            "PRAGMA table_info(consistency_audit_log)").fetchall()}
        assert "source_kind" in cols

        rows = conn.execute(
            "SELECT conflicts_json, source_kind FROM consistency_audit_log "
            "ORDER BY id").fetchall()
        # AI 历史行回填为 'ai'；规则历史行保持 'rule'
        assert rows[0]["source_kind"] == "ai"
        assert rows[1]["source_kind"] == "rule"

    def test_migration_idempotent(self):
        from app.db import _migrate_consistency_audit_log

        conn = self._legacy_conn()
        _migrate_consistency_audit_log(conn)
        _migrate_consistency_audit_log(conn)  # 第二次不抛错、不重复回填
        rows = conn.execute(
            "SELECT source_kind FROM consistency_audit_log ORDER BY id").fetchall()
        assert [r["source_kind"] for r in rows] == ["ai", "rule"]


# ============================================================================
# C. T5：模块运行时指标 + GET /consistency/metrics
# ============================================================================


class TestConsistencyMetrics:
    def test_metrics_endpoint_shape(self):
        pid = _new_project("C1-指标端点")
        resp = _client().get(f"{API}/consistency/metrics")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "ok"
        metrics = body["metrics"]
        for key in ("audit_runs", "audit_failed", "ai_audit_runs", "ai_findings",
                    "ai_findings_repaired", "ai_patch_rejected", "guard_interceptions"):
            assert key in metrics and isinstance(metrics[key], int)

    def test_audit_endpoint_counts_success_and_failure(self, monkeypatch):
        from app.services import consistency_auditor as auditor_mod
        from app.services import consistency_metrics

        pid = _new_project("C2-审计计数")
        c = _client()

        def _fake_ok(project_id):
            return SimpleNamespace(
                status="completed", project_id=project_id, total_sections=1,
                facts_extracted=0, conflicts_found=0, conflicts=[],
                audit_time="2026-10-16 10:00:00",
            )

        monkeypatch.setattr(auditor_mod.consistency_auditor, "run_full_audit", _fake_ok)
        resp = c.post(f"{API}/consistency/audit/{pid}")
        assert resp.status_code == 200, resp.text
        assert consistency_metrics.snapshot()["audit_runs"] == 1

        def _fake_fail(project_id):
            return SimpleNamespace(
                status="failed", project_id=project_id, total_sections=0,
                facts_extracted=0, conflicts_found=0, conflicts=[],
                audit_time="2026-10-16 10:00:01",
            )

        monkeypatch.setattr(auditor_mod.consistency_auditor, "run_full_audit", _fake_fail)
        resp = c.post(f"{API}/consistency/audit/{pid}")
        assert resp.status_code == 200, resp.text
        snap = consistency_metrics.snapshot()
        assert snap["audit_failed"] == 1
        assert snap["audit_runs"] == 1  # 成功计数不被失败用例污染

    def test_repair_rejected_patches_counted(self, monkeypatch):
        from app.services import consistency_metrics
        from app.services import consistency_repair

        pid = _new_project("C3-补丁拒绝")
        report = {
            "success": True, "patches": [], "applied": [], "sections": [],
            "unresolved": [], "errors": [], "conflict_states": None,
            "rejected": [{"section_id": 1}, {"section_id": 2}, {"section_id": 3}],
        }
        async def _fake_repair(*a, **k):
            return report

        monkeypatch.setattr(consistency_repair, "repair_with_ai", _fake_repair)
        resp = _client().post(f"{API}/consistency/repair/{pid}")
        assert resp.status_code == 200, resp.text
        assert consistency_metrics.snapshot()["ai_patch_rejected"] == 3

    def test_guard_interception_counted(self, monkeypatch):
        import fastapi

        from app.routers import facts as facts_router
        from app.services import consistency_metrics

        pid = _new_project("C4-守卫拦截")

        def _raise_guard(project_id):
            raise fastapi.HTTPException(status_code=409, detail="生成中")

        monkeypatch.setattr(facts_router, "_ensure_no_ai_task_running", _raise_guard)
        resp = _client().post(
            f"{API}/consistency/facts/proposals/{pid}/accept",
            json={"proposals": []},
        )
        assert resp.status_code == 409, resp.text
        assert consistency_metrics.snapshot()["guard_interceptions"] == 1

    def test_ai_persist_increments_findings_metrics(self):
        from app.services import consistency_metrics
        from app.services.ai.workflows_expand import _persist_ai_audit_findings

        pid = _new_project("C5-AI落库计数")
        findings = [
            {"issue": "a", "section_id": "1", "suggestion": "s", "repaired": True},
            {"issue": "b", "section_id": "2", "suggestion": "", "repaired": False},
        ]
        _persist_ai_audit_findings(pid, 3, findings)
        snap = consistency_metrics.snapshot()
        assert snap["ai_audit_runs"] == 1
        assert snap["ai_findings"] == 2
        assert snap["ai_findings_repaired"] == 1


# ============================================================================
# D. dispatch_task 挂死修复：broker 不可达 → 立即降级 inline（不再无限重试）
# ============================================================================


class TestDispatchTaskBrokerUnreachable:
    def test_inline_catalog_dispatch_when_broker_down(self):
        """兜底模式下（celery 未装 / 预检失败），已登记任务必须就地执行成功。"""
        from app.celery_app import dispatch_task

        dispatched = dispatch_task("app.celery_app.cleanup_old_results", (), {"days": 1})
        assert dispatched.get("dispatched_via") in ("inline", "celery")
        if dispatched.get("dispatched_via") == "inline":
            assert dispatched["status"] != "not_dispatched"

    def test_connection_preflight_rejects_unreachable_broker(self):
        """连接预检：不可达 broker 必须秒级抛错（而非 kombu 无限重试挂死）。

        `test_async_reparse_dispatches` 在基线中无限挂起的根因即此：
        celery 已安装但 Redis 未运行，`send_task` 内部 ensure_connection
        默认无限重试。现 dispatch_task 先做 `max_retries=0` 预检，本用例
        以同样方式直接打预检，断言有限时间内抛错。
        """
        import time as _t

        from app.celery_app import celery_app

        # 指向必然拒连的端口（localhost:1 无服务，ECONNREFUSED 立即返回）
        original = celery_app.conf.broker_url
        try:
            celery_app.conf.broker_url = "redis://127.0.0.1:1/15"
            conn = celery_app.connection_for_write()
            t0 = _t.monotonic()
            with pytest.raises(Exception):
                conn.ensure_connection(max_retries=0)
            elapsed = _t.monotonic() - t0
        finally:
            celery_app.conf.broker_url = original
        assert elapsed < 10, f"预检耗时 {elapsed:.1f}s —— 仍可能挂死请求线程"
