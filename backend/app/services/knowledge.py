"""知识库服务层

实现 9 步幂等流水线 + 增强 2.1 向量索引：
1. copy_source            复制原件到知识库存储目录
2. convert_markdown       转换为 Markdown
3. build_blocks           切块 + 语义合并 + 筛除无价值块
4. extract_first_items    AI 首次提取候选知识条目
5. extract_supplement_items AI 补充提取
6. merge_candidates       合并去重
7. match_batches          分批 AI 匹配补全正文
8. recover_missing        补漏
9. save_result            保存到知识库表
10. vector_index (增强 2.1) 向量索引到 ChromaDB
"""

from datetime import UTC, datetime
import json
import logging
from pathlib import Path
import re
import shutil
from typing import Any

from ..config import (
    KNOWLEDGE_DATA_DIR,
    KNOWLEDGE_MARKDOWN_DIR,
    KNOWLEDGE_SOURCE_DIR,
)
from ..db import get_db, safe_update
from .ai.prompts import get_prompt, sanitize_prompt_value
from .ai.provider_factory import create_provider, get_ai_config
from .ai.providers.base import AIMessage
from .ai.workflows import collect_json_response
from .files import extract_text_from_path


logger = logging.getLogger(__name__)


# ========== 文档管理 ==========


def create_document(filename: str, file_path: str, file_type: str, industry: str = "") -> int:
    """创建知识库文档记录"""
    with get_db() as conn:
        cursor = conn.execute(
            """INSERT INTO knowledge_documents
               (filename, file_path, file_type, industry, status, step_log, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'pending', '[]', datetime('now', 'localtime'), datetime('now', 'localtime'))""",
            (filename, file_path, file_type, industry),
        )
        return cursor.lastrowid


def get_document(doc_id: int) -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM knowledge_documents WHERE id = ?", (doc_id,)).fetchone()
        if not row:
            return None
        return _row_to_dict(row)


def list_documents(limit: int = 50) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM knowledge_documents ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]


def list_document_summaries(limit: int = 200) -> list[dict]:
    """文档级摘要（可研报告「参考知识库」选材用）。

    ENHANCE 2026-09-15：返回条目数与素材字数，便于用户判断该文档是否值得选。
    """
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT d.id, d.filename, d.status, d.industry,
                   COUNT(i.id) AS item_count,
                   COALESCE(SUM(LENGTH(i.content)), 0) AS item_chars
            FROM knowledge_documents d
            LEFT JOIN knowledge_items i ON i.source_doc_id = d.id
            GROUP BY d.id
            ORDER BY d.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [
        {
            "id": int(r["id"]),
            "filename": r["filename"] or "",
            "status": r["status"] or "",
            "industry": r["industry"] or "",
            "itemCount": int(r["item_count"] or 0),
            "itemChars": int(r["item_chars"] or 0),
        }
        for r in rows
    ]


def list_items_by_document_ids(document_ids, limit: int = 500) -> list[dict]:
    """按来源文档批量取知识条目（可研选材：文档级选择 → 条目级素材）。

    ENHANCE 2026-09-15：本平台知识条目比易标更细（自带 title/summary/content），
    因此选材先按文档圈定范围，再在内存里按章节相关度挑选。
    """
    ids: list[int] = []
    for raw in document_ids or []:
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in ids:
            ids.append(value)
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT * FROM knowledge_items WHERE source_doc_id IN ({placeholders}) "
            "ORDER BY source_doc_id ASC, id ASC LIMIT ?",
            (*ids, limit),
        ).fetchall()
    result: list[dict] = []
    for row in rows:
        item = dict(row)
        for key in ("keywords", "tags"):
            raw = item.get(key)
            try:
                item[key] = json.loads(raw) if raw else []
            except (json.JSONDecodeError, TypeError):
                item[key] = []
        result.append(item)
    return result


def update_document(doc_id: int, **kwargs):
    """更新文档字段（列名白名单，防止动态列名拼接导致的 SQL 注入）"""
    _ALLOWED = {
        "status",
        "step_log",
        "blocks",
        "candidates",
        "match_result",
        "markdown_path",
        "coverage",
        "industry",
    }
    if not kwargs:
        return
    for k, v in kwargs.items():
        if k not in _ALLOWED:
            raise ValueError(f"不允许更新的字段: {k}")
        if k in ("step_log", "blocks", "candidates", "match_result"):
            kwargs[k] = json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else v

    with get_db() as conn:
        sql, params = safe_update("knowledge_documents", kwargs, "id", doc_id)
        conn.execute(sql, params)


def delete_document(doc_id: int):
    """删除文档及其知识条目"""
    with get_db() as conn:
        # 删除关联的知识条目
        conn.execute("DELETE FROM knowledge_items WHERE source_doc_id = ?", (doc_id,))
        # 删除文档记录
        conn.execute("DELETE FROM knowledge_documents WHERE id = ?", (doc_id,))


def _row_to_dict(row) -> dict:
    d = dict(row)
    for k in ("step_log", "blocks", "candidates"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except json.JSONDecodeError:
                d[k] = []
        else:
            d[k] = []
    if d.get("match_result"):
        try:
            d["match_result"] = json.loads(d["match_result"])
        except json.JSONDecodeError:
            d["match_result"] = {}
    else:
        d["match_result"] = {}
    return d


def _log_step(doc_id: int, step: str, status: str, message: str = "", data: Any = None):
    """记录流水线步骤（单事务原子操作，防止并发丢失更新）"""
    step_entry = {
        "step": step,
        "status": status,
        "message": message,
        "timestamp": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
        "data_summary": str(data)[:200] if data else "",
    }
    with get_db() as conn:
        row = conn.execute(
            "SELECT step_log FROM knowledge_documents WHERE id = ?",
            (doc_id,),
        ).fetchone()
        if not row:
            return
        step_log: list = json.loads(row["step_log"] or "[]")
        step_log.append(step_entry)
        conn.execute(
            "UPDATE knowledge_documents SET step_log = ? WHERE id = ?",
            (json.dumps(step_log, ensure_ascii=False), doc_id),
        )


# ========== 9 步流水线 ==========


async def run_pipeline(doc_id: int):
    """执行完整的 9 步流水线"""
    doc = get_document(doc_id)
    if not doc:
        return

    try:
        ai_config = get_ai_config()
        provider = create_provider(ai_config)
    except Exception as e:
        # knowledge_documents 表无 error 列，改为记录到 step_log 并标记错误状态
        _log_step(doc_id, "pipeline", "failed", f"AI 配置异常: {e}")
        update_document(doc_id, status="error")
        return

    try:
        # Step 1: copy_source
        update_document(doc_id, status="copying")
        _log_step(doc_id, "copy_source", "started")
        copied_path = _step_copy_source(doc)
        _log_step(doc_id, "copy_source", "completed", f"复制到 {copied_path}")

        # Step 2: convert_markdown
        update_document(doc_id, status="converting")
        _log_step(doc_id, "convert_markdown", "started")
        markdown_path, markdown_text = _step_convert_markdown(doc, copied_path)
        update_document(doc_id, markdown_path=str(markdown_path))
        _log_step(doc_id, "convert_markdown", "completed", f"转换完成，长度 {len(markdown_text)}")

        # Step 3: build_blocks
        update_document(doc_id, status="extracting")
        _log_step(doc_id, "build_blocks", "started")
        blocks = _step_build_blocks(markdown_text)
        update_document(doc_id, blocks=blocks)
        _log_step(doc_id, "build_blocks", "completed", f"切块 {len(blocks)} 个")

        # Step 4: extract_first_items
        _log_step(doc_id, "extract_first_items", "started")
        first_items = await _step_extract_items(provider, blocks, doc.get("industry", ""))
        _log_step(doc_id, "extract_first_items", "completed", f"提取 {len(first_items)} 个候选")

        # Step 5: extract_supplement_items
        _log_step(doc_id, "extract_supplement_items", "started")
        supplement_items = await _step_extract_supplement(
            provider,
            blocks,
            first_items,
            doc.get("industry", ""),
        )
        _log_step(
            doc_id,
            "extract_supplement_items",
            "completed",
            f"补充提取 {len(supplement_items)} 个",
        )

        # Step 6: merge_candidates
        _log_step(doc_id, "merge_candidates", "started")
        candidates = _step_merge_candidates(first_items + supplement_items)
        update_document(doc_id, candidates=candidates)
        _log_step(doc_id, "merge_candidates", "completed", f"合并后 {len(candidates)} 个")

        # Step 7: match_batches
        update_document(doc_id, status="matching")
        _log_step(doc_id, "match_batches", "started")
        matched = await _step_match_batches(provider, blocks, candidates)
        update_document(doc_id, match_result={"matched": matched})
        _log_step(doc_id, "match_batches", "completed", f"匹配 {len(matched)} 个")

        # Step 8: recover_missing
        _log_step(doc_id, "recover_missing", "started")
        recovered = await _step_recover_missing(provider, blocks, matched)
        _log_step(doc_id, "recover_missing", "completed", f"补漏 {len(recovered)} 个")

        # Step 9: save_result
        update_document(doc_id, status="saving")
        _log_step(doc_id, "save_result", "started")
        coverage, saved_items = _step_save_result(doc_id, recovered, blocks)
        update_document(doc_id, status="success", coverage=coverage)
        _log_step(doc_id, "save_result", "completed", f"覆盖率 {coverage:.2%}")

        # 增强 2.1: Step 10 - 向量索引（RAG 知识库索引）
        if saved_items:
            _log_step(doc_id, "vector_index", "started")
            try:
                _step_vector_index(doc_id, saved_items, doc.get("industry", ""))
                _log_step(doc_id, "vector_index", "completed", f"向量索引 {len(saved_items)} 条")
            except Exception as ve:
                logger.warning("[RAG] 向量索引失败: %s", ve)
                _log_step(doc_id, "vector_index", "failed", str(ve))

    except Exception as e:
        import traceback

        traceback.print_exc()
        update_document(doc_id, status="error")
        _log_step(doc_id, "pipeline", "failed", str(e))


# ========== Step 1: copy_source ==========


def _step_copy_source(doc: dict) -> str:
    """复制原件到知识库存储目录"""
    KNOWLEDGE_SOURCE_DIR.mkdir(parents=True, exist_ok=True)
    src_path = Path(doc["file_path"])
    if not src_path.exists():
        raise FileNotFoundError(f"原始文件不存在: {src_path}")

    # 使用 doc_id 避免重名
    dst = KNOWLEDGE_SOURCE_DIR / f"{doc['id']}_{src_path.name}"
    if dst.exists():
        # BUG-FIX-69: 区分文件和目录，目录用 rmtree 避免 unlink 失败
        if dst.is_file():
            dst.unlink()
        elif dst.is_dir():
            shutil.rmtree(dst)
    shutil.copy2(src_path, dst)
    return str(dst)


# ========== Step 2: convert_markdown ==========


def _step_convert_markdown(doc: dict, copied_path: str) -> tuple[Path, str]:
    """将文档转为 Markdown 格式"""
    KNOWLEDGE_MARKDOWN_DIR.mkdir(parents=True, exist_ok=True)

    # 简单实现：使用 extract_text_from_path 提取纯文本，再标记为 Markdown
    text = extract_text_from_path(copied_path)

    # 简单 Markdown 化：识别章节标题
    lines = text.split("\n")
    md_lines = []
    for line in lines:
        stripped = line.strip()
        # 识别"第X章"作为一级标题
        if re.match(r"^第[一二三四五六七八九十百零\d]+[章节部分篇]", stripped):
            md_lines.append(f"# {stripped}")
        # 识别数字编号 "1. xxx" 作为二级标题
        elif re.match(r"^\d+[\.\、]\s*\S+", stripped) and len(stripped) < 50:
            md_lines.append(f"## {stripped}")
        else:
            md_lines.append(stripped)

    markdown_text = "\n\n".join(md_lines)

    md_path = KNOWLEDGE_MARKDOWN_DIR / f"{doc['id']}_{Path(doc['filename']).stem}.md"
    md_path.write_text(markdown_text, encoding="utf-8")
    return md_path, markdown_text


# ========== Step 3: build_blocks ==========


def _step_build_blocks(text: str) -> list[dict]:
    """切块 + 语义合并 + 筛除无价值块"""
    if not text:
        return []

    # 按空行分段
    paragraphs = re.split(r"\n\s*\n", text)
    blocks = []
    current_block = ""
    block_idx = 0

    for _para in paragraphs:
        para = _para.strip()
        if not para:
            continue

        # 跳过无价值块
        if _is_valueless_block(para):
            continue

        # 合并过短块
        if len(current_block) < 100:
            current_block = f"{current_block}\n\n{para}".strip()
            continue

        # 当前块达到合理长度
        if current_block:
            blocks.append({"index": block_idx, "text": current_block, "type": "paragraph"})
            block_idx += 1
            current_block = para
        else:
            current_block = para

    if current_block:
        blocks.append({"index": block_idx, "text": current_block, "type": "paragraph"})

    return blocks


def _is_valueless_block(text: str) -> bool:
    """判断是否为无价值块（页码、目录、封面、签字页等）"""
    # 页码
    if re.match(r"^[\-\—\.\s]*\d+[\-\—\.\s]*$", text):
        return True
    # 纯数字
    if re.match(r"^\d+$", text):
        return True
    # 过短
    if len(text) < 10:
        return True
    # 目录关键字
    if re.match(r"^(目\s*录|Table of Contents|Contents)\s*$", text, re.IGNORECASE):
        return True
    # 签字页
    if "签字" in text and len(text) < 30:
        return True
    return False


# ========== Step 4 & 5: extract_items ==========


async def _step_extract_items(provider: Any, blocks: list[dict], industry: str = "") -> list[dict]:
    """AI 首次提取候选知识条目"""
    sys_prompt = get_prompt("KB_EXTRACT_ITEMS_SYSTEM")

    # 分批处理
    batch_size = 5
    all_items = []
    for i in range(0, len(blocks), batch_size):
        batch = blocks[i : i + batch_size]
        batch_text = "\n\n".join(f"[块 {b['index']}]\n{b['text']}" for b in batch)
        # SECURITY-FIX 2026-10-06：batch_text 为用户上传文档内容（外部内容），
        # 手动拼入 user_prompt 前做元标记中和，防御提示词注入。
        safe_batch = sanitize_prompt_value(batch_text)
        user_prompt = (
            f"行业：{industry or '通用'}\n\n请从以下文档块中提取知识条目：\n\n{safe_batch}"
        )
        messages = [
            AIMessage(role="system", content=sys_prompt),
            AIMessage(role="user", content=user_prompt),
        ]
        try:
            result = await collect_json_response(provider, messages, max_retries=2)
            if isinstance(result, list):
                for item in result:
                    item.setdefault("title", "")
                    item.setdefault("summary", "")
                    item.setdefault("keywords", [])
                all_items.extend(result)
        except Exception:
            logger.warning("AI首次提取候选条目失败", exc_info=True)
            continue
    return all_items


async def _step_extract_supplement(
    provider: Any,
    blocks: list[dict],
    existing_items: list[dict],
    industry: str = "",
) -> list[dict]:
    """AI 补充提取：对已提取的条目做二次扫描"""
    if not existing_items:
        return []

    sys_prompt = get_prompt("KB_EXTRACT_ITEMS_SYSTEM")

    # 已提取的标题集合
    existing_titles = {item.get("title", "") for item in existing_items if item.get("title")}

    # 对未匹配的块重新提取
    supplement_items = []
    batch_size = 8
    for i in range(0, len(blocks), batch_size):
        batch = blocks[i : i + batch_size]
        batch_text = "\n\n".join(f"[块 {b['index']}]\n{b['text']}" for b in batch)
        # SECURITY-FIX 2026-10-06：batch_text 为用户上传文档内容，做元标记中和。
        safe_batch = sanitize_prompt_value(batch_text)
        user_prompt = f"""行业：{industry or '通用'}

已提取的条目标题（避免重复）：
{json.dumps(list(existing_titles)[:50], ensure_ascii=False)}

请从以下文档块中提取**尚未被覆盖的**知识条目：

{safe_batch}

只返回新增的条目，避免与已提取的重复。"""
        messages = [
            AIMessage(role="system", content=sys_prompt),
            AIMessage(role="user", content=user_prompt),
        ]
        try:
            result = await collect_json_response(provider, messages, max_retries=2)
            if isinstance(result, list):
                supplement_items.extend(result)
                # BUG-FIX-75: 将本批次新提取标题加入已有集合，防止后续批次重复提取
                for item in result:
                    title = item.get("title", "") if isinstance(item, dict) else ""
                    if title:
                        existing_titles.add(title)
        except Exception:
            logger.warning("AI补充提取候选条目失败", exc_info=True)
            continue
    return supplement_items


# ========== Step 6: merge_candidates ==========


def _step_merge_candidates(candidates: list[dict]) -> list[dict]:
    """合并去重（按标题相似度）"""
    if not candidates:
        return []

    merged = []
    seen_titles = []
    for cand in candidates:
        title = cand.get("title", "").strip()
        if not title:
            continue

        is_dup = False
        for seen in seen_titles:
            if _text_similarity(title, seen) > 0.7:
                is_dup = True
                break

        if not is_dup:
            merged.append(cand)
            seen_titles.append(title)

    return merged


def _text_similarity(a: str, b: str) -> float:
    """改进的文本相似度：融合序列匹配与字符集，更适合中文

    策略：
    1. 精确匹配：完全相同 → 1.0
    2. 子串包含：一方包含另一方 → 较短者长度 / 较长者长度
    3. 序列相似度：基于公共子序列（LCS启发式）的有序匹配
    4. 字符集 Jaccard：兜底（权重较低）
    最终取 序列分 * 0.7 + 字符集分 * 0.3
    """
    if not a or not b:
        return 0
    a, b = a.strip(), b.strip()
    if a == b:
        return 1.0
    # 子串包含
    if a in b or b in a:
        return len(min(a, b, key=len)) / len(max(a, b, key=len))
    # 字符集 Jaccard（轻量兜底）
    set_a, set_b = set(a), set(b)
    jaccard = len(set_a & set_b) / len(set_a | set_b) if set_a | set_b else 0

    # 序列相似度：滑动窗口 bigram 重叠
    def _bigrams(s: str) -> set:
        return {s[i : i + 2] for i in range(len(s) - 1) if len(s) >= 2}

    a_big, b_big = _bigrams(a), _bigrams(b)
    if not a_big or not b_big:
        return jaccard
    bigram_overlap = len(a_big & b_big) / max(len(a_big), len(b_big))
    return bigram_overlap * 0.7 + jaccard * 0.3


# ========== Step 7: match_batches ==========


async def _step_match_batches(
    provider: Any,
    blocks: list[dict],
    candidates: list[dict],
) -> list[dict]:
    """分批 AI 匹配，补全正文内容"""
    if not candidates:
        return []

    sys_prompt = get_prompt("KB_MATCH_BLOCKS_SYSTEM")

    # 固定全文前缀（用于 prompt cache 优化）— 预序列化一次，所有批次复用
    full_blocks_text = json.dumps(
        [{"index": b["index"], "text": b["text"]} for b in blocks],
        ensure_ascii=False,
    )

    batch_size = 10
    matched = []
    for i in range(0, len(candidates), batch_size):
        batch = candidates[i : i + batch_size]
        batch_text = json.dumps(batch, ensure_ascii=False)

        # 多 user 消息：固定前缀 + 变量批次
        messages = [
            AIMessage(role="system", content=sys_prompt),
            AIMessage(role="user", content=f"全文块列表（固定）：\n{full_blocks_text}"),
            AIMessage(
                role="user",
                content=f"本批次候选条目（变量）：\n{batch_text}\n\n请匹配并返回完整 JSON 数组。",
            ),
        ]
        try:
            result = await collect_json_response(provider, messages, max_retries=2)
            if isinstance(result, list):
                matched.extend(result)
        except Exception:
            logger.warning("AI分批匹配候选条目失败", exc_info=True)
            continue
    return matched


# ========== Step 8: recover_missing ==========


async def _step_recover_missing(
    provider: Any,
    blocks: list[dict],
    matched: list[dict],
) -> list[dict]:
    """补漏：检查未匹配的块，重新尝试"""
    if not matched:
        return []

    # 找出 content 为空的条目
    missing = [m for m in matched if not m.get("content", "").strip()]
    if not missing:
        return matched

    sys_prompt = get_prompt("KB_MATCH_BLOCKS_SYSTEM")
    full_blocks_text = json.dumps(
        [{"index": b["index"], "text": b["text"]} for b in blocks],
        ensure_ascii=False,
    )
    messages = [
        AIMessage(role="system", content=sys_prompt),
        AIMessage(role="user", content=f"全文块列表（固定）：\n{full_blocks_text}"),
        AIMessage(
            role="user",
            content=f"以下条目未匹配到正文，请重新匹配：\n{json.dumps(missing, ensure_ascii=False)}",
        ),
    ]
    try:
        result = await collect_json_response(provider, messages, max_retries=2)
        if isinstance(result, list):
            # 用重新匹配的结果替换原条目
            recovered_map = {r.get("title"): r for r in result if r.get("content")}
            for m in matched:
                if not m.get("content", "").strip():
                    recovered = recovered_map.get(m.get("title"))
                    if recovered:
                        m["content"] = recovered["content"]
                        if recovered.get("source_ref"):
                            m["source_ref"] = recovered["source_ref"]
    except Exception:
        logger.warning("AI补漏恢复内容失败", exc_info=True)

    return matched


# ========== Step 9: save_result ==========


def _step_save_result(
    doc_id: int, matched: list[dict], blocks: list[dict]
) -> tuple[float, list[dict]]:
    """保存到知识库表，返回覆盖率和已保存条目列表"""
    doc = get_document(doc_id)
    industry = doc.get("industry", "") if doc else ""

    saved_count = 0
    saved_items = []
    with get_db() as conn:
        for item in matched:
            title = item.get("title", "").strip()
            content = item.get("content", "").strip()
            if not title or not content:
                continue
            keywords = item.get("keywords", [])
            if isinstance(keywords, str):
                try:
                    keywords = json.loads(keywords)
                except json.JSONDecodeError:
                    keywords = [keywords]

            conn.execute(
                """INSERT INTO knowledge_items
                   (title, summary, content, keywords, industry, tags, source_doc_id, source_ref, created_at)
                   VALUES (?, ?, ?, ?, ?, '[]', ?, ?, datetime('now', 'localtime'))""",
                (
                    title,
                    item.get("summary", ""),
                    content,
                    json.dumps(keywords, ensure_ascii=False),
                    industry,
                    doc_id,
                    item.get("source_ref", ""),
                ),
            )
            saved_count += 1
            saved_items.append(
                {
                    "id": str(conn.execute("SELECT last_insert_rowid()").fetchone()[0]),
                    "title": title,
                    "content": content,
                    "summary": item.get("summary", ""),
                    "industry": industry,
                }
            )

    coverage = saved_count / len(blocks) if blocks else 0
    return coverage, saved_items


# ========== 知识条目检索 ==========


def search_items(
    keyword: str = "",
    industry: str = "",
    tag: str = "",
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """检索知识条目"""
    sql = "SELECT * FROM knowledge_items WHERE 1=1"
    params = []

    # BUG-FIX: LIKE 通配符转义（与 industry_tree.py 保持一致）
    # 用户输入中的 % 和 _ 会被 SQL LIKE 当作通配符，必须转义
    def _like_escape(s: str) -> str:
        return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    if keyword:
        # BUG-FIX 2026-09-29：ESCAPE 子句在 SQLite 里只能附着于单个 LIKE，
        # 原写法 `(... OR ... OR ...) ESCAPE '\'` 把 ESCAPE 挂在括号组之后，
        # 触发 `near "ESCAPE": syntax error` —— 任何带关键字的知识检索都会 500，
        # 废标检查的 knowledge-suggestions 端点因此从未真正工作过。
        # 现逐个 LIKE 各带一个 ESCAPE，通配符转义语义保持不变。
        sql += (
            " AND (title LIKE ? ESCAPE '\\'"
            " OR content LIKE ? ESCAPE '\\'"
            " OR summary LIKE ? ESCAPE '\\')"
        )
        kw = f"%{_like_escape(keyword)}%"
        params.extend([kw, kw, kw])

    if industry:
        sql += " AND industry = ?"
        params.append(industry)

    if tag:
        sql += " AND tags LIKE ? ESCAPE '\\'"
        params.append(f"%{_like_escape(tag)}%")

    # 计算总数
    with get_db() as conn:
        count_sql = sql.replace("SELECT *", "SELECT COUNT(*) as cnt")
        total = conn.execute(count_sql, params).fetchone()["cnt"]

        sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = conn.execute(sql, params).fetchall()

        items = []
        for r in rows:
            d = dict(r)
            for k in ("keywords", "tags"):
                if d.get(k):
                    try:
                        d[k] = json.loads(d[k])
                    except json.JSONDecodeError:
                        d[k] = []
                else:
                    d[k] = []
            items.append(d)

    return {"total": total, "items": items, "limit": limit, "offset": offset}


def get_item(item_id: int) -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM knowledge_items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        for k in ("keywords", "tags"):
            if d.get(k):
                try:
                    d[k] = json.loads(d[k])
                except json.JSONDecodeError:
                    d[k] = []
            else:
                d[k] = []
        return d


def update_item(item_id: int, **kwargs) -> dict | None:
    """更新知识条目（列名白名单，防止 SQL 注入）"""
    _ALLOWED = {"title", "summary", "content", "keywords", "tags", "industry"}
    if not kwargs:
        return get_item(item_id)

    for k, v in kwargs.items():
        if k not in _ALLOWED:
            raise ValueError(f"不允许更新的字段: {k}")
        if k in ("keywords", "tags") and isinstance(v, list):
            kwargs[k] = json.dumps(v, ensure_ascii=False)

    with get_db() as conn:
        sql, params = safe_update("knowledge_items", kwargs, "id", item_id)
        conn.execute(sql, params)

    return get_item(item_id)


def delete_item(item_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM knowledge_items WHERE id = ?", (item_id,))
        conn.execute("DELETE FROM knowledge_usage WHERE item_id = ?", (item_id,))


def list_industries() -> list[str]:
    """列出所有行业分类"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT DISTINCT industry FROM knowledge_items WHERE industry != '' ORDER BY industry",
        ).fetchall()
        return [r["industry"] for r in rows]


def list_tags() -> list[str]:
    """列出所有标签"""
    with get_db() as conn:
        rows = conn.execute("SELECT tags FROM knowledge_items WHERE tags != '[]'").fetchall()
        all_tags = set()
        for r in rows:
            try:
                tags = json.loads(r["tags"])
                if isinstance(tags, list):
                    all_tags.update(tags)
            except json.JSONDecodeError:
                pass
        return sorted(all_tags)


# ========== 集成：目录增强 ==========


async def enhance_outline(
    provider: Any,
    current_outline: list[dict],
    industry: str = "",
    scoring_points: list | None = None,
) -> dict:
    """目录增强：补充缺失的三级目录"""
    sys_prompt = get_prompt("KB_OUTLINE_ENHANCE_SYSTEM")

    # 检索相关条目
    search_result = search_items(keyword="", industry=industry, limit=20)
    related_items = search_result["items"]

    user_prompt = f"""当前目录结构：
{json.dumps(current_outline, ensure_ascii=False, indent=2)}

行业：{industry or '通用'}
评分点：{json.dumps(scoring_points or [], ensure_ascii=False)}

知识库相关条目（参考）：
{json.dumps([{'id': i['id'], 'title': i['title'], 'summary': i.get('summary', '')} for i in related_items], ensure_ascii=False, indent=2)}

请补充缺失的三级目录条目。"""
    messages = [
        AIMessage(role="system", content=sys_prompt),
        AIMessage(role="user", content=user_prompt),
    ]
    try:
        return await collect_json_response(provider, messages, max_retries=2)
    except Exception:
        return {"additions": []}


# ========== 集成：正文生成素材 ==========


def get_relevant_items(
    industry: str = "",
    keywords: list[str] | None = None,
    limit: int = 5,
) -> list[dict]:
    """获取与项目相关的知识条目（用于正文生成）"""
    if keywords:
        # 按关键词检索
        results = []
        for kw in keywords[:3]:
            r = search_items(keyword=kw, industry=industry, limit=limit)
            results.extend(r["items"])
        # 去重
        seen = set()
        unique = []
        for item in results:
            if item["id"] not in seen:
                seen.add(item["id"])
                unique.append(item)
        return unique[:limit]
    return search_items(industry=industry, limit=limit)["items"]


def record_usage(item_id: int, project_id: int | None, section_id: int | None, used_in: str):
    """记录知识条目使用情况"""
    with get_db() as conn:
        conn.execute(
            """INSERT INTO knowledge_usage (item_id, project_id, section_id, used_in, created_at)
               VALUES (?, ?, ?, ?, datetime('now', 'localtime'))""",
            (item_id, project_id, section_id, used_in),
        )


# ========== 迁移工具 ==========


def migrate_from_v2() -> dict:
    """从旧版 index.json 迁移数据"""
    old_index_path = KNOWLEDGE_DATA_DIR / "index.json"
    if not old_index_path.exists():
        return {"migrated": False, "message": "未检测到旧版数据"}

    try:
        old_data = json.loads(old_index_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"migrated": False, "message": "旧版数据格式错误"}

    if not isinstance(old_data, list):
        return {"migrated": False, "message": "旧版数据不是数组"}

    migrated_count = 0
    skipped_count = 0
    for entry in old_data:
        if entry.get("status") != "success":
            skipped_count += 1
            continue

        title = entry.get("title", "").strip()
        content = entry.get("content", "").strip()
        if not title or not content:
            skipped_count += 1
            continue

        with get_db() as conn:
            conn.execute(
                """INSERT INTO knowledge_items
                   (title, summary, content, keywords, industry, tags, source_doc_id, source_ref, created_at)
                   VALUES (?, ?, ?, '[]', '', '[]', NULL, ?, datetime('now', 'localtime'))""",
                (
                    title,
                    entry.get("summary", ""),
                    content,
                    entry.get("source", ""),
                ),
            )
        migrated_count += 1

    # 备份旧版数据
    backup_path = old_index_path.with_suffix(".json.bak")
    # BUG-FIX-70: 备份文件冲突防护，若 .bak 已存在则追加数字后缀
    if backup_path.exists():
        counter = 1
        while backup_path.exists():
            backup_path = old_index_path.with_suffix(f".json.bak{counter}")
            counter += 1
    old_index_path.rename(backup_path)

    return {
        "migrated": True,
        "migrated_count": migrated_count,
        "skipped_count": skipped_count,
        "backup_path": str(backup_path),
    }


# ========== 增强 2.1: 向量索引 ==========


def _step_vector_index(doc_id: int, saved_items: list[dict], industry: str):
    """将知识条目向量化并存入 ChromaDB

    在 Step 9 保存完成后调用，为 RAG 检索建立向量索引。
    """
    if not saved_items:
        logger.info("[RAG] 无需向量索引：无新条目")
        return

    try:
        from .rag.embeddings import EmbeddingService
        from .rag.vector_store import VectorStore

        texts = [item["content"] for item in saved_items]
        embeddings = EmbeddingService().embed(texts)
        VectorStore().add_items(saved_items, embeddings)
        logger.info("[RAG] 向量索引完成: %s 条 (doc_id=%s)", len(saved_items), doc_id)
    except ImportError as e:
        logger.warning("[RAG] 向量索引模块未安装: %s", e)
    except Exception as e:
        logger.error("[RAG] 向量索引异常: %s", e)
