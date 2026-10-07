"""行业树服务层

实现功能：
- CRUD 操作（节点增删改查）
- 树结构构建（嵌套渲染）
- AI 自动生成子节点
- 批量导入种子数据
- 搜索与文本匹配
"""

from datetime import UTC, datetime
import json
import logging
import threading
import time

from ..db import get_db, safe_update
from .ai.prompts import get_prompt
from .ai.provider_factory import create_provider, get_ai_config
from .ai.providers.base import AIMessage
from .ai.workflows import collect_json_response


logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────
# 缓存配置
# ──────────────────────────────────────────────────────────────────────
_FULL_TREE_CACHE: dict = {}  # {active_only: {data, timestamp}}
_FULL_TREE_TTL = 60  # 60 秒 TTL
# BUG-FIX-78: 添加线程锁保护缓存，防止并发读写竞态
_FULL_TREE_CACHE_LOCK = threading.Lock()


def _get_cached_full_tree(active_only: bool) -> list[dict]:
    """获取带 TTL 缓存的完整行业树"""
    now = time.time()
    # BUG-FIX-78: 加锁读取缓存，避免并发时读到半更新状态
    with _FULL_TREE_CACHE_LOCK:
        cached = _FULL_TREE_CACHE.get(active_only)
        if cached and (now - cached["timestamp"]) < _FULL_TREE_TTL:
            return cached["data"]
    data = _build_full_tree(active_only)
    # BUG-FIX-78: 加锁写入缓存
    with _FULL_TREE_CACHE_LOCK:
        _FULL_TREE_CACHE[active_only] = {"data": data, "timestamp": now}
    return data


def _invalidate_full_tree_cache() -> None:
    """写入操作后失效缓存"""
    # BUG-FIX-78: 加锁清空缓存
    with _FULL_TREE_CACHE_LOCK:
        _FULL_TREE_CACHE.clear()


def _build_full_tree(active_only: bool = True) -> list[dict]:
    """从数据库构建完整行业树（嵌套结构）"""
    with get_db() as conn:
        sql = "SELECT * FROM industry_tree"
        if active_only:
            sql += " WHERE is_active = 1"
        sql += " ORDER BY order_index, name"
        rows = conn.execute(sql).fetchall()
        nodes = [_row_to_dict(r) for r in rows]

    children_map: dict[str, list[dict]] = {}
    node_map: dict[str, dict] = {}
    for n in nodes:
        n["children"] = []
        node_map[n["id"]] = n
        pid = n.get("parent_id")
        if pid:
            children_map.setdefault(pid, []).append(n)

    for n in nodes:
        n["children"] = children_map.get(n["id"], [])

    return [n for n in nodes if n.get("parent_id") is None]


# ========== 查询操作 ==========


def get_root_nodes(active_only: bool = True) -> list[dict]:
    with get_db() as conn:
        sql = "SELECT * FROM industry_tree WHERE parent_id IS NULL"
        if active_only:
            sql += " AND is_active = 1"
        sql += " ORDER BY order_index, name"
        rows = conn.execute(sql).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_children(parent_id: str, active_only: bool = True) -> list[dict]:
    with get_db() as conn:
        sql = "SELECT * FROM industry_tree WHERE parent_id = ?"
        params = [parent_id]
        if active_only:
            sql += " AND is_active = 1"
        sql += " ORDER BY order_index, name"
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_full_tree(active_only: bool = True) -> list[dict]:
    """获取完整行业树（嵌套结构，带 TTL 缓存）"""
    return _get_cached_full_tree(active_only)


def get_path(node_id: str) -> list[str]:
    """获取节点的完整路径（面包屑）—— 单次 CTE 查询替代 N+1

    使用递归 CTE 一次性获取所有祖先节点，避免逐层查询数据库。
    """
    try:
        with get_db() as conn:
            row = conn.execute(
                """
                WITH RECURSIVE ancestor_path AS (
                    SELECT id, name, parent_id, name AS path_name, 0 AS depth
                    FROM industry_tree WHERE id = ?
                    UNION ALL
                    SELECT t.id, t.name, t.parent_id,
                           a.path_name || ' / ' || t.name, a.depth + 1
                    FROM industry_tree t
                    INNER JOIN ancestor_path a ON t.id = a.parent_id
                )
                SELECT path_name FROM ancestor_path ORDER BY depth DESC LIMIT 1
            """,
                (node_id,),
            ).fetchone()
            if not row:
                return []
            # 将 "A / B / C" 拆分为列表
            return row["path_name"].split(" / ") if row["path_name"] else []
    except Exception:
        # 回退：逐层查询（兼容旧 SQLite 版本）
        path = []
        current_id = node_id
        with get_db() as conn:
            while current_id:
                r = conn.execute(
                    "SELECT id, name, parent_id FROM industry_tree WHERE id = ?",
                    (current_id,),
                ).fetchone()
                if not r:
                    break
                path.insert(0, r["name"])
                current_id = r["parent_id"]
        return path


def get_node_by_id(node_id: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM industry_tree WHERE id = ?", (node_id,)).fetchone()
        if not row:
            return None
        return _row_to_dict(row)


# ========== 写操作 ==========


def _generate_id(name: str, parent_id: str | None, level: int) -> str:
    """生成语义化 ID"""
    name_slug = ""
    for ch in name:
        if "\u4e00" <= ch <= "\u9fff" or ch.isalnum() or ch in "_":
            name_slug += ch
    if not name_slug:
        name_slug = name[:4]
    prefix = f"l{level}_"
    if parent_id:
        prefix = parent_id + "_"
    raw = prefix + name_slug
    # 确保不超过合理长度
    if len(raw) > 60:
        raw = raw[:60]
    # 去重：如果已存在，加后缀
    base = raw
    counter = 1
    while get_node_by_id(raw):
        raw = f"{base}_{counter}"
        counter += 1
    return raw


def create_node(
    name: str,
    parent_id: str | None = None,
    level: int | None = None,
    description: str = "",
    **kwargs,
) -> dict:
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    if level is None and parent_id:
        parent = get_node_by_id(parent_id)
        if parent:
            level = parent["level"] + 1
        else:
            level = 1
    elif level is None:
        level = 1

    node_id = kwargs.get("id") or _generate_id(name, parent_id, level)
    name_en = kwargs.get("name_en", "")
    prompt_hint = kwargs.get("prompt_hint", "")
    icon = kwargs.get("icon", "")
    knowledge_base_ids = json.dumps(kwargs.get("knowledge_base_ids", []), ensure_ascii=False)
    default_scoring_items = json.dumps(kwargs.get("default_scoring_items", []), ensure_ascii=False)
    is_active = 1 if kwargs.get("is_active", True) else 0
    is_ai_generated = 1 if kwargs.get("is_ai_generated", False) else 0
    order_index = kwargs.get("order_index", 0)
    created_by = kwargs.get("created_by", "system")

    with get_db() as conn:
        conn.execute(
            """INSERT INTO industry_tree
               (id, name, name_en, parent_id, level, description, prompt_hint, icon,
                knowledge_base_ids, default_scoring_items, is_active, is_ai_generated,
                order_index, reference_count, created_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)""",
            (
                node_id,
                name,
                name_en,
                parent_id,
                level,
                description,
                prompt_hint,
                icon,
                knowledge_base_ids,
                default_scoring_items,
                is_active,
                is_ai_generated,
                order_index,
                created_by,
                now,
                now,
            ),
        )

    # 记录审计日志
    log_industry_change(node_id, "CREATE", None, {"name": name, "parent_id": parent_id})
    _invalidate_full_tree_cache()
    return get_node_by_id(node_id)


def update_node(node_id: str, data: dict) -> dict | None:
    existing = get_node_by_id(node_id)
    if not existing:
        return None

    updatable_fields = [
        "name",
        "name_en",
        "description",
        "prompt_hint",
        "icon",
        "knowledge_base_ids",
        "default_scoring_items",
        "is_active",
        "order_index",
        "parent_id",
    ]
    updates = {}
    for k in updatable_fields:
        if k in data and data[k] is not None:
            v = data[k]
            if isinstance(v, (list, dict)):
                v = json.dumps(v, ensure_ascii=False)
            elif isinstance(v, bool):
                v = 1 if v else 0
            updates[k] = v

    if not updates:
        return existing

    updates["updated_at"] = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        sql, params = safe_update("industry_tree", updates, "id", node_id)
        conn.execute(sql, params)

    # 记录审计日志
    log_industry_change(node_id, "UPDATE", existing, updates)
    _invalidate_full_tree_cache()
    return get_node_by_id(node_id)


def delete_node(node_id: str, force: bool = False) -> dict:
    """删除节点，检查引用计数和子节点"""
    node = get_node_by_id(node_id)
    if not node:
        return {"success": False, "error": "节点不存在"}

    if not force and node.get("reference_count", 0) > 0:
        return {"success": False, "error": f"节点已被引用 {node['reference_count']} 次，无法删除"}

    children = get_children(node_id, active_only=False)
    if children and not force:
        return {
            "success": False,
            "error": f"节点含有 {len(children)} 个子节点，请先删除子节点或使用强制删除",
        }

    with get_db() as conn:
        if force:
            _delete_tree(conn, node_id)
        else:
            conn.execute("DELETE FROM industry_tree WHERE id = ?", (node_id,))
    return {"success": True}


def _delete_tree(conn, node_id: str, _depth: int = 0):
    """级联删除子树

    BUG-FIX-79: 添加深度限制，防止数据污染导致循环引用时无限递归
    """
    if _depth > 20:  # 行业树最多 3 层，20 层是安全上限
        logger.error("删除子树深度超限（%d），可能存在循环引用: node_id=%s", _depth, node_id)
        return
    children = conn.execute(
        "SELECT id FROM industry_tree WHERE parent_id = ?",
        (node_id,),
    ).fetchall()
    for child in children:
        _delete_tree(conn, child["id"], _depth + 1)
    conn.execute("DELETE FROM industry_tree WHERE id = ?", (node_id,))
    _invalidate_full_tree_cache()


# ========== AI 生成 ==========


async def generate_children(parent_id: str, count: int = 5) -> list[dict]:
    """调用 AI 为指定父节点生成子节点"""
    # BUG-FIX-84: 校验 count 参数，防止负数或过大值
    try:
        count = int(count)
    except (TypeError, ValueError):
        count = 5
    if count < 1:
        count = 1
    elif count > 20:
        count = 20

    parent = get_node_by_id(parent_id)
    if not parent:
        raise ValueError(f"父节点 {parent_id} 不存在")

    if parent["level"] >= 3:
        return []

    ai_config = get_ai_config()
    provider = create_provider(ai_config)
    # P2 修复：原未通过 kwargs 传参，后续手动 .replace()，触发 WARNING 日志
    user_message = get_prompt(
        "INDUSTRY_GENERATE_CHILDREN_SYSTEM",
        COUNT=str(count),
        PARENT_NAME=parent["name"],
        PARENT_LEVEL=str(parent["level"]),
        PARENT_DESCRIPTION=parent.get("description", ""),
    )

    messages = [
        AIMessage(role="system", content="你是专业的工程项目分类专家。"),
        AIMessage(role="user", content=user_message),
    ]

    result = await collect_json_response(
        provider=provider,
        messages=messages,
        max_retries=2,
    )

    if not isinstance(result, list):
        raise ValueError(f"AI 返回格式错误：期望数组，得到 {type(result)}")

    created_nodes = []
    for i, item in enumerate(result):
        name = item.get("name", "")
        desc = item.get("description", "")
        if not name:
            continue
        node = create_node(
            name=name,
            parent_id=parent_id,
            description=desc,
            is_ai_generated=True,
            order_index=i,
        )
        created_nodes.append(node)

    return created_nodes


# ========== 批量操作 ==========


def batch_import(nodes: list[dict]) -> int:
    """批量导入行业树数据

    BUG-FIX-86: 记录失败节点详情到日志，便于排查导入失败原因

    Returns:
        int: 成功导入的节点数
    """
    count = 0
    failed: list[dict] = []
    for n in nodes:
        try:
            create_node(**n)
            count += 1
        except Exception as e:
            logger.error("导入节点失败 %s: %s", n.get("name", ""), e)
            failed.append({"name": n.get("name", ""), "error": str(e)})
    if failed:
        logger.warning("批量导入: 成功 %d, 失败 %d: %s", count, len(failed), [f["name"] for f in failed])
    return count


def reorder_nodes(parent_id: str, ordered_ids: list[str]) -> bool:
    """调整排序"""
    with get_db() as conn:
        for idx, node_id in enumerate(ordered_ids):
            conn.execute(
                "UPDATE industry_tree SET order_index = ? WHERE id = ? AND (parent_id = ? OR (parent_id IS NULL AND ? IS NULL))",
                (idx, node_id, parent_id, parent_id),
            )
    _invalidate_full_tree_cache()
    return True


# ========== 引用管理 ==========


def increment_reference(node_id: str):
    with get_db() as conn:
        conn.execute(
            "UPDATE industry_tree SET reference_count = reference_count + 1 WHERE id = ?",
            (node_id,),
        )


def decrement_reference(node_id: str):
    with get_db() as conn:
        conn.execute(
            "UPDATE industry_tree SET reference_count = MAX(0, reference_count - 1) WHERE id = ?",
            (node_id,),
        )


# ========== 搜索与匹配 ==========


def search_nodes(keyword: str, level: int | None = None, limit: int = 50) -> list[dict]:
    """全文搜索节点"""
    # BUG-FIX-85: 转义 LIKE 通配符 % 和 _，防止关键词中的特殊字符被误认为通配符
    escaped_kw = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    with get_db() as conn:
        sql = "SELECT * FROM industry_tree WHERE (name LIKE ? ESCAPE '\\' OR name_en LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\')"
        params = [f"%{escaped_kw}%", f"%{escaped_kw}%", f"%{escaped_kw}%"]
        if level is not None:
            sql += " AND level = ?"
            params.append(level)
        sql += " AND is_active = 1 ORDER BY level, order_index LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
        return [_row_to_dict(r) for r in rows]


def match_industry_by_text(text: str) -> list[dict] | None:
    """根据文本匹配最合适的行业节点（关键词匹配）"""
    if not text:
        return None

    # 获取所有叶子节点
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM industry_tree WHERE level = 3 AND is_active = 1",
        ).fetchall()
        leaf_nodes = [_row_to_dict(r) for r in rows]

    # 简单关键词匹配（后续可用 AI 增强）
    text_lower = text.lower()
    scored = []
    for node in leaf_nodes:
        score = 0
        name = node["name"]
        desc = node.get("description") or ""
        if name and name.lower() in text_lower:
            score += 0.5
        if desc and any(kw.lower() in text_lower for kw in desc.split()):
            score += 0.3
        if score > 0:
            scored.append({"node_id": node["id"], "node_name": name, "confidence": round(score, 2)})

    scored.sort(key=lambda x: x["confidence"], reverse=True)
    return scored[:5] if scored else None


# ========== 种子数据 ==========

SEED_DATA: list[dict] = [
    # === 一级：建筑工程 ===
    {
        "id": "construct",
        "name": "建筑工程",
        "level": 1,
        "description": "各类建筑工程建设",
        "order_index": 0,
    },
    {
        "id": "construct_building",
        "name": "房建工程",
        "level": 2,
        "parent_id": "construct",
        "description": "房屋建筑工程",
        "icon": "home",
        "order_index": 0,
    },
    {
        "id": "construct_building_residential",
        "name": "住宅建筑",
        "level": 3,
        "parent_id": "construct_building",
        "description": "各类住宅建筑的新建与修缮",
        "order_index": 0,
        "default_scoring_items": [
            {"name": "施工组织设计", "max_score": 20, "weight": 0.15},
            {"name": "质量保证措施", "max_score": 15, "weight": 0.12},
            {"name": "安全生产措施", "max_score": 10, "weight": 0.08},
            {"name": "工期保证措施", "max_score": 10, "weight": 0.08},
            {"name": "文明施工措施", "max_score": 5, "weight": 0.05},
        ],
    },
    {
        "id": "construct_building_commercial",
        "name": "商业建筑",
        "level": 3,
        "parent_id": "construct_building",
        "description": "商场、写字楼等商业建筑",
        "order_index": 1,
        "default_scoring_items": [
            {"name": "施工组织设计", "max_score": 20, "weight": 0.15},
            {"name": "质量保证措施", "max_score": 15, "weight": 0.12},
            {"name": "安全生产措施", "max_score": 10, "weight": 0.08},
            {"name": "工期保证措施", "max_score": 10, "weight": 0.08},
        ],
    },
    {
        "id": "construct_building_industrial",
        "name": "工业建筑",
        "level": 3,
        "parent_id": "construct_building",
        "description": "厂房、仓库等工业建筑",
        "order_index": 2,
    },
    {
        "id": "construct_building_public",
        "name": "公共建筑",
        "level": 3,
        "parent_id": "construct_building",
        "description": "学校、医院、体育馆等公共建筑",
        "order_index": 3,
    },
    {
        "id": "construct_municipal",
        "name": "市政公用工程",
        "level": 2,
        "parent_id": "construct",
        "description": "市政基础设施建设",
        "order_index": 1,
    },
    {
        "id": "construct_municipal_road",
        "name": "城市道路工程",
        "level": 3,
        "parent_id": "construct_municipal",
        "description": "城市道路新建与改造",
        "order_index": 0,
        "default_scoring_items": [
            {"name": "施工组织设计", "max_score": 20, "weight": 0.15},
            {"name": "质量保证措施", "max_score": 15, "weight": 0.12},
            {"name": "交通组织方案", "max_score": 10, "weight": 0.08},
            {"name": "工期保证措施", "max_score": 10, "weight": 0.08},
        ],
    },
    {
        "id": "construct_municipal_bridge",
        "name": "城市桥梁工程",
        "level": 3,
        "parent_id": "construct_municipal",
        "description": "城市桥梁建设与维护",
        "order_index": 1,
        "default_scoring_items": [
            {"name": "施工组织设计", "max_score": 20, "weight": 0.15},
            {"name": "质量保证措施", "max_score": 15, "weight": 0.12},
            {"name": "结构安全措施", "max_score": 15, "weight": 0.12},
            {"name": "工期保证措施", "max_score": 10, "weight": 0.08},
        ],
    },
    {
        "id": "construct_municipal_pipeline",
        "name": "给排水管网工程",
        "level": 3,
        "parent_id": "construct_municipal",
        "description": "城市给水排水管网系统",
        "order_index": 2,
    },
    {
        "id": "construct_municipal_greening",
        "name": "市政园林绿化",
        "level": 3,
        "parent_id": "construct_municipal",
        "description": "市政园林绿化工程",
        "order_index": 3,
    },
    {
        "id": "construct_decoration",
        "name": "建筑装饰装修",
        "level": 2,
        "parent_id": "construct",
        "description": "建筑装饰装修工程",
        "icon": "skin",
        "order_index": 2,
    },
    {
        "id": "construct_decoration_interior",
        "name": "室内装饰装修",
        "level": 3,
        "parent_id": "construct_decoration",
        "description": "室内精装修工程",
        "order_index": 0,
        "default_scoring_items": [
            {"name": "施工组织设计", "max_score": 20, "weight": 0.15},
            {"name": "质量保证措施", "max_score": 15, "weight": 0.12},
            {"name": "环保措施", "max_score": 10, "weight": 0.08},
            {"name": "工期保证措施", "max_score": 10, "weight": 0.08},
        ],
    },
    {
        "id": "construct_decoration_curtain",
        "name": "建筑幕墙工程",
        "level": 3,
        "parent_id": "construct_decoration",
        "description": "玻璃幕墙、石材幕墙等",
        "order_index": 1,
    },
    {
        "id": "construct_decoration_exterior",
        "name": "外墙整修与翻新",
        "level": 3,
        "parent_id": "construct_decoration",
        "description": "外墙整修翻新工程",
        "order_index": 2,
    },
    {
        "id": "construct_renovation",
        "name": "建筑修缮改造",
        "level": 2,
        "parent_id": "construct",
        "description": "既有建筑修缮改造",
        "order_index": 3,
    },
    {
        "id": "construct_renovation_roof",
        "name": "屋面防水修缮",
        "level": 3,
        "parent_id": "construct_renovation",
        "description": "屋面防水修缮工程",
        "order_index": 0,
        "default_scoring_items": [
            {"name": "施工组织设计", "max_score": 20, "weight": 0.15},
            {"name": "防水质量保证", "max_score": 20, "weight": 0.15},
            {"name": "安全措施", "max_score": 10, "weight": 0.08},
            {"name": "工期保证措施", "max_score": 10, "weight": 0.08},
        ],
    },
    {
        "id": "construct_renovation_structure",
        "name": "结构加固改造",
        "level": 3,
        "parent_id": "construct_renovation",
        "description": "建筑结构加固改造",
        "order_index": 1,
    },
    {
        "id": "construct_renovation_energy",
        "name": "既有建筑节能改造",
        "level": 3,
        "parent_id": "construct_renovation",
        "description": "建筑节能改造工程",
        "order_index": 2,
    },
    # === 一级：专项工程 ===
    {
        "id": "specialty",
        "name": "专项工程",
        "level": 1,
        "description": "各类专项工程",
        "order_index": 1,
    },
    {
        "id": "specialty_fire",
        "name": "消防设施工程",
        "level": 2,
        "parent_id": "specialty",
        "description": "消防设施工程",
        "icon": "fire",
        "order_index": 0,
    },
    {
        "id": "specialty_fire_alarm",
        "name": "消防报警系统",
        "level": 3,
        "parent_id": "specialty_fire",
        "description": "火灾自动报警系统",
        "order_index": 0,
        "default_scoring_items": [
            {"name": "施工组织设计", "max_score": 20, "weight": 0.15},
            {"name": "质量保证措施", "max_score": 15, "weight": 0.12},
            {"name": "系统调试方案", "max_score": 15, "weight": 0.12},
            {"name": "工期保证措施", "max_score": 10, "weight": 0.08},
        ],
    },
    {
        "id": "specialty_fire_sprinkler",
        "name": "自动喷水灭火系统",
        "level": 3,
        "parent_id": "specialty_fire",
        "description": "自动喷水灭火系统",
        "order_index": 1,
    },
    {
        "id": "specialty_fire_gas",
        "name": "气体灭火系统",
        "level": 3,
        "parent_id": "specialty_fire",
        "description": "气体灭火系统",
        "order_index": 2,
    },
    {
        "id": "specialty_fire_smoke",
        "name": "防排烟系统",
        "level": 3,
        "parent_id": "specialty_fire",
        "description": "防排烟系统",
        "order_index": 3,
    },
    {
        "id": "specialty_intelligent",
        "name": "电子与智能化工程",
        "level": 2,
        "parent_id": "specialty",
        "description": "电子与智能化工程",
        "order_index": 1,
    },
    {
        "id": "specialty_intelligent_building",
        "name": "建筑智能化系统",
        "level": 3,
        "parent_id": "specialty_intelligent",
        "description": "建筑智能化系统集成",
        "order_index": 0,
        "default_scoring_items": [
            {"name": "施工组织设计", "max_score": 20, "weight": 0.15},
            {"name": "质量保证措施", "max_score": 15, "weight": 0.12},
            {"name": "系统集成方案", "max_score": 15, "weight": 0.12},
            {"name": "工期保证措施", "max_score": 10, "weight": 0.08},
        ],
    },
    {
        "id": "specialty_intelligent_security",
        "name": "安防监控系统",
        "level": 3,
        "parent_id": "specialty_intelligent",
        "description": "安防监控系统",
        "order_index": 1,
    },
    {
        "id": "specialty_intelligent_cabling",
        "name": "综合布线系统",
        "level": 3,
        "parent_id": "specialty_intelligent",
        "description": "综合布线系统",
        "order_index": 2,
    },
    {
        "id": "specialty_intelligent_smart",
        "name": "智慧工地管理系统",
        "level": 3,
        "parent_id": "specialty_intelligent",
        "description": "智慧工地管理系统",
        "order_index": 3,
    },
    {
        "id": "specialty_electric",
        "name": "电力工程",
        "level": 2,
        "parent_id": "specialty",
        "description": "电力工程",
        "icon": "thunderbolt",
        "order_index": 2,
    },
    {
        "id": "specialty_electric_transmission",
        "name": "输变电工程",
        "level": 3,
        "parent_id": "specialty_electric",
        "description": "输变电工程",
        "order_index": 0,
        "default_scoring_items": [
            {"name": "施工组织设计", "max_score": 20, "weight": 0.15},
            {"name": "质量保证措施", "max_score": 15, "weight": 0.12},
            {"name": "安全措施", "max_score": 15, "weight": 0.12},
            {"name": "工期保证措施", "max_score": 10, "weight": 0.08},
        ],
    },
    {
        "id": "specialty_electric_distribution",
        "name": "配电工程",
        "level": 3,
        "parent_id": "specialty_electric",
        "description": "配电工程",
        "order_index": 1,
    },
    {
        "id": "specialty_electric_newenergy",
        "name": "新能源发电工程",
        "level": 3,
        "parent_id": "specialty_electric",
        "description": "新能源发电工程",
        "order_index": 2,
    },
    {
        "id": "specialty_mechanical",
        "name": "建筑机电安装工程",
        "level": 2,
        "parent_id": "specialty",
        "description": "建筑机电安装",
        "order_index": 3,
    },
    {
        "id": "specialty_mechanical_hvac",
        "name": "暖通空调安装",
        "level": 3,
        "parent_id": "specialty_mechanical",
        "description": "暖通空调系统安装",
        "order_index": 0,
    },
    {
        "id": "specialty_mechanical_plumbing",
        "name": "给排水及消防安装",
        "level": 3,
        "parent_id": "specialty_mechanical",
        "description": "给排水及消防安装",
        "order_index": 1,
    },
    {
        "id": "specialty_mechanical_electrical",
        "name": "电气设备安装",
        "level": 3,
        "parent_id": "specialty_mechanical",
        "description": "电气设备安装",
        "order_index": 2,
    },
    {
        "id": "specialty_waterproof",
        "name": "防水防腐保温工程",
        "level": 2,
        "parent_id": "specialty",
        "description": "防水防腐保温",
        "order_index": 4,
    },
    {
        "id": "specialty_waterproof_building",
        "name": "建筑防水工程",
        "level": 3,
        "parent_id": "specialty_waterproof",
        "description": "建筑防水工程",
        "order_index": 0,
    },
    {
        "id": "specialty_waterproof_anticorrosion",
        "name": "防腐保温工程",
        "level": 3,
        "parent_id": "specialty_waterproof",
        "description": "防腐保温工程",
        "order_index": 1,
    },
    {
        "id": "specialty_waterproof_roof",
        "name": "屋面防水工程",
        "level": 3,
        "parent_id": "specialty_waterproof",
        "description": "屋面防水工程",
        "order_index": 2,
    },
    {
        "id": "specialty_elevator",
        "name": "电梯工程",
        "level": 2,
        "parent_id": "specialty",
        "description": "电梯工程",
        "order_index": 5,
    },
    {
        "id": "specialty_elevator_install",
        "name": "电梯安装工程",
        "level": 3,
        "parent_id": "specialty_elevator",
        "description": "电梯安装工程",
        "order_index": 0,
    },
    {
        "id": "specialty_elevator_replace",
        "name": "电梯以旧换新改造",
        "level": 3,
        "parent_id": "specialty_elevator",
        "description": "电梯以旧换新改造",
        "order_index": 1,
    },
    {
        "id": "specialty_elevator_maintain",
        "name": "电梯维保服务",
        "level": 3,
        "parent_id": "specialty_elevator",
        "description": "电梯维保服务",
        "order_index": 2,
    },
    {
        "id": "specialty_tech",
        "name": "通用技术服务",
        "level": 2,
        "parent_id": "specialty",
        "description": "通用技术服务",
        "order_index": 6,
    },
    {
        "id": "specialty_tech_it",
        "name": "信息技术服务",
        "level": 3,
        "parent_id": "specialty_tech",
        "description": "信息技术服务",
        "order_index": 0,
    },
    {
        "id": "specialty_tech_consult",
        "name": "技术咨询服务",
        "level": 3,
        "parent_id": "specialty_tech",
        "description": "技术咨询服务",
        "order_index": 1,
    },
    {
        "id": "specialty_tech_inspect",
        "name": "检测检验服务",
        "level": 3,
        "parent_id": "specialty_tech",
        "description": "检测检验服务",
        "order_index": 2,
    },
    {
        "id": "specialty_equipment",
        "name": "设备采购安装",
        "level": 2,
        "parent_id": "specialty",
        "description": "设备采购安装",
        "order_index": 7,
    },
    {
        "id": "specialty_equipment_large",
        "name": "大型设备采购安装",
        "level": 3,
        "parent_id": "specialty_equipment",
        "description": "大型设备采购安装",
        "order_index": 0,
    },
    {
        "id": "specialty_equipment_special",
        "name": "特种设备安装",
        "level": 3,
        "parent_id": "specialty_equipment",
        "description": "特种设备安装",
        "order_index": 1,
    },
    {
        "id": "specialty_equipment_lab",
        "name": "实验室设备安装",
        "level": 3,
        "parent_id": "specialty_equipment",
        "description": "实验室设备安装",
        "order_index": 2,
    },
    {
        "id": "specialty_advertising",
        "name": "广告及标识工程",
        "level": 2,
        "parent_id": "specialty",
        "description": "广告及标识工程",
        "order_index": 8,
    },
    {
        "id": "specialty_advertising_outdoor",
        "name": "户外广告工程",
        "level": 3,
        "parent_id": "specialty_advertising",
        "description": "户外广告工程",
        "order_index": 0,
    },
    {
        "id": "specialty_advertising_indoor",
        "name": "室内标识系统",
        "level": 3,
        "parent_id": "specialty_advertising",
        "description": "室内标识系统",
        "order_index": 1,
    },
    {
        "id": "specialty_advertising_light",
        "name": "灯光亮化工程",
        "level": 3,
        "parent_id": "specialty_advertising",
        "description": "灯光亮化工程",
        "order_index": 2,
    },
    # === 一级：基础设施与运维 ===
    {
        "id": "infrastructure",
        "name": "基础设施与运维",
        "level": 1,
        "description": "基础设施与运维服务",
        "order_index": 2,
    },
    {
        "id": "infra_highway",
        "name": "公路工程",
        "level": 2,
        "parent_id": "infrastructure",
        "description": "公路工程",
        "icon": "car",
        "order_index": 0,
    },
    {
        "id": "infra_highway_road",
        "name": "公路路基路面工程",
        "level": 3,
        "parent_id": "infra_highway",
        "description": "公路路基路面工程",
        "order_index": 0,
    },
    {
        "id": "infra_highway_bridge",
        "name": "桥梁隧道工程",
        "level": 3,
        "parent_id": "infra_highway",
        "description": "桥梁隧道工程",
        "order_index": 1,
    },
    {
        "id": "infra_highway_safety",
        "name": "交通安全设施",
        "level": 3,
        "parent_id": "infra_highway",
        "description": "交通安全设施",
        "order_index": 2,
    },
    {
        "id": "infra_water",
        "name": "水运工程",
        "level": 2,
        "parent_id": "infrastructure",
        "description": "水运工程",
        "order_index": 1,
    },
    {
        "id": "infra_water_port",
        "name": "港口与航道工程",
        "level": 3,
        "parent_id": "infra_water",
        "description": "港口与航道工程",
        "order_index": 0,
    },
    {
        "id": "infra_water_testing",
        "name": "水运试验检测",
        "level": 3,
        "parent_id": "infra_water",
        "description": "公路水运试验检测",
        "order_index": 1,
    },
    {
        "id": "infra_petrochem",
        "name": "石油化工工程",
        "level": 2,
        "parent_id": "infrastructure",
        "description": "石油化工工程",
        "order_index": 2,
    },
    {
        "id": "infra_petrochem_equip",
        "name": "石油化工设备安装",
        "level": 3,
        "parent_id": "infra_petrochem",
        "description": "石油化工设备安装",
        "order_index": 0,
    },
    {
        "id": "infra_petrochem_pipe",
        "name": "化工管道工程",
        "level": 3,
        "parent_id": "infra_petrochem",
        "description": "化工管道工程",
        "order_index": 1,
    },
    {
        "id": "infra_petrochem_safety",
        "name": "防爆与安全工程",
        "level": 3,
        "parent_id": "infra_petrochem",
        "description": "防爆与安全工程",
        "order_index": 2,
    },
    {
        "id": "infra_property",
        "name": "物业管理与运维",
        "level": 2,
        "parent_id": "infrastructure",
        "description": "物业管理与运维服务",
        "icon": "build",
        "order_index": 3,
    },
    {
        "id": "infra_property_service",
        "name": "物业管理服务",
        "level": 3,
        "parent_id": "infra_property",
        "description": "物业管理服务",
        "order_index": 0,
    },
    {
        "id": "infra_property_equipment",
        "name": "设施设备运维",
        "level": 3,
        "parent_id": "infra_property",
        "description": "设施设备运维服务",
        "order_index": 1,
    },
    {
        "id": "infra_property_repair",
        "name": "专项维修服务",
        "level": 3,
        "parent_id": "infra_property",
        "description": "专项维修服务",
        "order_index": 2,
    },
]


def seed_industry_tree():
    """初始化种子数据"""
    with get_db() as conn:
        existing = conn.execute("SELECT id FROM industry_tree LIMIT 1").fetchone()
        if existing:
            return
        for node in SEED_DATA:
            now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
            knowledge_base_ids = json.dumps(node.get("knowledge_base_ids", []), ensure_ascii=False)
            default_scoring_items = json.dumps(
                node.get("default_scoring_items", []),
                ensure_ascii=False,
            )
            conn.execute(
                """INSERT INTO industry_tree
                   (id, name, name_en, parent_id, level, description, prompt_hint, icon,
                    knowledge_base_ids, default_scoring_items, is_active, is_ai_generated,
                    order_index, reference_count, created_by, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, 0, 'system', ?, ?)""",
                (
                    node["id"],
                    node["name"],
                    node.get("name_en"),
                    node.get("parent_id"),
                    node["level"],
                    node.get("description"),
                    node.get("prompt_hint"),
                    node.get("icon"),
                    knowledge_base_ids,
                    default_scoring_items,
                    node.get("order_index", 0),
                    now,
                    now,
                ),
            )


def get_stats() -> dict:
    """统计信息"""
    with get_db() as conn:
        total = conn.execute("SELECT COUNT(*) FROM industry_tree").fetchone()[0]
        active = conn.execute("SELECT COUNT(*) FROM industry_tree WHERE is_active = 1").fetchone()[
            0
        ]
        ai_gen = conn.execute(
            "SELECT COUNT(*) FROM industry_tree WHERE is_ai_generated = 1",
        ).fetchone()[0]
        roots = conn.execute(
            "SELECT COUNT(*) FROM industry_tree WHERE parent_id IS NULL AND is_active = 1",
        ).fetchone()[0]
        leafs = conn.execute(
            "SELECT COUNT(*) FROM industry_tree WHERE is_active = 1 AND id NOT IN (SELECT DISTINCT parent_id FROM industry_tree WHERE parent_id IS NOT NULL AND is_active = 1)",
        ).fetchone()[0]
        latest = conn.execute("SELECT MAX(updated_at) FROM industry_tree").fetchone()[0] or ""
        return {
            "total": total,
            "active": active,
            "ai_generated": ai_gen,
            "root_count": roots,
            "leaf_count": leafs,
            "last_updated": latest,
        }


def link_knowledge_to_industry(doc_id: int, industry_id: str) -> bool:
    """将知识库文档关联到行业树节点（双向关联）"""
    # 先在事务外处理节点侧更新：get_node_by_id / update_node 内部各自获取
    # 数据库连接，若嵌套在 get_db() 事务内调用会重复获取非重入写锁导致死锁。
    node = get_node_by_id(industry_id)
    if node:
        try:
            kb_ids = json.loads(node.get("knowledge_base_ids", "[]") or "[]")
            if not isinstance(kb_ids, list):
                kb_ids = []
        except (json.JSONDecodeError, TypeError):
            kb_ids = []
        if str(doc_id) not in kb_ids:
            kb_ids.append(str(doc_id))
            update_node(industry_id, {"knowledge_base_ids": kb_ids})
    with get_db() as conn:
        # 更新知识库文档的行业树关联
        conn.execute(
            "UPDATE knowledge_documents SET industry_tree_id = ? WHERE id = ?",
            (industry_id, doc_id),
        )
    return True


def get_industry_knowledge_map() -> dict:
    """获取行业树节点 → 知识库文档的映射"""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT industry_tree_id, GROUP_CONCAT(id) as doc_ids
               FROM knowledge_documents
               WHERE industry_tree_id IS NOT NULL AND industry_tree_id != ''
               GROUP BY industry_tree_id""",
        ).fetchall()
        return {r["industry_tree_id"]: r["doc_ids"].split(",") for r in rows if r["doc_ids"]}


def get_industry_references(node_id: str) -> list[dict]:
    """获取引用该行业节点的项目列表"""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, name, industry, created_at
               FROM projects
               WHERE industry_id = ?
               ORDER BY created_at DESC""",
            (node_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_all_leaf_nodes() -> list[dict]:
    """获取所有叶子节点（用于 AI 匹配）"""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT * FROM industry_tree
               WHERE is_active = 1
               AND id NOT IN (SELECT DISTINCT parent_id FROM industry_tree WHERE parent_id IS NOT NULL AND is_active = 1)""",
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def get_default_scoring_items(industry_id: str) -> list:
    """获取行业的默认评分项"""
    node = get_node_by_id(industry_id)
    if not node:
        return []
    scoring_items = node.get("default_scoring_items")
    if isinstance(scoring_items, str):
        try:
            return json.loads(scoring_items)
        except (json.JSONDecodeError, TypeError):
            return []
    elif isinstance(scoring_items, list):
        return scoring_items
    return []


# ========== AI 语义匹配 ==========


async def match_industry_by_ai(text: str, top_k: int = 5) -> list[dict]:
    """使用 AI 进行语义匹配"""
    if not text:
        return []

    try:
        ai_config = get_ai_config()
        provider = create_provider(ai_config)

        # 获取所有叶子节点摘要
        leaf_nodes = get_all_leaf_nodes()
        if not leaf_nodes:
            return []

        tree_summary = "\n".join(
            [f"- {n['id']}: {n['name']} - {n.get('description', '')}" for n in leaf_nodes[:50]],
        )

        user_prompt = f"""
请根据以下项目描述，从行业树中匹配最合适的{top_k}个行业节点。

项目描述：{text}

行业树摘要（叶子节点）：
{tree_summary}

请按置信度排序返回 JSON 数组：
[
  {{"node_id": "节点ID", "node_name": "节点名称", "confidence": 0.95, "reason": "匹配原因"}},
  ...
]

要求：
1. confidence 范围 0-1，表示匹配程度
2. 至少返回 1 个结果，最多返回 {top_k} 个
3. 如果没有合适匹配，返回空数组 []
"""

        messages = [
            AIMessage(
                role="system",
                content="你是专业的工程项目分类专家，擅长从项目描述中识别所属行业。",
            ),
            AIMessage(role="user", content=user_prompt),
        ]

        result = await collect_json_response(provider=provider, messages=messages, max_retries=2)

        if isinstance(result, list):
            return result[:top_k]
        return []
    except Exception as e:
        logger.error("AI 匹配失败: %s", e)
        return []


# ========== 审计日志 ==========


def log_industry_change(
    node_id: str, action: str, old_value: dict | None = None, new_value: dict | None = None
):
    """记录行业树变更日志"""
    with get_db() as conn:
        conn.execute(
            """INSERT INTO industry_tree_audit (node_id, action, old_value, new_value, changed_by, changed_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                node_id,
                action,
                json.dumps(old_value or {}),
                json.dumps(new_value or {}),
                "system",
                datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )


def get_audit_logs(node_id: str | None = None, limit: int = 50) -> list[dict]:
    """获取审计日志"""
    with get_db() as conn:
        if node_id:
            rows = conn.execute(
                """SELECT * FROM industry_tree_audit WHERE node_id = ? ORDER BY changed_at DESC LIMIT ?""",
                (node_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM industry_tree_audit ORDER BY changed_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


# ========== 版本管理 ==========


def create_version(changelog: str = "") -> int:
    """创建行业树版本快照"""
    # 先在事务外读取树快照：get_full_tree 内部（缓存未命中时）会调用
    # _build_full_tree → get_db()。若在下方 get_db() 事务内调用，
    # 会嵌套获取非重入的全局写锁（_db_write_lock）导致死锁（60s 超时）。
    tree_snapshot = json.dumps(get_full_tree())
    with get_db() as conn:
        # BUG-FIX-80: 使用 COALESCE(MAX(...), 0) + 1 在单条 SQL 中原子递增，
        # 避免并发调用读到相同 version_number 后插入重复值
        row = conn.execute(
            "SELECT COALESCE(MAX(version_number), 0) + 1 as next_version FROM industry_tree_versions"
        ).fetchone()
        version_num = row["next_version"]

        # 保存快照
        conn.execute(
            """INSERT INTO industry_tree_versions (version_number, snapshot, changelog, created_by, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                version_num,
                tree_snapshot,
                changelog,
                "system",
                datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
        return version_num


def rollback_to_version(version_num: int) -> bool:
    """回滚到指定版本"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT snapshot FROM industry_tree_versions WHERE version_number = ?",
            (version_num,),
        ).fetchone()
        if not row:
            return False

        # 清空当前数据
        conn.execute("DELETE FROM industry_tree")

        # 恢复快照
        # 快照为嵌套树结构（get_full_tree 返回值），需前序遍历展开所有层级，
        # 且父节点先于子节点插入以满足 parent_id 外键约束
        tree_snapshot = json.loads(row["snapshot"])

        def _iter_nodes(nodes):
            for n in nodes:
                yield n
                yield from _iter_nodes(n.get("children", []) or [])

        for node in _iter_nodes(tree_snapshot):
            conn.execute(
                """INSERT INTO industry_tree (id, name, name_en, parent_id, level, description, prompt_hint, icon,
                   knowledge_base_ids, default_scoring_items, is_active, is_ai_generated, order_index, reference_count,
                   created_by, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, 0, 'system', ?, ?)""",
                (
                    node["id"],
                    node["name"],
                    node.get("name_en"),
                    node.get("parent_id"),
                    node["level"],
                    node.get("description", ""),
                    node.get("prompt_hint", ""),
                    node.get("icon", ""),
                    json.dumps(node.get("knowledge_base_ids", [])),
                    json.dumps(node.get("default_scoring_items", [])),
                    node.get("order_index", 0),
                    node.get("created_at"),
                    node.get("updated_at"),
                ),
            )
        _invalidate_full_tree_cache()
        # BUG-FIX-81: 记录回滚操作到审计日志，使回滚本身可审计
        log_industry_change(
            "ALL",
            f"ROLLBACK_TO_V{version_num}",
            None,
            {"version_num": version_num, "node_count": sum(1 for _ in _iter_nodes(tree_snapshot))},
        )
        return True


def get_versions() -> list[dict]:
    """获取所有版本列表"""
    with get_db() as conn:
        rows = conn.execute(
            """SELECT version_number, changelog, created_by, created_at
               FROM industry_tree_versions
               ORDER BY version_number DESC""",
        ).fetchall()
        return [dict(r) for r in rows]


def _row_to_dict(row) -> dict:
    d = dict(row)
    for k in ("knowledge_base_ids", "default_scoring_items"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except json.JSONDecodeError:
                d[k] = []
        else:
            d[k] = []
    for k in ("is_active", "is_ai_generated"):
        if k in d:
            d[k] = bool(d[k])
    return d
