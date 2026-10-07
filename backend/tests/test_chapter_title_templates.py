"""专项方案名称源（chapter_title_templates.json）启动期健全性测试。

遗留加固项：该 JSON 含 324+ 条专项方案/章节标题模板，是「专项方案名称」注入的
唯一数据源。模块加载失败时仅 warning 并回退空 dict（`workflows_outline_utils.py`
的 try/except），不会抛异常——因此需要一个显式护栏在测试期捕捉「文件损坏/缺失/
结构非法」导致的静默降级，避免目录生成在无提示下质量回退。
"""
from __future__ import annotations

import json
import os

from app.services.ai.workflows_outline_utils import CHAPTER_TITLE_TEMPLATES

_JSON_PATH = os.path.join(
    os.path.dirname(__file__), "..", "app", "services", "ai", "prompts",
    "chapter_title_templates.json",
)


def test_chapter_title_templates_loaded_and_nonempty():
    # 加载失败（JSON 损坏/缺失）时模块仅 warning 并回退空 dict，此处捕捉静默降级
    assert isinstance(CHAPTER_TITLE_TEMPLATES, dict)
    # 正常约 28 个顶层专项方案类别；加载失败（JSON 损坏/缺失）时回退空 dict=0，
    # 此处阈值取明显低于正常量、又高于「被截断成极少数」的下限，捕捉静默降级。
    assert len(CHAPTER_TITLE_TEMPLATES) > 10, "专项方案名称源异常偏少，疑似 JSON 加载失败"


def test_chapter_title_templates_structure_valid():
    # 已知存在的专项方案键，结构校验的锚点
    assert "项目概述" in CHAPTER_TITLE_TEMPLATES
    for key, val in CHAPTER_TITLE_TEMPLATES.items():
        assert isinstance(val, dict), f"条目 {key} 非 dict"
        # level2 必为 list（加载代码直接取值，缺失会 KeyError；这里再显式守护）
        assert isinstance(val.get("level2"), list) and val["level2"], f"{key}.level2 非法"
        # level3 可为 dict 或 list，但必须为真（非空）
        assert val.get("level3"), f"{key}.level3 缺失或为空"


def test_chapter_title_templates_file_parseable():
    # 双保险：直接读取文件确认可解析且为 dict（与内存对象口径交叉验证）
    with open(_JSON_PATH, encoding="utf-8") as f:
        data = json.load(f)
    assert isinstance(data, dict) and data
