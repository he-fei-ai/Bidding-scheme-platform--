"""V5.1 目录→图表模块数据链修复验证（2026-10-08）。

修复点：目录生成阶段由 AI 标注的 graph_suitable / suggested_chart 此前只存在于内存树，
落库时被整条丢弃（INSERT 不含这两列），且 build_tree 读链也不含 —— 跨模块数据传递丢失。
现：建表补列 + INSERT 落库 + saved_node 透出 + build_tree 读回。

本测试验证：带图表预测信号的目录树经 _clear_and_save_outline 落库后，
build_tree 仍能读回 graph_suitable / suggested_chart（跨模块数据不再丢失）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import get_db  # noqa: E402
from app.routers.sse import sse_db_ops  # noqa: E402
from app.services.sections import build_tree  # noqa: E402


def _make_project(conn) -> int:
    cur = conn.execute(
        "INSERT INTO projects (name, industry) VALUES (?, ?)", ("图表链路测试", "市政")
    )
    return int(cur.lastrowid)


def test_outline_chart_fields_persist_and_read_back():
    with get_db() as conn:
        pid = _make_project(conn)

    outline = [
        {
            "title": "第一章 总体实施方案",
            "graph_suitable": True,
            "suggested_chart": "flowchart,gantt",
            "children": [
                {
                    "title": "安全管理机构",
                    "graph_suitable": False,
                    "suggested_chart": "",
                    "children": [],
                }
            ],
        }
    ]

    sse_db_ops._clear_and_save_outline(pid, outline)

    tree = build_tree(pid)
    assert tree, "目录树未落库"
    root = tree[0]
    assert root.graph_suitable is True, "graph_suitable 未落库/读回"
    assert root.suggested_chart == "flowchart,gantt", "suggested_chart 未落库/读回"
    child = root.children[0]
    assert child.graph_suitable is False
    assert child.suggested_chart == ""


def test_outline_chart_fields_default_when_absent():
    """上游未给图表信号时，默认值不应破坏落库（graph_suitable=0 / suggested_chart=''）。"""
    with get_db() as conn:
        pid = _make_project(conn)

    outline = [{"title": "第二章 质量管理", "children": []}]
    sse_db_ops._clear_and_save_outline(pid, outline)

    tree = build_tree(pid)
    assert tree[0].graph_suitable is False
    assert tree[0].suggested_chart == ""
