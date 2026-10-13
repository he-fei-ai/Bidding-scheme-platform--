"""【导出文档模块】第三轮深度审计与修复（2026-10-13）

覆盖缺陷（全部先复现后修复，测试钉住修复后行为）：
  | F-1 | `_register_stream_journal` 以 `(finished_at or created_at)+TTL`
        | 判定过期 → **未终态的活动流**在 created_at 超过 10 分钟后被误淘汰：
        | 大型标书构建 >10 分钟 + 期间有新导出注册 journal → 旧流 journal
        | 丢失 → 客户端 resume 404 → 降级同步导出 → 撞仍在持有的组锁 → 409。
  | F-2 | HTML 导出（2026-10-12 新增）① 未抢导出组锁：同项目并发 HTML
        | 导出同秒撞名互相截断；② 直写最终路径：写盘失败留半截 .html；
        | ③ 空文档不放行（DOCX/SSE 均为 400）。
  | F-3 | L1 图表渲染缓存 `_get_cached_chart_render` 直接返回共享 BytesIO：
        | 多项目并发命中同一条目时光标互相打断 → 空/半截图 → 插入失败
        | 占位符（图号虚跳）。
  | F-4 | 章节渲染失败回滚正文元素（含已渲染图表）后**图号计数器不还原**：
        | 重试拿到新号 → 成品图号断档。
"""
from __future__ import annotations

import asyncio
import io
import json
import os
from pathlib import Path
import threading
import time
import uuid

from docx import Document
from fastapi import HTTPException
import pytest

from app.db import get_db, init_db
from app.models import ExportConfig
from app.routers import export as export_router
from app.routers.export import export_html
from app.security.auth import User
from app.services import export as export_mod
from app.services import html_export as html_export_mod
from app.services import task_lock
from app.services.export_cache import (
    _fig_counter_ctx_var,
    _get_cached_chart_render,
    _next_figure_number,
    _reset_figure_counter,
    _restore_figure_counter,
    _snapshot_figure_counter,
    _store_chart_render,
)
from app.services.html_export import build_html


OWNER = 4300


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


def _seed_section(project_id: int, *, title: str, content: str, sort_order: int = 0, level: int = 1) -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO sections (project_id, title, level, sort_order, content, status, section_type) "
            "VALUES (?, ?, ?, ?, ?, 'completed', 'normal')",
            (project_id, title, level, sort_order, content),
        )
        return cur.lastrowid


def _make_png_bytes(w: int = 60, h: int = 40) -> bytes:
    PilImage = pytest.importorskip("PIL.Image", reason="PIL 不可用时无法构造图片")
    buf = io.BytesIO()
    PilImage.new("RGB", (w, h), (30, 90, 160)).save(buf, format="PNG")
    return buf.getvalue()


# ============================================================================
# F-1 —— SSE journal 误淘汰活动流
# ============================================================================


class TestSseJournalEviction:
    """F-1：TTL 只淘汰已终态 journal；活动流不得因 created_at 过期被误淘汰。"""

    @pytest.fixture(autouse=True)
    def _isolate_stream_journals(self):
        _STREAM_JOURNALS = export_router._STREAM_JOURNALS
        _STREAM_JOURNALS.clear()
        yield
        _STREAM_JOURNALS.clear()

    def test_active_stream_survives_ttl_when_new_stream_registers(self):
        """F-1 主场景：构建 15 分钟仍未终态的流，不得在下一次注册时被淘汰。"""
        import queue

        q: queue.Queue = queue.Queue()
        long_running = export_router._StreamJournal("long-running", 1, q)
        long_running.created_at = time.time() - export_router._STREAM_JOURNAL_TTL_SECONDS - 300
        # 注意：**不** mark_terminal —— 构建仍在进行（大文档常见 >10 分钟）
        export_router._register_stream_journal(long_running)

        # 触发淘汰逻辑：注册一条新流
        newcomer = export_router._StreamJournal("newcomer", 1, queue.Queue())
        export_router._register_stream_journal(newcomer)

        assert "long-running" in export_router._STREAM_JOURNALS, (
            "未终态的活动流不得被 TTL 误淘汰（否则断线续传 404 → 降级同步导出 → 组锁 409）"
        )

    def test_terminal_stream_still_evicted_after_ttl(self):
        """与既有口径一致：已终态且封存超 TTL 的 journal 仍被注册时 GC。"""
        import queue

        q: queue.Queue = queue.Queue()
        j = export_router._StreamJournal("finished-old", 1, q)
        j.mark_terminal()
        j.finished_at = time.time() - export_router._STREAM_JOURNAL_TTL_SECONDS - 1
        export_router._register_stream_journal(j)
        assert "finished-old" not in export_router._STREAM_JOURNALS

    def test_max_capacity_still_enforced_for_active_streams(self):
        """活动流过多时容量上限仍是硬闸（popitem 最旧优先），防止内存无界增长。"""
        import queue

        for i in range(export_router._STREAM_JOURNAL_MAX + 5):
            j = export_router._StreamJournal(f"active-{i}", 1, queue.Queue())
            export_router._register_stream_journal(j)
        assert len(export_router._STREAM_JOURNALS) <= export_router._STREAM_JOURNAL_MAX


# ============================================================================
# F-2 —— HTML 导出：原子写盘
# ============================================================================


class TestHtmlExportAtomicWrite:
    """F-2：build_html 原子写盘（写坏不得留半截成品；并发同名不得互相截断）。"""

    def test_write_failure_leaves_no_final_file_and_cleans_tmp(self, tmp_path, monkeypatch):
        pid = _new_project(f"HTML原子写-{uuid.uuid4().hex[:6]}")
        _seed_section(pid, title="编制说明", content="本章说明文字。")
        out = tmp_path / "out.html"

        def _boom(src, dst):
            raise OSError("disk full simulated")

        monkeypatch.setattr(html_export_mod.os, "replace", _boom)
        with pytest.raises(OSError):
            build_html(pid, ExportConfig(), output_path=str(out))

        assert not out.exists(), "写盘失败不得在最终路径留下半截 HTML"
        leftovers = list(tmp_path.glob("out.html.tmp.*"))
        assert leftovers == [], f"临时件必须被清理，残留: {leftovers}"

    def test_concurrent_same_target_leaves_intact_file(self, tmp_path, monkeypatch):
        """同项目并发导出按秒级时间戳撞名：原子替换保证最终文件是完整的一份。"""
        pid = _new_project(f"HTML并发撞名-{uuid.uuid4().hex[:6]}")
        _seed_section(pid, title="施工方案", content="本章施工方案文字。")
        out = tmp_path / "shared.html"

        real_replace = os.replace
        _lock = threading.Lock()

        def _slow_replace(src, dst):
            # 拉长「临时件已写完 → 替换」窗口，放大并发互踩概率
            with _lock:
                time.sleep(0.05)
            real_replace(src, dst)

        monkeypatch.setattr(html_export_mod.os, "replace", _slow_replace)

        errors: list[BaseException] = []

        def _worker():
            try:
                build_html(pid, ExportConfig(), output_path=str(out))
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=_worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"并发导出不应抛异常: {errors}"
        assert out.exists()
        content = out.read_text(encoding="utf-8")
        assert content.rstrip().endswith("</html>"), "最终文件必须是完整 HTML（非半截）"
        assert content.lstrip().startswith("<!DOCTYPE html>")


# ============================================================================
# F-2 —— HTML 路由：组锁互斥 + 空文档口径
# ============================================================================


class TestHtmlRouteGuards:
    """F-2：/export/html 与 DOCX 路径同口径（组锁 409 / 空文档 400）。"""

    def test_empty_project_returns_400(self):
        pid = _new_project(f"HTML空项目-{uuid.uuid4().hex[:6]}")
        with pytest.raises(HTTPException) as ei:
            asyncio.run(export_html(pid, ExportConfig(), identity=User(OWNER, "owner", "user")))
        assert ei.value.status_code == 400

    def test_lock_conflict_returns_409(self):
        """手动占用导出组锁后调用路由 → 409（修复前无锁，并发双写撞名）。"""
        pid = _new_project(f"HTML锁冲突-{uuid.uuid4().hex[:6]}")
        _seed_section(pid, title="施工组织", content="本章施工组织文字。")
        owner_id = f"test-holder-{uuid.uuid4().hex[:8]}"
        record = task_lock.acquire(
            "export", project_id=pid, owner_id=owner_id, note="test holder"
        )
        try:
            with pytest.raises(HTTPException) as ei:
                asyncio.run(export_html(pid, ExportConfig(), identity=User(OWNER, "owner", "user")))
            assert ei.value.status_code == 409
        finally:
            task_lock.release(record.get("lock_key", ""), owner_id)

    def test_happy_path_returns_file_response(self):
        pid = _new_project(f"HTML正常-{uuid.uuid4().hex[:6]}")
        _seed_section(pid, title="施工组织设计", content="本章施工组织设计文字。")
        resp = asyncio.run(export_html(pid, ExportConfig(), identity=User(OWNER, "owner", "user")))
        assert resp.media_type == "text/html; charset=utf-8"
        assert Path(resp.path).exists()
        assert Path(resp.path).suffix == ".html"


# ============================================================================
# F-3 —— L1 图表渲染缓存返回共享 BytesIO
# ============================================================================


class TestL1ChartRenderCacheCopy:
    """F-3：缓存命中必须返回独立副本（多项目并发命中同一条目不得互相打断光标）。"""

    PAYLOAD = json.dumps({"type": "gantt", "title": "施工进度计划"}, ensure_ascii=False)

    def test_two_hits_return_independent_streams(self):
        data = _make_png_bytes()
        _store_chart_render(self.PAYLOAD, io.BytesIO(data), "gantt")
        a, size_a = _get_cached_chart_render(self.PAYLOAD, "gantt")
        b, size_b = _get_cached_chart_render(self.PAYLOAD, "gantt")
        assert a is not None and b is not None
        assert a is not b, "两次命中不得返回同一 BytesIO 对象（并发读竞态）"
        assert (size_a, size_b) == (len(data), len(data))
        # 消费 a 不影响 b（修复前 b.read() 返回空串）
        assert a.read() == data
        assert b.read() == data

    def test_concurrent_hits_all_get_full_bytes(self):
        data = _make_png_bytes()
        _store_chart_render(self.PAYLOAD, io.BytesIO(data), "gantt")
        results: dict[int, bytes] = {}
        barrier = threading.Barrier(8)

        def _worker(i: int):
            barrier.wait()
            stream, _ = _get_cached_chart_render(self.PAYLOAD, "gantt")
            results[i] = stream.read() if stream else b""

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert all(v == data for v in results.values()), (
            f"并发命中存在读到空/半截字节流的线程: {sorted(k for k, v in results.items() if v != data)}"
        )


# ============================================================================
# F-4 —— 失败章节回滚后图号计数器不还原 → 图号断档
# ============================================================================


class TestFigureCounterSnapshotRestore:
    """F-4 单元层：快照/还原让重试从同一号码继续。"""

    def test_restore_reuses_same_number(self):
        _reset_figure_counter(1)
        ctx: dict = {}
        token = _fig_counter_ctx_var.set(ctx)
        try:
            snap = _snapshot_figure_counter()
            n1 = _next_figure_number("1", 1)
            assert n1 == "图1-1"
            _restore_figure_counter(snap)
            n2 = _next_figure_number("1", 1)
            assert n2 == "图1-1", f"还原后应从同一号码继续，实际: {n2}（修复前为 图1-2，成品断档）"
        finally:
            _fig_counter_ctx_var.reset(token)

    def test_global_fallback_state_is_covered(self):
        """ContextVar 未设置（直接调用方）时快照走进程级全局状态。"""
        _reset_figure_counter(1)
        snap = _snapshot_figure_counter()
        _next_figure_number("1", 1)
        _restore_figure_counter(snap)
        n = _next_figure_number("1", 1)
        assert n == "图1-1"


class TestFailedSectionFigureNumberingEndToEnd:
    """F-4 集成层：章节渲染失败回滚（含已渲染图表）→ 重试 → 图号连续不断档。"""

    def test_retry_keeps_continuous_figure_number(self, monkeypatch):
        pid = _new_project(f"图号断档-{uuid.uuid4().hex[:6]}")
        marker = (
            '<<CHART:{"type":"gantt","title":"施工进度计划",'
            '"tasks":[{"name":"土方开挖","start":"2026-01-01","duration":10}]}>>'
        )
        _seed_section(
            pid, title="施工进度安排", level=1,
            content=f"本节安排施工进度。\n\n{marker}\n",
        )

        # 渲染器打桩：返回真实 PNG 字节（图片可入 docx）
        def _fake_render(code, chart_type, skip_http=False):
            return io.BytesIO(_make_png_bytes())

        monkeypatch.setattr(export_mod, "render_mermaid_to_bytes", _fake_render)

        # 第一次 _render_section_charts 抛错 → 节点失败整体回滚 → 重试成功
        real_render_section_charts = export_mod._render_section_charts
        calls = {"n": 0}

        def _flaky_render_section_charts(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated chart pipeline failure")
            return real_render_section_charts(*args, **kwargs)

        monkeypatch.setattr(export_mod, "_render_section_charts", _flaky_render_section_charts)

        out_path, _export_id = export_mod.build_docx(pid, ExportConfig())
        assert calls["n"] >= 2, "失败节点应被重试"

        doc = Document(out_path)
        texts = [p.text.strip() for p in doc.paragraphs]
        fig_captions = [t for t in texts if t.startswith("图1-")]
        assert fig_captions == ["图1-1 施工进度计划"], (
            f"重试后图号应连续复用 图1-1（回滚的图不占号），实际: {fig_captions}"
        )
        assert not any("渲染异常" in t for t in texts), "重试成功后占位符应被移除"


# ============================================================================
# 与其他模块调用链：HTML 导出落盘目录与 DOCX 一致（供 history/latest 口径复用）
# ============================================================================


class TestHtmlExportArtifactLocation:
    def test_artifact_lands_in_project_exports_dir(self):
        from app.config import PROJECTS_DATA_DIR

        pid = _new_project(f"HTML落盘-{uuid.uuid4().hex[:6]}")
        _seed_section(pid, title="概述", content="概述文字。")
        path, _fingerprint = build_html(pid, ExportConfig())
        p = Path(path)
        assert p.parent == (PROJECTS_DATA_DIR / str(pid) / "exports").resolve()
        assert p.name.startswith("HTML落盘-")
