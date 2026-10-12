"""上传解析与要点提取模块 —— 2026-10-12 专项回归

本文件钉住本次专项的每一项修改，全部为「可复现缺陷 → 修复后有断言」的对照式用例。

修复清单：
  1. BUG-3（高·事件循环阻塞）：``GET /projects/{id}/tasks/files/status``
     此前在 ``async def`` 内直调同步 sqlite 查询。该端点被前端在上传在途期间
     以 1.5s 间隔轮询，是全模块频率最高的只读接口。
  2. BUG-5（悬空路径 · 字段错位）：``save_upload`` / ``re_extract_text``
     在「提取失败/空文本」时仍把 ``extracted_text_path`` 指向一个**磁盘上并不存在**
     的 .txt（或一个 0 字节空文件），与 ``clear_project_texts`` 的空串口径自相矛盾。
  3. Gap-TD6（单一真相端点）：新增 ``GET /projects/{id}/files/supported-formats``，
     ``accept`` 串由 ``SUPPORTED_EXTENSIONS`` 现场生成，供前端驱动
     ``Upload.Dragger.accept``，消除手抄副本。
"""
import io
import re
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.main import app  # noqa: E402
from app.db import get_db  # noqa: E402
from app.services import files as files_mod  # noqa: E402


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def project_id(client):
    resp = client.post("/api/v1/projects", json={
        "name": "上传解析 2026-10-12 回归项目",
        "industry": "公共建筑",
        "bidder_name": "测试投标人",
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


def _upload(client, project_id, filename, content):
    resp = client.post(
        f"/api/v1/projects/{project_id}/files",
        files={"file": (filename, content,
                        "application/octet-stream")},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _file_row(file_id: int) -> dict:
    with get_db(readonly=True) as conn:
        row = conn.execute(
            "SELECT status, message, extracted_text_path FROM uploaded_files WHERE id = ?",
            (file_id,),
        ).fetchone()
    return dict(row) if row else {}


# ============================================================================
# BUG-3：tasks/files/status 不得在事件循环线程内做同步 sqlite I/O
# ============================================================================
class TestFileParsingStatusNotBlocking:
    def test_endpoint_dispatches_db_query_to_thread(self):
        """源码级：端点必须经 asyncio.to_thread 调用 _get_file_parsing_status"""
        import inspect

        from app.routers import tasks as tasks_mod

        src = inspect.getsource(tasks_mod.get_file_parsing_status)
        assert "asyncio.to_thread(_get_file_parsing_status, project_id)" in src, (
            "GET /tasks/files/status 仍在事件循环线程内直调同步 sqlite 查询 —— "
            "前端上传期间每 1.5s 轮询一次，会冻结整条事件循环"
        )
        # AST 级断言：函数体内除「作为 to_thread 实参」外，不得再出现裸调用。
        # （字符串/注释扫描会被 docstring 里的说明文字误伤，这里走语法树。）
        import ast

        tree = ast.parse(inspect.getsource(tasks_mod.get_file_parsing_status))

        def _is_bare_call(node) -> bool:
            if not isinstance(node, ast.Call):
                return False
            fn = node.func
            if not (isinstance(fn, ast.Attribute) or isinstance(fn, ast.Name)):
                return False
            name = fn.attr if isinstance(fn, ast.Attribute) else fn.id
            if name != "_get_file_parsing_status":
                return False
            # to_thread(_get_file_parsing_status, project_id) 的第一个实参是 ast.Name
            return not isinstance(fn, ast.Name)

        bare = [ast.dump(n) for n in ast.walk(tree) if _is_bare_call(n)]
        assert not bare, f"发现未包裹 to_thread 的同步调用: {bare}"

    def test_endpoint_still_returns_idle_when_no_tasks(self, client, project_id):
        """修复不得改变响应体形状：无任务时仍返回 idle 兜底"""
        resp = client.get(f"/api/v1/projects/{project_id}/tasks/files/status")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "idle", body
        assert "message" in body

    def test_endpoint_aggregates_after_upload(self, client, project_id):
        """修复后功能不回归：上传后仍能聚合出解析状态"""
        _upload(client, project_id, "回归.docx", _make_docx_bytes("正文内容"))
        resp = client.get(f"/api/v1/projects/{project_id}/tasks/files/status")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "completed", body
        assert body["recent_completed"] == 1, body
        assert body["recent_failed"] == 0, body

    def test_aggregate_endpoint_also_non_blocking(self):
        """同函数族的另一个调用方（/tasks/status）此前已修，本用例锁住不回退"""
        import inspect

        from app.routers import tasks as tasks_mod

        src = inspect.getsource(tasks_mod.get_all_tasks_status)
        assert "asyncio.to_thread(_get_file_parsing_status, project_id)" in src


# ============================================================================
# BUG-5：提取失败时不得留下悬空 extracted_text_path
# ============================================================================
class TestNoDanglingExtractedTextPath:
    def test_upload_failure_leaves_path_empty(self, client, project_id, monkeypatch):
        """上传解析失败 → extracted_text_path 必须为空串（不得指向不存在的文件）"""
        def _boom(file_path: str) -> str:
            raise RuntimeError("模拟解析器崩溃")

        monkeypatch.setattr(files_mod, "extract_text_from_path", _boom)
        body = _upload(client, project_id, "崩溃文件.docx", _make_docx_bytes("x"))

        assert body["status"] == "failed", body
        row = _file_row(body["id"])
        assert row["extracted_text_path"] == "", (
            f"解析失败仍写入 extracted_text_path={row['extracted_text_path']!r} —— "
            "DB 声称有正文但磁盘上并不存在该文件"
        )

    def test_upload_empty_text_leaves_path_empty(self, client, project_id, monkeypatch):
        """上传解析成功但文本为空 → 同样不得写悬空路径"""
        monkeypatch.setattr(files_mod, "extract_text_from_path", lambda p: "")
        body = _upload(client, project_id, "空正文.docx", _make_docx_bytes("x"))

        assert body["status"] == "success", body
        assert body["message"] == files_mod.EMPTY_TEXT_MESSAGE, body
        row = _file_row(body["id"])
        assert row["extracted_text_path"] == "", dict(row)

    def test_upload_success_still_records_path(self, client, project_id):
        """成功路径不受影响：文本落盘且路径入库"""
        body = _upload(client, project_id, "正常文件.docx",
                       _make_docx_bytes("这里是正文"))
        assert body["status"] == "success", body
        row = _file_row(body["id"])
        assert row["extracted_text_path"], dict(row)
        assert Path(row["extracted_text_path"]).exists(), dict(row)

    def test_reextract_failure_leaves_path_empty(
        self, client, project_id, monkeypatch
    ):
        """重提取失败 → 不落盘空 .txt，且 DB 字段置空"""
        body = _upload(client, project_id, "重试文件.docx",
                       _make_docx_bytes("第一版正文"))
        assert body["status"] == "success", body
        first_path = _file_row(body["id"])["extracted_text_path"]
        assert first_path

        def _boom(file_path: str) -> str:
            raise RuntimeError("模拟重提取崩溃")

        monkeypatch.setattr(files_mod, "extract_text_from_path", _boom)
        resp = client.post(
            f"/api/v1/projects/{project_id}/files/{body['id']}/re-extract"
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "failed", resp.json()

        row = _file_row(body["id"])
        assert row["status"] == "failed", dict(row)
        assert row["extracted_text_path"] == "", dict(row)

    def test_reextract_empty_result_does_not_create_zero_byte_file(
        self, client, project_id, monkeypatch
    ):
        """重提取产出空文本 → 磁盘上不得留下 0 字节 .txt

        BUG 现象：此前无条件 write_text，失败/空结果会在 texts/ 目录里
        凭空多出空文件，且 DB 指向它；下游 latest_extracted_text /
        _first_text_by_keywords 只判 exists() 不判大小，会把空串当正文。
        """
        body = _upload(client, project_id, "重试空文件.docx",
                       _make_docx_bytes("第一版正文"))
        texts_dir = files_mod.PROJECTS_DATA_DIR / str(project_id) / "texts"

        monkeypatch.setattr(files_mod, "extract_text_from_path", lambda p: "")
        resp = client.post(
            f"/api/v1/projects/{project_id}/files/{body['id']}/re-extract"
        )
        assert resp.status_code == 200, resp.text
        res = resp.json()
        assert res["status"] == "success", res
        assert res["message"] == files_mod.EMPTY_TEXT_MESSAGE, res
        assert res["text_length"] == 0, res

        row = _file_row(body["id"])
        assert row["extracted_text_path"] == "", dict(row)
        zero_byte = [p for p in texts_dir.glob("*.txt") if p.stat().st_size == 0]
        assert not zero_byte, f"texts/ 留下 0 字节文件: {zero_byte}"

    def test_latest_text_ignores_files_without_text_path(
        self, client, project_id, monkeypatch
    ):
        """端到端：失败文件不会被 latest_extracted_text 当作主正文"""
        import asyncio

        _upload(client, project_id, "唯一主文件.docx", _make_docx_bytes("真实正文"))

        def _boom(file_path: str) -> str:
            raise RuntimeError("boom")

        monkeypatch.setattr(files_mod, "extract_text_from_path", _boom)
        _upload(client, project_id, "坏文件.docx", _make_docx_bytes("x"))

        files_mod._text_cache_clear(project_id)
        text = asyncio.run(files_mod.latest_extracted_text(project_id))
        assert "真实正文" in text, text[:200]


# ============================================================================
# Gap-TD6：supported-formats 单一真相端点
# ============================================================================
class TestSupportedFormatsEndpoint:
    def test_returns_accept_derived_from_service_truth(self, client, project_id):
        resp = client.get(f"/api/v1/projects/{project_id}/files/supported-formats")
        assert resp.status_code == 200, resp.text
        body = resp.json()

        # accept 必须与 SUPPORTED_EXTENSIONS 逐项一致（唯一真相）
        assert body["accept"] == ",".join(sorted(files_mod.SUPPORTED_EXTENSIONS))
        assert set(body["extensions"]) == set(files_mod.SUPPORTED_EXTENSIONS)
        assert body["image_extensions"] == sorted(files_mod.IMAGE_EXTENSIONS)

    def test_reports_size_limit_and_ocr_flag(self, client, project_id):
        from app.routers import files as files_router

        body = client.get(
            f"/api/v1/projects/{project_id}/files/supported-formats"
        ).json()
        assert body["max_file_size"] == files_router.MAX_FILE_SIZE
        assert body["max_file_size_mb"] == files_router.MAX_FILE_SIZE // (1024 * 1024)
        assert body["ocr_enabled"] is bool(files_mod.OCR_ENABLED)

    def test_requires_project_access(self, client, project_id):
        """属权校验与同模块其余端点同口径（不存在 → 404）"""
        resp = client.get("/api/v1/projects/99999999/files/supported-formats")
        assert resp.status_code == 404, resp.text

    def test_frontend_fallback_list_matches_backend(self):
        """跨层契约：前端降级常量必须与后端白名单完全一致

        前端 frontend/src/utils.ts::FALLBACK_ACCEPT 是 supported-formats
        不可达时的降级值。两边不一致 = 后端加了格式而前端降级路径拦不住
        （或反之多放行），这正是 2026-09-18「入口封死」故障的前端版本。
        前端侧有 utils.supportedFormats.test.ts 做同样断言。
        """
        fe = Path(BACKEND_DIR).parent / "frontend" / "src" / "utils.ts"
        if not fe.exists():
            pytest.skip("frontend/src/utils.ts 不存在（单仓部署场景）")
        text = fe.read_text(encoding="utf-8")
        start = text.index("export const FALLBACK_ACCEPT")
        end = text.index("];", start)
        block = text[start:end]
        fe_exts = set(re.findall(r'"(\.[a-z0-9]+)"', block))
        assert fe_exts == set(files_mod.SUPPORTED_EXTENSIONS), (
            "前端 FALLBACK_ACCEPT 与后端 SUPPORTED_EXTENSIONS 不一致："
            f"仅前端有 {fe_exts - set(files_mod.SUPPORTED_EXTENSIONS)}，"
            f"仅后端有 {set(files_mod.SUPPORTED_EXTENSIONS) - fe_exts}"
        )

    def test_route_registered_before_file_id_routes(self):
        """路由顺序：/supported-formats 不得被 /{file_id}/... 抢占"""
        from app.routers import files as files_router

        paths = [r.path for r in files_router.router.routes]
        fmt = "/projects/{project_id}/files/supported-formats"
        assert fmt in paths, f"端点未注册，router 现有路径: {paths}"
        # 关键不变式：/supported-formats 之前不得存在「单段路径参数」的 GET 路由
        # （如 GET /{file_id}），否则会把字面量 supported-formats 当成 file_id。
        before = paths[: paths.index(fmt)]
        offenders = [
            p for p in before
            if p.startswith("/projects/{project_id}/files/{")
            and p.count("/") == len("/projects/{project_id}/files/{x}")
        ]
        assert not offenders, (
            f"/supported-formats 之前存在单段路径参数路由 {offenders}，"
            "字面量 supported-formats 会被当作 file_id 匹配"
        )


# ============================================================================
# TD-12 / TD-13：只读查询改用 readonly 连接 + 合并文本查询移出事件循环
# ============================================================================
class TestReadOnlyQueriesSkipWriteLock:
    def test_read_functions_use_readonly_connection(self):
        """TD-12：get_project_files / get_file_text / _first_text_by_keywords
        仅做 SELECT，必须以 readonly=True 跳过全局写锁，避免与 save_upload
        在批量上传高峰期互相排队。"""
        import inspect

        for fn_name in (
            "get_project_files",
            "get_file_text",
            "_first_text_by_keywords",
        ):
            src = inspect.getsource(getattr(files_mod, fn_name))
            assert "get_db(readonly=True)" in src, (
                f"{fn_name} 仍是写锁连接 get_db()，只读查询应改用 "
                "get_db(readonly=True) 跳过全局写锁"
            )

    def test_latest_text_db_query_runs_off_event_loop(self):
        """TD-13：latest_extracted_text 的同步 DB 查询必须整体移入线程池，
        与同模块 list_files / get_file_text 的 to_thread 口径一致。"""
        import ast
        import inspect

        src = inspect.getsource(files_mod.latest_extracted_text)
        assert "asyncio.to_thread(_latest_text_query_rows, project_id)" in src, (
            "latest_extracted_text 仍在事件循环线程内直连 DB —— "
            "与 list_files/get_file_text 的 to_thread 口径不一致"
        )
        helper_src = inspect.getsource(files_mod._latest_text_query_rows)
        assert "get_db(readonly=True)" in helper_src

    def test_read_functions_behave_unchanged(self, client, project_id):
        """修复不得改变行为：上传后各只读函数仍能正确返回数据"""
        import asyncio

        _upload(client, project_id, "只读校验.docx", _make_docx_bytes("采购人：测试单位"))

        files = files_mod.get_project_files(project_id)
        assert len(files) == 1, files
        assert files[0]["filename"] == "只读校验.docx"

        fid = files[0]["id"]
        text = files_mod.get_file_text(project_id, fid)
        assert "采购人：测试单位" in text, text

        # 合并文本（主文件优先）应包含该正文
        files_mod._text_cache_clear(project_id)
        merged = asyncio.run(files_mod.latest_extracted_text(project_id))
        assert "采购人：测试单位" in merged, merged[:200]

        # 无关键词文件 → 清单检索返回空，行为不变
        assert files_mod.get_boq_file_text(project_id) == ""

    def test_latest_text_consistent_with_direct_file_text(
        self, client, project_id
    ):
        """TD-13 回归：移出事件循环后，合并文本与单文件文本口径一致"""
        import asyncio

        _upload(client, project_id, "一致性.docx", _make_docx_bytes("工期：120 天"))
        files = files_mod.get_project_files(project_id)
        direct = files_mod.get_file_text(project_id, files[0]["id"])
        files_mod._text_cache_clear(project_id)
        merged = asyncio.run(files_mod.latest_extracted_text(project_id))
        assert "工期：120 天" in direct
        assert "工期：120 天" in merged


# ============================================================================
# BUG-5（异步链路悬空路径） + R-3（图片上传后台异步解析）
# ============================================================================
class TestAsyncParseDanglingPathAndImageAsync:
    def test_persist_extraction_never_writes_dangling_path(
        self, client, project_id, monkeypatch
    ):
        """BUG-5 异步链路：空文本 / OCR 未启用时，_persist_file_extraction 不得写
        悬空 extracted_text_path（磁盘上并不存在的 .txt）。与 save_upload /
        re_extract_text 同口径。"""
        monkeypatch.setattr(files_mod, "extract_text_from_path", lambda p: "")
        with get_db() as conn:
            cur = conn.execute(
                "INSERT INTO uploaded_files "
                "(project_id, filename, path, file_type, file_role, status) "
                "VALUES (?, ?, ?, 'png', 'main', 'processing')",
                (project_id, "扫描件.png", "x"),
            )
            fid = cur.lastrowid
        files_mod._create_parse_task(project_id, "扫描件.png", ".png")
        status = files_mod._persist_file_extraction(
            project_id, fid, "x", "扫描件.png", ".png", create_task_row=False
        )
        row = _file_row(fid)
        assert row["extracted_text_path"] == "", (
            f"空文本却写了悬空 extracted_text_path={row['extracted_text_path']!r}"
        )
        assert status == "success", status
        assert row["message"] == files_mod.EMPTY_TEXT_MESSAGE, row

    def test_image_upload_falls_back_to_sync_when_dispatch_fails(
        self, client, project_id, monkeypatch
    ):
        """R-3 兜底：broker 不可达 / 派发异常时，图片上传必须同步收尾并返回终态
        （success/failed），不得卡在 processing，也不得 500。"""
        import app.celery_app as celery_mod

        def _raise(*a, **k):
            raise RuntimeError("broker unreachable (simulated)")

        monkeypatch.setattr(celery_mod, "dispatch_task", _raise)
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
        resp = client.post(
            f"/api/v1/projects/{project_id}/files",
            files={"file": ("扫描件.png", png, "application/octet-stream")},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] in ("success", "failed"), body
        assert body["status"] != "processing", (
            "派发失败时图片上传不应停留在 processing"
        )

    def test_image_upload_dispatches_async_when_celery(
        self, client, project_id, monkeypatch
    ):
        """R-3：broker 可达（dispatched_via='celery'）时，图片上传返回 processing，
        且解析任务行可见（供前端轮询）。"""
        import app.celery_app as celery_mod

        monkeypatch.setattr(
            celery_mod,
            "dispatch_task",
            lambda *a, **k: {
                "status": "queued",
                "dispatched_via": "celery",
                "task_name": "tasks.file.parse_and_extract",
            },
        )
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
        resp = client.post(
            f"/api/v1/projects/{project_id}/files",
            files={"file": ("扫描件.png", png, "application/octet-stream")},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "processing", body
        st = client.get(
            f"/api/v1/projects/{project_id}/tasks/files/status"
        ).json()
        assert st["status"] in ("processing", "completed"), st

