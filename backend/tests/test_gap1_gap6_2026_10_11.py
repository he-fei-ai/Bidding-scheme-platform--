"""Gap-1 / Gap-6 针对性测试 2026-10-11

钉住：
  Gap-1  DELETE /projects/{id}/files/texts 端点（清空提取文本）
  Gap-6  _do_extract_text 直连分支失败后回退 OLE2 降级链
"""
import io
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


API = "/api/v1"


# ─────────────────────────────────────────────────────────────────────────
# Gap-1: DELETE /projects/{id}/files/texts 端点级测试
# ─────────────────────────────────────────────────────────────────────────


@pytest.fixture()
def client():
    from app.main import app
    return TestClient(app)


def _make_project(client, name="Gap1 测试") -> int:
    r = client.post(f"{API}/projects", json={"name": name, "industry": "市政工程"})
    assert r.status_code in (200, 201), f"创建项目失败: {r.status_code} {r.text}"
    return r.json()["id"]


class TestGap1ClearTextsEndpoint:
    """DELETE /projects/{id}/files/texts —— 清空提取文本并重置状态"""

    def test_clear_texts_returns_cleared(self, client):
        """基本功能：上传后清空 → 返回 {status:'cleared', files_reset>0}"""
        pid = _make_project(client)

        # 上传文件（正确端点：router prefix /projects/{id}/files + POST 根）
        r = client.post(
            f"{API}/projects/{pid}/files",
            files={"file": ("test.txt", io.BytesIO(b"hello world"), "text/plain")},
        )
        assert r.status_code == 200, f"上传失败: {r.status_code} {r.text}"

        # 直接把 status 改成 success
        from app.db import get_db
        with get_db() as conn:
            conn.execute(
                "UPDATE uploaded_files SET status = 'success', extracted_text_path = ? WHERE project_id = ?",
                ("/tmp/fake.txt.txt", pid),
            )

        # 执行清空
        r = client.delete(f"{API}/projects/{pid}/files/texts")
        assert r.status_code == 200, f"清空失败: {r.status_code} {r.text}"
        body = r.json()
        assert body["status"] == "cleared"
        assert body["files_reset"] >= 1
        assert "tasks_cleared" in body

        # 验证：文件状态已重置为 pending
        r = client.get(f"{API}/projects/{pid}/files")
        files = r.json()
        pending_count = sum(1 for f in files if f["status"] == "pending")
        assert pending_count >= 1, f"清空后文件状态应为 pending"

    def test_clear_texts_unknown_project_returns_proper_error(self, client):
        """清空不存在的项目 —— 不应返回 200"""
        r = client.delete(f"{API}/projects/9999999/files/texts")
        assert r.status_code != 200, f"不存在项目不应成功: {r.status_code}"

    def test_clear_texts_access_enforcement(self, client, monkeypatch):
        """基本可达性测试：端点存在且不 crash"""
        pid = _make_project(client)
        r = client.delete(f"{API}/projects/{pid}/files/texts")
        assert r.status_code == 200, f"清空端点可达: {r.status_code}"


class TestGap1ServiceLayerEnhancements:
    """clear_project_texts 服务层返回值 + file_parse_tasks 清理"""

    def test_clear_project_texts_returns_dict(self, tmp_path):
        """改造后的 clear_project_texts 应返回 dict"""
        from app.db import get_db, init_db
        init_db()
        from app.services import files as files_mod
        from app.config import PROJECTS_DATA_DIR

        pid = 99992
        # 先建 project（避免外键约束失败）
        with get_db() as conn:
            # projects 表有 created_by 但 SQLite 允许 NULL（默认）
            conn.execute(
                "INSERT INTO projects (id, name, industry, created_by) VALUES (?, ?, ?, NULL)",
                (pid, "Gap1 服务层测试", "市政工程"),
            )

        proj_dir = PROJECTS_DATA_DIR / str(pid)
        proj_dir.mkdir(parents=True, exist_ok=True)
        texts_dir = proj_dir / "texts"
        texts_dir.mkdir(exist_ok=True)
        (texts_dir / "dummy.txt").write_text("stale content")

        with get_db() as conn:
            conn.execute(
                "INSERT INTO uploaded_files (project_id, filename, path, file_type, file_role, status, message, extracted_text_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (pid, "test.docx", str(proj_dir / "test.docx"), "docx", "main", "success", "", str(texts_dir / "test.txt")),
            )
            # file_parse_tasks 是项目级追踪表（无 file_id 列）
            conn.execute(
                "INSERT INTO file_parse_tasks (project_id, filename, file_type, status, error_message) VALUES (?, ?, ?, ?, ?)",
                (pid, "test.docx", "docx", "completed", "ok"),
            )
            conn.execute(
                "INSERT INTO file_parse_tasks (project_id, filename, file_type, status, error_message) VALUES (?, ?, ?, ?, ?)",
                (pid, "test.docx", "docx", "failed", "parse error"),
            )
            conn.execute(
                "INSERT INTO file_parse_tasks (project_id, filename, file_type, status, error_message) VALUES (?, ?, ?, ?, ?)",
                (pid, "test.docx", "docx", "processing", ""),
            )

        result = files_mod.clear_project_texts(pid)

        assert isinstance(result, dict), f"应返回 dict，实际 {type(result)}"
        assert result["status"] == "cleared"
        assert result["files_reset"] >= 1
        assert result["tasks_cleared"] >= 2  # completed + failed

        # processing 行应保留
        with get_db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM file_parse_tasks WHERE project_id = ? AND status = 'processing'",
                (pid,),
            ).fetchone()
            processing_count = row[0]
            assert processing_count == 1, f"processing 行应保留，实际 {processing_count}"

        # texts/ 目录已重建（空）
        assert texts_dir.exists()
        assert list(texts_dir.iterdir()) == []


# ─────────────────────────────────────────────────────────────────────────
# Gap-6: _do_extract_text 直连失败后回退 OLE2 降级链
# ─────────────────────────────────────────────────────────────────────────


class TestGap6DirectFallbackToOLE2Chain:
    """_do_extract_text 直连分支返回提示串/空串 + 文件头确认为 OLE2 → 回退降级链"""

    def test_doc_direct_falls_back_to_chain_when_olefile_empty(self, tmp_path, monkeypatch):
        """.doc 直连 _extract_doc 返回 EXTRACT_DOC_NOTICE，OLE2 链中 _extract_xls 能产真实文本"""
        from app.services import files as files_mod

        ole_header = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
        test_file = tmp_path / "fake_doc.doc"
        test_file.write_bytes(ole_header + b"\x00" * 100)

        monkeypatch.setattr(files_mod, "_extract_doc", lambda _p: files_mod.EXTRACT_DOC_NOTICE)
        monkeypatch.setattr(files_mod, "_extract_xls", lambda _p: "真实 Excel 文本来自 OLE2 降级链")

        result = files_mod._do_extract_text(str(test_file), ".doc", ".doc")

        assert result == "真实 Excel 文本来自 OLE2 降级链", (
            f"预期回退降级链成功，实际: {result!r}"
        )

    def test_ppt_direct_falls_back_to_chain(self, tmp_path, monkeypatch):
        """.ppt 直连 _extract_ppt 返回提示串 → 回退降级链"""
        from app.services import files as files_mod

        ole_header = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
        test_file = tmp_path / "fake_ppt.ppt"
        test_file.write_bytes(ole_header + b"\x00" * 100)

        monkeypatch.setattr(files_mod, "_extract_ppt", lambda _p: files_mod._PPT_FALLBACK_HINT)
        monkeypatch.setattr(files_mod, "_extract_doc", lambda _p: "来自 Word 流的真实内容")

        result = files_mod._do_extract_text(str(test_file), ".ppt", ".ppt")
        assert result == "来自 Word 流的真实内容", f"预期回退降级链成功，实际: {result!r}"

    def test_no_fallback_for_non_ole_files(self, tmp_path, monkeypatch):
        """非 OLE2 文件（纯 txt 改后缀 .doc）不应回退降级链"""
        from app.services import files as files_mod

        test_file = tmp_path / "not_ole.doc"
        test_file.write_bytes(b"this is just a text file content")

        monkeypatch.setattr(files_mod, "_extract_doc", lambda _p: files_mod.EXTRACT_DOC_NOTICE)

        result = files_mod._do_extract_text(str(test_file), ".doc", ".doc")
        assert result == files_mod.EXTRACT_DOC_NOTICE, (
            f"非 OLE 文件不应回退降级链，实际: {result!r}"
        )

    def test_no_fallback_when_direct_succeeds(self, tmp_path, monkeypatch):
        """直连提取成功（非空非提示串）→ 不回退降级链"""
        from app.services import files as files_mod

        ole_header = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
        test_file = tmp_path / "ole_doc.doc"
        test_file.write_bytes(ole_header + b"\x00" * 100)

        doc_call_count = [0]
        def tracking_extract_doc(_p):
            doc_call_count[0] += 1
            return "直连成功的真实内容"

        monkeypatch.setattr(files_mod, "_extract_doc", tracking_extract_doc)

        result = files_mod._do_extract_text(str(test_file), ".doc", ".doc")
        assert result == "直连成功的真实内容"
        assert doc_call_count[0] == 1, f"直连只应被调一次，实际 {doc_call_count[0]} 次"
