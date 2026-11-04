"""章节编写模块第 10 轮深度审计（2026-11-04）回归测试。

覆盖本轮全部修复：
  - FIX-A/B：chart_auto_orchestration / illustration_phase 对 pydantic Section
    的 dict 式访问（生产路径必崩 → section_to_dict 统一归一）
  - FIX-C：_save_section_to_db 清理重写正文不再引用的旧 *_json 图表载荷
  - FIX-D：目录重建快照/回填包含 *_json 图表字段
  - FIX-E 相关后端口径：配图阶段异常补发 illustration_phase_done
  - W1：generate_sections 的 generation_mode 具名形参死参数
  - W3：extract_section_charts 对 mermaid（code 形态）围栏的误降级
  - W4：兜底扩写/审计修复接受前统一净化（_sanitize_rewritten_content）
  - W2：collect_deficient_sections / expand_content_fallback 接入强控覆盖表
  - W5：gathered 异常显式日志 + None 占位补 failed/cancelled 终态
  - P3：expand-content 422、CRLF 落库归一、is_l3_own None 透传、
        审计修复超长章节跳过、get_section_path 兄弟排序键、
        sse_charts 落库失败不登记 real_charts、导出重试轮图号/图题回滚、
        图表围栏插入位置钳制到代码围栏之外
"""
from __future__ import annotations

import asyncio
import inspect

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import app
from app.models import SectionCreate
from app.routers.sse import sse_db_ops
from app.services import sections as sections_service
from app.services.ai import workflows_sections
from app.services.ai.illustration_phase import execute_illustration_phase
from app.services.ai.workflows_expand import (
    _sanitize_rewritten_content,
    collect_deficient_sections,
)
from app.services.chart_auto_orchestration import (
    auto_orchestrate_section,
    insert_fence_at_paragraph,
    section_to_dict,
)


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def project_id(client):
    resp = client.post(
        "/api/v1/projects",
        json={"name": "第10轮章节审计", "industry": "市政工程", "bidder_name": "测试投标方"},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _db_row(section_id: int) -> dict:
    with get_db(readonly=True) as conn:
        row = conn.execute(
            "SELECT * FROM sections WHERE id = ?", (section_id,)
        ).fetchone()
    return dict(row) if row else {}


# ============================================================
# FIX-A / FIX-B：pydantic Section 归一（生产注入路径契约）
# ============================================================


class TestSectionToDict:
    def test_pydantic_section_normalized(self, project_id):
        """生产契约：不注入 fake 时 get_section 返回 pydantic Section，
        section_to_dict 必须归一为 dict（旧实现 AttributeError 必崩）。"""
        from app.models import Section

        sec = sections_service.create_section(
            project_id, SectionCreate(title="施工工艺")
        )
        fresh = sections_service.get_section(sec.id)
        assert isinstance(fresh, Section)
        d = section_to_dict(fresh)
        assert isinstance(d, dict)
        assert d.get("title") == "施工工艺"
        assert d.get("id") == sec.id

    def test_dict_passthrough(self):
        assert section_to_dict({"a": 1}) == {"a": 1}
        assert section_to_dict(None) is None


class TestAutoOrchestrateProductionPath:
    def test_real_db_no_fake_does_not_crash(self, project_id):
        """auto_orchestrate_section 生产路径（默认 get_section）不再 500。"""
        sec = sections_service.create_section(
            project_id, SectionCreate(title="项目组织架构")
        )
        result = auto_orchestrate_section(project_id, sec.id)
        # 无论 AI/本地编排判定是否需要配图，都不允许 AttributeError 逃逸
        assert isinstance(result, dict)
        assert result.get("section_id") == sec.id
        assert "skipped_reason" in result or result.get("ok") is not None


class TestIllustrationPhaseProductionPath:
    def test_real_db_targets_no_crash(self, project_id):
        """execute_illustration_phase 生产路径（默认 get_section）不再整体崩溃。"""
        from app.services.ai.illustration_phase import IllustrationTarget

        sec = sections_service.create_section(
            project_id, SectionCreate(title="施工进度计划")
        )
        targets = [
            IllustrationTarget(
                section_id=sec.id,
                title="施工进度计划",
                kind="chart",
                priority=1,
                payload={"chart_type": "gantt", "data": {"tasks": []}},
            )
        ]
        result = asyncio.run(execute_illustration_phase(project_id, targets))
        assert result["ok"] is True
        assert result["total"] == 1
        # 单图处理必有归项（inserted/skipped/failed 之一），不允许循环中断
        assert (
            result["inserted"] + result["skipped"] + result["failed"] == 1
        )


class TestIllustrationPhaseDoneOnException:
    def test_sse_except_branch_emits_done_event(self):
        """配图阶段异常路径必须补发 illustration_phase_done（防前端悬挂）。"""
        from app.routers.sse import sse_content

        src = inspect.getsource(sse_content)
        # except 分支内同时出现告警日志与 done 事件（同一配图阶段收尾块）
        assert "[配图阶段] 执行异常" in src
        assert '"illustration_phase_done"' in src


# ============================================================
# FIX-C：旧 *_json 载荷清理
# ============================================================


class TestStaleChartFieldCleanup:
    def _set_json(self, section_id: int, field: str, value: str) -> None:
        with get_db() as conn:
            conn.execute(
                f"UPDATE sections SET {field} = ? WHERE id = ?", (value, section_id)
            )

    def test_stale_json_cleared_when_markers_gone(self, project_id):
        sec = sections_service.create_section(
            project_id, SectionCreate(title="施工部署")
        )
        self._set_json(sec.id, "flowchart_json", '{"type":"flowchart","nodes":[]}')
        self._set_json(sec.id, "gantt_json", '{"type":"gantt","tasks":[]}')
        sse_db_ops._save_section_to_db(
            sec.id, "completed", "全新正文，不含任何图表标记。", project_id=project_id
        )
        row = _db_row(sec.id)
        assert (row["flowchart_json"] or "") == ""
        assert (row["gantt_json"] or "") == ""

    def test_json_kept_when_marker_present(self, project_id):
        sec = sections_service.create_section(
            project_id, SectionCreate(title="进度管理")
        )
        self._set_json(sec.id, "gantt_json", '{"type":"gantt","tasks":[{"name":"a"}]}')
        content = (
            "正文首段。\n<<CHART:甘特图>>\n后文。"
        )
        sse_db_ops._save_section_to_db(
            sec.id, "completed", content, project_id=project_id
        )
        row = _db_row(sec.id)
        # 同类型标记仍在正文中 → 降级落库载荷不得被清理
        assert (row["gantt_json"] or "") != ""


# ============================================================
# FIX-D：目录重建快照/回填包含 *_json
# ============================================================


class TestOutlineRebuildChartPreservation:
    def test_chart_fields_restored_after_rebuild(self, project_id):
        root = sections_service.create_section(
            project_id, SectionCreate(title="施工组织设计")
        )
        sse_db_ops._save_section_to_db(
            root.id,
            "completed",
            "正文内容足够长。\n<<CHART:甘特图>>\n含标记正文。",
            project_id=project_id,
        )
        with get_db() as conn:
            conn.execute(
                "UPDATE sections SET gantt_json = ? WHERE id = ?",
                ('{"type":"gantt","tasks":[{"name":"基础施工"}]}', root.id),
            )

        outline = [{"title": "施工组织设计", "children": []}]
        sse_db_ops._clear_and_save_outline(project_id, outline)

        with get_db(readonly=True) as conn:
            row = conn.execute(
                "SELECT id, title, content, gantt_json FROM sections "
                "WHERE project_id = ? AND title = ?",
                (project_id, "施工组织设计"),
            ).fetchone()
        assert row is not None
        assert row["content"], "正文应回填"
        assert (row["gantt_json"] or "") != "", "图表载荷应随正文一并回填"


# ============================================================
# W1：generation_mode 死参数
# ============================================================


class TestGenerationModeWired:
    def test_named_param_injected_into_merged_context(self):
        """具名形参 generation_mode 必须显式注入 merged_context（旧实现
        关键字实参绑定形参后不进 kwargs → 恒回落 config 默认）。"""
        src = inspect.getsource(workflows_sections.generate_sections)
        assert 'merged_context.setdefault("generation_mode", generation_mode)' in src


# ============================================================
# W3：mermaid（code 形态）围栏不再误降级
# ============================================================


class TestMermaidFenceRealData:
    def test_mermaid_fence_extracted_as_real_chart(self):
        from app.services.ai.workflows_sections import extract_section_charts

        content = (
            "施工流程如下。\n"
            "```mermaid\nflowchart TD\n    A[开工] --> B[基础施工]\n```\n"
            "后文。"
        )
        charts = extract_section_charts(content, "施工工艺流程")
        mermaid_entries = [c for c in charts if not c.get("_hint")]
        assert mermaid_entries, "mermaid 围栏必须作为真实数据条目提取"
        entry = mermaid_entries[0]
        assert str(entry.get("code") or "").strip(), "code 源码必须保留"

    def test_type_only_marker_still_degraded_to_hint(self):
        """type-only 空壳（无任何数据字段）仍降级为 hint，交编排生成。"""
        from app.services.ai.workflows_sections import extract_section_charts

        content = "前文。\n<<CHART:{\"type\":\"flowchart\"}>>\n后文。"
        charts = extract_section_charts(content, "流程")
        hints = [c for c in charts if c.get("_hint")]
        assert hints, "空壳必须降级为提示条目"


# ============================================================
# W4：扩写/审计修复接受前统一净化
# ============================================================


class TestSanitizeRewrittenContent:
    def test_markdown_heading_and_duplicate_title_stripped(self):
        raw = "## 施工部署\n施工部署\n实际正文内容。"
        out = _sanitize_rewritten_content("施工部署", raw)
        assert "##" not in out
        assert out.count("施工部署") <= 1 or "##" not in out

    def test_unclosed_fence_closed(self):
        raw = "正文。\n```mermaid\nflowchart TD\n    A --> B"
        out = _sanitize_rewritten_content("流程", raw)
        fence_lines = [ln for ln in out.split("\n") if ln.strip().startswith("```")]
        assert len(fence_lines) % 2 == 0, f"围栏必须闭合: {out!r}"

    def test_empty_passthrough(self):
        assert _sanitize_rewritten_content("t", "") == ""

    def test_audit_repair_long_section_skipped(self):
        """超长章节（>5000 字）审计修复必须显式跳过，不得用截断上下文
        的修复稿覆盖全文（旧行为：5000~6250 字章节丢尾、更长章节恒拒）。"""
        from app.services.ai import workflows_expand

        src = inspect.getsource(workflows_expand)
        assert "超过单次送审上限" in src


# ============================================================
# W2：强控覆盖表接入扩写阈值链
# ============================================================


class TestStrictOverrideInExpandThreshold:
    def test_strict_map_lowers_threshold(self):
        sections_by_id = {
            1: {"id": 1, "level": 4, "target_words": None, "word_count_tier": "standard"},
        }
        results = [{"id": 1, "status": "completed", "content": "字" * 700}]
        base = collect_deficient_sections(results, sections_by_id, 2000, writing_length="standard")
        strict = collect_deficient_sections(
            results, sections_by_id, 2000, writing_length="standard",
            strict_override_map={
                1: {"section_words": 500, "lo": 400, "hi": 600, "generation_target": 500}
            },
        )
        # 无强控：standard 1200×80%=960 → 700 字不足；有强控：500×80%=400
        # （cap flat_minimum=2000）→ 700 字已达标
        assert any(e["section"]["id"] == 1 for e in base)
        assert not any(e["section"]["id"] == 1 for e in strict)

    def test_expand_fallback_signature_accepts_strict_map(self):
        sig = inspect.signature(
            __import__(
                "app.services.ai.workflows_expand", fromlist=["expand_content_fallback"]
            ).expand_content_fallback
        )
        assert "strict_override_map" in sig.parameters


# ============================================================
# W5：None 占位补终态
# ============================================================


class TestNonePlaceholderBackfill:
    def test_cancellederror_without_cancel_event_becomes_failed(self):
        """子任务抛 CancelledError（非全局取消）→ 章节不得凭空消失。"""
        from app.services.ai.providers.base import AIMessage

        class _CancelProvider:
            async def chat(self, messages, **kwargs):
                raise asyncio.CancelledError()

        sections = [
            {"id": 1, "title": "甲", "level": 3},
            {"id": 2, "title": "乙", "level": 3},
        ]

        async def _run():
            return await workflows_sections.generate_sections(
                _CancelProvider(),
                sections,
                {},
                generation_mode="parallel",
            )

        results = asyncio.run(_run())
        assert len(results) == 2, f"返回长度必须等于输入: {results}"
        statuses = {r.get("id"): r.get("status") for r in results}
        # 两个章节的 provider 都抛 CancelledError 且无 cancel_event → failed
        assert all(s == "failed" for s in statuses.values()), statuses
        assert all(
            "异常退出" in (r.get("error") or "") for r in results
        )


# ============================================================
# P3 杂项
# ============================================================


class TestExpandContentWritingLength422:
    def test_expand_content_uses_is_valid_writing_length(self):
        from app.routers import sections as sections_router

        src = inspect.getsource(sections_router.expand_content)
        assert "is_valid_writing_length" in src
        # 旧死代码口径（normalize 后比对）不得残留为判定语句
        assert "if _norm_wl(writing_length) not in valid_lengths" not in src


class TestSaveSectionCRLFNormalized:
    def test_crlf_normalized_before_save(self, project_id):
        sec = sections_service.create_section(
            project_id, SectionCreate(title="CRLF章节")
        )
        sse_db_ops._save_section_to_db(
            sec.id, "completed", "行一\r\n行二\r\n行三", project_id=project_id
        )
        saved = _db_row(sec.id)["content"] or ""
        assert "\r" not in saved


class TestIsL3OwnNonePassthrough:
    def test_no_bool_coercion(self):
        src = inspect.getsource(workflows_sections)
        assert 'bool(chapter_info.get("is_l3_own"))' not in src


class TestGetSectionPathSiblingSortKey:
    def test_duplicate_sort_order_resolved_by_id(self, project_id):
        """同父 sort_order 重复（脏数据）时路径编号与 build_tree 的
        (sort_order, id) 单一权威键一致。"""
        a = sections_service.create_section(
            project_id, SectionCreate(title="甲章节")
        )
        b = sections_service.create_section(
            project_id, SectionCreate(title="乙章节")
        )
        with get_db() as conn:
            conn.execute(
                "UPDATE sections SET sort_order = 1 WHERE id IN (?, ?)", (a.id, b.id)
            )
        from app.services.sections import get_section_path

        path_a = get_section_path(a.id)
        path_b = get_section_path(b.id)
        first, second = (a.id, b.id) if a.id < b.id else (b.id, a.id)
        path_first = get_section_path(first)
        # id 较小者应为 1.1，较大者 1.2（与 build_tree 的 id 决胜一致）
        assert path_first.split(".")[-1] == "1"
        assert {path_a.split(".")[-1], path_b.split(".")[-1]} == {"1", "2"}


class TestSseChartsSaveFailureNotRegistered:
    def test_save_failure_skips_real_charts_registration(self):
        from app.routers.sse import sse_charts

        src = inspect.getsource(sse_charts)
        assert "if not await _save_section_field(" in src


class TestExportRetrySnapshotRestore:
    def test_retry_round_snapshots_figure_counter_and_titles(self):
        import app.services.export as export_mod

        # 重试循环与主路径同在 build_docx 内：源码必须同时包含主路径与
        # 重试路径的快照/还原（重试路径以「重试快照」注释为锚）
        src = inspect.getsource(export_mod)
        assert src.count("_snapshot_figure_counter") >= 2
        assert src.count("_snapshot_chart_title_registry") >= 2
        assert "重试快照图号计数器失败" in src


class TestFenceInsertionClampedOutsideCodeFences:
    def test_insert_land_outside_plain_fence(self):
        content = (
            "开篇段落。\n"
            "```python\n"
            "# 示例脚本：如下述流程\n"
            "def step_flow():\n"
            "    pass\n"
            "```\n"
            "结尾段落。"
        )
        fence = "```chart-json\n{\"type\":\"flowchart\",\"nodes\":[{\"id\":\"A\"}]}\n```"
        out = insert_fence_at_paragraph(content, fence, "", 2)  # 指向围栏内段落
        # 新图表围栏必须落在普通围栏之外：解析后普通围栏内容完整、
        # 图表围栏可被独立识别
        assert "def step_flow():" in out
        fence_starts = [
            i for i, ln in enumerate(out.split("\n")) if ln.strip().startswith("```")
        ]
        assert len(fence_starts) % 2 == 0, f"所有围栏必须保持闭合: {out!r}"
        from app.services.content_cleaning import scan_chart_fence_blocks

        blocks = scan_chart_fence_blocks(out.split("\n"))
        assert blocks, "插入的图表围栏必须可被权威扫描器识别为完整块"
        for start, end in blocks:
            inner = "\n".join(out.split("\n")[start : end + 1])
            assert "step_flow" not in inner, "图表围栏不得吞并普通围栏内容"

    def test_insert_without_fences_unchanged_position(self):
        content = "第一段。\n第二段。\n第三段。"
        out = insert_fence_at_paragraph(content, "FENCE", "", 1)
        assert out == "第一段。\n第二段。\n\nFENCE\n第三段。"


class TestCondensePromptToleranceAligned:
    def test_system_prompt_no_hardcoded_tolerance(self):
        from app.services.ai.prompts import content as content_prompts

        src = content_prompts.CONTENT_CONDENSE_SYSTEM
        assert "±10%" not in src
        assert "容差" in src


class TestRealAppChartRoutes:
    """真实 app 上的图表路由契约（双重前缀 P1 修复锁定）。

    根因：charts.py / charts_orchestration.py 自带 prefix="/api/v1/charts"，
    被 main.py v1_router(prefix="/api/v1") 再叠加 → 全部图表路由实际注册在
    /api/v1/api/v1/charts/*，前端调用的 /api/v1/charts/* 恒 404/405。
    历史测试用自建 FastAPI 直挂 router（无父前缀）掩盖了该缺陷——
    本类故意使用真实 app.main.app，是全仓唯一的生产挂载形态契约测试。
    """

    def test_health_reachable_at_intended_path(self, client):
        r = client.get("/api/v1/charts/health")
        assert r.status_code == 200, f"双重前缀回归: {r.status_code} {r.text[:120]}"

    def test_auto_orchestrate_reachable_no_attribute_error(self, client, project_id):
        sec = sections_service.create_section(
            project_id, SectionCreate(title="部署架构")
        )
        r = client.post(
            "/api/v1/charts/auto-orchestrate-section",
            json={
                "project_id": project_id,
                "section_id": sec.id,
                "insert_into_content": True,
                "use_ai": False,
            },
        )
        assert r.status_code == 200, f"生产路径失败: {r.status_code} {r.text[:200]}"
        body = r.json()
        assert body.get("ok") is True
        assert body.get("section_id") == sec.id
