"""图表管线模块（2026-10-12 从 export.py 拆出）

职责：
  1. 图表类型推断（_CHART_TYPE_MAP, _infer_chart_type_from_text, _afc_enabled）
  2. 本地兜底 Mermaid 模板（_generate_local_mermaid_chart）
  3. 通用格式化工具（_heading_font_for, _heading_size_for, _apply_font, _nested_list_left_chars）
  4. 内联图表标记检测与缓存（_detect_inline_chart_*, _get_cached_inline_chart_*）
  5. 图表渲染管线（_add_inline_chart, _add_diagram_picture, _render_section_charts）
  6. 渲染线程池管理（_get_render_executor, _preload_chart_images_parallel）

从 export.py 拆出后，export.py 顶部加 `from .export_chart_pipeline import *` re-export，
所有既有 `from app.services.export import _add_inline_chart` 调用方无需改动。

循环 import 说明：
  - 本模块纯数据（Block 1）+ 纯推断（Block 2）+ 通用格式化工具（Block 3）+ 检测层（Block 4）
    全部零循环依赖——要么纯函数，要么只依赖 export_cache.py（独立加载）或 docx 库。
  - 渲染层函数（Block 5）会调用 Block 1-4 本模块函数，也会调用 export_cache.py 的
    re-exported 符号——全部是已经建立好的 import 链路，无循环。
  - export.py 顶部的 re-export 放在 export_cache import 之后、其余代码之前，
    此时 export_cache 已加载完毕（它无 export 反向依赖），不会死锁。
"""
from __future__ import annotations

import contextvars  # HARDENING 2026-10-11（遗留·图号计数器会话化）
import json
import logging
import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING

from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Pt

if TYPE_CHECKING:
    from .export import ExportConfig  # 仅类型提示，不触发循环加载

logger = logging.getLogger(__name__)


# ============================================================
# 统一图表类型映射（Block 1: 纯数据）
# ============================================================

#: 中央化图表类型 → JSON 字段名 + 默认标题（消除 _add_inline_chart 和
#: _render_section_charts 的重复定义）
_CHART_TYPE_MAP = {
    "gantt": ("gantt_json", "施工进度计划"),
    "flowchart": ("flowchart_json", "施工流程图"),
    "architecture": ("architecture_json", "组织架构图"),
    "labor": ("labor_json", "劳动力配置计划"),
    "comparison": ("comparison_json", "对比图"),
    "layout": ("layout_json", "总平面布置图"),
    "timeline": ("timeline_json", "关键里程碑时间线"),
}

# FALLBACK MERMAID GENERATOR (NO AGNES API REQUIRED)
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
# 默认关闭（"0"）以保留现有行为；设为 "1" 开启。
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
        # Gantt fallback 日期生成：按 YYYY-MM-DD 格式保证月份/日期始终两位数（BUG-FIX 2026-10-11）
        # 原实现 f"2025-0{(i+1)*2}" 在 i>=4 时产生 "2025-010"（无效日期），
        # 额外的 len(start)>9 修补分支逻辑混乱。现统一用零填充格式化。
        _base_year = 2025
        _base_month = 1
        for i, task in enumerate(tasks):
            # 每个 section 跨 2 个月（起始月 / 结束月），月份溢出到 13+ 时按 12 个月循环
            start_month = ((_base_month + i * 2 - 1) % 12) + 1
            end_month = ((start_month) % 12) + 1  # 下一个月（12月→1月时年份 +1）
            start_year = _base_year + ((_base_month + i * 2 - 1) // 12)
            end_year = start_year + (1 if start_month == 12 else 0)
            start = f"{start_year}-{start_month:02d}-01"
            end = f"{end_year}-{end_month:02d}-01"
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


# ============================================================
# Block 3: 通用格式化工具（纯函数，export.py N 处消费）
# ============================================================
# 这些本来留在 export.py 因为它们被表格/列表/图片等**所有**导出组件使用。
# 移进 export_chart_pipeline.py 后通过 re-export 保持透明，消费者零改动。
# 选在这里集中放是因为图表渲染层（Block 5）依赖它们——如果放 export_utils.py
# 反而要额外新建模块。零循环 import 风险：这四个函数都是纯函数，只依赖
# ExportConfig（类型注解）或 docx 库（第三方）。


def _heading_font_for(config: "ExportConfig", level: int) -> str:
    """按层级取标题字体；heading_fonts 未提供或长度不足时回退 font_title。"""
    fonts = getattr(config, "heading_fonts", None) or []
    index = level - 1
    if 0 <= index < len(fonts) and str(fonts[index]).strip():
        return str(fonts[index]).strip()
    return config.font_title


def _heading_size_for(config: "ExportConfig", level: int) -> float:
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


def _apply_font(run, font_name: str, font_size: float, bold: bool = False):
    """给 docx Run 对象应用字体（字号 + 粗体 + 中英文字体统一）。

    纯函数：不依赖 export.py 全局状态，仅操作传入的 python-docx run 对象。
    """
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


def _nested_list_left_chars(level) -> int:
    """列表嵌套缩进增量（字符单位，每层 2 字符 = 200 单位，最多 4 层）。

    HARDENING 2026-10-11（遗留·列表解析解耦）：E-12 的缩进算法从
    `_add_parsed_content` 内联逻辑提取为纯函数，使嵌套列表缩进可被单元测试
    直接验证（不再依赖 python-docx / 文档上下文）。
    """
    try:
        level_int = int(level)
    except (TypeError, ValueError):  # noqa: BLE001
        level_int = 0
    return 200 * min(max(level_int, 0), 4)


# ============================================================
# Block 4: 内联图表标记检测层（无循环依赖）
# ============================================================
# 依赖关系：
#   - _detect_inline_chart_markers → _CHART_TYPE_MAP（本模块）+ .content_cleaning（独立）
#   - _detect_inline_chart_types → 调 _detect_inline_chart_markers（同层）
#   - _get_cached_inline_chart_markers → export_cache.py 的 _inline_marker_cache_*（re-export）
#   - _get_cached_inline_chart_types → 调 _get_cached_inline_chart_markers（同层）


def _detect_inline_chart_types(content: str) -> set[str]:
    """一次性扫描内容，返回所有已内联标记的图表类型集合。

    替代 _has_inline_marker 的重复扫描，每次调用只扫描内容一次，通过 LRU
    缓存按 section_detail id 复用结果。

    CHART-MARKER-UNIFY 2026-09-16：解析口径统一收敛到
    `content_cleaning.iter_chart_markers` + `resolve_chart_marker`（与 parse_content
    完全同源）。
    """
    return _detect_inline_chart_markers(content)[0]


def _detect_inline_chart_markers(content: str) -> tuple[set[str], set[str]]:
    """返回 (内联图表类型全集, 「配图位置提示」子集)。

    - 全集：内容里出现了标记的类型 → 导出时由**内联路径**渲染，章节末尾不再补图，
      避免同一张图渲染两次。
    - 提示子集：标记内**没有真实数据**的类型，其数据来自章节 DB 绑定字段。
      这类图表的渲染载荷与 `_add_inline_chart` 内联渲染时**完全一致**
      （缓存键已剔除图号），因此可以并行走预渲染。
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
            continue  # 标记内自带真实数据 → 内联渲染使用标记内数据
        hint_types.add(resolved[0])
    # ENHANCE 2026-10-04（D2）：```mermaid / ```chart-json 围栏块同样计入「已内联类型」。
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

    V2.0 增强：TTL 淘汰 + 最大容量限制。
    V3.0 增强：命中率统计。
    """
    from .export_cache import (
        _inline_marker_cache,
        _inline_marker_cache_hits,
        _inline_marker_cache_lock,
        _inline_marker_cache_max,
        _inline_marker_cache_misses,
        _inline_marker_cache_ttl,
    )

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
            del _inline_marker_cache[section_id]
    _inline_marker_cache_misses += 1
    content = getattr(section_detail, "content", "") or ""
    all_types, hint_types = _detect_inline_chart_markers(content)
    with _inline_marker_cache_lock:
        if len(_inline_marker_cache) >= _inline_marker_cache_max:
            sorted_items = sorted(_inline_marker_cache.items(), key=lambda x: x[1][-1])
            for key, _ in sorted_items[: len(sorted_items) // 2]:
                del _inline_marker_cache[key]
        _inline_marker_cache[section_id] = (all_types, hint_types, time.time())
    return all_types, hint_types


def _get_cached_inline_chart_types(section_detail) -> set[str]:
    """内联图表类型全集（兼容旧调用方）"""
    return _get_cached_inline_chart_markers(section_detail)[0]


__all__ = [
    # Block 1: 常量
    "_CHART_TYPE_MAP",
    "_CHART_FALLBACK_KEYWORDS",
    "AUTO_FALLBACK_CHARTS",
    # Block 2: 推断
    "_afc_enabled",
    "_generate_local_mermaid_chart",
    "_infer_chart_type_from_text",
    # Block 3: 通用格式化工具
    "_heading_font_for",
    "_heading_size_for",
    "_apply_font",
    "_nested_list_left_chars",
    # Block 4: 检测层
    "_detect_inline_chart_types",
    "_detect_inline_chart_markers",
    "_get_cached_inline_chart_markers",
    "_get_cached_inline_chart_types",
]
