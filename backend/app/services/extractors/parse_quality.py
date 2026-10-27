# -*- coding: utf-8 -*-
"""解析质量校验（标书智能体（一）引入：解析失败不再静默返回空）。

参考文档《标书智能体（一）——AI解析招标文件代码+提示词》第二节「文件内容提取」
与「引入方案」第二节 4「解析质量校验」：

    提取后必须检查文本长度是否异常、表格数量是否符合预期、是否存在乱码 /
    控制字符 / 编码异常；解析失败时给出明确错误，不静默返回空。

本模块为纯函数工具，被 ``files.extract_text_from_path`` 在解析完成后调用，对
"明显解析失败"（非空文件却解析出空/乱码）做显式告警，必要时抛错，填补此前
"静默返回空、下游误以为文件无内容"的缺口（引入方案 BUG 修复重点 #4）。
"""

from __future__ import annotations

import dataclasses
import logging
import os
import unicodedata


logger = logging.getLogger(__name__)


# 控制字符集合（不含常见空白 \t \n \r \f \v 与正常可见字符）。
# range(0, 32) 含 \t(9) \n(10) \r(13) \f(12) \v(11)，逐个排除后保留其余 C0 控制符，
# 并显式加入 NUL(0x00)（二进制碎片常见）。
_CONTROL_CHARS = {
    chr(c) for c in range(0, 32) if c not in (9, 10, 13, 11, 12)
} | {"\x00"}


@dataclasses.dataclass(frozen=True)
class ParseQualityIssue:
    """一条解析质量问题的结构化描述。

    severity: ``"error"``（严重，应阻止静默落库）/ ``"warning"``（可疑，需关注）。
    code: 稳定的机器可识别代码，便于测试与日志聚类。
    message: 面向运维/开发的中文说明。
    """

    severity: str
    code: str
    message: str


def validate_parsed_text(
    text: "str | None",
    *,
    filename: str = "",
    min_chars: int = 1,
    min_printable_ratio: float = 0.6,
    max_replacement_ratio: float = 0.05,
    max_control_ratio: float = 0.02,
) -> "list[ParseQualityIssue]":
    """对单段已解析文本做质量校验，返回问题列表（空列表表示通过）。

    Args:
        text: 解析出的文本（None 视为空串）。
        filename: 文件名，仅用于问题说明的可读性。
        min_chars: 有效文本（去空白后）最少字符数，低于即视为解析失败。
        min_printable_ratio: 可打印字符占比下限，过低说明文本大量不可读。
        max_replacement_ratio: U+FFFD 替换符占比上限，过高说明编码/解码大量丢字节。
        max_control_ratio: 控制字符占比上限，过高说明文本是二进制/乱码而非正常文档。

    Returns:
        按严重程度排序的问题列表（error 在前）。
    """
    issues: "list[ParseQualityIssue]" = []
    if text is None:
        text = ""
    stripped = text.strip()
    n = len(stripped)

    if n < min_chars:
        issues.append(
            ParseQualityIssue(
                "error",
                "EMPTY_OR_TOO_SHORT",
                f"解析文本为空或长度过短（{n} 字符，阈值 {min_chars}），"
                f"文件可能解析失败{f'（{filename}）' if filename else ''}",
            )
        )
        return issues

    # 1) 替换符（U+FFFD）占比：过高说明编码/解码大量丢失字节。
    repl = stripped.count("\ufffd")
    ratio_repl = repl / n
    if ratio_repl > max_replacement_ratio:
        issues.append(
            ParseQualityIssue(
                "warning",
                "HIGH_REPLACEMENT_RATIO",
                f"替换符（U+FFFD）占比 {ratio_repl:.1%} 过高，存在编码/解码丢失"
                f"（{repl} 处），疑似文件损坏或编码识别错误",
            )
        )

    # 2) 控制字符占比：过高说明文本是二进制碎片/乱码而非正常文档文本。
    ctrl = sum(1 for ch in stripped if ch in _CONTROL_CHARS)
    ratio_ctrl = ctrl / n
    if ratio_ctrl > max_control_ratio:
        issues.append(
            ParseQualityIssue(
                "error",
                "HIGH_CONTROL_CHAR_RATIO",
                f"控制字符占比 {ratio_ctrl:.1%} 过高，文本疑似二进制/乱码而非正常文档"
                f"文本（{ctrl} 处）",
            )
        )

    # 3) 可打印字符占比：过低说明文本含大量不可读内容。
    printable = sum(
        1
        for ch in stripped
        if unicodedata.category(ch)[0] in ("L", "N", "P", "S") or ch in " \t\n\r"
    )
    ratio_print = printable / n
    if ratio_print < min_printable_ratio:
        issues.append(
            ParseQualityIssue(
                "warning",
                "LOW_PRINTABLE_RATIO",
                f"可打印字符占比 {ratio_print:.1%} 低于阈值 {min_printable_ratio:.1%}，"
                f"文本可能含大量不可读内容",
            )
        )

    issues.sort(key=lambda i: (i.severity != "error", i.code))
    return issues


def assess_extraction(
    file_path: str, text: str, *, filename: str = ""
) -> "list[ParseQualityIssue]":
    """结合文件体积判断解析是否"明显失败"。

    仅当文本本身校验已出问题时直接返回；否则再结合文件体积做"非空文件却几乎
    没解析出文本"的兜底判定。对图片/OCR 等正常空返回不告警，避免误伤。

    Args:
        file_path: 源文件路径（用于取得体积）。
        text: 解析出的文本。
        filename: 覆盖用文件名（不传则取 file_path 基名）。
    """
    name = filename or (file_path and os.path.basename(file_path)) or ""
    issues = validate_parsed_text(text, filename=name)
    if issues:
        return issues
    try:
        size = os.path.getsize(file_path)
    except OSError:
        return issues
    # 文件明显有内容（>1KB），却几乎没解析出文本（<50 字符）：
    # 多为损坏/加密/扫描件未启用 OCR，应明确告警而非静默当作无内容。
    if size > 1024 and len(text.strip()) < 50:
        issues.append(
            ParseQualityIssue(
                "error",
                "SUSPICIOUS_EMPTY",
                f"文件体积 {size} 字节却仅解析出 {len(text.strip())} 字符，"
                f"疑似解析失败（损坏/加密/扫描件未启用OCR）{f'（{name}）' if name else ''}",
            )
        )
    return issues


class ParsedTextQualityError(Exception):
    """解析质量严重不达标时抛出（供需要显式失败的调用方使用）。"""

    def __init__(self, issues: "list[ParseQualityIssue]", filename: str = ""):
        self.issues = list(issues)
        self.filename = filename
        detail = "; ".join(
            f"[{i.severity}] {i.code}: {i.message}" for i in self.issues
        )
        super().__init__(
            f"文件解析质量不达标{f'（{filename}）' if filename else ''}: {detail}"
        )


def assert_parse_quality(
    file_path: str, text: str, *, filename: str = "", strict: bool = True
) -> "list[ParseQualityIssue]":
    """解析后校验；若存在 error 级问题且 ``strict=True`` 则抛 ``ParsedTextQualityError``。

    返回全部问题列表（含 warning），便于调用方既做强制拦截也做记录。
    """
    issues = assess_extraction(file_path, text, filename=filename)
    errors = [i for i in issues if i.severity == "error"]
    if errors and strict:
        raise ParsedTextQualityError(
            errors, filename=filename or (file_path and os.path.basename(file_path)) or ""
        )
    return issues
