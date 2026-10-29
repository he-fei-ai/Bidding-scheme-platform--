"""可观测性子包（ENHANCE 2026-10-29）。

统一导出核心 API，供业务模块与路由使用：

    from app.observability import (
        structured, tracing, trajectory, metrics, alerts, storage, query, fields, redact,
        log_event, log_error, log_ai_call, log_tool_call, log_state_change,
        log_db_write, log_file_op, log_cache, log_retry, log_degrade, log_correction,
        span, bind_trace, new_trace_id, snapshot, bind_snapshot,
        record_state_change, TaskTracker,
        record_metric, record_error, fire_alert,
    )
"""
from __future__ import annotations

from . import alerts, fields, metrics, query, redact, storage, structured, tracing, trajectory
from .structured import (
    log_ai_call, log_cache, log_correction, log_db_write, log_degrade,
    log_error, log_event, log_file_op, log_retry, log_state_change, log_tool_call,
)
from .tracing import bind_snapshot, bind_trace, new_span_id, new_trace_id, snapshot, span
from .trajectory import TaskTracker, record_state_change
from .metrics import record_error, record_metric
from .alerts import fire_alert

__all__ = [
    "alerts", "fields", "metrics", "query", "redact", "storage", "structured",
    "tracing", "trajectory",
    "log_event", "log_error", "log_ai_call", "log_tool_call", "log_state_change",
    "log_db_write", "log_file_op", "log_cache", "log_retry", "log_degrade",
    "log_correction",
    "span", "bind_trace", "new_trace_id", "new_span_id", "snapshot", "bind_snapshot",
    "record_state_change", "TaskTracker", "record_metric", "record_error", "fire_alert",
]
