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

优先级（唯一公式，5 处共用）
---------------------------
1. ``level < 3`` → ``None``（L1/L2 是父节点，不生成正文，不参与字数控制）；
2. 单节点 ``target_words`` 精确覆盖 → 用户显式设置，最高优先级；
3. 单节点 ``word_count_tier`` 档位覆盖 → 该档位 ``target_words`` / ``tolerance_ratio``；
4. ``level == 3`` 且为「L3 自身正文」（默认口径；调用方已知有无下级章节时用
   ``is_l3_own`` 显式声明）→ ``L3_OWN_TARGET_WORDS``(300) ± ``L3_OWN_TOLERANCE_RATIO``(20%)；
5. 全局 ``writing_length`` 档位 → 该档位配置；
6. 默认档位（standard）。

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
SOURCE_SECTION_TARGET_WORDS = "section_target_words"
SOURCE_SECTION_TIER = "section_tier"
SOURCE_L3_OWN = "l3_own"
SOURCE_WRITING_LENGTH_TIER = "writing_length_tier"
SOURCE_DEFAULT_TIER = "default_tier"

#: 无档位信息时的默认容差（与历史硬编码 0.10 一致）。
DEFAULT_SECTION_TOLERANCE = 0.10


@dataclass(frozen=True)
class SectionWordTarget:
    """单节的字数控制口径解析结果（不可变）。"""

    target: int
    lo: int
    hi: int
    tolerance: float
    source: str

    @property
    def label(self) -> str:
        """人类可读来源标签（提示词与前端 tooltip 用）。"""
        return {
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


def resolve_section_word_target(
    *,
    level: Any = 3,
    target_words: Any = None,
    word_count_tier: Any = None,
    writing_length: Any = None,
    is_l3_own: Optional[bool] = None,
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

    Returns:
        ``SectionWordTarget``；``level < 3`` 时返回 ``None``（不参与控制）。
    """
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
) -> Optional[SectionWordTarget]:
    """从章节 dict / ORM 行解析目标字数（字段名兼容两种表达）。"""
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
    )


__all__ = [
    "DEFAULT_SECTION_TOLERANCE",
    "SOURCE_DEFAULT_TIER",
    "SOURCE_L3_OWN",
    "SOURCE_SECTION_TARGET_WORDS",
    "SOURCE_SECTION_TIER",
    "SOURCE_WRITING_LENGTH_TIER",
    "SectionWordTarget",
    "resolve_section_word_target",
    "resolve_section_word_target_from_mapping",
]
