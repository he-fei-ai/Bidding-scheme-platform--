"""提示词安全增强（2026-10-06）。

提供两类防御，用于「用户/外部内容注入提示词变量值」场景：

1. **元标记中和（meta-marker neutralization）**
   变量值里若包含 `{shared:NAME}`、`__VAR__` 这类提示词模板语法，
   在本模块统一渲染路径（render_prompt 单遍替换）下虽已不会被二次替换，
   但仍属于「提示词注入」风险面（一旦未来渲染逻辑回归多遍扫描即会复活）。
   这里显式中和，做到纵深防御。

2. **敏感信息脱敏（sensitive-info desensitization）**
   变量值里常见的邮箱、手机号、身份证号、加密密钥串（ENC:/sk-/api_key=）
   在发送给 LLM 前做掩码，降低敏感数据外泄面。

两个能力均为**显式 opt-in**：render_prompt 默认 sanitize=False /
desensitize=False，向后兼容历史调用方；注入用户内容的调用方应使用
render_prompt_safe（sanitize=True）。

本模块只依赖标准库，避免与 prompts 包其它模块形成导入环。
"""

from __future__ import annotations

import re

# ──────────────────────────────────────────────────────────────────────
# 1. 元标记中和
# ──────────────────────────────────────────────────────────────────────

# 匹配 {shared:NAME} 引用（NAME 为字母/数字/下划线）
_SHARED_REF_PATTERN = re.compile(r"\{shared:[A-Za-z_]\w{1,}\}")

# 匹配 __UPPER_SNAKE__ 形式的元占位符（不要误伤 JSON 的普通花括号）
_META_VAR_PATTERN = re.compile(r"__[A-Z_][A-Z0-9_]*__")

# 匹配 {VarName} / {varname} 形式的占位符（仅当出现在「变量值」内部时中和，
# 普通 JSON 的 {"k": ...} 不会被命中，因为要求键名为字母开头且整体形如 {x}）。
# 为避免误伤 JSON 对象，这里要求占位符后紧跟 '}' 或空白，降低误判。
_INLINE_VAR_PATTERN = re.compile(r"\{[A-Za-z_]\w{1,}\}")


def neutralize_meta_markers(text: str) -> str:
    """中和变量值中的提示词元标记，使其无法被渲染逻辑重新解释。

    仅处理 `{shared:NAME}`、`__VAR__` 与裸 `{var}` 形式的标记；
    **不会**触碰真实 JSON 对象（如 {"type":"flowchart"}），因为 JSON 的
    花括号内含冒号/引号，与 `{var}` 单 token 形态不符。
    """
    if not text:
        return text
    # 先处理 {shared:...}（最危险，可拉取其它共享规则内容）
    text = _SHARED_REF_PATTERN.sub("[共享规则引用已屏蔽]", text)
    # 再处理 __VAR__ 元占位符
    text = _META_VAR_PATTERN.sub("[元占位符已屏蔽]", text)
    # 最后处理裸 {var}（仅当它是独立 token，避免误伤 JSON）
    text = _INLINE_VAR_PATTERN.sub("[占位符已屏蔽]", text)
    return text


def wrap_user_data(text: str, *, start: str = "<<<USER_DATA_START>>>", end: str = "<<<USER_DATA_END>>>") -> str:
    """用明确边界包裹用户数据，提示模型将其视作数据而非指令（注入防御）。

    仅建议用于「自由文本型用户内容」（如招标正文、项目概述），不要用于
    需要模型解析的结构化 JSON 载荷。
    """
    if not text:
        return text
    return f"{start}\n{text}\n{end}"


def sanitize_prompt_value(
    value: str,
    *,
    neutralize: bool = True,
    wrap: bool = False,
) -> str:
    """对注入提示词的变量值做安全清洗。

    Args:
        value: 原始变量值（常为用户/外部内容）。
        neutralize: 是否中和元标记（默认开，纵深防御）。
        wrap: 是否用数据边界包裹（默认关，避免破坏需解析的结构化数据）。

    Returns:
        清洗后的字符串。
    """
    if not value:
        return value
    out = value
    if neutralize:
        out = neutralize_meta_markers(out)
    if wrap:
        out = wrap_user_data(out)
    return out


# ──────────────────────────────────────────────────────────────────────
# 2. 敏感信息脱敏
# ──────────────────────────────────────────────────────────────────────

_EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_CN_MOBILE_PATTERN = re.compile(r"\b1[3-9]\d{9}\b")
# 身份证号（18 位，末位可为 X）
_ID_CARD_PATTERN = re.compile(r"\b\d{17}[\dXx]\b")
# 加密密钥串：ENC: 前缀（项目密钥存储政策）
_ENC_KEY_PATTERN = re.compile(r"\bENC:[A-Za-z0-9+/=]{8,}\b")
# 通用 API Key：sk- 开头长串 / api_key=/apiKey: 赋值
_API_KEY_PATTERN = re.compile(
    r"(sk-[A-Za-z0-9]{8,})|(api[_-]?key\s*[:=]\s*['\"]?[A-Za-z0-9_\-]{8,})",
    re.IGNORECASE,
)


def desensitize_sensitive_info(text: str) -> str:
    """对文本中的常见敏感信息做掩码脱敏。

    覆盖：邮箱、中国大陆手机号、身份证号、ENC: 加密密钥串、常见 API Key。
    本函数为 opt-in（desensitize=True 时生效），不影响默认渲染。
    """
    if not text:
        return text
    out = text
    out = _EMAIL_PATTERN.sub("[邮箱已脱敏]", out)
    out = _CN_MOBILE_PATTERN.sub("[手机号已脱敏]", out)
    out = _ID_CARD_PATTERN.sub("[身份证号已脱敏]", out)
    out = _ENC_KEY_PATTERN.sub("ENC:[已脱敏]", out)
    out = _API_KEY_PATTERN.sub(lambda m: "[API密钥已脱敏]", out)
    return out


__all__ = [
    "neutralize_meta_markers",
    "wrap_user_data",
    "sanitize_prompt_value",
    "desensitize_sensitive_info",
]
