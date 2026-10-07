"""审核与预检 / 导出文档模块 —— 组件级交互测试（ENHANCE 2026-09-29）。

覆盖本轮修复的三条主链：
  A. 项目属权校验助手（project_access）—— owner/foreign/missing/无归属/dict/APIKey 六态。
  B. IDOR 收口（P1）—— rejection / duplicate / consistency / word_count / export
     五条此前"只验登录不验归属"的路由，跨项目访问一律 403、不存在一律 404。
     这是本轮最重要的安全缺口：consistency autofix 能改写正文、export 能下载全文、
     rejection import 能读全文，此前任意登录用户换个 id 即可越权。
  C. 跨模块数据链 —— knowledge-suggestions 补齐 status 字段（前端以
     `res.status === "success"` 判定，旧后端不返回该键 → 知识库建议链路从未接通）。
  D. 导出内容指纹与 SECTION_EXPORT_COLUMNS 对齐 —— inlined_chart_json 入指纹，
     维持"任一导出列变化 → 指纹变 → 缓存失效"的不变量。

设计原则：直接调用路由协程函数并显式传入身份（不依赖真实 HTTP/AI/网络），
只测行为不测实现；断言状态码与关键返回字段。
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException

from app.db import get_db, init_db
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


def _seed_rejection_task(project_id: int | None) -> int:
    from app.services import rejection

    return rejection.create_task(project_id, "招标正文", [], [], ["rejection"])


def _seed_duplicate_task(project_id: int | None) -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO duplicate_checks (project_id, status, progress) VALUES (?, 'pending', 0)",
            (project_id,),
        )
        return cur.lastrowid


def _run(coro):
    return asyncio.run(coro)


def _status_of(fn, *args, **kwargs):
    """调用协程并断言其抛出 HTTPException，返回状态码。"""
    with pytest.raises(HTTPException) as exc:
        _run(fn(*args, **kwargs))
    return exc.value


# ============================================================
# A. 项目属权校验助手
# ============================================================


class TestProjectAccessHelper:
    def test_owner_passes(self):
        from app.routers.project_access import enforce_project_access

        pid = _new_project("助手-属主", created_by=OWNER)
        # 不抛异常即通过
        _run(enforce_project_access(pid, User(OWNER, "owner", "user")))

    def test_stranger_rejected_403(self):
        from app.routers.project_access import enforce_project_access

        pid = _new_project("助手-越权", created_by=OWNER)
        exc = _status_of(enforce_project_access, pid, User(STRANGER, "other", "user"))
        assert exc.status_code == 403

    def test_missing_project_404(self):
        from app.routers.project_access import enforce_project_access

        exc = _status_of(enforce_project_access, 999999, User(OWNER, "owner", "user"))
        assert exc.status_code == 404

    def test_legacy_project_without_owner_passes(self):
        """向后兼容：created_by 为 NULL 的历史项目放行（与 sections 同口径）。"""
        from app.routers.project_access import enforce_project_access

        pid = _new_project("助手-历史无主", created_by=None)
        _run(enforce_project_access(pid, User(STRANGER, "other", "user")))

    def test_dict_identity_is_converted(self):
        """JWT 中间件把用户塞成 dict {'id',...}，助手须能识别并鉴权。"""
        from app.routers.project_access import enforce_project_access

        pid = _new_project("助手-dict属主", created_by=OWNER)
        _run(enforce_project_access(pid, {"id": OWNER, "username": "o", "role": "user"}))
        exc = _status_of(enforce_project_access, pid, {"id": STRANGER, "username": "s", "role": "user"})
        assert exc.status_code == 403

    def test_api_key_identity_without_user_id_passes(self):
        """API Key 身份（无用户 id）保持既有开放语义，不做项目级拦截。"""
        from app.routers.project_access import enforce_project_access

        pid = _new_project("助手-apikey", created_by=OWNER)
        _run(enforce_project_access(pid, {"key_id": 7, "name": "k", "permissions": []}))

    def test_task_access_traverses_to_project(self):
        from app.routers.project_access import enforce_task_access

        pid = _new_project("助手-任务鉴权", created_by=OWNER)
        tid = _seed_rejection_task(pid)
        # 属主通过
        assert _run(enforce_task_access("rejection_checks", tid, User(OWNER, "o", "user"))) == pid
        # 越权 403
        exc = _status_of(enforce_task_access, "rejection_checks", tid, User(STRANGER, "s", "user"))
        assert exc.status_code == 403

    def test_task_access_missing_task_404(self):
        from app.routers.project_access import enforce_task_access

        exc = _status_of(enforce_task_access, "rejection_checks", 987654, User(OWNER, "o", "user"))
        assert exc.status_code == 404


# ============================================================
# B. IDOR 收口：五条路由跨项目一律拒绝
# ============================================================


class TestRejectionRouteGuards:
    def test_get_status_foreign_403_and_owner_ok(self):
        from app.routers.rejection import get_status

        pid = _new_project("废标-状态鉴权", created_by=OWNER)
        tid = _seed_rejection_task(pid)
        exc = _status_of(get_status, tid, user=User(STRANGER, "s", "user"))
        assert exc.status_code == 403
        result = _run(get_status(tid, user=User(OWNER, "o", "user")))
        assert result["id"] == tid

    def test_get_result_foreign_403(self):
        from app.routers.rejection import get_result

        pid = _new_project("废标-结果鉴权", created_by=OWNER)
        tid = _seed_rejection_task(pid)
        exc = _status_of(get_result, tid, user=User(STRANGER, "s", "user"))
        assert exc.status_code == 403

    def test_list_tasks_foreign_project_403(self):
        from app.routers.rejection import list_tasks

        pid = _new_project("废标-列表鉴权", created_by=OWNER)
        exc = _status_of(list_tasks, pid, 20, user=User(STRANGER, "s", "user"))
        assert exc.status_code == 403

    def test_project_tasks_foreign_403(self):
        from app.routers.rejection import get_project_tasks

        pid = _new_project("废标-项目任务鉴权", created_by=OWNER)
        exc = _status_of(get_project_tasks, pid, 100, user=User(STRANGER, "s", "user"))
        assert exc.status_code == 403

    def test_import_from_project_foreign_403(self):
        from app.routers.rejection import import_from_project

        pid = _new_project("废标-导入鉴权", created_by=OWNER)
        exc = _status_of(import_from_project, pid, user=User(STRANGER, "s", "user"))
        assert exc.status_code == 403

    def test_batch_delete_rejects_whole_batch_on_foreign(self):
        """批量删除：任一任务越权即整批 403，不做部分删除。"""
        from app.models import BatchTaskDeleteRequest
        from app.routers.rejection import batch_delete_tasks

        pid = _new_project("废标-批量删除鉴权", created_by=OWNER)
        tid = _seed_rejection_task(pid)
        req = BatchTaskDeleteRequest(task_ids=[tid])
        exc = _status_of(batch_delete_tasks, req, user=User(STRANGER, "s", "user"))
        assert exc.status_code == 403
        # 任务仍在（未被删）
        with get_db(readonly=True) as conn:
            assert conn.execute("SELECT 1 FROM rejection_checks WHERE id = ?", (tid,)).fetchone()


class TestDuplicateRouteGuards:
    def test_get_status_foreign_403(self):
        from app.routers.duplicate import get_status

        pid = _new_project("查重-状态鉴权", created_by=OWNER)
        tid = _seed_duplicate_task(pid)
        exc = _status_of(get_status, tid, user=User(STRANGER, "s", "user"))
        assert exc.status_code == 403

    def test_list_tasks_foreign_project_403(self):
        from app.routers.duplicate import list_tasks

        pid = _new_project("查重-列表鉴权", created_by=OWNER)
        exc = _status_of(list_tasks, 20, pid, user=User(STRANGER, "s", "user"))
        assert exc.status_code == 403


class TestConsistencyRouteGuards:
    def test_get_conflicts_foreign_403(self):
        from app.routers.consistency import get_conflicts

        pid = _new_project("一致性-冲突鉴权", created_by=OWNER)
        exc = _status_of(get_conflicts, pid, user=User(STRANGER, "s", "user"))
        assert exc.status_code == 403

    def test_autofix_foreign_403_before_touching_content(self):
        """autofix 能改写正文 —— 越权必须在落文之前就被拒。"""
        from app.routers.consistency import apply_autofix

        pid = _new_project("一致性-自动修复鉴权", created_by=OWNER)
        exc = _status_of(
            apply_autofix, "c-1", pid, new_value="450", dry_run=True, user=User(STRANGER, "s", "user")
        )
        assert exc.status_code == 403

    def test_summary_foreign_403(self):
        from app.routers.consistency import get_audit_summary

        pid = _new_project("一致性-摘要鉴权", created_by=OWNER)
        exc = _status_of(get_audit_summary, pid, user=User(STRANGER, "s", "user"))
        assert exc.status_code == 403


class TestWordCountRouteGuards:
    def test_overview_foreign_403(self):
        from app.routers.word_count import overview

        pid = _new_project("字数-概览鉴权", created_by=OWNER)
        exc = _status_of(overview, pid, user=User(STRANGER, "s", "user"))
        assert exc.status_code == 403

    def test_section_stats_foreign_403(self):
        from app.routers.word_count import section_stats

        pid = _new_project("字数-明细鉴权", created_by=OWNER)
        exc = _status_of(section_stats, pid, user=User(STRANGER, "s", "user"))
        assert exc.status_code == 403

    def test_owner_passes(self):
        from app.routers.word_count import overview

        pid = _new_project("字数-属主通过", created_by=OWNER)
        result = _run(overview(pid, user=User(OWNER, "o", "user")))
        assert result["status"] == "ok" and result["project_id"] == pid


class TestExportRouteGuards:
    def test_check_foreign_403(self):
        from app.routers.export import export_check

        pid = _new_project("导出-预检鉴权", created_by=OWNER)
        exc = _status_of(export_check, pid, False, identity=User(STRANGER, "s", "user"))
        assert exc.status_code == 403

    def test_history_foreign_403(self):
        from app.routers.export import export_history

        pid = _new_project("导出-历史鉴权", created_by=OWNER)
        exc = _status_of(export_history, pid, identity=User(STRANGER, "s", "user"))
        assert exc.status_code == 403

    def test_download_foreign_403(self):
        from app.routers.export import download_export

        pid = _new_project("导出-下载鉴权", created_by=OWNER)
        exc = _status_of(download_export, pid, 1, identity=User(STRANGER, "s", "user"))
        assert exc.status_code == 403


# ============================================================
# C. 跨模块数据链：知识库建议 status 字段
# ============================================================


class TestKnowledgeSuggestionsContract:
    def test_response_carries_success_status_for_frontend_gate(self):
        """前端以 res.status === "success" 判定，缺该键则建议链路永不触发。"""
        from app.routers.rejection import get_knowledge_suggestions

        pid = _new_project("知识库-建议契约", created_by=OWNER)
        result = _run(get_knowledge_suggestions(pid, user=User(OWNER, "o", "user")))
        assert result["status"] == "success", "必须回 status，否则前端加载知识库建议按钮恒不出现"
        assert "suggestions" in result and isinstance(result["suggestions"], list)

    def test_foreign_project_rejected_before_kb_query(self):
        from app.routers.rejection import get_knowledge_suggestions

        pid = _new_project("知识库-鉴权", created_by=OWNER)
        exc = _status_of(get_knowledge_suggestions, pid, user=User(STRANGER, "s", "user"))
        assert exc.status_code == 403

    def test_search_items_with_keyword_does_not_crash(self):
        """锁定 SQL 修复：带关键字的知识检索不得再因 ESCAPE 语法错抛异常，
        且命中项能真正返回（证明知识库 → 废标建议的数据链打通）。"""
        from app.services.knowledge import search_items

        with get_db() as conn:
            cur = conn.execute(
                "INSERT INTO knowledge_items (title, content, summary) VALUES (?, ?, ?)",
                ("废标条款：未提交保证金", "投标文件未提交保证金的按废标处理", "废标"),
            )
            kid = cur.lastrowid
        result = search_items(keyword="废标", limit=10)
        assert isinstance(result["items"], list)
        assert any(i["id"] == kid for i in result["items"]), "含关键字的条目应被检索到"
        # 通配符转义：把 % 当普通字符查，不应因 LIKE 通配而误命中全部
        assert search_items(keyword="100%不存在", limit=10)["items"] == []

    def test_endpoint_returns_seeded_suggestion(self):
        """端到端：路由把命中的知识条目作为 suggestions 回给前端。"""
        from app.routers.rejection import get_knowledge_suggestions

        with get_db() as conn:
            conn.execute(
                "INSERT INTO knowledge_items (title, content, summary) VALUES (?, ?, ?)",
                ("废标项：工期承诺低于最低要求", "投标人承诺工期少于招标要求的按废标", "废标"),
            )
        pid = _new_project("知识库-端到端", created_by=OWNER)
        result = _run(get_knowledge_suggestions(pid, user=User(OWNER, "o", "user")))
        assert result["status"] == "success"
        assert result["suggestions"], "命中数据时 suggestions 应非空"


# ============================================================
# D. 导出内容指纹与导出列对齐
# ============================================================


class TestContentFingerprintCoversExportColumns:
    def _seed_section(self, pid: int) -> int:
        with get_db() as conn:
            cur = conn.execute(
                "INSERT INTO sections (project_id, title, level, sort_order, content, status) "
                "VALUES (?, '第一章', 1, 0, '正文内容。', 'completed')",
                (pid,),
            )
            return cur.lastrowid

    def test_fingerprint_changes_when_inlined_chart_json_changes(self):
        """inlined_chart_json 在 SECTION_EXPORT_COLUMNS 内，指纹必须随之变化，
        否则将来内联图渲染消费该列后，改图却命中旧缓存。"""
        from app.services.export import _compute_content_fingerprint

        pid = _new_project("指纹-内联图", created_by=OWNER)
        sid = self._seed_section(pid)

        before = _compute_content_fingerprint(pid)
        with get_db() as conn:
            conn.execute(
                "UPDATE sections SET inlined_chart_json = ? WHERE id = ?",
                ('{"chart":"gantt","data":[1,2,3]}', sid),
            )
        after = _compute_content_fingerprint(pid)

        assert before and after
        assert before != after, "改了导出相关列，指纹必须变（缓存随之失效）"

    def test_fingerprint_is_importable_and_stable(self):
        from app.services.export import _compute_content_fingerprint

        pid = _new_project("指纹-稳定", created_by=OWNER)
        self._seed_section(pid)
        assert _compute_content_fingerprint(pid) == _compute_content_fingerprint(pid)
