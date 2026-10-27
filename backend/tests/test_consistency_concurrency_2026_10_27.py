"""一致性校核模块并发性能回归测试（2026-10-27）。

本轮并发优化（全部有代码依据）：

- OPT-1 ``_load_suggestion_payload``（fix-suggestion / 采纳建议热路径）纯读改
  ``get_db(readonly=True)``，不再抢全局写锁 ``_db_write_lock``。
- OPT-2 ``consistency_repair._load_context``（AI 修复上下文准备）纯读改 readonly。
- 既有并发模型复核确认（不改代码，测试钉住）：
  * 写互斥按**项目粒度**（``_CONSISTENCY_OP_LOCKS: dict[int, asyncio.Lock]``），
    跨项目写操作互不阻塞；
  * 纯读端点（冲突列表 /summary）走 readonly 连接，WAL 下与写事务并发。

运行：
    cd backend
    python -m pytest tests/test_consistency_concurrency_2026_10_27.py -v
"""
from __future__ import annotations

import asyncio
import inspect
import threading
import time

from app.db import _db_write_lock, get_db, init_db


def _new_project(name: str) -> int:
    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO projects (name, industry, created_by) VALUES (?, ?, ?)",
            (name, "通用", 100),
        )
        return conn.execute(
            "SELECT id FROM projects ORDER BY id DESC LIMIT 1"
        ).fetchone()["id"]


class TestCrossProjectParallelism:
    def test_locks_are_per_project_not_global(self):
        """写互斥必须按项目粒度：项目 A 持锁期间，项目 B 的工作区锁立即可获得。

        回归背景：并发模型是「同项目写串行、跨项目并行」。若有人把
        `_CONSISTENCY_OP_LOCKS` 改回单一全局锁，本测试立即失败。
        """
        from app.routers.consistency import _LockManager

        pid_a = _new_project("并发-A")
        pid_b = _new_project("并发-B")

        async def _scenario() -> None:
            async with _LockManager(pid_a):
                # A 持锁期间，B 必须能立即进入临界区（跨项目不排队）
                acquired_b = asyncio.wait_for(_LockManager(pid_b).__aenter__(), timeout=1.0)
                started = time.monotonic()
                await acquired_b
                elapsed = time.monotonic() - started
                assert elapsed < 1.0, (
                    f"跨项目锁获取被阻塞 {elapsed:.2f}s —— 锁粒度退化成了全局锁"
                )
                # 清理：B 的锁由生成器封装释放（这里手动对称退出）
            # A 正常退出

        asyncio.run(_scenario())

    def test_same_project_lock_still_serializes(self):
        """对照用例：同项目的两把锁请求必须互斥（写串行语义未被优化破坏）。"""
        from app.routers.consistency import _LockManager

        pid = _new_project("并发-同项目")

        async def _scenario() -> bool:
            async with _LockManager(pid):
                inner = _LockManager(pid)
                try:
                    await asyncio.wait_for(inner.__aenter__(), timeout=0.2)
                    return False  # 不该拿到
                except asyncio.TimeoutError:
                    return True  # 正确：被同项目锁挡住
                finally:
                    # 若超时未持有则无需释放；若意外持有，对称释放
                    if inner._lock is not None and inner._lock.locked():
                        pass  # __aexit__ 由 with 管理；此处仅超时路径

        assert asyncio.run(_scenario()), "同项目写操作必须仍被 _LockManager 串行化"


class TestReadsSkipWriteLock:
    def test_readonly_read_completes_while_write_lock_held(self):
        """纯读（readonly 连接）必须能在一笔长写事务进行中并发完成。

        这是 WAL + readonly 跳锁模型的直接验证：另一个线程持有
        `_db_write_lock` 时，readonly 读不得在其上排队。
        """
        pid = _new_project("并发-读跳锁")
        with get_db() as conn:
            conn.execute(
                "INSERT INTO sections (project_id, title, level, sort_order, content, status) "
                "VALUES (?, '并发章节', 1, 0, '内容', 'completed')",
                (pid,),
            )

        release = threading.Event()
        lock_acquired = threading.Event()

        def _hold_write_lock() -> None:
            # 直接持有全局写锁，模拟一笔进行中的写事务（审计落库/导出等）
            with _db_write_lock:
                lock_acquired.set()
                release.wait(timeout=10)

        holder = threading.Thread(target=_hold_write_lock, daemon=True)
        holder.start()
        assert lock_acquired.wait(timeout=5), "测试前置：写锁未按预期被持有"

        try:
            assert _db_write_lock.locked(), "测试前置：写锁应处于持有态"
            started = time.monotonic()
            with get_db(readonly=True) as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM sections WHERE project_id = ?", (pid,)
                ).fetchone()
                assert row["n"] == 1
            elapsed = time.monotonic() - started
            assert elapsed < 2.0, (
                f"readonly 读在写锁被持有时仍耗时 {elapsed:.2f}s —— 读通道被写锁阻塞"
            )
            # 期间写锁从未释放（证明读确实绕开了它，而非碰巧趁锁空闲）
            assert _db_write_lock.locked()
        finally:
            release.set()
            holder.join(timeout=5)

    def test_suggestion_payload_uses_readonly(self):
        """OPT-1 契约：修复建议读取（采纳建议热路径）必须用 readonly 连接。"""
        from app.services import consistency_auditor

        src = inspect.getsource(
            consistency_auditor.ConsistencyAuditor._load_suggestion_payload
        )
        assert "get_db(readonly=True)" in src
        assert "with get_db() as conn" not in src

    def test_repair_context_uses_readonly(self):
        """OPT-2 契约：AI 修复上下文准备（纯读）必须用 readonly 连接。"""
        from app.services import consistency_repair

        src = inspect.getsource(consistency_repair._load_context)
        assert "get_db(readonly=True)" in src
        assert "with get_db() as conn" not in src

    def test_suggestion_payload_works_end_to_end(self):
        """OPT-1 行为回归：readonly 化后建议读取的取值推导不被破坏。"""
        from app.services.consistency_auditor import consistency_auditor

        pid = _new_project("并发-建议读取")
        cid = "conc-c1"
        with get_db() as conn:
            conn.execute(
                "INSERT INTO consistency_conflicts "
                "(project_id, conflict_id, fact_name, values_json, sources_json, "
                " severity, description, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    pid,
                    cid,
                    "工期",
                    # 取值数组为纯字符串（_build_suggestion 的输入契约，
                    # 与 test_consistency_routes_extension 的种子格式一致）
                    '["30天", "45天"]',
                    '["section_1", "section_1"]',
                    "high",
                    "测试冲突",
                    "2026-10-27 00:00:00",
                ),
            )
        target, text = consistency_auditor._load_suggestion_payload(cid, pid)
        # 多数派推导：两个取值、无多数 → 期望给出建议文本（非空）且目标值可解析
        assert isinstance(target, (str, type(None)))
        # 存在记录时不应静默返回 (None, None) 而丢掉建议上下文
        assert target is not None or text is not None
