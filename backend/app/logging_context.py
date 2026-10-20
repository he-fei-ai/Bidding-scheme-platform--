"""结构化日志上下文（F-P0-1，2026-10-07）。

提供基于 contextvars 的请求级追踪标识，使所有业务日志都能带上
request_id / project_id / section_id / task_id，从而可用 ID 串联全链路、
定位断点与状态不一致（解决"用 ID 串联全链路"的核心诉求）。

用法：
  - 在请求入口（中间件 / FastAPI 依赖）调用 bind_request() / bind_project() 等设置；
  - 在 logging 配置中将 ContextFilter 挂到 Handler，并在 Formatter 中使用
    %(request_id)s %(project_id)s %(section_id)s %(task_id)s。
  - 业务代码中也可临时绑定：
        from .logging_context import bind_section
        bind_section(section_id)   # 该协程任务内后续日志均带 sid
"""
from __future__ import annotations

import contextvars
import logging
import uuid

request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")
project_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("project_id", default="-")
section_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("section_id", default="-")
task_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("task_id", default="-")


def bind_request(rid: str | None = None) -> str:
    """设置（或生成）request_id，返回实际使用的 id。"""
    rid = rid or uuid.uuid4().hex[:12]
    request_id_var.set(rid)
    return rid


def bind_project(pid) -> None:
    if pid is not None:
        project_id_var.set(str(pid))


def bind_section(sid) -> None:
    if sid is not None:
        section_id_var.set(str(sid))


def bind_task(tid) -> None:
    if tid is not None:
        task_id_var.set(str(tid))


def reset() -> None:
    request_id_var.set("-")
    project_id_var.set("-")
    section_id_var.set("-")
    task_id_var.set("-")


def new_trace_id() -> str:
    """生成新的链路追踪 ID（跨进程/跨线程传递时使用）。"""
    return uuid.uuid4().hex[:16]


def snapshot() -> dict[str, str]:
    """导出当前链路标识快照，供跨线程/跨进程传递后回放。"""
    return {
        "request_id": request_id_var.get(),
        "project_id": project_id_var.get(),
        "section_id": section_id_var.get(),
        "task_id": task_id_var.get(),
    }


def bind_snapshot(ctx: dict | None) -> None:
    """回放链路标识快照；`ctx` 为空或缺失键时保持原值不变。"""
    if not ctx:
        return
    _apply = {
        "request_id": bind_request,
        "project_id": bind_project,
        "section_id": bind_section,
        "task_id": bind_task,
    }
    for key, setter in _apply.items():
        value = ctx.get(key)
        if value is not None and value != "-":
            setter(value)


def make_context_bound(fn, *args, **kwargs):
    """在**调用点**捕获当前 contextvars，返回一个可在任意线程执行的零参 callable。

    背景：子线程默认不继承主线程的 contextvars，后台线程内的 ERROR/WARNING
    日志 `rid/pid/sid/tid` 会全为 `-`（基线中占比 77.3%），无法与发起请求
    串联定位。

    ⚠️ 关键：**必须在发起侧（如 `threading.Thread(target=...)` 的实参位置）
    调用本函数**，此时 `copy_context()` 复制的才是发起线程的上下文；若在子
    线程内再调用，复制到的将是子线程自己的空上下文，传播失效。

    用法：
        threading.Thread(target=make_context_bound(worker, arg1), daemon=True).start()
    """
    ctx = contextvars.copy_context()
    return lambda: ctx.run(lambda: fn(*args, **kwargs))


def context_payload() -> dict[str, str]:
    """生成可随 Celery 任务 payload 传递的链路标识字典。"""
    return snapshot()


class ContextFilter(logging.Filter):
    """将当前 contextvars 中的追踪标识注入 LogRecord，供 Formatter 使用。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.project_id = project_id_var.get()
        record.section_id = section_id_var.get()
        record.task_id = task_id_var.get()
        return True
