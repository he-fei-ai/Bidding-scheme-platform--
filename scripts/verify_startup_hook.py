"""启动钩子回归验证：init_db() 时自动清理遗留「（其N）」后缀。"""

import json
import os
import shutil
import tempfile

# 必须在导入 app 之前指定独立数据目录
_TMP = tempfile.mkdtemp(prefix="bid_startup_hook_")
os.environ["BID_ASSISTANT_DATA_DIR"] = _TMP

try:
    from app.db import get_db, init_db

    init_db()

    pid = None
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO projects (name, industry) VALUES (?, ?)",
            ("启动钩子验证", "市政"),
        )
        pid = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO sections (project_id, title, level, sort_order) VALUES (?, ?, ?, ?)",
            (pid, "砌体成品保护（其2）", 3, 1),
        )
        conn.execute(
            "INSERT INTO v51_tasks (project_id, phase, current_phase, final_outline_json) "
            "VALUES (?, ?, ?, ?)",
            (
                pid,
                "final",
                "final",
                json.dumps(
                    [{"title": "总体实施策划", "children": [{"title": "施工组织设计（其3）（其4）"}]}],
                    ensure_ascii=False,
                ),
            ),
        )
        conn.commit()

    # 第二次 init_db() 模拟「再次启动」，此时应触发清理
    init_db()

    with get_db() as conn:
        title = conn.execute(
            "SELECT title FROM sections WHERE project_id = ?", (pid,)
        ).fetchone()[0]
        raw = conn.execute(
            "SELECT final_outline_json FROM v51_tasks WHERE project_id = ?", (pid,)
        ).fetchone()[0]

    assert title == "砌体成品保护", f"sections.title 未被清理: {title!r}"
    assert json.loads(raw)[0]["children"][0]["title"] == "施工组织设计", f"outline JSON 未被清理: {raw!r}"
    print("STARTUP HOOK OK")
finally:
    shutil.rmtree(_TMP, ignore_errors=True)
