# -*- coding: utf-8 -*-
"""可行性研究报告工作区存储（ENHANCE 2026-09-15，移植自易标 feasibilityReportStore.cjs）。

设计要点：
- **单工作区**（meta 恒为 id=1）：可研是「一份报告一份工作区」的文档型流程，
  与废标/查重的多任务并行模型不同；
- **六步工作流**：materials（项目资料）→ analysis（资料分析）→ outline（报告目录）
  → parameters（关键参数）→ content（正文生成）；
- **下游失效**（易标 clearDownstream* 的移植）：上游改动必须使下游产物失效，
  否则旧分析/旧目录/旧正文会带着已过时的事实继续存活；
- **目录节点表**：三级目录平铺存 `feasibility_outline_nodes`（node_id 形如 "1.2.3"），
  正文逐叶生成后按节点回写，支持暂停后续跑与人工单章编辑。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import FEASIBILITY_REPORTS_DIR
from ..db import get_db

logger = logging.getLogger(__name__)

STEPS = ("materials", "analysis", "outline", "parameters", "content")
PROJECT_TYPES = ("government", "enterprise")
TASK_TYPES = (
    "feasibility-analysis",
    "feasibility-outline",
    "feasibility-parameters",
    "feasibility-content",
)
TASK_TYPE_LABELS = {
    "feasibility-analysis": "资料分析",
    "feasibility-outline": "报告目录",
    "feasibility-parameters": "关键参数",
    "feasibility-content": "正文生成",
}
OUTLINE_SAVE_REASONS = ("sort", "edit", "delete", "add-root", "add-child", "replace")
DEFAULT_PROJECT_INFO: dict[str, str] = {
    "projectName": "",
    "projectType": "government",
    "industry": "",
    "constructionUnit": "",
    "location": "",
    "constructionContent": "",
    "constructionPeriodYears": "2",
    "operationPeriodYears": "20",
    "totalInvestment": "",
    "fundingSource": "",
}
DEFAULT_TARGET_WORDS = 30000
#: 目录层级上限（易标同样最多三级）
MAX_OUTLINE_LEVEL = 3
#: 一级 / 每层子节点数量上限（防 AI 输出爆炸式目录）
MAX_ROOT_CHILDREN = 16
MAX_CHILDREN = 20
#: 参考知识库文档数量上限（防前端传入超长 id 列表）
MAX_REFERENCE_DOCUMENTS = 50
#: 单章节精确指定的知识条目数上限
MAX_NODE_KNOWLEDGE_ITEMS = 30


class TaskRunningError(RuntimeError):
    """有后台任务在跑时禁止改写目录/正文（路由层映射为 409）。

    与易标 `assertContentUnlocked` 同语义：任务运行中并发编辑会让 checkpoint
    写回旧大纲，导致人工改动被静默覆盖。
    """


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S")


def _safe_json(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return fallback


# ===== 归一化 =====


def normalize_step(value: Any) -> str:
    return value if value in STEPS else "materials"


def normalize_project_info(value: Any) -> dict[str, str]:
    source = value if isinstance(value, dict) else {}
    info = dict(DEFAULT_PROJECT_INFO)
    for key in info:
        if key == "projectType":
            info[key] = source.get(key) if source.get(key) in PROJECT_TYPES else "government"
        else:
            info[key] = str(source.get(key) or "").strip()
    info["constructionPeriodYears"] = info["constructionPeriodYears"] or "2"
    info["operationPeriodYears"] = info["operationPeriodYears"] or "20"
    return info


def normalize_target_words(value: Any) -> int:
    words = round(float(value) if value else 0)
    return words if words >= 1000 else DEFAULT_TARGET_WORDS


def normalize_save_reason(value: Any) -> str:
    return value if value in OUTLINE_SAVE_REASONS else "replace"


def _normalize_positive_ids(value: Any, cap: int) -> list[int]:
    """把任意输入归一化为去重、有序、正整数的 id 列表（保持输入顺序）。"""
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple, set)):
        return []
    result: list[int] = []
    for raw in value:
        try:
            item_id = int(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if item_id > 0 and item_id not in result:
            result.append(item_id)
        if len(result) >= cap:
            break
    return result


def normalize_document_ids(value: Any) -> list[int]:
    """参考知识库文档 id 归一化（去重 / 剔除非正整数 / 限长）。"""
    return _normalize_positive_ids(value, MAX_REFERENCE_DOCUMENTS)


def normalize_knowledge_item_ids(value: Any) -> list[int]:
    """章节精确指定的知识条目 id 归一化。"""
    return _normalize_positive_ids(value, MAX_NODE_KNOWLEDGE_ITEMS)


def strip_markdown_fence(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline >= 0:
            text = text[first_newline + 1 :]
    if text.endswith("```"):
        text = text[:-3]
    return text.strip()


# ===== 目录树归一化（移植易标 normalizeOutline / assignOutlineIds） =====


def normalize_outline_tree(
    items: Any,
    level: int = 1,
    allowed_knowledge_ids: Any = None,
) -> list[dict]:
    """递归清洗 AI 输出的目录：剥编号、裁层级、去空节点、限每层子节点数。

    `allowed_knowledge_ids`（可选）：AI 会为叶子节点推荐知识素材 id，
    这里只保留**确实存在于参考知识库**的 id —— 防止模型幻觉出不存在条目 id
    （易标同样是「目录 Agent 只能从给定清单里挑」）。
    """
    if not isinstance(items, list) or level > MAX_OUTLINE_LEVEL:
        return []
    cap = MAX_ROOT_CHILDREN if level == 1 else MAX_CHILDREN
    allowed: set[int] | None = None
    if allowed_knowledge_ids is not None:
        allowed = set(normalize_knowledge_item_ids(allowed_knowledge_ids))
    result: list[dict] = []
    for item in items[:cap]:
        if not isinstance(item, dict):
            continue
        from .feasibility_prompts import strip_title_prefix

        title = strip_title_prefix(item.get("title")).strip() or "未命名章节"
        description = str(item.get("description") or "").strip()
        children = normalize_outline_tree(item.get("children"), level + 1, allowed)
        node: dict[str, Any] = {"title": title, "description": description}
        if children:
            node["children"] = children
        # 全空节点丢弃（无标题内容、无描述、无子节点）
        if title == "未命名章节" and not description and not children:
            continue
        knowledge_ids = normalize_knowledge_item_ids(
            item.get("knowledge_item_ids", item.get("knowledgeItemIds"))
        )
        if allowed is not None:
            knowledge_ids = [i for i in knowledge_ids if i in allowed]
        if knowledge_ids:
            node["knowledge_item_ids"] = knowledge_ids
        result.append(node)
    return result


def assign_outline_ids(items: list[dict], prefix: str = "") -> list[dict]:
    """给清洗后的目录树分配层级编号 id（"1"、"1.2"、"1.2.3"）。

    `content` 与 `knowledge_item_ids` 会被原样带下去：前者是人工/已生成的章节正文，
    后者是人工精确指定的知识条目（两者都必须挺过「重新分配编号」这一步）。
    """
    result: list[dict] = []
    for index, item in enumerate(items):
        node_id = f"{prefix}.{index + 1}" if prefix else f"{index + 1}"
        node: dict[str, Any] = {
            "id": node_id,
            "title": str(item.get("title") or "未命名章节"),
            "description": str(item.get("description") or ""),
        }
        content = str(item.get("content") or "")
        if content:
            node["content"] = content
        knowledge_ids = normalize_knowledge_item_ids(
            item.get("knowledge_item_ids", item.get("knowledgeItemIds"))
        )
        if knowledge_ids:
            node["knowledge_item_ids"] = knowledge_ids
        children = item.get("children") or []
        if children:
            node["children"] = assign_outline_ids(children, node_id)
        result.append(node)
    return result


def sanitize_outline_for_save(items: Any, level: int = 1) -> list[dict]:
    """净化人工提交的目录树：限三级、限节点数、剔空节点、清洗标题与写作重点。

    与 `normalize_outline_tree`（面向 AI 输出、会剥标题编号）不同：这里是人工
    编辑结果，标题按原样保留（前端已重新编号），只做结构性约束。
    """
    if not isinstance(items, list) or level > MAX_OUTLINE_LEVEL:
        return []
    cap = MAX_ROOT_CHILDREN if level == 1 else MAX_CHILDREN
    result: list[dict] = []
    for item in items[:cap]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        description = str(item.get("description") or "").strip()
        children = sanitize_outline_for_save(item.get("children"), level + 1)
        if not title and not description and not children:
            continue  # 全空节点丢弃，避免脏数据落库
        node: dict[str, Any] = {
            "title": title or "未命名章节",
            "description": description,
        }
        content = str(item.get("content") or "")
        if content:
            node["content"] = content
        knowledge_ids = normalize_knowledge_item_ids(
            item.get("knowledge_item_ids", item.get("knowledgeItemIds"))
        )
        if knowledge_ids:
            node["knowledge_item_ids"] = knowledge_ids
        if children:
            node["children"] = children
        result.append(node)
    return result


def collect_leaves(items: list[dict], trail: list[str] | None = None, leaves: list[dict] | None = None) -> list[dict]:
    """收集叶子节点，并附带章节路径 trail（供提示词与导出使用）。

    BUG-FIX 2026-09-15：原实现每次调用都 `leaves = list(leaves or [])` 复制一份累加器，
    递归返回值又没被接收 —— 结果**所有嵌套层级（二/三级章节）的叶子被静默丢弃**，
    只有一级叶子能返回。可研报告基本都是三级目录，这会让「正文生成」实际不写任何章节、
    完成率永远显示 0。修法：直接复用传入的累加器（原地 append），不再复制/丢弃返回值。
    """
    trail = list(trail or [])
    if leaves is None:
        leaves = []
    for item in items or []:
        next_trail = [*trail, item.get("title", "")]
        children = item.get("children") or []
        if children:
            collect_leaves(children, next_trail, leaves)
        else:
            leaves.append({**item, "trail": next_trail})
    return leaves


def replace_leaf_content(items: list[dict], node_id: str, content: str) -> list[dict]:
    result: list[dict] = []
    for item in items or []:
        if item.get("id") == node_id:
            result.append({**item, "content": content})
            continue
        children = item.get("children") or []
        if children:
            result.append({**item, "children": replace_leaf_content(children, node_id, content)})
            continue
        result.append(item)
    return result


def clear_tree_content(items: list[dict]) -> list[dict]:
    return [
        {**item, "content": "", "children": clear_tree_content(item.get("children") or [])}
        for item in items or []
    ]


# ===== 存储层 =====


def _ensure_meta_row(conn) -> dict:
    row = conn.execute("SELECT * FROM feasibility_meta WHERE id = 1").fetchone()
    if row:
        return dict(row)
    conn.execute("INSERT INTO feasibility_meta (id, step) VALUES (1, 'materials')")
    return dict(conn.execute("SELECT * FROM feasibility_meta WHERE id = 1").fetchone())


def _update_meta(conn, partial: dict) -> None:
    _ensure_meta_row(conn)
    fields = {k: v for k, v in partial.items() if k != "updated_at"}
    fields["updated_at"] = _now()
    if not fields:
        return
    assigns = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE feasibility_meta SET {assigns} WHERE id = 1", tuple(fields.values()))


def save_task_state(task_type: str, state: dict | None) -> None:
    """写任务状态；`state=None` 表示清除该类型任务（任务完成或下游失效时）。"""
    with get_db() as conn:
        if state is None:
            conn.execute("DELETE FROM feasibility_tasks WHERE type = ?", (task_type,))
            return
        conn.execute(
            "INSERT INTO feasibility_tasks (type, task_id, status, progress, stats_json, error, "
            "started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(type) DO UPDATE SET task_id = excluded.task_id, status = excluded.status, "
            "progress = excluded.progress, stats_json = excluded.stats_json, error = excluded.error, "
            "updated_at = excluded.updated_at",
            (
                task_type,
                str(state.get("task_id") or ""),
                str(state.get("status") or "running"),
                max(0, min(100, round(float(state.get("progress") or 0)))),
                json.dumps(state["stats"], ensure_ascii=False) if state.get("stats") is not None else None,
                str(state.get("error")) if state.get("error") else None,
                str(state.get("started_at") or _now()),
                _now(),
            ),
        )


def clear_task_state_if_owner(task_type: str, task_id: str) -> bool:
    """仅当当前任务行的 `task_id` 与给定值一致时才清除该行。

    BUG-FIX 2026-09-18（中）：`_finish_task` 原先无条件调
    `save_task_state(task_type, None)` —— 后者是 **按 type 整行 DELETE**，
    完全不看 `task_id`（该形参是死参数）。后果：若同一类型的新任务在旧任务
    收尾之前就已经启动（组锁 TTL 过期被抢、旧任务被取消后立刻重跑等），
    旧任务的收尾会把**新任务的状态行一并删掉** —— 前端表现为"刚启动的任务
    凭空消失、进度归零"，且没有任何报错。

    Returns:
        True 表示已清除；False 表示行不存在或已被更新的任务接管（此时不清除）。
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT task_id FROM feasibility_tasks WHERE type = ?", (task_type,)
        ).fetchone()
        if row is None:
            return False
        current = row["task_id"] or ""
        if task_id and current and current != str(task_id):
            logger.warning(
                "[可研报告] 跳过清理：任务 %s 已被新任务接管（当前 task_id=%s，待清理 task_id=%s）",
                task_type, current, task_id,
            )
            return False
        conn.execute("DELETE FROM feasibility_tasks WHERE type = ?", (task_type,))
        return True


def _task_from_row(row: dict) -> dict:
    return {
        "task_id": row["task_id"],
        "type": row["type"],
        "status": row["status"],
        "progress": int(row["progress"] or 0),
        "stats": _safe_json(row["stats_json"], None),
        "error": row["error"] or "",
        "started_at": row["started_at"],
        "updated_at": row["updated_at"],
    }


def _load_tasks(conn) -> dict[str, dict]:
    rows = conn.execute("SELECT * FROM feasibility_tasks").fetchall()
    return {row["type"]: _task_from_row(dict(row)) for row in rows if row["type"] in TASK_TYPES}


def _load_outline(conn) -> dict | None:
    rows = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM feasibility_outline_nodes "
            "ORDER BY level ASC, parent_node_id ASC, sort_order ASC"
        ).fetchall()
    ]
    if not rows:
        return None
    node_map: dict[str, dict] = {}
    for row in rows:
        node: dict[str, Any] = {
            "id": row["node_id"],
            "title": row["title"],
            "description": row["description"] or "",
            "content": row["content"] or "",
            "children": [],
        }
        knowledge_ids = normalize_knowledge_item_ids(
            _safe_json(row.get("knowledge_item_ids_json"), None)
        )
        if knowledge_ids:
            node["knowledgeItemIds"] = knowledge_ids
        node_map[row["node_id"]] = node
    roots: list[dict] = []
    for row in rows:
        node = node_map[row["node_id"]]
        parent = row["parent_node_id"]
        if parent and parent in node_map:
            node_map[parent]["children"].append(node)
        else:
            roots.append(node)
    meta = _ensure_meta_row(conn)
    return {
        "outline": roots,
        "project_name": meta["outline_project_name"] or None,
        "project_overview": meta["outline_project_overview"] or None,
    }


def _save_outline_data(conn, outline_data: dict | None) -> None:
    if not outline_data or not outline_data.get("outline"):
        conn.execute("DELETE FROM feasibility_outline_nodes")
        _update_meta(conn, {"outline_project_name": None, "outline_project_overview": None})
        return
    # 平铺 + 全量替换（删除不再存在的节点）
    rows: list[tuple] = []
    now = _now()

    def flatten(items: list, parent: str | None, level: int) -> None:
        for index, item in enumerate(items):
            node_id = str((item or {}).get("id") or "").strip()
            if not node_id:
                continue  # 没有编号的节点无法落库（调用方必须先 assign_outline_ids）
            knowledge_ids = normalize_knowledge_item_ids(
                item.get("knowledge_item_ids", item.get("knowledgeItemIds"))
            )
            rows.append(
                (
                    node_id,
                    parent,
                    index,
                    level,
                    item.get("title") or "未命名章节",
                    item.get("description") or "",
                    item.get("content") or "",
                    json.dumps(knowledge_ids, ensure_ascii=False) if knowledge_ids else None,
                    now,
                )
            )
            flatten(item.get("children") or [], node_id, level + 1)

    outline = outline_data["outline"]
    flatten(outline, None, 1)
    conn.execute("DELETE FROM feasibility_outline_nodes")
    conn.executemany(
        "INSERT INTO feasibility_outline_nodes "
        "(node_id, parent_node_id, sort_order, level, title, description, content, "
        "knowledge_item_ids_json, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    _update_meta(
        conn,
        {
            "outline_project_name": outline_data.get("project_name"),
            "outline_project_overview": outline_data.get("project_overview"),
        },
    )


def load_report() -> dict:
    """完整工作区状态（步骤、项目信息、资料、分析、目录、参数、任务）。"""
    with get_db() as conn:
        meta = _ensure_meta_row(conn)
        sources = [
            {
                "id": r["id"],
                "fileName": r["file_name"],
                "markdownChars": int(r["content_chars"] or 0),
                "importedAt": r["imported_at"],
            }
            for r in conn.execute(
                "SELECT id, file_name, content_chars, imported_at FROM feasibility_sources "
                "ORDER BY imported_at ASC"
            ).fetchall()
        ]
        return {
            "step": normalize_step(meta["step"]),
            "projectInfo": normalize_project_info(_safe_json(meta["project_info_json"], {})),
            "sourceFiles": sources,
            "analysisMarkdown": meta["analysis_markdown"] or "",
            "outlineTemplate": meta["outline_template"] or "government",
            "targetWords": int(meta["target_words"] or DEFAULT_TARGET_WORDS),
            "keyParametersMarkdown": meta["key_parameters_markdown"] or "",
            # 参考知识库（可研选材）：易标 referenceDocumentIds 同语义
            "referenceDocumentIds": normalize_document_ids(
                _safe_json(meta.get("reference_document_ids_json"), [])
            ),
            "outlineData": _load_outline(conn),
            **_load_tasks(conn),
        }


def update_report_state(partial: dict) -> dict:
    """按需更新 meta / 目录 / 任务字段（易标 updateFeasibilityReportWithoutReload 移植）。"""
    with get_db() as conn:
        meta_patch: dict = {}
        if "step" in partial:
            meta_patch["step"] = normalize_step(partial["step"])
        if "projectInfo" in partial:
            meta_patch["project_info_json"] = json.dumps(
                normalize_project_info(partial["projectInfo"]), ensure_ascii=False
            )
        if "analysisMarkdown" in partial:
            meta_patch["analysis_markdown"] = str(partial["analysisMarkdown"] or "") or None
        if "outlineTemplate" in partial:
            from .feasibility_prompts import normalize_template

            meta_patch["outline_template"] = normalize_template(partial["outlineTemplate"])
        if "targetWords" in partial:
            meta_patch["target_words"] = normalize_target_words(partial["targetWords"])
        if "keyParametersMarkdown" in partial:
            meta_patch["key_parameters_markdown"] = str(partial["keyParametersMarkdown"] or "") or None
        if "referenceDocumentIds" in partial:
            meta_patch["reference_document_ids_json"] = json.dumps(
                normalize_document_ids(partial["referenceDocumentIds"]), ensure_ascii=False
            )
        if meta_patch:
            _update_meta(conn, meta_patch)
        if "outlineData" in partial:
            _save_outline_data(conn, partial["outlineData"])
        for task_type in TASK_TYPES:
            if task_type in partial:
                state = partial[task_type]
                if state is None:
                    conn.execute("DELETE FROM feasibility_tasks WHERE type = ?", (task_type,))
                else:
                    _upsert_task_conn(conn, task_type, state)
    return load_report()


def _upsert_task_conn(conn, task_type: str, state: dict) -> None:
    conn.execute(
        "INSERT INTO feasibility_tasks (type, task_id, status, progress, stats_json, error, "
        "started_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(type) DO UPDATE SET task_id = excluded.task_id, status = excluded.status, "
        "progress = excluded.progress, stats_json = excluded.stats_json, error = excluded.error, "
        "updated_at = excluded.updated_at",
        (
            task_type,
            str(state.get("task_id") or ""),
            str(state.get("status") or "running"),
            max(0, min(100, round(float(state.get("progress") or 0)))),
            json.dumps(state["stats"], ensure_ascii=False) if state.get("stats") is not None else None,
            str(state.get("error")) if state.get("error") else None,
            str(state.get("started_at") or _now()),
            _now(),
        ),
    )


# ===== 下游失效 =====


def _clear_downstream(conn, stage: str) -> None:
    """按改动阶段清空下游产物（易标 clearDownstream* 语义）。"""
    from collections import OrderedDict

    # 阶段 → 需清除的任务类型（含自身下游全部阶段）
    downstream: dict[str, list[str]] = {
        "materials": list(TASK_TYPES),
        "analysis": ["feasibility-outline", "feasibility-parameters", "feasibility-content"],
        "outline": ["feasibility-parameters", "feasibility-content"],
        "parameters": ["feasibility-content"],
    }
    for task_type in downstream.get(stage, []):
        conn.execute("DELETE FROM feasibility_tasks WHERE type = ?", (task_type,))
    if stage in ("materials", "analysis"):
        conn.execute("DELETE FROM feasibility_outline_nodes")
        _update_meta(
            conn,
            {
                "outline_project_name": None,
                "outline_project_overview": None,
                # materials 改动连分析结论一起失效（易标 clearDownstreamFromMaterials 同语义）
                **({"analysis_markdown": None, "key_parameters_markdown": None}
                   if stage == "materials"
                   else {"key_parameters_markdown": None}),
            },
        )
    elif stage == "outline":
        _update_meta(conn, {"key_parameters_markdown": None})
    elif stage == "parameters":
        # 参数更新后旧正文全部失效（口径可能已变）
        data = _load_outline(conn)
        if data and data.get("outline"):
            _save_outline_data(conn, {**data, "outline": clear_tree_content(data["outline"])})


# ===== 业务操作 =====


def save_project_info(project_info: dict, *, clear_downstream: bool = True) -> dict:
    info = normalize_project_info(project_info)
    if not info["projectName"]:
        raise ValueError("请填写项目名称")
    with get_db() as conn:
        _update_meta(conn, {"project_info_json": json.dumps(info, ensure_ascii=False)})
        if clear_downstream:
            _clear_downstream(conn, "materials")
    return load_report()


def add_source(file_name: str, content: str) -> dict:
    name = str(file_name or "").strip() or "未命名资料"
    text = str(content or "").strip()
    if not text:
        raise ValueError("资料内容为空")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO feasibility_sources (id, file_name, content, content_hash, content_chars, "
            "imported_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                uuid.uuid4().hex[:12],
                name,
                text,
                hashlib.sha1(text.encode("utf-8")).hexdigest(),
                len(text),
                _now(),
            ),
        )
        _clear_downstream(conn, "materials")
    return load_report()


def remove_source(source_id: str) -> dict:
    with get_db() as conn:
        exists = conn.execute(
            "SELECT 1 FROM feasibility_sources WHERE id = ?", (str(source_id or "").strip(),)
        ).fetchone()
        if not exists:
            raise ValueError("未找到要移除的资料")
        conn.execute("DELETE FROM feasibility_sources WHERE id = ?", (str(source_id or "").strip(),))
        _clear_downstream(conn, "materials")
    return load_report()


def read_combined_source_markdown() -> str:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT file_name, content FROM feasibility_sources ORDER BY imported_at ASC"
        ).fetchall()
    parts = [f"## {r['file_name']}\n\n{(r['content'] or '').strip()}" for r in rows if (r["content"] or "").strip()]
    return "\n\n".join(parts)


def save_analysis(markdown: str) -> dict:
    with get_db() as conn:
        _update_meta(conn, {"analysis_markdown": str(markdown or "").strip() or None})
        _clear_downstream(conn, "analysis")
    return load_report()


def save_outline_config(
    outline_template: Any,
    target_words: Any,
    reference_document_ids: Any = None,
) -> dict:
    """保存目录口径（模板/目标字数）与参考知识库文档。

    `reference_document_ids=None` 表示「不改动当前选材」（保留旧前端兼容）。
    """
    from .feasibility_prompts import normalize_template

    patch: dict[str, Any] = {
        "outline_template": normalize_template(outline_template),
        "target_words": normalize_target_words(target_words),
    }
    if reference_document_ids is not None:
        patch["reference_document_ids_json"] = json.dumps(
            normalize_document_ids(reference_document_ids), ensure_ascii=False
        )
    with get_db() as conn:
        _update_meta(conn, patch)
    return load_report()


def save_reference_documents(document_ids: Any) -> dict:
    """单独保存参考知识库文档选择（不动模板与字数）。"""
    with get_db() as conn:
        _update_meta(
            conn,
            {
                "reference_document_ids_json": json.dumps(
                    normalize_document_ids(document_ids), ensure_ascii=False
                )
            },
        )
    return load_report()


def _outline_snapshot(conn) -> dict[str, dict]:
    """当前目录节点快照（node_id → 正文 / 精确选材），供重新编号时搬运内容。"""
    rows = conn.execute(
        "SELECT node_id, content, knowledge_item_ids_json FROM feasibility_outline_nodes"
    ).fetchall()
    return {r["node_id"]: dict(r) for r in rows}


def _carry_node_payload(item: dict, snapshot: dict, reverse_map: dict, affected: set, clear_all: bool) -> dict:
    """按 id 映射把旧节点的正文/精确选材搬到新编号上（易标 saveOutline 的 reverseMap 逻辑）。

    要点：删除/新增/排序都会让层级编号整体前移，若不做映射，「1.3」的正文会
    被丢弃或错挂到别的章节；被显式标记为 affected 的节点视为内容已失效，清空正文。
    """
    node_id = item["id"]
    old_id = reverse_map.get(node_id, node_id)
    previous = snapshot.get(old_id) or snapshot.get(node_id) or {}
    is_affected = clear_all or node_id in affected or old_id in affected

    node = {**item}
    carried_content = item.get("content") or previous.get("content") or ""
    node["content"] = "" if is_affected else carried_content
    if not node.get("knowledge_item_ids"):
        carried_ids = normalize_knowledge_item_ids(_safe_json(previous.get("knowledge_item_ids_json"), None))
        if carried_ids:
            node["knowledge_item_ids"] = carried_ids
    children = item.get("children") or []
    if children:
        node["children"] = [
            _carry_node_payload(child, snapshot, reverse_map, affected, clear_all) for child in children
        ]
    return node


def save_outline(
    outline_data: dict,
    reason: Any = "replace",
    id_map: dict | None = None,
    affected_node_ids: list | None = None,
) -> dict:
    """保存目录树（人工树形编辑器 / AI 重生成 / 排序）。

    - `reason="sort"`：仅顺序变化，不失效下游参数与正文；
    - 其它原因（edit/delete/add-root/add-child/replace）：清空关键参数与正文任务；
    - `id_map`（旧编号 → 新编号）与 `affected_node_ids`：重新编号后搬运正文，
      被 affected 命中的节点正文清空（新增或内容已失效）；
    - 有后台任务运行时直接拒绝（易标 assertContentUnlocked 同语义）。
    """
    assert_no_running_task()
    norm_reason = normalize_save_reason(reason)
    source = outline_data if isinstance(outline_data, dict) else {}
    outline = assign_outline_ids(sanitize_outline_for_save(source.get("outline")))
    payload = {**source, "outline": outline}

    with get_db() as conn:
        # 统一走「按 id 映射搬运正文」：即使客户端漏传正文，也能从库里旧快照恢复，
        # 只有 replace 或显式 affected 的节点才会被清空。
        snapshot = _outline_snapshot(conn)
        reverse_map = {
            str(new_id): str(old_id)
            for old_id, new_id in (id_map or {}).items()
            if str(old_id).strip() and str(new_id).strip()
        }
        affected = {str(x) for x in (affected_node_ids or [])}
        clear_all = norm_reason == "replace"
        payload["outline"] = [
            _carry_node_payload(node, snapshot, reverse_map, affected, clear_all) for node in outline
        ]
        _save_outline_data(conn, payload)
        if norm_reason != "sort":
            _clear_downstream(conn, "outline")
    return load_report()


def save_key_parameters(markdown: str) -> dict:
    with get_db() as conn:
        _update_meta(conn, {"key_parameters_markdown": str(markdown or "").strip() or None})
        _clear_downstream(conn, "parameters")
    return load_report()


def write_chapter_content(node_id: str, content: str) -> None:
    """底层写章节正文（不加任务锁）。

    仅限后台任务内部 checkpoint 调用：任务自己就是「运行中」的那一方，
    不能走 `save_chapter_content` 的编辑锁；对外编辑一律走公开函数。
    """
    node_id = str(node_id or "").strip()
    if not node_id:
        raise ValueError("缺少章节编号")
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE feasibility_outline_nodes SET content = ?, updated_at = ? WHERE node_id = ?",
            (str(content or ""), _now(), node_id),
        )
        if not cursor.rowcount:
            raise ValueError("未找到要保存的章节")


def save_chapter_content(node_id: str, content: str) -> dict:
    assert_no_running_task()
    write_chapter_content(node_id, content)
    return load_report()


def save_node_knowledge_items(node_id: str, item_ids: Any) -> dict:
    """保存单个章节精确指定的知识条目（空列表 = 恢复自动匹配）。"""
    node_id = str(node_id or "").strip()
    if not node_id:
        raise ValueError("缺少章节编号")
    assert_no_running_task()
    ids = normalize_knowledge_item_ids(item_ids)
    with get_db() as conn:
        cursor = conn.execute(
            "UPDATE feasibility_outline_nodes SET knowledge_item_ids_json = ?, updated_at = ? "
            "WHERE node_id = ?",
            (json.dumps(ids, ensure_ascii=False) if ids else None, _now(), node_id),
        )
        if not cursor.rowcount:
            raise ValueError("未找到要保存的章节")
    return load_report()


def assert_no_running_task() -> None:
    """目录/正文人工编辑前检查：有任务在跑时禁止改（易标 assertContentUnlocked）。"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT type, status FROM feasibility_tasks WHERE status IN ('running', 'paused')"
        ).fetchall()
    if rows:
        labels = "、".join(TASK_TYPE_LABELS.get(r["type"], r["type"]) for r in rows)
        raise TaskRunningError(f"{labels} 任务正在运行，请等待完成或先重置")


def clear_workspace() -> dict:
    with get_db() as conn:
        conn.execute("DELETE FROM feasibility_outline_nodes")
        conn.execute("DELETE FROM feasibility_sources")
        conn.execute("DELETE FROM feasibility_tasks")
        _update_meta(
            conn,
            {
                "step": "materials",
                "project_info_json": None,
                "analysis_markdown": None,
                "outline_template": "government",
                "target_words": DEFAULT_TARGET_WORDS,
                "key_parameters_markdown": None,
                "outline_project_name": None,
                "outline_project_overview": None,
                "reference_document_ids_json": None,
            },
        )
    return load_report()


# ===== DOCX 导出 =====


def export_docx() -> str:
    """把可研报告（项目信息 + 关键参数 + 目录树正文）导出为 Word。

    结构：封面信息段 → 按目录树层级写入标题（1/2/3 级）与正文段落。
    保存到 FEASIBILITY_REPORTS_DIR（已登记在 `config.report_download_roots()` 白名单）。
    """
    from docx import Document
    from docx.shared import Pt

    report = load_report()
    outline_data = report.get("outlineData")
    if not outline_data or not outline_data.get("outline"):
        raise ValueError("尚未生成报告目录，无法导出")
    has_content = bool(collect_leaves(outline_data["outline"]))

    doc = Document()
    info = report["projectInfo"]
    title = doc.add_heading(info.get("projectName") or "可行性研究报告", level=0)

    # 封面信息段
    from .feasibility_prompts import format_project_info

    for line in format_project_info(info).splitlines():
        p = doc.add_paragraph()
        run = p.add_run(line)
        run.font.size = Pt(12)

    if report.get("keyParametersMarkdown"):
        doc.add_heading("关键参数与编制口径", level=1)
        for block in report["keyParametersMarkdown"].splitlines():
            if block.strip():
                doc.add_paragraph(block.rstrip())

    def write_nodes(nodes: list[dict], level: int) -> None:
        for node in nodes:
            if level <= 3:
                doc.add_heading(node.get("title") or "未命名章节", level=level)
            content = (node.get("content") or "").strip()
            if content:
                for block in content.split("\n"):
                    text = block.rstrip()
                    if text.startswith("#"):
                        text = text.lstrip("#").strip()
                    if text:
                        doc.add_paragraph(text)
            write_nodes(node.get("children") or [], level + 1)

    write_nodes(outline_data["outline"], 1)
    if not has_content:
        doc.add_paragraph("【待补充】正文尚未生成，可在「正文生成」步骤运行 AI 撰写。")

    FEASIBILITY_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    safe_name = re.sub(r'[\\/:*?"<>|]', "_", info.get("projectName") or "可行性研究报告")[:40] or "可行性研究报告"
    file_path = FEASIBILITY_REPORTS_DIR / f"feasibility_{safe_name}_{stamp}.docx"
    doc.save(str(file_path))
    logger.info("[可研报告] 已导出 DOCX: %s", file_path)
    return str(file_path)
