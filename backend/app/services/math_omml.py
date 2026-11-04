"""LaTeX → Word OMML 公式渲染层（2026-11-04 导出文档模块加固轮·遗留项 1）。

背景：导出模块此前对正文中的 LaTeX 公式（``$E=mc^2$`` / ``$$\\frac{a}{b}$$``）
一律按纯文本写出，Word 里呈现为带定界符的原文——标书技术方案章节出现公式时
观感与合规性均差。本模块补齐「文本 → 公式」渲染层：导出时把公式片段转换为
OMML（Office Math Markup Language）原生公式对象插入 DOCX；HTML 通道输出
MathML（现代浏览器原生渲染）。

设计原则（与导出模块「无降级/不伪造」纪律一致）：

1. **纯转换不造数据**：只做 LaTeX→OMML/MathML 格式转换，任何环节失败一律
   返回 None，调用方保持原文本（与历史行为逐字一致，零回归）。
2. **依赖可选**：latex2mathml / mathml2omml 未安装时整体降级为纯文本呈现
   （仅 debug 日志一次性提示），导出主链路永不因公式层异常中断。
3. **误报防护**：标书正文里 ``$`` 可能是价格符号（"单价 $100 … $200"）。
   仅当片段**同时满足**以下条件才替换，否则保持原文本：
   - 含数学特征字符（``\\`` 命令 / ``^`` 上标 / ``_`` 下标）；
   - 不含 CJK 字符（公式中不应出现汉字；"100 和 200" 这类价格误配直接排除）；
   - latex2mathml + mathml2omml 转换全程成功。
4. **输出受控**：转换产物经 ``<script`` 注入守卫后才进入 HTML（MathML 直嵌），
   DOCX 侧经 python-docx ``parse_xml``（XML 合法性由解析器保证）。

对外接口：
  - ``split_math_segments(text)``        文本 → [(kind, payload, is_display)] 段序列
  - ``latex_to_omml_xml(latex, display)`` LaTeX → OMML XML 字符串（含 m 命名空间声明）
  - ``latex_to_mathml_html(latex)``       LaTeX → MathML 字符串（HTML 直嵌用）
  - ``math_deps_available()``             依赖可用性探测（进程内缓存）
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

#: OMML 命名空间（mathml2omml 产物不带声明，插入 XML 前补齐）
OMML_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"

# ---------------------------------------------------------------------------
# 依赖可用性（可选依赖，进程内缓存；缺失时整体降级纯文本）
# ---------------------------------------------------------------------------

_math_deps_state: tuple[bool, object, object] | None = None


def math_deps_available() -> bool:
    """探测 latex2mathml / mathml2omml 是否可用（进程内缓存一次结果）。"""
    global _math_deps_state
    if _math_deps_state is None:
        try:
            import latex2mathml.converter as _l2m  # noqa: F401
            import mathml2omml as _m2o  # noqa: F401

            _math_deps_state = (True, _l2m, _m2o)
        except ImportError:
            logger.debug(
                "公式渲染依赖缺失（latex2mathml/mathml2omml），公式将以纯文本导出。"
                "如需 Word 原生公式，请安装：pip install latex2mathml mathml2omml"
            )
            _math_deps_state = (False, None, None)
    return _math_deps_state[0]


# ---------------------------------------------------------------------------
# 数学片段切分（$...$ 行内 / $$...$$ 独立公式；尊重 \$ 转义）
# ---------------------------------------------------------------------------

#: 行内公式：$...$ 内不得跨行；独立公式：$$...$$ 允许跨行。
#: 交替顺序重要：先匹配 $$ 防止 $ 分割。(?<!\\) 排除 \$ 转义。
_MATH_TOKEN_RE = re.compile(r"\$\$((?:[^$]|\$(?!\$))+?)\$\$|(?<!\\)\$([^$\n]+?)\$")

#: 数学特征：含 LaTeX 命令 / 上下标才值得尝试转换（价格符号误配的强排除）
_MATHISH_RE = re.compile(r"[\\^_]")

#: 公式片段不应包含 CJK（"单价 $100 和 $200" 这类误配的核心排除）
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

#: 单个公式片段长度上限（防止把整段误配文本送去转换）
_MAX_MATH_SEGMENT = 400


def split_math_segments(text: str) -> list[tuple[str, str, bool]]:
    """把正文切分为「文本段 / 公式段」序列。

    Returns:
        [(kind, payload, is_display)]，kind ∈ {"text", "math"}；
        math 段 payload 是**已剥离定界符**的 LaTeX 源，is_display 表示 $$ 形态。
        无任何候选片段时返回 [("text", 原文, False)]（调用方走历史路径）。

    说明：本函数只做**切分**，不做数学特征校验（校验在转换侧逐段进行，
    校验失败的段由调用方按原文本回填——包括定界符）。
    """
    if not text or "$" not in text:
        return [("text", text, False)] if text else []

    segments: list[tuple[str, str, bool]] = []
    last = 0
    found = False
    for m in _MATH_TOKEN_RE.finditer(text):
        found = True
        if m.start() > last:
            segments.append(("text", text[last:m.start()], False))
        if m.group(1) is not None:
            segments.append(("math", m.group(1), True))
        else:
            segments.append(("math", m.group(2), False))
        last = m.end()
    if not found:
        return [("text", text, False)]
    if last < len(text):
        segments.append(("text", text[last:], False))
    return segments


# ---------------------------------------------------------------------------
# LaTeX → OMML / MathML 转换（全程可失败，失败返回 None）
# ---------------------------------------------------------------------------


def _sanitize_latex(latex: str) -> str:
    """剥离 LaTeX 源中的 XML 不安全控制字符（复用导出侧同款字符集语义）。"""
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]", "", latex or "")


def latex_to_omml_xml(latex: str, display: bool = False) -> str | None:
    """LaTeX → OMML XML 字符串（根节点 <m:oMath[Para]>，带 m 命名空间声明）。

    失败（依赖缺失 / 语法非法 / 转换器异常 / 空产物）一律返回 None。
    display=True 时包一层 <m:oMathPara>（Word「独立公式」标准形态）。
    """
    if not math_deps_available():
        return None
    latex = _sanitize_latex(latex).strip()
    if not latex or len(latex) > _MAX_MATH_SEGMENT:
        return None
    if not _MATHISH_RE.search(latex) or _CJK_RE.search(latex):
        return None
    try:
        _ok, l2m, m2o = _math_deps_state
        mathml = l2m.convert(latex)  # type: ignore[union-attr]
        if not mathml or "<math" not in mathml:
            return None
        omml = m2o.convert(mathml)  # type: ignore[union-attr]
        if not omml or "<m:oMath" not in omml:
            return None
        omml = omml.replace("<m:oMath>", f'<m:oMath xmlns:m="{OMML_NS}">', 1)
        if display:
            omml = f'<m:oMathPara xmlns:m="{OMML_NS}">{omml}</m:oMathPara>'
        return omml
    except Exception as e:
        logger.debug("公式 OMML 转换失败（保持原文本）: latex=%.60s err=%s", latex, e)
        return None


#: HTML 侧注入守卫：MathML 产物出现脚本标记时按原文本回退（防御性）
_HTML_UNSAFE_TOKENS = ("<script", "javascript:", "onerror", "onload")


def latex_to_mathml_html(latex: str) -> str | None:
    """LaTeX → MathML 字符串（HTML 通道直嵌；浏览器原生渲染公式）。

    失败语义同 latex_to_omml_xml：None = 保持原文本。
    """
    if not math_deps_available():
        return None
    latex = _sanitize_latex(latex).strip()
    if not latex or len(latex) > _MAX_MATH_SEGMENT:
        return None
    if not _MATHISH_RE.search(latex) or _CJK_RE.search(latex):
        return None
    try:
        _ok, l2m, _m2o = _math_deps_state
        mathml = l2m.convert(latex)  # type: ignore[union-attr]
        if not mathml or "<math" not in mathml:
            return None
        _low = mathml.lower()
        if any(tok in _low for tok in _HTML_UNSAFE_TOKENS):
            logger.debug("公式 MathML 产物含不安全标记，按原文本回退: %.60s", latex)
            return None
        return mathml
    except Exception as e:
        logger.debug("公式 MathML 转换失败（保持原文本）: latex=%.60s err=%s", latex, e)
        return None
