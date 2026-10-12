"""提示词模块遗留项加固测试（2026-10-11 续）。

覆盖本轮加固的两项遗留项：
  - C10：truncate_to_tokens 小预算标记越界降级
  - 遗留项1：collect_json_response 静态修复前置（激活 _output_repair）
"""

import pytest

from app.services.ai.prompts import (
    estimate_tokens,
    truncate_to_tokens,
)
from app.services.ai.prompts._context import _TRUNCATION_MARKER


class TestTruncateSmallBudgetMarker:
    """C10：小预算场景截断标记本身越界 → 降级为无标记截断。"""

    def test_small_budget_does_not_exceed(self):
        """max_tokens 小于标记开销时，产出不超预算。"""
        text = "abcdefghij" * 100  # 1000 字符
        marker_cost = estimate_tokens(_TRUNCATION_MARKER)
        # 用比标记更小的预算
        small_budget = max(1, marker_cost - 5)
        result = truncate_to_tokens(text, small_budget)
        assert estimate_tokens(result) <= small_budget, (
            f"小预算越界：{estimate_tokens(result)} > {small_budget}"
        )

    def test_tiny_budget_returns_no_marker(self):
        """极小预算（1 token）不应返回截断标记。"""
        text = "a" * 200
        result = truncate_to_tokens(text, 1)
        assert _TRUNCATION_MARKER not in result
        assert estimate_tokens(result) <= 1

    def test_normal_budget_still_has_marker(self):
        """正常预算仍附截断标记（回归保护）。"""
        text = "a" * 5000
        result = truncate_to_tokens(text, 100)
        assert _TRUNCATION_MARKER in result

    def test_budget_equals_marker_cost_boundary(self):
        """预算恰好等于标记开销：应能容纳标记 + 少量内容。"""
        text = "x" * 200
        marker_cost = estimate_tokens(_TRUNCATION_MARKER)
        result = truncate_to_tokens(text, marker_cost)
        assert estimate_tokens(result) <= marker_cost


class TestCollectJsonStaticRepair:
    """遗留项1：collect_json_response 静态修复前置。

    验证 AI 返回「合法 JSON 被代码围栏包裹」时，静态修复直接提取，
    不触发 AI 修复调用（省成本 + 降延迟）。
    """

    @pytest.mark.asyncio
    async def test_static_repair_catches_fenced_json(self):
        """AI 返回 ```json 围栏包裹的合法 JSON → 静态修复成功，不调 AI 修复。"""
        from app.services.ai.workflows_base import collect_json_response

        call_count = {"chat": 0}

        class _FakeProvider:
            async def chat(self, messages, **kwargs):
                call_count["chat"] += 1
                # 第一次 chat 返回围栏包裹的 JSON
                if call_count["chat"] == 1:
                    return '以下是结果：\n```json\n{"level": 1, "title": "施工准备", "children": []}\n```\n以上。'
                # 如果走到 AI 修复（不应发生），返回合法 JSON
                return '{"level": 1, "title": "施工准备", "children": []}'

        from app.services.ai.providers.base import AIMessage

        result = await collect_json_response(
            _FakeProvider(),
            [AIMessage(role="user", content="test")],
            max_retries=1,
        )
        # 静态修复应成功提取 JSON
        assert isinstance(result, dict)
        assert result["title"] == "施工准备"
        # 只调了 1 次 AI（初始生成），未调 AI 修复
        assert call_count["chat"] == 1, (
            f"静态修复应跳过 AI 修复，但调了 {call_count['chat']} 次"
        )

    @pytest.mark.asyncio
    async def test_static_repair_fails_falls_through_to_ai(self):
        """静态修复失败（JSON 本身非法）→ 继续走 AI 修复链路。"""
        from app.services.ai.workflows_base import collect_json_response
        from app.services.ai.providers.base import AIMessage

        call_count = {"chat": 0}

        class _FakeProvider:
            async def chat(self, messages, **kwargs):
                call_count["chat"] += 1
                if call_count["chat"] == 1:
                    # 返回根本无法解析的内容
                    return "这不是 JSON，也无法提取"
                # AI 修复返回合法 JSON
                return '{"result": "repaired"}'

        result = await collect_json_response(
            _FakeProvider(),
            [AIMessage(role="user", content="test")],
            max_retries=1,
        )
        # AI 修复应成功
        assert isinstance(result, dict)
        assert result["result"] == "repaired"
        # 调了 2 次 AI（初始 + 修复）
        assert call_count["chat"] == 2

    @pytest.mark.asyncio
    async def test_static_repair_with_schema_validation(self):
        """静态修复结果需通过 schema 校验才返回。"""
        from app.services.ai.workflows_base import collect_json_response
        from app.services.ai.providers.base import AIMessage
        from pydantic import BaseModel

        class _Schema(BaseModel):
            name: str
            value: int

        class _FakeProvider:
            async def chat(self, messages, **kwargs):
                return '```json\n{"name": "test", "value": 42}\n```'

        result = await collect_json_response(
            _FakeProvider(),
            [AIMessage(role="user", content="test")],
            max_retries=1,
            schema=_Schema,
        )
        assert result.name == "test"
        assert result.value == 42


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
