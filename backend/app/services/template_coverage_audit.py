# -*- coding: utf-8 -*-
"""template_coverage_audit.py — 已有方案扩写 · 原方案内容覆盖率审计

ENHANCE 2026-10-20（易标机制引入·遗留项 4 落地）：移植自 OpenBidKit 易标
``contentGenerationTask.cjs`` 的 original-plan coverage audit：

  - ``buildOriginalCoverageAuditMessages``（L1897）→ 本模块 ``build_coverage_audit_messages``
  - ``runOriginalPlanCoverageAuditIfEnabled``（L5145）→ ``run_template_coverage_audit_if_enabled``
  - ``buildOriginalPlanCoveragePatchMessages``（L5292）→ ``build_coverage_patch_messages``

为什么本平台需要它（修正 2026-10-20 第一轮报告的误判）：
    第一轮引入报告称"本平台无原方案扩写工作流，覆盖率审计不适用"——该结论
    **有误**。本平台实际存在完整的「已有方案扩写」工作流：
    ``services/template_expansion.py``（上传历史方案 → AI 提取目录 → 拆分
    原方案段落 → 映射到当前目录节点）+ ``routers/templates.py``
    （``POST /{template_id}/audit`` 目录级覆盖审计）。缺的恰恰是易标有的
    **内容级**覆盖率审计：扩写生成只做"目录标题覆盖"检查，原方案段落里的
    实质要点（参数、做法、结论）是否被新正文保留，没有任何机制保障——
    用户规格五「扩写模式下原方案要点丢失」即此缺口。

机制（对齐易标，按本平台数据结构适配）：
  1. 段落映射：原方案段落 → 当前目录叶子节点（确定性匹配，见
     ``map_paragraphs_to_nodes``；不修改 template_expansion 里 TODO 的
     朴素标题包含匹配，避免影响既有调用方与测试）；
  2. 覆盖审计：逐节点把「原方案段落 + 扩写后新正文」交给 AI，按四态判定
     （covered / partial / missing / conflict），**只判定不改写**；
  3. 缺口修复：对 partial/missing/conflict 的节点生成补写提示词，把原方案
     要点补回新正文（保留新正文结构与风格，只做补写/校正），最多 2 轮，
     每轮后重审被修节点；仍不达标即如实报告，不无限循环；
  4. 落库：审计报告写入 ``project_templates.content_coverage_json``
     （与目录级 ``coverage_audit_json`` 分列，互不覆盖）。

兼容性：
  - ``TEMPLATE_COVERAGE_AUDIT_ENABLED`` 默认 **false**：不改变任何现有行为；
  - 项目无模板（未走扩写流程）时自动跳过，零开销；
  - 旧数据库无 ``content_coverage_json`` 列时由迁移 #39 补齐；读取侧对缺列
    容错（按空报告处理）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from .ai.providers.base import AIMessage

logger = logging.getLogger(__name__)


# ========== 判定口径（与易标 originalCoverageAudit 四态同口径） ==========

#: 要点完整保留（表述可不同，实质信息不丢失）
VERDICT_COVERED = "covered"
#: 部分保留（有关键信息损耗）
VERDICT_PARTIAL = "partial"
#: 要点丢失（新正文未涉及该段核心内容）
VERDICT_MISSING = "missing"
#: 与原方案矛盾（数据/做法/结论被改写失真）
VERDICT_CONFLICT = "conflict"

#: 合法判定值集合（解析时白名单过滤，AI 臆造值一律丢弃）
VALID_VERDICTS = frozenset({VERDICT_COVERED, VERDICT_PARTIAL, VERDICT_MISSING, VERDICT_CONFLICT})

#: 需要修复的判定值（covered 不需要动作）
GAP_VERDICTS = frozenset({VERDICT_PARTIAL, VERDICT_MISSING, VERDICT_CONFLICT})

#: 单节点审计的最大重试修复轮数（易标同口径：最多 2 次）
MAX_REPAIR_ROUNDS = 2

#: 原方案段落注入提示词的单段最大字符数（防上下文爆炸；超出截断留痕）
_PARAGRAPH_MAX_CHARS = 1200

#: 新正文注入提示词的最大字符数（与段落同口径）
_CONTENT_MAX_CHARS = 6000

#: 映射相似度阈值（bigram Jaccard；中文标题 2 字以上即有区分度）
_MAP_SIMILARITY_THRESHOLD = 0.34


# ========== 提示词 ==========

_AUDIT_SYSTEM = """你是专项施工方案的内容覆盖审计员。任务：判断「扩写后的新正文」是否完整保留了「原方案段落」的实质要点。

判定口径（四态，逐段判定）：
- covered：要点完整保留（表述可以不同，实质信息不丢失）
- partial：部分保留（有关键信息损耗）
- missing：要点丢失（新正文未涉及该段落的核心内容）
- conflict：与原方案矛盾（数据/做法/结论被改写失真）

硬性规则：
1. 只判定、不改写——你输出的只有 JSON，禁止输出修改建议、禁止输出正文
2. 按段落逐一判定，一个段落一条判定，不得合并、不得遗漏
3. 新正文为空的段落一律判 missing
4. 「编排结构调整」「语言专业化改写」不算丢失；只有实质信息（参数、做法、结论、标准依据）损耗才判 partial/missing
5. 只输出严格 JSON：{"verdicts": [{"id": "P001", "status": "covered|partial|missing|conflict", "reason": "一句话依据"}]}"""


_PATCH_SYSTEM = """你是专项施工方案的内容修复编辑。任务：把原方案里丢失/受损的要点补回新正文。

硬性规则：
1. 保留新正文的全部已有内容与结构风格，只做「补写」与「校正」，不得整篇重写
2. 补写内容必须来自给定的原方案要点，不得新增原方案没有的事实
3. 与原方案矛盾的表述，按原方案校正
4. 直接输出修复后的完整正文，不要任何解释、不要 JSON、不要 Markdown 围栏"""


# ========== 段落 → 节点映射（确定性，替代朴素标题包含匹配） ==========

def _normalize_title(title: str) -> str:
    """标题归一化：去空白/常见标点/大小写，用于确定性比对。"""
    t = str(title or "").strip().lower()
    return re.sub(r"[\s，。、；：（）()\[\]【】<>《》\-—_/\\|#*]+", "", t)


def _title_tokens(title: str) -> set:
    """标题二元组集合（中文无空格分词，用字符 bigram 近似词集合）。"""
    t = _normalize_title(title)
    if len(t) < 2:
        return {t} if t else set()
    return {t[i: i + 2] for i in range(len(t) - 1)}


def _similarity(a: str, b: str) -> float:
    """标题相似度：bigram Jaccard（0~1）。"""
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta) + len(tb) - inter
    return inter / union if union else 0.0


def _sec_id(sec: Any) -> str:
    if isinstance(sec, dict):
        return str(sec.get("id") or "")
    return str(getattr(sec, "id", "") or "")


def _sec_title(sec: Any) -> str:
    if isinstance(sec, dict):
        tp = sec.get("title_path") or []
    else:
        tp = getattr(sec, "title_path", None) or []
    return " > ".join(str(t) for t in tp) if tp else ""


def _sec_content(sec: Any) -> str:
    if isinstance(sec, dict):
        return str(sec.get("content") or "")
    return str(getattr(sec, "content", "") or "")


def map_paragraphs_to_nodes(template_sections: list, leaf_nodes: list) -> tuple:
    """把原方案段落确定性映射到当前目录叶子节点。

    与 ``template_expansion._simple_title_match`` 的关系：后者是标题包含
    匹配（TODO 待升级为 AI 语义匹配），且被既有调用方/测试消费。本函数是
    **审计专用**的升级版，独立实现以避免影响既有链路：
      1. 归一化标题完全相等 → 直接命中；
      2. 一方包含另一方 → 命中；
      3. bigram 相似度 ≥ ``_MAP_SIMILARITY_THRESHOLD`` → 命中（取最相似节点）；
      4. 都不满足 → 进 ``unmapped``（**不静默丢弃**——未映射段落在审计报告
         里单独计数，提示人工确认，而不是假装覆盖）。

    Args:
        template_sections: 原方案段落（对象或 dict，需有 id/title_path/content）
        leaf_nodes: 当前目录叶子节点（dict，需有 id/title）

    Returns:
        (items, unmapped)：
        items = [{"node_id", "node_title", "paragraphs": [{"id", "title", "content"}]}]
        unmapped = [{"id", "title", "chars"}]
    """
    items: list = []
    unmapped: list = []
    node_norm = [
        (n.get("id"), str(n.get("title") or ""), _normalize_title(n.get("title")))
        for n in leaf_nodes or []
        if isinstance(n, dict) and n.get("id") is not None
    ]

    for sec in template_sections or []:
        sec_id = _sec_id(sec)
        sec_title = _sec_title(sec)
        content = _sec_content(sec) or ""
        if not content.strip():
            continue  # 空段落无可审计内容
        norm = _normalize_title(sec_title)
        best_idx, best_score = None, 0.0
        if node_norm:
            for idx, (_nid, _ntitle, nnorm) in enumerate(node_norm):
                if norm and nnorm and (norm == nnorm or norm in nnorm or nnorm in norm):
                    best_idx, best_score = idx, 1.0
                    break
                sim = _similarity(sec_title, _ntitle)
                if sim > best_score:
                    best_idx, best_score = idx, sim
        if best_idx is not None and best_score >= _MAP_SIMILARITY_THRESHOLD:
            node_id, node_title, _ = node_norm[best_idx]
            existing = next((it for it in items if it["node_id"] == node_id), None)
            if existing is None:
                existing = {"node_id": node_id, "node_title": node_title, "paragraphs": []}
                items.append(existing)
            existing["paragraphs"].append({
                "id": sec_id, "title": sec_title, "content": content,
            })
        else:
            unmapped.append({"id": sec_id, "title": sec_title, "chars": len(content)})
    return items, unmapped


# ========== 提示词构建 ==========

def build_coverage_audit_messages(item: dict) -> list:
    """构建单节点覆盖审计消息（对齐易标 buildOriginalCoverageAuditMessages）。

    Args:
        item: ``map_paragraphs_to_nodes`` 产出的单节点项，需含
            node_title / paragraphs / generated（扩写后新正文，由调用方注入）

    Returns:
        [system, user] 两条 AIMessage
    """
    node_title = str(item.get("node_title") or "")
    paragraphs = item.get("paragraphs") or []
    generated = str(item.get("generated") or "")

    para_lines = []
    for p in paragraphs:
        content = str(p.get("content") or "")[:_PARAGRAPH_MAX_CHARS]
        para_lines.append("[%s] %s\n%s" % (p.get("id"), p.get("title"), content))
    paras_block = "\n\n".join(para_lines) or "（无原方案段落）"
    gen_block = generated[:_CONTENT_MAX_CHARS] if generated else "（空）"

    user = (
        "### 章节\n%s\n\n"
        "### 原方案段落（被审计对象，共 %d 段）\n%s\n\n"
        "### 扩写后的新正文\n%s\n\n"
        "请按判定口径对每个原方案段落输出一条 JSON 判定。"
        % (node_title, len(paragraphs), paras_block, gen_block)
    )
    return [
        AIMessage(role="system", content=_AUDIT_SYSTEM),
        AIMessage(role="user", content=user),
    ]


def build_coverage_patch_messages(node_title: str, current_content: str, gap_paragraphs: list) -> list:
    """构建缺口修复消息（对齐易标 buildOriginalPlanCoveragePatchMessages）。

    Args:
        node_title: 章节标题
        current_content: 当前（扩写后）正文
        gap_paragraphs: 待补回的段落 [{id, title, content, status, reason}]
    """
    gap_lines = []
    for p in gap_paragraphs:
        content = str(p.get("content") or "")[:_PARAGRAPH_MAX_CHARS]
        gap_lines.append(
            "[%s] %s（判定：%s，依据：%s）\n%s"
            % (p.get("id"), p.get("title"), p.get("status"), p.get("reason"), content)
        )
    user = (
        "### 章节\n%s\n\n"
        "### 当前正文（保留其结构与风格，只做补写/校正）\n%s\n\n"
        "### 需要补回的原方案要点\n%s\n\n"
        "请输出补写后的完整章节正文。"
        % (node_title, str(current_content or "")[:_CONTENT_MAX_CHARS], "\n\n".join(gap_lines))
    )
    return [
        AIMessage(role="system", content=_PATCH_SYSTEM),
        AIMessage(role="user", content=user),
    ]


# ========== 响应解析 ==========

def parse_coverage_verdicts(text: str, allowed_ids: set) -> list:
    """解析覆盖审计响应为判定列表（白名单过滤 + 容错）。

    容错口径（与全仓 JSON 稳定性策略一致）：
      1. 优先 ``json.loads``；失败走 ``extract_json_from_text``（剥围栏/配平/修复）；
      2. 只接受 ``{"verdicts": [...]}`` 或裸数组两种形态；
      3. ``id`` 必须在 ``allowed_ids``（本轮审计的段落 id）内——AI 臆造 id 丢弃；
      4. ``status`` 必须属四态白名单，否则丢弃该条；
      5. 同一 id 多次判定：保留首条（稳定优先）。
    """
    from ..utils.json_parser import extract_json_from_text

    data = None
    raw = str(text or "").strip()
    if raw:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            data = extract_json_from_text(raw)
    if isinstance(data, dict):
        data = data.get("verdicts")
    if not isinstance(data, list):
        logger.warning("[覆盖审计] 响应解析失败（无 verdicts 数组），按无判定处理")
        return []

    out: list = []
    seen: set = set()
    for entry in data:
        if not isinstance(entry, dict):
            continue
        vid = str(entry.get("id") or "").strip()
        status = str(entry.get("status") or "").strip().lower()
        if vid not in allowed_ids or status not in VALID_VERDICTS:
            continue
        if vid in seen:
            continue
        seen.add(vid)
        out.append({
            "id": vid,
            "status": status,
            "reason": str(entry.get("reason") or "").strip()[:200],
        })
    return out


def summarize_verdicts(verdicts: list, total_paragraphs: int) -> dict:
    """汇总判定为覆盖报告（四态计数 + 加权覆盖率）。

    覆盖率口径：covered 计 1、partial 计 0.5、missing/conflict 计 0
    （与易标 originalCoverageAudit 的加权口径一致：部分保留折半）。
    """
    counts = {v: 0 for v in VALID_VERDICTS}
    for v in verdicts:
        counts[v["status"]] = counts.get(v["status"], 0) + 1
    total = total_paragraphs or len(verdicts)
    score = counts[VERDICT_COVERED] + 0.5 * counts[VERDICT_PARTIAL]
    return {
        "total": total,
        "covered": counts[VERDICT_COVERED],
        "partial": counts[VERDICT_PARTIAL],
        "missing": counts[VERDICT_MISSING],
        "conflict": counts[VERDICT_CONFLICT],
        "coverage_rate": round(score / total * 100, 1) if total else 0.0,
        "verdicts": verdicts,
    }


# ========== 审计执行 ==========

async def audit_template_coverage(
    provider: Any,
    items: list,
    *,
    concurrency: int = 2,
    cancel_event: Any = None,
) -> dict:
    """对全部映射节点执行覆盖审计（有界并发）。

    Args:
        provider: AI 提供商
        items: ``map_paragraphs_to_nodes`` 产出（每项需含 generated 正文）
        concurrency: 并发上限（审计是逐节点 AI 调用，默认 2 控制成本）
        cancel_event: 取消信号

    Returns:
        汇总报告 dict（见 ``summarize_verdicts``）；无 items 时 total=0。
    """
    if not items:
        return {
            "total": 0, "covered": 0, "partial": 0, "missing": 0,
            "conflict": 0, "coverage_rate": 0.0, "verdicts": [],
        }

    sem = asyncio.Semaphore(max(1, int(concurrency)))
    total_paragraphs = sum(len(it.get("paragraphs") or []) for it in items)

    async def _audit_one(item: dict) -> list:
        async with sem:
            if cancel_event is not None and cancel_event.is_set():
                return []
            allowed = {str(p.get("id")) for p in item.get("paragraphs") or []}
            messages = build_coverage_audit_messages(item)
            try:
                raw = await provider.chat(messages, max_tokens=1500, temperature=0.1)
                text = raw if isinstance(raw, str) else (raw or {}).get("content", str(raw))
            except Exception as e:
                logger.warning("[覆盖审计] 节点 %s 审计调用失败: %s", item.get("node_id"), e)
                return []
            return parse_coverage_verdicts(text, allowed)

    results = await asyncio.gather(*[_audit_one(it) for it in items])
    verdicts = [v for group in results for v in group]
    return summarize_verdicts(verdicts, total_paragraphs)


async def repair_coverage_gaps(
    provider: Any,
    items: list,
    verdicts: list,
    content_by_node: dict,
    *,
    max_rounds: int = MAX_REPAIR_ROUNDS,
    concurrency: int = 2,
    cancel_event: Any = None,
) -> tuple:
    """对覆盖缺口执行修复循环（最多 ``max_rounds`` 轮）。

    策略（对齐易标 runOriginalPlanCoverageAuditIfEnabled）：
      1. 每轮找出仍有缺口的节点，生成补写提示词 → AI 补写 → 更新内容映射；
      2. 补写后**只重审被修过的节点**（成本有界）；
      3. 缺口清零或轮数用尽即停止——仍不达标如实返回，不无限循环。

    Returns:
        (content_by_node, report)：content_by_node 为更新后的 {node_id: 正文}；
        report 含最终判定与 repairs 记录 [{node_id, round, status_before}]。
    """
    verdict_by_id = {v["id"]: v for v in verdicts}
    content_by_node = dict(content_by_node)
    repairs: list = []

    for round_no in range(1, max_rounds + 1):
        if cancel_event is not None and cancel_event.is_set():
            break
        # 找出仍有缺口的节点（按节点聚合其段落的最新判定）
        gap_items = []
        for item in items:
            gaps = []
            for p in item.get("paragraphs") or []:
                v = verdict_by_id.get(str(p.get("id")))
                if v and v["status"] in GAP_VERDICTS:
                    gaps.append(dict(p, status=v["status"], reason=v.get("reason", "")))
            if gaps:
                gap_items.append((item, gaps))
        if not gap_items:
            break

        logger.info(
            "[覆盖审计] 第 %d/%d 轮修复：%d 个节点存在覆盖缺口",
            round_no, max_rounds, len(gap_items),
        )
        sem = asyncio.Semaphore(max(1, int(concurrency)))

        async def _patch_one(item: dict, gaps: list):
            async with sem:
                node_id = str(item.get("node_id"))
                current = content_by_node.get(node_id) or item.get("generated") or ""
                messages = build_coverage_patch_messages(
                    item.get("node_title") or "", current, gaps
                )
                try:
                    raw = await provider.chat(messages, max_tokens=4000, temperature=0.3)
                    text = raw if isinstance(raw, str) else (raw or {}).get("content", str(raw))
                except Exception as e:
                    logger.warning("[覆盖审计] 节点 %s 补写调用失败: %s", node_id, e)
                    return None
                if not text or not text.strip():
                    return None
                return node_id, text.strip(), gaps[0]["status"]

        patched = await asyncio.gather(*[_patch_one(it, g) for it, g in gap_items])
        changed_nodes = set()
        for res in patched:
            if res is None:
                continue
            node_id, new_text, status_before = res
            content_by_node[node_id] = new_text
            changed_nodes.add(node_id)
            repairs.append({
                "node_id": node_id, "round": round_no, "status_before": status_before,
            })

        if not changed_nodes:
            logger.warning("[覆盖审计] 本轮无任何补写成功，停止修复")
            break

        # 重审被修过的节点（成本有界：只审改动项）
        re_items = []
        for item in items:
            if str(item.get("node_id")) not in changed_nodes:
                continue
            re_items.append(dict(item, generated=content_by_node.get(str(item.get("node_id")) or "")))
        if re_items:
            re_report = await audit_template_coverage(
                provider, re_items, concurrency=concurrency, cancel_event=cancel_event
            )
            for v in re_report.get("verdicts") or []:
                verdict_by_id[v["id"]] = v

    total_paragraphs = sum(len(it.get("paragraphs") or []) for it in items)
    report = summarize_verdicts(list(verdict_by_id.values()), total_paragraphs)
    report["repairs"] = repairs
    return content_by_node, report


# ========== 门禁与编排 ==========

def template_coverage_audit_enabled() -> bool:
    """开关：``TEMPLATE_COVERAGE_AUDIT_ENABLED``，默认关（兼容现有行为）。"""
    raw = str(os.getenv("TEMPLATE_COVERAGE_AUDIT_ENABLED", "false") or "false").strip().lower()
    return raw in {"1", "true", "on", "yes"}


def load_project_templates(project_id: int) -> list:
    """加载项目的就绪模板（同步 DB 读；由调用方派发到线程池）。"""
    from ..db import get_db

    with get_db(readonly=True) as conn:
        try:
            rows = conn.execute(
                "SELECT id, filename, file_path, outline_json FROM project_templates "
                "WHERE project_id = ? AND status = 'ready' ORDER BY id",
                (project_id,),
            ).fetchall()
        except Exception as e:  # 旧库缺列等异常：按无模板处理
            logger.debug("[覆盖审计] 模板查询失败（按无模板处理）: %s", e)
            return []
    return [dict(r) for r in rows]


def split_template_file(template: dict) -> list:
    """读取模板文件并拆分原方案段落（IO + 解析，同步；调用方派发线程池）。"""
    from .files import extract_text_from_path
    from .template_expansion import TemplateExpansionService

    file_path = Path(str(template.get("file_path") or ""))
    if not file_path.exists():
        logger.warning("[覆盖审计] 模板文件不存在: %s", file_path)
        return []
    try:
        text = extract_text_from_path(file_path)
    except Exception as e:
        logger.warning("[覆盖审计] 模板文本提取失败 %s: %s", file_path, e)
        return []
    if not text or not text.strip():
        return []
    return TemplateExpansionService().split_template_to_sections(text)


async def run_template_coverage_audit_if_enabled(
    project_id: int,
    provider: Any,
    results: list,
    *,
    cancel_event: Any = None,
    max_repair_rounds: int = MAX_REPAIR_ROUNDS,
) -> Any:
    """扩写模式下的原方案覆盖率审计门禁 + 编排（对齐易标
    ``runOriginalPlanCoverageAuditIfEnabled``）。

    门禁（全部满足才执行，否则零开销返回 None）：
      1. ``TEMPLATE_COVERAGE_AUDIT_ENABLED`` 为真（默认关）；
      2. 项目存在就绪模板（走了「已有方案扩写」流程）；
      3. results 非空且未被取消。

    执行：加载模板 → 拆分段落 → 映射到叶子节点 → 审计 → 缺口修复
    （≤ ``max_repair_rounds`` 轮）→ 就地更新 results 的 content
    （与 consistency_audit_content 同模式）→ 落库报告。

    Returns:
        报告 dict（含 unmapped/repairs）；未执行时返回 None。
    """
    if not template_coverage_audit_enabled():
        return None
    if not results or (cancel_event is not None and cancel_event.is_set()):
        return None

    templates = await asyncio.to_thread(load_project_templates, project_id)
    if not templates:
        logger.info("[覆盖审计] 项目无就绪模板，跳过覆盖率审计")
        return None

    # 叶子节点 = results 本身（正文生成的最小单位即叶子）
    leaf_nodes = [
        {"id": r.get("id"), "title": r.get("title", "")}
        for r in results
        if isinstance(r, dict) and r.get("id") is not None
    ]
    content_by_node = {
        str(r.get("id")): (r.get("content") or "")
        for r in results
        if isinstance(r, dict) and r.get("id") is not None
    }

    all_items: list = []
    unmapped_all: list = []
    for tpl in templates:
        sections = await asyncio.to_thread(split_template_file, tpl)
        if not sections:
            continue
        items, unmapped = map_paragraphs_to_nodes(sections, leaf_nodes)
        all_items.extend(items)
        unmapped_all.extend(unmapped)

    if not all_items:
        logger.info("[覆盖审计] 原方案段落未能映射到任何目录节点，跳过")
        return {
            "total": 0, "covered": 0, "partial": 0, "missing": 0,
            "conflict": 0, "coverage_rate": 0.0, "verdicts": [],
            "unmapped": unmapped_all, "repairs": [],
        }

    for item in all_items:
        item["generated"] = content_by_node.get(str(item.get("node_id")), "")

    report = await audit_template_coverage(provider, all_items, cancel_event=cancel_event)
    content_by_node, report = await repair_coverage_gaps(
        provider,
        all_items,
        report.get("verdicts") or [],
        content_by_node,
        max_rounds=max_repair_rounds,
        cancel_event=cancel_event,
    )
    report["unmapped"] = unmapped_all

    # 就地回写 results（与 consistency_audit_content 同模式：修复后的正文
    # 必须回到 results，后续落库/导出才拿得到）
    by_id = {str(r.get("id")): r for r in results if isinstance(r, dict)}
    for node_id, new_content in content_by_node.items():
        r = by_id.get(node_id)
        if r is not None and new_content and new_content != (r.get("content") or ""):
            r["content"] = new_content

    await asyncio.to_thread(_persist_coverage_report, project_id, report)
    logger.info(
        "[覆盖审计] 完成：总段落 %d，covered=%d partial=%d missing=%d conflict=%d，"
        "覆盖率 %.1f%%，修复 %d 次",
        report.get("total", 0), report.get("covered", 0), report.get("partial", 0),
        report.get("missing", 0), report.get("conflict", 0),
        report.get("coverage_rate", 0.0), len(report.get("repairs") or []),
    )
    return report


def _persist_coverage_report(project_id: int, report: dict) -> None:
    """把内容覆盖率报告写入该项目全部就绪模板的 content_coverage_json 列。"""
    from ..db import get_db

    verdicts = list(report.get("verdicts") or [])[:200]
    payload = json.dumps(
        {
            "total": report.get("total", 0),
            "covered": report.get("covered", 0),
            "partial": report.get("partial", 0),
            "missing": report.get("missing", 0),
            "conflict": report.get("conflict", 0),
            "coverage_rate": report.get("coverage_rate", 0.0),
            "unmapped": report.get("unmapped", [])[:100],
            "repairs": report.get("repairs", [])[:100],
            "verdicts": verdicts,
        },
        ensure_ascii=False,
    )
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE project_templates SET content_coverage_json = ?, "
                "updated_at = datetime('now', 'localtime') "
                "WHERE project_id = ? AND status = 'ready'",
                (payload, project_id),
            )
    except Exception as e:
        # 旧库缺列（迁移未跑）不阻断审计主流程
        logger.warning("[覆盖审计] 报告落库失败（不影响审计结果）: %s", e)
