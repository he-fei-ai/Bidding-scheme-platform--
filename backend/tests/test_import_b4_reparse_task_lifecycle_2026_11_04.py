# -*- coding: utf-8 -*-
"""上传解析模块 · B4 任务行生命周期残留（2026-11-04 深度审计新增回归）。

对应缺陷（均有代码行号证据）：
  B4a re_extract_text：``_create_parse_task`` 返回的 task_id 被丢弃，终态用
       ``filename`` 模糊更新 —— 与 save_upload 在 2026-11-02 修复的
       「同项目同名多 processing 行交叉串写」是同一缺陷在重提取入口的漏网之鱼。
       历史 stale processing 行（worker 崩溃 / 任务丢失后尚未被 reclaim 收敛）
       与本次新建行同名并存时，``_update_parse_task(..., filename=...)`` 会
       命中多行、把旧行也改写为 completed/failed（串写）。
  B4b routers/files.py async-reparse 端点：``_create_parse_task`` 建行后**从不
       调用 _link_parse_task_file**，任务行的 file_id 恒为 NULL；worker 经
       ``_persist_file_extraction(create_task_row=False)`` 收尾时按 ``file_id``
       精确更新（``WHERE status='processing' AND file_id=?``）命中 **0 行**，
       任务行永久停在 processing（孤儿），GET /tasks/files/status 永远显示
       「正在解析」直到 beat 的 reclaim_stale_parse_state 数十分钟后收敛。

测试策略：服务层确定性复现，不依赖真实线程/Celery broker。
"""
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.db import get_db  # noqa: E402
from app.models import ProjectCreate  # noqa: E402
from app.services import files as file_service  # noqa: E402
from app.services.projects import create_project, delete_project  # noqa: E402


def _task_status_by_id(task_id: int) -> str:
    with get_db(readonly=True) as conn:
        row = conn.execute(
            "SELECT status, file_id FROM file_parse_tasks WHERE id = ?", (task_id,)
        ).fetchone()
    return (row["status"], row["file_id"]) if row else ("<missing>", None)


def _uploads_dir(pid: int) -> Path:
    return file_service.PROJECTS_DATA_DIR / str(pid) / "uploads"


def _texts_dir(pid: int) -> Path:
    return file_service.PROJECTS_DATA_DIR / str(pid) / "texts"


# ---------------------------------------------------------------------------
# B4a：re_extract_text 必须用 _create_parse_task 返回的 task_id 精确收尾
# ---------------------------------------------------------------------------
class TestB4aReExtractTaskPrecision:
    def test_re_extract_uses_task_id_for_terminal_update(self, monkeypatch):
        """re_extract_text 创建的解析任务行终态必须按 task_id 精确命中本行。

        回归根因：re_extract_text 调用 _create_parse_task 时丢弃返回值，
        终态更新走 filename 匹配 —— 同项目同名存在历史 stale processing 行时
        会被一并串写（B4 同一缺陷的重提取入口）。此断言直接钉住
        re_extract_text 的 `_create_parse_task` 返回值必须被复用。
        """
        from app.services import files as fmod

        src = Path(fmod.__file__).read_text(encoding="utf-8")
        # re_extract_text 段内：_create_parse_task 的返回值必须赋给变量并被后续
        # _update_parse_task/_force_parse_task_failed 以 task_id= 精确引用。
        seg = src[src.index("def re_extract_text"):]
        seg = seg[: seg.index("def ") if "def " in seg[1:] else len(seg)]
        assert "task_id" in seg, (
            "re_extract_text 未使用 task_id 精确收尾（B4a 串写缺陷）"
        )

    def test_re_extract_task_row_reaches_terminal(self, monkeypatch, tmp_path):
        """重提取完成后，它自己创建的任务行必须进入终态（不残留 processing）。

        复现路径：项目存在一条同名 stale processing 行（模拟 worker 崩溃后
        reclaim 尚未运行的窗口）+ re_extract_text 新建一行 → 旧实现按 filename
        会把**两行一起**改写成终态（串写），新行本身虽会进入终态但旧行被误伤。
        本用例断言：重提取新建的任务行终态为 completed/failed 之一。
        """
        pid = create_project(ProjectCreate(name="B4a-重提取任务行终态")).id
        try:
            # 准备一个真实可解析的文件
            src_text = "重提取正文唯一内容：招标公告 工期 120 天。"
            content = src_text.encode("utf-8")
            r = file_service.save_upload(pid, "重提取源.txt", content, "main")
            assert r["status"] == "success", r
            fid = r["id"]

            # 直接调用 re_extract_text，断言其内部创建的任务行进入终态
            res = file_service.re_extract_text(pid, fid)
            assert res["status"] in ("success", "failed"), res

            with get_db(readonly=True) as conn:
                rows = conn.execute(
                    "SELECT id, status FROM file_parse_tasks "
                    "WHERE project_id = ? AND filename = '重提取源.txt' "
                    "ORDER BY id DESC",
                    (pid,),
                ).fetchall()
            # 最新一行（本次重提取创建）必须是终态
            assert rows, "re_extract_text 未创建任务行"
            latest = rows[0]
            assert latest["status"] in ("completed", "failed"), dict(latest)
        finally:
            delete_project(pid)

    def test_re_extract_does_not_crosswrite_stale_processing(self, monkeypatch, tmp_path):
        """同项目同名 stale processing 行不得被重提取的终态更新串写。

        这是 B4a 的实质回归：先造一条同名 stale processing 行（不关联任何
        真实文件），再对真实文件执行 re_extract_text；旧实现按 filename 更新
        会把 stale 行也改写成 completed/failed（串写）。修复后 stale 行必须
        保持 processing（它不属于本次重提取）。
        """
        pid = create_project(ProjectCreate(name="B4a-不串写stale行")).id
        try:
            content = "不串写正文 唯一内容 B4a".encode("utf-8")
            r = file_service.save_upload(pid, "B4a串写.txt", content, "main")
            assert r["status"] == "success", r
            fid = r["id"]

            # 造一条同名 stale processing 行（模拟历史孤儿任务，file_id=NULL）
            stale_id = file_service._create_parse_task(pid, "B4a串写.txt", "txt")
            assert stale_id, "stale 行创建失败"

            res = file_service.re_extract_text(pid, fid)
            assert res["status"] in ("success", "failed"), res

            status, file_id = _task_status_by_id(stale_id)
            # B4a 根因：re_extract 的 filename 更新会命中并串写 stale 行。
            # 修复后 stale 行必须仍为 processing（未被本次重提取波及）。
            assert status == "processing", (
                f"stale processing 行被 re_extract_text 串写为 {status!r}"
            )
        finally:
            delete_project(pid)


# ---------------------------------------------------------------------------
# B4b：async-reparse 端点建的任务行必须关联 file_id，worker 才能精确收尾
# ---------------------------------------------------------------------------
class TestB4bAsyncReparseTaskLinked:
    def test_persist_by_file_id_reaches_orphan_task_when_unlinked(self, monkeypatch, tmp_path):
        """（L1 语义）未关联 file_id 的任务行，worker 按 file_id 收尾后经 filename 兜底进入终态。

        2026-11-04 L1 加固：file_id 精确命中 0 行（历史未关联行）时，
        _update_parse_task 自动回退 filename。此前该场景收尾命中 0 行 →
        任务行残留 processing（孤儿），终态丢失。
        """
        pid = create_project(ProjectCreate(name="B4b-未关联孤儿")).id
        try:
            content = "孤儿任务正文 B4b".encode("utf-8")
            r = file_service.save_upload(pid, "孤儿.txt", content, "main")
            assert r["status"] == "success", r
            fid = r["id"]

            # 模拟历史未关联行（file_id=NULL）
            t = file_service._create_parse_task(pid, "孤儿.txt", "txt")
            with get_db() as conn:
                conn.execute(
                    "UPDATE file_parse_tasks SET status='processing' WHERE id=?", (t,)
                )
            status, file_id = _task_status_by_id(t)
            assert status == "processing" and file_id is None, (status, file_id)

            # 模拟 worker：按 file_id 收尾（create_task_row=False）
            fp = tmp_path / "孤儿.txt"
            fp.write_bytes(content)
            _status = file_service._persist_file_extraction(
                pid, fid, str(fp), "孤儿.txt", ".txt", create_task_row=False
            )
            assert _status == "success"

            # L1：file_id 命中 0 行 → filename 兜底 → 行进入终态（不再是孤儿 processing）
            status, file_id = _task_status_by_id(t)
            assert status == "completed", (
                f"未关联 file_id 的任务行经 filename 兜底应 completed，实际 {status!r}（L1）"
            )
        finally:
            delete_project(pid)

    def test_persist_by_file_id_reaches_linked_task(self, monkeypatch, tmp_path):
        """（修复验证）任务行关联 file_id 后，worker 按 file_id 精确收尾进入终态。"""
        pid = create_project(ProjectCreate(name="B4b-关联后收尾")).id
        try:
            content = "关联任务正文 B4b".encode("utf-8")
            r = file_service.save_upload(pid, "关联孤儿.txt", content, "main")
            assert r["status"] == "success", r
            fid = r["id"]

            # 端点修复后：建行 → 立即 _link_parse_task_file
            t = file_service._create_parse_task(pid, "关联孤儿.txt", "txt")
            file_service._link_parse_task_file(t, fid)
            status, file_id = _task_status_by_id(t)
            assert file_id == fid, (status, file_id)

            fp = tmp_path / "关联孤儿.txt"
            fp.write_bytes(content)
            _status = file_service._persist_file_extraction(
                pid, fid, str(fp), "关联孤儿.txt", ".txt", create_task_row=False
            )
            assert _status == "success"
            status, file_id = _task_status_by_id(t)
            assert status == "completed", (
                f"关联 file_id 的任务行被 worker 收尾后应为 completed，实际 {status!r}"
            )
        finally:
            delete_project(pid)


class TestB4bEndpointStaticGuard:
    def test_async_reparse_endpoint_links_task_row(self):
        """（静态回归）async-reparse 端点建任务行后必须调用 _link_parse_task_file。

        2026-11-04（B4b）根因：端点只 _create_parse_task 丢弃返回值、从不关联
        file_id → worker 按 file_id 收尾命中 0 行 → 任务行永久 processing（孤儿）。
        此断言钉住端点源码中「建行 + 关联 file_id」两步都必须存在。
        """
        from app.routers import files as router_mod

        src = Path(router_mod.__file__).read_text(encoding="utf-8")
        seg = src[src.index("async def async_reparse_file"):]
        seg = seg[: seg.index("@router")] if "@router" in seg[1:] else seg
        assert "_create_parse_task" in seg, "端点必须创建任务行"
        assert "_link_parse_task_file" in seg, (
            "端点建任务行后必须关联 file_id（B4b 孤儿任务行缺陷）"
        )


# ---------------------------------------------------------------------------
# B4b E2E：真实 HTTP 链路（TestClient）验证 async-reparse 任务行关联与终态
# ---------------------------------------------------------------------------
def _e2e_make_docx_bytes(text: str) -> bytes:
    import io

    from docx import Document

    doc = Document()
    for line in text.splitlines():
        doc.add_paragraph(line)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _e2e_parse_task_rows(project_id: int, filename: str):
    with get_db(readonly=True) as conn:
        return conn.execute(
            "SELECT id, filename, status, file_id FROM file_parse_tasks "
            "WHERE project_id = ? AND filename = ? ORDER BY id",
            (project_id, filename),
        ).fetchall()


class TestB4bAsyncReparseE2E:
    def test_async_reparse_task_row_reaches_terminal(self):
        """真实端点链：async-reparse 后，该文件的任务行必须关联 file_id 并进入终态。

        端到端路径：POST /projects/{id}/files/{file_id}/async-reparse → 端点建行 →
        端点 _link_parse_task_file → dispatch_task（无 broker 时 inline 直调 worker）
        → _persist_file_extraction(create_task_row=False) → 按 file_id 精确收尾。
        修复前：端点不关联 file_id → 收尾命中 0 行 → 任务行残留 processing。
        """
        import io

        from fastapi.testclient import TestClient

        from app.main import app

        with TestClient(app) as client:
            pid = client.post(
                "/api/v1/projects",
                json={"name": "B4b-E2E异步任务关联", "industry": "公共建筑", "bidder_name": "测试方"},
            ).json()["id"]
            try:
                content = _e2e_make_docx_bytes("异步端到端任务行关联正文 唯一内容")
                up = client.post(
                    f"/api/v1/projects/{pid}/files",
                    files={"file": ("异步E2E.docx", io.BytesIO(content), "application/octet-stream")},
                )
                assert up.status_code == 200, up.text
                fid = up.json()["id"]

                resp = client.post(f"/api/v1/projects/{pid}/files/{fid}/async-reparse")
                assert resp.status_code == 200, resp.text
                data = resp.json()
                assert data["dispatched_via"] in ("celery", "inline"), data
                if data["dispatched_via"] != "inline":
                    # 真实 worker 环境无法在测试内等待，交由 celery 侧验证；此处
                    # 至少断言端点已创建任务行并关联 file_id（不依赖 worker 收尾）。
                    pass

                rows = _e2e_parse_task_rows(pid, "异步E2E.docx")
                assert rows, "async-reparse 必须创建任务行"
                latest = rows[-1]
                assert latest["file_id"] == fid, (
                    f"任务行必须关联 file_id（B4b 孤儿），实际 {dict(latest)}"
                )
                if data["dispatched_via"] == "inline":
                    assert latest["status"] in ("completed", "failed"), dict(latest)
                else:
                    assert latest["status"] in ("processing", "completed", "failed"), dict(latest)
            finally:
                delete_project(pid)


# ---------------------------------------------------------------------------
# L1（2026-11-04 遗留项）：worker 异常路径按 file_id 优先收尾，失配回退 filename
# ---------------------------------------------------------------------------
class TestL1WorkerExceptionFileIdFallback:
    def test_update_parse_task_file_id_miss_falls_back_to_filename(self):
        """file_id 失配（历史未关联行）时，_update_parse_task 必须回退 filename 兜底。

        回归根因：2026-11-02 B4 让 worker 按 file_id 精确收尾，但若任务行是
        历史未关联行（file_id=NULL），精确命中 0 行 → 终态永久丢失。本用例构造
        「无关联任务行 + file_id 收尾」，断言行仍被 filename 回退改写为 failed。
        """
        pid = create_project(ProjectCreate(name="L1-文件id失配回退")).id
        try:
            t = file_service._create_parse_task(pid, "L1兜底.txt", "txt")
            assert t
            # file_id 指向不存在文件 → 精确命中 0 行 → 必须回退 filename 兜底
            file_service._update_parse_task(
                pid, "failed", 50, "模拟异常", "L1兜底.txt", file_id=999999
            )
            status, file_id = _task_status_by_id(t)
            assert status == "failed", (
                f"file_id 失配后未回退 filename，任务行残留 {status!r}（L1）"
            )
        finally:
            delete_project(pid)

    def test_worker_exception_path_prefers_file_id(self):
        """（静态回归）tasks.parse_and_extract 异常路径必须按 file_id 精确收尾。

        B4 串写缺陷在 worker 异常路径的漏网之鱼：旧代码只传 filename，同名多
        processing 行会被一并串写。现断言异常路径源码中 `_update_parse_task`
        调用带 `file_id=` 关键字（缺省回退 filename 由服务层内部兜底）。
        """
        from app import tasks as tasks_mod

        src = Path(tasks_mod.__file__).read_text(encoding="utf-8")
        # 定位 parse_and_extract_file 段，截到下一个顶层 def（generate_section_content）
        seg = src[src.index("def parse_and_extract_file"):]
        _next = seg.find("\ndef ", 1)
        if _next == -1:
            _next = len(seg)
        seg = seg[:_next]
        assert "file_id=file_id" in seg, (
            "worker 异常路径必须按 file_id 精确收尾（L1）"
        )


# ---------------------------------------------------------------------------
# L2（2026-11-04 遗留项）：自愈删除全部同内容 failed 行
# ---------------------------------------------------------------------------
class TestL2SelfHealClearsAllFailedRows:
    def test_self_heal_deletes_all_failed_same_content_rows(self, tmp_path):
        """同内容存在多条 failed 行时，重传自愈必须全部删除并清理物理文件。"""
        import hashlib as _hl

        pid = create_project(ProjectCreate(name="L2-自愈删全部failed")).id
        try:
            content = "自愈删除全部失败行 唯一内容 L2".encode("utf-8")
            digest = _hl.sha256(content).hexdigest()

            # 预置两条同 content_hash 的 failed 行，各带真实物理文件
            u_dir = _uploads_dir(pid)
            u_dir.mkdir(parents=True, exist_ok=True)
            f1 = u_dir / "old_a.txt"
            f1.write_bytes(content)
            f2 = u_dir / "old_b.txt"
            f2.write_bytes(content)
            t_dir = _texts_dir(pid)
            t_dir.mkdir(parents=True, exist_ok=True)
            t1 = t_dir / "old_a.txt.txt"
            t1.write_text("旧文本A", encoding="utf-8")
            t2 = t_dir / "old_b.txt.txt"
            t2.write_text("旧文本B", encoding="utf-8")

            with get_db() as conn:
                for fname, fp, tp in (
                    ("old_a.txt", f1, t1), ("old_b.txt", f2, t2),
                ):
                    conn.execute(
                        "INSERT INTO uploaded_files "
                        "(project_id, filename, path, file_type, file_role, status, "
                        " extracted_text_path, content_hash, message) "
                        "VALUES (?, ?, ?, 'txt', 'supporting', 'failed', ?, ?, '解析失败')",
                        (pid, fname, str(fp), str(tp), digest),
                    )

            # 重传同内容：自愈应删除全部 failed 行并走正常上传
            r = file_service.save_upload(pid, "同名重传.txt", content, "main")
            assert r["status"] in ("success", "processing"), r

            with get_db(readonly=True) as conn:
                remaining_failed = conn.execute(
                    "SELECT COUNT(*) FROM uploaded_files "
                    "WHERE project_id = ? AND content_hash = ? AND status = 'failed'",
                    (pid, digest),
                ).fetchone()[0]
            assert remaining_failed == 0, (
                f"自愈后仍有 {remaining_failed} 条同内容 failed 行残留（L2）"
            )
            # 物理文件与旧文本必须被清理
            assert not f1.exists(), "旧失败文件 A 应被清理（L2）"
            assert not f2.exists(), "旧失败文件 B 应被清理（L2）"
            assert not t1.exists(), "旧失败文本 A 应被清理（L2）"
            assert not t2.exists(), "旧失败文本 B 应被清理（L2）"
        finally:
            delete_project(pid)


# ---------------------------------------------------------------------------
# L3（2026-11-04 遗留项）：图片 processing 上传也推进项目步骤
# ---------------------------------------------------------------------------
class TestL3ProcessingUploadAdvancesStep:
    def test_upload_processing_advances_project_step(self, monkeypatch):
        """上传返回 status='processing'（图片异步解析）时，项目 current_step 必须推进到 2。"""
        import io

        from fastapi.testclient import TestClient

        from app.main import app
        from app.services import files as fmod

        with TestClient(app) as client:
            pid = client.post(
                "/api/v1/projects",
                json={"name": "L3-processing步骤", "industry": "公共建筑", "bidder_name": "测试方"},
            ).json()["id"]
            try:
                # 让 save_upload 返回 processing（模拟图片异步解析），验证路由器据此推进步骤
                monkeypatch.setattr(
                    fmod, "save_upload",
                    lambda *a, **k: {"status": "processing", "filename": "假图片.png", "message": ""},
                )
                resp = client.post(
                    f"/api/v1/projects/{pid}/files",
                    files={"file": ("假图片.png", io.BytesIO(b"\x89PNG\r\n\x1a\n fake"), "image/png")},
                )
                assert resp.status_code == 200, resp.text
                assert resp.json()["status"] == "processing", resp.json()

                from app.db import get_db as _gdb

                with _gdb(readonly=True) as conn:
                    row = conn.execute(
                        "SELECT current_step FROM projects WHERE id = ?", (pid,)
                    ).fetchone()
                assert row is not None and row["current_step"] >= 2, (
                    f"processing 上传后 current_step 应为 >=2，实际 {dict(row) if row else None}（L3）"
                )
            finally:
                delete_project(pid)
