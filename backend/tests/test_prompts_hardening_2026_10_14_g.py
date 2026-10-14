"""提示词模块 · 遗留项加固回归测试（2026-10-14 BUG-PROMPT-G4 / G5）。

覆盖上一轮深度审计报告中标记为「遗留 / 低优先级」的两处技术债，本轮按最优方案加固：

BUG-PROMPT-G4（校验口径不一致）
  get_prompt_with_validation 之前用 validate_prompt_variables(key) 校验的是
  **注册表默认模板**的变量集合，而它返回的正文却由 get_prompt 按 **实际生效模板**
  （DB 优先、可能被用户增删占位符）渲染——这正是 get_prompt 内 BUG-FIX-PROMPT-D3
  已修复、但本函数遗漏的同一问题：DB 新增占位符漏检、DB 删除占位符误报噪声。
  现改为对「实际模板内容」做校验与渲染，与 get_prompt 完全同口径。

BUG-PROMPT-G5（上下文优先级缺失）
  PROJECT_SCOPE（承包范围）是 build_context_block 的核心片段，却从未列入
  DEFAULT_PRIORITIES，静默回落到魔法默认值 50——低于 INDUSTRY_PROFILE/CONTEXT/
  BOQ_ANALYSIS/CONSTRUCTION_METHOD 等所有具名片段，预算收紧时「界定项目边界」的
  承包范围反而最先被截断/丢弃。现显式置于 72。
"""

from __future__ import annotations

import pytest

from app.services.ai.prompts import (
    _ALL_PROMPTS,
    get_prompt,
    get_prompt_with_validation,
)
from app.services.ai.prompts import _cache
from app.services.ai.prompts._context import DEFAULT_PRIORITIES, assemble_prompt_context
from app.services.ai.prompts._registry import _reg

PROBE_KEY = "_G4_PROBE_KEY_DO_NOT_USE"


@pytest.fixture
def probe_cache():
    """临时把 DB 优先缓存替换为探针内容，并在用例后完整还原，避免污染注册表/缓存。"""
    orig_cache = _cache._prompt_cache
    orig_meta = _ALL_PROMPTS.get(PROBE_KEY)
    _reg(PROBE_KEY, "test", "G4探针", "注册表默认仅 {FOO}")
    try:
        yield
    finally:
        _cache._prompt_cache = orig_cache
        if orig_meta is not None:
            _ALL_PROMPTS[PROBE_KEY] = orig_meta
        else:
            _ALL_PROMPTS.pop(PROBE_KEY, None)


class TestPromptG4ValidationCaliber:
    def test_missing_reflects_actual_db_not_registry_default(self, probe_cache):
        """核心：DB 实际模板新增的 {BAR} 必须被报缺（旧口径只看注册表 {FOO} → 漏检）。"""
        _cache._prompt_cache = {PROBE_KEY: "实际内容 {FOO} 与 {BAR}"}
        prompt, missing = get_prompt_with_validation(PROBE_KEY, FOO="甲")
        assert missing == ["BAR"], f"应按实际模板报缺 BAR，实得 {missing}"
        # 未提供的占位符在渲染产物中原样保留
        assert "甲" in prompt
        assert "{BAR}" in prompt

    def test_removed_registry_var_no_longer_false_flagged(self, probe_cache):
        """核心：DB 实际模板删掉的占位符不得再被误报为缺失（旧口径噪声）。"""
        # 注册表默认有 {FOO}，但 DB 实际模板已不含 {FOO}
        _cache._prompt_cache = {PROBE_KEY: "实际内容只有 {ZED}"}
        prompt, missing = get_prompt_with_validation(PROBE_KEY, ZED="乙")
        assert "FOO" not in missing, f"注册表遗留变量 FOO 不应被误报缺失: {missing}"
        assert missing == [], f"实际占位符已全部提供，应无缺失: {missing}"
        assert "乙" in prompt

    def test_rendered_output_matches_get_prompt_exactly(self, probe_cache):
        """同口径保证：validation 渲染产物必须与 get_prompt(key, **kwargs) 完全一致。"""
        _cache._prompt_cache = {PROBE_KEY: "{FOO}-{BAR}"}
        p1, missing = get_prompt_with_validation(PROBE_KEY, FOO="x", BAR="y")
        p2 = get_prompt(PROBE_KEY, FOO="x", BAR="y")
        assert missing == []
        assert p1 == p2 == "x-y"

    def test_no_kwargs_returns_raw_and_no_missing(self, probe_cache):
        """无变量注入调用：返回未渲染实际模板本体，missing 恒为空。"""
        _cache._prompt_cache = {PROBE_KEY: "原文 {FOO} {BAR}"}
        prompt, missing = get_prompt_with_validation(PROBE_KEY)
        assert missing == []
        assert prompt == "原文 {FOO} {BAR}"

    def test_unknown_key_returns_empty_pair(self):
        """未注册的 key：返回空串与空缺失列表，不抛异常。"""
        prompt, missing = get_prompt_with_validation("__NOT_A_REAL_PROMPT_KEY__", A="1")
        assert prompt == ""
        assert missing == []


class TestPromptG5ProjectScopePriority:
    def test_project_scope_is_explicitly_listed(self):
        """PROJECT_SCOPE 必须显式列入优先级表，且高于魔法回落值 50。"""
        assert "PROJECT_SCOPE" in DEFAULT_PRIORITIES, "承包范围未列入 DEFAULT_PRIORITIES（G5 回归）"
        assert DEFAULT_PRIORITIES["PROJECT_SCOPE"] > 50, "承包范围优先级不应回落到魔法默认 50"

    def test_project_scope_ranking_between_named_parts(self):
        """对齐实质重要性：行业画像 > 承包范围 >= 通用上下文/BOQ。"""
        assert DEFAULT_PRIORITIES["INDUSTRY_PROFILE"] > DEFAULT_PRIORITIES["PROJECT_SCOPE"]
        assert DEFAULT_PRIORITIES["PROJECT_SCOPE"] >= DEFAULT_PRIORITIES["CONTEXT"]
        assert DEFAULT_PRIORITIES["PROJECT_SCOPE"] >= DEFAULT_PRIORITIES["BOQ_ANALYSIS"]

    def test_scope_outranks_unlisted_fallback_under_budget(self):
        """行为验证：预算收紧时，72 优先的 PROJECT_SCOPE 应先于未列名（回落50）片段保留。

        关键设计：把未列名片段放在 dict 最前——修复前二者同为 50、稳定排序保持插入顺序，
        未列名片段会胜出；修复后 PROJECT_SCOPE=72 优先，稳定胜出。
        """
        parts = {
            "ZZZ_UNLISTED": "其他" * 3000,   # 未列入优先级表 → 回落 50
            "PROJECT_SCOPE": "范围" * 3000,   # 修复后 72 > 50
        }
        out = assemble_prompt_context(parts, max_tokens=60)
        assert "【PROJECT_SCOPE】" in out, "承包范围应以更高优先级作为边界片段保留"
        assert "【ZZZ_UNLISTED】" not in out, "更低优先级的未列名片段应整体丢弃"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v", "-p", "no:randomly"]))
