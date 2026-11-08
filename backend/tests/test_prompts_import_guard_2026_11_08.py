"""提示词模块导入守卫与资产完整性（2026-11-08）。

背景（P0 崩溃修复的回归守卫）：
    ``_integrate_batch2/3/4.py`` 三个收编批次模块此前被 ``.gitignore`` 的
    全局 ``_*.py`` 规则误伤而从未入库（见 .gitignore 第 87 行与修复注释），
    而 ``app/services/ai/prompts/__init__.py`` 仍 ``from . import _integrate_batch2/3/4``，
    导致 ``import app.services.ai.prompts`` 抛 ``ImportError`` → 整个
    ``app.services.ai`` 无法导入 → 后端启动崩溃（已实测复现）。

    本文件锁定修复后的不变量：
      1. prompts 包可完整导入（无 ImportError / 无 ModuleNotFoundError）；
      2. batch2/3/4 的收编 key 全部注册（管理页可见、DB 可覆盖）；
      3. 全部模板通过 ``validate_prompt_content``（无坏共享引用 / 无畸形占位符）；
      4. 全部模板 ``get_prompt(key)`` 可渲染且非空（DB 不可用时回退硬编码）。

    若未来 .gitignore / 文件删除再次导致任一不变量破坏，本文件立即红牌。
"""

from __future__ import annotations

import pytest


# batch2 / batch3 / batch4 收编的 key（与 _integrate_batch*.py 的 _reg 调用一一对应）。
# 来源：恢复文件 `_reg(` 首参逐一提取（2026-11-08）。
_BATCH2_KEYS = [
    "CHART_ARRANGE_USER",
    "CHART_MERMAID_GEN_USER",
    "CHART_STRUCT_DATA_GEN_USER",
    "CHART_DATA_FIX_USER",
    "CHART_REPAIR_CONTEXT_BLOCK",
    "CHART_TYPE_MISMATCH_WARNING",
    "MERMAID_REPAIR_USER",
    "INLINE_CHART_DETECT_SYSTEM",
    "INLINE_CHART_DETECT_USER",
    "CONTENT_SKELETON_USER",
    "QUALITY_EVAL_USER",
    "OUTLINE_EXPAND_SYSTEM",
    "OUTLINE_EXPAND_USER",
    "SECTION_EXPAND_SYSTEM",
    "SECTION_EXPAND_USER",
    "CONSISTENCY_AUDIT_SYSTEM",
    "CONSISTENCY_AUDIT_USER",
    "CONSISTENCY_REPAIR_SYSTEM",
    "CONSISTENCY_REPAIR_USER",
]

_BATCH3_KEYS = [
    "FACTS_EXTRACT_SYSTEM",
    "FACTS_KB_SUPPLEMENT_SYSTEM",
    "FACTS_KB_SUPPLEMENT_USER",
    "FACTS_TPL_SUPPLEMENT_SYSTEM",
    "FACTS_TPL_SUPPLEMENT_USER",
    "FACTS_AUDIT_SYSTEM",
    "QA_SYSTEM_ROLE",
    "QA_CONTEXT_TENDER_BLOCK",
    "QA_CONTEXT_SECTIONS_BLOCK",
    "QA_ANSWER_REQUIREMENTS",
    "TEMPLATE_EXTRACT_OUTLINE_OBJECT_SYSTEM",
    "JSON_REPAIR_SYSTEM_LEGACY",
    "MERMAID_REPAIR_SYSTEM_ANY",
]

_BATCH4_KEYS = [
    "FEASIBILITY_ANALYSIS_SYSTEM",
    "FEASIBILITY_ANALYSIS_MERGE_SYSTEM",
    "FEASIBILITY_ANALYSIS_MERGE_USER",
    "FEASIBILITY_OUTLINE_SYSTEM",
    "FEASIBILITY_PARAMETERS_SYSTEM",
    "FEASIBILITY_PARAMETERS_USER",
    "FEASIBILITY_CONTENT_SYSTEM",
    "FEASIBILITY_CONTENT_WRITING_RULES",
    "FEASIBILITY_KB_BACKGROUND_NOTE",
    "FEASIBILITY_KB_CATALOG_NOTE",
    "FEASIBILITY_KB_MATERIAL_NOTE",
    "FEASIBILITY_HUMAN_WRITING_SYSTEM",
    "FEASIBILITY_ANALYSIS_USER",
    "FEASIBILITY_OUTLINE_USER",
    "KB_REFILL_EXTRACT_USER",
    "KB_REFILL_GAP_USER",
]


def _import_prompts():
    """导入提示词包（P0 崩溃的核心判定）。"""
    import app.services.ai.prompts as P

    return P


def test_prompts_package_imports_ok():
    """P0：prompts 包必须可完整导入（batch2/3/4 缺失时此处抛 ImportError）。"""
    P = _import_prompts()
    assert P is not None


def test_registry_has_substantial_templates():
    """注册表规模合理（历史基线 135，含 3 条共享规则）。"""
    P = _import_prompts()
    assert len(P._ALL_PROMPTS) >= 130


@pytest.mark.parametrize("key", _BATCH2_KEYS)
def test_batch2_keys_registered(key):
    """batch2 收编 key 全部注册。"""
    assert key in _import_prompts()._ALL_PROMPTS


@pytest.mark.parametrize("key", _BATCH3_KEYS)
def test_batch3_keys_registered(key):
    """batch3 收编 key 全部注册。"""
    assert key in _import_prompts()._ALL_PROMPTS


@pytest.mark.parametrize("key", _BATCH4_KEYS)
def test_batch4_keys_registered(key):
    """batch4 收编 key 全部注册。"""
    assert key in _import_prompts()._ALL_PROMPTS


def test_shared_rules_registered():
    """三条共享规则注册（管理页只读资产）。"""
    P = _import_prompts()
    for k in ("SHARED_HEADING_RULES", "SHARED_FORBIDDEN_WORDS", "SHARED_OUTPUT_SPEC"):
        assert k in P._ALL_PROMPTS


def test_all_templates_validate():
    """全部模板通过内容校验（无坏共享引用 / 无畸形占位符 / 未删默认变量）。"""
    from app.services.ai.prompts._registry import validate_prompt_content

    P = _import_prompts()
    problems = []
    for key, meta in P._ALL_PROMPTS.items():
        for p in validate_prompt_content(key, meta["content"]):
            problems.append(f"{key}: {p}")
    assert not problems, "\n".join(problems)


def test_all_templates_render_nonempty():
    """全部模板无变量 get_prompt 渲染非空（DB 不可用时回退硬编码）。"""
    from app.services.ai.prompts import get_prompt

    P = _import_prompts()
    empty = [k for k, meta in P._ALL_PROMPTS.items() if not get_prompt(k)]
    # 允许空模板？注册表 content 均非空；get_prompt 回退硬编码后应非空。
    assert not empty, f"渲染为空: {empty}"


def test_batch4_fallback_texts_match_registry():
    """batch4 关键收编的 fallback 文本与注册表逐字一致（行为零变化守卫）。

    覆盖 facts / templates / qa 三处已在生产接线且依赖「fallback == 注册表」的链路。
    """
    from app.services.ai.prompts import get_prompt

    # facts_service._FACTS_EXTRACT_SYSTEM_FB 与 FACTS_EXTRACT_SYSTEM 逐字一致
    from app.services.facts_service import _FACTS_EXTRACT_SYSTEM_FB

    assert _FACTS_EXTRACT_SYSTEM_FB == get_prompt("FACTS_EXTRACT_SYSTEM")

    # routers/templates._TEMPLATE_EXTRACT_SYSTEM_FB 与 TEMPLATE_EXTRACT_SYSTEM 一致
    from app.routers.templates import _TEMPLATE_EXTRACT_SYSTEM_FB

    assert _TEMPLATE_EXTRACT_SYSTEM_FB == get_prompt("TEMPLATE_EXTRACT_SYSTEM")
