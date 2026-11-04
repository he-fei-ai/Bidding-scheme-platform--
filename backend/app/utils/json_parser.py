"""JSON 解析工具模块

提供从文本中提取 JSON 和安全解析 JSON 的公共工具函数。
集中管理 JSON 解析逻辑，避免各模块重复实现。

候选链（ENHANCE 2026-09-14：移植自易标投标工具箱 aiService.cjs::parseJsonContent）：
    1. 原文直接解析
    2. 剥离 Markdown 代码围栏后解析
    3. 括号配平扫描提取第一个完整 JSON 后解析
    4. 对以上每个候选做「非法转义修复」再解析
    5. 对以上每个候选做「尾随逗号 / 注释清理」再解析

第 4、5 级是本模块新增能力：大模型输出中文长文本时，最常见的失败并非语法结构
错误，而是字符串内部出现非法转义（如 `\\d`、`\\.`、裸换行）或尾随逗号，
这类内容在旧实现下会被直接判为「解析失败」并触发一次额外的 AI 修复调用
（成本高、延迟高）。新增的本地确定性修复可以把这类失败在本地消化掉。
"""

import json
import logging
import re
from typing import Any


logger = logging.getLogger(__name__)


# 合法 JSON 转义字符（\uXXXX 单独处理）
_JSON_ESCAPE_CHARS = frozenset('"\\/bfnrt')

# Markdown 转义字符：模型常把 Markdown 的 `\.` `\(` `\-` 等写进 JSON 字符串，
# 这些在 JSON 中属非法转义，修复策略是「去掉反斜杠、保留字符本身」。
_MARKDOWN_ESCAPE_CHARS = frozenset('.[]{}#*+-_!<>|`()')

# 字符串内不允许出现的裸控制字符 → 其合法转义形式
_BARE_CONTROL_ESCAPES = {
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\b": "\\b",
    "\f": "\\f",
}

_HEX4_RE = re.compile(r"[0-9a-fA-F]{4}")

# 尾随逗号：`,}` 或 `,]`（允许中间有空白）
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")


def extract_json_from_text(text: str) -> dict | list | None:
    """从文本中提取 JSON 对象或数组。

    支持以下格式：
    - 纯 JSON 字符串（直接解析）
    - Markdown 代码围栏包裹（```json ... ```）
    - 文本中嵌入的 JSON 对象（通过查找第一个 { 或 [ 定位）
    - 字符串内含非法转义 / 尾随逗号的「近似 JSON」（本地确定性修复后解析）

    Args:
        text: 可能包含 JSON 的原始文本字符串。

    Returns:
        解析成功的 JSON 对象（dict 或 list），
        无法提取时返回 None。
    """
    if not text:
        return None
    content = text.strip()
    if not content:
        return None

    text_len = len(content)
    # 输入过大时截断，防止正则回溯和内存问题
    if text_len > 500000:
        logger.warning("extract_json_from_text 输入过长(%d 字符)，仅处理前 500000 字符", text_len)
        content = content[:500000]

    # ===== 第 1 级：原文直接解析 =====
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    # ===== 收集候选文本（原文 / 去围栏 / 配平子串）=====
    candidates: list[str] = []

    # 第 2 级：剥离 Markdown 代码围栏（```json ... ```）
    code_block_match = re.search(
        r"```(?:json)?\s*\n(.*?)(?:\n```|```)",
        content,
        re.DOTALL,
    )
    if code_block_match:
        fenced = code_block_match.group(1).strip()
        if fenced:
            candidates.append(fenced)

    # 第 3 级：括号配平扫描提取第一个完整 JSON（正确处理嵌套）
    balanced = _extract_balanced_json(content)
    if balanced is not None and balanced.strip() != content:
        candidates.append(balanced.strip())

    # 去重后逐个尝试：先原样解析，再转义修复，最后逗号/注释清理
    for candidate in _unique(candidates):
        parsed = _try_parse_candidate(candidate)
        if parsed is not None:
            return parsed

    # 兜底：把整段文本当作候选再做一次修复尝试（应对无围栏、无配平子串的场景）
    parsed = _try_parse_candidate(content)
    if parsed is not None:
        return parsed

    logger.debug("extract_json_from_text 所有解析策略均失败 (输入长度=%d)", text_len)
    return None


def _unique(items: list[str]) -> list[str]:
    """按原顺序去重（保持稳定顺序，避免候选链顺序漂移导致行为不可预测）"""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        key = item.strip()
        if key and key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _try_parse_candidate(candidate: str) -> dict | list | None:
    """对单个候选文本依次尝试「原样 → 转义修复 → 逗号/注释清理 → 组合修复」多级解析。

    ENHANCE 2026-09-14：新增「转义修复」「逗号/注释清理」两级本地修复，减少因模型
    输出瑕疵导致的整轮 AI 重试。
    ENHANCE 2026-09-16：新增「转义修复 + 宽松语法清理」的组合修复。大模型输出常在同一
    段 JSON 里**同时**出现非法转义（如 `\\.`、裸换行）与尾随逗号/注释，单独做任一种
    修复都无法解析，必须两者组合才能消化（否则仍会退化到昂贵的 AI 修复重试）。
    """
    # BUG-FIX 2026-10-08：修复变体改为惰性计算——先尝试原样解析，
    # 成功则无需计算任何修复。原实现在首次 json.loads 成功后仍会
    # 执行全部 4 种修复变换，白耗 CPU。
    def _try_variant(text: str | None) -> dict | list | None:
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None
        return parsed if isinstance(parsed, (dict, list)) else None

    # 1. 原样解析
    result = _try_variant(candidate)
    if result is not None:
        return result

    # 2. 仅修复转义
    result = _try_variant(repair_invalid_json_escapes(candidate))
    if result is not None:
        return result

    # 3. 仅修复语法（逗号/注释）
    result = _try_variant(_repair_loose_syntax(candidate))
    if result is not None:
        return result

    # 4. 先修转义再清语法
    result = _try_variant(_repair_loose_syntax(repair_invalid_json_escapes(candidate)))
    if result is not None:
        return result

    # 5. 先清语法再修转义
    result = _try_variant(repair_invalid_json_escapes(_repair_loose_syntax(candidate)))
    if result is not None:
        return result

    return None


def repair_invalid_json_escapes(content: str) -> str:
    """修复 JSON 字符串内部的非法转义序列。

    移植自易标投标工具箱 `aiService.cjs::repairInvalidJsonStringEscapes`，
    并针对 Python/中文场景做了两点增强：

    1. 模型写出的 Markdown 转义（`\\.`、`\\(`、`\\-` 等）→ 去掉反斜杠保留字符；
    2. **新增**：字符串内的裸控制字符（换行/回车/制表符）→ 替换为合法转义
       （`\\n` / `\\r` / `\\t`）。大模型输出含代码或分点列表的中文长文本时
       极易在字符串里直接换行，原实现对此仍会解析失败。

    修复规则：
    - 合法转义（`\\" \\\\ \\/ \\b \\f \\n \\r \\t \\uXXXX`）原样保留；
    - Markdown 转义字符 → 丢弃反斜杠；
    - 其他非法转义（如 `\\d`、`\\p`）→ 把反斜杠转义为 `\\\\`（保留字面量语义）；
    - 字符串末尾孤立的反斜杠 → 转义为 `\\\\`。

    对合法 JSON 输入，本函数是恒等变换（幂等），可安全地无条件调用。
    """
    text = str(content or "")
    out: list[str] = []
    in_string = False
    index = 0
    length = len(text)

    while index < length:
        char = text[index]

        if not in_string:
            out.append(char)
            if char == '"':
                in_string = True
            index += 1
            continue

        # —— 字符串内部 ——
        if char == '"':
            out.append(char)
            in_string = False
            index += 1
            continue

        if char in _BARE_CONTROL_ESCAPES:
            # 增强点：裸控制字符在 JSON 字符串中非法，替换为转义形式
            out.append(_BARE_CONTROL_ESCAPES[char])
            index += 1
            continue

        if char != "\\":
            out.append(char)
            index += 1
            continue

        next_char = text[index + 1] if index + 1 < length else ""
        if not next_char:
            # 孤立反斜杠（字符串未闭合时截断）
            out.append("\\\\")
            index += 1
            continue

        if next_char == "u":
            digits = text[index + 2 : index + 6]
            if len(digits) == 4 and _HEX4_RE.fullmatch(digits):
                out.append(text[index : index + 6])
                index += 6
            else:
                out.append("\\\\")
                index += 1
            continue

        if next_char in _JSON_ESCAPE_CHARS:
            out.append(char + next_char)
            index += 2
            continue

        if next_char in _MARKDOWN_ESCAPE_CHARS:
            out.append(next_char)
            index += 2
            continue

        # 其余非法转义：保留字面反斜杠
        out.append("\\\\")
        index += 1

    return "".join(out)


def _repair_loose_syntax(content: str) -> str:
    """清理 JSON 之外的宽松语法：尾随逗号、`//` 与 `/* */` 注释。

    ENHANCE 2026-09-14：模型经常输出 JS 风格的注释与尾随逗号，
    `json.loads` 对此一律拒绝。这里做保守清理（仅在字符串外部操作）。
    """
    text = str(content or "")
    if not text:
        return text

    # 1) 去掉字符串外部的注释
    out: list[str] = []
    in_string = False
    escape = False
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if in_string:
            out.append(char)
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            index += 1
            continue

        if char == '"':
            in_string = True
            out.append(char)
            index += 1
            continue

        if char == "/" and index + 1 < length and text[index + 1] == "/":
            # 行注释：跳到行尾（保留换行符本身）
            newline = text.find("\n", index)
            index = length if newline == -1 else newline
            continue

        if char == "/" and index + 1 < length and text[index + 1] == "*":
            end = text.find("*/", index + 2)
            index = length if end == -1 else end + 2
            continue

        out.append(char)
        index += 1

    cleaned = "".join(out)

    # 2) 去掉尾随逗号（循环处理 `,,` 之类的连续情况）
    previous = None
    while previous != cleaned:
        previous = cleaned
        cleaned = _TRAILING_COMMA_RE.sub(r"\1", cleaned)

    return cleaned


def _extract_balanced_json(content: str) -> str | None:
    """从文本中按括号配平提取第一个完整的 JSON（对象或数组）。

    相比正则 `\\{.*?\\}`，本方法能正确处理嵌套结构、字符串内的括号、
    以及文本中混杂多个 JSON 片段的场景。

    实现（BUG-FIX 2026-11-04）：改为**单趟栈扫描**。
    旧实现虽然用 `content.find` 跳开非开括号字符，但每个候选起点都要把
    剩余全文重扫一遍：`"{" * 1000 + '{"a": 1}' + " " * 10000` 这种
    「大量不匹配开括号 + 长尾」输入实测 5.77s（tests/test_import_fixes_
    2026_10_08.py::test_many_unmatched_openers_fast 阈值 5s），仍属
    O(k·n) 退化。单趟扫描把每个字符只看一次，最坏 O(n)。

    语义保持不变：返回**最靠左**的完整配平片段。栈底开括号闭合（栈空）
    即最左成功起点，可直接返回；若始终未清空（外层不闭合），则回退到
    「闭合过的最左开括号」（例如 `{ {a}` → 返回 `{a}`，与旧实现一致）。

    引号处理沿用旧实现的「每个候选起点重置字符串状态」：栈为空时引号视为
    普通文本（允许被引号包裹的 JSON 被提取），栈非空时才按 JSON 字符串
    规则屏蔽其中的括号。

    Returns:
        配平后的 JSON 子串；若找不到则返回 None。
    """
    openers = {"{": "}", "[": "]"}
    closers = {"}": "{", "]": "["}

    # 性能优化：先定位第一个开括号，避免对超长纯文本（无任何 JSON）做逐字符无谓扫描。
    first_open = content.find("{")
    first_bracket = content.find("[")
    if first_open == -1 and first_bracket == -1:
        return None
    start = min(p for p in (first_open, first_bracket) if p != -1)
    n = len(content)

    def _scan(respect_quotes: bool) -> str | None:
        """单趟栈扫描；respect_quotes=False 时把引号当普通文本（兼容重试语义）。"""
        stack: list[int] = []
        in_string = False
        escape = False
        # 已闭合但外层未闭合时的最佳候选（开括号下标, 闭括号下标）
        best: tuple[int, int] | None = None

        i = start
        while i < n:
            c = content[i]
            if in_string:
                if escape:
                    escape = False
                elif c == "\\":
                    escape = True
                elif c == '"':
                    in_string = False
                i += 1
                continue

            if not stack:
                # 栈为空：此处要么开启新片段，要么是片段之间的普通文本；
                # 引号按普通文本处理（等价于旧实现「在新起点重置字符串状态」）。
                if c in openers:
                    stack.append(i)
                i += 1
                continue

            if respect_quotes and c == '"':
                in_string = True
            elif c in openers:
                stack.append(i)
            elif c in closers:
                if content[stack[-1]] != closers[c]:
                    # 括号类型错配：当前所有未闭合候选都不可能配平（旧实现逐个
                    # 重试也必然在同一个字符处失败），整体丢弃，从后续文本重新开始。
                    stack.clear()
                    in_string = False
                    escape = False
                else:
                    popped = stack.pop()
                    if not stack:
                        return content[popped : i + 1]
                    if best is None or popped < best[0]:
                        best = (popped, i)
            i += 1

        if best is not None:
            return content[best[0] : best[1] + 1]
        return None

    found = _scan(respect_quotes=True)
    if found is not None:
        return found
    # 兜底（等价于旧实现「每个候选起点重置字符串状态」的恢复能力）：
    # 首个片段里出现未闭合引号时，字符串状态会把后续合法 JSON 的括号全部
    # 屏蔽掉（如 `{ "未闭合 ... {"a": 1}` 的流式半成品）。再跑一趟
    # 「引号视为普通文本」的扫描，仍是最坏 O(n)，不引入重扫退化。
    return _scan(respect_quotes=False)


def safe_json_parse(text: str, default: Any = None) -> dict | list | None:
    """安全解析 JSON 字符串，解析失败时返回默认值。

    与 extract_json_from_text 的区别：
    - safe_json_parse 直接对文本进行 json.loads 尝试，不进行模式匹配提取
    - 适用于已知输入为纯净 JSON 字符串的场景

    Args:
        text: 待解析的 JSON 字符串。
        default: 解析失败时返回的默认值，默认为 None。

    Returns:
        解析成功的 JSON 对象（dict 或 list），
        解析失败时返回 default。
    """
    if not text or not text.strip():
        return default

    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError) as e:
        logger.debug("JSON 解析失败: %s", e)
        return default
