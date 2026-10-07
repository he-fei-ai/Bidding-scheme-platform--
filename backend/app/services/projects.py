from datetime import UTC, datetime
import logging
import shutil

from ..config import PROJECTS_DATA_DIR
from ..db import get_db, safe_update
from ..models import Project, ProjectCreate, ProjectUpdate
from ..services.redis_cache import CacheService


logger = logging.getLogger(__name__)


def list_projects() -> list[Project]:
    # 优先从 Redis 缓存读取
    cached = CacheService.get_project_list()
    if cached is not None:
        return [Project(**dict(r)) for r in cached]

    with get_db() as conn:
        rows = conn.execute("SELECT * FROM projects ORDER BY updated_at DESC").fetchall()
        projects = [dict(r) for r in rows]

    # 写入 Redis 缓存（5分钟 TTL，项目列表变化不频繁）
    CacheService.set_project_list(projects)
    return [Project(**r) for r in projects]


def get_project(project_id: int) -> Project | None:
    # 优先从 Redis 缓存读取
    cached = CacheService.get_project_detail(project_id)
    if cached is not None:
        return Project(**cached)

    with get_db() as conn:
        row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()

    if row is None:
        return None
    project = Project(**dict(row))

    # 写入 Redis 缓存（30秒 TTL，项目详情变化较频繁）
    CacheService.set_project_detail(project_id, dict(project))
    return project


def create_project(data: ProjectCreate, created_by: int | None = None) -> Project:
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        cursor = conn.execute(
            """INSERT INTO projects (name, industry, industry_id, bidder_name, notes, created_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (data.name, data.industry, data.industry_id, data.bidder_name, data.notes, created_by, now, now),
        )
        project_id = cursor.lastrowid

        # 在同一个事务内增加行业树节点引用计数（修复跨事务边界问题）
        if data.industry_id:
            try:
                conn.execute(
                    "UPDATE industry_tree SET reference_count = reference_count + 1 WHERE id = ?",
                    (data.industry_id,),
                )
            except Exception:
                # 引用计数更新失败不影响项目创建，记录日志即可
                pass

    project_dir = PROJECTS_DATA_DIR / str(project_id)
    project_dir.mkdir(parents=True, exist_ok=True)
    for sub in ("uploads", "texts", "exports"):
        (project_dir / sub).mkdir(exist_ok=True)
    # 清除项目列表缓存（新项目加入使列表失效）
    CacheService.invalidate_project_list()
    return get_project(project_id)


def update_project(project_id: int, data: ProjectUpdate) -> Project | None:
    existing = get_project(project_id)
    if not existing:
        return None

    updates = data.model_dump(exclude_unset=True)
    if not updates:
        return existing

    # 处理行业树节点引用计数变化（在事务内执行）
    old_industry_id = existing.industry_id
    new_industry_id = updates.get("industry_id", existing.industry_id)

    if new_industry_id and new_industry_id != old_industry_id:
        updates["_adjust_reference"] = {
            "old_id": old_industry_id,
            "new_id": new_industry_id,
        }

    updates["updated_at"] = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        sql, params = safe_update("projects", updates, "id", project_id)
        conn.execute(sql, params)

        # 调整引用计数（在同一事务内）
        if "_adjust_reference" in updates:
            ref_adj = updates["_adjust_reference"]
            if ref_adj["old_id"]:
                try:
                    conn.execute(
                        "UPDATE industry_tree SET reference_count = MAX(0, reference_count - 1) WHERE id = ?",
                        (ref_adj["old_id"],),
                    )
                except Exception as _e:
                    pass
            if ref_adj["new_id"] != ref_adj["old_id"]:
                try:
                    conn.execute(
                        "UPDATE industry_tree SET reference_count = reference_count + 1 WHERE id = ?",
                        (ref_adj["new_id"],),
                    )
                except Exception as _e:
                    pass

    # 清除项目缓存（项目信息已变更）
    CacheService.invalidate_project(project_id)
    return get_project(project_id)


def delete_project(project_id: int) -> bool:
    with get_db() as conn:
        # 先获取项目的行业树节点ID
        row = conn.execute(
            "SELECT industry_id FROM projects WHERE id = ?",
            (project_id,),
        ).fetchone()
        industry_id = row["industry_id"] if row else None

        cursor = conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        if cursor.rowcount == 0:
            return False

        # BUG-FIX 2026-09-18（P2）：file_parse_tasks 无外键约束，项目删除后
        # 解析任务行永久残留（只写不删）。在同一事务内级联清理。
        try:
            conn.execute("DELETE FROM file_parse_tasks WHERE project_id = ?", (project_id,))
        except Exception as _fpt_e:
            logger.warning("清理项目解析任务记录失败 project=%d: %s", project_id, _fpt_e)

        # 在同一个事务内减少行业树节点引用计数（修复跨事务边界问题）
        if industry_id:
            try:
                conn.execute(
                    "UPDATE industry_tree SET reference_count = MAX(0, reference_count - 1) WHERE id = ?",
                    (industry_id,),
                )
            except Exception:
                # 引用计数更新失败不影响项目删除，记录日志即可
                pass

    project_dir = PROJECTS_DATA_DIR / str(project_id)
    if project_dir.exists():
        shutil.rmtree(project_dir)

    CacheService.invalidate_project(project_id)
    CacheService.invalidate_project_list()
    CacheService.invalidate_sections(project_id)
    return True


def update_step(project_id: int, step: int) -> None:
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        conn.execute(
            "UPDATE projects SET current_step = MAX(current_step, ?), updated_at = ? WHERE id = ?",
            (step, now, project_id),
        )
    # BUG-FIX 2026-09-29（缓存未失效，跨模块：文件上传→项目步骤→前端向导）：
    # get_project/list_projects 优先读 Redis 缓存（详情 TTL 30s），而上传路由在
    # update_step 之前就会因存在性校验预热旧步骤缓存，导致步骤推进后前端在
    # TTL 窗口内仍看到旧进度。与同文件其余写操作一致，变更后立即失效两级缓存。
    CacheService.invalidate_project(project_id)
    CacheService.invalidate_project_list()
