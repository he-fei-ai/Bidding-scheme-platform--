"""上传解析与要点提取模块 —— 2026-10-16 第八轮专项回归（F-6）

本轮在既有 7 轮审计（HEAD 590a957 + 工作区改动）基础上做**独立复核**。
前 7 轮的主线是「同一缺陷、不同入口」，本轮继续沿该主线排查剩余写入/读取
路径，并覆盖前几轮修复**自身**引入的字段语义问题：

  F-6（re_extract_text 写盘失败时 text_length 不实）
    2026-10-15b 的 F-1/F-2 修好了「写盘失败必须如实判 failed」，但
    re_extract_text 写盘失败分支的返回体把 text_length **硬编码为 0**。
    text_length 的契约是「本次提取到的文本长度」而非「落盘长度」：
    磁盘满/权限拒绝只发生在落盘环节，extracted_text 此时可能已提取到
    数千字。报 0 会让调用方把一次**写入故障**误读成「什么都没提取到」
    （内容问题），排查方向被带偏。空文本/提取失败路径的 extracted_text
    本就是空串，取值不变（既有回归锁定）。

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
from app.services import files as files_mod  # noqa: E402


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def project_id(client):
    resp = client.post("/api/v1/projects", json={
        "name": "上传解析 2026-10-16 回归项目",
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


def _file_row(file_id: int) -> dict:
    with get_db(readonly=True) as conn:
        row = conn.execute(
            "SELECT status, message, extracted_text_path FROM uploaded_files WHERE id = ?",
            (file_id,),
        ).fetchone()
    return dict(row) if row else {}


def _raise_on_txt_write(monkeypatch, error: OSError):
    """让 ``Path.write_text`` 仅在目标为提取文本时抛错（原始文件走 write_bytes，不受影响）。

    F-G FIX 2026-10-26：``re_extract_text`` 改为「先写 .txt.tmp 再原子替换」，
    因此失败注入必须同时覆盖临时文件（否则模拟不到落盘失败）。
    """
    real = files_mod.Path.write_text

    def raiser(self, data, *args, **kwargs):
        name = str(self)
        if name.endswith(".txt") or name.endswith(".txt.tmp"):
            raise error
        return real(self, data, *args, **kwargs)

    monkeypatch.setattr(files_mod.Path, "write_text", raiser)


# ============================================================================
# F-6：re_extract_text 写盘失败时 text_length 必须如实反映「已提取长度」
# ============================================================================


def test_reextract_write_failure_text_length_is_honest(client, project_id, monkeypatch):
    """F-6：提取成功但 .txt 落盘失败 → text_length 必须是真实提取长度，而不是 0。

    旧实现在该分支硬编码 text_length=0，而 extracted_text 此时可能已有数千字
    （磁盘满/权限拒绝只发生在落盘环节）——调用方会把「写入故障」误读成
    「什么都没提取到」，排查方向被带偏。
    """
    # 先正常上传（不 mock 写盘），拿到带正文的文件行
    content = "重提取时的真实正文内容"
    body = _upload(client, project_id, "含正文.txt", content.encode("utf-8"))
    assert body["status"] == "success", body

    # 再让 .txt 落盘失败并触发重提取
    _raise_on_txt_write(monkeypatch, OSError("disk full"))
    resp = client.post(f"/api/v1/projects/{project_id}/files/{body['id']}/re-extract")
    assert resp.status_code == 200, resp.text
    res = resp.json()

    assert res["status"] == "failed", res
    assert "写入提取文本失败" in res["message"], res
    # 核心：如实反映本次提取到的文本长度（非 0）
    assert res["text_length"] == len(content), (
        f"写盘失败分支把已提取 {len(content)} 字符的内容报成 0，"
        "调用方会把写入故障误读成内容问题"
    )
    # 写盘失败时刻意保留上一次成功落盘的旧文本（2026-09-18 设计：
    # 「写失败如实返回 failed 且保留旧文本」——旧文本仍可读，优于丢失），
    # 因此 DB 行的 extracted_text_path 仍是旧路径、status 不被改写。
    row = _file_row(body["id"])
    assert row["extracted_text_path"], dict(row)
    assert Path(row["extracted_text_path"]).exists(), dict(row)


def test_reextract_empty_text_length_stays_zero(client, project_id, monkeypatch):
    """反向对照：空文本路径的 text_length 仍是 0（既有回归锁定的行为不变）"""
    body = _upload(client, project_id, "空文本.txt", "初始内容".encode("utf-8"))

    monkeypatch.setattr(files_mod, "extract_text_from_path", lambda p: "")
    resp = client.post(f"/api/v1/projects/{project_id}/files/{body['id']}/re-extract")
    assert resp.status_code == 200, resp.text
    res = resp.json()

    assert res["status"] == "success", res
    assert res["text_length"] == 0, res
    row = _file_row(body["id"])
    assert row["extracted_text_path"] == "", dict(row)


def test_reextract_extract_crash_text_length_stays_zero(client, project_id, monkeypatch):
    """反向对照：提取崩溃路径的 text_length 仍是 0（extracted_text 本就是空串）"""

    def _boom(file_path: str) -> str:
        raise RuntimeError("模拟重提取崩溃")

    body = _upload(client, project_id, "崩溃.txt", "初始内容".encode("utf-8"))
    monkeypatch.setattr(files_mod, "extract_text_from_path", _boom)

    resp = client.post(f"/api/v1/projects/{project_id}/files/{body['id']}/re-extract")
    assert resp.status_code == 200, resp.text
    res = resp.json()

    assert res["status"] == "failed", res
    assert res["text_length"] == 0, res


# ============================================================================
# 静态守卫：写盘失败分支的 text_length 口径
# ============================================================================


def test_text_length_not_hardcoded_in_write_failure_branch():
    """静态守卫：re_extract_text 写盘失败分支不得再把 text_length 写死 0。

    该分支是 early return（位于函数尾部），锚定其 message 字面量后的
    text_length 取值。任何人把 ``len(extracted_text)`` 改回 ``0``，
    本用例即变红。
    """
    src = Path(files_mod.__file__).read_text(encoding="utf-8")
    # message 与 text_length 之间允许注释行（源码中该分支带 BUG-FIX 说明块）
    m = re.search(
        r'写入提取文本失败: \{e\}",.*?"text_length":\s*([^,\n]+),',
        src,
        re.S,
    )
    assert m, "未找到 re_extract_text 写盘失败分支的 text_length 字面量"
    assert m.group(1).strip() == "len(extracted_text)", (
        f"text_length 被写死为 {m.group(1).strip()}："
        "提取成功但落盘失败时，接口会把写入故障谎报成内容问题"
    )
