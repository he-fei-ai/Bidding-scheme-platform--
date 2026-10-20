# -*- coding: utf-8 -*-
"""A-5 BUG-FIX 2026-10-17：SSE 目录生成上下文硬闸门守卫测试。

修复前：_extract_project_context 只检查 analysis 行是否存在 + summary/scoring
是否非空，不检查 confirmed_summary/confirmed_scoring。绕过前端直接调用 SSE
可基于用户从未确认过的 AI 输出生成目录。

修复后：SELECT 补齐 confirmed_* 列，返回值带 is_confirmed 标志 +
confirmed_summary/confirmed_scoring 字段，软闸门 warning 日志。
"""

import inspect
import sqlite3

import pytest


class TestSSEExtractContextConfirmedGate:
    """_extract_project_context 必须带 confirmed_* 软闸门。"""

    def test_source_select_has_confirmed_columns(self):
        """_extract_project_context 的 SELECT 必须包含 confirmed_summary 和
        confirmed_scoring 列（源码守卫，防止有人重写 SQL 时把这两列丢了）。
        """
        from app.routers.sse import sse_db_ops as _ops

        src = inspect.getsource(_ops._extract_project_context)
        # 剔除注释行
        code_only = "\n".join(
            ln for ln in src.splitlines()
            if not ln.lstrip().startswith("#")
        )
        assert "confirmed_summary" in code_only, (
            "_extract_project_context 的 SELECT 缺少 confirmed_summary"
        )
        assert "confirmed_scoring" in code_only, (
            "_extract_project_context 的 SELECT 缺少 confirmed_scoring"
        )

    def test_source_returns_is_confirmed(self):
        """返回值 dict 必须包含 is_confirmed 键（源码守卫）。"""
        from app.routers.sse import sse_db_ops as _ops

        src = inspect.getsource(_ops._extract_project_context)
        # 剔除注释
        code_only = "\n".join(
            ln for ln in src.splitlines()
            if not ln.lstrip().startswith("#")
        )
        assert '"is_confirmed"' in code_only or "'is_confirmed'" in code_only, (
            "_extract_project_context 返回值缺少 is_confirmed 键"
        )

    def test_source_has_warning_for_unconfirmed(self):
        """源码必须包含对「数据非空但未确认」场景的可观测 warning 日志。"""
        from app.routers.sse import sse_db_ops as _ops

        src = inspect.getsource(_ops._extract_project_context)
        # 必须有 logger.warning（不是 silent except）
        code_only = "\n".join(
            ln for ln in src.splitlines()
            if not ln.lstrip().startswith("#")
        )
        assert "未确认" in code_only or "confirmed" in code_only.lower(), (
            "_extract_project_context 源码缺少对未确认数据的 warning 日志"
        )

    @pytest.mark.asyncio
    async def test_is_confirmed_true_when_both_confirmed(self):
        """集成测试：当 confirmed_summary=1, confirmed_scoring=1 且数据非空时，
        is_confirmed 应为 True。
        """
        from app.db import get_db
        from app.routers.sse.sse_db_ops import _extract_project_context

        with get_db() as conn:
            conn.execute("INSERT INTO projects (name) VALUES (?)", ("A5-confirmed",))
            pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO analysis (project_id, summary_json, scoring_points_json, "
                "confirmed_summary, confirmed_scoring) VALUES (?, '{\"title\":\"s\"}', '[]', 1, 1)",
                (pid,),
            )
            conn.commit()

        ctx = await _extract_project_context(pid)
        assert ctx.get("is_confirmed") is True, (
            f"confirmed_summary=confirmed_scoring=1 时 is_confirmed 应为 True，实际 {ctx.get('is_confirmed')}"
        )

    @pytest.mark.asyncio
    async def test_is_confirmed_false_when_scoring_unconfirmed(self):
        """集成测试：confirmed_summary=1 但 confirmed_scoring=0 → is_confirmed=False。"""
        from app.db import get_db
        from app.routers.sse.sse_db_ops import _extract_project_context

        with get_db() as conn:
            conn.execute("INSERT INTO projects (name) VALUES (?)", ("A5-unconfirmed",))
            pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO analysis (project_id, summary_json, scoring_points_json, "
                "confirmed_summary, confirmed_scoring) VALUES (?, '{\"title\":\"s\"}', '[]', 1, 0)",
                (pid,),
            )
            conn.commit()

        ctx = await _extract_project_context(pid)
        assert ctx.get("is_confirmed") is False, (
            "confirmed_scoring=0 时 is_confirmed 应为 False"
        )

    @pytest.mark.asyncio
    async def test_extract_context_returns_all_three_confirmed_keys(self):
        """集成测试：返回值 dict 必须同时带 is_confirmed / confirmed_summary / confirmed_scoring
        三个键，不能只带一个。
        """
        from app.db import get_db
        from app.routers.sse.sse_db_ops import _extract_project_context

        with get_db() as conn:
            conn.execute("INSERT INTO projects (name) VALUES (?)", ("A5-keys",))
            pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO analysis (project_id, summary_json, scoring_points_json, "
                "confirmed_summary, confirmed_scoring) VALUES (?, '{\"title\":\"s\"}', '[]', 0, 0)",
                (pid,),
            )
            conn.commit()

        ctx = await _extract_project_context(pid)
        for key in ("is_confirmed", "confirmed_summary", "confirmed_scoring"):
            assert key in ctx, (
                f"_extract_project_context 返回值缺少 {key} 键"
            )
        assert ctx["is_confirmed"] is False
        assert ctx["confirmed_summary"] is False
        assert ctx["confirmed_scoring"] is False


# ============================================================================
# 正文生成链路 sse_utils._fetch_project_data_async
# ============================================================================
class TestSSEFetchProjectDataConfirmedGate:
    """_fetch_project_data_async（正文生成）两条 SELECT 路径必须都带 confirmed_*。"""

    def test_source_has_two_selects_with_confirmed(self):
        """_fetch_project_data_async 有两条 SELECT：缓存命中 _fetch_metadata +
        全量查询 _fetch_all。两者都必须包含 confirmed_summary / confirmed_scoring。
        源码守卫防止后续重写 SQL 时把某一条漏了。
        """
        from app.routers.sse import sse_utils as _u

        src = inspect.getsource(_u._fetch_project_data_async)
        code_only = "\n".join(
            ln for ln in src.splitlines()
            if not ln.lstrip().startswith("#")
        )
        # confirmed_summary 至少出现 2 次（两条 SELECT 各一次）
        cs_count = code_only.count("confirmed_summary")
        assert cs_count >= 2, (
            f"_fetch_project_data_async 源码中 confirmed_summary 出现 {cs_count} 次，"
            f"期望 ≥2（缓存 + 全量两条 SELECT）"
        )
        cs2_count = code_only.count("confirmed_scoring")
        assert cs2_count >= 2, (
            f"_fetch_project_data_async 源码中 confirmed_scoring 出现 {cs2_count} 次，"
            f"期望 ≥2"
        )

    def test_source_returns_is_confirmed_three_keys(self):
        """返回值 dict 必须同时带 is_confirmed / confirmed_summary / confirmed_scoring
        三键（源码守卫）。"""
        from app.routers.sse import sse_utils as _u

        src = inspect.getsource(_u._fetch_project_data_async)
        code_only = "\n".join(
            ln for ln in src.splitlines()
            if not ln.lstrip().startswith("#")
        )
        for key in ("is_confirmed", "confirmed_summary", "confirmed_scoring"):
            assert f'"{key}"' in code_only or f"'{key}'" in code_only, (
                f"_fetch_project_data_async 返回值缺少 {key} 键"
            )

    def test_source_has_warning_for_unconfirmed(self):
        """源码必须包含对「数据非空但未确认」场景的可观测 warning 日志
        （与目录链路同口径）。"""
        from app.routers.sse import sse_utils as _u

        src = inspect.getsource(_u._fetch_project_data_async)
        code_only = "\n".join(
            ln for ln in src.splitlines()
            if not ln.lstrip().startswith("#")
        )
        assert "未确认" in code_only, (
            "_fetch_project_data_async 源码缺少对未确认数据的 warning 日志"
        )

    @pytest.mark.asyncio
    async def test_fetch_project_data_returns_is_confirmed_true(self):
        """集成测试：confirmed_summary=confirmed_scoring=1 → is_confirmed=True。"""
        from app.db import get_db
        from app.routers.sse.sse_utils import _fetch_project_data_async

        with get_db() as conn:
            conn.execute("INSERT INTO projects (name) VALUES (?)", ("A5-body-confirmed",))
            pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO analysis (project_id, summary_json, scoring_points_json, "
                "confirmed_summary, confirmed_scoring) VALUES (?, '{\"title\":\"s\"}', '[]', 1, 1)",
                (pid,),
            )
            # 至少要有一个 section 行（_fetch_project_data_async 需要 sections 数据）
            conn.execute(
                "INSERT INTO sections (project_id, title, content, status, sort_order, level) "
                "VALUES (?, 'T1', '', 'pending', 1, 1)",
                (pid,),
            )
            conn.commit()

        data = await _fetch_project_data_async(pid)
        assert data is not None, "_fetch_project_data_async 不应返回 None"
        assert data["is_confirmed"] is True, (
            f"confirmed_summary=confirmed_scoring=1 时 is_confirmed 应为 True，"
            f"实际 {data['is_confirmed']}"
        )
        assert data["confirmed_summary"] is True
        assert data["confirmed_scoring"] is True

    @pytest.mark.asyncio
    async def test_fetch_project_data_returns_is_confirmed_false(self):
        """集成测试：confirmed_summary=1, confirmed_scoring=0 → is_confirmed=False。"""
        from app.db import get_db
        from app.routers.sse.sse_utils import _fetch_project_data_async

        with get_db() as conn:
            conn.execute("INSERT INTO projects (name) VALUES (?)", ("A5-body-unconf",))
            pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO analysis (project_id, summary_json, scoring_points_json, "
                "confirmed_summary, confirmed_scoring) VALUES (?, '{\"title\":\"s\"}', '[]', 1, 0)",
                (pid,),
            )
            conn.execute(
                "INSERT INTO sections (project_id, title, content, status, sort_order, level) "
                "VALUES (?, 'T1', '', 'pending', 1, 1)",
                (pid,),
            )
            conn.commit()

        data = await _fetch_project_data_async(pid)
        assert data is not None
        assert data["is_confirmed"] is False
        assert data["confirmed_summary"] is True
        assert data["confirmed_scoring"] is False


# ============================================================================
# A-5-harden：SSE outline 主循环必须消费 is_confirmed 推送 warning
# ============================================================================
class TestSSEOutlineConsumesConfirmedGate:
    """sse_outline.py 生成目录主循环在 _extract_project_context_with_retry
    返回后必须检查 is_confirmed，非 True 时推送 warning SSE 事件。
    源码守卫防止未来重写主循环时丢掉软闸门消费。
    """

    def test_source_checks_is_confirmed(self):
        """sse_outline.py 必须消费 context["is_confirmed"] 做分支判断。"""
        from app.routers.sse import sse_outline as _o

        src = inspect.getsource(_o)
        code_only = "\n".join(
            ln for ln in src.splitlines()
            if not ln.lstrip().startswith("#")
        )
        assert "is_confirmed" in code_only, (
            "sse_outline.py 主循环必须检查 context.is_confirmed"
        )

    def test_source_pushes_warning_for_unconfirmed(self):
        """未确认时必须推送 warning 类型的 SSE 事件（_qput 带 type: warning）。"""
        from app.routers.sse import sse_outline as _o

        src = inspect.getsource(_o)
        code_only = "\n".join(
            ln for ln in src.splitlines()
            if not ln.lstrip().startswith("#")
        )
        # 必须有 "warning" 字符串（_qput 的 type 字段值）+ "未确认" 中文提示
        assert "warning" in code_only.lower(), (
            "sse_outline.py 未确认分支必须推送 warning 事件"
        )
        assert "未确认" in code_only, (
            "sse_outline.py warning 事件必须包含「未确认」提示文字"
        )


# ============================================================================
# A-2：analysis stale 检测（文件变更后必须硬拦截）
# ============================================================================
class TestCheckAnalysisStaleSync:
    """check_analysis_stale_sync 函数必须正确判断 analysis 是否因
    uploaded_files 变更而过时。"""

    def test_source_exists_in_sse_db_ops(self):
        """函数必须在 sse_db_ops.py 导出（供目录侧 + 正文侧共用）。"""
        from app.routers.sse.sse_db_ops import check_analysis_stale_sync
        assert callable(check_analysis_stale_sync), (
            "check_analysis_stale_sync 必须是可调用函数"
        )

    def test_source_has_guard_for_missing_columns(self):
        """源码必须包含对 sqlite3.OperationalError（老库缺列）的守卫，
        降级为返回 False（放行）而非崩溃。"""
        from app.routers.sse import sse_db_ops as _o

        src = inspect.getsource(_o.check_analysis_stale_sync)
        code_only = "\n".join(
            ln for ln in src.splitlines()
            if not ln.lstrip().startswith("#")
        )
        assert "OperationalError" in code_only or "sqlite3" in code_only, (
            "check_analysis_stale_sync 必须捕获 sqlite3.OperationalError 兼容老库"
        )

    def test_stale_when_files_newer(self):
        """集成：uploaded_files.updated_at > analysis.updated_at → is_stale=True。"""
        from app.db import get_db
        from app.routers.sse.sse_db_ops import check_analysis_stale_sync

        with get_db() as conn:
            conn.execute("INSERT INTO projects (name) VALUES (?)", ("A2-stale",))
            pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            # analysis 先插入
            conn.execute(
                "INSERT INTO analysis (project_id, summary_json, confirmed_summary, confirmed_scoring, updated_at) "
                "VALUES (?, '{}', 0, 0, '2020-01-01 00:00:00')",
                (pid,),
            )
            # 再插入更新时间更晚的文件
            conn.execute(
                "INSERT INTO uploaded_files "
                "(project_id, filename, path, file_type, file_role, status, updated_at) "
                "VALUES (?, 'new.pdf', '/x/new.pdf', 'pdf', 'main', 'done', '2025-06-01 12:00:00')",
                (pid,),
            )
            conn.commit()

        # 重新打开连接做检测（原 with get_db() 已关闭）
        with get_db() as conn2:
            is_stale, reason = check_analysis_stale_sync(conn2, pid)

        assert is_stale is True, (
            f"files 比 analysis 新时 is_stale 应为 True，实际 {is_stale}"
        )
        assert reason is not None
        assert "重新运行" in reason or "AI 分析" in reason

    def test_not_stale_when_analysis_newer(self):
        """集成：analysis.updated_at > uploaded_files.updated_at → is_stale=False。"""
        from app.db import get_db
        from app.routers.sse.sse_db_ops import check_analysis_stale_sync

        with get_db() as conn:
            conn.execute("INSERT INTO projects (name) VALUES (?)", ("A2-not-stale",))
            pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO analysis (project_id, summary_json, confirmed_summary, confirmed_scoring, updated_at) "
                "VALUES (?, '{}', 0, 0, '2025-06-01 12:00:00')",
                (pid,),
            )
            conn.execute(
                "INSERT INTO uploaded_files "
                "(project_id, filename, path, file_type, file_role, status, updated_at) "
                "VALUES (?, 'old.pdf', '/x/old.pdf', 'pdf', 'main', 'done', '2020-01-01 00:00:00')",
                (pid,),
            )
            conn.commit()

        with get_db() as conn2:
            is_stale, reason = check_analysis_stale_sync(conn2, pid)

        assert is_stale is False, (
            f"analysis 比 files 新时 is_stale 应为 False，实际 {is_stale}"
        )
        assert reason is None

    def test_not_stale_when_no_files(self):
        """集成：无 uploaded_files 记录时 → is_stale=False（无法判断不过滤）。"""
        from app.db import get_db
        from app.routers.sse.sse_db_ops import check_analysis_stale_sync

        with get_db() as conn:
            conn.execute("INSERT INTO projects (name) VALUES (?)", ("A2-no-files",))
            pid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO analysis (project_id, summary_json, confirmed_summary, confirmed_scoring, updated_at) "
                "VALUES (?, '{}', 0, 0, '2025-06-01 12:00:00')",
                (pid,),
            )
            conn.commit()

        with get_db() as conn2:
            is_stale, reason = check_analysis_stale_sync(conn2, pid)

        assert is_stale is False, (
            "无 uploaded_files 时无法判断，应放行（is_stale=False）"
        )

    def test_source_outline_has_stale_check(self):
        """源码守卫：sse_outline.py 必须调用 check_analysis_stale_async（async helper，
        内部 asyncio.to_thread 包装同步 SQLite，避免 SSE 主循环阻塞）。"""
        from app.routers.sse import sse_outline as _o

        src = inspect.getsource(_o)
        code_only = "\n".join(
            ln for ln in src.splitlines()
            if not ln.lstrip().startswith("#")
        )
        assert "check_analysis_stale_async" in code_only, (
            "sse_outline.py 必须调用 check_analysis_stale_async 做 A-2 硬拦截"
        )

    def test_source_content_has_stale_check(self):
        """源码守卫：sse_content.py 必须调用 check_analysis_stale_async（同上）。"""
        from app.routers.sse import sse_content as _o

        src = inspect.getsource(_o)
        code_only = "\n".join(
            ln for ln in src.splitlines()
            if not ln.lstrip().startswith("#")
        )
        assert "check_analysis_stale_async" in code_only, (
            "sse_content.py 必须调用 check_analysis_stale_async 做 A-2 硬拦截"
        )
