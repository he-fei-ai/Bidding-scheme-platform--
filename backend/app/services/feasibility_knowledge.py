# -*- coding: utf-8 -*-
"""可研报告「参考知识库」选材（ENHANCE 2026-09-15）。

为什么不能照搬易标（两平台知识库结构不同）：
- 易标是「文档 → 轻量条目（id / title / resume）」两层，条目正文另存，
  选材靠 `chapter.knowledge_item_ids` 或「章节标题字符重合度」取 top3；
- 本平台 `knowledge_items` 粒度更细且自带 `title / summary / content / keywords /
  source_doc_id`，正文可直接使用，因此选材流程设计为：

1. 用户在「报告目录」步骤勾选**参考文档**（document 级，落 `feasibility_meta`）；
2. 系统按「章节标题 + 写作重点」与「条目标题 + 摘要 + 关键词 + 正文」的
   字符重合度打分，每章节自动取 top-N（默认 3，score >= 2）作为写作素材；
3. 章节可写 `knowledge_item_ids` 精确指定条目，**人工选择覆盖自动匹配**；
4. 素材注入提示词时必须改写到当前项目语境，禁止提及「知识库 / 历史文档 / 资料来源」。

打分函数与选材/拼接逻辑全部是纯函数，便于不经数据库直接单测。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: 自动匹配的候选门槛（与易标一致：score > 1）
SCORE_MIN = 2
#: 每章节自动匹配的素材条数上限
DEFAULT_TOP_N = 3
#: 单条素材正文截断（防单条超大条目挤掉其它素材）
MAX_ITEM_CHARS = 1500
#: 单章节素材总字符上限（与易标 selectKnowledgeContents 的 24000 同量级）
MAX_BLOCK_CHARS = 20000
#: 轻量条目（目录/参数任务用）条数上限
MAX_LIGHTWEIGHT_ITEMS = 300
#: 参与打分/拼接时忽略的标点（只比较实义字符）
_IGNORED_CHARS = set(" \t\r\n，。；：、（）()《》“”\"'’‘·-—_/\\|!！?？,.;:+*#[]{}<>%％&")


# ===== 纯函数：打分与选材 =====


def _unique_content_chars(text: Any) -> list[str]:
    """取出实义字符（去标点/空白/去重，保持顺序）。"""
    seen: dict[str, None] = {}
    for char in str(text or ""):
        if char in _IGNORED_CHARS:
            continue
        seen.setdefault(char, None)
    return list(seen)


def normalize_knowledge_item(raw: Any) -> dict | None:
    """把知识库行归一化为选材用的轻结构；无 id 或全空则返回 None。"""
    if not isinstance(raw, dict):
        return None
    try:
        item_id = int(str(raw.get("id") or "").strip())
    except (TypeError, ValueError):
        return None
    if item_id <= 0:
        return None
    keywords = raw.get("keywords")
    if isinstance(keywords, str):
        keywords = [keywords] if keywords.strip() else []
    elif not isinstance(keywords, (list, tuple)):
        keywords = []
    return {
        "id": item_id,
        "documentId": int(raw.get("source_doc_id") or raw.get("documentId") or 0),
        "documentName": str(raw.get("documentName") or "").strip(),
        "title": str(raw.get("title") or "").strip(),
        "summary": str(raw.get("summary") or raw.get("resume") or "").strip(),
        "keywords": [str(k).strip() for k in keywords if str(k).strip()],
        "content": str(raw.get("content") or "").strip(),
    }


def score_knowledge_item(item: dict, chapter: dict) -> int:
    """章节与素材的相关度（移植易标 scoreKnowledge：字符级重合计数）。

    查询侧取章节标题 + 写作重点，目标侧取条目标题 + 摘要 + 关键词 + 正文，
    命中的**不同字符**数量即为分数（中文单字重合度已足够区分主题）。
    """
    query = f"{(chapter or {}).get('title') or ''}{(chapter or {}).get('description') or ''}"
    target = "{}{}{}{}".format(
        item.get("title") or "",
        item.get("summary") or "",
        "".join(item.get("keywords") or []),
        item.get("content") or "",
    )
    return sum(1 for char in _unique_content_chars(query) if char in target)


def select_knowledge_items(
    items: list[dict],
    chapter: dict,
    *,
    explicit_ids: Any = None,
    top_n: int = DEFAULT_TOP_N,
    min_score: int = SCORE_MIN,
) -> list[dict]:
    """为单个章节选材：显式指定优先，否则按相关度取 top-N。

    显式 id 命中不到实体时**回退到自动匹配**（而不是返回空）——
    知识条目可能已被删除，此时不应让章节彻底失去素材。
    """
    pool = [item for item in (items or []) if isinstance(item, dict)]
    if not pool:
        return []
    explicit: list[int] = []
    for raw in explicit_ids or []:
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in explicit:
            explicit.append(value)
    if explicit:
        by_id = {item.get("id"): item for item in pool}
        picked = [by_id[i] for i in explicit if i in by_id]
        if picked:
            return picked
    scored = sorted(
        ((score_knowledge_item(item, chapter), int(item.get("id") or 0), item) for item in pool),
        key=lambda entry: (-entry[0], entry[1]),
    )
    return [item for score, _id, item in scored if score >= min_score][: max(1, top_n)]


def format_knowledge_material(
    items: list[dict],
    *,
    item_chars: int = MAX_ITEM_CHARS,
    max_chars: int = MAX_BLOCK_CHARS,
) -> str:
    """把素材拼成提示词块（截断保护 + 不暴露来源）。"""
    blocks: list[str] = []
    used = 0
    for item in items or []:
        title = str(item.get("title") or "").strip()
        body = str(item.get("content") or "").strip() or str(item.get("summary") or "").strip()
        if not title and not body:
            continue
        body = body[:item_chars]
        block = f"### {title}\n\n{body}".strip() if title else body
        if used + len(block) > max_chars:
            remaining = max_chars - used
            if remaining < 200:
                break
            block = block[:remaining]
        blocks.append(block)
        used += len(block)
    return "\n\n".join(blocks)


def format_lightweight_catalog(items: list[dict], limit: int = MAX_LIGHTWEIGHT_ITEMS) -> str:
    """轻量条目清单（id/标题/摘要），供目录与参数任务参考。"""
    lines: list[str] = []
    for item in (items or [])[:limit]:
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        summary = str(item.get("summary") or "").strip().replace("\n", " ")[:120]
        lines.append(f"- [{item.get('id')}] {title}" + (f"：{summary}" if summary else ""))
    return "\n".join(lines)


# ===== 数据装载 =====


def load_reference_items(document_ids: Any) -> list[dict]:
    """按参考文档 id 取全部素材条目（未知 id 直接忽略）。"""
    ids = [int(x) for x in (document_ids or []) if str(x).strip().isdigit() and int(x) > 0]
    if not ids:
        return []
    from . import knowledge as knowledge_store

    try:
        names = {
            int(doc["id"]): str(doc.get("filename") or "")
            for doc in knowledge_store.list_document_summaries(limit=500)
        }
        rows = knowledge_store.list_items_by_document_ids(ids)
    except Exception:  # pragma: no cover - 数据库异常不应让任务整体失败
        logger.warning("[可研报告] 读取参考知识库失败，跳过知识库素材", exc_info=True)
        return []
    items: list[dict] = []
    for row in rows:
        item = normalize_knowledge_item({**row, "documentName": names.get(int(row.get("source_doc_id") or 0), "")})
        if item:
            items.append(item)
    return items


def list_reference_catalog(limit: int = 200) -> list[dict]:
    """参考知识库可选文档清单（含条目数/素材字数）。"""
    from . import knowledge as knowledge_store

    return knowledge_store.list_document_summaries(limit=limit)


def chapter_material_text(node: dict, items: list[dict]) -> str:
    """章节最终素材文本（供任务与 UI 复用）。"""
    if not items:
        return ""
    explicit = node.get("knowledge_item_ids") or node.get("knowledgeItemIds")
    selected = select_knowledge_items(items, node or {}, explicit_ids=explicit)
    return format_knowledge_material(selected)


def node_explicit_ids(node: dict) -> list[int]:
    """章节显式指定的素材编号（无则空列表）。"""
    raw = (node or {}).get("knowledge_item_ids") or (node or {}).get("knowledgeItemIds") or []
    ids: list[int] = []
    for value in raw if isinstance(raw, (list, tuple)) else []:
        try:
            item_id = int(str(value).strip())
        except (TypeError, ValueError):
            continue
        if item_id > 0 and item_id not in ids:
            ids.append(item_id)
    return ids


def find_outline_node(items: Any, node_id: str) -> dict | None:
    """在目录树里按编号找节点（UI 查询章节选材用）。"""
    target = str(node_id or "").strip()
    if not target:
        return None
    stack = list(items or [])
    while stack:
        node = stack.pop(0)
        if not isinstance(node, dict):
            continue
        if str(node.get("id") or "") == target:
            return node
        stack.extend(node.get("children") or [])
    return None


def chapter_knowledge_overview(
    node: dict,
    items: list[dict],
    *,
    top_n: int = DEFAULT_TOP_N,
    candidate_limit: int = 30,
) -> dict:
    """章节选材概览（前端面板用）：显式选择 + 自动匹配结果 + 相关度候选列表。"""
    pool = [item for item in (items or []) if isinstance(item, dict)]
    explicit = node_explicit_ids(node)
    selected = select_knowledge_items(pool, node or {}, explicit_ids=explicit, top_n=top_n)
    selected_ids = {item.get("id") for item in selected}
    scored = sorted(
        ((score_knowledge_item(item, node or {}), item) for item in pool),
        key=lambda entry: (-entry[0], int(entry[1].get("id") or 0)),
    )
    return {
        "nodeId": str((node or {}).get("id") or ""),
        "title": str((node or {}).get("title") or ""),
        "explicit": bool(explicit),
        "explicitItemIds": explicit,
        "selectedItemIds": sorted(i for i in selected_ids if i),
        "candidates": [
            {
                "id": item.get("id"),
                "title": item.get("title") or "",
                "summary": (item.get("summary") or "")[:160],
                "documentName": item.get("documentName") or "",
                "score": score,
                "selected": item.get("id") in selected_ids,
            }
            for score, item in scored[: max(1, candidate_limit)]
        ],
    }
