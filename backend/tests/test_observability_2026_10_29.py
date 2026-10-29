"""可观测性（结构化日志/全链路追踪/运行轨迹/存储/指标/告警/查询）测试（2026-10-29）。

覆盖需求九「验证与回归」要点：
- 日志格式统一、字段完整
- trace_id 贯穿全链路、调用树可还原、断链检测
- 运行轨迹记录生命周期与状态迁移
- 错误日志含错误码/错误层/可重试性
- 敏感信息脱敏
- 异步写入、轮转保留清理
- 查询接口可用（trace_id/task_id/project_id/组合）
- 告警规则与去重冷却
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from app.observability import (
    fields as f,
    redact,
    tracing,
    structured,
    trajectory,
    metrics,
    storage,
    query,
    alerts,
)


# ──────────────────────────────────────────────────────────────────
# 一、字段规范
# ──────────────────────────────────────────────────────────────────
class TestFields:
    def test_log_fields_complete(self):
        assert "timestamp" in f.LOG_FIELDS
        assert "level" in f.LOG_FIELDS
        assert "module" in f.LOG_FIELDS
        assert "event" in f.LOG_FIELDS
        assert "trace_id" in f.LOG_FIELDS
        assert "span_id" in f.LOG_FIELDS
        assert "parent_span_id" in f.LOG_FIELDS
        assert "request_id" in f.LOG_FIELDS
        assert "task_id" in f.LOG_FIELDS
        assert "project_id" in f.LOG_FIELDS
        assert "chapter_id" in f.LOG_FIELDS
        assert "duration_ms" in f.LOG_FIELDS
        assert "status" in f.LOG_FIELDS
        assert "error_code" in f.LOG_FIELDS
        assert "error_layer" in f.LOG_FIELDS
        assert "retryable" in f.LOG_FIELDS
        assert "message" in f.LOG_FIELDS
        assert "detail" in f.LOG_FIELDS
        assert len(f.LOG_FIELDS) >= 18

    def test_module_error_codes(self):
        assert "PARSE_001" in f.ERROR_CODES
        assert "OUTLINE_002" in f.ERROR_CODES
        assert "CONTENT_003" in f.ERROR_CODES
        assert "EXPORT_004" in f.ERROR_CODES
        assert f.MODULE_PREFIXES["parse"] == "PARSE"
        assert f.MODULE_PREFIXES["outline"] == "OUTLINE"
        assert f.MODULE_PREFIXES["content"] == "CONTENT"
        assert f.MODULE_PREFIXES["export"] == "EXPORT"

    def test_trajectory_transitions(self):
        assert trajectory.validate_transition("running", "completed")
        assert trajectory.validate_transition("running", "failed")
        assert trajectory.validate_transition("created", "queued")
        assert not trajectory.validate_transition("completed", "running")
        assert not trajectory.validate_transition("failed", "running")
        assert "created" in f.TRAJECTORY_STATES
        assert "failed" in f.TRAJECTORY_STATES


# ──────────────────────────────────────────────────────────────────
# 二、脱敏
# ──────────────────────────────────────────────────────────────────
class TestRedact:
    def test_api_key(self):
        # 保留前 3 + 后 4（sk-****abcd 形式）
        assert redact.redact_text("sk-abcdefghijklmnop") == "sk-****mnop"

    def test_phone(self):
        assert redact.redact_text("联系电话13812345678") == "联系电话138****5678"

    def test_idcard(self):
        out = redact.redact_text("证件 110101199003071234")
        assert "****" in out and "1234" in out

    def test_token_and_jwt(self):
        assert redact.redact_text("Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.abc.def") == "Bearer ***"

    def test_dict_sensitive_keys(self):
        out = redact.redact({"api_key": "sk-1234567890", "password": "p", "model": "gpt-4o"})
        assert out["api_key"] == "***已配置***"
        assert out["password"] == "***已配置***"
        assert out["model"] == "gpt-4o"

    def test_nested_redact(self):
        out = redact.redact({"a": {"token": "abc", "list": [{"secret": "x", "ok": 1}]}})
        assert out["a"]["token"] == "***已配置***"
        assert out["a"]["list"][0]["secret"] == "***已配置***"


# ──────────────────────────────────────────────────────────────────
# 三、全链路追踪
# ──────────────────────────────────────────────────────────────────
class TestTracing:
    def setup_method(self):
        tracing.reset()
        tracing.clear_spans()

    def test_bind_and_snapshot(self):
        tid = tracing.bind_trace("trace-abc")
        assert tid == "trace-abc"
        snap = tracing.snapshot()
        assert snap["trace_id"] == "trace-abc"
        tracing.bind_snapshot(snap)
        assert tracing.current()["trace_id"] == "trace-abc"

    def test_span_nesting_tree(self):
        tid = tracing.bind_trace("t1")
        with tracing.span("root", module="api", event="api.request") as root:
            with tracing.span("ai", module="content", event="content.generate.start") as ai:
                with tracing.span("tool", module="db", event="db.write"):
                    pass
        tree = tracing.span_tree(tid)
        assert len(tree) == 1
        assert tree[0]["span_id"] == root.span_id
        assert tree[0]["children"][0]["span_id"] == ai.span_id
        assert tree[0]["children"][0]["children"][0]["depth"] == 2
        assert tracing.current()["trace_id"] == "t1"

    def test_span_chain_sorted_by_time(self):
        tid = tracing.bind_trace("t2")
        with tracing.span("a"):
            time.sleep(0.001)
            with tracing.span("b"):
                time.sleep(0.001)
        chain = tracing.span_chain(tid)
        assert len(chain) == 2
        assert chain[0]["depth"] <= chain[1]["depth"]

    def test_break_detection(self):
        tid = tracing.bind_trace("t3")
        tracing.bind_span("orphan", parent_span_id="no-such-parent")
        assert tracing.warn_if_broken(tid, "orphan") is True
        tracing.bind_span("root", parent_span_id="-")

# ──────────────────────────────────────────────────────────────────
# 四、结构化日志
# ──────────────────────────────────────────────────────────────────
class TestStructured:
    def test_build_record_fields(self):
        tracing.bind_trace("t4")
        rec = structured.build_record(
            "ERROR", "content.generate.fail", module="content", message="失败",
            status="failed", error_code="CONTENT_001", error_layer="llm",
            retryable=True, detail={"sections": 3}, request_id="req1",
            task_id="task1", project_id="9", chapter_id="c1",
        )
        assert rec["level"] == "ERROR"
        assert rec["module"] == "content"
        assert rec["event"] == "content.generate.fail"
        assert rec["trace_id"] == "t4"
        assert rec["error_code"] == "CONTENT_001"
        assert rec["error_layer"] == "llm"
        assert rec["retryable"] is True
        assert rec["request_id"] == "req1"
        assert rec["project_id"] == "9"
        assert rec["timestamp"]
        assert isinstance(rec["detail"], dict)

    def test_log_error_standard(self):
        tracing.bind_trace("t5")
        try:
            raise ValueError("boom")
        except Exception as e:
            structured.log_error(
                "parse.fail", module="parse", message="解析失败",
                exc=e, error_code="PARSE_001", error_layer="io", retryable=True,
                filename="tender.pdf",
            )
        rec = structured.build_record(
            "ERROR", "parse.fail", module="parse", message="解析失败",
            error_code="PARSE_001", error_layer="io", retryable=True,
            detail={"context": {"trace_id": "t5"}, "stack": "x"},
        )
        assert rec["error_code"] == "PARSE_001"
        assert rec["retryable"] is True

    def test_semantic_event_names(self):
        assert "parse.start" in f.EVENTS
        assert "content.generate.retry" in f.EVENTS
        assert "export.docx.fail" in f.EVENTS
        assert "outline.generate.retry" in f.EVENTS
        assert "chart.degrade" in f.EVENTS


# ──────────────────────────────────────────────────────────────────
# 五、运行轨迹
# ──────────────────────────────────────────────────────────────────
class TestTrajectory:
    def test_state_machine_lifecycle(self):
        tracker = trajectory.TaskTracker("task-a", task_type="outline", project_id="9")
        tracker.start()
        tracker.run()
        tracker.pause()
        tracker.resume()
        tracker.complete()
        hist = tracker.history()
        states = [h["to_status"] for h in hist]
        assert states == ["started", "running", "paused", "resumed", "completed"]
        assert hist[1]["from_status"] == "started"

    def test_illegal_transition_warns(self):
        rec = trajectory.record_state_change(
            task_id="task-b", task_type="content",
            from_status="completed", to_status="running",
        )
        assert rec["valid"] is False

    def test_record_and_get(self):
        trajectory.record_state_change(
            task_id="task-c", task_type="parse", from_status="created",
            to_status="queued", reason="入队", operator="api",
        )
        traj = trajectory.get_trajectory("task-c")
        assert any(x["to_status"] == "queued" for x in traj)


# ──────────────────────────────────────────────────────────────────
# 六、指标
# ──────────────────────────────────────────────────────────────────
class TestMetrics:
    def test_percentile(self):
        vals = list(range(1, 101))
        # 线性插值百分位
        assert metrics.percentile(vals, 50) == pytest.approx(50.5)
        assert metrics.percentile(vals, 95) == pytest.approx(95.05)
        assert metrics.percentile(vals, 99) == pytest.approx(99.01)

    def test_registry_summary(self):
        reg = metrics.MetricsRegistry(window_seconds=60)
        for i in range(10):
            reg.record("ai.call.duration", float(i + 1))
        s = reg.summary("ai.call.duration")
        assert s["count"] == 10
        assert 0 < s["p50"] <= 10
        assert s["p95"] >= s["p50"]

    def test_snapshot_structure(self):
        reg = metrics.MetricsRegistry(window_seconds=60)
        reg.record("x", 1.0)
        reg.record_error("CONTENT_001")
        reg.record_event("call")
        snap = reg.snapshot()
        assert "error_top10" in snap
        assert snap["error_top10"][0]["code"] == "CONTENT_001"


# ──────────────────────────────────────────────────────────────────
# 七、存储（异步写入 + 轮转 + 保留清理）
# ──────────────────────────────────────────────────────────────────
class TestStorage:
    def test_async_write_and_flush(self, tmp_path):
        writer = storage.AsyncLogWriter(
            enabled=True, buffer_size=1000, flush_interval=0.05, rate_limit=100000,
        )
        try:
            os.environ["OBS_LOG_DIR"] = str(tmp_path)
            rec = structured.build_record("INFO", "parse.start", module="parse", message="x")
            assert writer.write(rec) is True
            writer.flush(timeout=1.0)
            files = list(tmp_path.glob("obs-INFO-*.jsonl"))
            assert files, "应生成按级别日轮转 JSONL 文件"
            lines = files[0].read_text(encoding="utf-8").strip().splitlines()
            assert lines and json.loads(lines[0])["event"] == "parse.start"
        finally:
            writer.stop()
            os.environ.pop("OBS_LOG_DIR", None)

    def test_rate_limit_drop(self):
        writer = storage.AsyncLogWriter(enabled=True, rate_limit=1.0, buffer_size=100)
        try:
            accepted = sum(1 for _ in range(50) if writer.write({}))
            assert accepted < 50
        finally:
            writer.stop()

    def test_cleanup_retention(self, tmp_path):
        os.environ["OBS_LOG_DIR"] = str(tmp_path)
        try:
            old = tmp_path / "obs-DEBUG-2020-01-01.jsonl"
            old.write_text("x\n", encoding="utf-8")
            mtime = time.time() - 100 * 86400
            os.utime(old, (mtime, mtime))
            storage.cleanup_retention()
            assert not old.exists()
        finally:
            os.environ.pop("OBS_LOG_DIR", None)

    def test_stats_shape(self):
        s = storage.get_writer().stats()
        assert "written" in s and "dropped" in s and "queue_size" in s


# ──────────────────────────────────────────────────────────────────
# 八、查询服务
# ──────────────────────────────────────────────────────────────────
class TestQuery:
    def test_persist_and_query_by_trace(self):
        rec = structured.build_record(
            "INFO", "outline.generate.end", module="outline", message="完成",
            trace_id="tq-1", task_id="task-q", project_id="99",
            status="ok", detail={"chapters": 5},
        )
        assert query.persist_log(rec) is not None
        res = query.query_logs(trace_id="tq-1")
        assert res["total"] >= 1
        assert any(x["event"] == "outline.generate.end" for x in res["items"])

    def test_query_combine_filters(self):
        rec = structured.build_record(
            "WARN", "retry", module="content", message="重试",
            trace_id="tq-2", task_id="task-q2", project_id="88",
            status="retry", error_code="CONTENT_001",
        )
        query.persist_log(rec)
        res = query.query_logs(trace_id="tq-2", level="WARN", error_code="CONTENT_001")
        assert res["total"] >= 1

    def test_trace_chain(self):
        rec = structured.build_record("INFO", "parse.start", module="parse", message="s",
                                       trace_id="tq-3", span_id="s1", parent_span_id="-")
        query.persist_log(rec)
        chain = query.get_trace_chain("tq-3")
        assert chain["trace_id"] == "tq-3"
        assert chain["log_count"] >= 1

    def test_export_formats(self):
        items = [{"timestamp": "t", "level": "INFO", "message": "m"}]
        assert "message" in query.export_csv(items)
        assert json.loads(query.export_json(items))[0]["message"] == "m"


# ──────────────────────────────────────────────────────────────────
# 九、告警（去重 + 冷却）
# ──────────────────────────────────────────────────────────────────
class TestAlerts:
    def test_fire_and_dedup_cooldown(self):
        engine = alerts.AlertEngine(cooldown_seconds=60)
        assert engine.fire("error_rate", 60.0, 50.0) is True
        assert engine.fire("error_rate", 70.0, 50.0) is False  # 冷却期去重
        assert engine.fire("p99_latency", 2000.0, 1000.0) is True

    def test_history(self):
        engine = alerts.AlertEngine(cooldown_seconds=0)
        engine.fire("error_rate", 60.0, 50.0)
        engine.fire("queue_backlog", 1000.0, 100.0)
        assert len(engine.history()) == 2
        assert engine.history()[0]["type"] in ("error_rate", "queue_backlog")


# ──────────────────────────────────────────────────────────────────
# 十、API 集成（查询接口 + 前端看板）
# ──────────────────────────────────────────────────────────────────
class TestAPI:
    def _client(self):
        from fastapi.testclient import TestClient
        from app.main import app

        return TestClient(app)

    def test_logs_query_api(self):
        rec = structured.build_record(
            "INFO", "export.docx.start", module="export", message="导出",
            trace_id="tapi-1", task_id="task-api", project_id="77",
        )
        query.persist_log(rec)
        client = self._client()
        r = client.get("/api/v1/observability/logs", params={"trace_id": "tapi-1"})
        assert r.status_code == 200
        data = r.json()
        assert data["total"] >= 1
        assert any(x["event"] == "export.docx.start" for x in data["items"])

    def test_trace_chain_api(self):
        rec = structured.build_record(
            "INFO", "parse.end", module="parse", message="解析完成",
            trace_id="tapi-2", span_id="sp1", parent_span_id="-",
        )
        query.persist_log(rec)
        client = self._client()
        r = client.get("/api/v1/observability/trace/tapi-2")
        assert r.status_code == 200
        data = r.json()
        assert data["trace_id"] == "tapi-2"
        assert data["log_count"] >= 1

    def test_trajectory_api(self):
        trajectory.record_state_change(
            task_id="task-api-2", task_type="outline",
            from_status="created", to_status="started", reason="start", operator="api",
        )
        client = self._client()
        r = client.get("/api/v1/observability/trajectory/task-api-2")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] >= 1
        assert data["items"][0]["to_status"] == "started"

    def test_metrics_api(self):
        metrics.record_metric("test.duration", 5.0)
        client = self._client()
        r = client.get("/api/v1/observability/metrics")
        assert r.status_code == 200
        data = r.json()
        assert "storage" in data
        assert "error_top10" in data

    def test_dashboard_html(self):
        client = self._client()
        r = client.get("/api/v1/observability/dashboard")
        assert r.status_code == 200
        assert "可观测性看板" in r.text
        assert "loadLogs" in r.text

    def test_export_csv_api(self):
        client = self._client()
        r = client.get("/api/v1/observability/logs/export", params={"fmt": "csv", "limit": 5})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
