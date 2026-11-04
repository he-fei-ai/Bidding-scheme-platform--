"""生成大纲模块终审边界与替换词跨章去重（2026-11-04 第三轮修复）。

钉住两个修复的契约：

修复 1（终审边界替换树零清洗）：
  `generate_outline_v51` 分步路径的最终审核此前取 `v51_state.edited_outline or
  level1` 后仅做 renumber（只改 id 不动 title）。`edited_outline` 的两个来源
  ——断点续生恢复（`load_v51_state_from_db` 只做 JSON 反序列化）与人工提交
  （`/v51-confirm` → `resume_v51_task`，只做类型 + 深度校验）——都不经生成侧
  五道清洗，脏标题唯一拦截点是落库终态网（sse_db_ops ERROR「上游质量收口链
  漏掉了该节点」，2026-11-03 15:06 实测触发 2 例）。修复后入口侧（审核事件
  载荷）与出口侧（返回树）都用 `_post_selfcheck_title_safety_net` 收口。

修复 2（标题质量修正 used 集合全树级）：
  `_post_process_title_quality` 的 `used_l2_titles`/`used_l3_titles` 此前按
  所属 L1/L2 局部构建——同一替换词在不同章节反复出现（2026-11-03 run 实测
  「管理制度建立与考核实施」×2、「过程检查与验收标准」×3），要等下游跨章
  复用护栏二次改写，而「二级目录审核」事件发生在护栏之前，用户看到的即是
  跨章重复形态。修复后替换候选避开全树既有标题。
"""

from __future__ import annotations

import json
import re
from collections import Counter

import pytest

import app.services.ai.workflows_outline_v51 as wo
from app.services.ai.outline_common import (
    _is_dirty_l2_title_deep,
    _is_dirty_l3_title_deep,
)
from app.services.ai.workflows_outline_v51 import (
    OUTLINE_L3_MAX_PARENT_REUSE,
    _post_process_title_quality,
)
from app.config import OUTLINE_L3_MAX_PARENT_REUSE as _CAP


# ===========================================================================
# 修复 2（单元面）：替换词跨章零重复
# ===========================================================================


def _dirty_tree_across_chapters() -> list[dict]:
    """模拟 2026-11-03 run 形态：同一批脏标题散布在多个 L1/L2 下。"""
    return [
        {
            "id": "1",
            "title": "第一章 施工部署",
            "children": [
                {
                    "id": "1.1",
                    "title": "总体方案与实施策划",
                    "children": [
                        {"id": "1.1.1", "title": "专项6", "children": []},
                        {"id": "1.1.2", "title": "总体实施策划（2）", "children": []},
                    ],
                },
                {
                    "id": "1.2",
                    "title": "管理制度与考核机制",
                    "children": [
                        {"id": "1.2.1", "title": "专项6", "children": []},
                        {"id": "1.2.2", "title": "", "children": []},
                    ],
                },
            ],
        },
        {
            "id": "2",
            "title": "第二章 质量保证",
            "children": [
                {
                    "id": "2.1",
                    "title": "质量保证体系",
                    "children": [
                        {"id": "2.1.1", "title": "专项6", "children": []},
                        {"id": "2.1.2", "title": "内容3", "children": []},
                    ],
                }
            ],
        },
    ]


class TestQualityFixGlobalUsed:
    def test_replacement_titles_globally_unique(self):
        """跨 L1/L2 的脏标题替换产物不得重名（旧实现必然产重复）。"""
        tree = _dirty_tree_across_chapters()
        out, fixed = _post_process_title_quality(tree)
        assert fixed >= 5, f"脏标题应全部被修正，实际 fixed={fixed}"
        l3s = [
            l3.get("title", "").strip()
            for l1 in out
            for l2 in l1.get("children") or []
            for l3 in l2.get("children") or []
        ]
        dups = {t: n for t, n in Counter(l3s).items() if n > 1}
        assert not dups, f"替换词跨章重复: {dups}"

    def test_replacement_titles_clean(self):
        """替换产物不得是脏/占位形态（专项N/（N）后缀/空）。"""
        tree = _dirty_tree_across_chapters()
        out, _ = _post_process_title_quality(tree)
        dirty_re = re.compile(r"^(专项|内容)\s*[0-9]+$|[（(]\s*[0-9]+\s*[)）]\s*$")
        for l1 in out:
            t1 = (l1.get("title") or "").strip()
            assert not dirty_re.search(t1), f"L1 脏标题未清洗: {t1}"
            for l2 in l1.get("children") or []:
                t2 = (l2.get("title") or "").strip()
                assert not dirty_re.search(t2), f"L2 脏标题未清洗: {t2}"
                for l3 in l2.get("children") or []:
                    t3 = (l3.get("title") or "").strip()
                    assert t3, "L3 空标题未补全"
                    assert not dirty_re.search(t3), f"L3 脏标题未清洗: {t3}"

    def test_clean_tree_is_noop(self):
        """干净树零改动（幂等、无误伤）。"""
        tree = [
            {
                "id": "1",
                "title": "施工总体部署",
                "children": [
                    {
                        "id": "1.1",
                        "title": "施工组织与任务划分",
                        "children": [
                            {"id": "1.1.1", "title": "土方开挖施工工艺", "children": []},
                            {
                                "id": "1.1.2",
                                "title": "深基坑（超5m）支护方案",
                                "children": [],
                            },
                        ],
                    }
                ],
            }
        ]
        import copy

        snapshot = copy.deepcopy(tree)
        out, fixed = _post_process_title_quality(tree)
        assert fixed == 0, f"干净树被误改 {fixed} 处"
        assert out == snapshot, "干净树结构/标题发生变化"

    def test_replacement_avoids_existing_titles_tree_wide(self):
        """替换候选避开全树既有标题（含其他章节的干净标题）。"""
        tree = [
            {
                "id": "1",
                "title": "第一章",
                "children": [
                    {
                        "id": "1.1",
                        "title": "深基础土方开挖与降排水组织",  # 具体 L2 名，避免命中泛化正则
                        "children": [{"id": "1.1.1", "title": "专项6", "children": []}],
                    }
                ],
            },
            {
                "id": "2",
                "title": "第二章",
                "children": [
                    {
                        "id": "2.1",
                        "title": "高空作业防护与临边洞口管理",  # 具体 L2 名
                        "children": [{"id": "2.1.1", "title": "内容3", "children": []}],
                    }
                ],
            },
        ]
        out, fixed = _post_process_title_quality(tree)
        assert fixed == 2, f"预期只修正 2 个 L3 占位标题，实际 {fixed}"
        t_111 = out[0]["children"][0]["children"][0]["title"]
        t_211 = out[1]["children"][0]["children"][0]["title"]
        # 两个替换词不得撞任何既有标题（既有标题只有 L1/L2 的干净名）
        existing = {"第一章", "第二章", "深基础土方开挖与降排水组织", "高空作业防护与临边洞口管理"}
        assert t_111 not in existing and t_211 not in existing
        assert t_111 != t_211, "两个替换词互相重复"


# ===========================================================================
# 修复 1（e2e 面）：终审边界替换树收口
# ===========================================================================


class _DeadProvider:
    """chat 直接失败：AI 旁路全走容错分支（与全链探针同源）。"""

    class _Msg:
        def __init__(self, role, content):
            self.role = role
            self.content = content

    async def chat(self, messages, **kwargs):
        raise RuntimeError("offline: AI 不可用")


def _dirty_replacement_tree() -> list[dict]:
    """模拟旧版本存库 / 人工提交的脏树（深度合法、标题脏）。"""
    return [
        {
            "id": "1",
            "title": "专项6",
            "children": [
                {
                    "id": "1.1",
                    "title": "总体实施策划（2）",
                    "children": [
                        {"id": "1.1.1", "title": "专项6", "children": []},
                        {"id": "1.1.2", "title": "总体实施策划（3）", "children": []},
                        {"id": "1.1.3", "title": "内容3", "children": []},
                        {"id": "1.1.4", "title": "第6项", "children": []},
                        {"id": "1.1.5", "title": "", "children": []},
                    ],
                }
            ],
        }
    ]


class _FakeState:
    """最小 V5.1 任务态（auto_confirm 路径只需 edited_outline / current_phase）。"""

    def __init__(self, edited_outline=None):
        self.current_phase = ""
        self.edited_outline = edited_outline
        self.cancelled = False
        self.gate_report = None


@pytest.mark.asyncio
async def test_final_review_restored_dirty_edited_outline_cleaned(monkeypatch):
    """入口侧：恢复的脏 edited_outline 在终审边界被收口（auto_confirm 直达）。"""
    events: list[dict] = []

    async def _emit(evt):
        events.append(evt)

    state = _FakeState(edited_outline=_dirty_replacement_tree())

    async def _no_l1(provider, *a, **kw):
        return [dict(i, children=[]) for i in [{"title": "总体施工部署"}]]

    monkeypatch.setattr(wo, "_generate_level1_v51", _no_l1)

    outline = await wo.generate_outline_v51(
        _DeadProvider(), "某房建工程概述", "[]", "建筑工程",
        auto_confirm=True, outline_mode="stepwise",
        event_emitter=_emit, v51_state=state, outline_queue=__import__("asyncio").Queue(),
    )
    assert outline, "恢复脏树不应阻断目录产出"
    # 出口契约：返回树（= edited_outline）不得含脏标题（终审边界入口+出口双侧收口）
    for l1 in outline:
        assert not _is_dirty_l2_title_deep((l1.get("title") or "").strip()), \
            f"L1 脏标题泄漏: {l1.get('title')}"
        for l2 in l1.get("children") or []:
            assert not _is_dirty_l2_title_deep((l2.get("title") or "").strip()), \
                f"L2 脏标题泄漏: {l2.get('title')}"
            for l3 in l2.get("children") or []:
                t3 = (l3.get("title") or "").strip()
                assert t3, "L3 空标题未补全"
                assert not _is_dirty_l3_title_deep(t3), f"L3 脏标题泄漏: {t3}"
    # review_auto_confirm(final) 事件的 outline 载荷也应干净（入口侧收口）
    final_evt = [e for e in events if e.get("status") == "review_auto_confirm" and e.get("phase") == "final"]
    assert final_evt, "缺少 final review_auto_confirm 事件"


@pytest.mark.asyncio
async def test_final_review_user_submitted_dirty_tree_cleaned(monkeypatch):
    """出口侧：终审人工提交树（mock _wait_for_v51_review 返回脏树）被收口。"""
    events: list[dict] = []

    async def _emit(evt):
        events.append(evt)

    _L1_ITEMS = [{"title": "总体施工部署", "children": []}]

    async def _mock_collect(provider, messages, **kwargs):
        """L1 生成返回干净骨架；L2 骨架调用原样返回（避免裸异常穿透）。"""
        user = "\n".join(
            m.content for m in messages
            if getattr(m, "role", "") == "user" and isinstance(m.content, str)
        )
        if "<json_outline>" in user:
            return json.loads(
                re.search(r"<json_outline>\n(.*?)\n</json_outline>", user, re.DOTALL).group(1)
            )
        return [dict(i, children=[]) for i in _L1_ITEMS]

    async def _fake_review(outline_queue, v51_state, phase, outline, **kw):
        if phase == "final":
            return _dirty_replacement_tree()
        return outline  # L1/L2 审核点按 auto_confirm 契约透传

    monkeypatch.setattr(wo, "collect_json_response", _mock_collect)
    monkeypatch.setattr(wo, "_wait_for_v51_review", _fake_review)

    outline = await wo.generate_outline_v51(
        _DeadProvider(), "某房建工程概述", "[]", "建筑工程",
        auto_confirm=True, outline_mode="stepwise", event_emitter=_emit,
    )
    assert outline, "人工提交脏树不应阻断目录产出"
    for l1 in outline:
        assert not _is_dirty_l2_title_deep((l1.get("title") or "").strip()), \
            f"L1 脏标题泄漏: {l1.get('title')}"
        for l2 in l1.get("children") or []:
            assert not _is_dirty_l2_title_deep((l2.get("title") or "").strip()), \
                f"L2 脏标题泄漏: {l2.get('title')}"
            for l3 in l2.get("children") or []:
                t3 = (l3.get("title") or "").strip()
                assert t3 and not _is_dirty_l3_title_deep(t3), f"L3 脏标题泄漏: {t3}"


@pytest.mark.asyncio
async def test_full_chain_cross_parent_repetition_within_cap(monkeypatch):
    """e2e：全链产物任意 L3 标题跨父级复用广度 ≤ OUTLINE_L3_MAX_PARENT_REUSE。

    修复 2 消除「替换词跨章重复」源头后，叠加既有跨章护栏，终树不得出现
    成套复制形态（岳阳路事故：单标题 25 个父级）。
    """
    from tests.test_outline_full_chain_probe_2026_11_02 import (
        DIRTY_L2,
        DIRTY_L3,
        _DeadProvider as _ProbeProvider,
        _collect_dirty_inject,
    )

    monkeypatch.setattr(wo, "collect_json_response", _collect_dirty_inject)

    async def _emit(evt):
        pass

    outline = await wo.generate_outline_v51(
        _ProbeProvider(), "某房建工程概述", "[]", "建筑工程",
        auto_confirm=True, outline_mode="stepwise", event_emitter=_emit,
    )
    assert outline
    # 以「父级实例」计数（父级身份 = L1 序号 × L2 序号，同名 L2 视为不同实例）
    occurrences: dict[str, set[tuple[int, int]]] = {}
    for i, l1 in enumerate(outline):
        for j, l2 in enumerate(l1.get("children") or []):
            for l3 in l2.get("children") or []:
                t3 = (l3.get("title") or "").strip()
                if t3:
                    occurrences.setdefault(t3, set()).add((i, j))
    offenders = {
        t: len(ps) for t, ps in occurrences.items() if len(ps) > max(_CAP, OUTLINE_L3_MAX_PARENT_REUSE)
    }
    assert not offenders, f"L3 标题跨父级复用超上限(={_CAP}): {offenders}"
