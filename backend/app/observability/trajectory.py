"""任务运行轨迹（ENHANCE 2026-10-29）。

记录任务生命周期（created/queued/started/running/paused/resumed/stopping/
stopped/completed/failed）与状态变化（from_status/to_status/原因/操作者），
供「运行轨迹视图」按 task_id 查询与时间线还原。

设计：
- `record_state_change()`：写库（obs_trajectory 表）+ 进程内缓存 + 非法迁移告警。
- `get_trajectory()` / `list_trajectories()`：按 task_id / 状态 / 类型 / 时间查询。
- `TaskTracker`：便捷类，管理单任务状态（ensure/transition 自动推导 from）。

只依赖标准库 + 本包 fields；DB 访问用函数内延迟导入避免循环。
"""
from __future__ import annotations

import threading
import time
from typing import Any

from . import fields as _f

#: 进程内环形缓存（task_id -> 最近 N 条）
_traj_cache: dict[str, list[dict[str, Any]]] = {}
_cache_lock = threading.Lock()
_MAX_CACHE_PER_TASK = 200


def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()) + f".{int(time.time() * 1000) % 1000:03d}"


def _normalize_state(state: str) -> str:
    return (state or "").strip().lower()


def validate_transition(from_status: str, to_status: str) -> bool:
    """状态迁移是否合法（见 fields.TRAJECTORY_TRANSITIONS）。"""
    allowed = _f.TRAJECTORY_TRANSITIONS.get(_normalize_state(from_status), ())
    return _normalize_state(to_status) in allowed


def record_state_change(
    *,
    task_id: str,
    task_type: str = "",
    from_status: str,
    to_status: str,
    reason: str = "",
    operator: str = "system",
    project_id: str = "",
    **fields: Any,
) -> dict[str, Any]:
    """记录一次状态变化；非法迁移记 WARN（TASK_001）但仍落库（标记 warning）。"""
    from_status = _normalize_state(from_status)
    to_status = _normalize_state(to_status)
    valid = validate_transition(from_status, to_status)
    rec: dict[str, Any] = {
        "task_id": str(task_id),
        "task_type": task_type,
        "project_id": str(project_id or ""),
        "from_status": from_status or "-",
        "to_status": to_status,
        "reason": reason,
        "operator": operator,
        "trace_id": fields.pop("trace_id", "-"),
        "created_at": _ts(),
        "valid": valid,
        "id": None,
    }
    with _cache_lock:
        bucket = _traj_cache.setdefault(str(task_id), [])
        bucket.append(rec)
        if len(bucket) > _MAX_CACHE_PER_TASK:
            del bucket[: len(bucket) - _MAX_CACHE_PER_TASK]

    if not valid:
        try:
            from . import structured as _s

            _s.log_error(
                "state.change", module="task",
                message=f"任务 {task_id} 非法状态迁移 {from_status}->{to_status}",
                error_code="TASK_001", error_layer="validation", retryable=False,
                task_id=task_id, from_status=from_status, to_status=to_status, reason=reason,
            )
        except Exception:
            pass

    try:
        rec["id"] = _persist(rec)
    except Exception:
        rec["id"] = -1
    return rec


def _persist(rec: dict[str, Any]) -> int | None:
    from ..db import get_db_with_retry

    with get_db_with_retry() as conn:
        cur = conn.execute(
            "INSERT INTO obs_trajectory "
            "(task_id, task_type, project_id, from_status, to_status, reason, operator, "
            " trace_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rec["task_id"], rec["task_type"], rec["project_id"],
                rec["from_status"], rec["to_status"], rec["reason"],
                rec["operator"], rec.get("trace_id", "-"), rec["created_at"],
            ),
        )

def get_trajectory(task_id: str) -> list[dict[str, Any]]:
    """按 task_id 返回按时间排序的运行轨迹。"""
    try:
        from ..db import get_db_with_retry

        with get_db_with_retry(readonly=True) as conn:
            rows = conn.execute(
                "SELECT * FROM obs_trajectory WHERE task_id = ? ORDER BY id",
                (str(task_id),),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        with _cache_lock:
            return list(_traj_cache.get(str(task_id), []))


def list_trajectories(
    *,
    status: str = "",
    task_type: str = "",
    task_id: str = "",
    project_id: str = "",
    limit: int = 200,
    offset: int = 0,
) -> dict[str, Any]:
    """按条件分页查询运行轨迹（任务列表）。"""
    clauses: list[str] = []
    params: list[Any] = []
    if status:
        clauses.append("to_status = ?")
        params.append(_normalize_state(status))
    if task_type:
        clauses.append("task_type = ?")
        params.append(task_type)
    if task_id:
        clauses.append("task_id = ?")
        params.append(str(task_id))
    if project_id:
        clauses.append("project_id = ?")
        params.append(str(project_id))
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    try:
        from ..db import get_db_with_retry

        with get_db_with_retry(readonly=True) as conn:
            total = conn.execute(
                f"SELECT COUNT(*) AS c FROM obs_trajectory {where}", params
            ).fetchone()["c"]
            rows = conn.execute(
                f"SELECT * FROM obs_trajectory {where} "
                "ORDER BY id DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
        return {"total": total, "items": [dict(r) for r in rows]}
    except Exception:
        with _cache_lock:
            items = list(_traj_cache.get(task_id, [])) if task_id else []
        return {"total": len(items), "items": items[:limit]}


class TaskTracker:
    """单任务状态跟踪器（自动推导 from_status，校验迁移）。"""

    def __init__(self, task_id: str, task_type: str = "", project_id: str = "") -> None:
        self.task_id = str(task_id)
        self.task_type = task_type
        self.project_id = str(project_id)
        self._state = "created"
        self._history: list[dict[str, Any]] = []

    @property
    def state(self) -> str:
        return self._state

    def ensure(self, to_status: str, reason: str = "", operator: str = "system", **fields: Any) -> dict[str, Any]:
        """记录一次迁移；若 to==from 则仅刷新时间戳。"""
        if _normalize_state(to_status) == self._state:
            rec = record_state_change(
                task_id=self.task_id, task_type=self.task_type,
                from_status=self._state, to_status=self._state,
                reason=reason or "noop", operator=operator,
                project_id=self.project_id, **fields,
            )
            self._history.append(rec)
            return rec
        rec = record_state_change(
            task_id=self.task_id, task_type=self.task_type,
            from_status=self._state, to_status=to_status,
            reason=reason, operator=operator,
            project_id=self.project_id, **fields,
        )
        self._state = _normalize_state(to_status)
        self._history.append(rec)
        return rec

    def start(self, reason: str = "", operator: str = "system", **fields: Any) -> dict[str, Any]:
        return self.ensure("started", reason, operator, **fields)

    def run(self, reason: str = "", operator: str = "system", **fields: Any) -> dict[str, Any]:
        return self.ensure("running", reason, operator, **fields)

    def pause(self, reason: str = "", operator: str = "system", **fields: Any) -> dict[str, Any]:
        return self.ensure("paused", reason, operator, **fields)

    def resume(self, reason: str = "", operator: str = "system", **fields: Any) -> dict[str, Any]:
        return self.ensure("resumed", reason, operator, **fields)

    def stopping(self, reason: str = "", operator: str = "system", **fields: Any) -> dict[str, Any]:
        return self.ensure("stopping", reason, operator, **fields)

    def stopped(self, reason: str = "", operator: str = "system", **fields: Any) -> dict[str, Any]:
        return self.ensure("stopped", reason, operator, **fields)

    def complete(self, reason: str = "", operator: str = "system", **fields: Any) -> dict[str, Any]:
        return self.ensure("completed", reason, operator, **fields)

    def fail(self, reason: str = "", operator: str = "system", **fields: Any) -> dict[str, Any]:
        return self.ensure("failed", reason, operator, **fields)

    def history(self) -> list[dict[str, Any]]:
        return list(self._history)

        return cur.lastrowid
