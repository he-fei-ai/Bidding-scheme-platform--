"""全文一致性 AI 补丁修复。

ENHANCE 2026-09-14：移植自易标 `contentGenerationTask.cjs` 的 `auditing` 阶段
（`buildConsistencyRepairMessages` / `normalizeConsistencyRepairResponse` /
`applyConsistencyRepairPatches`）。

## 与 `consistency_auditor` 的分工

| | consistency_auditor（宿主确定性路径） | 本模块（AI 语义路径） |
|---|---|---|
| 适用 | 同一事实被写成两种取值（工期 30 / 45） | 同一事实用**不同措辞**表达（正文「2026 年 5 月竣工」vs 事实「工期 90 天」） |
| 手段 | 把旧值统一成目标值（字符替换） | AI 读懂语义后产出 `{old_text, new_text}` 局部补丁 |
| 落文 | `sections.content` | `sections.content` |

## 关键安全设计

**AI 只产出局部补丁，正文改写由宿主完成。** 这是相对「让 AI 整段重写」的核心优势：

- AI 编造的 `old_text` 会被 `text_patch` 的唯一性校验拒绝（找不到 / 找到多处），
  幻觉**无法污染正文**；
- 补丁逐条错误隔离，单条失败不影响其余条；
- 无法安全修复的冲突由 AI 在 `unresolved` 里说明，不硬改。

## 冲突状态收敛

AI 路径的补丁不绑定具体 conflict_id（提示词里也没要求），所以**不靠补丁反推冲突已解决**。
改为落文后按**可验证判据**重判：某条冲突的多个取值中若仍有 ≥2 个能在正文里找到，
就保持未解决；只剩 ≤1 个才标已解决。这样不会出现「谎报修复」。
"""

from __future__ import annotations

from datetime import datetime
import json
import logging
from typing import Any

from ..db import get_db
from . import text_patch
from .ai.prompts import get_prompt
from .ai.provider_factory import create_provider, get_ai_config
from .ai.providers.base import AIMessage
from .ai.workflows import collect_json_response
from .facts_service import facts_for_generation


logger = logging.getLogger(__name__)


# 单批送审正文的字符上限。超了就按小节切批，避免一次塞爆上下文。
REPAIR_BATCH_CHARS = 24000


# ========== 提示词拼装 ==========


def build_facts_text(facts: Any) -> str:
    """把全局事实渲染成提示词文本。兼容 `{label,value}` 与 `{key,value}` 两种结构。"""
    lines = []
    for fact in facts or []:
        if not isinstance(fact, dict):
            continue
        label = str(fact.get("label") or fact.get("key") or "").strip()
        value = str(fact.get("value") or "").strip()
        if label or value:
            lines.append(f"- {label}：{value}")
    return "\n".join(lines) if lines else "未提供"


def build_conflicts_text(conflicts: Any) -> str:
    """把冲突记录渲染成提示词文本。"""
    lines = []
    for conflict in conflicts or []:
        if isinstance(conflict, dict):
            name = conflict.get("fact_name") or ""
            values = conflict.get("values") or []
            description = conflict.get("description") or ""
        else:
            name = getattr(conflict, "fact_name", "")
            values = getattr(conflict, "values", []) or []
            description = getattr(conflict, "description", "") or ""
        lines.append(f"- {name}：{', '.join(str(v) for v in values)}（{description}）")
    return "\n".join(lines) if lines else "未提供"


def build_repair_user_prompt(*, sections: list[dict], conflicts_text: str, facts_text: str) -> str:
    """拼装送审 user prompt：事实变量 + 冲突清单 + 带行号的正文。

    带行号是为了让 AI 能填 `start_line/end_line` 作为加速线索；
    `text_patch.normalize_patch_text` 会自动剥掉 AI 抄回来的行号前缀。
    """
    allowed_ids = [str(section.get("node_id") or section.get("id") or "") for section in sections]
    blocks = []
    for section in sections:
        section_id = str(section.get("node_id") or section.get("id") or "")
        blocks.append(
            f"<section>\n编号：{section_id}\n标题：{section.get('title') or '未命名章节'}\n"
            f"正文：\n{text_patch.format_content_with_line_numbers(section.get('content') or '')}\n</section>"
        )
    return (
        f"事实变量（正文必须与之一致）：\n{facts_text}\n\n"
        f"审计发现的冲突：\n{conflicts_text}\n\n"
        f"待修复小节正文（带行号，patch 的 old_text/new_text 不要包含行号）：\n\n"
        + "\n\n".join(blocks)
        + f"\n\npatches[*].section_id 必须是以下编号之一：{'、'.join(allowed_ids) or '无'}\n请只返回 JSON。"
    )


def plan_batches(sections: list[dict], batch_chars: int = REPAIR_BATCH_CHARS) -> list[list[dict]]:
    """按字符预算把待修小节分批。单个小节超预算时自成一批（不切分正文，避免破坏补丁定位）。"""
    batches: list[list[dict]] = []
    current: list[dict] = []
    used = 0
    for section in sections:
        size = len(str(section.get("content") or ""))
        if current and used + size > batch_chars:
            batches.append(current)
            current = []
            used = 0
        current.append(section)
        used += size
    if current:
        batches.append(current)
    return batches


# ========== AI 调用 ==========


async def generate_patches(
    provider: Any,
    *,
    sections: list[dict],
    conflicts_text: str,
    facts_text: str,
) -> tuple[list[dict], list[dict], str | None]:
    """调用 AI 生成补丁。

    Returns:
        `(patches, unresolved, error)`；出错时 `patches=[]`、`error` 为原因。
    """
    allowed_ids = {str(s.get("node_id") or s.get("id") or "") for s in sections}
    sys_prompt = get_prompt("CONSISTENCY_PATCH_SYSTEM")
    user_prompt = build_repair_user_prompt(
        sections=sections, conflicts_text=conflicts_text, facts_text=facts_text
    )

    try:
        result = await collect_json_response(
            provider,
            [
                AIMessage(role="system", content=sys_prompt),
                AIMessage(role="user", content=user_prompt),
            ],
            temperature=0.2,
            max_retries=1,
        )
    except Exception as exc:  # AI 不可用不能中断整轮修复，交由调用方降级
        logger.warning("一致性补丁生成失败: %s", exc)
        return [], [], f"AI 调用失败：{exc}"

    patches = text_patch.normalize_patches(result)

    # 只保留编号在本次送审范围内的补丁（AI 编造编号会让宿主改错小节）
    kept = []
    for patch in patches:
        if patch.get("section_id") in allowed_ids:
            kept.append(patch)
        else:
            logger.warning(
                "丢弃编号越界的一致性补丁: section_id=%s（允许：%s）",
                patch.get("section_id") or "空",
                sorted(allowed_ids),
            )

    unresolved = []
    if isinstance(result, dict) and isinstance(result.get("unresolved"), list):
        for item in result["unresolved"]:
            if isinstance(item, dict):
                unresolved.append(
                    {
                        "section_id": str(item.get("section_id") or "").strip(),
                        "reason": str(item.get("reason") or "").strip(),
                    }
                )
    return kept, unresolved, None


# ========== 编排 ==========


def _load_context(project_id: int, section_ids: list[int] | None) -> dict:
    """读取事实变量、未解决冲突、待修小节。"""
    with get_db() as conn:
        facts_row = conn.execute(
            "SELECT global_facts_json FROM analysis WHERE project_id = ?", (project_id,)
        ).fetchone()
        conflict_rows = conn.execute(
            "SELECT conflict_id, fact_name, values_json, description FROM consistency_conflicts "
            "WHERE project_id = ? AND resolved = 0 ORDER BY id",
            (project_id,),
        ).fetchall()
        params: list[Any] = [project_id]
        sql = (
            "SELECT id, title, content FROM sections "
            "WHERE project_id = ? AND content IS NOT NULL AND content != ''"
        )
        if section_ids:
            placeholders = ",".join("?" for _ in section_ids)
            sql += f" AND id IN ({placeholders})"
            params.extend(section_ids)
        sql += " ORDER BY sort_order, id"
        section_rows = conn.execute(sql, params).fetchall()

    # ENHANCE 2026-09-29（facts-ns + 占位泄漏）：原先自己 `json.loads` 且只认顶层
    # list，与正文生成链路不同口径：{"items": [...]} 包装形态在这里会被静默
    # 当作“无事实”→ AI 修复拿不到事实基准，只能凭语义猜目标值；而 placeholder
    # 档的【待填写】占位项又会被当成真实事实写回正文。现统一走服务层正本。
    facts: list[dict] = []
    if facts_row and facts_row["global_facts_json"]:
        facts = facts_for_generation(facts_row["global_facts_json"])

    conflicts = []
    for row in conflict_rows:
        try:
            values = json.loads(row["values_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            values = []
        conflicts.append(
            {
                "conflict_id": row["conflict_id"],
                "fact_name": row["fact_name"],
                "values": values if isinstance(values, list) else [],
                "description": row["description"] or "",
            }
        )

    sections = [
        {"node_id": row["id"], "id": row["id"], "title": row["title"], "content": row["content"] or ""}
        for row in section_rows
    ]
    return {"facts": facts, "conflicts": conflicts, "sections": sections}


def refresh_conflict_states(conn, project_id: int, sections: dict[int, str]) -> dict:
    """按可验证判据收敛冲突状态：正文里还能找到 ≥2 个取值 → 保持未解决。

    不做「补丁跑过就标已解决」这种谎报。返回 `{resolved, still_open}`。
    """
    rows = conn.execute(
        "SELECT conflict_id, values_json FROM consistency_conflicts "
        "WHERE project_id = ? AND resolved = 0",
        (project_id,),
    ).fetchall()
    resolved = 0
    still_open = 0
    for row in rows:
        try:
            values = json.loads(row["values_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            values = []
        if not isinstance(values, list):
            values = []
        present = [
            str(value)
            for value in values
            if str(value)
            and any(
                # BUG-FIX 2026-10-08（判定口径不一致 → 谎报「仍待处理」）：
                # 原实现用裸子串 `str(value) in content` 找取值，而确定性修复路径
                # （`consistency_auditor.apply_autofix` → `text_patch.replace_values`）
                # 走的是带**数字边界保护**的 `find_value_occurrences`。两边口径不一致
                # 导致：冲突值「30」在正文只写成「工期300天」时，裸子串判定
                # 「30 仍在正文里」→ 冲突被永久判为 still_open，尽管 AI 补丁
                # 其实已经把该值统一掉了。反向亦然（子串漏判可致误标已解决）。
                # 现统一到 `text_patch` 的同一套定位口径。
                text_patch.find_value_occurrences(content, str(value))
                for content in sections.values()
            )
        ]
        unique_present = list(dict.fromkeys(present))
        if len(unique_present) <= 1:
            conn.execute(
                "UPDATE consistency_conflicts SET resolved = 1, resolved_value = ?, resolved_at = ? "
                "WHERE conflict_id = ? AND project_id = ?",
                (
                    unique_present[0] if unique_present else "",
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    row["conflict_id"],
                    project_id,
                ),
            )
            resolved += 1
        else:
            still_open += 1
    return {"resolved": resolved, "still_open": still_open}


async def repair_with_ai(
    project_id: int,
    *,
    dry_run: bool = False,
    provider: Any = None,
    section_ids: list[int] | None = None,
) -> dict:
    """AI 补丁修复主流程。

    Args:
        project_id: 项目ID
        dry_run: 只算补丁与预览结果，不写库、不改冲突状态
        provider: 复用调用方已建的 provider；为空则自行创建
        section_ids: 只修指定小节（为空则全部有正文的小节）

    Returns:
        修复报告：`patches` / `applied` / `rejected` / `sections` / `unresolved` /
        `conflict_states` / `message`。
    """
    report: dict = {
        "success": False,
        "dry_run": dry_run,
        "project_id": project_id,
        "facts_used": 0,
        "conflicts_used": 0,
        "batches": 0,
        "patches": [],
        "applied": [],
        "rejected": [],
        "sections": [],
        "unresolved": [],
        "conflict_states": None,
        "errors": [],
        "message": "",
    }

    context = _load_context(project_id, section_ids)
    sections = context["sections"]
    report["facts_used"] = len(context["facts"])
    report["conflicts_used"] = len(context["conflicts"])

    if not sections:
        report["message"] = "没有可供修复的正文内容"
        return report
    if not context["conflicts"]:
        report["message"] = "没有未解决的一致性冲突，无需修复"
        return report

    if provider is None:
        provider = create_provider(get_ai_config())

    facts_text = build_facts_text(context["facts"])
    conflicts_text = build_conflicts_text(context["conflicts"])

    batches = plan_batches(sections)
    report["batches"] = len(batches)

    # 1) 逐批生成补丁，按小节归并
    patches_by_section: dict[str, list[dict]] = {}
    for batch in batches:
        patches, unresolved, error = await generate_patches(
            provider, sections=batch, conflicts_text=conflicts_text, facts_text=facts_text
        )
        if error:
            report["errors"].append(error)
        report["unresolved"].extend(unresolved)
        for patch in patches:
            patches_by_section.setdefault(patch["section_id"], []).append(patch)

    report["patches"] = [patch for group in patches_by_section.values() for patch in group]
    if not report["patches"]:
        report["message"] = (
            "AI 未产出可用补丁"
            + (f"（{report['errors'][0]}）" if report["errors"] else "")
        )
        return report

    # 2) 逐小节应用补丁（引擎内部逐条错误隔离）
    content_by_id = {int(section["id"]): section["content"] for section in sections}
    planned: list[tuple[int, str, str]] = []
    for section in sections:
        section_key = str(section["node_id"])
        group = patches_by_section.get(section_key)
        if not group:
            continue
        original = content_by_id[int(section["id"])]
        result = text_patch.apply_patches(original, group)
        for item in result["results"]:
            index = item.get("index")
            reason = ""
            if isinstance(index, int) and 0 <= index < len(group):
                reason = str(group[index].get("reason") or "")
            entry = {
                "section_id": section_key,
                "title": section["title"],
                "index": index,
                "reason": reason,
                "old_text": item.get("old_text"),
                "new_text": item.get("new_text"),
                "applied": item.get("applied"),
                "error": item.get("error"),
            }
            (report["applied"] if item.get("applied") else report["rejected"]).append(entry)
        report["errors"].extend(result["errors"])
        if result["changed"]:
            report["sections"].append(
                {
                    "section_id": section_key,
                    "title": section["title"],
                    "applied": result["applied_count"],
                    "rejected": result["rejected_count"],
                }
            )
            planned.append((int(section["id"]), section_key, result["content"]))

    if not planned:
        report["message"] = (
            f"AI 产出 {len(report['patches'])} 条补丁，但全部未能安全定位到原文，未做任何修改"
        )
        return report

    if dry_run:
        report["success"] = True
        report["message"] = (
            f"预览：{len(report['applied'])} 条补丁可应用，将修改 {len(planned)} 个小节；"
            f"{len(report['rejected'])} 条被拒绝"
        )
        return report

    # 3) 落文 + 收敛冲突状态
    try:
        with get_db() as conn:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for section_id, _key, new_content in planned:
                conn.execute(
                    "UPDATE sections SET content = ?, updated_at = ? WHERE id = ? AND project_id = ?",
                    (new_content, now, section_id, project_id),
                )
            content_by_id.update({section_id: content for section_id, _k, content in planned})
            report["conflict_states"] = refresh_conflict_states(
                conn, project_id, dict(content_by_id)
            )
    except Exception as exc:
        logger.error("一致性 AI 修复落文失败: %s", exc, exc_info=True)
        report["message"] = f"落文失败：{exc}"
        return report

    # BUG-FIX 2026-09-18（P1）：旁路直写正文后失效三层缓存（内存/Redis/build_tree），
    # 否则旧正文会在 TTL 内被重新生成回灌，覆盖本次修复。
    from .sections import invalidate_content_caches
    invalidate_content_caches(project_id, [sid for sid, _k, _c in planned])

    report["success"] = True
    states = report["conflict_states"] or {}
    report["message"] = (
        f"已应用 {len(report['applied'])} 条补丁，修改 {len(planned)} 个小节"
        f"（拒绝 {len(report['rejected'])} 条）；"
        f"冲突已解决 {states.get('resolved', 0)} 条，仍待处理 {states.get('still_open', 0)} 条"
    )
    logger.info(
        "一致性 AI 修复完成: project=%s, 补丁=%d, 小节=%d, 冲突解决=%d",
        project_id,
        len(report["applied"]),
        len(planned),
        states.get("resolved", 0),
    )
    return report
