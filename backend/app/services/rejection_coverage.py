"""废标条款应答覆盖率（E-6，文章 A3「一一对应」+ A1「事实底稿」在废标模块的复用）。

## 与既有「废标项检查」的分工

| | 废标项检查（既有） | 本模块（应答覆盖率） |
|---|---|---|
| 输入 | 招标文件 + 投标文件全文，检「是否出现废标风险点」 | 最新一次废标检查的 check_items（clauses）|
| 输出 | 疑似问题点列表 | 每个废标条款 → 投标正文的**应答覆盖率**（覆盖/薄弱/未答）|
| 用途 | 定位风险 | 量化「风险是否被正文正面回应」，指导补答章节 |

## 判定口径

确定性字符 bigram 重合度（与 scoring_compliance 同一思路，不调 AI、无超时的
可复现指标）：对每个废标条款文本与项目全部正文求字符二元组集合重合率。

- ``coverage >= threshold`` → covered（正文明显承接了该条款要求）
- 0 < coverage < threshold → weak（有提及但不够正面）
- coverage == 0 且条款非空 → empty（完全未应答）

覆盖率低≠一定废标，但它是「该条款要求有没有在被正文正面回应」的量化提示，
配合废标项检查的问题点一起看，能避免「查出了风险却没在方案里补答」的断层。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from ..db import get_db
from .consistency_repair import _load_all_project_content


logger = logging.getLogger(__name__)

#: 与 scoring_compliance.COVERAGE_WEAK 同量级的默认薄弱阈值
COVERAGE_WEAK = 0.25

#: 参与「命中最强章节」统计的条款上限（其余不逐个扫章节）
_TOP_CLAUSES = 30


def _char_bigrams(text: str) -> set[str]:
    """中文友好的字符二元组集合（去掉空白后）。"""
    compact = "".join(text.split())
    if len(compact) < 2:
        return {compact} if compact else set()
    return {compact[i:i + 2] for i in range(len(compact) - 1)}


def _extract_clauses(results: Any) -> list[dict]:
    """从 rejection_checks.results 的多种形态里提取条款文本（防御式解析）。"""
    clauses: list[dict] = []

    def _push(raw: dict) -> None:
        if not isinstance(raw, dict):
            return
        text = (
            str(raw.get("clause") or raw.get("standard") or raw.get("requirement")
               or raw.get("name") or raw.get("title") or raw.get("item") or "").strip()
        )
        if len(text) < 4:
            return
        clauses.append({
            "id": str(raw.get("id") or raw.get("item_id") or len(clauses) + 1),
            "name": str(raw.get("name") or raw.get("title") or "")[:120],
            "text": text,
            "raw": raw,
        })

    if isinstance(results, list):
        for item in results:
            _push(item)
        return clauses

    if isinstance(results, dict):
        for key in ("items", "check_items", "clauses", "results", "findings"):
            value = results.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        _push(item)
                if clauses:
                    return clauses
            elif isinstance(value, dict):
                for v in value.values():
                    if isinstance(v, dict):
                        _push(v)
                if clauses:
                    return clauses
        # 再兜底：直接是 {item_id: {…}} 形态
        for v in results.values():
            if isinstance(v, dict):
                _push(v)
    return clauses


def rejection_answer_coverage(
    project_id: int, threshold: float = COVERAGE_WEAK
) -> dict:
    """计算每个废标条款在投标正文中的应答覆盖率（E-6）。

    Args:
        project_id: 项目ID
        threshold: 低于该覆盖率判 weak（默认取服务层标定值）

    Returns:
        {status, scanned_clauses, full_chars, items:[{id,name,status,coverage,
         section_hits:[{section_id,title,matched}]}], weak_count, empty_count}
        status = no_checks（无已完成检查任务）/ done
    """
    with get_db() as conn:
        row = conn.execute(
            "SELECT results FROM rejection_checks "
            "WHERE project_id = ? AND status = 'completed' ORDER BY id DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        content_by_id = _load_all_project_content(conn, project_id)

    if row is None or not row.get("results"):
        return {
            "status": "no_checks",
            "message": "该项目尚无已完成的大数据废标检查结果，无法计算应答覆盖率",
            "scanned_clauses": 0,
        }

    try:
        results = json.loads(row["results"] or "{}")
    except json.JSONDecodeError:
        results = {}

    clauses = _extract_clauses(results)
    if not clauses:
        return {"status": "no_checks", "scanned_clauses": 0,
                "message": "废标检查结果中没有可解析的检查条款"}

    full_text_bigrams: set[str] = set()
    for content in content_by_id.values():
        if content:
            full_text_bigrams |= _char_bigrams(content)

    full_chars = sum(len(c or "") for c in content_by_id.values())
    items: list[dict] = []
    weak_count = empty_count = 0
    for clause in clauses[:200]:
        cbg = _char_bigrams(clause["text"])
        if not cbg:
            continue
        matched = len(cbg & full_text_bigrams)
        coverage = round(matched / len(cbg), 4)
        if coverage >= threshold:
            status = "covered"
        elif coverage > 0:
            status = "weak"
            weak_count += 1
        else:
            status = "empty"
            empty_count += 1

        section_hits: list[dict] = []
        if len(items) < _TOP_CLAUSES and coverage > 0:
            scored = []
            for sid, content in content_by_id.items():
                if not content:
                    continue
                n = len(_char_bigrams(content) & cbg)
                if n > 1:
                    scored.append((n, sid))
            scored.sort(key=lambda x: -x[0])
            for n, sid in scored[:3]:
                section_hits.append({"section_id": sid, "matched_bigrams": n})

        items.append({
            "id": clause["id"],
            "name": clause["name"] or clause["text"][:60],
            "text": clause["text"][:200],
            "status": status,
            "coverage": coverage,
            "section_hits": section_hits[:3],
        })

    return {
        "status": "done",
        "scanned_clauses": len(items),
        "full_chars": full_chars,
        "threshold": threshold,
        "weak_count": weak_count,
        "empty_count": empty_count,
        "items": items,
    }