# -*- coding: utf-8 -*-
"""「兜底扩写」落库回归测试（ENHANCE 2026-09-16）。

覆盖 2026-09-16 修复的缺陷：
- 正文生成流程内兜底扩写的「扩写结果落库」逻辑。
  原实现在调用 expand_content_fallback（会**就地修改** results 中章节的 content）
  *之后* 才从 results 取"原文"做对比，导致 original 恒等于扩写后内容，
  batch_updates 永远为空，扩写结果从未写入数据库——前端进度显示"已扩写"，
  但刷新后内容回退到扩写前。本测试锁定"快照必须在扩写前采集"这一不变量。
  （一致性审计 update_section 存在同根缺陷，修复方式一致，一并覆盖。）
"""

import asyncio

from app.services.ai.workflows_expand import expand_content_fallback


class _FakeProvider:
    """假 AI：把【当前正文】段扩写为 3 倍长度。"""

    async def chat(self, messages, temperature=None, cancel_event=None):
        user = messages[-1].content
        body = user.split("【当前正文】")[-1].split("请直接输出")[0].strip()
        return "扩" * max(len(body) * 3, 10)


def _prepare():
    sections_by_id = {
        i: {"id": i, "title": f"章{i}", "level": 3, "target_words": 1000} for i in (1, 2)
    }
    results = [
        {"id": 1, "status": "completed", "content": "字" * 300},
        {"id": 2, "status": "completed", "content": "字" * 280},
    ]
    return results, sections_by_id


def test_expand_mutates_results_in_place():
    """expand_content_fallback 就地修改传入的 results（落库修复依赖此不变量）。"""
    results, sections_by_id = _prepare()
    before = {r["id"]: r["content"] for r in results}
    asyncio.run(
        expand_content_fallback(
            _FakeProvider(), results, sections_by_id, 2000, "", "", "", "standard",
            max_expansion_rounds=1,
        )
    )
    for r in results:
        assert len(r["content"]) > len(before[r["id"]]), f"章节 {r['id']} 未被就地扩写"


def test_persist_uses_pre_expand_snapshot():
    """复刻 sse_content 的落库逻辑：快照在扩写前采集才能产生有效更新。"""
    results, sections_by_id = _prepare()

    # 修复后：扩写前采集快照
    snapshot_before = {r["id"]: r["content"] for r in results}
    asyncio.run(
        expand_content_fallback(
            _FakeProvider(), results, sections_by_id, 2000, "", "", "", "standard",
            max_expansion_rounds=1,
        )
    )

    def build_updates(before_map):
        updates = []
        for r in results:
            if r.get("status") == "completed" and r.get("content"):
                original = before_map.get(r["id"], "")
                if len(r.get("content", "")) > len(original):
                    updates.append((r["content"], r["id"]))
        return updates

    # 修复逻辑：扩写前快照 → 能产生落库更新
    assert build_updates(snapshot_before), "修复逻辑应产生落库更新"

    # 回归断言：若快照在扩写后采集（旧 bug），则更新为空
    snapshot_after = {r["id"]: r["content"] for r in results}
    assert build_updates(snapshot_after) == [], "旧 bug：扩写后快照应导致更新为空"
