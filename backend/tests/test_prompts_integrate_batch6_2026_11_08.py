"""收编批次 6 测试（2026-11-08）：全局事实提取 user 侧提示词。

覆盖 _integrate_batch6.py 注册的 2 个 key：
- FACTS_EXTRACT_USER（单段）
- FACTS_EXTRACT_SEGMENT_USER（分段）

断言：
- 注册存在性 + fallback 常量与注册文本逐字一致；
- 渲染结果与原 f-string 构建逐字一致（含/不含动态块）；
- fallback（.replace 兜底）路径同样一致；
- 无残留占位符。
"""
import pytest

from app.services.ai.prompts import get_prompt, render_prompt
from app.routers import facts as facts_mod


def _orig_single(source_text, outline_block, chapter_scope_block,
                 step02_parts, summary, scoring_points):
    """复现收编前的原 f-string 构建（L807-825 旧逻辑）。"""
    s = f"请从以下项目文档中提取全局事实变量：\n\n{source_text}\n{outline_block}\n{chapter_scope_block}\n"
    if step02_parts:
        s += (
            "\n\n以下是 Step02 关键解析结果（作为补充事实来源，与文档冲突时以文档为准）：\n"
            + "\n\n".join(step02_parts)
        )
    if summary:
        s += f"\n\n参考摘要：{summary[:800]}"
    if scoring_points:
        s += f"\n参考评分点：{scoring_points[:800]}"
    s += "\n\n请提取所有可确定的事实变量，输出 JSON 对象（dict）。"
    return s


def _render_single(source_text, outline_block, chapter_scope_block,
                   step02_block, summary_block, scoring_block):
    return render_prompt(
        get_prompt("FACTS_EXTRACT_USER"),
        source_text=source_text,
        outline_block=outline_block,
        chapter_scope_block=chapter_scope_block,
        step02_block=step02_block,
        summary_block=summary_block,
        scoring_block=scoring_block,
    )


def test_facts_extract_user_registered():
    from app.services.ai.prompts import _ALL_PROMPTS
    assert "FACTS_EXTRACT_USER" in _ALL_PROMPTS
    assert "FACTS_EXTRACT_SEGMENT_USER" in _ALL_PROMPTS


def test_facts_extract_user_fallback_matches_registry():
    from app.services.ai.prompts import _ALL_PROMPTS
    assert _ALL_PROMPTS["FACTS_EXTRACT_USER"]["content"] == facts_mod._FACTS_EXTRACT_USER_FB
    assert (
        _ALL_PROMPTS["FACTS_EXTRACT_SEGMENT_USER"]["content"]
        == facts_mod._FACTS_EXTRACT_SEGMENT_USER_FB
    )
    # DB 无行时 get_prompt 返回注册文本（模板无首尾空白，clean 不变）
    assert get_prompt("FACTS_EXTRACT_USER").strip() == facts_mod._FACTS_EXTRACT_USER_FB.strip()
    assert (
        get_prompt("FACTS_EXTRACT_SEGMENT_USER").strip()
        == facts_mod._FACTS_EXTRACT_SEGMENT_USER_FB.strip()
    )


def test_single_render_identical_with_dyn_blocks():
    source_text = "招标全文ABC"
    outline_block = "\n\n### 目录块"
    chapter_scope_block = "\n\n### 章节范围"
    step02_parts = ["【项目信息摘要】\n摘要X"]
    summary = "摘要Y"
    scoring_points = "评分Z"
    step02_block = (
        "\n\n以下是 Step02 关键解析结果（作为补充事实来源，与文档冲突时以文档为准）：\n"
        + "\n\n".join(step02_parts)
    )
    summary_block = f"\n\n参考摘要：{summary[:800]}"
    scoring_block = f"\n参考评分点：{scoring_points[:800]}"
    want = _orig_single(
        source_text, outline_block, chapter_scope_block,
        step02_parts, summary, scoring_points,
    )
    got = _render_single(
        source_text, outline_block, chapter_scope_block,
        step02_block, summary_block, scoring_block,
    )
    assert got.strip() == want.strip()


def test_single_render_identical_no_dyn_blocks():
    source_text = "招标全文ABC"
    outline_block = "\n\n### 目录块"
    chapter_scope_block = "\n\n### 章节范围"
    want = _orig_single(source_text, outline_block, chapter_scope_block, [], "", "")
    got = _render_single(
        source_text, outline_block, chapter_scope_block, "", "", "",
    )
    assert got.strip() == want.strip()


def test_segment_render_identical():
    from app.services.ai.prompts import _ALL_PROMPTS
    seg = "分段1"
    idx, total = 2, 5
    outline_block = "\n\n### 目录块"
    chapter_scope_block = "\n\n### 章节范围"
    tail = "\n\n请提取所有可确定的事实变量，输出 JSON 对象（dict）。"
    want = (
        f"请从以下项目文档中提取全局事实变量（这是第 {idx}/{total} 个分段，"
        "只提取本分段有明确依据的事实；本分段没有提及不代表整份文档没有提及，"
        f"不要因某项信息未在本段出现而放弃提取）：\n\n{seg}\n{outline_block}\n{chapter_scope_block}{tail}"
    )
    got = render_prompt(
        _ALL_PROMPTS["FACTS_EXTRACT_SEGMENT_USER"]["content"],
        seg_index=idx, seg_total=total, seg=seg,
        outline_block=outline_block, chapter_scope_block=chapter_scope_block,
        extract_tail=tail,
    )
    assert got.strip() == want.strip()


def test_single_fallback_replace_identical():
    source_text = "招标全文ABC"
    outline_block = "\n\n### 目录块"
    chapter_scope_block = "\n\n### 章节范围"
    step02_parts = ["【项目信息摘要】\n摘要X"]
    summary = "摘要Y"
    scoring_points = "评分Z"
    step02_block = (
        "\n\n以下是 Step02 关键解析结果（作为补充事实来源，与文档冲突时以文档为准）：\n"
        + "\n\n".join(step02_parts)
    )
    summary_block = f"\n\n参考摘要：{summary[:800]}"
    scoring_block = f"\n参考评分点：{scoring_points[:800]}"
    want = _orig_single(
        source_text, outline_block, chapter_scope_block,
        step02_parts, summary, scoring_points,
    )
    fb = (
        facts_mod._FACTS_EXTRACT_USER_FB.replace("{source_text}", source_text)
        .replace("{outline_block}", outline_block)
        .replace("{chapter_scope_block}", chapter_scope_block)
        .replace("{step02_block}", step02_block)
        .replace("{summary_block}", summary_block)
        .replace("{scoring_block}", scoring_block)
    )
    assert fb == want


def test_no_residual_placeholders():
    from app.services.ai.prompts import has_residual_placeholders
    got = _render_single("干净全文", "", "", "", "", "")
    assert not has_residual_placeholders(got)
