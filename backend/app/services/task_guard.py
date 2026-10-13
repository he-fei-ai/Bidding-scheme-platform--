"""task_guard — 「AI 生成任务运行中」的共享防护门。

ENHANCE 2026-09-15 起源：技术方案侧的章节编辑端点曾被 SSE 目录生成并发改写
（`_clear_and_save_outline` 收尾的 `DELETE FROM sections` + 全量 INSERT 会把
人工改动静默覆盖），当时在 `routers/sections.py` 内实现了 `_reject_if_generation_running`。

BUG-FIX 2026-10-13（L2 · 旁路写正文链路无守卫）：该防护只覆盖了章节 CRUD 路由，
而同样会**改写 sections.content** 的三条旁路链路一直是裸奔：
  1. 一致性审计自动修复（`routers/consistency.py::apply_autofix`）；
  2. AI 一致性修复（`routers/consistency.py::repair_with_ai`）；
  3. 版本回滚（`routers/content_version.py::restore_content_version`）。
目录生成进行中执行它们，写入会被收尾重建静默丢弃（用户以为修好了，实际被覆盖），
且与 `DELETE+INSERT` 存在理论竞争窗口。现将防护抽为服务层共享函数，三条旁路
链路与章节 CRUD 使用同一口径。

粒度语义（与 2026-09-15 既定口径一致，不扩大拦截面）：
  - **目录生成中**（outline-generation）：一切章节写入都不允许——结束时整表重建，
    任何改动必然丢失；
  - **正文生成中**（其他任务类型）：`allow_content_edit=True` 的**内容类**写入
    （手写编辑 / autofix / 版本回滚）放行——与 `routers/sections.py` 的内容编辑
    路由同性质；结构性改动仍拒绝。
"""

from __future__ import annotations

from fastapi import HTTPException

from .task_lock import TASK_LABELS, get_active_lock, is_lock_live


def live_group_lock(project_id: int) -> dict | None:
    """读取该项目所在技术方案任务组的存活锁（不存在或已过期返回 None）。

    只看行镇锁不看进程内状态：多 worker 部署下 `TaskRegistry` 是进程内单例，
    跨进程看不见彼此的任务；而锁行 sink 到 sqlite，任何 worker 都能读到同一个
    判定依据（`services/task_lock.py` 的文档已说明这条取舍）。
    """
    lock = get_active_lock("outline-generation", project_id=project_id)
    if not lock:
        return None
    if not is_lock_live(lock.get("lock_key", "")):
        # 持有者进程崩溃留下的超时锁，对调用方等价于无锁
        return None
    return lock


def reject_if_generation_running(
    project_id: int, *, allow_content_edit: bool = False
) -> None:
    """生成任务运行中防护（HTTP 409）。

    Args:
        project_id: 项目 ID
        allow_content_edit: True 时正文生成中放行（内容类写入）；目录生成中仍拒绝
    """
    lock = live_group_lock(project_id)
    if lock is None:
        return
    task_type = lock.get("task_type", "")
    if task_type != "outline-generation" and allow_content_edit:
        return
    label = TASK_LABELS.get(task_type, task_type or "AI 生成任务")
    raise HTTPException(
        status_code=409,
        detail=f'当前正在执行「{label}」，目录已锁定，请任务结束后再编辑章节。',
    )
