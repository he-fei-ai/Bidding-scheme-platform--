"""正文精确补丁引擎。

ENHANCE 2026-09-14：移植自易标 `contentGenerationTask.cjs` 的 consistency patch 机制
（`findExactOccurrences` / `extractLineRangeText` / `applyExactConsistencyPatch` /
`applyConsistencyRepairPatches`，以及 `utils/textEdit.cjs` 的 `applyRangeEdits`）。

参考实现的核心资产是**两级定位 + 唯一性要求**：

  1. 先试行号区间：区间内文本逐字等于 `old_text` → 按区间替换；
  2. 否则退化到「精确字符串匹配」，且**必须唯一**：
     - 出现 0 次 → 拒绝（AI 编造了原文）；
     - 出现多次 → 拒绝（上下文不足，要求补充更多上下文）。

为什么不做模糊匹配：正文是最终交付物，**改错一个字比不改更糟**。宁可拒绝、交回人工，
也不要猜。这条规则是防止 AI 幻觉改写正文的最后一道闸。

ENHANCE 2026-09-15（移植自 `textEdit.cjs::isDisproportionateMatch`）：补上**区间占比保护**。
唯一精确匹配还不足以防住「AI 把整节正文当 old_text 回灌」——这种补丁能唯一命中、
能通过校验，却会把整节替换成可能已被截断的改写版，内容静默丢失。
占比超过 `DISPROPORTIONATE_RATIO`（默认 90%，且正文 ≥ `DISPROPORTIONATE_MIN_CHARS` 才启用）
即判为「整段重写」并拒绝。

两条使用路径：
  - **AI 路径**（`apply_patches`）：AI 产出 `{old_text, new_text}` 局部补丁，宿主精确应用；
  - **宿主路径**（`replace_values`）：宿主自己算出字符区间做替换，用于「事实冲突统一取值」
    这类确定性修复。该路径额外带**数字边界保护**——本软件的 `FACT_PATTERNS` 会把工期
    抽成 `"30"` 这种极短串，不加保护会把 `"300"`、`"3.30"` 里的 `30` 一起改掉。

本模块是纯函数，不调 AI、不碰数据库。
"""

from __future__ import annotations

import hashlib
import itertools
import logging
import re
from typing import Any


logger = logging.getLogger(__name__)


# 行号前缀：AI 看到的是带行号的正文，常在 old_text 里把 `[0001] ` 一起抄回来
_LINE_NUMBER_PREFIX_RE = re.compile(r"^\[\d{1,6}\]\s?")

# 纯数字值（含千分位与小数）：判定"数字边界"用
_NUMERIC_VALUE_RE = re.compile(r"^\d[\d,]*(?:\.\d+)?$")


class PatchApplyError(Exception):
    """补丁应用失败（old_text 未命中 / 命中多次 / 参数非法）。"""


# ========== 基础工具 ==========


def normalize_newlines(text: Any) -> str:
    """统一换行符为 \\n（CRLF / CR → LF）。"""
    return str(text or "").replace("\r\n", "\n").replace("\r", "\n")


def single_line(value: Any) -> str:
    """压成单行（合并所有空白并去首尾）。"""
    return re.sub(r"\s+", " ", str(value or "")).strip()


def strip_prompt_line_numbers(text: Any) -> str:
    """去掉每行行首的 `[0001] ` 行号前缀。"""
    lines = normalize_newlines(text).split("\n")
    return "\n".join(_LINE_NUMBER_PREFIX_RE.sub("", line) for line in lines)


def normalize_patch_text(text: Any) -> str:
    """归一化补丁文本：统一换行 → 去行号前缀 → 去首尾空白。"""
    return strip_prompt_line_numbers(text).strip()


def text_metrics(value: Any) -> dict:
    """文本指纹（字符数 + 短哈希），用于判断"内容是否真的变了"。"""
    content = str(value or "")
    return {
        "chars": len(content),
        "hash": hashlib.sha1(content.encode("utf-8")).hexdigest()[:12],
    }


def format_content_with_line_numbers(content: Any) -> str:
    """给正文逐行加行号，供提示词使用（宽度对齐，便于模型引用行号）。

    注意：patch 的 old_text/new_text **不要**包含这些行号——
    `normalize_patch_text` 会自动剥掉，这里只是提示词展示。
    """
    lines = normalize_newlines(content).split("\n")
    width = max(3, len(str(len(lines))))
    return "\n".join(f"[{i + 1:0{width}d}] {line}" for i, line in enumerate(lines))


# ========== 精确匹配与行号区间 ==========


def find_exact_occurrences(content: Any, search: Any) -> list[int]:
    """返回 search 在 content 中所有出现的起始下标（非重叠、从左到右）。"""
    text = str(content or "")
    needle = str(search or "")
    if not needle:
        return []
    indexes: list[int] = []
    start = 0
    while start <= len(text):
        index = text.find(needle, start)
        if index < 0:
            break
        indexes.append(index)
        start = index + len(needle)
    return indexes


def extract_line_range_text(content: Any, start_line: Any, end_line: Any) -> str | None:
    """取 [start_line, end_line] 闭区间的文本（1 基行号）。越界返回 None。"""
    lines = normalize_newlines(content).split("\n")
    start = max(1, _to_int(start_line))
    end = max(start, _to_int(end_line))
    if start < 1 or end > len(lines):
        return None
    return "\n".join(lines[start - 1 : end])


def replace_line_range(content: Any, start_line: Any, end_line: Any, replacement: Any) -> str:
    """用 replacement 替换 [start_line, end_line] 闭区间。"""
    lines = normalize_newlines(content).split("\n")
    start = max(1, _to_int(start_line))
    end = max(start, _to_int(end_line))
    new_lines = [
        *lines[: start - 1],
        *normalize_newlines(replacement).split("\n"),
        *lines[end:],
    ]
    return "\n".join(new_lines)


def _to_int(value: Any) -> int:
    """宽松转 int，失败返回 0（与参考实现 `Math.round(Number(x) || 0)` 对齐）。"""
    try:
        return round(float(value))
    except (TypeError, ValueError):
        return 0


# ========== 补丁归一化（字段别名兼容） ==========


def normalize_patch(raw: Any, expected_section_id: Any = None) -> dict:
    """把 AI 返回的原始补丁归一化成统一 schema。

    兼容参考实现里出现过的全部字段别名（AI 输出不稳定，别名兼容是必须的）：
      - old_text  ← old_text | oldText | original | before
      - new_text  ← new_text | newText | replacement | after
      - start_line← start_line | startLine | line_start | lineStart
      - section_id← section_id | sectionId | id
    """
    source = raw if isinstance(raw, dict) else {}
    section_id = single_line(source.get("section_id") or source.get("sectionId") or source.get("id") or "")
    # AI 常原样抄回提示词里的占位符，此时回退到宿主已知的小节号
    if not section_id or section_id == "当前小节编号":
        section_id = single_line(expected_section_id or "")
    return {
        "section_id": section_id,
        "start_line": _to_int(
            source.get("start_line", source.get("startLine", source.get("line_start", source.get("lineStart", 0))))
        ),
        "end_line": _to_int(
            source.get("end_line", source.get("endLine", source.get("line_end", source.get("lineEnd", 0))))
        ),
        "old_text": normalize_patch_text(
            source.get("old_text", source.get("oldText", source.get("original", source.get("before", ""))))
        ),
        "new_text": normalize_patch_text(
            source.get("new_text", source.get("newText", source.get("replacement", source.get("after", ""))))
        ),
        "reason": str(source.get("reason") or source.get("description") or "").strip(),
    }


def normalize_patches(value: Any, expected_section_id: Any = None) -> list[dict]:
    """把 AI 返回的多种包装格式归一化成补丁列表。

    接受：`[{...}]` / `{"patches": [...]}` / `{"operations": [...]}` / 单个补丁对象 /
    `{"result": {...}}`（参考实现里 `collectJsonResponse` 会包一层 result）。
    """
    source = value
    if isinstance(source, dict) and isinstance(source.get("result"), dict):
        source = source["result"]
    if isinstance(source, list):
        raw_patches = source
    elif isinstance(source, dict):
        for key in ("patches", "operations"):
            if isinstance(source.get(key), list):
                raw_patches = source[key]
                break
        else:
            # 单补丁对象：只有它看起来真像补丁时才收
            if any(k in source for k in ("old_text", "oldText", "new_text", "newText")):
                raw_patches = [source]
            else:
                raw_patches = []
    else:
        raw_patches = []
    return [normalize_patch(item, expected_section_id) for item in raw_patches]


# ========== 补丁应用 ==========


# ===== ENHANCE 2026-09-15：补丁「区间占比保护」=====
# 移植自易标 `textEdit.cjs::isDisproportionateMatch` 的思想并做了本地化说明：
# 易标用它拦截「锚点模糊匹配命中的区间远大于 oldText」；本模块不做模糊匹配，
# 但存在同类风险 —— AI 把**整节正文**当成 old_text 回灌，命中后整节被替换成
# 可能已被截断的改写版。正文生成阶段的 AI 修复路径（`consistency_repair`）就是
# 这种模式：提示词里只给了 `current_content[:5000]`，模型却输出"完整正文"。
# 这类"整段重写"不是局部修正，应该交回人工或走正文重新生成，而不是静默落库。
#
#: 占比上限：old_text 长度达到正文该比例即拒绝
DISPROPORTIONATE_RATIO = 0.9
#: 仅对足够长的正文启用（短文本整段替换是正常场景，不必误伤）
DISPROPORTIONATE_MIN_CHARS = 400


def is_disproportionate_patch(
    content: Any,
    old_text: Any,
    *,
    ratio: float | None = DISPROPORTIONATE_RATIO,
    min_chars: int = DISPROPORTIONATE_MIN_CHARS,
) -> tuple[bool, dict]:
    """判断补丁是否为「整段重写」而非局部修正。

    Returns:
        (是否应拒绝, 诊断详情 {content_chars, old_text_chars, share, limit})
    """
    text = normalize_newlines(content)
    needle = normalize_patch_text(old_text)
    total = len(text)
    detail: dict[str, Any] = {
        "content_chars": total,
        "old_text_chars": len(needle),
        "share": 0.0,
        "limit": float(ratio or 0),
    }
    if not needle or not ratio or ratio <= 0 or total < min_chars:
        return False, detail
    share = len(needle) / total
    detail["share"] = round(share, 4)
    return share >= ratio, detail


def describe_patch_match(content: Any, patch: dict) -> dict:
    """诊断一个补丁与当前正文的匹配情况（不改内容，供报告与排障）。"""
    current = normalize_newlines(content)
    old_text = normalize_patch_text(patch.get("old_text"))
    new_text = normalize_patch_text(patch.get("new_text"))
    start_line = _to_int(patch.get("start_line"))
    end_line = _to_int(patch.get("end_line"))

    detail: dict[str, Any] = {
        "section_id": single_line(patch.get("section_id")),
        "start_line": start_line,
        "end_line": end_line,
        "old_text": old_text,
        "new_text": new_text,
        "line_range": None,
        "exact_match_count": 0,
    }

    if start_line > 0 and end_line >= start_line:
        candidate = extract_line_range_text(current, start_line, end_line)
        detail["line_range"] = {
            "exists": candidate is not None,
            "matches_old_text": candidate == old_text,
        }

    detail["exact_match_count"] = len(find_exact_occurrences(current, old_text))
    # 占比保护诊断：把「为什么被拒」写进报告，避免用户只看到"没生效"
    disproportionate, proportion = is_disproportionate_patch(current, old_text)
    detail["disproportionate"] = disproportionate
    detail["proportion"] = proportion
    return detail


def apply_exact_patch(
    content: Any,
    patch: dict,
    *,
    disproportionate_ratio: float | None = DISPROPORTIONATE_RATIO,
) -> str:
    """精确应用单个补丁，返回新正文。失败抛 `PatchApplyError`。

    两级定位：行号区间命中且逐字相等 → 区间替换；否则要求全局唯一精确匹配。

    `disproportionate_ratio` 为区间占比保护的阈值（传 None 或 0 关闭）。
    """
    current = normalize_newlines(content)
    old_text = normalize_patch_text(patch.get("old_text"))
    new_text = normalize_patch_text(patch.get("new_text"))

    if not old_text:
        raise PatchApplyError("old_text 为空")
    if not new_text:
        raise PatchApplyError("new_text 为空")
    if old_text == new_text:
        raise PatchApplyError("old_text 与 new_text 相同")

    disproportionate, detail = is_disproportionate_patch(
        current, old_text, ratio=disproportionate_ratio
    )
    if disproportionate:
        raise PatchApplyError(
            f"old_text 覆盖整节正文的 {detail['share'] * 100:.0f}%"
            f"（上限 {detail['limit'] * 100:.0f}%），疑似整段重写已拒绝；"
            "如需整节改写请走正文重新生成"
        )

    start_line = _to_int(patch.get("start_line"))
    end_line = _to_int(patch.get("end_line"))
    if start_line > 0 and end_line >= start_line:
        candidate = extract_line_range_text(current, start_line, end_line)
        if candidate == old_text:
            return replace_line_range(current, start_line, end_line, new_text)

    matches = find_exact_occurrences(current, old_text)
    if not matches:
        raise PatchApplyError("old_text 未在当前小节正文中找到")
    if len(matches) > 1:
        raise PatchApplyError(f"old_text 在当前小节正文中出现 {len(matches)} 次，请提供更多上下文确保唯一定位")

    index = matches[0]
    return f"{current[:index]}{new_text}{current[index + len(old_text):]}"


def apply_patches(
    content: Any,
    patches: Any,
    *,
    disproportionate_ratio: float | None = DISPROPORTIONATE_RATIO,
) -> dict:
    """串行应用多个补丁，**逐条错误隔离**：单条失败只记录，不影响其余条。

    `disproportionate_ratio` 透传给 `apply_exact_patch` 的区间占比保护。

    返回：
      {
        "content": 新正文,
        "applied_count": 成功条数,
        "rejected_count": 失败条数,
        "changed": 内容是否真的变了,
        "results": [逐条诊断 + applied/error],
        "errors": ["patch[0] ...", ...],
      }
    """
    next_content = normalize_newlines(content)
    before_hash = text_metrics(next_content)["hash"]
    results: list[dict] = []
    errors: list[str] = []
    applied_count = 0

    for index, patch in enumerate(patches or []):
        detail = {"index": index, **describe_patch_match(next_content, patch)}
        try:
            next_content = apply_exact_patch(
                next_content, patch, disproportionate_ratio=disproportionate_ratio
            )
            applied_count += 1
            results.append({**detail, "applied": True, "after_metrics": text_metrics(next_content)})
        except PatchApplyError as exc:
            message = str(exc)
            errors.append(f"patch[{index}] {message}")
            results.append({**detail, "applied": False, "error": message})
        except Exception as exc:  # 兜底：补丁结构异常也不能中断整批
            message = f"应用异常：{exc}"
            errors.append(f"patch[{index}] {message}")
            results.append({**detail, "applied": False, "error": message})

    return {
        "content": next_content,
        "applied_count": applied_count,
        "rejected_count": len(results) - applied_count,
        "changed": text_metrics(next_content)["hash"] != before_hash,
        "results": results,
        "errors": errors,
    }


# ========== 字符区间编辑（宿主路径，用于确定性修复） ==========


def find_value_occurrences(content: Any, value: Any, *, digit_boundary: bool = True) -> list[int]:
    """定位 value 在正文中的所有出现位置。

    **数字边界保护**：当 value 是纯数字（含千分位/小数）时，要求其左右不能紧邻数字或小数点，
    否则跳过。本软件的 `FACT_PATTERNS` 会把工期抽成 `"30"` 这种极短串，不加保护会把
    `"300 日历天"`、`"3.30"`、`"1300"` 里的 `30` 一并改掉。
    """
    text = str(content or "")
    needle = str(value or "")
    if not needle:
        return []

    need_boundary = bool(digit_boundary and _NUMERIC_VALUE_RE.match(needle))
    positions: list[int] = []
    start = 0
    while start <= len(text):
        index = text.find(needle, start)
        if index < 0:
            break
        start = index + len(needle)
        if need_boundary:
            before = text[index - 1] if index > 0 else ""
            after = text[start] if start < len(text) else ""
            if before.isdigit() or before == "." or after.isdigit() or after == ".":
                continue
        positions.append(index)
    return positions


def build_value_replacement_edits(
    content: Any,
    old_value: Any,
    new_value: Any,
    *,
    digit_boundary: bool = True,
) -> list[dict]:
    """为「把 old_value 统一成 new_value」构造字符区间编辑列表。"""
    text = str(content or "")
    replacement = str(new_value or "")
    edits = []
    for position in find_value_occurrences(text, old_value, digit_boundary=digit_boundary):
        edits.append(
            {
                "start": position,
                "end": position + len(str(old_value or "")),
                "new_text": replacement,
            }
        )
    return edits


def apply_range_edits(content: Any, edits: Any) -> dict:
    """按字符区间批量编辑正文（移植自 `textEdit.cjs: applyRangeEdits`）。

    与原实现一致的两条硬约束：
      - 区间必须合法（整数、0 ≤ start ≤ end ≤ 长度、替换后确有变化）；
      - **任意一条有问题就整批拒绝**（区间编辑靠下标定位，部分应用会让后续下标全部错位）。
    应用顺序按 start 倒序，避免前面的编辑位移影响后面的下标。
    区间两两重叠时倒序应用会互相吞噬对方区间（例如 content="ABCDEFGH" 传入
    [0-5)"X" 与 [3-8)"Y" 会得到 "XY"），结果既不符合预期也不报错，
    因此与 `replace_values` 一致，重叠即整批拒绝。
    """
    source = str(content or "")
    planned: list[dict] = []
    errors: list[str] = []

    for index, edit in enumerate(edits or []):
        raw = edit if isinstance(edit, dict) else {}
        start = raw.get("start")
        end = raw.get("end")
        new_text = str(raw.get("newText", raw.get("new_text", "")))
        if not isinstance(start, int) or not isinstance(end, int):
            errors.append(f"rangeEdit[{index}] start/end 必须是整数")
            continue
        if start < 0 or end < start or end > len(source):
            errors.append(f"rangeEdit[{index}] 区间越界或无效：{start}-{end}")
            continue
        if source[start:end] == new_text:
            errors.append(f"rangeEdit[{index}] 替换内容没有变化")
            continue
        planned.append({"index": index, "start": start, "end": end, "newText": new_text})

    # BUG-FIX（P2）：补齐缺失的重叠校验。docstring 一直声称「任意一条有问题就
    # 整批拒绝」，但这里从未检测区间重叠。
    if not errors:
        for a, b in itertools.pairwise(sorted(planned, key=lambda x: (x["start"], x["end"]))):
            if b["start"] < a["end"]:
                errors.append(
                    f"rangeEdit[{a['index']}] 与 rangeEdit[{b['index']}] 区间重叠"
                    f"（{a['start']}-{a['end']} 与 {b['start']}-{b['end']}），已整批拒绝"
                )
        if errors:
            return {"content": source, "changed": False, "applied": [], "errors": errors}

    if errors:
        return {"content": source, "changed": False, "applied": [], "errors": errors}
    if not planned:
        return {"content": source, "changed": False, "applied": [], "errors": []}

    next_content = source
    applied: list[dict] = []
    for item in sorted(planned, key=lambda x: (-x["start"], -x["end"])):
        next_content = f"{next_content[:item['start']]}{item['newText']}{next_content[item['end']:]}"
        applied.insert(
            0,
            {
                "index": item["index"],
                "start": item["start"],
                "end": item["end"],
                "old_text": source[item["start"] : item["end"]],
                "new_text": item["newText"],
            },
        )

    return {
        "content": next_content,
        "changed": next_content != source,
        "applied": applied,
        "errors": [],
    }


def replace_values(
    content: Any,
    replacements: Any,
    *,
    digit_boundary: bool = True,
) -> dict:
    """把多组 `{old_value → new_value}` 一次性替换进正文（宿主路径）。

    `replacements` 接受 `{old: new}` 字典或 `[(old, new)]` 列表。
    所有替换合并成一批区间编辑，因此**任意一处区间非法或重叠就整批拒绝**——
    这是有意的：一致性修复要么整体成功，要么交回人工，不做半吊子修改。
    """
    if isinstance(replacements, dict):
        pairs = list(replacements.items())
    else:
        pairs = [tuple(item) for item in (replacements or [])]

    text = str(content or "")
    edits: list[dict] = []
    for old_value, new_value in pairs:
        if not old_value or str(old_value) == str(new_value):
            continue
        edits.extend(build_value_replacement_edits(text, old_value, new_value, digit_boundary=digit_boundary))

    if not edits:
        return {"content": text, "changed": False, "applied": [], "errors": []}

    # 区间重叠检测：区间编辑靠下标定位，重叠会让结果不可预期
    ordered = sorted(edits, key=lambda x: (x["start"], x["end"]))
    for previous, current in itertools.pairwise(ordered):
        if current["start"] < previous["end"]:
            return {
                "content": text,
                "changed": False,
                "applied": [],
                "errors": [
                    f"替换区间重叠：{previous['start']}-{previous['end']} 与 "
                    f"{current['start']}-{current['end']}（可能由多个待替换值互相包含导致）"
                ],
            }

    return apply_range_edits(text, edits)
