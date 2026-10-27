"""直出（方案一）路径与分步路径的输出质量口径一致性回归测试（2026-10-23）。

对齐 yibiao-simple 文档《AI 标书提纲生成工作流程》方案一对「层级完整」的要求：
一次性直出后仍需保证目录层级完整。本测试锁定直出路径的 L3（叶子）最小数量
必须与分步路径保持同一标尺（min_level3=5），避免直出产物比分步路径偏浅。
"""

import asyncio

from app.services.ai import workflows_outline_v51 as v51


def _sparse_direct_payload() -> dict:
    """模拟强模型一次性直出、但 L3 叶子偏少的产物（每 L2 仅 2 个 L3）。"""
    return {
        "outline": [
            {
                "title": "第一章 总体概述",
                "children": [
                    {"title": "1.1 项目背景", "children": [{"title": "a"}, {"title": "b"}]},
                    {"title": "1.2 编制原则", "children": [{"title": "c"}, {"title": "d"}]},
                ],
            }
        ]
    }


def test_direct_path_l3_min_caliber_matches_stepwise(monkeypatch):
    """直出路径对偏浅的 L3 叶子应补齐到每 L2 至少 5 个（与分步路径 min_level3=5 一致）。"""

    async def _fake_collect(provider, messages, **kwargs):
        return _sparse_direct_payload()

    monkeypatch.setattr(v51, "collect_json_response", _fake_collect)

    outline = asyncio.run(
        v51.generate_outline_direct(object(), "某市政道路工程概述", "[]", "市政工程")
    )
    assert outline, "直出路径未产出目录"
    assert len(outline) == 1, "单章输入应产出单章"

    for l1 in outline:
        for l2 in l1.get("children", []):
            l3 = l2.get("children", [])
            assert len(l3) >= 5, (
                f"直出路径 L3 叶子数={len(l3)} < 5，与分步路径口径（min_level3=5）不一致"
            )


def test_direct_path_falls_back_when_ai_returns_empty(monkeypatch):
    """AI 返回空时直出路径返回 []，由调用方回退分步路径（不静默丢目录）。"""

    async def _fake_collect(provider, messages, **kwargs):
        return []

    monkeypatch.setattr(v51, "collect_json_response", _fake_collect)

    outline = asyncio.run(
        v51.generate_outline_direct(object(), "某市政道路工程概述", "[]", "市政工程")
    )
    assert outline == [], "AI 空返回应触发回退（返回空列表）"
