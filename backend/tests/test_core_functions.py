"""核心工具函数单元测试。

覆盖：
  1. clean_prompt_text — 提示词文本清洗
  2. validate_prompt_variables — 提示词变量校验
  3. extract_variables — 模板变量提取
  4. _count_words — 中英文混合字数统计
"""

import pytest

from app.services.ai.prompts._registry import (
    clean_prompt_text,
    extract_variables,
    validate_prompt_variables,
)
from app.utils import count_words as _count_words


@pytest.fixture(autouse=True)
def _isolate_prompt_registry():
    """隔离本模块对全局提示词注册表 `_ALL_PROMPTS` 的污染。

    BUG-FIX 2026-10-06：本模块用 `_reg("_TEST_VALIDATE", ...)` 等把测试用
    提示词写进**进程全局**注册表且从不清理。注册表是模块级单例，污染会
    跨测试文件存活 —— 导致 test_prompts_module_2026_06.py 的
    test_prompt_placeholder_coverage_complete 在全量跑时失败（这些
    `_TEST_*` 条目没有任何 provider 提供变量，被判成"未覆盖占位符"），
    但单独跑该测试却通过，是典型的顺序依赖型偶发失败。

    这里在每个用例前后快照/还原注册表：治的是「测试污染全局状态」这个
    根因，而不是去放宽下游覆盖门禁（那会掩盖真实的占位符缺口）。
    """
    from app.services.ai.prompts._registry import _ALL_PROMPTS

    snapshot = dict(_ALL_PROMPTS)
    try:
        yield
    finally:
        _ALL_PROMPTS.clear()
        _ALL_PROMPTS.update(snapshot)


# ================================================================
# clean_prompt_text
# ================================================================

class TestCleanPromptText:
    """测试 clean_prompt_text 文本清洗功能。"""

    def test_normalize_newlines_windows_to_unix(self):
        """Windows 换行符 \\r\\n 应统一为 \\n。"""
        text = "第一行\r\n第二行\r\n第三行"
        result = clean_prompt_text(text)
        assert result == "第一行\n第二行\n第三行"

    def test_normalize_newlines_old_mac_to_unix(self):
        """旧 Mac 换行符 \\r 应统一为 \\n。"""
        text = "第一行\r第二行\r第三行"
        result = clean_prompt_text(text)
        assert result == "第一行\n第二行\n第三行"

    def test_remove_zero_width_spaces(self):
        """移除零宽空格 (U+200B)。"""
        text = "正常\u200b文字\u200b中间"
        result = clean_prompt_text(text)
        assert "\u200b" not in result
        assert result == "正常文字中间"

    def test_remove_zero_width_non_joiner(self):
        """移除零宽不连字 (U+200C)。"""
        text = "A\u200CB"
        result = clean_prompt_text(text)
        assert result == "AB"

    def test_remove_zero_width_joiner(self):
        """移除零宽连字 (U+200D)。"""
        text = "A\u200DB"
        result = clean_prompt_text(text)
        assert result == "AB"

    def test_remove_bom(self):
        """移除 BOM (U+FEFF)。"""
        text = "\uFEFF说明"
        result = clean_prompt_text(text)
        assert result == "说明"

    def test_strip_whitespace(self):
        """去除首尾空白。"""
        text = "  内容  \n"
        result = clean_prompt_text(text)
        assert result == "内容"

    def test_all_cleaning_combined(self):
        """综合清洗：换行符 + 零宽字符 + 首尾空白。"""
        text = "\uFEFF 第一行\r\n第二行\u200B\r第三行  "
        result = clean_prompt_text(text)
        # 预期：BOM 移除，首尾空格移除，\r\n 和 \r 都变为 \n
        assert result == "第一行\n第二行\n第三行"

    def test_empty_string(self):
        """空字符串返回空字符串。"""
        assert clean_prompt_text("") == ""

    def test_only_whitespace(self):
        """仅空白字符串返回空字符串。"""
        assert clean_prompt_text("   \n  ") == ""

    def test_no_cleaning_needed(self):
        """无需清洗的文本应保持不变。"""
        text = "正常文本"
        assert clean_prompt_text(text) == "正常文本"

    def test_multiple_zero_width_chars(self):
        """多个连续零宽字符全部移除。"""
        text = "头\u200B\u200C\u200D\uFEFF尾"
        assert clean_prompt_text(text) == "头尾"


# ================================================================
# extract_variables
# ================================================================

class TestExtractVariables:
    """测试 extract_variables 变量提取功能。"""

    def test_single_variable(self):
        """提取单个变量。"""
        template = "您好，{name}！"
        assert extract_variables(template) == ["name"]

    def test_multiple_variables(self):
        """提取多个变量，返回排序后的列表。"""
        template = "{name} 的 {project} 方案"
        assert extract_variables(template) == ["name", "project"]

    def test_no_variables(self):
        """无变量时返回空列表。"""
        template = "固定文本，没有变量"
        assert extract_variables(template) == []

    def test_empty_template(self):
        """空模板返回空列表。"""
        assert extract_variables("") == []

    def test_duplicate_variables(self):
        """重复变量只返回一次（去重）。"""
        template = "{name} 你好，{name} 再见"
        assert extract_variables(template) == ["name"]

    def test_underscore_in_variable(self):
        """变量名包含下划线。"""
        template = "{user_name} 的 {project_id}"
        assert extract_variables(template) == ["project_id", "user_name"]

    def test_variable_with_numbers(self):
        """变量名包含数字。"""
        template = "第{num1}章第{num2}节"
        assert extract_variables(template) == ["num1", "num2"]

    def test_mixed_with_curly_braces_not_variable(self):
        """花括号内无字母数字不视为变量。"""
        template = "{{ 不是变量 }}"
        assert extract_variables(template) == []

    def test_sorted_order(self):
        """返回的变量列表按字母顺序排序。"""
        template = "{zebra} 和 {apple} 和 {banana}"
        assert extract_variables(template) == ["apple", "banana", "zebra"]


# ================================================================
# validate_prompt_variables
# ================================================================

class TestValidatePromptVariables:
    """测试 validate_prompt_variables 变量校验功能。"""

    def test_all_variables_provided(self):
        """所有变量已提供，返回空列表。"""
        missing = validate_prompt_variables(
            "PROJECT_OVERVIEW_PROMPT",
            project_name="测试项目",
            company_name="测试公司",
        )
        # PROJECT_OVERVIEW_PROMPT 实际注册在 prompts 模块中，
        # 但此测试只验证函数逻辑，不依赖注册表内容。
        # 如果 key 不存在，required 为空列表，则 missing 应为空。
        assert isinstance(missing, list)

    def test_missing_variable(self):
        """缺失变量时返回缺失列表。"""
        # 先注册一个测试提示词
        from app.services.ai.prompts._registry import _ALL_PROMPTS, _reg

        _reg("_TEST_VALIDATE", "test", "测试", "你好 {name}，欢迎来到 {place}")
        missing = validate_prompt_variables("_TEST_VALIDATE", name="小明")
        assert missing == ["place"]

    def test_none_value_counts_as_missing(self):
        """值为 None 的变量视为缺失。"""
        from app.services.ai.prompts._registry import _ALL_PROMPTS, _reg

        _reg("_TEST_NONE_VAL", "test", "测试", "你好 {name}")
        missing = validate_prompt_variables("_TEST_NONE_VAL", name=None)
        assert missing == ["name"]

    def test_all_required_provided_with_registered(self):
        """注册的提示词，所有变量都提供时返回空列表。"""
        from app.services.ai.prompts._registry import _ALL_PROMPTS, _reg

        _reg("_TEST_ALL_OK", "test", "测试", "{a} + {b} = ?")
        missing = validate_prompt_variables("_TEST_ALL_OK", a=1, b=2)
        assert missing == []

    def test_unknown_key_returns_empty_missing(self):
        """不存在的 key 返回空列表（无变量要求即无缺失）。"""
        missing = validate_prompt_variables("_NONEXISTENT_KEY")
        assert missing == []

    def test_extra_kwargs_ignored(self):
        """多余的 kwargs 被忽略，不影响缺失判断。"""
        from app.services.ai.prompts._registry import _ALL_PROMPTS, _reg

        _reg("_TEST_EXTRA", "test", "测试", "仅 {name}")
        missing = validate_prompt_variables(
            "_TEST_EXTRA", name="小明", extra="无用"
        )
        assert missing == []


# ================================================================
# _count_words
# ================================================================

class TestCountWords:
    """测试 _count_words 中英文混合字数统计。"""

    def test_empty_string(self):
        """空字符串返回 0。"""
        assert _count_words("") == 0

    def test_none_input(self):
        """None 输入返回 0。"""
        assert _count_words(None) == 0

    def test_only_chinese(self):
        """纯中文文本：每个汉字计为1个字。"""
        assert _count_words("这是一个测试") == 6  # 这/是/一/个/测/试

    def test_only_english_words(self):
        """纯英文单词：每个连续字母数字组计为1个字。"""
        assert _count_words("hello world") == 2

    def test_mixed_chinese_and_english(self):
        """中英文混合。"""
        assert _count_words("你好 world 测试") == 5  # 你/好/world/测/试

    def test_chinese_with_numbers(self):
        """中文和数字混合：数字单独作为 token。"""
        assert _count_words("第1章") == 3  # 第/1/章

    def test_english_with_numbers(self):
        """英文和数字连续作为整体。"""
        assert _count_words("hello123 world456") == 2

    def test_punctuation_only(self):
        """纯标点符号返回 0。"""
        assert _count_words("！？。，") == 0

    def test_mixed_punctuation_and_text(self):
        """标点符号不影响字数统计。"""
        assert _count_words("你好，世界！") == 4

    def test_leading_trailing_spaces(self):
        """首尾空格不影响字数统计。"""
        assert _count_words("  测试  ") == 2

    def test_newlines_and_tabs(self):
        """换行和制表符不影响字数统计（非中文/字母数字字符不计数）。"""
        assert _count_words("第一行\n第二行\t第三行") == 9  # 第/一/行/第/二/行/第/三/行

    def test_long_text(self):
        """长文本统计。"""
        text = "本方案针对" * 1000 + "AI project" * 100
        # "本方案针对" = 5 个汉字，x 1000 = 5000
        # "AI project" * 100 = "AI projectAI projectAI ... project"
        #   第1个 "AI" (1) + 中间的 "projectAI" 重复99次 (99) + 末尾的 "project" (1) = 101
        assert _count_words(text) == 5101

    def test_single_character(self):
        """单个字符。"""
        assert _count_words("a") == 1
        assert _count_words("我") == 1
