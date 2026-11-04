"""一致性校核 —— F-2 AI 语义发现分流与修复上下文（2026-11-02）。

覆盖方案《方案_一致性校核模块深度审计与修复_2026-11-02》F-2：

`consistency_conflicts` 表同时承载两类语义完全不同的行——
确定性规则冲突（`conflict_*`，有结构化 `values_json`，可被补丁收敛）与
AI 语义审计发现（`AI-*`，`values_json` 恒为 `'[]'`，无法自动收敛）。
此前 AI 补丁修复与 Agent 修复把两类**混在同一个 `conflicts_text`** 里喂给
模型：① 白占 token；② 模型被诱导为无取值的条目硬编补丁；③
`refresh_conflict_states` 里这些条目每轮都计入「仍待处理」，报告恒显
「仍待处理 N 条」，用户反复点修复反复看到"没修完"。

修复后的口径（本文件钉住的被测行为）：
  - `split_rule_and_ai`：按 `AI-` 前缀（`rules.is_ai_conflict_id` 单一事实来源）拆分；
  - `consistency_repair._load_context`：返回 `{conflicts(规则), ai_findings(AI)}`；
  - `build_agent_workspace`：同口径拆分，AI 发现只进"仅参考"文本区；
  - `build_repair_user_prompt(ai_findings_text=...)`：非空才追加参考区，
    为空时提示词与历史**逐字一致**（保护提示词前缀顺序与缓存命中）；
  - `repair_with_ai`：`conflicts_used` 收窄为规则条数、新增 `ai_findings_used`；
    只剩 AI 发现时**早退不调 AI**，明确提示走人工核对。

设计原则：不调用真实 AI；`generate_patches` 用 monkeypatch 桩替换。
"""
from __future__ import annotations

import asyncio

import pytest

from app.db import get_db, init_db
from app.services import consistency_repair as repair_mod
from app.services import consistency_rules as rules
from app.services.agent_repair import build_agent_workspace
from app.services.consistency_repair import (
    _load_context,
    build_ai_findings_text,
    build_repair_user_prompt,
    repair_with_ai,
    split_rule_and_ai,
)


OWNER = 3510


def _new_project(name: str = "AI发现分流测试") -> int:
    init_db()
    with get_db() as conn:
        conn.execute(
            "INSERT INTO projects (name, industry, created_by) VALUES (?, ?, ?)",
            (name, "通用", OWNER),
        )
        return conn.execute("SELECT id FROM projects ORDER BY id DESC LIMIT 1").fetchone()["id"]


def _seed_section(project_id: int, *, title: str, content: str, sort_order: int = 0) -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO sections (project_id, title, level, sort_order, content, status, section_type) "
            "VALUES (?, ?, 1, ?, ?, 'completed', 'normal')",
            (project_id, title, sort_order, content),
        )
        return cur.lastrowid


def _seed_conflict(
    project_id: int,
    *,
    conflict_id: str,
    fact_name: str = "工期",
    values_json: str = '["45","50"]',
    description: str = "测试冲突",
) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO consistency_conflicts "
            "(project_id, conflict_id, fact_name, values_json, sources_json, "
            " severity, description, resolved, created_at) "
            "VALUES (?, ?, ?, ?, '[\"summary\"]', 'medium', ?, 0, '2026-11-02 10:00:00')",
            (project_id, conflict_id, fact_name, values_json, description),
        )


def _ai_id(suffix: str) -> str:
    return f"{rules.AI_CONFLICT_ID_PREFIX}{suffix}"


# ============================================================================
# split_rule_and_ai —— 拆分口径本身
# ============================================================================


class TestSplitRuleAndAi:
    def test_dict_items_split_by_ai_prefix(self):
        items = [
            {"conflict_id": "conflict_工期_001", "fact_name": "工期", "values": ["45", "50"]},
            {"conflict_id": _ai_id("0001"), "fact_name": "表述前后不一致", "values": []},
        ]
        rule, ai = split_rule_and_ai(items)
        assert [r["conflict_id"] for r in rule] == ["conflict_工期_001"]
        assert [a["conflict_id"] for a in ai] == [_ai_id("0001")]

    def test_object_items_supported(self):
        from app.services.consistency_auditor import Conflict

        objs = [
            Conflict(
                conflict_id="conflict_x", fact_name="工期", values=["1", "2"],
                sources=[], severity="high", description="d",
            ),
            Conflict(
                conflict_id=_ai_id("x"), fact_name="语义", values=[],
                sources=[], severity="medium", description="d",
            ),
        ]
        rule, ai = split_rule_and_ai(objs)
        assert len(rule) == 1 and len(ai) == 1

    def test_defensive_unknown_shapes_go_to_rule_side(self):
        """非 dict 且无 conflict_id 属性的元素归规则侧（宁可当可处理项，
        也不误标成语义发现而从修复上下文消失）。"""
        class NoId:
            pass

        rule, ai = split_rule_and_ai([NoId(), {"conflict_id": None}, "plain-string"])
        assert len(rule) == 3
        assert ai == []

    def test_empty_and_none_input(self):
        assert split_rule_and_ai(None) == ([], [])
        assert split_rule_and_ai([]) == ([], [])


# ============================================================================
# build_ai_findings_text / build_repair_user_prompt —— 提示词参考区
# ============================================================================


class TestPromptReferenceSection:
    def test_ai_findings_text_renders_without_values(self):
        text = build_ai_findings_text([
            {"fact_name": "工期表述", "description": "前后表述的工期含义可能不一致"},
        ])
        assert "工期表述" in text
        assert "前后表述的工期含义可能不一致" in text

    def test_ai_findings_text_empty_input_returns_placeholder(self):
        assert build_ai_findings_text([]) == "未提供"

    def test_prompt_without_ai_findings_is_verbatim_legacy_shape(self):
        """`ai_findings_text` 为空时提示词与历史逐字一致（不追加任何区块，
        保护提示词前缀顺序与缓存命中）。"""
        sections = [{"id": 1, "title": "t", "content": "正文"}]
        p_default = build_repair_user_prompt(
            sections=sections, conflicts_text="- 工期：45, 50（d）", facts_text="- 工期：50",
        )
        p_empty = build_repair_user_prompt(
            sections=sections, conflicts_text="- 工期：45, 50（d）", facts_text="- 工期：50",
            ai_findings_text="",
        )
        assert p_default == p_empty
        assert "仅供参考" not in p_default

    def test_prompt_appends_reference_block_when_findings_given(self):
        sections = [{"id": 1, "title": "t", "content": "正文"}]
        prompt = build_repair_user_prompt(
            sections=sections,
            conflicts_text="- 工期：45, 50（d）",
            facts_text="- 工期：50",
            ai_findings_text="- 语义发现甲：表述可能不一致",
        )
        assert "仅供参考" in prompt
        assert "语义发现甲" in prompt
        # 参考区追加在基础提示词之后（前缀逐字保持）
        base = build_repair_user_prompt(
            sections=sections, conflicts_text="- 工期：45, 50（d）", facts_text="- 工期：50",
        )
        assert prompt.startswith(base)


# ============================================================================
# _load_context / build_agent_workspace —— 两条修复链路的拆分口径
# ============================================================================


class TestContextSplit:
    def _seed_mixed(self, name: str) -> int:
        pid = _new_project(name)
        _seed_section(pid, title="总体", content="本项目建设工期：45 天。")
        _seed_conflict(pid, conflict_id=f"c_rule_{pid}", fact_name="工期")
        _seed_conflict(
            pid, conflict_id=_ai_id(f"c_ai_{pid}"), fact_name="工序衔接表述不一",
            values_json="[]", description="两处工序衔接描述疑似矛盾",
        )
        return pid

    def test_load_context_splits_rule_and_ai(self):
        pid = self._seed_mixed("F2-load_context")
        ctx = _load_context(pid, None)
        rule_ids = [c["conflict_id"] for c in ctx["conflicts"]]
        ai_ids = [c["conflict_id"] for c in ctx["ai_findings"]]
        assert rule_ids == [f"c_rule_{pid}"]
        assert ai_ids == [_ai_id(f"c_ai_{pid}")]

    def test_agent_workspace_splits_and_provides_reference_text(self):
        pid = self._seed_mixed("F2-agent_workspace")
        ws = build_agent_workspace(pid, base=None)
        assert [c["conflict_id"] for c in ws["conflicts"]] == [f"c_rule_{pid}"]
        assert [c["conflict_id"] for c in ws["ai_findings"]] == [_ai_id(f"c_ai_{pid}")]
        assert "工序衔接表述不一" in ws["ai_findings_text"]
        # 规则冲突不再混进参考区
        assert f"c_rule_{pid}" not in ws["ai_findings_text"]


# ============================================================================
# repair_with_ai —— 计数口径、补丁上下文、只剩 AI 发现时早退
# ============================================================================


class TestRepairWithAiRouting:
    def test_only_ai_findings_early_exit_without_calling_ai(self, monkeypatch):
        """无规则冲突、只剩 AI 发现：不调 AI，早退并明确提示人工核对。"""
        pid = _new_project("F2-早退")
        _seed_section(pid, title="总体", content="本项目建设工期：45 天。")
        _seed_conflict(
            pid, conflict_id=_ai_id(f"only_{pid}"), fact_name="语义发现",
            values_json="[]",
        )

        def _boom(*args, **kwargs):  # 被调用即失败
            raise AssertionError("早退分支不应调用 generate_patches")

        monkeypatch.setattr(repair_mod, "generate_patches", _boom)
        report = asyncio.run(repair_with_ai(pid, provider=object()))

        assert report["success"] is False
        assert report["conflicts_used"] == 0
        assert report["ai_findings_used"] == 1
        assert report["patches"] == []
        assert "没有可自动修复的规则冲突" in report["message"]
        assert "1 条 AI 语义发现" in report["message"]

    def test_conflicts_counts_narrowed_and_prompt_split(self, monkeypatch):
        """混合场景：`conflicts_used` 只算规则条数；送进 generate_patches 的
        `conflicts_text` 不含 AI 发现，AI 发现只走 `ai_findings_text` 参考区。"""
        pid = _new_project("F2-上下文拆分")
        _seed_section(pid, title="总体", content="本项目建设工期：45 天。")
        _seed_conflict(pid, conflict_id=f"cnt_rule_{pid}", fact_name="工期")
        _seed_conflict(
            pid, conflict_id=_ai_id(f"cnt_ai_{pid}"), fact_name="只有AI可见的标记词",
            values_json="[]", description="AI独有的描述标记XYZ",
        )

        calls: list[dict] = []

        async def fake_generate(provider, *, sections, conflicts_text, facts_text,
                               failure_note="", ai_findings_text=""):
            calls.append({
                "conflicts_text": conflicts_text,
                "ai_findings_text": ai_findings_text,
                "n_sections": len(sections),
            })
            return [], [], "桩：不产出补丁"

        monkeypatch.setattr(repair_mod, "generate_patches", fake_generate)
        report = asyncio.run(repair_with_ai(pid, provider=object()))

        assert report["conflicts_used"] == 1
        assert report["ai_findings_used"] == 1
        assert len(calls) >= 1
        for call in calls:
            assert "工期" in call["conflicts_text"]
            # AI 发现彻底不混进规则冲突区（旧行为回归钉）
            assert "只有AI可见的标记词" not in call["conflicts_text"]
            assert "AI独有的描述标记XYZ" not in call["conflicts_text"]
            assert "只有AI可见的标记词" in call["ai_findings_text"]
        assert "AI 未产出可用补丁" in report["message"]

    def test_dry_run_no_db_write(self, monkeypatch):
        """dry_run 下即使桩产出补丁也不写库、不改冲突状态。"""
        pid = _new_project("F2-dryrun")
        sec = _seed_section(pid, title="总体", content="本项目建设工期：45 天。")
        _seed_conflict(pid, conflict_id=f"dry_{pid}", fact_name="工期")

        async def fake_generate(provider, *, sections, conflicts_text, facts_text,
                               failure_note="", ai_findings_text=""):
            patch = {
                "section_id": str(sec),
                "old_text": "45 天",
                "new_text": "50 天",
                "reason": "统一工期",
            }
            return [patch], [], None

        monkeypatch.setattr(repair_mod, "generate_patches", fake_generate)
        report = asyncio.run(repair_with_ai(pid, dry_run=True, provider=object()))

        assert report["dry_run"] is True
        with get_db(readonly=True) as conn:
            content = conn.execute(
                "SELECT content FROM sections WHERE id = ?", (sec,)
            ).fetchone()["content"]
            resolved = conn.execute(
                "SELECT resolved FROM consistency_conflicts WHERE conflict_id = ?",
                (f"dry_{pid}",),
            ).fetchone()["resolved"]
        assert content == "本项目建设工期：45 天。"
        assert int(resolved) == 0
