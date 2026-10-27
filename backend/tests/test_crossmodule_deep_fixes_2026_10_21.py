# -*- coding: utf-8 -*-
"""六模块跨模块深度探索与 BUG 修复 —— 本轮回归测试（2026-10-21）。

覆盖本轮实证确认并修复/增强的 4 项（全部先复现后修复，测试钉住修复后行为）：

- BUG-1（正文生成·R10 全文缩写）：``run_total_word_adjustments`` 的收敛
  判定写反（``total >= maximum_words`` 即 break），多轮缩写实际只能跑 1 轮。
  回归见 ``test_word_adjust_pipeline_2026_10_12.py``（本轮已补两条用例）。

- BUG-2（导出·HTML 空壳文件）：``/export/html`` 缺「有章节但所有章节正文
  均为空」判据（DOCX/SSE 分支 2026-09-18 已收口，HTML 2026-10-13 只补了
  total_sections==0 与 0 字节两道），用户可下载只有封面的空壳 HTML。

- BUG-3（正文生成·SSE 重试轮次）：重试循环内第二条 ``retry_round`` 事件的
  ``round`` 字段与首条重复（均报 1），前端「第 1/2 轮」展示两次且与文案
  「继续 Round 2/2」矛盾。

- GAP-1（审核×导出打通·一致性闸门）：``check_document`` 新增
  ``block_on_high_conflicts`` 参数与 ``high_conflicts_open`` 返回字段；
  DOCX/HTML/SSE 三个导出入口接线；默认关闭保持历史行为（向后兼容）。
"""
from __future__ import annotations

import asyncio
import json
import uuid

from fastapi import HTTPException
import pytest

from app.db import get_db, init_db
from app.models import ExportConfig
from app.routers.export import export_html
from app.security.auth import User


OWNER = 4399


# ============================================================================
# 公共夹具
# ============================================================================


def _new_project(name: str) -> int:
    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO projects (name, industry, created_by) VALUES (?, ?, ?)",
            (name, "通用", OWNER),
        )
        return conn.execute("SELECT id FROM projects ORDER BY id DESC LIMIT 1").fetchone()["id"]


def _seed_section(project_id: int, *, title: str, content: str, sort_order: int = 0, level: int = 1) -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO sections (project_id, title, level, sort_order, content, status, section_type) "
            "VALUES (?, ?, ?, ?, ?, 'completed', 'normal')",
            (project_id, title, level, sort_order, content),
        )
        return cur.lastrowid


def _seed_conflict(
    project_id: int,
    *,
    fact_name: str = "工期",
    severity: str = "high",
    resolved: int = 0,
    conflict_id: str | None = None,
) -> str:
    """向 consistency_conflicts 插入一条冲突行，返回 conflict_id。"""
    cid = conflict_id or f"R-{uuid.uuid4().hex[:12]}"
    with get_db() as conn:
        conn.execute(
            "INSERT INTO consistency_conflicts "
            "(project_id, conflict_id, fact_name, values_json, sources_json, severity, resolved) "
            "VALUES (?, ?, ?, '[]', '[]', ?, ?)",
            (project_id, cid, fact_name, severity, resolved),
        )
    return cid


# ============================================================================
# BUG-2 —— HTML 导出：全空正文判据
# ============================================================================


class TestHtmlExportAllEmptyContent:
    """BUG-2：有章节但所有章节正文均为空时，HTML 导出必须 400。

    build_html 只输出标题/封面（产物非 0 字节，0 字节检查拦不住），
    修复前会导出一份没有正文的空壳 HTML。
    """

    def test_sections_but_all_empty_returns_400(self):
        pid = _new_project(f"HTML全空-{uuid.uuid4().hex[:6]}")
        _seed_section(pid, title="第一章", content="")
        _seed_section(pid, title="1.1 子节", content="", sort_order=1)
        with pytest.raises(HTTPException) as ei:
            asyncio.run(export_html(pid, ExportConfig(), identity=User(OWNER, "owner", "user")))
        assert ei.value.status_code == 400
        assert "所有章节内容均为空" in str(ei.value.detail)

    def test_partial_content_still_exports(self):
        """有任一章节有正文 → 不得被误判为全空（与 DOCX 分支同口径）。"""
        pid = _new_project(f"HTML部分空-{uuid.uuid4().hex[:6]}")
        _seed_section(pid, title="第一章", content="")
        _seed_section(pid, title="1.1 有内容", content="本章正文内容。" * 20, sort_order=1)
        resp = asyncio.run(export_html(pid, ExportConfig(), identity=User(OWNER, "owner", "user")))
        assert resp.media_type == "text/html; charset=utf-8"


# ============================================================================
# GAP-1 —— 一致性校核 × 导出闸门
# ============================================================================


class TestConsistencyExportGate:
    """审核结果接入导出：默认关闭（向后兼容），开启后 high 冲突阻断导出。"""

    def test_check_document_default_off_is_backward_compatible(self):
        """默认（不传 block_on_high_conflicts）：即使有 high 冲突也不统计、
        不加 error、passed 不受影响 —— 历史行为逐字不变。"""
        from app.services.export import check_document

        pid = _new_project(f"闸门默认-{uuid.uuid4().hex[:6]}")
        _seed_section(pid, title="第一章", content="本章正文内容。" * 20)
        _seed_conflict(pid, fact_name="工期", severity="high", resolved=0)

        r = check_document(pid)
        assert r.high_conflicts_open == 0
        assert not any("一致性冲突" in e for e in r.errors)

    def test_check_document_counts_unresolved_high(self):
        from app.services.export import check_document

        pid = _new_project(f"闸门计数-{uuid.uuid4().hex[:6]}")
        _seed_section(pid, title="第一章", content="本章正文内容。" * 20)
        _seed_conflict(pid, fact_name="工期", severity="high", resolved=0)
        _seed_conflict(pid, fact_name="合同金额", severity="high", resolved=0)
        # 已解决 / 非 high → 不计入
        _seed_conflict(pid, fact_name="质量标准", severity="high", resolved=1)
        _seed_conflict(pid, fact_name="项目经理", severity="medium", resolved=0)

        r = check_document(pid, block_on_high_conflicts=True)
        assert r.high_conflicts_open == 2
        assert any("2 个未解决的高严重度一致性冲突" in e for e in r.errors)
        assert r.passed is False

    def test_check_document_error_message_lists_fact_names(self):
        from app.services.export import check_document

        pid = _new_project(f"闸门文案-{uuid.uuid4().hex[:6]}")
        _seed_section(pid, title="第一章", content="本章正文内容。" * 20)
        _seed_conflict(pid, fact_name="工期", severity="high", resolved=0)

        r = check_document(pid, block_on_high_conflicts=True)
        joined = "；".join(r.errors)
        assert "工期" in joined
        assert "一致性校核" in joined

    def test_no_conflicts_gate_passes(self):
        from app.services.export import check_document

        pid = _new_project(f"闸门无冲突-{uuid.uuid4().hex[:6]}")
        _seed_section(pid, title="第一章", content="本章正文内容。" * 20)

        r = check_document(pid, block_on_high_conflicts=True)
        assert r.high_conflicts_open == 0
        assert r.passed is True

    def test_route_blocks_when_gate_on_and_high_conflict(self):
        pid = _new_project(f"闸门阻断-{uuid.uuid4().hex[:6]}")
        _seed_section(pid, title="第一章", content="本章正文内容。" * 20)
        _seed_conflict(pid, fact_name="工期", severity="high", resolved=0)
        with pytest.raises(HTTPException) as ei:
            asyncio.run(
                export_html(
                    pid,
                    ExportConfig(block_on_high_conflicts=True),
                    identity=User(OWNER, "owner", "user"),
                )
            )
        assert ei.value.status_code == 400
        assert "一致性冲突" in str(ei.value.detail)

    def test_route_allows_when_gate_off(self):
        """同项目同冲突，闸门关闭 → 正常导出（向后兼容的关键证据）。"""
        pid = _new_project(f"闸门放行-{uuid.uuid4().hex[:6]}")
        _seed_section(pid, title="第一章", content="本章正文内容。" * 20)
        _seed_conflict(pid, fact_name="工期", severity="high", resolved=0)
        resp = asyncio.run(
            export_html(
                pid,
                ExportConfig(block_on_high_conflicts=False),
                identity=User(OWNER, "owner", "user"),
            )
        )
        assert resp.media_type == "text/html; charset=utf-8"


# ============================================================================
# BUG-3 —— SSE retry_round 轮次编号
# ============================================================================


class _StubGM2:
    """generation_manager 桩（只保留本链路触及的面）。"""

    _lock = asyncio.Lock()
    _persist_locks: dict = {}
    _tasks: dict = {}

    async def create_task(self, *a, **kw):
        return "gen-test"

    async def update_section_progress(self, *a, **kw):
        return None

    async def is_paused(self, *a, **kw):
        return False

    async def finalize_task(self, *a, **kw):
        return None

    def _persist_task(self, *a, **kw):
        return None


class _StubCache2:
    @staticmethod
    def invalidate_sections(*a, **kw):
        return None


class _WCState2:
    class effective_options:
        section_words = 300
        strict_section_words = False

    def gate_error(self):
        return None


def _run_retry_generation_two_rounds(monkeypatch) -> list[dict]:
    """驱动真实 generate_content_impl：主生成 2 章全败 → 重试 Round 1 仍全败
    → 重试 Round 2 全成。

    返回全部 SSE 事件（已解析 JSON）。用于验证 BUG-3：循环内第二条
    retry_round 的 round 必须为 2（即将开始的轮次），与 message 文案
    「继续 Round 2/2」一致；修复前恒为 1，与首条事件重复。
    """
    from app.routers.sse import sse_content as sc
    from app.services.ai import workflows_sections as ws

    leaves = [
        {"id": 201, "title": "施工部署", "level": 3, "target_words": 300,
         "description": "", "path": "1", "parent_id": None, "sort_order": 1},
        {"id": 202, "title": "质量保证", "level": 3, "target_words": 300,
         "description": "", "path": "2", "parent_id": None, "sort_order": 2},
    ]
    sections_by_id = {s["id"]: dict(s) for s in leaves}
    call_state = {"n": 0}

    async def _fake_run_sections(provider, sections, **kwargs):
        call_state["n"] += 1
        cb = kwargs.get("section_complete_callback")
        results = []
        for s in sections:
            if call_state["n"] <= 2:
                # 第 1 次（主生成）与第 2 次（重试 Round 1）均失败
                res = {"id": s["id"], "title": s.get("title", ""), "status": "failed",
                       "content": "", "error": "模拟失败", "quality": None}
            else:
                res = {"id": s["id"], "title": s.get("title", ""), "status": "completed",
                       "content": "正文内容" * 30, "error": "", "quality": None}
            results.append(res)
            if cb is not None:
                await cb(res)
        return results

    async def _fake_provider(*a, **kw):
        return object(), None, 2

    async def _fake_project_data(project_id):
        return {
            "summary": "项目摘要", "scoring_points": "[]", "industry_profile": "",
            "tender_context": "", "construction_method": "",
            "sections_by_id": sections_by_id, "children_map": {}, "all_rows": [],
            "global_facts_items": None, "facts_policy": "fabricate", "industry": "",
        }

    async def _noop(*a, **kw):
        return True

    async def _noop_condense(results, *a, **kw):
        return {"condensed_count": 0, "fail_count": 0}

    monkeypatch.setattr(sc, "_try_get_provider", _fake_provider)
    monkeypatch.setattr(sc, "CacheService", _StubCache2)
    monkeypatch.setattr(sc, "_fetch_project_data_async", _fake_project_data)
    monkeypatch.setattr(sc, "_build_leaf_dicts", lambda **kw: [dict(s) for s in leaves])
    monkeypatch.setattr(sc, "build_tree", lambda *a, **kw: [])
    monkeypatch.setattr(sc, "update_step", lambda *a, **kw: None)
    monkeypatch.setattr(sc, "generation_manager", _StubGM2())
    monkeypatch.setattr(sc, "_run_sections", _fake_run_sections)
    monkeypatch.setattr(sc, "_save_section_to_db", _noop)
    monkeypatch.setattr(sc, "_auto_generate_charts_for_section", _noop)
    monkeypatch.setattr(sc, "_OVERALL_PROGRESS_THROTTLE_SECS", 0.0)

    import app.services.word_control_runtime as wcr
    monkeypatch.setattr(wcr, "load_project_word_control", lambda *a, **kw: _WCState2())
    monkeypatch.setattr(wcr, "build_section_override", lambda *a, **kw: {})

    import app.services.sections as sec_svc
    monkeypatch.setattr(sec_svc, "invalidate_content_caches", lambda *a, **kw: None)
    monkeypatch.setattr(ws, "condense_overlong_results", _noop_condense)

    async def _drive():
        resp = await sc.generate_content_impl(
            project_id=998878,
            writing_length="standard",
            regenerate_ids="",
            root_section_id=None,
            regenerate_missing=False,
            use_orchestration=False,
            level2_default=3500,
            use_word_sampling=False,
            use_fallback_expand=False,
            use_consistency_audit=False,
            concurrency=2,
            orch_max_ai_images=6,
            orch_table_ratio="moderate",
            orch_prefer_mermaid=True,
            orch_section_per_segment=3,
            use_word_adjust=False,
        )
        events = []
        async for chunk in resp.body_iterator:
            line = chunk.strip()
            if line.startswith("data: "):
                try:
                    events.append(json.loads(line[6:]))
                except json.JSONDecodeError:
                    pass
        return events

    return asyncio.run(_drive())


class TestSSERetryRoundNumbering:
    """BUG-3：retry_round 事件轮次编号不得重复，须与 message 文案一致。"""

    def test_second_retry_event_reports_next_round(self, monkeypatch):
        events = _run_retry_generation_two_rounds(monkeypatch)
        rounds = [e for e in events if e.get("status") == "retry_round"]
        assert len(rounds) >= 2, f"应至少两条 retry_round 事件，实际 {len(rounds)}: {rounds}"
        # 首条宣告 Round 1 开始；第二条宣告 Round 2 即将开始（修复前两条均 round=1）
        assert rounds[0]["round"] == 1
        assert rounds[1]["round"] == 2
        assert rounds[1]["total_rounds"] == 2
        # 文案与编号一致：「继续 Round 2/2」
        assert "继续 Round 2/2" in rounds[1]["message"]
        # 编号序列严格递增（前端「第 X/2 轮」不得重复展示）
        seq = [e["round"] for e in rounds]
        assert seq == sorted(set(seq)), f"轮次编号重复或乱序: {seq}"
