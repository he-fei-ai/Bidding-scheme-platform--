"""回归测试（2026-09-16）：性能瓶颈接线修复

覆盖的缺陷与改动文件：
1. B-W1  services/sections.py::get_section_path —— perf_optimizations.build_sort_lookup
        已实现但从未接线，排序键内 next(...) 线性扫描导致单父节点 N 子章节时
        键计算 O(N²)。已改为一次遍历预建 O(1) 查找表。
2. B-W2  services/sections.py::move_section / reorder_sections —— 逐行 UPDATE
        改为 executemany 批量执行（行为不变）。
3. B-W3  services/sections.py::validate_outline_integrity —— 环检测加记忆化，
        链状大纲 O(N·depth)（最坏 O(N²)）→ O(N)，报错语义保持一致。
4. B-W4  routers/files.py::list_files / get_file_text / delete_uploaded_file ——
        同步 DB/文件 I/O 包装 asyncio.to_thread，避免生成高峰期阻塞事件循环。

测试原则：只测行为（正确性 + 规模化性能护栏），不测实现细节。
"""

import asyncio
import sqlite3
import time

import pytest
from fastapi import HTTPException

from app.config import DATABASE_PATH
from app.db import get_db
from app.models import ProjectCreate, SectionCreate
from app.routers import files as files_router
from app.services import files as file_service
from app.services import sections as sections_service
from app.services.projects import create_project

# 本测试文件创建的项目 id，teardown 时统一清理。
# 必须用「关外键的独立连接」直接删除：本文件会构造 2000 节点深链，
# 若走外键级联删除（ON DELETE CASCADE 逐层递归），会超过 SQLite
# 触发器递归深度上限（~1000 层），抛 "too many levels of trigger recursion"
# 并污染同库后续所有测试（如 test_performance_optimizations.py 的 DELETE FROM sections）。
_created_project_ids: list[int] = []


@pytest.fixture(autouse=True)
def _cleanup_created_projects():
    yield
    if not _created_project_ids:
        return
    conn = sqlite3.connect(str(DATABASE_PATH))
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        placeholders = ",".join("?" for _ in _created_project_ids)
        conn.execute(f"DELETE FROM sections WHERE project_id IN ({placeholders})", _created_project_ids)
        conn.execute(f"DELETE FROM projects WHERE id IN ({placeholders})", _created_project_ids)
        conn.commit()
    finally:
        conn.close()
        _created_project_ids.clear()


def _set_parent_ignoring_fk(section_id: int, parent_id: int) -> None:
    """用独立连接临时关闭外键构造异常数据（悬空父节点/成环）"""
    conn = sqlite3.connect(str(DATABASE_PATH))
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("UPDATE sections SET parent_id = ? WHERE id = ?", (parent_id, section_id))
        conn.commit()
    finally:
        conn.close()


def _make_project() -> int:
    pid = create_project(ProjectCreate(name="perf-bottleneck 回归测试项目")).id
    _created_project_ids.append(pid)
    return pid


def _add(pid: int, title: str, parent_id=None, level=1, sort_order=0) -> int:
    return sections_service.create_section(
        pid, SectionCreate(title=title, parent_id=parent_id, level=level, sort_order=sort_order)
    ).id


# ============================================================================
# B-W1: get_section_path —— 排序键 O(1) 查找表
# ============================================================================


class TestGetSectionPath:
    def test_path_follows_sort_order_not_creation_order(self):
        """子章节按 sort_order（而非创建顺序）编号，验证查找表排序正确性"""
        pid = _make_project()
        root = _add(pid, "第一章", sort_order=1)
        c3 = _add(pid, "丙节", parent_id=root, level=2, sort_order=3)
        c1 = _add(pid, "甲节", parent_id=root, level=2, sort_order=1)
        c2 = _add(pid, "乙节", parent_id=root, level=2, sort_order=2)

        assert sections_service.get_section_path(c1) == "1.1"
        assert sections_service.get_section_path(c2) == "1.2"
        assert sections_service.get_section_path(c3) == "1.3"
        assert sections_service.get_section_path(root) == "1"

    def test_deep_chain_path(self):
        """多层嵌套路径逐级编号正确"""
        pid = _make_project()
        l1 = _add(pid, "L1", sort_order=1)
        l2 = _add(pid, "L2", parent_id=l1, level=2, sort_order=1)
        l3 = _add(pid, "L3", parent_id=l2, level=3, sort_order=1)
        assert sections_service.get_section_path(l3) == "1.1.1"

    def test_large_sibling_set_correct_and_fast(self):
        """规模化护栏：单父节点 1500 个子章节，路径计算正确且耗时有限。

        旧实现排序键内逐键线性扫描全部 rows（O(N²)，~225 万次行扫描），
        新实现 O(N) 建表 + O(N log N) 排序。
        """
        pid = _make_project()
        root = _add(pid, "根", sort_order=1)
        items = [
            {"title": f"节{i}", "parent_id": root, "level": 2, "sort_order": i}
            for i in range(1, 1501)
        ]
        created = sections_service.batch_create_sections(pid, items)
        target_id = created[749].id  # sort_order=750 的章节

        t0 = time.perf_counter()
        path = sections_service.get_section_path(target_id)
        elapsed = time.perf_counter() - t0

        assert path == "1.750"
        assert elapsed < 3.0, f"get_section_path 耗时 {elapsed:.3f}s（1500 同级章节），疑似退化回 O(N²)"

    def test_dangling_parent_path_does_not_crash(self):
        """父节点悬空（parent 指向不存在章节）时路径计算不崩溃（不抛异常即可，
        编号语义与旧实现一致：悬空父下自成一组，子节点编号 1.1）"""
        pid = _make_project()
        child = _add(pid, "子", sort_order=1)
        _set_parent_ignoring_fk(child, 999999)
        assert sections_service.get_section_path(child) == "1.1"


# ============================================================================
# B-W2: move_section / reorder_sections —— executemany 批量更新
# ============================================================================


class TestMoveAndReorder:
    def _siblings(self, pid: int, n: int = 4) -> list[int]:
        root = _add(pid, "父", sort_order=1)
        ids = [_add(pid, f"子{i}", parent_id=root, level=2, sort_order=i) for i in range(1, n + 1)]
        return ids

    def _orders(self, pid: int) -> dict[int, int]:
        with get_db() as conn:
            rows = conn.execute(
                "SELECT id, sort_order FROM sections WHERE project_id = ? AND parent_id IS NOT NULL "
                "ORDER BY sort_order",
                (pid,),
            ).fetchall()
        return {r["id"]: r["sort_order"] for r in rows}

    def test_move_up_rebuilds_contiguous_order(self):
        pid = _make_project()
        ids = self._siblings(pid)  # [子1, 子2, 子3, 子4]
        assert sections_service.move_section(ids[2], "up") is True
        orders = self._orders(pid)
        assert sorted(orders.values()) == [1, 2, 3, 4]  # 连续无间隙
        assert orders[ids[2]] == 2  # 子3 上移到第 2 位
        assert orders[ids[1]] == 3

    def test_move_down_and_boundary(self):
        pid = _make_project()
        ids = self._siblings(pid)
        assert sections_service.move_section(ids[0], "up") is False   # 顶部上移无效
        assert sections_service.move_section(ids[3], "down") is False  # 底部下移无效
        assert sections_service.move_section(ids[0], "down") is True
        orders = self._orders(pid)
        assert orders[ids[0]] == 2 and orders[ids[1]] == 1

    def test_reorder_groups_contiguous(self):
        pid = _make_project()
        ids = self._siblings(pid)
        reordered = sections_service.reorder_sections(
            pid, [{"parent_id": None, "ordered_ids": []}]
        )  # 空组直接返回树，不报错
        assert isinstance(reordered, list)

        result = sections_service.reorder_sections(
            pid, [{"parent_id": _root_id(pid), "ordered_ids": [ids[3], ids[0], ids[2], ids[1]]}]
        )
        assert isinstance(result, list)
        orders = self._orders(pid)
        assert [orders[i] for i in [ids[3], ids[0], ids[2], ids[1]]] == [1, 2, 3, 4]


def _root_id(pid: int) -> int:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM sections WHERE project_id = ? AND parent_id IS NULL LIMIT 1", (pid,)
        ).fetchone()
    return row["id"]


# ============================================================================
# B-W3: validate_outline_integrity —— 记忆化环检测
# ============================================================================


class TestValidateOutlineIntegrity:
    def test_valid_chain_is_valid_and_fast(self):
        """规模化护栏：2000 节点链状大纲校验正确且耗时有限（旧实现最坏 O(N²)）"""
        pid = _make_project()
        prev = None
        items = []
        for i in range(1, 2001):
            items.append(
                {"title": f"N{i}", "parent_id": prev, "level": min(i + 1, 7), "sort_order": 1}
            )
            created = sections_service.batch_create_sections(pid, [items[-1]])
            prev = created[0].id

        t0 = time.perf_counter()
        result = sections_service.validate_outline_integrity(pid)
        elapsed = time.perf_counter() - t0

        assert result["valid"] is True, result["errors"]
        assert elapsed < 3.0, f"2000 节点链校验耗时 {elapsed:.3f}s，疑似退化回 O(N²)"

    def test_cycle_detected(self):
        pid = _make_project()
        a = _add(pid, "A", sort_order=1)
        b = _add(pid, "B", parent_id=a, level=2, sort_order=1)
        with get_db() as conn:
            conn.execute("UPDATE sections SET parent_id = ? WHERE id = ?", (b, a))
        result = sections_service.validate_outline_integrity(pid)
        assert result["valid"] is False
        assert any("循环引用" in e for e in result["errors"])

    def test_dangling_parent_detected(self):
        pid = _make_project()
        s = _add(pid, "孤", sort_order=1)
        _set_parent_ignoring_fk(s, 999999)
        result = sections_service.validate_outline_integrity(pid)
        assert result["valid"] is False
        assert any("parent_id" in e for e in result["errors"])

    def test_duplicate_sort_order_detected(self):
        pid = _make_project()
        root = _add(pid, "父", sort_order=1)
        _add(pid, "甲", parent_id=root, level=2, sort_order=1)
        _add(pid, "乙", parent_id=root, level=2, sort_order=1)
        result = sections_service.validate_outline_integrity(pid)
        assert result["valid"] is False
        assert any("sort_order 重复" in e for e in result["errors"])


# ============================================================================
# B-W4: files 路由同步 I/O 包装 to_thread
# ============================================================================


class TestFilesRouterOffload:
    def _project(self) -> int:
        return _make_project()

    def test_list_files_returns_records(self):
        pid = self._project()
        file_service.save_upload(pid, "招标文件.txt", "测试内容".encode("utf-8"))
        result = asyncio.run(files_router.list_files(pid))
        assert isinstance(result, list)
        assert any(f["filename"] == "招标文件.txt" for f in result)

    def test_get_file_text_returns_content(self):
        pid = self._project()
        saved = file_service.save_upload(pid, "清单.txt", "工程量清单内容".encode("utf-8"))
        result = asyncio.run(files_router.get_file_text(pid, saved["id"]))
        assert "工程量清单内容" in result["text"]

    def test_delete_file_404_for_missing(self):
        pid = self._project()
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(files_router.delete_uploaded_file(pid, 999999))
        assert exc_info.value.status_code == 404

    def test_delete_file_success(self):
        pid = self._project()
        saved = file_service.save_upload(pid, "待删除.txt", "内容".encode("utf-8"))
        result = asyncio.run(files_router.delete_uploaded_file(pid, saved["id"]))
        assert result["status"] == "deleted"
