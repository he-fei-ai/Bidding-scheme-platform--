from datetime import UTC, datetime
import logging
import threading

from ..db import get_db, safe_update
from ..models import Section, SectionCreate, SectionTreeNode, SectionUpdate
from ..utils import count_readable_words as _count_readable_words
from ..utils import count_words as _count_words
from ..utils import normalize_newlines  # HARDEN 2026-10-12：读出侧 CRLF 归一
from ..utils.performance import timer


logger = logging.getLogger(__name__)

# 性能优化：缓存 build_tree 结果（项目ID -> 树结构），TTL=60s
_build_tree_cache: dict[int, tuple[float, list[SectionTreeNode]]] = {}
_BUILD_TREE_CACHE_TTL = 60.0
_BUILD_TREE_CACHE_MAX = 200  # 最多缓存 200 个项目的树结构
_BUILD_TREE_CACHE_LOCK = threading.Lock()
# BUG-FIX 2026-10-08（并发竞态 · 高）：build_tree 是「读缓存 → 查 DB → 写缓存」
# 三步，中间任何一次章节写操作调用 `_invalidate_build_tree_cache` 都会被**随后
# 的写缓存覆盖**——即失效被吞。表现为：改名/删除/重排后 60s 内刷新仍拿到旧树，
# 且因为 TTL 已重置而要等满 60s，且下一次重建又会再被覆盖一次。
# 解法：全局代号（epoch）。build_tree 进入时快照代号，写缓存前比对；
# 不一致说明期间发生过失效/写入，直接放弃写缓存（返回本次新鲜结果，
# 下次调用会重新查库），既保证正确性又不引入额外 IO 竞争。
_build_tree_epoch = 0

# ENHANCE 2026-10-08：目录树「可管理层级深度」上限。编号命名空间
# （heading_standard.DEFAULT_NUMBERING_TEMPLATES）只定义到 L8，Word 大纲也只到
# 9 级；深度失控（脏数据成链 / 异常超深 PATCH）时再往上比对 level 已无意义，
# 故 validate_outline_levels 只在受管范围内逐节点比对，超出部分整体报一条。
_MAX_MANAGED_OUTLINE_DEPTH = 8


def _current_build_tree_epoch() -> int:
    with _BUILD_TREE_CACHE_LOCK:
        return _build_tree_epoch


def _invalidate_build_tree_cache(project_id: int) -> None:
    """失效指定项目的 build_tree 缓存

    同时推进全局代号，使**正在构建中**的 build_tree 放弃写缓存（见模块注释的
    竞态说明）。这是必须与 pop 同时做的——只 pop 不改代号，竞态窗口依然存在。
    """
    global _build_tree_epoch
    with _BUILD_TREE_CACHE_LOCK:
        _build_tree_cache.pop(project_id, None)
        _build_tree_epoch += 1


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
            # HARDEN 2026-10-12：与 get_section 同构，读出侧统一归一 CRLF。
            d["content"] = normalize_newlines(d.get("content", "") or "")
            d["word_count"] = _count_words(d.get("content", "") or "")
            result.append(Section(**d))
        return result


def get_section(section_id: int) -> Section | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM sections WHERE id = ?", (section_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        # HARDEN 2026-10-12（数据链最上游归一）：Windows 环境下 DB 里历史正文
        # 可能带 \r\n（AI 网关未清洗前的旧数据 / 通过旧接口写入的内容）。
        # get_section 是所有单节读出的唯一入口（详情接口 / 编辑器回填 / 前端渲染），
        # 在此一次性归一，等价于把 clean_generated_content 的入口保护向前再推一层，
        # 保证下游 content_cleaning / 围栏识别 / 前端 Markdown 编辑器看到的都是纯 LF。
        d["content"] = normalize_newlines(d.get("content", "") or "")
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


def _normalize_sibling_sort_order(conn, section_id: int, requested: int | None = None) -> None:
    """把章节在新父节点下的 sort_order 归一到「1 起连续、无重复」。

    BUG-FIX 2026-10-08（多级编号不同步）：换父后原 sort_order 是**在旧父下的
    位次**，直接沿用会与新父的既有子节点大面积撞号（`build_tree` 的排序键是
    `(sort_order, created_at)`，撞号后退化为按创建时间排 = 用户拖拽顺序被吞）。
    与 move_section / reorder_sections 共用同一份「重建为 1 起连续」契约。
    未显式指定顺序时追加到尾部（口径同 `_normalize_section_position` 的创建行为）。

    Args:
        conn: 已在事务中的连接
        section_id: 被移动的章节
        requested: 客户端显式指定的 sort_order（1 起）；None = 追加到尾部
    """
    row = conn.execute("SELECT parent_id FROM sections WHERE id = ?", (section_id,)).fetchone()
    if row is None:
        return
    parent_id = row["parent_id"]
    if parent_id is None:
        sib_rows = conn.execute(
            "SELECT id FROM sections WHERE project_id = "
            "(SELECT project_id FROM sections WHERE id = ?) AND parent_id IS NULL "
            "ORDER BY COALESCE(sort_order, 0), id",
            (section_id,),
        ).fetchall()
    else:
        sib_rows = conn.execute(
            "SELECT id FROM sections WHERE parent_id = ? ORDER BY COALESCE(sort_order, 0), id",
            (parent_id,),
        ).fetchall()
    ids = [r["id"] for r in sib_rows]
    if section_id not in ids:
        ids.append(section_id)
    if requested is not None and section_id in ids:
        try:
            target = max(1, min(int(requested), len(ids)))
        except (TypeError, ValueError):
            target = len(ids)
        ids.remove(section_id)
        ids.insert(target - 1, section_id)
    conn.executemany(
        "UPDATE sections SET sort_order = ? WHERE id = ?",
        list(enumerate(ids, start=1)),
    )


def _insert_section(conn, project_id: int, data: SectionCreate, now: str) -> Section:
    """使用已有连接插入章节（用于事务内调用）"""
    cursor = conn.execute(
        """INSERT INTO sections
           (project_id, parent_id, title, level, sort_order, section_type,
            scoring_point_id, word_count_tier, target_words, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            project_id,
            data.parent_id,
            data.title,
            data.level,
            data.sort_order,
            data.section_type,
            data.scoring_point_id,
            data.word_count_tier,
            data.target_words,
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
        # BUG-FIX 2026-10-08（多级编号不同步 · 高）：PATCH 可改 parent_id，但此前
        # **只改父指针、不重算层级**。把一个 L3 节点挂到 L1 下，DB 里 level 仍是 3，
        # 其整棵子树也仍是旧层级 → 连锁错三处：
        #   1) 前端 computeHeadingCounters 按 node.level 分组，编号与真实深度脱节；
        #   2) DOCX 导出按 level 设 w:outlineLvl / 套用 L1..L4 编号模板，目录层级错；
        #   3) get_section_word_budget 按 level 分档取 target_words，字数预算落到错误档。
        # 与 create 的 _normalize_section_position / 落库的 _save_nodes 统一契约：
        # 「层级只由树位置派生」。注意 SectionUpdate **没有 level 字段**
        # （Pydantic v2 默认 extra='ignore'，客户端传 level 会被静默丢弃），
        # 因此这里刻意不接受显式 level —— 一旦允许客户端指定，换父后就会出现
        # 「父层 2 / 子层 3（手动指定）」这类父子倒挂的脏树，正是本修复要消灭的。
        if "parent_id" in updates and updates["parent_id"] != existing_row["parent_id"]:
            updates.pop("level", None)  # 防御：直接调服务层时也别让它落库
            _new_pid = updates["parent_id"]
            if _new_pid is None:
                _base_level = 1
            else:
                _prow = conn.execute(
                    "SELECT level FROM sections WHERE id = ?", (_new_pid,)
                ).fetchone()
                _base_level = ((_prow["level"] or 1) if _prow else 0) + 1
            # 递归 CTE 一次性取回「自身 + 全部后代」的相对深度，再按行批量重写
            # level，避免 Python 侧逐层查询（链状大纲下 O(N) 次往返）。
            # 分两步而不是 `WITH ... UPDATE ... SET level = ? + depth`：
            # SQLite 不允许在 UPDATE 的 SET 子句里引用 CTE 定义的列
            # （sqlite3.OperationalError: no such column: depth）。
            _depth_rows = conn.execute(
                """
                WITH RECURSIVE subtree(id, depth) AS (
                    SELECT id, 0 FROM sections WHERE id = ?
                    UNION ALL
                    SELECT s.id, sub.depth + 1 FROM sections s
                    JOIN subtree sub ON s.parent_id = sub.id
                )
                SELECT id, depth FROM subtree
                """,
                (section_id,),
            ).fetchall()
            if _depth_rows:
                conn.executemany(
                    "UPDATE sections SET level = ? WHERE id = ?",
                    [(_base_level + r["depth"], r["id"]) for r in _depth_rows],
                )
            # BUG-P3-B FIX 2026-10-11：换父后正文内 Markdown 标题编号残留净化。
            # 此前只重算 level、不触及 content——重排后导出端从目录结构重新生成
            # 编号时，目录层级与正文内嵌编号（## 1.1 xxx / ### 2.3.1 yyy）
            # 可能不一致。现对整棵子树的 content 剥离内嵌编号前缀，只保留标题正文。
            try:
                from .content_cleaning import strip_heading_numbering_from_content
                for r in _depth_rows:
                    _cid = r["id"]
                    _content_row = conn.execute(
                        "SELECT id, content FROM sections WHERE id = ?", (_cid,)
                    ).fetchone()
                    if _content_row and _content_row["content"]:
                        _new_content = strip_heading_numbering_from_content(_content_row["content"])
                        if _new_content != _content_row["content"]:
                            _wc = _count_words(_new_content)
                            conn.execute(
                                "UPDATE sections SET content = ?, word_count = ?, "
                                "readable_word_count = ? WHERE id = ?",
                                (_new_content, _wc, _count_readable_words(_new_content), _cid),
                            )
            except Exception as _strip_err:
                # 编号净化是非关键路径；失败降级为只重算 level
                logger.debug("[章节重排] 正文编号净化跳过: %s", _strip_err)
            # 换父后同级 sort_order 可能与既有节点撞号（前端拖拽只带 order 不带
            # sort_order 时尤其明显），落库前归一到 1 起连续，编号/展示顺序才确定。
            _normalize_sibling_sort_order(conn, section_id, updates.get("sort_order"))
        # BUG-FIX 2026-10-04（P1·BUG-C）：正文变更时同一条 SQL 同步持久化 word_count。
        # 旧实现只在返回对象里重算字数，DB 列陈旧——一致性审计路径
        # （sse_content.update_section(SectionUpdate(content=...))）与前端编辑保存后，
        # 目录字数列/达标判定与正文实际字数不一致。口径对齐兜底扩写的裸 SQL（已同步）。
        if "content" in updates:
            _content_new = updates.get("content") or ""
            _wc = _count_words(_content_new)
            conn.execute(
                "UPDATE sections SET word_count = ?, readable_word_count = ? WHERE id = ?",
                (_wc, _count_readable_words(_content_new), section_id),
            )
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
    COALESCE(readable_word_count, 0) AS readable_word_count,
    CASE WHEN length(content) > 0 THEN 1 ELSE 0 END AS has_content,
    COALESCE(length(flowchart_json), 0) > 0 AS has_flowchart,
    COALESCE(length(gantt_json), 0) > 0 AS has_gantt,
    COALESCE(length(architecture_json), 0) > 0 AS has_architecture,
    COALESCE(length(labor_json), 0) > 0 AS has_labor,
CASE WHEN graph_suitable = 1 THEN 1 ELSE 0 END AS graph_suitable,
    COALESCE(suggested_chart, '') AS suggested_chart,
    -- BUG-FIX 2026-10-09（跨模块数据链 / 字数档位）：此前 build_tree 的
    -- 轻量列清单不含 word_count_tier，而 SectionTreeNode 的
    -- word_count_tier / tier_label / tier_target 三个字段在构造时也没传，
    -- 于是 models.py 里那三个字段永远取默认值 None。
    -- 后果：前端 utils.ts 的 resolveTargetForNode / resolveToleranceForNode
    -- 其「档位分支」是整段死代码，PATCH /sections/{id}/word-count-tier
    -- 在 UI 上完全无效。现补齐 SELECT 与构造，保证 目录 → 章节编写 →
    -- 字数校验 三段共用同一份档位事实。
COALESCE(word_count_tier, '') AS word_count_tier
"""


def _load_tier_label_maps() -> tuple[dict[str, str], dict[str, int]]:
    """一次性载入字数档位的 {key: label} 与 {key: target_words} 映射。

    BUG-FIX 2026-10-09（跨模块数据链 / 字数档位）：`build_tree` 需要给每个
    SectionTreeNode 填 `tier_label` / `tier_target`，而这两个值来自
    `word_count_tiers` 表。若在逐节点循环里调 `get_tier_config` 会产生 N+1
    查询（一次 build_tree 最多几百个节点）；这里改为循环外查一次并复用。

    失败（表不存在 / DB 不可用）时返回两个空 dict，此时 `tier_label` 与
    `tier_target` 为 None，前端按层级默认值回落 —— 与本修复前的行为一致。
    """
    try:
        from .ai.writing_length_config import list_tier_configs

        cfgs = list_tier_configs() or []
    except Exception as e:  # pragma: no cover - 档位表异常不应影响目录树
        logger.warning("字数档位配置载入失败，档位展示字段留空（忽略）: %s", e)
        return {}, {}
    labels: dict[str, str] = {}
    targets: dict[str, int] = {}
    for cfg in cfgs:
        key = cfg.get("key")
        if not key:
            continue
        labels[key] = cfg.get("label") or ""
        tw = cfg.get("target_words")
        if isinstance(tw, int) and tw > 0:
            targets[key] = tw
    return labels, targets


@timer
def build_tree(project_id: int) -> list[SectionTreeNode]:
    """构建章节目录树（带缓存，TTL=60s，BUG-FIX 2026-10-08：代号防失效竞态）"""
    import time

    now = time.time()
    # BUG-FIX 2026-10-08（并发竞态）：进入时快照代号，写缓存前比对，
    # 期间若有任何失效/写入发生则放弃写缓存，避免把过期结果再塞回缓存。
    epoch_at_start = _current_build_tree_epoch()
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
        # 回填另开写会话走全局写锁，readonly 会话内绝不可写。
        # readable_word_count 同样纳入自愈条件：老库该列默认 0，此处顺带按
        # R1 口径把存量正文一次性补齐（与 word_count 同一次回读，零额外 SELECT）。
        dirty_ids = [
            r["id"] for r in rows
            if (r["word_count"] == 0 or r.get("readable_word_count", 0) == 0)
            and r["has_content"]
        ]
        if dirty_ids:
            placeholders = ",".join("?" * len(dirty_ids))
            content_rows = conn.execute(
                f"SELECT id, content FROM sections WHERE id IN ({placeholders})",
                dirty_ids,
            ).fetchall()
            wc_map = {
                cr["id"]: (
                    _count_words(cr["content"] or ""),
                    _count_readable_words(cr["content"] or ""),
                )
                for cr in content_rows
            }

    if wc_map:
        try:
            with get_db() as wconn:
                wconn.executemany(
                    "UPDATE sections SET word_count = ?, readable_word_count = ? WHERE id = ?",
                    [(wc, rwc, sid) for sid, (wc, rwc) in wc_map.items()],
                )
        except Exception as e:
            # 回填失败不影响树正确性（本次已重算），下次重建重试
            logger.warning("word_count 惰性回填失败（忽略）: %s", e)

    by_id: dict[int, SectionTreeNode] = {}
    roots: list[SectionTreeNode] = []

    # BUG-FIX 2026-10-09（跨模块数据链 / 字数档位）：一次性载入档位配置表，
    # 避免在逐节点循环里反复查库（N+1）。档位缺失时保持 None，
    # 由前端回落到层级默认值，行为与修复前一致（向后兼容）。
    # `_build_tier_label_map` 失败不影响树本身，仅档位展示字段为空。
    tier_label_map, tier_target_map = _load_tier_label_maps()

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
        _tier_key = (r.get("word_count_tier") or "").strip() or None
        # 惰性回填的 (word_count, readable_word_count) 元组优先于可能为 0 的列值；
        # 未命中回填集合时直接用列值（readable 列仅回填过才非 0）。
        _wc_pair = wc_map.get(r["id"])
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
            word_count=_wc_pair[0] if _wc_pair else r["word_count"],
            chart_flags=flags,
graph_suitable=bool(r["graph_suitable"]),
            suggested_chart=r["suggested_chart"] or "",
            # BUG-FIX 2026-10-09：透出档位三元组，让前端
            # resolveTargetForNode / resolveToleranceForNode 的档位分支真正生效。
            # key 为空（未设档位）时三个字段均保持 None，行为与修复前完全一致。
            word_count_tier=_tier_key,
            tier_label=tier_label_map.get(_tier_key) if _tier_key else None,
            tier_target=tier_target_map.get(_tier_key) if _tier_key else None,
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

    # 累计字数：每节点的 cumulative_word_count = 自身 word_count + 子树全部后代
    # word_count（控制层级 L1/L2 使用该累计值作为累计统计）。
    def _cumulate(node: SectionTreeNode) -> int:
        total = node.word_count
        for child in node.children:
            total += _cumulate(child)
        node.cumulative_word_count = total
        return total

    for node in by_id.values():
        _cumulate(node)

# 写入缓存（带锁保护 + 大小限制）

    with _BUILD_TREE_CACHE_LOCK:
        if _build_tree_epoch != epoch_at_start:
            # 竞态：本次构建期间发生过失效/写入，本次结果可能已过期，
            # 宁可不缓存（下次调用重查库）也不要把脏树留给 60s 内的其他请求。
            logger.debug("build_tree 构建期间发生缓存失效，跳过写缓存 project=%d", project_id)
            return roots
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

            # ENHANCE 2026-10-08（日志不准确 + 自愈）：原实现只比对 sort_order
            # 是否为 1..N，不一致时打一条 warning 就把树交出去 —— 三处问题：
            #   (a) 校验排序键是 `ORDER BY sort_order`，而 build_tree 的真实排序键是
            #       `(sort_order, created_at)`。sort_order 撞号时校验看到的顺序与
            #       实际展示顺序不同，「校验通过」并不代表用户看到的就是这个顺序；
            #   (b) ordered_ids 只覆盖调用方给的分组，其余分组里的历史撞号/间隙
            #       完全没人管，用户拖完之后一部分章节顺序「莫名其妙」；
            #   (c) 发现异常只写日志、不修复、也不回传调用方，前端拿到 200 +
            #       一棵顺序不确定的树，无从提示。
            # 现改为：按 build_tree 的真实排序键复读 → 不连续就就地重建为 1..N
            #（自愈）→ 自愈动作与最终顺序都记日志，异常从 warning 升为 error。
            repaired_groups: list[str] = []
            for group in groups:
                parent_id = group.get("parent_id")
                if parent_id is None:
                    sib = conn.execute(
                        "SELECT id, sort_order, created_at FROM sections "
                        "WHERE project_id = ? AND parent_id IS NULL "
                        "ORDER BY COALESCE(sort_order, 0), created_at, id",
                        (project_id,),
                    ).fetchall()
                else:
                    sib = conn.execute(
                        "SELECT id, sort_order, created_at FROM sections "
                        "WHERE project_id = ? AND parent_id = ? "
                        "ORDER BY COALESCE(sort_order, 0), created_at, id",
                        (project_id, parent_id),
                    ).fetchall()
                if not sib:
                    continue
                actual = [r["sort_order"] for r in sib]
                expected = list(range(1, len(sib) + 1))
                has_dup = len(set(actual)) != len(actual)
                if has_dup:
                    # 重复 = build_tree 的排序键 (sort_order, created_at) 退化到
                    # 「按创建时间排」，用户拖的顺序被完全吞掉 —— 必须自愈。
                    logger.error(
                        "重排后同父 sort_order 重复，已自动重建 parent_id=%s 原值=%s -> %s",
                        parent_id, actual, expected,
                    )
                    conn.executemany(
                        "UPDATE sections SET sort_order = ? WHERE id = ?",
                        [(i, r["id"]) for i, r in enumerate(sib, start=1)],
                    )
                    repaired_groups.append(f"parent_id={parent_id} 原值={actual}")
                elif actual != expected:
                    # 只是起点/间隙不连续（值唯一 → 顺序仍然确定），保持既有
                    # warning 语义、不改状态：跨项目 id 被 AND project_id 过滤后
                    # ordered_ids 位次与实际兄弟数不对齐就会走到这里，属于
                    # 「调用方分组不完整」而非数据损坏，强行重排反而丢位次。
                    logger.warning(
                        "重排后 sort_order 非 1 起连续（值唯一，顺序仍确定）: parent_id=%s, 期望=%s, 实际=%s",
                        parent_id,
                        expected,
                        actual,
                    )
            if repaired_groups:
                logger.warning(
                    "重排自愈完成 project=%d，涉及分组 %s", project_id, "; ".join(repaired_groups)
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
    """计算字数预算（2026-10-08 版：按三级目录档位分档累加）。

    字数控制策略变更：
    - 控制层级从二级目录下沉到三级目录（L3，叶子节点）
    - 四档标准：简短 800 / 标准 1200 / 详细 2000 / 超长 2500，±10% 浮动
    - L1/L2 字数由其下所有 L3 字数累加得出，仅作统计展示

    Args:
        project_id: 项目 ID
        default_level2: 历史兼容参数（不再使用），仅保留避免旧调用报错

    Returns:
        dict: 字数预算信息（含 level3_count / level3_default / tier_key / total_estimate 等）
    """
    from ..services.ai.writing_length_config import (
        get_tier_config,
        get_default_tier_config,
        get_l3_own_target,
        get_l3_own_tolerance,
        get_l3_own_range,
        WRITING_LENGTH_TIER_CONFIG,
    )

    sections = list_sections(project_id)
    level_counts = {1: 0, 2: 0, 3: 0, 4: 0, "leaf_deep": 0}
    has_custom_target = 0

    # 全局默认档位（从配置表读取）
    default_cfg = get_default_tier_config()
    tier_key = default_cfg.get("key", "standard")
    tier_label = default_cfg.get("label", "标准")
    tier_target = int(default_cfg.get("target_words", 1200))
    tier_tolerance = float(default_cfg.get("tolerance_ratio", 0.10))

    # 方案三（2026-10-12 混合口径）：预估总字数只按「每个 L3」累加，
    #   L1/L2 不生成正文不计入；L4+ 并入父 L3 子树不单独计入（避免重复计数）。
    #   - 叶子 L3（无下级）：固定 L3 自身正文 300 ± 20%，不受四档下拉影响；
    #   - 含下级的 L3：四档 target 作为「子树总字数」；
    #   - 覆盖优先级：单节点 target_words > 单节点 word_count_tier > 上述规则。
    l3_own_target = get_l3_own_target()
    l3_own_tolerance = get_l3_own_tolerance()
    l3_own_lo, l3_own_hi = get_l3_own_range()

    # 有下级的父节点 id 集合，用于判定某 L3 是否为叶子
    parent_ids = {s.parent_id for s in sections if s.parent_id is not None}

    total_estimate = 0
    leaf_l3_count = 0
    subtree_l3_count = 0
    for s in sections:
        level = s.level or 3
        if level in (1, 2, 3, 4):
            level_counts[level] += 1
        else:
            level_counts["leaf_deep"] += 1

        # 预估总字数只累加每个 L3；L1/L2/L4+ 不单独计入
        if level != 3:
            continue

        has_children = s.id in parent_ids
        if has_children:
            subtree_l3_count += 1
        else:
            leaf_l3_count += 1

        tw = s.target_words or 0
        if tw > 0:
            # 1) 单节点精确覆盖
            total_estimate += tw
            has_custom_target += 1
        elif getattr(s, "word_count_tier", None):
            # 2) 单节点档位覆盖
            cfg = get_tier_config(s.word_count_tier)
            total_estimate += int(cfg["target_words"]) if cfg else tier_target
        elif has_children:
            # 3) 含下级的 L3：四档作为子树总字数
            total_estimate += tier_target
        else:
            # 4) 叶子 L3：自身正文固定 L3_OWN(300)
            total_estimate += l3_own_target

    return {
        "level1_count": level_counts[1],
        "level2_count": level_counts[2],
        "level3_count": level_counts[3],
        "level4_count": level_counts[4],
        "level_deep_count": level_counts["leaf_deep"],
        # 三级目录档位信息（四档 = 子树总字数，仅对含下级目录的 L3 生效）
        "tier_key": tier_key,
        "tier_label": tier_label,
        "tier_target": tier_target,
        "tier_tolerance": tier_tolerance,
        # 方案三：叶子 L3 自身正文口径（默认 300±20%）——前端预算卡片每章展示以此为准
        "l3_own_target": l3_own_target,
        "l3_own_tolerance": l3_own_tolerance,
        "l3_own_lo": l3_own_lo,
        "l3_own_hi": l3_own_hi,
        "leaf_l3_count": leaf_l3_count,
        "subtree_l3_count": subtree_l3_count,
        # level3_default 改为叶子 L3 自身正文目标（旧值 = 四档 tier_target 会虚高约 4 倍）
        "level3_default": l3_own_target,
        # 保留旧字段以兼容（值为 0，L2 不再直接控制）
        "level1_min": 0,
        "level2_default": 0,
        "level4_default": 0,
        "has_custom_targets": has_custom_target,
        "total_estimate": total_estimate,
    }


def get_section_path(section_id: int) -> str:
    """获取章节路径（如 1.1.3）

    性能优化：一次性获取整棵树，避免递归 N+1 查询。

    2026-10-11 标记（O-12）：此为后端路径式编号的**唯一权威实现**。
    前端 `collectHeadingPaths`（`frontend/src/utils.ts:161-195`）为独立实现，
    审核态可能显示不同编号。后续需将前端实现对齐到本函数口径，
    或改为调用后端 API 获取路径，消除双实现漂移风险。
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
    # BUG-FIX 2026-10-08（死循环/容错）：原实现 `while current_id is not None` 只判
    # None。一旦库里存在 parent_id 环（batch_create_sections 手工传 parent_id、
    # 历史脏数据、并发改父指针的中间态），沿链走永不为 None → 工作线程死循环、
    # GIL 占用、正文生成整任务挂起（与 BUG-FIX 2026-09-18 修的
    # _get_path_and_ancestors 同类风险的漏网副本）。
    # 加「已访问集合」护栏：一旦回到走过的节点即判定成环，中止并 error 留痕。
    path_parts = []
    current_id: int | None = section_id
    visited: set[int] = set()
    while current_id is not None:
        if current_id in visited:
            logger.error(
                "章节路径存在 parent_id 环，中止遍历 section_id=%d 环点=%d",
                section_id, current_id,
            )
            break
        visited.add(current_id)
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
    # BUG-FIX 2026-10-08（死循环/容错）：同 get_section_path，parent_id 成环时
    # 原文是无限循环，祖先标题永远拼不完（正文生成的上下文拼装直接挂死）。
    titles = []
    current_id: int | None = section_id
    visited: set[int] = set()
    while current_id is not None:
        if current_id in visited:
            logger.error(
                "祖先标题存在 parent_id 环，中止遍历 section_id=%d 环点=%d",
                section_id, current_id,
            )
            break
        visited.add(current_id)
        titles.insert(0, title_map.get(current_id, ""))
        current_id = parent_map.get(current_id)

    return titles


def validate_outline_integrity(project_id: int) -> dict:
    """校验目录树形结构完整性，在保存前检查以下问题：

    1. 所有 parent_id 指向有效存在的章节（或为 None）
    2. 无循环引用（如 A->B->A）
    3. 同一父节点下无重复的 sort_order

    注：「level 与实际深度是否一致」是**独立**的第四类体检，见
    `validate_outline_levels`（ENHANCE 2026-10-08）。刻意不并入本函数：
    `valid` 是既有对外契约（落库前自检、测试断言都按「三项结构」理解），
    混入层级语义会让「结构没问题但层级漂移」的目录被判 invalid 而无法落库。

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


def validate_outline_levels(project_id: int) -> dict:
    """校验「level 是否等于该节点的实际父子深度」（ENHANCE 2026-10-08）

    背景：本模块全局契约是「level 由父子关系推导」——创建走
    `_normalize_section_position`、目录落库走 `_save_nodes` / `_flatten_outline_tree`、
    换父走 `update_section` 的递归 CTE 重算。但 `validate_outline_integrity` 只查
    结构三项，level 漂移（历史数据、绕过 `update_section` 的裸 SQL 改父指针、
    数据迁移）完全不可见，而 level 漂移会让三处同时错：
      · 前端 `computeHeadingCounters` 按 level 分组 → 编号错位
      · DOCX 导出按 level 设 `w:outlineLvl` / 套编号模板 → 大纲层级错
      · `get_section_word_budget` 按 level 分档 → 字数预算落错档
    故作为独立的第四类体检暴露，供落库后自检与运维巡检调用。

    与 `validate_outline_integrity` 分离的两个理由：
      1. `valid` 是既有对外契约，混入层级语义会让「结构完好、层级漂移」的目录
         被判 invalid 而无法落库；
      2. 超过 `_MAX_MANAGED_OUTLINE_DEPTH` 的畸形深链，其 level 本就无定义
         （Word 只支持 9 级大纲），逐节点比对只会刷屏无信息量的错误。

    Returns:
        dict: {"valid": bool, "errors": list[str], "checked": int}
    """
    from collections import defaultdict

    with get_db(readonly=True) as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT id, parent_id, level FROM sections WHERE project_id = ?",
                (project_id,),
            ).fetchall()
        ]
    if not rows:
        return {"valid": True, "errors": [], "checked": 0}

    children_of: dict[int | None, list[int]] = defaultdict(list)
    level_of: dict[int, int] = {}
    for r in rows:
        children_of[r["parent_id"]].append(r["id"])
        level_of[r["id"]] = r["level"] or 0

    errors: list[str] = []
    expected_depth: dict[int, int] = {}
    seen_bfs: set[int] = set()
    frontier = children_of.get(None, [])
    depth = 1
    while frontier and depth <= _MAX_MANAGED_OUTLINE_DEPTH:
        nxt: list[int] = []
        for sid in frontier:
            if sid in seen_bfs:  # parent_id 成环 → 深度不可信，跳过（结构问题由 validate_outline_integrity 报）
                continue
            seen_bfs.add(sid)
            expected_depth[sid] = depth
            nxt.extend(children_of.get(sid, []))
        frontier = nxt
        depth += 1
    if frontier:
        errors.append(
            f"目录树深度超过可管理上限 {_MAX_MANAGED_OUTLINE_DEPTH}，"
            f"其余 {len(frontier)} 个分支的 level 语义已无定义"
        )
    for sid, exp in expected_depth.items():
        got = level_of.get(sid)
        if got != exp:
            errors.append(
                f"章节 {sid} 的 level={got} 与实际层级深度 {exp} 不一致（编号/导出层级会错位）"
            )
    return {"valid": len(errors) == 0, "errors": errors, "checked": len(expected_depth)}
