"""Comparison 图表渲染器。

由 scripts/_split_mermaid.py 从原 mermaid_renderer.py 自动拆分而来。
所有公共工具从 .mermaid_common 导入。
"""
from __future__ import annotations

import logging
import math
from io import BytesIO

from PIL import Image, ImageDraw

from .mermaid_common import (
    _build_gantt_marks,
    _draw_text_center,
    _fit_text_with_ellipsis,
    _image_font,
    _image_to_stream,
    _render_hq,
    _text_height,
    _text_width,
    _wrap_text,
)


import re

logger = logging.getLogger(__name__)



def _parse_mermaid_pie(code: str) -> dict | None:
    """解析 Mermaid pie 代码为对比图数据（供 v2 渲染器）

    语法：
        pie title 图表标题
            "标签1" : 40
            "标签2" : 60
    """
    if not isinstance(code, str) or not code.strip().lower().startswith("pie"):
        return None
    title = ""
    items: list[dict] = []
    for line in code.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("%%") or stripped.startswith("//"):
            continue
        low = stripped.lower()
        if low.startswith("pie"):
            _t = stripped[3:].strip()
            if _t.lower().startswith("title"):
                title = _t[5:].strip()
            elif _t.startswith(("showData", "themeConfig")):
                continue
            continue
        if low.startswith("title"):
            title = stripped[5:].strip()
            continue
        m = re.match(r'^"([^"]+)"\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*$', stripped)
        if m:
            items.append({"label": m.group(1).strip(), "value": float(m.group(2))})
            continue
        # 兼容无引号格式：标签 : 40
        m2 = re.match(r"^([^:\"#]+?)\s*:\s*([0-9]+(?:\.[0-9]+)?)\s*$", stripped)
        if m2 and not stripped.startswith(("showData", "themeConfig")):
            items.append({"label": m2.group(1).strip(), "value": float(m2.group(2))})
    if len(items) < 2:
        return None
    return {"type": "comparison", "title": title or "占比对比", "items": items}

def _parse_mermaid_xychart(code: str) -> dict | None:
    """解析 Mermaid xychart-beta 柱状图代码为对比图数据（供 v2 渲染器）

    语法：
        xychart-beta
            title "图表标题"
            x-axis [标签1, 标签2, 标签3]
            y-axis "数值" 0 --> 100
            bar [10, 20, 30]
    """
    if not isinstance(code, str):
        return None
    head = code.strip().split("\n", 1)[0].strip().lower()
    if not (head.startswith("xychart-beta") or head.startswith("xychart")):
        return None
    title = ""
    x_labels: list[str] = []
    bar_values: list[float] = []
    for line in code.split("\n"):
        stripped = line.strip()
        if not stripped or stripped.startswith("%%"):
            continue
        if stripped.lower().startswith(("xychart-beta", "xychart")):
            continue
        m_title = re.match(r'^title\s+"?([^"]+)"?\s*$', stripped)
        if m_title:
            title = m_title.group(1).strip()
            continue
        m_axis = re.match(r'^x-axis\s+(?:"[^"]*"\s+)?\[(.*)\]\s*$', stripped)
        if m_axis:
            x_labels = [s.strip().strip("\"'") for s in m_axis.group(1).split(",") if s.strip()]
            continue
        m_bar = re.match(r"^bar\s+\[(.*)\]\s*$", stripped)
        if m_bar:
            try:
                bar_values = [float(s.strip()) for s in m_bar.group(1).split(",") if s.strip()]
            except ValueError:
                bar_values = []
            continue
    if not x_labels or not bar_values:
        return None
    n = min(len(x_labels), len(bar_values))
    if n < 2:
        return None
    items = [{"label": x_labels[i], "value": bar_values[i]} for i in range(n)]
    return {"type": "comparison", "title": title or "数值对比", "items": items}

@_render_hq
def _render_comparison_image_v2(data: dict) -> BytesIO | None:
    """商业级对比图渲染器 v2：左饼图 + 右柱状图

    支持的 data 格式：
      1. 标准表格：{"headers": ["方案", "材料费", "人工费", ...], "rows": [{"方案": "A", "材料费": 80, ...}, ...]}
      2. 简化表格：{"rows": [{"label": "方案A", "values": [80, 50, 30, 160]}, ...]}（values 对应除第一列外的多指标）
      3. 极简对比：{"rows": [{"label": "A", "value": 30}, ...]} 或 {"items": [{"label": "A", "value": 30}, ...]}
    """
    if not isinstance(data, dict):
        return None

    rows_data = data.get("rows", [])
    headers = data.get("headers", ["对比项", "数值"])

    if not rows_data and "items" in data:
        # Mermaid pie 数据格式: {"items": [{"label": "A", "value": 30}]}
        items = data.get("items", [])
        if items:
            rows_data = [
                {"label": it.get("label", ""), "value": it.get("value", 0)} for it in items
            ]
            headers = ["对比项", "数值"]

    if not rows_data:
        return None

    # 准备数据：兼容多种数据格式
    parsed_rows = []
    for row in rows_data:
        if not isinstance(row, dict):
            continue
        # 1) 极简：{"label": "A", "value": 30}
        if "label" in row and ("value" in row or "数值" in row or "占比" in row):
            label = str(row.get("label", "")).strip()
            try:
                value = float(row.get("value") or row.get("数值") or row.get("占比") or 0)
            except (TypeError, ValueError):
                value = 0.0
            if label and value > 0:
                parsed_rows.append({"label": label, "value": value})
            continue

        # 2) 多指标表格：{"label": "方案A", "values": [80, 50, 30, 160]}
        if "label" in row and "values" in row and isinstance(row["values"], list):
            label = str(row.get("label", "")).strip()
            values = row["values"]
            # 使用 values 总和作为对比值；若第一项是总和则用第一项
            try:
                value = float(sum(values)) if values else 0
            except (TypeError, ValueError):
                value = 0.0
            if label and value > 0:
                # 附加多指标详情
                detail = []
                for idx, v in enumerate(values):
                    if idx + 1 < len(headers):
                        try:
                            detail.append({"name": str(headers[idx + 1]), "value": float(v)})
                        except (TypeError, ValueError):
                            continue
                parsed_rows.append({"label": label, "value": value, "detail": detail})
            continue

        # 3) 标准表格：{"方案": "A", "材料费": 80, "人工费": 50, ...}
        # 取第一列作为 label，第二列作为 value
        if headers and len(headers) >= 2:
            label_key = headers[0]
            value_key = headers[1]
            label = str(row.get(label_key, "")).strip()
            try:
                value = float(row.get(value_key, 0) or 0)
            except (TypeError, ValueError):
                value = 0.0
            if label and value > 0:
                parsed_rows.append({"label": label, "value": value})
                continue
            # label 或 value 为空/0，不 continue，让代码落入第4步

        # 4) 数值对：{ "key": val, ... }
        if len(row) >= 1:
            for k, v in list(row.items())[:1]:
                try:
                    value = float(v)
                except (TypeError, ValueError):
                    continue
                if value > 0:
                    parsed_rows.append({"label": str(k), "value": value})
                    break

    if not parsed_rows:
        return None

    logger.info(
        "[PIL v2渲染] 对比图渲染启动: 方案数=%d",
        len(parsed_rows),
    )

    total = sum(r["value"] for r in parsed_rows)
    logger.info(
        "[PIL v2渲染] 对比图数据准备: 总数值=%.1f 方案数=%d 标题=%s",
        total,
        len(parsed_rows),
        str(data.get("title") or "对比图"),
    )

    # ---- 尺寸 ----
    scale = 2.5
    title_h = int(56 * scale)
    padding = int(30 * scale)
    pie_size = int(340 * scale)
    bar_chart_w = int(480 * scale)
    bar_chart_h = int(320 * scale)
    bar_row_h = int(38 * scale)

    # 自动决定高度
    rows_h = max(bar_chart_h, len(parsed_rows) * bar_row_h + int(60 * scale))
    total_w = padding * 2 + pie_size + int(40 * scale) + bar_chart_w
    total_h = title_h + padding * 2 + max(pie_size, rows_h) + int(30 * scale)  # 额外给图例留空间
    logger.info(
        "[PIL v2渲染] 对比图画布: %dx%dpx 饼图=%dx%dpx 柱状图=%dx%dpx",
        total_w,
        total_h,
        pie_size,
        pie_size,
        bar_chart_w,
        bar_chart_h,
    )

    img = Image.new("RGB", (total_w, int(total_h)), "white")
    draw = ImageDraw.Draw(img)
    font_title = _image_font(int(20 * scale), bold=True)
    font_label = _image_font(int(15 * scale), bold=True)
    font_value = _image_font(int(14 * scale))
    font_pct = _image_font(int(13 * scale))
    font_caption = _image_font(int(13 * scale))
    font_bar_value = _image_font(int(12 * scale), bold=True)

    # 标题
    title = str(data.get("title") or "对比图")
    draw.text((padding, int(18 * scale)), title, fill="#1E3A5F", font=font_title)

    # 配色（商业级调色板）
    palette = [
        "#2B579A",
        "#E8A33D",
        "#70AD47",
        "#C00000",
        "#7030A0",
        "#4472C4",
        "#9966CC",
        "#A8D8EA",
        "#FFB6B9",
        "#FAE3D9",
        "#BBDED6",
        "#8B9DC3",
        "#DFB2E0",
        "#9DC3E6",
        "#F4A261",
    ]

    # ---- 左：饼图（3D 效果）----
    pie_cx = padding + pie_size / 2
    pie_cy = title_h + padding + pie_size / 2
    pie_r = pie_size / 2 - int(10 * scale)
    # 3D 偏移量
    d3d_offset = int(8 * scale)

    # 先绘制 3D 阴影层（偏移后的扇区）
    start_angle = -90
    for i, row in enumerate(parsed_rows):
        sweep = (row["value"] / total) * 360
        if sweep < 1:
            start_angle += sweep
            continue
        # 阴影层
        draw.pieslice(
            [
                pie_cx - pie_r,
                pie_cy - pie_r + d3d_offset,
                pie_cx + pie_r,
                pie_cy + pie_r + d3d_offset,
            ],
            start=start_angle,
            end=start_angle + sweep,
            fill="#D1D5DB",
            outline=None,
        )
        start_angle += sweep

    # 再绘制主体扇区
    start_angle = -90
    for i, row in enumerate(parsed_rows):
        color = palette[i % len(palette)]
        sweep = (row["value"] / total) * 360
        if sweep < 1:
            start_angle += sweep
            continue
        draw.pieslice(
            [pie_cx - pie_r, pie_cy - pie_r, pie_cx + pie_r, pie_cy + pie_r],
            start=start_angle,
            end=start_angle + sweep,
            fill=color,
            outline="white",
            width=2,
        )
        # 标注百分比（扇区中心）
        mid_angle = (start_angle + start_angle + sweep) / 2
        rad = math.radians(mid_angle)
        label_r = pie_r * 0.65
        lx = pie_cx + label_r * math.cos(rad)
        ly = pie_cy + label_r * math.sin(rad)
        pct = (row["value"] / total) * 100
        pct_text = f"{pct:.1f}%"
        if pct >= 5:  # 只标注 > 5% 的扇区
            bbox = draw.textbbox((0, 0), pct_text, font=font_pct)
            tw = bbox[2] - bbox[0]
            th = bbox[3] - bbox[1]
            # 先画阴影再画文字
            draw.text((lx - tw / 2 + 1, ly - th / 2 + 1), pct_text, fill="#B0B0B0", font=font_pct)
            draw.text((lx - tw / 2, ly - th / 2), pct_text, fill="white", font=font_pct)
        start_angle += sweep

    # 外圈
    draw.ellipse(
        [pie_cx - pie_r, pie_cy - pie_r, pie_cx + pie_r, pie_cy + pie_r],
        outline="#1E3A5F",
        width=int(2 * scale),
    )

    # ---- 右：柱状图 ----
    bar_x = padding + pie_size + int(40 * scale)
    bar_y = title_h + padding
    chart_h = rows_h - int(40 * scale)
    bar_max_w = bar_chart_w - int(180 * scale)  # 给图例和数值留空间

    max_value = max(r["value"] for r in parsed_rows)
    n_rows = len(parsed_rows)
    row_height = (chart_h - int(20 * scale)) / max(n_rows, 1)

    for i, row in enumerate(parsed_rows):
        color = palette[i % len(palette)]
        cy = bar_y + int(20 * scale) + i * row_height
        # 标签
        draw.text((bar_x, cy - int(8 * scale)), row["label"], fill="#1E3A5F", font=font_label)
        # 柱（带阴影）
        bar_w = (row["value"] / max_value) * bar_max_w
        # 阴影
        draw.rounded_rectangle(
            [
                bar_x + int(120 * scale) + int(2 * scale),
                cy - int(4 * scale),
                bar_x + int(120 * scale) + bar_w + int(2 * scale),
                cy + int(16 * scale),
            ],
            radius=int(4 * scale),
            fill="#D1D5DB",
        )
        # 主体
        draw.rounded_rectangle(
            [
                bar_x + int(120 * scale),
                cy - int(6 * scale),
                bar_x + int(120 * scale) + bar_w,
                cy + int(14 * scale),
            ],
            radius=int(4 * scale),
            fill=color,
        )
        # 柱上数值标签
        if bar_w > int(40 * scale):
            bbox = draw.textbbox((0, 0), f"{row['value']:.0f}", font=font_bar_value)
            tw_v = bbox[2] - bbox[0]
            draw.text(
                (bar_x + int(120 * scale) + bar_w / 2 - tw_v / 2, cy - int(4 * scale)),
                f"{row['value']:.0f}",
                fill="white",
                font=font_bar_value,
            )

        # 数值 + 百分比
        pct = (row["value"] / total) * 100
        value_text = f"{row['value']:.1f} ({pct:.1f}%)"
        draw.text(
            (bar_x + int(120 * scale) + bar_w + int(8 * scale), cy - int(7 * scale)),
            value_text,
            fill="#374151",
            font=font_value,
        )

    # ---- 图例 ----
    legend_y = total_h - int(24 * scale)
    legend_x = padding
    draw.text(
        (legend_x, legend_y - int(13 * scale)),
        f"总计: {total:.1f}",
        fill="#1E3A5F",
        font=font_caption,
    )

    result_stream = _image_to_stream(img)
    size_kb = len(result_stream.getvalue()) // 1024
    logger.info(
        "[PIL v2渲染] 对比图渲染完成: 方案数=%d 尺寸=%dKB DPI=300",
        len(parsed_rows),
        size_kb,
    )
    return result_stream

