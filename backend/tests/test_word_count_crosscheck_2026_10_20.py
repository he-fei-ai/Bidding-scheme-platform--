"""字数控制口径跨链一致性回归（2026-10-20 P0/P1 修复的常备用例）。

覆盖报告《正文生成模块与文字生成数量控制规则深度探索_2026-10-20》第 4 节确认的
口径分叉修复，把当时的临时探针固化为回归断言，验收标准：
**同一章节树在「生成 / 校验 / 面板 / 兜底扩写」四处目标与区间逐值相等**。

- D1：含下级的 L3 走四档「子树总字数」，不再被校验侧误判为 L3 自身正文 240~360；
- D2：校验判定与压缩全程统一用易标 R1 可读字数（count_readable_words）；
- D3：强控项目校验区间读装配层 build_section_override 的 R5 区间；
- D5：兜底扩写达标线走唯一权威解析器（L3 自身叶子 = 300 而非全局档位）。

全部为纯函数断言，不触库、不调 AI。
"""
from __future__ import annotations

from app.routers import word_count as wc
from app.services.ai.writing_length_config import level_target
from app.services.word_target_resolver import (
    resolve_section_word_target_from_mapping,
)
from app.utils import count_readable_words, count_words


class _Sec:
    """最小章节桩：只暴露 word_count 校验链用到的属性。"""

    def __init__(self, sid, level, pid=None, target_words=None, tier=None, content=""):
        self.id = sid
        self.level = level
        self.parent_id = pid
        self.target_words = target_words
        self.word_count_tier = tier
        self.content = content
        self.word_count = count_words(content)


# 章节树：L1(1) → L2(2) → L3 含下级(3) → L4 叶子(4)；L3 自身叶子(5)
_TREE = [
    _Sec(1, 1, None),
    _Sec(2, 2, 1),
    _Sec(3, 3, 2),
    _Sec(4, 4, 3, content="施工部署" * 300),
    _Sec(5, 3, 2, content="质量管理" * 75),
]
_BY_ID = {s.id: s for s in _TREE}
_CHILDREN = wc._build_children_ids(_TREE)


# --------------------------------------------------------------------------
# D1：含下级的 L3 校验目标 = 四档子树总字数，与生成侧对称
# --------------------------------------------------------------------------
def test_children_ids_includes_parent_l3():
    assert _CHILDREN == {1, 2, 3}


def test_is_l3_own_requires_no_children():
    l3_with_children = _BY_ID[3]
    l3_leaf = _BY_ID[5]
    # 不传 children_ids：保持旧多数派口径（视为自身正文）
    assert wc._is_l3_own_section(l3_with_children) is True
    # 传 children_ids：含下级的 L3 不再是「自身正文」
    assert wc._is_l3_own_section(l3_with_children, _CHILDREN) is False
    assert wc._is_l3_own_section(l3_leaf, _CHILDREN) is True


def test_resolve_target_l3_with_children_follows_global_tier():
    # 含下级的 L3 → detailed 全局档位 2000（修复前误判 300）
    assert wc._resolve_target_for_section(_BY_ID[3], "detailed", children_ids=_CHILDREN) == 2000
    # L3 自身叶子 → 300
    assert wc._resolve_target_for_section(_BY_ID[5], "detailed", children_ids=_CHILDREN) == 300


def test_target_bounds_l3_with_children_not_240_360():
    lo, hi = wc._target_bounds(_BY_ID[3], writing_length="detailed", children_ids=_CHILDREN)
    assert (lo, hi) == (1800, 2200)  # 2000 ± 10%，而非 L3 自身正文 (240, 360)
    lo, hi = wc._target_bounds(_BY_ID[5], writing_length="detailed", children_ids=_CHILDREN)
    assert (lo, hi) == (240, 360)


def test_validation_parity_with_generator():
    """校验侧与权威解析器对同一 L3 逐值相等（生成侧亦走同一解析器）。"""
    gen = resolve_section_word_target_from_mapping(
        {"level": 3, "target_words": None, "word_count_tier": None},
        writing_length="detailed",
        is_l3_own=False,  # 生成侧对含下级的 L3 传 False
    )
    val = resolve_section_word_target_from_mapping(
        _BY_ID[3], writing_length="detailed", is_l3_own=wc._is_l3_own_section(_BY_ID[3], _CHILDREN)
    )
    assert gen.target == val.target == 2000
    assert (gen.lo, gen.hi) == (val.lo, val.hi)


# --------------------------------------------------------------------------
# D2：校验判定统一使用 R1 可读字数（含表格/URL 时原始字数膨胀）
# --------------------------------------------------------------------------
def test_section_actual_words_uses_readable_not_raw():
    mixed = _Sec(9, 3, 2, content="部署方案要点说明" * 50 + "\n| 项 | 值 |\n|---|---|\n| a | b | https://x.com/y\n")
    assert mixed.content  # 有正文
    # readable 口径必须等于 count_readable_words，且严格小于原始 count_words
    assert wc._section_actual_words(mixed) == count_readable_words(mixed.content)
    assert count_readable_words(mixed.content) < count_words(mixed.content)


def test_section_actual_words_empty_content_falls_back_to_word_count():
    # 兼容仅设 word_count、content 为空的旧测试桩（生产环境 word_count 恒由 content 派生）
    stub = _Sec(10, 3, 2, content="")
    stub.word_count = 300
    assert wc._section_actual_words(stub) == 300


def test_is_above_target_uses_readable():
    # L3 自身叶子 300±20% → hi=360；可读 400 但原始更大 → 应判超长
    over = _Sec(11, 3, 2, content="文" * 400)
    assert wc._is_above_target(over, writing_length="standard", children_ids=_CHILDREN) is True


# --------------------------------------------------------------------------
# D3：强控覆盖区间优先，读 build_section_override 的 R5 区间
# --------------------------------------------------------------------------
def test_target_bounds_strong_override():
    override = {3: {"lo": 1200, "hi": 1800, "section_words": 1500}}
    lo, hi = wc._target_bounds(
        _BY_ID[3], writing_length="detailed", children_ids=_CHILDREN, override=override
    )
    assert (lo, hi) == (1200, 1800)  # R5 区间覆盖档位区间 (1800, 2200)


def test_target_bounds_override_only_for_hit_sections():
    override = {3: {"lo": 1200, "hi": 1800, "section_words": 1500}}
    # 未在覆盖表内的 L3 叶子仍走档位口径
    lo, hi = wc._target_bounds(
        _BY_ID[5], writing_length="detailed", children_ids=_CHILDREN, override=override
    )
    assert (lo, hi) == (240, 360)


# --------------------------------------------------------------------------
# D5：兜底扩写达标线走权威解析器（L3 自身叶子 = 300，而非全局档位）
# --------------------------------------------------------------------------
def test_fallback_expand_target_l3_leaf_is_own_not_global():
    l3_leaf = _BY_ID[5]
    # 生产环境 children_map 为 dict；本用例用 _build_children_ids 返回的 set，
    # 叶节点 id 不在集合中 → 无下级 → is_l3_own=True（与 sse_content 兜底扩写同逻辑）。
    is_own = l3_leaf.id not in _CHILDREN
    resolved = resolve_section_word_target_from_mapping(
        l3_leaf, writing_length="detailed", is_l3_own=is_own
    )
    # 修复前用 level_target(detailed, 3) 会得到 2000；修复后走解析器得 L3 自身 300
    assert int(resolved.target) == 300
    assert level_target("detailed", 3, 300) == 2000  # 旧口径（仅对照，说明差异确曾存在）
