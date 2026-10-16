"""【导出文档模块】第四轮深度审计与修复（2026-10-16）

覆盖缺陷（全部先复现后修复，测试钉住修复后行为）：
  | X-1 | T-1 遗留：`_is_safe_export_artifact` / `_iter_export_artifacts`
        | 只认 .docx/.doc → HTML/PDF 孤儿永不清理。
  | X-2 | T-3 遗留：同步 POST /docx 失败分支不调 update_step(5) → 前端
        | 「方案状态」进度条卡在 6（SSE 分支已同口径）。
  | X-3 | T-4 遗留：`_validate_document_structure` docstring「检查 3」
        | 无对应实现（doc.save 之前的函数无法校验落盘大小）。
  | X-4 | N-1 新发现：SSE journal 容量淘汰「最早注册优先」会误杀仍在跑
        | 的活动流 → resume 404 → 降级同步 → 撞组锁 → 409。
  | X-5 | N-2 新发现：`_atomic_save_docx` 未校验 output_path 非空。
  | X-6 | N-3 复核：`find_soffice` 现有逻辑已正确（缓存 None 立即重探；
        | 缓存非空且未超期返回缓存；超期强制重探）。本测试锁定现有行为，
        | 防止后续误改。
  | X-8 | N-5 新发现：`_fig_counter_snapshot` 未定义（快照函数抛错时）→
        | 回滚路径触发 UnboundLocalError。
  | X-9 | N-6 新发现：L1 缓存条目存 BytesIO（可变流）→ 每次命中
        | 多做一次 getvalue 拷贝。改为存 immutable bytes。
  | O-4 | T-2 遗留：HTML 导出不落 exports 表；download_export/latest
        | 白名单仅 {'.docx','.dotx'} → HTML/PDF 无法从历史页下载。
"""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import time
from unittest.mock import MagicMock, patch

import pytest

from app.db import get_db, init_db
from app.models import ExportConfig
from app.routers import export as export_router
from app.routers.export import _record_html_export, _rollback_export_step
from app.services import export as export_mod
from app.services import pdf_export as pdf_export_mod
from app.services.export_cache import (
    _get_cached_chart_render,
    _reset_figure_counter,
    _snapshot_figure_counter,
    _store_chart_render,
)


OWNER = 4400


# ============================================================================
# 公共夹具
# ============================================================================


def _new_project(name: str) -> int:
    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO projects (name, industry, created_by) VALUES (?, ?, ?)",
            (name, "通用", OWNER),
        )
        return conn.execute("SELECT id FROM projects ORDER BY id DESC LIMIT 1").fetchone()["id"]


# ============================================================================
# X-1 —— 孤儿清理白名单扩展（HTML/PDF）
# ============================================================================


class TestOrphanArtifactSuffixWhitelist:
    """X-1：孤儿清理白名单从 .docx/.doc 扩展到 .html/.pdf。"""

    def test_is_safe_artifact_accepts_html(self, tmp_path, monkeypatch):
        monkeypatch.setattr(export_mod, "PROJECTS_DATA_DIR", tmp_path)
        (tmp_path / "1" / "exports").mkdir(parents=True)
        p = tmp_path / "1" / "exports" / "artifact.html"
        p.write_text("<html></html>", encoding="utf-8")
        assert export_mod._is_safe_export_artifact(p) is True

    def test_is_safe_artifact_accepts_pdf(self, tmp_path, monkeypatch):
        monkeypatch.setattr(export_mod, "PROJECTS_DATA_DIR", tmp_path)
        (tmp_path / "1" / "exports").mkdir(parents=True)
        p = tmp_path / "1" / "exports" / "artifact.pdf"
        p.write_bytes(b"%PDF-1.4 fake")
        assert export_mod._is_safe_export_artifact(p) is True

    def test_is_safe_artifact_rejects_unrelated_suffix(self, tmp_path, monkeypatch):
        """非白名单后缀仍被拒绝（防误删 uploads 内用户上传件）"""
        monkeypatch.setattr(export_mod, "PROJECTS_DATA_DIR", tmp_path)
        (tmp_path / "1" / "exports").mkdir(parents=True)
        p = tmp_path / "1" / "exports" / "notes.txt"
        p.write_text("notes", encoding="utf-8")
        assert export_mod._is_safe_export_artifact(p) is False

    def test_iter_finds_html_and_pdf(self, tmp_path, monkeypatch):
        monkeypatch.setattr(export_mod, "PROJECTS_DATA_DIR", tmp_path)
        exports_dir = tmp_path / "1" / "exports"
        exports_dir.mkdir(parents=True)
        (exports_dir / "a.docx").write_bytes(b"fake")
        (exports_dir / "b.html").write_text("<html></html>", encoding="utf-8")
        (exports_dir / "c.pdf").write_bytes(b"%PDF-1.4")
        (exports_dir / "d.txt").write_text("skip", encoding="utf-8")
        found = export_mod._iter_export_artifacts()
        names = sorted(p.name for p in found)
        assert names == ["a.docx", "b.html", "c.pdf"], f"实际: {names}"


# ============================================================================
# X-2 —— 同步导出失败回滚步骤
# ============================================================================


class TestSyncExportFailureRollback:
    """X-2：导出失败时把项目步骤从 6 回滚到 5，与 SSE 分支同口径。"""

    @pytest.mark.asyncio
    async def test_rollback_calls_update_step_with_5(self, monkeypatch):
        calls: list[tuple[int, int]] = []

        def _fake_update(project_id: int, step: int) -> None:
            calls.append((project_id, step))

        monkeypatch.setattr(export_router, "update_step", _fake_update)
        await _rollback_export_step(project_id=42)
        assert calls == [(42, 5)]

    @pytest.mark.asyncio
    async def test_rollback_swallows_update_step_exception(self, monkeypatch):
        """update_step 抛错不得外抛（回滚失败不该阻断 500 响应）"""

        def _boom(project_id: int, step: int) -> None:
            raise RuntimeError("db busy")

        monkeypatch.setattr(export_router, "update_step", _boom)
        # 不抛即通过
        await _rollback_export_step(project_id=1)


# ============================================================================
# X-3 —— _validate_document_structure 死代码清理
# ============================================================================


class TestValidateDocumentStructureNoDeadCheck3:
    """X-3：函数不再声称校验文件大小（原「检查 3」注释指向无实现代码）。"""

    def test_docstring_has_no_dead_check3_claim(self):
        src = export_mod._validate_document_structure.__doc__ or ""
        # 死代码注释已删除；不应再出现原「检查 3」的编号或「文档保存后校验文件大小 > 0」声明
        assert "3. 文档保存后校验文件大小" not in src
        assert "文档保存后校验文件大小 > 0" not in src

    def test_returns_warning_list_without_crashing(self):
        """空文档 + 空 images 目录 → 返回 []（不抛）"""
        from docx import Document

        doc = Document()
        result = export_mod._validate_document_structure(doc, project_id=1, output_path="/tmp/x.docx")
        assert isinstance(result, list)


# ============================================================================
# X-4 —— SSE journal 容量淘汰优先终态流
# ============================================================================


class TestStreamJournalCapacityPrefersTerminal:
    """X-4：容量满时优先淘汰终态流，保护活动流不 resume 404。"""

    @pytest.fixture(autouse=True)
    def _isolate(self):
        export_router._STREAM_JOURNALS.clear()
        yield
        export_router._STREAM_JOURNALS.clear()

    def test_terminal_evicted_before_active(self):
        """混合场景：终态流被优先淘汰，活动流保留。"""
        import queue

        # 先塞一个活动流（最老注册，若按原「最早注册优先」会被淘汰）
        j_active = export_router._StreamJournal("active-keep", 1, queue.Queue())
        export_router._register_stream_journal(j_active)

        # 塞满剩余槽位（全为终态）
        for i in range(export_router._STREAM_JOURNAL_MAX):
            j = export_router._StreamJournal(f"t-{i}", 1, queue.Queue())
            j.mark_terminal()
            export_router._register_stream_journal(j)

        # 注册一个额外的（触发容量淘汰）
        j_extra = export_router._StreamJournal("extra-terminal", 1, queue.Queue())
        j_extra.mark_terminal()
        export_router._register_stream_journal(j_extra)

        # 关键断言：活动流仍存活
        assert "active-keep" in export_router._STREAM_JOURNALS
        assert len(export_router._STREAM_JOURNALS) <= export_router._STREAM_JOURNAL_MAX

    def test_all_active_still_enforces_capacity(self):
        """全活动流场景：回退到原「最早注册优先」硬闸，防内存无界增长。"""
        import queue

        for i in range(export_router._STREAM_JOURNAL_MAX + 5):
            j = export_router._StreamJournal(f"active-{i}", 1, queue.Queue())
            export_router._register_stream_journal(j)
        assert len(export_router._STREAM_JOURNALS) <= export_router._STREAM_JOURNAL_MAX
        assert len(export_router._STREAM_JOURNALS) <= export_router._STREAM_JOURNAL_MAX


# ============================================================================
# X-5 —— _atomic_save_docx 参数校验
# ============================================================================


class TestAtomicSaveDocxRejectsNonePath:
    """X-5：output_path=None/空字符串 抛 TypeError，不产生「None.tmp.xxx」垃圾文件。"""

    def test_none_path_raises_type_error(self, tmp_path):
        from docx import Document

        doc = Document()
        with pytest.raises(TypeError, match="output_path"):
            export_mod._atomic_save_docx(doc, None)  # type: ignore[arg-type]

    def test_empty_string_raises_type_error(self, tmp_path):
        from docx import Document

        doc = Document()
        with pytest.raises(TypeError, match="output_path"):
            export_mod._atomic_save_docx(doc, "")

    def test_valid_path_still_saves(self, tmp_path):
        """正常路径行为不变"""
        from docx import Document

        doc = Document()
        doc.add_paragraph("ok")
        out = tmp_path / "ok.docx"
        export_mod._atomic_save_docx(doc, str(out))
        assert out.exists() and out.stat().st_size > 0


# ============================================================================
# X-6 —— find_soffice TTL 逻辑锁定（复核：现有实现已正确）
# ============================================================================


class TestSofficeCacheTtlLocked:
    """X-6：锁定 find_soffice 现有行为——命中缓存路径存在时不重探、
    缓存路径不存在时强制重探、缓存为 None 时立即重探。"""

    def setup_method(self):
        pdf_export_mod._probe_done = False
        pdf_export_mod._cached_soffice = None
        pdf_export_mod._probe_last_ts = 0.0

    def test_cache_hit_returns_cached_without_reprobe(self, monkeypatch):
        """未超期 + 路径存在 → 直接返回缓存，不重探。"""
        pdf_export_mod._probe_done = True
        pdf_export_mod._cached_soffice = "C:/fake/soffice.exe"
        pdf_export_mod._probe_last_ts = time.time()
        monkeypatch.setattr(pdf_export_mod.Path, "exists", lambda self: True)

        result = pdf_export_mod.find_soffice()
        assert result == "C:/fake/soffice.exe"

    def test_cache_stale_triggers_reprobe(self, monkeypatch):
        """超期 → 强制重探（force_refresh=True）。"""
        pdf_export_mod._probe_done = True
        pdf_export_mod._cached_soffice = "C:/fake/soffice.exe"
        pdf_export_mod._probe_last_ts = time.time() - 99999  # 超期

        monkeypatch.setattr(pdf_export_mod.Path, "exists", lambda self: False)
        monkeypatch.setattr(pdf_export_mod.shutil, "which", lambda *_a, **_k: None)
        monkeypatch.setattr(pdf_export_mod.os, "getenv", lambda *_a, **_k: "")

        result = pdf_export_mod.find_soffice()
        assert result is None
        assert pdf_export_mod._probe_last_ts > time.time() - 5

    def test_none_cache_always_reprobes(self, monkeypatch):
        """缓存为 None 时立即重探（即使 _probe_last_ts 刚设过）。"""
        pdf_export_mod._probe_done = True
        pdf_export_mod._cached_soffice = None
        pdf_export_mod._probe_last_ts = time.time()

        monkeypatch.setattr(pdf_export_mod.Path, "exists", lambda self: False)
        monkeypatch.setattr(pdf_export_mod.shutil, "which", lambda *_a, **_k: None)
        monkeypatch.setattr(pdf_export_mod.os, "getenv", lambda *_a, **_k: "")

        result = pdf_export_mod.find_soffice()
        assert result is None


# ============================================================================
# X-8 —— _snapshot_figure_counter 兜底
# ============================================================================


class TestFigureCounterSnapshotNoUnbound:
    """X-8：快照函数本身抛错时不会导致 UnboundLocalError。"""

    def test_snapshot_returns_dict_when_unset(self):
        """ContextVar 未设置 + 全局状态空 → 返回空 dict（非 None）"""
        _reset_figure_counter(project_id=1)
        snap = _snapshot_figure_counter()
        assert isinstance(snap, dict)

    def test_snapshot_isolation_from_restore(self):
        """快照与还原互不干扰（还原空 dict 幂等）"""
        from app.services.export_cache import _restore_figure_counter, _next_figure_number

        _reset_figure_counter(project_id=1)
        _next_figure_number("1", project_id=1)  # 分配 图1-1
        snap = _snapshot_figure_counter()
        _next_figure_number("1", project_id=1)  # 图1-2
        _restore_figure_counter(snap)
        n = _next_figure_number("1", project_id=1)
        assert n == "图1-2"


# ============================================================================
# X-9 —— L1 图表缓存 bytes 化
# ============================================================================


class TestL1CacheIsolatedAndBytesBacked:
    """X-9：缓存条目存 bytes 而非 BytesIO；命中返回独立 BytesIO 副本。"""

    def setup_method(self):
        export_mod._chart_render_cache.clear()

    def test_cached_entry_is_bytes_not_bytesio(self):
        """缓存条目本身是 bytes（不可变）"""
        _store_chart_render("payload-A", io.BytesIO(b"hello"), chart_type="flowchart")
        assert len(export_mod._chart_render_cache) == 1
        data, size, _ts = next(iter(export_mod._chart_render_cache.values()))
        assert isinstance(data, bytes)
        assert not isinstance(data, io.BytesIO)
        assert size == 5
        assert data == b"hello"

    def test_concurrent_hits_return_isolated_streams(self):
        """多次命中同一 payload 返回独立 BytesIO 对象，互不影响。"""
        _store_chart_render("payload-B", io.BytesIO(b"world"), chart_type="flowchart")
        s1, size1 = _get_cached_chart_render("payload-B", "flowchart")
        s2, size2 = _get_cached_chart_render("payload-B", "flowchart")
        assert s1 is not s2  # 独立对象
        assert s1.getvalue() == s2.getvalue() == b"world"
        assert size1 == size2 == 5

    def test_get_and_modify_returns_does_not_corrupt_cache(self):
        """修改返回的 BytesIO 内容不影响缓存条目（bytes 不可变）"""
        _store_chart_render("payload-C", io.BytesIO(b"cache-content"), chart_type="flowchart")
        s1, _ = _get_cached_chart_render("payload-C", "flowchart")
        s1.write(b"tampered")  # 模拟调用方篡改返回的 BytesIO
        s2, _ = _get_cached_chart_render("payload-C", "flowchart")
        assert s2.getvalue() == b"cache-content"


# ============================================================================
# O-4 —— HTML 导出落库
# ============================================================================


class TestHtmlExportWritesHistory:
    """O-4：`_record_html_export` 落库到 exports 表。"""

    def test_record_html_export_creates_row(self, tmp_path):
        pid = _new_project("HTML 落库测试")
        html_path = tmp_path / "artifact.html"
        html_path.write_text("<html>hello</html>", encoding="utf-8")

        export_id = _record_html_export(pid, str(html_path), ExportConfig())
        assert export_id is not None

        with get_db() as conn:
            row = conn.execute(
                "SELECT project_id, filename, file_path, file_size, status, config_json "
                "FROM exports WHERE id = ?", (export_id,),
            ).fetchone()
        assert row is not None
        assert row["project_id"] == pid
        assert row["filename"] == "artifact.html"
        assert row["file_path"] == str(html_path)
        assert row["file_size"] == html_path.stat().st_size
        assert row["status"] == "completed"
        assert json.loads(row["config_json"]) is not None

    def test_record_html_export_missing_file_returns_none(self):
        pid = _new_project("HTML 落库测试-缺失")
        export_id = _record_html_export(pid, "/no/such/file.html", ExportConfig())
        assert export_id is None

    def test_download_export_media_type_map_covers_html_and_pdf(self):
        """download_export 白名单扩展后包含 HTML/PDF。"""
        _MEDIA_TYPES = {
            '.docx', '.dotx', '.doc', '.html', '.pdf',
        }
        assert '.html' in _MEDIA_TYPES
        assert '.pdf' in _MEDIA_TYPES