"""解析提取模块 · 角色切换与解析状态聚合回归测试（2026-10-04）

覆盖三项遗留增强：
1. P3#2 guess_file_role：主文件正向关键词、图片扫描件默认 supporting
2. P4 set_file_role：手动切换主/辅 → 合并文本主文件切换、缓存失效、非法角色 400、缺文件 404
3. P3#1 GET /tasks/files/status：项目级聚合（processing/recent_* 计数 + latest 向后兼容）
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
from app.services.files import guess_file_role  # noqa: E402


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def project_id(client):
    resp = client.post("/api/v1/projects", json={
        "name": "角色与状态测试项目",
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


# ---------- 1. guess_file_role 增强 ----------

class TestGuessFileRoleEnhanced:
    def test_main_positive_keywords(self):
        # 主文件正向关键词 → main
        assert guess_file_role("采购公告.pdf") == "main"
        assert guess_file_role("技术需求书.docx") == "main"
        assert guess_file_role("投标人须知.pdf") == "main"
        assert guess_file_role("tender_document.docx") == "main"

    def test_supporting_still_wins(self):
        # 辅助关键词优先级最高（即便含其它字）
        assert guess_file_role("工程量清单.xlsx") == "supporting"
        assert guess_file_role("投标报价表.pdf") == "supporting"

    def test_image_without_keyword_is_supporting(self):
        # 图片扫描件且无关键词 → supporting（避免空内容图片顶替主文件）
        assert guess_file_role("现场照片.png") == "supporting"
        assert guess_file_role("IMG_2026.jpg") == "supporting"

    def test_non_image_no_keyword_defaults_main(self):
        # 向后兼容：非图片、无关键词的杂件仍默认 main
        assert guess_file_role("营业执照.pdf") == "main"
        assert guess_file_role("其他文件.docx") == "main"

    def test_backward_compatible_existing(self):
        # 原 test_files.py 用例仍成立
        assert guess_file_role("招标文件.pdf") == "main"
        assert guess_file_role("招标公告.docx") == "main"


# ---------- 2. set_file_role 路由与服务 ----------

class TestSetFileRole:
    def test_toggle_role_flips_main_and_merged_text(self, client, project_id):
        a_content = _make_docx_bytes("主文件A：评标办法与技术要求")
        b_content = _make_docx_bytes("清单项B：土方开挖 1200 立方米")

        ra = _upload(client, project_id, "招标文件A.docx", a_content)
        rb = _upload(client, project_id, "工程量清单B.docx", b_content)
        assert ra.status_code == 200 and rb.status_code == 200
        ida, idb = ra.json()["id"], rb.json()["id"]
        # A 命中"招标"→main；B 命中"清单"→supporting
        assert ra.json()["file_role"] == "main"
        assert rb.json()["file_role"] == "supporting"

        # 手动把 B 设为主文件，A 设辅助
        put_b = client.put(
            f"/api/v1/projects/{project_id}/files/{idb}/role",
            json={"file_role": "main"},
        )
        assert put_b.status_code == 200, put_b.text
        assert put_b.json()["file_role"] == "main"
        client.put(
            f"/api/v1/projects/{project_id}/files/{ida}/role",
            json={"file_role": "supporting"},
        )

        listed = {f["id"]: f for f in client.get(f"/api/v1/projects/{project_id}/files").json()}
        assert listed[idb]["file_role"] == "main"
        assert listed[ida]["file_role"] == "supporting"

        # 合并文本应以新的主文件 B 开头（缓存已因角色变更失效）
        merged = client.get(f"/api/v1/projects/{project_id}/files/text").json()["text"]
        assert "土方开挖" in merged
        assert merged.index("土方开挖") < merged.index("评标办法")

    def test_illegal_role_400(self, client, project_id):
        r = _upload(client, project_id, "招标文件X.docx", _make_docx_bytes("内容"))
        fid = r.json()["id"]
        bad = client.put(
            f"/api/v1/projects/{project_id}/files/{fid}/role",
            json={"file_role": "admin"},
        )
        assert bad.status_code == 400
        assert "非法" in bad.json()["detail"]

    def test_missing_file_404(self, client, project_id):
        resp = client.put(
            f"/api/v1/projects/{project_id}/files/999999/role",
            json={"file_role": "main"},
        )
        assert resp.status_code == 404


# ---------- 3. 解析状态聚合 ----------

class TestParseStatusAggregate:
    def test_idle_when_no_task(self, client):
        resp = client.post("/api/v1/projects", json={"name": "空任务项目", "industry": "公共建筑"})
        pid = resp.json()["id"]
        status = client.get(f"/api/v1/projects/{pid}/tasks/files/status").json()
        assert status["status"] == "idle"

    def test_aggregate_after_upload(self, client, project_id):
        _upload(client, project_id, "招标文件.docx", _make_docx_bytes("聚合状态验证"))
        resp = client.get(f"/api/v1/projects/{project_id}/tasks/files/status")
        assert resp.status_code == 200
        data = resp.json()
        # 新聚合结构：含计数与 latest（向后兼容）
        assert data["status"] in ("completed", "processing", "failed", "completed_with_errors")
        assert data["total_tasks"] >= 1
        assert "processing" in data and "recent_completed" in data
        assert data["latest"] is not None and data["latest"]["filename"] == "招标文件.docx"
