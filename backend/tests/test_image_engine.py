#!/usr/bin/env python3
"""Unit tests for AI image engine module (validate_mermaid / repair_mermaid).

ILLUSTRATION-REMOVAL 2026-11-04：生图功能下线（遗留项3）——原
generate_illustration_arrange/prompt/image 与 build_illustration_url 的测试
随函数一并移除；生图服务类 ImageGenerationService（image_engine 内）唯一
消费方即已删除的 generate_illustration_image，同批删除。校验/修复函数的
测试全量保留。
"""

import pytest

from app.services.ai.image_engine import (
    repair_mermaid,
    validate_mermaid,
    generate_architecture_arrange,
    generate_labor_arrange,
    generate_flowchart_arrange,
)


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


def test_validate_mermaid_short_flowchart_all_directions():
    """BUG-FIX 2026-11-02 回归：所有合法方向（TD/TB/BT/RL/LR）的最小流程图
    都不得被"流程图代码过短"长度门误杀（旧实现只豁免 TD，LR 图被静默丢弃）。"""
    for code in (
        "graph LR\nA-->B",
        "graph RL\nA-->B",
        "graph TB\nA-->B",
        "graph BT\nA-->B",
        "flowchart LR\nA-->B",
    ):
        ok, msg = validate_mermaid(code)
        assert ok, f"{code!r} 应通过校验，实际: {msg}"


def test_validate_mermaid_degenerate_flowchart_still_rejected():
    """长度门泛化后，退化输入仍须被结构检查拦截，不得因豁免而放行。"""
    # 无箭头 → 缺连接关系
    ok, _ = validate_mermaid("graph LR\nA")
    assert not ok
    # 单节点裸行无箭头 → 落入语法错误/节点不足分支
    ok, msg = validate_mermaid("graph LR\nA-->B\nC")
    assert not ok, f"孤立无箭头行应被拦截: {msg}"


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


def test_imports_available():
    """Test that expected chart-arrangement functions are imported from image_engine."""
    from app.services.ai import image_engine
    assert hasattr(image_engine, 'generate_architecture_arrange')
    assert hasattr(image_engine, 'generate_labor_arrange')
    assert hasattr(image_engine, 'generate_flowchart_arrange')
    # 生图功能已下线（ILLUSTRATION-REMOVAL 2026-11-04）：函数与缓存不再存在
    assert not hasattr(image_engine, 'generate_illustration_arrange')
    assert not hasattr(image_engine, 'generate_illustration_prompt')
    assert not hasattr(image_engine, 'generate_illustration_image')
    assert not hasattr(image_engine, 'build_illustration_url')
    assert not hasattr(image_engine, 'ImageGenerationService')


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
