"""正文深层编号净化开关（2026-10-26 · 遗留问题 1）。

背景：导出端只对 L1~L4 标题重编号，L5+（≥3 段小数）保留 AI 原文。AI 编号
漂移时会与相邻 DB 子章节编号冲突，且旧实现只在结构变更时才净化。

方案：``CONTENT_DEEP_HEADING_RENUMBER_ENABLED``（默认 true）在正文落库前把
深层标题改写为「章节路径 + 相对序号」，段数不变 → parse_content 层级不变
（标题识别不受影响），编号冲突消除；false 时回退历史行为。

关键约束（本测试锁定）：
  - 只动 ≥3 段小数；L4（1.1）及更浅、数量短语、代码围栏一律不动；
  - 幂等：同一内容二次净化结果不变；
  - 落库链路真实生效（``_save_section_to_db``），且关开关后原样保留。
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import app
from app.models import SectionCreate
from app.routers.sse import sse_db_ops
from app.services import sections as sections_service
from app.services.ai.outline_common import _is_dirty_l3_title_deep
from app.services.content_cleaning import (
    has_deep_heading_numbering,
    normalize_deep_heading_numbering,
)


class TestNormalizeHelper:
    def test_deep_headings_anchored_to_section_path(self):
        content = "### 3.2.1 施工准备\n### 4.5.6.7 验收"
        out = normalize_deep_heading_numbering(content, "3.1.2")
        assert out == "### 3.1.2.1 施工准备\n### 3.1.2.2 验收"

    def test_deeper_heading_keeps_relative_depth(self):
        content = "### 4.5.6.7 验收\n### 4.5.6.7.8 复检\n### 4.5.6.9 资料"
        out = normalize_deep_heading_numbering(content, "3.1.2")
        assert out == "### 3.1.2.1 验收\n### 3.1.2.1.1 复检\n### 3.1.2.2 资料"

    def test_plain_numbered_heading_also_rewritten(self):
        # 落库后的 content 没有 # 标记，纯文本深层编号同样需要净化
        out = normalize_deep_heading_numbering("2.7.9 施工准备", "1.2")
        assert out == "1.2.1 施工准备"

    def test_l4_and_shallow_headings_untouched(self):
        content = "## 1.1 一般规定\n1.1 一般规定\n## 3 施工准备"
        assert normalize_deep_heading_numbering(content, "3.1.2") == content

    def test_prose_quantity_not_touched(self):
        content = "3 天内完成移交\n2026 年施工计划\n1.8 米净高满足要求"
        assert normalize_deep_heading_numbering(content, "3.1.2") == content

    def test_code_fence_content_untouched(self):
        content = "```\n3.2.1 伪标题\n```\n### 3.2.1 真标题"
        out = normalize_deep_heading_numbering(content, "3.1.2")
        assert "3.2.1 伪标题" in out
        assert "### 3.1.2.1 真标题" in out

    def test_idempotent(self):
        content = "### 3.2.1 施工准备\n### 4.5.6.7 验收"
        once = normalize_deep_heading_numbering(content, "3.1.2")
        twice = normalize_deep_heading_numbering(once, "3.1.2")
        assert once == twice

    def test_empty_inputs(self):
        assert normalize_deep_heading_numbering("", "3.1.2") == ""
        assert normalize_deep_heading_numbering("### 1.1.1 x", "") == "### 1.1.1 x"
        assert normalize_deep_heading_numbering(None, "3.1.2") is None

    def test_quick_detection_guard(self):
        # 用于跳过无深层编号章节的 get_section_path 全表查询开销
        assert has_deep_heading_numbering("### 3.1.2.1 施工准备")
        assert has_deep_heading_numbering("3.1.2.1 施工准备")
        assert not has_deep_heading_numbering("## 1.1 一般规定")
        assert not has_deep_heading_numbering("3 天内完成移交")
        assert not has_deep_heading_numbering("")


class TestHeadingBodySanitize:
    """2026-10-27 修复：开关只净编号时，标题文本里的占位名/冲突后缀会漏出。"""

    def test_placeholder_heading_body_rewritten(self):
        out = normalize_deep_heading_numbering(
            "### 2.9.4 专项6", "1.1.1", context_title="施工准备"
        )
        body = out.split(" ", 1)[1]
        assert not _is_dirty_l3_title_deep(body), out

    def test_family_variants_all_cleaned(self):
        content = (
            "### 2.9.4 专项6\n"
            "### 2.9.5 总体实施策划（2）\n"
            "### 2.9.6 总体实施策划(2)\n"
            "### 2.9.7 分阶段推进专项13\n"
            "### 2.9.8 内容3"
        )
        out = normalize_deep_heading_numbering(content, "1.1.1", context_title="施工准备")
        bodies = [ln.split(" ", 1)[1] for ln in out.split("\n")]
        assert not [b for b in bodies if _is_dirty_l3_title_deep(b)], bodies

    def test_ordinal_suffix_stripped_keeping_concrete_body(self):
        out = normalize_deep_heading_numbering("### 2.9.4 钢筋绑扎验收（2）", "1.1.1")
        assert out == "### 1.1.1.1 钢筋绑扎验收"

    def test_ordinal_prefix_stripped_keeping_concrete_body(self):
        out = normalize_deep_heading_numbering("### 2.9.4 （2）钢筋绑扎验收", "1.1.1")
        assert out == "### 1.1.1.1 钢筋绑扎验收"

    def test_rewritten_bodies_are_unique(self):
        content = "### 2.9.4 专项6\n### 2.9.5 专项6\n### 2.9.6 专项6"
        out = normalize_deep_heading_numbering(content, "1.1.1", context_title="施工准备")
        bodies = [ln.split(" ", 1)[1] for ln in out.split("\n")]
        assert len(set(bodies)) == 3, bodies

    def test_shallow_heading_body_cleaned_without_renumber(self):
        # 浅层编号由导出端统一重编号，这里只净化文本
        out = normalize_deep_heading_numbering("## 1.1 总体实施策划（2）", "3.1.2")
        assert out.startswith("## 1.1 ")
        assert "（2）" not in out
        assert not _is_dirty_l3_title_deep(out.split(" ", 2)[2])

    def test_body_sanitize_idempotent(self):
        content = "### 2.9.4 专项6\n### 2.9.5 钢筋绑扎验收（2）"
        once = normalize_deep_heading_numbering(content, "1.1.1", context_title="施工准备")
        twice = normalize_deep_heading_numbering(once, "1.1.1", context_title="施工准备")
        assert once == twice

    def test_unumbered_atx_heading_body_cleaned(self):
        """AI 漏写编号的 `### 专项6`：只净文本，`#` 数量不变（不擅自编号）。"""
        content = "### 专项6\n### 钢筋绑扎验收（2）\n### 施工准备"
        out = normalize_deep_heading_numbering(content, "1.1.1", context_title="施工准备")
        _lines = out.split("\n")
        assert all(ln.startswith("### ") for ln in _lines), out
        assert "专项6" not in out and "（2）" not in out
        # 具体主体保留，非脏标题原样保留
        assert _lines[1] == "### 钢筋绑扎验收"
        assert _lines[2] == "### 施工准备"
        assert not _is_dirty_l3_title_deep(_lines[0][4:]), out

    def test_unumbered_atx_heading_not_renumbered(self):
        # 无编号 = 无锚定依据，不得凭空产生编号（避免与目录编号冲突）
        out = normalize_deep_heading_numbering("### 专项6", "3.1.2", context_title="施工准备")
        assert re.match(r"^### [^0-9]", out), out


class TestPlainPseudotitleGuard:
    """审计7 BUG-1 修复（2026-10-27）：纯文本日期行/句式行不得被误重编号。

    现象：正文行首出现 3 段小数（如日期 "2026.10.27 为计划开工日期。"）时，
    旧逻辑把它当深层标题重编号为 "3.1.2.x"，日期本体被洗掉，且占用编号槽位
    令后续真实标题跳号。
    """

    def test_date_line_not_renumbered(self):
        content = "### 3.2.1 施工准备\n2026.10.27 为计划开工日期。\n### 3.2.2 材料进场"
        out = normalize_deep_heading_numbering(content, "3.1.2")
        assert "2026.10.27" in out, out
        # 日期行不得占用编号槽位：真实标题编号连续
        assert "### 3.1.2.1 施工准备" in out
        assert "### 3.1.2.2 材料进场" in out

    def test_version_line_with_sentence_punct_not_renumbered(self):
        out = normalize_deep_heading_numbering("3.14.15 圆周率取值为 3.14。", "1.2")
        assert out == "3.14.15 圆周率取值为 3.14。"

    def test_plain_heading_without_punct_still_rewritten(self):
        # 与 parse_content 的 L5 标题解析语义一致：无句末标点的编号行仍重编号
        assert (
            normalize_deep_heading_numbering("2.7.9 施工准备", "1.2")
            == "1.2.1 施工准备"
        )

    def test_atx_line_with_date_still_renumbered(self):
        # ATX 是 AI 显式标题标记，不受纯文本伪标题保护影响
        out = normalize_deep_heading_numbering("### 2026.10.27 开工日期安排", "3.1.2")
        assert out == "### 3.1.2.1 开工日期安排"

    def test_idempotent_after_guard(self):
        content = "### 3.2.1 施工准备\n2026.10.27 为计划开工日期。"
        once = normalize_deep_heading_numbering(content, "3.1.2")
        twice = normalize_deep_heading_numbering(once, "3.1.2")
        assert once == twice

    def test_dunhao_separator_loss_structure_neutral(self):
        """审计7遗留观察项4验证（2026-10-27）：纯文本编号行重写时「、」→「 」
        分隔符变化是结构中性的——parse_content 的 L3 模式同时接受两种分隔，
        仅 raw_line 保留原文。导出端还会按 heading_standard 统一重编号，
        故无需保留顿号；此测试锁定等价性，防止未来改动破坏该结论。"""
        from app.services.content_cleaning import parse_content

        dunhao = parse_content("3、总体实施策划")[0]
        space = parse_content("3 总体实施策划")[0]
        assert dunhao.level == space.level == 3
        assert dunhao.text == space.text == "总体实施策划"


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture()
def project_id(client):
    resp = client.post(
        "/api/v1/projects",
        json={"name": "深层编号净化", "industry": "公共建筑", "bidder_name": "测试投标方"},
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _saved_content(section_id: int) -> str:
    with get_db(readonly=True) as conn:
        row = conn.execute(
            "SELECT content FROM sections WHERE id = ?", (section_id,)
        ).fetchone()
    return row["content"] if row else ""


class TestSavePathSwitch:
    def test_switch_on_rewrites_deep_headings(self, project_id):
        root = sections_service.create_section(
            project_id, SectionCreate(title="施工组织设计")
        )
        # 首行编号会被既有 ContentCleaner._remove_leading_numbering 剥掉，
        # 故用「正文首段 + 内嵌深层标题」验证净化本身的行为。
        _save_section(
            sections_service,
            root.id,
            "正文首段\n### 2.9.4 施工准备\n### 2.9.4.6 复检",
            project_id,
        )

        saved = _saved_content(root.id)
        # 章节路径为 1 → 3 段标题锚定 1.1.1，4 段标题锚定 1.1.1.1
        assert "1.1.1 施工准备" in saved
        assert "1.1.1.1 复检" in saved
        assert "2.9.4" not in saved

    def test_switch_off_keeps_legacy_behavior(self, project_id, monkeypatch):
        monkeypatch.setattr(sse_db_ops, "CONTENT_DEEP_HEADING_RENUMBER_ENABLED", False)
        root = sections_service.create_section(
            project_id, SectionCreate(title="施工组织设计")
        )
        _save_section(
            sections_service, root.id, "正文首段\n### 2.9.4 施工准备", project_id
        )

        saved = _saved_content(root.id)
        assert "2.9.4 施工准备" in saved

    def test_switch_on_sanitizes_heading_text(self, project_id):
        """2026-10-27 回归：编号锚定正确但标题文本仍带占位名/冲突后缀（实测漏出）。"""
        root = sections_service.create_section(
            project_id, SectionCreate(title="施工准备")
        )
        _save_section(
            sections_service,
            root.id,
            "正文首段\n"
            "### 2.9.4 专项6\n"
            "### 2.9.5 钢筋绑扎验收（2）\n"
            "### 2.9.6 总体实施策划（2）",
            project_id,
        )

        saved = _saved_content(root.id)
        heading_lines = [
            ln for ln in saved.split("\n") if re.match(r"^1\.1\.\d+ ", ln)
        ]
        assert len(heading_lines) == 3, saved
        bodies = [ln.split(" ", 1)[1] for ln in heading_lines]
        assert not [b for b in bodies if _is_dirty_l3_title_deep(b)], bodies
        # 具体主体保留（剥后缀而不是整体替换）
        assert "钢筋绑扎验收" in saved
        assert "（2）" not in saved and "专项6" not in saved

    def test_switch_on_sanitizes_unumbered_atx_heading(self, project_id):
        """落库链路：无编号 `### 专项7` 也要净化文本，且不得凭空补编号。"""
        root = sections_service.create_section(
            project_id, SectionCreate(title="施工准备")
        )
        _save_section(
            sections_service, root.id, "正文首段\n### 专项7\n### 钢筋绑扎验收（3）", project_id
        )

        saved = _saved_content(root.id)
        assert "专项7" not in saved, saved
        assert "（3）" not in saved, saved
        assert "钢筋绑扎验收" in saved


def _save_section(service, section_id: int, content: str, project_id: int) -> None:
    sse_db_ops._save_section_to_db(section_id, "completed", content, project_id=project_id)
    assert service.get_section(section_id) is not None
