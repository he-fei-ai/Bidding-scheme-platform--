from app.models import ExportConfig, SectionCreate, SectionTreeNode


def test_section_create_defaults():
    data = SectionCreate(title="测试章节", parent_id=None, level=1, sort_order=1)
    assert data.title == "测试章节"
    assert data.parent_id is None
    assert data.level == 1
    assert data.sort_order == 1


def test_section_create_with_children():
    child = SectionCreate(title="子章节", parent_id=1, level=2, sort_order=1)
    assert child.parent_id == 1
    assert child.level == 2


def test_section_tree_node_defaults():
    node = SectionTreeNode(
        id=1, title="根节点", level=1, sort_order=1,
        section_type="text", status="pending",
    )
    assert node.id == 1
    assert node.title == "根节点"
    assert node.children == []


def test_export_config_defaults():
    config = ExportConfig(font_body="宋体", size_body=12)
    assert config.font_body == "宋体"
    assert config.line_spacing == 1.5
