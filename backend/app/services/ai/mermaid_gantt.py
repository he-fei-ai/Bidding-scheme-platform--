"""Gantt 图表渲染器。

由 scripts/_split_mermaid.py 从原 mermaid_renderer.py 自动拆分而来。
所有公共工具从 .mermaid_common 导入。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
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



def _parse_mermaid_gantt(mermaid_code: str) -> list[dict] | None:
    """解析 Mermaid 甘特图代码，提取任务及真实起止日期（相对天数偏移）。

    相比旧实现（按任务顺序伪造 5 天间隔），这里解析 ``dateFormat`` 以及
    每行任务中的真实开始日期与工期，计算相对最早日期的天数偏移，使导出的
    横道图与实际进度计划保持一致。
    """
    date_format = "%Y-%m-%d"
    for line in mermaid_code.split("\n"):
        m = re.search(r"dateFormat\s+(\S+)", line)
        if m:
            fmt = m.group(1)
            if "YYYY" in fmt or "%Y" in fmt:
                date_format = "%Y-%m-%d" if "-" in fmt else "%Y/%m/%d"
            elif "YY" in fmt:
                date_format = "%y-%m-%d"
            break

    raw_tasks: list[dict] = []
    current_section = ""
    for line in mermaid_code.strip().split("\n"):
        line = line.strip()
        if line.startswith("section "):
            current_section = line[len("section ") :].strip()
            continue
        if ":" not in line or any(line.startswith(k) for k in ("date", "title", "axis")):
            continue
        name, _, spec = line.partition(":")
        name = name.strip()
        spec = spec.strip()
        if not name:
            continue
        tokens = [t.strip() for t in spec.split(",")]
        start_str = None
        duration_days = None
        for tok in tokens:
            if re.match(r"^\d{2,4}[-/]\d{1,2}[-/]\d{1,2}$", tok):
                start_str = tok
            elif re.match(r"^\d+[dwmy]$", tok.lower()):
                unit_mul = {"d": 1, "w": 7, "m": 30, "y": 365}
                duration_days = int(tok[:-1]) * unit_mul[tok.lower()[-1]]
        raw_tasks.append(
            {
                "section": current_section,
                "task": name,
                "spec": spec,
                "start_str": start_str,
                "duration": duration_days,
            }
        )

    if not raw_tasks:
        return None

    parsed = []
    for rt in raw_tasks:
        sd = None
        if rt["start_str"]:
            for fmt in (date_format, "%Y-%m-%d", "%Y/%m/%d"):
                try:
                    sd = datetime.strptime(rt["start_str"], fmt).date()
                    break
                except ValueError:
                    continue
        parsed.append({**rt, "start_date": sd})

    dated = [p["start_date"] for p in parsed if p["start_date"]]
    min_date = min(dated) if dated else None

    cursor = 0
    result = []
    for p in parsed:
        if p["start_date"] and min_date is not None:
            start_off = (p["start_date"] - min_date).days + 1
        else:
            # 未给出日期（如 after X 依赖）则顺序顺延，避免重叠
            start_off = cursor + 1
        dur = p["duration"] if p["duration"] else 5
        end_off = start_off + dur - 1
        cursor = max(cursor, end_off)
        result.append(
            {
                "section": p["section"],
                "task": p["task"],
                "time": p["spec"],
                "start": start_off,
                "end": end_off,
            }
        )
    return result if result else None

@_render_hq
def _render_gantt_image_v2(plan: dict, highlight_critical_path: bool = False) -> BytesIO | None:
    """商业级甘特图渲染器 v2

    适用：
    - Mermaid gantt 解析后的任务列表
    - 直接传入的 GanttPlan JSON

    特性：
    - 自动归一化时间轴
    - 真实依赖关系连线
    - 里程碑节点特殊标识（橙色菱形）
    - 多色任务条（按阶段分组）
    - 关键路径高亮（highlight_critical_path=True 时启用红色填充+红色边框）
    - 工日柱状 + 文本双信息
    - 优化：批量渐变绘制，避免逐行循环

    Args:
        highlight_critical_path: 是否启用关键路径高亮（红色填充+红色依赖线）
    """
    if not isinstance(plan, dict):
        return None

    # 提取任务
    tasks = plan.get("tasks", [])
    logger.info(
        "[PIL v2渲染] 甘特图渲染启动: 任务数=%d 总工期=%d 依赖数=%d highlight_critical_path=%s",
        len(tasks),
        plan.get("duration", 0),
        len([t for t in tasks if t.get("dependencies")]),
        highlight_critical_path,
    )
    raw_tasks = plan.get("tasks") or []
    if not raw_tasks and "items" in plan:
        raw_tasks = plan["items"]

    if not isinstance(raw_tasks, list) or not raw_tasks:
        return None

    # 规范化
    tasks: list[dict] = []
    for idx, t in enumerate(raw_tasks[:30]):
        if not isinstance(t, dict):
            continue
        name = str(t.get("name") or t.get("task") or t.get("title") or "").strip()
        if not name:
            continue
        try:
            start_v = int(t.get("start") or t.get("start_day") or t.get("from") or 1)
        except (TypeError, ValueError):
            start_v = 1
        try:
            end_v = int(t.get("end") or t.get("end_day") or t.get("to") or start_v)
        except (TypeError, ValueError):
            end_v = start_v
        is_milestone = bool(t.get("isMilestone")) or (start_v == end_v)
        deps = t.get("dependencies") or []
        if not isinstance(deps, list):
            deps = []
        # 关键路径标记
        is_critical = bool(t.get("critical"))
        tasks.append(
            {
                "id": t.get("id") or (idx + 1),
                "name": name,
                "start": start_v,
                "end": max(start_v, end_v),
                "isMilestone": is_milestone,
                "dependencies": deps,
                "critical": is_critical,
            }
        )

    if not tasks:
        return None

    # 总工期
    total_days = plan.get("totalDays") or plan.get("duration") or 0
    try:
        total_days = int(total_days)
    except (TypeError, ValueError):
        total_days = 0
    max_end = max((t["end"] for t in tasks), default=0)
    if total_days < max_end:
        total_days = max_end
    if total_days <= 0:
        total_days = max_end or 90

    # ---- 尺寸 ----
    scale = 2.5
    table_left = int(50 * scale)
    table_top = int(70 * scale)
    row_h = int(48 * scale)
    header_h = int(80 * scale)
    left_widths = [int(w * scale) for w in [60, 360, 80, 80, 80]]
    axis_w = int(1100 * scale)
    table_w = sum(left_widths) + axis_w

    # 备注区域（仅非关键路径模式有备注）
    remarks = [t.get("remark") for t in tasks if t.get("remark")][:4]
    remarks_h = int(min(80, 30 + len(remarks) * 18) * scale) if remarks else 0

    # 图例区域
    legend_h = int(50 * scale) if highlight_critical_path else int(40 * scale)

    width = table_left * 2 + table_w
    height = table_top + header_h + len(tasks) * row_h + remarks_h + legend_h + int(30 * scale)

    # 使用 2.5x 缩放渲染 + @_render_hq 保持原分辨率并添加 300 DPI 元数据
    # 2.5x 渲染 + 不降采样 = 真实超采样抗锯齿，最终 DOCX 插入时 DPI 感知缩放到合适尺寸
    # 注意：width/height 已按 scale 预缩放（上方各尺寸分量均已 * scale），此处不得二次缩放，
    # 否则画布比内容大 2.5 倍（右侧/下方大片空白）且内存膨胀 6.25 倍（曾触发 PIL DecompressionBombWarning）
    sw, sh = int(width), int(height)
    img = Image.new("RGB", (sw, sh), "white")
    draw = ImageDraw.Draw(img)
    font_header = _image_font(int(22 * scale), bold=True)
    font_title = _image_font(int(26 * scale), bold=True)
    font_body = _image_font(int(20 * scale))
    font_small = _image_font(int(17 * scale))
    font_axis = _image_font(int(18 * scale))

    # ---- 配色 ----
    border = "#6B7280"
    grid = "#D1D5DB"
    header_fill = "#EAF2F8"
    alt_fill = "#F8FAFC"
    text = "#111827"
    palette = [
        "#2B579A",
        "#E8A33D",
        "#70AD47",
        "#C00000",
        "#7030A0",
        "#4472C4",
        "#9966CC",
        "#5B9BD5",
        "#DC2626",
        "#16A34A",
    ]
    # 关键路径配色
    critical_outline = "#DC2626"

    # ---- 标题 ----
    title = str(plan.get("title") or "施工进度计划")
    draw.text((table_left, int(22 * scale)), title, fill="#1E3A5F", font=font_title)
    # 总工期标识
    total_label = f"总工期：{total_days}天"
    bbox = draw.textbbox((0, 0), total_label, font=font_small)
    tw = bbox[2] - bbox[0]
    draw.text(
        (width - table_left - tw, int(30 * scale)), total_label, fill="#1E3A5F", font=font_small
    )

    # ---- 表头 ----
    x = table_left
    left_headers = ["#", "工作内容", "开始", "完成", "工期(天)"]
    for col, col_w in zip(left_headers, left_widths, strict=False):
        draw.rectangle(
            [x, table_top, x + col_w, table_top + header_h],
            fill=header_fill,
            outline=border,
            width=int(2 * scale),
        )
        _draw_text_center(
            draw,
            (x + int(4 * scale), table_top, x + col_w - int(4 * scale), table_top + header_h),
            col,
            font_header,
            text,
            max_lines=1,
        )
        x += col_w

    # ---- 时间轴头部 ----
    axis_left = table_left + sum(left_widths)
    draw.rectangle(
        [axis_left, table_top, axis_left + axis_w, table_top + header_h],
        fill=header_fill,
        outline=border,
        width=int(2 * scale),
    )
    _draw_text_center(
        draw,
        (axis_left, table_top + int(4 * scale), axis_left + axis_w, table_top + int(30 * scale)),
        f"施工进度时间轴（{total_days}天）",
        font_header,
        text,
        max_lines=1,
    )

    # ---- 时间刻度 ----
    marks = _build_gantt_marks(total_days, max(1, total_days // 12), int(18 * scale))
    axis_label_top = table_top + int(36 * scale)
    for mark in marks:
        px = axis_left + int((mark / total_days) * axis_w)
        draw.line([px, axis_label_top, px, table_top + header_h], fill=border, width=int(1 * scale))
        bbox = draw.textbbox((0, 0), str(mark), font=font_axis)
        tw_m = bbox[2] - bbox[0]
        draw.text(
            (px - tw_m / 2, axis_label_top + int(8 * scale)), str(mark), fill=text, font=font_axis
        )

    # ---- 任务行 ----
    y = table_top + header_h
    task_pos: dict[int, dict] = {}
    # BUG-FIX 2026-09-14：渐变已改为行内就地绘制，不再需要收集区域（原批量绘制
    # 晚于文本绘制，会把起止日期文字覆盖）。保留变量仅为兼容后续可能的扩展。
    task_bar_regions: list[dict] = []

    for row_index, task in enumerate(tasks, start=1):
        row_fill = alt_fill if row_index % 2 == 0 else "white"
        draw.rectangle(
            [table_left, y, table_left + table_w, y + row_h],
            fill=row_fill,
            outline=grid,
            width=int(1 * scale),
        )

        # 单元格
        x = table_left
        cells = [
            str(row_index),
            task["name"],
            str(task["start"]),
            str(task["end"]),
            f"{task['end'] - task['start'] + 1}天",
        ]
        for value, col_w in zip(cells, left_widths, strict=False):
            draw.rectangle([x, y, x + col_w, y + row_h], outline=grid, width=int(1 * scale))
            _draw_text_center(
                draw,
                (
                    x + int(6 * scale),
                    y + int(4 * scale),
                    x + col_w - int(6 * scale),
                    y + row_h - int(4 * scale),
                ),
                value,
                font_body,
                text,
                max_lines=2,
            )
            x += col_w

        # 网格线
        for mark in marks:
            px = axis_left + int((mark / total_days) * axis_w)
            draw.line([px, y, px, y + row_h], fill=grid, width=int(1 * scale))

        # 任务条
        start_x = axis_left + int(((task["start"] - 1) / total_days) * axis_w)
        end_x = axis_left + int((task["end"] / total_days) * axis_w)

        if task["isMilestone"]:
            # 里程碑：橙色菱形（带阴影）
            d = int(12 * scale)
            cx = (start_x + end_x) / 2
            cy = y + row_h / 2
            shadow_offset = int(3 * scale)
            draw.polygon(
                [
                    (cx + shadow_offset, cy - d + shadow_offset),
                    (cx + d + shadow_offset, cy + shadow_offset),
                    (cx + shadow_offset, cy + d + shadow_offset),
                    (cx - d + shadow_offset, cy + shadow_offset),
                ],
                fill="#D1D5DB",
                outline=None,
            )
            draw.polygon(
                [(cx, cy - d), (cx + d, cy), (cx, cy + d), (cx - d, cy)],
                fill="#FBBF24",
                outline="#92400E",
                width=int(2 * scale),
            )
            # 编号
            bbox = draw.textbbox((0, 0), "◆", font=font_axis)
            draw.text(
                (cx - int(4 * scale), cy - int(8 * scale)), "◆", fill="#7C2D12", font=font_axis
            )
        else:
            # 普通任务
            bar_top = y + int(10 * scale)
            bar_bottom = y + row_h - int(10 * scale)
            end_x_safe = max(start_x + int(8 * scale), end_x)

            if highlight_critical_path and task.get("critical"):
                # 关键路径高亮模式：红色边框 + 浅红填充
                for i in range(int(bar_top), int(bar_bottom)):
                    draw.line([(start_x, i), (end_x_safe, i)], fill=(254, 226, 226))
                draw.rounded_rectangle(
                    [start_x, bar_top, end_x_safe, bar_bottom],
                    radius=int(8 * scale),
                    outline=critical_outline,
                    width=int(3 * scale),
                )
            else:
                # 普通任务：圆角矩形 + 渐变效果
                # BUG-FIX 2026-09-14（中）：原实现把渐变区域收集到 task_bar_regions，
                # 在**整个任务循环结束后**才批量绘制 draw.line —— 而任务起止日期文本
                # 是在循环内先画好的，结果被后画的不透明渐变条完全覆盖，
                # 甘特图上所有「2026-03-01 ~ 2026-05-20」之类的白字全部看不见。
                # 修复：渐变改为在绘制文本之前就地完成，后续轮廓/文本按序叠加。
                color = palette[(row_index - 1) % len(palette)]
                # 解析颜色
                r0, g0, b0 = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
                # 亮色版本
                r1, g1, b1 = min(r0 + 31, 255), min(g0 + 153, 255), min(b0 + 93, 255)
                # 就地铺渐变底（必须在绘制起止日期文本之前）
                _span = max(1, int(bar_bottom) - int(bar_top))
                for _i in range(int(bar_top), int(bar_bottom)):
                    _t = (_i - bar_top) / _span
                    draw.line(
                        [(start_x, _i), (end_x_safe, _i)],
                        fill=(
                            int(r0 + (r1 - r0) * _t),
                            int(g0 + (g1 - g0) * _t),
                            int(b0 + (b1 - b0) * _t),
                        ),
                    )
                # 轮廓
                outline_color = "#DC2626" if task.get("critical") else "#1D4ED8"
                outline_width = int(2 * scale) if task.get("critical") else int(1 * scale)
                draw.rounded_rectangle(
                    [start_x, bar_top, end_x_safe, bar_bottom],
                    radius=int(8 * scale),
                    outline=outline_color,
                    width=outline_width,
                )

            # 任务起止文本
            if end_x - start_x > int(80 * scale):
                label = f"{task['start']}-{task['end']}"
                text_color = (
                    "#DC2626" if (highlight_critical_path and task.get("critical")) else "white"
                )
                _draw_text_center(
                    draw,
                    (start_x + int(4 * scale), bar_top, end_x - int(4 * scale), bar_bottom),
                    label,
                    font_small,
                    text_color,
                    max_lines=1,
                )

        task_pos[task["id"]] = {
            "y": y,
            "h": row_h,
            "start_x": start_x,
            "end_x": end_x,
            "cy": y + row_h / 2,
        }
        y += row_h

    # 注：渐变底已在各任务行内就地绘制（BUG-FIX 2026-09-14）。
    # 原「批量绘制」实现晚于文本绘制，会把任务起止日期文字完全覆盖，已移除。

    # ---- 依赖关系连线 ----
    for task in tasks:
        if not task.get("dependencies"):
            continue
        if task["id"] not in task_pos:
            continue
        target = task_pos[task["id"]]
        for dep_id in task["dependencies"]:
            if dep_id not in task_pos:
                continue
            source = task_pos[dep_id]
            # 从源行右中到目标行左中
            sx = source["end_x"]
            sy = source["cy"]
            ex = target["start_x"] - int(6 * scale)
            ey = target["cy"]
            # 折线
            mid_x = (sx + ex) / 2

            if highlight_critical_path:
                # 关键路径模式：依赖线颜色根据关键路径状态
                dep_color = (
                    "#DC2626"
                    if (
                        task.get("critical")
                        or any(t.get("critical") for t in tasks if t["id"] == dep_id)
                    )
                    else "#94A3B8"
                )
                # 连线阴影
                draw.line(
                    [
                        (sx + int(1 * scale), sy + int(1 * scale)),
                        (mid_x + int(1 * scale), sy + int(1 * scale)),
                        (mid_x + int(1 * scale), ey + int(1 * scale)),
                        (ex - int(4 * scale) + int(1 * scale), ey + int(1 * scale)),
                    ],
                    fill="#E5E7EB",
                    width=int(2 * scale),
                )
                draw.line(
                    [(sx, sy), (mid_x, sy), (mid_x, ey), (ex - int(4 * scale), ey)],
                    fill=dep_color,
                    width=int(1 * scale),
                )
                # 箭头
                draw.polygon(
                    [
                        (ex, ey),
                        (ex - int(6 * scale), ey - int(3 * scale)),
                        (ex - int(6 * scale), ey + int(3 * scale)),
                    ],
                    fill=dep_color,
                )
            else:
                # 普通模式：灰色依赖线
                draw.line(
                    [
                        (sx + int(1 * scale), sy + int(1 * scale)),
                        (mid_x + int(1 * scale), sy + int(1 * scale)),
                        (mid_x + int(1 * scale), ey + int(1 * scale)),
                        (ex - int(4 * scale) + int(1 * scale), ey + int(1 * scale)),
                    ],
                    fill="#E5E7EB",
                    width=int(2 * scale),
                )
                draw.line(
                    [(sx, sy), (mid_x, sy), (mid_x, ey), (ex - int(4 * scale), ey)],
                    fill="#94A3B8",
                    width=int(2 * scale),
                )
                # 箭头
                draw.polygon(
                    [
                        (ex, ey),
                        (ex - int(7 * scale), ey - int(4 * scale)),
                        (ex - int(7 * scale), ey + int(4 * scale)),
                    ],
                    fill="#94A3B8",
                )

    # ---- 外边框 ----
    draw.rectangle(
        [table_left, table_top, table_left + table_w, table_top + header_h + len(tasks) * row_h],
        outline=border,
        width=int(2 * scale),
    )

    # ---- 备注（仅非关键路径模式有备注）----
    if remarks and not highlight_critical_path:
        note_top = y + int(16 * scale)
        notes_text = "  ".join(f"· {r}" for r in remarks if r)
        if notes_text:
            draw.text((table_left, note_top), notes_text, fill="#374151", font=font_small)
        y += remarks_h

    # ---- 图例 ----
    legend_y = y + int(18 * scale)
    legend_x = table_left
    if highlight_critical_path:
        # 增强版图例（含关键路径）
        legend_items = [
            ("#2B579A", "普通任务"),
            ("#FBBF24", "里程碑 ◆"),
            ("#DC2626", "关键路径"),
        ]
        for color, label in legend_items:
            if label == "关键路径":
                draw.rounded_rectangle(
                    [
                        legend_x,
                        legend_y - int(12 * scale),
                        legend_x + int(24 * scale),
                        legend_y + int(4 * scale),
                    ],
                    radius=int(3 * scale),
                    fill="#FEE2E2",
                    outline="#DC2626",
                    width=int(2 * scale),
                )
                draw.text(
                    (legend_x + int(30 * scale), legend_y - int(12 * scale)),
                    label,
                    fill="#DC2626",
                    font=font_small,
                )
            else:
                draw.rounded_rectangle(
                    [
                        legend_x,
                        legend_y - int(12 * scale),
                        legend_x + int(24 * scale),
                        legend_y + int(4 * scale),
                    ],
                    radius=int(3 * scale),
                    fill=color,
                    outline="#1E3A5F",
                    width=int(1 * scale),
                )
                draw.text(
                    (legend_x + int(30 * scale), legend_y - int(12 * scale)),
                    label,
                    fill="#1E3A5F",
                    font=font_small,
                )
            legend_x += int(130 * scale)
    else:
        legend_items = [
            ("#2B579A", "任务条"),
            ("#FBBF24", "里程碑 ◆"),
            ("#DC2626", "关键路径（红色边框）"),
        ]
        for color, label in legend_items:
            draw.rounded_rectangle(
                [
                    legend_x,
                    legend_y - int(12 * scale),
                    legend_x + int(24 * scale),
                    legend_y + int(4 * scale),
                ],
                radius=int(3 * scale),
                fill=color,
                outline="#1E3A5F",
                width=int(1 * scale),
            )
            draw.text(
                (legend_x + int(30 * scale), legend_y - int(12 * scale)),
                label,
                fill="#1E3A5F",
                font=font_small,
            )
            legend_x += int(200 * scale)

    result_stream = _image_to_stream(img)
    size_kb = len(result_stream.getvalue()) // 1024
    logger.info(
        "[PIL v2渲染] 甘特图渲染完成: 任务=%d 尺寸=%dKB DPI=300",
        len(tasks),
        size_kb,
    )
    return result_stream

def gantt_json_to_mermaid(plan: dict) -> str:
    """将 tasks + dependencies 结构转换为 Mermaid Gantt 代码字符串

    自动计算日期偏移和持续时间。

    Args:
        plan: {"tasks": [{"id":"T1","name":"施工准备","start":"2026-01-01",
                          "end":"2026-01-10","dependencies":[],"critical":true},...]}

    Returns:
        Mermaid Gantt 代码字符串
    """
    try:
        tasks = plan.get("tasks", [])
        if not tasks:
            return ""

        lines = [
            "gantt",
            f'    title {plan.get("title", "施工进度计划")}',
            "    dateFormat YYYY-MM-DD",
            "    axisFormat %m-%d",
        ]

        # 计算基准日期

        base_date = None
        for t in tasks:
            start_str = t.get("start", "")
            if isinstance(start_str, str) and start_str.strip():
                try:
                    d = datetime.strptime(start_str.strip(), "%Y-%m-%d")
                    if base_date is None or d < base_date:
                        base_date = d
                except ValueError:
                    pass

        if base_date is None:
            base_date = datetime.now()

        current_section = ""
        for t in tasks:
            name = t.get("name", "")
            start_str = t.get("start", "")
            end_str = t.get("end", "")
            section = t.get("section", "")

            if section and section != current_section:
                lines.append(f"    section {section}")
                current_section = section

            if (
                isinstance(start_str, str)
                and isinstance(end_str, str)
                and start_str.strip()
                and end_str.strip()
            ):
                lines.append(f"    {name} : {start_str}, {end_str}")
            elif isinstance(start_str, (int, float)):
                start_day = int(start_str)
                end_day = int(t.get("end", start_day))
                start_date = base_date + timedelta(days=start_day - 1)
                end_date = base_date + timedelta(days=end_day - 1)
                lines.append(
                    f'    {name} : {start_date.strftime("%Y-%m-%d")}, {end_date.strftime("%Y-%m-%d")}'
                )

        return "\n".join(lines)
    except Exception as e:
        logger.warning("gantt_json_to_mermaid 转换失败: %s", e)
        return ""

def _render_gantt_with_dependencies(plan: dict) -> BytesIO | None:
    """增强版甘特图渲染器（向后兼容别名）

    已合并到 _render_gantt_image_v2，使用 highlight_critical_path=True 参数。
    保留此函数作为向后兼容的别名。

    Args:
        plan: 甘特图计划数据

    Returns:
        BytesIO PNG 图片，失败时返回 None
    """
    return _render_gantt_image_v2(plan, highlight_critical_path=True)

