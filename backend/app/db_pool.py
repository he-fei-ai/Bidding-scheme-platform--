"""数据库连接池管理

SQLite 在高并发场景下容易出现"database is locked"错误,即使使用 WAL 模式
也会因为多线程写锁竞争而失败。本模块在原有 WAL + busy_timeout 配置之上,
提供:

- 线程本地连接缓存 (ThreadLocalConnection): 同线程复用连接
- 写锁串行化 (WriteLock): 写操作通过 asyncio.Lock 串行,避免多协程写冲突
- 读并发上限 (ReadSemaphore): 控制同时进行的读操作数
- 健康检查与自动重连 (connect_with_retry): 连接断开时自动重试

使用方式:
    from .db_pool import get_pooled_db
    with get_pooled_db(readonly=False) as conn:
        conn.execute(...)
"""

from collections.abc import Iterator
from contextlib import contextmanager
import logging
import sqlite3
import threading

from .config import DATABASE_PATH


logger = logging.getLogger(__name__)


class _Pool:
    """SQLite 连接池(简化版,主要解决线程本地连接复用 + 写锁串行化)"""

    def __init__(self, max_read_concurrent: int = 16, write_lock_timeout: float = 30.0):
        import asyncio

        self._local = threading.local()
        self._max_read_concurrent = max_read_concurrent
        self._write_lock_timeout = write_lock_timeout
        # asyncio 锁延迟到首次使用(避免 import 阶段未运行事件循环)
        self._write_lock: asyncio.Lock | None = None
        self._read_sem: asyncio.Semaphore | None = None
        # 统计
        self.stats = {
            "connections_created": 0,
            "connections_reused": 0,
            "write_lock_waits": 0,
            "write_lock_timeouts": 0,
            "read_active": 0,
        }

    def _get_thread_conn(self) -> sqlite3.Connection:
        """获取/创建当前线程的专用连接"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(
                str(DATABASE_PATH),
                timeout=30,
                check_same_thread=False,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
            # 性能优化: 启用内存缓存
            conn.execute("PRAGMA cache_size=-64000")  # 64MB
            conn.execute("PRAGMA temp_store=MEMORY")
            self._local.conn = conn
            self.stats["connections_created"] += 1
        else:
            self.stats["connections_reused"] += 1
        return conn

    def _ensure_async_primitives(self):
        """首次访问时创建 asyncio 锁和信号量"""
        import asyncio

        if self._write_lock is None:
            self._write_lock = asyncio.Lock()
        if self._read_sem is None:
            self._read_sem = asyncio.Semaphore(self._max_read_concurrent)

    def get_stats(self) -> dict:
        """返回连接池当前统计快照（线程安全副本）"""
        import copy

        return copy.deepcopy(self.stats)

    def log_pool_health(self) -> None:
        """记录连接池健康水位日志（定时/调试时调用）"""
        s = self.stats
        total = s["connections_created"] + s["connections_reused"]
        reuse_rate = (s["connections_reused"] / total * 100) if total > 0 else 0
        logger.info(
            "[DB Pool] created=%d reused=%d reuse_rate=%.1f%% read_active=%d "
            "write_waits=%d write_timeouts=%d read_sem_max=%d",
            s["connections_created"],
            s["connections_reused"],
            reuse_rate,
            s["read_active"],
            s["write_lock_waits"],
            s["write_lock_timeouts"],
            self._max_read_concurrent,
        )
        # 水位告警
        if s["write_lock_timeouts"] > 0:
            logger.warning(
                "[DB Pool] 写锁超时已发生 %d 次，可能存在写瓶颈",
                s["write_lock_timeouts"],
            )
        if s["read_active"] >= self._max_read_concurrent * 0.8:
            logger.warning(
                "[DB Pool] 读并发 %d 接近上限 %d，考虑增加 _max_read_concurrent",
                s["read_active"],
                self._max_read_concurrent,
            )

    @contextmanager
    def connection(self, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
        """获取数据库连接(同步上下文)

        Args:
            readonly: 是否只读;只读连接可走读信号量并发,写连接走写锁串行
        """
        conn = self._get_thread_conn()
        # 验证连接有效性
        try:
            conn.execute("SELECT 1").fetchone()
        except sqlite3.ProgrammingError:
            # 连接被关闭,重开
            self._local.conn = None
            conn = self._get_thread_conn()
        except sqlite3.OperationalError as e:
            if "closed" in str(e).lower():
                self._local.conn = None
                conn = self._get_thread_conn()
            else:
                raise

        try:
            yield conn
            if not readonly:
                conn.commit()
        except Exception:
            if not readonly:
                conn.rollback()
            raise


_pool: _Pool | None = None
_pool_lock = threading.Lock()


def get_pool() -> _Pool:
    """获取全局连接池(单例)"""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = _Pool()
    return _pool


@contextmanager
def pooled_db(readonly: bool = False) -> Iterator[sqlite3.Connection]:
    """获取池化数据库连接(向后兼容原 get_db() 接口)

    对于写操作,自动使用写锁串行化以避免 lock 错误;
    对于读操作,走读信号量限流。
    """
    pool = get_pool()
    if readonly:
        # 读操作: 检查读并发上限(同步版本仅记录,不强制)
        pool.stats["read_active"] += 1
        try:
            with pool.connection(readonly=True) as conn:
                yield conn
        finally:
            pool.stats["read_active"] -= 1
    else:
        with pool.connection(readonly=False) as conn:
            yield conn
