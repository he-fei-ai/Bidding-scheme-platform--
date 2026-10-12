# -*- coding: utf-8 -*-
"""探针2：验证「清洗→去重」顺序是否会把脏标题重新写回去。"""
import sys
sys.path.insert(0, r"J:\编程\招投标方案平台\backend")
from app.services.ai import outline_common as oc
import io, logging
logging.basicConfig(level=logging.INFO, stream=io.StringIO())

def build(n_rep):
    return [{"title": "第1章 总体布置", "children": [
        {"title": "一、总体部署", "children": [
            {"title": "总体实施策划"} for _ in range(n_rep)
        ]},
    ]}]

for n in (3, 6, 10, 20):
    tree = build(n)
    fixed = oc._cleanup_placeholder_titles(tree)
    oc._uniquify_outline_titles(tree)
    out = [x["title"] for x in tree[0]["children"][0]["children"]]
    dirty = [t for t in out if oc._is_dirty_l3_title(t)]
    print(f"n={n:2d} 清洗{fixed}个 -> 最终{len(out)}个")
    print(f"   输出: {out}")
    print(f"   其中脏标题: {dirty}")
    print()

# 半角括号重复检测
print("=" * 60)
tree2 = [{"title": "第1章", "children": [
    {"title": "一、", "children": [
        {"title": "总体实施策划"},
        {"title": "总体实施策划(2)"},
        {"title": "总体实施策划2"},
    ]},
]}]
print("混合全/半角输入:", [x["title"] for x in tree2[0]["children"][0]["children"]])
oc._cleanup_placeholder_titles(tree2)
oc._uniquify_outline_titles(tree2)
out2 = [x["title"] for x in tree2[0]["children"][0]["children"]]
print("去重后:", out2)
print("脏:", [t for t in out2 if oc._is_dirty_l3_title(t)])
