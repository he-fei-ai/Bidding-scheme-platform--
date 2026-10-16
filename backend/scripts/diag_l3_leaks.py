"""诊断脚本：三级标题占位/泛化/编号冲突泄漏点定位（只读，不落库）。

覆盖：
  D1 深度脏判定器对「消歧产物」与半角括号变体的判定
  D2 不经清洗直接去重：泛化标题是否被追加维度词后落库
  D3 repair_l3_generic_titles 空标题 / 配额泄漏
  D4 跨章复用护栏默认参数下的改写结果
  D5 生产链路顺序（cleanup→uniquify→cap→cleanup）端到端残留
  D6 标题增删改后编号重算（outline_numbering / outline_gate）
  D7 _ensure_min_children 补齐标题质量
  D8 _quick_local_check / _auto_fix_issues 的空标题兜底质量
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import (  # noqa: E402
    OUTLINE_L3_MAX_PARENT_REUSE,
    OUTLINE_L3_QUALITY_ENHANCE_ENABLED,
    OUTLINE_L3_REPAIR_MAX,
    OUTLINE_L3_REPAIR_QUOTA_BY_TASK,
)
from app.services.ai import workflows_outline_v51 as V  # noqa: E402
from app.services.ai.outline_common import (  # noqa: E402
    _cap_cross_chapter_repetition,
    _cleanup_placeholder_titles,
    _ensure_min_children,
    _is_dirty_l3_title,
    _is_dirty_l3_title_deep,
    _uniquify_outline_titles,
)

OUT: dict = {}


def _tree_generic_l2(n_l1: int = 6, n_l3: int = 5) -> list:
    """构造生产形态：多章 + 通用型 L2（总体方案与实施策划）+ 泛化 L3。"""
    tree = []
    for i in range(1, n_l1 + 1):
        tree.append(
            {
                "id": str(i),
                "title": f"第{i}章 总体方案与实施策划",
                "children": [
                    {
                        "id": f"{i}.1",
                        "title": "总体方案与实施策划",
                        "children": [
                            {"id": f"{i}.1.{k+1}", "title": "总体实施策划"}
                            for k in range(n_l3)
                        ],
                    }
                ],
            }
        )
    return tree


def d1_detector() -> None:
    cases = [
        "总体实施策划专项方案",
        "总体实施策划方案",
        "总体实施策划措施",
        "总体实施策划补充措施",
        "总体实施策划(2)",
        "总体实施策划（2）",
        "总体实施策划(六)",
        "总体实施策划专项6",
        "总体实施策划之六",
        "总体实施策划之一",
        "第一章施工部署",
        "工序交接流程5",
        "质量管控措施（第2项）",
        "第一阶段目标",
        "阶段目标之一",
    ]
    OUT["D1_detector"] = {
        c: (_is_dirty_l3_title(c), _is_dirty_l3_title_deep(c)) for c in cases
    }


def d2_uniquify_without_cleanup() -> None:
    t = _tree_generic_l2(1, 5)
    V._uniquify_outline_titles(t)
    OUT["D2_uniquify_only"] = [c["title"] for c in t[0]["children"][0]["children"]]


def d3_repair_quota() -> None:
    from app.services.ai.workflows_outline_utils import repair_l3_generic_titles

    class _Echo:
        async def chat(self, m, **k):
            return "专项7"

    tr = [
        {
            "id": "1",
            "title": "总体施工部署",
            "children": [
                {
                    "id": "1.1",
                    "title": "总体方案与实施策划",
                    "children": [
                        {"id": "1.1.1", "title": "总体实施策划（2）"},
                        {"id": "1.1.2", "title": "总体实施策划"},
                        {"id": "1.1.3", "title": "专项6"},
                        {"id": "1.1.4", "title": ""},
                        {"id": "1.1.5", "title": "总体实施策划(2)"},
                    ],
                }
            ],
        }
    ]
    out = asyncio.run(
        repair_l3_generic_titles(_Echo(), tr, "", "", "", "", "", max_repair=3)
    )
    OUT["D3_after_repair_max3"] = [c["title"] for c in out[0]["children"][0]["children"]]
    # 配额泄漏复现：空标题放在最前两位。旧实现 warnings[:3] 切片后
    # `if not l3_title: continue` 跳过两个空标题 → 实际只修 1 个，
    # 且 1.1.4/1.1.5 因配额已满永远进不了队列。
    tr2 = [
        {
            "id": "1",
            "title": "总体施工部署",
            "children": [
                {
                    "id": "1.1",
                    "title": "总体方案与实施策划",
                    "children": [
                        {"id": "1.1.1", "title": ""},
                        {"id": "1.1.2", "title": ""},
                        {"id": "1.1.3", "title": "总体实施策划（2）"},
                        {"id": "1.1.4", "title": "专项6"},
                        {"id": "1.1.5", "title": "总体实施策划(2)"},
                    ],
                }
            ],
        }
    ]
    out2 = asyncio.run(
        repair_l3_generic_titles(_Echo(), tr2, "", "", "", "", "", max_repair=3)
    )
    _t2 = [c["title"] for c in out2[0]["children"][0]["children"]]
    OUT["D3_empty_first_max3"] = _t2
    OUT["D3_empty_first_repaired"] = sum(1 for t in _t2 if t.strip())
    OUT["D3_config"] = {
        "REPAIR_MAX": OUTLINE_L3_REPAIR_MAX,
        "MAX_PARENT_REUSE": OUTLINE_L3_MAX_PARENT_REUSE,
        "L3_QUALITY_ENHANCE": OUTLINE_L3_QUALITY_ENHANCE_ENABLED,
        "QUOTA_BY_TASK": OUTLINE_L3_REPAIR_QUOTA_BY_TASK,
    }
def d4_cap_guard() -> None:
    t = _tree_generic_l2(6, 5)
    _cleanup_placeholder_titles(t)
    before = len({c["title"] for ch in t for c in ch["children"][0]["children"]})
    t2, fixed = _cap_cross_chapter_repetition(t, max_reuse=3)
    all_t = [c["title"] for ch in t2 for c in ch["children"][0]["children"]]
    OUT["D4_cap"] = {
        "before_unique": before,
        "after_unique": len(set(all_t)),
        "rewritten": fixed,
        "ch1": all_t[:5],
        "ch6": all_t[-5:],
    }


def d5_prod_chain() -> None:
    t = _tree_generic_l2(6, 5)
    t0 = time.perf_counter()
    _cleanup_placeholder_titles(t)
    t = _uniquify_outline_titles(t)
    t, _f = _cap_cross_chapter_repetition(t)
    t = _uniquify_outline_titles(t)
    _cleanup_placeholder_titles(t)
    dt = (time.perf_counter() - t0) * 1000
    all_l3 = [c["title"] for ch in t for c in ch["children"][0]["children"]]
    OUT["D5_prod_chain"] = {
        "ms": round(dt, 1),
        "dirty": [x for x in all_l3 if _is_dirty_l3_title_deep(x)],
        "uniq": len(set(all_l3)),
        "total": len(all_l3),
        "sample_ch3": [c["title"] for c in t[2]["children"][0]["children"]],
    }


def d6_renumber_after_edit() -> None:
    from app.services.ai.outline_gate import renumber_outline_ids

    t = _tree_generic_l2(1, 4)
    l2 = t[0]["children"][0]
    l3 = l2["children"]
    l3.insert(1, {"id": "", "title": "新插入标题", "children": []})
    renumber_outline_ids(t)
    after_insert = [c["id"] for c in l2["children"]]
    del l2["children"][2]
    renumber_outline_ids(t)
    after_delete = [c["id"] for c in l2["children"]]
    before = [c["id"] for c in l2["children"]]
    l2["children"][0]["title"] = "改名后的标题"
    renumber_outline_ids(t)
    OUT["D6_renumber"] = {
        "after_insert": after_insert,
        "after_delete": after_delete,
        "stable_on_rename": before == [c["id"] for c in l2["children"]],
        "ids_are_1based_contiguous": after_delete
        == ["1.1." + str(i) for i in range(1, len(after_delete) + 1)],
    }


def d7_ensure_min_children() -> None:
    t = [
        {
            "id": "1",
            "title": "工程质量保证措施",
            "children": [
                {"id": "1.1", "title": "总体方案与实施策划", "children": []}
            ],
        }
    ]
    out = _ensure_min_children(t, min_level2=3, min_level3=5)
    l2 = out[0]["children"]
    OUT["D7_ensure_min"] = {
        "l2_count": len(l2),
        "l2_titles": [n["title"] for n in l2],
        "l3_titles": [c["title"] for c in l2[0]["children"]],
        "l3_dirty": [
            c["title"]
            for n in l2
            for c in n["children"]
            if _is_dirty_l3_title_deep(c["title"])
        ],
    }


def d8_quick_local_check() -> None:
    t = [
        {
            "id": "1",
            "title": "总体施工部署",
            "children": [
                {
                    "id": "1.1",
                    "title": "总体方案与实施策划",
                    "children": [
                        {"id": "1.1.1", "title": "总体实施策划（2）"},
                        {"id": "1.1.2", "title": ""},
                    ],
                }
            ],
        }
    ]
    issues = V._quick_local_check(t)
    OUT["D8_quick_local_check"] = issues
    fixed = V._auto_fix_issues(t, issues)
    OUT["D8_after_autofix"] = [c["title"] for c in fixed[0]["children"][0]["children"]]
    OUT["D8_dirty_after_autofix"] = [
        c["title"]
        for c in fixed[0]["children"][0]["children"]
        if _is_dirty_l3_title_deep(c["title"])
    ]


if __name__ == "__main__":
    for fn in (
        d1_detector,
        d2_uniquify_without_cleanup,
        d3_repair_quota,
        d4_cap_guard,
        d5_prod_chain,
        d6_renumber_after_edit,
        d7_ensure_min_children,
        d8_quick_local_check,
    ):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            OUT[f"{fn.__name__}_EXC"] = f"{type(e).__name__}: {e}"
    print(json.dumps(OUT, ensure_ascii=False, indent=2, default=str))

