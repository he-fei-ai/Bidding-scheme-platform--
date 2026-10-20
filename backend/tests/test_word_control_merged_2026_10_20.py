"""字数控制体系 A/B 合并回归（2026-10-20 第三轮）。

验收标准（计划《字数控制体系 A/B 合并》测试计划）：

1. ``strict_override`` 命中 → 区间等于 ``derive_strict_bounds``、
   source=``word_control_strict``、generation_anchor=R8 初稿倒推目标；
2. strict 与单节 ``target_words`` 并存 → strict 短路生效（体系 B 优先）；
3. 无 override → 原体系 A 五级优先级逐值不变（防回归）；
4. 同一章节在「生成提示词 / 压缩阈值 / 校验页 / 兜底扩写」四处经统一解析器
   输出逐值相等（合并的终极目标：5 处双分支收敛为一条链）；
5. 装配层 ``build_section_override`` 产物含 ``tolerance`` / ``source`` 自描述键，
   GUARD（R8 倒推超 R5 上限时回落 S）逐值不变。

全部为纯函数 / 提示词构造断言，不触库、不调 AI。
"""
from __future__ import annotations

from app.routers import word_count as wc_router
from app.services import word_control as wc
from app.services import word_control_runtime as wcr
from app.services.ai.workflows_expand import section_expand_threshold
from app.services.ai.workflows_sections import build_chapter_content_messages
from app.services.word_target_resolver import (
    SOURCE_L3_OWN,
    SOURCE_SECTION_TARGET_WORDS,
    SOURCE_SECTION_TIER,
    SOURCE_STRICT,
    resolve_section_word_target,
    resolve_section_word_target_from_mapping,
)


class _Sec:
    """最小章节桩：只暴露校验链 / 装配层用到的属性。"""

    def __init__(self, sid, level, pid=None, target_words=None, tier=None):
        self.id = sid
        self.level = level
        self.parent_id = pid
        self.target_words = target_words
        self.word_count_tier = tier


def _strict_state(section_words: int, maximum_words: int = 0, minimum_words: int = 0):
    """构造一个「强控生效」的项目状态（携带有效快照，R14）。"""
    snapshot = {
        "section_words": section_words,
        "strict_section_words": True,
        "maximum_words": maximum_words,
        "minimum_words": minimum_words,
    }
    opts = wc.normalize_word_control_options(snapshot)
    return wcr.ProjectWordControl(1, opts, snapshot, None)


# --------------------------------------------------------------------------
# 1. strict_override 命中：短路返回体系 B R5 区间 + R8 锚点
# --------------------------------------------------------------------------
def test_strict_override_hit_returns_r5_bounds():
    S = 1500
    lo, hi = wc.derive_strict_bounds(S)  # (1200, 1800)
    override = {
        "section_words": S, "lo": lo, "hi": hi,
        "generation_target": 1600, "strict": True,
        "tolerance": wc.STRICT_SECTION_TOLERANCE, "source": SOURCE_STRICT,
    }
    r = resolve_section_word_target(
        level=3, target_words=None, strict_override=override,
    )
    assert r is not None
    assert r.source == SOURCE_STRICT
    assert r.target == S
    assert (r.lo, r.hi) == (lo, hi) == (1200, 1800)
    # tolerance 不信任入参，恒取权威常量（公式零重复）
    assert r.tolerance == wc.STRICT_SECTION_TOLERANCE == 0.20
    # R8 初稿倒推目标作为写作锚点附带返回
    assert r.generation_anchor == 1600
    assert r.label == "强控快照（易标 R5）"


def test_strict_generation_anchor_defaults_zero():
    # 未设全文上限 → generation_target=0 → 锚点回落 0（提示词侧用 target 兜底）
    override = {"section_words": 1500, "lo": 1200, "hi": 1800, "generation_target": 0}
    r = resolve_section_word_target(level=3, strict_override=override)
    assert r.generation_anchor == 0
    assert r.target == 1500


# --------------------------------------------------------------------------
# 2. strict 与单节 target_words 并存 → strict 短路（体系 B 优先于体系 A）
# --------------------------------------------------------------------------
def test_strict_short_circuits_section_target_words():
    override = {"section_words": 1500, "lo": 1200, "hi": 1800, "generation_target": 0}
    r = resolve_section_word_target(
        level=3, target_words=3500, word_count_tier="detailed", strict_override=override,
    )
    assert r.source == SOURCE_STRICT
    assert r.target == 1500  # 而非单节 3500，也不是档位 2000
    assert (r.lo, r.hi) == (1200, 1800)


def test_invalid_override_falls_back_to_tier_chain():
    # 非法覆盖（section_words<=0 / hi<=0）视为未命中，回落体系 A
    for bad in (None, {}, {"section_words": 0, "lo": 0, "hi": 0},
                {"section_words": 1500, "lo": 1200, "hi": 0}):
        r = resolve_section_word_target(
            level=3, target_words=3500, strict_override=bad,
        )
        assert r.source == SOURCE_SECTION_TARGET_WORDS
        assert r.target == 3500


# --------------------------------------------------------------------------
# 3. 无 override → 原体系 A 五级优先级逐值不变（防回归）
# --------------------------------------------------------------------------
def test_five_level_priority_unchanged():
    # L1/L2 不参与控制
    assert resolve_section_word_target(level=1, target_words=3500) is None
    assert resolve_section_word_target(level=2, target_words=3500) is None
    # ① 单节 target_words 精确覆盖（±10%）
    r = resolve_section_word_target(level=3, target_words=3500)
    assert (r.source, r.target, r.lo, r.hi, r.tolerance) == (
        SOURCE_SECTION_TARGET_WORDS, 3500, 3150, 3850, 0.10,
    )
    # ② 单节档位覆盖
    r = resolve_section_word_target(
        level=3, target_words=None, word_count_tier="detailed", is_l3_own=False,
    )
    assert r.source == SOURCE_SECTION_TIER
    assert r.target == 2000
    # ③ L3 自身正文 300±20%
    r = resolve_section_word_target(level=3, is_l3_own=True)
    assert r.source == SOURCE_L3_OWN
    assert (r.target, r.lo, r.hi, r.tolerance) == (300, 240, 360, 0.20)
    # ④ 全局 writing_length 档位（含下级的 L3 走四档子树）
    r = resolve_section_word_target(level=3, is_l3_own=False, writing_length="detailed")
    assert r.target == 2000


def test_from_mapping_strict_override_short_circuits():
    sec = _Sec(7, 3, 2, target_words=3500)
    override = {"section_words": 1500, "lo": 1200, "hi": 1800, "generation_target": 0}
    r = resolve_section_word_target_from_mapping(sec, writing_length="standard", strict_override=override)
    assert r.source == SOURCE_STRICT and r.target == 1500


# --------------------------------------------------------------------------
# 4. 跨消费者逐值相等（合并终极目标：四处同走一条链）
# --------------------------------------------------------------------------
def test_cross_consumer_parity_strict():
    S = 1500
    state = _strict_state(S)
    sec = _Sec(3, 3, 2)
    override_table = wcr.build_section_override(state, [sec])
    assert sec.id in override_table
    ov = override_table[sec.id]
    lo, hi = wc.derive_strict_bounds(S)
    assert (ov["lo"], ov["hi"]) == (lo, hi) == (1200, 1800)

    # (a) 统一解析器基准
    base = resolve_section_word_target_from_mapping(sec, writing_length="detailed", strict_override=ov)
    assert (base.target, base.lo, base.hi, base.source) == (S, lo, hi, SOURCE_STRICT)

    # (b) 校验页区间（透传 override 表，经统一解析器）
    v_lo, v_hi = wc_router._target_bounds(
        sec, writing_length="detailed", override=override_table,
    )
    assert (v_lo, v_hi) == (base.lo, base.hi)
    assert wc_router._resolve_target_for_section(sec, "detailed", override=override_table) == base.target

    # (c) 兜底扩写达标线 = 目标 × 80%（恰为 R5 下界）
    flat_min = 3000
    thr = section_expand_threshold(
        {"level": 3, "target_words": None, "word_count_tier": None},
        flat_min, writing_length="detailed", strict_override=ov,
    )
    assert thr == max(200, min(flat_min, int(S * 0.8))) == lo

    # (d) 生成提示词：锚点（无 R8 时=S）+ 硬性区间 R5
    msgs = build_chapter_content_messages(
        {"id": sec.id, "level": 3, "title": "测试小节", "summary": "",
         "content": "", "is_l3_own": True, "target_words": None},
        facts=[], scoring_context="", project_scope="市政",
        word_control=override_table,
    )
    user_prompt = "\n".join(m.content for m in msgs if m.role == "user")
    assert f"约 {S} 字左右（硬性上限 {hi} 字，最低 {lo} 字）" in user_prompt


def test_cross_consumer_parity_tier_no_override():
    """无强控时四处同样一致（回落体系 A，防合并破坏档位链）。"""
    sec = _Sec(5, 3, 2)  # L3 自身叶子
    base = resolve_section_word_target_from_mapping(
        sec, writing_length="detailed", is_l3_own=True,
    )
    assert (base.target, base.lo, base.hi) == (300, 240, 360)
    v_lo, v_hi = wc_router._target_bounds(sec, writing_length="detailed")
    assert (v_lo, v_hi) == (240, 360)


# --------------------------------------------------------------------------
# 5. 装配层：产物含自描述键 + GUARD 回落
# --------------------------------------------------------------------------
def test_build_section_override_self_describing_keys():
    state = _strict_state(1500)
    sec = _Sec(3, 3, 2)
    ov = wcr.build_section_override(state, [sec])[sec.id]
    assert ov["tolerance"] == wc.STRICT_SECTION_TOLERANCE
    assert ov["source"] == SOURCE_STRICT
    # 既有四键不变
    assert ov["section_words"] == 1500
    assert (ov["lo"], ov["hi"]) == (1200, 1800)
    assert ov["strict"] is True


def test_build_section_override_guard_clamps_to_S():
    # max 远宽松于单节上限：R8 倒推 floor(0.8×max/叶子数) 会超过 R5 hi，
    # GUARD 回落标称 S（避免「目标 80000，硬性上限 1800」的矛盾提示词）。
    S = 1500
    state = _strict_state(S, maximum_words=100000)
    sec = _Sec(3, 3, 2)
    ov = wcr.build_section_override(state, [sec])[sec.id]
    assert ov["generation_target"] == S  # 被钳制回 1500，而非 80000


def test_build_section_override_r8_anchor_within_bounds():
    # max 合理收紧：单叶子时 R8 倒推 floor(0.8×max/1)=1600 落在 [lo,hi]=[1200,1800]
    # 内，GUARD 不触发 → 锚点为倒推值（resolver 以 generation_anchor 附带返回）。
    S = 1500
    state = _strict_state(S, maximum_words=2000)
    sec = _Sec(3, 3, 2)
    ov = wcr.build_section_override(state, [sec])[sec.id]
    assert ov["generation_target"] == 1600
    assert ov["lo"] <= ov["generation_target"] <= ov["hi"]
    # 统一解析器把倒推锚点透传为 generation_anchor
    r = resolve_section_word_target_from_mapping(sec, strict_override=ov)
    assert r.generation_anchor == 1600 and r.source == SOURCE_STRICT
