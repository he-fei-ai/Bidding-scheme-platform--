"""图表增强与修复测试（2026-11-03，F1-F5 实施验证）

覆盖：
  A. suggest_insertion_position 三级策略（F1·需求 4·位置插入优化）：
     引导语段优先 / 图号变体 / 结构化行过滤 / 旧口径兜底；
  B. content_has_chart_marker_type（F2·需求 11·同类型内联标记判定）；
  C. auto_orchestrate_section 同类型标记防双图（F2·集成）：
     标记存在 → 降级落库不插围栏 / 不同类型互不阻止 / 竞态窗口标记写入；
  D. infer_mermaid_code_type / build_mermaid_payload 类型推断（F4·需求 3/8）；
  E. execute_illustration_phase 去重双检（F4b·集成）；
  F. _run_chat_sync 模块级线程池复用（F5·需求 10）。
"""

import asyncio
import json
import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import pytest

from app.models import SectionUpdate
from app.services.ai.chart_orchestrator import suggest_insertion_position
from app.services.ai.illustration_phase import (
    IllustrationTarget,
    build_mermaid_payload,
    execute_illustration_phase,
    infer_mermaid_code_type,
)
from app.services.chart_auto_orchestration import (
    auto_orchestrate_section,
    content_has_chart_marker_type,
)


# ============================================================
# 公共桩
# ============================================================

class _UpdateRecorder:
    def __init__(self):
        self.calls: list[tuple[int, SectionUpdate]] = []

    def __call__(self, section_id, data: SectionUpdate):
        self.calls.append((section_id, data))
        return None


def _make_section(title: str, content: str) -> dict:
    return {"id": 1, "project_id": 10, "title": title, "content": content}


def _force_analysis(chart_type: str):
    """monkeypatch analyze_chart_needs 强制判定需要指定类型图表。"""
    return lambda section_title, content, project_summary="", ai_client=None: {
        "needed": True, "chart_type": chart_type, "confidence": 0.9,
        "reason": "mock", "title_hint": section_title, "insert_after": "",
        "source": "mock",
    }


class _Store:
    """execute_illustration_phase 的 get/update 桩（内容随更新演进）。"""

    def __init__(self, content: str):
        self.content = content
        self.updates: list[SectionUpdate] = []

    def get(self, sid):
        return {"id": sid, "project_id": 1, "title": "t", "content": self.content}

    def update(self, sid, data: SectionUpdate):
        self.updates.append(data)
        self.content = data.content


VALID_MERMAID = (
    "flowchart TD\n"
    'A["需求确认"] --> B["方案设计"]\n'
    'B["方案设计"] --> C["施工准备"]'
)


# ============================================================
# A. suggest_insertion_position 三级策略（F1）
# ============================================================

class TestSuggestInsertionPosition:
    def test_leadin_paragraph_preferred(self):
        """① 引导语段优先：图随「下图为…」铺垫句。"""
        content = "第一段：项目概况。\n\n下图为施工流程：\n\n第三段：质量保证。"
        assert suggest_insertion_position(content, "flowchart", "施工工艺") == 1

    def test_leadin_with_figure_number_variant(self):
        """「如图3-2所示」变体（旧正则 \\d* 不匹配连字符，已修为 [\\d\\-]*）。"""
        content = "第一段。\n\n施工总体安排如图3-2所示，详见下文。\n\n第三段。"
        assert suggest_insertion_position(content, "flowchart") == 1

    def test_structured_lines_skipped(self):
        """② 唯一关键词命中行是列表项 → 不可作锚点，兜底前移到可锚点段。"""
        content = "段落甲。\n\n- 施工流程包括土方开挖与混凝土浇筑\n\n段落乙。"
        assert suggest_insertion_position(content, "flowchart") == 0

    def test_heading_lines_skipped(self):
        """小节标题行不作锚点（防图文断裂）。"""
        content = "段落甲。\n\n### 施工流程说明\n\n段落乙含工艺流程描述。"
        pos = suggest_insertion_position(content, "flowchart")
        # 标题行被过滤，命中段落乙（index 2）
        assert pos == 2

    def test_fallback_keeps_old_semantics(self):
        """③ 无引导语/无关键词 → 旧口径 min(1, len-1)。"""
        content = "段落一。\n\n段落二。\n\n段落三。"
        assert suggest_insertion_position(content, "comparison") == 1

    def test_title_bonus_fallback_first_paragraph(self):
        """③ 正文无关键词命中但标题含类型关键词 → 首段作锚点（弱兜底）。

        FIX 回归锚：标题加权不得进入 ② 打分（否则压过正文真实命中段，
        2026-10-17 契约 test_needed_section_fence_inserted_into_content 实测破坏）。
        """
        content = "项目总体说明段。\n\n第二段无关键词。"
        assert suggest_insertion_position(content, "architecture", "项目组织架构") == 0

    def test_body_keyword_beats_title_bonus(self):
        """正文命中段优先于标题加权：含「岗位」的第二段胜过标题引入的首段。"""
        content = "第一段：项目概况说明。\n\n第二段：本项目设置项目经理部，下设技术负责人、安全员、质检员等岗位。"
        assert suggest_insertion_position(content, "architecture", "项目组织架构") == 1

    def test_empty_content(self):
        assert suggest_insertion_position("", "flowchart") == -1
        assert suggest_insertion_position(None, "flowchart") == -1  # type: ignore[arg-type]


# ============================================================
# B. content_has_chart_marker_type（F2 第一部分）
# ============================================================

class TestContentHasChartMarkerType:
    def test_same_type_marker_detected(self):
        assert content_has_chart_marker_type("正文\n\n<<CHART:gantt>>\n\n后续", "gantt") is True

    def test_explicit_prefix_marker_detected(self):
        assert content_has_chart_marker_type("正文\n\n<<GANTT:施工进度>>\n\n后续", "gantt") is True

    def test_different_type_not_detected(self):
        """不同类型不匹配（甘特图标记不阻止流程图插入）。"""
        assert content_has_chart_marker_type("正文\n\n<<CHART:gantt>>\n\n后续", "flowchart") is False

    def test_fence_is_not_marker(self):
        """围栏不算标记（围栏去重由 content_has_chart_fence 负责）。"""
        content = "正文\n\n```mermaid\nflowchart TD\nA-->B\n```\n"
        assert content_has_chart_marker_type(content, "flowchart") is False

    def test_empty_inputs(self):
        assert content_has_chart_marker_type("", "gantt") is False
        assert content_has_chart_marker_type("正文", "") is False
        assert content_has_chart_marker_type("正文", "none") is False


# ============================================================
# C. auto_orchestrate_section 同类型标记防双图（F2 集成）
# ============================================================

class TestAutoOrchestrateSameTypeMarker:
    def test_same_type_marker_falls_back_to_chart_field(self, monkeypatch):
        """正文已有同类型 <<CHART:labor>> 标记 → 不插围栏，降级落库 labor_json。"""
        monkeypatch.setattr(
            "app.services.chart_auto_orchestration.analyze_chart_needs",
            _force_analysis("labor"),
        )
        rec = _UpdateRecorder()
        # 正文补充「数字+人」数据线索（A3 建议 2 空态判定要求 labor 载荷有
        # 真实数据支撑；钉桩意图——同类型标记防双图降级——不变）
        content = "正文第一段：本阶段配置管理人员 5 人。\n\n<<CHART:labor>>\n\n正文第二段。"
        result = auto_orchestrate_section(
            10, 1,
            _get_section=lambda sid: _make_section("劳动力配置计划", content),
            _update_section=rec,
        )
        assert result["inserted"] is False
        assert result["persisted_chart_field"] == "labor_json"
        assert result["skipped_reason"] == "chart_marker_exists_fallback_chart_field"
        assert result["fence"] == ""
        assert len(rec.calls) == 1
        _, update = rec.calls[0]
        assert json.loads(update.labor_json)["type"] == "labor"
        # 正文未被改写（围栏未插入，标记原样保留）
        assert update.content is None

    def test_different_type_marker_does_not_block(self, monkeypatch):
        """不同类型标记互不阻止：gantt 标记 + architecture 判定 → 正常插入。"""
        monkeypatch.setattr(
            "app.services.chart_auto_orchestration.analyze_chart_needs",
            _force_analysis("architecture"),
        )
        rec = _UpdateRecorder()
        content = "第一段：项目概况。\n\n<<CHART:gantt>>\n\n第二段：本节说明组织机构与岗位职责。"
        result = auto_orchestrate_section(
            10, 1,
            _get_section=lambda sid: _make_section("项目组织架构", content),
            _update_section=rec,
        )
        assert result["inserted"] is True
        assert len(rec.calls) == 1
        _, update = rec.calls[0]
        assert "```chart-json" in (update.content or "")
        # 原 gantt 标记保留（不同类型的图并存是合理形态）
        assert "<<CHART:gantt>>" in (update.content or "")

    def test_race_guard_marker_written_in_window(self, monkeypatch):
        """RACE-GUARD：重读发现竞态窗口内已写入同类型标记 → 转降级落库。"""
        monkeypatch.setattr(
            "app.services.chart_auto_orchestration.analyze_chart_needs",
            _force_analysis("labor"),
        )
        rec = _UpdateRecorder()
        calls = {"n": 0}

        def _get(sid):
            calls["n"] += 1
            if calls["n"] == 1:
                # 第 1 读正文含「数字+人」线索（A3 建议 2 空态判定通过后才能
                # 走到 RACE-GUARD；钉桩意图——竞态窗口标记写入转降级——不变）
                return _make_section("劳动力配置计划", "正文第一段：配置劳动力 10 人。\n\n正文第二段。")
            return _make_section("劳动力配置计划", "正文第一段：配置劳动力 10 人。\n\n<<CHART:labor>>\n\n正文第二段。")

        result = auto_orchestrate_section(10, 1, _get_section=_get, _update_section=rec)
        assert result["inserted"] is False
        assert result["persisted_chart_field"] == "labor_json"
        assert result["skipped_reason"] == "chart_marker_exists_fallback_chart_field"


# ============================================================
# D. infer_mermaid_code_type / build_mermaid_payload（F4）
# ============================================================

class TestInferMermaidCodeType:
    @pytest.mark.parametrize("code,expected", [
        ("gantt\n    title X", "gantt"),
        ("  GANTT\n    section A", "gantt"),
        ("flowchart TD\nA-->B", "flowchart"),
        ("graph TD\nA-->B", "flowchart"),
        ("", "flowchart"),
        ("这不是 mermaid", "flowchart"),
    ])
    def test_infer(self, code, expected):
        assert infer_mermaid_code_type(code) == expected

    def test_build_payload_gantt_label(self):
        """gantt 代码 → type=gantt，无 title 时兜底名「甘特图」（需求 8）。"""
        payload = build_mermaid_payload("gantt\n    title 施工计划", "")
        assert payload["type"] == "gantt"
        assert payload["title"] == "甘特图"

    def test_build_payload_flowchart_label(self):
        payload = build_mermaid_payload("flowchart TD\nA-->B", "施工流程")
        assert payload["type"] == "flowchart"
        assert payload["title"] == "施工流程"


# ============================================================
# E. execute_illustration_phase 去重双检（F4b 集成）
# ============================================================

class TestExecuteIllustrationDedup:
    def test_same_type_marker_skips(self):
        """正文已有同类型标记 → skipped（reason=chart_marker_exists），不双图。"""
        store = _Store("正文\n\n<<CHART:flowchart>>\n\n更多正文")
        targets = [IllustrationTarget(
            section_id=7, title="施工工艺", kind="mermaid", priority=4,
            payload={"title": "施工流程", "code": VALID_MERMAID},
        )]
        result = asyncio.run(execute_illustration_phase(
            1, targets, get_section=store.get, update_section=store.update,
        ))
        assert result["inserted"] == 0
        assert result["skipped"] == 1
        assert result["items"][0]["reason"] == "chart_marker_exists"
        assert store.updates == []

    def test_different_type_marker_still_inserts(self):
        """不同类型标记不阻止：gantt 标记 + flowchart 目标 → 正常插入。"""
        store = _Store("正文\n\n<<CHART:gantt>>\n\n更多正文")
        targets = [IllustrationTarget(
            section_id=7, title="施工工艺", kind="mermaid", priority=4,
            payload={"title": "施工流程", "code": VALID_MERMAID},
        )]
        result = asyncio.run(execute_illustration_phase(
            1, targets, get_section=store.get, update_section=store.update,
        ))
        assert result["inserted"] == 1
        assert store.updates and "```mermaid" in store.updates[0].content


# ============================================================
# F. _run_chat_sync 模块级线程池复用（F5）
# ============================================================

class TestChatBridgePool:
    def test_sync_provider_no_pool(self):
        """同步 provider（测试 fake）直接返回，不建池。"""
        from app.services.ai import chart_orchestrator as mod

        class SyncProvider:
            def chat(self, messages, **kw):
                return "sync-ok"

        assert mod._run_chat_sync(SyncProvider(), []) == "sync-ok"

    def test_running_loop_reuses_bridge_pool(self):
        """有运行循环 → 提交到模块级池；多次调用复用同一实例。"""
        from app.services.ai import chart_orchestrator as mod

        class AsyncProvider:
            async def chat(self, messages, **kw):
                return "ok"

        async def main():
            r1 = mod._run_chat_sync(AsyncProvider(), [])
            p1 = mod._get_chat_bridge_pool()
            r2 = mod._run_chat_sync(AsyncProvider(), [])
            p2 = mod._get_chat_bridge_pool()
            return r1, r2, p1 is p2

        r1, r2, same = asyncio.run(main())
        assert r1 == "ok" and r2 == "ok" and same
        # 测试收尾：关闭并重置模块级池（后续用例如需重建会惰性新建）
        if mod._CHAT_BRIDGE_POOL is not None:
            mod._CHAT_BRIDGE_POOL.shutdown(wait=False)
            mod._CHAT_BRIDGE_POOL = None

    def test_bridge_pool_threadsafe_init(self):
        """R3 加固锚：并发首调只建一个池（双重检查锁，防竞态泄漏）。"""
        import threading as _threading

        from app.services.ai import chart_orchestrator as mod

        results = []
        barrier = _threading.Barrier(8)

        def _worker():
            barrier.wait()
            results.append(mod._get_chat_bridge_pool())

        threads = [_threading.Thread(target=_worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len({id(p) for p in results}) == 1
        # 测试收尾：关闭并重置模块级池
        if mod._CHAT_BRIDGE_POOL is not None:
            mod._CHAT_BRIDGE_POOL.shutdown(wait=False)
            mod._CHAT_BRIDGE_POOL = None
