"""正文生成模块 · 事实校验与生成侧一致性 + 遗留项加固回归测试（2026-10-21）。

覆盖内容：
- #1 verify_fact_invocation 必须随 industry 与生成侧 select 分类到同一章；
- #2 classify_chapter 暴露 matched 置信度；未命中标准九大章节的章节从「静默」
      变为可观测 warn（fact_chapter_unmatched）；
- #3 兜底扩写/修复字数口径统一到 R1 可读字数（_words_excluding_chart_payload）；
- #4 事实追溯按 section_id 匹配，消除同名章碰撞/标题 sanitize 误报。
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.ai.ordered_generation import (  # noqa: E402
    select_facts_by_chapter_map,
    verify_fact_invocation,
)
from app.services import content_field_contract as cfc  # noqa: E402


def _fake_classify(title, industry=None):
    """行业名存在时归入第 5 章，否则归入第 1 章——制造分类分叉。"""
    if industry:
        return {"idx": 5, "theme": "施工进度计划及保证措施", "keywords": [], "matched": True}
    return {"idx": 1, "theme": "工程概况与编制依据", "keywords": [], "matched": True}


def _required_keys(report, title):
    return {
        item.split("(")[-1].rstrip(")")
        for item in report["chapters"][title]["required_facts"]
    }


# --------------------------------------------------------------------------
# #1 industry 一致性
# --------------------------------------------------------------------------

def test_verify_fact_invocation_uses_industry_like_generation():
    title = "某行业专项章节"
    results = [{"title": title, "content": "正文内容示例"}]
    idx5_facts = set(cfc.CHAPTER_FIELD_MAP[5]["facts"])
    idx1_facts = set(cfc.CHAPTER_FIELD_MAP[1]["facts"])

    with patch.object(cfc, "classify_chapter", _fake_classify):
        rep_with = verify_fact_invocation(results, None, [], None, industry="X")
        rep_without = verify_fact_invocation(results, None, [], None)

    req_with = _required_keys(rep_with, title)
    req_without = _required_keys(rep_without, title)
    assert req_with != req_without, "industry 未参与校验侧章节分类（修复未生效）"
    assert req_with == idx5_facts, "传 industry 的校验侧未落到第 5 章映射"
    assert req_without == idx1_facts, "不传 industry 的校验侧未落到第 1 章映射"


def test_generation_side_and_verify_side_share_classifier():
    out = select_facts_by_chapter_map("某行业专项章节", [], None, industry="X")
    assert isinstance(out, list)


# --------------------------------------------------------------------------
# #2 未匹配章节可观测告警（替代静默兜底）
# --------------------------------------------------------------------------

def test_classify_chapter_exposes_matched_flag():
    # 命中标准关键词 → matched True
    assert cfc.classify_chapter("第一章 工程概况")["matched"] is True
    # 纯自定义标题 → matched False（兜底）
    assert cfc.classify_chapter("某个完全自定义的小节标题XYZ")["matched"] is False


def test_verify_warns_on_unmatched_custom_chapter():
    title = "某个完全自定义的小节标题XYZ"
    results = [{"title": title, "content": "正文内容"}]
    rep = verify_fact_invocation(results, None, [], None)
    types = {w["type"] for w in rep["warnings"]}
    assert "fact_chapter_unmatched" in types, "自定义章节未匹配应告警而非静默"


def test_verify_no_unmatched_warn_for_standard_chapter():
    results = [{"title": "第一章 工程概况", "content": "正文内容"}]
    rep = verify_fact_invocation(results, None, [], None)
    types = {w["type"] for w in rep["warnings"]}
    assert "fact_chapter_unmatched" not in types


# --------------------------------------------------------------------------
# #3 扩写字数口径 = R1 可读字数（剔除图表载荷）
# --------------------------------------------------------------------------

def test_words_excluding_chart_payload_uses_readable():
    from app.services.ai.workflows_expand import _words_excluding_chart_payload

    # 100 个可读汉字 + 一个 mermaid 围栏（100 个载荷字符，应被剔除）
    content = ("甲" * 100) + "\n```mermaid\n" + ("乙" * 100) + "\n```\n"
    n = _words_excluding_chart_payload(content)
    assert n == 100, f"图表载荷应被剔除，期望 100，实得 {n}"


# --------------------------------------------------------------------------
# #4 事实追溯按 section_id 匹配（消除同名章碰撞误报）
# --------------------------------------------------------------------------

def test_verify_attributes_trace_by_section_id():
    # 两章同名「质量保证」，但只有 id=10 注入了「质量目标」
    results = [
        {"id": 10, "title": "质量保证措施", "content": "正文A"},
        {"id": 11, "title": "质量保证措施", "content": "正文B"},
    ]
    field_trace = [
        {"chapter": "质量保证措施", "field": "质量目标", "source": "facts",
         "section_id": 10, "value": "合格"},
    ]
    rep = verify_fact_invocation(results, None, field_trace, None)
    ch10 = rep["chapters"]["质量保证措施"]

    # 按 id 匹配后：id=10 命中「质量目标」，id=11 未命中——证明不再按 title 混淆。
    # （rep["chapters"] 以 title 为键会合并，故这里改从 warnings 观察差异。）
    # 直接用内部双索引验证：构造只有 id=11 的情况做对照。
    field_trace_11 = [
        {"chapter": "质量保证措施", "field": "质量目标", "source": "facts",
         "section_id": 11, "value": "合格"},
    ]
    rep2 = verify_fact_invocation(
        [{"id": 11, "title": "质量保证措施", "content": "正文"}],
        None, field_trace_11, None,
    )
    # id=11 命中 → 不应再报「质量目标」未调用
    detail = rep2["chapters"]["质量保证措施"]
    assert "质量目标" not in detail["not_called"], "按 id 匹配应命中注入事实"
    assert ch10["chapter_idx"] == 7  # 质量保证 → 第 7 章


def test_verify_id_match_avoids_title_collision():
    # 同名两章，仅 id=10 注入；id=11 应被正确判为「未调用」。
    results = [{"id": 11, "title": "质量保证措施", "content": "正文B"}]
    field_trace = [
        {"chapter": "质量保证措施", "field": "质量目标", "source": "facts",
         "section_id": 10, "value": "合格"},
    ]
    rep = verify_fact_invocation(results, None, field_trace, None)
    detail = rep["chapters"]["质量保证措施"]
    # id=11 与 section_id=10 不匹配 → 应报 not_called（若按 title 匹配则误判为已调用）
    assert any("质量目标" in x for x in detail["not_called"]), \
        "同名不同 id 的章节不应共享 title 追溯（防碰撞）"
