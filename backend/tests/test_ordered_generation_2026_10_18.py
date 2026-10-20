"""正文生成模块 · 顺序生成 / 全局事实 / 上下文注入 增强回归测试（2026-10-18 第二轮）。

覆盖本轮修复与增强（每条对应一个真实缺陷或需求点）：

A. 目录树顺序排序（一）
   - A1 回归：按**全局 sort_order** 排序会把两个 L2 子树交错（1.1.1, 1.2.1, 1.1.2, 1.2.2）
     —— 即「跳章乱序」。正确口径：path 前序 + sort_order + id 稳定决胜。
   - A2 `_build_leaf_dicts` 集成：多 L1/L2 真实库数据，叶子必须按目录树顺序。
   - A3 排序稳定性：同输入多次排序输出一致。
   - A4 `verify_tree_order` / `find_resume_point`（断点续生定位）。

B. 顺序生成策略与批次（一.2 / 一.3）
   - B1 `build_generation_batches`：同父归批、上限切批、父变切批、保序。
   - B2 `generate_sections` serial 模式：完成顺序 == 目录树顺序，前序摘要注入后续章节。
   - B3 batch 模式：批内失败按顺序重试（不跳过后移），重试成功后序章节拿到其摘要。
   - B4 parallel 模式（向后兼容）：失败章节留待外层重试；重试后校验 ok。

C. 实际生成顺序追踪与校验（一.5 / 五.1）
   - C1 `GenerationOrderTracker`：serial 不一致 → error；parallel 不一致 → 仅告警。
   - C2 失败后重试成功**不计重复生成**（≥2 次成功完成才计）。
   - C3 `verify_generation_completeness` 四合一（顺序/完整性/质量/事实）。

D. 全局事实调用（二 / 五.2）
   - D1 `select_facts_by_chapter_map`：显式映射选事实；注入标签保留**事实原名**。
   - D2 人员类事实宽关键词：「项目经理」事实可被 personnel_count 映射调中。
   - D3 `verify_fact_invocation`：映射事实未注入 → warn；与全局字典冲突 → error。

E. 上下文注入（三 / 五.3）
   - E1 `extract_standards_from_text`：GB/JGJ 编号确定性抽取、去重、无命中为空。
   - E2 `audit_message_stack`：层级齐全/超限/截断/前序摘要校验。
   - E3 危大工程：`match_hazards_for_chapter` + `format_hazard_context`。
   - E4 提示词含顺序约束（第几章第几节 / 不得跨章 / 编号继承 / 图表同步）。

F. 提示词增强（四）
   - F1 `tree_index/tree_total` 进入任务层提示词。
   - F2 规范清单 / 危大参数 / 前序摘要按需注入且默认不注入（无数据时零变化）。

G. SSE 入口（七 / 八）
   - G1 `generation_mode` 查询参数贯通（含重试透传）。
   - G2 非法 generation_mode 回落 parallel（Query 对象泄漏回归）。
"""

import asyncio
import json
from pathlib import Path
import sys
import time as _t
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
import pytest


BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.db import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.routers.sse import sse_content  # noqa: E402
from app.routers.sse.sse_utils import _build_leaf_dicts  # noqa: E402
from app.security.auth import get_current_user  # noqa: E402
from app.services.ai.ordered_generation import (  # noqa: E402
    PREVIOUS_SECTIONS_SUMMARY_HEADER,
    PREVIOUS_SECTIONS_SUMMARY_MARKER,
    GenerationOrderTracker,
    audit_children_map,
    audit_message_stack,
    build_generation_batches,
    extract_section_summary,
    extract_standards_from_text,
    find_duplicate_sibling_sort_orders,
    find_resume_point,
    inject_previous_section_context,
    load_sections_in_tree_order,
    order_tree_dfs,
    repair_duplicate_sibling_sort_orders,
    select_facts_by_chapter_map,
    sibling_sort_key,
    sort_sections_by_tree_order,
    tree_sort_key,
    verify_fact_invocation,
    verify_generation_completeness,
    verify_generation_order,
    verify_tree_order,
)
from app.services.ai.workflows_sections import (  # noqa: E402
    build_chapter_content_messages,
    generate_sections,
)


API = "/api/v1/projects"


# ======================= 公共假件 =======================


class FlakyProvider:
    """按标题记录调用次数；指定标题首次调用抛错，其余返回固定正文。"""

    def __init__(self, fail_first_for: str = "", content: str = "正文内容"):
        self.calls: dict = {}
        self.stacks: list = []
        self._fail_first_for = fail_first_for
        self._content = content

    async def chat(self, messages, **kwargs):
        self.stacks.append(messages)
        title = ""
        for m in messages:
            if getattr(m, "role", "") == "user" and str(m.content).startswith("# 章节:"):
                title = str(m.content).split("\n")[0]
        n = self.calls.get(title, 0)
        self.calls[title] = n + 1
        if self._fail_first_for and self._fail_first_for in title and n == 0:
            raise RuntimeError("模拟首次失败")
        # 返回足够长的正文（>30 字）以通过质量校验
        return {"content": f"《{title}》{self._content}" * 40, "finish_reason": "stop"}


def _leaf(sid, title, path, parent_id=None, sort_order=1):
    return {
        "id": sid, "title": title, "level": 3, "parent_id": parent_id,
        "sort_order": sort_order, "path": path, "status": "pending",
        "section_type": "text", "scoring_point_id": None,
    }


def _two_subtree_leaves():
    """两个 L2 子树交错的叶子集（复现跳章乱序的最小用例）。"""
    return [
        _leaf(11, "1.1.1 章节A", "1.1.1", parent_id=2, sort_order=1),
        _leaf(12, "1.1.2 章节B", "1.1.2", parent_id=2, sort_order=2),
        _leaf(21, "1.2.1 章节C", "1.2.1", parent_id=3, sort_order=1),
        _leaf(22, "1.2.2 章节D", "1.2.2", parent_id=3, sort_order=2),
    ]


# ======================= A. 目录树顺序排序 =======================


class TestTreeOrderSort:
    def test_global_sort_order_does_not_interleave_subtrees(self):
        """回归：按全局 sort_order 主键会把两个 L2 子树交错（跳章乱序）。"""
        leaves = _two_subtree_leaves()
        # 旧（错误）口径：(sort_order, path) → 11, 21, 12, 22
        legacy = sorted(leaves, key=lambda d: (d["sort_order"], tuple(int(p) for p in d["path"].split("."))))
        assert [s["id"] for s in legacy] == [11, 21, 12, 22], "前置条件：旧口径确实乱序"
        # 新（正确）口径：path 前序 → 11, 12, 21, 22
        ordered = sort_sections_by_tree_order(leaves)
        assert [s["id"] for s in ordered] == [11, 12, 21, 22]
        assert [s["path"] for s in ordered] == ["1.1.1", "1.1.2", "1.2.1", "1.2.2"]

    def test_sort_is_stable_and_deterministic(self):
        leaves = list(reversed(_two_subtree_leaves()))
        first = sort_sections_by_tree_order(leaves)
        for _ in range(5):
            assert [s["id"] for s in sort_sections_by_tree_order(leaves)] == [s["id"] for s in first]
        # 入参不被修改
        assert [s["id"] for s in leaves] == [22, 21, 12, 11]

    def test_tie_break_by_sort_order_then_id(self):
        a = _leaf(99, "x", "1.1", parent_id=2, sort_order=1)
        b = _leaf(98, "x", "1.1", parent_id=2, sort_order=1)
        assert [s["id"] for s in sort_sections_by_tree_order([a, b])] == [98, 99]

    def test_missing_path_falls_back_without_crash(self):
        bad = _leaf(1, "无编号", "0")
        bad.pop("path")
        assert tree_sort_key(bad)[0] == (0,)
        assert [s["id"] for s in sort_sections_by_tree_order([bad])] == [1]

    def test_verify_tree_order_detects_violation(self):
        shuffled = [_two_subtree_leaves()[1], _two_subtree_leaves()[0]]
        res = verify_tree_order(shuffled)
        assert res["ok"] is False and res["violations"]
        assert verify_tree_order(_two_subtree_leaves())["ok"] is True

    def test_find_resume_point(self):
        ids = [11, 12, 21, 22]
        r = find_resume_point(ids, [11, 12])
        assert r["next_index"] == 2 and r["next_id"] == 21
        assert r["last_completed_id"] == 12 and r["remaining"] == 2
        assert find_resume_point(ids, ids)["all_completed"] is True
        # 乱序完成集不影响定位（按目录树顺序找第一个未完成）
        r2 = find_resume_point(ids, [22, 11])
        assert r2["next_id"] == 12


class TestBuildLeafDictsTreeOrder:
    """真实 DB：多 L1/L2 项目，_build_leaf_dicts 必须按目录树顺序输出。"""

    @pytest.fixture()
    def client(self):
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
        with TestClient(app) as c:
            yield c
        app.dependency_overrides.clear()

    @pytest.fixture()
    def project_with_two_subtrees(self, client):
        resp = client.post(API, json={"name": "顺序生成测试项目", "industry": "公共建筑"})
        assert resp.status_code in (200, 201), resp.text
        pid = resp.json()["id"]

        def mk(payload):
            r = client.post(f"{API}/{pid}/sections", json=payload)
            assert r.status_code == 201, r.text
            return r.json()

        root = mk({"title": "第一章 施工组织"})
        l2a = mk({"title": "1.1 工期保证", "parent_id": root["id"]})
        l2b = mk({"title": "1.2 质量保证", "parent_id": root["id"]})
        leaf_a1 = mk({"title": "1.1.1 工期承诺", "parent_id": l2a["id"]})
        leaf_a2 = mk({"title": "1.1.2 人员配置", "parent_id": l2a["id"]})
        leaf_b1 = mk({"title": "1.2.1 质量目标", "parent_id": l2b["id"]})
        leaf_b2 = mk({"title": "1.2.2 验收标准", "parent_id": l2b["id"]})
        return {
            "pid": pid,
            "expected_order": [leaf_a1["id"], leaf_a2["id"], leaf_b1["id"], leaf_b2["id"]],
        }

    def _maps(self, pid):
        with get_db() as conn:
            rows = conn.execute(
                "SELECT id, parent_id, title, level, sort_order, section_type,"
                " scoring_point_id, status, content, target_words FROM sections"
                " WHERE project_id = ? ORDER BY sort_order, id",
                (pid,),
            ).fetchall()
        rows = [dict(r) for r in rows]
        by_id = {r["id"]: r for r in rows}
        children: dict = {}
        for r in rows:
            children.setdefault(r["parent_id"], []).append(r)
        return by_id, children

    def test_leaves_in_tree_order(self, project_with_two_subtrees):
        pid = project_with_two_subtrees["pid"]
        by_id, children = self._maps(pid)
        leaves = _build_leaf_dicts(
            project_id=pid, sections_by_id=by_id, children_map=children,
        )
        assert [lf["id"] for lf in leaves] == project_with_two_subtrees["expected_order"]
        assert [lf["path"] for lf in leaves] == ["1.1.1", "1.1.2", "1.2.1", "1.2.2"]

    def test_regenerate_ids_subset_still_ordered(self, project_with_two_subtrees):
        pid = project_with_two_subtrees["pid"]
        by_id, children = self._maps(pid)
        ids = project_with_two_subtrees["expected_order"]
        leaves = _build_leaf_dicts(
            project_id=pid, sections_by_id=by_id, children_map=children,
            regenerate_ids=f"{ids[3]},{ids[1]}",
        )
        assert [lf["id"] for lf in leaves] == [ids[1], ids[3]]


# ======================= B. 批次与顺序生成 =======================


class TestBuildGenerationBatches:
    def test_same_parent_grouped_capped_and_ordered(self):
        secs = [
            _leaf(1, "a", "1.1.1", parent_id=2, sort_order=1),
            _leaf(2, "b", "1.1.2", parent_id=2, sort_order=2),
            _leaf(3, "c", "1.1.3", parent_id=2, sort_order=3),
            _leaf(4, "d", "1.2.1", parent_id=3, sort_order=1),
            _leaf(5, "e", "1.2.2", parent_id=3, sort_order=2),
        ]
        batches = build_generation_batches(secs, max_batch_size=2)
        assert [[s["id"] for s in b] for b in batches] == [[1, 2], [3], [4, 5]]

    def test_empty_and_single(self):
        assert build_generation_batches([]) == []
        one = build_generation_batches([_leaf(1, "a", "1.1.1")])
        assert len(one) == 1 and len(one[0]) == 1


class TestPreviousSectionContext:
    def test_inject_and_extract(self):
        ctx = inject_previous_section_context({}, [
            {"title": "第一章", "summary": "工期 450 天"},
            {"title": "第二章", "summary": "人员 86 人"},
        ])
        assert "前序章节内容摘要" in ctx["previous_sections_summary"]
        assert "第一章" in ctx["previous_sections_summary"]
        # 空列表 → 移除键
        assert "previous_sections_summary" not in inject_previous_section_context(ctx, [])

    def test_extract_summary_strips_markdown(self):
        s = extract_section_summary("# 标题\n**加粗** `code`\n\n正文" * 30)
        assert "#" not in s and "**" not in s and len(s) <= 201

    def test_keeps_at_most_five(self):
        ctx = inject_previous_section_context(
            {}, [{"title": f"第{i}章", "summary": f"内容{i}"} for i in range(8)]
        )
        assert "第3章" in ctx["previous_sections_summary"]
        assert "第1章" not in ctx["previous_sections_summary"]


class TestGenerateSectionsOrderedModes:
    """generate_sections 三模式行为（顺序 / 重试 / 前序摘要）。"""

    def _run(self, mode, fail_first_for=""):
        provider = FlakyProvider(fail_first_for=fail_first_for)
        tracker = GenerationOrderTracker()
        sections = _two_subtree_leaves()

        async def _go():
            return await generate_sections(
                provider, sections,
                context={"generation_mode": mode, "use_cache": False, "project_id": 999101},
                concurrency=2,
                order_tracker=tracker,
            )

        results = asyncio.run(_go())
        return provider, tracker, results, sections

    def test_serial_mode_strict_order_and_prev_context(self):
        provider, tracker, results, sections = self._run("serial")
        assert all(r["status"] == "completed" for r in results)
        assert tracker.first_occurrence_order() == [11, 12, 21, 22]
        ver = tracker.verify([s["id"] for s in sections], mode="serial")
        assert ver["ok"], ver
        # 后续章节（21）的提示词注入了前序摘要（含 12 章节B）
        later = [st for st in provider.stacks
                 if any(getattr(m, "role", "") == "user" and str(m.content).startswith("# 章节: 1.2.1") for m in st)]
        assert later, "未找到 1.2.1 的提示词"
        joined = "\n".join(str(m.content) for m in later[0])
        assert "前序章节内容摘要" in joined and "章节B" in joined

    def test_batch_mode_retries_failed_in_order(self):
        provider, tracker, results, sections = self._run("batch", fail_first_for="章节B")
        statuses = {r["id"]: r["status"] for r in results}
        assert statuses == {11: "completed", 12: "completed", 21: "completed", 22: "completed"}
        # 完成顺序严格目录树顺序；失败后重试成功不计重复
        assert tracker.first_occurrence_order() == [11, 12, 21, 22]
        assert tracker.duplicates() == []
        assert tracker.verify([s["id"] for s in sections], mode="batch")["ok"]
        # 重试成功的章节B 摘要进入后续章节上下文
        later = [st for st in provider.stacks
                 if any(getattr(m, "role", "") == "user" and str(m.content).startswith("# 章节: 1.2.1") for m in st)]
        joined = "\n".join(str(m.content) for m in later[0])
        assert "章节B" in joined

    def test_default_mode_resolves_to_config_batch(self):
        """不传 generation_mode → 回落 config.CONTENT_GENERATION_MODE（batch 行为：
        完成顺序严格目录树序 + 前序摘要注入）。"""
        from app.config import CONTENT_GENERATION_MODE

        provider = FlakyProvider()
        tracker = GenerationOrderTracker()
        sections = _two_subtree_leaves()

        async def _go():
            return await generate_sections(
                provider, sections,
                context={"use_cache": False, "project_id": 999104},
                concurrency=2,
                order_tracker=tracker,
            )

        results = asyncio.run(_go())
        assert all(r["status"] == "completed" for r in results)
        assert tracker.first_occurrence_order() == [11, 12, 21, 22]
        ver = tracker.verify([s["id"] for s in sections], mode=CONTENT_GENERATION_MODE)
        assert ver["ok"], ver

    def test_parallel_mode_failure_left_for_outer_retry(self):
        provider, tracker, results, sections = self._run("parallel", fail_first_for="章节B")
        statuses = {r["id"]: r["status"] for r in results}
        assert statuses[12] == "failed", "parallel 模式不做批内重试（留给 SSE 外层）"
        ver = tracker.verify([s["id"] for s in sections], mode="parallel")
        assert ver["missing"] == [12] and ver["ok"] is False

        # 模拟 SSE 外层重试：只重跑失败章节（共享 tracker）
        async def _go2():
            return await generate_sections(
                provider, [s for s in sections if s["id"] == 12],
                context={"generation_mode": "parallel", "use_cache": False, "project_id": 999101},
                concurrency=2,
                order_tracker=tracker,
            )

        retry_results = asyncio.run(_go2())
        assert retry_results[0]["status"] == "completed"
        ver2 = tracker.verify([s["id"] for s in sections], mode="parallel")
        assert ver2["ok"], ver2  # 失败+重试成功 → 集合完整、不计重复


# ======================= C. 顺序追踪与综合校验 =======================


class TestOrderTracker:
    def test_serial_mismatch_is_error(self):
        t = GenerationOrderTracker()
        for sid in (11, 21, 12, 22):  # 乱序完成
            t.record(sid, "completed")
        ver = t.verify([11, 12, 21, 22], mode="serial")
        assert ver["ok"] is False and ver["order_mismatch"] is True and ver["level"] == "error"

    def test_parallel_mismatch_is_warning_only(self):
        t = GenerationOrderTracker()
        for sid in (11, 21, 12, 22):
            t.record(sid, "completed")
        ver = t.verify([11, 12, 21, 22], mode="parallel")
        assert ver["ok"] is True and ver["order_mismatch"] is True and ver["level"] == "warn"

    def test_retry_success_not_counted_as_duplicate(self):
        t = GenerationOrderTracker()
        t.record(12, "failed")
        t.record(12, "completed")
        assert t.duplicates() == []
        assert t.completed_sequence == [12]

    def test_true_duplicate_detected(self):
        t = GenerationOrderTracker()
        t.record(12, "completed")
        t.record(12, "completed")
        assert t.duplicates() == [12]
        ver = t.verify([12], mode="serial")
        assert ver["ok"] is False and ver["duplicates"] == [12]

    def test_missing_and_extra(self):
        t = GenerationOrderTracker()
        t.record(11, "completed")
        ver = t.verify([11, 12], mode="serial")
        assert ver["missing"] == [12] and ver["ok"] is False


class TestVerifyGenerationCompleteness:
    def test_ok_case_with_tracker(self):
        t = GenerationOrderTracker()
        for sid in (11, 12, 21, 22):
            t.record(sid, "completed")
        results = [
            {"id": 11, "title": "1.1.1 章节A", "status": "completed", "content": "内容" * 50},
            {"id": 12, "title": "1.1.2 章节B", "status": "completed", "content": "内容" * 50},
            {"id": 21, "title": "1.2.1 章节C", "status": "completed", "content": "内容" * 50},
            {"id": 22, "title": "1.2.2 章节D", "status": "completed", "content": "内容" * 50},
        ]
        ver = verify_generation_completeness(
            results, [11, 12, 21, 22], generation_mode="batch", order_tracker=t,
        )
        assert ver["ok"], ver
        assert ver["order"]["order_mismatch"] is False

    def test_too_short_and_missing(self):
        results = [{"id": 11, "title": "A", "status": "completed", "content": "短"}]
        ver = verify_generation_completeness(results, [11, 12], generation_mode="serial")
        types = {i["type"] for i in ver["issues"]}
        assert "too_short" in types and "missing_sections" in types
        assert ver["ok"] is False

    def test_verify_generation_order_duplicates(self):
        res = verify_generation_order([1, 1, 2], [1, 2])
        assert res["ok"] is False and res["duplicates"] == [1]


# ======================= D. 全局事实调用 =======================


FACTS_FOR_MAP = [
    {"key": "totalDuration", "label": "总工期", "value": "540 日历天", "category": "schedule"},
    {"key": "pm", "label": "项目经理", "value": "张三", "category": "personnel"},
    {"key": "equip", "label": "塔式起重机", "value": "QTZ80", "category": "technical"},
]


class TestFactSelectionAndVerification:
    def test_explicit_map_selects_chapter_facts(self):
        selected = select_facts_by_chapter_map("第五章 施工进度计划及保证措施", FACTS_FOR_MAP)
        # 返回的 key 为**字段键**（duration），值来自事实「总工期 540 日历天」
        keys = {f["key"] for f in selected}
        assert "duration" in keys
        assert all(f["value"] for f in selected)
        dur = next(f for f in selected if f["key"] == "duration")
        assert dur["value"] == "540 日历天"

    def test_injected_label_prefers_fact_original_name(self):
        """BUG-FIX：注入标签保留事实原名（总工期），而非字段名（工期）。"""
        selected = select_facts_by_chapter_map("第五章 施工进度计划及保证措施", FACTS_FOR_MAP)
        dur = next(f for f in selected if f["key"] == "duration")
        assert dur["label"] == "总工期"

    def test_personnel_fact_matched_via_wide_keywords(self):
        """人员类事实（项目经理）可被 personnel_count 映射调中（E2E 回归）。"""
        selected = select_facts_by_chapter_map("第二章 施工部署与组织机构", FACTS_FOR_MAP)
        personnel = [f for f in selected if f["key"] == "personnel_count"]
        assert personnel, f"项目经理事实未被调中: {selected}"
        assert personnel[0]["value"] == "张三"
        assert personnel[0]["label"] == "项目经理"

    def test_fact_label_reaches_prompt(self):
        msgs = build_chapter_content_messages(
            {"id": "s1", "title": "第五章 施工进度计划及保证措施", "level": 3},
            facts=FACTS_FOR_MAP, facts_policy="omit",
        )
        user_text = "\n".join(m.content for m in msgs if m.role == "user")
        assert "总工期" in user_text and "540 日历天" in user_text

    def test_verify_fact_invocation_reports_not_called_and_conflicts(self):
        contract = {
            "global_dictionary": {
                "fields": {"duration": "540 日历天"},
                "by_chapter": {},
                "version": "1.0",
            },
        }
        # ① 未注入映射事实 → warn
        results = [{"id": 1, "title": "第五章 施工进度计划", "status": "completed",
                    "content": "本章安排施工进度，总工期 540 日历天，节点合理。"}]
        trace = [{"chapter": "第五章 施工进度计划", "field": "其他字段", "source": "facts", "value": "x"}]
        rep = verify_fact_invocation(results, contract, trace, FACTS_FOR_MAP)
        assert rep["ok"] is True  # 无冲突
        assert rep["warnings"], "映射事实未注入应告警"
        assert any(w["type"] == "fact_not_called" for w in rep["warnings"])

        # ② 正文与全局事实冲突 → error
        bad = [{"id": 1, "title": "第五章 施工进度计划", "status": "completed",
                "content": "本章总工期为 600 日历天，远远超出计划安排。"}]
        rep2 = verify_fact_invocation(bad, contract, [], FACTS_FOR_MAP)
        assert rep2["ok"] is False
        assert any(i["type"] == "fact_conflict" for i in rep2["issues"])


# ======================= E. 上下文注入 =======================


class TestStandardsExtraction:
    def test_extract_codes_with_titles(self):
        text = "依据 GB 50720-2011《建设工程施工现场消防安全技术规范》及 JGJ 59-2011《建筑施工安全检查标准》施工。"
        out = extract_standards_from_text(text)
        codes = [s["code"] for s in out]
        assert "GB 50720-2011" in codes and "JGJ 59-2011" in codes
        gb = next(s for s in out if s["code"] == "GB 50720-2011")
        assert "消防安全" in gb["title"]

    def test_dedup_and_empty(self):
        text = "按 GB 50720-2011 和 GB50720-2011 同一标准。"
        out = extract_standards_from_text(text)
        assert len(out) == 1
        assert extract_standards_from_text("") == []
        assert extract_standards_from_text(None) == []


class TestContextAudit:
    def test_wellformed_stack_passes(self):
        from app.services.ai.providers.base import AIMessage

        msgs = [
            AIMessage(role="system", content="system rules"),
            AIMessage(role="user", content="项目摘要: x"),
            AIMessage(role="user", content="# 章节: 第一章 施工组织设计\n正文任务"),
        ]
        rep = audit_message_stack(msgs, {"title": "第一章 施工组织设计"})
        assert rep["ok"], rep
        assert rep["layers"] == 3

    def test_missing_system_and_title(self):
        from app.services.ai.providers.base import AIMessage

        msgs = [AIMessage(role="user", content="no system")]
        rep = audit_message_stack(msgs, {"title": "第一章"})
        assert rep["ok"] is False
        assert any(i["type"] == "missing_system" for i in rep["issues"])
        assert any(i["type"] == "task_layer_missing_title" for i in rep["issues"])

    def test_truncation_and_overload_warn(self):
        from app.services.ai.providers.base import AIMessage

        msgs = [
            AIMessage(role="system", content="sys"),
            AIMessage(role="user", content="# 章节: X\n" + "数据…" * 100),
        ]
        rep = audit_message_stack(msgs, {"title": "X"})
        assert rep["truncated"] is True
        assert any(w["type"] == "truncated_context" for w in rep["warnings"])

    def test_require_previous(self):
        from app.services.ai.providers.base import AIMessage

        msgs = [
            AIMessage(role="system", content="sys"),
            AIMessage(role="user", content="# 章节: X"),
        ]
        rep = audit_message_stack(msgs, {"title": "X"}, require_previous=True)
        assert any(w["type"] == "missing_previous_context" for w in rep["warnings"])


class TestHazardContext:
    def test_match_hazards_for_chapter(self):
        from app.services.ai.hazard_projects import match_hazards_for_chapter

        matched = match_hazards_for_chapter("深基坑工程专项施工方案", ["第四章 专项施工方案"])
        assert any(hp.code == "deep_excavation" for hp in matched)
        # 无关章节不命中
        assert match_hazards_for_chapter("第七章 质量管理体系与措施", []) == []

    def test_format_hazard_context(self):
        from app.services.ai.hazard_projects import format_hazard_context, match_hazards_for_chapter

        matched = match_hazards_for_chapter("高大模板工程专项施工方案")
        text = format_hazard_context(matched)
        assert "危大工程判定参数" in text
        assert "必含章节" in text and "适用规范" in text
        assert format_hazard_context([]) == ""

    def test_hazard_injected_only_for_related_chapter(self):
        hazard_info = {
            "detected": [{
                "code": "deep_excavation", "name": "深基坑工程",
                "required_sections": ["基坑支护方案"],
                "required_tables": ["基坑监测点位与频率表"],
                "required_charts": ["基坑支护剖面图"],
                "safety_threshold": {"depth_m": 3},
                "reference_standards": ["JGJ 120-2012《建筑基坑支护技术规程》"],
            }],
            "suggested_sections": ["基坑支护方案"],
        }
        related = build_chapter_content_messages(
            {"id": "h1", "title": "深基坑工程专项施工方案", "level": 3},
            hazard_info=hazard_info,
        )
        unrelated = build_chapter_content_messages(
            {"id": "h2", "title": "第七章 质量管理体系与措施", "level": 3},
            hazard_info=hazard_info,
        )
        rel_text = "\n".join(m.content for m in related)
        unrel_text = "\n".join(m.content for m in unrelated)
        assert "危大工程判定参数" in rel_text
        assert "危大工程判定参数" not in unrel_text, "无关章节不得注入危大参数"

    def test_standards_injected_only_when_present(self):
        without = build_chapter_content_messages({"id": "s1", "title": "第一章 施工组织设计", "level": 3})
        with_std = build_chapter_content_messages(
            {"id": "s1", "title": "第一章 施工组织设计", "level": 3},
            standards_list=[{"code": "GB 50720-2011", "title": "建设工程施工现场消防安全技术规范"}],
        )
        assert "适用规范清单" not in "\n".join(m.content for m in without)
        assert "适用规范清单" in "\n".join(m.content for m in with_std)


class TestPromptOrderConstraints:
    def test_order_and_structure_constraints_present(self):
        msgs = build_chapter_content_messages(
            {"id": "s1", "title": "1.2.3 危大工程监测", "level": 3, "path": "1.2.3",
             "ancestors": ["第一章", "1.2 专项方案"], "sibling_chapters": [{"title": "1.2.1 其他"}]},
            tree_index=3, tree_total=40,
        )
        joined = "\n".join(m.content for m in msgs)
        assert "【顺序与结构约束】" in joined
        assert "不得提前写后续章节内容" in joined
        assert "正文编号继承目录编号体系" in joined
        assert "图号不虚跳" in joined
        assert "第 3/40 个章节" in joined
        assert "章节位置" in joined and "1.2.3" in joined

    def test_context_check_attached_to_section(self):
        section = {"id": "s1", "title": "第一章 施工组织设计", "level": 3}
        provider = FlakyProvider()

        async def _go():
            return await generate_sections(
                provider, [section],
                context={"use_cache": False, "project_id": 999102},
                concurrency=1,
            )

        results = asyncio.run(_go())
        assert results[0]["status"] == "completed"
        assert isinstance(results[0].get("context_check"), dict)
        assert results[0]["context_check"]["ok"] is True

    def test_hazard_and_standards_flow_through_generate_sections(self):
        """集成：hazard_info / standards_list 必须经 kw_to_ctx 进入提示词
        （防「扫描到了但注入不到」的静默丢弃回归）。"""
        hazard_info = {
            "detected": [{
                "code": "deep_excavation", "name": "深基坑工程",
                "required_sections": ["基坑支护方案"],
                "required_tables": [], "required_charts": [],
                "safety_threshold": {"depth_m": 3},
                "reference_standards": ["JGJ 120-2012《建筑基坑支护技术规程》"],
            }],
            "suggested_sections": ["基坑支护方案"],
        }
        provider = FlakyProvider()
        sections = [_leaf(31, "深基坑工程专项施工方案", "4.1.1", parent_id=4)]

        async def _go():
            return await generate_sections(
                provider, sections,
                context={"use_cache": False, "project_id": 999103},
                concurrency=1,
                hazard_info=hazard_info,
                standards_list=[{"code": "GB 50720-2011", "title": "消防安全技术规范"}],
            )

        results = asyncio.run(_go())
        assert results[0]["status"] == "completed"
        joined = "\n".join(str(m.content) for m in provider.stacks[0])
        assert "危大工程判定参数" in joined, "hazard_info 未进入提示词（kw_to_ctx 漏登记？）"
        assert "适用规范清单" in joined, "standards_list 未进入提示词（kw_to_ctx 漏登记？）"


# ======================= F2. 并发压力与基准 =======================


class TestOrderStressAndBenchmark:
    """并发规模下的顺序追踪稳定性 + 性能基准（防 O(n²) 与守护回归）。"""

    def _many_sections(self, n: int) -> list[dict]:
        """n 个叶子，按 5 个 L2 子树均匀分布（每父 5 的倍数切批）。"""
        out: list[dict] = []
        for i in range(n):
            parent = 2 + i // 5
            out.append(_leaf(1000 + i, f"章节{i}", f"1.{parent - 1}.{i % 5 + 1}",
                             parent_id=parent, sort_order=i % 5 + 1))
        return out

    def test_parallel_random_completion_detected_as_warning_only(self):
        """parallel + 随机完成顺序：集合完整 ok，顺序不一致仅 warn（设计行为）。"""
        import random

        rnd = random.Random(42)
        sections = self._many_sections(60)
        tracker = GenerationOrderTracker()
        order = [s["id"] for s in sections]
        rnd.shuffle(order)
        for sid in order:
            tracker.record(sid, "completed")
        ver = tracker.verify([s["id"] for s in sections], mode="parallel")
        assert ver["ok"] is True and ver["order_mismatch"] is True
        assert ver["level"] == "warn" and ver["missing"] == []

    def test_batch_mode_strict_order_at_scale(self):
        """batch + 120 节：完成顺序必须逐节等于目录树顺序（零乱序）。"""
        sections = self._many_sections(120)
        provider = FlakyProvider()
        tracker = GenerationOrderTracker()

        async def _go():
            return await generate_sections(
                provider, sections,
                context={"generation_mode": "batch", "use_cache": False,
                         "project_id": 999105},
                concurrency=4,
                order_tracker=tracker,
            )

        results = asyncio.run(_go())
        assert len(results) == 120 and all(r["status"] == "completed" for r in results)
        expected = [s["id"] for s in sections]
        assert tracker.first_occurrence_order() == expected
        assert tracker.verify(expected, mode="batch")["ok"]

    def test_retry_storm_no_false_duplicates(self):
        """半数章节首次失败、重试成功后：集合完整、无重复、顺序仍正确。"""
        sections = self._many_sections(20)
        provider = FlakyProvider(fail_first_for="章节")  # 全部首次失败
        tracker = GenerationOrderTracker()

        async def _go():
            return await generate_sections(
                provider, sections,
                context={"generation_mode": "batch", "use_cache": False,
                         "project_id": 999106},
                concurrency=5,
                order_tracker=tracker,
            )

        results = asyncio.run(_go())
        assert all(r["status"] == "completed" for r in results)
        expected = [s["id"] for s in sections]
        assert tracker.duplicates() == []
        assert tracker.first_occurrence_order() == expected
        assert tracker.verify(expected, mode="batch")["ok"]

    def test_sort_and_track_performance_baseline(self):
        """基准：2000 节排序 + 2000 次追踪记录须远低于秒级（防 O(n²) 回归）。"""
        import time as _time

        sections = self._many_sections(2000)
        t0 = _time.monotonic()
        ordered = sort_sections_by_tree_order(sections)
        t1 = _time.monotonic()
        tracker = GenerationOrderTracker()
        for s in ordered:
            tracker.record(s["id"], "completed")
        t2 = _time.monotonic()
        assert [s["id"] for s in ordered] == [s["id"] for s in sections]
        # 宽松上限（CI 机器远快于此）；只为抓住量级倒退
        assert t1 - t0 < 2.0, f"排序 2000 节耗时 {t1 - t0:.2f}s"
        assert t2 - t1 < 2.0, f"追踪 2000 次耗时 {t2 - t1:.2f}s"
        ver = tracker.verify([s["id"] for s in sections], mode="batch")
        assert ver["ok"]


# ======================= G. SSE 入口 =======================


class TestSSEGenerationModeParam:
    @pytest.fixture()
    def client(self):
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
        with TestClient(app) as c:
            yield c
        app.dependency_overrides.clear()

    @pytest.fixture()
    def seeded(self, client):
        resp = client.post(API, json={"name": "SSE顺序模式测试项目", "industry": "公共建筑"})
        pid = resp.json()["id"]

        def mk(payload):
            r = client.post(f"{API}/{pid}/sections", json=payload)
            assert r.status_code == 201, r.text
            return r.json()

        root = mk({"title": "第一章 施工组织"})
        child = mk({"title": "1.1 工期保证措施", "parent_id": root["id"]})
        leaf1 = mk({"title": "1.1.1 工期承诺", "parent_id": child["id"]})
        leaf2 = mk({"title": "1.1.2 人员配置", "parent_id": child["id"]})
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO analysis (project_id, summary_json,"
                " scoring_points_json, global_facts_json) VALUES (?, ?, ?, ?)",
                (pid, json.dumps({"project_overview": "公共建筑施工招标"}, ensure_ascii=False),
                 json.dumps([{"point": "工期保证", "score": 10}], ensure_ascii=False),
                 json.dumps([{"key": "duration", "label": "总工期", "value": "180天",
                              "category": "schedule"}], ensure_ascii=False)),
            )
            conn.commit()
        return {"pid": pid, "leaf_ids": [leaf1["id"], leaf2["id"]]}

    def _gm_mock(self):
        """generation_manager 桩（与 test_content_generation_interactions 同口径）。"""
        gm = MagicMock(name="generation_manager")
        gm.create_task = AsyncMock(return_value="gen-test-task")
        gm.update_section_progress = AsyncMock()
        gm.finalize_task = AsyncMock()
        gm.is_paused = AsyncMock(return_value=False)
        gm.resume_task = AsyncMock(return_value=None)
        gm.pause_task = AsyncMock(return_value=True)
        gm._lock = asyncio.Lock()
        gm._tasks = {}
        gm._persist_locks = {}
        return gm

    def _consume(self, client, url, max_lines=300, wall_timeout=8.0):
        events = []
        deadline = _t.monotonic() + wall_timeout
        ended = False
        with client.stream("GET", url) as r:
            assert r.status_code == 200, r.text
            for line in r.iter_lines():
                if _t.monotonic() > deadline or len(events) >= max_lines:
                    break
                if not line or not line.startswith("data:"):
                    continue
                try:
                    events.append(json.loads(line[len("data:"):].strip()))
                except json.JSONDecodeError:
                    continue
            else:
                ended = True
        return events, ended

    def test_generation_mode_reaches_run_sections_including_retry(self, client, seeded):
        """generation_mode 必须贯通主生成与重试（旧代码重试漏传致静默回落 parallel）。"""
        pid = seeded["pid"]
        seen_modes: list = []

        async def fake_run_sections(provider, leaves, **kwargs):
            seen_modes.append(kwargs.get("generation_mode"))
            # 新上下文通道必须随 kwargs 下发（漏传 = 生产永远注入不到）
            assert "hazard_info" in kwargs, "hazard_info 未随 _run_sections 下发"
            assert "standards_list" in kwargs, "standards_list 未随 _run_sections 下发"
            assert "order_tracker" in kwargs, "order_tracker 未随 _run_sections 下发"
            complete_cb = kwargs["section_complete_callback"]
            results = []
            for ld in leaves:
                # 第一章首次失败 → 触发重试路径
                if ld["id"] == seeded["leaf_ids"][0] and len(seen_modes) == 1:
                    res = {"id": ld["id"], "title": ld["title"], "status": "failed",
                           "content": "", "error": "模拟失败"}
                else:
                    res = {"id": ld["id"], "title": ld["title"], "status": "completed",
                           "content": f"《{ld['title']}》正文内容，工期 180 天，长度足够通过校验。"}
                await complete_cb(res)
                results.append(res)
            return results

        gm = self._gm_mock()

        with patch.object(sse_content, "_run_sections", fake_run_sections), \
             patch.object(sse_content, "_try_get_provider",
                          AsyncMock(return_value=(MagicMock(), None, 2))), \
             patch.object(sse_content, "generation_manager", gm), \
             patch("app.routers.sections.require_project_permission", AsyncMock()):
            events, ended = self._consume(
                client, f"{API}/{pid}/sections/generate-content?generation_mode=batch"
            )

        assert ended, "SSE 流未自行结束"
        assert seen_modes, "generate_sections 未被调用"
        assert all(m == "batch" for m in seen_modes), f"generation_mode 未贯通: {seen_modes}"
        # 流必须正常收敛（Query 对象泄漏回归：曾致 json.dumps 抛错提前断流）
        terminal = [e for e in events
                    if e.get("status") in ("completed", "error", "stopped")
                    and e.get("section_id") is None]
        assert terminal and terminal[-1]["status"] == "completed", (
            terminal[-1] if terminal else events
        )

    def test_default_mode_follows_config(self, client, seeded):
        """不传 generation_mode → config.CONTENT_GENERATION_MODE（batch，
        2026-10-18 第二轮起前后端默认统一；显式传 parallel 仍是旧行为）。"""
        from app.config import CONTENT_GENERATION_MODE

        assert CONTENT_GENERATION_MODE in ("parallel", "batch", "serial")
        pid = seeded["pid"]
        seen_modes: list = []

        async def fake_run_sections(provider, leaves, **kwargs):
            seen_modes.append(kwargs.get("generation_mode"))
            complete_cb = kwargs["section_complete_callback"]
            results = []
            for ld in leaves:
                res = {"id": ld["id"], "title": ld["title"], "status": "completed",
                       "content": f"《{ld['title']}》正文内容，工期 180 天，长度足够通过校验。"}
                await complete_cb(res)
                results.append(res)
            return results

        gm = self._gm_mock()

        with patch.object(sse_content, "_run_sections", fake_run_sections), \
             patch.object(sse_content, "_try_get_provider",
                          AsyncMock(return_value=(MagicMock(), None, 2))), \
             patch.object(sse_content, "generation_manager", gm), \
             patch("app.routers.sections.require_project_permission", AsyncMock()):
            events, ended = self._consume(
                client, f"{API}/{pid}/sections/generate-content"
            )

        assert ended
        assert seen_modes and all(m == CONTENT_GENERATION_MODE for m in seen_modes), seen_modes

    def test_explicit_parallel_still_supported(self, client, seeded):
        """向后兼容：显式传 parallel 必须透传（旧行为可显式获得）。"""
        pid = seeded["pid"]
        seen_modes: list = []

        async def fake_run_sections(provider, leaves, **kwargs):
            seen_modes.append(kwargs.get("generation_mode"))
            complete_cb = kwargs["section_complete_callback"]
            results = []
            for ld in leaves:
                res = {"id": ld["id"], "title": ld["title"], "status": "completed",
                       "content": f"《{ld['title']}》正文内容，工期 180 天，长度足够通过校验。"}
                await complete_cb(res)
                results.append(res)
            return results

        gm = self._gm_mock()

        with patch.object(sse_content, "_run_sections", fake_run_sections), \
             patch.object(sse_content, "_try_get_provider",
                          AsyncMock(return_value=(MagicMock(), None, 2))), \
             patch.object(sse_content, "generation_manager", gm), \
             patch("app.routers.sections.require_project_permission", AsyncMock()):
            events, ended = self._consume(
                client, f"{API}/{pid}/sections/generate-content?generation_mode=parallel"
            )

        assert ended
        assert seen_modes and all(m == "parallel" for m in seen_modes), seen_modes

    def test_case_variant_generation_mode_normalized(self, client, seeded):
        """大小写变体（BATCH）被 impl 归一化为 batch，不崩溃不断流。"""
        pid = seeded["pid"]
        seen_modes: list = []

        async def fake_run_sections(provider, leaves, **kwargs):
            seen_modes.append(kwargs.get("generation_mode"))
            complete_cb = kwargs["section_complete_callback"]
            results = []
            for ld in leaves:
                res = {"id": ld["id"], "title": ld["title"], "status": "completed",
                       "content": f"《{ld['title']}》正文内容，工期 180 天，长度足够通过校验。"}
                await complete_cb(res)
                results.append(res)
            return results

        gm = self._gm_mock()

        with patch.object(sse_content, "_run_sections", fake_run_sections), \
             patch.object(sse_content, "_try_get_provider",
                          AsyncMock(return_value=(MagicMock(), None, 2))), \
             patch.object(sse_content, "generation_manager", gm), \
             patch("app.routers.sections.require_project_permission", AsyncMock()):
            events, ended = self._consume(
                client, f"{API}/{pid}/sections/generate-content?generation_mode=BATCH"
            )

        assert ended, "大小写变体导致 SSE 断流"
        assert seen_modes and all(m == "batch" for m in seen_modes), seen_modes


# ======================= H. 四方顺序一致性（落库 / 展示 / 导出 / 生成） =======================
#
# 需求口径：排序规则统一由目录生成模块写入（sort_order），下游只读不重排；
# 落库顺序、展示顺序、导出顺序必须与目录树顺序一致。本轮把「目录树深度优先
# 前序」提升为四方共享的唯一实现，以下测试锁定该契约与三处历史偏差。


def _row(sid, title, parent_id=None, sort_order=1, level=1, created_at="2026-01-01"):
    return {
        "id": sid, "title": title, "parent_id": parent_id,
        "sort_order": sort_order, "level": level, "created_at": created_at,
    }


class TestTreeDfsOrderAuthority:
    """order_tree_dfs / sibling_sort_key：不依赖 path 的目录树前序权威。"""

    def test_dfs_prefix_order_without_path(self):
        rows = [
            _row(10, "L1-2", parent_id=None, sort_order=2),
            _row(11, "L1-1", parent_id=None, sort_order=1),
            _row(12, "1.2.1", parent_id=10, sort_order=1),
            _row(13, "1.1.2", parent_id=11, sort_order=2),
            _row(14, "1.1.1", parent_id=11, sort_order=1),
        ]
        out = order_tree_dfs(rows)
        assert [r["id"] for r in out] == [11, 14, 13, 10, 12]
        # 集合不变、不丢节点、不重复
        assert sorted(r["id"] for r in out) == sorted(r["id"] for r in rows)
        # 入参不被修改
        assert [r["id"] for r in rows] == [10, 11, 12, 13, 14]

    def test_legacy_level_sort_order_is_breadth_first_and_interleaves(self):
        """回归：旧 HTML 口径 ORDER BY level, sort_order, id 是广度优先，
        会把同一棵 L1 子树劈开（1 的子树 {1,3,5,7} 被 2 的子树 {2,4,6,8} 插断）。"""
        rows = [
            _row(1, "L1-1", sort_order=1, level=1),
            _row(2, "L1-2", sort_order=2, level=1),
            _row(3, "1.1", parent_id=1, sort_order=1, level=2),
            _row(4, "1.2", parent_id=2, sort_order=1, level=2),
            _row(5, "1.2", parent_id=1, sort_order=2, level=2),
            _row(6, "1.2", parent_id=2, sort_order=2, level=2),
            _row(7, "1.1.1", parent_id=3, sort_order=1, level=3),
            _row(8, "1.2.1", parent_id=4, sort_order=1, level=3),
        ]
        legacy = [
            r["id"] for r in sorted(rows, key=lambda r: (r["level"], r["sort_order"], r["id"]))
        ]
        assert legacy == [1, 2, 3, 4, 5, 6, 7, 8]
        # 旧口径是广度优先：root 1 的子树 {1,3,5,7} 被 root 2 的子树拆断
        # （5 是 1 的子孙，却排在同层的 4、6 之后）
        assert legacy.index(4) < legacy.index(5), "前置条件：旧口径确实跨子树交错"
        dfs = [r["id"] for r in order_tree_dfs(rows)]
        assert dfs == [1, 3, 7, 5, 2, 4, 8, 6]
        # 新口径下每棵子树连续成块
        kids_of: dict = {}
        for r in rows:
            kids_of.setdefault(r["parent_id"], []).append(r["id"])

        def subtree(root):
            out = []

            def walk(sid):
                out.append(sid)
                for kid in sorted(kids_of.get(sid, [])):
                    walk(kid)

            walk(root)
            return out

        for root in (1, 2):
            sub = subtree(root)
            start = dfs.index(root)
            assert dfs[start:start + len(sub)] == sub, f"子树 {root} 必须连续成块"

    def test_dfs_equivalent_to_path_tuple_order(self):
        """DFS 前序 == path 元组升序：两口径在干净数据上给出同一顺序。"""
        rows = [
            _row(1, "L1-1", sort_order=1, level=1),
            _row(2, "L1-2", sort_order=2, level=1),
            _row(3, "1.1", parent_id=1, sort_order=1, level=2),
            _row(4, "1.2", parent_id=1, sort_order=2, level=2),
            _row(5, "2.1", parent_id=2, sort_order=1, level=2),
            _row(6, "1.1.1", parent_id=3, sort_order=1, level=3),
            _row(7, "1.1.2", parent_id=3, sort_order=2, level=3),
        ]
        dfs = order_tree_dfs(rows)
        # 由树结构派生 path（同级序号即 path 段），再按 path 元组排序 → 同一序列
        kids: dict = {}
        for r in rows:
            kids.setdefault(r["parent_id"], []).append(r["id"])
        by_id = {r["id"]: r for r in rows}
        idx_of = {
            sid: i + 1
            for bucket in kids.values()
            for i, sid in enumerate(sorted(bucket))
        }

        def path_of_id(sid):
            parts, cur, seen = [], sid, set()
            while cur is not None and cur not in seen:
                seen.add(cur)
                parts.append(idx_of[cur])
                cur = by_id[cur]["parent_id"]
            return tuple(reversed(parts))

        by_path = sorted(dfs, key=lambda r: path_of_id(r["id"]))
        assert [r["id"] for r in by_path] == [r["id"] for r in dfs]
        # 同时校验 path 编号本身与排序字段一致（path 段 == 兄弟序号）
        assert path_of_id(6) == (1, 1, 1) and path_of_id(7) == (1, 1, 2)
        assert path_of_id(4) == (1, 2) and path_of_id(5) == (2, 1)

    def test_dfs_tie_break_by_id_not_created_at(self):
        """同 sort_order（脏数据）时按 id 决胜 —— created_at 秒级并列不影响。"""
        a = _row(9, "x", sort_order=1, created_at="2026-01-02")
        b = _row(8, "y", sort_order=1, created_at="2026-01-01")
        assert [r["id"] for r in order_tree_dfs([a, b])] == [8, 9]
        assert sibling_sort_key(a) > sibling_sort_key(b)

    def test_dfs_orphan_parent_does_not_drop_node(self):
        """孤儿节点（parent_id 指向不存在的父）挂根层，不丢节点。"""
        rows = [_row(1, "ok", sort_order=1), _row(2, "orphan", parent_id=999, sort_order=1)]
        out = order_tree_dfs(rows)
        assert sorted(r["id"] for r in out) == [1, 2]

    def test_dfs_parent_cycle_terminates(self):
        """parent_id 环不会死循环，且不丢节点。"""
        rows = [
            _row(1, "a", parent_id=2),
            _row(2, "b", parent_id=1),
            _row(3, "c", sort_order=1),
        ]
        out = order_tree_dfs(rows)
        assert sorted(r["id"] for r in out) == [1, 2, 3]
        assert len(out) == len(rows)

    def test_dfs_empty(self):
        assert order_tree_dfs([]) == []

    def test_find_duplicate_sibling_sort_orders(self):
        rows = [
            _row(1, "a", parent_id=10, sort_order=1),
            _row(2, "b", parent_id=10, sort_order=1),  # 同父重复
            _row(3, "c", parent_id=20, sort_order=1),
            _row(4, "d", parent_id=20, sort_order=2),
        ]
        dups = find_duplicate_sibling_sort_orders(rows)
        assert dups == [{"parent_id": 10, "sort_order": 1, "count": 2}]
        assert find_duplicate_sibling_sort_orders([]) == []

    def test_sibling_sort_key_accepts_objects(self):
        """sibling_sort_key 同时支持 dict 与 pydantic 风格对象节点。"""
        obj = SimpleNamespace(sort_order=2, id=11)
        assert sibling_sort_key(obj) == (2, 11)
        assert sibling_sort_key({"sort_order": "3", "id": "42"}) == (3, 42)
        assert sibling_sort_key(SimpleNamespace(sort_order=None, id=None)) == (0, 0)


class TestFourWayOrderConsistency:
    """真实 DB：落库 / 展示(HTML) / 导出指纹 / 正文生成 四方顺序一致。"""

    @pytest.fixture()
    def client(self):
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
        with TestClient(app) as c:
            yield c
        app.dependency_overrides.clear()

    @pytest.fixture()
    def tree_project(self, client):
        """两棵 L1 子树 + 各自 2 个 L2 + 每个 L2 下 2 个 L3（显式 sort_order 全序）。

        直接写库而非 POST /sections：① 端点限流（100 次/60s，测试共享）在批量
        建 11 个章节时会被其它用例误伤成 429；② 可精确控制 id 与 sort_order
        的错开关系（id 按插入序 1..11，sort_order 刻意与之不同）。
        """
        from app.services.sections import _invalidate_build_tree_cache

        resp = client.post(API, json={"name": "四方顺序一致性测试", "industry": "公共建筑"})
        assert resp.status_code in (200, 201), resp.text
        pid = resp.json()["id"]

        # (title, 父节点插入序, level, sort_order)；插入序 = id 递增序
        spec = [
            ("第二编 质量管理体系", None, 1, 2),   # 0 -> l1b（先建，id 最小）
            ("第一编 施工组织总设计", None, 1, 1),  # 1 -> l1a
            ("1.1 施工部署", 1, 2, 1),              # 2 -> l2a1
            ("1.2 进度计划", 1, 2, 2),              # 3 -> l2a2
            ("2.1 质量目标", 0, 2, 1),              # 4 -> l2b1
            ("2.2 验收标准", 0, 2, 2),              # 5 -> l2b2
            ("1.1.1 总平面布置", 2, 3, 1),          # 6 -> a1
            ("1.1.2 人员配置", 2, 3, 2),            # 7 -> a2
            ("1.2.1 关键线路", 3, 3, 1),            # 8 -> a3
            ("2.1.1 质量目标分解", 4, 3, 1),        # 9 -> b1
            ("2.2.1 验收程序", 5, 3, 1),            # 10 -> b2
        ]
        ids = []
        with get_db() as conn:
            for title, parent_idx, level, so in spec:
                parent_id = ids[parent_idx] if parent_idx is not None else None
                cur = conn.execute(
                    "INSERT INTO sections (project_id, parent_id, title, level, sort_order,"
                    " section_type, content, status, word_count)"
                    " VALUES (?, ?, ?, ?, ?, 'text', '', 'pending', 0)",
                    (pid, parent_id, title, level, so),
                )
                ids.append(cur.lastrowid)
            conn.commit()
        _invalidate_build_tree_cache(pid)

        # 期望目录树前序：L1 按 sort_order → L1-1 整棵子树 → L1-2 整棵子树
        return {
            "pid": pid,
            "ids": ids,
            "expected": [ids[1], ids[2], ids[6], ids[7], ids[3], ids[8],
                         ids[0], ids[4], ids[9], ids[5], ids[10]],
        }

    def test_load_sections_in_tree_order_is_dfs_preorder(self, tree_project):
        pid = tree_project["pid"]
        with get_db() as conn:
            rows = load_sections_in_tree_order(conn, pid)
        assert [r["id"] for r in rows] == tree_project["expected"]

    def test_creation_order_differs_from_tree_order(self, tree_project):
        """前置条件：id 创建序 != 目录树序（否则旧 ORDER BY id 无法被证伪）。"""
        pid = tree_project["pid"]
        with get_db() as conn:
            by_id = [r["id"] for r in conn.execute(
                "SELECT id FROM sections WHERE project_id=? ORDER BY id", (pid,)).fetchall()]
            by_level = [r["id"] for r in conn.execute(
                "SELECT id FROM sections WHERE project_id=? ORDER BY level, sort_order, id",
                (pid,)).fetchall()]
        assert by_id != tree_project["expected"]
        assert by_level != tree_project["expected"]

    def test_build_tree_sibling_order_uses_authority(self, tree_project):
        """build_tree 的兄弟排序与唯一权威一致（不再依赖 created_at 决胜）。"""
        from app.services.sections import _invalidate_build_tree_cache, build_tree

        pid = tree_project["pid"]
        _invalidate_build_tree_cache(pid)
        tree = build_tree(pid)

        flat = []

        def walk(nodes):
            for n in nodes:
                flat.append(n.id)
                walk(n.children)

        walk(tree)
        assert flat == tree_project["expected"]

    def test_no_false_duplicate_warning_on_clean_project(self, tree_project, caplog):
        """同父 sort_order 全序唯一时不得误报重复告警。

        回归：SectionTreeNode 无 parent_id 字段，若用节点 dump 分组会把全部节点
        归到 parent_id=None 一组、跨层级误判重复（11 个节点全部落一组）。
        """
        from app.services.sections import _invalidate_build_tree_cache, build_tree

        pid = tree_project["pid"]
        _invalidate_build_tree_cache(pid)
        with caplog.at_level("WARNING", logger="app.services.sections"):
            build_tree(pid)
        assert not [
            r for r in caplog.records
            if "sort_order 重复" in r.getMessage()
        ], "干净项目不应触发同父 sort_order 重复告警"

    def test_duplicate_sibling_sort_order_warns(self, tree_project, caplog):
        """同父 sort_order 真重复时必须告警（可观测而非静默）。"""
        from app.services.sections import _invalidate_build_tree_cache, build_tree

        pid = tree_project["pid"]
        with get_db() as conn:
            conn.execute(
                "UPDATE sections SET sort_order = 1 WHERE id = ?",
                (tree_project["ids"][7],),  # a2 与 a1 同为 l2a1 的兄弟
            )
            conn.commit()
        _invalidate_build_tree_cache(pid)
        with caplog.at_level("WARNING", logger="app.services.sections"):
            build_tree(pid)
        assert any("sort_order 重复" in r.getMessage() for r in caplog.records)

    def test_html_load_sections_in_tree_order(self, tree_project):
        """HTML 展示顺序 = 目录树顺序（回归：旧 ORDER BY level, sort_order）。"""
        from app.services.html_export import _load_sections_for_html

        pid = tree_project["pid"]
        rows = _load_sections_for_html(pid)
        assert [r["id"] for r in rows] == tree_project["expected"]

    def test_build_leaf_dicts_matches_tree_order(self, tree_project):
        """正文生成叶子顺序 = 目录树顺序中叶子子序列。"""
        pid = tree_project["pid"]
        with get_db() as conn:
            rows = conn.execute(
                "SELECT id, parent_id, title, level, sort_order, section_type,"
                " scoring_point_id, status, content, target_words FROM sections"
                " WHERE project_id = ? ORDER BY sort_order, id", (pid,)).fetchall()
        rows = [dict(r) for r in rows]
        by_id = {r["id"]: r for r in rows}
        children: dict = {}
        for r in rows:
            children.setdefault(r["parent_id"], []).append(r)
        leaves = _build_leaf_dicts(project_id=pid, sections_by_id=by_id, children_map=children)
        expected_leaves = [i for i in tree_project["expected"] if by_id[i]["level"] >= 3]
        assert [lf["id"] for lf in leaves] == expected_leaves

    def test_fingerprint_stable_and_reacts_to_tree_reorder(self, tree_project):
        """导出指纹：内容不变则稳定；目录重排则失效（顺序进指纹）。"""
        from app.services.export import _compute_content_fingerprint

        pid = tree_project["pid"]
        f1 = _compute_content_fingerprint(pid)
        assert f1
        assert _compute_content_fingerprint(pid) == f1, "内容未变指纹必须稳定（缓存可命中）"

        exp = tree_project["expected"]
        idx = {r: i for i, r in enumerate(exp)}
        # 互换两个**同父兄弟**（l2a1 / l2a2，均为 l1a 的子节点）的 sort_order
        l2a1 = [i for i in exp if idx[i] == 1][0]
        l2a2 = [i for i in exp if idx[i] == 4][0]
        with get_db() as conn:
            assert by_id_siblings(conn, l2a1, l2a2)
            conn.execute("UPDATE sections SET sort_order = 2 WHERE id = ?", (l2a1,))
            conn.execute("UPDATE sections SET sort_order = 1 WHERE id = ?", (l2a2,))
            conn.commit()
        f2 = _compute_content_fingerprint(pid)
        assert f2 != f1, "目录重排必须使导出缓存失效"

    def test_fingerprint_reacts_to_content_change(self, tree_project):
        from app.services.export import _compute_content_fingerprint

        pid = tree_project["pid"]
        f1 = _compute_content_fingerprint(pid)
        target = tree_project["expected"][2]
        with get_db() as conn:
            conn.execute(
                "UPDATE sections SET content = '新正文内容，用于触发指纹变化' WHERE id = ?",
                (target,))
            conn.commit()
        assert _compute_content_fingerprint(pid) != f1


def by_id_siblings(conn, a, b):
    """断言 a、b 为同父兄弟（保证 sort_order 互换是合法的重排）。"""
    ra = conn.execute("SELECT parent_id FROM sections WHERE id=?", (a,)).fetchone()
    rb = conn.execute("SELECT parent_id FROM sections WHERE id=?", (b,)).fetchone()
    assert ra and rb and ra["parent_id"] == rb["parent_id"]
    return True


# ======================= I. 遗留问题加固（2026-10-19） =======================


def _bucket_map(rows):
    """按 parent_id 分桶（与 sse_utils._load_project_data 同构）。"""
    cm = {}
    for r in rows:
        cm.setdefault(r["parent_id"], []).append(dict(r))
    return cm


def _so_of(conn, section_id):
    return conn.execute(
        "SELECT sort_order FROM sections WHERE id = ?", (section_id,)
    ).fetchone()["sort_order"]


class TestChildrenMapAudit:
    """遗留1：children_map 桶表自检（path 派生的唯一依据必须可信）。"""

    def test_clean_map_passes(self):
        rows = _two_subtree_leaves()
        res = audit_children_map(rows, _bucket_map(rows))
        assert res["ok"] is True, res
        assert res["total_rows"] == res["bucket_total"] == 4
        assert res["issues"] == [] and res["duplicates"] == []

    def test_unsorted_bucket_is_flagged(self):
        """桶内乱序 → path 编号会错位，必须告警。"""
        rows = _two_subtree_leaves()
        cm = _bucket_map(rows)
        cm[2] = [cm[2][1], cm[2][0]]  # 故意把 (12, 11) 倒过来
        res = audit_children_map(rows, cm)
        assert res["ok"] is False
        assert any(i["type"] == "bucket_not_sorted" for i in res["issues"]), res

    def test_missing_node_is_flagged(self):
        rows = _two_subtree_leaves()
        cm = _bucket_map(rows)
        cm[3] = [cm[3][0]]  # 丢一个节点
        res = audit_children_map(rows, cm)
        assert res["ok"] is False
        types = {i["type"] for i in res["issues"]}
        assert "bucket_total_mismatch" in types, res
        assert "node_coverage" in types, res

    def test_duplicate_node_is_flagged(self):
        rows = _two_subtree_leaves()
        cm = _bucket_map(rows)
        first = cm[3][0]
        cm[3] = [first, first]  # 节点被挂两次（总数仍相等，须靠 id 覆盖性发现）
        res = audit_children_map(rows, cm)
        assert res["ok"] is False
        assert any(i["type"] == "node_coverage" for i in res["issues"]), res

    def test_duplicate_sibling_sort_order_is_flagged(self):
        rows = [
            _leaf(1, "甲", "1.1", parent_id=None, sort_order=1),
            _leaf(2, "乙", "1.2", parent_id=None, sort_order=1),  # 同父重复
            _leaf(3, "丙", "1.3", parent_id=None, sort_order=3),
        ]
        res = audit_children_map(rows, _bucket_map(rows))
        assert res["ok"] is False
        assert any(i["type"] == "duplicate_sibling_sort_order" for i in res["issues"])
        assert res["duplicates"] == [{"parent_id": None, "sort_order": 1, "count": 2}]


class TestRepairDuplicateSortOrders:
    """遗留2：同父 sort_order 重复的一次性修复（幂等 + 最小改动面）。"""

    @pytest.fixture()
    def client(self):
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
        with TestClient(app) as c:
            yield c
        app.dependency_overrides.clear()

    @pytest.fixture()
    def tree_project(self, client):
        from app.services.sections import _invalidate_build_tree_cache

        resp = client.post(API, json={"name": "遗留加固-序号修复", "industry": "公共建筑"})
        assert resp.status_code in (200, 201), resp.text
        pid = resp.json()["id"]

        spec = [
            ("第一编 施工组织总设计", None, 1, 1),  # 0 -> l1a
            ("1.1 施工部署", 0, 2, 1),              # 1 -> l2a1
            ("1.2 进度计划", 0, 2, 2),              # 2 -> l2a2
            ("2.1 质量目标", 0, 2, 3),              # 3 -> l2a3（无重复的对照组）
            ("1.1.1 总平面布置", 1, 3, 1),           # 4 -> a1
            ("1.1.2 人员配置", 1, 3, 2),             # 5 -> a2
        ]
        ids = []
        with get_db() as conn:
            for title, parent_idx, level, so in spec:
                parent_id = ids[parent_idx] if parent_idx is not None else None
                cur = conn.execute(
                    "INSERT INTO sections (project_id, parent_id, title, level, sort_order,"
                    " section_type, content, status, word_count)"
                    " VALUES (?, ?, ?, ?, ?, 'text', '', 'pending', 0)",
                    (pid, parent_id, title, level, so),
                )
                ids.append(cur.lastrowid)
            conn.commit()
        _invalidate_build_tree_cache(pid)
        return {"pid": pid, "ids": ids}

    def _make_duplicate(self, pid, a, b):
        with get_db() as conn:
            by_id_siblings(conn, a, b)
            conn.execute("UPDATE sections SET sort_order = 1 WHERE id = ?", (a,))
            conn.execute("UPDATE sections SET sort_order = 1 WHERE id = ?", (b,))
            conn.commit()

    def test_dry_run_reports_without_writing(self, tree_project):
        pid = tree_project["pid"]
        a, b = tree_project["ids"][1], tree_project["ids"][2]
        self._make_duplicate(pid, a, b)

        with get_db() as conn:
            res = repair_duplicate_sibling_sort_orders(conn, pid, dry_run=True)

        assert res["dry_run"] is True
        assert res["groups_repaired"] == 1 and len(res["changes"]) == 1
        assert res["rows_updated"] == 0
        with get_db() as conn:
            # dry_run 不得写库
            assert _so_of(conn, a) == 1 and _so_of(conn, b) == 1
            assert find_duplicate_sibling_sort_orders(
                load_sections_in_tree_order(conn, pid))
        assert {c["id"] for c in res["changes"]} == {b}
        assert res["changes"][0]["new"] == 2

    def test_apply_fixes_and_is_idempotent(self, tree_project):
        pid = tree_project["pid"]
        a, b = tree_project["ids"][1], tree_project["ids"][2]
        self._make_duplicate(pid, a, b)

        with get_db() as conn:
            res = repair_duplicate_sibling_sort_orders(conn, pid, dry_run=False)
            conn.commit()

        assert res["rows_updated"] == 1
        with get_db() as conn:
            assert _so_of(conn, a) == 1 and _so_of(conn, b) == 2
            rows = load_sections_in_tree_order(conn, pid)
            assert find_duplicate_sibling_sort_orders(rows) == []
            second = repair_duplicate_sibling_sort_orders(conn, pid, dry_run=True)
        # 幂等：第二次无变更
        assert second["changes"] == [] and second["groups_repaired"] == 0

    def test_clean_groups_are_not_touched(self, tree_project):
        """无重复的组必须一字不动（最小改动面）。"""
        pid = tree_project["pid"]
        with get_db() as conn:
            res = repair_duplicate_sibling_sort_orders(conn, pid, dry_run=True)
        assert res["changes"] == [] and res["groups_repaired"] == 0


class TestReadonlyQueriesConverged:
    """遗留3：只读统计/列表查询收敛到目录树顺序（不再跨父交错）。"""

    @pytest.fixture()
    def client(self):
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
        with TestClient(app) as c:
            yield c
        app.dependency_overrides.clear()

    @pytest.fixture()
    def tree_project(self, client):
        from app.services.sections import _invalidate_build_tree_cache

        resp = client.post(API, json={"name": "遗留加固-只读查询", "industry": "公共建筑"})
        assert resp.status_code in (200, 201), resp.text
        pid = resp.json()["id"]

        spec = [
            ("第二编 质量管理体系", None, 1, 2),
            ("第一编 施工组织总设计", None, 1, 1),
            ("1.1 施工部署", 1, 2, 1),
            ("1.2 进度计划", 1, 2, 2),
            ("2.1 质量目标", 0, 2, 1),
            ("1.1.1 总平面布置", 2, 3, 1),
            ("1.1.2 人员配置", 2, 3, 2),
            ("1.2.1 关键线路", 3, 3, 1),
            ("2.1.1 质量目标分解", 4, 3, 1),
        ]
        ids = []
        with get_db() as conn:
            for title, parent_idx, level, so in spec:
                parent_id = ids[parent_idx] if parent_idx is not None else None
                cur = conn.execute(
                    "INSERT INTO sections (project_id, parent_id, title, level, sort_order,"
                    " section_type, content, status, word_count)"
                    " VALUES (?, ?, ?, ?, ?, 'text', '', 'pending', 0)",
                    (pid, parent_id, title, level, so),
                )
                ids.append(cur.lastrowid)
            conn.commit()
        _invalidate_build_tree_cache(pid)
        return {"pid": pid, "ids": ids,
                "expected": [ids[1], ids[2], ids[5], ids[6], ids[3], ids[7],
                             ids[0], ids[4], ids[8]]}

    def test_list_sections_light_is_dfs_preorder(self, tree_project):
        from app.services.perf_optimizations import list_sections_light

        got = list_sections_light(tree_project["pid"])
        assert [r["id"] for r in got] == tree_project["expected"]
        # 轻量查询仍不携带 content 列
        assert "content" not in got[0]

    def test_get_pending_sections_is_dfs_preorder(self, tree_project):
        from app.services.generation_manager import generation_manager

        pending = asyncio.run(generation_manager.get_pending_sections(tree_project["pid"]))
        assert [r["id"] for r in pending] == tree_project["expected"]
        # 排序所需字段必须随结果下发（否则 order_tree_dfs 无法收敛）
        for r in pending:
            assert "parent_id" in r and "sort_order" in r, r
        # 分页稳定：同一份数据翻页不重不漏
        assert len(pending[:5]) == 5
        assert [r["id"] for r in pending[:5]] == tree_project["expected"][:5]

    def test_pending_sections_excludes_completed(self, tree_project):
        """口径不变：仅未完成章节进入待恢复列表。

        父章节被排除后，其未完成子章节成为子树的孤儿根（`order_tree_dfs`
        的既定防御行为：挂到根层参与排序，不丢节点）。故此处断言的是
        「对**待恢复子集**的权威树序」，而非「全树序删除一项」。
        """
        from app.services.generation_manager import generation_manager

        pid = tree_project["pid"]
        ids = tree_project["ids"]
        done = tree_project["expected"][1]  # 排除一个中间层父章节

        with get_db() as conn:
            conn.execute("UPDATE sections SET status='completed' WHERE id=?", (done,))
            conn.commit()

        pending = asyncio.run(generation_manager.get_pending_sections(pid))
        assert done not in {r["id"] for r in pending}

        # 孤儿根上移后的权威序（用插入序下标，不假设自增 id 值）
        assert [r["id"] for r in pending] == [
            ids[1], ids[3], ids[7], ids[5], ids[0], ids[4], ids[8], ids[6]
        ]

        # 仍在集合内的父章节必须严格先于其子章节
        pos = {r["id"]: i for i, r in enumerate(pending)}
        for r in pending:
            if r["parent_id"] is not None and r["parent_id"] in pos:
                assert pos[r["parent_id"]] < pos[r["id"]]

        # 分页稳定性：重复查询结果完全一致（翻页不重不漏的前提）
        again = asyncio.run(generation_manager.get_pending_sections(pid))
        assert [r["id"] for r in again] == [r["id"] for r in pending]


class TestPreviousMarkerSingleSource:
    """遗留5：前序摘要标记由常量双向引用（注入端改写文案不再导致校验误报）。"""

    def _section(self, title="第一章"):
        return {
            "id": 1, "title": title, "level": 3, "parent_id": None,
            "sort_order": 1, "path": "1.1", "status": "pending",
            "section_type": "text", "scoring_point_id": None,
        }

    def test_marker_is_single_source_of_truth(self):
        """注入端产物必须可被校验端识别（真实往返，不各自硬编码文案）。"""
        assert PREVIOUS_SECTIONS_SUMMARY_MARKER in PREVIOUS_SECTIONS_SUMMARY_HEADER
        ctx = inject_previous_section_context(
            {}, [{"title": "第一章", "summary": "正文摘要"}])
        assert PREVIOUS_SECTIONS_SUMMARY_MARKER in ctx["previous_sections_summary"]

        # 与生产调用形态一致：build_chapter_content_messages(section, **context)
        section = self._section("第二章")
        messages = build_chapter_content_messages(
            section, previous_sections_summary=ctx["previous_sections_summary"])
        res = audit_message_stack(messages, section, require_previous=True)
        assert res["ok"] is True, res
        assert not any(
            w["type"] == "missing_previous_context" for w in res["warnings"]
        ), res["warnings"]

    def test_missing_previous_still_warns(self):
        """未注入时仍必须告警（常量收敛不得让校验失效）。"""
        section = self._section("第一章")
        messages = build_chapter_content_messages(section)
        res = audit_message_stack(messages, section, require_previous=True)
        assert any(w["type"] == "missing_previous_context" for w in res["warnings"])
        assert res["ok"] is True, "前序摘要缺失为 warn 级，不阻断生成"


class TestOfflineE2E:
    """遗留6：无 AI Key 也能跑通顺序生成全链路（离线 mock，零网络）。

    覆盖 真实 DB → 目录树加载 → 批次划分 → 前序摘要注入 → 消息栈审计
    → 三模式生成 → 完成顺序校验 → 与导出/展示同源顺序比对。
    """

    @pytest.fixture()
    def client(self):
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1)
        with TestClient(app) as c:
            yield c
        app.dependency_overrides.clear()

    @pytest.fixture()
    def tree_project(self, client):
        from app.services.sections import _invalidate_build_tree_cache

        resp = client.post(API, json={"name": "遗留加固-离线E2E", "industry": "公共建筑"})
        assert resp.status_code in (200, 201), resp.text
        pid = resp.json()["id"]

        spec = [
            ("第二编 质量管理体系", None, 1, 2),
            ("第一编 施工组织总设计", None, 1, 1),
            ("1.1 施工部署", 1, 2, 1),
            ("1.2 进度计划", 1, 2, 2),
            ("2.1 质量目标", 0, 2, 1),
            ("1.1.1 总平面布置", 2, 3, 1),
            ("1.1.2 人员配置", 2, 3, 2),
            ("1.2.1 关键线路", 3, 3, 1),
            ("2.1.1 质量目标分解", 4, 3, 1),
        ]
        ids = []
        with get_db() as conn:
            for title, parent_idx, level, so in spec:
                parent_id = ids[parent_idx] if parent_idx is not None else None
                cur = conn.execute(
                    "INSERT INTO sections (project_id, parent_id, title, level, sort_order,"
                    " section_type, content, status, word_count)"
                    " VALUES (?, ?, ?, ?, ?, 'text', '', 'pending', 0)",
                    (pid, parent_id, title, level, so),
                )
                ids.append(cur.lastrowid)
            conn.commit()
        _invalidate_build_tree_cache(pid)

        expected_leaves = [ids[5], ids[6], ids[7], ids[8]]
        return {"pid": pid, "ids": ids, "expected_leaves": expected_leaves}

    def _leaves(self, pid, expected):
        with get_db() as conn:
            rows = load_sections_in_tree_order(conn, pid)
        leaves = [dict(r) for r in rows if r["level"] == 3]
        # path 为运行时派生字段（无 DB 列），从标题前缀回填
        for s in leaves:
            s.setdefault("path", str(s.get("title", "")).split()[0])
        assert [s["id"] for s in leaves] == expected
        return leaves

    def test_e2e_all_modes_offline(self, tree_project):
        pid = tree_project["pid"]
        leaves = self._leaves(pid, tree_project["expected_leaves"])
        expected = tree_project["expected_leaves"]

        for mode in ("batch", "serial", "parallel"):
            provider = FlakyProvider()
            tracker = GenerationOrderTracker()

            async def _go(_p=provider, _t=tracker, _m=mode, _lv=leaves, _pid=pid):
                return await generate_sections(
                    _p, _lv,
                    context={"generation_mode": _m, "use_cache": False, "project_id": _pid},
                    concurrency=2,
                    order_tracker=_t,
                )

            results = asyncio.run(_go())
            assert all(r["status"] == "completed" for r in results), (mode, results)
            assert all((r["content"] or "").strip() for r in results), mode
            # 结果顺序 == 目录树顺序（生成侧顺序 = 落库/展示/导出侧顺序）
            assert [r["id"] for r in results] == expected, (mode, results)
            ver = tracker.verify(expected, mode=mode)
            assert ver["ok"], (mode, ver)
            # 完整度综合校验（顺序 + 完整性 + 质量）
            comp = verify_generation_completeness(
                results, expected, order_tracker=tracker, generation_mode=mode)
            assert comp["ok"], (mode, comp)

    def test_e2e_batch_injects_previous_summary(self, tree_project):
        """batch 模式下后续章节必须拿到前序摘要（顺序生成核心收益）。"""
        pid = tree_project["pid"]
        leaves = self._leaves(pid, tree_project["expected_leaves"])
        last_leaf_title = str(leaves[-1]["title"]).split()[0]

        provider = FlakyProvider()
        tracker = GenerationOrderTracker()

        async def _go():
            return await generate_sections(
                provider, leaves,
                context={"generation_mode": "batch", "use_cache": False, "project_id": pid},
                concurrency=2, order_tracker=tracker,
            )

        results = asyncio.run(_go())
        assert all(r["status"] == "completed" for r in results)
        later = [st for st in provider.stacks
                 if any(getattr(m, "role", "") == "user" and str(m.content).startswith(f"# 章节: {last_leaf_title}")
                        for m in st)]
        assert later, f"未找到 {last_leaf_title} 的提示词"
        joined = "\n".join(str(m.content) for m in later[0])
        assert PREVIOUS_SECTIONS_SUMMARY_MARKER in joined

    def test_e2e_generation_order_matches_export_and_html(self, tree_project):
        """离线跑通后，生成顺序必须与导出指纹源 / HTML 展示源同源一致。"""
        from app.services.html_export import _load_sections_for_html

        pid = tree_project["pid"]
        expected = tree_project["expected_leaves"]
        leaf_set = set(expected)

        with get_db() as conn:
            export_rows = load_sections_in_tree_order(conn, pid)

        # 叶子子序列（生成侧真实遍历顺序）== 导出源叶子子序列
        assert [r["id"] for r in export_rows if r["id"] in leaf_set] == expected
        # HTML 展示源的叶子子序列同样一致
        assert [s["id"] for s in _load_sections_for_html(pid) if s["id"] in leaf_set] == expected
        # 前序性质：任一父章节必须严格先于其全部子章节
        pos = {r["id"]: i for i, r in enumerate(export_rows)}
        for r in export_rows:
            if r["parent_id"] is not None and r["parent_id"] in pos:
                assert pos[r["parent_id"]] < pos[r["id"]], (
                    f"父章节 {r['parent_id']} 未先于子章节 {r['id']}，前序性质被破坏"
                )
