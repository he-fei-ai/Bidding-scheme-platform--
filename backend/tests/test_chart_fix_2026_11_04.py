# -*- coding: utf-8 -*-
"""图表模块批次 A 修复回归测试（2026-11-04 深度加固轮）。

覆盖项（每项对应一处已验证缺陷的修复）：
  A1  image_engine.generate_all_chart_arrangements 的
      _BATCH_ARRANGE_SYS_FALLBACK 先引用后赋值（UnboundLocalError → 整章静默丢图）
  A2  validate_chart_for_ai_pipeline 对 JSON 数据载荷的结构化校验分支
      （layout/timeline/gantt/comparison/flowchart；含 AI 修复轮无 code 的 dict 载荷）
  A2b _strip_generation_fence 对 ```json / ```mermaid / 无标签围栏的剥离
  A3  甘特图 PIL 兜底 plan title 取载荷包装器 title（不再硬编码「施工进度计划」）
  A4  flowchart_json_to_mermaid 不再生成非法 `title` 行
  A5  export_cache L1 缓存命中/未命中计数在锁内自增
  A7  validate_flowchart_structure 节点 ID ASCII 收紧（re.ASCII）
  A8  _resolve_chart_provider 失败降级行为契约（返回 None，不抛异常）
"""

import asyncio
import io
import json
import threading

import pytest

from app.services.ai import image_engine
from app.services.ai.image_engine import (
    _strip_generation_fence,
    generate_all_chart_arrangements,
    validate_chart_for_ai_pipeline,
)
from app.services.ai.mermaid_flowchart import flowchart_json_to_mermaid
from app.services.chart_validators import validate_flowchart_structure


# ============================================================
# A1：批量编排 fallback 常量先引用后赋值
# ============================================================
class TestBatchArrangeFallback:
    def test_empty_registry_prompt_uses_fallback_without_unbound_error(self, monkeypatch):
        """get_prompt 返回空（注册表/DB 故障）时不再抛 UnboundLocalError，
        回退到硬编码 fallback 提示词并正常返回编排结果。"""
        monkeypatch.setattr(image_engine, "get_prompt", lambda *a, **k: "")

        async def _fake_collect(provider, messages, max_retries=3):
            # 捕获 system 提示词，验证 fallback 文本确实被使用
            captured["system"] = messages[0].content
            return {"flowchart": {"needed": True, "type": "construction", "priority": 5}}

        captured = {}
        monkeypatch.setattr(image_engine, "collect_json_response", _fake_collect)

        result = asyncio.run(
            generate_all_chart_arrangements(
                None,
                section_title="外墙保温施工",
                section_content="本工程采用逐层施工工艺。",
            )
        )
        assert isinstance(result, dict)
        assert result["flowchart"]["needed"] is True
        # fallback 提示词必须生效（包含 V8.0 版本头）
        assert "批量图表编排" in (captured.get("system") or "")

    def test_registry_prompt_still_wins(self, monkeypatch):
        """注册表提示词可用时优先于 fallback（既有行为不回归）。"""
        monkeypatch.setattr(
            image_engine, "get_prompt", lambda *a, **k: "# REGISTRY PROMPT"
        )

        async def _fake_collect(provider, messages, max_retries=3):
            captured["system"] = messages[0].content
            return {"gantt": {"needed": False}}

        captured = {}
        monkeypatch.setattr(image_engine, "collect_json_response", _fake_collect)

        asyncio.run(
            generate_all_chart_arrangements(
                None, section_title="x", section_content="y"
            )
        )
        assert captured["system"].startswith("# REGISTRY PROMPT")


# ============================================================
# A2：JSON 数据载荷结构化校验分支
# ============================================================
class TestJsonPayloadValidation:
    def test_layout_json_code_payload_passes_and_unwraps(self):
        """layout 载荷 code=JSON 字符串 → 结构化校验通过，zones 提升到顶层、code 移除。"""
        zones = [
            {"id": "Z1", "name": "办公区", "x": 5, "y": 5, "w": 30, "h": 30},
            {"id": "Z2", "name": "加工区", "x": 45, "y": 5, "w": 30, "h": 30},
            {"id": "Z3", "name": "材料堆场", "x": 5, "y": 45, "w": 30, "h": 30},
        ]
        payload = {
            "type": "layout",
            "code": json.dumps({"zones": zones, "connections": []}, ensure_ascii=False),
            "title": "施工现场平面布置图",
        }
        ok, errors, data, mutated, _ctx = validate_chart_for_ai_pipeline("layout", payload)
        assert ok, errors
        assert "zones" in data and len(data["zones"]) == 3
        assert "code" not in data  # JSON 数据型载荷统一无 code（与 architecture/labor 同构）
        assert data["title"] == "施工现场平面布置图"
        assert mutated is True

    def test_layout_invalid_json_payload_reports_errors(self):
        """layout zones 重叠 → 结构化校验报错（供 AI 修复轮消费）。"""
        zones = [
            {"id": "Z1", "name": "A", "x": 5, "y": 5, "w": 40, "h": 40},
            {"id": "Z2", "name": "B", "x": 10, "y": 10, "w": 30, "h": 30},
        ]
        payload = {
            "type": "layout",
            "code": json.dumps({"zones": zones}, ensure_ascii=False),
            "title": "布置图",
        }
        ok, errors, data, _mutated, _ctx = validate_chart_for_ai_pipeline("layout", payload)
        assert not ok
        assert errors

    def test_layout_fix_round_dict_without_code_passes(self):
        """AI 修复轮返回无 code 的 LayoutPlan dict（原二轮必败场景）→ 现通过。"""
        payload = {
            "type": "layout",
            "title": "总平面布置图",
            "zones": [
                {"id": "Z1", "name": "办公区", "x": 5, "y": 5, "w": 25, "h": 25},
                {"id": "Z2", "name": "加工场", "x": 40, "y": 5, "w": 25, "h": 25},
            ],
        }
        ok, errors, data, _mutated, _ctx = validate_chart_for_ai_pipeline("layout", payload)
        assert ok, errors
        assert "zones" in data
        assert "code" not in data

    def test_timeline_json_payload_passes(self):
        """timeline 载荷 code=TimelinePlan JSON → 结构化校验通过。"""
        milestones = [
            {"title": "开工", "date": "2026-01-01", "status": "completed"},
            {"title": "主体封顶", "date": "2026-06-01", "status": "in_progress"},
            {"title": "竣工", "date": "2026-12-01", "status": "pending"},
        ]
        payload = {
            "type": "timeline",
            "code": json.dumps({"milestones": milestones}, ensure_ascii=False),
            "title": "关键里程碑时间线",
        }
        ok, errors, data, _mutated, _ctx = validate_chart_for_ai_pipeline("timeline", payload)
        assert ok, errors
        assert "milestones" in data and len(data["milestones"]) == 3
        assert "code" not in data

    def test_gantt_json_tasks_payload_passes(self):
        """gantt 载荷 code=GanttPlan JSON（tasks）→ 结构化校验通过（渲染端已支持）。"""
        tasks = [
            {"id": 1, "name": "土方开挖", "start": "2026-01-01", "end": "2026-02-01"},
            {"id": 2, "name": "主体结构", "start": "2026-02-01", "end": "2026-06-01"},
        ]
        payload = {
            "type": "gantt",
            "code": json.dumps({"needed": True, "totalDays": 150, "tasks": tasks}, ensure_ascii=False),
            "title": "施工进度计划",
        }
        ok, errors, data, _mutated, _ctx = validate_chart_for_ai_pipeline("gantt", payload)
        assert ok, errors
        assert len(data["tasks"]) == 2
        assert "code" not in data

    def test_comparison_table_json_minimal_check(self):
        """表格型对比图（headers+rows 多指标）→ 只做 rows 非空检查，不被饼图百分比校验误杀。"""
        payload = {
            "type": "comparison",
            "code": json.dumps(
                {
                    "headers": ["方案", "材料费", "人工费"],
                    "rows": [
                        {"方案": "A", "材料费": 80, "人工费": 50},
                        {"方案": "B", "材料费": 65, "人工费": 70},
                    ],
                },
                ensure_ascii=False,
            ),
            "title": "方案对比",
        }
        ok, errors, data, _mutated, _ctx = validate_chart_for_ai_pipeline("comparison", payload)
        assert ok, errors
        assert "code" not in data

    def test_flowchart_json_nodes_payload_passes(self):
        """flowchart 载荷 code={"nodes","edges"} JSON → 结构化校验通过。"""
        payload = {
            "type": "flowchart",
            "code": json.dumps(
                {
                    "nodes": [
                        {"id": "A", "label": "开始"},
                        {"id": "B", "label": "结束"},
                    ],
                    "edges": [{"from": "A", "to": "B"}],
                },
                ensure_ascii=False,
            ),
            "title": "施工流程图",
        }
        ok, errors, data, _mutated, _ctx = validate_chart_for_ai_pipeline("flowchart", payload)
        assert ok, errors
        assert "nodes" in data
        assert "code" not in data

    def test_mermaid_syntax_still_goes_mermaid_branch(self):
        """Mermaid 语法载荷不被 JSON 分支劫持：坏语法仍报「语法」错误。"""
        payload = {"type": "gantt", "code": "gantt\n缺dateFormat", "title": "t"}
        ok, errors, _data, _mutated, _ctx = validate_chart_for_ai_pipeline("gantt", payload)
        assert not ok
        assert any("语法" in e or "dateFormat" in e for e in errors)

    def test_garbage_json_reports_errors_not_crash(self):
        """非法 JSON 文本（{ 开头但解析失败）→ 回落 Mermaid 分支报语法错误，不抛异常。"""
        payload = {"type": "layout", "code": "{zones: [broken", "title": "t"}
        ok, errors, _data, _mutated, _ctx = validate_chart_for_ai_pipeline("layout", payload)
        assert not ok
        assert errors


# ============================================================
# A2b：_strip_generation_fence 围栏剥离
# ============================================================
class TestStripGenerationFence:
    def test_json_fence_stripped(self):
        raw = "```json\n{\"zones\": []}\n```"
        assert _strip_generation_fence(raw) == '{"zones": []}'

    def test_mermaid_fence_stripped(self):
        raw = "```mermaid\nflowchart TD\n    A --> B\n```"
        assert _strip_generation_fence(raw).startswith("flowchart TD")

    def test_plain_json_passthrough(self):
        assert _strip_generation_fence('{"a": 1}') == '{"a": 1}'

    def test_unclosed_fence_stripped(self):
        raw = "```json\n{\"a\": 1}"
        assert _strip_generation_fence(raw) == '{"a": 1}'


# ============================================================
# A3：甘特图 PIL 兜底 plan title
# ============================================================
class TestGanttPilTitle:
    def test_plan_title_from_payload_wrapper(self, monkeypatch):
        """载荷包装器 title 必须传入甘特 v2 渲染 plan（原硬编码「施工进度计划」）。"""
        from app.services.ai import mermaid_renderer

        captured = {}

        def _fake_gantt_render(plan, *a, **k):
            captured["title"] = plan.get("title")
            return io.BytesIO(b"\x89PNG fake")

        monkeypatch.setattr(mermaid_renderer, "_render_gantt_image_v2", _fake_gantt_render)

        gantt_code = (
            "gantt\n"
            "    title 外墙整修工程施工进度计划\n"
            "    dateFormat  YYYY-MM-DD\n"
            "    section 准备阶段\n"
            "    测量放线 :a1, 2026-01-01, 5d\n"
            "    section 主体阶段\n"
            "    外墙抹灰 :a2, 2026-01-06, 10d\n"
        )
        wrapper = json.dumps(
            {"type": "gantt", "code": gantt_code, "title": "外墙整修工程总进度计划"},
            ensure_ascii=False,
        )
        # skip_http=True 直接走 PIL 兜底路径
        out = mermaid_renderer.render_mermaid_to_bytes(wrapper, "gantt", skip_http=True)
        assert out is not None
        assert captured["title"] == "外墙整修工程总进度计划"


# ============================================================
# A4：flowchart_json_to_mermaid 不再生成非法 title 行
# ============================================================
class TestFlowchartJsonToMermaid:
    def test_no_title_line_emitted(self):
        data = {
            "title": "施工流程图",
            "direction": "TD",
            "nodes": [
                {"id": "A", "label": "开始", "type": "start"},
                {"id": "B", "label": "结束", "type": "end"},
            ],
            "edges": [{"from": "A", "to": "B"}],
        }
        code = flowchart_json_to_mermaid(data)
        assert code is not None
        assert not any(
            ln.strip().startswith("title") for ln in code.split("\n")
        ), f"flowchart 代码不应包含 title 行:\n{code}"
        assert code.split("\n")[0].startswith("flowchart")


# ============================================================
# A5：L1 渲染缓存计数器（锁内自增）
# ============================================================
class TestL1CacheCounters:
    def test_hit_and_miss_counters_increment(self):
        from app.services import export_cache as ec

        payload = "TEST_PAYLOAD_A5_2026_11_04"
        ec._store_chart_render(payload, io.BytesIO(b"png-bytes"), "flowchart")
        before_hits = ec._chart_render_cache_hits
        before_misses = ec._chart_render_cache_misses

        hit = ec._get_cached_chart_render(payload, "flowchart")
        assert hit is not None
        assert ec._chart_render_cache_hits == before_hits + 1

        miss = ec._get_cached_chart_render(payload, "gantt")  # 不同类型 → 不同键
        assert miss is None
        assert ec._chart_render_cache_misses == before_misses + 1

    def test_concurrent_counter_smoke(self):
        """并发命中/未命中不抛异常且计数单调不减（竞态冒烟）。"""
        from app.services import export_cache as ec

        payload = "TEST_PAYLOAD_A5_CONCURRENT"
        ec._store_chart_render(payload, io.BytesIO(b"png"), "layout")
        before = ec._chart_render_cache_hits

        def _worker():
            for _ in range(20):
                ec._get_cached_chart_render(payload, "layout")
                ec._get_cached_chart_render("no-such-payload", "layout")

        threads = [threading.Thread(target=_worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert ec._chart_render_cache_hits >= before + 1


# ============================================================
# A7：validate_flowchart_structure 节点 ID ASCII 收紧
# ============================================================
class TestFlowchartAsciiNodeId:
    def test_chinese_node_id_rejected(self):
        data = {
            "nodes": [
                {"id": "A", "label": "开始"},
                {"id": "中文ID", "label": "坏ID"},
            ],
            "edges": [{"from": "A", "to": "中文ID"}],
        }
        ok, errs = validate_flowchart_structure(data)
        assert not ok
        assert any("ASCII" in e for e in errs)

    def test_ascii_node_id_passes(self):
        data = {
            "nodes": [
                {"id": "A", "label": "开始"},
                {"id": "B", "label": "结束"},
            ],
            "edges": [{"from": "A", "to": "B"}],
        }
        ok, errs = validate_flowchart_structure(data)
        assert ok, errs


# ============================================================
# A8：_resolve_chart_provider 失败降级契约
# ============================================================
class TestResolveChartProviderContract:
    def test_provider_failure_returns_none_not_raise(self, monkeypatch):
        from app.services.ai import chart_orchestrator

        def _raise(*a, **k):
            raise RuntimeError("provider 创建失败")

        monkeypatch.setattr(
            "app.services.ai.provider_factory.create_provider_with_fallback", _raise
        )
        # 传入配置 dict（routers/charts_orchestration 的实际形态）
        result = chart_orchestrator._resolve_chart_provider({"model": "x"})
        assert result is None  # 降级走本地推断，不抛异常

    def test_platform_provider_passthrough(self):
        from app.services.ai import chart_orchestrator

        class _Fake:
            def chat(self, *a, **k):
                return ""

        obj = _Fake()
        assert chart_orchestrator._resolve_chart_provider(obj) is obj


# ============================================================
# B1：编排去重守卫放宽（同类型去重 + 每章 ≤2 图）
# ============================================================
class TestFenceTypeConflictGuard:
    def test_fence_types_mermaid_inference(self):
        from app.services.chart_auto_orchestration import content_chart_fence_types

        content = (
            "正文\n\n```mermaid\ngantt\n dateFormat YYYY-MM-DD\n 任务 :a1, 2026-01-01, 5d\n```\n"
            "中间段\n\n```mermaid\nflowchart TD\n A-->B\n```\n"
        )
        types = content_chart_fence_types(content)
        assert types == ["gantt", "flowchart"]

    def test_fence_types_chart_json_payload(self):
        from app.services.chart_auto_orchestration import content_chart_fence_types

        content = (
            '正文\n\n```chart-json\n{"type": "layout", "title": "布置", '
            '"zones": [{"x": 5, "y": 5, "w": 20, "h": 20}]}\n```\n'
        )
        assert content_chart_fence_types(content) == ["layout"]

    def test_same_type_fence_conflicts(self):
        from app.services.chart_auto_orchestration import chart_fence_type_conflict

        content = "正文\n\n```mermaid\nflowchart TD\n A-->B\n```\n"
        assert chart_fence_type_conflict(content, "flowchart") is True
        assert chart_fence_type_conflict(content, "gantt") is False  # 不同类型放行

    def test_two_fences_reach_cap(self):
        from app.services.chart_auto_orchestration import chart_fence_type_conflict

        content = (
            "正文\n\n```mermaid\nflowchart TD\n A-->B\n```\n"
            "\n```mermaid\npie title 占比\n \"A\": 50\n \"B\": 50\n```\n"
        )
        assert chart_fence_type_conflict(content, "gantt") is True  # 已达 2 图上限

    def test_second_type_chart_inserted(self):
        """正文已有甘特图围栏 → 流程图仍可补插（图文互补，对齐写作契约）。"""
        from app.services.chart_auto_orchestration import auto_orchestrate_section

        class _Rec:
            calls = []

            def __call__(self, sid, update):
                self.calls.append((sid, update))

        rec = _Rec()
        content = (
            "本工程采用逐层施工工艺：先放线、再开挖、后浇筑，"
            "每道工序检验合格后方可进入下一道工序。\n\n"
            "```mermaid\ngantt\n dateFormat YYYY-MM-DD\n "
            "基础阶段 :a1, 2026-01-01, 30d\n```\n"
        )
        result = auto_orchestrate_section(
            10, 1,
            _get_section=lambda sid: {"id": sid, "title": "施工工艺流程", "content": content},
            _update_section=rec,
        )
        assert result["ok"] is True
        assert result["inserted"] is True, result
        assert len(rec.calls) == 1
        new_content = rec.calls[0][1].content
        assert "```mermaid" in new_content
        # 原甘特图围栏保留（正文不被吞）
        assert "gantt" in new_content
        assert "dateFormat" in new_content

    def test_same_type_fence_still_blocks(self):
        """正文已有同类型围栏 → 仍拒绝（防同章双同类图，F2 语义延续）。"""
        from app.services.chart_auto_orchestration import auto_orchestrate_section

        class _Rec:
            calls = []

            def __call__(self, sid, update):
                self.calls.append((sid, update))

        rec = _Rec()
        content = "正文\n\n```mermaid\nflowchart TD\n A-->B\n```\n"
        result = auto_orchestrate_section(
            10, 1,
            _get_section=lambda sid: {"id": sid, "title": "施工工艺流程", "content": content},
            _update_section=rec,
        )
        assert result["skipped_reason"] == "chart_fence_exists"
        assert result["inserted"] is False
        assert rec.calls == []
