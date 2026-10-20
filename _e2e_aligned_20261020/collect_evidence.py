# -*- coding: utf-8 -*-
"""E2E 验证 · 证据收集：sandbox DB 落库检查 + 服务端日志锚点 + 九大章节确定性演示。

用法（任意 cwd）：python collect_evidence.py free|aligned
输出：<root>/<mode>/evidence.md（供人工与报告引用）
"""
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BACKEND = Path(r"j:\编程\招投标方案平台\backend")
MODE = (sys.argv[1] if len(sys.argv) > 1 else "").strip().lower()
if MODE not in ("free", "aligned"):
    print("用法: python collect_evidence.py free|aligned")
    sys.exit(2)

WORK = ROOT / MODE
meta = json.loads((WORK / "run_meta.json").read_text(encoding="utf-8"))
pid = meta.get("project_id")
lines: list[str] = [f"# E2E 证据 · mode={MODE} · project_id={pid}", ""]

# ---------- 1. sandbox DB：sections 落库检查 ----------
db = sqlite3.connect(f"file:{(WORK / 'data' / 'bid_assistant.sqlite3')}?mode=ro", uri=True)
db.row_factory = sqlite3.Row
rows = [dict(r) for r in db.execute(
    """SELECT id, parent_id, level, sort_order, title, scoring_point_id, status,
              LENGTH(COALESCE(content,'')) AS content_len
       FROM sections WHERE project_id=? ORDER BY level, sort_order, id""",
    (pid,),
)]
l1 = [r for r in rows if r["level"] == 1]
lines.append("## 1. 一级目录落库（sections 表）")
lines.append("")
lines.append("| 序 | 标题 | scoring_point_id | 子节点数 |")
lines.append("|---|---|---|---|")
for r in l1:
    kids = sum(1 for x in rows if x["parent_id"] == r["id"])
    lines.append(f"| {r['sort_order']} | {r['title']} | {r['scoring_point_id'] or '-'} | {kids} |")
sp_pat = sum(1 for r in l1 if r["scoring_point_id"] and re.match(r"^[RS]\d+$", r["scoring_point_id"]))
lines.append("")
lines.append(f"- L1 总数 {len(l1)}；scoring_point_id 命中 R{{n}}/S{{n}} 命名空间的 **{sp_pat}/{len(l1)}**")
lines.append(f"- 总节点 {len(rows)}；叶子 {sum(1 for r in rows if not any(x['parent_id'] == r['id'] for x in rows))}")
done = [r for r in rows if r["content_len"] > 100]
lines.append(f"- 已生成正文节点 {len(done)}（status 分布：" + json.dumps(
    {s: sum(1 for r in rows if r['status'] == s) for s in sorted({r['status'] for r in rows})}, ensure_ascii=False) + "）")
lines.append("")

# ---------- 2. 服务端日志锚点 ----------
log_path = WORK / "server.log"
lines.append("## 2. 服务端日志锚点")
if log_path.exists():
    # Windows 重定向的 stderr 默认按本地代码页（GBK）写入，优先 GBK 解码
    raw = log_path.read_bytes()
    try:
        text = raw.decode("gbk")
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    anchors = {
        "Aligned L1 程序构造": r"Aligned 模式[:：].{0,40}(构造|程序)|L1 Aligned",
        "locked 抑制 zone 重排": r"一级目录全部由程序锁定（Aligned）",
        "zone 机械重排实际发生": r"工程逻辑顺序倒置，已机械重排",
        "直出路径": r"直出",
        "Aligned 回落 AI": r"回落 AI 生成",
    }
    for name, pat in anchors.items():
        hits = re.findall(pat, text)
        lines.append(f"- {name}: {'命中 %d 处' % len(hits) if hits else '未出现'}")
    # 摘录关键行
    key = [ln for ln in text.splitlines() if ("Aligned" in ln or "程序锁定" in ln or "机械重排" in ln or "回退分步" in ln)][:12]
    if key:
        lines.append("")
        lines.append("```")
        lines.extend(k[:200] for k in key)
        lines.append("```")
else:
    lines.append("- server.log 不存在")
lines.append("")

# ---------- 3. 九大章节确定性演示（不经 AI，程序构造本体） ----------
lines.append("## 3. 九大章节程序构造演示（build_nine_chapter_l1，确定性无 AI）")
sys.path.insert(0, str(BACKEND))
try:
    from app.services.ai.outline_alignment import build_nine_chapter_l1  # noqa: E402
    nine = build_nine_chapter_l1()
    for n in nine:
        lines.append(f"- {n['source_requirement_id']} {n['title']}  locked={n.get('locked')}  content_mode={n.get('content_mode')}")
except Exception as e:  # noqa: BLE001
    lines.append(f"- 演示失败: {e!r}")

(WORK / "evidence.md").write_text("\n".join(lines), encoding="utf-8")
print("\n".join(lines))
print(f"\n[collect] 写入 {WORK / 'evidence.md'}")
