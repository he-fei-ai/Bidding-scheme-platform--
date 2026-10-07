"""提示词模块深度审计与修复回归测试（2026-10-04）。

覆盖本轮修复项：
  - B-1 短标书直出路径（generate_outline_direct）此前直接 import 硬编码常量并用
    str.replace 注入，导致提示词管理页对 OUTLINE_DIRECT_SYSTEM/USER 的 DB 编辑
    完全不生效。现改为走 get_prompt，验证 DB 覆盖能生效且变量被正确渲染。
  - B-2 L3 标题修复统一走 render_prompt 注入（验证 DB 覆盖 + 大小写/ {var} 兼容）。
  - D-3 get_prompt 变量校验改为「按实际生效模板内容」，而非注册表默认模板：
      · DB 模板删掉默认变量后不再误报「缺失变量」告警（消除噪声）；
      · DB 模板新增的占位符能被校验出来（漏检修复）。
"""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import patch

from app.db import get_db
from app.services.ai import workflows_outline_utils as outline_utils
from app.services.ai import workflows_outline_v51 as v51
from app.services.ai.prompts import _ALL_PROMPTS, extract_variables, get_prompt
from app.services.ai.prompts._cache import reload_prompt_cache


def _set_prompt(key: str, content: str) -> None:
    """直接把某 key 的内容写入 prompt_templates 并刷新缓存（模拟管理页保存）。"""
    meta = _ALL_PROMPTS.get(key, {})
    with get_db() as conn:
        conn.execute(
            """INSERT INTO prompt_templates
               (key, category, label, content, default_content, updated_at)
               VALUES (?, ?, ?, ?, ?, datetime('now', 'localtime'))
               ON CONFLICT(key) DO UPDATE SET content=excluded.content""",
            (key, meta.get("category", ""), meta.get("label", ""), content,
             meta.get("content", "")),
        )
    reload_prompt_cache()


def _restore_prompt(key: str) -> None:
    """恢复到注册表默认内容，避免污染其它用例。"""
    _set_prompt(key, _ALL_PROMPTS[key]["content"])


# ──────────────────────────────────────────────────────────────────────
# B-1：短标书直出必须走 get_prompt（DB 覆盖生效 + 统一渲染）
# ──────────────────────────────────────────────────────────────────────
def test_direct_outline_honors_db_override_and_renders():
    cap = {}

    async def _fake_collect(provider, messages, **kwargs):
        cap["messages"] = messages
        return []  # 空列表 → generate_outline_direct 走回退分支返回 []

    try:
        _set_prompt("OUTLINE_DIRECT_SYSTEM", "自定义系统提示 DB_MARKER_SYS")
        # 用 {var} 小写形式，验证 render_prompt 大小写不敏感 + B-1 走统一渲染
        _set_prompt(
            "OUTLINE_DIRECT_USER",
            "DB_MARKER_USER 概述={summary} 范围={project_scope} 行业={industry_profile}",
        )
        with patch.object(v51, "collect_json_response", _fake_collect):
            asyncio.run(
                v51.generate_outline_direct(
                    None, "某某纪念馆项目", "评分点A", "修缮改造", "外立面整修"
                )
            )
        sys_msg, user_msg = cap["messages"][0], cap["messages"][1]
        assert sys_msg.content == "自定义系统提示 DB_MARKER_SYS"
        assert "DB_MARKER_USER" in user_msg.content
        # 变量已注入实际值
        assert "某某纪念馆项目" in user_msg.content
        assert "外立面整修" in user_msg.content
        assert "修缮改造" in user_msg.content
        # 不应残留未替换的占位符
        assert not extract_variables(user_msg.content)
    finally:
        _restore_prompt("OUTLINE_DIRECT_SYSTEM")
        _restore_prompt("OUTLINE_DIRECT_USER")


# ──────────────────────────────────────────────────────────────────────
# B-2：L3 标题修复走 render_prompt 注入（DB 覆盖生效）
# ──────────────────────────────────────────────────────────────────────
def test_l3_repair_template_db_override_renders():
    try:
        _set_prompt(
            "L3_TITLE_REPAIR_PROMPT",
            "L3_MARKER 一级={l1_title} 二级={l2_title} 三级={l3_title} 上下文={project_context}",
        )
        template = get_prompt("L3_TITLE_REPAIR_PROMPT")
        rendered = outline_utils.render_prompt(
            template,
            L1_TITLE="项目概述",
            L2_TITLE="质量保证体系",
            L3_TITLE="质量控制措施",
            PROJECT_CONTEXT="真实项目上下文",
        )
        assert "L3_MARKER" in rendered
        assert "质量保证体系" in rendered
        assert "真实项目上下文" in rendered
        assert not extract_variables(rendered)
    finally:
        _restore_prompt("L3_TITLE_REPAIR_PROMPT")


# ──────────────────────────────────────────────────────────────────────
# D-3：变量校验按「实际生效模板内容」而非注册表默认
# ──────────────────────────────────────────────────────────────────────
def test_missing_var_warning_follows_actual_content(caplog):
    key = "CONTENT_ORCHESTRATION_USER"
    default_vars = set(extract_variables(_ALL_PROMPTS[key]["content"]))
    assert default_vars, "用例前提：默认模板应含变量"

    try:
        # 场景 1：DB 模板删光了所有默认占位符 → 旧实现会按注册表误报缺失，新实现不应告警
        _set_prompt(key, "这是一个不含任何占位符的自定义模板。")
        with caplog.at_level(logging.WARNING):
            get_prompt(key, **{v.lower(): "x" for v in default_vars})
        warnings = [r for r in caplog.records if "missing variables" in r.getMessage()]
        assert not warnings, f"实际模板无占位符却误报缺失: {[r.getMessage() for r in warnings]}"

        # 场景 2：DB 模板新增一个默认模板没有的占位符，且调用方未传 → 应被校验出来
        _set_prompt(key, "含新占位符 __BRAND_NEW_VAR__ 的模板 {OUTLINE}")
        with caplog.at_level(logging.WARNING):
            get_prompt(key, OUTLINE="目录")
        msgs = [r.getMessage() for r in caplog.records if "missing variables" in r.getMessage()]
        assert msgs and "BRAND_NEW_VAR" in msgs[-1], msgs
    finally:
        _restore_prompt(key)


# ──────────────────────────────────────────────────────────────────────
# 兜底：无 kwargs 抓取原始模板时不产生缺失变量告警（编排/审核调用方）
# ──────────────────────────────────────────────────────────────────────
def test_fetch_raw_template_no_warning(caplog):
    key = "L3_TITLE_REPAIR_PROMPT"  # 含 4 个变量，若不传 kwargs 旧逻辑会告警
    with caplog.at_level(logging.WARNING):
        raw = get_prompt(key)
    assert raw.strip() == _ALL_PROMPTS[key]["content"].strip()
    assert "__L1_TITLE__" in raw  # 未渲染的原始模板（DB 默认内容）
    assert not [r for r in caplog.records if "missing variables" in r.getMessage()]
