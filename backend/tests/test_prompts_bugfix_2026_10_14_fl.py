"""提示词模块 · 花括号口径修复回归测试（2026-10-14 BUG-PROMPT-F1）。

BUG-PROMPT-F1：CONSISTENCY_AGENT_USER 收编进注册表前是 `_AGENT_USER_HDR.format(...)`
形态，JSON 示例按 str.format 约定写成双花括号 `{{ }}`。收编后主渲染路径改为
get_prompt -> render_prompt（单遍 re.sub，不做 `{{`→`{` 反转义），于是双花括号被
原样发给模型，AI 看到的输出格式示例是畸形的 `{{"done": ...}}`，且与 DB 不可用时
`_AGENT_USER_HDR.format` fallback 产出的单花括号不一致，可能诱导模型回显双花括号
触发 _parse_agent_json 的 json.loads 失败。

本组用例：
 1. 硬编码模板本身不含 str.format 式双花括号转义（防回归再引入）。
 2. render_prompt 渲染产物为合法单花括号 JSON 示例，四个变量正常注入、无残留。
 3. 主路径（render_prompt）与 fallback（_AGENT_USER_HDR.format）产物完全一致。
 4. 模板变量集合仍只含 4 个业务变量（裸 JSON 花括号未被误判为占位符）。
"""

from __future__ import annotations

import pytest

from app.services.ai.prompts import (
    _ALL_PROMPTS,
    extract_variables,
    has_residual_placeholders,
    render_prompt,
)


VAR_KWARGS = {
    "global_facts": "【全局事实样例】工期 300 天",
    "conflicts_text": "【冲突】章节A 与 章节B 人数不一致",
    "chapter_index": "5\t安全文明施工\t1200字",
    "prev_feedback": "（本轮暂无操作记录）",
}


class TestConsistencyAgentBraceCaliber:
    def test_hardcode_has_no_format_style_escaped_braces(self):
        """注册表硬编码模板不得残留 str.format 式 `{{`/`}}` 转义。"""
        content = _ALL_PROMPTS["CONSISTENCY_AGENT_USER"]["content"]
        assert "{{" not in content, "硬编码模板重新引入了 {{ 转义（F1 回归）"
        assert "}}" not in content, "硬编码模板重新引入了 }} 转义（F1 回归）"

    def test_render_prompt_yields_valid_single_brace_json_example(self):
        """主渲染路径必须产出合法单花括号 JSON 示例，且四个变量全部注入。"""
        out = render_prompt(
            _ALL_PROMPTS["CONSISTENCY_AGENT_USER"]["content"], **VAR_KWARGS
        )
        # 合法单花括号 JSON 示例存在
        assert '{"done": bool' in out
        assert '{"action": "search"' in out
        assert '[{"section_id": "5"' in out
        # 绝不含双花括号（否则模型可能回显导致下游解析失败）
        assert "{{" not in out
        # 变量注入完整、无残留占位符
        assert "工期 300 天" in out
        assert "安全文明施工" in out
        assert not has_residual_placeholders(out), f"渲染后仍有残留占位符: {out}"

    def test_primary_and_fallback_renderings_are_identical(self):
        """BUG-PROMPT-F1 核心：主路径与 DB 不可用 fallback 必须给 AI 完全一致内容。"""
        primary = render_prompt(
            _ALL_PROMPTS["CONSISTENCY_AGENT_USER"]["content"], **VAR_KWARGS
        )
        from app.services.agent_repair import _AGENT_USER_HDR

        fallback = _AGENT_USER_HDR.format(**VAR_KWARGS)
        assert primary == fallback, (
            "主路径与 fallback 渲染结果不一致：管理员改注册表将无法预测生效内容"
        )

    def test_variables_set_unchanged(self):
        """裸 JSON 花括号不得被误判为变量：模板声明变量仍恰为 4 个业务变量。"""
        content = _ALL_PROMPTS["CONSISTENCY_AGENT_USER"]["content"]
        vars_found = set(extract_variables(content))
        assert vars_found == {
            "global_facts",
            "conflicts_text",
            "chapter_index",
            "prev_feedback",
        }, f"变量集合异常: {sorted(vars_found)}"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v", "-p", "no:randomly"]))
