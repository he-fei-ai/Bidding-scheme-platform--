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
from .consistency_auditor import ConsistencyAuditor
from .facts_service import (
    content_repair_max_attempts,
    facts_for_generation,
    is_placeholder_value,
    parse_stored_items,
)


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


def build_repair_user_prompt(
    *, sections: list[dict], conflicts_text: str, facts_text: str, failure_note: str = ""
) -> str:
    """拼装送审 user prompt：事实变量 + 冲突清单 + 带行号的正文。

    带行号是为了让 AI 能填 `start_line/end_line` 作为加速线索；
    `text_patch.normalize_patch_text` 会自动剥掉 AI 抄回来的行号前缀。

    P1-4（facts-ns 2026-10-14，对齐易标失败回喂重试）：`failure_note` 非空时
    追加「上一轮补丁应用失败」区块，告知失败原因并要求 old_text 从下方
    最新正文原样摘抄；为空时提示词与历史完全一致。
    """
    allowed_ids = [str(section.get("node_id") or section.get("id") or "") for section in sections]
    blocks = []
    for section in sections:
        section_id = str(section.get("node_id") or section.get("id") or "")
        blocks.append(
            f"<section>\n编号：{section_id}\n标题：{section.get('title') or '未命名章节'}\n"
            f"正文：\n{text_patch.format_content_with_line_numbers(section.get('content') or '')}\n</section>"
        )
    prompt = (
        f"事实变量（正文必须与之一致）：\n{facts_text}\n\n"
        f"审计发现的冲突：\n{conflicts_text}\n\n"
        f"待修复小节正文（带行号，patch 的 old_text/new_text 不要包含行号）：\n\n"
        + "\n\n".join(blocks)
        + f"\n\npatches[*].section_id 必须是以下编号之一：{'、'.join(allowed_ids) or '无'}\n请只返回 JSON。"
    )
    if failure_note:
        prompt += (
            "\n\n上一轮修复补丁未能应用，原因清单：\n" + failure_note
            + "\n请基于上方最新正文重新生成补丁：old_text 必须从正文中原样摘抄"
            "（逐字一致，不要凭记忆改写），且在正文中能唯一定位；"
            "若确实无法定位可安全替换的片段，请放进 unresolved 说明，不要硬猜。"
        )
    return prompt


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
    failure_note: str = "",
) -> tuple[list[dict], list[dict], str | None]:
    """调用 AI 生成补丁。

    Args:
        failure_note: P1-4 失败回喂信息（上一轮 rejected 补丁的失败原因），
            为空时行为与历史完全一致。

    Returns:
        `(patches, unresolved, error)`；出错时 `patches=[]`、`error` 为原因。
    """
    allowed_ids = {str(s.get("node_id") or s.get("id") or "") for s in sections}
    sys_prompt = get_prompt("CONSISTENCY_PATCH_SYSTEM")
    user_prompt = build_repair_user_prompt(
        sections=sections,
        conflicts_text=conflicts_text,
        facts_text=facts_text,
        failure_note=failure_note,
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

        # BUG-FIX 2026-10-09：sqlite3.Row 不是快照，而是**惰性解引用**——它读的是连接上
        # 那个共享、会被下一次 execute 重新 prepare、并在 with 块退出时 commit() 被
        # finalize 掉的预处理语句的当前行/列。上面三次 conn.execute 依次覆盖了同一语句
        # 的列布局，所以一旦把 Row 带到块外再取值：facts_row["global_facts_json"] 可能
        # 读到 sections 语句那一列（AI 修复因此拿不到事实基准、只能凭语义猜目标值），
        # conflict_rows 各项也会串列。这里立即物化成 dict。
        # 同类修复：consistency_auditor._collect_fact_instances。
        facts_json: str | None = facts_row["global_facts_json"] if facts_row else None
        conflicts_raw: list[dict] = [
            {
                "conflict_id": r["conflict_id"],
                "fact_name": r["fact_name"],
                "values_json": r["values_json"],
                "description": r["description"],
            }
            for r in conflict_rows
        ]
        sections_raw: list[dict] = [
            {"id": r["id"], "title": r["title"], "content": r["content"]}
            for r in section_rows
        ]

    # ENHANCE 2026-09-29（facts-ns + 占位泄漏）：原先自己 `json.loads` 且只认顶层
    # list，与正文生成链路不同口径：{"items": [...]} 包装形态在这里会被静默
    # 当作“无事实”→ AI 修复拿不到事实基准，只能凭语义猜目标值；而 placeholder
    # 档的【待填写】占位项又会被当成真实事实写回正文。现统一走服务层正本。
    facts: list[dict] = []
    if facts_json:
        facts = facts_for_generation(facts_json)

    conflicts = []
    for row in conflicts_raw:
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
        for row in sections_raw
    ]
    return {"facts": facts, "conflicts": conflicts, "sections": sections}


def _load_all_project_content(conn, project_id: int) -> dict[int, str]:
    """载入项目**全量**小节正文，供 `refresh_conflict_states` 做收敛判定。

    BUG-FIX 2026-10-09：见 `repair_with_ai` 落文处的说明 —— 冲突状态判定
    必须基于全项目正文，否则带 `section_ids` 的局部修复会把「旧取值仍在
    其它小节」的冲突误标为已解决。

    复用调用方已持有的 conn（同一事务内可读到刚落库的补丁结果），
    不额外开连接、不占用全局写锁。读取失败时返回空 dict：
    此时 `refresh_conflict_states` 的判据会退化为「全都不存在 → 全标已解决」，
    因此调用方必须在 update 之后、读失败时可感知 —— 故此处抛出而非静默吞掉，
    由外层 except 统一转成 report['message']。
    """
    rows = conn.execute(
        "SELECT id, content FROM sections WHERE project_id = ? ORDER BY sort_order, id",
        (project_id,),
    ).fetchall()
    return {int(r["id"]): (r["content"] or "") for r in rows}


def _load_project_summary_and_baseline(conn, project_id: int) -> dict[str, str]:
    """载入项目**摘要文本**与**已确认事实基准**，供 `refresh_conflict_states` 做收敛判定。

    BUG-FIX 2026-10-11（正确性 / 误标已解决）：`refresh_conflict_states` 的判据是
    「全文还能找到 ≥2 个冲突取值 → 仍算冲突」，因此**扫描宇宙必须与审计器的事实
    来源宇宙一致**。审计器（`ConsistencyAuditor._collect_fact_instances`）除章节
    正文外还抽 `projects.notes/bidder_name/industry`（摘要）与
    `analysis.global_facts_json`（已确认事实基准），所以冲突的两个取值可能
    **都落在正文之外**：例如摘要写「工期 45 天」而基准写「450 日历天」，正文只写
    其中一个；或 AI 补丁把正文里那个值改掉后，正文只剩一个取值。此时只扫
    `sections` 会把「正文里只剩 1 个取值」误判成「全文统一」→ 冲突被**错误标记为
    已解决**，而正文与摘要/基准之间的不一致仍真实存在。这与 2026-10-09 修的
    「局部修复洗白其它小节」同源，只是发生在摘要/基准维度。

    基准只纳入 `FACT_BASELINE_CATEGORIES` 声明的三类：那是审计器**真正会拿去比**的
    基准范围，照抄它才能与审计口径对齐 —— 纳入未映射的类别会让无关基准值
    （如付款金额）误压住工期冲突、使其永久判为未解决。

    基准值按**原始 value 文本**整条给出，而不是先做正则抽取：
    `refresh_conflict_states` 的查找走 `text_patch.find_value_occurrences`
    （带数字边界保护），把「建设工期450天」整句交给它查「450」与抽出的「450」
    结果一致；查「45」时因数字边界保护不会误命中「450」里的「45」。

    复用调用方已持有的 conn：与同一事务内的章节读取保持一致视图，
    不额外开连接、不占用全局写锁。失败向上抛，由外层 except 统一转成
    report['message']（与 `_load_all_project_content` 同口径）。
    """
    texts: dict[str, str] = {}

    summary_row = conn.execute(
        "SELECT notes, bidder_name, industry FROM projects WHERE id = ?",
        (project_id,),
    ).fetchone()
    summary = dict(summary_row) if summary_row is not None else None
    if summary:
        summary_text = ""
        if summary.get("notes"):
            summary_text += f"项目备注: {summary['notes']}\n"
        if summary.get("bidder_name"):
            summary_text += f"投标人: {summary['bidder_name']}\n"
        if summary.get("industry"):
            summary_text += f"行业: {summary['industry']}\n"
        if summary_text.strip():
            texts["summary"] = summary_text

    facts_row = conn.execute(
        "SELECT global_facts_json FROM analysis WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    raw_json = facts_row["global_facts_json"] if facts_row is not None else ""
    mapped_categories = set(ConsistencyAuditor.FACT_BASELINE_CATEGORIES.keys())
    baseline_values = [
        str(item.get("value") or "").strip()
        for item in parse_stored_items(raw_json)
        if str(item.get("value") or "").strip()
        and not is_placeholder_value(str(item.get("value") or ""))
        and str(item.get("category") or "") in mapped_categories
    ]
    if baseline_values:
        texts["global_facts"] = "\n".join(baseline_values)

    return texts


def refresh_conflict_states(
    conn,
    project_id: int,
    sections: dict[int, str],
    *,
    extra_texts: dict[str, str] | None = None,
) -> dict:
    """按可验证判据收敛冲突状态：全文里还能找到 ≥2 个取值 → 保持未解决。

    不做「补丁跑过就标已解决」这种谎报。返回 `{resolved, still_open}`。

    `sections` 是章节正文（`{section_id: content}`）；`extra_texts` 是章节正文
    **之外**的事实来源文本（`{来源标签: 文本}`，如摘要与已确认事实基准，
    见 `_load_project_summary_and_baseline`）。判据是「全文还能找到 ≥2 个取值」，
    故两处都必须纳入扫描 —— 否则只在摘要/基准里存在旧取值的冲突会被误标已解决。
    向后兼容：`extra_texts` 缺省为 None 时，行为与「只扫章节正文」完全一致。
    """
    rows = conn.execute(
        "SELECT conflict_id, values_json FROM consistency_conflicts "
        "WHERE project_id = ? AND resolved = 0",
        (project_id,),
    ).fetchall()
    # 扫描宇宙 = 全量章节正文 + 章节之外的来源文本（摘要 / 已确认事实基准）。
    scan_texts = list(sections.values())
    if extra_texts:
        scan_texts.extend(extra_texts.values())

    resolved = 0
    still_open = 0
    for row in rows:
        try:
            values = json.loads(row["values_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            values = []
        if not isinstance(values, list):
            values = []
        # BUG-FIX 2026-10-13（第六轮·P1·AI 语义发现被误标已解决）：
        # AI 审计路径（`ai/workflows_expand._persist_ai_audit_findings`）写入的
        # 冲突 `values_json` 恒为 `'[]'`——自由文本发现（LLM 只给
        # section_id/issue/suggestion），没有结构化取值。对这类冲突，
        # 「全文还能找到几个取值」的判据**不可计算**：`present` 恒为空 →
        # `len(unique_present) <= 1` 恒成立 → 每跑一次 AI 补丁修复 / Agent
        # 修复，全部未解决的 AI 发现都被静默标成 `resolved=1`：用户看到冲突
        # 凭空消失，而正文里的问题可能原样存在（实测见
        # `tests/test_consistency_round6_2026_10_13.py::TestEmptyValueConflictsNotFalselyResolved`）。
        # 口径：**空取值 = 无法验证 = 保持未解决**（与「宁多报也不漏报」一致）；
        # AI 发现的收敛由下一轮生成时审计负责——它先删 `AI-%` 旧行再按最新
        # 正文重写（`_persist_ai_audit_findings` 的既定设计）。
        verifiable = [str(v) for v in values if str(v).strip()]
        if not verifiable:
            still_open += 1
            continue
        present = [
            str(value)
            for value in verifiable
            if any(
                # BUG-FIX 2026-10-08（判定口径不一致 → 谎报「仍待处理」）：
                # 原实现用裸子串 `str(value) in content` 找取值，而确定性修复路径
                # （`consistency_auditor.apply_autofix` → `text_patch.replace_values`）
                # 走的是带**数字边界保护**的 `find_value_occurrences`。两边口径不一致
                # 导致：冲突值「30」在正文只写成「工期300天」时，裸子串判定
                # 「30 仍在正文里」→ 冲突被永久判为 still_open，尽管 AI 补丁
                # 其实已经把该值统一掉了。反向亦然（子串漏判可致误标已解决）。
                # 现统一到 `text_patch` 的同一套定位口径。
                text_patch.find_value_occurrences(content, str(value))
                for content in scan_texts
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
            content_by_id[int(section["id"])] = result["content"]
            report["sections"].append(
                {
                    "section_id": section_key,
                    "title": section["title"],
                    "applied": result["applied_count"],
                    "rejected": result["rejected_count"],
                }
            )
            planned.append((int(section["id"]), section_key, result["content"]))

    # P1-4（facts-ns 2026-10-14，对齐易标失败回喂重试循环）：被拒绝的补丁
    # 此前只进报告，AI 无从得知「old_text 定位不到」，用户只能再点一次修复
    # 从头再生成。现在按小节把「最新正文 + 失败原因」回喂给 AI 重新产出补丁，
    # 至多 CONSISTENCY_REPAIR_MAX_ATTEMPTS 轮（默认 1 = 不重试，
    # 与历史行为完全一致）。重试轮只处理本轮含 rejected 的小节，失败仍回写报告。
    _max_attempts = content_repair_max_attempts()
    section_by_key = {str(s["node_id"]): s for s in sections}
    _attempt = 1
    _round_rejected = list(report["rejected"])
    while _attempt < _max_attempts and _round_rejected:
        _rejected_by_section: dict[str, list[dict]] = {}
        for _e in _round_rejected:
            _rejected_by_section.setdefault(_e["section_id"], []).append(_e)
        _next_rejected: list[dict] = []
        for _key, _rejects in _rejected_by_section.items():
            _section = section_by_key.get(_key)
            if _section is None:
                _next_rejected.extend(_rejects)
                continue
            _sid = int(_section["id"])
            _latest = content_by_id[_sid]
            _failure_note = "\n".join(
                f"- old_text「{str(_r.get('old_text') or '')[:60]}」：{_r.get('error') or '未能唯一定位到原文'}"
                for _r in _rejects
            )
            # 先把该小节上一轮的 rejected 移出报告，本轮仍失败的会重新回写
            report["rejected"] = [e for e in report["rejected"] if e["section_id"] != _key]
            _retry_patches, _retry_unresolved, _retry_error = await generate_patches(
                provider,
                sections=[{**_section, "content": _latest}],
                conflicts_text=conflicts_text,
                facts_text=facts_text,
                failure_note=_failure_note,
            )
            if _retry_error:
                report["errors"].append(_retry_error)
                report["rejected"].extend(_rejects)
                continue
            if not _retry_patches:
                # AI 认定无法产出可安全定位的补丁（通常随附 unresolved 说明）：
                # 保留上一轮失败记录，不硬猜。
                report["unresolved"].extend(_retry_unresolved)
                report["rejected"].extend(_rejects)
                continue
            report["unresolved"].extend(_retry_unresolved)
            report["patches"].extend(_retry_patches)
            _result = text_patch.apply_patches(_latest, _retry_patches)
            for _item in _result["results"]:
                _index = _item.get("index")
                _reason = ""
                if isinstance(_index, int) and 0 <= _index < len(_retry_patches):
                    _reason = str(_retry_patches[_index].get("reason") or "")
                _entry = {
                    "section_id": _key,
                    "title": _section["title"],
                    "index": _index,
                    "reason": _reason,
                    "old_text": _item.get("old_text"),
                    "new_text": _item.get("new_text"),
                    "applied": _item.get("applied"),
                    "error": _item.get("error"),
                }
                if _item.get("applied"):
                    report["applied"].append(_entry)
                else:
                    report["rejected"].append(_entry)
                    _next_rejected.append(_entry)
            report["errors"].extend(_result["errors"])
            if _result["changed"]:
                content_by_id[_sid] = _result["content"]
                # planned 后写覆盖前写：落文循环按序 UPDATE，重试轮内容在最后
                planned.append((_sid, _key, _result["content"]))
                if all(s["section_id"] != _key for s in report["sections"]):
                    report["sections"].append(
                        {
                            "section_id": _key,
                            "title": _section["title"],
                            "applied": _result["applied_count"],
                            "rejected": _result["rejected_count"],
                        }
                    )
        _round_rejected = _next_rejected
        _attempt += 1

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
            # BUG-FIX 2026-10-09（正确性 / 误标已解决）：此前把「本次载入的
            # 小节」content_by_id 直接交给 refresh_conflict_states。该函数以
            # 「全项目正文里还能找到 ≥2 个冲突取值 → 仍算冲突」为判据，因此
            # **扫描范围必须覆盖整个项目**。而 `sections` 来自 `_load_context`，
            # 当调用方带 `section_ids` 参数（POST /consistency/repair/{id}?section_ids=…）
            # 时它只包含被点名的那些小节 —— 于是仍残留在「未参与修复的小节」
            # 里的旧取值根本扫不到，`len(unique_present) <= 1` 成立，
            # 冲突被**错误标记为已解决**。人工点几个小节修复，反而会把
            # 其它小节里真实存在的不一致「洗白」，且没有任何提示。
            # 现改为：落文后按 project_id 重新加载**全量**正文，
            # 再用本次已写入的 planned 内容覆盖对应条目后做收敛判定。
            verify_scope = _load_all_project_content(conn, project_id)
            verify_scope.update(content_by_id)
            # BUG-FIX 2026-10-11（正确性 / 误标已解决）：收敛判据的扫描宇宙必须
            # 与审计器一致 —— 除全量章节正文外还要纳入摘要与已确认事实基准。
            # 否则「旧取值只存在于摘要/基准」的冲突会被「正文里只剩 1 个取值」
            # 误判为全文统一。见 `_load_project_summary_and_baseline` 的说明。
            report["conflict_states"] = refresh_conflict_states(
                conn,
                project_id,
                verify_scope,
                extra_texts=_load_project_summary_and_baseline(conn, project_id),
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
