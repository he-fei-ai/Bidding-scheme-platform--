"""全链路对抗探针（离线，不调 AI）——由 `.tmp_probe_e2e.py` 固化为正式回归测试。

背景（报告《报告_生成大纲模块深度探索与修复_2026-11-02.md》§八 遗留 2）：
上轮探查向 L2 骨架填充同时注入 22 种 L3 脏形态 + 4 种 L2 脏形态（占位家族、
括号编号后缀、中文序号、前缀形态），经「判脏→清洗→唯一化→落库安全网」全链
后 DB 泄漏 0。该探针证明了三道防线的有效性，但一次性脚本形态无法防后续回归，
故在此转成 pytest 断言钉住（脚本本身不入库——它依赖现网 DB / 顶层执行，不适合
collection）。

钉住的不变量：
1. 脏标题注入 L2/L3 后经 `generate_outline_v51` 全链，**不得有脏标题泄漏**
   （以 `_is_dirty_l2_title_deep` / `_is_dirty_l3_title_deep` 同口径复核）；
2. 清洗后**同级 L3 不得重名**（`_uniquify_outline_titles` 契约）；
3. 主流程不中断（探针场景 AI 旁路全失败，目录仍照常产出）；
4. 若终局 L1 仍有占位脏名，`outline_l1_placeholder` 事件必须透知（遗留 3 契约
   的 e2e 面）；L1 干净则该事件不得出现（无噪声）。
"""

from __future__ import annotations

import json
import re

import pytest

import app.services.ai.workflows_outline_v51 as wo
from app.services.ai.outline_common import (
    _is_dirty_l2_title_deep,
    _is_dirty_l3_title_deep,
)

# 广谱脏形态：占位家族 + 编号冲突后缀 + L2 级脏 + 前缀形态（与探针同源）
DIRTY_L3 = [
    "总体实施策划（2）", "总体实施策划（3）", "总体实施策划(2)", "专项6", "专项7",
    "专项（6）", "第六专项", "专项措施6", "内容3", "子章节2", "章节1", "第6项",
    "第六项", "总体实施策划第2项", "质量管控措施（第2项）", "分阶段推进专项13",
    "资源配置与协调保障（2）", "总结评估与持续改进（3）", "实施要点", "总体实施策划二",
    "2、总体实施策划", "（2）总体实施策划",
]
DIRTY_L2 = ["总体实施策划（2）", "专项6", "分阶段推进计划（3）", "内容1"]

_L1_ITEMS = [
    {"title": "总体施工部署", "children": []},
    {"title": "工程质量管理", "children": []},
    {"title": "安全生产管理", "children": []},
]


class _DeadProvider:
    """chat 直接失败：AI 自检等旁路调用全部走容错分支。"""

    class _Msg:
        def __init__(self, role, content):
            self.role = role
            self.content = content

    async def chat(self, messages, **kwargs):
        raise RuntimeError("offline: AI 不可用")


def _user_text(messages) -> str:
    # 提示词顺序优化后骨架在**最后一条** user 消息——合并全部 user 内容判定
    # （旧 e2e 测试 next() 只取第一条导致 L2 注入从未真实生效，见遗留 1）。
    return "\n".join(
        m.content for m in messages
        if getattr(m, "role", "") == "user" and isinstance(m.content, str)
    )


async def _collect_dirty_inject(provider, messages, **kwargs):
    """L1 正常；L2 填充成功但把骨架 L2/L3 标题全部替换为脏形态。"""
    user = _user_text(messages)
    if "<json_outline>" in user:
        m = re.search(r"<json_outline>\n(.*?)\n</json_outline>", user, re.DOTALL)
        skeleton = json.loads(m.group(1))
        for i, l2 in enumerate(skeleton.get("children", [])):
            # 同时注入 L2 级脏标题（此前旧测试只注入 L3）
            l2["title"] = DIRTY_L2[i % len(DIRTY_L2)]
            for k, l3 in enumerate(l2.get("children") or []):
                l3["title"] = DIRTY_L3[(i * 3 + k) % len(DIRTY_L3)]
        return skeleton
    return [dict(i, children=[]) for i in _L1_ITEMS]


@pytest.mark.asyncio
async def test_full_chain_dirty_titles_zero_leak(monkeypatch):
    """22+4 种脏形态灌入全链后：L2/L3 脏泄漏 0、同级重名 0、主流程不中断。"""
    monkeypatch.setattr(wo, "collect_json_response", _collect_dirty_inject)

    events: list[dict] = []

    async def _emit(evt):
        events.append(evt)

    outline = await wo.generate_outline_v51(
        _DeadProvider(), "某房建工程概述", "[]", "建筑工程",
        auto_confirm=True, outline_mode="stepwise", event_emitter=_emit,
    )
    assert outline, "对抗注入不应阻断目录产出"

    bad_l2: list[tuple] = []
    bad_l3: list[tuple] = []
    dupes: list[tuple] = []
    for l1 in outline:
        for l2 in l1.get("children") or []:
            t2 = (l2.get("title") or "").strip()
            if _is_dirty_l2_title_deep(t2):
                bad_l2.append((l1.get("title"), t2))
            l3_titles = []
            for l3 in l2.get("children") or []:
                t3 = (l3.get("title") or "").strip()
                if _is_dirty_l3_title_deep(t3):
                    bad_l3.append((t2, t3))
                l3_titles.append(t3)
            if len(l3_titles) != len(set(l3_titles)):
                dupes.append((t2, [t for t in l3_titles if l3_titles.count(t) > 1]))

    assert not bad_l2, f"L2 脏标题泄漏: {bad_l2}"
    assert not bad_l3, f"L3 脏标题泄漏: {bad_l3}"
    assert not dupes, f"同级 L3 重名: {dupes}"

    # 遗留 3 契约（e2e 面）：L1 章名由 mock 产出且干净 → 终局不得有占位透知噪声；
    # 若未来链路使脏名穿过 L1，则必须发事件而不是静默交付——两种情形二选一钉死。
    l1_dirty = [
        (l1.get("title") or "").strip() for l1 in outline
        if _is_dirty_l2_title_deep((l1.get("title") or "").strip())
    ]
    placeholder_evt = [e for e in events if e.get("status") == "outline_l1_placeholder"]
    if l1_dirty:
        assert placeholder_evt, f"L1 脏名 {l1_dirty} 静默交付，未透知"
    else:
        assert not placeholder_evt, f"L1 干净却出现占位透知噪声: {placeholder_evt}"


# ===== ENHANCE 2026-11-04（本轮）：L1 脏标题全链注入（L1 清洗分支的 e2e 面） =====
# 背景：`test_full_chain_dirty_titles_zero_leak` 只注入 L2/L3 脏形态；而 2026-11-02
# 新增的 `_cleanup_placeholder_titles` L1 分支（确定性剥号保主体）此前只有单元测试
# （test_outline_l1_cleanup_2026_11_02.py），无全链探针。本用例把脏 L1 形态灌入
# `generate_outline_v51`，钉住两条契约：
#   1. 「总体实施策划（2）」作 L1 → 确定性剥号 →「总体实施策划」（干净主体，不触发事件）；
#   2. 「专项6」作 L1（剥无可剥）→ 保持原样 + `outline_l1_placeholder` 事件透知（绝不静默）；
#   3. 主流程不中断（AI 全失败仍产出目录）。

_L1_DIRTY_ITEMS = [
    {"title": "总体实施策划（2）", "children": []},  # 可剥离 → 主体干净
    {"title": "专项6", "children": []},              # 剥无可剥 → 保持 + 透知
    {"title": "工程质量管理", "children": []},        # 合法 → 零改动
]


async def _collect_l1_dirty_inject(provider, messages, **kwargs):
    """L1 生成/自检返回脏 L1；L2 骨架调用返回骨架（保持结构合法）。"""
    user = _user_text(messages)
    if "<json_outline>" in user:
        m = re.search(r"<json_outline>\n(.*?)\n</json_outline>", user, re.DOTALL)
        skeleton = json.loads(m.group(1))
        # 骨架保留结构（L2/L3 数量由规则兜底填充），不注入额外脏 L3（本用例聚焦 L1）。
        return skeleton
    return [dict(i, children=[]) for i in _L1_DIRTY_ITEMS]


@pytest.mark.asyncio
async def test_full_chain_l1_dirty_titles_cleaned_or_notified(monkeypatch):
    """L1 脏形态全链：可剥者变干净主体、剥无可剥者透知、主流程不中断。"""
    monkeypatch.setattr(wo, "collect_json_response", _collect_l1_dirty_inject)

    events: list[dict] = []

    async def _emit(evt):
        events.append(evt)

    outline = await wo.generate_outline_v51(
        _DeadProvider(), "某房建工程概述", "[]", "建筑工程",
        auto_confirm=True, outline_mode="stepwise", event_emitter=_emit,
    )
    assert outline, "L1 脏注入不应阻断目录产出"

    titles = [(l1.get("title") or "").strip() for l1 in outline]
    # 契约 1：可剥离 L1「总体实施策划（2）」→ 主体「总体实施策划」（干净）
    assert "总体实施策划" in titles, f"可剥离 L1 未被清洗: {titles}"
    assert not any(_is_dirty_l2_title_deep(t) for t in titles if "总体实施策划" in t), titles
    # 契约 2：「专项6」保持原样（剥无可剥）+ outline_l1_placeholder 事件透知
    l1_dirty = [t for t in titles if _is_dirty_l2_title_deep(t)]
    placeholder_evt = [e for e in events if e.get("status") == "outline_l1_placeholder"]
    if l1_dirty:
        assert placeholder_evt, f"L1 脏名 {l1_dirty} 静默交付，未透知"
        assert "专项6" in l1_dirty, f"预期专项6 保留并透知，实际 {l1_dirty}"
    else:
        assert not placeholder_evt, f"L1 干净却出现占位透知噪声: {placeholder_evt}"
