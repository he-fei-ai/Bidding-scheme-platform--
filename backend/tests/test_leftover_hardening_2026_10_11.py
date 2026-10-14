"""遗留项加固测试（2026-10-11 第三轮：tiktoken + chart_validators 激活）。

覆盖：
  - 遗留项3：estimate_tokens tiktoken 精确计数 + 启发式回退
  - 遗留项2：chart_validators v3.1 校验器在 fence 路径激活
"""

import json
import logging

import pytest

from app.services.ai.prompts import estimate_tokens
from app.services.ai.prompts._context import _TIKTOKEN_ENCODER
from app.services.content_cleaning import _fence_payload_to_marker_text


class TestEstimateTokensTiktoken:
    """遗留项3：tiktoken 精确计数集成。"""

    def test_tiktoken_available(self):
        """tiktoken 已安装时编码器可用。"""
        # 本环境已安装 tiktoken
        assert _TIKTOKEN_ENCODER is not None

    def test_ascii_accuracy(self):
        """ASCII 文本：tiktoken 计数比启发式更准确。"""
        text = "hello world"  # tiktoken: 2 tokens; 启发式: (11+3)//4 = 3
        result = estimate_tokens(text)
        assert result == 2  # tiktoken 精确值

    def test_cjk_accuracy(self):
        """CJK 文本：tiktoken 与启发式接近（每 CJK ≈ 1-2 token）。"""
        text = "项目施工方案"  # 6 CJK 字符
        result = estimate_tokens(text)
        # tiktoken 对 CJK 通常 1-2 token/字，结果应 > 0 且合理
        assert 3 <= result <= 12

    def test_empty_returns_zero(self):
        assert estimate_tokens("") == 0

    def test_mixed_text_positive(self):
        """混排文本：计数为正。"""
        assert estimate_tokens("项目 content 项目") > 0

    def test_truncate_uses_tiktoken_budget(self):
        """截断函数使用 tiktoken 计数后仍不超预算。"""
        from app.services.ai.prompts import truncate_to_tokens

        text = "a" * 5000
        max_tokens = 100
        result = truncate_to_tokens(text, max_tokens)
        assert estimate_tokens(result) <= max_tokens


class TestChartValidatorsFenceActivation:
    """遗留项2：chart_validators v3.1 在 fence 路径激活。"""

    def test_valid_gantt_passes_silently(self, caplog):
        """合法甘特图数据：校验通过，无 WARNING。"""
        body = json.dumps({
            "type": "gantt",
            "title": "施工进度计划",
            "totalDays": 60,
            "tasks": [
                {"id": 1, "name": "基础施工", "start": 1, "end": 10, "dependencies": [], "isMilestone": False},
                {"id": 2, "name": "主体施工", "start": 11, "end": 40, "dependencies": [1], "isMilestone": False},
            ],
        }, ensure_ascii=False)
        with caplog.at_level(logging.WARNING):
            result = _fence_payload_to_marker_text("chart-json", body)
        assert result is not None
        # 合法数据不应有校验 WARNING
        assert not any("图表校验" in r.message for r in caplog.records)

    def test_invalid_gantt_logs_warning(self, caplog):
        """非法甘特图数据（重复 ID）：校验失败记 WARNING，但不删块。"""
        body = json.dumps({
            "type": "gantt",
            "title": "进度计划",
            "totalDays": 30,
            "tasks": [
                {"id": 1, "name": "任务A", "start": 1, "end": 5, "dependencies": [], "isMilestone": False},
                {"id": 1, "name": "任务B", "start": 6, "end": 10, "dependencies": [99], "isMilestone": False},
            ],
        }, ensure_ascii=False)
        with caplog.at_level(logging.WARNING):
            result = _fence_payload_to_marker_text("chart-json", body)
        # 不删块（向后兼容，交渲染层降级）
        assert result is not None
        # 应有校验 WARNING（重复 ID + 不存在的依赖）
        assert any("图表校验" in r.message for r in caplog.records)

    def test_unsupported_type_skips_validation(self, caplog):
        """未覆盖类型（如 comparison）：跳过校验，无 WARNING。"""
        body = json.dumps({
            "type": "comparison",
            "title": "方案对比",
            "headers": ["指标", "A", "B"],
            "rows": [{"label": "工期", "values": [30, 45]}],
        }, ensure_ascii=False)
        with caplog.at_level(logging.WARNING):
            result = _fence_payload_to_marker_text("chart-json", body)
        assert result is not None
        assert not any("图表校验" in r.message for r in caplog.records)

    def test_valid_flowchart_passes_silently(self, caplog):
        """合法流程图数据：校验通过。"""
        body = json.dumps({
            "type": "flowchart",
            "title": "施工流程",
            "nodes": [
                {"id": "a", "label": "开工"},
                {"id": "b", "label": "施工"},
                {"id": "c", "label": "竣工"},
            ],
            "edges": [
                {"from": "a", "to": "b"},
                {"from": "b", "to": "c"},
            ],
        }, ensure_ascii=False)
        with caplog.at_level(logging.WARNING):
            result = _fence_payload_to_marker_text("chart-json", body)
        assert result is not None
        assert not any("图表校验" in r.message for r in caplog.records)

    def test_mermaid_block_unaffected_by_validator(self, caplog):
        """mermaid 围栏块不触发 chart_validators（仅 chart-json 触发）。"""
        body = "flowchart TD\nA[开工] --> B[竣工]"
        with caplog.at_level(logging.WARNING):
            result = _fence_payload_to_marker_text("mermaid", body)
        assert result is not None
        assert not any("图表校验" in r.message for r in caplog.records)


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
