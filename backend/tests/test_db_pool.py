"""数据库连接池单元测试"""
import os
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

# 临时覆盖配置以使用测试数据库
import unittest.mock as mock

from app.config import DATABASE_PATH  # noqa: E402

# 在测试期间使用临时数据库
# BUG-FIX 2026-09-18（低）：原固定路径依赖 import 时一次性 unlink，Windows 下
# 文件被占用时跳过删除 → 上次残留的表导致 "table already exists"。改为：
# 1) 文件名带 PID 避免跨进程残留冲突；2) 每张表建表前先 DROP IF EXISTS 保证幂等。
_TEST_DB = Path(tempfile.gettempdir()) / f"test_pool_{os.getpid()}.sqlite3"
if _TEST_DB.exists():
    try:
        _TEST_DB.unlink()
    except PermissionError:
        # Windows下文件被占用时跳过删除，后续建表前的 DROP 保证幂等
        pass


def test_pool_creates_connection():
    """首次访问应创建新连接"""
    import importlib
    # 重置 _pool 单例
    from app import db_pool
    importlib.reload(db_pool)

    # 替换数据库路径
    with mock.patch.object(db_pool, "DATABASE_PATH", _TEST_DB):
        # 初始化测试表（先删后建，保证重复运行幂等）
        with sqlite3.connect(str(_TEST_DB)) as conn:
            conn.execute("DROP TABLE IF EXISTS test")
            conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY, name TEXT)")

        pool = db_pool.get_pool()
        with pool.connection() as conn:
            conn.execute("INSERT INTO test (name) VALUES (?)", ("hello",))
            cur = conn.execute("SELECT name FROM test WHERE id = 1")
            assert cur.fetchone()["name"] == "hello"
        print("[PASS] test_pool_creates_connection")


def test_pool_reuses_thread_connection():
    """同线程应复用连接"""
    import importlib
    from app import db_pool
    importlib.reload(db_pool)

    with mock.patch.object(db_pool, "DATABASE_PATH", _TEST_DB):
        pool = db_pool.get_pool()
        conn1 = pool._get_thread_conn()
        conn2 = pool._get_thread_conn()
        assert conn1 is conn2, "同线程应返回同一连接"
        print("[PASS] test_pool_reuses_thread_connection")


def test_pool_thread_isolation():
    """不同线程应使用不同连接"""
    import importlib
    from app import db_pool
    importlib.reload(db_pool)

    with mock.patch.object(db_pool, "DATABASE_PATH", _TEST_DB):
        pool = db_pool.get_pool()
        results = {}

        def worker(name: str):
            results[name] = pool._get_thread_conn()

        t1 = threading.Thread(target=worker, args=("t1",))
        t2 = threading.Thread(target=worker, args=("t2",))
        t1.start(); t2.start()
        t1.join(); t2.join()

        assert results["t1"] is not results["t2"], "不同线程应使用不同连接"
        print("[PASS] test_pool_thread_isolation")


def test_pool_stats():
    """连接池统计应正确累计"""
    import importlib
    from app import db_pool
    importlib.reload(db_pool)

    with mock.patch.object(db_pool, "DATABASE_PATH", _TEST_DB):
        pool = db_pool.get_pool()
        # 触发新连接
        pool._get_thread_conn()
        pool._get_thread_conn()
        pool._get_thread_conn()

        stats = pool.stats
        assert stats["connections_created"] >= 1
        assert stats["connections_reused"] >= 2
        print(f"[PASS] test_pool_stats (created={stats['connections_created']}, reused={stats['connections_reused']})")


def test_pool_rollback_on_exception():
    """异常时应回滚事务"""
    import importlib
    from app import db_pool
    importlib.reload(db_pool)

    with mock.patch.object(db_pool, "DATABASE_PATH", _TEST_DB):
        with sqlite3.connect(str(_TEST_DB)) as conn:
            conn.execute("DROP TABLE IF EXISTS rollback_test")
            conn.execute("CREATE TABLE rollback_test (id INTEGER PRIMARY KEY, val TEXT)")

        pool = db_pool.get_pool()
        try:
            with pool.connection() as conn:
                conn.execute("INSERT INTO rollback_test (val) VALUES (?)", ("will_rollback",))
                raise RuntimeError("测试回滚")
        except RuntimeError:
            pass

        # 验证数据未持久化
        with sqlite3.connect(str(_TEST_DB)) as conn:
            cur = conn.execute("SELECT COUNT(*) FROM rollback_test")
            count = cur.fetchone()[0]
            assert count == 0, f"回滚后数据应不存在,但找到 {count} 行"
        print("[PASS] test_pool_rollback_on_exception")


def test_pooled_db_context_manager():
    """pooled_db 上下文管理器接口"""
    import importlib
    from app import db_pool
    importlib.reload(db_pool)

    with mock.patch.object(db_pool, "DATABASE_PATH", _TEST_DB):
        with sqlite3.connect(str(_TEST_DB)) as conn:
            conn.execute("DROP TABLE IF EXISTS cm_test")
            conn.execute("CREATE TABLE cm_test (id INTEGER PRIMARY KEY)")

        with db_pool.pooled_db() as conn:
            conn.execute("INSERT INTO cm_test (id) VALUES (1)")

        with sqlite3.connect(str(_TEST_DB)) as conn:
            count = conn.execute("SELECT COUNT(*) FROM cm_test").fetchone()[0]
            assert count == 1
        print("[PASS] test_pooled_db_context_manager")


if __name__ == "__main__":
    test_pool_creates_connection()
    test_pool_reuses_thread_connection()
    test_pool_thread_isolation()
    test_pool_stats()
    test_pool_rollback_on_exception()
    test_pooled_db_context_manager()
    print("\n[OK] 全部 6 个 db_pool 测试通过")
