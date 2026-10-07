"""验证 check_document 优化后语义保持

通过包内 import，让 config / db / services 走真实环境。
"""
import os
import sys
import shutil
import tempfile
import sqlite3

sys.path.insert(0, r"J:\编程\招投标方案平台\backend")

# 临时 DB 目录
tmp = tempfile.mkdtemp()
os.environ["BID_ASSISTANT_DATA_DIR"] = tmp

# 在 app 真正启动前 patch DATABASE_PATH
import app.config as _cfg_mod
from pathlib import Path
_cfg_mod.DATABASE_PATH = Path(tmp) / "test.sqlite3"
_cfg_mod.PROJECTS_DATA_DIR = Path(tmp) / "projects"
_cfg_mod.KNOWLEDGE_DATA_DIR = Path(tmp) / "knowledge"
_cfg_mod.REJECTION_REPORTS_DIR = Path(tmp) / "rejection_reports"
_cfg_mod.DUPLICATE_REPORTS_DIR = Path(tmp) / "duplicate_reports"

import app.db as _db
import app.services.sections as _sec
import app.services.export as _exp

# 初始化 schema
with _db.get_db() as conn:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY,
            name TEXT
        );
        CREATE TABLE IF NOT EXISTS sections (
            id INTEGER PRIMARY KEY,
            project_id INTEGER,
            parent_id INTEGER,
            title TEXT,
            level INTEGER,
            sort_order INTEGER,
            section_type TEXT,
            scoring_point_id INTEGER,
            status TEXT,
            content TEXT,
            target_words INTEGER,
            word_count INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS analysis (
            project_id INTEGER PRIMARY KEY,
            summary_json TEXT,
            scoring_points_json TEXT,
            global_facts_json TEXT
        );
    """)
    conn.execute("INSERT INTO projects (id, name) VALUES (1, 'Test')")
    test_sections = [
        (1, 1, None, "第一章 概述", 1, 1, "text", None, "active", "本项目是测试", 0, 0),
        (2, 1, 1, "1.1 背景", 2, 1, "text", None, "active", "背景介绍" * 100, 0, 0),
        (3, 1, 1, "1.2 范围", 2, 2, "text", None, "active", "范围" * 50, 0, 0),
        (4, 1, 2, "1.1.1 详细说明", 3, 1, "text", None, "active", "", 0, 0),
        (5, 1, 3, "1.2.1 边界", 3, 1, "text", None, "completed", "短", 0, 0),
    ]
    for s in test_sections:
        conn.execute(
            "INSERT INTO sections (id, project_id, parent_id, title, level, sort_order, "
            "section_type, scoring_point_id, status, content, target_words, word_count) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            s,
        )

# 1. 旧实现：Python 累加
sections = _sec.list_sections(1)
old_total = sum(len(s.content or "") for s in sections)
print(f"Old (Python sum):  total_chars = {old_total}")

# 2. 新实现：SQL SUM
with _db.get_db() as conn:
    row = conn.execute(
        "SELECT COALESCE(SUM(LENGTH(content)), 0) AS total_chars "
        "FROM sections WHERE project_id = ?",
        (1,),
    ).fetchone()
    new_total = int(row["total_chars"] or 0)
print(f"New (SQL SUM):     total_chars = {new_total}")

if old_total != new_total:
    print(f"❌ FAIL: 不一致 old={old_total} new={new_total}")
    sys.exit(1)
print("✅ PASS: total_chars 数值完全一致")

# 3. 完整 check_document 验证
res = _exp.check_document(1)
print(f"\ncheck_document:")
print(f"  total_chars        = {res.total_chars}")
print(f"  total_sections     = {res.total_sections}")
print(f"  empty_sections     = {len(res.empty_sections)}")
print(f"  short_sections     = {len(res.short_sections)}")
print(f"  empty_title_secs   = {len(res.empty_title_sections)}")
print(f"  passed             = {res.passed}")

assert res.total_chars == old_total, f"check_document total_chars 不一致: {res.total_chars} != {old_total}"
assert res.total_sections == 5, f"total_sections 应为 5: {res.total_sections}"
assert len(res.empty_sections) == 1, f"empty_sections 应为 1 (id=4 content=''): {len(res.empty_sections)}"
assert len(res.short_sections) == 2, f"short_sections 应为 2 (id=1 '本项目是测试' + id=5 '短'): {len(res.short_sections)}"
print("\n✅ check_document 返回结构与原实现一致")

# 4. 性能对比（小型数据集，看量级即可）
import time

# 旧实现（在 check_document 中已经在做循环累加了，这里我们模拟 O(n) Python 累加）
t0 = time.perf_counter()
for _ in range(100):
    _ = sum(len(s.content or "") for s in sections)
old_t = (time.perf_counter() - t0) * 1000

# 新实现
t0 = time.perf_counter()
for _ in range(100):
    with _db.get_db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(LENGTH(content)), 0) FROM sections WHERE project_id = ?",
            (1,),
        ).fetchone()
        _ = int(row[0] or 0)
new_t = (time.perf_counter() - t0) * 1000
print(f"\n性能（5 章节 × 100 次重复）: 旧={old_t:.1f}ms  新={new_t:.1f}ms")

# 清理
shutil.rmtree(tmp, ignore_errors=True)
print("\n✅ 全部验证通过")
