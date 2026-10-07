from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import UTC, datetime
import gc
import hashlib
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import re
import threading
import time

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
import httpx


try:
    from docx.enum.text import WD_ALIGN_PARAGRAPH
except ImportError:
    WD_ALIGN_PARAGRAPH = None
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Cm, Pt, RGBColor


try:
    from PIL import Image
except ImportError:
    Image = None
    # BUG-E-04 修复：PIL不可用时添加警告日志，便于排查图表渲染降级问题
    import logging as _logging

    _pil_logger = _logging.getLogger(__name__)
    _pil_logger.warning(
        "PIL/Pillow 未安装，图像相关功能（图表渲染、图片验证）将不可用。"
        "请运行: pip install Pillow"
    )

from ..utils.scoring_points import strip_scoring_placeholders
from ..config import (
    CHART_FAIL_PLACEHOLDER,
    PROJECTS_DATA_DIR,
    EXPORT_TABLE_HEADER_BG,
    EXPORT_TABLE_ALT_ROW_BG,
    EXPORT_TABLE_HEADER_FG,
)
from ..db import get_db
from ..models import DocumentCheckResult, EmptySection, ExportConfig
from .ai.heading_standard import (
    HEADING_STYLE_CONFIG,
    HeadingNumberingGeneratorV2,
)
from .ai.mermaid_renderer import (
    ChartCache,
    render_mermaid_to_bytes,
)
from .content_cleaning import extract_mermaid_title, parse_content
from .export_presets import resolve_export_config
from .sections import build_tree, list_sections


logger = logging.getLogger(__name__)

# 统一图表类型映射（中央化维护，消除 _add_inline_chart 和 _render_section_charts 的重复定义）
# 格式: {chart_type: (json_field, default_title)}
_CHART_TYPE_MAP = {
    "gantt": ("gantt_json", "施工进度计划"),
    "flowchart": ("flowchart_json", "施工流程图"),
    "architecture": ("architecture_json", "组织架构图"),
    "labor": ("labor_json", "劳动力配置计划"),
    "comparison": ("comparison_json", "对比图"),
    "layout": ("layout_json", "总平面布置图"),
    "timeline": ("timeline_json", "关键里程碑时间线"),
}

# V6.0 图表渲染缓存（基于 Mermaid 代码哈希值，避免重复渲染）
# 缓存在项目数据目录下，确保跨会话持久化且不会丢失
# =====================================================================
# FALLBACK MERMAID GENERATOR (NO AGNES API REQUIRED)
# =====================================================================
# When Agnes API is throttled (429) or sections have no chart_json data,
# we can still render charts locally using title-based templates.
_CHART_FALLBACK_KEYWORDS = {
        "gantt": ["甘特", "进度", "工期", "计划", "阶段", "时序", "穿插", "流水", "劳动力"],
        "flowchart": ["流程", "工艺", "步骤", "操作", "验收", "检验", "绘制", "施工", "交付"],
        "architecture": ["架构", "组织", "体系", "职责", "矩阵", "岗位", "角色"],
        "labor": ["劳动力", "人员", "工种", "班组", "人数"],
        "comparison": ["对比", "比选", "方案", "权重"],
        "layout": ["布置", "平面", "规划", "场地", "塔吊"],
        "timeline": ["里程碑", "时间线", "关键节点"],
    }

# D5 加固开关（2026-10-07）：章节无图表数据/无图表标记时，按标题本地模板兜底补图。
# 默认关闭（"0"）以保留现有行为；设为 "1" 开启。开启后：
#   1) 图表类型已识别但章节无 *_json 数据时，用 _generate_local_mermaid_chart 生成
#      结构骨架图（非伪造数字，符合"无降级"原则），避免该位置完全空白；
#   2) 章节标题命中 _CHART_FALLBACK_KEYWORDS 且无任何图表标记时，自动补一张标题图。
# 依赖 render_mermaid_to_bytes 的渲染器（HTTP Service / PIL v2）可用，否则仍按
# 既有逻辑跳过（不伪造数据）。
AUTO_FALLBACK_CHARTS = os.getenv("AUTO_FALLBACK_CHARTS", "0") == "1"


def _afc_enabled(config) -> bool:
    """D5 加固：是否启用图表标题兜底补图。

    优先级：显式传入的 ExportConfig.auto_fallback_charts（逐次导出可控）> 环境变量全局开关。
    默认两者皆关，保持历史行为不变。
    """
    if config is not None and getattr(config, "auto_fallback_charts", False):
        return True
    return AUTO_FALLBACK_CHARTS


def _generate_local_mermaid_chart(chart_type: str, title: str = "", description: str = "") -> dict | None:
    """本地生成兜底 Mermaid 数据（不依赖 Agnes API）

    Returns a dict with "type", "code" (Mermaid text), and "title".
    Returns None if chart_type is unsupported.
    """
    title = (title or "").strip() or _CHART_TYPE_MAP.get(chart_type, ("", "图表"))[1]
    ctype = chart_type.lower()

    if ctype == "flowchart":
        # Generate 4-6 generic workflow steps based on title keywords
        steps = ["准备", "执行", "检查", "完成"]
        if "验收" in title or "检验" in title:
            steps = ["进场", "自检", "专检", "验收"]
        elif "施工" in title or "工艺" in title:
            steps = ["放线", "开挖", "施工", "验收", "交付"]
        elif "流程" in title:
            steps = ["起点", "处理", "审核", "结束"]
        mermaid = "flowchart TD\n"
        for i, step in enumerate(steps):
            mermaid += f"    A{i}[{step}]"
            if i < len(steps) - 1:
                mermaid += f" --> A{i+1}"
            mermaid += "\n"
        return {"type": "flowchart", "code": mermaid, "title": title}

    elif ctype == "gantt":
        tasks = ["准备阶段", "主体阶段", "收尾阶段"]
        if "施工" in title:
            tasks = ["土方开挖", "主体结构", "装饰工程", "竣工验收"]
        elif "穿插" in title:
            tasks = ["地下工程", "主体工程", "围护工程", "装饰工程"]
        mermaid = f"""gantt
    title {title}
    dateFormat  YYYY-MM-DD
    axisFormat %m/%d
    """
        for i, task in enumerate(tasks):
            start = f"2025-0{(i+1)*2}"
            end = f"2025-0{(i+1)*2+1}"
            if i > 0 and len(start) > 9:
                start = f"2025-0{i*2+1}"
                end = f"2025-0{i*2+2}"
            mermaid += f"    section {task}\n    {task} :a{i}, {start}, 30d\n"
        return {"type": "gantt", "code": mermaid, "title": title}

    elif ctype == "architecture":
        mermaid = f"""flowchart TD
    subgraph {title}
        direction LR
        A[决策层] --> B[管理层]
        B --> C[执行层]
    end"""
        return {"type": "architecture", "code": mermaid, "title": title}

    elif ctype == "labor":
        mermaid = f"""graph LR
    A[项目总工] --> B[施工员]
    A --> C[质检员]
    B --> D[劳务班组]
    C --> D"""
        return {"type": "labor", "code": mermaid, "title": title}

    elif ctype == "comparison":
        mermaid = f"""flowchart LR
    A[方案一] --> C{{比选}}
    B[方案二] --> C
    C --> D[推荐方案]"""
        return {"type": "comparison", "code": mermaid, "title": title}

    elif ctype == "layout":
        mermaid = f"""graph TD
    A[塔吊区] --> B[加工场]
    B --> C[材料堆场]
    C --> D[施工区]"""
        return {"type": "layout", "code": mermaid, "title": title}

    elif ctype == "timeline":
        mermaid = f"""flowchart LR
    A[开工] --> B[关键节点1]
    B --> C[关键节点2]
    C --> D[竣工]"""
        return {"type": "timeline", "code": mermaid, "title": title}

    return None


def _infer_chart_type_from_text(text: str) -> str:
    """从中文文本推断图表类型"""
    if not text:
        return "flowchart"
    scores = {ct: sum(1 for kw in kws if kw.lower() in text.lower()) for ct, kws in _CHART_FALLBACK_KEYWORDS.items()}
    return max(scores, key=scores.get) if max(scores.values()) > 0 else "flowchart"


_EXPORT_CACHE_DIR = PROJECTS_DATA_DIR / "_exports" / "charts"
_EXPORT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_chart_cache = ChartCache(cache_dir=str(_EXPORT_CACHE_DIR))

# V1.2 性能优化：内联图表标记缓存（按 section_detail id 缓存检测结果，避免重复字符串扫描）
# V2.0 增强：添加 TTL 淘汰机制，避免长期缓存占用内存
# V3.1：value 扩为 (内联类型全集, 位置提示子集, timestamp)
_inline_marker_cache: dict[int, tuple[set[str], set[str], float]] = {}  # key -> (all, hints, ts)
_inline_marker_cache_lock = threading.Lock()
_inline_marker_cache_ttl = 300  # TTL: 5 分钟
_inline_marker_cache_max = 200  # 最多缓存 200 个章节
_inline_marker_cache_hits = 0
_inline_marker_cache_misses = 0

# V7.0 图表编号（标书级 DOCX 导出）
# 每个导出会话内按 section 级 parent number 计数，格式 "图{chapter}-{seq}"
# 同一 parent section 下的图表递增序号，跨 section 重置
# P1 修复 2026-09-29（并发导出串号）：计数器 key 加 project 维度。
#   原实现是**进程级全局** dict + build_docx 开头无条件 clear：两个项目同时
#   导出时，A 的计数器会被 B 的 reset 清空（A 后续图号从头重算 → 同一文档内
#   出现重复图号），且 A/B 共享计数 → 图号互串。
_figure_counter_lock = threading.Lock()
_figure_counter_state: dict[str, int] = {}  # {"{project_id}|{parent_number}": current_count}

# 图号前缀（"图1-2 "）——用于从失败文案里剔除已回滚的编号
_FIG_NO_PREFIX_RE = re.compile(r"^图\d+-\d+\s*")


def _figure_counter_key(parent_section: str, project_id: int | None) -> str:
    """计数器分桶键：项目维度 + 父章节号（缺项目时归入 '-' 桶）"""
    return f"{project_id if project_id is not None else '-'}|{parent_section or 'default'}"


def _reset_figure_counter(project_id: int | None = None):
    """导出开始时重置图表计数器

    Args:
        project_id: 给定时只清该项目的桶（并发导出互不干扰）；
            不给定时清全部（保留给单测/全量重跑等既有调用方式）。
    """
    with _figure_counter_lock:
        if project_id is None:
            _figure_counter_state.clear()
        else:
            prefix = f"{project_id}|"
            for k in [k for k in _figure_counter_state if k.startswith(prefix)]:
                _figure_counter_state.pop(k, None)


def _strip_fig_no_for_cache(payload) -> str:
    """生成图表缓存键：剔除 fig_no（会话内递增图号），并做 key 排序归一化。

    fig_no 每次导出都在变，若进入缓存键会导致预渲染与正式渲染永不命中；
    sort_keys 归一化可让语义相同、字段顺序不同的 payload 共享同一份缓存。
    """
    if isinstance(payload, dict):
        obj: object = {k: v for k, v in payload.items() if k != "fig_no"}
    elif isinstance(payload, str):
        try:
            obj = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return payload
        if isinstance(obj, dict):
            obj = {k: v for k, v in obj.items() if k != "fig_no"}
    else:
        obj = payload
    try:
        return json.dumps(obj, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(obj)


def _next_figure_number(parent_section: str, project_id: int | None = None) -> str:
    """获取下一个图表编号（线程安全）

    Args:
        parent_section: 父章节编号字符串（如 "3" 或 "3.2"）
        project_id: 项目 ID；与父章节号一起构成分桶键，避免跨项目串号

    Returns:
        格式化的图号，如 "图3-1"、"图3-2"
    """
    bucket = _figure_counter_key(parent_section, project_id)
    with _figure_counter_lock:
        _figure_counter_state[bucket] = _figure_counter_state.get(bucket, 0) + 1
        seq = _figure_counter_state[bucket]
    # 取 parent_section 的第一段作为章号（"3.2" → "3"）
    key = parent_section or "default"
    chapter = key.split(".")[0] if "." in key else key
    return f"图{chapter}-{seq}"


def _rewind_figure_number(parent_section: str, project_id: int | None = None) -> None:
    """回滚刚分配的图号（P1 修复 2026-09-29：渲染失败不得占号）

    原实现在调渲染器**之前**就取号，渲染失败时号不回退 → 文档图号虚跳
    （图1-1 直接跳到图1-3），且失败说明里带着一个实际不存在的编号。
    回滚后下一张图复用该号，正文图号保持连续。
    """
    bucket = _figure_counter_key(parent_section, project_id)
    with _figure_counter_lock:
        cur = _figure_counter_state.get(bucket, 0)
        if cur <= 1:
            _figure_counter_state.pop(bucket, None)
        else:
            _figure_counter_state[bucket] = cur - 1


# BUG-E-05 修复：后台 TTL 清理线程，避免缓存仅靠访问时清理导致的长期内存占用
def _inline_marker_cache_cleanup_loop():
    """后台线程：每 60 秒扫描一次，清理过期条目"""
    while True:
        time.sleep(60)
        now = time.time()
        with _inline_marker_cache_lock:
            expired = [
                k
                for k, (_, _, ts) in _inline_marker_cache.items()
                if now - ts >= _inline_marker_cache_ttl
            ]
            for k in expired:
                del _inline_marker_cache[k]


_inline_marker_cache_cleanup_thread = None


def _start_inline_marker_cache_cleanup():
    """启动后台清理线程（单例）"""
    global _inline_marker_cache_cleanup_thread
    if (
        _inline_marker_cache_cleanup_thread is None
        or not _inline_marker_cache_cleanup_thread.is_alive()
    ):
        _inline_marker_cache_cleanup_thread = threading.Thread(
            target=_inline_marker_cache_cleanup_loop,
            daemon=True,
            name="inline_marker_cache_cleanup",
        )
        _inline_marker_cache_cleanup_thread.start()


# V1.2 性能优化：图表渲染结果 L1 内存缓存（避免重复 render_mermaid_to_bytes 调用）
# V2.0 增强：添加 TTL 淘汰机制，避免长期缓存占用内存
# V3.0 增强：添加命中率统计，支持缓存监控
_chart_render_cache: dict[str, tuple[BytesIO, int, float]] = {}  # key -> (stream, size, timestamp)
_chart_render_cache_lock = threading.Lock()
_chart_render_cache_max = 50  # 最多缓存 50 个不同 chart 渲染结果
_chart_render_cache_ttl = 300  # TTL: 5 分钟（300秒），超过此时间自动淘汰
_chart_render_cache_hits = 0
_chart_render_cache_misses = 0

# =====================================================================
# SINGLE-FLIGHT 2026-10-06：并发同 key 渲染合并（缓存击穿修复）
# =====================================================================
# 问题：预渲染线程池（≤8 线程）与正文渲染可能同时请求「同一份图表」
# （同一 payload，仅 fig_no 不同，已被 _strip_fig_no_for_cache 归一化为同 key）。
# 旧实现 L1/L2 缓存都 miss 后各自调 render_mermaid_to_bytes → 同一图表被重复渲染
# N 次（实测 8 线程并发同数据，真实渲染被触发 8 次），浪费 CPU/IO 且放大超时风险。
# 修复：同 cache_key 的并发请求合并为一次渲染——首个线程（owner）真正渲染，
# 其余线程等待其完成后共享结果；owner 渲染失败则结果为 None，等待方同步判失败。
# 仅合并「真正耗时的渲染动作」，缓存命中路径不受影响（不串行化热点）。
_inflight_renders: dict[str, threading.Event] = {}   # key -> 进行中的渲染事件
_inflight_results: dict[str, bytes | None] = {}     # key -> owner 渲染产物（成功字节/None=失败）
_inflight_lock = threading.Lock()
_INFLIGHT_WAIT_TIMEOUT = 90  # 等待方最长等待（s），超过则兜底自行判失败，不死等


def _single_flight_render(cache_key: str, do_render) -> BytesIO | None:
    """同 cache_key 的并发渲染合并为一次（single-flight）。

    Args:
        cache_key: 已剔除 fig_no 的缓存键（_strip_fig_no_for_cache 的产物）。
        do_render: 无参可调用，真正执行渲染并返回 BytesIO | None。仅 owner 会调用。

    Returns:
        owner：自己渲染的产物（可能为 None）。
        等待方：与 owner 相同的产物（成功为 PNG BytesIO，失败为 None）。
    """
    with _inflight_lock:
        ev = _inflight_renders.get(cache_key)
        if ev is None:
            ev = threading.Event()
            _inflight_renders[cache_key] = ev
            # owner 接管前清掉上一轮残留的结果，避免读到陈旧产物
            _inflight_results.pop(cache_key, None)
            owner = True
        else:
            owner = False

    if not owner:
        # 非 owner：等待 owner 渲染完成，最长 _INFLIGHT_WAIT_TIMEOUT
        ev.wait(timeout=_INFLIGHT_WAIT_TIMEOUT)
        data = _inflight_results.get(cache_key)
        return BytesIO(data) if data else None

    # owner：真正执行渲染
    data: bytes | None = None
    out: BytesIO | None = None
    try:
        out = do_render()
        data = out.getvalue() if out else None
        return out
    finally:
        with _inflight_lock:
            _inflight_results[cache_key] = data
            ev.set()
            # owner 注销；同 key 的后续新请求将成为新 owner（重新渲染）
            _inflight_renders.pop(cache_key, None)


# 标题编号前缀模式 —— V3.1 七级新标准
# 覆盖：第X节/第X章、一、1、1.1、1.1.1、1）、a 等
_HEADING_NUMBER_COMBINED = re.compile(
    r"^(?:第[一二三四五六七八九十百千万\d]+[章节][、，]?\s*"
    r"|\d+\.\d+\.\d+\s*[、．.]\s*"
    r"|\d+\.\d+\s*[、．.]\s*"
    r"|\d+\s*[、．.]\s*"
    r"|[一二三四五六七八九十百千]+\s*[、．.]\s*"
    r"|\d+\s*[)）]\s*[、．.]\s*"
    r"|[a-z]\s*[、．.]\s*"
    r"|[（(]\s*[一二三四五六七八九十百千\d]+\s*[)）]\s*)"
)


def _strip_heading_numbering(text: str) -> str:
    """去除标题文本开头已有的编号前缀，避免与自动编号重复

    V3.1 优化：使用合并后的单一正则（七级）
    """
    return _HEADING_NUMBER_COMBINED.sub("", text).strip()


def _format_outline_number(path_indices: list[int], level: int) -> str:
    """根据章节树路径和层级生成指定编号格式（V3.1 七级新标准 - 模板化实现）

    标书标题编号标准（V3.1 严格执行）：
    一级目录：第X节/第X章、  示例：第一章、施工组织设计
    二级目录：X、(中文数字)  示例：一、编制依据与适用标准
    三级目录：X、(阿拉伯数字) 示例：1、设计与施工规范依据
    四级目录：X.X、         示例：1.1、工程质量管控
    五级目录：X.X.X、       示例：1.1.1、项目专项文件与技术条件
    六级目录：X）、         示例：1）、标准化输出与归档管理
    七级目录：a、           示例：a、现场平面布置规划

    实现：使用基于 ID 路径的模板化编号系统（format_outline_number_by_template）。
    返回结果**不含**标点（调用方统一添加顿号"、"）。

    path_indices 约定（V3.1）：
      - L1 → [L1_idx]  → id="L1_idx"
      - L2 → [L2_idx]  → id="L2_idx"
      - L3 → [L3_idx]  → id="L3_idx"
      - L4 → [L3_idx, L4_idx]  → id="L3_idx.L4_idx"
      - L5 → [L3_idx, L4_idx, L5_idx]  → id="L3_idx.L4_idx.L5_idx"
      - L6 → [L6_idx]  → id="L6_idx"
      - L7 → [L7_idx]  → id="L7_idx"
    """
    # 将 path_indices 转换为 id_str
    if not path_indices:
        id_str = "1"  # 默认值
    else:
        id_str = ".".join(str(max(1, int(idx))) for idx in path_indices)

    # 使用模板化系统生成编号
    from .ai.heading_standard import format_outline_number_by_template, get_heading_template

    config = get_heading_template(level)
    template = config.get("numbering_template", "{num}")
    number = format_outline_number_by_template(id_str, template)
    return number


def _section_number_to_arabic(numbering: str) -> str:
    """把 section heading 的编号格式统一转阿拉伯字符串（用于 L4/L5 子标题前缀 / 图号前缀）

    输入示例："第一节" → 1，"二" → 2，"1" → 1，"第十一节" → 11
    支持:
      - L1 "第一节"/"第一章" → 提取中文数字或阿拉伯数字
      - L2 "一"/"二" → 中文数字转阿拉伯
      - L3 "1"/"2" → 直接返回
    """
    import re

    if not numbering:
        return ""

    # 1. 先尝试直接提取阿拉伯数字
    arabic_match = re.search(r"\d+", numbering)
    if arabic_match:
        return str(int(arabic_match.group()))

    # 2. 中文数字 → 阿拉伯（支持十以上）
    cn_map = {
        "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
        "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    }
    val = 0
    section_part = re.split(r"[章节]", numbering)[0]  # "第十一节" → "十一"
    if "十" in section_part:
        parts = section_part.split("十", 1)
        tens = cn_map.get(parts[0], 1) if parts[0] else 1  # "十" 前面没东西 → 1
        ones = cn_map.get(parts[1], 0) if len(parts) > 1 and parts[1] else 0
        val = tens * 10 + ones
    else:
        for ch in section_part:
            if ch in cn_map:
                val = val * 10 + cn_map[ch]
    return str(val)


# ====== 图表渲染样式辅助 ======

# BUG-E-06 修复：表格样式魔法数字已提取至 config.py，此处改为引用配置值
# 保持模块级变量以便下游函数直接引用（零修改下游代码）
_TABLE_HEADER_BG = EXPORT_TABLE_HEADER_BG
_TABLE_ALT_ROW_BG = EXPORT_TABLE_ALT_ROW_BG


def _set_cell_shading(cell, color: str):
    """设置单元格背景色（color 为不带 # 的 6 位十六进制）"""
    shading = parse_xml(f'<w:shd {nsdecls("w")} w:fill="{color}"/>')
    cell._tc.get_or_add_tcPr().append(shading)


def _theme_color(config: ExportConfig | None, field: str, default: str) -> str:
    """取主题色字段，为空/非法时回退到系统默认常量。

    ENHANCE 2026-09-14：主题预设（蓝/绿/橙…）通过这里注入到文档 XML。
    统一做一次规范化，避免前端传入 `#RRGGBB` 或 3 位简写时污染 w:shd/@w:fill
    （Word 对非法色值会整表渲染为黑底，是历史踩过的坑）。
    """
    raw = str(getattr(config, field, "") or "").strip().lstrip("#")
    if len(raw) == 3 and all(c in "0123456789abcdefABCDEF" for c in raw):
        raw = "".join(c * 2 for c in raw)
    if len(raw) == 6 and all(c in "0123456789abcdefABCDEF" for c in raw):
        return raw.upper()
    return default


def _heading_font_for(config: ExportConfig, level: int) -> str:
    """按层级取标题字体；heading_fonts 未提供或长度不足时回退 font_title。"""
    fonts = getattr(config, "heading_fonts", None) or []
    index = level - 1
    if 0 <= index < len(fonts) and str(fonts[index]).strip():
        return str(fonts[index]).strip()
    return config.font_title


def _heading_size_for(config: ExportConfig, level: int) -> float:
    """按层级取标题字号；heading_sizes 未提供或长度不足时回退 size_title_N。

    L1~L3 用 size_title_1/2/3，L4 复用 size_title_3，L5+ 用 size_body —— 与历史行为一致。
    """
    sizes = getattr(config, "heading_sizes", None) or []
    index = level - 1
    if 0 <= index < len(sizes):
        try:
            value = float(sizes[index])
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    if level == 1:
        return config.size_title_1
    if level == 2:
        return config.size_title_2
    if level in (3, 4):
        return config.size_title_3
    return config.size_body


def _body_indent_chars(config: ExportConfig) -> float:
    """正文首行缩进字符数（预设可调；非法值回退 2 字符）。"""
    try:
        value = float(getattr(config, "body_first_line_indent_chars", 2.0))
    except (TypeError, ValueError):
        return 2.0
    return value if value >= 0 else 2.0


def _apply_table_border_color(table, color: str):
    """覆盖 Table Grid 的边框色为指定主题色。

    ENHANCE 2026-09-14：Table Grid 内置样式的边框是黑色硬编码，
    只能通过显式 tblBorders 覆盖；不覆盖时主题色只会作用于表头底色，
    表格边框仍是黑的，视觉上不统一。
    """
    try:
        tbl_pr = table._tbl.tblPr
        existing = tbl_pr.find(qn("w:tblBorders"))
        if existing is not None:
            tbl_pr.remove(existing)
        border_xml = "".join(
            f'<w:{edge} w:val="single" w:sz="6" w:space="0" w:color="{color}"/>'
            for edge in ("top", "left", "bottom", "right", "insideH", "insideV")
        )
        tbl_pr.append(parse_xml(f'<w:tblBorders {nsdecls("w")}>{border_xml}</w:tblBorders>'))
    except Exception as e:
        logger.debug("表格边框主题色应用失败（非致命）: %s", e)


def _style_table_header(table, headers: list[str], config: ExportConfig):
    """统一设置表格表头：主题底色 + 主题文字色 + 居中"""
    row = table.rows[0]
    header_bg = _theme_color(config, "table_header_bg", _TABLE_HEADER_BG)
    header_fg = _theme_color(config, "table_header_fg", EXPORT_TABLE_HEADER_FG)
    for i, header in enumerate(headers):
        cell = row.cells[i]
        cell.text = ""
        p = cell.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _set_paragraph_indent_chars(p, first_line_chars=0)
        run = p.add_run(header)
        _apply_font(run, config.font_title, 10.5, bold=True)
        # BUG-E-06: 使用配置的表头文字颜色；ENHANCE 2026-09-14：改为主题色优先
        run.font.color.rgb = RGBColor.from_string(header_fg)
        _set_cell_shading(cell, header_bg)


def _apply_alternating_rows(
    table,
    start_row: int = 1,
    end_row: int | None = None,
    config: ExportConfig | None = None,
):
    """对表格数据行应用交替行背景色（ENHANCE 2026-09-14：支持主题斑马纹色）"""
    if end_row is None:
        end_row = len(table.rows)
    alt_bg = _theme_color(config, "table_alt_row_bg", _TABLE_ALT_ROW_BG)
    for i in range(start_row, end_row):
        if (i - start_row) % 2 == 1:
            for cell in table.rows[i].cells:
                _set_cell_shading(cell, alt_bg)


#: XML 1.0 不允许出现在文档里的字符。python-docx 底层是 lxml，写入这些字符会抛
#: `ValueError: All strings must be XML compatible`，进而整节标题/正文被占位符吞掉：
#:   - C0 控制符（\x0b 垂直制表符最常见，从 Word/网页粘贴标题时极易带入）
#:   - 孤立代理项 \ud800-\udfff（截断 emoji / 错误编解码的产物）
#:   - 非字符 \ufffe / \uffff
_XML_UNSAFE_PATTERN = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff\ufffe\uffff]"
)

#: Word 单元格与段落的单文本上限（Word 硬限制 32767，留 767 余量）
MAX_DOCX_TEXT_CHARS = 32000


def sanitize_for_docx(text, max_chars: int | None = None) -> str:
    """清洗将写入 DOCX 的任意文本（标题 / 正文 / 单元格 / 页眉页脚）。

    BUG-FIX（P1 静默丢内容）：此前只有正文做过控制字符清洗，章节标题直接把
    数据库里的 `node.title` 交给 `add_run`。标题里带一个 `\\x0b` 就会让
    `_add_section_heading` 抛 ValueError，被逐节点兜底 except 捕获后
    **标题完全不落盘**，正文变成「本小节内容渲染异常」占位符，目录也少一项；
    重试两轮必然重复失败，因为重试走的是同一条路径。
    """
    if not text:
        return ""
    cleaned = _XML_UNSAFE_PATTERN.sub("", text if isinstance(text, str) else str(text))
    if max_chars is not None and len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars] + "…"
    return cleaned


def _set_cell_text(
    cell,
    text: str,
    align: str = "center",
    bold: bool = False,
    font_name: str | None = None,
    font_size: float | None = None,
):
    """设置单元格文本并应用格式"""
    cell.text = ""
    p = cell.paragraphs[0]
    if align == "center":
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_paragraph_indent_chars(p, first_line_chars=0)
    # BUG-FIX：Word 单元格同样有 32767 字符上限，超长单元格会让 Word 打开时
    # 提示「文档需要修复」；同时补齐 XML 不安全字符清洗。
    run = p.add_run(sanitize_for_docx(text, MAX_DOCX_TEXT_CHARS))
    _apply_font(run, font_name or "宋体", font_size or 10.5, bold=bold)


def _set_paragraph_indent_chars(
    paragraph,
    first_line_chars: float = 0,
    left_chars: float = 0,
    hanging_chars: float = 0,
):
    """使用Word字符单位设置段落缩进（更专业，随字号自动调整）。
    1字符 = 100单位，所以2字符=200。
    """
    pPr = paragraph._p.get_or_add_pPr()
    ind = pPr.find(qn("w:ind"))
    if ind is None:
        ind = parse_xml(f'<w:ind {nsdecls("w")}></w:ind>')
        pPr.append(ind)
    if first_line_chars > 0:
        ind.set(qn("w:firstLineChars"), str(int(first_line_chars * 100)))
        ind.set(qn("w:firstLine"), "0")
    elif hanging_chars > 0:
        ind.set(qn("w:hangingChars"), str(int(hanging_chars * 100)))
        ind.set(qn("w:hanging"), "0")
    else:
        ind.set(qn("w:firstLineChars"), "0")
        ind.set(qn("w:firstLine"), "0")
    if left_chars > 0:
        ind.set(qn("w:leftChars"), str(int(left_chars * 100)))
        ind.set(qn("w:left"), "0")


# ---------------------------------------------------------------------------
# D2 修复（2026-10-07）：真正的 Word 列表编号（numPr）
# 原实现仅做悬挂缩进 + 原文（含 "• "/"1) " 符号），无 numPr，列表被拍平为纯文本，
# 编号无法重起、无法嵌套。以下助手在文档 numbering part 中确保「无序 / 有序」两组
# num 定义（幂等），导出端按列表类型写入 numPr。
# ---------------------------------------------------------------------------
_LIST_NUMID_CACHE_ATTR = "_bid_list_numids"


def _ensure_list_numbering(doc):
    """确保文档 numbering part 含「无序 / 有序」两个 num 定义，返回 {'bullet':numId,'ordered':numId}。

    幂等：同一 doc 对象仅创建一次（缓存在 doc 自定义属性上）。
    顺序合规：abstractNum 必须位于 num 之前，故 abstractNum 插在首个 num 之前，num 追加到末尾。
    """
    cached = getattr(doc, _LIST_NUMID_CACHE_ATTR, None)
    if cached:
        return cached

    np = doc.part.numbering_part
    root = np.element
    W = nsdecls("w")

    existing_abs = [int(a.get(qn("w:abstractNumId"), "0")) for a in root.findall(qn("w:abstractNum"))]
    existing_num = [int(n.get(qn("w:numId"), "0")) for n in root.findall(qn("w:num"))]
    next_abs = max(existing_abs + [0]) + 1
    next_num = max(existing_num + [0]) + 1

    bullet_abs = parse_xml(
        f'<w:abstractNum w:abstractNumId="{next_abs}" {W}>'
        '<w:multiLevelType w:val="hybridMultilevel"/>'
        '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/>'
        '<w:lvlText w:val="\u2022"/><w:lvlJc w:val="left"/>'
        '<w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr></w:lvl>'
        f'<w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="bullet"/>'
        '<w:lvlText w:val="\u25cb"/><w:lvlJc w:val="left"/>'
        '<w:pPr><w:ind w:left="1440" w:hanging="360"/></w:pPr></w:lvl>'
        '</w:abstractNum>'
    )
    decimal_abs = parse_xml(
        f'<w:abstractNum w:abstractNumId="{next_abs + 1}" {W}>'
        '<w:multiLevelType w:val="hybridMultilevel"/>'
        '<w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/>'
        '<w:lvlText w:val="%1."/><w:lvlJc w:val="left"/>'
        '<w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr></w:lvl>'
        f'<w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="decimal"/>'
        '<w:lvlText w:val="%1.%2."/><w:lvlJc w:val="left"/>'
        '<w:pPr><w:ind w:left="1440" w:hanging="360"/></w:pPr></w:lvl>'
        '</w:abstractNum>'
    )

    # abstractNum 必须插在首个 num 之前，保持 schema 顺序
    num_elems = root.findall(qn("w:num"))
    insert_idx = list(root).index(num_elems[0]) if num_elems else len(root)
    root.insert(insert_idx, bullet_abs)
    root.insert(insert_idx + 1, decimal_abs)

    bullet_num = parse_xml(f'<w:num w:numId="{next_num}" {W}><w:abstractNumId w:val="{next_abs}"/></w:num>')
    decimal_num = parse_xml(
        f'<w:num w:numId="{next_num + 1}" {W}><w:abstractNumId w:val="{next_abs + 1}"/></w:num>'
    )
    root.append(bullet_num)
    root.append(decimal_num)

    result = {"bullet": next_num, "ordered": next_num + 1}
    setattr(doc, _LIST_NUMID_CACHE_ATTR, result)
    return result


_LIST_MARKER_RE = re.compile(r"^\s*(?:[-•·▪■●○]|\d+[)）.、]|\（\d+\）|\(\d+\))\s*")


def _strip_list_marker(text: str) -> str:
    """去除列表文本开头残留的 "• "/"1) "/"1. "/"1、"/"- " 等手工符号，交由 Word 自动编号。"""
    return _LIST_MARKER_RE.sub("", text, count=1)


def _ensure_heading_styles(doc, body_font: str = "宋体", body_size_pt: float = 12.0):
    """确保 Heading 1~8 样式在文档中存在，防止 Word 回退为正文样式

    2026-09-04 修订：L5+ 目录标题按正文样式（不加粗、字号/字体对齐正文），
    但仍保留 Heading 样式名与大纲级别，确保 Word 自动目录/折叠可用。

    - 1~4 级标题：四号字（14pt）、加粗、宋体、不得倾斜
    - 5~8 级标题：正文字号/字体、**不加粗**、不得倾斜
    """
    for level in range(1, 9):
        config = HEADING_STYLE_CONFIG.get(level, HEADING_STYLE_CONFIG[3])
        is_body_style = config.get("body_style", False)  # L5+ 为 True
        style_name = f"Heading {level}"
        if is_body_style:
            font_size = body_size_pt
            font_name = body_font
            bold = False
        else:
            font_size = config.get("font_size", 14)
            font_name = config.get("font_name", "宋体")
            bold = True
        try:
            existing = doc.styles[style_name]
            existing.font.bold = bold
            existing.font.italic = False
            existing.font.size = Pt(font_size)
            existing.font.name = font_name
        except (KeyError, AttributeError):
            try:
                new_style = doc.styles.add_style(style_name, 1)
                new_style.base_style = doc.styles["Normal"]
                new_style.font.bold = bold
                new_style.font.italic = False
                new_style.font.size = Pt(font_size)
                new_style.font.name = font_name
            except Exception as e:
                logger.warning("创建 Heading 样式失败: %s", e)


def _apply_heading_style(doc, paragraph, level: int):
    """为段落应用Word内置标题样式（Heading 1~9），并设置显式大纲级别。

    2026-09-04 修订：L5+ 目录标题按正文样式（不加粗），但仍保留 Heading 样式名
    与大纲级别，确保 Word 自动目录/折叠可用。
    """
    if level < 1 or level > 9:
        return
    cfg = HEADING_STYLE_CONFIG.get(level, HEADING_STYLE_CONFIG[3])
    is_body_style = cfg.get("body_style", False)
    style_name = f"Heading {level}"
    try:
        paragraph.style = doc.styles[style_name]
    except (KeyError, AttributeError):
        try:
            new_style = doc.styles.add_style(style_name, 1)
            new_style.base_style = doc.styles["Normal"]
            new_style.font.bold = not is_body_style
            new_style.font.italic = False
            new_style.font.name = cfg.get("font_name", "宋体")
            paragraph.style = new_style
        except Exception as e:
            logger.warning("应用 Heading 样式失败: %s", e)
    outline_level = str(level - 1)
    pPr = paragraph._p.get_or_add_pPr()
    existing_outline = pPr.find(qn("w:outlineLvl"))
    if existing_outline is not None:
        pPr.remove(existing_outline)
    outlineLvl = parse_xml(f'<w:outlineLvl {nsdecls("w")} w:val="{outline_level}"/>')
    pPr.append(outlineLvl)


def check_document(
    project_id: int,
    strict_mode: bool = False,
) -> DocumentCheckResult:
    """文档完整性检查（V1.0 增强：支持宽松/严格模式）

    V1.0 增强（设计文档要求）：
    - 支持"宽松模式"与"严格模式"，用户可选择忽略警告继续导出
    - 返回结构化错误报告（如 {"章节":"1.2","问题":"缺少正文","行号":12}），
      便于前端精准定位
    - 严格模式下：所有 warning 升级为 error

    Args:
        project_id: 项目 ID
        strict_mode: 严格模式开关。True 时将所有 warning 升级为 error，
                     False 时 warning 仅作为提示，不影响 passed 状态

    Returns:
        DocumentCheckResult: 包含 passed、errors、warnings 等字段
    """
    sections = list_sections(project_id)

    # 空值防御：如果获取章节列表失败或返回空，直接返回错误结果
    if sections is None:
        logger.warning("check_document: list_sections 返回 None (project_id=%d)", project_id)
        sections = []
    elif not isinstance(sections, list):
        logger.warning(
            "check_document: list_sections 返回非列表类型 (project_id=%d, type=%s)",
            project_id,
            type(sections).__name__,
        )
        sections = []

    empty_sections = []
    short_sections = []
    empty_title_sections = []
    orphan_sections = []
    control_char_sections = []
    invalid_images = []
    uncovered_scoring_points = []
    warnings = []
    errors = []
    total_chars = 0
    _image_ref_total = 0

    # 预检增强（V1.0）：1. 结构校验——先收集全部节点ID，用于检测孤立节点
    section_ids = {s.id for s in sections}
    # BUG-FIX 2026-09-18（中）：区分「父章节」与「叶子章节」。
    # AI 生成链路只在叶子节点上写正文（`_build_leaf_dicts`），父章节 content
    # 恒为空 —— 原实现不区分，把它们一律计入 empty_sections，于是每次预检
    # 都必然报「有 N 个章节内容为空」（N = 父章节数量），一条用户永远消不掉、
    # 也定位不到真实问题的恒真误报。
    # 注：导出侧已放宽为「凡有正文即写出（叶子/非叶子一视同仁）」，用户在父级
    # 章节手写的正文同样会进文档。故「排除父章节」只用于**消除 AI 生成链路下的
    # 结构性误报**，而下方「全部为空」的致命判据必须与这一契约解耦（见该处注释）。
    parent_ids = {s.parent_id for s in sections if s.parent_id is not None}
    _any_content = any((s.content or "").strip() for s in sections)

    # Word 不兼容的控制字符（垂直制表符等，python-docx 写入时会损坏文档）
    # BUG-FIX：与写入侧统一为 sanitize_for_docx 使用的字符集（补上代理项/非字符）
    _CONTROL_CHAR_PATTERN = _XML_UNSAFE_PATTERN

    # V5.0 优化：total_chars 改用单次 SQL 聚合，避免在 Python 循环中累加 len(content)
    # 性能提升：O(n) Python 累加 → O(1) SQL SUM，导出检查耗时从 O(n*content_size) 降到 O(content_size)
    try:
        with get_db() as _conn:
            _row = _conn.execute(
                "SELECT COALESCE(SUM(LENGTH(content)), 0) AS total_chars, "
                "COUNT(*) AS total_sections "
                "FROM sections WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            total_chars = int(_row["total_chars"] or 0)
    except Exception as _e:
        logger.warning("check_document: SQL 聚合 total_chars 失败（回退 Python 累加）: %s", _e)
        # 失败回退：保持原行为（Python 循环累加）
        total_chars = sum(len(s.content or "") for s in sections)

    for s in sections:
        title = (s.title or "").strip()
        content = s.content or ""

        # 1.1 空标题检查
        if not title:
            empty_title_sections.append(
                EmptySection(id=s.id, title="", level=s.level),
            )

        # 1.2 孤立节点检查（父节点不存在或不属于本项目）
        if s.parent_id is not None and s.parent_id not in section_ids:
            orphan_sections.append(
                EmptySection(id=s.id, title=title, level=s.level),
            )

        # 2. 内容完整性：空内容 / 过短内容（<50字警告）
        # 父章节跳过（见上方 parent_ids 说明）：它没有正文是设计如此，不是缺陷。
        if s.id in parent_ids:
            pass
        elif not content.strip():
            empty_sections.append(
                EmptySection(id=s.id, title=title, level=s.level),
            )
        elif len(content.strip()) < 50:
            short_sections.append(
                EmptySection(id=s.id, title=title, level=s.level),
            )

        # 4. 特殊字符过滤：扫描 Word 不兼容控制字符，提前标记以便导出前替换
        # BUG-FIX：此前只扫 content 不扫 title，标题带 \x0b 时预检报不出来，
        # 导出时才炸（标题整条丢失 + 正文变占位符）。
        if _CONTROL_CHAR_PATTERN.search(content) or _CONTROL_CHAR_PATTERN.search(title):
            control_char_sections.append(
                EmptySection(id=s.id, title=title, level=s.level),
            )

        # 3. 资源路径可达性：检查内容中引用的图片（本地路径 / 远程URL）
        _img_refs = _extract_content_image_refs(content)
        _image_ref_total += len(_img_refs)
        for ref in _img_refs:
            if not _image_ref_available(ref, project_id):
                invalid_images.append(ref)

    # 5. 预估页数与内存评估：按字数 + 图片密度粗估（A4/12pt/1.5倍行距约 900 字/页）
    _image_count = _image_ref_total
    estimated_pages = total_chars // 900 + _image_count // 3 + 1
    if estimated_pages > 800:
        warnings.append(f"预估页数约 {estimated_pages} 页，超过 800 页建议分卷导出")

    with get_db() as conn:
        row = conn.execute(
            "SELECT scoring_points_json FROM analysis WHERE project_id = ?",
            (project_id,),
        ).fetchone()

    scoring_ids = set()
    if row is None:
        # 没有分析数据，跳过评分点检查
        pass
    else:
        try:
            points = json.loads(row["scoring_points_json"])
            # BUG-FIX 2026-09-18（中）：剔除 `__weight_warning__` 系统提示项。
            # 它是 extract_scoring_points 在权重之和 ≠100 时插入的校验提醒，
            # 不是评分项、也不可能有对应章节。原实现把它算进 scoring_ids，
            # 于是预检恒报「有 1 个评分点未被目录覆盖」，用户无论如何补章节
            # 都消不掉这条告警（伪阳性）。判据统一走 app.utils.scoring_points。
            clean_points = strip_scoring_placeholders(points)
            for p in clean_points if isinstance(clean_points, list) else []:
                if isinstance(p, dict):
                    scoring_ids.add(p.get("id", ""))
        except (json.JSONDecodeError, TypeError):
            errors.append("评分点数据解析失败")

    covered_ids = {s.scoring_point_id for s in sections if s.scoring_point_id}
    for sid in scoring_ids:
        if sid and sid not in covered_ids:
            uncovered_scoring_points.append(sid)

    if empty_sections:
        warnings.append(f"有 {len(empty_sections)} 个章节内容为空")
        # 当所有章节都为空时，视为严重错误而非仅警告。
        # 判据必须与「正文挂在哪类节点」解耦：契约已从「只挂叶子」放宽为
        # 「有正文即写出（叶子/非叶子一视同仁）」，若用叶子数做分母，
        # 会出现「正文都在父章节上、叶子全空 → 误判为全部为空」的假致命错误。
        # 直接看「是否还有任何章节有正文」最稳。
        if sections and not _any_content:
            errors.append("所有章节内容均为空，请先生成章节内容")

    if short_sections:
        warnings.append(f"有 {len(short_sections)} 个章节字数少于50，导出后内容可能过于单薄")

    if empty_title_sections:
        errors.append(f"有 {len(empty_title_sections)} 个章节标题为空，请先补充标题")

    if orphan_sections:
        warnings.append(f"有 {len(orphan_sections)} 个孤立章节（父节点缺失）")

    if control_char_sections:
        warnings.append(
            f"有 {len(control_char_sections)} 个章节含 Word 不兼容控制字符，导出时将自动清理"
        )

    if invalid_images:
        warnings.append(f"有 {len(invalid_images)} 个图片引用无法访问，导出后将显示占位符")

    if uncovered_scoring_points:
        warnings.append(f"有 {len(uncovered_scoring_points)} 个评分点未被目录覆盖")

    if total_chars == 0:
        errors.append("文档内容为空")

    # V1.0 增强：严格模式 — 所有 warning 升级为 error
    if strict_mode and warnings:
        for w in warnings:
            errors.append(f"[严格模式] {w}")
        warnings = []

    passed = len(errors) == 0

    return DocumentCheckResult(
        passed=passed,
        total_sections=len(sections),
        empty_sections=empty_sections,
        uncovered_scoring_points=uncovered_scoring_points,
        warnings=warnings,
        errors=errors,
        total_chars=total_chars,
        short_sections=short_sections,
        empty_title_sections=empty_title_sections,
        orphan_sections=orphan_sections,
        control_char_sections=control_char_sections,
        invalid_images=invalid_images,
        estimated_pages=estimated_pages,
    )


def _extract_content_image_refs(content: str) -> list[str]:
    """从章节内容中提取图片引用（支持 Markdown 图片语法与纯 URL 行）。

    Args:
        content: 章节正文内容
    Returns:
        list[str]: 图片引用列表（本地路径或 URL）
    """
    if not content:
        return []
    refs: list[str] = []
    # 富文本 HTML 图片: <img src="...">（RichTextEditor 插入，含 data URL / URL / 本地路径）
    for m in re.finditer(r"<img[^>]*\bsrc\s*=\s*[\"']([^\"']+)[\"']", content, re.IGNORECASE):
        ref = m.group(1).strip()
        if ref:
            refs.append(ref)
    # Markdown 图片语法: ![alt](url)
    for m in re.finditer(r"!\[[^\]]*\]\(([^)]+)\)", content):
        ref = m.group(1).strip().strip('"').strip("'")
        if ref:
            refs.append(ref)
    # 纯图片扩展名 URL / 本地路径行（常见于 AI 生成内容的独立图片行）
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(
            r"^(?:https?://\S+|(?:[A-Za-z]:)?[\\/][^\s]+)\.(?:png|jpe?g|gif|webp|bmp)(?:[?#]\S*)?$",
            line,
            re.IGNORECASE,
        )
        if m:
            refs.append(line)
    return refs


def _image_ref_available(ref: str, project_id: int) -> bool:
    """判断图片引用是否可访问（本地文件存在 或 远程 URL 结构合法）。

    说明：远程 URL 不做真实网络请求（避免预检耗时），仅校验格式；
    本地路径检查文件是否存在，并兼容项目 images 目录。
    """
    if not ref:
        return False
    if ref.startswith(("http://", "https://", "data:")):
        return True
    try:
        local_path = Path(ref)
        if local_path.exists():
            return True
        alt_path = PROJECTS_DATA_DIR / str(project_id) / "images" / local_path.name
        return alt_path.exists()
    except Exception:
        return False


def _apply_font(run, font_name: str, font_size: float, bold: bool = False):
    run.font.size = Pt(font_size)
    run.font.bold = bold
    run.font.italic = False
    run.font.name = font_name
    r = run._element
    rPr = r.find(qn("w:rPr"))
    if rPr is None:
        rPr = parse_xml(f'<w:rPr {nsdecls("w")}></w:rPr>')
        r.insert(0, rPr)
    rFonts = rPr.find(qn("w:rFonts"))
    if rFonts is None:
        rFonts = parse_xml(f'<w:rFonts {nsdecls("w")}></w:rFonts>')
        rPr.insert(0, rFonts)
    rFonts.set(qn("w:eastAsia"), font_name)
    rFonts.set(qn("w:ascii"), font_name)
    rFonts.set(qn("w:hAnsi"), font_name)


def _add_paragraph_with_format(
    doc,
    text: str,
    config: ExportConfig,
    block_type: str,
    level: int = 0,
):
    # BUG-FIX（P1）：所有写进 DOCX 的文本统一清洗，避免单个非法字符炸掉整段
    # BUG-FIX 2026-10-04（P1 超长内容）：与表格单元格同口径截断到 Word 单段
    # 上限（32767）之内。此前段落只清洗不截断，AI 超长单段（>3.2 万字）直接
    # 落盘 → Word 打开成品提示「文档需要修复」，整本标书被标记损坏。
    text = sanitize_for_docx(text, MAX_DOCX_TEXT_CHARS)
    p = doc.add_paragraph()

    pf = p.paragraph_format
    pf.line_spacing = config.line_spacing

    if block_type.startswith("heading"):
        # === L1~L4：标题样式 ===
        if level == 1:
            size = config.size_title_1
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            pf.space_before = Pt(24)
            pf.space_after = Pt(12)
        elif level == 2:
            size = config.size_title_2
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            pf.space_before = Pt(18)
            pf.space_after = Pt(6)
        elif level == 3:
            size = config.size_title_3
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            pf.space_before = Pt(12)
            pf.space_after = Pt(6)
        elif level == 4:
            size = config.size_title_3
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            pf.space_before = Pt(10)
            pf.space_after = Pt(4)
        # === L5+：正文样式（2026-09-04 修订） ===
        else:  # level >= 5
            size = config.size_body
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            pf.space_before = Pt(0)
            pf.space_after = Pt(0)

        _apply_heading_style(doc, p, level)
        # ENHANCE 2026-09-14：一级标题另起一页（与 _add_section_heading 保持一致）
        if level == 1 and getattr(config, "heading_level1_page_break", False):
            pf.page_break_before = True
        if level >= 5:
            # L5+：正文首行缩进、不加粗
            _set_paragraph_indent_chars(p, first_line_chars=_body_indent_chars(config))
            run = p.add_run(text)
            _apply_font(run, config.font_body, config.size_body, bold=False)
        else:
            _set_paragraph_indent_chars(p, first_line_chars=0)
            run = p.add_run(text)
            # ENHANCE 2026-09-14：L1~L4 支持逐级字体/字号
            _apply_font(run, _heading_font_for(config, level), _heading_size_for(config, level), bold=True)
        run.font.color.rgb = RGBColor.from_string(
            _theme_color(config, "heading_color", "000000")
        )
        run.font.italic = False

    elif block_type in ("list_item", "list_bullet", "list_ordered"):
        p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        pf.space_before = Pt(0)
        pf.space_after = Pt(0)
        _set_paragraph_indent_chars(p, left_chars=2, hanging_chars=2)
        # D2 修复：写入真正的 Word 编号（numPr），避免列表被拍平为纯文本
        try:
            _num_ids = _ensure_list_numbering(doc)
            _num_id = _num_ids["ordered"] if block_type == "list_ordered" else _num_ids["bullet"]
            _pPr = p._p.get_or_add_pPr()
            _existing = _pPr.find(qn("w:numPr"))
            if _existing is not None:
                _pPr.remove(_existing)
            _np = parse_xml(
                '<w:numPr %s><w:ilvl w:val="0"/><w:numId w:val="%d"/></w:numPr>' % (nsdecls("w"), _num_id)
            )
            _pPr.append(_np)
            # 去除文本里残留的 "• "/"1) " 等手工符号，交由 Word 自动编号
            text = _strip_list_marker(text)
        except Exception as _e:
            # 容错：编号写入失败也不影响正文（维持原缩进 + 文本行为）
            logger.debug("[列表编号] numPr 写入失败，回退纯文本: %s", _e)
        run = p.add_run(text)
        _apply_font(run, config.font_body, config.size_body)

    else:
        p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        pf.space_before = Pt(0)
        pf.space_after = Pt(0)
        # ENHANCE 2026-09-14：正文首行缩进改为可配置（预设可调）
        _set_paragraph_indent_chars(p, first_line_chars=_body_indent_chars(config))
        run = p.add_run(text)
        _apply_font(run, config.font_body, config.size_body)

    return p


def _add_table_from_markup(doc, markup: str, config: ExportConfig):
    cleaned = (
        markup.replace("<<表格：", "")
        .replace("<<表格:", "")
        .replace("<<表格", "")
        .replace(">>", "")
        .strip()
    )
    lines = [l.strip() for l in cleaned.split("\n") if l.strip()]
    if len(lines) < 1:
        return

    lines = [l.replace("｜", "|") for l in lines]

    for marker in ["<<表格：", "<<表格:", "<<表格"]:
        lines = [l.replace(marker, "").strip() for l in lines]

    cleaned_lines = []
    for l in lines:
        if re.match(r"^[\|\-:\s]+$", l) and "|" in l:
            continue
        cleaned_lines.append(l)
    lines = cleaned_lines
    if not lines:
        return

    first_line = lines[0]
    has_separate_title = "|" not in first_line

    if has_separate_title:
        table_title = first_line
        data_start = 1
    else:
        table_title = None
        data_start = 0

    if data_start >= len(lines):
        return

    # BUG-E-01 修复：保留空单元格，不再丢弃
    # 旧代码 [h.strip() for h in ... if h.strip()] 会丢弃空单元格，导致列数与数据不对齐
    # 新代码：先按 "|" 分割（保留空字符串），去除首尾由 "|" 产生的空单元格，再统计列数
    def _split_row(line: str) -> list[str]:
        """按 | 分割行，去除首尾由分隔符产生的空单元格

        规则：如果行以 | 开头和结尾，则 split 产生的首尾空字符串是分隔符的副作用，
        应移除；中间的空字符串（如 |  | 列B |  |）是真正的空单元格，保留。
        """
        cells = [h.strip() for h in line.split("|")]
        # 如果行以 | 开头，去除第一个空字符串
        if line.startswith("|") and cells and not cells[0]:
            cells.pop(0)
        # 如果行以 | 结尾，去除最后一个空字符串
        if line.rstrip().endswith("|") and cells and not cells[-1]:
            cells.pop()
        return cells

    raw_headers = _split_row(lines[data_start])
    if not raw_headers:
        return
    # 统计有效列数：以 header 和所有 data row 中最大列数为准
    all_rows_cells = [raw_headers]
    for dl in lines[data_start + 1 :]:
        if dl.strip() and "|" in dl:
            row_cells = _split_row(dl)
            if row_cells:
                all_rows_cells.append(row_cells)
    col_count = max((len(r) for r in all_rows_cells), default=0)
    if col_count < 1:
        return
    # 用有效 header 数创建表头
    display_headers = raw_headers[:col_count]
    while len(display_headers) < col_count:
        display_headers.append("")
    # 从有效 headers 中提取非空表头
    non_empty_headers = [h for h in display_headers if h]
    if not non_empty_headers:
        return

    if table_title:
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        _set_paragraph_indent_chars(p, first_line_chars=0)
        run = p.add_run(table_title)
        _apply_font(run, config.font_title, 10.5, bold=True)
        pPr = p._element.get_or_add_pPr()
        pPr.append(parse_xml(f'<w:spacing {nsdecls("w")} w:after="40"/>'))

    data_lines = lines[data_start + 1 :]
    data_lines = [l for l in data_lines if l.strip() and "|" in l]
    if not data_lines:
        return

    # BUG-FIX（P2 防拖垮导出）：AI 输出一个畸形 `<<表格：` 标记后跟上千行含 `|`
    # 的正文时，会创建 N 万个单元格，每个都走 cell.text + add_run + _apply_font
    # （含 parse_xml）→ 导出卡死数分钟、docx 体积暴涨。给行列数加上限。
    _MAX_TABLE_ROWS = 500
    _MAX_TABLE_COLS = 50
    if col_count > _MAX_TABLE_COLS:
        logger.warning(
            "表格列数 %d 超过上限 %d，已截断", col_count, _MAX_TABLE_COLS
        )
        col_count = _MAX_TABLE_COLS
    if len(data_lines) + 1 > _MAX_TABLE_ROWS:
        logger.warning(
            "表格行数 %d 超过上限 %d，已截断", len(data_lines) + 1, _MAX_TABLE_ROWS
        )
        data_lines = data_lines[: _MAX_TABLE_ROWS - 1]

    # BUG-FIX：col_count 可能被 _MAX_TABLE_COLS 截断到 50，但 display_headers 仍按原始
    # 列数占位。必须在建表前将 display_headers 同步截断到 col_count，否则下方
    # _style_table_header 按 display_headers 长度遍历会越界访问 row.cells[i]（i>=cols）
    # 抛 IndexError，导致整张超宽表格（及所在章节）导出崩溃。
    if len(display_headers) > col_count:
        display_headers = display_headers[:col_count]

    # BUG-E-01 修复：以 col_count 为准（考虑空列），不再依赖 headers 长度
    row_count = 1 + len(data_lines)
    table = doc.add_table(rows=row_count, cols=col_count)
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    # ENHANCE 2026-09-14：表格边框色跟随主题（默认黑色，保持历史外观）
    _apply_table_border_color(table, _theme_color(config, "table_border_color", "000000"))

    # BUG-E-01 修复：使用 display_headers（含空列）渲染表头
    _style_table_header(table, display_headers, config)

    for row_idx, line in enumerate(data_lines, 1):
        if row_idx >= len(table.rows):
            break
        # BUG-E-01 修复：使用 _split_row 解析数据行，去除首尾空单元格
        cells = _split_row(line)
        row = table.rows[row_idx]
        for col_idx in range(col_count):
            if col_idx < len(row.cells):
                cell = row.cells[col_idx]
                # BUG-E-01 修复：安全索引，当 cells 列数少于 col_count 时填空字符串
                cell_text = cells[col_idx] if col_idx < len(cells) else ""
                _set_cell_text(cell, cell_text, "center")

    _apply_alternating_rows(table, config=config)
    # BUG-E-01 增强日志：记录表格解析结果，便于排查列数不对齐等问题
    logger.debug(
        "表格解析完成: 表头列数=%d, 数据行数=%d, 最大列数=%d",
        len(display_headers),
        len(data_lines),
        col_count,
    )


# ========== 修复2: 图片下载超时和本地缓存 ==========
_IMAGE_DOWNLOAD_TIMEOUT = 30  # 图片下载超时时间（秒）


def _download_and_cache_image(url: str, project_id: int) -> Path | None:
    """下载远程图片到本地缓存，避免导出时链接失效

    将图片保存到 data/projects/{project_id}/images/ 目录，
    下载超时 30 秒，失败时返回 None 而不是抛出异常。

    Args:
        url: 图片远程 URL
        project_id: 项目 ID

    Returns:
        Path | None: 本地缓存文件路径，下载失败时返回 None
    """
    if not url or not url.startswith(("http://", "https://")):
        return None

    # 确定缓存目录
    cache_dir = PROJECTS_DATA_DIR / str(project_id) / "images"
    cache_dir.mkdir(parents=True, exist_ok=True)

    # 用 URL 的哈希值作为文件名，避免特殊字符问题
    url_hash = hashlib.md5(url.encode("utf-8")).hexdigest()[:16]
    ext = _guess_image_ext(url)
    cache_path = cache_dir / f"{url_hash}{ext}"

    # 如果已缓存，直接返回
    if cache_path.exists():
        return cache_path

    # BUG-FIX 2026-09-15（高，SSRF）：拒绝拉取内网/保留地址，防止服务端请求伪造
    from app.utils.ssrf import is_safe_http_url
    if not is_safe_http_url(url):
        logger.warning("拒绝下载非安全图片 URL（疑似内网地址，存在 SSRF 风险）: %s", url)
        return None

    try:
        with httpx.Client(timeout=_IMAGE_DOWNLOAD_TIMEOUT) as client:
            response = client.get(url)
            response.raise_for_status()
            cache_path.write_bytes(response.content)
            logger.info("图片下载成功并缓存: %s -> %s", url, cache_path)
            return cache_path
    except httpx.TimeoutException:
        logger.warning("图片下载超时 (%ds): %s", _IMAGE_DOWNLOAD_TIMEOUT, url)
    except httpx.HTTPStatusError as e:
        logger.warning("图片下载 HTTP 错误 (%s): %s", e.response.status_code, url)
    except httpx.RequestError as e:
        logger.warning("图片下载请求失败: %s - %s", url, e)
    except OSError as e:
        logger.warning("图片缓存写入失败: %s - %s", url, e)
    return None


def _guess_image_ext(url: str) -> str:
    """根据 URL 猜测图片扩展名，默认 .png"""
    url_lower = url.lower().split("?")[0]
    for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"):
        if url_lower.endswith(ext):
            return ext
    return ".png"


# BUG C 修复：data URL 图片（前端 RichTextEditor 插入的 base64 图片）
# 支持 raster 格式（png/jpeg/gif/webp/bmp）；image/svg+xml 因 python-docx 不支持且
# 环境未安装 SVG 栅格化库，降级为占位符并在日志中提示（与前端 svgStringToPngDataUrl 降级链路对齐）。
_DATA_URL_PATTERN = re.compile(
    r"^data:(image/(?:png|jpe?g|gif|webp|bmp));base64,(.+)$",
    re.IGNORECASE | re.DOTALL,
)


def _add_image_placeholder(
    doc, config: ExportConfig, caption: str = "", reason: str = "图片未加载"
):
    """插入灰色占位文本 + 可选图题（图片下载失败 / 数据无效 / 格式不支持时使用）"""
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    _set_paragraph_indent_chars(p, first_line_chars=0)
    run = p.add_run(f"[{reason}]")
    _apply_font(run, config.font_body, config.size_body)
    run.font.color.rgb = RGBColor(180, 180, 180)
    run.font.italic = True
    if caption:
        cap_p = doc.add_paragraph()
        cap_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        cap_p.paragraph_format.space_before = Pt(2)
        _set_paragraph_indent_chars(cap_p, first_line_chars=0)
        # BUG-FIX 2026-10-04（P1 特殊字符）：caption 来自章节 *_json 图表标题，
        # 未经正文链路清洗；含 XML 不安全字符时 add_run 直接抛 ValueError，
        # 「降级占位符」这条最后防线也会炸，整张图静默消失。
        cap_run = cap_p.add_run(sanitize_for_docx(caption))
        _apply_font(cap_run, config.font_body, config.size_body - 1, bold=True)
        cap_run.font.italic = True


def _add_data_url_image(doc, url: str, config: ExportConfig, caption: str = ""):
    """解码 data:image/...;base64 图片并插入文档（BUG C 修复）。

    Args:
        doc: Document 对象
        url: data URL（如 data:image/png;base64,iVBOR...）
        config: 导出配置
        caption: 图片标题
    """
    import base64

    m = _DATA_URL_PATTERN.match(url)
    if not m:
        # 仅记录前 80 字符避免刷屏日志
        logger.warning("不支持的 data URL 图片格式，已降级为占位符: %.80s...", url)
        _add_image_placeholder(doc, config, caption, reason="图片格式不支持")
        return
    try:
        img_bytes = base64.b64decode(m.group(2))
        if not img_bytes:
            raise ValueError("空图片数据")
        # BUG-FIX 2026-09-18（P1）：插入失败（如不支持的编码）时补占位符，
        # 不再静默空白
        if not _add_diagram_picture(doc, BytesIO(img_bytes), config, caption=caption):
            _add_image_placeholder(doc, config, caption, reason="图片格式无法编码")
    except Exception as e:
        logger.warning("data URL 图片解码失败，已降级为占位符: %s", e)
        _add_image_placeholder(doc, config, caption, reason="图片数据无效")


def _add_image_block(doc, url: str, project_id: int, config: ExportConfig, caption: str = ""):
    """在文档中插入图片（支持远程 URL / 本地路径 / data URL）

    Args:
        doc: Document 对象
        url: 图片 URL、本地路径或 data:image/...;base64 URL
        project_id: 项目 ID
        config: 导出配置
        caption: 图片标题
    """
    image_path = None
    # Data URL（前端富文本编辑器插入的 base64 图片，BUG C 修复）
    if url.startswith("data:"):
        _add_data_url_image(doc, url, config, caption)
        return
    # 远程 URL
    if url.startswith(("http://", "https://")):
        image_path = _download_and_cache_image(url, project_id)
    # 本地路径
    else:
        local_path = Path(url)
        if local_path.exists():
            image_path = local_path
        else:
            # 尝试在 images 目录下查找
            alt_path = PROJECTS_DATA_DIR / str(project_id) / "images" / local_path.name
            if alt_path.exists():
                image_path = alt_path

    if image_path and image_path.exists():
        try:
            image_bytes = image_path.read_bytes()
            # BUG-FIX 2026-09-18（P1）：_add_diagram_picture 内部失败不再抛异常，
            # 必须按返回值判定；失败时落到下方占位符分支，不留空白
            if _add_diagram_picture(doc, BytesIO(image_bytes), config, caption=caption):
                return
            logger.warning("本地图片插入失败（格式不支持等），已降级为占位符: %s", image_path)
        except Exception as e:
            logger.warning("读取本地图片失败: %s - %s", image_path, e)

    # 下载失败或图片不存在，插入占位文本
    _add_image_placeholder(doc, config, caption)


def _add_parsed_content(
    doc,
    content: str,
    config: ExportConfig,
    base_level: int = 0,
    section_detail=None,
    project_id: int | None = None,
    _http_service_available: bool | None = None,
    parent_section_number: str = "",
):
    # 预检增强（V1.0）：写入前清理 Word 不兼容控制字符（垂直制表符等），
    # 与 check_document 的 control_char_sections 检查呼应，防止 python-docx 写入损坏文档
    # 统一走 sanitize_for_docx：除 C0 控制符外，还覆盖孤立代理项与 \ufffe/\uffff
    if content:
        content = sanitize_for_docx(content)
    logger.info(
        "[DIAG-EXPORT] _add_parsed_content: content_len=%d section_detail=%s",
        len(content) if content else 0,
        # P2：调用方可能传入非 Section 轻对象（如只有部分属性的代理），
        # 直取 .id 会 AttributeError；getattr 防护。
        getattr(section_detail, "id", None),
    )
    blocks = parse_content(content, base_level=base_level)
    logger.info("[DIAG-EXPORT] parse_content done, blocks_count=%d", len(blocks) if blocks else 0)
    heading_counters: dict[int, int] = {}
    last_heading_level = 0
    # D5 加固：记录本章节是否出现过图表标记，用于"标题兜底补图"判断
    _has_chart_marker = False

    for block in blocks:
        if block.type == "table":
            _add_table_from_markup(doc, block.text, config)
        elif block.type == "chart_marker":
            try:
                marker = json.loads(block.text)
                _has_chart_marker = True
                chart_type = marker.get("chart_type", "")
                chart_desc = marker.get("description", "")
                if section_detail:
                    _add_inline_chart(
                        doc,
                        chart_type,
                        chart_desc,
                        section_detail,
                        config,
                        _http_service_available=_http_service_available,
                        parent_section_number=parent_section_number,
                    )
            except Exception as e:
                logger.warning("解析图表标记失败: %s", e)
                # BUG-FIX 2026-09-18（P2）：原仅记日志，成品文档中该图表位置
                # 完全空白，读者/评审专家无法判断是遗漏还是损坏。插入事实性
                # 说明文字（非伪造数据，不违反无降级原则）。
                try:
                    _bad_para = doc.add_paragraph()
                    _bad_run = _bad_para.add_run("（此处图表标记数据损坏，渲染已跳过，请核对章节图表数据后重新导出）")
                    _apply_font(_bad_run, config.font_body, config.size_body)
                    _bad_run.font.color.rgb = RGBColor(0x99, 0x99, 0x99)
                    _bad_run.italic = True
                except Exception:
                    logger.warning("插入图表损坏说明也失败，该位置将空白")
        elif block.type == "image":
            _add_image_block(doc, block.text, project_id or 0, config)
        elif block.type.startswith("heading"):
            level = block.level
            rel_level = block.relative_level if block.relative_level > 0 else level
            # 上下文级联修正：AI 可能将 L6/L7 标题误输出为 L3 格式（如 "5、" 而非 "1）"）
            # 仅当标题文本较短（≤20字符）触发，且验证标题文本不含多级编号（避免误改 L4/L5）
            if last_heading_level >= 5 and level == 3 and len(block.text) <= 20:
                # 检查标题文本是否包含点号分隔的编号（如 "1.1"），若有则为 L4+ 不应修正
                if not re.match(r"^\d+\.\d+", block.text):
                    level = 6
                    rel_level = 6

            # [V3.1] 强制树深度校准：仅对 L1/L2/L3/L6/L7 等无小数点编号的标题生效
            # L4+（1.1 / 1.1.1）有自洽的小数点层级语义，跳过校准，保持原始层级
            # 这样所有 1.1 格式标题统一为 Heading 4，不会因父章节层级不同而混乱
            if base_level > 0 and rel_level < 4:
                expected_child_level = base_level + 1
                # 规则 1：同级重复 → 跳过
                if rel_level == base_level:
                    continue
                # 规则 2：层级过浅（低于 base_level）→ 修正
                elif rel_level < base_level:
                    logger.debug(
                        "Level correction (too shallow): AI generated L%d format in L%d context, "
                        "correcting to L%d (expected child)",
                        rel_level,
                        base_level,
                        expected_child_level,
                    )
                    level = expected_child_level
                    rel_level = expected_child_level
                # 规则 4：层级过深（超出 2 级以上）→ 修正
                elif rel_level > expected_child_level + 1:
                    logger.debug(
                        "Level correction (too deep): AI generated L%d format in L%d context, "
                        "correcting to L%d (expected child)",
                        rel_level,
                        base_level,
                        expected_child_level,
                    )
                    level = expected_child_level
                    rel_level = expected_child_level
                # 规则 3：层级合理（expected_child_level 或 expected_child_level + 1）→ 保持不变

            # 当 base_level > 0 时，跳过与 base_level 相同相对层级的标题
            # 因为树节点已通过 _add_section_heading 添加了该层级的标题，避免重复
            # 但是！L3（被跳过时）代表一个新父节点，必须：
            #   1. 更新 heading_counters[base_level]（父级编号递增）
            #   2. 重置所有子级计数器（避免兄弟父节点的子节点编号连续递增）
            if base_level > 0 and rel_level == base_level:
                # 即使跳过，也要让父级计数器递增 + 重置子级
                # 这样 L4 在 "2、父节点" 下面就会变成 "2.1、" 而不是 "1.1、"
                heading_counters = {k: v for k, v in heading_counters.items() if k <= base_level}
                heading_counters[base_level] = heading_counters.get(base_level, 0) + 1
                last_heading_level = base_level
                continue
            # 重置低于当前层级的所有计数器（保持层次结构）
            heading_counters = {k: v for k, v in heading_counters.items() if k <= rel_level}
            heading_counters[rel_level] = heading_counters.get(rel_level, 0) + 1
            counter = heading_counters[rel_level]
            last_heading_level = rel_level

            # 受控层级（2026-09-04）：仅 L1~L4 强制编号；L5~L7 保留原文，不重格式化
            # 但所有层级仍应用 Heading 样式与大纲级别（level 传给 _add_paragraph_with_format）
            if rel_level >= 5:
                # L5+：保留原文（AI 生成的格式），不强制清除也不强制重新编号
                # BUG-FIX 2026-10-04（BUG-E）：block.text 在 parse 层已剥离编号前缀，
                # 旧实现直接渲染导致 1）/a、等 L5+ 编号在导出文档中丢失。
                # 改用 raw_line（原始整行）；空值（旧构造点/外部块）退回 text，
                # 行为与修复前一致，向后兼容。
                display_text = getattr(block, "raw_line", "") or block.text
                _add_paragraph_with_format(doc, display_text, config, block.type, level)
                continue

            # V3.1 七级新标准：构建编号路径（仅 L1~L4 进入此分支）
            # L1→[1], L2→[1], L3→[1], L4→[1,1]
            # 核心修复：L4 的父段优先使用 parent_section_number
            # 这样 section "2、总体实施策划" 下的 L4 就是 "2.1、" 而不是 "1.1、"
            clean_text = _strip_heading_numbering(block.text)
            if rel_level == 4:
                # 四级：2.1、→ [parent_section, L4_counter]
                if parent_section_number:
                    numbering = _format_outline_number([parent_section_number, counter], rel_level)
                else:
                    parent = heading_counters.get(3, 1)
                    numbering = _format_outline_number([parent, counter], rel_level)
            else:
                # L1/L2/L3 使用独立编号
                numbering = _format_outline_number([counter], rel_level)
            display_text = f"{numbering}、{clean_text}"
            # 关键修复：传校准后的 level，而不是原始 block.level
            # 否则树深度校准逻辑（L1→L4, L2→L4 等）白做，Heading 样式全部错位
            _add_paragraph_with_format(doc, display_text, config, block.type, level)
        elif block.type in ("list_item", "list_bullet", "list_ordered"):
            _add_paragraph_with_format(doc, block.text, config, block.type)
        else:
            _add_paragraph_with_format(doc, block.text, config, "paragraph")

    # D5 加固（2026-10-07）：章节标题命中图表关键词且全文无任何图表标记时，
    # 自动补一张本地标题模板图，避免技术标书"有章节无图"。仅命中强关键词才触发，
    # 默认关闭（auto_fallback_charts=False）；若渲染器不可用仍按既有逻辑跳过（不伪造数据）。
    if _afc_enabled(config) and not _has_chart_marker and section_detail is not None:
        _sec_title = getattr(section_detail, "title", "") or ""
        if _sec_title and any(
            kw in _sec_title
            for kws in _CHART_FALLBACK_KEYWORDS.values()
            for kw in kws
        ):
            _inferred = _infer_chart_type_from_text(_sec_title)
            if _inferred:
                try:
                    _add_inline_chart(
                        doc,
                        _inferred,
                        _sec_title,
                        section_detail,
                        config,
                        _http_service_available=_http_service_available,
                        parent_section_number=parent_section_number,
                    )
                except Exception as _e:
                    logger.warning("[D5加固] 标题兜底图表插入失败（已忽略）: %s", _e)


def _add_inline_chart(
    doc,
    chart_type: str,
    chart_desc: str,
    section_detail,
    config: ExportConfig,
    preload_only: bool = False,
    _http_service_available: bool | None = None,
    parent_section_number: str = "",
):
    """根据图表类型和章节数据，在当前位置内联渲染图表（商业级 v2 渲染器）

    支持全部 7 类图表：gantt / flowchart / architecture / labor / comparison / layout / timeline
    优先级：
      1. 内联 JSON 数据（chart_desc 解析结果）→ 使用 v2 商业级渲染器
      2. 章节绑定数据（section_detail.*_json）→ 使用 v2 商业级渲染器
      3. 无数据源时跳过图表占位符（无降级原则：绝不伪造数据）

    BUG-E-02 修复：新增 _http_service_available 参数
    - True：HTTP Service 可用，可走 Level 1.5 渲染路径
    - False：HTTP Service 不可用，直接跳过重试等待，走 PIL v2
    - None：自动检测（每次调用会触发 health check，性能差）

    线程安全说明（preload_only=True）：
      并行预渲染（_preload_chart_images_parallel）从多线程调用本函数。
      python-docx Document（lxml）非线程安全，预加载模式只做缓存预热、
      不向 doc 写入任何内容，因此可安全并发调用。
    """
    inline_data = None
    if chart_desc and chart_desc.strip().startswith("{"):
        try:
            parsed = json.loads(chart_desc)
            if isinstance(parsed, dict) and "type" in parsed:
                inline_data = parsed
                chart_type = parsed.get("type", chart_type).lower()
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass

    chart_type = (chart_type or "").lower().strip()

    # 统一图表类型处理（无降级模式）
    # 所有图表类型统一调用 _render_commercial：真实数据经渲染缓存 →
    # render_mermaid_to_bytes 双路径渲染；全部失败时跳过该图表（不伪造数据）。
    _section_title = getattr(section_detail, "title", "")

    # P1 修复 2026-09-29：图号计数器按项目分桶，避免两个项目并发导出互串
    _project_id = getattr(section_detail, "project_id", None)

    # BUG-E-02 修复：将 HTTP 服务可用性传递到渲染器
    # 当 HTTP 服务不可用时，直接跳过 Level 1.5 路径，节省重试超时
    _http_available = _http_service_available

    def _render_commercial(typ: str, payload, caption: str) -> bool:
        """商业级 v2 渲染器 — 无降级模式（V6.1）

        渲染顺序：
        1. L1 内存缓存（同一导出会话内去重）
        2. V6.0 内容指纹持久化缓存
        3. render_mermaid_to_bytes（Level 1.5 HTTP Service 2.5x → Level 2 PIL v2 2.5x超采样+300DPI）

        Returns:
            True 表示渲染成功；False 表示真实数据渲染失败，跳过该图表
        """

        def _emit(stream, cap: str):
            # 预加载模式：仅触发渲染并填充缓存，不写入 Document（线程安全）
            if not preload_only:
                # BUG-FIX 2026-09-18（P1）：渲染成功但插入失败（异常编码等）
                # 时补灰色占位说明，避免成品静默空白
                if not _add_diagram_picture(doc, stream, config, caption=cap):
                    _add_image_placeholder(doc, config, cap, reason="图表图片插入失败")

        payload_str = (
            json.dumps(payload, ensure_ascii=False) if not isinstance(payload, str) else payload
        )
        # BUG-FIX 2026-09-14（高）：缓存键必须剔除 fig_no。
        # fig_no 是导出会话内的**递增序号**，预渲染阶段与正式渲染阶段拿到的号不同
        # （预渲染先跑一轮，正式阶段计数器接着往上走），导致两阶段的 payload_str
        # 必然不同 → L1 内存缓存与磁盘持久化缓存命中率恒为 0，预渲染 100% 白做，
        # 且每次导出都往磁盘缓存塞 N 份内容相同、编号不同的 PNG。
        # 修复：缓存查找/写入统一使用剔除 fig_no 后的序列化结果。
        cache_payload_str = _strip_fig_no_for_cache(payload)
        last_error = None

        # V1.2 策略 0: L1 内存缓存（避免同一次导出中重复 render_mermaid_to_bytes）
        cached = _get_cached_chart_render(cache_payload_str, typ)
        if cached:
            cached_stream, _ = cached
            _emit(cached_stream, caption)
            logger.debug("商业级渲染: L1缓存命中 (%s)", typ)
            return True

        # 策略 1: V6.0 持久化缓存
        try:
            cached_path = _chart_cache.get(cache_payload_str, chart_type=typ)
            if cached_path:
                cached_bytes = cached_path.read_bytes()
                _emit(BytesIO(cached_bytes), caption)
                _store_chart_render(cache_payload_str, BytesIO(cached_bytes), chart_type=typ)
                logger.debug("商业级渲染: 策略1缓存命中 (%s)", typ)
                return True
        except Exception as e:
            last_error = e

        # 策略 2: 商业级 v2 渲染器
        # BUG-E-03 修复：移除每图 ThreadPoolExecutor 开销（创建线程池~10ms/次）
        # 改用进程级共享线程池 _get_render_executor() 复用线程，
        # 单图节省 ~10ms，对于 150 张图可节省 ~1.5s
        #
        # BUG-EXPORT-06 修复：预加载模式下绕过渲染池，直接调用 render_mermaid_to_bytes。
        # 预加载池（8线程）与渲染池（4线程）嵌套时，8个预加载线程竞争4个渲染槽位：
        #   - 渲染池满时，任务在队列中排队，_render_future.result(timeout=25) 从"提交时"
        #     开始计时，而非"渲染真正开始时"，导致任务还未开始渲染就已超时
        #   - 超时后 cancel() 对已在运行的任务无效，渲染线程变成"孤儿线程"
        #   - 多轮后渲染池被阻塞，导出进度卡在 chart_preload 25%
        # 预加载本身已在线程池中，直接调用消除嵌套竞争。
        try:
            logger.info(
                "[商业级渲染] 策略2: 调用 render_mermaid_to_bytes chart_type=%s 数据长度=%d",
                typ,
                len(payload_str),
            )
            _skip_http = _http_available is not True

            def _do_render():
                """真正产生渲染字节流（仅 single-flight owner 会执行）。"""
                if preload_only:
                    # BUG-EXPORT-06 修复：预加载模式直接调用，不走渲染池
                    return render_mermaid_to_bytes(
                        payload_str,
                        typ,
                        skip_http=_skip_http,
                    )
                # BUG-F-02 修复：从45s降至25s，与内部 HTTP 总预算匹配。
                # BUG-FIX 2026-09-18（P1）：25s 低于单图合法最坏路径（HTTP ≤20s +
                # PIL v2 复杂甘特 ≈15s ≈ 35s）：超时后 future.cancel() 对已在跑的
                # worker 无效，4 线程共享池逐个被“孤儿渲染”占满，后半文档图表
                # 级联判超时丢失。现内部已去固定 sleep/非 PNG 提前失败，上界≈35s，
                # 超时上调到 40s 覆盖合法最坏耗时，消除“未开始渲染先超时”饥饿。
                _render_timeout = 40
                _render_future = _get_render_executor().submit(
                    render_mermaid_to_bytes,
                    payload_str,
                    typ,
                    skip_http=_skip_http,
                )
                try:
                    return _render_future.result(timeout=_render_timeout)
                except TimeoutError:
                    logger.warning(
                        "[商业级渲染] 策略2超时(%ds): render_mermaid_to_bytes chart_type=%s",
                        _render_timeout,
                        typ,
                    )
                    _render_future.cancel()
                    return None

            # SINGLE-FLIGHT 2026-10-06：同 cache_key 的并发渲染合并为一次。
            # 预渲染线程池（≤8）与正文渲染可能同时请求同一份图表（已剔除 fig_no），
            # 旧实现 L1/L2 miss 后各自调 render → 同图被重复渲染 N 次（缓存击穿）。
            # 首个线程（owner）真正渲染，其余线程等待并共享产物，杜绝重复计算。
            stream = _single_flight_render(cache_payload_str, _do_render)

            if stream:
                stream.seek(0)
                _render_bytes = stream.getvalue()
                _chart_cache.set(cache_payload_str, _render_bytes, chart_type=typ)
                _store_chart_render(cache_payload_str, BytesIO(_render_bytes), chart_type=typ)
                _emit(stream, caption)
                logger.info(
                    "[商业级渲染] 策略2成功: %s (原始数据, %d bytes)", typ, len(_render_bytes)
                )
                return True
            else:
                logger.warning(
                    "[商业级渲染] 策略2失败: render_mermaid_to_bytes 返回 None chart_type=%s", typ
                )
        except Exception as e:
            logger.warning("[商业级渲染] 策略2异常: %s (%s)", typ, e)
            last_error = e

        # V6.1 无降级渲染终止点：
        #   真实数据经 Level 1.5 (HTTP Service 2.5x) → Level 2 (PIL v2 2.5x超采样+300DPI)
        #   双路径均失败时，跳过该图表（不插入任何伪造数据/占位图）。
        #   原因：投标文件中的图表必须基于真实项目数据，伪造图表数据（劳动力人数、
        #   进度节点、成本占比等）会造成严重的商业与合规风险，宁缺毋假。
        #   已移除的策略（V6.0 及之前）：
        #     - 策略3 _repair_chart_data：渲染失败时按标题关键词伪造节点/任务/人数
        #     - 策略4 generate_context_aware_chart_data：伪造整套"智能默认"图表数据
        #     - 策略5 _render_text_fallback_image：插入无信息量的文本占位图
        logger.error(
            "[商业级渲染] 真实数据渲染失败，跳过该图表（无降级原则）: type=%s section=%s error=%s",
            typ,
            _section_title,
            last_error,
        )
        # BUG-FIX-03 修复：渲染失败时插入文字说明（非伪造数据），避免文档出现空白区域。
        # 原实现直接 return False，文档中该图表位置完全空白，读者无法判断是遗漏还是渲染失败。
        # 此处插入的是"渲染失败说明"（事实陈述），不是伪造的图表数据，符合无降级原则。
        # ENHANCE 2026-10-04（需求 9）：受 CHART_FAIL_PLACEHOLDER 开关控制。
        # 默认 false：静默跳过（不占图号、不留失败说明）；true：保留可见的排错提示。
        if not preload_only and CHART_FAIL_PLACEHOLDER:
            try:
                from docx.shared import Pt as _Pt

                _fail_para = doc.add_paragraph()
                # P1 修复 2026-09-29：渲染失败后图号会被回滚复用，失败说明
                # 不能再携带该编号，否则正文会出现两个同号（一个假图一个真图）
                _fail_label = _FIG_NO_PREFIX_RE.sub("", caption or "").strip()
                # BUG-FIX 2026-10-04（P1 特殊字符）：caption 源于图表 JSON 标题，
                # 含 XML 不安全字符时本兜底 add_run 会再炸一次，导致连「渲染失败
                # 说明」都写不进去，文档该位置完全空白。
                _fail_run = _fail_para.add_run(
                    sanitize_for_docx(f"【图表渲染失败】{_fail_label or typ}（请检查原始数据或重新导出）")
                )
                _fail_run.font.size = _Pt(9)
                _fail_run.font.color.rgb = RGBColor(0x99, 0x99, 0x99)
                _fail_run.italic = True
            except Exception:
                logger.warning("[商业级渲染] 插入渲染失败说明文字也失败，文档该位置将空白")
        return False

    # 统一图表类型处理（无降级模式）
    _CHART_TYPE_CONFIG = [
        (ctype, default_title, json_field)
        for ctype, (json_field, default_title) in _CHART_TYPE_MAP.items()
    ]

    for ctype, default_title, json_field in _CHART_TYPE_CONFIG:
        if chart_type == ctype:
            source = (
                inline_data
                if inline_data
                else (getattr(section_detail, json_field, None) if section_detail else None)
            )
            # === V7.0 标题提取增强 ===
            # 优先从 source JSON 中提取 AI 提供的专业标题（比 default_title 更精准）
            # 回退链：source.title → chart_desc(若非 JSON) → default_title
            source_obj = None
            if source:
                if isinstance(source, str):
                    try:
                        source_obj = json.loads(source)
                    except (json.JSONDecodeError, TypeError):
                        source_obj = None
                elif isinstance(source, dict):
                    source_obj = source

            # 提取标题（ENHANCE 2026-10-04 四级优先级，需求 8）：
            #   载荷 title/caption > Mermaid 代码内 title 行 > 引导语 chart_desc > 类型通用名
            _mermaid_title = ""
            if isinstance(source_obj, dict):
                _code_field = source_obj.get("code")
                if isinstance(_code_field, str) and _code_field.strip() and not _code_field.lstrip().startswith("{"):
                    # 仅纯 Mermaid 语法载荷提 title；JSON 内嵌 code 不交叉提取
                    _mermaid_title = extract_mermaid_title(_code_field)
            if source_obj and isinstance(source_obj, dict):
                title_text = (
                    source_obj.get("title")
                    or source_obj.get("caption")
                    or _mermaid_title
                    or (
                        chart_desc
                        if isinstance(chart_desc, str) and not chart_desc.startswith("{")
                        else default_title
                    )
                )
            else:
                title_text = (
                    chart_desc
                    if isinstance(chart_desc, str) and not chart_desc.startswith("{")
                    else default_title
                )
            # 标题清理：去除 AI 可能残留的 emoji/Markdown，限制长度
            if title_text:
                title_text = str(title_text).strip()[:24]

            # D5 加固（2026-10-07）：无数据源但开启兜底时，先用标题本地模板（结构骨架）
            # 尝试渲染，替代原本的"静默跳过"。仅结构骨架、不伪造数字，契合"无降级"原则。
            if source_obj is None and _afc_enabled(config):
                _local = _generate_local_mermaid_chart(ctype, _section_title or default_title, "")
                if _local and _local.get("code"):
                    logger.info(
                        "[D5加固] 章节无 %s 图表数据，改用本地标题模板兜底渲染: section=%s",
                        ctype, _section_title,
                    )
                    source = _local
                    source_obj = _local

            if source_obj is not None:
                # 确保 source 是 dict 格式供渲染器使用
                if isinstance(source, str):
                    source = source_obj
                # V7.0 注入图号 + 构建标书级图题
                # BUG-FIX 2026-09-18（低）：预渲染（preload_only）阶段不得消耗图号计数器。
                # 图号计数器是**导出会话级**的（build_docx 开头 _reset_figure_counter），
                # 预渲染先跑一轮会把号吃掉，正文阶段从 N+1 接着数 → 首图不是「图X-1」。
                # 目前预渲染未传 parent_section_number（都落在 "default" 桶）所以尚未串号，
                # 但这只是巧合：一旦给预渲染补上章节号就会立刻串位，故显式短路。
                # 图号不进缓存键（见 _strip_fig_no_for_cache），预热拿不到号不影响命中。
                fig_no = (
                    ""
                    if preload_only
                    else _next_figure_number(parent_section_number, _project_id)
                )
                if isinstance(source, dict):
                    source["fig_no"] = fig_no
                final_caption = (
                    f"{fig_no} {title_text}" if (fig_no and title_text) else (title_text or fig_no)
                )
                logger.info("[图表导出] %s: %s", fig_no, title_text or ctype)
                # 商业级 v2 渲染器（真实数据，无降级）
                # P2：透传渲染结果，上层（预渲染计数/失败重试）才能感知成败
                # P1 修复 2026-09-29：渲染失败时回滚图号，消除正文图号虚跳
                _rendered = _render_commercial(ctype, source, final_caption)
                if fig_no and not _rendered:
                    _rewind_figure_number(parent_section_number, _project_id)
                return _rendered
            # V6.1 无数据源：跳过图表（无降级原则）
            #   正文出现图表占位但章节无绑定图表数据时，不得伪造图表内容
            #   （伪造的劳动力/进度/成本数据在投标文件中存在商业与合规风险）
            logger.warning(
                "[商业级渲染] 章节无 %s 图表数据，跳过占位符（无降级原则）: section=%s",
                ctype,
                _section_title,
            )
            return

    # chart 占位符（自动从内部 type 识别）
    # BUG-E-04 修复：防止 inner_type == "chart" 导致无限递归
    if chart_type == "chart" and inline_data:
        inner_type = (inline_data.get("type") or "").lower()
        if inner_type and inner_type != "chart":
            # BUG 修复：递归调用必须透传 _http_service_available，
            # 否则默认 None → 每次都触发健康检查/跳过 Level 1.5 高质量路径
            return _add_inline_chart(
                doc,
                inner_type,
                json.dumps(inline_data, ensure_ascii=False),
                section_detail,
                config,
                preload_only=preload_only,
                _http_service_available=_http_service_available,
                parent_section_number=parent_section_number,
            )

    if inline_data:
        # 尝试商业级渲染（按 inner_type）
        inner_type = (inline_data.get("type") or "").lower()
        if inner_type in {
            "gantt",
            "flowchart",
            "architecture",
            "labor",
            "comparison",
            "layout",
            "timeline",
        }:
            # BUG 修复：同上，递归调用透传 _http_service_available
            return _add_inline_chart(
                doc,
                inner_type,
                json.dumps(inline_data, ensure_ascii=False),
                section_detail,
                config,
                preload_only=preload_only,
                _http_service_available=_http_service_available,
                parent_section_number=parent_section_number,
            )
        # V7.0 未识别类型也注入图号（同上：预渲染阶段不消耗会话级图号计数器）
        fig_no = (
            ""
            if preload_only
            else _next_figure_number(parent_section_number, _project_id)
        )
        if isinstance(inline_data, dict):
            inline_data["fig_no"] = fig_no
        fb_caption = (
            f"{fig_no} {chart_desc}" if (fig_no and chart_desc) else (chart_desc or fig_no)
        )
        # P2：透传渲染结果（原裸 return 永远 None，调用方无法感知失败）
        # P1 修复 2026-09-29：渲染失败回滚图号
        _rendered_fb = _render_commercial(inner_type or "chart", inline_data, fb_caption)
        if fig_no and not _rendered_fb:
            _rewind_figure_number(parent_section_number, _project_id)
        return _rendered_fb

    # V6.1 无数据源且无法识别图表类型：跳过（无降级原则，不输出文本占位）
    logger.warning(
        "[商业级渲染] 无法识别的图表占位符，跳过（无降级原则）: type=%s section=%s",
        chart_type,
        _section_title,
    )


def _add_diagram_picture(doc, image_stream, config: ExportConfig, caption: str = "", wide=False):
    """在文档中插入图片，自动缩放到页面可用宽度（商业级：图 + 标题）

    增强：使用 PIL 获取原始分辨率，计算目标 DPI（不低于 config.image_dpi），
    确保插入图片在打印时达到高分辨率质量。
    增强 v2：图片质量验证，确保插入的图片有效。
    """
    if not image_stream:
        return False
    try:
        # ---- 增强：WD_ALIGN_PARAGRAPH 回退 ----
        align_center = getattr(WD_ALIGN_PARAGRAPH, "CENTER", None) if WD_ALIGN_PARAGRAPH else None

        p = doc.add_paragraph()
        if align_center is not None:
            p.alignment = align_center
        _set_paragraph_indent_chars(p, first_line_chars=0)
        section = doc.sections[-1]
        available_cm = max(
            8.0,
            (float(section.page_width) - float(section.left_margin) - float(section.right_margin))
            / 360000,
        )
        configured_max = float(config.image_max_width_cm)
        if wide:
            width_cm = min(available_cm, max(configured_max, 24.0))
        else:
            width_cm = min(available_cm, configured_max)

        # ---- 增强：DPI 感知的高分辨率图片插入 ----
        # 优先读取图片内嵌 DPI，再回退到配置值
        # 增强 V4.0：@_render_hq 装饰器已在 PNG 中嵌入 300 DPI 元数据，
        # 此处读取该值确保 DOCX 精确缩放到物理尺寸

        # Save original stream position
        original_pos = image_stream.tell()
        image_stream.seek(0)
        stream_bytes = image_stream.read()

        # 使用 PIL 获取图片原始像素尺寸和内嵌 DPI
        pixel_width = 0
        pixel_height = 0
        embedded_dpi = 0
        try:
            from PIL import Image as PilImage

            # BUG-E-06 修复：使用 with 语句确保 PIL Image 资源被正确释放
            _src_fmt = ""
            with PilImage.open(BytesIO(stream_bytes)) as img_temp:
                pixel_width, pixel_height = img_temp.size
                _src_fmt = (img_temp.format or "").upper()
                # 读取图片内嵌 DPI（由 @_render_hq 装饰器写入）
                img_dpi = img_temp.info.get("dpi")
                if img_dpi and len(img_dpi) >= 2 and img_dpi[0] > 0:
                    embedded_dpi = int(round(img_dpi[0]))
            # BUG-FIX 2026-09-18（P1）：python-docx 只认 BMP/GIF/JPEG/PNG/TIFF
            # 等原生格式，富文本编辑器/图片服务产出的 WEBP、CMYK-TIFF 等
            # 会在 add_picture 处抛 UnrecognizedImageError 被吞→整图静默丢失。
            # 非原生格式统一用 PIL 转码为 PNG 后再插入。
            if _src_fmt and _src_fmt not in ("PNG", "JPEG", "JPG", "GIF", "BMP", "TIFF"):
                with PilImage.open(BytesIO(stream_bytes)) as img_conv:
                    _to_save = img_conv
                    if img_conv.mode not in ("1", "L", "RGB", "RGBA", "P"):
                        _to_save = img_conv.convert("RGB")
                    _png_buf = BytesIO()
                    _to_save.save(_png_buf, format="PNG")
                stream_bytes = _png_buf.getvalue()
                logger.info(
                    "[图表导出] 图片格式 %s 非 docx 原生支持，已转码为 PNG (%d bytes)",
                    _src_fmt,
                    len(stream_bytes),
                )
            logger.info(
                "[图表导出] 图片原始尺寸: %dx%dpx, 内嵌DPI=%d, 配置DPI=%d, 图表类型=%s",
                pixel_width,
                pixel_height,
                embedded_dpi,
                getattr(config, "image_dpi", 300),
                caption,
            )
        except Exception:
            logger.debug("_add_diagram_picture: 无法获取图片原始尺寸，使用默认宽度")

        # 目标 DPI：内嵌 DPI > 配置值 > 300
        target_dpi = embedded_dpi or max(300, getattr(config, "image_dpi", 300))

        # 根据 DPI 计算物理宽度（英寸 → 厘米）
        # 增强：如果 DPI 计算宽度 < 可用宽度的 50%，使用可用宽度（允许放大）
        # 这确保低分辨率图片也能在文档中适当放大显示
        if pixel_width > 0 and target_dpi > 0:
            dpi_based_width_cm = (pixel_width / target_dpi) * 2.54
            # 如果 DPI 计算宽度 < 可用宽度的 50%，使用可用宽度（允许放大以匹配文档布局）
            if dpi_based_width_cm < available_cm * 0.5:
                logger.debug(
                    "_add_diagram_picture: DPI 计算宽度 %.1fcm < 可用宽度 50%% (%.1fcm)，使用可用宽度 %.1fcm",
                    dpi_based_width_cm,
                    available_cm * 0.5,
                    available_cm,
                )
                # BUG-E-08 修复：根据 wide 标志正确选择宽度上限
                max_width = max(configured_max, 24.0) if wide else configured_max
                width_cm = min(available_cm, max_width)
            else:
                # 取 DPI 计算宽度和页面可用宽度的较小值
                width_cm = min(width_cm, dpi_based_width_cm)
            logger.debug(
                "_add_diagram_picture: DPI 计算 - 像素=%d, DPI=%d, 物理宽度=%.2fcm, 最终宽度=%.2fcm",
                pixel_width,
                target_dpi,
                dpi_based_width_cm,
                width_cm,
            )

        run = p.add_run()

        # Try method 1: Insert from BytesIO stream
        try:
            stream_copy = BytesIO(stream_bytes)
            run.add_picture(stream_copy, width=Cm(width_cm))
        except Exception:
            # Method 1 failed - try file-based approach (fallback for some platform issues)
            import tempfile

            # BUG-FIX 2026-09-16（低）：tmp_path 原在 with 块内才赋值，若
            # tmp.write 抛异常（磁盘满等），finally 里引用未定义变量抛
            # UnboundLocalError，被外层 except 吞掉后段落清理被跳过。
            tmp_path = None
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp.write(stream_bytes)
                tmp_path = tmp.name
            try:
                # BUG-FIX：python-docx 的 Paragraph 没有 clear() 方法（只有 _Cell 有
                # _clear_content()），原兜底分支必然抛 AttributeError → 落到外层
                # except 把整个段落删掉，图片静默丢失。改为手工移除既有 run。
                for _old_run in list(p.runs):
                    _el = _old_run._element
                    if _el.getparent() is not None:
                        _el.getparent().remove(_el)
                run = p.add_run()
                run.add_picture(tmp_path, width=Cm(width_cm))
            except Exception:
                # 两种方法都失败，移除空段落
                if p._element.getparent() is not None:
                    p._element.getparent().remove(p._element)
                logger.warning("插入图片失败（BytesIO 和文件方法均失败），已移除空段落")
                # BUG-FIX 2026-09-18（P1）：向调用方明确报告失败，由调用方补占位符
                return False
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    os.unlink(tmp_path)

        # ---- 增强：图片质量验证 ----
        try:
            image_stream.seek(0)
            verify_bytes = image_stream.read()
            if len(verify_bytes) < 100:
                logger.warning(
                    "_add_diagram_picture: 插入的图片数据过小 (%d bytes)，可能无效",
                    len(verify_bytes),
                )
            # 尝试用 PIL 验证图片有效性
            if Image is not None:
                from PIL import Image as PilImage

                # BUG-E-06 修复：使用 with 语句释放 PIL 资源
                with PilImage.open(BytesIO(verify_bytes)) as verify_img:
                    verify_img.verify()
            else:
                # BUG-E-04 修复：PIL不可用时记录信息日志，告知图片验证被跳过
                logger.info("_add_diagram_picture: PIL 不可用，跳过图片有效性验证")
        except Exception as ve:
            logger.warning("_add_diagram_picture: 图片质量验证警告: %s", ve)

        # Restore original stream position
        image_stream.seek(original_pos)

        if caption:
            cap_p = doc.add_paragraph()
            if align_center is not None:
                cap_p.alignment = align_center
            # V7.0 标书级图题格式：caption前留 12pt（与上图间隔），后留 18pt（与下文间隔）
            cap_p.paragraph_format.space_before = Pt(12)
            cap_p.paragraph_format.space_after = Pt(18)
            _set_paragraph_indent_chars(cap_p, first_line_chars=0)
            # BUG-FIX 2026-10-04（P1 特殊字符）：图题同样源于章节 *_json 的标题字段，
            # 未走正文清洗链路；add_run 炸掉会让「渲染成功的好图」整个按失败处理。
            cap_run = cap_p.add_run(sanitize_for_docx(caption, MAX_DOCX_TEXT_CHARS))
            # 图题字体：黑体/加粗/小五号（标书标准）
            _apply_font(cap_run, config.font_body, config.size_body - 1, bold=True)
        return True
    except Exception as e:
        logger.warning("插入图表图片失败: %s", e)
        # BUG-FIX 2026-09-18（P1）：原失败仅 warning 后“正常返回”，调用方无法
        # 感知，成品文档该位置完全空白无占位提示。改为返回 False 交由调用方降级。
        return False


def _apply_heading_border(paragraph, color: str):
    """给标题段落加下框装饰线（近似「章框标题」效果）。

    ENHANCE 2026-09-14：移植自易标 `heading_border` 配置。易标用表格模拟章框，
    这里改用段落边框（pBdr），对目录域/大纲级别零干扰，且不引入表格节点，
    避免影响 Word 导航与自动目录。
    """
    pPr = paragraph._p.get_or_add_pPr()
    existing = pPr.find(qn("w:pBdr"))
    if existing is not None:
        pPr.remove(existing)
    border = parse_xml(
        f'<w:pBdr {nsdecls("w")}>'
        f'<w:bottom w:val="single" w:sz="12" w:space="4" w:color="{color}"/>'
        f"</w:pBdr>"
    )
    pPr.append(border)


def _add_section_heading(doc, text: str, level: int, config: ExportConfig, numbering: str = ""):
    p = doc.add_paragraph()
    pf = p.paragraph_format

    # === L1~L4：标题样式 ===
    if level == 1:
        size = config.size_title_1
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        pf.space_before = Pt(24)
        pf.space_after = Pt(12)
    elif level == 2:
        size = config.size_title_2
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT
        pf.space_before = Pt(18)
        pf.space_after = Pt(6)
    elif level == 3:
        size = config.size_title_3
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT
        pf.space_before = Pt(12)
        pf.space_after = Pt(6)
    elif level == 4:
        size = config.size_title_3
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT
        pf.space_before = Pt(10)
        pf.space_after = Pt(4)
    # === L5+：正文样式（2026-09-04 修订） ===
    else:  # level >= 5
        size = config.size_body
        p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        pf.space_before = Pt(0)
        pf.space_after = Pt(0)

    pf.line_spacing = config.line_spacing
    # ENHANCE 2026-09-14：一级标题另起一页（Word 原生 pageBreakBefore，
    # 对文档首段会被 Word 自动忽略，不会产生空白首页）
    if level == 1 and getattr(config, "heading_level1_page_break", False):
        pf.page_break_before = True
    _apply_heading_style(doc, p, level)

    # BUG-FIX（P1）：标题此前未做 XML 安全清洗，一个 \x0b 就让整节标题消失
    display_text = f"{numbering}{_strip_heading_numbering(text)}" if numbering else text
    run = p.add_run(sanitize_for_docx(display_text))
    if level >= 5:
        # L5+：正文首行缩进、不加粗、字号字体同正文（保持 2026-09-04 既定行为）
        _set_paragraph_indent_chars(p, first_line_chars=_body_indent_chars(config))
        _apply_font(run, config.font_body, config.size_body, bold=False)
    else:
        _set_paragraph_indent_chars(p, first_line_chars=0)
        # ENHANCE 2026-09-14：L1~L4 支持逐级字体/字号（预设可给 L4 用楷体）
        _apply_font(
            run,
            _heading_font_for(config, level),
            _heading_size_for(config, level),
            bold=True,
        )
    run.font.color.rgb = RGBColor.from_string(
        _theme_color(config, "heading_color", "000000")
    )
    run.font.italic = False

    # ENHANCE 2026-09-14：一级标题章框装饰线（主题色）
    if level == 1 and getattr(config, "heading_border_enabled", False):
        try:
            _apply_heading_border(
                p, _theme_color(config, "heading_border_color", "000000")
            )
        except Exception as e:
            logger.debug("标题章框装饰线应用失败（非致命）: %s", e)


def _add_toc_field(doc, config: ExportConfig):
    """在文档中插入 Word 自动目录域（TOC \\o "1-4" \\h \\z \\u），
    仅包含 L1~L4 目录标题；L5+ 不出现在目录中但仍保留 Heading 样式名和大纲级别
    （Word 导航面板/折叠照常工作）。标记 w:dirty 确保 Word 打开时提示更新目录页码。"""
    p = doc.add_paragraph()

    run1 = p.add_run()
    # w:dirty="true" 确保 Word 打开时提示更新域
    fld_begin = parse_xml(f'<w:fldChar {nsdecls("w")} w:fldCharType="begin" w:dirty="true"/>')
    run1._element.append(fld_begin)

    run2 = p.add_run()
    instr = parse_xml(
        f'<w:instrText {nsdecls("w")} xml:space="preserve"> TOC \\o "1-4" \\h \\z \\u </w:instrText>',
    )
    run2._element.append(instr)

    run3 = p.add_run()
    fld_separate = parse_xml(f'<w:fldChar {nsdecls("w")} w:fldCharType="separate"/>')
    run3._element.append(fld_separate)

    run4 = p.add_run("请右键此处选择「更新域」以生成目录")
    _apply_font(run4, config.font_body, config.size_body)

    run5 = p.add_run()
    fld_end = parse_xml(f'<w:fldChar {nsdecls("w")} w:fldCharType="end"/>')
    run5._element.append(fld_end)


# ========== 修复3: 导出前校验 ==========


def _validate_document_structure(doc, project_id: int, output_path: str) -> list[str]:
    """导出前校验文档结构

    检查项：
    1. 是否有空章节（无内容的段落）
    2. 图片链接是否有效（本地文件是否存在）
    3. 文档保存后校验文件大小 > 0

    Args:
        doc: Document 对象
        project_id: 项目 ID
        output_path: 输出文件路径

    Returns:
        list[str]: 警告信息列表
    """
    warnings: list[str] = []

    # 检查 1: 空段落（可能代表空章节）
    try:
        empty_para_count = 0
        for para in doc.paragraphs:
            text = para.text.strip()
            if not text and not para.runs:
                empty_para_count += 1
        if empty_para_count > 50:  # 超过 50 个空段落视为异常
            warnings.append(f"文档中存在 {empty_para_count} 个空段落，可能存在空章节")
        elif empty_para_count > 10:
            logger.info("文档中存在 %d 个空段落（可接受范围）", empty_para_count)
    except Exception as e:
        logger.warning("检查空段落时出错: %s", e)

    # 检查 2: 验证图片缓存目录中的文件是否有效（V1.2 优化：限制最多检查 50 个文件，避免大项目卡顿）
    images_dir = PROJECTS_DATA_DIR / str(project_id) / "images"
    if images_dir.exists():
        try:
            invalid_images = 0
            checked_count = 0
            for img_file in images_dir.iterdir():
                if checked_count >= 50:
                    break
                if img_file.is_file() and img_file.suffix.lower() in (
                    ".jpg",
                    ".jpeg",
                    ".png",
                    ".gif",
                    ".webp",
                ):
                    checked_count += 1
                    if img_file.stat().st_size == 0:
                        invalid_images += 1
                        logger.warning("图片文件为空: %s", img_file)
            if invalid_images > 0:
                warnings.append(f"存在 {invalid_images} 个空图片文件")
        except Exception as e:
            logger.warning("检查图片文件时出错: %s", e)

    # 检查 3: 文档保存后校验文件大小
    # 注意：此函数在 doc.save() 之前调用，因此不在此处检查文件是否存在。
    # 文件大小校验已在 build_docx 中 doc.save() 之后执行（line 2532-2540）。
    return warnings


# ========== BUG-E-06 修复：HTTP Service 可用性检查辅助函数 ==========
# build_docx 在线程池线程中运行（无 running loop），不能直接用
# asyncio loop.run_until_complete()（在线程内必然失败）。
# 用 _safe_asyncio_run 统一处理，并做进程级缓存避免重复检查。
_HTTP_CHECK_CACHE: dict[str, tuple[bool, float]] = {}  # key -> (available, 检查时间戳)
_HTTP_CHECK_CACHE_LOCK = threading.Lock()
# CHART-MARKER-UNIFY 2026-09-16：可用性结果增加 TTL。
# 原实现把检查结果**永久**缓存：只要进程启动时（或首次导出时）Mermaid HTTP Service
# 尚未就绪，之后即便服务已启动，本进程内的所有导出都会永久跳过质量最高的
# Level 1.5 渲染路径（只剩 PIL v2），与「不做降级、按第一优先级方案渲染」相悖。
# 加 TTL 后：TTL 内仍只检查一次（保留避免每图 5s 健康检查超时的初衷），
# 过期后允许重新探测，服务晚启动也能自动回到第一优先级路径。
_HTTP_CHECK_CACHE_TTL = 600.0


def _check_http_service_available() -> bool:
    """检查 Mermaid HTTP Service（第一优先级渲染路径）是否可用（带锁 + TTL 缓存）

    与 MermaidServiceClient 内置的 60s TTL 缓存不同，本函数是进程级缓存：
    整个导出流程（preload + 正文写入）只执行一次真实检查，其余调用直接命中缓存，
    避免重复的 5s 健康检查超时；缓存 TTL 见 `_HTTP_CHECK_CACHE_TTL`
    （过期后允许重新探测，服务晚启动也能自动回到第一优先级路径）。

    Returns:
        bool: True=HTTP 服务可用；False=不可用或检查失败
    """
    _now = time.time()
    with _HTTP_CHECK_CACHE_LOCK:
        _cached = _HTTP_CHECK_CACHE.get("available")
        if _cached is not None and _now - _cached[1] < _HTTP_CHECK_CACHE_TTL:
            return _cached[0]
    try:
        from .ai.mermaid_service import get_mermaid_service_client
        from .ai.workflows_base import _safe_asyncio_run

        # PERF-FIX 2026-09-18（PO-4）：原 docker/railway 各起一次 _safe_asyncio_run
        # （每次新建一个事件循环），合并为单循环内顺序探测，省一次 loop 创建；
        # 保留原有“单项异常不阻断另一项”的容错语义。
        async def _probe_available() -> bool:
            _c = get_mermaid_service_client()
            try:
                if await _c.check_docker_available():
                    return True
            except Exception:
                pass
            try:
                return bool(await _c.check_railway_available())
            except Exception:
                return False

        _available = bool(_safe_asyncio_run(_probe_available()))
    except Exception as e:
        logger.debug("HTTP Service 可用性检查异常（按不可用处理）: %s", e)
        _available = False
    with _HTTP_CHECK_CACHE_LOCK:
        _HTTP_CHECK_CACHE["available"] = (_available, time.time())
    logger.info("HTTP Service 可用性检查完成: available=%s", _available)
    return _available


# ========== BUG-E-03 修复：共享图表渲染线程池 ==========
# 每张图创建 ThreadPoolExecutor(max_workers=1) 有 ~10ms 创建/关闭开销，
# 150 张图合计 ~1.5s。改为进程级共享线程池复用线程，避免反复创建。
_RENDER_EXECUTOR: ThreadPoolExecutor | None = None
_RENDER_EXECUTOR_LOCK = threading.Lock()


def _get_render_executor() -> ThreadPoolExecutor:
    """获取全局共享图表渲染线程池（懒初始化，进程内复用）"""
    global _RENDER_EXECUTOR
    with _RENDER_EXECUTOR_LOCK:
        if _RENDER_EXECUTOR is None:
            _RENDER_EXECUTOR = ThreadPoolExecutor(
                max_workers=4,
                thread_name_prefix="chart_render_pool",
            )
        return _RENDER_EXECUTOR


# ========== V1.0 并行图表预渲染（多线程优化） ==========
def _preload_chart_images_parallel(
    detail_index: dict[int, object],
    config: ExportConfig,
    max_workers: int | None = None,
    _http_service_available: bool | None = None,
) -> int:
    """使用 ThreadPoolExecutor 并行预渲染所有章节的图表图片到缓存。

    Args:
        detail_index: 章节详情字典 {section_id: Section}
        config: 导出配置
        max_workers: 线程池大小
        _http_service_available: BUG-E-02 修复：HTTP 服务可用性预检查结果
            - True/False：直接使用（跳过重复检查）
            - None：在此处执行检查（向后兼容）

    Returns:
        int: 成功预渲染的图表数量
    """
    if max_workers is None:
        # BUG-E-04 修复：限制并发数，避免 CPU 争抢导致整体变慢
        max_workers = min(8, (os.cpu_count() or 4) * 2)

    # BUG-E-02 修复：如果调用方已传入 HTTP 检查结果，直接使用
    # 否则在此处执行一次性检查（向后兼容直接调用场景）
    # BUG-E-07 修复：改用 _check_http_service_available（进程级缓存 + 锁），
    # 移除遗留的 asyncio.get_event_loop()（在线程池中必然失败）
    if _http_service_available is None:
        try:
            _http_service_available = _check_http_service_available()
        except Exception as e:
            logger.debug("HTTP Service 预检查异常（按不可用处理）: %s", e)
            _http_service_available = False

    chart_tasks: list[tuple[int, str, str, object]] = []
    for section_id, section_detail in detail_index.items():
        if not section_detail or not section_detail.content:
            continue
        cached_types, hint_types = _get_cached_inline_chart_markers(section_detail)
        for chart_type, (json_field, default_title) in _CHART_TYPE_MAP.items():
            # CHART-MARKER-UNIFY 2026-09-16：
            # - 标记内含真实数据的类型：内联渲染用标记内数据，预渲染载荷不同 → 跳过
            # - 「配图位置提示」型标记（<<CHART:类型>> 等）：渲染数据来自章节 DB 字段，
            #   与预渲染载荷（缓存键剔除图号后）完全一致 → 必须一并预热，
            #   否则正文写作阶段这些图表会退化为串行渲染，大文档导出明显变慢
            if chart_type in cached_types and chart_type not in hint_types:
                continue
            chart_data = getattr(section_detail, json_field, None)
            if chart_data:
                chart_tasks.append((section_id, chart_type, default_title, section_detail))

    if not chart_tasks:
        return 0

    logger.info(
        "并行预渲染 %d 个图表（线程池大小=%d, HTTP可用=%s）",
        len(chart_tasks),
        max_workers,
        _http_service_available,
    )

    # 线程安全修复：preload_only=True 只做缓存预热，不写入任何 Document
    def _preload_single(task: tuple) -> bool:
        section_id, chart_type, default_title, section_detail = task
        try:
            # P2：原实现忽略 _add_inline_chart 返回值直接 return True，
            # 渲染失败（返回 False）也被计入成功 → 日志里“全部成功”假象，
            # 真实预渲染命中率无从监控。现透传成败结果。
            _ok = _add_inline_chart(
                None,
                chart_type,
                default_title,
                section_detail,
                config,
                preload_only=True,
                _http_service_available=_http_service_available,
            )
            return _ok is True
        except Exception as e:
            logger.debug(
                "预渲染图表失败: section_id=%s, chart_type=%s, %s", section_id, chart_type, e
            )
            return False

    success_count = 0
    # BUG-EXPORT-06 修复：从120s恢复到300s。预加载已绕过渲染池（直接调用），
    # 每图耗时大幅降低，但大文档（50+图表）仍需多个批次，120s容易超时。
    # 300s 与前端 TOTAL_TIMEOUT_MS 对齐，确保大文档也能完成预加载。
    TOTAL_PRELOAD_TIMEOUT = 300
    executor = ThreadPoolExecutor(max_workers=max_workers)
    try:
        futures_map = {executor.submit(_preload_single, t): t for t in chart_tasks}
        # BUG-FIX 2026-09-14（高）：原实现 `done, remaining = wait(...)`，
        # concurrent.futures.wait 返回的是 **set**，第二轮循环执行 `remaining.keys()`
        # 会抛 AttributeError，被上层"非致命"吞掉后预渲染只完成 1 张图，
        # 其余全部退化为正文阶段串行渲染（大文档导出耗时翻倍）。
        # 修复：用独立的 pending 字典保留 future→task 映射，只把 not_done 写回。
        pending = dict(futures_map)
        deadline = time.time() + TOTAL_PRELOAD_TIMEOUT
        while pending:
            if time.time() >= deadline:
                logger.warning(
                    "图表预渲染总体超时（%ds），已放弃 %d 个未完成任务",
                    TOTAL_PRELOAD_TIMEOUT,
                    len(pending),
                )
                for f in pending:
                    f.cancel()
                break
            done, not_done = wait(
                pending.keys(),
                timeout=max(0, deadline - time.time()),
                return_when=FIRST_COMPLETED,
            )
            pending = {f: t for f, t in pending.items() if f in not_done}
            for future in done:
                try:
                    if future.result(timeout=30):
                        success_count += 1
                except Exception as e:
                    logger.warning("图表预渲染线程异常: %s", e)
    finally:
        # BUG-EXPORT-06 修复：shutdown(wait=False) 避免阻塞导出流程。
        # 超时后仍有线程在运行（cancel() 对已启动线程无效），wait=True 会
        # 等待这些线程完成，导致导出进度卡在 chart_preload 25% 不动。
        executor.shutdown(wait=False)

    logger.info("图表预渲染完成: %d/%d 成功", success_count, len(chart_tasks))
    return success_count


def _atomic_save_docx(doc, output_path: str) -> None:
    """原子写盘：写同目录临时文件 → 大小校验 → os.replace 替换为最终路径。

    ATOMIC-SAVE（2026-10-06）：原 `doc.save(output_path)` 直写最终路径。docx 打包是
    一次完整 XML 序列化 + ZIP 写盘，中途失败（磁盘满 / 权限 / 异常）会在 exports/
    留下截断损坏的 .docx。现改为「同目录 .tmp 写满 → 零字节拒绝 → os.replace 原子
    替换」：任一步失败都只留下 .tmp.* 临时件并被清理，最终 .docx 要么不存在、要么完整。
    与本文件 _save_export_cache_index 的原子写口径一致。
    """
    tmp_save_path = f"{output_path}.tmp.{os.getpid()}"
    try:
        doc.save(tmp_save_path)
        # 零字节半成品不进入最终路径
        if os.path.getsize(tmp_save_path) == 0:
            raise RuntimeError("临时文件大小为 0，拒绝原子替换")
        os.replace(tmp_save_path, output_path)
    except Exception as e:
        logger.error("保存文档失败: %s", e)
        # 清理可能残留的临时件，避免 exports/ 目录堆积半成品
        try:
            if os.path.exists(tmp_save_path):
                os.remove(tmp_save_path)
        except OSError:
            pass
        raise


def build_docx(
    project_id: int,
    config: ExportConfig | None = None,
    output_path: str | None = None,
    on_progress: Callable | None = None,
) -> tuple[str, int | None]:
    """主函数：生成完整的 DOCX 文档

    Args:
        project_id: 项目 ID
        config: 导出配置
        output_path: 输出路径，为 None 时自动生成
        on_progress: 进度回调函数，签名 (step: str, detail: str, progress: float) -> None

    Returns:
        tuple[str, int | None]: 生成的 DOCX 文件路径 与 导出记录 ID
    """
    start_time = time.time()

    def _progress(step: str, detail: str = "", progress: float = 0.0):
        if on_progress:
            try:
                on_progress(step, detail, progress)
            except Exception as e:
                logger.debug("进度回调异常: %s", e)

    if config is None:
        config = ExportConfig()

    # ENHANCE 2026-09-14：导出格式预设解析（版式 + 主题）。
    # 必须在计算导出缓存键之前完成，否则「同一内容 + 不同预设」会命中同一条缓存，
    # 用户换了预设却拿到旧排版的文档。resolve_export_config 只覆盖调用方未显式
    # 传值的字段，因此不会回滚前端表单里的用户改动。
    config = resolve_export_config(config)

    # 关键修复（P1-4）：只清空当前项目的缓存，避免影响其他并发导出请求
    # 原代码 _GET_SECTION_CACHE.clear() 会清掉所有项目的缓存
    _clear_section_cache_for_project(project_id)
    # BUG-FIX 2026-09-14（高）：_figure_counter_state 是进程级全局，
    # 而 _reset_figure_counter() 全仓库无任何调用方 → 图号跨导出会话累加，
    # 第二次导出同一项目会从「图3-8」开始编号（标书图号错误，直接影响评分合规性）。
    # 修复：每次 build_docx 开始时重置。
    # P1 修复 2026-09-29：只重置**本项目**的桶。原实现无条件 clear 全部，
    # 与另一个项目并发导出时会抹掉对已分配的号 → 对后续图号从头重算，
    # 同一文档内出现重复图号。（同项目并发的两个导出会话仍共享计数，
    # 由上游任务组锁保证同项目同时只有一个导出。）
    _reset_figure_counter(project_id)
    # BUG-E-05 修复：移除此处的 _cleanup_section_cache() 调用
    # 原代码在每次导出开始时清理所有项目的缓存，导致并发导出互相干扰
    # _cleanup_section_cache() 仅在 _get_section_detail_with_cleanup 中按需调用

    # V7.0 导出结果缓存：相同配置+内容指纹时直接复用最近结果
    _progress("init", "初始化文档...", 0.0)
    # BUG-FIX 2026-09-14（高）：_compute_content_fingerprint 异常时返回 None，
    # 原实现仍把 None 拼进 cache_key，得到 f"{pid}|{config}|None" 这一**常量键**，
    # 导致此后该项目永远命中第一次（可能是错误的）导出结果，用户改了正文也拿旧文档。
    # 修复：指纹为 None 时显式禁用缓存。
    content_fingerprint = _compute_content_fingerprint(project_id)
    cache_key = None
    cached_path = None
    if content_fingerprint is not None:
        cache_key = _compute_cache_key(project_id, config, content_fingerprint)
        cached_path = _get_cached_export_path(cache_key)
    else:
        logger.warning("内容指纹计算失败，本次导出禁用结果缓存 project_id=%s", project_id)
    if cached_path and Path(cached_path).exists():
        logger.info("命中导出缓存，直接复用: %s", cached_path)
        _progress("cache_hit", "复用历史导出...", 0.95)
        output_path = cached_path
        filename = Path(output_path).name
        export_id = _record_export(project_id, filename, output_path, config, start_time)
        _progress("done", "导出完成（缓存复用）", 1.0)
        return output_path, export_id

    doc = Document()

    # 设置文档更新字段选项（修复4: 确保目录页页码在 Word 打开时自动刷新）
    try:
        settings = doc.settings.element
        existing_update = settings.find(qn("w:updateFields"))
        if existing_update is None:
            update_fields = parse_xml(
                f'<w:updateFields {nsdecls("w")} w:val="true" w:dirty="true"/>'
            )
            settings.append(update_fields)
        else:
            # 确保已存在的 updateFields 元素也标记为 dirty
            existing_update.set(qn("w:val"), "true")
            existing_update.set(qn("w:dirty"), "true")
    except Exception as e:
        logger.warning("设置文档更新字段选项失败: %s", e)

    # 设置页面边距（ENHANCE 2026-09-14：支持预设的横向纸张与自定义页边距）
    try:
        section = doc.sections[0]
        landscape = str(getattr(config, "page_orientation", "portrait") or "").lower() == "landscape"
        if landscape:
            section.page_width = Cm(29.7)
            section.page_height = Cm(21)
        else:
            section.page_width = Cm(21)
            section.page_height = Cm(29.7)
        # 0 表示「未指定」，回退历史默认值；预设给的都是 >0 的显式值
        _margin = lambda value, default: Cm(float(value)) if float(value or 0) > 0 else Cm(default)  # noqa: E731
        section.top_margin = _margin(getattr(config, "page_margin_top_cm", 0), 2.54)
        section.bottom_margin = _margin(getattr(config, "page_margin_bottom_cm", 0), 2.54)
        section.left_margin = _margin(getattr(config, "page_margin_left_cm", 0), 3.18)
        section.right_margin = _margin(getattr(config, "page_margin_right_cm", 0), 3.18)
    except Exception as e:
        logger.warning("设置页面边距失败: %s", e)

    # 确保 Heading 1~8 样式存在（L5+ 按正文样式：不加粗、字号/字体对齐正文）
    _progress("styles", "应用标题样式...", 0.05)
    _ensure_heading_styles(doc, body_font=config.font_body, body_size_pt=float(config.size_body))

    # 获取项目信息（与 analysis 查询合并，一次 DB 连接取两表）
    project_name = "未知项目"
    bidder_name = ""
    try:
        with get_db() as conn:
            project_row = conn.execute(
                "SELECT name, bidder_name FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
            if project_row:
                project_name = project_row["name"] or "未知项目"
                bidder_name = config.bidder_name or (
                    project_row["bidder_name"] if project_row["bidder_name"] else ""
                )
    except Exception as e:
        logger.error("获取项目信息失败: %s", e)

    # BUG-FIX 2026-10-04（P1 特殊字符）：封面项目名/投标单位与页眉文本直接来自
    # projects 表原文，未走 sanitize 链路。项目名带 \x0b 等控制字符时 add_run 抛
    # ValueError，被封面/页眉各自的 try/except 整块吞掉 → 封面或页眉**整体静默
    # 消失**（与正文标题曾因同类问题修复前的形态一致，见 sanitize_for_docx 注释）。
    project_name = sanitize_for_docx(project_name)
    bidder_name = sanitize_for_docx(bidder_name)

    # 添加封面
    if config.show_title_page:
        _progress("cover", "添加封面...", 0.1)
        try:
            for _ in range(6):
                doc.add_paragraph()

            title_p = doc.add_paragraph()
            title_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            _set_paragraph_indent_chars(title_p, first_line_chars=0)
            run = title_p.add_run(project_name)
            _apply_font(run, config.font_title, config.size_title_1 + 4, bold=True)

            doc.add_paragraph()

            if bidder_name:
                bidder_p = doc.add_paragraph()
                bidder_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                _set_paragraph_indent_chars(bidder_p, first_line_chars=0)
                run = bidder_p.add_run(f"投标单位: {bidder_name}")
                _apply_font(run, config.font_body, config.size_body)

            date_p = doc.add_paragraph()
            date_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            _set_paragraph_indent_chars(date_p, first_line_chars=0)
            run = date_p.add_run(datetime.now(UTC).strftime("%Y年%m月"))
            _apply_font(run, config.font_body, config.size_body)

            doc.add_page_break()
        except Exception as e:
            logger.error("添加封面失败: %s", e)

    # 添加目录
    if config.show_toc:
        _progress("toc", "添加目录...", 0.15)
        try:
            toc_heading = doc.add_paragraph()
            toc_heading.alignment = WD_ALIGN_PARAGRAPH.CENTER
            _set_paragraph_indent_chars(toc_heading, first_line_chars=0)
            run = toc_heading.add_run("目  录")
            _apply_font(run, config.font_title, config.size_title_1, bold=True)

            _add_toc_field(doc, config)

            # ENHANCE 2026-09-14：一级标题另起页时，由 pageBreakBefore 负责分页，
            # 此处再插一个硬分页会产生整页空白。
            if not getattr(config, "heading_level1_page_break", False):
                doc.add_page_break()
        except Exception as e:
            logger.error("添加目录失败: %s", e)

    # 添加正文内容（最耗时的部分）
    _progress("content_start", "开始写入正文内容...", 0.2)
    content_start_time = time.time()
    try:
        tree = build_tree(project_id)
        # 批量预加载所有有正文的章节详情（含非叶子），消除 N+1 查询
        content_ids = _collect_content_node_ids(tree)
        detail_index = _batch_load_section_details(project_id, content_ids) if content_ids else {}
        _progress("content_load", f"已加载 {len(detail_index)} 个章节详情", 0.22)

        # BUG-E-02 修复：一次性检查 HTTP Service 可用性，缓存结果供后续使用
        # 在本地开发环境中 HTTP 服务通常不可用，每图检查 5s 超时会严重拖慢导出
        # BUG-E-06 修复：build_docx 在线程池线程中运行（无 running loop），
        # 必须用 _safe_asyncio_run 而非 asyncio.get_event_loop()（后者在线程内必然失败）
        # BUG-E-07 修复：统一走 _check_http_service_available（进程级缓存，带锁）
        # 检查失败时默认 False（按不可用处理）——绝不能置 None：
        # None 会让 _add_inline_chart 走 HTTP 路径并等待 30s×2 重试超时
        # （150 张图 ≈ 2.7 小时），是导出卡顿的最大风险点
        try:
            _http_service_available = _check_http_service_available()
        except Exception as e:
            logger.debug("HTTP Service 预检查异常（按不可用处理）: %s", e)
            _http_service_available = False

        # V1.0 增强：并行图表预渲染（多线程预热图表缓存）
        _progress("chart_preload", "并行预渲染图表...", 0.25)
        try:
            preloaded = _preload_chart_images_parallel(
                detail_index,
                config,
                _http_service_available=_http_service_available,
            )
            if preloaded > 0:
                logger.info("并行预渲染 %d 个图表完成", preloaded)
        except Exception as e:
            logger.warning("图表并行预渲染异常（非致命）: %s", e)
        _progress("chart_preload_done", "图表预渲染完成", 0.28)

        _write_tree_content_with_progress(
            doc,
            tree,
            config,
            project_id=project_id,
            on_progress=_progress,
            detail_index=detail_index,
            _http_service_available=_http_service_available,
        )
        # 性能日志：记录正文写入耗时
        content_elapsed = time.time() - content_start_time
        # BUG-FIX 2026-09-18（P0）：`_collect_leaf_ids` 已被
        # `_collect_content_node_ids` 取代（同一个 P1 修复的两半），但本行的
        # `leaf_ids` 漏改 → 每次导出都在收尾日志处抛
        # `NameError: name 'leaf_ids' is not defined`，被下面的 except 捕获后
        # 重新抛出，导出直接 500（文档已经写完却拿不到）。改用 `content_ids`。
        logger.info("正文写入完成: 耗时 %.2fs, 章节数=%d", content_elapsed, len(content_ids))
    except Exception as e:
        logger.error("写入正文内容失败: %s", e)
        raise

    # 添加页眉
    if config.show_header_footer:
        _progress("header", "添加页眉...", 0.85)
        try:
            _hf_color = _theme_color(config, "header_footer_color", "")
            for sec in doc.sections:
                header = sec.header
                header.is_linked_to_previous = False
                hp = header.paragraphs[0] if header.paragraphs else header.add_paragraph()
                hp.text = ""
                hp.alignment = WD_ALIGN_PARAGRAPH.CENTER
                _set_paragraph_indent_chars(hp, first_line_chars=0)
                # 页眉增强：项目名称 + 投标单位（公司名称），满足招标文件"页眉显示招标编号/公司名称"要求
                _header_text = project_name
                if bidder_name:
                    _header_text = f"{_header_text} ｜ {bidder_name}"
                run = hp.add_run(_header_text)
                _apply_font(run, config.font_body, config.size_body - 2)
                if _hf_color:
                    run.font.color.rgb = RGBColor.from_string(_hf_color)
        except Exception as e:
            logger.warning("添加页眉失败: %s", e)

    # 添加页脚：页码（PAGE / NUMPAGES 域，延迟绑定，Word 打开时自动刷新）
    # ENHANCE 2026-09-14：页码开关与格式由导出预设控制（如紧凑版只显示 "{page}"）
    if config.show_header_footer and getattr(config, "page_number_enabled", True):
        try:
            _hf_color = _theme_color(config, "header_footer_color", "")
            _page_format = str(getattr(config, "page_number_format", "") or "第{page}页")
            if "{page}" not in _page_format:
                _page_format = f"第{_page_format}页" if _page_format else "第{page}页"
            _prefix, _, _suffix = _page_format.partition("{page}")
            for sec in doc.sections:
                footer = sec.footer
                footer.is_linked_to_previous = False
                fp = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
                fp.text = ""
                fp.alignment = WD_ALIGN_PARAGRAPH.CENTER
                _set_paragraph_indent_chars(fp, first_line_chars=0)

                def _styled(text: str | None = None, field: str | None = None):
                    """统一的页脚 run 构造：文字 run 与域 run 都套用主题色"""
                    r = fp.add_run(text or "")
                    if field:
                        r._element.append(parse_xml(f'<w:fldChar {nsdecls("w")} w:fldCharType="begin"/>'))
                        r._element.append(parse_xml(
                            f'<w:instrText {nsdecls("w")} xml:space="preserve"> {field} </w:instrText>'
                        ))
                        r._element.append(parse_xml(f'<w:fldChar {nsdecls("w")} w:fldCharType="separate"/>'))
                        r._element.append(parse_xml(f'<w:t {nsdecls("w")} xml:space="preserve">1</w:t>'))
                        r._element.append(parse_xml(f'<w:fldChar {nsdecls("w")} w:fldCharType="end"/>'))
                    _apply_font(r, config.font_body, config.size_body - 2)
                    if _hf_color:
                        r.font.color.rgb = RGBColor.from_string(_hf_color)
                    return r

                # 文本格式为「前缀 + {page} + 后缀」；页码后追加 " / 共 N 页"
                if _prefix:
                    _styled(_prefix)
                _styled(field="PAGE")
                if _suffix:
                    _styled(_suffix)
                _styled(" / 共 ")
                _styled(field="NUMPAGES")
                _styled(" 页")
        except Exception as e:
            logger.warning("添加页脚失败: %s", e)

    # 保存文档
    _progress("save", "保存文档...", 0.95)
    # V2.0 大文档优化：保存前强制 GC 并记录内存状态
    gc.collect()
    try:
        import psutil

        _mem = psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
        logger.info("导出前内存使用: %.1f MB (project_id=%d)", _mem, project_id)
    except ImportError:
        logger.debug("psutil 未安装，跳过内存监控")
    except Exception as _e:
        logger.debug("[silent-except] export.py: line 2295 - %s", _e)
    if output_path is None:
        try:
            export_dir = PROJECTS_DATA_DIR / str(project_id) / "exports"
            export_dir.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
            safe_name = "".join(c if c.isalnum() or c in "_.-" else "_" for c in project_name)
            # BUG-FIX：招标项目名常是完整标段名（上百字），叠加 data/projects/<id>/exports
            # 目录深度与时间戳后缀，容易突破 Windows 260 路径 / 255 文件名上限，
            # doc.save() 直接 OSError → 500。按字符截断到安全长度。
            safe_name = safe_name[:80].strip("_.-") or "project"
            filename = f"{safe_name}_{timestamp}.docx"
            output_path = str(export_dir / filename)
        except Exception as e:
            logger.error("创建导出目录失败: %s", e)
            raise
    else:
        output_dir = os.path.dirname(output_path)
        if output_dir:
            try:
                os.makedirs(output_dir, exist_ok=True)
            except Exception as e:
                logger.error("创建输出目录失败: %s", e)
        filename = os.path.basename(output_path)

    # ========== 修复3: 导出前文档结构校验 ==========
    _progress("validate", "校验文档结构...", 0.96)
    _validate_start = time.time()
    try:
        pre_warnings = _validate_document_structure(doc, project_id, output_path)
        for w in pre_warnings:
            logger.warning("导出前校验警告: %s", w)
    except Exception as e:
        logger.warning("导出前校验异常: %s", e)
    logger.info("导出前校验耗时: %.3fs", time.time() - _validate_start)

    # ========== P0-FIX: doc.save() 性能追踪 + 中间进度 ==========
    # python-docx save() 对大文档（300+节）需要做完整 XML 序列化 + ZIP 打包，
    # 耗时可达数秒~数十秒。之前无任何中间进度，前端卡在 96% 让用户误以为卡死。
    # 修复：拆分 save 为 97% (开始写盘) → save → 99% (写盘完成) → 100% (完成)
    _progress("save_start", "正在打包 DOCX 文件...", 0.97)
    _save_start = time.time()
    # ATOMIC-SAVE（2026-10-06）：原子写盘收口到 _atomic_save_docx，可独立单元测试。
    _atomic_save_docx(doc, output_path)
    logger.info("doc.save() 耗时: %.3fs (project_id=%d)", time.time() - _save_start, project_id)

    _progress("save_done", "正在校验导出结果...", 0.99)
    # ========== 修复3: 导出后文件大小校验 ==========
    try:
        file_size = Path(output_path).stat().st_size
        if file_size == 0:
            logger.error("导出文件大小为 0，导出失败")
            raise RuntimeError(f"导出文件大小为 0: {output_path}")
        logger.info("导出文件大小: %d bytes", file_size)
    except OSError as e:
        logger.error("校验导出文件大小失败: %s", e)
        raise

    # 记录导出信息
    export_id = None
    try:
        if cache_key:
            _set_cached_export_path(cache_key, output_path)
        export_id = _record_export(project_id, filename, output_path, config, start_time)
    except Exception as e:
        logger.error("记录导出信息失败: %s", e)

    _progress("done", "导出完成", 1.0)
    # 性能日志：记录导出总耗时
    total_elapsed = time.time() - start_time
    logger.info(
        "DOCX导出完成: 总耗时 %.2fs, 文件=%s, 大小=%d bytes", total_elapsed, output_path, file_size
    )
    return output_path, export_id


# ====== V7.0 导出缓存辅助 ======
_EXPORT_RESULT_CACHE_DIR = PROJECTS_DATA_DIR / "_exports"
_EXPORT_RESULT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
_EXPORT_CACHE_INDEX_PATH = _EXPORT_RESULT_CACHE_DIR / "_cache_index.json"
_EXPORT_CACHE_MAX_ENTRIES = 8  # 最多缓存 8 个不同配置的导出
_EXPORT_CACHE: dict[str, str] = {}
_EXPORT_CACHE_LOADED = False
_EXPORT_CACHE_LOCK = threading.RLock()  # 保护缓存索引并发读写；P0-FIX: 必须用 RLock（可重入），否则 _set_cached_export_path → _load_export_cache_index 嵌套获取会导致死锁
_EXPORT_CACHE_HITS = 0
_EXPORT_CACHE_MISSES = 0
# BUG-FIX 2026-09-18（P1）：索引 JSON 损坏被重置为 {} 时置位，
# 启动孤儿清理据此跳过，避免把仍在用的成品文件误判为孤儿全删。
_EXPORT_CACHE_INDEX_CORRUPTED = False
# 孤儿文件只清理「最后修改超过 7 天」的，保护正在写入/刚导出的产物。
_EXPORT_ORPHAN_MIN_AGE_SECONDS = 7 * 24 * 3600


def _is_safe_export_artifact(p: Path) -> bool:
    """判断路径是否为「可安全删除的导出产物」。

    BUG-FIX 2026-09-18（中）：原淘汰判据是 `"_exports" in str(p)`，但导出产物实际
    落在 `PROJECTS_DATA_DIR/<project_id>/exports/`（见 build_docx 内 export_dir），
    路径中根本不含 `_exports` 这个子串 → 淘汰分支永远不成立。后果：
    缓存索引虽限制 8 条，磁盘上的 DOCX 却只增不减，长期运行必然占满磁盘。
    同理 `_cleanup_orphaned_export_files` 只 glob `_exports/*.docx`，也扫不到真实产物。

    改为实质校验：必须位于项目数据目录内，且后缀是 .docx/.doc（避免误删用户上传件）。
    """
    try:
        rp = p.resolve()
        base = PROJECTS_DATA_DIR.resolve()
        rp.relative_to(base)
    except (ValueError, OSError):
        return False
    return rp.suffix.lower() in (".docx", ".doc")


def _iter_export_artifacts() -> list[Path]:
    """枚举所有真实导出产物：`<数据目录>/<project_id>/exports/*.docx`。

    BUG-FIX 2026-09-18（中）：与 `_is_safe_export_artifact` 同一处缺陷的配套修复。
    """
    found: list[Path] = []
    try:
        for proj_dir in PROJECTS_DATA_DIR.iterdir():
            if not proj_dir.is_dir():
                continue
            exp_dir = proj_dir / "exports"
            if not exp_dir.is_dir():
                continue
            for f in exp_dir.glob("*.docx"):
                found.append(f)
    except OSError as e:
        logger.warning("[导出缓存] 枚举导出产物失败: %s", e)
    return found


def _cleanup_orphaned_export_files():
    """BUG-FIX-04 修复：启动时清理孤儿导出文件。

    场景：服务异常重启、缓存索引损坏、手动删除索引文件等情况下，
    _exports 目录下可能残留不在缓存索引中的孤儿 DOCX 文件，持续占用磁盘空间。

    策略：
    1. 加载缓存索引，收集所有已注册的文件路径
    2. 扫描 _exports 目录下的所有 .docx 文件
    3. 删除不在索引中的孤儿文件
    4. 仅在启动时执行一次，不影响运行时性能
    """
    try:
        global _EXPORT_CACHE_INDEX_CORRUPTED
        if _EXPORT_CACHE_INDEX_CORRUPTED:
            logger.warning("[导出缓存] 索引文件损坏，启动清理跳过（防误删在用成品）")
            return
        index = _load_export_cache_index()
        registered_paths = set()
        for path_str in index.values():
            try:
                registered_paths.add(Path(path_str).resolve())
            except Exception:
                continue

        # BUG-FIX 2026-09-18（P1）：白名单必须并入 exports 表登记的 file_path——
        # 导出缓存索引只保留最近 8 条，但历史导出记录（前端下载列表）仍指向
        # 磁盘文件；仅以索引为白名单会把用户历史下载件删掉 → 404。
        try:
            with get_db() as conn:
                rows = conn.execute("SELECT file_path FROM exports WHERE file_path IS NOT NULL").fetchall()
            for r in rows:
                try:
                    registered_paths.add(Path(r["file_path"]).resolve())
                except Exception:
                    continue
        except Exception as e:
            # 查不到表/DB 异常时无法确认哪些文件在用 → 保守跳过本轮清理
            logger.warning("[导出缓存] 读取 exports 表失败，启动清理跳过: %s", e)
            return

        now_ts = time.time()
        orphan_count = 0
        # BUG-FIX 2026-09-18（中）：原来只 glob `_EXPORT_RESULT_CACHE_DIR/*.docx`
        # （即 `<数据目录>/_exports/`），而真实产物在 `<数据目录>/<pid>/exports/`，
        # 扫的是空目录 → 孤儿清理实际从未生效。改用 _iter_export_artifacts 扫描真实位置。
        for f in _iter_export_artifacts():
            try:
                resolved = f.resolve()
                if resolved in registered_paths:
                    continue
                # P1：只清理修改时间超过阈值的文件，避免删掉并发 worker
                # 正在写入的新导出产物（尚未登记到索引/表）。
                if now_ts - f.stat().st_mtime < _EXPORT_ORPHAN_MIN_AGE_SECONDS:
                    continue
                f.unlink()
                orphan_count += 1
            except OSError:
                continue
        if orphan_count > 0:
            logger.info("[导出缓存] 启动时清理孤儿文件 %d 个", orphan_count)
    except Exception as e:
        logger.warning("[导出缓存] 启动时清理孤儿文件失败: %s", e)


def _load_export_cache_index() -> dict[str, str]:
    """从磁盘加载导出缓存索引（key -> file_path）"""
    global _EXPORT_CACHE, _EXPORT_CACHE_LOADED, _EXPORT_CACHE_INDEX_CORRUPTED
    with _EXPORT_CACHE_LOCK:
        if _EXPORT_CACHE_LOADED:
            return _EXPORT_CACHE
        try:
            if _EXPORT_CACHE_INDEX_PATH.exists():
                with open(_EXPORT_CACHE_INDEX_PATH, encoding="utf-8") as f:
                    _EXPORT_CACHE = json.load(f) or {}
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("加载导出缓存索引失败: %s", e)
            # P1：索引损坏 ≠ 索引为空。若据此把全部成品当孤儿删除是灾难性误删，
            # 置损坏标记供启动清理判断。
            _EXPORT_CACHE_INDEX_CORRUPTED = True
            _EXPORT_CACHE = {}
        _EXPORT_CACHE_LOADED = True
        return _EXPORT_CACHE


def _save_export_cache_index():
    """将导出缓存索引持久化到磁盘

    BUG-FIX（P1 缓存索引损坏）：原实现直接 `open(path, "w")` 截断写 ——
    进程内虽有 `_EXPORT_CACHE_LOCK`，但 uvicorn 多 worker / Celery worker 是
    不同进程，同时写会把 `_cache_index.json` 截成半个 JSON。更糟的是启动时的
    `_cleanup_orphaned_export_files` 读不到索引会把**仍在用的缓存 DOCX 当孤儿
    unlink 掉**。改为「写临时文件 + os.replace 原子替换」。
    """
    tmp_path = None
    try:
        with _EXPORT_CACHE_LOCK:
            tmp_path = f"{_EXPORT_CACHE_INDEX_PATH}.tmp.{os.getpid()}"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(_EXPORT_CACHE, f, ensure_ascii=False, indent=2)
            # Windows 上 os.replace 要求目标不存在同名；replace 自身是原子操作
            os.replace(tmp_path, _EXPORT_CACHE_INDEX_PATH)
            tmp_path = None
    except OSError as e:
        logger.warning("保存导出缓存索引失败: %s", e)
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _compute_content_fingerprint(project_id: int) -> str | None:
    """计算项目内容指纹（章节内容、图表 JSON、章节树结构的综合哈希）

    仅依赖内容（不依赖配置），用于在内容不变时复用导出。
    异常时返回 None，调用方应跳过缓存。

    BUG-FIX-02 修复：指纹增加章节树结构字段（parent_id, level, order, title）。
    原实现仅包含 content 和图表 JSON，当章节被重新排序、层级调整或标题修改时，
    指纹不会变化，导致返回过期的缓存文档（章节顺序/层级与实际不一致）。
    """
    h = hashlib.sha256()
    try:
        with get_db() as conn:
            # BUG-FIX 2026-09-18（高）：指纹必须纳入 projects.name / bidder_name。
            # build_docx 用这两列渲染封面的项目名、封面的投标单位与页眉
            # （`bidder_name = config.bidder_name or project_row["bidder_name"]`，
            #  未显式传时取项目表的值 —— 该值不在 config 里，也就进不了 config 维度的缓存键）。
            # 原指纹只哈希 sections，用户在项目设置里改了投标单位后重新导出，
            # 指纹不变 → 命中上一次缓存的 DOCX → 封面与页眉仍是旧单位名称。
            # 投标文件中的投标人名称错误是硬性废标风险，必须让缓存失效。
            proj = conn.execute(
                "SELECT name, bidder_name FROM projects WHERE id = ?",
                (project_id,),
            ).fetchone()
            if proj:
                for field in ("name", "bidder_name"):
                    h.update(field.encode("utf-8"))
                    h.update(str(proj[field] or "").encode("utf-8"))
            rows = conn.execute(
                """SELECT id, title, content, parent_id, level, sort_order,
                          flowchart_json, gantt_json, architecture_json,
                          labor_json, comparison_json, layout_json, timeline_json,
                          inlined_chart_json
                   FROM sections WHERE project_id = ?
                   ORDER BY id""",
                (project_id,),
            ).fetchall()
        for row in rows:
            for field in (
                "title",
                "content",
                "parent_id",
                "level",
                "sort_order",
                "flowchart_json",
                "gantt_json",
                "architecture_json",
                "labor_json",
                "comparison_json",
                "layout_json",
                "timeline_json",
                # ENHANCE 2026-09-29（P3 指纹与导出列对齐）：inlined_chart_json
                # 已列入 SECTION_EXPORT_COLUMNS（导出批取字段），指纹必须覆盖同一集合，
                # 否则将来内联图渲染一旦消费该列，改了图却因指纹不变而命中旧缓存。
                # 今日该列尚无渲染消费者，纳入仅改变哈希值、不影响导出产物（向后兼容）。
                "inlined_chart_json",
            ):
                v = row[field] if row[field] is not None else ""
                h.update(str(row["id"]).encode("utf-8"))
                h.update(field.encode("utf-8"))
                h.update(str(v).encode("utf-8"))
        # ENHANCE 2026-10-04（T5）：指纹纳入本地图片文件的磁盘状态（名称/mtime_ns/size）。
        # 正文图片引用会落到 data/projects/{pid}/images/（含 _download_and_cache_image
        # 下载落盘）；替换同名图片不改任何 DB 列 → 旧指纹命中旧缓存 DOCX，
        # 导出成品里图片仍是旧图。逐文件 stat 成本极低；目录不存在则跳过。
        img_dir = PROJECTS_DATA_DIR / str(project_id) / "images"
        if img_dir.is_dir():
            for _img_f in sorted(img_dir.iterdir()):
                if not _img_f.is_file():
                    continue
                try:
                    _st = _img_f.stat()
                    h.update(_img_f.name.encode("utf-8"))
                    h.update(str(_st.st_mtime_ns).encode("utf-8"))
                    h.update(str(_st.st_size).encode("utf-8"))
                except OSError:
                    continue
    except Exception as e:
        logger.warning("计算内容指纹失败: %s", e)
        return None
    return h.hexdigest()


def _compute_cache_key(project_id: int, config: ExportConfig, fingerprint: str) -> str:
    """计算缓存键 = 项目ID + 配置序列化 + 内容指纹"""
    config_str = config.model_dump_json()
    raw = f"{project_id}|{config_str}|{fingerprint}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _get_cached_export_path(cache_key: str) -> str | None:
    """获取缓存的导出文件路径（若存在）

    V3.0 增强：命中率统计，支持缓存监控。
    """
    global _EXPORT_CACHE_HITS, _EXPORT_CACHE_MISSES
    index = _load_export_cache_index()
    path = index.get(cache_key)
    if path:
        _EXPORT_CACHE_HITS += 1
    else:
        _EXPORT_CACHE_MISSES += 1
    return path


def _set_cached_export_path(cache_key: str, file_path: str) -> None:
    """将导出结果注册到缓存索引

    - 若超过最大缓存数，淘汰最旧条目（删除对应文件）
    - BUG-E-07 修复：使用锁保护整个读-改-写流程，防止并发竞态
    """
    with _EXPORT_CACHE_LOCK:
        index = _load_export_cache_index()
        # BUG-FIX 2026-09-18（P2）：_EXPORT_CACHE 是进程内内存副本，多 worker /
        # Celery 并发登记时“最后写者胜”会全量覆盖掉其他进程新登记的键。
        # 写盘前重读磁盘索引并合并（同键以内存为准），跨进程更新收敛为
        # “只增不丢”；被淘汰又复活的陈旧键由读取方 exists() 校验兜底重建。
        try:
            if _EXPORT_CACHE_INDEX_PATH.exists():
                with open(_EXPORT_CACHE_INDEX_PATH, encoding="utf-8") as f:
                    _disk_index = json.load(f) or {}
                if isinstance(_disk_index, dict):
                    for _dk, _dv in _disk_index.items():
                        index.setdefault(_dk, _dv)
        except (OSError, json.JSONDecodeError) as _merge_e:
            logger.warning("重读导出缓存索引失败（本次写入可能丢失其他进程键）: %s", _merge_e)
        index[cache_key] = file_path
        if len(index) > _EXPORT_CACHE_MAX_ENTRIES:
            # 按插入顺序淘汰：转为 list 截断
            keys = list(index.keys())
            for k in keys[:-_EXPORT_CACHE_MAX_ENTRIES]:
                old_path = index.pop(k, None)
                # BUG-FIX 2026-09-18（P1）：淘汰只退索引、不再 unlink 磁盘文件——
                # exports 表的历史下载记录（前端“导出历史”列表）仍指向该文件，
                # 此处删文件会导致历史记录点击即 404。磁盘回收统一由启动时
                # `_cleanup_orphaned_export_files`（白名单 = 索引 ∪ exports 表，
                # 且只删修改超 7 天的真孤儿）负责。
                _ = old_path  # 仅作淘汰日志用途，不删文件
        # 在锁内同步持久化
        # BUG-FIX：必须走 _save_export_cache_index（临时文件 + os.replace 原子替换），
        # 不能用 open("w") 直接截断写。本项目已在 _save_export_cache_index 的 docstring 中
        # 记录过这一坑：多 worker / Celery 是不同进程，同时 open("w") 会把
        # _cache_index.json 截成半个 JSON，且启动孤儿清理会据此误删仍在用的缓存 DOCX。
        # 此前这里漏用了原子写入，等于重新引入该损坏风险。_EXPORT_CACHE_LOCK 是 RLock，
        # 此处已在锁内，_save_export_cache_index 内部再次获取锁可重入，安全。
        _save_export_cache_index()


def _record_export(
    project_id: int, filename: str, output_path: str, config: ExportConfig, start_time: float
) -> int | None:
    """将导出记录写入数据库，返回新插入的 export_id"""
    try:
        file_size = os.path.getsize(output_path)
        elapsed = time.time() - start_time
        with get_db() as conn:
            cursor = conn.execute(
                """INSERT INTO exports
                   (project_id, filename, file_path, file_size, config_json, status)
                   VALUES (?, ?, ?, ?, ?, 'completed')""",
                (project_id, filename, output_path, file_size, config.model_dump_json()),
            )
            export_id = cursor.lastrowid
        logger.info("导出完成: %s (%d bytes, %.2fs)", filename, file_size, elapsed)
        return export_id
    except Exception as e:
        logger.error("记录导出信息失败: %s", e)
        return None


def record_failed_export(project_id: int, error: str, config: ExportConfig | None = None) -> int | None:
    """ENHANCE 2026-10-04（T2）：失败的导出尝试同样落库（status='failed'）。

    exports 表早有 status 列但一直恒为 completed：失败不落库 → 无失败审计
    线索、前端历史看不到异常。现插入 file_path=''/file_size=0 的失败行，
    filename 携带时间戳与错误摘要便于列表直读。

    兼容性：latest/下载/白名单均已按 status='completed' 或非空 file_path 过滤，
    失败行不会被当作可下载产物；导出结果缓存不受影响。
    """
    try:
        summary = (error or "未知错误")[:120]
        with get_db() as conn:
            cursor = conn.execute(
                """INSERT INTO exports
                   (project_id, filename, file_path, file_size, config_json, status)
                   VALUES (?, ?, '', 0, ?, 'failed')""",
                (
                    project_id,
                    f"导出失败 {time.strftime('%Y-%m-%d %H:%M:%S')}：{summary}",
                    config.model_dump_json() if config is not None else "{}",
                ),
            )
            return cursor.lastrowid
    except Exception as e:
        logger.error("记录失败导出历史时再次失败: %s", e)
        return None


# 业务过滤：以下标题在导出时直接跳过
# （这些章节是模板占位/不适用标记，不应出现在最终文档中）
_EXCLUDED_TITLE_TOKENS = (
    "（本项目不涉及）",
    "(本项目不涉及)",
    "本项目不涉及",
)


def _should_skip_section(title: str) -> bool:
    """判断该章节是否应在导出时跳过"""
    if not title:
        return False
    stripped = title.strip()
    for token in _EXCLUDED_TITLE_TOKENS:
        if token in stripped:
            return True
    return False


def _detect_inline_chart_types(content: str) -> set[str]:
    """一次性扫描内容，返回所有已内联标记的图表类型集合

    V1.2 性能优化：替代 _has_inline_marker 的重复扫描，
    每次调用只扫描内容一次，通过 LRU 缓存按 section_detail id 复用结果。

    CHART-MARKER-UNIFY 2026-09-16：解析口径统一收敛到
    `content_cleaning.iter_chart_markers` + `resolve_chart_marker`（与 parse_content
    完全同源）。原实现自带三套互不一致的扫描逻辑（大写占位符字符串包含判断、
    `<<CHART:{纯文本标题}>>` 关键词推断、`<<CHART:{JSON}>>` 括号配平），与
    `parse_content` 的解析结果经常对不上，直接造成静默丢图：

      - `<<CHART:{施工进度甘特图}>>`（AI 实际输出的主流形态）被本函数判定为
        「内容已内联 gantt」→ `_render_section_charts` 跳过甘特图；
        而 `parse_content` 又因「纯文本占位符无真实数据」直接丢弃该标记 →
        内联/章节末尾两边都不渲染，图表彻底消失（DB 里的载荷还是空壳）。
      - `<<CHART:GANTT>>` 这类「配图位置提示标记」两边都识别不了。

    统一后，`_detect_inline_chart_types` 返回的类型集合 == `parse_content` 实际
    产出的 chart_marker 块类型集合，既不会重复出图，也不会两边都不出图。

    支持格式（详见 content_cleaning.resolve_chart_marker）：
    1. `[[FLOWCHART: 描述]]` / `<<FLOWCHART: 描述>>` — 传统占位符
    2. `<<GANTT:>>` / `<<GANTT:施工进度计划>>` — 显式类型前缀提示
    3. `<<CHART:GANTT>>` / `<<CHART:{gantt}>>` — 通配前缀 + 类型令牌
    4. `<<CHART:{施工进度甘特图}>>` — 通配前缀 + 纯文本标题（关键词推断）
    5. `<<CHART:{"type":"flowchart",...}>>` — 真实 JSON 数据块
    """
    return _detect_inline_chart_markers(content)[0]


def _detect_inline_chart_markers(content: str) -> tuple[set[str], set[str]]:
    """返回 (内联图表类型全集, 「配图位置提示」子集)。

    - 全集：内容里出现了标记的类型 → 导出时由**内联路径**渲染，章节末尾不再补图，
      避免同一张图渲染两次。
    - 提示子集：标记内**没有真实数据**（`<<CHART:GANTT>>` / `<<GANTT:标题>>` /
      传统 `<<FLOWCHART:描述>>`）的类型，其数据来自章节 DB 绑定字段。
      这类图表的渲染载荷与 `_add_inline_chart` 内联渲染时**完全一致**
      （缓存键已剔除图号），因此可以并行走预渲染，避免大文档里它们退化成长时间的
      串行渲染。标记里自带 JSON 数据的类型不预渲染（载荷不同，预热等于白做）。
    """
    if not content:
        return set(), set()

    from .content_cleaning import iter_chart_fences, iter_chart_markers, resolve_chart_marker

    valid_types = set(_CHART_TYPE_MAP)
    all_types: set[str] = set()
    hint_types: set[str] = set()
    for _marker, _prefix, _desc in iter_chart_markers(content):
        resolved = resolve_chart_marker(_prefix, _desc)
        if not resolved or resolved[0] not in valid_types:
            continue
        all_types.add(resolved[0])
        if _desc.lstrip().startswith("{"):
            continue  # 标记内自带真实数据 → 内联渲染使用标记内数据，预渲染键不同
        hint_types.add(resolved[0])
    # ENHANCE 2026-10-04（D2）：```mermaid / ```chart-json 围栏块同样计入「已内联类型」。
    # 围栏块自带真实数据（parse_content 产出同类型 chart_marker 块就地渲染），
    # 章末尾不再按 DB 字段补图；且不进预渲染（载荷与 DB 字段不同，预热白做）。
    for _lang, _body, _marker_text in iter_chart_fences(content):
        if _marker_text is None:
            continue  # 坏块：parse 侧会删除，不构成「已内联」
        try:
            _mt = json.loads(_marker_text)
            _ft = str(_mt.get("chart_type") or "").lower()
        except (json.JSONDecodeError, TypeError):
            continue
        if _ft in valid_types:
            all_types.add(_ft)
    return all_types, hint_types


def _get_cached_inline_chart_markers(section_detail) -> tuple[set[str], set[str]]:
    """按 section_detail id 缓存内联图表标记检测结果（(全集, 提示子集)）。

    V2.0 增强：TTL 淘汰 + 最大容量限制，避免长期缓存占用内存。
    V3.0 增强：命中率统计，支持缓存监控。
    """
    global _inline_marker_cache_hits, _inline_marker_cache_misses
    section_id = getattr(section_detail, "id", None)
    if section_id is None:
        _inline_marker_cache_misses += 1
        return set(), set()
    now = time.time()
    with _inline_marker_cache_lock:
        if section_id in _inline_marker_cache:
            cached_all, cached_hints, cached_ts = _inline_marker_cache[section_id]
            if now - cached_ts < _inline_marker_cache_ttl:
                _inline_marker_cache_hits += 1
                return cached_all, cached_hints
            # TTL 过期，移除
            del _inline_marker_cache[section_id]
    _inline_marker_cache_misses += 1
    content = getattr(section_detail, "content", "") or ""
    all_types, hint_types = _detect_inline_chart_markers(content)
    with _inline_marker_cache_lock:
        # 达到上限时淘汰最旧的一半（ts 位于元组末位）
        if len(_inline_marker_cache) >= _inline_marker_cache_max:
            sorted_items = sorted(_inline_marker_cache.items(), key=lambda x: x[1][-1])
            for key, _ in sorted_items[: len(sorted_items) // 2]:
                del _inline_marker_cache[key]
        _inline_marker_cache[section_id] = (all_types, hint_types, time.time())
    return all_types, hint_types


def _get_cached_inline_chart_types(section_detail) -> set[str]:
    """内联图表类型全集（兼容旧调用方）"""
    return _get_cached_inline_chart_markers(section_detail)[0]


def _get_cached_chart_render(payload_str: str, chart_type: str) -> tuple[BytesIO, int] | None:
    """L1 内存缓存：从内存中读取已渲染的图表（避免重复 render_mermaid_to_bytes 调用）

    V2.0 增强：检查 TTL，过期条目自动淘汰。
    V3.0 增强：添加命中率统计，支持缓存监控。
    P2 修复：cache key 原仅用 payload_str，同一段 Mermaid 代码以不同
    chart_type（如 flowchart vs sequence）渲染时会命中错误缓存。
    """
    global _chart_render_cache_hits, _chart_render_cache_misses
    now = time.time()
    cache_key = f"{chart_type}:{payload_str}"
    with _chart_render_cache_lock:
        if cache_key in _chart_render_cache:
            stream, size, ts = _chart_render_cache[cache_key]
            if now - ts < _chart_render_cache_ttl:
                _chart_render_cache_hits += 1
                return stream, size
            # TTL 过期，删除
            del _chart_render_cache[cache_key]
    _chart_render_cache_misses += 1
    return None


def _store_chart_render(payload_str: str, stream: BytesIO, chart_type: str = "") -> None:
    """L1 内存缓存：存储已渲染的图表结果到内存

    V2.0 增强：存储时附带时间戳，支持 TTL 淘汰。
    P2 修复：cache key 拼接 chart_type 避免不同图表类型命中错误缓存。
    """
    now = time.time()
    cache_key = f"{chart_type}:{payload_str}"
    with _chart_render_cache_lock:
        # 淘汰过期条目
        expired_keys = [
            k
            for k, (_, _, ts) in _chart_render_cache.items()
            if now - ts >= _chart_render_cache_ttl
        ]
        for k in expired_keys:
            del _chart_render_cache[k]

        # 如果仍然超过最大大小，淘汰最旧（按插入顺序）
        if len(_chart_render_cache) >= _chart_render_cache_max:
            oldest_key = next(iter(_chart_render_cache))
            del _chart_render_cache[oldest_key]
        _chart_render_cache[cache_key] = (stream, len(stream.getvalue()), now)


def _render_section_charts(
    doc,
    section_detail,
    node_title: str,
    config: ExportConfig,
    _http_service_available: bool | None = None,
    parent_section_number: str = "",
):
    """V7.0 无降级图表渲染（仅 DB 有真实结构化 JSON 数据时才渲染）

    原则：
    1. 只渲染 section_detail 上存在真实 JSON 数据字段的图表类型（gantt_json 等）
    2. 不调用 _generate_local_mermaid_chart 伪造数据（无降级原则）
    3. 内容中已有内联图表标记（<<CHART:...>>）的类型由 PATH 2 处理，此处跳过
    4. 无真实数据 → 不渲染（宁缺毋假）
    """
    if not section_detail or not section_detail.content:
        return

    cached_types = _get_cached_inline_chart_types(section_detail)

    # V7.0 严格模式：只渲染有真实 DB 数据的类型
    rendered_count = 0
    for chart_type, (json_field, default_title) in _CHART_TYPE_MAP.items():
        chart_data = getattr(section_detail, json_field, None)
        if not chart_data:
            continue  # 无 DB 数据 → 跳过（无降级）
        if chart_type in cached_types:
            continue  # 内容已有内联标记 → PATH 2 处理
        _add_inline_chart(
            doc,
            chart_type,
            default_title,
            section_detail,
            config,
            _http_service_available=_http_service_available,
            parent_section_number=parent_section_number,
        )
        rendered_count += 1

    if rendered_count > 0:
        logger.info(
            "[V7.0 图表渲染] section=%s title=%s 渲染 %d 张 DB 绑定图表",
            getattr(section_detail, "id", "unknown"),
            getattr(section_detail, "title", "")[:30],
            rendered_count,
        )


def _batch_load_section_details(project_id: int, section_ids: list[int]) -> dict[int, object]:
    """批量加载章节详情，消除 N+1 查询

    Args:
        project_id: 项目 ID
        section_ids: 章节 ID 列表

    Returns:
        字典 {section_id: Section}，未找到的章节不出现在结果中
    """
    if not section_ids:
        return {}
    # 优先用 IN 子查询一次性取回（显式指定列，避免 SELECT * 带来的不必要开销）
    from ..models import SECTION_EXPORT_COLUMNS, Section

    placeholders = ",".join("?" for _ in section_ids)
    columns = SECTION_EXPORT_COLUMNS
    with get_db() as conn:
        rows = conn.execute(
            f"SELECT {columns} FROM sections WHERE project_id = ? AND id IN ({placeholders})",
            (project_id, *section_ids),
        ).fetchall()
    return {row["id"]: Section(**dict(row)) for row in rows}


def _collect_content_node_ids(tree: list) -> list[int]:
    """从目录树中收集所有「有正文」的节点 ID（按遍历顺序）

    BUG-FIX 2026-09-18（P1）：原实现只收集叶子节点，而非叶子章节（用户在
    父级目录上手写的正文）不在预加载集合里，叠写出端的 `not has_children`
    门后静默丢正文。现改为递归收集所有 has_content 的节点（含非叶子）。
    """
    content_ids: list[int] = []
    for node in tree:
        if node.has_content:
            content_ids.append(node.id)
        if node.children:
            content_ids.extend(_collect_content_node_ids(node.children))
    return content_ids


def _write_tree_content_with_progress(
    doc,
    tree: list,
    config: ExportConfig,
    project_id: int | None = None,
    parent_numbers: list[int] | None = None,
    on_progress: Callable | None = None,
    detail_index: dict[int, object] | None = None,
    _http_service_available: bool | None = None,
):
    """带进度追踪的树内容写入

    Args:
        detail_index: 章节详情字典（已批量预加载）。若提供则避免 N+1 查询。
        _http_service_available: BUG-E-02 修复：HTTP 服务可用性检查结果
    """
    # 修复：使用 HeadingNumberingGeneratorV2 进行正确编号跟踪
    numbering_tracker = HeadingNumberingGeneratorV2()

    # V1.0 增强：失败章节收集，用于重试机制
    failed_sections: list[dict] = []

    def _flatten_tree(node):
        yield node
        for child in node.children or []:
            yield from _flatten_tree(child)

    total_nodes = sum(1 for node in tree for _ in _flatten_tree(node))
    processed = [0]

    logger.info(
        "[DIAG-EXPORT] _write_tree_content_with_progress ENTER total_nodes=%d project_id=%d detail_index_keys=%d",
        total_nodes,
        project_id,
        len(detail_index) if detail_index else 0,
    )
    logger.info(
        "[DIAG-EXPORT] detail_index sample keys: %s", list((detail_index or {}).keys())[:10]
    )

    # ========== export.py 异步化增强：递归 → 显式栈迭代 ==========
    # BUG-O-03 延伸：标书目录层级一般 ≤8，理论上不会爆栈。
    # 但在极端场景（如用户手工创建 200+ 层嵌套目录时）递归版本会 RecursionError。
    # 改为显式栈迭代后，支持任意深度目录无栈溢出风险。
    # 关键：用 reversed 入栈 + pop()，保证前序遍历顺序与原递归一致。
    def _write_node_list(
        nodes: list,
        config: ExportConfig,
        project_id: int | None,
        detail_index: dict[int, object] | None,
        numbering_tracker: HeadingNumberingGeneratorV2,
        on_progress: Callable | None,
        processed_ref: list[int],
        total_nodes: int,
    ):
        if not nodes:
            return

        # 前序遍历栈：处理当前 node → push children（倒序） → 处理下一个
        # 倒序入栈保证 pop 出栈顺序 = 原递归 for 循环顺序
        stack: list[tuple[object, int | None]] = [
            (node, None) for node in reversed(nodes)
        ]

        while stack:
            node, current_parent_id = stack.pop()

            logger.info(
                "[DIAG-EXPORT] Node ENTER: id=%s level=%d has_children=%s processed=%d title=%s",
                node.id, node.level, bool(node.children), processed_ref[0], node.title[:30],
            )
            if processed_ref[0] == 0:
                logger.info(
                    "[DIAG-EXPORT] Processing first node: id=%s title=%s level=%d has_children=%s",
                    node.id, node.title[:40], node.level, bool(node.children),
                )
            # 业务过滤
            if _should_skip_section(node.title):
                # 跳过节点仍需跳过其子树（栈中已包含 children）
                continue

            numbering = numbering_tracker.update_counter(node.level, str(current_parent_id)) + "、"
            has_children = bool(node.children)

            try:
                _add_section_heading(doc, node.title, node.level, config, numbering=numbering)

                # BUG-FIX 2026-09-18（P1）：去掉 `not has_children` 门——用户在
                # 非叶子（父级）章节上手工编辑的正文此前被静默丢弃，而字数
                # 统计/文档检查按全量统计，导出成品与统计对不上。现凡有
                # 正文即写出（叶子/非叶子一视同仁）。
                if project_id:
                    if detail_index is not None:
                        section_detail = detail_index.get(node.id)
                    else:
                        section_detail = _get_section_detail(project_id, node.id)
                    if section_detail:
                        if section_detail.content:
                            logger.info(
                                "[DIAG-EXPORT] Before _add_parsed_content: node_id=%s content_len=%d",
                                node.id, len(section_detail.content),
                            )
                            _add_parsed_content(
                                doc, section_detail.content, config,
                                base_level=node.level, section_detail=section_detail,
                                project_id=project_id,
                                _http_service_available=_http_service_available,
                                parent_section_number=_section_number_to_arabic(numbering.rstrip("、")),
                            )
                            logger.info(
                                "[DIAG-EXPORT] After _add_parsed_content: node_id=%s", node.id
                            )
                        logger.info(
                            "[DIAG-EXPORT] Before _render_section_charts: node_id=%s", node.id
                        )
                        _render_section_charts(
                            doc, section_detail, node.title, config,
                            _http_service_available=_http_service_available,
                            parent_section_number=_section_number_to_arabic(numbering.rstrip("、")),
                        )
                        logger.info(
                            "[DIAG-EXPORT] After _render_section_charts: node_id=%s", node.id
                        )
            except Exception as e:
                logger.exception(
                    "章节渲染失败（已跳过并写入占位符）: node=%s, title=%s", node.id, node.title
                )
                failed_sections.append({
                    "node_id": node.id, "title": node.title, "level": node.level,
                    "error": str(e)[:200],
                    "parent_section_number": _section_number_to_arabic(numbering.rstrip("、")),
                })
                _ph = None
                try:
                    _ph = doc.add_paragraph()
                    _set_paragraph_indent_chars(_ph, first_line_chars=0)
                    _ph_run = _ph.add_run("（本小节内容渲染异常，请检查数据后重新导出）")
                    _apply_font(_ph_run, config.font_body, config.size_body)
                    _ph_run.font.color.rgb = RGBColor(180, 180, 180)
                except Exception:
                    logger.warning("写入失败占位符也失败，忽略: node=%s", node.id)
                # BUG-FIX 2026-09-14（中）：保留占位符元素引用，重试成功后可把
                # 内容搬回原位（见下方重试逻辑），而不是追加到文档末尾。
                failed_sections[-1]["placeholder"] = _ph

            # 进度更新 + 分批 GC
            if on_progress and total_nodes > 0:
                processed_ref[0] += 1
                progress = 0.2 + (processed_ref[0] / total_nodes) * 0.65
                on_progress("content", f"处理章节 {node.title}", progress)
                if processed_ref[0] % 50 == 0:
                    gc.collect()
                    if processed_ref[0] % 200 == 0:
                        logger.info(
                            "大文档导出: 已处理 %d/%d 章节，执行 GC 回收",
                            processed_ref[0], total_nodes,
                        )

            # push children（倒序，保证正序弹出）
            # 深度优先：当前 node 处理完 → children 紧跟处理 → 再处理下一个 sibling
            if has_children:
                for child in reversed(node.children):
                    stack.append((child, node.id))

    # 从根节点开始迭代
    _write_node_list(
        tree,
        config,
        project_id,
        detail_index,
        numbering_tracker,
        on_progress,
        processed,
        total_nodes,
    )

    # V1.0 增强：失败章节重试机制（最多重试 2 次）
    # BUG-FIX 2026-09-14（中）：原条件 `if failed_sections and on_progress` 使
    # 同步导出路径（无 on_progress）完全不重试；改为只看是否有失败章节。
    if failed_sections:
        max_retries = 2
        for retry_attempt in range(1, max_retries + 1):
            if not failed_sections:
                break
            retry_count = len(failed_sections)
            logger.info("重试第 %d 轮：%d 个失败章节", retry_attempt, retry_count)
            if on_progress:
                # BUG-FIX：重试阶段把主进度打回 0.0 会让前端进度条瞬间归零，
                # 用户以为导出重启了。正文此时已到 ~85%，保持在当前水位。
                on_progress(
                    "retry",
                    f"重试第 {retry_attempt} 轮（{retry_count} 个章节）",
                    0.85,
                )

            # 收集本轮重试失败的章节
            still_failed: list[dict] = []
            for fs in failed_sections:
                # BUG-FIX 2026-09-14（中）：python-docx 没有"在指定位置插入"的 API，
                # 原实现直接用 _add_parsed_content 把重试内容追加到**文档末尾**，
                # 导致恢复出来的章节跑到整份标书最后面（章节顺序错乱，属于硬性废标风险）。
                # 修复：先记录 body 快照长度，渲染完成后把新元素整体搬到占位符之前，
                # 再删除占位符；失败则回滚本轮新增元素，避免残留半截内容。
                _ph = fs.get("placeholder")
                _body = doc.element.body
                # BUG-FIX 2026-09-14（高）：body 末尾固定是 <w:sectPr>，
                # add_paragraph/add_table 都会插到它**前面**。用 len(list(body))
                # 当游标会多算一位，导致"重试内容的第一个元素"被漏掉、
                # 仍然留在文档末尾（=修复前的章节错乱问题换个形式复现）。
                _mark = _body_insertion_mark(_body) if _ph is not None else 0
                try:
                    # 重新获取章节详情（可能之前有临时IO错误）
                    section_detail = _get_section_detail(project_id, fs["node_id"])
                    if section_detail and section_detail.content:
                        _add_parsed_content(
                            doc,
                            section_detail.content,
                            config,
                            base_level=fs["level"],
                            section_detail=section_detail,
                            project_id=project_id,
                            _http_service_available=_http_service_available,
                            parent_section_number=fs.get("parent_section_number", ""),
                        )
                        _render_section_charts(
                            doc,
                            section_detail,
                            fs["title"],
                            config,
                            _http_service_available=_http_service_available,
                            parent_section_number=fs.get("parent_section_number", ""),
                        )
                    if _ph is not None:
                        _move_body_elements_before(list(_body)[_mark:], _ph._p)
                    logger.info("重试成功: node=%s, title=%s", fs["node_id"], fs["title"])
                except Exception as e:
                    logger.warning(
                        "重试仍失败: node=%s, title=%s, error=%s",
                        fs["node_id"],
                        fs["title"],
                        str(e)[:100],
                    )
                    if _ph is not None:
                        _remove_body_elements(list(_body)[_mark:])
                    still_failed.append(fs)

            failed_sections = still_failed
            if not failed_sections:
                logger.info("重试成功，所有章节已恢复")
                if on_progress:  # BUG-FIX：补齐守卫，同步路径无 on_progress
                    on_progress("retry_done", "重试完成，所有章节已恢复", 0.9)
                break

        if failed_sections:
            logger.warning("仍有 %d 个章节重试失败，已写入占位符", len(failed_sections))
            if on_progress:
                on_progress("retry_partial", f"仍有 {len(failed_sections)} 个章节重试失败", 0.9)


def _body_insertion_mark(body) -> int:
    """返回 body 中「新增内容起始下标」。

    python-docx 的 body 末尾固定是 <w:sectPr>，所有 add_paragraph / add_table
    都插到它前面。因此用 len(list(body)) 当游标会**偏后一位**，切片时会漏掉
    第一个新增元素（它会继续留在文档末尾）。这里把尾部 sectPr 排除掉。
    """
    n = len(list(body))
    if n > 0 and _is_sect_pr(list(body)[n - 1]):
        return n - 1
    return n


def _is_sect_pr(el) -> bool:
    """判断是否为 body 的 sectPr（节属性）元素。

    必须在"移动新增元素"时排除它：sectPr 永远位于 body 末尾，
    一旦被搬走会破坏整篇文档的页面设置（页边距/纸张/页眉页脚）。
    """
    tag = getattr(el, "tag", "")
    return isinstance(tag, str) and tag.endswith("}sectPr")


def _move_body_elements_before(elements: list, anchor) -> None:
    """把一组 body 子元素按顺序移动到 anchor 元素之前，并删除 anchor。

    用于失败章节重试：把重渲染出来的内容搬回占位符原来的位置，
    而不是像 python-docx 默认行为那样追加到文档末尾。
    """
    for el in elements:
        if _is_sect_pr(el):
            continue
        anchor.addprevious(el)
    parent = anchor.getparent()
    if parent is not None:
        parent.remove(anchor)


def _remove_body_elements(elements: list) -> None:
    """从 body 中移除一组子元素（重试失败时回滚半截内容）"""
    for el in elements:
        if _is_sect_pr(el):
            continue
        parent = el.getparent()
        if parent is not None:
            parent.remove(el)


_GET_SECTION_CACHE: dict[int, dict[int, tuple[dict | None, float]]] = {}
_SECTION_CACHE_TTL = 60.0  # 缓存有效期 60 秒
_SECTION_CACHE_MAX_SIZE = 1000  # 缓存最大条目数
# 关键修复（P1-4）：用 RLock 保护 _GET_SECTION_CACHE 并发访问
# build_docx 在 SSE 导出场景下运行在独立线程，且前端可能同时触发多个
# 章节查询 / 文档导出，导致 _GET_SECTION_CACHE 出现 RuntimeError:
# dictionary changed size during iteration 或并发 dict 写入崩溃。
_SECTION_CACHE_LOCK = threading.RLock()
# 关键修复（P1-4）：清理计数器用整数属性 + 锁保护
# 原代码用函数属性 _get_section_detail_with_cleanup.call_count += 1，
# 在多线程并发下属于非原子操作，会出现计数丢失。
_SECTION_CACHE_CLEANUP_COUNTER = 0
_SECTION_CACHE_CLEANUP_INTERVAL = 100


def _cleanup_section_cache():
    """清理过期的章节详情缓存，防止内存泄漏

    清理策略：
    1. 移除所有过期条目（超过TTL）
    2. 如果缓存超过最大大小，移除最旧的条目

    关键修复（P1-4）：整体放入 RLock 内，避免与并发的 get/set 发生竞态
    """
    now = time.time()

    with _SECTION_CACHE_LOCK:
        expired_projects = []

        for project_id, section_cache in list(_GET_SECTION_CACHE.items()):
            # 清理该项目的过期条目
            to_remove = []
            for section_id, (data, timestamp) in section_cache.items():
                if now - timestamp >= _SECTION_CACHE_TTL:
                    to_remove.append(section_id)

            for section_id in to_remove:
                section_cache.pop(section_id, None)

            # 如果该项目没有任何缓存条目，标记项目级清理
            if not section_cache:
                expired_projects.append(project_id)

        # 移除空的项目缓存
        for project_id in expired_projects:
            _GET_SECTION_CACHE.pop(project_id, None)

        # 如果缓存总体积过大，清理最早的项目缓存
        if len(_GET_SECTION_CACHE) > 100:  # 超过100个项目
            sorted_projects = sorted(
                _GET_SECTION_CACHE.items(),
                key=lambda x: min(ts for _, ts in x[1].values()) if x[1] else 0,
            )
            # 保留最新的50个项目
            for project_id, _ in sorted_projects[:-50]:
                _GET_SECTION_CACHE.pop(project_id, None)

        # 检查单个缓存是否超过最大条目数
        for project_id, section_cache in list(_GET_SECTION_CACHE.items()):
            if len(section_cache) > _SECTION_CACHE_MAX_SIZE:
                sorted_items = sorted(
                    section_cache.items(),
                    key=lambda x: x[1][1],  # 按时间戳排序
                )
                for section_id, _ in sorted_items[:-_SECTION_CACHE_MAX_SIZE]:
                    section_cache.pop(section_id, None)


def _get_section_detail_with_cleanup(project_id: int, section_id: int):
    """带自动清理的章节详情获取"""
    global _SECTION_CACHE_CLEANUP_COUNTER

    # 关键修复（P1-4）：计数 + 周期清理都在锁内完成
    with _SECTION_CACHE_LOCK:
        _SECTION_CACHE_CLEANUP_COUNTER += 1
        should_cleanup = _SECTION_CACHE_CLEANUP_COUNTER % _SECTION_CACHE_CLEANUP_INTERVAL == 0

    if should_cleanup:
        # 周期清理放锁外，避免长锁阻塞查询
        _cleanup_section_cache()

    return _get_section_detail_impl(project_id, section_id)


def _get_section_detail_impl(project_id: int, section_id: int):
    """获取章节详情的实际实现

    关键修复（P1-4）：所有对 _GET_SECTION_CACHE 的访问都在 RLock 内完成，
    避免并发场景下的 dict 修改崩溃和脏读。
    """
    now = time.time()

    with _SECTION_CACHE_LOCK:
        project_cache = _GET_SECTION_CACHE.get(project_id)
        if project_cache is not None and section_id in project_cache:
            cached, ts = project_cache[section_id]
            if now - ts < _SECTION_CACHE_TTL:
                return cached

    # 缓存未命中：走数据库查询（放锁外，避免长锁）
    from ..models import Section

    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM sections WHERE id = ? AND project_id = ?",
            (section_id, project_id),
        ).fetchone()
        result = Section(**dict(row)) if row else None

    # 写入缓存
    with _SECTION_CACHE_LOCK:
        project_cache = _GET_SECTION_CACHE.get(project_id)
        if project_cache is None:
            project_cache = {}
            _GET_SECTION_CACHE[project_id] = project_cache
        project_cache[section_id] = (result, now)

    return result


def _get_section_detail(project_id: int, section_id: int):
    """获取章节详情（带缓存和自动清理）"""
    return _get_section_detail_with_cleanup(project_id, section_id)


def _clear_section_cache_for_project(project_id: int) -> None:
    """关键修复（P1-4）：导出新文档前清空指定项目的缓存

    原代码在 build_docx 中直接 _GET_SECTION_CACHE.clear()，会清空所有项目，
    影响其他并发导出的请求。改为按项目清空，作用域更精确。
    """
    with _SECTION_CACHE_LOCK:
        _GET_SECTION_CACHE.pop(project_id, None)


def get_latest_export(project_id: int, *, readonly: bool = False) -> str | None:
    """获取指定项目的最新导出文件路径

    PERF（2026-10-06）：透传 readonly，默认 False 保持既有口径（向后兼容）。
    """
    info = get_latest_export_info(project_id, readonly=readonly)
    return info["file_path"] if info else None


def get_latest_export_id(project_id: int, *, readonly: bool = False) -> int | None:
    """获取指定项目的最新导出记录ID"""
    info = get_latest_export_info(project_id, readonly=readonly)
    return info["id"] if info else None


def get_latest_export_info(project_id: int, *, readonly: bool = False) -> dict | None:
    """一次查询获取最新导出的 id + file_path + filename，消除两次独立 DB 往返

    ENHANCE 2026-10-04（T2）：只取 status='completed' —— 失败行（file_path=''）
    插入后不得污染 latest，否则 /latest 会拿空路径做下载。

    PERF（2026-10-06）：纯读查询新增 `readonly` 开关（默认 False = 保持既有写锁
    连接口径，**向后兼容**，既有调用方无需改动）。只读端点（`/export/latest`）
    传 readonly=True 走共享读连接，避免在高并发导出期与写事务争用全局写锁。
    """
    with get_db(readonly=readonly) as conn:
        row = conn.execute(
            "SELECT id, file_path, filename FROM exports "
            "WHERE project_id = ? AND status = 'completed' ORDER BY created_at DESC LIMIT 1",
            (project_id,),
        ).fetchone()
    return dict(row) if row else None


def list_exports(project_id: int, *, readonly: bool = False) -> list[dict]:
    """列出项目的导出历史记录

    PERF（2026-10-06）：同上，纯读场景可传 readonly=True 走读连接
    （默认 False 保持既有口径，向后兼容）。
    """
    with get_db(readonly=readonly) as conn:
        rows = conn.execute(
            """SELECT id, filename, file_path, file_size, status, config_json, created_at
               FROM exports WHERE project_id = ? ORDER BY created_at DESC""",
            (project_id,),
        ).fetchall()
    # 将绝对路径转换为相对路径，便于前端构造下载 URL
    base_dir = PROJECTS_DATA_DIR.resolve()
    result = []
    for row in rows:
        d = dict(row)
        fp = d.get("file_path", "")
        if fp:
            try:
                actual_path = Path(fp).resolve()
                if actual_path.is_relative_to(base_dir):
                    d["file_path"] = str(actual_path.relative_to(base_dir))
                else:
                    d["file_path"] = Path(fp).name
            except Exception:
                d["file_path"] = Path(fp).name
        result.append(d)
    return result


# 启动内联标记缓存后台清理线程（BUG-E-05 修复）
_start_inline_marker_cache_cleanup()


# ========== 缓存监控统计（V3.0 新增）==========


def get_export_cache_stats() -> dict:
    """获取导出模块所有缓存实例的统计信息"""
    # 1. 内联图表标记缓存
    total = _inline_marker_cache_hits + _inline_marker_cache_misses
    inline_stats = {
        "name": "inline_marker_cache",
        "type": "memory",
        "hits": _inline_marker_cache_hits,
        "misses": _inline_marker_cache_misses,
        "hit_rate": f"{round(_inline_marker_cache_hits / total * 100, 1)}%"
        if total > 0
        else "0.0%",
        "size": len(_inline_marker_cache),
        "max_size": _inline_marker_cache_max,
        "ttl_seconds": _inline_marker_cache_ttl,
    }

    # 2. 图表渲染内存缓存（L1） —— V3.0 新增命中率统计
    total_render = _chart_render_cache_hits + _chart_render_cache_misses
    render_stats = {
        "name": "chart_render_cache",
        "type": "memory_l1",
        "hits": _chart_render_cache_hits,
        "misses": _chart_render_cache_misses,
        "hit_rate": f"{round(_chart_render_cache_hits / total_render * 100, 1)}%"
        if total_render > 0
        else "0.0%",
        "size": len(_chart_render_cache),
        "max_size": _chart_render_cache_max,
        "ttl_seconds": _chart_render_cache_ttl,
    }

    # 3. 图表渲染磁盘缓存（ChartCache）
    chart_stats = _chart_cache.get_stats()
    chart_stats["name"] = "chart_cache"
    chart_stats["type"] = "file"

    # 4. 导出结果缓存
    total2 = _EXPORT_CACHE_HITS + _EXPORT_CACHE_MISSES
    export_stats = {
        "name": "export_result_cache",
        "type": "file_index",
        "hits": _EXPORT_CACHE_HITS,
        "misses": _EXPORT_CACHE_MISSES,
        "hit_rate": f"{round(_EXPORT_CACHE_HITS / total2 * 100, 1)}%" if total2 > 0 else "0.0%",
        "size": len(_EXPORT_CACHE),
        "max_size": _EXPORT_CACHE_MAX_ENTRIES,
    }

    return {
        "caches": [inline_stats, render_stats, chart_stats, export_stats],
    }


# =====================
# Export API Compatibility Wrapper
# =====================
def export_to_docx(project_id: int, config: dict | None = None) -> Path:
    """导出DOCX（用于Celery任务等异步场景）

    Args:
        project_id: 项目ID
        config: 导出配置字典或ExportConfig对象

    Returns:
        生成的DOCX文件路径
    """
    from ..models import ExportConfig as ExportConfigModel

    # 转换config参数
    if isinstance(config, dict):
        config_model = ExportConfigModel(**config)
    elif config is None:
        config_model = ExportConfigModel()
    else:
        config_model = config

    # 调用主构建函数
    output_path, _ = build_docx(project_id, config=config_model)
    return Path(output_path)


def export_to_docx_recorded(project_id: int, config: dict | None = None) -> tuple[Path, int | None]:
    """导出 DOCX 并一并返回导出记录 ID。

    BUG-FIX 2026-09-18（高）：`export_to_docx` 只回传路径，把 `build_docx` 已经
    拿到的 `export_id` 丢掉了。Celery 任务 `tasks.export.generate_docx` 因此
    不得不**自己再 INSERT 一条 exports**，于是每次异步导出产生两条记录，
    且第二条是残缺的：

      - `config_json` 硬编码成 `'{}'` —— 用户选的版式/主题/页边距全被丢弃；
      - `filename` 硬编码成 `project_{id}_{时间戳}.docx`，与真实落盘文件名
        （`{项目名}_{时间戳}.docx`）不一致 —— 导出列表里显示的名字对不上文件；
      - `created_at` 用本地时间，而 `build_docx` 内部用 UTC，时间口径不一致。

    前端 `get_latest_export*` 取 id 最大的那条 → 拿到的正是这条残缺记录。
    这里把 export_id 透出来，调用方无需再自己记账。
    """
    from ..models import ExportConfig as ExportConfigModel

    if isinstance(config, dict):
        config_model = ExportConfigModel(**config)
    elif config is None:
        config_model = ExportConfigModel()
    else:
        config_model = config

    output_path, export_id = build_docx(project_id, config=config_model)
    return Path(output_path), export_id


# =====================
# 模块初始化（所有函数定义完成后执行）
# =====================
_cleanup_orphaned_export_files()
