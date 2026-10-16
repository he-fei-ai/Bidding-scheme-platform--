from contextlib import contextmanager
import json
import logging
import sqlite3
import threading
import time
from typing import Any

from .config import DATABASE_PATH, PROJECT_ROOT


# SQLite 写锁串行化（全局级，所有线程/协程共享）
# 解决高并发下 "database is locked" 错误：
# - WAL 模式允许并发读，但写操作仍需要串行
# - 使用 threading.RLock 允许同一线程重入（避免嵌套 get_db() 调用自锁）
# - 读操作不受影响（readonly=True 时跳过锁）
_db_write_lock = threading.RLock()
# 写锁获取超时：缩到 8 秒以在 SQLite 被杀毒扫描/外部进程短暂锁库时
# 快速失败并触发 get_db 内置的 I/O 重试（每次 0.5/1.0/1.5s，3 次约 3s），
# 而不是让后续 100+ 请求白等 60s 再一起超时。
_db_write_lock_timeout = 8.0

#: 进程内全局写锁争用的可重试异常。
#: BUG-FIX 2026-10-07：此前写锁超时抛的是裸 RuntimeError，而 `_is_io_error`
#: 只识别 sqlite3.OperationalError，于是这条**典型的瞬时争用**既不会被
#: query_retry / get_db_with_retry 重试，也不会宽带为 5xx 语义，直接变成 500。
#: 生产日志实证（logs/backend.log，8 次）：
#:   app.routers.sections:489 自动修复结果落库失败 project=990308: db locked
#: 成因是正文生成的 SSE 流水线持续写入 sections，而 `_clear_and_save_outline`
#: 是「DELETE + 批量 INSERT + 正文回填」的长事务，两者抢同一把进程级写锁。
#: 抽成专用类型后，重试装饰器能明确把它当作可重试的瞬时错误。
class DatabaseWriteLockTimeout(RuntimeError):
    """获取进程级数据库写锁超时（通常与并发写入争用有关，可安全重试）。"""

logger = logging.getLogger(__name__)

__all__ = [
    "DatabaseWriteLockTimeout",
    "get_db",
    "get_db_with_retry",
    "init_db",
    "query_retry",
]

# get_db / get_db_with_retry 共用的重入状态（线程本地）：
# depth>0 表示本线程已存在未退出的 db 上下文（嵌套时内层只借用连接，
# commit/丢弃/写锁释放由最外层统一负责）。
_db_ctx = threading.local()


# 死线程判定阈值（秒）：仅作为历史记录保留。
# BUG-FIX 2026-09-18（P1）：回收判据已改为「线程不在 threading.enumerate() 中
# 即回收」（枚举只含存活线程，不存在误杀 executor idle 线程的问题），
# 不再以空闲时长作为门槛；保留常量仅供旧引用/监控读取。
DEAD_THREAD_IDLE_SECONDS = 60.0

# 「存活但长期空闲」线程的连接抢占阈值（秒）：
# 连接池配额被**存活**线程（典型是 asyncio 默认 ThreadPoolExecutor 的 idle worker，
# 每新建一个事件循环就新起一批线程，旧线程永不退出）占满时，死线程回收救不回来
# ——实测全量测试正是这样打满 max=32 并抛 "Database connection pool exhausted"，
# 抛错之后整个进程的所有数据库操作永久失败、只能重启。
# 这里允许抢占空闲超过该阈值的连接，被抢占的线程通过世代号（generation）自行重建。
POOL_IDLE_RECLAIM_SECONDS = 30.0


class DBPool:
    """线程级 SQLite 连接池

    每个线程通过 threading.local() 缓存自己的连接，避免 SQLite 连接跨线程安全问题。
    支持最大连接数限制，并提供 close_all() 用于应用关闭时清理所有连接。

    V2.0 增强：
    - 连接健康检查（自动检测失效连接并重建）
    - 自动重试机制（数据库忙时等待并重试）
    - 连接使用统计
    - I/O 错误自动重试

    V2.1 增强（BUG #6 修复）：
    - 死线程连接回收：asyncio.to_thread 派发的 worker 线程在完成后
      会保留其 thread-local 连接在 _all_connections 中，导致连接数永久增长，
      直至超过 max_connections=16 触发 "Database connection pool exhausted"。
    - 修复方案：每次 acquire 时扫描 _all_connections，移除已终止的线程对应的连接。

    V2.2 增强（BUG #10 修复）：
    - ThreadPoolExecutor 的 idle 线程 is_alive() 仍为 True，原 reap 逻辑
      永远无法回收任何线程，连接池依然会耗尽。
    - 修复方案：以「是否还在 threading.enumerate() 中」为唯一死线程判据
      （2026-09-18 简化：去掉旧的空闲时长附加条件，见 _is_dead_thread）。
    """

    def __init__(self, max_connections: int = 10):
        self._local = threading.local()
        self._max_connections = max_connections
        self._lock = threading.RLock()
        self._all_connections: list[tuple[int, sqlite3.Connection]] = []  # (thread_id, conn)
        # BUG #10 修复：记录每个连接的最后使用时间（time.time()）
        self._last_used: dict[int, float] = {}
        self._stats = {
            "acquired": 0,
            "released": 0,
            "errors": 0,
            "io_retries": 0,
            "dead_thread_reaped": 0,
            "idle_reclaimed": 0,
        }
        self._thread_index: dict[int, sqlite3.Connection] = {}  # 线程ID -> 连接，避免每次扫描
        self._last_reap_time: float = 0.0  # BUG #1 修复：回收冷却时间戳
        # BUG-FIX 2026-09-18（高）：抢占「存活但长期空闲」线程的连接（generation 机制）。
        # _thread_epoch[thread_id] = 该线程连接当前的世代号；线程本地 _local.epoch 记录
        # 取连接时的世代号。二者不一致 ⇒ 连接已被抢占关闭，必须丢弃重建（否则本线程会
        # 拿到一个已 close 的连接）。抢占时只递增**被抢占线程**的世代号，不影响其他线程。
        self._thread_epoch: dict[int, int] = {}
        self._idle_reclaim_seconds: float = POOL_IDLE_RECLAIM_SECONDS
        # WAL 恢复信号：当检测到 WAL 损坏时设置，所有线程应在下次获取连接时刷新
        self._wal_recovery_event = threading.Event()
        # 恢复锁：确保只有一个线程执行恢复
        self._recovery_lock = threading.Lock()

    def _create_connection(self) -> sqlite3.Connection:
        """创建新的数据库连接"""
        db_path = str(DATABASE_PATH)
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=60000")
        return conn

    def _refresh_all_connections(self) -> None:
        """安全地刷新所有连接（仅在 _create_connection 失败时调用）

        当检测到 'file is not a database' 错误时，说明现有连接可能已失效。
        此方法仅：
        1. 关闭所有连接池中的连接
        2. 清空连接索引
        3. 设置恢复信号通知其他线程在下一次 get_connection 时自行刷新

        不触碰任何数据库文件（WAL/SHM），避免在活跃请求中间损坏数据库。
        SQLite 会在下次连接时自动重建 WAL 文件。
        """
        self._wal_recovery_event.set()
        logger.warning("[DBPool] 刷新所有连接（不修改数据库文件）")

        with self._lock:
            closed_count = 0
            for _, conn in self._all_connections:
                try:
                    conn.close()
                    closed_count += 1
                except Exception as _e:
                    logger.debug("[silent-except] db.py: line 113 - %s", _e)
            self._all_connections.clear()
            self._thread_index.clear()
            self._last_used.clear()
            self._local.connection = None

        logger.info("[DBPool] 已关闭 %d 个连接，等待线程按需重建", closed_count)

    def _is_thread_alive(self, thread_id: int) -> bool:
        """检查线程是否在当前活动线程列表中

        BUG #10 修复：原代码直接用 is_alive()，但 ThreadPoolExecutor 的 idle 线程
        也是 alive 状态，永远不会回收。现在判断是否在 threading.enumerate() 中。
        """
        active_thread_ids = {t.ident for t in threading.enumerate() if t.is_alive()}
        active_thread_ids.add(threading.main_thread().ident)
        return thread_id in active_thread_ids

    def _is_dead_thread(self, thread_id: int) -> bool:
        """判定死线程：不在当前活动线程列表中即为死线程

        BUG-FIX 2026-09-18（P1）：去掉旧的「且空闲超过 60s」附加条件。
        threading.enumerate() 只返回存活线程，ThreadPoolExecutor 的 idle
        线程同样在列，因此「不在列表中」⟺线程已终止，其 thread-local
        连接不可能再被任何代码路径使用；附加空闲条件只会让死线程连接
        在快速连续的场景（如测试套件/高并发短请求）里堆积到池上限。
        """
        return not self._is_thread_alive(thread_id)

    def _reap_dead_thread_connections(self, force: bool = False) -> int:
        """回收已终止线程持有的连接（BUG #6 + BUG #10 + BUG #1 修复）

        BUG-FIX 2026-09-18（高）：新增 `force` —— 为 True 时**跳过 10s 冷却**。
        调用方在判定「池已满、准备抛 exhausted」之前用它做最后一次抢救扫描，
        否则冷却期内死掉的线程所持有的连接明明可以回收，却被直接判死。

        BUG #1 增强：
        - 增加 _last_reap_time 冷却机制，避免每次 acquire 都扫描
        - 连接关闭操作移到锁外部，减少锁持有时间
        - 冷却时间设为 10s，避免高频扫描
        """
        reaped = 0
        now = time.time()

        # 冷却机制：每 10s 最多执行一次回收（force=True 时跳过，见方法 docstring）
        if (
            not force
            and hasattr(self, "_last_reap_time")
            and (now - self._last_reap_time) < 10.0
        ):
            return 0
        self._last_reap_time = now

        dead_connections = []
        with self._lock:
            current_thread_ids = {t.ident for t in threading.enumerate() if t.is_alive()}
            # 总是保留主线程的连接
            current_thread_ids.add(threading.main_thread().ident)

            new_all = []
            for thread_id, conn in self._all_connections:
                # BUG-FIX 2026-09-18（P1）：判据从「线程不在活跃列表 且 空闲>60s」
                # 改为「线程不在活跃列表」即回收：不在 enumerate() 中的线程必已
                # 终止（executor idle 线程仍在列，不会被误杀），其连接不可能再被
                # 使用，继续保留只会在快速连续建连的场景（TestClient 每用例新
                # 事件循环/每次 asyncio.to_thread 新 worker）里把池填到上限，
                # 实测表现为全量测试时 test_integration 报 pool exhausted(32/32)。
                is_in_active = thread_id in current_thread_ids

                if is_in_active:
                    new_all.append((thread_id, conn))
                else:
                    # 死线程持有的连接：标记为待关闭（移出列表，但 close 在锁外执行）
                    dead_connections.append((thread_id, conn))
                    self._thread_index.pop(thread_id, None)
                    self._last_used.pop(thread_id, None)
                    reaped += 1
            if reaped > 0:
                self._all_connections = new_all
                self._stats["dead_thread_reaped"] += reaped
                logger.info(
                    "[DBPool] 回收 %d 个死线程连接，当前活跃连接数=%d", reaped, len(new_all)
                )

        # 锁外执行 close，避免阻塞其他线程
        for thread_id, conn in dead_connections:
            try:
                conn.close()
            except Exception as _e:
                logger.debug("[silent-except] db.py: line 199 - %s", _e)
        return reaped

    def _reclaim_idle_connection(self, exclude_thread_id: int | None = None) -> int:
        """抢占一个「存活但长期空闲」线程持有的连接。

        BUG-FIX 2026-09-18（高）：死线程回收（_reap_dead_thread_connections）只能回收
        **已终止**线程的连接；而 asyncio 默认 ThreadPoolExecutor 的 idle worker 永远
        is_alive()，且每新建一个事件循环就会新起一批线程 —— 配额就是这样被永久占满的。
        实测全量测试打到 max=32 后抛 "Database connection pool exhausted"，
        **抛错之后整个进程的所有数据库操作永久失败、只能重启**，所以这里必须做最后抢救。

        安全性由世代号（generation）保证：抢占后只递增被抢占线程的世代号，该线程下次
        get_connection() 时发现 `_thread_epoch[tid] != _local.epoch`，会丢弃旧连接重建，
        不会拿到已 close 的连接；其他线程不受影响。

        抢占判据（避免误伤正在干活的连接）：
        1. 不是调用方自己（exclude_thread_id）；
        2. 不是主线程（主线程连接常驻，重建代价大且必然被立刻再次取用）；
        3. 空闲时长超过 _idle_reclaim_seconds（每次复用连接都会刷新 _last_used）。

        Returns:
            实际抢占的连接数（0 或 1）。
        """
        with self._lock:
            now = time.time()
            main_tid = threading.main_thread().ident
            victim: tuple[int, sqlite3.Connection] | None = None
            victim_idle = 0.0

            for thread_id, conn in self._all_connections:
                if thread_id == exclude_thread_id or thread_id == main_tid:
                    continue
                last_used = self._last_used.get(thread_id)
                if last_used is None:
                    idle = self._idle_reclaim_seconds + 1.0
                else:
                    idle = now - last_used
                    if idle <= self._idle_reclaim_seconds:
                        continue
                # 抢占「最久未用」的那一根，尽量少影响还在用的线程
                if victim is None or idle > victim_idle:
                    victim = (thread_id, conn)
                    victim_idle = idle

            if victim is None:
                return 0

            thread_id, conn = victim
            self._all_connections = [
                (tid, c) for tid, c in self._all_connections if c is not conn
            ]
            self._thread_index.pop(thread_id, None)
            self._last_used.pop(thread_id, None)
            self._thread_epoch[thread_id] = self._thread_epoch.get(thread_id, 0) + 1
            self._stats["idle_reclaimed"] += 1

        try:
            conn.close()
        except Exception as _e:
            logger.debug("[silent-except] db.py: _reclaim_idle_connection - %s", _e)

        logger.warning(
            "[DBPool] 配额不足，抢占空闲 %.0fs 的线程 %s 的连接（该线程下次会自行重建）",
            victim_idle,
            thread_id,
        )
        return 1

    def get_connection(self) -> sqlite3.Connection:
        """获取数据库连接（带健康检查、自动重试、WAL 恢复信号响应）"""
        current_thread_id = threading.get_ident()
        conn = getattr(self._local, "connection", None)
        # BUG-FIX：下面的 `raise ... from e` 位于 `with self._lock:` 块内，
        # 而 `e` 只在「已有连接但健康检查失败」的 except 分支里绑定。
        # 首次取连接（conn is None）时走到这里会抛 UnboundLocalError，
        # 把「连接池耗尽」这个真实原因掩盖掉。
        last_error: BaseException | None = None

        # 如果设置了 WAL 恢复信号，强制清除现有连接以触发重建
        if self._wal_recovery_event.is_set() and conn is not None:
            logger.info("[DBPool] WAL 恢复信号检测到，强制刷新线程 %d 的连接", current_thread_id)
            try:
                conn.close()
            except Exception as _e:
                logger.debug("[silent-except] db.py: line 213 - %s", _e)
            with self._lock:
                self._all_connections = [
                    (tid, c) for tid, c in self._all_connections if c is not conn
                ]
                self._thread_index.pop(current_thread_id, None)
                self._last_used.pop(current_thread_id, None)
            self._local.connection = None
            conn = None

        # BUG-FIX 2026-09-18：世代号校验 —— 本线程的连接若已被空闲抢占（见
        # _reclaim_idle_connection），它的 sqlite3 对象已被 close，必须丢弃重建，
        # 否则健康检查会在一个已关闭的连接上执行并抛 "Cannot operate on a closed database"。
        if conn is not None and getattr(self._local, "epoch", 0) != self._thread_epoch.get(
            current_thread_id, 0
        ):
            logger.info("[DBPool] 线程 %d 的连接已被空闲抢占，丢弃并重建", current_thread_id)
            stale_conn = conn
            try:
                stale_conn.close()
            except Exception as _e:
                logger.debug("[silent-except] db.py: epoch-stale - %s", _e)
            with self._lock:
                self._all_connections = [
                    (tid, c) for tid, c in self._all_connections if c is not stale_conn
                ]
                self._thread_index.pop(current_thread_id, None)
                self._last_used.pop(current_thread_id, None)
            self._local.connection = None
            conn = None

        # 检查现有连接是否有效
        if conn is not None:
            try:
                conn.execute("SELECT 1")  # 健康检查
                self._stats["acquired"] += 1
                # BUG #10 修复：更新最后使用时间
                with self._lock:
                    self._last_used[current_thread_id] = time.time()
                return conn
            except Exception as e:
                # 连接已失效，关闭并从连接池移除，避免配额泄漏
                dead_conn = conn
                try:
                    dead_conn.close()
                except Exception as _e:
                    logger.debug("[silent-except] db.py: line 238 - %s", _e)
                # 用锁保护，从 _all_connections 中移除失效连接，避免 max_connections 配额被永久占用
                with self._lock:
                    self._all_connections = [
                        (tid, c) for tid, c in self._all_connections if c is not dead_conn
                    ]
                    self._thread_index.pop(current_thread_id, None)
                    self._last_used.pop(current_thread_id, None)
                self._local.connection = None
                conn = None
                last_error = e

                # 如果是严重错误（如 file is not a database），触发全局刷新
                err_msg = str(e).lower()
                if any(kw in err_msg for kw in ("file is not a database", "file changed")):
                    self._refresh_all_connections()

        with self._lock:
            # BUG #1 修复：调用统一的回收方法（带冷却机制），代替内联的重复逻辑
            # 内联的重复逻辑导致：
            #   1. 代码维护困难（两处逻辑必须同步修改）
            #   2. 锁持有时间过长（close 在锁内执行）
            #   3. 回收频率过高（每次 acquire 都扫描）
            self._reap_dead_thread_connections()

            if len(self._all_connections) >= self._max_connections:
                # BUG-FIX 2026-09-18（高）：池满时**先强制再扫一次**再判失败。
                # `_reap_dead_thread_connections` 有 10s 冷却；若上一轮回收刚发生过，
                # 这期间死掉的线程持有的连接要等冷却结束才释放 —— 而本函数马上
                # 就要因"池满"抛错。实测全量测试正是这样挂掉的：明明有可回收的
                # 死线程连接，却因冷却被直接判死，日志里只剩
                # "Database connection pool exhausted (max=32, active=32)"，
                # 而**抛错之后整个进程的所有数据库操作永久失败、只能重启**。
                # 这里在真正放弃前强制扫一次（RLock 可重入，安全）。
                self._reap_dead_thread_connections(force=True)

            if len(self._all_connections) >= self._max_connections:
                # BUG-FIX 2026-09-18（高）：最后一道抢救 —— 抢占「存活但长期空闲」线程
                # 的连接。上面的 force 扫描只管已**终止**的线程，而占满配额的往往是
                # 永不退出的 ThreadPoolExecutor idle worker。被抢占的线程通过世代号
                # 自行重建（见 _reclaim_idle_connection），不会拿到已关闭的连接。
                self._reclaim_idle_connection(current_thread_id)

            if len(self._all_connections) >= self._max_connections:
                raise RuntimeError(
                    f"Database connection pool exhausted "
                    f"(max={self._max_connections}, active={len(self._all_connections)})"
                ) from last_error
            conn = self._create_connection()
            self._all_connections.append((current_thread_id, conn))
            self._thread_index[current_thread_id] = conn
            # BUG #10 修复：记录最后使用时间
            self._last_used[current_thread_id] = time.time()
            # BUG-FIX 2026-09-18：记住本世代号，供下次 get_connection 校验是否被抢占
            self._local.epoch = self._thread_epoch.get(current_thread_id, 0)
            self._local.connection = conn
            self._stats["acquired"] += 1
            return conn

    def discard_connection(self, conn: sqlite3.Connection | None = None) -> None:
        """彻底丢弃一个连接：关闭它并从池的所有索引中摘除。

        BUG-FIX（P0 连接泄漏）：`get_db()` / `get_db_with_retry()` 的异常分支此前
        只做 `rollback()` + 把 `_local.connection` 置 None，**既没有 close 也没有从
        `_all_connections` 里移除**。由于该 thread_id 属于当前存活线程，
        `_reap_dead_thread_connections` 的 `is_in_active` 判据会永久保留它，
        于是泄漏的连接永不回收。累积到 `max_connections`(32) 后，
        `get_connection()` 就会抛 “connection pool exhausted”，
        **整个进程的所有数据库操作永久失败，只能重启**。

        而 `task_lock.acquire` 冲突、 `task_snapshot.register` 冲突这类
        「正常的业务互斥」正是在 `with get_db()` 体内 raise，用户每点一次被拦截的
        生成按钮就泄漏一个连接。
        """
        if conn is None:
            conn = getattr(self._local, "connection", None)
        if conn is None:
            self._local.connection = None
            return

        try:
            conn.rollback()
        except Exception as _e:
            logger.debug("[silent-except] db.py discard.rollback - %s", _e)
        try:
            conn.close()
        except Exception as _e:
            logger.debug("[silent-except] db.py discard.close - %s", _e)

        thread_id = threading.get_ident()
        with self._lock:
            self._all_connections = [
                (tid, c) for tid, c in self._all_connections if c is not conn
            ]
            self._thread_index.pop(thread_id, None)
            self._last_used.pop(thread_id, None)
        # BUG-FIX 2026-09-18：同步世代号，本线程下次取连接时按当前世代重建
        self._local.epoch = self._thread_epoch.get(thread_id, 0)
        self._local.connection = None
        self._stats["released"] += 1

    def get_stats(self) -> dict:
        """获取连接池统计信息"""
        return {
            **self._stats,
            "active_connections": len(self._all_connections),
            "max_connections": self._max_connections,
        }

    def close_all(self):
        """关闭所有连接"""
        with self._lock:
            for _, conn in self._all_connections:
                try:
                    conn.close()
                except Exception as _e:
                    logger.debug("[silent-except] db.py: line 291 - %s", _e)
            self._all_connections.clear()
            self._thread_index.clear()
            self._last_used.clear()
            self._thread_epoch.clear()
            self._local.connection = None
            self._stats = {
                "acquired": 0,
                "released": 0,
                "errors": 0,
                "io_retries": 0,
                "dead_thread_reaped": 0,
                "idle_reclaimed": 0,
            }


# BUG #1 修复：从 16 增加到 32，应对并发正文生成时的数据库连接需求
# 正文生成使用 asyncio.to_thread 派发多个 worker 线程，每个 worker 需要一个连接
# 默认 concurrency=4，加上 SSE 处理、心跳、其他 API 请求，容易超过 16
_pool = DBPool(max_connections=32)


def get_pool() -> DBPool:
    return _pool


def _get_connection() -> sqlite3.Connection:
    return _pool.get_connection()


def safe_update(
    table: str, updates: dict[str, Any], where_col: str, where_val: Any
) -> tuple[str, list]:
    # 过滤内部字段（不写入数据库）
    updates = {k: v for k, v in updates.items() if not k.startswith("_")}
    valid_cols = _TABLE_COLUMNS.get(table) or _TABLE_COLUMNS.setdefault(
        table,
        _load_table_columns(table),
    )
    invalid = [k for k in updates if k not in valid_cols]
    if invalid:
        raise ValueError(f"Invalid column names for {table}: {invalid}")
    set_clause = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [where_val]
    return f"UPDATE {table} SET {set_clause} WHERE {where_col} = ?", values


_TABLE_COLUMNS: dict[str, set[str]] = {}


def _load_table_columns(table: str) -> set[str]:
    conn = _get_connection()
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _is_io_error(exc: Exception) -> bool:
    """检查是否是「可重试」的数据库瞬时错误。

    包含两类：
      1. SQLite 自身报告的 I/O / 锁冲突（OperationalError）；
      2. 本模块进程级写锁争用的超时（DatabaseWriteLockTimeout）——
         BUG-FIX 2026-10-07：见该类上方的日志实证说明。
    """
    if isinstance(exc, DatabaseWriteLockTimeout):
        return True
    if isinstance(exc, sqlite3.OperationalError):
        error_msg = str(exc).lower()
        return any(
            kw in error_msg
            for kw in (
                "disk i/o error",
                "io error",
                "database is locked",
                "locked",
                "file is not a database",
                "file changed",
            )
        )
    return False


def query_retry(func=None, *, max_retries: int = 3, retry_delay: float = 0.5):
    """查询重试装饰器：当 get_db_with_retry 内部的查询发生 I/O 错误时自动重试整个块

    支持两种写法::

        @query_retry
        def _fetch(): ...

        @query_retry(max_retries=3, delay=0.5)
        def _fetch(): ...

    BUG-FIX 2026-09-16：旧实现从 ``kwargs`` 里 pop ``max_retries`` / ``retry_delay``
    来读配置，会**吞掉被包装函数的同名实参**（目标函数有这两个形参时静默少参，
    轻则取到默认值、重则 TypeError）。改为从装饰器闭包读配置，不再动 kwargs。
    """
    import functools

    def decorator(f):
        @functools.wraps(f)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try:
                    return f(*args, **kwargs)
                except Exception as e:
                    if _is_io_error(e) and attempt < max_retries - 1:
                        logger.warning(
                            "查询 I/O 错误，第 %d/%d 次重试: %s", attempt + 1, max_retries, e
                        )
                        time.sleep(retry_delay * (attempt + 1))
                    else:
                        raise
            raise RuntimeError("查询重试 %d 次后仍失败" % max_retries)

        return wrapper

    if func is None:
        return decorator
    return decorator(func)


def _acquire_healthy_connection(
    attempts: int = 3, retry_delay: float = 0.5, count_retry_stat: bool = False
) -> sqlite3.Connection:
    """取一条连接并做 `SELECT 1` 探活，遇到 I/O 错误按 attempts 重试。

    抽成独立函数的目的：让 `get_db` / `get_db_with_retry` 的重入深度
    （`_db_ctx.depth`）恢复逻辑能统一放在**最外层 try/finally** 里。
    此前取连接失败会在 try/finally **之外** raise，线程本地的 depth 永远回不去，
    该线程之后所有的 get_db 都被当成「嵌套内层」—— 既不 commit 也不归还连接，
    写入静默丢失，且会逐渐耗尽连接池。
    """
    conn = None
    for attempt in range(attempts):
        try:
            conn = _get_connection()
            conn.execute("SELECT 1")
            return conn
        except Exception as e:
            if _is_io_error(e) and attempt < attempts - 1:
                if count_retry_stat:
                    _pool._stats["io_retries"] += 1
                logger.warning(
                    "数据库 I/O 错误，第 %d/%d 次重试: %s", attempt + 1, attempts, e
                )
                time.sleep(retry_delay * (attempt + 1))
                if conn is not None:
                    # BUG-FIX 2026-09-18（中）：此前只 close + 清 _local，连接仍留在
                    # `_all_connections` 里占着配额（本线程存活，死线程回收也救不回来）。
                    # 走 discard_connection 才会真正从池的所有索引中摘除。
                    _pool.discard_connection(conn)
                conn = None
            else:
                raise
    raise RuntimeError("获取数据库连接失败")


@contextmanager
def get_db(readonly: bool = False):
    """获取数据库连接（自动重试 I/O 错误，向后兼容）

    V2.0 增强：增加全局写锁串行化，解决高并发下 SQLite "database is locked" 错误。
    - 写操作（任何非 SELECT 语句）通过 threading.RLock 串行
    - 读操作不受影响（readonly=True 时跳过写锁）
    - 写锁超时后抛出 DatabaseWriteLockTimeout，避免死锁。超时值是**可配置的**
      ``_db_write_lock_timeout``（当前 8.0s，见模块顶部），不是硬编码常量；
      改超时请改配置项，不要在本 docstring 里再抄一份数字。

    BUG-FIX 2026-09-16（中）：重入（嵌套）支持。连接按线程缓存，同线程嵌套
    `with get_db()` 拿到的是**同一个连接**；旧实现内层退出即 `conn.commit()`
    （把外层未完成的写入一并提交），内层异常分支还会 `discard_connection`
    close 掉外层仍在使用的连接。现在用线程本地位记录嵌套深度：
    只有**最外层**负责 commit / 丢弃连接 / 释放写锁，内层只是借用。
    注意：内层块内**禁止**执行耗时操作（外层写锁仍被本线程持有）。

    临界区约定（HARDEN 2026-10-11·R-11）：写锁只串行化**单个事务块**，
    **不覆盖块与块之间的间隙**。因此任何「读 → 内存修改 → 写」必须落在同一个
    ``with get_db()`` 块内，拆成两个块会产生 lost update（A 读空快照 → B 读空
    快照 → A 写 → B 写覆盖 A）。锁内 I/O 还会把持锁时长推过
    ``_db_write_lock_timeout``（当前 8.0s），让争用方超时失败。范例与三条
    完整约定见 ``services/consistency_backflow.py`` 模块 docstring。

    BUG-FIX 2026-09-16（P1）：depth 的恢复放在**覆盖全流程**的 finally 里。
    旧实现在 try 之前自增 depth，而取连接失败发生在 try 之外 —— 一旦重试耗尽
    抛异常，depth 永远停在 +1，该线程之后所有的写操作都「不提交、不归还连接」。
    """
    depth = getattr(_db_ctx, "depth", 0)
    _db_ctx.depth = depth + 1
    outermost = depth == 0

    conn = None
    write_locked = False
    try:
        conn = _acquire_healthy_connection(attempts=3, retry_delay=0.5)

        try:
            # 获取写锁（仅写操作需要；readonly=True 跳过以允许并发读）
            # 重入时外层已持有写锁（RLock 可重入，此处直接跳过，避免计数错配）
            if not readonly:
                # BUG-FIX：获取写锁失败若被吞掉，后面的写操作会在**无锁保护**下继续
                # 执行，写串行化失效（此前非 RuntimeError 异常被静默忽略）。
                acquired = _db_write_lock.acquire(timeout=_db_write_lock_timeout)
                if not acquired:
                    raise DatabaseWriteLockTimeout(
                        f"获取数据库写锁超时（{_db_write_lock_timeout}秒），可能存在并发写入"
                    )
                write_locked = True

            yield conn
            if outermost:
                # 只有最外层提交：内层退出不再提前提交外层未完成的事务
                conn.commit()
        except Exception:
            # BUG-FIX（P0）：异常退出时必须把连接真正关闭并移出池，不能只清 _local，
            # 否则该连接会永久占用 max_connections 配额（详见 DBPool.discard_connection）。
            # 注意：正常（无异常）路径不能丢弃，连接由线程复用。
            # 重入时只有最外层负责丢弃（内层异常向上传播，由外层统一收尾）。
            if outermost:
                _pool.discard_connection(conn)
            raise
        finally:
            if write_locked:
                try:
                    _db_write_lock.release()
                except Exception as _e:
                    logger.debug("[silent-except] db.py write-lock release - %s", _e)
    finally:
        _db_ctx.depth = depth


@contextmanager
def get_db_with_retry(
    max_retries: int = 3, retry_delay: float = 0.5, readonly: bool = False
):
    """获取数据库连接并执行操作，遇到 I/O 错误时自动重试

    用法:
        with get_db_with_retry() as conn:
            conn.execute("SELECT ...")

    ENHANCE 2026-09-16（并发）：新增 `readonly` 形参。与 `get_db` 对齐，纯读查询
    传 readonly=True 可跳过全局写锁 —— 此前这里**无条件**抢写锁，导致热路径上的
    只读请求（ai_config 读取、generation_manager 持久化前的探查等）被单把锁串行化，
    而 get_db(readonly=True) 的并发优化在这一侧完全失效。
    """
    # BUG-FIX（P1）：此前完全绕过 `_db_write_lock`，导致「写串行化」只对
    # get_db() 生效，而热路径（generation_manager 的进度/任务持久化、
    # rollback_dirty_sections 等）走的都是这里。争用时只能靠 SQLite 的
    # busy_timeout 硬等，设计中的快速失败形同虚设。
    # BUG-FIX 2026-09-16（中）：重入语义与 get_db 一致 —— 嵌套时只有最外层
    # 负责 commit / 丢弃连接 / 释放写锁（见 get_db docstring）。
    # BUG-FIX 2026-09-16（P1）：取连接失败不再泄漏 depth（同 get_db）。
    depth = getattr(_db_ctx, "depth", 0)
    _db_ctx.depth = depth + 1
    outermost = depth == 0

    conn = None
    write_locked = False
    try:
        # 先循环重试获取健康连接，确保 yield 只发生一次（@contextmanager 要求生成器只能 yield 一次）
        conn = _acquire_healthy_connection(
            attempts=max_retries, retry_delay=retry_delay, count_retry_stat=True
        )

        try:
            if not readonly:
                acquired = _db_write_lock.acquire(timeout=_db_write_lock_timeout)
                if not acquired:
                    raise DatabaseWriteLockTimeout(
                        f"获取数据库写锁超时（{_db_write_lock_timeout}秒），可能存在并发写入"
                    )
                write_locked = True
        except Exception:
            if outermost:
                _pool.discard_connection(conn)
            raise

        yield conn
        # 查询执行后再次健康检查，防止查询过程中连接变为无效
        # 这修复了"health check 通过但实际查询报 disk I/O error"的问题
        try:
            conn.execute("SELECT 1")
        except Exception as post_check_err:
            if _is_io_error(post_check_err):
                logger.warning("查询后健康检查失败，关闭连接: %s", post_check_err)
            # BUG-FIX 2026-09-16（高）：内层不能丢弃外层的连接 —— 嵌套调用时内层
            # close 掉连接，外层继续用会 "attempt to write a readonly database"，
            # 整个外层事务丢失。与其余分支一致，只由最外层负责回收。
            if outermost:
                _pool.discard_connection(conn)
            raise
        if outermost:
            conn.commit()
    except Exception:
        # BUG-FIX（P0）：同 get_db()，异常退出必须真正释放连接，不能只清 _local
        # 重入时只有最外层负责丢弃
        if outermost:
            _pool.discard_connection(conn)
        raise
    finally:
        if write_locked:
            try:
                _db_write_lock.release()
            except Exception as _e:
                logger.debug("[silent-except] db.py write-lock release - %s", _e)
        _db_ctx.depth = depth


def init_db():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                industry TEXT NOT NULL DEFAULT '',
                bidder_name TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                current_step INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            );

            CREATE TABLE IF NOT EXISTS uploaded_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                filename TEXT NOT NULL,
                path TEXT NOT NULL,
                file_type TEXT NOT NULL,
                file_role TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                extracted_text_path TEXT,
                message TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS analysis (
                project_id INTEGER PRIMARY KEY,
                summary_json TEXT NOT NULL DEFAULT '{}',
                scoring_points_json TEXT NOT NULL DEFAULT '[]',
                confirmed_summary INTEGER NOT NULL DEFAULT 0,
                confirmed_scoring INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS usage_limits (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                total_limit INTEGER NOT NULL DEFAULT 10,
                used_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS project_usage (
                project_id INTEGER PRIMARY KEY,
                consumed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
        """)
        _migrate(conn)

        # BUG-FIX 2026-09-16（中）：这两条 DDL 原先写在 `with get_db()` 块**外**，
        # 用的是已经退出的上下文里的 conn —— 既不持有写锁（与并发迁移竞争），
        # 失败时也不会走 discard_connection 让坏连接出池。并入同一事务即可。
        try:
            conn.execute("ALTER TABLE analysis ADD COLUMN scope TEXT DEFAULT NULL")
        except sqlite3.OperationalError:
            pass
        # ENHANCE 2026-09-15：全局事实的「缺失值策略」。
        # 历史行为（唯一一种）等价于 fabricate —— 资料没给值就由 AI 编一个合理值，
        # 风险是虚构的人名/工期/金额会被当成确定性事实写进全篇标书。
        # 现在允许用户选择 omit（不编造，写笼统承诺）/ placeholder（留【待填写】占位）。
        try:
            conn.execute(
                "ALTER TABLE analysis ADD COLUMN facts_policy TEXT NOT NULL DEFAULT 'fabricate'"
            )
        except sqlite3.OperationalError:
            pass
        # ENHANCE 2026-10-07：结构化提取项目字段表（正文生成 extraction_fields
        # 显式直供通道的持久化载体，随 global_facts_json 同步覆写）。
        try:
            conn.execute(
                "ALTER TABLE analysis ADD COLUMN extraction_fields_json TEXT NOT NULL DEFAULT ''"
            )
        except sqlite3.OperationalError:
            pass

    seed_prompt_templates()

    # 同步执行增强功能数据库迁移（generation_tasks 等表）
    # BUG 修复：此前仅在 enhancement_orchestrator 启动后台任务中异步调用，
    # 超时（30s）或初始化失败时会被静默跳过，导致线上库 generation_tasks
    # 保持旧表结构（缺 generation_id/outline_version 等列）、
    # section_generation_progress 表缺失，正文生成暂停/恢复功能失效。
    from .db_migrations import clean_legacy_outline_title_suffixes, ensure_enhancement_tables

    ensure_enhancement_tables()

    # 2026-10-11：清理旧版全树去重写进库的「（其2）（其3）…」式无意义标题后缀。
    # 幂等；缺表/坏 JSON 均静默跳过，失败不影响启动。
    with get_db() as conn:
        clean_legacy_outline_title_suffixes(conn)


def _migrate(conn: sqlite3.Connection):
    try:
        conn.execute("ALTER TABLE projects ADD COLUMN industry_id TEXT DEFAULT NULL")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE analysis ADD COLUMN scope TEXT DEFAULT NULL")
    except sqlite3.OperationalError:
        pass
    # ENHANCE 2026-10-07：结构化提取项目字段表（老库补列）
    try:
        conn.execute(
            "ALTER TABLE analysis ADD COLUMN extraction_fields_json TEXT NOT NULL DEFAULT ''"
        )
    except sqlite3.OperationalError:
        pass

    _ensure_table(
        conn,
        "sections",
        """
        CREATE TABLE sections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            parent_id INTEGER DEFAULT NULL,
            title TEXT NOT NULL,
            level INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0,
            section_type TEXT NOT NULL DEFAULT 'text',
            scoring_point_id TEXT DEFAULT NULL,
            ai_prompt_snapshot TEXT DEFAULT NULL,
            flowchart_json TEXT DEFAULT NULL,
            gantt_json TEXT DEFAULT NULL,
            illustration_type TEXT DEFAULT NULL,
            illustration_data TEXT DEFAULT NULL,
            content TEXT NOT NULL DEFAULT '',
            target_words INTEGER DEFAULT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            graph_suitable INTEGER NOT NULL DEFAULT 0,
            suggested_chart TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY(parent_id) REFERENCES sections(id) ON DELETE CASCADE
        )
    """,
        {
            "project_id": "INTEGER NOT NULL DEFAULT 0",
            "parent_id": "INTEGER DEFAULT NULL",
            "title": "TEXT NOT NULL DEFAULT ''",
            "level": "INTEGER NOT NULL DEFAULT 1",
            "sort_order": "INTEGER NOT NULL DEFAULT 0",
            "section_type": "TEXT NOT NULL DEFAULT 'text'",
            "scoring_point_id": "TEXT DEFAULT NULL",
            "ai_prompt_snapshot": "TEXT DEFAULT NULL",
            "flowchart_json": "TEXT DEFAULT NULL",
            "gantt_json": "TEXT DEFAULT NULL",
            "illustration_type": "TEXT DEFAULT NULL",
            "illustration_data": "TEXT DEFAULT NULL",
            "content": "TEXT NOT NULL DEFAULT ''",
            "target_words": "INTEGER DEFAULT NULL",
            "status": "TEXT NOT NULL DEFAULT 'pending'",
            "graph_suitable": "INTEGER NOT NULL DEFAULT 0",
            "suggested_chart": "TEXT NOT NULL DEFAULT ''",
        },
    )

    _ensure_table(
        conn,
        "exports",
        """
        CREATE TABLE exports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            file_path TEXT NOT NULL,
            file_size INTEGER NOT NULL DEFAULT 0,
            config_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'completed',
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        )
    """,
        {
            "project_id": "INTEGER NOT NULL DEFAULT 0",
            "filename": "TEXT NOT NULL DEFAULT ''",
            "file_path": "TEXT NOT NULL DEFAULT ''",
            "file_size": "INTEGER NOT NULL DEFAULT 0",
            "config_json": "TEXT NOT NULL DEFAULT '{}'",
            "status": "TEXT NOT NULL DEFAULT 'completed'",
        },
    )

    _ensure_table(
        conn,
        "ai_config",
        """
        CREATE TABLE ai_config (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            provider_name TEXT NOT NULL DEFAULT '',
            provider TEXT NOT NULL DEFAULT 'openai_compatible',
            api_key TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT 'gpt-3.5-turbo',
            base_url TEXT NOT NULL DEFAULT 'https://api.openai.com/v1',
            document_profile TEXT NOT NULL DEFAULT '',
            concurrency INTEGER NOT NULL DEFAULT 4,
            temperature REAL NOT NULL DEFAULT 0.7,
            max_tokens INTEGER NOT NULL DEFAULT 4096,
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """,
        {
            "provider_name": "TEXT NOT NULL DEFAULT ''",
            "provider": "TEXT NOT NULL DEFAULT 'openai_compatible'",
            "api_key": "TEXT NOT NULL DEFAULT ''",
            "model": "TEXT NOT NULL DEFAULT 'gpt-3.5-turbo'",
            "base_url": "TEXT NOT NULL DEFAULT 'https://api.openai.com/v1'",
            "document_profile": "TEXT NOT NULL DEFAULT ''",
            "concurrency": "INTEGER NOT NULL DEFAULT 4",
            "temperature": "REAL NOT NULL DEFAULT 0.7",
            "max_tokens": "INTEGER NOT NULL DEFAULT 4096",
        },
    )

    _ensure_table(
        conn,
        "prompt_templates",
        """
        CREATE TABLE prompt_templates (
            key TEXT PRIMARY KEY,
            category TEXT NOT NULL DEFAULT '',
            label TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            default_content TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """,
        {
            "category": "TEXT NOT NULL DEFAULT ''",
            "label": "TEXT NOT NULL DEFAULT ''",
            "content": "TEXT NOT NULL DEFAULT ''",
            "default_content": "TEXT NOT NULL DEFAULT ''",
        },
    )

    # ========== 提示词版本历史表（版本控制与回滚）==========
    # 增强 2026-09-29：prompt_templates 仅存当前值，编辑即覆盖，用户误删占位符或
    # 改坏内容后无法恢复（reset 只能回到硬编码默认，自定义版本永久丢失）。
    # 该表在每次 update/reset/rollback 前归档被覆盖的**旧内容**，支持版本列表查询
    # 与按版本回滚。key 非外键（允许硬编码新增 key 尚无 DB 行时也能记录历史）。
    _ensure_table(
        conn,
        "prompt_versions",
        """
        CREATE TABLE prompt_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            default_content TEXT NOT NULL DEFAULT '',
            label TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'edit',
            changed_by TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """,
        {
            "key": "TEXT NOT NULL DEFAULT ''",
            "content": "TEXT NOT NULL DEFAULT ''",
            "default_content": "TEXT NOT NULL DEFAULT ''",
            "label": "TEXT NOT NULL DEFAULT ''",
            "category": "TEXT NOT NULL DEFAULT ''",
            "source": "TEXT NOT NULL DEFAULT 'edit'",
            "changed_by": "TEXT NOT NULL DEFAULT ''",
        },
    )
    # 历史查询索引：按 key 取最新版本
    try:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_prompt_versions_key "
            "ON prompt_versions (key, id DESC)"
        )
    except Exception:
        pass

    # ========== 废标检查模块 ==========
    _ensure_table(
        conn,
        "rejection_checks",
        """
        CREATE TABLE rejection_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER,
            tender_text TEXT NOT NULL DEFAULT '',
            bid_documents TEXT NOT NULL DEFAULT '[]',
            check_items TEXT NOT NULL DEFAULT '[]',
            enabled_dimensions TEXT NOT NULL DEFAULT '["rejection","spelling","logic"]',
            status TEXT NOT NULL DEFAULT 'pending',
            progress INTEGER NOT NULL DEFAULT 0,
            progress_message TEXT NOT NULL DEFAULT '',
            results TEXT NOT NULL DEFAULT '{}',
            false_positives TEXT NOT NULL DEFAULT '[]',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """,
        {
            "project_id": "INTEGER",
            "tender_text": "TEXT NOT NULL DEFAULT ''",
            "bid_documents": "TEXT NOT NULL DEFAULT '[]'",
            "check_items": "TEXT NOT NULL DEFAULT '[]'",
            "enabled_dimensions": 'TEXT NOT NULL DEFAULT \'["rejection","spelling","logic"]\'',
            "status": "TEXT NOT NULL DEFAULT 'pending'",
            "progress": "INTEGER NOT NULL DEFAULT 0",
            "progress_message": "TEXT NOT NULL DEFAULT ''",
            "results": "TEXT NOT NULL DEFAULT '{}'",
            "false_positives": "TEXT NOT NULL DEFAULT '[]'",
            "error": "TEXT NOT NULL DEFAULT ''",
        },
    )

    # AUDIT 2026-09-18（空任务排查）：`rejection_templates` 是**死表** ——
    # 全库（含前端）没有任何 INSERT / SELECT / UPDATE / DELETE，也没有对应路由。
    # 它是为「废标项模板库」预留的表结构，该功能从未落地。
    # 保留建表（删除属于破坏性变更，且老库可能已有数据），但**不要据此认为
    # 存在模板管理功能**；若要实现该功能，需同时补 service 层与路由层。
    _ensure_table(
        conn,
        "rejection_templates",
        """
        CREATE TABLE rejection_templates (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT '废标',
            is_custom INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """,
        {
            "name": "TEXT NOT NULL DEFAULT ''",
            "description": "TEXT NOT NULL DEFAULT ''",
            "category": "TEXT NOT NULL DEFAULT '废标'",
            "is_custom": "INTEGER NOT NULL DEFAULT 0",
        },
    )

    # ========== 标书查重模块 ==========
    _ensure_table(
        conn,
        "duplicate_checks",
        """
        CREATE TABLE duplicate_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER,
            file_ids TEXT NOT NULL DEFAULT '[]',
            tender_file_id INTEGER,
            signature TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            progress INTEGER NOT NULL DEFAULT 0,
            progress_message TEXT NOT NULL DEFAULT '',
            results TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """,
        {
            # BUG-FIX 2026-09-18（P1）：查重任务此前不归属项目，/duplicate/tasks
            # 全站混排，任何人都能看到/猜测他人查重任务。新增列（存量库由
            # _ensure_table 自动 ALTER）。
            "project_id": "INTEGER",
            "file_ids": "TEXT NOT NULL DEFAULT '[]'",
            "tender_file_id": "INTEGER",
            "signature": "TEXT NOT NULL DEFAULT ''",
            "status": "TEXT NOT NULL DEFAULT 'pending'",
            "progress": "INTEGER NOT NULL DEFAULT 0",
            "progress_message": "TEXT NOT NULL DEFAULT ''",
            "results": "TEXT NOT NULL DEFAULT '{}'",
            "error": "TEXT NOT NULL DEFAULT ''",
        },
    )

    # ========== 可行性研究报告模块（ENHANCE 2026-09-15，移植自易标 feasibility-report） ==========
    # 单工作区设计（meta 恒为 id=1）：可研报告是「一份报告一份工作区」的文档型流程，
    # 与废标/查重的多任务并行模型不同，参考实现即单行 meta + 节点表 + 任务表。
    _ensure_table(
        conn,
        "feasibility_meta",
        """
        CREATE TABLE feasibility_meta (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            step TEXT NOT NULL DEFAULT 'materials',
            project_info_json TEXT,
            source_files_json TEXT,
            analysis_markdown TEXT,
            outline_template TEXT NOT NULL DEFAULT 'government',
            target_words INTEGER NOT NULL DEFAULT 30000,
            key_parameters_markdown TEXT,
            outline_project_name TEXT,
            outline_project_overview TEXT,
            reference_document_ids_json TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """,
        {
            "key_parameters_markdown": "TEXT",
            "outline_project_name": "TEXT",
            "outline_project_overview": "TEXT",
            # ENHANCE 2026-09-15：参考知识库文档 id（可研选材用，易标 reference_document_ids_json 同语义）
            "reference_document_ids_json": "TEXT",
        },
    )

    _ensure_table(
        conn,
        "feasibility_outline_nodes",
        """
        CREATE TABLE feasibility_outline_nodes (
            node_id TEXT PRIMARY KEY,
            parent_node_id TEXT,
            sort_order INTEGER NOT NULL DEFAULT 0,
            level INTEGER NOT NULL DEFAULT 1,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            knowledge_item_ids_json TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """,
        {
            # ENHANCE 2026-09-15：章节精确指定的知识条目 id（覆盖自动选材）
            "knowledge_item_ids_json": "TEXT",
        },
    )

    _ensure_table(
        conn,
        "feasibility_sources",
        """
        CREATE TABLE feasibility_sources (
            id TEXT PRIMARY KEY,
            file_name TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            content_hash TEXT NOT NULL DEFAULT '',
            content_chars INTEGER NOT NULL DEFAULT 0,
            imported_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """,
        {},
    )

    _ensure_table(
        conn,
        "feasibility_tasks",
        """
        CREATE TABLE feasibility_tasks (
            type TEXT PRIMARY KEY,
            task_id TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'running',
            progress INTEGER NOT NULL DEFAULT 0,
            stats_json TEXT,
            error TEXT,
            started_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """,
        {},
    )

    # ========== 知识库模块 ==========
    _ensure_table(
        conn,
        "knowledge_documents",
        """
        CREATE TABLE knowledge_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL,
            file_path TEXT NOT NULL,
            file_type TEXT,
            industry TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            step_log TEXT NOT NULL DEFAULT '[]',
            markdown_path TEXT,
            blocks TEXT NOT NULL DEFAULT '[]',
            candidates TEXT NOT NULL DEFAULT '[]',
            match_result TEXT NOT NULL DEFAULT '{}',
            coverage REAL NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """,
        {
            "filename": "TEXT NOT NULL",
            "file_path": "TEXT NOT NULL",
            "file_type": "TEXT",
            "industry": "TEXT NOT NULL DEFAULT ''",
            "status": "TEXT NOT NULL DEFAULT 'pending'",
            "step_log": "TEXT NOT NULL DEFAULT '[]'",
            "markdown_path": "TEXT",
            "blocks": "TEXT NOT NULL DEFAULT '[]'",
            "candidates": "TEXT NOT NULL DEFAULT '[]'",
            "match_result": "TEXT NOT NULL DEFAULT '{}'",
            "coverage": "REAL NOT NULL DEFAULT 0",
        },
    )

    _ensure_table(
        conn,
        "knowledge_items",
        """
        CREATE TABLE knowledge_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            summary TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            keywords TEXT NOT NULL DEFAULT '[]',
            industry TEXT NOT NULL DEFAULT '',
            tags TEXT NOT NULL DEFAULT '[]',
            source_doc_id INTEGER,
            source_ref TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """,
        {
            "title": "TEXT NOT NULL",
            "summary": "TEXT NOT NULL DEFAULT ''",
            "content": "TEXT NOT NULL DEFAULT ''",
            "keywords": "TEXT NOT NULL DEFAULT '[]'",
            "industry": "TEXT NOT NULL DEFAULT ''",
            "tags": "TEXT NOT NULL DEFAULT '[]'",
            "source_doc_id": "INTEGER",
            "source_ref": "TEXT NOT NULL DEFAULT ''",
        },
    )

    _ensure_table(
        conn,
        "knowledge_usage",
        """
        CREATE TABLE knowledge_usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL,
            project_id INTEGER,
            section_id INTEGER,
            used_in TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """,
        {
            "item_id": "INTEGER NOT NULL",
            "project_id": "INTEGER",
            "section_id": "INTEGER",
            "used_in": "TEXT NOT NULL DEFAULT ''",
        },
    )

    _ensure_column(conn, "projects", "bidder_name", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "projects", "industry", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "projects", "notes", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "projects", "current_step", "INTEGER NOT NULL DEFAULT 1")
    # BUG-FIX 2026-09-18（P1）：created_by 此前"有读无写无列"——
    # sections/tasks 的权限检查 PRAGMA 检测到列不存在就整体跳过项目归属校验，
    # 多用户隔离形同虚设。补列（迁移）并在 create_project 写入当前用户。
    _ensure_column(conn, "projects", "created_by", "INTEGER")
    _ensure_column(conn, "uploaded_files", "file_role", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "uploaded_files", "extracted_text_path", "TEXT")

    _ensure_column(conn, "knowledge_documents", "industry_tree_id", "TEXT DEFAULT NULL")
    _ensure_column(conn, "knowledge_items", "industry_tree_id", "TEXT DEFAULT NULL")

    _ensure_table(
        conn,
        "industry_tree_audit",
        """
        CREATE TABLE IF NOT EXISTS industry_tree_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_id TEXT NOT NULL,
            action TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            changed_by TEXT DEFAULT 'system',
            changed_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY(node_id) REFERENCES industry_tree(id)
        )
    """,
        {
            "node_id": "TEXT NOT NULL",
            "action": "TEXT NOT NULL",
            "old_value": "TEXT",
            "new_value": "TEXT",
            "changed_by": "TEXT NOT NULL DEFAULT 'system'",
            "changed_at": "TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))",
        },
    )

    audit_fks = conn.execute("PRAGMA foreign_key_list(industry_tree_audit)").fetchall()
    if any(fk["table"] == "industry_tree" for fk in audit_fks):
        conn.executescript(
            """
            PRAGMA foreign_keys=OFF;
            CREATE TABLE industry_tree_audit_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id TEXT NOT NULL,
                action TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                changed_by TEXT NOT NULL DEFAULT 'system',
                changed_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            );
            INSERT INTO industry_tree_audit_new (id, node_id, action, old_value, new_value, changed_by, changed_at)
                SELECT id, node_id, action, old_value, new_value, changed_by, changed_at
                FROM industry_tree_audit;
            DROP TABLE industry_tree_audit;
            ALTER TABLE industry_tree_audit_new RENAME TO industry_tree_audit;
            PRAGMA foreign_keys=ON;
            """,
        )
        _TABLE_COLUMNS.pop("industry_tree_audit", None)

    _ensure_table(
        conn,
        "industry_tree_versions",
        """
        CREATE TABLE IF NOT EXISTS industry_tree_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version_number INTEGER NOT NULL UNIQUE,
            snapshot TEXT NOT NULL,
            changelog TEXT,
            created_by TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """,
        {
            "version_number": "INTEGER NOT NULL UNIQUE",
            "snapshot": "TEXT NOT NULL",
            "changelog": "TEXT",
            "created_by": "TEXT",
            "created_at": "TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))",
        },
    )
    _ensure_column(conn, "uploaded_files", "message", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "analysis", "confirmed_summary", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "analysis", "confirmed_scoring", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "analysis", "boq_json", "TEXT NOT NULL DEFAULT '[]'")
    _ensure_column(conn, "analysis", "work_interface_json", "TEXT NOT NULL DEFAULT '[]'")
    _ensure_column(conn, "analysis", "construction_method_json", "TEXT NOT NULL DEFAULT '[]'")
    _ensure_column(conn, "analysis", "confirmed_boq", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "analysis", "confirmed_work_interface", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "analysis", "confirmed_construction_method", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "analysis", "global_facts_json", "TEXT NOT NULL DEFAULT '[]'")
    _ensure_column(conn, "analysis", "confirmed_facts", "INTEGER NOT NULL DEFAULT 0")
    # ENHANCE 2026-09-29（全局事实）：一致性审计结果落库。
    # 原实现审计只回 HTTP 响应、不留痕迹：刷新页面就丢，用户只能反复点“一致性审计”
    # 重复烧 AI 配额；也无法对比“修完再审”的前后差异。现存本次报告 JSON + 时刻。
    _ensure_column(conn, "analysis", "facts_audit_json", "TEXT NOT NULL DEFAULT ''")
    _ensure_column(conn, "analysis", "facts_audit_at", "TEXT")
    _ensure_column(conn, "usage_limits", "total_limit", "INTEGER NOT NULL DEFAULT 10")
    _ensure_column(conn, "usage_limits", "used_count", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "project_usage", "consumed_at", "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP")
    _ensure_column(conn, "sections", "target_words", "INTEGER DEFAULT NULL")
    _ensure_column(conn, "sections", "word_count_tier", "TEXT DEFAULT NULL")
    _ensure_column(conn, "sections", "architecture_json", "TEXT DEFAULT NULL")
    _ensure_column(conn, "sections", "labor_json", "TEXT DEFAULT NULL")
    _ensure_column(conn, "sections", "inlined_chart_json", "TEXT DEFAULT NULL")
    _ensure_column(conn, "sections", "comparison_json", "TEXT DEFAULT NULL")
    _ensure_column(conn, "sections", "layout_json", "TEXT DEFAULT NULL")
    _ensure_column(conn, "sections", "timeline_json", "TEXT DEFAULT NULL")

    # BUG #2 修复：为 ai_config 表添加 timeout 列
    _ensure_column(conn, "ai_config", "timeout", "INTEGER NOT NULL DEFAULT 120")

    # ========== AI 图像配置表（model_configs）==========
    # 注意：user_id 作为主键维度，支持多用户独立配置；默认 user_id=1 对应全局默认
    _ensure_table(
        conn,
        "model_configs",
        """
        CREATE TABLE model_configs (
            user_id INTEGER PRIMARY KEY DEFAULT 1,
            image_provider TEXT NOT NULL DEFAULT 'baidu',
            image_model TEXT NOT NULL DEFAULT 'ernie-4.0-turbo-8k',
            image_base_url TEXT NOT NULL DEFAULT '',
            image_api_key_enc TEXT NOT NULL DEFAULT '',
            image_max_concurrency INTEGER NOT NULL DEFAULT 2,
            image_max_per_project INTEGER NOT NULL DEFAULT 10,
            image_quality TEXT NOT NULL DEFAULT 'standard',
            image_style_injection TEXT NOT NULL DEFAULT '',
            image_enabled INTEGER NOT NULL DEFAULT 1,
            image_default_size TEXT NOT NULL DEFAULT '1024x1024',
            image_default_ratio TEXT NOT NULL DEFAULT '1:1',
            image_price_per_image REAL NOT NULL DEFAULT 0.0,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
        """,
        {
            "user_id": "INTEGER PRIMARY KEY DEFAULT 1",
            "image_provider": "TEXT NOT NULL DEFAULT 'baidu'",
            "image_model": "TEXT NOT NULL DEFAULT 'ernie-4.0-turbo-8k'",
            "image_base_url": "TEXT NOT NULL DEFAULT ''",
            "image_api_key_enc": "TEXT NOT NULL DEFAULT ''",
            "image_max_concurrency": "INTEGER NOT NULL DEFAULT 2",
            "image_max_per_project": "INTEGER NOT NULL DEFAULT 10",
            "image_quality": "TEXT NOT NULL DEFAULT 'standard'",
            "image_style_injection": "TEXT NOT NULL DEFAULT ''",
            "image_enabled": "INTEGER NOT NULL DEFAULT 1",
            "image_default_size": "TEXT NOT NULL DEFAULT '1024x1024'",
            "image_default_ratio": "TEXT NOT NULL DEFAULT '1:1'",
            "image_price_per_image": "REAL NOT NULL DEFAULT 0.0",
            "created_at": "TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))",
            "updated_at": "TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))",
        },
    )

    # ========== AI 生成任务记录表（image_generation_tasks）==========
    _ensure_table(
        conn,
        "image_generation_tasks",
        """
        CREATE TABLE image_generation_tasks (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL DEFAULT '',
            section_index INTEGER NOT NULL DEFAULT 0,
            provider TEXT NOT NULL DEFAULT '',
            model TEXT NOT NULL DEFAULT '',
            prompt TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            image_url TEXT NOT NULL DEFAULT '',
            error_message TEXT NOT NULL DEFAULT '',
            cost REAL NOT NULL DEFAULT 0.0,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            completed_at TEXT
        )
        """,
        {
            "id": "TEXT PRIMARY KEY",
            "project_id": "TEXT NOT NULL DEFAULT ''",
            "section_index": "INTEGER NOT NULL DEFAULT 0",
            "provider": "TEXT NOT NULL DEFAULT ''",
            "model": "TEXT NOT NULL DEFAULT ''",
            "prompt": "TEXT NOT NULL DEFAULT ''",
            "status": "TEXT NOT NULL DEFAULT 'pending'",
            "image_url": "TEXT NOT NULL DEFAULT ''",
            "error_message": "TEXT NOT NULL DEFAULT ''",
            "cost": "REAL NOT NULL DEFAULT 0.0",
            "created_at": "TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))",
            "completed_at": "TEXT",
        },
    )

    # ========== AI 提供商使用历史记录表（provider_history）==========
    # 用于实现 Last-Saved-First 降级优先级：最近使用的 provider 优先尝试
    _ensure_table(
        conn,
        "provider_history",
        """
        CREATE TABLE provider_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider_name TEXT NOT NULL,
            provider_type TEXT NOT NULL DEFAULT 'text',
            base_url TEXT NOT NULL DEFAULT '',
            success INTEGER NOT NULL DEFAULT 0,
            failed INTEGER NOT NULL DEFAULT 0,
            last_used_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
        """,
        {
            "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
            "provider_name": "TEXT NOT NULL",
            "provider_type": "TEXT NOT NULL DEFAULT 'text'",
            "base_url": "TEXT NOT NULL DEFAULT ''",
            "success": "INTEGER NOT NULL DEFAULT 0",
            "failed": "INTEGER NOT NULL DEFAULT 0",
            "last_used_at": "TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))",
            "created_at": "TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))",
        },
    )

    # ========== 行业树模块 ==========
    _ensure_table(
        conn,
        "industry_tree",
        """
        CREATE TABLE industry_tree (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            name_en TEXT,
            parent_id TEXT REFERENCES industry_tree(id) ON DELETE CASCADE,
            level INTEGER NOT NULL,
            description TEXT,
            prompt_hint TEXT,
            icon TEXT,
            knowledge_base_ids TEXT NOT NULL DEFAULT '[]',
            default_scoring_items TEXT NOT NULL DEFAULT '[]',
            is_active INTEGER NOT NULL DEFAULT 1,
            is_ai_generated INTEGER NOT NULL DEFAULT 0,
            order_index INTEGER NOT NULL DEFAULT 0,
            reference_count INTEGER NOT NULL DEFAULT 0,
            created_by TEXT NOT NULL DEFAULT 'system',
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """,
        {
            "name_en": "TEXT",
            "description": "TEXT",
            "prompt_hint": "TEXT",
            "icon": "TEXT",
            "knowledge_base_ids": "TEXT NOT NULL DEFAULT '[]'",
            "default_scoring_items": "TEXT NOT NULL DEFAULT '[]'",
            "is_active": "INTEGER NOT NULL DEFAULT 1",
            "is_ai_generated": "INTEGER NOT NULL DEFAULT 0",
            "order_index": "INTEGER NOT NULL DEFAULT 0",
            "reference_count": "INTEGER NOT NULL DEFAULT 0",
            "created_by": "TEXT NOT NULL DEFAULT 'system'",
        },
    )

    _ensure_column(conn, "industry_tree", "name_en", "TEXT")
    _ensure_column(conn, "industry_tree", "description", "TEXT")
    _ensure_column(conn, "industry_tree", "prompt_hint", "TEXT")
    _ensure_column(conn, "industry_tree", "icon", "TEXT")
    _ensure_column(conn, "industry_tree", "knowledge_base_ids", "TEXT NOT NULL DEFAULT '[]'")
    _ensure_column(conn, "industry_tree", "default_scoring_items", "TEXT NOT NULL DEFAULT '[]'")
    _ensure_column(conn, "industry_tree", "is_active", "INTEGER NOT NULL DEFAULT 1")
    _ensure_column(conn, "industry_tree", "is_ai_generated", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "industry_tree", "order_index", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "industry_tree", "reference_count", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "industry_tree", "created_by", "TEXT NOT NULL DEFAULT 'system'")

    # ========== 目录参考数据库模块 ==========
    _ensure_table(
        conn,
        "reference_outlines",
        """
        CREATE TABLE IF NOT EXISTS reference_outlines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            industry TEXT NOT NULL DEFAULT '',
            keywords TEXT NOT NULL DEFAULT '[]',
            content TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            sort_order INTEGER NOT NULL DEFAULT 0,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """,
        {
            "name": "TEXT NOT NULL DEFAULT ''",
            "industry": "TEXT NOT NULL DEFAULT ''",
            "keywords": "TEXT NOT NULL DEFAULT '[]'",
            "content": "TEXT NOT NULL DEFAULT ''",
            "description": "TEXT NOT NULL DEFAULT ''",
            "source": "TEXT NOT NULL DEFAULT ''",
            "sort_order": "INTEGER NOT NULL DEFAULT 0",
            "is_active": "INTEGER NOT NULL DEFAULT 1",
        },
    )

    _seed_reference_outlines(conn)

    # ========== 项目已有方案模板表 ==========
    _ensure_table(
        conn,
        "project_templates",
        """
        CREATE TABLE IF NOT EXISTS project_templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            file_path TEXT NOT NULL DEFAULT '',
            outline_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'pending',
            coverage_audit_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        )
    """,
        {},
    )

    # 修复旧版INSERT OR REPLACE导致的分析数据损坏
    conn.execute(
        "UPDATE analysis SET confirmed_summary = 0 WHERE summary_json = '{}' AND confirmed_summary = 1",
    )
    conn.execute(
        "UPDATE analysis SET confirmed_scoring = 0 WHERE scoring_points_json = '[]' AND confirmed_scoring = 1",
    )

    # 修复旧版目录数据：为没有scoring_point_id的目录按顺序关联评分点

    projects = conn.execute("SELECT id FROM projects").fetchall()
    for proj in projects:
        pid = proj["id"]
        analysis = conn.execute(
            "SELECT scoring_points_json FROM analysis WHERE project_id = ?",
            (pid,),
        ).fetchone()
        if not analysis or not analysis["scoring_points_json"]:
            continue
        try:
            sp_list = json.loads(analysis["scoring_points_json"])
        except Exception:
            continue
        if not sp_list:
            continue
        level1_sections = conn.execute(
            "SELECT id FROM sections WHERE project_id = ? AND level = 1 AND parent_id IS NULL ORDER BY sort_order, id",
            (pid,),
        ).fetchall()
        for idx, sec in enumerate(level1_sections):
            if idx >= len(sp_list):
                break
            sp_id = sp_list[idx].get("id", f"R{idx+1}")
            sec_id = sec["id"]
            existing = conn.execute(
                "SELECT scoring_point_id FROM sections WHERE id = ?",
                (sec_id,),
            ).fetchone()
            if existing and existing["scoring_point_id"]:
                continue
            conn.execute(
                "UPDATE sections SET scoring_point_id = ? WHERE id = ?",
                (sp_id, sec_id),
            )
            _propagate_sp_id_to_children(conn, sec_id, sp_id)

    # ========== 性能优化：添加数据库索引 ==========
    # 基础索引（已有）
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sections_project_id ON sections(project_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sections_parent_id ON sections(parent_id)")

    # 新增复合索引：查询项目下所有目录（按层级和排序）
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sections_project_level_sort ON sections(project_id, level, sort_order)"
    )

    # 新增复合索引：查询父目录下的所有子目录（树形结构遍历）
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sections_parent_project ON sections(parent_id, project_id)"
    )

    # 新增复合索引：查询项目下特定状态的目录
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sections_project_status ON sections(project_id, status)"
    )

    # 新增复合索引：查询叶子节点（无子节点的目录）
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sections_project_leaf ON sections(project_id, parent_id, status)"
    )

    # ========== 新增性能索引（2026-08-11 性能优化） ==========
    # 章节标题索引：支持按标题模糊搜索
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sections_title ON sections(title)")

    # 章节内容长度索引（用 target_words）：支持字数预算查询
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sections_target_words ON sections(target_words)")

    # 项目表索引：支持按创建时间查询
    conn.execute("CREATE INDEX IF NOT EXISTS idx_projects_created_at ON projects(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_projects_updated_at ON projects(updated_at)")

    # 目录表索引：支持按父节点查询（树形遍历）
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sections_project_parent_level ON sections(project_id, parent_id, level)"
    )

    # AI 配置表索引：支持快速查找默认配置
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_config_id ON ai_config(id)")

    # 知识库文档索引：支持按文件名和状态查询
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_documents_filename ON knowledge_documents(filename)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_documents_status ON knowledge_documents(status)"
    )

    # 废标检查表索引：支持按项目状态查询
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rejection_checks_project_status ON rejection_checks(project_id, status)"
    )

    # 查重表索引
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_duplicate_checks_project ON duplicate_checks(tender_file_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_duplicate_checks_status ON duplicate_checks(status)"
    )
    # P1：按项目维度查查重任务列表
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_duplicate_checks_project_id ON duplicate_checks(project_id)"
    )

    # 上传文件索引
    conn.execute("CREATE INDEX IF NOT EXISTS idx_uploaded_files_status ON uploaded_files(status)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_uploaded_files_project_status ON uploaded_files(project_id, status)"
    )

    # 行业树索引
    conn.execute("CREATE INDEX IF NOT EXISTS idx_industry_tree_parent ON industry_tree(parent_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_industry_tree_level ON industry_tree(level)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_industry_tree_active ON industry_tree(is_active)")

    # 出口表索引（补充）
    conn.execute("CREATE INDEX IF NOT EXISTS idx_exports_status ON exports(status)")

    # 项目模板索引
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_project_templates_status ON project_templates(status)"
    )

    #  exports 表索引
    conn.execute("CREATE INDEX IF NOT EXISTS idx_exports_project_id ON exports(project_id)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_exports_project_created ON exports(project_id, created_at)"
    )

    # uploaded_files 表索引
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_uploaded_files_project_id ON uploaded_files(project_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_uploaded_files_project_type ON uploaded_files(project_id, file_type)"
    )

    # analysis 表索引
    conn.execute("CREATE INDEX IF NOT EXISTS idx_analysis_project_id ON analysis(project_id)")

    # rejection_checks 表索引
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rejection_checks_project_id ON rejection_checks(project_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rejection_checks_project_status ON rejection_checks(project_id, status)"
    )

    # knowledge_documents 表索引
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_documents_industry ON knowledge_documents(industry)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_documents_status ON knowledge_documents(status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_documents_industry_status ON knowledge_documents(industry, status)"
    )

    # knowledge_items 表索引
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_items_source_doc_id ON knowledge_items(source_doc_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_items_industry ON knowledge_items(industry)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_items_tags ON knowledge_items(tags)")

    # knowledge_usage 表索引
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_usage_item_id ON knowledge_usage(item_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_usage_project_id ON knowledge_usage(project_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_knowledge_usage_section_id ON knowledge_usage(section_id)"
    )

    # project_templates 表索引
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_project_templates_project_id ON project_templates(project_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_project_templates_project_status ON project_templates(project_id, status)"
    )

    # reference_outlines 表索引
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reference_outlines_industry ON reference_outlines(industry)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reference_outlines_active ON reference_outlines(is_active)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_reference_outlines_industry_active ON reference_outlines(industry, is_active)"
    )

    # ========== 一致性审计表 ==========
    # BUG-FIX 2026-09-14（中）：这两张表原先只在 main.py 启动钩子里、用**硬编码路径**
    # `backend/data/bid_assistant.sqlite3` 创建，绕过了 `BID_ASSISTANT_DATA_DIR` 配置。
    # 后果：配置了自定义数据目录时（测试环境正是如此），表被建到真实库里，
    # 而应用实际使用的库里没有 → 一致性审计一调用就 `no such table: consistency_conflicts`。
    # 现收归 init_db，与其余 schema 同一入口（conftest 调 init_db 即可拿到完整 schema）。
    conn.execute("""
        CREATE TABLE IF NOT EXISTS consistency_conflicts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            conflict_id TEXT NOT NULL UNIQUE,
            fact_name TEXT NOT NULL,
            values_json TEXT NOT NULL,
            sources_json TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'medium',
            description TEXT,
            resolved INTEGER DEFAULT 0,
            resolved_value TEXT,
            resolved_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS consistency_audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            total_sections INTEGER DEFAULT 0,
            facts_extracted INTEGER DEFAULT 0,
            conflicts_found INTEGER DEFAULT 0,
            conflicts_json TEXT,
            audit_time TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'completed',
            -- T2 根治（2026-10-16）：审计来源列。两条写入路径直接写入
            -- （'rule' / 'ai'），摘要接口直接读取，剔除此前靠 conflicts_json
            -- 侧信道推断的分支。老库见下方迁移补列 + 历史行回填。
            source_kind TEXT NOT NULL DEFAULT 'rule',
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_conflict_project ON consistency_conflicts(project_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_audit_project ON consistency_audit_log(project_id)"
    )
    # T2 根治（2026-10-16）：老库补列 + 历史行回填。
    # `consistency_audit_log` 此前无来源列，摘要接口靠 `conflicts_json` 侧信道
    # （conflict_id 前缀：AI 路径恒 `AI-`，确定性路径恒 `conflict_`）推断审计
    # 来源。现给日志表加 `source_kind` 列，两条写入路径直接写入，摘要直接读取。
    # 历史行回填按 JSON 内 `"conflict_id": "AI-` 模式标记为 'ai'（json.dumps
    # 默认分隔符产 `: `，故该模式确定性匹配 AI 路径写入的行）；无 findings 的
    # AI 行（conflicts_json='[]'）无法确定性区分，保持默认 'rule'——属历史数据
    # 极端边角，此后新写入一律由写入路径正确标注。
    if "source_kind" not in _get_column_names(conn, "consistency_audit_log"):
        conn.execute(
            "ALTER TABLE consistency_audit_log "
            "ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'rule'"
        )
        conn.execute(
            "UPDATE consistency_audit_log SET source_kind = 'ai' "
            "WHERE conflicts_json LIKE '%\"conflict_id\": \"AI-%'"
        )

    # ========== 评分标准达标对齐（ENHANCE 2026-10-06）==========
    # 缺口：导出预检的 uncovered_scoring_points 只判「评分点有没有被章节挂载」，
    # 不看章节正文里到底答没答评分标准要求的内容（存在性 ≠ 达标性）。
    # services/scoring_compliance.py 在既有 sections.scoring_point_id 映射之上
    # 补一层内容覆盖度判定。零迁移：随 init_db 建表，无需独立迁移脚本。
    conn.execute("""
        CREATE TABLE IF NOT EXISTS scoring_compliance_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            audit_time TEXT NOT NULL,
            total_points INTEGER DEFAULT 0,
            unmapped INTEGER DEFAULT 0,
            empty INTEGER DEFAULT 0,
            weak INTEGER DEFAULT 0,
            covered INTEGER DEFAULT 0,
            result_json TEXT,
            status TEXT NOT NULL DEFAULT 'completed',
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_scoring_compliance_project "
        "ON scoring_compliance_audit(project_id)"
    )

    # ========== 字数档位配置（ENHANCE 2026-10-08）==========
    # 字数控制策略：由原来的二级目录下沉到三级目录（叶子节点）。
    # 四档标准：简短 800 / 标准 1200 / 详细 2000 / 超长 2500，每档允许 ±10% 浮动。
    # 该表允许管理员在运行时调整档位目标字数与浮动范围，无需修改代码。
    # sections.word_count_tier 字段记录单节点覆盖档位（NULL 表示跟随全局默认）。
    conn.execute("""
        CREATE TABLE IF NOT EXISTS word_count_tiers (
            key TEXT PRIMARY KEY,
            label TEXT NOT NULL,
            target_words INTEGER NOT NULL,
            tolerance_ratio REAL NOT NULL DEFAULT 0.10,
            is_default INTEGER NOT NULL DEFAULT 0,
            sort_order INTEGER NOT NULL DEFAULT 0,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        )
    """)
    # 插入四档默认配置（幂等：仅当 key 不存在时插入）
    for _tier_key, _label, _target, _ratio, _default, _order in (
        ("brief", "简短", 800, 0.10, 0, 1),
        ("standard", "标准", 1200, 0.10, 1, 2),
        ("detailed", "详细", 2000, 0.10, 0, 3),
        ("long", "超长", 2500, 0.10, 0, 4),
    ):
        conn.execute(
            "INSERT OR IGNORE INTO word_count_tiers "
            "(key, label, target_words, tolerance_ratio, is_default, sort_order, enabled) "
            "VALUES (?, ?, ?, ?, ?, ?, 1)",
            (_tier_key, _label, _target, _ratio, _default, _order),
        )

    # ========== 字数控制配置与快照（ENHANCE 2026-10-12，移植自易标）==========
    # 移植自《报告_易标文字生成数量控制规则与标准_2026-10-12》：
    #   R2 万字输入单位 / R5 强控 ±20% 小节区间 / R14 配置快照锚定。
    #
    # 核心设计是「快照配对」：用户在目录阶段设置的字数配置，必须在目录生成
    # 成功那一刻固化为 outline_word_control_snapshot_json，正文生成只认快照，
    # 从而杜绝「用户手工改了目录但控制链仍按旧配置跑」的静默脱节。
    # 快照与 outline 数据同 checkpoint 落盘，因此两列放在同一张表里。
    #
    # 该表与上方 word_count_tiers（档位配置）**职责不同、互不覆盖**：
    #   word_count_tiers   —— 全局「档位」字典（简短/标准/详细/超长），
    #                         由管理员维护，sections.word_count_tier 引用；
    #   word_control_settings —— 每个项目一次正文任务的**绝对字数目标**
    #                         （最少/最多/每节 + 强控开关）与其生效快照，
    #                         由项目作者在生成前填写。
    # 迁移安全：CREATE TABLE IF NOT EXISTS + _ensure_column，老库零破坏。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS word_control_settings (
            project_id INTEGER PRIMARY KEY,
            minimum_words INTEGER NOT NULL DEFAULT 0,
            maximum_words INTEGER NOT NULL DEFAULT 0,
            section_words INTEGER NOT NULL DEFAULT 0,
            strict_section_words INTEGER NOT NULL DEFAULT 0,
            outline_word_control_snapshot_json TEXT DEFAULT NULL,
            snapshot_at TEXT DEFAULT NULL,
            word_control_warning TEXT DEFAULT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
        )
    """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_word_control_settings_project "
        "ON word_control_settings(project_id)"
    )

    # sections 表补充易标口径的字数落库列。
    # - readable_word_count：按 R1 可读字数口径重算的实际字数
    #   （剔除 Markdown 标记 / 图片 / 代码块 / URL / HTML 后的
    #    CJK 字符数 + 西文词数）。与 word_count 并存以便迁移期对比。
    # - word_count_target：本轮生成的目标字数（来自快照派生），
    #   config.py 2026-10-11 修复说明里已承诺该列但当时未落库。
    # - word_count_deviation：实际字数 − 目标字数，正为超写、负为欠写。
    for _wc_col, _wc_def in [
        ("readable_word_count", "INTEGER NOT NULL DEFAULT 0"),
        ("word_count_target", "INTEGER NOT NULL DEFAULT 0"),
        ("word_count_deviation", "INTEGER NOT NULL DEFAULT 0"),
    ]:
        _ensure_column(conn, "sections", _wc_col, _wc_def)

    # ========== 任务组互斥锁（ENHANCE 2026-09-15）==========
    # 多进程（FastAPI + Celery worker）下内存锁无效，锁必须下沉到 sqlite 行级唯一约束。
    # DDL 集中在 task_lock.ensure_schema，避免与 app/services/task_lock.py 两处漂移。
    # 这里用函数内局部导入：task_lock 模块需要 `from ..db import get_db`，
    # 模块级导入会与 db.py 形成循环。
    from .services import task_lock as _task_lock

    _task_lock.ensure_schema(conn)

    # ========== 后台任务快照（ENHANCE 2026-09-15，Task 9）==========
    # 目录生成等长任务在**运行期间**就要有持久化行 + 心跳，否则进程被强杀时
    # `finally` 不执行，用户重启后完全看不到「上次跑到哪一步」。
    # 与上面同理：DDL 集中在 task_snapshot.ensure_schema，用函数内局部导入避免循环。
    from .services import task_snapshot as _task_snapshot

    _task_snapshot.ensure_schema(conn)

    # ========== 种子数据：默认 AI 厂商（agnes，开箱即用，无需用户手动输入）==========
    _seed_default_agnes_config(conn)


def _seed_default_agnes_config(conn: sqlite3.Connection):
    """向 ai_config 写入内置默认厂商 agnes（仅当尚无配置时）。

    该配置即系统默认厂商，APIKEY 由后端内置，用户无需手动填写。
    使用加密存储，与用户手动配置保持一致。
    """
    try:
        from .config import AGNES_API_KEY, AGNES_BASE_URL, AGNES_MODEL
        # API Key 统一明文存储（不再加密；读取兼容旧 ENC: 数据）

        existing = conn.execute("SELECT * FROM ai_config WHERE id = 1").fetchone()
        # 没有记录 → 直接插入默认配置
        if not existing:
            conn.execute(
                """
                INSERT INTO ai_config (
                    id, provider_name, provider, api_key, model, base_url,
                    document_profile, concurrency, temperature, max_tokens, timeout
                ) VALUES (1, 'agnes', 'openai_compatible', ?, ?, ?, '', 4, 0.7, 16384, 120)
                """,
                (AGNES_API_KEY or "", AGNES_MODEL, AGNES_BASE_URL),
            )
            logger.info("已写入默认 AI 厂商配置：agnes (%s)", AGNES_MODEL)
        else:
            # 有记录但 api_key 为空 → 用户尚未配置，用 .env 默认值填充
            existing_api_key = (existing["api_key"] or "").strip()
            if not existing_api_key:
                conn.execute(
                    """
                    UPDATE ai_config SET
                        provider_name = 'agnes',
                        provider = 'openai_compatible',
                        api_key = ?,
                        model = ?,
                        base_url = ?,
                        updated_at = datetime('now', 'localtime')
                    WHERE id = 1
                    """,
                    (AGNES_API_KEY or "", AGNES_MODEL, AGNES_BASE_URL),
                )
                logger.info(
                    "ai_config 为空，已用 .env 默认值填充：agnes (%s)", AGNES_MODEL
                )
            else:
                # 用户已有配置 → 不覆盖，保留用户设置
                logger.debug("ai_config 已有用户配置，跳过 seed 写入")
    except Exception as e:  # 种子失败不应阻断数据库初始化
        logger.warning("写入 agnes 默认配置失败（可忽略）：%s", e)


def _propagate_sp_id_to_children(conn: sqlite3.Connection, parent_id: int, sp_id: str):
    """使用 CTE 一次性查找所有后代节点并更新（替代递归 N+1 查询）"""
    conn.execute(
        """
        WITH RECURSIVE descendant_ids AS (
            SELECT id FROM sections WHERE id = ?
            UNION ALL
            SELECT s.id FROM sections s
            INNER JOIN descendant_ids d ON s.parent_id = d.id
        )
        UPDATE sections SET scoring_point_id = ?
        WHERE id IN (SELECT id FROM descendant_ids)
          AND scoring_point_id IS NULL
    """,
        (parent_id, sp_id),
    )


def _ensure_table(
    conn: sqlite3.Connection,
    table_name: str,
    create_ddl: str,
    columns: dict[str, str],
):
    existing = _get_column_names(conn, table_name)
    if not existing:
        conn.executescript(create_ddl)
        return
    for col_name, col_def in columns.items():
        if col_name not in existing:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_def}")


def _ensure_column(conn: sqlite3.Connection, table_name: str, col_name: str, col_def: str):
    existing = _get_column_names(conn, table_name)
    if col_name not in existing:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {col_name} {col_def}")


def _get_column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    cursor = conn.execute(f"PRAGMA table_info({table})")
    return {row["name"] for row in cursor.fetchall()}


def seed_prompt_templates():
    from .services.ai.prompts import _ALL_PROMPTS

    old_placeholders = (
        "{scoring_points}",
        "{summary}",
        "{industry_profile}",
        "{existing_points}",
        "{context}",
        "{tender_context}",
        "{outline}",
        "{children}",
        "{review_points}",
        "{first_points}",
    )

    with get_db() as conn:
        existing = conn.execute(
            "SELECT key, content, default_content FROM prompt_templates",
        ).fetchall()
        existing_map = {
            row["key"]: {
                "content": row["content"],
                "default_content": row["default_content"],
            }
            for row in existing
        }
        existing_keys = set(existing_map.keys())

        for key, meta in _ALL_PROMPTS.items():
            new_content = meta["content"]
            if key not in existing_keys:
                conn.execute(
                    """INSERT INTO prompt_templates (key, category, label, content, default_content, updated_at)
                       VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime'))""",
                    (key, meta["category"], meta["label"], new_content, new_content),
                )
            else:
                db_data = existing_map.get(key, {})
                db_content = db_data.get("content", "")
                db_default = db_data.get("default_content", "")

                need_update = False
                update_content = False

                if any(ph in db_content for ph in old_placeholders):
                    need_update = True
                    update_content = True

                if db_default != new_content:
                    need_update = True
                    if db_content == db_default:
                        update_content = True

                if need_update:
                    if update_content:
                        conn.execute(
                            """UPDATE prompt_templates
                               SET content = ?, default_content = ?, updated_at = datetime('now', 'localtime')
                               WHERE key = ?""",
                            (new_content, new_content, key),
                        )
                    else:
                        conn.execute(
                            """UPDATE prompt_templates
                               SET default_content = ?, updated_at = datetime('now', 'localtime')
                               WHERE key = ?""",
                            (new_content, key),
                        )


def _seed_reference_outlines(conn: sqlite3.Connection):
    universal_ref_paths = [
        PROJECT_ROOT / "平台智能体设计" / "目录生成" / "技术标—施工组织设计目录生成样本.md",
        PROJECT_ROOT / "技术标—施工组织设计目录生成样本.md",
    ]
    universal_content = ""
    for p in universal_ref_paths:
        try:
            with open(p, encoding="utf-8") as f:
                universal_content = f.read()
                break
        except Exception:
            continue

    housing_prefab_paths = [
        PROJECT_ROOT
        / "平台智能体设计"
        / "目录生成"
        / "房建装配式－预制构件类项目投标技术标文件一级目录.md",
        PROJECT_ROOT / "房建装配式－预制构件类项目投标技术标文件一级目录.md",
    ]
    housing_prefab_content = ""
    for p in housing_prefab_paths:
        try:
            with open(p, encoding="utf-8") as f:
                housing_prefab_content = f.read()
                break
        except Exception:
            continue

    reference_data = [
        {
            "name": "技术标—施工组织设计目录生成样本（通用强制参考）",
            "industry": "通用",
            "keywords": json.dumps(["通用", "所有项目", "强制"], ensure_ascii=False),
            "content": universal_content,
            "description": "【强制优先】所有项目目录生成时必须参考的一级目录架构，共二十八章。固定章节不可删减，二级目录可根据项目实际内容补充完善。",
            "source": "技术标—施工组织设计目录生成样本.md",
            "sort_order": 0,
            "is_active": 1,
        },
        {
            "name": "房屋建筑含装配式（预制构件类）项目投标技术标文件一级目录",
            "industry": "房建装配式",
            "keywords": json.dumps(
                ["房建", "装配式", "PC", "预制构件", "高层"], ensure_ascii=False
            ),
            "content": housing_prefab_content,
            "description": "房屋建筑含装配式/预制构件类项目的标准投标技术标一级目录，共十三章，涵盖项目重难点、工程概况、编制依据、施工方案、总平面布置、质量安全文明施工、进度计划、资源配备、PC专项方案、危大工程、总包管理、项目管理机构、工期优化。",
            "source": "房建装配式－预制构件类项目投标技术标文件一级目录.md",
            "sort_order": 1,
            "is_active": 1,
        },
        {
            "name": "公共建筑项目投标技术标文件一级目录",
            "industry": "公共建筑",
            "keywords": json.dumps(
                ["公共建筑", "办公楼", "商业", "学校", "医院", "文体"], ensure_ascii=False
            ),
            "content": """# 公共建筑项目投标技术标文件一级目录

## 第一章 编制说明与编制依据
第一节 编制说明
第二节 编制依据
第三节 工程概况及施工条件分析

## 第二章 施工组织部署
第一节 项目管理组织架构
第二节 施工总体部署
第三节 施工区域划分与流水段安排

## 第三章 主要分部分项工程施工方案
第一节 地基与基础工程
第二节 主体结构工程
第三节 建筑装饰装修工程
第四节 屋面及防水工程
第五节 机电安装工程
第六节 智能化系统工程

## 第四章 专项施工方案
第一节 高大模板支撑体系
第二节 脚手架工程
第三节 起重吊装工程
第四节 临时用电方案
第五节 深基坑支护方案（如有）

## 第五章 施工进度计划及保证措施
第一节 施工总进度计划
第二节 关键节点控制
第三节 工期保证措施

## 第六章 资源配置计划
第一节 劳动力配置计划
第二节 主要机械设备配置
第三节 主要材料进场计划

## 第七章 施工现场平面布置
第一节 施工总平面布置
第二节 临时设施规划
第三节 交通组织与物流通道

## 第八章 质量管理体系与措施
第一节 质量目标与管理体系
第二节 质量保证措施
第三节 成品保护措施

## 第九章 安全文明施工与环境保护
第一节 安全生产管理体系
第二节 文明施工措施
第三节 环境保护与扬尘治理
第四节 绿色施工措施

## 第十章 季节性施工措施
第一节 雨季施工措施
第二节 冬季施工措施
第三节 高温季节施工措施

## 第十一章 应急预案与风险控制
第一节 突发事件应急预案
第二节 消防安全预案
第三节 防汛防台预案
第四节 风险控制措施

## 第十二章 总包管理与专业分包配合
第一节 总包管理服务体系
第二节 专业分包协调配合
第三节 与各参建方协同机制""",
            "description": "公共建筑（办公楼、商业综合体、学校、医院、文体场馆等）项目投标技术标标准目录。",
            "source": "系统内置",
            "sort_order": 2,
            "is_active": 1,
        },
        {
            "name": "修缮改造项目投标技术标文件一级目录",
            "industry": "修缮改造",
            "keywords": json.dumps(
                ["修缮", "改造", "加固", "翻新", "旧改", "既有建筑"], ensure_ascii=False
            ),
            "content": """# 修缮改造项目投标技术标文件一级目录

## 第一章 工程概况与现状分析
第一节 项目基本情况
第二节 原建筑结构与使用状况调查
第三节 施工环境及周边条件分析
第四节 施工重难点分析

## 第二章 施工部署与组织机构
第一节 项目管理组织架构
第二节 施工总体部署原则
第三节 施工阶段划分与工序衔接
第四节 与原建筑使用功能的协调方案

## 第三章 主要分部分项工程施工方案
第一节 拆除工程
第二节 结构加固工程
第三节 装饰装修改造工程
第四节 屋面防水修缮工程
第五节 给排水管道改造工程
第六节 电气线路改造与升级工程
第七节 暖通空调系统改造

## 第四章 专项施工方案
第一节 脚手架与高空作业方案
第二节 临时支撑与结构保护方案
第三节 有限空间作业方案
第四节 动火作业安全管理方案
第五节 夜间施工技术方案

## 第五章 施工进度计划及保证措施
第一节 施工进度总计划
第二节 关键节点与里程碑
第三节 工期保证措施

## 第六章 资源配置计划
第一节 劳动力配置计划
第二节 机械设备配置计划
第三节 材料供应计划

## 第七章 施工现场平面布置
第一节 施工总平面布置
第二节 材料堆放与运输通道
第三节 临时设施布置

## 第八章 质量保障措施
第一节 质量管理体系
第二节 重点工序质量控制
第三节 成品保护与交叉施工防护

## 第九章 安全文明施工与环境保护
第一节 安全生产管理体系
第二节 施工噪音与粉尘控制
第三节 建筑垃圾清运与处置
第四节 周边环境影响控制

## 第十章 应急管理与风险控制
第一节 突发事件应急预案
第二节 结构安全风险管控
第三节 火灾与触电应急预案

## 第十一章 与原产权方及使用方的协调配合
第一节 沟通机制与协调流程
第二节 施工期间使用保障方案
第三节 投诉处理与反馈机制""",
            "description": "修缮改造项目（建筑加固、翻新改造、旧改等）投标技术标标准目录。",
            "source": "系统内置",
            "sort_order": 3,
            "is_active": 1,
        },
        {
            "name": "高层建筑施工项目投标技术标文件一级目录",
            "industry": "高层项目",
            "keywords": json.dumps(
                ["高层", "超高层", "塔楼", "高层住宅", "高层公建"], ensure_ascii=False
            ),
            "content": """# 高层建筑施工项目投标技术标文件一级目录

## 第一章 工程概况与施工条件分析
第一节 项目总体概况
第二节 建筑设计概况
第三节 结构设计概况
第四节 施工条件与环境分析
第五节 工程特点、重点与难点分析

## 第二章 施工组织部署
第一节 项目管理组织架构
第二节 施工总体部署
第三节 施工区段划分与流水组织
第四节 施工顺序与交叉作业安排

## 第三章 主要分部分项工程施工方案
第一节 测量放线方案
第二节 地基与基础工程
第三节 地下室结构工程
第四节 主体结构工程（混凝土/钢结构）
第五节 大体积混凝土施工
第六节 钢筋工程
第七节 模板工程
第八节 混凝土工程
第九节 砌体工程
第十节 装饰装修工程
第十一节 屋面工程
第十二节 机电安装工程
第十三节 幕墙工程

## 第四章 垂直运输与大型机械方案
第一节 塔吊选型与布置
第二节 施工电梯配置方案
第三节 泵送混凝土方案
第四节 大型机械安拆方案

## 第五章 专项施工方案
第一节 深基坑支护与降排水方案
第二节 高支模施工方案
第三节 外脚手架/爬架方案
第四节 起重吊装方案
第五节 临时用电方案
第六节 消防保卫方案
第七节 防雷接地方案

## 第六章 施工进度计划及保证措施
第一节 施工总进度计划
第二节 关键线路与里程碑节点
第三节 工期保证组织措施
第四节 工期保证技术措施
第五节 赶工预案

## 第七章 资源配置计划
第一节 劳动力配置计划
第二节 主要施工机械设备配置
第三节 主要周转材料配置
第四节 主要材料供应计划

## 第八章 施工现场平面布置
第一节 施工总平面布置（分阶段）
第二节 垂直运输机械布置
第三节 材料堆场与加工区布置
第四节 临时道路与出入口布置
第五节 临时水电布置

## 第九章 质量管理体系与保证措施
第一节 质量目标与管理体系
第二节 质量保证措施
第三节 关键工序质量控制
第四节 质量通病防治
第五节 成品保护措施

## 第十章 安全文明施工与环境保护
第一节 安全管理体系与保证措施
第二节 高处作业安全防护
第三节 临边洞口防护
第四节 文明施工措施
第五节 环境保护与扬尘治理
第六节 噪声控制措施

## 第十一章 季节性施工措施
第一节 雨季施工措施
第二节 夏季高温施工措施
第三节 冬季施工措施
第四节 台风季施工措施

## 第十二章 应急预案与风险控制
第一节 应急救援组织体系
第二节 坍塌事故应急预案
第三节 高处坠落应急预案
第四节 火灾应急预案
第五节 防汛防台应急预案
第六节 大型机械事故应急预案

## 第十三章 总包管理与专业分包配合
第一节 总包管理服务方案
第二节 专业分包协调管理机制
第三节 与各参建方协同配合""",
            "description": "高层/超高层建筑项目投标技术标标准目录，涵盖深基坑、高支模、爬架、塔吊等高层施工核心内容。",
            "source": "系统内置",
            "sort_order": 4,
            "is_active": 1,
        },
        {
            "name": "市政基础设施项目投标技术标文件一级目录",
            "industry": "市政",
            "keywords": json.dumps(
                ["市政", "道路", "桥梁", "管网", "隧道", "轨道交通"], ensure_ascii=False
            ),
            "content": """# 市政基础设施项目投标技术标文件一级目录

## 第一章 编制说明与编制依据
第一节 编制说明
第二节 编制依据
第三节 工程概况及施工条件分析

## 第二章 施工组织部署
第一节 项目管理组织架构
第二节 施工总体部署
第三节 施工分区与流水段划分

## 第三章 主要分部分项工程施工方案
第一节 道路工程
第二节 桥梁工程
第三节 排水管道工程
第四节 给水管道工程
第五节 电力管沟工程
第六节 交通设施工程

## 第四章 专项施工方案
第一节 深基坑支护方案
第二节 交通导改方案
第三节 地下管线保护方案
第四节 临时排水方案
第五节 围挡与封闭施工方案

## 第五章 施工进度计划及保证措施
第一节 施工总进度计划
第二节 关键节点控制
第三节 工期保证措施

## 第六章 资源配置计划
第一节 劳动力配置计划
第二节 主要机械设备配置
第三节 主要材料供应计划

## 第七章 施工现场平面布置
第一节 施工总平面布置
第二节 交通组织方案
第三节 临时设施布置

## 第八章 质量管理体系与措施
第一节 质量目标与管理体系
第二节 质量保证措施
第三节 质量通病防治

## 第九章 安全文明施工与环境保护
第一节 安全生产管理体系
第二节 交通疏导与安全防护
第三节 文明施工与扬尘治理
第四节 环境保护措施

## 第十章 季节性施工措施
第一节 雨季施工措施
第二节 冬季施工措施

## 第十一章 应急预案与风险控制
第一节 突发事件应急预案
第二节 管线破坏应急预案
第三节 交通事故应急预案

## 第十二章 与各部门协调配合
第一节 与交通管理部门配合
第二节 与管线单位配合
第三节 与周边社区协调""",
            "description": "市政基础设施项目（道路、桥梁、管网、隧道等）投标技术标标准目录。",
            "source": "系统内置",
            "sort_order": 5,
            "is_active": 1,
        },
        {
            "name": "工业厂房项目投标技术标文件一级目录",
            "industry": "工业厂房",
            "keywords": json.dumps(
                ["工业", "厂房", "钢结构", "车间", "仓库", "生产线"], ensure_ascii=False
            ),
            "content": """# 工业厂房项目投标技术标文件一级目录

## 第一章 工程概况与施工条件分析
第一节 项目总体概况
第二节 建筑与结构概况
第三节 施工条件与环境分析
第四节 工程特点与重难点分析

## 第二章 施工组织部署
第一节 项目管理组织架构
第二节 施工总体部署
第三节 施工分区与流水段划分

## 第三章 主要分部分项工程施工方案
第一节 地基与基础工程
第二节 钢结构制作与安装
第三节 钢筋混凝土结构工程
第四节 围护结构工程
第五节 地面工程
第六节 装饰装修工程
第七节 机电安装工程
第八节 工艺管道工程

## 第四章 专项施工方案
第一节 大型构件吊装方案
第二节 高支模施工方案
第三节 脚手架工程
第四节 临时用电方案
第五节 焊接工程专项方案

## 第五章 施工进度计划及保证措施
第一节 施工总进度计划
第二节 关键节点控制
第三节 工期保证措施

## 第六章 资源配置计划
第一节 劳动力配置计划
第二节 主要机械设备配置
第三节 主要材料供应计划

## 第七章 施工现场平面布置
第一节 施工总平面布置
第二节 大型构件堆放与吊装场地
第三节 临时设施布置

## 第八章 质量管理体系与措施
第一节 质量目标与管理体系
第二节 钢结构焊接质量控制
第三节 质量保证措施

## 第九章 安全文明施工与环境保护
第一节 安全生产管理体系
第二节 高处作业与吊装安全
第三节 文明施工措施
第四节 环境保护措施

## 第十章 季节性施工措施
第一节 雨季施工措施
第二节 冬季施工措施

## 第十一章 应急预案与风险控制
第一节 突发事件应急预案
第二节 吊装事故应急预案
第三节 火灾应急预案""",
            "description": "工业厂房项目（钢结构厂房、生产车间、仓库等）投标技术标标准目录。",
            "source": "系统内置",
            "sort_order": 6,
            "is_active": 1,
        },
    ]

    # PERF-8 修复：用 executemany 替代循环逐条 INSERT，减少 SQL 编译开销
    conn.executemany(
        """INSERT OR IGNORE INTO reference_outlines
           (name, industry, keywords, content, description, source, sort_order, is_active)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                item["name"],
                item["industry"],
                item["keywords"],
                item["content"],
                item["description"],
                item["source"],
                item["sort_order"],
                item["is_active"],
            )
            for item in reference_data
        ],
    )


def get_reference_outline_by_industry(industry: str) -> str:
    with get_db() as conn:
        row = conn.execute(
            "SELECT content FROM reference_outlines WHERE sort_order = 0 AND is_active = 1 LIMIT 1",
        ).fetchone()
        if row:
            return row["content"]

        row = conn.execute(
            "SELECT content FROM reference_outlines WHERE industry = ? AND is_active = 1 ORDER BY sort_order LIMIT 1",
            (industry,),
        ).fetchone()
        if row:
            return row["content"]

        row = conn.execute(
            "SELECT content, keywords FROM reference_outlines WHERE is_active = 1 ORDER BY sort_order",
        ).fetchall()
        if not row:
            return ""

        industry_lower = industry.lower()
        best_match = ""
        for r in row:
            try:
                keywords = json.loads(r["keywords"]) if isinstance(r["keywords"], str) else []
            except Exception:
                keywords = []
            for kw in keywords:
                if kw and kw.lower() in industry_lower:
                    best_match = r["content"]
                    break
            if best_match:
                break

        return best_match


# D-3（2026-10-06）：解除 app.db ↔ app.db_migrations 的**导入期循环依赖**。
# 此前 db.py 底部 `from .db_migrations import ensure_enhancement_tables` 使
# `import app.db_migrations` 必须先 import app.db，反之亦然，直接导入 db_migrations
# 会抛 "partially initialized module" —— 只能靠调用方猜导入顺序。
# 现改为 PEP 562 惰性导出：属性首次访问时才导入，既保留
# `from app.db import ensure_enhancement_tables` 的既有用法，又消除导入顺序约束。
def __getattr__(name: str):
    if name == "ensure_enhancement_tables":
        from .db_migrations import ensure_enhancement_tables

        return ensure_enhancement_tables
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "get_db",
    "init_db",
    "ensure_enhancement_tables",
]
