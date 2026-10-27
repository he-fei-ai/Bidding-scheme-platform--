# -*- coding: utf-8 -*-
"""解析质量校验测试（标书智能体（一）引入）。

覆盖 parse_quality 的纯函数：空文本、控制字符/乱码、替换符、可打印占比、
结合文件体积的兜底判定、以及 assert_parse_quality 抛错路径。
"""

import os

from app.services.extractors.parse_quality import (
    ParseQualityIssue,
    ParsedTextQualityError,
    assert_parse_quality,
    assess_extraction,
    validate_parsed_text,
)


def test_empty_text_is_error():
    issues = validate_parsed_text("", filename="a.docx")
    assert any(i.severity == "error" and i.code == "EMPTY_OR_TOO_SHORT" for i in issues)


def test_none_text_treated_as_empty():
    issues = validate_parsed_text(None)
    assert any(i.code == "EMPTY_OR_TOO_SHORT" for i in issues)


def test_normal_text_has_no_error():
    text = "第一章 工程概况\n1.1 工程名称：某商业综合体基坑工程。\n1.2 开挖深度 6.5m。"
    issues = validate_parsed_text(text)
    assert not any(i.severity == "error" for i in issues)


def test_control_char_heavy_text_is_error():
    # 大量 NUL 控制字符 -> 判定为二进制/乱码
    text = "正常中文开头" + "\x00" * 200 + "结尾"
    issues = validate_parsed_text(text)
    assert any(i.severity == "error" and i.code == "HIGH_CONTROL_CHAR_RATIO" for i in issues)


def test_replacement_char_warns():
    # 替换符占比过高 -> warning
    text = "内容" + "\ufffd" * 60
    issues = validate_parsed_text(text)
    assert any(i.code == "HIGH_REPLACEMENT_RATIO" for i in issues)


def test_low_printable_ratio_warns():
    # 不可打印字符为主 -> warning
    text = "".join(chr(c) for c in range(0x2000, 0x2100))  # 大量符号类
    issues = validate_parsed_text(text)
    # 符号类实际可打印，构造一个低可打印场景：大量 C1 控制区
    text2 = "".join(chr(c) for c in range(0x80, 0x9F)) * 30
    issues2 = validate_parsed_text(text2)
    assert any(i.code == "LOW_PRINTABLE_RATIO" for i in issues2)


def test_assess_extraction_flags_big_file_small_text(tmp_path):
    p = tmp_path / "big.pdf"
    p.write_bytes(b"%PDF-1.4" + b"\x00" * 5000)  # >1KB 但不是可读文本
    # 非空但明显不足（40 字符），且文件体积 >1KB -> 兜底判定为解析可疑
    issues = assess_extraction(str(p), "x" * 40)
    assert any(i.code == "SUSPICIOUS_EMPTY" for i in issues)


def test_assess_extraction_passes_for_small_file(tmp_path):
    p = tmp_path / "tiny.txt"
    p.write_text("短文本")
    issues = assess_extraction(str(p), "短文本")
    assert not any(i.severity == "error" for i in issues)


def test_assert_parse_quality_raises_on_error(tmp_path):
    p = tmp_path / "big.pdf"
    p.write_bytes(b"%PDF-1.4" + b"\x00" * 5000)
    try:
        assert_parse_quality(str(p), "x" * 40)
    except ParsedTextQualityError as e:
        assert any(i.code == "SUSPICIOUS_EMPTY" for i in e.issues)
    else:
        raise AssertionError("应当抛出 ParsedTextQualityError")


def test_assert_parse_quality_non_strict_returns_issues(tmp_path):
    p = tmp_path / "big.pdf"
    p.write_bytes(b"%PDF-1.4" + b"\x00" * 5000)
    issues = assert_parse_quality(str(p), "   ", strict=False)
    assert isinstance(issues, list)
    assert any(isinstance(i, ParseQualityIssue) for i in issues)
