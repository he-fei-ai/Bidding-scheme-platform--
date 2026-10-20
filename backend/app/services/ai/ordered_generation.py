"""Ordered content generation utilities (2026-10-18 ENHANCE).

Provides batch-based ordered generation, previous section context extraction,
and post-generation order verification for the content generation pipeline.

Design principles:
  * Ordered generation follows the directory tree sort order (sort_order, path).
  * Batch mode: consecutive sections sharing the same parent are generated
    in parallel within a batch; batches are processed sequentially.
  * Serial mode: one section at a time, with previous section summary injected.
  * Parallel mode: legacy behavior (asyncio.gather all at once), backward compatible.
  * Previous section summaries are extracted from completed content and injected
    into subsequent sections for natural context continuation.

2026-10-18 第二轮增强（正文生成模块）：
  * ``tree_sort_key`` / ``sort_sections_by_tree_order`` —— 目录树顺序的唯一权威
    排序实现（按 path 前序 + sort_order + id 稳定决胜），修复「按全局 sort_order
    排序导致跨父节点跳章乱序」的缺陷。排序规则由目录生成模块写入（sort_order），
    正文生成只读不重排。
  * ``GenerationOrderTracker`` —— 记录**实际**完成顺序（含并发/重试），供生成后
    校验「实际生成顺序 vs 目录树顺序」。
  * ``find_resume_point`` —— 断点续生成：定位目录树顺序上第一个未完成节点。
  * ``extract_standards_from_text`` —— 从招标文本确定性抽取适用规范清单
    （GB/JGJ/CJJ/CECS 等标准编号），作为上下文注入源。
  * ``verify_fact_invocation`` —— 生成后校验「章节 → 所需事实」映射表调用完整性
    （不遗漏 / 不错映射 / 与全局事实不冲突）。
   * ``audit_message_stack`` —— 注入前/后上下文校验（层级齐全、长度未超限、未被截断）。
   * ``sibling_sort_key`` / ``order_tree_dfs`` / ``load_sections_in_tree_order``
     —— 目录树顺序在**落库、展示、导出、正文生成**四方的唯一共享实现：
     同级 ``(sort_order, id)``、父先于子（深度优先前序），不依赖 path 字段，
     消除 ``ORDER BY level, sort_order``（广度优先 + 跨父节点 sort_order 交错）
     与 ``ORDER BY id``（创建序，仅碰巧正确）造成的顺序不一致。
   * ``find_duplicate_sibling_sort_orders`` —— 脏数据告警：同父节点下
     ``sort_order`` 重复（排序稳定性的前置条件）。
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any


logger = logging.getLogger(__name__)

#: 前序章节摘要层的固定标记（注入端与校验端唯一事实源）。
#:
#: 历史缺陷：`inject_previous_section_context` 与 `audit_message_stack(require_previous)`
#: 各自硬编码同一串中文文案，任一端改写文案都会导致 `missing_previous_context`
#: 误报（顺序生成明明注入了，校验却说没注入）。现统一收敛到本常量双向引用。
PREVIOUS_SECTIONS_SUMMARY_MARKER = "前序章节内容摘要"
#: 前序章节摘要层的完整表头（仅注入端使用；校验端只认上面的短标记）。
PREVIOUS_SECTIONS_SUMMARY_HEADER = (
    f"{PREVIOUS_SECTIONS_SUMMARY_MARKER}（请自然承接，避免重复，确保行文连贯）:\n"
)


# ============================================================
# 一、目录树顺序排序（正文生成顺序的唯一权威实现）
# ============================================================
#
# 排序字段：节点 ID、编号（path）、父节点 ID、兄弟节点序号（sort_order）。
# 排序规则统一由目录生成模块写入（sections.sort_order 1 起连续），正文生成只读不重排。
#
# 为什么不能按「全局 sort_order」排：sort_order 是**同父节点下**的兄弟序号，
# 不同父节点下的 sort_order 会重复（每个 L2 的第一个子节 sort_order 都是 1）。
# 2026-10-18 首轮增强曾误用 `(sort_order, path)` 作排序键，导致两个 L2 子树
# 交错排序（1.1.1, 1.2.1, 1.1.2, 1.2.2）——即「跳章乱序」。正确口径：
# 编号 path 是位置派生编号（child_number 逐级拼接 sort_order），按 path 元组
# 升序即严格的深度优先前序（L1→L7 层级顺序、同级按编号升序）。


def _path_tuple(path: Any) -> tuple:
    """把 "1.2.3" 形态编号转为可比较的整数元组（容错非法值）。"""
    parts = str(path or "0").split(".")
    out: list[int] = []
    for p in parts:
        try:
            out.append(int(p))
        except (TypeError, ValueError):
            out.append(99999)
    return tuple(out)


def _to_int(value: Any, default: int = 0) -> int:
    """容错整型转换（None / 非数字 → default）。"""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _get_field(section: Any, key: str) -> Any:
    """兼容 dict 与对象（pydantic SectionTreeNode / SimpleNamespace）取字段。

    排序工具被**叶子 dict 列表**（正文生成）与**对象树节点**（build_tree / 导出）
    共同复用，统一走此入口避免两套取字段口径。
    """
    if isinstance(section, dict):
        return section.get(key)
    return getattr(section, key, None)


def sibling_sort_key(section: Any) -> tuple:
    """同级排序键（目录生成写入、下游只读的**唯一权威兄弟序号口径**）。

    Returns:
        (sort_order, id)：``sort_order`` 由目录生成模块写入（同父节点下 1 起
        连续），``id`` 为自增主键、单调递增，作决胜键保证排序稳定。
        选用 id 而非 created_at：created_at 秒级精度可能并列，id 不会。
    """
    return (_to_int(_get_field(section, "sort_order")), _to_int(_get_field(section, "id")))


def tree_sort_key(section: Any) -> tuple:
    """目录树顺序排序键（稳定、确定、可复现）。

    Returns:
        (path_tuple, sort_order, id)：path 为主键（前序遍历），
        sort_order 与 id 仅在同 path 前缀下决胜，保证排序结果稳定。

    入参既可为 dict（叶子 dict 列表），也可为对象节点（见 ``_get_field``）。
    """
    _so, id_val = sibling_sort_key(section)
    return (_path_tuple(_get_field(section, "path")), _so, id_val)


def order_tree_dfs(sections: list) -> list:
    """按目录树深度优先前序重排扁平章节列表（**不依赖 path 字段**）。

    口径：同级按 ``(sort_order, id)`` 升序（:func:`sibling_sort_key`），父节点
    严格先于其子节点（前序遍历）。与 :func:`tree_sort_key` 的 path 元组升序
    等价 —— path 由同级序号逐级派生 —— 但无需预先计算 path，因此可供
    SQL 扁平加载场景（导出正文、HTML 展示、缓存指纹）复用同一排序口径，
    保证「落库顺序 = 展示顺序 = 导出顺序 = 正文生成顺序」。

    前置约束：排序规则由目录生成模块写入（``sections.sort_order``），
    本函数只读不重排值。排序结果稳定：同输入任意次排序输出一致。

    防御：
      * 孤儿节点（``parent_id`` 指向不存在的父）挂到根层参与排序；
      * ``parent_id`` 环不会造成死循环（visited 守卫）；
      * 环 / 断链残留节点按同级键追加到末尾 —— **输出集合与输入一致，不丢节点**。

    Returns:
        新列表（不修改入参）。
    """
    items = list(sections or [])
    if not items:
        return []

    by_id = {_get_field(s, "id"): s for s in items}
    children: dict[Any, list] = {}
    roots: list = []
    for s in items:
        pid = _get_field(s, "parent_id")
        if pid is None or pid not in by_id:
            roots.append(s)
        else:
            children.setdefault(pid, []).append(s)

    for bucket in (roots, *children.values()):
        bucket.sort(key=sibling_sort_key)

    out: list = []
    seen: set = set()
    stack: list = list(reversed(roots))
    while stack:
        node = stack.pop()
        nid = _get_field(node, "id")
        if nid in seen:
            continue
        seen.add(nid)
        out.append(node)
        for child in reversed(children.get(nid, [])):
            if _get_field(child, "id") not in seen:
                stack.append(child)

    if len(out) != len(items):  # pragma: no cover - 仅脏数据（环/断链）触发
        leftovers = [s for s in items if _get_field(s, "id") not in seen]
        leftovers.sort(key=sibling_sort_key)
        out.extend(leftovers)
    return out


def load_sections_in_tree_order(
    conn: Any,
    project_id: int,
    columns: str | None = None,
) -> list[dict]:
    """按目录树顺序加载项目全部章节（SQL 扁平加载 + DFS 重排）。

    用于导出正文、HTML 展示、导出缓存指纹等「直接消费扁平行序」的场景，
    确保它们与正文生成共用 :func:`order_tree_dfs` 这一唯一排序实现，
    消除 ``ORDER BY level, sort_order``（广度优先 + 跨父节点 sort_order 交错）
    与 ``ORDER BY id``（创建序，仅碰巧正确）导致的顺序不一致。

    Args:
        conn: 已打开的数据库连接（sqlite3.Connection）。
        project_id: 项目 ID。
        columns: SELECT 列表达式；缺省取正文/展示所需的核心列。

    Returns:
        按目录树前序排列的 dict 列表。
    """
    cols = columns or (
        "id, project_id, parent_id, title, level, sort_order, content"
    )
    rows = conn.execute(
        f"SELECT {cols} FROM sections WHERE project_id = ?", (project_id,)
    ).fetchall()
    return order_tree_dfs([dict(r) for r in rows])


def find_duplicate_sibling_sort_orders(rows: list) -> list[dict]:
    """脏数据告警：同父节点下 ``sort_order`` 重复或不连续。

    排序稳定性依赖「同父节点下 sort_order 唯一」。重复时决胜落到 id（结果仍
    确定），但与目录生成写入的兄弟序号语义脱节，须在生成/导出前可观测。

    Returns:
        [{"parent_id", "sort_order", "count"}...]（无问题时空列表）。
    """
    groups: dict[Any, dict[int, int]] = {}
    for r in rows or []:
        pid = _get_field(r, "parent_id")
        so = _to_int(_get_field(r, "sort_order"))
        groups.setdefault(pid, {})[so] = groups.setdefault(pid, {}).get(so, 0) + 1
    out: list[dict] = []
    for pid, counts in groups.items():
        for so, n in counts.items():
            if n > 1:
                out.append({"parent_id": pid, "sort_order": so, "count": n})
    return out


def audit_children_map(rows: list, children_map: dict) -> dict:
    """校验「父 ID → 子节点列表」桶表的完整性与桶内顺序（只读，不修复）。

    `children_map` 是 `_get_path_and_ancestors` 派生编号 path 的唯一依据
    （path = 节点在同级列表中的下标 +1）。一旦桶表被**其它代码路径**绕过
    `_load_project_data` 的显式排序直接构建，path 编号就会错位，正文生成
    顺序随之错乱 —— 而这类错位在生成结果里极难察觉。

    校验项：
      1. 覆盖性：全部行必须恰好出现在一个桶里（不丢节点、不重复）；
      2. 桶内顺序：每个桶必须已按唯一权威 sibling_sort_key 有序；
      3. 兄弟序号唯一性：桶内 sort_order 不得重复。

    Returns:
        {"ok": bool, "issues": [{"type", "detail"}...],
         "duplicates": [...], "total_rows": int, "bucket_total": int}
    """
    issues: list[dict] = []
    children_map = children_map or {}

    seen: dict[Any, int] = {}
    bucket_total = 0
    for bucket in children_map.values():
        for s in bucket or []:
            sid = _get_field(s, "id")
            seen[sid] = seen.get(sid, 0) + 1
            bucket_total += 1

    total_rows = len(rows or [])
    if bucket_total != total_rows:
        issues.append({
            "type": "bucket_total_mismatch",
            "detail": f"children_map 桶内节点数 {bucket_total} != 全量行数 {total_rows}",
        })
    for s in rows or []:
        sid = _get_field(s, "id")
        if seen.get(sid, 0) != 1:
            issues.append({
                "type": "node_coverage",
                "detail": f"节点 id={sid} 在 children_map 中出现 {seen.get(sid, 0)} 次（应为 1）",
            })

    for pid, bucket in children_map.items():
        keys = [sibling_sort_key(s) for s in (bucket or [])]
        if any(b < a for a, b in zip(keys, keys[1:])):
            issues.append({
                "type": "bucket_not_sorted",
                "detail": f"父节点 {pid} 的子节点桶未按 sibling_sort_key 排序，"
                          f"path 编号派生将错位",
            })

    duplicates = find_duplicate_sibling_sort_orders(
        [dict(s) if not isinstance(s, dict) else s for s in (rows or [])]
    )
    if duplicates:
        issues.append({
            "type": "duplicate_sibling_sort_order",
            "detail": f"同父 sort_order 重复 {len(duplicates)} 组，"
                      f"决胜将回落到 id（建议运行 repair_duplicate_sibling_sort_orders）",
        })

    ok = not issues
    if not ok:
        logger.warning(
            "[children-map] 同级桶表自检发现问题 %d 项: %s",
            len(issues), [i["type"] for i in issues],
        )
    return {
        "ok": ok,
        "issues": issues,
        "duplicates": duplicates,
        "total_rows": total_rows,
        "bucket_total": bucket_total,
    }


def repair_duplicate_sibling_sort_orders(conn, project_id: int, *, dry_run: bool = True) -> dict:
    """一次性修复同父节点下 sort_order 重复的历史脏数据。

    口径：在每个父节点组内**保持现有权威顺序**（sibling_sort_key = sort_order, id），
    把重复的组重排为 1..N 连续序号；无重复的组不动（最小改动面）。
    修复后「同父 sort_order 唯一」这一排序稳定性前置条件恢复，下游可继续
    只读不重排。

    Args:
        conn: sqlite 连接（调用方负责事务边界；本函数不 commit）。
        project_id: 项目 ID。
        dry_run: True 时只计算不写库（默认，安全）。

    Returns:
        {"dry_run", "groups_repaired", "rows_updated", "changes"}
    """
    rows = conn.execute(
        "SELECT id, parent_id, sort_order FROM sections "
        "WHERE project_id = ? ORDER BY parent_id, sort_order, id",
        (project_id,),
    ).fetchall()

    changes: list[dict] = []
    groups: dict[Any, list[dict]] = {}
    for r in rows:
        pid = r["parent_id"]
        groups.setdefault(pid, []).append({
            "id": r["id"],
            "sort_order": _to_int(r["sort_order"]),
        })

    for pid, members in groups.items():
        orders = [m["sort_order"] for m in members]
        if len(set(orders)) == len(orders):
            continue  # 该组序号已唯一，不动
        for idx, member in enumerate(members, 1):
            if member["sort_order"] != idx:
                changes.append({
                    "id": member["id"],
                    "parent_id": pid,
                    "old": member["sort_order"],
                    "new": idx,
                })

    groups_repaired = len({c["parent_id"] for c in changes})
    if changes and not dry_run:
        conn.executemany(
            "UPDATE sections SET sort_order = ? WHERE id = ? AND project_id = ?",
            [(c["new"], c["id"], project_id) for c in changes],
        )

    return {
        "dry_run": dry_run,
        "groups_repaired": groups_repaired,
        "rows_updated": 0 if dry_run else len(changes),
        "changes": changes,
    }


def sort_sections_by_tree_order(sections: list[dict]) -> list[dict]:
    """按目录树顺序排序章节列表（不修改入参，返回新列表）。

    排序结果必须稳定：同一输入任意次排序输出一致（Python sort 稳定 +
    决胜键含 id）。用于正文生成的叶子节点列表、批次划分与断点续生。
    """
    return sorted(sections, key=tree_sort_key)


def verify_tree_order(sections: list[dict]) -> dict:
    """校验给定列表是否已按目录树顺序排列（只读校验，不重排）。

    Returns:
        {"ok": bool, "violations": [{"index", "id", "path"}...]} —— 供
        生成前/生成后对「落库顺序、展示顺序、导出顺序」做一致性告警。
    """
    violations: list[dict] = []
    prev_key: tuple | None = None
    for i, sec in enumerate(sections):
        key = tree_sort_key(sec)
        if prev_key is not None and key < prev_key:
            violations.append({
                "index": i,
                "id": sec.get("id"),
                "path": sec.get("path"),
                "title": sec.get("title", ""),
            })
        prev_key = key
    return {"ok": not violations, "violations": violations}


def find_resume_point(ordered_ids: list, completed_ids) -> dict:
    """断点续生成：定位目录树顺序上第一个未完成节点。

    规则：记录最后成功节点，从**下一节点**按顺序继续；不随机挑选、不跳章。

    Returns:
        {"next_index": int, "next_id": Any, "last_completed_id": Any,
         "remaining": int, "all_completed": bool}
    """
    done = set(completed_ids or [])
    last_completed = None
    for idx, sid in enumerate(ordered_ids or []):
        if sid in done:
            last_completed = sid
            continue
        return {
            "next_index": idx,
            "next_id": sid,
            "last_completed_id": last_completed,
            "remaining": len(ordered_ids) - idx,
            "all_completed": False,
        }
    return {
        "next_index": len(ordered_ids or []),
        "next_id": None,
        "last_completed_id": last_completed,
        "remaining": 0,
        "all_completed": True,
    }


def extract_section_summary(content: str, max_chars: int = 200) -> str:
    """Extract a short summary from generated section content.

    Takes the first `max_chars` characters, stripping markdown formatting
    and collapsing whitespace for compact injection into subsequent prompts.
    """
    if not content:
        return ""
    # Strip markdown headers, bold, code blocks for compactness
    text = content.strip()
    # Remove markdown code fences
    text = re.sub(r"```[\s\S]*?```", "[图表]", text)
    # Remove inline markdown formatting
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"\*(.+?)\*", r"\1", text)
    text = re.sub(r"#{1,6}\s*", "", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars] + "…"
    return text


def build_generation_batches(
    sections: list[dict],
    max_batch_size: int = 4,
) -> list[list[dict]]:
    """Group sections into ordered batches for sequential batch processing.

    Strategy:
      - Consecutive sections sharing the same parent_id form a batch.
      - Batches are capped at `max_batch_size` sections.
      - Sections with different parent_ids are split into separate batches.
      - Result preserves the original section order.

    前置约束：入参 **必须已按目录树顺序排序**（调用方使用
    ``sort_sections_by_tree_order``）。批次按目录树顺序切分，不跨批次乱序；
    批次内节点属于同一父节点或相邻节点；批次完成后按顺序合并。

    Args:
        sections: List of section dicts (already sorted by tree order).
        max_batch_size: Maximum number of sections per batch.

    Returns:
        List of batches, where each batch is a list of section dicts.
    """
    if not sections:
        return []

    batches: list[list[dict]] = []
    current_batch: list[dict] = []
    current_parent: Any = None

    for section in sections:
        pid = section.get("parent_id")
        if not current_batch:
            current_batch.append(section)
            current_parent = pid
        elif pid == current_parent and len(current_batch) < max_batch_size:
            current_batch.append(section)
        else:
            batches.append(current_batch)
            current_batch = [section]
            current_parent = pid

    if current_batch:
        batches.append(current_batch)

    logger.info(
        "[ordered-gen] built %d batches from %d sections (max_batch_size=%d)",
        len(batches), len(sections), max_batch_size,
    )
    return batches


def inject_previous_section_context(
    context: dict,
    prev_summaries: list[dict],
) -> dict:
    """Inject previous section summaries into the generation context.

    Creates a compact representation of previously generated sections
    for natural context continuation. 本函数是「前序章节摘要」注入的
    **唯一实现**（生产调度与测试共用），避免多处内联导致行为漂移。

    Args:
        context: The existing context dict (will be modified).
        prev_summaries: List of {"title": str, "summary": str} dicts
            from previously completed sections.

    Returns:
        The modified context dict (also returned for convenience).
    """
    if not prev_summaries:
        context.pop("previous_sections_summary", None)
        return context

    # Keep only the most recent 5 sections to avoid context bloat
    recent = prev_summaries[-5:]
    lines = []
    for i, prev in enumerate(recent, 1):
        title = prev.get("title", "").strip()
        summary = prev.get("summary", "").strip()
        if title and summary:
            lines.append(f"  {i}. [{title}] {summary}")

    if lines:
        context["previous_sections_summary"] = (
            PREVIOUS_SECTIONS_SUMMARY_HEADER
            + "\n".join(lines)
        )
    else:
        context.pop("previous_sections_summary", None)

    return context


# ============================================================
# 二、实际生成顺序追踪（并发/重试下的真实完成序列）
# ============================================================


class GenerationOrderTracker:
    """记录章节的**实际**完成顺序（含并发生成与重试）。

    生成完成后与目录树期望顺序比对：
      * serial / batch 模式：不一致 → error（顺序铁律被破坏）；
      * parallel 模式：天然不保证顺序，不一致 → warning（记录审计）。

    判重口径：只有同一章节**成功完成 ≥2 次**才记为重复生成（双计费）；
    失败后重试成功是正常恢复路径，不计重复。
    """

    def __init__(self) -> None:
        self._events: list[dict] = []

    def record(self, section_id: Any, status: str = "completed") -> None:
        self._events.append({
            "id": section_id,
            "status": status,
            "ts": time.monotonic(),
        })

    @property
    def sequence(self) -> list:
        """按实际完成顺序的 section_id 序列（全部事件，含失败与重试）。"""
        return [e["id"] for e in self._events]

    @property
    def completed_sequence(self) -> list:
        """仅成功完成的事件序列（顺序校验与判重的权威口径）。"""
        return [e["id"] for e in self._events if e.get("status") == "completed"]

    def first_occurrence_order(self) -> list:
        """去重（保留首次出现）的完成顺序。"""
        seen: set = set()
        out: list = []
        for sid in self.completed_sequence:
            if sid not in seen:
                seen.add(sid)
                out.append(sid)
        return out

    def duplicates(self) -> list:
        """成功完成 ≥2 次的章节（真正的重复生成）。"""
        counts: dict = {}
        for sid in self.completed_sequence:
            counts[sid] = counts.get(sid, 0) + 1
        return [sid for sid, n in counts.items() if n >= 2]

    def verify(self, expected_ids: list, mode: str = "parallel") -> dict:
        """比对实际完成顺序与目录树期望顺序。

        Returns:
            {"ok", "mode", "order_mismatch", "missing", "extra",
             "duplicates", "expected", "actual"}
        """
        return self._verify(self.completed_sequence, expected_ids, mode=mode)

    @staticmethod
    def _verify(actual_sequence: list, expected_ids: list, mode: str = "parallel") -> dict:
        gen_set = set(actual_sequence)
        exp_set = set(expected_ids or [])
        missing = [sid for sid in (expected_ids or []) if sid not in gen_set]
        extra = [sid for sid in actual_sequence if sid not in exp_set]

        # 重复生成检测（保留首次出现的去重序列做顺序比对）
        seen: set = set()
        dups: list = []
        for sid in actual_sequence:
            if sid in seen and sid not in dups:
                dups.append(sid)
            seen.add(sid)

        actual_order: list = []
        seen2: set = set()
        for sid in actual_sequence:
            if sid not in seen2:
                seen2.add(sid)
                actual_order.append(sid)

        expected_common = [sid for sid in (expected_ids or []) if sid in gen_set]
        order_mismatch = actual_order != expected_common

        # parallel 模式天然不保证完成顺序：集合完整性仍须满足，顺序仅告警
        if mode == "parallel":
            ok = not missing and not extra
            level = "warn" if order_mismatch else "info"
        else:
            ok = not missing and not extra and not order_mismatch and not dups
            level = "error" if (missing or extra or order_mismatch or dups) else "info"

        if not ok or order_mismatch:
            logger.warning(
                "[ordered-gen] 实际生成顺序校验(%s): mismatch=%s missing=%s extra=%s dups=%s",
                mode, order_mismatch, missing[:10], extra[:10], dups[:10],
            )
        return {
            "ok": ok,
            "mode": mode,
            "level": level,
            "order_mismatch": order_mismatch,
            "missing": missing,
            "extra": extra,
            "duplicates": dups,
            "expected": (expected_ids or [])[:20],
            "actual": actual_order[:20],
        }

    @staticmethod
    def verify_static(actual_order: list, expected_ids: list, mode: str = "parallel") -> dict:
        """无实例静态校验入口（仅持 id 序列时使用；无法区分失败/成功事件，
        重复 id 一律按重复生成上报，供 verify_generation_completeness 兜底）。"""
        return GenerationOrderTracker._verify(actual_order, expected_ids, mode=mode)


# ============================================================
# 三、适用规范清单抽取（上下文注入源）
# ============================================================

#: 常见国标/行业标准编号（GB、GB/T、JGJ、JGJ/T、CJJ、CECS、HJ、SL、NB、DL、JB、TB、JC）
_STANDARD_CODE_RE = re.compile(
    r"(?P<code>(?:GB|JGJ|CJJ|CECS|HJ|SL|NB|DL|JB|TB|JC|QB|YB|SG|CY)(?:/T)?\s?\d{2,5}[-\u2013]\d{4})"
)


def extract_standards_from_text(text: str | None, max_items: int = 30) -> list[dict]:
    """从招标文件文本确定性抽取适用规范/标准清单。

    只认标准编号形态（GB 50720-2011 等），不做语义猜测；同一编号去重，
    按首次出现顺序返回。作为正文生成的「适用规范清单」上下文来源，
    无匹配时返回空列表（调用方跳过注入，行为不变）。

    Returns:
        [{"code": "GB 50720-2011", "title": "建设工程施工现场消防安全技术规范"}...]
    """
    if not text or not isinstance(text, str):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for m in _STANDARD_CODE_RE.finditer(text):
        code = re.sub(r"\s+", " ", m.group("code")).strip()
        norm = code.replace(" ", "")
        if norm in seen:
            continue
        seen.add(norm)
        title = ""
        # 在编号后的小窗口内找《标题》
        window = text[m.end():m.end() + 80]
        tm = re.match(r"\s*[《<](?P<t>[^》>]{2,60})[》>]", window)
        if tm:
            title = tm.group("t").strip()
        out.append({"code": code, "title": title})
        if len(out) >= max_items:
            break
    return out


# ============================================================
# 四、全局事实调用校验（章节 → 所需事实 映射表完整性）
# ============================================================


def verify_fact_invocation(
    results: list[dict],
    field_contract: dict | None = None,
    field_trace: list[dict] | None = None,
    facts: list | None = None,
) -> dict:
    """生成后校验全局事实调用（二.3 调用完整性 / 二.4 调用一致性）。

    校验项：
      1. 每章按 CHAPTER_FIELD_MAP 应注入的事实字段是否**完整注入**（不遗漏）；
      2. 注入字段是否有值（不截断/不错映射到空值）；
      3. 正文与全局数据字典是否冲突（调用一致性）。

    Args:
        results: generate_sections 返回的结果列表（含 title/content）。
        field_contract: build_field_contract 产物（含 global_dictionary）。
        field_trace: build_chapter_content_messages 记录的字段调用追溯。
        facts: 全局事实列表（未用，保留供将来审计扩展）。

    Returns:
        {"ok", "issues", "warnings", "chapters": {title: detail}}
    """
    issues: list[dict] = []
    warnings: list[dict] = []
    chapters: dict[str, dict] = {}

    try:
        from ...services.content_field_contract import (
            CHAPTER_FIELD_MAP,
            _field_label,
            check_content_consistency,
            classify_chapter,
        )
    except Exception:  # pragma: no cover - 防御性
        return {"ok": True, "issues": [], "warnings": [], "chapters": {}}

    trace = field_trace or []
    # chapter title -> {(source, field_key)}
    traced: dict[str, set] = {}
    for t in trace:
        if not isinstance(t, dict):
            continue
        ch = t.get("chapter", "")
        src = t.get("source", "")
        fld = t.get("field", "")
        traced.setdefault(ch, set()).add((src, fld))

    for r in results or []:
        if not isinstance(r, dict):
            continue
        title = r.get("title", "")
        content = r.get("content") or ""
        if not title:
            continue
        ch = classify_chapter(title)
        idx = ch["idx"]
        mapping = CHAPTER_FIELD_MAP.get(idx, {})
        required_fact_keys = mapping.get("facts", [])
        called = traced.get(title, set())
        called_keys = {fld for _, fld in called}

        detail = {
            "chapter_idx": idx,
            "required_facts": [],
            "called_facts": sorted(called_keys),
            "not_called": [],
            "conflicts": [],
        }
        for fk in required_fact_keys:
            lbl = _field_label(fk)
            detail["required_facts"].append(f"{lbl}({fk})")
            # 调用判定：field key 或中文标签任一出现在追溯记录中
            if fk not in called_keys and lbl not in called_keys:
                detail["not_called"].append(f"{lbl}({fk})")

        # 一致性：正文 vs 全局数据字典
        if content and field_contract and field_contract.get("global_dictionary"):
            conflicts = check_content_consistency(
                content, field_contract["global_dictionary"]
            )
            if conflicts:
                detail["conflicts"] = conflicts
                for c in conflicts:
                    issues.append({
                        "type": "fact_conflict",
                        "level": "error",
                        "chapter": title,
                        "detail": f"{c.get('field')} 全局事实值「{c.get('expected')}」"
                                  f"与正文「{c.get('actual')}」冲突",
                    })

        if detail["not_called"]:
            warnings.append({
                "type": "fact_not_called",
                "level": "warn",
                "chapter": title,
                "detail": f"章节「{title}」映射事实未完整注入: {detail['not_called']}",
            })
        chapters[title] = detail

    return {
        "ok": not issues,
        "issues": issues,
        "warnings": warnings,
        "chapters": chapters,
    }


# ============================================================
# 五、上下文注入校验（注入前齐全 / 注入后未超限 / 注入后被使用）
# ============================================================

#: 单章节提示词总长度软上限（字符）。超过记 warning（不阻断，防上下文过载）。
MESSAGE_STACK_SOFT_LIMIT = 24000
#: 单条消息长度软上限
MESSAGE_SOFT_LIMIT = 8000


def audit_message_stack(
    messages: list,
    chapter_info: dict | None = None,
    field_contract: dict | None = None,
    require_previous: bool = False,
) -> dict:
    """对组装完成的分层消息栈做注入校验（三.3）。

    校验项：
      1. system 层存在且非空；
      2. 稳定上下文层存在（项目摘要/字典/规范等至少一项）；
      3. 任务层（最后一条 user 消息）存在且包含章节标题；
      4. 总长度与单条长度未超软上限（防过载注入）；
      5. 存在截断标记（"…"）时记录（防关键数据被截断）；
      6. 顺序生成场景要求前序摘要层存在（require_previous=True）。

    Returns:
        {"ok", "issues", "warnings", "total_chars", "layers", "truncated"}
    """
    issues: list[dict] = []
    warnings: list[dict] = []
    chapter_info = chapter_info or {}
    title = chapter_info.get("title", "")

    roles = [getattr(m, "role", None) for m in messages]
    contents = [getattr(m, "content", "") or "" for m in messages]
    total_chars = sum(len(c) for c in contents)
    truncated = any("…" in c for c in contents)

    if not messages or roles[0] != "system" or not contents[0].strip():
        issues.append({"type": "missing_system", "level": "error",
                       "detail": "system 层缺失或为空"})
    if len(messages) < 2:
        issues.append({"type": "missing_layers", "level": "error",
                       "detail": f"消息层数不足: {len(messages)}"})

    # 稳定上下文层：第 2 条 user 消息（存在多条时）
    if len(messages) >= 2 and roles[1] == "user" and not contents[1].strip():
        warnings.append({"type": "empty_stable_context", "level": "warn",
                         "detail": "稳定上下文层为空（无项目摘要/字典/规范）"})

    # 任务层必须包含章节标题
    task_text = contents[-1] if contents else ""
    if title and title not in task_text:
        issues.append({"type": "task_layer_missing_title", "level": "error",
                       "detail": "任务层未包含当前章节标题"})

    if total_chars > MESSAGE_STACK_SOFT_LIMIT:
        warnings.append({"type": "context_overload", "level": "warn",
                         "detail": f"提示词总长 {total_chars} 超过软上限 "
                                   f"{MESSAGE_STACK_SOFT_LIMIT}"})
    for i, c in enumerate(contents):
        if len(c) > MESSAGE_SOFT_LIMIT:
            warnings.append({"type": "message_overload", "level": "warn",
                             "detail": f"第 {i} 层长度 {len(c)} 超过软上限 "
                                       f"{MESSAGE_SOFT_LIMIT}"})
    if truncated:
        warnings.append({"type": "truncated_context", "level": "warn",
                         "detail": "消息栈存在截断标记（…），请确认关键数据未被截断"})

    if require_previous:
        joined = "\n".join(contents)
        if PREVIOUS_SECTIONS_SUMMARY_MARKER not in joined:
            warnings.append({"type": "missing_previous_context", "level": "warn",
                              "detail": "顺序生成模式下前序章节摘要未注入"})

    return {
        "ok": not issues,
        "issues": issues,
        "warnings": warnings,
        "total_chars": total_chars,
        "layers": len(messages),
        "truncated": truncated,
    }


# ============================================================
# 六、生成后综合校验（顺序 / 完整性 / 质量 / 字段契约）
# ============================================================


def verify_generation_completeness(
    results: list[dict],
    expected_section_ids: list[int],
    field_contract: dict | None = None,
    actual_order: list | None = None,
    generation_mode: str = "parallel",
    field_trace: list[dict] | None = None,
    facts: list | None = None,
    order_tracker: Any = None,
) -> dict:
    """Comprehensive post-generation verification.

    Checks:
      1. Order verification (actual vs expected, mode-aware)
      2. Completeness (all sections generated)
      3. Content quality (non-empty, reasonable length)
      4. Field contract compliance (facts used correctly)
      5. Fact invocation completeness (章节 → 所需事实 映射表)

    Args:
        results: Generation results list.
        expected_section_ids: Expected section IDs in order.
        field_contract: Field contract dict (optional).
        actual_order: Actual completion order (list of ids). None → 回落按
            results 顺序（旧行为，仅覆盖索引对齐场景）。
        generation_mode: parallel / batch / serial.
        field_trace: 字段调用追溯（供事实调用完整性校验）。
        facts: 全局事实列表（可选）。
        order_tracker: GenerationOrderTracker 实例（优先于 actual_order，
            可区分失败/成功事件，避免把「失败后重试成功」误判为重复生成）。

    Returns:
        dict with verification results.
    """
    issues: list[dict] = []
    warnings: list[dict] = []

    # 1. Order verification（实际完成顺序 vs 目录树顺序）
    tracker_result: dict | None = None
    if order_tracker is not None and hasattr(order_tracker, "verify"):
        tracker_result = order_tracker.verify(
            expected_section_ids, mode=generation_mode
        )
    elif actual_order is not None:
        tracker_result = GenerationOrderTracker.verify_static(
            actual_order, expected_section_ids, mode=generation_mode
        )
    if tracker_result is not None:
        if not tracker_result["ok"]:
            issues.append({
                "type": "order_mismatch",
                "level": "error",
                "detail": f"实际生成顺序与目录树顺序不一致({generation_mode}): "
                          f"{tracker_result}",
            })
        elif tracker_result["order_mismatch"]:
            warnings.append({
                "type": "order_mismatch_parallel",
                "level": "warn",
                "detail": f"并发({generation_mode})模式下实际完成顺序与目录树顺序不一致"
                          f"（集合完整，顺序不保证）",
            })
        if tracker_result.get("duplicates"):
            warnings.append({
                "type": "duplicate_generation",
                "level": "warn",
                "detail": f"重复生成章节: {tracker_result['duplicates']}",
            })
    else:
        # 旧行为：按 results 顺序校验（索引对齐场景）
        generated_ids = [r.get("id") for r in results if r.get("id") is not None]
        tracker_result = verify_generation_order(generated_ids, expected_section_ids)
        if not tracker_result["ok"]:
            issues.append({
                "type": "order_mismatch",
                "level": "error",
                "detail": f"生成顺序与目录树顺序不一致: {tracker_result}",
            })

    # 2. Completeness check
    gen_ids = {r.get("id") for r in results if r.get("id") is not None}
    missing = [sid for sid in expected_section_ids if sid not in gen_ids]
    if missing:
        issues.append({
            "type": "missing_sections",
            "level": "error",
            "detail": f"缺失章节: {missing}",
        })

    # 3. Content quality check
    for r in results:
        content = r.get("content", "")
        title = r.get("title", "")
        if not content or len(content.strip()) < 30:
            issues.append({
                "type": "too_short",
                "level": "error",
                "detail": f"章节 '{title}' 内容过短 ({len(content)} 字)",
            })
        elif len(content) > 10000:
            warnings.append({
                "type": "too_long",
                "level": "warn",
                "detail": f"章节 '{title}' 内容过长 ({len(content)} 字)",
            })

    # 4. Field contract compliance（与全局数据字典冲突 / 占位符）
    if field_contract and field_contract.get("global_dictionary"):
        dictionary = field_contract["global_dictionary"]
        for r in results:
            content = r.get("content", "")
            title = r.get("title", "")
            if not content:
                continue
            try:
                from ...services.content_field_contract import (
                    check_content_consistency,
                    check_placeholder_in_content,
                )
                conflicts = check_content_consistency(content, dictionary)
                if conflicts:
                    issues.append({
                        "type": "consistency_conflict",
                        "level": "error",
                        "detail": f"章节 '{title}' 与全局事实冲突: {conflicts}",
                    })
                placeholders = check_placeholder_in_content(content)
                if placeholders:
                    issues.append({
                        "type": "placeholder",
                        "level": "warn",
                        "detail": f"章节 '{title}' 包含占位符: {placeholders}",
                    })
            except Exception:
                pass

    # 5. 全局事实调用完整性（章节 → 所需事实 映射表）
    fact_report = verify_fact_invocation(results, field_contract, field_trace, facts)
    issues.extend(fact_report["issues"])
    warnings.extend(fact_report["warnings"])

    return {
        "ok": len(issues) == 0,
        "issues": issues,
        "warnings": warnings,
        "total_sections": len(results),
        "expected_sections": len(expected_section_ids),
        "order": tracker_result,
        "facts": {
            "ok": fact_report["ok"],
            "chapters": fact_report["chapters"],
        },
    }


def verify_generation_order(
    generated_ids: list[int],
    expected_ids: list[int],
) -> dict:
    """Verify that actual generation order matches expected tree order.

    Args:
        generated_ids: Section IDs in the order they were actually generated.
        expected_ids: Section IDs in the expected directory tree order.

    Returns:
        dict with verification results:
            {"ok", "mismatches", "missing", "extra", "duplicates"}
    """
    gen_set = set(generated_ids)
    exp_set = set(expected_ids)

    missing = [sid for sid in expected_ids if sid not in gen_set]
    extra = [sid for sid in generated_ids if sid not in exp_set]

    # 重复生成检测（重试未去重场景）
    seen: set = set()
    duplicates: list = []
    for sid in generated_ids:
        if sid in seen and sid not in duplicates:
            duplicates.append(sid)
        seen.add(sid)

    # Check order of common elements
    common = [sid for sid in generated_ids if sid in exp_set]
    expected_common = [sid for sid in expected_ids if sid in gen_set]
    order_mismatch = common != expected_common

    ok = not missing and not extra and not order_mismatch and not duplicates

    return {
        "ok": ok,
        "mismatches": [] if not order_mismatch else {
            "expected": expected_common[:20],
            "actual": common[:20],
        },
        "missing": missing,
        "extra": extra,
        "duplicates": duplicates,
    }


# ============================================================
# 七、全局事实按「章节 → 所需事实」映射选择（显式映射优先）
# ============================================================


def select_facts_by_chapter_map(
    chapter_title: str,
    facts: list[dict],
    extraction_fields: dict | None = None,
    industry: str | None = None,
) -> list[dict]:
    """Select facts for a chapter using explicit CHAPTER_FIELD_MAP.

    This replaces the keyword-scoring-based fact selection with a deterministic
    mapping from chapter title to required fact fields, ensuring complete and
    correct fact invocation.

    Args:
        chapter_title: The chapter title to classify.
        facts: List of global fact dicts.
        extraction_fields: Pre-extracted project fields (optional).
        industry: Industry name for chapter classification.

    Returns:
        List of fact dicts that are relevant to this chapter.
    """
    try:
        from ...services.content_field_contract import (
            _EXTRACTION_BY_KEY,
            _FACT_ONLY_LABELS,
            CHAPTER_FIELD_MAP,
            FACT_ONLY_KEYWORDS,
            _match_fact,
            _norm_text,
            classify_chapter,
        )
    except Exception:
        return []

    ch = classify_chapter(chapter_title, industry=industry)
    idx = ch["idx"]
    mapping = CHAPTER_FIELD_MAP.get(idx, {"facts": []})
    fact_keys = mapping.get("facts", [])
    if not fact_keys:
        return []

    extraction_fields = extraction_fields or {}
    selected: list[dict] = []
    seen_labels: set[str] = set()

    for fk in fact_keys:
        meta = _EXTRACTION_BY_KEY.get(fk)
        lbl = meta["label"] if meta else _FACT_ONLY_LABELS.get(fk, fk)
        if lbl in seen_labels:
            continue

        # Try extraction fields first
        if meta and extraction_fields.get(fk):
            val = _norm_text(extraction_fields[fk])
            if val:
                selected.append({
                    "key": fk,
                    "label": lbl,
                    "value": val,
                    "source": "extraction",
                })
                seen_labels.add(lbl)
                continue

        # Try matching from facts
        if meta:
            val, prov = _match_fact(facts, meta)
        else:
            # 仅事实字段：用宽关键词表（标签窄匹配会漏调人员/设备类事实）
            kw = FACT_ONLY_KEYWORDS.get(fk) or [_FACT_ONLY_LABELS.get(fk, fk)]
            val, prov = _match_fact(facts, {"fact_category": None, "fact_keywords": kw})

        if val:
            # BUG-FIX 2026-10-18（第二轮）：注入标签优先保留**事实原名**
            # （如用户确认的「总工期」），而非字段名「工期」—— 事实原名是
            # 用户在全球事实页确认的口径，正文引用它更不易被判定冲突；
            # 无溯源信息时回落字段名。
            fact_lbl = ""
            if isinstance(prov, dict):
                fact_lbl = _norm_text(prov.get("fact_label"))
            out_lbl = fact_lbl or lbl
            if out_lbl in seen_labels:
                continue
            selected.append({
                "key": fk,
                "label": out_lbl,
                "value": val,
                "source": "facts",
            })
            seen_labels.add(out_lbl)

    return selected


# ============================================================
# 八、章节位置与九大章节必含要素（提示词注入）
# ============================================================


def build_chapter_position_context(
    chapter_info: dict,
    all_sections: list[dict],
) -> str:
    """Build chapter position context (第几章第几节) for prompt injection.

    Args:
        chapter_info: The current chapter info dict.
        all_sections: All section dicts for context.

    Returns:
        A string describing the chapter's position in the tree.
    """
    parts = []
    ancestors = chapter_info.get("ancestors") or []
    level = int(chapter_info.get("level", 3) or 3)
    path = chapter_info.get("path", "")

    if path:
        parts.append(f"章节编号: {path}")
    if ancestors:
        parts.append(f"上级章节: {' → '.join(ancestors)}")
    parts.append(f"层级: L{level}")

    # Count siblings for position info
    siblings = chapter_info.get("sibling_chapters") or []
    if siblings:
        parts.append(f"同级章节数: {len(siblings)}")

    return "；".join(parts)


def build_nine_chapter_elements(
    chapter_title: str,
    industry: str | None = None,
) -> str:
    """Build nine-chapter required elements list for prompt injection.

    Returns a list of required elements that each chapter type must include.

    Args:
        chapter_title: The chapter title to classify.
        industry: Industry name.

    Returns:
        A string listing required elements for this chapter type.
    """
    try:
        from ...services.content_field_contract import classify_chapter
    except Exception:
        return ""

    ch = classify_chapter(chapter_title, industry=industry)
    idx = ch["idx"]
    theme = ch["theme"]

    elements_map = {
        1: ["工程概况", "编制依据", "编制原则", "工程特点分析"],
        2: ["施工部署", "组织机构", "项目管理架构", "职责分工"],
        3: ["施工方法", "施工工艺", "技术参数", "质量控制要点"],
        4: ["专项方案", "安全措施", "应急预案", "专家论证"],
        5: ["进度计划", "工期保证措施", "进度控制", "里程碑节点"],
        6: ["劳动力计划", "机械设备", "材料计划", "资金使用计划"],
        7: ["质量目标", "质量管理体系", "质量控制措施", "验收标准"],
        8: ["安全目标", "安全管理体系", "文明施工措施", "环境保护"],
        9: ["应急预案", "风险识别", "风险控制措施", "应急资源"],
    }

    elements = elements_map.get(idx, [])
    if not elements:
        return ""

    lines = [f"本章主题: {theme}（第{idx}章）"]
    lines.append("必含要素:")
    for i, elem in enumerate(elements, 1):
        lines.append(f"  {i}. {elem}")
    return "\n".join(lines)
