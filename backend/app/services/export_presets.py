"""导出格式预设（版式预设 + 主题预设）。

ENHANCE 2026-09-14：移植自「易标投标工具箱」`client/src/features/export-format/exportFormatPresets.ts`
的 `EXPORT_LAYOUT_PRESETS` / `EXPORT_THEME_PRESETS` 体系。

背景：本平台原 `ExportConfig` 只有 16 个字段（字体/字号/行距/开关），用户每次导出都要
手工调 6 个字号 + 2 个字体 + 行距，且没有任何「一键成型」的入口；而标书排版有强约定
（黑体标题 + 宋体正文、一级标题另起页、装订留白、章框标题等），手调极易出错。
易标用「版式预设 × 主题配色」两维正交的方式解决了这个问题，本模块按同样的思路移植。

设计：
  - 版式预设（LAYOUT_PRESETS）：决定字体、字号、行距、段距、页边距、页码、分页、图片宽度
  - 主题预设（THEME_PRESETS）  ：决定标题字色、表头底色、斑马纹、边框色、页眉页脚色
  - 两者正交组合：8 套版式 × 12 套主题 = 96 种组合

对外接口：
  - list_presets() -> dict                    给前端/开放 API 的元数据
  - get_layout_preset(preset_id) -> dict|None
  - get_theme_preset(theme_id) -> dict|None
  - resolve_export_config(config) -> ExportConfig
      以预设为「基底」，仅覆盖调用方**未显式传值**的字段（Pydantic v2 的
      `model_fields_set` 提供这一信息）。前端会在本地先把预设写进表单，
      所以显式字段以用户当前表单为准；脚本/开放 API 只传 preset_id 时则完整生效。
"""

from __future__ import annotations

import copy
import logging
from typing import Any

from ..models import ExportConfig


logger = logging.getLogger(__name__)


# ==================== 中文字号 → pt ====================

# 国标中文字号与磅值的对应关系（GB/T 9704 常用字号）
_CN_FONT_SIZE_PT: dict[str, float] = {
    "初号": 42,
    "小初": 36,
    "一号": 26,
    "小一": 24,
    "二号": 22,
    "小二": 18,
    "三号": 16,
    "小三": 15,
    "四号": 14,
    "小四": 12,
    "五号": 10.5,
    "小五": 9,
    "六号": 7.5,
    "小六": 6.5,
    "七号": 5.5,
    "八号": 5,
}


def cn_size_pt(name: str) -> float:
    """把中文字号名转换为磅值；未知字号返回 12pt（小四）。"""
    return _CN_FONT_SIZE_PT.get(str(name).strip(), 12.0)


def _body_width_cm(margin_left_cm: float, margin_right_cm: float, landscape: bool = False) -> float:
    """按 A4 页面与左右页边距计算正文可用宽度（cm）。

    A4：纵向宽 21cm、横向宽 29.7cm。图片最大宽度按可用宽度的 90% 取整，
    避免图片顶到页边距边缘导致 Word 自动缩排错位。
    """
    page_width = 29.7 if landscape else 21.0
    usable = page_width - margin_left_cm - margin_right_cm
    return round(max(6.0, usable * 0.9), 1)


# ==================== 版式预设 ====================

# 每个预设的 config 片段直接使用 ExportConfig 的字段名，便于 model_copy(update=...)。
LAYOUT_PRESETS: list[dict[str, Any]] = [
    {
        "id": "standard-bid",
        "label": "标准投标版",
        "description": "A4 纵向、2cm 常规边距、小四正文、黑体标题，适合绝大多数技术方案。",
        "config": {
            "font_title": "黑体",
            "font_body": "宋体",
            "heading_fonts": ["黑体", "黑体", "黑体", "楷体", "黑体", "宋体"],
            "heading_sizes": [18.0, 14.0, 12.0, 12.0, 12.0, 12.0],
            "size_title_1": 18.0,
            "size_title_2": 14.0,
            "size_title_3": 12.0,
            "size_title_sub": 12.0,
            "size_body": 12.0,
            "line_spacing": 1.2,
            "page_orientation": "portrait",
            "page_margin_top_cm": 2.0,
            "page_margin_bottom_cm": 2.0,
            "page_margin_left_cm": 2.0,
            "page_margin_right_cm": 2.0,
            "page_number_enabled": False,
            "page_number_format": "第{page}页",
            "heading_level1_page_break": False,
            "heading_border_enabled": False,
            "body_first_line_indent_chars": 2.0,
            "image_max_width_cm": _body_width_cm(2.0, 2.0),
            "gantt_max_columns": 18,
        },
    },
    {
        "id": "formal-binding",
        "label": "正式装订版",
        "description": "左侧留 2.8cm 装订空间、一级标题另起页、仿宋正文，适合打印后装订递交。",
        "config": {
            "font_title": "黑体",
            "font_body": "仿宋",
            "heading_fonts": ["黑体", "黑体", "黑体", "楷体", "黑体", "宋体"],
            "heading_sizes": [22.0, 16.0, 15.0, 14.0, 12.0, 12.0],
            "size_title_1": 22.0,
            "size_title_2": 16.0,
            "size_title_3": 15.0,
            "size_title_sub": 12.0,
            "size_body": 12.0,
            "line_spacing": 1.5,
            "page_orientation": "portrait",
            "page_margin_top_cm": 2.4,
            "page_margin_bottom_cm": 2.2,
            "page_margin_left_cm": 2.8,
            "page_margin_right_cm": 2.0,
            "page_number_enabled": True,
            "page_number_format": "第{page}页",
            "heading_level1_page_break": True,
            "heading_border_enabled": False,
            "body_first_line_indent_chars": 2.0,
            "image_max_width_cm": _body_width_cm(2.8, 2.0),
            "gantt_max_columns": 18,
        },
    },
    {
        "id": "compact-review",
        "label": "紧凑评审版",
        "description": "1.5cm 边距、五号正文、1.15 行距，适合内容多、需压缩页数的评审稿。",
        "config": {
            "font_title": "黑体",
            "font_body": "宋体",
            "heading_fonts": ["黑体", "黑体", "黑体", "楷体", "黑体", "宋体"],
            "heading_sizes": [16.0, 15.0, 14.0, 12.0, 12.0, 10.5],
            "size_title_1": 16.0,
            "size_title_2": 15.0,
            "size_title_3": 14.0,
            "size_title_sub": 10.5,
            "size_body": 10.5,
            "line_spacing": 1.15,
            "page_orientation": "portrait",
            "page_margin_top_cm": 1.5,
            "page_margin_bottom_cm": 1.5,
            "page_margin_left_cm": 1.6,
            "page_margin_right_cm": 1.6,
            "page_number_enabled": True,
            "page_number_format": "{page}",
            "heading_level1_page_break": False,
            "heading_border_enabled": False,
            "body_first_line_indent_chars": 2.0,
            "image_max_width_cm": _body_width_cm(1.6, 1.6),
            "gantt_max_columns": 20,
        },
    },
    {
        "id": "wide-table-landscape",
        "label": "宽表格横版",
        "description": "A4 横向 + 紧凑边距，适合设备清单、进度计划、劳动力配置等宽表章节。",
        "config": {
            "font_title": "黑体",
            "font_body": "宋体",
            "heading_fonts": ["黑体", "黑体", "黑体", "楷体", "黑体", "宋体"],
            "heading_sizes": [16.0, 15.0, 14.0, 12.0, 12.0, 10.5],
            "size_title_1": 16.0,
            "size_title_2": 15.0,
            "size_title_3": 14.0,
            "size_title_sub": 10.5,
            "size_body": 10.5,
            "line_spacing": 1.15,
            "page_orientation": "landscape",
            "page_margin_top_cm": 1.4,
            "page_margin_bottom_cm": 1.4,
            "page_margin_left_cm": 1.5,
            "page_margin_right_cm": 1.5,
            "page_number_enabled": True,
            "page_number_format": "第{page}页",
            "heading_level1_page_break": False,
            "heading_border_enabled": False,
            "body_first_line_indent_chars": 2.0,
            "image_max_width_cm": _body_width_cm(1.5, 1.5, landscape=True),
            "gantt_max_columns": 26,
        },
    },
    {
        "id": "visual-report",
        "label": "图文展示版",
        "description": "1.3 倍行距、图片占比更高，适合流程图/架构图/效果图较多的方案。",
        "config": {
            "font_title": "黑体",
            "font_body": "宋体",
            "heading_fonts": ["黑体", "黑体", "黑体", "楷体", "黑体", "宋体"],
            "heading_sizes": [18.0, 15.0, 14.0, 12.0, 12.0, 12.0],
            "size_title_1": 18.0,
            "size_title_2": 15.0,
            "size_title_3": 14.0,
            "size_title_sub": 12.0,
            "size_body": 12.0,
            "line_spacing": 1.3,
            "page_orientation": "portrait",
            "page_margin_top_cm": 1.8,
            "page_margin_bottom_cm": 1.8,
            "page_margin_left_cm": 2.0,
            "page_margin_right_cm": 2.0,
            "page_number_enabled": True,
            "page_number_format": "第{page}页",
            "heading_level1_page_break": False,
            "heading_border_enabled": False,
            "body_first_line_indent_chars": 2.0,
            "image_max_width_cm": _body_width_cm(2.0, 2.0),
            "image_dpi": 300,
            "gantt_max_columns": 18,
        },
    },
    {
        "id": "chapter-frame",
        "label": "章框标题版",
        "description": "一级标题居中并加章框装饰线，适合需要强视觉分章的重点项目。",
        "config": {
            "font_title": "黑体",
            "font_body": "宋体",
            "heading_fonts": ["黑体", "黑体", "黑体", "楷体", "黑体", "宋体"],
            "heading_sizes": [18.0, 16.0, 14.0, 12.0, 12.0, 12.0],
            "size_title_1": 18.0,
            "size_title_2": 16.0,
            "size_title_3": 14.0,
            "size_title_sub": 12.0,
            "size_body": 12.0,
            "line_spacing": 1.4,
            "page_orientation": "portrait",
            "page_margin_top_cm": 2.2,
            "page_margin_bottom_cm": 2.2,
            "page_margin_left_cm": 2.6,
            "page_margin_right_cm": 2.2,
            "page_number_enabled": True,
            "page_number_format": "第{page}页",
            "heading_level1_page_break": True,
            "heading_border_enabled": True,
            "body_first_line_indent_chars": 2.0,
            "image_max_width_cm": _body_width_cm(2.6, 2.2),
            "gantt_max_columns": 18,
        },
    },
    {
        "id": "left-title-frame",
        "label": "左栏标题框版",
        "description": "仿宋正文 + 大号章框标题 + 装订边距，接近公文式排版规范。",
        "config": {
            "font_title": "黑体",
            "font_body": "仿宋",
            "heading_fonts": ["黑体", "黑体", "黑体", "楷体", "黑体", "宋体"],
            "heading_sizes": [22.0, 16.0, 15.0, 14.0, 12.0, 12.0],
            "size_title_1": 22.0,
            "size_title_2": 16.0,
            "size_title_3": 15.0,
            "size_title_sub": 12.0,
            "size_body": 12.0,
            "line_spacing": 1.5,
            "page_orientation": "portrait",
            "page_margin_top_cm": 2.4,
            "page_margin_bottom_cm": 2.2,
            "page_margin_left_cm": 2.6,
            "page_margin_right_cm": 2.0,
            "page_number_enabled": True,
            "page_number_format": "第{page}页",
            "heading_level1_page_break": True,
            "heading_border_enabled": True,
            "body_first_line_indent_chars": 2.0,
            "image_max_width_cm": _body_width_cm(2.6, 2.0),
            "gantt_max_columns": 18,
        },
    },
    {
        "id": "none",
        "label": "无装饰（兼容旧版）",
        "description": "不加章框、不加页码，等同于平台历史默认排版，便于与旧稿保持一致。",
        "config": {
            "font_title": "宋体",
            "font_body": "宋体",
            "heading_fonts": ["宋体", "宋体", "宋体", "宋体", "宋体", "宋体"],
            "heading_sizes": [14.0, 14.0, 14.0, 14.0, 12.0, 12.0],
            "size_title_1": 14.0,
            "size_title_2": 14.0,
            "size_title_3": 14.0,
            "size_title_sub": 12.0,
            "size_body": 12.0,
            "line_spacing": 1.5,
            "page_orientation": "portrait",
            "page_margin_top_cm": 2.54,
            "page_margin_bottom_cm": 2.54,
            "page_margin_left_cm": 3.18,
            "page_margin_right_cm": 3.18,
            "page_number_enabled": True,
            "page_number_format": "第{page}页",
            "heading_level1_page_break": False,
            "heading_border_enabled": False,
            "body_first_line_indent_chars": 2.0,
            "image_max_width_cm": _body_width_cm(3.18, 3.18),
            "gantt_max_columns": 18,
        },
    },
]


# ==================== 主题预设 ====================

# 颜色统一存「不带 # 的 6 位十六进制」，因为 python-docx 的 w:shd/@w:fill 与
# RGBColor.from_string 都要求不带 # 的形式；入口处再做一次规范化。
THEME_PRESETS: list[dict[str, Any]] = [
    {
        "id": "none",
        "label": "无（黑白）",
        "description": "黑字、黑边框、白底，最保守的打印友好配色。",
        "swatches": ["#000000", "#ffffff"],
        "heading_color": "000000",
        "heading_border_color": "000000",
        "header_footer_color": "000000",
        "table_border_color": "000000",
        "table_header_fg": "000000",
        "table_header_bg": "ffffff",
        "table_alt_row_bg": "ffffff",
    },
    {
        "id": "blue",
        "label": "蓝色",
        "description": "深蓝标题与浅蓝表头，正式科技类方案常用。",
        "swatches": ["#173f82", "#2174fd", "#dbeafe"],
        "heading_color": "173f82",
        "heading_border_color": "2174fd",
        "header_footer_color": "315b9f",
        "table_border_color": "8db8ff",
        "table_header_fg": "123a78",
        "table_header_bg": "dbeafe",
        "table_alt_row_bg": "eef5ff",
    },
    {
        "id": "light-blue",
        "label": "淡蓝",
        "description": "低饱和蓝灰，适合长文档与轻量商务风。",
        "swatches": ["#2f6f92", "#a9d7f2", "#f3fbff"],
        "heading_color": "2f6f92",
        "heading_border_color": "a9d7f2",
        "header_footer_color": "55798f",
        "table_border_color": "bddded",
        "table_header_fg": "245f82",
        "table_header_bg": "eaf7ff",
        "table_alt_row_bg": "f4fbff",
    },
    {
        "id": "green",
        "label": "绿色",
        "description": "稳重绿色，适合运维、环保、政企服务类方案。",
        "swatches": ["#116a3a", "#22a05a", "#dcfce7"],
        "heading_color": "116a3a",
        "heading_border_color": "22a05a",
        "header_footer_color": "2e7449",
        "table_border_color": "8fd3a6",
        "table_header_fg": "0f5a32",
        "table_header_bg": "dcfce7",
        "table_alt_row_bg": "effaf2",
    },
    {
        "id": "light-green",
        "label": "淡绿",
        "description": "清爽淡绿，适合报告型与服务保障型文档。",
        "swatches": ["#3d764b", "#a7dcb3", "#f2fbf4"],
        "heading_color": "3d764b",
        "heading_border_color": "a7dcb3",
        "header_footer_color": "5f8468",
        "table_border_color": "bfdfc5",
        "table_header_fg": "356a43",
        "table_header_bg": "eaf8ee",
        "table_alt_row_bg": "f5fbf6",
    },
    {
        "id": "orange",
        "label": "橙色",
        "description": "暖橙强调重点，适合交付计划与服务承诺类内容。",
        "swatches": ["#8a4b10", "#f59e0b", "#fff3d6"],
        "heading_color": "8a4b10",
        "heading_border_color": "f59e0b",
        "header_footer_color": "9b6123",
        "table_border_color": "f2c46f",
        "table_header_fg": "79420e",
        "table_header_bg": "fff3d6",
        "table_alt_row_bg": "fff8e8",
    },
    {
        "id": "light-purple",
        "label": "淡紫",
        "description": "柔和紫色，适合创新平台、数据治理类方案。",
        "swatches": ["#5b3ca6", "#a78bfa", "#f2edff"],
        "heading_color": "5b3ca6",
        "heading_border_color": "a78bfa",
        "header_footer_color": "7054aa",
        "table_border_color": "c9b8ff",
        "table_header_fg": "553798",
        "table_header_bg": "f2edff",
        "table_alt_row_bg": "f8f5ff",
    },
    {
        "id": "teal",
        "label": "青蓝",
        "description": "青蓝主色，适合水利、市政、环保工程类方案。",
        "swatches": ["#0f5f66", "#22b8c4", "#d9f6f8"],
        "heading_color": "0f5f66",
        "heading_border_color": "22b8c4",
        "header_footer_color": "2f7f86",
        "table_border_color": "93dfe6",
        "table_header_fg": "0d5459",
        "table_header_bg": "d9f6f8",
        "table_alt_row_bg": "eefbfc",
    },
    {
        "id": "red",
        "label": "朱红",
        "description": "朱红强调，适合安全、消防、应急类专项方案。",
        "swatches": ["#9b1c1c", "#ef4444", "#fee2e2"],
        "heading_color": "9b1c1c",
        "heading_border_color": "ef4444",
        "header_footer_color": "a63b3b",
        "table_border_color": "f3a3a3",
        "table_header_fg": "8a1818",
        "table_header_bg": "fee2e2",
        "table_alt_row_bg": "fef2f2",
    },
    {
        "id": "brown",
        "label": "赭石",
        "description": "赭石棕，适合装饰装修、园林绿化、古建类方案。",
        "swatches": ["#6b4423", "#a97142", "#f5e9dc"],
        "heading_color": "6b4423",
        "heading_border_color": "a97142",
        "header_footer_color": "7f5a37",
        "table_border_color": "d8b795",
        "table_header_fg": "5e3b1e",
        "table_header_bg": "f5e9dc",
        "table_alt_row_bg": "fbf4ec",
    },
    {
        "id": "indigo",
        "label": "靛青",
        "description": "靛青沉稳，适合电力、能源、轨道交通类方案。",
        "swatches": ["#312e81", "#6366f1", "#e0e7ff"],
        "heading_color": "312e81",
        "heading_border_color": "6366f1",
        "header_footer_color": "4a48a3",
        "table_border_color": "a5a9f6",
        "table_header_fg": "2a276f",
        "table_header_bg": "e0e7ff",
        "table_alt_row_bg": "f1f3ff",
    },
    {
        "id": "gray",
        "label": "石墨灰",
        "description": "低饱和石墨灰，适合纯文字、重内容的正式评审稿。",
        "swatches": ["#333333", "#9ca3af", "#f3f4f6"],
        "heading_color": "333333",
        "heading_border_color": "9ca3af",
        "header_footer_color": "4b5563",
        "table_border_color": "c9cdd4",
        "table_header_fg": "2f3437",
        "table_header_bg": "f3f4f6",
        "table_alt_row_bg": "fafafa",
    },
]


_LAYOUT_INDEX = {item["id"]: item for item in LAYOUT_PRESETS}
_THEME_INDEX = {item["id"]: item for item in THEME_PRESETS}

DEFAULT_LAYOUT_ID = "standard-bid"
DEFAULT_THEME_ID = "blue"


def get_layout_preset(preset_id: str | None) -> dict[str, Any] | None:
    """按 id 取版式预设；不存在时返回 None（调用方决定是否回退默认）。"""
    if not preset_id:
        return None
    return _LAYOUT_INDEX.get(str(preset_id).strip())


def get_theme_preset(theme_id: str | None) -> dict[str, Any] | None:
    """按 id 取主题预设；不存在时返回 None。"""
    if not theme_id:
        return None
    return _THEME_INDEX.get(str(theme_id).strip())


def _normalize_hex(value: str | None) -> str:
    """把颜色规范化为不带 # 的 6 位小写十六进制；非法值返回空串。"""
    raw = str(value or "").strip().lstrip("#").lower()
    if len(raw) == 3 and all(c in "0123456789abcdef" for c in raw):
        raw = "".join(c * 2 for c in raw)
    if len(raw) == 6 and all(c in "0123456789abcdef" for c in raw):
        return raw
    return ""


def theme_to_config_fields(theme: dict[str, Any] | None) -> dict[str, Any]:
    """把主题预设转换为 ExportConfig 字段（颜色做规范化，非法值直接丢弃）。"""
    if not theme:
        return {}
    mapping = {
        "heading_color": "heading_color",
        "heading_border_color": "heading_border_color",
        "header_footer_color": "header_footer_color",
        "table_border_color": "table_border_color",
        "table_header_fg": "table_header_fg",
        "table_header_bg": "table_header_bg",
        "table_alt_row_bg": "table_alt_row_bg",
    }
    result: dict[str, Any] = {}
    for src, dst in mapping.items():
        normalized = _normalize_hex(theme.get(src))
        if normalized:
            result[dst] = normalized
    return result


def layout_to_config_fields(preset: dict[str, Any] | None) -> dict[str, Any]:
    """把版式预设转换为 ExportConfig 字段（深拷贝，避免调用方改到预设常量）。"""
    if not preset:
        return {}
    fields = copy.deepcopy(preset.get("config") or {})
    # 颜色字段同样规范化，避免预设里写 # 前缀时污染文档 XML
    for key in ("heading_color", "heading_border_color", "header_footer_color",
                "table_border_color", "table_header_fg", "table_header_bg",
                "table_alt_row_bg"):
        if key in fields:
            normalized = _normalize_hex(fields[key])
            if normalized:
                fields[key] = normalized
            else:
                fields.pop(key, None)
    return fields


def resolve_export_config(config: ExportConfig | None) -> ExportConfig:
    """把「预设基底 + 调用方显式字段」合成为一个完整的 ExportConfig。

    合并顺序（后者覆盖前者）：
      1. ExportConfig 的字段默认值
      2. 版式预设（preset_id）
      3. 主题预设（theme_id）
      4. 调用方**显式传入**的字段（Pydantic v2 `model_fields_set`）

    这样两种调用方式都正确：
      - 开放 API / 脚本：只传 `{"preset_id": "formal-binding", "theme_id": "blue"}`
        → 得到完整可用的排版配置；
      - 前端表单：本地已把预设写进各字段并允许用户微调，显式字段覆盖预设，
        用户改动不会被服务端预设悄悄回滚。

    任何异常都退回原始 config（导出主流程不能因预设解析失败而中断）。
    """
    if config is None:
        return ExportConfig()
    try:
        base = ExportConfig()
        layout = get_layout_preset(getattr(config, "preset_id", ""))
        theme = get_theme_preset(getattr(config, "theme_id", ""))

        merged = base.model_dump()
        merged.update(layout_to_config_fields(layout))
        merged.update(theme_to_config_fields(theme))

        # 显式字段优先级最高
        explicit = set(getattr(config, "model_fields_set", set()) or set())
        for field in explicit:
            if field in ("preset_id", "theme_id"):
                continue
            merged[field] = getattr(config, field)

        # preset_id / theme_id 原样保留，便于导出记录里追溯用了哪套预设
        merged["preset_id"] = getattr(config, "preset_id", "") or ""
        merged["theme_id"] = getattr(config, "theme_id", "") or ""

        resolved = ExportConfig(**merged)
        return resolved
    except Exception:
        logger.warning("导出预设合并失败，回退为原始配置", exc_info=True)
        return config


def list_presets() -> dict[str, Any]:
    """返回预设元数据，供 `GET /export/presets` 与前端选择器使用。"""
    return {
        "layouts": [
            {
                "id": item["id"],
                "label": item["label"],
                "description": item["description"],
                # 前端本地应用预设时直接使用该字段片段
                "config": layout_to_config_fields(item),
            }
            for item in LAYOUT_PRESETS
        ],
        "themes": [
            {
                "id": item["id"],
                "label": item["label"],
                "description": item["description"],
                "swatches": list(item.get("swatches") or []),
                "config": theme_to_config_fields(item),
            }
            for item in THEME_PRESETS
        ],
        "defaults": {
            "layout_id": DEFAULT_LAYOUT_ID,
            "theme_id": DEFAULT_THEME_ID,
        },
    }
