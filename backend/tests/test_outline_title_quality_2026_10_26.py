"""大纲标题质量 · L2 编号占位 + 空标题/配额泄漏修复（2026-10-26）。

覆盖用户复报的目录问题与本次探查实证缺陷：

1. 「专项6」「总体实施策划（2）」这类编号式占位标题：此前只在 level>=3 被清洗，
   **二级标题**（AI 常产出「专项方案2」「内容3」「第6项」）完全穿过
   `_post_process_title_quality`，在「二级目录审核」界面与落库 JSON 中原样展示。
2. 空标题被修复配额挤掉：`repair_l3_generic_titles` 的历史口径对空标题
   `continue` 跳过，配额口径下也会被挤出本轮；而 `normalize_outline_items`
   落库时会**删除**空标题节点 → 目录缺节。
3. 落库前零脏标题硬保证：`_clear_and_save_outline` 的每道安全网都是
   try/except 吞异常，异常时脏标题可能原样落库；现补一次不依赖前序成功的
   终态确定性改写，并保证空标题节点不因归一化而消失。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import app
from app.routers.sse.sse_db_ops import (
    _clear_and_save_outline,
    _fill_or_fix_outline_titles,
)
from app.services.ai.outline_common import (
    _is_dirty_l3_title_deep,
    _is_placeholder_numbered_title,
)
from app.services.ai.workflows_outline_utils import (
    repair_l3_generic_titles,
    validate_l3_specificity,
)
from app.services.ai.workflows_outline_v51 import _post_process_title_quality


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def project_id(client):
    resp = client.post(
        "/api/v1/projects",
        json={"name": "目录标题质量回归", "industry": "公共建筑", "bidder_name": "测试投标方"},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _dirty_l2_tree() -> list[dict]:
    """二级为编号占位、三级混入脏/空标题的目录。"""
    l2_names = ["专项方案1", "内容3", "第6项", "总体实施策划（2）", "基坑支护专项方案"]
    # 同一父级内不得出现「核心名相同」的标题（如（2）/（3）），否则会触发
    # 同父级真·重复去重（这是既有合法行为），导致节点数不守恒、干扰本用例断言。
    l3_names = ["专项6", "总体实施策划（2）", "", "内容3", "桩基技术交底", "基坑监测方案"]
    tree = []
    for ci in range(2):
        l1 = {"title": f"第{ci + 1}章 {'施工组织设计' if ci == 0 else '质量保证措施'}", "children": []}
        for li, name in enumerate(l2_names):
            l2 = {"title": name, "children": []}
            for k in range(6):
                l2["children"].append({"title": l3_names[(li + k) % len(l3_names)], "children": []})
            l1["children"].append(l2)
        tree.append(l1)
    return tree


class TestL2PlaceholderCleanedBeforeReview:
    """二级审核是用户第一次看到二三级目录的界面，编号占位必须提前清掉。"""

    def test_l2_and_l3_numbered_placeholders_are_cleaned(self):
        tree = _dirty_l2_tree()
        tree, fixed = _post_process_title_quality(tree)
        assert fixed > 0

        l2_titles = [
            (l2.get("title") or "").strip()
            for l1 in tree
            for l2 in (l1.get("children") or [])
        ]
        assert l2_titles, "目录树结构不应被破坏"
        assert [t for t in l2_titles if _is_placeholder_numbered_title(t)] == []

        l3_titles = [
            (l3.get("title") or "").strip()
            for l1 in tree
            for l2 in (l1.get("children") or [])
            for l3 in (l2.get("children") or [])
        ]
        assert [t for t in l3_titles if not t or _is_dirty_l3_title_deep(t)] == []

    def test_legit_l1_l2_words_not_over_filtered(self):
        """「其他/概述」这类在 L1/L2 合法的词不得被编号占位判定误伤。"""
        assert _is_placeholder_numbered_title("其他") is False
        assert _is_placeholder_numbered_title("概述") is False
        assert _is_placeholder_numbered_title("工程质量保证措施") is False
        # 带裸编号/序数的形态才判占位
        assert _is_placeholder_numbered_title("专项方案2") is True
        assert _is_placeholder_numbered_title("总体实施策划（2）") is True
        assert _is_placeholder_numbered_title("（3）施工部署") is True
        assert _is_placeholder_numbered_title("第6项") is True


class TestRepairEmptyTitleQuotaExemption:
    """空标题零 AI 成本，必须无条件修复（否则落库丢节点）。"""

    @pytest.mark.asyncio
    async def test_empty_titles_fixed_beyond_repair_quota(self):
        tree = [
            {
                "title": "第一章 施工组织设计",
                "children": [
                    {
                        "title": "基坑支护专项方案",
                        "children": [{"title": "", "children": []} for _ in range(8)],
                    }
                ],
            }
        ]
        # 配额只给 2 条，远小于 8 个空标题
        out = await repair_l3_generic_titles(
            None, tree, "", "", "", "", "", max_repair=2, use_ai=False
        )
        empties = [n for n in out[0]["children"][0]["children"] if not (n.get("title") or "").strip()]
        assert empties == []
        passed, warnings = validate_l3_specificity(out)
        assert passed is True and warnings == []

        # 节点数量必须守恒（空标题不能因修复而丢失）
        assert len(out[0]["children"][0]["children"]) == 8


class TestSaveTimeHardGuarantee:
    """落库唯一入口的终态校验：脏标题/空标题绝不进 DB。"""

    def test_fill_helper_fills_empty_then_fixes_dirty(self):
        tree = [
            {
                "title": "第一章 施工组织设计",
                "children": [
                    {
                        "title": "基坑支护专项方案",
                        "children": [
                            {"title": "专项6", "children": []},
                            {"title": "总体实施策划（2）", "children": []},
                            {"title": "", "children": []},
                            {"title": "桩基技术交底", "children": []},
                        ],
                    }
                ],
            }
        ]
        # 第一步（归一化前）：只补空标题，脏标题保持（后续清洗链还有机会语义化）
        assert _fill_or_fix_outline_titles(tree, fix_dirty=False) == 1
        assert [c["title"] for c in tree[0]["children"][0]["children"]] == [
            "专项6", "总体实施策划（2）", "机电管线安全防护", "桩基技术交底",
        ]
        # 第二步（全部安全网之后）：强制改写脏标题
        assert _fill_or_fix_outline_titles(tree, fix_dirty=True) == 2
        titles = [c["title"] for c in tree[0]["children"][0]["children"]]
        assert [t for t in titles if _is_dirty_l3_title_deep(t)] == []
        assert [t for t in titles if not t.strip()] == []
        assert len(set(titles)) == len(titles)

    def test_saved_rows_have_no_dirty_or_empty_titles(self, project_id):
        _clear_and_save_outline(project_id, _dirty_l2_tree())
        with get_db(readonly=True) as conn:
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT id, parent_id, level, sort_order, title FROM sections "
                    "WHERE project_id = ? ORDER BY level, parent_id, sort_order",
                    (project_id,),
                ).fetchall()
            ]
        assert rows, "目录必须落库"
        l3_rows = [r for r in rows if r["level"] >= 3]
        assert l3_rows, "三级节点必须全部落库（空标题不得被归一化删除）"
        for r in l3_rows:
            title = (r["title"] or "").strip()
            assert title, f"三级节点标题不得为空（id={r['id']}）"
            assert not _is_dirty_l3_title_deep(title), f"三级节点标题不得为占位/泛化：{title}"
        # 空标题节点数量守恒：2 章 × 5 个二级 × 6 个三级
        assert len(l3_rows) == 2 * 5 * 6
