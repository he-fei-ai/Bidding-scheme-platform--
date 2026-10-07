"""数据库批量查询优化服务

提供批量数据库操作优化，减少IO次数，提升查询性能。
支持批量插入、批量更新、批量删除等操作。
"""

from datetime import datetime
import logging

from ..db import get_db


logger = logging.getLogger(__name__)


class BatchQueryOptimizer:
    """批量查询优化器

    提供批量数据库操作，减少IO次数，提升性能。
    """

    @staticmethod
    def batch_get_projects(project_ids: list[int]) -> list[dict]:
        """批量获取项目信息

        Args:
            project_ids: 项目ID列表

        Returns:
            项目信息列表
        """
        if not project_ids:
            return []

        # 构建IN查询
        placeholders = ",".join(["?"] * len(project_ids))
        sql = f"SELECT * FROM projects WHERE id IN ({placeholders}) ORDER BY created_at DESC"

        with get_db() as conn:
            rows = conn.execute(sql, project_ids).fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    def batch_get_sections(section_ids: list[int]) -> list[dict]:
        """批量获取章节信息

        Args:
            section_ids: 章节ID列表

        Returns:
            章节信息列表
        """
        if not section_ids:
            return []

        placeholders = ",".join(["?"] * len(section_ids))
        sql = f"SELECT * FROM sections WHERE id IN ({placeholders})"

        with get_db() as conn:
            rows = conn.execute(sql, section_ids).fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    def batch_update_section_status(project_id: int, section_ids: list[tuple[int, str]]) -> int:
        """批量更新章节状态

        V2.1 优化：使用 executemany 替代逐条 UPDATE，减少 ~N 次 SQL 编译开销。
        """
        if not section_ids:
            return 0

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        params = [(status, now, section_id, project_id) for section_id, status in section_ids]

        with get_db() as conn:
            cursor = conn.executemany(
                """UPDATE sections
                   SET status = ?, updated_at = ?
                   WHERE id = ? AND project_id = ?""",
                params,
            )
            changed = cursor.rowcount
        # BUG-FIX 2026-09-18（P2）：build_tree 缓存节点携带 status，批量改
        # 状态后不失效 → 目录树 60s 内仍显示旧状态（前端生成进度假死）。
        if changed:
            try:
                from .sections import _invalidate_build_tree_cache
                _invalidate_build_tree_cache(project_id)
            except Exception:
                logger.warning("批量更新状态后失效目录树缓存失败（忽略）", exc_info=True)
        return changed

    @staticmethod
    def batch_insert_sections(sections_data: list[dict]) -> list[int]:
        """批量插入章节

        V2.1 优化：使用 executemany 替代逐条 INSERT，减少 ~N 次 SQL 编译开销。
        """
        if not sections_data:
            return []

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        params = [
            (
                d["project_id"],
                d.get("title", ""),
                d.get("content", ""),
                d.get("status", "pending"),
                d.get("order", 0),
                d.get("outline_id"),
                now,
                now,
            )
            for d in sections_data
        ]

        with get_db() as conn:
            cursor = conn.executemany(
                """INSERT INTO sections
                   (project_id, title, content, status, "order", outline_id, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                params,
            )
            # SQLite 的 lastrowid 在 executemany 后是最后一条的 rowid
            # 需要单独查询已插入行的 id
            last_rowid = cursor.lastrowid
            inserted_ids = []
            for i in range(len(params)):
                row = conn.execute(
                    "SELECT id FROM sections WHERE rowid = ?",
                    (last_rowid - len(params) + i + 1,),
                ).fetchone()
                if row:
                    inserted_ids.append(row["id"])
            return inserted_ids

    @staticmethod
    def batch_delete_sections(section_ids: list[int]) -> int:
        """批量删除章节

        Args:
            section_ids: 章节ID列表

        Returns:
            删除的行数
        """
        if not section_ids:
            return 0

        placeholders = ",".join(["?"] * len(section_ids))
        sql = f"DELETE FROM sections WHERE id IN ({placeholders})"

        with get_db() as conn:
            cursor = conn.execute(sql, section_ids)
            return cursor.rowcount

    @staticmethod
    def batch_get_project_files(project_ids: list[int]) -> dict[int, list[dict]]:
        """批量获取多个项目的文件列表

        Args:
            project_ids: 项目ID列表

        Returns:
            {project_id: [file_dict, ...], ...}
        """
        if not project_ids:
            return {}

        placeholders = ",".join(["?"] * len(project_ids))
        sql = f"""
            SELECT project_id, id, filename, file_type, file_role, status, created_at
            FROM uploaded_files 
            WHERE project_id IN ({placeholders})
            ORDER BY project_id, created_at DESC
        """

        with get_db() as conn:
            rows = conn.execute(sql, project_ids).fetchall()

            # 按project_id分组
            result = {}
            for row in rows:
                pid = row["project_id"]
                if pid not in result:
                    result[pid] = []
                result[pid].append(dict(row))

            return result

    @staticmethod
    def batch_update_project_status(project_ids: list[tuple[int, str]]) -> int:
        """批量更新项目状态

        V2.1 优化：使用 executemany 替代逐条 UPDATE。
        """
        if not project_ids:
            return 0

        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        params = [(status, now, pid) for pid, status in project_ids]

        with get_db() as conn:
            cursor = conn.executemany(
                """UPDATE projects
                   SET status = ?, updated_at = ?
                   WHERE id = ?""",
                params,
            )
            return cursor.rowcount

    @staticmethod
    def batch_count_by_status(table: str, status_field: str = "status") -> dict[str, int]:
        """批量统计各状态的数量

        Args:
            table: 表名
            status_field: 状态字段名

        Returns:
            {status: count, ...}
        """
        # 白名单校验防止SQL注入
        allowed_tables = {"projects", "sections", "files"}
        allowed_fields = {"status", "section_type", "project_id"}
        if table not in allowed_tables or status_field not in allowed_fields:
            raise ValueError(f"非法的表名或字段名: {table}.{status_field}")

        sql = f"""
            SELECT {status_field}, COUNT(*) as cnt
            FROM {table}
            GROUP BY {status_field}
        """

        with get_db() as conn:
            rows = conn.execute(sql).fetchall()
            return {row[status_field]: row["cnt"] for row in rows}

    @staticmethod
    def batch_upsert_projects(projects_data: list[dict]) -> list[int]:
        """批量插入或更新项目（UPSERT）

        Args:
            projects_data: [{"id": int, "name": str, ...}, ...]
                          如果包含id则更新，否则插入

        Returns:
            操作的项目ID列表
        """
        operated_ids = []

        with get_db() as conn:
            for data in projects_data:
                project_id = data.get("id")
                now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                if project_id:
                    # 更新现有项目
                    conn.execute(
                        """UPDATE projects 
                           SET name = ?, description = ?, status = ?, updated_at = ?
                           WHERE id = ?""",
                        (
                            data.get("name", ""),
                            data.get("description", ""),
                            data.get("status", "draft"),
                            now,
                            project_id,
                        ),
                    )
                    operated_ids.append(project_id)
                else:
                    # 插入新项目
                    cursor = conn.execute(
                        """INSERT INTO projects 
                           (name, description, status, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?)""",
                        (
                            data.get("name", ""),
                            data.get("description", ""),
                            data.get("status", "draft"),
                            now,
                            now,
                        ),
                    )
                    operated_ids.append(cursor.lastrowid)

        return operated_ids

    @staticmethod
    def get_project_statistics() -> dict:
        """获取项目统计数据（一次性批量查询）

        Returns:
            统计数据结构
        """
        with get_db() as conn:
            # 项目总数
            total = conn.execute("SELECT COUNT(*) as cnt FROM projects").fetchone()["cnt"]

            # 按步骤统计（使用current_step代替status）
            by_step = conn.execute(
                "SELECT current_step, COUNT(*) as cnt FROM projects GROUP BY current_step"
            ).fetchall()
            step_counts = {f"step_{row['current_step']}": row["cnt"] for row in by_step}

            # 文件总数
            file_total = conn.execute("SELECT COUNT(*) as cnt FROM uploaded_files").fetchone()[
                "cnt"
            ]

            # 按状态统计文件
            file_by_status = conn.execute(
                "SELECT status, COUNT(*) as cnt FROM uploaded_files GROUP BY status"
            ).fetchall()
            file_status_counts = {row["status"]: row["cnt"] for row in file_by_status}

            # 章节总数
            section_total = conn.execute("SELECT COUNT(*) as cnt FROM sections").fetchone()["cnt"]

            # 最近7天注册项目数
            seven_days_ago = (datetime.now() - __import__("datetime").timedelta(days=7)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            recent_projects = conn.execute(
                "SELECT COUNT(*) as cnt FROM projects WHERE created_at > ?",
                (seven_days_ago,),
            ).fetchone()["cnt"]

        return {
            "total_projects": total,
            "projects_by_step": step_counts,
            "total_files": file_total,
            "files_by_status": file_status_counts,
            "total_sections": section_total,
            "recent_projects_7days": recent_projects,
        }


# 全局实例
batch_optimizer = BatchQueryOptimizer()
