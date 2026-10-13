"""章节编写模块 —— 中间插入净化兄弟正文编号（BUG-FIX 2026-10-13 · B2/B2-2）。

背景：
  `create_section` 在客户端显式传 `sort_order`（中间插入）时，后续兄弟的
  DB 派生编号（`get_section_path` 按「排序后位置索引」现算）会整体后移，
  但它们正文里 AI 写下的内嵌编号仍指向旧位置 → 文档出现「H2 是三、xxx，
  其下却挂 2.1」的编号不一致。修复：对「因本次插入而真的后移」的兄弟调用
  `_strip_body_heading_numbering` 剥掉内嵌编号（只剥标题编号、保留标题正文）。

B2-2（本轮新修 · 过度剥离）：
  `get_section_path` 的兄弟排序键是 `(sort_order, created_at)` **升序**，
  新章节 `created_at` 最新 → 它排在所有**同 sort_order** 兄弟之后、仅排在
  **严格更大** sort_order 兄弟之前。故真正后移（+1）的只有 sort_order
  **严格大于**插入值的兄弟；**同值兄弟并不移动，其内嵌编号仍然有效**。
  原实现用 `COALESCE(sort_order,0) >= ?` 把同值兄弟一并剥号（探针实证：
  插到 sort_order=2 时乙路径仍为 2，`## 2 乙…` 却被剥成 `## 乙…`），反而
  制造编号不一致。已改为 `> ?` 精准命中真正后移的兄弟。

本测试固化三条不变量：
  1) 显式 sort_order 中间插入 → 仅**严格更大** sort_order 的兄弟被剥号；
  2) **同值**兄弟（未移动）与**更小** sort_order 兄弟的编号原样保留；
  3) 尾部追加（缺省 sort_order）→ 不做任何剥离（无谓剥离会误伤合法编号）。
并含 AST/源码守卫：`create_section` 必须接入净化助手，且谓词必须为 `>`。
"""
from __future__ import annotations

import ast
from pathlib import Path

from app.db import get_db, init_db
from app.models import SectionCreate
from app.services import sections as S
from app.services.sections import get_section_path


_BACKEND = Path(__file__).resolve().parents[1]


def _proj(name: str) -> int:
    init_db()
    with get_db() as conn:
        return conn.execute(
            "INSERT INTO projects (name, industry) VALUES (?, '通用')", (name,)
        ).lastrowid


def _set_content(sid: int, content: str) -> None:
    with get_db() as conn:
        conn.execute("UPDATE sections SET content=? WHERE id=?", (content, sid))


def _content(sid: int) -> str:
    with get_db() as conn:
        r = conn.execute("SELECT content FROM sections WHERE id=?", (sid,)).fetchone()
        return r["content"] if r else ""


class TestMidInsertStripsOnlyShiftedSiblings:
    """显式 sort_order 中间插入：只剥「真正后移」的兄弟。"""

    def test_tied_sibling_kept_strictly_greater_stripped(self):
        """插到 sort_order=1（与甲同值）→ 甲不移动保留编号；乙/丙后移被剥。"""
        pid = _proj("b2-tie")
        a = S.create_section(pid, SectionCreate(title="甲"))
        b = S.create_section(pid, SectionCreate(title="乙"))
        c = S.create_section(pid, SectionCreate(title="丙"))
        _set_content(a.id, "## 1 甲正文")
        _set_content(b.id, "## 2 乙正文")
        _set_content(c.id, "## 3 丙正文")

        x = S.create_section(pid, SectionCreate(title="插入", sort_order=1))

        # 位置：甲 1（未动）、插入 2、乙 3、丙 4
        assert get_section_path(a.id) == "1"
        assert get_section_path(x.id) == "2"
        assert get_section_path(b.id) == "3"
        assert get_section_path(c.id) == "4"
        # 甲与插入点同值、未移动 → 内嵌编号必须保留
        assert _content(a.id) == "## 1 甲正文", "同值未移动的甲被过度剥离"
        # 乙/丙严格更大、真的后移 → 内嵌编号被剥（保留标题正文）
        assert _content(b.id) == "## 乙正文"
        assert _content(c.id) == "## 丙正文"

    def test_insert_in_middle_unique_gap(self):
        """唯一插入点（与任何兄弟都不同值）→ 严格更大的兄弟被剥。"""
        pid = _proj("b2-gap")
        a = S.create_section(pid, SectionCreate(title="甲", sort_order=1))
        b = S.create_section(pid, SectionCreate(title="乙", sort_order=5))
        _set_content(a.id, "## 1 甲正文")
        _set_content(b.id, "## 5 乙正文")

        # 插到 3（1 与 5 之间的空档，唯一）→ 乙(5>3) 后移被剥；甲(1<3) 不动
        x = S.create_section(pid, SectionCreate(title="插入", sort_order=3))
        assert get_section_path(a.id) == "1", "更小 sort_order 的甲不应移动"
        assert get_section_path(x.id) == "2"
        assert get_section_path(b.id) == "3"
        assert _content(a.id) == "## 1 甲正文"
        assert _content(b.id) == "## 乙正文"

    def test_child_insert_scoped_to_same_parent(self):
        """子级中间插入只影响同父兄弟，父级与其他子树不受波及。"""
        pid = _proj("b2-child")
        p1 = S.create_section(pid, SectionCreate(title="第一章"))
        p2 = S.create_section(pid, SectionCreate(title="第二章"))
        c1 = S.create_section(pid, SectionCreate(title="1.1", parent_id=p1.id))
        c2 = S.create_section(pid, SectionCreate(title="1.2", parent_id=p1.id))
        _set_content(c1.id, "## 1 子一")
        _set_content(c2.id, "## 2 子二")
        _set_content(p2.id, "## 2 第二章正文")

        # 在第一章下、sort_order=1（与 1.1 同值）插入 → 只动第一章的子级
        S.create_section(pid, SectionCreate(title="1.0", parent_id=p1.id, sort_order=1))
        assert _content(c1.id) == "## 1 子一", "同值未移动的 1.1 不应被剥"
        assert _content(c2.id) == "## 子二", "同父且后移的 1.2 应被剥"
        assert _content(p2.id) == "## 2 第二章正文", "其他父级章节不受影响"


class TestAppendDoesNotStrip:
    """尾部追加（缺省 sort_order）不改变任何兄弟编号 → 不做剥离。"""

    def test_append_keeps_sibling_numbering(self):
        pid = _proj("b2-append")
        a = S.create_section(pid, SectionCreate(title="甲"))
        b = S.create_section(pid, SectionCreate(title="乙"))
        _set_content(a.id, "## 1 甲正文")
        _set_content(b.id, "## 2 乙正文")

        S.create_section(pid, SectionCreate(title="丙"))  # 追加，无 sort_order

        assert get_section_path(a.id) == "1"
        assert get_section_path(b.id) == "2"
        assert _content(a.id) == "## 1 甲正文", "追加不应误剥既有兄弟编号"
        assert _content(b.id) == "## 2 乙正文", "追加不应误剥既有兄弟编号"


class TestB2SourceGuards:
    """源码/AST 守卫：create_section 必须接入净化，且谓词为严格大于。"""

    def test_create_section_calls_helper(self):
        """原守卫只检查 update/move/reorder，漏掉 10-13 新增的 create_section。"""
        src = (_BACKEND / "app" / "services" / "sections.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fns = {
            n.name: n
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef)
            and n.name in {"create_section", "update_section", "move_section", "reorder_sections"}
        }
        for fname in ("create_section", "update_section", "move_section", "reorder_sections"):
            fn = fns.get(fname)
            assert fn is not None, f"未找到 {fname}"
            calls = [
                n
                for n in ast.walk(fn)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == "_strip_body_heading_numbering"
            ]
            assert calls, f"{fname} 未调用 _strip_body_heading_numbering（正文编号会残留）"

    def test_create_section_uses_strict_greater_predicate(self):
        """锁定 B2-2 修复：中间插入的兄弟选取必须是 `> ?`，不得回退到 `>= ?`。

        `>=` 会把「同 sort_order 但未移动」的兄弟一并剥号（制造编号不一致）。
        """
        src = (_BACKEND / "app" / "services" / "sections.py").read_text(encoding="utf-8")
        i = src.index("def create_section(")
        seg = src[i : i + 2600]
        assert "COALESCE(sort_order, 0) > ?" in seg, (
            "create_section 中间插入必须用严格大于 `>` 选取真正后移的兄弟"
        )
        assert "COALESCE(sort_order, 0) >= ?" not in seg, (
            "create_section 不得用 `>=`（会过度剥离同值未移动的兄弟）"
        )
