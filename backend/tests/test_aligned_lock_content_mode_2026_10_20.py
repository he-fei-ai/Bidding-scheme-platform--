"""Aligned 锁定与正文模式落库加固（2026-10-20 遗留 3/5）。

覆盖两条遗留的闭环：

- 遗留 3：aligned 模式程序构造的一级目录（locked=True）此前只存在于内存树，
  页面刷新/跨请求正文生成即丢失。本轮把 locked / content_mode 落库到 sections，
  build_tree / 正文叶子字典透出，服务层拒绝对锁定章节做标题/父级/排序修改与
  移动/删除（正文、状态、字数预算编辑不受影响）。
- 遗留 5：content_mode 从「结构落点」升级为「可消费契约」——落库、随正文叶子
  字典下发，正文生成入口 resolve_content_mode 统一解析；暂无实现的模式
  （template-fill/point-to-point）显式告警并回落 ai-generate。

钉子口径：直接复用生产写入路径 `_clear_and_save_outline`（目录生成落库唯一
入口），避免测试自造 INSERT 与真实链路漂移。
"""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import app
from app.models import SectionCreate, SectionUpdate
from app.routers.sse.sse_db_ops import _clear_and_save_outline
from app.routers.sse.sse_utils import _build_leaf_dicts
from app.services import sections as sections_service
from app.services.ai import outline_alignment as oa


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def project_id(client):
    resp = client.post(
        "/api/v1/projects",
        json={"name": "Aligned锁定落库回归", "industry": "公共建筑", "bidder_name": "测试投标方"},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _aligned_outline() -> list[dict]:
    """与 aligned 生产链路同构的目录：L1 锁定 + content_mode，子级不锁定。"""
    return [
        {
            "title": "施工组织设计",
            "locked": True,
            "content_mode": "ai-generate",
            "source_requirement_id": "R1",
            "children": [{"title": "编制依据", "children": []}],
        },
        {"title": "质量保证措施", "locked": True, "content_mode": "ai-generate",
         "source_requirement_id": "R2", "children": []},
    ]


def _rows_by_title(pid: int) -> dict[str, dict]:
    with get_db(readonly=True) as conn:
        rows = conn.execute(
            "SELECT * FROM sections WHERE project_id = ?", (pid,)
        ).fetchall()
    return {r["title"]: dict(r) for r in rows}


# ---------------------------------------------------------------------------
# 落库与读取
# ---------------------------------------------------------------------------


class TestLockPersistence:
    def test_locked_and_content_mode_persisted(self, project_id):
        _clear_and_save_outline(project_id, _aligned_outline())
        rows = _rows_by_title(project_id)

        assert rows["施工组织设计"]["locked"] == 1
        assert rows["施工组织设计"]["content_mode"] == "ai-generate"
        # 零迁移回落列仍在：source_requirement_id → scoring_point_id
        assert rows["施工组织设计"]["scoring_point_id"] == "R1"
        # 子章节未被锁定（锁定只属于程序构造的一级目录）
        assert rows["编制依据"]["locked"] == 0
        assert rows["编制依据"]["content_mode"] is None
        assert rows["质量保证措施"]["locked"] == 1

    def test_build_tree_exposes_locked_and_content_mode(self, project_id):
        _clear_and_save_outline(project_id, _aligned_outline())
        tree = sections_service.build_tree(project_id)

        root = tree[0]
        assert root.locked is True
        assert root.content_mode == "ai-generate"
        assert root.children[0].locked is False
        assert tree[1].locked is True

    def test_leaf_dicts_carry_content_mode(self, project_id):
        _clear_and_save_outline(project_id, _aligned_outline())
        with get_db(readonly=True) as conn:
            all_rows = [dict(r) for r in conn.execute(
                "SELECT * FROM sections WHERE project_id = ?", (project_id,)
            ).fetchall()]
        sections_by_id = {r["id"]: r for r in all_rows}
        children_map: dict = {}
        for r in all_rows:
            children_map.setdefault(r["parent_id"], []).append(r)

        leaves = _build_leaf_dicts(project_id, sections_by_id, children_map)
        assert leaves, "未构建出叶子章节"
        assert all(leaf["content_mode"] == "ai-generate" for leaf in leaves)


# ---------------------------------------------------------------------------
# 结构保护（服务层 + HTTP 层）
# ---------------------------------------------------------------------------


class TestLockedStructureGuard:
    def test_update_section_rejects_structure_allows_content(self, project_id):
        saved = _clear_and_save_outline(project_id, _aligned_outline())
        root_id = saved[0]["id"]

        with pytest.raises(ValueError, match="锁定"):
            sections_service.update_section(root_id, SectionUpdate(title="改名"))
        with pytest.raises(ValueError, match="锁定"):
            sections_service.update_section(root_id, SectionUpdate(sort_order=9))

        updated = sections_service.update_section(
            root_id, SectionUpdate(content="锁定章节正文仍可编辑", status="completed")
        )
        assert updated is not None
        assert updated.content == "锁定章节正文仍可编辑"
        assert updated.locked is True

    def test_move_and_delete_rejected_for_locked(self, project_id):
        saved = _clear_and_save_outline(project_id, _aligned_outline())
        root_id = saved[0]["id"]

        with pytest.raises(ValueError, match="锁定"):
            sections_service.move_section(root_id, "down")
        with pytest.raises(ValueError, match="锁定"):
            sections_service.delete_section_tree(root_id)

        # 锁定章节的子目录仍可删除（锁定只保护 L1 自身结构）
        child_id = saved[0]["children"][0]["id"]
        result = sections_service.delete_section_tree(child_id)
        assert result["deleted_count"] == 1

    def test_http_routes_return_400_for_locked(self, client, project_id):
        saved = _clear_and_save_outline(project_id, _aligned_outline())
        root_id = saved[0]["id"]

        r = client.patch(
            f"/api/v1/projects/{project_id}/sections/{root_id}", json={"title": "改名"}
        )
        assert r.status_code == 400 and "锁定" in r.text

        r = client.post(
            f"/api/v1/projects/{project_id}/sections/{root_id}/move?direction=down"
        )
        assert r.status_code == 400 and "锁定" in r.text

        r = client.delete(f"/api/v1/projects/{project_id}/sections/{root_id}")
        assert r.status_code == 400 and "锁定" in r.text

    def test_unlocked_section_keeps_legacy_behavior(self, project_id):
        """未锁定章节（free 模式/手工章节）历史行为零变化。"""
        _clear_and_save_outline(project_id, _aligned_outline())
        rows = _rows_by_title(project_id)
        normal = sections_service.create_section(
            project_id, SectionCreate(title="手工章节")
        )
        assert normal.locked is False
        renamed = sections_service.update_section(normal.id, SectionUpdate(title="手工章节改名"))
        assert renamed is not None and renamed.title == "手工章节改名"
        assert rows["质量保证措施"]["locked"] == 1  # 既有锁定不受影响


# ---------------------------------------------------------------------------
# content_mode 契约（遗留 5）
# ---------------------------------------------------------------------------


class TestContentModeContract:
    def test_normalize_content_mode(self):
        assert oa.normalize_content_mode(None) == oa.CONTENT_MODE_AI_GENERATE
        assert oa.normalize_content_mode("") == oa.CONTENT_MODE_AI_GENERATE
        assert oa.normalize_content_mode("AI") == oa.CONTENT_MODE_AI_GENERATE
        assert oa.normalize_content_mode("template_fill") == oa.CONTENT_MODE_TEMPLATE_FILL
        # 未知值回落历史默认，避免脏值进入生成链路
        assert oa.normalize_content_mode("garbage") == oa.CONTENT_MODE_AI_GENERATE

    def test_resolve_unimplemented_mode_warns_and_falls_back(self, caplog):
        with caplog.at_level(logging.WARNING):
            mode = oa.resolve_content_mode(
                {"content_mode": "template-fill", "title": "施工组织设计"}
            )
        assert mode == oa.CONTENT_MODE_AI_GENERATE
        assert any("content_mode=template-fill" in rec.getMessage() for rec in caplog.records)

    def test_resolve_implemented_mode_is_silent(self, caplog):
        with caplog.at_level(logging.WARNING):
            mode = oa.resolve_content_mode({"content_mode": "ai-generate"})
        assert mode == oa.CONTENT_MODE_AI_GENERATE
        assert not [r for r in caplog.records if "content_mode=" in r.getMessage()]

    def test_augment_normalizes_mode_and_preserves_explicit_lock(self):
        items = [
            {"title": "甲"},
            {"title": "乙", "content_mode": "AI", "locked": False},
            {"title": "丙", "content_mode": "point_to_point"},
        ]
        out = oa.augment_aligned_l1(items)
        assert out[0]["content_mode"] == oa.CONTENT_MODE_AI_GENERATE
        assert out[0]["locked"] is True
        assert out[1]["content_mode"] == oa.CONTENT_MODE_AI_GENERATE
        assert out[1]["locked"] is False  # setdefault 不覆盖显式值
        assert out[2]["content_mode"] == oa.CONTENT_MODE_POINT_TO_POINT
