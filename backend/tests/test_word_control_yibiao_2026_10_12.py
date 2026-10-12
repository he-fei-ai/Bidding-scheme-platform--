"""易标（OpenBidKit）字数控制规则与标准移植测试（R1–R15）。

对应《报告_易标文字生成数量控制规则与标准_2026-10-12》第 9 节速查表逐条
验证，覆盖：R1 可读字数口径 / R2 万字输入 / R3 默认单节口径 / R4 页数预估 /
R5 强控 ±20% 区间 / R6 目标小节数反推与可行性 / R7 二审容差 ±2 /
R8 初稿折扣倒推 / R9 调整粒度 / R10 扩缩批次预算 / R11 六道硬校验 /
R12 失败兜底警告 / R13 内容保真条款 / R14 配置快照 / R15 可研报告下限。

运行：cd backend && pytest tests/test_word_control_yibiao_2026_10_12.py -v

本测试只依赖纯函数，不触碰数据库、不调 AI，可在任何环境秒级跑完。
"""

from __future__ import annotations

import pytest

from app.services import word_control as wc
from app.utils import count_readable_words, count_words, strip_readables_markup


# =============================================================================
# R1 —— 可读字数口径
# =============================================================================


class TestR1ReadableWordCount:
    def test_cjk_each_char_counts_as_one(self):
        assert count_readable_words("中文字数") == 4

    def test_western_tokens_count_as_one(self):
        assert count_readable_words("hello world 2026") == 3
        # 连接形态（易标报告 3 节：a.b / x-y / c/d 这类记 1 字）
        assert count_readable_words("v1.2.3 x-y c/d") == 3
        # 下划线是 Markdown 强调标记，会被剥离，因此 a_b 记作 2 个词
        assert count_readable_words("a_b") == 2

    def test_markdown_markup_is_excluded(self):
        # 标题 #、列表符号、表格分隔行与竖线、强调符、行内代码都不计；
        # 期望值：CJK 11 字（标题/第一点/列/列/值/值/强调）+ 西文 token 5 个
        # （A、B、1、2、code）= 16。
        text = "# 标题\n- 第一点\n| 列A | 列B |\n| --- | --- |\n| 值1 | 值2 |\n*强调* `code`"
        assert count_readable_words(text) == 16

    def test_fenced_code_block_is_excluded(self):
        text = "前置文字\n```mermaid\nflowchart TD\nA-->B\n```\n后置文字"
        assert count_readable_words(text) == len("前置文字后置文字")

    def test_unclosed_fence_is_excluded_until_eof(self):
        text = "前置文字\n```mermaid\nflowchart TD\nA-->B\n后置文字"
        assert count_readable_words(text) == len("前置文字")

    def test_markdown_image_is_excluded(self):
        assert count_readable_words("看图 ![图片](https://x/y.png) 结束") == len("看图结束")

    def test_url_is_excluded(self):
        for url in (
            "https://example.com/a?b=1",
            "http://example.com",
            "file:///C:/path/to/file.docx",
            "yibiao-asset://local/asset-1",
        ):
            assert count_readable_words(f"前 {url} 后") == 2, url

    def test_html_comment_and_tags_are_excluded(self):
        assert count_readable_words("前 <!-- 注释内容 --> 中 <b>加粗</b>后") == len("前中加粗后")
        assert count_readable_words("前<br>后") == 2

    def test_link_keeps_anchor_text_only(self):
        assert count_readable_words("见 [官网首页](https://example.com) 了解") == len("见官网首页了解")

    def test_reference_style_link_keeps_anchor_only(self):
        assert count_readable_words("见 [官网][1] 与 [1]: https://example.com") == len("见官网与")

    def test_cjk_extension_a_range_is_counted(self):
        # \u3400-\u9fff 覆盖扩展 A 区（易标同款区间，比 \u4e00-\u9fff 更全）
        assert count_readable_words("\u3400\u3401") == 2

    def test_punctuation_and_table_separator_do_not_count(self):
        text = "第一行，第二行。\n| --- | --- |"
        assert count_readable_words(text) == len("第一行第二行")

    def test_empty_and_whitespace_only(self):
        assert count_readable_words("") == 0
        assert count_readable_words("   \n\t  ") == 0
        assert count_readable_words("# * _ - > | ` [ ]") == 0

    def test_readable_differ_from_legacy_count_words_on_markup(self):
        # 旧口径 count_words 不剥离 Markdown：图表围栏里的源码也会被计入。
        text = "结论一句话\n```mermaid\nflowchart TD\nA-->B\nC-->D\n```\n建议二句话"
        assert count_readable_words(text) < count_words(text)
        assert count_readable_words(text) == len("结论一句话建议二句话")

    def test_strip_readables_markup_returns_plain_text(self):
        """表格**分隔行**被删除，但**数据行**的文字保留（与易标口径一致）。"""
        cleaned = strip_readables_markup("# 标题\n- 一点\n![x](y)\n| a | b |\n|---|---|\n")
        assert "```" not in cleaned and "|" not in cleaned and "!" not in cleaned
        assert cleaned == "标题 一点 a b"

    def test_crlf_normalized_before_stripping(self):
        """CRLF 输入必须先归一化再剥离（否则行首锚点正则被 \r 挡住）。"""
        assert count_readable_words("甲\r\n乙\r丙") == 3

# =============================================================================
# R2 / R3 / R5 —— 配置输入、归一化与强控区间
# =============================================================================


class TestR2InputParsing:
    @pytest.mark.parametrize(
        "draft, expected",
        [("2", 20000), ("0.15", 1500), ("1", 10000), ("0", 0), ("", 0), ("0.0001", 1)],
    )
    def test_wan_unit_parsing(self, draft, expected):
        """R2 —— 前端以万字为单位，最多 4 位小数。"""
        assert wc.parse_word_count_draft(draft) == expected

    @pytest.mark.parametrize(
        "draft", ["-1", "1.23456", "1e3", "abc", "1.2.3", "  -0.5", "1,000", "Infinity"]
    )
    def test_invalid_drafts_rejected(self, draft):
        """非法值一律返回 None，由调用方报「字数设置只允许填写非负整数」。"""
        assert wc.parse_word_count_draft(draft) is None

    def test_none_returns_zero(self):
        assert wc.parse_word_count_draft(None) == 0


class TestR2R5Normalization:
    def test_int_and_float_floor(self):
        opts = wc.normalize_word_control_options(
            {"minimum_words": 1000.9, "maximum_words": "2000.9", "section_words": 3000}
        )
        assert (opts.minimum_words, opts.maximum_words, opts.section_words) == (1000, 2000, 3000)

    def test_negative_and_invalid_become_zero(self):
        opts = wc.normalize_word_control_options(
            {"minimum_words": -5, "maximum_words": "abc", "section_words": None}
        )
        assert opts == wc.WordControlOptions()

    def test_strict_forced_off_when_no_section_words(self):
        """R5 归一化硬规则：strong 开关仅在 section_words > 0 时允许为真。"""
        opts = wc.normalize_word_control_options(
            {"maximum_words": 20000, "strict_section_words": True}
        )
        assert opts.strict_section_words is False
        assert opts.maximum_words == 20000

    @pytest.mark.parametrize("raw", [1, True, "true", "yes", "on", "1"])
    def test_strict_accepts_common_truthy_forms(self, raw):
        opts = wc.normalize_word_control_options(
            {"section_words": 1200, "strict_section_words": raw}
        )
        assert opts.strict_section_words is True

    @pytest.mark.parametrize("raw", [0, False, "false", "no", "off", "", None])
    def test_strict_accepts_common_falsy_forms(self, raw):
        opts = wc.normalize_word_control_options(
            {"section_words": 1200, "strict_section_words": raw}
        )
        assert opts.strict_section_words is False

    def test_camel_case_keys_supported(self):
        opts = wc.normalize_word_control_options(
            {"minimumWords": 10000, "maximumWords": 20000, "sectionWords": 3000,
             "strictSectionWords": True}
        )
        assert opts == wc.WordControlOptions(10000, 20000, 3000, True)

    def test_non_mapping_raises_type_error(self):
        with pytest.raises(TypeError):
            wc.normalize_word_control_options(["not", "a", "mapping"])

    def test_empty_source_gives_all_zero(self):
        assert wc.normalize_word_control_options() == wc.WordControlOptions()
        assert wc.normalize_word_control_options(None) == wc.WordControlOptions()

    def test_bool_semantics(self):
        """全 0 表示「未配置字数控制」，任何一项非 0 即为已配置。"""
        assert bool(wc.WordControlOptions(0, 100, 0, False)) is True
        assert bool(wc.WordControlOptions()) is False

    def test_default_section_words_is_3000(self):
        """R3 —— 全局默认单节口径 3000，section_words 为 0 时回落。"""
        assert wc.DEFAULT_SECTION_WORDS == 3000
        assert wc.effective_section_words(wc.WordControlOptions()) == 3000
        assert wc.effective_section_words(wc.WordControlOptions(0, 0, 1200, True)) == 1200


class TestR5ValidationAndBounds:
    def test_max_smaller_than_min_rejected(self):
        opts = wc.WordControlOptions(40000, 20000, 3000, True)
        errors = wc.validate_word_control_options(opts)
        assert len(errors) == 1 and "最多字数" in errors[0]

    def test_infeasible_leaf_range_rejected(self):
        """max ≥ min 但叶子数区间不存在：最少需要 2 节，最多只允许 1 节。"""
        opts = wc.WordControlOptions(3500, 4000, 3000, False)
        errors = wc.validate_word_control_options(opts)
        assert any("区间不存在" in e for e in errors), errors

    def test_max_below_one_leaf_rejected(self):
        """最多字数不足 1 个小节（max < effective S）。"""
        opts = wc.WordControlOptions(0, 100, 3000, False)
        errors = wc.validate_word_control_options(opts)
        assert any("不足 1 个小节" in e for e in errors), errors

    def test_valid_configs_have_no_errors(self):
        for opts in (
            wc.WordControlOptions(20000, 40000, 3000, True),
            wc.WordControlOptions(0, 30000, 0, False),
            wc.WordControlOptions(5000, 0, 1500, False),
            wc.WordControlOptions(),
        ):
            assert wc.validate_word_control_options(opts) == [], opts

    @pytest.mark.parametrize(
        "section_words, expected",
        [
            (300, (240, 360)),
            (3000, (2400, 3600)),
            (1200, (960, 1440)),
            (0, (0, 0)),
            (-10, (0, 0)),
            (333, (267, 399)),
            (250, (200, 300)),
        ],
    )
    def test_strict_bounds_formula(self, section_words, expected):
        """R5 —— [ceil(0.8S), floor(1.2S)]：下界向上取整、上界向下取整。"""
        assert wc.derive_strict_bounds(section_words) == expected

    def test_strict_bounds_width_never_exceeds_40_percent(self):
        for s in range(1, 5000, 7):
            lo, hi = wc.derive_strict_bounds(s)
            assert hi - lo <= int(s * 0.4) + 1


class TestR4PageEstimation:
    def test_both_bounds_use_average(self):
        """R4 —— 双边取均值 (min+max)/2 ÷ 650 向上取整。"""
        assert wc.estimate_pages(20000, 40000) == 47  # 30000 / 650 = 46.15

    def test_only_max(self):
        assert wc.estimate_pages(0, 13000) == 20

    def test_only_min_exact_multiple(self):
        assert wc.estimate_pages(1300, 0) == 2

    def test_only_min_rounds_up(self):
        assert wc.estimate_pages(1301, 0) == 3

    def test_no_bounds_returns_zero(self):
        assert wc.estimate_pages(0, 0) == 0

    def test_custom_words_per_page(self):
        assert wc.estimate_pages(0, 1300, words_per_page=1300) == 1
        assert wc.estimate_pages(0, 1301, words_per_page=1300) == 2
        assert wc.estimate_pages(0, 1300, words_per_page=0) == 0


# =============================================================================
# R6 —— 目录阶段：把字数翻译成小节数
# =============================================================================


class TestR6LeafCount:
    def test_leaf_range_both_bounds(self):
        opts = wc.WordControlOptions(20000, 40000, 3000, True)
        assert wc.leaf_count_range(opts) == (7, 13)  # ceil(20000/3000), floor(40000/3000)

    def test_leaf_range_unbounded_max(self):
        opts = wc.WordControlOptions(9000, 0, 3000, False)
        assert wc.leaf_count_range(opts) == (3, None)

    def test_leaf_range_unbounded_min(self):
        opts = wc.WordControlOptions(0, 40000, 3000, False)
        assert wc.leaf_count_range(opts) == (0, 13)

    def test_leaf_range_falls_back_to_default_3000(self):
        opts = wc.WordControlOptions(6000, 0, 0, False)
        assert wc.leaf_count_range(opts) == (2, None)

    @pytest.mark.parametrize(
        "options, leaf, expected",
        [
            ((20000, 40000, 3000), 10, False),  # 在 [7, 13] 内
            ((20000, 40000, 3000), 5, True),    # 少于下限
            ((20000, 40000, 3000), 20, True),   # 超过上限
            ((0, 0, 0), 999, False),            # 未配置不拦截
            ((9000, 0, 3000), 1, True),         # 低于下限
            ((0, 40000, 3000), 1, False),       # 无下限，1 合法
        ],
    )
    def test_is_leaf_count_outside_range(self, options, leaf, expected):
        assert (
            wc.is_outline_leaf_count_outside_range(wc.WordControlOptions(*options), leaf) is expected
        )

    @pytest.mark.parametrize(
        "options, expected",
        [
            ((20000, 40000, 3000), 10),  # ceil(30000/3000) = 10
            ((0, 40000, 3000), 11),      # floor(40000/3000)=13 - 2
            ((12000, 0, 3000), 6),       # ceil(12000/3000)=4 + 2
            ((0, 0, 3000), None),        # 都未设置 → 自主决定
            ((3000, 0, 3000), 3),        # ceil(3000/3000)=1 + 2
            ((30000, 0, 3000), 12),      # ceil(30000/3000)=10 + 2
        ],
    )
    def test_derive_target_leaf_count(self, options, expected):
        """R6 —— 双边取均值；单上限减 2；单下限加 2；都不设返回 None。"""
        assert wc.derive_target_leaf_count(wc.WordControlOptions(*options)) == expected

    def test_single_upper_bound_never_below_one(self):
        # max = 1000, S = 3000 → floor(0.33)=0 - 2 = -2 → 钳制到 1
        assert wc.derive_target_leaf_count(wc.WordControlOptions(0, 1000, 3000, False)) == 1

    def test_none_source_returns_none(self):
        assert wc.derive_target_leaf_count(None) is None

    def test_feasibility_blocks_below_required_leaf_count(self):
        opts = wc.WordControlOptions(20000, 40000, 3000, True)  # 目标 10 节
        msg = wc.check_leaf_count_feasibility(opts, required_leaf_count=50)
        assert msg is not None and "50" in msg

    def test_feasibility_ok_when_required_met(self):
        opts = wc.WordControlOptions(20000, 40000, 3000, True)
        assert wc.check_leaf_count_feasibility(opts, required_leaf_count=10) is None
        assert wc.check_leaf_count_feasibility(opts) is None

    def test_feasibility_blocks_strict_min_overflow(self):
        """强控下限 2400 字 × 必需 1 节 已超出全文上限 2000 字。"""
        opts = wc.WordControlOptions(1000, 2000, 3000, True)
        msg = wc.check_leaf_count_feasibility(opts, required_leaf_count=1)
        assert msg is not None and "已超出全文上限" in msg

    def test_feasibility_none_target(self):
        assert wc.check_leaf_count_feasibility(wc.WordControlOptions(), 100) is None


class TestR7ReviewTolerance:
    def test_review_bounds(self):
        """R7 —— 二审容差 ±2。"""
        assert wc.OUTLINE_REVIEW_LEAF_TOLERANCE == 2
        assert wc.outline_review_bounds(12) == (10, 14)
        assert wc.outline_review_bounds(100) == (98, 102)
        assert wc.outline_review_bounds(None) is None
        assert wc.outline_review_bounds(0) is None

    def test_review_bounds_floor_at_one(self):
        assert wc.outline_review_bounds(1) == (1, 3)

    @pytest.mark.parametrize(
        "target, actual, expected",
        [
            (12, 10, True),
            (12, 14, True),
            (12, 12, True),
            (12, 9, False),
            (12, 15, False),
            (None, 999, True),
        ],
    )
    def test_within_review_tolerance(self, target, actual, expected):
        assert wc.is_leaf_count_within_review_tolerance(target, actual) is expected


# =============================================================================
# R8 / R9 —— 正文生成阶段：初稿折扣目标与调整粒度
# =============================================================================


class TestR8GenerationTarget:
    def test_strict_with_max_uses_max_over_leaf(self):
        """R8 —— generationTarget = max(ceil(0.8S), floor(0.8 × max / 叶子数))。"""
        # 0.8 × 50000 / 10 = 4000 > ceil(0.8 × 3000) = 2400
        assert wc.compute_generation_word_target(3000, 50000, 10, True) == 4000

    def test_section_floor_dominates_when_max_is_tight(self):
        # 0.8 × 20000 / 100 = 160 < 2400 → 以小节下限为准，避免引导到越界区
        assert wc.compute_generation_word_target(3000, 20000, 100, True) == 2400

    def test_section_floor_dominates_when_max_is_loose_but_many_leaves(self):
        assert wc.compute_generation_word_target(3000, 10000, 100, True) == 2400

    @pytest.mark.parametrize(
        "section_words, maximum, leaf, strict, expected",
        [
            (3000, 0, 10, True, 0),      # 未设上限 → 不生效
            (3000, 50000, 10, False, 0), # 非强控 → 不生效
            (0, 50000, 10, True, 0),     # 无单节字数 → 不生效
            (3000, 50000, 0, True, 0),   # 叶子数为 0 → 不生效
            (3000, -1, 10, True, 0),     # 负数上限 → 不生效
        ],
    )
    def test_non_applicable_combinations_return_zero(
        self, section_words, maximum, leaf, strict, expected
    ):
        assert wc.compute_generation_word_target(section_words, maximum, leaf, strict) == expected

    def test_ratio_constant_is_0_8(self):
        assert wc.GENERATION_WORD_TARGET_RATIO == 0.8


class TestR9Granularity:
    def test_large_gap_uses_paragraph(self):
        assert wc.granularity_for_gap(100, 300) == wc.Granularity.PARAGRAPH
        assert wc.granularity_for_gap(800, 300) == wc.Granularity.PARAGRAPH

    def test_small_gap_uses_sentence(self):
        assert wc.granularity_for_gap(280, 300) == wc.Granularity.SENTENCE
        assert wc.granularity_for_gap(330, 300) == wc.Granularity.SENTENCE

    def test_exactly_20_percent_uses_sentence(self):
        """阈值语义是「大于 20%」，恰好 20% 走句子级。"""
        assert wc.granularity_for_gap(240, 300) == wc.Granularity.SENTENCE
        assert wc.granularity_for_gap(361, 300) == wc.Granularity.PARAGRAPH

    def test_degenerate_inputs_fallback_to_paragraph(self):
        assert wc.granularity_for_gap(0, 300) == wc.Granularity.PARAGRAPH
        assert wc.granularity_for_gap(100, 0) == wc.Granularity.PARAGRAPH
        assert wc.granularity_for_gap(100, -1) == wc.Granularity.PARAGRAPH

    def test_round_constants(self):
        """R9 / R10 轮数与批次常量与易标一致。"""
        assert wc.MAX_WORD_ADJUSTMENT_ROUNDS == 3
        assert wc.MAX_EXPANSION_NO_PROGRESS_ROUNDS == 3
        assert wc.TOTAL_WORD_ADJUSTMENT_BATCH_SIZE == 10


# =============================================================================
# R10 —— 全文字数调整批次预算
# =============================================================================


def _secs(items: list[tuple[int, int]]) -> list[dict]:
    """构造测试用章节（id, words）。"""
    return [{"id": sid, "title": f"第{sid}节", "word_count": w} for sid, w in items]


class TestR10ExpansionBatch:
    def test_no_gap_returns_empty(self):
        opts = wc.WordControlOptions(1000, 0, 0, False)
        assert wc.build_total_word_expansion_batch(
            _secs([(1, 1500)]), total_words=1500, minimum_words=1000, options=opts
        ) == []

    def test_negative_gap_returns_empty(self):
        """实际字数已超过下限 → 差额为负，本轮无需扩写。"""
        opts = wc.WordControlOptions(1500, 0, 0, False)
        assert wc.build_total_word_expansion_batch(
            _secs([(1, 5000)]), total_words=5000, minimum_words=1500, options=opts
        ) == []

    def test_empty_pool_returns_empty(self):
        opts = wc.WordControlOptions(5000, 0, 0, False)
        assert wc.build_total_word_expansion_batch(
            [], total_words=0, minimum_words=5000, options=opts
        ) == []
        assert wc.build_total_word_expansion_batch(
            _secs([(1, 0)]), total_words=0, minimum_words=5000, options=opts
        ) == []

    def test_equal_gap_distributed_evenly(self):
        """非强控：容量无限，指导缺口相同时均分，合计正好补齐差额。"""
        opts = wc.WordControlOptions(12000, 0, 0, False)
        batch = wc.build_total_word_expansion_batch(
            _secs([(1, 1000), (2, 1000), (3, 1000)]),
            total_words=3000, minimum_words=12000, options=opts,
        )
        assert len(batch) == 3
        assert sum(i["budget"] for i in batch) == 9000
        assert {i["budget"] for i in batch} == {3000}

    def test_strict_capacity_caps_each_section(self):
        """强控：单节容量封顶为 1.2S − 当前字数，合计仍精确补齐差额。"""
        opts = wc.WordControlOptions(2000, 0, 1000, True)  # 区间 [800, 1200]
        batch = wc.build_total_word_expansion_batch(
            _secs([(1, 900), (2, 500)]),
            total_words=1400, minimum_words=2000, options=opts,
        )
        by_id = {i["id"]: i["budget"] for i in batch}
        assert by_id[1] <= 300  # 1200 - 900
        assert by_id[2] <= 700  # 1200 - 500
        assert sum(by_id.values()) == 600

    def test_no_section_exceeds_strict_upper_bound(self):
        opts = wc.WordControlOptions(30000, 0, 1000, True)
        batch = wc.build_total_word_expansion_batch(
            _secs([(1, 100), (2, 200), (3, 300)]),
            total_words=600, minimum_words=30000, options=opts,
        )
        for item in batch:
            assert item["current_words"] + item["budget"] <= 1200

    def test_batch_items_shape(self):
        opts = wc.WordControlOptions(20000, 0, 0, False)
        batch = wc.build_total_word_expansion_batch(
            _secs([(7, 1500)]), total_words=1500, minimum_words=20000, options=opts,
        )
        assert batch[0]["id"] == 7
        assert batch[0]["title"] == "第7节"
        assert batch[0]["current_words"] == 1500
        assert batch[0]["reason"] == "expand"
        assert batch[0]["budget"] == 18500

    def test_guidance_words_override(self):
        """显式 guidance_words 作为分配依据（本平台 L3 自身正文目标即走这条）。"""
        opts = wc.WordControlOptions(1300, 0, 0, False)
        batch = wc.build_total_word_expansion_batch(
            _secs([(1, 100)]), total_words=100, minimum_words=1300, options=opts,
            guidance_words=300,
        )
        assert batch[0]["budget"] == 1200

    def test_zero_words_sections_skipped(self):
        """0 字小节没有可扩展的锚点，不参与分配。"""
        opts = wc.WordControlOptions(1000, 0, 0, False)
        batch = wc.build_total_word_expansion_batch(
            _secs([(1, 0), (2, 500)]), total_words=500, minimum_words=1000, options=opts,
        )
        assert [i["id"] for i in batch] == [2]
        assert batch[0]["budget"] == 500


class TestR10ShrinkBatch:
    def test_no_gap_returns_empty(self):
        opts = wc.WordControlOptions(0, 5000, 0, False)
        assert wc.build_total_word_shrink_batch(
            _secs([(1, 1500)]), total_words=1500, maximum_words=5000, options=opts
        ) == []

    def test_empty_pool_returns_empty(self):
        opts = wc.WordControlOptions(0, 100, 0, False)
        assert wc.build_total_word_shrink_batch(
            [], total_words=500, maximum_words=100, options=opts
        ) == []
        # 只有 0/1 字小节：没有可缩空间
        assert wc.build_total_word_shrink_batch(
            _secs([(1, 1), (2, 1)]), total_words=2, maximum_words=1, options=opts
        ) == []

    def test_orders_by_words_descending(self):
        """按字数降序优先动最大的节，合计补齐差额。"""
        opts = wc.WordControlOptions(0, 5000, 0, False)
        batch = wc.build_total_word_shrink_batch(
            _secs([(1, 1000), (2, 2000), (3, 3000)]),
            total_words=6000, maximum_words=5000, options=opts,
        )
        assert [i["id"] for i in batch] == [3, 2, 1]
        assert sum(i["budget"] for i in batch) == 1000

    def test_non_strict_capacity_never_below_one_word(self):
        """非强控容量 = words − 1：不允许把正文缩成空节。"""
        opts = wc.WordControlOptions(0, 1, 0, False)
        batch = wc.build_total_word_shrink_batch(
            _secs([(1, 2), (2, 2), (3, 2), (4, 2)]),
            total_words=8, maximum_words=1, options=opts,
        )
        assert batch, "应有可缩批次"
        for item in batch:
            assert item["current_words"] - item["budget"] >= 1

    def test_strict_capacity_respects_25_percent_and_lower_bound(self):
        """强控容量 = min(floor(words × 0.25), words − 0.8S, words − 1)。"""
        opts = wc.WordControlOptions(0, 800, 1000, True)  # 区间 [800, 1200]
        batch = wc.build_total_word_shrink_batch(
            _secs([(1, 1200)]), total_words=1200, maximum_words=800, options=opts,
        )
        # min(300, 400, 1199) = 300：25% 单轮上限先触发，缺口 400 无法一轮补齐
        assert batch[0]["budget"] == 300
        assert 1200 - 300 >= 800  # 缩完仍落在强控区间内

    def test_strict_capacity_never_below_lower_bound(self):
        """即使想缩更多，也不能把某节缩到强控下限以下。"""
        opts = wc.WordControlOptions(0, 1050, 1000, True)  # 区间 [800, 1200]
        batch = wc.build_total_word_shrink_batch(
            _secs([(1, 1100)]), total_words=1100, maximum_words=1050, options=opts,
        )
        assert batch[0]["budget"] == 50
        assert 1100 - batch[0]["budget"] >= 800

    def test_batch_size_limits_candidates(self):
        """每轮最多处理 batch_size 个小节。"""
        opts = wc.WordControlOptions(0, 100, 0, False)
        secs = _secs([(i, 100) for i in range(1, 13)])  # 12 节，共 1200 字
        batch = wc.build_total_word_shrink_batch(
            secs, total_words=1200, maximum_words=100, options=opts, batch_size=5,
        )
        assert len(batch) <= 5

    def test_min_capacity_filter_skips_small_sections(self):
        """可缩空间 < 平均预算 × 30% 的节被跳过。"""
        opts = wc.WordControlOptions(0, 100, 0, False)
        # s1 只有 3 字（容量 2），s2 有 1000 字（容量 999）
        batch = wc.build_total_word_shrink_batch(
            _secs([(1, 3), (2, 1000)]), total_words=1003, maximum_words=100, options=opts,
        )
        assert [i["id"] for i in batch] == [2]

    def test_batch_items_shape(self):
        opts = wc.WordControlOptions(0, 100, 0, False)
        batch = wc.build_total_word_shrink_batch(
            _secs([(9, 500)]), total_words=500, maximum_words=100, options=opts,
        )
        assert batch[0]["id"] == 9
        assert batch[0]["reason"] == "shrink"
        assert batch[0]["budget"] == 400

    def test_all_sections_at_lower_bound_gives_empty(self):
        """所有候选都已贴住强控下限 → 无可缩空间。"""
        opts = wc.WordControlOptions(0, 100, 1000, True)
        assert wc.build_total_word_shrink_batch(
            _secs([(1, 800), (2, 801)]), total_words=1601, maximum_words=100, options=opts,
        ) == []


# =============================================================================
# R11 —— 字数调整的六道程序侧硬校验
# =============================================================================


class TestR11Validation:
    def test_accept_valid_expand(self):
        ok, reason = wc.validate_word_adjustment_result(
            current_words=1000, new_words=1100, mode=wc.MODE_EXPAND,
            target_words=1200, maximum_change_words=200,
        )
        assert ok is True and reason == "ok"

    def test_accept_valid_shrink(self):
        ok, _ = wc.validate_word_adjustment_result(
            current_words=2000, new_words=1800, mode=wc.MODE_SHRINK,
            target_words=1500, maximum_change_words=300,
        )
        assert ok is True

    def test_rule_1_empty_content_rejected(self):
        ok, reason = wc.validate_word_adjustment_result(
            current_words=1000, new_words=0, mode=wc.MODE_SHRINK,
        )
        assert ok is False and "空" in reason

    def test_rule_2_expand_must_increase(self):
        ok, reason = wc.validate_word_adjustment_result(
            current_words=1000, new_words=900, mode=wc.MODE_EXPAND,
        )
        assert ok is False and "净增字" in reason

    def test_rule_2_expand_no_change_rejected(self):
        ok, _ = wc.validate_word_adjustment_result(
            current_words=1000, new_words=1000, mode=wc.MODE_EXPAND,
        )
        assert ok is False

    def test_rule_2_shrink_must_decrease(self):
        ok, reason = wc.validate_word_adjustment_result(
            current_words=1000, new_words=1200, mode=wc.MODE_SHRINK,
        )
        assert ok is False and "净减字" in reason

    def test_rule_2_invalid_mode_rejected(self):
        ok, reason = wc.validate_word_adjustment_result(
            current_words=1000, new_words=1100, mode="rewrite",
        )
        assert ok is False and "非法调整方向" in reason

    def test_rule_3_budget_overshoot_rejected(self):
        """不允许过冲：实际变化不得超过本轮额度。"""
        ok, reason = wc.validate_word_adjustment_result(
            current_words=1000, new_words=1400, mode=wc.MODE_EXPAND,
            target_words=1500, maximum_change_words=200,
        )
        assert ok is False and "额度" in reason

    def test_rule_3_no_budget_constraint_when_none(self):
        ok, _ = wc.validate_word_adjustment_result(
            current_words=1000, new_words=5000, mode=wc.MODE_EXPAND, target_words=9000,
        )
        assert ok is True

    def test_rule_4_gap_must_strictly_shrink(self):
        """防震荡：与目标的差距必须严格缩小，等距或倒退一律拒绝。"""
        ok, reason = wc.validate_word_adjustment_result(
            current_words=1000, new_words=1401, mode=wc.MODE_EXPAND, target_words=1200,
        )
        assert ok is False and "未缩小" in reason
        ok2, _ = wc.validate_word_adjustment_result(
            current_words=1000, new_words=1400, mode=wc.MODE_EXPAND, target_words=1200,
        )
        assert ok2 is False

    def test_rule_5_section_bounds_enforced(self):
        ok, reason = wc.validate_word_adjustment_result(
            current_words=1000, new_words=1100, mode=wc.MODE_EXPAND, target_words=1200,
            section_bounds=(240, 360), enforce_section_bounds=True,
        )
        assert ok is False and "强控区间" in reason

    def test_rule_5_section_bounds_passed(self):
        ok, _ = wc.validate_word_adjustment_result(
            current_words=200, new_words=300, mode=wc.MODE_EXPAND, target_words=300,
            section_bounds=(240, 360), enforce_section_bounds=True,
        )
        assert ok is True

    def test_rule_5_skipped_when_not_enforced(self):
        ok, _ = wc.validate_word_adjustment_result(
            current_words=1000, new_words=1500, mode=wc.MODE_EXPAND, target_words=2000,
            section_bounds=(240, 360), enforce_section_bounds=False,
        )
        assert ok is True

    def test_rule_6_total_upper_bound(self):
        ok, reason = wc.validate_word_adjustment_result(
            current_words=1000, new_words=1100, mode=wc.MODE_EXPAND,
            total_words=5000, maximum_words=5000, enforce_total_bounds=True,
        )
        assert ok is False and "上限" in reason

    def test_rule_6_total_lower_bound(self):
        ok, reason = wc.validate_word_adjustment_result(
            current_words=1000, new_words=900, mode=wc.MODE_SHRINK,
            total_words=1050, minimum_words=1000, enforce_total_bounds=True,
        )
        assert ok is False and "下限" in reason

    def test_rule_6_total_bounds_passed(self):
        ok, _ = wc.validate_word_adjustment_result(
            current_words=1000, new_words=1100, mode=wc.MODE_EXPAND,
            total_words=4000, minimum_words=1000, maximum_words=5000,
            enforce_total_bounds=True,
        )
        assert ok is True


# =============================================================================
# R12 —— 控制失败的兜底警告（不硬裁，交人工裁决）
# =============================================================================


class TestR12Warnings:
    def test_all_on_target_returns_none(self):
        """全部达标时返回 None（前端不弹窗）。"""
        opts = wc.WordControlOptions(1000, 5000, 1000, True)  # 区间 [800, 1200]
        assert wc.build_word_control_warning(
            total_words=3000, options=opts, sections=_secs([(1, 1000), (2, 1000), (3, 1000)]),
        ) is None

    def test_total_above_maximum_warns(self):
        opts = wc.WordControlOptions(1000, 3000, 1000, True)
        warn = wc.build_word_control_warning(
            total_words=4100, options=opts, sections=_secs([(1, 1100), (2, 1500), (3, 1500)]),
        )
        assert warn is not None
        assert warn["kind"] == "content"
        assert warn["message"] == wc.CONTENT_WORD_CONTROL_WARNING
        assert warn["total"]["status"] == "above"
        assert warn["total"]["expected"] == [1000, 3000]
        assert warn["total"]["actual"] == 4100

    def test_total_below_minimum_warns(self):
        opts = wc.WordControlOptions(3000, 0, 0, False)
        warn = wc.build_word_control_warning(
            total_words=100, options=opts, sections=_secs([(1, 100)]),
        )
        assert warn is not None and warn["total"]["status"] == "below"

    def test_section_out_of_range_is_listed_per_item(self):
        """逐节列出越界小节的 id、标题、实际字数（易标弹窗同款明细）。"""
        opts = wc.WordControlOptions(0, 0, 1000, True)  # 区间 [800, 1200]
        warn = wc.build_word_control_warning(
            total_words=1880, options=opts, sections=_secs([(1, 380), (2, 1500)]),
        )
        assert warn is not None
        assert warn["sections"]["out_of_range_count"] == 2
        items = {i["id"]: i for i in warn["sections"]["items"]}
        assert items[1]["status"] == "below" and items[1]["words"] == 380
        assert items[2]["status"] == "above" and items[2]["title"] == "第2节"
        assert warn["sections"]["range"] == [800, 1200]

    def test_single_section_uses_section_message(self):
        opts = wc.WordControlOptions(0, 0, 300, True)
        warn = wc.build_word_control_warning(
            single_section=True, total_words=500, options=opts, sections=_secs([(1, 500)]),
        )
        assert warn["message"] == wc.SECTION_WORD_CONTROL_WARNING
        assert warn["single_section"] is True

    def test_non_strict_never_flags_sections(self):
        """非强控时不做单节越界判定（3000 只是分配依据，不是上限）。"""
        opts = wc.WordControlOptions(0, 0, 300, False)
        assert wc.build_word_control_warning(
            total_words=9999, options=opts, sections=_secs([(1, 9999)]),
        ) is None

    def test_warning_uses_provided_total_over_sum(self):
        opts = wc.WordControlOptions(0, 100, 0, False)
        warn = wc.build_word_control_warning(
            total_words=500, options=opts, sections=_secs([(1, 1)]),
        )
        assert warn["total"]["actual"] == 500

    def test_warning_without_config_is_none(self):
        """完全没有字数配置 → 不做任何判定。"""
        assert wc.build_word_control_warning(total_words=9999, sections=_secs([(1, 9999)])) is None

    def test_leaf_count_warning_inside_tolerance_is_none(self):
        """落在二审容差 ±2 内不算问题。"""
        assert wc.build_leaf_count_warning(expected_leaf_count=12, actual_leaf_count=13) is None

    def test_leaf_count_warning_outside_tolerance(self):
        warn = wc.build_leaf_count_warning(expected_leaf_count=12, actual_leaf_count=17)
        assert warn["kind"] == "leaf-count"
        assert warn["expected"] == 12 and warn["actual"] == 17
        assert warn["review_bounds"] == [10, 14]
        assert "12" in warn["message"] and "17" in warn["message"]

    def test_leaf_count_warning_custom_review_bounds(self):
        assert wc.build_leaf_count_warning(
            expected_leaf_count=12, actual_leaf_count=17, review_bounds=(5, 20),
        ) is None


# =============================================================================
# R13 / R14 / R15 —— 内容保真条款、配置快照、可研报告下限
# =============================================================================


class TestR13ContentPreservation:
    def test_clause_present_and_mentions_must_keep_items(self):
        """R13 —— 字数服从内容保真：条款必须点明不得删除的关键要素。"""
        clause = wc.SECTION_WORD_CONTENT_PRESERVATION_CLAUSE
        assert "字数要求不能覆盖" in clause
        for token in ("技术路线", "参数", "周期", "人员", "验收", "售后"):
            assert token in clause

    def test_warning_messages_match_yibiao_verbatim(self):
        """失败文案与易标源码逐字一致（用户已形成认知，不允许改动）。"""
        assert wc.CONTENT_WORD_CONTROL_WARNING == "经多轮修复，字数仍未达预期，请您人工核对"
        assert wc.SECTION_WORD_CONTROL_WARNING == "字数未达预期，请您人工核对"


class TestR14Snapshot:
    def test_derive_snapshot_bounds_strict(self):
        snap = wc.derive_snapshot_bounds(
            {"minimum_words": 20000, "maximum_words": 40000,
             "section_words": 3000, "strict_section_words": True},
        )
        assert snap["minimum_words"] == 20000 and snap["maximum_words"] == 40000
        assert snap["section_words"] == 3000
        assert snap["strict_section_words"] is True
        assert snap["section_minimum_words"] == 2400
        assert snap["section_maximum_words"] == 3600

    def test_derive_snapshot_bounds_non_strict_gives_zero_bounds(self):
        snap = wc.derive_snapshot_bounds({"section_words": 3000, "strict_section_words": False})
        assert snap["section_minimum_words"] == 0 and snap["section_maximum_words"] == 0

    def test_derive_snapshot_bounds_empty_source(self):
        assert wc.derive_snapshot_bounds(None) == {
            "minimum_words": 0, "maximum_words": 0, "section_words": 0,
            "section_minimum_words": 0, "section_maximum_words": 0,
            "strict_section_words": False,
        }

    def test_strict_forced_off_without_section_words(self):
        snap = wc.derive_snapshot_bounds({"strict_section_words": True})
        assert snap["strict_section_words"] is False
        assert snap["section_minimum_words"] == 0

    @pytest.mark.parametrize(
        "snapshot, expected",
        [
            ({"section_words": 3000}, True),
            ('{"section_words": 3000}', True),
            ({}, False),
            (None, False),
            ("", False),
            ("null", False),
            ("{}", False),
            (0, False),
            (123, False),
        ],
    )
    def test_has_valid_snapshot(self, snapshot, expected):
        """R14 硬门槛：无快照禁止生成正文。"""
        assert wc.has_valid_snapshot(snapshot) is expected

    def test_normalize_outline_alias_matches_normalize(self):
        src = {"minimumWords": 10000, "maximumWords": 20000,
               "sectionWords": 3000, "strictSectionWords": True}
        assert wc.normalize_outline_word_control_options(src) == wc.normalize_word_control_options(src)


class TestR15FeasibilityTarget:
    def test_default_when_empty(self):
        assert wc.normalize_target_words(None) == 30000
        assert wc.normalize_target_words("") == 30000
        assert wc.normalize_target_words(0) == 30000
        assert wc.normalize_target_words("abc") == 30000

    def test_hard_floor_at_1000(self):
        assert wc.normalize_target_words(500) == 1000
        assert wc.normalize_target_words(1) == 1000

    def test_valid_value_passed_through(self):
        assert wc.normalize_target_words(30000) == 30000
        assert wc.normalize_target_words("50000") == 50000
        assert wc.normalize_target_words(12345.9) == 12345

    def test_custom_default(self):
        assert wc.normalize_target_words(None, default=8000) == 8000

    def test_constants(self):
        assert wc.FEASIBILITY_MIN_TARGET_WORDS == 1000
        assert wc.FEASIBILITY_DEFAULT_TARGET_WORDS == 30000


class TestExportSurface:
    """__all__ 完整性：防止后续新增函数忘记导出导致调用方踩坑。"""

    def test_all_names_resolvable(self):
        missing = [name for name in wc.__all__ if not hasattr(wc, name)]
        assert missing == []

    def test_key_rule_constants_exported(self):
        for name in (
            "DEFAULT_SECTION_WORDS",
            "STRICT_SECTION_TOLERANCE",
            "GENERATION_WORD_TARGET_RATIO",
            "OUTLINE_REVIEW_LEAF_TOLERANCE",
            "MAX_WORD_ADJUSTMENT_ROUNDS",
            "TOTAL_WORD_ADJUSTMENT_BATCH_SIZE",
            "WORD_COUNT_INPUT_UNIT",
        ):
            assert name in wc.__all__

    def test_readable_word_count_reexported(self):
        """R1 权威口径必须从 word_control 可导入（单一口径）。"""
        from app.services.word_control import count_readable_words as reexported

        assert reexported("中文字数 hello") == 5

    def test_ratio_constants(self):
        assert wc.STRICT_SECTION_TOLERANCE == 0.20
        assert wc.GRANULARITY_GAP_THRESHOLD == 0.20
        assert wc.TOTAL_WORD_SHRINK_SECTION_RATIO == 0.25
        assert wc.TOTAL_WORD_SHRINK_MIN_CAPACITY_RATIO == 0.30
        assert wc.ESTIMATED_WORDS_PER_PAGE == 650
        assert wc.WORD_COUNT_INPUT_UNIT == 10000

        assert count_readable_words("甲\r\n乙\r丙") == 3
