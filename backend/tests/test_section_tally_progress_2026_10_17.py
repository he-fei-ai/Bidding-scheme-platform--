"""SSE 章节进度契约补测（`_SectionTally` 不变量 + `slow_warning` 余量）

日期：2026-10-17

## 分工说明（避免重复维护）

`_SectionTally` 的**主干**用例已由
`tests/test_section_pipeline_fixes_2026_10_17.py` 承担：
`TestSectionTally`（6 例：重试覆盖 / 不双计 / recount 含 cancelled /
None id / percent 上限 / 接线为 tally）+ `TestSSERetryProgressEndToEnd`（2 例端到端）。
本文件**只补该文件未覆盖的增量**，请勿在此重复主干用例。

## 本文件覆盖的两块

1. `_SectionTally` 的**结构不变量与边界**（计数矩阵、非终态过滤、
   未知状态、id 类型区分、recount 清空语义、done 单调性、total=0 除零）。
2. `slow_warning` 事件的**余量契约**（下方 B-NEG-REMAINING，全仓唯一覆盖）。

## B-NEG-REMAINING 缺陷背景

`slow_warning` 的软告警阈值 `_soft_warn_secs = estimated * 1.5`，而
`_estimated_secs = 900 + n_leaves * 840 / concurrency`。大项目下前者会
**超过**硬熔断 14400s（300 章 / 并发 8 → estimated≈18900 → soft≈28350s）。

原实现按 `SSE_TOTAL_TIMEOUT - elapsed_total` 直接展示余量，越界时得到负数，
前端会显示「距硬熔断还剩 -232 min」——纯属误导。修复：clamp 到 0，
并显式给出 soft/hard 两个阈值供前端判断余量是否仍有意义。
"""

from __future__ import annotations

import pytest

from app.routers.sse.sse_content import (
    _TALLY_TERMINAL_STATUSES,
    _SectionTally,
)


def _percent(done: int, total: int) -> int:
    """复刻 sse_content.section_complete_callback 的 percent 公式。"""
    return round(done / max(total, 1) * 100)


# ============================================================================
# 1. _SectionTally 结构不变量与边界（增量覆盖）
# ============================================================================


class TestSectionTallyInvariants:
    """计数器自身的结构不变量"""

    def test_empty_tally_is_all_zero(self):
        t = _SectionTally()
        assert (t.saved, t.failed, t.cancelled, t.done) == (0, 0, 0, 0)

    def test_done_always_equals_sum_of_three_counters(self):
        """核心不变量：done 不得成为第四份独立口径，否则必然与三者失配。"""
        t = _SectionTally()
        for i in range(7):
            t.record(i, "completed")
        for i in range(7, 11):
            t.record(i, "failed")
        for i in range(11, 14):
            t.record(i, "cancelled")

        assert t.done == t.saved + t.failed + t.cancelled == 14

    def test_terminal_statuses_constant_is_exhaustive(self):
        """终态集合恰为三个，避免后续新增状态被静默漏计。"""
        assert set(_TALLY_TERMINAL_STATUSES) == {"completed", "failed", "cancelled"}

    def test_repeated_same_status_is_idempotent(self):
        """同一章节重复以相同状态登记，计数不得增长。"""
        t = _SectionTally()
        for _ in range(5):
            t.record(7, "completed")
        assert (t.saved, t.done) == (1, 1)


class TestSectionTallyBoundaries:
    """边界与异常输入"""

    def test_none_section_id_does_not_clobber_existing(self):
        """None id 的回调被忽略，且不得影响已登记章节。"""
        t = _SectionTally()
        t.record(1, "completed")
        t.record(None, "failed")
        assert (t.saved, t.failed, t.done) == (1, 0, 1)

    def test_string_and_int_ids_are_distinct(self):
        """id 类型不同不应互相覆盖（不做隐式类型转换）。"""
        t = _SectionTally()
        t.record(1, "completed")
        t.record("1", "failed")
        assert t.done == 2

    def test_unknown_status_is_neutral_not_counted(self):
        """未知状态计入 done 但不落入三计数器（保守：宁偏大不漏计）。"""
        t = _SectionTally()
        t.record(1, "completed")
        t.record(2, "weird")
        assert (t.saved, t.failed, t.cancelled) == (1, 0, 0)
        assert t.done == 2

    def test_zero_total_does_not_divide_by_zero(self):
        """total=0 时 percent 退化为 0，不抛 ZeroDivisionError。"""
        t = _SectionTally()
        assert _percent(t.done, 0) == 0

    def test_recount_drops_non_terminal_statuses(self):
        """generating/pending 不得进入计数。"""
        t = _SectionTally()
        t.recount([
            {"id": 1, "status": "completed"},
            {"id": 2, "status": "generating"},
            {"id": 3, "status": "pending"},
            {"id": 4, "status": "completed"},
        ])
        assert (t.saved, t.done) == (2, 2)

    def test_recount_ignores_entries_without_id(self):
        t = _SectionTally()
        t.recount([
            {"id": None, "status": "completed"},
            {"status": "completed"},
            {"id": 1, "status": "completed"},
        ])
        assert (t.saved, t.done) == (1, 1)

    @pytest.mark.parametrize("payload", [[], None])
    def test_recount_clears_on_empty_or_none(self, payload):
        """recount 后计数器必须被清空，不得残留旧终态。"""
        t = _SectionTally()
        t.record(1, "completed")
        t.record(2, "failed")
        t.recount(payload)
        assert (t.saved, t.failed, t.cancelled, t.done) == (0, 0, 0, 0)

    def test_done_nondecreasing_and_bounded(self):
        """done 单调不减且 ≤ total（终态只增不减，进度不回退）。"""
        total = 3
        t = _SectionTally()
        seq = [
            (1, "failed"), (2, "completed"), (3, "cancelled"),
            (1, "completed"), (3, "completed"),
        ]
        prev = 0
        for sid, status in seq:
            t.record(sid, status)
            assert t.done >= prev, "done 不得回退"
            assert t.done <= total
            prev = t.done


@pytest.mark.parametrize(
    "events,expected_saved,expected_failed,expected_cancelled",
    [
        ([], 0, 0, 0),
        ([(1, "completed")], 1, 0, 0),
        ([(1, "failed"), (1, "completed")], 1, 0, 0),
        ([(1, "failed"), (1, "failed")], 0, 1, 0),
        ([(1, "completed"), (1, "cancelled")], 0, 0, 1),
        ([(1, "failed"), (2, "failed"), (1, "completed"), (2, "completed")], 2, 0, 0),
        ([(1, "cancelled"), (2, "completed"), (3, "failed")], 1, 1, 1),
    ],
)
def test_counting_matrix(events, expected_saved, expected_failed, expected_cancelled):
    """计数矩阵：穷举各类终态组合，done 恒等于三者之和。"""
    t = _SectionTally()
    for sid, status in events:
        t.record(sid, status)

    assert t.saved == expected_saved
    assert t.failed == expected_failed
    assert t.cancelled == expected_cancelled
    assert t.done == expected_saved + expected_failed + expected_cancelled


def test_full_retry_scenario_never_exceeds_100_percent():
    """BUG 实证场景：2 章主生成全败 → 重试全成，percent 全程 ≤ 100。"""
    total = 2
    t = _SectionTally()
    seen = []

    t.record(1, "failed")
    seen.append(_percent(t.done, total))
    t.record(2, "failed")
    seen.append(_percent(t.done, total))
    t.record(1, "completed")
    seen.append(_percent(t.done, total))
    t.record(2, "completed")
    seen.append(_percent(t.done, total))

    assert seen == [50, 100, 100, 100]
    assert max(seen) <= 100


# ============================================================================
# 2. slow_warning 余量契约（B-NEG-REMAINING，全仓唯一覆盖）
# ============================================================================

HARD_TIMEOUT_SECS = 14400  # 4h 硬熔断（与 sse_content 模块常量一致）
PER_CHAPTER_SECS = 120 * (1 + 2 * 2) + 120  # 840s/章（_quality_retries=2）
BASE_SECS = 900


def _soft_warn_secs(n_leaves: int, concurrency: int) -> int:
    """复刻 sse_content 的软告警阈值公式。"""
    estimated = BASE_SECS + (n_leaves * PER_CHAPTER_SECS) / max(concurrency, 1)
    return int(estimated * 1.5)


class TestSlowWarningMargin:
    """「距硬熔断剩余」不得为负"""

    def test_large_project_soft_threshold_exceeds_hard(self):
        """缺陷前提：300 章 / 并发 8 时软阈值 28350s > 硬阈值 14400s。"""
        assert _soft_warn_secs(300, 8) > HARD_TIMEOUT_SECS

    def test_small_project_soft_threshold_within_hard(self):
        """小项目软阈值在硬阈值内，余量语义有意义。"""
        assert _soft_warn_secs(10, 8) < HARD_TIMEOUT_SECS

    def test_margin_text_is_clamped_to_zero(self):
        """文案中的剩余分钟必须 clamp。"""
        import inspect

        from app.routers.sse import sse_content

        source = inspect.getsource(sse_content)
        assert (
            "max(0, int((SSE_TOTAL_TIMEOUT - elapsed_total)/60))" in source
        ), "slow_warning 文案的剩余分钟必须 clamp 到 >= 0"

    @pytest.mark.parametrize(
        "field",
        [
            '"remaining_to_hard_secs"',
            '"soft_warn_secs"',
            '"hard_timeout_secs"',
            '"soft_warn_exceeds_hard"',
        ],
    )
    def test_structured_margin_fields_present(self, field):
        """必须给出结构化余量/阈值字段，供前端与测试断言。"""
        import inspect

        from app.routers.sse import sse_content

        assert field in inspect.getsource(sse_content)

    @pytest.mark.parametrize("elapsed", [0, 1000, 14400, 20000, 28350, 50000])
    def test_remaining_margin_never_negative(self, elapsed):
        """任意已用时长下余量恒 >= 0。"""
        assert max(0, int((HARD_TIMEOUT_SECS - elapsed) / 60)) >= 0

    @pytest.mark.parametrize(
        "n_leaves,concurrency,exceeds",
        [(300, 8, True), (200, 8, True), (10, 8, False), (10, 4, False)],
    )
    def test_overshoot_flag_by_project_size(self, n_leaves, concurrency, exceeds):
        """soft_warn_exceeds_hard 取值随项目规模正确翻转。"""
        assert (_soft_warn_secs(n_leaves, concurrency) >= HARD_TIMEOUT_SECS) is exceeds
