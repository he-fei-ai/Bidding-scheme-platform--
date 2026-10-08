"""上传解析 → 要点提取 跨模块数据链回归测试（2026-10-08）

锁定「上传解析模块」与「要点提取（facts）模块」之间的数据传递与路由衔接：
- 上传主招标文件（.docx）与事实变量文件（含「事实」关键词的 .txt）后，
  要点提取链路优先消费的 ``get_facts_file_text(project_id)`` 必须命中事实文件文本；
- ``latest_extracted_text(project_id)`` 必须合并主文件 + 事实文件文本，
  作为 AI 提取缺事实文件时的回退源（设计：事实文件 → 招标全文 → …）；
- 删除主文件后主文件自动降级、合并文本缓存失效，保证数据链一致无脏读。

这些契约是 routers/facts.py ``generate_facts`` 中
``get_facts_file_text`` → 否则 ``latest_extracted_text`` 回退 的数据链前提，
任一环节断裂都会导致要点提取拿到空文本 / 错文本。
"""
import asyncio
import io
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.main import app  # noqa: E402


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def project_id(client):
    resp = client.post(
        "/api/v1/projects",
        json={
            "name": "上传要点数据链测试项目",
            "industry": "公共建筑",
            "bidder_name": "测试投标方",
        },
    )
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


def _upload(client, project_id, filename, content: bytes, file_role: str = ""):
    return client.post(
        f"/api/v1/projects/{project_id}/files",
        files={"file": (filename, io.BytesIO(content), "application/octet-stream")},
        data={"file_role": file_role} if file_role else None,
    )


class TestUploadToFactsDataChain:
    """上传解析产物必须能被要点提取链路正确消费。"""

    def test_facts_file_text_selected_by_keyword(self, client, project_id):
        from app.services.files import get_facts_file_text, latest_extracted_text

        main = _upload(
            client, project_id, "招标文件.docx",
            _make_docx_bytes("招标正文内容：本工程工期360天，项目经理张三"),
        )
        assert main.status_code == 200, main.text
        assert main.json()["status"] == "success"

        facts_text = "项目经理：张三；工期：180天；质量目标：合格"
        facts = _upload(
            client, project_id, "事实清单.txt",
            facts_text.encode("utf-8"),
        )
        assert facts.status_code == 200, facts.text
        assert facts.json()["status"] == "success"

        # 要点提取优先源：按「事实」关键词命中事实文件文本
        selected = get_facts_file_text(project_id)
        assert facts_text in selected, (
            "get_facts_file_text 必须命中含「事实」关键词的文件文本"
        )

        # 回退源：合并主文件 + 事实文件文本，二者都应在合并文本里
        merged = asyncio.run(latest_extracted_text(project_id))
        assert "招标正文内容" in merged, "合并文本必须包含主招标文件正文"
        assert "项目经理：张三" in merged, "合并文本必须包含事实文件正文（回退源完整）"

    def test_no_facts_file_falls_back_to_main_text(self, client, project_id):
        from app.services.files import get_facts_file_text, latest_extracted_text

        main = _upload(
            client, project_id, "采购文件.docx",
            _make_docx_bytes("仅主文件：项目经理李四，工期200天"),
        )
        assert main.json()["status"] == "success"

        # 没有「事实」关键词文件时，要点提取优先源为空，应回退到招标全文
        assert get_facts_file_text(project_id) == ""
        assert "项目经理李四" in asyncio.run(latest_extracted_text(project_id))

    def test_main_file_deletion_invalidates_text_cache(self, client, project_id):
        from app.services.files import latest_extracted_text

        main = _upload(
            client, project_id, "招标文件.docx",
            _make_docx_bytes("主文件正文A"),
        )
        assert main.json()["status"] == "success"
        main_id = main.json()["id"]

        # 合并文本应含主文件正文
        assert "主文件正文A" in asyncio.run(latest_extracted_text(project_id))

        # 删除主文件后：主文件降级 + 合并文本缓存失效，旧正文不再泄漏
        del_resp = client.delete(f"/api/v1/projects/{project_id}/files/{main_id}")
        assert del_resp.status_code == 200, del_resp.text
        assert "主文件正文A" not in asyncio.run(latest_extracted_text(project_id))


class TestAsyncReparseDispatchContract:
    """遗留 L2：异步重解析端点的返回契约（dispatched_via 如实说明走哪条路）。"""

    def test_async_reparse_returns_dispatch_info(self, client, project_id):
        up = _upload(
            client, project_id, "异步入口.docx",
            _make_docx_bytes("异步派发正文"),
        )
        file_id = up.json()["id"]

        resp = client.post(
            f"/api/v1/projects/{project_id}/files/{file_id}/async-reparse"
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["file_id"] == file_id
        # 无 broker 时应为 inline（或 queued），绝不能是未派发
        assert data["dispatched_via"] in ("inline", "celery", "none")
        assert "dispatched" in data
