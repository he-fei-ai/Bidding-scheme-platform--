"""解析提取模块（文件导入/import）增强与缺陷修复回归测试 — 2026-10-05

覆盖本次专项的增强项与 BUG 修复，全部为「可复现缺陷 → 修复后有断言」的对照式用例：

1. 解析能力单真相（TD3）：路由白名单 == 服务层 SUPPORTED_EXTENSIONS，且每个放行
   扩展名都能落到真实提取分支（历史上因两份手工维护的真相导致 .wps/图片入口封死）
2. B2 file_role 非法值归一化：非法值不再污染 DB，项目仍能有显式 main
3. G3 re-extract 任务表断链：重提取写入 file_parse_tasks 终态
4. B4 re-extract 空文本提示丢失：与 save_upload 同口径
5. B5 删除主文件 0 main 态：自动补位（与 set_file_role 的 422 保护口径一致）
6. B6 clear_project_texts 数据链断裂：DB 侧一并重置为 pending
7. G1 图片 OCR 配置护栏：OCR_ENGINE=none / 超过 OCR_MAX_FILE_SIZE_MB 时按配置跳过
8. B8 编码异常：UTF-8 BOM 与 UTF-16LE 文本正确解码，不留 \\ufeff / \\x00
9. 格式误判：扩展名与真实内容不一致（docx 内容命名 .pdf）按真实内容解析
10. TD2 关键词取文本去重后行为不变（get_boq_file_text 等四个公开函数）

遗留问题收口（2026-10-06）：
11. L1 .ppt 的 PowerPoint COM 自动化：开关 PPT_COM_ENABLED 与超时 PPT_COM_TIMEOUT
12. L2 Celery 任务 tasks.file.parse_and_extract 接入正式派发入口（异步重解析端点）
13. L3 file_parse_tasks 保留条数策略（prune_parse_tasks + beat 定期裁剪）
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
from app.routers import files as files_router  # noqa: E402
from app.services import files as files_mod  # noqa: E402


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def project_id(client):
    resp = client.post("/api/v1/projects", json={
        "name": "解析模块增强回归项目",
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


def _parse_task_rows(project_id: int, filename: str | None = None):
    from app.db import get_db

    sql = "SELECT filename, status FROM file_parse_tasks WHERE project_id = ?"
    args: list = [project_id]
    if filename:
        sql += " AND filename = ?"
        args.append(filename)
    with get_db(readonly=True) as conn:
        return conn.execute(sql, args).fetchall()


def _file_row(file_id: int):
    from app.db import get_db

    with get_db(readonly=True) as conn:
        return conn.execute(
            "SELECT id, filename, file_role, status, message, extracted_text_path "
            "FROM uploaded_files WHERE id = ?",
            (file_id,),
        ).fetchone()


class TestSingleSourceOfTruthForExtensions:
    """TD3：路由白名单与服务层解析能力必须同源"""

    def test_route_whitelist_equals_service_supported(self):
        assert files_router.ALLOWED_EXTENSIONS == set(files_mod.SUPPORTED_EXTENSIONS)
        # 关键回归：历史上服务层早已支持、路由层未放行导致功能入口封死
        for ext in (".wps", ".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".webp"):
            assert ext in files_router.ALLOWED_EXTENSIONS, ext

    def test_every_allowed_ext_has_real_extractor(self, tmp_path):
        """每个放行扩展名都必须落到真实提取分支，而不是落到 raise 分支"""
        # .ppt 在 Windows 上走 win32com 拉起 PowerPoint（COM 自动化）：会真的启动
        # PowerPoint 进程并留下 RPC 连接，导致解释器退出时抛 0x800706ba，
        # 因此这里只用「分支可达性」断言（见下方单独用例），不实际执行。
        skip_exec = {".ppt"} if sys.platform == "win32" else set()
        for ext in sorted(files_mod.SUPPORTED_EXTENSIONS):
            if ext in skip_exec:
                continue
            p = tmp_path / f"probe{ext}"
            p.write_bytes(b"probe")
            try:
                files_mod._do_extract_text(str(p), ext)
            except files_mod.UnsupportedFileFormatError as e:
                pytest.fail(f"放行扩展名 {ext} 却没有对应提取器: {e}")
            except Exception:
                # ImportError / 解析失败（探针不是真文件）都属正常，只要不是
                # 「不支持的文件格式」即说明有对应分支
                pass

    def test_ppt_branch_reachable(self, tmp_path, monkeypatch):
        """.ppt 走 win32com 拉起 PowerPoint；这里伪装非 win32 平台以避开 COM，
        断言分支确实存在（返回降级提示而不是「不支持的文件格式」）。"""
        monkeypatch.setattr(sys, "platform", "linux")
        p = tmp_path / "probe.ppt"
        p.write_bytes(b"probe")
        out = files_mod._do_extract_text(str(p), ".ppt")
        assert "无法自动提取" in out

    def test_unsupported_ext_raises(self, tmp_path):
        p = tmp_path / "x.exe"
        p.write_bytes(b"MZ")
        with pytest.raises(files_mod.UnsupportedFileFormatError):
            files_mod._do_extract_text(str(p), ".exe")


class TestFileRoleNormalization:
    """B2：非法 file_role 不得污染 DB"""

    def test_normalize_file_role_values(self):
        assert files_mod.normalize_file_role("main") == "main"
        assert files_mod.normalize_file_role("  MAIN ") == "main"
        assert files_mod.normalize_file_role("Supporting") == "supporting"
        # 非法值 → 回退自动检测（不抛错、不改接口形状）
        assert files_mod.normalize_file_role("主文件", "招标文件.pdf") == "main"
        assert files_mod.normalize_file_role("xyz", "工程量清单.xlsx") == "supporting"
        # 空值 → 自动检测（与修复前一致）
        assert files_mod.normalize_file_role("", "招标文件.pdf") == "main"

    def test_upload_with_illegal_role_still_yields_explicit_main(self, client, project_id):
        """非法 file_role 上传后，项目必须有显式 main（否则 _pick_main_file 静默回退）"""
        resp = _upload(client, project_id, "招标文件.docx",
                       _make_docx_bytes("投标人须知正文"), file_role="MAIN")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["file_role"] == "main", data

        row = _file_row(data["id"])
        assert row["file_role"] == "main"

    def test_upload_supporting_role_respected(self, client, project_id):
        resp = _upload(client, project_id, "招标附件.docx",
                       _make_docx_bytes("附件说明"), file_role="supporting")
        assert resp.status_code == 200, resp.text
        assert resp.json()["file_role"] == "supporting"


class TestReExtractTaskAndMessage:
    """G3 / B4：重提取的任务行与空文本提示"""

    def test_re_extract_writes_parse_task_terminal_state(self, client, project_id):
        content = _make_docx_bytes("重提取任务行检查正文")
        up = _upload(client, project_id, "重提取.docx", content)
        file_id = up.json()["id"]

        before = len(_parse_task_rows(project_id, "重提取.docx"))
        resp = client.post(f"/api/v1/projects/{project_id}/files/{file_id}/re-extract")
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "success"

        after = _parse_task_rows(project_id, "重提取.docx")
        assert len(after) == before + 1, "重提取必须新建任务行"
        assert all(r["status"] == "completed" for r in after), [dict(r) for r in after]

    def test_re_extract_keeps_empty_text_warning(self, client, project_id):
        """空白文本：上传提示「未提取到文本」，重提取后提示不得消失"""
        up = _upload(client, project_id, "空白.txt", b"   \n\t \n")
        assert up.status_code == 200, up.text
        data = up.json()
        assert data["status"] == "success"
        assert "未提取到文本" in data["message"], data

        file_id = data["id"]
        re_resp = client.post(f"/api/v1/projects/{project_id}/files/{file_id}/re-extract")
        assert re_resp.status_code == 200, re_resp.text
        re_data = re_resp.json()
        assert re_data["status"] == "success"
        assert "未提取到文本" in re_data["message"], re_data

        assert "未提取到文本" in _file_row(file_id)["message"]


class TestMainFileInvariantOnDelete:
    """B5：删除主文件后自动补位"""

    def test_delete_main_promotes_next_file(self, client, project_id):
        main_up = _upload(client, project_id, "招标文件.docx", _make_docx_bytes("主文件正文"))
        sup_up = _upload(client, project_id, "工程量清单.txt", "土方开挖 1200 立方米".encode("gb18030"))
        main_id = main_up.json()["id"]
        sup_id = sup_up.json()["id"]
        assert main_up.json()["file_role"] == "main"
        assert sup_up.json()["file_role"] == "supporting"

        del_resp = client.delete(f"/api/v1/projects/{project_id}/files/{main_id}")
        assert del_resp.status_code == 200, del_resp.text
        body = del_resp.json()
        assert body["promoted_file_id"] == sup_id, body

        assert _file_row(sup_id)["file_role"] == "main"

    def test_delete_last_file_leaves_no_main(self, client, project_id):
        up = _upload(client, project_id, "唯一文件.docx", _make_docx_bytes("正文"))
        file_id = up.json()["id"]
        body = client.delete(f"/api/v1/projects/{project_id}/files/{file_id}").json()
        assert body["promoted_file_id"] is None


class TestClearProjectTextsChain:
    """B6：清空文本必须同步重置 DB 记录"""

    def test_clear_project_texts_resets_db_rows(self, client, project_id):
        up = _upload(client, project_id, "待清空.docx", _make_docx_bytes("正文内容"))
        file_id = up.json()["id"]
        assert _file_row(file_id)["status"] == "success"

        files_mod.clear_project_texts(project_id)

        row = _file_row(file_id)
        assert row["status"] == "pending", dict(row)
        assert not row["extracted_text_path"], dict(row)


class TestImageOcrConfigGuardrails:
    """G1：OCR_* 配置必须真正生效"""

    def test_engine_none_skips_ocr(self, tmp_path, monkeypatch):
        monkeypatch.setattr(files_mod, "OCR_ENGINE", "none")
        p = tmp_path / "scan.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        assert files_mod._extract_image_ocr(str(p)) == ""

    def test_oversized_image_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(files_mod, "OCR_ENGINE", "paddleocr")
        monkeypatch.setattr(files_mod, "OCR_MAX_FILE_SIZE_MB", 0)  # 任何文件都超限
        p = tmp_path / "big.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        assert files_mod._extract_image_ocr(str(p)) == ""

    def test_ocr_semaphore_bounded_by_config(self):
        assert files_mod._ocr_semaphore._value == max(1, files_mod.OCR_MAX_CONCURRENT)


class TestTextEncodingRobustness:
    """B8：BOM / UTF-16 文本解码"""

    def test_utf8_bom_stripped(self, tmp_path):
        p = tmp_path / "bom.txt"
        p.write_bytes(b"\xef\xbb\xbf" + "第一章 项目概况".encode("utf-8"))
        text = files_mod._read_text_auto(str(p))
        assert text == "第一章 项目概况"
        assert not text.startswith("\ufeff")

    def test_utf16le_decoded(self, tmp_path):
        p = tmp_path / "u16.txt"
        p.write_bytes(b"\xff\xfe" + "投标人须知：资质要求".encode("utf-16-le"))
        text = files_mod._read_text_auto(str(p))
        assert "投标人须知" in text
        assert "\x00" not in text

    def test_gb18030_still_works(self, tmp_path):
        p = tmp_path / "gb.txt"
        p.write_bytes("工程量清单：土方开挖".encode("gb18030"))
        assert "土方开挖" in files_mod._read_text_auto(str(p))


class TestFormatMisjudge:
    """格式误判：扩展名与真实内容不一致"""

    def test_docx_content_with_pdf_extension(self, client, project_id):
        """docx 正文被命名成 .pdf —— 修复前走 PDF 分支得到空文本，修复后按真实内容解析"""
        content = _make_docx_bytes("投标人须知：本项目不接受联合体投标")
        up = _upload(client, project_id, "招标文件.pdf", content)
        assert up.status_code == 200, up.text
        data = up.json()
        assert data["status"] == "success", data
        # DB 侧 file_type 仍按用户声明的扩展名（如实反映上传内容）
        assert data["file_type"] == "pdf"

        text = client.get(f"/api/v1/projects/{project_id}/files/{data['id']}/text").json()["text"]
        assert "不接受联合体投标" in text, text[:200]

    def test_pdf_content_with_docx_extension(self, client, project_id):
        pdf_bytes = (
            b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[]/Count 0>>endobj\n"
            b"trailer<</Root 1 0 R/Size 3>>\n%%EOF"
        )
        up = _upload(client, project_id, "扫描件.docx", pdf_bytes)
        assert up.status_code == 200, up.text
        # 无正文可提取（0 页 PDF），但必须如实给出空文本提示而非「解析失败」
        assert up.json()["status"] == "success"
        assert "未提取到文本" in up.json()["message"]

    def test_sniff_extension_helper(self, tmp_path):
        pdf = tmp_path / "a.bin"
        pdf.write_bytes(b"%PDF-1.4 ...")
        assert files_mod._sniff_extension(str(pdf)) == ".pdf"

        docx = tmp_path / "b.bin"
        docx.write_bytes(_make_docx_bytes("正文"))
        assert files_mod._sniff_extension(str(docx)) == ".docx"

        ole = tmp_path / "c.bin"
        ole.write_bytes(b"\xd0\xcf\x11\xe0fake")  # OLE2：无法区分 doc/xls/ppt/wps
        assert files_mod._sniff_extension(str(ole)) == ""

        unknown = tmp_path / "d.bin"
        unknown.write_bytes(b"plain text")
        assert files_mod._sniff_extension(str(unknown)) == ""


class TestKeywordFileTextAfterDedupe:
    """TD2：四个关键词取文本函数去重后行为不变"""
    def test_boq_and_work_interface_lookup(self, client, project_id):
        _upload(client, project_id, "工程量清单.txt", "土方开挖 1200 立方米".encode("gb18030"))
        _upload(client, project_id, "工作界面说明.txt", "界面划分：土建与安装".encode("utf-8"))

        assert "土方开挖" in files_mod.get_boq_file_text(project_id)
        assert "界面划分" in files_mod.get_work_interface_file_text(project_id)
        # 不存在的分类返回空串而非抛错
        assert files_mod.get_construction_method_file_text(project_id) == ""
        assert files_mod.get_facts_file_text(project_id) == ""


class TestAsyncParseTaskConsistency:
    """G7：Celery 异步解析链路（tasks.file.parse_and_extract）与主链路口径一致"""

    def _run_task(self, file_path: str, project_id: int, file_id: int):
        from app.tasks import parse_and_extract_file

        # 绑定任务：run() 会自动注入 self，不需要构造 Celery 请求上下文
        return parse_and_extract_file.run(file_path, project_id, file_id)

    def test_async_parse_writes_task_row(self, client, project_id):
        up = _upload(client, project_id, "异步解析.docx", _make_docx_bytes("异步链路正文"))
        file_id = up.json()["id"]

        from app.db import get_db

        with get_db(readonly=True) as conn:
            row = conn.execute(
                "SELECT path FROM uploaded_files WHERE id = ?", (file_id,)
            ).fetchone()

        before = len(_parse_task_rows(project_id, "异步解析.docx"))
        self._run_task(row["path"], project_id, file_id)

        rows = _parse_task_rows(project_id, "异步解析.docx")
        assert len(rows) == before + 1, [dict(r) for r in rows]
        assert all(r["status"] == "completed" for r in rows), [dict(r) for r in rows]

    def test_async_parse_empty_text_message(self, client, project_id):
        up = _upload(client, project_id, "异步空白.txt", b"  \n ")
        file_id = up.json()["id"]

        from app.db import get_db

        with get_db(readonly=True) as conn:
            row = conn.execute(
                "SELECT path FROM uploaded_files WHERE id = ?", (file_id,)
            ).fetchone()

        self._run_task(row["path"], project_id, file_id)
        assert "未提取到文本" in (_file_row(file_id)["message"] or ""), dict(_file_row(file_id))


class TestEndToEndImportWorkflow:
    """端到端：上传 → 列表 → 合并文本 → 状态 → 角色 → 重提取 → 删除"""

    def test_full_workflow(self, client, project_id):
        # 1) 批量上传：主招标文件 + GBK 清单 + 图片扫描件
        main_up = _upload(client, project_id, "招标文件.docx",
                          _make_docx_bytes("第一章 项目概况\n投标人须具备市政一级资质"))
        assert main_up.status_code == 200, main_up.text
        main_id = main_up.json()["id"]
        assert main_up.json()["file_role"] == "main"

        boq_up = _upload(client, project_id, "工程量清单.txt",
                         "土方开挖 1200 立方米\nC30 混凝土 80 立方米".encode("gb18030"))
        assert boq_up.status_code == 200, boq_up.text
        boq_id = boq_up.json()["id"]
        assert boq_up.json()["file_role"] == "supporting"

        png = bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000d47414d4d00e2c5be8d0000000a49444154789c6360000002000151"
            "4f4e4e0000000049454e44ae426082"
        )
        img_up = _upload(client, project_id, "扫描页.png", png)
        assert img_up.status_code == 200, img_up.text
        assert img_up.json()["file_role"] == "supporting"

        # 2) 列表字段与前端 FileInfo 契约一致
        listed = client.get(f"/api/v1/projects/{project_id}/files").json()
        assert len(listed) == 3
        for f in listed:
            for key in ("id", "filename", "file_type", "file_role", "status", "message", "created_at"):
                assert key in f, f

        # 3) 合并文本：主文件全文 + 辅助摘要（下游 AI 数据源）
        merged = client.get(f"/api/v1/projects/{project_id}/files/text").json()["text"]
        assert "市政一级资质" in merged
        assert "土方开挖" in merged

        # 4) 项目级解析状态聚合（前端进度条数据源）
        status = client.get(f"/api/v1/projects/{project_id}/tasks/files/status").json()
        assert status["total_tasks"] >= 3
        assert status["status"] in ("completed", "completed_with_errors", "processing")

        # 5) 角色切换：main 全项目互斥
        role = client.put(f"/api/v1/projects/{project_id}/files/{boq_id}/role",
                          json={"file_role": "main"}).json()
        assert role["file_role"] == "main"
        assert role["demoted_file_ids"] == [main_id], role
        assert _file_row(main_id)["file_role"] == "supporting"

        # 6) 重新提取：状态与任务行同步
        re_resp = client.post(f"/api/v1/projects/{project_id}/files/{boq_id}/re-extract")
        assert re_resp.status_code == 200, re_resp.text
        assert re_resp.json()["status"] == "success"
        assert re_resp.json()["text_length"] > 0

        # 7) 删除当前主文件 → 自动补位，项目不会进入 0 main 态
        del_resp = client.delete(f"/api/v1/projects/{project_id}/files/{boq_id}")
        assert del_resp.status_code == 200, del_resp.text
        assert del_resp.json()["promoted_file_id"] == main_id

        remaining = client.get(f"/api/v1/projects/{project_id}/files").json()
        mains = [f for f in remaining if f["file_role"] == "main"]
        assert len(mains) == 1, remaining


class TestPptComGuardrails:
    """遗留 L1：PowerPoint COM 自动化的开关与超时护栏"""

    def test_disabled_skips_com(self, tmp_path, monkeypatch):
        def _must_not_be_called(_p):  # pragma: no cover - 仅防回归
            raise AssertionError("PPT_COM_ENABLED=false 时不得调用 COM")

        monkeypatch.setattr(files_mod, "PPT_COM_ENABLED", False)
        monkeypatch.setattr(files_mod, "_ppt_via_powerpoint_com", _must_not_be_called)

        p = tmp_path / "a.ppt"
        p.write_bytes(b"\xd0\xcf\x11\xe0fake")
        assert "无法自动提取" in files_mod._extract_ppt(str(p))

    def test_timeout_falls_back_to_hint(self, tmp_path, monkeypatch):
        """COM 卡死超过 PPT_COM_TIMEOUT 时必须放弃等待并返回降级提示，不能挂住请求"""
        monkeypatch.setattr(files_mod, "PPT_COM_ENABLED", True)
        monkeypatch.setattr(files_mod, "PPT_COM_TIMEOUT", 0)
        # BUG-FIX 2026-10-08：强制可用性探测为 True，使无 Office 的 CI 也能走到
        # 真实的超时包装逻辑（否则注册表探测会直接跳过 COM，无法验证超时护栏）
        monkeypatch.setattr(files_mod, "_ppt_com_available", lambda: True)
        # 子进程模式：跨进程 monkeypatch 不生效，改用环境变量让子进程挂死 60s
        monkeypatch.setenv("PPT_COM_INJECT_HANG", "1")

        p = tmp_path / "b.ppt"
        p.write_bytes(b"\xd0\xcf\x11\xe0fake")

        import time as _t

        _t0 = _t.monotonic()
        out = files_mod._extract_ppt(str(p))
        _elapsed = _t.monotonic() - _t0
        assert "无法自动提取" in out
        assert _elapsed < 3, f"超时护栏失效，等待了 {_elapsed:.1f}s"

    def test_success_path_returns_text(self, tmp_path, monkeypatch):
        monkeypatch.setattr(files_mod, "PPT_COM_ENABLED", True)
        monkeypatch.setattr(files_mod, "_ppt_com_available", lambda: True)
        # 子进程模式：通过环境变量把结果注入给子进程（跨进程 monkeypatch 不生效）
        monkeypatch.setenv("PPT_COM_INJECT_RESULT", "幻灯片正文")
        p = tmp_path / "c.ppt"
        p.write_bytes(b"\xd0\xcf\x11\xe0fake")
        assert files_mod._extract_ppt(str(p)) == "幻灯片正文"


class TestAsyncReparseEndpoint:
    """遗留 L2：Celery 解析任务从此有了真实派发入口"""

    def test_async_reparse_dispatches(self, client, project_id):
        up = _upload(client, project_id, "异步入口.docx", _make_docx_bytes("异步派发正文"))
        file_id = up.json()["id"]
        before = len(_parse_task_rows(project_id, "异步入口.docx"))

        resp = client.post(f"/api/v1/projects/{project_id}/files/{file_id}/async-reparse")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["file_id"] == file_id
        assert data["dispatched_via"] in ("celery", "inline"), data
        # 无论走队列还是进程内兜底，都必须真的执行了解析（文本长度 > 0）
        if data["dispatched_via"] == "inline":
            assert data["detail"]["result"]["text_length"] > 0, data

        # 状态端点要能看到这次任务
        assert len(_parse_task_rows(project_id, "异步入口.docx")) >= before + 1

    def test_async_reparse_missing_file_404(self, client, project_id):
        resp = client.post(f"/api/v1/projects/{project_id}/files/999999/async-reparse")
        assert resp.status_code == 404, resp.text


class TestParseTaskRetention:
    """遗留 L3：file_parse_tasks 的保留条数策略"""

    def test_prune_keeps_latest_and_preserves_inflight(self, project_id, monkeypatch):
        monkeypatch.setattr(files_mod, "PARSE_TASK_RETENTION", 3)
        for i in range(6):
            files_mod._create_parse_task(project_id, f"f{i}.txt")
            files_mod._update_parse_task(project_id, "completed", 100, filename=f"f{i}.txt")
        # 在途任务：裁剪时必须保留，否则正在解析的任务会从状态端点上消失
        files_mod._create_parse_task(project_id, "inflight.txt")

        files_mod.prune_parse_tasks(project_id)

        rows = _parse_task_rows(project_id)
        names = [r["filename"] for r in rows]
        assert len(rows) == 3, names
        assert "inflight.txt" in names, names
        assert "f5" + ".txt" in names and "f4.txt" in names, names
        assert "f0.txt" not in names, names
        inflight = [r for r in rows if r["filename"] == "inflight.txt"]
        assert inflight[0]["status"] == "processing"

    def test_retention_zero_disables_prune(self, project_id, monkeypatch):
        monkeypatch.setattr(files_mod, "PARSE_TASK_RETENTION", 0)
        for i in range(4):
            files_mod._create_parse_task(project_id, f"keep{i}.txt")
            files_mod._update_parse_task(project_id, "completed", 100, filename=f"keep{i}.txt")
        assert files_mod.prune_parse_tasks(project_id) == 0
        assert len(_parse_task_rows(project_id)) >= 4
