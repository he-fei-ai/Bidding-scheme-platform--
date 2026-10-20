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
# ── ENHANCE 2026-10-17（脱敏覆盖面缺口 G-SEC-2）──────────────────────
# 审计复现：以下五类高频敏感信息原实现**完全不脱敏**，而它们正是招标文件
# 与系统配置里的常客，导致 desensitize=True 形同虚设：
#   银行卡号 6222021234567890123      -> 原样透传
#   Authorization: Bearer eyJhbGci...  -> 原样透传（JWT 明文外泄）
#   password=Abcd12345678              -> 原样透传
#   服务器IP 192.168.1.100             -> 原样透传
#   mysql://root:Passw0rd123@10.0.0.5  -> 连接串密码原样透传
# 均改为掩码。前后瞻沿用 BUG-FIX-PROMPT-S2 的数字边界口径（兼容 CJK 邻接）。
# 银行卡号：13~19 位（银联/Visa/MC 等主流卡号长度区间）
_BANK_CARD_PATTERN = re.compile(r"(?<!\d)\d{13,19}(?!\d)")
# Bearer / JWT：Authorization: Bearer <token> 或裸 JWT（eyJ 开头三段式）
_BEARER_PATTERN = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9_\-.=]{8,}")
_JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}\.[A-Za-z0-9_\-]{4,}")
# 口令赋值：password/ passwd/ pwd/ secret/ token/ access_token = xxx
_SECRET_ASSIGN_PATTERN = re.compile(
    r"(?i)\b(pass(?:word|wd)?|secret|token|access[_-]?token|client[_-]?secret)"
    r"(\s*[:=]\s*)(['\"]?)([^\s'\",;)]{4,})\3"
)
# IPv4（含端口）
_IPV4_PATTERN = re.compile(r"(?<!\d)((?:\d{1,3}\.){3}\d{1,3})(?::\d{1,5})?(?!\d)")
# URL 内嵌凭据：scheme://user:pass@host
_URL_CRED_PATTERN = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*://)([^:/@\s]+):([^@/\s]+)@")


def desensitize_sensitive_info(text: str) -> str:
    """对文本中的常见敏感信息做掩码脱敏。

    覆盖：邮箱、中国大陆手机号、身份证号、ENC: 加密密钥串、常见 API Key。
    本函数为 opt-in（desensitize=True 时生效），不影响默认渲染。

    BUG-FIX-PROMPT-S2：手机号/身份证改用数字前后瞻界定边界（而非 \b），
    使紧贴中文的号码同样能被脱敏（招标正文的常态）。
    BUG-FIX-PROMPT-S3：调整处理顺序——身份证(18位)先于手机号(11位)，
    否则 18 位身份证中的 11 位子串可能先被手机号规则切碎，残留 7 位明文数字。

    ENHANCE 2026-10-17（覆盖面缺口）：新增银行卡号（Luhn 校验，避免误伤订单号
    等长数字）、JWT / Bearer 凭证、password|secret|token 赋值、URL 内嵌凭据、
    IPv4 五类。顺序上「URL 凭据 → JWT → Bearer → 口令赋值 → 身份证 → 银行卡
    → 手机号」，保证长串/结构化模式先于裸数字规则，避免子串被切断后残留明文。
    """
    if not text:
        return text
    out = text
    out = _EMAIL_PATTERN.sub("[邮箱已脱敏]", out)
    # URL 内嵌凭据须先于 IPv4（否则主机 IP 先被吃掉，@ 前后结构被破坏）
    out = _URL_CRED_PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2)}:[凭据已脱敏]@", out)
    out = _JWT_PATTERN.sub("[JWT凭证已脱敏]", out)
    out = _BEARER_PATTERN.sub(lambda m: f"{m.group(1)}[凭证已脱敏]", out)
    out = _SECRET_ASSIGN_PATTERN.sub(
        lambda m: f"{m.group(1)}{m.group(2)}{m.group(3)}[口令已脱敏]{m.group(3)}", out
    )
    # 身份证优先（长串先匹配，避免被手机号规则从中切断）
    out = _ID_CARD_PATTERN.sub("[身份证号已脱敏]", out)
    # 银行卡：仅对通过 Luhn 校验的卡号脱敏，避免误伤订单号/编号等长数字串
    out = _BANK_CARD_PATTERN.sub(lambda m: "[银行卡号已脱敏]" if _luhn_ok(m.group(0)) else m.group(0), out)
    out = _CN_MOBILE_PATTERN.sub("[手机号已脱敏]", out)
    out = _ENC_KEY_PATTERN.sub("ENC:[已脱敏]", out)
    out = _API_KEY_PATTERN.sub(lambda m: "[API密钥已脱敏]", out)
    # IP 放最后：其数字形态可能被前述规则局部改写，且需排除版本号（v1.2.3）
    out = _IPV4_PATTERN.sub(lambda m: "[IP已脱敏]" if _is_ipv4(m.group(1)) else m.group(0), out)
    return out


def _luhn_ok(digits: str) -> bool:
    """Luhn 校验：区分真实银行卡号与等长业务编号，降低脱敏误伤。"""
    if not digits.isdigit() or not (13 <= len(digits) <= 19):
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = ord(ch) - 48
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _is_ipv4(candidate: str) -> bool:
    """IPv4 有效性判定：四段且每段 0~255（避免把 1.2.3.4 这类版本号当 IP）。"""
    parts = candidate.split(".")
    if len(parts) != 4:
        return False
    for p in parts:
        if not p.isdigit() or len(p) > 3 or int(p) > 255:
            return False
    return True


# ──────────────────────────────────────────────────────────────────────
# 3. 提示词注入风险检测（ENHANCE 2026-10-17）
# ──────────────────────────────────────────────────────────────────────

# 典型「指令劫持」话术：试图让模型放弃既有角色/规则。命中即计风险分。
# 说明：仅用于**检测与告警/包裹**，绝不据此直接丢弃用户内容（避免误伤正常
# 招标文本，如「本章应严格执行以下要求」）。
_INJECTION_PATTERNS: list[tuple[re.Pattern, int]] = [
    (re.compile(r"忽略(?:以上|前述|上面|之前)?(?:所有|全部)?(?:的)?(?:指令|规则|要求|约束|提示)"), 3),
    (re.compile(r"无视(?:以上|前述)?(?:所有)?(?:指令|规则|要求|约束)"), 3),
    (re.compile(r"忘记(?:你|您)?(?:之前|以上|所有)?(?:的)?(?:指令|规则|设定|身份)"), 3),
    (re.compile(r"(?:disregard|ignore)\s+(?:all\s+)?(?:previous|above|prior)\s+instructions", re.I), 3),
    (re.compile(r"你现在是|从现在起你是|请你扮演|扮演一个", re.I), 2),
    (re.compile(r"(?:system|assistant)\s*[:：]\s*", re.I), 2),   # 伪造对话角色
    (re.compile(r"输出(?:你的|系统|完整)?(?:系统)?提示词(?:全文)?|泄露(?:系统)?提示词"), 3),
    (re.compile(r"<<<[A-Z_]+_START>>>|<<<[A-Z_]+_END>>>"), 4),   # 伪造数据边界
]


def detect_injection_risk(text: str) -> tuple[int, list[str]]:
    """评估文本作为「提示词变量值」时的注入风险。

    用于可观测性与分级处置：调用方可依据风险分决定是否
    ``wrap_user_data``（边界包裹）/ 记审计日志 / 告警。

    Args:
        text: 待评估的变量值（通常为用户/外部内容）。

    Returns:
        (风险分, 命中的模式列表)。0 分表示未命中已知注入话术。
        分值仅作相对排序，不做绝对阈值断言。
    """
    if not text:
        return 0, []
    hits: list[str] = []
    score = 0
    for pattern, weight in _INJECTION_PATTERNS:
        if pattern.search(text):
            score += weight
            hits.append(pattern.pattern)
    return score, hits


def sanitize_for_injection(value: str, *, wrap_on_risk: int = 3) -> str:
    """「按风险自适应」的注入防御：中和元标记 + 风险达标时自动边界包裹。

    相比 ``sanitize_prompt_value(wrap=True)`` 无差别包裹，本函数只对**命中
    注入话术**的内容加边界——既堵住高风险输入，又避免给正常结构化载荷
    （JSON/Mermaid）套上破坏可解析性的包裹层。

    Args:
        value: 原始变量值。
        wrap_on_risk: 风险分达到该值即自动包裹数据边界。

    Returns:
        处理后的字符串。
    """
    if not value:
        return value
    out = neutralize_meta_markers(value)
    score, _ = detect_injection_risk(out)
    if score >= wrap_on_risk:
        out = wrap_user_data(out)
    return out


__all__ = [
    "neutralize_meta_markers",
    "wrap_user_data",
    "sanitize_prompt_value",
    "desensitize_sensitive_info",
    "detect_injection_risk",
    "sanitize_for_injection",
]
