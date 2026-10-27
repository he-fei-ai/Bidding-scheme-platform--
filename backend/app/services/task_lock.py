"""跨进程安全的任务组互斥锁。

ENHANCE 2026-09-15：移植自易标 `taskService.cjs` 的 `lockPolicy: 'group-exclusive'`
与 `assertTaskCanStart`（第 28-173、552-609 行）。

## 为什么要重写，而不是照搬参考实现

参考实现是 **Electron 单进程**桌面应用，它的锁有两层：

1. 内存层：`const activeTasks = new Map()`（`taskService.cjs:320`），键是任务类型；
2. 持久层：工作区状态文件里的 `xxxTask.status`，只有
   「`status === 'paused'` 就拒绝启动同组新任务」这一条**能跨重启生效**。

本软件是 **FastAPI + Celery** 的多进程部署（`app/celery_app.py`、
`app/tasks/__init__.py::tasks.content.generate_sections` 在独立 worker 进程里跑
`generation_manager.create_task`），所以：

- 内存 Map 在 worker 之间**完全失效** —— A 进程拿的锁 B 进程看不见；
- 现有 `GenerationManager._tasks`（`dict[project_id, GenerationTask]`）同理，
  `TaskRegistry`（`app/services/task_registry.py`）也只是一个进程内的 asyncio 单例。

因此本模块把锁下沉到 **sqlite 行级唯一约束**：唯一的仲裁者是数据库，
任何进程、任何 worker 都看到同一份锁状态。

## 锁粒度：任务组 × 项目

参考实现是单工作区的，`group` 天然等于全局。本软件一个库里装多个项目，
若照搬「组级全局锁」，两个**不同项目**就无法并发生成正文 —— 这是功能倒退。
所以锁键是 `"{group}:{project_id}"`：同项目的同组任务互斥，不同项目互不干扰。

## 三个必须遵守的语义

1. **唯一性靠部分唯一索引**（`uq_task_locks_active ... WHERE status='active'`），
   不靠「先查后插」——先查后插在多进程下有 TOCTOU 窗口。
2. **过期靠心跳**，不靠「内存里没有就判定死了」。
   现有的 `generation_manager.get_project_progress` 用
   「DB 里 running 但内存无实例 → 标记 failed」做残留清理，这条判据在
   **多进程下会误杀另一个 worker 正在跑的任务**。接入本模块后，
   该判据改为「锁仍活跃且未过期 → 不动」。
3. **释放必须校验 owner**。否则一个已被超时抢占的任务在收尾时会把
   新持有者的锁一起释放掉（经典 ABA）。
"""

from __future__ import annotations

from datetime import datetime, timedelta
import atexit
import logging
import os
import re
import sqlite3
import threading
from typing import Any, Callable
import uuid

from ..db import get_db


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 任务组注册表（对应参考实现的 taskDefinitions.group / lockPolicy）
# ---------------------------------------------------------------------------

#: 任务类型 → 任务组。同组任务互斥。
#: 只有登记在此的任务类型才参与互斥；未登记的类型视为不参与（对应
#: 参考实现的 `lockPolicy: 'none'`）。
TASK_GROUPS: dict[str, str] = {
    # 技术方案组：参考实现里 7 个任务全是 group-exclusive
    "bid-section-extraction": "technical-plan",
    "bid-analysis": "technical-plan",
    "outline-generation": "technical-plan",
    "outline-adjustment": "technical-plan",
    "global-facts-generation": "technical-plan",
    "global-facts-adjustment": "technical-plan",
    "content-generation": "technical-plan",
    # 废标项检查组
    "rejection-items-extraction": "rejection-check",
    "rejection-check-run": "rejection-check",
    # 标书查重组
    "duplicate-analysis": "duplicate-check",
    # 可行性研究报告组（ENHANCE 2026-09-15）：单工作区流程，
    # 分析/目录/参数/正文四个 AI 任务互斥，避免并发读写同一份大纲与参数
    "feasibility-analysis": "feasibility-report",
    "feasibility-outline": "feasibility-report",
    "feasibility-parameters": "feasibility-report",
    "feasibility-content": "feasibility-report",
    # 导出组
    "export": "export",
}

GROUP_LABELS: dict[str, str] = {
    "technical-plan": "技术方案",
    "rejection-check": "废标项检查",
    "duplicate-check": "标书查重",
    "feasibility-report": "可行性研究报告",
    "export": "文档导出",
}

TASK_LABELS: dict[str, str] = {
    "bid-section-extraction": "多标段识别",
    "bid-analysis": "招标文件解析",
    "outline-generation": "目录生成",
    "outline-adjustment": "目录AI调整",
    "global-facts-generation": "全局事实设定",
    "global-facts-adjustment": "全局事实AI调整",
    "content-generation": "正文生成",
    "rejection-items-extraction": "无效与废标项解析",
    "rejection-check-run": "废标项检查",
    "duplicate-analysis": "标书查重分析",
    "feasibility-analysis": "可研资料分析",
    "feasibility-outline": "可研报告目录",
    "feasibility-parameters": "可研关键参数",
    "feasibility-content": "可研正文生成",
    "export": "文档导出",
}

#: 锁的默认存活时间。持有者必须在此之前调用 `heartbeat` 续约，
#: 否则锁会被 `reclaim_stale` 回收（视为持有者进程已死）。
#: 取 30 分钟：正文生成单章节的 AI 调用最长可能十几分钟，
#: 而心跳点在每个章节进度更新处，正常情况下远早于超时。
DEFAULT_TTL_SECONDS = 1800

#: 单进程内的持有者标识前缀，便于诊断时区分是哪个进程/哪台机器
_PROCESS_TAG = f"{os.getpid()}:{uuid.uuid4().hex[:6]}"


class TaskLockConflict(RuntimeError):
    """任务组互斥冲突：同组已有活跃任务。

    携带 `holder`（占用者的锁记录）与 `lock_key`，供路由层拼装
    面向用户的提示（对应参考实现 `assertTaskCanStart` 抛出的错误文案）。
    """

    def __init__(self, holder: dict[str, Any], requested_task_type: str):
        self.holder = holder
        self.lock_key = holder.get("lock_key", "")
        self.requested_task_type = requested_task_type

        group = holder.get("group_name", "")
        group_label = GROUP_LABELS.get(group, group or "任务组")
        holder_label = TASK_LABELS.get(holder.get("task_type", ""), holder.get("task_type", "任务"))
        requested_label = TASK_LABELS.get(requested_task_type, requested_task_type)
        super().__init__(
            f"当前{group_label}正在执行“{holder_label}”，请完成后再启动“{requested_label}”。"
        )


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _fmt(moment: datetime) -> str:
    """统一的时间戳格式（与项目其余表一致：本地时间、秒级精度）。

    该格式按字典序排序即等价于按时间排序，所以超时判断可以直接用
    SQL 字符串比较，不必在 SQL 里做时间运算。
    """
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _now(now: datetime | None = None) -> datetime:
    return now if now is not None else datetime.now()


def lock_key_for(group_name: str, project_id: int | None) -> str:
    """互斥键：同项目同组互斥，不同项目互不干扰。"""
    scope = "global" if project_id is None else str(project_id)
    return f"{group_name}:{scope}"


def group_for_task(task_type: str) -> str | None:
    """任务类型 → 任务组；未登记的任务类型不参与互斥。"""
    return TASK_GROUPS.get(task_type)


def make_owner_id(task_type: str = "task", project_id: int | None = None) -> str:
    """生成持有者标识。

    形如 `content-generation:42:1234:ab12cd:9f3e...`，同时带上进程标识，
    便于在 `GET /tasks/locks` 里看出锁被哪个进程拿着。
    """
    scope = "global" if project_id is None else str(project_id)
    return f"{task_type}:{scope}:{_PROCESS_TAG}:{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------


def ensure_schema(conn: sqlite3.Connection) -> None:
    """建表 + 部分唯一索引。

    由 `db.init_db()` 调用（**不要**在 `main.py` 启动钩子里另起一份 DDL：
    历史上一致性两张表就因为写在启动钩子里、用硬编码路径，绕过了
    `BID_ASSISTANT_DATA_DIR`，导致自定义数据目录下功能直接 `no such table`）。
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_locks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            lock_key TEXT NOT NULL,
            group_name TEXT NOT NULL,
            task_type TEXT NOT NULL,
            project_id INTEGER,
            owner_id TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            acquired_at TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            released_at TEXT,
            note TEXT NOT NULL DEFAULT ''
        )
        """
    )
    # 关键：唯一性只约束「活跃」行，已释放/已过期的历史行可以保留，
    # 这样既能保证互斥，又留了一份锁的审计轨迹。
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_task_locks_active
            ON task_locks(lock_key) WHERE status = 'active'
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_locks_group ON task_locks(group_name, status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_task_locks_expires ON task_locks(status, expires_at)"
    )


def _row_to_dict(row: sqlite3.Row | tuple | None, columns: list[str] | None = None) -> dict | None:
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        # sqlite3.Row 迭代出的是**值**不是键，`for k in row` 会拿到 value、
        # 再 `row[k]` 直接报错，所以必须显式 .keys()（ruff SIM118 在此为误报）。
        return {k: row[k] for k in row.keys()}  # noqa: SIM118
    names = columns or [
        "id",
        "lock_key",
        "group_name",
        "task_type",
        "project_id",
        "owner_id",
        "status",
        "acquired_at",
        "heartbeat_at",
        "expires_at",
        "released_at",
        "note",
    ]
    return dict(zip(names, row, strict=False))


_LOCK_COLUMNS = (
    "id, lock_key, group_name, task_type, project_id, owner_id, "
    "status, acquired_at, heartbeat_at, expires_at, released_at, note"
)


def _fetch_active(conn: sqlite3.Connection, lock_key: str) -> dict | None:
    row = conn.execute(
        f"SELECT {_LOCK_COLUMNS} FROM task_locks WHERE lock_key = ? AND status = 'active'",
        (lock_key,),
    ).fetchone()
    return _row_to_dict(row)


def _read_state(conn: sqlite3.Connection, lock_key: str, moment: datetime) -> dict[str, Any]:
    """在给定连接上读出锁状态（不触发回收）。"""
    holder = _fetch_active(conn, lock_key)
    if holder is None:
        return {"held": False, "live": False, "holder": None}
    expires_at = holder.get("expires_at") or ""
    return {
        "held": True,
        "live": bool(expires_at and expires_at >= _fmt(moment)),
        "holder": holder,
    }


def read_lock_state(
    conn: sqlite3.Connection,
    task_type: str,
    project_id: int | None = None,
    *,
    group_name: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """在**调用方已有的连接**上读取锁状态。

    为什么不直接调 `get_active_lock`：那个函数自己开 `get_db(readonly=True)`，
    而 `get_db` 在退出时会 `conn.commit()`；如果调用方正处于
    `get_db_with_retry()` 事务中（例如 `get_project_progress` 的残留修正
    分支会显式 `BEGIN`），嵌套调用会把外层事务提前提交掉。
    需要「在事务里顺带看一眼锁」时用本函数。

    Returns:
        `{"held": bool, "live": bool, "holder": dict | None}`
        - `held`：存在活跃锁行
        - `live`：该锁尚未超时（`held` 为假时必为假）
    """
    group = group_name or group_for_task(task_type)
    if group is None:
        return {"held": False, "live": False, "holder": None}
    key = lock_key_for(group, project_id)
    return _read_state(conn, key, _now(now))


# ---------------------------------------------------------------------------
# 过期回收
# ---------------------------------------------------------------------------


def _reclaim(conn: sqlite3.Connection, now: datetime) -> int:
    """把已超时的活跃锁标记为 expired。返回回收条数。

    **只按时间判定**，不参考任何进程内状态 —— 这正是本模块相对
    现有「内存里没有就判死」判据的修正点：内存状态在跨进程场景下不可信。
    """
    cur = conn.execute(
        "UPDATE task_locks SET status = 'expired', released_at = ? "
        "WHERE status = 'active' AND expires_at < ?",
        (_fmt(now), _fmt(now)),
    )
    return cur.rowcount or 0


def reclaim_stale(*, now: datetime | None = None) -> int:
    """回收所有已超时的锁。返回回收条数。"""
    moment = _now(now)
    with get_db() as conn:
        count = _reclaim(conn, moment)
    if count:
        logger.warning("[task-lock] 回收 %d 个超时未续约的锁", count)
    return count


#: owner_id 中的进程标识段（形如 `12345:ab12cd`，即 `<pid>:<实例 hex6>`）
_PROCESS_TAG_SEGMENT_RE = re.compile(r"\d+:[0-9a-f]{6}")


def _is_foreign_owner(owner_id: str) -> bool:
    """owner_id 是否属于**其它**进程实例（孤儿锁判定）。

    FIX 2026-10-26（P0 回归）：旧判定用 `NOT LIKE f"{_PROCESS_TAG}:%"`（前缀），
    但 `make_owner_id` 的形态是 `{task}:{scope}:{_PROCESS_TAG}:{uuid}` ——
    进程标识在**中间**，于是本进程自己的锁也被判成外来锁、每次 acquire 都被
    回收 → 组锁彻底失效，所有并发冲突用例（409/续约）全线失败。
    现按「是否含进程标识段」判定：含本进程标识 → 自己人；含其它进程标识 →
    孤儿锁；不含任何进程标识（调用方自定义 owner）→ 视为自己人，不回收。
    """
    text = str(owner_id or "")
    if not text:
        return False
    if _PROCESS_TAG in text:
        return False
    return bool(_PROCESS_TAG_SEGMENT_RE.search(text))


def _reclaim_foreign_for_key(
    conn: sqlite3.Connection, lock_key: str, moment: datetime
) -> int:
    """回收单个 key 下、不属于本进程实例的活跃锁（孤儿锁）。

    单一后端进程是唯一锁仲裁者；本进程持有的活跃锁其 owner_id 含 `_PROCESS_TAG`
    （pid + 实例 uuid）。带其它进程标识的活跃锁必然是已退出进程留下的残留
    （例如软件被关闭时进程未优雅释放），必须过期回收，否则会一直挡住同组
    新任务直到 30 分钟 TTL 到期。

    Returns:
        受影响行数。
    """
    rows = conn.execute(
        "SELECT owner_id FROM task_locks WHERE status = 'active' AND lock_key = ?",
        (lock_key,),
    ).fetchall()
    foreign = [
        (row[0],) for row in rows if _is_foreign_owner(row[0])
    ]
    if not foreign:
        return 0
    cur = conn.executemany(
        "UPDATE task_locks SET status = 'expired', released_at = ? "
        "WHERE status = 'active' AND lock_key = ? AND owner_id = ?",
        [(_fmt(moment), lock_key, owner) for (owner,) in foreign],
    )
    return cur.rowcount or 0


def reclaim_foreign_locks(*, now: datetime | None = None) -> int:
    """回收所有不属于本进程实例的活跃锁（跨进程孤儿锁）。返回回收条数。

    典型用途：应用启动 / 关闭时调用，清理上一个（已退出）进程实例遗留的锁，
    避免"关闭软件后锁未释放、再启动被自己挡住"。
    """
    moment = _now(now)
    with get_db() as conn:
        rows = conn.execute(
            "SELECT lock_key, owner_id FROM task_locks WHERE status = 'active'"
        ).fetchall()
        foreign = [
            (row[0], row[1]) for row in rows if _is_foreign_owner(row[1])
        ]
        if not foreign:
            return 0
        cur = conn.executemany(
            "UPDATE task_locks SET status = 'expired', released_at = ? "
            "WHERE status = 'active' AND lock_key = ? AND owner_id = ?",
            [(_fmt(moment), key, owner) for (key, owner) in foreign],
        )
        count = cur.rowcount or 0
    if count:
        logger.warning("[task-lock] 回收 %d 个跨进程残留锁（非本进程持有）", count)
    return count


def release_owned_locks(*, now: datetime | None = None) -> int:
    """释放本进程实例持有的全部活跃锁。返回释放条数。

    用于进程优雅退出（atexit / 关闭钩子）：进程退出后其持有的锁再也无法续约，
    与其等 30 分钟 TTL 过期，不如主动释放，让同组任务可立即启动。
    """
    moment = _now(now)
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE task_locks SET status = 'released', released_at = ? "
            "WHERE status = 'active' AND owner_id LIKE ?",
            (_fmt(moment), f"%{_PROCESS_TAG}:%"),
        )
        count = cur.rowcount or 0
    if count:
        logger.info("[task-lock] 进程退出，释放 %d 个本进程持有的锁", count)
    return count


def _release_owned_locks_on_exit() -> None:
    """atexit 回调：进程退出时尽力释放本进程持有的锁。

    非优雅退出（SIGKILL / 断电）不会触发；但正常关闭、Ctrl-C、Docker 停容器
    等会触发，覆盖绝大多数"关闭软件"场景。失败仅告警，不影响退出。
    """
    try:
        release_owned_locks()
    except Exception as _e:  # pragma: no cover - 退出期兜底
        logger.warning("[task-lock] 进程退出释放锁失败（忽略）: %s", _e)


atexit.register(_release_owned_locks_on_exit)


# ---------------------------------------------------------------------------
# 获取 / 续约 / 释放
# ---------------------------------------------------------------------------


def acquire(
    task_type: str,
    *,
    project_id: int | None = None,
    group_name: str | None = None,
    owner_id: str | None = None,
    ttl_seconds: int | None = None,
    note: str = "",
    now: datetime | None = None,
    allow_same_owner: bool = True,
) -> dict[str, Any]:
    """获取任务组锁。冲突时抛 `TaskLockConflict`。

    Args:
        task_type: 任务类型（须登记在 `TASK_GROUPS`；未登记则本函数直接
            返回一个 `acquired=False, reason='ungrouped'` 的占位结果，
            表示该任务不参与互斥）。
        project_id: 项目 ID。`None` 表示全局锁。
        group_name: 覆盖默认分组（一般不用传）。
        owner_id: 持有者标识。不传则自动生成。**传入稳定值可让同一持有者
            重复获取变成续约**（`allow_same_owner=True` 时）。
        ttl_seconds: 存活时间，默认 `DEFAULT_TTL_SECONDS`。
        note: 备注，写进锁记录便于诊断。
        now: 注入当前时间（测试用）。
        allow_same_owner: 同一 `owner_id` 重复获取时视为续约而非冲突。

    Returns:
        锁记录 dict，额外带 `acquired` 布尔字段。

    Raises:
        TaskLockConflict: 同组已有其他活跃任务。
        ValueError: `ttl_seconds` 非正。
    """
    group = group_name or group_for_task(task_type)
    if group is None:
        # 未登记的任务类型不参与互斥：返回占位结果，调用方无需分支处理
        logger.debug("[task-lock] 任务类型 %s 未登记分组，跳过互斥", task_type)
        return {
            "acquired": False,
            "reason": "ungrouped",
            "lock_key": "",
            "group_name": "",
            "task_type": task_type,
            "project_id": project_id,
            "owner_id": owner_id or "",
        }

    ttl = DEFAULT_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds)
    if ttl <= 0:
        raise ValueError(f"ttl_seconds 必须为正数，收到 {ttl}")

    moment = _now(now)
    key = lock_key_for(group, project_id)
    owner = owner_id or make_owner_id(task_type, project_id)
    expires = moment + timedelta(seconds=ttl)

    with get_db() as conn:
        # 先回收超时锁，避免「持有者已死但锁还在」把任务永久挡住
        _reclaim(conn, moment)

        # ENHANCE（修复"关闭软件后锁未释放"）：单一后端进程是唯一锁仲裁者，
        # 正常情形下本进程持有的 active 锁其 owner_id 必以 _PROCESS_TAG 开头。
        # 若发现该 key 已有 active 锁、但其 owner_id 不属本进程（来自已退出/
        # 被关闭的旧进程实例），说明它是孤儿锁（进程退出时未来得及释放），
        # 直接过期回收，避免它把新任务挡在 30 分钟 TTL 之外。
        _reclaim_foreign_for_key(conn, key, moment)

        try:
            conn.execute(
                "INSERT INTO task_locks "
                "(lock_key, group_name, task_type, project_id, owner_id, status, "
                " acquired_at, heartbeat_at, expires_at, note) "
                "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)",
                (key, group, task_type, project_id, owner, _fmt(moment), _fmt(moment), _fmt(expires), note),
            )
        except sqlite3.IntegrityError:
            # 唯一索引挡住了 → 确实有活跃持有者
            holder = _fetch_active(conn, key)
            if holder is None:
                # 极端竞态：冲突行在本次事务里刚好被释放。
                # 不吞掉异常、也不谎报成功，交给调用方重试。
                raise TaskLockConflict(
                    {
                        "lock_key": key,
                        "group_name": group,
                        "task_type": "unknown",
                        "owner_id": "",
                    },
                    task_type,
                ) from None

            if allow_same_owner and holder.get("owner_id") == owner:
                # 同一持有者重复获取 = 续约（例如任务重试后重新进入入口）
                _touch(conn, key, owner, ttl, moment)
                refreshed = _fetch_active(conn, key)
                if refreshed is not None:
                    refreshed["acquired"] = True
                    refreshed["reentrant"] = True
                    return refreshed

            raise TaskLockConflict(holder, task_type) from None

        record = _fetch_active(conn, key)

    if record is None:  # pragma: no cover - 插入成功后必然能查到
        raise RuntimeError(f"获取任务锁后无法读回锁记录: {key}")

    record["acquired"] = True
    logger.info(
        "[task-lock] 获取锁 key=%s owner=%s ttl=%ss", key, owner, ttl
    )
    return record


def _touch(
    conn: sqlite3.Connection,
    lock_key: str,
    owner_id: str,
    ttl_seconds: int,
    moment: datetime,
) -> int:
    """续约：仅当锁仍是该 owner 的活跃锁时才更新。返回受影响行数。"""
    expires = moment + timedelta(seconds=ttl_seconds)
    cur = conn.execute(
        "UPDATE task_locks SET heartbeat_at = ?, expires_at = ? "
        "WHERE lock_key = ? AND owner_id = ? AND status = 'active'",
        (_fmt(moment), _fmt(expires), lock_key, owner_id),
    )
    return cur.rowcount or 0


def heartbeat(
    lock_key: str,
    owner_id: str,
    *,
    ttl_seconds: int | None = None,
    now: datetime | None = None,
) -> bool:
    """续约。返回是否续约成功。

    **返回 False 意味着锁已经不属于你了**（被超时回收后由别的任务抢走，
    或已被释放）。调用方应当据此中止当前工作，不要继续写正文 ——
    否则会与新的持有者同时改同一批数据。
    """
    if not lock_key or not owner_id:
        return False
    ttl = DEFAULT_TTL_SECONDS if ttl_seconds is None else int(ttl_seconds)
    if ttl <= 0:
        raise ValueError(f"ttl_seconds 必须为正数，收到 {ttl}")
    moment = _now(now)
    with get_db() as conn:
        changed = _touch(conn, lock_key, owner_id, ttl, moment)
    if not changed:
        logger.warning(
            "[task-lock] 续约失败：锁已不属于当前持有者 key=%s owner=%s", lock_key, owner_id
        )
    return bool(changed)


def release(
    lock_key: str,
    owner_id: str,
    *,
    now: datetime | None = None,
) -> bool:
    """释放锁。返回是否真的释放了。

    只释放 `owner_id` 匹配的活跃锁 —— 防止一个已被超时抢占的旧任务在收尾时
    把新持有者的锁误释放（ABA）。
    """
    if not lock_key:
        return False
    moment = _now(now)
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE task_locks SET status = 'released', released_at = ? "
            "WHERE lock_key = ? AND owner_id = ? AND status = 'active'",
            (_fmt(moment), lock_key, owner_id),
        )
        released = bool(cur.rowcount)
    if released:
        logger.info("[task-lock] 释放锁 key=%s owner=%s", lock_key, owner_id)
    return released


#: keepalive 后台续约的默认间隔：5 分钟一次，相对 30 分钟 TTL 留有 6 倍余量，
#: 单次 DB 写失败/调度延迟不会导致锁被 reclaim_stale 误回收。
DEFAULT_KEEPALIVE_INTERVAL_SECONDS = 300


def start_keepalive(
    lock_key: str,
    owner_id: str,
    *,
    interval_seconds: int = DEFAULT_KEEPALIVE_INTERVAL_SECONDS,
    ttl_seconds: int | None = None,
) -> Callable[[], None]:
    """ENHANCE 2026-10-04（T4）：启动后台心跳续约线程。

    场景：长任务（如包含大量图表的全量导出）本身可能超过 DEFAULT_TTL_SECONDS，
    而任务内部并无固定心跳点 —— 锁被 reclaim_stale 提前回收后，同组新任务
    可持锁并发执行。本函数把「续约」从业务代码解耦：拿锁后 start、
    finally 里调用返回的 stop 函数即可（先 stop 再 release，避免续约与
    释放交错把已释放行误更新 —— _touch 带 status='active' 条件，实际也打不中）。

    续约失败（锁已不属于自己）时只告警并停止续约，不中止业务：
    导出产物按时间戳命名互不覆盖，最坏后果退化为修复前的并发行为，
    中止反而会把可完成的导出变成硬失败。

    Returns:
        `stop()` —— 幂等停止函数（可重复调用）。
    """
    if not lock_key or not owner_id:
        return lambda: None
    stop_event = threading.Event()

    def _loop() -> None:
        # 首次等待间隔再续约：避免刚 acquire（expires=now+ttl）就白打一次心跳
        while not stop_event.wait(interval_seconds):
            try:
                if not heartbeat(lock_key, owner_id, ttl_seconds=ttl_seconds):
                    logger.warning(
                        "[task-lock] keepalive 续约失败（锁已被回收或易主）key=%s owner=%s",
                        lock_key, owner_id,
                    )
                    return
            except Exception as e:  # pragma: no cover - DB 异常下轮重试
                logger.warning("[task-lock] keepalive 心跳异常（下轮重试）key=%s: %s", lock_key, e)

    t = threading.Thread(
        target=_loop, name=f"task-lock-keepalive:{lock_key}", daemon=True
    )
    t.start()

    def _stop() -> None:
        stop_event.set()

    return _stop


# ---------------------------------------------------------------------------
# 查询 / 断言
# ---------------------------------------------------------------------------


def get_active_lock(
    task_type: str,
    project_id: int | None = None,
    *,
    group_name: str | None = None,
) -> dict | None:
    """查询某个任务组当前是否有活跃锁（不含超时回收，纯读）。"""
    group = group_name or group_for_task(task_type)
    if group is None:
        return None
    key = lock_key_for(group, project_id)
    with get_db(readonly=True) as conn:
        return _fetch_active(conn, key)


def list_locks(*, active_only: bool = True, limit: int = 200) -> list[dict]:
    """列出锁记录（诊断用）。"""
    sql = f"SELECT {_LOCK_COLUMNS} FROM task_locks"
    params: list[Any] = []
    if active_only:
        sql += " WHERE status = 'active'"
    sql += " ORDER BY heartbeat_at DESC LIMIT ?"
    params.append(int(limit))
    with get_db(readonly=True) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows if r is not None]


def is_lock_live(lock_key: str, *, now: datetime | None = None) -> bool:
    """锁是否仍然活跃且未过期（不触发回收）。"""
    if not lock_key:
        return False
    with get_db(readonly=True) as conn:
        return _read_state(conn, lock_key, _now(now))["live"]


def assert_group_available(
    task_type: str,
    *,
    project_id: int | None = None,
    now: datetime | None = None,
) -> None:
    """断言同组没有活跃任务，否则抛 `TaskLockConflict`。

    对应参考实现的 `assertTaskCanStart` 的「内存层」判定。

    ## 与参考实现的一处有意差异（不要当成 bug 去"修"）

    参考实现除了内存层，还额外检查持久化的
    「`contentGenerationTask.status === 'paused'` 就拒绝启动同组新任务」，
    并把「恢复」作为唯一的豁免条件（`payload.resume`）。

    本模块**没有**实现这条。原因是本软件的「暂停」与参考实现语义不同：
    `task_control.pause_generation` 会真正 set cancel_event 并 await 取消
    asyncio 任务，所以暂停后的生成循环**确实已经停了**，那条 paused 记录
    只是一个休眠的存档；而锁的心跳只在 `update_section_progress` 里发，
    暂停后自然停止续约、`DEFAULT_TTL_SECONDS`（30 分钟）后过期。

    后果：暂停超过 30 分钟后，同项目可以启动新的生成任务。
    这是可接受的 —— 旧任务的循环已经停了，不会真的并发写正文。
    若将来要严格对齐参考实现，需要额外加「paused 行 → 拒绝」的判据，
    并同时给恢复流程一个显式豁免位（否则会把自己的恢复路径挡住）。
    """
    group = group_for_task(task_type)
    if group is None:
        return
    key = lock_key_for(group, project_id)
    with get_db(readonly=True) as conn:
        state = _read_state(conn, key, _now(now))
    if not state["held"] or not state["live"]:
        # 无锁，或锁已超时（对调用方等价于无锁：acquire 会先回收它）
        return
    raise TaskLockConflict(state["holder"], task_type)


__all__ = [
    "DEFAULT_KEEPALIVE_INTERVAL_SECONDS",
    "DEFAULT_TTL_SECONDS",
    "GROUP_LABELS",
    "TASK_GROUPS",
    "TASK_LABELS",
    "TaskLockConflict",
    "acquire",
    "assert_group_available",
    "ensure_schema",
    "get_active_lock",
    "group_for_task",
    "heartbeat",
    "is_lock_live",
    "list_locks",
    "lock_key_for",
    "make_owner_id",
    "read_lock_state",
    "reclaim_foreign_locks",
    "reclaim_stale",
    "release",
    "release_owned_locks",
    "start_keepalive",
]
