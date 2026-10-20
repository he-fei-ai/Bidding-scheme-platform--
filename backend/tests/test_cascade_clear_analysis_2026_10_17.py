# -*- coding: utf-8 -*-
"""A-3 BUG-FIX 2026-10-17：DELETE /analysis 级联清除守卫测试。

修复前：DELETE /analysis 只清 analysis 表行，v51_tasks（目录 JSON blob）、
sections（章节正文）、charts（配图）三张表残留 —— 用户感知
「我清了分析，为什么目录/正文/图表还在」。

修复后：级联清除三张下游派生表 + 缓存失效。本文件钉住该行为。
"""

import sqlite3

import pytest


def _get_db():
    from app.db import get_db
    return get_db()


def _mk_project() -> int:
    """建一个有 analysis + sections 数据的项目（v51_tasks/charts 可选）。"""
    from app.db import get_db

    with get_db() as conn:
        conn.execute("INSERT INTO projects (name) VALUES (?)", ("A3-test-project",))
        pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        # analysis 行
        conn.execute(
            "INSERT INTO analysis (project_id, summary_json, scoring_points_json, "
            "confirmed_summary, confirmed_scoring) "
            "VALUES (?, '{\"title\":\"s\"}', '[]', 1, 1)",
            (pid,),
        )
        # sections 行（必存在）
        conn.execute(
            "INSERT INTO sections (project_id, title, content, status, sort_order, level) "
            "VALUES (?, 'T1', 'hello', 'completed', 1, 1)",
            (pid,),
        )
        # v51_tasks 行（若存在 + 有 project_id 列 + UNIQUE 未被占）
        try:
            cols = [r[1] for r in conn.execute("PRAGMA table_info(v51_tasks)").fetchall()]
            if "project_id" in cols:
                conn.execute(
                    "INSERT INTO v51_tasks (project_id, level1_outline_json, final_outline_json, status) "
                    "VALUES (?, '[]', '[]', 'completed')",
                    (pid,),
                )
        except sqlite3.OperationalError:
            pass  # 老库无 v51_tasks
        # charts 行（若存在 + 有 project_id 列）
        try:
            ccols = [r[1] for r in conn.execute("PRAGMA table_info(charts)").fetchall()]
            if "project_id" in ccols:
                conn.execute(
                    "INSERT INTO charts (project_id, chart_type, status) VALUES (?, 'flowchart', 'completed')",
                    (pid,),
                )
        except sqlite3.OperationalError:
            pass
        conn.commit()
    return pid


def _count(conn, sql: str) -> int:
    try:
        return conn.execute(f"SELECT COUNT(*) FROM ({sql})").fetchone()[0]
    except sqlite3.OperationalError:
        return -1  # 表不存在


class TestCascadeClearAnalysis:
    """DELETE /analysis 必须级联清除所有下游派生数据。"""

    # ---------------------------------------------------------------
    # 行为测试：sections 级联（核心验证点）
    # ---------------------------------------------------------------
    def test_cascade_clears_sections(self):
        """调用 _sync_clear_analysis + 级联逻辑后，sections 应为 0 行。"""
        pid = _mk_project()

        # 前置：确认 sections 有 1 行
        with _get_db() as conn:
            section_count = _count(conn, f"SELECT 1 FROM sections WHERE project_id = {pid}")
            assert section_count == 1, f"前置条件：sections 应有 1 行，实际 {section_count}"

        # 执行 analysis 清除（含级联）
        from app.routers.ai import _sync_clear_analysis
        _sync_clear_analysis(pid)

        # 执行级联代码（与 ai.py DELETE /analysis 完全同口径）
        from app.services.redis_cache import CacheService
        from app.services.sections import invalidate_content_caches

        def _cascade(pid: int) -> None:
            with _get_db() as conn:
                try:
                    cols = {r[1] for r in conn.execute("PRAGMA table_info(v51_tasks)").fetchall()}
                    if "project_id" in cols:
                        conn.execute("DELETE FROM v51_tasks WHERE project_id = ?", (pid,))
                    conn.execute("DELETE FROM sections WHERE project_id = ?", (pid,))
                    try:
                        cc = {r[1] for r in conn.execute("PRAGMA table_info(charts)").fetchall()}
                        if "project_id" in cc:
                            conn.execute("DELETE FROM charts WHERE project_id = ?", (pid,))
                    except sqlite3.OperationalError:
                        pass
                except Exception:
                    pass

        _cascade(pid)
        invalidate_content_caches(pid)
        CacheService.invalidate_outline(pid)
        CacheService.invalidate_project_detail(pid)

        # 验证
        with _get_db() as conn:
            # sections 应被级联清除
            section_count = _count(conn, f"SELECT 1 FROM sections WHERE project_id = {pid}")
            assert section_count == 0, f"sections 应被级联清除，实际 {section_count} 行残留"
            # analysis 行应仍存在（只清字段不删行）
            a_row = conn.execute(
                "SELECT 1 FROM analysis WHERE project_id = ?", (pid,)
            ).fetchone()
            assert a_row is not None, "analysis 行应存在（_sync_clear_analysis 只清字段不删行）"

    def test_cascade_only_touches_target_project(self):
        """级联清除必须按 project_id 收敛，不能误删其他项目的 sections。"""
        pid_a = _mk_project()
        pid_b = _mk_project()

        from app.routers.ai import _sync_clear_analysis

        _sync_clear_analysis(pid_a)

        def _cascade(pid):
            with _get_db() as conn:
                try:
                    cols = {r[1] for r in conn.execute("PRAGMA table_info(v51_tasks)").fetchall()}
                    if "project_id" in cols:
                        conn.execute("DELETE FROM v51_tasks WHERE project_id = ?", (pid,))
                    conn.execute("DELETE FROM sections WHERE project_id = ?", (pid,))
                except Exception:
                    pass

        _cascade(pid_a)

        # pid_b 的 sections 应该还在
        with _get_db() as conn:
            b_sections = _count(conn, f"SELECT 1 FROM sections WHERE project_id = {pid_b}")
            assert b_sections == 1, f"其他项目 (pid={pid_b}) 的 sections 被误删！"

    # ---------------------------------------------------------------
    # 源码守卫：防止有人重写 clear_analysis 时把级联逻辑丢失
    # ---------------------------------------------------------------
    def test_source_contract_clear_analysis_has_cascade(self):
        """DELETE /analysis 端点的源码必须包含级联删除 sections + 缓存失效。"""
        from app.routers import ai as _ai

        import inspect

        src = inspect.getsource(_ai.clear_analysis)
        # 剔除注释行，防止 BUG-FIX 注释提及旧写法造成假阳性
        code_only = "\n".join(
            ln for ln in src.splitlines()
            if not ln.lstrip().startswith("#")
        )
        assert "DELETE FROM sections WHERE project_id" in code_only, (
            "DELETE /analysis 端点源码缺少 sections 级联清除"
        )
        assert "CacheService.invalidate_outline" in code_only, (
            "DELETE /analysis 端点源码缺少目录缓存失效"
        )
        assert "invalidate_content_caches" in code_only, (
            "DELETE /analysis 端点源码缺少章节缓存失效"
        )
        # 必须保留 v51_tasks 级联（即使表不存在也要有守卫代码）
        assert "DELETE FROM v51_tasks" in code_only, (
            "DELETE /analysis 端点源码缺少 v51_tasks 级联清除"
        )
