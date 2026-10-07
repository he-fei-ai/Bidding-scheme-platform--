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


def _layout_architecture_tree(root: dict) -> tuple[list[dict], int, int]:
    """为架构树节点计算坐标（自顶向下、横向展开）。"""
    nodes_pos: list[dict] = []
    leaf_counter = [0]

    def _dfs(node: dict, depth: int, x_start: float, parent_id: str | None = None) -> float:
        """DFS 返回该子树的 x 结束位置（独占宽度）。"""
        # 深度护栏：超限即停止展开，防止递归过深
        if depth > _MAX_ARCH_DEPTH:
            return x_start
        node_w = 220
        node_h = 52
        h_gap = 24
        v_gap = 48
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

        if not children:
            # 叶子节点：自己占用一个位置
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
                }
            )
            return x + node_w + h_gap

        # 有子节点：先布局子节点，再居中放置自己
        child_x = x_start
        child_positions: list[float] = []
        for child in children:
            if not isinstance(child, dict):
                continue
            end_x = _dfs(child, depth + 1, child_x, parent_id=nid)
            child_positions.append((child_x, end_x - child_x - h_gap))
            child_x = end_x

        if not child_positions:
            # 过滤后无有效子节点
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
                }
            )
            return x + node_w + h_gap

        # 自己的 x = 第一个子节点中心 - 自己宽度的一半
        first_x, first_w = child_positions[0]
        last_x, last_w = child_positions[-1]
        children_center = (first_x + last_x + last_w) / 2
        x = children_center - node_w / 2
        y = depth * (node_h + v_gap) + 60
        if len(nodes_pos) >= _MAX_ARCH_NODES:
            return max(child_x, x + node_w + h_gap)
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
            }
        )
        return max(child_x, x + node_w + h_gap)

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
    """商业级架构图渲染器 v2：真正的树形可视化，含父子连线。"""
    if not isinstance(data, dict):
        return None
    # 兼容嵌套结构：{"root": {...}} / {"tree": {...}} / {"data": {...}}
    root = data.get("root") or data.get("tree") or data.get("data") or data
    if not isinstance(root, dict):
        return None
    if not (root.get("label") or root.get("name")):
        return None

    nodes_pos, width, height = _layout_architecture_tree(root)
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

