"""PDF 导出降级链实测（2026-11-04 收尾 P3）。

本机环境实测（2026-11-04，Python 3.14 / Windows）：
    docx2pdf    未安装
    win32com    importable（但 Word COM 需本机安装 Office，PDF 转换链未使用）
    soffice     PATH 与常见安装路径均未找到
故真实转换用例按条件 skip，并在测试输出中如实反映；不得声称跑过真实转换。

本文件锁定以下「失败不静默、不返回损坏文件」语义：
1. 无任何可用转换器（LibreOffice 缺失；docx2pdf/Word COM 不在本实现链上）
   → ``PdfUnavailable``（中文可操作提示），路由层 501，DOCX 链路不受影响。
2. 转换器存在但执行失败（非零退出/无产物/超时）→ ``RuntimeError``，路由层 500
   并记录失败；既不返回旧 PDF 伪装成功，也不留下半写 PDF。
3. 本机若存在 soffice，则追加一条真实转换用例（当前环境 skip）。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.db import get_db, init_db
from app.security.auth import User
from app.services import pdf_export

OWNER = 100


def _new_project(name: str) -> int:
    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO projects (name, industry, created_by) VALUES (?, ?, ?)",
            (name, "通用", OWNER),
        )
        return conn.execute("SELECT id FROM projects ORDER BY id DESC LIMIT 1").fetchone()["id"]


def _insert_export_row(pid: int, file_path: str) -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO exports (project_id, filename, file_path, file_size, config_json, status) "
            "VALUES (?, '测试导出.docx', ?, 10, '{}', 'completed')",
            (pid, str(file_path)),
        )
        return cur.lastrowid


def _route_fixture(tmp_path, monkeypatch):
    import app.routers.export as er

    base = tmp_path / "data"
    base.mkdir(exist_ok=True)
    monkeypatch.setattr(er, "PROJECTS_DATA_DIR", base)
    pid = _new_project(f"PDF降级链-{tmp_path.name}")
    docx = base / str(pid) / "doc.docx"
    docx.parent.mkdir(parents=True, exist_ok=True)
    docx.write_bytes(b"PK fake docx")
    eid = _insert_export_row(pid, str(docx))
    return er, pid, eid, docx


# ──────────────────────────────────────────────────────────────────────
# 1. 全转换器不可用：明确错误码 + 中文可操作提示，不静默
# ──────────────────────────────────────────────────────────────────────


def test_all_converters_absent_raises_actionable_error(monkeypatch, tmp_path):
    monkeypatch.setattr(pdf_export.shutil, "which", lambda name: None)
    monkeypatch.setattr(pdf_export, "_SOFFICE_CANDIDATES", ())
    monkeypatch.delenv("PDF_SOFFICE_PATH", raising=False)
    monkeypatch.setattr(pdf_export, "_cached_soffice", None)
    monkeypatch.setattr(pdf_export, "_probe_done", False)

    # 现状实测：本实现没有 docx2pdf / Word COM 转换分支（见模块 docstring），
    # 因此「三者都不可用」等价于「LibreOffice 不可用」。
    with pytest.raises(pdf_export.PdfUnavailable) as ei:
        pdf_export.convert_docx_to_pdf(tmp_path / "a.docx")
    msg = str(ei.value)
    assert "LibreOffice" in msg
    assert "PDF_SOFFICE_PATH" in msg
    assert "DOCX 导出不受影响" in msg


def test_route_returns_501_with_visible_message_when_unavailable(tmp_path, monkeypatch):
    er, pid, eid, _docx = _route_fixture(tmp_path, monkeypatch)

    def _unavail(p, **kw):
        raise pdf_export.PdfUnavailable(
            "未检测到 LibreOffice（soffice）。请安装 LibreOffice 或设置环境变量 "
            "PDF_SOFFICE_PATH 指向 soffice 可执行文件后重试；DOCX 导出不受影响。"
        )

    monkeypatch.setattr(pdf_export, "convert_docx_to_pdf", _unavail)
    from app.routers.export import export_pdf

    with pytest.raises(HTTPException) as ei:
        asyncio.run(export_pdf(pid, eid, identity=User(OWNER, "o", "user")))
    assert ei.value.status_code == 501
    assert "PDF_SOFFICE_PATH" in ei.value.detail
    # 失败不得静默：终态位不存在任何 PDF，旧文件未被伪造
    assert not (_docx.parent / "doc.pdf").exists()


# ──────────────────────────────────────────────────────────────────────
# 2. 转换器存在但失败：500 且无损坏产物
# ──────────────────────────────────────────────────────────────────────


def test_route_returns_500_and_keeps_no_partial_pdf(tmp_path, monkeypatch):
    er, pid, eid, docx = _route_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(pdf_export, "find_soffice", lambda **kw: "soffice")

    def _fail(cmd, **kw):
        # 模拟 soffice 异常退出且不产出文件
        return SimpleNamespace(returncode=1, stdout=b"", stderr=b"conversion error")

    monkeypatch.setattr(pdf_export.subprocess, "run", _fail)
    from app.routers.export import export_pdf

    with pytest.raises(HTTPException) as ei:
        asyncio.run(export_pdf(pid, eid, identity=User(OWNER, "o", "user")))
    assert ei.value.status_code == 500
    assert "PDF 转换失败" in ei.value.detail
    assert not (docx.parent / "doc.pdf").exists(), "失败路径不得留下损坏/半写 PDF"
    assert not list(docx.parent.glob(".pdf_tmp_*")), "失败路径必须清理临时目录"
    # 失败必须留痕：exports 新增 failed 行（可追溯，不静默）
    with get_db(readonly=True) as conn:
        failed = conn.execute(
            "SELECT COUNT(*) AS c FROM exports WHERE project_id = ? AND status = 'failed'",
            (pid,),
        ).fetchone()["c"]
    assert failed >= 1


def test_conversion_timeout_is_explicit_not_silent(monkeypatch, tmp_path):
    docx = tmp_path / "slow.docx"
    docx.write_bytes(b"docx")
    monkeypatch.setattr(pdf_export, "find_soffice", lambda **kw: "soffice")

    def _timeout(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 1))

    monkeypatch.setattr(pdf_export.subprocess, "run", _timeout)
    with pytest.raises(RuntimeError) as ei:
        pdf_export.convert_docx_to_pdf(docx, timeout_seconds=1)
    assert "超时" in str(ei.value)
    assert not (tmp_path / "slow.pdf").exists()


# ──────────────────────────────────────────────────────────────────────
# 3. 本机有转换器时的真实转换（当前环境无 -> skip）
# ──────────────────────────────────────────────────────────────────────

_SOFFICE = (
    shutil.which("soffice")
    or shutil.which("libreoffice")
    or next((p for p in pdf_export._SOFFICE_CANDIDATES if Path(p).exists()), None)
)


@pytest.mark.skipif(not _SOFFICE, reason="本机未安装 LibreOffice/soffice，真实转换用例不执行")
def test_real_docx_to_pdf_conversion(tmp_path, monkeypatch):
    """仅在检测到真实 soffice 时运行；用于证明「不是只有 mock」。"""
    from docx import Document

    monkeypatch.setattr(pdf_export, "_cached_soffice", _SOFFICE)
    monkeypatch.setattr(pdf_export, "_probe_done", True)
    monkeypatch.setattr(pdf_export, "_probe_last_ts", 0.0)

    docx_path = tmp_path / "real.docx"
    doc = Document()
    doc.add_paragraph("招投标方案平台 PDF 真实转换用例")
    doc.save(docx_path)

    out = pdf_export.convert_docx_to_pdf(docx_path, timeout_seconds=120)
    assert out.exists()
    assert out.read_bytes()[:5] == b"%PDF-", "产物必须是真实 PDF 魔数，不是空/损坏文件"
    assert out.stat().st_size > 1024
    # 清理产物，避免污染 tmp 目录审计
    os.remove(out)
