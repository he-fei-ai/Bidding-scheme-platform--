# -*- coding: utf-8 -*-
"""AI 生图风格约束 —— 唯一正本（ENHANCE 2026-10-05 / P-16）。

对齐易标规格六.3「图片生成流程：提示词 + 图片风格要求」：风格约束是生成
提示词的一部分，必须有用户可改的入口，而不是散落硬编码。

在此之前，同一套风格文案被**逐字复制了三遍**：

    | 位置 | 用途 |
    | --- | --- |
    | `image_providers/service.py::_inject_style`       | provider 适配层注入 |
    | `image_engine.py`（两处 style_prefix 字典）        | 编排/渲染管线注入 |
    | `prompts/illustration.py`                         | 图片提示词注册表 |

任一处改动都不会同步到另外两处，于是「避免品牌标识/水印」这类硬约束
在不同调用路径下时隐时现。本模块把三份文案收敛为一份，并通过
`IMAGE_STYLE_OVERRIDE` 环境变量支持按风格整体覆盖。

覆盖格式（JSON，键为风格 id，值为完整替换文案）：

    IMAGE_STYLE_OVERRIDE='{"realistic_photo":"画面采用……","my_style":"……"}'

未覆盖的风格沿用默认文案，故默认行为与历史完全一致（向后兼容）。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


#: 易标规格六.3 风格要求（正本）。
#: 文案要点：工程项目图示风格 / 专业克制 / 避免品牌标识、水印、夸张营销元素。
DEFAULT_IMAGE_STYLES: dict[str, str] = {
    "engineering_diagram": (
        "画面采用工程项目图示风格，结构清晰、专业克制、适合投标技术方案插图。"
        "避免出现品牌标识、水印、夸张营销元素和无关文字。"
    ),
    "realistic_photo": (
        "画面采用专业实景照片风格，真实、克制、适合投标技术方案插图。"
        "避免出现品牌标识、水印、夸张营销元素和无关文字。"
    ),
    "technical_drawing": (
        "画面采用工程技术图纸风格，线条清晰、标注规范、适合投标技术方案。"
        "避免出现品牌标识、水印、夸张营销元素和无关文字。"
    ),
}

#: 默认风格（style 未知或为空时回退）
DEFAULT_STYLE = "engineering_diagram"

#: 允许的风格 id（供路由层参数校验用）
IMAGE_STYLE_IDS: tuple[str, ...] = tuple(DEFAULT_IMAGE_STYLES.keys())

_OVERRIDE_CACHE: dict[str, str] | None = None


def _load_override() -> dict[str, str]:
    """解析 IMAGE_STYLE_OVERRIDE 环境变量；解析失败则忽略（保持默认）。"""
    global _OVERRIDE_CACHE
    if _OVERRIDE_CACHE is not None:
        return _OVERRIDE_CACHE

    raw = os.environ.get("IMAGE_STYLE_OVERRIDE", "").strip()
    if not raw:
        _OVERRIDE_CACHE = {}
        return _OVERRIDE_CACHE

    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError) as e:
        logger.warning(
            "IMAGE_STYLE_OVERRIDE 解析失败，已忽略并使用默认风格文案: %s (raw=%r)", e, raw[:200]
        )
        _OVERRIDE_CACHE = {}
        return _OVERRIDE_CACHE

    if not isinstance(parsed, dict):
        logger.warning("IMAGE_STYLE_OVERRIDE 必须是 JSON 对象，已忽略: %r", raw[:200])
        _OVERRIDE_CACHE = {}
        return _OVERRIDE_CACHE

    clean = {
        str(k): str(v)
        for k, v in parsed.items()
        if isinstance(v, (str, int, float)) and str(v).strip()
    }
    _OVERRIDE_CACHE = clean
    if clean:
        logger.info("已加载 AI 生图风格覆盖: %s", sorted(clean.keys()))
    return clean


def get_image_style(style: str | None, *, include_fallback: bool = True) -> str:
    """返回某风格的完整约束文案。

    Args:
        style: 风格 id（如 engineering_diagram / realistic_photo / technical_drawing），
            也支持自定义风格 id（通过 IMAGE_STYLE_OVERRIDE 注册）。
        include_fallback: True 时未知风格回退到默认风格文案；
            False 时未知风格返回空串（调用方想自行判断是否注入时用）。

    Returns:
        str: 风格约束文案；无匹配且 include_fallback=False 时为空串。
    """
    override = _load_override()
    key = str(style or "").strip() or DEFAULT_STYLE

    if key in override:
        return override[key]
    if key in DEFAULT_IMAGE_STYLES:
        return DEFAULT_IMAGE_STYLES[key]
    if include_fallback and DEFAULT_STYLE in override:
        return override[DEFAULT_STYLE]
    if include_fallback:
        return DEFAULT_IMAGE_STYLES[DEFAULT_STYLE]
    return ""


def list_image_styles() -> list[dict[str, Any]]:
    """列出全部可用风格（供前端配置界面枚举）。"""
    override = _load_override()
    ids = list(dict.fromkeys(list(DEFAULT_IMAGE_STYLES.keys()) + list(override.keys())))
    return [
        {
            "id": sid,
            "label": _style_label(sid),
            "prompt": override.get(sid) or DEFAULT_IMAGE_STYLES.get(sid, ""),
            "custom": sid in override,
            "default": sid == DEFAULT_STYLE,
        }
        for sid in ids
    ]


_STYLE_LABELS: dict[str, str] = {
    "engineering_diagram": "工程项目图示",
    "realistic_photo": "专业实景照片",
    "technical_drawing": "工程技术图纸",
}


def _style_label(sid: str) -> str:
    return _STYLE_LABELS.get(sid, "自定义风格")


def invalidate_cache() -> None:
    """清空覆盖缓存（配置变更后调用，便于测试与热更新）。"""
    global _OVERRIDE_CACHE
    _OVERRIDE_CACHE = None
