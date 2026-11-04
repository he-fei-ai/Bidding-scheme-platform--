"""一致性校核 —— autofix 作用域/rowcount 防假成功 与 NULL resolved 可见性（2026-11-02）。

覆盖方案《方案_一致性校核模块深度审计与修复_2026-11-02》两项：

F-1（P1，`consistency_auditor.apply_autofix`）：
  1. 限定范围 SELECT 缺 `project_id` 过滤 → 跨项目来源章节被 SELECT 命中、
     却不被带 `AND project_id = ?` 的 UPDATE 命中 → 正文一字未改却回
     「已修改 N 个章节」并关闭冲突（问题被隐藏）。
  2. 落文循环不校验 `cursor.rowcount` → SELECT 与 UPDATE 之间章节被删除时
     同样假成功。
  修复后断言：`written_sections` 只算真实落库数；未命中记入 `skipped` 且
  冲突保持未解决；`scope_missing_section_ids` 显式暴露被项目过滤掉的来源。

TD-1（`consistency_rules.RESOLVED_SQL_OPEN` 单一事实来源）：
  `consistency_conflicts.resolved` 列可空（DEFAULT 0 但允许显式 NULL），
  裸 `resolved = 0` 因 SQL 三值逻辑把 NULL 行判为 false → 冲突在列表可见
  却对修复/回流/收敛链路隐形。现统一 `COALESCE(resolved, 0) = 0`，
  本文件钉住五条消费链路对 NULL 行的可见性：
  /conflicts 路由、AI 修复 `_load_context`、Agent `build_agent_workspace`、
  `refresh_conflict_states`、事实回流 `_open_conflicts`。

设计原则：不调用真实 AI；服务层直调为主，仅 /conflicts 用 TestClient。
"""
from __future__ import annotations

import pytest

from app.db import get_db, init_db
from app.services import consistency_auditor as auditor_mod
from app.services import consistency_rules as rules
from app.services import text_patch
from app.services.consistency_auditor import ConsistencyAuditor
from app.services.consistency_repair import _load_context, refresh_conflict_states
from app.services.consistency_backflow import _open_conflicts
from app.services.agent_repair import build_agent_workspace


OWNER = 3500


def _new_project(name: str = "autofix作用域测试") -> int:
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
    fact_name: str = "工期",
    values_json: str = '["45","50"]',
    sources_json: str = "[]",
    severity: str = "high",
    resolved=None,
) -> None:
    """注入冲突记录；`resolved=None` 显式写 NULL（TD-1 场景构造）。"""
    with get_db() as conn:
        if resolved is None:
            conn.execute(
                "INSERT INTO consistency_conflicts "
                "(project_id, conflict_id, fact_name, values_json, sources_json, "
                " severity, description, resolved, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, '测试冲突', NULL, '2026-11-02 10:00:00')",
                (project_id, conflict_id, fact_name, values_json, sources_json, severity),
            )
        else:
            conn.execute(
                "INSERT INTO consistency_conflicts "
                "(project_id, conflict_id, fact_name, values_json, sources_json, "
                " severity, description, resolved, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, '测试冲突', ?, '2026-11-02 10:00:00')",
                (project_id, conflict_id, fact_name, values_json, sources_json, severity, resolved),
            )


def _get_conflict_row(project_id: int, conflict_id: str):
    with get_db(readonly=True) as conn:
        return conn.execute(
            "SELECT resolved, resolved_value FROM consistency_conflicts "
            "WHERE project_id = ? AND conflict_id = ?",
            (project_id, conflict_id),
        ).fetchone()


def _get_section_content(section_id: int) -> str | None:
    with get_db(readonly=True) as conn:
        row = conn.execute(
            "SELECT content FROM sections WHERE id = ?", (section_id,)
        ).fetchone()
        return None if row is None else row["content"]


# ============================================================================
# F-1 · 场景A：来源章节属于其他项目 → 项目过滤后不写库、不关冲突
# ============================================================================


class TestAutofixCrossProjectScope:
    def test_cross_project_source_is_not_written(self):
        """sources_json 指向他项目章节：正文不改、written_sections=0、
        scope_missing_section_ids 记录、冲突保持未解决（F-1 假成功回归钉）。"""
        pid_a = _new_project("F1-项目A")
        pid_b = _new_project("F1-项目B")
        sec_b = _seed_section(pid_b, title="施工组织", content="本项目建设工期：45 天。")
        _seed_conflict(
            pid_a,
            conflict_id="conflict_f1_cross",
            sources_json=f'["section_{sec_b}"]',
            resolved=0,
        )

        auditor = ConsistencyAuditor()
        report = auditor.apply_autofix("conflict_f1_cross", pid_a, "50")

        assert report["success"] is False
        assert "未在正文中定位到待统一的值" in report["message"]
        assert report["written_sections"] == 0
        assert report["changed_sections"] == 0
        assert report["scope_missing_section_ids"] == [sec_b]
        # 他项目正文一个字没改
        assert _get_section_content(sec_b) == "本项目建设工期：45 天。"
        # 冲突没有被假成功关闭
        row = _get_conflict_row(pid_a, "conflict_f1_cross")
        assert int(row["resolved"]) == 0
        assert row["resolved_value"] in (None, "")

    def test_mixed_scope_writes_only_own_project(self):
        """来源同时含本项目与他项目章节：只写本项目，他项目进 scope_missing，
        本项目落库成功即整体成功。"""
        pid_a = _new_project("F1-混合A")
        pid_b = _new_project("F1-混合B")
        sec_a = _seed_section(pid_a, title="总体部署", content="本项目建设工期：45 天。")
        sec_b = _seed_section(pid_b, title="施工组织", content="建设工期：45 天。")
        _seed_conflict(
            pid_a,
            conflict_id="conflict_f1_mixed",
            sources_json=f'["section_{sec_a}", "section_{sec_b}"]',
            resolved=0,
        )

        report = ConsistencyAuditor().apply_autofix("conflict_f1_mixed", pid_a, "50")

        assert report["success"] is True
        assert report["written_sections"] == 1
        assert report["scope_missing_section_ids"] == [sec_b]
        assert "45" not in (_get_section_content(sec_a) or "")
        # 他项目章节仍保持原值
        assert _get_section_content(sec_b) == "建设工期：45 天。"

    def test_dry_run_never_writes(self):
        """dry_run：changed_sections 表示"将要改"，written_sections 恒为 0，不落库。"""
        pid = _new_project("F1-dryrun")
        sec = _seed_section(pid, title="工期章节", content="本项目建设工期：45 天。")
        _seed_conflict(
            pid, conflict_id="conflict_f1_dry",
            sources_json=f'["section_{sec}"]', resolved=0,
        )

        report = ConsistencyAuditor().apply_autofix("conflict_f1_dry", pid, "50", dry_run=True)

        assert report["success"] is True
        assert report["changed_sections"] == 1
        assert report["written_sections"] == 0
        assert _get_section_content(sec) == "本项目建设工期：45 天。"
        row = _get_conflict_row(pid, "conflict_f1_dry")
        assert int(row["resolved"]) == 0


# ============================================================================
# F-1 · 场景B：SELECT 与 UPDATE 之间章节被删除 → rowcount 校验拦住假成功
# ============================================================================


class TestAutofixRowcountGuard:
    def test_deleted_section_between_plan_and_update(self, monkeypatch):
        """竞态模拟：replace_values 计算成功后、UPDATE 前章节行被删。
        修复后 `cursor.rowcount` 校验应把该章节记入 skipped，
        written_sections=0、冲突保持未解决。"""
        pid = _new_project("F1-rowcount")
        sec = _seed_section(pid, title="工期章节", content="本项目建设工期：45 天。")
        _seed_conflict(
            pid, conflict_id="conflict_f1_rc",
            sources_json=f'["section_{sec}"]', resolved=0,
        )

        original = text_patch.replace_values
        calls = {"n": 0}

        def racing_replace(content, mapping, keep=None):
            result = original(content, mapping, keep=keep)
            calls["n"] += 1
            if result["changed"]:
                # 同线程嵌套 get_db()：拿到与 apply_autofix 外层同一个连接，
                # 模拟"计划生成后、落库前章节被并发删除"。
                with get_db() as conn:
                    conn.execute("DELETE FROM sections WHERE id = ?", (sec,))
            return result

        monkeypatch.setattr(text_patch, "replace_values", racing_replace)
        try:
            report = ConsistencyAuditor().apply_autofix("conflict_f1_rc", pid, "50")
        finally:
            monkeypatch.setattr(text_patch, "replace_values", original)

        assert calls["n"] >= 1
        assert report["written_sections"] == 0
        assert report["changed_sections"] == 0
        assert report["success"] is False
        assert any(
            item["section_id"] == sec and "UPDATE 未命中任何行" in item["reason"]
            for item in report["skipped"]
        )
        # 章节真被删了（构造生效），且冲突没被假成功关闭
        assert _get_section_content(sec) is None
        row = _get_conflict_row(pid, "conflict_f1_rc")
        assert int(row["resolved"]) == 0

    def test_partial_rowcount_failure_keeps_conflict_open(self, monkeypatch):
        """两来源章节只有一条真实落库：差额记 skipped，
        `sections`/`total_replacements` 口径同步剔除未写入项，冲突保持未解决。"""
        pid = _new_project("F1-partial")
        sec1 = _seed_section(pid, title="章节一", content="本项目建设工期：45 天。", sort_order=0)
        sec2 = _seed_section(pid, title="章节二", content="合同工期：45 天。", sort_order=1)
        _seed_conflict(
            pid, conflict_id="conflict_f1_part",
            sources_json=f'["section_{sec1}", "section_{sec2}"]', resolved=0,
        )

        original = text_patch.replace_values

        def delete_second(content, mapping, keep=None):
            result = original(content, mapping, keep=keep)
            if result["changed"]:
                with get_db() as conn:
                    conn.execute("DELETE FROM sections WHERE id = ?", (sec2,))
            return result

        monkeypatch.setattr(text_patch, "replace_values", delete_second)
        try:
            report = ConsistencyAuditor().apply_autofix("conflict_f1_part", pid, "50")
        finally:
            monkeypatch.setattr(text_patch, "replace_values", original)

        assert report["written_sections"] == 1
        assert report["changed_sections"] == 1
        # 逐章节明细只算真正写入的那条
        assert [s["section_id"] for s in report["sections"]] == [sec1]
        assert report["total_replacements"] == sum(
            s["replacements"] for s in report["sections"]
        )
        assert any(item["section_id"] == sec2 for item in report["skipped"])
        assert report["success"] is False
        row = _get_conflict_row(pid, "conflict_f1_part")
        assert int(row["resolved"]) == 0
        # 写成功的第一章节正文确实变了
        assert "50" in (_get_section_content(sec1) or "")


# ============================================================================
# TD-1 · NULL resolved 在五条链路的可见性（COALESCE 谓词单一事实来源）
# ============================================================================


class TestResolvedNullVisibility:
    """`resolved` 显式为 NULL 的未解决冲突必须在所有"待处理"链路可见。"""

    def _seed_null_conflict(self, pid: int) -> str:
        """注入 NULL resolved 行；`conflict_id` 全表唯一，按项目生成后缀。"""
        cid = f"conflict_td1_null_{pid}"
        _seed_conflict(
            pid, conflict_id=cid, values_json='["45","50"]',
            sources_json="[]", severity="high", resolved=None,
        )
        return cid

    def test_predicate_constant_is_coalesce(self):
        assert rules.RESOLVED_SQL_OPEN == "COALESCE(resolved, 0) = 0"
        assert rules.RESOLVED_SQL_RESOLVED == "COALESCE(resolved, 0) = 1"

    def test_conflicts_route_open_filter_includes_null(self):
        from fastapi.testclient import TestClient

        from app.main import app
        from app.security.auth import User, get_current_user

        pid = _new_project("TD1-路由")
        cid = self._seed_null_conflict(pid)
        try:
            app.dependency_overrides[get_current_user] = lambda: User(OWNER, "u", "user")
            client = TestClient(app)
            resp = client.get(f"/api/v1/consistency/conflicts/{pid}?resolved=open")
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == "ok"
            ids = [c["conflict_id"] for c in body["conflicts"]]
            assert cid in ids
            # 响应层把 NULL 归一化成 open / 0
            target = next(c for c in body["conflicts"] if c["conflict_id"] == cid)
            assert target["state"] == "open"
            assert target["resolved"] == 0
        finally:
            app.dependency_overrides.clear()

    def test_repair_load_context_includes_null(self):
        pid = _new_project("TD1-修复上下文")
        cid = self._seed_null_conflict(pid)
        ctx = _load_context(pid, None)
        assert any(c["conflict_id"] == cid for c in ctx["conflicts"])

    def test_agent_workspace_includes_null(self):
        pid = _new_project("TD1-Agent工作区")
        cid = self._seed_null_conflict(pid)
        ws = build_agent_workspace(pid, base=None)
        assert any(c["conflict_id"] == cid for c in ws["conflicts"])

    def test_refresh_conflict_states_sees_null(self):
        pid = _new_project("TD1-状态收敛")
        self._seed_null_conflict(pid)
        sec = _seed_section(pid, title="工期", content="建设工期 45 天，另有表述 50 天。")
        with get_db() as conn:
            states = refresh_conflict_states(conn, pid, {sec: "建设工期 45 天，另有表述 50 天。"})
        # 两个取值都还在 → 保持未解决（而不是因 NULL 行根本不被扫描而全 0）
        assert states["still_open"] >= 1
        assert set(states) == {"resolved", "still_open", "not_verifiable"}

    def test_backflow_open_conflicts_includes_null(self):
        pid = _new_project("TD1-事实回流")
        cid = self._seed_null_conflict(pid)
        with get_db(readonly=True) as conn:
            rows = _open_conflicts(conn, pid)
        assert any(r["conflict_id"] == cid for r in rows)
