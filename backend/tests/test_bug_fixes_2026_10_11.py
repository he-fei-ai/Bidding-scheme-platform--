"""BUG 修复回归测试 2026-10-11
覆盖 BUG-P1a / P1b / P2a / P2b / P3a / P3b 的核心修复逻辑
"""
import sys, os, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


# =====================================================================
# BUG-P1a 测试：续写未闭合围栏检测
# =====================================================================
class TestDetectUnclosedFence:
    def test_closed_fence_returns_none(self):
        """正常闭合围栏返回 None"""
        from app.services.ai.workflows_sections import _detect_unclosed_chart_fence
        ok = """正文内容
```mermaid
graph TD; A-->B;
```
继续正文"""
        assert _detect_unclosed_chart_fence(ok) is None

    def test_unclosed_mermaid_detected(self):
        """未闭合 mermaid 围栏被检测到"""
        from app.services.ai.workflows_sections import _detect_unclosed_chart_fence
        bad = """正文
```mermaid
graph TD; A-->B;
被截断"""
        result = _detect_unclosed_chart_fence(bad)
        assert result is not None
        assert result["lang"] == "mermaid"
        assert result["open_line_no"] == 2

    def test_unclosed_chart_json_detected(self):
        """未闭合 chart-json 围栏被检测到"""
        from app.services.ai.workflows_sections import _detect_unclosed_chart_fence
        bad = """前文
```chart-json
{"type":"pie","data":{"labels":["a","b"]
被截断"""
        result = _detect_unclosed_chart_fence(bad)
        assert result is not None
        assert result["lang"] == "chart-json"

    def test_two_fences_one_closed_one_open(self):
        """两个围栏：第一个闭合，第二个未闭合 → 检测到第二个"""
        from app.services.ai.workflows_sections import _detect_unclosed_chart_fence
        bad = """正文
```mermaid
graph A;
```
继续
```mermaid
graph B
未闭合"""
        result = _detect_unclosed_chart_fence(bad)
        assert result is not None
        # 数一下：第1行正文, 第2行```mermaid(open1), 第3行graph A;, 第4行```(close1),
        # 第5行继续, 第6行```mermaid(open2) → open_line_no=6
        assert result["open_line_no"] == 6

    def test_empty_content(self):
        from app.services.ai.workflows_sections import _detect_unclosed_chart_fence
        assert _detect_unclosed_chart_fence("") is None


# =====================================================================
# BUG-P1b 测试：压缩图表围栏保护
# =====================================================================
class TestCompressChartProtection:
    def test_mask_restore_roundtrip(self):
        """mask 后再 restore 应还原为原文"""
        from app.services.ai.workflows_sections import (
            _mask_chart_fences_for_compression,
            _restore_chart_fences_after_compression,
        )
        orig = """# 标题
正文段落

```mermaid
graph TD;
  A-->B;
  B-->C;
```

更多正文

```chart-json
{"type":"pie","data":{"labels":["a","b"],"values":[1,2]}}
```

结尾"""
        masked, originals = _mask_chart_fences_for_compression(orig)
        assert len(originals) == 2
        assert "<<CHART_BLOCK_1>>" in masked
        assert "<<CHART_BLOCK_2>>" in masked
        assert "```mermaid" not in masked
        assert "```chart-json" not in masked

        restored = _restore_chart_fences_after_compression(masked, originals)
        assert restored == orig

    def test_no_chart_blocks(self):
        """无图表时 mask 返回原内容"""
        from app.services.ai.workflows_sections import (
            _mask_chart_fences_for_compression,
            _restore_chart_fences_after_compression,
        )
        plain = "纯正文段落，无图表"
        masked, originals = _mask_chart_fences_for_compression(plain)
        assert originals == []
        assert masked == plain
        restored = _restore_chart_fences_after_compression(masked, originals)
        assert restored == plain

    def test_compression_preserves_chart_blocks(self):
        """模拟 AI 压缩：占位符仍在 → 还原后图表完整"""
        from app.services.ai.workflows_sections import (
            _mask_chart_fences_for_compression,
            _restore_chart_fences_after_compression,
        )
        orig = """正文开头

```mermaid
graph TD; A-->B;
```

中间正文（长）

```chart-json
{"type":"pie","data":{}}
```

正文结尾"""
        masked, originals = _mask_chart_fences_for_compression(orig)
        # 模拟 AI 压缩：AI 保留占位符不变，压缩前后正文
        condensed_masked = masked  # AI 实际会压缩正文，但我们这里只验证占位符还原
        restored = _restore_chart_fences_after_compression(condensed_masked, originals)
        assert "```mermaid" in restored
        assert "```chart-json" in restored
        assert "graph TD; A-->B;" in restored
        assert '"type":"pie"' in restored


# =====================================================================
# BUG-P2a 测试：LIST_PAREN_PATTERN 支持 3 位数字 + 年份排除
# =====================================================================
class TestListParenPattern:
    def test_three_digit_list_matched(self):
        """(100) 外墙铲除 → 识别为有序列表"""
        from app.services.content_cleaning import _is_list_item
        is_list, content, ltype = _is_list_item("(100) 外墙铲除与基层处理")
        assert is_list is True
        assert ltype == "ordered"
        assert content == "外墙铲除与基层处理"

    def test_two_digit_list_still_works(self):
        """(12) 测试 → 识别为有序列表"""
        from app.services.content_cleaning import _is_list_item
        is_list, content, ltype = _is_list_item("(12) 测试项")
        assert is_list is True
        assert ltype == "ordered"

    def test_one_digit_list_still_works(self):
        """（1）项 → 识别为有序列表"""
        from app.services.content_cleaning import _is_list_item
        is_list, content, ltype = _is_list_item("（1）第一项")
        assert is_list is True
        assert ltype == "ordered"

    def test_year_excluded_from_list(self):
        """(2026)年... → 正文，不是列表"""
        from app.services.content_cleaning import _is_list_item
        is_list, content, ltype = _is_list_item("(2026)年度施工计划")
        # 年份是 4 位数，LIST_PAREN_PATTERN 现在匹配 \d{1,3}，所以 2026 不会命中
        # 即 is_list 应为 False
        assert is_list is False

    def test_year_with_chinese_paren_excluded(self):
        """（2026）年... → 正文"""
        from app.services.content_cleaning import _is_list_item
        is_list, content, ltype = _is_list_item("（2026）年版招标文件")
        assert is_list is False


# =====================================================================
# BUG-P3b 测试：正文标题编号净化
# =====================================================================
class TestStripHeadingNumberingFromContent:
    def test_strips_atx_decimal_numbering(self):
        """## 1.1 施工部署 → ## 施工部署；# 第一章 总则 → # 总则"""
        from app.services.content_cleaning import strip_heading_numbering_from_content
        content = """# 第一章 总则

## 1.1 施工部署

正文段落

### 1.1.1 外墙工程

更多正文"""
        result = strip_heading_numbering_from_content(content)
        # 所有 Markdown ATX 标题的编号前缀都应被剥离，包括"第X章"
        assert "# 总则" in result  # "第一章" 被剥离
        assert "## 施工部署" in result
        assert "### 外墙工程" in result

    def test_strips_chinese_numbering(self):
        """## 一、施工准备 → ## 施工准备"""
        from app.services.content_cleaning import strip_heading_numbering_from_content
        content = """## 一、施工准备

正文

### 二、材料进场

正文"""
        result = strip_heading_numbering_from_content(content)
        assert "## 施工准备" in result
        assert "### 材料进场" in result

    def test_preserves_code_blocks(self):
        """代码块内部不处理"""
        from app.services.content_cleaning import strip_heading_numbering_from_content
        content = """## 1.1 标题

```python
# 这是代码注释，不是标题
def foo(): pass
```

## 2.1 另一标题"""
        result = strip_heading_numbering_from_content(content)
        assert "## 标题" in result
        assert "## 另一标题" in result
        # 代码块内的 # 注释不应被改
        assert "# 这是代码注释，不是标题" in result

    def test_preserves_plain_paragraphs(self):
        """正文段落中的数字不被剥离"""
        from app.services.content_cleaning import strip_heading_numbering_from_content
        content = """## 1.1 标题

1. 这是列表项（不是 Markdown ATX 标题）
2. 另一个列表项

2026年施工计划正在推进。"""
        result = strip_heading_numbering_from_content(content)
        assert "## 标题" in result
        assert "1. 这是列表项" in result  # 正文列表项不变
        assert "2026年施工计划" in result  # 正文年份不变

    def test_empty_content(self):
        from app.services.content_cleaning import strip_heading_numbering_from_content
        assert strip_heading_numbering_from_content("") == ""
        assert strip_heading_numbering_from_content(None) is None  # type: ignore


# =====================================================================
# Risk-P2a / P2b / P3a 加固专项测试 2026-10-11
# =====================================================================
class TestForceCloseUnclosedFence:
    """Risk-P2a FIX：程序化兜底闭合"""

    def test_noop_when_all_fences_closed(self):
        from app.services.ai.workflows_sections import _force_close_unclosed_chart_fence
        ok = "正文\n\n```mermaid\ngraph TD; A-->B;\n```\n"
        result, closed = _force_close_unclosed_chart_fence(ok)
        assert closed is False
        assert result == ok

    def test_appends_close_when_mermaid_unclosed(self):
        from app.services.ai.workflows_sections import _force_close_unclosed_chart_fence
        bad = "正文\n\n```mermaid\ngraph TD; A-->B;\n被截断"
        result, closed = _force_close_unclosed_chart_fence(bad)
        assert closed is True
        assert result.rstrip().endswith("```")
        # 围栏内容仍在
        assert "graph TD; A-->B;" in result

    def test_appends_close_when_chart_json_unclosed(self):
        from app.services.ai.workflows_sections import _force_close_unclosed_chart_fence
        bad = "前文\n\n```chart-json\n{\"type\":\"pie\",\"data\":\n被截断"
        result, closed = _force_close_unclosed_chart_fence(bad)
        assert closed is True
        assert result.rstrip().endswith("```")

    def test_empty_content_noop(self):
        from app.services.ai.workflows_sections import _force_close_unclosed_chart_fence
        result, closed = _force_close_unclosed_chart_fence("")
        assert closed is False
        assert result == ""


class TestCondenseLeftoverPlaceholder:
    """Risk-P2b FIX：condense restore 后残留占位符检查"""

    def test_restore_without_leftovers(self):
        """正常 restore 后无残留占位符"""
        from app.services.ai.workflows_sections import (
            _mask_chart_fences_for_compression,
            _restore_chart_fences_after_compression,
        )
        orig = "正文\n\n```mermaid\ngraph A;\n```\n"
        masked, originals = _mask_chart_fences_for_compression(orig)
        restored = _restore_chart_fences_after_compression(masked, originals)
        assert "<<CHART_BLOCK_" not in restored
        assert "```mermaid" in restored

    def test_leftover_detected(self):
        """AI 篡改占位符后残留子串被检测（正则 <<...BLOCK...>> 命中）"""
        import re
        from app.services.ai.workflows_sections import (
            _mask_chart_fences_for_compression,
            _restore_chart_fences_after_compression,
        )
        orig = "正文\n\n```mermaid\ngraph A;\n```\n"
        masked, originals = _mask_chart_fences_for_compression(orig)
        # 模拟 AI 篡改占位符：把中间字符改掉，str.replace 无法匹配
        tampered_masked = masked.replace("CHART_BLOCK", "CHARTBLOCK")
        restored = _restore_chart_fences_after_compression(tampered_masked, originals)
        # restore 后残留篡改后的占位符 <<CHARTBLOCK_1>>
        # 正则 <<[^>]*[Bb][Ll][Oo][Cc][Kk][^>]*>> 应能命中
        _leftover_re = re.compile(r"<<[^>]*[Bb][Ll][Oo][Cc][Kk][^>]*>>")
        assert _leftover_re.search(restored) is not None


class TestStripHeadingSetext:
    """Risk-P3a FIX：Setext 标题（===/---）编号剥离"""

    def test_setext_h1_strips_numbering(self):
        """1.1 施工部署 + ===== → 施工部署 + ====="""
        from app.services.content_cleaning import strip_heading_numbering_from_content
        content = """1.1 施工部署
==========

正文段落"""
        result = strip_heading_numbering_from_content(content)
        assert "施工部署" in result
        assert "==========" in result
        # 编号前缀被剥离
        assert "1.1 施工部署" not in result

    def test_setext_h2_chinese_numbering(self):
        """一、施工准备 + ----- → 施工准备 + -----"""
        from app.services.content_cleaning import strip_heading_numbering_from_content
        content = """一、施工准备
----------

正文"""
        result = strip_heading_numbering_from_content(content)
        assert "施工准备" in result
        assert "----------" in result
        assert "一、施工准备" not in result

    def test_setext_not_false_positive_on_normal_content(self):
        """普通含 === 的正文（不是标题）不被误处理"""
        from app.services.content_cleaning import strip_heading_numbering_from_content
        content = """价格区间 500-1000 元
=====================
这是一段正文，包含数字和横线。"""
        result = strip_heading_numbering_from_content(content)
        # 普通正文行不应被当作 Setext 标题剥离
        assert "500-1000" in result
        # === 行保留
        assert "=====================" in result

    def test_atx_and_setext_mixed(self):
        """混合 ATX + Setext 标题都被处理"""
        from app.services.content_cleaning import strip_heading_numbering_from_content
        content = """# 第一章 总则

## 1.1 施工部署

施工准备
-------

正文"""
        result = strip_heading_numbering_from_content(content)
        assert "# 总则" in result
        assert "## 施工部署" in result
        assert "施工准备" in result
        assert "-------" in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
