"""图表生成与渲染模块 · 遗留项加固回归测试（2026-10-14）。

覆盖本轮对两项遗留问题的修复与加固：

1. BUG-CHART-TL-FALLBACK：纯 Mermaid `timeline` 语法围栏此前**无 PIL 兜底**——
   `render_mermaid_to_bytes` 的 timeline 分支只在载荷为 `{` 开头 JSON 时渲染，
   HTTP Mermaid Service 不可用时纯 `timeline` 代码被静默丢图。新增
   `_parse_mermaid_timeline` 把语法解析为与数据型 timeline 同构的字典后交
   `_render_timeline_image_v2`；无事件时返回 None（宁缺毋假，绝不伪造里程碑）。

2. HARDEN-PROMPT-DRIFT：`_check_hardcode_drift` 旧实现只要 `DB content != 硬编码`
   就告警，无法区分「硬编码更新未同步的过时默认」与「管理员有意自定义」，后者
   每次启动误报。加固后仅对**未被用户编辑**（content == 本行 default_content）
   且与硬编码不一致的行告警；查询 default_content 失败时保守回退旧口径。
"""

from __future__ import annotations

import contextlib
import json
import logging

import pytest

from app.services.ai.mermaid_renderer import render_mermaid_to_bytes
from app.services.ai.mermaid_timeline import _parse_mermaid_timeline


PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

TL_MERMAID = (
    "timeline\n"
    "    title 项目里程碑\n"
    "    section 第一阶段\n"
    "        2024 : 完成设计 : 通过评审\n"
    "    section 第二阶段\n"
    "        2025 : 完成开发\n"
)


# ──────────────────────────────────────────────────────────────────────
# 1. _parse_mermaid_timeline：结构解析正确性与「不伪造」原则
# ──────────────────────────────────────────────────────────────────────


class TestParseMermaidTimeline:
    def test_parses_title_sections_and_multi_events(self):
        data = _parse_mermaid_timeline(TL_MERMAID)
        assert data is not None
        assert data["type"] == "timeline"
        assert data["title"] == "项目里程碑"
        ms = data["milestones"]
        # 同一 period 多事件 → 展开为多条里程碑
        assert len(ms) == 3
        assert {m["name"] for m in ms} == {"完成设计", "通过评审", "完成开发"}
        # date/group 归属正确
        design = next(m for m in ms if m["name"] == "完成设计")
        assert design["date"] == "2024"
        assert design["group"] == "第一阶段"
        dev = next(m for m in ms if m["name"] == "完成开发")
        assert dev["date"] == "2025"
        assert dev["group"] == "第二阶段"

    def test_default_title_when_absent(self):
        data = _parse_mermaid_timeline("timeline\n    section S\n        2020 : 事件")
        assert data is not None
        assert data["title"] == "关键里程碑时间线"

    def test_empty_events_returns_none_no_fabrication(self):
        # 仅有标题、无任何事件行 → 绝不伪造里程碑
        assert _parse_mermaid_timeline("timeline\n    title 只有标题") is None

    def test_non_timeline_returns_none(self):
        assert _parse_mermaid_timeline("graph TD\n    A-->B") is None

    def test_invalid_input_returns_none(self):
        assert _parse_mermaid_timeline("") is None
        assert _parse_mermaid_timeline("   ") is None
        assert _parse_mermaid_timeline(None) is None  # type: ignore[arg-type]

    def test_extended_double_colon_kept_as_single_event(self):
        # 事件名内的 `::`（Mermaid 备注分隔）应保留为一条，不被误拆
        data = _parse_mermaid_timeline(
            "timeline\n    2024 : 主体封顶 :: 提前两周完成"
        )
        assert data is not None
        assert len(data["milestones"]) == 1
        assert data["milestones"][0]["name"] == "主体封顶 :: 提前两周完成"
        assert data["milestones"][0]["date"] == "2024"

    def test_comments_and_blank_lines_ignored(self):
        data = _parse_mermaid_timeline(
            "timeline\n    %% 注释\n\n    title T\n        2030 : 竣工"
        )
        assert data is not None
        assert [m["name"] for m in data["milestones"]] == ["竣工"]


# ──────────────────────────────────────────────────────────────────────
# 2. 端到端：纯 Mermaid timeline 语法在 HTTP 不可用时产出 PNG
# ──────────────────────────────────────────────────────────────────────


class TestTimelineMermaidPILFallback:
    def test_mermaid_timeline_renders_png_without_http(self):
        # 复刻生产围栏包装：{"type":"timeline","code":<mermaid>,"title":...}
        payload = json.dumps(
            {"type": "timeline", "code": TL_MERMAID, "title": "项目里程碑"},
            ensure_ascii=False,
        )
        img = render_mermaid_to_bytes(payload, "timeline", skip_http=True)
        assert img is not None, "纯 Mermaid timeline 语法应有 PIL 兜底渲染"
        data = img.getvalue()
        assert data[:8] == PNG_MAGIC
        assert len(data) > 1000

    def test_empty_timeline_still_skipped(self):
        # 无事件：兜底不得伪造里程碑，应维持「宁缺毋假」跳过
        payload = json.dumps(
            {"type": "timeline", "code": "timeline\n    title 空", "title": "空"},
            ensure_ascii=False,
        )
        assert render_mermaid_to_bytes(payload, "timeline", skip_http=True) is None

    def test_json_payload_path_unchanged(self):
        # 数据型 JSON 载荷仍走原分支（回归保护：兜底不应干扰既有路径）
        payload = json.dumps(
            {
                "type": "timeline",
                "title": "里程碑",
                "milestones": [
                    {"name": "开工", "date": "第1天"},
                    {"name": "封顶", "date": "第90天"},
                ],
            },
            ensure_ascii=False,
        )
        img = render_mermaid_to_bytes(payload, "timeline", skip_http=True)
        assert img is not None
        assert img.getvalue()[:8] == PNG_MAGIC


# ──────────────────────────────────────────────────────────────────────
# 3. _check_hardcode_drift：区分「过时默认」与「用户定制」，不误报
# ──────────────────────────────────────────────────────────────────────


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, _sql):
        return self

    def fetchall(self):
        return self._rows


@contextlib.contextmanager
def _fake_get_db(rows=None, raise_err=False):
    if raise_err:
        raise RuntimeError("no such column: default_content")
    yield _FakeConn(rows or [])


DRIFT_KEY = "CHART_BATCH_ARRANGE_SYSTEM"


class TestHardcodeDriftFalsePositiveHarden:
    @pytest.fixture()
    def _prep(self, monkeypatch):
        import app.db as db_mod
        from app.db import get_db  # noqa: F401  (确保 app.db 可导入)
        from app.services.ai.prompts import _cache
        from app.services.ai.prompts._registry import _ALL_PROMPTS

        assert DRIFT_KEY in _ALL_PROMPTS
        return monkeypatch, db_mod, _cache, _ALL_PROMPTS

    def _run_drift(self, monkeypatch, db_mod, _cache, caplog, rows, *, db_content):
        monkeypatch.setattr(_cache, "_prompt_cache", {DRIFT_KEY: db_content})
        monkeypatch.setattr(
            db_mod,
            "get_db",
            lambda *a, **k: _fake_get_db(rows=rows),
        )
        with caplog.at_level(logging.WARNING, logger="app.services.ai.prompts._cache"):
            _cache._check_hardcode_drift()
        return any("存在差异" in r.message for r in caplog.records)

    def test_stale_default_warns(self, _prep, caplog):
        monkeypatch, db_mod, _cache, prompts = _prep
        # DB 存的仍是旧默认（content == default_content）且与硬编码不同 → 真实漂移
        stale = "【旧版默认】原始提示词内容"
        rows = [{"key": DRIFT_KEY, "default_content": stale}]
        warned = self._run_drift(
            monkeypatch, db_mod, _cache, caplog, rows, db_content=stale
        )
        assert warned, "未编辑的过时默认应被告警"

    def test_user_customization_silent(self, _prep, caplog):
        monkeypatch, db_mod, _cache, prompts = _prep
        # 用户有意自定义：content != 本行 default_content → 非漂移，静默
        rows = [{"key": DRIFT_KEY, "default_content": "【系统默认】"}]
        warned = self._run_drift(
            monkeypatch,
            db_mod,
            _cache,
            caplog,
            rows,
            db_content="【用户精心定制的提示词】",
        )
        assert not warned, "用户自定义不应误报漂移"

    def test_query_failure_falls_back_to_warn(self, _prep, caplog):
        monkeypatch, db_mod, _cache, prompts = _prep
        # 无法读取 default_content（旧库缺列）→ 保守回退到旧口径：不同即告警
        monkeypatch.setattr(_cache, "_prompt_cache", {DRIFT_KEY: "任意不一致内容"})
        monkeypatch.setattr(
            db_mod, "get_db", lambda *a, **k: _fake_get_db(raise_err=True)
        )
        with caplog.at_level(logging.WARNING, logger="app.services.ai.prompts._cache"):
            _cache._check_hardcode_drift()
        assert any("存在差异" in r.message for r in caplog.records), (
            "查询失败时应保守告警，宁多报不漏报真实漂移"
        )

    def test_matching_hardcoded_no_warn(self, _prep, caplog):
        from app.services.ai.prompts import _cache
        from app.services.ai.prompts._registry import _ALL_PROMPTS, clean_prompt_text

        monkeypatch, db_mod, _cache, prompts = _prep
        exact = clean_prompt_text(_ALL_PROMPTS[DRIFT_KEY]["content"])
        rows = [{"key": DRIFT_KEY, "default_content": exact}]
        warned = self._run_drift(
            monkeypatch, db_mod, _cache, caplog, rows, db_content=exact
        )
        assert not warned, "与硬编码一致不应告警"
