# -*- coding: utf-8 -*-
"""探针：验证三级标题脏标题判定与去重的真实行为。

针对用户报告的「专项6」「总体实施策划（2）」「总体实施策划（3）」问题。
"""
import sys
sys.path.insert(0, r"J:\编程\招投标方案平台\backend")

from app.services.ai import outline_common as oc

CASES = [
    "专项6",
    "总体实施策划（2）",
    "总体实施策划（3）",
    "总体实施策划(2)",          # 半角括号
    "总体实施策划",
    "总体实施策划2",            # 无括号裸数字后缀
    "总体实施策划3",
    "脚手架专项6",
    "分阶段推进专项13",
    "总体实施策划（第2项）",
    "总体实施策划（其3）",
    "总体实施策划  （2）",      # 带空白
    "质量保证组织体系",
    "主体结构混凝土浇筑",
]
print("=" * 60)
print("脏标题判定（True=脏）：")
for c in CASES:
    print(f"  {str(oc._is_dirty_l3_title(c)):>5}  {c}")
print()
print("=" * 60)
print("归一化 core 输出（判定「是否重复」的实际键）：")
for c in CASES:
    print(f"  {repr(oc._strip_ordinal_suffix(c)):<30} <- {c}")

print()
print("=" * 60)
print("配置开关：")
from app import config
for name in ["OUTLINE_L3_QUALITY_ENHANCE_ENABLED", "OUTLINE_L3_REPAIR_ENABLED",
             "OUTLINE_L3_REPAIR_MAX", "OUTLINE_L3_REPAIR_CONCURRENCY",
             "OUTLINE_DIRECT_MAX_LEVEL1", "OUTLINE_DIRECT_MAX_SCORING_CHARS",
             "OUTLINE_DIRECT_MAX_SCORING_ITEMS"]:
    print(f"  {name} = {getattr(config, name, '<MISSING>')}")

print()
print("=" * 60)
print("去重实测：构造含「总体实施策划」重复 + 脏标题的 L3 树")
tree = [{"title": "第1章 总体布置", "children": [
    {"title": "一、总体部署", "children": [
        {"title": "总体实施策划"},
        {"title": "总体实施策划"},
        {"title": "总体实施策划"},
        {"title": "总体实施策划（2）"},
        {"title": "专项6"},
        {"title": "总体实施策划(2)"},
        {"title": "总体实施策划2"},
    ]},
]}]
before = [n["title"] for n in tree[0]["children"][0]["children"]]
print("  输入 :", before)
fixed = oc._cleanup_placeholder_titles(tree)
after_cleanup = [n["title"] for n in tree[0]["children"][0]["children"]]
print(f"  清洗 {fixed} 个后: {after_cleanup}")
tree = oc._uniquify_outline_titles(tree)
after_uniq = [n["title"] for n in tree[0]["children"][0]["children"]]
print("  去重后:          ", after_uniq)
still_dirty = [t for t in after_uniq if oc._is_dirty_l3_title(t)]
print("  去重后仍脏:", still_dirty)
