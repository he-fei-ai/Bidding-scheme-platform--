"""提示词模块第 13 轮加固回归测试（2026-11-04，BUG-FIX-PROMPT-M1 ~ M8）。

覆盖本轮修复：
- M1  get_prompt / _check_hardcode_drift 锁外裸读 _prompt_cache 与 reload 竞态
- M2  DB 空内容行进入缓存导致硬编码兜底失效（get_prompt 返回 ""）
- M3  启动「预热」实为空操作（reload_prompt_cache 不加载）→ warm_prompt_cache
- M4  start_auto_snapshot 从未接线 → /prompts/metrics/history 恒空
- M5  reset_prompt 首次 INSERT 缺 category/label（R2/R3 同类遗漏）
- M6  {VAR } 畸形占位符三处漏检（校验/残留/渲染路径）
- M7  OUTLINE_REVIEW 占位符白名单无法覆盖 DB 定制新增变量
- M8  模板文件 docstring 计数漂移守卫（content 5→7 / checks 8→10）

测试约定与既有提示词测试一致：conftest 提供独立临时数据目录（init_db 已
seed 全部注册 key），模块级可变状态（_prompt_cache）在用例内保存/恢复。
"""

from __future__ import annotations

import ast
import re as _re
import threading
import time
from pathlib import Path

import pytest

from app.services.ai.prompts import (
    _ALL_PROMPTS,
    clean_prompt_text,
    get_prompt,
    has_residual_placeholders,
    validate_prompt_content,
)
from app.services.ai.prompts import _cache, _registry


# ──────────────────────────────────────────────────────────────────────
# M1：get_prompt 与 reload_prompt_cache 的竞态
# ──────────────────────────────────────────────────────────────────────


class TestM1ReloadRace:
    def test_get_prompt_never_typeerror_during_reload(self):
        """读线程高频 get_prompt × 写线程置 None：不得抛 TypeError（历史实测复现）。"""
        _registry._reg("M1_RACE_KEY", "测试", "M1 竞态回归探针", "硬编码内容X{V}")
        old_cache = _cache._prompt_cache
        _cache._prompt_cache = {"M1_RACE_KEY": "DB内容X"}
        errors: list[str] = []

        def reader():
            for _ in range(5000):
                try:
                    get_prompt("M1_RACE_KEY", V="y")
                except Exception as e:  # 历史表现为 TypeError: in NoneType
                    errors.append(repr(e))
                    return

        def reloader():
            for _ in range(150):
                # 直接置 None 模拟 reload_prompt_cache 持锁写与本线程交错的瞬间
                _cache._prompt_cache = None
                time.sleep(0.0005)

        try:
            t1 = threading.Thread(target=reader)
            t2 = threading.Thread(target=reloader)
            t1.start()
            t2.start()
            t1.join()
            t2.join()
        finally:
            _cache._prompt_cache = old_cache
        assert not errors, f"竞态下 get_prompt 抛异常: {errors[:2]}"

    def test_check_hardcode_drift_uses_snapshot(self):
        """_check_hardcode_drift 遍历体必须使用局部快照 `cache`，不再裸读全局。

        注意：inspect.getsource 对该函数会把其后的模块级注释块一并扫入
        （注释含 `_prompt_cache` 字样），故按行精确断言遍历逻辑，不做全文否定。
        """
        import inspect

        src = inspect.getsource(_cache._check_hardcode_drift)
        assert "cache = _prompt_cache" in src, "应存在入口快照"
        body_after = src.split("cache = _prompt_cache", 1)[1]
        loop_guard = [l for l in body_after.splitlines() if "if key not in" in l]
        assert loop_guard, "未找到漂移遍历的 DB 存在性判断"
        assert all("_prompt_cache" not in l for l in loop_guard), (
            "漂移遍历仍裸读全局 _prompt_cache（M1 回归）"
        )
        db_content_lines = [l for l in body_after.splitlines() if "db_content =" in l]
        assert db_content_lines and "cache[" in db_content_lines[0], (
            "漂移遍历读取 DB 内容应走局部快照 cache[key]"
        )


# ──────────────────────────────────────────────────────────────────────
# M2：DB 空内容行回退硬编码
# ──────────────────────────────────────────────────────────────────────


class TestM2EmptyContentFallback:
    def test_empty_db_content_falls_back_to_hardcode(self, monkeypatch):
        """DB 行 content 为空串时 get_prompt 应回退注册表硬编码，而非返回 ""。"""
        _registry._reg("M2_EMPTY_DB_KEY", "测试", "M2 空内容探针", "硬编码回退内容甲")
        monkeypatch.setattr(_cache, "_prompt_cache", {"M2_EMPTY_DB_KEY": ""})
        assert get_prompt("M2_EMPTY_DB_KEY") == "硬编码回退内容甲"

    def test_whitespace_db_content_falls_back_to_hardcode(self, monkeypatch):
        """纯空白内容（clean_prompt_text 后为空）同样回退。"""
        _registry._reg("M2_WS_DB_KEY", "测试", "M2 空白探针", "硬编码回退内容乙")
        monkeypatch.setattr(_cache, "_prompt_cache", {"M2_WS_DB_KEY": "  \n\t "})
        assert get_prompt("M2_WS_DB_KEY") == "硬编码回退内容乙"

    def test_nonempty_db_content_still_wins(self, monkeypatch):
        """非空 DB 内容仍优先于硬编码（M2 不得改变正常优先级）。"""
        _registry._reg("M2_OK_DB_KEY", "测试", "M2 正常探针", "硬编码内容丙")
        monkeypatch.setattr(_cache, "_prompt_cache", {"M2_OK_DB_KEY": "DB定制内容"})
        assert get_prompt("M2_OK_DB_KEY") == "DB定制内容"


# ──────────────────────────────────────────────────────────────────────
# M3：warm_prompt_cache 真预热
# ──────────────────────────────────────────────────────────────────────


class TestM3WarmPromptCache:
    def test_warm_loads_eagerly(self, monkeypatch):
        """warm_prompt_cache 后缓存应为已加载状态（此前 reload 语义为 None）。"""
        monkeypatch.setattr(_cache, "_prompt_cache", None)
        n = _cache.warm_prompt_cache()
        assert _cache._prompt_cache is not None, "预热后缓存仍为 None（M3 回归）"
        assert n > 0
        assert n == len(_cache._prompt_cache)

    def test_reload_still_only_invalidates(self):
        """reload_prompt_cache 保持「仅失效」语义不变（保存后延迟重载的既有契约）。"""
        old = _cache._prompt_cache
        try:
            _cache.reload_prompt_cache()
            assert _cache._prompt_cache is None
        finally:
            _cache._prompt_cache = old


# ──────────────────────────────────────────────────────────────────────
# M4：自动快照接线
# ──────────────────────────────────────────────────────────────────────


class TestM4AutoSnapshotWiring:
    def test_main_startup_shutdown_wired(self):
        """main.py 启动接线 start_auto_snapshot/warm_prompt_cache，关停接线 stop。"""
        import inspect

        from app import main

        src = inspect.getsource(main)
        assert "warm_prompt_cache" in src, "启动预热应改调 warm_prompt_cache（M3）"
        assert "start_auto_snapshot" in src, "启动应接线 start_auto_snapshot（M4）"
        assert "stop_auto_snapshot" in inspect.getsource(main._shutdown), (
            "_shutdown 应接线 stop_auto_snapshot（M4）"
        )

    def test_start_stop_idempotent(self):
        """start 幂等（不重复起线程），stop 后定时器清空。"""
        from app.services.ai.prompts import start_auto_snapshot, stop_auto_snapshot

        try:
            start_auto_snapshot(interval=9999)
            t1 = _cache._snapshot_timer
            assert t1 is not None
            start_auto_snapshot(interval=9999)
            assert _cache._snapshot_timer is t1, "重复 start 不应另起定时器"
        finally:
            stop_auto_snapshot()
        assert _cache._snapshot_timer is None


# ──────────────────────────────────────────────────────────────────────
# M5：reset_prompt 首次 INSERT 补 category/label
# ──────────────────────────────────────────────────────────────────────


class TestM5ResetPromptCategory:
    def test_source_carries_category_label(self):
        import inspect

        from app.routers import prompts as prompts_router

        src = inspect.getsource(prompts_router.reset_prompt)
        assert 'meta["category"]' in src and 'meta["label"]' in src, (
            "reset_prompt 首次 INSERT 分支应携带 meta 的 category/label（M5）"
        )

    def test_reset_after_row_deleted_restores_category(self):
        """端到端：删除 DB 行后 reset，落库行不得缺 category/label。

        与既有路由回归测试同口径：直接挂载 prompts.router 构造 TestClient
        （app.main 的完整应用在单测里不挂 /prompts 前缀之外的依赖差异）。
        """
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.db import get_db
        from app.routers import prompts as prompts_router

        key = "JSON_REPAIR_PROMPT"
        app = FastAPI()
        app.include_router(prompts_router.router)
        with TestClient(app) as client:
            with get_db() as conn:
                conn.execute("DELETE FROM prompt_templates WHERE key = ?", (key,))
            r = client.post(f"/prompts/{key}/reset")
            assert r.status_code == 200, r.text
        with get_db(readonly=True) as conn:
            row = conn.execute(
                "SELECT category, label, content, default_content "
                "FROM prompt_templates WHERE key = ?",
                (key,),
            ).fetchone()
        assert row is not None
        assert row["category"], "reset 首次 INSERT 后 category 不得为空（M5）"
        assert row["label"], "reset 首次 INSERT 后 label 不得为空（M5）"
        assert row["content"] == row["default_content"]


# ──────────────────────────────────────────────────────────────────────
# M6：{VAR } 畸形占位符检测
# ──────────────────────────────────────────────────────────────────────


class TestM6MalformedPlaceholder:
    def test_validate_detects_space_before_closing_brace(self):
        problems = validate_prompt_content("K", "xx {VAR } yy")
        assert any("空白" in p for p in problems), problems

    def test_residual_detection_covers_malformed(self):
        assert has_residual_placeholders("xx {VAR } yy") is True

    def test_normal_var_and_json_not_flagged(self):
        """正常变量、JSON 示例、中文标签 Mermaid 判定节点均不得误伤。

        注意：英文标签判定节点 ``A{cond}`` 中的 ``{cond}`` 会被既有残留模式
        （``{[A-Za-z_]\\w{1,}}``）视为未替换变量——这是 2026-10-06 起的既有口径
        （仅告警不阻断），非本轮引入；本项目 Mermaid 节点标签为中文（``{开始}``），
        不受影响。
        """
        assert validate_prompt_content("K", "填入 {VAR} 与 {\"a\": 1} 和 A{cond} --> B") == []
        assert has_residual_placeholders('{"a": 1} A{开始} --> B') is False

    def test_registry_self_validation_clean(self):
        """不变式：全部注册模板对自身内容校验零问题（含新畸形规则）。"""
        for key, meta in _ALL_PROMPTS.items():
            problems = validate_prompt_content(key, meta["content"])
            assert not problems, f"{key}: {problems}"


# ──────────────────────────────────────────────────────────────────────
# M7：目录审核占位符动态派生
# ──────────────────────────────────────────────────────────────────────


class TestM7ReviewPlaceholderDynamic:
    def test_dynamic_normalization_covers_db_added_vars(self):
        """DB 定制新增的 {WRITING_LENGTH} 应被规范化（白名单只认 4 个固定名）。"""
        from app.services.ai.workflows_quality import _normalize_review_placeholders_dynamic

        assert (
            _normalize_review_placeholders_dynamic("x {WRITING_LENGTH} y")
            == "x __WRITING_LENGTH__ y"
        )

    def test_dynamic_normalization_no_json_false_positive(self):
        from app.services.ai.workflows_quality import _normalize_review_placeholders_dynamic

        assert _normalize_review_placeholders_dynamic('{"a": 1}') == '{"a": 1}'

    def test_review_templates_vars_within_whitelist(self):
        """不变式：当前两个审核模板的 extract_variables ⊆ 固定白名单（动态派生
        与白名单行为现状一致，改造零行为变化的依据）。"""
        from app.services.ai.workflows_quality import _REVIEW_PLACEHOLDER_NAMES

        for key in ("OUTLINE_REVIEW_SYSTEM", "OUTLINE_REVIEW_USER"):
            vars_ = set(_registry.extract_variables(_ALL_PROMPTS[key]["content"]))
            assert vars_ <= set(_REVIEW_PLACEHOLDER_NAMES), f"{key}: {vars_ - set(_REVIEW_PLACEHOLDER_NAMES)}"

    def test_build_outline_review_texts_fills_all_known(self):
        """既有填充行为不回归：user 侧三变量注入，system 侧 INDUSTRY_PROFILE
        按设计走「标签替换」（_fill_template 语义），无字面量占位符残留。"""
        from app.services.ai.workflows_quality import _OUTLINE_REVIEW_LABELS, _build_outline_review_texts

        sys_text, user_text = _build_outline_review_texts("评分点A", "目录B", "概述C", "行业D")
        # OUTLINE_REVIEW_USER 模板占位符为 OUTLINE/SCORING_POINTS/SUMMARY 三个
        for frag in ("评分点A", "目录B", "概述C"):
            assert frag in user_text, frag
        # system 侧 INDUSTRY_PROFILE 以标签文字注入（首现/标签语义），不注入原始值
        assert _OUTLINE_REVIEW_LABELS["__INDUSTRY_PROFILE__"][:4] in sys_text
        assert "{SCORING_POINTS}" not in user_text
        assert "{OUTLINE}" not in user_text
        _residual = _re.compile(r"__[A-Z][A-Z0-9_]*__|\{[A-Za-z_]\w{1,}\}")
        assert not _residual.search(user_text), "user 文本残留占位符"
        assert not _residual.search(sys_text), "system 文本残留占位符"


# ──────────────────────────────────────────────────────────────────────
# M8：docstring 计数守卫
# ──────────────────────────────────────────────────────────────────────


class TestM8DocstringCounts:
    def test_docstring_count_matches_reg_calls(self):
        """每个模板文件的「（N 个提示词）」声明须与文件内 _reg 调用数一致。

        __init__.py 的头部清单（文件: 个数）不在本守卫范围（其「（14 个）」等
        字样属于清单行，非声明）；无计数的文件跳过。
        """
        base = Path(__file__).resolve().parent.parent / "app" / "services" / "ai" / "prompts"
        for f in sorted(base.glob("*.py")):
            if f.name == "__init__.py":
                continue
            src = f.read_text(encoding="utf-8")
            tree = ast.parse(src)
            n = sum(
                1
                for node in ast.walk(tree)
                if isinstance(node, ast.Call)
                and (
                    (isinstance(node.func, ast.Name) and node.func.id == "_reg")
                    or (isinstance(node.func, ast.Attribute) and node.func.attr == "_reg")
                )
            )
            m = _re.search(r"（(\d+)\s*个提示词", src)
            if m:
                assert str(n) == m.group(1), (
                    f"{f.name} docstring 声称 {m.group(1)} 个提示词，实际 _reg {n} 个（M8 回归）"
                )
