"""workflows 子模块：workflows_expand（由 scripts/split_workflows.py 自动生成，勿手改）"""

import asyncio
import json
import logging
from typing import Any

from ...utils import count_words as _count_words
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
    """单章节的扩写阈值（ENHANCE 2026-09-15，统一两处口径）。

    旧实现有两个互相矛盾且有问题的口径：
    - 生成流程内用 `max(全章节 target)×80%` 做全局阈值 → L3 小节（分档目标 300 字）
      也会被强拉到 ~2800 字，浪费大量 token 且违背字数分档标准；
    - 独立「兜底扩写」端点用 writing_length 平铺阈值（standard=2000）→ 同样无视章节自身 target。
    统一为：有 target 用 `target×80%`；无 target 按层级分档（L1=500/L2=level2_default/
    L3=300/L4=400）；两者都不超过平铺阈值，且保底 200 字。

    ENHANCE 2026-10-07：新增 `writing_length` —— 二级目录及以下章节在正文生成阶段
    已改由写作长度模式（简短/标准/详细/超长）实际控制字数，故此处对 L2+ 同样优先
    取该模式的层级目标（中间值），避免「生成按模式、扩写又拉回旧目标」的自相矛盾。
    """
    level = int((section or {}).get("level") or 3)
    if writing_length and level >= 2:
        # 与正文生成一致：L2+ 由写作长度模式控制（中间值 ×80% 作为扩写达标线）
        from .writing_length_config import level_target
        _t = level_target(writing_length, level)
        if _t:
            return max(200, min(int(flat_minimum), int(_t * 0.8)))
    target = int((section or {}).get("target_words") or 0)
    if target <= 0:
        target = {1: 500, 2: level2_default, 3: 300, 4: 400}.get(level, 300)
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
        words = _count_words(content)
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
                words = _count_words(content)
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
            if _count_words(content) >= threshold:
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


def _split_audit_groups(completed_sections: list[dict]) -> list[list[dict]]:
    """把待审计小节按 `AUDIT_GROUP_CHAR_LIMIT` 顺序装箱成组（字符口径）。

    装箱用的是「本节实际会送审的字符数」（即截断后的长度），因此
    **不会出现"分组预算很大、实际送审被截掉大半"的口径错位**。
    """
    groups: list[list[dict]] = []
    current: list[dict] = []
    current_chars = 0
    for section in completed_sections:
        content = section.get("content") or ""
        chars = min(len(content), AUDIT_SECTION_CHAR_LIMIT)
        if current and current_chars + chars > AUDIT_GROUP_CHAR_LIMIT:
            groups.append(current)
            current = []
            current_chars = 0
        current.append(section)
        current_chars += chars
    if current:
        groups.append(current)
    return groups


async def _audit_one_group(
    provider: Any,
    group: list[dict],
    group_idx: int,
    total_groups: int,
    sections_by_id: dict,
    results: list[dict],
    summary: str,
    cancel_event: asyncio.Event | None,
) -> int:
    """审计并修复一组正文，返回成功修复的小节数。

    拆成独立协程是为了让多组可以并发（首组仍串行预热 prompt cache）。
    异常由调用方 `_run` 兜住，单组失败不影响其余组。
    """
    if cancel_event and cancel_event.is_set():
        return 0

    parts: list[str] = []
    for r in group:
        section = sections_by_id.get(r["id"], {})
        title = section.get("title", "")
        content = r.get("content", "") or ""
        if len(content) > AUDIT_SECTION_CHAR_LIMIT:
            # 单节超长只能抽样送审 —— 但必须留下可见的痕迹，
            # 否则"审计覆盖率不足"会变成无从察觉的静默降级。
            logger.warning(
                "一致性审计：章节 %s（%s）正文 %d 字符超出单节送审上限 %d，本次只审计前 %d 字符",
                r.get("id"),
                title or "-",
                len(content),
                AUDIT_SECTION_CHAR_LIMIT,
                AUDIT_SECTION_CHAR_LIMIT,
            )
            content = content[:AUDIT_SECTION_CHAR_LIMIT]
        parts.append(f"=== {title} ===\n{content}")
    group_content = "\n\n".join(parts)

    audit_prompt = (
        f"你是一个标书一致性审计专家。请检查以下正文内容是否与项目事实变量存在冲突或不一致。\n\n"
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
        return 0

    if not (parsed.get("has_conflicts") and parsed.get("conflicts")):
        return 0

    conflicts = parsed["conflicts"]
    if not isinstance(conflicts, list):
        logger.warning("一致性审计冲突数据格式异常，跳过")
        return 0

    repaired_count = 0
    for conflict in conflicts:
        if not isinstance(conflict, dict):
            continue
        section_id = conflict.get("section_id")
        suggestion = conflict.get("suggestion", "")
        if not (section_id and suggestion):
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

    return repaired_count


async def consistency_audit_content(
    provider: Any,
    results: list[dict],
    sections_by_id: dict,
    summary: str,
    tender_context: str,
    cancel_event: asyncio.Event | None = None,
) -> list[dict]:
    """全文一致性审计：检查正文与项目事实变量的冲突。

    参考易标模式：
    1. 按字数上限分组
    2. 审计正文与事实变量的冲突
    3. AI 修复冲突小节

    ENHANCE 2026-09-15：
      - 分组口径统一为「字符」，分组预算 == 送审提示词上限（见模块常量注释）；
      - 首组串行预热 prompt cache，其余组按 `AUDIT_GROUP_CONCURRENCY` 并发。

    Args:
        provider: AI 提供商实例
        results: 生成结果列表 [{id, content, status, ...}]
        sections_by_id: {id: section_dict} 字典
        summary: 项目摘要（含项目事实）
        tender_context: 招标文件全文
        cancel_event: 取消事件
    Returns:
        list[dict]: 修复后的结果列表
    """
    if cancel_event and cancel_event.is_set():
        return results

    completed_sections = [r for r in results if r.get("status") == "completed" and r.get("content")]
    if not completed_sections:
        return results

    audit_groups = _split_audit_groups(completed_sections)
    logger.info(
        "一致性审计：%d 个小节 → %d 组（单组上限 %d 字符，单节上限 %d 字符）",
        len(completed_sections),
        len(audit_groups),
        AUDIT_GROUP_CHAR_LIMIT,
        AUDIT_SECTION_CHAR_LIMIT,
    )

    async def _run(index: int, group: list[dict]) -> int:
        """跑一组并把异常收敛成本组的 0 修复，避免拖垮其余组。"""
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
            )
        except Exception as e:
            logger.warning("一致性审计第 %d 组失败: %s", index + 1, e)
            return 0

    # 首组串行：让服务端先建立 prompt cache —— 审计的 system prompt 与「项目事实」
    # 片段是各组共用的稳定前缀，首组跑完后再并发，后续组可以命中这份缓存。
    repaired_total = await _run(0, audit_groups[0])

    remaining = list(enumerate(audit_groups))[1:]
    if remaining and not (cancel_event and cancel_event.is_set()):
        semaphore = asyncio.Semaphore(max(1, AUDIT_GROUP_CONCURRENCY))

        async def _guarded(index: int, group: list[dict]) -> int:
            async with semaphore:
                return await _run(index, group)

        repaired_total += sum(await asyncio.gather(*(_guarded(i, g) for i, g in remaining)))

    logger.info("一致性审计完成：共修复 %d 个小节", repaired_total)
    return results
