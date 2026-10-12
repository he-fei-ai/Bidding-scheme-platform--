"""CONVERGE 2026-10-12 —— 目标字数「唯一权威口径」收敛测试。

背景：本平台曾有 **5 份**「有效目标字数」同构实现（sse_utils /
word_count / workflows_sections 提示词 / workflows_sections 压缩 /
workflows_expand 扩写阈值），历史上两次因顺序不一致产生「生成按 detailed 写、
校验按 standard 判」的系统性误报。本次统一到
``app/services/word_target_resolver.py``，本测试固定：

1. 权威解析器的优先级与容差随来源走；
2. 5 个历史消费点与权威解析器**逐项同口径**（防再次分叉）；
3. 收敛过程中保留的两处兼容入口仍可用；
4. 收敛带来的**唯一行为修正**（L3 自身正文 300±20% 优先于全局档位）。
"""

import pytest

from app.services.word_target_resolver import (
    DEFAULT_SECTION_TOLERANCE,
    SOURCE_L3_OWN,
    SOURCE_SECTION_TARGET_WORDS,
    SOURCE_SECTION_TIER,
    SOURCE_WRITING_LENGTH_TIER,
    resolve_section_word_target,
    resolve_section_word_target_from_mapping,
)


def _t(**kw):
    return resolve_section_word_target(**kw)


class TestPriority:
    def test_l1_l2_not_controlled(self):
        assert _t(level=1) is None
        assert _t(level=2) is None

    def test_target_words_overrides_everything(self):
        r = _t(level=3, target_words=888, word_count_tier="detailed", writing_length="long")
        assert r.target == 888
        assert r.source == SOURCE_SECTION_TARGET_WORDS
        assert r.tolerance == DEFAULT_SECTION_TOLERANCE
        assert (r.lo, r.hi) == (799, 976)  # 888 ±10%（int 截断）

    def test_section_tier_overrides_writing_length(self):
        r = _t(level=3, word_count_tier="detailed", writing_length="brief")
        assert (r.target, r.source) == (2000, SOURCE_SECTION_TIER)
        assert (r.lo, r.hi) == (1800, 2200)

    def test_l3_own_beats_writing_length(self):
        """L3 自身正文 300±20% 优先于全局档位（多数派口径，2026-10-12 修正）。"""
        r = _t(level=3, writing_length="detailed")
        assert r.target == 300
        assert r.source == SOURCE_L3_OWN
        assert (r.lo, r.hi) == (240, 360)
        assert r.tolerance == pytest.approx(0.20)

    def test_l3_with_children_uses_tier(self):
        """显式声明 is_l3_own=False 的 L3 走四档「子树总字数」口径。"""
        r = _t(level=3, writing_length="detailed", is_l3_own=False)
        assert r.target == 2000
        assert r.source == SOURCE_WRITING_LENGTH_TIER

    def test_l4_falls_back_to_writing_length_then_default(self):
        r = _t(level=4, writing_length="long")
        assert (r.target, r.source) == (2500, SOURCE_WRITING_LENGTH_TIER)
        r2 = _t(level=4)
        assert r2.target == 1200  # 默认档位 standard

    def test_target_words_accepts_numeric_string(self):
        r = _t(level=3, target_words="1200")
        assert r.target == 1200

    def test_invalid_level_falls_back_to_l3(self):
        assert _t(level="abc").target == 300


class TestMappingAdapter:
    def test_reads_dict(self):
        r = resolve_section_word_target_from_mapping(
            {"level": 3, "target_words": 500, "word_count_tier": "brief"}
        )
        assert r.target == 500

    def test_reads_orm_like_object(self):
        class _S:
            level = 3
            target_words = None
            word_count_tier = "long"

        assert resolve_section_word_target_from_mapping(_S()).target == 2500

    def test_none_section_returns_none(self):
        assert resolve_section_word_target_from_mapping(None) is None


class TestConsumerParity:
    """5 个历史消费点必须与权威解析器逐项一致。"""

    def test_sse_utils_matches_resolver(self):
        from app.routers.sse import sse_utils

        cases = [
            {"level": 3, "target_words": 700},
            {"level": 3, "word_count_tier": "detailed"},
            {"level": 3},
            {"level": 4, "writing_length": None},
            {"level": 1},
        ]
        for sec in cases:
            for wl in (None, "detailed"):
                expected = _t(
                    level=sec.get("level", 3),
                    target_words=sec.get("target_words"),
                    word_count_tier=sec.get("word_count_tier"),
                    writing_length=wl,
                )
                got = sse_utils._get_effective_target_words(dict(sec), writing_length=wl)
                assert got == (expected.target if expected else None), (sec, wl, got)

    def test_word_count_router_matches_resolver(self):
        from app.routers import word_count as wcr

        class _S:
            def __init__(self, level=3, tw=None, tier=None):
                self.level = level
                self.target_words = tw
                self.word_count_tier = tier

        cases = [
            _S(3, 700, None),
            _S(3, None, "brief"),
            _S(3, None, None),
            _S(4, None, None),
            _S(2),
        ]
        for s in cases:
            for wl in (None, "long"):
                expected = _t(
                    level=s.level, target_words=s.target_words,
                    word_count_tier=s.word_count_tier, writing_length=wl,
                )
                assert wcr._resolve_target_for_section(s, wl) == (
                    expected.target if expected else None
                )
            # _target_bounds 历史签名不带 writing_length（审计端点无该上下文），
            # 断言它与「无全局档位」的权威解析结果一致。
            expected = _t(
                level=s.level, target_words=s.target_words,
                word_count_tier=s.word_count_tier,
            )
            bounds = wcr._target_bounds(s)
            assert bounds == ((expected.lo, expected.hi) if expected else (0, 0))

    def test_expand_threshold_uses_resolver_target(self):
        from app.services.ai.workflows_expand import section_expand_threshold

        # L3 自身正文 300 → 达标线 240
        assert section_expand_threshold({"level": 3}, 2000) == 240
        # CONVERGE 行为修正：全局档位不再越过 L3 自身正文口径（此前为 1600）
        assert section_expand_threshold({"level": 3}, 2000, writing_length="detailed") == 240
        # L4+ 仍走全局档位 → 2000×0.8=1600
        assert section_expand_threshold({"level": 4}, 2000, writing_length="detailed") == 1600
        # 单节覆盖优先
        assert section_expand_threshold({"level": 3, "target_words": 1000}, 2000) == 800
        # L1/L2 不参与
        assert section_expand_threshold({"level": 2}, 2000) == 0

    def test_condense_threshold_matches_resolver(self):
        """压缩侧（L5 消费点）：target×1.15 的触发线必须由权威 target 推出。"""
        from app.services import word_control as wc

        r = _t(level=3)
        assert int(r.target * 1.15) == 345  # 300×1.15
        assert wc.Granularity.PARAGRAPH  # 常量可达（冒烟）

    def test_compat_entrypoints_preserved(self):
        """收敛不得破坏历史符号（仍有测试/调用方引用）。"""
        from app.routers import word_count as wcr

        class _S:
            level = 3
            target_words = None
            word_count_tier = None

        assert callable(wcr._tolerance_for_section)
        assert callable(wcr._resolve_word_target_for_section)
        assert wcr._tolerance_for_section_via_resolver(_S()) == pytest.approx(0.20)
        assert wcr._tolerance_for_section(_S()) == pytest.approx(0.20)
