"""上传解析与要点提取模块 —— 2026-10-15 专项回归（M-1 … M-5）

五项缺陷均为「后端已有诊断信息 / 已发生失败，但消费端看不见」这一类
静默失效（silent failure），修复后必须有断言钉住，否则下次重构极易回退。

  M-1（解析警告静默丢弃 · 前后端同修）
    `file_parse_tasks` 的 completed 分支 SQL 从不写 error_message，而
    save_upload / re_extract_text / _persist_file_extraction 三条写入路径
    都把 EMPTY_TEXT_MESSAGE（「提取成功但正文为空」）按第四参数传了进来 ——
    警告只落到文件行，任务行永远是「completed 且无信息」；
    GET /projects/{id}/tasks/files/status 的 latest.error_message 因此恒空，
    前端 formatParsingStatusText 也就无从播报。

  M-2（招标上下文吞异常 · 四处口径不一）
    2026-09-18 只给 sse_utils 的那一份 _extract_project_context 副本补了告警，
    sse_db_ops / sse_content / tasks 里的另外三处一直是
    `except Exception: tender_context = ""`。取文失败时目录/正文照常生成，
    事后既无日志也无从判断「生成结果为何与招标文件脱节」。

  M-3（scoring_points_json 坏 JSON 静默塌成空列表）
    行为不变（不阻塞生成），补可排查的告警。

  M-4（图片同步回退解析失败 → 假 processing 终态丢失）
    _save_upload_image_async 的 except 分支只 logger.error，文件行仍以
    status='processing' 落库且无人改写终态；POST /files 于是返回 200 +
    processing，前端进度条永久卡住。

  M-5（清空提取的破坏性假成功）
    clear_project_texts 里 texts/ 已真删（不可回滚），DB 重置却失败时
    旧实现照常返回 {'status': 'cleared', 'files_reset': 0}，前端弹
    「已重置 0 个文件状态为待提取」—— 用户以为清空完成，实际列表仍显示
    「已提取」且取文为空（悬空路径）。现透出 warning 字段，由前端分级告警。

约定：与 tests/conftest.py 一致，``asyncio_mode = "auto"``，async 用例直接 def→async def。
"""
import logging
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

from app.services.files import EMPTY_TEXT_MESSAGE  # noqa: E402


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def project_id(client):
    resp = client.post("/api/v1/projects", json={
        "name": "上传解析 2026-10-15 回归项目",
        "industry": "公共建筑",
        "bidder_name": "测试投标人",
    })
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _upload(client, project_id, filename, content):
    resp = client.post(
        f"/api/v1/projects/{project_id}/files",
        files={"file": (filename, content, "application/octet-stream")},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _task_row(project_id: int, filename: str) -> dict:
    """该项目指定文件名的最新一条解析任务行"""
    with get_db(readonly=True) as conn:
        row = conn.execute(
            "SELECT status, progress, completed_files, error_message, filename "
            "FROM file_parse_tasks WHERE project_id = ? AND filename = ? "
            "ORDER BY id DESC LIMIT 1",
            (project_id, filename),
        ).fetchone()
    return dict(row) if row else {}


def _file_row(file_id: int) -> dict:
    with get_db(readonly=True) as conn:
        row = conn.execute(
            "SELECT status, message, extracted_text_path FROM uploaded_files WHERE id = ?",
            (file_id,),
        ).fetchone()
    return dict(row) if row else {}


# ============================================================================
# M-1：「解析成功但正文为空」的警告必须写进任务行并被状态端点透出
# ============================================================================

def test_empty_text_warning_is_persisted_to_parse_task_row(client, project_id):
    """空白 .txt：文件行 success + 警告，任务行也必须带上同一条警告

    修复前 completed 分支的 SQL 没有 error_message 列，任务行永远是空串。
    """
    result = _upload(client, project_id, "空正文.txt", b"   \n\t  \n")
    assert result["status"] == "success", result
    assert result["message"] == EMPTY_TEXT_MESSAGE

    task = _task_row(project_id, result["filename"])
    assert task, "任务行缺失"
    assert task["status"] == "completed"
    assert task["progress"] == 100
    assert task["completed_files"] == 1
    # 核心断言：警告不再在 completed 分支被丢弃
    assert task["error_message"] == EMPTY_TEXT_MESSAGE


def test_real_text_upload_leaves_task_row_message_empty(client, project_id):
    """正常非空上传不得伪造警告 —— 「行内无消息 == 一切正常」的读法要保持"""
    body = "第一章 编制说明\n本工程为公共建筑装修改造，工期 90 日历天。\n"
    result = _upload(client, project_id, "真实正文.txt", body.encode("utf-8"))
    assert result["status"] == "success"
    assert result["message"] == ""

    task = _task_row(project_id, result["filename"])
    assert task["status"] == "completed"
    assert task["error_message"] == ""

    file_row = _file_row(result["id"])
    assert file_row["extracted_text_path"]  # 非空文本确实落盘并记录了路径


def test_tasks_files_status_endpoint_exposes_empty_text_warning(client, project_id):
    """GET /tasks/files/status 的 latest.error_message 是前端唯一能看到警告的通道"""
    # 注：0 字节上传在 router 层就被 400 拦下，走的是「纯空白正文」这一入口
    result = _upload(client, project_id, "扫描件为空.txt", b" \n\xe3\x80\x80 \n")
    assert result["status"] == "success"

    resp = client.get(f"/api/v1/projects/{project_id}/tasks/files/status")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body is not None, "无任何任务行"
    latest = body["latest"]
    assert latest["filename"] == result["filename"]
    assert latest["status"] == "completed"
    assert latest["error_message"] == EMPTY_TEXT_MESSAGE


def test_re_extract_keeps_same_warning_channel(client, project_id, monkeypatch):
    """重新提取与上传同口径：空文本 → 任务行 completed + 警告

    patch extract_text_from_path 返回空串，避免依赖真实空文件的可移植性。
    """
    result = _upload(
        client, project_id, "重提取目标.txt", "有内容的一段正文。\n".encode("utf-8")
    )
    assert _task_row(project_id, result["filename"])["error_message"] == ""

    monkeypatch.setattr(files_mod, "extract_text_from_path", lambda p: "   ")
    resp = client.post(
        f"/api/v1/projects/{project_id}/files/{result['id']}/re-extract"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "success"
    assert body["message"] == EMPTY_TEXT_MESSAGE

    # re_extract 会新建一条任务行（filename 同名），取最新一条
    task = _task_row(project_id, result["filename"])
    assert task["status"] == "completed"
    assert task["error_message"] == EMPTY_TEXT_MESSAGE


# ============================================================================
# M-2：招标正文取文失败不再静默 —— 四处副本统一「不阻塞 + 必留告警」
# ============================================================================

def _analysis_exists(project_id: int) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO analysis (project_id, summary_json, scoring_points_json) "
            "VALUES (?, '', '[]')",
            (project_id,),
        )


async def test_sse_db_ops_logs_tender_context_failure(client, project_id, monkeypatch, caplog):
    from app.routers.sse import sse_db_ops

    _analysis_exists(project_id)

    async def _boom(_pid: int) -> str:
        raise RuntimeError("disk read failed")

    monkeypatch.setattr(sse_db_ops, "latest_extracted_text", _boom)

    with caplog.at_level(logging.WARNING):
        ctx = await sse_db_ops._extract_project_context(project_id)

    # 行为不变：不抛异常，降级为空上下文
    assert ctx["tender_context"] == ""
    messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("招标正文/上下文提取失败" in m for m in messages), messages


def test_tender_context_swallow_sites_all_log():
    """四处副本统一口径：捕获后必须留痕（M-2 的缺陷形态是「吞掉且不打日志」）

    sse_utils / sse_db_ops / sse_content / tasks 各有一份取招标正文的
    try/except。除行为用例（上面那条覆盖 sse_db_ops）外，这里静态核对
    每一处都在 `except Exception as _tc_e` 后调用了 logger.warning。
    """
    sites = [
        BACKEND_DIR / "app" / "routers" / "sse" / "sse_utils.py",
        BACKEND_DIR / "app" / "routers" / "sse" / "sse_db_ops.py",
        BACKEND_DIR / "app" / "routers" / "sse" / "sse_content.py",
        BACKEND_DIR / "app" / "tasks" / "__init__.py",
    ]
    pattern = re.compile(
        r"except Exception as _tc_e:\s*\n(?:\s*#[^\n]*\n)*\s*logger\.warning",
    )
    missing = [
        str(p.relative_to(BACKEND_DIR))
        for p in sites
        if not pattern.search(p.read_text(encoding="utf-8"))
    ]
    assert missing == [], f"以下位置的招标上下文异常仍未留痕: {missing}"


def test_no_bare_tender_context_swallow_remains():
    """静态守卫：全仓库不得再出现 `except Exception:` 后紧跟 `tender_context = ""`

    这类裸吞是 M-2 的根因形态（失败被翻译成空字符串且不留痕），
    用正则钉住，避免将来新增副本时又退回旧写法。
    """
    pattern = re.compile(
        r"except Exception:\s*\n\s*(?:#\.[^\n]*\n\s*)*tender_context\s*=\s*[\"']",
        re.MULTILINE,
    )
    roots = [
        BACKEND_DIR / "app" / "routers" / "sse",
        BACKEND_DIR / "app" / "tasks",
        BACKEND_DIR / "app" / "services",
    ]
    offenders = []
    for root in roots:
        for py in root.rglob("*.py"):
            if "__pycache__" in str(py):
                continue
            if pattern.search(py.read_text(encoding="utf-8")):
                offenders.append(str(py.relative_to(BACKEND_DIR)))
    assert offenders == [], f"仍存在裸吞 tender_context 的位置: {offenders}"


# ============================================================================
# M-3：坏 scoring_points_json 降级为空列表时必须留痕
# ============================================================================

async def test_unparsable_scoring_points_logs_warning(client, project_id, caplog):
    from app.routers.sse import sse_db_ops

    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO analysis "
            "(project_id, summary_json, scoring_points_json) VALUES (?, '', ?)",
            (project_id, "{这不是合法 JSON"),
        )

    with caplog.at_level(logging.WARNING):
        ctx = await sse_db_ops._extract_project_context(project_id)

    assert ctx["scoring_points_list"] == []
    messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("scoring_points_json 不可解析" in m for m in messages), messages


# ============================================================================
# M-4：图片同步回退解析失败不得停留在假 processing
# ============================================================================

def test_image_fallback_failure_writes_terminal_failed_state(project_id, monkeypatch):
    """broker 不可达 → 同步回退；回退解析抛错 → 文件行与任务行都要落 failed

    修复前 except 只打日志，函数继续读回 status='processing'，
    上传接口返回 200 + processing，前端进度条永久卡住。
    """
    up = files_mod.PROJECTS_DATA_DIR / str(project_id) / "uploads"
    up.mkdir(parents=True, exist_ok=True)
    img = up / "扫描件.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    monkeypatch.setattr(
        files_mod, "_dispatch_async_parse",
        lambda *a, **k: {"status": "not_dispatched", "dispatched_via": "none"},
    )

    def _boom(*a, **k):
        raise RuntimeError("OCR 引擎崩溃")

    monkeypatch.setattr(files_mod, "_persist_file_extraction", _boom)

    result = files_mod._save_upload_image_async(project_id, "扫描件.png", str(img), "main")

    assert result["status"] == "failed", result
    assert "OCR 引擎崩溃" in result["message"]

    file_row = _file_row(result["id"])
    assert file_row["status"] == "failed"
    assert "OCR 引擎崩溃" in file_row["message"]


def test_image_celery_dispatch_still_reports_processing(project_id, monkeypatch):
    """M-4 的对照用例：真正入队成功时仍返回 processing（异步在途，不是假成功）"""
    up = files_mod.PROJECTS_DATA_DIR / str(project_id) / "uploads"
    up.mkdir(parents=True, exist_ok=True)
    img = up / "在途.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nfake")

    calls = []

    def _fake_dispatch(pid, file_path, filename, file_id, ext):
        calls.append(file_id)
        # 模拟 worker 已接管：把行置为 processing（本用例不执行解析）
        with get_db() as conn:
            conn.execute(
                "UPDATE uploaded_files SET status = 'processing' WHERE id = ?", (file_id,)
            )
        return {"status": "dispatched", "dispatched_via": "celery", "celery_task_id": "t-1"}

    monkeypatch.setattr(files_mod, "_dispatch_async_parse", _fake_dispatch)

    result = files_mod._save_upload_image_async(project_id, "在途.png", str(img), "main")
    assert calls == [result["id"]]
    assert result["status"] == "processing"
    assert result["filename"] == "在途.png"


# ============================================================================
# M-5：清空提取的「文本已删 / 库未重置」不一致必须透出
# ============================================================================

def test_clear_texts_reports_counts_on_success(client, project_id):
    _upload(client, project_id, "清空目标.txt", "一段正文。\n".encode("utf-8"))

    resp = client.delete(f"/api/v1/projects/{project_id}/files/texts")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "cleared"
    assert body["files_reset"] >= 1
    assert body["tasks_cleared"] >= 1
    # 成功路径不带警告 —— 前端据此弹 success
    assert body["warning"] == ""


def test_clear_texts_surfaces_db_reset_failure(client, project_id, monkeypatch):
    """texts/ 已删但 DB 重置失败：必须返回非空 warning，不能再报假成功"""
    _upload(client, project_id, "悬空.txt", "一段正文。\n".encode("utf-8"))

    real_get_db = files_mod.get_db

    class _FailWrite:
        def __enter__(self):
            raise RuntimeError("database is locked")

        def __exit__(self, *a):
            return False

    def _fake_get_db(*args, **kwargs):
        # readonly 连接仍走真实实现（前置的缓存失效查询要能跑通）
        if kwargs.get("readonly"):
            return real_get_db(*args, **kwargs)
        return _FailWrite()

    monkeypatch.setattr(files_mod, "get_db", _fake_get_db)

    out = files_mod.clear_project_texts(project_id)

    assert out["status"] == "cleared"          # 仍不抛异常（破坏性动作已发生）
    assert out["files_reset"] == 0
    assert out["warning"], "DB 重置失败必须透出 warning，不能报假成功"
    assert "数据库状态重置失败" in out["warning"]
    assert "重新提取" in out["warning"]         # 给出可行动的下一步
