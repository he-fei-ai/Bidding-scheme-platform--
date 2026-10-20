"""章节目标字数 / 达标区间的**单一权威解析口径**（CONVERGE 2026-10-12）。

背景
----
本平台历史上「有效目标字数」被**四份同构实现**各写一遍：

| # | 位置 | 用途 |
| --- | --- | --- |
| 1 | ``routers/sse/sse_utils.py::_get_effective_target_words`` | 正文生成 SSE 编排、落库统计 |
| 2 | ``routers/word_count.py::_resolve_target_for_section`` | 字数统计 / 审核校验页 |
| 3 | ``services/ai/workflows_sections.py::build_chapter_content_messages`` | 生成提示词字数指令 |
| 4 | ``services/ai/workflows_expand.py::section_expand_threshold`` | 兜底扩写达标线 |
| （5） | ``services/ai/workflows_sections.py::condense_overlong_results`` | 压缩触发阈值 |

历史故障正是由此而来：2026-10-09 修过一次「生成按 detailed 写、校验按 standard 判」
的分叉，2026-10-12 又在第 5 处发现第 4 处对 L3 的顺序不一致（扩写把 L3 自身正文
按 300 字口径判，兜底扩写却按全局档位算阈值）。**每新增一个消费者就多一次分叉
机会**，因此本次把公式收敛到本模块，5 处全部改为调用它。

统一优先级（合并后的唯一公式，体系 A/B 单一入口）
------------------------------------------------
本平台历史上存在**两套并列、互不覆盖**的字数控制体系：

| 体系 | 口径 | 规则来源 | 历史入口 |
| --- | --- | --- | --- |
| A 档位（相对） | L3 自身 300±20% / 四档±10% / 单节覆盖 | ``word_count_tiers`` 表 + ``writing_length_config`` 常量 | 本模块五级公式 |
| B 易标（绝对强控） | R5 ``[ceil(0.8S), floor(1.2S)]`` + R8 初稿倒推 | ``services/word_control.py`` 纯函数（零 DB/零 AI） | ``word_control_runtime.build_section_override`` 覆盖表 |

2026-10-20 体系合并（第三轮）：体系 B 的强控覆盖作为**最高优先级项**
（``strict_override``）收编进本模块统一公式，全部消费者（提示词侧、压缩侧、
SSE 侧、校验页侧、兜底扩写侧、R12 回写侧）收敛为一次调用，不再各自维护
「先查强控覆盖、未命中再走档位」的双分支。**公式零重复**——R5/R8 数值依旧
全部来自 ``word_control.py`` 纯函数，本模块只做「装配后的区间选择」。

优先级（唯一公式，所有消费者共用）：
1. ``strict_override`` 命中（体系 B 快照派生 R5 区间）→ target=S、lo/hi=R5、
   tolerance=0.20、source=``word_control_strict``（附带 R8 初稿锚点 generation_anchor）；
2. ``level < 3`` → ``None``（L1/L2 是父节点，不生成正文，不参与字数控制）；
3. 单节点 ``target_words`` 精确覆盖 → 用户显式设置，体系 A 内最高优先级
   （2026-10-20 起生成侧同样尊重，移除旧「提示词侧契约 P-22」例外）；
4. 单节点 ``word_count_tier`` 档位覆盖 → 该档位 ``target_words`` / ``tolerance_ratio``；
5. ``level == 3`` 且为「L3 自身正文」（调用方已知有无下级章节时用
   ``is_l3_own`` 显式声明）→ ``L3_OWN_TARGET_WORDS``(300) ± ``L3_OWN_TOLERANCE_RATIO``(20%)；
6. 全局 ``writing_length`` 档位 → 该档位配置；默认档位（standard）兜底。

注：覆盖表由装配层 ``word_control_runtime.build_section_override`` 产出并由
调用方传入（本模块不 import 装配层，避免反向依赖）。历史上本模块曾是
 5 份同构实现的收编点（sse_utils / word_count / workflows_sections /
 workflows_expand / condense 内联解析），现已升级为两套体系的唯一入口。

容差口径
--------
容差随「目标来源」走，避免出现「目标取自档位、容差却硬编码 0.10」的分叉：
``target_words`` 覆盖 → ``DEFAULT_SECTION_TOLERANCE``(0.10)；档位来源 → 该档位
``tolerance_ratio``；L3 自身正文 → ``get_l3_own_tolerance()``。

行为差异（唯一一处，刻意对齐多数派）
------------------------------------
第 4 处（``section_expand_threshold``）此前对「L3 无档位覆盖但有全局
``writing_length``」的顺序是「档位优先于 L3 自身 300 字」，与 1/2/5 处相反。
本模块统一为多数派口径（L3 自身正文 300 ±20% 优先），即
``section_expand_threshold({"level": 3}, 2000, writing_length="detailed")``
由 1600 修正为 240 —— 与生成、校验、压缩、R12 警告四处保持一致。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional


#: 目标来源标识（便于日志与 SSE payload 自描述，便于回归断言）。
SOURCE_STRICT = "word_control_strict"
SOURCE_SECTION_TARGET_WORDS = "section_target_words"
SOURCE_SECTION_TIER = "section_tier"
SOURCE_L3_OWN = "l3_own"
SOURCE_WRITING_LENGTH_TIER = "writing_length_tier"
SOURCE_DEFAULT_TIER = "default_tier"

#: 无档位信息时的默认容差（与历史硬编码 0.10 一致）。
DEFAULT_SECTION_TOLERANCE = 0.10


@dataclass(frozen=True)
class SectionWordTarget:
    """单节的字数控制口径解析结果（不可变）。

    ``generation_anchor``：仅体系 B 强控命中时非 0 —— R8 初稿倒推目标
    （含装配侧 GUARD 钳制），供提示词侧取写作锚点；档位路径恒为 0。
    """

    target: int
    lo: int
    hi: int
    tolerance: float
    source: str
    generation_anchor: int = 0

    @property
    def label(self) -> str:
        """人类可读来源标签（提示词与前端 tooltip 用）。"""
        return {
            SOURCE_STRICT: "强控快照（易标 R5）",
            SOURCE_SECTION_TARGET_WORDS: "单节目标覆盖",
            SOURCE_SECTION_TIER: "单节档位",
            SOURCE_L3_OWN: "L3 自身正文",
            SOURCE_WRITING_LENGTH_TIER: "全局档位",
            SOURCE_DEFAULT_TIER: "默认档位",
        }.get(self.source, self.source)


def _cfg(key: Optional[str]) -> Optional[dict]:
    """取档位配置（DB 优先、异常回落由 ``get_tier_config`` 内部兜底）。"""
    if not key:
        return None
    try:
        from .ai.writing_length_config import get_tier_config

        return get_tier_config(key)
    except Exception:
        return None


def _default_cfg() -> Optional[dict]:
    try:
        from .ai.writing_length_config import get_default_tier_config

        return get_default_tier_config()
    except Exception:
        return None


def _bounds(target: int, tolerance: float) -> tuple[int, int]:
    tol = max(0.0, min(1.0, float(tolerance or 0.0)))
    return int(target * (1 - tol)), int(target * (1 + tol))


def _strict_from_override(
    strict_override: Optional[Mapping],
) -> Optional[SectionWordTarget]:
    """体系 B 强控覆盖 → 统一口径对象（命中时短路，优先级最高）。

    命中条件：``section_words > 0`` 且 ``hi > 0`` 且 ``lo > 0``（装配层
    ``build_section_override`` 产物恒含四键；非法/缺键视为未命中，
    由调用方回落体系 A 档位链）。tolerance 不信任入参，始终取
    ``word_control.STRICT_SECTION_TOLERANCE`` 权威常量（公式零重复）。
    """
    if not strict_override or not isinstance(strict_override, Mapping):
        return None
    try:
        section_words = int(strict_override.get("section_words") or 0)
        lo = int(strict_override.get("lo") or 0)
        hi = int(strict_override.get("hi") or 0)
        anchor = int(strict_override.get("generation_target") or 0)
    except (TypeError, ValueError):
        return None
    if section_words <= 0 or hi <= 0 or lo <= 0:
        return None
    try:
        from .word_control import STRICT_SECTION_TOLERANCE

        tol = float(STRICT_SECTION_TOLERANCE)
    except Exception:
        tol = 0.20
    return SectionWordTarget(
        target=section_words, lo=lo, hi=hi, tolerance=tol,
        source=SOURCE_STRICT, generation_anchor=anchor,
    )


def resolve_section_word_target(
    *,
    level: Any = 3,
    target_words: Any = None,
    word_count_tier: Any = None,
    writing_length: Any = None,
    is_l3_own: Optional[bool] = None,
    strict_override: Optional[Mapping] = None,
) -> Optional[SectionWordTarget]:
    """解析单节的有效目标字数与达标区间（唯一权威口径）。

    Args:
        level: 章节层级（L1/L2 返回 None）。
        target_words: 单节点目标字数覆盖（``sections.target_words``）。
        word_count_tier: 单节点档位覆盖（``sections.word_count_tier``）。
        writing_length: 全局写作长度档位（brief/standard/detailed/long）。
        is_l3_own: L3 是否为「自身正文」节点。``None`` 表示调用方无子节点信息，
            按既有多数派口径视为 True（L3 叶子是默认形态）；已知下级章节的调用方
            （正文提示词侧）应显式传 False，让该 L3 回落到四档「子树总字数」口径。
        strict_override: 体系 B 强控覆盖（``word_control_runtime.
            build_section_override`` 的逐节产物）；命中时短路返回 R5 区间，
            优先于含 L1/L2 过滤在内的所有档位口径（覆盖表本就只装 L3+ 节）。

    Returns:
        ``SectionWordTarget``；``level < 3`` 时返回 ``None``（不参与控制）。
    """
    # 0) 体系 B 强控覆盖（合并后最高优先级，2026-10-20）
    strict = _strict_from_override(strict_override)
    if strict is not None:
        return strict

    try:
        lvl = int(level or 3)
    except (TypeError, ValueError):
        lvl = 3
    if lvl < 3:
        return None

    # 1) 单节点精确覆盖
    try:
        tw = int(target_words or 0)
    except (TypeError, ValueError):
        tw = 0
    if tw > 0:
        lo, hi = _bounds(tw, DEFAULT_SECTION_TOLERANCE)
        return SectionWordTarget(tw, lo, hi, DEFAULT_SECTION_TOLERANCE, SOURCE_SECTION_TARGET_WORDS)

    # 2) 单节点档位覆盖
    cfg = _cfg(word_count_tier)
    if cfg and int(cfg.get("target_words") or 0) > 0:
        target = int(cfg["target_words"])
        tol = float(cfg.get("tolerance_ratio", DEFAULT_SECTION_TOLERANCE))
        lo, hi = _bounds(target, tol)
        return SectionWordTarget(target, lo, hi, tol, SOURCE_SECTION_TIER)

    # 3) L3 自身正文（无档位覆盖时的默认口径）
    if lvl == 3 and (is_l3_own is None or bool(is_l3_own)):
        try:
            from .ai.writing_length_config import get_l3_own_range, get_l3_own_target, get_l3_own_tolerance

            target = int(get_l3_own_target())
            tol = float(get_l3_own_tolerance())
            lo, hi = get_l3_own_range()
        except Exception:
            target, tol = 300, 0.20
            lo, hi = _bounds(target, tol)
        return SectionWordTarget(target, int(lo), int(hi), tol, SOURCE_L3_OWN)

    # 4) 全局档位
    cfg = _cfg(writing_length)
    if cfg and int(cfg.get("target_words") or 0) > 0:
        target = int(cfg["target_words"])
        tol = float(cfg.get("tolerance_ratio", DEFAULT_SECTION_TOLERANCE))
        lo, hi = _bounds(target, tol)
        return SectionWordTarget(target, lo, hi, tol, SOURCE_WRITING_LENGTH_TIER)

    # 5) 默认档位
    cfg = _default_cfg()
    target = int((cfg or {}).get("target_words") or 1200)
    tol = float((cfg or {}).get("tolerance_ratio", DEFAULT_SECTION_TOLERANCE))
    lo, hi = _bounds(target, tol)
    return SectionWordTarget(target, lo, hi, tol, SOURCE_DEFAULT_TIER)


def resolve_section_word_target_from_mapping(
    section: Any,
    *,
    writing_length: Any = None,
    is_l3_own: Optional[bool] = None,
    strict_override: Optional[Mapping] = None,
) -> Optional[SectionWordTarget]:
    """从章节 dict / ORM 行解析目标字数（字段名兼容两种表达）。

    ``strict_override`` 为体系 B 强控覆盖（可选），命中时短路优先。
    """
    if section is None:
        return None
    if isinstance(section, Mapping):
        get = section.get
    else:
        def get(key, default=None):
            return getattr(section, key, default)

    return resolve_section_word_target(
        level=get("level", 3),
        target_words=get("target_words", None),
        word_count_tier=get("word_count_tier", None),
        writing_length=writing_length,
        is_l3_own=is_l3_own,
        strict_override=strict_override,
    )


__all__ = [
    "DEFAULT_SECTION_TOLERANCE",
    "SOURCE_DEFAULT_TIER",
    "SOURCE_L3_OWN",
    "SOURCE_SECTION_TARGET_WORDS",
    "SOURCE_SECTION_TIER",
    "SOURCE_STRICT",
    "SOURCE_WRITING_LENGTH_TIER",
    "SectionWordTarget",
    "resolve_section_word_target",
    "resolve_section_word_target_from_mapping",
]
