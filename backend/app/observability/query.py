"""日志查询服务（ENHANCE 2026-10-29）。

`persist_log()`：把关键业务日志写库（obs_structured_log），供查询接口读取。
`query_logs()`：按 trace_id / task_id / project_id / module / level / 时间 /
  关键词 / 错误码组合查询，支持分页 / 排序。
`get_trace_chain()`：按 trace_id 返回完整调用链（按时间排序 + span 层级标注）。
`export_csv()` / `export_json()`：查询结果导出。

写库使用独立短事务（get_db_with_retry），失败宽容（日志绝不能影响业务）。
只依赖标准库 + 本包 fields；DB 访问函数内延迟导入。
"""
from __future__ import annotations

import csv
import io
import json
import time
from typing import Any

from . import fields as _f


def _iso_ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()) + f".{int(time.time() * 1000) % 1000:03d}"


def _to_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def persist_log(rec: dict[str, Any]) -> int | None:
    """写一条关键业务日志到库（宽容失败，返回 id 或 None）。"""
    try:
        from ..db import get_db_with_retry

        detail = rec.get("detail")
        detail_json = json.dumps(detail, ensure_ascii=False, default=repr) if detail is not None else "{}"
        with get_db_with_retry() as conn:
            cur = conn.execute(
                "INSERT INTO obs_structured_log "
                "(timestamp, level, module, event, trace_id, span_id, parent_span_id, "
                " request_id, task_id, project_id, chapter_id, duration_ms, status, "
                " error_code, error_layer, retryable, message, detail_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    rec.get("timestamp", _iso_ts()),
                    str(rec.get("level", "INFO")).upper(),
                    rec.get("module", ""),
                    rec.get("event", ""),
                    rec.get("trace_id", "-"),
                    rec.get("span_id", "-"),
                    rec.get("parent_span_id", "-"),
                    rec.get("request_id", "-"),
                    rec.get("task_id", "-"),
                    rec.get("project_id", "-"),
                    rec.get("chapter_id", "-"),
                    rec.get("duration_ms"),
                    rec.get("status", "-"),
                    rec.get("error_code"),
                    rec.get("error_layer"),
                    _to_int(rec.get("retryable"), 0),
                    rec.get("message", ""),
                    detail_json,
                ),
            )
            return cur.lastrowid
    except Exception:
        return None


def _row_to_dict(row: Any) -> dict[str, Any]:
    d = dict(row)
    dj = d.pop("detail_json", "{}")
    if dj:
        try:
            d["detail"] = json.loads(dj)
        except Exception:
            d["detail"] = dj
    return d

def query_logs(
    *,
    trace_id: str = "",
    task_id: str = "",
    project_id: str = "",
    module: str = "",
    level: str = "",
    event: str = "",
    error_code: str = "",
    keyword: str = "",
    start: str = "",
    end: str = "",
    limit: int = 100,
    offset: int = 0,
    sort: str = "desc",
) -> dict[str, Any]:
    """组合查询结构化日志（分页 / 排序）。"""
    clauses: list[str] = []
    params: list[Any] = []
    if trace_id:
        clauses.append("trace_id = ?")
        params.append(str(trace_id))
    if task_id:
        clauses.append("task_id = ?")
        params.append(str(task_id))
    if project_id:
        clauses.append("project_id = ?")
        params.append(str(project_id))
    if module:
        clauses.append("module = ?")
        params.append(module)
    if level:
        clauses.append("level = ?")
        params.append(str(level).upper())
    if event:
        clauses.append("event = ?")
        params.append(event)
    if error_code:
        clauses.append("error_code = ?")
        params.append(error_code)
    if start:
        clauses.append("timestamp >= ?")
        params.append(start)
    if end:
        clauses.append("timestamp <= ?")
        params.append(end)
    if keyword:
        clauses.append("(message LIKE ? OR detail_json LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%"])
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    order = "ASC" if sort == "asc" else "DESC"
    limit = max(1, min(limit, 1000))
    offset = max(0, offset)
    try:
        from ..db import get_db_with_retry

        with get_db_with_retry(readonly=True) as conn:
            total = conn.execute(
                f"SELECT COUNT(*) AS c FROM obs_structured_log {where}", params
            ).fetchone()["c"]
            rows = conn.execute(
                f"SELECT * FROM obs_structured_log {where} ORDER BY id {order} LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()
        return {"total": total, "items": [_row_to_dict(r) for r in rows]}
    except Exception:
        return {"total": 0, "items": []}


def get_trace_chain(trace_id: str) -> dict[str, Any]:
    """按 trace_id 返回完整调用链（按时间排序 + span 层级标注）。"""
    logs = query_logs(trace_id=trace_id, limit=1000)["items"]
    spans: list[dict[str, Any]] = []
    try:
        from ..db import get_db_with_retry

        with get_db_with_retry(readonly=True) as conn:
            rows = conn.execute(
                "SELECT * FROM obs_span WHERE trace_id = ? ORDER BY id",
                (str(trace_id),),
            ).fetchall()
        for r in rows:
            d = dict(r)
            dj = d.pop("detail_json", "{}")
            if dj:
                try:
                    d["detail"] = json.loads(dj)
                except Exception:
                    d["detail"] = dj
            spans.append(d)
    except Exception:
        pass
    if not spans:
        try:
            from . import tracing as _tr

            spans = _tr.span_chain(trace_id)
        except Exception:
            spans = []
    depth_map: dict[str, int] = {}

    def _depth(s: dict[str, Any]) -> int:
        sid = s.get("span_id")
        if sid in depth_map:
            return depth_map[sid]
        pid = s.get("parent_span_id", "-")
        if not pid or pid == "-":
            depth_map[sid] = 0
            return 0
        parent = next((x for x in spans if x.get("span_id") == pid), None)
        d = _depth(parent) + 1 if parent else 0
        depth_map[sid] = d
        return d

    for s in spans:
        s["depth"] = _depth(s)
    span_ids = {s.get("span_id") for s in spans}
    broken = any(
        s.get("parent_span_id", "-") != "-" and s.get("parent_span_id") not in span_ids
        for s in spans
    )
    return {
        "trace_id": trace_id,
        "logs": logs,
        "spans": spans,
        "broken": broken,
        "chain_count": len(spans),
        "log_count": len(logs),
    }


def export_csv(items: list[dict[str, Any]]) -> str:
    """导出 CSV 文本（含表头）。"""
    fields = ["timestamp", "level", "module", "event", "trace_id", "span_id",
              "parent_span_id", "request_id", "task_id", "project_id", "chapter_id",
              "duration_ms", "status", "error_code", "error_layer", "retryable",
              "message"]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(fields)
    for it in items:
        w.writerow([it.get(f, "") for f in fields])
    return buf.getvalue()


def export_json(items: list[dict[str, Any]]) -> str:
    """导出 JSON 文本。"""
    return json.dumps(items, ensure_ascii=False, default=repr, indent=2)

    return d
