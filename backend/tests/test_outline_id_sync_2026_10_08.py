"""V5.1 目录生成：位置式 id 编号统一与安全网（2026-10-08 修复验证）。

覆盖：
- 修复点 1：直出路径（generate_outline_v51, outline_mode="direct"）原本不经门禁
  rennumber，返回树缺 id / id 与位置不一致；现落库前统一重排，直出与分步产物契约一致。
- 修复点 2：最终审核的人工编辑/重排发生在门禁 rennumber 之后，返回前按最终真实结构
  重排 id，修复「标题修改后编号未重算」。
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services.ai import workflows_outline_v51 as wo  # noqa: E402


def _fake_provider():
    return object()


def test_generate_outline_v51_direct_assigns_position_ids(monkeypatch):
    """直出路径也须产出位置正确的 id（1 / 1.1 / 1.1.1），含被修正的错误 id。"""

    async def _fake_collect(provider, messages, **kwargs):
        # 故意给出「错乱」的 id（"WRONG"、缺位），用于验证重排兜底
        return [
            {
                "id": "1",
                "title": "第一章 总体实施方案",
                "children": [
                    {
                        "id": "1.1",
                        "title": "安全管理机构",
                        "children": [
                            {"id": "WRONG", "title": "安全技术措施", "children": []},
                        ],
                    }
                ],
            },
            {"id": "2", "title": "第二章 质量管理", "children": []},
        ]

    monkeypatch.setattr(wo, "collect_json_response", _fake_collect)
    # outline_mode="direct" 强制直出；v51_state=None 跳过审核直接返回
    outline = asyncio.run(
        wo.generate_outline_v51(
            _fake_provider(),
            "某市政道路工程概述",
            "[]",
            "市政工程",
            auto_confirm=True,
            outline_mode="direct",
        )
    )
    assert outline, "直出路径未产出目录"
    # L1 位置编号
    assert outline[0]["id"] == "1"
    assert outline[1]["id"] == "2"
    # L2 位置编号（_ensure_min_children 会把 L2 补到 >=3，索引 0 仍是我们那支）
    assert outline[0]["children"][0]["id"] == "1.1"
    # 错误 id "WRONG" 被重排为位置式 1.1.1
    assert outline[0]["children"][0]["children"][0]["id"] == "1.1.1"
    # 门禁事实校验：无非法/重复 id
    facts = wo.outline_gate.compute_outline_facts(outline)
    assert not facts["invalid_id_nodes"], "直出路径产出非法/重复编号"


def test_generate_outline_v51_final_review_edit_gets_renumbered(monkeypatch):
    """最终审核后的用户编辑（可能重排/改顺序）在返回前被重排 id，编号不残留错乱。

    直接 mock 各子步骤，跳过真实 AI 调用：
    - L1/L2/L3/自检 产出「门禁已正确编号」的 tree；
    - _wait_for_v51_review 在最终审核时返回用户塞入的「id 错乱」编辑稿（edited_outline）。
    修复点要求：最终 return 前按真实结构重排 id，因此返回树必须是位置正确的 1/2/1.1。
    """

    base = [
        {"title": "总体实施方案", "children": [{"title": "安全管理机构", "children": []}]},
        {"title": "质量管理", "children": []},
    ]

    async def _fake_l1(*a, **k):
        return base

    async def _fake_l2(*a, **k):
        return base

    async def _fake_l3(*a, **k):
        return base

    async def _fake_self_check(*a, **k):
        # 门禁按位置重排（模拟修复前已有正确 id）
        out, _ = wo.outline_gate.renumber_outline_ids(base)
        return out

    async def _fake_review(queue, state, phase, outline, **kwargs):
        # 三处审核点统一返回：有 edited_outline 则返回编辑稿（模拟最终人工编辑），否则原样返回
        return state.edited_outline if state.edited_outline is not None else outline

    monkeypatch.setattr(wo, "_generate_level1_v51", _fake_l1)
    monkeypatch.setattr(wo, "_generate_level2_v51", _fake_l2)
    monkeypatch.setattr(wo, "_generate_level3_v51", _fake_l3)
    monkeypatch.setattr(wo, "_self_check_v51", _fake_self_check)
    monkeypatch.setattr(wo, "_wait_for_v51_review", _fake_review)

    from app.services import v51_state as v51_mod

    v51_mod.create_v51_state(999)
    state = v51_mod.get_v51_state(999)
    # 模拟用户在最终审核时把章节顺序打乱、编号写错
    state.edited_outline = [
        {
            "id": "X",
            "title": "第二章 质量管理",
            "children": [{"id": "Y", "title": "创优计划", "children": []}],
        },
        {"id": "Z", "title": "第一章 总体实施方案", "children": []},
    ]

    outline = asyncio.run(
        wo.generate_outline_v51(
            _fake_provider(),
            "某工程概述",
            "[]",
            "建筑工程",
            auto_confirm=True,
            outline_mode="stepwise",
            v51_state=state,
        )
    )
    # 返回前按最终真实结构重排：第一条应为 1、其下 1.1；第二条为 2
    assert outline[0]["id"] == "1"
    assert outline[1]["id"] == "2"
    assert outline[0]["children"][0]["id"] == "1.1"
    facts = wo.outline_gate.compute_outline_facts(outline)
    assert not facts["invalid_id_nodes"], "最终审核编辑后仍存在非法/重复编号"
    v51_mod.remove_v51_state(999)
