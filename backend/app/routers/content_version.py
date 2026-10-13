"""内容版本管理路由

提供章节内容的版本保存、历史查询、恢复（回滚）功能。
支持"可编辑工作流"：人工编辑后可随时保存快照，回溯到任意历史版本。
"""

from datetime import datetime
import asyncio
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..db import get_db
from ..security.auth import get_current_user
from .sections import require_project_permission
from ..services.sections import invalidate_content_caches
from ..services.task_guard import reject_if_generation_running


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects/{project_id}/content-versions", tags=["content-version"])


class ContentVersionCreate(BaseModel):
    """创建内容版本请求"""

    section_id: int
    content: str = ""
    label: str = ""
    user_id: str = ""


class ContentVersionRestore(BaseModel):
    """恢复内容版本请求"""

    version_id: int


@router.get("")
async def list_content_versions(
    project_id: int,
    section_id: int = Query(..., description="章节ID"),
    limit: int = Query(20, ge=1, le=200),
    current_user: Annotated[Any, Depends(get_current_user)] = None,
):
    """获取指定章节的内容版本历史

    Args:
        project_id: 项目ID
        section_id: 章节ID
        limit: 最多返回的版本数

    Returns:
        list[dict]: 版本列表，按创建时间倒序
    """
    # BUG-FIX 2026-09-18（P1）：原全部端点只登录不验项目归属，任意登录用户
    # 猜到 project_id 即可读/删/回滚他人项目正文版本。现统一接入
    # require_project_permission（与 facts/sections 同口径）。
    if current_user:
        await require_project_permission(project_id, current_user)

    # PERF（2026-10-05）：纯读查询 readonly + to_thread，不在事件循环内抢写锁
    def _read():
        with get_db(readonly=True) as conn:
            return [dict(r) for r in conn.execute(
                """SELECT id, section_id, user_id, label, content, created_at
               FROM content_versions
               WHERE project_id = ? AND section_id = ?
               ORDER BY created_at DESC
               LIMIT ?""",
                (project_id, section_id, limit),
            ).fetchall()]

    rows = await asyncio.to_thread(_read)

    return [
        {
            "id": r["id"],
            "section_id": r["section_id"],
            "user_id": r["user_id"] or "",
            "label": r["label"] or "",
            "content_preview": (r["content"] or "")[:200],
            "content_length": len(r["content"] or ""),
            "created_at": r["created_at"],
        }
        for r in rows
    ]


@router.get("/{version_id}")
async def get_content_version(
    project_id: int,
    version_id: int,
    current_user: Annotated[Any, Depends(get_current_user)] = None,
):
    """获取指定版本号的完整内容"""
    if current_user:
        await require_project_permission(project_id, current_user)

    # PERF（2026-10-05）：纯读 readonly + to_thread，不在事件循环内抢写锁
    def _read_row():
        with get_db(readonly=True) as conn:
            row = conn.execute(
                "SELECT * FROM content_versions WHERE id = ? AND project_id = ?",
                (version_id, project_id),
            ).fetchone()
            return dict(row) if row else None

    row = await asyncio.to_thread(_read_row)

    if not row:
        raise HTTPException(status_code=404, detail="版本不存在")

    return {
        "id": row["id"],
        "section_id": row["section_id"],
        "user_id": row["user_id"] or "",
        "label": row["label"] or "",
        "content": row["content"] or "",
        "created_at": row["created_at"],
    }


@router.post("", status_code=201)
async def save_content_version(
    project_id: int,
    data: ContentVersionCreate,
    current_user: Annotated[Any, Depends(get_current_user)] = None,
):
    """保存当前内容为历史版本

    用于人工定稿过程中的"保存快照"操作。

    Args:
        project_id: 项目ID
        data: 版本创建请求
            - section_id: 章节ID
            - content: 章节内容（完整）
            - label: 版本标签（如"初稿"、"V2修改后"、"定稿"）

    Returns:
        dict: {version_id: int, message: str}
    """
    if current_user:
        await require_project_permission(project_id, current_user)

    # 自动生成标签（如果为空）
    label = data.label.strip()
    if not label:
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        label = f"手动保存 @ {now}"

    # PERF（2026-10-05）：验证读 + 插入写的整段同步 I/O 派发至工作线程，
    # 写锁在 worker 线程阻塞，事件循环不受影响（与 files.py 既有做法一致）
    def _save_version():
        with get_db() as conn:
            section = conn.execute(
                "SELECT id, title FROM sections WHERE id = ? AND project_id = ?",
                (data.section_id, project_id),
            ).fetchone()
            if not section:
                return None
            # 插入版本记录
            cursor = conn.execute(
                """INSERT INTO content_versions
               (project_id, section_id, user_id, label, content, created_at)
               VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime'))""",
                (project_id, data.section_id, data.user_id, label, data.content),
            )
            return cursor.lastrowid

    version_id = await asyncio.to_thread(_save_version)

    if version_id is None:
        raise HTTPException(status_code=404, detail="章节不存在")

    logger.info(
        "保存内容版本: project=%d, section=%d, version=%d, label=%s",
        project_id,
        data.section_id,
        version_id,
        label,
    )

    return {"version_id": version_id, "message": "版本已保存"}


@router.post("/restore")
async def restore_content_version(
    project_id: int,
    data: ContentVersionRestore,
    current_user: Annotated[Any, Depends(get_current_user)] = None,
):
    """恢复指定版本的内容到章节

    将指定版本的 content 写入 sections 表的对应章节，
    同时创建一条新的版本记录用于回溯。

    Args:
        project_id: 项目ID
        data: 恢复请求
            - version_id: 要恢复的版本ID

    Returns:
        dict: {section_id: int, content_preview: str, message: str}
    """
    if current_user:
        await require_project_permission(project_id, current_user)

    # BUG-FIX 2026-10-13（L2 · 旁路写正文无守卫）：回滚是裸 SQL 写
    # sections.content 的旁路链路，目录生成进行中执行会被收尾的
    # DELETE+INSERT 静默覆盖（用户以为回滚成功）。按内容类写入口径防护：
    # 目录生成中拒绝（409），正文生成中放行（与手写编辑同性质）。
    reject_if_generation_running(project_id, allow_content_edit=True)

    # PERF（2026-10-05）：读版本/验章节/写正文/记回滚版本整段同步 I/O
    # 派发至工作线程（写锁在 worker 阻塞，不冻结事件循环）
    def _restore():
        with get_db() as conn:
            version = conn.execute(
                "SELECT * FROM content_versions WHERE id = ? AND project_id = ?",
                (data.version_id, project_id),
            ).fetchone()
            if not version:
                return None
            section_id = version["section_id"]
            content = version["content"] or ""
            section = conn.execute(
                "SELECT id, title FROM sections WHERE id = ? AND project_id = ?",
                (section_id, project_id),
            ).fetchone()
            if not section:
                return {"section_missing": True, "section_id": section_id, "content": content, "version": dict(version)}
            # 更新章节内容
            conn.execute(
                """UPDATE sections
               SET content = ?, updated_at = datetime('now', 'localtime')
               WHERE id = ? AND project_id = ?""",
                (content, section_id, project_id),
            )
            # 创建回滚版本记录（用于回溯"回滚操作"本身）
            rollback_label = f"回滚到版本 #{data.version_id}（{version['label']}）"
            conn.execute(
                """INSERT INTO content_versions
               (project_id, section_id, user_id, label, content, created_at)
               VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime'))""",
                (project_id, section_id, version["user_id"] or "", rollback_label, content),
            )
            return {"section_id": section_id, "content": content, "version": dict(version)}

    result = await asyncio.to_thread(_restore)

    if result is None:
        raise HTTPException(status_code=404, detail="版本不存在")
    if result.get("section_missing"):
        raise HTTPException(status_code=404, detail="章节不存在")

    section_id = result["section_id"]
    content = result["content"]
    version = result["version"]

    # BUG-FIX 2026-09-18（P1）：回滚裸 SQL 写正文，原不失效任何正文缓存 →
    # 目录树 60s 内显示旧 has_content/字数，章节缓存 TTL 内读到回滚前正文。
    # 另 UPDATE 语句补上 project_id 条件，与同文件其余查询口径一致。
    invalidate_content_caches(project_id, [section_id])

    logger.info(
        "恢复内容版本: project=%d, section=%d, version=%d",
        project_id,
        section_id,
        data.version_id,
    )

    return {
        "section_id": section_id,
        "content_preview": content[:200],
        "message": f"已恢复到版本 #{data.version_id}",
    }


@router.delete("/{version_id}")
async def delete_content_version(
    project_id: int,
    version_id: int,
    current_user: Annotated[Any, Depends(get_current_user)] = None,
):
    """删除指定版本"""
    if current_user:
        await require_project_permission(project_id, current_user)

    # PERF（2026-10-05）：删除写操作派发至工作线程，写锁不在 loop 内久持
    def _delete():
        with get_db() as conn:
            return conn.execute(
                "DELETE FROM content_versions WHERE id = ? AND project_id = ?",
                (version_id, project_id),
            ).rowcount

    if await asyncio.to_thread(_delete) == 0:
        raise HTTPException(status_code=404, detail="版本不存在")

    return {"status": "deleted", "message": "版本已删除"}
