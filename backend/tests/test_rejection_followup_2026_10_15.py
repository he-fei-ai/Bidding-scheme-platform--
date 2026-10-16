"""废标模块后续加固测试 — 2026-10-15 第二轮。

覆盖审计报告后续项：
  D8. split_into_chunks overlap 边界死循环防护（P1 硬修复）
  R1. Router 层集成测试（13+ 端点，含 404/403/400/409 错误路径）
  A1. AI 调用层 Mock 测试（三轮流程/降级/异常处理）
  E1. export_report 单元测试（DOCX 结构验证）

运行方式：
    cd backend && .venv/Scripts/python.exe -m pytest tests/test_rejection_followup_2026_10_15.py -q -p no:cacheprovider
"""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.db import get_db
from app.security.auth import User
from app.services import rejection as rj

OWNER = 100
STRANGER = 200


def _run(coro):
    return asyncio.run(coro)


def _status_of(fn, *args, **kwargs):
    with pytest.raises(HTTPException) as exc:
        _run(fn(*args, **kwargs))
    return exc.value


def _new_project(name="测试项目", created_by=OWNER):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO projects (name, industry, created_by) VALUES (?, ?, ?)",
            (name, "通用", created_by),
        )
        return conn.execute("SELECT id FROM projects ORDER BY id DESC LIMIT 1").fetchone()["id"]


def _seed_task(pid=None, status="completed", results=None, fps=None, check_items=None):
    tid = rj.create_task(
        pid, "招标正文",
        [{"id": 1, "name": "投标书.docx", "text": "投标正文内容"}],
        check_items or [{"id": "r001", "description": "投标文件未加盖公章", "category": "废标"}],
        ["rejection", "spelling", "logic"],
    )
    with get_db() as conn:
        conn.execute(
            "UPDATE rejection_checks SET status=?, results=?, false_positives=? WHERE id=?",
            (status, json.dumps(results or {}, ensure_ascii=False),
             json.dumps(fps or [], ensure_ascii=False), tid))
    return tid

# === D8. split_into_chunks overlap 边界死循环防护 ===

class TestSplitIntoChunksHardening:
    """D8: overlap >= chunk_size 导致死循环的防护。"""

    def test_overlap_equals_chunk_size_no_hang(self):
        """overlap == chunk_size → step=0 → 旧代码死循环；新代码钳制后安全返回。"""
        chunks = rj.split_into_chunks("X" * 500, chunk_size=100, overlap=100)
        assert len(chunks) > 0
        assert len(chunks) < 50  # 不会无限增长

    def test_overlap_larger_than_chunk_size_clamped(self):
        """overlap > chunk_size → step<0 → 旧代码倒退死循环；新代码钳制。"""
        chunks = rj.split_into_chunks("Y" * 500, chunk_size=100, overlap=200)
        assert len(chunks) > 0
        assert len(chunks) < 50

    def test_negative_overlap_treated_as_zero(self):
        """overlap < 0 → 钳制为 0（无重叠，纯顺序切分）。"""
        chunks = rj.split_into_chunks("Z" * 100, chunk_size=50, overlap=-10)
        assert len(chunks) == 2
        assert chunks[0] == "Z" * 50
        assert chunks[1] == "Z" * 50

    def test_normal_overlap_unchanged(self):
        """正常 overlap 不被钳制，行为不变。"""
        text = "A" * 500
        chunks = rj.split_into_chunks(text, chunk_size=200, overlap=50)
        assert len(chunks) > 1
        step = 200 - 50
        expected = 1 + (500 - 200 + step - 1) // step
        assert len(chunks) == expected

    def test_no_content_loss_after_clamp(self):
        """钳制后仍不丢内容：每个字符至少出现在一个分块中。"""
        text = "段" * 500
        chunks = rj.split_into_chunks(text, chunk_size=100, overlap=100)
        for ch in text:
            assert any(ch in c for c in chunks)

# === R1. Router 层集成测试 ===

class TestRouterStatusResult:
    """R1a: status / result 端点。"""

    def test_status_not_found_404(self):
        from app.routers.rejection import get_status
        exc = _status_of(get_status, 999999, user=User(OWNER, "o", "user"))
        assert exc.status_code == 404

    def test_result_not_found_404(self):
        from app.routers.rejection import get_result
        exc = _status_of(get_result, 999999, user=User(OWNER, "o", "user"))
        assert exc.status_code == 404

    def test_status_owner_ok(self):
        from app.routers.rejection import get_status
        pid = _new_project("状态-属主")
        tid = _seed_task(pid)
        r = _run(get_status(tid, user=User(OWNER, "o", "user")))
        assert r["id"] == tid and r["status"] == "completed"

    def test_result_owner_ok(self):
        from app.routers.rejection import get_result
        pid = _new_project("结果-属主")
        tid = _seed_task(pid, results={"rejection": [{"id": "i1", "description": "问题1"}]})
        r = _run(get_result(tid, user=User(OWNER, "o", "user")))
        assert r["id"] == tid
        assert "rejection" in r.get("results", {})


class TestRouterMarkFP:
    """R1b: mark-false-positive 端点。"""

    def test_mark_fp_not_found_404(self):
        from app.routers.rejection import mark_false_positive
        from app.models import MarkFalsePositiveRequest
        req = MarkFalsePositiveRequest(item_id="i1", is_false_positive=True)
        exc = _status_of(mark_false_positive, req, 999999, user=User(OWNER, "o", "user"))
        assert exc.status_code == 404

    def test_batch_mark_fp_not_found_404(self):
        from app.routers.rejection import batch_mark_false_positive
        from app.models import BatchMarkFalsePositiveRequest
        req = BatchMarkFalsePositiveRequest(task_id=999999, item_ids=["i1"])
        exc = _status_of(batch_mark_false_positive, req, user=User(OWNER, "o", "user"))
        assert exc.status_code == 404


class TestRouterRetryDimension:
    """R1c: retry-dimension 端点。"""

    def test_retry_not_found_404(self):
        from app.routers.rejection import retry_dimension
        from app.models import RetryDimensionRequest
        req = RetryDimensionRequest(dimension="rejection")
        exc = _status_of(retry_dimension, req, 999999, bg=None, user=User(OWNER, "o", "user"))
        assert exc.status_code == 404

    def test_retry_wrong_state_409(self):
        from app.routers.rejection import retry_dimension
        from app.models import RetryDimensionRequest
        pid = _new_project("重试-状态")
        tid = _seed_task(pid, status="pending")
        req = RetryDimensionRequest(dimension="rejection")
        exc = _status_of(retry_dimension, req, tid, bg=None, user=User(OWNER, "o", "user"))
        assert exc.status_code == 409

    def test_retry_dim_not_enabled_400(self):
        from app.routers.rejection import retry_dimension
        from app.models import RetryDimensionRequest
        pid = _new_project("重试-维度")
        tid = _seed_task(pid, status="completed")
        req = RetryDimensionRequest(dimension="nonexistent")
        exc = _status_of(retry_dimension, req, tid, bg=None, user=User(OWNER, "o", "user"))
        assert exc.status_code == 400


class TestRouterExportReport:
    """R1d: export-report 端点。"""

    def test_invalid_format_400(self):
        from app.routers.rejection import export_report
        pid = _new_project("导出-格式")
        tid = _seed_task(pid)
        exc = _status_of(export_report, tid, fmt="pdf", user=User(OWNER, "o", "user"))
        assert exc.status_code == 400

    def test_export_not_found_404(self):
        from app.routers.rejection import export_report
        exc = _status_of(export_report, 999999, fmt="docx", user=User(OWNER, "o", "user"))
        assert exc.status_code == 404


class TestRouterListAndDelete:
    """R1e: tasks / project-tasks / batch-delete 端点。"""

    def test_list_tasks_no_project_ok(self):
        from app.routers.rejection import list_tasks
        r = _run(list_tasks(None, 20, user=User(OWNER, "o", "user")))
        assert isinstance(r, list)

    def test_batch_delete_empty_ok(self):
        from app.routers.rejection import batch_delete_tasks
        from app.models import BatchTaskDeleteRequest
        req = BatchTaskDeleteRequest(task_ids=[])
        r = _run(batch_delete_tasks(req, user=User(OWNER, "o", "user")))
        assert r["success"] == 0


class TestRouterExtractAndImport:
    """R1f: extract-items / import-from-project 端点。"""

    def test_extract_items_empty_text_400(self):
        from app.routers.rejection import extract_items
        from app.models import ExtractItemsRequest
        req = ExtractItemsRequest(tender_text="")
        exc = _status_of(extract_items, req)
        assert exc.status_code == 400

    def test_import_from_project_not_found_404(self):
        from app.routers.rejection import import_from_project
        exc = _status_of(import_from_project, 999999, user=User(OWNER, "o", "user"))
        assert exc.status_code == 404


class TestRouterSchedules:
    """R1g: schedule / all-schedules 端点。"""

    def test_all_schedules_non_admin_403(self):
        from app.routers.rejection import list_all_schedules
        exc = _status_of(list_all_schedules, 100, user=User(OWNER, "o", "user"))
        assert exc.status_code == 403

    def test_all_schedules_admin_ok(self):
        from app.routers.rejection import list_all_schedules
        r = _run(list_all_schedules(100, user=User(OWNER, "o", "admin")))
        assert "total" in r and "items" in r

# === A1. AI 调用层 Mock 测试 ===

class TestAIMockSingleRound:
    """A1a: 单轮检查流程 Mock 测试。"""

    def test_single_round_produces_issues(self):
        mock_provider = MagicMock()

        async def mock_collect(provider, messages, max_retries=2):
            return [{"id": "i1", "description": "未加盖公章", "check_item_id": "r001"}]

        with patch("app.services.rejection.collect_json_response", side_effect=mock_collect):
            bid_doc = {"id": 1, "name": "投标书.docx", "text": "投标文件正文内容"}
            issues = _run(rj._run_rejection_single_round(
                mock_provider, bid_doc, [{"id": "r001", "description": "测试"}], "",
                ["投标文件正文内容"]
            ))
        assert len(issues) == 1 and issues[0]["description"] == "未加盖公章"

    def test_single_round_ai_failure_returns_empty(self):
        mock_provider = MagicMock()

        async def mock_collect(provider, messages, max_retries=2):
            raise Exception("AI 不可用")

        with patch("app.services.rejection.collect_json_response", side_effect=mock_collect):
            issues = _run(rj._run_rejection_single_round(
                mock_provider, {"id": 1, "name": "doc", "text": "内容"},
                [{"id": "r001", "description": "测试"}], "", ["内容"]
            ))
        assert issues == []


class TestAIMockThreeRoundFallback:
    """A1b: 三轮流程降级逻辑 Mock 测试。"""

    def test_fallback_on_round1_empty(self):
        with patch.object(rj, "_run_text_round", new_callable=AsyncMock, return_value=""):
            result = _run(rj._run_rejection_three_rounds(
                MagicMock(), {"id": 1, "name": "doc", "text": "内容"},
                [{"id": "r001", "description": "测试"}], "", ["内容"], "内容"
            ))
        assert result is None

    def test_fallback_on_round2_empty(self):
        with patch.object(rj, "_run_text_round", new_callable=AsyncMock, return_value=""):
            result = _run(rj._run_rejection_three_rounds(
                MagicMock(), {"id": 1, "name": "doc", "text": "内容"},
                [{"id": "r001", "description": "测试"}], "", ["内容"], "内容"
            ))
        assert result is None

    def test_fallback_on_round3_exception(self):
        async def mock_text_round(provider, prompt_key, user_prompt, timeout):
            return "模拟文本"

        async def mock_collect(provider, messages, max_retries=2):
            raise Exception("第三轮失败")

        with patch.object(rj, "_run_text_round", side_effect=mock_text_round):
            with patch("app.services.rejection.collect_json_response", side_effect=mock_collect):
                result = _run(rj._run_rejection_three_rounds(
                    MagicMock(), {"id": 1, "name": "doc", "text": "内容"},
                    [{"id": "r001", "description": "测试"}], "", ["内容"], "内容"
                ))
        assert result is None

    def test_three_round_success(self):
        async def mock_text_round(provider, prompt_key, user_prompt, timeout):
            return "模拟文本"

        async def mock_collect(provider, messages, max_retries=2):
            return [{"id": "i1", "description": "三轮定稿", "check_item_id": "r001"}]

        with patch.object(rj, "_run_text_round", side_effect=mock_text_round):
            with patch("app.services.rejection.collect_json_response", side_effect=mock_collect):
                result = _run(rj._run_rejection_three_rounds(
                    MagicMock(), {"id": 1, "name": "doc", "text": "内容"},
                    [{"id": "r001", "description": "测试"}], "", ["内容"], "内容"
                ))
        assert result is not None and len(result) == 1


class TestAIFinalizePipeline:
    """A1c: _finalize_rejection_issues 收尾管线测试。"""

    def test_finalize_assigns_fields(self):
        issues = [
            {"id": "i1", "description": "未加盖公章", "check_item_id": "r001", "quote": "公章"},
            {"id": "i2", "description": "未加盖公章", "check_item_id": "r001"},
        ]
        result = rj._finalize_rejection_issues(
            issues, {"id": 1, "name": "doc"}, [{"id": "r001"}], "加盖了公章"
        )
        assert len(result) == 1
        assert result[0]["bid_doc_id"] == 1
        assert result[0]["severity"] in ("critical", "warning", "info")
        assert "code_matched" in result[0]

    def test_finalize_empty(self):
        assert rj._finalize_rejection_issues([], {"id": 1}, [], "") == []

    def test_finalize_validates_evidence(self):
        issues = [{"id": "i1", "description": "问题", "quote": "安全生产许可证"}]
        result = rj._finalize_rejection_issues(
            issues, {"id": 1, "name": "doc"}, [{"id": "r001"}],
            "文本包含安全生产许可证的内容"
        )
        assert result[0]["validated"] is True
        assert result[0]["evidence_kind"] == "quote"

# === E1. export_report 单元测试 ===

class TestExportReportDocx:
    """E1: export_report DOCX 生成测试（使用真实 python-docx）。"""

    def test_export_not_found_raises(self):
        with pytest.raises(ValueError, match="任务不存在"):
            rj.export_report(999999)

    def test_export_generates_docx(self):
        tid = _seed_task(results={
            "rejection": [{"id": "i1", "description": "未加盖公章", "severity": "critical"}],
            "spelling": [], "logic": [],
        })
        path = rj.export_report(tid)
        assert path.endswith(".docx")
        from pathlib import Path
        assert Path(path).exists() and Path(path).stat().st_size > 0

    def test_export_false_positives_excluded(self):
        tid = _seed_task(
            results={"rejection": [
                {"id": "i1", "description": "问题1", "severity": "critical"},
                {"id": "i2", "description": "误报", "severity": "warning"},
            ], "spelling": [], "logic": []},
            fps=["i2"],
        )
        task = rj.get_task(tid)
        fps = set(task.get("false_positives", []))
        vis = [i for i in task["results"]["rejection"] if i.get("id") not in fps]
        assert len(vis) == 1 and vis[0]["id"] == "i1"

    def test_export_empty_results(self):
        tid = _seed_task(results={"rejection": [], "spelling": [], "logic": []})
        from pathlib import Path
        assert Path(rj.export_report(tid)).exists()

    def test_export_with_evidence_fields(self):
        tid = _seed_task(results={
            "rejection": [{
                "id": "i1", "description": "问题1", "severity": "critical",
                "validated": True, "line_hint": "第 5 行",
                "quote": "安全生产许可证", "suggestion": "补充证件",
            }], "spelling": [], "logic": [],
        })
        from pathlib import Path
        assert Path(rj.export_report(tid)).exists()