"""废标检查报告的定时调度**执行器**。

BUG-FIX 2026-09-18（高）：`rejection_report_schedules` 表此前**只有 CRUD、
没有任何执行器** —— 路由层提供了「创建/启用/禁用/删除/列表」五个端点，
用户能看到调度"已启用"，但全库没有任何代码读取 `schedule_type` /
`schedule_time` 并真正生成报告。这是一张**只写不读的表**，等同于空任务：
用户配好了每日报告，却永远收不到任何产物，也没有任何报错。

本模块补上执行侧：

  - `run_due_schedules()` 扫描启用中的调度，对「已到点且本周期未生成过」的
    调度调用 `rejection.export_report()` 生成 DOCX 报告；
  - 生成结果回写 `last_run_at` / `run_count` / `last_error` 三列（见
    `db_migrations.ensure_enhancement_tables`），既让前端能看到执行记录，
    也保证每小时轮询一次时不会重复生成；
  - 单条调度失败不影响其余调度，失败原因落 `last_error`。

由 `app/celery_app.run_scheduled_reports`（beat 每小时）驱动；
也可通过 `dispatch_task('app.celery_app.run_scheduled_reports', force_inline=True)`
在进程内直接调用。
"""

from __future__ import annotations

from datetime import date, datetime, time
import logging
from typing import Any


logger = logging.getLogger(__name__)


# 支持的周期类型
_PERIODIC_TYPES = frozenset({"daily", "weekly", "monthly"})

# 周期型调度未配置 `schedule_time` 时的兜底时刻。
# 不用「未配置即跳过」——那会让用户配好了却永远等不到（又一处空任务），
# 而是按当天 00:00 处理。
_DEFAULT_TIME_OF_DAY = time(0, 0)

_TIME_FMT = "%Y-%m-%d %H:%M:%S"


def _fmt(moment: datetime) -> str:
    return moment.strftime(_TIME_FMT)


def _parse_dt(value: Any) -> datetime | None:
    """解析 `datetime('now','localtime')` 写出的时间串。"""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    for fmt in (_TIME_FMT, "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _parse_schedule_time(raw: Any) -> tuple[str, time | None, datetime | None]:
    """解析 `schedule_time`。

    Returns:
        `(kind, time_of_day, exact_datetime)`：
        - kind="time_of_day" → `HH:MM`（周期型用）
        - kind="datetime"    → 具体时间点（`once` 用，ISO 串）
        - kind="none"        → 未配置或无法解析
    """
    text = ("" if raw is None else str(raw)).strip()
    if not text:
        return "none", None, None
    # 先试 HH:MM / HH:MM:SS
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return "time_of_day", datetime.strptime(text, fmt).time(), None
        except ValueError:
            continue
    exact = _parse_dt(text)
    if exact is not None:
        return "datetime", None, exact
    return "none", None, None


def _week_key(moment: datetime) -> tuple[int, int]:
    iso = moment.isocalendar()
    return (iso[0], iso[1])


def is_due(schedule: dict[str, Any], now: datetime) -> bool:
    """判断某条调度在 `now` 时刻是否应当生成报告。

    判定只看「周期是否到了 + 本周期是否已生成过」，不依赖任何进程内状态
    （多条 worker 同时轮询也不会重复生成）。
    """
    stype = str(schedule.get("schedule_type") or "daily").strip().lower()
    kind, tod, exact = _parse_schedule_time(schedule.get("schedule_time"))
    run_count = int(schedule.get("run_count") or 0)
    last_run = _parse_dt(schedule.get("last_run_at"))

    if stype == "once":
        # 一次性：只跑一次（run_count 是唯一可靠的判据，进程重启也不影响）
        if run_count:
            return False
        if kind == "datetime" and exact is not None:
            return now >= exact
        if kind == "time_of_day" and tod is not None:
            return now.time() >= tod
        return True

    if stype not in _PERIODIC_TYPES:
        return False

    moment_of_day = tod if (kind == "time_of_day" and tod is not None) else _DEFAULT_TIME_OF_DAY
    if now.time() < moment_of_day:
        return False
    if last_run is None:
        return True

    if stype == "daily":
        return last_run.date() < now.date()
    if stype == "weekly":
        return _week_key(last_run) != _week_key(now)
    # monthly
    return (last_run.year, last_run.month) != (now.year, now.month)


def due_reason(schedule: dict[str, Any], now: datetime) -> str:
    """返回未到点的原因（便于日志与测试定位），到点时返回空串。"""
    if not int(schedule.get("enabled") or 0):
        return "disabled"
    stype = str(schedule.get("schedule_type") or "daily").strip().lower()
    if stype != "once" and stype not in _PERIODIC_TYPES:
        return f"unsupported schedule_type={stype}"
    if is_due(schedule, now):
        return ""
    kind, tod, _exact = _parse_schedule_time(schedule.get("schedule_time"))
    if stype == "once":
        return "once 已执行过" if int(schedule.get("run_count") or 0) else "尚未到指定时间"
    moment_of_day = tod if (kind == "time_of_day" and tod is not None) else _DEFAULT_TIME_OF_DAY
    if now.time() < moment_of_day:
        return f"未到当日触发时刻 {moment_of_day:%H:%M}"
    return f"本周期已生成（last_run_at={schedule.get('last_run_at')}）"


def _record_result(schedule_id: int, moment: datetime, *, error: str | None = None) -> None:
    """回写执行结果：成功记 last_run_at/run_count，失败记 last_error。"""
    from ..db import get_db

    with get_db() as conn:
        if error is not None:
            conn.execute(
                "UPDATE rejection_report_schedules SET last_error = ?, "
                "updated_at = datetime('now','localtime') WHERE id = ?",
                (error[:500], schedule_id),
            )
        else:
            conn.execute(
                "UPDATE rejection_report_schedules SET last_run_at = ?, last_error = NULL, "
                "run_count = COALESCE(run_count, 0) + 1, "
                "updated_at = datetime('now','localtime') WHERE id = ?",
                (_fmt(moment), schedule_id),
            )


def run_due_schedules(*, now: datetime | None = None, limit: int = 100) -> dict[str, Any]:
    """扫描并生成所有到点的报告。

    Args:
        now: 判定时刻（测试注入用）；缺省取本机当前时间
        limit: 最多扫描多少条调度

    Returns:
        {"status": "ok"|"failed", "scanned": int, "generated": int,
         "skipped": int, "failed": int, "details": [...]}
    """
    from . import rejection as _rejection

    moment = now if now is not None else datetime.now()
    result: dict[str, Any] = {
        "status": "ok",
        "scanned": 0,
        "generated": 0,
        "skipped": 0,
        "failed": 0,
        "details": [],
    }

    schedules = _rejection.get_all_report_schedules(limit) or []
    for schedule in schedules:
        result["scanned"] += 1
        # 防御：`get_all_report_schedules` 返回 dict，但调用方（含测试）也可能直接
        # 传 sqlite3.Row / 元组转换来的对象。`Row` 没有 `.get()`，直接下标访问会
        # 抛 AttributeError 而被下面的 except 误记成「报告生成失败」。
        if not isinstance(schedule, dict):
            try:
                schedule = dict(schedule)
            except Exception:
                result["skipped"] += 1
                continue
        schedule_id = int(schedule.get("id") or 0)
        if not schedule_id:
            continue

        if not int(schedule.get("enabled") or 0):
            result["skipped"] += 1
            continue

        reason = due_reason(schedule, moment)
        if reason:
            logger.debug("[报告调度] 跳过 schedule=%d: %s", schedule_id, reason)
            result["skipped"] += 1
            continue

        task_id = int(schedule.get("task_id") or 0)
        try:
            task = _rejection.get_task(task_id)
            if not task:
                raise ValueError(f"废标检查任务 {task_id} 不存在（可能已被清理）")
            report_path = _rejection.export_report(task_id)
        except Exception as exc:
            result["failed"] += 1
            logger.warning("[报告调度] 生成失败 schedule=%d task=%d: %s", schedule_id, task_id, exc)
            try:
                _record_result(schedule_id, moment, error=str(exc))
            except Exception as rec_exc:  # pragma: no cover - 记账失败不掩盖主错误
                logger.warning("[报告调度] 回写失败原因失败 schedule=%d: %s", schedule_id, rec_exc)
            result["details"].append(
                {"schedule_id": schedule_id, "task_id": task_id, "error": str(exc)}
            )
            continue

        _record_result(schedule_id, moment)
        result["generated"] += 1
        result["details"].append(
            {
                "schedule_id": schedule_id,
                "task_id": task_id,
                "report_path": str(report_path),
            }
        )
        logger.info(
            "[报告调度] 已生成报告 schedule=%d task=%d -> %s", schedule_id, task_id, report_path
        )

    if result["failed"]:
        result["status"] = "failed"
    return result
