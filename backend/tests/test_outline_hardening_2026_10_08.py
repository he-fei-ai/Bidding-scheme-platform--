"""生成大纲模块 · 加固回归（BUG-FIX 2026-10-08）

覆盖本轮定位并修复的 10 条故障链，全部为「先复现、后修复」的钉子测试：

1. BUG-01  children 非 list 时静默丢子树（_flatten_outline_tree / _renumber_items）
2. BUG-02  normalize_outline_items 不收口非 list children → 落库链路 AttributeError
3. BUG-03  update_section 改 parent_id 后 level 未重算（多级编号不同步）
4. BUG-04  update_section 换父后同父 sort_order 撞号
5. BUG-05  build_tree 缓存失效竞态（写缓存覆盖失效）
6. BUG-06  get_section_path / get_ancestor_titles 父链成环 → 死循环
7. BUG-07  review-with-autofix 非写锁异常未捕获（前后端状态分裂）
8. OPT-01  validate_outline_levels 层级体检（level vs 实际深度）
9. OPT-02  reorder_sections 同父重复 sort_order 自愈
10. BUG-08  手工 PATCH 标题/改名的编号前缀剥离与编号重算联动
"""
from pathlib import Path
import sqlite3
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
import pytest


BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.db import get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import SectionCreate, SectionUpdate  # noqa: E402
from app.security.auth import get_current_user  # noqa: E402
from app.services import sections as sections_service  # noqa: E402
from app.services.ai.workflows_outline_utils import (  # noqa: E402
    _dedupe_outline_titles,
    _flatten_outline_tree,
    _renumber_items,
    normalize_outline_items,
)


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def project_id(client):
    resp = client.post(
        "/api/v1/projects",
        json={"name": "大纲加固回归项目", "industry": "公共建筑", "bidder_name": "测试投标方"},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _mk_section(client, pid, payload):
    resp = client.post(f"/api/v1/projects/{pid}/sections", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _levels_of(pid):
    with get_db(readonly=True) as conn:
        return {
            r["id"]: r["level"]
            for r in conn.execute(
                "SELECT id, level FROM sections WHERE project_id = ?", (pid,)
            ).fetchall()
        }


def _orders_of(pid, parent_id):
    with get_db(readonly=True) as conn:
        if parent_id is None:
            rows = conn.execute(
                "SELECT id, sort_order FROM sections WHERE project_id = ? AND parent_id IS NULL"
                " ORDER BY sort_order, created_at",
                (pid,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, sort_order FROM sections WHERE project_id = ? AND parent_id = ?"
                " ORDER BY sort_order, created_at",
                (pid, parent_id),
            ).fetchall()
    return [(r["id"], r["sort_order"]) for r in rows]


# ============================================================================
# BUG-01 / BUG-02：children 形态异常
# ============================================================================


class TestChildrenShapeHardening:
    """children 为 dict/字符串时既不能静默丢子树，也不能把脏值传下游炸 500"""

    def test_flatten_dict_children_does_not_silently_drop_subtree(self, caplog):
        tree = [{"title": "A", "children": {"0": {"title": "A1"}, "1": {"title": "A2"}}}]
        with caplog.at_level("WARNING"):
            flat = _flatten_outline_tree(tree)
        # 关键：不能像修复前那样把 A1/A2 静默丢弃且毫无痕迹
        assert len(flat) == 1, "父节点必须保留"
        assert flat[0]["title"] == "A"
        assert any("children 类型非法" in r.message for r in caplog.records), (
            "非法形态必须留痕，否则事后无法追责（无声丢目录）"
        )

    def test_renumber_dict_children_does_not_silently_drop_subtree(self, caplog):
        tree = [{"title": "A", "children": {"0": {"title": "A1"}}}]
        with caplog.at_level("WARNING"):
            renumbered = _renumber_items(tree)
        assert renumbered[0]["number"] == "1"
        assert renumbered[0]["children"] == {"0": {"title": "A1"}}
        assert any("children 类型非法" in r.message for r in caplog.records)

    def test_flatten_normal_list_still_recurses_with_correct_levels(self):
        tree = [{"title": "A", "children": [{"title": "A1", "children": [{"title": "A1.1"}]}]}]
        flat = _flatten_outline_tree(tree)
        assert [(f["title"], f["level"], f["sort_order"]) for f in flat] == [
            ("A", 1, 1),
            ("A1", 2, 1),
            ("A1.1", 3, 1),
        ]

    def test_renumber_normal_list_numbering_is_path_style(self):
        tree = [{"title": "A", "children": [{"title": "A1", "children": [{"title": "A1.1"}]}]}]
        out = _renumber_items(tree)
        assert out[0]["number"] == "1"
        assert out[0]["children"][0]["number"] == "1.1"
        assert out[0]["children"][0]["children"][0]["number"] == "1.1.1"
        # title 必须保持纯文本（编号命名空间统一：编号只在 number 字段）
        assert out[0]["children"][0]["children"][0]["title"] == "A1.1"

    def test_normalize_collapses_non_list_children_to_empty(self):
        tree = [{"title": "A", "children": {"0": {"title": "A1"}}}]
        out = normalize_outline_items(tree)
        assert out[0]["children"] == [], (
            "归一化必须收口形态：原实现把 dict 原样传下去，"
            "_save_nodes 的 enumerate 会拿到字符串 key 再 .get() → AttributeError → 500"
        )

    def test_normalize_keeps_valid_children(self):
        tree = [{"title": "A", "children": [{"title": "A1"}]}]
        out = normalize_outline_items(tree)
        assert out[0]["children"][0]["title"] == "A1"
        assert out[0]["children"][0]["level"] == 2

    def test_full_pipeline_dict_children_does_not_raise(self):
        """normalize -> dedupe -> renumber -> flatten 全链路不得抛异常"""
        tree = [{"title": "A", "children": {"0": {"title": "A1"}}}]
        out = _dedupe_outline_titles(normalize_outline_items(tree))
        assert _flatten_outline_tree(out)
        assert _renumber_items(out)

    def test_empty_and_none_inputs_are_safe(self):
        assert _flatten_outline_tree([]) == []
        assert _renumber_items([]) == []
        assert normalize_outline_items([]) == []
        # None 也要能安全降级（落库链路会传 outline or []）
        assert normalize_outline_items(None) == []


# ============================================================================
# BUG-03 / BUG-04：换父后 level 与 sort_order 重算
# ============================================================================


class TestMoveParentRecomputesLevel:
    """PATCH parent_id 必须重算「自身 + 全部后代」的 level 与同父位次"""

    def _chain(self, client, pid):
        l1 = _mk_section(client, pid, {"title": "第一章"})
        l2 = _mk_section(client, pid, {"title": "编制说明", "parent_id": l1["id"]})
        l3 = _mk_section(client, pid, {"title": "引用规范", "parent_id": l2["id"]})
        l4 = _mk_section(client, pid, {"title": "规范细则", "parent_id": l3["id"]})
        return l1, l2, l3, l4

    def test_level_recomputed_for_whole_subtree_on_demotion(self, client, project_id):
        l1, l2, l3, l4 = self._chain(client, project_id)
        assert _levels_of(project_id) == {l1["id"]: 1, l2["id"]: 2, l3["id"]: 3, l4["id"]: 4}

        # 把原 L2 提到根 → 它与整棵子树都必须上浮一层
        resp = client.patch(
            f"/api/v1/projects/{project_id}/sections/{l2['id']}", json={"parent_id": None}
        )
        assert resp.status_code == 200, resp.text
        assert _levels_of(project_id) == {
            l1["id"]: 1, l2["id"]: 1, l3["id"]: 2, l4["id"]: 3
        }, "换父后 level 未随深度重算 → 前端编号分组 / DOCX outlineLvl / 字数分档三处错位"

    def test_level_recomputed_on_promotion(self, client, project_id):
        l1, l2, l3, _l4 = self._chain(client, project_id)
        # 把 L3 挂到根 → 它和它的子节点都变浅
        resp = client.patch(
            f"/api/v1/projects/{project_id}/sections/{l3['id']}", json={"parent_id": None}
        )
        assert resp.status_code == 200, resp.text
        levels = _levels_of(project_id)
        assert levels[l3["id"]] == 1

    def test_sibling_sort_order_normalized_after_reparent(self, client, project_id):
        a = _mk_section(client, project_id, {"title": "甲"})
        b = _mk_section(client, project_id, {"title": "乙"})
        c = _mk_section(client, project_id, {"title": "丙"})
        root = _mk_section(client, project_id, {"title": "目标父"})
        # a 已是 root 的子（sort_order=1）；把 b 挂到 root 下
        client.patch(
            f"/api/v1/projects/{project_id}/sections/{a['id']}",
            json={"parent_id": root["id"]},
        )
        resp = client.patch(
            f"/api/v1/projects/{project_id}/sections/{b['id']}",
            json={"parent_id": root["id"]},
        )
        assert resp.status_code == 200, resp.text
        orders = [so for _sid, so in _orders_of(project_id, root["id"])]
        assert orders == sorted(orders), "同父 sort_order 必须保持有序"
        assert len(set(orders)) == len(orders), "同父 sort_order 撞号会让 build_tree 排序退化到 created_at"
        # c 仍在根层，不得被牵连
        assert _levels_of(project_id)[c["id"]] == 1

    def test_explicit_level_is_ignored_level_only_from_position(self, client, project_id):
        """层级只由树位置派生：PATCH 不接受显式 level（P0 契约：父子不得倒挂）

        SectionUpdate 没有 level 字段（Pydantic v2 extra='ignore'），
        客户端传的 level 被静默丢弃；直接调服务层塞 level 也必须被剔除。
        """
        l1 = _mk_section(client, project_id, {"title": "父节点"})
        c = _mk_section(client, project_id, {"title": "子节点", "parent_id": l1["id"]})
        gc = _mk_section(client, project_id, {"title": "孙节点", "parent_id": c["id"]})

        # 显式 level=3 + 提到根 —— 期望按位置派生为 1（而非落库成 3 造成父子倒挂）
        sections_service.update_section(
            c["id"], SectionUpdate(parent_id=None, level=3)  # type: ignore[call-arg]
        )
        assert _levels_of(project_id)[c["id"]] == 1
        assert _levels_of(project_id)[gc["id"]] == 2, "子孙必须跟随父节点层级派生"

    def test_level_only_field_patch_is_noop(self, client, project_id):
        l1 = _mk_section(client, project_id, {"title": "父节点"})
        resp = client.patch(
            f"/api/v1/projects/{project_id}/sections/{l1['id']}", json={"level": 9}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["level"] == 1, "PATCH 无法单独改层级（层级由树位置派生）"

    def test_cycle_guard_still_rejects_and_does_not_touch_levels(self, client, project_id):
        l1, l2, _l3, _l4 = self._chain(client, project_id)
        resp = client.patch(
            f"/api/v1/projects/{project_id}/sections/{l1['id']}",
            json={"parent_id": l2["id"]},
        )
        assert resp.status_code == 400, resp.text
        levels = _levels_of(project_id)
        assert levels[l1["id"]] == 1 and levels[l2["id"]] == 2, "被拒的换父不得留下半写入的层级"
        assert levels[_l3["id"]] == 3 and levels[_l4["id"]] == 4


# ============================================================================
# BUG-05：build_tree 缓存失效竞态
# ============================================================================


class TestBuildTreeCacheRace:
    """构建期间发生的失效不得被随后的写缓存覆盖"""

    def test_invalidate_during_build_is_not_swallowed(self, client, project_id):
        """构建期间发生的失效必须让本次构建**放弃写缓存**（代号机制）"""
        sec = _mk_section(client, project_id, {"title": "竞态章节"})
        sections_service._invalidate_build_tree_cache(project_id)

        import app.services.sections as svc

        entered = threading.Event()
        release = threading.Event()
        real_get_db = svc.get_db

        class _Blocking:
            """在只读连接建立后、制造一个确定性的「构建中失效」窗口"""

            def __init__(self, ctx):
                self._ctx = ctx

            def __enter__(self):
                conn = self._ctx.__enter__()
                entered.set()
                release.wait(timeout=10)
                # 模拟并发线程在本次构建读库之后完成了写操作
                sections_service._invalidate_build_tree_cache(project_id)
                return conn

            def __exit__(self, *exc):
                return self._ctx.__exit__(*exc)

        def _hooked(*a, **kw):
            return _Blocking(real_get_db(*a, **kw))

        result: dict = {}

        def _build():
            try:
                result["tree"] = svc.build_tree(project_id)
            except Exception as e:  # pragma: no cover
                result["err"] = e

        with patch.object(svc, "get_db", _hooked):
            t = threading.Thread(target=_build, daemon=True)
            t.start()
            assert entered.wait(timeout=10), "未进入查库阶段"
            release.set()
            t.join(timeout=15)

        assert "err" not in result, result.get("err")
        assert {n.title for n in result["tree"]} == {"竞态章节"}
        # 关键断言：构建期失效后不得把本次（可能已过期）的结果写回缓存
        assert project_id not in sections_service._build_tree_cache, (
            "构建期间发生失效却仍写回缓存 → 失效被吞，脏树会被后续请求读满 60s"
        )
        # 下一次读取必须重新查库，且结果正确
        fresh = sections_service.build_tree(project_id)
        assert [n.id for n in fresh] == [sec["id"]]

    def test_epoch_advances_on_invalidate(self):
        before = sections_service._current_build_tree_epoch()
        sections_service._invalidate_build_tree_cache(1_000_001)
        after = sections_service._current_build_tree_epoch()
        assert after > before, "失效必须推进代号，否则构建期失效仍会被写缓存吞掉"

    def test_cache_hit_returns_same_object_for_hot_project(self, client, project_id):
        _mk_section(client, project_id, {"title": "热项目"})
        a = sections_service.build_tree(project_id)
        b = sections_service.build_tree(project_id)
        assert a is b, "60s TTL 内应命中缓存（性能契约）"

    def test_write_path_invalidates_cache(self, client, project_id):
        sec = _mk_section(client, project_id, {"title": "初始"})
        assert sections_service.build_tree(project_id) is not None
        client.patch(
            f"/api/v1/projects/{project_id}/sections/{sec['id']}",
            json={"title": "改名后"},
        )
        tree = sections_service.build_tree(project_id)
        assert tree[0].title == "改名后", "写路径必须失效 build_tree 缓存"


# ============================================================================
# BUG-06：父链成环时不死循环
# ============================================================================


class TestParentChainCycleGuard:
    def test_get_section_path_survives_cycle(self, project_id, client):
        a = _mk_section(client, project_id, {"title": "A"})
        b = _mk_section(client, project_id, {"title": "B", "parent_id": a["id"]})
        # 裸 SQL 造环，绕过 update_section 的环检测（模拟历史脏数据/并发中间态）
        with get_db() as conn:
            conn.execute("UPDATE sections SET parent_id = ? WHERE id = ?", (b["id"], a["id"]))

        done = threading.Event()

        def _run():
            sections_service.get_section_path(a["id"])
            sections_service.get_ancestor_titles(a["id"])
            done.set()

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        assert done.wait(timeout=10), "父链成环时 get_section_path/get_ancestor_titles 必须中止而非死循环"
        # 返回值必须是字符串/列表，不能是 None 或抛异常
        assert isinstance(sections_service.get_section_path(b["id"]), str)
        assert isinstance(sections_service.get_ancestor_titles(b["id"]), list)

    def test_normal_chain_path_still_correct(self, project_id, client):
        l1 = _mk_section(client, project_id, {"title": "总则"})
        l2 = _mk_section(client, project_id, {"title": "编制依据", "parent_id": l1["id"]})
        l3 = _mk_section(client, project_id, {"title": "招标文件", "parent_id": l2["id"]})
        assert sections_service.get_section_path(l3["id"]) == "1.1.1"
        assert sections_service.get_ancestor_titles(l3["id"]) == ["总则", "编制依据", "招标文件"]


# ============================================================================
# BUG-07：review-with-autofix 落库失败的 500 语义
# ============================================================================


_FIXED_OUTLINE = [
    {"title": "编制依据", "children": [{"title": "编制说明", "children": []}]},
    {"title": "施工方案", "children": []},
]


class TestReviewAutofixPersistFailureSemantics:
    def _call(self, client, pid):
        result = {
            "passed": False,
            "repaired": True,
            "fixed_outline": _FIXED_OUTLINE,
            "issues": [],
            "suggestions": [],
        }
        with patch(
            "app.routers.sections._prepare_review",
            AsyncMock(return_value=(MagicMock(), "[]", "[]")),
        ), patch(
            "app.services.ai.workflows.review_outline_with_autofix",
            AsyncMock(return_value=result),
        ):
            return client.post(f"/api/v1/projects/{pid}/sections/review-with-autofix")

    def test_sqlite_locked_returns_500_with_reason(self, client, project_id):
        """sqlite3.OperationalError 不是 DatabaseWriteLockTimeout，
        修复前会裸穿路由 → 生产 500 无信息、前后端状态分裂"""
        with patch(
            "app.routers.sse.sse_db_ops._clear_and_save_outline",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            resp = self._call(client, project_id)
        assert resp.status_code == 500, resp.text
        assert "保存失败" in resp.json()["detail"]

    def test_generic_error_returns_500_with_reason(self, client, project_id):
        with patch(
            "app.routers.sse.sse_db_ops._clear_and_save_outline",
            side_effect=OSError("磁盘空间不足"),
        ):
            resp = self._call(client, project_id)
        assert resp.status_code == 500, resp.text
        assert "保存失败" in resp.json()["detail"]
        assert "磁盘空间不足" in resp.json()["detail"]

    def test_write_lock_timeout_still_returns_503_retryable(self, client, project_id):
        from app.db import DatabaseWriteLockTimeout

        with patch(
            "app.routers.sse.sse_db_ops._clear_and_save_outline",
            side_effect=DatabaseWriteLockTimeout("busy"),
        ):
            resp = self._call(client, project_id)
        assert resp.status_code == 503, resp.text
        assert "稍后重试" in resp.json()["detail"], "可重试争用必须保持 503 语义（向前兼容）"

    def test_success_returns_persisted_tree_with_numeric_ids(self, client, project_id):
        _mk_section(client, project_id, {"title": "旧章节"})
        resp = self._call(client, project_id)
        assert resp.status_code == 200, resp.text
        tree = resp.json()["result"]["tree"]
        for node in tree:
            assert isinstance(node["id"], int) and node["id"] > 0


# ============================================================================
# OPT-01：层级体检 validate_outline_levels
# ============================================================================


class TestValidateOutlineLevels:
    def test_clean_tree_is_valid(self, project_id, client):
        l1 = _mk_section(client, project_id, {"title": "一"})
        _mk_section(client, project_id, {"title": "一.1", "parent_id": l1["id"]})
        _mk_section(client, project_id, {"title": "一.2", "parent_id": l1["id"]})
        result = sections_service.validate_outline_levels(project_id)
        assert result["valid"] is True, result["errors"]
        assert result["checked"] == 3

    def test_level_drift_detected(self, project_id, client):
        l1 = _mk_section(client, project_id, {"title": "一"})
        c = _mk_section(client, project_id, {"title": "一.1", "parent_id": l1["id"]})
        # 裸 SQL 制造 level 漂移（历史数据 / 旁路写入）
        with get_db() as conn:
            conn.execute("UPDATE sections SET level = 7 WHERE id = ?", (c["id"],))
        result = sections_service.validate_outline_levels(project_id)
        assert result["valid"] is False
        assert any("与实际层级深度" in e for e in result["errors"])

    def test_empty_project_is_valid(self, project_id):
        result = sections_service.validate_outline_levels(project_id)
        assert result["valid"] is True and result["checked"] == 0

    def test_structural_validator_contract_unchanged(self, project_id, client):
        """validate_outline_integrity 的三项结构契约不受层级体检影响"""
        l1 = _mk_section(client, project_id, {"title": "一"})
        c = _mk_section(client, project_id, {"title": "一.1", "parent_id": l1["id"]})
        with get_db() as conn:
            conn.execute("UPDATE sections SET level = 9 WHERE id = ?", (c["id"],))
        # level 漂移不应让结构校验判 invalid（valid 语义只覆盖结构三项）
        assert sections_service.validate_outline_integrity(project_id)["valid"] is True
        assert sections_service.validate_outline_levels(project_id)["valid"] is False


# ============================================================================
# OPT-02：reorder_sections 自愈
# ============================================================================


class TestReorderSelfHeal:
    def test_duplicate_sort_order_is_repaired(self, project_id, client):
        a = _mk_section(client, project_id, {"title": "甲"})
        _mk_section(client, project_id, {"title": "乙"})
        _mk_section(client, project_id, {"title": "丙"})
        # 人为制造同父撞号
        with get_db() as conn:
            conn.execute("UPDATE sections SET sort_order = 1 WHERE project_id = ?", (project_id,))
        sections_service._invalidate_build_tree_cache(project_id)

        tree = sections_service.reorder_sections(
            project_id, [{"parent_id": None, "ordered_ids": [a["id"]]}]
        )
        orders = [n.sort_order for n in tree]
        assert sorted(orders) == list(range(1, len(tree) + 1)), (
            "重复 sort_order 会让 build_tree 排序退化为 created_at（拖拽顺序被吞），必须自愈"
        )

    def test_partial_group_keeps_unique_order(self, project_id, client):
        """分组不完整（值唯一）时不改状态，只告警 —— 避免强行重排丢位次"""
        a = _mk_section(client, project_id, {"title": "甲"})
        tree = sections_service.reorder_sections(
            project_id, [{"parent_id": None, "ordered_ids": [a["id"]]}]
        )
        assert len(tree) == 1
        with get_db(readonly=True) as conn:
            so = conn.execute(
                "SELECT sort_order FROM sections WHERE id = ?", (a["id"],)
            ).fetchone()["sort_order"]
        assert so == 1


# ============================================================================
# BUG-08：标题编号前缀剥离与改名后的编号重算
# ============================================================================


class TestTitleNumberingContract:
    def test_create_strips_embedded_number_prefix(self, client, project_id):
        sec = _mk_section(client, project_id, {"title": "1.1 工期保证措施"})
        assert sec["title"] == "工期保证措施", "标题内嵌编号会与派生编号形成双重编号"

    def test_patch_strips_embedded_number_prefix(self, client, project_id):
        sec = _mk_section(client, project_id, {"title": "原始标题"})
        resp = client.patch(
            f"/api/v1/projects/{project_id}/sections/{sec['id']}",
            json={"title": "1.2 修订后的标题"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["title"] == "修订后的标题"

    def test_rename_keeps_numbering_correct(self, client, project_id):
        """改名后编号必须按新树位置重算（标题内容不影响编号）"""
        a = _mk_section(client, project_id, {"title": "原甲"})
        b = _mk_section(client, project_id, {"title": "原乙"})
        client.patch(
            f"/api/v1/projects/{project_id}/sections/{a['id']}", json={"title": "新甲"}
        )
        tree = sections_service.build_tree(project_id)
        assert [n.title for n in tree] == ["新甲", "原乙"]
        assert [n.id for n in tree] == [a["id"], b["id"]], "编号序列不变（由位置派生）"

    def test_word_budget_uses_recomputed_level(self, project_id, client):
        """换父后 level 变化必须体现在字数分档上（跨模块联动）"""
        l1 = _mk_section(client, project_id, {"title": "第一章"})
        l2 = _mk_section(client, project_id, {"title": "编制说明", "parent_id": l1["id"]})
        before = sections_service.get_section_word_budget(project_id)
        assert before["level2_count"] == 1 and before["level1_count"] == 1

        client.patch(
            f"/api/v1/projects/{project_id}/sections/{l2['id']}", json={"parent_id": None}
        )
        after = sections_service.get_section_word_budget(project_id)
        assert after["level2_count"] == 0, "换父后仍被算作 L2 → 字数预算落到错误档"
        assert after["level1_count"] == 2


# ============================================================================
# 端到端：目录树结构完整性贯通
# ============================================================================


class TestOutlineEndToEndStructure:
    def test_full_chain_structure_and_numbering(self, project_id, client):
        """三级目录 CRUD → 换父 → 重排 → 完整性校验 全链路贯通"""
        outline = [
            {
                "title": "第一章 总则",
                "children": [
                    {"title": "编制依据", "children": [{"title": "招标文件"}, {"title": "设计规范"}]},
                    {"title": "适用范围"},
                ],
            },
            {"title": "第二章 施工部署", "children": [{"title": "总体安排"}]},
        ]
        root = _mk_section(client, project_id, {"title": outline[0]["title"]})
        l2a = _mk_section(
            client, project_id,
            {"title": outline[0]["children"][0]["title"], "parent_id": root["id"]},
        )
        l3a = _mk_section(
            client, project_id,
            {
                "title": outline[0]["children"][0]["children"][0]["title"],
                "parent_id": l2a["id"],
            },
        )
        l3b = _mk_section(
            client, project_id,
            {
                "title": outline[0]["children"][0]["children"][1]["title"],
                "parent_id": l2a["id"],
            },
        )
        l2b = _mk_section(
            client, project_id,
            {"title": outline[0]["children"][1]["title"], "parent_id": root["id"]},
        )
        root2 = _mk_section(client, project_id, {"title": outline[1]["title"]})
        l2c = _mk_section(
            client, project_id,
            {"title": outline[1]["children"][0]["title"], "parent_id": root2["id"]},
        )

        assert sections_service.validate_outline_integrity(project_id)["valid"] is True
        assert sections_service.validate_outline_levels(project_id)["valid"] is True

        # 编号按路径派生
        assert sections_service.get_section_path(l3a["id"]) == "1.1.1"
        assert sections_service.get_section_path(l3b["id"]) == "1.1.2"
        assert sections_service.get_section_path(l2c["id"]) == "2.1"

        # 把 1.2 提到根 → 全链路重算
        client.patch(
            f"/api/v1/projects/{project_id}/sections/{l2b['id']}", json={"parent_id": None}
        )
        assert sections_service.validate_outline_levels(project_id)["valid"] is True
        levels = _levels_of(project_id)
        assert levels[l2b["id"]] == 1
        assert sections_service.get_section_path(l2b["id"]) == "2"

        # 删除无子节点的叶子
        resp = client.delete(
            f"/api/v1/projects/{project_id}/sections/{l3a['id']}"
        )
        assert resp.status_code == 200, resp.text
        assert sections_service.validate_outline_integrity(project_id)["valid"] is True
        assert sections_service.validate_outline_levels(project_id)["valid"] is True
