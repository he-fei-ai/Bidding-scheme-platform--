"""Mermaid 图表渲染公共工具：字体、文本绘制、300 DPI 装饰器、Gantt 坐标。

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
    "C:\\Windows\\Fonts\\msyh.ttc",
    "C:\\Windows\\Fonts\\msyhbd.ttc",
    "C:\\Windows\\Fonts\\simsun.ttc",
    "C:\\Windows\\Fonts\\simhei.ttf",
    "C:\\Windows\\Fonts\\yahei.ttf",
    "C:\\Windows\\Fonts\\deng.ttf",
    "C:\\Windows\\Fonts\\FZSTK.TTF",
    "C:\\Windows\\Fonts\\FZHTK.TTF",
]

_DEFAULT_BOLD_FONT_PATHS = [
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "C:\\Windows\\Fonts\\msyhbd.ttc",
    "C:\\Windows\\Fonts\\simhei.ttf",
    "C:\\Windows\\Fonts\\msyh.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc",
]


def _resolve_font_paths(bold: bool = False) -> list[str]:
    """解析字体路径列表，优先级：环境变量 > 内置默认值

    环境变量 FONT_PATH 可指定一个或多个字体路径（分号分隔），
    若该环境变量存在则优先使用其中的路径；
    否则使用内置的 _DEFAULT_FONT_PATHS / _DEFAULT_BOLD_FONT_PATHS。
    """
    env_fonts = os.environ.get("FONT_PATH", "").strip()
    if env_fonts:
        return [p.strip() for p in env_fonts.split(";") if p.strip()]
    if bold:
        # 粗体优先列表 + 常规列表作为后备
        return _DEFAULT_BOLD_FONT_PATHS + _DEFAULT_FONT_PATHS
    return list(_DEFAULT_FONT_PATHS)

def _auto_detect_chinese_font() -> list[str]:
    """自动检测系统中可用的中文字体，返回发现的字体路径列表。

    在 Windows 上扫描 C:\\Windows\\Fonts 下常见中文字体模式；
    在 macOS 上扫描 /System/Library/Fonts 和 ~/Library/Fonts；
    在 Linux 上扫描 /usr/share/fonts 下的常见路径。
    """
    detected = []
    if sys.platform == "win32":
        font_dir = "C:\\Windows\\Fonts"
        patterns = ["msyh*", "simsun*", "simhei*", "yahei*", "deng*", "FZST*", "FZHT*", "mf*"]
        if os.path.isdir(font_dir):
            for pat in patterns:
                try:
                    from glob import glob

                    matched = glob(os.path.join(font_dir, pat))
                    detected.extend(matched)
                except Exception:
                    continue
    elif sys.platform == "darwin":
        search_dirs = [
            "/System/Library/Fonts",
            "/System/Library/Fonts/Supplemental",
            os.path.expanduser("~/Library/Fonts"),
        ]
        for d in search_dirs:
            if os.path.isdir(d):
                try:
                    for f in os.listdir(d):
                        if any(
                            kw in f.lower()
                            for kw in ["stheit", "songti", "pingfang", "noto", "cjk"]
                        ):
                            detected.append(os.path.join(d, f))
                except Exception:
                    continue
    else:  # Linux
        search_dirs = ["/usr/share/fonts", "/usr/local/share/fonts", os.path.expanduser("~/.fonts")]
        for d in search_dirs:
            if os.path.isdir(d):
                try:
                    for root, dirs, files in os.walk(d):
                        for f in files:
                            if any(
                                kw in f.lower()
                                for kw in ["noto", "cjk", "wqy", "droid", "chinese", "hans", "sc"]
                            ):
                                detected.append(os.path.join(root, f))
                except Exception:
                    continue
    return detected

def _image_font(size: int, bold: bool = False):
    if ImageFont is None:
        return None

    # PERF-FIX 2026-09-18（PO-1）：大文档导出每图调 5 次本函数，150 图≈ 750 次
    # ImageFont.truetype 重新打开解析 ~20MB 的 msyh.ttc；失败路径还会每次
    # glob 扫描字体目录。字体对象创建后只读、渲染路径持有 GIL，可跨线程
    # 复用，故对加载结果与自动探测列表做 lru_cache 记忆化；
    # 加载抛 OSError 时不会被缓存，降级语义不变。
    # 1) 解析字体路径（环境变量 > 内置默认值）
    preferred = _resolve_font_paths(bold=bold)

    # 2) 尝试每个可用路径
    tried = []
    for font_path in preferred:
        if not font_path:
            continue
        try:
            return _load_truetype_cached(font_path, size)
        except OSError:
            tried.append(font_path)
            continue

    # 3) 自动检测中文字体（兜底搜索，探测结果全局只算一次）
    auto_fonts = _get_auto_detected_fonts()
    for font_path in auto_fonts:
        if font_path in tried:
            continue
        try:
            logger.info("_image_font: 自动检测到中文字体: %s", font_path)
            return _load_truetype_cached(font_path, size)
        except OSError:
            continue

    # 4) 最后降级：PIL 默认字体
    try:
        return ImageFont.load_default()
    except Exception:
        return None


@functools.lru_cache(maxsize=128)
def _load_truetype_cached(font_path: str, size: int):
    """按 (路径,字号) 记忆化加载字体对象（异常不入缓存，调用方自行捕获 OSError）"""
    return ImageFont.truetype(font_path, size)


@functools.lru_cache(maxsize=1)
def _get_auto_detected_fonts():
    """自动字体探测目录扫描结果全局缓存（首次成功后不再重复 glob）"""
    return tuple(_auto_detect_chinese_font())

def _render_hq(func):
    """Decorator: 高保真输出 — 固定 300 DPI 元数据，不降采样

    V4.0 重写：
      - 不再降采样（保留原始 v2 渲染器的完整分辨率）
      - 仅添加 300 DPI 元数据，确保 DOCX 打印质量
      - 确保输出为 PNG 格式
      - 保留原始色彩深度和透明度

    V4.1 修复：
      - 检测传入的 BytesIO 流是否已包含 300 DPI 元数据，如果已有则跳过重复编解码
      - 避免与 _image_to_stream 的双重 PNG 编码

    注意：v2 渲染器自身应使用 2.5x 缩放渲染以获得超采样抗锯齿效果，
    此装饰器仅负责最终输出质量标准化。
    """
    import functools

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        result = func(*args, **kwargs)
        if result is None:
            return None
        try:
            from PIL import Image as PilImage

            # BUG-MERMAID-IMG 修复：原代码 img = PilImage.open(result) 后
            # 没有显式 close()，PIL Image 持有底层内存/文件句柄，在高并发渲染
            # 场景下（后台任务批量生成配图）会累计造成资源耗尽。改用 with
            # 确保每次处理完自动释放。
            with PilImage.open(result) as img:
                # 检查是否已包含 300 DPI 元数据，避免重复编解码（V1.1 修复：使用 round 避免浮点精度问题）
                existing_dpi = img.info.get("dpi")
                if existing_dpi and len(existing_dpi) == 2:
                    dpi_x, dpi_y = existing_dpi
                    if round(dpi_x) == 300 and round(dpi_y) == 300:
                        result.seek(0)
                        return result
                buf = BytesIO()
                # 仅添加 300 DPI 元数据，不改变像素尺寸
                img.save(buf, format="PNG", optimize=True, dpi=(300, 300))
            buf.seek(0)
            return buf
        except Exception:
            result.seek(0)
            return result

    return wrapper

def _image_to_stream(image: Image.Image) -> BytesIO:
    """将 PIL Image 保存为 PNG 字节流（增强：300 DPI + 优化）"""
    buf = BytesIO()
    try:
        image.save(buf, format="PNG", optimize=True, dpi=(300, 300))
    except Exception:
        image.save(buf, format="PNG")
    buf.seek(0)
    return buf

def _text_width(draw, text: str, font) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]

def _text_height(draw, text: str, font) -> int:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[3] - bbox[1]

def _draw_text_center(draw, box, text: str, font, fill: str, max_lines: int | None = None):
    x1, y1, x2, y2 = box
    lines = _wrap_text(draw, text, font, int(x2 - x1), max_lines=max_lines)
    line_h = max(16, _text_height(draw, "国", font) + 4)
    total_h = line_h * len(lines)
    y = y1 + max(0, (y2 - y1 - total_h) / 2)
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        text_w = bbox[2] - bbox[0]
        draw.text((x1 + (x2 - x1 - text_w) / 2, y), line, fill=fill, font=font)
        y += line_h

def _wrap_text(draw, text: str, font, max_width: int, max_lines: int | None = None) -> list[str]:
    text = str(text or "").strip()
    if not text:
        return [""]
    lines = []
    current = ""
    for char in text:
        candidate = current + char
        if _text_width(draw, candidate, font) <= max_width or not current:
            current = candidate
            continue
        lines.append(current)
        current = char
        if max_lines and len(lines) >= max_lines:
            break
    if current and (not max_lines or len(lines) < max_lines):
        lines.append(current)
    if max_lines and len(lines) == max_lines and "".join(lines) != text:
        lines[-1] = _fit_text_with_ellipsis(draw, lines[-1], font, max_width)
    return lines[:max_lines] if max_lines else lines

def _fit_text_with_ellipsis(draw, text: str, font, max_width: int) -> str:
    value = text
    while value and _text_width(draw, f"{value}...", font) > max_width:
        value = value[:-1]
    return f"{value}..." if value else "..."

def _build_gantt_marks(duration: int, requested_tick: int, max_columns: int = 18) -> list[int]:
    duration = max(1, duration)
    requested_tick = max(1, requested_tick)
    max_columns = max(4, max_columns)
    nice_steps = [1, 2, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180]
    step = next(
        (v for v in nice_steps if v >= requested_tick and duration // v + 2 <= max_columns),
        requested_tick,
    )
    while duration // step + 2 > max_columns:
        step += requested_tick
    marks = list(range(0, duration + 1, step))
    if marks[-1] != duration:
        marks.append(duration)
    return marks

