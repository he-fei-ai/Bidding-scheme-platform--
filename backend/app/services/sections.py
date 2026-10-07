from datetime import UTC, datetime
import logging
import threading

from ..db import get_db, safe_update
from ..models import Section, SectionCreate, SectionTreeNode, SectionUpdate
from ..utils import count_words as _count_words
from ..utils.performance import timer


logger = logging.getLogger(__name__)

# 性能优化：缓存 build_tree 结果（项目ID -> 树结构），TTL=60s
_build_tree_cache: dict[int, tuple[float, list[SectionTreeNode]]] = {}
_BUILD_TREE_CACHE_TTL = 60.0
_BUILD_TREE_CACHE_MAX = 200  # 最多缓存 200 个项目的树结构
_BUILD_TREE_CACHE_LOCK = threading.Lock()


def _invalidate_build_tree_cache(project_id: int) -> None:
    """失效指定项目的 build_tree 缓存"""
    with _BUILD_TREE_CACHE_LOCK:
        _build_tree_cache.pop(project_id, None)


def invalidate_content_caches(project_id: int, section_ids: list[int] | None = None) -> None:
    """旁路直写 sections.content 后的三层正文缓存一键失效。

    BUG-FIX 2026-09-18（P1）：一致性审计自动修复（consistency_auditor.apply_autofix）、
    AI 修复（consistency_repair）、版本回滚（content_version.restore）都用裸 SQL
    改正文但不失效任何一层缓存 → 旧正文会被内存缓存（1h TTL）/Redis（30min）/
    build_tree（60s）回灌，“越修越被覆盖”。这里提供唯一入口，三层逐一失效；
    单层失败不阻断其余层（缓存失效只影响新鲜度，不影响正确性）。

    Args:
        project_id: 项目 ID
        section_ids: 被改动的章节 id 列表；None = 该项目全部
    """
    _invalidate_build_tree_cache(project_id)
    try:
        from .redis_cache import CacheService
        CacheService.invalidate_sections(project_id)
    except Exception as e:
        logger.warning("失效章节 Redis 缓存失败（忽略）: %s", e)
    try:
        from .ai.workflows_sections import _section_content_cache
        _section_content_cache.invalidate_ids_sync(project_id, section_ids)
    except Exception as e:
        logger.warning("失效章节内存正文缓存失败（忽略）: %s", e)


@timer
def list_sections(project_id: int, *, readonly: bool = False) -> list[Section]:
    """列出项目全部章节。

    PERF（2026-10-06）：纯读场景新增 `readonly` 开关（默认 False = 保持既有写锁
    连接口径，**向后兼容**，既有调用方零改动）。注意本函数并非廉价操作：
    它对**每个章节的完整正文**跑 `_count_words`，大项目单次可达数十 MB 级
    文本扫描，必须由调用方放到线程池（见 routers/word_count.py），
    并尽量传 readonly=True 避免与写事务争用全局写锁。
    """
    with get_db(readonly=readonly) as conn:
        rows = conn.execute(
            "SELECT * FROM sections WHERE project_id = ? ORDER BY sort_order, created_at",
            (project_id,),
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["word_count"] = _count_words(d.get("content", "") or "")
            result.append(Section(**d))
        return result


def get_section(section_id: int) -> Section | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM sections WHERE id = ?", (section_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["word_count"] = _count_words(d.get("content", "") or "")
        return Section(**d)


def _strip_title_numbering(title: str) -> str:
    """剥离手工录入标题中的编号前缀（与目录生成落库同一份归一化契约）。

    BUG-FIX 2026-10-04（编号命名空间统一）：目录链路的编号是由树位置派生的
    （前端展示 + DOCX 导出重新生成），AI 落库前会剥离编号前缀，但手工
    创建/改名（PATCH title）不剥：存入「1.1 施工部署」后，界面/导出会变成
    「一、1.1 施工部署」双重编号，正文生成提示词也跟着把旧编号吹进 Markdown
    子标题，与导出重编号冲突。改为入口统一剥前缀；剥离后为空（如纯编号标题
    「第一章」）时保留原标题，向后兼容。
    """
    if not title:
        return title
    try:
        from .ai.workflows_outline_utils import _strip_title_number_prefix
        stripped = _strip_title_number_prefix(title)
        return stripped or title
    except Exception:
        # 归一化失败不阻断写入，退回原行为
        return title


def create_section(project_id: int, data: SectionCreate) -> Section:
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    data = data.model_copy(update={"title": _strip_title_numbering(data.title)})
    with get_db() as conn:
        data = _normalize_section_position(conn, project_id, data)
        section = _insert_section(conn, project_id, data, now)
    # BUG-FIX 2026-09-29（跨模块缓存断点）：结构变更后除 build_tree（60s）外，
    # 还须失效 Redis 整包章节快照（30min）——正文生成入口
    # `_fetch_project_data_async` 优先读它，此前新增/修改章节后短时间内重跑
    # 正文生成，拿到的仍是旧快照（旧章节 id），生成结果写向已不存在的行。
    invalidate_content_caches(project_id)
    return section


def _normalize_section_position(conn, project_id: int, data: SectionCreate) -> SectionCreate:
    """补齐缺省的 level/sort_order（按父子关系与兄弟序自动推导）。

    BUG-FIX 2026-09-29（多级编号不同步/排序错乱）：此前 create_section 直接信任
    客户端传入的 level/sort_order：前端「添加子目录」在二级节点下仍固定传 level=2，
    三级章节入库层级错位，导出编号/字数预算分档全部跟着错；sort_order 缺省 0
    使新章节在 build_tree 排序（sort_order, created_at）中跳到同级最前，界面追加
    在尾部、刷新后却排头部。现改为：
    · level 未传 → 有父则父 level+1，无父则 1；
    · sort_order 未传 → 同父下 MAX+1（追加到尾部）；
    · 显式传值仍原样入库（向后兼容）。
    """
    updates: dict = {}
    if data.level is None:
        if data.parent_id is not None:
            prow = conn.execute(
                "SELECT level FROM sections WHERE id = ? AND project_id = ?",
                (data.parent_id, project_id),
            ).fetchone()
            updates["level"] = (prow["level"] or 1) + 1 if prow else 1
        else:
            updates["level"] = 1
    if data.sort_order is None:
        if data.parent_id is None:
            row = conn.execute(
                "SELECT COALESCE(MAX(sort_order), 0) FROM sections WHERE project_id = ? AND parent_id IS NULL",
                (project_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COALESCE(MAX(sort_order), 0) FROM sections WHERE project_id = ? AND parent_id = ?",
                (project_id, data.parent_id),
            ).fetchone()
        updates["sort_order"] = (row[0] or 0) + 1
    if updates:
        data = data.model_copy(update=updates)
    return data


def _insert_section(conn, project_id: int, data: SectionCreate, now: str) -> Section:
    """使用已有连接插入章节（用于事务内调用）"""
    cursor = conn.execute(
        """INSERT INTO sections
           (project_id, parent_id, title, level, sort_order, section_type,
            scoring_point_id, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            project_id,
            data.parent_id,
            data.title,
            data.level,
            data.sort_order,
            data.section_type,
            data.scoring_point_id,
            now,
            now,
        ),
    )
    section_id = cursor.lastrowid
    # 回读刚插入的记录
    row = conn.execute("SELECT * FROM sections WHERE id = ?", (section_id,)).fetchone()
    if row:
        d = dict(row)
        d["word_count"] = _count_words(d.get("content", "") or "")
        return Section(**d)
    return get_section(section_id)


@timer
def update_section(section_id: int, data: SectionUpdate) -> Section | None:
    updates = data.model_dump(exclude_unset=True)
    if not updates:
        return get_section(section_id)
    # BUG-FIX 2026-10-04：改名同样剥离编号前缀，与 create/目录生成落库同一契约，
    # 避免手工改名重新引入「展示编号 + 标题内嵌编号」双重编号。
    if isinstance(updates.get("title"), str) and updates["title"]:
        updates["title"] = _strip_title_numbering(updates["title"])
    updates["updated_at"] = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        # 在同一连接中执行读-改-写，防止丢失更新
        existing_row = conn.execute("SELECT * FROM sections WHERE id = ?", (section_id,)).fetchone()
        if not existing_row:
            return None
        # BUG-FIX 2026-09-18（P1）：PATCH 可直接改 parent_id 且无任何环检测，
        # 把章节移动到自身子孙下即构造 parent 环 → 正文生成的
        # _get_path_and_ancestors 沿链 while 循环永不死出，整任务卡死/内存膨胀。
        # 这里在写入前同连接校验：新父不得为自身、须存在且同项目、
        # 且不得位于自身的子树内（沿新父向上走，若回到自身即成环）。
        if "parent_id" in updates:
            new_pid = updates["parent_id"]
            if new_pid is not None:
                if new_pid == section_id:
                    raise ValueError("不允许将章节移动到自身之下")
                parent_row = conn.execute(
                    "SELECT id, project_id, parent_id FROM sections WHERE id = ?", (new_pid,)
                ).fetchone()
                if parent_row is None:
                    raise ValueError(f"新父章节 {new_pid} 不存在")
                if existing_row["project_id"] is not None and parent_row["project_id"] != existing_row["project_id"]:
                    raise ValueError("不允许跨项目移动章节")
                cursor = parent_row
                hops = 0
                while cursor is not None and hops < 1000:
                    if cursor["id"] == section_id:
                        raise ValueError("不允许将章节移动到其自身或其子孙节点下（会形成循环引用）")
                    pid = cursor["parent_id"]
                    if pid is None:
                        break
                    cursor = conn.execute(
                        "SELECT id, project_id, parent_id FROM sections WHERE id = ?", (pid,)
                    ).fetchone()
                    hops += 1
        sql, params = safe_update("sections", updates, "id", section_id)
        conn.execute(sql, params)
        # BUG-FIX 2026-10-04（P1·BUG-C）：正文变更时同一条 SQL 同步持久化 word_count。
        # 旧实现只在返回对象里重算字数，DB 列陈旧——一致性审计路径
        # （sse_content.update_section(SectionUpdate(content=...))）与前端编辑保存后，
        # 目录字数列/达标判定与正文实际字数不一致。口径对齐兜底扩写的裸 SQL（已同步）。
        if "content" in updates:
            _wc = _count_words(updates.get("content") or "")
            conn.execute("UPDATE sections SET word_count = ? WHERE id = ?", (_wc, section_id))
        # 回读最新数据
        row = conn.execute("SELECT * FROM sections WHERE id = ?", (section_id,)).fetchone()
        if row:
            d = dict(row)
            d["word_count"] = _count_words(d.get("content", "") or "")
            result = Section(**d)
            # 失效缓存（含 Redis 整包快照，口径同 create_section）
            invalidate_content_caches(result.project_id, [section_id])
            return result
        return None


def delete_section(section_id: int) -> dict:
    """删除章节前先检查是否有子章节，防止意外删除有子节点的数据

    Args:
        section_id: 章节ID

    Returns:
        dict: {
            "success": bool,        # 删除成功为True
            "children_count": int,  # 子章节数量（删除成功时为0）
            "message": str          # 提示信息
        }
    """
    with get_db() as conn:
        # 合并查询：一次获取 children_count 和 project_id，减少 DB 往返
        row = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM sections WHERE parent_id = ?) AS children_count,
                (SELECT project_id FROM sections WHERE id = ?) AS project_id
            """,
            (section_id, section_id),
        ).fetchone()
        children_count = row["children_count"] if row else 0
        project_id = row["project_id"] if row else None

        if children_count > 0:
            logger.warning(
                "尝试删除含有子章节的 section_id=%d，包含 %d 个子章节，操作被阻止",
                section_id,
                children_count,
            )
            return {
                "success": False,
                "children_count": children_count,
                "message": f"章节包含 {children_count} 个子章节，请先删除子章节后再操作",
            }

        cursor = conn.execute("DELETE FROM sections WHERE id = ?", (section_id,))
        deleted = cursor.rowcount > 0
        if deleted:
            if project_id:
                invalidate_content_caches(project_id, [section_id])
            logger.info("成功删除章节 section_id=%d", section_id)
            return {
                "success": True,
                "children_count": 0,
                "message": "章节已删除",
            }
        else:
            logger.warning("删除章节失败，section_id=%d 不存在", section_id)
            return {
                "success": False,
                "children_count": 0,
                "message": "章节不存在",
            }


@timer
def delete_section_tree(section_id: int) -> dict:
    with get_db() as conn:
        # 先获取父项目ID
        sec = conn.execute("SELECT project_id FROM sections WHERE id = ?", (section_id,)).fetchone()
        project_id = sec["project_id"] if sec else None
        deleted = _delete_tree(conn, section_id)
    if project_id:
        invalidate_content_caches(project_id)
    return {"status": "success", "deleted_count": deleted}


def _delete_tree(conn, section_id: int) -> int:
    """使用递归 CTE 一次性删除整棵子树（替代 N+1 循环 DELETE）。

    SQLite 的 cursor.rowcount 对 CTE DELETE 返回 -1，
    因此先计数再删除。
    """
    # 第一步：统计待删除节点数
    count_row = conn.execute(
        """
        WITH RECURSIVE descendant_ids(id) AS (
            SELECT id FROM sections WHERE id = ?
            UNION ALL
            SELECT s.id FROM sections s
            INNER JOIN descendant_ids d ON s.parent_id = d.id
        )
        SELECT COUNT(*) FROM descendant_ids;
        """,
        (section_id,),
    ).fetchone()
    count = count_row[0] if count_row else 0

    if count == 0:
        return 0

    # 第二步：执行 CTE DELETE
    conn.execute(
        """
        WITH RECURSIVE descendant_ids(id) AS (
            SELECT id FROM sections WHERE id = ?
            UNION ALL
            SELECT s.id FROM sections s
            INNER JOIN descendant_ids d ON s.parent_id = d.id
        )
        DELETE FROM sections WHERE id IN (SELECT id FROM descendant_ids);
        """,
        (section_id,),
    )
    return count


# PERF-B-W5（2026-10-05）：树构建轻量投影 —— 上一轮 B-W5 只建好未接线。
# 不拉 content / ai_prompt_snapshot 大列：
#  - has_content 用 length(content) 在 SQL 侧判定；
#  - chart_flags 只需 4 个 JSON 列的「非空」真值（COALESCE 兜住旧库无列）；
#  - word_count 用已持久化列（update_section 写入路径维护），仅对「列为 0 且
#    正文非空」的存量脏行回读正文重算，并在同一次只读会话里惰性回填自愈
#    （口径同 2026-10-04 BUG-C），下次重建直接命中干净列。
_TREE_LIGHT_COLUMNS = """
    id, project_id, parent_id, title, level, sort_order, section_type,
    scoring_point_id, target_words, status, created_at,
    COALESCE(word_count, 0) AS word_count,
    CASE WHEN length(content) > 0 THEN 1 ELSE 0 END AS has_content,
    COALESCE(length(flowchart_json), 0) > 0 AS has_flowchart,
    COALESCE(length(gantt_json), 0) > 0 AS has_gantt,
    COALESCE(length(architecture_json), 0) > 0 AS has_architecture,
    COALESCE(length(labor_json), 0) > 0 AS has_labor
"""


@timer
def build_tree(project_id: int) -> list[SectionTreeNode]:
    """构建章节目录树（带缓存，TTL=60s）"""
    import time

    now = time.time()
    with _BUILD_TREE_CACHE_LOCK:
        cached = _build_tree_cache.get(project_id)
        if cached and (now - cached[0]) < _BUILD_TREE_CACHE_TTL:
            return cached[1]

    wc_map: dict[int, int] = {}
    with get_db(readonly=True) as conn:
        rows = [dict(r) for r in conn.execute(
            f"SELECT {_TREE_LIGHT_COLUMNS} FROM sections WHERE project_id = ? ORDER BY sort_order, created_at",
            (project_id,),
        ).fetchall()]
        # 惰性自愈：脏行（persisted=0 但正文非空）单独回读正文重算；
        # 回填另开写会话走全局写锁，readonly 会话内绝不可写
        dirty_ids = [r["id"] for r in rows if r["word_count"] == 0 and r["has_content"]]
        if dirty_ids:
            placeholders = ",".join("?" * len(dirty_ids))
            content_rows = conn.execute(
                f"SELECT id, content FROM sections WHERE id IN ({placeholders})",
                dirty_ids,
            ).fetchall()
            wc_map = {cr["id"]: _count_words(cr["content"] or "") for cr in content_rows}

    if wc_map:
        try:
            with get_db() as wconn:
                wconn.executemany(
                    "UPDATE sections SET word_count = ? WHERE id = ?",
                    [(wc, sid) for sid, wc in wc_map.items()],
                )
        except Exception as e:
            # 回填失败不影响树正确性（本次已重算），下次重建重试
            logger.warning("word_count 惰性回填失败（忽略）: %s", e)

    by_id: dict[int, SectionTreeNode] = {}
    roots: list[SectionTreeNode] = []

    for r in rows:
        flags: list[str] = []
        if r["has_flowchart"]:
            flags.append("flowchart")
        if r["has_gantt"]:
            flags.append("gantt")
        if r["has_architecture"]:
            flags.append("architecture")
        if r["has_labor"]:
            flags.append("labor")
        node = SectionTreeNode(
            id=r["id"],
            title=r["title"],
            level=r["level"],
            sort_order=r["sort_order"],
            section_type=r["section_type"],
            scoring_point_id=r["scoring_point_id"],
            target_words=r["target_words"],
            status=r["status"],
            created_at=r["created_at"],
            has_content=bool(r["has_content"]),
            word_count=wc_map.get(r["id"], r["word_count"]),
            chart_flags=flags,
        )
        by_id[r["id"]] = node

    for r in rows:
        node = by_id[r["id"]]
        if r["parent_id"] and r["parent_id"] in by_id:
            by_id[r["parent_id"]].children.append(node)
        else:
            roots.append(node)

    for node in by_id.values():
        node.children.sort(key=lambda x: (x.sort_order, x.created_at))

    roots.sort(key=lambda x: (x.sort_order, x.created_at))

    # 写入缓存（带锁保护 + 大小限制）
    with _BUILD_TREE_CACHE_LOCK:
        if len(_build_tree_cache) >= _BUILD_TREE_CACHE_MAX:
            # 淘汰最旧的一半
            sorted_items = sorted(_build_tree_cache.items(), key=lambda x: x[1][0])
            for key, _ in sorted_items[: len(sorted_items) // 2]:
                _build_tree_cache.pop(key, None)
        _build_tree_cache[project_id] = (now, roots)
    return roots


def batch_create_sections(project_id: int, items: list[dict]) -> list[Section]:
    """批量创建章节（单事务操作）"""
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    created = []
    with get_db() as conn:
        try:
            for item in items:
                data = SectionCreate(
                    **{
                        k: v
                        for k, v in item.items()
                        if k
                        in (
                            "parent_id",
                            "title",
                            "level",
                            "sort_order",
                            "section_type",
                            "scoring_point_id",
                        )
                    },
                )
                # BUG-FIX 2026-10-04：批量创建与单条创建同一归一化口径
                data = data.model_copy(update={"title": _strip_title_numbering(data.title)})
                # BUG-FIX 2026-09-29：批量创建同样走归一化，缺省 level/sort_order
                # 按父子关系/兄弟序补齐，与单条创建保持同一份写入契约。
                data = _normalize_section_position(conn, project_id, data)
                section = _insert_section(conn, project_id, data, now)
                created.append(section)
        except Exception as e:
            logger.error("批量创建章节失败: %s", e)
            conn.rollback()
            raise
    # BUG-FIX 2026-09-29：批量创建同样须失效 Redis 整包快照（口径同 create_section）
    invalidate_content_caches(project_id)
    return created


def clear_project_sections(project_id: int):
    """清空项目的所有章节"""
    with get_db() as conn:
        conn.execute("DELETE FROM sections WHERE project_id = ?", (project_id,))
    # BUG 修复：清空后失效全部三层缓存，否则前端 loadTree 会在 TTL 内返回旧的过期树，
    # 且正文生成会读到旧章节快照（口径同 create_section）
    invalidate_content_caches(project_id)


def move_section(section_id: int, direction: str) -> bool:
    """移动章节排序（up 上移 / down 下移），返回是否成功移动

    移动后重建所有兄弟节点的 sort_order 为连续递增（从 1 开始），确保排序无间隙。
    """
    section = get_section(section_id)
    if not section:
        return False

    with get_db() as conn:
        # 修复：parent_id IS ? 对非 NULL 参数在 SQLite 中不兼容
        # 分开处理 NULL 和非 NULL 情况
        if section.parent_id is None:
            rows = conn.execute(
                "SELECT id, sort_order FROM sections WHERE project_id = ? AND parent_id IS NULL ORDER BY sort_order ASC NULLS LAST, id",
                (section.project_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, sort_order FROM sections WHERE project_id = ? AND parent_id = ? ORDER BY sort_order ASC NULLS LAST, id",
                (section.project_id, section.parent_id),
            ).fetchall()
        siblings = [dict(r) for r in rows]

        idx = next((i for i, s in enumerate(siblings) if s["id"] == section_id), -1)
        if idx < 0:
            logger.error(
                "移动失败：section_id=%d 未在兄弟节点中找到，parent_id=%s，共 %d 个兄弟",
                section_id,
                section.parent_id,
                len(siblings),
            )
            return False

        # 在列表中交换位置，然后统一重建 sort_order
        if direction == "up" and idx > 0:
            siblings[idx], siblings[idx - 1] = siblings[idx - 1], siblings[idx]
        elif direction == "down" and idx < len(siblings) - 1:
            siblings[idx], siblings[idx + 1] = siblings[idx + 1], siblings[idx]
        else:
            logger.warning(
                "移动失败：section_id=%d direction=%s idx=%d 边界位置", section_id, direction, idx
            )
            return False

        try:
            # 重建所有兄弟节点的 sort_order 为连续递增（从 1 开始）
            # PERF（2026-09-16）：逐行 UPDATE → executemany 批量执行，
            # 减少 N 次 SQL 编译/步进开销
            conn.executemany(
                "UPDATE sections SET sort_order = ? WHERE id = ?",
                [(new_order, sib["id"]) for new_order, sib in enumerate(siblings, start=1)],
            )
            logger.info(
                "移动章节 section_id=%d %s 成功，已重建 %d 个兄弟节点的 sort_order",
                section_id,
                direction,
                len(siblings),
            )
            # BUG 修复：移动后失效全部章节缓存，避免返回过期排序数据
            invalidate_content_caches(section.project_id)
            return True
        except Exception as e:
            logger.error("移动章节失败 id=%d: %s", section_id, e)
            conn.rollback()
            return False


def get_next_sort_order(project_id: int, parent_id: int | None) -> int:
    """获取指定父节点下的下一个可用的 sort_order"""
    with get_db() as conn:
        if parent_id is None:
            row = conn.execute(
                "SELECT COALESCE(MAX(sort_order), 0) FROM sections WHERE project_id = ? AND parent_id IS NULL",
                (project_id,),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT COALESCE(MAX(sort_order), 0) FROM sections WHERE project_id = ? AND parent_id = ?",
                (project_id, parent_id),
            ).fetchone()
        return (row[0] or 0) + 1


@timer
def reorder_sections(project_id: int, groups: list[dict]) -> list[SectionTreeNode]:
    """批量重排章节顺序

    Args:
        project_id: 项目 ID
        groups: 排序分组列表，每项含 parent_id 和 ordered_ids（按顺序的章节 ID 数组）
                [{"parent_id": null, "ordered_ids": [3, 1, 2]},
                 {"parent_id": 5, "ordered_ids": [7, 6, 8]}]

    Returns:
        list[SectionTreeNode]: 重排后的完整目录树
    """
    if not groups:
        return build_tree(project_id)

    with get_db() as conn:
        try:
            for group in groups:
                parent_id = group.get("parent_id")
                ordered_ids = group.get("ordered_ids", [])
                if not ordered_ids:
                    continue
                # PERF（2026-09-16）：逐行 UPDATE → executemany 批量执行
                conn.executemany(
                    "UPDATE sections SET sort_order = ? WHERE id = ? AND project_id = ?",
                    [(idx + 1, sid, project_id) for idx, sid in enumerate(ordered_ids)],
                )

            # 验证排序连续性：检查每个父节点下 sort_order 是否从 1 开始连续无间隙
            for group in groups:
                parent_id = group.get("parent_id")
                ordered_ids = group.get("ordered_ids", [])
                if not ordered_ids:
                    continue
                if parent_id is None:
                    rows = conn.execute(
                        "SELECT sort_order FROM sections WHERE project_id = ? AND parent_id IS NULL ORDER BY sort_order",
                        (project_id,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT sort_order FROM sections WHERE project_id = ? AND parent_id = ? ORDER BY sort_order",
                        (project_id, parent_id),
                    ).fetchall()
                expected = list(range(1, len(rows) + 1))
                actual = [r["sort_order"] for r in rows]
                if actual != expected:
                    logger.warning(
                        "重排后 sort_order 连续性异常: parent_id=%s, 期望=%s, 实际=%s",
                        parent_id,
                        expected,
                        actual,
                    )
        except Exception as e:
            logger.error("重排章节失败: %s", e)
            conn.rollback()
            raise
    # BUG 修复：重排后失效全部章节缓存，避免 DB 已更新但返回过期排序数据
    invalidate_content_caches(project_id)
    return build_tree(project_id)


def get_leaf_sections(project_id: int) -> list[Section]:
    """获取所有叶子节点（无子章节的条目）"""
    all_sections = list_sections(project_id)
    parent_ids = {s.parent_id for s in all_sections if s.parent_id is not None}
    return [s for s in all_sections if s.id not in parent_ids and s.level >= 1]


def get_subtree_leaf_ids(project_id: int, root_id: int) -> list[int]:
    """获取指定节点子树下所有叶子节点的ID"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, parent_id FROM sections WHERE project_id = ? ORDER BY sort_order, created_at",
            (project_id,),
        ).fetchall()

    children_map: dict[int | None, list[int]] = {}
    all_ids = set()
    for r in rows:
        pid = r["parent_id"]
        children_map.setdefault(pid, []).append(r["id"])
        all_ids.add(r["id"])

    if root_id not in all_ids:
        return []

    subtree_ids = set()
    stack = [root_id]
    while stack:
        nid = stack.pop()
        subtree_ids.add(nid)
        for cid in children_map.get(nid, []):
            stack.append(cid)

    parent_ids = set()
    for r in rows:
        if r["parent_id"] in subtree_ids:
            parent_ids.add(r["parent_id"])

    return [sid for sid in subtree_ids if sid not in parent_ids]


@timer
def get_section_word_budget(project_id: int, default_level2: int = 3500) -> dict:
    """计算字数预算（覆盖全层级，含 L3+ 叶子）

    与 _get_effective_target_words 分档标准保持一致：
      L1 → 500
      L2 → level2_default（默认 3500，可自定义）
      L3 → 300 （具体执行条目）
      L4 → 400
      L5+ → 300

    注：如果章节设置了自定义 target_words（>0），优先使用自定义值。
    """
    sections = list_sections(project_id)
    level1_min = 500
    level2_default = default_level2

    # 层级分档映射（与 sse_utils._get_effective_target_words 保持一致）
    LEVEL_FALLBACK = {1: 500, 2: level2_default, 3: 300, 4: 400}
    def _level_default(level: int) -> int:
        return LEVEL_FALLBACK.get(level, 300)

    total_estimate = 0
    level_counts = {1: 0, 2: 0, 3: 0, 4: 0, "leaf_deep": 0}
    has_custom_target = 0

    for s in sections:
        # BUG-FIX 2026-09-18（P0）：level 原先只在 else 分支赋值，但下方的
        # 层级分档计数在两个分支外统一读取 level —— 若首个章节设置了自定义
        # target_words（走 tw>0 分支）则 level 从未定义 → UnboundLocalError（500）；
        # 否则 level 取上一轮循环残留值，自定义字数章节被错归到上一章的层级档。
        # 改为循环体首行统一取值。
        level = s.level or 3
        tw = s.target_words or 0
        if tw > 0:
            total_estimate += tw
            has_custom_target += 1
        else:
            total_estimate += _level_default(level)

        if level in (1, 2, 3, 4):
            level_counts[level] += 1
        else:
            level_counts["leaf_deep"] += 1

    return {
        "level1_count": level_counts[1],
        "level2_count": level_counts[2],
        "level3_count": level_counts[3],
        "level4_count": level_counts[4],
        "level_deep_count": level_counts["leaf_deep"],
        "level1_min": level1_min,
        "level2_default": level2_default,
        "level3_default": 300,
        "level4_default": 400,
        "has_custom_targets": has_custom_target,
        "total_estimate": total_estimate,
    }


def get_section_path(section_id: int) -> str:
    """获取章节路径（如 1.1.3）

    性能优化：一次性获取整棵树，避免递归 N+1 查询。
    """
    section = get_section(section_id)
    if not section:
        return ""

    # 一次性获取项目下所有章节（按层级组织）
    project_id = section.project_id
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, parent_id, title, sort_order, created_at FROM sections "
            "WHERE project_id = ? ORDER BY sort_order, created_at",
            (project_id,),
        ).fetchall()

    # 构建父节点映射和排序索引
    # PERF-B7-WIRE（2026-09-16）：perf_optimizations.build_sort_lookup 此前已实现但
    # 未接线，原实现在排序键 lambda 里用 next(...) 逐个线性扫描全部 rows，
    # 单父节点 N 个子章节时为 O(N²)。现在一次遍历预建 O(1) 查找表。
    parent_map: dict[int, int | None] = {}
    siblings_by_parent: dict[int | None, list[int]] = {}
    sort_lookup: dict[int, tuple] = {}
    for r in rows:
        sid = r["id"]
        pid = r["parent_id"]
        parent_map[sid] = pid
        siblings_by_parent.setdefault(pid, []).append(sid)
        sort_lookup[sid] = (r["sort_order"] or 0, r["created_at"] or "")

    # 对每个父节点下的子节点按 sort_order 排序（O(N log N)，键计算 O(N)）
    for pid in siblings_by_parent:
        siblings_by_parent[pid].sort(key=lambda sid: sort_lookup.get(sid, (0, "")))

    # 自底向上构建路径
    path_parts = []
    current_id = section_id
    while current_id is not None:
        pid = parent_map.get(current_id)
        siblings = siblings_by_parent.get(pid, [])
        idx = siblings.index(current_id) + 1 if current_id in siblings else 1
        path_parts.insert(0, str(idx))
        current_id = pid

    return ".".join(path_parts)


def get_ancestor_titles(section_id: int) -> list[str]:
    """获取祖先节点标题列表（从根到当前）

    性能优化：一次性查询整棵树，避免递归 N+1 查询。
    """
    section = get_section(section_id)
    if not section:
        return []

    project_id = section.project_id
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, parent_id, title FROM sections "
            "WHERE project_id = ? ORDER BY sort_order, created_at",
            (project_id,),
        ).fetchall()

    # 构建父节点映射
    parent_map: dict[int, int | None] = {}
    title_map: dict[int, str] = {}
    for r in rows:
        parent_map[r["id"]] = r["parent_id"]
        title_map[r["id"]] = r["title"]

    # 自底向上收集标题
    titles = []
    current_id = section_id
    while current_id is not None:
        titles.insert(0, title_map.get(current_id, ""))
        current_id = parent_map.get(current_id)

    return titles


def validate_outline_integrity(project_id: int) -> dict:
    """校验目录树形结构完整性，在保存前检查以下问题：

    1. 所有 parent_id 指向有效存在的章节（或为 None）
    2. 无循环引用（如 A->B->A）
    3. 同一父节点下无重复的 sort_order

    Args:
        project_id: 项目 ID

    Returns:
        dict: {"valid": bool, "errors": list[str]}
              valid 为 True 表示无问题，errors 列出所有发现的错误
    """
    from collections import defaultdict

    errors: list[str] = []

    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, parent_id, sort_order FROM sections WHERE project_id = ? ORDER BY id",
            (project_id,),
        ).fetchall()

    sections = [dict(r) for r in rows]
    all_ids: set[int] = {s["id"] for s in sections}

    if not sections:
        return {"valid": True, "errors": []}

    # 1. 检查所有 parent_id 是否指向有效存在的章节
    for s in sections:
        pid = s["parent_id"]
        if pid is not None and pid not in all_ids:
            errors.append(f"章节 {s['id']} 的 parent_id={pid} 指向不存在的章节")

    # 2. 检查是否有循环引用（DFS 检测环 + 记忆化）
    # PERF（2026-09-16）：原实现从每个节点各自向上走完整条祖先链，链状大纲下
    # 为 O(N·depth)（最坏 O(N²)）。现缓存「已确认可安全到达根」的节点，
    # 整体降为 O(N)；报错语义与原实现一致（环上每个起点各报一次）。
    parent_map: dict[int, int | None] = {s["id"]: s["parent_id"] for s in sections}
    safe_nodes: set[int] = set()  # 已确认无环（可到达根/已知安全节点）的节点
    for sid in all_ids:
        if sid in safe_nodes:
            continue
        path: list[int] = []
        on_path: set[int] = set()
        current: int | None = sid
        cycle_found = False
        while current is not None:
            if current in safe_nodes:
                break
            if current in on_path:
                errors.append(f"检测到循环引用，涉及章节 ID: {current}")
                cycle_found = True
                break
            on_path.add(current)
            path.append(current)
            current = parent_map.get(current)
        if not cycle_found:
            safe_nodes.update(path)

    # 3. 检查同一父节点下是否有重复的 sort_order
    parent_groups: dict[int | None, list[dict]] = defaultdict(list)
    for s in sections:
        parent_groups[s["parent_id"]].append(s)

    for pid, group in parent_groups.items():
        seen_orders: dict[int, int] = {}
        for s in group:
            so = s["sort_order"]
            if so in seen_orders:
                errors.append(
                    f"父节点 {pid} 下章节 {s['id']} 和 {seen_orders[so]} 的 sort_order 重复 ({so})"
                )
            else:
                seen_orders[so] = s["id"]

    return {"valid": len(errors) == 0, "errors": errors}
