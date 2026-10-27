"""上传解析与要点提取模块 —— 2026-10-26 第九轮专项回归

本轮聚焦九轮审计（见同名前端用例）中剩余的高危缺口，全部为**可验证行为变更**：

  G-1（clear_project_texts 与在途解析竞态 · 幽灵任务）
     原实现 ``UPDATE uploaded_files SET status='pending' WHERE project_id=?``
     无 status 过滤，把**正在后台解析**（status='processing'）的行也重置为
     'pending'。'pending' 语义是「已入库、从未解析」，而该文件此刻正有 Celery
     任务在跑；前端又把 pending 当「在途」注册进 taskStore，于是「清空提取」
     会让顶栏凭空多出 N 条「文件解析 0%」且永不进终态。

  G-2（supported-formats 上限整数截断）
     ``max_file_size_mb = MAX_FILE_SIZE // (1024*1024)`` 是整数除法，运维把上限
     调成 51MB / 25.5MB 这类非整 MB 值时前端拿到向下截断值，「前端放行、后端
     400」的区间重新出现。补 ``max_file_size_exact_mb``（精确浮点）与
     ``ocr_max_file_size_mb``（图片 OCR 独立上限），老字段保留不动。

  G-3（上传 4xx 无机器可读错误码 → 错误提示双弹）
     上传被 400 拒绝时 axios 响应拦截器先弹一次「请求失败: {detail}」，组件
     catch 再弹一次「XX 上传失败: ...」。约定：本模块业务校验类 4xx 都带
     ``X-Error-Code`` 响应头，前端拦截器见头即跳过通用弹窗，把唯一提示权交给
     调用方。``detail`` 仍是字符串，老前端与既有断言完全兼容。

  G-4（MAX_FILE_SIZE 硬编码）
     单文件上限此前写死在路由层 ``50 * 1024 * 1024``，改上限要改代码。
     现从 ``config.UPLOAD_MAX_FILE_SIZE_MB`` 派生（默认 50MB，向后兼容）。

约定：与 tests/conftest.py 一致，``asyncio_mode = "auto"``。
"""
import re
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from fastapi.testclient import TestClient  # noqa: E402

from app.db import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.routers import files as files_router  # noqa: E402


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def project_id(client):
    resp = client.post("/api/v1/projects", json={
        "name": "上传解析 2026-10-26 回归项目",
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


def _set_status(project_id: int, file_id: int, status: str) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE uploaded_files SET status = ?, updated_at = datetime('now', 'localtime') "
            "WHERE id = ? AND project_id = ?",
            (status, file_id, project_id),
        )


def _rows(project_id: int) -> dict[int, dict]:
    with get_db(readonly=True) as conn:
        rows = conn.execute(
            "SELECT id, filename, status, message, extracted_text_path "
            "FROM uploaded_files WHERE project_id = ? ORDER BY id",
            (project_id,),
        ).fetchall()
    return {r["id"]: dict(r) for r in rows}


# ============================================================================
# G-1：clear_project_texts 不得把在途 processing 行重置为 pending
# ============================================================================


def test_clear_project_texts_keeps_processing_rows(client, project_id):
    """G-1：清空提取只重置非在途行，在途 processing 行保持原状态。

    旧实现把 processing 行也改成 pending —— 该文件此刻正有后台任务在跑，
    改状态等于让 DB 与真实世界不一致；前端再把 pending 当在途注册 taskStore，
    于是「清空提取」凭空制造一批永不进终态的幽灵任务。
    """
    ok = _upload(client, project_id, "招标文件.docx", "招标正文".encode("utf-8"))
    img = _upload(
        client, project_id, "扫描件.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    )
    # 模拟「上传已入库、后台 OCR 仍在跑」的窗口
    _set_status(project_id, img["id"], "processing")
    _set_status(project_id, ok["id"], "success")

    resp = client.delete(f"/api/v1/projects/{project_id}/files/texts")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["status"] == "cleared", body
    # 在途行不被重置（核心断言）
    assert body["files_kept_processing"] == 1, body
    assert body["files_reset"] == 1, body
    rows = _rows(project_id)
    assert rows[img["id"]]["status"] == "processing", rows[img["id"]]
    assert rows[ok["id"]]["status"] == "pending", rows[ok["id"]]
    # 已重置行的悬空路径与 message 一并清空（既有口径不变）
    assert rows[ok["id"]]["extracted_text_path"] == "", rows[ok["id"]]
    assert rows[ok["id"]]["message"] == "", rows[ok["id"]]


def test_clear_project_texts_no_processing_kept_reports_zero(client, project_id):
    """反向对照：无在途行时 files_kept_processing 为 0，files_reset 计数不变"""
    body = _upload(client, project_id, "甲.txt", "甲内容".encode("utf-8"))
    body2 = _upload(client, project_id, "乙.txt", "乙内容".encode("utf-8"))
    assert body["id"] != body2["id"]

    resp = client.delete(f"/api/v1/projects/{project_id}/files/texts")
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["files_kept_processing"] == 0, out
    assert out["files_reset"] == 2, out


def test_clear_project_texts_repeated_is_idempotent(client, project_id):
    """重复清空：在途行始终被保留，第二次调用 files_reset 为 0"""
    img = _upload(client, project_id, "扫描件2.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
    _set_status(project_id, img["id"], "processing")

    first = client.delete(f"/api/v1/projects/{project_id}/files/texts").json()
    second = client.delete(f"/api/v1/projects/{project_id}/files/texts").json()

    assert first["files_kept_processing"] == 1, first
    assert second["files_kept_processing"] == 1, second
    assert second["files_reset"] == 0, second
    rows = _rows(project_id)
    assert rows[img["id"]]["status"] == "processing", rows[img["id"]]


# ============================================================================
# G-2：supported-formats 下发精确上限与 OCR 独立上限
# ============================================================================


def test_supported_formats_exposes_exact_and_ocr_limits(client, project_id):
    """G-2：端点下发 max_file_size_exact_mb 与 ocr_max_file_size_mb（新增可选字段）"""
    resp = client.get(f"/api/v1/projects/{project_id}/files/supported-formats")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # 老字段保持不动（既有断言锁定）
    assert body["max_file_size"] == files_router.MAX_FILE_SIZE, body
    assert body["max_file_size_mb"] == files_router.MAX_FILE_SIZE // (1024 * 1024), body

    assert "max_file_size_exact_mb" in body, body
    # 整 MB 配置下精确值与整数值相等（行为与旧版一致）
    assert body["max_file_size_exact_mb"] == pytest.approx(
        body["max_file_size"] / (1024 * 1024)
    ), body
    assert body["max_file_size_exact_mb"] == body["max_file_size_mb"], body

    assert "ocr_max_file_size_mb" in body, body
    assert body["ocr_max_file_size_mb"] == pytest.approx(50), body


def test_supported_formats_exact_mb_survives_truncation(client, project_id, monkeypatch):
    """G-2 核心：非整 MB 上限时 exact 字段不被截断，老字段仍向下取整

    运维把上限调成 25.5MB 时，前端若只读 max_file_size_mb（=25）就会放行
    25~25.5MB 的文件被后端 400；exact 字段给出真实值供前端优先使用。
    """
    monkeypatch.setattr(files_router, "MAX_FILE_SIZE", int(25.5 * 1024 * 1024))
    resp = client.get(f"/api/v1/projects/{project_id}/files/supported-formats")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["max_file_size"] == int(25.5 * 1024 * 1024), body
    assert body["max_file_size_mb"] == 25, body          # 老字段：截断
    assert body["max_file_size_exact_mb"] == pytest.approx(25.5), body  # 新字段：精确


def test_max_file_size_derives_from_config():
    """G-4 静态守卫：路由上限由 config.UPLOAD_MAX_FILE_SIZE_MB 派生，不再硬编码

    任何人把 ``MAX_FILE_SIZE = 50 * 1024 * 1024`` 改回硬编码字面量，本用例即变红。
    只锚定赋值行本身，避免被注释里的字面量误伤。
    """
    src = Path(files_router.__file__).read_text(encoding="utf-8")
    m = re.search(r"^MAX_FILE_SIZE\s*=\s*(.+)$", src, re.M)
    assert m, "未找到 MAX_FILE_SIZE 定义"
    assert m.group(1).strip() == "UPLOAD_MAX_FILE_SIZE_MB * 1024 * 1024", (
        f"MAX_FILE_SIZE 被写死为 {m.group(1).strip()}："
        "改上限又要改代码（2026-10-26 G-4 回退）"
    )
    assert "from ..config import" in src and "UPLOAD_MAX_FILE_SIZE_MB" in src.split("from ..config import", 1)[1].split("\n", 1)[0], (
        "UPLOAD_MAX_FILE_SIZE_MB 未从 app.config 引入"
    )


# ============================================================================
# G-3：上传业务校验类 4xx 带 X-Error-Code（前端据此抑制重复弹窗）
# ============================================================================


def test_upload_too_large_returns_error_code(client, project_id, monkeypatch):
    """G-3：超限上传返回 400 + X-Error-Code: file_too_large"""
    monkeypatch.setattr(files_router, "MAX_FILE_SIZE", 1024)
    resp = client.post(
        f"/api/v1/projects/{project_id}/files",
        files={"file": ("大文件.txt", b"a" * 2048, "text/plain")},
    )
    assert resp.status_code == 400, resp.text
    assert resp.headers.get("x-error-code") == "file_too_large", dict(resp.headers)
    assert "MB 限制" in resp.json()["detail"], resp.text


def test_upload_unsupported_extension_returns_error_code(client, project_id):
    """G-3：不支持的扩展名返回 400 + X-Error-Code: file_type_unsupported"""
    resp = client.post(
        f"/api/v1/projects/{project_id}/files",
        files={"file": ("恶意.exe", b"MZ", "application/octet-stream")},
    )
    assert resp.status_code == 400, resp.text
    assert resp.headers.get("x-error-code") == "file_type_unsupported", dict(resp.headers)


def test_upload_empty_content_returns_error_code(client, project_id):
    """G-3：空文件返回 400 + X-Error-Code: file_empty"""
    resp = client.post(
        f"/api/v1/projects/{project_id}/files",
        files={"file": ("空.txt", b"", "text/plain")},
    )
    assert resp.status_code == 400, resp.text
    assert resp.headers.get("x-error-code") == "file_empty", dict(resp.headers)


def test_upload_error_code_does_not_break_detail_contract(client, project_id):
    """G-3 兼容性：detail 仍是字符串（老前端与既有断言不受影响）"""
    resp = client.post(
        f"/api/v1/projects/{project_id}/files",
        files={"file": ("x.exe", b"MZ", "application/octet-stream")},
    )
    assert isinstance(resp.json()["detail"], str), resp.text
    assert resp.headers.get("x-error-code"), "业务校验类 4xx 必须带错误码"


def test_upload_success_has_no_error_code(client, project_id):
    """反向对照：成功上传不带 X-Error-Code（避免前端误判）"""
    resp = client.post(
        f"/api/v1/projects/{project_id}/files",
        files={"file": ("正常.txt", "正文".encode("utf-8"), "text/plain")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.headers.get("x-error-code") is None, dict(resp.headers)


# ============================================================================
# G-1 补测：解析任务行不受在途保护影响（终态行照常清理）
# ============================================================================


def test_clear_project_texts_still_cleans_terminal_task_rows(client, project_id):
    """G-1 边界：只豁免 processing 行的状态重置，不豁免终态任务行清理"""
    body = _upload(client, project_id, "待清空.txt", "内容".encode("utf-8"))
    with get_db(readonly=True) as conn:
        before = conn.execute(
            "SELECT COUNT(*) AS c FROM file_parse_tasks "
            "WHERE project_id = ? AND status IN ('completed', 'failed')",
            (project_id,),
        ).fetchone()
    assert (before["c"] if before else 0) >= 1, "上传后应已有终态任务行"

    out = client.delete(f"/api/v1/projects/{project_id}/files/texts").json()
    assert out["tasks_cleared"] >= 1, out
    with get_db(readonly=True) as conn:
        after = conn.execute(
            "SELECT COUNT(*) AS c FROM file_parse_tasks "
            "WHERE project_id = ? AND status IN ('completed', 'failed')",
            (project_id,),
        ).fetchone()
    assert (after["c"] if after else 0) == 0, out

    rows = _rows(project_id)
    assert rows[body["id"]]["status"] == "pending", rows[body["id"]]
