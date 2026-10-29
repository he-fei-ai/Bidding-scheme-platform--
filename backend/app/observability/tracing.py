"""全链路追踪（ENHANCE 2026-10-29）。

提供 trace_id / span_id / parent_span_id 三级链路标识，贯穿单次请求内的
AI / 工具 / DB / 文件操作，形成可还原的调用树。

核心概念：
- trace_id：一次业务请求的全局唯一 ID（跨进程/线程传递）。
- span_id：一次调用段（span）的唯一 ID。
- parent_span_id：父调用段 ID；为 "-" 表示根 span。
- span()：上下文管理器，进入时派生新 span、退出时自动恢复父 span。
- current()/snapshot()/bind_snapshot()：链路标识快照与跨线程传播。
- span_tree()/span_chain()：调用树 / 时间线还原。
- warn_if_broken()：断链检测。

设计约束：只依赖标准库 + 本包 redact/fields；用 contextvars 保证异步安全。
"""
from __future__ import annotations

import contextvars
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

# ── contextvars 链路标识 ──
trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("obs_trace_id", default="-")
span_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("obs_span_id", default="-")
parent_span_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("obs_parent_span_id", default="-")

#: 运行期 span 存储（按 trace_id -> {span_id: SpanRecord}），供调用树还原。
_SPAN_RECORDS: dict[str, dict[str, "SpanRecord"]] = {}
_MAX_SPANS_PER_TRACE = 5000


def new_trace_id() -> str:
    """生成新的 trace_id（32 位 hex）。"""
    return uuid.uuid4().hex


def new_span_id() -> str:
    """生成新的 span_id（12 位 hex）。"""
    return uuid.uuid4().hex[:12]


def bind_trace(trace_id: str | None = None) -> str:
    """绑定（或生成）trace_id，同时重置 span 上下文（新链路根）。"""
    tid = trace_id or new_trace_id()
    trace_id_var.set(tid)
    span_id_var.set("-")
    parent_span_id_var.set("-")
    return tid


def bind_span(span_id: str | None = None, parent_span_id: str | None = None) -> str:
    """绑定当前 span_id（不改变 trace）。返回实际 span_id。

    同时把该 span 登记到 ``_SPAN_RECORDS``，使 ``span_tree``/``span_chain`` 与
    ``warn_if_broken`` 能覆盖 ``bind_span`` 创建的节点（不再只依赖 ``span()``
    上下文管理器登记），避免断链检测误报或调用树缺节点。
    """
    sid = span_id or new_span_id()
    pid = parent_span_id if parent_span_id is not None else parent_span_id_var.get()
    span_id_var.set(sid)
    parent_span_id_var.set(pid)
    record_span(
        SpanRecord(
            trace_id=trace_id_var.get(),
            span_id=sid,
            parent_span_id=pid,
            module="bind",
            event="bind",
            status="running",
        )
    )
    return sid


def reset() -> None:
    """重置所有链路标识。"""
    trace_id_var.set("-")
    span_id_var.set("-")
    parent_span_id_var.set("-")


def snapshot() -> dict[str, str]:
    """导出链路标识快照（供跨线程/跨进程传递）。"""
    return {
        "trace_id": trace_id_var.get(),
        "span_id": span_id_var.get(),
        "parent_span_id": parent_span_id_var.get(),
    }


def bind_snapshot(ctx: dict | None) -> None:
    """回放链路标识快照；缺失键保持原值。"""
    if not ctx:
        return
    if ctx.get("trace_id") and ctx["trace_id"] != "-":
        trace_id_var.set(ctx["trace_id"])
    if ctx.get("span_id") and ctx["span_id"] != "-":
        span_id_var.set(ctx["span_id"])
    if ctx.get("parent_span_id"):
        parent_span_id_var.set(ctx["parent_span_id"])


def current() -> dict[str, str]:
    """当前链路标识快照。"""
    return snapshot()


@dataclass
class SpanRecord:
    """单条 span 记录（调用树节点）。"""
    trace_id: str
    span_id: str
    parent_span_id: str = "-"
    module: str = "system"
    event: str = "span"
    status: str = "running"
    duration_ms: float = 0.0
    started_at: float = field(default_factory=time.time)
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "module": self.module,
            "event": self.event,
            "status": self.status,
            "duration_ms": round(self.duration_ms, 2),
            "started_at": self.started_at,
            "detail": self.detail,
        }


def _records_for(trace_id: str) -> dict[str, SpanRecord]:
    return _SPAN_RECORDS.setdefault(trace_id, {})


class span:
    """span 上下文管理器：进入派生新 span、退出自动恢复父 span 并记录耗时。

    用法：
        with span("ai.call", module="content", event="content.generate.start") as sp:
            ...
            sp.set_detail(model="gpt-4o", tokens=1024)
    """

    def __init__(
        self,
        name: str = "span",
        *,
        module: str = "system",
        event: str = "span",
        trace_id: str | None = None,
        parent_span_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.module = module
        self.event = event
        self._detail = dict(detail or {})
        self._started = time.perf_counter()
        self._started_at = time.time()
        self._saved: tuple[str, str, str] = ("-", "-", "-")
        self.trace_id = trace_id or trace_id_var.get()
        self.span_id = new_span_id()
        self.parent_span_id = parent_span_id if parent_span_id is not None else span_id_var.get()
        self.status = "running"
        self._rec: SpanRecord | None = None

    def set_detail(self, **kw: Any) -> "span":
        self._detail.update(kw)
        return self

    def __enter__(self) -> "span":
        if self.trace_id == "-":
            self.trace_id = new_trace_id()
            bind_trace(self.trace_id)
        self._saved = (trace_id_var.get(), span_id_var.get(), parent_span_id_var.get())
        trace_id_var.set(self.trace_id)
        span_id_var.set(self.span_id)
        parent_span_id_var.set(self.parent_span_id)
        self._rec = SpanRecord(
            trace_id=self.trace_id, span_id=self.span_id,
            parent_span_id=self.parent_span_id, module=self.module,
            event=self.event, status="running", started_at=self._started_at,
            detail=self._detail,
        )
        record_span(self._rec)
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        duration = (time.perf_counter() - self._started) * 1000
        self.status = "failed" if exc_type else "ok"
        if self._rec is not None:
            self._rec.status = self.status
            self._rec.duration_ms = duration
        trace_id_var.set(self._saved[0])
        span_id_var.set(self._saved[1])
        parent_span_id_var.set(self._saved[2])
        return False

    @property
    def duration_ms(self) -> float:
        return round((time.perf_counter() - self._started) * 1000, 2)


def span_tree(trace_id: str) -> list[dict[str, Any]]:
    """按 trace_id 还原调用树：按 parent 关系分层、按时间排序、标注层级。

    返回树形结构（嵌套 children），根为 parent_span_id == '-' 的 span。
    """
    records = list(_records_for(trace_id).values())
    if not records:
        return []
    children: dict[str, list[SpanRecord]] = {}
    for r in records:
        children.setdefault(r.parent_span_id, []).append(r)
    for lst in children.values():
        lst.sort(key=lambda r: r.started_at)

    def build(pid: str, depth: int, visited: set[str]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for r in children.get(pid, []):
            if r.span_id in visited:
                continue  # 防御：同一 span 不再重复进入，避免环形 parent 导致无限递归
            visited.add(r.span_id)
            node = r.to_dict()
            node["depth"] = depth
            node["children"] = build(r.span_id, depth + 1, visited)
            out.append(node)
        return out

    return build("-", 0, set())


def span_chain(trace_id: str) -> list[dict[str, Any]]:
    """按时间顺序返回扁平 span 列表（含层级 depth），供时间线/表格渲染。"""
    records = list(_records_for(trace_id).values())
    records.sort(key=lambda r: r.started_at)
    depth_map: dict[str, int] = {}

    def depth_of(r: SpanRecord) -> int:
        """计算节点深度（向上追溯 parent），带环检测，避免无限递归。"""
        if r.span_id in depth_map:
            return depth_map[r.span_id]
        path: list[SpanRecord] = []
        cur = r
        base = 0
        while True:
            if cur.span_id in depth_map:
                base = depth_map[cur.span_id]
                break
            if cur.parent_span_id == "-":
                base = 0
                break
            parent = _records_for(trace_id).get(cur.parent_span_id)
            if parent is None or parent.span_id == cur.span_id:
                # 父缺失或自环：深度定为 0
                base = 0
                break
            if any(p.span_id == cur.span_id for p in path):
                # 检测到环：环内节点深度定为 0，断环
                base = 0
                break
            path.append(cur)
            cur = parent
        depth = base
        for node in reversed(path):
            depth += 1
            depth_map[node.span_id] = depth
        depth_map.setdefault(r.span_id, depth)
        return depth_map[r.span_id]

    out: list[dict[str, Any]] = []
    for r in records:
        node = r.to_dict()
        node["depth"] = depth_of(r)
        out.append(node)
    return out



def record_span(rec: SpanRecord) -> None:
    """登记 span 到进程内缓存（供调用树还原）。"""
    records = _records_for(rec.trace_id)
    if len(records) >= _MAX_SPANS_PER_TRACE:
        try:
            records.pop(next(iter(records)), None)
        except StopIteration:
            pass
    records[rec.span_id] = rec


def clear_spans(trace_id: str | None = None) -> None:
    """清除 span 缓存（trace_id 为空则全清）。"""
    if trace_id is None:
        _SPAN_RECORDS.clear()
    else:
        _SPAN_RECORDS.pop(trace_id, None)


def warn_if_broken(trace_id: str | None = None, span_id: str | None = None) -> bool:
    """断链检测：当前 span 的 parent 不为 '-' 且不在本 trace 已记录 span 集合中。

    返回 True 表示父 span 缺失（断链），应记 WARN 级 trace.break 事件。
    """
    tid = trace_id or trace_id_var.get()
    sid = span_id or span_id_var.get()
    pid = parent_span_id_var.get()
    if tid == "-" or sid == "-" or pid == "-":
        return False  # 根 span 不算断链
    return pid not in _records_for(tid)
