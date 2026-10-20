"""BUG-FIX 2026-10-19：PDF 提取超时护栏 + 第三方日志降噪的回归测试。

背景：上传 278 页招标 PDF 后，根 logger DEBUG 使 pdfminer 逐 token 写
RotatingFileHandler（实测 ~1MB/8s 轮转风暴），4 个并发上传线程互相放大，
解析在请求线程里挂死 → 前端判失败、file_parse_tasks 永停 processing、
uploaded_files 零记录。修复两层：
  1. main.py 把 pdfminer/pdfplumber/pypdf 等第三方 logger 默认降噪到 WARNING；
  2. extractors._extract_pdf 外层加 PDF_EXTRACT_TIMEOUT 线程护栏，
     超时抛 TimeoutError 走 save_upload 既有 failed 分支。
"""

import logging
from pathlib import Path
import re

import pytest

from app.services import extractors
from app.services import files as file_service


class TestPdfExtractTimeoutGuard:
    def test_timeout_raises_and_names_file(self, monkeypatch):
        """原实现挂住不返回 → 护栏按超时抛 TimeoutError（旧行为是永久挂死）。"""
        monkeypatch.setattr(extractors, "PDF_EXTRACT_TIMEOUT", 1)

        def _hang(file_path: str) -> str:
            import time

            time.sleep(3)  # 远超 1s 护栏即被判超时；短暂 sleep 不抢 GIL
            return ""

        monkeypatch.setattr(extractors, "_extract_pdf_impl", _hang)
        with pytest.raises(TimeoutError, match="PDF 解析超时"):
            extractors._extract_pdf("any.pdf")

    def test_fast_path_passthrough(self, monkeypatch):
        """未超时：结果原样返回，异常原样上抛（护栏不吞语义）。"""
        monkeypatch.setattr(extractors, "PDF_EXTRACT_TIMEOUT", 30)
        monkeypatch.setattr(extractors, "_extract_pdf_impl", lambda p: f"ok:{p}")
        assert extractors._extract_pdf("a.pdf") == "ok:a.pdf"

        def _boom(file_path: str) -> str:
            raise ValueError("inner")

        monkeypatch.setattr(extractors, "_extract_pdf_impl", _boom)
        with pytest.raises(ValueError, match="inner"):
            extractors._extract_pdf("a.pdf")

    def test_zero_timeout_disables_guard(self, monkeypatch):
        """PDF_EXTRACT_TIMEOUT=0 关闭护栏，直调原实现（保持旧行为出口）。"""
        monkeypatch.setattr(extractors, "PDF_EXTRACT_TIMEOUT", 0)
        monkeypatch.setattr(extractors, "_extract_pdf_impl", lambda p: "direct")
        assert extractors._extract_pdf("a.pdf") == "direct"

    def test_entry_never_hangs_synchronously(self, monkeypatch):
        """护栏必须限时返回：即使原实现死循环，调用方也在 timeout 内拿到异常。"""
        monkeypatch.setattr(extractors, "PDF_EXTRACT_TIMEOUT", 1)

        def _spin(file_path: str) -> str:
            # 有限自旋（≈2s，超过 1s 护栏即被判超时）：
            # 无限纯计算线程会持续抢 GIL，把整个 pytest 进程拖慢一个数量级
            import time

            t0 = time.monotonic()
            while time.monotonic() - t0 < 2:
                pass

        monkeypatch.setattr(extractors, "_extract_pdf_impl", _spin)
        with pytest.raises(TimeoutError):
            extractors._extract_pdf("x.pdf")


class TestUploadFailurePersistence:
    def test_save_upload_marks_task_failed_on_extract_timeout(self, monkeypatch):
        """提取抛错时 save_upload 走 failed 分支：任务行与文件行都有终态，
        不再出现「任务永停 processing + uploaded_files 零记录」。"""
        from app.models import ProjectCreate
        from app.services.projects import create_project

        pid = create_project(ProjectCreate(name="护栏-提取超时")).id
        try:
            # save_upload 经 files 模块属性调 _extract_pdf（R-1 晚解析接缝），
            # patch files 层即命中真实链路（extractors 层改动同样经此生效）
            def _timeout(file_path: str) -> str:
                raise TimeoutError("PDF 解析超时（>1s）")

            monkeypatch.setattr(file_service, "_extract_pdf", _timeout)
            r = file_service.save_upload(pid, "招标文件.pdf", b"%PDF-1.4 fake", "main")
            assert r["status"] == "failed", r
            assert "超时" in r["message"], r

            with file_service.get_db(readonly=True) as conn:
                task = conn.execute(
                    "SELECT status, error_message FROM file_parse_tasks "
                    "WHERE project_id = ? AND filename = ?",
                    (pid, "招标文件.pdf"),
                ).fetchone()
                frow = conn.execute(
                    "SELECT status FROM uploaded_files WHERE project_id = ? AND filename = ?",
                    (pid, "招标文件.pdf"),
                ).fetchone()
            assert task is not None, "任务行必须存在且被写成终态（缺陷正是永停 processing）"
            assert task["status"] == "failed", dict(task)
            assert "超时" in (task["error_message"] or "")
            assert frow is not None and frow["status"] == "failed", frow
        finally:
            from app.services.projects import delete_project

            delete_project(pid)


class TestUploadedFilesUpdatedAtMigration:
    """第三层根因：老库 uploaded_files 缺 updated_at 列（旧 ALTER 用了
    SQLite 非法的非常量默认值，异常被 pass 吞掉，列永远加不上 →
    save_upload 的 INSERT 必然失败并回滚删除已落盘文件）。"""

    def _old_db_like_conn(self):
        """在测试库上模拟老 schema：DROP 掉 updated_at 列后交还给 _migrate 修复。"""
        import sqlite3

        from app.db import get_db

        try:
            with get_db() as conn:
                conn.execute("ALTER TABLE uploaded_files DROP COLUMN updated_at")
        except sqlite3.OperationalError as e:  # 老版本 SQLite 不支持 DROP COLUMN
            pytest.skip(f"SQLite 不支持 DROP COLUMN 模拟老库: {e}")
        return get_db()

    def test_migrate_adds_column_on_old_db(self):
        from app.db import _migrate
        from app.models import ProjectCreate
        from app.services.projects import create_project, delete_project

        pid = create_project(ProjectCreate(name="迁移验证-updated_at")).id
        ctx = self._old_db_like_conn()
        try:
            with ctx as conn:
                cols_before = {r["name"] for r in conn.execute("PRAGMA table_info(uploaded_files)")}
                assert "updated_at" not in cols_before  # 模拟生效
                conn.execute(
                    "INSERT INTO uploaded_files (project_id, filename, path, file_type, status)"
                    " VALUES (?, 'legacy-迁移验证.pdf', '/p', 'pdf', 'success')",
                    (pid,),
                )
                _migrate(conn)
                cols = {r["name"] for r in conn.execute("PRAGMA table_info(uploaded_files)")}
                assert "updated_at" in cols, "老库迁移后必须有 updated_at 列（缺陷正是列永远加不上）"
                # save_upload 的 INSERT 口径：显式写 updated_at，不得再报 no column
                conn.execute(
                    "INSERT INTO uploaded_files (project_id, filename, path, file_type,"
                    " file_role, status, extracted_text_path, message, updated_at)"
                    " VALUES (?, 'new-迁移验证.pdf', '/p2', 'pdf', 'main', 'success', '', '',"
                    " datetime('now', 'localtime'))",
                    (pid,),
                )
                # 存量行回填 created_at，而非空串
                row = conn.execute(
                    "SELECT updated_at, created_at FROM uploaded_files "
                    "WHERE project_id=? AND filename='legacy-迁移验证.pdf'",
                    (pid,),
                ).fetchone()
                assert row["updated_at"] and row["updated_at"] == row["created_at"]
        finally:
            delete_project(pid)

    def test_migrate_is_idempotent(self):
        from app.db import _migrate, get_db

        # 上一用例已把列加回，此处只验证重复执行不报错（duplicate column 静默幂等）
        with get_db() as conn:
            _migrate(conn)
            _migrate(conn)
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(uploaded_files)")}
            assert "updated_at" in cols


class TestThirdPartyLogQuiet:
    def test_main_downgrades_third_party_loggers(self):
        """main.py 加载后，pdfminer 等第三方 logger 级别 ≥ WARNING。"""
        import app.main  # noqa: F401  (导入即执行日志配置)

        for name in ("pdfminer", "pdfplumber", "pypdf"):
            lv = logging.getLogger(name).getEffectiveLevel()
            assert lv >= logging.WARNING, f"{name} 仍为 {lv}，日志风暴防线失效"

    def test_env_override_supported(self):
        """THIRD_PARTY_LOG_LEVEL 可显式放开 DEBUG 排障（配置口径钉住在 main.py）。"""
        # app/services/extractors/__init__.py → parents[2] = app/（main.py 所在）
        main_src = (Path(extractors.__file__).resolve().parents[2] / "main.py").read_text(encoding="utf-8")
        assert re.search(r"THIRD_PARTY_LOG_LEVEL", main_src)
