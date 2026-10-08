# -*- coding: utf-8 -*-
"""「兜底扩写」工具栏链路回归测试（ENHANCE 2026-09-15）。

覆盖本轮修复/增强：
A. per-section 扩写阈值（修复 L3 小节被 max(target)×80% 强拉到 ~2800 字的口径缺陷）；
B. collect_deficient_sections 筛选与排序；
C. expand_content_fallback 并发扩写（旧实现每轮只救 1 章）、取消、质量闸、回调；
D. P0：expand_content_impl 必须返回 StreamingResponse（此前漏写 return，
   端点返回 null，SSE 流建立不起来，兜底扩写从未真正可用）；
E. 任务组锁互斥（与正文生成同组，生成进行中扩写必须被拦截）。
"""

import asyncio

import pytest
from starlette.responses import StreamingResponse

from app.services.ai.workflows_expand import (
    collect_deficient_sections,
    expand_content_fallback,
    section_expand_threshold,
)


# ======================= A. 阈值口径 =======================


class TestSectionExpandThreshold:
    def test_target_words_take_priority(self):
        # target 1000 → 800（不超过平铺阈值 2000）
        assert section_expand_threshold({"target_words": 1000, "level": 3}, 2000) == 800

    def test_level_tier_when_no_target(self):
        # 2026-10-09 方案三契约更新：字数控制下沉到三级目录（L3）自身正文——
        # L1/L2 父节点不参与扩写（阈值 0）；L3 无 target / 无档位 →
        # L3 自身正文 300 字 × 80% = 240。
        assert section_expand_threshold({"level": 3}, 2000) == 240
        assert section_expand_threshold({"level": 2}, 2000, level2_default=3500) == 0
        assert section_expand_threshold({"level": 2}, 5000, level2_default=3500) == 0

    def test_l4_falls_back_to_default_tier(self):
        # 方案三：四档是「L3 子树总字数」口径；L4+ 深层叶子无显式配置时
        # 回落默认档位（standard 1200）× 80% = 960，而不是被 L3 的 300 拉低。
        assert section_expand_threshold({"level": 4}, 2000) == 960
        assert section_expand_threshold({"level": 4}, 2000, writing_length="detailed") == 1600

    def test_l3_explicit_tier_wins_over_l3_own(self):
        # L3 显式指定档位时按档位（子树总字数）× 80% 计算，不走 L3 自身 300。
        assert section_expand_threshold({"level": 3, "word_count_tier": "detailed"}, 2000) == 1600

    def test_floor_200(self):
        # 极小 target 也要保底 200，避免阈值过小永远「达标」
        assert section_expand_threshold({"target_words": 100}, 2000) == 200

    def test_flat_minimum_caps_everything(self):
        # 2026-10-08 契约更新：阈值仅对 L3+ 生效（L2 返回 0），
        # 平铺阈值封顶用 L3 验证：min(2000, 9000×80%) = 2000
        assert section_expand_threshold({"target_words": 9000, "level": 3}, 2000) == 2000


# ======================= B. 不足章节筛选 =======================


class TestCollectDeficient:
    def _build(self):
        sections_by_id = {
            1: {"id": 1, "title": "甲", "level": 3, "target_words": 1000},
            2: {"id": 2, "title": "乙", "level": 3, "target_words": 500},
            3: {"id": 3, "title": "丙", "level": 3, "target_words": 1000},
        }
        results = [
            {"id": 1, "status": "completed", "content": "字" * 300},   # 300/800 → 0.375
            {"id": 2, "status": "completed", "content": "字" * 100},   # 100/400 → 0.25（最缺）
            {"id": 3, "status": "completed", "content": "字" * 900},   # 达标
            {"id": 4, "status": "pending", "content": ""},             # 待生成，不归扩写管
            {"id": 5, "status": "completed", "content": ""},           # 空内容，不归扩写管
        ]
        return results, sections_by_id

    def test_filter_and_order(self):
        results, sections_by_id = self._build()
        deficient = collect_deficient_sections(results, sections_by_id, 2000)
        assert [entry["result"]["id"] for entry in deficient] == [2, 1]
        assert deficient[0]["deficit"] == 400 - 100

    def test_none_deficient(self):
        results, sections_by_id = self._build()
        results[0]["content"] = "字" * 2000
        results[1]["content"] = "字" * 2000
        assert collect_deficient_sections(results, sections_by_id, 2000) == []


# ======================= C. 并发扩写 =======================


class FakeProvider:
    """假 AI 提供商：返回比原文更长的内容；可注入异常与取消。"""

    def __init__(self, reply_factory=None, fail_ids=()):
        self.calls: list[int] = []
        self.reply_factory = reply_factory
        self.fail_ids = set(fail_ids)

    async def chat(self, messages, temperature=None, cancel_event=None):
        # 从提示词里解析不出 id，用调用顺序计数由测试侧断言
        self.calls.append(1)
        if self.fail_ids and len(self.calls) in self.fail_ids:
            raise RuntimeError("模拟 AI 失败")
        user = messages[-1].content
        # 从【当前正文】段取原文字数，扩写为 3 倍
        body = user.split("【当前正文】")[-1].split("【章节标题】")[0]
        body = body.split("请直接输出")[0].strip()
        return "扩" * max(len(body) * 3, 10)


class TestExpandContentFallback:
    def _prepare(self):
        sections_by_id = {
            i: {"id": i, "title": f"章{i}", "level": 3, "target_words": 1000} for i in (1, 2, 3)
        }
        results = [
            {"id": 1, "status": "completed", "content": "字" * 300},
            {"id": 2, "status": "completed", "content": "字" * 280},
            {"id": 3, "status": "completed", "content": "字" * 900},
        ]
        return results, sections_by_id

    def test_all_deficient_sections_expanded_in_one_call(self):
        # 旧实现 max_expansion_rounds=1 只能救 1 章；新实现一次调用全部达标。
        # 2026-10-08 契约更新：单节点 target_words 优先于全局档位——
        # target 1000 → 阈值 800，300/280 两章不足触发扩写；
        # 章节3（900 ≥ 800）本就达标，不消耗 AI 调用。
        results, sections_by_id = self._prepare()
        provider = FakeProvider()
        out = asyncio.run(
            expand_content_fallback(
                provider, results, sections_by_id, 2000, "", "", "", "standard",
                max_expansion_rounds=1,
            )
        )
        reached = [r["id"] for r in out if r["id"] in (1, 2, 3) and len(r["content"]) > 300]
        assert sorted(reached) == [1, 2, 3]
        assert len(provider.calls) == 2

    def test_cancel_event_blocks_all_calls(self):
        results, sections_by_id = self._prepare()
        cancel = asyncio.Event()
        cancel.set()
        provider = FakeProvider()
        out = asyncio.run(
            expand_content_fallback(
                provider, results, sections_by_id, 2000, "", "", "", "standard",
                cancel_event=cancel,
            )
        )
        assert provider.calls == []
        assert out[0]["content"] == "字" * 300

    def test_quality_gate_rejects_shorter_reply(self):
        results, sections_by_id = self._prepare()
        provider = FakeProvider()
        provider.reply_factory = None

        class ShortProvider:
            calls = 0

            async def chat(self, messages, temperature=None, cancel_event=None):
                ShortProvider.calls += 1
                return "短"  # 比原文短 → 质量闸必须拒绝

        out = asyncio.run(
            expand_content_fallback(
                ShortProvider(), results, sections_by_id, 2000, "", "", "", "standard",
                max_expansion_rounds=2,
            )
        )
        assert out[0]["content"] == "字" * 300

    def test_provider_failure_does_not_break_others(self):
        results, sections_by_id = self._prepare()

        class FlakyProvider:
            def __init__(self):
                self.n = 0

            async def chat(self, messages, temperature=None, cancel_event=None):
                self.n += 1
                if self.n == 1:
                    raise RuntimeError("模拟失败")
                body = messages[-1].content.split("【当前正文】")[-1].split("【章节标题】")[0]
                body = body.split("请直接输出")[0].strip()
                return "扩" * (len(body) * 3)

        out = asyncio.run(
            expand_content_fallback(
                FlakyProvider(), results, sections_by_id, 2000, "", "", "", "standard",
                max_expansion_rounds=2, concurrency=1,
            )
        )
        # 串行下按缺口率排序：章 2（280/800）先处理且第 1 次调用失败，
        # 章 1（300/800）后处理成功 —— 单章失败不影响其它章节
        assert len(out[0]["content"]) > 300
        assert out[1]["content"] == "字" * 280

    def test_on_section_done_callback(self):
        results, sections_by_id = self._prepare()
        provider = FakeProvider()
        done: list[int] = []
        asyncio.run(
            expand_content_fallback(
                provider, results, sections_by_id, 2000, "", "", "", "standard",
                max_expansion_rounds=1,
                on_section_done=lambda sid, title: done.append(sid),
            )
        )
        # 2026-10-08 契约更新：单节点 target 1000 → 阈值 800。
        # 回调仅在达标后触发：章 1（300→900）、章 2（280→840）单轮扩写后
        # 均 ≥ 800 达标回调；章 3（900）本就达标不进入扩写、无扩写回调。
        assert sorted(done) == [1, 2]

    def test_no_deficient_short_circuits(self):
        results, sections_by_id = self._prepare()
        results[0]["content"] = "字" * 5000
        results[1]["content"] = "字" * 5000
        results[2]["content"] = "字" * 5000
        provider = FakeProvider()
        asyncio.run(
            expand_content_fallback(
                provider, results, sections_by_id, 2000, "", "", "", "standard",
            )
        )
        assert provider.calls == []


# ======================= D/E. 端点契约 =======================


class TestExpandEndpointContract:
    def test_impl_returns_streaming_response(self):
        """P0 回归：expand_content_impl 曾漏写 return StreamingResponse，
        端点返回 null，SSE 流建立不起来，兜底扩写从未真正可用。"""
        from app.routers.sse.sse_content import expand_content_impl

        async def _run():
            resp = await expand_content_impl(987654)
            assert isinstance(resp, StreamingResponse)
            # 只取 connected 事件即关闭，避免真的跑扩写
            iterator = resp.body_iterator.__aiter__()
            first = await iterator.__anext__()
            await resp.body_iterator.aclose()
            return first

        first = asyncio.run(_run())
        assert "connected" in first

    def test_expand_conflicts_with_running_generation(self):
        """任务组锁：正文生成持有 content-generation 锁时，扩写必须被拦截。"""
        from app.routers.sse.sse_content import expand_content_impl
        from app.services import task_lock

        record = task_lock.acquire("content-generation", project_id=987655, note="gen-running")
        assert record.get("acquired")
        try:

            async def _run():
                resp = await expand_content_impl(987655)
                iterator = resp.body_iterator.__aiter__()
                chunks = []
                for _ in range(2):
                    try:
                        chunks.append(await iterator.__anext__())
                    except StopAsyncIteration:
                        break
                await resp.body_iterator.aclose()
                return "".join(chunks)

            text = asyncio.run(_run())
            assert "TASK_LOCK_CONFLICT" in text
        finally:
            task_lock.release(record["lock_key"], record["owner_id"])

    def test_lock_released_after_stream_close(self):
        """流关闭（含中途断开）后必须释放任务组锁，否则项目被永久锁死。"""
        from app.routers.sse.sse_content import expand_content_impl
        from app.services import task_lock

        async def _run():
            resp = await expand_content_impl(987656)
            iterator = resp.body_iterator.__aiter__()
            await iterator.__anext__()
            await resp.body_iterator.aclose()

            def _held():
                state = task_lock.read_lock_state_for_project(987656) if hasattr(
                    task_lock, "read_lock_state_for_project"
                ) else None
                return state

            return _held

        asyncio.run(_run())
        with __import__("app.db", fromlist=["get_db"]).get_db() as conn:
            rows = conn.execute(
                "SELECT COUNT(*) AS cnt FROM task_locks WHERE project_id = ? AND status = 'active'",
                (987656,),
            ).fetchone()
        assert rows["cnt"] == 0
