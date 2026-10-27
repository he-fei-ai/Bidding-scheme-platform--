"""目录生成落库冒烟门禁（2026-10-26）。

把「落库前零脏/零空标题 + 节点数守恒 + 层级编号完整」固化为快速门禁：

    pytest -m smoke -q

门禁直接复用生产唯一落库入口 ``_clear_and_save_outline``（不造 INSERT），
因此能同时覆盖：标题质量收口、空标题兜底、归一化去重、层级自检、编号落库。

失败含义：
  - 出现脏/空三级标题 → 标题质量链有漏点（会导致目录出现「专项6」式占位名）；
  - 节点数不守恒        → 某处静默删除节点（章节丢失）；
  - 路径编号不符预期    → 编号命名空间漂移（前端/导出将显示错号）。
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import app
from app.routers.sse.sse_db_ops import _clear_and_save_outline, _save_section_to_db
from app.services import sections as sections_service
from app.services.ai.outline_common import _is_dirty_l3_title_deep

pytestmark = pytest.mark.smoke

_L1_COUNT = 2
_L2_PER_L1 = 3
_L3_PER_L2 = 6
# 同父级内必须「核心名互不相同」：既包含脏/空标题，也避免触发同父级
# 真·重复去重（那是既有合法行为，会让节点数守恒断言失真）。
_L3_TEMPLATES = ["专项6", "总体实施策划（2）", "", "内容3", "桩基技术交底", "基坑监测方案"]


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def project_id(client):
    resp = client.post(
        "/api/v1/projects",
        json={"name": "目录冒烟门禁", "industry": "公共建筑", "bidder_name": "测试投标方"},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _dirty_outline() -> list[dict]:
    l1_titles = ["第1章 施工组织设计", "第2章 质量保证措施"]
    # 二级标题用干净名称：L2 质量由上游 _post_process_title_quality 保证，
    # 本门禁只锁定「落库前三级零脏零空 + 节点守恒」，不重复断言 L2 链路。
    l2_titles = ["总体实施策划", "专项施工方案", "基坑支护方案"]
    outline: list[dict] = []
    for li in range(_L1_COUNT):
        l2_nodes = []
        for l2i in range(_L2_PER_L1):
            l3_nodes = [
                {
                    "title": _L3_TEMPLATES[(l2i + k) % len(_L3_TEMPLATES)],
                    "children": [],
                }
                for k in range(_L3_PER_L2)
            ]
            l2_nodes.append({"title": l2_titles[l2i], "children": l3_nodes})
        outline.append({"title": l1_titles[li], "children": l2_nodes})
    return outline


def _rows(project_id: int) -> list[dict]:
    with get_db(readonly=True) as conn:
        rows = conn.execute(
            "SELECT id, parent_id, title, sort_order, level FROM sections "
            "WHERE project_id = ? ORDER BY sort_order, id",
            (project_id,),
        ).fetchall()
    return [dict(r) for r in rows]


class TestOutlineSaveSmokeGate:
    def test_zero_dirty_zero_empty_and_node_conservation(self, project_id):
        _clear_and_save_outline(project_id, _dirty_outline())

        rows = _rows(project_id)
        l1_rows = [r for r in rows if r["parent_id"] is None]
        l2_rows = [r for r in rows if r["parent_id"] is not None and r["level"] == 2]
        l3_rows = [r for r in rows if r["level"] == 3]

        # 层级完整：2 / 6 / 36
        assert len(l1_rows) == _L1_COUNT
        assert len(l2_rows) == _L1_COUNT * _L2_PER_L1
        assert len(l3_rows) == _L1_COUNT * _L2_PER_L1 * _L3_PER_L2

        # 零空 / 零脏（深层判定含「（其N）」二次剥离，防漏网）
        bad = [r["title"] for r in l3_rows if not r["title"].strip()
               or _is_dirty_l3_title_deep(r["title"])]
        assert not bad, f"冒烟门禁失败：残留脏/空三级标题 {bad[:10]}"

        # 编号正确率：每个三级节点的路径号 = 父路径 + 本级序号
        for r in l3_rows:
            path = sections_service.get_section_path(r["id"])
            parts = path.split(".")
            assert len(parts) == 3 and all(p.isdigit() and int(p) >= 1 for p in parts), path

    def test_tree_is_three_levels_and_leaves_are_l3(self, project_id):
        _clear_and_save_outline(project_id, _dirty_outline())
        tree = sections_service.build_tree(project_id)

        assert len(tree) == _L1_COUNT
        for l1 in tree:
            assert l1.children, "一级章节不得空置"
            for l2 in l1.children:
                assert l2.children, "二级章节不得空置"
                for l3 in l2.children:
                    assert not l3.children, "三级节点应为叶子"

    def test_placeholder_suffix_absent_across_all_levels(self, project_id):
        _clear_and_save_outline(project_id, _dirty_outline())
        rows = _rows(project_id)

        offenders = [
            r["title"]
            for r in rows
            if re.search(r"专项\s*\d+$", r["title"])
            or re.search(r"（\d+）$", r["title"])
            or re.search(r"第\s*\d+\s*项$", r["title"])
        ]
        assert not offenders, f"冒烟门禁失败：残留编号式占位标题 {offenders[:10]}"

    def test_saved_body_headings_clean_and_anchored(self, project_id):
        """正文三级标题落库后：零占位泛化名、零编号冲突后缀、编号锚定章节路径。

        2026-10-27 补：此前门禁只覆盖目录标题，正文标题文本的占位名与
        「（2）」后缀会漏出（编号已锚定但文本未净化），故纳入同一道门禁。
        """
        _clear_and_save_outline(project_id, _dirty_outline())
        with get_db(readonly=True) as conn:
            row = conn.execute(
                "SELECT id FROM sections WHERE project_id = ? AND level = 3 "
                "ORDER BY id LIMIT 1",
                (project_id,),
            ).fetchone()
        assert row, "目录落库后应存在三级章节"
        section_id = row["id"]
        path = sections_service.get_section_path(section_id)

        _save_section_to_db(
            section_id,
            "completed",
            "正文首段\n"
            "### 2.9.4 专项6\n"
            "### 2.9.5 钢筋绑扎验收（2）\n"
            "### 2.9.6 总体实施策划（2）",
            project_id=project_id,
        )
        with get_db(readonly=True) as conn:
            content = conn.execute(
                "SELECT content FROM sections WHERE id = ?", (section_id,)
            ).fetchone()["content"]

        headings = re.findall(r"^(\d+(?:\.\d+)+) (.+)$", content, re.MULTILINE)
        assert len(headings) == 3, content
        for num, body in headings:
            assert not _is_dirty_l3_title_deep(body), f"正文残留占位标题：{num} {body}"
            assert num.startswith(path + "."), f"正文编号未锚定章节路径 {path}：{num}"
            assert not re.search(r"（\d+）$", body), f"正文残留编号冲突后缀：{body}"
