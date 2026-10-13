"""2026-10-13 L2 加固：旁路写正文链路的「生成中」防护门。

覆盖：
  1. `services/task_guard.reject_if_generation_running` 单元行为
     （无锁放行 / 目录生成中 409 / 正文生成中内容类放行 / 正文生成中结构类 409）；
  2. 章节 CRUD 路由经薄包装保持既有 409 行为（不回归）；
  3. 三条旁路写正文路由（consistency autofix / AI repair / content_version restore）
     在目录生成中被 409 拒绝、无任务时放行。

锁的构造：直接向 task_lock 行镇锁写入活跃锁（与生产 TaskRegistry 的
sink 口径一致：`get_active_lock` + `is_lock_live` 只认行镇锁）。
"""

import time

import pytest
from fastapi import HTTPException

from app.services.task_guard import (
    live_group_lock,
    reject_if_generation_running,
)


def _new_project(name: str) -> int:
    from app.db import get_db

    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO projects (name, industry) VALUES (?, '通用')", (name,)
        )
        return cur.lastrowid


def _acquire_lock(project_id: int, task_type: str):
    """给项目上一把活跃的任务组锁（返回释放函数）。"""
    from app.services.task_lock import acquire, release

    rec = acquire(task_type, project_id=project_id, note="test-guard")
    assert rec.get("acquired"), f"测试锁获取失败: {rec}"
    lock_key = rec["lock_key"]
    owner_id = rec["owner_id"]

    def _release():
        release(lock_key, owner_id)

    return _release


class TestTaskGuardUnit:
    def test_no_lock_passes(self):
        pid = _new_project("守卫-无锁")
        # 不抛异常即放行
        reject_if_generation_running(pid)
        reject_if_generation_running(pid, allow_content_edit=True)

    def test_outline_generation_rejects_all(self):
        pid = _new_project("守卫-目录生成中")
        release = _acquire_lock(pid, "outline-generation")
        try:
            with pytest.raises(HTTPException) as ei:
                reject_if_generation_running(pid)
            assert ei.value.status_code == 409
            # 内容类写入口径在目录生成中同样拒绝（整表重建必然丢失）
            with pytest.raises(HTTPException) as ei2:
                reject_if_generation_running(pid, allow_content_edit=True)
            assert ei2.value.status_code == 409
        finally:
            release()
        # 释放后放行
        reject_if_generation_running(pid)

    def test_content_generation_allows_content_edit_only(self):
        pid = _new_project("守卫-正文生成中")
        release = _acquire_lock(pid, "content-generation")
        try:
            # 内容类写入放行（手写编辑 / autofix / 回滚同性质）
            reject_if_generation_running(pid, allow_content_edit=True)
            # 结构类写入仍拒绝
            with pytest.raises(HTTPException) as ei:
                reject_if_generation_running(pid)
            assert ei.value.status_code == 409
        finally:
            release()

    def test_live_group_lock_roundtrip(self):
        pid = _new_project("守卫-锁读取")
        assert live_group_lock(pid) is None
        release = _acquire_lock(pid, "outline-generation")
        try:
            lock = live_group_lock(pid)
            assert lock is not None
            assert lock.get("task_type") == "outline-generation"
        finally:
            release()
        assert live_group_lock(pid) is None


class TestBypassWriteRoutesGuarded:
    """旁路写正文路由的守卫（真实 TestClient 端到端）。

    API 前缀为 /api/v1（main.py 的 v1_router 挂载口径）。
    """

    API = "/api/v1"

    def _client(self):
        from fastapi.testclient import TestClient
        from app.main import app

        return TestClient(app)

    def _seed_section(self, project_id: int) -> int:
        from app.db import get_db

        with get_db() as conn:
            cur = conn.execute(
                "INSERT INTO sections (project_id, title, level, sort_order, content, status) "
                "VALUES (?, ?, 1, 1, ?, 'completed')",
                (project_id, "施工部署章", "旧正文"),
            )
            return cur.lastrowid

    def _seed_version(self, project_id: int, section_id: int) -> int:
        from app.db import get_db
        from app.services.collaboration import init_collaboration_tables

        # content_versions 表由协作模块建表（init_db 不含它），先确保存在
        init_collaboration_tables()
        with get_db() as conn:
            cur = conn.execute(
                "INSERT INTO content_versions (project_id, section_id, user_id, label, content, created_at) "
                "VALUES (?, ?, '', ?, ?, datetime('now', 'localtime'))",
                (project_id, section_id, "v1", "版本正文"),
            )
            return cur.lastrowid

    def test_restore_rejected_during_outline_generation(self):
        pid = _new_project("守卫-回滚拒绝")
        sid = self._seed_section(pid)
        vid = self._seed_version(pid, sid)
        release = _acquire_lock(pid, "outline-generation")
        try:
            client = self._client()
            resp = client.post(
                f"{self.API}/projects/{pid}/content-versions/restore",
                json={"version_id": vid},
            )
            assert resp.status_code == 409, resp.text
            # 正文未被改写
            from app.db import get_db
            with get_db() as conn:
                row = conn.execute(
                    "SELECT content FROM sections WHERE id = ?", (sid,)
                ).fetchone()
            assert row["content"] == "旧正文"
        finally:
            release()

    def test_restore_allowed_without_task(self):
        pid = _new_project("守卫-回滚放行")
        sid = self._seed_section(pid)
        vid = self._seed_version(pid, sid)
        client = self._client()
        resp = client.post(
            f"{self.API}/projects/{pid}/content-versions/restore",
            json={"version_id": vid},
        )
        assert resp.status_code == 200, resp.text
        from app.db import get_db
        with get_db() as conn:
            row = conn.execute(
                "SELECT content FROM sections WHERE id = ?", (sid,)
            ).fetchone()
        assert row["content"] == "版本正文", "无任务时回滚应正常落文"

    def test_autofix_rejected_during_outline_generation(self):
        """autofix 在目录生成中 409（不进入项目操作锁排队）。

        注意 URL 形态：`POST /consistency/conflicts/{conflict_id}/autofix`
        —— conflict_id 是路径参数，project_id 是 query 参数（与既有
        test_consistency_check_module 的调用口径一致）。
        """
        pid = _new_project("守卫-autofix拒绝")
        release = _acquire_lock(pid, "outline-generation")
        try:
            client = self._client()
            resp = client.post(
                f"{self.API}/consistency/conflicts/nonexistent/autofix",
                params={"project_id": pid, "new_value": "统一值"},
            )
            assert resp.status_code == 409, resp.text
        finally:
            release()

    def test_ai_repair_rejected_during_outline_generation(self):
        pid = _new_project("守卫-AI修复拒绝")
        release = _acquire_lock(pid, "outline-generation")
        try:
            client = self._client()
            resp = client.post(f"{self.API}/consistency/repair/{pid}")
            assert resp.status_code == 409, resp.text
        finally:
            release()


class TestSectionRoutesStillGuarded:
    """章节 CRUD 经薄包装保持既有 409 行为（不回归）。"""

    def test_create_section_rejected_during_outline_generation(self):
        from app.services.sections import create_section
        from app.models import SectionCreate

        pid = _new_project("守卫-章节CRUD")
        release = _acquire_lock(pid, "outline-generation")
        try:
            from app.routers.sections import _reject_if_generation_running
            with pytest.raises(HTTPException) as ei:
                _reject_if_generation_running(pid)
            assert ei.value.status_code == 409
            # 服务层函数本身未被守卫污染（守卫只在路由层）
            create_section(pid, SectionCreate(title="直接调用不受路由守卫约束", level=1))
        finally:
            release()
