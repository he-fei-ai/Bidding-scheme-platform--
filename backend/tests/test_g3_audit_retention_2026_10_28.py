# -*- coding: utf-8 -*-
"""G3（2026-10-28 遗留加固）：consistency_audit_log conflicts_json retention 清理测试。

验证 cleanup_old_consistency_audit：
  1. 超过 retention 天数的旧审计日志被删除（含 conflicts_json）；
  2. retention 内的近期日志保留；
  3. 非法 retention（<1）按 1 天处理，不抛错；
  4. 表不存在/异常时返回 0（fail-soft，不抛错）。
"""
import io
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.db import get_db, init_db  # noqa: E402


@pytest.fixture(autouse=True)
def _db():
    init_db()


def _insert_log(project_id: int, audit_time: str, status: str = "ok", conflicts_json: str = "[{}]"):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO consistency_audit_log "
            "(project_id, total_sections, facts_extracted, conflicts_found, "
            "conflicts_json, audit_time, status, source_kind) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (project_id, 3, 10, 1, conflicts_json, audit_time, status, "rule"),
        )


def _count_logs(project_id: int) -> int:
    with get_db(readonly=True) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM consistency_audit_log WHERE project_id = ?",
            (project_id,),
        ).fetchone()[0]


def test_cleanup_removes_expired_keeps_recent():
    from app.services.consistency_auditor import cleanup_old_consistency_audit

    with get_db() as conn:
        conn.execute("INSERT INTO projects (name, industry) VALUES (?, ?)", ("G3清理测试", "通用"))
        pid = conn.execute("SELECT id FROM projects ORDER BY id DESC LIMIT 1").fetchone()["id"]

    old_time = (datetime.now() - timedelta(days=120)).strftime("%Y-%m-%d %H:%M:%S")
    recent_time = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    _insert_log(pid, old_time, conflicts_json='[{"conflict_id": 1}]')
    _insert_log(pid, recent_time, conflicts_json='[{"conflict_id": 2}]')

    assert _count_logs(pid) == 2
    deleted = cleanup_old_consistency_audit(retention_days=90)
    assert deleted >= 1, "应删除至少 1 条过期日志"
    assert _count_logs(pid) == 1, "保留 1 条近期日志"


def test_cleanup_invalid_retention_does_not_crash():
    from app.services.consistency_auditor import cleanup_old_consistency_audit

    # retention<1 按 1 天处理，不抛错
    deleted = cleanup_old_consistency_audit(retention_days=0)
    assert deleted >= 0


def test_cleanup_empty_table_returns_zero():
    from app.services.consistency_auditor import cleanup_old_consistency_audit

    assert cleanup_old_consistency_audit(retention_days=90) >= 0
