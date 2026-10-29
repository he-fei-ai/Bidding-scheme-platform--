"""统一结构化日志（ENHANCE 2026-10-29）。

提供：
- `ObsFormatter`：输出统一 JSON 格式，字段见 fields.LOG_FIELDS（18 核心字段）。
- `build_record()`：按规范组装一条日志字典（自动填充 timestamp/level/链路标识/脱敏）。
- `log_event()`：语义化事件主入口（自动路由：写文件 + 关键事件写库）。
- `log_ai_call()` / `log_tool_call()` / `log_state_change()` / `log_db_write()` /
  `log_file_op()` / `log_cache()` / `log_retry()` / `log_degrade()` /
  `log_correction()`：各关键节点便捷埋点。
- `log_error()`：标准化错误（error_code/error_layer/retryable/堆栈摘要/上下文快照）。

设计约束：
- 只依赖标准库 + 本包 fields/redact/tracing；业务模块导入本模块不会反向依赖。
- 存储/轨迹采用**函数内延迟导入**，避免循环导入。
- 所有函数对异常宽容（日志绝不能抛异常影响主流程）。
"""
from __future__ import annotations

import json
import logging
import time
import traceback as _tb
from typing import Any

from . import fields as _f
from . import redact as _redact
from . import tracing as _tr

#: 业务侧可通过 extra= 传入并透传的字段
_PASSTHROUGH = set(_f.LOG_FIELDS)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()) + f".{int(time.time() * 1000) % 1000:03d}"


def _json_default(o: Any) -> str:
    try:
        return repr(o)
    except Exception:
        return "<unserializable>"


class ObsFormatter(logging.Formatter):
    """统一 JSON 格式器：输出 fields.LOG_FIELDS 全部核心字段 + extra 透传。

    兼容既有 `JsonFormatter` 的调用方式：从 LogRecord 读取 contextvars 注入的
    request_id/project_id/task_id 等，并透传 extra 字段（如 step/duration_ms）。
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": _now_iso(),
            "level": record.levelname,
            "module": getattr(record, "module_tag", "system"),
            "event": getattr(record, "event", getattr(record, "step", "log")),
            "trace_id": getattr(record, "trace_id", _tr.trace_id_var.get()),
            "span_id": getattr(record, "span_id", _tr.span_id_var.get()),
            "parent_span_id": getattr(record, "parent_span_id", _tr.parent_span_id_var.get()),
            "request_id": getattr(record, "request_id", "-"),
            "task_id": getattr(record, "task_id", "-"),
            "project_id": getattr(record, "project_id", "-"),
            "chapter_id": getattr(record, "chapter_id", "-"),
            "duration_ms": getattr(record, "duration_ms", None),
            "status": getattr(record, "status", "-"),
            "error_code": getattr(record, "error_code", None),
            "error_layer": getattr(record, "error_layer", None),
            "retryable": getattr(record, "retryable", None),
            "message": record.getMessage(),
        }
        # 透传 extra 字段
        for key, value in record.__dict__.items():
            if key.startswith("_") or key in payload:
                continue
            if key in (
                "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
                "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
                "created", "msecs", "relativeCreated", "thread", "threadName",
                "processName", "process", "getMessage", "message", "taskName",
                "module_tag", "event", "trace_id", "span_id", "parent_span_id",
                "request_id", "task_id", "project_id", "chapter_id",
                "duration_ms", "status", "error_code", "error_layer", "retryable",
                "detail", "stack",
            ):
                continue
            payload[key] = _redact.redact(value)
        # detail 字段（结构化）
        if getattr(record, "detail", None) is not None:
            payload["detail"] = _redact.redact(record.detail)
        if getattr(record, "stack", None):
            payload["stack"] = record.stack
        if record.exc_info:
            payload["stack"] = self.formatException(record.exc_info)
        try:
            return json.dumps(payload, ensure_ascii=False, default=_json_default)
        except Exception:
            return json.dumps(
                {"timestamp": _now_iso(), "level": record.levelname, "message": payload.get("message", "<log-format-error>")},
                ensure_ascii=False,
            )


def build_record(
    level: str,
    event: str,
    module: str = "system",
    message: str = "",
    *,
    status: str | None = None,
    duration_ms: float | None = None,
    error_code: str | None = None,
    error_layer: str | None = None,
    retryable: bool | None = None,
    detail: Any = None,
    **fields: Any,
) -> dict[str, Any]:
    """按规范组装一条日志字典（自动填充时间戳 / 链路标识 / 脱敏）。

    显式传入的 trace_id / span_id / parent_span_id / request_id / task_id /
    project_id / chapter_id 优先于 contextvars（便于测试与跨线程显式指定）。
    """
    t = _tr.current()
    rec: dict[str, Any] = {
        "timestamp": _now_iso(),
        "level": level.upper() if level else "INFO",
        "module": module,
        "event": event,
        "trace_id": fields.pop("trace_id", t["trace_id"]),
        "span_id": fields.pop("span_id", t["span_id"]),
        "parent_span_id": fields.pop("parent_span_id", t["parent_span_id"]),
        "request_id": fields.pop("request_id", "-"),
        "task_id": fields.pop("task_id", "-"),
        "project_id": fields.pop("project_id", "-"),
        "chapter_id": fields.pop("chapter_id", "-"),
        "duration_ms": duration_ms,
        "status": status if status is not None else "-",
        "error_code": error_code,
        "error_layer": error_layer,
        "retryable": retryable,
        "message": message,
        "detail": detail,
    }
    # 其余字段并入 extra（脱敏）
    if fields:
        rec["extra"] = _redact.redact(fields)
    if detail is not None:
        rec["detail"] = _redact.redact(detail)
    return rec


def _route(rec: dict[str, Any]) -> None:
    """路由：写文件（结构化 JSON 行）+ 关键事件写库（DB）+ 指标 + 轨迹关联。

    写库失败 / 存储不可用一律降级不抛错（见 storage/query 的宽容设计）。
    """
    try:
        from . import storage as _storage

        _storage.write(rec)
    except Exception:
        pass
    # 关键业务事件写库（供查询接口），仅当事件在 DB_WRITE_EVENTS 中
    if rec.get("event") in _f.DB_WRITE_EVENTS and rec.get("level") in ("INFO", "WARN", "ERROR", "FATAL"):
        try:
            from . import query as _q

            _q.persist_log(rec)
        except Exception:
            pass


def log_event(
    level: str,
    event: str,
    module: str = "system",
    message: str = "",
    *,
    status: str | None = None,
    duration_ms: float | None = None,
    error_code: str | None = None,
    error_layer: str | None = None,
    retryable: bool | None = None,
    detail: Any = None,
    **fields: Any,
) -> None:
    """语义化事件主入口。

    例：
        log_event("INFO", "parse.start", module="parse", message="开始解析", file=name)
        log_event("ERROR", "content.generate.fail", module="content",
                  error_code="CONTENT_001", error_layer="llm", retryable=True,
                  detail={"sections": 5})
    """
    if not isinstance(level, str) or level.upper() not in _f.LOG_LEVELS:
        level = "INFO"
    rec = build_record(
        level, event, module, message,
        status=status, duration_ms=duration_ms,
        error_code=error_code, error_layer=error_layer,
        retryable=retryable, detail=detail, **fields,
    )
    _route(rec)


# 便捷别名：按级别
def debug(event: str, module: str = "system", message: str = "", **kw: Any) -> None:
    log_event("DEBUG", event, module, message, **kw)


def info(event: str, module: str = "system", message: str = "", **kw: Any) -> None:
    log_event("INFO", event, module, message, **kw)


def warn(event: str, module: str = "system", message: str = "", **kw: Any) -> None:
    log_event("WARN", event, module, message, **kw)


def error(
    event: str,
    module: str = "system",
    message: str = "",
    *,
    error_code: str | None = None,
    error_layer: str | None = None,
    retryable: bool | None = None,
    **kw: Any,
) -> None:
    log_event(
        "ERROR", event, module, message,
        error_code=error_code, error_layer=error_layer, retryable=retryable, **kw,
    )


# ──────────────────────────────────────────────────────────────────
# 关键节点便捷埋点
# ──────────────────────────────────────────────────────────────────
def log_ai_call(
    *,
    model_name: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cached_tokens: int = 0,
    cost: float = 0.0,
    duration_ms: float | None = None,
    streaming: bool = False,
    degraded: bool = False,
    provider: str = "",
    status: str = "ok",
    module: str = "content",
    event: str = "content.generate.start",
    message: str = "",
    **fields: Any,
) -> None:
    """AI 调用明细埋点（model_name/tokens/cost/duration/流式/降级）。"""
    log_event(
        "INFO", event, module, message or f"AI调用 {provider}/{model_name}",
        status=status, duration_ms=duration_ms,
        model_name=model_name, provider=provider,
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        cached_tokens=cached_tokens, cost=cost,
        streaming=streaming, degraded=degraded, **fields,
    )


def log_tool_call(
    *,
    tool_name: str = "",
    param_summary: Any = None,
    result_summary: Any = None,
    duration_ms: float | None = None,
    status: str = "ok",
    module: str = "system",
    event: str = "tool.call",
    message: str = "",
    **fields: Any,
) -> None:
    """工具调用明细埋点（tool_name/参数摘要/结果摘要/duration/成功）。"""
    log_event(
        "INFO", event, module, message or f"工具调用 {tool_name}",
        status=status, duration_ms=duration_ms,
        tool_name=tool_name,
        param_summary=_redact.redact(param_summary) if param_summary is not None else None,
        result_summary=_redact.redact(result_summary) if result_summary is not None else None,
        **fields,
    )


def log_state_change(
    *,
    from_status: str,
    to_status: str,
    reason: str = "",
    operator: str = "system",
    task_id: str = "",
    task_type: str = "",
    module: str = "task",
    message: str = "",
    **fields: Any,
) -> None:
    """状态变化埋点（from_status/to_status/原因/操作者）。"""
    log_event(
        "INFO", "state.change", module, message or f"状态变化 {from_status}->{to_status}",
        status=to_status, task_id=task_id,
        from_status=from_status, to_status=to_status,
        reason=reason, operator=operator, task_type=task_type, **fields,
    )
    # 同时写入运行轨迹
    try:
        from . import trajectory as _traj

        _traj.record_state_change(
            task_id=task_id, task_type=task_type,
            from_status=from_status, to_status=to_status,
            reason=reason, operator=operator, **fields,
        )
    except Exception:
        pass


def log_db_write(
    *,
    table: str = "",
    op: str = "insert",
    rows: int = 0,
    duration_ms: float | None = None,
    status: str = "ok",
    module: str = "db",
    message: str = "",
    **fields: Any,
) -> None:
    """DB 写入埋点。"""
    log_event(
        "INFO", "db.write", module, message or f"DB写入 {table} {op} {rows}行",
        status=status, duration_ms=duration_ms,
        table=table, op=op, rows=rows, **fields,
    )


def log_file_op(
    *,
    path: str = "",
    op: str = "read",
    size: int = 0,
    duration_ms: float | None = None,
    status: str = "ok",
    module: str = "system",
    message: str = "",
    **fields: Any,
) -> None:
    """文件操作埋点。"""
    log_event(
        "INFO", "file.op", module, message or f"文件{op} {path}",
        status=status, duration_ms=duration_ms, path=path, op=op, size=size, **fields,
    )


def log_cache(
    *,
    key: str = "",
    hit: bool,
    module: str = "prompt",
    event: str | None = None,
    message: str = "",
    **fields: Any,
) -> None:
    """缓存命中/失效埋点。"""
    log_event(
        "INFO", event or ("cache.hit" if hit else "cache.miss"), module,
        message or f"缓存{'命中' if hit else '失效'} {key}",
        status="ok", cache_key=key, hit=hit, **fields,
    )


def log_retry(
    *,
    attempt: int,
    max_attempts: int,
    reason: str = "",
    module: str = "system",
    event: str = "retry",
    message: str = "",
    **fields: Any,
) -> None:
    """重试埋点。"""
    log_event(
        "WARN", event, module, message or f"重试 {attempt}/{max_attempts}",
        status="retry", attempt=attempt, max_attempts=max_attempts,
        reason=reason, **fields,
    )


def log_degrade(
    *,
    reason: str = "",
    from_mode: str = "",
    to_mode: str = "",
    module: str = "content",
    message: str = "",
    **fields: Any,
) -> None:
    """降级埋点。"""
    log_event(
        "WARN", "degrade", module, message or f"降级 {from_mode}->{to_mode}: {reason}",
        status="degraded", reason=reason, from_mode=from_mode, to_mode=to_mode, **fields,
    )


def log_correction(
    *,
    phase: str = "start",  # start/end
    method: str = "",
    section_id: str = "",
    old_text: str = "",
    new_text: str = "",
    unique_hit: bool = True,
    module: str = "content",
    message: str = "",
    **fields: Any,
) -> None:
    """修正开始/结束埋点（含 old_text/new_text 摘要与唯一命中）。"""
    evt = "correction.start" if phase == "start" else "correction.end"
    # old/new 只保留摘要，避免全文入日志
    _old = (old_text[:40] + "...") if len(old_text) > 40 else old_text
    _new = (new_text[:40] + "...") if len(new_text) > 40 else new_text
    log_event(
        "INFO", evt, module, message or f"修正{phase} {method}",
        status="ok", method=method, section_id=section_id,
        old_text=_old, new_text=_new, unique_hit=unique_hit, **fields,
    )


def _stack_summary(exc: BaseException | None, limit: int = 8) -> str:
    """堆栈摘要（前 limit 帧），用于错误日志不刷屏。"""
    if exc is None:
        return ""
    try:
        return "".join(_tb.format_exception(type(exc), exc, exc.__traceback__, limit=limit))
    except Exception:
        return repr(exc)


def _context_snapshot(**fields: Any) -> dict[str, Any]:
    """上下文快照：链路标识 + 关键字段摘要（脱敏）。"""
    t = _tr.current()
    snap: dict[str, Any] = {
        "trace_id": t["trace_id"],
        "span_id": t["span_id"],
        "parent_span_id": t["parent_span_id"],
        "request_id": fields.pop("request_id", "-"),
        "task_id": fields.pop("task_id", "-"),
        "project_id": fields.pop("project_id", "-"),
        "chapter_id": fields.pop("chapter_id", "-"),
    }
    # 附加业务字段（截断到 200 字符，防上下文爆炸）
    for k, v in list(fields.items()):
        if k in snap:
            continue
        try:
            s = json.dumps(_redact.redact(v), ensure_ascii=False, default=_json_default)
            snap[k] = s[:200] + ("..." if len(s) > 200 else "")
        except Exception:
            snap[k] = "<unserializable>"
    return snap


def log_error(
    event: str,
    module: str = "system",
    message: str = "",
    *,
    exc: BaseException | None = None,
    error_code: str = "UNKNOWN",
    error_layer: str = "unspecified",
    retryable: bool = False,
    status: str = "failed",
    **fields: Any,
) -> None:
    """标准化错误日志：强制带 error_code/error_layer/retryable + 堆栈摘要 + 上下文快照。

    用法：
        log_error("content.generate.fail", module="content", message="生成失败",
                  exc=e, error_code="CONTENT_001", error_layer="llm", retryable=True,
                  section_id=sec)
    """
    stack = _stack_summary(exc)
    ctx = _context_snapshot(**fields)
    detail: dict[str, Any] = {
        "context": ctx,
        "error_code": error_code,
        "error_layer": error_layer,
        "retryable": retryable,
    }
    if stack:
        detail["stack"] = stack[:4000]
    log_event(
        "ERROR", event, module, message or f"错误 {error_code}",
        status=status, error_code=error_code, error_layer=error_layer,
        retryable=retryable, detail=detail, **fields,
    )


def log_break(trace_id: str | None = None, span_id: str | None = None, message: str = "") -> None:
    """断链 WARN 事件：父 span 缺失、调用链无法还原时记录。"""
    log_event(
        "WARN", "trace.break", module="observability",
        message=message or f"链路断链 trace={trace_id or '-'} span={span_id or '-'}",
        status="broken", trace_id=trace_id, span_id=span_id,
    )
