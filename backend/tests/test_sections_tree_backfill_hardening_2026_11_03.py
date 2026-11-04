"""章节目录树 word_count 惰性回填竞态加固回归（2026-11-03）。

来源（章节编写模块第八轮深度审计与修复_2026-10-27.md §十一 遗留 C.1）：
build_tree 的惰性回填原为「readonly 锁外预计算 → 写锁内盲写同一批值」，
预计算与写入之间若并发写更新了正文，回填会用旧正文字数覆盖新值，且覆盖后
word_count 非 0 不再满足脏行条件、**不会自愈**。加固为写锁临界区内重读
content 再计算再写（读-改-写整体原子）。

运行：
    cd backend
    python -m pytest tests/test_sections_tree_backfill_hardening_2026_11_03.py -q
"""
from __future__ import annotations

import sqlite3

from app.config import DATABASE_PATH
from app.db import get_db, init_db

OWNER = 100

_V1 = "短正文。"  # 字数少
_V2 = "这一版正文由并发写者提交，内容显著更长，字数与第一版完全不同，用于区分回填写入的到底是哪一版。"  # 字数多


def _new_project_with_dirty_section(name: str) -> tuple[int, int]:
    """造脏行：content=v1 但 word_count/readable_word_count=0（触发惰性回填）。"""
    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO projects (name, industry, created_by) VALUES (?, ?, ?)",
            (name, "通用", OWNER),
        )
        pid = conn.execute(
            "SELECT id FROM projects ORDER BY id DESC LIMIT 1"
        ).fetchone()["id"]
        cur = conn.execute(
            "INSERT INTO sections (project_id, title, level, sort_order, content, status) "
            "VALUES (?, '第一节 总体部署', 1, 0, ?, 'completed')",
            (pid, _V1),
        )
        sid = cur.lastrowid
        # 显式把字数列置 0（INSERT 默认即 0，这里防 schema 默认值变化）
        conn.execute(
            "UPDATE sections SET word_count = 0, readable_word_count = 0 WHERE id = ?",
            (sid,),
        )
        return pid, sid


class TestLazyBackfillRace:
    def test_backfill_recomputes_from_content_at_write_time(self, monkeypatch):
        """C.1：readonly 快照(v1)之后、写锁会话之前并发写者提交 v2 ——
        回填必须以写锁内重读到的 v2 计算，不得盲写快照的 v1 字数。"""
        from app.services import sections as sec_mod

        pid, sid = _new_project_with_dirty_section("C1回填竞态-项目")
        real_get_db = sec_mod.get_db
        state = {"armed": True}

        def racing_get_db(*args, **kwargs):
            # 仅拦截首次裸 get_db()（= build_tree 的回填写会话）：
            # 用独立裸连接模拟带外并发写者更新正文 v1 → v2
            if not args and not kwargs and state["armed"]:
                state["armed"] = False
                raw = sqlite3.connect(str(DATABASE_PATH), timeout=5)
                try:
                    raw.execute(
                        "UPDATE sections SET content = ? WHERE id = ?", (_V2, sid)
                    )
                    raw.commit()
                finally:
                    raw.close()
            return real_get_db(*args, **kwargs)

        monkeypatch.setattr(sec_mod, "get_db", racing_get_db)
        sec_mod._build_tree_cache.pop(pid, None)
        sec_mod.build_tree(pid)

        with get_db(readonly=True) as conn:
            row = conn.execute(
                "SELECT content, word_count, readable_word_count FROM sections WHERE id = ?",
                (sid,),
            ).fetchone()
        assert row["content"] == _V2, "前置：并发写者的 v2 必须已在库中"
        assert row["word_count"] == sec_mod._count_words(_V2), (
            "回填必须以写锁内重读的正文计算 —— 盲写快照值即陈旧覆盖回归"
        )
        assert row["readable_word_count"] == sec_mod._count_readable_words(_V2)

    def test_backfill_still_self_heals_zero_columns(self, monkeypatch):
        """回归对照：无并发写时，脏行回填行为与加固前一致（字数列被补齐）。"""
        from app.services import sections as sec_mod

        pid, sid = _new_project_with_dirty_section("C1回填对照-项目")
        sec_mod._build_tree_cache.pop(pid, None)
        sec_mod.build_tree(pid)
        with get_db(readonly=True) as conn:
            row = conn.execute(
                "SELECT word_count, readable_word_count FROM sections WHERE id = ?",
                (sid,),
            ).fetchone()
        assert row["word_count"] == sec_mod._count_words(_V1)
        assert row["readable_word_count"] == sec_mod._count_readable_words(_V1)

    def test_tree_response_uses_snapshot_values(self, monkeypatch):
        """树响应与 DB 回填解耦：响应使用本次 readonly 快照重算值（读到什么显示什么）。"""
        from app.services import sections as sec_mod

        pid, sid = _new_project_with_dirty_section("C1响应快照-项目")
        real_get_db = sec_mod.get_db
        state = {"armed": True}

        def racing_get_db(*args, **kwargs):
            if not args and not kwargs and state["armed"]:
                state["armed"] = False
                raw = sqlite3.connect(str(DATABASE_PATH), timeout=5)
                try:
                    raw.execute(
                        "UPDATE sections SET content = ? WHERE id = ?", (_V2, sid)
                    )
                    raw.commit()
                finally:
                    raw.close()
            return real_get_db(*args, **kwargs)

        monkeypatch.setattr(sec_mod, "get_db", racing_get_db)
        sec_mod._build_tree_cache.pop(pid, None)
        tree = sec_mod.build_tree(pid)
        node = next(n for n in tree if n.id == sid)
        assert node.word_count == sec_mod._count_words(_V1), (
            "树响应应反映 readonly 快照时点的正文（v1），与 DB 回填(v2)解耦"
        )
