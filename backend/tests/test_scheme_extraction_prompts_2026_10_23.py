# -*- coding: utf-8 -*-
"""五段式专项提取提示词测试（标书智能体（一）引入）。

验证：
- 五个提取任务的 *SYSTEM/*USER 提示词已在注册表注册；
- 每个 *SYSTEM 严格遵循五段式结构（目标定位/提取内容要求/处理规则/输出示例/验证步骤）；
- 提示词复用共享上下文（"上方提供的文档全文"），不内嵌全文，保证缓存命中；
- 五个任务覆盖文档「引入方案」定义的全部字段清单。
"""

import pytest

from app.services.ai.prompts import _ALL_PROMPTS, get_prompt
from app.services.ai.prompts.scheme_extraction import (
    SCHEME_EXTRACTION_TASKS,
    build_five_part_prompt,
)


_FIVE_PART_MARKERS = [
    "### 1. 目标定位",
    "### 2. 提取内容要求",
    "### 3. 处理规则",
    "### 4. 输出示例",
    "### 5. 验证步骤",
]


@pytest.mark.parametrize(
    "task,system_key,user_key",
    [
        ("project_overview", "PROJECT_OVERVIEW_SYSTEM", "PROJECT_OVERVIEW_USER"),
        ("scheme_type", "SCHEME_TYPE_SYSTEM", "SCHEME_TYPE_USER"),
        ("dangerous_work_params", "DANGEROUS_WORK_PARAMS_SYSTEM", "DANGEROUS_WORK_PARAMS_USER"),
        ("basis_list", "BASIS_LIST_SYSTEM", "BASIS_LIST_USER"),
        ("construction_condition", "CONSTRUCTION_CONDITION_SYSTEM", "CONSTRUCTION_CONDITION_USER"),
    ],
)
def test_task_prompts_registered(task, system_key, user_key):
    assert task in SCHEME_EXTRACTION_TASKS
    assert system_key in _ALL_PROMPTS, f"{system_key} 未注册"
    assert user_key in _ALL_PROMPTS, f"{user_key} 未注册"


@pytest.mark.parametrize("system_key", [v[0] for v in SCHEME_EXTRACTION_TASKS.values()])
def test_system_prompt_follows_five_part_structure(system_key):
    text = get_prompt(system_key)
    for marker in _FIVE_PART_MARKERS:
        assert marker in text, f"{system_key} 缺少五段式标记: {marker}"


@pytest.mark.parametrize("system_key", [v[0] for v in SCHEME_EXTRACTION_TASKS.values()])
def test_system_prompt_uses_shared_context_not_inline_fulltext(system_key):
    text = get_prompt(system_key)
    # 复用共享上下文块（ANALYSIS_CONTEXT_BLOCK），提示词中引用"上方提供的文档原文"，
    # 由上下文块（ANALYSIS_CONTEXT_BLOCK）承载全文，任务提示词本身不内嵌全文
    assert "上方提供的文档原文" in text
    # 不应出现注册表 CONTEXT 占位符（全文由上下文块单独承载）
    assert "{CONTEXT}" not in text


def test_project_overview_covers_required_fields():
    text = get_prompt("PROJECT_OVERVIEW_SYSTEM")
    for field in ["工程名称", "工程地点", "建设规模", "结构形式", "地质条件", "周边环境", "参建单位"]:
        assert field in text


def test_scheme_type_covers_six_dangerous_categories():
    text = get_prompt("SCHEME_TYPE_SYSTEM")
    for cat in ["基坑工程", "模板工程及支撑体系", "脚手架工程", "起重吊装及安装拆卸工程", "拆除工程", "其他"]:
        assert cat in text


def test_dangerous_work_params_covers_required_params():
    text = get_prompt("DANGEROUS_WORK_PARAMS_SYSTEM")
    for p in ["开挖深度", "支撑高度", "跨度", "施工总荷载", "集中线荷载", "起吊重量", "脚手架搭设高度", "幕墙施工高度", "钢结构跨度", "人工挖孔桩深度"]:
        assert p in text


def test_basis_list_requires_original_code():
    text = get_prompt("BASIS_LIST_SYSTEM")
    assert "不补全" in text and "不简写" in text
    for cat in ["法规", "标准", "施工图", "施组", "勘察报告", "合同"]:
        assert cat in text


def test_construction_condition_covers_required_categories():
    text = get_prompt("CONSTRUCTION_CONDITION_SYSTEM")
    for cat in ["场地", "交通", "水电", "周边建筑", "地下管线"]:
        assert cat in text


def test_build_five_part_prompt_helper():
    out = build_five_part_prompt(
        goal="目标说明",
        fields=["字段A", "字段B"],
        rules="规则说明",
        examples="示例说明",
        self_check=["检查项1", "检查项2"],
        output_hint="JSON 数组",
    )
    for marker in _FIVE_PART_MARKERS:
        assert marker in out
    assert "字段A" in out
    assert "JSON 数组" in out
