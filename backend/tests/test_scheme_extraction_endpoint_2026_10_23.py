# -*- coding: utf-8 -*-
"""专项提取端点 + 数据库迁移测试（标书智能体（一）引入）。

验证：
- POST /projects/{id}/ai/scheme-extraction 编排端点：取全文→并发提取→落库→返回；
- 单任务失败被隔离，端点仍 200 且其余任务可用；
- analysis 表 scheme_extraction_json 列增量迁移（幂等、不破坏老库）；
- GET /analysis 解析并返回 scheme_extraction。
"""

import sqlite3
from unittest.mock import AsyncMock, Mock, patch

import pytest

import app.routers.ai as ai_module
from app.db import _ensure_column


SAMPLE_RESULTS = {
    "project_overview": [{"field": "工程名称", "value": "某基坑工程"}],
    "scheme_type": [{"scheme_type": "基坑工程"}],
    "dangerous_work_params": [{"param": "开挖深度", "value": "6.5"}],
    "basis_list": [{"category": "标准", "name": "施工脚手架通用规范"}],
    "construction_condition": [{"category": "周边建筑", "description": "东侧住宅"}],
}


@pytest.mark.asyncio
async def test_generate_scheme_extraction_happy_path():
    mock_save = Mock()
    with patch.object(ai_module, "enforce_project_access", new=AsyncMock()), \
         patch.object(ai_module, "latest_extracted_text", new=AsyncMock(return_value="全文内容")), \
         patch.object(ai_module, "get_ai_config", new=lambda: {}), \
         patch.object(ai_module, "get_working_provider", new=AsyncMock(return_value=(None, None))), \
         patch.object(ai_module, "run_all_scheme_extractions", new=AsyncMock(return_value=SAMPLE_RESULTS)), \
         patch.object(ai_module, "_sync_insert_or_update", new=mock_save):
        resp = await ai_module.generate_scheme_extraction(123, None)
    assert resp["scheme_extraction"] == SAMPLE_RESULTS
    assert "scheme_extraction_json" in resp
    # 落库调用使用了正确列名
    assert mock_save.call_args.args[2] == "scheme_extraction_json"


@pytest.mark.asyncio
async def test_generate_scheme_extraction_no_file_raises_400():
    with patch.object(ai_module, "enforce_project_access", new=AsyncMock()), \
         patch.object(ai_module, "latest_extracted_text", new=AsyncMock(return_value="")):
        from fastapi import HTTPException

        try:
            await ai_module.generate_scheme_extraction(123, None)
        except HTTPException as e:
            assert e.status_code == 400
        else:
            raise AssertionError("空文件应抛 400")


@pytest.mark.asyncio
async def test_generate_scheme_extraction_isolates_task_failure():
    # 一个子任务报错，端点仍 200，且出错项标记 error，其余保留
    partial = dict(SAMPLE_RESULTS)
    partial["project_overview"] = {"error": "AI 超时"}

    with patch.object(ai_module, "enforce_project_access", new=AsyncMock()), \
         patch.object(ai_module, "latest_extracted_text", new=AsyncMock(return_value="全文内容")), \
         patch.object(ai_module, "get_ai_config", new=lambda: {}), \
         patch.object(ai_module, "get_working_provider", new=AsyncMock(return_value=(None, None))), \
         patch.object(ai_module, "run_all_scheme_extractions", new=AsyncMock(return_value=partial)), \
         patch.object(ai_module, "_sync_insert_or_update", new=lambda *a, **k: None):
        resp = await ai_module.generate_scheme_extraction(123, None)
    assert "error" in resp["scheme_extraction"]["project_overview"]
    assert resp["scheme_extraction"]["scheme_type"] == SAMPLE_RESULTS["scheme_type"]


@pytest.mark.asyncio
async def test_generate_scheme_extraction_provider_error_maps_502():
    with patch.object(ai_module, "enforce_project_access", new=AsyncMock()), \
         patch.object(ai_module, "latest_extracted_text", new=AsyncMock(return_value="全文内容")), \
         patch.object(ai_module, "get_ai_config", new=lambda: {}), \
         patch.object(ai_module, "get_working_provider", new=AsyncMock(return_value=(None, None))), \
         patch.object(ai_module, "run_all_scheme_extractions", new=AsyncMock(side_effect=RuntimeError("AI 挂了"))):
        from fastapi import HTTPException

        try:
            await ai_module.generate_scheme_extraction(123, None)
        except HTTPException as e:
            assert e.status_code == 502
        else:
            raise AssertionError("provider 故障应映射 502")


def test_scheme_extraction_column_migration_idempotent():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row  # 与 app 运行时一致，_get_column_names 依赖 Row 工厂
    conn.execute("CREATE TABLE analysis (project_id INTEGER)")
    # 增量迁移：添加列
    _ensure_column(conn, "analysis", "scheme_extraction_json", "TEXT NOT NULL DEFAULT '[]'")
    cols = [r[1] for r in conn.execute("PRAGMA table_info(analysis)")]
    assert "scheme_extraction_json" in cols
    # 幂等：再次执行不报错
    _ensure_column(conn, "analysis", "scheme_extraction_json", "TEXT NOT NULL DEFAULT '[]'")
    cols2 = [r[1] for r in conn.execute("PRAGMA table_info(analysis)")]
    assert cols2.count("scheme_extraction_json") == 1
    # 可写可读
    conn.execute(
        "INSERT INTO analysis (project_id, scheme_extraction_json) VALUES (?, ?)",
        (1, '{"project_overview":[]}'),
    )
    row = conn.execute("SELECT scheme_extraction_json FROM analysis WHERE project_id=?", (1,)).fetchone()
    assert row[0] == '{"project_overview":[]}'


@pytest.mark.asyncio
async def test_get_analysis_returns_scheme_extraction():
    fake_row = {
        "summary_json": "", "scoring_points_json": "[]", "confirmed_summary": 0,
        "confirmed_scoring": 0, "boq_json": "[]", "work_interface_json": "[]",
        "construction_method_json": "[]", "confirmed_boq": 0, "confirmed_work_interface": 0,
        "confirmed_construction_method": 0, "global_facts_json": "[]", "confirmed_facts": 0,
        "scheme_extraction_json": '{"project_overview":[{"field":"工程名称","value":"X"}]}',
        "scope": "", "facts_policy": "fabricate",
    }
    with patch.object(ai_module, "enforce_project_access", new=AsyncMock()), \
         patch.object(ai_module, "_sync_get_analysis", new=lambda pid: fake_row):
        resp = await ai_module.get_analysis(123, None)
    assert resp["scheme_extraction"] == {"project_overview": [{"field": "工程名称", "value": "X"}]}
    assert "scheme_extraction_json" in resp


class _FakeConn:
    """``get_db`` 行级读取的轻量替身（仅实现 execute().fetchone()）。"""

    def __init__(self, row):
        self._row = row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        class _Cur:
            def __init__(self, row):
                self._row = row

            def fetchone(self):
                return self._row

        return _Cur(self._row)


@pytest.mark.asyncio
async def test_get_scheme_outline_template_known_type():
    # analysis 中已识别方案类型为「基坑工程」
    payload = '{"scheme_type":[{"scheme_type":"基坑工程"}]}'
    with patch.object(ai_module, "enforce_project_access", new=AsyncMock()), \
         patch.object(ai_module, "get_db", new=lambda: _FakeConn((payload,))):
        resp = await ai_module.get_scheme_outline_template(123, None)
    assert resp["scheme_type"] == "基坑工程"
    assert resp["is_default"] is False
    # 推荐模板在通用 23 章基础上追加了基坑工程特有关卡
    titles = [ch["title"] for ch in resp["outline_template"]]
    assert "基坑监测方案" in titles
    # id 唯一
    ids = [ch["id"] for ch in resp["outline_template"]]
    assert len(ids) == len(set(ids))


@pytest.mark.asyncio
async def test_get_scheme_outline_template_default_when_unidentified():
    # 未识别方案类型：返回通用 23 章模板，is_default=true
    with patch.object(ai_module, "enforce_project_access", new=AsyncMock()), \
         patch.object(ai_module, "get_db", new=lambda: _FakeConn((None,))):
        resp = await ai_module.get_scheme_outline_template(123, None)
    assert resp["scheme_type"] == ""
    assert resp["is_default"] is True
    # 通用模板无方案特有关卡
    titles = [ch["title"] for ch in resp["outline_template"]]
    assert "基坑监测方案" not in titles


@pytest.mark.asyncio
async def test_get_scheme_outline_template_silent_on_db_error():
    # 数据库读取异常时安全降级为通用模板（不抛 500）
    with patch.object(ai_module, "enforce_project_access", new=AsyncMock()), \
         patch.object(ai_module, "get_db", new=lambda: _raise_conn()):
        resp = await ai_module.get_scheme_outline_template(123, None)
    assert resp["scheme_type"] == ""
    assert resp["is_default"] is True


@pytest.mark.asyncio
async def test_get_scheme_outline_template_deepens_by_dangerous_params():
    # 无方案类型但识别到危大参数 → 仍按危大参数深化「计算书与图纸」「应急处置」
    payload = ('{"dangerous_work_params":['
               '{"param":"开挖深度","value":"6.5","unit":"m","threshold":"超规模","source":"3.2"}'
               ']}')
    with patch.object(ai_module, "enforce_project_access", new=AsyncMock()), \
         patch.object(ai_module, "get_db", new=lambda: _FakeConn((payload,))):
        resp = await ai_module.get_scheme_outline_template(123, None)
    assert resp["scheme_type"] == ""
    # 有危大参数深化 → 非默认模板
    assert resp["is_default"] is False
    titles = [ch["title"] for ch in resp["outline_template"]]
    assert "基坑工程计算书（支护稳定性与降水验算）" in titles
    assert "基坑坍塌及涌水涌砂应急预案" in titles
    # 无方案类型特有关卡
    assert "基坑监测方案" not in titles
    # 来源标注：危大深化章节标注触发参数名
    src = {ch["title"]: ch.get("source") for ch in resp["outline_template"]}
    assert src["基坑工程计算书（支护稳定性与降水验算）"] == "危大参数：开挖深度"
    assert src["基坑坍塌及涌水涌砂应急预案"] == "危大参数：开挖深度"


def _raise_conn():
    raise RuntimeError("db down")
