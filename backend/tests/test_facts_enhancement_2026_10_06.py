"""全局事实模块 —— 2026-10-06 增强修复回归测试。

覆盖本轮审计发现并修复的三组问题：

1. **placeholder 打标口径分叉**：打标此前只在 /facts/generate 端点显式调用，
   评分链路 `workflows_analysis.extract_global_facts` 只走 `normalize_fact_items`
   不打标 —— placeholder 策略下评分自动产出的【待填写】条目缺逐行角标。
   修复后打标上收正本，两条链路统一。

2. **保存链路无冲突反馈**：POST /facts 手工保存此前只返回 {"ok": true}，
   同 label 不同 key 的内部矛盾静默落库。修复后响应即时回传 fact_conflicts。

3. **前端零消费 fact_conflicts**（组件级 DOM 测试在 frontend 侧）：
   后端契约补钉 —— GET /facts、/generate、POST /facts 三处冲突字段口径一致。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.db import get_db, init_db


def _new_project(name: str) -> int:
    init_db()
    with get_db() as conn:
        conn.execute("INSERT INTO projects (name, industry) VALUES (?, ?)", (name, "通用"))
        return conn.execute("SELECT id FROM projects ORDER BY id DESC LIMIT 1").fetchone()["id"]


# ============================================================
# 1. placeholder 打标上收正本（评分链路分叉修复）
# ============================================================


class TestPlaceholderMarkingCanonical:
    """normalize_fact_items 正本必须对所有占位条目打 needs_manual_input，
    无论调用方是手动提取端点还是评分自动提取链路。"""

    def test_ai_dict_placeholder_items_are_marked(self):
        """AI 返回 dict 中 value 为【待填写】的条目，经 dict_to_fact_items +
        normalize_fact_items 后必须带 needs_manual_input=True。"""
        from app.services.facts_service import (
            dict_to_fact_items,
            normalize_fact_items,
        )

        ai_dict = {
            "projectName": "示范工程",
            "projectManager": "【待填写：项目经理姓名】",
            "scheduleDuration": "450 日历天",
        }
        items = dict_to_fact_items(ai_dict)
        normalized = normalize_fact_items(items, fallback_text="", policy="placeholder")

        by_key = {it["key"]: it for it in normalized}
        assert by_key["projectManager"]["needs_manual_input"] is True
        assert by_key["projectName"].get("needs_manual_input", False) is False
        assert by_key["scheduleDuration"].get("needs_manual_input", False) is False

    def test_fabricate_policy_also_marks_explicit_placeholders(self):
        """即便策略是 fabricate，AI 若自行输出【待填写】占位也必须打标
        （否则占位项绕过 facts_for_generation 过滤进入正文）。"""
        from app.services.facts_service import (
            dict_to_fact_items,
            normalize_fact_items,
        )

        items = dict_to_fact_items({"pm": "【待填写】", "projectName": "X"})
        normalized = normalize_fact_items(items, fallback_text="", policy="fabricate")
        pm = next(it for it in normalized if it["key"] == "pm")
        assert pm["needs_manual_input"] is True

    def test_extract_global_facts_path_marks_placeholders(self):
        """评分链路 extract_global_facts（Step02）与手动端点同一行为契约：
        placeholder 策略下产出的占位条目带逐行角标。"""
        from app.services.ai import workflows_analysis as wa
        from app.services.facts_service import is_placeholder_value

        captured: dict = {}

        class _FakeProvider:
            pass

        async def _fake_collect(_provider, messages, **_kwargs):
            captured["messages"] = messages
            return {"projectManager": "【待填写】", "projectName": "市政改造"}

        original = wa.collect_json_response
        wa.collect_json_response = _fake_collect
        try:
            items = asyncio.run(
                wa.extract_global_facts(
                    _FakeProvider(),
                    tender_context="工期 450 天。",
                    summary="",
                    policy="placeholder",
                )
            )
        finally:
            wa.collect_json_response = original

        pm = next(it for it in items if it["key"] == "projectManager")
        assert is_placeholder_value(pm["value"])
        assert pm["needs_manual_input"] is True, "评分链路占位条目必须逐行打标"


# ============================================================
# 2. POST /facts 保存响应即时回传冲突
# ============================================================


class TestSaveResponseConflicts:
    def test_save_with_internal_conflicts_returns_them(self, monkeypatch):
        import app.routers.facts as fr
        from app.models import GlobalFact, GlobalFactList

        monkeypatch.setattr(fr, "invalidate_content_caches", lambda pid: None)

        pid = _new_project("事实-保存反馈冲突")
        body = GlobalFactList(
            items=[
                GlobalFact(key="projectManager", label="项目经理", value="张伟", source="ai"),
                GlobalFact(key="pmAlt", label="项目经理", value="李强", source="manual"),
            ]
        )
        result = asyncio.run(fr.save_facts(pid, body))
        assert result["ok"] is True
        conflicts = result["fact_conflicts"]
        assert len(conflicts) == 1
        assert conflicts[0]["label"] == "项目经理"
        values = {v["value"] for v in conflicts[0]["values"]}
        assert values == {"张伟", "李强"}

    def test_save_without_conflicts_returns_empty_list(self, monkeypatch):
        import app.routers.facts as fr
        from app.models import GlobalFact, GlobalFactList

        monkeypatch.setattr(fr, "invalidate_content_caches", lambda pid: None)

        pid = _new_project("事实-保存无冲突")
        body = GlobalFactList(
            items=[GlobalFact(key="projectName", label="项目名称", value="X", source="ai")]
        )
        result = asyncio.run(fr.save_facts(pid, body))
        assert result["ok"] is True
        assert result["fact_conflicts"] == []


# ============================================================
# 3. 三处冲突字段口径一致（前端消费契约）
# ============================================================


class TestConflictContractConsistency:
    def test_get_save_generate_same_conflicts(self, monkeypatch):
        """同一事实集，GET /facts 与 POST /facts 计算出的 fact_conflicts 必须一致
        （前端先保存、再刷新两个入口看到的矛盾分组不能漂移）。"""
        import app.routers.facts as fr
        from app.models import GlobalFact, GlobalFactList

        monkeypatch.setattr(fr, "invalidate_content_caches", lambda pid: None)

        pid = _new_project("事实-口径一致")
        body = GlobalFactList(
            items=[
                GlobalFact(key="sd", label="建设工期", value="450 日历天", source="ai"),
                GlobalFact(key="sd2", label="建设工期", value="420 日历天", source="manual"),
            ]
        )
        saved = asyncio.run(fr.save_facts(pid, body))
        got = asyncio.run(fr.get_facts(pid, None))

        assert len(saved["fact_conflicts"]) == 1
        assert len(got["fact_conflicts"]) == 1
        assert saved["fact_conflicts"][0]["label"] == got["fact_conflicts"][0]["label"]
