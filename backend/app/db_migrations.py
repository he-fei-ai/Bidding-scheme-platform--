"""数据库迁移脚本

添加增强功能和新模块所需的表和字段。
包括：
1. 图像生成相关：model_configs 扩展列（图像配置）和 image_generation_tasks 表（任务追踪）
2. generation_tasks 表（暂停/恢复机制）
3. section_generation_progress 表（章节级进度）
4. sections 表新增字段
5. api_keys 表（API开放接口）
6. api_key_usage 表（使用统计）
7. ai_usage_stats 表（AI 调用用量追踪）
8. ai_profiles 表（多配置 Profile 系统）
"""

import json
import logging
import os
import sqlite3
from pathlib import Path

from .db import get_db


logger = logging.getLogger(__name__)

# S-1 加密收口覆盖的表与列（表名, 密钥列名）
_API_KEY_STORAGE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("ai_config", "api_key"),
    ("model_configs", "image_api_key_enc"),
    ("text_provider_configs", "api_key"),
    ("image_provider_configs", "api_key"),
    ("ai_profiles", "api_key_enc"),
)


def unify_api_key_encryption(conn, columns: tuple[tuple[str, str], ...] | None = None) -> dict[str, int]:
    """把各配置表中的明文 API Key 统一加密为 ENC: 形式（幂等，可重复执行）。

    - encrypt_api_key 对空串与已是 ENC: 的值幂等透传，重复调用无副作用；
    - 读取侧兼容明文与 ENC: 双形态（decrypt_api_key 对明文透传），
      因此对升级中的历史数据完全向后兼容；
    - columns 参数供测试注入临时表（默认 None = 全量正式表）；
    - 返回 {表名: 转换行数}，便于测试与运维观测。
    """
    from .services.crypto import encrypt_api_key, is_encrypted

    stats: dict[str, int] = {}
    for table, col in (columns or _API_KEY_STORAGE_COLUMNS):
        converted = 0
        # 注意：rowid 必须显式别名 —— INTEGER PRIMARY KEY 表中 rowid 是主键列的
        # 别名，sqlite3.Row 会以主键列名（如 id）回报，按 "rowid" 取值会 IndexError。
        rows = conn.execute(f"SELECT rowid AS _rid, {col} AS k FROM {table}").fetchall()
        for r in rows:
            old_k = r["k"] or ""
            if not old_k or is_encrypted(old_k):
                continue
            conn.execute(
                f"UPDATE {table} SET {col} = ? WHERE rowid = ?",
                (encrypt_api_key(old_k), r["_rid"]),
            )
            converted += 1
        stats[table] = converted
    conn.commit()
    if any(stats.values()):
        logger.info("[OK] 存量明文 API Key 已统一加密为 ENC: 存储: %s", stats)
    return stats


# ========== 2026-10-11 / 2026-10-12：遗留目录标题清理 ==========
#
# 1) 冗余后缀清理：旧版目录去重为消除「不同章节下合法的同名子标题」而追加
#    「（其2）（其3）…」式后缀，既污染了目录（Word 自动目录会把这些后缀渲染成
#    二级条目），又把正文章节整段删掉。生成侧已改为同级去重，这里把存量数据里
#    已经写死进库的后缀清掉。只剥离后缀字符串本身，不改动任何真实内容。
#
# 2) 占位/泛化标题清理（2026-10-12 追加）：加固前（2026-10-10 之前）生成的目录
#    会出现「专项6」「总体实施策划（2）」这类占位/泛化脏标题（见 outline_common
#    里的 _is_dirty_l3_title / _semantic_fallback_title）。这里复用生成侧的同
#    一套清洗逻辑，把存量 JSON 树里的 L3+ 脏标题改写为语义化标题。
#    只作用于 JSON 树（sections.title 单行存储只存 L1/L2，不含 L3 占位）。

#: JSON 目录树形态的表与列：(表名, 需要清洗的列)
_LEGACY_TITLE_JSON_COLUMNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "v51_tasks",
        ("level1_outline_json", "level2_outline_json", "final_outline_json", "edited_outline_json"),
    ),
    ("project_templates", ("outline_json",)),
)

#: 单行标题形态的表与列：(表名, 主键列, 标题列)
_LEGACY_TITLE_TEXT_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("sections", "id", "title"),
    ("feasibility_outline_nodes", "node_id", "title"),
)


def _db_table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """取表的全部列名；表不存在或查询失败时返回空集合。"""
    try:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


def _strip_suffix_tree(obj, strip_fn) -> bool:
    """递归剥离目录树（list/dict 混合）中的冗余后缀，返回内容是否发生变化。"""
    changed = False
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in ("title", "name") and isinstance(value, str):
                new = strip_fn(value)
                if new != value:
                    obj[key] = new
                    changed = True
            elif isinstance(value, (dict, list)):
                if _strip_suffix_tree(value, strip_fn):
                    changed = True
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)) and _strip_suffix_tree(item, strip_fn):
                changed = True
    return changed


def clean_legacy_outline_title_suffixes(conn: sqlite3.Connection, *, commit: bool = True) -> dict[str, int]:
    """清理存量目录标题上的冗余后缀与占位标题（幂等，可重复执行）。

    两个清理阶段（先后缀后占位，确保顺序正确）：
      阶段 1：剥离「（其2）」「（其 2）」式冗余序数后缀（所有标题层级）
      阶段 2：改写 L3+ 的「专项N」「总体实施策划」等占位/泛化脏标题

    覆盖：
      - sections.title / feasibility_outline_nodes.title（单行字符串，仅阶段 1 生效）
      - v51_tasks 的 level1/level2/final/edited outline JSON（两阶段都生效）
      - project_templates.outline_json（两阶段都生效）

    全部按「表/列是否存在」守卫，坏 JSON 静默跳过 —— 清理失败绝不阻塞启动。
    返回 {目标: 受影响的行数}，供启动日志与测试观测。
    """
    try:
        from .services.ai.outline_common import (
            _strip_legacy_redundant_suffix as strip_fn,
            _cleanup_placeholder_titles,
        )
    except Exception as exc:  # 清理失败不能阻塞启动
        logger.warning("[silent-except] 无法加载标题清洗函数，跳过遗留后缀清理: %s", exc)
        return {}

    stats: dict[str, int] = {}

    for table, pk_col, title_col in _LEGACY_TITLE_TEXT_COLUMNS:
        columns = _db_table_columns(conn, table)
        if not columns or pk_col not in columns or title_col not in columns:
            continue
        try:
            rows = conn.execute(
                f"SELECT rowid AS _rid, {title_col} AS t FROM {table}"
            ).fetchall()
        except sqlite3.Error as exc:
            logger.warning("[silent-except] 读取 %s 失败，跳过: %s", table, exc)
            continue
        changed = 0
        for row in rows:
            old = row["t"] or ""
            new = strip_fn(old)
            if new != old:
                conn.execute(
                    f"UPDATE {table} SET {title_col} = ? WHERE rowid = ?",
                    (new, row["_rid"]),
                )
                changed += 1
        stats[table] = changed

    for table, json_cols in _LEGACY_TITLE_JSON_COLUMNS:
        columns = _db_table_columns(conn, table)
        if not columns:
            continue
        targets = [c for c in json_cols if c in columns]
        if not targets:
            continue
        try:
            rows = conn.execute(
                f"SELECT rowid AS _rid, {', '.join(targets)} FROM {table}"
            ).fetchall()
        except sqlite3.Error as exc:
            logger.warning("[silent-except] 读取 %s 失败，跳过: %s", table, exc)
            continue
        changed = 0
        for row in rows:
            payload = []
            dirty = False
            for col in targets:
                raw = row[col] or ""
                if not raw.strip():
                    payload.append(raw)
                    continue
                try:
                    tree = json.loads(raw)
                except (ValueError, TypeError):
                    payload.append(raw)
                    continue
                row_dirty = False
                # 阶段 1：剥离「（其N）」式冗余后缀
                if _strip_suffix_tree(tree, strip_fn):
                    row_dirty = True
                # 阶段 2：清洗「专项N」/「总体实施策划」等 L3+ 占位标题
                # _cleanup_placeholder_titles 要求输入是 list[dict]（一级目录数组），
                # 否则直接跳过；内部已做完整的脏标题判定 + 父级语义派生兜底。
                try:
                    if isinstance(tree, list) and tree and all(
                        isinstance(n, dict) for n in tree
                    ):
                        _ph_fixed = _cleanup_placeholder_titles(tree)
                        if _ph_fixed > 0:
                            row_dirty = True
                except Exception as _ph_exc:
                    # 占位清洗失败不阻塞后缀清洗
                    logger.debug(
                        "[silent-except] 占位标题清洗跳过（继续后缀清理）: %s", _ph_exc
                    )
                if row_dirty:
                    payload.append(json.dumps(tree, ensure_ascii=False))
                    dirty = True
                else:
                    payload.append(raw)
            if dirty:
                conn.execute(
                    f"UPDATE {table} SET {', '.join(c + ' = ?' for c in targets)} WHERE rowid = ?",
                    (*payload, row["_rid"]),
                )
                changed += 1
        stats[table] = changed

    if commit:
        conn.commit()
    if any(stats.values()):
        logger.info(
            "[OK] 已清理存量目录标题：「（其N）」冗余后缀 + L3+ 占位/泛化标题（专项N/总体实施策划）: %s",
            stats,
        )
    else:
        # 2026-10-12 加固：stats 全 0 时也打印 debug 级别日志，
        # 运维能看到清理逻辑确实被执行了（幂等，已清洗过的数据正常返回全 0）
        logger.debug(
            "[OK] clean_legacy_outline_title_suffixes 执行完成，存量数据已清洁：stats=%s",
            stats,
        )
    return stats


def ensure_enhancement_tables():
    """确保所有增强功能和图像模块所需的表和字段存在"""

    with get_db() as conn:
        # ===== 图像生成模块相关（新）=====

        # 确保 model_configs 表存在
        conn.execute("""
            CREATE TABLE IF NOT EXISTS model_configs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                api_key_enc TEXT,
                base_url TEXT,
                config TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
        """)

        # 1. 扩展 model_configs 表以支持图像配置（推荐方案）
        # 按规格文档中的 SQL 语句，逐个添加图像相关的列
        # 辅助函数：安全添加列，兼容 SQLite 多种错误信息格式
        def _add_column_if_not_exists(table: str, column: str, col_def: str):
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")
                logger.info("添加 %s.%s 列", table, column)
            except Exception as e:
                err_msg = str(e).lower()
                if "no such table" in err_msg:
                    # BUG-FIX 2026-10-12：目标表尚未建出（增量迁移场景：只跑了
                    # 子集 DDL 的老库 / 测试夹具）。此时 ALTER 无从谈起，但也不该
                    # 让整条迁移链崩掉——该表由 db.py::init_db 在启动时建出，
                    # 建表语句里已含这些列。跳过即可。
                    logger.warning("表 %s 尚未建出，跳过添加 %s 列", table, column)
                    return
                if "already exists" not in err_msg and "duplicate column" not in err_msg:
                    raise  # 如果不是已存在错误，抛出
                logger.debug("%s.%s 列已存在", table, column)

        _add_column_if_not_exists("model_configs", "image_base_url", "TEXT")
        _add_column_if_not_exists("model_configs", "image_api_key_enc", "TEXT")
        _add_column_if_not_exists("model_configs", "image_model", "TEXT")
        _add_column_if_not_exists("model_configs", "image_provider", "TEXT")
        _add_column_if_not_exists("model_configs", "image_max_concurrency", "INTEGER DEFAULT 2")
        _add_column_if_not_exists("model_configs", "image_max_per_project", "INTEGER DEFAULT 10")
        _add_column_if_not_exists("model_configs", "image_quality", "TEXT DEFAULT 'standard'")
        _add_column_if_not_exists("model_configs", "image_style_injection", "TEXT")
        _add_column_if_not_exists("model_configs", "image_enabled", "BOOLEAN DEFAULT 1")
        _add_column_if_not_exists("model_configs", "image_default_size", "TEXT DEFAULT '2K'")
        _add_column_if_not_exists("model_configs", "image_default_ratio", "TEXT DEFAULT '1:1'")
        _add_column_if_not_exists("model_configs", "image_price_per_image", "REAL DEFAULT 0.0")
        _add_column_if_not_exists("model_configs", "user_id", "INTEGER DEFAULT 1")

        # 2. 创建图像生成任务记录表（用量追踪）
        # 按规格文档中的 CREATE TABLE 语句
        conn.execute("""
            CREATE TABLE IF NOT EXISTS image_generation_tasks (
                id TEXT PRIMARY KEY,
                project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                chapter_id TEXT,                          -- 关联的章节ID
                provider TEXT NOT NULL,                   -- 使用的平台
                model TEXT NOT NULL,                      -- 使用的模型
                prompt TEXT NOT NULL,                     -- 原始提示词
                injected_prompt TEXT,                     -- 注入风格后的完整提示词
                size TEXT,                                -- 输出尺寸
                ratio TEXT,                               -- 宽高比
                status TEXT NOT NULL DEFAULT 'pending',   -- pending | generating | success | failed | cancelled
                image_url TEXT,                           -- 生成的图片 URL
                image_path TEXT,                          -- 本地存储路径
                cost REAL,                                -- 本次调用成本（元）
                retry_count INTEGER DEFAULT 0,
                error_message TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                completed_at TEXT
            )
        """)
        logger.info("创建 image_generation_tasks 表")

        # 3. 为 image_generation_tasks 表创建索引
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_img_tasks_project ON image_generation_tasks(project_id)"
        )
        logger.info("创建 image_generation_tasks.project_id 索引")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_img_tasks_status ON image_generation_tasks(status)"
        )
        logger.info("创建 image_generation_tasks.status 索引")

        # 3.1 兼容早期创建的旧表结构：早期版本只有
        #     id/project_id/section_index/provider/model/prompt/status/image_url/
        #     error_message/cost/created_at/completed_at，缺少下述列会导致
        #     任务记录写入（INSERT 17 列）与任务列表查询（SELECT 新列）失败，
        #     这里对已存在的表逐一 ALTER 补齐（新表已含这些列，幂等跳过）。
        for _img_col, _img_def in (
            ("chapter_id", "TEXT"),
            ("injected_prompt", "TEXT"),
            ("size", "TEXT"),
            ("ratio", "TEXT"),
            ("image_path", "TEXT"),
            ("retry_count", "INTEGER DEFAULT 0"),
        ):
            _add_column_if_not_exists("image_generation_tasks", _img_col, _img_def)

        # ──────────────────────────────────────────────────────────────────
        # repair_mermaid 统计持久化（ENHANCE 2026-10-11 图表增强）
        # 设计：JSON blob 表，每次 flush 时 INSERT 一行完整快照
        # （per_chart_type + type_mismatches + cases + timestamp），
        # 无复杂 schema → 零迁移风险，跨重启保留历史统计趋势。
        # ──────────────────────────────────────────────────────────────────
        conn.execute("""
            CREATE TABLE IF NOT EXISTS repair_stats_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                total_validations INTEGER NOT NULL DEFAULT 0,
                stats_json TEXT NOT NULL DEFAULT '{}',          -- per_chart_type: {type: {step: count}}
                mismatches_json TEXT NOT NULL DEFAULT '{}',    -- {outer: {orig: count}}
                cases_json TEXT NOT NULL DEFAULT '[]',          -- 环形 buffer 快照
                high_freq_json TEXT NOT NULL DEFAULT '[]',      -- _get_high_freq_mismatches 输出
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
        """)
        logger.info("创建 repair_stats_snapshots 表")

        # ===== 原有增强功能相关（保持）=====

        # 4. 生成任务表（暂停/恢复机制）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS generation_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                generation_id TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'running',
                outline_version TEXT,
                started_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                paused_at TEXT,
                resumed_at TEXT,
                completed_at TEXT,
                total_sections INTEGER NOT NULL DEFAULT 0,
                completed_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                cancelled_count INTEGER NOT NULL DEFAULT 0,
                writing_length TEXT NOT NULL DEFAULT 'standard',
                concurrency INTEGER NOT NULL DEFAULT 4,
                error_message TEXT DEFAULT ''
            )
        """)

        # 5. 章节级进度表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS section_generation_progress (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                generation_id TEXT NOT NULL,
                section_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                progress INTEGER NOT NULL DEFAULT 0,
                retry_count INTEGER NOT NULL DEFAULT 0,
                quality_score REAL,
                quality_grade TEXT,
                content_hash TEXT,
                last_error TEXT,
                started_at TEXT,
                completed_at TEXT,
                UNIQUE(generation_id, section_id)
            )
        """)

        # 6. API Key表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL DEFAULT 1,
                name TEXT NOT NULL,
                key_hash TEXT NOT NULL UNIQUE,
                prefix TEXT NOT NULL,
                permissions TEXT NOT NULL DEFAULT '[]',
                rate_limit INTEGER NOT NULL DEFAULT 100,
                usage_count INTEGER NOT NULL DEFAULT 0,
                last_used_at TEXT,
                expires_at TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
        """)

        # 7. API Key使用记录表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS api_key_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key_id INTEGER NOT NULL,
                used_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                ip_address TEXT,
                endpoint TEXT,
                FOREIGN KEY(key_id) REFERENCES api_keys(id) ON DELETE CASCADE
            )
        """)

        # 8. 创建索引
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_gen_task_project ON generation_tasks(project_id)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_gen_task_status ON generation_tasks(status)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sgpg_gen_id ON section_generation_progress(generation_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sgpg_sec_id ON section_generation_progress(section_id)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_api_key_usage_key_time ON api_key_usage(key_id, used_at)"
        )

        # 9. 为 sections 表添加新字段（如果不存在）
        try:
            conn.execute("ALTER TABLE sections ADD COLUMN generation_id TEXT DEFAULT NULL")
            logger.info("添加 sections.generation_id 字段（已存在则忽略）")
        except Exception as _e:
            logger.debug("[silent-except] db_migrations.py: line 217 - %s", _e)

        # 9b. 章节状态自愈：'active' → 'pending'
        # BUG-FIX 2026-10-09（跨模块数据链 / 章节状态机）：目录生成此前向
        # sections.status 硬编码写入 'active'，但真实状态机是
        # pending → generating → completed | failed | cancelled，全仓库无任何
        # 写入方会产生 'active'。后果：
        #   · 前端 collectLeafIds(filterPendingFailed=true)（utils.ts:421）
        #     只认 pending/failed → 大纲生成后尚未写正文的章节在「补全生成」
        #     里永远选不中；
        #   · word_count 路由按 status 分组统计时这些章节落入未知分组。
        # 写入侧已改为 'pending'；此处负责把历史脏数据一次性归位。
        # 向后兼容：仅改写精确等于 'active' 的行，不触碰其他任何取值。
        try:
            _cur = conn.execute(
                "UPDATE sections SET status = 'pending' WHERE status = 'active'"
            )
            if _cur.rowcount and _cur.rowcount > 0:
                logger.info("章节状态自愈：%s 条 'active' 已归位为 'pending'", _cur.rowcount)
        except Exception as _e:
            logger.debug("[silent-except] db_migrations.py: active->pending - %s", _e)

        try:
            conn.execute("ALTER TABLE sections ADD COLUMN progress INTEGER NOT NULL DEFAULT 0")
            logger.info("添加 sections.progress 字段（已存在则忽略）")
        except Exception as _e:
            logger.debug("[silent-except] db_migrations.py: line 223 - %s", _e)

        try:
            conn.execute("ALTER TABLE sections ADD COLUMN quality_score REAL")
            logger.info("添加 sections.quality_score 字段（已存在则忽略）")
        except Exception as _e:
            logger.debug("[silent-except] db_migrations.py: line 229 - %s", _e)

        # 10. 为 projects 表添加 mode 字段（扩写模式标识）
        try:
            conn.execute("ALTER TABLE projects ADD COLUMN mode TEXT NOT NULL DEFAULT 'standard'")
            logger.info("添加 projects.mode 字段（已存在则忽略）")
        except Exception as _e:
            logger.debug("[silent-except] db_migrations.py: line 236 - %s", _e)

        # 11. 创建废标检查报告调度表（用于定时导出报告）
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS rejection_report_schedules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    schedule_type TEXT NOT NULL DEFAULT 'daily',
                    schedule_time TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                    FOREIGN KEY(task_id) REFERENCES rejection_checks(id) ON DELETE CASCADE
                )
            """)
            logger.info("添加 rejection_report_schedules 表")
        except Exception as e:
            logger.error("创建 rejection_report_schedules 表失败: %s", e)

        # 11b. BUG-FIX 2026-09-18（高）：为报告调度表补「执行记录」列。
        # 该表此前**只有 CRUD、没有任何执行器** —— 定时报告只落库、永不生成。
        # 执行器每小时轮询一次，必须知道「上次跑到什么时候」才能避免重复生成，
        # 也需要一个地方记录失败原因，故补这三列。
        for _ddl in (
            "ALTER TABLE rejection_report_schedules ADD COLUMN last_run_at TEXT",
            "ALTER TABLE rejection_report_schedules ADD COLUMN run_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE rejection_report_schedules ADD COLUMN last_error TEXT",
        ):
            try:
                conn.execute(_ddl)
            except Exception as _e:
                # 列已存在（重复迁移）属正常路径，不打 warning 以免刷屏
                logger.debug("[silent-except] db_migrations.py: 列已存在 - %s", _e)

        # 12. 提交事务
        conn.commit()

        logger.info("[OK] 数据库迁移完成（包含图像生成模块支持）")

        # ===== AI 模型配置增强模块 =====

        # 13. AI 调用用量统计表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_usage_stats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                call_id TEXT NOT NULL UNIQUE,
                provider TEXT NOT NULL DEFAULT 'openai_compatible',
                model TEXT NOT NULL,
                base_url TEXT NOT NULL,
                task_type TEXT DEFAULT 'unknown',
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd REAL NOT NULL DEFAULT 0.0,
                response_time_ms INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'success',
                error_message TEXT DEFAULT '',
                profile_name TEXT DEFAULT 'default',
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
        """)
        logger.info("创建 ai_usage_stats 表")
        # 为 ai_usage_stats 创建索引
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_usage_stats_provider ON ai_usage_stats(provider)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_stats_model ON ai_usage_stats(model)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_usage_stats_created ON ai_usage_stats(created_at)"
        )
        # 复合索引：created_at + provider + model，加速 usage_dashboard 的
        # "WHERE created_at >= ? GROUP BY provider, model" 查询，避免全表扫描
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_usage_stats_date_provider_model ON ai_usage_stats(created_at, provider, model)"
        )
        # 复合索引：created_at + task_type，加速 daily_trend 的
        # "WHERE created_at >= ? GROUP BY day, task_type" 查询
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_usage_stats_date_tasktype ON ai_usage_stats(created_at, task_type)"
        )

        # 14. AI Profile 多配置表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL DEFAULT '',
                description TEXT DEFAULT '',
                provider_name TEXT NOT NULL DEFAULT 'agnes',
                provider TEXT NOT NULL DEFAULT 'openai_compatible',
                api_key_enc TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT 'gpt-4o',
                base_url TEXT NOT NULL DEFAULT '',
                max_tokens INTEGER NOT NULL DEFAULT 16384,
                temperature REAL NOT NULL DEFAULT 0.7,
                concurrency INTEGER NOT NULL DEFAULT 4,
                timeout INTEGER NOT NULL DEFAULT 120,
                json_mode INTEGER NOT NULL DEFAULT 0,
                task_type TEXT NOT NULL DEFAULT '',
                rate_limit_per_min INTEGER NOT NULL DEFAULT 0,
                token_budget_daily INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
        """)
        logger.info("创建 ai_profiles 表")

        # 15. 写入内置默认 Profile
        existing = conn.execute("SELECT id FROM ai_profiles WHERE name = 'default'").fetchone()
        if not existing:
            raw_key_row = conn.execute("SELECT api_key FROM ai_config WHERE id = 1").fetchone()
            raw_key = (raw_key_row["api_key"] if raw_key_row else "") or ""
            # API Key 统一明文存储（读取兼容旧 ENC: 数据）
            enc_key = raw_key
            conn.execute(
                """
                INSERT INTO ai_profiles
                    (name, display_name, description, provider_name, provider, api_key_enc,
                     model, base_url, max_tokens, temperature, concurrency, timeout,
                     is_active, is_default)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1)
            """,
                (
                    "default",
                    "默认配置",
                    "系统默认 AI 配置，使用内置 agnes 厂商",
                    "agnes",
                    "openai_compatible",
                    enc_key,
                    "agnes-2.0-flash",
                    "https://api.agnes-ai.cn/v1",
                    400000,
                    0.7,
                    10,
                    120,
                ),
            )
            logger.info("写入默认 AI Profile: default")

        # 16. 为 ai_config 表添加 current_profile_name 列（记录当前激活的 profile）
        try:
            conn.execute(
                "ALTER TABLE ai_config ADD COLUMN current_profile_name TEXT DEFAULT 'default'"
            )
            logger.info("添加 ai_config.current_profile_name 列")
        except Exception as _e:
            logger.debug("[silent-except] db_migrations.py: line 370 - %s", _e)

        # 17. 预置常用 Profile
        preset_profiles = [
            {
                "name": "outline",
                "display_name": "目录生成（精准）",
                "description": "生成投标文件目录时使用：温度低、输出稳定、长上下文",
                "temperature": 0.3,
                "max_tokens": 8192,
                "concurrency": 3,
                "task_type": "outline",
            },
            {
                "name": "content",
                "display_name": "正文撰写（平衡）",
                "description": "正文内容生成：中等温度、平衡创意与准确性",
                "temperature": 0.7,
                "max_tokens": 4096,
                "concurrency": 6,
                "task_type": "content",
            },
            {
                "name": "analysis",
                "display_name": "分析任务（JSON）",
                "description": "招标分析、评分点提取：强制 JSON 输出，温度低",
                "temperature": 0.1,
                "max_tokens": 4096,
                "concurrency": 2,
                "json_mode": 1,
                "task_type": "analysis",
            },
            {
                "name": "quick",
                "display_name": "快速草稿（高速）",
                "description": "快速草稿/预览：低延迟，允许一定容错",
                "temperature": 0.9,
                "max_tokens": 2048,
                "concurrency": 12,
                "task_type": "quick",
            },
        ]
        for p in preset_profiles:
            existing = conn.execute(
                "SELECT id FROM ai_profiles WHERE name = ?", (p["name"],)
            ).fetchone()
            if not existing:
                # 获取主配置的 api_key 和 base_url
                main_config = conn.execute(
                    "SELECT api_key, model, base_url FROM ai_config WHERE id = 1"
                ).fetchone()
                import sys as _sys2

                _sys2.path.insert(0, str(Path(__file__).resolve().parent))
                from app.services.crypto import decrypt_api_key as _dec_key
                from app.services.crypto import is_encrypted as _enc_flag

                _cfg_row = conn.execute("SELECT api_key FROM ai_config WHERE id = 1").fetchone()
                raw_key = (_cfg_row["api_key"] if _cfg_row else "") or ""
                # 统一为明文：存量 ENC: 先解密，再按明文写入
                plain_key = _dec_key(raw_key) if raw_key and _enc_flag(raw_key) else raw_key
                enc_key = plain_key if main_config else ""
                conn.execute(
                    """
                    INSERT INTO ai_profiles
                        (name, display_name, description, provider_name, provider, api_key_enc,
                         model, base_url, max_tokens, temperature, concurrency, timeout,
                         json_mode, task_type, is_active, is_default)
                    VALUES (?, ?, ?, 'agnes', 'openai_compatible', ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0)
                """,
                    (
                        p["name"],
                        p["display_name"],
                        p["description"],
                        enc_key,
                        main_config["model"] if main_config else "agnes-2.0-flash",
                        main_config["base_url"] if main_config else "https://api.agnes-ai.cn/v1",
                        p["max_tokens"],
                        p["temperature"],
                        p["concurrency"],
                        120,
                        p.get("json_mode", 0),
                        p["task_type"],
                    ),
                )
                logger.info("写入预设 Profile: %s", p["name"])

        conn.commit()
        logger.info("[OK] AI 模型配置增强迁移完成（ai_usage_stats + ai_profiles）")

        # 17. generation_tasks 表补充 generation_id 列（兼容旧数据库）
        try:
            conn.execute(
                "ALTER TABLE generation_tasks ADD COLUMN generation_id TEXT NOT NULL DEFAULT ''"
            )
            conn.commit()
            logger.info("[OK] generation_tasks.generation_id 列已添加")
        except Exception as _e:
            logger.debug("[silent-except] db_migrations.py: line 468 - %s", _e)

        # 18. generation_tasks 表补充 started_at 列（兼容旧数据库）
        try:
            conn.execute(
                "ALTER TABLE generation_tasks ADD COLUMN started_at TEXT DEFAULT (datetime('now', 'localtime'))"
            )
            conn.commit()
            logger.info("[OK] generation_tasks.started_at 列已添加")
        except Exception as _e:
            logger.debug("[silent-except] db_migrations.py: line 478 - %s", _e)

        # 19. generation_tasks 表补充 completed_at 列（兼容旧数据库）
        try:
            conn.execute("ALTER TABLE generation_tasks ADD COLUMN completed_at TEXT")
            conn.commit()
            logger.info("[OK] generation_tasks.completed_at 列已添加")
        except Exception as _e:
            logger.debug("[silent-except] db_migrations.py: line 486 - %s", _e)

        # 20. generation_tasks 表补充 paused_at 列（兼容旧数据库）
        try:
            conn.execute("ALTER TABLE generation_tasks ADD COLUMN paused_at TEXT")
            conn.commit()
            logger.info("[OK] generation_tasks.paused_at 列已添加")
        except Exception as _e:
            logger.debug("[silent-except] db_migrations.py: line 494 - %s", _e)

        # 21. generation_tasks 表补充 resumed_at 列（兼容旧数据库）
        try:
            conn.execute("ALTER TABLE generation_tasks ADD COLUMN resumed_at TEXT")
            conn.commit()
            logger.info("[OK] generation_tasks.resumed_at 列已添加")
        except Exception as _e:
            logger.debug("[silent-except] db_migrations.py: line 502 - %s", _e)

        # 22. generation_tasks 表补充 outline_version 列（兼容旧数据库）
        try:
            conn.execute("ALTER TABLE generation_tasks ADD COLUMN outline_version TEXT DEFAULT ''")
            conn.commit()
            logger.info("[OK] generation_tasks.outline_version 列已添加")
        except Exception as _e:
            logger.debug("[silent-except] db_migrations.py: line 510 - %s", _e)

        # 23. generation_tasks 表补充 completed_count/failed_count/cancelled_count 列（兼容旧数据库）
        try:
            conn.execute(
                "ALTER TABLE generation_tasks ADD COLUMN completed_count INTEGER NOT NULL DEFAULT 0"
            )
            conn.execute(
                "ALTER TABLE generation_tasks ADD COLUMN failed_count INTEGER NOT NULL DEFAULT 0"
            )
            conn.execute(
                "ALTER TABLE generation_tasks ADD COLUMN cancelled_count INTEGER NOT NULL DEFAULT 0"
            )
            conn.commit()
            logger.info("[OK] generation_tasks.count 列已添加")
        except Exception as _e:
            logger.debug("[silent-except] db_migrations.py: line 526 - %s", _e)

        # 24. generation_tasks 表补充 writing_length/concurrency 列（兼容旧数据库）
        try:
            conn.execute(
                "ALTER TABLE generation_tasks ADD COLUMN writing_length TEXT NOT NULL DEFAULT 'standard'"
            )
            conn.execute(
                "ALTER TABLE generation_tasks ADD COLUMN concurrency INTEGER NOT NULL DEFAULT 4"
            )
            conn.commit()
            logger.info("[OK] generation_tasks.writing_length/concurrency 列已添加")
        except Exception as _e:
            logger.debug("[silent-except] db_migrations.py: line 539 - %s", _e)

        # 24.1 generation_tasks 补充任务组锁凭据列（多 worker 下跨进程释放锁）
        # 背景：锁凭据原先只存在内存里，一旦「停止生成」的 HTTP 请求落到另一个
        # worker，finalize_task 找不到内存任务就直接 return，锁要等 30 分钟 TTL
        # 才自动回收。落库后任意进程都能按 owner 精确释放（release 内部校验 owner）。
        for _col, _def in [
            ("lock_key", "TEXT NOT NULL DEFAULT ''"),
            ("lock_owner", "TEXT NOT NULL DEFAULT ''"),
        ]:
            try:
                conn.execute(
                    f"ALTER TABLE generation_tasks ADD COLUMN {_col} {_def}"
                )
                conn.commit()
                logger.info("[OK] generation_tasks.%s 列已添加", _col)
            except Exception as _e:
                logger.debug(
                    "[silent-except] db_migrations.py generation_tasks.%s - %s", _col, _e
                )

        # 25. sections 表补充 word_count 列（兼容旧数据库）
        try:
            conn.execute("ALTER TABLE sections ADD COLUMN word_count INTEGER NOT NULL DEFAULT 0")
            conn.commit()
            logger.info("[OK] sections.word_count 列已添加")
        except Exception as _e:
            logger.debug("[silent-except] db_migrations.py: line 547 - %s", _e)

        # 26. sections 表补充 level/section_type/scoring_point_id 列（兼容旧数据库）
        for col_name, col_def in [
            ("level", "INTEGER NOT NULL DEFAULT 1"),
            ("section_type", "TEXT NOT NULL DEFAULT ''"),
            ("scoring_point_id", "INTEGER"),
        ]:
            try:
                conn.execute(f"ALTER TABLE sections ADD COLUMN {col_name} {col_def}")
                conn.commit()
                logger.info("[OK] sections.%s 列已添加", col_name)
            except Exception as _e:
                logger.debug("[silent-except] db_migrations.py: line 560 - %s", _e)

        # 27. generation_tasks 表重建（若检测到旧版结构）
        # 旧版结构包含 strategy/progress_json/completed_sections 旧列以及指向
        # projects 的 FOREIGN KEY，与代码写入的字段（generation_id、outline_version、
        # completed_count 等）不一致，会导致 INSERT 因外键/缺列静默失败，
        # 正文生成暂停/恢复、进度查询等功能失效。
        try:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(generation_tasks)").fetchall()}
            legacy_marker = {"strategy", "progress_json", "completed_sections"} & cols
            if legacy_marker:
                import uuid as _uuid

                logger.warning("检测到旧版 generation_tasks 表结构，重建为标准结构...")
                old_cols = [
                    r[1] for r in conn.execute("PRAGMA table_info(generation_tasks)").fetchall()
                ]
                old_rows = conn.execute("SELECT * FROM generation_tasks").fetchall()
                conn.execute("DROP TABLE generation_tasks")
                conn.execute("""
                    CREATE TABLE generation_tasks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        project_id INTEGER NOT NULL,
                        generation_id TEXT NOT NULL UNIQUE,
                        status TEXT NOT NULL DEFAULT 'running',
                        outline_version TEXT,
                        started_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                        paused_at TEXT,
                        resumed_at TEXT,
                        completed_at TEXT,
                        total_sections INTEGER NOT NULL DEFAULT 0,
                        completed_count INTEGER NOT NULL DEFAULT 0,
                        failed_count INTEGER NOT NULL DEFAULT 0,
                        cancelled_count INTEGER NOT NULL DEFAULT 0,
                        writing_length TEXT NOT NULL DEFAULT 'standard',
                        concurrency INTEGER NOT NULL DEFAULT 4,
                        error_message TEXT DEFAULT ''
                    )
                """)
                for row in old_rows:
                    d = dict(zip(old_cols, row, strict=False))
                    # 旧表以 completed_sections 记录完成数，迁移到新列 completed_count
                    completed = d.get("completed_count")
                    if completed is None:
                        completed = d.get("completed_sections") or 0
                    gen_id = d.get("generation_id") or str(_uuid.uuid4())
                    conn.execute(
                        """INSERT OR IGNORE INTO generation_tasks
                           (project_id, generation_id, status, outline_version,
                            started_at, paused_at, resumed_at, completed_at,
                            total_sections, completed_count, failed_count,
                            cancelled_count, writing_length, concurrency,
                            error_message)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            d.get("project_id", 0),
                            gen_id,
                            d.get("status") or "running",
                            d.get("outline_version") or "",
                            d.get("started_at") or d.get("created_at") or "",
                            d.get("paused_at"),
                            d.get("resumed_at"),
                            d.get("completed_at"),
                            d.get("total_sections") or 0,
                            completed,
                            d.get("failed_count") or 0,
                            d.get("cancelled_count") or 0,
                            d.get("writing_length") or "standard",
                            d.get("concurrency") or 4,
                            d.get("error_message") or "",
                        ),
                    )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_gen_task_project ON generation_tasks(project_id)"
                )
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_gen_task_status ON generation_tasks(status)"
                )
                conn.commit()
                logger.info("[OK] generation_tasks 表已重建为标准结构")
        except Exception as e:
            logger.warning("generation_tasks 表重建失败（不影响核心功能）: %s", e)

        # 28. v51_tasks 表（V5.1 目录生成断点续生持久化）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS v51_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL UNIQUE,
                phase TEXT NOT NULL DEFAULT 'level1',
                current_phase TEXT NOT NULL DEFAULT 'level1',
                level1_outline_json TEXT NOT NULL DEFAULT '[]',
                level2_outline_json TEXT,
                final_outline_json TEXT,
                edited_outline_json TEXT,
                cancelled INTEGER NOT NULL DEFAULT 0,
                summary TEXT,
                scoring_points TEXT,
                industry_profile TEXT,
                tender_context TEXT,
                project_scope TEXT,
                project_info TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                expired_at TEXT
            )
        """)
        conn.commit()
        logger.info("[OK] v51_tasks 表已创建（V5.1 断点续生）")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_v51_project ON v51_tasks(project_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_v51_expired ON v51_tasks(expired_at)")
        conn.commit()
        logger.info("[OK] v51_tasks 索引已创建")

        # 清理过期任务（超过2小时视为过期）
        try:
            conn.execute(
                "UPDATE v51_tasks SET expired_at = created_at WHERE julianday('now') - julianday(created_at) > 2 AND cancelled = 0"
            )
            conn.commit()
            logger.info("[OK] 过期 V5.1 任务已标记")
        except Exception as _e:
            logger.debug("[silent-except] db_migrations.py: line 681 - %s", _e)

        # 29. file_parse_tasks 表（文件解析任务持久化追踪）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS file_parse_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                filename TEXT NOT NULL,
                file_type TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'queued',
                progress INTEGER NOT NULL DEFAULT 0,
                total_files INTEGER NOT NULL DEFAULT 1,
                completed_files INTEGER NOT NULL DEFAULT 0,
                error_message TEXT DEFAULT '',
                started_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                completed_at TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fpt_project ON file_parse_tasks(project_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_fpt_status ON file_parse_tasks(status)")
        conn.commit()
        logger.info("[OK] file_parse_tasks 表已创建（文件解析任务持久化）")

        # 30. text_provider_configs 表（按厂商隔离的文本模型配置）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS text_provider_configs (
                provider_name TEXT PRIMARY KEY,
                provider_type TEXT NOT NULL DEFAULT 'openai_compatible',
                api_key TEXT NOT NULL DEFAULT '',
                base_url TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                max_tokens INTEGER NOT NULL DEFAULT 4096,
                concurrency INTEGER NOT NULL DEFAULT 4,
                temperature REAL NOT NULL DEFAULT 0.7,
                document_profile TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
        """)
        conn.commit()
        logger.info("[OK] text_provider_configs 表已创建（按厂商隔离的文本模型配置）")

        # 从旧 ai_config 迁移数据到 text_provider_configs（如果存在且尚未迁移）
        existing = conn.execute("SELECT COUNT(*) FROM text_provider_configs").fetchone()[0]
        if existing == 0:
            old_row = conn.execute("SELECT * FROM ai_config WHERE id = 1").fetchone()
            if old_row:
                d = dict(old_row)
                pname = d.get("provider_name") or "agnes"
                conn.execute(
                    """
                    INSERT OR IGNORE INTO text_provider_configs
                        (provider_name, provider_type, api_key, base_url, model,
                         max_tokens, concurrency, temperature, document_profile)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    (
                        pname,
                        d.get("provider") or "openai_compatible",
                        d.get("api_key") or "",
                        d.get("base_url") or "",
                        d.get("model") or "",
                        d.get("max_tokens") or 4096,
                        d.get("concurrency") or 4,
                        d.get("temperature") or 0.7,
                        d.get("document_profile") or "",
                    ),
                )
                conn.commit()
                logger.info(
                    "[OK] 已从 ai_config 迁移数据到 text_provider_configs (provider=%s)", pname
                )

        # 31. image_provider_configs 表（按厂商隔离的图像模型配置）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS image_provider_configs (
                provider_name TEXT PRIMARY KEY,
                api_key TEXT NOT NULL DEFAULT '',
                base_url TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                max_concurrency INTEGER NOT NULL DEFAULT 2,
                max_per_project INTEGER NOT NULL DEFAULT 10,
                quality TEXT NOT NULL DEFAULT 'standard',
                style_injection TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 1,
                default_size TEXT NOT NULL DEFAULT '1024x1024',
                default_ratio TEXT NOT NULL DEFAULT '1:1',
                price_per_image REAL NOT NULL DEFAULT 0.0,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
        """)
        conn.commit()
        logger.info("[OK] image_provider_configs 表已创建（按厂商隔离的图像模型配置）")

        # 从旧 model_configs 迁移数据到 image_provider_configs
        existing_img = conn.execute("SELECT COUNT(*) FROM image_provider_configs").fetchone()[0]
        if existing_img == 0:
            old_img_row = conn.execute("""
                SELECT image_provider, image_api_key_enc, image_base_url, image_model,
                       image_max_concurrency, image_max_per_project, image_quality,
                       image_style_injection, image_enabled, image_default_size,
                       image_default_ratio, image_price_per_image
                FROM model_configs WHERE user_id = 1
            """).fetchone()
            if old_img_row and old_img_row[0]:
                # 迁移时把 api_key 统一转为明文（存量数据可能是 ENC: 加密格式）
                img_vals = list(old_img_row)
                try:
                    from app.services.crypto import decrypt_api_key as _dec_img
                    from app.services.crypto import is_encrypted as _is_enc_img

                    k = img_vals[1] or ""
                    if k and _is_enc_img(k):
                        img_vals[1] = _dec_img(k)
                except Exception as _e:
                    logger.debug("[silent-except] db_migrations.py: line 800 - %s", _e)
                conn.execute(
                    """
                    INSERT OR IGNORE INTO image_provider_configs
                        (provider_name, api_key, base_url, model, max_concurrency,
                         max_per_project, quality, style_injection, enabled,
                         default_size, default_ratio, price_per_image)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                    tuple(img_vals),
                )
                conn.commit()
                logger.info(
                    "[OK] 已从 model_configs 迁移数据到 image_provider_configs (provider=%s)",
                    old_img_row[0],
                )

        # 32. 存量 API Key 统一收口为 ENC: 加密存储（幂等，明文→ENC: 单向迁移）
        # S-1 修复（2026-10-06，安全 P0）：旧实现把所有 ENC: 密文**解密回明文**，
        # 与 F-1/L-2（2026-10-04）确立的「密钥加密落库」政策直接冲突 —— 写路径
        # 加密入库，后端每重启一次本迁移就把全部密钥还原为明文，加密承诺形同虚设。
        # 现反转方向：明文 → ENC:（encrypt_api_key 对空串/密文幂等透传）。
        # 兼容性：读取侧（get_ai_config / get_configured_providers / image_engine
        # _resolve_api_key / image service._decrypt_api_key / profile_service）
        # 本就兼容明文与 ENC: 双形态，无 schema 变更、无行为变化。
        try:
            unify_api_key_encryption(conn)
        except Exception as e:
            logger.warning("存量 API Key 加密收口跳过: %s", e)

        # 33. text_provider_configs 强制"当前使用"唯一（最多一个 is_active=1）
        try:
            # 修复脏数据：若多个厂商同时 is_active=1，仅保留 updated_at 最新者
            dup = conn.execute("""
                SELECT provider_name, updated_at FROM text_provider_configs
                WHERE is_active = 1 AND rowid NOT IN (
                    SELECT rowid FROM text_provider_configs
                    WHERE is_active = 1 ORDER BY updated_at DESC LIMIT 1
                )
            """).fetchall()
            for row in dup:
                conn.execute(
                    "UPDATE text_provider_configs SET is_active = 0 WHERE provider_name = ?",
                    [row["provider_name"]],
                )
            if dup:
                logger.info(
                    "[OK] 修复 text_provider_configs 多激活: %s", [r["provider_name"] for r in dup]
                )

            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS idx_tpc_unique_active
                    ON text_provider_configs(is_active)
                    WHERE is_active = 1
            """)
            logger.info("[OK] text_provider_configs 已添加 is_active 唯一索引（最多一个当前使用）")
            conn.commit()
        except Exception as e:
            logger.warning("text_provider_configs 唯一激活迁移跳过: %s", e)

        # 34. users 表（JWT 认证用户存储，幂等）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                salt TEXT NOT NULL,
                role TEXT DEFAULT 'admin',
                is_active INTEGER DEFAULT 1,
                created_at TEXT,
                last_login_at TEXT
            )
        """)
        conn.commit()
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if count == 0:
            import hashlib as _hashlib
            import secrets as _secrets

            try:
                from .config import AUTH_INITIAL_PASSWORD
            except ImportError:
                AUTH_INITIAL_PASSWORD = os.environ.get("AUTH_INITIAL_PASSWORD", "admin123")
            plain_pw = os.environ.get("AUTH_INITIAL_PASSWORD", AUTH_INITIAL_PASSWORD)
            salt_bytes = _secrets.token_bytes(32)
            salt_hex = salt_bytes.hex()
            pw_hash = _hashlib.pbkdf2_hmac("sha256", plain_pw.encode(), salt_bytes, 120000).hex()
            conn.execute(
                "INSERT INTO users (username, password_hash, salt, role, is_active, created_at) VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime'))",
                ("admin", pw_hash, salt_hex, "admin", 1),
            )
            conn.commit()
            logger.warning(
                "users 表为空，已创建默认 admin 用户（密码来自 AUTH_INITIAL_PASSWORD 环境变量，默认 admin123）。"
                "请在首次登录后立即通过 /api/v1/auth/change-password 修改密码。"
            )

        # ===== 文本模型配置模块遗留闭环（2026-10-04）=====

        # 35. ai_config_state：配置版本计数器（跨进程缓存失效，L-3）
        #     读侧 5 秒防抖比对 ai_config_version，变化即丢弃内存缓存重建；
        #     表缺失时读侧静默退化为纯 TTL，不影响旧库。
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_config_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT '',
                updated_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        """)

        # 36. ai_config_audit：配置变更审计日志（L-5）
        #     记录 who/action/target/脱敏字段摘要；密钥仅存后 4 位或 (masked)。
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ai_config_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor TEXT NOT NULL DEFAULT 'unknown',
                action TEXT NOT NULL,
                target TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT '',
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_ai_config_audit_created
            ON ai_config_audit(created_at DESC)
        """)
        conn.commit()

        # 37. 易标字数控制（2026-10-12）：word_control_settings 表 + sections 三列。
        #     DDL 与 db.py::init_db 内的建表语句保持一致（多环境部署时 init_db 与
        #     迁移序列两处都必须建出，见《报告_易标字数控制移植实施与差距审计_
        #     2026-10-12》第 6.5 条）。老库执行本步即可补齐，零破坏：
        #     CREATE TABLE IF NOT EXISTS + 列存在时静默跳过。
        conn.execute("""
            CREATE TABLE IF NOT EXISTS word_control_settings (
                project_id INTEGER PRIMARY KEY,
                minimum_words INTEGER NOT NULL DEFAULT 0,
                maximum_words INTEGER NOT NULL DEFAULT 0,
                section_words INTEGER NOT NULL DEFAULT 0,
                strict_section_words INTEGER NOT NULL DEFAULT 0,
                outline_word_control_snapshot_json TEXT DEFAULT NULL,
                snapshot_at TEXT DEFAULT NULL,
                word_control_warning TEXT DEFAULT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_word_control_settings_project "
            "ON word_control_settings(project_id)"
        )
        for _wc_col, _wc_def in [
            ("readable_word_count", "INTEGER NOT NULL DEFAULT 0"),
            ("word_count_target", "INTEGER NOT NULL DEFAULT 0"),
            ("word_count_deviation", "INTEGER NOT NULL DEFAULT 0"),
        ]:
            _add_column_if_not_exists("sections", _wc_col, _wc_def)
        conn.commit()


if __name__ == "__main__":
    ensure_enhancement_tables()
    print("[OK] 数据库迁移完成")
