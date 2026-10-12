# -*- coding: utf-8 -*-
"""验证 L3 脏标题判定与去重修复（含死循环防护）。"""
import sys, threading
sys.path.insert(0, r"J:\编程\招投标方案平台\backend")
import logging; logging.disable(logging.CRITICAL)

from app.services.ai import outline_common as oc


class _Timeout(Exception):
    pass


def run(name, fn, secs=8):
    box = {}

    def target():
        try:
            box["r"] = fn()
        except BaseException as e:
            box["e"] = e

    th = threading.Thread(target=target, daemon=True)
    th.start()
    th.join(secs)
    if th.is_alive():
        print(f"  [FAIL] {name}: 超时 {secs}s / 疑似死循环")
        return False
    if "e" in box:
        print(f"  [ERROR] {name}: {type(box['e']).__name__}: {box['e']}")
        return False
    print(f"  [OK] {name}")
    return True


# A. 应为脏
DIRTY = ["", "专项6", "分阶段推进专项13", "总体实施策划专项6",
         "总体实施策划（2）", "总体实施策划（3）", "总体实施策划（第6项）",
         "总体实施策划（二）", "总体实施策划(2)", "总体实施策划2",
         "总体实施策划20", "总体实施策划", "其他", "实施要点",
         "总体方案与实施策划", "资源配置与协调保障"]
# B. 应为合法（防误伤）
CLEAN = ["样板区质量控制", "关键线路进度计划", "地下室防水施工工艺",
         "脚手架专项施工方案", "质量保证组织体系",
         "总体实施策划（其3）",           # 合法极端兜底序数
         "2026年施工组织计划", "工期365天保障措施", "成本控制在预算95%以内",
         "深基坑（超5m）支护方案", "混凝土强度C30试验方案", "混凝土(28天)配合比",
         "钢筋保护层厚度25mm控制", "基坑(超深5m)监测方案", "工期365天",
         "三级抗震设防措施", "主体一次结构施工", "第2版施工方案",
         "深基坑(超5m)支护方案编制",
         # 关键：数字有业务语义的合法标题，不得误伤
         "工序交接流程5", "工序交接流程8", "质量验收流程2",
         "专项施工方案5", "样板引路流程3", "危大工程2类", "危大工程3类"]

fails = []
for t in DIRTY:
    if not oc._is_dirty_l3_title(t):
        fails.append(("应为脏但判合法", t))
for t in CLEAN:
    if oc._is_dirty_l3_title(t):
        fails.append(("应合法但判脏", t))
print("A/B 判定:")
for k, v in fails:
    print(f"  [FAIL] {k}: {v!r}")
print(f"  {'全部通过' if not fails else f'{len(fails)} 处失败'}  (脏{len(DIRTY)}/合法{len(CLEAN)})")

# C. 归一化剥离
print("\nC. _strip_ordinal_suffix:")
for t, exp in [("总体实施策划（2）", "总体实施策划"), ("总体实施策划(2)", "总体实施策划"),
               ("总体实施策划（第6项）", "总体实施策划"), ("总体实施策划2", "总体实施策划2"),
               ("总体实施策划（其3）", "总体实施策划（其3）"),
               ("深基坑（超5m）支护方案", "深基坑（超5m）支护方案"),
               ("混凝土强度C30试验方案", "混凝土强度C30试验方案")]:
    got = oc._strip_ordinal_suffix(t)
    flag = "OK " if got == exp else "FAIL"
    print(f"  [{flag}] {t!r} -> {got!r}  (期望 {exp!r})")
    if got != exp:
        fails.append(("剥离不符", t, got, exp))

# D. 去重链路：大规模重复 + 混合形态
print("\nD. 去重链路（防死循环 + 结果无脏标题）:")
def dedup_test():
    tree = [{"title": "第1章 总体布置", "children": [
        {"title": "一、总体部署", "children": [
            {"title": "总体实施策划"} for _ in range(60)
        ]},
    ]}]
    oc._uniquify_outline_titles(tree)
    out = [x["title"] for x in tree[0]["children"][0]["children"]]
    oc._cleanup_placeholder_titles(tree)
    out2 = [x["title"] for x in tree[0]["children"][0]["children"]]
    dirty = [t for t in out2 if oc._is_dirty_l3_title(t)]
    print(f"    60个重复 → {len(out2)}个, 唯一={len(set(out2))==len(out2)}, 清洗后脏={len(dirty)}")
    if dirty:
        print(f"    [FAIL] 脏标题: {dirty[:6]}")
        fails.append(("去重后仍脏", dirty[:3]))
    print(f"    样例: {out2[:6]}")
run("60重复去重", dedup_test)

def mixed_test():
    tree = [{"title": "第1章", "children": [
        {"title": "一、总体部署", "children": [
            {"title": "总体实施策划"}, {"title": "总体实施策划"},
            {"title": "总体实施策划（2）"}, {"title": "总体实施策划(2)"},
            {"title": "总体实施策划2"}, {"title": "总体实施策划（其3）"},
            {"title": "专项6"}, {"title": "总体实施策划（第6项）"},
        ]},
    ]}]
    oc._uniquify_outline_titles(tree)
    oc._cleanup_placeholder_titles(tree)
    out = [x["title"] for x in tree[0]["children"][0]["children"]]
    dirty = [t for t in out if oc._is_dirty_l3_title(t)]
    print(f"    8个混合形态 → {len(out)}个, 唯一={len(set(out))==len(out)}, 脏={len(dirty)}")
    print(f"    结果: {out}")
    if dirty:
        print(f"    [FAIL] 脏: {dirty}")
        fails.append(("混合去重后仍脏", dirty))
run("混合形态去重", mixed_test)

# E. 语义兜底池耗尽
print("\nE. _semantic_fallback_title 池耗尽:")
def fallback_test():
    used = {f"总体实施策划专项方案{k}" for k in range(1, 12)}
    used |= {f"总体实施策划之{k}" for k in "一二三四五六七八九十"}
    for i in range(5):
        t = oc._semantic_fallback_title("总体实施策划", i, used)
        if oc._is_dirty_l3_title(t):
            print(f"    [FAIL] 产出脏标题: {t!r}")
            fails.append(("兜底产出脏标题", t))
        else:
            used.add(t)
    print("    通过（无脏标题）")
run("兜底池耗尽", fallback_test)

print("\n" + "=" * 50)
print(f"总计: {len(fails)} 处失败" if fails else "总计: 全部通过")
for f in fails:
    print(f"  - {f}")
sys.exit(1 if fails else 0)
