"""工具模块，提供通用工具函数、性能监控和 JSON 解析工具。"""

import functools
import re

from .json_parser import extract_json_from_text, safe_json_parse


# 预编译正则表达式，避免每次调用时重复编译（性能优化）
_COUNT_WORDS_RE = re.compile(r"[\u4e00-\u9fff]|[a-zA-Z0-9]+")

# ---------------------------------------------------------------------------
# R1 可读字数口径（移植自易标 wordCount.cjs / wordCount.ts）
# ---------------------------------------------------------------------------
# 易标报告第 3 节：主进程与渲染层两份实现算法逐行一致，保证生成侧、校验侧、
# 展示侧统计不出现偏差。本平台只保留这一份实现（app/utils 为平台历史约定的
# 「权威口径」），workflows_sections.py 与 routers/word_count.py 必须从这里取。
#
# 口径定义：**中文字符数 + 西文词数 = 字数**。标点、格式符号、图片、代码、
# URL 一律不计。CJK 区间取 \u3400-\u9fff（易标同款，比本平台历史实现的
# \u4e00-\u9fff 多覆盖 CJK 扩展 A 区 10 个码位段的生僻字）。
_READABLE_FENCE_RE = re.compile(r"```[\s\S]*?(?:```|$)")
_READABLE_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_READABLE_URL_RE = re.compile(r"(?:https?|file|yibiao-asset)://\S+", re.IGNORECASE)
_READABLE_HTML_COMMENT_RE = re.compile(r"<!--[\s\S]*?-->")
_READABLE_HTML_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_READABLE_HTML_TAG_RE = re.compile(r"</?[A-Za-z][A-Za-z0-9]*(?:\s[^<>]*)?/?>")
_READABLE_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
# 引用式链接：正文里的 `[文字][标签]` 与定义行 `[标签]: url ...`
_READABLE_REF_LINK_USE_RE = re.compile(r"\[([^\]]*)\]\[([^\]]*)\]")
_READABLE_REF_LINK_DEF_RE = re.compile(r"(?:\A|\s)\[([^\]]+)\]:[^\n]*")
_READABLE_TABLE_SEP_RE = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$", re.MULTILINE
)
_READABLE_HEADING_RE = re.compile(r"^\s*#{1,6}\s+", re.MULTILINE)
_READABLE_QUOTE_RE = re.compile(r"^\s*(?:>\s?)+", re.MULTILINE)
_READABLE_ORDERED_LIST_RE = re.compile(r"^\s*\d{1,4}[.)]\s+", re.MULTILINE)
_READABLE_UNORDERED_LIST_RE = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
_READABLE_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_READABLE_WESTERN_TOKEN_RE = re.compile(r"[A-Za-z0-9]+(?:[-_./][A-Za-z0-9]+)*")
_READABLE_Nbsp_RE = re.compile(r"&nbsp;", re.IGNORECASE)
_READABLE_WHITESPACE_RE = re.compile(r"\s+")
# 行内标记符（与易标 wordCount.cjs 的字符清理集合逐字一致）：
# 反引号、~ * _ # > [ ] ( ) { } |。注意**不含** `-` / `.` / `_` 语义上的冲突项——
# `_` 属于强调标记必须剔除，而 `-` 与 `.` 是西文 token 的连接符
# （a-b、1.2.3、v1.2.3 必须被记作 1 个词，不能拆开）。
_READABLE_MARKUP_CHARS = "`~*_#>[](){}|"


@functools.lru_cache(maxsize=256)
def count_words(text: str) -> int:
    """统计中英文混合文本的字数（中文字符逐个计数，英文/数字按词计数）

    性能优化：
    - 使用预编译正则表达式（全局常量）
    - LRU 缓存：重复内容直接返回缓存结果。
      性能修复：maxsize 从 2048 降至 256——旧值最长可持有 2048 份完整正文
      （数十 MB 内存），而正则扫描 5KB 文本耗时不足 1ms，缓存miss代价低，
      缩小上限可控制内存占用且几乎不影响命中率（同一内容的重复计数是突发性的）
    """
    if not text:
        return 0
    return len(_COUNT_WORDS_RE.findall(text))


def strip_readables_markup(text: str) -> str:
    """按易标 R1 口径剥离 Markdown / 图片 / URL / HTML / 代码块，返回纯文本。

    公开为函数的原因：字数**展示侧**（前端编辑器、导出预览、SSE 推送的
    ``readable_word_count``）与**校验侧**（生成后校验、字数审计端点）都必须
    用同一份剥离逻辑；如果只暴露 :func:`count_readable_words` 的计数结果，
    展示侧要拿剥离后的文本（例如高亮、分页计算）时只能各自重写一遍，
    就会重新出现易标报告 3 节指出的「两份实现漂移」问题。
    """
    cleaned = normalize_newlines(text)
    # 1. 围栏代码块（含未闭合围栏，直接吃到文本末尾）
    cleaned = _READABLE_FENCE_RE.sub("", cleaned)
    # 2. Markdown 图片
    cleaned = _READABLE_IMAGE_RE.sub("", cleaned)
    # 3. URL（含 file / yibiao-asset 自定义协议）
    cleaned = _READABLE_URL_RE.sub(" ", cleaned)
    # 4. HTML 注释与标签（<br> 折成空格，其他标签整体剔除）
    cleaned = _READABLE_HTML_COMMENT_RE.sub(" ", cleaned)
    cleaned = _READABLE_HTML_BR_RE.sub(" ", cleaned)
    cleaned = _READABLE_HTML_TAG_RE.sub(" ", cleaned)
    # 5. 链接保文字：[文字][标签] 与 [文字](url) 只留锚文字，定义行整行删除
    cleaned = _READABLE_REF_LINK_USE_RE.sub(r"\1", cleaned)
    cleaned = _READABLE_REF_LINK_DEF_RE.sub("", cleaned)
    cleaned = _READABLE_MD_LINK_RE.sub(r"\1", cleaned)
    # 6. 行归一化：表格分隔行整行删除，标题 # / 引用 > / 列表符号 / 表格竖线
    cleaned = _READABLE_TABLE_SEP_RE.sub("", cleaned)
    cleaned = _READABLE_HEADING_RE.sub("", cleaned)
    cleaned = _READABLE_QUOTE_RE.sub("", cleaned)
    cleaned = _READABLE_ORDERED_LIST_RE.sub("", cleaned)
    cleaned = _READABLE_UNORDERED_LIST_RE.sub("", cleaned)
    cleaned = cleaned.replace("|", " ").replace("```", " ")
    for ch in _READABLE_MARKUP_CHARS:
        cleaned = cleaned.replace(ch, " ")
    # 7. 实体与空白折叠
    cleaned = _READABLE_Nbsp_RE.sub(" ", cleaned)
    return _READABLE_WHITESPACE_RE.sub(" ", cleaned).strip()


@functools.lru_cache(maxsize=256)
def count_readable_words(text: str) -> int:
    """统计**可读字数**（R1 权威口径，移植自易标 ``countReadableWords``）。

    口径：**每个 CJK 字符记 1 字** + **每个西文词/数字串记 1 字**。

    剔除项（易标报告 3 节逐条对应）：围栏代码块、Markdown 图片、URL
    （http/file/yibiao-asset 协议）、HTML 注释与标签、表格分隔行、标题与
    列表标记、行内标记符。链接只保留锚文字，URL 部分不计。

    与 :func:`count_words` 的差异（重要）：

    - 本函数会先做剥离再计数，图表围栏里的 Mermaid 源码、表格数据行、
      图片语法都不计入；``count_words`` 不剥离，会把它们一起算进去。
    - CJK 区间为 ``\\u3400-\\u9fff``（多覆盖扩展 A 区）；
    - 西文 token 识别 ``a.b`` / ``x-y`` / ``v1.2.3`` 这类连接形态为 1 个词。

    性能：与 :func:`count_words` 相同策略——预编译正则 + LRU 缓存（256），
    剥离后的短文本重复计数几乎零成本。
    """
    if not text:
        return 0
    cleaned = strip_readables_markup(text)
    if not cleaned:
        return 0
    cjk_count = len(_READABLE_CJK_RE.findall(cleaned))
    western = _READABLE_WESTERN_TOKEN_RE.findall(_READABLE_CJK_RE.sub(" ", cleaned))
    return cjk_count + len(western)


def normalize_newlines(text: str) -> str:
    """把 CRLF (\r\n) 与 CR (\r) 统一归一为 LF (\n)。

    HARDEN 2026-10-12：AI 网关、Windows 环境下的 DB 历史正文、上传的 .md 文档
    均可能带 \r\n。content_cleaning 中所有围栏识别与 parse_content 都基于
    `text.split("\n")` 与 `[ \t]*$` 行尾锚点，`\r` 不在 `[ \t]` 类中会导致：
      ① 图表围栏识别失败，整段图表"人间蒸发"；
      ② 未闭合围栏坏块剔除边界失效；
      ③ 标题 / 列表 / 编号前缀正则锚点被挡住。
    `clean_generated_content` 已在 AI 生成 → 清洗链路上做归一，但以下旁路仍会
    把 CRLF 带回内存 / DB / 前端：
      - 压缩链路：condense_overlong_content 拿到 AI 返回直接落库，未过清洗；
      - DB 读出：历史行 / 导入路径遗留的 \r\n 会回灌到前端编辑器。
    本函数是「双保险」最上游入口。使用 `if "\r" not in text` 短路，正常 LF
    输入零成本；仅在实际含 CR 时才做 replace。

    Args:
        text: 原始文本（None / 空串均返回 ""，容错调用方直接传 row["content"]）

    Returns:
        str: 归一后的 LF 文本
    """
    if not text:
        return ""
    if "\r" not in text:
        return text
    return text.replace("\r\n", "\n").replace("\r", "\n")


__all__ = [
    "extract_json_from_text",
    "safe_json_parse",
    "count_words",
    "count_readable_words",
    "strip_readables_markup",
    "normalize_newlines",
]
