"""Labor 图表渲染器。

由 scripts/_split_mermaid.py 从原 mermaid_renderer.py 自动拆分而来。
所有公共工具从 .mermaid_common 导入。
"""
from __future__ import annotations

import logging
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



@_render_hq
def _render_labor_image_v2(labor_data: dict) -> BytesIO | None:
    """商业级劳动力图渲染器 v2：四合一视图

    布局：左 1/3 = 工种总投入柱状图；右 2/3 = 上 = 堆叠柱状图、下 = 时段总人数曲线图

    使用 2.5x 超采样渲染，确保 DOCX 导出达到印刷级质量（300 DPI）。
    """
    if not isinstance(labor_data, dict):
        return None
    phases = labor_data.get("phases") or labor_data.get("time") or labor_data.get("stages") or []
    categories = labor_data.get("categories") or labor_data.get("types") or []
    data = labor_data.get("data") or []

    if not phases or not categories or not data:
        return None

    # 规范化数据（兼容两种格式：[{phase, values: [...]}] 和 [[v1, v2, ...]]）
    matrix: list[list[float]] = []
    for row in data:
        if isinstance(row, dict):
            values = row.get("values") or []
            if not isinstance(values, list):
                continue
            try:
                matrix.append([float(v) for v in values])
            except (TypeError, ValueError):
                continue
        elif isinstance(row, (list, tuple)):
            try:
                matrix.append([float(v) for v in row])
            except (TypeError, ValueError):
                continue
    if not matrix:
        return None

    n_phases = len(phases)
    n_cats = len(categories)
    if n_cats == 0:
        return None
    logger.info(
        "[PIL v2渲染] 劳动力图渲染启动: 阶段数=%d 工种数=%d 数据矩阵=%dx%d",
        n_phases,
        n_cats,
        len(matrix),
        n_cats,
    )
    # 维度校验：matrix 行数应等于 phases 数
    if len(matrix) != n_phases:
        logger.warning(
            "[劳动力图] phases(%d) 与 matrix行数(%d) 不匹配，自动截断/补齐",
            n_phases,
            len(matrix),
        )
        if len(matrix) > n_phases:
            matrix[:] = matrix[:n_phases]
        else:
            matrix.extend([[0.0] * n_cats for _ in range(n_phases - len(matrix))])
    # 补齐或截断列数
    for r in matrix:
        if len(r) < n_cats:
            r.extend([0.0] * (n_cats - len(r)))
        elif len(r) > n_cats:
            del r[n_cats:]

    # 计算各工种总和 & 各阶段总和
    cat_totals = [sum(matrix[p][c] for p in range(n_phases)) for c in range(n_cats)]
    phase_totals = [sum(matrix[p]) for p in range(n_phases)]

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
    ]

    # ---- 超采样缩放 ----
    # 使用 2.5x 缩放渲染 + @_render_hq 保持原分辨率并添加 300 DPI 元数据
    # 2.5x 渲染 + 不降采样 = 真实超采样抗锯齿，最终 DOCX 插入时 DPI 感知缩放到合适尺寸
    scale = 2.5

    # ---- 尺寸（全部乘以 scale）----
    title_h = int(60 * scale)
    padding = int(24 * scale)
    sub_gap = int(24 * scale)

    # 左 1/3：工种柱状图
    left_w = int(380 * scale)
    left_chart_h = int(360 * scale)

    # 右 2/3：堆叠柱状图 + 时段曲线图
    right_w = int(720 * scale)
    right_chart_h_each = int(220 * scale)
    right_chart_h = right_chart_h_each * 2 + sub_gap

    total_w = padding * 2 + left_w + sub_gap + right_w
    total_h = title_h + padding * 2 + max(left_chart_h, right_chart_h) + int(40 * scale)

    img = Image.new("RGB", (total_w, total_h), "white")
    draw = ImageDraw.Draw(img)
    font_title = _image_font(int(20 * scale), bold=True)
    font_section = _image_font(int(16 * scale), bold=True)
    font_label = _image_font(int(13 * scale), bold=True)
    font_value = _image_font(int(12 * scale))
    font_axis = _image_font(int(11 * scale))
    font_legend = _image_font(int(12 * scale))

    title = str(labor_data.get("title") or "劳动力投入及工种分布图")
    draw.text((padding, int(18 * scale)), title, fill="#1E3A5F", font=font_title)

    # ============== 左 1/3：工种总投入柱状图（横向） ==============
    left_x0 = padding
    left_y0 = title_h + padding
    label_w = int(100 * scale)
    bar_area_w = left_w - label_w - int(60 * scale)

    # 小标题
    draw.text((left_x0, left_y0), "① 各工种总投入（人·日）", fill="#1E3A5F", font=font_section)

    max_cat = max(cat_totals) if cat_totals else 1
    n_cat = len(cat_totals)
    cat_row_h = (left_chart_h - int(40 * scale)) / max(n_cat, 1)

    for i, (cat, val) in enumerate(zip(categories, cat_totals, strict=False)):
        cy = left_y0 + int(36 * scale) + i * cat_row_h
        # 标签
        draw.text((left_x0, cy - int(4 * scale)), str(cat)[:10], fill="#1E3A5F", font=font_label)
        # 柱
        bar_w = (val / max_cat) * bar_area_w if max_cat > 0 else 0
        color = palette[i % len(palette)]
        draw.rounded_rectangle(
            [left_x0 + label_w, cy, left_x0 + label_w + bar_w, cy + int(18 * scale)],
            radius=int(3 * scale),
            fill=color,
        )
        # 数值
        draw.text(
            (left_x0 + label_w + bar_w + int(6 * scale), cy - int(2 * scale)),
            f"{val:.0f}",
            fill="#374151",
            font=font_value,
        )

    # ============== 右 2/3 上：堆叠柱状图 ==============
    right_x0 = padding + left_w + sub_gap
    right_y0 = title_h + padding
    draw.text(
        (right_x0, right_y0), "② 各阶段工种分布（堆叠柱状图）", fill="#1E3A5F", font=font_section
    )

    sc_y0 = right_y0 + int(32 * scale)
    sc_h = right_chart_h_each - int(50 * scale)
    sc_w = right_w - int(80 * scale)
    max_phase = max(phase_totals) if phase_totals else 1

    # Y 轴刻度
    y_ticks = 5
    for i in range(y_ticks + 1):
        val = int(max_phase * (y_ticks - i) / y_ticks)
        y_pos = sc_y0 + int(i * sc_h / y_ticks)
        draw.text(
            (right_x0 - int(32 * scale), y_pos - int(8 * scale)),
            str(val),
            fill="#64748B",
            font=font_axis,
        )
        draw.line(
            [(right_x0 - int(6 * scale), y_pos), (right_x0 + sc_w, y_pos)],
            fill="#E5E7EB",
            width=int(1 * scale),
        )

    # 柱
    col_w = sc_w / max(n_phases, 1)
    for p in range(n_phases):
        cx = right_x0 + p * col_w + col_w * 0.15
        cw = col_w * 0.7
        cum = 0.0
        for c in range(n_cats):
            val = matrix[p][c]
            if val <= 0:
                continue
            seg_h = (val / max_phase) * sc_h
            seg_y = sc_y0 + sc_h - cum - seg_h
            cum += seg_h
            color = palette[c % len(palette)]
            draw.rectangle(
                [cx, seg_y, cx + cw, sc_y0 + sc_h],
                fill=color,
                outline="white",
                width=int(1 * scale),
            )
            # 每段上标注数值
            if seg_h > int(15 * scale):
                bbox = draw.textbbox((0, 0), f"{val:.0f}", font=font_value)
                tw_v = bbox[2] - bbox[0]
                draw.text(
                    (cx + cw / 2 - tw_v / 2, seg_y + seg_h / 2 - int(6 * scale)),
                    f"{val:.0f}",
                    fill="white",
                    font=font_value,
                )
        # 阶段标签
        bbox = draw.textbbox((0, 0), str(phases[p]), font=font_value)
        tw = bbox[2] - bbox[0]
        draw.text(
            (cx + cw / 2 - tw / 2, sc_y0 + sc_h + int(4 * scale)),
            str(phases[p])[:8],
            fill="#374151",
            font=font_value,
        )

    # ============== 右 2/3 下：时段总人数曲线图 ==============
    line_y0 = right_y0 + right_chart_h_each + sub_gap
    draw.text(
        (right_x0, line_y0), "③ 各阶段总人数变化（折线图）", fill="#1E3A5F", font=font_section
    )

    lc_y0 = line_y0 + int(32 * scale)
    lc_h = right_chart_h_each - int(50 * scale)
    lc_w = right_w - int(80 * scale)
    max_total = max(phase_totals) if phase_totals else 1

    # Y 轴刻度
    for i in range(y_ticks + 1):
        val = int(max_total * (y_ticks - i) / y_ticks)
        y_pos = lc_y0 + int(i * lc_h / y_ticks)
        draw.text(
            (right_x0 - int(32 * scale), y_pos - int(8 * scale)),
            str(val),
            fill="#64748B",
            font=font_axis,
        )
        draw.line(
            [(right_x0 - int(6 * scale), y_pos), (right_x0 + lc_w, y_pos)],
            fill="#E5E7EB",
            width=int(1 * scale),
        )

    # 折线
    points: list[tuple[float, float]] = []
    for p, total in enumerate(phase_totals):
        x = right_x0 + (p + 0.5) * (lc_w / max(n_phases, 1))
        y = lc_y0 + lc_h - (total / max_total) * lc_h if max_total > 0 else lc_y0 + lc_h
        points.append((x, y))

    if len(points) >= 2:
        for i in range(len(points) - 1):
            draw.line([points[i], points[i + 1]], fill="#2B579A", width=int(3 * scale))
        # 填充
        fill_pts = points + [(points[-1][0], lc_y0 + lc_h), (points[0][0], lc_y0 + lc_h)]
        try:
            draw.polygon(fill_pts, fill=(43, 87, 154))  # 移除了 alpha 通道，兼容 RGB 模式
        except Exception as _e:
            logger.debug("[silent-except] mermaid_labor.py: line 291 - %s", _e)

    for i, (x, y) in enumerate(points):
        draw.ellipse(
            [x - int(5 * scale), y - int(5 * scale), x + int(5 * scale), y + int(5 * scale)],
            fill="#1E3A5F",
            outline="white",
            width=int(2 * scale),
        )
        bbox = draw.textbbox((0, 0), str(int(phase_totals[i])), font=font_axis)
        tw = bbox[2] - bbox[0]
        draw.text(
            (x - tw / 2, y - int(22 * scale)),
            str(int(phase_totals[i])),
            fill="#1E3A5F",
            font=font_axis,
        )
        # X 轴标签
        bbox2 = draw.textbbox((0, 0), str(phases[i]), font=font_value)
        tw2 = bbox2[2] - bbox2[0]
        draw.text(
            (x - tw2 / 2, lc_y0 + lc_h + int(4 * scale)),
            str(phases[i])[:8],
            fill="#374151",
            font=font_value,
        )

    # ============== 图例 ==============
    legend_y = total_h - int(32 * scale)
    legend_x = padding
    for c, cat in enumerate(categories):
        color = palette[c % len(palette)]
        draw.rectangle(
            [
                legend_x,
                legend_y - int(10 * scale),
                legend_x + int(14 * scale),
                legend_y + int(2 * scale),
            ],
            fill=color,
            outline="#1E3A5F",
            width=int(1 * scale),
        )
        draw.text(
            (legend_x + int(18 * scale), legend_y - int(12 * scale)),
            str(cat)[:8],
            fill="#1E3A5F",
            font=font_legend,
        )
        legend_x += int(90 * scale)
        if legend_x + int(90 * scale) > total_w:
            legend_x = padding
            legend_y += int(16 * scale)

    result_stream = _image_to_stream(img)
    size_kb = len(result_stream.getvalue()) // 1024
    logger.info(
        "[PIL v2渲染] 劳动力图渲染完成: 阶段=%d 工种=%d 尺寸=%dKB DPI=300",
        n_phases,
        n_cats,
        size_kb,
    )
    return result_stream

