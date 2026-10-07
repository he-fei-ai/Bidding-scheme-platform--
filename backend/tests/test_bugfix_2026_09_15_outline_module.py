"""目录生成模块缺陷修复回归测试（BUG-FIX 2026-09-15）

深度探索目录生成模块（V5.1 SSE 链路 → 工作流 → 门禁 → 提示词）后落地的四项修复：

1. `generate_outline_v51(reference_outline=...)` 形参从未被读取 —— 路由层从
   `outline_common._get_template_list_json()` 取出现成的 28 章模板 JSON 一路传进来，
   却被静默丢弃，实际用的是硬编码的 23 章 `REFERENCE_TEMPLATE_V51`。三份「固定
   参考目录」（CORE_28_CHAPTERS / FIXED_28_CHAPTERS / REFERENCE_TEMPLATE_V51）
   长期漂移且无人察觉。
2. 生成侧（L1 提示词 + `_check_scoring_coverage`）强制一级目录数 = 评分项数，
   而审核侧提示词（`OUTLINE_REVIEW_SYSTEM/USER`）明确豁免该检查 —— 两套标准相反，
   审核门禁对「评分项漏映射」完全失明。
3. `parse_word_budget` 把区间两侧无条件按同一量级处理，`10000~2万字` 的前侧被乘
   10000 得到 1 亿字；且无最小可信值护栏，招标全文里「每页 500 字」被当成全文
   预算，`derive_target_leaf_count` 推出 1 个目标叶子，leaf-count 检查随即对一份
   几百节点的正常目录误报。
4. `sse_outline.py` 20+ 未使用导入 / 3 处孤立 `time.time()` 死语句 / 冗余
   `nonlocal`；`_generate_level3_v51` 的 docstring 自称流水线，实际是全量串行后处理。

按项目约定：测行为与可验证事实，不测实现细节。
"""

import ast
import inspect
import json
from pathlib import Path

import pytest

_BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _read_source(rel: str) -> str:
    return (_BACKEND_ROOT / rel).read_text(encoding="utf-8")


# ===========================================================================
# 1. 字数预算解析：区间单位 + 噪声护栏
# ===========================================================================


class TestWordBudgetParsing:
    """`outline_gate.parse_word_budget` 的区间单位与噪声护栏。"""

    def test_range_with_mixed_unit_notation_keeps_absolute_side(self):
        """「10000~2万字」前侧已是绝对值，不得再乘 10000。

        原实现取 `unit = low_unit or high_unit` 后两侧同乘，前侧 10000 变成
        1 亿字，目标叶子数被推导成 16670。
        """
        from app.services.ai.outline_gate import derive_target_leaf_count, parse_word_budget

        budget = parse_word_budget("技术方案 10000~2万字")
        assert budget["min_words"] == 10000
        assert budget["max_words"] == 20000
        leaf = derive_target_leaf_count(**budget)
        assert leaf is not None and leaf < 100, f"目标叶子数仍异常: {leaf}"

    def test_range_with_wan_on_both_sides(self):
        from app.services.ai.outline_gate import parse_word_budget

        budget = parse_word_budget("篇幅 1万~2万字")
        assert (budget["min_words"], budget["max_words"]) == (10000, 20000)

    def test_range_with_omitted_unit_on_low_side(self):
        """「1~2万字」前侧省略了「万」，应补成 1 万~2 万。"""
        from app.services.ai.outline_gate import parse_word_budget

        budget = parse_word_budget("篇幅 1~2万字")
        assert (budget["min_words"], budget["max_words"]) == (10000, 20000)

    def test_range_with_no_unit(self):
        from app.services.ai.outline_gate import parse_word_budget

        budget = parse_word_budget("篇幅 10000~20000字")
        assert (budget["min_words"], budget["max_words"]) == (10000, 20000)

    @pytest.mark.parametrize(
        "noise",
        [
            "投标文件每页 500 字，双面打印",
            "技术方案不超过 100 页，每页约 800 字",
            "正文小四号字，每行 30 字",
        ],
    )
    def test_page_level_noise_is_not_treated_as_budget(self, noise):
        """「每页 N 字」是排版格式要求，不是全文预算 —— 必须整体判为无法解析。

        否则 `derive_target_leaf_count` 会推出个位数目标叶子，leaf-count 检查
        对正常目录报出「叶子数严重不足」的误报。
        """
        from app.services.ai.outline_gate import derive_target_leaf_count, parse_word_budget

        budget = parse_word_budget(noise)
        assert budget == {"min_words": None, "max_words": None, "total_words": None}
        assert derive_target_leaf_count(**budget) is None

    def test_real_budget_still_parsed(self):
        """护栏不能误伤正常写法。"""
        from app.services.ai.outline_gate import parse_word_budget

        assert parse_word_budget("技术方案不少于 20000 字")["min_words"] == 20000
        assert parse_word_budget("全文约 3 万字")["total_words"] == 30000


# ===========================================================================
# 2. reference_outline 死参数修复
# ===========================================================================


class TestReferenceOutlineWiring:
    """调用方传入的参考目录必须真正生效，而非被静默丢弃。"""

    def test_route_template_reaches_workflow(self):
        """路由层 `_get_template_list_json()` 的 28 章模板应原样进入提示词参考段。"""
        from app.services.ai.outline_common import _get_template_list_json
        from app.services.ai.workflows_outline_v51 import _resolve_reference_json

        route_json = _get_template_list_json()
        resolved = json.loads(_resolve_reference_json(route_json))
        assert len(resolved) == 28, "reference_outline 未生效（仍回退内置 23 章模板）"
        assert resolved[0]["title"] == "项目概述"

    def test_empty_falls_back_to_builtin(self):
        from app.services.ai.workflows_outline_v51 import (
            REFERENCE_TEMPLATE_V51,
            _resolve_reference_json,
        )

        for empty in ("", None, "   ", []):
            resolved = json.loads(_resolve_reference_json(empty))
            assert len(resolved) == len(REFERENCE_TEMPLATE_V51)

    def test_malformed_json_falls_back_instead_of_raising(self):
        """参考目录是「锦上添花」，非法输入不能炸掉整条生成链路。"""
        from app.services.ai.workflows_outline_v51 import (
            REFERENCE_TEMPLATE_V51,
            _resolve_reference_json,
        )

        resolved = json.loads(_resolve_reference_json("{不是合法 JSON"))
        assert len(resolved) == len(REFERENCE_TEMPLATE_V51)

    def test_accepts_parsed_list_and_slims_fields(self):
        from app.services.ai.workflows_outline_v51 import _resolve_reference_json

        resolved = json.loads(
            _resolve_reference_json(
                [
                    {"id": "1", "title": "项目概述", "zone": "总纲", "extra": "噪声"},
                    {"id": "2", "title": "   "},
                    "不是 dict",
                ]
            )
        )
        assert resolved == [{"id": "1", "title": "项目概述"}]

    def test_main_entry_actually_uses_the_resolver(self):
        """源码级：主入口必须调用解析器，而不是硬编码内置模板。"""
        src = _read_source("app/services/ai/workflows_outline_v51.py")
        assert "reference_json = _resolve_reference_json(reference_outline)" in src
        assert "reference_json = json.dumps(REFERENCE_TEMPLATE_V51, ensure_ascii=False)" not in src


# ===========================================================================
# 3. 生成侧 / 审核侧对「一级目录数量」的判据统一
# ===========================================================================


class TestOutlineCountCriterionConsistency:
    """生成侧强制「一级目录数 = 评分项数」，审核侧不得反过来豁免该检查。"""

    def test_generation_side_still_requires_one_to_one(self):
        from app.services.ai.prompts import get_prompt

        level1_prompt = get_prompt("OUTLINE_V51_LEVEL1_SYSTEM")
        assert "章节数量与评分项数量严格相等" in level1_prompt
        # 参考模板不得反过来改变章节数量（新增约束）
        assert "不得据此改变章节数量" in level1_prompt

    @pytest.mark.parametrize(
        "prompt_name",
        ["OUTLINE_REVIEW_SYSTEM", "OUTLINE_REVIEW_USER"],
    )
    def test_review_side_no_longer_exempts_count_check(self, prompt_name):
        """原提示词写「不要求一级目录与评分点一一对应」「不需要检查数量」，
        与生成侧的硬校验直接矛盾，导致审核对「评分项漏映射」失明。"""
        from app.services.ai.prompts import get_prompt

        text = get_prompt(prompt_name)
        assert "不要求一级目录与评分点一一对应" not in text
        assert "不要求与评分点一一对应" not in text
        assert "不需要**检查" not in text
        assert "**不检查**一级目录数量" not in text
        # 必须改为要求核对数量
        assert "评分点覆盖" in text
        assert "差异若超过 1" in text or "差异 ≤1" in text

    def test_review_side_checks_count_without_hard_failing(self):
        """数量差异按警告报告而非一票否决 —— 审核是事后检查，且用户可能手动增删章节。"""
        from app.services.ai.prompts import get_prompt

        text = get_prompt("OUTLINE_REVIEW_SYSTEM")
        assert "不单独触发 `passed=false`" in text


# ===========================================================================
# 4. 静态清理与文档如实性
# ===========================================================================


class TestOutlineSseStaticHygiene:
    """`sse_outline.py` 的死代码与未使用导入。"""

    @staticmethod
    def _module_ast():
        return ast.parse(_read_source("app/routers/sse/sse_outline.py"))

    def test_no_orphan_expression_statements(self):
        """`time.time()` 这类无副作用的孤立表达式语句是拆分脚本的残留，必须清掉。"""
        tree = self._module_ast()
        orphans = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                func = node.value.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name in ("time", "time_ns", "monotonic"):
                    orphans.append(node.lineno)
        assert not orphans, f"sse_outline.py 仍存在孤立时间调用语句: {orphans}"

    def test_no_redundant_nonlocal_for_list_holder(self):
        """`outline_finished` 是列表容器（靠下标写入），`nonlocal` 声明是多余的。"""
        src = _read_source("app/routers/sse/sse_outline.py")
        assert "nonlocal outline_finished" not in src

    def test_unused_imports_removed(self):
        src = _read_source("app/routers/sse/sse_outline.py")
        for dead in ("import hashlib", "import logging", "from ...db import get_db"):
            assert dead not in src, f"未使用导入残留: {dead}"

    def test_still_exposes_public_entrypoints(self):
        """清理不能误删 sse/__init__.py 依赖的两个入口。"""
        src = _read_source("app/routers/sse/sse_outline.py")
        assert "async def generate_outline_sse_impl(" in src
        assert "async def _outline_main_loop(" in src
        assert "_task_lock.acquire" in src
        assert "task_snapshot.register" in src


class TestGateCategoryNormalization:
    """门禁策略表的类别名归一化。

    AI 自检提示词要求的是下划线写法（`parent_word_leak`），宿主产出的是连字符
    写法（`score-coverage`），模型还会自由发挥出 `Score_Coverage` 这类变体。
    不做归一化时 `_policy` 取不到策略表条目、落到默认 `("warning", True)` ——
    既比设计严重度更严格，又会因 `confirmation_required=True` 让 `derive_status`
    把整份裁决判成 `user_feedback`（要求用户逐条确认）。
    """

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("parent_word_leak", ("warning", False)),
            ("parent-word-leak", ("warning", False)),
            ("Parent_Word_Leak", ("warning", False)),
            ("generic_l3", ("warning", False)),
            ("generic-l3", ("warning", False)),
            ("mixed_lang", ("warning", False)),
            ("score_coverage", ("warning", True)),
            ("Score_Coverage", ("warning", True)),
            ("score-coverage", ("warning", True)),
        ],
    )
    def test_alias_forms_hit_policy_table(self, raw, expected):
        from app.services.ai.outline_gate import _policy, normalize_category

        assert _policy(raw) == expected, f"{raw} 未归一到策略表（norm={normalize_category(raw)}）"

    def test_unknown_category_stays_conservative(self):
        from app.services.ai.outline_gate import _policy

        assert _policy("完全没见过的类别") == ("warning", True)

    def test_machine_fixable_categories_do_not_force_user_feedback(self):
        """可机械修复的 AI 问题不应把裁决推成「必须用户逐条确认」。"""
        from app.services.ai.outline_gate import derive_status, map_ai_issues

        mapped = map_ai_issues(
            [
                {"type": "parent_word_leak", "node_id": "1.2.3", "fix": "改为独立标准描述"},
                {"type": "generic-l3", "target": "2.1.1", "fix": "注入技术手段"},
            ]
        )
        assert derive_status(mapped) == "simple_fix"

    def test_ai_issue_without_problem_gets_readable_label(self):
        """自检提示词只要求 type/target/fix，缺 problem 时不得把英文类别名当描述。"""
        from app.services.ai.outline_gate import map_ai_issues

        mapped = map_ai_issues([{"type": "parent_word_leak", "node_id": "1.2.3"}])
        assert mapped[0]["problem"] == "三级标题泄漏了父级词汇"

    def test_ai_supplied_problem_is_preserved(self):
        from app.services.ai.outline_gate import map_ai_issues

        mapped = map_ai_issues([{"type": "overlap", "problem": "自定义描述", "fix": "x"}])
        assert mapped[0]["problem"] == "自定义描述"


class TestReferenceTemplateConsistency:
    """三份「固定参考目录」的一致性护栏。

    它们分散在三处且内容不同步，历史上「改参考目录只改一处」无人察觉。
    这里把**当前已知差异**显式固化成断言：任何新增漂移都会让用例失败，
    迫使改动者明确决定「同步」还是「登记为例外」，而不是静默分叉。

    ⚠️ 改动 `CORE_28_CHAPTERS` / `FIXED_28_CHAPTERS` / `REFERENCE_TEMPLATE_V51`
    中任何一份时，必须同步更新本类的三张已知差异表。
    """

    # FIXED 与 CORE 同 id 标题不同的项（CORE 版本 → 更详细，多「（包括投资监理）」）
    KNOWN_CORE_FIXED_TITLE_DIVERGENCE = {
        23: "与发包人、项目管理单位、监理（包括投资监理）及设计人的配合",
    }

    # V51 兜底模板相对 FIXED_28 缺少的章（有意精简，非缺陷）
    KNOWN_V51_MISSING = {
        "文明施工措施计划",
        "拟配备本工程的试验和检测仪器设备表",
        "任何可能的紧急情况的处理措施、预案以及抵抗风险的措施",
        "与发包人、项目管理单位、监理及设计人的配合",
        "对总包管理及专业分包配合方案／与各参建方配合",
        "其他",
    }

    # V51 兜底模板对 FIXED 标题的改写（FIXED 原标题 → V51 措辞）
    KNOWN_V51_REWRITTEN = {
        "对总包管理及专业分包配合方案／与各参建方配合": "与各参建方的配合协调",
    }

    @staticmethod
    def _fixed():
        from app.services.ai.outline_common import FIXED_28_CHAPTERS

        return FIXED_28_CHAPTERS

    @staticmethod
    def _core():
        from app.services.ai.outline_sort_optimizer import CORE_28_CHAPTERS

        return CORE_28_CHAPTERS

    def test_core_and_fixed_share_same_id_sequence(self):
        """两份 28 章模板的 id 序列必须逐位相同，否则排序器与骨架构建会错位。"""
        fixed_ids = [c["id"] for c in self._fixed()]
        core_ids = [c["id"] for c in self._core()]
        assert fixed_ids == core_ids
        assert len(fixed_ids) == 28

    def test_core_fixed_title_divergence_is_exactly_known(self):
        """同 id 标题的差异集合必须与登记表完全一致 —— 新增差异即失败。"""
        diverged = {
            int(f["id"]): c["title"]
            for f, c in zip(self._fixed(), self._core())
            if f["title"] != c["title"]
        }
        assert diverged == self.KNOWN_CORE_FIXED_TITLE_DIVERGENCE, (
            "CORE_28_CHAPTERS 与 FIXED_28_CHAPTERS 出现未登记的新标题差异："
            f"{diverged}；请同步两份模板或更新 KNOWN_CORE_FIXED_TITLE_DIVERGENCE"
        )

    def test_v51_template_missing_set_is_exactly_known(self):
        from app.services.ai.workflows_outline_v51 import REFERENCE_TEMPLATE_V51

        v51_titles = [c["title"] for c in REFERENCE_TEMPLATE_V51]
        fixed_titles = [c["title"] for c in self._fixed()]
        missing = {t for t in fixed_titles if t not in v51_titles}
        assert missing == self.KNOWN_V51_MISSING, (
            "REFERENCE_TEMPLATE_V51 相对 FIXED_28_CHAPTERS 的缺失集合发生变化："
            f"{missing}；请同步或更新 KNOWN_V51_MISSING"
        )

    def test_v51_template_rewrites_are_exactly_known(self):
        from app.services.ai.workflows_outline_v51 import REFERENCE_TEMPLATE_V51

        v51_titles = [c["title"] for c in REFERENCE_TEMPLATE_V51]
        fixed_titles = [c["title"] for c in self._fixed()]
        extra = [t for t in v51_titles if t not in fixed_titles]
        rewritten = {
            original: new
            for original, new in self.KNOWN_V51_REWRITTEN.items()
            if new in extra
        }
        assert rewritten == self.KNOWN_V51_REWRITTEN, (
            f"REFERENCE_TEMPLATE_V51 的改写项发生变化：{extra}；"
            "请同步或更新 KNOWN_V51_REWRITTEN"
        )

    def test_v51_count_follows_from_missing_and_rewrite(self):
        """23 = 28 - 缺 6 + 改写 1（改写项补回一个位置）。"""
        from app.services.ai.workflows_outline_v51 import REFERENCE_TEMPLATE_V51

        assert len(REFERENCE_TEMPLATE_V51) == 28 - len(self.KNOWN_V51_MISSING) + 1

    def test_zone_map_covers_every_core_title(self):
        """排序器的 zone 映射必须覆盖 CORE 全部标题，否则分区排序会静默降级。"""
        from app.services.ai.outline_sort_optimizer import CORE_ZONE_MAP, ZONE_ORDER

        for chapter in self._core():
            zone = CORE_ZONE_MAP.get(chapter["title"])
            assert zone is not None, f"{chapter['title']!r} 没有分区映射"
            assert zone in ZONE_ORDER, f"分区 {zone!r} 不在 ZONE_ORDER 里"

    def test_fixed_23_divergence_actually_breaks_zone_lookup(self):
        """记录一个真实后果：FIXED 的 id=23 标题查不到 zone。

        这不是要修的 bug，而是 id=23 标题分叉的既有影响 —— 固化下来，
        避免有人误以为 CORE_ZONE_MAP 能覆盖 FIXED 的全部章节。
        """
        from app.services.ai.outline_sort_optimizer import CORE_ZONE_MAP

        fixed_23 = next(c for c in self._fixed() if c["id"] == "23")
        assert fixed_23["title"] not in CORE_ZONE_MAP


class TestSortOptimizerStableOrdering:
    """`outline_sort_optimizer` 降级排序的稳定性。

    该模块目前是孤儿（无生产调用方），但它的降级路径有个真实缺陷：补充章的
    tie-breaker 用的是 `title`，导致同分区章节按**标题字典序**乱排。而 V5.1 的
    L1 输出是「评分项专业化改写」标题，实测 8/8 都命中不了 `CORE_TITLE_SET`
    （`is_core` 全为 False），于是全部落到补充章分支 —— AI 按
    「总纲→组织→方案→管理→收尾」排好的顺序会被字典序彻底打乱。
    若将来接线本模块，这个缺陷会直接破坏目录顺序。
    """

    # 真实 L1 输出风格的标题（评分项专业化改写，非参考模板原文）
    L1_STYLE_TITLES = [
        "项目总体概况与建设条件分析",
        "施工组织部署与资源配置方案",
        "主要分部分项工程施工技术方案",
        "质量保证体系与过程控制措施",
        "安全生产管理与文明施工保障",
        "施工进度计划与工期保证措施",
        "环境保护与绿色施工措施",
        "售后服务与工程保修承诺",
    ]

    @staticmethod
    def _outline(titles):
        return [{"id": str(i + 1), "title": t} for i, t in enumerate(titles)]

    def test_real_l1_titles_are_not_recognised_as_core(self):
        """固化前提：改写标题命中不了核心章集合，所以降级路径是主路径。"""
        from app.services.ai.outline_sort_optimizer import CORE_TITLE_SET

        hits = [t for t in self.L1_STYLE_TITLES if t in CORE_TITLE_SET]
        assert hits == [], f"用例前提失效，这些标题被当成了核心章: {hits}"

    def test_v1_fallback_is_stable_for_real_l1_titles(self):
        from app.services.ai.outline_sort_optimizer import CORE_TITLE_SET, OutlineSortOptimizer

        opt = OutlineSortOptimizer(provider=None)
        outline = self._outline(self.L1_STYLE_TITLES)
        enriched = opt._enrich_zones(outline, CORE_TITLE_SET)
        after = [c["title"] for c in opt._fallback_sort_by_zone(enriched, CORE_TITLE_SET)]
        assert after == self.L1_STYLE_TITLES, f"同分区章节被乱排: {after}"

    def test_v1_fallback_still_regroups_across_zones(self):
        """稳定化不能把「跨分区归组」也一起废掉。"""
        from app.services.ai.outline_sort_optimizer import CORE_TITLE_SET, OutlineSortOptimizer

        opt = OutlineSortOptimizer(provider=None)
        shuffled = [
            "售后服务与工程保修承诺",
            "项目总体概况与建设条件分析",
            "质量保证体系与过程控制措施",
        ]
        enriched = opt._enrich_zones(self._outline(shuffled), CORE_TITLE_SET)
        after = [c["title"] for c in opt._fallback_sort_by_zone(enriched, CORE_TITLE_SET)]
        assert after[0] == "项目总体概况与建设条件分析", after

    def test_v1_fallback_preserves_core_chapter_order(self):
        from app.services.ai.outline_sort_optimizer import (
            CORE_28_CHAPTERS,
            CORE_TITLE_SET,
            OutlineSortOptimizer,
        )

        opt = OutlineSortOptimizer(provider=None)
        core = [{"id": c["id"], "title": c["title"]} for c in CORE_28_CHAPTERS]
        enriched = opt._enrich_zones(core, CORE_TITLE_SET)
        after = [c["title"] for c in opt._fallback_sort_by_zone(enriched, CORE_TITLE_SET)]
        assert after == [c["title"] for c in CORE_28_CHAPTERS]

    def test_v2_composite_is_stable_without_weights(self):
        from app.services.ai.outline_sort_optimizer import (
            CORE_TITLE_SET,
            OutlineSortOptimizerV2,
        )

        v2 = OutlineSortOptimizerV2(provider=None)
        enriched = v2._enrich_zones(self._outline(self.L1_STYLE_TITLES), CORE_TITLE_SET)
        after = [c["title"] for c in v2._composite_sort(enriched, CORE_TITLE_SET, {})]
        assert after == self.L1_STYLE_TITLES, f"V2 同分区章节被乱排: {after}"

    def test_v2_score_weight_still_reorders_within_zone(self):
        """稳定化不能把「同分区内按评分权重前移」的设计意图废掉。"""
        from app.services.ai.outline_sort_optimizer import (
            CORE_TITLE_SET,
            OutlineSortOptimizerV2,
        )

        v2 = OutlineSortOptimizerV2(provider=None)
        same_zone = [
            "环境保护与绿色施工措施",
            "质量保证体系与过程控制措施",
            "安全生产管理与文明施工保障",
        ]
        outline = self._outline(same_zone)
        zones = {e["zone"] for e in v2._enrich_zones(outline, CORE_TITLE_SET)}
        assert len(zones) == 1, f"用例前提失效：三章应同分区，实为 {zones}"

        weights = {"安全生产管理与文明施工保障": 30.0}
        enriched = v2._enrich_zones(outline, CORE_TITLE_SET)
        after = [c["title"] for c in v2._composite_sort(enriched, CORE_TITLE_SET, weights)]
        assert after[0] == "安全生产管理与文明施工保障", after
        assert set(after) == set(same_zone), "排序不得增删章节"

    def test_v2_never_crosses_zone_boundary_by_weight_alone(self):
        """固化既有设计意图：评分权重只在同分区内生效，不能跨越分区边界。

        这不是缺陷 —— `_composite_sort` 的注释明确写「同分区内评分权重高的
        适当前移」。用例存在的意义是：若有人调整权重公式，会立刻看到这层语义。
        """
        from app.services.ai.outline_sort_optimizer import (
            CORE_TITLE_SET,
            OutlineSortOptimizerV2,
        )

        v2 = OutlineSortOptimizerV2(provider=None)
        outline = self._outline(self.L1_STYLE_TITLES)
        weights = {"售后服务与工程保修承诺": 30.0}
        enriched = v2._enrich_zones(outline, CORE_TITLE_SET)
        after = [c["title"] for c in v2._composite_sort(enriched, CORE_TITLE_SET, weights)]
        # 售后服务属「专项预案」区，权重不足以把它提到「管理保障」区之前
        assert after.index("售后服务与工程保修承诺") > after.index("环境保护与绿色施工措施")


class TestLevel2PromptDisallowedWords:
    """L2 提示词的「禁用词」判据必须与 `L3_TITLE_REPAIR_PROMPT` 一致。"""

    def test_disallowed_words_are_scoped_to_title_core(self):
        """原措辞只说「禁用词」，与 L3 修复提示词的「不得作为标题核心」不一致，
        会让模型把「施工准备/质量控制/验收标准」等标准标书用词整条回避。"""
        from app.services.ai.prompts import get_prompt

        text = get_prompt("OUTLINE_V51_LEVEL2_SYSTEM")
        assert "禁用词（判据：不得作为标题核心）" in text
        # 必须给出正反例与自检判据
        assert "❌" in text and "✅" in text
        assert "具体的工程部位" in text

    def test_l3_repair_prompt_uses_same_criterion(self):
        from app.services.ai.prompts import get_prompt

        text = get_prompt("L3_TITLE_REPAIR_PROMPT")
        assert "作为标题核心" in text


class TestLevel3DocstringHonesty:
    """`_generate_level3_v51` 的 docstring 必须如实描述实现。"""

    def test_docstring_no_longer_claims_pipeline(self):
        from app.services.ai.workflows_outline_v51 import _generate_level3_v51

        doc = inspect.getdoc(_generate_level3_v51) or ""
        title = doc.splitlines()[0]
        # 标题行不得再自称「流水线生成」，须如实说明是全量串行后处理
        assert "流水线生成" not in title
        assert "全量串行后处理" in title
        # 正文应说明 l2_done_queue 形参当前未被消费（而非继续宣称流水线语义）
        assert "l2_done_queue" in doc
        assert "从未被消费" in doc

    def test_docstring_states_no_ai_call(self):
        from app.services.ai.workflows_outline_v51 import _generate_level3_v51

        doc = inspect.getdoc(_generate_level3_v51) or ""
        assert "完全不调用 AI" in doc


def _l1_outline(titles: list[str]) -> list[dict]:
    """把一级标题列表拼成最小目录（无子节点），只用于分区顺序检测"""
    return [{"id": str(i + 1), "title": t, "children": []} for i, t in enumerate(titles)]


# 标准叙事顺序：宏观→前期→技术→管理→进度→资源→预案→协调→收尾
_ASCENDING_9 = [
    "项目概述",
    "编制依据与规范标准",
    "主要施工方案与技术措施",
    "质量管理体系与保证措施",
    "施工进度计划与工期保证",
    "劳动力与机械设备配置",
    "应急预案与风险管理",
    "与发包人及监理的配合",
    "竣工验收与资料移交",
]


class TestZoneOrderGateDetection:
    """ENHANCE 2026-09-15：一级章节「工程逻辑顺序倒置」的门禁检测。

    设计立场：**只检测、不重排** —— 与门禁整体一致（出结论、不阻断）。
    因此这里最关键的回归点是「不误报」：干净目录、通用标题、同分区调换、
    两份 28 章模板，都必须零 issue。
    """

    def test_ascending_outline_has_no_violation(self):
        from app.services.ai import outline_gate

        facts = outline_gate.compute_outline_facts(_l1_outline(_ASCENDING_9))
        assert facts["zone_order_violations"] == []
        assert outline_gate.collect_deterministic_issues(facts) == []

    def test_all_nine_zones_are_inferred(self):
        """关键词表要真能覆盖标准叙事的九个分区，否则本检测形同虚设"""
        from app.services.ai import outline_gate

        facts = outline_gate.compute_outline_facts(_l1_outline(_ASCENDING_9))
        zones = [z["zone"] for z in facts["zone_sequence"]]
        assert zones == [
            "宏观总览",
            "前期准备",
            "核心技术",
            "管理保障",
            "进度管控",
            "资源保障",
            "专项预案",
            "协调配合",
            "收尾优化",
        ]

    def test_inverted_order_is_reported(self):
        """应急预案插在质量管理之前：典型的叙事流断裂"""
        from app.services.ai import outline_gate

        facts = outline_gate.compute_outline_facts(
            _l1_outline(["项目概述", "应急预案与风险管理", "质量管理体系与保证措施", "施工进度计划"])
        )
        issues = outline_gate.collect_deterministic_issues(facts)
        assert [i["category"] for i in issues] == ["zone-order"]
        assert "应急预案与风险管理" in issues[0]["problem"]
        assert "质量管理体系与保证措施" in issues[0]["problem"]
        # 指向两个章节，便于前端定位
        assert issues[0]["node_ids"] == ["2", "3"]

    def test_unknown_zone_chapter_is_never_judged(self):
        """「施工组织设计」这类通用标题命中不了关键词表 —— 不得据此判它放错位置"""
        from app.services.ai import outline_gate

        facts = outline_gate.compute_outline_facts(
            _l1_outline(["施工组织设计", "质量管理体系", "安全文明施工"])
        )
        zones = {z["title"]: z["zone"] for z in facts["zone_sequence"]}
        assert zones["施工组织设计"] == outline_gate.UNKNOWN_ZONE
        assert facts["zone_order_violations"] == []

    def test_same_zone_swap_is_not_reported(self):
        """同分区内调换顺序（权重相等）不算倒置 —— 那是排版偏好，不是逻辑错误"""
        from app.services.ai import outline_gate

        facts = outline_gate.compute_outline_facts(
            _l1_outline(["安全生产保证措施", "质量管理体系", "文明施工与环境保护"])
        )
        assert len({z["zone"] for z in facts["zone_sequence"]}) == 1
        assert facts["zone_order_violations"] == []

    def test_one_issue_per_offending_chapter(self):
        """一处放错不该炸出 O(n²) 条 issue：每个章节最多一条，其余计入 later_count"""
        from app.services.ai import outline_gate

        facts = outline_gate.compute_outline_facts(
            _l1_outline(["应急预案与风险管理", "质量管理体系", "施工进度计划", "劳动力配置计划"])
        )
        violations = facts["zone_order_violations"]
        assert len(violations) == 1
        assert violations[0]["later_count"] == 3
        issues = outline_gate.collect_deterministic_issues(facts)
        assert len(issues) == 1
        assert "还有 2 个更靠前的分区" in issues[0]["problem"]

    def test_zone_order_does_not_escalate_verdict(self):
        """关键词推断必然有误判 —— 不能让单这一条把裁决推成 user_feedback"""
        from app.services.ai import outline_gate

        facts = outline_gate.compute_outline_facts(
            _l1_outline(["应急预案与风险管理", "质量管理体系"])
        )
        issues = outline_gate.collect_deterministic_issues(facts)
        assert issues[0]["severity"] == "warning"
        assert issues[0]["confirmation_required"] is False
        assert outline_gate.derive_status(issues) == "simple_fix"

    def test_zone_order_does_not_trigger_extra_ai_call(self):
        """AI 语义审对顺序问题给不出额外信息，不该为它多跑一次大模型"""
        from app.services.ai import outline_gate

        facts = outline_gate.compute_outline_facts(
            _l1_outline(["应急预案与风险管理", "质量管理体系"])
        )
        issues = outline_gate.collect_deterministic_issues(facts)
        assert outline_gate.should_run_ai_review(facts, issues) is False
        # 但其它类别的问题照旧触发
        issues.append({"category": "score-coverage", "severity": "warning", "confirmation_required": True})
        assert outline_gate.should_run_ai_review(facts, issues) is True

    def test_both_28_chapter_templates_are_clean(self):
        """护栏：关键词表一旦改动导致标准模板被判乱序，这里立刻失败"""
        from app.services.ai import outline_gate
        from app.services.ai.outline_sort_optimizer import CORE_28_CHAPTERS
        from app.services.ai.workflows_outline_v51 import REFERENCE_TEMPLATE_V51

        for name, template in (("core28", CORE_28_CHAPTERS), ("v51", REFERENCE_TEMPLATE_V51)):
            facts = outline_gate.compute_outline_facts(_l1_outline([c["title"] for c in template]))
            assert facts["zone_order_violations"] == [], f"{name} 模板被误判为乱序"
            assert all(z["zone"] != outline_gate.UNKNOWN_ZONE for z in facts["zone_sequence"]), (
                f"{name} 模板有章节推断不出分区"
            )

    def test_gate_prompt_carries_zone_violations(self):
        from app.services.ai import outline_gate

        facts = outline_gate.compute_outline_facts(
            _l1_outline(["应急预案与风险管理", "质量管理体系"])
        )
        prompt = outline_gate.build_gate_user_prompt(facts, "[]")
        assert "一级章节分区顺序倒置" in prompt
        assert "应急预案与风险管理" in prompt

    def test_category_is_registered_in_policy_and_labels(self):
        """未登记的类别会落到默认 (warning, True)，把裁决推成 user_feedback"""
        from app.services.ai import outline_gate

        assert outline_gate.normalize_category("zone_order") == "zone-order"
        assert outline_gate.normalize_category("Zone-Order") == "zone-order"
        severity, needs_confirm = outline_gate.CATEGORY_POLICY["zone-order"]
        assert severity == "warning"
        assert needs_confirm is False
        assert outline_gate._CATEGORY_LABELS["zone-order"]

    def test_infer_chapter_zone_prefers_core_titles(self):
        from app.services.ai import outline_gate
        from app.services.ai.outline_sort_optimizer import CORE_ZONE_MAP

        title, zone = next(iter(CORE_ZONE_MAP.items()))
        assert outline_gate.infer_chapter_zone(title) == zone
        assert outline_gate.infer_chapter_zone("") == outline_gate.UNKNOWN_ZONE
        assert outline_gate.infer_chapter_zone("第三章 现场总览") == outline_gate.UNKNOWN_ZONE


class TestScoreCoverageRecall:
    """BUG-FIX 2026-09-15（高）：评分项覆盖检测的漏报。

    原实现只用二元组 Jaccard 判「评分项是否被某个一级章节覆盖」。而 L1 产出的是
    评分项的**专业化扩写**标题，扩写只增大 Jaccard 的分母、把分数压到阈值 0.5
    以下，于是明明覆盖了的评分项被报成 missing，进而产出 `score-coverage` issue
    （warning + 需用户确认 → 把整份裁决推成 user_feedback）。

    不是"阈值偏低"：实测不相关的组合最高只有 0.143，与真实覆盖区间没有重叠。
    修法是补上二元组覆盖率（只看评分项自身被覆盖多少，扩写不影响分子）。
    """

    # （评分项, 章节标题）——章节是评分项的扩写，必须判为已覆盖
    EXPANDED_PAIRS = [
        ("质量保证措施", "质量管理体系与保证措施"),
        ("文明施工措施", "文明施工与环境保护措施"),
        ("应急预案", "应急预案与风险管理"),
        ("劳动力计划", "劳动力与机械设备配置计划"),
        ("施工组织设计", "施工组织总体设计"),
    ]

    # （评分项, 章节标题）——确实无关，必须判为未覆盖
    UNRELATED_PAIRS = [
        ("质量管理体系", "文明施工管理措施"),
        ("应急预案", "质量管理体系与保证措施"),
        ("进度计划", "资源配置计划"),
    ]

    def test_pure_jaccard_would_have_missed_them(self):
        """固化缺陷现场：证明这些组合在旧判据下确实过不了阈值"""
        from app.services.ai import outline_gate
        from app.services.ai.workflows_outline_utils import _title_similarity

        for score_title, chapter_title in self.EXPANDED_PAIRS:
            jaccard = _title_similarity(score_title, chapter_title)
            # 「施工组织设计 / 施工组织总体设计」恰好等于阈值（0.500，临界），
            # 其余四组明确低于阈值 —— 用 <= 覆盖临界那组，另单独断言漏报规模。
            assert jaccard <= outline_gate.SCORE_MATCH_THRESHOLD, (
                f"{score_title} / {chapter_title} 已能靠 Jaccard 命中，本用例前提失效"
            )
        strictly_below = sum(
            1
            for s, c in self.EXPANDED_PAIRS
            if _title_similarity(s, c) < outline_gate.SCORE_MATCH_THRESHOLD
        )
        assert strictly_below >= 4, f"仅 {strictly_below} 组低于阈值，旧判据的漏报规模已缩小"

    @pytest.mark.parametrize(("score_title", "chapter_title"), EXPANDED_PAIRS)
    def test_expanded_title_counts_as_covered(self, score_title, chapter_title):
        from app.services.ai import outline_gate

        assert outline_gate._score_match_strength(score_title, chapter_title) >= (
            outline_gate.SCORE_MATCH_THRESHOLD
        )

    @pytest.mark.parametrize(("score_title", "chapter_title"), EXPANDED_PAIRS)
    def test_no_missing_issue_for_expanded_title(self, score_title, chapter_title):
        from app.services.ai import outline_gate

        facts = outline_gate.compute_outline_facts(
            _l1_outline([chapter_title]), scoring_titles=[score_title]
        )
        assert facts["score_coverage"]["missing"] == []
        assert [m["chapter_path"] for m in facts["score_coverage"]["matched"]] == ["1"]
        assert outline_gate.collect_deterministic_issues(facts) == []

    @pytest.mark.parametrize(("score_title", "chapter_title"), UNRELATED_PAIRS)
    def test_unrelated_title_is_still_missing(self, score_title, chapter_title):
        """召回上去了，精度不能掉"""
        from app.services.ai import outline_gate

        assert outline_gate._score_match_strength(score_title, chapter_title) < (
            outline_gate.SCORE_MATCH_THRESHOLD
        )
        facts = outline_gate.compute_outline_facts(
            _l1_outline([chapter_title]), scoring_titles=[score_title]
        )
        assert facts["score_coverage"]["missing"] == [score_title]

    def test_substring_rule_still_yields_strong_match(self):
        from app.services.ai import outline_gate

        # 整体子串包含 → 覆盖率 1.0（旧的 0.9 规则被覆盖率严格支配，已移除）
        assert outline_gate._score_match_strength("施工进度计划", "施工进度计划与工期保证") == 1.0
        assert outline_gate._score_match_strength("质量", "质量") == 1.0
        assert outline_gate._score_match_strength("", "质量管理体系") == 0.0

    def test_coverage_metric_ignores_expansion_length(self):
        """覆盖率的分子分母都只看评分项自身 —— 章节再怎么扩写都不影响"""
        from app.services.ai import outline_gate

        short = outline_gate._bigram_coverage("应急预案", "应急预案")
        long_ = outline_gate._bigram_coverage("应急预案", "应急预案与风险管理及抵抗风险的措施")
        assert short == 1.0
        assert long_ == 1.0


def _l1_with_children(titles: list[str]) -> list[dict]:
    """一级章节 + 各带两级子节点（自检的本地检查会给缺子节点的章节补全，
    补出来的通用标题互相近义重复，会污染断言——这里自带够用的三层结构）"""
    return [
        {
            "id": str(i + 1),
            "title": t,
            "children": [
                {
                    "id": f"{i + 1}.{j + 1}",
                    "title": f"{t}分项{j + 1}",
                    "children": [
                        {"id": f"{i + 1}.{j + 1}.{k + 1}", "title": f"{t}分项{j + 1}控制点{k + 1}", "children": []}
                        for k in range(2)
                    ],
                }
                for j in range(2)
            ],
        }
        for i, t in enumerate(titles)
    ]


_INVERTED_3 = ["应急预案与风险管理", "质量管理体系与保证措施", "施工进度计划"]
_INVERTED_3_FIXED = ["质量管理体系与保证措施", "施工进度计划", "应急预案与风险管理"]


class TestZoneReorder:
    """ENHANCE 2026-09-15：`reorder_by_zone` 的机械重排（无 AI、不增删、不改标题）。"""

    def test_ascending_outline_is_untouched(self):
        from app.services.ai import outline_gate

        outline = _l1_outline(_ASCENDING_9)
        new, moves = outline_gate.reorder_by_zone(outline)
        assert moves == []
        assert all(a is b for a, b in zip(outline, new)), "无倒置时必须原样返回同一批节点对象"

    def test_inverted_order_is_reordered(self):
        from app.services.ai import outline_gate

        outline = _l1_outline(_INVERTED_3)
        new, moves = outline_gate.reorder_by_zone(outline)
        assert [n["title"] for n in new] == _INVERTED_3_FIXED
        assert len(moves) == 3
        # 重排后不再有倒置
        assert outline_gate.compute_outline_facts(new)["zone_order_violations"] == []

    def test_unknown_zone_is_an_anchor(self):
        """推断不出分区的章节是锚点：位置不变，两侧章节也不跨过它。

        代价是这种倒置**只报不修** —— 拿关键词表替用户决定「施工组织设计」
        该放哪儿，比放着不动更糟。
        """
        from app.services.ai import outline_gate

        outline = _l1_outline(["应急预案与风险管理", "施工组织设计", "质量管理体系与保证措施"])
        new, moves = outline_gate.reorder_by_zone(outline)
        assert moves == []
        assert [n["title"] for n in new] == [n["title"] for n in outline]
        # 但仍要在裁决里报出来，交给人判断
        assert len(outline_gate.compute_outline_facts(outline)["zone_order_violations"]) == 1

    def test_stable_within_zone(self):
        """同分区内保持原有相对顺序，不对同层先后做主张"""
        from app.services.ai import outline_gate

        outline = _l1_outline(["施工进度计划", "安全文明施工管理", "质量管理体系", "资源配置计划"])
        new, _ = outline_gate.reorder_by_zone(outline)
        assert [n["title"] for n in new] == [
            "安全文明施工管理",
            "质量管理体系",
            "施工进度计划",
            "资源配置计划",
        ]

    def test_no_node_added_or_lost_and_children_kept(self):
        from app.services.ai import outline_gate

        outline = _l1_with_children(_INVERTED_3)
        new, _ = outline_gate.reorder_by_zone(outline)
        assert len(new) == len(outline)
        assert sorted(n["title"] for n in new) == sorted(n["title"] for n in outline)
        by_title = {n["title"]: n for n in new}
        for node in outline:
            assert by_title[node["title"]]["children"] == node["children"]

    def test_ids_are_renumbered_after_reorder(self):
        """编号即位置的隐含契约：重排后必须重新编号，否则前端按 id 编辑会改错节点"""
        from app.services.ai import outline_gate

        new, _ = outline_gate.reorder_by_zone(_l1_with_children(_INVERTED_3))
        assert [n["id"] for n in new] == ["1", "2", "3"]
        for node in new:
            for j, child in enumerate(node["children"], 1):
                assert child["id"] == f"{node['id']}.{j}"

    def test_apply_flag_exists_and_defaults_to_true(self):
        from app.services.ai import outline_gate

        assert outline_gate.APPLY_ZONE_REORDER is True


class TestZoneReorderWiring:
    """重排必须接进自检的机械修复链，并记入 `auto_fixed`（不做静默改动）。"""

    async def test_self_check_reorders_and_reports(self):
        from app.services.ai import workflows_outline_v51 as w
        from types import SimpleNamespace

        state = SimpleNamespace()
        result = await w._self_check_v51(
            object(), _l1_with_children(_INVERTED_3), 0.0, v51_state=state
        )
        assert [n["title"] for n in result] == _INVERTED_3_FIXED
        assert any("工程逻辑顺序重排" in a for a in state.gate_report["auto_fixed"])
        assert [i for i in state.gate_report["issues"] if i["category"] == "zone-order"] == []

    async def test_self_check_keeps_order_when_flag_disabled(self, monkeypatch):
        """招标文件指定目录顺序时，置 False 即退回"只提示不改" """
        from app.services.ai import outline_gate, workflows_outline_v51 as w
        from types import SimpleNamespace

        monkeypatch.setattr(outline_gate, "APPLY_ZONE_REORDER", False)
        state = SimpleNamespace()
        result = await w._self_check_v51(
            object(), _l1_with_children(_INVERTED_3), 0.0, v51_state=state
        )
        assert [n["title"] for n in result] == _INVERTED_3
        assert state.gate_report["auto_fixed"] == []
        assert any(i["category"] == "zone-order" for i in state.gate_report["issues"])
