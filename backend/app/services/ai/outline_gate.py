"""目录生成审核门禁（确定性事实层）。

ENHANCE 2026-09-14：移植自易标 `outlineGenerationTaskV2.cjs` 的 `outline_review` 阶段。

参考实现的核心设计不是"两道会短路的门禁"，而是**宿主先算事实、AI 拿着事实做语义审**——
让 AI 别自己去数节点、自己去找重复。这样既省 token，也避免 AI 数错。
本模块只负责"算事实 + 判确定性规则 + 拼提示词 + 映射结论"，**不发起 AI 调用**
（AI 调用留在 `workflows_outline_v51._ai_deep_check`，避免循环导入）。

相对参考实现补上的几项（都是本软件实际踩过的坑）：
  - **占位符残留检测**：`__XXX__` / `{{}}` / `待补充`。框架会打印 missing variables 警告，
    但此前没有任何地方检查最终目录里是否真的残留了占位符。
  - **id 缺失 / 非法 / 不连续检测**：参考实现有 JSON Schema 校验
    （`^[1-9]\\d*(?:\\.[1-9]\\d*){level-1}$`），本软件此前只在生成时赋值、**从不校验**。
    实测 `_ensure_min_children` 补全的新节点不带 `id`，且它在 id 分配之后运行。
  - **标题长度上下限**。
  - **评分项覆盖的标题级比对**：原 `_check_scoring_coverage` 只比数量，
    8 个完全跑偏的章节也能通过。

设计立场：**门禁只产出结论，不做阻断**。AI 审查失败时降级为"仅确定性结论"并放行——
不因为一次审查超时就毁掉整份已经生成好的目录（参考实现在这里会直接让任务失败）。
"""

from __future__ import annotations

import json
import logging
import math
import re
from typing import Any

# 工程逻辑分区的关键词表与权重表复用排序器，不在本模块放第二份副本——
# 排序器（改顺序）与门禁（判顺序）必须共用同一套判据，否则会出现
# "排序器认为合规、门禁却报乱序"的自相矛盾。
from ...utils.scoring_points import is_scoring_placeholder
from .outline_sort_optimizer import CORE_ZONE_MAP, KEYWORD_ZONE_MAP, ZONE_ORDER
from .workflows_outline_utils import _is_title_duplicate, _normalize_title_core, _title_similarity


logger = logging.getLogger(__name__)


# ========== 常量与阈值 ==========

# AI 审查触发阈值（与原 AI 深度检查保持一致，避免凭空改变既有延迟特征）
OUTLINE_GATE_AI_NODE_THRESHOLD = 80

# 叶子数目标容差（参考实现用 ±2）
LEAF_TOLERANCE = 2

# 单章节默认字数预算（参考实现 sectionWords 默认值）
DEFAULT_SECTION_WORDS = 3000

# 标题长度上下限（中文字符数）
TITLE_MIN_CHARS = 3
TITLE_MAX_CHARS = 30

# 评分项覆盖判定：匹配强度达到该值即视为覆盖（见 `_score_match_strength`）
SCORE_MATCH_THRESHOLD = 0.5

# 近义重复检测的规模上限：全量两两比对是 O(n²)，目录节点过多时只查完全重复
_NEAR_DUP_NODE_LIMIT = 800

# id 分段必须是正整数（无前导零），与参考实现的 schema 一致
_POSITIVE_INT_RE = re.compile(r"^[1-9]\d*$")

# 字数预算解析（用于推导目标叶子数）
#   区间：10000~20000 字 / 1 万~2 万字 / 3-5万字
_WORD_RANGE_RE = re.compile(
    r"(\d{1,7})\s*(万?)\s*(?:~|～|-|—|－|至|到)\s*(\d{1,7})\s*(万?)\s*字"
)
#   下限：不少于 20000 字 / 不低于 2 万字 / 至少 3 万字以上
_WORD_MIN_RE = re.compile(
    r"(?:不少于|不低于|不小于|不得少于|至少|最少)\s*(\d{1,7})\s*(万?)\s*字"
)
#   上限：不超过 20000 字 / 不高于 2 万字 / 最多 3 万字
_WORD_MAX_RE = re.compile(
    r"(?:不超过|不高于|不大于|不得超过|至多|最多)\s*(\d{1,7})\s*(万?)\s*字"
)
#   总量：约 30000 字 / 总计 3 万字 / 2 万字以上
_WORD_TOTAL_RE = re.compile(
    r"(?:约|大约|总计|共计|合计|全文|总字数)?\s*(\d{1,7})\s*(万?)\s*字(?:以上|左右|以内|以下)?"
)

# 字数预算的最小可信值（BUG-FIX 2026-09-15）。
# 低于该值基本是"每页 N 字""限 N 字"这类排版格式要求，不是标书正文的总字数
# 预算。原实现没有这道护栏，招标全文里随便一句"每页 500 字"都会被当成全文
# 预算，`derive_target_leaf_count` 据此推出 1 个目标叶子，leaf-count 检查随即
# 对一份几百节点的正常目录报出"叶子数严重不足"的误报。
MIN_PLAUSIBLE_WORD_BUDGET = 1000

# 占位符 / 未渲染残留规则：(标记名, 正则)
_PLACEHOLDER_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("双下划线占位符", re.compile(r"__[A-Za-z0-9_]{2,}__")),
    ("花括号占位符", re.compile(r"\{\{?[A-Za-z0-9_\u4e00-\u9fff]{1,40}\}?\}")),
    ("待补充标记", re.compile(r"待补充|待完善|待填写|待定|待确认|TBD|TODO|X{3,}|示例文本|（略）|\(略\)")),
)

# 类别策略：类别 → (严重度, 是否需要用户确认)
#   confirmation_required=False 的条目理论上可机械修复，不必打断用户；
#   True 的条目需要用户判断（例如"评分项没被覆盖"要决定是加章节还是改标题）。
CATEGORY_POLICY: dict[str, tuple[str, bool]] = {
    "placeholder": ("error", False),
    "invalid-id": ("error", False),
    "empty-title": ("error", False),
    "duplicate-title": ("error", False),
    "single-child": ("warning", False),
    "title-length": ("warning", False),
    "near-duplicate": ("warning", True),
    "leaf-count": ("warning", True),
    "score-coverage": ("warning", True),
    "score-unexpected": ("info", True),
    "structure": ("error", True),
    "professional-structure": ("warning", True),
    # 工程逻辑顺序倒置（ENHANCE 2026-09-15）。
    # 分区靠关键词推断，必然有误判；因此定为「只提示、不需用户确认」——
    # 一旦设 confirmation_required=True，单这一条就会把整份裁决推成
    # user_feedback（`derive_status`），让用户在"逐条确认"弹窗里看到一条
    # 由关键词表猜出来的结论。
    "zone-order": ("warning", False),
    # 以下为既有自检提示词（OUTLINE_V51_SELFCHECK_SYSTEM）的 7 个维度
    "truncated": ("error", True),
    "mixed_lang": ("warning", False),
    "overlap": ("warning", True),
    "count_violation": ("warning", True),
    "parent_word_leak": ("warning", False),
    "generic_l3": ("warning", False),
}


# 类别别名归一化（BUG-FIX 2026-09-15）。
# 宿主产出的类别用连字符（`score-coverage`），而 AI 自检提示词要求的下划线写法
# （`parent_word_leak`）以及模型自由发挥的变体（`score_coverage`、`Score_Coverage`）
# 会以不同拼写进来。不做归一化时 `_policy` 取不到策略表条目、落到默认的
# `("warning", True)` —— 既比策略表定义的严重度更严格，又会因
# `confirmation_required=True` 让 `derive_status` 直接把整份裁决判成
# `user_feedback`（要求用户逐条确认），与设计意图不符。
_CATEGORY_ALIASES: dict[str, str] = {
    "score_coverage": "score-coverage",
    "score_coverage_check": "score-coverage",
    "score_unexpected": "score-unexpected",
    "duplicate_title": "duplicate-title",
    "duplicate_titles": "duplicate-title",
    "near_duplicate": "near-duplicate",
    "near_duplicate_titles": "near-duplicate",
    "empty_title": "empty-title",
    "empty_titles": "empty-title",
    "invalid_id": "invalid-id",
    "invalid_ids": "invalid-id",
    "title_length": "title-length",
    "leaf_count": "leaf-count",
    "single_child": "single-child",
    "parent-word-leak": "parent_word_leak",
    "generic-l3": "generic_l3",
    "mixed-lang": "mixed_lang",
    "count-violation": "count_violation",
    "professional_structure": "professional-structure",
    "professional": "professional-structure",
    "structure_check": "structure",
    "zone_order": "zone-order",
    "zone_order_check": "zone-order",
    "chapter_order": "zone-order",
}


def normalize_category(category: Any) -> str:
    """把类别名归一到策略表的 key（去空白、转小写、展开别名）。"""
    key = str(category or "").strip().lower()
    return _CATEGORY_ALIASES.get(key, key)


def _policy(category: str) -> tuple[str, bool]:
    """取类别的（严重度, 是否需要用户确认），未知类别按 warning + 需确认处理。"""
    return CATEGORY_POLICY.get(normalize_category(category), ("warning", True))


# 类别 → 中文短标签。自检提示词只要求 AI 返回 `{"type","target","fix"}`，
# 没有 problem 字段；原实现缺省时直接拿英文类别名当问题描述（"overlap"、
# "generic_l3"），前端展示给用户看的是一串英文标识符。这里补上可读文案。
_CATEGORY_LABELS: dict[str, str] = {
    "truncated": "目录 JSON 可能被截断",
    "mixed_lang": "标题中英混用",
    "overlap": "章节内容重叠",
    "count_violation": "节点数量不合规",
    "parent_word_leak": "三级标题泄漏了父级词汇",
    "generic_l3": "三级标题空泛、缺技术含量",
    "placeholder": "标题残留未渲染占位符",
    "invalid-id": "节点编号缺失或与层级不匹配",
    "empty-title": "节点标题为空",
    "duplicate-title": "标题完全重复",
    "near-duplicate": "存在近义标题",
    "single-child": "存在只有一个子节点的章节",
    "title-length": "标题长度越界",
    "leaf-count": "叶子节点数不在目标区间",
    "score-coverage": "评分要点未被章节覆盖",
    "score-unexpected": "存在不对应任何评分要点的章节",
    "structure": "目录结构异常",
    "professional-structure": "专业性 / 结构问题",
    "zone-order": "一级章节的工程逻辑顺序倒置",
}

# 纯提示性类别：宿主能机械判出来、但 AI 语义审帮不上忙的问题。
# 这类问题不触发额外的 AI 审查调用（`should_run_ai_review`）——
# 否则目录里只要有一处顺序倒置就多跑一次大模型，只为得到一个
# 与宿主结论无关的语义评价。
_ADVISORY_CATEGORIES: frozenset[str] = frozenset({"zone-order"})


# ========== 目录树遍历与 id 重排 ==========


def _flatten_outline(outline: list[dict]) -> list[dict]:
    """展平目录树。

    返回每条记录：{node, level, path, parent_path, node_id, title, child_count}
    path 用 "/" 连接各级 id（id 缺失时回退为位置序号），便于定位到具体节点。
    """
    records: list[dict] = []

    def _walk(nodes: Any, level: int, parent_path: str) -> None:
        if not isinstance(nodes, list):
            return
        for index, node in enumerate(nodes, 1):
            if not isinstance(node, dict):
                continue
            node_id = str(node.get("id") or "").strip()
            label = node_id or f"#{index}"
            path = f"{parent_path}/{label}" if parent_path else label
            children = node.get("children")
            child_count = len(children) if isinstance(children, list) else 0
            records.append(
                {
                    "node": node,
                    "level": level,
                    "path": path,
                    "parent_path": parent_path,
                    "node_id": node_id,
                    "title": str(node.get("title") or "").strip(),
                    "child_count": child_count,
                }
            )
            _walk(children, level + 1, path)

    _walk(outline, 1, "")
    return records


def _id_matches_level(node_id: str, level: int) -> bool:
    """id 是否与层级匹配：段数等于层级，且每段都是无前导零的正整数。

    对应参考实现的 `^[1-9]\\d*(?:\\.[1-9]\\d*){level-1}$`：
    L1 形如 "1"，L2 形如 "1.2"，L3 形如 "1.2.3"。
    """
    parts = str(node_id or "").split(".")
    if len(parts) != level:
        return False
    return all(_POSITIVE_INT_RE.match(part) for part in parts)


def renumber_outline_ids(outline: list[dict]) -> tuple[list[dict], int]:
    """按位置重排整棵树的 id，返回 (新目录, 变更节点数)。

    只在门禁判定 id 缺失/非法/重复后调用。按位置重排能同时修好三种问题，
    且保证 L1/L2/L3 编号严格连续（1 / 1.1 / 1.1.1）。

    ENHANCE 2026-10-12（P0-1）：如果 config.OUTLINE_NUMBERING_ENABLED=true，
    同时用 outline_numbering 引擎计算 `number` 字段（模板化编号），
    原 id 字段保持不变以维持向后兼容。
    """
    changed = 0

    def _walk(nodes: Any, prefix: str, level: int) -> None:
        nonlocal changed
        if not isinstance(nodes, list):
            return
        for index, node in enumerate(nodes, 1):
            if not isinstance(node, dict):
                continue
            new_id = f"{prefix}{index}" if not prefix else f"{prefix}.{index}"
            if str(node.get("id") or "") != new_id:
                node["id"] = new_id
                changed += 1
            _walk(node.get("children"), new_id, level + 1)

    _walk(outline, "", 1)

    # ENHANCE 2026-10-12（P0-1）：可选的模板化编号
    try:
        from ...config import OUTLINE_NUMBERING_ENABLED, OUTLINE_NUMBERING_TEMPLATE
        if OUTLINE_NUMBERING_ENABLED:
            from .outline_numbering import renumber_outline_with_template
            renumber_outline_with_template(outline, mutate=True)
    except Exception:
        # 静默失败，不阻断主流程
        pass

    return outline, changed


# ========== 字数预算解析 ==========


def parse_word_budget(text: Any) -> dict[str, int | None]:
    """从招标要求 / 评分要点文本里解析字数预算。

    支持「不少于 20000 字」「2 万字以上」「10000~20000 字」「约 3 万字」等常见写法。
    解析不到时三个字段都是 None，调用方据此跳过叶子数区间检查。

    返回 `{"min_words", "max_words", "total_words"}`，可直接展开传给
    `derive_target_leaf_count`。
    """
    result: dict[str, int | None] = {"min_words": None, "max_words": None, "total_words": None}
    if not isinstance(text, str) or not text.strip():
        return result

    def _num(raw: str, has_unit: bool) -> int | None:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        if value <= 0:
            return None
        total = value * 10000 if has_unit else value
        # 低于可信下限的一律当噪声丢弃（见 MIN_PLAUSIBLE_WORD_BUDGET）
        return total if total >= MIN_PLAUSIBLE_WORD_BUDGET else None

    def _omits_unit(raw: str) -> bool:
        """该数值是否属于「省略了万」的写法（如「1~2万字」的前侧）。

        只有三位以内的数才可能省略量级词；`10000~2万字` 的前侧已是绝对值。
        """
        try:
            return int(raw) < 1000
        except (TypeError, ValueError):
            return False

    # 1. 区间优先（区间信息量最大，且能同时给出上下限）
    match = _WORD_RANGE_RE.search(text)
    if match:
        low_raw, high_raw = match.group(1), match.group(3)
        low_unit, high_unit = bool(match.group(2)), bool(match.group(4))
        # BUG-FIX 2026-09-15（中）：原实现无条件取 `unit = low_unit or high_unit`，
        # 把两侧按同一量级处理。这对「1 万~2 万字」是对的，但「10000~2 万字」的
        # 前侧本来就是绝对值，再乘 10000 会得到 1 亿字，目标叶子数被推导成
        # 16670，leaf-count 检查随即误报。改为只在**小数值一侧**补量级词。
        if low_unit and not high_unit and _omits_unit(high_raw):
            high_unit = True
        elif high_unit and not low_unit and _omits_unit(low_raw):
            low_unit = True
        low = _num(low_raw, low_unit)
        high = _num(high_raw, high_unit)
        if low and high:
            result["min_words"], result["max_words"] = min(low, high), max(low, high)

    # 2. 下限
    if result["min_words"] is None:
        match = _WORD_MIN_RE.search(text)
        if match:
            result["min_words"] = _num(match.group(1), bool(match.group(2)))

    # 3. 上限
    if result["max_words"] is None:
        match = _WORD_MAX_RE.search(text)
        if match:
            result["max_words"] = _num(match.group(1), bool(match.group(2)))

    # 4. 总量（「约 3 万字」这类只给单值的写法）
    if result["min_words"] is None and result["max_words"] is None:
        match = _WORD_TOTAL_RE.search(text)
        if match:
            result["total_words"] = _num(match.group(1), bool(match.group(2)))

    return result


# ========== 目标叶子数推导（移植自参考实现 deriveTargetLeafCount） ==========


def derive_target_leaf_count(
    total_words: int | None = None,
    section_words: int = DEFAULT_SECTION_WORDS,
    min_words: int | None = None,
    max_words: int | None = None,
) -> int | None:
    """按字数预算推导目标叶子（三级节点）数量。

    参考实现的算法：
      - min 与 max 都有 → ceil(((min+max)/2) / sectionWords)
      - 只有 max        → floor(max / sectionWords) - 2
      - 只有 min        → ceil(min / sectionWords) + 2

    无法推导（全为空或非正数）时返回 None，调用方跳过叶子数区间检查。
    """
    try:
        section_words = int(section_words)
    except (TypeError, ValueError):
        return None
    if section_words <= 0:
        return None

    def _as_int(value: Any) -> int | None:
        try:
            number = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    low = _as_int(min_words)
    high = _as_int(max_words)
    total = _as_int(total_words)

    if low is not None and high is not None:
        return max(1, math.ceil((low + high) / 2 / section_words))
    if high is not None:
        return max(1, high // section_words - 2)
    if low is not None:
        return max(1, math.ceil(low / section_words) + 2)
    if total is not None:
        return max(1, math.ceil(total / section_words))
    return None


# ========== 评分项解析 ==========


def parse_scoring_item_titles(scoring_points: Any) -> list[str]:
    """从评分点数据中提取标题列表。

    接受 JSON 字符串 / list / dict（dict 取 items 或 scoring_items），
    兼容 name / title / 标题 三种字段名。
    """
    data = scoring_points
    if isinstance(data, str):
        text = data.strip()
        if not text:
            return []
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            # 不是 JSON：按行拆成候选标题，至少比"完全没有评分项"强
            return [line.strip() for line in text.splitlines() if line.strip()][:200]

    if isinstance(data, dict):
        data = data.get("items") or data.get("scoring_items") or data.get("data") or []
    if not isinstance(data, list):
        return []

    titles: list[str] = []
    for item in data:
        if isinstance(item, str):
            title = item.strip()
        elif isinstance(item, dict):
            title = str(item.get("name") or item.get("title") or item.get("标题") or "").strip()
            # 过滤 normalize_writing_targets 插入的系统提示项
            # ENHANCE 2026-09-18：判据收敛到 app.utils.scoring_points，
            # 新增伪项类型只需登记一处，不必逐个模块改字符串字面量。
            if is_scoring_placeholder(item):
                continue
        else:
            continue
        if title and title not in titles:
            titles.append(title)
    return titles


# ========== 工程逻辑分区顺序（ENHANCE 2026-09-15）==========

UNKNOWN_ZONE = "未知分区"

# 核心 28 章标题 → 分区（小写索引，避免每次调用都重建集合）
_CORE_ZONE_BY_LOWER: dict[str, str] = {title.lower(): zone for title, zone in CORE_ZONE_MAP.items()}

# 单次最多报多少条倒置。目录最多几十个一级章节，但关键词误判可能连锁放大，
# 这里兜个上限，避免一条规则刷出满屏 issue 把真正的问题淹掉。
_ZONE_MAX_REPORTED = 8

# 是否允许工作流对「一级章节工程逻辑顺序」做机械重排（ENHANCE 2026-09-15）。
# 招标文件若明确指定了目录顺序，把它置 False —— 此时门禁仍然在裁决里提示，
# 只是不再替用户改动顺序。
APPLY_ZONE_REORDER = True


def infer_chapter_zone(title: str) -> str:
    """推断一级章节所属的工程逻辑分区，推断不出时返回 `UNKNOWN_ZONE`。

    判定顺序：核心 28 章精确匹配 → 关键词表（同组内长关键词优先，
    与 `outline_sort_optimizer._infer_zone_by_keywords` 保持一致）。
    """
    text = str(title or "").strip()
    if not text:
        return UNKNOWN_ZONE
    zone = _CORE_ZONE_BY_LOWER.get(text.lower())
    if zone:
        return zone
    lowered = text.lower()
    for keywords, mapped_zone in KEYWORD_ZONE_MAP:
        for keyword in sorted(keywords, key=len, reverse=True):
            if keyword in lowered:
                return mapped_zone
    return UNKNOWN_ZONE


def detect_zone_order_violations(zone_sequence: list[dict]) -> list[dict]:
    """在「一级章节 → 分区」序列里找出工程逻辑顺序倒置的章节对。

    判据：前面的章节分区权重 **严格大于** 后面某个章节的分区权重
    （权重见 `ZONE_ORDER`，越小越靠前）。

    两条刻意的保守：
      - 分区为 `UNKNOWN_ZONE` 的章节**完全不参与判定**。关键词表覆盖不到
        不等于放错了位置，把它们算进来会让"施工组织设计"这类通用标题
        把整份目录误判成乱序。
      - 每个"放得太靠前"的章节只产出一条记录（指向其后第一个分区更小的
        章节），不做两两全枚举——否则一处误判会炸出 O(n²) 条 issue。
    """
    known = [item for item in zone_sequence or [] if item.get("zone") and item["zone"] != UNKNOWN_ZONE]
    violations: list[dict] = []
    for idx, item in enumerate(known):
        weight = ZONE_ORDER.get(item["zone"])
        if weight is None:
            continue
        later = [
            other
            for other in known[idx + 1 :]
            if (ZONE_ORDER.get(other.get("zone")) is not None)
            and ZONE_ORDER[other["zone"]] < weight
        ]
        if not later:
            continue
        first = later[0]
        violations.append(
            {
                "earlier": {
                    "id": item.get("id", ""),
                    "path": item.get("path", ""),
                    "title": item.get("title", ""),
                },
                "later": {
                    "id": first.get("id", ""),
                    "path": first.get("path", ""),
                    "title": first.get("title", ""),
                },
                "earlier_zone": item["zone"],
                "later_zone": first["zone"],
                "later_count": len(later),
            }
        )
        if len(violations) >= _ZONE_MAX_REPORTED:
            break
    return violations


def reorder_by_zone(outline: list[dict]) -> tuple[list[dict], list[dict]]:
    """按工程逻辑分区**机械重排**一级章节（无 AI、不改标题、不增删节点）。

    返回 `(新目录, 移动记录)`；不存在倒置时原样返回（同一批节点对象，零改动）。

    三条保守性约束，缺一不可：
      - `未知分区` 章节当**锚点**：位置绝对不变，其它章节也不会跨过锚点。
        关键词表推断不出分区时让它参与排序，等于拿一张关键词表替用户决定
        「施工组织设计」该放哪儿。
      - 同分区内**保持原有相对顺序**（稳定排序）。本函数只解决"分区层级倒置"，
        不对同分区内的先后做任何主张。
      - 重排后统一走 `renumber_outline_ids`，保证 id 与位置一致 ——
        否则前端按 id 定位编辑会改错节点（编号即位置的隐含契约）。
    """
    nodes = list(outline or [])
    sequence = [
        {
            "id": str(node.get("id") or idx + 1),
            "path": str(node.get("id") or idx + 1),
            "title": str(node.get("title") or ""),
            "zone": infer_chapter_zone(str(node.get("title") or "")),
        }
        for idx, node in enumerate(nodes)
    ]
    if not detect_zone_order_violations(sequence):
        return nodes, []

    # 以锚点切段：段内稳定排序，段间（锚点两侧）不互换
    segments: list[list[tuple[int | None, int]]] = []
    current: list[tuple[int | None, int]] = []
    for idx, item in enumerate(sequence):
        # 注意：`ZONE_ORDER` 里显式登记了 `"未知分区": 5`（排序器用它当默认权重），
        # 所以判定锚点必须比 zone 名，**不能**靠 `ZONE_ORDER.get(...) is None` ——
        # 那样锚点会永远失效（实测「施工组织设计」两侧章节被互换）。
        zone = item["zone"]
        weight = None if zone == UNKNOWN_ZONE else ZONE_ORDER.get(zone)
        if weight is None:
            segments.append(current)
            segments.append([(None, idx)])
            current = []
        else:
            current.append((weight, idx))
    segments.append(current)

    new_order: list[int] = []
    for segment in segments:
        if not segment:
            continue
        anchors = [idx for weight, idx in segment if weight is None]
        if anchors:
            new_order.extend(anchors)
            continue
        new_order.extend(idx for _weight, idx in sorted(segment, key=lambda pair: (pair[0], pair[1])))

    if new_order == list(range(len(nodes))):
        return nodes, []

    moved = [
        {"title": sequence[old]["title"], "from": old + 1, "to": new + 1}
        for new, old in enumerate(new_order)
        if new != old
    ]
    reordered, _changed = renumber_outline_ids([nodes[i] for i in new_order])
    return reordered, moved


# ========== 确定性事实包 ==========


def _bigram_coverage(a: str, b: str) -> float:
    """`a` 的二元组有多大比例出现在 `b` 里（a 为空返回 0）。

    与 `_title_similarity`（二元组 Jaccard）的区别：Jaccard 的分母随 `b` 变长而膨胀，
    覆盖率只看 `a` 自身被覆盖了多少——`b` 扩写不影响它。
    """
    na = _normalize_title_core(a)
    nb = _normalize_title_core(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    set_a = {na[i : i + 2] for i in range(len(na) - 1)} if len(na) > 1 else {na}
    set_b = {nb[i : i + 2] for i in range(len(nb) - 1)} if len(nb) > 1 else {nb}
    if not set_a:
        return 0.0
    return len(set_a & set_b) / len(set_a)


def _score_match_strength(score_title: str, chapter_title: str) -> float:
    """评分项标题与一级章节标题的"覆盖强度"（0~1）。

    BUG-FIX 2026-09-15（高）：原实现只用二元组 Jaccard（`_title_similarity`），
    而 L1 产出的章节标题是「评分项的专业化扩写」，扩写只增大 Jaccard 的分母、
    把分数压到阈值以下，于是**明明覆盖了的评分项被报成 missing**。实测：

        质量保证措施   vs 质量管理体系与保证措施    = 0.364（漏报）
        文明施工措施   vs 文明施工与环境保护措施    = 0.364（漏报）
        应急预案       vs 应急预案与风险管理        = 0.375（漏报）
        劳动力计划     vs 劳动力与机械设备配置计划   = 0.250（漏报）
        施工组织设计   vs 施工组织总体设计          = 0.500（临界）

    而真正不相关的组合最高只有 0.143（质量管理体系 vs 文明施工管理措施），
    两者并没有重叠区间，说明不是"阈值偏低"而是"指标选错了"。

    补上二元组覆盖率（只看评分项自身被覆盖多少，扩写不影响分子）后上述五组
    分别升到 0.8 / 0.8 / 1.0 / 0.75 / 0.8；不相关组合最高 0.33，
    仍在阈值 0.5 之下——召回上来了，精度没有损失。

    原实现还有一条"评分项整体是章节标题的子串 → 0.9"的规则，这里不再单独保留：
    子串包含时评分项的全部二元组必然都出现在章节里，覆盖率本就是 1.0，
    旧规则被严格支配（留着只是死代码）。
    """
    return max(
        _title_similarity(score_title, chapter_title),
        _bigram_coverage(score_title, chapter_title),
    )


def compute_outline_facts(
    outline: list[dict],
    *,
    scoring_titles: list[str] | None = None,
    expected_leaf_count: int | None = None,
) -> dict:
    """算出目录的确定性事实包（宿主负责，AI 不需要自己再数一遍）。"""
    records = _flatten_outline(outline)

    levels: dict[int, int] = {}
    max_depth = 0
    leaf_records = []
    single_child: list[dict] = []
    empty_titles: list[str] = []
    placeholder_hits: list[dict] = []
    invalid_ids: list[dict] = []
    title_length_hits: list[dict] = []

    for record in records:
        level = record["level"]
        levels[level] = levels.get(level, 0) + 1
        max_depth = max(max_depth, level)

        if record["child_count"] == 0:
            leaf_records.append(record)
        elif record["child_count"] == 1:
            single_child.append(
                {
                    "id": record["node_id"],
                    "path": record["path"],
                    "level": level,
                    "title": record["title"],
                    "child_count": 1,
                }
            )

        if not record["title"]:
            empty_titles.append(record["path"])
            continue

        if not _id_matches_level(record["node_id"], level):
            invalid_ids.append(
                {
                    "id": record["node_id"],
                    "path": record["path"],
                    "level": level,
                    "title": record["title"],
                }
            )

        for marker, pattern in _PLACEHOLDER_RULES:
            matched = pattern.search(record["title"])
            if matched:
                placeholder_hits.append(
                    {
                        "id": record["node_id"],
                        "path": record["path"],
                        "title": record["title"],
                        "marker": marker,
                        "matched": matched.group(0),
                    }
                )
                break

        title_len = len(record["title"])
        if title_len < TITLE_MIN_CHARS or title_len > TITLE_MAX_CHARS:
            title_length_hits.append(
                {
                    "id": record["node_id"],
                    "path": record["path"],
                    "title": record["title"],
                    "length": title_len,
                    "expected": f"{TITLE_MIN_CHARS}~{TITLE_MAX_CHARS}",
                }
            )

    # 遗留修复（编号冲突）：`invalid_ids` 原先只覆盖「缺失 / 与层级不匹配」，
    # 检测不到**重复编号** —— 两个节点都是 "1.1" 时格式完全合法、层级也对，
    # 却破坏了「编号即位置」的隐含契约（前端按 id 定位编辑会改错节点）。
    # 这里把重复编号一并归入 invalid_ids，复用既有的 invalid-id 自动重排链路，
    # 无需新增问题类别或改动门禁策略。
    _id_counts: dict[str, int] = {}
    for record in records:
        node_id = str(record["node_id"] or "").strip()
        if node_id:
            _id_counts[node_id] = _id_counts.get(node_id, 0) + 1
    _dup_ids = {i for i, c in _id_counts.items() if c > 1}
    if _dup_ids:
        for record in records:
            if str(record["node_id"] or "").strip() in _dup_ids:
                invalid_ids.append(
                    {
                        "id": record["node_id"],
                        "path": record["path"],
                        "level": record["level"],
                        "title": record["title"],
                    }
                )

    # 完全重复标题（归一化后相等）
    # 作用域：**同一父节点下**。跨父同名是标书合法形态（3.1 / 4.1 各自一节），
    # 报成 error 会误伤正常目录；只有同父下的真·重复才需要限定语。
    # 归一化用 `_title_core`（剥离「（其N）」等冗余后缀），否则遗留数据里
    # 「X」与「X（其2）」会被当成两个不同标题而漏检。
    from .outline_common import _title_core

    exact_groups: dict[tuple[str, str], list[dict]] = {}
    for record in records:
        if not record["title"]:
            continue
        key = _title_core(record["title"])
        if key:
            exact_groups.setdefault((record["parent_path"], key), []).append(
                {"path": record["path"], "id": record["node_id"], "title": record["title"]}
            )
    duplicate_titles = [
        {"title": group[0]["title"], "nodes": group}
        for group in exact_groups.values()
        if len(group) > 1
    ]

    # 近义重复标题（同父节点下同层级两两比对，规模过大时跳过，见 _NEAR_DUP_NODE_LIMIT）
    near_duplicates: list[dict] = []
    near_dup_skipped = False
    titled = [r for r in records if r["title"]]
    if len(titled) <= _NEAR_DUP_NODE_LIMIT:
        for i, left in enumerate(titled):
            for right in titled[i + 1 :]:
                if left["level"] != right["level"]:
                    continue
                # 跨父节点的同层标题是合法的（不同章节各一节），不参与重复判定
                if left["parent_path"] != right["parent_path"]:
                    continue
                # 已被完全重复覆盖的组不再重复报近义
                if _title_core(left["title"]) == _title_core(right["title"]):
                    continue
                is_dup, score = _is_title_duplicate(left["title"], right["title"])
                if is_dup:
                    near_duplicates.append(
                        {
                            "a": {"id": left["node_id"], "path": left["path"], "title": left["title"]},
                            "b": {
                                "id": right["node_id"],
                                "path": right["path"],
                                "title": right["title"],
                            },
                            "similarity": round(float(score), 3),
                        }
                    )
    else:
        near_dup_skipped = True
        logger.warning(
            "目录节点数 %d 超过 %d，跳过近义重复检测（仅检测完全重复）",
            len(titled),
            _NEAR_DUP_NODE_LIMIT,
        )

    # 叶子数区间
    leaf_count = len(leaf_records)
    leaf_range = None
    leaf_within_range = None
    if expected_leaf_count:
        leaf_range = [
            max(1, int(expected_leaf_count) - LEAF_TOLERANCE),
            int(expected_leaf_count) + LEAF_TOLERANCE,
        ]
        leaf_within_range = leaf_range[0] <= leaf_count <= leaf_range[1]

    # 评分项覆盖（标题级比对）
    score_coverage = None
    if scoring_titles:
        matched: list[dict] = []
        missing: list[str] = []
        # 只在 L1 标题里找覆盖：一级章节本就该一一对应评分要点
        l1_titles = [r for r in records if r["level"] == 1 and r["title"]]
        used_paths: set[str] = set()
        for score_title in scoring_titles:
            best_path, best_score = None, 0.0
            for record in l1_titles:
                if record["path"] in used_paths:
                    continue
                sim = _score_match_strength(score_title, record["title"])
                if sim > best_score:
                    best_path, best_score = record["path"], sim
            if best_path and best_score >= SCORE_MATCH_THRESHOLD:
                used_paths.add(best_path)
                matched.append(
                    {"score_title": score_title, "chapter_path": best_path, "similarity": round(best_score, 3)}
                )
            else:
                missing.append(score_title)
        unexpected = [
            {"path": r["path"], "title": r["title"]}
            for r in l1_titles
            if r["path"] not in used_paths
        ]
        score_coverage = {
            "matched": matched,
            "missing": missing,
            "unexpected": unexpected,
            "valid": not missing and not unexpected,
        }

    # 一级章节的工程逻辑分区与顺序（ENHANCE 2026-09-15）。
    # 只检测、不重排：门禁的立场是"出结论"，改顺序的风险由人/上游承担。
    zone_sequence = [
        {
            "id": r["node_id"],
            "path": r["path"],
            "title": r["title"],
            "zone": infer_chapter_zone(r["title"]),
        }
        for r in records
        if r["level"] == 1 and r["title"]
    ]
    zone_order_violations = detect_zone_order_violations(zone_sequence)

    structure_valid = (
        max_depth <= 6
        and not single_child
        and not empty_titles
        and not invalid_ids
        and not placeholder_hits
    )

    return {
        "node_count": len(records),
        "level1_count": levels.get(1, 0),
        "leaf_count": leaf_count,
        "target_leaf_count": int(expected_leaf_count) if expected_leaf_count else None,
        "leaf_acceptable_range": leaf_range,
        "leaf_within_range": leaf_within_range,
        "max_depth": max_depth,
        "node_counts_by_level": {str(k): v for k, v in sorted(levels.items())},
        "single_child_nodes": single_child,
        "empty_title_nodes": empty_titles,
        "invalid_id_nodes": invalid_ids,
        "placeholder_titles": placeholder_hits,
        "title_length_violations": title_length_hits,
        "duplicate_titles": duplicate_titles,
        "near_duplicate_titles": near_duplicates,
        "near_duplicate_skipped": near_dup_skipped,
        "score_coverage": score_coverage,
        "zone_sequence": zone_sequence,
        "zone_order_violations": zone_order_violations,
        "structure_valid": structure_valid,
    }


# ========== 事实 → 结构化 issue ==========


def collect_deterministic_issues(facts: dict) -> list[dict]:
    """把事实包转成结构化 issue 列表（含严重度与是否需要用户确认）。"""

    def _issue(category: str, problem: str, repair: str, node_ids: list[str] | None = None) -> dict:
        severity, needs_confirm = _policy(category)
        return {
            "category": normalize_category(category),
            "severity": severity,
            "problem": problem,
            "repair": repair,
            "confirmation_required": needs_confirm,
            "source": "host",
            "node_ids": node_ids or [],
        }

    issues: list[dict] = []
    facts = facts or {}

    for item in facts.get("placeholder_titles") or []:
        issues.append(
            _issue(
                "placeholder",
                f"标题残留未渲染占位符：{item['title']}",
                f"替换占位符「{item['matched']}」为真实内容",
                [item.get("id") or item.get("path", "")],
            )
        )

    if facts.get("invalid_id_nodes"):
        ids = [i.get("id") or "(缺失)" for i in facts["invalid_id_nodes"]]
        issues.append(
            _issue(
                "invalid-id",
                f"{len(ids)} 个节点的编号缺失、重复或与层级不匹配：{', '.join(ids[:8])}",
                "按位置重排整棵树的编号（L1=1、L2=1.1、L3=1.1.1），同时消除重复编号",
                [str(i) for i in ids],
            )
        )

    if facts.get("empty_title_nodes"):
        issues.append(
            _issue(
                "empty-title",
                f"{len(facts['empty_title_nodes'])} 个节点标题为空",
                "依据父级标题语义派生标题，或删除该节点",
                list(facts["empty_title_nodes"]),
            )
        )

    for group in facts.get("duplicate_titles") or []:
        paths = [n.get("path", "") for n in group.get("nodes", [])]
        issues.append(
            _issue(
                "duplicate-title",
                f"标题完全重复：{group.get('title')}（{len(paths)} 处）",
                "为重复标题补充限定语以区分侧重点",
                paths,
            )
        )

    for item in facts.get("near_duplicate_titles") or []:
        issues.append(
            _issue(
                "near-duplicate",
                "近义标题："
                f"{item['a']['title']}（{item['a']['path']}）与 "
                f"{item['b']['title']}（{item['b']['path']}），相似度 {item['similarity']}",
                "确认是否需要区分侧重点；若确为同一内容应合并",
                [item["a"].get("id", ""), item["b"].get("id", "")],
            )
        )

    if facts.get("single_child_nodes"):
        titles = [n.get("title") or n.get("path", "") for n in facts["single_child_nodes"]]
        issues.append(
            _issue(
                "single-child",
                f"{len(titles)} 个节点只有 1 个子节点：{', '.join(titles[:8])}",
                "补充子节点至至少 2 个（单子节点在标书中显得结构失衡）",
                [str(n.get("id") or n.get("path", "")) for n in facts["single_child_nodes"]],
            )
        )

    for item in facts.get("title_length_violations") or []:
        issues.append(
            _issue(
                "title-length",
                f"标题长度 {item['length']} 超出 {item['expected']}：{item['title']}",
                "精简或补充标题，使其落在建议长度区间内",
                [str(item.get("id") or item.get("path", ""))],
            )
        )

    if facts.get("leaf_within_range") is False:
        issues.append(
            _issue(
                "leaf-count",
                f"叶子节点数 {facts['leaf_count']} 不在目标区间 {facts['leaf_acceptable_range']}"
                f"（目标 {facts['target_leaf_count']}）",
                "按目标区间增补或合并三级节点",
            )
        )

    coverage = facts.get("score_coverage")
    if coverage and coverage.get("missing"):
        issues.append(
            _issue(
                "score-coverage",
                f"{len(coverage['missing'])} 个评分要点未在章节标题中体现："
                f"{', '.join(coverage['missing'][:6])}",
                "补充对应章节，或调整现有章节标题以覆盖该评分要点",
            )
        )
    if coverage and coverage.get("unexpected"):
        titles = [u.get("title", "") for u in coverage["unexpected"]]
        issues.append(
            _issue(
                "score-unexpected",
                f"{len(titles)} 个章节未对应任何评分要点：{', '.join(titles[:6])}",
                "确认是否为必要的非评分章节；否则考虑合并或删除",
                [u.get("path", "") for u in coverage["unexpected"]],
            )
        )

    for item in facts.get("zone_order_violations") or []:
        earlier = item.get("earlier", {})
        later = item.get("later", {})
        extra = f"（其后还有 {item['later_count'] - 1} 个更靠前的分区）" if item.get("later_count", 0) > 1 else ""
        issues.append(
            _issue(
                "zone-order",
                f"工程逻辑顺序倒置：「{earlier.get('title')}」（{item.get('earlier_zone')}）"
                f"排在「{later.get('title')}」（{item.get('later_zone')}）之前{extra}",
                f"建议将「{earlier.get('title')}」移到「{later.get('title')}」之后，使目录遵循"
                "「宏观总览→前期准备→核心技术→管理保障→进度管控→资源保障→专项预案→协调配合→收尾优化」"
                "的叙事顺序；若招标文件明确要求该顺序则忽略本条",
                [str(earlier.get("id") or earlier.get("path", "")), str(later.get("id") or later.get("path", ""))],
            )
        )

    if facts.get("max_depth") and facts["max_depth"] > 6:
        issues.append(
            _issue(
                "structure",
                f"目录层级深度 {facts['max_depth']} 超过 6 层",
                "压平过深层级，标书目录一般不超过三层",
            )
        )

    return issues


def derive_status(issues: list[dict]) -> str:
    """由 issue 列表推导整体裁决。

    - 无 issue → passed
    - 有 error 级，或存在需要用户确认的条目 → user_feedback（必须让人看一眼）
    - 其余（都是可机械修复的 warning/info）→ simple_fix
    """
    if not issues:
        return "passed"
    if any(i.get("severity") == "error" for i in issues):
        return "user_feedback"
    if any(i.get("confirmation_required") for i in issues):
        return "user_feedback"
    return "simple_fix"


def summarize_issues(issues: list[dict]) -> str:
    """把 issue 列表压成一行摘要，用于日志与进度文案。"""
    if not issues:
        return "未发现问题"
    by_category: dict[str, int] = {}
    for issue in issues:
        category = str(issue.get("category") or "unknown")
        by_category[category] = by_category.get(category, 0) + 1
    return ", ".join(f"{k}×{v}" for k, v in sorted(by_category.items()))


# ========== 送审提示词拼装 ==========


def build_gate_user_prompt(facts: dict, outline_json: str) -> str:
    """拼装送审 user prompt：把宿主算好的事实前置，明确要求 AI 不要重算。

    这是参考实现的关键设计——AI 收到 `outline-review-context.json` 后
    "结构事实直接采用 context 结果，不要重新统计、编写脚本"。
    """
    facts_for_ai = {
        "节点总数": facts.get("node_count"),
        "一级章节数": facts.get("level1_count"),
        "叶子(三级)节点数": facts.get("leaf_count"),
        "目标叶子数": facts.get("target_leaf_count"),
        "叶子数可接受区间": facts.get("leaf_acceptable_range"),
        "最大层级深度": facts.get("max_depth"),
        "各层级节点数": facts.get("node_counts_by_level"),
        "单子节点": facts.get("single_child_nodes"),
        "空标题节点": facts.get("empty_title_nodes"),
        "编号缺失或非法的节点": facts.get("invalid_id_nodes"),
        "残留占位符的标题": facts.get("placeholder_titles"),
        "完全重复的标题": facts.get("duplicate_titles"),
        "近义重复的标题": facts.get("near_duplicate_titles"),
        "评分要点覆盖情况": facts.get("score_coverage"),
        "一级章节分区顺序倒置": facts.get("zone_order_violations"),
        "结构校验是否通过": facts.get("structure_valid"),
    }
    return (
        "【宿主已完成的确定性统计（请直接采用，不要重新统计、不要编写脚本核算）】\n"
        f"{json.dumps(facts_for_ai, ensure_ascii=False, indent=2)}\n\n"
        "【目录全文】\n"
        f"{outline_json}\n\n"
        "请基于上述事实做语义审查，重点判断宿主无法判断的问题："
        "评分要点覆盖是否到位、近义标题该如何区分侧重点、"
        "章节专业性与项目契合度、内容重叠、三级标题是否空泛或泄漏父级词汇。"
    )


def map_ai_issues(ai_issues: Any) -> list[dict]:
    """把既有自检提示词返回的 issues 映射到门禁统一 schema。

    既有提示词（OUTLINE_V51_SELFCHECK_SYSTEM）的 issue 形如
    `{"type": "...", "target"/"node_id"/"nodes": ..., "fix": "..."}`，
    没有严重度与 confirmation_required——这里按类别策略补齐。
    """
    if not isinstance(ai_issues, list):
        return []
    mapped: list[dict] = []
    for item in ai_issues:
        if not isinstance(item, dict):
            continue
        category = normalize_category(item.get("type") or "professional-structure")
        target = item.get("target") or item.get("node_id") or item.get("nodes") or ""
        if isinstance(target, list):
            node_ids = [str(t) for t in target]
        else:
            node_ids = [str(target)] if target else []
        severity, needs_confirm = _policy(category)
        problem = str(item.get("problem") or item.get("issue") or "").strip()
        if not problem:
            problem = _CATEGORY_LABELS.get(category, category)
        mapped.append(
            {
                "category": category,
                "severity": severity,
                "problem": problem,
                "repair": str(item.get("fix") or item.get("repair") or ""),
                "confirmation_required": needs_confirm,
                "source": "ai",
                "node_ids": node_ids,
            }
        )
    return mapped


def should_run_ai_review(facts: dict, deterministic_issues: list[dict]) -> bool:
    """是否需要跑 AI 语义审查。

    默认策略：**有确定性问题时必跑；没问题时只在目录足够大（>80 节点）才跑**。
    这样既不改变既有延迟特征（原实现就是 >80 节点才做 AI 深度检查），
    又保证"宿主发现了问题"时一定拿到语义层面的补充判断。

    `_ADVISORY_CATEGORIES` 里的类别（如 `zone-order`）不计入"有问题"：
    它们是宿主的机械观察，AI 语义审给不出额外信息，不该为它们多跑一次调用。
    """
    actionable = [
        i for i in deterministic_issues if str(i.get("category")) not in _ADVISORY_CATEGORIES
    ]
    if actionable:
        return True
    return int(facts.get("node_count") or 0) > OUTLINE_GATE_AI_NODE_THRESHOLD


def build_verdict(
    facts: dict,
    deterministic_issues: list[dict],
    ai_issues: list[dict] | None = None,
    ai_error: str | None = None,
    ai_skipped: bool = False,
) -> dict:
    """组装门禁最终裁决。

    AI 审查失败时**不阻断**：只记录 ai_error，裁决仍由确定性结论给出。
    """
    issues = list(deterministic_issues) + list(ai_issues or [])
    # 同类同问题去重（确定性层与 AI 层可能报同一件事）
    deduped: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for issue in issues:
        key = (str(issue.get("category")), str(issue.get("problem"))[:80])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(issue)

    return {
        "status": derive_status(deduped),
        "issues": deduped,
        "facts": facts,
        "issue_count": len(deduped),
        "error_count": sum(1 for i in deduped if i.get("severity") == "error"),
        "needs_user_confirmation": sum(1 for i in deduped if i.get("confirmation_required")),
        "ai_reviewed": bool(ai_issues is not None) and not ai_skipped,
        "ai_skipped": ai_skipped,
        "ai_error": ai_error,
        "summary": summarize_issues(deduped),
    }
