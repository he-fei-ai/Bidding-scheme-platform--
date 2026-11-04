"""PDF 转换并发竞态加固回归（2026-11-03）。

来源（一致性校核与导出文档模块_第九轮深度加固_2026-10-27.md §7-1）：
同一 export_id 并发 /pdf 时，原实现 `--outdir <源目录>` 让两个 soffice 直写
同一 `{name}.pdf`（半写被读走 / 转换失败摧毁旧产物）。加固为「同卷独立临时
目录 + 原子 os.replace 收编」。

运行：
    cd backend
    python -m pytest tests/test_pdf_convert_race_hardening_2026_11_03.py -q
"""
from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import pdf_export


def _setup_docx(tmp_path: Path, name: str = "a.docx") -> Path:
    docx = tmp_path / name
    docx.write_bytes(b"docx")
    return docx


def _fake_run_writes_outdir(payload: bytes, log: dict):
    """模拟 LibreOffice：把产物写进 cmd 的 --outdir（而非终态位）。"""

    def _run(cmd, **kw):
        log["cmds"] = log.get("cmds", []) + [[str(c) for c in cmd]]
        outdir = Path(cmd[cmd.index("--outdir") + 1])
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / Path(cmd[-1]).with_suffix(".pdf").name).write_bytes(payload)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    return _run


class TestPdfConvertRaceHardening:
    def test_second_racer_reuses_fresh_target_without_overwrite(self, monkeypatch, tmp_path):
        """并发收口：后完成者发现目标已「明确新于源」→ 复用对方产物，不覆盖。"""
        docx = _setup_docx(tmp_path)
        monkeypatch.setattr(pdf_export, "find_soffice", lambda **kw: "soffice")
        log: dict = {}

        # 第一请求：正常转换（真实 os.replace 收编路径）
        monkeypatch.setattr(
            pdf_export.subprocess, "run", _fake_run_writes_outdir(b"winner pdf", log)
        )
        out1 = pdf_export.convert_docx_to_pdf(docx)
        assert out1.read_bytes() == b"winner pdf"
        winner_mtime = out1.stat().st_mtime

        # 第二请求（并发窗口后到）：其临时产物内容不同（b"loser pdf"），
        # 但终态位已被 winner 收编且明确新于源 → 必须复用 winner，不覆盖
        monkeypatch.setattr(
            pdf_export.subprocess, "run", _fake_run_writes_outdir(b"loser pdf", log)
        )
        out2 = pdf_export.convert_docx_to_pdf(docx)
        assert out2 == out1
        assert out2.read_bytes() == b"winner pdf", "并发窗口内后完成者不得覆盖先到者产物"
        assert out2.stat().st_mtime == winner_mtime, "复用语义：mtime 不应被重写"

    def test_failed_conversion_preserves_existing_target(self, monkeypatch, tmp_path):
        """转换失败（无产物）→ RuntimeError，且既有旧 PDF 字节保持原样。"""
        docx = _setup_docx(tmp_path)
        target = tmp_path / "a.pdf"
        target.write_bytes(b"old but valid pdf")
        # 旧 PDF 相对陈旧（否则入口缓存命中直接复用，走不到失败路径）
        _newest = docx.stat().st_mtime + 5
        os.utime(docx, (_newest, _newest))
        monkeypatch.setattr(pdf_export, "find_soffice", lambda **kw: "soffice")

        def _no_product(cmd, **kw):
            return SimpleNamespace(returncode=1, stdout=b"", stderr=b"boom")

        monkeypatch.setattr(pdf_export.subprocess, "run", _no_product)
        with pytest.raises(RuntimeError, match="未生成 PDF"):
            pdf_export.convert_docx_to_pdf(docx)
        assert target.read_bytes() == b"old but valid pdf", (
            "失败路径不得摧毁既有产物（原实现 soffice 直写终态位会半写/清空旧 PDF）"
        )

    def test_tmp_outdir_cleaned_on_success_and_failure(self, monkeypatch, tmp_path):
        """临时产物目录（.pdf_tmp_*）在成功与失败路径都必须清理，不在导出目录堆积。"""
        docx = _setup_docx(tmp_path)
        monkeypatch.setattr(pdf_export, "find_soffice", lambda **kw: "soffice")

        monkeypatch.setattr(
            pdf_export.subprocess, "run", _fake_run_writes_outdir(b"ok pdf", {})
        )
        pdf_export.convert_docx_to_pdf(docx)
        assert not list(tmp_path.glob(".pdf_tmp_*")), "成功路径临时目录必须清理"

        def _no_product(cmd, **kw):
            # 模拟 soffice 连 outdir 都没建就退出
            return SimpleNamespace(returncode=1, stdout=b"", stderr=b"fail")

        # 第一段成功转换后 target 已新于源 → 入口缓存会命中；把 docx 拨到最新
        # 强制第二段走真实转换失败路径
        _newest = docx.stat().st_mtime + 10
        os.utime(docx, (_newest, _newest))
        monkeypatch.setattr(pdf_export.subprocess, "run", _no_product)
        with pytest.raises(RuntimeError):
            pdf_export.convert_docx_to_pdf(docx)
        assert not list(tmp_path.glob(".pdf_tmp_*")), "失败路径临时目录必须清理"

    def test_outdir_is_sibling_same_volume_dir(self, monkeypatch, tmp_path):
        """--outdir 必须指向源文件同目录下的同卷临时目录（保证 os.replace 原子性）。"""
        docx = _setup_docx(tmp_path)
        monkeypatch.setattr(pdf_export, "find_soffice", lambda **kw: "soffice")
        log: dict = {}
        monkeypatch.setattr(
            pdf_export.subprocess, "run", _fake_run_writes_outdir(b"x", log)
        )
        pdf_export.convert_docx_to_pdf(docx)
        outdir = Path(log["cmds"][0][log["cmds"][0].index("--outdir") + 1])
        assert outdir.parent == docx.parent, "临时目录必须与源同目录（同卷）"
        assert outdir.name.startswith(".pdf_tmp_")

    def test_replace_retry_on_transient_windows_lock(self, monkeypatch, tmp_path):
        """os.replace 遇瞬态占用（Windows 读句柄）时退避重试一次后成功。"""
        docx = _setup_docx(tmp_path)
        monkeypatch.setattr(pdf_export, "find_soffice", lambda **kw: "soffice")
        monkeypatch.setattr(
            pdf_export.subprocess, "run", _fake_run_writes_outdir(b"new pdf", {})
        )
        calls = {"n": 0}
        real_replace = os.replace

        def flaky_replace(src, dst, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("目标被并发读句柄占用（模拟）")
            return real_replace(src, dst, **kw)

        monkeypatch.setattr(pdf_export.os, "replace", flaky_replace)
        out = pdf_export.convert_docx_to_pdf(docx)
        assert out.read_bytes() == b"new pdf"
        assert calls["n"] == 2, "瞬态占用必须重试一次而非直接失败"
