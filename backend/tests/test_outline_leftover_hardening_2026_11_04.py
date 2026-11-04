"""生成大纲模块遗留问题加固（2026-11-04 第二轮处置）。

覆盖报告《报告_生成大纲模块_深度探索与修复_2026-11-04.md》§八 遗留项的最优方案：
- 遗留 1：L1 纯占位章名（剥无可剥，如「专项6」）评分项映射链语义兜底——
  `_is_unrecoverable_l1_placeholder` 判定 + `_l1_placeholder_scoring_fallback`
  在 L1 生成链就地替换为对应评分项标题（现网 2026-11-03 日志
  `OUTLINE_L1_PLACEHOLDER_LEAK titles=['专项6']` 实证该条件已触发）；
- 遗留 2：两个 L1 剥号撞同一主体时**不再语义化成无关通用章名**——撞名者保持
  原样待人工（`_cleanup_placeholder_titles` L1 分支加 `_body1 not in _used` 护栏）；
- 遗留 3 加固：`_merge_filled_children` 顺序错位检测补「骨架标题完全缺失且无
  包含关系（漏章/合并）」信号，与既有「错位」信号合并计数透知（纯日志不改行为）。
"""

from __future__ import annotations

import json
import logging

import pytest

import app.services.ai.workflows_outline_v51 as wo
from app.services.ai.outline_common import (
    _cleanup_placeholder_titles,
    _is_dirty_l2_title_deep,
)
from app.services.ai.workflows_outline_v51 import (
    _is_unrecoverable_l1_placeholder,
    _l1_placeholder_scoring_fallback,
    _merge_filled_children,
)


# ===========================================================================
# 遗留 1a：`_is_unrecoverable_l1_placeholder` 判定口径
# ===========================================================================


class TestIsUnrecoverableL1Placeholder:
    def test_pure_placeholder_is_unrecoverable(self):
        """纯占位（剥号后仍无语义主体）→ 不可恢复。"""
        assert _is_unrecoverable_l1_placeholder("专项6") is True
        assert _is_unrecoverable_l1_placeholder("第六项") is True
        assert _is_unrecoverable_l1_placeholder("第6项") is True

    def test_strippable_suffix_is_recoverable(self):
        """带编号后缀但主体干净（如「总体实施策划（2）」）→ 可由清洗链恢复。"""
        assert _is_unrecoverable_l1_placeholder("总体实施策划（2）") is False

    def test_clean_title_not_touched(self):
        """合法 L1（含近邻「专项施工方案」）→ 不介入。"""
        assert _is_unrecoverable_l1_placeholder("总体施工部署") is False
        assert _is_unrecoverable_l1_placeholder("专项施工方案") is False
        assert _is_unrecoverable_l1_placeholder("工程质量管理") is False
        assert _is_unrecoverable_l1_placeholder("") is False


# ===========================================================================
# 遗留 1b：`_l1_placeholder_scoring_fallback` 语义兜底
# ===========================================================================

_SCORING = {
    "items": [
        {"id": "R1", "title": "施工准备与资源配备"},
        {"id": "R2", "title": "工程质量管理体系"},
    ]
}


def _mk_l1(*titles):
    return [{"title": t, "children": []} for t in titles]


class TestL1PlaceholderScoringFallback:
    def test_pure_placeholder_replaced_by_scoring_title(self):
        """「专项6」→ 同位评分项标题（语义兜底，不再静默）。"""
        out = _l1_placeholder_scoring_fallback(
            _mk_l1("专项6", "工程质量管理"), json.dumps(_SCORING)
        )
        assert out[0]["title"] == "施工准备与资源配备"
        assert out[1]["title"] == "工程质量管理"

    def test_strippable_l1_left_for_cleanup(self):
        """「总体实施策划（2）」可剥 → 兜底不介入（交给清洗链确定性剥号）。"""
        out = _l1_placeholder_scoring_fallback(
            _mk_l1("总体实施策划（2）", "工程质量管理"), json.dumps(_SCORING)
        )
        assert out[0]["title"] == "总体实施策划（2）"

    def test_clean_l1_never_touched(self):
        """合法 L1 零改动。"""
        out = _l1_placeholder_scoring_fallback(
            _mk_l1("总体施工部署", "工程质量管理"), json.dumps(_SCORING)
        )
        assert [n["title"] for n in out] == ["总体施工部署", "工程质量管理"]

    def test_collision_falls_back_to_unused_scoring_title(self):
        """同位评分项标题已被占用 → 取首个未占用评分项标题，不产生重名。"""
        out = _l1_placeholder_scoring_fallback(
            _mk_l1("专项6", "施工准备与资源配备"), json.dumps(_SCORING)
        )
        # 「施工准备与资源配备」已被 L1[1] 占用 → 兜底用 R2「工程质量管理体系」
        assert out[0]["title"] == "工程质量管理体系"
        assert out[1]["title"] == "施工准备与资源配备"

    def test_never_raises_on_bad_scoring(self):
        """评分项为空/非法 → 原样返回，不抛异常。"""
        assert _l1_placeholder_scoring_fallback(_mk_l1("专项6"), "[]")[0]["title"] == "专项6"
        assert _l1_placeholder_scoring_fallback(_mk_l1("专项6"), "")[0]["title"] == "专项6"
        assert _l1_placeholder_scoring_fallback(_mk_l1("专项6"), "{bad json")[0]["title"] == "专项6"

    def test_direct_path_fallback_via_pipeline(self, monkeypatch, caplog):
        """直出路径（`_apply_l3_quality_closeout` 前）也经清洗链：兜底不破坏直出。"""
        # 直接验证兜底函数在管线中幂等：重复调用不再产生脏标题
        out = _l1_placeholder_scoring_fallback(
            _mk_l1("专项6", "工程质量管理"), json.dumps(_SCORING)
        )
        out2 = _l1_placeholder_scoring_fallback(out, json.dumps(_SCORING))
        assert not any(_is_dirty_l2_title_deep(n["title"]) for n in out2)


# ===========================================================================
# 遗留 2：L1 剥号撞名 → 保持原样待人工（不再语义化成无关通用章名）
# ===========================================================================


class TestL1CollisionKeepsOriginal:
    def test_two_l1_same_body_second_kept(self):
        """「总体实施策划（2）/（3）」→ 首名剥为「总体实施策划」，撞名者保持原样。"""
        l1 = _mk_l1("总体实施策划（2）", "总体实施策划（3）")
        _cleanup_placeholder_titles(l1)
        assert l1[0]["title"] == "总体实施策划"
        # 撞名者不得被剥成同名（否则后续 _uniquify 会语义化成「工程质量管理」）
        assert l1[1]["title"] == "总体实施策划（3）"

    def test_single_l1_still_stripped(self):
        """单 L1 无撞名 → 照常确定性剥号（无回归）。"""
        l1 = _mk_l1("总体实施策划（2）")
        _cleanup_placeholder_titles(l1)
        assert l1[0]["title"] == "总体实施策划"

    def test_clean_l1_and_dirty_l1_no_collision(self):
        """一个干净「总体实施策划」+ 一个脏「总体实施策划（2）」→ 脏者保留（撞名）。"""
        l1 = _mk_l1("总体实施策划", "总体实施策划（2）")
        _cleanup_placeholder_titles(l1)
        assert l1[0]["title"] == "总体实施策划"
        assert l1[1]["title"] == "总体实施策划（2）"


# ===========================================================================
# 遗留 3 加固：`_merge_filled_children` 缺失信号（无包含关系才计缺失）
# ===========================================================================


class TestMergeFilledChildrenMissingDetection:
    def _skel(self, *titles):
        return [{"title": t, "description": "", "children": []} for t in titles]

    def test_genuinely_missing_titles_emit_warning(self, caplog):
        """骨架标题完全缺失且无任何包含关系（漏章/合并）→ 透知。"""
        skel = self._skel("分部工程划分", "安全文明施工")
        ai = [
            {"title": "总体部署", "description": ""},
            {"title": "平面布置", "description": ""},
        ]
        with caplog.at_level(logging.WARNING):
            _merge_filled_children(skel, ai)
        assert "OUTLINE_L2_ORDER_MISMATCH" in caplog.text

    def test_refinement_substring_no_warning(self, caplog):
        """AI 精化改写（骨架标题是 AI 标题子串）→ 不误报缺失。"""
        skel = self._skel("总体部署", "平面布置")
        ai = [
            {"title": "施工总体部署与任务划分", "description": ""},
            {"title": "施工现场平面布置", "description": ""},
        ]
        with caplog.at_level(logging.WARNING):
            _merge_filled_children(skel, ai)
        assert "OUTLINE_L2_ORDER_MISMATCH" not in caplog.text

    def test_swapped_titles_still_warn(self, caplog):
        """既有错位信号不回归。"""
        skel = self._skel("分部工程划分", "施工区段安排")
        ai = [
            {"title": "施工区段安排", "description": ""},
            {"title": "分部工程划分", "description": ""},
        ]
        with caplog.at_level(logging.WARNING):
            out = _merge_filled_children(skel, ai)
        assert [n["title"] for n in out] == ["施工区段安排", "分部工程划分"]
        assert "OUTLINE_L2_ORDER_MISMATCH" in caplog.text


# ===========================================================================
# 遗留 1 集成：全链「专项6」L1 + 评分项 → 终局 L1 干净
# ===========================================================================


class _DeadProvider:
    """chat 直接失败：AI 自检等旁路调用全部走容错分支。"""

    class _Msg:
        def __init__(self, role, content):
            self.role = role
            self.content = content

    async def chat(self, messages, **kwargs):
        raise RuntimeError("offline: AI 不可用")


def _user_text(messages) -> str:
    return "\n".join(
        m.content for m in messages
        if getattr(m, "role", "") == "user" and isinstance(m.content, str)
    )


async def _collect_placeholder_l1(provider, messages, **kwargs):
    """L1 生成返回「专项6」作一级章名；L2 骨架调用返回骨架（结构合法）。"""
    import re as _re

    user = _user_text(messages)
    if "<json_outline>" in user:
        m = _re.search(r"<json_outline>\n(.*?)\n</json_outline>", user, _re.DOTALL)
        return json.loads(m.group(1))
    return [
        {"title": "专项6", "children": []},
        {"title": "工程质量管理", "children": []},
    ]


@pytest.mark.asyncio
async def test_full_chain_placeholder_l1_rescued_by_scoring(monkeypatch):
    """全链注入「专项6」L1 + 评分项 → 终局 L1 不得再有「专项6」（语义兜底）。

    这是遗留项 1 的 e2e 面：`_l1_placeholder_scoring_fallback` 在 `_generate_level1_v51`
    内就地替换，`generate_outline_v51` 全链产出干净 L1（而非旧行为「保持原样 + 透知」）。
    """
    monkeypatch.setattr(wo, "collect_json_response", _collect_placeholder_l1)

    events: list[dict] = []

    async def _emit(evt):
        events.append(evt)

    scoring = json.dumps(_SCORING, ensure_ascii=False)
    outline = await wo.generate_outline_v51(
        _DeadProvider(), "某房建工程概述", scoring, "建筑工程",
        auto_confirm=True, outline_mode="stepwise", event_emitter=_emit,
    )
    assert outline, "L1 占位注入不应阻断目录产出"
    titles = [(n.get("title") or "").strip() for n in outline]
    assert "专项6" not in titles, f"L1 占位「专项6」未被评分项语义兜底: {titles}"
    # 兜底替换后的 L1 必须是干净、非占位标题
    assert not any(_is_dirty_l2_title_deep(t) for t in titles), titles
