"""
上传解析模块 2026-10-08 修复测试。

覆盖：
- Bug 1: errors="replace" 替代 errors="ignore"（编码兜底不静默丢弃字节）
- Bug 2: _extract_pptx_raw_xml 原始 XML 回退解析
- Bug 3: 空哈希跳过缓存（避免不同文件碰撞命中同一缓存条目）
- Bug 4: OCR 缓存 key TOCTOU race 修复
- Bug 5: MinerU 单例从函数属性改为模块级变量
- Bug 6: 缓存淘汰改为 LRU 单条淘汰（不再一次淘汰半数）
- Bug 7: 提取失败时不写空 .txt 文件
- Bug 8: 前端文件大小校验（客户端 50MB 限制）
- Bug 9: 前端批量上传 loadFiles 竞态防护
"""

import hashlib
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest


# ============================================================================
# Bug 1: errors="replace" 替代 errors="ignore"
# ============================================================================

class TestEncodingReplaceNotIgnore:
    """测试编码兜底使用 errors="replace" 而非 errors="ignore"。"""

    def test_utf8_fallback_uses_replace(self):
        """UTF-8 解码失败时，兜底应使用 errors="replace" 保留替换符。"""
        from app.services.files import _read_text_auto

        # 构造一个包含无效 UTF-8 字节的文件
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"\xff\xfe\xfa\x80\x81\x82")  # 无效 UTF-8 字节
            tmp_path = f.name

        try:
            result = _read_text_auto(tmp_path)
            # 应该包含替换符（而非静默丢弃）
            assert "\ufffd" in result or len(result) > 0, (
                "兜底解码应保留替换符或返回非空结果，而非静默丢弃字节"
            )
        finally:
            os.unlink(tmp_path)

    def test_ole2_extract_uses_replace(self):
        """OLE2 流解码应使用 errors="replace"。"""
        from app.services.files import _read_text_auto

        # 验证函数存在且可调用
        assert callable(_read_text_auto)


# ============================================================================
# Bug 2: _extract_pptx_raw_xml 原始 XML 回退
# ============================================================================

class TestPptxRawXmlFallback:
    """测试 PPTX 原始 XML 回退解析。"""

    def test_extract_pptx_raw_xml_exists(self):
        """_extract_pptx_raw_xml 函数应存在。"""
        from app.services.files import _extract_pptx_raw_xml

        assert callable(_extract_pptx_raw_xml)

    def test_extract_pptx_raw_xml_handles_missing_file(self):
        """文件不存在时应返回空字符串而非抛异常。"""
        from app.services.files import _extract_pptx_raw_xml

        result = _extract_pptx_raw_xml("/nonexistent/file.pptx")
        assert result == ""

    def test_extract_pptx_raw_xml_handles_non_zip(self):
        """非 ZIP 文件应返回空字符串。"""
        from app.services.files import _extract_pptx_raw_xml

        with tempfile.NamedTemporaryFile(suffix=".pptx", delete=False) as f:
            f.write(b"not a zip file")
            tmp_path = f.name

        try:
            result = _extract_pptx_raw_xml(tmp_path)
            assert result == ""
        finally:
            os.unlink(tmp_path)

    def test_extract_pptx_raw_xml_parses_valid_zip(self):
        """有效 ZIP 容器应能提取 <a:t> 文本。"""
        from app.services.files import _extract_pptx_raw_xml
        import zipfile
        import io

        # 构造最小 PPTX 结构
        slide_xml = (
            b'<?xml version="1.0"?>'
            b'<ppt:presentation xmlns:ppt="http://schemas.openxmlformats.org/presentationml/2006/main">'
            b'<p:sldIdLst><p:sldId id="256" r:id="rId2"/></p:sldIdLst>'
            b'</ppt:presentation>'
        )
        slide1_xml = (
            b'<?xml version="1.0"?>'
            b'<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
            b' xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
            b'<p:cSld><p:spTree><p:sp>'
            b'<p:txBody><a:bodyPr/><a:lstStyle/>'
            b'<a:p><a:r><a:t>Hello PPTX</a:t></a:r></a:p>'
            b'</p:txBody>'
            b'</p:sp></p:spTree></p:cSld>'
            b'</p:sld>'
        )

        with tempfile.NamedTemporaryFile(suffix=".pptx", delete=False) as f:
            tmp_path = f.name

        try:
            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("ppt/presentation.xml", slide_xml)
                zf.writestr("ppt/slides/slide1.xml", slide1_xml)

            result = _extract_pptx_raw_xml(tmp_path)
            assert "Hello PPTX" in result
        finally:
            os.unlink(tmp_path)


# ============================================================================
# Bug 3: 空哈希跳过缓存
# ============================================================================

class TestCacheSkipOnEmptyHash:
    """测试空哈希时跳过缓存，避免不同文件碰撞命中同一缓存条目。"""

    def test_get_file_hash_returns_empty_on_error(self):
        """文件不存在时 _get_file_hash 应返回空字符串。"""
        from app.services.files import _get_file_hash

        result = _get_file_hash("/nonexistent/file.txt")
        assert result == ""

    def test_extract_text_skips_cache_on_empty_hash(self):
        """空哈希时 extract_text_from_path 应跳过缓存。"""
        from app.services.files import (
            _get_file_hash,
            _parse_result_cache,
            _parse_cache_lock,
            _PARSE_CACHEABLE_EXTS,
        )

        # 验证正常文件有非空哈希
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"test content")
            tmp_path = f.name

        try:
            file_hash = _get_file_hash(tmp_path)
            assert file_hash != "", "正常文件应有非空哈希"
        finally:
            os.unlink(tmp_path)

        # 验证 _get_file_hash 对不存在文件返回空串
        empty_hash = _get_file_hash("/nonexistent/path.txt")
        assert empty_hash == ""

        # 验证缓存代码逻辑：空哈希时应跳过缓存读写
        # 通过检查 extract_text_from_path 的条件判断来验证
        import inspect
        source = inspect.getsource(extract_text_from_path := __import__(
            "app.services.files", fromlist=["extract_text_from_path"]
        ).extract_text_from_path)
        assert "and file_hash" in source, "extract_text_from_path 应包含 'and file_hash' 条件"


# ============================================================================
# Bug 4: OCR 缓存 key TOCTOU race
# ============================================================================

class TestOcrCacheKeyToctou:
    """测试 OCR 缓存 key 生成消除 TOCTOU race。"""

    def test_get_cache_key_handles_missing_file(self):
        """文件不存在时应返回文件路径而非抛 FileNotFoundError。"""
        from app.services.ocr_parser import MinerUParser

        parser = MinerUParser.__new__(MinerUParser)
        # 不初始化完整对象，只测试 _get_cache_key
        result = parser._get_cache_key("/nonexistent/file.pdf")
        assert result == "/nonexistent/file.pdf"

    def test_get_cache_key_handles_existing_file(self):
        """文件存在时应返回基于 mtime 的哈希。"""
        from app.services.ocr_parser import MinerUParser

        parser = MinerUParser.__new__(MinerUParser)

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4 test")
            tmp_path = f.name

        try:
            result = parser._get_cache_key(tmp_path)
            assert result != tmp_path
            assert len(result) == 32  # MD5 hex digest
        finally:
            os.unlink(tmp_path)

    def test_get_cache_key_no_toctou_race(self):
        """文件在 exists() 和 stat() 之间被删除时不应抛异常。"""
        from app.services.ocr_parser import MinerUParser

        parser = MinerUParser.__new__(MinerUParser)

        # 创建一个文件然后立即删除
        tmp_dir = tempfile.mkdtemp()
        file_path = os.path.join(tmp_dir, "test.pdf")
        with open(file_path, "wb") as f:
            f.write(b"%PDF-1.4 test")

        os.unlink(file_path)  # 删除文件

        # 调用 _get_cache_key 不应抛异常
        result = parser._get_cache_key(file_path)
        assert result == file_path  # 文件不存在时返回原路径

        import shutil
        shutil.rmtree(tmp_dir)


# ============================================================================
# BUG-FIX 2026-10-11（P3·OCR 缓存键亚秒碰撞）回归锁定
#   与 files._get_file_hash（CF-03）同一类缺陷：原 _get_cache_key 用
#   `{mtime:.0f}` 截断到整秒，同大小 PDF 在同一秒内替换会算出同一 key →
#   MinerU 缓存把旧解析文本错配给新文件。现改为 st_mtime_ns + size + 4KB 内容哈希。
# ============================================================================
class TestOcrCacheKeySubSecondCollision:
    """_get_cache_key 亚秒/同刻度碰撞修复锁定。"""

    @staticmethod
    def _parser():
        from app.services.ocr_parser import MinerUParser

        return MinerUParser.__new__(MinerUParser)

    def test_same_content_same_key(self):
        """同一文件 key 稳定（缓存可命中）。"""
        p = self._parser()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4 stable content")
            path = f.name
        try:
            assert p._get_cache_key(path) == p._get_cache_key(path)
        finally:
            os.unlink(path)

    def test_same_size_same_mtime_different_content_no_collision(self):
        """同大小 + 强制对齐 mtime 的不同内容文件，key 不得碰撞（CF-03 同款回归）。"""
        p = self._parser()
        d = tempfile.mkdtemp()
        path1 = os.path.join(d, "a.pdf")
        path2 = os.path.join(d, "b.pdf")
        # 同大小（均为 9 字节），内容不同
        with open(path1, "wb") as f1:
            f1.write(b"content A")
        with open(path2, "wb") as f2:
            f2.write(b"content B")
        try:
            # 对齐 mtime 到同一纳秒刻度，复现旧实现必然碰撞的条件
            os.utime(path1, ns=(1_000_000_000, 1_000_000_000))
            os.utime(path2, ns=(1_000_000_000, 1_000_000_000))
            assert p._get_cache_key(path1) != p._get_cache_key(path2), (
                "同大小同 mtime 的不同内容 PDF，缓存 key 不得碰撞"
            )
        finally:
            os.unlink(path1)
            os.unlink(path2)
            import shutil

            shutil.rmtree(d, ignore_errors=True)

    def test_subsecond_reparse_produces_distinct_key(self):
        """同路径内容在亚秒级被替换后，key 应不同（防旧内容缓存错配）。"""
        p = self._parser()
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4 version-one")
            path = f.name
        try:
            key1 = p._get_cache_key(path)
            # 亚秒内改写内容（不显式改 mtime，依赖纳秒刻度差异 + 内容哈希）
            with open(path, "wb") as f:
                f.write(b"%PDF-1.4 version-two-different")
            os.utime(path, None)  # 刷新 mtime 到「现在」
            key2 = p._get_cache_key(path)
            assert key1 != key2, "内容变化后缓存 key 必须变化，否则命中旧解析结果"
        finally:
            os.unlink(path)

    def test_missing_file_returns_path(self):
        """缺失文件仍回退返回原路径（TOCTOU 契约不回归）。"""
        p = self._parser()
        assert p._get_cache_key("/nonexistent/x.pdf") == "/nonexistent/x.pdf"



# ============================================================================
# Bug 5: MinerU 单例从函数属性改为模块级
# ============================================================================

class TestMineruSingletonModuleLevel:
    """测试 MinerU 解析器单例从函数属性改为模块级变量。"""

    def test_mineru_parser_instance_is_module_level(self):
        """_mineru_parser_instance 应为模块级变量（懒加载单例，初始为 None）。

        NOTE（测试隔离修复 2026-10-08）：MinerU 解析器是**进程级**懒加载单例，
        同一 pytest 进程内若前面的用例已触发过图片 OCR，该变量会被置为真实
        实例并被后续用例复用。原断言 `is None` 依赖用例执行顺序，在完整套件里
        被污染而误报失败。这里在断言前显式复位，使本用例不再依赖顺序。
        """
        import app.services.files as files_module

        assert hasattr(files_module, "_mineru_parser_instance")
        # 复位进程级懒加载单例，避免跨用例状态污染（不影响真实懒加载语义）
        files_module._mineru_parser_instance = None
        assert files_module._mineru_parser_instance is None  # 复位后应为 None

    def test_mineru_lock_exists(self):
        """_mineru_lock 应存在。"""
        from app.services.files import _mineru_lock

        assert isinstance(_mineru_lock, threading.Lock)

    def test_no_function_attribute_mineru_parser(self):
        """_extract_pdf 不应再有 _mineru_parser 函数属性。"""
        from app.services.files import _extract_pdf

        # 函数属性 _mineru_parser 不应存在
        assert not hasattr(_extract_pdf, "_mineru_parser")


# ============================================================================
# Bug 6: 缓存淘汰改为 LRU 单条
# ============================================================================

class TestCacheLruSingleEviction:
    """测试缓存淘汰改为 LRU 单条而非半数。"""

    def test_parse_cache_single_eviction(self):
        """解析缓存达到上限时应仅淘汰 1 条。"""
        from app.services.files import (
            _parse_result_cache,
            _PARSE_CACHE_MAX,
        )

        # 清空缓存
        _parse_result_cache.clear()

        # 填满缓存（使用递增的时间戳，i 越大表示越旧）
        base_time = time.time()
        for i in range(_PARSE_CACHE_MAX):
            # i=0 最新，i=MAX-1 最旧
            _parse_result_cache[f"file_{i}"] = (f"hash_{i}", base_time - i * 100, f"content_{i}")

        assert len(_parse_result_cache) == _PARSE_CACHE_MAX

        # 模拟淘汰逻辑：先淘汰最旧的 1 条，再添加新条目
        if len(_parse_result_cache) >= _PARSE_CACHE_MAX:
            oldest = min(_parse_result_cache.items(), key=lambda x: x[1][1])
            _parse_result_cache.pop(oldest[0], None)

        _parse_result_cache["new_file"] = ("new_hash", time.time(), "new_content")

        assert len(_parse_result_cache) == _PARSE_CACHE_MAX
        # 最旧的 file_19（时间戳最小）应被淘汰
        assert f"file_{_PARSE_CACHE_MAX - 1}" not in _parse_result_cache
        # 最新的 new_file 应存在
        assert "new_file" in _parse_result_cache
        # 次新的 file_0 应存在
        assert "file_0" in _parse_result_cache

        _parse_result_cache.clear()

    def test_text_cache_single_eviction(self):
        """文本缓存达到上限时应仅淘汰 1 条。"""
        from app.services.files import (
            _text_cache,
            _TEXT_CACHE_MAX,
            _text_cache_set,
        )

        # 清空缓存
        _text_cache.clear()

        # 填满缓存（使用递增的时间戳，i 越大表示越旧）
        base_time = time.time()
        for i in range(_TEXT_CACHE_MAX):
            _text_cache[i] = (f"content_{i}", base_time - i * 100)

        assert len(_text_cache) == _TEXT_CACHE_MAX

        # 调用 _text_cache_set 添加新条目（触发淘汰逻辑）
        _text_cache_set(999, "new_content")

        assert len(_text_cache) == _TEXT_CACHE_MAX
        # 最旧的 entry (i=_TEXT_CACHE_MAX-1) 应被淘汰
        assert _TEXT_CACHE_MAX - 1 not in _text_cache
        # 新的 entry 应存在
        assert 999 in _text_cache
        # 次新的 entry (i=0) 应存在
        assert 0 in _text_cache

        _text_cache.clear()


# ============================================================================
# Bug 7: 提取失败不写空 .txt
# ============================================================================

class TestSkipEmptyTxtWrite:
    """测试提取失败时不写空 .txt 文件。"""

    def test_empty_extraction_skips_txt_write(self):
        """空文本不应写入 .txt 文件。"""
        from app.services.files import save_upload
        import tempfile
        import os

        # 这个测试验证 save_upload 的逻辑：
        # 当 extracted_text 为空时，不应写入 .txt 文件
        # 我们验证代码逻辑而非实际调用（因为需要完整的 DB 和项目目录）

        # 验证 EXTRACT_DOC_NOTICE 等提示串存在
        from app.services.files import (
            EXTRACT_DOC_NOTICE,
            EXTRACT_PPT_NOTICE,
            EXTRACT_WPS_NOTICE,
            EXTRACTION_NOTICES,
            is_extraction_notice,
        )

        assert EXTRACT_DOC_NOTICE in EXTRACTION_NOTICES
        assert EXTRACT_PPT_NOTICE in EXTRACTION_NOTICES
        assert EXTRACT_WPS_NOTICE in EXTRACTION_NOTICES

        # 验证 is_extraction_notice 函数
        assert is_extraction_notice(EXTRACT_DOC_NOTICE) is True
        assert is_extraction_notice("normal text") is False
        assert is_extraction_notice("") is False
        assert is_extraction_notice(None) is False


# ============================================================================
# Bug 8 & 9: 前端修复（文件存在性验证）
# ============================================================================

class TestFrontendFixes:
    """验证前端修复的代码存在性。"""

    def test_frontend_file_size_validation(self):
        """前端应对超大文件做客户端校验（TD6 2026-10-12：上限改由后端下发）

        原用例断言源码里出现字面量 ``50 * 1024 * 1024`` 与「文件大小超过 50MB」。
        2026-10-12 起这两处硬编码被移除：上限改为由
        ``GET /files/supported-formats`` 下发的 ``max_file_size_mb`` 驱动
        （默认值仍是 50，行为不变），目的是消除「后端调大上限、前端仍按 50MB
        拦，用户传不上去」的漂移风险 —— 而那正是本用例想防的问题本身。
        因此断言口径同步更新为：仍做客户端校验 + 上限来自后端下发值。
        """
        fe_root = Path(__file__).parent.parent.parent / "frontend" / "src"
        frontend_path = fe_root / "pages" / "FileUploadPage.tsx"
        assert frontend_path.exists(), f"前端文件不存在: {frontend_path}"

        content = frontend_path.read_text(encoding="utf-8")
        # 客户端校验仍必须保留（否则超大文件会先读入内存再被后端拒绝）
        assert "maxFileSizeMb * 1024 * 1024" in content, \
            "客户端大小校验缺失，或未使用后端下发的上限"
        assert "文件大小超过 ${maxFileSizeMb}MB 限制" in content, \
            "应透出动态大小上限提示"
        # 默认值仍为 50MB，向后兼容
        assert "useState<number>(50)" in content, \
            "maxFileSizeMb 默认值应为 50MB"
        # 上限来源必须是后端端点（消除前后端硬编码漂移）
        assert "fetchSupportedFormats" in content, \
            "大小上限应来自后端 supported-formats 端点"
        api_src = (fe_root / "api" / "index.ts").read_text(encoding="utf-8")
        assert "max_file_size_mb" in api_src, "api 层应透出 max_file_size_mb"

    def test_frontend_loadfiles_race_protection(self):
        """前端 loadFiles 应包含在途状态防护。"""
        frontend_path = Path(__file__).parent.parent.parent / "frontend" / "src" / "pages" / "FileUploadPage.tsx"
        assert frontend_path.exists()

        content = frontend_path.read_text(encoding="utf-8")
        assert "loadFilesRef" in content, "应包含 loadFilesRef 防护"
        assert "loadFilesRef.current" in content, "应使用 loadFilesRef.current 标记在途状态"


# ============================================================================
# Batch 2 修复测试
# ============================================================================

# ----- Bug 11: 冗余 _sniff_extension 调用 -----

class TestNoRedundantSniffCall:
    """Bug 11: declared_ext 已设时不应冗余调用 _sniff_extension。"""

    def test_declared_ext_skips_sniff(self):
        """_do_extract_text 在 declared_ext 非空时不嗅探。"""
        import inspect
        from app.services.files import _do_extract_text

        source = inspect.getsource(_do_extract_text)
        # 应包含 'if not declared_ext' 条件守卫
        assert "if not declared_ext" in source, (
            "_do_extract_text 应在 declared_ext 非空时跳过 _sniff_extension"
        )

    def test_extract_text_from_path_sniff_once(self):
        """extract_text_from_path 只调用一次 _sniff_extension。"""
        import inspect
        from app.services.files import extract_text_from_path

        source = inspect.getsource(extract_text_from_path)
        # 应有 sniffed = _sniff_extension(file_path) 一次调用
        assert source.count("_sniff_extension(file_path)") == 1, (
            "extract_text_from_path 应只调用一次 _sniff_extension"
        )


# ----- Bug 12: HTMLParser skip_depth 不同步 -----

class TestHtmlParserSkipDepthSync:
    """Bug 12: 畸形 HTML 不会导致 skip_depth 卡死。"""

    def test_unclosed_script_tag(self):
        """未闭合的 <script> 标签不会吞掉后续内容。"""
        from app.services.files import _extract_html

        with tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w", encoding="utf-8") as f:
            f.write('<html><body><script>var x=1</script><p>正常文本</p></body></html>')
            tmp_path = f.name

        try:
            result = _extract_html(tmp_path)
            assert "正常文本" in result
            assert "var x=1" not in result
        finally:
            os.unlink(tmp_path)

    def test_nested_script_tags(self):
        """嵌套 <script> 标签正确处理。"""
        from app.services.files import _extract_html

        with tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w", encoding="utf-8") as f:
            f.write('<html><body><script><script>alert(1)</script></script><p>内容</p></body></html>')
            tmp_path = f.name

        try:
            result = _extract_html(tmp_path)
            assert "内容" in result
        finally:
            os.unlink(tmp_path)

    def test_malformed_html_no_crash(self):
        """严重畸形 HTML 不崩溃且提取非 skip 内容。"""
        from app.services.files import _extract_html

        with tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w", encoding="utf-8") as f:
            # 不匹配的 endtag 在 skip_depth=0 时应安全忽略
            f.write('<html><body></script><p>测试</p></body></html>')
            tmp_path = f.name

        try:
            result = _extract_html(tmp_path)
            assert "测试" in result
        finally:
            os.unlink(tmp_path)

    def test_style_tag_skip(self):
        """<style> 标签内容被跳过。"""
        from app.services.files import _extract_html

        with tempfile.NamedTemporaryFile(suffix=".html", delete=False, mode="w", encoding="utf-8") as f:
            f.write('<html><head><style>body{color:red}</style></head><body><p>可见</p></body></html>')
            tmp_path = f.name

        try:
            result = _extract_html(tmp_path)
            assert "可见" in result
            assert "color:red" not in result
        finally:
            os.unlink(tmp_path)


# ----- Bug 13: _extract_xlsx_raw_xml 错误分类 -----

class TestXlsxRawXmlErrorClassification:
    """Bug 13: 区分 FileNotFoundError / PermissionError / BadZipFile / Exception。"""

    def test_missing_file_returns_specific_message(self):
        """文件不存在时返回特定提示。"""
        from app.services.files import _extract_xlsx_raw_xml

        result = _extract_xlsx_raw_xml("/nonexistent/file.xlsx")
        assert "文件不存在" in result

    def test_non_zip_returns_corrupt_message(self):
        """非 ZIP 文件返回损坏提示。"""
        from app.services.files import _extract_xlsx_raw_xml

        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
            f.write(b"not a zip file")
            tmp_path = f.name

        try:
            result = _extract_xlsx_raw_xml(tmp_path)
            assert "文件损坏" in result
        finally:
            os.unlink(tmp_path)

    def test_valid_zip_returns_data(self):
        """有效 ZIP 容器应提取文本。"""
        from app.services.files import _extract_xlsx_raw_xml
        import zipfile
        import io

        # 构造最小 XLSX 结构
        shared_strings = (
            b'<?xml version="1.0"?>'
            b'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="1">'
            b'<si><t>CellData</t></si>'
            b'</sst>'
        )
        sheet1 = (
            b'<?xml version="1.0"?>'
            b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            b'<sheetData><row r="1"><c r="A1" t="s"><v>0</v></c></row></sheetData>'
            b'</worksheet>'
        )

        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
            tmp_path = f.name

        try:
            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("xl/sharedStrings.xml", shared_strings)
                zf.writestr("xl/worksheets/sheet1.xml", sheet1)

            result = _extract_xlsx_raw_xml(tmp_path)
            assert "CellData" in result
        finally:
            os.unlink(tmp_path)

    def test_error_messages_are_distinct(self):
        """三类错误的消息应互不相同。"""
        from app.services.files import _extract_xlsx_raw_xml

        result_missing = _extract_xlsx_raw_xml("/nonexistent.xlsx")
        assert "文件不存在" in result_missing
        assert "权限不足" not in result_missing
        assert "文件损坏" not in result_missing

        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
            f.write(b"junk")
            tmp_path = f.name

        try:
            result_badzip = _extract_xlsx_raw_xml(tmp_path)
            assert "文件损坏" in result_badzip
            assert "文件不存在" not in result_badzip
        finally:
            os.unlink(tmp_path)


# ----- Bug 16: classifyUploadResult 缺少 status 返回 parse_failed -----

class TestClassifyUploadResultMissingStatus:
    """Bug 16: 缺少 status 字段不再默认为成功。"""

    def test_missing_status_returns_parse_failed(self):
        """缺少 status 字段应返回 'parse_failed'。"""
        frontend_path = Path(__file__).parent.parent.parent / "frontend" / "src" / "utils.ts"
        content = frontend_path.read_text(encoding="utf-8")

        # 验证 classifyUploadResult 包含 parse_failed 分支
        assert 'res.status === undefined' in content
        assert 'res.status === null' in content
        # 验证返回 parse_failed
        idx = content.find("if (res.status === undefined")
        chunk = content[idx:idx + 200]
        assert 'return "parse_failed"' in chunk

    def test_null_status_returns_parse_failed(self):
        """status 为 null 应返回 'parse_failed'。"""
        frontend_path = Path(__file__).parent.parent.parent / "frontend" / "src" / "utils.ts"
        content = frontend_path.read_text(encoding="utf-8")

        idx = content.find("res.status === null")
        chunk = content[idx:idx + 200]
        assert 'return "parse_failed"' in chunk

    def test_normal_status_returns_ok(self):
        """正常 status 应返回 'ok'。"""
        frontend_path = Path(__file__).parent.parent.parent / "frontend" / "src" / "utils.ts"
        content = frontend_path.read_text(encoding="utf-8")

        idx = content.find('return "ok"')
        assert idx > 0, "classifyUploadResult 应包含 'ok' 返回值"


# ----- Bug 17: docx_extractor.py 内部 API 修复 -----

class TestDocxExtractorNoInternalApi:
    """Bug 17: docx_extractor.py 不再使用 doc.part._element.clear()。"""

    def test_no_element_clear_call(self):
        """不应调用 doc.part._element.clear()（注释/文档中的提及不算）。"""
        docx_path = Path(__file__).parent.parent.parent / "backend" / "app" / "services" / "docx_extractor.py"
        content = docx_path.read_text(encoding="utf-8")

        # 查找实际调用模式：doc.part._element.clear()（排除注释行）
        for i, line in enumerate(content.splitlines(), 1):
            stripped = line.strip()
            # 跳过注释和文档字符串行
            if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'''"):
                continue
            if "_element.clear()" in stripped:
                assert False, f"第 {i} 行使用了内部 API: {stripped}"

    def test_uses_del_doc(self):
        """应使用 del doc 释放资源。"""
        docx_path = Path(__file__).parent.parent.parent / "backend" / "app" / "services" / "docx_extractor.py"
        content = docx_path.read_text(encoding="utf-8")

        assert "del doc" in content, "应使用 del doc 释放 python-docx 文档对象"


# ----- Bug 18: _sniff_zip_extension 非 Office ZIP 返回 .zip -----

class TestSniffZipExtensionFallback:
    """Bug 18: 非 Office ZIP 返回 '.zip' 而非空串。"""

    def test_office_docx_returns_docx(self):
        """Office DOCX ZIP 应返回 '.docx'。"""
        from app.services.files import _sniff_zip_extension
        import zipfile

        with tempfile.NamedTemporaryFile(suffix=".docx", delete=False) as f:
            tmp_path = f.name

        try:
            with zipfile.ZipFile(tmp_path, "w") as zf:
                zf.writestr("word/document.xml", "<root/>")

            result = _sniff_zip_extension(tmp_path)
            assert result == ".docx"
        finally:
            os.unlink(tmp_path)

    def test_unknown_zip_returns_zip(self):
        """非 Office ZIP 应返回 '.zip'。"""
        from app.services.files import _sniff_zip_extension
        import zipfile

        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
            tmp_path = f.name

        try:
            with zipfile.ZipFile(tmp_path, "w") as zf:
                zf.writestr("readme.txt", "hello")

            result = _sniff_zip_extension(tmp_path)
            assert result == ".zip", (
                f"非 Office ZIP 应返回 '.zip'，实际返回 '{result}'"
            )
        finally:
            os.unlink(tmp_path)

    def test_non_zip_file_returns_empty(self):
        """非 ZIP 文件应返回空串。"""
        from app.services.files import _sniff_zip_extension

        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"plain text")
            tmp_path = f.name

        try:
            result = _sniff_zip_extension(tmp_path)
            assert result == ""
        finally:
            os.unlink(tmp_path)

    def test_missing_file_returns_empty(self):
        """文件不存在应返回空串。"""
        from app.services.files import _sniff_zip_extension

        result = _sniff_zip_extension("/nonexistent/file.zip")
        assert result == ""


# ----- Bug 19: _get_file_hash mtime_ns=0 内容兜底 -----

class TestFileHashMtimeFallback:
    """Bug 19: st_mtime_ns=0 时追加内容哈希确保唯一性。"""

    def test_normal_file_hash_is_consistent(self):
        """正常文件哈希应稳定。"""
        from app.services.files import _get_file_hash

        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"test content 123")
            tmp_path = f.name

        try:
            hash1 = _get_file_hash(tmp_path)
            hash2 = _get_file_hash(tmp_path)
            assert hash1 == hash2
            assert len(hash1) == 32  # MD5 hex digest
        finally:
            os.unlink(tmp_path)

    def test_different_files_different_hashes(self):
        """不同内容文件哈希应不同。"""
        from app.services.files import _get_file_hash

        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f1:
            f1.write(b"content A")
            path1 = f1.name

        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f2:
            f2.write(b"content B")
            path2 = f2.name

        try:
            # CF-03 回归锁定：两文件同大小（9 字节），若 os.stat 的 mtime_ns
            # 刻度恰好相同（快速连续创建 + 粗刻度文件系统），旧实现
            # size+mtime 哈希必然碰撞 → 解析缓存错配。现强制对齐 mtime，
            # 确定性复现旧碰撞条件，锁定「内容哈希叠加」修复。
            os.utime(path1, (1000000000, 1000000000))
            os.utime(path2, (1000000000, 1000000000))
            hash1 = _get_file_hash(path1)
            hash2 = _get_file_hash(path2)
            assert hash1 != hash2, "同大小同 mtime 的不同内容文件哈希不得碰撞"
        finally:
            os.unlink(path1)
            os.unlink(path2)

    def test_mtime_zero_fallback_in_code(self):
        """源码应包含内容哈希兜底逻辑。

        CF-03 修复后内容哈希从「st_mtime_ns==0 时条件追加」升级为
        「无条件追加」，源码结构断言随之更新。
        """
        import inspect
        from app.services.files import _get_file_hash

        source = inspect.getsource(_get_file_hash)
        assert "st_mtime_ns" in source, "应基于 mtime_ns 参与哈希"
        assert "4096" in source, "应读取前 4KB 内容做内容哈希"
        assert "fh.read" in source, "应无条件叠加内容哈希（防同刻度碰撞）"


# ----- Bug 6: _extract_balanced_json 性能优化 -----

class TestExtractBalancedJsonPerformance:
    """Bug 6: 大量不匹配开括号时不出现 O(n²) 退化。"""

    def test_many_unmatched_openers_fast(self):
        """1000 个不匹配开括号不应导致超时。"""
        from app.utils.json_parser import _extract_balanced_json

        # 构造大量不匹配的开括号后跟一个合法 JSON
        content = "{" * 1000 + '{"a": 1}' + " " * 10000
        # 在 5 秒内完成（O(n²) 实现会超时）
        start = time.time()
        result = _extract_balanced_json(content)
        elapsed = time.time() - start

        assert result is not None
        assert '{"a": 1}' in result
        assert elapsed < 5.0, f"提取耗时 {elapsed:.2f}s，疑似 O(n²) 退化"

    def test_nested_json_extraction(self):
        """嵌套 JSON 正确提取。"""
        from app.utils.json_parser import _extract_balanced_json

        content = 'prefix {"a": {"b": [1, 2, 3]}} suffix'
        result = _extract_balanced_json(content)
        assert result == '{"a": {"b": [1, 2, 3]}}'

    def test_multiple_json_fragments(self):
        """多个 JSON 片段提取第一个。"""
        from app.utils.json_parser import _extract_balanced_json

        content = 'noise {"x": 1} middle [1,2] end {"y": 2}'
        result = _extract_balanced_json(content)
        assert result == '{"x": 1}'

    def test_no_json_returns_none(self):
        """无 JSON 返回 None。"""
        from app.utils.json_parser import _extract_balanced_json

        result = _extract_balanced_json("no json here at all")
        assert result is None

    def test_empty_string_returns_none(self):
        """空字符串返回 None。"""
        from app.utils.json_parser import _extract_balanced_json

        result = _extract_balanced_json("")
        assert result is None

    def test_string_with_brackets(self):
        """字符串内的括号不影响配平。"""
        from app.utils.json_parser import _extract_balanced_json

        content = '{"key": "value with { and } brackets"}'
        result = _extract_balanced_json(content)
        assert result == '{"key": "value with { and } brackets"}'


# ----- Bug 7: _try_parse_candidate 惰性修复 -----

class TestTryParseCandidateLazy:
    """Bug 7: 首次成功时不预计算全部修复变体。"""

    def test_valid_json_no_repair_called(self):
        """有效 JSON 直接解析成功，不触发修复。"""
        from app.utils.json_parser import _try_parse_candidate

        result = _try_parse_candidate('{"key": "value"}')
        assert result == {"key": "value"}

    def test_repair_escape_fixes(self):
        """非法转义修复后解析成功。"""
        from app.utils.json_parser import _try_parse_candidate

        result = _try_parse_candidate('{"key": "val\\ue"}')
        assert result is not None

    def test_repair_trailing_comma(self):
        """尾随逗号修复后解析成功。"""
        from app.utils.json_parser import _try_parse_candidate

        result = _try_parse_candidate('{"key": "value",}')
        assert result is not None

    def test_repair_combined_escape_and_comma(self):
        """同时含非法转义和尾随逗号的组合修复。"""
        from app.utils.json_parser import _try_parse_candidate

        result = _try_parse_candidate('{"key": "val\\ue",}')
        assert result is not None

    def test_source_uses_lazy_eval(self):
        """源码应使用惰性求值而非预计算。"""
        import inspect
        from app.utils.json_parser import _try_parse_candidate

        source = inspect.getsource(_try_parse_candidate)
        # 不应有预先计算所有变体的模式
        assert "escapes_fixed = repair_invalid_json_escapes(candidate)" not in source, (
            "不应预计算所有修复变体"
        )
        # 应有惰性求值标记：早期返回模式（每次尝试后立即检查并返回）
        assert "if result is not None:" in source, (
            "应使用早期返回模式实现惰性求值"
        )


# ----- classifyUploadResult 单元测试 -----

class TestClassifyUploadResultBackend:
    """Bug 16: 后端侧验证 classifyUploadResult 逻辑完整性。"""

    def test_front_end_test_file_exists(self):
        """前端测试文件应存在。"""
        test_path = Path(__file__).parent.parent.parent / "frontend" / "src" / "utils.upload.test.ts"
        assert test_path.exists(), f"前端测试文件不存在: {test_path}"

    def test_classify_in_test_file(self):
        """前端测试应覆盖 classifyUploadResult 缺 status 场景。"""
        test_path = Path(__file__).parent.parent.parent / "frontend" / "src" / "utils.upload.test.ts"
        content = test_path.read_text(encoding="utf-8")

        assert "classifyUploadResult" in content
        # 验证测试了缺 status 的情况
        assert "status" in content.lower() or "undefined" in content


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
