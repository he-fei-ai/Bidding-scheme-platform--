"""回归测试：目录生成工作流程优化（2026-10-19）

对应《目录生成工作流程优化方案》落地项：

  A. 质量评分报告（优化 6）：`outline_quality.score_outline_quality`
     五维评分（完整性/唯一性/层级深度/命名规范/结构合理性）+ A/B/C/D 等级，
     纯确定性计算（同输入同输出、无 AI、无时钟）。
  B. AI 局部自检（优化 3）：`_detect_potential_conflicts` 预检测冲突 →
     `_build_focus_outline_json` 裁出最小上下文子树 → `_resolve_review_payload`
     决定送审体（局部优先，焦点超上限回退全量）→ `_ai_deep_check(focus_paths=...)`。
  C. `_self_check_v51` 接线：裁决追加 `quality_score`、新增细分阶段事件
     （本地校验/规则校验/自动修复/质量评分）、两个新开关可回退。
  D. 前后端步骤口径守卫：前端 `OUTLINE_STEPS_V51` 13 步 ↔ 后端
     `sse_outline.py` step_map 值域 1-13（防再次出现 8/13 步错位）。

设计原则：只测行为不测实现。所有用例不依赖真实 AI 与外部网络。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
for _p in (str(TESTS_DIR), str(BACKEND_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from app.services.ai import outline_gate  # noqa: E402
from app.services.ai import outline_quality  # noqa: E402
from app.services.ai import workflows_outline_v51 as w  # noqa: E402


# ====================  fixtures：目录树与事实包 ====================


def _tree_with_duplicate() -> list[dict]:
    """1.1 下两个 L3 同父重复（合法 id、非空标题、层级齐全）。"""
    return [
        {
            "id": "1",
            "title": "项目概述",
            "children": [
                {
                    "id": "1.1",
                    "title": "工程概况",
                    "children": [
                        {"id": "1.1.1", "title": "项目背景"},
                        {"id": "1.1.2", "title": "项目背景"},
                    ],
                },
                {
                    "id": "1.2",
                    "title": "编制依据",
                    "children": [{"id": "1.2.1", "title": "招标文件"}],
                },
            ],
        },
        {
            "id": "2",
            "title": "施工部署",
            "children": [
                {
                    "id": "2.1",
                    "title": "组织架构",
                    "children": [{"id": "2.1.1", "title": "项目班子"}],
                }
            ],
        },
    ]


def _tree_clean() -> list[dict]:
    """结构完整的干净目录（无任何确定性问题）。"""
    return [
        {
            "id": "1",
            "title": "项目概述",
            "children": [
                {
                    "id": "1.1",
                    "title": "工程概况",
                    "children": [
                        {"id": "1.1.1", "title": "项目背景与必要性"},
                        {"id": "1.1.2", "title": "招标范围与界面"},
                    ],
                },
                {
                    "id": "1.2",
                    "title": "编制依据",
                    "children": [
                        {"id": "1.2.1", "title": "招标文件与答疑"},
                        {"id": "1.2.2", "title": "现行规范标准"},
                    ],
                },
            ],
        },
        {
            "id": "2",
            "title": "施工部署",
            "children": [
                {
                    "id": "2.1",
                    "title": "组织架构",
                    "children": [
                        {"id": "2.1.1", "title": "项目班子配置"},
                        {"id": "2.1.2", "title": "职责分工矩阵"},
                    ],
                },
                {
                    "id": "2.2",
                    "title": "总体部署",
                    "children": [
                        {"id": "2.2.1", "title": "施工区段划分"},
                        {"id": "2.2.2", "title": "大型机械布置"},
                    ],
                },
            ],
        },
    ]


# ==================== A. 质量评分器 ====================


class TestOutlineQualityScorer:
    """outline_quality.score_outline_quality 行为钉住。"""

    def test_clean_facts_scores_grade_a(self):
        facts = outline_gate.compute_outline_facts(_tree_clean())
        quality = outline_quality.score_outline_quality(facts)
        assert quality["total_score"] >= 90
        assert quality["grade"].startswith("A")
        assert quality["node_count"] == facts["node_count"]
        assert quality["chapter_count"] == facts["level1_count"]
        assert quality["leaf_count"] == facts["leaf_count"]

    def test_dirty_facts_scores_low(self):
        dirty = _tree_with_duplicate()
        # 同时打穿多个维度：占位符标题 + 空标题 + 编号缺失（1.1 下保持重复对）
        dirty[0]["children"][1]["children"][0]["title"] = "__TBD__"  # 占位符
        dirty[1]["children"][0]["children"][0]["title"] = ""  # 空标题
        dirty[1]["id"] = ""  # 编号缺失 → invalid-id
        facts = outline_gate.compute_outline_facts(dirty)
        quality = outline_quality.score_outline_quality(facts)
        clean_total = outline_quality.score_outline_quality(
            outline_gate.compute_outline_facts(_tree_clean())
        )["total_score"]
        # 脏目录必须显著低于干净目录（单维扣罚经权重放大后仍可感知）
        assert quality["total_score"] < clean_total - 10
        assert not quality["grade"].startswith("A")
        # 唯一定维度被重复标题打穿
        assert quality["detail"]["uniqueness"]["score"] < 100
        # 命名规范被占位符打穿
        assert quality["detail"]["naming"]["score"] < 100
        # 完整性被空标题打穿
        assert quality["detail"]["completeness"]["score"] < 100
        # 结构合理性被非法 id 打穿
        assert quality["detail"]["structure"]["score"] < 100

    def test_detail_has_five_dimensions_with_labels(self):
        facts = outline_gate.compute_outline_facts(_tree_clean())
        detail = outline_quality.score_outline_quality(facts)["detail"]
        assert set(detail) == {
            "completeness",
            "uniqueness",
            "depth",
            "naming",
            "structure",
        }
        for dim, item in detail.items():
            assert 0 <= item["score"] <= 100
            assert item["label"] == outline_quality.DIMENSION_LABELS[dim]
            assert isinstance(item["note"], str) and item["note"]

    def test_grade_boundaries(self):
        assert outline_quality.grade_for(90.0).startswith("A")
        assert outline_quality.grade_for(89.9).startswith("B")
        assert outline_quality.grade_for(75.0).startswith("B")
        assert outline_quality.grade_for(74.9).startswith("C")
        assert outline_quality.grade_for(60.0).startswith("C")
        assert outline_quality.grade_for(59.9).startswith("D")
        assert outline_quality.grade_for(0.0).startswith("D")

    def test_score_is_deterministic(self):
        """同一事实包两次评分必须逐字节相等（前端报告与后端裁决复用同一口径）。"""
        facts = outline_gate.compute_outline_facts(_tree_with_duplicate())
        first = outline_quality.score_outline_quality(facts)
        second = outline_quality.score_outline_quality(facts)
        assert json.dumps(first, sort_keys=True, ensure_ascii=False) == json.dumps(
            second, sort_keys=True, ensure_ascii=False
        )

    def test_defensive_on_empty_and_none(self):
        for facts in (None, {}, {"node_count": 0}):
            quality = outline_quality.score_outline_quality(facts or {})
            assert 0 <= quality["total_score"] <= 100
            assert quality["grade"]

    def test_completeness_penalizes_missing_score_coverage(self):
        base = outline_gate.compute_outline_facts(_tree_clean())
        with_cov = dict(base)
        with_cov["score_coverage"] = {
            "matched": [],
            "missing": ["a", "b", "c", "d"],
            "unexpected": [],
            "valid": False,
        }
        before = outline_quality.score_outline_quality(base)["detail"]["completeness"]["score"]
        after = outline_quality.score_outline_quality(with_cov)["detail"]["completeness"]["score"]
        assert after < before

    def test_summarize_quality(self):
        facts = outline_gate.compute_outline_facts(_tree_clean())
        quality = outline_quality.score_outline_quality(facts)
        summary = outline_quality.summarize_quality(quality)
        assert str(quality["total_score"]) in summary
        assert "A" in summary
        assert outline_quality.summarize_quality(None) == "质量评分不可用"


# ==================== B. AI 局部自检 ====================


class TestDetectPotentialConflicts:
    """_detect_potential_conflicts：疑似冲突预检测（局部自检焦点）。"""

    def test_finds_duplicate_paths(self):
        facts = outline_gate.compute_outline_facts(_tree_with_duplicate())
        focus = w._detect_potential_conflicts(facts, [])
        assert "1/1.1/1.1.1" in focus
        assert "1/1.1/1.1.2" in focus

    def test_empty_for_clean_tree(self):
        facts = outline_gate.compute_outline_facts(_tree_clean())
        assert w._detect_potential_conflicts(facts, []) == []

    def test_ignores_zone_order_only_facts(self):
        """分区顺序倒置是宿主机械观察，AI 给不出额外信息 → 不纳入焦点。"""
        facts = {
            "zone_order_violations": [
                {"earlier": {"id": "1", "path": "1"}, "later": {"id": "2", "path": "2"}}
            ]
        }
        assert w._detect_potential_conflicts(facts, []) == []

    def test_covers_placeholder_and_length_sources(self):
        facts = {
            "placeholder_titles": [{"path": "3/3.1", "title": "专项__X__"}],
            "title_length_violations": [{"path": "3/3.2", "title": "超长标题" * 10}],
            "single_child_nodes": [{"path": "4", "title": "单子节点章"}],
        }
        focus = w._detect_potential_conflicts(facts, [])
        assert set(focus) == {"3/3.1", "3/3.2", "4"}

    def test_defensive_on_none(self):
        assert w._detect_potential_conflicts(None, []) == []
        assert w._detect_potential_conflicts({}, []) == []


class TestBuildFocusOutlineJson:
    """_build_focus_outline_json：最小上下文子树裁剪。"""

    def test_expands_conflict_l2_only(self):
        slim_json, n_focus = w._build_focus_outline_json(
            _tree_with_duplicate(), ["1/1.1/1.1.1"]
        )
        slim = json.loads(slim_json)
        assert n_focus == 1
        # 第 1 章：1.1 完整展开（含两个 L3），1.2 仅标题
        ch1 = slim[0]
        assert ch1["title"] == "项目概述"
        l2_map = {c["id"]: c for c in ch1["children"]}
        assert set(l2_map["1.1"]["children"][0]) >= {"id", "title"}
        assert [c["title"] for c in l2_map["1.1"]["children"]] == ["项目背景", "项目背景"]
        assert "children" not in l2_map["1.2"]
        # 第 2 章（无冲突）：折叠为章标题，children 整体裁掉
        assert slim[1] == {"id": "2", "title": "施工部署"}

    def test_smaller_than_full_payload(self):
        full = w._truncate_outline_for_check(_tree_with_duplicate())
        slim_json, _ = w._build_focus_outline_json(
            _tree_with_duplicate(), ["1/1.1/1.1.1"]
        )
        assert len(slim_json) < len(full)

    def test_empty_focus_returns_empty(self):
        assert w._build_focus_outline_json(_tree_with_duplicate(), []) == ("", 0)
        assert w._build_focus_outline_json(_tree_with_duplicate(), None) == ("", 0)

    def test_l1_level_focus_expands_chapter(self):
        """焦点命中整章（如章级空标题）时该章全部 L2 完整展开。"""
        slim_json, n_focus = w._build_focus_outline_json(
            _tree_with_duplicate(), ["1"]
        )
        slim = json.loads(slim_json)
        assert n_focus == 1
        assert all("children" in c for c in slim[0]["children"])
        assert "children" not in slim[1]


class TestResolveReviewPayload:
    """_resolve_review_payload：送审体决策（局部优先 + 双重回退）。"""

    def test_full_when_no_focus(self):
        full = w._truncate_outline_for_check(_tree_with_duplicate())
        assert w._resolve_review_payload(_tree_with_duplicate(), None) == full
        assert w._resolve_review_payload(_tree_with_duplicate(), []) == full

    def test_full_when_over_limit(self, monkeypatch):
        monkeypatch.setattr(w, "OUTLINE_AI_LOCAL_REVIEW_MAX_NODES", 1)
        full = w._truncate_outline_for_check(_tree_with_duplicate())
        # 2 个焦点 > 上限 1 → 回退全量
        assert w._resolve_review_payload(
            _tree_with_duplicate(), ["1/1.1/1.1.1", "1/1.1/1.1.2"]
        ) == full

    def test_local_note_prefix_when_within_limit(self):
        payload = w._resolve_review_payload(_tree_with_duplicate(), ["1/1.1/1.1.1"])
        assert payload.startswith(w._LOCAL_REVIEW_NOTE)
        assert "1.1.2" in payload  # 同辈上下文保留

    @pytest.mark.asyncio
    async def test_ai_deep_check_uses_local_payload(self, monkeypatch):
        """focus_paths 接线到 _ai_deep_check：user prompt 带局部说明且体积更小。"""
        captured: dict = {}

        async def _fake_collect(provider, messages, **kwargs):
            # _call_ai_v51 构造 [system, user] 两条 AIMessage，取 user 正文
            captured["user"] = str(getattr(messages[1], "content", messages[1]))
            return {"passed": True, "issues": []}

        monkeypatch.setattr(w, "collect_json_response", _fake_collect)
        outline = _tree_with_duplicate()
        issues, error = await w._ai_deep_check(
            object(),
            outline,
            None,
            facts=outline_gate.compute_outline_facts(outline),
            focus_paths=["1/1.1/1.1.1"],
        )
        assert error is None
        assert "【局部送审说明】" in captured["user"]
        # 无冲突章的 L3 标题不应出现在送审体里（已折叠）
        assert "项目班子" not in captured["user"]

    @pytest.mark.asyncio
    async def test_ai_deep_check_full_when_no_focus(self, monkeypatch):
        captured: dict = {}

        async def _fake_collect(provider, messages, **kwargs):
            captured["user"] = str(getattr(messages[1], "content", messages[1]))
            return {"passed": True, "issues": []}

        monkeypatch.setattr(w, "collect_json_response", _fake_collect)
        outline = _tree_with_duplicate()
        await w._ai_deep_check(object(), outline, None)
        assert "【局部送审说明】" not in captured["user"]
        assert "项目班子" in captured["user"]  # 全量送审，无冲突章细节也在


# ==================== C. _self_check_v51 接线 ====================


class TestSelfCheckQualityWiring:
    """_self_check_v51：质量评分 + 细分阶段事件 + 开关回退。"""

    @pytest.mark.asyncio
    async def test_verdict_has_quality_score(self):
        state = SimpleNamespace()
        await w._self_check_v51(
            object(), _tree_with_duplicate(), 0.0, v51_state=state
        )
        quality = state.gate_report.get("quality_score")
        assert quality, "裁决必须携带质量评分报告"
        assert quality["total_score"] < 100
        assert set(quality["detail"]) == set(outline_quality.DIMENSION_WEIGHTS)
        assert state.gate_report["issue_count"] > 0

    @pytest.mark.asyncio
    async def test_emits_new_phase_events(self):
        phases: list[str] = []

        async def _cb(phase: str, message: str):
            phases.append(phase)

        await w._self_check_v51(
            object(),
            _tree_with_duplicate(),
            0.0,
            phase_callback=_cb,
            v51_state=SimpleNamespace(),
        )
        assert "V5.1本地校验" in phases  # 步骤 7
        assert "V5.1规则校验" in phases  # 步骤 9
        assert "V5.1质量评分" in phases  # 步骤 11

    @pytest.mark.asyncio
    async def test_quality_disabled_rolls_back(self, monkeypatch):
        monkeypatch.setattr(w, "OUTLINE_QUALITY_SCORE_ENABLED", False)
        phases: list[str] = []

        async def _cb(phase: str, message: str):
            phases.append(phase)

        state = SimpleNamespace()
        await w._self_check_v51(
            object(),
            _tree_with_duplicate(),
            0.0,
            phase_callback=_cb,
            v51_state=state,
        )
        assert "quality_score" not in state.gate_report
        assert "V5.1质量评分" not in phases

    @pytest.mark.asyncio
    async def test_local_review_switch_controls_focus(self, monkeypatch):
        captured: dict = {}

        async def _fake_ai_deep_check(
            provider, level1, project_info=None, *, facts=None,
            deterministic_issues=None, focus_paths=None,
        ):
            captured["focus"] = focus_paths
            return [], None

        monkeypatch.setattr(w, "_ai_deep_check", _fake_ai_deep_check)

        # 开启（默认）：有确定性问题 → 焦点非空
        await w._self_check_v51(
            object(), _tree_with_duplicate(), 0.0, v51_state=SimpleNamespace()
        )
        assert captured["focus"], "开启局部自检后必须定位到疑似冲突节点"

        # 关闭：focus_paths=None，维持全量送审
        monkeypatch.setattr(w, "OUTLINE_AI_LOCAL_REVIEW_ENABLED", False)
        await w._self_check_v51(
            object(), _tree_with_duplicate(), 0.0, v51_state=SimpleNamespace()
        )
        assert captured["focus"] is None


# ==================== D. 前后端步骤口径守卫 ====================


class TestStepContractGuards:
    """防 8/13 步口径再次错位（BUG-FIX 2026-09-18 同型缺陷的持续守卫）。"""

    FRONTEND_UTILS = BACKEND_DIR.parent / "frontend" / "src" / "utils.ts"

    def test_frontend_steps_is_13(self):
        text = self.FRONTEND_UTILS.read_text(encoding="utf-8")
        m = re.search(
            r"export const OUTLINE_STEPS_V51: string\[\] = \[(.*?)\];",
            text,
            re.DOTALL,
        )
        assert m, "前端 OUTLINE_STEPS_V51 定义未找到"
        labels = re.findall(r'"([^"]+)"', m.group(1))
        assert len(labels) == 13, f"前端步骤必须为 13 步，实际 {len(labels)}"

    def test_backend_step_map_within_range(self):
        """后端 step_map 值域 1-13，且关键阶段映射到方案第五节步骤号。"""
        src = (BACKEND_DIR / "app" / "routers" / "sse" / "sse_outline.py").read_text(
            encoding="utf-8"
        )
        m = re.search(r"step_map = \{(.*?)\n                    \}", src, re.DOTALL)
        assert m, "后端 step_map 未找到"
        entries = dict(re.findall(r'"([^"]+)":\s*(\d+),', m.group(1)))
        assert entries, "step_map 条目解析失败"
        for phase, step in entries.items():
            assert 1 <= int(step) <= 13, f"{phase} 步骤号 {step} 越界（1-13）"
        expected = {
            "V5.1准备": 1,
            "V5.1一级生成": 2,
            "V5.1一级校验": 3,
            "V5.1二级生成": 4,
            "V5.1三级生成": 6,
            "V5.1本地校验": 7,
            "V5.1自检": 8,
            "V5.1规则校验": 9,
            "V5.1自动修复": 10,
            "V5.1质量评分": 11,
            "V5.1保存": 12,
            "V5.1最终审核通过": 13,
        }
        for phase, step in expected.items():
            assert entries.get(phase) == str(step), (
                f"{phase} 应映射到步骤 {step}，实际 {entries.get(phase)}"
            )

    def test_completed_event_carries_step_and_quality(self):
        """completed 事件必须带 outline_step=13 与 quality 字段挂点。"""
        src = (BACKEND_DIR / "app" / "routers" / "sse" / "sse_db_ops.py").read_text(
            encoding="utf-8"
        )
        assert '"outline_step": 13' in src, "completed 事件缺少终态步骤号 13"
        assert '"quality"' in src, "completed 事件缺少质量评分字段挂点"

    def test_new_config_flags_exist(self):
        from app import config

        assert isinstance(config.OUTLINE_L2_CONCURRENCY, int)
        assert config.OUTLINE_L2_CONCURRENCY >= 0
        assert isinstance(config.OUTLINE_QUALITY_SCORE_ENABLED, bool)
        assert isinstance(config.OUTLINE_AI_LOCAL_REVIEW_ENABLED, bool)
        assert config.OUTLINE_AI_LOCAL_REVIEW_MAX_NODES >= 1
