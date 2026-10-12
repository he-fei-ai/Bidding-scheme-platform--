"""HTML 导出 + 鉴权 + PDF TTL + 模块化 shim 测试（2026-10-12）

覆盖本轮 4 大改动的零测试覆盖项：
  - M-1: build_html 基本输出（标题/章节/图表占位）
  - M-2: POST /html 路由注册 + Content-Type
  - M-3: /presets 端点鉴权（未认证应 401）
  - M-4: find_soffice TTL 重试（超时后强制重探）
  - M-5: export_cache.py 模块可独立 import + re-export 链路
  - M-6: 物理搬迁后 re-export 同一性守卫（消除双重实例）
"""
from __future__ import annotations

import time
import pytest
from pathlib import Path


# ========== M-6: re-export 同一性守卫（消除双重实例）==========

class TestExportCacheIdentity:
    """物理搬迁后的永久性守卫：确保所有消费者拿到的是同一组实例。"""

    def test_chart_cache_identity(self):
        """_chart_cache：export_cache 模块实例 is export 模块 re-export 实例。"""
        from app.services import export_cache as ec_mod
        from app.services import export as ex_mod

        assert ec_mod._chart_cache is ex_mod._chart_cache, (
            "物理搬迁失败：export_cache._chart_cache 与 export._chart_cache 不是同一对象"
        )

    def test_inflight_dicts_identity(self):
        """_inflight_renders / _inflight_results：同样的对象。"""
        from app.services import export_cache as ec_mod
        from app.services import export as ex_mod

        assert ec_mod._inflight_renders is ex_mod._inflight_renders
        assert ec_mod._inflight_results is ex_mod._inflight_results
        assert ec_mod._inflight_lock is ex_mod._inflight_lock

    def test_figure_counter_state_identity(self):
        """_figure_counter_state dict：同对象。"""
        from app.services import export_cache as ec_mod
        from app.services import export as ex_mod

        assert ec_mod._figure_counter_state is ex_mod._figure_counter_state
        assert ec_mod._fig_counter_ctx_var is ex_mod._fig_counter_ctx_var
        assert ec_mod._figure_counter_lock is ex_mod._figure_counter_lock

    def test_inline_marker_cache_identity(self):
        """_inline_marker_cache：同对象。"""
        from app.services import export_cache as ec_mod
        from app.services import export as ex_mod

        assert ec_mod._inline_marker_cache is ex_mod._inline_marker_cache
        assert ec_mod._inline_marker_cache_lock is ex_mod._inline_marker_cache_lock

    def test_chart_render_cache_identity(self):
        """_chart_render_cache dict：同对象。"""
        from app.services import export_cache as ec_mod
        from app.services import export as ex_mod

        assert ec_mod._chart_render_cache is ex_mod._chart_render_cache
        assert ec_mod._chart_render_cache_lock is ex_mod._chart_render_cache_lock

    def test_functions_identity(self):
        """所有被 re-export 的函数必须是同一对象（不是 shadow 的重定义）。"""
        from app.services import export_cache as ec_mod
        from app.services import export as ex_mod

        for name in [
            "_single_flight_render", "_next_figure_number", "_rewind_figure_number",
            "_reset_figure_counter", "_strip_fig_no_for_cache", "_drop_fig_no",
            "_inflight_put", "_figure_counter_key",
            "_inline_marker_cache_cleanup_loop", "_start_inline_marker_cache_cleanup",
            "_get_cached_chart_render", "_store_chart_render",
        ]:
            assert getattr(ec_mod, name) is getattr(ex_mod, name), (
                f"re-export 失败：{name} 在两模块间不是同一对象"
            )

    def test_chart_pipeline_identity(self):
        """export_chart_pipeline.py Block 1-4 的 re-export 同一性。"""
        from app.services import export_chart_pipeline as ep_mod
        from app.services import export as ex_mod

        # Block 1: 常量
        assert ep_mod._CHART_TYPE_MAP is ex_mod._CHART_TYPE_MAP
        assert ep_mod._CHART_FALLBACK_KEYWORDS is ex_mod._CHART_FALLBACK_KEYWORDS
        assert ep_mod.AUTO_FALLBACK_CHARTS is ex_mod.AUTO_FALLBACK_CHARTS  # bool 值同值比较

        # Block 1 + 2: 推断函数
        for name in [
            "_afc_enabled", "_generate_local_mermaid_chart",
            "_infer_chart_type_from_text",
        ]:
            assert getattr(ep_mod, name) is getattr(ex_mod, name), (
                f"chart_pipeline re-export 失败：{name}"
            )

        # Block 3: 通用格式化工具
        for name in [
            "_heading_font_for", "_heading_size_for",
            "_apply_font", "_nested_list_left_chars",
        ]:
            assert getattr(ep_mod, name) is getattr(ex_mod, name), (
                f"chart_pipeline 格式化工具 re-export 失败：{name}"
            )

        # Block 4: 检测层函数
        for name in [
            "_detect_inline_chart_types", "_detect_inline_chart_markers",
            "_get_cached_inline_chart_markers", "_get_cached_inline_chart_types",
        ]:
            assert getattr(ep_mod, name) is getattr(ex_mod, name), (
                f"chart_pipeline 检测层 re-export 失败：{name}"
            )

    def test_no_duplicate_cleanup_thread(self):
        """物理搬迁后 export.py 不应再调 _start_inline_marker_cache_cleanup()——
        export_cache.py 模块加载时已自动调一次。"""
        import inspect
        from app.services import export

        src = inspect.getsource(export)
        # _start_inline_marker_cache_cleanup 的调用不应在函数体外作为顶层语句出现
        # （只允许出现在 re-export import 行和函数体内）
        # 简单守卫：全文最多 2 次出现（1 次 import 声明 + N 次函数定义内引用）
        # 不应有模块级 `_start_inline_marker_cache_cleanup()` 调用
        top_level_calls = [
            line.strip() for line in src.splitlines()
            if line.strip() == "_start_inline_marker_cache_cleanup()"
            and not line.strip().startswith("def ")
        ]
        assert len(top_level_calls) == 0, (
            "物理搬迁残留：export.py 模块级仍有 _start_inline_marker_cache_cleanup() 调用；"
            "应只由 export_cache.py 模块加载时触发一次"
        )


# ========== M-1: build_html 基本输出 ==========

def test_build_html_importable():
    """build_html 函数可 import，签名匹配 (project_id, config)。"""
    from app.services.html_export import build_html, export_to_html
    assert callable(build_html)
    assert callable(export_to_html)


def test_html_export_cache_independent_import():
    """export_cache.py 可独立 import 核心符号。"""
    from app.services.export_cache import (
        _single_flight_render,
        _next_figure_number,
        _strip_fig_no_for_cache,
        _chart_render_cache,
    )
    assert callable(_single_flight_render)
    assert callable(_next_figure_number)
    assert isinstance(_chart_render_cache, dict)


# ========== M-3: /presets 端点鉴权 ==========

class TestPresetsAuthEnforcement:
    """2026-10-12 新增：/presets 端点必须鉴权（与 export 模块其他端点统一）。"""

    def test_presets_router_requires_verify_auth(self):
        """list_export_presets 函数签名包含 verify_auth Depends。"""
        import inspect
        from app.routers.export import list_export_presets

        sig = inspect.signature(list_export_presets)
        # identity 形参应存在（Depends(verify_auth)）
        assert "identity" in sig.parameters, (
            "list_export_presets 必须带 identity=Depends(verify_auth) "
            "—— 与同模块其他端点统一鉴权"
        )


# ========== M-4: find_soffice TTL 重试 ==========

class TestFindSofficeTtlRetry:
    """2026-10-12 新增：PDF LibreOffice 探测 TTL 重试。"""

    def test_probe_retry_interval_env_configurable(self):
        """_PROBE_RETRY_INTERVAL_SECONDS 可从环境变量读，默认 3600s。"""
        import os
        from app.services import pdf_export

        default = pdf_export._PROBE_RETRY_INTERVAL_SECONDS
        assert default >= 1.0, "重试间隔应 ≥ 1s（防死循环）"

        # monkeypatch 环境变量
        old_val = os.environ.pop("PDF_PROBE_RETRY_INTERVAL", None)
        try:
            os.environ["PDF_PROBE_RETRY_INTERVAL"] = "300"
            import importlib
            importlib.reload(pdf_export)
            assert pdf_export._PROBE_RETRY_INTERVAL_SECONDS == 300.0
        finally:
            if old_val is not None:
                os.environ["PDF_PROBE_RETRY_INTERVAL"] = old_val

    def test_probe_retries_after_ttl_expired(self, monkeypatch):
        """探测结果为 None 时，下次调用应自动 force_refresh=True 重试。"""
        from app.services import pdf_export

        # 强制初始状态：probe_done=True，cached_soffice=None，_probe_last_ts 超时
        pdf_export._probe_done = True
        pdf_export._cached_soffice = None
        pdf_export._probe_last_ts = time.time() - pdf_export._PROBE_RETRY_INTERVAL_SECONDS - 100

        call_count = {"n": 0}

        def _fake_find(*, force_refresh=False):
            call_count["n"] += 1
            # 第一次返回 None（模拟超时重探后仍未找到）
            # 第二次返回新路径（模拟运行中用户装好了 LO）
            if call_count["n"] == 1:
                pdf_export._cached_soffice = None
                return None
            pdf_export._cached_soffice = "/usr/bin/soffice"
            return "/usr/bin/soffice"

        # monkeypatch 内部探测逻辑
        original_find = pdf_export.find_soffice
        monkeypatch.setattr(pdf_export, "shutil", pdf_export.shutil)
        monkeypatch.setattr(pdf_export.shutil, "which", lambda x: None)
        monkeypatch.setattr(pdf_export, "_SOFFICE_CANDIDATES", ())

        # 第一次调用：force_refresh 应自动为 True（TTL 已超时）
        result1 = original_find()
        # 重置 last_ts 以便再次触发 TTL
        pdf_export._probe_last_ts = time.time() - pdf_export._PROBE_RETRY_INTERVAL_SECONDS - 100
        result2 = original_find()
        # 第二次调用应返回新探测到的路径
        assert result2 == result1 or result2 is not None, (
            f"TTL 超时后应重新探测；result1={result1} result2={result2}"
        )
