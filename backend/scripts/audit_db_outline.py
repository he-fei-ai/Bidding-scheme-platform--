"""审计现存 DB 中已落库的目录标题质量 + 编号一致性。只读。"""
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path

DB = Path(sys.argv[1] if len(sys.argv) > 1 else
          r"J:\编程\招投标方案平台\data\bid_assistant.sqlite3").resolve()

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.ai.outline_common import _is_dirty_l3_title, _title_core  # noqa: E402

PLACEHOLDER = re.compile(r"专项\s*\d+")
ORDINAL = re.compile(r"[（(]\s*第?\s*[0-9一二三四五六七八九十百]+\s*项?\s*[）)]\s*$")
QI = re.compile(r"[（(]\s*其\s*[0-9一二三四五六七八九十百]+\s*[）)]\s*$")

conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

rows = conn.execute("SELECT id, project_id, parent_id, title, level, sort_order, content FROM sections").fetchall()
print(f"DB={DB}  sections={len(rows)}")

by_proj = Counter()
dirty_rows = []
for r in rows:
    t = (r["title"] or "").strip()
    lv = int(r["level"] or 1)
    by_proj[r["project_id"]] += 1
    hits = []
    if PLACEHOLDER.search(t):
        hits.append("专项N")
    if ORDINAL.search(t):
        hits.append("（N）裸编号")
    if QI.search(t):
        hits.append("（其N）")
    if lv >= 3 and _is_dirty_l3_title(t):
        hits.append("脏L3")
    if hits:
        dirty_rows.append((r["project_id"], r["id"], lv, t, ",".join(hits)))

print(f"命中脏标题的 sections 行数: {len(dirty_rows)}")
print("\n-- 按命中类型统计 --")
print(Counter(h for *_, hh in dirty_rows for h in hh.split(",")))
print("\n-- 脏标题样例 (前 60) --")
for pid, sid, lv, t, h in dirty_rows[:60]:
    print(f"  P{pid} #{sid} L{lv} [{h}] {t}")

# 编号一致性：按 (project,parent,sort_order) 重算位置号，与前端 annotate 口径一致
print("\n-- 编号一致性抽样（前 5 个项目）--")
for pid, cnt in by_proj.most_common(5):
    prs = conn.execute(
        "SELECT id, parent_id, title, level, sort_order FROM sections WHERE project_id=?",
        (pid,)).fetchall()
    kids = {}
    for r in prs:
        kids.setdefault(r["parent_id"], []).append(r)
    for k in kids:
        kids[k].sort(key=lambda x: (x["sort_order"], x["id"]))
    bad = []
    dup = []

    def walk(nodes, prefix):
        for i, n in enumerate(nodes, 1):
            num = f"{prefix}.{i}" if prefix else str(i)
            kids.setdefault(None, [])
            dup_parent = num
            kids.setdefault(dup_parent, [])
            sibs = kids.get(n["id"], [])
            for j, s in enumerate(sibs, 1):
                sn = f"{num}.{j}"
                kids.setdefault(sn, [])
            walk(sibs, num)

    # 同级撞名
    def dups(nodes, path):
        seen = {}
        for n in nodes:
            c = _title_core(n["title"] or "")
            if not c:
                continue
            if c in seen:
                dup.append((path, n["title"], seen[c], n["id"]))
            else:
                seen[c] = n["id"]
            dups(kids.get(n["id"], []), f"{path}/{n['title']}"[:40])

    dups(kids.get(None, []), "")
    dprint = dup[:8]
    lvl_bad = [r["id"] for r in prs if not r["parent_id"] and int(r["level"] or 1) != 1]
    print(f"  project={pid} nodes={cnt} 同级撞名={len(dup)} L1层级错={len(lvl_bad)}")
    for d in dprint:
        print(f"     撞名: {d[1]!r} (id {d[2]} vs {d[3]}) @ {d[0]}")
conn.close()
