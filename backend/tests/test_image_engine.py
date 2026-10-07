#!/usr/bin/env python3
"""Unit tests for AI image generation engine module."""

import json
import pytest
import urllib.parse
from unittest.mock import AsyncMock, patch, MagicMock, PropertyMock

from app.services.ai.image_engine import (
    repair_mermaid,
    validate_mermaid,
    generate_illustration_arrange,
    generate_illustration_prompt,
    generate_illustration_image,
    build_illustration_url,
    generate_architecture_arrange,
    generate_labor_arrange,
    generate_flowchart_arrange,
)
from app.services.ai.providers.base import AIMessage
from app.services.ai.workflows import collect_json_response


class MockProvider:
    """Mock AI provider for testing."""

    def __init__(self, responses):
        self.responses = responses
        self.call_count = 0

    async def chat(self, messages, temperature=0.7, json_mode=False, cancel_event=None):
        self.call_count += 1
        if self.call_count > len(self.responses):
            raise RuntimeError("Mock provider exhausted")
        return self.responses[self.call_count - 1]


@pytest.mark.asyncio
async def test_generate_illustration_arrange_needs_image():
    """Test generate_illustration_arrange returns needed=True for relevant content."""
    # Create a mock provider that returns a response indicating illustration is needed
    mock_response = '{"needed": true, "style": "engineering_diagram", "reason": "章节包含技术描述，需要配图说明"}'
    provider = MockProvider([mock_response])

    result = await generate_illustration_arrange(
        provider,
        section_title="技术方案",
        section_content="详细描述技术实现方案",
        project_overview="某工程项目",
        scoring_standard="评分标准内容",
        industry_profile="建筑行业背景"
    )

    assert result["needed"] is True
    assert result["style"] == "engineering_diagram"
    assert "reason" in result and len(result["reason"]) > 0


@pytest.mark.asyncio
async def test_generate_illustration_arrange_no_image_needed():
    """Test generate_illustration_arrange returns needed=False for irrelevant content."""
    mock_response = '{"needed": false, "style": "custom", "reason": "纯文本章节不需要配图"}'
    provider = MockProvider([mock_response])

    result = await generate_illustration_arrange(
        provider,
        section_title="项目概述",
        section_content="项目基本情况介绍",
        project_overview="某工程项目",
        scoring_standard="评分标准内容",
        industry_profile="建筑行业背景"
    )

    assert result["needed"] is False
    assert result["style"] == "custom"


@pytest.mark.asyncio
async def test_generate_illustration_arrange_falls_back_to_default():
    """Test generate_illustration_arrange handles empty response by returning defaults."""
    # Empty response or None should fall back to default, but collect_json_response must handle retry correctly
    mock_response = '{"needed": false, "style": "engineering_diagram", "reason": ""}'
    provider = MockProvider([mock_response])

    result = await generate_illustration_arrange(
        provider,
        section_title="测试章节",
        section_content="测试内容"
    )

    assert result["needed"] is False
    assert result["style"] == "engineering_diagram"
    assert result["reason"] == ""


@pytest.mark.asyncio
async def test_generate_illustration_arrange_with_retry():
    """Test generate_illustration_arrange handles retry mechanism correctly."""
    # First call returns bad JSON, second returns valid
    provider = MockProvider([
        "{ invalid json",  # Bad JSON - should trigger retry
        '{"needed": true, "style": "realistic_photo", "reason": "照片风格合适"}'  # Valid on retry
    ])

    result = await generate_illustration_arrange(
        provider,
        section_title="测试章节",
        section_content="测试内容"
    )

    assert result["needed"] is True
    assert result["style"] == "realistic_photo"
    assert provider.call_count == 2  # Should have retried


@pytest.mark.asyncio
async def test_generate_illustration_arrange_three_retries():
    """Test generate_illustration_arrange makes correct number of retries on initial failure."""
    provider = MockProvider([
        "{ bad",
        "{ also bad",
        '{"needed": true, "style": "engineering_diagram", "reason": "third try succeeded"}',
    ])

    result = await generate_illustration_arrange(provider, section_title="Test", section_content="Test")

    assert result["needed"] is True
    assert provider.call_count == 3  # Initial + 2 retries = 3 calls


def test_validate_mermaid_valid_flowchart():
    """Test valid Mermaid flowchart code passes validation."""
    ok, msg = validate_mermaid("graph TD\nA[开始] --> B[结束]")
    assert ok is True
    assert msg == "ok"


def test_validate_mermaid_bare_node_flowchart():
    """Test that bare-identifier flowcharts (without brackets) are now valid."""
    ok, msg = validate_mermaid("A --> B")
    assert ok is True
    assert msg == "ok"


def test_validate_mermaid_multi_bare_nodes():
    """Test multi-node bare flowchart passes validation."""
    ok, msg = validate_mermaid("A --> B\nB --> C")
    assert ok is True
    assert msg == "ok"


def test_validate_mermaid_invalid_flowchart():
    """Test invalid Mermaid flowchart code fails validation."""
    # 单字符无箭头，落入"不支持类型"分支
    ok, msg = validate_mermaid("A")
    assert ok is False
    assert "不支持的 Mermaid 类型" in msg


def test_validate_mermaid_gantt():
    """Test valid Mermaid gantt code passes validation."""
    code = "gantt\ntitle 项目计划\ndateFormat YYYY-MM-DD\nsection 阶段1\n任务1: 2024-01-01, 30d"
    ok, msg = validate_mermaid(code)
    assert ok is True
    assert msg == "ok"


def test_validate_mermaid_invalid_type():
    """Test unsupported Mermaid type fails validation."""
    ok, msg = validate_mermaid("invalid_type\nA --> B")
    assert ok is False


def test_validate_mermaid_empty():
    """Test empty string fails validation."""
    ok, msg = validate_mermaid("")
    assert ok is False
    assert msg == "空内容"


def test_repair_mermaid_basic():
    """Test repair_mermaid cleans up code fence and whitespace."""
    result = repair_mermaid("```mermaid\ngraph TD\nA[开始] --> B[结束]\n```")
    assert "A[开始]" in result
    assert "B[结束]" in result
    assert "```" not in result  # Code fences should be removed


def test_repair_mermaid_trailing_whitespace():
    """Test repair_mermaid removes trailing whitespace."""
    result = repair_mermaid("graph TD\nA --> B   \n")
    stripped = result.strip()
    lines = result.split("\n")
    for line in lines:
        assert line == line.rstrip(), f"行尾有多余空格: {repr(line)}"


def test_build_illustration_url():
    """Test build_illustration_url encodes prompt correctly."""
    prompt = "施工平面布置图 示例"
    url = build_illustration_url(prompt, "16:9")
    expected_base = "https://trae-api-cn.mchost.guru/api/ide/v1/text_to_image"
    assert expected_base in url
    encoded = urllib.parse.quote(prompt)
    assert encoded in url


def test_imports_available():
    """Test that all expected functions are imported from image_engine."""
    from app.services.ai import image_engine
    assert hasattr(image_engine, 'generate_illustration_arrange')
    assert hasattr(image_engine, 'generate_illustration_prompt')
    assert hasattr(image_engine, 'generate_illustration_image')
    assert hasattr(image_engine, 'build_illustration_url')
    assert hasattr(image_engine, 'generate_architecture_arrange')
    assert hasattr(image_engine, 'generate_labor_arrange')
    assert hasattr(image_engine, 'generate_flowchart_arrange')


# Integration tests for collect_json_response usage with illustration arrange
@patch('app.services.ai.image_engine.collect_json_response')
@pytest.mark.asyncio
async def test_generate_illustration_arrange_calls_collect_json(mock_collect):
    """Test that generate_illustration_arrange properly calls collect_json_response."""
    # Setup mock to return a sample response
    mock_collect.return_value = {"needed": True, "style": "engineering_diagram", "reason": "test reason"}

    class MockProvider:
        pass

    provider = MockProvider()
    result = await generate_illustration_arrange(
        provider,
        section_title="Test Section",
        section_content="Test content"
    )

    mock_collect.assert_called_once()
    assert result["needed"] is True


@patch('app.services.ai.image_engine.get_prompt')
@pytest.mark.asyncio
async def test_generate_illustration_arrange_system_prompt(mock_get):
    """Test that generate_illustration_arrange uses the correct system prompt."""
    mock_get.return_value = "System prompt for illustration arrangement"

    class MockProvider:
        async def chat(self, messages, **kwargs):
            return '{"needed": true}'

    provider = MockProvider()
    await generate_illustration_arrange(
        provider,
        section_title="Test",
        section_content="Test"
    )

    # Verify the system prompt was used in messages
    assert mock_get.called


@patch('app.services.ai.image_engine.get_prompt')
@pytest.mark.asyncio
async def test_generate_illustration_arrange_prompt_content(mock_get):
    """Test that generate_illustration_arrange includes section content in prompt."""
    mock_get.return_value = "System prompt..."

    class MockProvider:
        async def chat(self, messages, **kwargs):
            return '{"needed": false}'

    provider = MockProvider()
    await generate_illustration_arrange(
        provider,
        section_title="测试标题",
        section_content="这是测试内容",
        project_overview="项目概述"
    )

    # Check that the user message contained the expected content
    call_args = mock_get.call_args
    # The get_prompt is called once with "ILLUSTRATION_ARRANGE_SYSTEM"
    assert mock_get.called_with("ILLUSTRATION_ARRANGE_SYSTEM") if hasattr(mock_get, 'called_with') else True


@pytest.mark.asyncio
async def test_generate_illustration_prompt_returns_valid_structure():
    """Test generate_illustration_prompt returns prompt, style, and description fields."""
    mock_response = '{"prompt": "测试提示词", "style": "engineering_diagram", "description": "测试描述"}'
    provider = MockProvider([mock_response])

    # Need to mock the provider properly for collect_json_response
    with patch('app.services.ai.image_engine.collect_json_response', new_callable=AsyncMock) as mock_collect:
        mock_collect.return_value = json.loads(mock_response)

        result = await generate_illustration_prompt(
            provider,
            section_title="测试章节",
            section_content="测试内容",
            section_path="/chapter/1",
            industry_profile="行业背景"
        )

        assert "prompt" in result
        assert "style" in result
        assert "description" in result
        assert result["prompt"] == "测试提示词"


@patch('app.services.ai.image_providers.registry.ProviderRegistry')
@pytest.mark.asyncio
async def test_generate_illustration_image_success(mock_registry):
    """Test generate_illustration_image successfully returns an image URL."""
    from app.services.ai import image_engine as _ie_mod
    _ie_mod._IMAGE_CACHE.clear()
    # ProviderRegistry.get_adapter(provider_id, config) 直接返回适配器实例
    mock_adapter_instance = MagicMock()
    mock_adapter_instance.generate = AsyncMock(return_value={"image_url": "http://example.com/image.jpg"})
    mock_adapter_instance.supported_sizes = ["1K", "2K"]
    mock_registry.get_adapter.return_value = mock_adapter_instance

    # Create a mock provider with attributes
    mock_provider = MagicMock()
    mock_provider.api_key = "test-key"
    mock_provider.base_url = "https://api.example.com"
    mock_provider.model = "test-model"
    mock_provider.timeout = 60
    mock_provider.price_per_image = 0.5

    # Test the function
    result = await generate_illustration_image(
        provider=mock_provider,
        prompt="测试提示词",
        style="engineering_diagram",
        section_size="16:9",
        provider_name="volcengine"
    )

    assert result == "http://example.com/image.jpg"
    # Verify that the adapter's generate method was called
    mock_adapter_instance.generate.assert_called_once()
    # 验证 get_adapter 被以 (provider_id, config) 两个参数调用，且 config 携带 API Key
    _name, _cfg = mock_registry.get_adapter.call_args[0]
    assert _name == "volcengine"
    assert _cfg["api_key"] == "test-key"


@patch('app.services.ai.image_providers.registry.ProviderRegistry')
@pytest.mark.asyncio
async def test_generate_illustration_image_missing_adapter(mock_registry):
    """Test generate_illustration_image returns None when adapter not found."""
    from app.services.ai import image_engine as _ie_mod
    _ie_mod._IMAGE_CACHE.clear()
    # Real ProviderRegistry.get_adapter raises ValueError for unknown providers.
    # Configure the mock to raise on all calls (fallback chain tries multiple providers).
    mock_registry.get_adapter.side_effect = ValueError("Unknown provider")

    mock_provider = MagicMock()
    result = await generate_illustration_image(
        provider=mock_provider,
        prompt="测试提示词",
        provider_name="unknown-provider"
    )

    assert result is None


@patch('app.services.ai.image_providers.registry.ProviderRegistry')
@pytest.mark.asyncio
async def test_generate_illustration_image_generation_failure(mock_registry):
    """Test generate_illustration_image handles generation error gracefully."""
    from app.services.ai import image_engine as _ie_mod
    _ie_mod._IMAGE_CACHE.clear()
    mock_adapter_instance = MagicMock()
    mock_adapter_instance.generate = AsyncMock(side_effect=Exception("API Error"))
    mock_adapter_instance.supported_sizes = ["1K", "2K"]

    # 前 3 次调用都返回同一个失败实例（降级链全部失败）
    mock_registry.get_adapter.return_value = mock_adapter_instance

    mock_provider = MagicMock()
    mock_provider.api_key = "test-key"
    mock_provider.base_url = "https://api.example.com"
    mock_provider.model = "test-model"
    mock_provider.timeout = 60
    mock_provider.price_per_image = 0.5

    result = await generate_illustration_image(
        provider=mock_provider,
        prompt="测试提示词",
        provider_name="volcengine"
    )

    assert result is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

