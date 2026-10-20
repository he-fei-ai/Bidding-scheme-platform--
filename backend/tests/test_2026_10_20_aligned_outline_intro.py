# -*- coding: utf-8 -*-
"""test_2026_10_20_aligned_outline_intro.py — 易标 Aligned/Free 双模式目录生成引入（P0）

覆盖 2026-10-20 引入项：
1. `augment_aligned_l1`：易标 OutlineItem 扩展字段回填（幂等 / 不覆盖已有值）；
2. `_generate_level1_v51` Aligned 分支：程序构造 L1、零 AI 调用、字段齐全；
3. Aligned 回落：评分项为空时自动走 AI 生成路径（不阻断链路）；
4. Free 模式（默认）：行为与历史完全一致（aligned 分支不触发）；
5. `normalize_outline_items`：扩展字段透传不丢失（向后兼容映射）。
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import config as app_config
from app.services.ai import workflows_outline_v51 as v51
from app.services.ai import workflows_sections as sections_mod
from app.services.ai.outline_alignment import augment_aligned_l1
from app.services.ai.providers.base import AIMessage
from app.services.ai.workflows_orchestration import _build_knowledge_catalog_text
from app.services.ai.workflows_outline_utils import normalize_outline_items


# ---------------------------------------------------------------------------
# 1. augment_aligned_l1 — 扩展字段回填
# ---------------------------------------------------------------------------

class TestAugmentAlignedL1:

    def test_fills_default_fields(self):
        """无扩展字段的 L1 节点回填 R{n}/ai-generate/locked。"""
        l1 = [{"id": "1", "title": "工程概况", "description": "x"},
              {"id": "2", "title": "施工计划", "description": "y"}]
        out = augment_aligned_l1(l1)
        assert out[0]["source_requirement_id"] == "R1"
        assert out[1]["source_requirement_id"] == "R2"
        assert out[0]["source_requirement_title"] == "工程概况"
        assert all(n["content_mode"] == "ai-generate" for n in out)
        assert all(n["locked"] is True for n in out)

    def test_idempotent_and_no_override(self):
        """幂等：已有值不覆盖（允许调用方预先定制）。"""
        l1 = [{"title": "A", "source_requirement_id": "S9", "content_mode": "template-fill"}]
        augment_aligned_l1(l1)
        out = augment_aligned_l1(l1)
        assert out[0]["source_requirement_id"] == "S9"
        assert out[0]["content_mode"] == "template-fill"
        assert out[0]["locked"] is True

    def test_tolerates_none_and_bad_items(self):
        """None / 非 dict 元素不崩溃（与全仓防御口径一致）。"""
        assert augment_aligned_l1(None) == []
        assert augment_aligned_l1([None, "x", {"title": "ok"}])[2]["source_requirement_id"] == "R3"


# ---------------------------------------------------------------------------
# 测试用假 provider：被调用即记录（用于断言 aligned 分支零 AI 调用）
# ---------------------------------------------------------------------------

class _RecordingProvider:
    def __init__(self, reply):
        self._reply = reply
        self.calls = 0

    async def chat(self, messages, **kwargs):
        self.calls += 1
        return self._reply


def _sp_text(n: int = 3) -> str:
    return json.dumps(
        [{"id": f"R{i+1}", "title": f"评分项{i+1}工艺要求", "standard": f"要点{i+1}"} for i in range(n)],
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# 2/3/4. _generate_level1_v51 的 aligned / free 分支
# ---------------------------------------------------------------------------

class TestGenerateLevel1Aligned:

    def test_aligned_builds_l1_without_ai(self, monkeypatch):
        """aligned 模式：程序构造 L1，provider.chat 一次都不调用。"""
        monkeypatch.setattr(v51, "OUTLINE_ALIGNED_MODE", "aligned")
        provider = _RecordingProvider("[]")
        level1 = asyncio.run(
            v51._generate_level1_v51(provider, "某医院装修工程概述", _sp_text(3), "[]")
        )
        assert provider.calls == 0
        assert len(level1) == 3
        assert [n["title"] for n in level1] == [f"评分项{i+1}工艺要求" for i in range(3)]
        # P0-2 扩展字段在 L1 上生效
        assert all(n.get("source_requirement_id") == f"R{i+1}" for i, n in enumerate(level1))
        assert all(n.get("content_mode") == "ai-generate" for n in level1)
        assert all(n.get("locked") is True for n in level1)
        assert all(isinstance(n.get("children"), list) for n in level1)

    def test_aligned_no_scoring_builds_nine_chapter_l1(self, monkeypatch):
        """aligned 模式无评分项：按专项方案九大章节标准程序构造（适配层，
        任务规格四.1），不调 AI、顺序锁定。"""
        monkeypatch.setattr(v51, "OUTLINE_ALIGNED_MODE", "aligned")
        provider = _RecordingProvider("[]")
        level1 = asyncio.run(
            v51._generate_level1_v51(provider, "某某医院精装修工程概述", "", "[]")
        )
        assert provider.calls == 0
        assert [n["title"] for n in level1] == [
            "工程概况", "编制依据", "施工计划", "施工工艺技术", "安全保证措施",
            "人员配备", "验收要求", "应急处置措施", "计算书与图纸",
        ]
        assert all(n["locked"] is True for n in level1)

    def test_aligned_scoring_construct_insufficient_falls_back_to_ai(self, monkeypatch):
        """aligned 模式有评分项但程序构造不足：回落 AI 生成（不阻断链路）。

        直接 stub 构造器返回 1 章（期望 3 章，验收阈值 2）触发不足分支。
        """
        monkeypatch.setattr(v51, "OUTLINE_ALIGNED_MODE", "aligned")
        monkeypatch.setattr(
            v51, "_build_l1_from_scoring_items",
            lambda sp, expected: [{"id": "1", "title": "只有一个章节", "description": "d"}],
        )
        provider = _RecordingProvider('[{"id":"1","title":"工程概况","description":"d"},{"id":"2","title":"施工方案","description":"d"},{"id":"3","title":"安全措施","description":"d"}]')
        level1 = asyncio.run(
            v51._generate_level1_v51(provider, "概述", _sp_text(3), "[]")
        )
        assert provider.calls >= 1  # 回落 AI 路径
        assert level1

    def test_free_mode_default_unchanged(self, monkeypatch):
        """默认 free 模式：aligned 分支不触发，历史 AI 路径行为不变。

        假 AI 返回与评分项等量的 3 章，确保走纯 AI 路径（不触发数量校验
        失败后的评分项兜底）——断言 AI 产物不带 aligned 扩展字段。
        """
        monkeypatch.setattr(v51, "OUTLINE_ALIGNED_MODE", "free")
        provider = _RecordingProvider(json.dumps([
            {"id": "1", "title": "工程概况", "description": "d"},
            {"id": "2", "title": "施工方案", "description": "d"},
            {"id": "3", "title": "安全措施", "description": "d"},
        ], ensure_ascii=False))
        level1 = asyncio.run(
            v51._generate_level1_v51(provider, "概述", _sp_text(3), "[]")
        )
        assert provider.calls >= 1  # free 模式必须走 AI
        assert level1 and "source_requirement_id" not in level1[0]


# ---------------------------------------------------------------------------
# 5. normalize_outline_items 透传扩展字段（向后兼容映射）
# ---------------------------------------------------------------------------

class TestNormalizePreservesExtendedFields:

    def test_extended_fields_survive_normalization(self):
        outline = [{
            "id": "1", "title": "工程概况",
            "source_requirement_id": "R1", "content_mode": "ai-generate", "locked": True,
            "children": [{"id": "1.1", "title": "项目背景"}],
        }]
        out = normalize_outline_items(outline)
        assert out[0]["source_requirement_id"] == "R1"
        assert out[0]["content_mode"] == "ai-generate"
        assert out[0]["locked"] is True
        # 子节点未带扩展字段也不报错（旧数据兼容）
        assert out[0]["children"][0]["title"] == "项目背景"

    def test_old_data_without_extended_fields(self):
        """旧数据（无扩展字段）归一化结果不含新键，导出链路不感知。"""
        out = normalize_outline_items([{"id": "1", "title": "概述", "children": []}])
        assert "content_mode" not in out[0]
        assert "source_requirement_id" not in out[0]


# ---------------------------------------------------------------------------
# 6. 截断长度配置化（规格二.5）：默认值 = 历史硬编码，行为不变
# ---------------------------------------------------------------------------

class TestTruncationConfigurable:

    def test_default_values_match_history(self):
        assert app_config.KNOWLEDGE_CATALOG_TITLE_MAX == 60
        assert app_config.KNOWLEDGE_CATALOG_SUMMARY_MAX == 120
        assert app_config.OUTLINE_REFERENCE_JSON_MAX_CHARS == 3000

    def test_catalog_text_uses_config_values(self, monkeypatch):
        """配置调小时目录文本按新值截断；默认值下与历史输出一致。"""
        catalog = [{"id": 1, "title": "标" * 100, "summary": "摘" * 300}]
        base = _build_knowledge_catalog_text(catalog)
        assert base == "K1: " + "标" * 60 + " — " + "摘" * 120
        monkeypatch.setattr(app_config, "KNOWLEDGE_CATALOG_TITLE_MAX", 30, raising=False)
        import app.services.ai.workflows_orchestration as orch
        # orchestration 模块是顶层 import 绑定，需同时 patch 模块属性
        monkeypatch.setattr(orch, "KNOWLEDGE_CATALOG_TITLE_MAX", 30)
        monkeypatch.setattr(orch, "KNOWLEDGE_CATALOG_SUMMARY_MAX", 120)
        out = orch._build_knowledge_catalog_text(catalog)
        assert out == "K1: " + "标" * 30 + " — " + "摘" * 120


# ---------------------------------------------------------------------------
# 7. 长上下文降级（规格三.7）
# ---------------------------------------------------------------------------

class TestDegradeLongContext:

    def _msgs(self, user_len: int):
        return [
            AIMessage(role="system", content="铁律" * 50),
            AIMessage(role="user", content="资" * user_len),
        ]

    def test_under_threshold_untouched(self, monkeypatch):
        monkeypatch.setattr(app_config, "CONTENT_CONTEXT_LIMIT_CHARS", 100000)
        monkeypatch.setattr(app_config, "CONTENT_CONTEXT_DEGRADE_RATIO", 0.7)
        msgs = self._msgs(1000)
        out = sections_mod._degrade_long_context_messages(msgs, {"id": "1"})
        assert out[1].content == msgs[1].content  # 未裁剪

    def test_over_threshold_truncates_user_keeps_system(self, monkeypatch):
        monkeypatch.setattr(app_config, "CONTENT_CONTEXT_LIMIT_CHARS", 2000)
        monkeypatch.setattr(app_config, "CONTENT_CONTEXT_DEGRADE_RATIO", 0.7)
        section = {"id": "9"}
        msgs = self._msgs(5000)
        sys_before = msgs[0].content
        out = sections_mod._degrade_long_context_messages(msgs, section)
        total = sum(len(m.content) for m in out)
        assert total <= int(2000 * 0.7) + 64  # 回到阈值内（允许标记尾巴）
        assert out[0].content == sys_before     # system 不动
        assert "已截断" in out[1].content        # 截断标记
        assert section["_context_degraded"] is True

    def test_dict_messages_supported(self, monkeypatch):
        monkeypatch.setattr(app_config, "CONTENT_CONTEXT_LIMIT_CHARS", 2000)
        monkeypatch.setattr(app_config, "CONTENT_CONTEXT_DEGRADE_RATIO", 0.7)
        msgs = [
            {"role": "system", "content": "铁律" * 50},
            {"role": "user", "content": "资" * 5000},
        ]
        out = sections_mod._degrade_long_context_messages(msgs, {"id": "1"})
        assert len(out[1]["content"]) < 5000

    def test_degrade_disabled_by_default(self):
        assert app_config.CONTENT_CONTEXT_DEGRADE_ENABLED is False
        assert 0 < app_config.CONTENT_CONTEXT_DEGRADE_RATIO <= 1


# ---------------------------------------------------------------------------
# 8. 配图编排同类意图去重（规格三.2，易标标题归一化去重对齐）
# ---------------------------------------------------------------------------

class TestIllustrationDedup:

    def test_same_reason_mermaid_kept_once(self):
        from app.services.ai.workflows_orchestration import _adjudicate_illustrations
        # 两个章节同一配图意图（标点/空格差异归一后相同），section_per_segment=1
        # 时每章占一段，旧逻辑会各裁一张 mermaid；去重后仅保留 priority 高的。
        plans = [
            {"section_id": "1", "plan": {"mermaid": {"needed": True, "priority": 3, "reason": "施工流程图，表达工序"}}},
            {"section_id": "2", "plan": {"mermaid": {"needed": True, "priority": 5, "reason": " 施工流程图 表达工序"}}},
        ]
        out = _adjudicate_illustrations(plans, max_ai_images=0, section_per_segment=1, prefer_mermaid=True)
        types = [p["illustration_type"] for p in out]
        assert types.count("mermaid") == 1
        assert types[1] == "mermaid"  # 高 priority（第2章）胜出保留

    def test_different_reason_both_kept(self):
        from app.services.ai.workflows_orchestration import _adjudicate_illustrations
        plans = [
            {"section_id": "1", "plan": {"mermaid": {"needed": True, "priority": 3, "reason": "施工工艺流程"}}},
            {"section_id": "2", "plan": {"mermaid": {"needed": True, "priority": 4, "reason": "组架构组职责划分"}}},
        ]
        out = _adjudicate_illustrations(plans, max_ai_images=0, section_per_segment=1, prefer_mermaid=True)
        assert [p["illustration_type"] for p in out] == ["mermaid", "mermaid"]

    def test_empty_reason_not_deduped(self):
        """无 reason 描述的候选不参与去重（保持历史行为）。"""
        from app.services.ai.workflows_orchestration import _adjudicate_illustrations
        plans = [
            {"section_id": "1", "plan": {"mermaid": {"needed": True, "priority": 3}}},
            {"section_id": "2", "plan": {"mermaid": {"needed": True, "priority": 3}}},
        ]
        out = _adjudicate_illustrations(plans, max_ai_images=0, section_per_segment=1, prefer_mermaid=True)
        assert [p["illustration_type"] for p in out] == ["mermaid", "mermaid"]


# ---------------------------------------------------------------------------
# 9. 专项方案九大章节程序构造（适配层，任务规格四.1）
# ---------------------------------------------------------------------------

class TestNineChapterStandard:

    NINE_TITLES = [
        "工程概况", "编制依据", "施工计划", "施工工艺技术", "安全保证措施",
        "人员配备", "验收要求", "应急处置措施", "计算书与图纸",
    ]

    def test_build_nine_chapter_l1_structure(self):
        from app.services.ai.outline_alignment import build_nine_chapter_l1
        l1 = build_nine_chapter_l1()
        assert [n["title"] for n in l1] == self.NINE_TITLES
        assert [n["id"] for n in l1] == [str(i) for i in range(1, 10)]
        assert all(n["locked"] is True for n in l1)
        assert all(n["content_mode"] == "ai-generate" for n in l1)
        assert all(n["source_requirement_id"] == f"S{i}" for i, n in enumerate(l1, 1))
        assert all(isinstance(n["children"], list) and not n["children"] for n in l1)
        assert all(n["description"] for n in l1)

    def test_zone_violation_would_exist_without_protection(self):
        """九大章节顺序按 gate 分区判据确会检出倒置（验证 locked 保护的必要性）。"""
        from app.services.ai import outline_gate
        seq = [
            {"id": str(i), "path": str(i), "title": t, "zone": outline_gate.infer_chapter_zone(t)}
            for i, t in enumerate(self.NINE_TITLES, 1)
        ]
        # 「验收要求」(收尾) 排在「应急处置措施」(专项预案) 之前 → 至少 1 处倒置
        assert outline_gate.detect_zone_order_violations(seq)


# ---------------------------------------------------------------------------
# 10. BUG-FIX 2026-10-20（P0）：aligned 溢出合并 + 确定性覆盖校验
#     背景：评分项超过 OUTLINE_L1_MAX_CHAPTERS 时构造器按 items[:cap] 截断，
#     溢出项静默丢失且 L1 锁定无法下游补回 → 一级目录未覆盖方案类型。
# ---------------------------------------------------------------------------

class TestAlignedOverflowMergeAndCoverage:

    def _sp_items(self, n: int) -> list[dict]:
        return [
            {"id": f"R{i + 1}", "title": f"评分项{i + 1}技术要求", "weight": "5"}
            for i in range(n)
        ]

    def test_merge_overflow_into_last_chapter(self, monkeypatch):
        """溢出评分项合并进最后一章：description 记清单 + merged_requirement_ids 记 id。"""
        merged = v51._merge_overflow_scoring_into_last_l1(
            [{"id": "1", "title": "甲", "description": "甲说明"}],
            self._sp_items(4),
            1,
        )
        assert merged == 3
        node = {"id": "1", "title": "甲", "description": "甲说明"}
        v51._merge_overflow_scoring_into_last_l1([node], self._sp_items(4), 1)
        assert "评分项2技术要求" in node["description"]
        assert "评分项4技术要求" in node["description"]
        assert node["merged_requirement_ids"] == ["R2", "R3", "R4"]

    def test_merge_noop_within_cap(self):
        """未超上限：不修改任何数据、返回 0。"""
        node = {"id": "1", "title": "甲", "description": "d"}
        l1 = [node, {"id": "2", "title": "乙", "description": "d"}]
        assert v51._merge_overflow_scoring_into_last_l1(l1, self._sp_items(2), 2) == 0
        assert "merged_requirement_ids" not in node
        assert node["description"] == "d"

    def test_merge_handles_str_items_and_dict_wrapper(self):
        """裸字符串评分项与 {"items": [...]} 包装形态都不崩溃。"""
        l1 = [{"id": "1", "title": "甲", "description": ""}]
        assert v51._merge_overflow_scoring_into_last_l1(l1, ["评分项A", "评分项B"], 1) == 1
        assert "评分项B" in l1[0]["description"]
        # 字符串无 id，不写 merged_requirement_ids 键（不产生空列表噪声）
        assert "merged_requirement_ids" not in l1[0]
        wrapped = {"items": self._sp_items(3)}
        l2 = [{"id": "1", "title": "甲", "description": ""}]
        assert v51._merge_overflow_scoring_into_last_l1(l2, wrapped, 1) == 2
        assert l2[0]["merged_requirement_ids"] == ["R2", "R3"]

    def test_aligned_overflow_full_flow(self, monkeypatch):
        """端到端：20 个评分项 + 上限 15 → 15 章、末章合并 5 项、全部 id 可覆盖。"""
        monkeypatch.setattr(v51, "OUTLINE_ALIGNED_MODE", "aligned")
        monkeypatch.setattr(v51, "OUTLINE_L1_MAX_CHAPTERS", 15)
        provider = _RecordingProvider("[]")
        sp_text = json.dumps(self._sp_items(20), ensure_ascii=False)
        level1 = asyncio.run(
            v51._generate_level1_v51(provider, "某工程概述", sp_text, "[]")
        )
        assert provider.calls == 0  # aligned 程序构造，零 AI 调用
        assert len(level1) == 15
        last = level1[-1]
        assert last["merged_requirement_ids"] == [f"R{i}" for i in range(16, 21)]
        assert "评分项20技术要求" in last["description"]
        # 全部 20 个评分项 id 都能在某个一级章节上追溯到（覆盖不断链）
        covered: set[str] = set()
        for n in level1:
            covered.add(str(n.get("source_requirement_id") or ""))
            covered.update(str(m) for m in n.get("merged_requirement_ids") or [])
        assert {f"R{i}" for i in range(1, 21)} <= covered

    def test_aligned_carries_real_requirement_ids(self, monkeypatch):
        """构造器节点携带评分项真实 id（非位置编号），salvage/缺序场景不错位。"""
        items = [
            {"id": "SP-7", "name": "施工组织设计"},
            {"id": "SP-9", "name": "安全保证措施"},
        ]
        built = v51._build_l1_from_scoring_items(items, 2)
        assert [n["source_requirement_id"] for n in built] == ["SP-7", "SP-9"]
        # 无 id 的裸字符串项不写 source_requirement_id（由 augment 位置兜底）
        built_str = v51._build_l1_from_scoring_items(["甲", "乙"], 2)
        assert all("source_requirement_id" not in n for n in built_str)

    def test_aligned_coverage_check_warns_on_missing(self, monkeypatch, caplog):
        """构造结果漏项时：error 日志 + phase 事件双通道告警（不阻断、不静默）。"""
        monkeypatch.setattr(v51, "OUTLINE_ALIGNED_MODE", "aligned")
        # stub 构造器：3 个评分项只返回 2 章且只关联 R1/R2 → R3 漏项
        monkeypatch.setattr(
            v51, "_build_l1_from_scoring_items",
            lambda sp, expected: [
                {"id": "1", "title": "甲", "description": "d", "source_requirement_id": "R1"},
                {"id": "2", "title": "乙", "description": "d", "source_requirement_id": "R2"},
            ],
        )
        events: list[tuple[str, str]] = []

        async def _phase(stage, message):
            events.append((stage, message))

        provider = _RecordingProvider("[]")
        with caplog.at_level("ERROR"):
            level1 = asyncio.run(
                v51._generate_level1_v51(
                    provider, "概述", _sp_text(3), "[]", phase_callback=_phase
                )
            )
        assert len(level1) == 2  # 仍返回构造结果（不阻断链路）
        assert any("R3" in r.message and "覆盖校验失败" in r.message for r in caplog.records)
        assert any("R3" in msg for _, msg in events)  # phase 事件同步告警


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
