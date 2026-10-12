"""易标（OpenBidKit）文字生成数量控制规则与标准 · 移植实现。

来源：《报告_易标文字生成数量控制规则与标准_2026-10-12》（审计对象
`OpenBidKit_Yibiao-main-2026-09-14`）。该审计报告把易标的字数控制拆解为
R1–R15 共 15 条可执行的数值标准，本模块是其在**本软件**侧的权威实现。

设计原则（与易标一致）：

1. **纯函数**：本模块不调 AI、不读写数据库、不产生副作用，便于单元测试与
   跨模块复用；所有外部副作用（落库、推送 SSE、调用 AI）由调用方负责。
2. **单一口径**：所有派生公式（±20% 容差、折扣系数、±2 二审容差、批次上限）
   只在本文件定义一次，提示词侧、校验侧、展示侧必须引用这里的常量，
   不允许出现第二套口径 —— 这正是本平台历史故障的根因
   （`word_count_tiers.tolerance_ratio` 生成侧与校验侧分叉，见
   ``app/routers/word_count.py::_tolerance_for_section`` 的 BUG-FIX 注释）。
3. **控制失败不硬裁**：R12 的警告构造器只产出「期望区间 vs 实际值 + 逐节
   明细」的数据结构，交由前端弹窗引导人工裁决，绝不静默截断正文。
4. **失败即回退**：R11 的六道硬校验任一失败都返回 ``(False, reason)``，
   调用方保留原正文，与易标 ``requestWordAdjustment`` 的六道程序侧校验一致。

规则编号对照（便于代码审查时逐条回溯易标审计报告第 9 节速查表）：

    R1  字数口径        count_readable_words（app/utils/__init__.py，本模块重导出）
    R2  输入单位        parse_word_count_draft / WORD_COUNT_INPUT_PATTERN
    R3  默认单节口径    DEFAULT_SECTION_WORDS
    R4  页数预估        estimate_pages
    R5  强控小节区间    derive_strict_bounds / normalize_word_control_options
    R6  目标小节数      derive_target_leaf_count / leaf_count_range / check_leaf_count_feasibility
    R7  目录二审容差    outline_review_bounds
    R8  初稿折扣目标    compute_generation_word_target
    R9  小节调整轮数    MAX_WORD_ADJUSTMENT_ROUNDS + granularity_for_gap
    R10 全文调整批次    build_total_word_expansion_batch / build_total_word_shrink_batch
    R11 调整硬校验      validate_word_adjustment_result
    R12 失败兜底        build_word_control_warning
    R13 内容优先级      SECTION_WORD_CONTENT_PRESERVATION_CLAUSE
    R14 配置快照        normalize_outline_word_control_options / derive_snapshot_bounds
    R15 可研报告下限    normalize_target_words
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping, Optional, Sequence

# R1 —— 可读字数口径的权威实现放在 app.utils（平台历史约定：
# workflows_sections.py 注释称其为「权威口径」）。这里只做重导出，保证
# 「字数统计」在本平台只有一份实现，避免易标报告指出的「主进程与渲染层
# 两份实现漂移」在本平台复现。
from ..utils import count_readable_words as count_readable_words  # noqa: F401


__all__ = [
    # 常量
    "DEFAULT_SECTION_WORDS",
    "WORD_COUNT_INPUT_UNIT",
    "WORD_COUNT_INPUT_PATTERN",
    "ESTIMATED_WORDS_PER_PAGE",
    "STRICT_SECTION_TOLERANCE",
    "GENERATION_WORD_TARGET_RATIO",
    "OUTLINE_REVIEW_LEAF_TOLERANCE",
    "OUTLINE_LEAF_TARGET_MARGIN",
    "MAX_WORD_ADJUSTMENT_ROUNDS",
    "MAX_EXPANSION_NO_PROGRESS_ROUNDS",
    "TOTAL_WORD_ADJUSTMENT_BATCH_SIZE",
    "TOTAL_WORD_SHRINK_SECTION_RATIO",
    "TOTAL_WORD_SHRINK_MIN_CAPACITY_RATIO",
    "GRANULARITY_GAP_THRESHOLD",
    "CONTENT_WORD_CONTROL_WARNING",
    "SECTION_WORD_CONTROL_WARNING",
    "FEASIBILITY_MIN_TARGET_WORDS",
    "FEASIBILITY_DEFAULT_TARGET_WORDS",
    "SECTION_WORD_CONTENT_PRESERVATION_CLAUSE",
    "MODE_EXPAND",
    "MODE_SHRINK",
    "Granularity",
    "WordControlOptions",
    # 重导出（R1 权威口径）
    "count_readable_words",
    # 函数
    "normalize_word_control_options",
    "parse_word_count_draft",
    "validate_word_control_options",
    "effective_section_words",
    "derive_strict_bounds",
    "estimate_pages",
    "leaf_count_range",
    "is_outline_leaf_count_outside_range",
    "derive_target_leaf_count",
    "check_leaf_count_feasibility",
    "outline_review_bounds",
    "is_leaf_count_within_review_tolerance",
    "compute_generation_word_target",
    "granularity_for_gap",
    "build_total_word_expansion_batch",
    "build_total_word_shrink_batch",
    "validate_word_adjustment_result",
    "build_word_control_warning",
    "build_leaf_count_warning",
    "normalize_outline_word_control_options",
    "derive_snapshot_bounds",
    "has_valid_snapshot",
    "normalize_target_words",
]


# =============================================================================
# 标准常量（R3 / R4 / R5 / R7 / R8 / R9 / R10 / R12 / R15）
# =============================================================================

#: R3 —— 全局默认单节口径。sectionWords 未设置时的内部指导值，
#: **不构成小节上限**（易标报告 6.1：DEFAULT_SECTION_WORD_GUIDANCE 只是分配
#: 依据，非强控时容量视为无限）。
DEFAULT_SECTION_WORDS = 3000

#: R2 —— 前端字数输入单位（万字）。填 2 = 20000 字，填 0.15 = 1500 字。
WORD_COUNT_INPUT_UNIT = 10000

#: R2 —— 输入草稿合法性正则：非负整数或最多 4 位小数。
#: 非法值（负号、科学计数法、超过 4 位小数）一律拒绝，提示
#: 「字数设置只允许填写非负整数」。
WORD_COUNT_INPUT_PATTERN = re.compile(r"^\d*(?:\.\d{0,4})?$")

#: R4 —— 页数预估分母：650 字/页。页面必须同时明示
#: 「页数和排版有关，无法精确预估」，不得当成硬指标。
ESTIMATED_WORDS_PER_PAGE = 650

#: R5 —— 强控小节容差 ±20%。派生、UI 展示、校验三处必须使用同一公式。
STRICT_SECTION_TOLERANCE = 0.20

#: R8 —— 生成初稿目标折扣系数：预留 AI 系统性偏高的缓冲，降低初稿超量概率。
GENERATION_WORD_TARGET_RATIO = 0.8

#: R7 —— 目录二审容差（叶子数 ±2）。区间内不算 leaf-count 问题。
OUTLINE_REVIEW_LEAF_TOLERANCE = 2

#: R6 —— 「仅上限 / 仅下限」场景下刻意预留的叶子数余量。
OUTLINE_LEAF_TARGET_MARGIN = 2

#: R9 / R10 —— 小节强控调整的最大轮数；全文缩写的最大轮数。
MAX_WORD_ADJUSTMENT_ROUNDS = 3

#: R10 —— 全文扩写不限轮数，但连续 N 轮无有效增字即停（防无限烧 token）。
MAX_EXPANSION_NO_PROGRESS_ROUNDS = 3

#: R10 —— 全文调整每轮最多处理的小节数。
TOTAL_WORD_ADJUSTMENT_BATCH_SIZE = 10

#: R10 —— 强控下单节单次最多缩减的比例。
TOTAL_WORD_SHRINK_SECTION_RATIO = 0.25

#: R10 —— 可缩空间 < 平均预算 × 该比例的小节直接跳过（集中资源到真正缩得动的节）。
TOTAL_WORD_SHRINK_MIN_CAPACITY_RATIO = 0.30

#: R9 —— 粒度选择阈值：|当前 − S| / S 超过该比例走段落级，否则句子级。
GRANULARITY_GAP_THRESHOLD = 0.20

#: R12 —— 失败兜底文案（与易标源码逐字一致，用户已形成认知）。
CONTENT_WORD_CONTROL_WARNING = "经多轮修复，字数仍未达预期，请您人工核对"
SECTION_WORD_CONTROL_WARNING = "字数未达预期，请您人工核对"

#: R15 —— 可研报告模块目标总字数的硬下限与默认值（软引导，非闭环强控）。
FEASIBILITY_MIN_TARGET_WORDS = 1000
FEASIBILITY_DEFAULT_TARGET_WORDS = 30000

#: R13 —— 内容保真优先级条款：字数要求不得覆盖保留原方案实质内容的要求。
SECTION_WORD_CONTENT_PRESERVATION_CLAUSE = (
    "字数要求不能覆盖保留原方案实质内容的要求；可以消除重复和冗余，"
    "但不得删除技术路线、参数、周期、人员、验收、售后和承诺。"
)

#: 调整方向枚举（R11 第 2 道校验使用）。
MODE_EXPAND = "expand"
MODE_SHRINK = "shrink"


class Granularity:
    """调整粒度枚举（R9）。

    ``paragraph`` 用于偏差较大（>20%）的场景，可整段增删；
    ``sentence`` 用于微调，改动粒度更小、更不容易破坏结构。
    """

    PARAGRAPH = "paragraph"
    SENTENCE = "sentence"




# =============================================================================
# R2 / R3 / R5 —— 用户配置层（输入标准与归一化）
# =============================================================================


@dataclass(frozen=True)
class WordControlOptions:
    """易标口径的字数控制配置（R2 / R5 归一化后的结果）。

    字段语义与易标 ``outline.ts`` 的 OutlineWordControlOptions 完全对齐：

    - ``minimum_words``：全文最少字数，0 表示不控制；
    - ``maximum_words``：全文最多字数，0 表示不控制；
    - ``section_words``：每小节目标字数，0 表示不控制；
    - ``strict_section_words``：强控开关，**仅在 section_words > 0 时允许为真**
      （否则 ±20% 区间无从定义，归一化阶段直接清零）。
    """

    minimum_words: int = 0
    maximum_words: int = 0
    section_words: int = 0
    strict_section_words: bool = False

    def __bool__(self) -> bool:
        """是否配置了任何有效的字数控制（全 0 视为未配置）。"""
        return bool(self.minimum_words or self.maximum_words or self.section_words)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _to_non_negative_int(value: Any) -> int:
    """把任意输入转成非负整数（向下取整）。

    易标 ``normalizeOutlineWordControlOptions`` 规则：全部取非负整数
    （``Math.floor``）。非法/空值视为 0（不控制），保证任何脏输入都不会
    让下游公式抛异常或产生负区间。
    """
    if value is None or isinstance(value, bool):
        return 0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(number):
        return 0
    return max(0, math.floor(number))


def parse_word_count_draft(draft: Optional[str]) -> Optional[int]:
    """把前端「万字」输入草稿解析成绝对字数（R2）。

    - 单位是**万字**，最多 4 位小数：``"2"`` → 20000，``"0.15"`` → 1500；
    - 空串 / ``"0"`` 合法，返回 0（表示不控制）；
    - 含负号、科学计数法、超过 4 位小数 → 返回 ``None``（调用方报
      「字数设置只允许填写非负整数」）；
    - 小数部分四舍五入到整数字（0.1543 万字 = 1543 字，不产生小数字数）。
    """
    if draft is None:
        return 0
    text = str(draft).strip()
    if text == "":
        return 0
    if not WORD_COUNT_INPUT_PATTERN.fullmatch(text):
        return None
    return int(round(float(text) * WORD_COUNT_INPUT_UNIT))


def normalize_word_control_options(
    source: Optional[Mapping[str, Any]] = None,
) -> WordControlOptions:
    """把任意来源的配置归一化成 :class:`WordControlOptions`（R2 / R5）。

    可接受的 key（snake_case 与 camelCase 均支持）：

        minimum_words / minimumWords, maximum_words / maximumWords,
        section_words / sectionWords, strict_section_words / strictSectionWords

    归一化规则（逐条对应易标报告 2.1）：

    1. 三个字数项全部取非负整数（向下取整），非法输入归零；
    2. ``strict_section_words`` 仅在 ``section_words > 0`` 时才允许为真，
       否则强制清零 —— 没有单节目标就没有 ±20% 区间，强控开关开着却无
       区间会让校验侧无所适从，这是易标控制链不断裂的关键设计。
    """
    if source is None:
        source = {}
    if not isinstance(source, Mapping):
        raise TypeError(f"word control 配置必须是映射对象，收到: {type(source).__name__}")

    lowered = {str(k).lower().replace("_", ""): v for k, v in source.items()}

    def _pick(*names: str) -> Any:
        for name in names:
            key = name.lower().replace("_", "")
            if key in lowered:
                return lowered[key]
        return None

    minimum = _to_non_negative_int(_pick("minimum_words"))
    maximum = _to_non_negative_int(_pick("maximum_words"))
    section = _to_non_negative_int(_pick("section_words"))

    raw_strict = _pick("strict_section_words")
    if isinstance(raw_strict, bool):
        strict = raw_strict
    elif isinstance(raw_strict, (int, float)):
        strict = int(raw_strict) != 0
    elif isinstance(raw_strict, str):
        strict = raw_strict.strip().lower() in ("1", "true", "yes", "on")
    else:
        strict = False
    if section <= 0:
        strict = False
    return WordControlOptions(
        minimum_words=minimum,
        maximum_words=maximum,
        section_words=section,
        strict_section_words=strict,
    )


def validate_word_control_options(options: WordControlOptions) -> list[str]:
    """校验配置的可行性，返回错误信息列表（空列表 = 可保存）。

    对应易标 ``normalizeWordControlDraft`` 的三项拦截：

    1. 最多字数 ≥ 最少字数；
    2. 叶子数区间必须存在：``minimumLeafCount = ceil(min/S)``、
       ``maximumLeafCount = floor(max/S)``，S 为 :func:`effective_section_words`；
    3. ``maximumLeafCount >= 1`` 且 ``minimumLeafCount <= maximumLeafCount``，
       否则该组合数学上不可满足（例如最少 10000 字、每节 200 字、最多 2000 字）。
    """
    errors: list[str] = []
    if options.maximum_words and options.minimum_words:
        if options.maximum_words < options.minimum_words:
            errors.append(
                f"最多字数（{options.maximum_words}）不能小于最少字数"
                f"（{options.minimum_words}）"
            )
    if not errors:
        effective = effective_section_words(options)
        min_leaf, max_leaf = leaf_count_range(options)
        if max_leaf is not None and max_leaf < 1:
            errors.append(
                f"按每小节 {effective} 字计算，最多字数不足 1 个小节，"
                "请提高最多字数或降低每小节字数"
            )
        elif max_leaf is not None and min_leaf > max_leaf:
            errors.append(
                f"最少字数需要 {min_leaf} 个小节，最多字数只允许 {max_leaf} 个小节，"
                "区间不存在，请调整字数设置"
            )
    return errors


def effective_section_words(options: WordControlOptions) -> int:
    """有效单节字数（R3）：section_words 为 0 时回落到全局默认 3000。"""
    return options.section_words if options.section_words > 0 else DEFAULT_SECTION_WORDS


def derive_strict_bounds(section_words: int) -> tuple[int, int]:
    """派生强控小节的达标区间（R5）：``[ceil(0.8S), floor(1.2S)]``。

    易标报告 4 节明确：``sectionMinimumWords = ceil(sectionWords × 0.8)``、
    ``sectionMaximumWords = floor(sectionWords × 1.2)``，**±20% 容差在
    派生、UI 展示、校验三处使用同一公式，无第二套口径**。这里 ceil/floor
    的方向是刻意的：下界向上取整、上界向下取整，保证区间宽度严格不超过
    40%，不留任何「刚好卡边」的模糊地带。

    ``section_words <= 0`` 时返回 ``(0, 0)`` 表示「不控制」，调用方必须先
    判断再使用，否则会误把所有正文判为越界。
    """
    if not section_words or section_words <= 0:
        return (0, 0)
    return (
        math.ceil(section_words * (1 - STRICT_SECTION_TOLERANCE)),
        math.floor(section_words * (1 + STRICT_SECTION_TOLERANCE)),
    )


def estimate_pages(
    minimum_words: int = 0,
    maximum_words: int = 0,
    words_per_page: int = ESTIMATED_WORDS_PER_PAGE,
) -> int:
    """预估页数（R4）：以 (min+max)/2（或单边值）÷ 650 字/页 向上取整。

    这是**展示用软指标**，页面必须同时标注「页数和排版有关，无法精确
    预估」，不得作为验收标准参与任何校验判定。
    """
    if minimum_words > 0 and maximum_words > 0:
        target = (minimum_words + maximum_words) / 2
    else:
        target = maximum_words if maximum_words > 0 else minimum_words
    if target <= 0 or words_per_page <= 0:
        return 0
    return math.ceil(target / words_per_page)


# =============================================================================
# R6 / R7 —— 目录阶段的数量控制（把字数翻译成小节数）
# =============================================================================


def leaf_count_range(
    options: WordControlOptions,
) -> tuple[int, Optional[int]]:
    """叶子数可行区间 ``[ceil(min/S), floor(max/S)]``（R6）。

    ``max`` 未设置时上限为 ``None``（无界），``min`` 未设置时下界为 0。
    S = :func:`effective_section_words`（section_words 为 0 时取 3000）。

    本区间同时被 :func:`validate_word_control_options`（保存前拦截）与
    :func:`is_outline_leaf_count_outside_range`（手工编辑目录时预警）使用，
    两处口径必须一致。
    """
    section = effective_section_words(options)
    min_leaf = math.ceil(options.minimum_words / section) if options.minimum_words > 0 else 0
    max_leaf = (
        math.floor(options.maximum_words / section) if options.maximum_words > 0 else None
    )
    return (min_leaf, max_leaf)


def is_outline_leaf_count_outside_range(
    options: WordControlOptions, leaf_count: int
) -> bool:
    """判断手工编辑后的叶子数是否落在可行区间之外（R6 预警）。

    对应易标 ``isOutlineLeafCountOutsideRange``：用于用户手动增删目录节点
    时的即时提示「您手动修改的目录可能导致生成正文字数不符合预期」。
    未配置字数控制、或未配置任何边界时返回 False（不拦截）。
    """
    if options is None or leaf_count is None or leaf_count < 0:
        return False
    min_leaf, max_leaf = leaf_count_range(options)
    if min_leaf > 0 and leaf_count < min_leaf:
        return True
    if max_leaf is not None and leaf_count > max_leaf:
        return True
    return False


def derive_target_leaf_count(options: WordControlOptions) -> Optional[int]:
    """目标叶子数公式（R6，对应易标 ``deriveTargetLeafCount``）。

    +--------------------+-------------------------------------------+
    | 配置组合           | 目标小节数                                |
    +--------------------+-------------------------------------------+
    | min 和 max 都设置  | ``ceil(((min + max) / 2) / S)``           |
    | 仅 max             | ``floor(max / S) - 2``（刻意减 2 留余量） |
    | 仅 min             | ``ceil(min / S) + 2``（刻意加 2 留余量）  |
    | 都未设置           | ``None``（Prompt 自主决定）               |
    +--------------------+-------------------------------------------+

    单边场景的 ±2 是刻意的：只给上限时 AI 倾向写满，必须留余量才能把
    正文压在 max 之内；只给下限时 AI 倾向写少，必须留余量才能撑到 min。
    结果下限钳制为 1（再小也无意义），可行性由
    :func:`check_leaf_count_feasibility` 单独把关。
    """
    if options is None or (not options.minimum_words and not options.maximum_words):
        return None
    section = effective_section_words(options)
    if options.minimum_words > 0 and options.maximum_words > 0:
        target = math.ceil(((options.minimum_words + options.maximum_words) / 2) / section)
    elif options.maximum_words > 0:
        target = math.floor(options.maximum_words / section) - OUTLINE_LEAF_TARGET_MARGIN
    else:
        target = math.ceil(options.minimum_words / section) + OUTLINE_LEAF_TARGET_MARGIN
    return max(1, target)


def check_leaf_count_feasibility(
    options: WordControlOptions,
    required_leaf_count: int = 0,
) -> Optional[str]:
    """独立成册下限修正的可行性校验（R6，对应 ``enforceMinimumLeafTarget``）。

    Args:
        options: 已归一化的字数控制配置。
        required_leaf_count: 平台侧的**最小必需叶子数**，即
            ``固定 AI 叶子数 + 技术评分分支数``（每个技术评分分支至少保留
            一个正文叶子，否则该分支无正文可写、评分点必然失分）。

    Returns:
        可行性错误信息（需提示用户调整配置）；``None`` 表示可行。

    判定规则：

    1. 目标叶子数不得小于 ``required_leaf_count``；
    2. 强控且设了 max 时校验可行性：
       ``maximumLeafCount = floor(max / ceil(S × 0.8))``，
       若小于最小需求，说明「单节下限 × 必需叶子数」已超出全文上限，
       该组合数学上不可能满足，必须提示用户
       「提高全文最大字数、降低单节字数或减少技术评分分支后重新生成目录」。
    """
    target = derive_target_leaf_count(options)
    if target is None:
        return None
    if required_leaf_count > 0 and target < required_leaf_count:
        return (
            f"按当前字数设置只能生成约 {target} 个小节，少于项目必需的最小"
            f"小节数 {required_leaf_count}（每个技术评分分支至少保留 1 个正文小节）。"
            "请提高全文最大字数、降低单节字数或减少技术评分分支后重新生成目录。"
        )
    if options.strict_section_words and options.maximum_words > 0:
        section = effective_section_words(options)
        section_min, _ = derive_strict_bounds(section)
        if section_min <= 0:
            return None
        max_leaf = math.floor(options.maximum_words / section_min)
        if required_leaf_count > 0 and max_leaf < required_leaf_count:
            return (
                f"强控区间下限 {section_min} 字 × 必需 {required_leaf_count} 个小节"
                f"已超出全文上限 {options.maximum_words} 字。"
                "请提高全文最大字数、降低单节字数或减少技术评分分支后重新生成目录。"
            )
    return None



def outline_review_bounds(target_leaf_count: Optional[int]) -> Optional[tuple[int, int]]:
    """目录二审可接受区间（R7）：``[target − 2, target + 2]``。

    易标 ``buildOutlineReviewContext`` 只在**设置了总字数目标**时给出该区间；
    未设置时返回 ``None``（Agent 不得凭叶子数量提意见）。区间内不算
    leaf-count 问题，避免 Agent 对四舍五入的余量斤斤计较。
    """
    if not target_leaf_count or target_leaf_count <= 0:
        return None
    return (
        max(1, target_leaf_count - OUTLINE_REVIEW_LEAF_TOLERANCE),
        target_leaf_count + OUTLINE_REVIEW_LEAF_TOLERANCE,
    )


def is_leaf_count_within_review_tolerance(
    target_leaf_count: Optional[int], actual_leaf_count: int
) -> bool:
    """实际叶子数是否落在二审容差区间内（R7）。"""
    bounds = outline_review_bounds(target_leaf_count)
    if bounds is None:
        return True
    return bounds[0] <= actual_leaf_count <= bounds[1]


# =============================================================================
# R8 / R9 —— 正文生成阶段的数量控制（初稿折扣目标与调整粒度）
# =============================================================================


def compute_generation_word_target(
    section_words: int,
    maximum_words: int,
    leaf_count: int,
    strict: bool,
) -> int:
    """倒推初稿目标字数（R8，对应 ``computeGenerationWordTarget``）。

    仅在**强控且设了全文上限**时生效，公式：

        generationTarget = max(ceil(0.8 × S), floor(0.8 × max / 叶子数))

    rationale（易标源码注释原文转述）：

    - ``0.8`` 折扣是「预留 AI 系统性偏高的缓冲，降低初稿超量概率」——
      本平台的实测结论一致（10 个 L3 章节平均偏差 +34.2%、100% 系统性偏高）；
    - ``max(0.8S, …)`` 保证「不低于小节下限，避免倒推目标把 AI 引导到强控
      范围之外」——当全文上限很宽松时倒推值可能低于单节下限，此时必须以
      单节下限为准，否则 AI 会按一个必然越界的低目标写作。

    其他组合（非强控 / 未设上限 / 叶子数为 0）返回 0，表示
    「本次未设置倒推目标」，由调用方回落到常规提示词话术。

    已知薄弱点（易标报告第 10 节确认，本平台继承同一限制）：强控但**未设
    全文上限**时本函数返回 0，大量小节要靠 3 轮调整拉回，
    ``word_control_warning`` 出现频率会显著升高。
    """
    if not strict or section_words <= 0 or maximum_words <= 0 or leaf_count <= 0:
        return 0
    section_floor = math.ceil(section_words * (1 - STRICT_SECTION_TOLERANCE))
    derived = math.floor(maximum_words * GENERATION_WORD_TARGET_RATIO / leaf_count)
    return max(section_floor, derived)


def granularity_for_gap(current_words: int, section_words: int) -> str:
    """调整粒度选择（R9）。

    ``|当前 − S| / S > 20%`` 用 :attr:`Granularity.PARAGRAPH`（段落级，可整段
    增删），否则 :attr:`Granularity.SENTENCE`（句子级，微调）。

    ``section_words <= 0`` 或当前字数为 0 时回落到段落级 —— 此时没有任何
    可用的相对基准，段落级是更保守的默认值。
    """
    if not section_words or section_words <= 0 or current_words <= 0:
        return Granularity.PARAGRAPH
    gap_ratio = abs(current_words - section_words) / section_words
    return (
        Granularity.PARAGRAPH
        if gap_ratio > GRANULARITY_GAP_THRESHOLD
        else Granularity.SENTENCE
    )




# =============================================================================
# R10 —— 全文字数调整（扩写 / 缩写批次预算）
# =============================================================================


def _section_id(section: Any) -> Any:
    """从多种章节表达（dict / ORM 行 / dataclass）中取稳定标识。"""
    if isinstance(section, Mapping):
        return section.get("id")
    return getattr(section, "id", None)


def _section_title(section: Any) -> str:
    if isinstance(section, Mapping):
        return str(section.get("title") or "")
    return str(getattr(section, "title", "") or "")


def _section_words(section: Any) -> int:
    if isinstance(section, Mapping):
        raw = section.get("words", section.get("word_count", 0))
    else:
        raw = getattr(section, "word_count", 0)
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        return 0


def _batch_item(section: Any, budget: int, reason: str) -> dict:
    return {
        "id": _section_id(section),
        "title": _section_title(section),
        "current_words": _section_words(section),
        "budget": max(0, int(budget)),
        "reason": reason,
    }


def build_total_word_expansion_batch(
    sections: Iterable[Any],
    *,
    total_words: int,
    minimum_words: int,
    options: WordControlOptions,
    guidance_words: Optional[int] = None,
) -> list[dict]:
    """全文扩写的一轮批次预算（R10，对应 ``buildTotalWordExpansionBatch``）。

    方向判定由调用方负责（``total_words < minimum_words`` → expand），本函数
    只负责**在容量约束下把总差额公平地分配到各小节**。

    分配算法（与易标实现逐条对应）：

    1. ``gap = max(0, minimum_words - total_words)``，为 0 直接返回空批次；
    2. **指导缺口**：``guidance_gap_i = max(0, G − words_i)``，G 为
       ``guidance_words``（默认 :data:`DEFAULT_SECTION_WORDS` = 3000）。
       非强控时容量视为无限，3000 只是**分配依据，不是上限**；
       强控时容量封顶为 ``max(0, 1.2S − words_i)``；
    3. **按比例分配**：``alloc_i = floor(gap × guidance_gap_i / Σguidance_gap)``，
       受容量封顶；
    4. **余数补偿**：剩余 ``gap − Σalloc`` 按小数部分降序，每节 +1，直到
       额度耗尽或全部到顶（避免系统性偏向列表尾部）；
    5. **公平均摊**：仍有剩余额度时在未到容量的小节间均摊
       （``fairShare = ceil(剩余 / 可用节数)``），循环直到额度耗尽或无可用节。

    Returns:
        每项 ``{"id", "title", "current_words", "budget", "reason"}``，
        仅含 ``budget > 0`` 的项，按原顺序返回；空列表表示本轮无需扩写。
    """
    pool = [s for s in (sections or []) if _section_words(s) > 0]
    gap = max(0, int(minimum_words) - max(0, int(total_words)))
    if gap <= 0 or not pool:
        return []

    section_words = options.section_words if options.section_words > 0 else DEFAULT_SECTION_WORDS
    bounds = derive_strict_bounds(section_words) if options.strict_section_words else None
    guidance = guidance_words if guidance_words and guidance_words > 0 else DEFAULT_SECTION_WORDS

    capacity: list[float] = []
    guidance_gap: list[int] = []
    for s in pool:
        words = _section_words(s)
        cap = (bounds[1] - words) if bounds else None
        capacity.append(cap if cap is not None else math.inf)
        guidance_gap.append(max(0, guidance - words))

    allocation = [0.0] * len(pool)
    denom = sum(guidance_gap)
    if denom > 0:
        for i, gg in enumerate(guidance_gap):
            if gg <= 0:
                continue
            allocation[i] = min(math.floor(gap * gg / denom), capacity[i])
    else:
        for i in range(len(pool)):
            allocation[i] = min(gap // len(pool), capacity[i])

    # 步骤 4：余数按小数部分降序补 1
    remaining = gap - sum(int(a) for a in allocation)
    if denom > 0 and remaining > 0:
        fractions = [
            (gap * guidance_gap[i] / denom) - math.floor(gap * guidance_gap[i] / denom)
            for i in range(len(pool))
        ]
        order = sorted(range(len(pool)), key=lambda i: (-fractions[i], i))
        for i in order:
            if remaining <= 0:
                break
            if allocation[i] >= capacity[i]:
                continue
            allocation[i] += 1
            remaining -= 1

    # 步骤 5：公平均摊（fairShare = ceil(剩余 / 可用节数)）
    while remaining > 0:
        available = [i for i in range(len(pool)) if allocation[i] < capacity[i]]
        if not available:
            break
        fair = math.ceil(remaining / len(available))
        progressed = False
        for i in available:
            if remaining <= 0:
                break
            headroom = capacity[i] - allocation[i]
            add = min(fair, headroom, remaining)
            if add > 0:
                allocation[i] += add
                remaining -= add
                progressed = True
        if not progressed:
            break

    return [
        _batch_item(pool[i], int(allocation[i]), "expand")
        for i in range(len(pool))
        if allocation[i] > 0
    ]



def build_total_word_shrink_batch(
    sections: Iterable[Any],
    *,
    total_words: int,
    maximum_words: int,
    options: WordControlOptions,
    batch_size: int = TOTAL_WORD_ADJUSTMENT_BATCH_SIZE,
) -> list[dict]:
    """全文缩写的一轮批次预算（R10，对应 ``buildTotalWordShrinkBatch``）。

    方向判定由调用方负责（``total_words > maximum_words`` → shrink）。

    分配算法（与易标实现逐条对应）：

    1. ``gap = max(0, total_words - maximum_words)``，为 0 直接返回空批次；
    2. 按字数**降序**优先动最大的节，每轮最多 ``batch_size``（默认 10）节；
    3. 单节容量：
       - 强控：``min(floor(words × 0.25), words − 0.8S, words − 1)`` ——
         既不超过单次 25% 的上限，又保证缩完仍留在强控下限之上，
         且**至少留 1 字**（不允许把正文缩成空节）；
       - 非强控：``words − 1``；
    4. 候选过滤：可缩空间 < 本轮平均预算 × 30% 的节直接跳过，
       「集中资源到真正缩得动的小节」；
    5. 每节预算 = ``min(剩余差额, fairShare, 容量)``，公平均摊循环直到
       额度耗尽或全部到顶。

    Returns:
        每项 ``{"id", "title", "current_words", "budget", "reason"}``，
        仅含 ``budget > 0`` 的项，按字数降序返回。
    """
    pool = [s for s in (sections or []) if _section_words(s) > 1]
    gap = max(0, int(total_words) - max(0, int(maximum_words)))
    if gap <= 0 or not pool:
        return []

    section_words = options.section_words if options.section_words > 0 else DEFAULT_SECTION_WORDS
    bounds = derive_strict_bounds(section_words) if options.strict_section_words else None
    limit = max(1, int(batch_size))

    # 按字数降序取前 limit 节，并计算各自容量
    ranked = sorted(pool, key=lambda s: -_section_words(s))[:limit]
    kept: list[Any] = []
    capacity: list[int] = []
    for s in ranked:
        words = _section_words(s)
        if bounds:
            cap = min(
                math.floor(words * TOTAL_WORD_SHRINK_SECTION_RATIO),
                words - bounds[0],
                words - 1,
            )
        else:
            cap = words - 1
        if cap > 0:
            kept.append(s)
            capacity.append(int(cap))
    if not kept:
        return []

    remaining = gap
    allocation = [0] * len(kept)
    while remaining > 0:
        avg_budget = remaining / len(kept)
        threshold = avg_budget * TOTAL_WORD_SHRINK_MIN_CAPACITY_RATIO
        active: list[int] = []
        for i, cap in enumerate(capacity):
            headroom = cap - allocation[i]
            if headroom > 0 and headroom >= threshold:
                active.append(i)
        if not active:
            break
        fair = math.ceil(remaining / len(active))
        progressed = False
        for i in active:
            if remaining <= 0:
                break
            headroom = capacity[i] - allocation[i]
            add = min(fair, headroom, remaining)
            if add > 0:
                allocation[i] += add
                remaining -= add
                progressed = True
        if not progressed:
            break

    return [
        _batch_item(kept[i], allocation[i], "shrink")
        for i in range(len(kept))
        if allocation[i] > 0
    ]



# =============================================================================
# R11 —— 字数调整的六道程序侧硬校验（防 AI 失控）
# =============================================================================


def validate_word_adjustment_result(
    *,
    current_words: int,
    new_words: int,
    mode: str,
    target_words: Optional[int] = None,
    maximum_change_words: Optional[int] = None,
    section_bounds: Optional[tuple[int, int]] = None,
    enforce_section_bounds: bool = False,
    total_words: Optional[int] = None,
    maximum_words: Optional[int] = None,
    minimum_words: Optional[int] = None,
    enforce_total_bounds: bool = False,
) -> tuple[bool, str]:
    """校验一次字数调整的结果，返回 ``(是否接受, 失败原因)``（R11）。

    与易标 ``requestWordAdjustment`` 应用补丁后的六道程序侧校验一一对应，
    **任一失败即放弃本轮、保留原正文并记日志**：

    1. 调整后必须有有效可读内容（``new_words > 0``）；
    2. 扩写必须净增字、缩写必须净减字（禁止零变化与反向调整）；
    3. 实际变化 ≤ 本轮额度 ``maximum_change_words``（不允许过冲）；
    4. 与目标的差距必须**严格缩小**（防震荡：来回在同一区间摆动）；
    5. 强控 + ``enforce_section_bounds`` 时结果必须落在 ``section_bounds``；
    6. 全文边界：扩写后总量不得超过 ``maximum_words``、
       缩写后不得低于 ``minimum_words``。

    返回元组而非抛异常，是因为调用方（AI 调整循环）需要把失败原因记入
    runtime 日志并保留原稿重试，抛异常会丢失「哪一道校验失败」的信息。
    """
    before = max(0, int(current_words or 0))
    after = max(0, int(new_words or 0))
    delta = after - before

    # 1. 非空
    if after <= 0:
        return False, "调整后正文为空"

    # 2. 单调性（方向非法也在此拦截）
    if mode == MODE_EXPAND and delta <= 0:
        return False, f"扩写未产生净增字（{before} → {after}）"
    if mode == MODE_SHRINK and delta >= 0:
        return False, f"缩写未产生净减字（{before} → {after}）"
    if mode not in (MODE_EXPAND, MODE_SHRINK):
        return False, f"非法调整方向: {mode!r}（仅支持 expand / shrink）"

    # 3. 额度（不允许过冲）
    if maximum_change_words is not None and maximum_change_words >= 0:
        if abs(delta) > maximum_change_words:
            return False, f"实际变化 {abs(delta)} 字超出本轮额度 {maximum_change_words} 字"

    # 4. 与目标的差距必须严格缩小（防震荡）
    if target_words is not None and target_words > 0:
        before_gap = abs(before - target_words)
        after_gap = abs(after - target_words)
        if after_gap >= before_gap:
            return False, f"与目标 {target_words} 字的差距未缩小（{before_gap} → {after_gap}）"

    # 5. 小节边界（强控时结果必须落回区间内）
    if enforce_section_bounds and section_bounds:
        lo, hi = section_bounds
        if hi > 0 and not (lo <= after <= hi):
            return False, f"调整后字数 {after} 超出强控区间 [{lo}, {hi}]"

    # 6. 全文边界（扩写不超上限、缩写不低于下限）
    # 6. 全文边界（扩写不超上限、缩写不低于下限）
    #    BUG-FIX 2026-10-12（生产接线暴露）：此前两个边界**不看方向**，导致
    #    「全文低于下限时执行扩写」永远被下限判负（new_total 仍 < minimum_words）
    #    → 扩写被 100% 打回，R10 扩写方向彻底失效。该缺陷此前不可见，只因为
    #    本函数零生产调用点（纯函数 + 单测）。现按方向分别约束，与易标
    #    R11 第 6 道语义逐字一致。
    if enforce_total_bounds and total_words is not None:
        new_total = max(0, int(total_words)) + delta
        if (
            mode == MODE_EXPAND
            and maximum_words is not None
            and maximum_words > 0
            and new_total > maximum_words
        ):
            return False, f"扩写后全文 {new_total} 字将超出上限 {maximum_words} 字"
        if (
            mode == MODE_SHRINK
            and minimum_words is not None
            and minimum_words > 0
            and new_total < minimum_words
        ):
            return False, f"缩写后全文 {new_total} 字将低于下限 {minimum_words} 字"


    return True, "ok"



# =============================================================================
# R12 —— 控制失败的兜底警告（不硬裁，交人工裁决）
# =============================================================================


def build_word_control_warning(
    *,
    single_section: bool = False,
    total_words: Optional[int] = None,
    options: Optional[WordControlOptions] = None,
    sections: Optional[Iterable[Any]] = None,
) -> Optional[dict]:
    """构造字数控制收尾警告（R12，对应易标 ``word_control_warning``）。

    任务完成前逐节做有效性检查并 ``rebuildContentWordCounts`` 重算全文后调用；
    **存在小节越界（强控）或全文仍偏离区间时**才返回警告结构，全部达标时
    返回 ``None``（前端不弹窗）。

    设计要点（易标报告第 10 节「失败透明」）：不静默截断正文，最终以数据化
    弹窗交人工裁决——期望区间 / 实际值 / 逐节明细（id、标题、实际字数）
    全部交给前端弹窗展示。

    Args:
        single_section: 是否为单节重生成模式（决定使用哪条文案）。
        total_words: 全文实际可读字数（未提供时按各节字数求和）。
        options: 字数控制配置。
        sections: 章节列表（dict / ORM 行均可，需可取到 id / title / word_count）。

    Returns:
        ``None`` 或 ``{"kind", "message", "single_section", "total", "sections"}``。
    """
    opts = options or WordControlOptions()
    pool = [s for s in (sections or []) if _section_words(s) > 0]
    actual = (
        max(0, int(total_words))
        if total_words is not None
        else sum(_section_words(s) for s in pool)
    )

    # 小节越界判定（仅强控且设了单节字数时启用）
    bounds = derive_strict_bounds(opts.section_words) if opts.strict_section_words else None
    out_of_range: list[dict] = []
    if bounds and bounds[1] > 0:
        lo, hi = bounds
        for s in pool:
            words = _section_words(s)
            if words < lo or words > hi:
                out_of_range.append(
                    {
                        "id": _section_id(s),
                        "title": _section_title(s),
                        "words": words,
                        "status": "below" if words < lo else "above",
                    }
                )

    # 全文区间判定
    total_status: Optional[str] = None
    if actual > 0:
        if opts.minimum_words > 0 and actual < opts.minimum_words:
            total_status = "below"
        elif opts.maximum_words > 0 and actual > opts.maximum_words:
            total_status = "above"
        elif opts.minimum_words > 0 or opts.maximum_words > 0:
            total_status = "ok"

    if not out_of_range and total_status in (None, "ok"):
        return None

    return {
        "kind": "content",
        "message": SECTION_WORD_CONTROL_WARNING if single_section else CONTENT_WORD_CONTROL_WARNING,
        "single_section": bool(single_section),
        "total": {
            "expected": (
                [opts.minimum_words or 0, opts.maximum_words or 0]
                if (opts.minimum_words or opts.maximum_words)
                else None
            ),
            "actual": actual,
            "status": total_status,
        },
        "sections": {
            "target": opts.section_words if opts.strict_section_words else None,
            "range": [bounds[0], bounds[1]] if bounds and bounds[1] > 0 else None,
            "out_of_range_count": len(out_of_range),
            "items": out_of_range,
        },
    }


def build_leaf_count_warning(
    *,
    expected_leaf_count: int,
    actual_leaf_count: int,
    review_bounds: Optional[tuple[int, int]] = None,
) -> Optional[dict]:
    """目录阶段叶子数偏差警告（R12 的 leaf-count 分支）。

    对应易标 ``word_adjustment_warning``（kind=``leaf-count``）：Agent 调整
    一次后仍不一致时写入，前端弹窗展示「AI生成小节数量未达到预期：
    预期 N 个 / 实际 M 个」。落在二审容差区间内时返回 ``None``。
    """
    bounds = review_bounds or outline_review_bounds(expected_leaf_count)
    if bounds is not None and bounds[0] <= actual_leaf_count <= bounds[1]:
        return None
    return {
        "kind": "leaf-count",
        "message": (
            f"AI生成小节数量未达到预期：预期 {expected_leaf_count} 个 / "
            f"实际 {actual_leaf_count} 个"
        ),
        "expected": expected_leaf_count,
        "actual": actual_leaf_count,
        "review_bounds": list(bounds) if bounds else None,
    }


# =============================================================================
# R14 —— 配置快照（控制链锚定，防「改了目录字数失控」）
# =============================================================================


def normalize_outline_word_control_options(
    source: Optional[Mapping[str, Any]] = None,
) -> WordControlOptions:
    """目录生成阶段的配置归一化（R14 入口）。

    与 :func:`normalize_word_control_options` 同义，单列以对齐易标的命名与
    调用点（``normalizeOutlineWordControlOptions``）。
    """
    return normalize_word_control_options(source)


def derive_snapshot_bounds(snapshot: Optional[Mapping[str, Any]]) -> dict[str, Any]:
    """基于快照派生强控区间（R14）。

    对应易标 ``normalizeOutlineWordControlSnapshot``：

        sectionMinimumWords = ceil(sectionWords × 0.8)
        sectionMaximumWords = floor(sectionWords × 1.2)
        strictSectionWords  = sectionWords > 0 && 快照开启强控

    ``snapshot`` 为 ``None`` / 空映射时返回全 0 结构（不控制），调用方可直接
    读取字段而无需再判空。
    """
    opts = normalize_word_control_options(snapshot)
    bounds = derive_strict_bounds(opts.section_words) if opts.strict_section_words else (0, 0)
    return {
        "minimum_words": opts.minimum_words,
        "maximum_words": opts.maximum_words,
        "section_words": opts.section_words,
        "section_minimum_words": bounds[0],
        "section_maximum_words": bounds[1],
        "strict_section_words": opts.strict_section_words,
    }


def has_valid_snapshot(snapshot: Any) -> bool:
    """判断目录是否持有有效的字数控制生效快照（R14 硬门槛）。

    对应易标 ``taskService.cjs`` L1467 的启动门槛：

        if (!technicalPlan.outlineWordControlSnapshot) {
            throw new Error('当前目录没有字数控制生效快照，请重新生成目录');
        }

    快照必须与目录版本**配对**存在（目录生成成功时与 ``outlineData`` 同一
    checkpoint 落盘）；手工编辑目录、清空目录、切换工作流都必须置空快照，
    否则会出现「改了目录但字数控制链脱节」的静默故障。

    接受三种表达：``None``（无快照）、JSON 字符串（含空串/``null``/``{}``
    视为无效）、``Mapping``（空映射视为无效）。
    """
    if snapshot is None:
        return False
    if isinstance(snapshot, str):
        text = snapshot.strip()
        if not text or text in ("null", "{}"):
            return False
        return True
    if isinstance(snapshot, Mapping):
        return bool(snapshot)
    return False


# =============================================================================
# R15 —— 可研报告目标总字数（软引导）
# =============================================================================


def normalize_target_words(
    value: Any, default: int = FEASIBILITY_DEFAULT_TARGET_WORDS
) -> int:
    """可研报告目标总字数归一化（R15）。

    规则：非法/空值回落到默认 30000；有效值再钳制到硬下限 1000
    （"目标总字数不能低于 1000"）。

    可研报告模块只有这一条软引导（大纲 Prompt 注入目标总字数），
    **不构成闭环强控**，与技术方案模块的 ±20% 强控区分开。
    """
    number = _to_non_negative_int(value)
    if number <= 0:
        return default
    return max(FEASIBILITY_MIN_TARGET_WORDS, number)

