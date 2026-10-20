# -*- coding: utf-8 -*-
"""E2E · free vs aligned 对比：SSE 事件流中的 locked/auto_fixed 证据 + 目录结构差异。"""
import io
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def walk(node, acc):
    """递归扫描事件里所有 dict，统计易标扩展字段透出情况。"""
    if isinstance(node, dict):
        if node.get("locked"):
            acc["locked"] += 1
        if node.get("source_requirement_id"):
            acc["sr"] += 1
        for v in node.values():
            walk(v, acc)
    elif isinstance(node, list):
        for v in node:
            walk(v, acc)


for mode in ("free", "aligned"):
    evs = [json.loads(l) for l in io.open(ROOT / mode / "05_outline_sse.jsonl", encoding="utf-8") if l.strip()]
    auto = [e for e in evs if "重排" in json.dumps(e, ensure_ascii=False)]
    acc = {"locked": 0, "sr": 0}
    for e in evs:
        walk(e, acc)
    print(f"== {mode} ==")
    print(f"  SSE 事件总数: {len(evs)}")
    print(f"  含「重排」事件: {len(auto)}")
    for a in auto[:3]:
        print("   >", json.dumps(a, ensure_ascii=False)[:200])
    print(f"  SSE 中透出 locked=True 节点: {acc['locked']}")
    print(f"  SSE 中透出 source_requirement_id 节点: {acc['sr']}")

# 目录结构对比（06_sections_flat.json）
for mode in ("free", "aligned"):
    flat = json.loads((ROOT / mode / "06_sections_flat.json").read_text(encoding="utf-8"))
    l1 = [s for s in flat if s.get("level") == 1]
    lv = {}
    for s in flat:
        lv[s["level"]] = lv.get(s["level"], 0) + 1
    sp = sum(1 for s in l1 if s.get("scoring_point_id"))
    print(f"== {mode} 结构 ==  L1={len(l1)} 层级分布={lv} L1带scoring_point_id={sp}")

# 导出文件对比
for mode in ("free", "aligned"):
    p = ROOT / mode / "09_export.docx"
    print(mode, "export.docx:", p.stat().st_size if p.exists() else "缺失")
