"""章节编写模块第五轮深度审计修复回归测试（2026-10-15）

覆盖：
  - P1-1 / P1-2：压缩前置保护（_mask_chart_fences_for_compression）只认 3 个反引号，
    漏保护波浪线围栏、并把 4+ 反引号围栏错位匹配。
  - P2-1：接缝去重（_strip_seam_overlap）对 1~5 字巧合误剥离，续写句子丢主语。
  - P2-2：零宽字符让标题/列表锚点正则失配，标题降级、列表结构错乱。

缺陷实证探针：backend/scripts/_audit5_recheck.py（修复前）
              backend/scripts/_audit5_verify_fix.py（修复后，18/18 通过）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# =====================================================================
# P1-1 / P1-2：图表围栏 mask 口径（原为 hand-rolled 3 反引号正则）
# =====================================================================
class TestMaskChartFenceVariants:
    """波浪线围栏 / 4+ 反引号围栏必须被 mask 保护且能严格往返。"""

    def test_tilde_mermaid_fence_is_masked(self):
        """~~~mermaid（波浪线围栏）原本不被 mask → 图表源码裸露给 AI 压缩"""
        from app.services.ai.workflows_sections import (
            _mask_chart_fences_for_compression,
            _restore_chart_fences_after_compression,
        )
        orig = "前言段落\n\n~~~mermaid\ngraph TD\n  A-->B\n~~~\n\n后记段落\n"
        masked, originals = _mask_chart_fences_for_compression(orig)
        assert len(originals) == 1
        assert "~~~mermaid" not in masked
        assert "<<CHART_BLOCK_1>>" in masked
        assert _restore_chart_fences_after_compression(masked, originals) == orig

    def test_tilde_chart_json_fence_is_masked(self):
        """~~~chart_json（波浪线 + 下划线变体）同样必须被保护"""
        from app.services.ai.workflows_sections import _mask_chart_fences_for_compression
        orig = "前\n\n~~~chart_json\n{\"type\":\"pie\"}\n~~~~\n\n后\n"
        _, originals = _mask_chart_fences_for_compression(orig)
        assert len(originals) == 1

    def test_quad_backtick_fence_matches_full_marker(self):
        """````mermaid（4 反引号）原本被从第 2 个反引号起错位匹配成 ```mermaid"""
        from app.services.ai.workflows_sections import (
            _mask_chart_fences_for_compression,
            _restore_chart_fences_after_compression,
        )
        orig = "前言\n\n````mermaid\ngraph TD\nA-->B\n````\n\n后记\n"
        masked, originals = _mask_chart_fences_for_compression(orig)
        assert len(originals) == 1
        # 保留完整的 4 反引号标记（旧实现只留 3 个）
        assert originals[0].startswith("````mermaid")
        # 占位符不得被多余的反引号包裹（旧实现产出 `<<CHART_BLOCK_1>>`）
        assert "`<<CHART_BLOCK_1>>`" not in masked
        assert _restore_chart_fences_after_compression(masked, originals) == orig

    def test_unclosed_fence_is_left_untouched(self):
        """未闭合围栏不参与 mask（交由 _force_close_unclosed_chart_fence 兜底）"""
        from app.services.ai.workflows_sections import _mask_chart_fences_for_compression
        content = "前\n\n```mermaid\ngraph TD\n  A--\n后记"
        masked, originals = _mask_chart_fences_for_compression(content)
        assert originals == []
        assert masked == content

    def test_mask_keeps_surrounding_text_byte_identical(self):
        """行区间替换保证块前后文本与空行逐字符不变"""
        from app.services.ai.workflows_sections import _mask_chart_fences_for_compression
        orig = ("# 标题\n正文段落\n\n```mermaid\ngraph TD;\n  A-->B;\n```\n\n"
                "```chart-json\n"
                '{"type":"pie","data":{"labels":["a","b"],"values":[1,2]}}\n'
                "```\n\n结尾")
        masked, originals = _mask_chart_fences_for_compression(orig)
        assert len(originals) == 2
        assert masked == "# 标题\n正文段落\n\n<<CHART_BLOCK_1>>\n\n<<CHART_BLOCK_2>>\n\n结尾"


# =====================================================================
# P2-1：接缝去重的最小重叠阈值
# =====================================================================
class TestSeamOverlapMinLength:
    def test_single_char_coincidence_not_stripped(self):
        """final 尾部与 extra 首字巧合 → 不再剥掉主语"""
        from app.services.ai.workflows_sections import _strip_seam_overlap
        assert _strip_seam_overlap("本阶段已完成采购", "采购的时间节点如下：") == \
            "采购的时间节点如下："

    def test_short_word_coincidence_not_stripped(self):
        """2~5 字巧合视为「新句首词与上一轮截断处巧合相同」，保守保留"""
        from app.services.ai.workflows_sections import _strip_seam_overlap
        assert _strip_seam_overlap("资源配置与", "与业主单位协商后落实") == \
            "与业主单位协商后落实"
        assert _strip_seam_overlap("总体质量目标为", "质量目标为可量化指标") == \
            "质量目标为可量化指标"

    def test_real_partial_overlap_still_stripped(self):
        """真实的部分重叠（>= 阈值）仍须剥离"""
        from app.services.ai.workflows_sections import _strip_seam_overlap
        out = _strip_seam_overlap(
            "第一段内容。第二段内容是重点。", "第二段内容是重点。第三段新内容。"
        )
        assert out == "第三段新内容。"

    def test_full_overlap_always_stripped(self):
        """完全重叠（extra 整体都在尾部窗口内）无条件剥离 —— 历史契约。

        即使重叠长度低于 SEAM_OVERLAP_MIN_CHARS 也必须生效：此时 AI 未写出
        任何新内容，保留只是把同一段话重复一遍。
        """
        from app.services.ai.workflows_sections import _strip_seam_overlap
        assert _strip_seam_overlap("第一段。第二段。", "第二段。") == ""

    def test_full_overlap_inside_tail_window(self):
        """尾部窗口内的完全重叠仍剥离 —— 历史契约"""
        from app.services.ai.workflows_sections import _strip_seam_overlap
        assert _strip_seam_overlap("X" * 500 + "唯一长句标记", "唯一长句标记") == ""

    def test_no_overlap_returns_as_is(self):
        from app.services.ai.workflows_sections import _strip_seam_overlap
        assert _strip_seam_overlap("旧正文结束。", "新内容开始。") == "新内容开始。"

    def test_empty_inputs(self):
        from app.services.ai.workflows_sections import _strip_seam_overlap
        assert _strip_seam_overlap("", "新内容") == "新内容"
        assert _strip_seam_overlap("旧正文", "") == ""

    def test_config_zero_restores_historical_behavior(self, monkeypatch):
        """SEAM_OVERLAP_MIN_CHARS=0 完全回退到历史行为（k=1 起逐字探测）"""
        import app.config as cfg
        import app.services.ai.workflows_sections as ws
        monkeypatch.setattr(cfg, "SEAM_OVERLAP_MIN_CHARS", 0)
        assert ws._strip_seam_overlap("本阶段已完成采购", "采购的时间节点如下：") == \
            "的时间节点如下："


# =====================================================================
# P2-2：零宽字符清理
# =====================================================================
class TestZeroWidthCleanup:
    ZW = "\u200b"

    def test_heading_not_downgraded(self):
        """「1\u200b、施工部署」原本被降级成段落（导出丢标题样式与编号）"""
        import app.services.content_cleaning as cc
        text = "1\u200b、施工部署\n正文内容第一段。\n"
        types = [str(b.type) for b in cc.parse_content(text)]
        assert types[0] == "heading3"

    def test_numbered_form_restored(self):
        """「1\u200b）施工准备」原本降级，现恢复为六级标题"""
        import app.services.content_cleaning as cc
        text = "1\u200b）施工准备\n内容。\n"
        assert [str(b.type) for b in cc.parse_content(text)][0] == "heading6"

    def test_paren_list_restored(self):
        """「（1\u200b）安全交底」原本降级，现恢复为有序列表"""
        import app.services.content_cleaning as cc
        text = "（1\u200b）安全交底\n内容。\n"
        assert [str(b.type) for b in cc.parse_content(text)][0] == "list_ordered"

    def test_zero_width_inside_fence_marker(self):
        """围栏语言标记内的零宽字符同样会被清理，图表不再漏识别"""
        import app.services.content_cleaning as cc
        text = "正文。\n\n```\u200bmermaid\ngraph TD\n```\n"
        assert "```mermaid" in cc.clean_generated_content(text)

    def test_zero_width_removed_from_body(self):
        import app.services.content_cleaning as cc
        assert "\u200b" not in cc.clean_generated_content("施工\u200b部署的\u200b要求")

    def test_variation_selector_preserved(self):
        """变体选择符 U+FE0F 影响 emoji 呈现，有意不清理"""
        import app.services.content_cleaning as cc
        assert cc._strip_zero_width_chars("x\ufe0f") == "x\ufe0f"

    def test_config_false_restores_historical_behavior(self, monkeypatch):
        """STRIP_ZERO_WIDTH_CHARS=false 完全回退到历史行为"""
        import app.config as cfg
        import app.services.content_cleaning as cc
        monkeypatch.setattr(cfg, "STRIP_ZERO_WIDTH_CHARS", False)
        src = "1\u200b、标题"
        assert cc._strip_zero_width_chars(src) == src
        assert [str(b.type) for b in cc.parse_content(src + "\n正文。")][0] == "paragraph"

    def test_empty_input(self):
        import app.services.content_cleaning as cc
        assert cc._strip_zero_width_chars("") == ""