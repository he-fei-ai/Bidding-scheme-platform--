"""LaTeX 公式 → 可读 Unicode 纯文本（《装饰装修专项施工方案》文档质量分析报告·修复5）。

## 为什么需要这个模块

报告「格式问题 7：公式渲染为方块」记录了如下缺陷：

    Page 82:
    \\[N = 1.2(N\\dot{\\iota}\\dot{\\iota} G + N_{Gk}) + 1.4\\sum_{i = 1}^{n}N Q k\\iota\\]
    \\[\\boxed {\\Xi}\\leq f\\]

根因：AI 在计算书章节输出 LaTeX 公式，导出链路没有 LaTeX 转换，
`\\dot{\\iota}`、`\\boxed{...}` 等命令原样进入 DOCX，读者看到的是转义乱码。

## 设计取舍

- requirements.txt 里**没有** latex2mathml / mathml2omml 等 LaTeX 栈，
  引入新依赖会扩大部署面。本模块实现一个**自包含的、面向「可读性」而非
  「排版精度」**的降级转换：公式转 Unicode 纯文本（∑、≤、下标等），
  即报告降级方案所要求的「纯文本公式」，保证计算书章节不再出现乱码。
- 转换函数对任何输入都**不抛异常**：解析失败逐级降级，最终兜底为
  「去反斜杠的原文」，不会因一个生僻命令让整章导出失败。
- 只处理数学定界符（\\[ \\]、$$、\\( \\)、成对 $），且行内 $...$ 需命中
  LaTeX 特征（含反斜杠 / ^ / _ / 花括号）才转换，避免把「$500」这类
  金额误伤。
- 转换在 `content_cleaning.clean_generated_content` 中通过
  `_apply_outside_chart_fences` 调用 —— mermaid / chart-json 围栏载荷
  天然受保护，不会被改写。
"""

from __future__ import annotations

import re

# 希腊字母（命令 → 字符）
_GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ",
    "epsilon": "ε", "varepsilon": "ε", "zeta": "ζ", "eta": "η",
    "theta": "θ", "vartheta": "θ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π",
    "rho": "ρ", "sigma": "σ", "tau": "τ", "upsilon": "υ",
    "phi": "φ", "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω",
    "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ", "Lambda": "Λ",
    "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ",
    "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
}

# 数学符号/算子（命令 → 字符）
_SYMBOLS = {
    "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠", "ne": "≠",
    "approx": "≈", "equiv": "≡", "sim": "∼", "propto": "∝",
    "pm": "±", "mp": "∓", "times": "×", "div": "÷", "cdot": "·",
    "sum": "∑", "prod": "∏", "int": "∫", "iint": "∬", "oint": "∮",
    "infty": "∞", "partial": "∂", "nabla": "∇", "forall": "∀",
    "exists": "∃", "in": "∈", "notin": "∉", "subset": "⊂",
    "subseteq": "⊆", "cup": "∪", "cap": "∩", "emptyset": "∅",
    "to": "→", "rightarrow": "→", "leftarrow": "←",
    "Rightarrow": "⇒", "Leftarrow": "⇐", "leftrightarrow": "↔",
    "ldots": "…", "cdots": "⋯", "vdots": "⋮", "dots": "…",
    "perp": "⊥", "parallel": "∥", "angle": "∠", "degree": "°",
    "therefore": "∴", "because": "∵", "ell": "ℓ", "hbar": "ℏ",
    "Re": "ℜ", "Im": "ℑ", "aleph": "ℵ", "prime": "′",
    "wedge": "∧", "vee": "∨", "oplus": "⊕", "otimes": "⊗",
    "circ": "∘", "bullet": "•", "ast": "∗", "star": "⋆",
    "min": "min", "max": "max", "log": "log", "ln": "ln",
    "exp": "exp", "sin": "sin", "cos": "cos", "tan": "tan",
    "lim": "lim", "mod": "mod",
}

# 全部「命令 → Unicode」映射（符号 + 希腊字母），供单次 token 替换使用
_KNOWN_COMMANDS = {**_SYMBOLS, **_GREEK}

# 上/下标字符映射（Unicode 上标/下标）
_SUPERSCRIPTS = {
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴",
    "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹",
    "+": "⁺", "-": "⁻", "=": "⁼", "(": "⁽", ")": "⁾",
    "n": "ⁿ", "i": "ⁱ", "a": "ₐ", "e": "ₑ", "o": "ₒ", "x": "ₓ",
}
_SUBSCRIPTS = {
    "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄",
    "5": "₅", "6": "₆", "7": "₇", "8": "₈", "9": "₉",
    "+": "₊", "-": "₋", "=": "₌", "(": "₍", ")": "₎",
    "a": "ₐ", "e": "ₑ", "h": "ₕ", "i": "ᵢ", "j": "ⱼ",
    "k": "ₖ", "l": "ₗ", "m": "ₘ", "n": "ₙ", "o": "ₒ",
    "p": "ₚ", "r": "ᵣ", "s": "ₛ", "t": "ₜ", "u": "ᵤ",
    "v": "ᵥ", "x": "ₓ",
}

# 需要剥掉外层命令、保留花括号内容的「包装命令」
_UNWRAP_COMMANDS = (
    "mathrm", "mathbf", "mathit", "mathsf", "mathtt", "mathcal",
    "mathbb", "mathfrak", "boldsymbol", "operatorname", "text",
    "textbf", "textit", "emph", "boxed", "fbox", "overline",
    "underline", "widehat", "widetilde", "bar", "hat", "tilde",
    "vec", "dot", "ddot", "check", "breve", "acute", "grave",
)

# 强调类包装命令 → Unicode 组合附加符。
# 直接剥掉会丢语义（报告案例 \\dot{\\iota}\\dot{\\iota} 会变成 ιι），
# 保留「被修饰的字符 + 组合符」可让读者看到原意（ι̇ι̇）。
_ACCENT_COMMANDS = {
    "dot": "\u0307",       # ˙ 上点
    "ddot": "\u0308",      # ¨ 上两点
    "acute": "\u0301",     # ´ 锐音符
    "grave": "\u0300",     # ` 钝音符
    "tilde": "\u0303",     # ˜ 波浪
    "hat": "\u0302",       # ^ 尖音符
    "check": "\u030C",     # ˇ 上钩
    "breve": "\u0306",     # ˘ 短音符
    "bar": "\u0304",       # ¯ 长音符
    "vec": "\u20d7",       # ⃗ 上箭头（向量）
}

# 丢弃的纯排版命令（无语义，直接删除）
_DROP_COMMANDS = (
    "left", "right", "big", "Big", "bigg", "Bigg", "displaystyle",
    "textstyle", "scriptstyle", "limits", "nolimits", "quad", "qquad",
    "hspace", "vspace", "label", "ref", "cite", "nonumber", "notag",
    "begin", "end", "phantom", "hfill", "vfill", "smallskip",
    "medskip", "bigskip", "centering", "allowbreak",
)

_BRACE_RE = re.compile(r"\{([^{}]*)\}")
_COMMAND_RE = re.compile(r"\\([a-zA-Z]+)")
_FRAC_RE = re.compile(r"\\frac\s*(\{[^{}]*\})\s*(\{[^{}]*\})")
_SQRT_RE = re.compile(
    r"\\sqrt\s*(?:\[[^\]]*\])?\s*(\{[^{}]*\}|\([^()]*\)|\[[^\]]*\])"
)
_SCRIPT_RE = re.compile(r"([_^])\s*(\{[^{}]*\}|[^\s_^\\])")



def _unwrap_command(match: re.Match) -> str:
    """`\\boxed{X}` / `\\dot{\\iota}` → 取花括号内容（包装命令降级为纯内容）。"""
    # 注意：正则只有一个捕获组 —— group(1) 即命令后的 `{...}` 或单字符。
    rest = match.group(1) or ""
    m = _BRACE_RE.match(rest)
    if m:
        return m.group(1)
    # 无花括号形态（如 \\dot X）：吃掉紧跟的单字符
    if rest:
        return rest[0]
    return ""


def _script_repl(match: re.Match) -> str:
    """`_{Gk}` / `^2` → Unicode 上下标；无法映射时退化为 `_(...)` / `^(...)`。"""
    kind, body = match.group(1), match.group(2)
    table = _SUBSCRIPTS if kind == "_" else _SUPERSCRIPTS
    if body.startswith("{") and body.endswith("}"):
        body = body[1:-1]
    if body and all(ch in table for ch in body):
        return "".join(table[ch] for ch in body)
    return f"{kind}({body})"


def _cmd_token_repl(match: re.Match) -> str:
    """完整命令 token → Unicode 字符。

    - token 命中映射表：直接替换（如 \\Xi → Ξ）。
    - token 未命中但**以**某个已知命令开头：切分替换（如 ``\\boxed{\\Xi}\\overline{a}``
      剥壳后黏成 ``\\Xia``，按最长已知前缀还原为 Ξa）。
      代价：极少数「以希腊字母开头的未知命令」会被切开，属可接受的降级取舍。
    - 其余未知命令：保留名字、去反斜杠（可读性优先）。
    """
    token = match.group(1)
    if token in _KNOWN_COMMANDS:
        return _KNOWN_COMMANDS[token]
    for length in range(len(token) - 1, 1, -1):
        head = token[:length]
        if head in _KNOWN_COMMANDS:
            return f"{_KNOWN_COMMANDS[head]}{token[length:]}"
    return token


def latex_expr_to_text(expr: str) -> str | None:
    """单条 LaTeX 公式 → Unicode 纯文本。

    Returns:
        转换后的文本；任何异常时返回 None（调用方按兜底策略处理），
        保证「一个生僻命令炸掉整章导出」不可能发生。
    """
    if not expr or not isinstance(expr, str):
        return None
    try:
        s = expr.strip()
        # 1) 定界符与空白类命令
        s = re.sub(r"\\(?:left|right)\b\s*[(\[\{|)\]\}.]?", "", s)
        s = s.replace("\\,", " ").replace("\\;", " ").replace("\\!", "")
        s = s.replace("\\ ", " ").replace("~", " ")
        # 2) \frac / \sqrt 优先（需要配对结构）
        for _ in range(8):  # 嵌套上限，防病态输入
            if "\\frac" not in s and "\\sqrt" not in s:
                break
            prev = s
            s = _FRAC_RE.sub(
                lambda m: f"({m.group(1)[1:-1]})/({m.group(2)[1:-1]})", s
            )
            s = _SQRT_RE.sub(
                lambda m: f"√({m.group(1).strip('{}()[]')})", s
            )
            if s == prev:
                break
        # 3) 包装命令（boxed/text/…）：普通包装取花括号内容；
        #    强调类（dot/ddot/vec/…）额外追加组合附加符，不丢语义
        for name in _UNWRAP_COMMANDS:
            pattern = rf"\\{name}\s*(\{{[^{{}}]*\}}|.)"
            accent = _ACCENT_COMMANDS.get(name)
            if accent:
                s = re.sub(pattern, lambda m, a=accent: _unwrap_command(m) + a, s)
            else:
                s = re.sub(pattern, _unwrap_command, s)
        # 4) 上下标
        s = _SCRIPT_RE.sub(_script_repl, s)
        # 5) 希腊字母与符号：一次性按「完整命令 token」替换（见 _cmd_token_repl）。
        s = re.sub(r"\\([a-zA-Z]+)", _cmd_token_repl, s)
        # 6) 丢弃纯排版命令（含其花括号参数）
        for name in _DROP_COMMANDS:
            s = re.sub(rf"\\{name}\b\s*(\{{[^{{}}]*\}})?", "", s)
        # 7) 未知命令：去反斜杠保留名字（可读性优先）
        s = _COMMAND_RE.sub(lambda m: m.group(1), s)
        # 8) 结构字符清理
        for a, b in (
            ("\\[", ""), ("\\]", ""), ("\\(", ""), ("\\)", ""),
            ("\\{", "{"), ("\\}", "}"), ("\\%", "%"), ("\\#", "#"),
            ("\\&", "&"), ("\\_", "_"), ("\\$", "$"), ("\\\\", " "),
        ):
            s = s.replace(a, b)
        s = s.replace("$", "")
        s = re.sub(r"\{([^{}]*)\}", r"\1", s)
        s = re.sub(r"[ \t]{2,}", " ", s)
        s = re.sub(r"\s+([,;:.，。；：）)])", r"\1", s)
        return s.strip() or None
    except Exception:
        return None


def _fallback_plain(expr: str) -> str:
    """转换失败的兜底：去掉明显噪声的反斜杠，保留可读原文（报告降级方案）。"""
    try:
        s = expr
        for name in _DROP_COMMANDS + _UNWRAP_COMMANDS:
            s = re.sub(rf"\\{name}\b", "", s)
        s = _COMMAND_RE.sub(lambda m: m.group(1), s)
        s = s.replace("\\", "").replace("$", "")
        s = re.sub(r"\{([^{}]*)\}", r"\1", s)
        return s.strip()
    except Exception:
        return expr


def _convert_one(expr: str) -> str:
    """单次转换：LaTeX→Unicode，失败则去斜杠兜底（永不抛异常）。"""
    text = latex_expr_to_text(expr)
    return text if text else _fallback_plain(expr)


def _looks_like_latex(body: str) -> bool:
    """行内 `$...$` 的保守判定：必须含 LaTeX 特征，避免误伤金额等普通文本。"""
    return "\\" in body or "^" in body or "_" in body or (
        "{" in body and "}" in body
    )


def convert_latex_in_text(text: str) -> str:
    """把文本中的 LaTeX 公式定界符段落转成可读 Unicode 文本。

    支持：``\\[...\\]``（块级）、``$$...$$``（块级）、``\\(...\\)``（行内）、
    ``$...$``（行内，需命中 :func:`_looks_like_latex` 特征）。

    任何情况下都不抛异常；转换失败按降级方案输出纯文本公式。
    """
    if not text:
        return text
    if "\\" not in text and "$" not in text:
        return text

    def _replace(match: re.Match) -> str:
        """围栏匹配器替换函数：对捕获到的公式载荷做单次降级转换。"""
        return _convert_one(match.group(1))

    # 1) \[...\] / \(...\)（DOTALL：AI 常把长公式折行）
    text = re.sub(r"\\\[([\s\S]*?)\\\]", _replace, text)
    text = re.sub(r"\\\(([\s\S]*?)\\\)", _replace, text)
    # 2) $$...$$（先于单 $，避免截断）
    text = re.sub(r"\$\$([\s\S]*?)\$\$", _replace, text)

    # 3) 行内 $...$ —— 逐个候选保守转换
    def _inline_dollar(match: re.Match) -> str:
        """行内单 $ 替换函数：命中 LaTeX 特征才转换，否则原样返回（保护金额）。"""
        body = match.group(1)
        if _looks_like_latex(body):
            return _convert_one(body)
        return match.group(0)

    text = re.sub(r"\$([^$\n]+)\$", _inline_dollar, text)
    return text
