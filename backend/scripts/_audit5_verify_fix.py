# -*- coding: utf-8 -*-
"""章节编写模块第五轮修复验证脚本。用法：python scripts/_audit5_verify_fix.py"""
import os, sys
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
os.chdir(BACKEND)
sys.dont_write_bytecode = True

import app.config as cfg
import app.services.content_cleaning as cc
import app.services.ai.workflows_sections as ws

NL, BT, TILDE, ZW = "\n", "`", "~", "\u200b"
results = []

def rec(name, ok, detail=""):
    results.append((name, ok))
    print(("[PASS] " if ok else "[FAIL] ") + name)
    if detail:
        print("       " + detail)

def ptypes(text):
    return [str(b.type) for b in cc.parse_content(text)]

# === FIX-A: 波浪线图表围栏现在被 mask ===
tilde = NL.join(["前言", "", TILDE*3 + "mermaid", "graph TD", "  A-->B", TILDE*3, "", "后记"])
m1, o1 = ws._mask_chart_fences_for_compression(tilde)
ok = (len(o1) == 1 and (TILDE*3 + "mermaid") not in m1 and "<<CHART_BLOCK_1>>" in m1)
rt = ws._restore_chart_fences_after_compression(m1, o1) == tilde
rec("FIX-A ~~~mermaid 围栏被保护且可严格往返", ok and rt,
    "originals=%d 裸露=%s 往返=%s" % (len(o1), (TILDE*3+"mermaid") in m1, rt))

# === FIX-B: 4 反引号围栏按完整标记匹配 ===
quad = NL.join(["前言", "", BT*4 + "mermaid", "graph TD", "A-->B", BT*4, "", "后记"])
m2, o2 = ws._mask_chart_fences_for_compression(quad)
ok = (len(o2) == 1 and o2[0].startswith(BT*4 + "mermaid")
      and m2.count(BT + "<<CHART_BLOCK_1>>" + BT) == 0)
rt2 = ws._restore_chart_fences_after_compression(m2, o2) == quad
rec("FIX-B 四反引号围栏完整匹配、占位符不被包裹", ok and rt2,
    "originals[0]前12字=%r 包裹数=%d 往返=%s"
    % (o2[0][:12] if o2 else None, m2.count(BT+"<<CHART_BLOCK_1>>"+BT), rt2))

# === FIX-C: 波浪线 chart-json 围栏 ===
tj = NL.join(["前", "", TILDE*4 + "chart_json", "{\"type\":\"pie\"}", TILDE*4, "", "后"])
_, oj = ws._mask_chart_fences_for_compression(tj)
rec("FIX-C 波浪线+下划线变体 chart_json 被保护", len(oj) == 1, "originals=%d" % len(oj))

# === FIX-D: 未闭合围栏不被 mask ===
unclosed = NL.join(["前", "", BT*3 + "mermaid", "graph TD", "  A--", "后记"])
_, ou = ws._mask_chart_fences_for_compression(unclosed)
rec("FIX-D 未闭合围栏保持原样（交由兜底闭合）", ou == [], "originals=%d" % len(ou))

# === FIX-E: 接缝去重最小重叠 ===
f1 = ws._strip_seam_overlap("本阶段已完成采购", "采购的时间节点如下：")
rec("FIX-E1 单字巧合不再误剥", f1 == "采购的时间节点如下：", "got=%r" % f1)
f2 = ws._strip_seam_overlap("总体质量目标为", "质量目标为可量化指标")
rec("FIX-E2 短词巧合不再误剥（保守保留）", f2 == "质量目标为可量化指标", "got=%r" % f2)
f3 = ws._strip_seam_overlap("第一段内容。第二段内容是重点。", "第二段内容是重点。第三段新内容。")
rec("FIX-E3 真实部分重叠仍剥离", f3 == "第三段新内容。", "got=%r" % f3)
f4 = ws._strip_seam_overlap("第一段。第二段。", "第二段。")
rec("FIX-E4 完全重叠仍无条件剥离(历史契约)", f4 == "", "got=%r" % f4)
f5 = ws._strip_seam_overlap("X"*500 + "唯一长句标记", "唯一长句标记")
rec("FIX-E5 尾部窗口内完全重叠仍剥离(历史契约)", f5 == "", "got=%r" % f5)
f6 = ws._strip_seam_overlap("旧正文结束。", "新内容开始。")
rec("FIX-E6 无重叠原样返回(历史契约)", f6 == "新内容开始。", "got=%r" % f6)
orig_cfg = getattr(cfg, "SEAM_OVERLAP_MIN_CHARS", None)
cfg.SEAM_OVERLAP_MIN_CHARS = 0
f7 = ws._strip_seam_overlap("本阶段已完成采购", "采购的时间节点如下：")
rec("FIX-E7 SEAM_OVERLAP_MIN_CHARS=0 回退历史行为", f7 == "的时间节点如下：", "got=%r" % f7)
cfg.SEAM_OVERLAP_MIN_CHARS = orig_cfg

# === FIX-F: 零宽字符清理（通过配置开关做 A/B 对照）===
zw_head = "1" + ZW + "、施工部署" + NL + "正文内容第一段。" + NL
# 注："1.施工准备" 在本模块本就定义为 heading3（LIST_PATTERN 要求数字后是 ) 或 ）），
# 故真正的列表形态用例是 "1）..." / "（1）..."。
zw_list = "1" + ZW + "）施工准备" + NL + "内容。" + NL
zw_paren = "（1" + ZW + "）安全交底" + NL + "内容。" + NL

_orig_zw = cfg.STRIP_ZERO_WIDTH_CHARS
cfg.STRIP_ZERO_WIDTH_CHARS = False
before_head = ptypes(zw_head)
cfg.STRIP_ZERO_WIDTH_CHARS = True
after_head = ptypes(zw_head)
cfg.STRIP_ZERO_WIDTH_CHARS = False
before_list = ptypes(zw_list)
before_paren = ptypes(zw_paren)
cfg.STRIP_ZERO_WIDTH_CHARS = True
after_list = ptypes(zw_list)
after_paren = ptypes(zw_paren)
cfg.STRIP_ZERO_WIDTH_CHARS = _orig_zw

rec("FIX-F1 标题不再被零宽字符降级", before_head[0] != "heading3" and after_head[0] == "heading3",
    "开关关=%r 开关开=%r" % (before_head, after_head))
_list_like = lambda ts: any(str(x).startswith("list") for x in ts)
# 注："1）" 在本模块属 HEADING_PATTERNS 六级（^\d+\s*[)）]），非列表。
rec("FIX-F2 编号形态（1））结构被正确恢复",
    before_list[0] != "heading6" and after_list[0] == "heading6",
    "开关关=%r 开关开=%r" % (before_list, after_list))
rec("FIX-F2b 括号编号列表（（1））不被零宽字符破坏",
    (not _list_like(before_paren)) and _list_like(after_paren),
    "开关关=%r 开关开=%r" % (before_paren, after_paren))

zw_chart = "正文。" + NL + NL + BT*3 + ZW + "mermaid" + NL + "graph TD" + NL + BT*3 + NL
rec("FIX-F3 围栏语言标记内的零宽字符被清理", "graph TD" in cc.clean_generated_content(zw_chart))

cfg.STRIP_ZERO_WIDTH_CHARS = False
rec("FIX-F4 STRIP_ZERO_WIDTH_CHARS=false 回退历史行为",
    cc._strip_zero_width_chars("1" + ZW + "、标题") == "1" + ZW + "、标题")
cfg.STRIP_ZERO_WIDTH_CHARS = _orig_zw
rec("FIX-F5 变体选择符 U+FE0F 不被清理", cc._strip_zero_width_chars("x\ufe0f") == "x\ufe0f")
rec("FIX-F6 零宽字符不影响正常中文正文", "施工部署" in cc.clean_generated_content(zw_head))
print("=" * 74)
passed = sum(1 for _n, o in results if o)
print("结论：%d 项验证，通过 %d 项，失败 %d 项" % (len(results), passed, len(results) - passed))
for n, o in results:
    if not o:
        print("  [FAIL] " + n)
print("=" * 74)