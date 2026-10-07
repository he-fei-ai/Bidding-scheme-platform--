"""目录生成模块 BUG 修复回归测试（2026-10-06）

覆盖本轮深度探查后落地的修复，按项目约定：优先测行为，必要时做源码守卫断言。

- FIX-1 数据保全（P0）：目录重建前快照旧正文失败时，原实现仍执行 DELETE 把用户
  已生成正文无声清空；修复后中止重建并保留既有正文。
- FIX-2/3 编号命名空间统一（P1）：`_assign_ids` / `_fix_all_ids` / `_fix_ids_v51`
  原硬编码只处理 L1/L2/L3 三层，且 `_fix_ids_v51` 缺类型保护；修复后真递归任意深度、
  非 dict 子节点安全跳过，与 `renumber_outline_ids` 口径一致。
- FIX-4 缓存失效（P2）：旁路直写（正文保存 / 状态变更 / 脏状态回滚）后失效 Redis 整包
  章节缓存与内存正文缓存，避免「改了正文但 AI/前端读到旧内容」的脏读。
"""

import sqlite3
from contextlib import contextmanager

import pytest

from app.db import get_db
from app.routers.sse import sse_db_ops
from app.services.ai import workflows_outline_v51 as wo


def _seed_project(name: str) -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO projects (name, industry) VALUES (?, ?)", (name, "市政")
        )
        return int(cur.lastrowid)


def _seed_section(pid: int) -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO sections (project_id, parent_id, title, level, sort_order, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (pid, None, "S", 1, 1, "pending"),
        )
        return int(cur.lastrowid)


# ─────────────────────────────────────────────────────────────────────────────
# FIX-1：快照失败时中止重建，保留既有正文（数据保全 P0）
# ─────────────────────────────────────────────────────────────────────────────


class _RaiseOnSnapshotConn:
    """模拟 DB：仅在目录重建的「旧正文快照」查询上抛异常，其余查询不触发。"""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        # 旧正文快照查询会 SELECT content/status/target_words
        if "target_words" in (sql or ""):
            raise sqlite3.OperationalError("simulated snapshot failure")
        raise sqlite3.OperationalError(f"unexpected query in snapshot test: {sql}")


@contextmanager
def _fake_get_db_raising_snapshot():
    yield _RaiseOnSnapshotConn()


class TestSnapshotFailurePreservesContent:
    def test_rebuild_aborts_when_snapshot_fails(self):
        """快照失败时 _clear_and_save_outline 抛错而非清空正文"""
        pid = _seed_project("FIX1-保留正文")
        with get_db() as conn:
            conn.execute(
                "INSERT INTO sections (project_id, parent_id, title, level, sort_order, "
                "content, status, word_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (pid, None, "既有第一章", 1, 1, "用户耗时生成的长正文", "completed", 120),
            )

        orig = sse_db_ops.get_db
        sse_db_ops.get_db = _fake_get_db_raising_snapshot
        try:
            with pytest.raises(RuntimeError, match="中止重建"):
                sse_db_ops._clear_and_save_outline(pid, [{"title": "新章节", "children": []}])
        finally:
            sse_db_ops.get_db = orig

        # 既有正文必须仍然存在
        with get_db() as conn:
            rows = conn.execute(
                "SELECT title, content FROM sections WHERE project_id = ?", (pid,)
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["content"] == "用户耗时生成的长正文"


# ─────────────────────────────────────────────────────────────────────────────
# FIX-2/3：编号真递归任意深度 + 类型保护
# ─────────────────────────────────────────────────────────────────────────────


class TestRecursiveIdAssignment:
    def test_assign_ids_recursive_deep_levels(self):
        tree = [
            {
                "title": "A",
                "children": [
                    {
                        "title": "A1",
                        "children": [
                            {
                                "title": "A1a",
                                "children": [{"title": "A1a1", "children": []}],
                            }
                        ],
                    }
                ],
            }
        ]
        out = wo._assign_ids(tree)
        assert out[0]["id"] == "1"
        assert out[0]["children"][0]["id"] == "1.1"
        assert out[0]["children"][0]["children"][0]["id"] == "1.1.1"
        assert (
            out[0]["children"][0]["children"][0]["children"][0]["id"] == "1.1.1.1"
        )

    def test_assign_ids_coerces_str_and_non_dict_children(self):
        tree = [
            "纯字符串章节",
            {
                "title": "B",
                "children": [
                    "子串标题",
                    {"title": "B1", "children": [42, None, {"title": "B1a"}]},
                ],
            },
        ]
        out = wo._assign_ids(tree)
        # 字符串被规整为 dict 并拿到 id
        assert out[0]["id"] == "1"
        assert out[0]["title"] == "纯字符串章节"
        assert out[1]["id"] == "2"
        kids = out[1]["children"]
        assert kids[0]["id"] == "2.1"
        assert kids[1]["id"] == "2.2"
        # 第 3 层非 dict 元素被规整
        leaves = kids[1]["children"]
        assert leaves[0]["title"] == "42" and leaves[0]["id"] == "2.2.1"
        assert leaves[1]["title"] == "" and leaves[1]["id"] == "2.2.2"
        assert leaves[2]["id"] == "2.2.3"

    def test_fix_all_ids_recursive(self):
        tree = [
            {
                "title": "A",
                "children": [
                    {
                        "title": "A1",
                        "children": [
                            {
                                "title": "A1a",
                                "children": [{"title": "A1a1", "children": []}],
                            }
                        ],
                    }
                ],
            }
        ]
        out = wo._fix_all_ids(tree)
        assert out[0]["children"][0]["children"][0]["children"][0]["id"] == "1.1.1.1"

    def test_fix_ids_v51_skips_non_dict_safely(self):
        """原 _fix_ids_v51 对非 dict 子节点会 AttributeError；修复后安全跳过"""
        tree = [
            "顶层字符串",
            {
                "title": "B",
                "children": ["子串", {"title": "B1", "children": [{"title": "B1a"}]}],
            },
        ]
        # 不应抛异常
        out = wo._fix_ids_v51(tree)
        assert out[0] == "顶层字符串"  # 非 dict 原样保留
        assert out[1]["id"] == "2"
        assert out[1]["children"][0] == "子串"  # 非 dict 子节点保留
        assert out[1]["children"][1]["id"] == "2.2"
        assert out[1]["children"][1]["children"][0]["id"] == "2.2.1"


# ─────────────────────────────────────────────────────────────────────────────
# FIX-4：旁路直写后失效整包章节缓存
# ─────────────────────────────────────────────────────────────────────────────


class TestBypassWriteCacheInvalidation:
    def test_save_section_invalidates_cache(self, monkeypatch):
        pid = _seed_project("FIX4-保存失效")
        sid = _seed_section(pid)

        calls = []
        monkeypatch.setattr(
            sse_db_ops,
            "invalidate_content_caches",
            lambda p, section_ids=None: calls.append((p, section_ids)),
        )
        sse_db_ops._save_section_to_db(
            sid, "completed", "新生成的正文内容", project_id=pid
        )
        assert calls, "正文保存后必须调用 invalidate_content_caches"
        assert calls[-1][0] == pid
        assert sid in calls[-1][1]

    def test_mark_generating_invalidates_cache(self, monkeypatch):
        pid = _seed_project("FIX4-生成中失效")
        sid = _seed_section(pid)

        calls = []
        monkeypatch.setattr(
            sse_db_ops,
            "invalidate_content_caches",
            lambda p, section_ids=None: calls.append((p, section_ids)),
        )
        sse_db_ops._mark_sections_generating([sid])
        assert calls, "状态标记后必须调用 invalidate_content_caches"
        assert calls[-1][0] == pid
        assert sid in calls[-1][1]

    def test_rollback_dirty_invalidates_cache(self, monkeypatch):
        pid = _seed_project("FIX4-回滚失效")
        sid = _seed_section(pid)

        calls = []
        monkeypatch.setattr(
            sse_db_ops,
            "invalidate_content_caches",
            lambda p, section_ids=None: calls.append((p, section_ids)),
        )
        sse_db_ops._rollback_dirty_sections([sid])
        assert calls, "脏状态回滚后必须调用 invalidate_content_caches"
        assert calls[-1][0] == pid
        assert sid in calls[-1][1]
