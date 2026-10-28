# -*- coding: utf-8 -*-
"""G8（2026-10-28 后续建议#4）：上传内容哈希去重测试。

验证 save_upload 的 content-hash 去重：
  1. 同项目、同内容二次上传 → status='duplicate'，filename 不变，不新增文件行/任务行；
  2. 同项目、同内容但**不同文件名**上传 → 同样 duplicate（按内容而非文件名去重）；
  3. 同项目、同文件名但**不同内容**上传 → 仍重命名 _1（防覆盖契约保留）；
  4. 不同项目、同内容上传 → 不去重（按项目隔离）；
  5. 去重命中后 file_parse_tasks 无 processing 残留。
"""
import io
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def project_id(client):
    resp = client.post("/api/v1/projects", json={
        "name": "G8去重测试项目",
        "industry": "公共建筑",
        "bidder_name": "测试投标方",
    })
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _make_docx_bytes(text: str) -> bytes:
    from docx import Document

    doc = Document()
    for line in text.splitlines():
        doc.add_paragraph(line)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _upload(client, project_id, filename, content: bytes, file_role: str = ""):
    return client.post(
        f"/api/v1/projects/{project_id}/files",
        files={"file": (filename, io.BytesIO(content), "application/octet-stream")},
        data={"file_role": file_role} if file_role else None,
    )


def _count_uploaded_rows(project_id: int) -> int:
    from app.db import get_db

    with get_db(readonly=True) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM uploaded_files WHERE project_id = ?",
            (project_id,),
        ).fetchone()[0]


def _count_task_rows(project_id: int) -> int:
    from app.db import get_db

    with get_db(readonly=True) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM file_parse_tasks WHERE project_id = ?",
            (project_id,),
        ).fetchone()[0]


class TestG8UploadContentDedup:
    """G8：内容哈希去重"""

    def test_same_content_same_name_duplicates(self, client, project_id):
        content = _make_docx_bytes("G8去重正文-同一份")
        r1 = _upload(client, project_id, "招标文件.docx", content)
        assert r1.status_code == 200
        assert r1.json().get("status") == "success", r1.json()

        r2 = _upload(client, project_id, "招标文件.docx", content)
        assert r2.status_code == 200
        assert r2.json().get("status") == "duplicate", r2.json()
        assert r2.json().get("filename") == "招标文件.docx"
        assert r2.json().get("duplicate_of") == r1.json().get("id")
        # 不新增文件行/任务行
        assert _count_uploaded_rows(project_id) == 1
        assert _count_task_rows(project_id) == 1

    def test_same_content_different_name_duplicates(self, client, project_id):
        content = _make_docx_bytes("G8去重正文-换名")
        r1 = _upload(client, project_id, "原文件.docx", content)
        assert r1.json().get("status") == "success", r1.json()

        r2 = _upload(client, project_id, "拷贝副本.docx", content)
        assert r2.status_code == 200
        assert r2.json().get("status") == "duplicate", r2.json()
        assert _count_uploaded_rows(project_id) == 1

    def test_same_name_different_content_still_renames(self, client, project_id):
        r1 = _upload(client, project_id, "方案.docx", _make_docx_bytes("内容A"))
        assert r1.json().get("status") == "success", r1.json()

        r2 = _upload(client, project_id, "方案.docx", _make_docx_bytes("内容B不同"))
        assert r2.status_code == 200
        assert r2.json().get("status") != "duplicate", r2.json()
        assert r2.json().get("filename") == "方案_1.docx", r2.json()
        assert _count_uploaded_rows(project_id) == 2

    def test_different_project_not_deduped(self, client, project_id):
        content = _make_docx_bytes("G8跨项目不重")
        r1 = _upload(client, project_id, "共享.docx", content)
        assert r1.json().get("status") == "success", r1.json()

        # 新项目上传相同内容，不应被旧项目去重
        resp = client.post("/api/v1/projects", json={
            "name": "G8另一个项目",
            "industry": "公共建筑",
            "bidder_name": "乙",
        })
        pid2 = resp.json()["id"]
        r2 = _upload(client, pid2, "共享.docx", content)
        assert r2.status_code == 200
        assert r2.json().get("status") == "success", r2.json()
        assert _count_uploaded_rows(pid2) == 1

    def test_no_processing_residue_after_duplicate(self, client, project_id):
        content = _make_docx_bytes("G8终态检查")
        r1 = _upload(client, project_id, "终态.docx", content)
        assert r1.json().get("status") == "success", r1.json()
        r2 = _upload(client, project_id, "终态.docx", content)
        assert r2.json().get("status") == "duplicate", r2.json()

        from app.db import get_db
        with get_db(readonly=True) as conn:
            rows = conn.execute(
                "SELECT status FROM file_parse_tasks WHERE project_id = ?",
                (project_id,),
            ).fetchall()
        assert rows
        assert all(r["status"] != "processing" for r in rows), [dict(r) for r in rows]
