"""Timeline 图表渲染器。

由 scripts/_split_mermaid.py 从原 mermaid_renderer.py 自动拆分而来。
所有公共工具从 .mermaid_common 导入。
"""
from __future__ import annotations

import copy
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
def _render_timeline_image_v2(data: dict, scale: float = 2.5) -> BytesIO | None:
    """商业级时间轴/里程碑图渲染器 v2

    适用于：项目关键里程碑、阶段时间节点、重大事件时间线。
    元素：
    - 横向时间轴
    - 里程碑标记（菱形）+ 名称 + 日期
    - 阶段分组（可选）

    Args:
        scale: 超采样缩放比例（默认 2.5，提供 2.5x 抗锯齿）
    """
    if not isinstance(data, dict):
        return None
    milestones = data.get("milestones") or data.get("events") or data.get("points") or []
    if not isinstance(milestones, list) or not milestones:
        return None

    # 规范化
    norm: list[dict] = []
    for i, m in enumerate(milestones):
        if not isinstance(m, dict):
            continue
        name = str(m.get("name") or m.get("title") or m.get("label") or f"里程碑{i+1}").strip()
        if not name:
            continue
        date = str(m.get("date") or m.get("time") or m.get("day") or "").strip()
        desc = str(m.get("description") or m.get("desc") or "").strip()
        group = str(m.get("group") or m.get("phase") or "").strip()
        norm.append({"name": name, "date": date, "desc": desc, "group": group})

    if not norm:
        return None
    n = len(norm)

    logger.info(
        "[PIL v2渲染] 时间轴渲染启动: 里程碑=%d 分组数=%d scale=%.1f",
        n,
        len(set(m["group"] for m in norm if m["group"])),
        scale,
    )

    # 尺寸（乘以 scale 超采样）
    padding = int(40 * scale)
    title_h = int(60 * scale)
    node_h = int(80 * scale)
    int(6 * scale)
    arrow_w = int(16 * scale)
    label_h = int(60 * scale)
    desc_max = int(50 * scale)

    # 横向布局：宽度根据节点数自适应
    min_node_w = int(180 * scale)
    total_w = max(int(960 * scale), padding * 2 + n * min_node_w + (n - 1) * int(24 * scale))
    total_h = title_h + padding * 2 + node_h + label_h + desc_max

    img = Image.new("RGB", (int(total_w), int(total_h)), "white")
    draw = ImageDraw.Draw(img)
    font_title = _image_font(int(20 * scale), bold=True)
    font_name = _image_font(int(15 * scale), bold=True)
    font_date = _image_font(int(12 * scale))
    font_desc = _image_font(int(11 * scale))
    font_axis = _image_font(int(12 * scale))

    title = str(data.get("title") or "项目关键里程碑时间线")
    draw.text((padding, int(18 * scale)), title, fill="#1E3A5F", font=font_title)

    # 计算每个节点的水平位置
    inner_left = padding + int(30 * scale)
    inner_right = total_w - padding - int(30 * scale)
    if n > 1:
        step = (inner_right - inner_left) / (n - 1)
    else:
        step = 0

    axis_y = title_h + padding + node_h / 2

    # 绘制时间轴主线
    ax_line_width = int(4 * scale)
    draw.line([(inner_left, axis_y), (inner_right, axis_y)], fill="#64748B", width=ax_line_width)
    # 末端箭头（更大更醒目）
    draw.polygon(
        [
            (inner_right, axis_y),
            (inner_right - arrow_w, axis_y - int(10 * scale)),
            (inner_right - arrow_w, axis_y + int(10 * scale)),
        ],
        fill="#64748B",
    )
    # 时间轴阴影
    draw.line(
        [(inner_left, axis_y + int(2 * scale)), (inner_right, axis_y + int(2 * scale))],
        fill="#E5E7EB",
        width=ax_line_width,
    )

    # 阶段分组配色
    group_colors = [
        ("#2B579A", "#EAF2F8"),
        ("#16A34A", "#EAFBEA"),
        ("#D97706", "#FFF7E6"),
        ("#7C3AED", "#F3E8FF"),
        ("#DC2626", "#FEE2E2"),
    ]
    all_groups: list[str] = []
    for m in norm:
        if m["group"] and m["group"] not in all_groups:
            all_groups.append(m["group"])
    logger.info(
        "[PIL v2渲染] 时间轴绘制: 节点=%d 分组=%s 画布=%dx%dpx 时间步长=%.1f",
        n,
        all_groups,
        total_w,
        total_h,
        step if n > 1 else 0,
    )

    # 绘制里程碑
    for i, m in enumerate(norm):
        cx = inner_left if n == 1 else inner_left + i * step

        # 节点位置（上下交错显示）
        is_up = i % 2 == 0
        box_y = axis_y - int(70 * scale) if is_up else axis_y + int(14 * scale)

        # 颜色
        group_idx = (
            all_groups.index(m["group"]) if m["group"] in all_groups else i % len(group_colors)
        )
        border_c, fill_c = group_colors[group_idx % len(group_colors)]

        # 连接线（从 box 到 axis）
        if is_up:
            line_y_top = box_y + int(56 * scale)
            line_y_bottom = axis_y - int(6 * scale)
        else:
            line_y_top = axis_y + int(6 * scale)
            line_y_bottom = box_y
        draw.line(
            [(cx, line_y_top), (cx, line_y_bottom)],
            fill=border_c,
            width=int(2 * scale),
            joint="curve",
        )

        # 圆角矩形（里程碑卡片）带阴影
        box_w = min_node_w - int(10 * scale)
        box_x = cx - box_w / 2
        shadow_offset = int(3 * scale)
        card_h = int(56 * scale)
        radius = int(6 * scale)
        draw.rounded_rectangle(
            [
                box_x + shadow_offset,
                box_y + shadow_offset,
                box_x + box_w + shadow_offset,
                box_y + card_h + shadow_offset,
            ],
            radius=radius,
            fill="#D1D5DB",
            outline=None,
        )
        draw.rounded_rectangle(
            [box_x, box_y, box_x + box_w, box_y + card_h],
            radius=radius,
            fill=fill_c,
            outline=border_c,
            width=int(2 * scale),
        )
        # 名称
        pad = int(6 * scale)
        _draw_text_center(
            draw,
            (box_x + pad, box_y + int(4 * scale), box_x + box_w - pad, box_y + int(22 * scale)),
            m["name"],
            font_name,
            border_c,
            max_lines=1,
        )
        # 日期
        if m["date"]:
            _draw_text_center(
                draw,
                (
                    box_x + pad,
                    box_y + int(22 * scale),
                    box_x + box_w - pad,
                    box_y + int(38 * scale),
                ),
                m["date"],
                font_date,
                "#1E3A5F",
                max_lines=1,
            )
        # 描述
        if m["desc"]:
            _draw_text_center(
                draw,
                (
                    box_x + pad,
                    box_y + int(38 * scale),
                    box_x + box_w - pad,
                    box_y + int(54 * scale),
                ),
                m["desc"],
                font_desc,
                "#374151",
                max_lines=1,
            )

        # 菱形里程碑节点（位于轴上，带阴影）
        d = int(12 * scale)
        # 阴影
        draw.polygon(
            [
                cx + shadow_offset,
                axis_y - d + shadow_offset,
                cx + d + shadow_offset,
                axis_y + shadow_offset,
                cx + shadow_offset,
                axis_y + d + shadow_offset,
                cx - d + shadow_offset,
                axis_y + shadow_offset,
            ],
            fill="#D1D5DB",
            outline=None,
        )
        # 主体
        draw.polygon(
            [
                (cx, axis_y - d),
                (cx + d, axis_y),
                (cx, axis_y + d),
                (cx - d, axis_y),
            ],
            fill="#FBBF24",
            outline="#92400E",
            width=int(3 * scale),
        )
        # 节点编号
        bbox = draw.textbbox((0, 0), str(i + 1), font=font_axis)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        draw.text((cx - tw / 2, axis_y - th / 2), str(i + 1), fill="#7C2D12", font=font_axis)

    # ---- 图例（阶段分组）----
    if all_groups:
        legend_y = total_h - int(30 * scale)
        legend_x = padding
        draw.text((legend_x, legend_y - int(4 * scale)), "阶段：", fill="#1E3A5F", font=font_axis)
        legend_x += int(50 * scale)
        for g in all_groups:
            gi = all_groups.index(g) % len(group_colors)
            border_c, fill_c = group_colors[gi]
            draw.rectangle(
                [legend_x, legend_y, legend_x + int(14 * scale), legend_y + int(12 * scale)],
                fill=fill_c,
                outline=border_c,
                width=int(1 * scale),
            )
            draw.text(
                (legend_x + int(18 * scale), legend_y - int(2 * scale)),
                g,
                fill="#1E3A5F",
                font=font_axis,
            )
            bbox = draw.textbbox((0, 0), g, font=font_axis)
            legend_x += bbox[2] - bbox[0] + int(32 * scale)
            if legend_x + int(100 * scale) > total_w:
                break

    result_stream = _image_to_stream(img)
    size_kb = len(result_stream.getvalue()) // 1024
    logger.info(
        "[PIL v2渲染] 时间轴渲染完成: 里程碑=%d 分组=%d 尺寸=%dKB DPI=300",
        len(norm),
        len(all_groups),
        size_kb,
    )
    return result_stream

def _repair_timeline_data(data: dict) -> dict | None:
    """修复时间轴数据：补齐缺失字段，确保渲染器能处理

    Args:
        data: 原始时间轴数据

    Returns:
        修复后的数据字典，或 None 如果无法修复
    """
    if not isinstance(data, dict):
        return None
    repaired = copy.deepcopy(data)

    # 保证 milestones 存在
    if not repaired.get("milestones"):
        repaired["milestones"] = [
            {
                "name": "项目开工",
                "date": "第1天",
                "description": "项目正式开工",
                "status": "completed",
            },
            {
                "name": "基础完成",
                "date": "第30天",
                "description": "基础工程验收",
                "status": "completed",
            },
            {
                "name": "主体封顶",
                "date": "第75天",
                "description": "主体结构封顶",
                "status": "in_progress",
            },
            {
                "name": "竣工验收",
                "date": "第120天",
                "description": "项目整体竣工验收",
                "status": "pending",
            },
        ]

    # 补齐每个 milestone 的缺失字段
    for i, m in enumerate(repaired.get("milestones", [])):
        if not isinstance(m, dict):
            continue
        if not m.get("name"):
            m["name"] = m.get("title", f"里程碑{i+1}")
        if not m.get("date"):
            m["date"] = f"第{(i+1)*30}天"
        if not m.get("status"):
            m["status"] = "pending"
        if not m.get("description"):
            m["description"] = ""

    if not repaired.get("title"):
        repaired["title"] = "项目关键里程碑时间线"

    return repaired

