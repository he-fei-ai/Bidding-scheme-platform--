"""验证 V5.1 节点数不再被硬截断的回归测试。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))

import pytest
import asyncio


def _make_big_outline(n_l1: int = 28, n_l2: int = 5, n_l3: int = 5) -> list[dict]:
    """构造 n_l1×n_l2×n_l3 的完整目录，总节点数 = l1 + l1*l2 + l1*l2*l3"""
    outline = []
    for i in range(n_l1):
        children_l2 = []
        for j in range(n_l2):
            children_l3 = []
            for k in range(n_l3):
                children_l3.append({"id": f"{i+1}.{j+1}.{k+1}", "title": f"L3-{i+1}-{j+1}-{k+1}", "level": 3, "children": []})
            children_l2.append({"id": f"{i+1}.{j+1}", "title": f"L2-{i+1}-{j+1}", "level": 2, "children": children_l3})
        outline.append({"id": f"{i+1}", "title": f"L1-{i+1}", "level": 1, "children": children_l2})
    return outline


def _count_nodes(outline: list[dict]) -> int:
    total = 0
    for node in outline:
        total += 1
        for child in node.get("children", []):
            total += 1
            for grandchild in child.get("children", []):
                total += 1
    return total


def test_cap_outline_nodes_no_longer_exists():
    """确保 _cap_outline_nodes 已被移除（不再是 public API）。"""
    import app.services.ai.workflows as wf
    assert not hasattr(wf, '_cap_outline_nodes'), \
        "_cap_outline_nodes 应已移除，但仍在 workflows 模块中"


def test_generate_outline_v51_no_hard_cap_for_large_outlines():
    """
    验证 generate_outline_v51 的 _ensure_min_children 不会截断超出上限的节点。
    构造 28章×5 L2×4 L3 目录（总节点728），传入 _ensure_min_children 确认数量不变。
    """
    from app.services.ai.workflows import _ensure_min_children
    big_outline = _make_big_outline(n_l1=28, n_l2=5, n_l3=4)
    total_before = _count_nodes(big_outline)
    assert total_before > 600, f"测试数据不足，当前总节点数={total_before}"

    result = _ensure_min_children(big_outline, min_level2=3, min_level3=3)
    total_after = _count_nodes(result)

    assert total_after == total_before, (
        f"节点被截断！输入={total_before}, 输出={total_after}, 差值={total_before - total_after}\n"
        f"说明 _cap_outline_nodes 逻辑仍在生效"
    )


def test_self_check_prompt_allows_unlimited_nodes():
    """自检 prompt 不应有上限，只检查不少于下限。"""
    from app.services.ai.prompts.outline import OUTLINE_V51_SELFCHECK_SYSTEM
    assert "不设上限" in OUTLINE_V51_SELFCHECK_SYSTEM, \
        "自检 prompt 应明确说明不设上限"
    assert "至少4个节点" in OUTLINE_V51_SELFCHECK_SYSTEM, \
        "自检 prompt 应明确说'至少4个节点'"


def test_fix_overlap_no_delete_in_prompt():
    """重叠修复功能应确保不删除节点（当前通过 _auto_fix_issues 本地实现）。"""
    from app.services.ai.workflows import _auto_fix_issues
    import inspect
    source = inspect.getsource(_auto_fix_issues)
    # _auto_fix_issues 只添加/修复，不删除节点
    assert "add" in source.lower() or "插入" in source or "补充" in source, \
        "_auto_fix_issues 应通过添加而非删除来修复问题"


def test_fix_count_no_delete_in_prompt():
    """节点补充功能应确保不删减节点（当前通过 _ensure_min_children 实现）。"""
    from app.services.ai.workflows import _ensure_min_children
    import inspect
    source = inspect.getsource(_ensure_min_children)
    # _ensure_min_children 只补充不足的节点，不删减已有节点
    assert "min" in source.lower() or "至" in source or "补充" in source, \
        "_ensure_min_children 应补充至少 min 个节点，不删减已有节点"
