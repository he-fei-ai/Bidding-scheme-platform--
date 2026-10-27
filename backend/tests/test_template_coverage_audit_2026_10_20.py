# -*- coding: utf-8 -*-
"""test_template_coverage_audit_2026_10_20.py — 原方案内容覆盖率审计（易标机制引入）

覆盖 2026-10-20 遗留项 4 落地：``services/template_coverage_audit.py``
（移植自易标 original-plan coverage audit，L1897 / L5145 / L5292）。

锁定点：
  1. 段落→节点映射三级策略（归一化相等 / 包含 / bigram 相似度）+ 未映射不丢弃；
  2. 审计提示词包含段落与新正文、系统提示"只判定不改写"；
  3. 判定解析白名单（臆造 id/非法状态丢弃、重复取首条、围栏/近似 JSON 容错）；
  4. 报告汇总口径（covered=1、partial=0.5 加权）；
  5. 修复循环有界（≤2 轮）、只重审被修节点、缺口清零即停；
  6. 门禁：默认关 → None；无模板 → None；results 空 → None；
  7. 端到端（stub provider）：审计 → 修复 → results 就地回写。
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services import template_coverage_audit as tca  # noqa: E402


def _para(pid: str, title: str, content: str = "原方案要点内容"):
    return {"id": pid, "title_path": [title], "content": content}


LEAVES = [
    {"id": "3.2.1", "title": "施工工艺流程"},
    {"id": "3.2.2", "title": "安全保证措施"},
    {"id": "3.2.3", "title": "应急预案"},
]


# ---------------------------------------------------------------------------
# 1. 段落 → 节点映射
# ---------------------------------------------------------------------------

class TestMapParagraphsToNodes:
    def test_exact_normalized_match(self):
        """归一化后完全相等 → 直接命中。"""
        items, unmapped = tca.map_paragraphs_to_nodes(
            [_para("P001", "施工工艺流程")], LEAVES
        )
        assert len(items) == 1 and items[0]["node_id"] == "3.2.1"
        assert [p["id"] for p in items[0]["paragraphs"]] == ["P001"]
        assert unmapped == []

    def test_containment_match(self):
        """标题互相包含 → 命中（带编号前缀/后缀的常见形态）。"""
        items, _ = tca.map_paragraphs_to_nodes(
            [_para("P001", "3.2.1 施工工艺流程与操作要点")], LEAVES
        )
        assert items and items[0]["node_id"] == "3.2.1"

    def test_similarity_match(self):
        """近义标题（bigram 相似度达标）→ 命中最相似节点。"""
        items, _ = tca.map_paragraphs_to_nodes(
            [_para("P001", "施工工艺流程图")], LEAVES
        )
        assert items and items[0]["node_id"] == "3.2.1"

    def test_unmapped_not_dropped(self):
        """完全不相关的段落进 unmapped（不静默丢弃、不假装覆盖）。"""
        items, unmapped = tca.map_paragraphs_to_nodes(
            [_para("P001", "财务付款计划与结算")], LEAVES
        )
        assert items == []
        assert [u["id"] for u in unmapped] == ["P001"]

    def test_no_leaf_nodes_all_unmapped(self):
        """无叶子节点时全部段落未映射（事实不丢）。"""
        items, unmapped = tca.map_paragraphs_to_nodes([_para("P001", "甲")], [])
        assert items == [] and len(unmapped) == 1

    def test_multiple_paragraphs_same_node_grouped(self):
        """多段落命中同一节点 → 聚合到同一项。"""
        items, _ = tca.map_paragraphs_to_nodes(
            [_para("P001", "施工工艺流程"), _para("P002", "施工工艺流程（续）")],
            LEAVES,
        )
        assert len(items) == 1
        assert len(items[0]["paragraphs"]) == 2

    def test_empty_paragraph_skipped(self):
        """空内容段落跳过（无可审计内容）。"""
        items, unmapped = tca.map_paragraphs_to_nodes(
            [_para("P001", "施工工艺流程", "")], LEAVES
        )
        assert items == [] and unmapped == []

    def test_object_paragraphs_supported(self):
        """兼容 TemplateSection 对象形态（dataclass）。"""
        from types import SimpleNamespace

        sec = SimpleNamespace(id="P001", title_path=["施工工艺流程"], content="内容")
        items, _ = tca.map_paragraphs_to_nodes([sec], LEAVES)
        assert items and items[0]["node_id"] == "3.2.1"


# ---------------------------------------------------------------------------
# 2. 提示词构建
# ---------------------------------------------------------------------------

class TestPromptBuilding:
    def test_audit_messages_contain_paragraphs_and_content(self):
        item = {
            "node_title": "施工工艺流程",
            "paragraphs": [_para("P001", "施工工艺流程", "放坡 1:0.5")],
            "generated": "新正文：采用放坡 1:0.6",
        }
        messages = tca.build_coverage_audit_messages(item)
        assert messages[0].role == "system"
        assert "只判定" in messages[0].content
        user = messages[1].content
        assert "P001" in user and "放坡 1:0.5" in user
        assert "新正文" in user

    def test_audit_messages_empty_generated(self):
        """新正文为空 → 提示词标注（空），系统规则要求判 missing。"""
        item = {"node_title": "T", "paragraphs": [_para("P001", "T")], "generated": ""}
        user = tca.build_coverage_audit_messages(item)[1].content
        assert "（空）" in user

    def test_patch_messages_contain_gaps_and_current(self):
        messages = tca.build_coverage_patch_messages(
            "施工工艺流程", "现有正文",
            [{"id": "P001", "title": "施工工艺流程", "content": "要点X",
              "status": "missing", "reason": "未涉及"}],
        )
        assert messages[0].role == "system"
        user = messages[1].content
        assert "现有正文" in user and "要点X" in user and "missing" in user


# ---------------------------------------------------------------------------
# 3. 判定解析
# ---------------------------------------------------------------------------

class TestParseVerdicts:
    def test_valid_envelope(self):
        text = json.dumps({"verdicts": [
            {"id": "P001", "status": "covered", "reason": "完整"},
            {"id": "P002", "status": "partial", "reason": "损耗"},
        ]}, ensure_ascii=False)
        out = tca.parse_coverage_verdicts(text, {"P001", "P002"})
        assert [v["status"] for v in out] == ["covered", "partial"]

    def test_bare_array_accepted(self):
        text = json.dumps([{"id": "P001", "status": "missing"}], ensure_ascii=False)
        out = tca.parse_coverage_verdicts(text, {"P001"})
        assert out and out[0]["status"] == "missing"

    def test_fenced_and_approximate_json_repaired(self):
        """带围栏 + 尾随逗号的近似 JSON 也能解析（复用全仓 JSON 稳定性策略）。"""
        text = '```json\n{"verdicts": [{"id": "P001", "status": "conflict",},]}\n```'
        out = tca.parse_coverage_verdicts(text, {"P001"})
        assert out and out[0]["status"] == "conflict"

    def test_hallucinated_id_and_status_dropped(self):
        """白名单外 id 与非法状态一律丢弃（防 AI 臆造）。"""
        text = json.dumps({"verdicts": [
            {"id": "P999", "status": "covered"},
            {"id": "P001", "status": "probably_ok"},
            {"id": "P001", "status": "covered"},
        ]}, ensure_ascii=False)
        out = tca.parse_coverage_verdicts(text, {"P001"})
        assert [v["id"] for v in out] == ["P001"] and out[0]["status"] == "covered"

    def test_duplicate_id_first_wins(self):
        text = json.dumps({"verdicts": [
            {"id": "P001", "status": "covered"},
            {"id": "P001", "status": "missing"},
        ]}, ensure_ascii=False)
        out = tca.parse_coverage_verdicts(text, {"P001"})
        assert len(out) == 1 and out[0]["status"] == "covered"

    def test_garbage_returns_empty(self):
        assert tca.parse_coverage_verdicts("完全不是 JSON", {"P001"}) == []


# ---------------------------------------------------------------------------
# 4. 报告汇总
# ---------------------------------------------------------------------------

class TestSummarize:
    def test_weighted_rate(self):
        """covered=1、partial=0.5、missing/conflict=0 加权。"""
        report = tca.summarize_verdicts(
            [
                {"id": "P001", "status": "covered"},
                {"id": "P002", "status": "partial"},
                {"id": "P003", "status": "missing"},
                {"id": "P004", "status": "conflict"},
            ],
            4,
        )
        assert report["covered"] == 1 and report["partial"] == 1
        assert report["missing"] == 1 and report["conflict"] == 1
        assert report["coverage_rate"] == 37.5  # (1 + 0.5) / 4

    def test_empty_report(self):
        report = tca.summarize_verdicts([], 0)
        assert report["total"] == 0 and report["coverage_rate"] == 0.0


# ---------------------------------------------------------------------------
# 5. 修复循环
# ---------------------------------------------------------------------------

class _ScriptedProvider:
    """按调用次序返回脚本化响应（审计 / 补写交替）。"""

    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    async def chat(self, messages, **kwargs):
        self.calls += 1
        return self._script.pop(0) if self._script else "{}"


class TestRepairLoop:
    def _items(self):
        return [{
            "node_id": "3.2.1", "node_title": "施工工艺流程",
            "paragraphs": [_para("P001", "施工工艺流程", "放坡1:0.5")],
            "generated": "新正文",
        }]

    def test_repair_bounded_two_rounds(self):
        """缺口始终存在（补写后仍判 missing）→ 最多 2 轮后停止，不无限循环。

        调用次序说明：repair_coverage_gaps 的首次调用是**补写**（初版判定由
        调用方传入），补写后才重审被修节点——故脚本按
        patch → re-audit → patch → re-audit 排布。
        """
        provider = _ScriptedProvider([
            "补写后的正文",                       # round1 patch
            json.dumps({"verdicts": [{"id": "P001", "status": "missing"}]}),
            "再补写后的正文",                     # round2 patch
            json.dumps({"verdicts": [{"id": "P001", "status": "missing"}]}),
        ])
        content, report = asyncio.run(tca.repair_coverage_gaps(
            provider, self._items(),
            [{"id": "P001", "status": "missing", "reason": "未涉及"}],
            {"3.2.1": "新正文"}, max_rounds=2,
        ))
        assert content["3.2.1"] == "再补写后的正文"
        assert len(report["repairs"]) == 2
        assert all(r["round"] <= 2 for r in report["repairs"])
        assert report["missing"] == 1  # 仍不达标，如实报告

    def test_gap_cleared_stops_early(self):
        """一轮修复后缺口清零 → 不再进第二轮（成本有界）。"""
        provider = _ScriptedProvider([
            "补写后的正文",
            json.dumps({"verdicts": [{"id": "P001", "status": "covered"}]}),
        ])
        content, report = asyncio.run(tca.repair_coverage_gaps(
            provider, self._items(),
            [{"id": "P001", "status": "missing", "reason": "未涉及"}],
            {"3.2.1": "新正文"}, max_rounds=2,
        ))
        assert content["3.2.1"] == "补写后的正文"
        assert len(report["repairs"]) == 1
        assert report["covered"] == 1 and report["missing"] == 0

    def test_no_gap_no_call(self):
        """无缺口 → 零 AI 调用，内容原样返回。"""
        provider = _ScriptedProvider([])
        content, report = asyncio.run(tca.repair_coverage_gaps(
            provider, self._items(),
            [{"id": "P001", "status": "covered", "reason": "ok"}],
            {"3.2.1": "新正文"}, max_rounds=2,
        ))
        assert provider.calls == 0
        assert content["3.2.1"] == "新正文"
        assert report["repairs"] == []

    def test_patch_failure_keeps_original(self):
        """补写调用返回空 → 保留原正文，不进无谓重审。"""
        provider = _ScriptedProvider(["   "])
        content, report = asyncio.run(tca.repair_coverage_gaps(
            provider, self._items(),
            [{"id": "P001", "status": "partial", "reason": "损耗"}],
            {"3.2.1": "新正文"}, max_rounds=2,
        ))
        assert content["3.2.1"] == "新正文"
        assert report["repairs"] == []


# ---------------------------------------------------------------------------
# 6. 门禁
# ---------------------------------------------------------------------------

class TestGate:
    def test_default_disabled(self, monkeypatch):
        """默认关闭 → 返回 None（零开销、行为不变）。"""
        monkeypatch.delenv("TEMPLATE_COVERAGE_AUDIT_ENABLED", raising=False)
        assert tca.template_coverage_audit_enabled() is False
        out = asyncio.run(tca.run_template_coverage_audit_if_enabled(
            1, _ScriptedProvider([]), [{"id": "1", "title": "T", "content": "C"}]
        ))
        assert out is None

    def test_enabled_but_no_templates(self, monkeypatch):
        """开启但项目无就绪模板 → None（非扩写项目零开销）。"""
        monkeypatch.setenv("TEMPLATE_COVERAGE_AUDIT_ENABLED", "1")
        monkeypatch.setattr(tca, "load_project_templates", lambda pid: [])
        out = asyncio.run(tca.run_template_coverage_audit_if_enabled(
            1, _ScriptedProvider([]), [{"id": "1", "title": "T", "content": "C"}]
        ))
        assert out is None

    def test_enabled_empty_results(self, monkeypatch):
        """开启但 results 为空 → None。"""
        monkeypatch.setenv("TEMPLATE_COVERAGE_AUDIT_ENABLED", "1")
        out = asyncio.run(tca.run_template_coverage_audit_if_enabled(
            1, _ScriptedProvider([]), []
        ))
        assert out is None

    def test_enabled_cancelled(self, monkeypatch):
        """已取消 → None（不再发起 AI 调用）。"""
        monkeypatch.setenv("TEMPLATE_COVERAGE_AUDIT_ENABLED", "1")

        class _Ev:
            def is_set(self):
                return True

        out = asyncio.run(tca.run_template_coverage_audit_if_enabled(
            1, _ScriptedProvider([]), [{"id": "1", "title": "T", "content": "C"}],
            cancel_event=_Ev(),
        ))
        assert out is None


# ---------------------------------------------------------------------------
# 7. 端到端（stub 全链路）
# ---------------------------------------------------------------------------

class TestEndToEnd:
    def test_audit_repair_and_write_back(self, monkeypatch):
        """开启 + 有模板 → 审计 → 补写 → results 就地回写 → 报告结构完整。"""
        monkeypatch.setenv("TEMPLATE_COVERAGE_AUDIT_ENABLED", "1")
        # 模板加载与拆分 stub（不触真实文件 IO）
        monkeypatch.setattr(
            tca, "load_project_templates",
            lambda pid: [{"id": 1, "file_path": "x.docx", "outline_json": "[]"}],
        )
        monkeypatch.setattr(
            tca, "split_template_file",
            lambda tpl: [_para("P001", "施工工艺流程", "放坡1:0.5，降水井6口")],
        )
        monkeypatch.setattr(tca, "_persist_coverage_report", lambda pid, rep: None)

        provider = _ScriptedProvider([
            json.dumps({"verdicts": [{"id": "P001", "status": "missing"}]}),
            "补写后的完整正文（含放坡1:0.5与降水井6口）",
            json.dumps({"verdicts": [{"id": "P001", "status": "covered"}]}),
        ])
        results = [{"id": 11, "title": "施工工艺流程", "content": "残缺正文"}]
        report = asyncio.run(tca.run_template_coverage_audit_if_enabled(
            1, provider, results,
        ))
        assert report is not None
        assert report["covered"] == 1 and report["missing"] == 0
        assert report["total"] == 1
        assert len(report["repairs"]) == 1
        # 修复后的正文已就地回写 results（后续落库/导出拿得到）
        assert "补写后" in results[0]["content"]
        assert "unmapped" in report

    def test_unmapped_reported_not_dropped(self, monkeypatch):
        """未映射段落在报告里单独计数（不静默丢失）。"""
        monkeypatch.setenv("TEMPLATE_COVERAGE_AUDIT_ENABLED", "1")
        monkeypatch.setattr(
            tca, "load_project_templates",
            lambda pid: [{"id": 1, "file_path": "x.docx", "outline_json": "[]"}],
        )
        monkeypatch.setattr(
            tca, "split_template_file",
            lambda tpl: [_para("P001", "财务付款计划", "付款节点……")],
        )
        monkeypatch.setattr(tca, "_persist_coverage_report", lambda pid, rep: None)
        report = asyncio.run(tca.run_template_coverage_audit_if_enabled(
            1, _ScriptedProvider([]),
            [{"id": 11, "title": "施工工艺流程", "content": "正文"}],
        ))
        assert report is not None
        assert [u["id"] for u in report["unmapped"]] == ["P001"]
        assert report["total"] == 0


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
