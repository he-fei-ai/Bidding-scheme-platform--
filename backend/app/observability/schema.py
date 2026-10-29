"""可观测性数据表（ENHANCE 2026-10-29）。

集中管理 obs_structured_log / obs_trajectory / obs_span 三张表的 DDL 与索引，
供 db.init_db 调用（与 task_lock.ensure_schema / task_snapshot.ensure_schema 同模式）。

- obs_structured_log：关键业务结构化日志（可按 trace_id/task_id/project_id/
  module/level/event/error_code/时间查询）。
- obs_trajectory：任务运行轨迹（生命周期状态变化）。
- obs_span：span 调用段（跨进程持久化，供调用树还原）。
"""
from __future__ import annotations

import sqlite3


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS obs_structured_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            level TEXT NOT NULL DEFAULT 'INFO',
            module TEXT NOT NULL DEFAULT '',
            event TEXT NOT NULL DEFAULT '',
            trace_id TEXT NOT NULL DEFAULT '-',
            span_id TEXT NOT NULL DEFAULT '-',
            parent_span_id TEXT NOT NULL DEFAULT '-',
            request_id TEXT NOT NULL DEFAULT '-',
            task_id TEXT NOT NULL DEFAULT '-',
            project_id TEXT NOT NULL DEFAULT '-',
            chapter_id TEXT NOT NULL DEFAULT '-',
            duration_ms REAL,
            status TEXT NOT NULL DEFAULT '-',
            error_code TEXT,
            error_layer TEXT,
            retryable INTEGER,
            message TEXT NOT NULL DEFAULT '',
            detail_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_obs_log_trace ON obs_structured_log(trace_id, id);
        CREATE INDEX IF NOT EXISTS idx_obs_log_task ON obs_structured_log(task_id, id);
        CREATE INDEX IF NOT EXISTS idx_obs_log_project ON obs_structured_log(project_id, id);
        CREATE INDEX IF NOT EXISTS idx_obs_log_event ON obs_structured_log(event, id);
        CREATE INDEX IF NOT EXISTS idx_obs_log_level ON obs_structured_log(level, timestamp);
        CREATE INDEX IF NOT EXISTS idx_obs_log_error ON obs_structured_log(error_code, id);
        CREATE INDEX IF NOT EXISTS idx_obs_log_ts ON obs_structured_log(timestamp);
        """
    )
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS obs_trajectory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL DEFAULT '',
            task_type TEXT NOT NULL DEFAULT '',
            project_id TEXT NOT NULL DEFAULT '',
            from_status TEXT NOT NULL DEFAULT '-',
            to_status TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL DEFAULT '',
            operator TEXT NOT NULL DEFAULT 'system',
            trace_id TEXT NOT NULL DEFAULT '-',
            created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_obs_traj_task ON obs_trajectory(task_id, id);
        CREATE INDEX IF NOT EXISTS idx_obs_traj_status ON obs_trajectory(to_status, id);
        CREATE INDEX IF NOT EXISTS idx_obs_traj_type ON obs_trajectory(task_type, id);
        CREATE INDEX IF NOT EXISTS idx_obs_traj_project ON obs_trajectory(project_id, id);
        """
    )
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS obs_span (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trace_id TEXT NOT NULL DEFAULT '-',
            span_id TEXT NOT NULL DEFAULT '',
            parent_span_id TEXT NOT NULL DEFAULT '-',
            module TEXT NOT NULL DEFAULT '',
            event TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'running',
            duration_ms REAL NOT NULL DEFAULT 0,
            started_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
            detail_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_obs_span_trace ON obs_span(trace_id, id);
        """
    )
