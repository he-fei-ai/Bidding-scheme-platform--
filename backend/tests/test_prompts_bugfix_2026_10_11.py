"""提示词模块深度审计与 BUG 修复测试（2026-10-11）。

覆盖本轮修复的三项缺陷：
  - BUG-P1 (C9)：truncate_to_tokens ASCII 文本预算浪费 75%
  - BUG-P2 (O4)：validate_output_format 遗漏 6 种合法 Mermaid 类型
  - BUG-P3 (O5)：strip_code_fences 不认连字符语言标识（chart-json 泄漏）
"""

import pytest

from app.services.ai.prompts import (
    estimate_tokens,
    strip_code_fences,
    truncate_to_tokens,
    validate_output_format,
)
from app.services.ai.prompts._context import _TRUNCATION_MARKER


class TestTruncateToTokensBudget:
    """truncate_to_tokens 必须对 CJK 和 ASCII 都充分利用预算。"""

    def test_cjk_text_uses_budget(self):
        text = "项目" * 500  # 1000 CJK 字符 ≈ 1000 token
        max_tokens = 400
        result = truncate_to_tokens(text, max_tokens)
        assert result.endswith(_TRUNCATION_MARKER)
        assert estimate_tokens(result) <= max_tokens

    def test_ascii_text_uses_full_budget(self):
        """ASCII 文本：修复前只用 25% 预算，修复后应接近 max_tokens。"""
        text = "a" * 5000  # 5000 ASCII 字符 ≈ 1250 token
        max_tokens = 400
        result = truncate_to_tokens(text, max_tokens)
        assert estimate_tokens(result) <= max_tokens
        assert estimate_tokens(result) >= 300, (
            f"ASCII 预算浪费：结果仅 {estimate_tokens(result)} token"
        )

    def test_mixed_text_uses_budget(self):
        text = "项目说明 content " * 300
        max_tokens = 300
        result = truncate_to_tokens(text, max_tokens)
        assert estimate_tokens(result) <= max_tokens
        assert estimate_tokens(result) >= 200

    def test_short_text_not_truncated(self):
        assert truncate_to_tokens("短文本", 1000) == "短文本"

    def test_empty_text(self):
        assert truncate_to_tokens("", 100) == ""
        assert truncate_to_tokens(None, 100) is None

    def test_result_does_not_exceed_budget(self):
        # max_tokens 须大于截断标记自身开销（~14 token），否则标记本身即越界
        for max_tokens in (50, 100, 500):
            text = "abcdefghij" * 1000
            result = truncate_to_tokens(text, max_tokens)
            assert estimate_tokens(result) <= max_tokens


class TestValidateOutputFormatMermaidTypes:
    """validate_output_format 必须接受所有 image_engine.validate_mermaid 支持的类型。"""

    @pytest.mark.parametrize("chart_code", [
        "flowchart TD\nA-->B",
        "graph TD\nA-->B",
        "gantt\ndateFormat YYYY-MM-DD\nsection S\nT :a, 2026-01-01, 3d",
        "sequenceDiagram\nA->>B: msg",
        "classDiagram\nclass A",
        "stateDiagram\n[*]-->A",
        "stateDiagram-v2\n[*]-->A",
        "erDiagram\nA ||--o{ B",
        "pie title Test\n\"a\": 1",
        "xychart-beta\n",
        "timeline\ntitle T\nsection S\nEvent : 2026-01-01",
        "journey\ntitle T\nsection S",
    ])
    def test_all_mermaid_types_accepted(self, chart_code):
        ok, msg = validate_output_format(chart_code, "mermaid")
        assert ok is True, f"合法类型被误拒: {chart_code[:30]}... -> {msg}"

    def test_invalid_mermaid_rejected(self):
        ok, msg = validate_output_format("random text not mermaid", "mermaid")
        assert ok is False
        assert "Mermaid" in msg


class TestStripCodeFencesHyphenatedLang:
    """strip_code_fences 必须正确处理含连字符的语言标识（chart-json）。"""

    def test_chart_json_fence_clean_extraction(self):
        text = '```chart-json\n{"type": "gantt", "title": "进度计划"}\n```'
        result = strip_code_fences(text)
        assert not result.startswith("-"), f"连字符语言标识泄漏: {result[:30]}"
        assert '"type"' in result

    def test_plain_json_fence_still_works(self):
        text = '```json\n{"key": "value"}\n```'
        result = strip_code_fences(text)
        assert result == '{"key": "value"}'

    def test_plain_fence_still_works(self):
        result = strip_code_fences('```\nplain content\n```')
        assert "plain content" in result

    def test_multiple_fences_picks_longest(self):
        text = '```json\n{"a": 1}\n```\n说明\n```json\n{"longer": "content here"}\n```'
        result = strip_code_fences(text)
        assert "longer" in result

    def test_no_fence_returns_stripped(self):
        assert strip_code_fences("  plain text  ") == "plain text"
        assert strip_code_fences("") == ""


class TestPromptChartFenceConsistency:
    """提示词模块产出的图表围栏格式与图表管线解析口径一致性。"""

    def test_strip_handles_mermaid_fence(self):
        text = '```mermaid\nflowchart TD\nA-->B\n```'
        result = strip_code_fences(text)
        assert "flowchart" in result
        assert "```" not in result

    def test_strip_handles_chart_json_fence(self):
        text = '```chart-json\n{"type": "labor", "title": "劳动力配置"}\n```'
        result = strip_code_fences(text)
        assert "```" not in result
        assert not result.startswith("-")
        assert '"type"' in result


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
