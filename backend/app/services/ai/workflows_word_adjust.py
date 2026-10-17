"""易标（OpenBidKit）R9 / R10 / R11 —— 字数**双向多轮调整**的生产接线。

移植现状与本模块的定位
----------------------
2026-10-12 的移植把 R1–R15 全部落成了 ``services/word_control.py`` 的**纯函数**
（含 R9 ``granularity_for_gap``、R10 ``build_total_word_expansion_batch`` /
``build_total_word_shrink_batch``、R11 ``validate_word_adjustment_result``），
并写了 185 条单测，但**整条调整链在生产链路上没有任何调用点**：
运行时只有「压缩（shrink，1 轮，默认阈值 1.15×target）」与「兜底扩写
（默认关闭、整篇重写式）」两条，且两者都不做额度分配与结果校验。
本模块补上这段接线：把纯函数接到真实 AI 调用上，成为生成链的一个新阶段
（``word_adjust``），由 ``routers/sse/sse_content.py`` 在 condense / fallback_expand
之后调用。

与既有两条链路的分工
--------------------
======================  ==========  ==========  ==========  =========
链路                     方向        轮数        额度        结果校验
======================  ==========  ==========  ==========  =========
``condense_overlong_*``  仅缩写      1 轮        无（阈值判定）  变短 + 不超上限
``expand_content_fallback``  仅扩写    3 轮       无（整篇重写）  变长 + ≥80%
**本模块 R9 小节调整**     双向        ≤3 轮/节   ``|当前−S|``   **R11 六道**
**本模块 R10 全文调整**     双向        缩写 ≤3 轮  按比例分配    **R11 六道**
======================  ==========  ==========  ==========  =========

设计约束
--------
1. **纯函数仍是唯一公式源**：额度分配（R10）、粒度选择（R9）、六道校验（R11）
   一律调用 ``word_control.py``，本模块只负责「把额度翻译成提示词 + 调 AI +
   把结果交给校验 + 落库回写」。
2. **失败安全**：任何一次调整未通过 R11 校验即**保留原文**并记录原因，
   绝不把未校验结果写进 ``results``；单章异常不影响其它章节。
3. **图表安全**：与压缩链同构——调整前把 ```mermaid / ```chart-json 围栏替换为
   ``<<CHART_BLOCK_N>>`` 占位符（不参与字数口径、模型不会改写），调整后按序
   还原；残留占位符（被篡改）一律拒绝本次调整。
4. **字数口径**：全程使用 **R1 可读字数** ``count_readable_words``（剔除 Markdown
   标记 / 图片 / 代码块 / URL），与 R12 警告、校验页同事实源；避免出现
   「调整按口径 A、验收按口径 B」的第二处分叉。
5. **CRLF 归一**：AI 网关在 Windows 下常返回 ``\\r\\n``，本链不走
   ``clean_generated_content``，独立归一一层（与 condense 同源修复）。
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Optional

from ...utils import count_readable_words, normalize_newlines
from .. import word_control as wc

logger = logging.getLogger(__name__)


#: 还原后残留占位符检测（AI 篡改了 ``<<CHART_BLOCK_N>>`` 字面量时命中）。
_LEFTOVER_PLACEHOLDER_RE = re.compile(r"<<[^>]*[Bb][Ll][Oo][Cc][Kk][^>]*>>")

#: system 提示词不可用时的兜底文本（DB 提示词表缺失 / 加载异常时启用）。
_FALLBACK_SYSTEM_PROMPT = (
    "你是资深招投标技术标编辑。任务：按给定方向与额度对章节正文做局部字数调整，"
    "保留全部有效内容与数据，不得整篇重写、不得新增事实。只输出调整后的完整正文。"
)


def _count_words(text: Optional[str]) -> int:
    """R1 可读字数（与 R12 警告 / 校验页同口径）。"""
    if not text:
        return 0
    try:
        return int(count_readable_words(text))
    except Exception:
        return 0


def _is_cancelled(cancel_event: Any) -> bool:
    return cancel_event is not None and bool(
        getattr(cancel_event, "is_set", lambda: False)()
    )


def _load_system_prompt() -> str:
    """优先取 DB 提示词 ``CONTENT_WORD_ADJUST_SYSTEM``，异常回落模块兜底文案。"""
    try:
        from .prompts._cache import get_prompt as _get_prompt

        prompt = _get_prompt("CONTENT_WORD_ADJUST_SYSTEM") if _get_prompt else None
        if prompt:
            return str(prompt)
    except Exception:
        logger.debug("[字数调整] DB 提示词加载失败，使用兜底文案", exc_info=True)
    return _FALLBACK_SYSTEM_PROMPT


def _mask_charts(content: str) -> tuple[str, list[str]]:
    """图表围栏掩蔽（复用压缩链实现，保证两端行为一致）。"""
    try:
        from .workflows_sections import _mask_chart_fences_for_compression

        return _mask_chart_fences_for_compression(content)
    except Exception:
        logger.debug("[字数调整] 图表掩蔽不可用（按无图表处理）", exc_info=True)
        return content, []


def _restore_charts(content: str, originals: list[str]) -> str:
    try:
        from .workflows_sections import _restore_chart_fences_after_compression

        return _restore_chart_fences_after_compression(content, originals)
    except Exception:
        logger.debug("[字数调整] 图表还原不可用", exc_info=True)
        return content


def _detect_broken_charts(restored: str, originals: list[str]) -> Optional[str]:
    """图表还原完整性检查，返回失败原因（``None`` 表示完好）。

    比压缩链的 ``<<...BLOCK...>>`` 正则**更严**，因为「还原失败」在本链路等价于
    **图表丢失**（正文里出现 mermaid 源码 = 渲染失败/占号错乱）：

    1. 有占位符残留（AI 篡改或改写了 ``<<CHART_BLOCK_N>>`` 字面量）；
    2. 原始围栏块未全部回到正文（被 AI 删除或截断）。
    """
    if not originals:
        return None
    if _LEFTOVER_PLACEHOLDER_RE.search(restored) or re.search(r"<<[^>\n]{1,40}>>", restored):
        return "残留图表占位符"
    missing = [i for i, block in enumerate(originals, start=1) if block.strip() not in restored]
    if missing:
        return "原始图表代码块丢失（第 {} 块）".format("、".join(str(i) for i in missing[:3]))
    return None


def build_word_adjust_messages(
    *,
    section_title: str,
    masked_content: str,
    current_words: int,
    mode: str,
    granularity: str,
    budget: int,
    target_words: int,
    lo: int,
    hi: int,
    global_facts: list | None = None,
    facts_policy: str = "",
) -> list[Any]:
    """构造调整提示词（对应易标 ``buildWordAdjustmentMessages``）。

    提示词必须把三件事说死，否则六道校验必然打回：
    1. **方向**（expand / shrink）与**粒度**（paragraph / sentence）；
    2. **本轮额度**（``|Δ| ≤ budget``，不允许过冲——R11 第 3 道）；
    3. **目标区间**（``lo``~``hi``，越界即回退——R11 第 5 道）。

    ENHANCE 2026-10-17（对齐易标 R9/R10 字数调整注入全局事实）：
      - ``global_facts``: 扁平事实列表 [{key,label,value,...}]；非空时注入
        「本章节需要使用的全局事实变量」块，保证扩写/缩写不引入未提供的事实。
      - ``facts_policy``: 缺失值策略（fabricate/omit/placeholder）；非空时
        追加「缺值口径」约束，与正文生成/一致性修复侧保持同一来源函数。
    """
    from .providers.base import AIMessage

    direction = "扩写" if mode == wc.MODE_EXPAND else "缩写"
    scope = (
        "以段落为单位整段增补或整段删除"
        if granularity == wc.Granularity.PARAGRAPH
        else "以句子为单位微调，优先删修饰语/补实指细节"
    )
    user_msg = (
        f"章节标题：{section_title}\n\n"
        f"【调整方向】{direction}\n"
        f"【调整粒度】{scope}\n"
        f"【本轮额度】最多{'增加' if mode == wc.MODE_EXPAND else '减少'} {max(0, int(budget))} 字，"
        f"严禁超出额度（超出会被程序侧校验直接丢弃本轮结果）。\n"
        f"【目标区间】{lo}~{hi} 字（目标约 {int(target_words)} 字）；"
        f"当前正文 {int(current_words)} 字。\n\n"
        f"【当前正文】\n{masked_content}\n\n"
        f"请输出{direction}后的完整正文。"
    )

    # ENHANCE 2026-10-17：注入全局事实与缺值策略约束
    system_parts = [_load_system_prompt()]
    if global_facts:
        from .workflows_sections import _format_facts_list
        facts_text = _format_facts_list(global_facts)
        if facts_text:
            system_parts.append(
                f"\n\n【本章节全局事实变量（调整时不得引入上述事实之外的具体人名/日期/品牌/型号/承诺）】\n{facts_text}"
            )
    if facts_policy:
        try:
            from ...facts_service import content_completeness_instruction
            instr = content_completeness_instruction(facts_policy)
            if instr:
                system_parts.append(f"\n\n【缺值策略约束】\n{instr}")
        except Exception:
            pass
    return [
        AIMessage(role="system", content="\n".join(system_parts)),
        AIMessage(role="user", content=user_msg),
    ]


async def adjust_section_words(
    *,
    provider: Any,
    content: str,
    section_title: str,
    mode: str,
    budget: int,
    target_words: int,
    lo: int,
    hi: int,
    granularity: Optional[str] = None,
    timeout_seconds: int = 120,
    cancel_event: Any = None,
    section_bounds: Optional[tuple[int, int]] = None,
    enforce_section_bounds: bool = False,
    total_words: Optional[int] = None,
    maximum_words: Optional[int] = None,
    minimum_words: Optional[int] = None,
    enforce_total_bounds: bool = False,
    global_facts: list | None = None,
    facts_policy: str = "",
) -> tuple[Optional[str], dict]:
    """对**单节**做一次字数调整并返回 ``(新正文 | None, meta)``。

    完整执行链（R9 粒度 → 掩蔽 → 调 AI → 归一 → 还原 → R11 六道校验）：

    1. 字数用 R1 可读口径；``budget`` 为本轮允许的最大变化量（不允许过冲）；
    2. 粒度默认按 R9 ``granularity_for_gap`` 选取（偏差 >20% 段落级，否则句子级）；
    3. 调 AI 前把图表围栏掩蔽成占位符，避免模型截断 mermaid / chart-json；
    4. AI 返回先归一 CRLF、strip，再还原图表；残留占位符 → 拒绝本轮；
    5. 结果交给 ``validate_word_adjustment_result``（R11 六道硬校验），
       **任一失败即返回 None（保留原文）并把原因写进 ``meta["reason"]``**。

    Args:
        mode: ``expand`` / ``shrink``。
        budget: 本轮额度（正整数，绝对值语义）。
        target_words / lo / hi: 目标与区间（提示词与 R11 第 4/5 道共用）。
        section_bounds: 强控区间；``enforce_section_bounds=True`` 时 R11 第 5 道生效。
        total_words / maximum_words / minimum_words: 全文边界（R11 第 6 道）。
    """
    from .providers.base import AIMessage as _AIMessageProbe  # 提前校验依赖可导入

    del _AIMessageProbe
    gran = granularity or wc.granularity_for_gap(_count_words(content), int(target_words))
    before = _count_words(content)
    meta = {
        "mode": mode,
        "granularity": gran,
        "budget": max(0, int(budget)),
        "before_words": before,
        "after_words": before,
        "delta": 0,
        "accepted": False,
        "reason": "",
        "title": section_title,
        "target_words": int(target_words),
        "lo": int(lo),
        "hi": int(hi),
    }
    if not content or before <= 0:
        meta["reason"] = "正文为空，跳过调整"
        return None, meta
    if meta["budget"] <= 0:
        meta["reason"] = "额度为 0，跳过调整"
        return None, meta
    if _is_cancelled(cancel_event):
        meta["reason"] = "用户取消"
        return None, meta

    masked, chart_originals = _mask_charts(content)
    try:
        messages = build_word_adjust_messages(
            section_title=section_title,
            masked_content=masked,
            current_words=before,
            mode=mode,
            granularity=gran,
            budget=meta["budget"],
            target_words=int(target_words),
            lo=int(lo),
            hi=int(hi),
            global_facts=global_facts,
            facts_policy=facts_policy,
        )
        if provider is None or not hasattr(provider, "chat"):
            raise RuntimeError("provider 为空或无 chat 接口")
        try:
            from .workflows_base import with_timeout
        except Exception:  # pragma: no cover - 理论上不会发生
            with_timeout = None
        coro = provider.chat(
            messages, temperature=0.4, cancel_event=cancel_event
        )
        raw = (
            await with_timeout(coro, timeout=timeout_seconds)
            if with_timeout is not None
            else await coro
        )
        adjusted_masked = normalize_newlines(str(raw or "")).strip()
        if not adjusted_masked:
            meta["reason"] = "AI 返回空内容"
            return None, meta
        adjusted = _restore_charts(adjusted_masked, chart_originals)
        leftover = _detect_broken_charts(adjusted, chart_originals)
        if leftover:
            meta["reason"] = f"图表围栏还原失败（{leftover}），保留原文"
            logger.warning("[字数调整] %s：%s", section_title, meta["reason"])
            return None, meta
        after = _count_words(adjusted)

        ok, reason = wc.validate_word_adjustment_result(
            current_words=before,
            new_words=after,
            mode=mode,
            target_words=int(target_words),
            maximum_change_words=meta["budget"],
            section_bounds=section_bounds or (int(lo), int(hi)),
            enforce_section_bounds=bool(enforce_section_bounds),
            total_words=total_words,
            maximum_words=maximum_words,
            minimum_words=minimum_words,
            enforce_total_bounds=bool(enforce_total_bounds),
        )
        meta["after_words"] = after
        meta["delta"] = after - before
        meta["reason"] = reason
        if not ok:
            logger.info(
                "[字数调整] %s 第 1 轮被 R11 校验拒绝：%s（%d → %d 字）",
                section_title, reason, before, after,
            )
            return None, meta
        meta["accepted"] = True
        return adjusted, meta
    except Exception as exc:
        meta["reason"] = f"调整异常: {exc}"
        logger.warning("[字数调整] %s 调整失败：%s", section_title, exc)
        return None, meta


def _view_of(r: dict, sections_by_id: dict) -> dict:
    """构造 R10 批次函数所需的视图（id / title / word_count，R1 口径）。"""
    sid = r.get("id")
    sec = (sections_by_id or {}).get(sid) or {}
    return {
        "id": sid,
        "title": r.get("title") or sec.get("title") or "",
        "word_count": _count_words(r.get("content")),
    }


async def run_section_word_adjustments(
    provider: Any,
    results: list,
    sections_by_id: dict,
    *,
    word_control: Optional[dict] = None,
    max_rounds: Optional[int] = None,
    concurrency: int = 4,
    cancel_event: Any = None,
    global_facts: list | None = None,
    facts_policy: str = "",
) -> dict:
    """R9 —— 强控小节的**双向**拉回（每节 ≤ ``MAX_WORD_ADJUSTMENT_ROUNDS`` 轮）。

    生效范围：**仅强控（R5）生效的章节**。与易标 ``section-word-adjusting``
    阶段一致——非强控项目没有「硬区间」，靠 condense / 兜底扩写即可，强控项目
    才需要「区间是硬约束」的多轮拉回。

    每轮流程：重测字数 → 判越界 → 选方向 → 选粒度（R9）→ 额度 ``|当前−S|``
    → 调 ``adjust_section_words``（内部走 R11 六道）→ 通过则回写 ``content``。
    收敛或达轮数上限退出；仍越界的进入 ``unresolved_ids``（交人工裁决 / R12 警告）。

    Returns:
        ``{checked, adjusted_count, fail_count, unresolved_ids, rounds, expanded, shrunk,
        adjusted_ids}``（``results`` 就地更新）。
    """
    rounds_cap = max(1, int(max_rounds or wc.MAX_WORD_ADJUSTMENT_ROUNDS))
    summary = {
        "stage": "section",
        "checked": 0,
        "adjusted_count": 0,
        "fail_count": 0,
        "unresolved_ids": [],
        "rounds": 0,
        "expanded": 0,
        "shrunk": 0,
        "adjusted_ids": [],
    }
    if not results or not word_control:
        return summary

    from ..word_control_runtime import resolve_override as _resolve_ov

    candidates: list[tuple[dict, dict]] = []
    for r in results:
        if r.get("status") != "completed" or not r.get("content"):
            continue
        sec = (sections_by_id or {}).get(r.get("id"))
        if not sec:
            continue
        try:
            ov = _resolve_ov(word_control, sec)
        except Exception:
            ov = None
        if not ov:
            continue
        words = _count_words(r["content"])
        lo, hi = int(ov.get("lo") or 0), int(ov.get("hi") or 0)
        if lo <= 0 or hi <= 0:
            continue
        summary["checked"] += 1
        if words < lo or words > hi:
            candidates.append((r, ov))

    if not candidates:
        logger.info("[字数调整-R9] 强控小节全部在区间内，跳过")
        return summary

    logger.info(
        "[字数调整-R9] %d/%d 个强控小节越界，进入多轮拉回（并发=%d，每节≤%d 轮）",
        len(candidates), summary["checked"], max(1, int(concurrency or 1)), rounds_cap,
    )
    semaphore = asyncio.Semaphore(max(1, int(concurrency or 1)))
    unresolved: list[Any] = []

    async def _adjust_one(r: dict, ov: dict) -> None:
        async with semaphore:
            sid = r.get("id")
            title = r.get("title") or (sections_by_id or {}).get(sid, {}).get("title", "")
            lo, hi = int(ov.get("lo") or 0), int(ov.get("hi") or 0)
            target = int(ov.get("section_words") or 0) or (lo + hi) // 2
            for _round in range(rounds_cap):
                if _is_cancelled(cancel_event):
                    unresolved.append(sid)
                    return
                words = _count_words(r.get("content"))
                if lo <= words <= hi:
                    return
                mode = wc.MODE_EXPAND if words < lo else wc.MODE_SHRINK
                budget = abs(words - target) or 1
                new_content, meta = await adjust_section_words(
                    provider=provider,
                    content=r.get("content") or "",
                    section_title=title,
                    mode=mode,
                    budget=budget,
                    target_words=target,
                    lo=lo,
                    hi=hi,
                    cancel_event=cancel_event,
                    global_facts=global_facts,
                    facts_policy=facts_policy,
                    # R11 第 5 道（强控边界）**不在单轮强制**：一轮额度未必能把
                    # 500 字直接压进 [240,360]，若强制入区间则合法的部分进展
                    # （500→380）会被整轮丢弃，模型反复重写同一篇、白烧轮次。
                    # 因此本轮只强制 R11 第 3/4 道（额度 + 差距严格缩小），
                    # 「是否最终落回区间」交给本循环重测判定 —— 与易标
                    # enforceSectionBounds 可选开关的语义一致。
                    enforce_section_bounds=False,
                )
                summary["rounds"] += 1
                if new_content:
                    r["content"] = new_content
                    r["word_count_adjusted"] = True
                    summary["adjusted_count"] += 1
                    summary["adjusted_ids"].append(sid)
                    if mode == wc.MODE_EXPAND:
                        summary["expanded"] += 1
                    else:
                        summary["shrunk"] += 1
                else:
                    summary["fail_count"] += 1
                    logger.info(
                        "[字数调整-R9] %s 第 %d 轮未通过校验：%s",
                        title, _round + 1, meta.get("reason", ""),
                    )
                    break
            if not (lo <= _count_words(r.get("content")) <= hi):
                unresolved.append(sid)

    _gather_results = await asyncio.gather(
        *(_adjust_one(r, ov) for r, ov in candidates), return_exceptions=True
    )
    # BUG-FIX 2026-10-17（gather 吞异常防护）：return_exceptions=True 会把协程内
    # 的异常作为返回值吞掉（之前的 TypeError: adjust_section_words() got an
    # unexpected keyword argument 'global_facts' 就是被这里静默吞掉的，导致用户
    # 看到「调整 0 节、失败 0」的假成功）。现在显式检查异常实例并记日志，使
    # 生产排障能从日志中发现被吞的异常。
    for _i, _res in enumerate(_gather_results):
        if isinstance(_res, Exception):
            _cand = candidates[_i] if _i < len(candidates) else None
            _sid = _cand[0].get("id") if _cand else "?"
            logger.error(
                "[字数调整-R9] 小节 %s 协程异常（被 gather 吞掉，现已显式记录）: %s",
                _sid, _res, exc_info=_res,
            )
            summary["fail_count"] += 1
    # adjusted_ids 去重（同一节多轮命中只记一次）
    seen: set = set()
    deduped = []
    for sid in summary["adjusted_ids"]:
        if sid not in seen:
            seen.add(sid)
            deduped.append(sid)
    summary["adjusted_ids"] = deduped
    summary["unresolved_ids"] = sorted({s for s in unresolved if s is not None})
    logger.info(
        "[字数调整-R9] 完成：调整 %d 节（扩写 %d / 缩写 %d），失败 %d，未达标 %d",
        summary["adjusted_count"], summary["expanded"], summary["shrunk"],
        summary["fail_count"], len(summary["unresolved_ids"]),
    )
    return summary


async def run_total_word_adjustments(
    provider: Any,
    results: list,
    sections_by_id: dict,
    *,
    options: Optional[wc.WordControlOptions] = None,
    word_control: Optional[dict] = None,
    max_rounds: Optional[int] = None,
    concurrency: int = 4,
    cancel_event: Any = None,
    global_facts: list | None = None,
    facts_policy: str = "",
) -> dict:
    """R10 —— **全文**扩写 / 缩写（额度按比例分配，六道校验逐节把关）。

    方向判定（对应易标 ``getTotalWordDirection``）：

    - 全文 ``< minimum_words`` → ``expand``（目标下限）；
    - 全文 ``> maximum_words`` → ``shrink``（目标上限）；
    - 区间内 → 不动作（``direction="none"``）。

    轮次策略：**缩写最多 3 轮**；**扩写不限轮**，但连续
    ``MAX_EXPANSION_NO_PROGRESS_ROUNDS``(3) 轮有效字数无增长即停（防烧 token）。
    每轮批次由 ``build_total_word_expansion_batch`` / ``build_total_word_shrink_batch``
    计算（每轮 ≤10 节、缩写单节 ≤25%、可缩空间过小的节跳过）。

    Returns:
        ``{direction, rounds, adjusted_count, fail_count, total_before, total_after,
        adjusted_ids}``。
    """
    rounds_cap = max(1, int(max_rounds or wc.MAX_WORD_ADJUSTMENT_ROUNDS))
    summary = {
        "stage": "total",
        "direction": "none",
        "rounds": 0,
        "adjusted_count": 0,
        "fail_count": 0,
        "total_before": 0,
        "total_after": 0,
        "adjusted_ids": [],
    }
    opts = options or wc.WordControlOptions()
    completed = [
        r for r in (results or [])
        if r.get("status") == "completed" and (r.get("content") or "").strip()
    ]
    if not completed or not opts:
        return summary

    total = sum(_count_words(r.get("content")) for r in completed)
    summary["total_before"] = total
    summary["total_after"] = total

    minimum_words = int(opts.minimum_words or 0)
    maximum_words = int(opts.maximum_words or 0)
    if minimum_words > 0 and total < minimum_words:
        mode = wc.MODE_EXPAND
    elif maximum_words > 0 and total > maximum_words:
        mode = wc.MODE_SHRINK
    else:
        logger.info(
            "[字数调整-R10] 全文 %d 字已在区间内（min=%d / max=%d），跳过",
            total, minimum_words, maximum_words,
        )
        return summary
    summary["direction"] = mode

    semaphore = asyncio.Semaphore(max(1, int(concurrency or 1)))
    no_progress = 0
    round_idx = 0
    seen_ids: set = set()

    while True:
        if _is_cancelled(cancel_event):
            break
        round_idx += 1
        if mode == wc.MODE_SHRINK and round_idx > rounds_cap:
            break
        views = [_view_of(r, sections_by_id) for r in completed]
        if mode == wc.MODE_EXPAND:
            batch = wc.build_total_word_expansion_batch(
                views,
                total_words=total,
                minimum_words=minimum_words,
                options=opts,
                guidance_words=int(opts.section_words or 0) or None,
            )
        else:
            batch = wc.build_total_word_shrink_batch(
                views,
                total_words=total,
                maximum_words=maximum_words,
                options=opts,
            )
        if not batch:
            logger.info(
                "[字数调整-R10] 第 %d 轮无可调整额度，退出（总量 %d 字）", round_idx, total
            )
            break

        logger.info(
            "[字数调整-R10] 第 %d 轮 %s：%d 节待调，总缺口 %d 字（当前总量 %d）",
            round_idx, "扩写" if mode == wc.MODE_EXPAND else "缩写",
            len(batch), sum(it["budget"] for it in batch), total,
        )
        by_id = {r.get("id"): r for r in completed}
        round_before_total = total

        async def _adjust_batch_item(item: dict) -> None:
            async with semaphore:
                if _is_cancelled(cancel_event):
                    return
                r = by_id.get(item.get("id"))
                if not r:
                    return
                sid = item.get("id")
                sec = (sections_by_id or {}).get(sid) or {}
                # 全文阶段的「目标/区间」语义与小节强控不同：此处目标就是
                # **本轮额度终点**（current ± budget），区间是本轮可达范围。
                # 若沿用小节目标（S），R11 第 4 道（差距必须缩小）会把
                # 「总量不足但单节本已达标」的正确扩写全部判成失败。
                cur = int(item.get("current_words") or _count_words(r.get("content")))
                budget = int(item.get("budget") or 0)
                target = cur + budget if mode == wc.MODE_EXPAND else cur - budget
                lo, hi = min(cur, target), max(cur, target)
                new_content, meta = await adjust_section_words(
                    provider=provider,
                    content=r.get("content") or "",
                    section_title=item.get("title") or sec.get("title", ""),
                    mode=mode,
                    budget=budget,
                    target_words=target,
                    lo=lo,
                    hi=hi,
                    cancel_event=cancel_event,
                    global_facts=global_facts,
                    facts_policy=facts_policy,
                    # R11 第 6 道：全文边界（扩写不超上限 / 缩写不低于下限）。
                    total_words=total,
                    maximum_words=maximum_words,
                    minimum_words=minimum_words,
                    enforce_total_bounds=True,
                )
                summary["rounds"] += 1
                if new_content:
                    r["content"] = new_content
                    r["word_count_adjusted"] = True
                    summary["adjusted_count"] += 1
                    seen_ids.add(sid)
                else:
                    summary["fail_count"] += 1
                    logger.info(
                        "[字数调整-R10] %s 未通过校验：%s",
                        item.get("title") or sid, meta.get("reason", ""),
                    )

        _batch_results = await asyncio.gather(
            *(_adjust_batch_item(item) for item in batch), return_exceptions=True
        )
        # BUG-FIX 2026-10-17（gather 吞异常防护）：同 R9，显式检查异常实例并记日志。
        for _i, _res in enumerate(_batch_results):
            if isinstance(_res, Exception):
                _bid = batch[_i].get("id") if _i < len(batch) else "?"
                logger.error(
                    "[字数调整-R10] 小节 %s 协程异常（被 gather 吞掉，现已显式记录）: %s",
                    _bid, _res, exc_info=_res,
                )
                summary["fail_count"] += 1

        total = sum(_count_words(r.get("content")) for r in completed)
        if mode == wc.MODE_EXPAND:
            # 扩写不限轮，但连续 N 轮无有效增字即停。
            no_progress = 0 if total > round_before_total else no_progress + 1
            if no_progress >= wc.MAX_EXPANSION_NO_PROGRESS_ROUNDS:
                logger.info(
                    "[字数调整-R10] 扩写连续 %d 轮无进展，退出（总量 %d 字）",
                    no_progress, total,
                )
                break
        if mode == wc.MODE_SHRINK and total >= (maximum_words or total):
            break

    summary["total_after"] = total
    summary["adjusted_ids"] = sorted(i for i in seen_ids if i is not None)
    logger.info(
        "[字数调整-R10] 完成：方向=%s，轮数=%d，调整 %d 节，失败 %d，总量 %d → %d 字",
        summary["direction"], summary["rounds"], summary["adjusted_count"],
        summary["fail_count"], summary["total_before"], summary["total_after"],
    )
    return summary


async def run_word_adjustment_pipeline(
    provider: Any,
    results: list,
    sections_by_id: dict,
    *,
    options: Optional[wc.WordControlOptions] = None,
    word_control: Optional[dict] = None,
    max_rounds: Optional[int] = None,
    concurrency: int = 4,
    cancel_event: Any = None,
    enable_total: bool = True,
    on_summary: Any = None,
    global_facts: list | None = None,
    facts_policy: str = "",
) -> dict:
    """编排：**R9 小节强控拉回 → R10 全文扩/缩写**。

    顺序与易标正文阶段一致（``section-word-adjusting`` 先于 ``total-word-adjusting``）：
    先把每节拉进硬区间，再看全文总量——否则总量调整会把已达标的小节再次推出去。

    ``on_summary`` 为可选回调 ``(stage, summary_dict)``，供 SSE 实时推送阶段进度。
    汇总返回 ``{enabled, section, total, adjusted_ids}``；``results`` 就地更新。
    """
    out = {
        "enabled": bool(options) or bool(word_control),
        "section": {},
        "total": {},
        "adjusted_ids": [],
    }
    if not out["enabled"] or not results:
        return out

    section_summary = await run_section_word_adjustments(
        provider,
        results,
        sections_by_id,
        word_control=word_control,
        max_rounds=max_rounds,
        concurrency=concurrency,
        cancel_event=cancel_event,
        global_facts=global_facts,
        facts_policy=facts_policy,
    )
    out["section"] = section_summary
    if on_summary:
        try:
            res = on_summary("section", section_summary)
            if asyncio.iscoroutine(res):
                await res
        except Exception:
            logger.debug("[字数调整] on_summary 回调失败（忽略）", exc_info=True)

    total_summary: dict = {}
    if enable_total and not _is_cancelled(cancel_event):
        total_summary = await run_total_word_adjustments(
            provider,
            results,
            sections_by_id,
            options=options,
            word_control=word_control,
            max_rounds=max_rounds,
            concurrency=concurrency,
            cancel_event=cancel_event,
            global_facts=global_facts,
            facts_policy=facts_policy,
        )
        out["total"] = total_summary
        if on_summary:
            try:
                res = on_summary("total", total_summary)
                if asyncio.iscoroutine(res):
                    await res
            except Exception:
                logger.debug("[字数调整] on_summary 回调失败（忽略）", exc_info=True)

    ids = list(section_summary.get("adjusted_ids") or []) + list(
        total_summary.get("adjusted_ids") or []
    )
    out["adjusted_ids"] = sorted({i for i in ids if i is not None})
    return out


__all__ = [
    "adjust_section_words",
    "build_word_adjust_messages",
    "run_section_word_adjustments",
    "run_total_word_adjustments",
    "run_word_adjustment_pipeline",
]
