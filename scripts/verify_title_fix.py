"""验证目录冗余后缀修复：导入 + 行为回归。

运行：backend\\.venv\\Scripts\\python.exe scripts\\verify_title_fix.py
"""

from __future__ import annotations

import copy
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

# 1) 导入链完整性
import app.services.export  # noqa: F401
import app.services.ai.workflows_outline_v51  # noqa: F401
import app.services.ai.workflows_outline_utils  # noqa: F401
from app.services.ai.outline_common import (
    _cn_ordinal,
    _disambiguate_title,
    _parent_tokens,
    _strip_legacy_redundant_suffix,
    _title_core,
    _uniquify_outline_titles,
)
from app.services.ai.workflows_outline_utils import (
    _dedupe_outline_titles,
    validate_outline_business,
)
from app.services.ai.workflows_outline_v51 import _auto_fix_issues, _quick_local_check

BARE = re.compile(r"（[0-9一二三四五六七八九十]+）$")


def check(cond: bool, msg: str) -> None:
    print(("  PASS  " if cond else "  FAIL  ") + msg)
    assert cond, msg


def tree() -> list[dict]:
    """复现截图结构：同名三级条目分属不同二级章（标书合法形态）。"""
    return [
        {
            "title": "砌体工程",
            "children": [
                {"title": "施工组织与资源配置", "children": [{"title": "砌体成品保护"}]},
                {"title": "机电管线预留", "children": [{"title": "砌体成品保护"}]},
                {"title": "质量控制措施", "children": [{"title": "机电管线材料管理实施细则"}]},
                {"title": "验收标准", "children": [{"title": "机电管线材料管理实施细则"}]},
            ],
        }
    ]


print("\n[A] 归一化与剥离")
check(_strip_legacy_redundant_suffix("砌体成品保护（其2）") == "砌体成品保护", "剥离（其2）")
check(_strip_legacy_redundant_suffix("细则（其 3）") == "细则", "剥离带空格变体")
check(_strip_legacy_redundant_suffix("细则(其4)") == "细则", "剥离半角变体")
check(_strip_legacy_redundant_suffix("（其2）（其3）") == "", "堆叠后缀")
check(_strip_legacy_redundant_suffix("砌体成品保护") == "砌体成品保护", "无命中原样返回")
check(
    _strip_legacy_redundant_suffix("混凝土(28天)强度检验") == "混凝土(28天)强度检验",
    "不误伤合法半角括号说明",
)
check(_title_core("砌体成品保护（其2）") == _title_core("砌体成品保护"), "（其N）归一后同名")
for n, exp in [(1, "一"), (2, "二"), (3, "三"), (10, "十"), (11, "十一"), (19, "十九"), (25, "二十五"), (99, "九十九"), (100, "100")]:
    check(_cn_ordinal(n) == exp, f"中文序数 {n}->{exp}")

print("\n[B] 消歧：同父重名 + 跨父合法同名")
# B1 截图中被污染的结构 -> 必须保持原样（跨父同名合法）
t = tree()
_uniquify_outline_titles(t)
titles = [x["children"][0]["title"] for x in t[0]["children"]]
check(titles == ["砌体成品保护", "砌体成品保护", "机电管线材料管理实施细则", "机电管线材料管理实施细则"],
      f"跨父合法同名保持原样: {titles}")
check(not any(BARE.search(x) for x in titles), "无裸编号后缀")

# B2 存量脏数据：已有（其N）后缀 -> 剥离后跨父同名保留
t2 = copy.deepcopy(tree())
t2[0]["children"][1]["children"][0]["title"] = "砌体成品保护（其2）"
t2[0]["children"][3]["children"][0]["title"] = "机电管线材料管理实施细则（其2）"
_uniquify_outline_titles(t2)
t2t = [x["children"][0]["title"] for x in t2[0]["children"]]
check(t2t == titles, f"存量（其N）被剥离: {t2t}")

# B3 真·同级重名：必须消歧且不可用裸编号
t3 = [{
    "title": "质量保证",
    "children": [{
        "title": "质量管理措施",
        "children": [
            {"title": "总体实施策划"},
            {"title": "总体实施策划"},
            {"title": "总体实施策划"},
        ],
    }],
}]
_uniquify_outline_titles(t3)
t3t = [c["title"] for c in t3[0]["children"][0]["children"]]
check(len(set(t3t)) == 3, f"同级重名已消歧: {t3t}")
check(t3t[0] == "总体实施策划", "首项保持原样")
check(not any(BARE.search(x) or "（其" in x for x in t3t), f"无裸编号/（其N）: {t3t}")

# B4 幂等
before = copy.deepcopy(t3)
_uniquify_outline_titles(t3)
after = [c["title"] for c in t3[0]["children"][0]["children"]]
check(after == [c["title"] for c in before[0]["children"][0]["children"]], "重复调用幂等")

# B5 消歧函数直接调用：20 次海量重复（旧实现此处产出（其N））
seen: set[str] = set()
outs: list[str] = []
for _ in range(20):
    c = _disambiguate_title("总体实施策划", seen)
    seen.add(c)
    outs.append(c)
check(len(set(outs)) == 20, f"20 次重复全部唯一: {sorted(set(outs))}")
check(not any(BARE.search(x) or "（其" in x for x in outs), f"无冗余后缀: {outs}")

# B6 _parent_tokens 提取父级差异词
pt = _parent_tokens("专业分包资源配置与协调保障", "专业分包地下施工工艺")
check("资源配置" in pt, f"提取父级差异词: {pt}")
check(_parent_tokens("", "任意标题") == [], "空父级返回空")

print("\n[C] 其他去重环节作用域")
# C1 _dedupe_outline_titles：跨父合法同名不得删除
t4 = tree()
kept = _dedupe_outline_titles(t4)
check(len(kept[0]["children"]) == 4, f"跨父同名节点未删除: {len(kept[0]['children'])} 个二级")
# C2 同级真重名仍删除
t5 = [{
    "title": "A",
    "children": [
        {"title": "X", "children": [{"title": "Y"}, {"title": "Y"}]},
    ],
}]
k5 = _dedupe_outline_titles(t5)
check(len(k5[0]["children"][0]["children"]) == 1, "同级重复节点被删除")

# C3 validate_outline_business：跨父同名不再拒绝
ok, msg = validate_outline_business(
    [{"title": "A", "children": [
        {"title": "B1", "children": [{"title": "工艺"}, {"title": "材料"}]},
        {"title": "B2", "children": [{"title": "工艺"}, {"title": "材料"}]},
    ]}]
)
check(ok, f"跨父同名通过校验: {msg}")
ok2, msg2 = validate_outline_business(
    [{"title": "A", "children": [
        {"title": "B1", "children": [{"title": "工艺"}, {"title": "工艺"}]},
        {"title": "B2", "children": [{"title": "材料"}, {"title": "材料"}]},
    ]}]
)
check(not ok2, f"同级同名仍报错: {msg2}")
check("重复" in msg2, f"报错原因为同级重复: {msg2}")

# C4 _quick_local_check：跨父同名不报重复
iss = _quick_local_check(tree())
check(not any("重复标题" in x for x in iss), f"跨父同名不报重复: {iss}")
_fix_tree = [
    {
        "title": "A",
        "id": "1",
        "children": [
            {
                "title": "B1",
                "id": "1",
                "children": [
                    {"title": "Y", "id": "1"},
                    {"title": "Y", "id": "2"},
                ],
            },
        ],
    }
]
iss2 = _quick_local_check(_fix_tree)
check(any("重复标题" in x for x in iss2), f"同级同名仍报重复: {iss2}")

# C5 _auto_fix_issues：只修同级重名，且不用裸编号
fixed = _auto_fix_issues(_fix_tree, iss2)
ft = [c["title"] for c in fixed[0]["children"][0]["children"]]
check(len(set(ft)) == 2, f"同级重名已修复: {ft}")
check(not any(BARE.search(x) or "（其" in x for x in ft), f"修复结果无冗余后缀: {ft}")

print("\n全部通过")
