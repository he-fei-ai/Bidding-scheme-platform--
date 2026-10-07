"""按函数边界拆分 mermaid_renderer.py 为 7 个图表子文件 + 1 个公共 + 1 个 facade。

策略：
  - mermaid_common.py    : 字体/绘制/_render_hq/_build_gantt_marks
  - mermaid_flowchart.py : _parse_mermaid_flowchart / _is_decision_label / flowchart 节点解析
                           / _parse_flowchart_structure / _render_flowchart_image_v2
                           / flowchart_json_to_mermaid / _render_flowchart_with_edges
  - mermaid_gantt.py     : _parse_mermaid_gantt / _render_gantt_image_v2
                           / gantt_json_to_mermaid / _render_gantt_with_dependencies
  - mermaid_architecture.py: _layout_architecture_tree / _render_architecture_image_v2
  - mermaid_comparison.py: _parse_mermaid_pie / _parse_mermaid_xychart / _render_comparison_image_v2
  - mermaid_labor.py     : _render_labor_image_v2
  - mermaid_layout.py    : _repair_layout_data / _render_layout_image_v2
  - mermaid_timeline.py  : _repair_timeline_data / _render_timeline_image_v2
  - mermaid_renderer.py  : facade：render_mermaid_to_bytes + ChartCache + 公共 re-export

执行：python _split_mermaid.py
"""
import ast
import os

SRC = r"J:\编程\招投标方案平台\backend\app\services\ai\mermaid_renderer.py"
BACKUP = SRC + ".bak"
DST_DIR = r"J:\编程\招投标方案平台\backend\app\services\ai"

# (子文件名, 包含的顶层符号名列表)
SPLIT_PLAN = [
    (
        "mermaid_flowchart.py",
        [
            "_parse_mermaid_flowchart",
            "_DECISION_KEYWORDS",
            "_is_decision_label",
            "_extract_mermaid_node_label",
            "_register_node_if_match",
            "_strip_mermaid_node_brackets",
            "_split_mermaid_edge",
            "_parse_flowchart_structure",
            "_render_flowchart_image_v2",
            "flowchart_json_to_mermaid",
            "_render_flowchart_with_edges",
        ],
    ),
    (
        "mermaid_gantt.py",
        [
            "_parse_mermaid_gantt",
            "_render_gantt_image_v2",
            "gantt_json_to_mermaid",
            "_render_gantt_with_dependencies",
        ],
    ),
    (
        "mermaid_architecture.py",
        [
            "_layout_architecture_tree",
            "_render_architecture_image_v2",
        ],
    ),
    (
        "mermaid_comparison.py",
        [
            "_parse_mermaid_pie",
            "_parse_mermaid_xychart",
            "_render_comparison_image_v2",
        ],
    ),
    (
        "mermaid_labor.py",
        [
            "_render_labor_image_v2",
        ],
    ),
    (
        "mermaid_layout.py",
        [
            "_render_layout_image_v2",
            "_repair_layout_data",
        ],
    ),
    (
        "mermaid_timeline.py",
        [
            "_render_timeline_image_v2",
            "_repair_timeline_data",
        ],
    ),
]

# facade 保留的顶层符号
FACADE_KEEP = [
    "render_mermaid_to_bytes",
    "ChartCache",
    # 公共 re-export（向后兼容外部 import）
    "render_mermaid",
]


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    # 不切换工作目录——所有路径已用绝对路径

    with open(SRC, "r", encoding="utf-8") as f:
        src = f.read()

    # 备份原文件
    if not os.path.exists(BACKUP):
        with open(BACKUP, "w", encoding="utf-8") as f:
            f.write(src)
        print(f"Backup saved: {BACKUP}")

    tree = ast.parse(src, filename=SRC)

    # 收集所有顶层节点的源码片段
    def node_source(node):
        # ast.get_source_segment 在 Python 3.8+ 可用
        return ast.get_source_segment(src, node)

    # 索引顶层节点
    top = {}
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Assign)):
            name = getattr(n, "name", None)
            if name:
                top[name] = node_source(n)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            top[n.target.id] = node_source(n)

    # 检查所有声明的符号都找到了
    all_needed = set()
    for _, syms in SPLIT_PLAN:
        all_needed.update(syms)
    all_needed.update(FACADE_KEEP)
    missing = all_needed - set(top.keys())
    if missing:
        print(f"WARNING: symbols not found at top level: {missing}")

    # 为每个子文件生成内容
    common_header = '''"""Mermaid 图表渲染公共工具：字体、文本绘制、300 DPI 装饰器、Gantt 坐标。

由 scripts/_split_mermaid.py 从原 mermaid_renderer.py 自动拆分而来。
任何图表子模块（flowchart / gantt / ...）应从本模块导入共享工具。
"""
from __future__ import annotations

import functools
import logging
import os
import sys
from io import BytesIO

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    raise ImportError(
        "PIL/Pillow 未安装，Mermaid图表渲染功能不可用。"
        "请运行: pip install Pillow"
    )


logger = logging.getLogger(__name__)


# 字体路径配置（可被环境变量 FONT_PATH 覆盖）
_DEFAULT_FONT_PATHS = [
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/droid/DroidSansFallback.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "C:\\\\Windows\\\\Fonts\\\\msyh.ttc",
    "C:\\\\Windows\\\\Fonts\\\\msyhbd.ttc",
    "C:\\\\Windows\\\\Fonts\\\\simsun.ttc",
    "C:\\\\Windows\\\\Fonts\\\\simhei.ttf",
    "C:\\\\Windows\\\\Fonts\\\\yahei.ttf",
    "C:\\\\Windows\\\\Fonts\\\\deng.ttf",
    "C:\\\\Windows\\\\Fonts\\\\FZSTK.TTF",
    "C:\\\\Windows\\\\Fonts\\\\FZHTK.TTF",
]

_DEFAULT_BOLD_FONT_PATHS = [
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "C:\\\\Windows\\\\Fonts\\\\msyhbd.ttc",
    "C:\\\\Windows\\\\Fonts\\\\simhei.ttf",
    "C:\\\\Windows\\\\Fonts\\\\msyh.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
]


'''

    # 写公共模块
    common_path = os.path.join(DST_DIR, "mermaid_common.py")
    with open(common_path, "w", encoding="utf-8") as f:
        f.write(common_header)
        # 拷贝以下公共函数源码（不依赖文件内其它顺序，从 SRC 切片拿到）
        common_fns = [
            "_resolve_font_paths",
            "_auto_detect_chinese_font",
            "_image_font",
            "_render_hq",
            "_image_to_stream",
            "_text_width",
            "_text_height",
            "_draw_text_center",
            "_wrap_text",
            "_fit_text_with_ellipsis",
            "_build_gantt_marks",
        ]
        for name in common_fns:
            if name in top:
                f.write(top[name] + "\n\n")
            else:
                print(f"WARNING: common function {name} not found")
    print("Wrote: mermaid_common.py")

    # 写每个图表子文件
    for fname, syms in SPLIT_PLAN:
        header = f'''"""{fname.replace(".py", "").replace("mermaid_", "").capitalize()} 图表渲染器。

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


logger = logging.getLogger(__name__)


'''
        fpath = os.path.join(DST_DIR, fname)
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(header)
            for name in syms:
                if name in top:
                    f.write(top[name] + "\n\n")
                else:
                    print(f"WARNING: {fname}: {name} not found")
        print(f"Wrote: {fname}")

    # 写 facade（mermaid_renderer.py）
    # 先收集 facade 保留符号的源码
    facade_src = ''
    for name in FACADE_KEEP:
        if name in top:
            facade_src += top[name] + "\n\n"

    # render_mermaid 函数（可选，原来可能不存在）
    if "render_mermaid" in top:
        # 已经在 facade_src 里
        pass
    else:
        # 原文件可能没有 render_mermaid，legacy 兼容
        facade_src += '''def render_mermaid(mermaid_code: str, chart_type: str = "flowchart") -> str:
    """兼容接口：返回 Mermaid 源码（不实际渲染）。
    
    完整渲染请使用 render_mermaid_to_bytes()。该函数保留仅为向后兼容。
    """
    return mermaid_code


'''

    # facade 头部
    facade_header = '''"""Mermaid 图表渲染引擎（facade）。

本文件是拆分后的统一入口：保留向后兼容的公共 API，
实际渲染逻辑已拆分到 7 个图表子文件：

  - mermaid_common.py         公共工具（字体、文本绘制、300 DPI 装饰器）
  - mermaid_flowchart.py      流程图
  - mermaid_gantt.py          甘特图
  - mermaid_architecture.py   组织架构图
  - mermaid_comparison.py     对比图 / 饼图 / 柱状图
  - mermaid_labor.py          劳动力配置图
  - mermaid_layout.py         总平面布置图
  - mermaid_timeline.py       关键里程碑时间轴

外部调用方继续使用：
    from .mermaid_renderer import render_mermaid_to_bytes, ChartCache

所有拆分前可用的内部函数（_render_xxx_image_v2、_parse_xxx、_repair_xxx_data 等）
均通过 re-export 暴露，import 路径不变。
"""
from __future__ import annotations

import asyncio
from io import BytesIO
import logging
import re
import time

from .mermaid_service import MermaidRenderError, get_mermaid_service_client


logger = logging.getLogger(__name__)


'''

    # 公共 re-export（让外部 import 路径不变）
    reexport_lines = '''
# ============================================================
# 向后兼容 re-export（拆分前可用的所有内部符号）
# ============================================================
from .mermaid_common import (  # noqa: F401
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
from .mermaid_flowchart import (  # noqa: F401
    _DECISION_KEYWORDS,
    _extract_mermaid_node_label,
    _is_decision_label,
    _parse_flowchart_structure,
    _parse_mermaid_flowchart,
    _register_node_if_match,
    _render_flowchart_image_v2,
    _render_flowchart_with_edges,
    _split_mermaid_edge,
    _strip_mermaid_node_brackets,
    flowchart_json_to_mermaid,
)
from .mermaid_gantt import (  # noqa: F401
    _parse_mermaid_gantt,
    _render_gantt_image_v2,
    _render_gantt_with_dependencies,
    gantt_json_to_mermaid,
)
from .mermaid_architecture import (  # noqa: F401
    _layout_architecture_tree,
    _render_architecture_image_v2,
)
from .mermaid_comparison import (  # noqa: F401
    _parse_mermaid_pie,
    _parse_mermaid_xychart,
    _render_comparison_image_v2,
)
from .mermaid_labor import (  # noqa: F401
    _render_labor_image_v2,
)
from .mermaid_layout import (  # noqa: F401
    _render_layout_image_v2,
    _repair_layout_data,
)
from .mermaid_timeline import (  # noqa: F401
    _render_timeline_image_v2,
    _repair_timeline_data,
)

'''

    # 写新 facade
    with open(SRC, "w", encoding="utf-8") as f:  # SRC 是绝对路径
        f.write(facade_header)
        f.write(facade_src)
        f.write(reexport_lines)
    print(f"Wrote: {SRC} (facade)")

    print("\nDone. Backup:", BACKUP)


if __name__ == "__main__":
    main()
