from ..db import get_db


def get_usage(project_id: int | None = None) -> dict:
    """查询免费额度使用情况

    返回：
        total_limit: 总额度（默认 10 个项目）
        used_count: 已用额度
        remaining: 剩余额度
        consumed_project_ids: 已消耗项目 ID 列表
        project_consumed: 当前项目是否已消耗（如果传入 project_id）
    """
    with get_db() as conn:
        # 初始化 usage_limits 表（如果不存在）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS usage_limits (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                total_limit INTEGER NOT NULL DEFAULT 10,
                used_count INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS project_usage (
                project_id INTEGER PRIMARY KEY,
                consumed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # 获取或创建 usage_limits 记录
        row = conn.execute("SELECT * FROM usage_limits WHERE id = 1").fetchone()
        if not row:
            conn.execute("INSERT INTO usage_limits (id, total_limit, used_count) VALUES (1, 10, 0)")
            total_limit, used_count = 10, 0
        else:
            total_limit, used_count = row["total_limit"], row["used_count"]

        # 获取已消耗项目 ID 列表
        rows = conn.execute("SELECT project_id FROM project_usage").fetchall()
        consumed_project_ids = [r["project_id"] for r in rows]

        result = {
            "total_limit": total_limit,
            "used_count": used_count,
            "remaining": total_limit - used_count,
            "consumed_project_ids": consumed_project_ids,
        }

        if project_id is not None:
            result["project_consumed"] = project_id in consumed_project_ids

        return result


def ensure_project_generation_allowed(project_id: int) -> bool:
    """确保项目有额度进行正文生成

    逻辑：
    - 如果项目已消耗过额度，允许继续生成
    - 如果项目未消耗且额度已满，拒绝并返回 False
    - 如果项目未消耗且有额度，扣减额度并返回 True

    使用 INSERT OR IGNORE + rowcount 检测：INSERT 成功表示本请求获得了额度，
    INSERT 被忽略表示并发请求已抢先获得额度（不计入本请求）。
    SQLite WAL 模式下同一连接内的 SELECT-INSERT-UPDATE 是原子的。
    """
    with get_db() as conn:
        # 确保表存在（与后续读写在同一连接内，避免跨连接竞态）
        conn.execute(
            "CREATE TABLE IF NOT EXISTS usage_limits "
            "(id INTEGER PRIMARY KEY CHECK (id = 1), total_limit INTEGER NOT NULL DEFAULT 10, used_count INTEGER NOT NULL DEFAULT 0)",
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS project_usage "
            "(project_id INTEGER PRIMARY KEY, consumed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)",
        )

        # 已消耗过则直接放行
        already = conn.execute(
            "SELECT 1 FROM project_usage WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if already:
            return True

        row = conn.execute(
            "SELECT total_limit, used_count FROM usage_limits WHERE id = 1",
        ).fetchone()
        if not row:
            conn.execute("INSERT INTO usage_limits (id, total_limit, used_count) VALUES (1, 10, 0)")
            total_limit, used_count = 10, 0
        else:
            total_limit, used_count = row["total_limit"], row["used_count"]

        if used_count >= total_limit:
            return False

        # 使用 INSERT OR IGNORE 原子性获取额度：
        # - INSERT 成功（rowcount=1）：本请求获得额度，递增计数器
        # - INSERT 被忽略（rowcount=0）：并发请求已抢先获得，本请求也视为已放行
        cursor = conn.execute("INSERT INTO project_usage (project_id) VALUES (?)", (project_id,))
        if cursor.rowcount == 1:
            # 本请求成功获得额度，原子递增计数器
            conn.execute("UPDATE usage_limits SET used_count = used_count + 1 WHERE id = 1")
        # 无论哪种情况，项目已标记为消耗过，返回 True
        return True


def reset_usage():
    """重置用量（用于测试或管理）"""
    with get_db() as conn:
        conn.execute("UPDATE usage_limits SET used_count = 0 WHERE id = 1")
        conn.execute("DELETE FROM project_usage")
