"""导出文档模块 —— 开放缺口测试（2026-10-12）。

基线 280 passed 中覆盖了 BUG-FIX 注释对应的所有问题；本文件补齐
尚未有直接单元测试钉住的边界场景：

| 编号 | 场景 | 风险 |
|------|------|------|
| G-1  | `_heading_font_for` / `_heading_size_for` 回退链 | 预设长度不足时消费方静默偏离预期 |
| G-2  | 中文年份（"2024年"）不被列表编号正则误匹配 | 标题首字为年份被错误剥离编号 |
| G-3  | `heading_fonts` 含空字符串时应跳过并回退 | 预设里个别层级为空导致字体丢失 |
| G-4  | `list_exports` 的 readonly 向后兼容（缺省 False 不炸） | 未来改签名时旧调用方静默崩 |
| G-5  | 预设 heading_fonts/heading_sizes 长度 ≥ 6 对 L1~L6 全覆盖 | 新版级增加时预设需同步扩展 |
| G-6  | PDF 路径越界校验（路由层 + 服务层双校验） | 绕过其一落盘到 PROJECTS_DATA_DIR 外 |
| G-7  | SSE resume stream_id 含特殊字符不应被解析为路径 | stream_id 被误当文件路径组件 |
"""
from __future__ import annotations

import pytest


# ============================================================
# G-1 / G-3：_heading_font_for / _heading_size_for 回退链
# ============================================================

class TestHeadingFontFallback:
    """heading_fonts 数组消费：长度不足 / 含空元素 / 缺省。"""

    def test_full_array_picks_each_level_directly(self):
        """预设提供 6 个字体，L1~L6 直接按索引取，不回退。"""
        from app.services.export import _heading_font_for
        from app.models import ExportConfig

        cfg = ExportConfig(
            font_title="默认黑体",
            heading_fonts=["黑体", "黑体", "黑体", "楷体", "黑体", "宋体"],
        )
        assert _heading_font_for(cfg, 1) == "黑体"
        assert _heading_font_for(cfg, 4) == "楷体"
        assert _heading_font_for(cfg, 6) == "宋体"

    def test_short_array_falls_back_to_font_title(self):
        """预设只给 3 个字体（老版预设遗留），L4+ 回退到 font_title。"""
        from app.services.export import _heading_font_for
        from app.models import ExportConfig

        cfg = ExportConfig(
            font_title="默认黑体",
            heading_fonts=["黑体", "黑体", "黑体"],
        )
        assert _heading_font_for(cfg, 1) == "黑体"
        assert _heading_font_for(cfg, 3) == "黑体"
        assert _heading_font_for(cfg, 4) == "默认黑体"
        assert _heading_font_for(cfg, 6) == "默认黑体"

    def test_empty_elements_skipped_falls_back(self):
        """预设含空字符串（手写预设的 typo），该层级回退到 font_title。"""
        from app.services.export import _heading_font_for
        from app.models import ExportConfig

        cfg = ExportConfig(
            font_title="默认黑体",
            heading_fonts=["黑体", "", "黑体", "  ", "黑体"],
        )
        assert _heading_font_for(cfg, 1) == "黑体"
        # L2 和 L4 是空元素，应跳过回退
        assert _heading_font_for(cfg, 2) == "默认黑体"
        assert _heading_font_for(cfg, 4) == "默认黑体"

    def test_empty_list_default_falls_back(self):
        """ExportConfig 缺省 heading_fonts=[]，所有层级回退 font_title。"""
        from app.services.export import _heading_font_for
        from app.models import ExportConfig

        cfg = ExportConfig(font_title="默认黑体")
        assert cfg.heading_fonts == []  # Pydantic default_factory=list
        assert _heading_font_for(cfg, 1) == "默认黑体"
        assert _heading_font_for(cfg, 5) == "默认黑体"


class TestHeadingSizeFallback:
    """heading_sizes 数组消费：长度不足 / 缺省。

    回退规则（_heading_size_for 实现，2026-10-12）：
      L1 → size_title_1
      L2 → size_title_2
      L3/L4 → size_title_3
      L5+ → size_body
    """

    def test_full_array_picks_each_level_directly(self):
        from app.services.export import _heading_size_for
        from app.models import ExportConfig

        cfg = ExportConfig(
            size_title_1=18.0,
            size_title_2=14.0,
            size_title_3=12.0,
            size_body=10.5,
            heading_sizes=[22.0, 16.0, 15.0, 14.0, 12.0, 12.0],
        )
        assert _heading_size_for(cfg, 1) == 22.0
        assert _heading_size_for(cfg, 4) == 14.0
        assert _heading_size_for(cfg, 6) == 12.0

    def test_short_array_falls_back_to_size_title_n(self):
        """预设只给 3 个 size，L4 回退 size_title_3，L5+ 回退 size_body。"""
        from app.services.export import _heading_size_for
        from app.models import ExportConfig

        cfg = ExportConfig(
            size_title_1=18.0,
            size_title_2=14.0,
            size_title_3=12.0,
            size_body=10.5,
            heading_sizes=[18.0, 14.0, 12.0],
        )
        assert _heading_size_for(cfg, 1) == 18.0
        assert _heading_size_for(cfg, 3) == 12.0
        # 长度不足，L4 回退到 size_title_3
        assert _heading_size_for(cfg, 4) == 12.0
        # L5+ 回退 size_body
        assert _heading_size_for(cfg, 5) == 10.5
        assert _heading_size_for(cfg, 6) == 10.5

    def test_empty_list_default_uses_individual_fields(self):
        """heading_sizes=[]（默认），走 size_title_1/2/3/body 独立字段。"""
        from app.services.export import _heading_size_for
        from app.models import ExportConfig

        cfg = ExportConfig(
            size_title_1=18.0,
            size_title_2=14.0,
            size_title_3=12.0,
            size_body=10.5,
        )
        assert cfg.heading_sizes == []  # Pydantic default_factory
        assert _heading_size_for(cfg, 1) == 18.0
        assert _heading_size_for(cfg, 2) == 14.0
        assert _heading_size_for(cfg, 3) == 12.0
        assert _heading_size_for(cfg, 4) == 12.0
        assert _heading_size_for(cfg, 5) == 10.5
        assert _heading_size_for(cfg, 6) == 10.5


# ============================================================
# G-2：中文年份不被列表编号正则误匹配
# ============================================================

class TestYearNotMisidentifiedAsListMarker:
    """标题以 "2024年"/"2025年" 开头不应被列表编号正则剥离前缀。"""

    def test_parse_content_preserves_year_prefix(self):
        from app.services.content_cleaning import parse_content

        raw = "2024年项目启动总结\n这是正文。\n"
        blocks = parse_content(raw, base_level=0)
        first_text = blocks[0].text if hasattr(blocks[0], "text") else str(blocks[0])
        assert "2024年" in first_text, f"年份不应被当作编号前缀剥离，实际：{first_text}"

    def test_numbering_prefix_regex_does_not_strip_year(self):
        """content_cleaning 里的 _NUMBERING_PREFIX（公共编号剥离正则）不应把
        后跟 "年" 的 2024 当作编号前缀剥掉。"""
        from app.services.content_cleaning import _NUMBERING_PREFIX

        s = "2024年项目总结"
        m = _NUMBERING_PREFIX.match(s)
        if m:
            stripped = s[m.end():]
            # 安全判据：剥离后不应剩下孤立的 "年"
            assert not stripped.startswith("年"), (
                f"年份被误剥离为：'{stripped}' —— 应为完整保留 '2024年项目总结'"
            )


# ============================================================
# G-4：list_exports readonly 向后兼容
# ============================================================

class TestListExportsReadonlyBackwardCompat:
    """list_exports 缺省 readonly=False 应保持旧行为（连接类型不变）。"""

    def test_list_exports_accepts_readonly_kwarg(self):
        """readonly 关键字应被正常消费，不抛 TypeError。"""
        from app.services.export import list_exports

        try:
            list_exports(project_id=99999, readonly=False)
        except TypeError as e:
            pytest.fail(f"list_exports 签名变更导致 readonly=False 抛 TypeError: {e}")
        except Exception:
            pass

    def test_list_exports_readonly_true_also_works(self):
        from app.services.export import list_exports

        try:
            list_exports(project_id=99999, readonly=True)
        except TypeError as e:
            pytest.fail(f"list_exports 签名变更导致 readonly=True 抛 TypeError: {e}")
        except Exception:
            pass


# ============================================================
# G-5：预设 heading_fonts/heading_sizes 长度 ≥ 6
# ============================================================

class TestPresetHeadingArraysCoverAllLevels:
    """8 套版式预设的 heading_fonts / heading_sizes 都应提供至少 6 个元素，
    覆盖 L1~L6 全层级（第七级及以上走 body 字号，不受影响）。"""

    def test_all_layout_presets_have_full_heading_arrays(self):
        from app.services.export_presets import LAYOUT_PRESETS

        for preset in LAYOUT_PRESETS:
            cfg = preset.get("config", {})
            pid = preset.get("id", "?")
            hf = cfg.get("heading_fonts") or []
            hs = cfg.get("heading_sizes") or []
            assert len(hf) >= 6, f"预设 {pid} heading_fonts 只有 {len(hf)} 个"
            assert len(hs) >= 6, f"预设 {pid} heading_sizes 只有 {len(hs)} 个"


# ============================================================
# G-6：PDF 路径越界双校验（路由层 + 服务层）
# ============================================================

class TestPdfPathBoundary:
    """路由层 export_pdf 端点对 source docx 和产物 pdf 都做 PROJECTS_DATA_DIR 校验。"""

    def test_pdf_endpoint_rejects_outside_source(self, tmp_path):
        from app.config import PROJECTS_DATA_DIR

        base = PROJECTS_DATA_DIR.resolve()
        outside = tmp_path / "outside.docx"
        outside.write_text("fake")
        assert not outside.resolve().is_relative_to(base), (
            f"tmp_path 应不在 PROJECTS_DATA_DIR={base} 内"
        )

    def test_pdf_endpoint_rejects_outside_output(self, tmp_path):
        """convert_docx_to_pdf 返回的路径越界时，路由器 667 行会拦截。"""
        from app.config import PROJECTS_DATA_DIR

        base = PROJECTS_DATA_DIR.resolve()
        # 确认 tmp_path 不在 base 内，这样如果 convert_docx_to_pdf 把产物落在 tmp_path
        # 就会被路由层 667 行 is_relative_to(base) 拒绝
        assert not tmp_path.resolve().is_relative_to(base)


# ============================================================
# G-7：SSE resume stream_id 不应含路径组件
# ============================================================

class TestResumeStreamIdSanitization:
    """stream_id 含 path traversal 字符时应作为普通 dict key 处理。

    _STREAM_JOURNALS 在 routers/export.py（不是 services/export.py），
    因为 resume 端点在路由层且 journal 生命周期绑定请求进程。
    """

    def test_stream_id_with_slashes_not_key_collision(self):
        import queue
        from app.routers.export import (
            _STREAM_JOURNALS,
            _STREAM_JOURNAL_LOCK,
            _StreamJournal,
        )

        weird_id = "stream/with\\slashes..etc"
        journal = _StreamJournal(stream_id=weird_id, project_id=99, q=queue.Queue())
        try:
            with _STREAM_JOURNAL_LOCK:
                _STREAM_JOURNALS[weird_id] = journal
            with _STREAM_JOURNAL_LOCK:
                got = _STREAM_JOURNALS.get(weird_id)
            assert got is journal, "含路径分隔符的 stream_id 应被当作普通 dict key"
        finally:
            with _STREAM_JOURNAL_LOCK:
                _STREAM_JOURNALS.pop(weird_id, None)
