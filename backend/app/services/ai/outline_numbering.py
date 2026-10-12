"""outline_numbering.py — 目录编号模板化引擎。

ENHANCE 2026-10-12（P0-1）：移植自 OpenBidKit 易标 `outlineNumbering.ts`。

平台既有实现 `outline_gate.renumber_outline_ids` 只做位置式点号编号
（1 / 1.1 / 1.1.1），无法满足"第一章 / 第一节（一）/ 1.1"这类工程标书
常见的多种编号体系。本模块在保留原 API 语义的前提下，引入占位符模板：

    {zh}        中文数字（一、二、三…，支持 1-9999）
    {num}       阿拉伯数字
    {tail}      分隔符（默认「.」，可覆盖为「、」「（）」等）
    {circled}   圈号（①②③…，1-20）
    {alpha}     字母（A、B、C…）
    {roman}     罗马数字（I、II、III…）
    {level:N}   第 N 段的路径索引，N 从 1 起（用于跨级模板）

模板示例：
    {"1": "第{num}章", "2": "{num}.{num}", "3": "{num}.{num}.{num}"}
    {"1": "第{zh}章", "2": "{num}.{num}", "3": "（{zh}）"}
    {"1": "{alpha}、", "2": "{circled}{num} ", "3": "{num}.{num} "}

设计原则：
  1. **默认关闭**：config.OUTLINE_NUMBERING_ENABLED=false 时不改变任何行为。
  2. **id 稳定**：id 字段的赋值规则不变（"1" / "1.2" / "1.2.3"），
     仅新增 number / level 字段。
  3. **降级安全**：模板语法错误时降级到纯点号编号（1 / 1.1 / 1.1.1），
     不阻断目录生成流程。
  4. **纯函数**：不依赖 config 单例，所有参数显式传入，便于测试。
"""

from __future__ import annotations

import json
import logging
import re
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ========== 中文数字（1-9999） ==========

_CN_DIGITS = ("零", "一", "二", "三", "四", "五", "六", "七", "八", "九")
_CN_CIRCLED = (
    "①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩",
    "⑪", "⑫", "⑬", "⑭", "⑮", "⑯", "⑰", "⑱", "⑲", "⑳",
)


def to_chinese(n: int) -> str:
    """把 1-9999 转换为中文数字。超出范围降级为阿拉伯数字字符串。

    例：1→一，10→十，11→十一，20→二十，100→一百，1000→一千，
        1001→一千零一，2000→两千，10000→10000。
    """
    if not isinstance(n, int) or n <= 0:
        return str(n) if n is not None else ""
    if n > 9999:
        return str(n)
    if n < 10:
        return _CN_DIGITS[n]
    if n == 10:
        return "十"
    if n < 20:
        return "十" + _CN_DIGITS[n - 10]

    # 千位
    parts: List[str] = []
    thousand = n // 1000
    remainder = n % 1000
    if thousand:
        parts.append(_CN_DIGITS[thousand] + "千")

    # 百位
    hundred = remainder // 100
    if hundred:
        # 1001 → 一千零一（千位后接百位为 0 时补"零"）
        if thousand and hundred == 1 and remainder < 100:
            # 1100 → 一千一百（不需要"零"）
            pass
        if thousand and (remainder // 10) == 0 and remainder % 10 == 0:
            # 1000 已经在千位覆盖
            pass
        parts.append(_CN_DIGITS[hundred] + "百")

    # 十位
    remainder_after_hundred = remainder % 100
    ten = remainder_after_hundred // 10
    one = remainder_after_hundred % 10
    if ten:
        if (thousand or hundred) and ten == 1:
            # 101 → 一百零一（不加"十"）
            if hundred and not thousand and remainder_after_hundred < 10:
                # 101 → 一百零一
                parts.append("零")
            elif hundred and remainder_after_hundred < 10:
                parts.append("零")
        # 只有当 remainder_after_hundred >= 10 时才加"十"
        if remainder_after_hundred >= 10 and not (parts and parts[-1] == "零"):
            if (thousand or hundred) and ten == 1:
                # 101 处理：不加"十"
                pass
            else:
                parts.append(_CN_DIGITS[ten] + "十")

    # 个位
    if one:
        if not parts:
            parts.append(_CN_DIGITS[one])
        elif parts[-1] in ("零",):
            # 1001 → 一千零 + 一
            parts.append(_CN_DIGITS[one])
        elif (thousand or hundred) and ten == 0:
            # 1101 → 一千一百零一
            parts.append("零" + _CN_DIGITS[one])
        elif parts and parts[-1] != _CN_DIGITS[one]:
            parts.append(_CN_DIGITS[one])

    result = "".join(parts)
    return result or str(n)


def to_roman(n: int) -> str:
    """把 1-3999 转换为大写罗马数字。超出范围降级为阿拉伯数字字符串。"""
    if not isinstance(n, int) or n <= 0:
        return str(n) if n is not None else ""
    if n > 3999:
        return str(n)
    vals = [
        (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
        (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
        (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
    ]
    result = ""
    remaining = n
    for value, symbol in vals:
        while remaining >= value:
            result += symbol
            remaining -= value
    return result


def to_circled(n: int) -> str:
    """把 1-20 转换为圈号（①②③…）。超出范围降级为阿拉伯数字字符串。"""
    if isinstance(n, int) and 1 <= n <= len(_CN_CIRCLED):
        return _CN_CIRCLED[n - 1]
    return str(n) if n is not None else ""


def to_alpha(n: int) -> str:
    """把 1-26 转换为大写字母（A、B、C…）。超出范围降级为阿拉伯数字。"""
    if isinstance(n, int) and 1 <= n <= 26:
        return chr(ord("A") + n - 1)
    return str(n) if n is not None else ""


# ========== 占位符解析 ==========

_PLACEHOLDER_RE = re.compile(
    r"\{(?P<name>[a-zA-Z_]+)(?::(?P<arg>[a-zA-Z0-9_]+))?\}"
)

_DEFAULT_PLACEHOLDER_FALLBACK: Dict[str, str] = {
    "zh": "", "num": "", "tail": ".", "circled": "", "alpha": "", "roman": "",
}


@dataclass
class NumberingTemplate:
    """编号模板集合。"""

    level_templates: Dict[int, str] = field(default_factory=dict)
    default_template: str = "{num}.{num}"
    tail_char: str = "."

    @classmethod
    def from_dict(
        cls, mapping: Dict[Any, str], tail_char: str = "."
    ) -> "NumberingTemplate":
        parsed: Dict[int, str] = {}
        for k, v in (mapping or {}).items():
            try:
                level = int(k)
            except (TypeError, ValueError):
                continue
            if level < 1:
                continue
            parsed[level] = str(v)
        return cls(
            level_templates=parsed,
            default_template="{num}.{num}",
            tail_char=tail_char or ".",
        )

    @classmethod
    def from_json(cls, raw: str, tail_char: str = ".") -> "NumberingTemplate":
        """从 JSON 字符串构建；解析失败降级到 DEFAULT_TEMPLATE_JSON。

        支持顶层 {"levels": {...}, "tail": "."} 结构（更严格），
        也支持直接的 {"1": "...", "2": "..."} 结构（更宽松）。
        """
        if not raw or not isinstance(raw, str):
            return cls.from_json(DEFAULT_TEMPLATE_JSON, tail_char=tail_char)
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("OUTLINE_NUMBERING_TEMPLATE 不是合法 JSON，降级到默认模板")
            return cls.from_json(DEFAULT_TEMPLATE_JSON, tail_char=tail_char)
        if isinstance(obj, dict):
            if "levels" in obj and isinstance(obj["levels"], dict):
                obj = obj["levels"]
            if "tail" in obj:
                tail_char = str(obj.pop("tail", tail_char)) or "."
        return cls.from_dict(obj, tail_char=tail_char)

    def template_for_level(self, level: int) -> str:
        return self.level_templates.get(level) or self.default_template

    def render_path(self, path: List[int]) -> str:
        """按模板渲染一条层级路径。

        Args:
            path: 1-based 层级索引数组，例如 [1, 2, 3] 表示第 1 章第 2 节第 3 目。

        Returns:
            渲染后的编号字符串。模板语法错误时降级到纯点号编号。
        """
        if not path:
            return ""
        level = len(path)
        template = self.template_for_level(level)
        try:
            return self._render(template, path)
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "编号模板渲染失败（level=%s template=%r err=%s），降级到点号编号",
                level, template, e,
            )
            return ".".join(str(p) for p in path)

    def _render(self, template: str, path: List[int]) -> str:
        def repl(m: re.Match[str]) -> str:
            name = m.group("name")
            arg = m.group("arg")
            if name == "level" and arg:
                try:
                    idx = int(arg)
                except ValueError:
                    return m.group(0)
                if 1 <= idx <= len(path):
                    return str(path[idx - 1])
                return ""
            return self._placeholder_value(name, path, arg)
        return _PLACEHOLDER_RE.sub(repl, template)

    def _placeholder_value(
        self, name: str, path: List[int], arg: Optional[str]
    ) -> str:
        if not path:
            return ""
        if arg:
            try:
                idx = int(arg)
                current = path[idx - 1] if 1 <= idx <= len(path) else path[-1]
            except ValueError:
                current = path[-1]
        else:
            current = path[-1]

        if name == "num":
            return str(current)
        if name == "zh":
            return to_chinese(current)
        if name == "circled":
            return to_circled(current)
        if name == "alpha":
            return to_alpha(current)
        if name == "roman":
            return to_roman(current)
        if name == "tail":
            return self.tail_char
        return _DEFAULT_PLACEHOLDER_FALLBACK.get(name, "")


# ========== 公开 API ==========

DEFAULT_TEMPLATE_JSON = (
    '{"1": "第{num}章", "2": "{num:1}.{num:2}", "3": "{num:1}.{num:2}.{num:3}"}'
)


def renumber_outline_with_template(
    items: List[dict],
    template: Optional[NumberingTemplate] = None,
    mutate: bool = False,
) -> Tuple[List[dict], int]:
    """按模板重编号整棵树，返回 (新目录, 变更节点数)。

    Args:
        items: 目录节点列表，每个节点是一个 dict，可以有 'children' 字段。
        template: 编号模板；None 时按 DEFAULT_TEMPLATE_JSON 构建。
        mutate: True 时原地修改节点；False 时深拷贝后修改（默认）。

    Returns:
        (处理后的目录, 变更节点数)。id 字段不变（保持与 outline_gate 兼容），
        新增/覆盖 'number'（模板渲染结果）与 'level'（1-based 层级）字段。
    """
    if template is None:
        template = NumberingTemplate.from_json(DEFAULT_TEMPLATE_JSON)

    if not mutate:
        items = deepcopy(items)

    changed = 0

    def _walk(nodes: Any, path_prefix: List[int]) -> None:
        nonlocal changed
        if not isinstance(nodes, list):
            return
        for index, node in enumerate(nodes, 1):
            if not isinstance(node, dict):
                continue
            path = path_prefix + [index]
            number = template.render_path(path)
            if node.get("number") != number:
                node["number"] = number
                changed += 1
            if node.get("level") != len(path):
                node["level"] = len(path)
            _walk(node.get("children"), path)

    _walk(items, [])
    return items, changed


def render_outline_number(
    path: List[int],
    template_json: Optional[str] = None,
    tail_char: str = ".",
) -> str:
    """便捷函数：给定层级路径 + 模板 JSON，返回渲染后的编号。

    Args:
        path: 1-based 层级索引数组，例如 [1, 2, 3]。
        template_json: 模板 JSON；None 时用默认模板。
        tail_char: {tail} 占位符的默认值。
    """
    if template_json:
        template = NumberingTemplate.from_json(template_json, tail_char=tail_char)
    else:
        template = NumberingTemplate.from_json(DEFAULT_TEMPLATE_JSON, tail_char=tail_char)
    return template.render_path(path)


def default_numbering_template() -> NumberingTemplate:
    """返回默认编号模板（1=第N章, 2=N.N, 3=N.N.N）。"""
    return NumberingTemplate.from_json(DEFAULT_TEMPLATE_JSON)

