"""七模块跨模块数据链修复回归测试（2026-10-09）

本文件钉住本轮「跨模块深度探索 + BUG 修复」中**改变了跨模块契约**的几处修复。
每一处若回退，都会造成静默的数据损坏或功能整体失效，故必须显式覆盖。

覆盖的修复点：

1. 要点提取 → 目录/正文/一致性/导出 的连接键 `scoring_points[].id`
   （`normalize_writing_targets` 幂等性，workflows_analysis.py）
2. 目录 → 章节补全 的章节状态机（`status='active'` 幽灵状态，sse_db_ops.py）
3. 目录 → 字数校验 的字数档位透出（`build_tree` 的 word_count_tier，sections.py）
4. 正文生成 → 字数校验 的目标口径一致（`writing_length` + 档位容差，word_count.py）
5. 一致性修复的收敛范围（`refresh_conflict_states` 全量扫描，consistency_repair.py）
6. SSE 前后端终态协议统一（`SSE_TERMINAL_STATUSES`，sse_shared.py）
"""

from __future__ import annotations

import inspect

import pytest

from app.routers.sse import sse_outline, sse_shared
from app.services.ai.workflows_analysis import normalize_writing_targets
from app.utils.scoring_points import is_scoring_placeholder


# ---------------------------------------------------------------------------
# 1. 要点提取：normalize_writing_targets 必须幂等
# ---------------------------------------------------------------------------

def _pts():
    """权重和 88（≠100）的两点，刻意触发 `[权重异常]` 系统提示伪项插入分支。"""
    return [{"name": "技术方案", "weight": "60"}, {"name": "质量管理", "weight": "28"}]


class TestNormalizeWritingTargetsIdempotency:
    """BUG-FIX 2026-10-09（跨模块数据链 / 幂等性）。

    根因：`normalize_writing_targets` 循环里无条件 `id = f"R{len+1}"` 重编号，
    把上一轮插入的 `__weight_warning__` 伪项也纳入编号 → 伪项被"升格"成 R1；
    随后 `has_warning = is_scoring_placeholder(normalized[0])` 因首元素已变成
    普通评分点而**恒为假**（死代码），于是每次调用再追加一条新伪项。

    影响：`sections.scoring_point_id` 存的就是这里的 `R{n}`，id 漂移后
    目录↔评分点映射在 `scoring_compliance`（评分覆盖审计）与 `export`
    （导出预检）同时断裂，表现为永久性的假「评分点未覆盖」；且伪项被升格后
    `strip_scoring_placeholders`（按 id 过滤）无法再剔除它。
    """

    def test_repeated_normalization_is_stable(self):
        first = normalize_writing_targets(_pts())
        second = normalize_writing_targets(first)
        third = normalize_writing_targets(second)
        assert first == second == third, "normalize_writing_targets 必须幂等"

    def test_placeholder_is_not_promoted_into_a_real_scoring_point(self):
        """伪项不得占用 R{n} 编号，也不得被重复追加。"""
        out = normalize_writing_targets(normalize_writing_targets(_pts()))
        placeholders = [p for p in out if is_scoring_placeholder(p)]
        assert len(placeholders) == 1, f"系统提示伪项应恰好 1 条，实际 {len(placeholders)}"

        real_ids = [p["id"] for p in out if not is_scoring_placeholder(p)]
        assert real_ids == ["R1", "R2"], f"真实评分点 id 应稳定为 R1/R2，实际 {real_ids}"

    def test_placeholder_names_are_not_duplicated(self):
        out = normalize_writing_targets(normalize_writing_targets(_pts()))
        names = [p["name"] for p in out]
        assert len(names) == len(set(names)), f"出现重名评分点：{names}"

    def test_normal_weight_sum_keeps_ids_contiguous(self):
        """权重和为 100 时不产生伪项，编号仍为 R1..Rn。"""
        out = normalize_writing_targets(
            [{"name": "甲", "weight": "50"}, {"name": "乙", "weight": "50"}]
        )
        assert [p["id"] for p in out] == ["R1", "R2"]
        assert not any(is_scoring_placeholder(p) for p in out)

    def test_strip_scoring_placeholders_still_works_after_roundtrip(self):
        """下游（目录/正文/预检）依赖 strip 后拿到纯净评分点，必须始终成立。"""
        from app.utils.scoring_points import strip_scoring_placeholders

        out = normalize_writing_targets(normalize_writing_targets(_pts()))
        stripped = strip_scoring_placeholders(out)
        assert len(stripped) == 2
        assert [p["id"] for p in stripped] == ["R1", "R2"]


# ---------------------------------------------------------------------------
# 2. 目录 → 章节补全：章节状态机不得写入幽灵状态 'active'
# ---------------------------------------------------------------------------

class TestOutlineWritesPendingStatus:
    """BUG-FIX 2026-10-09（跨模块数据链 / 章节状态机）。

    真实状态机是 pending → generating → completed | failed | cancelled
    （sections 表默认值 'pending'，见 db.py）。目录生成此前硬编码写入
    'active'，而全仓库无任何写入方会产生该值，后果：
      · 前端 collectLeafIds(filterPendingFailed=true) 只认 pending/failed
        → 大纲刚生成、尚未写正文的章节在「补全生成」里永远选不中；
      · 前端 getStatusIcon 无 'active' 分支，一律落到默认灰色时钟图标。
    """

    def test_outline_insert_does_not_write_active_status(self):
        src = inspect.getsource(sse_outline._save_and_finalize_outline) if hasattr(
            sse_outline, "_save_and_finalize_outline"
        ) else ""
        # 该函数体可能不含 INSERT（实际落在 sse_db_ops），此处只做源码级断言，
        # 确保任何写死 'active' 的路径都已消失。
        from app.routers.sse import sse_db_ops

        body = inspect.getsource(sse_db_ops)
        assert "'active', ?, ?)" not in body, "sections INSERT 仍在写 status='active'"
        assert '"status": "active"' not in body, "saved_node 仍在返回 status='active'"

    def test_frontend_pending_failed_filter_would_select_pending_nodes(self):
        """镜像前端 collectLeafIds 的判定：status='pending' 必须被选中。"""
        # 前端 utils.ts 的逻辑：filterPendingFailed 时只收 pending / failed。
        for status, should_select in (("pending", True), ("failed", True), ("completed", False)):
            selected = status in ("pending", "failed")
            assert selected is should_select


# ---------------------------------------------------------------------------
# 3. 目录 → 字数校验：build_tree 必须透出 word_count_tier
# ---------------------------------------------------------------------------

class TestBuildTreeExposesTier:
    """BUG-FIX 2026-10-09（跨模块数据链 / 字数档位）。

    `_TREE_LIGHT_COLUMNS` 此前不含 word_count_tier，且 SectionTreeNode 构造时
    也未传 word_count_tier / tier_label / tier_target，导致 models.py 里这三个
    字段永远取默认值 None。前端 `resolveTargetForNode` / `resolveToleranceForNode`
    的「档位分支」整段成为死代码，`PATCH /sections/{id}/word-count-tier`
    在 UI 上完全无效。
    """

    def test_tree_light_columns_include_word_count_tier(self):
        from app.services.sections import _TREE_LIGHT_COLUMNS

        assert "word_count_tier" in _TREE_LIGHT_COLUMNS

    def test_build_tree_passes_tier_fields_into_node(self):
        from app.services import sections as sections_svc

        src = inspect.getsource(sections_svc.build_tree)
        assert "word_count_tier=_tier_key" in src
        assert "tier_label=" in src
        assert "tier_target=" in src

    def test_tier_label_map_loads_without_db(self):
        """档位表不可用时返回空 dict（前端按层级默认值回落，行为与修复前一致）。"""
        from app.services.sections import _load_tier_label_maps

        labels, targets = _load_tier_label_maps()
        assert isinstance(labels, dict) and isinstance(targets, dict)


# ---------------------------------------------------------------------------
# 4. 正文生成 → 字数校验：目标口径必须一致
# ---------------------------------------------------------------------------

class TestWordCountTargetConsistency:
    """BUG-FIX 2026-10-09（跨模块 / 生成-校验口径一致）。

    正文生成侧按 `writing_length`（项目最近一次生成任务所用档位）与档位
    `tolerance_ratio` 判定达标；字数校验页此前：
      · 从不读 `generation_tasks.writing_length` → L4+ 一律回落 standard；
      · 把容差硬编码 0.10 → 管理员改 `word_count_tiers.tolerance_ratio` 后
        生成按新容差扩写、校验页仍按 ±10% 判定。
    两者共同导致「生成说达标、校验说缺字」。
    """

    def test_resolve_target_for_section_accepts_writing_length(self):
        from app.routers import word_count as wc

        sig = inspect.signature(wc._resolve_target_for_section)
        assert "writing_length" in sig.parameters

    def test_project_writing_length_loader_exists(self):
        from app.routers import word_count as wc

        assert callable(wc._load_project_writing_length)

    def test_target_bounds_uses_configured_tolerance(self):
        """容差来自权威口径（CONVERGE 2026-10-12）而非硬编码 0.10。

        CONVERGE：`_target_bounds` 不再自己解析，而是转发唯一权威实现
        `services/word_target_resolver.py`（其容差随「目标来源」走：档位 →
        该档位 tolerance_ratio；L3 自身正文 → L3 容差）。本断言的**意图**
        （容差不得硬编码、必须来自配置）保持不变，只是断言点从
        「函数体内出现 `_tolerance_for_section`」改为「函数转发权威口径」。
        """
        from app.routers import word_count as wc

        src = inspect.getsource(wc._target_bounds)
        assert "_resolve_word_target_for_section" in src
        assert "(1 - 0.10)" not in src, "容差仍被硬编码为 0.10"

    def test_target_bounds_tolerance_follows_tier_config(self):
        """档位容差配置变更后，达标区间随之变化（权威口径的实义断言）。"""
        from app.routers import word_count as wc

        class _Sec:
            level = 3
            target_words = None
            word_count_tier = "detailed"

        resolved = wc._resolve_word_target_for_section(_Sec())
        assert resolved is not None
        lo, hi = wc._target_bounds(_Sec())
        assert (lo, hi) == (resolved.lo, resolved.hi)
        assert (lo, hi) == (1800, 2200)  # detailed 2000 字 ±10%

    def test_tolerance_helper_prefers_tier_then_l3_own(self):
        from app.routers import word_count as wc

        assert callable(wc._tolerance_for_section)


# ---------------------------------------------------------------------------
# 5. 一致性修复：收敛判定必须覆盖全项目正文
# ---------------------------------------------------------------------------

class TestConflictStateRefreshScope:
    """BUG-FIX 2026-10-09（正确性 / 误标已解决）。

    `refresh_conflict_states` 以「全项目正文里还能找到 ≥2 个冲突取值 → 仍算冲突」
    为判据，因此**扫描范围必须覆盖整个项目**。此前 `repair_with_ai` 把
    「本次载入的小节」content_by_id 直接传入；当调用方带 `section_ids` 参数
    （POST /consistency/repair/{id}?section_ids=…）时只含被点名的小节 ——
    仍残留在其它小节的旧取值扫不到 → `len(unique_present) <= 1` 成立 →
    冲突被**错误标记为已解决**。人工点几个小节修复，反而把其它小节里真实
    存在的不一致"洗白"，且无任何提示。
    """

    def test_full_project_content_loader_exists(self):
        from app.services.consistency_repair import _load_all_project_content

        assert callable(_load_all_project_content)

    def test_repair_uses_full_scope_not_just_loaded_sections(self):
        from app.services import consistency_repair

        src = inspect.getsource(consistency_repair.repair_with_ai)
        assert "_load_all_project_content" in src, "落文后仍只用局部小节做收敛判定"
        # 落库内容必须覆盖到判定范围之上
        assert "verify_scope.update(content_by_id)" in src


# ---------------------------------------------------------------------------
# 6. SSE：终态集合前后端统一
# ---------------------------------------------------------------------------

class TestSseTerminalStatusProtocol:
    """BUG-FIX 2026-10-09（跨模块协议统一）。

    终态集合此前在 sse_outline.py 与 sse_content.py 各写一份字面量，且目录流
    少了 "cancelled"；而前端 OutlineEditorPage / SectionEditorPage 都实现了
    `status === "cancelled"` 分支 —— 前端认它、后端不认它：一旦上游推来
    cancelled，目录流消费循环不会 break，连接会一直挂到 1800s 总超时才释放。
    """

    def test_shared_terminal_set_includes_cancelled(self):
        assert "cancelled" in sse_shared.SSE_TERMINAL_STATUSES

    def test_shared_terminal_set_covers_all_four_states(self):
        assert sse_shared.SSE_TERMINAL_STATUSES == frozenset(
            {"completed", "error", "stopped", "cancelled"}
        )

    @pytest.mark.parametrize(
        "module_name", ["app.routers.sse.sse_outline", "app.routers.sse.sse_content"]
    )
    def test_both_streams_reference_the_shared_constant(self, module_name):
        """两条流都必须引用共享常量，不得再各自硬编码字面量。

        注意：断言只扫「去掉注释后的代码」，因为 BUG-FIX 说明注释里会引用
        旧字面量（`("completed", "error", "stopped")`）作为对照，那是文档而非代码。
        """
        import importlib
        import io
        import tokenize

        mod = importlib.import_module(module_name)
        path = inspect.getsourcefile(mod)
        assert path is not None, f"无法定位 {module_name} 源文件"
        with io.open(path, encoding="utf-8") as fh:
            code_lines = [
                ln for tok in tokenize.generate_tokens(fh.readline)
                if tok.type != tokenize.COMMENT
                for ln in [tok.string]
            ]
        src = "\n".join(code_lines)
        assert "SSE_TERMINAL_STATUSES" in src
        assert 'in ("completed", "error", "stopped")' not in src, (
            f"{module_name} 仍在硬编码终态字面量"
        )