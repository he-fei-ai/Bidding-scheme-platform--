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


def neutralize_meta_markers(text: str, *, inline_vars: bool = False) -> str:
    """中和变量值中的提示词元标记，使其无法被渲染逻辑重新解释。

    默认（``inline_vars=False``）处理两类**模块自有元语法**：
    - ``{shared:NAME}``：可拉取共享规则全文
    - ``__VAR__``：元占位符（大小写下划线形态）

    BUG-FIX-PROMPT-S1（误伤内容）：原实现无条件中和裸 ``{var}``，而本项目
    的渲染链路（``_registry.render_prompt`` 与 ``workflows_base._fill_template``）
    均为**单遍 re.sub**，插入的变量值不会被二次扫描，故裸 ``{var}`` 在值里
    本就**不会被重新解释**——它不是注入面。反而会破坏真实内容，实测：
        neutralize_meta_markers('flowchart TD\\n A{开始} --> B[结束]')
        -> 'flowchart TD\\n A[占位符已屏蔽] --> B[结束]'   # 判定节点语法被毁
    Mermaid 的 ``{判定}`` 判定节点、图表 JSON 的嵌套对象都可能被误伤，
    导致图表管线拿到被污染的数据。故改为默认关闭，需要时显式开启。

    Args:
        text: 原始文本。
        inline_vars: 是否额外中和裸 ``{var}``。默认 False（仅中和模块自有
            元语法）。仅当确有「值会被再次渲染」的非常规场景时才开启。
    """
    if not text:
        return text
    # 先处理 {shared:...}（可拉取其它共享规则内容）
    text = _SHARED_REF_PATTERN.sub("[共享规则引用已屏蔽]", text)
    # 再处理 __VAR__ 元占位符
    text = _META_VAR_PATTERN.sub("[元占位符已屏蔽]", text)
    # 裸 {var} 仅在显式开启时中和（默认关闭，避免破坏 Mermaid/JSON，见上）
    if inline_vars:
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
    inline_vars: bool = False,
) -> str:
    """对注入提示词的变量值做安全清洗。

    Args:
        value: 原始变量值（常为用户/外部内容）。
        neutralize: 是否中和元标记（默认开，纵深防御）。
        wrap: 是否用数据边界包裹（默认关，避免破坏需解析的结构化数据）。
        inline_vars: 是否额外中和裸 ``{var}``（默认关，见
            ``neutralize_meta_markers`` 的 BUG-FIX-PROMPT-S1 说明）。

    Returns:
        清洗后的字符串。
    """
    if not value:
        return value
    out = value
    if neutralize:
        out = neutralize_meta_markers(out, inline_vars=inline_vars)
    if wrap:
        out = wrap_user_data(out)
    return out


# ──────────────────────────────────────────────────────────────────────
# 2. 敏感信息脱敏
# ──────────────────────────────────────────────────────────────────────

_EMAIL_PATTERN = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# BUG-FIX-PROMPT-S2（CJK 邻接漏脱敏）：原用 \b 界定手机号/身份证边界。Python 的
# re 默认 Unicode 语义下，中日韩汉字属于 \w，故「联系人13800138000」「身份证
# 11010119900307123X号」这类**紧贴中文**的号码左右都不存在 \b，规则整体失配，
# 完全不脱敏（实测复现）。招标正文里号码与中文之间通常无空格，是最常见形态，
# 等于脱敏对本场景形同虚设。改为用「非数字」负向前后瞻替代 \b：
#   - 前后不是数字 -> 允许中文/标点/空格邻接，同时仍能避免在更长数字串中误命中子串
_EMAIL_BOUNDARY_NOTE = "前后瞻 (?<!\\d)/(?!\\d) 取代 \\b，兼容 CJK 邻接且防止长数字串子串误命中"
_CN_MOBILE_PATTERN = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
# 身份证号（18 位，末位可为 X）
_ID_CARD_PATTERN = re.compile(r"(?<!\d)\d{17}[\dXx](?![\dXx])")
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

    BUG-FIX-PROMPT-S2：手机号/身份证改用数字前后瞻界定边界（而非 \b），
    使紧贴中文的号码同样能被脱敏（招标正文的常态）。
    BUG-FIX-PROMPT-S3：调整处理顺序——身份证(18位)先于手机号(11位)，
    否则 18 位身份证中的 11 位子串可能先被手机号规则切碎，残留 7 位明文数字。
    """
    if not text:
        return text
    out = text
    out = _EMAIL_PATTERN.sub("[邮箱已脱敏]", out)
    # 身份证优先（长串先匹配，避免被手机号规则从中切断）
    out = _ID_CARD_PATTERN.sub("[身份证号已脱敏]", out)
    out = _CN_MOBILE_PATTERN.sub("[手机号已脱敏]", out)
    out = _ENC_KEY_PATTERN.sub("ENC:[已脱敏]", out)
    out = _API_KEY_PATTERN.sub(lambda m: "[API密钥已脱敏]", out)
    return out


__all__ = [
    "neutralize_meta_markers",
    "wrap_user_data",
    "sanitize_prompt_value",
    "desensitize_sensitive_info",
]
