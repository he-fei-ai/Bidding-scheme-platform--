"""解析提取模块（文件导入/import）2026-10-06 专项批次的回归测试

覆盖本轮识别的缺陷修复（每项均为「缺陷可复现 → 修复后有断言」）：

R1  re-extract 与 save_upload 口径不一致：老格式降级提示串在「重新提取」入口
    仍被写成 status='success'（H1 中毒链路的漏网之鱼）
R2  格式误判补齐：OLE2 复合文档魔数嗅探（真 .doc 改名 .docx 可提取）+
    legacy 扩展名降级链（.doc→.xls→.ppt→.wps，声明扩展名优先补试）+
    伪装护栏（非 OLE 头的文件不进链、不多引擎试错）
R3  async-reparse 任务行重复：端点派发前已建任务行，任务内不得再建一行
R4  HTML 编码统一：UTF-16LE BOM / GB18030 编码的 HTML 不再乱码或抛异常

跨模块链路与前端交互契约（组件级）在
frontend/src/pages/FileUploadPage.dom.test.tsx 与 frontend/src/utils.upload.test.ts 覆盖。
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
from app.services import files as files_mod  # noqa: E402

OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def project_id(client):
    resp = client.post("/api/v1/projects", json={
        "name": "解析专项20261006项目",
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


def _file_row(file_id: int) -> dict:
    from app.db import get_db

    with get_db(readonly=True) as conn:
        row = conn.execute(
            "SELECT * FROM uploaded_files WHERE id = ?", (file_id,)
        ).fetchone()
    return dict(row)


def _parse_task_rows(project_id: int, filename: str | None = None):
    from app.db import get_db

    sql = "SELECT filename, status FROM file_parse_tasks WHERE project_id = ?"
    args: list = [project_id]
    if filename:
        sql += " AND filename = ?"
        args.append(filename)
    with get_db(readonly=True) as conn:
        return [dict(r) for r in conn.execute(sql, tuple(args)).fetchall()]


# ============================================================================
# R1：re-extract 与 save_upload 的降级提示串口径一致
# ============================================================================
class TestReExtractNoticeParity:

    def test_re_extract_notice_marks_failed(self, client, project_id, monkeypatch):
        """提取返回降级提示串时，re_extract_text 必须判 failed（而不是旧口径 success）"""
        # 先真实上传一个可解析的 docx，拿到 DB 行
        resp = _upload(client, project_id, "招标R1.docx",
                       _make_docx_bytes("投标人须知"))
        assert resp.status_code == 200, resp.text
        fid = resp.json()["id"]

        monkeypatch.setattr(
            files_mod, "extract_text_from_path",
            lambda p: files_mod.EXTRACT_DOC_NOTICE,
        )
        result = files_mod.re_extract_text(project_id, fid)
        assert result["status"] == "failed", result
        assert "无法自动提取" in result["message"], result
        row = _file_row(fid)
        assert row["status"] == "failed", row
        # 任务行也必须如实落 failed 终态（G3 链路口径）
        rows = _parse_task_rows(project_id, "招标R1.docx")
        assert any(r["status"] == "failed" for r in rows), rows

    def test_upload_then_re_extract_same_verdict(self, client, project_id, monkeypatch):
        """同一份「提取失败」的 .doc：上传入口与重提取入口的状态判定必须一致。

        缺陷形态（修复前）：save_upload 判 failed，re_extract 判 success ——
        用户对同一个失败文件点「重新提取」，文件反而被标成成功并可能被
        _pick_main_file 选作主文本，把一句提示语喂给下游 AI。
        """
        # 伪装 .doc：非 OLE 头，olefile 打开失败；屏蔽 antiword/catdoc/方法2
        monkeypatch.setattr(files_mod.shutil, "which", lambda _name: None)
        resp = _upload(client, project_id, "技术要求R1.doc", b"not a real ole doc")
        assert resp.status_code == 200, resp.text
        fid = resp.json()["id"]
        upload_row = _file_row(fid)

        result = files_mod.re_extract_text(project_id, fid)
        assert result["status"] == upload_row["status"] == "failed", (result, upload_row)
        assert files_mod.is_extraction_notice(result["message"]), result


# ============================================================================
# R2：OLE2 魔数嗅探 + 降级链 + 伪装护栏
# ============================================================================
class TestOle2SniffAndChain:

    def test_sniff_ole2_magic(self, tmp_path):
        p = tmp_path / "real.doc"
        p.write_bytes(OLE2_MAGIC + b"\x00" * 64)
        assert files_mod._sniff_extension(str(p)) == ".doc"

    def test_fake_docx_with_ole_header_recovers_text(self, monkeypatch, tmp_path):
        """真 .doc 改名 .docx：extract_text_from_path 嗅探判 .doc 并按链提取成功"""
        p = tmp_path / "伪装.docx"
        p.write_bytes(OLE2_MAGIC + b"\x00" * 64)
        # 屏蔽磁盘缓存与真实解析，模拟 olefile .doc 分支成功
        monkeypatch.setattr(files_mod, "_sniff_extension", lambda fp: ".doc")
        monkeypatch.setattr(files_mod, "_get_file_hash", lambda fp: "h")
        monkeypatch.setattr(files_mod, "_extract_doc", lambda fp: "招标正文恢复")
        out = files_mod.extract_text_from_path(str(p))
        assert out == "招标正文恢复"

    def test_chain_order_respects_declared_ext(self, monkeypatch, tmp_path):
        """声明 .xls 的 OLE 文件：.doc 失败后必须先试 .xls，不得先触发 .ppt(COM)"""
        p = tmp_path / "清单.xls"
        calls: list[str] = []

        def _mk(label, ret):
            def _h(fp):
                calls.append(label)
                return ret
            return _h

        monkeypatch.setattr(files_mod, "_extract_doc", _mk("doc", ""))
        monkeypatch.setattr(files_mod, "_extract_xls", _mk("xls", "工作表: 1|2"))
        monkeypatch.setattr(files_mod, "_extract_ppt", _mk("ppt", files_mod.EXTRACT_PPT_NOTICE))
        monkeypatch.setattr(files_mod, "_extract_wps", _mk("wps", ""))
        out = files_mod._extract_legacy_ole_chain(str(p), ".xls")
        assert out == "工作表: 1|2"
        assert calls == ["doc", "xls"], calls  # ppt/wps 未被无谓触发

    def test_all_branches_fail_returns_notice(self, monkeypatch, tmp_path):
        """全链失败：返回降级提示串（保持 H1 判 failed 的口径），不是空串"""
        p = tmp_path / "坏文件.doc"
        monkeypatch.setattr(files_mod, "_extract_doc", lambda fp: files_mod.EXTRACT_DOC_NOTICE)
        monkeypatch.setattr(files_mod, "_extract_xls", lambda fp: "")
        monkeypatch.setattr(files_mod, "_extract_ppt", lambda fp: files_mod.EXTRACT_PPT_NOTICE)
        monkeypatch.setattr(files_mod, "_extract_wps", lambda fp: files_mod.EXTRACT_WPS_NOTICE)
        out = files_mod._extract_legacy_ole_chain(str(p), ".doc")
        assert files_mod.is_extraction_notice(out), out

    def test_non_ole_file_with_wrong_ext_skips_chain(self, monkeypatch, tmp_path):
        """伪装护栏：非 OLE 头的文件不进链（不触发多引擎试错），保持原单分支行为"""
        p = tmp_path / "probe.ppt"
        p.write_bytes(b"probe")
        called: list[str] = []
        monkeypatch.setattr(files_mod, "_extract_doc", lambda fp: called.append("doc") or "")
        monkeypatch.setattr(files_mod, "_extract_xls", lambda fp: called.append("xls") or "")
        monkeypatch.setattr(files_mod, "_extract_wps", lambda fp: called.append("wps") or "")
        out = files_mod._do_extract_text(str(p), ".ppt", ".ppt")
        assert called == [], called
        assert out == files_mod.EXTRACT_PPT_NOTICE

    def test_ole_header_disagree_declared_skips_chain(self, tmp_path):
        """OLE 头但 ext 与声明扩展名和嗅探结果都不一致（如嗅探 .doc 而按 .wps 调用）：
        走原单分支，不进链（由 extract_text_from_path 统一以嗅探结果进链）"""
        p = tmp_path / "头不一致.wps"
        p.write_bytes(OLE2_MAGIC + b"\x00" * 64)
        # .wps 分支：olefile 打开这个「只有魔数的假 OLE」失败 → docx 兜底失败 → 提示串
        out = files_mod._do_extract_text(str(p), ".wps", ".docx")
        assert out == files_mod.EXTRACT_WPS_NOTICE

    def test_legacy_branch_still_reachable_for_single_ext(self, monkeypatch, tmp_path):
        """声明 .wps 的 OLE 文件可进链，且 .wps 分支文本能被取回"""
        p = tmp_path / "文档.wps"
        p.write_bytes(OLE2_MAGIC + b"\x00" * 64)
        monkeypatch.setattr(files_mod, "_extract_doc", lambda fp: "")
        monkeypatch.setattr(files_mod, "_extract_xls", lambda fp: "")
        monkeypatch.setattr(files_mod, "_extract_ppt", lambda fp: "")
        monkeypatch.setattr(files_mod, "_extract_wps", lambda fp: "WPS 正文恢复")
        out = files_mod._do_extract_text(str(p), ".wps", ".wps")
        assert out == "WPS 正文恢复"


# ============================================================================
# R3：async-reparse 派发参数（任务行不重复创建）
# ============================================================================
class TestAsyncReparseTaskRow:

    def test_dispatch_passes_create_task_row_false(self, client, project_id, monkeypatch):
        captured: dict = {}

        def _fake_dispatch(name, args=None, kwargs=None, **kw):
            captured["name"] = name
            captured["args"] = args
            captured["kwargs"] = kwargs
            return {"status": "queued", "dispatched_via": "celery", "celery_task_id": "t-1"}

        monkeypatch.setattr("app.celery_app.dispatch_task", _fake_dispatch)

        resp = _upload(client, project_id, "异步R3.docx", _make_docx_bytes("R3 正文"))
        fid = resp.json()["id"]
        # 上传入口（save_upload）已建一行，作为基线
        rows_after_upload = _parse_task_rows(project_id, "异步R3.docx")
        assert len(rows_after_upload) == 1, rows_after_upload

        r = client.post(f"/api/v1/projects/{project_id}/files/{fid}/async-reparse")
        assert r.status_code == 200, r.text
        assert captured["name"] == "tasks.file.parse_and_extract"
        assert captured["kwargs"] == {"create_task_row": False}, captured
        # 端点自建且仅建一行（fake dispatch 不入队、不跑任务，create_task_row=False
        # 的「任务内不再建行」保证由 kwargs 断言覆盖，另见下方 inline 直调用例）
        rows = _parse_task_rows(project_id, "异步R3.docx")
        assert len(rows) == len(rows_after_upload) + 1, rows
        assert any(row["status"] == "processing" for row in rows), rows

    def test_task_inline_does_not_create_row_when_false(self, project_id, monkeypatch):
        """核心保证：任务以 create_task_row=False 就地执行时，绝不新建任务行。

        缺陷形态（修复前）：端点派发前已建行，任务内又无条件 _create_parse_task，
        同一次异步重解析在 file_parse_tasks 留下两行（重复计数 + 孤儿行）。
        """
        # 屏蔽真实解析，只验证任务行的创建与否
        monkeypatch.setattr(
            files_mod, "extract_text_from_path", lambda p: "正文恢复"
        )
        before = len(_parse_task_rows(project_id, "inline不建行.docx"))
        # 走进程内直调（force_inline），create_task_row=False
        from app.celery_app import dispatch_task

        dispatch_task(
            "tasks.file.parse_and_extract",
            ("/nonexistent/inline不建行.docx", project_id, 0),
            {"create_task_row": False},
            force_inline=True,
        )
        after = len(_parse_task_rows(project_id, "inline不建行.docx"))
        assert after == before, (before, after)

    def test_task_signature_default_keeps_inline_row_creation(self):
        """Celery 任务默认 create_task_row=True：旧调用方（直调/重试）行为不变（向后兼容）"""
        import inspect

        from app.tasks import parse_and_extract_file

        sig = inspect.signature(parse_and_extract_file.run)
        assert sig.parameters["create_task_row"].default is True


# ============================================================================
# R4：HTML 编码统一走 _read_text_auto
# ============================================================================
class TestHtmlEncoding:

    def test_html_utf16le_bom(self, tmp_path):
        p = tmp_path / "公告.html"
        html = "<html><head><title>采购公告</title></head><body><p>投标人须知前附表</p></body></html>"
        p.write_bytes(b"\xff\xfe" + html.encode("utf-16-le"))
        out = files_mod._extract_html(str(p))
        assert "投标人须知前附表" in out
        assert "\x00" not in out and "\ufeff" not in out

    def test_html_gb18030(self, tmp_path):
        p = tmp_path / "公告gb.html"
        html = "<html><body><div>评标办法：综合评分法</div></body></html>"
        p.write_bytes(html.encode("gb18030"))
        out = files_mod._extract_html(str(p))
        assert "综合评分法" in out

    def test_html_plain_utf8_unchanged(self, tmp_path):
        p = tmp_path / "公告utf8.html"
        p.write_text(
            "<html><body><span>工程量清单&lt;表格&gt;</span></body></html>",
            encoding="utf-8",
        )
        out = files_mod._extract_html(str(p))
        assert "工程量清单<表格>" in out
