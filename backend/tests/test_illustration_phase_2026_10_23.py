"""标书智能体（6）——超长文本生成与图文控制对齐回归测试（2026-10-23）。

外部引用标准（《超长文本生成和图文控制》，冲突时以外部软件机制为准）：
  1. 预编排 JSON 结构：plan = {table, mermaid(title/code), chart(type/data),
     image(style/title/prompt), knowledge, facts}；
  2. 程序侧强制「只有 priority ≥ 3 才允许 needed = true」；
  3. AI 提名、程序拍板：illustration_type 最终只能一个（mermaid/chart/ai/none）；
  4. 正文与配图严格分离：正文严禁输出图表代码块，配图在全部正文完成后统一执行；
  5. 失败隔离：配图失败不影响正文结果。

本文件覆盖本轮落地的五项改动：
  A. 编排输出契约（system 提示词程序强制 plan 结构）；
  B. _normalize_plan 新结构归一化 + priority 门禁；
  C. _adjudicate_illustrations 数据型图表（chart）裁定；
  D. illustration_phase 配图阶段（收集目标 / 校验修复 / 插入 / 失败隔离）；
  E. CONTENT_SECTION_SYSTEM_PLANNED 规划式正文提示词 + 配图意图注入。
"""

import asyncio

import pytest

import app.services.ai.workflows_orchestration as orch_mod
import app.services.ai.illustration_phase as phase_mod
from app.services.ai.illustration_phase import (
    IllustrationTarget,
    build_chart_payload,
    collect_illustration_targets,
    compose_image_prompt,
    execute_illustration_phase,
)
from app.services.ai.workflows_orchestration import (
    _adjudicate_illustrations,
    _build_orch_system_prompt,
    _fallback_plan,
    _normalize_plan,
)

VALID_MERMAID = (
    "flowchart TD\n"
    'A["需求确认"] --> B["方案设计"]\n'
    'B --> C["实施部署"]\n'
    'C --> D["验收交付"]'
)


# ---------------------------------------------------------------------------
# A. 编排输出契约
# ---------------------------------------------------------------------------
class TestOrchSystemContract:

    def test_contract_appended(self):
        out = _build_orch_system_prompt("你是编排专家。")
        assert "【输出契约（程序强制，最高优先级）】" in out
        assert out.startswith("你是编排专家。")
        # 契约必须包含 plan 全部块（对齐外部文档结构）
        for kw in ('"plan"', '"mermaid"', '"chart"', '"image"', '"table"',
                   '"knowledge"', '"facts"', "priority"):
            assert kw in out

    def test_contract_idempotent(self):
        once = _build_orch_system_prompt("BASE")
        twice = _build_orch_system_prompt(once)
        assert twice == once

    def test_contract_chart_removable(self, monkeypatch):
        import app.config as cfg
        monkeypatch.setattr(cfg, "ORCHESTRATION_CHART_ENABLED", False)
        out = _build_orch_system_prompt("BASE")
        assert '"chart"' not in out
        # 其余块保留
        assert '"mermaid"' in out and '"image"' in out


# ---------------------------------------------------------------------------
# B. _normalize_plan 新结构归一化 + priority 门禁
# ---------------------------------------------------------------------------
class TestNormalizePlan:

    def _leaf(self):
        return {"id": 12, "title": "施工工艺"}

    def test_mermaid_title_and_code_normalized(self):
        raw = {
            "section_id": "12",
            "title": "施工工艺",
            "plan": {
                "mermaid": {
                    "needed": True, "priority": 4, "reason": "有流程关系",
                    "title": " 施工工艺流程图 ",
                    "code": "```mermaid\nflowchart TD\nA --> B\n```",
                },
            },
        }
        plan = _normalize_plan(raw, self._leaf())
        mm = plan["plan"]["mermaid"]
        assert mm["needed"] is True
        assert mm["priority"] == 4
        assert mm["title"] == "施工工艺流程图"
        assert mm["code"] == "flowchart TD\nA --> B"
        # 新结构：chart 块恒存在
        assert plan["plan"]["chart"]["needed"] is False

    def test_priority_below_three_rejected(self):
        """程序侧强制：priority<3 时 needed=true 被丢弃（外部规则）。"""
        raw = {
            "section_id": "12",
            "plan": {
                "mermaid": {"needed": True, "priority": 1, "reason": "r"},
                "image": {"needed": True, "priority": 0, "reason": "r"},
            },
        }
        plan = _normalize_plan(raw, self._leaf())
        assert plan["plan"]["mermaid"]["needed"] is False
        assert plan["plan"]["image"]["needed"] is False

    def test_chart_type_whitelist_and_data_required(self):
        raw = {
            "section_id": "12",
            "plan": {
                "chart": {
                    "needed": True, "priority": 5, "type": "mindmap", "data": {"a": 1},
                },
            },
        }
        plan = _normalize_plan(raw, self._leaf())
        assert plan["plan"]["chart"]["needed"] is False  # 类型不在白名单

        raw2 = {
            "section_id": "12",
            "plan": {
                "chart": {"needed": True, "priority": 5, "type": "gantt", "data": None},
            },
        }
        plan2 = _normalize_plan(raw2, self._leaf())
        assert plan2["plan"]["chart"]["needed"] is False  # 无真实数据不画图

    def test_image_style_and_prompt(self):
        raw = {
            "section_id": "12",
            "plan": {
                "image": {
                    "needed": True, "priority": 4,
                    "style": "poster_style",  # 非法风格 → 清空
                    "title": "部署架构",
                    "prompt": "生成系统部署示意",
                },
            },
        }
        plan = _normalize_plan(raw, self._leaf())
        img = plan["plan"]["image"]
        assert img["style"] == ""
        assert img["title"] == "部署架构"
        assert img["prompt"] == "生成系统部署示意"

    def test_fallback_plan_has_chart_block(self):
        fb = _fallback_plan({"id": 1, "title": "t"})
        assert fb["plan"]["chart"]["needed"] is False
        assert fb["illustration_type"] == "none"


# ---------------------------------------------------------------------------
# C. _adjudicate_illustrations 数据型图表裁定
# ---------------------------------------------------------------------------
class TestAdjudicateChart:

    def _plan(self, sid, *, mermaid=False, chart=False, image=False):
        return {
            "section_id": sid,
            "title": sid,
            "plan": {
                "table": {"needed": False, "purpose": ""},
                "mermaid": {"needed": mermaid, "priority": 4 if mermaid else 0, "reason": "r"},
                "chart": {"needed": chart, "priority": 5 if chart else 0, "reason": "r",
                          "type": "gantt", "title": "进度", "data": {"tasks": []}},
                "image": {"needed": image, "priority": 5 if image else 0, "reason": "r"},
                "knowledge": {"item_ids": []},
                "facts": {"titles": []},
            },
            "illustration_type": None,
        }

    def test_chart_adjudicated_when_no_mermaid(self):
        plans = [self._plan(f"{i}") for i in range(1, 4)]
        plans[0]["plan"]["chart"]["needed"] = True
        out = _adjudicate_illustrations(plans, max_ai_images=0)
        assert out[0]["illustration_type"] == "chart"

    def test_mermaid_wins_same_section(self):
        """同一章节 mermaid 与 chart 都提名 → mermaid 先占位（外部：最终只留一种）。"""
        plans = [self._plan("1", mermaid=True, chart=True)]
        out = _adjudicate_illustrations(plans, max_ai_images=0)
        assert out[0]["illustration_type"] == "mermaid"

    def test_chart_and_image_can_coexist_in_different_sections(self):
        plans = [self._plan(f"{i}") for i in range(1, 4)]
        plans[0]["plan"]["chart"]["needed"] = True
        plans[2]["plan"]["image"]["needed"] = True
        out = _adjudicate_illustrations(plans, max_ai_images=2)
        kinds = {p["section_id"]: p["illustration_type"] for p in out}
        assert kinds["1"] == "chart"
        assert kinds["3"] == "ai"

    def test_merged_mode_includes_chart(self):
        plans = [self._plan("1", chart=True), self._plan("2", mermaid=True)]
        out = _adjudicate_illustrations(plans, max_ai_images=2, prefer_mermaid=False)
        kinds = {p["illustration_type"] for p in out}
        assert "chart" in kinds and "mermaid" in kinds


# ---------------------------------------------------------------------------
# D. illustration_phase 配图阶段
# ---------------------------------------------------------------------------
class TestCollectTargets:

    def _leaf(self, sid, ill, plan_extra=None):
        plan = {
            "table": {"needed": False, "purpose": ""},
            "mermaid": {"needed": False, "priority": 0, "reason": "", "title": "", "code": ""},
            "chart": {"needed": False, "priority": 0, "reason": "", "type": "", "title": "", "data": {}},
            "image": {"needed": False, "priority": 0, "reason": "", "style": "", "title": "", "prompt": ""},
        }
        if plan_extra:
            plan.update(plan_extra)
        return {"id": sid, "title": f"章节{sid}", "plan": plan, "illustration_type": ill}

    def test_mapping_and_payload(self):
        leaves = [
            self._leaf(1, "mermaid", {"mermaid": {
                "needed": True, "priority": 4, "title": "流程", "code": VALID_MERMAID}}),
            self._leaf(2, "chart", {"chart": {
                "needed": True, "priority": 5, "type": "gantt", "title": "进度",
                "data": {"tasks": []}}}),
            self._leaf(3, "ai", {"image": {
                "needed": True, "priority": 3, "style": "engineering_diagram",
                "title": "架构", "prompt": "p"}}),
            self._leaf(4, "none"),
        ]
        targets = collect_illustration_targets(leaves)
        assert [t.section_id for t in targets] == [1, 2, 3]
        assert [t.kind for t in targets] == ["mermaid", "chart", "image"]
        assert targets[0].payload["code"] == VALID_MERMAID
        assert targets[1].payload["chart_type"] == "gantt"
        assert targets[2].kind == "image"  # ai → image 归一

    def test_global_cap_keeps_high_priority(self):
        leaves = [
            self._leaf(1, "mermaid", {"mermaid": {
                "needed": True, "priority": 3, "title": "a", "code": VALID_MERMAID}}),
            self._leaf(2, "mermaid", {"mermaid": {
                "needed": True, "priority": 5, "title": "b", "code": VALID_MERMAID}}),
            self._leaf(3, "mermaid", {"mermaid": {
                "needed": True, "priority": 4, "title": "c", "code": VALID_MERMAID}}),
        ]
        targets = collect_illustration_targets(leaves, max_charts=2)
        assert [t.section_id for t in targets] == [2, 3]  # 优先级 5/4 胜出，目录序输出


class _FakeSectionStore:
    def __init__(self, sections):
        self.sections = dict(sections)
        self.updates = []

    def get(self, sid):
        return self.sections.get(sid)

    def update(self, sid, model):
        self.updates.append((sid, model))
        self.sections[sid] = {**self.sections[sid], "content": model.content}
        return True


class TestExecutePhase:

    def _store(self, content="第一段正文。\n\n第二段正文。\n\n第三段正文。"):
        return _FakeSectionStore({7: {"id": 7, "title": "施工工艺", "content": content}})

    def test_mermaid_inserted(self):
        store = self._store()
        targets = [IllustrationTarget(
            section_id=7, title="施工工艺", kind="mermaid", priority=4,
            payload={"title": "施工流程", "code": VALID_MERMAID},
        )]
        result = asyncio.run(execute_illustration_phase(
            1, targets, get_section=store.get, update_section=store.update,
        ))
        assert result["inserted"] == 1
        assert store.updates and "```mermaid" in store.updates[0][1].content
        assert VALID_MERMAID in store.updates[0][1].content

    def test_failure_isolation_invalid_mermaid_without_provider(self):
        """失败隔离：无法渲染且无修复器 → 只跳过该图，正文不动。"""
        store = self._store()
        targets = [IllustrationTarget(
            section_id=7, title="t", kind="mermaid", priority=4,
            payload={"title": "t", "code": "这不是 mermaid 代码"},
        )]
        result = asyncio.run(execute_illustration_phase(
            1, targets, provider=None, get_section=store.get, update_section=store.update,
        ))
        assert result["inserted"] == 0 and result["failed"] == 1
        assert store.updates == []

    def test_chart_invalid_payload_failed(self):
        store = self._store()
        targets = [IllustrationTarget(
            section_id=7, title="t", kind="chart", priority=4,
            payload={"title": "t", "chart_type": "", "data": {}},
        )]
        result = asyncio.run(execute_illustration_phase(
            1, targets, get_section=store.get, update_section=store.update,
        ))
        assert result["failed"] == 1
        assert store.updates == []

    def test_image_skipped_by_default(self):
        """AI 生图默认关闭（外部文档口径），配图阶段跳过 image 目标。"""
        store = self._store()
        targets = [IllustrationTarget(
            section_id=7, title="t", kind="image", priority=4,
            payload={"title": "t", "style": "", "prompt": "p"},
        )]
        result = asyncio.run(execute_illustration_phase(
            1, targets, get_section=store.get, update_section=store.update,
        ))
        assert result["skipped"] == 1
        assert result["items"][0]["reason"] == "ai_image_disabled"

    def test_dedup_existing_fence(self):
        store = self._store(content="正文。\n\n```mermaid\nflowchart TD\nA --> B\n```\n\n更多。")
        targets = [IllustrationTarget(
            section_id=7, title="t", kind="mermaid", priority=4,
            payload={"title": "t", "code": VALID_MERMAID},
        )]
        result = asyncio.run(execute_illustration_phase(
            1, targets, get_section=store.get, update_section=store.update,
        ))
        assert result["skipped"] == 1
        assert result["items"][0]["reason"] == "chart_fence_exists"

    def test_section_not_found(self):
        store = self._store()
        targets = [IllustrationTarget(
            section_id=999, title="t", kind="mermaid", priority=4,
            payload={"title": "t", "code": VALID_MERMAID},
        )]
        result = asyncio.run(execute_illustration_phase(
            1, targets, get_section=store.get, update_section=store.update,
        ))
        assert result["skipped"] == 1
        assert result["items"][0]["reason"] == "section_not_found"


class TestPayloadHelpers:

    def test_compose_image_prompt_style_suffix(self):
        p = compose_image_prompt("系统部署示意", "engineering_diagram")
        assert "系统部署示意" in p
        assert "工程项目图示风格" in p
        assert "水印" in p
        # 未知风格 → 原样返回
        assert compose_image_prompt("abc", "unknown") == "abc"
        assert compose_image_prompt("", "engineering_diagram") == ""

    def test_build_chart_payload_merges_data(self):
        payload = build_chart_payload("gantt", "进度计划", {"tasks": [1], "type": "x"})
        assert payload["type"] == "gantt"
        assert payload["title"] == "进度计划"
        assert payload["tasks"] == [1]


# ---------------------------------------------------------------------------
# E. 规划式正文提示词 + 配图意图注入
# ---------------------------------------------------------------------------
class TestPlannedSystemPrompt:

    def test_planned_prompt_registered(self):
        from app.services.ai.prompts.content import CONTENT_SECTION_SYSTEM_PLANNED
        from app.services.ai.prompts._registry import _ALL_PROMPTS
        assert "CONTENT_SECTION_SYSTEM_PLANNED" in _ALL_PROMPTS
        # 外部标准核心约束逐条可检出
        for kw in (
            "严禁输出 Mermaid",
            "严禁输出图片 Markdown",
            "配图由系统在**全部正文生成完成后**统一执行",
            "表格必须服务于内容表达",
            "不要为了形式硬插",
            "避免与同级章节内容重复",
            "不生成标题",
            "九大章节标准",
        ):
            assert kw in CONTENT_SECTION_SYSTEM_PLANNED, f"缺少：{kw}"

    def test_resolver_switches_on_planned(self):
        from app.services.ai.workflows_sections import _resolve_section_system_prompt
        planned = _resolve_section_system_prompt(planned=True)
        normal = _resolve_section_system_prompt(planned=False)
        assert "图文分离铁律" in planned
        assert "图文分离铁律" not in normal
        assert "【智能配图规范】" in normal  # 既有正本保持不变


class TestPlanIntentInjection:

    def test_plan_intent_included_with_chart(self):
        from app.services.ai.workflows_sections import build_chapter_content_messages
        chapter = {
            "id": "3.1.2", "title": "施工工艺", "level": 3,
            "plan": {
                "table": {"needed": True, "purpose": "归纳工序与责任"},
                "mermaid": {"needed": False, "priority": 0, "reason": "", "title": "", "code": ""},
                "chart": {"needed": True, "priority": 5, "type": "gantt",
                          "title": "施工进度计划", "reason": "r", "data": {}},
                "image": {"needed": False, "priority": 0, "reason": "", "style": "", "title": "", "prompt": ""},
            },
            "illustration_type": "chart",
        }
        msgs = build_chapter_content_messages(chapter)
        text = "\n".join(str(getattr(m, "content", "")) for m in msgs)
        assert "数据型图表: 计划配图（类型：gantt）（图名：施工进度计划）" in text
        assert "表格: 需要（归纳工序与责任）" in text
        assert "不要输出任何图表代码块或图片语法" in text

    def test_mermaid_intent_includes_title(self):
        from app.services.ai.workflows_sections import build_chapter_content_messages
        chapter = {
            "id": "3.1.2", "title": "施工工艺", "level": 3,
            "plan": {
                "table": {"needed": False, "purpose": ""},
                "mermaid": {"needed": True, "priority": 4, "reason": "有流程",
                            "title": "工艺流程图", "code": ""},
                "chart": {"needed": False, "priority": 0, "reason": "", "type": "", "title": "", "data": {}},
                "image": {"needed": False, "priority": 0, "reason": "", "style": "", "title": "", "prompt": ""},
            },
            "illustration_type": "mermaid",
        }
        msgs = build_chapter_content_messages(chapter)
        text = "\n".join(str(getattr(m, "content", "")) for m in msgs)
        assert "Mermaid图: 计划配图（图名：工艺流程图）" in text

    def test_kw_channel_registered(self):
        """illustration_phase_planned 必须登记进 kw_to_ctx 白名单（否则静默丢失）。"""
        import inspect
        from app.services.ai.workflows_sections import generate_sections
        src = inspect.getsource(generate_sections)
        assert '"illustration_phase_planned"' in src
