# -*- coding: utf-8 -*-
"""BUG-FIX 2026-10-17（analysis.updated_at 一致性）：analysis 表 DDL 声明
updated_at NOT NULL DEFAULT datetime('now')，但 routers/ai.py 的 7 处写入路径中
有 5 处（_sync_update_scope / _sync_update_field / _sync_insert_or_update /
_sync_confirm_field / _sync_reset_facts_audit）跳过了该列的同步维护，
仅 _sync_clear_analysis 和 _sync_confirm_and_retrieve 两处正确写入。

场景：用户多次覆写 summary_json / scoring_points_json / scope / boq_json /
confirmed_* 标记后，analysis.updated_at 仍停留在行创建时刻，与真实数据变更脱节。
一旦下游消费者（缓存失效、变更检测、E2E 审计）需要依赖该列，会得出错误结论。

本文件钉住全部 7 处写入路径必须同步更新 updated_at 的源码守卫。
"""

import inspect

import pytest


def _get_ai_module():
    from app.routers import ai as _ai

    return _ai


class TestAnalysisUpdatedAtConsistency:
    """analysis 表所有写入路径必须维护 updated_at。

    源码守卫：对 routers/ai.py 中负责 analysis 表写入的每个函数，
    检查其函数体内（不含注释/日志行）必须包含 updated_at 子串。
    """

    # 7 个负责 analysis 表写入的函数名（不含 _sync_get_analysis/_sync_read_* 等只读函数）
    _WRITER_FUNCTIONS = [
        "_sync_update_scope",
        "_sync_update_field",
        "_sync_reset_facts_audit",
        "_sync_clear_analysis",
        "_sync_insert_or_update",
        "_sync_confirm_field",
        "_sync_confirm_and_retrieve",
    ]

    def _writer_source(self, func_name):
        mod = _get_ai_module()
        fn = getattr(mod, func_name, None)
        if fn is None:
            pytest.skip(f"{func_name} 不存在于 routers/ai.py")
        src = inspect.getsource(fn)
        # 剔除注释行，防止注释里提 updated_at 造成假阳性
        body_lines = [
            ln for ln in src.splitlines()
            if not ln.lstrip().startswith("#")
        ]
        return "\n".join(body_lines)

    def test_each_writer_touches_updated_at(self):
        """7 个写入函数的可执行代码必须包含 updated_at。"""
        missing = []
        for fn_name in self._WRITER_FUNCTIONS:
            src = self._writer_source(fn_name)
            if "updated_at" not in src:
                missing.append(fn_name)
        assert not missing, (
            f"以下 analysis 写入函数未维护 updated_at：{missing}\n"
            f"这与 2026-10-17 修复 uploaded_files.updated_at 为同类 BUG。"
        )

    def test_no_writer_uses_scope_excluded_field(self):
        """写入函数不得触碰不存在的列（白名单外字段名会拼进 SQL 标识符）。"""
        mod = _get_ai_module()
        src_all = inspect.getsource(mod)
        # 剔除注释和 docstring 行——BUG-FIX 注释里会出现 confirmed_scope 作为
        # 历史反例说明，不应被守卫误命中
        code_only_lines = [
            ln for ln in src_all.splitlines()
            if not ln.lstrip().startswith("#")
            and not ln.lstrip().startswith('"""')
            and not ln.lstrip().startswith("'''")
        ]
        code_only = "\n".join(code_only_lines)
        # 列出白名单外、但以 _json 结尾、且可能被当作 analysis 列名的字符串
        # 只做源码级安全网：不应该出现 f-string 拼 scope 之类非白名单列
        forbidden_fstring_patterns = [
            "confirmed_scope",  # scope 没有确认标记，派生会产出不存在的列
            "confirmed_facts_policy",  # facts_policy 同理
        ]
        for pat in forbidden_fstring_patterns:
            assert pat not in code_only, (
                f"routers/ai.py 执行代码不应出现 {pat}（不存在的 analysis 列）"
            )

    def test_on_conflict_paths_also_update_updated_at(self):
        """_sync_update_field 和 _sync_insert_or_update 的 ON CONFLICT DO UPDATE 分支
        必须也写 updated_at，而不只是 INSERT 分支。

        这是最容易被忽略的路径——INSERT 时 updated_at 由 DEFAULT 或显式列保证，
        但 ON CONFLICT DO UPDATE SET 如果只列业务字段就会跳过。
        """
        mod = _get_ai_module()
        for fn_name in ("_sync_update_field", "_sync_insert_or_update"):
            fn = getattr(mod, fn_name)
            src = inspect.getsource(fn)
            # ON CONFLICT DO UPDATE SET 后面必须同时包含业务字段和 updated_at
            # 简单判定：updated_at 在源码中出现至少 2 次（INSERT VALUES + DO UPDATE SET）
            occurrences = src.count("updated_at")
            assert occurrences >= 2, (
                f"{fn_name} 的 ON CONFLICT DO UPDATE SET 分支可能遗漏 updated_at："
                f"源码中 updated_at 仅出现 {occurrences} 次"
            )
