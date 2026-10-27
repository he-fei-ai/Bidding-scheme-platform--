# -*- coding: utf-8 -*-
"""残留清洗落库前置（2026-10-27 · 审计7遗留建议2）。

背景：系统残留清洗（10-26）此前只接线在导出端 ``clean_generated_content``，
落库链路 ``ContentCleaner`` 不清洗 —— 前端预览可见「请确认是否生成…」、
【章节L3】、示意性注释等残留，与导出文档不一致。

方案：``ContentCleaner.clean`` 落库前复用导出端同一套清洗器
（``_strip_non_body_markers`` + ``_strip_system_residue``），经
``_protect_chart_fences`` 占位符保护图表载荷（零污染），并补齐与
围栏保护的正交测试；另补空行归一（与导出端同口径）。

关键约束（本测试锁定）：
  - 系统残留行落库即删（系统提示/内部标记/示意性注释/非正文段/Git冲突）；
  - 图表围栏载荷内的「长得像残留」的行一律不动；
  - 合法注释（注：xxx，无示意/示例/演示字样）保留；
  - 幂等，且导出端二次清洗稳定（预览=导出所见一致）。
"""
from __future__ import annotations

import pytest

from app.services.content_cleaning import ContentCleaner, clean_generated_content


def _clean(content: str, title: str = "施工准备") -> str:
    return ContentCleaner().clean(title, content)


class TestResiduePresave:
    def test_system_prompt_line_removed(self):
        out = _clean("正文段落。\n\n请确认是否生成正式投标文本？\n\n结尾。")
        assert "请确认是否生成" not in out
        assert "正文段落" in out and "结尾" in out

    def test_internal_chapter_marker_removed(self):
        out = _clean("正文段落。\n【章节L3】施工准备\n结尾。")
        assert "【章节L3】" not in out
        assert "结尾" in out

    def test_illustrative_note_removed(self):
        out = _clean("正文段落。\n注：以下配置仅为演示。\n结尾。")
        assert "仅为演示" not in out

    def test_legit_note_kept(self):
        out = _clean("正文段落。\n注：以上工程量以设计图纸为准。\n结尾。")
        assert "注：以上工程量以设计图纸为准。" in out

    def test_non_body_marker_and_following_paragraph_removed(self):
        out = _clean("正文段落。\n【审阅稿·非正文】\n这段审阅稿不应保留。\n\n正常段。")
        assert "审阅稿" not in out
        assert "正常段" in out

    def test_git_conflict_head_side_kept(self):
        content = (
            "正文段落。\n"
            "<<<<<<< HEAD\n"
            "甲方指定品牌。\n"
            "=======\n"
            "乙方推荐品牌。\n"
            ">>>>>>> dev\n"
            "结尾。"
        )
        out = _clean(content)
        assert "甲方指定品牌" in out
        assert "乙方推荐品牌" not in out
        assert "<<<<<<<" not in out and ">>>>>>>" not in out

    def test_chart_fence_payload_untouched(self):
        """正交：围栏内「长得像残留」的行必须原样保留。"""
        content = (
            "正文段落。\n\n"
            "```mermaid\n"
            "flowchart LR\n"
            "注：示例节点行在围栏内\n"
            "A[开始] --> B[结束]\n"
            "```\n\n"
            "结尾。"
        )
        out = _clean(content)
        assert "注：示例节点行在围栏内" in out, out
        assert "A[开始] --> B[结束]" in out
        assert "```mermaid" in out

    def test_blank_lines_normalized(self):
        out = _clean("正文段落。\n\n\n\n\n结尾段落。")
        assert "\n\n\n" not in out
        assert "正文段落。\n\n结尾段落。" == out

    def test_idempotent(self):
        content = (
            "正文段落。\n请确认是否生成正式投标文本？\n注：仅为示例。\n正常段。"
        )
        once = _clean(content)
        twice = _clean(once)
        assert once == twice

    def test_export_second_pass_stable(self):
        """落库清洗后的内容经导出端全量清洗结果稳定（预览=导出一致）。"""
        content = (
            "正文段落。\n\n"
            "请确认是否生成正式投标文本？\n"
            "【章节L3】施工准备\n"
            "注：仅为示例。\n\n"
            "```chart-json\n"
            '{"type": "bar", "注：示例字段": 1}\n'
            "```\n\n"
            "结尾。"
        )
        saved = _clean(content)
        exported = clean_generated_content(saved)
        assert exported.strip() == saved.strip(), (saved, exported)
        assert "请确认是否生成" not in saved
        assert "注：示例字段" in saved  # 围栏载荷零污染

    def test_never_touches_global_fence_cache(self, monkeypatch):
        """并发安全锁定（审计7）：ContentCleaner 不得触碰 _fence_cache 全局缓存。

        根因回归：导出构建在 asyncio.to_thread 工作线程里执行
        clean_generated_content（_fence_cache 使用者）；ContentCleaner 由事件
        循环线程调用。若落库清洗也 clear() 全局缓存，会吞掉构建线程未还原的
        占位符 → 导出流挂死（全量回归中 test_disconnect_drains_journal_and_
        resume_replays 420s 超时的根因）。本测试用"一碰即炸"的哨兵对象锁定
        ContentCleaner 与全局缓存零接触。"""
        import app.services.content_cleaning as cc

        class _ExplodingCache:
            @property
            def blocks(self):
                raise AssertionError("ContentCleaner 不得触碰 _fence_cache（线程互踩）")

        monkeypatch.setattr(cc, "_fence_cache", _ExplodingCache())
        content = (
            "正文段落。\n请确认是否生成正式投标文本？\n"
            "```mermaid\nflowchart LR\nA[开始] --> B[结束]\n```\n正常段。"
        )
        out = _clean(content)
        assert "A[开始] --> B[结束]" in out
        assert "请确认是否生成" not in out

    def test_unclosed_fence_payload_residue_stripped_lenient(self):
        """未闭合围栏不在保护区间内（scan 只收闭合块）：其载荷按普通文本清洗。
        与导出端行为对齐——导出端直接删除未闭合坏块；落库端保留原文但允许
        残留行清洗（生成链路已由 _force_close_unclosed_chart_fence 兜底闭合，
        此分支仅防手工脏数据）。"""
        content = (
            "正文段落。\n```mermaid\nflowchart LR\n注：仅为示例。\n\n正常段。"
        )
        out = _clean(content)
        assert "仅为示例" not in out
        assert "正常段" in out


class TestPresaveSavePath:
    """落库链路真实生效：_save_section_to_db → ContentCleaner。"""

    @pytest.fixture()
    def project_id(self):
        from fastapi.testclient import TestClient

        from app.main import app

        client = TestClient(app)
        resp = client.post(
            "/api/v1/projects",
            json={"name": "残留前置", "industry": "公共建筑", "bidder_name": "测试投标方"},
        )
        assert resp.status_code in (200, 201), resp.text
        return resp.json()["id"]

    def test_saved_content_has_no_residue(self, project_id):
        from app.db import get_db
        from app.models import SectionCreate
        from app.routers.sse import sse_db_ops
        from app.services import sections as sections_service

        root = sections_service.create_section(
            project_id, SectionCreate(title="施工准备")
        )
        sse_db_ops._save_section_to_db(
            root.id,
            "completed",
            "请确认是否生成正式投标文本？\n【章节L3】施工准备\n正常段落。",
            project_id=project_id,
        )
        with get_db(readonly=True) as conn:
            row = conn.execute(
                "SELECT content FROM sections WHERE id = ?", (root.id,)
            ).fetchone()
        saved = row["content"] if row else ""
        assert "请确认是否生成" not in saved
        assert "【章节L3】" not in saved
        assert "正常段落。" in saved
