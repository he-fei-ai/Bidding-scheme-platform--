"""Architecture 图表渲染器。

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



# BUG-FIX 2026-09-14（高）：AI 生成的架构图节点数/层级不可控。
# 原实现对递归深度与节点总数均无上限，200+ 节点时画布宽达数万像素，
# 触发 PIL DecompressionBombWarning、内存峰值数百 MB，导出进程直接 OOM。
# 这里设置硬上限：超出部分直接丢弃（宁可少画，也不能把进程拖死）。
_MAX_ARCH_NODES = 120
_MAX_ARCH_DEPTH = 10
_MAX_ARCH_CANVAS_PX = 6000

# L3 叶子层竖排阈值：叶子节点数 > 此值时自动启用竖排布局
_VERTICAL_THRESHOLD = 6

# L3 竖排布局参数
_L3_VERTICAL_W = 60
_L3_VERTICAL_H = 200
_L3_VERTICAL_GAP = 8
_L3_HORIZONTAL_W = 220
_L3_HORIZONTAL_H = 52
_L3_HORIZONTAL_GAP = 24

# L2 中间节点在竖排模式下适度收窄（220→160），进一步压缩画布宽度
_L2_VERTICAL_W = 160


def _count_leaves(node: dict) -> int:
    """递归统计叶子节点数量。"""
    children = (
        node.get("children")
        or node.get("nodes")
        or node.get("sub_units")
        or node.get("branches")
        or []
    )
    if not isinstance(children, list) or len(children) == 0:
        return 1
    return sum(_count_leaves(c) for c in children if isinstance(c, dict))


def _draw_text_vertical(draw, box, text: str, font, fill: str, letter_spacing: int = 2):
    """在指定矩形内垂直绘制文字（从上到下，字符保持直立）。

    模拟 CSS writing-mode: vertical-rl + text-orientation: upright 的效果。
    """
    x1, y1, x2, y2 = box
    text = str(text or "").strip()
    if not text:
        return

    # 计算单字符尺寸
    test_bbox = draw.textbbox((0, 0), "国", font=font)
    char_w = max(1, test_bbox[2] - test_bbox[0])
    char_h = max(1, test_bbox[3] - test_bbox[1])

    # 可用区域（留边距）
    usable_w = max(1, x2 - x1 - 8)
    usable_h = max(1, y2 - y1 - 8)

    # 计算可显示的字符数（垂直方向）
    max_chars = max(1, int(usable_h / (char_h + letter_spacing)))

    # 分列显示：如果文本太长，分多列
    lines = []
    current = ""
    for char in text:
        current += char
        if len(current) >= max_chars:
            lines.append(current)
            current = ""
    if current:
        lines.append(current)

    # 如果只有一列，居中显示
    num_cols = len(lines)
    total_text_w = num_cols * (char_w + letter_spacing)
    col_x = x1 + max(0, (x2 - x1 - total_text_w) / 2)

    for col_idx, line in enumerate(lines):
        # 计算列的垂直居中位置
        line_h = len(line) * (char_h + letter_spacing) - letter_spacing
        col_y = y1 + max(0, (y2 - y1 - line_h) / 2)

        for char_idx, char in enumerate(line):
            if char == ' ':
                col_y += char_h * 0.5
                continue
            bbox = draw.textbbox((0, 0), char, font=font)
            cw = bbox[2] - bbox[0]
            ch = bbox[3] - bbox[1]
            draw.text(
                (col_x + (char_w - cw) / 2, col_y + char_idx * (char_h + letter_spacing)),
                char,
                fill=fill,
                font=font,
            )

    # 如果文本被截断，添加省略号
    if len(lines) * max_chars < len(text):
        last_line = lines[-1]
        if len(last_line) < max_chars:
            last_line += "…"
            lines[-1] = last_line


def _layout_architecture_tree(root: dict, vertical_l3: bool = False) -> tuple[list[dict], int, int]:
    """为架构树节点计算坐标（自顶向下、横向展开）。

    vertical_l3=True 时，L3 叶子节点使用竖排布局（窄高矩形，文字垂直排列）。
    """
    nodes_pos: list[dict] = []
    leaf_counter = [0]

    def _dfs(node: dict, depth: int, x_start: float, parent_id: str | None = None) -> float:
        """DFS 返回该子树的 x 结束位置（独占宽度）。"""
        if depth > _MAX_ARCH_DEPTH:
            return x_start

        label = str(node.get("label") or node.get("name") or "").strip()
        nid = str(node.get("id") or f"n{leaf_counter[0]}")
        leaf_counter[0] += 1

        children = (
            node.get("children")
            or node.get("nodes")
            or node.get("sub_units")
            or node.get("branches")
            or []
        )
        if not isinstance(children, list):
            children = []

        is_leaf = len(children) == 0

        # 根据节点类型和布局模式决定尺寸
        if is_leaf and depth >= 2:
            # L3 及更深层叶子：竖排/横排两套配置
            if vertical_l3:
                node_w, node_h, gap = _L3_VERTICAL_W, _L3_VERTICAL_H, _L3_VERTICAL_GAP
            else:
                node_w, node_h, gap = _L3_HORIZONTAL_W, _L3_HORIZONTAL_H, _L3_HORIZONTAL_GAP
        elif vertical_l3 and not is_leaf and depth >= 1:
            # 竖排模式下 L2 中间节点适度收窄（220→160），压缩画布宽度
            node_w, node_h, gap = _L2_VERTICAL_W, 52, 24
        else:
            node_w, node_h, gap = 220, 52, 24

        v_gap = 48

        if is_leaf:
            x = x_start
            y = depth * (node_h + v_gap) + 60
            if len(nodes_pos) >= _MAX_ARCH_NODES:
                return x_start
            nodes_pos.append(
                {
                    "id": nid,
                    "label": label,
                    "x": x,
                    "y": y,
                    "w": node_w,
                    "h": node_h,
                    "depth": depth,
                    "has_children": False,
                    "parent_id": parent_id,
                    "is_leaf": True,
                    "vertical": vertical_l3,
                }
            )
            return x + node_w + gap

        # 有子节点：先布局子节点，再居中放置自己
        child_x = x_start
        child_positions: list[tuple[float, float]] = []
        for child in children:
            if not isinstance(child, dict):
                continue
            end_x = _dfs(child, depth + 1, child_x, parent_id=nid)
            child_positions.append((child_x, end_x - child_x - gap))
            child_x = end_x

        if not child_positions:
            x = x_start
            y = depth * (node_h + v_gap) + 60
            if len(nodes_pos) >= _MAX_ARCH_NODES:
                return x_start
            nodes_pos.append(
                {
                    "id": nid,
                    "label": label,
                    "x": x,
                    "y": y,
                    "w": node_w,
                    "h": node_h,
                    "depth": depth,
                    "has_children": False,
                    "parent_id": parent_id,
                    "is_leaf": True,
                    "vertical": vertical_l3,
                }
            )
            return x + node_w + gap

        # 自己的 x = 第一个子节点中心 - 自己宽度的一半
        first_x, first_w = child_positions[0]
        last_x, last_w = child_positions[-1]
        children_center = (first_x + last_x + last_w) / 2
        x = children_center - node_w / 2
        y = depth * (node_h + v_gap) + 60
        if len(nodes_pos) >= _MAX_ARCH_NODES:
            return max(child_x, x + node_w + gap)
        nodes_pos.append(
            {
                "id": nid,
                "label": label,
                "x": x,
                "y": y,
                "w": node_w,
                "h": node_h,
                "depth": depth,
                "has_children": True,
                "parent_id": parent_id,
                "is_leaf": False,
                "vertical": False,
            }
        )
        return max(child_x, x + node_w + gap)

    _dfs(root, 0, 40)

    # 归一化坐标（让最左侧为 0）
    if nodes_pos:
        min_x = min(n["x"] for n in nodes_pos)
        for n in nodes_pos:
            n["x"] -= min_x

    max_x = max((n["x"] + n["w"] for n in nodes_pos), default=0)
    max_y = max((n["y"] + n["h"] for n in nodes_pos), default=0)
    width = int(max_x + 80)
    height = int(max_y + 40)

    # BUG-FIX 2026-09-14（高）：极端宽/深的树会让画布达到数万像素
    # （实测一棵 3900 节点的树 → 23968×N），PIL Image.new 直接吃掉数百 MB
    # 甚至 OOM，且导出的 PNG 大到 Word 打不开。
    # 这里统一收敛到 _MAX_ARCH_CANVAS_PX：先按比例压缩布局坐标（保证所有节点
    # 仍在画布内，不至于被整体裁掉），再夹紧最终画布尺寸。
    if nodes_pos:
        sx = min(1.0, float(_MAX_ARCH_CANVAS_PX - 80) / max(1.0, float(max_x)))
        sy = min(1.0, float(_MAX_ARCH_CANVAS_PX - 40) / max(1.0, float(max_y)))
        if sx < 1.0 or sy < 1.0:
            for n in nodes_pos:
                n["x"] = int(n["x"] * sx)
                n["y"] = int(n["y"] * sy)
                n["w"] = max(40, int(n["w"] * sx))
                n["h"] = max(24, int(n["h"] * sy))
            max_x = max((n["x"] + n["w"] for n in nodes_pos), default=0)
            max_y = max((n["y"] + n["h"] for n in nodes_pos), default=0)
            width = int(max_x + 80)
            height = int(max_y + 40)

    width = max(400, min(int(width), _MAX_ARCH_CANVAS_PX))
    height = max(300, min(int(height), _MAX_ARCH_CANVAS_PX))
    return nodes_pos, width, height

@_render_hq
def _render_architecture_image_v2(data: dict) -> BytesIO | None:
    """商业级架构图渲染器 v2：真正的树形可视化，含父子连线。

    当 L3 叶子节点数 > _VERTICAL_THRESHOLD 时，自动启用竖排布局，
    将画布宽度从横向 ~1600px 缩减至 ~800px，适配 A4 纸宽。
    """
    if not isinstance(data, dict):
        return None
    # 兼容嵌套结构：{"root": {...}} / {"tree": {...}} / {"data": {...}}
    root = data.get("root") or data.get("tree") or data.get("data") or data
    if not isinstance(root, dict):
        return None
    if not (root.get("label") or root.get("name")):
        return None

    # 检测 L3 叶子节点数，决定是否启用竖排
    leaf_count = _count_leaves(root)
    vertical_l3 = leaf_count > _VERTICAL_THRESHOLD
    logger.info(
        "[PIL v2渲染] 叶子节点数=%d, 竖排模式=%s (阈值=%d)",
        leaf_count, vertical_l3, _VERTICAL_THRESHOLD,
    )

    nodes_pos, width, height = _layout_architecture_tree(root, vertical_l3=vertical_l3)
    if not nodes_pos:
        return None
    logger.info(
        "[PIL v2渲染] 架构图渲染启动: 节点数=%d 画布=%dx%dpx",
        len(nodes_pos),
        width,
        height,
    )

    # 高度至少包含标题
    title_h = 56
    height += title_h

    # BUG-FIX 2026-09-14（高）：画布尺寸兜底钳制，避免异常数据导致超大画布 OOM
    width = max(400, min(int(width), _MAX_ARCH_CANVAS_PX))
    height = max(300, min(int(height), _MAX_ARCH_CANVAS_PX))

    # 使用 2.5x 缩放渲染 + @_render_hq 保持原分辨率并添加 300 DPI 元数据
    # 2.5x 渲染 + 不降采样 = 真实超采样抗锯齿，最终 DOCX 插入时 DPI 感知缩放到合适尺寸
    scale = 2.5

    img = Image.new("RGB", (int(width * scale), int(height * scale)), "white")
    draw = ImageDraw.Draw(img)
    font_title = _image_font(int(20 * scale), bold=True)
    font_node = _image_font(int(15 * scale), bold=True)
    _image_font(int(12 * scale))

    # ---- 标题 ----
    title = str(root.get("label") or root.get("name") or "组织架构图")
    draw.text((int(40 * scale), int(16 * scale)), title, fill="#1E3A5F", font=font_title)

    # 缩放节点位置
    for n in nodes_pos:
        n["x"] = int(n["x"] * scale)
        n["y"] = int(n["y"] * scale)
        n["w"] = int(n["w"] * scale)
        n["h"] = int(n["h"] * scale)

    # 根节点识别
    root_id = nodes_pos[0]["id"]
    node_by_id = {n["id"]: n for n in nodes_pos}

    # ---- 绘制连线（先画连线）----
    edge_color = "#64748B"
    for n in nodes_pos:
        if n["id"] == root_id:
            continue
        if n["depth"] == 0:
            continue
        # BUG-FIX-19: 使用 parent_id 直接查找父节点，替代原启发式算法
        # 原实现用"depth-1且x最近的"启发式，对多分支树会找到错误的父节点
        parent = node_by_id.get(n.get("parent_id")) if n.get("parent_id") else None
        if not parent:
            continue
        # 父节点底边中点 -> 子节点顶边中点（折线）
        sx = parent["x"] + parent["w"] / 2
        sy = parent["y"] + parent["h"] + title_h
        ex = n["x"] + n["w"] / 2
        ey = n["y"] + title_h
        mid_y = (sy + ey) / 2
        # 连线阴影
        draw.line(
            [(sx + 2, sy + 2), (sx + 2, mid_y + 2), (ex + 2, mid_y + 2), (ex + 2, ey + 2)],
            fill="#E5E7EB",
            width=int(3 * scale),
        )
        draw.line(
            [(sx, sy), (sx, mid_y), (ex, mid_y), (ex, ey)], fill=edge_color, width=int(2 * scale)
        )
        # 箭头（实心三角形）
        draw.polygon([(ex, ey - 2), (ex - 5, ey - 12), (ex + 5, ey - 12)], fill=edge_color)

    # ---- 绘制节点 ----
    depth_colors = [
        ("#1E3A5F", "#FFFFFF"),  # 根
        ("#2B579A", "#FFFFFF"),  # 一级
        ("#4472C4", "#FFFFFF"),  # 二级
        ("#5B9BD5", "#FFFFFF"),  # 三级
        ("#A8D8EA", "#1E3A5F"),  # 四级
    ]
    for n in nodes_pos:
        depth_idx = min(n["depth"], len(depth_colors) - 1)
        fill_c, text_c = depth_colors[depth_idx]
        x, y, w, h = n["x"], n["y"] + title_h, n["w"], n["h"]
        shadow_offset = 4
        if n["id"] == root_id:
            # 根节点特殊样式：双线粗框 + 阴影
            draw.rounded_rectangle(
                [
                    x - 2 + shadow_offset,
                    y - 2 + shadow_offset,
                    x + w + 2 + shadow_offset,
                    y + h + 2 + shadow_offset,
                ],
                radius=8,
                fill="#D1D5DB",
                outline=None,
            )
            draw.rounded_rectangle(
                [x - 2, y - 2, x + w + 2, y + h + 2],
                radius=8,
                fill="#FBBF24",
                outline="#92400E",
                width=3,
            )
            _draw_text_center(
                draw, (x, y, x + w, y + h), n["label"], font_node, "#1E3A5F", max_lines=2
            )
        elif n["has_children"]:
            # 中间节点带阴影
            draw.rounded_rectangle(
                [
                    x + shadow_offset,
                    y + shadow_offset,
                    x + w + shadow_offset,
                    y + h + shadow_offset,
                ],
                radius=8,
                fill="#D1D5DB",
                outline=None,
            )
            draw.rounded_rectangle(
                [x, y, x + w, y + h], radius=8, fill=fill_c, outline="#1E3A5F", width=int(2 * scale)
            )
            _draw_text_center(
                draw,
                (x + 4, y + 4, x + w - 4, y + h - 4),
                n["label"],
                font_node,
                text_c,
                max_lines=2,
            )
        else:
            # 叶子节点带阴影
            draw.rounded_rectangle(
                [
                    x + shadow_offset,
                    y + shadow_offset,
                    x + w + shadow_offset,
                    y + h + shadow_offset,
                ],
                radius=8,
                fill="#D1D5DB",
                outline=None,
            )
            draw.rounded_rectangle(
                [x, y, x + w, y + h], radius=8, fill=fill_c, outline="#1E3A5F", width=int(1 * scale)
            )
            # 根据布局模式选择文字渲染方式
            if n.get("vertical"):
                # 竖排文字：垂直排列
                font_vert = _image_font(int(14 * scale), bold=False)
                _draw_text_vertical(
                    draw,
                    (x + 4, y + 4, x + w - 4, y + h - 4),
                    n["label"],
                    font_vert,
                    text_c,
                    letter_spacing=int(2 * scale),
                )
            else:
                # 横排文字
                _draw_text_center(
                    draw,
                    (x + 4, y + 4, x + w - 4, y + h - 4),
                    n["label"],
                    font_node,
                    text_c,
                    max_lines=2,
                )

    result_stream = _image_to_stream(img)
    size_kb = len(result_stream.getvalue()) // 1024
    logger.info(
        "[PIL v2渲染] 架构图渲染完成: 节点=%d 尺寸=%dKB DPI=300",
        len(nodes_pos),
        size_kb,
    )
    return result_stream

