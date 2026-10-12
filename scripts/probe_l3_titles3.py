# -*- coding: utf-8 -*-
"""探针3：验证修复后判定层与去重层的行为。"""
import sys
sys.path.insert(0, r"J:\编程\招投标方案平台\backend")
from app.services.ai import outline_common as oc
import io, logging
logging.basicConfig(level=logging.INFO, stream=io.StringIO())

print("=" * 64)
print("A. 脏标题判定（应为 True）")
SHOULD_BE_DIRTY = [
    "", "专项6", "分阶段推进专项13", "总体实施策划专项6",
    "总体实施策划（2）", "总体实施策划（3）", "总体实施策划（第6项）",
    "总体实施策划（二）", "总体实施策划（其3）",
    "总体实施策划(2)",      # 半角
    "总体实施策划2",        # 裸数字
    "总体实施策划20",       # 裸数字两位
    "总体实施策划", "其他", "实施要点", "专项方案",
    "总体方案与实施策划", "资源配置与协调保障",
]
fail_a = []
for c in SHOULD_BE_DIRTY:
    r = oc._is_dirty_l3_title(c)
    if not r:
        fail_a.append(c)
    print(f"  {'✓' if r else '✗'}  {c!r}")
print(f"  失败: {fail_a}")

print()
print("=" * 64)
print("B. 合法标题（应为 False，验证无误伤）")
SHOULD_BE_CLEAN = [
    "样板区质量控制", "关键线路进度计划", "地下室防水施工工艺", "总平面布置",
    "脚手架专项施工方案", "质量保证组织体系",
    "2026年施工组织计划", "2025-2026年度采购方案",   # 年份
    "深基坑（超5m）支护方案", "混凝土强度C30试验方案",  # 括号内说明/数字
    "钢筋保护层厚度25mm控制", "模板支撑高度8m方案",     # 单位数字
    "一级资质企业准入要求", "三级资质要求",             # 中文序数合法
    "工期365天保障措施", "成本控制在预算95%以内",       # 百分比
    "基坑监测数据1000m范围分析",
    "主体一次结构施工", "三级抗震设防措施",
]
fail_b = []
for c in SHOULD_BE_CLEAN:
    r = oc._is_dirty_l3_title(c)
    if r:
        fail_b.append(c)
    print(f"  {'✓' if not r else '✗'}  {c!r}")
print(f"  失败: {fail_b}")

print()
print("=" * 64)
print("C. 归一化 _strip_ordinal_suffix")
for c in ["总体实施策划", "总体实施策划（2）", "总体实施策划(2)", "总体实施策划2",
          "总体实施策划（第6项）", "总体实施策划（其3）", "2026年施工组织计划",
          "深基坑（超5m）支护方案", "混凝土强度C30试验方案", "工期365天保障措施"]:
    print(f"  {oc._strip_ordinal_suffix(c)!r:<28} <- {c!r}")

print()
print("=" * 64)
print("D. 去重链路：重复泛化标题 + 半角括号混合")
tree = [{"title": "第1章 总体布置", "children": [
    {"title": "一、总体部署", "children": [
        {"title": "总体实施策划"}, {"title": "总体实施策划"},
        {"title": "总体实施策划（2）"}, {"title": "总体实施策划(2)"},
        {"title": "总体实施策划2"}, {"title": "总体实施策划（其3）"},
        {"title": "专项6"}, {"title": "总体实施策划（第6项）"},
    ]},
]}]
print("  输入:", [x["title"] for x in tree[0]["children"][0]["children"]])
tree = oc._uniquify_outline_titles(tree)
after1 = [x["title"] for x in tree[0]["children"][0]["children"]]
print("  去重后:", after1)
print("  去重后仍脏:", [t for t in after1 if oc._is_dirty_l3_title(t)])
n = oc._cleanup_placeholder_titles(tree)
after2 = [x["title"] for x in tree[0]["children"][0]["children"]]
print(f"  清洗{n}个后:", after2)
print("  清洗后仍脏:", [t for t in after2 if oc._is_dirty_l3_title(t)])
print("  唯一性:", len(set(after2)) == len(after2))

print()
print("=" * 64)
print("E. 大规模重复：60 个「总体实施策划」，验证不出现脏标题")
tree60 = [{"title": "第1章", "children": [
    {"title": "一、总体部署", "children": [{"title": "总体实施策划"} for _ in range(60)]},
]}]
oc._cleanup_placeholder_titles(tree60)
oc._uniquify_outline_titles(tree60)
out = [x["title"] for x in tree60[0]["children"][0]["children"]]
dirty = [t for t in out if oc._is_dirty_l3_title(t)]
print(f"  60个输入 → {len(out)}个输出, 唯一={len(set(out))==len(out)}, 脏={len(dirty)}")
print(f"  脏样例: {dirty[:8]}")
print(f"  输出样例: {out[:12]}")
