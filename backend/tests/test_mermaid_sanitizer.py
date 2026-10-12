"""MermaidSanitizer 单元测试。

BUG-2026-1008-01 修复（P0）配套测试：验证 Mermaid 语法预处理器的
四种核心能力（加引号 / 展开多连写 / 移除分号 / 规范化节点 ID）
以及幂等性、已合规代码无副作用等约束。

运行：cd backend && pytest tests/test_mermaid_sanitizer.py -v
"""

from app.services.ai.mermaid_sanitizer import (
    MermaidSanitizer,
    SANITIZER,
    sanitize_mermaid_code,
)


class TestAddQuotesToLabels:
    """Step 1：为含特殊字符/中文的节点标签添加双引号。"""

    def test_chinese_label_gets_quoted(self):
        """中文标签应加双引号。"""
        result = sanitize_mermaid_code("A[施工准备] --> B[主体结构]")
        assert 'A["施工准备"]' in result
        assert 'B["主体结构"]' in result

    def test_colon_in_label_gets_quoted(self):
        """含冒号的标签应加双引号（日期等场景）。"""
        result = sanitize_mermaid_code("A[开工: 2026-09-22] --> B[竣工: 2028-05-30]")
        assert 'A["开工: 2026-09-22"]' in result
        assert 'B["竣工: 2028-05-30"]' in result

    def test_comma_in_label_gets_quoted(self):
        """含逗号的标签应加双引号。"""
        result = sanitize_mermaid_code("A[混凝土,钢筋] --> B[模板,脚手架]")
        assert 'A["混凝土,钢筋"]' in result
        assert 'B["模板,脚手架"]' in result

    def test_percent_in_label_gets_quoted(self):
        """含百分号的标签应加双引号。"""
        result = sanitize_mermaid_code("A[完成率 95%] --> B[合格率 100%]")
        assert 'A["完成率 95%"]' in result
        assert 'B["合格率 100%"]' in result

    def test_slash_in_label_gets_quoted(self):
        """含斜杠的标签应加双引号。"""
        result = sanitize_mermaid_code("A[二次结构/抹灰] --> B[水电/暖通]")
        assert 'A["二次结构/抹灰"]' in result
        assert 'B["水电/暖通"]' in result

    def test_alphanumeric_label_unchanged(self):
        """纯 ASCII 无特殊字符的标签保持不变。"""
        result = sanitize_mermaid_code("A[Start] --> B[End]")
        assert "A[Start]" in result
        assert "B[End]" in result

    def test_already_quoted_label_unchanged(self):
        """已加双引号的标签保持不变。"""
        result = sanitize_mermaid_code('A["开工"] --> B["竣工"]')
        assert 'A["开工"]' in result
        assert 'B["竣工"]' in result

    def test_single_quoted_label_unchanged(self):
        """已加单引号的标签保持不变。"""
        result = sanitize_mermaid_code("A['label'] --> B['other']")
        assert "A['label']" in result
        assert "B['other']" in result
class TestExpandMultiConnections:
    """Step 2：展开 A --> B --> C 形式的多节点连写。"""

    def test_simple_three_node_chain(self):
        """三个节点的连写应拆成两行。"""
        result = sanitize_mermaid_code("A[Start] --> B[Mid] --> C[End]")
        lines = [l.strip() for l in result.strip().split("\n") if l.strip()]
        assert len(lines) == 2
        assert "A[Start] --> B[Mid]" in result
        assert "B[Mid] --> C[End]" in result

    def test_four_node_chain(self):
        """四个节点的连写应拆成三行。"""
        result = sanitize_mermaid_code("A --> B --> C --> D")
        lines = [l.strip() for l in result.strip().split("\n") if l.strip()]
        assert len(lines) == 3

    def test_single_arrow_unchanged(self):
        """单箭头不变。"""
        result = sanitize_mermaid_code("A[Start] --> B[End]")
        assert result == "A[Start] --> B[End]"

    def test_multiple_lines_with_chains(self):
        """多行中部分含连写，仅拆含连写的行。"""
        code = "A --> B\nC[Start] --> D[Mid] --> E[End]"
        result = sanitize_mermaid_code(code)
        assert "A --> B" in result
        assert "C[Start] --> D[Mid]" in result
        assert "D[Mid] --> E[End]" in result

    def test_flowchart_header_preserved(self):
        """flowchart 头部行不受影响。"""
        code = "flowchart TD\nA --> B --> C"
        result = sanitize_mermaid_code(code)
        assert result.startswith("flowchart TD")


class TestRemoveTrailingSemicolons:
    """Step 3：移除行末分号。"""

    def test_simple_trailing_semicolon(self):
        """行末分号应被移除。"""
        result = sanitize_mermaid_code("A[Start] --> B[End];")
        assert result == "A[Start] --> B[End]"

    def test_semicolon_with_trailing_space(self):
        """分号后的尾部空白也应被移除。"""
        result = sanitize_mermaid_code("A[Start] --> B[End];   ")
        assert result == "A[Start] --> B[End]"

    def test_semicolon_in_label_preserved(self):
        """标签内容中的分号不受影响。"""
        result = sanitize_mermaid_code('A["施工;验收"] --> B[End]')
        assert 'A["施工;验收"]' in result

    def test_no_semicolon_unchanged(self):
        """无分号的代码保持不变。"""
        code = "A[Start] --> B[End]"
        result = sanitize_mermaid_code(code)
        assert result == code


class TestNormalizeNodeIds:
    """Step 4：规范化非 ASCII 节点 ID。"""

    def test_chinese_node_id_replaced(self):
        """中文节点 ID 应被替换为 N1/N2/...。"""
        result = sanitize_mermaid_code("开工[2026-09-22] --> 出地面[2026-12-15]")
        assert "N1" in result
        assert "N2" in result

    def test_arrow_preserved_after_normalization(self):
        """ID 规范化后箭头应保留。"""
        result = sanitize_mermaid_code("开工[2026-09-22] --> 出地面[2026-12-15]")
        assert "-->" in result

    def test_ascii_node_id_unchanged(self):
        """ASCII 节点 ID 保持不变。"""
        result = sanitize_mermaid_code("A[Start] --> B[End]")
        assert "A[Start]" in result
        assert "B[End]" in result

    def test_label_content_not_polluted(self):
        """标签内容中的中文不应被误替换为 N1/N2。"""
        result = sanitize_mermaid_code('A["开工前检查"] --> B["开工后收尾"]')
        assert 'A["开工前检查"]' in result
        assert 'B["开工后收尾"]' in result
        assert "N1" not in result
        assert "N2" not in result

    def test_multiple_chinese_ids_sequential(self):
        """多个中文 ID 按出现顺序分配 N1、N2、N3...。"""
        result = sanitize_mermaid_code("节点1[准备] --> 节点2[施工] --> 节点3[验收]")
        assert "N1" in result
        assert "N2" in result
        assert "N3" in result

class TestEndToEndScenarios:
    """端到端场景：模拟 AI 生成的常见错误组合。"""

    def test_real_world_bug_scenario(self):
        """缺陷报告中的真实场景：中文标签 + 冒号 + 日期。"""
        code = 'flowchart TD\nA[开工: 2026-09-22] --> B[出地面: 2026-12-15]'
        result = sanitize_mermaid_code(code)
        assert 'A["开工: 2026-09-22"]' in result
        assert 'B["出地面: 2026-12-15"]' in result

    def test_mixed_errors_all_fixed(self):
        """多种错误同时存在，应全部修复。"""
        code = "A[开工: 2026-09-22] --> B[出地面: 2026-12-15] --> C[主体50%: 2027-04-30];"
        result = sanitize_mermaid_code(code)
        assert 'A["开工: 2026-09-22"]' in result
        assert 'B["出地面: 2026-12-15"]' in result
        assert 'C["主体50%: 2027-04-30"]' in result
        lines = [l.strip() for l in result.strip().split("\n") if l.strip()]
        assert len(lines) == 2
        assert not result.rstrip().endswith(";")

    def test_idempotent(self):
        """sanitize 两次结果应一致（幂等性）。"""
        code = 'flowchart TD\nA[开工: 2026-09-22] --> B[出地面: 2026-12-15]'
        once = sanitize_mermaid_code(code)
        twice = sanitize_mermaid_code(once)
        assert once == twice

    def test_already_valid_code_unchanged(self):
        """已合规代码应保持不变（无副作用）。"""
        valid_code = 'flowchart TD\nA["开工"] --> B["出地面"]\nB["出地面"] --> C["主体"]'
        result = sanitize_mermaid_code(valid_code)
        assert result == valid_code


class TestNeedsSanitization:
    """needs_sanitization 快速检测。"""

    def test_valid_code_returns_false(self):
        """合规代码返回 False。"""
        assert not MermaidSanitizer.needs_sanitization('A["Start"] --> B["End"]')

    def test_chinese_label_returns_true(self):
        """中文标签返回 True。"""
        assert MermaidSanitizer.needs_sanitization("A[开工] --> B[竣工]")

    def test_multi_arrow_returns_true(self):
        """多连写返回 True。"""
        assert MermaidSanitizer.needs_sanitization("A --> B --> C")

    def test_trailing_semicolon_returns_true(self):
        """行末分号返回 True。"""
        assert MermaidSanitizer.needs_sanitization("A --> B;")

    def test_chinese_node_id_returns_true(self):
        """中文节点 ID 返回 True。"""
        assert MermaidSanitizer.needs_sanitization("开工[标签] --> 竣工[标签]")

    def test_empty_string_returns_false(self):
        """空字符串返回 False。"""
        assert not MermaidSanitizer.needs_sanitization("")

    def test_none_returns_false(self):
        """None 返回 False。"""
        assert not MermaidSanitizer.needs_sanitization(None)


class TestEdgeCases:
    """边界情况测试。"""

    def test_empty_string(self):
        """空字符串原样返回。"""
        assert sanitize_mermaid_code("") == ""

    def test_none_input(self):
        """None 输入原样返回。"""
        assert sanitize_mermaid_code(None) is None

    def test_no_arrow_code(self):
        """无箭头的代码（仅节点定义）应正常处理。"""
        result = sanitize_mermaid_code("A[开工] \nB[竣工]")
        assert 'A["开工"]' in result
        assert 'B["竣工"]' in result

    def test_json_code_not_affected(self):
        """JSON 代码不应被处理（以 { 开头）。"""
        json_code = '{"type": "flowchart", "code": "flowchart TD"}'
        result = sanitize_mermaid_code(json_code)
        assert result == json_code

    def test_single_node_with_label(self):
        """单节点标签清洗。"""
        result = sanitize_mermaid_code("A[开工: 2026-09-22]")
        assert 'A["开工: 2026-09-22"]' in result


class TestModuleLevelAPI:
    """模块级 API 测试。"""

    def test_sanitizer_singleton(self):
        """SANITIZER 是模块级单例。"""
        assert isinstance(SANITIZER, MermaidSanitizer)

    def test_sanitize_function_uses_singleton(self):
        """sanitize_mermaid_code 使用全局单例。"""
        code = "A[开工] --> B[竣工]"
        result = sanitize_mermaid_code(code)
        expected = SANITIZER.sanitize(code)
        assert result == expected

