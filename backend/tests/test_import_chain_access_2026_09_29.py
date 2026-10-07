"""链首三路由（files / ai / projects）项目属权收口 —— 组件级交互测试（ENHANCE 2026-09-29）。

背景（P1 IDOR 补齐链首）：
  sections / facts / content_version / tasks 与审核四路由、导出路由早已接入
  `require_project_permission` / `enforce_project_access`，但七模块数据链的
  **链首** —— 上传解析（files）、提取项目（ai/analysis）、项目本身（projects）
  仍只验「已登录」不验「项目归属」：任意登录用户可向他人项目上传/删除文件、
  覆写摘要/评分点/全局事实/BOQ 分析（下游目录、正文、审计、导出全部被污染），
  或按 id 读改删他人项目。

本测试直接调用路由协程函数并显式传入身份（不依赖真实 HTTP/AI/网络），
覆盖六态：owner 放行 / stranger 403 / 项目不存在 404 / 历史无主项目放行 /
dict 身份识别 / 缺省 current_user=None 不误伤（开发模式语义）。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from app.db import get_db, init_db
from app.models import ProjectUpdate
from app.security.auth import User

OWNER = 100
STRANGER = 200


def _new_project(name: str, created_by: int | None = OWNER) -> int:
    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO projects (name, industry, created_by) VALUES (?, ?, ?)",
            (name, "通用", created_by),
        )
        return conn.execute("SELECT id FROM projects ORDER BY id DESC LIMIT 1").fetchone()["id"]


def _run(coro):
    return asyncio.run(coro)


def _status_of(fn, *args, **kwargs):
    """调用协程并断言其抛出 HTTPException，返回异常对象。"""
    with pytest.raises(HTTPException) as exc:
        _run(fn(*args, **kwargs))
    return exc.value


# ============================================================
# A. 上传解析路由（files）
# ============================================================


class TestFilesRouterAccess:
    def test_stranger_upload_403(self):
        from app.routers import files as files_mod

        pid = _new_project("files-越权上传", created_by=OWNER)
        exc = _status_of(files_mod.upload_file, pid, None, "", User(STRANGER, "other", "user"))
        assert exc.status_code == 403

    def test_stranger_list_403(self):
        from app.routers import files as files_mod

        pid = _new_project("files-越权列表", created_by=OWNER)
        exc = _status_of(files_mod.list_files, pid, User(STRANGER, "other", "user"))
        assert exc.status_code == 403

    def test_stranger_latest_text_403(self):
        from app.routers import files as files_mod

        pid = _new_project("files-越权文本", created_by=OWNER)
        exc = _status_of(files_mod.get_latest_text, pid, User(STRANGER, "other", "user"))
        assert exc.status_code == 403

    def test_stranger_file_text_403(self):
        from app.routers import files as files_mod

        pid = _new_project("files-越权单文件", created_by=OWNER)
        exc = _status_of(files_mod.get_file_text, pid, 1, User(STRANGER, "other", "user"))
        assert exc.status_code == 403

    def test_stranger_delete_403(self):
        from app.routers import files as files_mod

        pid = _new_project("files-越权删除", created_by=OWNER)
        exc = _status_of(files_mod.delete_uploaded_file, pid, 1, User(STRANGER, "other", "user"))
        assert exc.status_code == 403

    def test_stranger_re_extract_403(self):
        from app.routers import files as files_mod

        pid = _new_project("files-越权重提取", created_by=OWNER)
        exc = _status_of(files_mod.re_extract_file_text, pid, 1, User(STRANGER, "other", "user"))
        assert exc.status_code == 403

    def test_owner_list_passes(self, monkeypatch):
        """属主正常放行：走到服务层并返回其结果。"""
        from app.routers import files as files_mod

        pid = _new_project("files-属主列表", created_by=OWNER)
        monkeypatch.setattr(files_mod.file_service, "get_project_files", lambda p: [{"id": 1}])
        assert _run(files_mod.list_files(pid, User(OWNER, "owner", "user"))) == [{"id": 1}]

    def test_missing_project_404(self):
        from app.routers import files as files_mod

        exc = _status_of(files_mod.list_files, 999999, User(OWNER, "owner", "user"))
        assert exc.status_code == 404

    def test_legacy_project_passes(self, monkeypatch):
        """向后兼容：created_by 为 NULL 的历史项目放行。"""
        from app.routers import files as files_mod

        pid = _new_project("files-历史无主", created_by=None)
        monkeypatch.setattr(files_mod.file_service, "get_project_files", lambda p: [])
        assert _run(files_mod.list_files(pid, User(STRANGER, "other", "user"))) == []


# ============================================================
# B. 提取项目路由（ai / analysis）
# ============================================================


class TestAiRouterAccess:
    def test_stranger_get_analysis_403(self):
        from app.routers import ai as ai_mod

        pid = _new_project("ai-越权读分析", created_by=OWNER)
        exc = _status_of(ai_mod.get_analysis, pid, User(STRANGER, "other", "user"))
        assert exc.status_code == 403

    def test_stranger_clear_analysis_403(self):
        from app.routers import ai as ai_mod

        pid = _new_project("ai-越权清分析", created_by=OWNER)
        exc = _status_of(ai_mod.clear_analysis, pid, User(STRANGER, "other", "user"))
        assert exc.status_code == 403
        # 越权请求不得触达写库：analysis 行不应被创建
        with get_db() as conn:
            assert conn.execute("SELECT 1 FROM analysis WHERE project_id = ?", (pid,)).fetchone() is None

    def test_stranger_scope_403(self):
        from app.routers import ai as ai_mod

        pid = _new_project("ai-越权scope", created_by=OWNER)
        exc = _status_of(ai_mod.save_project_scope, pid, ai_mod.ScopeUpdate(scope="x"), User(STRANGER, "other", "user"))
        assert exc.status_code == 403

    def test_stranger_summary_403_before_ai(self):
        """摘要生成在 AI 调用前即被属权校验拦截（不烧陌生人配额）。"""
        from app.routers import ai as ai_mod

        pid = _new_project("ai-越权摘要", created_by=OWNER)
        exc = _status_of(ai_mod.generate_summary, pid, User(STRANGER, "other", "user"))
        assert exc.status_code == 403

    def test_stranger_scoring_403(self):
        from app.routers import ai as ai_mod

        pid = _new_project("ai-越权评分", created_by=OWNER)
        exc = _status_of(ai_mod.generate_scoring, pid, User(STRANGER, "other", "user"))
        assert exc.status_code == 403

    def test_stranger_confirm_summary_403(self):
        from app.routers import ai as ai_mod

        pid = _new_project("ai-越权确认摘要", created_by=OWNER)
        exc = _status_of(ai_mod.confirm_summary, pid, ai_mod.SummaryConfirm(summary_json="{}"), User(STRANGER, "other", "user"))
        assert exc.status_code == 403

    def test_stranger_confirm_scoring_403(self):
        from app.routers import ai as ai_mod

        pid = _new_project("ai-越权确认评分", created_by=OWNER)
        exc = _status_of(ai_mod.confirm_scoring, pid, ai_mod.ScoringConfirm(scoring_points_json="[]"), User(STRANGER, "other", "user"))
        assert exc.status_code == 403

    @pytest.mark.parametrize("fn_name", ["analyze_boq", "analyze_work_interface", "analyze_construction_method", "confirm_boq", "confirm_work_interface", "confirm_construction_method"])
    def test_stranger_boq_family_403(self, fn_name):
        from app.routers import ai as ai_mod

        pid = _new_project(f"ai-越权-{fn_name}", created_by=OWNER)
        exc = _status_of(getattr(ai_mod, fn_name), pid, User(STRANGER, "other", "user"))
        assert exc.status_code == 403

    def test_owner_get_analysis_default_shape(self):
        """属主放行且返回结构与既有契约一致（空库默认形态）。"""
        from app.routers import ai as ai_mod

        pid = _new_project("ai-属主读分析", created_by=OWNER)
        result = _run(ai_mod.get_analysis(pid, User(OWNER, "owner", "user")))
        assert result["summary_json"] == ""
        assert result["scoring_points_json"] == "[]"
        assert "global_facts_items" in result


# ============================================================
# C. 项目路由（projects 按 id 的读改删）
# ============================================================


class TestProjectsRouterAccess:
    def test_stranger_get_403(self):
        from app.routers import projects as projects_mod

        pid = _new_project("proj-越权读", created_by=OWNER)
        exc = _status_of(projects_mod.get_project, pid, User(STRANGER, "other", "user"))
        assert exc.status_code == 403

    def test_stranger_update_403(self):
        from app.routers import projects as projects_mod

        pid = _new_project("proj-越权改", created_by=OWNER)
        exc = _status_of(projects_mod.update_project, pid, ProjectUpdate(name="入侵"), User(STRANGER, "other", "user"))
        assert exc.status_code == 403
        with get_db() as conn:
            row = conn.execute("SELECT name FROM projects WHERE id = ?", (pid,)).fetchone()
        assert row["name"] == "proj-越权改"  # 越权请求不得改写

    def test_stranger_delete_403(self):
        from app.routers import projects as projects_mod

        pid = _new_project("proj-越权删", created_by=OWNER)
        exc = _status_of(projects_mod.delete_project, pid, User(STRANGER, "other", "user"))
        assert exc.status_code == 403
        with get_db() as conn:
            assert conn.execute("SELECT 1 FROM projects WHERE id = ?", (pid,)).fetchone() is not None

    def test_owner_get_passes(self):
        from app.routers import projects as projects_mod

        pid = _new_project("proj-属主读", created_by=OWNER)
        result = _run(projects_mod.get_project(pid, User(OWNER, "owner", "user")))
        assert result["id"] == pid
        assert result["name"] == "proj-属主读"

    def test_dict_identity_is_converted(self):
        """JwtAuthMiddleware dict 身份同样受属权约束。"""
        from app.routers import projects as projects_mod

        pid = _new_project("proj-dict越权", created_by=OWNER)
        exc = _status_of(projects_mod.get_project, pid, {"id": STRANGER, "username": "s", "role": "user"})
        assert exc.status_code == 403

    def test_no_user_identity_legacy_passes(self):
        """current_user=None（无身份信息）与 sections 对「无创建者记录」一致：不误伤。"""
        from app.routers import projects as projects_mod

        pid = _new_project("proj-无身份", created_by=OWNER)
        result = _run(projects_mod.get_project(pid, None))
        assert result["id"] == pid


# ============================================================
# D. 清除/覆盖事实后旧审计同步作废（提取项目 → 全局事实口径）
# ============================================================


class TestFactsAuditInvalidation:
    def _seed_audit(self, pid: int) -> None:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO analysis (project_id, global_facts_json, confirmed_facts, "
                "facts_audit_json, facts_audit_at) VALUES (?, '[]', 1, '{\"status\":\"ok\"}', datetime('now','localtime')) "
                "ON CONFLICT(project_id) DO UPDATE SET global_facts_json='[]', confirmed_facts=1, "
                "facts_audit_json='{\"status\":\"ok\"}', facts_audit_at=datetime('now','localtime')",
                (pid,),
            )

    def _read_audit(self, pid: int):
        with get_db() as conn:
            row = conn.execute("SELECT facts_audit_json, facts_audit_at FROM analysis WHERE project_id = ?", (pid,)).fetchone()
        return (row["facts_audit_json"], row["facts_audit_at"]) if row else (None, None)

    def test_clear_analysis_voids_old_audit(self):
        from app.routers.ai import _sync_clear_analysis

        pid = _new_project("审计-清除分析", created_by=OWNER)
        self._seed_audit(pid)
        _sync_clear_analysis(pid)
        assert self._read_audit(pid) == ("", None)

    def test_reset_helper_idempotent_without_row(self):
        """无 analysis 行时不报错、不建脏行。"""
        from app.routers.ai import _sync_reset_facts_audit

        pid = _new_project("审计-无行", created_by=OWNER)
        _sync_reset_facts_audit(pid)  # 不应抛异常
        with get_db() as conn:
            assert conn.execute("SELECT 1 FROM analysis WHERE project_id = ?", (pid,)).fetchone() is None

    def test_get_analysis_default_shape_has_all_keys(self):
        """无行/有行两分支返回键集一致（BUG-FIX 2026-09-29 形态统一）。"""
        from app.routers import ai as ai_mod

        pid = _new_project("分析-默认形态", created_by=OWNER)
        empty = _run(ai_mod.get_analysis(pid, User(OWNER, "owner", "user")))
        with get_db() as conn:
            conn.execute("INSERT INTO analysis (project_id, summary_json) VALUES (?, '{}')", (pid,))
        filled = _run(ai_mod.get_analysis(pid, User(OWNER, "owner", "user")))
        for key in ("global_facts_items", "facts_policy", "scope", "confirmed_facts", "global_facts_json"):
            assert key in empty, f"无行分支缺键 {key}"
            assert key in filled, f"有行分支缺键 {key}"
