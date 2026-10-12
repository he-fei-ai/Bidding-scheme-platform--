"""导出文档模块 —— 遗留问题修复轮回归测试（2026-10-04，报告 §8 T1-T6）。

覆盖范围（每项对应报告遗留问题与最优方案落地）：

- T4 组锁心跳续约：start_keepalive 单元行为（续约推进 expires_at、幂等 stop、
  锁易主后停止续约不抛异常）+ 同步/SSE/Celery 三链路接入的源码契约。
- T5 内容指纹纳入图片：images 目录文件 mtime/size 变化 → 指纹变化；
  目录不存在/为空 → 指纹与旧值一致（向后兼容）。
- T2 失败导出落库：record_failed_export 行语义、latest 不被失败行污染、
  download/pdf 端点拒绝失败记录、同步链路 build 抛错后出现 failed 行。
- T3 SSE 事件 journal：append 序号/_sse_frame id 行、drain 线程接管至终态、
  断线→journal→resume 全链路回放（真实流驱动）、未知 stream_id 404。
- T1 PDF 导出：find_soffice 探测优先级、convert_docx_to_pdf 幂等复用/失败语义
  （PdfUnavailable/超时/无产物）、端点 501/403/400/404/200 分支。
- T6 无界队列闭环：保留 queue.Full 防御分支且闭环决策注释在位。

测试规范：延续 test_bugfix_2026_10_04_export_docx.py —— 真实 DB 行断言、
真实流驱动（不 mock FastAPI 依赖链）、路由挂载走 app.openapi()["paths"]
（当前 FastAPI 版本 include_router 惰性挂载，app.routes 无 path 属性）。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import queue
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.db import get_db, init_db
from app.models import ExportConfig
from app.security.auth import User
from app.services import export as export_mod
from app.services import pdf_export
from app.services import task_lock

OWNER = 100


def _new_project(name: str, created_by: int | None = OWNER) -> int:
    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO projects (name, industry, created_by) VALUES (?, ?, ?)",
            (name, "通用", created_by),
        )
        return conn.execute(
            "SELECT id FROM projects ORDER BY id DESC LIMIT 1"
        ).fetchone()["id"]


def _new_user_project_with_section(name: str) -> int:
    """建项目 + 一条非空章节（check_document 过空文档拦截用）。"""
    pid = _new_project(name, created_by=OWNER)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO sections (project_id, title, level, sort_order, content, status) "
            "VALUES (?, '第一节 总体部署', 1, 0, ?, 'completed')",
            (pid, "本项目总体部署依据招标文件与国家标准编制，含进度计划与资源配置安排。"),
        )
    return pid


def _insert_export_row(pid: int, file_path: str, status: str = "completed",
                       filename: str = "测试导出.docx") -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO exports (project_id, filename, file_path, file_size, config_json, status) "
            "VALUES (?, ?, ?, 10, '{}', ?)",
            (pid, filename, str(file_path), status),
        )
        return cur.lastrowid


class _FakeRequest:
    """常连不断的假 Request（resume/端点直调驱动流用）。"""

    async def is_disconnected(self):
        return False


# ============================================================
# T4：组锁心跳续约
# ============================================================


def _lock_row(lock_key: str):
    with get_db() as conn:
        return conn.execute(
            "SELECT heartbeat_at, expires_at, status FROM task_locks WHERE lock_key = ?",
            (lock_key,),
        ).fetchone()


class TestKeepaliveUnit:
    def test_noop_on_empty_args(self):
        """lock_key/owner 为空时返回 no-op stop 函数，不起线程（防御直调用）。"""
        stop = task_lock.start_keepalive("", "")
        assert callable(stop)
        stop()  # 幂等，可重复调用
        stop()

    def test_keepalive_extends_expires_and_stop_is_idempotent(self):
        """interval=1s：约 1 秒后 heartbeat_at/expires_at 必须被推进；stop 后不再变化。"""
        owner = task_lock.make_owner_id("export", 900654)
        rec = task_lock.acquire("export", project_id=900654, owner_id=owner)
        lock_key = rec.get("lock_key", "")
        assert lock_key, "acquire 必须返回 lock_key"
        try:
            row0 = _lock_row(lock_key)
            stop = task_lock.start_keepalive(lock_key, owner, interval_seconds=1)
            time.sleep(2.3)  # 首个心跳在 wait(1) 后触发，2.3s 至少续约 1 次
            row1 = _lock_row(lock_key)
            assert row1["heartbeat_at"] > row0["heartbeat_at"], (
                "keepalive 未推进 heartbeat_at —— TTL 到期后锁会被 reclaim_stale 回收，"
                "同项目第二个导出将持新锁并发 build（T4 修复的原始缺陷）"
            )
            stop()
            stop()  # 幂等
            hb_after_stop = row1["heartbeat_at"]
            time.sleep(1.4)
            row2 = _lock_row(lock_key)
            assert row2["heartbeat_at"] == hb_after_stop, "stop() 后不得再续约"
        finally:
            task_lock.release(lock_key, owner)

    def test_keepalive_stops_quietly_when_lock_lost(self):
        """锁已释放后心跳续约失败：只告警停止，不得向业务线程抛异常。"""
        owner = task_lock.make_owner_id("export", 900655)
        rec = task_lock.acquire("export", project_id=900655, owner_id=owner)
        lock_key = rec.get("lock_key", "")
        task_lock.release(lock_key, owner)
        row0 = _lock_row(lock_key)
        stop = task_lock.start_keepalive(lock_key, owner, interval_seconds=1)
        try:
            time.sleep(1.6)
            row1 = _lock_row(lock_key)
            assert row1["heartbeat_at"] == row0["heartbeat_at"], (
                "已释放锁不得被 _touch 复活（status='active' 条件必须生效）"
            )
        finally:
            stop()


class TestKeepaliveContract:
    """三条导出链路必须都接入续约；源码契约防止未来单侧回退。"""

    def test_sync_route_wired(self):
        from app.routers.export import export_docx

        src = inspect.getsource(export_docx)
        assert "start_keepalive(lock_key, owner_id)" in src
        assert "_stop_keepalive()" in src

    def test_sse_route_wired(self):
        from app.routers.export import export_docx_stream

        src = inspect.getsource(export_docx_stream)
        assert "start_keepalive(lock_key, owner_id)" in src
        # 先停续约再释放锁，避免交错（_run_build finally 内）
        assert src.index("_stop_keepalive()") < src.index("_task_lock.release(lock_key, owner_id)")

    def test_celery_task_wired(self):
        tasks_file = Path(inspect.getfile(sys_modules_tasks())).read_text(encoding="utf-8")
        assert "start_keepalive" in tasks_file
        assert "_stop_keepalive()" in tasks_file


def sys_modules_tasks():
    import app.tasks as _t
    return _t


# ============================================================
# T5：内容指纹纳入图片文件状态
# ============================================================


class TestFingerprintImages:
    def test_image_change_invalidates_fingerprint(self, tmp_path, monkeypatch):
        """同名图片被替换（size/mtime 变）→ 指纹必须变（旧缺陷：命中旧缓存 DOCX，成品仍是旧图）。"""
        monkeypatch.setattr(export_mod, "PROJECTS_DATA_DIR", tmp_path)
        pid = _new_user_project_with_section("T5指纹-项目")
        fp1 = export_mod._compute_content_fingerprint(pid)
        assert fp1

        img_dir = tmp_path / str(pid) / "images"
        img_dir.mkdir(parents=True)
        (img_dir / "chart_1.png").write_bytes(b"old-image-bytes")
        fp2 = export_mod._compute_content_fingerprint(pid)
        assert fp2 != fp1, "新增图片文件必须让指纹变化"

        time.sleep(0.02)
        (img_dir / "chart_1.png").write_bytes(b"new-image-bytes-longer")
        fp3 = export_mod._compute_content_fingerprint(pid)
        assert fp3 != fp2, "同名图片替换（mtime+size 变）必须让指纹变化"

    def test_no_images_dir_backward_compatible(self, tmp_path, monkeypatch):
        """无 images 目录 / 空目录：指纹不变（历史项目零影响，向后兼容）。"""
        monkeypatch.setattr(export_mod, "PROJECTS_DATA_DIR", tmp_path)
        pid = _new_user_project_with_section("T5兼容-项目")
        fp1 = export_mod._compute_content_fingerprint(pid)
        (tmp_path / str(pid) / "images").mkdir(parents=True)
        fp2 = export_mod._compute_content_fingerprint(pid)
        assert fp2 == fp1, "空 images 目录不得改变指纹"


# ============================================================
# T2：失败导出落库 + 查询/下载过滤
# ============================================================


class TestFailedExportRecord:
    def test_record_failed_export_row(self):
        pid = _new_project("T2落库-项目", created_by=OWNER)
        fid = export_mod.record_failed_export(pid, "图表渲染炸裂：mock", ExportConfig())
        assert fid
        with get_db() as conn:
            row = conn.execute("SELECT * FROM exports WHERE id = ?", (fid,)).fetchone()
        assert row["status"] == "failed"
        assert row["file_path"] == ""
        assert row["file_size"] == 0
        assert "导出失败" in row["filename"] and "图表渲染炸裂" in row["filename"]

    def test_latest_not_polluted_by_failed_row(self):
        """失败行时间更新，但 latest 只能取 completed 行（否则 /latest 拿空路径下载）。"""
        pid = _new_project("T2latest-项目", created_by=OWNER)
        ok_id = _insert_export_row(pid, f"x/{pid}/good.docx", "completed")
        export_mod.record_failed_export(pid, "后发生的失败")
        info = export_mod.get_latest_export_info(pid)
        assert info and info["id"] == ok_id

    def test_download_rejects_failed_row(self):
        from app.routers.export import download_export

        pid = _new_project("T2下载-项目", created_by=OWNER)
        fid = export_mod.record_failed_export(pid, "失败记录不可下载")
        with pytest.raises(HTTPException) as ei:
            asyncio.run(download_export(pid, fid, identity=User(OWNER, "o", "user")))
        assert ei.value.status_code == 404

    def test_sync_export_failure_writes_failed_row(self, monkeypatch):
        """同步链路 build_docx 抛错 → 响应 500 的同时必须出现 failed 行（修复前失败无痕）。"""
        from app.routers.export import export_docx

        pid = _new_user_project_with_section("T2同步失败-项目")

        def _boom(*a, **kw):
            raise RuntimeError("mock 构建失败")

        monkeypatch.setattr("app.routers.export.build_docx", _boom)
        with pytest.raises(HTTPException) as ei:
            asyncio.run(export_docx(pid, ExportConfig(), identity=User(OWNER, "o", "user")))
        assert ei.value.status_code == 500
        with get_db() as conn:
            row = conn.execute(
                "SELECT status, filename FROM exports WHERE project_id = ? ORDER BY id DESC LIMIT 1",
                (pid,),
            ).fetchone()
        assert row and row["status"] == "failed"
        assert "mock 构建失败" in row["filename"]

    def test_sse_and_celery_failure_recorded_contract(self):
        """SSE _run_build finally 与 Celery except 分支的失败落库源码契约。"""
        from app.routers.export import export_docx_stream

        src = inspect.getsource(export_docx_stream)
        assert "_record_export_failure(project_id, config, error_detail)" in src
        tasks_src = Path(inspect.getfile(sys_modules_tasks())).read_text(encoding="utf-8")
        assert "record_failed_export" in tasks_src


# ============================================================
# T3：SSE 事件 journal 与断线续传
# ============================================================


class TestStreamJournalUnit:
    def test_append_seq_monotonic(self):
        from app.routers.export import _StreamJournal

        j = _StreamJournal("s1", 1, queue.Queue())
        assert j.append({"event": "connected"}) == 1
        assert j.append({"event": "progress"}) == 2
        assert j.events[0][1]["event"] == "connected"

    def test_sse_frame_id_line_and_keepalive_without_id(self):
        from app.routers.export import _sse_frame

        frame = _sse_frame(7, "progress", {"event": "progress", "n": 1})
        assert frame.startswith("id: 7\nevent: progress\ndata: ")
        assert frame.endswith("\n\n")
        assert "id:" not in _sse_frame(None, "keep_alive", {"event": "keep_alive"})

    def test_drain_consumes_queue_until_sentinel(self):
        from app.routers.export import _StreamJournal, _drain_queue_to_journal

        q: queue.Queue = queue.Queue()
        j = _StreamJournal("s2", 1, q)
        q.put({"event": "progress"})
        q.put({"event": "completed", "file_path": "/x"})
        q.put(None)
        _drain_queue_to_journal(j)
        deadline = time.time() + 5
        while not j.terminal and time.time() < deadline:
            time.sleep(0.02)
        assert j.terminal, "哨兵后 journal 必须封存为终态"
        assert [seq for seq, _ in j.events] == [1, 2]
        assert j.events[1][1]["event"] == "completed"

    def test_register_evicts_expired(self, monkeypatch):
        from app.routers import export as er

        q: queue.Queue = queue.Queue()
        j = er._StreamJournal("s3", 1, q)
        j.mark_terminal()
        monkeypatch.setattr(j, "finished_at", time.time() - er._STREAM_JOURNAL_TTL_SECONDS - 1)
        er._register_stream_journal(j)
        assert "s3" not in er._STREAM_JOURNALS, "过期 journal 必须在注册时被 GC"


class TestSseDisconnectResume:
    """SSE 断线后 resume 全链路测试（含超量淘汰隔离 fixture）。

    STABILITY 2026-10-12（加固）：前序测试累积的 _STREAM_JOURNALS 条目
    可能超过 _STREAM_JOURNAL_MAX=50 上限，popitem(last=False) 会把本测试
    刚注册的 journal 挤掉 → journal.terminal 永远为 False → 测试超时失败。
    加 autouse fixture 每次测试前清空 journal dict，隔离测试间状态泄漏。
    """

    @pytest.fixture(autouse=True)
    def _isolate_stream_journals(self):
        from app.routers.export import _STREAM_JOURNALS
        _STREAM_JOURNALS.clear()
        yield
        _STREAM_JOURNALS.clear()

    def test_disconnect_drains_journal_and_resume_replays(self):
        """全链路：真实流 → 第 2 次轮询判失联 → drain 接管 → 终态入 journal →
        resume 携 last_event_id 只回放未收事件直到 completed。"""
        from app.routers.export import (
            _STREAM_JOURNALS,
            export_docx_stream,
            export_docx_stream_resume,
        )

        pid = _new_user_project_with_section("T3续传-项目")

        class _FlakyRequest:
            def __init__(self):
                self.calls = 0

            async def is_disconnected(self):
                self.calls += 1
                return self.calls > 1  # 首个轮询保持，第二个判失联

        frames1: list[str] = []

        async def _collect():
            resp = await export_docx_stream(
                _FlakyRequest(), pid, ExportConfig(), identity=User(OWNER, "o", "user")
            )
            async for chunk in resp.body_iterator:
                frames1.append(chunk)

        asyncio.run(_collect())
        joined = "".join(frames1)
        assert "event: connected" in joined
        # connected 帧必须携 id 行与 stream_id（前端续传游标的来源）
        assert "id: 1\n" in joined
        sid = None
        for line in joined.splitlines():
            if line.startswith("data: "):
                payload = json.loads(line[6:])
                if payload.get("event") == "connected":
                    sid = payload.get("stream_id")
        assert sid and sid in _STREAM_JOURNALS, "connected 事件的 stream_id 必须已注册 journal"

        journal = _STREAM_JOURNALS[sid]
        deadline = time.time() + 120
        while not journal.terminal and time.time() < deadline:
            time.sleep(0.05)
        assert journal.terminal, "断线后 journal 应在 drain 接管下于构建结束时封存"
        events = [item for _, item in journal.events]
        terminal_events = [e for e in events if e.get("event") in ("completed", "error")]
        assert terminal_events, f"journal 缺少终态事件: {[e.get('event') for e in events]}"
        assert terminal_events[0]["event"] == "completed"

        last_id = max(
            (int(n) for f in frames1 for n in __import__("re").findall(r"id: (\d+)", f)),
            default=0,
        )
        replayed: list[str] = []

        async def _resume():
            resp = await export_docx_stream_resume(
                _FakeRequest(), pid, sid, last_event_id=last_id,
                identity=User(OWNER, "o", "user"),
            )
            async for chunk in resp.body_iterator:
                replayed.append(chunk)
                if "event: completed" in chunk or "event: error" in chunk:
                    break

        asyncio.run(_resume())
        rj = "".join(replayed)
        assert rj, "resume 端点必须回放事件（终态已入 journal）"
        assert "event: completed" in rj
        import re

        for n in re.findall(r"id: (\d+)", rj):
            assert int(n) > last_id, f"续传不得重复投递已收事件（id={n} <= {last_id}）"

    def test_resume_unknown_stream_404(self):
        """未知/过期 stream_id → 404（多 worker 部署命中不到时前端按历史兜底）。"""
        from app.routers.export import export_docx_stream_resume

        pid = _new_project("T3-404-项目", created_by=OWNER)
        with pytest.raises(HTTPException) as ei:
            asyncio.run(export_docx_stream_resume(
                _FakeRequest(), pid, "no-such-stream", identity=User(OWNER, "o", "user")
            ))
        assert ei.value.status_code == 404

    def test_resume_project_mismatch_404(self):
        """journal 属于项目 A，用项目 B 的请求续传 → 404（防跨项目偷听事件流）。"""
        from app.routers.export import _StreamJournal, _register_stream_journal, export_docx_stream_resume

        q: queue.Queue = queue.Queue()
        j = _StreamJournal("t3-mismatch", 555555, q)
        j.append({"event": "connected"})
        _register_stream_journal(j)
        other_pid = _new_project("T3-mismatch-项目", created_by=OWNER)
        assert other_pid != 555555
        with pytest.raises(HTTPException) as ei:
            asyncio.run(export_docx_stream_resume(
                _FakeRequest(), other_pid, "t3-mismatch", identity=User(OWNER, "o", "user")
            ))
        assert ei.value.status_code == 404


# ============================================================
# T1：PDF 导出（LibreOffice headless，优雅降级）
# ============================================================


class TestFindSoffice:
    def test_env_path_priority(self, tmp_path, monkeypatch):
        fake = tmp_path / "soffice.exe"
        fake.write_text("fake")
        monkeypatch.setattr(pdf_export, "_cached_soffice", None)
        monkeypatch.setattr(pdf_export, "_probe_done", False)
        monkeypatch.setenv("PDF_SOFFICE_PATH", str(fake))
        try:
            assert pdf_export.find_soffice(force_refresh=True) == str(fake)
        finally:
            monkeypatch.delenv("PDF_SOFFICE_PATH", raising=False)
            monkeypatch.setattr(pdf_export, "_cached_soffice", None)
            monkeypatch.setattr(pdf_export, "_probe_done", False)

    def test_probe_none_when_nothing_found(self, monkeypatch):
        monkeypatch.setattr(pdf_export, "_cached_soffice", None)
        monkeypatch.setattr(pdf_export, "_probe_done", False)
        monkeypatch.delenv("PDF_SOFFICE_PATH", raising=False)
        monkeypatch.setattr(pdf_export.shutil, "which", lambda name: None)
        monkeypatch.setattr(pdf_export, "_SOFFICE_CANDIDATES", ())
        try:
            assert pdf_export.find_soffice(force_refresh=True) is None
        finally:
            monkeypatch.setattr(pdf_export, "_cached_soffice", None)
            monkeypatch.setattr(pdf_export, "_probe_done", False)


class TestConvertDocxToPdf:
    def test_unavailable_when_no_soffice(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pdf_export, "find_soffice", lambda **kw: None)
        with pytest.raises(pdf_export.PdfUnavailable) as ei:
            pdf_export.convert_docx_to_pdf(tmp_path / "a.docx")
        assert "LibreOffice" in str(ei.value)

    def test_idempotent_reuse_newer_pdf(self, monkeypatch, tmp_path):
        """同名 .pdf 不早于源文件 → 直接复用，绝不重复起子进程。"""
        docx = tmp_path / "a.docx"
        docx.write_bytes(b"docx")
        pdf = tmp_path / "a.pdf"
        pdf.write_bytes(b"pdf")
        # 2026-10-11 加固：pdf_export 改为「严格 > 才复用」后，粗粒度 mtime
        # 文件系统（同秒写入）会让 pdf.mtime == docx.mtime → 误触发 subprocess。
        # 显式把 pdf 的 mtime 设到 docx 之后，消除文件系统时间粒度竞态。
        _future = docx.stat().st_mtime + 5
        import os as _os
        _os.utime(pdf, (_future, _future))
        monkeypatch.setattr(pdf_export, "find_soffice", lambda **kw: "soffice")

        def _never(*a, **kw):
            raise AssertionError("缓存命中时不得调用 subprocess")

        monkeypatch.setattr(pdf_export.subprocess, "run", _never)
        assert pdf_export.convert_docx_to_pdf(docx) == pdf

    def test_equal_mtime_reconverts_stale_pdf(self, monkeypatch, tmp_path):
        """BUG-1 回归（2026-10-11）：pdf.mtime == docx.mtime 必须重新转换。

        这是真实可达的陈旧 PDF 场景：build_docx 以秒级时间戳命名且可原地覆盖，
        在 FAT/exFAT（2s 取整）等粗粒度 mtime 文件系统上，「旧 PDF 与新 DOCX
        落得相同 mtime」时，若判据用 `>=` 会把上一版内容的 PDF 当成最新交付件
        返回。严格 `>` 判据下，相等即视为「无法证明 PDF 新于 DOCX」→ 重新转换。
        """
        docx = tmp_path / "e.docx"
        docx.write_bytes(b"new content")
        pdf = tmp_path / "e.pdf"
        pdf.write_bytes(b"stale pdf")
        # 把 pdf 与 docx 的 mtime 强制设为完全相等（模拟粗粒度 FS 上的歧义态）
        import os as _os
        _same = docx.stat().st_mtime
        _os.utime(pdf, (_same, _same))
        _os.utime(docx, (_same, _same))
        monkeypatch.setattr(pdf_export, "find_soffice", lambda **kw: "soffice")

        converted = {"n": 0}

        def _fake_run(cmd, **kw):
            converted["n"] += 1
            pdf.write_bytes(b"freshly converted pdf")  # 模拟 LibreOffice 覆写产物
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        monkeypatch.setattr(pdf_export.subprocess, "run", _fake_run)
        out = pdf_export.convert_docx_to_pdf(docx)
        assert converted["n"] == 1, "mtime 相等时不得复用陈旧 PDF，必须重新转换"
        assert out == pdf

    def test_success_invokes_soffice_with_isolated_profile(self, monkeypatch, tmp_path):
        docx = tmp_path / "b.docx"
        docx.write_bytes(b"docx")
        target = tmp_path / "b.pdf"
        monkeypatch.setattr(pdf_export, "find_soffice", lambda **kw: "soffice")
        captured = {}

        def _fake_run(cmd, **kw):
            captured["cmd"] = [str(c) for c in cmd]
            target.write_bytes(b"pdf!")
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        monkeypatch.setattr(pdf_export.subprocess, "run", _fake_run)
        out = pdf_export.convert_docx_to_pdf(docx)
        assert out == target
        assert any(str(c).startswith("-env:UserInstallation=") for c in captured["cmd"]), (
            "必须使用独立 UserInstallation profile（并发多转换时共享 profile 会静默失败）"
        )
        assert "--headless" in captured["cmd"] and "--convert-to" in captured["cmd"]

    def test_no_output_raises_runtime_error(self, monkeypatch, tmp_path):
        docx = tmp_path / "c.docx"
        docx.write_bytes(b"docx")
        monkeypatch.setattr(pdf_export, "find_soffice", lambda **kw: "soffice")
        monkeypatch.setattr(
            pdf_export.subprocess, "run",
            lambda cmd, **kw: SimpleNamespace(returncode=1, stdout=b"", stderr=b"fail"),
        )
        with pytest.raises(RuntimeError):
            pdf_export.convert_docx_to_pdf(docx)

    def test_timeout_raises_runtime_error(self, monkeypatch, tmp_path):
        docx = tmp_path / "d.docx"
        docx.write_bytes(b"docx")
        monkeypatch.setattr(pdf_export, "find_soffice", lambda **kw: "soffice")

        def _timeout(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 1)

        monkeypatch.setattr(pdf_export.subprocess, "run", _timeout)
        with pytest.raises(RuntimeError, match="超时"):
            pdf_export.convert_docx_to_pdf(docx)


class TestPdfEndpoint:
    def _make_docx_row(self, tmp_path, monkeypatch, status="completed"):
        import app.routers.export as er

        base = tmp_path / "data"
        base.mkdir(exist_ok=True)
        monkeypatch.setattr(er, "PROJECTS_DATA_DIR", base)
        pid = _new_project("T1端点-项目", created_by=OWNER)
        docx = base / str(pid) / "out.docx"
        docx.parent.mkdir(parents=True, exist_ok=True)
        docx.write_bytes(b"PK fake docx")
        eid = _insert_export_row(pid, str(docx), status)
        return er, pid, eid

    def test_501_when_soffice_missing(self, tmp_path, monkeypatch):
        from app.routers.export import export_pdf

        _er, pid, eid = self._make_docx_row(tmp_path, monkeypatch)

        def _unavail(p, **kw):
            raise pdf_export.PdfUnavailable("未检测到 LibreOffice")

        monkeypatch.setattr(pdf_export, "convert_docx_to_pdf", _unavail)
        with pytest.raises(HTTPException) as ei:
            asyncio.run(export_pdf(pid, eid, identity=User(OWNER, "o", "user")))
        assert ei.value.status_code == 501
        assert "LibreOffice" in ei.value.detail

    def test_404_for_failed_row(self, tmp_path, monkeypatch):
        from app.routers.export import export_pdf

        _er, pid, eid = self._make_docx_row(tmp_path, monkeypatch, status="failed")
        with pytest.raises(HTTPException) as ei:
            asyncio.run(export_pdf(pid, eid, identity=User(OWNER, "o", "user")))
        assert ei.value.status_code == 404

    def test_403_for_path_escape(self, tmp_path, monkeypatch):
        import app.routers.export as er

        base = tmp_path / "data"
        base.mkdir(exist_ok=True)
        monkeypatch.setattr(er, "PROJECTS_DATA_DIR", base)
        pid = _new_project("T1越界-项目", created_by=OWNER)
        outside = tmp_path / "secret.docx"
        outside.write_bytes(b"outside")
        eid = _insert_export_row(pid, str(outside))
        from app.routers.export import export_pdf

        with pytest.raises(HTTPException) as ei:
            asyncio.run(export_pdf(pid, eid, identity=User(OWNER, "o", "user")))
        assert ei.value.status_code == 403

    def test_400_for_non_word_suffix(self, tmp_path, monkeypatch):
        from app.routers.export import export_pdf

        _er, pid, _eid = self._make_docx_row(tmp_path, monkeypatch)
        base = _er.PROJECTS_DATA_DIR
        txt = base / str(pid) / "note.txt"
        txt.write_bytes(b"txt")
        bad_id = _insert_export_row(pid, str(txt))
        with pytest.raises(HTTPException) as ei:
            asyncio.run(export_pdf(pid, bad_id, identity=User(OWNER, "o", "user")))
        assert ei.value.status_code == 400

    def test_200_returns_pdf_file_response(self, tmp_path, monkeypatch):
        from app.routers.export import export_pdf

        _er, pid, eid = self._make_docx_row(tmp_path, monkeypatch)

        def _fake_convert(p, **kw):
            out = Path(p).with_suffix(".pdf")
            out.write_bytes(b"%PDF-1.4 fake")
            return out

        monkeypatch.setattr(pdf_export, "convert_docx_to_pdf", _fake_convert)
        resp = asyncio.run(export_pdf(pid, eid, identity=User(OWNER, "o", "user")))
        assert resp.media_type == "application/pdf"
        assert str(resp.path).endswith(".pdf")

    def test_routes_registered_on_openapi(self):
        """T1/T3 新端点必须出现在 OpenAPI（app.routes 惰性挂载无 path，不可用）。"""
        from app.main import app

        paths = set(app.openapi()["paths"].keys())
        assert "/api/v1/projects/{project_id}/export/pdf/{export_id}" in paths
        assert "/api/v1/projects/{project_id}/export/docx-stream/resume/{stream_id}" in paths


# ============================================================
# T6：无界队列闭环结论
# ============================================================


class TestQueueUnboundedDecision:
    def test_unbounded_decision_documented_and_defense_kept(self):
        """闭环结论：故意无界（有界化会真实丢终态事件）；queue.Full 保留为防御分支。"""
        from app.routers.export import export_docx_stream

        src = inspect.getsource(export_docx_stream)
        assert "故意保持无界" in src, "T6 闭环决策注释必须在位"
        assert "queue.Full" in src
        assert "queue.Queue()" in src, "队列必须保持无界构造（有界化=丢终态事件，见注释）"
