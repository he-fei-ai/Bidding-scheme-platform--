# -*- coding: utf-8 -*-
"""第六轮回归测试：图表围栏「行区间扫描」下沉为 content_cleaning 唯一权威实现。

背景：此前「找出正文里图表围栏块在源行列表上的区间」有两份独立遍历骨架——
content_cleaning.plan_chart_fence_lines（解析/检测共用）与
workflows_sections._scan_chart_fence_blocks（压缩前置 mask 专用）；
另有第三份骨架 _detect_unclosed_chart_fence 自写两个手写正则。
三份口径各自漂移，实测产生漏保护、误判、游离标记三类缺陷。

本轮把扫描器下沉到 content_cleaning（scan_chart_fence_blocks /
find_unclosed_chart_fence / fence_marker），workflows_sections 仅保留转发别名。

配套取证/验证脚本：backend/scripts/_audit6_verify_fix.py（37/37 通过）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from app.services import content_cleaning as cc
from app.services.ai import workflows_sections as ws


def _plan_stats(text):
    """返回 (block 块数, 有语言的 bad 块数)。"""
    plan = cc.plan_chart_fence_lines(text.split("\n"))
    return (sum(1 for a, l, b, c in plan if a == "block"),
            sum(1 for a, l, b, c in plan if a == "bad" and l))


# =====================================================================
# 1. 下沉结果：唯一权威实现 + 向后兼容别名
# =====================================================================
class TestFenceMarkerSinked:
    """fence_marker 下沉后行为正确，且私有别名仍可用。"""

    @pytest.mark.parametrize(
        ("line", "expected"),
        [("```mermaid", ("`", 3)), ("````mermaid", ("`", 4)),
         ("~~~chart_json", ("~", 3)), ("  ~~~mermaid", ("~", 3)),
         ("    ```mermaid", ("", 0)), ("x```y", ("", 0)),
         ("graph TD", ("", 0)), ("```python", ("`", 3)),
         (None, ("", 0)), ("", ("", 0))],
    )
    def test_marker_shape(self, line, expected):
        assert cc.fence_marker(line) == expected
        assert ws._fence_marker(line) == expected  # 私有别名逐例一致

    def test_head_pattern_public(self):
        assert cc.FENCE_HEAD_PATTERN is ws._FENCE_HEAD_RE


class TestScanChartFenceBlocks:
    """scan_chart_fence_blocks：区间扫描唯一入口。"""

    @pytest.mark.parametrize(
        "text",
        ["前\n\n~~~mermaid\ngraph TD\n  A-->B\n~~~\n\n后",
         "前\n\n````mermaid\ngraph TD\nA-->B\n````\n\n后",
         "前\n```mermaid\na\n```\n\n```chart-json\n{}\n```"],
    )
    def test_block_count_equals_plan(self, text):
        assert len(cc.scan_chart_fence_blocks(text.split("\n"))) == _plan_stats(text)[0]

    def test_cross_char_close_is_now_protected(self):
        """跨字符闭合（~~~ 开 / ``` 闭）：解析侧判有效 → 保护侧必须同样保护。

        修复前扫描器额外要求「同字符且长度 >= 开启标记」→ 判为未闭合 → 不被 mask
        → 原始 mermaid 源码裸露给压缩 AI。
        """
        text = "前\n\n~~~mermaid\ngraph TD\n  A-->B\n```\n\n后"
        masked, originals = ws._mask_chart_fences_for_compression(text)
        assert len(originals) == 1
        assert "~~~mermaid" not in masked
        assert "<<CHART_BLOCK_1>>" in masked
        assert (ws._restore_chart_fences_after_compression(masked, originals) == text)

    def test_short_close_marker_is_now_protected(self):
        text = "前\n\n````mermaid\ngraph TD\nA-->B\n```\n\n后"
        _, originals = ws._mask_chart_fences_for_compression(text)
        assert len(originals) == 1
        assert originals[0].startswith("````mermaid")

    def test_unclosed_fence_not_masked(self):
        text = "前\n\n```mermaid\ngraph TD\n  A--\n后记"
        masked, originals = ws._mask_chart_fences_for_compression(text)
        assert originals == []
        assert masked == text

    @pytest.mark.parametrize(
        "lines",
        [None, [], ("```mermaid", "x", "```"),
         ["a", None, "```mermaid", "x", "```"],
         ["```mermaid", "x\r", "```\r"],
         ["```", "x", "```"],
         ["    ```mermaid", "x", "```"]],
    )
    def test_input_robustness(self, lines):
        """不抛异常即可——修复前 None 元素会抛 AttributeError。"""
        assert isinstance(cc.scan_chart_fence_blocks(lines), list)

    def test_private_alias_matches_public(self):
        text = "前\n\n~~~mermaid\ngraph TD\n~~~\n后"
        assert (ws._scan_chart_fence_blocks(text.split("\n"))
                == cc.scan_chart_fence_blocks(text.split("\n")))


# =====================================================================
# 2. 未闭合围栏定位：与解析侧 plan 同源
# =====================================================================
class TestFindUnclosedChartFence:
    def test_matches_plan_first_bad_entry(self):
        text = "前\n\n```mermaid\ngraph TD\n  A--\n后记"
        lines = text.split("\n")
        info = cc.find_unclosed_chart_fence(lines)
        start, first_bad = 0, None
        for action, lang, payload, c in cc.plan_chart_fence_lines(lines):
            if action == "bad" and lang:
                first_bad = (start, len(payload), c)
                break
            start += c
        assert info is not None
        assert (info["open_index"], info["payload_len"], info["consumed"]) == first_bad
        assert info["close_insert_idx"] == info["open_index"] + 1 + info["payload_len"]

    def test_attr_string_close_line_no_longer_false_positive(self):
        """闭合行带属性串（```mermaid {json}）——BUG-FIX 2026-10-09 支持的写法。

        修复前 _FENCE_CLOSE_RE 匹配不到 {json} → 判为未闭合 → 兜底闭合在文档
        末尾追加一行游离的裸围栏标记。
        """
        text = "前\n```mermaid\ngraph TD\n```mermaid {json}\n后"
        assert cc.find_unclosed_chart_fence(text.split("\n")) is None
        fixed, did = ws._force_close_unclosed_chart_fence(text)
        assert did is False
        assert fixed == text

    def test_closed_empty_fence_not_reported(self):
        """已闭合的**空**图表围栏不是未闭合块（plan 把空载荷一律归为 bad）。"""
        assert cc.find_unclosed_chart_fence(["```mermaid", "```", ""]) is None

    def test_truncated_lone_open_line_reported(self):
        info = cc.find_unclosed_chart_fence(["```mermaid"])
        assert info is not None
        assert info["open_index"] == 0
        assert info["close_insert_idx"] == 1

    @pytest.mark.parametrize(
        ("open_line", "marker"),
        [("~~~mermaid", "~~~"), ("````mermaid", "````"), ("```mermaid", "```")],
    )
    def test_marker_char_and_length(self, open_line, marker):
        info = cc.find_unclosed_chart_fence([open_line, "graph TD"])
        assert info["marker"] == marker
        assert info["marker_len"] == len(marker)

    def test_lang_normalized(self):
        info = cc.find_unclosed_chart_fence(["前", "```chart_json", '{"a": 1'])
        assert info["lang"] == "chart-json"

    def test_detect_returns_expected_shape(self):
        d = ws._detect_unclosed_chart_fence("前\n```mermaid\ngraph TD\n被截断")
        assert d["lang"] == "mermaid"
        assert d["open_line_no"] == 2
        assert d["open_marker"] == "```"
        assert d["content_inside"] == "graph TD\n被截断"
        assert d["content_before_open"] == "前"

    def test_detect_payload_only_not_whole_doc_tail(self):
        """content_inside 只取载荷行，不再吞到「文档最后一个非空行」。"""
        text = ("正文前。\n```mermaid\ngraph TD\n  A --> B\n"
                "```mermaid\nflowchart TD\n```")
        d = ws._detect_unclosed_chart_fence(text)
        assert d is not None
        assert d["content_inside"] == "graph TD\n  A --> B"


# =====================================================================
# 3. 兜底闭合：标记匹配 + 就地插入 + 幂等
# =====================================================================
class TestForceCloseUnclosedChartFence:
    @pytest.mark.parametrize(
        ("open_line", "marker"),
        [("~~~mermaid", "~~~"), ("````mermaid", "````"),
         ("~~~chart_json", "~~~"), ("```mermaid", "```")],
    )
    def test_close_marker_matches_open(self, open_line, marker):
        unc = "前\n\n%s\ngraph TD\n  A --> B" % open_line
        fixed, did = ws._force_close_unclosed_chart_fence(unc)
        assert did is True
        assert fixed.split("\n")[-2] == marker

    def test_eof_truncation_byte_identical_to_old_behavior(self):
        unc = "正文\n\n```mermaid\ngraph TD; A-->B;\n被截断"
        fixed, did = ws._force_close_unclosed_chart_fence(unc)
        assert did is True
        assert fixed == "正文\n\n```mermaid\ngraph TD; A-->B;\n被截断\n```\n"

    def test_mid_doc_unclosed_block_closed_in_place(self):
        """未闭合块在文档中部：闭合行插在载荷末尾，而非追加到 EOF。"""
        text = ("正文前。\n```mermaid\ngraph TD\n  A --> B\n"
                "```mermaid\nflowchart TD\n  X --> Y\n```")
        fixed, did = ws._force_close_unclosed_chart_fence(text)
        assert did is True
        assert _plan_stats(fixed) == (2, 0)
        assert "正文前。" in fixed
        bare = [ln for ln in fixed.split("\n")
                if ln.strip() and set(ln.strip()) <= {"`", "~"}]
        assert len(bare) == 2  # 裸围栏行恰等于图表数，无多余标记

    def test_interruption_by_other_lang_fence(self):
        text = "```mermaid\ngraph TD\n```python\ndef f(): pass"
        fixed, did = ws._force_close_unclosed_chart_fence(text)
        assert did is True
        assert _plan_stats(fixed)[0] == 1

    @pytest.mark.parametrize(
        "text",
        ["", "纯正文，无围栏", "```mermaid\n```\n",
         "正文\n\n```mermaid\ngraph TD; A-->B;\n```\n"],
    )
    def test_noop_cases(self, text):
        result, closed = ws._force_close_unclosed_chart_fence(text)
        assert closed is False
        assert result == text

    @pytest.mark.parametrize(
        "text",
        ["正文\n\n```mermaid\ngraph TD; A-->B;\n被截断",
         "正文前。\n```mermaid\ngraph TD\n  A --> B\n```mermaid\nflowchart TD\n```",
         "```mermaid",
         "\n".join(["```mermaid"] + ["  line %d" % i for i in range(6000)])],
        ids=["eof 截断", "文档中部两图", "只有开启行", "病态超长 6000 行"],
    )
    def test_idempotent(self, text):
        """闭合后再调用不得再改动文档（修复前病态超长用例每次 +4 字节）。"""
        once, _d1 = ws._force_close_unclosed_chart_fence(text)
        twice, d2 = ws._force_close_unclosed_chart_fence(once)
        assert d2 is False
        assert twice == once

    def test_pathological_overlong_fence_left_untouched(self):
        """载荷行数超硬上限的病态围栏：闭合无法生效 → 原样返回，不追加无效标记。"""
        text = "\n".join(["```mermaid"] + ["  line %d" % i for i in range(6000)])
        fixed, did = ws._force_close_unclosed_chart_fence(text)
        assert did is False
        assert fixed == text


# =====================================================================
# 4. 规划器载荷行数上限 off-by-one
# =====================================================================
class TestPlanChartFenceLinesLimit:
    def test_payload_exactly_at_limit_is_a_valid_block(self):
        """载荷恰好 _HARD_CHART_FENCE_LINE_LIMIT 行 + 合法闭合行 → 判为有效块。

        修复前判据 `j - i > LIMIT` 在追加+步进后求值，等价于 `len(body) >= LIMIT`
        即中止，早停一步看不到紧随其后的闭合标记 → 整块被误判为 bad 丢弃。
        """
        limit = cc._HARD_CHART_FENCE_LINE_LIMIT
        text = "\n".join(["```mermaid"]
                         + ["  line %d" % i for i in range(limit)]
                         + ["```"])
        assert _plan_stats(text) == (1, 0)

    @pytest.mark.parametrize("payload_n", [1, 10, 500, 4999, 5000])
    def test_consumed_covers_region(self, payload_n):
        """条目连续无重叠：sum(consumed) == len(lines)。"""
        lines = (["头部正文", "```mermaid"]
                 + ["  line %d" % i for i in range(payload_n)]
                 + ["```", "尾部正文"])
        assert sum(c for _a, _l, _p, c
                   in cc.plan_chart_fence_lines(lines)) == len(lines)


# =====================================================================
# 5. mask 与解析侧口径不再分叉 + 端到端
# =====================================================================
class TestMaskPlanAlignment:
    CASES = {
        "无围栏": "纯正文段落，无图表",
        "单图": "前\n\n```mermaid\ngraph TD\n  A-->B\n```\n\n后",
        "波浪线": "前\n\n~~~mermaid\ngraph TD\n  A-->B\n~~~\n\n后",
        "四反引号": "前\n\n````mermaid\ngraph TD\nA-->B\n````\n\n后",
        "双图": "前\n```mermaid\na\n```\n\n```chart-json\n{}\n```",
        "跨字符闭合": "前\n\n~~~mermaid\ngraph TD\n  A-->B\n```\n\n后",
        "短闭合": "前\n\n````mermaid\ngraph TD\nA-->B\n```\n\n后",
        "属性串闭合": "前\n```mermaid\ngraph TD\n```mermaid {json}\n后",
        "未闭合": "前\n\n```mermaid\ngraph TD\n  A--\n后记",
        "多坏块": "\n".join(["```mermaid", "a", "```mermaid", "b", "```",
                            "```chart-json", "{}"]),
    }

    @pytest.mark.parametrize("name", sorted(CASES))
    def test_mask_count_equals_plan_block_count(self, name):
        """核心不变量：mask 块数恒等于 plan 的 block 块数（口径不再分叉）。"""
        text = self.CASES[name]
        masked_n = len(ws._mask_chart_fences_for_compression(text)[1])
        assert masked_n == _plan_stats(text)[0]

    @pytest.mark.parametrize("name", sorted(CASES))
    def test_mask_restore_roundtrip(self, name):
        text = self.CASES[name]
        masked, originals = ws._mask_chart_fences_for_compression(text)
        assert ws._restore_chart_fences_after_compression(masked, originals) == text

    @pytest.mark.parametrize(
        "text",
        ["前\n\n~~~mermaid\ngraph TD\n  A-->B\n~~~\n\n后",
         "前\n\n````mermaid\ngraph TD\nA-->B\n````\n\n后",
         "前\n\n~~~mermaid\ngraph TD\n  A-->B\n```\n\n后"],
    )
    def test_end_to_end_chart_survives_cleaning(self, text):
        blocks = cc.parse_content(cc.clean_generated_content(text))
        types = [str(b.type) for b in blocks]
        assert "chart_marker" in types
        prose = "".join(b.text for b in blocks if str(b.type) != "chart_marker")
        assert "graph TD" not in prose  # 源码不泄漏成正文段落
