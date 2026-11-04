"""HTML 导出格式（2026-10-12 新增）

与 DOCX 导出**并立**的第二交付格式：复用同一套内容组装链（章节树 + parse_content +
resolve_export_config），但消费端换成简洁 HTML 渲染器。

GAP-1（2026-11-02）：chart_marker 现消费 ChartCache 持久渲染件——命中
（此前 DOCX/PDF 导出或预渲染已产生 PNG）时以 base64 <img> 内嵌 + 图题
（与 DOCX 同源图号计数器）；未命中时保持占位符，**绝不在 HTML 链路内
触发实时渲染**（性能与失败面控制），也无降级原则（不伪造数据）。
无持久件时仍提示「真实图表需 DOCX/PDF 格式渲染」。

消费链：
  sections.build_tree → sections.list_sections → 逐节 parse_content → 逐块 HTML 渲染
  chart_marker → _chart_cache.get（复用 export.py 同一缓存键口径） → base64/占位符

与 DOCX 并行，不替代 DOCX/PDF；前端可在导出配置页选择「导出 HTML」。
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import logging
import os
import time
import uuid
from pathlib import Path

from ..config import PROJECTS_DATA_DIR
from ..db import get_db
from ..models import ExportConfig
from .degradation import log_degradation
from .ai.heading_standard import HeadingNumberingGeneratorV2
from .content_cleaning import extract_mermaid_title, parse_content
from .export_cache import (
    _chart_cache,
    _claim_chart_title,
    _fig_counter_ctx_var,
    _next_figure_number,
    _reset_chart_title_registry,
    _reset_figure_counter,
    _strip_fig_no_for_cache,
    _used_chart_titles_ctx_var,
)
from .export_chart_pipeline import _CHART_TYPE_MAP, _clean_lead_in_title
from .export_presets import resolve_export_config
from .sections import build_tree


logger = logging.getLogger(__name__)



# ========== HTML 样式 ==========
# 版式预设决定字体/字号/色值；主题预设决定色板。
# HTML 导出消费这两者，生成内联 <style> 块（避免外部 CSS 文件依赖）。

_DEFAULT_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
{styles}
</style>
</head>
<body class="tender-body">
{content}
</body>
</html>"""


def _build_html_styles(cfg: ExportConfig) -> str:
    """从 ExportConfig 生成内联 CSS 样式（字号/字体/色板/行距）。"""
    body_font = cfg.font_body or "宋体"
    body_size = cfg.size_body or 12.0
    heading_color = cfg.heading_color or "#1a1a1a"

    h1_size = cfg.heading_sizes[0] if cfg.heading_sizes else cfg.size_title_1
    h2_size = cfg.heading_sizes[1] if len(cfg.heading_sizes) > 1 else cfg.size_title_2
    h3_size = cfg.heading_sizes[2] if len(cfg.heading_sizes) > 2 else cfg.size_title_3

    body_indent = (cfg.body_first_line_indent_chars or 2.0) * body_size

    styles = [
        f".tender-body {{ font-family: '{body_font}', 'SimSun', serif; "
        f"font-size: {body_size}pt; line-height: {(cfg.line_spacing or 1.5)}; "
        f"color: #222; max-width: 900px; margin: 0 auto; padding: 24px; }}",
        f"h1 {{ font-size: {h1_size}pt; color: {heading_color}; "
        f"font-weight: bold; border-bottom: 2px solid {heading_color}; "
        f"margin-top: 24px; padding-bottom: 6px; }}",
        f"h2 {{ font-size: {h2_size}pt; color: {heading_color}; "
        f"font-weight: bold; margin-top: 20px; }}",
        f"h3 {{ font-size: {h3_size}pt; color: {heading_color}; "
        f"font-weight: bold; margin-top: 16px; }}",
        f"p {{ text-indent: {body_indent}pt; margin: 6px 0; }}",
        ".chart-placeholder { border: 1px dashed #aaa; padding: 12px; text-align: center; "
        "color: #666; margin: 12px 0; background: #fafafa; }",
        ".chart-placeholder figcaption { font-style: italic; margin-top: 4px; }",
        # GAP-1（2026-11-02）：base64 内嵌图表样式（与占位符区分）
        "figure.chart-figure { margin: 12px 0; text-align: center; }",
        "figure.chart-figure img { max-width: 100%; height: auto; }",
        "figure.chart-figure figcaption { font-style: italic; color: #444; margin-top: 4px; }",
        "hr { border: none; border-top: 1px solid #ddd; margin: 16px 0; }",
    ]
    return "\n".join(styles)


# ========== GAP-1：chart_marker → ChartCache 持久渲染件 ==========

def _figure_separator(config) -> str:
    """图题分隔符口径与 export._figure_caption 完全一致：
    仅接受 " " / "：" / ":" / ""，其余（含 None）回退默认空格。"""
    sep = getattr(config, "figure_no_separator", " ") if config is not None else " "
    if sep is None:
        sep = " "
    if str(sep) not in (" ", "：", ":", ""):
        sep = " "
    return str(sep)


def _resolve_chart_source(
    chart_type: str, chart_desc: str, section: dict, caption_hint: str = ""
):
    """定位图表数据源并归一为 dict，口径同 DOCX 侧 _add_inline_chart：

    优先级：内联 JSON 数据（chart_desc 解析）> 章节绑定字段（section.*_json）。
    返回 (source_dict, ctype, title_text)；无有效数据源返回 None（无降级原则）。
    """
    inline_data = None
    if chart_desc and chart_desc.strip().startswith("{"):
        try:
            parsed = json.loads(chart_desc)
            if isinstance(parsed, dict) and "type" in parsed:
                inline_data = parsed
                chart_type = str(parsed.get("type", chart_type) or chart_type).lower()
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass

    ctype = (chart_type or "").lower().strip()
    default_title = ""
    json_field = ""
    for t, (jf, dt) in _CHART_TYPE_MAP.items():
        if ctype == t:
            json_field, default_title = jf, dt
            break
    if not json_field:
        return None

    source = inline_data
    if source is None and section:
        source = section.get(json_field)

    source_obj = None
    if source:
        if isinstance(source, str):
            try:
                source_obj = json.loads(source)
            except (json.JSONDecodeError, TypeError):
                source_obj = None
        elif isinstance(source, dict):
            source_obj = source
    if not isinstance(source_obj, dict):
        return None

    # 图题四级优先级（同 DOCX V7.0 / ENHANCE 2026-10-04/10-17）：
    #   载荷 title/caption > Mermaid 代码内 title 行 > 引导语（caption_hint /
    #   非 JSON chart_desc，经 _clean_lead_in_title 剥框架词）> 类型通用名
    _mermaid_title = ""
    _code_field = source_obj.get("code")
    if isinstance(_code_field, str) and _code_field.strip() and not _code_field.lstrip().startswith("{"):
        # 仅纯 Mermaid 语法载荷提 title；JSON 内嵌 code 不交叉提取
        _mermaid_title = extract_mermaid_title(_code_field)
    _lead_in = (
        chart_desc
        if isinstance(chart_desc, str) and chart_desc and not chart_desc.startswith("{")
        else caption_hint
    )
    _lead_in = _clean_lead_in_title(_lead_in) if _lead_in else _lead_in
    title_text = (
        source_obj.get("title")
        or source_obj.get("caption")
        or _mermaid_title
        or _lead_in
        or default_title
        or ctype
    )
    title_text = str(title_text).strip()[:24]
    return source_obj, ctype, title_text


def _render_chart_marker_html(
    text: str, section: dict, config, *, project_id, chapter_number: str
) -> str:
    """chart_marker block → base64 内嵌图（缓存命中）或占位符（未命中）。

    缓存键与 DOCX 侧完全同构：_strip_fig_no_for_cache(payload)，
    payload 即解析后的图表数据 dict（fig_no 不进键，见 2026-09-14 修复注释）。
    """
    marker = None
    try:
        marker = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        log_degradation(
            "export",
            trigger="chart_marker JSON 解析失败",
            action="按空 marker 渲染为图表占位符",
            reason=str(exc)[:120],
            logger_obj=logger,
            section_id=str((section or {}).get("id", "")),
        )
    if not isinstance(marker, dict):
        marker = {}

    resolved = _resolve_chart_source(
        str(marker.get("chart_type", "") or ""),
        str(marker.get("description", "") or ""),
        section or {},
        caption_hint=str(marker.get("caption_hint", "") or ""),
    )

    placeholder_title = html.escape(
        (resolved[2] if resolved else "") or str(text).strip()[:40] or "图表占位符",
        quote=False,
    )

    if resolved is None:
        # 无有效数据源：与 DOCX 同口径静默跳过 → 占位符（不占图号、不伪造）
        return (
            f'<div class="chart-placeholder">'
            f"<div>[图表占位符：{placeholder_title}]</div>"
            f"<figcaption>真实图表需 DOCX/PDF 格式渲染</figcaption>"
            f"</div>\n"
        )

    source_obj, ctype, title_text = resolved
    # 仅查持久渲染件（查后即弃，绝不在 HTML 链路触发实时渲染）
    try:
        cache_key = _strip_fig_no_for_cache(source_obj)
        cached_path: "Path | None" = _chart_cache.get(cache_key, chart_type=ctype)
        img_bytes = cached_path.read_bytes() if cached_path else None
    except Exception as e:
        logger.debug("[HTML导出·GAP-1] 缓存件读取失败（按未命中处理）: %s", e)
        img_bytes = None

    if not img_bytes:
        return (
            f'<div class="chart-placeholder">'
            f"<div>[图表占位符：{placeholder_title}]</div>"
            f"<figcaption>真实图表需 DOCX/PDF 格式渲染</figcaption>"
            f"</div>\n"
        )

    # 缓存命中才消耗图号与图题登记（占位符不占号，与 DOCX
    # 「渲染失败不占号」的图号虚跳修复同口径）
    if not _claim_chart_title(chapter_number, title_text, project_id):
        return ""  # 同章重复图题：与 DOCX 一致仅保留首图
    fig_no = _next_figure_number(chapter_number, project_id)
    sep = _figure_separator(config)
    caption = f"{fig_no}{sep}{title_text}" if fig_no and title_text else (title_text or fig_no)
    b64 = base64.b64encode(img_bytes).decode("ascii")
    alt = html.escape(caption or ctype, quote=True)
    cap_html = html.escape(caption, quote=False)
    return (
        f'<figure class="chart-figure">'
        f'<img src="data:image/png;base64,{b64}" alt="{alt}">'
        f"<figcaption>{cap_html}</figcaption>"
        f"</figure>\n"
    )



# ========== 遗留项 1（2026-11-04）：LaTeX 公式 → MathML 渲染（HTML 通道） ==========

def _render_paragraph_with_math_html(text: str) -> str | None:
    """公式感知的 HTML 段落渲染；无公式候选时返回 None（调用方走历史路径）。

    - 文本段照常 html.escape（XSS 面不扩大）；
    - 公式段输出 latex2mathml 的 MathML（转换器产物为受控 XML，且经
      math_omml.latex_to_mathml_html 的 <script>/事件属性注入守卫）；
    - 个别公式转换失败 → 该片段按原文（含定界符）escape 后回填，零内容丢失。
    """
    from .math_omml import latex_to_mathml_html, split_math_segments

    try:
        segments = split_math_segments(text)
    except Exception:
        return None
    if len(segments) <= 1 and (not segments or segments[0][0] == "text"):
        return None

    parts: list[str] = []
    for kind, payload, is_display in segments:
        if kind == "text":
            parts.append(html.escape(payload, quote=False))
            continue
        mathml = latex_to_mathml_html(payload)
        if mathml is None:
            delim = "$$" if is_display else "$"
            parts.append(html.escape(f"{delim}{payload}{delim}", quote=False))
        else:
            parts.append(mathml)
    return f"<p>{''.join(parts)}</p>\n"


# ========== DocumentBlock → HTML 渲染 ==========

def _render_block_to_html(
    block,
    base_level: int = 0,
    heading_number: str = "",
    *,
    section: dict | None = None,
    config=None,
    project_id: int | None = None,
    chapter_number: str = "",
) -> str:
    """把单个 DocumentBlock 转成 HTML 片段。

    BlockType 覆盖：heading / paragraph / chart_marker / table / list 等。
    chart_marker 走 GAP-1 缓存嵌入路径（命中 base64，未命中占位符）。
    """
    t = getattr(block, "type", "")
    text = getattr(block, "text", "") or ""
    level = getattr(block, "level", base_level) or base_level
    raw_line = getattr(block, "raw_line", "") or ""

    safe_text = html.escape(text, quote=False)

    if t == "heading":
        level_clamped = max(1, min(level, 7))
        raw = html.escape(raw_line or safe_text, quote=False)
        if heading_number:
            return f'<h{level_clamped}>{heading_number}{raw}</h{level_clamped}>\n'
        return f'<h{level_clamped}>{raw}</h{level_clamped}>\n'

    if t == "paragraph":
        # 遗留项 1（2026-11-04 导出加固轮·公式渲染）：开关开启且段落含 LaTeX
        # 公式片段时，公式段以 MathML 原生输出（现代浏览器渲染），文本段照常
        # escape；关闭/无公式/依赖缺失/转换失败时与历史输出逐字一致（零回归）。
        if config is not None and getattr(config, "render_math_omml", False):
            math_html = _render_paragraph_with_math_html(text)
            if math_html is not None:
                return math_html
        return f"<p>{safe_text}</p>\n" if safe_text else ""

    if t == "chart_marker":
        return _render_chart_marker_html(
            text, section or {}, config,
            project_id=project_id, chapter_number=chapter_number,
        )

    if t == "table":
        # markdown 表格转简单 <table>
        return _render_markdown_table(text)

    if t in ("list", "list_item"):
        return f"<li>{safe_text}</li>\n" if safe_text else ""

    # 未知 block type：兜底输出为段落
    if safe_text:
        return f"<p><!-- type={t} -->{safe_text}</p>\n"
    return ""


def _render_markdown_table(markdown: str) -> str:
    """简化版 markdown 表格 → HTML table。

    输入形如：
      | 列1 | 列2 |
      | --- | --- |
      | A   | B   |
    """
    lines = [l.strip() for l in markdown.strip().split("\n") if l.strip()]
    if not lines:
        return ""

    def _split_row(s: str) -> list[str]:
        s = s.strip().strip("|")
        return [html.escape(c.strip(), quote=False) for c in s.split("|")]

    rows = [_split_row(l) for l in lines]
    # 跳过分隔行（| --- | --- |）
    rows = [r for r in rows if not all(set(c) <= {"-", ":", " "} for c in r)]
    if not rows:
        return ""

    html_parts = ['<table class="tender-table">\n']
    for i, row in enumerate(rows):
        tag = "th" if i == 0 else "td"
        html_parts.append("  <tr>" + "".join(f"<{tag}>{c}</{tag}>" for c in row) + "</tr>\n")
    html_parts.append("</table>\n")
    return "".join(html_parts)


# ========== 核心构建链 ==========

def _load_sections_for_html(project_id: int) -> list[dict]:
    """加载项目全部章节（含 content + 图表 JSON），供 HTML 渲染消费。

    2026-10-18 ENHANCE（第二轮·展示顺序）：排序改为目录树深度优先前序，
    与正文生成 / DOCX 导出 / 缓存指纹共用 ``ordered_generation`` 的唯一实现。
    旧口径 ``ORDER BY level, sort_order, id`` 是**广度优先**，且 ``sort_order``
    是同父节点下的兄弟序号 —— 跨 L1 父节点会重复（每个 L1 的第一个 L2 子节
    sort_order 都是 1），按它做全局二级排序会把两棵 L1 子树交错
    （1.1, 2.1, 1.2, 2.2），即「展示顺序跳章乱序」。现按 (parent_id,
    sort_order, id) 前序遍历，与正文生成顺序一致。
    """
    from ..models import Section  # noqa: F401  (保留显式引用，便于字段契约对照)
    from .ai.ordered_generation import load_sections_in_tree_order

    columns = (
        "id, title, level, parent_id, sort_order, content,"
        " flowchart_json, gantt_json, architecture_json,"
        " labor_json, comparison_json, layout_json, timeline_json,"
        " inlined_chart_json"
    )
    with get_db() as conn:
        sections = load_sections_in_tree_order(conn, project_id, columns=columns)

    # 按 parent_id 分组建树（渲染侧仍走 build_tree 的树遍历，此处顺序供
    # sections_by_id 与任何直接消费扁平列表的下游使用）
    return sections


def _section_html_body(
    section: dict,
    *,
    config=None,
    project_id: int | None = None,
    chapter_number: str = "",
) -> str:
    """单个章节的 HTML 内容块（正文 + 图表嵌入/占位）。"""
    content = section.get("content") or ""
    level = section.get("level", 1) or 1
    blocks = parse_content(content, base_level=level - 1)

    parts: list[str] = []
    for b in blocks:
        parts.append(
            _render_block_to_html(
                b, base_level=level,
                section=section, config=config,
                project_id=project_id, chapter_number=chapter_number,
            )
        )

    return "\n".join(parts)


def _build_html_impl(project_id: int, cfg: ExportConfig,
                     output_path: str | None) -> tuple[str, str]:
    """build_html 主体（在导出会话上下文已建立后调用）。"""
    sections = _load_sections_for_html(project_id)
    # BUG-FIX 2026-10-13（P0·上线即 500）：原实现 `build_tree(sections)` 把
    # list[dict] 当成 project_id 传入 —— build_tree 的签名是
    # (project_id: int)，其内部用该参数做 `_build_tree_cache.get(project_id)`，
    # list 不可哈希 → `TypeError: cannot use 'list' as a dict key`，
    # /export/html 自 2026-10-12 上线起 100% 返回 500。
    tree = build_tree(project_id)

    # 遗留项 5（2026-11-04 导出加固轮·HTML 图表预渲染 opt-in）：GAP-1 的设计是
    # 「HTML 链路绝不触发实时渲染」——用户先导 HTML 时，除既有持久渲染件外
    # 图表全是占位符，体验差。现提供显式开关（html_preload_charts，默认 False
    # 保持 GAP-1 行为）：开启时复用 DOCX 同款并行预渲染把 ChartCache 预热，
    # 随后 _render_chart_marker_html 的缓存命中路径即嵌入真实图表。预渲染
    # 任一异常仅告警（按占位符继续），不影响 HTML 生成主链路。
    if getattr(cfg, "html_preload_charts", False):
        try:
            from .export import (  # 惰性 import：避免模块加载顺序耦合
                _batch_load_section_details,
                _collect_content_node_ids,
                _preload_chart_images_parallel,
            )

            _content_ids = _collect_content_node_ids(tree)
            _detail_index = (
                _batch_load_section_details(project_id, _content_ids)
                if _content_ids
                else {}
            )
            if _detail_index:
                _preloaded = _preload_chart_images_parallel(_detail_index, cfg)
                logger.info(
                    "[HTML导出·预渲染] 预热 %d 个图表缓存（html_preload_charts=on, project=%s）",
                    _preloaded, project_id,
                )
        except Exception as _pre_e:
            logger.warning(
                "[HTML导出·预渲染] 预热失败（按占位符继续，不影响 HTML 生成）: %s", _pre_e
            )

    # 惰性 import：export.py 不反向依赖本模块，但避免模块加载顺序耦合
    from .export import _should_skip_section

    # 取项目名做标题
    with get_db(readonly=True) as conn:
        proj_row = conn.execute(
            "SELECT name, bidder_name FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
    proj_name = proj_row["name"] if proj_row else f"项目{project_id}"
    bidder = proj_row["bidder_name"] if proj_row else ""

    # 按层级迭代树，生成每个章节的 HTML
    content_parts: list[str] = []
    content_parts.append('<header class="tender-cover">')
    content_parts.append(f"<h1>{html.escape(proj_name, quote=False)}</h1>")
    if bidder:
        content_parts.append(f"<p>投标单位：{html.escape(bidder, quote=False)}</p>")
    content_parts.append("</header>\n<hr>\n")

    # 与 DOCX 同源的章节编号器（第X节 / 一、 / 1、 / 1.1 …）
    numbering_tracker = HeadingNumberingGeneratorV2()
    sections_by_id = {s["id"]: s for s in sections}

    # 扁平遍历所有章节（树的 pre-order）
    # BUG-FIX 2026-10-13（P0·AttributeError）：build_tree 返回 pydantic
    # SectionTreeNode（属性访问），原实现用 dict 风格 node.get("id") /
    # node.get("children") → AttributeError: 'SectionTreeNode' has no attribute 'get'。
    def _walk(node_list, parent_id: int | None = None):
        for node in node_list:
            section_id = node.id
            # 业务过滤：与 DOCX 同口径，模板占位章节（如「（涉及项目有关）」）
            # 不输出，且连同整棵子树一起跳过
            if _should_skip_section(node.title):
                continue
            full = sections_by_id.get(section_id)
            if not full:
                continue

            level = full.get("level", 1) or 1
            section_title = html.escape(node.title or "", quote=False)

            # 标题编号：与 DOCX _write_tree_content_with_progress 完全同源。
            # update_counter 对 L5+（不受控层级）返回空串 → 不加顿号，
            # 避免渲染成「、施工组织设计」（BUG-FIX 2026-10-09 同口径）。
            raw_number = numbering_tracker.update_counter(level, str(parent_id))
            section_num = f"{raw_number}、" if raw_number else ""
            # GAP-1（2026-11-02）：图号章号与 DOCX 同口径取**当前 L1 章序号**
            # （export.py L4167 同源）；counters[0] 在处理任意层级节点后保持当前章号。
            chapter_number = str(max(1, numbering_tracker.counters[0] or 1))

            level_clamped = max(1, min(level, 7))
            content_parts.append(
                f"<h{level_clamped}>{section_num}{section_title}</h{level_clamped}>\n"
            )

            # 章节正文
            body_html = _section_html_body(
                full, config=cfg, project_id=project_id,
                chapter_number=chapter_number,
            )
            if body_html.strip():
                content_parts.append(body_html)

            # 递归子节点（传 node.id 让下级编号重置计数器）
            if node.children:
                _walk(node.children, node.id)

    _walk(tree)

    styles = _build_html_styles(cfg)
    html_doc = _DEFAULT_HTML_TEMPLATE.format(
        title=html.escape(f"{proj_name} — 招标方案", quote=False),
        styles=styles,
        content="\n".join(content_parts),
    )

    # 计算内容指纹（用于缓存/比对）
    fingerprint = hashlib.sha256(html_doc.encode("utf-8")).hexdigest()[:16]

    # 落盘
    if output_path is None:
        ts = time.strftime("%Y%m%d_%H%M%S")
        out_dir = PROJECTS_DATA_DIR / str(project_id) / "exports"
        out_dir.mkdir(parents=True, exist_ok=True)
        safe_name = "".join(c for c in proj_name if c not in r"\/*?\"<>|:")
        output_path = str(out_dir / f"{safe_name}_{ts}.html")

    # BUG-FIX 2026-10-13（P1·原子写盘）：原实现 `Path(output_path).write_text`
    # 直写最终路径 —— ① 写盘中途失败会留下半截 .html 供用户下载；② 同项目并发
    # 导出按秒级时间戳撞名时两个写入者互相截断。改为「同目录临时件 + os.replace
    # 原子替换」，与 export.py 的 _atomic_save_docx 同一模式：
    #   1. 临时件名带 pid + uuid 段，并发写入互不覆盖；
    #   2. 非零字节才替换（零字节半成品不进最终路径）；
    #   3. os.replace 在 Windows/POSIX 均为原子操作 → 最终文件要么旧版完整版
    #      要么新版完整版，不存在半截中间态；
    #   4. 失败即清理临时件，不污染 exports/ 目录。
    tmp_path = ""
    try:
        tmp_path = f"{output_path}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(html_doc)
        if html_doc and os.path.getsize(tmp_path) == 0:
            raise RuntimeError("HTML 临时文件大小为 0，拒绝原子替换")
        os.replace(tmp_path, output_path)
        tmp_path = ""
    except Exception:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise

    logger.info("HTML 导出完成: %s (%d bytes)", output_path, len(html_doc))

    return str(Path(output_path).resolve()), fingerprint


def build_html(project_id: int, config: ExportConfig | None = None,
               output_path: str | None = None) -> tuple[str, str]:
    """构建 HTML 导出文件。

    Args:
        project_id: 项目 ID
        config: 导出配置（可选，缺省用 resolve_export_config 解析预设默认值）
        output_path: 显式输出路径；缺省落 PROJECTS_DATA_DIR/<pid>/exports/<项目名>_<ts>.html

    Returns:
        (实际落盘绝对路径, 内容指纹 hash)

    GAP-1（2026-11-02）：与 build_docx 同口径建立**导出会话上下文**
    （图号计数器 + 图题登记，contextvars 隔离），保证：
      1. 图号从「图X-1」起算，不跨导出会话累加（同 build_docx 2026-09-14 修复）；
      2. 只清本项目的进程级桶，并发导出其他项目不受影响（2026-09-29 修复同口径）；
      3. 与并发的 DOCX 导出会话通过 contextvars 相互隔离。
    """
    cfg = resolve_export_config(config or ExportConfig())

    _fig_ctx: dict[str, int] = {}
    _token = _fig_counter_ctx_var.set(_fig_ctx)
    _title_ctx: dict[str, set[str]] = {}
    _title_token = _used_chart_titles_ctx_var.set(_title_ctx)
    try:
        _reset_figure_counter(project_id)
        _reset_chart_title_registry(project_id)
        return _build_html_impl(project_id, cfg, output_path)
    finally:
        _fig_counter_ctx_var.reset(_token)
        _used_chart_titles_ctx_var.reset(_title_token)


def export_to_html(project_id: int, config: ExportConfig | None = None) -> Path:
    """对外简洁 API（与 export_to_docx 对称）。"""
    path, _ = build_html(project_id, config=config)
    return Path(path)
