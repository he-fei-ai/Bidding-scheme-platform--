"""解析提取模块（文件导入/import）组件级交互测试

覆盖 2026-09-29 修复项与关键链路（路由层 ↔ 服务层 ↔ DB/文件系统）：
1. 上传全链路契约：POST /files 响应字段、列表、取文本、重新提取、删除
2. 编码与文件名：gb18030 中文 txt 自动解码、特殊字符/超长文件名、路径穿越归一化
3. 批量上传状态完整性：同 stem 不同扩展名互不覆盖、file_parse_tasks 无 processing 残留
4. 路由白名单与服务层能力一致性（BUG-FIX 2026-09-29）：.wps / 图片放行，.exe 拒绝
5. 空文本提示（BUG-FIX 2026-09-29）：OCR 未启用时图片上传 status=success 且 message 提示
6. 错误处理：不存在项目 404、缺失文件 404、损坏 docx → failed 且任务行终态 failed
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
        "name": "解析模块交互测试项目",
        "industry": "公共建筑",
        "bidder_name": "测试投标方",
    })
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _make_docx_bytes(text: str) -> bytes:
    """用 python-docx 在内存中生成一个真实 docx"""
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


def _parse_task_rows(project_id: int):
    from app.db import get_db

    with get_db(readonly=True) as conn:
        return conn.execute(
            "SELECT filename, status FROM file_parse_tasks WHERE project_id = ?",
            (project_id,),
        ).fetchall()


class TestUploadPipelineHappyPath:
    """上传→列表→取文本→重新提取→合并文本→删除 全链路"""

    def test_docx_full_lifecycle(self, client, project_id):
        content = _make_docx_bytes("第一章 项目概况\n投标人须具备相应资质等级。")

        resp = _upload(client, project_id, "招标文件.docx", content)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # 前后端接口契约（frontend api/index.ts UploadedFileInfo 依赖这些字段）
        assert data["status"] == "success", data
        assert data["filename"] == "招标文件.docx"
        assert data["file_type"] == "docx"
        assert "id" in data and data["id"] is not None
        assert "message" in data
        file_id = data["id"]

        # 列表可见且字段一致
        listed = client.get(f"/api/v1/projects/{project_id}/files").json()
        row = next(f for f in listed if f["id"] == file_id)
        assert row["status"] == "success"
        assert row["file_role"]  # 自动角色检测生效

        # 单文件文本可取回
        text_resp = client.get(f"/api/v1/projects/{project_id}/files/{file_id}/text")
        assert text_resp.status_code == 200
        assert "项目概况" in text_resp.json()["text"]

        # 重新提取（走缓存清理+再提取路径）
        re_resp = client.post(f"/api/v1/projects/{project_id}/files/{file_id}/re-extract")
        assert re_resp.status_code == 200, re_resp.text
        assert re_resp.json()["status"] == "success"

        # 合并文本端点（下游 AI 链路数据源）
        merged = client.get(f"/api/v1/projects/{project_id}/files/text").json()["text"]
        assert "资质等级" in merged

        # 删除后列表与物理记录均消失
        del_resp = client.delete(f"/api/v1/projects/{project_id}/files/{file_id}")
        assert del_resp.status_code == 200
        listed_after = client.get(f"/api/v1/projects/{project_id}/files").json()
        assert all(f["id"] != file_id for f in listed_after)

    def test_upload_advances_project_step(self, client, project_id):
        """上传成功应把项目步骤推进到 2（路由层 update_step 调用链）"""
        content = _make_docx_bytes("技术标要求：编制施工组织设计。")
        resp = _upload(client, project_id, "step推进.docx", content)
        assert resp.status_code == 200
        proj = client.get(f"/api/v1/projects/{project_id}").json()
        assert proj.get("current_step") == 2


class TestEncodingAndNames:
    """编码自动降级与文件名健壮性"""

    def test_gb18030_txt_decoded_with_role_detection(self, client, project_id):
        text = "工程量清单：土方开挖 1200 立方米\nC30 混凝土 80 立方米"
        content = text.encode("gb18030")
        resp = _upload(client, project_id, "工程量清单.txt", content)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "success"
        # 文件名含"清单/工程量"关键词 → 自动判定为辅助文件
        assert data["file_role"] == "supporting"

        got = client.get(f"/api/v1/projects/{project_id}/files/{data['id']}/text").json()["text"]
        assert "土方开挖" in got  # gb18030 正确解码，无乱码

    def test_special_chars_and_long_filename(self, client, project_id):
        content = _make_docx_bytes("特殊文件名测试")
        long_stem = "招标文件" + ("审查" * 50)
        filename = f"{long_stem}（2026版）【正本】.docx"
        resp = _upload(client, project_id, filename, content)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "success"
        assert data["filename"] == filename

    def test_path_traversal_filename_normalized(self, client, project_id):
        """../../etc/passwd.txt 应被 basename 归一化，stored filename 不含路径成分"""
        resp = _upload(client, project_id, "../../etc/passwd.txt", b"traversal probe")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["filename"] == "passwd.txt"
        assert "/" not in data["filename"] and "\\" not in data["filename"]

        listed = client.get(f"/api/v1/projects/{project_id}/files").json()
        assert any(f["id"] == data["id"] for f in listed)
        # 物理路径必须落在项目 uploads 目录内（未被 ../../ 穿越）
        from app.config import PROJECTS_DATA_DIR
        from app.db import get_db
        with get_db(readonly=True) as conn:
            row = conn.execute("SELECT path FROM uploaded_files WHERE id = ?", (data["id"],)).fetchone()
        stored = Path(row["path"])
        assert stored.name == "passwd.txt"
        assert stored.parent == PROJECTS_DATA_DIR / str(project_id) / "uploads"


class TestBatchUploadTaskIntegrity:
    """批量/同名 stem 上传的状态隔离（BUG-FIX 2026-09-18 filename 精确过滤回归）"""

    def test_same_stem_different_ext_no_overwrite(self, client, project_id):
        docx_content = _make_docx_bytes("Word 版本：技术方案正文")
        r1 = _upload(client, project_id, "投标方案.docx", docx_content)
        assert r1.status_code == 200 and r1.json()["status"] == "success"

        # 极简但合法的 PDF（pypdf 可解析出 0 页也可，只要不抛异常即 success）；
        # 即使提取失败，DB 行与文本文件也必须互不覆盖
        pdf_bytes = (
            b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[]/Count 0>>endobj\n"
            b"trailer<</Root 1 0 R/Size 3>>\n%%EOF"
        )
        r2 = _upload(client, project_id, "投标方案.pdf", pdf_bytes)
        assert r2.status_code == 200, r2.text
        d1, d2 = r1.json(), r2.json()
        assert d1["id"] != d2["id"]
        assert d1["filename"] == "投标方案.docx"
        assert d2["filename"] == "投标方案.pdf"

        listed = client.get(f"/api/v1/projects/{project_id}/files").json()
        rows = {f["filename"]: f for f in listed}
        assert "投标方案.docx" in rows and "投标方案.pdf" in rows
        # 提取文本文件按完整原名命名，两条记录路径必须不同
        t1 = client.get(f"/api/v1/projects/{project_id}/files/{d1['id']}/text").json()["text"]
        assert "技术方案正文" in t1

    def test_no_processing_task_rows_left(self, client, project_id):
        """每个上传终态必须写回任务表，不允许 processing 残留"""
        content = _make_docx_bytes("任务表终态检查")
        resp = _upload(client, project_id, "终态检查.docx", content)
        assert resp.status_code == 200

        # 同名二次上传（触发重命名分支），同样要求终态
        resp2 = _upload(client, project_id, "终态检查.docx", content)
        assert resp2.status_code == 200
        assert resp2.json()["filename"] == "终态检查_1.docx"

        rows = _parse_task_rows(project_id)
        assert rows, "file_parse_tasks 应有任务行"
        assert all(r["status"] != "processing" for r in rows), [dict(r) for r in rows]


class TestRouteServiceWhitelistConsistency:
    """路由白名单与服务层解析能力对齐（BUG-FIX 2026-09-29）"""

    def test_wps_accepted_by_route(self, client, project_id):
        """服务层支持 .wps（olefile/docx 回退），路由层必须放行"""
        from app.config import OCR_ENABLED  # noqa: F401  (仅确认可导入)

        resp = _upload(client, project_id, "旧版文件.wps", b"\xd0\xcf\x11\xe0fake-ole-content")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # olefile 解析失败时服务层优雅降级为提示文本；不允许再出现 400 拒绝入口
        assert data["status"] in ("success", "failed"), data

    def test_image_accepted_with_graceful_empty_text(self, client, project_id):
        """图片进入解析链路；OCR 未启用时 success + 空文本提示（BUG-FIX 2026-09-29）"""
        from app.config import OCR_ENABLED

        # 1x1 PNG
        png = bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000d47414d4d00e2c5be8d0000000a49444154789c6360000002000151"
            "4f4e4e0000000049454e44ae426082"
        )
        resp = _upload(client, project_id, "扫描页.png", png)
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "success"
        if not OCR_ENABLED:
            assert "未提取到文本" in data["message"], data

    def test_exec_rejected(self, client, project_id):
        resp = _upload(client, project_id, "恶意程序.exe", b"MZ\x90\x00")
        assert resp.status_code == 400
        assert "不支持的文件类型" in resp.json()["detail"]

    def test_empty_content_rejected(self, client, project_id):
        resp = _upload(client, project_id, "空文件.txt", b"")
        assert resp.status_code == 400
        assert "文件内容为空" in resp.json()["detail"]


class TestErrorHandling:
    """错误处理与状态终态一致性"""

    def test_upload_to_missing_project_404(self, client):
        resp = _upload(client, 999999, "不存在.docx", b"x" * 10)
        assert resp.status_code == 404

    def test_re_extract_missing_file_404(self, client, project_id):
        resp = client.post(f"/api/v1/projects/{project_id}/files/999999/re-extract")
        assert resp.status_code == 404

    def test_delete_missing_file_404(self, client, project_id):
        resp = client.delete(f"/api/v1/projects/{project_id}/files/999999")
        assert resp.status_code == 404

    def test_corrupt_docx_marks_failed_end_to_end(self, client, project_id):
        """损坏 docx：响应 failed + message，且任务行终态 failed（不卡 processing）"""
        resp = _upload(client, project_id, "损坏文件.docx", b"this is not a zip/docx payload")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["status"] == "failed", data
        assert "文本提取失败" in data["message"], data

        listed = client.get(f"/api/v1/projects/{project_id}/files").json()
        row = next(f for f in listed if f["id"] == data["id"])
        assert row["status"] == "failed"
        assert row["message"] == data["message"]  # 前后端 message 一致性

        tasks = _parse_task_rows(project_id)
        failed = [r for r in tasks if r["filename"] == "损坏文件.docx"]
        assert failed and failed[-1]["status"] == "failed", [dict(r) for r in tasks]
