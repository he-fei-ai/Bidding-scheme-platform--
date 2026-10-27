import json

from app.services.ai.workflows import (
    validate_outline_business,
)


def _renumber_items(items: list[dict], parent_number: str = "") -> list[dict]:
    result = []
    for i, item in enumerate(items):
        num = f"{parent_number}{i + 1}" if parent_number else str(i + 1)
        item["number"] = num
        item["title"] = f"{num}. {item.get('title', '').lstrip('0123456789.').strip()}"
        children = item.get("children", [])
        if children:
            item["children"] = _renumber_items(children, f"{num}.")
        result.append(item)
    return result


def _flatten_outline_tree(nodes: list[dict], parent_id: int | None = None) -> list[dict]:
    flat = []
    for i, node in enumerate(nodes):
        flat.append({
            "parent_id": parent_id,
            "title": node.get("title", ""),
            "level": node.get("level", 1),
            "sort_order": i + 1,
            "section_type": "text",
            "scoring_point_id": node.get("scoring_point_id"),
        })
        children = node.get("children", [])
        if children:
            flat.extend(_flatten_outline_tree(children, i + 1))
    return flat


def test_renumber_empty():
    assert _renumber_items([]) == []


def test_renumber_single():
    items = [{"title": "概述", "children": []}]
    result = _renumber_items(items)
    assert result[0]["number"] == "1"
    assert "1." in result[0]["title"]


def test_renumber_nested():
    items = [{"title": "第一章", "children": [{"title": "概述", "children": []}]}]
    result = _renumber_items(items)
    assert result[0]["number"] == "1"
    assert result[0]["children"][0]["number"] == "1.1"


def test_flatten_empty():
    assert _flatten_outline_tree([]) == []


def test_flatten_single():
    nodes = [{"title": "概述", "level": 1}]
    flat = _flatten_outline_tree(nodes)
    assert len(flat) == 1
    assert flat[0]["title"] == "概述"


def test_flatten_nested():
    nodes = [
        {"title": "第一章", "level": 1, "children": [
            {"title": "第一节", "level": 2, "children": []},
        ]},
    ]
    flat = _flatten_outline_tree(nodes)
    assert len(flat) == 2


def test_validate_outline_business_valid():
    # 业务契约（2026-10-23 收紧）：一级下 3~5 二级、每二级下 ≥5 三级、深度达标、标题纯文本。
    items = [
        {"title": "项目概述", "level": 1, "children": [
            {"title": "编制依据", "level": 2, "children": [
                {"title": f"标准{k}", "level": 3, "children": []} for k in range(5)
            ]},
            {"title": "工程概况", "level": 2, "children": [
                {"title": f"规模{k}", "level": 3, "children": []} for k in range(5)
            ]},
            {"title": "目标承诺", "level": 2, "children": [
                {"title": f"目标{k}", "level": 3, "children": []} for k in range(5)
            ]},
        ]},
    ]
    ok, _ = validate_outline_business(items)
    assert ok is True


def test_validate_outline_business_no_level3():
    """V4.0：每个二级下至少 5 个三级节点（2026-10-23 起三级 <5 校验失败）"""
    items = [
        {"title": "施工部署", "level": 1, "children": [
            {"title": "总体流程", "level": 2, "children": [
                {"title": "a", "level": 3, "children": []},
            ]},
            {"title": "场地布置", "level": 2, "children": [
                {"title": "b", "level": 3, "children": []},
            ]},
            {"title": "资源计划", "level": 2, "children": [
                {"title": "c", "level": 3, "children": []},
            ]},
        ]},
    ]
    ok, msg = validate_outline_business(items)
    assert ok is False
    assert "三级目录不足" in msg


def test_validate_outline_business_number_prefix():
    """V4.0：标题严禁出现数字前缀（编号由系统自动生成）"""
    items = [{"title": "1、项目概述", "level": 1, "children": [
        {"title": f"编制依据{k}", "level": 2, "children": [
            {"title": f"标准{k}-{m}", "level": 3, "children": []} for m in range(5)
        ]} for k in range(3)
    ]}]
    ok, msg = validate_outline_business(items)
    assert ok is False
    assert "编号前缀" in msg


def test_validate_outline_business_duplicate_title():
    """同级重复标题检测（2026-10-11 起重复判定收窄为同级，此处为一级章同级重复）"""
    items = [
        {"title": "质量管理", "level": 1, "children": [
            {"title": f"体系{k}", "level": 2, "children": [
                {"title": f"项{k}-{m}", "level": 3, "children": []} for m in range(5)
            ]} for k in range(3)
        ]},
        {"title": "质量管理", "level": 1, "children": [
            {"title": f"验收{k}", "level": 2, "children": [
                {"title": f"验{k}-{m}", "level": 3, "children": []} for m in range(5)
            ]} for k in range(3)
        ]},
    ]
    ok, msg = validate_outline_business(items)
    assert ok is False
    assert "重复" in msg


def test_validate_outline_business_min_children():
    """V4.0：每个一级下至少 3 个二级节点（2026-10-23 起 3~5）"""
    items = [{"title": "安全文明施工", "level": 1, "children": [
        {"title": "安全制度", "level": 2, "children": [
            {"title": f"条{k}", "level": 3, "children": []} for k in range(5)
        ]},
    ]}]
    ok, msg = validate_outline_business(items)
    assert ok is False
    assert "二级目录不足" in msg


def test_validate_outline_business_invalid():
    """空标题的目录项校验不通过"""
    items = [{"title": "", "level": 1}]
    ok, msg = validate_outline_business(items)
    assert ok is False
