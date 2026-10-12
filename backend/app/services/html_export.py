"""HTML 导出格式（2026-10-12 新增）

与 DOCX 导出**并立**的第二交付格式：复用同一套内容组装链（章节树 + parse_content +
resolve_export_config），但消费端换成简洁 HTML 渲染器。图表标记（chart_marker）按
「无降级」原则输出占位符 + 标题（不伪造数据）——若需真实图表图，用 LibreOffice 转换
的 PDF 或 Word 原生 DOCX。

消费链：
  sections.build_tree → sections.list_sections → 逐节 parse_content → 逐块 HTML 渲染

与 DOCX 并行，不替代 DOCX/PDF；前端可在导出配置页选择「导出 HTML」。
"""
from __future__ import annotations

import hashlib
import html
import json
import logging
import time
from pathlib import Path

from ..config import PROJECTS_DATA_DIR
from ..db import get_db
from ..models import ExportConfig
from .content_cleaning import parse_content
from .export_presets import resolve_export_config
from .sections import build_tree, list_sections


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
        "hr { border: none; border-top: 1px solid #ddd; margin: 16px 0; }",
    ]
    return "\n".join(styles)


# ========== DocumentBlock → HTML 渲染 ==========

def _render_block_to_html(block, base_level: int = 0, heading_number: str = "") -> str:
    """把单个 DocumentBlock 转成 HTML 片段。

    BlockType 覆盖：heading / paragraph / chart_marker / table / list 等。
    chart_marker 按「无降级」原则输出占位符。
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
        return f"<p>{safe_text}</p>\n" if safe_text else ""

    if t == "chart_marker":
        title = html.escape(text.strip() or "图表占位符", quote=False)
        return (
            f'<div class="chart-placeholder">'
            f"<div>[图表占位符：{title}]</div>"
            f"<figcaption>真实图表需 DOCX/PDF 格式渲染</figcaption>"
            f"</div>\n"
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
    """加载项目全部章节（含 content + 图表 JSON），供 HTML 渲染消费。"""
    from ..models import Section

    with get_db() as conn:
        rows = conn.execute(
            """SELECT id, title, level, parent_id, sort_order, content,
                      flowchart_json, gantt_json, architecture_json,
                      labor_json, comparison_json, layout_json, timeline_json,
                      inlined_chart_json
               FROM sections WHERE project_id = ?
               ORDER BY level, sort_order, id""",
            (project_id,),
        ).fetchall()

    # 按 parent_id 分组建树
    sections = [dict(r) for r in rows]
    return sections


def _section_html_body(section: dict) -> str:
    """单个章节的 HTML 内容块（正文 + 图表占位）。"""
    content = section.get("content") or ""
    level = section.get("level", 1) or 1
    blocks = parse_content(content, base_level=level - 1)

    parts: list[str] = []
    for b in blocks:
        parts.append(_render_block_to_html(b, base_level=level))

    return "\n".join(parts)


def build_html(project_id: int, config: ExportConfig | None = None,
               output_path: str | None = None) -> tuple[str, str]:
    """构建 HTML 导出文件。

    Args:
        project_id: 项目 ID
        config: 导出配置（可选，缺省用 resolve_export_config 解析预设默认值）
        output_path: 显式输出路径；缺省落 PROJECTS_DATA_DIR/<pid>/exports/<项目名>_<ts>.html

    Returns:
        (实际落盘绝对路径, 内容指纹 hash)
    """
    cfg = resolve_export_config(config or ExportConfig())

    sections = _load_sections_for_html(project_id)
    tree = build_tree(sections)

    # 取项目名做标题
    with get_db(readonly=True) as conn:
        proj_row = conn.execute(
            "SELECT name, bidder_name FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
    proj_name = proj_row["name"] if proj_row else f"项目{project_id}"
    bidder = proj_row["bidder_name"] if proj_row else ""

    # 按层级迭代树，生成每个章节的 HTML
    content_parts: list[str] = []
    content_parts.append(f'<header class="tender-cover">')
    content_parts.append(f"<h1>{html.escape(proj_name, quote=False)}</h1>")
    if bidder:
        content_parts.append(f"<p>投标单位：{html.escape(bidder, quote=False)}</p>")
    content_parts.append("</header>\n<hr>\n")

    # 扁平遍历所有章节（树的 pre-order）
    def _walk(node_list, parent_num: str = ""):
        for idx, node in enumerate(node_list):
            section_id = node.get("id")
            if section_id is None:
                continue
            # 在 sections 列表里找完整 dict
            full = next((s for s in sections if s["id"] == section_id), None)
            if not full:
                continue

            level = full.get("level", 1) or 1
            section_title = html.escape(full.get("title", "") or "", quote=False)

            # 标题 + 编号（按项目已有层级）
            if parent_num:
                section_num = f"{parent_num}.{idx + 1}"
            else:
                section_num = str(idx + 1)

            level_clamped = max(1, min(level, 7))
            content_parts.append(
                f"<h{level_clamped}>{section_num} {section_title}</h{level_clamped}>\n"
            )

            # 章节正文
            body_html = _section_html_body(full)
            if body_html.strip():
                content_parts.append(body_html)

            # 递归子节点
            children = node.get("children") or []
            if children:
                _walk(children, section_num)

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

    Path(output_path).write_text(html_doc, encoding="utf-8")
    logger.info("HTML 导出完成: %s (%d bytes)", output_path, len(html_doc))

    return str(Path(output_path).resolve()), fingerprint


def export_to_html(project_id: int, config: ExportConfig | None = None) -> Path:
    """对外简洁 API（与 export_to_docx 对称）。"""
    path, _ = build_html(project_id, config=config)
    return Path(path)
