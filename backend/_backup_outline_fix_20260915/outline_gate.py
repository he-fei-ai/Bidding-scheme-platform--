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

# 评分项覆盖判定：归一化后相似度达到该值即视为覆盖
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
    # 以下为既有自检提示词（OUTLINE_V51_SELFCHECK_SYSTEM）的 7 个维度
    "truncated": ("error", True),
    "mixed_lang": ("warning", False),
    "overlap": ("warning", True),
    "count_violation": ("warning", True),
    "parent_word_leak": ("warning", False),
    "generic_l3": ("warning", False),
}


def _policy(category: str) -> tuple[str, bool]:
    """取类别的（严重度, 是否需要用户确认），未知类别按 warning + 需确认处理。"""
    return CATEGORY_POLICY.get(str(category or "").strip(), ("warning", True))


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

    def _num(raw: str, unit: str) -> int | None:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        if value <= 0:
            return None
        return value * 10000 if unit else value

    # 1. 区间优先（区间信息量最大，且能同时给出上下限）
    match = _WORD_RANGE_RE.search(text)
    if match:
        low_unit, high_unit = match.group(2), match.group(4)
        # 一侧写了「万」，另一侧通常省略，按同一量级处理（1 万~2 万字）
        unit = low_unit or high_unit
        low = _num(match.group(1), unit)
        high = _num(match.group(3), unit)
        if low and high:
            result["min_words"], result["max_words"] = min(low, high), max(low, high)

    # 2. 下限
    if result["min_words"] is None:
        match = _WORD_MIN_RE.search(text)
        if match:
            result["min_words"] = _num(match.group(1), match.group(2))

    # 3. 上限
    if result["max_words"] is None:
        match = _WORD_MAX_RE.search(text)
        if match:
            result["max_words"] = _num(match.group(1), match.group(2))

    # 4. 总量（「约 3 万字」这类只给单值的写法）
    if result["min_words"] is None and result["max_words"] is None:
        match = _WORD_TOTAL_RE.search(text)
        if match:
            result["total_words"] = _num(match.group(1), match.group(2))

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
            if str(item.get("id") or "") == "__weight_warning__":
                continue
        else:
            continue
        if title and title not in titles:
            titles.append(title)
    return titles


# ========== 确定性事实包 ==========


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

    # 完全重复标题（归一化后相等）
    exact_groups: dict[str, list[dict]] = {}
    for record in records:
        if not record["title"]:
            continue
        key = _normalize_title_core(record["title"])
        if key:
            exact_groups.setdefault(key, []).append(
                {"path": record["path"], "id": record["node_id"], "title": record["title"]}
            )
    duplicate_titles = [
        {"title": group[0]["title"], "nodes": group}
        for group in exact_groups.values()
        if len(group) > 1
    ]

    # 近义重复标题（同层级两两比对，规模过大时跳过，见 _NEAR_DUP_NODE_LIMIT）
    near_duplicates: list[dict] = []
    near_dup_skipped = False
    titled = [r for r in records if r["title"]]
    if len(titled) <= _NEAR_DUP_NODE_LIMIT:
        for i, left in enumerate(titled):
            for right in titled[i + 1 :]:
                if left["level"] != right["level"]:
                    continue
                # 已被完全重复覆盖的组不再重复报近义
                if _normalize_title_core(left["title"]) == _normalize_title_core(right["title"]):
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
                sim = _title_similarity(score_title, record["title"])
                if _normalize_title_core(score_title) and _normalize_title_core(
                    score_title
                ) in _normalize_title_core(record["title"]):
                    sim = max(sim, 0.9)
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
        "structure_valid": structure_valid,
    }


# ========== 事实 → 结构化 issue ==========


def collect_deterministic_issues(facts: dict) -> list[dict]:
    """把事实包转成结构化 issue 列表（含严重度与是否需要用户确认）。"""

    def _issue(category: str, problem: str, repair: str, node_ids: list[str] | None = None) -> dict:
        severity, needs_confirm = _policy(category)
        return {
            "category": category,
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
                f"{len(ids)} 个节点的编号缺失或与层级不匹配：{', '.join(ids[:8])}",
                "按位置重排整棵树的编号（L1=1、L2=1.1、L3=1.1.1）",
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
        category = str(item.get("type") or "professional-structure").strip()
        target = item.get("target") or item.get("node_id") or item.get("nodes") or ""
        if isinstance(target, list):
            node_ids = [str(t) for t in target]
        else:
            node_ids = [str(target)] if target else []
        severity, needs_confirm = _policy(category)
        mapped.append(
            {
                "category": category,
                "severity": severity,
                "problem": str(item.get("problem") or item.get("issue") or category),
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
    """
    if deterministic_issues:
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
