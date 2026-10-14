"""图号「同章连续、跨章唯一」回归测试（2026-10-14）。

缺陷：`_next_figure_number` 的分桶键用**完整** parent_section，而展示章号用
**截断**（第一个点号前）的章号，两者口径不一致。当调用方仅在未透传
`chapter_number`、回退用带点号的 `parent_section_number`（如 "3.1"/"3.2" 同属
第 3 章的两个子节）时，两张图落入不同桶却都渲染成「图3-x」，各自从 1 计数 →
第 3 章出现两个「图3-1」（编号冲突）。`_rewind_figure_number` 同样按完整桶回退，
与分配桶不一致会导致图号不回退（虚跳）。

修复：分桶键与展示章号统一取截断后的章号（`_fig_chapter`）。纯章号输入幂等，
生产主路径（透传 chapter_number）行为不变，零回归。
"""

from app.services.export_cache import (
    _fig_chapter,
    _fig_counter_ctx_var,
    _figure_counter_key,
    _next_figure_number,
    _reset_figure_counter,
    _rewind_figure_number,
)


class TestFigChapterNormalization:
    def test_dotted_truncates_to_chapter(self):
        assert _fig_chapter("3.2") == "3"
        assert _fig_chapter("10.1.4") == "10"

    def test_pure_chapter_idempotent(self):
        assert _fig_chapter("3") == "3"

    def test_empty_default_sentinel(self):
        assert _fig_chapter("") == ""
        assert _fig_chapter("   ") == ""
        assert _fig_chapter("default") == ""
        assert _fig_chapter("DEFAULT") == ""


class TestSameChapterContinuousNumbering:
    def test_dotted_parents_share_one_counter(self):
        """同章不同子节（3.1/3.2）→ 图号连续且唯一，修复前会重复「图3-1」。"""
        _reset_figure_counter(99)
        assert _next_figure_number("3.1", 99) == "图3-1"
        assert _next_figure_number("3.2", 99) == "图3-2"
        assert _next_figure_number("3.1", 99) == "图3-3"

    def test_different_chapters_independent(self):
        _reset_figure_counter(99)
        assert _next_figure_number("3.1", 99) == "图3-1"
        assert _next_figure_number("4.1", 99) == "图4-1"
        assert _next_figure_number("3.2", 99) == "图3-2"

    def test_pure_chapter_backward_compatible(self):
        """生产主路径：透传纯章号，行为与修复前完全一致。"""
        _reset_figure_counter(99)
        assert _next_figure_number("3", 99) == "图3-1"
        assert _next_figure_number("3", 99) == "图3-2"

    def test_empty_and_default_no_number(self):
        _reset_figure_counter(99)
        assert _next_figure_number("", 99) == ""
        assert _next_figure_number("default", 99) == ""


class TestRewindBucketConsistency:
    def test_rewound_number_is_reused(self):
        """渲染失败回滚后，下一个图号复用被回退的号（跨子节同样对齐桶）。"""
        _reset_figure_counter(99)
        assert _next_figure_number("5.2", 99) == "图5-1"
        _rewind_figure_number("5.2", 99)
        # 回退后同章的另一个子节再次分配，应重新拿到 图5-1
        assert _next_figure_number("5.1", 99) == "图5-1"

    def test_rewind_only_decrements_current_chapter(self):
        _reset_figure_counter(99)
        assert _next_figure_number("6.1", 99) == "图6-1"
        assert _next_figure_number("6.2", 99) == "图6-2"
        _rewind_figure_number("6.2", 99)
        assert _next_figure_number("6.1", 99) == "图6-2"

    def test_rewind_default_noop(self):
        _reset_figure_counter(99)
        # 空/default 不产生桶，rewind 不应抛异常也不影响其他桶
        _rewind_figure_number("", 99)
        _rewind_figure_number("default", 99)


class TestSessionContextVarNumbering:
    """build_docx 会话级计数器（ContextVar）路径同样按章归桶。"""

    def test_ctx_var_shares_chapter_bucket(self):
        ctx: dict = {}
        token = _fig_counter_ctx_var.set(ctx)
        try:
            assert _next_figure_number("7.1", 123) == "图7-1"
            assert _next_figure_number("7.2", 123) == "图7-2"
            # 桶键使用截断章号，同章子节合并到同一桶
            assert any(k.endswith("|7") for k in ctx), list(ctx)
        finally:
            _fig_counter_ctx_var.reset(token)

    def test_counter_key_uses_normalized_chapter(self):
        # 直接验证分桶键：同章不同子节归一后键相同
        assert _figure_counter_key(_fig_chapter("8.1"), 55) == _figure_counter_key(
            _fig_chapter("8.3"), 55
        )
