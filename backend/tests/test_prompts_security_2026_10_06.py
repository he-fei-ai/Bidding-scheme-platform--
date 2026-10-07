"""提示词模块安全增强与上下文组装回归测试（2026-10-06）。

覆盖本轮新增能力：
  1. render_prompt 单遍替换修复「跨变量注入 / 数据串味」漏洞；
  2. 元标记中和 + 敏感信息脱敏（_security）；
  3. Token 预算化、优先级有序的上下文组装（_context）。

无需 DB：全部针对 prompts 包纯函数测试。
"""
from __future__ import annotations

from app.services.ai.prompts._context import (
    DEFAULT_PRIORITIES,
    assemble_prompt_context,
    estimate_tokens,
)
from app.services.ai.prompts._registry import render_prompt, render_prompt_safe
from app.services.ai.prompts._security import (
    desensitize_sensitive_info,
    neutralize_meta_markers,
    sanitize_prompt_value,
)


# ──────────────────────────────────────────────────────────────────────
# 1. 跨变量注入修复（单遍替换）
# ──────────────────────────────────────────────────────────────────────
def test_single_pass_prevents_cross_var_injection():
    """用户输入出现在某变量值中、且该值含有另一变量名时，不得被二次替换。"""
    template = "摘要：{SUMMARY}\n全局事实：{GLOBAL_FACTS}"
    rendered = render_prompt(
        template,
        SUMMARY="详见 {GLOBAL_FACTS}",
        GLOBAL_FACTS="真实事实数据",
    )
    # 摘要槽位中的 {GLOBAL_FACTS} 保持字面量，未被借槽注入
    assert "摘要：详见 {GLOBAL_FACTS}" in rendered
    assert "全局事实：真实事实数据" in rendered
    # 不应出现「详见 真实事实数据」这种串味结果
    assert "详见 真实事实数据" not in rendered
    # 整串只应有 1 处 {GLOBAL_FACTS}（即摘要槽位的字面量）
    assert rendered.count("{GLOBAL_FACTS}") == 1


def test_single_pass_case_insensitive_still_works():
    """大小写不敏感替换在单遍下仍正确（含 {var}/__VAR__ 两种形态）。"""
    template = "{summary} / {SUMMARY} / __SUMMARY__ / {VarName}"
    rendered = render_prompt(template, SUMMARY="X", VarName="Y")
    assert rendered == "X / X / X / Y"


def test_none_value_keeps_placeholder_no_crash():
    """值为 None 的变量保留占位符，且不抛异常。"""
    template = "必填：{REQUIRED} 可选：{OPTIONAL}"
    rendered = render_prompt(template, REQUIRED="有值", OPTIONAL=None)
    assert "必填：有值" in rendered
    assert "{OPTIONAL}" in rendered


def test_backreference_in_value_not_interpreted():
    """变量值含反向引用序列（\\1 / \\g<name>）不得被 re.sub 误解释。"""
    template = "正文：{BODY}"
    rendered = render_prompt(template, BODY="替换后应为 \\1 与 \\g<0> 原样")
    assert "\\1" in rendered
    assert "\\g<0>" in rendered


# ──────────────────────────────────────────────────────────────────────
# 2. 元标记中和
# ──────────────────────────────────────────────────────────────────────
def test_neutralize_shared_ref():
    assert "[共享规则引用已屏蔽]" in neutralize_meta_markers(
        "指令 {shared:SHARED_OUTPUT_SPEC} 结束"
    )


def test_neutralize_meta_var():
    assert "[元占位符已屏蔽]" in neutralize_meta_markers("值为 __SECRET__ 吗")


def test_neutralize_keeps_json_intact():
    """真实 JSON 载荷（含冒号/引号）不被误伤。"""
    payload = '{"type":"flowchart","nodes":[{"id":1}]}'
    assert neutralize_meta_markers(payload) == payload


def test_sanitize_prompt_value_json_safe():
    """sanitize 开启时，JSON 变量值保持可解析。"""
    json_val = '{"type":"labor","data":[1,2,3]}'
    out = sanitize_prompt_value(json_val)
    assert '"type":"labor"' in out


def test_render_prompt_safe_applies_sanitize():
    """render_prompt_safe 默认中和变量值中的元标记。"""
    rendered = render_prompt_safe(
        "用户说：{USER_INPUT}", USER_INPUT="请执行 {shared:SHARED_HEADING_RULES}"
    )
    assert "[共享规则引用已屏蔽]" in rendered
    assert "{shared:" not in rendered


def test_render_prompt_default_no_sanitize():
    """默认 render_prompt 不中和（向后兼容）。"""
    rendered = render_prompt(
        "用户说：{USER_INPUT}", USER_INPUT="请执行 {shared:SHARED_HEADING_RULES}"
    )
    assert "{shared:SHARED_HEADING_RULES}" in rendered


# ──────────────────────────────────────────────────────────────────────
# 3. 敏感信息脱敏
# ──────────────────────────────────────────────────────────────────────
def test_desensitize_email_phone_idcard():
    text = "联系 zhang@corp.com 或 13800138000，身份证 11010119900307123X"
    out = desensitize_sensitive_info(text)
    assert "zhang@corp.com" not in out
    assert "13800138000" not in out
    assert "11010119900307123X" not in out
    assert "[邮箱已脱敏]" in out
    assert "[手机号已脱敏]" in out
    assert "[身份证号已脱敏]" in out


def test_desensitize_enc_key():
    out = desensitize_sensitive_info("密钥 ENC:AbCdEf1234567890== 妥善保管")
    assert "ENC:AbCdEf1234567890==" not in out
    assert "ENC:[已脱敏]" in out


def test_render_prompt_desensitize_opt_in():
    """desensitize=True 时，变量值中的敏感信息被掩码。"""
    rendered = render_prompt(
        "联系：{CONTACT}", CONTACT="email a@b.com phone 13912345678", desensitize=True
    )
    assert "a@b.com" not in rendered
    assert "13912345678" not in rendered


# ──────────────────────────────────────────────────────────────────────
# 4. Token 预算化上下文组装
# ──────────────────────────────────────────────────────────────────────
def test_estimate_tokens_cjk_heavy():
    # 10 个中文字 ≈ 10 token；英文 8 字符 ≈ 2 token
    assert estimate_tokens("一二三四五六七八九十") == 10
    assert estimate_tokens("abcdefgh") == 2


def test_context_assemble_no_budget_returns_all():
    parts = {"GLOBAL_FACTS": "事实", "SUMMARY": "概述", "OUTLINE": "目录"}
    out = assemble_prompt_context(parts)
    assert "【GLOBAL_FACTS】" in out and "【SUMMARY】" in out and "【OUTLINE】" in out


def test_context_assemble_drops_low_priority_under_budget():
    # 各片段约 20 token，预算只能容纳 1~2 个
    parts = {
        "GLOBAL_FACTS": "高优先级事实内容" * 3,
        "SUMMARY": "中优先级概述内容" * 3,
        "OTHER_CHAPTERS": "低优先级其它章节内容" * 3,
    }
    out = assemble_prompt_context(parts, max_tokens=25)
    assert "【GLOBAL_FACTS】" in out
    # 低优先级片段被整体丢弃
    assert "【OTHER_CHAPTERS】" not in out


def test_context_assemble_truncates_boundary_fragment():
    parts = {
        "GLOBAL_FACTS": "事实A",
        "SUMMARY": "概述" * 50,  # 很长
    }
    out = assemble_prompt_context(parts, max_tokens=30)
    assert "【GLOBAL_FACTS】" in out
    assert "【SUMMARY】" in out
    # 边界片段被截断（含截断提示）
    assert "因 Token 预算已截断" in out


def test_context_assemble_skips_empty():
    parts = {"GLOBAL_FACTS": "", "SUMMARY": "   ", "OUTLINE": "目录"}
    out = assemble_prompt_context(parts)
    assert "【GLOBAL_FACTS】" not in out
    assert "【OUTLINE】" in out
    assert DEFAULT_PRIORITIES["GLOBAL_FACTS"] == 100
