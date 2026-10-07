"""Layout 图表渲染器。

由 scripts/_split_mermaid.py 从原 mermaid_renderer.py 自动拆分而来。
所有公共工具从 .mermaid_common 导入。
"""
from __future__ import annotations

import copy
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



@_render_hq
def _render_layout_image_v2(data: dict, title: str = "", scale: float = 2.5) -> BytesIO | None:
    """商业级空间布局图渲染器 v2

    适用于：施工现场总平面布置、临建布置、区域划分。
    元素：
    - 区域（zones）：长方形/圆角矩形，标签为名称 + 用途
    - 连接（connections）：区域之间的连通关系
    - 图例：区域分类（生产区/生活区/材料堆放区/危险品区等）

    Args:
        scale: 超采样缩放比例（默认 2.5，提供 2.5x 抗锯齿）
    """
    if not isinstance(data, dict):
        return None
    zones = data.get("zones") or data.get("areas") or []
    if not isinstance(zones, list) or not zones:
        return None
    logger.info(
        "[PIL v2渲染] 布局图渲染启动: 区域数=%d 标题=%s scale=%.1f",
        len(zones),
        title,
        scale,
    )

    # 规范化区域
    norm_zones: list[dict] = []
    for i, z in enumerate(zones):
        if not isinstance(z, dict):
            continue
        name = str(z.get("name") or z.get("label") or z.get("title") or f"区域{i+1}").strip()
        if not name:
            continue
        # 兼容多种字段：category / type / role
        category = str(z.get("category") or z.get("type") or z.get("role") or "").strip()
        # 兼容多种字段：description / desc / position / size
        desc = str(z.get("description") or z.get("desc") or "").strip()
        position = str(z.get("position") or z.get("pos") or "").strip()
        size = str(z.get("size") or z.get("area") or "").strip()
        color = str(z.get("color") or "").strip()
        # 智能识别分类
        if not category:
            # 根据名称自动推断分类
            name.lower()
            if any(kw in name for kw in ("生活", "宿舍", "食堂", "休息")):
                category = "生活区"
            elif any(kw in name for kw in ("办公", "管理", "会议室")):
                category = "办公区"
            elif any(kw in name for kw in ("材料", "堆场", "仓库", "物资")):
                category = "材料堆放区"
            elif any(kw in name for kw in ("加工", "钢筋", "木工")):
                category = "加工区"
            elif any(kw in name for kw in ("施工", "作业", "现场", "主体")):
                category = "生产区"
            elif any(kw in name for kw in ("危险", "油料", "易燃", "易爆")):
                category = "危险品区"
            elif any(kw in name for kw in ("通道", "道路", "运输")):
                category = "运输通道"
            else:
                category = "default"

        # 自动填充 description（来自 position + size + color）
        desc_parts = []
        if position:
            desc_parts.append(f"位置：{position}")
        if size:
            desc_parts.append(f"面积：{size}")
        if desc:
            desc_parts.append(desc)
        full_desc = " · ".join(desc_parts) if desc_parts else ""

        # 解析自定义颜色
        custom_fill = None
        if color:
            m_color = re.match(r"^#([0-9A-Fa-f]{6})$", color)
            if m_color:
                custom_fill = "#" + m_color.group(1).upper()

        norm_zones.append(
            {
                "name": name,
                "category": category,
                "desc": full_desc,
                "custom_fill": custom_fill,
            }
        )

    if not norm_zones:
        return None

    # 区域分类（自动分配网格位置）
    n = len(norm_zones)
    cols = int(math.ceil(math.sqrt(n * 1.5)))  # 横向稍多一些
    rows = int(math.ceil(n / cols))

    # 区域尺寸（乘以 scale 超采样）
    zone_w = int(220 * scale)
    zone_h = int(110 * scale)
    h_gap = int(32 * scale)
    v_gap = int(48 * scale)
    padding = int(40 * scale)
    title_h = int(60 * scale)
    legend_h = int(60 * scale)

    logger.info(
        "[PIL v2渲染] 布局图布局计算: 区域=%d 网格=%dx%d 区域尺寸=%dx%dpx scale=%.1f",
        n,
        cols,
        rows,
        zone_w,
        zone_h,
        scale,
    )

    total_w = padding * 2 + cols * zone_w + (cols - 1) * h_gap
    total_h = title_h + padding * 2 + rows * zone_h + (rows - 1) * v_gap + legend_h

    img = Image.new("RGB", (total_w, int(total_h)), "white")
    draw = ImageDraw.Draw(img)
    font_title = _image_font(int(20 * scale), bold=True)
    font_zone = _image_font(int(16 * scale), bold=True)
    font_desc = _image_font(int(12 * scale))
    font_legend = _image_font(int(12 * scale))

    # 标题
    title = str(data.get("title") or "施工现场总平面布置图")
    draw.text((padding, int(18 * scale)), title, fill="#1E3A5F", font=font_title)

    # 区域分类配色
    category_colors = {
        "生产区": ("#EAF2F8", "#2B579A"),
        "生活区": ("#FFF7E6", "#D97706"),
        "办公区": ("#EAFBEA", "#16A34A"),
        "材料堆放区": ("#FEF3C7", "#CA8A04"),
        "加工区": ("#F3E8FF", "#7C3AED"),
        "危险品区": ("#FEE2E2", "#DC2626"),
        "临时设施": ("#DBEAFE", "#2563EB"),
        "运输通道": ("#F1F5F9", "#475569"),
        "default": ("#F1F5F9", "#1E3A5F"),
    }

    # 收集所有出现的分类
    all_categories: list[str] = []
    for z in norm_zones:
        if z["category"] not in all_categories:
            all_categories.append(z["category"])

    # 绘制区域
    zone_positions: list[dict] = []
    for i, z in enumerate(norm_zones):
        row = i // cols
        col = i % cols
        x = padding + col * (zone_w + h_gap)
        y = title_h + padding + row * (zone_h + v_gap)
        default_fill, border_c = category_colors.get(z["category"], category_colors["default"])
        fill_c = z.get("custom_fill") or default_fill
        shadow_offset = int(5 * scale)
        radius = int(10 * scale)
        # 阴影
        draw.rounded_rectangle(
            [
                x + shadow_offset,
                y + shadow_offset,
                x + zone_w + shadow_offset,
                y + zone_h + shadow_offset,
            ],
            radius=radius,
            fill="#D1D5DB",
            outline=None,
        )
        # 主体圆角矩形
        draw.rounded_rectangle(
            [x, y, x + zone_w, y + zone_h],
            radius=radius,
            fill=fill_c,
            outline=border_c,
            width=int(2 * scale),
        )
        # 区域名称
        pad = int(8 * scale)
        _draw_text_center(
            draw,
            (x + pad, y + int(12 * scale), x + zone_w - pad, y + int(36 * scale)),
            z["name"],
            font_zone,
            border_c,
            max_lines=1,
        )
        # 描述（位置/面积/自定义）
        if z["desc"]:
            _draw_text_center(
                draw,
                (x + pad, y + int(40 * scale), x + zone_w - pad, y + zone_h - pad),
                z["desc"],
                font_desc,
                "#1E3A5F",
                max_lines=3,
            )
        else:
            # 显示分类
            _draw_text_center(
                draw,
                (x + pad, y + int(50 * scale), x + zone_w - pad, y + zone_h - pad),
                f"【{z['category']}】",
                font_desc,
                "#1E3A5F",
                max_lines=1,
            )
        zone_positions.append(
            {
                "x": x,
                "y": y,
                "w": zone_w,
                "h": zone_h,
                "center_x": x + zone_w / 2,
                "center_y": y + zone_h / 2,
                "name": z["name"],
            }
        )

    # 绘制连接（如果指定了 connections）
    connections = data.get("connections") or []
    if isinstance(connections, list):
        # 构建名称 → 索引的映射
        name_to_idx: dict[str, int] = {zp["name"]: i for i, zp in enumerate(zone_positions)}
        for c in connections:
            if not isinstance(c, dict):
                continue
            from_idx = -1
            to_idx = -1
            # 支持 from/to 为索引数字 或 名称
            raw_from = c.get("from", -1)
            raw_to = c.get("to", -1)
            if isinstance(raw_from, int):
                from_idx = raw_from
            elif isinstance(raw_from, str):
                from_idx = name_to_idx.get(raw_from, -1)
                if from_idx < 0:
                    try:
                        from_idx = int(raw_from)
                    except (TypeError, ValueError):
                        from_idx = -1
            if isinstance(raw_to, int):
                to_idx = raw_to
            elif isinstance(raw_to, str):
                to_idx = name_to_idx.get(raw_to, -1)
                if to_idx < 0:
                    try:
                        to_idx = int(raw_to)
                    except (TypeError, ValueError):
                        to_idx = -1

            if 0 <= from_idx < len(zone_positions) and 0 <= to_idx < len(zone_positions):
                a, b = zone_positions[from_idx], zone_positions[to_idx]
                # 连线阴影
                shadow_offset = int(2 * scale)
                draw.line(
                    [
                        (a["center_x"] + shadow_offset, a["center_y"] + shadow_offset),
                        (b["center_x"] + shadow_offset, b["center_y"] + shadow_offset),
                    ],
                    fill="#E5E7EB",
                    width=int(3 * scale),
                )
                draw.line(
                    [(a["center_x"], a["center_y"]), (b["center_x"], b["center_y"])],
                    fill="#94A3B8",
                    width=int(2 * scale),
                )
                # 箭头（实心三角形）
                dx = b["center_x"] - a["center_x"]
                dy = b["center_y"] - a["center_y"]
                length = math.sqrt(dx * dx + dy * dy)
                if length > 0:
                    ux, uy = dx / length, dy / length
                    # 箭头在 b 的边缘
                    arrow_tip = (b["center_x"] - ux * zone_w / 2, b["center_y"] - uy * zone_h / 2)
                    arrow_back = (
                        arrow_tip[0] - ux * int(14 * scale),
                        arrow_tip[1] - uy * int(14 * scale),
                    )
                    # 垂直于连线方向
                    perp_x, perp_y = -uy, ux
                    p1 = (
                        arrow_back[0] + perp_x * int(7 * scale),
                        arrow_back[1] + perp_y * int(7 * scale),
                    )
                    p2 = (
                        arrow_back[0] - perp_x * int(7 * scale),
                        arrow_back[1] - perp_y * int(7 * scale),
                    )
                    draw.polygon([arrow_tip, p1, p2], fill="#475569")

    # ---- 图例 ----
    legend_y = total_h - legend_h + int(12 * scale)
    legend_x = padding
    draw.text((legend_x, legend_y - int(4 * scale)), "图例：", fill="#1E3A5F", font=font_legend)
    legend_x += int(50 * scale)
    for cat in all_categories:
        fill_c, border_c = category_colors.get(cat, category_colors["default"])
        draw.rectangle(
            [legend_x, legend_y, legend_x + int(16 * scale), legend_y + int(14 * scale)],
            fill=fill_c,
            outline=border_c,
            width=int(1 * scale),
        )
        draw.text(
            (legend_x + int(20 * scale), legend_y - int(2 * scale)),
            cat,
            fill="#1E3A5F",
            font=font_legend,
        )
        bbox = draw.textbbox((0, 0), cat, font=font_legend)
        legend_x += bbox[2] - bbox[0] + int(36 * scale)
        if legend_x + int(100 * scale) > total_w:
            legend_x = padding + int(50 * scale)
            legend_y += int(20 * scale)

    size_kb = len(img.tobytes()) // 1024
    logger.info(
        "[PIL v2渲染] 布局图渲染完成: 区域=%d 网格=%dx%d 尺寸=%dKB DPI=300",
        len(zones),
        cols,
        rows,
        size_kb,
    )
    return _image_to_stream(img)

def _repair_layout_data(data: dict) -> dict | None:
    """修复布局图数据：补齐缺失字段，确保渲染器能处理

    Args:
        data: 原始布局图数据

    Returns:
        修复后的数据字典，或 None 如果无法修复
    """
    if not isinstance(data, dict):
        return None
    repaired = copy.deepcopy(data)

    # 保证 zones 存在
    if not repaired.get("zones"):
        if repaired.get("areas"):
            repaired["zones"] = repaired["areas"]
        else:
            repaired["zones"] = [
                {
                    "id": "Z1",
                    "name": "办公区",
                    "category": "办公区",
                    "x": 5,
                    "y": 5,
                    "w": 20,
                    "h": 20,
                    "color": "#DDA0DD",
                },
                {
                    "id": "Z2",
                    "name": "生活区",
                    "category": "生活区",
                    "x": 30,
                    "y": 5,
                    "w": 20,
                    "h": 20,
                    "color": "#FFE4B5",
                },
                {
                    "id": "Z3",
                    "name": "材料堆场",
                    "category": "材料堆放区",
                    "x": 55,
                    "y": 5,
                    "w": 20,
                    "h": 20,
                    "color": "#87CEEB",
                },
                {
                    "id": "Z4",
                    "name": "加工区",
                    "category": "加工区",
                    "x": 5,
                    "y": 35,
                    "w": 20,
                    "h": 25,
                    "color": "#90EE90",
                },
                {
                    "id": "Z5",
                    "name": "主体施工区",
                    "category": "生产区",
                    "x": 30,
                    "y": 35,
                    "w": 45,
                    "h": 25,
                    "color": "#FFB6C1",
                },
            ]

    # 补齐每个 zone 的缺失字段
    for i, zone in enumerate(repaired.get("zones", [])):
        if not isinstance(zone, dict):
            continue
        if not zone.get("id"):
            zone["id"] = f"Z{i+1}"
        if not zone.get("x"):
            zone["x"] = 5 + (i % 3) * 30
        if not zone.get("y"):
            zone["y"] = 5 + (i // 3) * 30
        if not zone.get("w"):
            zone["w"] = 25
        if not zone.get("h"):
            zone["h"] = 20
        if not zone.get("color"):
            colors = ["#DDA0DD", "#FFE4B5", "#87CEEB", "#90EE90", "#FFB6C1", "#FFD700", "#98FB98"]
            zone["color"] = colors[i % len(colors)]
        if not zone.get("name"):
            zone["name"] = f"区域{i+1}"
        if not zone.get("category"):
            zone["category"] = "其他"

    # 修复 x/y/w/h 超出范围
    for zone in repaired.get("zones", []):
        if isinstance(zone, dict):
            for key in ("x", "y", "w", "h"):
                try:
                    val = int(zone.get(key, 0))
                    zone[key] = max(0, min(100, val))
                except (TypeError, ValueError):
                    zone[key] = 10

    if not repaired.get("title"):
        repaired["title"] = "施工现场总平面布置图"

    return repaired

