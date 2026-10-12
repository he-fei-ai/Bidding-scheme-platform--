"""探针 2：定位「专项N /（2）（3）」脏标题在生产链路中的真实漏出点。"""
from __future__ import annotations

import asyncio
import copy
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.ai.outline_common import (  # noqa: E402
    _cleanup_placeholder_titles,
    _is_dirty_l3_title,
    _uniquify_outline_titles,
)
from app.services.ai import workflows_outline_v51 as V  # noqa: E402

OUT = {}


def tree():
    return [
        {"id": "1", "title": "总体施工部署", "children": [
            {"id": "1.1", "title": "总体方案与实施策划", "children": [
                {"id": "1.1.1", "title": "总体实施策划（2）"},
                {"id": "1.1.2", "title": "总体实施策划"},
                {"id": "1.1.3", "title": "专项6"},
                {"id": "1.1.4", "title": ""},
                {"id": "1.1.5", "title": "总体实施策划(2)"},
            ]},
        ]},
    ]


# ============ P1: 审核回灌路径 int 赋值 BUG (v51.py:5554) ============
def probe_retry_int_bug():
    t = tree()
    r = V._cleanup_placeholder_titles(t)
    OUT["P1_cleanup_returns"] = (type(r).__name__, r)
    # 复刻 v51.py:5554-5557 的赋值序列
    retry_tree = t
    retry_tree = V._cleanup_placeholder_titles(retry_tree)   # ← 5554
    OUT["P1_retry_tree_type_after_5554"] = type(retry_tree).__name__
    try:
        retry_tree = V._uniquify_outline_titles(retry_tree)  # ← 5555
        OUT["P1_uniquify_ok"] = True
        OUT["P1_retry_tree_type_after_5555"] = type(retry_tree).__name__
    except Exception as e:  # noqa: BLE001
        OUT["P1_uniquify_ok"] = False
        OUT["P1_uniquify_error"] = f"{type(e).__name__}: {e}"
    # self_check 拿到 int 会怎样
    class _P:
        async def chat(self, m, **k):
            return "{}"
    try:
        asyncio.run(V._self_check_v51(_P(), retry_tree, 0.0, verdict_out={}))
        OUT["P1_self_check_ok"] = True
    except Exception as e:  # noqa: BLE001
        OUT["P1_self_check_ok"] = False
        OUT["P1_self_check_error"] = f"{type(e).__name__}: {e}"


# ============ P2: _self_check_v51 在 L3 收口之后改标题，无二次收口 ============
def probe_selfcheck_after_closeout():
    t = tree()
    # 先跑 L3 收口（生产顺序）
    V._cleanup_placeholder_titles(t)
    V._uniquify_outline_titles(t)
    OUT["P2_after_closeout"] = [c["title"] for c in t[0]["children"][0]["children"]]

    issues = V._quick_local_check(t)
    OUT["P2_quick_check_issues"] = issues
    fixed = V._auto_fix_issues(t, issues)
    OUT["P2_after_autofix"] = [c["title"] for c in fixed[0]["children"][0]["children"]]
    # self_check 之后没有再收口 → 检查脏标题是否残留
    OUT["P2_dirty_after_selfcheck"] = [
        c["title"] for c in fixed[0]["children"][0]["children"]
        if _is_dirty_l3_title(c["title"])
    ]
    # 再收口一次能否救回
    V._cleanup_placeholder_titles(fixed)
    V._uniquify_outline_titles(fixed)
    OUT["P2_after_second_closeout"] = [c["title"] for c in fixed[0]["children"][0]["children"]]


# ============ P3: _auto_fix_issues 空标题填「子章节N」占位 ============
def probe_placeholder_from_autofix():
    t = [{"id": "1", "title": "章一", "children": [
        {"id": "1.1", "title": "节一", "children": [
            {"id": "1.1.1", "title": ""},
            {"id": "1.1.2", "title": "  "},
        ]},
    ]}]
    issues = V._quick_local_check(t)
    OUT["P3_issues"] = issues
    V._auto_fix_issues(t, issues)
    titles = [c["title"] for c in t[0]["children"][0]["children"]]
    OUT["P3_after"] = titles
    OUT["P3_is_dirty"] = [_is_dirty_l3_title(x) for x in titles]
    OUT["P3_is_placeholder_like"] = [bool(re.fullmatch(r"子章节\d+", x)) for x in titles]


# ============ P4: repair_l3_generic_titles AI 返回脏标题时保留原脏标题 ============
def probe_repair_keeps_dirty():
    from app.services.ai.workflows_outline_utils import repair_l3_generic_titles

    class _Echo:
        async def chat(self, m, **k):
            return "专项7"

    tr = tree()
    out = asyncio.run(repair_l3_generic_titles(_Echo(), tr, "", "", "", "", "", max_repair=4))
    OUT["P4_after_repair"] = [c["title"] for c in out[0]["children"][0]["children"]]


# ============ P5: _disambiguate_title 参数语义（used 是 core 集 vs parent） ============
def probe_disambig_signature():
    import inspect

    from app.services.ai.outline_common import _disambiguate_title
    sig = inspect.signature(_disambiguate_title)
    OUT["P5_signature"] = str(sig)
    used = {"总体实施策划"}
    OUT["P5_result_no_parent"] = _disambiguate_title("总体实施策划", used)
    OUT["P5_result_with_parent"] = _disambiguate_title("总体实施策划", set(), parent_title="质量管理措施")


# ============ P6: _is_dirty_l3_title 对「子章节3」「专项方案6」等漏网形态 ============
def probe_detector_gaps():
    from app.services.ai.outline_common import _is_dirty_l3_title as d
    cases = [
        "子章节3", "子章节1", "章节1", "专项方案6", "专项方案（6）", "实施要点2",
        "总体实施策划（2）（3）", "总体实施策划(一)", "总体实施策划(六)",
        "质量管控措施（第2项）", "分阶段推进计划（3）", "资源配置与协调保障（2）",
        "过程控制与考核评价(2)", "总结评估与持续改进（2）",
        "L3-专项7", "专项方案7", "3.1 施工部署", "第一项措施",
    ]
    OUT["P6_detector"] = {c: d(c) for c in cases}


# ============ P7: 深树 / 宽树性能与爆栈 ============
def probe_deep_tree():
    import time

    # 极深树（L1..L12）
    root = {"id": "1", "title": "深层", "children": []}
    cur = root
    for i in range(2, 12):
        nxt = {"id": f"{cur['id']}.{1}", "title": f"层{i}", "children": []}
        cur["children"].append(nxt)
        cur = nxt
    t0 = time.perf_counter()
    _cleanup_placeholder_titles([root])
    V._uniquify_outline_titles([root])
    from app.services.ai.outline_gate import renumber_outline_ids
    renumber_outline_ids([root])
    OUT["P7_deep_ok_ms"] = round((time.perf_counter() - t0) * 1000, 2)

    # 极宽泛化：单一 L2 下 200 个「总体实施策划」
    wide = [{"id": "1", "title": "章", "children": [
        {"id": "1.1", "title": "总体方案与实施策划", "children": [
            {"id": f"1.1.{i+1}", "title": "总体实施策划"} for i in range(200)
        ]}
    ]}]
    t0 = time.perf_counter()
    V._cleanup_placeholder_titles(wide)
    V._uniquify_outline_titles(wide)
    dt = (time.perf_counter() - t0) * 1000
    titles = [c["title"] for c in wide[0]["children"][0]["children"]]
    OUT["P7_wide_ms"] = round(dt, 2)
    OUT["P7_wide_dirty"] = sum(1 for x in titles if _is_dirty_l3_title(x))
    OUT["P7_wide_unique"] = len(set(titles))
    OUT["P7_wide_samples"] = titles[:8] + titles[-6:]


if __name__ == "__main__":
    for fn in (probe_retry_int_bug, probe_selfcheck_after_closeout,
               probe_placeholder_from_autofix, probe_repair_keeps_dirty,
               probe_disambig_signature, probe_detector_gaps, probe_deep_tree):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            OUT[f"{fn.__name__}_EXC"] = f"{type(e).__name__}: {e}"
    import json
    print(json.dumps(OUT, ensure_ascii=False, indent=2, default=str))
