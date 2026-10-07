"""后台任务快照：跨进程持久化 + 启动对账。

ENHANCE 2026-09-15（Task 9）：移植易标 `taskService.cjs` 的
`recoverInterruptedOutlineGenerationTask`（第 1002-1093 行）等一组
`recoverInterrupted*Task` 函数的语义。

## 参考实现到底做了什么

易标的「恢复」**不是**真的断点续跑，而是**启动时的诚实对账**：

1. 若中断的任务正好停在「等待用户选目录」且 Agent 会话可复用 → 恢复它；
2. **否则** → 把任务状态从 `running/paused` 改成 `error`，并写入
   `'上次目录生成未完成，请重新生成目录。'`，同时把持久化的 Agent 任务标成
   `interrupted`、清掉半成品模板。

也就是说，它解决的核心痛点是：**别让用户面对一个永远"运行中"的幽灵任务**。
真正的续跑依赖它的 Agent 运行时（`task-state.json` + Pi SDK session），不可移植。

## 本软件缺什么

本软件已经有 `v51_tasks` 表存「目录生成断点续生」的**上下文快照**，但它：

- 只在 `_run_outline_task` 的 `finally` 里写一次 —— **进程被强杀（SIGKILL /
  断电 / `docker kill`）时 `finally` 不执行，一行都不会落库**；
- 没有心跳。`expired_at` 是「保存时刻 + 2 小时」的固定值，运行期间不会续期；
- 于是「上次目录生成跑到哪一步、为什么没了」对用户完全不可见。

`TaskRegistry` 也帮不上忙：它只是进程内的 asyncio 单例，重启即空。

## 本模块的设计

一张 `background_tasks` 表，记录**运行期间**就持续存在的任务快照：

- **唯一性靠部分唯一索引** `uq_background_tasks_running ... WHERE status='running'`，
  与 `task_lock` 同构。同一 `(project_id, task_type)` 同时只允许一个运行行，
  历史行保留作审计轨迹。
- **存活靠心跳**，不靠「内存里没有就判定死了」—— 多进程下内存状态不可信
  （`TaskRegistry` 只看得见本进程，Celery worker 在独立进程）。
- **只读与写侧分离**：
  - `list_interruptions()` 是**纯读**，供 `GET /tasks/status` 这类轮询接口使用
    （不能因为用户刷了个页面就改库）；
  - `reconcile_stale()` 是写侧，只在应用启动、以及真正要启动新任务前调用。
- **中断行只保留最新一条可见**：`list_interruptions` 按
  `(project_id, task_type)` 取最新，避免前端列表里出现一堆同类条目。

## 与 task_lock 的分工

| | `task_lock` | `task_snapshot` |
|---|---|---|
| 回答的问题 | 「现在能不能启动这个任务」 | 「上次那个任务怎么了」 |
| 生命周期 | 活跃锁 + 释放即消失 | 运行期 + 终态长期保留 |
| 关键字段 | `expires_at`（TTL） | `heartbeat_at` + `status` |
| 冲突语义 | 抛 `TaskLockConflict` | 只登记/收敛，不阻断 |

两者互补：锁负责**互斥**，快照负责**可观测与对账**。
"""

from __future__ import annotations

from datetime import datetime, timedelta
import json
import logging
import sqlite3
from typing import Any
import uuid

from ..db import get_db


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

#: 任务类型 → 中文名（用于拼面向用户的提示语）
TASK_TYPE_LABELS: dict[str, str] = {
    "outline_v51": "目录生成",
    "content_generation": "正文生成",
}

#: 状态取值
STATUS_RUNNING = "running"
STATUS_INTERRUPTED = "interrupted"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"
STATUS_SUPERSEDED = "superseded"

#: 终态集合
TERMINAL_STATUSES = (
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_CANCELLED,
    STATUS_SUPERSEDED,
)

#: 心跳超时阈值。心跳间隔 60 秒，取 5 倍余量：
#: 既不会因为一次 GC / 网络抖动误判，也不会让真正死掉的任务长期占用「运行中」。
#: **不要**改成「进程重启即判定全部 running 行已死」——多 worker 部署下
#: 后启动的进程会把先启动进程正在跑的任务全部误杀（这正是 Task 8 修过的同类缺陷）。
DEFAULT_STALE_SECONDS = 300

_TIME_FMT = "%Y-%m-%d %H:%M:%S"


class TaskSnapshotConflict(RuntimeError):
    """同一 `(project_id, task_type)` 已有运行中的快照。

    与 `task_lock.TaskLockConflict` 的区别：这个异常表示「数据层面已有运行行」，
    通常意味着调用方忘了先抢锁，或者上一次运行没能正常收尾。
    携带 `existing`（既有行）供调用方诊断。
    """

    def __init__(self, existing: dict[str, Any] | None, task_type: str):
        self.existing = existing or {}
        self.task_type = task_type
        label = TASK_TYPE_LABELS.get(task_type, task_type)
        super().__init__(f"「{label}」已有运行中的任务快照，未正常收尾")


# ---------------------------------------------------------------------------
# 时间 / 行转换
# ---------------------------------------------------------------------------


def _fmt(moment: datetime) -> str:
    return moment.strftime(_TIME_FMT)


def _now(now: datetime | None = None) -> datetime:
    return now if now is not None else datetime.now()


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.strptime(str(value), _TIME_FMT)
    except ValueError:
        return None


_COLUMNS = (
    "id, task_id, project_id, task_type, status, phase, progress, current_step, "
    "total_steps, message, owner_id, payload_json, started_at, heartbeat_at, "
    "updated_at, finished_at, error_message, interrupted_reason, acknowledged"
)


def _row_to_dict(row: sqlite3.Row | tuple | None) -> dict[str, Any] | None:
    if row is None:
        return None
    if isinstance(row, sqlite3.Row):
        # sqlite3.Row 迭代出的是**值**不是键，必须显式 .keys()
        # （ruff SIM118 在此为误报，与 task_lock 同）。
        return {k: row[k] for k in row.keys()}  # noqa: SIM118
    names = [c.strip() for c in _COLUMNS.split(",")]
    return dict(zip(names, row, strict=False))


def _decorate(record: dict[str, Any] | None, moment: datetime, stale_seconds: int) -> dict[str, Any] | None:
    """补充派生字段：中文标签、是否心跳超时、剩余可见秒数。"""
    if record is None:
        return None
    out = dict(record)
    out["task_type_label"] = TASK_TYPE_LABELS.get(out.get("task_type", ""), out.get("task_type", ""))
    hb = _parse(out.get("heartbeat_at"))
    if hb is None:
        out["heartbeat_age_seconds"] = None
        out["stale"] = True
    else:
        age = int((moment - hb).total_seconds())
        out["heartbeat_age_seconds"] = age
        out["stale"] = age > stale_seconds
    return out


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
        CREATE TABLE IF NOT EXISTS background_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            project_id INTEGER NOT NULL,
            task_type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            phase TEXT NOT NULL DEFAULT '',
            progress REAL NOT NULL DEFAULT 0,
            current_step INTEGER NOT NULL DEFAULT 0,
            total_steps INTEGER NOT NULL DEFAULT 0,
            message TEXT NOT NULL DEFAULT '',
            owner_id TEXT NOT NULL DEFAULT '',
            payload_json TEXT NOT NULL DEFAULT '{}',
            started_at TEXT NOT NULL,
            heartbeat_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            finished_at TEXT,
            error_message TEXT NOT NULL DEFAULT '',
            interrupted_reason TEXT NOT NULL DEFAULT '',
            acknowledged INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    # 关键：唯一性只约束「运行中」的行。终态行（含 interrupted）可以多行共存，
    # 这样既能保证「同项目同类型只有一个在跑」，又留了完整的中断审计轨迹。
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_background_tasks_running
            ON background_tasks(project_id, task_type) WHERE status = 'running'
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_background_tasks_task_id ON background_tasks(task_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_background_tasks_lookup "
        "ON background_tasks(project_id, task_type, status)"
    )


# ---------------------------------------------------------------------------
# 内部读写
# ---------------------------------------------------------------------------


def _fetch_running(
    conn: sqlite3.Connection, project_id: int, task_type: str
) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM background_tasks "
        "WHERE project_id = ? AND task_type = ? AND status = 'running'",
        (project_id, task_type),
    ).fetchone()
    return _row_to_dict(row)


def _fetch_by_task_id(conn: sqlite3.Connection, task_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM background_tasks WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    return _row_to_dict(row)


def _reconcile_rows(
    conn: sqlite3.Connection,
    moment: datetime,
    stale_seconds: int,
    *,
    project_id: int | None = None,
    task_type: str | None = None,
) -> list[dict[str, Any]]:
    """把心跳超时的 running 行收敛为 interrupted。返回被收敛的行（收敛前快照）。

    `project_id` / `task_type` 用于把收敛范围限定到单个键上：`acknowledge`
    只需要处理用户当前在看的那一条，不必顺手改写全库（虽然全库收敛也是幂等的，
    但会在别的项目的审计轨迹里留下与本项目无关的噪声）。
    """
    threshold = _fmt(moment - timedelta(seconds=stale_seconds))
    where = "status = 'running' AND heartbeat_at < ?"
    where_params: list[Any] = [threshold]
    if project_id is not None:
        where += " AND project_id = ?"
        where_params.append(project_id)
    if task_type is not None:
        where += " AND task_type = ?"
        where_params.append(task_type)
    stale_rows = [
        _row_to_dict(r)
        for r in conn.execute(
            f"SELECT {_COLUMNS} FROM background_tasks WHERE {where}",
            tuple(where_params),
        ).fetchall()
    ]
    if not stale_rows:
        return []
    reason = f"心跳停止超过 {stale_seconds} 秒，判定为进程异常退出"
    conn.execute(
        "UPDATE background_tasks SET status = ?, interrupted_reason = ?, "
        f"updated_at = ?, finished_at = ? WHERE {where}",
        (STATUS_INTERRUPTED, reason, _fmt(moment), _fmt(moment), *where_params),
    )
    for row in stale_rows:
        row["status"] = STATUS_INTERRUPTED
        row["interrupted_reason"] = reason
    return stale_rows


def _supersede_previous(conn: sqlite3.Connection, project_id: int, task_type: str, moment: datetime) -> int:
    """把同项目同类型的历史中断行标记为 superseded。

    语义：用户/系统已经开始新一轮生成，旧的中断提示不再需要展示。
    """
    cur = conn.execute(
        "UPDATE background_tasks SET status = ?, updated_at = ? "
        "WHERE project_id = ? AND task_type = ? AND status = ?",
        (STATUS_SUPERSEDED, _fmt(moment), project_id, task_type, STATUS_INTERRUPTED),
    )
    return cur.rowcount or 0


# ---------------------------------------------------------------------------
# 登记 / 续约 / 终态
# ---------------------------------------------------------------------------


def register(
    project_id: int,
    task_type: str,
    *,
    task_id: str | None = None,
    total_steps: int = 0,
    phase: str = "",
    progress: float = 0.0,
    message: str = "",
    payload: dict | None = None,
    owner_id: str = "",
    stale_seconds: int | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """登记一个运行中的任务快照。

    先做两件事，再插入：

    1. `_reconcile_rows` —— 把心跳超时的历史 running 行收敛为 interrupted
       （否则唯一索引会挡住新行，且那行其实早就死了）；
    2. `_supersede_previous` —— 把该键上遗留的中断行标为 superseded，
       因为调用方此刻正在开启新一轮。

    Args:
        project_id: 项目 ID。
        task_type: 任务类型（`outline_v51` / `content_generation`）。
        task_id: 任务标识；不传则自动生成。
        total_steps: 总步骤数。
        phase: 当前阶段（人可读）。
        progress: 进度百分比 0-100。
        message: 当前提示语。
        payload: 恢复所需的附加数据（JSON 可序列化）。
        owner_id: 持有者标识（与 `task_lock` 的 owner 对应，便于串联诊断）。
        stale_seconds: 覆盖默认心跳超时阈值。
        now: 注入当前时间（测试用）。

    Returns:
        新登记的行（dict），额外带 `registered=True`；若同一 owner 重复登记
        则返回既有行并带 `reentrant=True`。

    Raises:
        TaskSnapshotConflict: 同键已有**其他**持有者的运行行。
    """
    moment = _now(now)
    stale = DEFAULT_STALE_SECONDS if stale_seconds is None else int(stale_seconds)
    tid = task_id or f"{task_type}:{project_id}:{uuid.uuid4().hex[:12]}"
    stamp = _fmt(moment)
    payload_text = json.dumps(payload or {}, ensure_ascii=False)

    with get_db() as conn:
        _reconcile_rows(conn, moment, stale)
        _supersede_previous(conn, project_id, task_type, moment)

        try:
            conn.execute(
                "INSERT INTO background_tasks "
                "(task_id, project_id, task_type, status, phase, progress, current_step, "
                " total_steps, message, owner_id, payload_json, started_at, heartbeat_at, "
                " updated_at, acknowledged) "
                "VALUES (?, ?, ?, 'running', ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    tid,
                    project_id,
                    task_type,
                    phase,
                    float(progress or 0),
                    int(total_steps or 0),
                    message,
                    owner_id,
                    payload_text,
                    stamp,
                    stamp,
                    stamp,
                ),
            )
        except sqlite3.IntegrityError:
            existing = _fetch_running(conn, project_id, task_type)
            if existing is None:
                # 极端竞态：冲突行在本次事务里刚好被收敛。交给调用方重试。
                raise TaskSnapshotConflict(None, task_type) from None
            if owner_id and existing.get("owner_id") == owner_id:
                # 同一持有者重复登记 = 续约（任务重试后重新进入入口）
                conn.execute(
                    "UPDATE background_tasks SET heartbeat_at = ?, updated_at = ?, "
                    "total_steps = CASE WHEN ? > 0 THEN ? ELSE total_steps END, "
                    "message = CASE WHEN ? <> '' THEN ? ELSE message END "
                    "WHERE task_id = ? AND status = 'running'",
                    (stamp, stamp, int(total_steps or 0), int(total_steps or 0),
                     message, message, existing["task_id"]),
                )
                refreshed = _fetch_by_task_id(conn, existing["task_id"])
                if refreshed is not None:
                    refreshed["registered"] = True
                    refreshed["reentrant"] = True
                    return refreshed
            raise TaskSnapshotConflict(existing, task_type) from None

        record = _fetch_by_task_id(conn, tid)

    if record is None:  # pragma: no cover - 插入成功后必然能查到
        raise RuntimeError(f"登记任务快照后无法读回: {tid}")
    record["registered"] = True
    logger.info(
        "[task-snapshot] 登记 %s project=%d task_id=%s", task_type, project_id, tid
    )
    return record


def heartbeat(task_id: str, *, now: datetime | None = None) -> bool:
    """刷新心跳。返回是否刷新成功。

    返回 False 说明该行已经不是运行态（已被收敛为 interrupted / 已终态）。
    与 `task_lock.heartbeat` 一样，调用方**只告警不中断**：任务已经跑了一半，
    中断只会留下更破碎的状态，但必须留日志痕迹。
    """
    if not task_id:
        return False
    moment = _now(now)
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE background_tasks SET heartbeat_at = ?, updated_at = ? "
            "WHERE task_id = ? AND status = 'running'",
            (_fmt(moment), _fmt(moment), task_id),
        )
        changed = cur.rowcount or 0
    if not changed:
        logger.warning("[task-snapshot] 心跳失败：快照已非运行态 task_id=%s", task_id)
    return bool(changed)


def update_progress(
    task_id: str,
    *,
    phase: str | None = None,
    progress: float | None = None,
    current_step: int | None = None,
    total_steps: int | None = None,
    message: str | None = None,
    now: datetime | None = None,
) -> bool:
    """更新阶段/进度，并顺带刷新心跳。只在运行态生效。"""
    if not task_id:
        return False
    moment = _now(now)
    sets = ["heartbeat_at = ?", "updated_at = ?"]
    params: list[Any] = [_fmt(moment), _fmt(moment)]
    if phase is not None:
        sets.append("phase = ?")
        params.append(phase)
    if progress is not None:
        sets.append("progress = ?")
        params.append(float(progress))
    if current_step is not None:
        sets.append("current_step = ?")
        params.append(int(current_step))
    if total_steps is not None:
        sets.append("total_steps = ?")
        params.append(int(total_steps))
    if message is not None:
        sets.append("message = ?")
        params.append(message)
    params.append(task_id)
    with get_db() as conn:
        cur = conn.execute(
            f"UPDATE background_tasks SET {', '.join(sets)} "
            "WHERE task_id = ? AND status = 'running'",
            tuple(params),
        )
        return bool(cur.rowcount)


def finish(
    task_id: str,
    status: str = STATUS_COMPLETED,
    *,
    error_message: str = "",
    interrupted_reason: str = "",
    now: datetime | None = None,
) -> bool:
    """写入终态。返回是否真的改动了行。

    允许把 running 行收尾成 completed/failed/cancelled/interrupted。
    对已经是终态的行不重复写（返回 False），避免把审计轨迹覆盖掉。
    """
    if not task_id:
        return False
    if status not in (STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED, STATUS_INTERRUPTED):
        raise ValueError(f"非法终态: {status}")
    moment = _now(now)
    with get_db() as conn:
        cur = conn.execute(
            "UPDATE background_tasks SET status = ?, error_message = ?, "
            "interrupted_reason = ?, finished_at = ?, updated_at = ? "
            "WHERE task_id = ? AND status = 'running'",
            (
                status,
                error_message,
                interrupted_reason,
                _fmt(moment),
                _fmt(moment),
                task_id,
            ),
        )
        changed = cur.rowcount or 0
    if changed:
        logger.info("[task-snapshot] 收尾 task_id=%s status=%s", task_id, status)
    return bool(changed)


# ---------------------------------------------------------------------------
# 查询（只读）
# ---------------------------------------------------------------------------


def get_by_task_id(task_id: str, *, now: datetime | None = None) -> dict[str, Any] | None:
    """按 task_id 查询（带派生字段）。纯读。"""
    moment = _now(now)
    with get_db(readonly=True) as conn:
        return _decorate(_fetch_by_task_id(conn, task_id), moment, DEFAULT_STALE_SECONDS)


def list_running(project_id: int | None = None, *, now: datetime | None = None) -> list[dict[str, Any]]:
    """列出运行中的快照（含心跳超时但仍标记 running 的行）。纯读。"""
    moment = _now(now)
    sql = f"SELECT {_COLUMNS} FROM background_tasks WHERE status = 'running'"
    params: tuple = ()
    if project_id is not None:
        sql += " AND project_id = ?"
        params = (project_id,)
    sql += " ORDER BY id DESC"
    with get_db(readonly=True) as conn:
        rows = [_row_to_dict(r) for r in conn.execute(sql, params).fetchall()]
    return [d for d in (_decorate(r, moment, DEFAULT_STALE_SECONDS) for r in rows) if d is not None]


def list_interruptions(
    project_id: int | None = None,
    *,
    task_type: str | None = None,
    stale_seconds: int | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """列出「需要告知用户的中断」。

    **纯读，不写库** —— 供 `GET /tasks/status` 这类轮询接口使用。
    与 `reconcile_stale` 的区别：这里只是**报告**心跳超时的 running 行也算中断
    （`stale=True`），并不真的把它改成 interrupted；写库交给启动对账与
    任务入口去做。

    每个 `(project_id, task_type)` 只返回**最新**一条，避免前端列表里出现
    一堆同类条目（前端 `RecoveryBanner` 用 `opt.type` 当 React key）。

    Returns:
        中断行列表，每条带 `stale` / `heartbeat_age_seconds` /
        `task_type_label` / `effective_status`（恒为 'interrupted'）。
    """
    moment = _now(now)
    stale = DEFAULT_STALE_SECONDS if stale_seconds is None else int(stale_seconds)
    sql = f"SELECT {_COLUMNS} FROM background_tasks WHERE status IN (?, ?)"
    params: list[Any] = [STATUS_INTERRUPTED, STATUS_RUNNING]
    if project_id is not None:
        sql += " AND project_id = ?"
        params.append(project_id)
    if task_type is not None:
        sql += " AND task_type = ?"
        params.append(task_type)
    sql += " ORDER BY id DESC"
    with get_db(readonly=True) as conn:
        rows = [_row_to_dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]

    latest: dict[tuple, dict[str, Any]] = {}
    for raw in rows:
        item = _decorate(raw, moment, stale)
        if item is None:
            continue
        if item.get("status") == STATUS_INTERRUPTED and item.get("acknowledged"):
            continue
        if item.get("status") == STATUS_RUNNING and not item.get("stale"):
            # 还在正常心跳的运行中任务不算中断
            continue
        key = (item.get("project_id"), item.get("task_type"))
        if key in latest:
            continue
        item["effective_status"] = STATUS_INTERRUPTED
        latest[key] = item
    return list(latest.values())


def interruption_summary(item: dict[str, Any]) -> str:
    """把中断快照拼成一句面向用户的话（对应参考实现的固定文案，但更具体）。"""
    label = item.get("task_type_label") or item.get("task_type") or "后台任务"
    phase = item.get("phase") or "未知阶段"
    progress = item.get("progress")
    try:
        pct = int(round(float(progress or 0)))
    except (TypeError, ValueError):
        pct = 0
    return f"上次{label}在「{phase}」阶段中断（进度约 {pct}%），请重新发起{label}"


# ---------------------------------------------------------------------------
# 写侧：启动对账 / 确认
# ---------------------------------------------------------------------------


def reconcile_stale(*, stale_seconds: int | None = None, now: datetime | None = None) -> list[dict[str, Any]]:
    """把心跳超时的运行中快照收敛为 interrupted。返回被收敛的行。

    调用时机（**只在写侧用**）：

    1. 应用启动钩子 —— 进程重启后把上次遗留的幽灵任务落到明面上；
    2. 任务入口抢到锁、准备登记新快照之前 —— 防止唯一索引被僵尸行挡住。

    **判据是心跳，不是「进程重启了所以全死了」**：多 worker 部署下，
    后启动的进程会把先启动进程正在跑的任务全部误杀。
    """
    moment = _now(now)
    stale = DEFAULT_STALE_SECONDS if stale_seconds is None else int(stale_seconds)
    with get_db() as conn:
        rows = _reconcile_rows(conn, moment, stale)
    if rows:
        for row in rows:
            logger.warning(
                "[task-snapshot] 对账：%s project=%s 中断于「%s」阶段（进度 %.0f%%）",
                TASK_TYPE_LABELS.get(row.get("task_type", ""), row.get("task_type")),
                row.get("project_id"),
                row.get("phase") or "未知",
                float(row.get("progress") or 0),
            )
    return rows


def acknowledge(
    project_id: int, *, task_type: str | None = None, now: datetime | None = None
) -> int:
    """把中断快照标记为「用户已知晓」。返回受影响行数。

    用户点了「重新生成」或者手动关掉提示后调用，避免横幅一直挂着。

    这里会先把本项目范围内心跳超时的 running 行收敛为 interrupted，保证
    「`list_interruptions` 报告得出来的，`acknowledge` 就确认得掉」——两者必须
    共用同一套「什么算中断」的判据，否则前端会出现点不掉的假中断横幅。
    """
    moment = _now(now)
    sql = (
        "UPDATE background_tasks SET acknowledged = 1, updated_at = ? "
        "WHERE status = ? AND project_id = ?"
    )
    params: list[Any] = [_fmt(moment), STATUS_INTERRUPTED, project_id]
    if task_type:
        sql += " AND task_type = ?"
        params.append(task_type)
    with get_db() as conn:
        # BUG-FIX 2026-09-15（中）：先收敛本项目范围内「心跳超时的 running 行」。
        # `list_interruptions` 会把它们报告为中断（多 worker / 未重启场景下启动
        # 对账还没跑到），若这里不先收敛，`UPDATE ... WHERE status='interrupted'`
        # 匹配不到任何行 → 接口返回 0 → 前端横幅点不掉。
        _reconcile_rows(
            conn, moment, DEFAULT_STALE_SECONDS, project_id=project_id, task_type=task_type
        )
        cur = conn.execute(sql, tuple(params))
        count = cur.rowcount or 0
    if count:
        logger.info("[task-snapshot] 用户确认中断 project=%d 共 %d 条", project_id, count)
    return count


def read_state(
    conn: sqlite3.Connection,
    project_id: int,
    task_type: str,
    *,
    now: datetime | None = None,
    stale_seconds: int | None = None,
) -> dict[str, Any] | None:
    """在**调用方已有的连接**上读运行中快照（不触发任何写）。

    与 `task_lock.read_lock_state` 同样的理由：`get_db` 退出时会 `commit()`，
    调用方若正处于事务中，嵌套调用会把外层事务提前提交掉。
    """
    moment = _now(now)
    stale = DEFAULT_STALE_SECONDS if stale_seconds is None else int(stale_seconds)
    return _decorate(_fetch_running(conn, project_id, task_type), moment, stale)


__all__ = [
    "DEFAULT_STALE_SECONDS",
    "STATUS_CANCELLED",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_INTERRUPTED",
    "STATUS_RUNNING",
    "STATUS_SUPERSEDED",
    "TASK_TYPE_LABELS",
    "TERMINAL_STATUSES",
    "TaskSnapshotConflict",
    "acknowledge",
    "ensure_schema",
    "finish",
    "get_by_task_id",
    "heartbeat",
    "interruption_summary",
    "list_interruptions",
    "list_running",
    "read_state",
    "reconcile_stale",
    "register",
    "update_progress",
]
