"""workflows 子模块：workflows_expand（由 scripts/split_workflows.py 自动生成，勿手改）"""

import asyncio
import datetime
import json
import logging
import uuid
from typing import Any

from ...db import get_db

from ...utils import count_words as _count_words


def _words_excluding_chart_payload(content: str) -> int:
    """FIX 2026-10-08（P2 字数口径）：剔除图表围栏载荷（```mermaid / ```chart-json
    代码体）后再计字数。图表代码字符（节点定义、任务表等）不计入正文字数，
    否则大图表章节被误判「字数达标」而跳过扩写；探测字数同理失真。"""
    try:
        from ...services.content_cleaning import iter_chart_fences

        text = content
        for _lang, body, _mt in iter_chart_fences(content):
            if body:
                text = text.replace(body, "")
        return _count_words(text)
    except Exception:
        return _count_words(content)
from ...utils.json_parser import extract_json_from_text
from .providers.base import AIMessage


logger = logging.getLogger(__name__)


from .workflows_base import with_timeout
from .workflows_sections import generate_section_content


EARLY_CONTENT_PROBE_COUNT = 3

OUTLINE_EXPANSION_TARGET_RATIO = 0.8

MAX_OUTLINE_EXPANSION_ROUNDS = 3

MIN_SECTION_EXPANSION_INCREMENT = 800

# ===== ENHANCE 2026-09-15：一致性审计的分组口径与并发 =====
#: 单组送审正文上限（字符）。
#: BUG-FIX 2026-09-15：原实现三处口径互相矛盾 ——
#:   ① 分组预算 300000 用的是 `_count_words` 的「字」；
#:   ② 每节正文只取 `content[:5000]` 字符；
#:   ③ 整组 prompt 再截 `[:30000]` 字符。
#: 结果是「30 万字一组」实际只送审不到 3 万字符，绝大多数章节被**静默丢弃**
#: 且日志里看不出任何异常，审计覆盖率远低于预期。现统一为「字符」口径：
#: 分组预算 == 提示词上限，超长只发生在单节截断处并且**会记日志**。
AUDIT_GROUP_CHAR_LIMIT = 30000
#: 单节送审上限（字符）。超长章节只能抽样送审，截断时打 warning。
AUDIT_SECTION_CHAR_LIMIT = 5000
#: 组间并发上限。首组仍串行（预热 prompt cache），其余组按该上限并发。
AUDIT_GROUP_CONCURRENCY = 3


async def estimate_total_words_from_probe(
    provider: Any,
    probe_targets: list[dict],
    tender_context: str,
    summary: str,
    industry_profile: str,
    scoring_standards: list[dict],
    writing_length: str,
    cancel_event: asyncio.Event | None = None,
    project_id: int | None = None,
    total_leaves: int | None = None,
) -> int:
    """采样预估总字数（参考易标模式：采样头+中+尾3个小节）。

    在正文生成前，采样3个叶子节点（头、中、尾）生成正文，
    根据平均字数预估全部叶子节点的总字数。

    Args:
        probe_targets: 采样的3个叶子节点
        tender_context: 招标文件全文
        summary: 项目摘要
        industry_profile: 行业背景
        scoring_standards: 评分标准列表
        writing_length: 写作长度
        cancel_event: 取消事件
        total_leaves: 项目叶子节点总数（用于按真实规模推算）
    Returns:
        int: 预估总字数
    """
    if not probe_targets:
        return 0

    # 性能修复：3 个采样章节由串行改为并发（受全局 AI 并发闸约束），
    # 采样阶段耗时约降为原来的 1/3
    async def _probe_one(target: dict) -> int:
        scoring_standard = ""
        scoring_id = target.get("scoring_point_id")
        if scoring_id and scoring_standards:
            for s in scoring_standards:
                if s.get("id") == scoring_id:
                    scoring_standard = s.get("standard", "")
                    break
        # BUG-FIX 2026-09-14（中）：generate_section_content 的真实签名是
        # (provider, section: dict, context: dict, progress_callback)，其中
        # **section 是必填位置参数**。原实现只传 provider + 一堆关键字参数，
        # await 协程时必定抛
        #   TypeError: missing 1 required positional argument: 'section'
        # 而外层 gather(return_exceptions=True) 会静默吞掉 → probe_count=0
        # → 字数预估恒为 0 → "字数不足自动补目录"功能从未生效。
        # 修复：按真实签名构造 section 与 context。
        probe_section = {
            "id": target.get("id"),
            "title": target.get("title", ""),
            "level": target.get("level", 3),
            "project_id": project_id,
            "target_words": target.get("target_words"),
        }
        probe_context = {
            "summary": summary,
            "industry_profile": industry_profile,
            "scoring_context": scoring_standard,
            "section_path": target.get("path", ""),
            "content_type": target.get("section_type", "text"),
            "writing_length": writing_length,
            "target_words": target.get("target_words"),
            "section_level": target.get("level", 3),
            "cancel_event": cancel_event,
            "extract_charts": False,
            "project_id": project_id,
            # BUG-FIX 2026-09-16：探针必须绕过章节内容缓存，见
            # `_do_generate_section_with_optimizations` 中 use_cache 的说明。
            # 否则被抽中的 3 个章节会命中这份「无事实变量 / 无同级上下文」的
            # 采样正文，正式生成直接复用残次品。
            "use_cache": False,
        }
        content = await generate_section_content(provider, probe_section, probe_context)
        return _count_words(content)

    total_generated = 0
    probe_count = 0

    probe_results = await asyncio.gather(
        *(_probe_one(t) for t in probe_targets), return_exceptions=True
    )
    for pr in probe_results:
        if isinstance(pr, BaseException):
            if isinstance(pr, asyncio.CancelledError):
                continue
            logger.warning("字数采样失败: %s", pr)
        elif pr:
            total_generated += pr
            probe_count += 1

    if probe_count == 0:
        return 0

    avg_words = total_generated // probe_count

    # BUG 修复：原实现 estimated_total_leaves = max(len(probe_targets), probe_count * 10)
    # 硬编码假设项目约 30 个叶子节点，与真实章节数完全无关——
    # 100 章项目预估被低估 3 倍以上（误触发补目录），10 章小项目被高估 3 倍
    # （永不触发）。改为由调用方传入真实叶子节点总数 total_leaves 推算；
    # 未传时才回退到保守估算。
    if total_leaves and total_leaves > 0:
        return avg_words * total_leaves
    estimated_total_leaves = max(len(probe_targets), probe_count * 10)
    return avg_words * estimated_total_leaves


async def expand_outline_for_content(
    provider: Any,
    summary: str,
    scoring_points: str,
    industry_profile: str,
    tender_context: str,
    project_scope: str,
    current_outline: list[dict],
    min_additional_chapters: int = 2,
) -> list[dict]:
    """补目录：当字数不足时，补充二三级目录以增加内容量。

    参考易标模式：采样3个小节预估字数 → 若不足目标字数的80% → 执行最多3轮补目录。

    Args:
        provider: AI 提供商实例
        summary: 项目摘要
        scoring_points: 评分点 JSON
        industry_profile: 行业背景
        tender_context: 招标文件全文
        project_scope: 承包范围
        current_outline: 当前目录树
        min_additional_chapters: 最少补充章节数
    Returns:
        list[dict]: 补充后的目录树
    """
    outline_json = json.dumps(current_outline, ensure_ascii=False, indent=2)

    expand_prompt = (
        f"你是一个标书目录扩展专家。当前目录字数不足，需要补充更多二三级目录来增加内容量。\n\n"
        f"【补充原则】\n"
        f"1. 只补充二级和三级子目录，不修改一级目录结构\n"
        f"2. 在每个一级目录下补充 1-3 个新的二级目录，每个二级下补充 2-3 个三级目录\n"
        f"3. 补充的目录必须与所属一级章节的主题相关，不得跑题\n"
        f"4. 补充的目录要具体、可操作，不得使用'其他''补充'等占位词\n"
        f"5. 保持三级目录结构，新增三级目录的 children 为空数组\n\n"
        f"【当前目录】\n{outline_json}\n\n"
        f"【项目概述】\n{summary}\n\n"
        f"【评分点】\n{scoring_points}\n\n"
        f"请直接输出补充后的完整目录 JSON 数组。严禁附加解释文字。"
    )

    try:
        messages = [
            AIMessage(
                role="system",
                content="你是一个标书目录扩展专家，擅长补充完善目录结构。请直接输出 JSON 数组。",
            ),
            AIMessage(role="user", content=expand_prompt),
        ]
        result = await with_timeout(provider.chat(messages, temperature=0.4), timeout=60)
        parsed = extract_json_from_text(result)
        if isinstance(parsed, list) and len(parsed) > 0:
            return parsed
    except Exception as e:
        logger.warning("补目录失败: %s", e)

    return current_outline


def section_expand_threshold(
    section: dict,
    flat_minimum: int,
    level2_default: int = 3500,
    writing_length: str | None = None,
) -> int:
    """单章节的扩写阈值（CONVERGE 2026-10-12：目标解析转发唯一权威口径）。

    达标线 = 有效目标字数 × 80%，不超过 ``flat_minimum``，保底 200 字。

    口径（唯一公式，见 ``services/word_target_resolver.py``）：
      - **L1/L2**：父节点，不生成正文，不参与扩写，返回 0；
      - **优先级**：单节点 ``target_words`` > 单节点 ``word_count_tier``
        > **L3 自身正文 300±20%** > 全局 ``writing_length`` 档位 > 默认档位。

    CONVERGE 行为修正（唯一一处）：此前本函数对「L3 无档位覆盖但有全局
    ``writing_length``」按档位优先（detailed → 1600），与生成提示词侧 /
    校验页 / 压缩侧 / R12 警告的「L3 自身正文 300 字优先」相反，属真实口径
    分叉（兜底扩写会把本已达标的小节误判为不足并烧 token）。现已统一。

    Args:
        section: 章节字典（含 level / target_words / word_count_tier）
        flat_minimum: 平铺阈值（所有档位共享的扩写上限）
        level2_default: [已废弃] 不再使用
        writing_length: 全局档位 key（brief/standard/detailed/long）

    Returns:
        int: 扩写达标线（字数不足此值则触发扩写）；L1/L2 返回 0
    """
    from ..word_target_resolver import resolve_section_word_target_from_mapping

    resolved = resolve_section_word_target_from_mapping(section, writing_length=writing_length)
    if not resolved:
        return 0
    target = int(resolved.target)
    if target <= 0:
        return 0

    # 扩写达标线 = 目标字数 × 80%，不超过平铺阈值，保底 200 字
    return max(200, min(int(flat_minimum), int(target * 0.8)))


def collect_deficient_sections(
    results: list[dict],
    sections_by_id: dict,
    flat_minimum: int,
    level2_default: int = 3500,
    writing_length: str | None = None,
) -> list[dict]:
    """找出「字数不足」的章节，按字数/阈值比升序（最缺的排最前）。

    只统计 status=completed 且有内容的章节（空章节属于「待生成」，归补全生成管）。
    返回项：`{result, section, words, threshold, deficit}`，扩写与统计共用同一判定。
    """
    deficient: list[dict] = []
    for r in results or []:
        if (r or {}).get("status") != "completed":
            continue
        content = r.get("content") or ""
        if not content.strip():
            continue
        section = sections_by_id.get(r.get("id"))
        if not section:
            continue
        threshold = section_expand_threshold(section, flat_minimum, level2_default, writing_length)
        words = _words_excluding_chart_payload(content)
        if words < threshold:
            deficient.append(
                {
                    "result": r,
                    "section": section,
                    "words": words,
                    "threshold": threshold,
                    "deficit": threshold - words,
                }
            )
    deficient.sort(key=lambda item: item["words"] / max(item["threshold"], 1))
    return deficient


async def expand_content_fallback(
    provider: Any,
    results: list[dict],
    sections_by_id: dict,
    minimum_words: int,
    tender_context: str,
    summary: str,
    industry_profile: str,
    writing_length: str,
    cancel_event: asyncio.Event | None = None,
    max_expansion_rounds: int = 3,
    concurrency: int = 2,
    level2_default: int = 3500,
    on_section_done: Any = None,
) -> list[dict]:
    """兜底扩写：对所有字数不足的章节做 insert/replace 局部扩写（并发）。

    ENHANCE 2026-09-15 重写（旧实现的两个硬伤）：
    1. 旧实现每轮只挑**全局字数最少的一个**章节、最多 5 轮 → 一次调用最多救 5 章
       且完全串行，对几百章的项目杯水车薪；现在筛选全部不足章节，受
       `asyncio.Semaphore(concurrency)` 约束并发扩写，单章失败不影响其它章节；
    2. 阈值改为 per-section（见 `section_expand_threshold` 的口径说明）。

    Args:
        results: 生成结果列表 [{id, content, status, ...}]（就地更新 content）
        minimum_words: 平铺阈值下限（writing_length 映射值）
        concurrency: 并发上限
        max_expansion_rounds: 每个章节的最大扩写轮次
        on_section_done: 单章扩写达标后的回调 `(section_id, title)`，同步或异步均可
    Returns:
        list[dict]: 原results列表（扩写成功的条目已更新 content）
    """
    if not results:
        return results

    deficient = collect_deficient_sections(results, sections_by_id, minimum_words, level2_default, writing_length)
    if not deficient:
        logger.info("兜底扩写：没有字数不足的章节（阈值=per-section×80%%），跳过")
        return results

    concurrency = max(1, int(concurrency or 1))
    logger.info(
        "兜底扩写：%d 个章节字数不足（并发=%d，每章最多 %d 轮，总缺口 %d 字）",
        len(deficient),
        concurrency,
        max_expansion_rounds,
        sum(entry["deficit"] for entry in deficient),
    )
    semaphore = asyncio.Semaphore(concurrency)
    reached = 0

    async def _expand_one(entry: dict) -> None:
        nonlocal reached
        async with semaphore:
            if cancel_event and cancel_event.is_set():
                return
            r = entry["result"]
            section = entry["section"]
            threshold = entry["threshold"]
            title = section.get("title", "")
            content = r.get("content") or ""
            for round_idx in range(max(1, max_expansion_rounds)):
                if cancel_event and cancel_event.is_set():
                    return
                words = _words_excluding_chart_payload(content)
                if words >= threshold:
                    break
                expansion_target = max(words * 2, words + MIN_SECTION_EXPANSION_INCREMENT)
                expansion_target = min(expansion_target, threshold * 2)

                expansion_prompt = (
                    f"你是一个标书正文扩写专家。请扩写以下章节正文内容，使其更充实、专业。\n\n"
                    f"【扩写原则】\n"
                    f"1. 在原有内容基础上扩充，不删除原有内容\n"
                    f"2. 使用 insert/replace 方式：在段落之间插入新内容，或替换过于简略的段落\n"
                    f"3. 扩充内容必须与章节主题相关，不得跑题\n"
                    f"4. 保持标书正文的专业风格，使用行业术语\n"
                    f"5. 补充具体的技术参数、标准编号、施工方法、管理措施\n"
                    f"6. 目标字数：从 {words} 字扩写到 {expansion_target} 字以上\n\n"
                    f"【章节标题】\n{title}\n\n"
                    f"【当前正文】\n{content}\n\n"
                    f"请直接输出扩写后的完整正文，严禁附加解释文字。"
                )
                try:
                    messages = [
                        AIMessage(
                            role="system",
                            content="你是一个标书正文扩写专家，擅长在保留原有内容的基础上扩充正文。",
                        ),
                        AIMessage(role="user", content=expansion_prompt),
                    ]
                    # 正文可达数千字，120s 超时与章节正文生成保持一致
                    expanded = await with_timeout(
                        provider.chat(messages, temperature=0.7, cancel_event=cancel_event),
                        timeout=120,
                    )
                    expanded_stripped = (expanded or "").strip()
                    expanded_words = _count_words(expanded_stripped)
                    # 质量闸：必须变长且至少达到原字数的 80%（字数口径统一用 _count_words）
                    if (
                        expanded_stripped
                        and len(expanded_stripped) > len(content)
                        and expanded_words > words * 0.8
                    ):
                        content = expanded_stripped
                        r["content"] = content
                        logger.info(
                            "兜底扩写：%s 第 %d 轮 %d→%d 字（阈值 %d）",
                            title,
                            round_idx + 1,
                            words,
                            expanded_words,
                            threshold,
                        )
                    else:
                        logger.warning(
                            "兜底扩写：%s 第 %d 轮未有效增加内容（%d 字），放弃该章节",
                            title,
                            round_idx + 1,
                            expanded_words,
                        )
                        break
                except Exception as e:
                    logger.warning("兜底扩写：%s 第 %d 轮失败: %s", title, round_idx + 1, e)
                    break
            if _words_excluding_chart_payload(content) >= threshold:
                reached += 1
                if on_section_done is not None:
                    try:
                        outcome = on_section_done(r.get("id"), title)
                        if asyncio.iscoroutine(outcome):
                            await outcome
                    except Exception:
                        logger.debug("兜底扩写 on_section_done 回调失败（忽略）", exc_info=True)

    await asyncio.gather(*(_expand_one(entry) for entry in deficient), return_exceptions=True)
    logger.info("兜底扩写完成：%d/%d 个章节达标（共 %d 个不足）", reached, len(deficient), len(deficient))
    return results


def _format_global_facts_for_audit(global_facts: Any) -> str:
    """把扁平全局事实列表格式化为审计提示词块。

    ENHANCE 2026-10-15（对齐参考软件 `buildConsistencyAuditMessages` 的
    「Step04 全局事实变量」）：审计输入必须显式携带**结构化全局事实**，
    而不是只依赖摘要里的叙事文本。事实项扁平结构为 {key,label,value,category}，
    这里按 key/label/value 渲染（与 `workflows_sections._format_facts_list`
    同一取值口径）。

    Args:
        global_facts: 全局事实列表（list[dict]）或已格式化文本（str）。
    Returns:
        markdown 文本；空输入返回空串（审计退化为原历史行为）。
    """
    if not global_facts:
        return ""
    if isinstance(global_facts, str):
        return global_facts.strip()
    lines: list[str] = []
    items = global_facts if isinstance(global_facts, list) else [global_facts]
    for item in items:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or item.get("key") or item.get("title") or "").strip()
        value = str(item.get("value") or item.get("content") or "").strip()
        if not label or not value:
            continue
        lines.append(f"- **{label}**：{value}")
    return "\n".join(lines)


def _split_section_into_chunks(content: str, limit: int) -> list[str]:
    """把超长正文按段落切成多块（每块不超 ``limit``；单段超限按字符硬切）。

    ENHANCE 2026-10-15：AI 审计不再对超长节「截断送审」，改为**分块全量送审**，
    消除正文尾部永远不被审计的覆盖盲区。切分尽量在换行处断开（保留语义边界），
    无法断开的连续长行按字符硬切。块之间用 ``\\n\\n`` 分隔。
    """
    content = str(content or "")
    if not content:
        return []
    if limit <= 0 or len(content) <= limit:
        return [content]
    paragraphs = content.split("\n")
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for para in paragraphs:
        # 单段超过一块上限：先把当前块落盘，再对该段字符硬切，保证每块 <= limit
        while len(para) > limit:
            if current:
                chunks.append("\n".join(current))
                current, current_len = [], 0
            chunks.append(para[:limit])
            para = para[limit:]
        mark_len = len(para) + (1 if current else 0)
        if current and current_len + mark_len > limit:
            chunks.append("\n".join(current))
            current, current_len = [], 0
        current.append(para)
        current_len += len(para) + (1 if len(current) > 1 else 0)
    if current:
        chunks.append("\n".join(current))
    return chunks


def _audit_section_render_chars(content: str, limit: int) -> int:
    """该节实际会送审的渲染字符数（供分组按真实载荷装箱）。

    超长节 = 全部正文长度 + 每块续送前的「( 标题 第 i/n 块 )」提示词与分隔开销
    （按 16 字符常数预算），保证普通节行为不变。
    """
    if len(content) <= limit:
        return len(content)
    chunks = _split_section_into_chunks(content, limit)
    if len(chunks) <= 1:
        return len(content)
    return len(content) + (len(chunks) - 1) * 16


def _split_audit_groups(completed_sections: list[dict]) -> list[list[dict]]:
    """把待审计小节按 `AUDIT_GROUP_CHAR_LIMIT` 顺序装箱成组（字符口径）。

    装箱用的是「本节实际会送审的字符数」（即截断后的长度），因此
    **不会出现"分组预算很大、实际送审被截掉大半"的口径错位**。

    ENHANCE 2026-10-15：超长节不再按 `AUDIT_SECTION_CHAR_LIMIT` 截断计重，
    而是按「分块全量送审后的渲染字符数」计重（`_audit_section_render_chars`），
    超长节因其真实载荷自动独立成组，普通节行为不变。
    """
    groups: list[list[dict]] = []
    current: list[dict] = []
    current_chars = 0
    for section in completed_sections:
        content = section.get("content") or ""
        chars = _audit_section_render_chars(content, AUDIT_SECTION_CHAR_LIMIT)
        if current and current_chars + chars > AUDIT_GROUP_CHAR_LIMIT:
            groups.append(current)
            current = []
            current_chars = 0
        current.append(section)
        current_chars += chars
    if current:
        groups.append(current)
    return groups


async def _repair_conflicts_with_patches(
    provider: Any,
    repairable: list[tuple[dict, dict, dict]],
    sections_by_id: dict,
    facts_text: str,
    cancel_event: asyncio.Event | None,
) -> int:
    """引入项 B（2026-10-15，对齐易标 `applyExactConsistencyPatch`）：
    用 old_text/new_text 精确补丁修复审计冲突，替代整节重写。

    复用两个既有正本，不新设补丁引擎：
    - `consistency_repair.generate_patches`：带行号正文提示词、补丁别名归一、
      section_id 越界白名单过滤、failure_note 失败回喂；
    - `text_patch.apply_patches`：行区间优先、全文唯一精确匹配、
      0/多命中拒绝、逐条错误隔离、失衡比例保护。

    重试语义（与易标 CONSISTENCY_REPAIR_MAX_ATTEMPTS 一致）：本轮全部补丁
    被拒的小节，把失败原因回喂重新生成补丁，至多 `content_repair_max_attempts()`
    轮（默认 1 = 不重试）。只有命中才改字，绝不重写整节；失败保持原正文
    （finding.repaired=False 落库供用户跟进）。

    Args:
        repairable: `(finding, conflict, target)` 三元组列表（已由调用方确认
            有建议、有目标小节且有正文）。
        sections_by_id: {id: section_dict}，取章节标题供提示词展示。
        facts_text: 一致性基准文本（结构化全局事实优先，退化为项目摘要）。
    Returns:
        成功修复（至少一条补丁落文）的小节数。
    """
    from .. import text_patch
    from ..consistency_repair import generate_patches
    from ..facts_service import content_repair_max_attempts

    if not repairable:
        return 0

    # 按小节归并冲突（易标同为按章修复：一次请求携带该章全文与全部冲突）
    by_sid: dict[str, dict] = {}
    for finding, conflict, target in repairable:
        sid = str(conflict.get("section_id"))
        entry = by_sid.setdefault(
            sid,
            {
                "target": target,
                "title": (sections_by_id.get(target.get("id")) or {}).get("title", ""),
                "issues": [],
                "findings": [],
            },
        )
        entry["issues"].append(
            f"- {conflict.get('issue', '')}（修复建议：{conflict.get('suggestion', '')}）"
        )
        entry["findings"].append(finding)

    conflicts_text = "\n".join(
        f"【小节 {sid}】\n" + "\n".join(e["issues"]) for sid, e in by_sid.items()
    )

    repaired_count = 0
    pending = dict(by_sid)
    failure_note = ""
    max_attempts = max(1, content_repair_max_attempts())
    for _attempt in range(max_attempts):
        if not pending or (cancel_event and cancel_event.is_set()):
            break
        sections_payload = [
            {
                "node_id": sid,
                "title": e["title"],
                "content": e["target"].get("content", "") or "",
            }
            for sid, e in pending.items()
        ]
        patches, _unresolved, error = await with_timeout(
            generate_patches(
                provider,
                sections=sections_payload,
                conflicts_text=conflicts_text,
                facts_text=facts_text,
                failure_note=failure_note,
            ),
            timeout=120,
        )
        patches_by_sid: dict[str, list[dict]] = {}
        for patch in patches:
            patches_by_sid.setdefault(str(patch.get("section_id")), []).append(patch)

        reject_notes: list[str] = []
        if error:
            reject_notes.append(f"- [整体] {error}")
        for sid, entry in list(pending.items()):
            sec_patches = patches_by_sid.get(sid)
            if sec_patches is None:
                # AI 本轮未对该小节给出补丁（可能判为无需修/放进 unresolved）：
                # 不再重试该节，保持原正文。
                if not error:
                    pending.pop(sid, None)
                continue
            apply_result = text_patch.apply_patches(
                entry["target"].get("content", ""), sec_patches
            )
            if apply_result.get("changed"):
                entry["target"]["content"] = apply_result["content"]
                for finding in entry["findings"]:
                    finding["repaired"] = True
                repaired_count += 1
                pending.pop(sid, None)
            else:
                # 全部补丁被拒（未命中/不唯一/失衡）：该节正文未被改动。
                for err_msg in apply_result.get("errors") or ["补丁均未应用"]:
                    reject_notes.append(f"- [小节 {sid}] {err_msg}")

        if not pending or not reject_notes or _attempt + 1 >= max_attempts:
            break
        # 失败回喂：只重试全军覆没的小节，正文快照取当前最新值
        failure_note = "\n".join(reject_notes[-20:])
        logger.info(
            "一致性补丁修复重试（第 %d 轮）：%d 个小节待重做，失败原因已回喂",
            _attempt + 2,
            len(pending),
        )

    return repaired_count


async def _audit_one_group(
    provider: Any,
    group: list[dict],
    group_idx: int,
    total_groups: int,
    sections_by_id: dict,
    results: list[dict],
    summary: str,
    cancel_event: asyncio.Event | None,
    *,
    global_facts_text: str = "",
    completeness_text: str = "",
) -> tuple[int, list[dict]]:
    """审计并修复一组正文，返回 (成功修复的小节数, 本组审计发现列表)。

    拆成独立协程是为了让多组可以并发（首组仍串行预热 prompt cache）。
    异常由调用方 `_run` 兜住，单组失败不影响其余组。

    G1 修复（2026-10-13）：返回值从 `int` 扩为 `tuple[int, list[dict]]`，
    把 LLM 解析出的冲突发现（含修复状态）带回给调用方落库——此前发现只用于
    就地修复正文，函数一返回就丢，审计留痕为零。
    每个发现形如 ``{"section_id", "issue", "suggestion", "repaired"}``；
    只有带 `section_id` 的发现会被记录（无归属章节的发现无法落库追溯）。
    """
    if cancel_event and cancel_event.is_set():
        return 0, []

    parts: list[str] = []
    for r in group:
        section = sections_by_id.get(r["id"], {})
        title = section.get("title", "")
        content = r.get("content", "") or ""
        if len(content) > AUDIT_SECTION_CHAR_LIMIT:
            chunks = _split_section_into_chunks(content, AUDIT_SECTION_CHAR_LIMIT)
            if len(chunks) > 1:
                # ENHANCE 2026-10-15：超长节不再截断抽样，改为分块全量送审，
                # 各块带「( 标题 第 i/n 块 )」定位标记；审计返回的 conflicts 仍
                # 以原 section_id 关联，修复时用整节完整正文。
                blocks = [
                    f"（{title or '章节'} 第 {idx + 1}/{len(chunks)} 块）\n{chunk}"
                    for idx, chunk in enumerate(chunks)
                ]
                parts.append(f"=== {title} ===\n" + "\n\n".join(blocks))
                logger.warning(
                    "一致性审计：章节 %s（%s）正文 %d 字符超出单节送审上限 %d，"
                    "已拆 %d 块全量送审",
                    r.get("id"),
                    title or "-",
                    len(content),
                    AUDIT_SECTION_CHAR_LIMIT,
                    len(chunks),
                )
            else:
                parts.append(f"=== {title} ===\n{content}")
        else:
            parts.append(f"=== {title} ===\n{content}")
    group_content = "\n\n".join(parts)

    # ENHANCE 2026-10-15（对齐易标 buildConsistencyAuditMessages）：审计必须
    # 显式携带结构化全局事实作为一致性基准。正文中「已经明写且与之相背」才报
    # 冲突；不报缺失、不评文风、不建议整章重写。
    facts_block = ""
    if global_facts_text:
        facts_block = (
            "【全局事实变量（本项目的统一口径，正文中已明确写出且与其相背的内容才算冲突）】\n"
            f"{global_facts_text[:4000]}\n\n"
            "审计要求：\n"
            "1. 只找正文中已经明确写出、且与全局事实相违背的内容；正文没有涉及某条事实时，不要报告缺失，不要要求补充。\n"
            "2. 不要报告文风、质量、重复、篇幅、表达优化等问题；不要建议重写整章，只指出冲突位置与原因。\n"
            "3. 全文中同一事项出现不同值时，以全局事实变量里的值为准。\n\n"
        )
    completeness_block = ""
    if completeness_text:
        completeness_block = f"【缺值口径】\n{completeness_text}\n\n"

    audit_prompt = (
        f"你是一个标书一致性审计专家。请检查以下正文内容是否与项目事实变量存在冲突或不一致。\n\n"
        f"{facts_block}{completeness_block}"
        f"【项目事实】\n{summary[:5000]}\n\n"
        f"【正文内容（第 {group_idx + 1}/{total_groups} 组）】\n{group_content}\n\n"
        f"【一致性检查清单】\n"
        f"1. 项目名称、工期、质量标准等关键数据是否与项目事实一致\n"
        f"2. 技术参数、标准编号是否合理\n"
        f"3. 不同章节之间对同一事项的描述是否一致\n"
        f"4. 是否存在明显的矛盾或冲突\n\n"
        f"【输出格式】\n"
        f"返回 JSON 对象: {{\n"
        f'  "has_conflicts": true/false,\n'
        f'  "conflicts": [\n'
        f'    {{"section_id": "章节ID", "issue": "问题描述", "suggestion": "修复建议"}}\n'
        f"  ]\n"
        f"}}\n"
        f'如果没有冲突，返回 {{"has_conflicts": false, "conflicts": []}}\n'
        f"严禁附加解释文字。"
    )

    messages = [
        AIMessage(
            role="system",
            content="你是一个标书一致性审计专家，擅长发现正文中的矛盾和不一致。",
        ),
        AIMessage(role="user", content=audit_prompt),
    ]
    audit_result = await with_timeout(
        provider.chat(messages, temperature=0.1, cancel_event=cancel_event), timeout=60
    )
    parsed = extract_json_from_text(audit_result)

    if not isinstance(parsed, dict):
        logger.warning("一致性审计第 %d 组返回非JSON格式，跳过", group_idx + 1)
        return 0, []

    if not (parsed.get("has_conflicts") and parsed.get("conflicts")):
        return 0, []

    conflicts = parsed["conflicts"]
    if not isinstance(conflicts, list):
        logger.warning("一致性审计冲突数据格式异常，跳过")
        return 0, []

    repaired_count = 0
    findings: list[dict] = []

    # 引入项 B（2026-10-15，对齐易标 buildConsistencyRepairMessages）：
    # CONSISTENCY_PATCH_REPAIR_ENABLED 开启时走 old_text/new_text 精确补丁
    # （唯一命中才替换、失败原因回喂重试、绝不重写整节）；默认关 = 保持
    # 下方整节重写路径，提示词与行为逐字不变。
    from ..facts_service import consistency_patch_repair_enabled

    if consistency_patch_repair_enabled():
        repairable: list[tuple[dict, dict, dict]] = []
        for conflict in conflicts:
            if not isinstance(conflict, dict):
                continue
            section_id = conflict.get("section_id")
            issue = conflict.get("issue", "")
            suggestion = conflict.get("suggestion", "")
            if not section_id:
                continue
            finding = {
                "section_id": section_id,
                "issue": issue,
                "suggestion": suggestion,
                "repaired": False,
            }
            findings.append(finding)
            if not suggestion:
                continue
            target = next(
                (r for r in results if str(r.get("id")) == str(section_id)),
                None,
            )
            if target is None or not target.get("content"):
                continue
            repairable.append((finding, conflict, target))
        repaired_count = await _repair_conflicts_with_patches(
            provider,
            repairable,
            sections_by_id,
            global_facts_text or summary[:5000],
            cancel_event,
        )
        return repaired_count, findings

    for conflict in conflicts:
        if not isinstance(conflict, dict):
            continue
        section_id = conflict.get("section_id")
        suggestion = conflict.get("suggestion", "")
        issue = conflict.get("issue", "")
        if not section_id:
            continue
        finding = {
            "section_id": section_id,
            "issue": issue,
            "suggestion": suggestion,
            "repaired": False,
        }
        findings.append(finding)
        if not suggestion:
            continue
        target = next(
            (r for r in results if str(r.get("id")) == str(section_id)),
            None,
        )
        if target is None:
            continue
        current_content = target.get("content", "")
        if not current_content:
            continue
        repair_prompt = (
            f"请修复以下正文中的一致性问题。\n\n"
            f"【问题描述】\n{conflict.get('issue', '')}\n\n"
            f"【修复建议】\n{suggestion}\n\n"
            f"【当前正文】\n{current_content[:5000]}\n\n"
            f"请直接输出修复后的完整正文，仅修改问题部分，保留其他内容不变。"
        )
        try:
            repair_messages = [
                AIMessage(
                    role="system",
                    content="你是一个标书正文修复专家，请根据审计意见精确修复正文。",
                ),
                AIMessage(role="user", content=repair_prompt),
            ]
            repaired = await with_timeout(
                provider.chat(
                    repair_messages,
                    temperature=0.3,
                    cancel_event=cancel_event,
                ),
                timeout=60,
            )
            # BUG 修复：repair_prompt 中 current_content 被 [:5000] 截断，
            # AI 基于截断上下文输出"完整正文"。若原文超过 5000 字，
            # 修复结果会明显短于原文（后半部分被丢弃），而调用方
            # 对变化的内容无长度保护直接存库，造成长章节内容丢失。
            # 增加: 修复结果不得明显短于原文（80% 阈值），否则拒绝应用。
            if (
                repaired
                and len(repaired) > 100
                and _count_words(repaired)
                >= _count_words(current_content) * 0.8
            ):
                target["content"] = repaired
                repaired_count += 1
                finding["repaired"] = True
                logger.info("一致性审计修复：章节 %s 已修复", section_id)
            elif repaired:
                logger.warning(
                    "一致性审计修复结果过短（%d/%d 字），疑似截断，拒绝应用: 章节 %s",
                    _count_words(repaired),
                    _count_words(current_content),
                    section_id,
                )
        except Exception as repair_e:
            logger.warning("一致性审计修复失败: %s", repair_e)

    return repaired_count, findings


def _persist_ai_audit_findings(
    project_id: int,
    total_sections: int,
    findings: list[dict],
) -> None:
    """G1 修复（2026-10-13）：把 AI 一致性审计的发现落库。

    写入两张表（与确定性审计 `ConsistencyAuditor` 同一套数据模型）：

    1. ``consistency_conflicts`` —— 逐条冲突。AI 路径的冲突是自由文本发现
       （LLM 返回 ``section_id`` / ``issue`` / ``suggestion``，没有结构化的
       fact 取值），映射口径：

       - ``conflict_id`` = ``AI-{project_id}-{uuid12}``。确定性路径的 ID 前缀是
         ``conflict_``（见 ``detect_conflicts``），二者永不互撞；
       - ``fact_name`` 取 ``issue``（截 100 字），``description`` 带上修复建议；
       - ``values`` 无结构化取值 → ``[]``；``sources`` 记冲突所在章节 ID；
       - ``severity`` 无 LLM 判断 → 统一 ``medium``（``rules.SEVERITY_DEFAULT``
         口径，与确定性路径「未识别回落 medium」一致）；
       - **已修复**的发现（正文已被 AI 改写落库）直接记 ``resolved=1`` +
         ``resolved_value='AI 自动修复'`` —— 与 ``apply_autofix``「真实落文
         后才标 resolved」的语义一致，不会在待处理列表里留假未解决项；
         未修复成功的留 ``resolved=0`` 供用户跟进。

       写入前先清掉该项目**上一轮 AI 审计**的旧发现（``conflict_id LIKE
       'AI-%'``）——正文已重新生成，旧发现过期；只清 AI 前缀，绝不动确定性
       审计写入的 ``conflict_`` 行（那是「一致性检查」页的权威数据）。

    2. ``consistency_audit_log`` —— 本轮审计摘要（追加，不清历史）。
       ``facts_extracted`` 记 0：AI 路径不做结构化事实抽取，记 0 比编数诚实；
       ``status`` 复用 ``consistency_rules`` 的常量，前端按既有口径渲染。

    本函数只写库、不抛业务异常语义；调用方负责 try/except 兜底。
    """
    from .. import consistency_rules as rules

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        # 1) 清理上一轮 AI 审计的旧发现（见 docstring 的 ID 前缀约定）
        conn.execute(
            "DELETE FROM consistency_conflicts "
            "WHERE project_id = ? AND conflict_id LIKE 'AI-%'",
            (project_id,),
        )

        # 2) 写入本轮发现
        log_conflicts: list[dict] = []
        for finding in findings:
            issue = str(finding.get("issue") or "").strip()
            section_id = str(finding.get("section_id") or "")
            suggestion = str(finding.get("suggestion") or "").strip()
            repaired = bool(finding.get("repaired"))
            fact_name = issue[:100] or "AI 一致性发现"
            description = issue + (f"（修复建议：{suggestion}）" if suggestion else "")
            conflict_id = f"AI-{project_id}-{uuid.uuid4().hex[:12]}"
            severity = rules.SEVERITY_DEFAULT
            conn.execute(
                """INSERT INTO consistency_conflicts
                   (project_id, conflict_id, fact_name, values_json,
                    sources_json, severity, description,
                    resolved, resolved_value, resolved_at, created_at)
                   VALUES (?, ?, ?, '[]', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    project_id,
                    conflict_id,
                    fact_name,
                    json.dumps([section_id], ensure_ascii=False) if section_id else "[]",
                    severity,
                    description,
                    1 if repaired else 0,
                    "AI 自动修复" if repaired else None,
                    now_str if repaired else None,
                    now_str,
                ),
            )
            log_conflicts.append(
                {
                    "conflict_id": conflict_id,
                    "fact_name": fact_name,
                    "values": [],
                    "severity": severity,
                }
            )

        # 3) 审计日志（追加一行本轮摘要）
        conn.execute(
            """INSERT INTO consistency_audit_log
               (project_id, total_sections, facts_extracted,
                conflicts_found, conflicts_json, audit_time, status,
                source_kind)
               VALUES (?, ?, 0, ?, ?, ?, ?, ?)""",
            (
                project_id,
                total_sections,
                len(findings),
                json.dumps(log_conflicts, ensure_ascii=False),
                now_str,
                rules.AUDIT_STATUS_COMPLETED
                if findings
                else rules.AUDIT_STATUS_NO_CONFLICTS,
                # T2 根治（2026-10-16）：AI 语义审计路径，来源恒为 'ai'。
                rules.CONFLICT_SOURCE_AI,
            ),
        )

    # T5 指标埋点（2026-10-16）：AI 审计落库次数 / 累计发现数 / 已就地修复数。
    # 纯内存计数，重启归零，供 GET /consistency/metrics 实时观测。
    try:
        from ..consistency_metrics import consistency_metrics
        consistency_metrics.inc("ai_audit_runs")
        consistency_metrics.inc("ai_findings", len(findings))
        consistency_metrics.inc(
            "ai_findings_repaired",
            sum(1 for f in findings if f.get("repaired")),
        )
    except Exception:  # noqa: BLE001  指标埋点不得影响业务写入
        logger.debug("一致性模块指标埋点失败（AI 审计），已忽略", exc_info=True)


async def consistency_audit_content(
    project_id: int,
    provider: Any,
    results: list[dict],
    sections_by_id: dict,
    summary: str,
    tender_context: str,
    cancel_event: asyncio.Event | None = None,
    *,
    global_facts: Any = None,
    facts_policy: str = "",
) -> list[dict]:
    """全文一致性审计：检查正文与项目事实变量的冲突。

    参考易标模式：
     1. 按字数上限分组
     2. 审计正文与事实变量的冲突
     3. AI 修复冲突小节

    ENHANCE 2026-09-15：
      - 分组口径统一为「字符」，分组预算 == 送审提示词上限（见模块常量注释）；
      - 首组串行预热 prompt cache，其余组按 `AUDIT_GROUP_CONCURRENCY` 并发。

    G1 修复（2026-10-13）：审计发现落库——此前 AI 路径只就地改 `results` 里的
    content，函数一返回发现即丢，`consistency_conflicts` / `consistency_audit_log`
    永远只有确定性审计（`ConsistencyAuditor`）的写入，前端无从看到「生成时 AI
    审计查了什么、修了什么」。现由 `_persist_ai_audit_findings` 把发现写入两张表。

    G2 修复（2026-10-13）：接入 `task_guard.reject_if_generation_running`。
    本函数的写入性质是**内容类**（只改 `sections.content`），与章节内容编辑路由
    同性质，故用 `allow_content_edit=True`：正文生成中（本函数通常就运行在生成
    任务收尾段，锁即本任务所持）放行；若锁已被目录生成持有（全表重建会静默丢弃
    本次写入）则拦截，放弃审计并原样返回。注意：不能抛 409 给调用方——SSE 流内
    的调用方（`sse_content.generate_content_impl`）只 `except Exception` 记日志，
    HTTPException 会把整个审计静默吞掉，故此处降级为「跳过审计」。

    Args:
        project_id: 项目 ID
        provider: AI 提供商实例
        results: 生成结果列表 [{id, content, status, ...}]
        sections_by_id: {id: section_dict} 字典
        summary: 项目摘要（含项目事实）
        tender_context: 招标文件全文
        cancel_event: 取消事件
        global_facts: 结构化全局事实列表（ENHANCE 2026-10-15，对齐易标
            Step04 全局事实变量审计输入）；None/空时审计退化为只依赖摘要，
            行为与历史一致。
        facts_policy: 缺失值策略（fabricate / omit / placeholder），非空时
            追加「缺值口径」约束，omit/placeholder 下不得把笼统承诺/【待填写】
            误判为冲突。
    Returns:
        list[dict]: 修复后的结果列表
    """
    if cancel_event and cancel_event.is_set():
        return results

    # G2 修复：生成中路径拦截（内容类写入，allow_content_edit=True）。
    # 被拦截（目录生成持锁）时跳过审计、原样返回——正文保持生成结果，不报错。
    from fastapi import HTTPException

    from ..task_guard import reject_if_generation_running

    try:
        reject_if_generation_running(project_id, allow_content_edit=True)
    except HTTPException as _guard_e:
        logger.warning(
            "一致性审计被任务守卫拦截（目录生成进行中，跳过审计）: %s", _guard_e.detail
        )
        return results

    completed_sections = [r for r in results if r.get("status") == "completed" and r.get("content")]
    if not completed_sections:
        return results

    # ENHANCE 2026-10-15（对齐易标 buildConsistencyAuditMessages）：审计输入
    # 显式携带结构化全局事实，作为「同一事项以谁为准」的唯一裁决基准。
    global_facts_text = _format_global_facts_for_audit(global_facts)
    completeness_text = ""
    if facts_policy:
        try:
            from ..facts_service import content_completeness_instruction

            completeness_text = content_completeness_instruction(facts_policy)
        except Exception as _coord_e:  # pragma: no cover - 防御性
            logger.debug("缺值口径指令读取失败，审计不附加该块: %s", _coord_e)
            completeness_text = ""

    audit_groups = _split_audit_groups(completed_sections)
    logger.info(
        "一致性审计：%d 个小节 → %d 组（单组上限 %d 字符，单节上限 %d 字符）",
        len(completed_sections),
        len(audit_groups),
        AUDIT_GROUP_CHAR_LIMIT,
        AUDIT_SECTION_CHAR_LIMIT,
    )

    async def _run(index: int, group: list[dict]) -> tuple[int, list[dict]]:
        """跑一组并把异常收敛成本组的 0 修复 + 空发现，避免拖垮其余组。"""
        try:
            return await _audit_one_group(
                provider,
                group,
                index,
                len(audit_groups),
                sections_by_id,
                results,
                summary,
                cancel_event,
                global_facts_text=global_facts_text,
                completeness_text=completeness_text,
            )
        except Exception as e:
            logger.warning("一致性审计第 %d 组失败: %s", index + 1, e)
            return 0, []

    # 首组串行：让服务端先建立 prompt cache —— 审计的 system prompt 与「项目事实」
    # 片段是各组共用的稳定前缀，首组跑完后再并发，后续组可以命中这份缓存。
    repaired_total, all_findings = await _run(0, audit_groups[0])

    remaining = list(enumerate(audit_groups))[1:]
    if remaining and not (cancel_event and cancel_event.is_set()):
        semaphore = asyncio.Semaphore(max(1, AUDIT_GROUP_CONCURRENCY))

        async def _guarded(index: int, group: list[dict]) -> tuple[int, list[dict]]:
            async with semaphore:
                return await _run(index, group)

        for _n, _fs in await asyncio.gather(*(_guarded(i, g) for i, g in remaining)):
            repaired_total += _n
            all_findings.extend(_fs)

    logger.info(
        "一致性审计完成：共修复 %d 个小节，发现 %d 处冲突",
        repaired_total,
        len(all_findings),
    )

    # G1 修复：审计发现落库。失败只记日志——落库异常不能影响已完成的修复结果。
    try:
        _persist_ai_audit_findings(project_id, len(completed_sections), all_findings)
    except Exception as _persist_e:
        logger.warning("一致性审计发现落库失败（不影响审计结果）: %s", _persist_e)

    return results
