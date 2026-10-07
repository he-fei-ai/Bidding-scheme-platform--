from app.db import get_db, init_db
from app.services.consistency_auditor import ConsistencyAuditor


def _np(name):
    init_db()
    with get_db() as c:
        c.execute("INSERT INTO projects (name, industry, created_by) VALUES (?, ?, ?)", (name, "通用", 1))
        return c.execute("SELECT id FROM projects ORDER BY id DESC LIMIT 1").fetchone()["id"]


def _sec(pid, title, content, order):
    with get_db() as c:
        c.execute(
            "INSERT INTO sections (project_id, title, level, sort_order, content, status) "
            "VALUES (?, ?, 1, ?, ?, 'completed')",
            (pid, title, order, content),
        )


pid = _np("probe-role-merge")
# 同一章节列出不同岗位人数：属正常表述，不应判为「人员数量」冲突
_sec(pid, "人员配备", "本项目配备项目经理1人、技术人员20人、管理人员5人。", 0)
facts = ConsistencyAuditor().extract_facts_from_text(
    "本项目配备项目经理1人、技术人员20人、管理人员5人。"
)
print("EXTRACTED", [(f.name, f.value) for f in facts])

# 两章节：项目经理 5 人 vs 8 人（同岗位差异，真冲突）
pid2 = _np("probe-same-role")
_sec(pid2, "A", "项目经理：5人。", 0)
_sec(pid2, "B", "项目经理：8人。", 1)
cons = [c for c in ConsistencyAuditor().detect_conflicts(pid2) if c.fact_name == "人员数量"]
print("SAME_ROLE_CONFLICTS", [(c.fact_name, c.values) for c in cons])


def test_probe():
    multi = _np("probe-multi-role-conflict")
    _sec(multi, "人员配备", "本项目配备项目经理1人、技术人员20人、管理人员5人。", 0)
    _sec(multi, "团队", "项目经理1人，技术人员20人，管理人员5人。", 1)
    cs = [c for c in ConsistencyAuditor().detect_conflicts(multi) if c.fact_name == "人员数量"]
    print("MULTI_ROLE_CONFLICTS", [(c.fact_name, c.values, c.sources) for c in cs])
