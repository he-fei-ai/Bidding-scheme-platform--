"""易标 R9 / R10 / R11 生产接线测试（``services/ai/workflows_word_adjust.py``）。

移植时这三组规则只有纯函数与单测、**零生产调用点**；本次补上接线，本测试
固定生产语义（纯函数公式本身见 ``test_word_control_yibiao_2026_10_12.py``）：

- ``adjust_section_words``：单节调整的六道校验（不通过 → 返回 None 保留原文）；
- 图表围栏安全：掩蔽 → 还原；AI 篡改占位符 → 拒绝本轮；
- R9：仅强控章节生效、≤3 轮、方向/粒度/额度正确、未达标进 unresolved；
- R10：方向判定（低于下限扩写 / 高于上限缩写）、批次额度来自纯函数、
  缩写轮数上限、扩写「连续无进展即停」；
- 流水线：先 R9 后 R10、adjusted_ids 去重、取消即退出。
"""

import asyncio

import pytest

from app.services import word_control as wc
from app.services.ai.workflows_word_adjust import (
    adjust_section_words,
    build_word_adjust_messages,
    run_section_word_adjustments,
    run_total_word_adjustments,
    run_word_adjustment_pipeline,
)


def _zh(n: int, seed: str = "甲") -> str:
    """生成约 n 字的纯中文文本（count_readable_words 口径 ≈ n）。"""
    return (seed * (n // len(seed) + 1))[:n]


def _override(sid, s=300, lo=None, hi=None):
    return {
        sid: {
            "section_words": s,
            "lo": lo if lo is not None else int(s * 0.8),
            "hi": hi if hi is not None else int(s * 1.2),
            "generation_target": 0,
            "strict": True,
        }
    }


class _Provider:
    """按脚本返回内容的假 provider；记录调用次数与最后一次 messages。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
        self.last_messages = None

    async def chat(self, messages, **kwargs):  # noqa: ANN001
        self.calls += 1
        self.last_messages = messages
        if len(self.replies) == 1:
            return self.replies[0]
        return self.replies.pop(0)


# ---------------------------------------------------------------------------
# adjust_section_words —— R11 六道校验
# ---------------------------------------------------------------------------


class TestAdjustSectionWords:
    async def test_expand_accepts_valid_growth(self):
        p = _Provider([_zh(400)])
        new, meta = await adjust_section_words(
            provider=p, content=_zh(300), section_title="T", mode=wc.MODE_EXPAND,
            budget=200, target_words=400, lo=320, hi=480,
        )
        assert new is not None and meta["accepted"] is True
        assert meta["delta"] == 100

    async def test_empty_content_skipped(self):
        _new, meta = await adjust_section_words(
            provider=_Provider(["x"]), content="", section_title="T",
            mode=wc.MODE_EXPAND, budget=100, target_words=100, lo=80, hi=120,
        )
        assert meta["reason"] == "正文为空，跳过调整"

    async def test_zero_budget_skipped(self):
        _new, meta = await adjust_section_words(
            provider=_Provider([_zh(400)]), content=_zh(300), section_title="T",
            mode=wc.MODE_EXPAND, budget=0, target_words=400, lo=320, hi=480,
        )
        assert "额度为 0" in meta["reason"]

    async def test_no_growth_rejected(self):
        """R11 第 2 道：扩写必须净增字。"""
        _new, meta = await adjust_section_words(
            provider=_Provider([_zh(300)]), content=_zh(300), section_title="T",
            mode=wc.MODE_EXPAND, budget=200, target_words=400, lo=320, hi=480,
        )
        assert _new is None and meta["accepted"] is False
        assert "净增字" in meta["reason"]

    async def test_over_budget_rejected(self):
        """R11 第 3 道：实际变化不得超过本轮额度（防过冲）。"""
        _new, meta = await adjust_section_words(
            provider=_Provider([_zh(600)]), content=_zh(300), section_title="T",
            mode=wc.MODE_EXPAND, budget=100, target_words=400, lo=320, hi=480,
        )
        assert _new is None and "超出本轮额度" in meta["reason"]

    async def test_gap_must_shrink(self):
        """R11 第 4 道：与目标差距必须严格缩小（防震荡）。

        构造「越调越远」：目标 400 字，当前 300 字，缩写后变 299 字
        （净减字合法，但与目标的差距从 100 扩大到 101 → 拒绝）。
        """
        _new, meta = await adjust_section_words(
            provider=_Provider([_zh(299)]), content=_zh(300), section_title="T",
            mode=wc.MODE_SHRINK, budget=100, target_words=400, lo=320, hi=480,
        )
        assert _new is None and "差距未缩小" in meta["reason"]

    async def test_total_bounds_enforced(self):
        """R11 第 6 道：扩写后总量不得超上限。"""
        _new, meta = await adjust_section_words(
            provider=_Provider([_zh(400)]), content=_zh(300), section_title="T",
            mode=wc.MODE_EXPAND, budget=200, target_words=400, lo=320, hi=480,
            total_words=1000, maximum_words=1050, minimum_words=0,
            enforce_total_bounds=True,
        )
        assert _new is None and "将超出上限" in meta["reason"]

    async def test_empty_ai_reply_rejected(self):
        _new, meta = await adjust_section_words(
            provider=_Provider(["   "]), content=_zh(300), section_title="T",
            mode=wc.MODE_EXPAND, budget=200, target_words=400, lo=320, hi=480,
        )
        assert _new is None and meta["reason"] == "AI 返回空内容"

    async def test_provider_exception_isolated(self):
        class _Boom:
            async def chat(self, *a, **k):
                raise RuntimeError("network down")

        _new, meta = await adjust_section_words(
            provider=_Boom(), content=_zh(300), section_title="T",
            mode=wc.MODE_EXPAND, budget=200, target_words=400, lo=320, hi=480,
        )
        assert _new is None and "调整异常" in meta["reason"]


class TestChartFenceSafety:
    async def test_chart_fence_survives_adjustment(self):
        content = _zh(200) + "\n\n```mermaid\ngraph TD;\nA-->B;\n```\n\n" + _zh(200)
        kept = _zh(300) + "\n\n```mermaid\ngraph TD;\nA-->B;\n```\n\n" + _zh(300)
        p = _Provider([kept])
        new, meta = await adjust_section_words(
            provider=p, content=content, section_title="T", mode=wc.MODE_EXPAND,
            budget=300, target_words=600, lo=400, hi=800,
        )
        assert new is not None and meta["accepted"] is True
        assert "```mermaid" in new and "graph TD;" in new

    async def test_dropped_chart_block_rejected(self):
        """模型把图表整块删掉时必须拒绝本轮（否则图表消失 = 渲染失败）。"""
        content = _zh(200) + "\n\n```chart-json\n{\"a\":1}\n```\n\n" + _zh(200)
        p = _Provider([_zh(500)])
        new, meta = await adjust_section_words(
            provider=p, content=content, section_title="T", mode=wc.MODE_EXPAND,
            budget=300, target_words=500, lo=400, hi=600,
        )
        assert new is None and "图表代码块丢失" in meta["reason"]

    async def test_tampered_placeholder_rejected(self):
        content = _zh(200) + "\n\n```chart-json\n{\"a\":1}\n```\n\n" + _zh(200)
        p = _Provider([_zh(500) + "\n\n<<图表块_1>>"])
        new, meta = await adjust_section_words(
            provider=p, content=content, section_title="T", mode=wc.MODE_EXPAND,
            budget=300, target_words=500, lo=400, hi=600,
        )
        assert new is None and "占位符" in meta["reason"]

    def test_prompt_states_direction_granularity_budget(self):
        msgs = build_word_adjust_messages(
            section_title="施工部署", masked_content="正文", current_words=500,
            mode=wc.MODE_SHRINK, granularity=wc.Granularity.PARAGRAPH,
            budget=200, target_words=300, lo=240, hi=360,
        )
        text = "\n".join(getattr(m, "content", "") for m in msgs)
        assert "【调整方向】缩写" in text
        assert "段落" in text
        assert "最多减少 200 字" in text
        assert "240~360" in text


# ---------------------------------------------------------------------------
# R9 —— 强控小节双向拉回
# ---------------------------------------------------------------------------


class TestR9SectionAdjustments:
    async def test_skipped_without_word_control(self):
        results = [{"id": 1, "status": "completed", "content": _zh(900)}]
        summary = await run_section_word_adjustments(
            _Provider([]), results, {1: {"id": 1, "level": 3}}, word_control=None
        )
        assert summary["checked"] == 0 and summary["adjusted_count"] == 0

    async def test_all_within_range_skipped(self):
        results = [{"id": 1, "status": "completed", "content": _zh(300)}]
        summary = await run_section_word_adjustments(
            _Provider([]), results, {1: {"id": 1, "level": 3}},
            word_control=_override(1, 300),
        )
        assert summary["checked"] == 1 and summary["adjusted_count"] == 0

    async def test_shrink_pulls_back_into_range(self):
        results = [{"id": 1, "status": "completed", "content": _zh(500)}]
        # 500 → 340（区间 240~360），额度 |500-300|=200
        p = _Provider([_zh(340)])
        summary = await run_section_word_adjustments(
            p, results, {1: {"id": 1, "level": 3, "title": "T"}},
            word_control=_override(1, 300),
        )
        assert summary["shrunk"] == 1 and summary["adjusted_ids"] == [1]
        assert summary["unresolved_ids"] == []
        assert wc.count_readable_words(results[0]["content"]) == 340

    async def test_expand_pulls_up_into_range(self):
        results = [{"id": 1, "status": "completed", "content": _zh(150)}]
        p = _Provider([_zh(280)])
        summary = await run_section_word_adjustments(
            p, results, {1: {"id": 1, "level": 3, "title": "T"}},
            word_control=_override(1, 300),
        )
        assert summary["expanded"] == 1 and summary["unresolved_ids"] == []

    async def test_multi_round_converges(self):
        """多轮：每轮只走部分进展，靠「差距严格缩小」逐轮收敛。"""
        results = [{"id": 1, "status": "completed", "content": _zh(600)}]
        p = _Provider([_zh(420), _zh(340)])
        summary = await run_section_word_adjustments(
            p, results, {1: {"id": 1, "level": 3, "title": "T"}},
            word_control=_override(1, 300), max_rounds=3,
        )
        assert p.calls == 2
        assert summary["rounds"] == 2
        assert summary["unresolved_ids"] == []

    async def test_round_cap_marks_unresolved(self):
        """轮数用尽仍未达标 → unresolved（交 R12 警告 / 人工裁决）。"""
        results = [{"id": 1, "status": "completed", "content": _zh(600)}]
        # 每次都只减 1 字（净减字但差距几乎不变仍算缩小），3 轮后仍越界
        p = _Provider([_zh(590), _zh(580), _zh(570)])
        summary = await run_section_word_adjustments(
            p, results, {1: {"id": 1, "level": 3, "title": "T"}},
            word_control=_override(1, 300), max_rounds=3,
        )
        assert summary["unresolved_ids"] == [1]

    async def test_rejected_result_stops_section_early(self):
        """一次被 R11 打回即停止该节后续轮次（不白烧 token）。"""
        results = [{"id": 1, "status": "completed", "content": _zh(600)}]
        p = _Provider([_zh(600), _zh(340)])  # 第 1 轮无净减 → 打回
        summary = await run_section_word_adjustments(
            p, results, {1: {"id": 1, "level": 3, "title": "T"}},
            word_control=_override(1, 300),
        )
        assert p.calls == 1
        assert summary["fail_count"] == 1 and summary["adjusted_count"] == 0

    async def test_cancelled_event_skips(self):
        ev = asyncio.Event()
        ev.set()
        results = [{"id": 1, "status": "completed", "content": _zh(600)}]
        p = _Provider([_zh(340)])
        summary = await run_section_word_adjustments(
            p, results, {1: {"id": 1, "level": 3, "title": "T"}},
            word_control=_override(1, 300), cancel_event=ev,
        )
        assert p.calls == 0 and summary["adjusted_count"] == 0


# ---------------------------------------------------------------------------
# R10 —— 全文扩写 / 缩写
# ---------------------------------------------------------------------------


class TestR10TotalAdjustments:
    async def test_no_options_noop(self):
        summary = await run_total_word_adjustments(
            _Provider([]), [{"id": 1, "status": "completed", "content": _zh(100)}],
            {1: {"id": 1, "level": 3}},
            options=wc.WordControlOptions(),
        )
        assert summary["direction"] == "none"

    async def test_within_range_noop(self):
        opts = wc.WordControlOptions(minimum_words=500, maximum_words=1000)
        summary = await run_total_word_adjustments(
            _Provider([]), [{"id": 1, "status": "completed", "content": _zh(700)}],
            {1: {"id": 1, "level": 3}}, options=opts,
        )
        assert summary["direction"] == "none" and summary["rounds"] == 0

    async def test_expand_direction_and_budget(self):
        """总量不足 → 扩写；额度来自纯函数按比例分配。"""
        opts = wc.WordControlOptions(minimum_words=1200, section_words=600)
        results = [
            {"id": 1, "status": "completed", "content": _zh(200), "title": "A"},
            {"id": 2, "status": "completed", "content": _zh(200), "title": "B"},
        ]
        sections = {1: {"id": 1, "level": 3, "title": "A"}, 2: {"id": 2, "level": 3, "title": "B"}}
        p = _Provider([_zh(500), _zh(500)])
        summary = await run_total_word_adjustments(
            p, results, sections, options=opts,
        )
        assert summary["direction"] == "expand"
        assert summary["adjusted_count"] >= 1
        assert summary["total_after"] > summary["total_before"]
        # 扩写总量不得越过下限（应为「达到/接近」下限而非远超）
        assert summary["total_after"] <= 1200 + 50

    async def test_shrink_direction_and_rounds_cap(self):
        opts = wc.WordControlOptions(maximum_words=1000, section_words=300)
        results = [
            {"id": i, "status": "completed", "content": _zh(600), "title": f"S{i}"}
            for i in range(1, 4)
        ]
        sections = {i: {"id": i, "level": 3, "title": f"S{i}"} for i in range(1, 4)}
        # 每次只减 10 字（净减但进度极慢），验证缩写 ≤3 轮上限
        p = _Provider([_zh(590)] * 20)
        summary = await run_total_word_adjustments(
            p, results, sections, options=opts, max_rounds=3,
        )
        assert summary["direction"] == "shrink"
        assert summary["rounds"] <= 3 * wc.TOTAL_WORD_ADJUSTMENT_BATCH_SIZE
        assert summary["total_after"] <= 1800

    async def test_expansion_no_progress_exit(self):
        """扩写不限轮，但连续 N 轮无进展即停（防无限烧 token）。"""
        opts = wc.WordControlOptions(minimum_words=1000)
        results = [{"id": 1, "status": "completed", "content": _zh(100), "title": "A"}]
        sections = {1: {"id": 1, "level": 3, "title": "A"}}
        # 每次返回与原文等长 → R11 第 2 道打回 → 无进展
        p = _Provider([_zh(100)] * 50)
        summary = await run_total_word_adjustments(p, results, sections, options=opts)
        assert summary["direction"] == "expand"
        assert summary["adjusted_count"] == 0
        assert summary["fail_count"] >= wc.MAX_EXPANSION_NO_PROGRESS_ROUNDS

    async def test_shrink_runs_multiple_rounds_when_not_converged(self):
        """BUG-FIX 回归（本轮深度探索）：R10 缩写的收敛判定曾写反
        （``total >= maximum_words`` 即 break），等价于「仍未达标就退出」，
        多轮缩写实际只能跑 1 轮，rounds_cap 形同虚设。

        本用例构造「慢速缩写」：3 节各 600 字（总 1800，上限 1000），
        每轮每节仅净减 10 字。修复后应持续跑到轮数上限（3 轮 × 3 节
        = 9 次单节调整，总量 1800 → 1710）；修复前 rounds 恒为 3
        （只跑 1 轮）。
        """
        opts = wc.WordControlOptions(maximum_words=1000, section_words=300)
        results = [
            {"id": i, "status": "completed", "content": _zh(600), "title": f"S{i}"}
            for i in range(1, 4)
        ]
        sections = {i: {"id": i, "level": 3, "title": f"S{i}"} for i in range(1, 4)}
        # 脚本化回复：第 1 轮 590 ×3、第 2 轮 580 ×3、第 3 轮 570 ×3
        p = _Provider([_zh(590)] * 3 + [_zh(580)] * 3 + [_zh(570)] * 3)
        summary = await run_total_word_adjustments(
            p, results, sections, options=opts, max_rounds=3,
        )
        assert summary["direction"] == "shrink"
        # 3 个循环轮 × 每轮 3 节 = 9 次单节调整（修复前恒为 3）
        assert summary["rounds"] == 9
        assert summary["adjusted_count"] == 9
        assert summary["fail_count"] == 0
        # 1800 − 3 轮 × 3 节 × 10 字 = 1710（仍未达上限，靠轮数上限退出）
        assert summary["total_after"] == 1710

    async def test_shrink_exits_early_once_converged(self):
        """配套语义：一轮内已降到上限以下时应立即退出（不空跑后续轮）。"""
        opts = wc.WordControlOptions(maximum_words=1000, section_words=300)
        results = [
            {"id": i, "status": "completed", "content": _zh(600), "title": f"S{i}"}
            for i in range(1, 3)
        ]
        sections = {i: {"id": i, "level": 3, "title": f"S{i}"} for i in range(1, 3)}
        # 2 节各缩 100 字（恰好在批次额度内）→ 总量 1200 → 1000 收敛
        p = _Provider([_zh(500), _zh(500)])
        summary = await run_total_word_adjustments(
            p, results, sections, options=opts, max_rounds=3,
        )
        assert summary["direction"] == "shrink"
        assert summary["rounds"] == 2
        assert summary["adjusted_count"] == 2
        assert summary["total_after"] == 1000


# ---------------------------------------------------------------------------
# 流水线编排
# ---------------------------------------------------------------------------


class TestPipeline:
    async def test_disabled_without_options_and_override(self):
        out = await run_word_adjustment_pipeline(
            _Provider([]), [{"id": 1, "status": "completed", "content": _zh(100)}],
            {1: {"id": 1, "level": 3}},
        )
        assert out["enabled"] is False and out["adjusted_ids"] == []

    async def test_runs_section_then_total_and_dedups(self):
        opts = wc.WordControlOptions(minimum_words=1500, section_words=600)
        results = [
            {"id": 1, "status": "completed", "content": _zh(200), "title": "A"},
            {"id": 2, "status": "completed", "content": _zh(200), "title": "B"},
        ]
        sections = {i: {"id": i, "level": 3, "title": chr(64 + i)} for i in (1, 2)}
        p = _Provider([_zh(600)] * 6)
        stages = []

        async def _cb(stage, summary):
            stages.append(stage)

        out = await run_word_adjustment_pipeline(
            p, results, sections, options=opts,
            word_control=_override(1, 600) | _override(2, 600),
            on_summary=_cb,
        )
        assert out["enabled"] is True
        assert stages == ["section", "total"]  # 顺序：先 R9 后 R10
        assert out["adjusted_ids"] == sorted(set(out["adjusted_ids"]))
        assert wc.count_readable_words(results[0]["content"]) > 200

    async def test_cancelled_marks_stage_summaries(self):
        ev = asyncio.Event()
        ev.set()
        out = await run_word_adjustment_pipeline(
            _Provider([]), [{"id": 1, "status": "completed", "content": _zh(900)}],
            {1: {"id": 1, "level": 3}},
            options=wc.WordControlOptions(minimum_words=5000),
            word_control=_override(1, 300), cancel_event=ev,
        )
        assert out["adjusted_ids"] == []
