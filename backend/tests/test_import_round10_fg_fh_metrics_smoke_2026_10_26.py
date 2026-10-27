# -*- coding: utf-8 -*-
"""上传解析 / 要点提取模块 —— 2026-10-26 第十轮（F-G / F-H / 指标 / HTTP smoke）。

覆盖四项本轮改进：

1. **F-G（写盘失败口径）**：``re_extract_text`` 写文本失败时，旧解析结果必须
   **完整保留**（改为临时文件 + 原子替换），并在响应与 ``uploaded_files.message``
   中显式说明「已保留上一次结果」，而不是只在 toast 里闪一下失败。
2. **F-H（老库 reclaim 误判）**：``uploaded_files`` 缺 ``updated_at`` 列时，
   ``reclaim_stale_parse_state`` **不得**降级用 ``created_at`` 回收——那正是
   async-reparse 在途任务被误杀的根因。缺列时跳过该表回收，只回收
   ``file_parse_tasks``（该表自带 updated_at）。
3. **专项提取指标**：``run_all_scheme_extractions`` 按任务记录耗时/成败，
   ``scheme_extraction_metrics_snapshot`` 可读，``GET /ai/scheme-extraction-metrics``
   可查（进程内、重启归零）。
4. **HTTP 级联调 smoke**：项目创建 → 上传 docx → 读取提取文本 → 专项提取
   （仅替换 AI provider，其余走真实服务）→ ``GET /analysis`` 回读 →
   ``GET /scheme-outline-template`` 选型，验证前后端数据链在 HTTP 层贯通。
"""

import io
import json
import sqlite3
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.main import app  # noqa: E402
from app.db import get_db  # noqa: E402
from app.routers import ai as ai_module  # noqa: E402
from app.services import files as files_mod  # noqa: E402
from app.services.ai import workflows_scheme_extraction as wse  # noqa: E402


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def project_id(client):
    resp = client.post("/api/v1/projects", json={
        "name": "上传解析 2026-10-26 第十轮",
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


# ============================================================================
# 1. F-G：写盘失败 → 旧文本完整保留 + 提示显式可见
# ============================================================================


class TestReExtractWriteFailurePreservesOldText:
    def test_write_failure_keeps_old_text_and_surfaces_message(
        self, client, project_id, monkeypatch
    ):
        body = _upload(
            client,
            project_id,
            "基坑专项方案.docx",
            _make_docx_bytes("基坑开挖深度 6.5m\n支护形式为钻孔灌注桩+内支撑"),
        )
        fid = body["id"]
        first = _file_row(fid)
        assert first["status"] == "success", first
        old_path = first["extracted_text_path"]
        assert old_path and Path(old_path).exists()
        old_text = Path(old_path).read_text(encoding="utf-8")
        assert "基坑开挖深度" in old_text

        def _boom(self, *args, **kwargs):
            raise OSError(28, "No space left on device")

        # 只让「写文本」失败：上传已完成，重提取阶段的唯一 write_text 就是落盘文本
        monkeypatch.setattr(Path, "write_text", _boom)

        resp = client.post(f"/api/v1/projects/{project_id}/files/{fid}/re-extract")
        assert resp.status_code == 200, resp.text
        payload = resp.json()

        # ① 如实声明失败，且明确旧结果仍在
        assert payload["status"] == "failed", payload
        assert payload.get("text_preserved") is True, payload
        assert "保留上一次解析结果" in payload["message"], payload

        # ② DB 状态不被污染：status/路径仍是旧的成功结果，message 变成可见提示
        row = _file_row(fid)
        assert row["status"] == "success", row
        assert row["extracted_text_path"] == old_path, row
        assert "保留上一次解析结果" in row["message"], row

        # ③ 旧文本文件内容未被截断（原子替换的关键收益）
        assert Path(old_path).read_text(encoding="utf-8") == old_text

        # ④ 下游读取链路仍可用
        text_resp = client.get(f"/api/v1/projects/{project_id}/files/{fid}/text")
        assert text_resp.status_code == 200
        assert "基坑开挖深度" in text_resp.json()["text"]


# ============================================================================
# 2. F-H：老库缺 updated_at → 不降级 created_at，不误杀在途任务
# ============================================================================


def _sqlite_get_db(db_path: str):
    def _fake_get_db(*args, **kwargs):
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    return _fake_get_db


class TestReclaimOnLegacySchemaSkipsUploadedFiles:
    def test_missing_updated_at_column_does_not_use_created_at(self, tmp_path):
        db_path = str(tmp_path / "legacy_no_updated_at.db")
        with sqlite3.connect(db_path) as conn:
            # 老 schema：uploaded_files 只有 created_at
            conn.execute(
                """CREATE TABLE uploaded_files (
                    id INTEGER PRIMARY KEY,
                    project_id INTEGER NOT NULL,
                    filename TEXT NOT NULL,
                    path TEXT NOT NULL,
                    file_type TEXT NOT NULL,
                    file_role TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    extracted_text_path TEXT,
                    message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                )"""
            )
            conn.execute(
                """CREATE TABLE file_parse_tasks (
                    id INTEGER PRIMARY KEY,
                    project_id INTEGER NOT NULL,
                    filename TEXT NOT NULL,
                    file_type TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0,
                    total_files INTEGER NOT NULL DEFAULT 1,
                    completed_files INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT NOT NULL DEFAULT '',
                    started_at TEXT,
                    updated_at TEXT,
                    completed_at TEXT
                )"""
            )
            # created_at 已超阈值，但老库无法判断状态最近变更时间（async-reparse 场景）
            conn.execute(
                """INSERT INTO uploaded_files
                   (id, project_id, filename, path, file_type, file_role, status,
                    created_at)
                   VALUES (1, 1, 'in-flight.pdf', '/tmp/in-flight.pdf', 'pdf', 'main',
                           'processing', datetime('now', 'localtime', '-120 minutes'))"""
            )
            conn.execute(
                """INSERT INTO file_parse_tasks
                   (id, project_id, filename, file_type, status, progress,
                    total_files, completed_files, error_message, started_at,
                    updated_at, completed_at)
                   VALUES (1, 1, 'in-flight.pdf', 'pdf', 'processing', 10, 1, 0, '',
                           datetime('now', 'localtime', '-120 minutes'),
                           datetime('now', 'localtime', '-120 minutes'), NULL)"""
            )
            conn.commit()

        with patch.object(files_mod, "get_db", _sqlite_get_db(db_path)):
            out = files_mod.reclaim_stale_parse_state(stale_minutes=60)

        # uploaded_files：缺 updated_at → 跳过回收，宁可少回收也不误杀
        assert out["uploaded_files_failed"] == 0, out
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT status, message FROM uploaded_files WHERE id = 1"
            ).fetchone()
            assert row[0] == "processing", "老库缺 updated_at 时不得用 created_at 误判"
            assert row[1] == "", "不得写入 stale 失败提示"

            # file_parse_tasks 自带 updated_at，仍可正常回收
            task = conn.execute(
                "SELECT status FROM file_parse_tasks WHERE id = 1"
            ).fetchone()
            assert task[0] == "failed"
        assert out["parse_tasks_failed"] == 1, out

    def test_table_has_column_probe(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE t1 (id INTEGER)")
        assert files_mod._table_has_column(conn, "t1", "id") is True
        assert files_mod._table_has_column(conn, "t1", "nope") is False
        assert files_mod._table_has_column(conn, "missing_table", "id") is False
        conn.close()


# ============================================================================
# 3. 专项提取：结构化日志字段 + 耗时指标
# ============================================================================


def _detect_task(messages) -> str:
    last = messages[-1].content
    if "专项方案类型" in last:
        return "scheme_type"
    if "危大工程判定参数" in last:
        return "dangerous_work_params"
    if "编制依据清单" in last:
        return "basis_list"
    if "施工条件" in last:
        return "construction_condition"
    return "project_overview"


_PAYLOADS = {
    "project_overview": [{"field": "工程名称", "value": "示范工程", "unit": "", "source": "封面"}],
    "scheme_type": [{"scheme_type": "基坑工程", "main_content": "深基坑开挖", "confidence": "高"}],
    "dangerous_work_params": [{"param": "开挖深度", "value": "6.5", "unit": "m"}],
    "basis_list": [{"category": "标准", "name": "施工脚手架通用规范", "code": "GB 55023-2022"}],
    "construction_condition": [{"category": "周边建筑", "description": "东侧 8m 有住宅"}],
}


class _JsonProvider:
    """按任务专属提示词返回固定 JSON，可指定让某类任务抛错。"""

    def __init__(self, *, fail_tasks=()):
        self.fail_tasks = set(fail_tasks)
        self.calls: list[str] = []
        self.model = "fake-model"
        self.base_url = "http://fake.local"

    async def chat(self, messages, **kwargs):
        task = _detect_task(messages)
        self.calls.append(task)
        if task in self.fail_tasks:
            raise RuntimeError(f"{task} AI 超时")
        return json.dumps(_PAYLOADS[task], ensure_ascii=False)


class TestSchemeExtractionMetrics:
    def setup_method(self):
        wse.reset_scheme_extraction_metrics()

    def teardown_method(self):
        wse.reset_scheme_extraction_metrics()

    async def test_batch_records_task_and_batch_metrics(self):
        provider = _JsonProvider()
        results = await wse.run_all_scheme_extractions(provider, "招标文件全文")

        assert set(results) == set(wse.SCHEME_EXTRACTION_TASKS)
        assert all("error" not in r for r in results.values())

        snap = wse.scheme_extraction_metrics_snapshot()
        assert snap["batch"]["batches"] == 1
        assert snap["batch"]["task_runs"] == 5
        assert snap["batch"]["failed_tasks"] == 0
        assert snap["batch"]["last_duration_ms"] >= 0
        assert snap["batch"]["avg_duration_ms"] >= 0

        for task in wse.SCHEME_EXTRACTION_TASKS:
            m = snap["tasks"][task]
            assert m["runs"] == 1, m
            assert m["success"] == 1 and m["failure"] == 0, m
            assert m["last_outcome"] == "success"
            assert m["last_duration_ms"] >= 0
            assert m["max_duration_ms"] >= m["last_duration_ms"]
            assert m["last_finished_at"], "应记录完成时刻（便于日志对齐）"

    async def test_failed_task_counted_and_snapshot_average(self):
        provider = _JsonProvider(fail_tasks={"dangerous_work_params"})
        results = await wse.run_all_scheme_extractions(provider, "招标文件全文")
        assert "error" in results["dangerous_work_params"]
        assert "error" not in results["scheme_type"]

        snap = wse.scheme_extraction_metrics_snapshot()
        assert snap["batch"]["failed_tasks"] == 1
        bad = snap["tasks"]["dangerous_work_params"]
        assert bad["runs"] == 1 and bad["failure"] == 1 and bad["success"] == 0
        assert bad["last_outcome"] == "failed"
        assert "AI 超时" in bad["last_error"]
        # 平均耗时对失败任务同样有值（失败也会记录耗时）
        assert bad["avg_duration_ms"] >= 0

    async def test_reset_clears_metrics(self):
        await wse.run_all_scheme_extractions(_JsonProvider(), "全文")
        assert wse.scheme_extraction_metrics_snapshot()["batch"]["batches"] == 1
        wse.reset_scheme_extraction_metrics()
        snap = wse.scheme_extraction_metrics_snapshot()
        assert snap["tasks"] == {}
        assert snap["batch"]["batches"] == 0


# ============================================================================
# 4. HTTP 级联调 smoke：上传 → 解析 → 专项提取 → 回读 → 目录选型
# ============================================================================


class TestUploadToSchemeOutlineHttpSmoke:
    def test_full_chain_over_http(self, client, project_id):
        wse.reset_scheme_extraction_metrics()

        # ① 上传（同步解析）
        body = _upload(
            client,
            project_id,
            "深基坑专项方案.docx",
            _make_docx_bytes(
                "工程概况：地下三层车库深基坑工程\n"
                "基坑开挖深度 6.5m，支护形式为钻孔灌注桩+内支撑\n"
                "编制依据：GB 55023-2022 施工脚手架通用规范"
            ),
        )
        fid = body["id"]
        row = _file_row(fid)
        assert row["status"] == "success", row
        assert row["extracted_text_path"], "提取文本路径必须落库"

        # ② 单文件文本 / 合并文本可读（数据链第一段）
        one = client.get(f"/api/v1/projects/{project_id}/files/{fid}/text")
        assert one.status_code == 200 and "基坑开挖深度" in one.json()["text"]
        merged = client.get(f"/api/v1/projects/{project_id}/files/text")
        assert merged.status_code == 200 and "基坑开挖深度" in merged.json()["text"]

        # ③ 专项提取：只替换 AI provider，其余（并发编排、落库、指标）走真实代码
        provider = _JsonProvider()
        with patch.object(ai_module, "get_ai_config", new=lambda: {}), \
             patch.object(
                 ai_module,
                 "get_working_provider",
                 new=AsyncMock(return_value=(provider, None)),
             ):
            resp = client.post(f"/api/v1/projects/{project_id}/ai/scheme-extraction")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["scheme_extraction"]["scheme_type"][0]["scheme_type"] == "基坑工程"
        assert data["scheme_extraction_json"].strip()

        # ④ GET /analysis 回读（数据链第二段：落库 → 接口契约）
        analysis = client.get(f"/api/v1/projects/{project_id}/ai/analysis")
        assert analysis.status_code == 200, analysis.text
        se = analysis.json()["scheme_extraction"]
        assert se["scheme_type"][0]["scheme_type"] == "基坑工程"
        assert analysis.json()["scheme_extraction_json"].strip()

        # ⑤ 目录模板选型（数据链第三段：已识别类型 → 推荐模板）
        tpl = client.get(f"/api/v1/projects/{project_id}/ai/scheme-outline-template")
        assert tpl.status_code == 200, tpl.text
        tpl_data = tpl.json()
        assert tpl_data["scheme_type"] == "基坑工程"
        assert tpl_data["is_default"] is False
        assert isinstance(tpl_data["outline_template"], list) and tpl_data["outline_template"]

        # ⑥ 指标接口可查（HTTP 层暴露结构化指标）
        metrics = client.get("/api/v1/ai/scheme-extraction-metrics")
        assert metrics.status_code == 200, metrics.text
        m = metrics.json()
        assert m["batch"]["batches"] == 1
        assert m["tasks"]["scheme_type"]["runs"] == 1
        assert m["tasks"]["scheme_type"]["last_duration_ms"] >= 0

        wse.reset_scheme_extraction_metrics()

    def test_scheme_extraction_without_files_returns_400(self, client, project_id):
        resp = client.post(f"/api/v1/projects/{project_id}/ai/scheme-extraction")
        assert resp.status_code == 400, resp.text
        assert "请先上传文件" in resp.json()["detail"]
