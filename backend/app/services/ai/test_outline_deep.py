import sys
sys.path.insert(0, "..\..\..")

# === 测试 1: _auto_fix_issues 重复标题修复 ===
from workflows_outline_v51 import _auto_fix_issues

outline = [
    {"id": "1", "title": "质量保证", "children": [
        {"id": "1.1", "title": "质量检查", "children": []},
        {"id": "1.2", "title": "质量检查", "children": []},
        {"id": "1.3", "title": "质量检查", "children": []},
    ]}
]
issues = ["重复标题 '质量检查': 1/1 和 1/2", "重复标题 '质量检查': 1/1 和 1/3"]
result = _auto_fix_issues(outline, issues)
titles = [c["title"] for c in result[0]["children"]]
print(f"TEST1 3个重复标题修复: {titles}")
# 期望: ['质量检查', '质量检查（二）', '质量检查（三）'] — 全部唯一
assert len(set(titles)) == 3, f"FAIL: 标题不唯一 → {titles}"
print("  ✅ PASS")

# === 测试 2: _path_to_index 边界值 ===
def _path_to_index(path_part):
    path_part = path_part.strip()
    if "." in path_part:
        path_part = path_part.split(".")[-1]
    return int(path_part) - 1

try:
    _path_to_index("0")
    print("TEST2a _path_to_index('0') = -1 — 这会导致空数组访问，但不会抛 ValueError")
except ValueError as e:
    print(f"TEST2a _path_to_index('0') 抛 ValueError — 边界值问题")

try:
    r = _path_to_index("")
    print(f"TEST2b _path_to_index('') = {r} — 空字符串会抛 ValueError")
except ValueError as e:
    print(f"TEST2b _path_to_index('') 抛 ValueError: {e} — 被上层 except ValueError 吞掉了，修复无效")

# === 测试 3: _derive_child_title 的 while True 极端 ===
from outline_common import _derive_child_title
# 用一个超大 used 集合测试
used_big = {f"专项{n}" for n in range(1, 1000000)}
# 这会导致 while True 循环 100万次才返回！
import time
t0 = time.monotonic()
try:
    r = _derive_child_title("质量保证", 0, used_big, level=3)
    elapsed = time.monotonic() - t0
    print(f"TEST3 _derive_child_title 大 used 集合: 返回={r}, 耗时={elapsed:.2f}s")
except RecursionError as e:
    print(f"TEST3 RecursionError: {e}")

# === 测试 4: _merge_filled_children AI 返回比骨架多 ===
from workflows_outline_v51 import _merge_filled_children
skeleton = [{"id": "1.1", "title": "", "children": []}, {"id": "1.2", "title": "", "children": []}]
ai_return = [
    {"title": "方案A", "children": []},
    {"title": "方案B", "children": []},
    {"title": "方案C", "children": []},  # AI 多返回一个
]
result = _merge_filled_children(skeleton, ai_return)
print(f"TEST4 AI返回3个骨架2个: {[r['title'] for r in result]}")
assert len(result) == 2, f"FAIL: 骨架不应被扩展 → {result}"
print("  ✅ PASS")

# === 测试 5: _merge_filled_children 三级数量不一致 ===
skeleton2 = [
    {"id": "1.1", "title": "方案A", "children": [
        {"id": "1.1.1", "title": "", "children": []},
        {"id": "1.1.2", "title": "", "children": []},
        {"id": "1.1.3", "title": "", "children": []},
    ]}
]
ai_return2 = [
    {"title": "方案A", "children": [
        {"title": "要点1", "children": []},
        {"title": "要点2", "children": []},
    ]}
]
result2 = _merge_filled_children(skeleton2, ai_return2)
l3_titles = [c["title"] for c in result2[0]["children"]]
print(f"TEST5 AI返回2个三级骨架3个: {l3_titles}")
assert result2[0]["children"][2]["title"] == "", "FAIL: 第3个L3应保持空"
print("  ✅ PASS")

print("\n全部测试完成")
