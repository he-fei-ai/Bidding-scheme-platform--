"""行业树版本回滚测试

验证 rollback_to_version 能够递归恢复多级子节点，而非仅恢复顶层节点。
使用临时数据库文件隔离，避免污染真实的 bid_assistant.sqlite3。
"""

import pytest

from app import db as db_module
from app.services import industry_tree as it


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    """将数据库指向临时文件并初始化 schema，测试结束后自动丢弃。"""
    tmp_db = tmp_path / "test_rollback.sqlite3"
    # _get_connection() 在调用时读取 db_module.DATABASE_PATH，故 patch 此处即可隔离
    monkeypatch.setattr(db_module, "DATABASE_PATH", tmp_db)
    # 清空列名缓存，避免复用其它库的 PRAGMA 结果
    monkeypatch.setattr(db_module, "_TABLE_COLUMNS", {})
    # 重置连接池，确保不复用旧连接（尤其跨测试时 DATABASE_PATH 已变化）
    monkeypatch.setattr(db_module, "_pool", db_module.DBPool(max_connections=16))
    db_module.init_db()
    yield tmp_db


def _build_three_level_tree():
    """创建 根 -> 子 -> 孙 三级结构，返回各节点 id。"""
    root = it.create_node("建筑工程", parent_id=None, level=1, description="根节点", order_index=5)
    child = it.create_node("施工组织设计", parent_id=root["id"], description="二级", order_index=2)
    grandchild = it.create_node("临时设施布置", parent_id=child["id"], description="三级")
    return root["id"], child["id"], grandchild["id"]


def test_rollback_restores_all_descendants(isolated_db):
    root_id, child_id, grandchild_id = _build_three_level_tree()

    # 快照前确认结构完整
    tree_before = it.get_full_tree()
    assert len(tree_before) == 1
    assert tree_before[0]["id"] == root_id
    assert len(tree_before[0]["children"]) == 1
    assert tree_before[0]["children"][0]["id"] == child_id
    assert len(tree_before[0]["children"][0]["children"]) == 1
    assert tree_before[0]["children"][0]["children"][0]["id"] == grandchild_id

    # 步骤2：创建版本快照
    version = it.create_version("初始三级结构")
    assert version == 1

    # 步骤3：修改（改名）+ 删除（删除子节点，级联删除孙节点）
    it.update_node(root_id, {"name": "已改名的根"})
    it.create_node("临时新增根", parent_id=None, level=1)
    it.delete_node(child_id, force=True)  # 级联删除 grandchild

    # 确认破坏生效：孙节点已丢失，根被改名，多了一个新根
    assert it.get_node_by_id(grandchild_id) is None
    assert it.get_node_by_id(root_id)["name"] == "已改名的根"
    assert len(it.get_full_tree()) == 2

    # 步骤4：回滚
    ok = it.rollback_to_version(version)
    assert ok is True

    # 步骤5：查询数据库确认完整结构恢复
    tree_after = it.get_full_tree()
    assert len(tree_after) == 1, "回滚后应只剩快照中的单一根节点"

    restored_root = tree_after[0]
    assert restored_root["id"] == root_id
    assert restored_root["name"] == "建筑工程", "根节点名称应恢复为快照值"
    assert restored_root["order_index"] == 5, "order_index 应被正确恢复"

    # 关键：二级子节点必须恢复
    assert len(restored_root["children"]) == 1
    restored_child = restored_root["children"][0]
    assert restored_child["id"] == child_id
    assert restored_child["parent_id"] == root_id

    # 关键：三级孙节点必须恢复（原 BUG 会在此丢失）
    assert len(restored_child["children"]) == 1
    restored_grandchild = restored_child["children"][0]
    assert restored_grandchild["id"] == grandchild_id
    assert restored_grandchild["parent_id"] == child_id
    assert restored_grandchild["level"] == 3


def test_rollback_missing_version_returns_false(isolated_db):
    assert it.rollback_to_version(999) is False


def test_rollback_node_count_matches_snapshot(isolated_db):
    """回滚后节点总数应与快照时完全一致（含所有层级）。"""
    _build_three_level_tree()
    version = it.create_version("snapshot")

    # 打乱：新增两个节点
    extra = it.create_node("多余节点A", parent_id=None, level=1)
    it.create_node("多余节点B", parent_id=extra["id"])

    with db_module.get_db() as conn:
        count_before_rollback = conn.execute("SELECT COUNT(*) FROM industry_tree").fetchone()[0]
    assert count_before_rollback == 5

    it.rollback_to_version(version)

    with db_module.get_db() as conn:
        count_after = conn.execute("SELECT COUNT(*) FROM industry_tree").fetchone()[0]
    assert count_after == 3, "回滚后应精确恢复快照中的 3 个节点（根/子/孙）"
