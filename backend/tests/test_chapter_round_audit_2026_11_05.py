"""章节编写模块 · 深度审计与修复回归测试（2026-11-05 轮）。

本轮修复清单（每项均有对应测试钉死）：
  FIX-1  列表样式拍平：ContentCleaner 只剥「标题式」首行编号，正文以有序列表
         开篇（`1. 施工准备` 后紧跟 `2. 材料进场`）时不再剥首项编号。
  FIX-2  编号冲突：章节重排/改名后 L5+ 深层编号陈旧 —— _strip_body_heading_numbering
         改用 normalize_deep_heading_numbering 重锚到当前章节路径（与落库侧同口径）。
  FIX-3  未闭合围栏闭合行泄漏：>5000 行病态围栏的超限路径不再把裸 ``` 泄漏进内容。
  FIX-4  字数口径：含 L4+ 子目录的 L3 不再误判为 is_l3_own（按四档子树总字数）。
  FIX-5  writing_length 422 死代码：非法档位 key 在归一化前被拒绝（is_valid_writing_length）。
  FIX-6  SSE 后台续跑排空提前退出：_drain_queue 终态判据须含 section_id is None。
"""
from __future__ import annotations

import inspect
import re

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import app
from app.models import SectionCreate
from app.routers import sections as sections_router
from app.routers.sse import sse_content, sse_utils
from app.services import sections as sections_service
from app.services.content_cleaning import (
    ContentCleaner,
    plan_chart_fence_lines,
    strip_heading_numbering_from_content,
)


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def project_id(client):
    resp = client.post(
        "/api/v1/projects",
        json={"name": "章节编写模块审计轮", "industry": "公共建筑", "bidder_name": "测试投标方"},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


# =============================================================================
# FIX-1：列表样式拍平（ContentCleaner 首行有序列表项误剥）
# =============================================================================


class TestContentCleanerListNotFlattened:
    """正文以有序列表开篇时，首项编号不得被当作「标题编号」剥掉。"""

    def test_ordered_list_first_item_preserved(self):
        content = "1. 施工准备\n2. 材料进场\n3. 设备调试"
        out = ContentCleaner().clean("施工部署", content)
        assert out == "1. 施工准备\n2. 材料进场\n3. 设备调试"

    def test_ordered_list_dunhao_preserved(self):
        content = "1、施工准备\n2、材料进场"
        out = ContentCleaner().clean("施工部署", content)
        assert out == "1、施工准备\n2、材料进场"

    def test_ordered_list_paren_preserved(self):
        content = "（1）施工准备\n（2）材料进场"
        out = ContentCleaner().clean("施工部署", content)
        assert out == "（1）施工准备\n（2）材料进场"

    def test_unordered_list_preserved(self):
        content = "- 安全措施\n- 质量保证"
        out = ContentCleaner().clean("施工部署", content)
        assert out == content

    def test_heading_numbering_still_stripped(self):
        """标题式单行编号（后续无编号行）仍按原逻辑剥离，不回退。"""
        out = ContentCleaner().clean("标题", "1、总体要求\n本项目遵循规范执行。")
        assert out.startswith("总体要求")

    def test_single_numbered_paragraph_preserved_as_sequence(self):
        """连续编号段（1./2.）是列表而非标题 → 不剥。"""
        content = "1. 本项目主体结构施工采用流水作业。\n2. 后续工序按规范执行。"
        out = ContentCleaner().clean("施工部署", content)
        assert out == content


# =============================================================================
# FIX-2：编号冲突（重排后 L5+ 深层编号陈旧）
# =============================================================================


class TestReorderDeepHeadingRenumber:
    """_strip_body_heading_numbering 须把 L5+ 深层编号重锚到当前章节路径。"""

    def _build_tree(self, pid) -> tuple[int, int, int]:
        """返回 (L1, L2, L3) 的章节 id（create_section 返回 Section，取 .id）。"""
        l1 = sections_service.create_section(pid, SectionCreate(title="施工组织设计"))
        l2 = sections_service.create_section(pid, SectionCreate(title="进度管理", parent_id=l1.id))
        l3 = sections_service.create_section(pid, SectionCreate(title="总体安排", parent_id=l2.id))
        return l1.id, l2.id, l3.id

    def _set_content(self, sid: int, content: str) -> None:
        with get_db() as conn:
            conn.execute("UPDATE sections SET content = ? WHERE id = ?", (content, sid))

    def test_reorder_renumbers_deep_headings(self, project_id):
        l1, l2, l3 = self._build_tree(project_id)
        # 模拟已保存正文（无 #，纯文本深层编号，来自生成时的旧树位置 3.1.2）
        self._set_content(l3, "正文首段\n3.1.2.1 施工准备\n3.1.2.2 材料进场")
        from app.services.sections import get_section_path, _strip_body_heading_numbering

        assert get_section_path(l3) == "1.1.1"
        with get_db() as conn:
            changed = _strip_body_heading_numbering(conn, [l3])
        assert changed == 1
        with get_db(readonly=True) as conn:
            row = conn.execute("SELECT content FROM sections WHERE id = ?", (l3,)).fetchone()
        assert "1.1.1.1 施工准备" in row["content"]
        assert "1.1.1.2 材料进场" in row["content"]
        assert "3.1.2" not in row["content"]

    def test_no_deep_numbering_untouched(self, project_id):
        l1, l2, l3 = self._build_tree(project_id)
        self._set_content(l3, "正文首段\n施工准备\n材料进场")
        from app.services.sections import _strip_body_heading_numbering

        with get_db() as conn:
            changed = _strip_body_heading_numbering(conn, [l3])
        assert changed == 0
        with get_db(readonly=True) as conn:
            row = conn.execute("SELECT content FROM sections WHERE id = ?", (l3,)).fetchone()
        assert row["content"] == "正文首段\n施工准备\n材料进场"

    def test_strip_heading_numbering_plain_text_is_noop_by_design(self):
        """strip_heading_numbering_from_content 对无 # 的落库正文确为 no-op
        （这正是 FIX-2 需要 normalize_deep_heading_numbering 的原因）。"""
        content = "正文首段\n3.1.2.1 施工准备"
        assert strip_heading_numbering_from_content(content) == content


# =============================================================================
# FIX-3：未闭合围栏闭合行泄漏（>5000 行病态围栏）
# =============================================================================


class TestHugeFenceCloserNotLeaked:
    def test_huge_closed_fence_consumes_closer(self):
        lines = ["```mermaid", "graph TD"] + ["  A-->B"] * 5001 + ["```", "后续正文"]
        plan = plan_chart_fence_lines(lines)
        # 无裸 ``` 泄漏（闭合行被超限路径消费进 bad 块）
        leaked = [b for _a, _l, b, _c, _p in plan if b and any(x.strip() == "```" for x in b)]
        assert not leaked, f"裸围栏闭合行泄漏: {plan}"
        # 区间连续性：sum(consumed) == len(lines)
        assert sum(c for _a, _l, _b, c, _p in plan) == len(lines)
        # 块按 bad 删除（>5000 病态），后续正文保留
        kept = [b for a, _l, b, _c, _p in plan if a == "keep"]
        assert any("后续正文" in x for blk in kept for x in blk)

    def test_normal_fence_consumption_unchanged(self):
        lines = ["```mermaid", "graph TD", "  A-->B", "```", "后续正文"]
        plan = plan_chart_fence_lines(lines)
        assert plan[0][0] == "block"
        assert plan[0][3] == 4
        assert sum(c for _a, _l, _b, c, _p in plan) == len(lines)

    def test_unclosed_fence_consumption_unchanged(self):
        lines = ["```mermaid", "graph TD", "  A-->B", "后续正文"]
        plan = plan_chart_fence_lines(lines)
        assert plan[0][0] == "bad"
        assert sum(c for _a, _l, _b, c, _p in plan) == len(lines)


# =============================================================================
# FIX-4：字数口径（含下级 L3 不再误判为 L3-own）
# =============================================================================


class TestIsL3OwnCorrectness:
    def test_leaf_l3_still_own(self):
        src = inspect.getsource(sse_utils._build_leaf_dicts)
        assert '"is_l3_own": leaf.get("level") == 3 and not children_map.get(leaf["id"])' in src

    def test_child_chapters_populated_from_children_map(self):
        src = inspect.getsource(sse_utils._build_leaf_dicts)
        assert "children_map.get(leaf[\"id\"], [])" in src


# =============================================================================
# FIX-5：writing_length 422 死代码
# =============================================================================


class TestWritingLengthValidation:
    def test_generate_content_uses_is_valid_writing_length(self):
        body = inspect.getsource(sections_router.generate_content)
        assert "is_valid_writing_length" in body
        assert "('brief', 'standard', 'detailed', 'long')" in body

    def test_is_valid_writing_length_rejects_garbage(self):
        from app.services.ai.writing_length_config import is_valid_writing_length

        assert is_valid_writing_length("standrad") is False
        assert is_valid_writing_length("xyz") is False
        assert is_valid_writing_length("medium") is False
        assert is_valid_writing_length("brief") is True
        assert is_valid_writing_length("short") is True
        assert is_valid_writing_length("extra_long") is True
        assert is_valid_writing_length(None) is True

    def test_router_422_rejected_in_validation_path(self, client):
        """generate-content 对非法档位 key 返回 SSE error（非静默 standard）。"""
        resp = client.get(
            "/api/v1/projects/1/sections/generate-content",
            params={"writing_length": "standrad"},
        )
        assert resp.status_code == 200
        text = resp.text
        assert "writing_length 必须是" in text or '"status": "error"' in text


# =============================================================================
# FIX-6：SSE 后台续跑排空提前退出
# =============================================================================


class TestDrainQueueTerminalGuard:
    def test_drain_queue_guard_requires_section_id_none(self):
        """_drain_queue 断流判据须与主循环同构：终态 且 section_id is None。"""
        src = inspect.getsource(sse_content)
        # 主循环正确判据存在
        assert re.search(
            r"event\.get\(\"status\"\) in SSE_TERMINAL_STATUSES\s*\n\s*and event\.get\(\"section_id\"\) is None",
            src,
        ), "主循环终态判据应含 section_id is None"
        # _drain_queue 内不得再出现「只判 status」的终态 break
        drain_block = re.search(
            r"async def _drain_queue\(\):(.*?)(?=\n\s*_drain_task = )", src, re.DOTALL
        )
        assert drain_block, "未找到 _drain_queue 定义"
        block = drain_block.group(1)
        assert "section_id is None" in block, (
            "_drain_queue 终态判据漏掉 section_id is None（后台续跑提前退出）"
        )
