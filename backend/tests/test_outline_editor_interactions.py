"""目录生成模块（outline）组件级交互测试

覆盖 2026-09-29 修复项与目录编辑链路（路由层 ↔ 服务层 ↔ DB ↔ 跨模块）：
1. level/sort_order 服务端自动推导（BUG-FIX：客户端盲信导致层级错位/排序跳前）
   及显式传值的向后兼容
2. 编号同步：重排后 get_section_path 派生编号随之重算
3. reorder 项目隔离：跨项目章节 ID 不被改写（UPDATE 带 project_id 过滤回归）
4. clear/reorder 路由权限校验接线（BUG-FIX 2026-09-29 IDOR 修复）
5. review-with-autofix 数据链（BUG-FIX 2026-09-29）：修复结果落库、
   返回带数据库数字 ID 的完整目录树、旧正文按 (level,title) 快照回填且
   word_count 统一 _count_words 口径（含 DB 列同步）
"""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.main import app  # noqa: E402
from app.security.auth import get_current_user  # noqa: E402


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def project_id(client):
    resp = client.post("/api/v1/projects", json={
        "name": "目录模块交互测试项目",
        "industry": "公共建筑",
        "bidder_name": "测试投标方",
    })
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _create_section(client, pid, payload):
    resp = client.post(f"/api/v1/projects/{pid}/sections", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _get_tree(client, pid):
    resp = client.get(f"/api/v1/projects/{pid}/sections")
    assert resp.status_code == 200, resp.text
    return resp.json()


class TestSectionPositionNormalization:
    """create 路由：缺省 level/sort_order 由服务端推导（前缀树位置派生编号体系）"""

    def test_root_and_child_levels_derived(self, client, project_id):
        r1 = _create_section(client, project_id, {"title": "第一章 编制依据"})
        assert r1["level"] == 1
        assert r1["sort_order"] == 1  # MAX(0)+1，不再是默认 0

        r2 = _create_section(client, project_id, {"title": "第二章 施工方案"})
        assert r2["level"] == 1
        assert r2["sort_order"] == 2  # 追加而非跳前

        child = _create_section(client, project_id, {"title": "2.1 工艺流程", "parent_id": r2["id"]})
        assert child["level"] == 2  # 父 level+1
        assert child["sort_order"] == 1  # 同父下 MAX+1

        grand = _create_section(client, project_id, {"title": "2.1.1 工序细则", "parent_id": child["id"]})
        assert grand["level"] == 3
        assert grand["sort_order"] == 1

        # 树结构完整：三级链路 id 全部为数字（前端编辑依赖）
        tree = _get_tree(client, project_id)
        by_id = {n["id"]: n for n in tree}
        assert r1["id"] in by_id and r2["id"] in by_id
        ch = next(c for c in by_id[r2["id"]]["children"] if c["id"] == child["id"])
        assert next(c for c in ch["children"] if c["id"] == grand["id"])["level"] == 3

    def test_new_sibling_appended_at_tail(self, client, project_id):
        """BUG 回归：sort_order 缺省 0 时新章节刷新后跳到同级最前"""
        ids = [_create_section(client, project_id, {"title": f"顺序{i}"})["id"] for i in (1, 2, 3)]
        _create_section(client, project_id, {"title": "顺序4"})  # 应追加在尾部
        tree = _get_tree(client, project_id)
        assert [n["id"] for n in tree] == ids + [tree[3]["id"]]
        assert tree[3]["title"] == "顺序4"

    def test_explicit_values_backwards_compatible(self, client, project_id):
        """显式传 level/sort_order 仍原样入库（批量导入等既有调用方兼容）"""
        s = _create_section(client, project_id, {"title": "显式定位", "level": 5, "sort_order": 99})
        assert s["level"] == 5 and s["sort_order"] == 99

    def test_path_numbering_synced_after_reorder(self, client, project_id):
        """标题/顺序变化后 DB 树位置派生编号即时重算"""
        from app.services.sections import get_section_path

        a = _create_section(client, project_id, {"title": "甲"})
        b = _create_section(client, project_id, {"title": "乙"})
        _create_section(client, project_id, {"title": "丙"})
        assert get_section_path(b["id"]) == "2"

        resp = client.post(f"/api/v1/projects/{project_id}/sections/reorder", json={
            "groups": [{"parent_id": None, "ordered_ids": [b["id"], a["id"]]}],
        })
        assert resp.status_code == 200, resp.text
        assert get_section_path(b["id"]) == "1"
        assert get_section_path(a["id"]) == "2"


class TestReorderProjectIsolation:
    """reorder UPDATE 带 project_id 过滤，跨项目 ID 不被改写"""

    def test_foreign_section_id_ignored(self, client, project_id):
        own = _create_section(client, project_id, {"title": "本项目章节"})

        other = client.post("/api/v1/projects", json={
            "name": "目录隔离对照项目", "industry": "公共建筑", "bidder_name": "测试投标方",
        }).json()["id"]
        foreign = _create_section(client, other, {"title": "他项目章节"})

        resp = client.post(f"/api/v1/projects/{project_id}/sections/reorder", json={
            "groups": [{"parent_id": None, "ordered_ids": [foreign["id"], own["id"]]}],
        })
        assert resp.status_code == 200, resp.text

        from app.db import get_db
        with get_db(readonly=True) as conn:
            f_row = conn.execute("SELECT sort_order FROM sections WHERE id = ?", (foreign["id"],)).fetchone()
            o_row = conn.execute("SELECT sort_order FROM sections WHERE id = ?", (own["id"],)).fetchone()
        assert f_row["sort_order"] == foreign["sort_order"]  # 未被本项目的 reorder 改写
        assert o_row["sort_order"] == 2  # 仅本项目章节按新顺序编号（foreign 占位 1）


class TestClearReorderPermissionWiring:
    """clear/reorder 权限校验接线（IDOR 修复回归）"""

    def test_missing_project_404_with_user(self, client):
        fake_user = SimpleNamespace(id=1)
        app.dependency_overrides[get_current_user] = lambda: fake_user
        try:
            r1 = client.delete("/api/v1/projects/999999/sections")
            assert r1.status_code == 404, "清空路由应经项目归属校验（不存在项目 404）"
            r2 = client.post("/api/v1/projects/999999/sections/reorder", json={"groups": []})
            assert r2.status_code == 404, "重排路由应经项目归属校验"
        finally:
            app.dependency_overrides.clear()

    def test_permission_check_invoked_on_valid_project(self, client, project_id):
        fake_user = SimpleNamespace(id=1)
        app.dependency_overrides[get_current_user] = lambda: fake_user
        try:
            with patch("app.routers.sections.require_project_permission", AsyncMock()) as mock_perm:
                r = client.post(f"/api/v1/projects/{project_id}/sections/reorder", json={"groups": []})
                assert r.status_code == 200, r.text
                mock_perm.assert_awaited_with(project_id, fake_user)

                with patch("app.routers.sections.stop_generation", AsyncMock(return_value={})):
                    r2 = client.delete(f"/api/v1/projects/{project_id}/sections")
                assert r2.status_code == 200, r2.text
                assert mock_perm.await_count == 2
        finally:
            app.dependency_overrides.clear()

    def test_clear_empties_tree_anonymous(self, client, project_id):
        _create_section(client, project_id, {"title": "待清空"})
        resp = client.delete(f"/api/v1/projects/{project_id}/sections")
        assert resp.status_code == 200, resp.text
        assert _get_tree(client, project_id) == []


class TestReviewWithAutofixPersistence:
    """review-with-autofix 修复结果落库 + 返回带数字 ID 的树（数据链修复回归）"""

    _FIXED_OUTLINE = [
        {"title": "编制依据", "children": [{"title": "编制说明", "children": []}]},
        {"title": "施工方案", "children": []},
    ]

    def _call_route(self, client, pid, result):
        with patch("app.routers.sections._prepare_review", AsyncMock(return_value=(MagicMock(), "[]", "[]"))), \
             patch("app.services.ai.workflows.review_outline_with_autofix", AsyncMock(return_value=result)):
            return client.post(f"/api/v1/projects/{pid}/sections/review-with-autofix")

    def test_repaired_result_persisted_with_numeric_ids(self, client, project_id):
        _create_section(client, project_id, {"title": "旧章节"})
        result = {"passed": False, "repaired": True, "fixed_outline": self._FIXED_OUTLINE,
                  "issues": [{"type": "missing"}], "suggestions": []}

        resp = self._call_route(client, project_id, result)
        assert resp.status_code == 200, resp.text
        payload = resp.json()["result"]
        assert "tree" in payload, "修复成功必须返回落库后的完整目录树"

        tree = payload["tree"]
        titles = [n["title"] for n in tree]
        assert "编制依据" in titles and "施工方案" in titles
        # 关键链路修复：树节点必须携带数据库数字 ID（前端改名/删除/移动依赖）
        for node in tree:
            assert isinstance(node["id"], int) and node["id"] > 0
        parent = next(n for n in tree if n["title"] == "编制依据")
        child = parent["children"][0]
        assert child["title"] == "编制说明"
        assert child["level"] == 2  # 落库时按父子关系重算层级

        # 与 GET 树一致（同一 build_tree 数据源，前后端同步）
        fetched = _get_tree(client, project_id)
        assert {n["id"] for n in fetched} == {n["id"] for n in tree}

        # 编辑链路贯通：拿返回的 id 直接 PATCH 不再打在 id=undefined 上
        upd = client.patch(
            f"/api/v1/projects/{project_id}/sections/{child['id']}",
            json={"title": "编制说明（修订）"},
        )
        assert upd.status_code == 200, upd.text
        assert upd.json()["title"] == "编制说明（修订）"

    def test_content_snapshot_backfill_and_word_count_alignment(self, client, project_id):
        """旧正文按 (level,title) 回填，word_count 用 _count_words 口径且同步 DB 列"""
        from app.services.sections import _count_words

        root = _create_section(client, project_id, {"title": "编制依据"})
        child = _create_section(client, project_id, {"title": "编制说明", "parent_id": root["id"]})
        content = "本章说明技术标文件的编制依据，包括招标文件、设计规范及现行施工验收标准。"
        patch_resp = client.patch(
            f"/api/v1/projects/{project_id}/sections/{child['id']}",
            json={"content": content},
        )
        assert patch_resp.status_code == 200, patch_resp.text

        result = {"passed": False, "repaired": True, "fixed_outline": self._FIXED_OUTLINE,
                  "issues": [], "suggestions": []}
        resp = self._call_route(client, project_id, result)
        assert resp.status_code == 200, resp.text
        tree = resp.json()["result"]["tree"]
        new_child = next(n for n in next(x for x in tree if x["title"] == "编制依据")["children"]
                         if n["title"] == "编制说明")
        expected_wc = _count_words(content)
        assert new_child["has_content"] is True, "同名同层章节正文应被快照回填"
        assert new_child["word_count"] == expected_wc

        from app.db import get_db
        with get_db(readonly=True) as conn:
            row = conn.execute(
                "SELECT content, word_count FROM sections WHERE id = ?", (new_child["id"],)
            ).fetchone()
        assert row["content"] == content
        assert row["word_count"] == expected_wc, "DB word_count 列应与回填正文口径一致（非 0/非裸 len）"

    def test_not_repaired_keeps_tree_untouched(self, client, project_id):
        existing = _create_section(client, project_id, {"title": "无需修复的章节"})
        result = {"passed": True, "repaired": False, "fixed_outline": None,
                  "issues": [], "suggestions": []}
        resp = self._call_route(client, project_id, result)
        assert resp.status_code == 200, resp.text
        payload = resp.json()["result"]
        assert "tree" not in payload
        tree = _get_tree(client, project_id)
        assert [n["id"] for n in tree] == [existing["id"]]

    def test_persist_failure_returns_500(self, client, project_id):
        """落库失败必须显式 500，不能静默返回裸 JSON 造成前后端状态分裂"""
        result = {"passed": False, "repaired": True, "fixed_outline": self._FIXED_OUTLINE,
                  "issues": [], "suggestions": []}
        with patch("app.routers.sections._prepare_review", AsyncMock(return_value=(MagicMock(), "[]", "[]"))), \
             patch("app.services.ai.workflows.review_outline_with_autofix", AsyncMock(return_value=result)), \
             patch("app.routers.sse.sse_db_ops._clear_and_save_outline",
                   side_effect=RuntimeError("db locked")):
            resp = client.post(f"/api/v1/projects/{project_id}/sections/review-with-autofix")
        assert resp.status_code == 500
        assert "保存失败" in resp.json()["detail"]
