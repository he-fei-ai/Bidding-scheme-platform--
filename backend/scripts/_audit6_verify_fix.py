# -*- coding: utf-8 -*-
"""第六轮（图表围栏骨架单一权威化）修复验证脚本。

用法：python scripts/_audit6_verify_fix.py

覆盖：
  A. 下沉结果：scan_chart_fence_blocks / fence_marker 成为唯一权威实现
  B. 缺陷修复：跨字符/短闭合不再漏保护、属性串闭合行不再误判、闭合标记按
     开启标记字符与长度补齐、未闭合块就地闭合而非追加到 EOF
  C. 不变量：mask 块数 == plan[block] 块数、mask→restore 严格往返、detect
     与 plan 的坏块定位一致、sum(consumed) == len(lines)
"""
import os
import sys

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
os.chdir(BACKEND)
sys.dont_write_bytecode = True

import app.services.content_cleaning as cc
import app.services.ai.workflows_sections as ws

NL = "\n"
BT = "`"
TILDE = "~"
results = []


def rec(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(("  [PASS] " if ok else "  [FAIL] ") + name)
    if detail:
        print("           " + detail)


def plan_stats(text):
    """返回 (动作序列, block 块数, bad 图表块数)。"""
    plan = cc.plan_chart_fence_lines(text.split(NL))
    return (
        [(a, l) for a, l, b, c in plan],
        sum(1 for a, l, b, c in plan if a == "block"),
        sum(1 for a, l, b, c in plan if a == "bad" and l),
    )


def make_cases():
    mixed = NL.join(["前", "", "~~~mermaid", "graph TD", "  A-->B", "```", "", "后"])
    short = NL.join(["前", "", "````mermaid", "graph TD", "A-->B", "```", "", "后"])
    attrclose = NL.join(["前", "```mermaid", "graph TD", "```mermaid {json}", "后"])
    leaked = NL.join([
        "正文前。", "```mermaid", "graph TD", "  A --> B",
        "```mermaid", "flowchart TD", "  X --> Y", "```",
    ])
    return {
        "无围栏": "纯正文段落，无图表",
        "单图": NL.join(["前", "", "```mermaid", "graph TD", "  A-->B", "```", "", "后"]),
        "波浪线": NL.join(["前", "", "~~~mermaid", "graph TD", "  A-->B", "~~~", "", "后"]),
        "四反引号": NL.join(["前", "", "````mermaid", "graph TD", "A-->B", "````", "", "后"]),
        "两图": NL.join(["前", "```mermaid", "a", "```", "", "```chart-json", "{}", "```"]),
        "跨字符闭合": mixed,
        "短闭合": short,
        "属性串闭合": attrclose,
        "未闭合": "前\n\n```mermaid\ngraph TD\n  A--\n后记",
        "中漏闭合": leaked,
        "病态超长": NL.join(["```mermaid"] + ["  line %d" % i for i in range(5200)]),
        "多坏块": NL.join(["```mermaid", "a", "```mermaid", "b", "```",
                        "```chart-json", "{}"]),
    }


CASES = make_cases()
_actions = open(os.path.join(
    BACKEND, "app", "services", "ai", "workflows_sections.py"), encoding="utf-8").read()


print("=" * 78)
print("A. 下沉结果：唯一权威实现")
print("=" * 78)

_scan_src = _actions.split("def _scan_chart_fence_blocks", 1)[1].split(
    "def _mask_chart_fences_for_compression", 1)[0]
rec("A1 workflows_sections 不再自写围栏遍历骨架（_scan_chart_fence_blocks 仅转发）",
    "_classify_fence_line(" not in _actions
    and "return content_cleaning.scan_chart_fence_blocks(lines)" in _scan_src
    and "while " not in _scan_src,
    "残留 _classify_fence_line( 调用=%s；转发函数体仍含自写循环=%s"
    % ("_classify_fence_line(" in _actions, "while " in _scan_src))

rec("A2 _detect_unclosed_chart_fence 的手写双正则已消除",
    "_FENCE_OPEN_RE = re.compile" not in _actions
    and "_FENCE_CLOSE_RE = re.compile" not in _actions,
    "残留赋值 _FENCE_OPEN_RE=%s / _FENCE_CLOSE_RE=%s"
    % ("_FENCE_OPEN_RE = re.compile" in _actions,
       "_FENCE_CLOSE_RE = re.compile" in _actions))

_cases = ["```mermaid", "````mermaid", "~~~chart_json", "  ~~~mermaid",
          "    ```mermaid", "x```y", "graph TD", "```python", None, ""]
_exp = [("`", 3), ("`", 4), ("~", 3), ("~", 3), ("", 0), ("", 0),
        ("", 0), ("`", 3), ("", 0), ("", 0)]
_got = [cc.fence_marker(c) for c in _cases]
rec("A3 fence_marker 行为正确且与私有别名一致（10 例）",
    _got == _exp and [ws._fence_marker(c) for c in _cases] == _exp,
    "got=%s" % _got)

rec("A4 私有别名与公开实现返回同一结果",
    all(ws._scan_chart_fence_blocks(t.split(NL)) == cc.scan_chart_fence_blocks(t.split(NL))
        for t in CASES.values()))

rec("A5 健壮性：None 入参不抛异常", cc.scan_chart_fence_blocks(None) == [])
rec("A5 健壮性：None 元素不抛 AttributeError（修复前会抛）",
    cc.scan_chart_fence_blocks(["a", None, "```mermaid", "x", "```"]) == [(2, 4)])
rec("A5 健壮性：tuple 入参可用",
    cc.scan_chart_fence_blocks(("```mermaid", "x", "```")) == [(0, 2)])
rec("A5 健壮性：CRLF 行尾可识别",
    cc.scan_chart_fence_blocks(["```mermaid", "x\r", "```\r"]) == [(0, 2)])
rec("A5 健壮性：纯围栏标记开行（无语言）不计入",
    cc.scan_chart_fence_blocks(["```", "x", "```"]) == [])
rec("A5 健壮性：4 空格缩进围栏与解析侧口径一致（plan 用 strip() 判定 → 也计入）",
    cc.scan_chart_fence_blocks(["    ```mermaid", "x", "```"]) == [(0, 2)])

print()
print("=" * 78)
print("B. 缺陷修复")
print("=" * 78)

# B1 跨字符闭合：解析侧判有效 → 保护侧必须同样保护（修复前 mask 块数 = 0）
_m, _o = ws._mask_chart_fences_for_compression(CASES["跨字符闭合"])
rec("B1 跨字符闭合（~~~ 开 / ``` 闭）不再漏保护",
    len(_o) == 1 and "~~~mermaid" not in _m and "<<CHART_BLOCK_1>>" in _m,
    "mask块数=%d（修复前=0，plan 判为有效块=1）" % len(_o))

_m, _o = ws._mask_chart_fences_for_compression(CASES["短闭合"])
rec("B2 短闭合标记（```` 开 / ``` 闭）不再漏保护",
    len(_o) == 1 and _o[0].startswith("````mermaid"),
    "mask块数=%d（修复前=0）" % len(_o))

# B3 属性串闭合行不再被误判为未闭合（修复前会在文档末尾追加一行裸 ```）
_fixed, did = ws._force_close_unclosed_chart_fence(CASES["属性串闭合"])
rec("B3 闭合行带属性串（```mermaid {json}）不再误判为未闭合",
    did is False and _fixed == CASES["属性串闭合"],
    "did_close=%s（修复前=True）" % did)

# B4 闭合标记按开启标记的字符与长度补齐
for tag in ["~~~mermaid", "````mermaid", "~~~chart_json", "```mermaid"]:
    marker = tag.split("m")[0].split("c")[0]
    unc = NL.join(["前", "", tag, "graph TD", "  A --> B"])
    fixed, did = ws._force_close_unclosed_chart_fence(unc)
    rec("B4 未闭合 %s 按开启标记补齐（%s）" % (tag, marker),
        did is True and fixed.split(NL)[-2] == marker,
        "追加行=%r" % fixed.split(NL)[-2])

# B5 未闭合块在文档中部：就地闭合，不留游离标记
_fixed, did = ws._force_close_unclosed_chart_fence(CASES["中漏闭合"])
_plan_blocks = plan_stats(_fixed)[1]
_plan_bad = plan_stats(_fixed)[2]
rec("B5 文档中部未闭合块就地闭合（插入点在载荷末尾）",
    did is True and _plan_blocks == 2 and _plan_bad == 0,
    "plan[block]=%d plan[bad]=%d（修复前 block=1/bad=1）" % (_plan_blocks, _plan_bad))
rec("B5 误吞进围栏体的正文「正文前。」仍作为正文保留",
    "正文前。" in _fixed)
rec("B5 就地闭合后不残留游离裸围栏行（裸行数恰等于图表数 2）",
    len([ln for ln in _fixed.split(NL) if ln.strip()
         and set(ln.strip()) <= {BT, TILDE}]) == 2,
    "裸围栏行数=%d（两张图各 1 个闭合标记，无多余）"
    % len([ln for ln in _fixed.split(NL) if ln.strip()
           and set(ln.strip()) <= {BT, TILDE}]))

# B6 未闭合块直达 EOF（原始场景）：尾部形态与旧实现一致
_eof = "正文\n\n```mermaid\ngraph TD; A-->B;\n被截断"
_fixed, did = ws._force_close_unclosed_chart_fence(_eof)
rec("B6 截断在 EOF 时尾部形态与旧实现逐字节一致",
    did is True and _fixed == "正文\n\n```mermaid\ngraph TD; A-->B;\n被截断\n```\n",
    "result=%r" % _fixed)

_fixed, did = ws._force_close_unclosed_chart_fence("```mermaid")
rec("B7 只有开启行的空载荷块也能补齐闭合",
    did is True and _fixed == "```mermaid\n```\n", "result=%r" % _fixed)

_r = ws._detect_unclosed_chart_fence("前\n```chart_json\n{\"a\": 1")
rec("B8 chart_json 归一为 chart-json",
    _r is not None and _r["lang"] == "chart-json", "lang=%r" % (_r and _r["lang"]))

_rec = ws._detect_unclosed_chart_fence(CASES["中漏闭合"])
rec("B9 content_inside 只取载荷行（不再吞到文档最后一个非空行）",
    _rec is not None and _rec["content_inside"] == "graph TD\n  A --> B",
    "content_inside=%r" % (_rec and _rec["content_inside"]))

# B10 载荷恰好满 _HARD_CHART_FENCE_LINE_LIMIT 行的合法闭合围栏
_at = NL.join(["```mermaid"] + ["  line %d" % i
                  for i in range(cc._HARD_CHART_FENCE_LINE_LIMIT)] + ["```"])
_at_n, _at_bad = plan_stats(_at)[1], plan_stats(_at)[2]
rec("B10 满 %d 行载荷 + 合法闭合行 → 判为有效块" % cc._HARD_CHART_FENCE_LINE_LIMIT,
    _at_n == 1 and _at_bad == 0,
    "block=%d bad=%d（修复前 block=0/bad=1，整块图表被丢弃）" % (_at_n, _at_bad))

# B11 已闭合的**空**图表围栏不被重复闭合（plan 把空载荷一律归为 bad）
_fixed, _did = ws._force_close_unclosed_chart_fence("```mermaid\n```\n")
rec("B11 已闭合的空图表围栏不被追加多余标记",
    _did is False and _fixed == "```mermaid\n```\n",
    "did_close=%s result=%r（错误实现会把空载荷误判为未闭合、追加一行裸标记）"
    % (_did, _fixed))

# B12 被异语言围栏打断的未闭合块也能就地闭合（plan 的 KIND_OTHER 分支）
_intr = NL.join(["```mermaid", "graph TD", "```python", "def f(): pass"])
_fixed, _did = ws._force_close_unclosed_chart_fence(_intr)
_pl = plan_stats(_fixed)
rec("B12 被 ```python 打断的 mermaid 块就地闭合，图表载荷保留",
    _did is True and _pl[1] == 1 and _pl[2] == 0,
    "result=%r block=%d bad=%d" % (_fixed, _pl[1], _pl[2]))

print()
print("=" * 78)
print("C. 不变量（%d 个语料）" % len(CASES))
print("=" * 78)

# C1 mask 块数恒等于 plan 的 block 数（口径不再分叉）
_bad = []
for name, text in CASES.items():
    n_block = plan_stats(text)[1]
    got = len(ws._mask_chart_fences_for_compression(text)[1])
    if got != n_block:
        _bad.append("%s(mask=%d plan[block]=%d)" % (name, got, n_block))
rec("C1 mask 块数 == plan[block] 块数（零分叉）",
    not _bad, "; ".join(_bad) or "全部一致")

# C2 mask → restore 严格往返（含此前漏保护的场景）
_bad = []
for name, text in CASES.items():
    m, o = ws._mask_chart_fences_for_compression(text)
    if ws._restore_chart_fences_after_compression(m, o) != text:
        _bad.append(name)
rec("C2 mask → restore 严格字节往返", not _bad,
    "; ".join(_bad) or "全部往返一致")

# C3 detect 定位的未闭合块 == plan 的第一个 bad 块
_bad = []
for name, text in CASES.items():
    lines = text.split(NL)
    start, first_bad = 0, None
    for action, lang, payload, c in cc.plan_chart_fence_lines(lines):
        if action == "bad" and lang:
            first_bad = (start, len(payload), c)
            break
        start += c
    info = cc.find_unclosed_chart_fence(lines)
    d = ws._detect_unclosed_chart_fence(text)
    if (info is None) != (d is None):
        _bad.append("%s(命中不一致)" % name)
    elif info is not None:
        if (info["open_index"], info["payload_len"], info["consumed"]) != first_bad:
            _bad.append("%s(定位不一致)" % name)
        if d["close_insert_idx"] != info["close_insert_idx"]:
            _bad.append("%s(insert_idx 不一致)" % name)
rec("C3 detect 与 plan 的坏块定位逐例一致", not _bad,
    "; ".join(_bad) or "全部一致")

# C4 plan 条目连续无重叠（区间还原的前提）
_bad = [name for name, text in CASES.items()
        if sum(c for _a, _l, _p, c in cc.plan_chart_fence_lines(text.split(NL)))
        != len(text.split(NL))]
rec("C4 plan 条目连续无重叠：sum(consumed) == len(lines)",
    not _bad, "; ".join(_bad) or "全部满足")

# C5 兜底闭合幂等
_bad = []
for name, text in CASES.items():
    once, _d = ws._force_close_unclosed_chart_fence(text)
    twice, _d2 = ws._force_close_unclosed_chart_fence(once)
    if _d2 or twice != once:
        _bad.append(name)
rec("C5 _force_close 幂等：闭合后再调用不再改动", not _bad,
    "; ".join(_bad) or "全部幂等")

# C6 兜底闭合后无解析侧坏块残留（图表不再静默丢失）
_bad = []
_unfixable = []
for name, text in CASES.items():
    fixed, _d = ws._force_close_unclosed_chart_fence(text)
    n_bad = plan_stats(fixed)[2]
    if n_bad > 0:
        (_unfixable if not _d else _bad).append("%s(bad=%d)" % (name, n_bad))
rec("C6 可修复的未闭合块全部闭合（plan[bad] 归零）", not _bad,
    "; ".join(_bad) or "全部归零")
rec("C7 超行数硬上限的病态围栏按设计保持原样（不追加无效闭合行）",
    _unfixable == ["病态超长(bad=1)"],
    "保持原样的用例=%s" % _unfixable)

# C8 端到端：清洗后图表存活、源码不泄漏成正文
for tag in ["波浪线", "四反引号", "跨字符闭合"]:
    blocks = cc.parse_content(cc.clean_generated_content(CASES[tag]))
    types = [str(b.type) for b in blocks]
    prose = "".join(b.text for b in blocks if str(b.type) != "chart_marker")
    rec("C8 端到端 %s：产出 chart_marker 且源码不泄漏进正文" % tag,
        "chart_marker" in types and "graph TD" not in prose,
        "block 类型=%s 正文泄漏=%s" % (types, "graph TD" in prose))

print()
ok_n = sum(1 for _n, o in results if o)
print("=" * 78)
print("结果：%d/%d 通过" % (ok_n, len(results)))
print("=" * 78)
sys.exit(0 if ok_n == len(results) else 1)


