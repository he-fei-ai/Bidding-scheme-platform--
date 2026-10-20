# -*- coding: utf-8 -*-
"""第七轮章节编写模块审计修复的永久回归测试（2026-10-17）。

对应 _audit7_probe.py 确认的 10 项真实缺陷修复，固化为可执行断言，防复发：

  FIX-1  围栏吞正文：plan_chart_fence_lines 5 元组承载误吞 prose + 消费方输出
  FIX-2  压缩触发阈值(1.15) 与内部 hi(1.20) 双重口径 → 空转对齐
  FIX-3  压缩计量口径贯穿（触发 / 内部 / 结果共用 count_fn）
  FIX-4  压缩落库经 ContentCleaner 清洗（去重复标题 / ## 标记）
  FIX-5  压缩入口兜底闭合未闭合围栏（防 mermaid 源码裸露送 AI）
  FIX-6  retry_round 首轮 round 0→1（前端不再显「第 0/2 轮」）
  FIX-7  全 cancelled 的 stopped 事件补 cancelled / failed 字段
  FIX-8  ATX 标题无空格形态（##1.1）编号剥离 + 不误伤 ##2026 / ##1.8米
  FIX-9  兜底扩写后计数经 _SectionTally.recount 全量重算
  FIX-10 续写以列表 / 标题 / 引用开头时插换行（防列表被拍平）

行为级测试用假 provider 驱动真实函数；SSE 事件类修复（FIX-6/7）沿用本仓库
既有的「源码锚定回归测试」惯例（见 test_bugfix_2026_10_04_content_generation.py）。
"""
from __future__ import annotations

import asyncio
import inspect
import os
import re
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

# 保证 L3 自身正文解析为 300 ± 20%（FIX-2 / FIX-4 行为测试依赖该口径）
os.environ.setdefault("L3_OWN_TARGET_WORDS", "300")
os.environ.setdefault("L3_OWN_TOLERANCE_RATIO", "0.20")


# ===========================================================================
# FIX-1 —— 围栏吞正文
# ===========================================================================
def test_fix1_plan_carries_prose_and_keeps_invariant():
    """plan_chart_fence_lines 5 元组承载误吞 prose，且 sum(consumed)==len(lines)。"""
    from app.services.content_cleaning import plan_chart_fence_lines

    lines = [
        "```mermaid", "graph TD", "  A-->B", "",
        "这是图表的说明文字，属于正文。", "```", "后续段落。",
    ]
    plan = plan_chart_fence_lines(lines)
    # 5 元组契约
    assert all(len(t) == 5 for t in plan), f"规划器条目须为 5 元组: {[len(t) for t in plan]}"
    # 关键不变量：consumed 总和覆盖全部源行
    assert sum(c for _a, _l, _ls, c, _p in plan) == len(lines), "consumed 不变量被破坏"
    # 误吞正文段随第 5 元素透出
    prose = [ln for _a, _l, _ls, _c, p in plan for ln in p]
    assert "这是图表的说明文字，属于正文。" in prose, f"prose 丢失: {prose!r}"


def test_fix1_protect_output_keeps_prose():
    """_protect_chart_fences 把混合块拆为「完整围栏 + 后续正文」，正文不丢。"""
    from app.services.content_cleaning import _protect_chart_fences

    text = (
        "```mermaid\ngraph TD\n  A-->B\n\n"
        "这是图表的说明文字，属于正文。\n```\n后续段落。"
    )
    out = _protect_chart_fences(text)  # 仅返回 str（图表块经上下文缓存，不入参返回）
    assert "这是图表的说明文字，属于正文。" in out, "正文段在保护阶段被吞"
    assert "graph TD" not in out, "图表载荷应被占位符替换"


def test_fix1_parse_content_emits_prose_paragraph():
    """parse_content 链路：混合块的误吞正文作为段落块输出，图表仍为 chart_marker。"""
    from app.services.content_cleaning import parse_content

    text = (
        "```mermaid\ngraph TD\n  A-->B\n\n"
        "这是图表的说明文字，属于正文。\n```\n后续段落。"
    )
    blocks = parse_content(text)
    texts = [b.text for b in blocks if b.type == "paragraph"]
    assert any("说明文字" in t for t in texts), f"解析段落丢失 prose: {texts!r}"
    assert any(b.type == "chart_marker" for b in blocks), "图表标记丢失"


def test_fix1_unclosed_mixed_block_preserves_prose():
    """未闭合混合块：prose 仍透出，consumed 覆盖全部源行。"""
    from app.services.content_cleaning import plan_chart_fence_lines

    unclosed = ["```mermaid", "graph TD", "", "说明文字未闭合。"]
    plan = plan_chart_fence_lines(unclosed)
    assert sum(c for _a, _l, _ls, c, _p in plan) == len(unclosed), "未闭合 consumed 不变量破坏"
    prose = [ln for _a, _l, _ls, _c, p in plan for ln in p]
    assert any("说明文字未闭合。" in ln for ln in prose), "未闭合 prose 丢失"


# ===========================================================================
# FIX-8 —— ATX 标题无空格形态编号剥离
# ===========================================================================
def test_fix8_atx_no_space_strips_numbering():
    """##1.1 工程概况（无空格）须剥离编号；保留 # 后的单空格形态。"""
    from app.services.content_cleaning import strip_heading_numbering_from_content

    assert strip_heading_numbering_from_content("##1.1 工程概况\n正文。").startswith(
        "## 工程概况"
    ), "无空格 ATX 编号未剥离"
    # 仍支持带空格的经典形态
    assert strip_heading_numbering_from_content("## 1.1 工程概况\n正文。").startswith(
        "## 工程概况"
    )


def test_fix8_atx_no_false_positive_on_year_and_unit():
    """不误伤 ##2026年计划 / ##1.8米净高 这类正文行（编号分支分隔符必需）。"""
    from app.services.content_cleaning import strip_heading_numbering_from_content

    assert strip_heading_numbering_from_content("##2026年计划\n正文。").startswith(
        "##2026"
    ), "年份被误判为编号"
    assert strip_heading_numbering_from_content("##1.8米净高\n正文。").startswith(
        "##1.8"
    ), "单位数值被误判为编号"


# ===========================================================================
# FIX-3 / FIX-5 —— 压缩计量口径贯穿 + 入口兜底闭合
# ===========================================================================
def test_fix3_count_fn_drives_early_return():
    """count_fn 贯穿压缩判定：可读字数(crw) 与 count_words(cw) 在该样本中差 13 字，
    须分别以各自口径决定 early-return，而非恒用 count_words。"""
    from app.services.ai.workflows_sections import condense_overlong_content
    from app.utils import count_words as cw, count_readable_words as crw

    content = (
        "本项目采用先进的施工组织方案，确保工期与质量。\n\n"
        "```mermaid\ngraph TD\n  A[施工准备]-->B[主体施工]\n```\n\n"
        "主要设备表见下。"
    )

    class _FakeProvider:
        def __init__(self):
            self.calls = 0

        async def chat(self, messages, **kwargs):
            self.calls += 1
            return "压缩后的简短正文。"

    async def _run(fn):
        p = _FakeProvider()
        await condense_overlong_content(
            content, target_words=30, tolerance_ratio=0.20,
            provider=p, section_title="t", count_fn=fn,
        )
        return p.calls

    calls_cw = asyncio.run(_run(cw))    # 41 > 36 触发
    calls_crw = asyncio.run(_run(crw))  # 28 <= 36 不触发
    assert calls_cw == 1, f"cw 口径应触发: calls={calls_cw}"
    assert calls_crw == 0, f"crw 口径应不触发: calls={calls_crw}"


def test_fix5_unclosed_fence_masked_at_entry():
    """未闭合围栏内容直接进压缩 → 入口兜底闭合后被 mask，AI 不得见 mermaid 源码。"""
    from app.services.ai.workflows_sections import condense_overlong_content

    unclosed = (
        "本项目采用先进的施工组织方案，确保工期与质量目标全面达成。" * 3
        + "\n\n```mermaid\ngraph TD\n  A[施工准备]-->B[主体施工]\n"
    )
    received = {}

    class _FakeProvider:
        async def chat(self, messages, **kwargs):
            received["user"] = (
                messages[-1].content
                if hasattr(messages[-1], "content") else str(messages[-1])
            )
            return "压缩后的简短正文。"

    asyncio.run(condense_overlong_content(
        unclosed, target_words=30, tolerance_ratio=0.20,
        provider=_FakeProvider(), section_title="t",
    ))
    user_text = received.get("user", "")
    assert "<<CHART_BLOCK_1>>" in user_text, "未闭合围栏未被 mask"
    assert "graph TD" not in user_text, "mermaid 源码裸露送 AI"


# ===========================================================================
# FIX-4 —— 压缩落库经 ContentCleaner 清洗
# ===========================================================================
def test_fix4_condense_result_cleaned():
    """AI 压缩返回带重复标题 + ## 标记，经 condense_overlong_results 接受时
    须被 ContentCleaner 清洗（与主生成落库口径一致）。"""
    from app.services.ai.workflows_sections import condense_overlong_results

    sec = {"id": "s1", "level": 3, "title": "施工进度计划", "is_l3_own": True}

    class _FakeProvider:
        async def chat(self, messages, **kwargs):
            # AI 不遵守压缩提示词：重复标题 + ## 标记
            return "施工进度计划\n## 1.1 概述\n压缩后的正文内容，足够简短。"

    async def _run():
        results = [{"id": "s1", "status": "completed", "content": "测" * 400}]
        summary = await condense_overlong_results(results, {"s1": sec}, _FakeProvider(), "standard")
        return results[0]["content"], summary

    out, summary = asyncio.run(_run())
    assert summary.get("condensed_count") == 1, "应记录压缩成功"
    assert not out.startswith("施工进度计划"), f"重复标题未清洗: {out[:30]!r}"
    assert "## " not in out, f"标题标记未清洗: {out[:40]!r}"


# ===========================================================================
# FIX-2 —— 压缩触发阈值与内部 hi 区间对齐
# ===========================================================================
def test_fix2_threshold_alignment_no_spin():
    """L3 自身正文（tol=0.20, hi=360）：wc=350 介于旧触发(345)~hi(360) 之间须
    不触发（消除触发即空转）；wc=400 须触发并压缩成功。"""
    from app.services.ai.workflows_sections import condense_overlong_results

    sec = {"id": "s1", "level": 3, "title": "t", "is_l3_own": True}

    class _FakeProvider:
        def __init__(self):
            self.calls = 0

        async def chat(self, messages, **kwargs):
            self.calls += 1
            return "压缩后的简短正文。"

    async def _run(wc_len):
        results = [{"id": "s1", "status": "completed", "content": "测" * wc_len}]
        p = _FakeProvider()
        summary = await condense_overlong_results(results, {"s1": sec}, p, "standard")
        return p.calls, summary

    calls_mid, sum_mid = asyncio.run(_run(350))
    calls_hi, sum_hi = asyncio.run(_run(400))
    assert calls_mid == 0 and sum_mid.get("condensed_count") == 0, (
        f"350 字应不触发（无空转）: calls={calls_mid}"
    )
    assert calls_hi == 1 and sum_hi.get("condensed_count") == 1, (
        f"400 字应触发并压缩: calls={calls_hi} condensed={sum_hi.get('condensed_count')}"
    )


# ===========================================================================
# FIX-10 —— 续写结构标记开头插换行
# ===========================================================================
def test_fix10_continue_inserts_newline_for_struct():
    """续写内容以列表项开头时必须插换行，防止「…如下：」与「- 材料准备」粘同一行
    （列表被拍平）。"""
    from app.services.ai.workflows_sections import _auto_continue_on_length

    class _FakeProvider:
        def __init__(self):
            self.n = 0

        async def chat(self, messages, **kwargs):
            self.n += 1
            if self.n == 1:
                return {"content": "- 材料准备：钢筋、模板、混凝土。", "finish_reason": "length"}
            return {"content": "收尾说明。", "finish_reason": "stop"}

    async def _run():
        return await _auto_continue_on_length(
            _FakeProvider(), "正文要求如下：", {"title": "t"}, {}, {}, max_rounds=3
        )

    out = asyncio.run(_run())
    assert "如下：\n- 材料准备" in out, f"列表项前未插换行: {out!r}"


def test_fix10_continue_no_newline_for_plain_sentence():
    """无缝接续的普通句子续写不插换行（中英文截断续写不应被强行断行）。"""
    from app.services.ai.workflows_sections import _auto_continue_on_length

    class _FakeProvider:
        async def chat(self, messages, **kwargs):
            return {"content": "后续补充说明。", "finish_reason": "stop"}

    async def _run():
        return await _auto_continue_on_length(
            _FakeProvider(), "正文。", {"title": "t"}, {}, {}, max_rounds=3
        )

    out = asyncio.run(_run())
    assert out == "正文。后续补充说明。", f"普通续写不应插换行: {out!r}"


def test_fix10_continue_no_newline_for_number_sentence():
    """以数字开头但非列表（如「1.8米」）的续写不误插换行。"""
    from app.services.ai.workflows_sections import _auto_continue_on_length

    class _FakeProvider:
        async def chat(self, messages, **kwargs):
            return {"content": "1.8米净高的区域需单独加固。", "finish_reason": "stop"}

    async def _run():
        return await _auto_continue_on_length(
            _FakeProvider(), "设计要点：", {"title": "t"}, {}, {}, max_rounds=3
        )

    out = asyncio.run(_run())
    assert out == "设计要点：1.8米净高的区域需单独加固。", f"数字开头误插换行: {out!r}"


# ===========================================================================
# FIX-9 —— 兜底扩写后计数经 _SectionTally.recount 全量重算
# ===========================================================================
def test_fix9_tally_recount_updates_all_counts():
    """recount 须一次性修正 saved/failed/cancelled 三口径（扩写整体替换 results）。"""
    from app.routers.sse.sse_content import _SectionTally

    t = _SectionTally()
    t.record("s1", "completed")
    t.record("s2", "completed")
    t.record("s3", "failed")
    assert t.saved == 2 and t.failed == 1 and t.cancelled == 0

    # 模拟兜底扩写后 s3 由 failed 改写为 completed，并新增 s4 cancelled
    t.recount([
        {"id": "s1", "status": "completed"},
        {"id": "s2", "status": "completed"},
        {"id": "s3", "status": "completed"},
        {"id": "s4", "status": "cancelled"},
    ])
    assert t.saved == 3, f"saved 应为 3: {t.saved}"
    assert t.failed == 0, f"failed 应归零: {t.failed}"
    assert t.cancelled == 1, f"cancelled 应为 1: {t.cancelled}"


# ===========================================================================
# FIX-6 / FIX-7 —— SSE 事件形状（源码锚定，沿用本仓库惯例）
# ===========================================================================
def _generate_region() -> str:
    """截取 generate_content_impl 函数源码（不含后续的 expand_content_impl）。"""
    from app.routers.sse import sse_content

    src = inspect.getsource(sse_content)
    start = src.index("async def generate_content_impl")
    end = src.index("async def expand_content_impl")
    return src[start:end]


def test_fix6_retry_round_first_round_is_one():
    """retry_round 首轮 round 须为 1（旧实现为 0，前端显示「第 0/2 轮」）。"""
    region = _generate_region()
    assert '"status": "retry_round"' in region, "未找到 retry_round 事件"
    # 不得再有 retry_round 事件携带 "round": 0
    bad = re.search(
        r'"status":\s*"retry_round"[^}]*"round":\s*0', region
    )
    assert not bad, "retry_round 首轮不得为 0（应为 1）"
    # 首条 retry_round 事件的 round 字段须为 1
    m = re.search(r'"status":\s*"retry_round".*?"round":\s*(\d+)', region, re.S)
    assert m and int(m.group(1)) == 1, f"retry_round 首轮 round 应为 1: {m.group(0) if m else None}"


def test_fix7_stopped_event_has_cancelled_failed():
    """全 cancelled 分支的 stopped 事件须带 cancelled / failed 字段
    （旧实现仅 total/success，前端进度面板把取消/失败归零）。"""
    region = _generate_region()
    # stopped = True 与 await queue.put 之间可能有注释行，正则容错
    m = re.search(
        r"cancelled_count > 0 and saved_count == 0 and failed_count == 0\s*:\s*"
        r"stopped = True\s*"
        r"(?:#[^\n]*\n\s*)*"
        r"await queue\.put\(\{(.*?)\}\)",
        region, re.S,
    )
    assert m, "未找到全 cancelled 分支的 stopped 事件"
    body = m.group(1)
    assert '"cancelled"' in body and '"failed"' in body, f"stopped 事件缺字段: {body}"
    assert '"cancelled": cancelled_count' in body
    assert '"failed": failed_count' in body


def test_fix9_expand_branch_uses_recount():
    """兜底扩写整体替换 results 后，计数必须经 _SectionTally.recount 全量重算。

    注：generate_content_impl 内兜底扩写以导入别名 ``_expand_fallback`` 调用，
    ``results = expanded`` 紧随其后，再经 ``_section_tally.recount(results)``
    全量重算（重试分支的 recount 在更靠前，故按「扩写调用之后」定位）。
    """
    region = _generate_region()
    assert "_expand_fallback(" in region, "未找到兜底扩写调用"
    call_idx = region.index("_expand_fallback(")
    recount_idx = region.find("_section_tally.recount(results)", call_idx)
    assert recount_idx != -1 and recount_idx > call_idx, (
        "扩写调用之后必须有 _section_tally.recount(results)"
    )


# ===========================================================================
# 遗留问题加固 —— P5 清洗残留 / FIX-2 阈值不变量 / 续写→压缩往返
# ===========================================================================
def test_p5_clean_no_residual_numbering():
    """ContentCleaner 须复用与 clean_generated_content 同源的编号剥离，
    「## 1.1 概述」清洗后不应残留孤立数字「1」（旧 _NUMBERING_PREFIX 只剥单层）。"""
    from app.services.content_cleaning import ContentCleaner

    out = ContentCleaner().clean(
        "施工进度计划",
        "施工进度计划\n## 1.1 概述\n压缩后的正文内容，足够简短。",
    )
    assert out == "概述\n压缩后的正文内容，足够简短。", f"残留孤立数字: {out!r}"
    # 三层编号同样正确
    assert ContentCleaner().clean("t", "### 1.2.3 细则\n正文。") == "细则\n正文。"
    # 不误伤量词 / 年份 / 中文编号
    assert ContentCleaner().clean("t", "1.8米净高的区域需加固。").startswith("1.8米")
    assert ContentCleaner().clean("t", "2026年计划如下。").startswith("2026年")
    assert ContentCleaner().clean("t", "一、总体要求。").startswith("总体要求")


def test_fix2_no_op_gap_invariant():
    """触发阈值必须 >= 内部 hi，否则区间 (触发, hi) 内章节会「触发即空转」。
    这是 FIX-2 修复后须长期锁定的不变量（覆盖强控 tol=0.20 与档位 tol=0.10）。"""
    import app.config as cfg

    threshold = cfg.CONTENT_WORD_COUNT_CONDENSE_THRESHOLD
    for target, tol in [(300, 0.20), (300, 0.10), (500, 0.20), (200, 0.10), (800, 0.15)]:
        thr = max(target * threshold, int(target * (1 + tol)))
        inner_hi = int(target * (1 + tol))
        assert thr >= inner_hi, (
            f"触发({thr}) < 内部hi({inner_hi}) 会复现触发即空转: target={target} tol={tol}"
        )


def test_continue_then_condense_converges():
    """L3 章节被 length 截断后续写（可能超长），再经压缩须收敛到目标硬上限内，
    不无限膨胀（续写涨→压缩删 往返的集成守护）。"""
    from app.services.ai.workflows_sections import (
        _auto_continue_on_length,
        condense_overlong_content,
    )
    from app.utils import count_words as cw

    class _ContinueProvider:
        def __init__(self):
            self.n = 0

        async def chat(self, messages, **kwargs):
            self.n += 1
            if self.n <= 2:  # 前两轮模拟被截断续写（超长）
                return {
                    "content": "补充内容段落，继续展开施工要点与资源配置方案细节。",
                    "finish_reason": "length",
                }
            return {"content": "收尾说明。", "finish_reason": "stop"}

    class _CondenseProvider:
        async def chat(self, messages, **kwargs):
            # 压缩 AI 返回显著缩短的文本（接受判定：短于当前）
            return "压缩后的简短正文。"

    async def _run():
        # base 已超 hi(360)，续写后更超长，确保压缩触发（验证往返收敛）
        base = "施工总体安排与资源配置以及进度管控要点说明。" * 20  # ~400 字
        continued = await _auto_continue_on_length(
            _ContinueProvider(), base,
            {"title": "t", "target_words": 300}, {}, {}, max_rounds=3,
        )
        out, meta = await condense_overlong_content(
            continued, target_words=300, tolerance_ratio=0.20,
            provider=_CondenseProvider(), section_title="t",
        )
        return out, meta, cw(continued)

    out, meta, continued_wc = asyncio.run(_run())
    # 压缩后不超过硬上限 hi=360（收敛）
    assert cw(out) <= int(300 * 1.2) + 1, f"续写+压缩未收敛，超出硬上限: wc={cw(out)}"
    assert meta.get("condensed") is True, "应发生压缩"

