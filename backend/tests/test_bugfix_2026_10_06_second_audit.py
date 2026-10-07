"""BUG 回归测试 2026-10-06：正文生成模块第二轮深度审计修复。

覆盖：
1. 未闭合普通代码围栏（python/json/text 等）内容泄漏为正文段落
2. "数字+空格+中文标题"（无顿号/句点）不被识别为 L3 标题，被拍平为段落
"""
import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.services.content_cleaning import (
    clean_generated_content,
    parse_content,
    _match_heading,
    _strip_unclosed_plain_fences,
)


class TestUnclosedPlainFenceFix:
    """BUG-1: 未闭合普通代码围栏内容泄漏为正文。"""

    def test_unclosed_python_fence_strips_entire_bad_block(self):
        """未闭合 ```python 块的代码行（import/def/return）不得出现在正文。"""
        raw = """一、施工准备

本工程开工前需完成以下准备工作。

```python
import os
def setup():
    return True
（截断了，没有闭合围栏）
"""
        result = clean_generated_content(raw)
        assert "import os" not in result
        assert "def setup" not in result
        assert "return True" not in result
        # 前面的正文保留
        assert "本工程开工前需完成以下准备工作" in result

    def test_unclosed_json_fence_strips_bad_block(self):
        """未闭合 ```json 块的 JSON 内容不得泄漏为正文。"""
        raw = """项目概述如下：

```json
{
  "name": "测试项目",
  "budget": 1000000
}
截断了
"""
        result = clean_generated_content(raw)
        assert '"name"' not in result
        assert "测试项目" not in result  # 在坏块内，应被删除
        assert "项目概述如下" in result

    def test_closed_plain_fence_content_preserved(self):
        """闭合的普通代码块内容保留为正文（既有行为，向后兼容）。"""
        raw = """一、施工准备

```
示例格式：
第一条 第一条内容
```

二、施工流程
"""
        result = clean_generated_content(raw)
        assert "示例格式" in result
        assert "第一条 第一条内容" in result

    def test_unclosed_fence_at_end_removes_tail(self):
        """未闭合围栏出现在文末时，坏块及之后的行全部删除。"""
        raw = """正常正文段落。

```text
这段代码块没有闭合
后面还有一行正文
"""
        result = clean_generated_content(raw)
        assert "正常正文段落" in result
        assert "这段代码块没有闭合" not in result

    def test_mermaid_unclosed_still_handled_by_protect_layer(self):
        """未闭合 mermaid 围栏仍由 _protect_chart_fences 处理（不被双重删除）。"""
        raw = """正文...

```mermaid
graph TD
A[开始] --> B[结束]
未闭合
"""
        result = clean_generated_content(raw)
        assert "graph TD" not in result  # 图表坏块也被清理
        assert "正文" in result


class TestHeadingPatternL3SpaceFix:
    """BUG-2: "数字+空格+中文标题" 不被识别为 L3 标题。"""

    @pytest.mark.parametrize("text", [
        "1 施工准备",
        "12 安全文明施工",
        "3 质量管理体系",
    ])
    def test_number_space_chinese_recognized_as_l3(self, text):
        result = _match_heading(text)
        assert result is not None
        assert result[1] == 3, f"{text!r} 应识别为 L3 标题"

    @pytest.mark.parametrize("text", [
        "1、施工准备",   # 顿号
        "1. 施工准备",   # 句点+空格
        "1.施工准备",    # 句点无空格
    ])
    def test_original_l3_patterns_still_work(self, text):
        result = _match_heading(text)
        assert result is not None
        assert result[1] == 3

    @pytest.mark.parametrize("text", [
        "1 000 元",     # 空格后是数字（金额）
        "2 500 人",     # 空格后是数字（数量）
    ])
    def test_number_space_number_not_mistaken_as_heading(self, text):
        """空格后跟数字的正文（金额/数量）不得被误判为标题。"""
        result = _match_heading(text)
        assert result is None, f"{text!r} 不应被识别为标题"

    @pytest.mark.parametrize("text,expected_level", [
        ("1.1 总体方案", 4),
        ("1.1.1 子项", 5),
        ("一、工程概况", 2),
        ("第3章 施工组织", 1),
    ])
    def test_other_levels_unaffected(self, text, expected_level):
        result = _match_heading(text)
        assert result is not None
        assert result[1] == expected_level

    def test_parse_content_integrates_l3_space_heading(self):
        """端到端：parse_content 把 '数字+空格' 标题解析为 heading3 block。"""
        raw = """1 施工准备

本工程开工前需完成以下准备工作。
"""
        blocks = parse_content(raw)
        assert blocks[0].type == "heading3"
        assert "施工准备" in blocks[0].text
