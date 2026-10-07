"""提示词模块增强功能测试（2026-10-06 第二轮加固）。

覆盖本轮新增功能：
- build_chapter_context 统一上下文组装
- extract_json_body / try_parse_json / validate_output_format 输出修复工具
- get_prompt_metrics / reset_prompt_metrics 监控指标
- 硬编码漂移检测（间接验证）
"""

import pytest

from app.services.ai.prompts import (
    build_chapter_context,
    estimate_tokens,
    extract_json_body,
    get_prompt,
    get_prompt_metrics,
    reset_prompt_metrics,
    sanitize_prompt_value,
    strip_code_fences,
    try_parse_json,
    validate_output_format,
)


class TestBuildChapterContext:
    """统一上下文组装高层接口测试。"""

    def test_empty_input_returns_empty(self):
        """空输入返回空字符串。"""
        result = build_chapter_context()
        assert result == ""

    def test_single_part(self):
        """单个片段正常组装。"""
        result = build_chapter_context(summary="项目概述内容")
        assert "项目概述内容" in result
        assert "SUMMARY" in result

    def test_multiple_parts_ordered_by_priority(self):
        """多片段按优先级排序（全局事实优先于项目概述）。"""
        result = build_chapter_context(
            summary="项目概述",
            global_facts="全局事实",
        )
        # GLOBAL_FACTS 优先级 100 > SUMMARY 90，应排在前面
        pos_facts = result.find("GLOBAL_FACTS")
        pos_summary = result.find("SUMMARY")
        assert pos_facts < pos_summary

    def test_token_budget_drops_low_priority(self):
        """Token 预算不足时丢弃低优先级片段。"""
        long_content = "x" * 5000  # 约5000 token
        result = build_chapter_context(
            global_facts="重要事实",
            other_chapters=long_content,
            max_tokens=100,
        )
        # 高优先级的 GLOBAL_FACTS 应保留
        assert "重要事实" in result
        # 低优先级的 OTHER_CHAPTERS 应被丢弃（预算不足）
        assert "OTHER_CHAPTERS" not in result or "截断" in result

    def test_skip_empty_parts(self):
        """空内容片段自动跳过。"""
        result = build_chapter_context(
            summary="有内容",
            global_facts="",
            scoring_points="   ",  # 纯空白
        )
        assert "有内容" in result
        assert "GLOBAL_FACTS" not in result
        assert "SCORING_POINTS" not in result


class TestOutputRepair:
    """输出格式修复工具测试。"""

    def test_strip_code_fences_with_fence(self):
        """剥离 Markdown 代码围栏。"""
        text = '```json\n{"a": 1}\n```'
        result = strip_code_fences(text)
        assert result == '{"a": 1}'

    def test_strip_code_fences_without_fence(self):
        """无围栏时原样返回。"""
        text = '{"a": 1}'
        result = strip_code_fences(text)
        assert result == '{"a": 1}'

    def test_extract_json_body_with_surrounding_text(self):
        """从前后多余文字中提取 JSON 主体。"""
        text = '好的，结果如下：\n{"a": 1, "b": 2}\n希望对您有帮助'
        result = extract_json_body(text)
        assert result == '{"a": 1, "b": 2}'

    def test_extract_json_body_with_fence(self):
        """从代码围栏中提取 JSON。"""
        text = '```json\n[1, 2, 3]\n```'
        result = extract_json_body(text)
        assert result == '[1, 2, 3]'

    def test_try_parse_json_success(self):
        """正常 JSON 解析成功。"""
        data, err = try_parse_json('{"a": 1}')
        assert data == {"a": 1}
        assert err is None

    def test_try_parse_json_failure(self):
        """无效 JSON 返回错误。"""
        data, err = try_parse_json('{invalid}')
        assert data is None
        assert err is not None

    def test_validate_output_format_json_valid(self):
        """合法 JSON 校验通过。"""
        ok, msg = validate_output_format('{"a": 1}', "json")
        assert ok is True
        assert msg == ""

    def test_validate_output_format_json_invalid(self):
        """非法 JSON 校验失败。"""
        ok, msg = validate_output_format('{invalid}', "json")
        assert ok is False
        assert "JSON" in msg

    def test_validate_output_format_mermaid_valid(self):
        """合法 Mermaid 校验通过。"""
        ok, msg = validate_output_format('flowchart TD\nA-->B', "mermaid")
        assert ok is True

    def test_validate_output_format_mermaid_invalid(self):
        """非法 Mermaid 校验失败。"""
        ok, msg = validate_output_format('random text', "mermaid")
        assert ok is False
        assert "Mermaid" in msg


class TestPromptMetrics:
    """提示词使用监控指标测试。"""

    def test_reset_metrics(self):
        """重置指标后为空。"""
        reset_prompt_metrics()
        metrics = get_prompt_metrics()
        assert metrics == {}

    def test_get_prompt_records_metrics(self):
        """调用 get_prompt 后记录指标。"""
        reset_prompt_metrics()
        # 调用一个已知存在的提示词
        _ = get_prompt("TENDER_SUMMARY_SYSTEM")
        metrics = get_prompt_metrics()
        assert "TENDER_SUMMARY_SYSTEM" in metrics
        entry = metrics["TENDER_SUMMARY_SYSTEM"]
        assert entry["calls"] >= 1
        assert entry["total_chars"] > 0
        assert entry["total_tokens"] > 0

    def test_metrics_thread_safe(self):
        """指标记录是线程安全的（多次调用不报错）。"""
        reset_prompt_metrics()
        for _ in range(5):
            _ = get_prompt("TENDER_SUMMARY_SYSTEM")
        metrics = get_prompt_metrics()
        assert metrics["TENDER_SUMMARY_SYSTEM"]["calls"] >= 5


class TestSanitizeIntegration:
    """安全清洗集成测试（验证各模块导入正常）。"""

    def test_sanitize_basic(self):
        """基本元标记中和功能正常。"""
        text = "正常内容 __GLOBAL_FACTS__ 中间"
        result = sanitize_prompt_value(text)
        assert "__GLOBAL_FACTS__" not in result
        assert "正常内容" in result

    def test_sanitize_json_safe(self):
        """JSON 对象不被误伤。"""
        text = '{"type": "flowchart", "data": [1, 2, 3]}'
        result = sanitize_prompt_value(text)
        # JSON 的花括号不应被中和
        assert "flowchart" in result
        assert "data" in result
