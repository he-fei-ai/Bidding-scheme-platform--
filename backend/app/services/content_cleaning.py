from dataclasses import dataclass
import html
import json
import logging
import re
import threading
from typing import Literal

from .latex_text import convert_latex_in_text

logger = logging.getLogger(__name__)


BlockType = Literal[
    "heading1",
    "heading2",
    "heading3",
    "heading4",
    "heading5",
    "heading6",
    "heading7",
    "heading8",
    "paragraph",
    "list_item",
    "table",
    "chart_marker",
    "image",
]


# 具体图表类型（不含通配 `chart`），顺序与 prompts/charts.py::CHART_REGISTRY 一致
# - flowchart: 流程图（Mermaid 或 nodes 数组）
# - gantt: 甘特图（tasks + duration）
# - architecture: 架构图（递归树 ArchNode）
# - labor: 劳动力图（phases + categories + data）
# - comparison: 对比/占比图（headers + rows 或 pie 数据）
# - layout: 空间布局图（zones + connections）
# - timeline: 时间轴/里程碑图（milestones + date axis）
CHART_TYPES: tuple[str, ...] = (
    "flowchart",
    "gantt",
    "architecture",
    "labor",
    "comparison",
    "layout",
    "timeline",
)

# 支持的图表类型（具体类型 + 通配 `chart`：自动从内部 JSON 的 type 字段识别）
SUPPORTED_CHART_TYPES = set(CHART_TYPES) | {"chart"}


@dataclass
class DocumentBlock:
    type: BlockType
    text: str
    level: int = 0
    relative_level: int = 0
    # BUG-FIX 2026-10-04（BUG-E）：标题行的原始整行文本（含 L5-L7 编号前缀）。
    # parse 层会剥离标题编号（text 仅存正文），而导出端受控层级设计（2026-09-04）
    # 要求 L5~L7「保留 AI 原文格式不重新编号」——旧实现拿到的 text 已无编号，
    # 导出文档中 1）/a、等 L5+ 编号全部丢失（列表/小标题样式被拍平成普通标题正文）。
    # 默认空串保持向后兼容，所有既有构造点无需改动；导出端仅在 L5+ 优先用本字段。
    raw_line: str = ""


# BUG-FIX 2026-10-08（P0 正文数字被吞）：L3/L4 的「数字 + 空格 + 中文」分支是为
# 兼容 AI 偶发输出的「1 施工准备」（无顿号/句点）而加的，但它同时命中普通正文里的
# 数量/金额短语：「3 天内完成移交」「5 万元投资额」「10 平方米场地」「1.8 米净高满足要求」。
# _match_heading 只取 m.group(lastindex)=group(2) 作为标题文本 → **数字被丢弃**，
# 导出端再补一个编号，于是投标文档里出现「1、元投资额」这类金额被改写的内容（P0 数据损坏）。
#
# 修复：空格分支加「单位/量词」负向前瞻 —— 空格后紧跟量词时判定为正文数量短语而非标题。
# 正向形态（"1 施工准备"）与顿号/句点分支完全不受影响（2026-10-06 的 BUG-2 修复不回退）。
_UNIT_LEAD_CHARS = (
    "元万亿千元百米公分厘毫吨千克公斤克"
    "天日周月年小时分秒毫秒"
    "米立方平立方平方米立方米"
    "个处项组次件根块张本台套样批只"
    "层栋座棵株间道步轮期"
    "百千万亿兆京垓"
    "度温度测量份比率"
    "%％℃°"
)

HEADING_PATTERNS = [
    # 七级标题标准（与 heading_standard.HEADING_STYLE_CONFIG 对齐）
    # 注意：具体模式优先于通用模式，避免误匹配
    # 五级: 1.1.1 → L5（3层小数点）
    (re.compile(r"^(\d{1,4}(?:\.\d+){2,})\s*[、．.]?\s*(.+)$"), 5),
    # 四级: 1.1 → L4（2层小数点）
    # 空格分支同样加量词负向前瞻（见 _UNIT_LEAD_CHARS 说明）
    (re.compile(r"^(\d{1,4}\.\d+)(?!\.\d)(?:\s*[、．.]\s*|\s+(?![" + _UNIT_LEAD_CHARS + r"])(?=[一-鿿]))(.+)$"), 4),
    # 三级: 阿拉伯数字（1、2、） → L3（纯数字，无小数点）
    # BUG-FIX 2026-10-06（P1）：原模式 `[、．.]` 为**必需**分隔符，AI 偶尔输出的
    # "1 施工准备"（数字+空格、无顿号/句点）不匹配 → 整行被拍平为普通段落，
    # 标题层级/编号样式在导出文档中全部丢失。
    # 修复：分隔符改为「顿号/句点」或「空格+中文标题」二选一；空格分支用
    # lookahead 锚定中文字符开头，排除 "1 000 元"（空格后是数字）这类正文误匹配。
    # 空格分支同样加量词负向前瞻（见 _UNIT_LEAD_CHARS 说明）
    (re.compile(r"^(\d{1,4})(?!\.\d)(?:\s*[、．.]\s*|\s+(?![" + _UNIT_LEAD_CHARS + r"])(?=[一-鿿]))(.+)$"), 3),
    # 二级: 中文数字（一、二、） → L2
    (re.compile(r"^([一二三四五六七八九十百千]+)[、．.]\s*(.+)$"), 2),
    # 一级: 第X节/第X章 → L1
    (re.compile(r"^第[一二三四五六七八九十百千万0-9]+[章节]\s*[、．.．]?\s*(.+)$"), 1),
    # 六级: 1） → L6（数字+右括号）
    # NOTE 2026-10-04（审计定性，非 BUG）：本模式与 LIST_PATTERN 同形 ^(\d+)[)）]，
    # parse_content 中标题判定优先 → “1）xxx”恒解析为 L6 小标题而非列表项。
    # 这是七级编号标准的既定取舍（SHARED_HEADING_RULES：L6 格式即“1）”），
    # 导出端 L5+ 保留原文 + 悬挂缩进渲染，列表视觉不丢失（style 不被拍平）；
    # 真 bullet 列表（-/•/· 等）由 LIST_BULLET_PATTERN 独立覆盖。
    # 若未来需区分“真列表”，应在 parse_content 引入上下文启发式（如连续同格式
    # 行+短文本+句末标点），而非改动本模式——改模式会同时破坏七级编号导出。
    (re.compile(r"^(\d+)\s*[)）]\s*[、．.]?\s*(.+)$"), 6),
    # 七级: a、 → L7（小写字母+顿号）
    # P2 修复：分隔符仅保留中文顿号/全角句点，去掉 ASCII "."——
    # 否则英文缩写 "e.g. xxx"/"i.e. xxx" 被误解析为 L7 标题（"e" + 正文 "g. xxx"）
    (re.compile(r"^([a-z])\s*[、．]\s*(.+)$"), 7),
]

LIST_PATTERN = re.compile(r"^(\d+)\s*[)）]\s*(.+)$")
# FIX 2026-10-10（《文档质量分析报告》格式问题 12·修复 10）：
# 列表符号混用：「(1）外墙铲除与基层处理。」「（2）外墙抹灰修复。」这类
# 括号编号行既不命中 LIST_PATTERN（数字前有括号），也不命中标题模式，
# 以前整行连同符号拍平为普通段落，导出后列表符号体系混乱。
#
# BUG-P2a FIX 2026-10-11：原正则只匹配 1-2 位数字（\d{1,2}），
# 导致 (100) 外墙... 这类 3 位+列表项被拍平为普通段落。
# 改为任意位数 \d+ 但加负向前瞻排除年份/编号正文：
# \d{4} 后跟「年/月/日/号/版」等正文关键词 → 年份，不匹配列表
LIST_PAREN_PATTERN = re.compile(
    r"^[（(]\s*(\d{1,3})\s*[)）]\s*(.+)$"
)
# 年份负向前瞻：(2026)年... (2026-08)版 这类正文开头不命中列表模式
_LIST_PAREN_YEAR_RE = re.compile(r"^[（(]\s*\d{4}\s*[)）]\s*(?:年|月|日|号|版|年度|版次|年版)", re.MULTILINE)
LIST_BULLET_PATTERN = re.compile(r"^[-•·▪■●○]\s*(.+)$")
TABLE_PATTERN = re.compile(r"^<<表格[:：]")
MD_TABLE_PATTERN = re.compile(r"^\|.+\|.+$")
# CHART-MARKER-UNIFY 2026-09-16：
# - 描述部分放开为 `(.*?)`（允许 `<<GANTT:>>` / `<<CHART:>>` 这类「类型提示标记」）
# - 加 re.DOTALL：AI 偶尔把标记内的 JSON 折行输出，单行匹配会漏（漏掉后标记原文会被
#   当作普通段落写进正文/导出文档，形成标书中的乱码占位符）
# - 加 re.IGNORECASE：兼容小写 `<<gantt:...>>`
CHART_MARKER_PATTERN = re.compile(
    r"^(\[\[|<<)\s*(CHART|FLOWCHART|GANTT|ARCHITECTURE|LABOR|COMPARISON|LAYOUT|TIMELINE)\s*:\s*(.*?)\s*(\]\]|>>)$",
    re.DOTALL | re.IGNORECASE,
)

# 图表标记「起始」形状（未要求闭合）：用于多行标记预聚合
CHART_MARKER_OPEN_PATTERN = re.compile(
    r"^(?:\[\[|<<)\s*(?:CHART|FLOWCHART|GANTT|ARCHITECTURE|LABOR|COMPARISON|LAYOUT|TIMELINE)\s*:",
    re.IGNORECASE,
)

# =====================================================================
# ENHANCE 2026-10-04（图表模块 D1/D2）：Markdown 围栏代码块图表
# 正文 AI 直接在最优位置输出 ```mermaid / ```chart-json 代码块，
# 导出时按需渲染（Mermaid 语法走渲染引擎，chart-json 为结构化数据载荷）。
# 与 <<CHART:...>> 标记体系并存（向后兼容），解析后统一产出 chart_marker 块。
# =====================================================================
# BUG-FIX 2026-10-08（P0 围栏变体泄漏）：原模式只认 ```mermaid / ```chart-json 两种形态，
# AI 实际还会输出 ~~~mermaid（波浪线围栏）、```chart_json / ```chartjson（下划线/无连字符）、
# ```mermaid {json}（带属性串）。这些形态原先走「普通围栏」路径 → 闭合时被剥掉标记行、
# **把 mermaid 源码 / JSON 载荷整段写进投标正文**（导出文档里出现 graph TD / {"type":...）。
# 现统一按 `(?:`{3,}|~{3,})` + `chart[-_]?json` 收敛，并保留可选属性串。
_FENCE_MARK = r"(?:`{3,}|~{3,})"
_FENCE_ATTR = r"(?:\{[^}]*\})?"
CHART_FENCE_OPEN_PATTERN = re.compile(
    r"^" + _FENCE_MARK + r"[ \t]*(mermaid|chart[-_]?json)[ \t]*" + _FENCE_ATTR + r"[ \t]*$",
    re.IGNORECASE,
)
# 闭合行 / 任意围栏标记行（供状态机判定闭合性与坏块右边界）
#
# BUG-FIX 2026-10-09（P1·带 info-string 的闭合行被判未闭合 → 整块图表静默丢失）：
# AI 复现率很高的一种写法是把闭合行也写上语言标记，例如
#     ```mermaid
#     graph TD ...
#     ```mermaid        ← 闭合行带 info-string
# 旧 `FENCE_CLOSE_LINE_PATTERN`（`^```[ \t]*$`）不认它，而 `FENCE_ANY_LINE_PATTERN`
# 会命中 → 状态机走「遇到另一个围栏起始行 = 原块未闭合」分支，**整块被当作坏块删除**
# （`_protect_chart_fences` 只打一条 info 日志），图表在导出文档里消失且用户无感。
# 现允许闭合行带可选 info-string（语言字眼或属性串），与围栏开启行的口径对齐；
# 开启/扫描/状态机三处共用 `_FENCE_CLOSE_TAIL`，避免口径再次分叉。
_FENCE_CLOSE_TAIL = rf"[ \t]*[A-Za-z0-9_\-+.#]*[ \t]*{_FENCE_ATTR}[ \t]*$"
FENCE_CLOSE_LINE_PATTERN = re.compile(rf"^{_FENCE_MARK}{_FENCE_CLOSE_TAIL}")
FENCE_ANY_LINE_PATTERN = re.compile(rf"^{_FENCE_MARK}")

# 围栏行的完整形态：标记 + 可选 info-string（语言标记）+ 可选属性串。
# info-string 用 `.*?` 宽松捕获（AI 偶尔写中文/多词标记），比较时取首 token。
_FENCE_INFO_RE = re.compile(
    rf"^{_FENCE_MARK}[ \t]*(.*?)[ \t]*(?:\{{[^}}]*\}})?[ \t]*$"
)
# 图表围栏语言族（mermaid / chart-json）：闭合行语言一致性校验的判定范围
_CHART_FENCE_LANG_RE = re.compile(r"(?:mermaid|chart[-_]?json)$", re.IGNORECASE)

CHART_FENCE_SCAN_PATTERN = re.compile(
    r"^" + _FENCE_MARK + r"[ \t]*(mermaid|chart[-_]?json)[ \t]*" + _FENCE_ATTR + r"[ \t]*\n"
    r"(.*?)^" + _FENCE_MARK + r"[ \t]*(?:\1)?[ \t]*" + _FENCE_ATTR + r"[ \t]*$",
    re.DOTALL | re.IGNORECASE | re.MULTILINE,
)

#: 图表围栏载荷行数硬上限（防御）。**只用于判定「 pathological 坏块整块删除」**，
#: 不再像旧实现那样在收集到 200 行时「半途停止收集并把剩余代码当正文输出」。
_HARD_CHART_FENCE_LINE_LIMIT = 5000


def _normalize_fence_lang(lang: str | None) -> str:
    """把围栏语言标记归一为内部口径（'mermaid' / 'chart-json'）。

    BUG-FIX 2026-10-08：`````chart_json`` / `````chartjson`` 会被 ``chart[-_]?json`` 捕获，
    但下游 ``_fence_payload_to_marker_text`` 按 ``lang == "chart-json"`` 判定载荷格式，
    未归一时 chart_json 载荷会走 mermaid 分支被当源码处理。
    """
    s = (lang or "").strip().lower()
    if re.fullmatch(r"chart[-_]?json", s):
        return "chart-json"
    return "mermaid"

def fence_info_string(line: str) -> str | None:
    """取围栏行的 info-string（语言标记，如 `` ```python `` → ``python``）。

    非围栏行返回 ``None``；纯围栏标记行（`` ``` ``）返回空串 ``""``。
    """
    m = _FENCE_INFO_RE.match(str(line or "").strip())
    if not m:
        return None
    token = (m.group(1) or "").strip()
    # 只取首 token：「json {id: x}」或「python 代码」这类写法统一按语言标记处理
    return token.split()[0] if token else ""


def is_fence_close_line(line: str, open_info: str | None = None) -> bool:
    """判定某行是否是**当前块的合法闭合围栏行**（语言一致性校验版）。

    BUG-FIX 2026-10-09（P2·异语言围栏行被误判为闭合行 → 代码泄漏进正文）：
    早前为兼容 AI 把闭合行也写上语言标记（`` ```mermaid ``）的写法，
    FENCE_CLOSE_LINE_PATTERN 把闭合行的 info-string **完全放开**。副作用是——
    AI 漏写 mermaid 闭合标记后紧跟一个 `` ```python `` 起始行时，该行会被当成
    mermaid 的闭合行：坏块不再被删除，且 `` ```python `` 被整体消费，其后
    python 代码的载荷行（import/def/return…）在 parse_content 里落成**普通
    正文段落**写进投标文档（实测：导出正文出现孤立代码行乱码）。

    现按 CommonMark 语义收敛：闭合行的 info-string 只允许**为空**，或与开启行
    的语言标记一致（`` ```chart_json `` 与 `` ```chart-json `` 视为同一语言）。
    异语言的**图表**围栏行一律退回「另一个围栏起始行」分支 → 原块按未闭合坏块删除。
    注意不能直接用 _normalize_fence_lang 比较：它对非图表语言（python 等）一律
    回退返回 "mermaid"，会让 `` ```python `` 与 mermaid 判为「同语言」而误当闭合行。

    普通代码围栏（python/json/text 等）保持**历史宽松口径**：闭合行 info-string
    不一致时仍判闭合。原因有二：① 普通围栏按 CommonMark 本就不要求闭合行
    info-string 一致；② 既有测试 test_clean_code_fence_json 依赖该口径
    （'```json\n{"a": 1}\n```正文内容' 需保留代码体）；若收紧普通围栏会把
    后续正文一并删掉，属另一议题，不在本次修复范围内。

    Args:
        line: 待判定的行（可含首尾空白）。
        open_info: 开启行的语言标记；``None``/空串表示无法获知（如普通代码块
            的通用判定），此时按「任意围栏行都可闭合」的宽松口径接受。

    Returns:
        True 表示可作闭合行。
    """
    info = fence_info_string(line)
    if info is None:
        return False
    if not info or not open_info:
        return True
    a, b = info.strip().lower(), open_info.strip().lower()
    if a == b:
        return True
    a_chart = bool(_CHART_FENCE_LANG_RE.fullmatch(a))
    b_chart = bool(_CHART_FENCE_LANG_RE.fullmatch(b))
    if a_chart or b_chart:
        # 图表围栏：闭合行语言必须落在**同一图表语言族**内
        # （_normalize_fence_lang 对图表语言返回正确语言族名，mermaid 与
        #  chart-json 分属两族；非图表语言一律不在本分支）
        return a_chart and b_chart and _normalize_fence_lang(a) == _normalize_fence_lang(b)
    # 普通代码围栏：保持历史宽松口径（见 docstring）
    return True


# Mermaid 内部 title 行（图题提取优先级第二级：载荷 title > Mermaid title > 引导语 > 类型通用名）
_MERMAID_TITLE_PATTERN = re.compile(r"^[ \t]*title[ \t]+(.+?)[ \t]*$", re.MULTILINE | re.IGNORECASE)


def extract_mermaid_title(code: str) -> str:
    """从 Mermaid 代码中提取 title 行（无 title 返回空串）。"""
    if not code:
        return ""
    m = _MERMAID_TITLE_PATTERN.search(code)
    return m.group(1).strip() if m else ""


def iter_chart_fences(content: str):
    """遍历内容中所有**已闭合**的图表围栏代码块，产出 (lang, body, marker_text)。

    marker_text 为 None 表示内容非法（坏块）：检测/提取侧应忽略，
    解析侧（parse_content）会将其从正文删除。
    与 iter_chart_markers 口径一致：凡本函数识别出的类型，parse_content
    必产出同类型 chart_marker 块，避免「内联/章末两边都不出图」。
    """
    if not content:
        return
    for m in CHART_FENCE_SCAN_PATTERN.finditer(content):
        lang = _normalize_fence_lang(m.group(1))
        body = m.group(2).strip()
        yield lang, body, _fence_payload_to_marker_text(lang, body)


def _fence_payload_to_marker_text(lang: str, body: str) -> str | None:
    """把围栏代码块内容转换为 chart_marker 块的 JSON 文本。

    - chart-json: 载荷为结构化 JSON（含 type/title/数据），解析失败返回 None（坏块删除）
    - mermaid: 语法代码原样作为数据载荷（type 从代码首行关键字推断），空代码返回 None
    返回 None 表示该块无法识别 → 调用方删除坏块（绝不把代码原文写进正文）。
    """
    body = (body or "").strip()
    if not body:
        return None
    lang = (lang or "").lower()
    if lang == "chart-json":
        obj = _try_repair_json(body)
        if not isinstance(obj, dict):
            return None
        inner_type = str(obj.get("type") or obj.get("chart_type") or "").strip().lower()
        if inner_type not in SUPPORTED_CHART_TYPES:
            # 类型缺失/非法：按标题关键词推断，推不出则坏块删除（不猜测渲染）
            title_hint = str(obj.get("title") or "")
            if not title_hint:
                return None
            inner_type = infer_chart_type_from_text(title_hint, default="")
            if not inner_type:
                return None
            obj["type"] = inner_type
        else:
            obj["type"] = inner_type
        # 注意：不在这里调用 validate_chart_payload —— 围栏解析阶段只检查
        # JSON 可解析 + 类型可推断，数据完整性校验留到 export.py 渲染前
        # 的 _render_commercial 入口做（那里有完整的降级策略和日志链路）。
        return json.dumps(
            {"chart_type": inner_type, "description": json.dumps(obj, ensure_ascii=False)},
            ensure_ascii=False,
        )
    # mermaid：首行关键字映射类型；flowchart/graph 细分甘特以外统一 flowchart
    first = body.split("\n", 1)[0].strip().lower()
    if first.startswith("gantt"):
        ctype = "gantt"
    elif first.startswith("flowchart") or first.startswith("graph"):
        ctype = "flowchart"
    elif first.startswith("pie") or first.startswith("xychart"):
        ctype = "comparison"
    elif first.startswith("timeline"):
        ctype = "timeline"
    else:
        # sequenceDiagram/classDiagram/stateDiagram/erDiagram 等：渲染引擎支持 Mermaid 原样，
        # 归入 flowchart 通道（渲染层按代码内容自适应；HTTP Service 可直接渲染）
        ctype = "flowchart"
    # 集成校验与修复（需求 6）：先 repair_mermaid 机械化修复，再 validate_mermaid，
    # 修复后仍非法 → 删除坏块（返回 None，不把代码原文写进正文）。
    # 惰性导入 image_engine（避免循环依赖）；导入失败时降级为不校验（向后兼容）。
    try:
        from .ai.image_engine import repair_mermaid as _repair, validate_mermaid as _validate

        _repaired = _repair(body)
        _ok, _msg = _validate(_repaired)
        if not _ok:
            logger.info("[图表跳过] Mermaid 围栏块语法非法且修复后仍不通过，删除坏块: %s", _msg)
            return None
        body = _repaired
    except Exception as _e:  # 校验器不可用（循环导入等）：保留原块，交由渲染层兵底
        logger.debug("Mermaid 围栏预校验不可用（跳过预校验）: %s", _e)
    desc = json.dumps(
        {"type": ctype, "code": body, "title": extract_mermaid_title(body)},
        ensure_ascii=False,
    )
    return json.dumps({"chart_type": ctype, "description": desc}, ensure_ascii=False)

# 全量图表标记扫描（用于「内容中出现了哪些图表类型」的一致判定）
# 与 CHART_MARKER_PATTERN 使用同一套前缀与包裹符，确保「解析」与「检测」口径完全一致。
CHART_MARKER_SCAN_PATTERN = re.compile(
    r"(?:\[\[|<<)\s*(CHART|FLOWCHART|GANTT|ARCHITECTURE|LABOR|COMPARISON|LAYOUT|TIMELINE)\s*:\s*(.*?)\s*(?:\]\]|>>)",
    re.DOTALL | re.IGNORECASE,
)

# FIX 2026-10-08（遗留4）：「终止符在行尾」形态——优先用它，再回落到上面的宽松形态。
# 原扫描正则的终止符是**非贪婪且不锚定行尾**的 `(.*?)\s*(?:\]\]|>>)`：
# 描述串里只要出现 `>>` 就会被判为终止（mermaid sequenceDiagram 的 `->>`、
# JSON 里的 `}}>>`），导致载荷被截断或整条标记解析失败 → 静默丢图。
# 加上行尾锚定后，非贪婪会一直扩展到**本行最后一个**终止符，
# 中间的 `->>` 不再被误认。多行/非行尾形态仍由上方的宽松正则兜底。
CHART_MARKER_SCAN_PATTERN_LINE_TERMINAL = re.compile(
    r"(?:\[\[|<<)\s*(CHART|FLOWCHART|GANTT|ARCHITECTURE|LABOR|COMPARISON|LAYOUT|TIMELINE)\s*:\s*(.*?)\s*(?:\]\]|>>)[ \t]*$",
    re.DOTALL | re.IGNORECASE | re.MULTILINE,
)

# 图表类型关键词表（唯一权威来源）
# 历史上 export._CHART_KEYWORDS_FOR_DETECT 与 workflows_sections._CHART_TYPE_KEYWORDS
# 各维护一份且互有差异，导致「正文里写的图」与「导出的图」判定不一致。
# 统一收敛到此处，供 content_cleaning / export / workflows_sections 共用。
CHART_TYPE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "gantt": (
        "甘特", "进度", "工期", "计划", "阶段", "时点", "时序", "节奏",
        "劳动力投入", "穿插", "流水节拍", "里程碑",
    ),
    "flowchart": (
        "流程", "工艺", "步骤", "操作", "验收", "检验", "绘制", "交付",
        "防治", "降水", "土方开挖", "混凝土", "施工流程", "施工",
    ),
    "architecture": ("架构", "组织", "体系", "职责", "矩阵", "角色", "岗位", "WBS"),
    "labor": ("劳动力", "人员", "工种", "班组", "人数", "培训", "考核"),
    "comparison": ("对比", "比选", "方案", "权重", "优缺点", "利弊", "占比"),
    "layout": ("布置", "平面", "规划", "场地", "塔吊", "降水井", "总平面", "分区"),
    "timeline": ("里程碑", "时间线", "关键节点", "事件序列"),
}


def infer_chart_type_from_text(text: str, default: str = "flowchart") -> str:
    """按关键词从中文描述/标题推断图表类型（无法识别时返回 default）"""
    if not text:
        return default
    lowered = str(text).lower()
    scores = {
        ct: sum(1 for kw in kws if kw.lower() in lowered)
        for ct, kws in CHART_TYPE_KEYWORDS.items()
    }
    best = max(scores, key=scores.get) if scores else default
    return best if scores.get(best, 0) > 0 else default


def iter_chart_markers(content: str):
    """遍历内容中所有图表标记，产出 (完整标记文本, 前缀, 描述)。

    「解析」与「检测」共用本函数，保证：
      - parse_content 产出的 chart_marker 块
      - export._detect_inline_chart_types 判定出的「已内联类型」
    永远一致（否则会出现「标记被跳过、章节末尾又不渲染」的静默丢图）。
    """
    if not content:
        return
    # FIX 2026-10-08（遗留4）：优先用行尾锚定形态（抗 `->>` 截断），
    # 仅在它能匹配到的条目数**少于**宽松形态时才整体回退（说明存在多行/非行尾
    # 标记，宽松形态才能覆盖）——保证不比修复前更少。
    _terminal = list(CHART_MARKER_SCAN_PATTERN_LINE_TERMINAL.finditer(content))
    _loose = list(CHART_MARKER_SCAN_PATTERN.finditer(content))
    for m in (_terminal if len(_terminal) >= len(_loose) else _loose):
        yield m.group(0), m.group(1).lower(), m.group(2).strip()


def resolve_chart_marker(prefix: str, descriptor: str) -> tuple[str, str] | None:
    """把图表标记（前缀 + 描述）解析为 (chart_type, title)。

    仅解析「类型 / 标题」，**绝不构造任何图表数据**（无降级原则：宁缺毋假）。
    真实图表数据一律来自后端配图编排管线写入的 `sections.*_json` 字段。

    支持形态：
      1. `<<GANTT:>>` / `<<GANTT:施工进度计划>>`        —— 显式类型前缀（推荐）
      2. `<<CHART:GANTT>>` / `<<CHART:{gantt}>>`        —— 通配前缀 + 类型令牌
      3. `<<CHART:{施工进度甘特图}>>`                    —— 通配前缀 + 纯文本标题（关键词推断）
      4. `<<CHART:{"type":"gantt",...}>>`               —— 通配前缀 + 真实 JSON 数据
    返回 None 表示该标记无法识别（调用方应跳过，不做任何猜测性渲染）。
    """
    prefix = (prefix or "").strip().lower()
    desc = (descriptor or "").strip()

    if prefix in CHART_TYPES:
        explicit: str | None = prefix
    elif prefix == "chart":
        explicit = None
    else:
        return None

    # 形态 4：描述是带真实数据的 JSON —— 以内部 type 为准（缺失时回退前缀）
    if desc.startswith("{"):
        obj = _try_repair_json(desc)
        if isinstance(obj, dict):
            inner_type = str(obj.get("type") or obj.get("chart_type") or "").strip().lower()
            if inner_type not in CHART_TYPES:
                inner_type = explicit or ""
            if inner_type:
                title = str(obj.get("title") or obj.get("chart_title") or "").strip()
                return inner_type, title
            return None

    # 去掉可选的大括号包裹：<<CHART:{gantt}>> / <<GANTT:{施工进度计划}>>
    inner = desc
    if inner.startswith("{") and inner.endswith("}"):
        inner = inner[1:-1].strip()

    token = inner.strip().lower()

    # 形态 2：类型令牌
    if token in CHART_TYPES:
        return token, ""

    # 形态 2b：`type|标题` / `type:标题` / `type,标题`
    m = re.match(r"^([a-z_]+)\s*[|｜:：,，]\s*(.+)$", inner, re.IGNORECASE)
    if m and m.group(1).strip().lower() in CHART_TYPES:
        return m.group(1).strip().lower(), m.group(2).strip()[:80]

    # 形态 1：显式类型前缀 + 标题
    if explicit:
        return explicit, inner[:80]

    # 形态 3：通配前缀 + 纯文本标题 → 关键词推断类型
    if inner and len(inner) <= 60:
        return infer_chart_type_from_text(inner), inner

    return None

# [AI JSON 截断修复] AI 生成图表 JSON 时偶尔出现字符串缺闭合引号、括号不平衡等问题。
# 94.7% 直接有效, 4.5% 可通过补引号修复, 仅 0.8% 不可修复。
class _RepairBudgetExceeded(Exception):
    """JSON 修复总尝试预算耗尽的内部信号（不被各策略循环的 except 吞掉）"""


def _normalize_single_quoted(raw: str) -> str | None:
    """把 AI 偶发的 ASCII 单引号 JSON 归一为双引号 JSON。

    FIX 2026-10-08（遗留4）：`<<CHART:{'type':'gantt','title':'进度'}>>` 这类输出在
    三条解析管线上全部解析失败（既不是合法 JSON，补引号/补括号策略也救不回来），
    最终整条标记被跳过 → 静默丢图。

    实现要点：逐字符状态机——双引号段整体原样搬运（其内部的撇号不受影响），
    只对**结构性**单引号改写；遇到未闭合的单引号返回 None（不是可归一的形态）。
    """
    s = (raw or "").strip()
    if not s.startswith("{") or "'" not in s:
        return None
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == '"':
            j = i + 1
            while j < n:
                if s[j] == '"' and s[j - 1] != "\\":
                    break
                j += 1
            if j >= n:
                return None  # 双引号未闭合：交给既有策略处理
            out.append(s[i:j + 1])
            i = j + 1
            continue
        if c == "'":
            j = i + 1
            while j < n and s[j] != "'":
                if s[j] == "\\":
                    j += 1
                j += 1
            if j >= n:
                return None  # 单引号未闭合
            inner = s[i + 1:j]
            out.append('"' + inner.replace('"', '\\"') + '"')
            i = j + 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _try_repair_json(raw: str) -> dict | None:
    """尝试修复 AI 截断的 JSON，返回解析后的 dict 或 None"""
    import json as _json

    try:
        obj = _json.loads(raw)
        if isinstance(obj, dict):
            return obj
    except (_json.JSONDecodeError, TypeError):
        pass

    # FIX 2026-10-08（遗留4）：单引号归一——仅在直接解析失败后触发，
    # 一次尝试、失败即继续走既有修复策略，不影响原有预算与成功率。
    _sq = _normalize_single_quoted(raw)
    if _sq is not None:
        try:
            obj = _json.loads(_sq)
            if isinstance(obj, dict):
                return obj
        except (_json.JSONDecodeError, TypeError):
            pass

    # BUG-FIX 2026-09-18（P1）：修复总尝试硬预算。策略 1/3 的 120 次裁剪 × 4 后缀
    # 叠加括号枚举，单个严重畸变标记最坏可触发约十万次 json.loads，
    # 同步阻塞调用线程数十秒。超过预算立即放弃。
    _attempts = 0
    _MAX_ATTEMPTS = 3000

    def _loads(s: str):
        nonlocal _attempts
        _attempts += 1
        if _attempts > _MAX_ATTEMPTS:
            raise _RepairBudgetExceeded()
        return _json.loads(s)

    try:
        # 策略 0: 削尾部多余闭合符 — AI 偶尔多写 } 或 ]，导致 json.loads 报 Extra data
        # 例: {"type":"...","nodes":["a","b"]}}}} → {"type":"...","nodes":["a","b"]}
        stripped = raw.rstrip()
        for end in range(len(stripped), max(len(stripped) - 10, 1), -1):
            tail = stripped[end - 1]
            if tail not in "}]":
                break
            candidate = stripped[:end]
            # 只削尾部连续多余的 } 或 ]，前面的内容应该能独立解析
            # 避免误删合法 JSON 内部的有效字符
            last_valid = None
            for extra_end in range(len(candidate), max(len(candidate) - 80, 1), -1):
                c = candidate[:extra_end]
                try:
                    obj = _loads(c)
                    if isinstance(obj, dict):
                        last_valid = obj
                        break
                except (_json.JSONDecodeError, TypeError):
                    continue
            if last_valid is not None:
                return last_valid

        # 策略 1: 补末尾引号 — AI 经常在 remark/title 值内忘记闭合 "
        for cut in range(len(raw), max(len(raw) - 120, 0), -1):
            candidate = raw[:cut]
            for suffix in ('"}', '"}]', '"}]}', '"'):
                try:
                    repaired = candidate + suffix
                    obj = _loads(repaired)
                    if isinstance(obj, dict):
                        return obj
                except (_json.JSONDecodeError, TypeError):
                    continue

        # 策略 2: 补末尾括号 — AI 偶尔多写/漏写 } 或 ]
        # 性能修复：枚举上限 2^6=64 次。旧实现按 2^extra 全量枚举，
        # AI 返回 15+ 个未平衡括号时同步执行数百万次 json.loads，阻塞事件循环
        opens = raw.count("{") - raw.count("}")
        brackets = raw.count("[") - raw.count("]")
        if opens > 0 or brackets > 0:
            max_extra = min(opens + brackets + 3, 6)
            for extra in range(1, max_extra + 1):
                for combo in range(2**extra):
                    suffix = ""
                    rem_o, rem_b = opens, brackets
                    for i in range(extra):
                        if combo & (1 << i):
                            if rem_o > 0:
                                suffix += "}"
                                rem_o -= 1
                            elif rem_b > 0:
                                suffix += "]"
                                rem_b -= 1
                        else:
                            if rem_b > 0:
                                suffix += "]"
                                rem_b -= 1
                            elif rem_o > 0:
                                suffix += "}"
                                rem_o -= 1
                    if suffix:
                        try:
                            obj = _loads(raw + suffix)
                            if isinstance(obj, dict):
                                return obj
                        except (_json.JSONDecodeError, TypeError):
                            continue

        # 策略 3: 组合 — 先补引号再补括号
        for cut in range(len(raw), max(len(raw) - 120, 0), -1):
            candidate = raw[:cut]
            for qsuffix in ('"}', '"}]', '"}]}', '"'):
                try:
                    step1 = _loads(candidate + qsuffix)
                    if isinstance(step1, dict):
                        return step1
                except (_json.JSONDecodeError, TypeError):
                    repaired = candidate + qsuffix
                    opens = repaired.count("{") - repaired.count("}")
                    brackets = repaired.count("[") - repaired.count("]")
                    if opens > 0 or brackets > 0:
                        # 性能修复：同策略 2，枚举上限 2^6=64 次
                        max_extra = min(opens + brackets + 3, 6)
                        for extra in range(1, max_extra + 1):
                            for combo in range(2**extra):
                                bsuffix = ""
                                ro, rb = opens, brackets
                                for i in range(extra):
                                    if combo & (1 << i):
                                        if ro > 0: bsuffix += "}"; ro -= 1
                                        elif rb > 0: bsuffix += "]"; rb -= 1
                                    else:
                                        if rb > 0: bsuffix += "]"; rb -= 1
                                        elif ro > 0: bsuffix += "}"; ro -= 1
                                if bsuffix:
                                    try:
                                        obj = _loads(repaired + bsuffix)
                                        if isinstance(obj, dict):
                                            return obj
                                    except (_json.JSONDecodeError, TypeError):
                                        continue

        return None
    except _RepairBudgetExceeded:
        logger.warning("图表 JSON 修复尝试超出预算（%d 次），放弃修复（输入 %d 字符）", _MAX_ATTEMPTS, len(raw))
        return None

FULLWIDTH_SPACE_PATTERN = re.compile(r"^[\u3000\s]+")

# 富文本编辑器（RichTextEditor）产生的 HTML 标记（BUG C 修复）：
# - HTML_LIKE_PATTERN：识别包含 HTML 标签的行（<p>/<b>/<div>/<img> 等）
#   （不匹配 <<表格 / <<CHART 等占位符标记：其 "<" 后是 "<" 而非字母/斜杠/感叹号）
# - HTML_IMG_PATTERN：提取 <img src="..."> 的图片来源（data URL / http URL / 本地路径）
HTML_LIKE_PATTERN = re.compile(r"<[a-zA-Z/!][^>]*>")
HTML_TAG_PATTERN = re.compile(r"<[^>]+>")
HTML_IMG_PATTERN = re.compile(r"<img[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"']", re.IGNORECASE)


# 闭合图表围栏块保护占位符表：_protect_chart_fences 写入，_restore_chart_fences 消费
# 线程局部存储：clean_generated_content 可能被多线程（asyncio.to_thread）并发调用，
# 模块级共享列表会产生跨线程索引串位竞态（还原错块）。
_FENCE_PLACEHOLDER_PREFIX = "\x00CHARTFENCE"


class _FenceCacheLocal:
    """每线程独立的围栏块缓存（list 属性惰性初始化）。"""

    def __init__(self) -> None:
        self._local = threading.local()

    @property
    def blocks(self) -> list[str]:
        if not hasattr(self._local, "blocks"):
            self._local.blocks = []
        return self._local.blocks


_fence_cache = _FenceCacheLocal()


def _protect_chart_fences(text: str) -> str:
    """行级预处理图表围栏（ENHANCE 2026-10-04 D1）。

    - 闭合的 ```mermaid / ```chart-json 块 → 整块替换为占位符（后续普通围栏
      剥离不会破坏其内容），由 _restore_chart_fences 还原后交给 parse_content。
    - 未闭合（悬挂起始行）：删除起始行及其后直到下一个围栏闭合行/文末的内容
      （坏块删除，不吞正文：下一个普通 ``` 作为右边界；无则删到文末）。

    闭合行判定见 `is_fence_close_line`：info-string 必须为空或与开启行同语言。
    """
    lines = text.split("\n")
    out: list[str] = []
    blocks: list[str] = []
    i = 0
    while i < len(lines):
        m = CHART_FENCE_OPEN_PATTERN.match(lines[i].strip())
        if not m:
            out.append(lines[i])
            i += 1
            continue
        # 收集到闭合行。
        # BUG-FIX 2026-10-08（P1 部分删除泄漏）：旧实现在 `len(body) < 201` 处**停止收集**
        # 并按「未闭合」处理 → 200 行以上的合法 mermaid 块被截断丢弃，而 `i = _j` 让扫描
        # 从围栏内部继续，剩余源码（graph TD / 节点定义）被当作**正常正文**写进投标文档。
        # 现在只在超过 _HARD_CHART_FENCE_LINE_LIMIT（病态输入）时整块判坏，
        # 闭合行判定用 is_fence_close_line（同时支持 ``` 与 ~~~，且校验语言一致性）。
        body: list[str] = [lines[i]]
        _j = i + 1
        closed = False
        # 开启行的语言标记（如 mermaid / chart-json）：用于闭合行一致性校验
        _open_info = m.group(1)
        while _j < len(lines):
            _s = lines[_j].strip()
            # BUG-FIX 2026-10-09（P2）：闭合行必须「空 info-string」或
            # 「与开启行同语言」，避免异语言起始行（```python 等）被当成闭合行。
            if is_fence_close_line(_s, _open_info):
                body.append(lines[_j])
                closed = True
                _j += 1
                break
            # 遇到另一个围栏起始行：视为原块未闭合（AI 漏写闭合标记）
            if FENCE_ANY_LINE_PATTERN.match(_s):
                break
            body.append(lines[_j])
            _j += 1
            if _j - i > _HARD_CHART_FENCE_LINE_LIMIT:
                break  # 病态超长围栏：按坏块删除（区域有界）
        if closed:
            blocks.append("\n".join(body))
            out.append(f"{_FENCE_PLACEHOLDER_PREFIX}{len(blocks) - 1}\x00")
            i = _j
        else:
            # 未闭合：删除坏块（起始行 + 已收集的 body 行），从 _j 继续处理正常正文
            logger.info(
                "[图表跳过] 未闭合的图表围栏代码块（坏块 %d 行已删除）: lang=%s",
                len(body),
                _normalize_fence_lang(m.group(1)),
            )
            i = _j
    _fence_cache.blocks.extend(blocks)
    return "\n".join(out)


def _restore_chart_fences(text: str) -> str:
    """把占位符还原为完整的图表围栏块（与 _protect_chart_fences 配对）。"""
    blocks = _fence_cache.blocks
    for _i, _blk in enumerate(blocks):
        text = text.replace(f"{_FENCE_PLACEHOLDER_PREFIX}{_i}\x00", _blk)
    # BUG-FIX 2026-10-08（P1 哨兵泄漏）：若上游异常把占位符本身改坏（截断/拼接），
    # 未还原的 `\x00CHARTFENCE…` 会作为字面量进入正文与导出文档。这里显式兜底。
    if _FENCE_PLACEHOLDER_PREFIX in text:
        logger.warning(
            "[围栏还原] 仍有未还原占位符，已剔除: %r", text[:80]
        )
        text = re.sub(r"\x00CHARTFENCE[^\x00]*\x00?", "", text)
    return text


def _apply_outside_chart_fences(text: str, fn) -> str:
    """只对「图表围栏占位符以外」的文本片段应用 fn（载荷零污染）。

    BUG-FIX 2026-10-08（P0）：clean_generated_content 里的 Markdown 改写（去粗体、
    剥标题标记、\\n 字面量还原）原先在 _restore_chart_fences 之后执行，会直接作用到
    mermaid / chart-json 的载荷上。本函数把占位符当作分隔符，只改写普通文本片段。

    实现：用 re.split 的**捕获组**切分——捕获组内容（占位符）会保留在结果列表中，
    奇数索引恒为占位符本身、偶数索引恒为普通文本，与占位符数量和位置无关。

    BUG-FIX 2026-10-08b（P1 隔段漏洗）：旧实现给每个占位符**追加**哨兵再按
    `idx % 2 == 1 跳过` 二分——哨兵在占位符之后，split 后奇数片段实为
    「上一占位符之后的普通文本 + 本占位符」。两个图表之间的 `**加粗**`、字面量
    `\\n` 因此整段漏洗（多图章节一半的普通文本未被清洗）。捕获组切分根治。
    """
    if _FENCE_PLACEHOLDER_PREFIX not in text:
        return fn(text)
    _pieces = re.split(
        r"(" + re.escape(_FENCE_PLACEHOLDER_PREFIX) + r"\d+\x00)",
        text,
    )
    return "".join(p if idx % 2 == 1 else fn(p) for idx, p in enumerate(_pieces))


def _strip_markdown_markup(segment: str) -> str:
    """去 Markdown 粗体 + 剥行首标题标记（仅作用于非图表围栏片段）。

    FIX 2026-10-10（《文档质量分析报告》格式问题 4）：原正则要求 `#` 后必须有
    空白（`\\s+`），AI 输出的 `##1.1工程概况`（无空格）会原样进入导出文档。
    放宽为 `\\s*`，`#` 后直接接中文/数字也能剥掉。
    """
    segment = re.sub(r"\*\*(.*?)\*\*", r"\1", segment)
    return re.sub(r"^(#{1,6})[ \t]*", "", segment, flags=re.MULTILINE)


# FIX 2026-10-10（《文档质量分析报告》格式问题 13）：AI 输出的转义字符残留。
# 报告实例：「·普工：8\~10人」—— `\~` 应还原为 `~`，否则数值区间显示异常。
# 只收录语义安全的字符；**不含** `\|`（GFM 表格单元格内 `|` 是列分隔转义，
# 提前还原会把一列拆成两列）与 `\[`/`\]`/`\(`/`\)`（LaTeX 定界符，
# 由 latex_text.convert_latex_in_text 先行处理，这里只处理其漏网残留）。
_MD_ESCAPE_MAP = {
    "\\~": "~",
    "\\*": "*",
    "\\_": "_",
    "\\#": "#",
    "\\{": "{",
    "\\}": "}",
    "\\$": "$",
    "\\!": "!",
    "\\`": "`",
}


def _unescape_markdown_escapes(segment: str) -> str:
    """还原 Markdown 转义字符（仅作用于非图表围栏片段）。"""
    for escaped, unescaped in _MD_ESCAPE_MAP.items():
        if escaped in segment:
            segment = segment.replace(escaped, unescaped)
    return segment


def _strip_literal_newline_escapes(segment: str) -> str:
    """把字面量 `\\n` 还原为真换行（仅作用于非图表围栏片段）。"""
    return segment.replace("\\n", "\n")


def _strip_plain_code_blocks_enabled() -> bool:
    """是否整体剔除闭合普通代码块（遗留项 2026-10-08，config.STRIP_PLAIN_CODE_BLOCKS）。

    惰性读取 config 模块属性：① 避免顶层 import 造成循环依赖；② 每次调用取
    最新值，便于测试 monkeypatch；③ 环境变量改动重启后自然生效。
    """
    try:
        import app.config as _cfg

        return bool(getattr(_cfg, "STRIP_PLAIN_CODE_BLOCKS", False))
    except Exception:
        return False


def _strip_unclosed_plain_fences(text: str) -> str:
    """清理未闭合的**普通**代码围栏坏块（BUG-FIX 2026-10-06）。

    _protect_chart_fences 已处理图表围栏（mermaid/chart-json）：闭合块保护为
    占位符、未闭合块删除坏行。此后文本中残留的 ``` 行均为普通代码块
    （python/json/text/plain 等）。

    原缺陷：后续 L589/L590 的全局正则只删围栏标记行，未闭合块的**内容行**
    全部残留为正文段落——AI 输出的 ```python 代码（import/def/return 等）
    会一字不差地出现在标书正文里，形成乱码段落。

    修复：行级状态机逐块判定闭合性——
      - 闭合块：保留全部行（后续正则剥围栏行、保留内容，既有行为向后兼容）；
        ENHANCE 2026-10-08（遗留项1）：config.STRIP_PLAIN_CODE_BLOCKS=true 时
        改为整体剔除（标记行+内容），供「标书正文不含代码」的业务口径选用；
      - 未闭合坏块：删除从起始行到文末的所有坏行，绝不把代码原文写进正文。

    BUG-FIX 2026-10-08（P1 图表连带丢失）：坏块删除的右边界增加「图表占位符」。
    _protect_chart_fences 先行执行，其后的闭合图表块已变成 \x00CHARTFENCE{i}\x00
    占位符——占位符行不可能属于普通代码块（它是受保护的完整块），却命中
    「未闭合 → 删到文末」分支被一并删除，连占位符之后的正常正文也全部丢失
    （实测：未闭合 ```python + 其后合法 mermaid 块 → 图表与后续段落全消失）。
    修复：扫描闭合行时遇到占位符行即停——把「围栏起始行 → 占位符前一行」
    视为坏块删除，保留占位符本身及其后内容，并从占位符后继续状态机扫描。
    （AI 漏写闭合 ``` 时，其后的 ```mermaid 起始行语义上正是坏块的真实右边界，
    该行已被 _protect_chart_fences 消费成占位符，此处以占位符还原边界最可靠。）
    """
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        _s = lines[i].strip()
        # BUG-FIX 2026-10-08：原实现用 `stripped.startswith("```")` 判定围栏，
        # 波浪线围栏（~~~python）不在其列 → 未闭合的 ~~~ 块内容会整段泄漏为正文。
        if FENCE_ANY_LINE_PATTERN.match(_s):
            # 开启行的语言标记：闭合行只允许「空 info-string」或与其同语言，
            # 异语言围栏起始行（如未闭合 ```python 后接 ```json）是坏块的右边界，
            # 绝不可当成闭合行（否则代码载荷会整段残留为正文乱码）。
            _open_info = fence_info_string(_s)
            # 找闭合行（代码块不嵌套：第一个**同语言**围栏标记行即闭合）
            _j = i + 1
            closed = False
            boundary = None  # 坏块右边界行号（图表占位符行 或 异语言围栏起始行）
            while _j < len(lines):
                _ls = lines[_j].strip()
                if FENCE_ANY_LINE_PATTERN.match(_ls):
                    if is_fence_close_line(_ls, _open_info):
                        closed = True
                        break
                    boundary = _j
                    break
                # 图表占位符行：坏块的硬边界（占位符是受保护的完整图表块）
                if _FENCE_PLACEHOLDER_PREFIX in lines[_j]:
                    boundary = _j
                    break
                _j += 1
            if closed:
                # ENHANCE 2026-10-08（遗留项1）：开关开启时闭合普通代码块整体剔除
                if _strip_plain_code_blocks_enabled():
                    logger.info(
                        "[围栏清理] STRIP_PLAIN_CODE_BLOCKS 开启，闭合普通代码块已整体剔除（%d 行）: %s",
                        _j - i + 1, _s[:40],
                    )
                    i = _j + 1
                    continue
                out.extend(lines[i : _j + 1])
                i = _j + 1
            elif boundary is not None:
                # 未闭合但找到右边界（图表占位符 或 异语言围栏起始行）：只删
                # 「起始行 → 边界行前」，保留边界行及其后正文/图表，继续扫描后续行
                logger.info(
                    "[围栏清理] 未闭合普通代码块（%d 行已删除，%d 行保留）: %s",
                    boundary - i, len(lines) - boundary, _s[:40],
                )
                i = boundary
            else:
                logger.info(
                    "[围栏清理] 未闭合普通代码块（%d 行已删除）: %s",
                    len(lines) - i, _s[:40],
                )
                break
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


def clean_generated_content(raw: str) -> str:
    if not raw:
        return ""

    # BUG-FIX 2026-10-08（P3 编码）：剥离 UTF-8 BOM（\ufeff）。BOM 粘在首行会挡住
    # HEADING_PATTERNS 的 ^(\d{1,4}) 锚点，首标题被降级为普通段落，
    # 导出文档首个标题丢失编号样式。
    raw = raw.lstrip("\ufeff")

    # BUG-FIX 2026-10-12（P1 编码 / 图表丢失）：CRLF / CR 归一为 LF。
    # Windows 环境下 AI 网关、数据库读回的旧正文、上传的 .md 均可能带 \r\n。
    # content_cleaning 中所有围栏识别（CHART_FENCE_OPEN_PATTERN、iter_chart_fences、
    # _protect_chart_fences、parse_content）都基于 `text.split("\n")` 与
    # `[ \t]*$` 行尾锚点：`\r` 不在 `[ \t]` 类中，`"...mermaid\r\n"` 切完后
    # 首行变成 "```mermaid\r"，`$` 前必须有 `[ \t]*` 能吃掉 \r，但模式没有 →
    # 整段图表围栏不被识别为图表，`extract_section_charts` 返回 0，
    # 图表在数据链上"人间蒸发"，同时 mermaid 源码作为普通段落泄漏到正文。
    # 修复：入口处一次性归一，后续所有 split/正则都在纯 LF 文本上工作。
    if "\r" in raw:
        raw = raw.replace("\r\n", "\n").replace("\r", "\n")

    # BUG-FIX 2026-09-16（低）：去掉 DOTALL。原非贪婪跨行匹配会从任意
    # 「行首为 分析/推理」的行一路吞到全文第一个「输出/结果」——中文正文
    # 含「三、分析：……输出结果……」这类段落时整段丢失。改为**同行闭合**：
    # 仅清理单行内「think 前缀 → 行内 response/输出」形态的思考行。
    raw = re.sub(
        r"(?:^|\n)\s*(?:think|reason|思考|推理|分析)\s*[:：][^\n]*?(?:response|回答|输出|结果)\s*[:：]?[^\n]*",
        "",
        raw,
    )

    # BUG-FIX 2026-10-04（图表模块 D1，P1）：原实现无条件删除所有 ``` 围栏，
    # 把 AI 输出的 ```mermaid / ```chart-json 图表代码块内容一并吞掉（仅删围栏
    # 行，内部节点/载荷行残留成乱码段落）。
    # 行级预处理：① 闭合围栏整块保护为占位符（交由 parse_content 就地渲染）；
    # ② 未闭合围栏（悬挂起始行）删除起始行到文末（或下一个普通 ```）的坏块，
    # 绝不把代码原文写进正文；③ 普通（非图表）代码围栏仍按旧逻辑剥离。
    _fence_cache.blocks.clear()
    raw = _protect_chart_fences(raw)
    # BUG-FIX 2026-10-06（P1）：清理未闭合的**普通**代码围栏坏块。
    # _protect_chart_fences 只保护/删除图表围栏；普通围栏（python/json/text 等）
    # 若未闭合，原全局正则只删围栏行、块内代码残留为正文段落。
    raw = _strip_unclosed_plain_fences(raw)

    # ↓↓↓ 以下步骤必须发生在 _restore_chart_fences **之前** ↓↓↓
    # FIX 2026-10-10（《文档质量分析报告》格式问题 5/13，修复 5/11）：
    # ① LaTeX 公式先于其它清洗转换为可读 Unicode 文本 —— 必须排在
    #    _strip_literal_newline_escapes 之前：`\nu` 这类命令会被其误判成
    #    「字面量 \n + u」切断；\[...\] 也会被转义清理误伤。
    # ② Markdown 转义字符（\~ 等）在标题/粗体清理之后还原，避免还原出的
    #    # 又被剥标题正则吃掉。
    raw = _apply_outside_chart_fences(raw, convert_latex_in_text)
    raw = _apply_outside_chart_fences(raw, _strip_markdown_markup)
    raw = _apply_outside_chart_fences(raw, _unescape_markdown_escapes)
    raw = _apply_outside_chart_fences(raw, _strip_literal_newline_escapes)

    # BUG-FIX 2026-10-08（P1 围栏标记残留）：原正则 `r"```[\w]*\n?"` 的 \w 不含 "-",
    # ```chart-json 这类带连字符的围栏标记行**删不干净**，闭合标记 `` ``` `` 会残留
    # 成正文行。改为匹配整个 info-string（同时支持 ``` 与 ~~~）。
    raw = re.sub(r"(?:`{3,}|~{3,})[^\n`~]*\n?", "", raw)
    raw = re.sub(r"\n?(?:`{3,}|~{3,})", "", raw)

    raw = _restore_chart_fences(raw)
    _fence_cache.blocks.clear()
    # ↑↑↑ 载荷在此刻才被放回 ↑↑↑

    trimmed = raw.strip()
    if trimmed.startswith("{") and trimmed.endswith("}"):
        try:
            parsed = json.loads(trimmed)
            if isinstance(parsed, dict) and "content" in parsed:
                raw = str(parsed["content"])
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass

    raw = re.sub(r"\n{3,}", "\n\n", raw)

    return raw.strip()


def _strip_leading_spaces(text: str) -> tuple[str, int]:
    """去除前导全角/半角空格，返回(清理后文本, 被移除的缩进层级)"""
    match = FULLWIDTH_SPACE_PATTERN.match(text)
    if match:
        removed = match.group(0)
        indent_level = 0
        for ch in removed:
            if ch == "\u3000":
                indent_level += 1
            elif ch == " ":
                pass
        return text[match.end() :], indent_level
    return text, 0


def _match_heading(text: str, base_level: int = 0) -> tuple[int, int, str, str] | None:
    """尝试匹配标题，返回(实际层级, 相对层级, 标题文本, 粘连的正文)或None。"""
    relative_level = None
    m = None

    for pattern, level in HEADING_PATTERNS:
        m = pattern.match(text)
        if m:
            relative_level = level
            break

    if relative_level is None:
        return None

    # V3.0 修复：HEADING_PATTERNS 已直接映射到绝对层级，无需 base_level 偏移
    # 之前的 base_level + relative_level - 2 公式基于旧标准，导致层级膨胀
    actual_level = relative_level

    if len(m.groups()) >= 2:
        heading_text = m.group(m.lastindex).strip()
    elif len(m.groups()) == 1:
        heading_text = m.group(1).strip()
    else:
        heading_text = m.group(0).strip()

    concat_body = ""
    for punct in ["。", "？", "！", "：", ":"]:
        idx = heading_text.find(punct)
        if idx > 0 and idx < len(heading_text) - 1:
            potential_title = heading_text[:idx].strip()
            potential_body = heading_text[idx + 1 :].strip()
            if len(potential_title) <= 50 and len(potential_body) > 5:
                heading_text = potential_title
                concat_body = potential_body
                break

    if len(heading_text) <= 80:
        return actual_level, relative_level, heading_text, concat_body
    return None


# =====================================================================
# BUG-P3-B FIX 2026-10-11：章节重排后正文内旧编号残留
# -----------------------------------------------------------------------------
# 缺口：sections.update_section 改名时已通过 _strip_title_numbering 剥离
# DB title 字段的编号前缀，但 content 正文里 AI 输出的 Markdown 子标题
# （## 1.1 施工部署 / ### 2.3.1 xxx / 一、施工准备 ...）仍带历史编号。
# 导出端从目录结构重新生成编号时，目录层级与正文内嵌编号可能不一致。
#
# 修复：当 parent_id 改变（整棵子树层级重算）时，调用本函数把 content
# 里的所有 Markdown 标题编号前缀剥离，只保留标题正文 → 导出端统一从
# 目录结构生成编号，口径完全一致。
# =====================================================================
def strip_heading_numbering_from_content(content: str) -> str:
    """剥离正文中 Markdown 标题的编号前缀（1.1、一、、1）、第X章等），保留标题正文。

    用于章节重排/重命名后把正文内旧编号清除，让导出端统一从目录结构生成新编号。
    不修改列表项、正文段落或图表代码块。

    支持两种 Markdown 标题格式：
      - ATX: ## 1.1 施工部署 → ## 施工部署
      - Setext: 1.1 施工部署 + ===== → 施工部署 + =====
    """
    if not content:
        return content
    # BUG-P3-B FIX 2026-10-11：正则必须匹配整行。
    #
    # BUG-FIX 2026-10-13（B11 · 误伤正文数量短语）：旧阿拉伯编号分支
    # `\d{1,4}(?:\.\d+){0,2}\s*[、．.]?\s*` 把分隔符设为**可选**，
    # 于是 `## 3 天内完成移交` / `## 5 万元投资额` / `## 2026年施工计划`
    # 都会被剥成 `## 天内完成移交` 等——数字（数量/年份）被吞。
    # 修复：与 HEADING_PATTERNS 的 L3/L4 分支保持同一口径——
    #   · 分隔符改为**必需**（顿号/句点，或「空格 + 中文开头」）；
    #   · 空格分支加「单位/量词」负向前瞻（复用 _UNIT_LEAD_CHARS），
    #     「3 天」「5 万」「2026 年」这类数量短语不再命中；
    #   · 三层小数点（1.1.1）保留无分隔符兼容（AI 偶发 `### 1.1.1标题`）。
    _ATX_HEADING_RE = re.compile(
        r"^(#{1,6})\s+("  # group 1: # 前缀; group 2: 编号前缀（待剥离）
        r"\d{1,4}(?:\.\d+){2,}\s*[、．.]?\s*"  # 1.1.1 阿拉伯编号（三层小数点，分隔符可选）
        r"|\d{1,4}\.\d+(?!\.\d)(?:\s*[、．.]\s*|\s+(?![" + _UNIT_LEAD_CHARS + r"])(?=[一-鿿]))"  # 1.1（两层，分隔符必需，空格分支排量词）
        r"|\d{1,4}(?!\.\d)(?:\s*[、．.]\s*|\s+(?![" + _UNIT_LEAD_CHARS + r"])(?=[一-鿿]))"  # 1（单层，分隔符必需，空格分支排量词）
        r"|[一二三四五六七八九十百千]+[、．.]\s*"  # 一、二、中文编号
        r"|第[一二三四五六七八九十百千万0-9]+[章节]\s*[、．.．]?\s*"  # 第X章/第X节
        r"|\d+\s*[)）]\s*[、．.]?\s*"  # 1）数字右括号
        r"|[a-z]\s*[、．.]\s*)"  # a、字母+顿号
        r"(.+)$",  # group 3: 标题正文（保留）
        re.MULTILINE,
    )
    # Risk-P3a FIX 2026-10-11：Setext 标题（= / -）的编号剥离正则。
    # Setext 格式是「标题文本 + 下一行全 = 或全 -」，此处剥离标题行的编号前缀。
    # BUG-FIX 2026-10-13（B11）：与 ATX 分支同步——按小数点层数分支且分隔符
    # 必需（「1.」不再单独命中，避免「## 1.8 米净高」被剥成「## 8 米净高」），
    # 空格分支加量词负向前瞻。
    _NUMBER_PREFIX_RE = re.compile(
        r"^("  # group 1: 编号前缀（待剥离）
        r"\d{1,4}(?:\.\d+){2,}\s*[、．.]?\s*"
        r"|\d{1,4}\.\d+(?!\.\d)(?:\s*[、．.]\s*|\s+(?![" + _UNIT_LEAD_CHARS + r"])(?=[一-鿿]))"
        r"|\d{1,4}(?!\.\d)(?:\s*[、．.]\s*|\s+(?![" + _UNIT_LEAD_CHARS + r"])(?=[一-鿿]))"
        r"|[一二三四五六七八九十百千]+[、．.]\s*"
        r"|第[一二三四五六七八九十百千万0-9]+[章节]\s*[、．.．]?\s*"
        r"|\d+\s*[)）]\s*[、．.]?\s*"
        r"|[a-z]\s*[、．.]\s*)"
        r"(.+)$",  # group 2: 标题正文（保留）
    )
    _SETEXT_H1_RE = re.compile(r"^=+$")
    _SETEXT_H2_RE = re.compile(r"^-+$")

    def _atx_replacer(match: re.Match) -> str:
        hashes = match.group(1)
        body = match.group(3).strip()
        return f"{hashes} {body}"

    # 遍历 lines：ATX 标题行直接处理；Setext 需要同时看当前行和下一行
    lines = content.split("\n")
    in_code_block = False
    code_fence_char = None
    result_lines = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        # 围栏检测
        if stripped.startswith("```") or stripped.startswith("~~~"):
            if not in_code_block:
                in_code_block = True
                code_fence_char = stripped[:3]
            elif code_fence_char and stripped.startswith(code_fence_char):
                in_code_block = False
                code_fence_char = None
            result_lines.append(line)
            continue
        if in_code_block:
            result_lines.append(line)
            continue
        # 非代码块：优先尝试 ATX 标题剥离
        new_line = _ATX_HEADING_RE.sub(_atx_replacer, line)
        if new_line != line:
            result_lines.append(new_line)
            continue
        # ATX 没命中 → 检查是否是 Setext 标题行（下一行是 === 或 ---）
        next_line = lines[i + 1] if i + 1 < len(lines) else ""
        next_stripped = next_line.strip()
        if _SETEXT_H1_RE.match(next_stripped) or _SETEXT_H2_RE.match(next_stripped):
            # 当前行是 Setext 标题行，尝试剥离编号前缀
            _nm = _NUMBER_PREFIX_RE.match(stripped)
            if _nm:
                new_line = _nm.group(2).strip()
                # 保留原始缩进（非标题行可能有前导空格，但 Setext 标题一般不缩进）
                _leading_space = line[:len(line) - len(line.lstrip())]
                result_lines.append(f"{_leading_space}{new_line}")
                continue
        result_lines.append(line)
    return "\n".join(result_lines)


def _is_list_item(text: str) -> tuple[bool, str, str]:
    """判断是否为列表项。

    Returns:
        (是否列表, 去除手工符号后的内容文本, 类型)
        类型取值：'ordered'（如 "1) xxx" / "1. xxx"）、'bullet'（如 "- xxx" / "• xxx"）、''（非列表）。
    说明（D2 修复 2026-10-07）：原先把 "• "/ "1) " 之类符号硬编码进文本，导出端只做悬挂缩进、
    不写 numPr，导致列表被拍平为纯文本、编号无法重起。现改为返回去标记后的干净内容 + 类型，
    由导出端按类型写入真正的 Word 编号（bullet / decimal），视觉与原有符号一致但可正确嵌套与重起。
    """
    m = LIST_PATTERN.match(text)
    if m:
        return True, m.group(2).strip(), "ordered"
    # FIX 2026-10-10（格式问题 12）：`(1）` / `（1）` 括号编号 → 归一为有序列表，
    # 导出端写入真正的 Word 编号，中英文括号混用问题随之消除。
    m = LIST_PAREN_PATTERN.match(text)
    if m:
        # BUG-P2a FIX 2026-10-11：负向排除年份正文。
        # 原正则只匹配 1-2 位数字 → 误把 (2026)年... 这类正文当列表。
        # 现扩展到 1-3 位后，年份路径不会命中（4 位数被排除在 \d{1,3} 之外），
        # 但显式负检查更安全——防止未来扩展范围时误判。
        if _LIST_PAREN_YEAR_RE.match(text):
            pass  # 年份正文，跳过列表判定
        else:
            return True, m.group(2).strip(), "ordered"
    m = LIST_BULLET_PATTERN.match(text)
    if m:
        return True, m.group(1).strip(), "bullet"
    return False, text, ""


def _split_html_line(line: str) -> tuple[list[str], str]:
    """从富文本 HTML 行中提取图片引用与纯文本（BUG C 修复）。

    Args:
        line: 富文本编辑器输出的一行 HTML（如 <p><img src="data:..."></p> 或 <p>这是<b>粗体</b></p>）

    Returns:
        (images, text)：images 为 <img src> 引用列表；text 为剥离 HTML 标签、
        反转义实体后的纯文本（可能为空字符串，此时整行只含图片）
    """
    images = [m.group(1).strip() for m in HTML_IMG_PATTERN.finditer(line) if m.group(1).strip()]
    text = HTML_TAG_PATTERN.sub("", line)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text).strip()
    return images, text


# ============ FIX 2026-10-10（《文档质量分析报告》格式问题 5/6·修复 4/13）============
# AI 常直接输出 HTML 表格（<table><tr><td>...）。旧链路走「HTML 剥标签 → 纯文本」
# 分支，单元格内容连成一片（报告 Page 8/17/18 的表格全部不可读）。
# 现将 <table> 解析为行列网格（支持 colspan/rowspan 占位），再转成 GFM 管道表，
# 复用既有 table 块 → `_add_table_from_markup` 渲染链路（表头底色/斑马纹/边框全保留）。
_HTML_TABLE_OPEN_RE = re.compile(r"<table\b", re.IGNORECASE)
_HTML_TABLE_CLOSE_RE = re.compile(r"</table\s*>", re.IGNORECASE)
_HTML_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL | re.IGNORECASE)
_HTML_CELL_RE = re.compile(r"<(td|th)\b([^>]*)>(.*?)</\1>", re.DOTALL | re.IGNORECASE)
_HTML_COLSPAN_RE = re.compile(r"""colspan\s*=\s*["']?(\d{1,3})""", re.IGNORECASE)
_HTML_ROWSPAN_RE = re.compile(r"""rowspan\s*=\s*["']?(\d{1,3})""", re.IGNORECASE)

# 与 export._add_table_from_markup 的 _MAX_TABLE_ROWS/COLS 保持一致，防病态输入
_HTML_TABLE_MAX_ROWS = 500
_HTML_TABLE_MAX_COLS = 50
# 未闭合 <table> 的收集上限（行数），超过按已收集内容解析
_HTML_TABLE_MAX_SCAN_LINES = 600


def _html_cell_text(inner: str) -> str:
    """单元格内 HTML → 纯文本（<br> 折行还原为空格、实体反转义）。"""
    text = re.sub(r"<br\s*/?>", " ", inner, flags=re.IGNORECASE)
    text = HTML_TAG_PATTERN.sub("", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    # 单元格内竖线会破坏下游 GFM 解析（导出端按 | 切分列），替换为斜杠
    return text.replace("|", "/")


def parse_html_table_grid(table_html: str) -> list[list[str]]:
    """HTML 表格 → 均匀行列网格，正确处理 colspan / rowspan 占位。

    报告「格式问题 6：表格内容错位」的根因是合并单元格后内容粘连；
    这里用「占用表 + 延迟填充」算法把合并单元格展开成占位空串，
    保证每一行都恰好有相同的列数。
    """
    rows_html = _HTML_TR_RE.findall(table_html or "")
    if not rows_html:
        return []

    occupancy: list[dict[int, str]] = []
    carry: dict[int, tuple[str, int]] = {}  # 列 → (文本, 剩余待填充行数)

    for row_html in rows_html:
        row_idx = len(occupancy)
        if row_idx >= _HTML_TABLE_MAX_ROWS:
            break
        row: dict[int, str] = {}
        # 上一行 rowspan 下来的单元格先占位
        for col, (text, left) in list(carry.items()):
            row[col] = text
            left -= 1
            if left <= 0:
                carry.pop(col, None)
            else:
                carry[col] = (text, left)

        col = 0
        for _cell in _HTML_CELL_RE.finditer(row_html):
            attrs = _cell.group(2) or ""
            text = _html_cell_text(_cell.group(3) or "")
            m_col = _HTML_COLSPAN_RE.search(attrs)
            m_row = _HTML_ROWSPAN_RE.search(attrs)
            colspan = min(int(m_col.group(1)), _HTML_TABLE_MAX_COLS) if m_col else 1
            rowspan = min(int(m_row.group(1)), _HTML_TABLE_MAX_ROWS) if m_row else 1
            colspan = max(1, colspan)
            rowspan = max(1, rowspan)
            # 跳过已被上一行 rowspan 占用的列
            while col in row:
                col += 1
            for offset in range(colspan):
                target = col + offset
                if target >= _HTML_TABLE_MAX_COLS:
                    break
                cell_value = text if offset == 0 else ""
                row[target] = cell_value
                if rowspan > 1:
                    carry[target] = (cell_value, rowspan - 1)
            col += colspan
            if col >= _HTML_TABLE_MAX_COLS:
                break
        occupancy.append(row)

    if not occupancy:
        return []
    max_col = max((max(r) + 1 for r in occupancy if r), default=0)
    max_col = min(max_col, _HTML_TABLE_MAX_COLS)
    return [[row.get(c, "") for c in range(max_col)] for row in occupancy]


def grid_to_gfm_table(grid: list[list[str]]) -> str:
    """均匀网格 → GFM 管道表文本（首行非空行作表头，附分隔行）。

    首行可能被「整行合并的表题单元格」占满，此时取第一行非空行作表头，
    避免导出端因「表头全空」丢弃整张表。
    """
    if not grid:
        return ""
    header_idx = next((i for i, r in enumerate(grid) if any(c.strip() for c in r)), None)
    if header_idx is None:
        return ""
    header = [c.strip() for c in grid[header_idx]]
    body = [r for i, r in enumerate(grid) if i != header_idx]
    col_count = max(len(header), max((len(r) for r in body), default=0))
    header = header + [""] * (col_count - len(header))

    lines = ["| " + " | ".join(header) + " |"]
    lines.append("| " + " | ".join(["---"] * col_count) + " |")
    for row in body:
        row = list(row) + [""] * (col_count - len(row))
        lines.append("| " + " | ".join(c.strip() for c in row) + " |")
    if len(lines) <= 2:
        # 只有表头没有数据行：导出端会因无数据丢弃，补一个空数据行保表
        lines.append("| " + " | ".join([""] * col_count) + " |")
    return "\n".join(lines)


def collect_html_table(lines: list[str], start: int) -> tuple[int, str] | None:
    """从 lines[start] 起收集完整的 `<table>...</table>` 片段。

    Returns:
        (消费行数, 表格 HTML 文本)；lines[start] 不含 <table> 开标签时返回 None。
        未闭合时收集到上限（或文末），返回已收集部分（宁可解析出半张表，
        也不把原始 HTML 泄漏成正文乱码）。
    """
    if start >= len(lines) or not _HTML_TABLE_OPEN_RE.search(lines[start]):
        return None
    first = lines[start]
    if _HTML_TABLE_CLOSE_RE.search(first):
        return 1, first
    collected = [first]
    j = start + 1
    while j < len(lines) and (j - start) < _HTML_TABLE_MAX_SCAN_LINES:
        collected.append(lines[j])
        if _HTML_TABLE_CLOSE_RE.search(lines[j]):
            return j - start + 1, "\n".join(collected)
        j += 1
    return j - start, "\n".join(collected)


def split_html_table_segment(segment: str) -> tuple[str, str, str]:
    """把一行文本切分为（前文, 表格 HTML, 后文）；前/后文保留原始文本。"""
    open_match = _HTML_TABLE_OPEN_RE.search(segment)
    if not open_match:
        return "", "", segment
    close_match = _HTML_TABLE_CLOSE_RE.search(segment, open_match.end())
    before = segment[: open_match.start()]
    if close_match:
        table_html = segment[open_match.start(): close_match.end()]
        after = segment[close_match.end():]
    else:
        table_html = segment[open_match.start():]
        after = ""
    return before, table_html, after


def parse_content(raw: str, base_level: int = 0) -> list[DocumentBlock]:
    """
    将AI生成的纯文本解析为结构化文档块列表。

    处理逻辑：
    1. 按换行符拆分
    2. 去除前导全角空格（用于手动缩进的）
    3. 匹配标题/列表/表格/段落
    4. 处理"标题+正文粘连"情况（标题后直接跟句号和正文）

    Args:
        raw: 原始文本内容
        base_level: 基础层级（根据目录章节层级动态调整）
    """
    if not raw:
        return []

    raw = clean_generated_content(raw)

    lines = raw.split("\n")
    blocks: list[DocumentBlock] = []

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # BUG-FIX 2026-09-18（P2）：行内图表标记切分——检测侧（SCAN，非锁定）会
        # 把 `xxx <<GANTT:{...}>>` 计入图表提示，而解析侧要求“行首 OPEN + 整行
        # PATTERN”不识别它 → 落入 HTML 分支被剥成残字符写出乱码，该图内联/
        # 章末两边都不出。现把此类行拆成「前文 / 标记 / 后文」三段重新入队，
        # 标记段走下方标准解析，文本段按普通正文处理（可终止：每拆一次
        # 标记段必命中 OPEN 分支，剩余行内标记数递减）。
        if not CHART_MARKER_OPEN_PATTERN.match(stripped):
            _inline_m = CHART_MARKER_SCAN_PATTERN.search(stripped)
            if _inline_m and _inline_m.start() > 0:
                _before = stripped[: _inline_m.start()].rstrip()
                _marker = stripped[_inline_m.start(): _inline_m.end()]
                _after = stripped[_inline_m.end():].lstrip()
                lines[i : i + 1] = [x for x in (_before, _marker, _after) if x]
                continue

        # [BUG-FIX] 顺序调整：图表/表格标记优先于 HTML 检查
        # 因为 <<CHART:...>> 和 <<表格:...>> 都以 "<" 开头，
        # HTML_LIKE_PATTERN（<[a-zA-Z/!][^>]*>）会把 <<CHART 里的 "<C" 误判为 HTML 标签
        # 导致 51 个甘特图标记全部被当成 paragraph 跳过
        # ============ ENHANCE 2026-10-04（D2）：```mermaid / ```chart-json 围栏代码块 ============
        # 与 <<CHART:...>> 标记同构处理：解析为 chart_marker 块（就地渲染）；
        # 未闭合/内容非法 → 整块删除（绝不把代码原文写进正文形成乱码，不吞后续正文）。
        if CHART_FENCE_OPEN_PATTERN.match(stripped):
            _fence_open_i = i
            _lang = _normalize_fence_lang(CHART_FENCE_OPEN_PATTERN.match(stripped).group(1))
            # 开启行的原始语言标记：闭合行必须「空 info-string」或与其同语言，
            # 否则异语言起始行（```python 等）会被误判成闭合行（见 is_fence_close_line）。
            _open_info = CHART_FENCE_OPEN_PATTERN.match(stripped).group(1)
            _fence_body: list[str] = []
            _j = i + 1
            _closed = False
            # BUG-FIX 2026-10-08（P1 部分删除泄漏 + 围栏变体）：旧实现的 200 行上限会在
            # 半途停止收集并按「未闭合」处理，`i = _j` 让扫描从围栏内部继续 →
            # 超长 mermaid 块的剩余源码被当作**正文段落**输出到投标文档。
            # 现在统一到 _HARD_CHART_FENCE_LINE_LIMIT 病态上限，闭合判定支持 ~~~。
            while _j < len(lines):
                _s = lines[_j].strip()
                if is_fence_close_line(_s, _open_info):
                    _closed = True
                    break
                if FENCE_ANY_LINE_PATTERN.match(_s):
                    break
                _fence_body.append(lines[_j])
                _j += 1
                if _j - i > _HARD_CHART_FENCE_LINE_LIMIT:
                    break
            if _closed:
                _marker_text = _fence_payload_to_marker_text(_lang, "\n".join(_fence_body))
                if _marker_text is not None:
                    blocks.append(DocumentBlock(type="chart_marker", text=_marker_text))
                else:
                    logger.info("[图表跳过] 围栏代码块内容无法识别（坏块已删除）: lang=%s", _lang)
                i = _j + 1  # 越过闭合围栏行
                continue
            # 未闭合围栏：删除围栏行与已收集的内容行（坏块），不影响后续正文
            logger.info("[图表跳过] 未闭合的图表围栏代码块（%d 行已删除）: lang=%s", len(_fence_body) + 1, _lang)
            i = _j
            _ = _fence_open_i  # 保留定位语义（日志排查用）
            continue

        if CHART_MARKER_OPEN_PATTERN.match(stripped):
            # [CHART-MARKER-MULTILINE] AI 偶尔把标记内的 JSON 折行输出：
            # 单行正则匹配不到时，标记原文会被当作普通段落写进正文（导出后成乱码占位符）。
            # 这里先把「未闭合的图表标记」聚合到闭合符（≤40 行），再统一解析。
            _marker_line_i = i
            if not stripped.endswith((">>", "]]")):
            # FIX 2026-10-08（P2）：聚合上限 40 → 200。折行 JSON 标记超过 40 行时
            # 旧实现按「未闭合」回退为仅消费标记行，剩余 JSON 体被当普通正文
            # 写进文档（乱码段落），且导出端 DOTALL 扫描仍把该类型判为「已内联」
            # → 章末也不补图，图彻底丢失。200 行已覆盖合法折行标记的实际上限。
                _buf = [stripped]
                _j = i + 1
                while _j < len(lines) and len(_buf) < 200:
                    _nxt = lines[_j].strip()
                    _buf.append(_nxt)
                    _j += 1
                    if _nxt.endswith((">>", "]]")):
                        break
                logger.debug("[图表] 多行标记聚合 %d 行", len(_buf))
                stripped = "\n".join(_buf)
                i = _j - 1  # 循环末尾统一 i += 1

            chart_marker = CHART_MARKER_PATTERN.match(stripped)
            if chart_marker is None:
                # 未闭合 / 形态非法：整段消费掉，绝不把标记原文写进文档。
                # BUG-FIX 2026-09-16（低）：聚合失败（40 行内找不到闭合符，如标记被
                # 硬换行截断）时，原实现把后续 39 行正常正文一起丢进「图表跳过」。
                # 改为回退：仅消费标记行本身，后续行按普通正文继续处理。
                logger.info("[图表跳过] 无法解析的图表标记（可能未闭合），仅跳过标记行: %s", stripped[:60])
                i = _marker_line_i + 1
                continue

            chart_type = chart_marker.group(2).lower()
            chart_desc = chart_marker.group(3).strip()
            # [AI JSON 截断修复] 先用标准 json.loads，失败则用 _try_repair_json 修复
            # 修复后同时更新 chart_desc 为有效 JSON，避免下游渲染器重复修复
            parsed_obj = _try_repair_json(chart_desc)
            if parsed_obj is not None:
                # 有效 JSON 数据 → 更新 chart_desc 并创建渲染 block
                chart_desc = json.dumps(parsed_obj, ensure_ascii=False)
                inner_type = str(parsed_obj.get("type") or "").lower()
                if inner_type in SUPPORTED_CHART_TYPES:
                    chart_type = inner_type
                blocks.append(
                    DocumentBlock(
                        type="chart_marker",
                        text=json.dumps(
                            {"chart_type": chart_type, "description": chart_desc},
                            ensure_ascii=False,
                        ),
                    ),
                )
            else:
                # CHART-MARKER-UNIFY 2026-09-16：无内联数据的标记 = 「配图位置提示」。
                # 解析出图表类型后仍产出 chart_marker 块，由导出/预览端就地取用该章节
                # 后端生成的**真实**图表数据渲染；数据缺失时静默跳过（绝不伪造数据）。
                resolved = resolve_chart_marker(chart_type, chart_desc)
                if resolved:
                    _hint_type, _hint_title = resolved
                    blocks.append(
                        DocumentBlock(
                            type="chart_marker",
                            text=json.dumps(
                                {
                                    "chart_type": _hint_type,
                                    "description": _hint_title,
                                    "hint": True,
                                },
                                ensure_ascii=False,
                            ),
                        ),
                    )
                    logger.debug(
                        "[图表提示] 解析配图位置提示 type=%s title=%s",
                        _hint_type,
                        _hint_title[:30],
                    )
                else:
                    logger.info(
                        "[图表跳过] 标记无法识别类型且无真实数据，跳过渲染: %s",
                        chart_desc[:60],
                    )
            i += 1
            continue

        if TABLE_PATTERN.match(stripped):
            table_lines = [stripped]
            i += 1
            # P1 修复：单行闭合表格（如 `<<表格：a|b>>`）不应进入收集循环，
            # 否则循环会持续吞掉后续段落直到遇到下一个 `>>` 或标记行
            if stripped.endswith(">>"):
                blocks.append(DocumentBlock(type="table", text="\n".join(table_lines)))
                continue
            while i < len(lines):
                tline = lines[i].strip()
                if tline.endswith(">>"):
                    table_lines.append(tline)
                    i += 1
                    break
                if not tline:
                    i += 1
                    continue
                if (
                    CHART_MARKER_PATTERN.match(tline)
                    or MD_TABLE_PATTERN.match(tline)
                    or tline.startswith("[[")
                    or tline.startswith("<<")
                ):
                    break
                table_lines.append(tline)
                i += 1
                # P1 修复：无闭合 `>>` 的表格块设置行数上限，防止吞掉到 EOF
                if len(table_lines) >= 60:
                    break
            table_text = "\n".join(table_lines)
            blocks.append(DocumentBlock(type="table", text=table_text))
            continue

        # P2 修复：MD 表格检查先于 HTML 检查——
        # 含内联 HTML（如 `<br>`）的表格行 `| a | b<br>c |` 应整体归属表格，
        # 而不是被 HTML 分支剥标签拆散成段落
        if MD_TABLE_PATTERN.match(stripped):
            table_lines = [stripped]
            i += 1
            while i < len(lines):
                tline = lines[i].strip()
                if not tline or not tline.startswith("|"):
                    break
                # P2 修复：保留 GFM 分隔行（|---|---|），保证 block.text 是完整
                # 合法 GFM 表格（3+ 行、分隔行仅 - 和 :），下游序列化消费方
                # 不会产出非法 Markdown；DOCX 导出端已自行剔除分隔行，不受影响
                table_lines.append(tline)
                i += 1
            table_text = "\n".join(table_lines)
            blocks.append(DocumentBlock(type="table", text=table_text))
            continue

        # FIX 2026-10-10（《文档质量分析报告》格式问题 5/6·修复 4/13）：
        # HTML 表格优先于下方「剥标签成纯文本」分支 —— 旧链路把
        # <table><tr><td>… 的单元格连成一片写进正文，工程概况/材料参数表
        # 完全无法阅读。这里解析成网格 → GFM 管道表 → 复用 table 块渲染链路。
        if _HTML_TABLE_OPEN_RE.search(stripped):
            collected = collect_html_table(lines, i)
            consumed, table_source = collected if collected else (1, stripped)
            before_text, table_html, after_text = split_html_table_segment(table_source)
            # 同行前文/后文：仍按富文本规则提取图片与纯文本
            for seg in (before_text, after_text):
                if not seg.strip():
                    continue
                seg_images, seg_plain = _split_html_line(seg)
                for img_src in seg_images:
                    blocks.append(DocumentBlock(type="image", text=img_src))
                if seg_plain:
                    blocks.append(DocumentBlock(type="paragraph", text=seg_plain))
            grid = parse_html_table_grid(table_html)
            gfm = grid_to_gfm_table(grid)
            if gfm:
                blocks.append(DocumentBlock(type="table", text=gfm))
            else:
                # 无 <tr> 结构的畸形表格：退回旧行为，剥标签保文本（不泄漏 HTML）
                _fallback_images, _fallback_text = _split_html_line(table_source)
                for img_src in _fallback_images:
                    blocks.append(DocumentBlock(type="image", text=img_src))
                if _fallback_text:
                    blocks.append(DocumentBlock(type="paragraph", text=_fallback_text))
            i += consumed
            continue

        # 富文本编辑器（RichTextEditor）产生的 HTML 内容：
        # 1) <img src="..."> 提取为 image 块
        # 2) 其余 HTML 标签剥离为纯文本段落
        if HTML_LIKE_PATTERN.search(stripped):
            images, img_text = _split_html_line(stripped)
            for img_src in images:
                blocks.append(DocumentBlock(type="image", text=img_src))
            if img_text:
                blocks.append(DocumentBlock(type="paragraph", text=img_text))
            i += 1
            continue

        # FIX 2026-10-08（P2 列表拍平）：缩进层级必须从**原始行**计算——上方
        # stripped = line.strip() 已把前导全角空格剥掉，_strip_leading_spaces
        # 恒得 0，嵌套列表层级会整体丢失。口径：每个全角空格 1 级，半角空格
        # 每 2 个折 1 级，封顶 4 级。
        _m_lead = re.match(r"^([\u3000 ]+)", line)
        _indent = 0
        if _m_lead:
            _lead = _m_lead.group(1)
            _indent = min(4, _lead.count("\u3000") + len(_lead.replace("\u3000", "")) // 2)
        cleaned, _ = _strip_leading_spaces(stripped)

        heading_match = _match_heading(cleaned, base_level)
        if heading_match:
            level, rel_level, heading_text, concat_body = heading_match
            blocks.append(
                DocumentBlock(
                    type=f"heading{level}", text=heading_text, level=level, relative_level=rel_level,
                    raw_line=cleaned,
                )
            )
            if concat_body:
                blocks.append(DocumentBlock(type="paragraph", text=concat_body))
            i += 1
            continue

        is_list, list_text, list_kind = _is_list_item(cleaned)
        # FIX 2026-10-08（P3）：纯 "-" / "---" 水平装饰线命中 LIST_BULLET_PATTERN
        # 会变成列表项文本 "---"。剔除符号后仅剩连字符/破折号的行不是列表。
        if is_list and list_text and set(list_text.strip()) <= set("-—–‐‑‒"):
            is_list = False
        if is_list:
            # D2 修复：区分有序/无序列表，导出端据此写入真正的 Word 编号（numPr）
            # FIX 2026-10-08（P2 列表拍平）：缩进层级（全角空格数）写入 level 字段，
            # 导出端按 level 附加左缩进，嵌套列表层级不再被拍平为同级。
            list_type = "list_ordered" if list_kind == "ordered" else "list_bullet"
            blocks.append(DocumentBlock(type=list_type, text=list_text, level=min(_indent, 4)))
            i += 1
            continue

        if len(cleaned) > 0:
            blocks.append(DocumentBlock(type="paragraph", text=cleaned))

        i += 1

    blocks = _split_heading_body_concat(blocks, base_level)

    return blocks


def _split_heading_body_concat(
    blocks: list[DocumentBlock],
    base_level: int = 0,
) -> list[DocumentBlock]:
    """后处理：拆分段落中可能残留的"标题。正文"粘连情况"""
    result: list[DocumentBlock] = []

    for block in blocks:
        if block.type != "paragraph":
            result.append(block)
            continue

        text = block.text
        heading_match = _match_heading(text, base_level)
        if heading_match:
            level, _rel_level, heading_text, concat_body = heading_match
            if concat_body:
                result.append(DocumentBlock(
                    type=f"heading{level}", text=heading_text, level=level, raw_line=text,
                ))
                result.append(DocumentBlock(type="paragraph", text=concat_body))
            else:
                result.append(block)
        else:
            result.append(block)

    return result


# ===========================================================================
# BUG-2026-1008-02 修复（P1）：正文标题去重清洗器
# ===========================================================================


# 编号前缀正则：匹配 "1、" / "1." / "1." / "（1）" / "(1)" / "一、" / "（一）" 等
# BUG-FIX 2026-10-12（G-2）：裸数字分支此前会把 "2024年项目总结" 里的 "2024" 当作编号
# 前缀剥掉，导致标题首字 "年" 被孤立。加负向前瞻 (?![年月]) 排除年份 / 月份。
_NUMBERING_PREFIX = re.compile(
    r"^\s*(?:\d+[.、．,]|（?\d+(?![年月])）?|[\(（]\d+[\)）]|[一二三四五六七八九十]+[、．.])\s*"
)

# Markdown 标题标记正则：匹配行首 # 到 ######
_MARKDOWN_HEADING = re.compile(r"^\s*#{1,6}\s*")


class ContentCleaner:
    """正文清洗器：移除 AI 生成内容中的重复标题与格式残留。

    BUG-2026-1008-02 修复（P1）：AI 生成章节内容时，经常在 content 字段
    开头重复输出章节标题（如 content = "主体结构施工节奏管控\\n\\n本项目..."），
    导致导出文档中出现「2、主体结构施工节奏管控」+「主体结构施工节奏管控」
    的双重标题。

    清洗策略（保守、幂等）：
      1. _remove_duplicate_title：检查前 3 行是否重复标题，命中则移除
      2. _remove_markdown_headings：移除行首 # 标记
      3. _remove_leading_numbering：移除首行编号前缀

    使用方式::

        cleaner = ContentCleaner()
        cleaned = cleaner.clean(title="主体结构施工节奏管控", content="主体结构施工节奏管控\\n\\n本项目...")
    """

    def clean(self, title: str, content: str) -> str:
        """执行完整清洗流程，返回去重后的正文。

        Args:
            title: 章节标题（用于检测正文开头的重复标题）
            content: AI 生成的原始正文

        Returns:
            清洗后的正文。空/None 输入原样返回。
        """
        if not content or not isinstance(content, str):
            return content

        original = content
        content = self._remove_duplicate_title(title, content)
        content = self._remove_markdown_headings(content)
        content = self._remove_leading_numbering(content)
        cleaned = content.strip()

        if cleaned != original.strip():
            logger.info(
                "[ContentCleaner] 正文清洗生效: title=%r 原长=%d 新长=%d",
                (title or "")[:30],
                len(original),
                len(cleaned),
            )
        return cleaned

    def _remove_duplicate_title(self, title: str, content: str) -> str:
        """移除正文开头的重复标题行。

        检查前 3 行（跳过空行），若某行（去掉编号前缀后）与标题一致，
        则移除该行及其后的连续空行。

        示例：
          title = "主体结构施工节奏管控"
          content = "主体结构施工节奏管控\\n\\n本项目主体..."
          → "本项目主体..."

          title = "主体结构施工节奏管控"
          content = "2、主体结构施工节奏管控\\n本项目主体..."
          → "本项目主体..."
        """
        if not title or not title.strip():
            return content

        title_stripped = title.strip()
        lines = content.split("\n")

        for i in range(min(3, len(lines))):
            line = lines[i].strip()
            if not line:
                continue

            # 移除编号前缀后比较
            line_clean = _NUMBERING_PREFIX.sub("", line).strip()
            # 也移除 Markdown 标题标记
            line_clean = _MARKDOWN_HEADING.sub("", line_clean).strip()

            if line_clean == title_stripped:
                # 找到重复标题行，移除该行及其后连续空行
                lines = lines[i + 1 :]
                while lines and not lines[0].strip():
                    lines = lines[1:]
                return "\n".join(lines)

        return content

    def _remove_markdown_headings(self, content: str) -> str:
        """移除所有行首的 Markdown 标题标记（# 到 ######）。

        示例：
          "### 标题\\n正文" → "标题\\n正文"
          "# 一级标题" → "一级标题"

        注意：仅移除行首的 #，不影响正文中的 # 字符。
        """
        lines = content.split("\n")
        result = []
        for line in lines:
            cleaned = _MARKDOWN_HEADING.sub("", line)
            result.append(cleaned)
        return "\n".join(result)

    def _remove_leading_numbering(self, content: str) -> str:
        """移除首行的编号前缀（仅首行，保留列表编号）。

        示例：
          "1、本项目主体..." → "本项目主体..."
          "（1）本项目主体..." → "本项目主体..."
          "本项目主体..." → "本项目主体..."（无编号，不变）

        注意：仅处理首行，列表中的编号（如 "1. 第一步" 在段落中）不受影响。
        """
        lines = content.split("\n")
        if not lines:
            return content

        first_line = lines[0]
        cleaned_first = _NUMBERING_PREFIX.sub("", first_line).strip()
        lines[0] = cleaned_first

        return "\n".join(lines)

    def prepare_for_export(self, title: str, content: str) -> str:
        """导出端防御性清洗（《文档质量分析报告》格式问题 4/10·修复 8）。

        报告记录的缺陷：生成期清洗（`ContentCleaner.clean`）之后落库的数据、
        导入/手工编辑的历史正文，导出时仍可能带「正文首行重复章节标题」与
        「`## 1工程基本情况` 形态的 Markdown 标题残留」，最终出现在成品 DOCX。

        与 :meth:`clean` 的区别（刻意保守）：
          1. 只做**幂等且无损**的两步 —— 去重复标题 + 剥行首 `#`；
          2. **不做** `_remove_leading_numbering`：导出正文的首行可能是
             合法列表项「1. 《规范》…」，剥编号会改写内容。
        """
        if not content or not isinstance(content, str):
            return content
        content = self._remove_duplicate_title(title, content)
        content = self._remove_markdown_headings(content)
        return content

