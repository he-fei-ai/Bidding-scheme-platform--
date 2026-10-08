"""BUG-2026-10-08 缺陷修复集成测试。

覆盖两个缺陷：
  - BUG-2026-1008-01（P0）：流程图渲染失败 → MermaidSanitizer 语法预处理
  - BUG-2026-1008-02（P1）：三级标题重复 → ContentCleaner 正文清洗

运行：cd backend && pytest tests/test_bugfix_2026_10_08_chart_and_heading.py -v
"""

from app.services.ai.mermaid_sanitizer import sanitize_mermaid_code, MermaidSanitizer
from app.services.content_cleaning import ContentCleaner
from app.services.chart_validators import check_duplicate_title_in_content


# ===========================================================================
# P1: ContentCleaner 正文清洗测试
# ===========================================================================


class TestContentCleanerDuplicateTitle:
    """测试移除正文开头的重复标题。"""

    def test_remove_exact_duplicate_title(self):
        """正文开头完全重复标题 → 移除。"""
        cleaner = ContentCleaner()
        result = cleaner.clean(
            "主体结构施工节奏管控",
            "主体结构施工节奏管控\n\n本项目主体结构施工采用流水作业...",
        )
        assert not result.startswith("主体结构施工节奏管控")
        assert "本项目主体结构施工" in result

    def test_remove_numbered_duplicate_title(self):
        """正文开头带编号的重复标题 → 移除。"""
        cleaner = ContentCleaner()
        result = cleaner.clean(
            "主体结构施工节奏管控",
            "2、主体结构施工节奏管控\n本项目主体结构施工采用流水作业...",
        )
        assert not result.startswith("2、")
        assert not result.startswith("主体结构施工节奏管控")
        assert "本项目主体结构施工" in result

    def test_remove_markdown_heading_duplicate(self):
        """正文开头 Markdown 标题标记的重复标题 → 移除。"""
        cleaner = ContentCleaner()
        result = cleaner.clean(
            "主体结构施工节奏管控",
            "### 主体结构施工节奏管控\n\n本项目主体结构施工采用流水作业...",
        )
        assert "###" not in result.split("\n")[0]
        assert "本项目主体结构施工" in result

    def test_no_duplicate_title_unchanged(self):
        """正文无重复标题 → 不变。"""
        cleaner = ContentCleaner()
        content = "本项目主体结构施工采用流水作业，按三层楼为一施工段组织施工。"
        result = cleaner.clean("主体结构施工节奏管控", content)
        assert result == content

    def test_empty_title_no_error(self):
        """空标题不报错。"""
        cleaner = ContentCleaner()
        content = "本项目主体结构施工..."
        result = cleaner.clean("", content)
        assert "本项目主体结构施工" in result

    def test_none_content_returns_none(self):
        """None 内容原样返回。"""
        cleaner = ContentCleaner()
        assert cleaner.clean("标题", None) is None
class TestContentCleanerMarkdownHeadings:
    """测试移除 Markdown 标题标记。"""

    def test_remove_h1(self):
        """移除 # 一级标题标记。"""
        cleaner = ContentCleaner()
        result = cleaner.clean("标题", "# 一级标题\n正文内容")
        assert not result.startswith("#")
        assert "一级标题" in result

    def test_remove_h3(self):
        """移除 ### 三级标题标记。"""
        cleaner = ContentCleaner()
        result = cleaner.clean("标题", "### 三级标题\n正文内容")
        assert not result.startswith("#")
        assert "三级标题" in result

    def test_remove_multiple_headings(self):
        """移除多行标题标记。"""
        cleaner = ContentCleaner()
        content = "# 一级\n## 二级\n### 三级\n正文内容"
        result = cleaner.clean("标题", content)
        assert "#" not in result.split("\n")[0]
        assert "#" not in result.split("\n")[1]
        assert "#" not in result.split("\n")[2]


class TestContentCleanerLeadingNumbering:
    """测试移除首行编号前缀。"""

    def test_remove_dunhao_numbering(self):
        """移除 '1、' 形式的编号。"""
        cleaner = ContentCleaner()
        result = cleaner.clean("标题", "1、本项目主体结构施工采用流水作业。")
        assert not result.startswith("1、")
        assert "本项目主体结构施工" in result

    def test_remove_parenthesized_numbering(self):
        """移除 '（1）' 形式的编号。"""
        cleaner = ContentCleaner()
        result = cleaner.clean("标题", "（1）本项目主体结构施工采用流水作业。")
        assert not result.startswith("（1）")
        assert "本项目主体结构施工" in result

    def test_remove_chinese_numbering(self):
        """移除中文数字编号。"""
        cleaner = ContentCleaner()
        result = cleaner.clean("标题", "一、本项目主体结构施工采用流水作业。")
        assert not result.startswith("一、")
        assert "本项目主体结构施工" in result

    def test_no_numbering_unchanged(self):
        """无编号内容不变。"""
        cleaner = ContentCleaner()
        content = "本项目主体结构施工采用流水作业。"
        result = cleaner.clean("标题", content)
        assert result == content


# ===========================================================================
# P1: 导出预检 CMP-07 测试
# ===========================================================================


class TestCheckDuplicateTitleInContent:
    """测试导出预检的重复标题检测。"""

    def test_detects_duplicate_title(self):
        """检测到重复标题 → 返回 CMP-07 issue。"""
        issues = check_duplicate_title_in_content(
            "主体结构施工节奏管控",
            "主体结构施工节奏管控\n\n本项目主体结构施工...",
        )
        assert len(issues) == 1
        assert issues[0]["rule_id"] == "CMP-07"
        assert "重复标题" in issues[0]["message"]

    def test_detects_numbered_duplicate(self):
        """检测带编号的重复标题。"""
        issues = check_duplicate_title_in_content(
            "主体结构施工节奏管控",
            "2、主体结构施工节奏管控\n本项目主体结构施工...",
        )
        assert len(issues) == 1
        assert issues[0]["rule_id"] == "CMP-07"

    def test_no_duplicate_returns_empty(self):
        """无重复标题 → 返回空列表。"""
        issues = check_duplicate_title_in_content(
            "主体结构施工节奏管控",
            "本项目主体结构施工采用流水作业...",
        )
        assert len(issues) == 0

    def test_empty_content_returns_empty(self):
        """空内容 → 返回空列表。"""
        issues = check_duplicate_title_in_content("标题", "")
        assert len(issues) == 0

    def test_none_content_returns_empty(self):
        """None 内容 → 返回空列表。"""
        issues = check_duplicate_title_in_content("标题", None)
        assert len(issues) == 0


# ===========================================================================
# P0: MermaidSanitizer 集成验证
# ===========================================================================


class TestMermaidSanitizerIntegration:
    """MermaidSanitizer 与渲染管道的集成验证。"""

    def test_sanitizer_reduces_rendering_failures(self):
        """验证 sanitizer 能修复导致渲染失败的常见语法错误。"""
        # 模拟 AI 生成的有问题的 Mermaid 代码（含中文标签+冒号+日期，未加引号）
        bad_code = (
            "flowchart TD\n"
            "A[开工: 2026-09-22] --> B[出地面: 2026-12-15]\n"
            "B[出地面: 2026-12-15] --> C[主体50%: 2027-04-30]\n"
        )
        assert MermaidSanitizer.needs_sanitization(bad_code)
        cleaned = sanitize_mermaid_code(bad_code)
        assert '"' in cleaned
        assert 'A["开工: 2026-09-22"]' in cleaned

    def test_sanitizer_preserves_valid_code(self):
        """已合规代码经 sanitizer 后不变。"""
        valid_code = 'flowchart TD\nA["开工"] --> B["竣工"]'
        cleaned = sanitize_mermaid_code(valid_code)
        assert cleaned == valid_code

    def test_sanitizer_is_idempotent(self):
        """sanitize 两次结果一致。"""
        code = 'A[开工: 2026-09-22] --> B[竣工: 2028-05-30]'
        once = sanitize_mermaid_code(code)
        twice = sanitize_mermaid_code(once)
        assert once == twice

