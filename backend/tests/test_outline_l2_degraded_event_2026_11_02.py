"""L2 降级透知事件（BUG-FIX 2026-11-02）。

背景：2026-10-23 P0 修复把 L2「AI 失败 → 模板默认目录」的诊断做进了
`_generate_level2_v51(stats=...)` 出参，但消费端止步于
`generate_outline_v51` 里的一行 WARNING —— SSE/前端拿不到，用户看到的
是一份"生成成功"但多章为泛化默认目录（如「总体实施策划」）的目录树。

本组测试钉住：
1. L2 填充全部失败 → `event_emitter` 必须收到 `outline_degraded` 事件
   （字段：fallback_count / fallback_titles / degraded_ratio / phase）。
2. L2 正常填充 → 不得发送 `outline_degraded`（无回归、无噪声事件）。
3. 事件发送抛异常 → 主流程不受影响，目录照常返回。
"""

from __future__ import annotations

import re

import pytest

import app.services.ai.workflows_outline_v51 as wo


class _DeadProvider:
    """chat 直接失败：AI 自检等旁路调用全部走容错分支。"""

    class _Msg:
        def __init__(self, role, content):
            self.role = role
            self.content = content

    async def chat(self, messages, **kwargs):
        raise RuntimeError("offline: AI 不可用")


def _user_text(messages) -> str:
    # 标书智能体（4）提示词顺序优化后，user 侧是**多条**消息（稳定上下文块
    # 前置、任务差异后置），<json_outline> 骨架在**最后一条**里——必须合并
    # 全部 user 内容做判定，取第一条会漏检（旧 e2e 测试即踩此坑）。
    return "\n".join(
        m.content for m in messages
        if getattr(m, "role", "") == "user" and isinstance(m.content, str)
    )


_L1_ITEMS = [
    {"title": "总体施工部署", "children": []},
    {"title": "工程质量管理", "children": []},
]


async def _collect_fail_l2(provider, messages, **kwargs):
    """L1 阶段正常；L2 骨架填充（含 <json_outline>）一律失败。"""
    user = _user_text(messages)
    if "<json_outline>" in user:
        raise RuntimeError("e2e mock: L2 填充失败")
    return [dict(i, children=[]) for i in _L1_ITEMS]


async def _collect_ok_l2(provider, messages, **kwargs):
    """L1 与 L2 填充都成功（填充出具体标题，不触发降级）。"""
    import json as _json

    user = _user_text(messages)
    if "<json_outline>" in user:
        m = re.search(r"<json_outline>\n(.*?)\n</json_outline>", user, re.DOTALL)
        skeleton = _json.loads(m.group(1))
        # 骨架 L2 标题为空，填充校验要求非空——给每个 L2 唯一具体标题
        for i, l2 in enumerate(skeleton.get("children", [])):
            l2["title"] = l2.get("title") or f"分部工程划分与区段安排{i + 1}"
            for k, l3 in enumerate(l2.get("children") or []):
                l3["title"] = f"基坑支护变形监测点位{k + 1}号布设"
        return skeleton
    return [dict(i, children=[]) for i in _L1_ITEMS]


@pytest.mark.asyncio
async def test_l2_degraded_emits_outline_degraded_event(monkeypatch):
    """L2 全部章节回退默认目录 → 必须向前端发 outline_degraded 事件。"""
    monkeypatch.setattr(wo, "collect_json_response", _collect_fail_l2)

    events: list[dict] = []

    async def _emit(evt):
        events.append(evt)

    outline = await wo.generate_outline_v51(
        _DeadProvider(), "某房建工程概述", "[]", "建筑工程",
        auto_confirm=True, outline_mode="stepwise", event_emitter=_emit,
    )
    assert outline, "降级不应阻断目录产出"

    degraded = [e for e in events if e.get("status") == "outline_degraded"]
    assert degraded, f"未收到 outline_degraded 事件：{[e.get('status') for e in events]}"
    evt = degraded[0]
    assert evt["phase"] == "level2"
    assert evt["fallback_count"] >= 1
    assert isinstance(evt["fallback_titles"], list) and evt["fallback_titles"]
    assert 0.0 < evt["degraded_ratio"] <= 1.0
    assert "message" in evt and "模板默认目录" in evt["message"]
    # ENHANCE 2026-11-04：error_code 与日志口径一致，供监控按码聚合
    assert evt.get("error_code") == "OUTLINE_L2_DEGRADED", evt.get("error_code")


@pytest.mark.asyncio
async def test_no_degraded_event_when_l2_succeeds(monkeypatch):
    """L2 正常填充 → 不得出现 outline_degraded 噪声事件。"""
    monkeypatch.setattr(wo, "collect_json_response", _collect_ok_l2)

    events: list[dict] = []

    async def _emit(evt):
        events.append(evt)

    outline = await wo.generate_outline_v51(
        _DeadProvider(), "某房建工程概述", "[]", "建筑工程",
        auto_confirm=True, outline_mode="stepwise", event_emitter=_emit,
    )
    assert outline
    assert not [e for e in events if e.get("status") == "outline_degraded"], (
        f"L2 未降级却发送了降级事件: {events}"
    )


@pytest.mark.asyncio
async def test_degraded_event_emit_failure_does_not_break_flow(monkeypatch):
    """事件发送抛异常 → 主流程照常返回目录。"""
    monkeypatch.setattr(wo, "collect_json_response", _collect_fail_l2)

    async def _boom(evt):
        raise RuntimeError("SSE 队列已关闭")

    outline = await wo.generate_outline_v51(
        _DeadProvider(), "某房建工程概述", "[]", "建筑工程",
        auto_confirm=True, outline_mode="stepwise", event_emitter=_boom,
    )
    assert outline, "事件发送失败不得影响目录生成主流程"
