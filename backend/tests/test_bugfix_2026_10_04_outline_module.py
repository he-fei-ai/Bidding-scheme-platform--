"""目录生成模块 BUG 修复回归测试（2026-10-04，B1~B7）

覆盖本轮深度探查后落地的修复，按项目约定：优先测行为，不测实现细节；
仅对无法在单测中稳定复现的闭包级修复（B1）做源码守卫断言。

- B1 `sse_outline._run_outline_task`：`boq_json_str` 预初始化 + 兜底重跑补
  `outline_mode`（此前绑定前抛异常会让最后一道恢复链路因 NameError 再失效）
- B2 `sse_db_ops._clear_and_save_outline._save_nodes`：sort_order 改 1-based，
  与 move_section/reorder_sections/_normalize_section_position 的写入契约统一
- B4 `services.sections`：手工 create/update/batch 标题剥离编号前缀，
  与目录生成落库同一归一化契约（消除「展示编号+标题内嵌编号」双重编号）
- B5 `_wait_for_v51_review`：先发事件后 clear 的审核竞态（用户极快确认时
  resume 信号与编辑结果被 clear 抹掉，工作流挂到超时）
- B7 提示词编号标准统一：SHARED_HEADING_RULES 与 V3.1 实际模板
  （DEFAULT_NUMBERING_TEMPLATES：一级第X节/二级中文/三级阿拉伯/四级点分）对齐
"""

import asyncio
import inspect

import pytest

from app.db import get_db
from app.models import SectionCreate, SectionUpdate
from app.services.sections import (
    _strip_title_numbering,
    batch_create_sections,
    create_section,
    update_section,
)


def _seed_project(name: str) -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO projects (name, industry) VALUES (?, ?)", (name, "市政")
        )
        return int(cur.lastrowid)


# ─────────────────────────────────────────────────────────────────────────────
# B4：手工章节写入的标题编号前缀剥离（编号命名空间统一）
# ─────────────────────────────────────────────────────────────────────────────


class TestTitleNumberingStrip:
    def test_helper_strips_various_prefixes(self):
        """helper 直接行为断言：各类编号前缀剥离后只留纯文本"""
        assert _strip_title_numbering("1.1 施工部署") == "施工部署"
        assert _strip_title_numbering("第一节、总体设计") == "总体设计"
        assert _strip_title_numbering("一、编制依据") == "编制依据"
        assert _strip_title_numbering("3、现场勘查") == "现场勘查"
        assert _strip_title_numbering("纯文本标题") == "纯文本标题"

    def test_helper_never_returns_empty(self):
        """整串都是编号时（剥完为空）保留原标题，向后兼容不吞数据"""
        assert _strip_title_numbering("1.1") == "1.1"

    def test_create_section_strips_prefix(self):
        """POST 创建章节存「1.1 施工部署」→ DB 应为「施工部署」，
        否则导出/展示会变成「一、1.1 施工部署」双重编号"""
        pid = _seed_project("B4-create")
        sec = create_section(pid, SectionCreate(title="1.1 施工部署"))
        assert sec.title == "施工部署"
        with get_db() as conn:
            row = conn.execute("SELECT title FROM sections WHERE id = ?", (sec.id,)).fetchone()
        assert row["title"] == "施工部署"

    def test_update_section_strips_prefix(self):
        """改名同样剥离：PATCH title 存「第二章、进度计划」→「进度计划」"""
        pid = _seed_project("B4-update")
        sec = create_section(pid, SectionCreate(title="进度计划初稿"))
        updated = update_section(sec.id, SectionUpdate(title="第二章、进度计划"))
        assert updated is not None
        assert updated.title == "进度计划"

    def test_batch_create_strips_prefix(self):
        """批量创建与单条创建同一归一化口径"""
        pid = _seed_project("B4-batch")
        created = batch_create_sections(
            pid,
            [
                {"title": "一、质量管理", "level": 1, "sort_order": 1},
                {"title": "纯净标题", "level": 1, "sort_order": 2},
            ],
        )
        titles = {s.title for s in created}
        assert titles == {"质量管理", "纯净标题"}


# ─────────────────────────────────────────────────────────────────────────────
# B2：目录重建落库 sort_order 与全库 1-based 契约统一
# ─────────────────────────────────────────────────────────────────────────────


class TestSaveNodesSortOrder1Based:
    def _rows(self, pid: int):
        with get_db() as conn:
            return conn.execute(
                "SELECT id, parent_id, level, sort_order FROM sections "
                "WHERE project_id = ? ORDER BY id",
                (pid,),
            ).fetchall()

    def test_rebuild_writes_1_based_sort_order(self):
        from app.routers.sse.sse_db_ops import _clear_and_save_outline

        pid = _seed_project("B2-sortorder")
        outline = [
            {"title": f"第一章标题{i}", "children": [
                {"title": f"第二章标题{i}_{j}", "children": []} for j in range(2)
            ]}
            for i in range(3)
        ]
        saved = _clear_and_save_outline(pid, outline)

        # 返回给前端的树：1-based 连续
        assert [n["sort_order"] for n in saved] == [1, 2, 3]
        for n in saved:
            assert [c["sort_order"] for c in n["children"]] == [1, 2]

        # DB 行：同父兄弟 sort_order 从 1 起连续递增（与 reorder_sections
        # 连续性校验、_normalize_section_position MAX+1 同一契约）
        rows = self._rows(pid)
        by_parent: dict = {}
        for r in rows:
            by_parent.setdefault(r["parent_id"], []).append(r["sort_order"])
        for parent_id, orders in by_parent.items():
            assert sorted(orders) == list(range(1, len(orders) + 1)), (
                f"parent={parent_id} 的 sort_order 非 1-based 连续: {orders}"
            )

    def test_rebuild_tree_integrity_after_save(self):
        """B3 自检口径：正常重建后不得出现悬空 parent_id / 同父重复 sort_order"""
        from app.routers.sse.sse_db_ops import _clear_and_save_outline

        pid = _seed_project("B2-integrity")
        _clear_and_save_outline(pid, [
            {"title": "甲章", "children": [{"title": "甲一节", "children": []}]},
            {"title": "乙章", "children": []},
        ])
        with get_db() as conn:
            orphans = conn.execute(
                "SELECT COUNT(*) FROM sections s WHERE s.project_id = ? "
                "AND s.parent_id IS NOT NULL "
                "AND NOT EXISTS (SELECT 1 FROM sections p WHERE p.id = s.parent_id)",
                (pid,),
            ).fetchone()[0]
            dup_orders = conn.execute(
                "SELECT COUNT(*) FROM (SELECT parent_id, sort_order FROM sections "
                "WHERE project_id = ? GROUP BY parent_id, sort_order HAVING COUNT(*) > 1)",
                (pid,),
            ).fetchone()[0]
        assert orphans == 0
        assert dup_orders == 0


# ─────────────────────────────────────────────────────────────────────────────
# B5：审核等待竞态（先清残留、后发事件）
# ─────────────────────────────────────────────────────────────────────────────


class TestWaitForV51ReviewRace:
    @pytest.mark.asyncio
    async def test_stale_resume_cleared_before_review_event(self):
        """上一阶段残留的 resume_event/edited_outline 必须在发出 review_needed
        之前清掉：否则用户在「事件已发出、尚未 clear」窗口内的极快确认会被
        clear 抹掉，工作流挂到超时（分步生成状态错乱）。"""
        from app.services.ai.workflows_outline_v51 import _wait_for_v51_review
        from app.services.v51_state import V51TaskState

        queue: asyncio.Queue = asyncio.Queue()
        state = V51TaskState(project_id=1)
        # 模拟上一阶段残留：resume 已置位 + 旧编辑结果
        state.resume_event.set()
        state.edited_outline = [{"title": "旧阶段编辑"}]

        outline = [{"id": 1, "title": "新阶段目录"}]
        task = asyncio.create_task(
            _wait_for_v51_review(queue, state, "level2", outline, auto_confirm=False)
        )
        await asyncio.sleep(0.05)

        # 发出的必须是本阶段 review_needed，且残留已被清除
        evt = queue.get_nowait()
        assert evt["status"] == "review_needed"
        assert evt["phase"] == "level2"
        assert not state.resume_event.is_set()
        assert state.edited_outline is None
        assert not task.done()  # 残留 set 不能让本阶段直接放行

        # 本阶段真实确认 → 返回本阶段目录（不是旧 edited_outline）
        state.resume_event.set()
        result = await task
        assert result == outline

    @pytest.mark.asyncio
    async def test_cancelled_state_skips_review_event(self):
        """已取消的任务不再向用户发出审核请求，直接返回 None 中止"""
        from app.services.ai.workflows_outline_v51 import _wait_for_v51_review
        from app.services.v51_state import V51TaskState

        queue: asyncio.Queue = asyncio.Queue()
        state = V51TaskState(project_id=1)
        state.cancelled = True

        result = await _wait_for_v51_review(
            queue, state, "level3", [{"title": "x"}], auto_confirm=False, timeout=1
        )
        assert result is None
        assert queue.empty()  # 取消后不应再发出 review_needed 打扰前端


# ─────────────────────────────────────────────────────────────────────────────
# B1：SSE 兜底链路源码守卫（闭包内变量初始化，无法在单测中稳定触发）
# ─────────────────────────────────────────────────────────────────────────────


class TestSseOutlineFallbackGuard:
    def test_boq_json_str_preinitialized(self):
        """boq_json_str 必须在 try 之前的默认值区预置，且早于兜底重跑引用"""
        from app.routers.sse import sse_outline

        src = inspect.getsource(sse_outline)
        init_idx = src.find('boq_json_str = ""')
        assert init_idx != -1, "boq_json_str 缺少预初始化（兜底重跑会 NameError）"
        use_idx = src.find("boq_text=boq_json_str")
        assert use_idx != -1
        assert init_idx < use_idx, "预初始化必须位于兜底引用之前"

    def test_fallback_rerun_passes_outline_mode(self):
        """兜底重跑 _run_outline_workflow 必须带 outline_mode（直出/分步口径一致）"""
        from app.routers.sse import sse_outline

        src = inspect.getsource(sse_outline)
        assert "outline_mode=outline_mode" in src, (
            "兜底重跑丢失 outline_mode，降级提供商会丢掉用户选择的生成模式"
        )


# ─────────────────────────────────────────────────────────────────────────────
# B7：提示词编号标准与系统实际模板（V3.1）对齐
# ─────────────────────────────────────────────────────────────────────────────


class TestPromptNumberingConsistency:
    def test_shared_heading_rules_match_v31(self):
        """SHARED_HEADING_RULES 必须描述 V3.1 实际标准
        （DEFAULT_NUMBERING_TEMPLATES：一级第X节/二级中文/三级阿拉伯/四级点分），
        不再是陈旧的 V2.2 八级描述（一级第X章/二级第X节）"""
        from app.services.ai.prompts._shared import SHARED_HEADING_RULES
        from app.services.ai.heading_standard import DEFAULT_NUMBERING_TEMPLATES

        assert "V3.1" in SHARED_HEADING_RULES
        assert "- 一级：第X节" in SHARED_HEADING_RULES
        assert "- 二级：中文数字" in SHARED_HEADING_RULES
        assert "- 三级：阿拉伯数字" in SHARED_HEADING_RULES
        assert "- 四级：点分编号" in SHARED_HEADING_RULES
        assert "- 一级：第X章" not in SHARED_HEADING_RULES
        # 与代码侧模板交叉验证：L1 模板确实是 第{zh}节
        assert DEFAULT_NUMBERING_TEMPLATES[1]["numbering_template"] == "第{zh}节"

    def test_direct_prompt_examples_are_plain_titles(self):
        """直出目录示例标题不得内嵌「第一章」等编号前缀（防 AI 模仿）"""
        from app.services.ai.prompts.outline import (
            OUTLINE_DIRECT_SYSTEM,
            OUTLINE_REVIEW_SYSTEM,
        )

        # 示例标题不得内嵌编号前缀（防 AI 模仿）；提示词正文中作为
        # 禁用示例引用的"第一章"字样属合法，只约束 "title" 字段值
        assert '"title": "第一章' not in OUTLINE_DIRECT_SYSTEM
        assert '"title": "第' not in OUTLINE_DIRECT_SYSTEM
        # 审核提示词已改为「纯文本、不带编号前缀」规范
        assert "纯文本" in OUTLINE_REVIEW_SYSTEM


# ─────────────────────────────────────────────────────────────────────────────
# 遗留 L1：审核事件接入 event_emitter 广播链（断线重连客户端不再丢事件）
# ─────────────────────────────────────────────────────────────────────────────


class TestReviewEventEmitter:
    @pytest.mark.asyncio
    async def test_event_emitter_routes_review_needed(self):
        """传入 event_emitter 时，review_needed 走回调而不直入队列"""
        from app.services.ai.workflows_outline_v51 import _wait_for_v51_review
        from app.services.v51_state import V51TaskState

        queue: asyncio.Queue = asyncio.Queue()
        emitted: list = []

        async def _emitter(evt):
            emitted.append(evt)
            await queue.put(evt)  # 同步入队供后续 wait 前消费检查

        state = V51TaskState(project_id=1)
        task = asyncio.create_task(
            _wait_for_v51_review(
                queue, state, "level1", [{"title": "t"}],
                auto_confirm=False, event_emitter=_emitter,
            )
        )
        await asyncio.sleep(0.05)
        assert len(emitted) == 1 and emitted[0]["status"] == "review_needed"
        # 队列里的同一事件由 emitter 转发（走完整广播链后仍到达 SSE）
        evt = queue.get_nowait()
        assert evt["status"] == "review_needed"
        state.resume_event.set()
        assert await task is not None

    @pytest.mark.asyncio
    async def test_auto_confirm_also_uses_emitter(self):
        """auto_confirm 事件同样走 emitter（重连客户端能看到审核自动通过）"""
        from app.services.ai.workflows_outline_v51 import _wait_for_v51_review
        from app.services.v51_state import V51TaskState

        queue: asyncio.Queue = asyncio.Queue()
        emitted: list = []

        async def _emitter(evt):
            emitted.append(evt)

        state = V51TaskState(project_id=1)
        result = await _wait_for_v51_review(
            queue, state, "final", [{"title": "t"}],
            auto_confirm=True, event_emitter=_emitter,
        )
        assert result == [{"title": "t"}]
        assert emitted and emitted[0]["status"] == "review_auto_confirm"
        assert queue.empty()  # 不再裸写队列


# ─────────────────────────────────────────────────────────────────────────────
# 遗留 L2：正文快照 (level,title) 撞键 → 父链路径优先 + 消费式匹配
# ─────────────────────────────────────────────────────────────────────────────


class TestSnapshotPathKey:
    def test_duplicate_title_different_parents_no_loss(self):
        """不同父节点下同名同层旧章节各有正文：旧实现撞键覆盖（后写胜出），
        新实现按父链路径精确回填，且每条旧正文最多消费一次。"""
        from app.routers.sse.sse_db_ops import _clear_and_save_outline

        pid = _seed_project("L2-撞键")
        with get_db() as conn:
            a = conn.execute(
                "INSERT INTO sections (project_id, title, level, sort_order, status) "
                "VALUES (?, '甲', 1, 1, 'completed')", (pid,),
            ).lastrowid
            conn.execute(
                "INSERT INTO sections (project_id, parent_id, title, level, sort_order, "
                "status, content) VALUES (?, ?, '共同子标题', 2, 1, 'completed', '内容A')",
                (pid, a),
            )
            b = conn.execute(
                "INSERT INTO sections (project_id, title, level, sort_order, status) "
                "VALUES (?, '乙', 1, 2, 'completed')", (pid,),
            ).lastrowid
            conn.execute(
                "INSERT INTO sections (project_id, parent_id, title, level, sort_order, "
                "status, content) VALUES (?, ?, '共同子标题', 2, 1, 'completed', '内容B')",
                (pid, b),
            )

        # 新树：全局去重后只保留先出现的「甲/共同子标题」（与主链路口径一致）
        saved = _clear_and_save_outline(pid, [
            {"title": "甲", "children": [{"title": "共同子标题", "children": []}]},
            {"title": "乙", "children": []},
        ])
        child = saved[0]["children"][0]
        assert child["has_content"] is True
        with get_db() as conn:
            row = conn.execute(
                "SELECT content, status FROM sections WHERE id = ?", (child["id"],)
            ).fetchone()
        # 旧实现：快照 dict 被行序靠后的「内容B」覆盖 → 错误子树拿到别人正文；
        # 新实现：路径全等匹配 → 甲下的子节点拿到「内容A」
        assert row["content"] == "内容A"
        assert row["status"] == "completed"

    def test_rename_parent_still_restores_loose(self):
        """标题改名容忍（宽松口径）保留：唯一候选时无需路径全等也能回填"""
        from app.routers.sse.sse_db_ops import _clear_and_save_outline

        pid = _seed_project("L2-改名")
        with get_db() as conn:
            a = conn.execute(
                "INSERT INTO sections (project_id, title, level, sort_order, status) "
                "VALUES (?, '旧父名', 1, 1, 'completed')", (pid,),
            ).lastrowid
            conn.execute(
                "INSERT INTO sections (project_id, parent_id, title, level, sort_order, "
                "status, content) VALUES (?, ?, '子节', 2, 1, 'completed', '孤本正文')",
                (pid, a),
            )
        saved = _clear_and_save_outline(pid, [
            {"title": "新父名", "children": [{"title": "子节", "children": []}]},
        ])
        child = saved[0]["children"][0]
        assert child["has_content"] is True
        assert child["word_count"] > 0


# ─────────────────────────────────────────────────────────────────────────────
# 遗留 L4：SHARED_HEADING_RULES 由模板表派生（单一事实源）
# ─────────────────────────────────────────────────────────────────────────────


class TestHeadingRulesSingleSource:
    def test_rules_follow_template_change(self, monkeypatch):
        """修改导出模板表 → 提示词文本自动跟随；未知模板降级直述模板串"""
        from app.services.ai import heading_standard
        from app.services.ai.prompts._shared import _render_heading_rules

        monkeypatch.setitem(
            heading_standard.DEFAULT_NUMBERING_TEMPLATES, 1,
            {"numbering_format": "custom", "numbering_template": "第{zh}章"},
        )
        text = _render_heading_rules()
        assert "第X节" not in text.split("二级")[0], "L1 描述应随模板变化"
        # 未在描述表中的模板 → 可见降级（直述模板串，不说谎）
        assert "第{zh}章" in text

    def test_rules_text_still_pinned_v31(self):
        """当前模板下的渲染结果仍钉住 V3.1 口径（与历史提示词一致）"""
        from app.services.ai.prompts._shared import SHARED_HEADING_RULES

        assert "V3.1" in SHARED_HEADING_RULES
        assert "- 一级：第X节" in SHARED_HEADING_RULES
        assert "- 四级：点分编号" in SHARED_HEADING_RULES
