from dataclasses import dataclass
import html
import json
import logging
import re
import threading
from typing import Literal

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


HEADING_PATTERNS = [
    # 七级标题标准（与 heading_standard.HEADING_STYLE_CONFIG 对齐）
    # 注意：具体模式优先于通用模式，避免误匹配
    # 五级: 1.1.1 → L5（3层小数点）
    (re.compile(r"^(\d+(?:\.\d+){2,})\s*[、．.]?\s*(.+)$"), 5),
    # 四级: 1.1 → L4（2层小数点）
    (re.compile(r"^(\d+\.\d+)(?!\.\d)\s*[、．.]?\s*(.+)$"), 4),
    # 三级: 阿拉伯数字（1、2、） → L3（纯数字，无小数点）
    # BUG-FIX 2026-10-06（P1）：原模式 `[、．.]` 为**必需**分隔符，AI 偶尔输出的
    # "1 施工准备"（数字+空格、无顿号/句点）不匹配 → 整行被拍平为普通段落，
    # 标题层级/编号样式在导出文档中全部丢失。
    # 修复：分隔符改为「顿号/句点」或「空格+中文标题」二选一；空格分支用
    # lookahead 锚定中文字符开头，排除 "1 000 元"（空格后是数字）这类正文误匹配。
    (re.compile(r"^(\d+)(?!\.\d)(?:\s*[、．.]\s*|\s+(?=[一-鿿]))(.+)$"), 3),
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
CHART_FENCE_OPEN_PATTERN = re.compile(
    r"^```[ \t]*(mermaid|chart-json)[ \t]*$",
    re.IGNORECASE,
)
CHART_FENCE_SCAN_PATTERN = re.compile(
    r"^```[ \t]*(mermaid|chart-json)[ \t]*\n(.*?)^```[ \t]*$",
    re.DOTALL | re.IGNORECASE | re.MULTILINE,
)

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
        lang = m.group(1).lower()
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
    for m in CHART_MARKER_SCAN_PATTERN.finditer(content):
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


def _try_repair_json(raw: str) -> dict | None:
    """尝试修复 AI 截断的 JSON，返回解析后的 dict 或 None"""
    import json as _json

    try:
        obj = _json.loads(raw)
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
        # 收集到闭合行（上限 200 行防御）
        body: list[str] = [lines[i]]
        _j = i + 1
        closed = False
        while _j < len(lines) and len(body) < 201:
            if lines[_j].strip() == "```":
                body.append(lines[_j])
                closed = True
                _j += 1
                break
            # 遇到另一个围栏起始行：视为原块未闭合（AI 漏写 ```）
            if CHART_FENCE_OPEN_PATTERN.match(lines[_j].strip()) or lines[_j].lstrip().startswith("```"):
                break
            body.append(lines[_j])
            _j += 1
        if closed:
            blocks.append("\n".join(body))
            out.append(f"{_FENCE_PLACEHOLDER_PREFIX}{len(blocks) - 1}\x00")
            i = _j
        else:
            # 未闭合：删除坏块（起始行 + 已收集的 body 行），从 _j 继续处理正常正文
            logger.info(
                "[图表跳过] 未闭合的图表围栏代码块（坏块 %d 行已删除）: lang=%s",
                len(body),
                m.group(1).lower(),
            )
            i = _j
    _fence_cache.blocks.extend(blocks)
    return "\n".join(out)


def _restore_chart_fences(text: str) -> str:
    """把占位符还原为完整的图表围栏块（与 _protect_chart_fences 配对）。"""
    blocks = _fence_cache.blocks
    for _i, _blk in enumerate(blocks):
        text = text.replace(f"{_FENCE_PLACEHOLDER_PREFIX}{_i}\x00", _blk)
    return text


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
      - 未闭合坏块：删除从起始行到文末的所有坏行，绝不把代码原文写进正文。
    """
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith("```"):
            # 找闭合行（代码块不嵌套：第一个 ``` 行即闭合）
            _j = i + 1
            closed = False
            while _j < len(lines):
                if lines[_j].strip().startswith("```"):
                    closed = True
                    break
                _j += 1
            if closed:
                out.extend(lines[i : _j + 1])
                i = _j + 1
            else:
                logger.info(
                    "[围栏清理] 未闭合普通代码块（%d 行已删除）: %s",
                    len(lines) - i, stripped[:40],
                )
                break
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


def clean_generated_content(raw: str) -> str:
    if not raw:
        return ""

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

    raw = re.sub(r"```[\w]*\n?", "", raw)
    raw = re.sub(r"\n?```", "", raw)

    raw = _restore_chart_fences(raw)
    _fence_cache.blocks.clear()

    trimmed = raw.strip()
    if trimmed.startswith("{") and trimmed.endswith("}"):
        try:
            parsed = json.loads(trimmed)
            if isinstance(parsed, dict) and "content" in parsed:
                raw = str(parsed["content"])
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass

    raw = re.sub(r"\*\*(.*?)\*\*", r"\1", raw)

    # 清理 Markdown 标题标记（如 ### 标题 -> 标题）
    raw = re.sub(r"^(#{1,6})\s+", r"", raw, flags=re.MULTILINE)

    raw = raw.replace("\\n", "\n")

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
            _fence_body: list[str] = []
            _j = i + 1
            _closed = False
            while _j < len(lines) and len(_fence_body) < 200:
                _nxt = lines[_j]
                if _nxt.strip() == "```":
                    _closed = True
                    break
                _fence_body.append(_nxt)
                _j += 1
            _lang = CHART_FENCE_OPEN_PATTERN.match(stripped).group(1).lower()
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
                _buf = [stripped]
                _j = i + 1
                while _j < len(lines) and len(_buf) < 40:
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

        cleaned, _indent = _strip_leading_spaces(stripped)

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
        if is_list:
            # D2 修复：区分有序/无序列表，导出端据此写入真正的 Word 编号（numPr）
            list_type = "list_ordered" if list_kind == "ordered" else "list_bullet"
            blocks.append(DocumentBlock(type=list_type, text=list_text))
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
