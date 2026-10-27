# -*- coding: utf-8 -*-
"""第九轮：专项方案解析（要点提取）数据链闭环测试（2026-10-26）。

修复前实测缺陷（A/B 反向验证的「B」）：
  `ai_module._validate_field_name('scheme_extraction_json')` 直接抛
  ``ValueError: 非法字段名: scheme_extraction_json`` —— 因为该列不在
  ``_ALLOWED_FIELD_NAMES`` 白名单内。而 ``POST /ai/scheme-extraction`` 用
  try/except 吞掉异常仅打 warning，于是**五类专项提取结果从未写入 analysis 表**，
  前端/目录生成永远读不到已识别方案类型（调用链路断裂，但端点仍返回 200 + 结果，
  属于最隐蔽的一类「假成功」）。

本文件与 ``test_scheme_extraction_endpoint_2026_10_23.py`` 的差异：
  旧测试把 ``_sync_insert_or_update`` 整体 patch 成 Mock，恰恰掩盖了该缺陷；
  这里**使用真实 DB 与真实落库函数**，只隔离 AI 调用与鉴权。
"""

import json

import pytest

import app.routers.ai as ai_module
from app.routers.sse import sse_outline
from app.services.ai.workflows_scheme_extraction import parse_scheme_extraction_raw


SAMPLE_RESULTS = {
    "project_overview": [{"field": "工程名称", "value": "某基坑工程"}],
    "scheme_type": [{"scheme_type": "基坑工程"}],
    "dangerous_work_params": [{"param": "开挖深度", "value": "6.5", "unit": "m"}],
    "basis_list": [{"category": "标准", "name": "施工脚手架通用规范"}],
    "construction_condition": [{"category": "周边建筑", "description": "东侧住宅"}],
}


def _mk_project(name: str = "R9-scheme") -> int:
    from app.db import get_db

    with get_db() as conn:
        conn.execute("INSERT INTO projects (name) VALUES (?)", (name,))
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _read_raw(project_id: int) -> str | None:
    from app.db import get_db

    with get_db() as conn:
        row = conn.execute(
            "SELECT scheme_extraction_json FROM analysis WHERE project_id = ?",
            (project_id,),
        ).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# F-A：白名单与真实落库
# ---------------------------------------------------------------------------
def test_allowed_field_names_includes_scheme_extraction_json():
    """白名单必须包含 scheme_extraction_json（否则落库必抛 ValueError）。"""
    assert "scheme_extraction_json" in ai_module._ALLOWED_FIELD_NAMES
    # 反向证明：白名单校验对合法列不再抛错
    assert ai_module._validate_field_name("scheme_extraction_json") == "scheme_extraction_json"


def test_sync_insert_or_update_persists_scheme_extraction_json():
    """真实 DB：_sync_insert_or_update 能把专项提取 JSON 写入并可被 _sync_get_analysis 读回。"""
    pid = _mk_project("R9-sync-insert")
    payload = json.dumps(SAMPLE_RESULTS, ensure_ascii=False)

    ai_module._sync_insert_or_update(pid, payload, "scheme_extraction_json")

    row = ai_module._sync_get_analysis(pid)
    assert row is not None
    assert json.loads(row["scheme_extraction_json"]) == SAMPLE_RESULTS
    # 该列没有「已确认」标记列，不应被派生写入
    assert ai_module._derive_confirmed_field("scheme_extraction_json") is None


@pytest.mark.asyncio
async def test_generate_scheme_extraction_actually_persists_to_db():
    """端到端数据链：端点返回结果的同时，结果必须真正落到 analysis 表。

    这是旧测试（patch 掉 _sync_insert_or_update）遗漏的核心断言。
    """
    from unittest.mock import AsyncMock, patch

    pid = _mk_project("R9-endpoint-persist")
    with patch.object(ai_module, "enforce_project_access", new=AsyncMock()), \
         patch.object(ai_module, "latest_extracted_text", new=AsyncMock(return_value="全文内容")), \
         patch.object(ai_module, "get_ai_config", new=lambda: {}), \
         patch.object(ai_module, "get_working_provider", new=AsyncMock(return_value=(None, None))), \
         patch.object(ai_module, "run_all_scheme_extractions", new=AsyncMock(return_value=SAMPLE_RESULTS)):
        resp = await ai_module.generate_scheme_extraction(pid, None)

    assert resp["scheme_extraction"] == SAMPLE_RESULTS
    raw = _read_raw(pid)
    assert raw, "专项提取结果未落库（数据链断裂）"
    assert json.loads(raw) == SAMPLE_RESULTS
    # 下游 GET /analysis 能读回同一份结果
    readback = await ai_module.get_analysis(pid, None)
    assert readback["scheme_extraction"] == SAMPLE_RESULTS


@pytest.mark.asyncio
async def test_scheme_outline_template_reuses_persisted_result():
    """落库后，目录模板预览端点应能按已识别方案类型返回专用模板（跨模块消费）。"""
    from unittest.mock import AsyncMock, patch

    pid = _mk_project("R9-template-consume")
    ai_module._sync_insert_or_update(
        pid, json.dumps(SAMPLE_RESULTS, ensure_ascii=False), "scheme_extraction_json"
    )
    with patch.object(ai_module, "enforce_project_access", new=AsyncMock()):
        resp = await ai_module.get_scheme_outline_template(pid, None)
    assert resp["scheme_type"] == "基坑工程"
    assert resp["is_default"] is False
    assert "基坑监测方案" in [c["title"] for c in resp["outline_template"]]


# ---------------------------------------------------------------------------
# F-B：清除分析必须清掉专项提取残留
# ---------------------------------------------------------------------------
def test_clear_analysis_resets_scheme_extraction_json():
    pid = _mk_project("R9-clear")
    ai_module._sync_insert_or_update(
        pid, json.dumps(SAMPLE_RESULTS, ensure_ascii=False), "scheme_extraction_json"
    )
    assert json.loads(_read_raw(pid)) == SAMPLE_RESULTS

    ai_module._sync_clear_analysis(pid)

    raw = _read_raw(pid)
    assert raw in ("[]", "", None), f"清除分析后仍残留旧方案类型: {raw!r}"
    assert parse_scheme_extraction_raw(raw) == ("", [])
    # 其他分析字段同样被清（回归护栏）
    row = ai_module._sync_get_analysis(pid)
    assert row["summary_json"] == ""
    assert row["global_facts_json"] == "[]"


# ---------------------------------------------------------------------------
# F-D：解析口径单一真相（两处读取函数不得漂移）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, ("", [])),
        ("", ("", [])),
        ("not-json", ("", [])),
        ("[]", ("", [])),
        ('{"scheme_type":[{"scheme_type":" 模板工程及支撑体系 "}]}',
         ("模板工程及支撑体系", [])),
        ('{"scheme_type":"基坑工程"}', ("基坑工程", [])),
        ('{"scheme_type":[{"scheme_type":""}]}', ("", [])),
        ('{"scheme_type":[{"scheme_type":"拆除工程"}],"dangerous_work_params":"oops"}',
         ("拆除工程", [])),
        ({"scheme_type": [{"scheme_type": "脚手架工程"}],
          "dangerous_work_params": [{"param": "搭设高度"}]},
         ("脚手架工程", [{"param": "搭设高度"}])),
    ],
)
def test_parse_scheme_extraction_raw_shapes(raw, expected):
    assert parse_scheme_extraction_raw(raw) == expected


def test_two_reader_functions_share_same_parsing_result():
    """ai.py 与 sse_outline.py 的读取函数对同一原始值必须给出完全一致的结果。

    两者各自保留 DB 读取（各自模块的 get_db 便于测试隔离），但解析口径共用
    ``parse_scheme_extraction_raw``，此处用同一份伪造连接分别驱动两条链路。
    """
    payload = '{"scheme_type":[{"scheme_type":"起重吊装及安装拆卸工程"}],' \
              '"dangerous_work_params":[{"param":"起重量","value":"120"}]}'

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a, **k):
            class _Cur:
                def fetchone(self):
                    return (payload,)
            return _Cur()

    import unittest.mock as mock

    with mock.patch.object(ai_module, "get_db", new=lambda: _Conn()):
        ai_result = ai_module._read_scheme_extraction(1)
    with mock.patch.object(sse_outline, "get_db", new=lambda: _Conn()):
        outline_result = sse_outline._read_scheme_extraction_for_outline(1)
    assert ai_result == outline_result == (
        "起重吊装及安装拆卸工程",
        [{"param": "起重量", "value": "120"}],
    )


# ---------------------------------------------------------------------------
# F-E：解析质量 error 不再静默成功（回填 message 供前端展示）
# ---------------------------------------------------------------------------
def test_parse_quality_error_message_empty_for_normal_text(tmp_path):
    import app.services.files as files_module

    p = tmp_path / "ok.docx"
    p.write_bytes(b"x" * 3000)  # 体积正常
    assert files_module._parse_quality_error_message(str(p), "第一章 工程概况\n" * 40) == ""


def test_parse_quality_error_message_flags_control_char_text(tmp_path):
    import app.services.files as files_module

    p = tmp_path / "garbage.pdf"
    p.write_bytes(b"%PDF-1.4" + b"\x00" * 3000)
    msg = files_module._parse_quality_error_message(str(p), "开头" + "\x00" * 300 + "结尾")
    assert msg.startswith("解析质量异常：")
    assert "控制字符" in msg


def test_persist_file_extraction_surfaces_quality_warning(tmp_path):
    """异步解析主链路：提取出乱码文本时 status 仍 success，但 message 必须带质量告警。"""
    import unittest.mock as mock

    import app.services.files as files_module
    from app.db import get_db

    pid = _mk_project("R9-quality-message")
    src = tmp_path / "garbage.pdf"
    src.write_bytes(b"%PDF-1.4" + b"\x00" * 4000)
    with get_db() as conn:
        conn.execute(
            "INSERT INTO uploaded_files (project_id, filename, path, file_type, "
            "file_role, status, extracted_text_path, message) "
            "VALUES (?, ?, ?, ?, 'main', 'processing', '', '')",
            (pid, "garbage.pdf", str(src), "pdf"),
        )
        fid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    garbage = "正常开头" + "\x00" * 300 + "结尾"
    with mock.patch.object(files_module, "extract_text_from_path", return_value=garbage):
        status = files_module._persist_file_extraction(pid, fid, str(src), "garbage.pdf", ".pdf")

    assert status == "success"
    with get_db() as conn:
        row = conn.execute(
            "SELECT status, message FROM uploaded_files WHERE id = ?", (fid,)
        ).fetchone()
    assert row["status"] == "success"
    assert "解析质量异常" in row["message"]
