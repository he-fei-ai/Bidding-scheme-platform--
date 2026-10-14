"""knowledge_refill.py — 知识库两轮补漏。

ENHANCE 2026-10-12（P2-2）：移植自 OpenBidKit 易标「two-round knowledge extraction」方法论。

第一轮：从招标文件中提取知识条目。
第二轮：对比目录，检查是否有遗漏的关键信息，补充到知识库。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class KnowledgeItem:
    """知识条目。"""
    id: str
    title: str
    content: str
    source: str = ""
    category: str = ""
    keywords: List[str] = field(default_factory=list)


_EXTRACT_SYSTEM_PROMPT = """你是一个专业的工程招标文件分析专家。请从给定的招标文件内容中提取关键知识条目。

提取要求：
1. 识别项目基本信息（项目名称、规模、地点、工期、投资额等）
2. 识别技术要求（技术标准、规范、参数、指标等）
3. 识别商务要求（付款方式、结算方式、违约责任等）
4. 识别评分标准（评分项、权重、评分细则等）
5. 识别关键时间节点（开标时间、投标截止时间、合同履行期限等）
6. 识别参与方信息（招标人、代理机构、联系人等）

输出格式：JSON 数组，每个元素包含：
- title: 知识条目标题（简短，10-20 字）
- content: 知识条目详细内容（100-300 字）
- category: 分类（"项目信息" | "技术要求" | "商务要求" | "评分标准" | "时间节点" | "参与方" | "其他"）
- keywords: 关键词数组（3-5 个）

只返回 JSON 数组，不要包含任何解释或 markdown 围栏。"""


# ========== 修复提示词 ==========

def _extract_sys_prompt() -> str:
    """读取知识补漏提取系统提示词：DB 优先 → 注册表 → 硬编码回退。

    BUG-FIX-PROMPT-E6（2026-10-14）：原各调用点直接引用 _EXTRACT_SYSTEM_PROMPT
    硬编码，管理页无法查看/编辑/覆盖，_check_hardcode_drift 也无法比对。
    """
    try:
        from .prompts import get_prompt as _gp
        return _gp("KB_REFILL_EXTRACT_SYSTEM") or _EXTRACT_SYSTEM_PROMPT
    except Exception:
        return _EXTRACT_SYSTEM_PROMPT


@dataclass
class RefillResult:
    """补漏结果。"""
    round: int
    success: bool
    new_items: List[KnowledgeItem] = field(default_factory=list)
    total_items: int = 0
    skipped_duplicates: int = 0
    error: Optional[str] = None


def split_text_into_batches(
    text: str,
    max_batch_chars: int = 10000,
    overlap: int = 500,
) -> List[str]:
    """将长文本分割成多个批次。"""
    if not text:
        return []

    batches = []
    start = 0
    text_len = len(text)
    while start < text_len:
        end = min(start + max_batch_chars, text_len)
        batch = text[start:end]

        if end < text_len:
            last_newline = batch.rfind('\n', max(0, end - overlap), end)
            if last_newline > start:
                end = last_newline + 1
                batch = text[start:end]

        batches.append(batch.strip())

        # Always advance start to avoid infinite loop
        if end < text_len:
            next_start = max(start + 1, end - overlap)
            if next_start <= start:
                next_start = start + 1  # Ensure forward progress
            start = next_start
        else:
            break

    return batches


def _normalize_title(title: str) -> str:
    """标准化标题用于去重。"""
    if not title:
        return ""
    return "".join(c for c in title if c.isalnum())


def _is_duplicate(
    item: KnowledgeItem,
    existing_titles: Set[str],
    existing_contents: Set[str],
) -> bool:
    """检查条目是否重复。"""
    norm_title = _normalize_title(item.title)
    norm_content = _normalize_title(item.content[:100])

    if norm_title and norm_title in existing_titles:
        return True
    if norm_content and norm_content in existing_contents:
        return True
    return False


async def extract_knowledge_items(
    text: str,
    ai_call: Callable[[List[Dict[str, str]]], Any],
    *,
    max_batch_chars: int = 10000,
) -> List[KnowledgeItem]:
    """从文本中提取知识条目。"""
    import json

    batches = split_text_into_batches(text, max_batch_chars)
    all_items: List[KnowledgeItem] = []
    existing_titles: Set[str] = set()
    existing_contents: Set[str] = set()

    for i, batch in enumerate(batches):
        # BUG-FIX-PROMPT-E6（知识补漏提取提示词未注册）：原直接引用模块级
        # _EXTRACT_SYSTEM_PROMPT 硬编码，管理页无法查看/编辑/覆盖。现统一走
        # get_prompt()（DB 优先 → 注册表 → 硬编码回退），保留硬编码作为
        # DB 不可用时的最小 fallback。
        messages = [
            {"role": "system", "content": _extract_sys_prompt()},
            {"role": "user", "content": f"请提取以下招标文件内容中的关键知识条目：\n\n{batch}"},
        ]

        try:
            response = await ai_call(messages)
            response_str = str(response).strip()

            if response_str.startswith("```"):
                lines = response_str.split('\n')
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].startswith("```"):
                    lines = lines[:-1]
                response_str = '\n'.join(lines)

            data = json.loads(response_str)
            if not isinstance(data, list):
                continue

            for item_data in data:
                if not isinstance(item_data, dict):
                    continue

                title = item_data.get("title", "")
                content = item_data.get("content", "")
                if not title or not content:
                    continue

                item = KnowledgeItem(
                    id=f"item_{len(all_items) + 1}",
                    title=title,
                    content=content,
                    category=item_data.get("category", "其他"),
                    keywords=item_data.get("keywords", []),
                    source=f"batch_{i}",
                )

                if _is_duplicate(item, existing_titles, existing_contents):
                    continue

                existing_titles.add(_normalize_title(item.title))
                existing_contents.add(_normalize_title(item.content[:100]))
                all_items.append(item)

        except Exception as e:
            logger.warning("Knowledge extraction failed for batch %d: %s", i, e)
            continue

    return all_items



async def refill_knowledge_gaps(
    outline: List[Dict[str, Any]],
    existing_items: List[KnowledgeItem],
    bid_text: str,
    ai_call: Callable[[List[Dict[str, str]]], Any],
    *,
    batch_size: Optional[int] = None,
    max_bid_excerpt_chars: int = 8000,
) -> RefillResult:
    """第二轮补漏：检查目录中是否有遗漏的关键信息。

    ENHANCE 2026-10-11（引入项 #16，接入主链路时修正的潜在缺陷）：
    本函数此前接受 `bid_text` 形参却**从未把它注入 AI 提示词**——等于让模型
    "从招标文件中提取"却不给它看招标文件，只能凭空编造，补漏结果不可信；
    同时 `split_text_into_batches` 也是未使用的死函数。现修正为：把招标正文的
    有界摘录（`max_bid_excerpt_chars`，默认 8000 字）注入 user prompt 作为取材
    来源，超出部分截断并标注。因该模块此前**无任何生产调用点**，此改动零回归风险。
    """
    import json
    from ...config import KNOWLEDGE_REFILL_BATCH_SIZE

    if batch_size is None:
        batch_size = KNOWLEDGE_REFILL_BATCH_SIZE

    section_titles: List[str] = []

    def collect_titles(nodes: List[Dict[str, Any]]):
        for node in nodes:
            if isinstance(node, dict):
                title = node.get("title", "")
                if title:
                    section_titles.append(title)
                children = node.get("children", [])
                if isinstance(children, list):
                    collect_titles(children)

    collect_titles(outline)

    if not section_titles:
        return RefillResult(round=2, success=True, total_items=0)

    if not bid_text or not bid_text.strip():
        return RefillResult(round=2, success=False, error="招标文件正文为空，无法补漏")

    existing_titles_text = "\n".join([f"- {item.title}" for item in existing_items])
    section_titles_text = "\n".join([f"- {title}" for title in section_titles[:50]])

    # 取材来源：招标文件正文有界摘录。截断而非全量，避免超长正文撑爆上下文；
    # 与批次大小联动（batch_size 条目录 → 相应正文窗口）。
    _excerpt = bid_text.strip()
    _truncated = False
    if max_bid_excerpt_chars and len(_excerpt) > max_bid_excerpt_chars:
        _excerpt = _excerpt[:max_bid_excerpt_chars]
        _truncated = True
    _bid_source = _excerpt + ("\n…（招标文件正文已截断，请基于可见部分提取）" if _truncated else "")

    prompt = f"""请检查招标文件中是否有以下目录章节涉及但未提取的关键信息。

已有知识条目：
{existing_titles_text}

目录章节：
{section_titles_text}

招标文件正文（取材来源，只允许从中提取、禁止编造）：
{_bid_source}

请从上述招标文件正文中提取与这些章节相关、但尚未包含在已有知识条目中的关键信息。

输出格式：JSON 数组，每个元素包含：
- title: 知识条目标题
- content: 知识条目详细内容
- category: 分类
- keywords: 关键词数组

只返回 JSON 数组，不要包含任何解释或 markdown 围栏。"""


    messages = [
        # BUG-FIX-PROMPT-E6（同第一轮）：走 get_prompt()，保留硬编码 fallback。
        {"role": "system", "content": _extract_sys_prompt()},
        {"role": "user", "content": prompt},
    ]

    try:
        response = await ai_call(messages)
        response_str = str(response).strip()

        if response_str.startswith("```"):
            lines = response_str.split('\n')
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            response_str = '\n'.join(lines)

        data = json.loads(response_str)
        if not isinstance(data, list):
            return RefillResult(round=2, success=False, error="Invalid JSON response")

        new_items: List[KnowledgeItem] = []
        existing_titles_set = {_normalize_title(item.title) for item in existing_items}
        existing_contents_set = {_normalize_title(item.content[:100]) for item in existing_items}

        for item_data in data:
            if not isinstance(item_data, dict):
                continue

            title = item_data.get("title", "")
            content = item_data.get("content", "")
            if not title or not content:
                continue

            item = KnowledgeItem(
                id=f"refill_{len(new_items) + 1}",
                title=title,
                content=content,
                category=item_data.get("category", "其他"),
                keywords=item_data.get("keywords", []),
                source="refill_round_2",
            )

            if _is_duplicate(item, existing_titles_set, existing_contents_set):
                continue

            existing_titles_set.add(_normalize_title(item.title))
            existing_contents_set.add(_normalize_title(item.content[:100]))
            new_items.append(item)

        return RefillResult(
            round=2,
            success=True,
            new_items=new_items,
            total_items=len(existing_items) + len(new_items),
        )

    except Exception as e:
        return RefillResult(round=2, success=False, error=str(e))


def merge_knowledge_items(
    existing: List[KnowledgeItem],
    new_items: List[KnowledgeItem],
) -> List[KnowledgeItem]:
    """合并知识条目，自动去重。"""
    existing_titles: Set[str] = {_normalize_title(item.title) for item in existing}
    existing_contents: Set[str] = {_normalize_title(item.content[:100]) for item in existing}

    merged = list(existing)

    for item in new_items:
        if _is_duplicate(item, existing_titles, existing_contents):
            continue

        existing_titles.add(_normalize_title(item.title))
        existing_contents.add(_normalize_title(item.content[:100]))
        merged.append(item)

    return merged


def row_to_knowledge_item(row: Dict[str, Any]) -> KnowledgeItem:
    """把知识库表行（dict）映射为 :class:`KnowledgeItem`。

    ENHANCE 2026-10-11（引入项 #16）：主链路从 `knowledge_items` 表读到的条目
    是带 `keywords`/`tags` JSON 的 dict，而 `refill_knowledge_gaps` 需要
    `KnowledgeItem`；此映射层让模块与 DB schema 解耦。
    """
    keywords = row.get("keywords") or []
    if isinstance(keywords, str):
        try:
            import json as _json

            keywords = _json.loads(keywords)
        except (ValueError, TypeError):
            keywords = []
    return KnowledgeItem(
        id=str(row.get("id", "")),
        title=str(row.get("title") or ""),
        content=str(row.get("content") or ""),
        source=str(row.get("source_ref") or row.get("source_doc_id") or ""),
        category=str(row.get("category") or ""),
        keywords=list(keywords) if isinstance(keywords, list) else [],
    )


async def build_refill_preview(
    provider: Any,
    outline: List[Dict[str, Any]],
    existing_rows: List[Dict[str, Any]],
    bid_text: str,
) -> Dict[str, Any]:
    """把 provider 适配成 `refill_knowledge_gaps` 的 ai_call，产出前端可消费的预览。

    ENHANCE 2026-10-11（引入项 #16）：目录成稿后的"两轮补漏"预览。按用户决策，
    **仅返回预览、不写 `knowledge_items` 表**——补漏结果作为一条 SSE 事件下发，
    由用户在前端决定是否采纳，杜绝自动污染长期知识库。

    Returns:
        `{"success": bool, "count": int, "items": [{title,content,category,keywords}], "error": str|None}`
    """
    from .providers.base import AIMessage

    async def _ai_call(messages: List[Dict[str, str]]):
        amessages = [AIMessage(role=m.get("role", "user"), content=m.get("content", "")) for m in messages]
        raw = await provider.chat(amessages, max_tokens=1500, temperature=0.2)
        if isinstance(raw, dict):
            return raw.get("content", str(raw))
        return raw

    existing_items = [row_to_knowledge_item(r) for r in (existing_rows or [])]
    result = await refill_knowledge_gaps(outline, existing_items, bid_text or "", _ai_call)
    items = [
        {
            "title": it.title,
            "content": it.content,
            "category": it.category,
            "keywords": it.keywords,
        }
        for it in result.new_items
    ]
    return {
        "success": result.success,
        "count": len(items),
        "items": items,
        "error": result.error,
    }


