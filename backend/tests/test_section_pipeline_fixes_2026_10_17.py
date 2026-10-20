# -*- coding: utf-8 -*-
"""章节编写模块第六轮深度审计 BUG 回归测试（2026-10-17）。

覆盖本轮实证确认并修复的 5 项缺陷（探针 scripts/_audit6_probe.py 取证）：

- A1（P1·SSE）：重试成功的章节双计终态 → overall_progress.percent 越界
  （实测事件序列 50→100→150→200）。修复：_SectionTally 按 section_id
  去重登记终态，重试成功自动覆盖主生成的 failed。
- B1（P1·图表）：AI 压缩删除/复制图表占位符 → 图表静默丢失/重复插入。
  修复：condense_overlong_content 还原前校验占位符存在性 + 唯一性，
  不合格即拒绝压缩、保留原文。
- C1（P2·压缩）：threshold 与 meta["hi"] 同式 → 多轮压缩结构性不可达，
  且「有进展但未进区间」的压缩稿被整体丢弃。修复：接受有进展的中间稿、
  轮数用尽才退出；_charts_delta 与提示词基准随轮更新。
- D1（P2·编号）：strip_heading_numbering_from_content 手写围栏骨架
  （stripped[:3]）使 4 反引号围栏被内嵌 3 反引号行误闭合，围栏内标题
  编号被误剥。修复：复用权威 fence_marker（同字符且长度不小于）。
- E1（P2·格式）：<<表格：>> 块 60 行硬截断，长表格后半静默降级为
  段落。修复：300 行窗口内寻找闭合标记，找不到才回退 60 行安全上限
  （并 WARNING 告警）。

另覆盖 ENHANCE 2026-10-17：_auto_continue_on_length 续写目标按章节
字数预算收敛（无 target_words 时行为逐字不变）。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os

import pytest

from app.routers.sse.sse_content import _SectionTally
from app.services.ai import workflows_sections as ws
from app.services.content_cleaning import (
    parse_content,
    strip_heading_numbering_from_content,
)

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SSE_CONTENT_SRC = os.path.join(BACKEND_DIR, "app", "routers", "sse", "sse_content.py")


def _read_src() -> str:
    with open(SSE_CONTENT_SRC, "r", encoding="utf-8") as fh:
        return fh.read()


def _cn(n: int) -> str:
    """生成约 n 字中文正文（用于字数断言）。"""
    return "项目实施内容较为丰富，需要统筹规划。" * (n // 17 + 1)


# ===========================================================================
# A1：_SectionTally 终态去重（P1·SSE 进度越界）
# ===========================================================================
class TestSectionTally:
    def test_retry_success_overwrites_failed(self):
        """核心回归：主生成 failed → 重试成功，必须覆盖而非双计。"""
        t = _SectionTally()
        t.record(101, "failed")
        assert (t.saved, t.failed, t.done) == (0, 1, 1)
        t.record(101, "completed")  # 重试成功
        assert (t.saved, t.failed, t.done) == (1, 0, 1)

    def test_counts_never_double_count_across_retries(self):
        """多章节混合场景：done 恒等于已终态去重章节数。"""
        t = _SectionTally()
        t.record(1, "failed")
        t.record(2, "failed")
        t.record(1, "completed")  # 重试成功
        t.record(3, "cancelled")
        assert (t.saved, t.failed, t.cancelled, t.done) == (1, 1, 1, 3)

    def test_recount_from_results_includes_cancelled(self):
        """recount 全量重算必须覆盖 cancelled（旧实现只重算 saved/failed）。"""
        t = _SectionTally()
        t.record(1, "failed")
        t.recount([
            {"id": 1, "status": "completed"},
            {"id": 2, "status": "cancelled"},
            {"id": 3, "status": "failed"},
            {"id": None, "status": "completed"},   # 无 id 忽略
            {"id": 4, "status": "pending"},         # 非终态忽略
        ])
        assert (t.saved, t.failed, t.cancelled, t.done) == (1, 1, 1, 3)

    def test_record_none_id_ignored(self):
        t = _SectionTally()
        t.record(None, "completed")
        assert t.done == 0

    def test_percent_never_exceeds_total(self):
        """done ≤ total 不变量：percent 不可能越界（A1 的数学根因）。"""
        t = _SectionTally()
        for sid, st in [(1, "failed"), (2, "failed")]:
            t.record(sid, st)
        for sid in (1, 2):
            t.record(sid, "completed")  # 全部重试成功
        total = 2
        assert t.done <= total
        assert round(t.done / max(total, 1) * 100) == 100

    def test_wiring_uses_tally_not_bare_increment(self):
        """源码契约门禁：回调必须走 tally 登记，不得回退累加式计数。"""
        src = _read_src()
        assert "_SectionTally()" in src, "终态计数器未实例化"
        assert "_section_tally.record(sid, status)" in src, "回调未登记终态"
        assert "_section_tally.recount(results)" in src, "重试后未全量重算"
        assert "_done = _section_tally.done" in src, "整体进度未取 tally 口径"
        assert "saved_count += 1" not in src, "存在累加式计数残留（双计风险）"


# ===========================================================================
# A1 端到端：真实 generate_content_impl 链路，重试后 percent 不越界
# ===========================================================================
class _StubGM:
    """generation_manager 桩（只保留本链路触及的面）。"""

    _lock = asyncio.Lock()
    _persist_locks: dict = {}
    _tasks: dict = {}

    async def create_task(self, *a, **kw):
        return "gen-test"

    async def update_section_progress(self, *a, **kw):
        return None

    async def is_paused(self, *a, **kw):
        return False

    async def finalize_task(self, *a, **kw):
        return None

    def _persist_task(self, *a, **kw):
        return None


class _StubCache:
    @staticmethod
    def invalidate_sections(*a, **kw):
        return None


def _run_retry_generation(monkeypatch) -> list[dict]:
    """驱动真实 generate_content_impl：主生成 2 章全败 → 重试全成。

    返回全部 SSE 事件（已解析 JSON）。这是 A1 的端到端证据：
    旧实现在此链路下会推送 percent=150/200 的 overall_progress。
    """
    from app.routers.sse import sse_content as sc

    leaves = [
        {"id": 101, "title": "施工部署", "level": 3, "target_words": 300,
         "description": "", "path": "1", "parent_id": None, "sort_order": 1},
        {"id": 102, "title": "质量保证", "level": 3, "target_words": 300,
         "description": "", "path": "2", "parent_id": None, "sort_order": 2},
    ]
    sections_by_id = {s["id"]: dict(s) for s in leaves}
    call_state = {"n": 0}

    async def _fake_run_sections(provider, sections, **kwargs):
        call_state["n"] += 1
        cb = kwargs.get("section_complete_callback")
        results = []
        for s in sections:
            if call_state["n"] == 1:
                res = {"id": s["id"], "title": s.get("title", ""), "status": "failed",
                       "content": "", "error": "模拟主生成失败", "quality": None}
            else:
                res = {"id": s["id"], "title": s.get("title", ""), "status": "completed",
                       "content": "正文内容" * 30, "error": "", "quality": None}
            results.append(res)
            if cb is not None:
                await cb(res)
        return results

    async def _fake_provider(*a, **kw):
        return object(), None, 2

    async def _fake_project_data(project_id):
        return {
            "summary": "项目摘要", "scoring_points": "[]", "industry_profile": "",
            "tender_context": "", "construction_method": "",
            "sections_by_id": sections_by_id, "children_map": {}, "all_rows": [],
            "global_facts_items": None, "facts_policy": "fabricate", "industry": "",
        }

    class _WCState:
        class effective_options:
            section_words = 300
            strict_section_words = False

        def gate_error(self):
            return None

    async def _noop(*a, **kw):
        return True

    async def _noop_condense(results, *a, **kw):
        return {"condensed_count": 0, "fail_count": 0}

    monkeypatch.setattr(sc, "_try_get_provider", _fake_provider)
    monkeypatch.setattr(sc, "CacheService", _StubCache)
    monkeypatch.setattr(sc, "_fetch_project_data_async", _fake_project_data)
    monkeypatch.setattr(sc, "_build_leaf_dicts", lambda **kw: [dict(s) for s in leaves])
    monkeypatch.setattr(sc, "build_tree", lambda *a, **kw: [])
    monkeypatch.setattr(sc, "update_step", lambda *a, **kw: None)
    monkeypatch.setattr(sc, "generation_manager", _StubGM())
    monkeypatch.setattr(sc, "_run_sections", _fake_run_sections)
    monkeypatch.setattr(sc, "_save_section_to_db", _noop)
    monkeypatch.setattr(sc, "_auto_generate_charts_for_section", _noop)
    monkeypatch.setattr(sc, "_OVERALL_PROGRESS_THROTTLE_SECS", 0.0)

    import app.services.word_control_runtime as wcr
    monkeypatch.setattr(wcr, "load_project_word_control", lambda *a, **kw: _WCState())
    monkeypatch.setattr(wcr, "build_section_override", lambda *a, **kw: {})

    import app.services.sections as sec_svc
    monkeypatch.setattr(sec_svc, "invalidate_content_caches", lambda *a, **kw: None)
    monkeypatch.setattr(ws, "condense_overlong_results", _noop_condense)

    async def _drive():
        resp = await sc.generate_content_impl(
            project_id=998877,
            writing_length="standard",
            regenerate_ids="",
            root_section_id=None,
            regenerate_missing=False,
            use_orchestration=False,
            level2_default=3500,
            use_word_sampling=False,
            use_fallback_expand=False,
            use_consistency_audit=False,
            concurrency=2,
            orch_max_ai_images=6,
            orch_table_ratio="moderate",
            orch_prefer_mermaid=True,
            orch_section_per_segment=3,
            use_word_adjust=False,
        )
        events = []
        async for chunk in resp.body_iterator:
            line = chunk.strip()
            if line.startswith("data: "):
                try:
                    events.append(json.loads(line[6:]))
                except json.JSONDecodeError:
                    pass
        return events

    return asyncio.run(_drive())


class TestSSERetryProgressEndToEnd:
    def test_percent_never_exceeds_100_after_retry(self, monkeypatch):
        """A1 端到端：重试全成后不得出现 percent > 100，终态计数正确。"""
        events = _run_retry_generation(monkeypatch)
        progress = [e for e in events if e.get("status") == "overall_progress"]
        assert progress, "未捕获 overall_progress 事件（链路可能早退）"
        for e in progress:
            assert e["percent"] <= 100, f"percent 越界: {e}"
            assert e["done"] <= e["total"], f"done 越界: {e}"
        percents = [e["percent"] for e in progress]
        assert max(percents) == 100, f"最终进度应到 100，实际 {percents}"
        # 终态 completed 事件：2 章全部重试成功
        completed = [e for e in events if e.get("status") == "completed"]
        assert completed and completed[-1]["success"] == 2
        assert completed[-1]["failed"] == 0

    def test_progress_sequence_is_monotonic_nonregressing(self, monkeypatch):
        """进度序列不得出现「回退后双计」形态（failed 与 saved 同时含同一章）。"""
        events = _run_retry_generation(monkeypatch)
        progress = [e for e in events if e.get("status") == "overall_progress"]
        for e in progress:
            assert e["saved"] + e["failed"] + e["cancelled"] == e["done"], (
                f"计数与 done 不一致（双计）: {e}"
            )


# ===========================================================================
# B1：压缩占位符存在性 + 唯一性校验（P1·图表静默丢失/重复）
# ===========================================================================
_CHART_CONTENT = (
    "前言段落。" * 60 + "\n\n"
    "```mermaid\ngraph TD\n  A[开工] --> B[竣工]\n```\n\n"
    "中间段落。" * 60 + "\n\n"
    "```chart-json\n{\"type\": \"gantt\", \"tasks\": [{\"name\": \"基础\"}]}\n```\n\n"
    "后记段落。" * 60
)


class _DropPlaceholderProvider:
    """破坏指定占位符的假 provider（drop_idx=0 表示不删）。"""

    def __init__(self, drop_idx: int = 0, dup_idx: int = 0) -> None:
        self.drop_idx = drop_idx
        self.dup_idx = dup_idx

    async def chat(self, messages, **kwargs):
        user = messages[-1].content
        body = user.split("删除空话套话与冗余表述：\n\n", 1)[-1]
        if self.dup_idx:
            body = body + f"\n<<CHART_BLOCK_{self.dup_idx}>>"
        if self.drop_idx:
            body = body.replace(f"<<CHART_BLOCK_{self.drop_idx}>>", "")
        return "压缩后的正文内容。" * 20 + "\n\n" + body


class _KeepPlaceholderProvider:
    """保留占位符并压缩正文的假 provider（第 1 轮 ~300 字）。"""

    async def chat(self, messages, **kwargs):
        user = messages[-1].content
        body = user.split("删除空话套话与冗余表述：\n\n", 1)[-1]
        kept = [ln for ln in body.split("\n") if "<<CHART_BLOCK_" in ln]
        out: list[str] = []
        while ws.count_words("\n".join(out)) < 300:
            out.append("施工组织设计内容。")
        return "\n".join(out + kept)


class TestChartPlaceholderValidation:
    def test_helper_unit(self):
        ok, reason = ws._validate_chart_placeholders("a<<CHART_BLOCK_1>>b<<CHART_BLOCK_2>>c", ["x", "y"])
        assert ok and reason == ""
        ok, reason = ws._validate_chart_placeholders("a<<CHART_BLOCK_1>>b", ["x", "y"])
        assert not ok and "删除" in reason
        ok, reason = ws._validate_chart_placeholders("a<<CHART_BLOCK_1>><<CHART_BLOCK_1>>", ["x"])
        assert not ok and "复制" in reason
        ok, _ = ws._validate_chart_placeholders("无占位符", [])
        assert ok

    def test_deleted_placeholder_rejects_compression(self):
        """AI 删除占位符 → 拒绝压缩、保留原文（图表不丢）。"""
        provider = _DropPlaceholderProvider(drop_idx=2)
        final, meta = asyncio.run(ws.condense_overlong_content(
            _CHART_CONTENT, target_words=300, tolerance_ratio=0.2,
            provider=provider, section_title="审计章", max_rounds=1,
        ))
        assert final == _CHART_CONTENT, "必须保留原文（含全部图表）"
        assert "占位符" in meta["error"] and "删除" in meta["error"]
        assert meta["condensed"] is False

    def test_duplicated_placeholder_rejects_compression(self):
        """AI 复制占位符 → 拒绝压缩（防图表重复插入）。"""
        provider = _DropPlaceholderProvider(dup_idx=1)
        final, meta = asyncio.run(ws.condense_overlong_content(
            _CHART_CONTENT, target_words=300, tolerance_ratio=0.2,
            provider=provider, section_title="审计章", max_rounds=1,
        ))
        assert final == _CHART_CONTENT
        assert "复制" in meta["error"]

    def test_valid_placeholders_still_compress(self):
        """占位符完好时压缩正常完成（不误伤）。"""
        provider = _KeepPlaceholderProvider()
        final, meta = asyncio.run(ws.condense_overlong_content(
            _CHART_CONTENT, target_words=300, tolerance_ratio=0.2,
            provider=provider, section_title="审计章", max_rounds=1,
        ))
        assert meta["condensed"] is True
        assert "graph TD" in final, "mermaid 图表必须还原"
        assert '"gantt"' in final, "chart-json 图表必须还原"
        assert "<<CHART_BLOCK_" not in final, "占位符不得残留"


# ===========================================================================
# C1：多轮压缩可达性 + 基准随轮更新（P2）
# ===========================================================================
class _TwoRoundProvider:
    """第 1 轮 ~300 字（+图表仍超 hi），第 2 轮 ~200 字（进区间）。"""

    def __init__(self) -> None:
        self.user_msgs: list[str] = []

    async def chat(self, messages, **kwargs):
        user = messages[-1].content
        self.user_msgs.append(user)
        n = len(self.user_msgs)
        body = user.split("删除空话套话与冗余表述：\n\n", 1)[-1]
        kept = [ln for ln in body.split("\n") if "<<CHART_BLOCK_" in ln]
        target_w = 300 if n == 1 else 200
        out: list[str] = []
        while ws.count_words("\n".join(out)) < target_w:
            out.append("施工组织设计内容。")
        return "\n".join(out + kept)


class TestMultiRoundCondense:
    def _content(self):
        chart = "```mermaid\n" + ("graph TD\n  A[节点甲] --> B[节点乙]\n" * 12) + "```"
        return _cn(500) + "\n\n" + chart

    def test_multi_round_reachable_and_final_in_range(self):
        provider = _TwoRoundProvider()
        final, meta = asyncio.run(ws.condense_overlong_content(
            self._content(), target_words=300, tolerance_ratio=0.2,
            provider=provider, section_title="施工组织", max_rounds=2,
        ))
        assert len(provider.user_msgs) == 2, "多轮压缩必须真实可达"
        assert meta["rounds"] == 2
        assert meta["condensed"] is True
        assert meta["error"] == ""
        assert ws.count_words(final) <= meta["hi"], "终稿必须进目标区间"

    def test_partial_compression_kept_when_rounds_exhausted(self):
        """有进展但未进区间且轮数用尽 → 保留已压缩成果（旧实现丢弃）。"""
        provider = _TwoRoundProvider()
        final, meta = asyncio.run(ws.condense_overlong_content(
            self._content(), target_words=300, tolerance_ratio=0.2,
            provider=provider, section_title="施工组织", max_rounds=1,
        ))
        assert meta["condensed"] is True, "部分压缩成果必须被接受"
        assert ws.count_words(final) < ws.count_words(self._content())

    def test_no_progress_rejected_with_legacy_message(self):
        """毫无进展（越写越长）→ 保留原文，错误措辞保持历史契约。"""
        class _Longer:
            async def chat(self, messages, **kwargs):
                user = messages[-1].content
                body = user.split("删除空话套话与冗余表述：\n\n", 1)[-1]
                kept = [ln for ln in body.split("\n") if "<<CHART_BLOCK_" in ln]
                out: list[str] = []
                while ws.count_words("\n".join(out)) < 600:
                    out.append("施工组织设计内容。")
                return "\n".join(out + kept)

        content = self._content()
        final, meta = asyncio.run(ws.condense_overlong_content(
            content, target_words=300, tolerance_ratio=0.2,
            provider=_Longer(), section_title="施工组织", max_rounds=2,
        ))
        assert final == content
        assert "未减少" in meta["error"]

    def test_round2_prompt_uses_current_round_base(self):
        """第 2 轮提示词的「原文 N 字」必须播报本轮基准，而非首轮原值。"""
        provider = _TwoRoundProvider()
        asyncio.run(ws.condense_overlong_content(
            self._content(), target_words=300, tolerance_ratio=0.2,
            provider=provider, section_title="施工组织", max_rounds=2,
        ))
        assert len(provider.user_msgs) == 2
        msg1, msg2 = provider.user_msgs
        wc1 = ws.count_words(self._content())
        assert f"原文 {wc1} 字" in msg1, "第 1 轮播报原始字数"
        assert f"原文 {wc1} 字" not in msg2, (
            "第 2 轮不得播报首轮原值（基准必须随轮更新）"
        )


# ===========================================================================
# D1：编号剥离的围栏判定复用权威口径（P2）
# ===========================================================================
class TestStripHeadingNumberingFence:
    def test_heading_inside_four_backtick_fence_untouched(self):
        """4 反引号围栏内嵌 3 反引号行：围栏内标题编号不得被误剥。"""
        content = "````mermaid\ngraph TD\n```\n## 1.2 围栏内的标题\n````\n"
        out = strip_heading_numbering_from_content(content)
        assert "## 1.2 围栏内的标题" in out

    def test_tilde_fence_with_inner_backticks_untouched(self):
        """~~~ 围栏内嵌 ``` 行：围栏内标题不得被误剥。"""
        content = "~~~mermaid\ngraph TD\n```\n## 2.3 图表内标题\n~~~\n"
        out = strip_heading_numbering_from_content(content)
        assert "## 2.3 图表内标题" in out

    def test_normal_atx_stripping_still_works(self):
        """回归保护：围栏外标题编号照常剥离。"""
        content = "## 1.1 施工部署\n正文。\n\n### 2.1 质量保证\n更多正文。"
        out = strip_heading_numbering_from_content(content)
        assert "## 施工部署" in out
        assert "### 质量保证" in out
        assert "1.1" not in out and "2.1" not in out

    def test_headings_inside_plain_fence_untouched(self):
        """普通代码围栏内的编号样行同样受保护。"""
        content = "```python\n## 1.1 假标题\nprint('x')\n```\n"
        out = strip_heading_numbering_from_content(content)
        assert "## 1.1 假标题" in out

    def test_cross_char_close_does_not_close(self):
        """跨字符围栏不得互相闭合（``` 开启的块 ~~~ 行不闭合）。"""
        content = "```mermaid\ngraph TD\n~~~\n## 9.9 围栏内\n```\n"
        out = strip_heading_numbering_from_content(content)
        assert "## 9.9 围栏内" in out


# ===========================================================================
# E1：<<表格：>> 长表格完整入块（P2）
# ===========================================================================
class TestLongTableBlock:
    def test_80_row_table_complete(self):
        """80 行设备清单必须完整入一个 table 块，不得降级为段落。"""
        rows = [f"设备{i}|型号{i}|{i}台" for i in range(1, 81)]
        text = "<<表格：设备清单\n" + "\n".join(rows) + "\n>>"
        blocks = parse_content(text)
        tables = [b for b in blocks if b.type == "table"]
        paras = [b for b in blocks if b.type == "paragraph"]
        assert len(tables) == 1
        assert not paras, f"不应有段落降级: {[p.text[:20] for p in paras]}"
        assert tables[0].text.count("\n") + 1 == 82, "开行 + 80 数据行 + 闭合行"

    def test_unclosed_table_safety_cap_preserved(self):
        """无闭合 `>>` 的坏块仍受 60 行安全上限约束（防吞正文）。"""
        rows = [f"设备{i}|型号{i}|{i}台" for i in range(1, 101)]
        text = "<<表格：设备清单\n" + "\n".join(rows)
        blocks = parse_content(text)
        tables = [b for b in blocks if b.type == "table"]
        assert len(tables) == 1
        assert tables[0].text.count("\n") + 1 <= 61, "安全上限必须保持"

    def test_md_table_row_is_hard_boundary(self):
        """既有契约：管道表格行是硬边界（先关表格再开 MD 表）。"""
        text = "<<表格：清单\n| 甲 | 乙 |\n| --- | --- |\n正文段落"
        blocks = parse_content(text)
        assert any(b.type == "table" for b in blocks)
        assert any(b.type == "paragraph" and "正文段落" in b.text for b in blocks)

    def test_table_with_image_marker_stops(self):
        """既有契约：图片标记行终止表格收集（硬边界不吞后续块）。

        注意：`<<CHART:...>>` 行以 `>>` 结尾，按「先匹配闭合」口径会被
        当作表格闭合行吞入——这是新旧实现一致的历史行为，不在本轮范围；
        此处用不吞末字符的 `[[图片：]]` 标记验证硬边界语义。
        """
        text = "<<表格：清单\n行一|列二\n[[图片：示意图]]\n后续段落"
        blocks = parse_content(text)
        tables = [b for b in blocks if b.type == "table"]
        assert len(tables) == 1
        assert "[[图片：示意图]]" not in tables[0].text
        assert any(b.type == "paragraph" and "后续段落" in b.text for b in blocks)


# ===========================================================================
# ENHANCE：续写目标按章节字数预算收敛
# ===========================================================================
class _CapturePromptProvider:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def chat(self, messages, **options):
        self.calls.append(dict(options))
        return {"content": "续写的新内容。", "finish_reason": "stop"}


class TestContinueWordBudget:
    def _run(self, monkeypatch, section: dict, existing: str) -> int:
        captured: dict = {}

        def _fake_get_prompt(key, **kw):
            captured.update(kw)
            return f"prompt::{key}"

        monkeypatch.setattr(ws, "get_prompt", _fake_get_prompt)
        provider = _CapturePromptProvider()
        context = {"writing_length": "standard", "cancel_event": None}
        asyncio.run(ws._auto_continue_on_length(
            provider, existing, section, context,
            {"return_meta": True, "max_retries": 1}, max_rounds=1,
        ))
        assert provider.calls, "续写未实际调用 AI"
        return int(captured["target_chars"])

    def test_capped_by_word_budget_when_over_target(self, monkeypatch):
        """已超目标（800 字 vs 300 目标）→ 续写目标收敛到最小余量。"""
        target = self._run(
            monkeypatch,
            {"title": "测试章", "target_words": 300, "level": 3},
            _cn(800),
        )
        assert target == 200, f"应收敛到 200 字余量，实际 {target}"

    def test_legacy_formula_without_target_words(self, monkeypatch):
        """无 target_words（L1/L2 叶子）→ 与旧公式逐字一致。"""
        existing = _cn(800)
        target = self._run(
            monkeypatch, {"title": "测试章", "level": 1}, existing,
        )
        assert target == min(len(existing) * 2, max(1500, len(existing) + 2000))

    def test_under_target_allows_budget_bounded_growth(self, monkeypatch):
        """未达目标 → 续写目标不超过「剩余预算 + 200 余量」。"""
        target = self._run(
            monkeypatch,
            {"title": "测试章", "target_words": 2000, "level": 1},
            _cn(1400),
        )
        assert target <= (2000 - ws.count_words(_cn(1400))) + 200
