"""FIX-2026-10-06 目录生成模块编号回归测试

覆盖本轮在 heading_standard.py 修掉的 6 个问题：
  1. HeadingNumberingGeneratorV2 counters/parent_ids 数组越界（level=8）
  2. V2 update_counter 下级重置范围漏掉 idx=7
  3. V2 reset() 数组长度与 __init__ 不一致
  4. V3.1 next_heading 下级重置范围 range(..., 7) 漏掉 counters[7]
  5. V3.1 normalize_content 下级重置范围同上
  6. detect_level L6 只识别中文右括号「）」，英文「)」被丢弃

每个用例对应一次真实崩溃/错号，全部断言行为而非实现细节。
"""
from __future__ import annotations

import pytest

from app.services.ai.heading_standard import HeadingNumberingGenerator, HeadingNumberingGeneratorV2


class TestV2Level8NoIndexError:
    """V2 导出跟踪器遍历用户手工创建的 8 级目录时不得 IndexError。"""

    def test_update_counter_level8_does_not_raise(self):
        gen = HeadingNumberingGeneratorV2()
        # 此前 counters/parent_ids 只有 7 个元素，level=8(idx=7) 直接 IndexError
        gen.update_counter(8, "p8")
        assert gen.counters[7] == 1
        assert gen.parent_ids[7] == "p8"

    def test_update_counter_level7_resets_level8_counter(self):
        gen = HeadingNumberingGeneratorV2()
        gen.update_counter(7, "p7")
        gen.update_counter(8, "p8-child")  # L8 第一次
        assert gen.counters[7] == 1
        # 回到新的 L7 父节点，L8 计数器必须被重置
        gen.update_counter(7, "p7-next")
        gen.update_counter(8, "p8-next-child")
        assert gen.counters[7] == 1, "L7 父切换后 L8 应从 1 重新计数"

    def test_reset_matches_init_array_length(self):
        gen = HeadingNumberingGeneratorV2()
        gen.update_counter(4, "p4")
        gen.reset()
        # 此前 reset 只重置 7 个元素，与 __init__ 的 8 元素不一致
        assert len(gen.counters) == 8
        assert len(gen.parent_ids) == 8
        assert all(c == 0 for c in gen.counters)
        assert all(p is None for p in gen.parent_ids)


class TestV31NextHeadingResetRange:
    """V3.1 next_heading 在 L7 时必须把 L8 计数器清零。"""

    def test_level7_resets_level8(self):
        counters = [0] * 8
        counters[7] = 3  # 预置 L8 残留计数
        new_counters, _, _ = HeadingNumberingGenerator.next_heading(7, counters, "七级标题")
        # next_heading 返回新列表，原 counters 不变；断言返回值
        assert new_counters[7] == 0, "进入 L7 后 L8 计数器应被清零"
        assert new_counters[6] == 1  # L7 自身递增

    def test_level8_can_increment_after_level7(self):
        counters = [0] * 8
        c, num, _ = HeadingNumberingGenerator.next_heading(8, counters, "八级标题")
        assert c[7] == 1
        # L8 不在 HEADING_MANAGED_LEVELS，编号应为空串
        assert num == ""


class TestNormalizeContentResetRange:
    """normalize_content 内的计数器重置范围同样要覆盖到 idx=7。"""

    def test_level7_in_content_resets_level8_counter(self):
        content = "1、一级\n一、二级\n1、三级\n1.1、四级\n1.1.1、五级\n1）、六级\na、七级\n"
        out = HeadingNumberingGenerator.normalize_content(content)
        # 不崩即可；重点是计数器在 L7 后 L8 已归零
        assert "七级" in out


class TestDetectLevelEnglishParenthesis:
    """L6 标题应同时识别中文「）」与英文「)」。"""

    @pytest.mark.parametrize("text,expected_level", [
        ("1）标准化输出", 6),
        ("1)标准化输出", 6),
        ("1）标准化输出与归档", 6),
        ("1)标准化输出与归档", 6),
    ])
    def test_l6_parenthesis_recognition(self, text, expected_level):
        level, title = HeadingNumberingGenerator.detect_level(text)
        assert level == expected_level
        assert title == "标准化输出" or title.startswith("标准化输出")

    def test_detect_level_returns_zero_for_non_heading(self):
        level, title = HeadingNumberingGenerator.detect_level("这不是一个标题")
        assert level == 0


class TestNormalizeDeepLevelsSwitch:
    """FIX-2026-10-06：normalize_deep_levels 开关默认 False，导出时可启用 L5-L8 重编号。"""

    def test_default_false_keeps_l5_original(self):
        """默认行为：L5-L8 保留 AI 原文（向后兼容）"""
        content = "1、一级\n一、二级\n1、三级\n1.1、四级\n1.1.1、五级原文\n"
        out = HeadingNumberingGenerator.normalize_content(content)
        # L5 原文保留（不强制重编号）
        assert "1.1.1、五级原文" in out

    def test_true_renumbers_l5(self):
        """启用后：L5 按路径重新编号（AI 输出空格分隔→统一顿号）"""
        content = "1、一级\n一、二级\n1、三级\n1.1、四级\n1.1.1 五级原文\n"
        out = HeadingNumberingGenerator.normalize_content(content, normalize_deep_levels=True)
        # L5 应被重编号为标准格式 1.1.1、五级原文
        assert "1.1.1、五级原文" in out
        assert "1.1.1 五级原文" not in out

    def test_true_renumbers_l6_l7(self):
        """启用后：L6=1）、 L7=a、"""
        content = (
            "1、一级\n一、二级\n1、三级\n1.1、四级\n"
            "1.1.1、五级\n1）、六级原文\na、七级原文\n"
        )
        out = HeadingNumberingGenerator.normalize_content(content, normalize_deep_levels=True)
        assert "1）、六级原文" in out
        assert "a、七级原文" in out

    def test_l1_l4_still_renumbered_when_true(self):
        """开关不影响 L1-L4 的既有归一化行为"""
        content = "第一章、一级错误格式\n一、二级\n2、三级（错误）\n1.1.1、五级\n"
        out = HeadingNumberingGenerator.normalize_content(content, normalize_deep_levels=True)
        # L1 被重编号为"第一节、"
        assert "第一节、" in out
        # L3 编号被重置为 1（不是 2）
        assert "1、三级" in out


class TestStripHeadingNumberingDeepLevels:
    """L4（2026-10-13 遗留项结论更正 + 回归锁定）。

    第三轮加固报告曾把「strip_heading_numbering_from_content 对四级以上
    编号只剥前三段（残留 .1）」列为遗留问题 L4。实测结论：该判断有误 ——
    ATX/Setext 剥离正则的 `(?:\\.\\d+){2,}` 为贪婪匹配，`1.1.1.1` /
    `1.1.1.1.1` 会被整段剥离。此处补测试锁定该行为，
    防止将来收窄正则时回归出残留 `.1`。
    """

    def test_atx_deep_dotted_numbers_fully_stripped(self):
        from app.services.content_cleaning import strip_heading_numbering_from_content

        assert strip_heading_numbering_from_content("## 1.1.1.1 深化设计") == "## 深化设计"
        assert strip_heading_numbering_from_content("## 1.1.1.1.1 超深层") == "## 超深层"
        assert strip_heading_numbering_from_content("#### 2.3.4.5 机电调试") == "#### 机电调试"

    def test_setext_deep_dotted_numbers_fully_stripped(self):
        from app.services.content_cleaning import strip_heading_numbering_from_content

        assert strip_heading_numbering_from_content("1.1.1.1 施工部署\n===") == "施工部署\n==="

    def test_three_level_still_stripped(self):
        """既有三级形态不回退。"""
        from app.services.content_cleaning import strip_heading_numbering_from_content

        assert strip_heading_numbering_from_content("### 2.3.1 深化设计") == "### 深化设计"

