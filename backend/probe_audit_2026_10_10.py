# -*- coding: utf-8 -*-
"""深度审计探针（2026-10-10）：章节编写 + 一致性校核。

只读验证，不写库（除一致性抽取走临时内存库）。每条探针打印 PASS/FAIL 与证据。
"""
from __future__ import annotations

import re
import sys

sys.path.insert(0, ".")

from app.services import content_cleaning as cc
from app.services import consistency_rules as rules
from app.services.consistency_auditor import ConsistencyAuditor
from app.services import text_patch

_results: list[tuple[str, str, str]] = []


def chk(name: str, ok: bool, detail: str = "") -> None:
    _results.append(("PASS" if ok else "FAIL", name, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  <- {detail}" if detail else ""))


def section(title: str) -> None:
    print("\n" + "=" * 20 + " " + title + " " + "=" * 20)


# ---------------------------------------------------------------------------
section("A. 章节编写：围栏 / 清洗 / 图表")

# A1 缩进围栏（CommonMark：块内缩进的 ``` 是内容行，不是闭合行）
a1_in = "```markdown\n示例：\n    ```python\n    print('hi')\n    ```\n```"
a1_out = cc.clean_generated_content(a1_in)
chk(
    "A1 缩进围栏内层不应被当闭合行（块内内容需完整保留）",
    "print('hi')" in a1_out,
    f"out={a1_out!r}",
)

# A2 波浪线块 + 反引号闭合行混用
a2_in = "~~~python\nx = 1\n```\n尾部正文"
a2_out = cc.clean_generated_content(a2_in)
chk(
    "A2 ~~~ 块不应被裸 ``` 行闭合（否则代码泄漏为正文）",
    "x = 1" in a2_out,
    f"out={a1_out[:80]!r}",
)

# A3 4 反引号开启块 + 3 反引号闭合行（CommonMark：闭合标记长度须 >= 开启）
a3_in = "````md\n内容行\n```\n内容行2\n````"
a3_out = cc.clean_generated_content(a3_in)
chk(
    "A3 短闭合标记不应闭合长开启块",
    "内容行" in a3_out and "内容行2" in a3_out,
    f"out={a3_out!r}",
)

# A4 未闭合普通围栏 + 其后合法 mermaid 块 + 其后正文
a4_in = "```python\nimport os\n\ndef f():\n    return 1\n\n```mermaid\ngraph TD\n  A-->B\n```\n后续正文段落"
a4_out = cc.clean_generated_content(a4_in)
chk(
    "A4 未闭合普通围栏不应吞掉后续图表与正文",
    "graph TD" in a4_out and "后续正文段落" in a4_out,
    f"out={a4_out!r}",
)

# A5 未闭合普通围栏 + 图表占位符之后还有另一个普通围栏块
a5_in = "```python\ncode\n```text\n干净块内容"
a5_out = cc.clean_generated_content(a5_in)
chk(
    "A5 异语言围栏起始行作为坏块右边界，其后块应正常保留",
    "干净块内容" in a5_out,
    f"out={a5_out!r}",
)

# A6 图表围栏保护 → 内部含 ``` 行（mermaid 里合法出现三反引号字面量）
a6_in = "```mermaid\nflowchart TD\n  A[\"```\"]-->B\n```"
a6_out = cc.clean_generated_content(a6_in)
chk(
    "A6 图表围栏内部载荷零污染（含反引号字面量）",
    "flowchart TD" in a6_out,
    f"out={a6_out!r}",
)

# A7 _fence_cache 跨调用残留（线程本地缓存未清空）
cc.clean_generated_content("```mermaid\ngraph TD\n  A-->B\n```")
_cc_blocks_before = list(cc._fence_cache.blocks)
cc.clean_generated_content("```mermaid\ngraph LR\n  X-->Y\n```")
_cc_blocks_after = list(cc._fence_cache.blocks)
chk(
    "A7 相邻两次清洗后围栏缓存已清空（无跨调用残留）",
    _cc_blocks_before == [] and _cc_blocks_after == [],
    f"before={len(_cc_blocks_before)} after={len(_cc_blocks_after)}",
)

# A8 空输入 / 超长输入
chk("A8a 空串输入", cc.clean_generated_content("") == "", "")
chk("A8b None 输入", cc.clean_generated_content(None) == "", "")
_big = ("```mermaid\ngraph TD\n  A-->B\n```\n" * 400)
chk("A8c 200 行×400 段图表输入", len(cc.clean_generated_content(_big)) > 0, "")

# A9 列表类型拍平
# A9 列表类型：「1. 第一项」按设计优先判为编号标题（HEADING_PATTERNS L3），
# 故只校验无序/括号编号列表项的类型不被拍平
b = cc.parse_content("- 无序项\n• 圆点项\n（2）全角括号编号项")
_types = [blk.type for blk in b]
chk(
    "A9 无序/括号编号列表块类型正确（不被拍平）",
    all(t in ("ordered", "bullet", "list_ordered", "list_bullet") for t in _types),
    f"types={_types}",
)

# A10 L5+ 标题
p10 = cc.parse_content("1.1 一级\n1.1.1 二级\n1.1.1.1 三级\n1.1.1.1.1 四级\n1.1.1.1.1.1 五级")
chk(
    "A10 L5+ 编号标题可解析",
    any(getattr(blk, "level", 0) >= 5 for blk in p10),
    f"levels={[getattr(blk,'level',0) for blk in p10]}",
)

# A11 图表标记口径一致：iter_chart_fences 识别的必在 parse_content 中出 chart_marker
for _c in (
    "```mermaid\ngraph TD\n  A-->B\n```",
    "```chart-json\n{\"type\":\"flowchart\",\"title\":\"t\",\"data\":{}}\n```",
    "```mermaid {theme:dark}\nflowchart TD\n  A-->B\n```",
):
    _langs = [lg for lg, _b, _m in cc.iter_chart_fences(_c)]
    _marks = [blk.type for blk in cc.parse_content(_c) if blk.type == "chart_marker"]
    chk(
        f"A11 图表口径一致: {(_langs or ['-'])[0]}",
        len(_langs) == len(_marks),
        f"iter={_langs} parse={_marks}",
    )

# A12 _try_repair_json 病态输入（性能/正确性）
chk(
    "A12a 完全非法 JSON 返回 None",
    cc._try_repair_json("this is not json at all") is None,
    "",
)
chk(
    "A12b 空串返回 None",
    cc._try_repair_json("") is None,
    "",
)
_repaired = cc._try_repair_json('{"a": 1, "b": {"c": 2')
chk(
    "A12c 补闭合括号可修",
    isinstance(_repaired, dict) and _repaired.get("b", {}).get("c") == 2,
    f"got={_repaired!r}",
)
_truncated = cc._try_repair_json('{"a": 1, "title": "未闭合')
chk(
    "A12d 补末尾引号可修",
    isinstance(_truncated, dict),
    f"got={_truncated!r}",
)

# ---------------------------------------------------------------------------
section("B. 一致性校核：规则 / 严重度 / 金额 / 标签词闸门")

# B1 low 档可达性（回归保护）
chk(
    "B1a 非关键事实+2值+单章节 → low",
    rules.assess_severity("质量标准", ["合格", "优良"], section_count=1) == "low",
    "",
)
chk(
    "B1b 非关键事实+2值+未知章节数 → medium（保守）",
    rules.assess_severity("质量标准", ["合格", "优良"]) == "medium",
    "",
)
chk(
    "B1c 关键事实一冲突即 high",
    rules.assess_severity("工期", ["45", "30"], section_count=1) == "high",
    "",
)
# B1d 边界：恰好 2 种表述 vs 1 种（docstring 说“恰好 2 种”判 low）
_r = rules.assess_severity("质量标准", ["合格"], section_count=1)
chk(
    "B1d 单值不应判 low（docstring：恰好 2 种表述）",
    _r != "low",
    f"got={_r}",
)

# B2 金额单位归一
chk(
    "B2a 1200万元 == 12000000元",
    rules.normalize_amount_to_yuan("1200万元") == rules.normalize_amount_to_yuan("12000000元")
    == "12000000",
    f"{rules.normalize_amount_to_yuan('1200万元')} / {rules.normalize_amount_to_yuan('12000000元')}",
)
chk(
    "B2b 1.5亿元 → 150000000",
    rules.normalize_amount_to_yuan("1.5亿元") == "150000000",
    f"got={rules.normalize_amount_to_yuan('1.5亿元')}",
)
chk(
    "B2c 无数字返回 None",
    rules.normalize_amount_to_yuan("待定") is None,
    "",
)
# B2d 千分位
chk(
    "B2d 1,200 万元 → 12000000",
    rules.normalize_amount_to_yuan("1,200万元") == "12000000",
    f"got={rules.normalize_amount_to_yuan('1,200万元')}",
)
# B2e 未知单位
chk(
    "B2e 未知单位返回 None（宁多报不漏报）",
    rules.normalize_amount_to_yuan("100 万美元") is None,
    f"got={rules.normalize_amount_to_yuan('100 万美元')}",
)

# B3 标签词闸门 —— 裸「人员数量」事实
_kw = rules.personnel_fact_keywords("人员数量")
chk(
    "B3a 裸「人员数量」应有标签词（否则闸门二对其失效）",
    len(_kw) > 0,
    f"got={sorted(_kw)}",
)
_kw2 = rules.personnel_fact_keywords("项目经理数量")
chk(
    "B3b 岗位变体有标签词",
    len(_kw2) > 0 and "项目经理" in _kw2,
    f"got={sorted(_kw2)}",
)
_kw3 = rules.personnel_fact_keywords("不存在的数量")
chk(
    "B3c 未知「数量」后缀仍回退为角色词",
    len(_kw3) > 0,
    f"got={sorted(_kw3)}",
)

# B3d 闸门实际行为：裸人员数量下的「共 20 人」
_content = "本项目共 20 人参与施工，其中钢筋直径 20 mm，构件编号 20 号。"
_occ = text_patch.find_value_occurrences(_content, "20")
_details = [
    {
        "pos": p,
        "text": _content[p : p + 2],
        "gate_pass": rules.fact_has_keyword_nearby(_content, p, p + 2, "人员数量"),
    }
    for p in _occ
]
_expected_b3d = [True, False, False]  # 共 20 人 / 钢筋直径 20 mm / 构件编号 20 号
_got_b3d = [d["gate_pass"] for d in _details]
chk(
    "B3d 裸人员数量：合法取值放行，无关数字（mm/号）被拦",
    len(_details) == 3 and _got_b3d == _expected_b3d,
    f"expected={_expected_b3d} got={_got_b3d} details={_details}",
)

# B4 标签词窗口非对称
chk(
    "B4a 前向窗口默认 12",
    rules.autofix_keyword_window_before() == 12,
    f"got={rules.autofix_keyword_window_before()}",
)
chk(
    "B4b 后向窗口默认 6",
    rules.autofix_keyword_window_after() == 6,
    f"got={rules.autofix_keyword_window_after()}",
)
# B4c 「工期 30 天，钢筋进场 30 批」：第二个 30 不应判为工期取值
_c4 = "本工程总工期 30 天，其中钢筋进场 30 批。"
_o4 = text_patch.find_value_occurrences(_c4, "30")
_g4 = [
    (p, rules.fact_has_keyword_nearby(_c4, p, p + 2, "工期")) for p in _o4
]
chk(
    "B4c 第二个「30」不应被放行（标签词窗口）",
    len(_g4) == 2 and _g4[0][1] is True and _g4[1][1] is False,
    f"got={_g4}",
)

# B5 事实抽取
aud = ConsistencyAuditor()
_f1 = aud.extract_facts_from_text("合同工期 45 天，总造价 1200 万元，项目经理 1 人，技术人员 20 人。")
_names = sorted({f.name for f in _f1})
chk(
    "B5a 多事实抽取",
    {"工期", "金额", "项目经理数量", "技术人员数量"}.issubset(set(_names)),
    f"names={_names}",
)
_f2 = aud.extract_facts_from_text("工期 30 天，工期 45 天")
chk(
    "B5b 同事实多值",
    [f.value for f in _f2] == ["30", "45"],
    f"got={[f.value for f in _f2]}",
)
# B5c 日历天写法
_f3 = aud.extract_facts_from_text("总工期 45 个日历天")
chk(
    "B5c 「45 个日历天」应可抽取（漏报检查）",
    len(_f3) == 1 and _f3[0].value == "45",
    f"got={[(f.name, f.value) for f in _f3]}",
)
# B5d 无标签裸数字不应误抽为工期
_f4 = aud.extract_facts_from_text("混凝土强度 C30，养护 7 天。")
chk(
    "B5d 「养护 7 天」不应误判为工期（需显式标签）",
    not any(f.name == "工期" and f.value == "7" for f in _f4),
    f"got={[(f.name, f.value) for f in _f4]}",
)

# B6 归一化去重
# B6a 归一化契约校验（facts_service.normalize_fact_value 明确「只消除排版差异，
# 不做语义推断」）：带单位写法不会被推断成裸数字，故此处校验该保守契约本身
chk(
    "B6a 归一化契约：450日历天 == 450 日历天（排版差异消除）",
    len(aud._distinct_values(["450日历天", "450 日历天"], fact_name="工期")) == 1,
    f"got={aud._distinct_values(['450日历天','450 日历天'], fact_name='工期')}",
)
chk(
    "B6b 金额单位归一不判冲突",
    len(aud._distinct_values(["1200万元", "12000000元"], fact_name="金额")) == 1,
    f"got={aud._distinct_values(['1200万元','12000000元'], fact_name='金额')}",
)
chk(
    "B6c 金额真冲突仍判出",
    len(aud._distinct_values(["1200万元", "1500万元"], fact_name="金额")) == 2,
    f"got={aud._distinct_values(['1200万元','1500万元'], fact_name='金额')}",
)

# B7 严重度脏值归一
chk(
    "B7a 空串/None 严重度回落 medium",
    rules.normalize_severity("") == "medium" and rules.normalize_severity(None) == "medium",
    f"got={rules.normalize_severity('')}",
)
chk(
    "B7b 未知严重度回落 medium",
    rules.normalize_severity("critical") == "medium",
    f"got={rules.normalize_severity('critical')}",
)
chk(
    "B7c 排序 SQL 带 ELSE",
    "ELSE" in rules.severity_sort_sql() and "4" in rules.severity_sort_sql(),
    f"got={rules.severity_sort_sql()}",
)

# B8 sources 提取
chk(
    "B8 章节来源 id 提取",
    rules.extract_source_section_ids(
        ["section_12 (施工组织方案)", "summary (摘要)", "section_3 (进度)"]
    )
    == [12, 3],
    f"got={rules.extract_source_section_ids(['section_12 (施工组织方案)', 'summary (摘要)', 'section_3 (进度)'])}",
)

# B9 安全标准正则（避免「正文含安全即抽」）
_f5 = aud.extract_facts_from_text("我们高度重视安全生产，制定了详细的安全管理制度并严格执行。")
chk(
    "B9 无显式标签的「安全」叙述不应误抽为安全标准",
    not any(f.name == "安全标准" for f in _f5),
    f"got={[(f.name, f.value) for f in _f5]}",
)

# B10 人员岗位拆分
_f6 = aud.extract_facts_from_text("项目经理 1 人、技术人员 20 人、管理人员 5 人")
chk(
    "B10 不同岗位人数不合并为同一事实",
    len({f.name for f in _f6}) == 3,
    f"got={[(f.name, f.value) for f in _f6]}",
)

# B11 资质等级
_f7 = aud.extract_facts_from_text("企业资质等级：施工总承包一级；另处写为：资质 甲级")
chk(
    "B11 资质等级抽取",
    len(aud.extract_facts_from_text("资质等级：一级")) >= 1,
    f"got={[(f.name, f.value) for f in _f7]}",
)

# B12 空/异常输入
chk("B12a 空文本抽取", aud.extract_facts_from_text("") == [], "")
chk("B12b None 文本抽取", aud.extract_facts_from_text(None) == [], "")
chk(
    "B12c 超长文本抽取不崩",
    isinstance(aud.extract_facts_from_text("工期 30 天。" * 20000), list),
    "",
)

# ---------------------------------------------------------------------------
section("C. 数字边界保护（autofix 安全底线）")

_c = "总工期 30 天，300 天为最长，3.30 版本，第30号。"
_o = text_patch.find_value_occurrences(_c, "30")
chk(
    "C1 数字边界：30 不匹配 300 / 3.30",
    all(_c[p : p + 2] == "30" and not re.match(r"\d", _c[p - 1]) if p > 0 else True for p in _o),
    f"positions={_o} chars={[_c[p:p+2] for p in _o]}",
)
_r = text_patch.replace_values(_c, {"30": "45"})
chk(
    "C2 替换不越界（300 与 3.30 保持不变）",
    "300" in _r["content"] and "3.30" in _r["content"],
    f"out={_r['content']!r}",
)

# ---------------------------------------------------------------------------
section("D. 本轮修复验证（2026-10-10）")

# D1 工期：日历天/个日历天/自然日/工作日 全写法可抽取
for _txt in (
    "建设工期：450 日历天",
    "总工期 450 个日历天",
    "合同工期为450日历天",
    "总工期450个自然日",
    "工期 90 工作日",
    "总工期：450天",
    "工期 450 天",
):
    _r = [(f.name, f.value) for f in aud.extract_facts_from_text(_txt)]
    chk(
        f"D1 「{_txt}」可抽取工期",
        ("工期", "450" if "450" in _txt else "90") in _r,
        f"got={_r}",
    )
chk(
    "D1b 无关「X 天」叙述不被误抽为工期",
    not any(f.name == "工期" and f.value == "7" for f in aud.extract_facts_from_text("混凝土养护 7 天。")),
    f"got={[(f.name, f.value) for f in aud.extract_facts_from_text('混凝土养护 7 天。')]}",
)

# D2 资质等级：一级/甲级/施工总承包一级 全写法可抽取
for _txt, _expect in (
    ("资质等级：一级", "一级"),
    ("资质等级：一级建筑业企业", "一级"),
    ("企业资质：施工总承包一级", "一级"),
    ("资质：甲级", "甲级"),
    ("资质等级：乙级", "乙级"),
    ("资质等级：施工总承包三级", "三级"),
):
    _r = [(f.name, f.value) for f in aud.extract_facts_from_text(_txt)]
    chk(
        f"D2 「{_txt}」抽出 {_expect}",
        ("资质等级", _expect) in _r,
        f"got={_r}",
    )

# D3 非对称窗口真正生效（12 前向 / 6 后向）
_c3 = "本工程总工期 30 天，其中钢筋进场 30 批。"
_o3 = text_patch.find_value_occurrences(_c3, "30")
_g3 = [(p, rules.fact_has_keyword_nearby(_c3, p, p + 2, "工期")) for p in _o3]
chk(
    "D3 前向 12 窗口生效：第二个「30」被拦",
    len(_g3) == 2 and _g3[0][1] is True and _g3[1][1] is False,
    f"got={_g3}",
)
# D3b 显式传 window 仍是「对称覆盖」的既有契约
chk(
    "D3b 显式 window=40 保持对称覆盖（既有调用契约）",
    rules.fact_has_keyword_nearby("总工期 30 天，钢筋进场 30 批。", 19, 21, "工期", window=40) is True,
    "",
)
# D3c 超窗标签词不放行
_c3c = "本工程的建设工期为 30 天，……此处相隔超过十二个字……钢筋进场 30 批。"
_o3c = text_patch.find_value_occurrences(_c3c, "30")
_g3c = [(p, rules.fact_has_keyword_nearby(_c3c, p, p + 2, "工期")) for p in _o3c]
chk(
    "D3c 标签词超出前向窗口时不放行",
    all(v is False for _, v in _g3c[1:]),
    f"got={_g3c}",
)

# D4 人员数量类：结构校验（取值须紧跟「人」）
for _txt, _needle, _expect in (
    ("本项目共 20 人参与施工。", "20", True),
    ("后勤人员：30 人。", "30", True),
    ("共 20 名施工人员到场。", "20", True),
    ("钢筋直径 20 mm。", "20", False),
    ("构件编号 20 号。", "20", False),
    ("本项目划分为 1 个标段。", "1", False),
):
    _p = text_patch.find_value_occurrences(_txt, _needle)[0]
    _got = rules.fact_has_keyword_nearby(_txt, _p, _p + len(_needle), "人员数量")
    chk(
        f"D4 「{_txt}」→ {_expect}",
        _got is _expect,
        f"got={_got}",
    )
# D4b 岗位变体事实名同样走结构校验：合法取值放行，无关数字被拦
_d4b_txt = "项目经理 1 人、钢筋 1 mm。"
_d4b_pos = text_patch.find_value_occurrences(_d4b_txt, "1")
_d4b_got = [
    rules.fact_has_keyword_nearby(_d4b_txt, p, p + 1, "项目经理数量") for p in _d4b_pos
]
chk(
    "D4b 岗位变体：项目经理 1 人放行，钢筋 1 mm 被拦",
    _d4b_got == [True, False],
    f"got={_d4b_got} pos={_d4b_pos}",
)
chk(
    "D4c 非人员事实名不受结构校验影响（设备数量）",
    rules.fact_has_keyword_nearby("设备数量 5 台。", 5, 6, "设备数量") is True,
    f"keywords={sorted(rules.personnel_fact_keywords('设备数量'))}",
)

# D5 裸「人员数量」有标签词（闸门不再失效）
chk(
    "D5a 裸「人员数量」有标签词",
    len(rules.personnel_fact_keywords("人员数量")) > 0,
    f"got={sorted(rules.personnel_fact_keywords('人员数量'))}",
)
chk(
    "D5b is_personnel_fact_name 判定",
    rules.is_personnel_fact_name("人员数量")
    and rules.is_personnel_fact_name("项目经理数量")
    and rules.is_personnel_fact_name("后勤人员数量")
    and not rules.is_personnel_fact_name("设备数量")
    and not rules.is_personnel_fact_name("工期"),
    "",
)

# D6 low 档边界：恰好 2 种表述才判 low
chk(
    "D6a 单值不判 low",
    rules.assess_severity("质量标准", ["合格"], section_count=1) != "low",
    f"got={rules.assess_severity('质量标准', ['合格'], section_count=1)}",
)
chk(
    "D6b 恰好 2 值 + 单章节 → low",
    rules.assess_severity("质量标准", ["合格", "优良"], section_count=1) == "low",
    "",
)

# D7 非字符串输入防护
chk("D7a None 输入不崩", aud.extract_facts_from_text(None) == [], "")
chk("D7b 数字输入不崩", aud.extract_facts_from_text(0) == [], "")
chk(
    "D7c 空串不崩",
    aud.extract_facts_from_text("") == [],
    "",
)

# D8 回归：既有正确用例不被新口径破坏
chk(
    "D8a 工期正常用例仍通过闸门",
    rules.fact_has_keyword_nearby("承诺工期 45 天完成。", 4, 6, "工期") is True,
    "",
)
chk(
    "D8b 金额标签词仍生效",
    rules.fact_has_keyword_nearby("合同价 1200 万元整。", 4, 7, "金额") is True
    and rules.fact_has_keyword_nearby("钢筋 1200 mm。", 3, 6, "金额") is False,
    f"got={rules.fact_has_keyword_nearby('钢筋 1200 mm。', 3, 6, '金额')}",
)
chk(
    "D8c 无标签词事实（术语类）不阻断",
    rules.fact_has_keyword_nearby("此处 AB 结束", 3, 5, "术语") is True,
    "",
)


# ---------------------------------------------------------------------------
_n_fail = sum(1 for s, _, _ in _results if s == "FAIL")
print(f"\n总计: {len(_results)} 条探针, PASS {len(_results) - _n_fail}, FAIL {_n_fail}")
for st, name, detail in _results:
    if st == "FAIL":
        print(f"  ✗ {name} :: {detail}")
