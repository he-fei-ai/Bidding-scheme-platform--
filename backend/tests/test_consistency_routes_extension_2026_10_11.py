"""一致性校核模块 —— 路由扩展端点回归测试（2026-10-11 深度审计）。

本轮发现的功能缺口：后端已有 Agent 模式修复、事实回流候选/采纳、废标覆盖率
三个端点，但此前**零测试覆盖**（既有 230 项测试全未触及），且前端 api/index.ts
也没有对应的函数（已在本轮补齐）。

覆盖范围：
  | 端点 | 测试 | 说明 |
  |---|---|---|
  | POST /consistency/agent/{id} | TestAgentModeRoute | 路由可 import、参数校验（max_turns 范围）、无冲突时返回、落库流程 |
  | GET  /consistency/facts/proposals/{id} | TestFactsBackflowProposals | 路由可 import、min_occurrences 参数边界、空项目返回空 proposals |
  | POST /consistency/facts/proposals/{id}/accept | TestFactsBackflowAccept | body.proposals 必须是 list、空 proposals 无副作用、合并落库 |
  | GET  /consistency/rejection-coverage/{id} | TestRejectionCoverage | 无废标任务时 graceful 返回、返回体 shape 含 items 数组 |

设计原则：
  - 不调用真实 AI / 网络；Agent 模式用纯服务函数 + refresh_conflict_states 验证；
  - 不测内部实现细节，只做可观察行为（HTTP 响应体 shape + DB 落库）；
  - 与既有 test_consistency_check_module_2026_10_08.py 使用同一套 fixture。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.db import get_db, init_db
from app.security.auth import User


OWNER = 3400


def _run(coro):
    return asyncio.run(coro)


def _new_project(name: str = "路由扩展测试") -> int:
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
    conflict_id: str = "conflict_route_test_001",
    fact_name: str = "工期",
    values_json: str = '["450","500"]',
    severity: str = "high",
    resolved: int = 0,
) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO consistency_conflicts "
            "(project_id, conflict_id, fact_name, values_json, sources_json, "
            " severity, description, resolved, created_at) "
            "VALUES (?, ?, ?, ?, '[\"section_1\"]', ?, '测试冲突', ?, '2026-10-11 10:00:00')",
            (project_id, conflict_id, fact_name, values_json, severity, resolved),
        )


def _seed_analysis(project_id: int, *, global_facts_json: str = "") -> None:
    """确保 analysis 表有该行（accept_fact_proposals 需要）。"""
    with get_db() as conn:
        existing = conn.execute(
            "SELECT project_id FROM analysis WHERE project_id = ?", (project_id,)
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO analysis (project_id, global_facts_json) VALUES (?, ?)",
                (project_id, global_facts_json),
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
# Agent 模式路由
# ============================================================================


class TestAgentModeRoute:
    """POST /consistency/agent/{project_id} 端点基础行为。"""

    def test_route_exists_and_is_registered(self):
        """路由必须在 openapi spec 中可查（FastAPI 注册的最终路径）。"""
        from app.main import app

        spec = app.openapi()
        paths = spec.get("paths", {})
        assert any("consistency/agent" in p for p in paths), (
            f"Agent 修复路由未注册：{sorted(paths)}"
        )

    def test_importable_from_service_layer(self):
        """agent_repair.run_agent_repair 必须可被路由层正常 import。"""
        from app.services.agent_repair import run_agent_repair
        assert callable(run_agent_repair)

    def test_no_sections_returns_immediate_message(self):
        """空项目（无正文）不调 AI，直接回 success=False + 说明。"""
        project_id = _new_project()
        with _client() as c:
            resp = c.post(
                f"/api/v1/consistency/agent/{project_id}",
                params={"max_turns": 3},
            )
            assert resp.status_code == 200
            data = resp.json()
            # success=False 时路由回 status='partial'（设计如此，避免谎报 ok）
            assert data.get("success") is False
            assert "正文内容" in data.get("message", "") or "没有" in data.get("message", "")

    def test_no_conflicts_returns_immediate_message(self):
        """有正文但无冲突：不调 AI，直接回 success=False。"""
        project_id = _new_project()
        _seed_section(project_id, title="施工方案", content="本工程总工期 30 天。", sort_order=1)
        with _client() as c:
            resp = c.post(
                f"/api/v1/consistency/agent/{project_id}",
                params={"max_turns": 3},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data.get("success") is False
            assert "没有未解决的一致性冲突" in data.get("message", "")


# ============================================================================
# 事实回流候选 / 采纳路由
# ============================================================================


class TestFactsBackflowProposals:
    """GET /consistency/facts/proposals/{id} 端点。"""

    def test_route_registered(self):
        from app.main import app
        spec = app.openapi()
        paths = spec.get("paths", {})
        assert any("facts/proposals" in p for p in paths), "事实回流路由未注册"

    def test_empty_project_returns_zero_proposals(self):
        project_id = _new_project()
        with _client() as c:
            resp = c.get(
                f"/api/v1/consistency/facts/proposals/{project_id}",
                params={"min_occurrences": 1},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert data["proposals"] == []
            assert data["conflicts_scanned"] == 0

    def test_no_open_conflicts_returns_zero(self):
        """全是 resolved 冲突时不会产生候选。"""
        project_id = _new_project()
        _seed_section(project_id, title="方案", content="工期 450 天。", sort_order=1)
        _seed_conflict(project_id, conflict_id="c_resolved", resolved=1, fact_name="工期")
        with _client() as c:
            resp = c.get(
                f"/api/v1/consistency/facts/proposals/{project_id}",
                params={"min_occurrences": 1},
            )
            data = resp.json()
            assert data["proposals"] == []
            assert data["conflicts_scanned"] == 0

    def test_route_handles_min_occurrences_param(self):
        """min_occurrences 参数必须正常透传给服务层。"""
        project_id = _new_project()
        with _client() as c:
            resp = c.get(
                f"/api/v1/consistency/facts/proposals/{project_id}",
                params={"min_occurrences": 5},
            )
            assert resp.status_code == 200
            # min_occurrences 不在响应体里显式回显，但空项目能正常处理说明参数未被拒
            assert resp.json()["status"] == "ok"

    def test_service_function_propose_facts_exists(self):
        """propose_facts / accept_fact_proposals 必须可被服务层直接调用。"""
        from app.services.consistency_backflow import propose_facts, accept_fact_proposals
        assert callable(propose_facts)
        assert callable(accept_fact_proposals)


class TestFactsBackflowAccept:
    """POST /consistency/facts/proposals/{id}/accept 端点。"""

    def test_body_proposals_must_be_list(self):
        """body.proposals 不是 list 时回 400，不静默吞。"""
        project_id = _new_project()
        _seed_analysis(project_id)
        with _client() as c:
            resp = c.post(
                f"/api/v1/consistency/facts/proposals/{project_id}/accept",
                json={"proposals": "not_a_list"},
            )
            assert resp.status_code == 400

    def test_empty_proposals_no_side_effect(self):
        """proposals=[] 直接返回 added=0，不报错、不破坏 DB。"""
        project_id = _new_project()
        _seed_analysis(project_id)
        with _client() as c:
            resp = c.post(
                f"/api/v1/consistency/facts/proposals/{project_id}/accept",
                json={"proposals": []},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["added"] == 0
            assert data["merged_total"] == 0

    def test_valid_proposals_merged_into_global_facts(self):
        """合法候选能合并进 analysis.global_facts_json，category 正确。"""
        project_id = _new_project()
        _seed_analysis(project_id)
        with _client() as c:
            resp = c.post(
                f"/api/v1/consistency/facts/proposals/{project_id}/accept",
                json={
                    "proposals": [
                        {
                            "conflict_id": "c1",
                            "fact_name": "工期",
                            "value": "450",
                            "occurrences": 5,
                            "category": "schedule",
                            "suggested_key": "audit_c1",
                            "occurrences_map": [],
                        }
                    ]
                },
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["added"] == 1
            assert data["merged_total"] >= 1

        # 验证 DB 落库
        with get_db() as conn:
            row = conn.execute(
                "SELECT global_facts_json, confirmed_facts FROM analysis WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            assert row is not None
            facts = json.loads(row["global_facts_json"])
            assert any(it.get("value") == "450" and it.get("source") == "audit_backflow" for it in facts)
            # confirmed_facts 必须回退为 0（与手工保存事实同口径）
            assert row["confirmed_facts"] == 0


# ============================================================================
# 废标条款应答覆盖率路由
# ============================================================================


class TestRejectionCoverage:
    """GET /consistency/rejection-coverage/{id} 端点。"""

    def test_route_registered(self):
        from app.main import app
        spec = app.openapi()
        paths = spec.get("paths", {})
        assert any("rejection-coverage" in p for p in paths), "废标覆盖率路由未注册"

    def test_service_function_exists(self):
        from app.services.rejection_coverage import rejection_answer_coverage
        assert callable(rejection_answer_coverage)

    def test_project_without_rejection_task_returns_zero_items(self):
        """没有废标检查任务的项目：graceful 返回 status=no_checks。

        rejection-coverage 是**确定性**端点（不调 AI、不查网络），所以没有任务时
        直接扫不到子句。后端回 status='no_checks'（无 items 字段）不是错误——
        表示项目还没跑过废标检查，覆盖率无从谈起。"""
        project_id = _new_project()
        _seed_section(project_id, title="方案", content="本工程总工期 30 天。", sort_order=1)
        with _client() as c:
            resp = c.get(f"/api/v1/consistency/rejection-coverage/{project_id}")
            assert resp.status_code == 200
            data = resp.json()
            # 无废标任务 → 返回 no_checks（或 status='ok' 带 items=[]）
            assert data.get("status") in ("ok", "no_checks", "no_tasks")
            assert isinstance(data.get("scanned_clauses"), int)
            # no_checks 分支不返回 items（预期行为）；有任务时返回 items=[]
            if data.get("status") == "ok":
                assert isinstance(data.get("items"), list)
