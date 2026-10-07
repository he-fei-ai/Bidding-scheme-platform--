"""目录生成 → 正文生成跨模块缓存断点修复 —— 组件级交互测试（BUG-FIX 2026-09-29）。

缺陷：正文生成入口 `_fetch_project_data_async` 优先读 Redis 整包章节快照
（`SECTION_CONTENT:project:{id}`，TTL 30 分钟），但目录全量重建
（`_clear_and_save_outline`：DELETE + 重建，章节 id 全部变化）与章节 CRUD /
移动 / 重排 / 清空只失效进程内 build_tree（60s），从不失效该快照 ——
重建后 30 分钟内再跑正文生成，拿到的是**旧章节 id 的快照**，生成结果
UPDATE 到已不存在的行，正文静默丢失且无任何报错。

修复：结构/内容变更统一走 `services.sections.invalidate_content_caches`
（build_tree + Redis 快照 + 单章内存缓存三层）。本测试固化这条不变量：
**任何章节结构写操作之后，整包快照必须已失效**。
"""
from __future__ import annotations

from app.db import get_db, init_db
from app.models import SectionCreate, SectionUpdate
from app.services import sections as sections_service
from app.services.redis_cache import CacheService


def _new_project(name: str) -> int:
    init_db()
    with get_db() as conn:
        cur = conn.execute("INSERT INTO projects (name, industry) VALUES (?, '通用')", (name,))
        return cur.lastrowid


def _seed_section(pid: int, title: str, content: str = "") -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO sections (project_id, title, level, sort_order, content, status) "
            "VALUES (?, ?, 1, 1, ?, 'completed')",
            (pid, title, content),
        )
        return cur.lastrowid


def _stale_snapshot(pid: int) -> None:
    """模拟正文生成此前写入的整包快照（旧章节形态）。"""
    CacheService.set_project_sections(pid, [{"id": -999999, "title": "旧快照"}])


def _snapshot_polluted(pid: int) -> bool:
    cached = CacheService.get_project_sections(pid)
    return bool(cached) and any(
        isinstance(item, dict) and item.get("id") == -999999 for item in cached
    )


class TestStructureWritesInvalidateSnapshot:
    def test_create_section_invalidates(self):
        pid = _new_project("缓存-新增章节")
        _stale_snapshot(pid)
        sections_service.create_section(pid, SectionCreate(title="新章", level=1, sort_order=1))
        assert not _snapshot_polluted(pid), "create_section 后整包快照仍为旧数据"

    def test_update_section_invalidates(self):
        pid = _new_project("缓存-更新章节")
        sid = _seed_section(pid, "章一", "旧正文")
        _stale_snapshot(pid)
        sections_service.update_section(sid, SectionUpdate(content="新正文"))
        assert not _snapshot_polluted(pid), "update_section 后整包快照仍为旧数据"

    def test_delete_section_invalidates(self):
        pid = _new_project("缓存-删除章节")
        sid = _seed_section(pid, "章一")
        _stale_snapshot(pid)
        result = sections_service.delete_section(sid)
        assert result["success"] is True
        assert not _snapshot_polluted(pid), "delete_section 后整包快照仍为旧数据"

    def test_clear_project_sections_invalidates(self):
        pid = _new_project("缓存-清空章节")
        _seed_section(pid, "章一")
        _stale_snapshot(pid)
        sections_service.clear_project_sections(pid)
        assert not _snapshot_polluted(pid), "clear_project_sections 后整包快照仍为旧数据"

    def test_batch_create_invalidates(self):
        pid = _new_project("缓存-批量创建")
        _stale_snapshot(pid)
        sections_service.batch_create_sections(pid, [{"title": "A"}, {"title": "B"}])
        assert not _snapshot_polluted(pid), "batch_create_sections 后整包快照仍为旧数据"

    def test_outline_rebuild_invalidates(self):
        """核心场景：目录全量重建（章节 id 全变）后，快照必须失效。"""
        from app.routers.sse.sse_db_ops import _clear_and_save_outline

        pid = _new_project("缓存-目录重建")
        old_sid = _seed_section(pid, "旧第一章", "耗时生成的正文")
        _stale_snapshot(pid)
        saved = _clear_and_save_outline(pid, [{"title": "新第一章", "children": []}])
        assert saved, "目录重建应返回带新 id 的节点"
        new_sid = saved[0]["id"]
        assert new_sid != old_sid
        assert not _snapshot_polluted(pid), "_clear_and_save_outline 后整包快照仍为旧数据"

    def test_outline_rebuild_preserves_content_snapshot(self):
        """回归保护：同名同层级重建仍回填旧正文（ENHANCE 2026-09-18 数据保全）。"""
        from app.routers.sse.sse_db_ops import _clear_and_save_outline

        pid = _new_project("缓存-重建保正文")
        _seed_section(pid, "稳定的章", "重要正文")
        saved = _clear_and_save_outline(pid, [{"title": "稳定的章", "children": []}])
        with get_db() as conn:
            row = conn.execute(
                "SELECT content FROM sections WHERE id = ?", (saved[0]["id"],)
            ).fetchone()
        assert row and row["content"] == "重要正文"
