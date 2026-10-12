"""生成大纲模块 —— L3 标题质量 / 编号正确率 / 层级完整率 审计脚本（基线测量）。

用法（backend 目录下）：
    python -m scripts.audit_outline_l3

产出：JSON 报告到 stdout，含
    - dirty_titles      占位/泛化/裸编号后缀标题清单
    - dup_titles        同级撞名
    - numbering         编号正确率（id 位置式连续性）
    - level_integrity   层级完整率（level 与深度一致 + 最小子节点数）
    - scenarios         各场景明细
"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.ai.outline_common import (  # noqa: E402
    _cleanup_placeholder_titles,
    _ensure_min_children,
    _is_dirty_l3_title,
    _title_core,
    _uniquify_outline_titles,
)
from app.services.ai.outline_gate import renumber_outline_ids  # noqa: E402
from app.services.ai.workflows_outline_utils import (  # noqa: E402
    _dedupe_outline_titles,
    normalize_outline_items,
    validate_l3_specificity,
)

# ---------------------------------------------------------------- 脏标题样本库

DIRTY_SEEDS = [
    "专项6", "专项7", "专项13", "分阶段推进专项13", "总体方案专项6",
    "总体实施策划", "总体实施策划（2）", "总体实施策划（3）", "总体实施策划(2)",
    "总体实施策划（第6项）", "总体实施策划（二）", "总体实施策划2",
    "实施要点", "实施要点（2）", "基本要求", "主要内容", "专项方案",
    "总体方案与实施策划", "总体方案与实施策划（2）",
    "总体实施策划（其3）", "砌体成品保护（其2）",
    "总体实施策划（2）专项方案", "总体实施策划(2)专项方案（其2）",
]

LEGAL_SEEDS = [
    "地下室结构实测实量方案", "深基坑(超5m)支护方案", "混凝土(28天)配合比",
    "工期365天", "工序交接流程5", "样板引路与首件验收", "质量通病防治",
    "脚手架连墙件设置", "塔吊基础承重复核", "临时用电三级配电",
]


# ---------------------------------------------------------------- 测量工具


def walk(nodes, level=1, path=()):
    if not isinstance(nodes, list):
        return
    for i, n in enumerate(nodes, 1):
        if not isinstance(n, dict):
            continue
        p = path + (i,)
        yield n, level, p
        yield from walk(n.get("children"), level + 1, p)


def measure(outline: list) -> dict:
    nodes = list(walk(outline))
    total = len(nodes)

    dirty = [
        {"path": ".".join(map(str, p)), "level": lv, "title": n.get("title")}
        for n, lv, p in nodes
        if lv >= 3 and _is_dirty_l3_title(n.get("title") or "")
    ]
    dirty_any = [
        {"path": ".".join(map(str, p)), "level": lv, "title": n.get("title")}
        for n, lv, p in nodes
        if _is_dirty_l3_title(n.get("title") or "")
    ]

    # 同级撞名
    dups = []

    def dup_walk(ns, parent=""):
        if not isinstance(ns, list):
            return
        seen = {}
        for n in ns:
            if not isinstance(n, dict):
                continue
            t = (n.get("title") or "").strip()
            if t:
                c = _title_core(t)
                if c in seen:
                    dups.append({"parent": parent, "title": t, "ids": [seen[c], n.get("id")]})
                else:
                    seen[c] = n.get("id")
            dup_walk(n.get("children"), (n.get("title") or "").strip())

    dup_walk(outline)

    # 空标题
    empty = [
        {"path": ".".join(map(str, p)), "level": lv}
        for n, lv, p in nodes
        if not (n.get("title") or "").strip()
    ]

    # 编号正确率：id 必须是 1 / 1.2 / 1.2.3 且与路径一致
    id_ok = 0
    id_bad = []
    for n, lv, p in nodes:
        exp = ".".join(map(str, p))
        if str(n.get("id") or "") == exp:
            id_ok += 1
        else:
            id_bad.append({"id": n.get("id"), "expected": exp})

    # 层级完整率：level 与深度一致
    lvl_ok = sum(1 for n, lv, p in nodes if int(n.get("level") or lv) == lv)

    # 最小子节点
    min_l2 = sum(1 for n, lv, _ in nodes if lv == 1 and len(n.get("children") or []) < 3)
    min_l3 = sum(1 for n, lv, _ in nodes if lv == 2 and len(n.get("children") or []) < 5)
    l1 = [n for n, lv, _ in nodes if lv == 1]
    l2 = [n for n, lv, _ in nodes if lv == 2]
    l3 = [n for n, lv, _ in nodes if lv == 3]

    return {
        "total_nodes": total,
        "l1": len(l1),
        "l2": len(l2),
        "l3": len(l3),
        "dirty_l3_count": len(dirty),
        "dirty_l3_pct": round(100.0 * len(dirty) / max(len(l3), 1), 2),
        "dirty_any_level_count": len(dirty_any),
        "dirty_samples": [d["title"] for d in dirty[:15]],
        "dup_sibling_count": len(dups),
        "dup_samples": [d["title"] for d in dups[:10]],
        "empty_title_count": len(empty),
        "id_numbering_ok": id_ok,
        "id_numbering_pct": round(100.0 * id_ok / max(total, 1), 2),
        "id_bad_samples": id_bad[:8],
        "level_integrity_pct": round(100.0 * lvl_ok / max(total, 1), 2),
        "min_children_violation_l1": min_l2,
        "min_children_violation_l2": min_l3,
    }


# ---------------------------------------------------------------- 场景构造


def hostile_tree(l1_count: int = 8, l2_per: int = 3, l3_per: int = 6) -> list:
    """模拟真实故障：AI 复读泛化父级标题 + 裸编号后缀（用户报告形态）。"""
    tree = []
    generic_l2 = ["总体方案与实施策划", "分阶段推进计划", "资源配置与协调保障",
                  "过程控制与考核机制", "总结评估与持续改进"]
    for i in range(l1_count):
        l1 = {
            "id": str(i + 1),
            "title": f"第{i + 1}章 总体实施策划",
            "level": 1,
            "children": [],
        }
        for j in range(l2_per):
            base = generic_l2[(i + j) % len(generic_l2)]
            l2 = {
                "id": f"{i + 1}.{j + 1}",
                "title": base,
                "level": 2,
                "children": [],
            }
            for k in range(l3_per):
                # 轮换产生：纯泛化 / 专项N / 裸编号后缀 / 半角变体
                mode = k % 5
                if mode == 0:
                    t = base
                elif mode == 1:
                    t = f"专项{(i * 7 + j * 3 + k) % 20 + 1}"
                elif mode == 2:
                    t = f"{base}（{k // 5 + 2}）"
                elif mode == 3:
                    t = f"{base}({k // 5 + 2})"
                else:
                    t = f"分阶段推进专项{(i + j + k) % 15 + 1}"
                l2["children"].append(
                    {"id": f"{i + 1}.{j + 1}.{k + 1}", "title": t, "level": 3}
                )
            l1["children"].append(l2)
        tree.append(l1)
    return tree


def mixed_tree(n: int = 40) -> list:
    tree = []
    for i in range(4):
        l1 = {"id": str(i + 1), "title": f"章{i + 1} 质量保证措施", "level": 1, "children": []}
        for j in range(4):
            l2 = {"id": f"{i + 1}.{j + 1}", "title": f"分项{j + 1}", "level": 2, "children": []}
            for k in range(3):
                l2["children"].append({
                    "id": f"{i + 1}.{j + 1}.{k + 1}",
                    "title": (DIRTY_SEEDS + LEGAL_SEEDS)[(i * 7 + j * 3 + k) % (len(DIRTY_SEEDS) + len(LEGAL_SEEDS))],
                    "level": 3,
                })
            l1["children"].append(l2)
        tree.append(l1)
    return tree


class _EchoProvider:
    """复读输入的敌意 provider：任何修复请求都返回占位标题。"""

    def __init__(self):
        self.calls = 0

    async def chat(self, messages, **kwargs):
        self.calls += 1
        user = ""
        for m in messages:
            c = m.get("content") if isinstance(m, dict) else ""
            if "L3_TITLE" in str(m.get("content", ""))[:200] or True:
                user = str(c)
        for bad in DIRTY_SEEDS:
            if bad in user:
                return bad
        return "专项6"


class _GoodProvider:
    def __init__(self):
        self.calls = 0

    async def chat(self, messages, **kwargs):
        self.calls += 1
        pool = ["地下室结构实测实量方案", "深基坑支护监测方案", "临边洞口防护检查",
                "临时用电三级配电复核", "样板引路与首件验收", "工序交接与实测实量"]
        return pool[self.calls % len(pool)]


# ---------------------------------------------------------------- 场景执行


async def run_save_chain(tree: list) -> dict:
    """复刻 _clear_and_save_outline 的落库归一化链。"""
    t = copy.deepcopy(tree)
    t = normalize_outline_items(t)
    t = _dedupe_outline_titles(t)
    _ph = _cleanup_placeholder_titles(t)
    t = _uniquify_outline_titles(t)
    _ph2 = _cleanup_placeholder_titles(t)
    t = _ensure_min_children(t)
    t = _uniquify_outline_titles(t)
    renumber_outline_ids(t)
    return {"tree": t, "ph_fixed": _ph + _ph2}


async def run_full_closeout(tree: list, provider) -> dict:
    """复刻 _apply_l3_quality_closeout（含 AI 修复）。"""
    from app.services.ai.workflows_outline_v51 import _apply_l3_quality_closeout

    t = copy.deepcopy(tree)
    t = await _apply_l3_quality_closeout(provider, t, summary="测试项目", phase_callback=None)
    renumber_outline_ids(t)
    return {"tree": t}


def main() -> int:
    report: dict = {}

    # ---- S1 敌意目录 → 落库归一化链
    s1 = hostile_tree()
    report["s1_hostile_raw"] = measure(s1)
    r1 = asyncio.run(run_save_chain(s1))
    report["s1_hostile_saved"] = measure(r1["tree"])

    # ---- S2 敌意目录 + 直出收口（AI 复读占位）
    p_echo = _EchoProvider()
    r2 = asyncio.run(run_full_closeout(hostile_tree(), p_echo))
    report["s2_direct_closeout_echo_provider"] = measure(r2["tree"])
    report["s2_direct_closeout_echo_provider"]["provider_calls"] = p_echo.calls

    # ---- S3 敌意目录 + 直出收口（AI 正常）
    p_good = _GoodProvider()
    r3 = asyncio.run(run_full_closeout(hostile_tree(), p_good))
    report["s3_direct_closeout_good_provider"] = measure(r3["tree"])

    # ---- S4 混合脏/净目录 → 落库链（误伤检查）
    s4 = mixed_tree()
    report["s4_mixed_raw"] = measure(s4)
    r4 = asyncio.run(run_save_chain(s4))
    report["s4_mixed_saved"] = measure(r4["tree"])
    legal_before = {(n.get("title")) for n, _, _ in walk(s4) if (n.get("title") or "") in LEGAL_SEEDS}
    legal_after = {(n.get("title")) for n, _, _ in walk(r4["tree"]) if (n.get("title") or "") in LEGAL_SEEDS}
    report["s4_legal_preserved"] = {
        "before": sorted(legal_before),
        "after_kept": sorted(legal_before & legal_after),
        "lost": sorted(legal_before - legal_after),
    }

    # ---- S5 空/异常输入容错
    edge = {}
    for name, val in [("none", None), ("empty", []), ("scalar", [1, 2, 3]),
                      ("bad_children", [{"id": "1", "title": "A", "children": "oops"}])]:
        try:
            rr = asyncio.run(run_save_chain(val if isinstance(val, list) else []))
            edge[name] = {"ok": True, "measure": measure(rr["tree"])}
        except Exception as e:  # noqa: BLE001
            edge[name] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    report["s5_edge_inputs"] = edge

    # ---- 汇总
    dirty_total = 0
    l3_total = 0
    for k, v in report.items():
        if isinstance(v, dict) and "dirty_l3_count" in v and "raw" not in k:
            dirty_total += v["dirty_l3_count"]
            l3_total += v["l3"]
    report["_summary"] = {
        "dirty_l3_total": dirty_total,
        "l3_total": l3_total,
        "dirty_l3_pct": round(100.0 * dirty_total / max(l3_total, 1), 2),
    }

    text = json.dumps(report, ensure_ascii=False, indent=2)
    out = Path(__file__).resolve().parents[1] / "_audit_l3_report.json"
    out.write_text(text, encoding="utf-8")
    for key, val in report.items():
        if isinstance(val, dict) and "dirty_l3_count" in val:
            print(
                "%-38s dirty_l3=%3d/%3d (%5.2f%%)  dup=%d empty=%d id=%s%% lvl=%s%%"
                % (
                    key, val["dirty_l3_count"], val["l3"], val["dirty_l3_pct"],
                    val["dup_sibling_count"], val["empty_title_count"],
                    val["id_numbering_pct"], val["level_integrity_pct"],
                )
            )
    print("SUMMARY", report["_summary"])
    print("legal lost:", report["s4_legal_preserved"]["lost"])
    print("edge:", {k: (v.get("ok"), v.get("measure", {}).get("total_nodes"))
                    for k, v in report["s5_edge_inputs"].items()})
    print("report ->", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
