# -*- coding: utf-8 -*-
"""「正文生成模块」深度加固回归测试（ENHANCE 2026-09-16）。

覆盖本轮修复的真实缺陷（每条都对应一处曾经的静默失效）：

A. 断点续生链路（P0）
   - `GenerationManager.pause_task` 会把任务标记 paused 但**保留**内存任务与
     content-generation 组锁；旧实现下前端「恢复」流程
     （resumeGeneration → startGeneration → create_task）会用**新 owner** 再抢一次
     同一把锁 → 必然 TaskLockConflict，「暂停后恢复」100% 失败。
   - 现改为：`create_task` 识别「已暂停且无活循环（attached=False）」的任务 → 复用
     其 generation_id 与锁凭据续跑。
   - `pause_generation` 必须**先 pause_task 再 stop_generation**（反序会让 SSE 的
     finally 先 finalize_task 把任务删掉 → pause_task 找不到任务 → 接口恒返回 error，
     而前端却谎报「已暂停」）。

B. 正文生成上下文注入（P1 死代码）
   - `_build_leaf_dicts` 原先不产出 `parent_id`，`generate_sections` 靠它构建
     「同级 / 上级章节」映射，缺字段使两张映射表恒空；
   - `build_chapter_content_messages` 只从**关键字参数**读 parent_chapters /
     sibling_chapters，而调用方是把它们放在 chapter_info 里传的。
   - 结果：「同级章节（避免内容重复）」「上级章节（承接关系）」从未下发过。

C. 章节内容缓存
   - 字数采样的探针必须绕过缓存（其 prompt 无事实变量/同级上下文，写进缓存会被
     正式生成直接复用）；
   - `get_stats()` 原恒返回 hits=0/misses=0/hit_rate="-"，诊断面板永远无意义。

D. 章节终态事件去重
   - SSE 主链路上终态事件由 `section_complete_callback` 统一派发；`_gen_one` 原先再发
     一次同 section_id 的 completed/failed，导致前端失败计数翻倍。

E. 兜底扩写端点
   - SSE 硬超时按真实工作量自适应（旧实现写死 900s，而扩写已改为「全部不足章节并发」，
     大项目必然被误杀）；
   - 并发度 / level2_default 必须透传给 `expand_content_fallback`。

改动文件：`app/services/generation_manager.py`、`app/routers/task_control.py`、
`app/routers/sse/sse_content.py`、`app/routers/sse/sse_utils.py`、
`app/services/ai/workflows_sections.py`、`app/services/ai/workflows_expand.py`。
"""

import asyncio
import json

import pytest

from app.db import get_db
from app.routers.sse.sse_utils import _build_leaf_dicts
from app.services.ai.workflows_expand import estimate_total_words_from_probe
from app.services.ai.workflows_sections import (
    SectionContentCache,
    _do_generate_section_with_optimizations,
    build_chapter_content_messages,
    generate_sections,
)
from app.services.generation_manager import generation_manager
from app.services.task_lock import TaskLockConflict


# ======================= 公共假件 =======================


class FakeProvider:
    """记录调用次数、返回固定 meta 结构的假提供商。"""

    def __init__(self, content: str = "正文" * 200, finish_reason: str = "stop"):
        self.calls = 0
        self._content = content
        self._finish_reason = finish_reason

    async def chat(self, messages, **kwargs):
        self.calls += 1
        return {"content": self._content, "finish_reason": self._finish_reason}


def _section(pid, title, level=3, parent_id=None, content="", status="pending", target=None):
    return {
        "id": pid,
        "parent_id": parent_id,
        "title": title,
        "level": level,
        "section_type": "text",
        "scoring_point_id": None,
        "status": status,
        "content": content,
        "target_words": target,
    }


# ======================= A. 断点续生 =======================


class TestPauseAndResume:
    def test_pause_keeps_task_and_lock(self):
        """暂停必须保留内存任务与任务组锁（断点续生的前提）。"""

        async def _run():
            pid = 987001
            gid = await generation_manager.create_task(pid, "v1", {"concurrency": 4})
            task = generation_manager._tasks[pid]
            assert task.attached is True

            assert await generation_manager.pause_task(pid) is True
            # 关键：任务仍在 _tasks 里（旧实现被 SSE finally 的 finalize 删掉）
            assert pid in generation_manager._tasks
            assert await generation_manager.is_paused(pid) is True
            assert generation_manager._tasks[pid].attached is False
            # 锁凭据保留
            assert generation_manager._tasks[pid].lock_key
            assert generation_manager._tasks[pid].lock_owner

            await generation_manager.finalize_task(pid, task_status="completed")
            assert pid not in generation_manager._tasks
            return gid

        asyncio.run(_run())

    def test_create_task_reuses_paused_task_and_lock(self):
        """P0 回归：暂停后再次 create_task 必须复用同一 generation_id，而不是冲突。"""

        async def _run():
            pid = 987002
            gid1 = await generation_manager.create_task(pid, "v1", {"concurrency": 4})
            lock_key = generation_manager._tasks[pid].lock_key
            lock_owner = generation_manager._tasks[pid].lock_owner

            await generation_manager.pause_task(pid)
            # 旧实现在这一行必抛 TaskLockConflict（新 owner 抢同组锁）
            gid2 = await generation_manager.create_task(pid, "v1", {"concurrency": 6})
            assert gid2 == gid1

            task = generation_manager._tasks[pid]
            assert task.attached is True
            assert await generation_manager.is_paused(pid) is False
            assert task.lock_key == lock_key
            assert task.lock_owner == lock_owner
            assert task.concurrency == 6

            await generation_manager.finalize_task(pid, task_status="completed")

        asyncio.run(_run())

    def test_running_task_still_conflicts(self):
        """真并发（还有活循环）必须继续被拒绝 —— 复用的前提是 attached=False。"""

        async def _run():
            pid = 987003
            await generation_manager.create_task(pid, "v1", {})
            try:
                with pytest.raises(TaskLockConflict):
                    await generation_manager.create_task(pid, "v1", {})
            finally:
                await generation_manager.finalize_task(pid, task_status="completed")

        asyncio.run(_run())

    def test_pause_generation_pauses_before_stopping_stream(self, monkeypatch):
        """P0 回归：pause_generation 必须先标记暂停，再停 SSE。"""
        from app.routers import task_control

        order: list[str] = []

        async def fake_pause(pid):
            order.append("pause_task")
            return True

        async def fake_stop(pid, mark_failed=True):
            order.append(f"stop_generation({mark_failed})")

        monkeypatch.setattr(generation_manager, "pause_task", fake_pause)
        monkeypatch.setattr(task_control, "stop_generation", fake_stop)

        result = asyncio.run(task_control.pause_generation(987004))
        assert order == ["pause_task", "stop_generation(False)"]
        assert result["status"] == "ok"

    def test_pause_generation_reports_failure(self, monkeypatch):
        """没有可暂停的任务时必须如实返回 error（前端据此不再谎报"已暂停"）。"""
        from app.routers import task_control

        async def fake_pause(pid):
            return False

        async def fake_stop(pid, mark_failed=True):
            return {"status": "ok"}

        monkeypatch.setattr(generation_manager, "pause_task", fake_pause)
        monkeypatch.setattr(task_control, "stop_generation", fake_stop)

        result = asyncio.run(task_control.pause_generation(987005))
        assert result["status"] == "error"


# ======================= B. 章节上下文注入 =======================


class TestLeafContextInjection:
    def _tree(self):
        """1(一级) → 2/3(二级)；2 → 4/5(三级叶子)；3 → 6(三级叶子)。"""
        sections = [
            _section(1, "总述", level=1),
            _section(2, "甲章", level=2, parent_id=1),
            _section(3, "乙章", level=2, parent_id=1),
            _section(4, "甲一", parent_id=2),
            _section(5, "甲二", parent_id=2),
            _section(6, "乙一", parent_id=3),
        ]
        sections_by_id = {s["id"]: s for s in sections}
        children_map: dict = {}
        for s in sections:
            children_map.setdefault(s["parent_id"], []).append(s)
        return sections_by_id, children_map

    def test_leaf_dicts_carry_parent_id(self):
        """BUG-FIX：leaf dict 缺 parent_id 会让同级/上级映射恒为空。"""
        sections_by_id, children_map = self._tree()
        leaves = _build_leaf_dicts(7, sections_by_id, children_map)
        assert {d["id"] for d in leaves} == {4, 5, 6}
        assert {d["id"]: d["parent_id"] for d in leaves} == {4: 2, 5: 2, 6: 3}
        assert all("status" in d for d in leaves)

    def test_prompt_includes_sibling_and_parent_context(self):
        """BUG-FIX：两张上下文表原先恒空，注入分支从不执行。

        ENHANCE 2026-10-05（P-22）：上下文字段已拆到独立分层消息，
        断言改为「跨全部消息聚合」，语义等价（上级/同级内容确实被注入）。
        """
        msgs = build_chapter_content_messages(
            {
                "title": "甲一",
                "parent_chapters": [{"title": "总述"}, {"title": "甲章"}],
                "sibling_chapters": [{"title": "甲二"}],
            }
        )
        prompt = "\n".join(m.content for m in msgs)
        assert "上级章节" in prompt
        assert "同级章节" in prompt
        assert "甲二" in prompt

    def test_explicit_kwargs_still_win(self):
        """显式关键字参数（generate_sections 注入的 _local_ctx）优先于 chapter_info。

        ENHANCE 2026-10-05（P-22）：断言改为跨全部消息聚合。
        """
        msgs = build_chapter_content_messages(
            {"title": "甲一", "sibling_chapters": [{"title": "来自chapter_info"}]},
            sibling_chapters=[{"title": "来自kwargs"}],
        )
        prompt = "\n".join(m.content for m in msgs)
        assert "来自kwargs" in prompt
        assert "来自chapter_info" not in prompt


# ======================= C. 章节内容缓存 =======================


class TestSectionContentCache:
    def test_use_cache_false_bypasses_cache(self):
        """BUG-FIX：探针（字数采样）不得写入/读取章节内容缓存。"""
        provider = FakeProvider()
        section = {"id": 9101, "title": "采样章"}
        ctx = {"project_id": 987006, "use_cache": False}

        async def _run():
            await _section_content_cache_reset()
            c1 = await _do_generate_section_with_optimizations(provider, section, ctx)
            c2 = await _do_generate_section_with_optimizations(provider, section, ctx)
            cached = await _cache().get("section_content:987006:9101")
            return c1, c2, cached

        c1, c2, cached = asyncio.run(_run())
        assert provider.calls == 2, "绕过缓存后应每次都真正调用 AI"
        assert cached is None, "探针结果不得污染正式生成的缓存"
        assert c1 == c2

    def test_use_cache_true_reads_and_writes(self):
        provider = FakeProvider()
        section = {"id": 9102, "title": "正式章"}
        ctx = {"project_id": 987007}

        async def _run():
            await _section_content_cache_reset()
            c1 = await _do_generate_section_with_optimizations(provider, section, ctx)
            c2 = await _do_generate_section_with_optimizations(provider, section, ctx)
            return c1, c2

        c1, c2 = asyncio.run(_run())
        assert provider.calls == 1
        assert c1 == c2

    def test_stats_track_hits_and_misses(self):
        """BUG-FIX：get_stats 原先恒为 0/-，诊断面板永远无意义。"""
        cache = SectionContentCache(max_size=5)

        async def _run():
            assert await cache.get("missing") is None
            await cache.set("section_content:1:2", "内容")
            assert await cache.get("section_content:1:2") == "内容"

        asyncio.run(_run())
        stats = cache.get_stats()
        assert stats["hits"] == 1
        assert stats["misses"] == 1
        assert stats["hit_rate"] == "50.0%"
        assert stats["cache_size"] == 1

    def test_probe_bypasses_cache(self, monkeypatch):
        """字数采样的 probe_context 必须带 use_cache=False。"""
        import app.services.ai.workflows_expand as we

        captured: dict = {}

        async def fake_gen(provider, section, context, progress_callback=None):
            captured["context"] = context
            return "字" * 500

        monkeypatch.setattr(we, "generate_section_content", fake_gen)
        total = asyncio.run(
            we.estimate_total_words_from_probe(
                object(),
                [{"id": 1, "title": "甲", "level": 3, "target_words": 300}],
                "",
                "摘要",
                "",
                [],
                "standard",
                total_leaves=10,
            )
        )
        assert captured["context"].get("use_cache") is False
        assert total == 5000


def _cache():
    from app.services.ai.workflows_sections import _section_content_cache

    return _section_content_cache


async def _section_content_cache_reset():
    await _cache().clear()


# ======================= D. 终态事件去重 =======================


class TestTerminalProgressDedup:
    def _run(self, with_complete_callback: bool):
        events: list[tuple] = []

        async def progress_callback(sid, title, status, progress, message, word_count=0):
            events.append((sid, status))

        async def complete_callback(result):
            events.append((result["id"], "section_complete_callback"))

        provider = FakeProvider()
        sections = [{"id": 9201, "title": "甲", "parent_id": None}]

        asyncio.run(
            generate_sections(
                provider,
                sections,
                concurrency=1,
                progress_callback=progress_callback,
                section_complete_callback=complete_callback if with_complete_callback else None,
                project_id=987008,
                cancel_event=asyncio.Event(),
            )
        )
        return events

    def test_no_duplicate_terminal_event_with_complete_callback(self):
        """BUG-FIX：终态由 section_complete_callback 统一派发，不得在此处重发。"""
        events = self._run(True)
        assert (9201, "completed") not in events
        assert (9201, "section_complete_callback") in events

    def test_terminal_event_kept_without_complete_callback(self):
        """非 SSE 调用方（无 complete 回调）仍需拿到终态进度事件。"""
        events = self._run(False)
        assert (9201, "completed") in events


# ======================= E. 兜底扩写端点 =======================


class TestExpandEndpointHardening:
    def _seed_project(self, project_id: int, section_count: int) -> None:
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO projects (id, name, industry) VALUES (?, ?, ?)",
                (project_id, f"测试项目{project_id}", "建筑工程"),
            )
            conn.execute(
                "INSERT OR REPLACE INTO analysis (project_id, summary_json) VALUES (?, ?)",
                (project_id, "项目摘要"),
            )
            conn.execute("DELETE FROM sections WHERE project_id = ?", (project_id,))
            for i in range(section_count):
                conn.execute(
                    """INSERT INTO sections
                       (project_id, title, level, sort_order, section_type, content, status, target_words)
                       VALUES (?, ?, 3, ?, 'text', ?, 'completed', 1000)""",
                    (project_id, f"章节{i}", i, "字" * 50),
                )

    def test_timeout_scales_with_workload_and_concurrency_passed(self, monkeypatch):
        """旧实现 SSE 硬超时写死 900s：扩写改为「全部不足章节并发」后必然被误杀。"""
        import app.services.ai.provider_factory as pf
        import app.services.ai.workflows_expand as we
        from app.routers.sse.sse_content import expand_content_impl

        project_id = 987009
        self._seed_project(project_id, 40)

        async def fake_get_working_provider(_cfg):
            return FakeProvider(), None

        captured: dict = {}

        async def fake_expand(provider, results, sections_by_id, minimum_words, *args, **kwargs):
            captured["concurrency"] = kwargs.get("concurrency")
            captured["level2_default"] = kwargs.get("level2_default")
            return results

        monkeypatch.setattr(pf, "get_working_provider", fake_get_working_provider)
        monkeypatch.setattr(we, "expand_content_fallback", fake_expand)

        async def _run():
            # 注意：直接调用实现函数时 FastAPI 的 Query 默认值不会生效，
            # level2_default 必须显式传入（否则拿到的是 Query 对象本身）。
            resp = await expand_content_impl(
                project_id, writing_length="standard", level2_default=3500, concurrency=8
            )
            chunks = []
            async for chunk in resp.body_iterator:
                chunks.append(chunk)
                if '"status": "completed"' in chunk or '"status": "error"' in chunk:
                    break
            return "".join(chunks)

        text = asyncio.run(_run())
        payloads = [
            json.loads(line[len("data: "):])
            for line in text.split("\n")
            if line.startswith("data: ")
        ]
        gen_events = [p for p in payloads if p.get("status") == "generating"]
        assert gen_events, f"未收到 generating 事件: {text[:400]}"
        event = gen_events[-1]
        assert event["total"] == 40
        assert event["concurrency"] == 8
        # 40 章 × 3 轮 × 120s ÷ 并发 8 + 300s ≈ 2100s，必须大于旧的写死值 900s
        assert event["estimated_seconds"] > 900
        assert event["timeout_seconds"] > 900

        assert captured["concurrency"] == 8
        assert captured["level2_default"] == 3500
