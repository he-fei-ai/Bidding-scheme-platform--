"""收编批次 7 测试（2026-11-08）：目录自修复 + Mermaid 直修 user 侧。

覆盖 _integrate_batch7.py 注册的 3 个 key：
- OUTLINE_REPAIR_SYSTEM（目录自修复 system）
- OUTLINE_REPAIR_USER（目录自修复 user）
- MERMAID_REPAIR_DIRECT_USER（Mermaid 直修 user）

断言：注册存在性、注册文本与原硬编码/f-string 渲染逐字一致、花括号值保留。
"""
import json

from app.services.ai.prompts import get_prompt, render_prompt
from app.services.ai.prompts import _ALL_PROMPTS


def test_keys_registered():
    for k in ("OUTLINE_REPAIR_SYSTEM", "OUTLINE_REPAIR_USER", "MERMAID_REPAIR_DIRECT_USER"):
        assert k in _ALL_PROMPTS, k


def test_outline_repair_system_matches_original():
    want = "你是一个标书目录修复专家，擅长根据审核意见修复目录结构。请直接输出修复后的 JSON 数组。"
    assert _ALL_PROMPTS["OUTLINE_REPAIR_SYSTEM"]["content"] == want
    assert get_prompt("OUTLINE_REPAIR_SYSTEM").strip() == want


def test_outline_repair_user_identical_to_fstring():
    issues = ["问题1", "问题2"]
    suggestions = ["建议1"]
    current_outline = '[{"id": "1", "title": "X"}]'
    scoring_points = '[{"name": "评分"}]'
    ij = json.dumps(issues, ensure_ascii=False)
    sj = json.dumps(suggestions, ensure_ascii=False)
    want = (
        f"你是一个标书目录修复专家。以下目录审核未通过，请根据审核意见修复目录。\n\n"
        f"【审核结果】\n"
        f"issues: {ij}\n"
        f"suggestions: {sj}\n\n"
        f"【当前目录】\n{current_outline}\n\n"
        f"【评分点列表】\n{scoring_points}\n\n"
        f"请直接输出修复后的完整目录 JSON 数组，保持三级结构不变，"
        f"仅针对审核意见中的问题进行修正。严禁附加解释文字。"
    )
    got = render_prompt(
        _ALL_PROMPTS["OUTLINE_REPAIR_USER"]["content"],
        issues_json=ij, suggestions_json=sj,
        current_outline=current_outline, scoring_points=scoring_points,
    )
    assert got == want
    # 花括号值保留（单遍替换不二次处理）
    assert "【当前目录】" in got


def test_mermaid_repair_direct_user_identical_to_fstring():
    code = "flowchart TD\n A[开始]-->B{结束}"
    err = "Syntax error in graph"
    want = f"请修复以下 Mermaid 代码：\n\n{code}\n\n错误信息：{err}"
    got = render_prompt(
        _ALL_PROMPTS["MERMAID_REPAIR_DIRECT_USER"]["content"],
        current_code=code, syntax_error=err,
    )
    assert got == want
    # Mermaid 源码含花括号（判定节点）不被破坏
    assert "{结束}" in got
