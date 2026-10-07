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
    # V4.0 规则：标题必须为纯文本（编号由系统自动生成，严禁数字前缀）
    items = [
        {"title": "项目概述", "level": 1, "children": [
            {"title": "编制依据", "level": 2, "children": [
                {"title": "相关标准", "level": 3, "children": []},
                {"title": "项目资料", "level": 3, "children": []},
            ]},
            {"title": "工程概况", "level": 2, "children": [
                {"title": "建设规模", "level": 3, "children": []},
                {"title": "建设条件", "level": 3, "children": []},
            ]},
            {"title": "目标承诺", "level": 2, "children": [
                {"title": "质量目标", "level": 3, "children": []},
                {"title": "工期目标", "level": 3, "children": []},
            ]},
        ]},
    ]
    ok, _ = validate_outline_business(items)
    assert ok is True


def test_validate_outline_business_no_level3():
    """V4.0：每个二级下至少 2 个三级节点（无三级则校验失败）"""
    items = [
        {"title": "施工部署", "level": 1, "children": [
            {"title": "总体流程", "level": 2, "children": []},
            {"title": "场地布置", "level": 2, "children": []},
            {"title": "资源计划", "level": 2, "children": []},
        ]},
    ]
    ok, msg = validate_outline_business(items)
    assert ok is False
    assert "三级目录不足" in msg


def test_validate_outline_business_number_prefix():
    """V4.0：标题严禁出现数字前缀（编号由系统自动生成）"""
    items = [{"title": "1、项目概述", "level": 1, "children": [
        {"title": "编制依据", "level": 2, "children": []},
        {"title": "工程概况", "level": 2, "children": []},
    ]}]
    ok, msg = validate_outline_business(items)
    assert ok is False
    assert "编号前缀" in msg


def test_validate_outline_business_duplicate_title():
    """V4.0：全局重复标题检测"""
    items = [
        {"title": "质量管理", "level": 1, "children": [
            {"title": "体系建立", "level": 2, "children": [
                {"title": "组织架构", "level": 3, "children": []},
                {"title": "制度文件", "level": 3, "children": []},
            ]},
            {"title": "过程控制", "level": 2, "children": [
                {"title": "检查流程", "level": 3, "children": []},
                {"title": "整改闭环", "level": 3, "children": []},
            ]},
        ]},
        {"title": "质量管理", "level": 1, "children": [
            {"title": "验收标准", "level": 2, "children": [
                {"title": "检验批划分", "level": 3, "children": []},
                {"title": "合格标准", "level": 3, "children": []},
            ]},
            {"title": "资料归档", "level": 2, "children": [
                {"title": "归档范围", "level": 3, "children": []},
                {"title": "移交要求", "level": 3, "children": []},
            ]},
        ]},
    ]
    ok, msg = validate_outline_business(items)
    assert ok is False
    assert "重复" in msg


def test_validate_outline_business_min_children():
    """V4.0：每个一级下至少 2 个二级节点"""
    items = [{"title": "安全文明施工", "level": 1, "children": [
        {"title": "安全制度", "level": 2, "children": []},
    ]}]
    ok, msg = validate_outline_business(items)
    assert ok is False
    assert "二级目录不足" in msg


def test_validate_outline_business_invalid():
    """空标题的目录项校验不通过"""
    items = [{"title": "", "level": 1}]
    ok, msg = validate_outline_business(items)
    assert ok is False
