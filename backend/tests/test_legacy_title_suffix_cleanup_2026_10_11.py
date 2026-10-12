"""遗留目录标题冗余后缀清理回归测试（2026-10-11）。

旧版全树去重把「（其2）（其3）…（其7）」式无意义后缀写进了库里的标题，
这些后缀不是内容（Word 自动目录只是把它们渲染成二级条目），因此启动时
做一次幂等清理即可。覆盖：

- sections.title / feasibility_outline_nodes.title（单行字符串）
- v51_tasks 的 level1 / level2 / final / edited outline JSON（断点续生状态）
- project_templates.outline_json（已存模板）

同时保证：清理不丢行、不改动真实内容、不碰非标题字段、坏 JSON 静默跳过。
"""

import json

from app.db import get_db
from app.db_migrations import (
    _strip_suffix_tree,
    clean_legacy_outline_title_suffixes,
)
from app.services.ai.outline_common import _strip_legacy_redundant_suffix


def _seed_project(name: str) -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO projects (name, industry) VALUES (?, ?)", (name, "市政")
        )
        return int(cur.lastrowid)


def _seed_section_title(project_id: int, title: str) -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO sections (project_id, title, level, sort_order) VALUES (?, ?, ?, ?)",
            (project_id, title, 3, 1),
        )
        return int(cur.lastrowid)


def _get_titles(ids: list[int]) -> list[str]:
    marks = ",".join("?" for _ in ids)
    with get_db() as conn:
        return [
            r["title"]
            for r in conn.execute(
                f"SELECT title FROM sections WHERE id IN ({marks}) ORDER BY id", ids
            ).fetchall()
        ]


def _insert_v51_task(project_id: int, raw_json: str) -> None:
    with get_db() as conn:
        conn.execute(
            "INSERT INTO v51_tasks (project_id, phase, current_phase, final_outline_json) "
            "VALUES (?, ?, ?, ?)",
            (project_id, "final", "final", raw_json),
        )


def _read_v51_task(project_id: int) -> str:
    with get_db() as conn:
        return conn.execute(
            "SELECT final_outline_json FROM v51_tasks WHERE project_id = ?", (project_id,)
        ).fetchone()[0]


# ─────────────────────────────────────────────────────────────────────────────
# 1. 单行标题列
# ─────────────────────────────────────────────────────────────────────────────


class TestTextColumnCleanup:
    def test_sections_titles_cleaned_without_losing_rows(self):
        pid = _seed_project("文本列清洗")
        ids = [
            _seed_section_title(pid, "砌体成品保护"),
            _seed_section_title(pid, "砌体成品保护（其2）"),
            _seed_section_title(pid, "砌体成品保护（其3）（其4）"),
        ]

        with get_db() as conn:
            stats = clean_legacy_outline_title_suffixes(conn)

        assert stats.get("sections", 0) >= 2
        # 只剥离后缀，不丢行、不改真实内容
        assert _get_titles(ids) == ["砌体成品保护"] * 3

    def test_idempotent(self):
        pid = _seed_project("幂等")
        sid = _seed_section_title(pid, "施工组织设计（其2）")

        with get_db() as conn:
            clean_legacy_outline_title_suffixes(conn)
            second_pass = clean_legacy_outline_title_suffixes(conn)

        assert _get_titles([sid]) == ["施工组织设计"]
        # 第二次运行无可清理项
        assert second_pass.get("sections", 0) == 0

    def test_feasibility_nodes_cleaned(self):
        _seed_project("可研节点")
        with get_db() as conn:
            conn.execute(
                "INSERT INTO feasibility_outline_nodes "
                "(node_id, parent_node_id, sort_order, level, title) "
                "VALUES (?, ?, ?, ?, ?)",
                ("n1", None, 1, 3, "成品保护措施（其2）"),
            )
            clean_legacy_outline_title_suffixes(conn)
            row = conn.execute(
                "SELECT title FROM feasibility_outline_nodes WHERE node_id = 'n1'"
            ).fetchone()
        assert row["title"] == "成品保护措施"


# ─────────────────────────────────────────────────────────────────────────────
# 2. JSON 目录树列
# ─────────────────────────────────────────────────────────────────────────────


class TestJsonTreeCleanup:
    def test_v51_tasks_outline_json_cleaned_and_cross_parent_kept(self):
        pid = _seed_project("V51清洗")
        outline = [
            {
                "title": "总体实施策划",
                "children": [
                    {"title": "施工组织设计", "children": []},
                    {"title": "施工组织设计（其2）", "children": []},
                ],
            },
            {
                "title": "技术保障措施",
                "children": [{"title": "施工组织设计（其3）（其4）", "children": []}],
            },
        ]
        _insert_v51_task(pid, json.dumps(outline, ensure_ascii=False))

        with get_db() as conn:
            stats = clean_legacy_outline_title_suffixes(conn)

        assert stats.get("v51_tasks", 0) == 1
        stored = json.loads(_read_v51_task(pid))
        assert [c["title"] for c in stored[0]["children"]] == ["施工组织设计", "施工组织设计"]
        assert [c["title"] for c in stored[1]["children"]] == ["施工组织设计"]
        # 跨父同名保留（不能删内容），只是后缀被剥离

    def test_project_templates_outline_json_cleaned(self):
        pid = _seed_project("模板清洗")
        outline = [
            {"title": "项目概述", "children": [{"title": "工程概况（其2）", "children": []}]}
        ]
        with get_db() as conn:
            cur = conn.execute(
                "INSERT INTO project_templates (project_id, filename, outline_json) "
                "VALUES (?, ?, ?)",
                (pid, "模板.docx", json.dumps(outline, ensure_ascii=False)),
            )
            tid = int(cur.lastrowid)
            clean_legacy_outline_title_suffixes(conn)
            raw = conn.execute(
                "SELECT outline_json FROM project_templates WHERE id = ?", (tid,)
            ).fetchone()[0]
        assert json.loads(raw)[0]["children"][0]["title"] == "工程概况"

    def test_malformed_json_left_alone(self):
        pid = _seed_project("坏JSON")
        _insert_v51_task(pid, "{not json")

        with get_db() as conn:
            stats = clean_legacy_outline_title_suffixes(conn)

        assert stats.get("v51_tasks", 0) == 0
        assert _read_v51_task(pid) == "{not json"


# ─────────────────────────────────────────────────────────────────────────────
# 3. 树形剥离器本身
# ─────────────────────────────────────────────────────────────────────────────


def test_strip_suffix_tree_preserves_structure_and_other_fields():
    tree = [
        {
            "title": "总体实施策划（其2）",
            "name": "总体实施策划（其3）",
            "id": "1",
            "children": [
                {"title": "施工组织设计（其2）（其4）", "note": "（其5）"}
            ],
        }
    ]
    assert _strip_suffix_tree(tree, _strip_legacy_redundant_suffix) is True
    assert tree[0]["title"] == "总体实施策划"
    assert tree[0]["name"] == "总体实施策划"
    assert tree[0]["id"] == "1"
    assert tree[0]["children"][0]["title"] == "施工组织设计"
    # 非标题字段不参与清洗
    assert tree[0]["children"][0]["note"] == "（其5）"


def test_strip_suffix_tree_reports_no_change_when_already_clean():
    tree = [{"title": "施工组织设计", "children": []}]
    assert _strip_suffix_tree(tree, _strip_legacy_redundant_suffix) is False

