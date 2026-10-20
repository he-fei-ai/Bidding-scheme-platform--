"""日志与可观测性加固回归测试（2026-10-18）。

覆盖：
- D2/P1：导出 SSE 帧序列化兜底（不可序列化负载不杀流）；
- P2-14：跨线程 traceId 上下文传播；
- P2：结构化 JSON 日志字段与敏感信息脱敏。
"""
from __future__ import annotations

import io
import json
import logging
import threading
from datetime import datetime

import pytest

from app.logging_context import (
    ContextFilter,
    bind_project,
    bind_request,
    make_context_bound,
    snapshot,
)
from app.logging_format import JsonFormatter, desensitize, log_error, log_step
from app.routers.export import _sse_frame


# ────────────────────────── D2: SSE 序列化兜底 ──────────────────────────
def test_sse_frame_tolerates_unserializable_payload():
    """负载含 datetime/set/object 等不可序列化对象时，仍产出合法 SSE 帧。"""
    payload = {
        "event": "completed",
        "check_result": {
            "warnings": [datetime.now(), {"x": set([1, 2])}, object()],
            "errors": [type("X", (), {})()],
        },
    }
    frame = _sse_frame(5, "completed", payload)
    assert frame.startswith("id: 5\n")
    assert "event: completed\n" in frame
    data_line = [ln for ln in frame.splitlines() if ln.startswith("data: ")][0]
    parsed = json.loads(data_line[len("data: ") :])
    # 正常路径不应触发兜底降级
    assert parsed.get("_serialization_error") is None
    assert parsed["event"] == "completed"


def test_sse_frame_normal_payload_roundtrip():
    """正常负载行为不变，可被 json.loads 还原。"""
    frame = _sse_frame(None, "progress", {"p": 1})
    data_line = [ln for ln in frame.splitlines() if ln.startswith("data: ")][0]
    assert json.loads(data_line[len("data: ") :]) == {"p": 1}


def test_sse_json_default_never_raises():
    from app.logging_format import _json_default

    assert isinstance(_json_default(object()), str)


# ──────────────────── P2-14: 跨线程 traceId 传播 ────────────────────
def test_context_propagates_into_thread():
    """在主线程绑定 rid/pid 后，子线程内应能读到同一链路标识。"""
    rid = bind_request()
    bind_project(777)
    captured: dict = {}

    def worker():
        captured.update(snapshot())

    # 关键：make_context_bound 必须在**发起侧**调用，才能复制发起线程的上下文
    t = threading.Thread(target=make_context_bound(worker), daemon=True)
    t.start()
    t.join(timeout=5)

    assert captured.get("request_id") == rid, "子线程未继承 request_id"
    assert captured.get("project_id") == "777", "子线程未继承 project_id"


def test_bind_snapshot_restores_and_ignores_missing():
    from app.logging_context import bind_snapshot, reset

    reset()
    bind_snapshot({"request_id": "abc", "project_id": "123"})
    snap = snapshot()
    assert snap["request_id"] == "abc"
    reset()
    bind_snapshot(None)  # 不应抛异常
    assert snapshot()["request_id"] == "-"


# ────────────────────── P2: 结构化日志与脱敏 ──────────────────────
@pytest.fixture
def app_log_capture():
    """临时接管 `app` logger（log_step/log_error 的默认出口），用后**完整还原**。

    直接改全局 logger 的 handlers 有污染其他测试的风险，故用 fixture 保存并
    恢复原 handlers/level/propagate。
    """
    lg = logging.getLogger("app")
    saved = (list(lg.handlers), lg.level, lg.propagate)
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(ContextFilter())
    lg.handlers = [handler]
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    try:
        yield buf
    finally:
        lg.handlers, lg.level, lg.propagate = saved


def _capture_json(logger_name: str) -> tuple[io.StringIO, logging.Logger]:
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(ContextFilter())
    lg = logging.getLogger(logger_name)
    lg.handlers = [handler]
    lg.setLevel(logging.DEBUG)
    lg.propagate = False
    return buf, lg


def test_json_formatter_includes_trace_fields():
    from app.logging_context import reset

    reset()
    rid = bind_request()
    bind_project(999)
    buf, lg = _capture_json("probe.json.fmt")
    lg.error("boom happened")
    rec = json.loads(buf.getvalue().strip())
    assert rec["level"] == "ERROR"
    assert rec["rid"] == rid
    assert rec["pid"] == "999"
    assert rec["msg"] == "boom happened"


def test_log_step_emits_structured_fields(app_log_capture):
    log_step("ai_call", status="ok", provider="agnes", duration_ms=123.4)
    rec = json.loads(app_log_capture.getvalue().strip())
    assert rec["step"] == "ai_call"
    assert rec["status"] == "ok"
    assert rec["duration_ms"] == 123.4


def test_log_error_emits_standardized_fields(app_log_capture):
    try:
        raise RuntimeError("db busy")
    except Exception as e:
        log_error("导出失败", e, error_code="DB_LOCKED", error_layer="db", retryable=True)
    rec = json.loads(app_log_capture.getvalue().strip())
    assert rec["error_code"] == "DB_LOCKED"
    assert rec["error_layer"] == "db"
    assert rec["retryable"] is True
    assert "exc" in rec  # 异常栈必须保留，杜绝 error=None


def test_desensitize_masks_sensitive_keys():
    masked = desensitize({"api_key": "sk-live", "model": "m", "n": {"authorization": "Bearer z"}})
    assert masked["model"] == "m"
    assert masked["api_key"] != "sk-live"
    assert masked["n"]["authorization"] != "Bearer z"


# ────────────────── P2: 运行期配置缺口自诊断 ──────────────────
def test_config_gaps_structure_and_no_secret_leak():
    """诊断输出必须是稳定结构，且**绝不回显密钥原文**。"""
    from app.config_diagnostics import collect_config_gaps

    gaps = collect_config_gaps()
    assert set(("generated_at", "overall", "items", "counts")) <= set(gaps)
    assert gaps["overall"] in ("ok", "degraded")
    assert set(gaps["counts"]) >= {"P0", "P1", "P2"}

    keys = {it["key"] for it in gaps["items"]}
    assert keys >= {"text_model", "image_model", "ocr", "mermaid_service"}

    for it in gaps["items"]:
        assert it["status"] in ("ok", "missing", "unknown")
        assert "message" in it
        if it["status"] == "missing":
            assert "severity" in it and "hint" in it
        # 硬性安全约束：任何字段都不得包含密钥原文
        blob = json.dumps(it, ensure_ascii=False).lower()
        for secret_hint in ("sk-", "bearer "):
            assert secret_hint not in blob, f"诊断字段疑似泄露敏感值: {it['key']}"
