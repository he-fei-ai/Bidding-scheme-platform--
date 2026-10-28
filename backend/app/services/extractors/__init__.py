# -*- coding: utf-8 -*-
"""R-1 2026-10-12：文件格式文本提取器（从 services/files.py 原样搬迁）。

files.py 单文件承载 11+ 个提取器 + 缓存 + 任务表 + OCR 编排，可维护性差
（报告 §7.1 R-1）。本包为**纯搬迁，不改行为**。

迁移约定：
  · 与 files.py 单向依赖：本包不在模块级 import files（否则成环）。
  · **跨符号调用一律经 `_late()` 按 files 模块属性晚解析**。这是保住既有测试
    monkeypatch 接缝的关键：原先 `_extract_legacy_ole_chain` 直接调同模块的
    `_extract_wps`，搬走后若不晚解析，测试 patch `files._extract_wps` 就失效
    （首版实测降级链因此拿到 PPT 提示串而非 WPS 正文）。晚解析后未 patch 时取到
    同一实现、patch 时取到补丁，行为与搬迁前一致。
  · 提示串常量在 `._notices`（中立模块），两边共用。
"""

import logging
import os
import re
import shutil
import threading
import time
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from ...config import (
    OCR_ENABLED,
    OCR_ENGINE,
    OCR_MAX_CONCURRENT,
    OCR_MAX_FILE_SIZE_MB,
    OCR_TIMEOUT,
    PDF_EXTRACT_TIMEOUT,
    PDF_TABLE_EXTRACT_ENABLED,
    PPT_COM_ENABLED,
    PPT_COM_TIMEOUT,
)
from ._notices import (
    EXTRACT_DOC_NOTICE,
    EXTRACT_PPT_NOTICE,
    EXTRACT_WPS_NOTICE,
    is_extraction_notice,
)

logger = logging.getLogger(__name__)


def _late(name: str):
    """按 `files` 模块属性晚解析被调用方（保住 monkeypatch 接缝，见模块 docstring）。

    调用时才 import，避免与 files.py 顶部对本包的 re-export 形成循环导入。
    """
    from ...services import files as _files

    return getattr(_files, name)


# BUG-FIX-71: MinerU 单例初始化锁，防止多线程并发创建多个实例
_mineru_lock = threading.Lock()

# BUG-FIX 2026-10-08：MinerU 解析器单例从函数属性改为模块级变量，
# 避免模块重载时属性丢失导致重复创建实例（内存泄漏风险）
_mineru_parser_instance = None


def _extract_docx(file_path: str) -> str:
    """提取 Word 文档文本（python-docx 主引擎 + docx2python 回退，详见 docx_extractor 模块）"""
    return _late("_extract_docx_docx")(file_path)


# BUG-FIX 2026-10-06（R2）：旧版 Office 二进制格式（OLE2 复合文档）降级链。
# 魔数只能判定「是 OLE 容器」，.doc/.xls/.ppt/.wps 共享同一容器格式：
#   - 用户把真 .doc 改名 .docx → 嗅探判 .doc（见 _sniff_extension）；
#   - 按 .doc 分支提取为空/降级提示时，该文件仍可能是改名/混用的 .xls/.ppt/.wps。
# 逐个尝试四个提取器，取首个「真实文本」（非空且非降级提示串）；全部失败时
# 返回最后一个降级提示串（保持 H1 判定链路如实把文件标为 failed），
# 若所有提取器都只返回空串则返回空串（success + 空文本提示，与既有口径一致）。
_LEGACY_OLE_EXTS: frozenset[str] = frozenset({".doc", ".xls", ".ppt", ".wps"})

# OLE2 复合文档魔数（.doc/.xls/.ppt/.wps 共享的容器头）
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _is_ole2_file(file_path: str) -> bool:
    """文件头是否为 OLE2 复合文档魔数（读失败按 False 处理）"""
    try:
        with open(file_path, "rb") as _f:
            return _f.read(8) == _OLE2_MAGIC
    except OSError:
        return False


def _extract_legacy_ole_chain(file_path: str, declared_ext: str = "") -> str:
    """OLE2 容器降级提取：声明扩展名分支优先，其余 .xls/.ppt/.wps 按需补试。

    排序规则（BUG-FIX 2026-10-06 R2）：
      - ``.doc`` 永远第一个试（olefile 廉价链路，覆盖绝大多数场景）；
      - 用户声明的扩展名（如 .xls）排第二 —— 声明 .xls 的文件不该先白等
        一次 PowerPoint COM（可能拉起 Office 进程、最长 _late("PPT_COM_TIMEOUT") 秒）；
      - 其余分支按 xls → ppt → wps 兜底。
    """
    _order = [".doc", ".xls", ".ppt", ".wps"]
    if declared_ext in _LEGACY_OLE_EXTS and declared_ext != ".doc":
        _order.remove(declared_ext)
        _order.insert(1, declared_ext)
    # R-1：字典里存**名字**而非函数引用，调用时经 _late() 按 files 模块属性解析。
    # 若像搬迁前那样直接存本包函数引用，测试 monkeypatch `files._extract_wps`
    # 就再也影响不到降级链（首版实测因此拿到 PPT 提示串而非 WPS 正文）。
    _handlers = {
        ".doc": "_extract_doc",
        ".xls": "_extract_xls",
        ".ppt": "_extract_ppt",
        ".wps": "_extract_wps",
    }
    last_notice = ""
    for label in _order:
        handler = _late(_handlers[label])
        try:
            text = handler(file_path)
        except Exception as e:
            logger.debug("[OLE降级链] %s 分支失败（尝试下一分支）: %s", label, e)
            continue
        if text and not _late("is_extraction_notice")(text):
            if label != declared_ext and declared_ext in _LEGACY_OLE_EXTS:
                logger.info(
                    "[OLE降级链] %s 声明 %s，实际按 %s 分支提取成功",
                    Path(file_path).name, declared_ext, label,
                )
            return text
        if text:
            last_notice = text
    return last_notice


def _extract_pdf_pdfplumber(file_path: str) -> str:
    """用 pdfplumber 提取 PDF 文本（表格感知 + 页码标识 + 表格标记）。

    ENHANCE 2026-10-05（P-2）：对齐易标规格一.1：
      - 按页提取，每页前加 ``--- 第 N 页 ---`` 页码标识
      - 表格用 ``[表格 n]`` ... ``[表格结束]`` 包裹（n 为该页内的表格序号），
        与 Word 路径的 ``[表格内容]/[表格结束]`` 标记形成对照

    与 pypdf 的差异：pdfplumber 能区分表格区域并按行还原表格单元格，
    pypdf 只输出纯文本流、表格结构完全丢失。

    容错策略（任一环节失败都降级，绝不抛出）：
      - pdfplumber 未安装 → 返回空串（由调用方回退 pypdf）
      - 文件打不开 / 页迭代失败 → 返回空串
      - 单页 extract_tables 失败 → 该页只保留纯文本、跳过表格标记
      - 单页 extract_text 失败 → 跳过该页

    Returns:
        str: 带页码标识与表格标记的全文；失败或无内容时为空串。
    """
    try:
        import pdfplumber
    except ImportError:
        logger.debug("[PDF] pdfplumber 未安装，跳过表格感知提取")
        return ""

    parts: list[str] = []
    try:
        with pdfplumber.open(file_path) as pdf:
            for page_no, page in enumerate(pdf.pages, 1):
                body = ""
                try:
                    body = page.extract_text() or ""
                except Exception as e:
                    logger.warning("[PDF] 第 %d 页文本提取失败，跳过该页: %s", page_no, e)
                    continue

                table_blocks: list[str] = []
                try:
                    tables = page.extract_tables() or []
                except Exception as e:
                    logger.debug("[PDF] 第 %d 页表格提取失败（仅保留纯文本）: %s", page_no, e)
                    tables = []

                for t_idx, table in enumerate(tables, 1):
                    if not table:
                        continue
                    rows_txt = [_late("_format_pdf_table_row")(row) for row in table]
                    rows_txt = [r for r in rows_txt if r]
                    if not rows_txt:
                        continue
                    table_blocks.append(
                        f"\n[表格 {t_idx}]\n" + "\n".join(rows_txt) + "\n[表格结束]\n"
                    )

                if not body.strip() and not table_blocks:
                    continue

                chunks = [f"\n--- 第 {page_no} 页 ---\n"]
                if body.strip():
                    chunks.append(body)
                if table_blocks:
                    chunks.append("\n".join(table_blocks))
                parts.append("".join(chunks))
    except Exception as e:
        logger.warning("[PDF] pdfplumber 打开/迭代失败: %s (%s)", Path(file_path).name, e)
        return ""

    return "\n".join(parts)


def _format_pdf_table_row(row: list) -> str:
    """把 pdfplumber 的一行单元格渲染成 `| a | b | c |` 形式的文本行。

    单元格可能为 None（合并单元格）或含换行，统一清洗为单行，
    避免破坏 Markdown 表格的可解析性。
    """
    cells = []
    for cell in row:
        if cell is None:
            cells.append("")
        else:
            cells.append(str(cell).replace("\r", " ").replace("\n", " ").strip())
    return "| " + " | ".join(cells) + " |"


def _extract_pdf(file_path: str) -> str:
    """提取 PDF 文本（带超时护栏的外层入口）。

    BUG-FIX 2026-10-19（上传解析卡死）：pdfplumber/pypdf 对病态 PDF 或
    环境放大（DEBUG 日志风暴下每条记录持锁写盘）可能挂几十分钟不返回。
    解析跑在请求线程（asyncio.to_thread）里，前端超时判失败，而
    file_parse_tasks 停在 processing、uploaded_files 无记录。与
    PPT_COM_TIMEOUT 同教训，给整个提取加限时护栏：
      - 超时抛 TimeoutError，由 save_upload 既有失败分支如实写
        「文本提取失败」进任务行与文件行，不再无限挂住上传请求；
      - 子线程为 daemon，超时后自行收尾（与 PPT COM daemon 线程同口径）；
      - PDF_EXTRACT_TIMEOUT=0 关闭护栏（保持旧行为）。
    被包装的原实现见 `_extract_pdf_impl`（逐字未动，保持既有测试对
    `files._extract_pdf` 源码的 inspect 断言与 monkeypatch 接缝语义不变——
    patch 本模块 `files._extract_pdf` 仍然命中此入口）。
    """
    timeout = PDF_EXTRACT_TIMEOUT
    if timeout and timeout > 0:
        box: dict[str, Any] = {}

        def _run() -> None:
            try:
                box["result"] = _extract_pdf_impl(file_path)
            except BaseException as e:  # 原样回传给调用方判型（含 TimeoutError 语义透传）
                box["error"] = e

        worker = threading.Thread(target=_run, daemon=True, name="pdf-extract-guard")
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            raise TimeoutError(
                f"PDF 解析超时（>{timeout}s），文件可能过于复杂或损坏，"
                "请拆分/修复后重新上传"
            )
        if "error" in box:
            raise box["error"]
        return box.get("result", "")
    return _extract_pdf_impl(file_path)


def _extract_pdf_impl(file_path: str) -> str:
    """提取 PDF 文本（增强 1.1: 支持扫描件 OCR 自动降级）

    使用 pypdf 提取文本，若文本不足 100 字符则自动尝试 MinerU OCR 解析。

    ENHANCE 2026-10-05（P-2）：优先走 pdfplumber 表格感知提取——
    对齐易标规格一.1「PDF 文件提取：按页提取、添加页码标识、表格标记
    [表格 n]/[表格结束]」。旧实现只有 pypdf 纯文本流，**表格结构完全丢失**
    （pypdf 不区分表格与正文），下游 AI 分析无法还原表格数据。
    pdfplumber 不可用或整页提取失败时回退 pypdf（原有行为，向后兼容）。
    """
    # 优先：pdfplumber（表格感知 + 页码标识 + 表格标记）
    if _late("PDF_TABLE_EXTRACT_ENABLED"):
        try:
            plumber_text = _late("_extract_pdf_pdfplumber")(file_path)
        except ImportError:
            plumber_text = ""
        except Exception as e:
            logger.warning("[PDF] pdfplumber 提取失败，回退 pypdf: %s", e)
            plumber_text = ""
        if plumber_text and len(plumber_text.strip()) >= 100:
            logger.info(
                "[PDF] pdfplumber 提取成功: %s -> %d 字符（含表格标记）",
                Path(file_path).name, len(plumber_text),
            )
            return plumber_text
        if plumber_text:
            logger.info(
                "[PDF] pdfplumber 仅提取 %d 字符，视为疑似扫描件，继续走 pypdf/OCR 路径",
                len(plumber_text.strip()),
            )

    # 回退：pypdf（原有路径）
    try:
        from pypdf import PdfReader
    except ImportError:
        raise ImportError("需要安装 pypdf: pip install pypdf")

    try:
        reader = PdfReader(file_path)
    except Exception as e:
        # PDF 文件严重损坏，无法打开
        logger.warning("[PDF] 无法打开文件: %s, 错误: %s", Path(file_path).name, e)
        return ""

    try:
        pages = []
        # BUG 修复：某些 PDF（如含损坏对象、版本兼容问题的文件）单页
        # extract_text() 会抛异常（如 'NumberObject' object has no attribute 'get'），
        # 此前会让整个文件解析失败。逐页 try/except 容错，坏页跳过。
        try:
            page_iter = enumerate(reader.pages, 1)
        except Exception as e:
            # PDF 结构损坏，无法获取页面列表（如 'NumberObject' 错误）
            logger.warning("[PDF] 无法获取页面列表，文件可能已损坏: %s", e)
            return ""

        try:
            for i, page in page_iter:
                try:
                    text = page.extract_text()
                except Exception as e:
                    logger.warning("[PDF] 第 %d 页文本提取失败，跳过该页: %s", i, e)
                    continue
                if text and text.strip():
                    pages.append(f"\n--- 第 {i} 页 ---\n{text}")
        except Exception as e:
            # 迭代过程中发生错误（某些 pypdf 版本在迭代时才会触发损坏检测）
            logger.warning("[PDF] 页面迭代失败，文件可能已损坏: %s", e)
            return ""

        pypdf_text = "\n".join(pages)
    finally:
        try:
            reader.stream.close()
        except Exception as _e:
            logger.debug("[silent-except] files.py: line 391 - %s", _e)

    # 若提取文本不足 100 字符，高度疑似扫描件（图片型 PDF），记录日志并触发 OCR
    pypdf_len = len(pypdf_text.strip())
    if pypdf_len < 100:
        logger.info(
            "[PDF] pypdf 仅提取 %d 字符，文件可能为扫描件（图片型 PDF），尝试 OCR 降级",
            pypdf_len,
        )
    if pypdf_len < 100 and _late("OCR_ENABLED"):
        try:
            from ..ocr_parser import MinerUParser

            # 模块级单例，避免每次创建新实例导致缓存失效
            # BUG-FIX 2026-10-08：从函数属性改为模块级变量，避免模块重载时属性丢失
            global _mineru_parser_instance
            with _mineru_lock:
                if _mineru_parser_instance is None:
                    _mineru_parser_instance = MinerUParser()
            parser = _mineru_parser_instance
            if parser.is_available():
                ocr_text = parser.parse_pdf(file_path)
                if ocr_text and len(ocr_text.strip()) >= 100:
                    logger.info(
                        "[PDF OCR] 扫描件解析成功: %s -> %s 字符", Path(file_path).name, len(ocr_text)
                    )
                    return ocr_text
                else:
                    logger.warning("[PDF OCR] MinerU 解析结果为空，保留 pypdf 结果")
            else:
                logger.info("[PDF OCR] MinerU 不可用，保留 pypdf 结果")
        except ImportError:
            logger.debug("[PDF OCR] MinerU 模块未安装，跳过 OCR 解析")
        except Exception as e:
            logger.warning("[PDF OCR] MinerU 解析异常: %s", e)

    return pypdf_text


def _extract_doc(file_path: str) -> str:
    """提取旧版 .doc 文件文本（使用 olefile 直接从二进制流提取）"""
    # 方法 1: olefile 提取 Word 文档的文本流
    try:
        import olefile

        ole = olefile.OleFileIO(file_path)
        try:
            # 尝试读取 Word 文档流
            text_parts = []
            for stream_name in ole.listdir():
                name = "/".join(stream_name).lower()
                # WordDocument 流包含主要的文本内容
                if name == "worddocument":
                    data = ole.openstream(stream_name).read()
                    # Unicode 文本通常以 \0 分隔，尝试解码
                    # BUG-FIX 2026-10-08：errors="ignore"→"replace"，
                    # 配合 isprintable() 过滤保留真实字符、丢弃二进制噪声
                    try:
                        raw = data.decode("utf-16-le", errors="replace")
                        clean = "".join(c for c in raw if (c.isprintable() and c != '\ufffd') or c in "\n\r\t")
                        if clean.strip():
                            text_parts.append(clean)
                    except Exception as _e:
                        logger.debug("[silent-except] files.py: line 452 - %s", _e)
                # 表格、文本流等（可能含合同条款、工程量清单等）
                # BUG-FIX 2026-10-11（Dead Code Removal）：原实现连续有两个完全相同的
                # ``elif "text" in name or "table" in name:`` 分支 —— 第二个用旧的
                # ``errors="ignore"``（违背 2026-10-08 的 ignore→replace 修复），
                # 且因 if/elif/elif 结构**永远不可达**。已删除，仅保留第一个正确版本。
                elif "text" in name or "table" in name:
                    data = ole.openstream(stream_name).read()
                    try:
                        raw = data.decode("utf-16-le", errors="replace")
                        clean = "".join(c for c in raw if (c.isprintable() and c != '\ufffd') or c in "\n\r\t")
                        if clean.strip():
                            text_parts.append(clean)
                    except Exception as _e:
                        logger.debug("[silent-except] files.py: _extract_doc ole - %s", _e)
            if text_parts:
                return "\n".join(text_parts).strip()
        finally:
            ole.close()
    except ImportError:
        logger.warning("olefile 未安装，尝试其他方法提取 .doc 文件")
    except Exception:
        logger.warning("olefile 提取 .doc 失败", exc_info=True)

    # 方法 2: python-docx 兼容打开（部分 .doc 可被识别）
    try:
        from docx import Document

        doc = Document(file_path)
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except Exception:
        logger.warning("doc提取方法2失败", exc_info=True)

    # 方法 3: antiword 或 catdoc 命令行
    import subprocess

    antiword_path = shutil.which("antiword")
    catdoc_path = shutil.which("catdoc")

    if antiword_path:
        try:
            file_path_str = str(file_path)
            # BUG-FIX 2026-09-18（中）：原为字符串前缀比较，可被
            # `projects_evil` 或 `projects/../..` 绕过，改为真实父子关系判定。
            #
            # BUG-FIX 2026-10-10：原在此处 `return ""`。空串会被上层判定为
            # 「解析成功但正文为空」（_late("is_extraction_notice")('') 为 False ->
            # status='success'），而非「无法提取」——于是主文件静默变成空正文，
            # 解析提取 -> 全局事实/摘要/评分点 -> 目录 -> 正文 整条提示词数据链
            # 全部基于空上下文生成，且用户全程无感。
            # 该分支可达性很高：Git for Windows 自带 antiword
            # (C:\Program Files\Git\mingw64\bin\antiword.EXE)，shutil.which 必然命中。
            # 正确做法：守卫命中时**跳过该命令行方法**并继续降级链，
            # 最终落到 return EXTRACT_DOC_NOTICE，让文件如实标为 failed。
            if _late("_is_under_projects_dir")(file_path):
                result = subprocess.run(
                    [antiword_path, "-t", file_path_str],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode == 0 and result.stdout:
                    return result.stdout.strip()
        except Exception:
            logger.warning("doc提取方法3(antiword)失败", exc_info=True)

    if catdoc_path:
        try:
            file_path_str = str(file_path)
            # 同上：守卫命中时跳过本方法，而非提前返回空串。
            if _late("_is_under_projects_dir")(file_path):
                result = subprocess.run(
                    [catdoc_path, file_path_str],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode == 0 and result.stdout:
                    return result.stdout.strip()
        except Exception:
            logger.warning("doc提取方法4(catdoc)失败", exc_info=True)

    return EXTRACT_DOC_NOTICE


def _extract_xlsx_openpyxl(file_path: str) -> str:
    """使用 openpyxl 提取 .xlsx 文件文本（支持表格结构、合并单元格、多工作表）"""
    try:
        import openpyxl
    except ImportError:
        raise ImportError("需要安装 openpyxl: pip install openpyxl")

    try:
        wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
        try:
            all_sheets = []

            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                rows = []
                for row in ws.iter_rows(values_only=True):
                    cells = [str(cell).strip() if cell is not None else "" for cell in row]
                    if any(c for c in cells):
                        rows.append(" | ".join(cells))
                if rows:
                    all_sheets.append(f"=== 工作表: {sheet_name} ===")
                    all_sheets.extend(rows)
            return "\n".join(all_sheets) if all_sheets else ""
        finally:
            # BUG-FIX 2026-09-16（低）：异常路径以前从不 close —— 损坏的 xlsx 每次
            # 重新提取都泄漏一个句柄/内存映射，Windows 上还会让原文件删不掉。
            try:
                wb.close()
            except Exception as _close_e:
                logger.debug("[silent-except] files.py wb.close - %s", _close_e)
    except Exception:
        logger.warning("openpyxl 提取失败，回退到 XML 解析", exc_info=True)

    # 回退：直接解析 ZIP 内 XML
    return _late("_extract_xlsx_raw_xml")(file_path)


def _extract_xlsx_raw_xml(file_path: str) -> str:
    """直接解析 ZIP 内 XML 提取 Excel 文本（无 openpyxl 时的回退方案）"""
    rows = []
    ns = {
        "s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
        "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    }

    try:
        with zipfile.ZipFile(file_path) as z:
            shared_strings = []
            if "xl/sharedStrings.xml" in z.namelist():
                tree = ElementTree.parse(z.open("xl/sharedStrings.xml"))
                for si in tree.findall(".//s:si", ns):
                    texts = si.findall(".//s:t", ns)
                    shared_strings.append("".join(t.text or "" for t in texts))

            sheet_files = [
                f for f in z.namelist() if f.startswith("xl/worksheets/") and f.endswith(".xml")
            ]
            for sheet_file in sheet_files:
                tree = ElementTree.parse(z.open(sheet_file))
                sheet_rows = []
                for row in tree.findall(".//s:row", ns):
                    cells = []
                    for cell in row.findall("s:c", ns):
                        value = cell.find("s:v", ns)
                        cell_text = value.text if value is not None else ""
                        if cell.get("t") == "s" and cell_text and shared_strings:
                            try:
                                cell_text = shared_strings[int(cell_text)]
                            except (IndexError, ValueError):
                                pass
                        cells.append(cell_text.strip())
                    if any(c for c in cells):
                        sheet_rows.append(" | ".join(cells))
                if sheet_rows:
                    rows.append(f"=== 工作表: {Path(sheet_file).stem} ===")
                    rows.extend(sheet_rows)
    except FileNotFoundError as e:
        logger.error("Excel 文件不存在: %s - %s", file_path, e)
        return f"[Excel 文件不存在] {e}"
    except PermissionError as e:
        logger.error("Excel 文件权限不足: %s - %s", file_path, e)
        return f"[Excel 权限不足] {e}"
    except zipfile.BadZipFile as e:
        logger.error("Excel 文件损坏（非 ZIP 容器）: %s - %s", file_path, e)
        return f"[Excel 文件损坏] {e}"
    except Exception as e:
        logger.error("Excel 解析失败: %s - %s", file_path, e, exc_info=True)
        return f"[Excel 解析错误] {e}"

    return "\n".join(rows)


def _extract_xls(file_path: str) -> str:
    """使用 xlrd 提取旧版 .xls 文件文本"""
    try:
        import xlrd
    except ImportError:
        raise ImportError("需要安装 xlrd==1.2.0: pip install xlrd==1.2.0")

    try:
        wb = xlrd.open_workbook(file_path)
        all_sheets = []

        for sheet_idx in range(wb.nsheets):
            ws = wb.sheet_by_index(sheet_idx)
            sheet_name = ws.name
            rows = []
            for row_idx in range(ws.nrows):
                cells = []
                for col_idx in range(ws.ncols):
                    cell = ws.cell(row_idx, col_idx)
                    val = _late("_xls_cell_value")(cell)
                    cells.append(val)
                if any(c for c in cells):
                    rows.append(" | ".join(cells))
            if rows:
                all_sheets.append(f"=== 工作表: {sheet_name} ===")
                all_sheets.extend(rows)

        return "\n".join(all_sheets) if all_sheets else ""
    except Exception as e:
        logger.error("xlrd 提取 .xls 失败: %s", e)
        return f"[Excel 解析错误] {e}"


def _extract_pptx(file_path: str) -> str:
    """提取PPTX/PPT演示文稿文本内容"""
    try:
        from pptx import Presentation
    except ImportError:
        raise ImportError("需要安装 python-pptx: pip install python-pptx")

    try:
        prs = Presentation(file_path)
        text_parts = []

        for slide_idx, slide in enumerate(prs.slides, 1):
            text_parts.append(f"\n--- 幻灯片 {slide_idx}/{len(prs.slides)} ---")

            for shape in slide.shapes:
                if not shape.has_text_frame:
                    continue

                for para in shape.text_frame.paragraphs:
                    para_text = para.text.strip()
                    if para_text:
                        # 判断是否为标题
                        is_title = False
                        if para.level == 0 and para.font:
                            font_size = para.font.size
                            if para.font.bold or (font_size and font_size.pt > 24):
                                is_title = True

                        if is_title:
                            text_parts.append(f"# {para_text}")
                        elif para.level > 0:
                            indent = "  " * para.level
                            text_parts.append(f"{indent}- {para_text}")
                        else:
                            text_parts.append(para_text)

                # 处理形状内的表格
                if shape.has_table:
                    table = shape.table
                    text_parts.append("\n[表格内容]")
                    for row_idx, row in enumerate(table.rows):
                        cells = []
                        for cell in row.cells:
                            cell_text = cell.text.strip()
                            cells.append(cell_text if cell_text else "")
                        text_parts.append(" | ".join(cells))
                    text_parts.append("[表格结束]")

        return "\n".join(text_parts)
    except Exception as e:
        logger.error("PPTX python-pptx 提取失败: %s", e, exc_info=True)
        # BUG-FIX 2026-10-08：python-pptx 解析失败时回退到原始 XML 解析，
        # 避免单个损坏文件导致整个上传管线崩溃（与 _extract_xlsx_openpyxl 同口径）
        try:
            fallback = _late("_extract_pptx_raw_xml")(file_path)
            if fallback:
                logger.warning("PPTX 原始 XML 回退解析成功: %s (%d 字符)", file_path, len(fallback))
                return fallback
        except Exception as fe:
            logger.warning("PPTX 原始 XML 回退解析也失败: %s", fe)
        return f"[PPTX 解析错误] {e}"


def _extract_pptx_raw_xml(file_path: str) -> str:
    """原始 XML 回退解析 PPTX（python-pptx 失败时兜底）

    PPTX 是 ZIP 容器，幻灯片文本在 ppt/slides/slideN.xml 的 <a:t> 元素中。
    与 _extract_xlsx_raw_xml 同口径，用 stdlib zipfile + ElementTree 解析。
    """
    try:
        import re
        from xml.etree import ElementTree as ET

        slide_texts: list[str] = []
        with zipfile.ZipFile(file_path) as z:
            slide_names = sorted(
                n for n in z.namelist()
                if re.match(r"ppt/slides/slide\d+\.xml$", n)
            )
            for slide_name in slide_names:
                with z.open(slide_name) as f:
                    tree = ET.parse(f)
                root = tree.getroot()
                # <a:t> 包含所有文本
                ns_a = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
                texts = [t.text or "" for t in root.iter(f"{ns_a}t") if t.text]
                if texts:
                    slide_texts.append("\n".join(texts))
        return "\n\n".join(slide_texts)
    except Exception:
        return ""


# 遗留修复 2026-10-06（L1）：.ppt COM 自动化的降级提示（多处共用，文案保持一致）
_PPT_FALLBACK_HINT = (
    EXTRACT_PPT_NOTICE
)


def _ppt_com_extract_sync(file_path: str) -> str:
    """在独立子进程里同步执行 PowerPoint COM 提取（**可能长时间不返回**）。

    调用方 :func:`_ppt_via_powerpoint_com` 会把它放进可超时的子进程执行
    （原生崩溃只杀子进程，不连累解析进程）。

    拆成独立函数的理由：COM 调用是唯一可能挂死 / 原生崩溃的部分，把它与「超时包装」解耦后，
    超时逻辑才可以被单测直接覆盖（见 tests/test_import_enhancements_2026_10_05.py
    的 TestPptComGuardrails）。

    测试钩子（仅测试使用，生产绝不设置）：
      - ``PPT_COM_INJECT_RESULT``：直接返回该文本，跳过真实 COM；
      - ``PPT_COM_INJECT_HANG``：睡眠 60s，用于验证超时护栏。
    """
    import os

    # —— 测试注入钩子（默认不触发）——
    _inject = os.environ.get("PPT_COM_INJECT_RESULT")
    if _inject is not None:
        return _inject
    if os.environ.get("PPT_COM_INJECT_HANG") == "1":
        import time

        time.sleep(60)
        return ""

    powerpoint = None
    presentation = None
    try:
        import pythoncom

        import win32com.client

        # COM 是线程亲和的：每个使用 COM 的线程必须自行 CoInitialize/CoUninitialize
        pythoncom.CoInitialize()
        try:
            powerpoint = win32com.client.Dispatch("PowerPoint.Application")
            powerpoint.Visible = False
            presentation = powerpoint.Presentations.Open(os.path.abspath(file_path))

            text_parts = []
            total_slides = len(presentation.Slides)
            for slide_idx in range(1, total_slides + 1):
                text_parts.append(f"\n--- 幻灯片 {slide_idx}/{total_slides} ---")
                slide = presentation.Slides(slide_idx)
                for shape in slide.Shapes:
                    if shape.HasTextFrame:
                        for para in shape.TextFrame.TextRange.Paragraphs():
                            para_text = para.Text.strip()
                            if para_text:
                                text_parts.append(para_text)
            return "\n".join(text_parts)
        finally:
            try:
                if presentation is not None:
                    presentation.Close()
            except Exception as _e:
                logger.debug("[silent-except] files.py ppt close - %s", _e)
            try:
                if powerpoint is not None:
                    powerpoint.Quit()
            except Exception as _e:
                logger.debug("[silent-except] files.py ppt quit - %s", _e)
            try:
                pythoncom.CoUninitialize()
            except Exception as _e:
                logger.debug("[silent-except] files.py ppt coUninit - %s", _e)
    except Exception as e:
        logger.warning("win32com PPT提取失败: %s", e, exc_info=True)
        return ""


def _ppt_com_child_target(q, file_path):
    """子进程入口：抑制自身控制台输出（含 faulthandler 原生崩溃堆栈），执行提取并回传。

    关键加固（TD-A / 最优容错）：PowerPoint COM 在「注册表残留但无可用二进制」的机器上
    会触发**原生访问违规**，其堆栈由 faulthandler 写向 fd=2。把子进程 fd 1/2 重定向到
    devnull 并禁用 faulthandler，可彻底避免污染解析（父）进程控制台；原生崩溃也只杀死
    子进程，不影响解析主流程（父进程 join 超时 / 收不到结果即返回降级）。
    """
    import faulthandler
    import os

    try:
        faulthandler.disable()
    except Exception:
        pass
    try:
        _devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(_devnull, 1)
        os.dup2(_devnull, 2)
    except Exception:
        pass
    try:
        q.put(_late("_ppt_com_extract_sync")(file_path))
    except BaseException:
        try:
            q.put("")
        except Exception:
            pass


def _ppt_com_terminate(proc) -> None:
    try:
        proc.terminate()
    except Exception:
        pass
    try:
        proc.close()
    except Exception:
        pass


def _ppt_via_powerpoint_com(file_path: str) -> str:
    """把 PowerPoint COM 提取放进**独立子进程**并限时等待（TD-A 最优加固）。

    为什么必须放子进程：COM 调用在损坏文件 / 超大文件 / 弹出模态框时可能永不返回，
    也可能触发**原生访问违规**（线程里会拖垮整个 Python 进程并打印 fatal exception）。
    子进程可被 ``join(timeout)`` 上界约束；超时或原生崩溃都只影响子进程，父（解析）进程
    始终返回 ""（由 :func:`_extract_ppt` 转为降级提示），绝不挂死 / 崩溃 / 污染控制台。

    向后兼容：行为与原线程版一致（超时 / COM 不可用 → 返回 "" → 降级提示）；仅执行载体
    由线程升级为子进程，对「注册表残留但无可用 PowerPoint」的机器额外消除了原生崩溃噪声。
    """
    import multiprocessing as mp

    ctx = mp.get_context("spawn")
    q = ctx.Queue(maxsize=1)
    proc = ctx.Process(target=_ppt_com_child_target, args=(q, file_path), daemon=True)
    proc.start()
    proc.join(timeout=_late("PPT_COM_TIMEOUT"))

    if proc.is_alive():
        logger.error(
            "PowerPoint COM 转换超时（>%ds），放弃等待并返回降级提示: %s",
            _late("PPT_COM_TIMEOUT"), Path(file_path).name,
        )
        _late("_ppt_com_terminate")(proc)
        return ""

    try:
        text = q.get(timeout=1)
    except Exception:
        text = ""
    _late("_ppt_com_terminate")(proc)
    return text or ""


_PPT_OFFICE_PROBE: bool | None = None


def _ppt_com_available() -> bool:
    """探测本进程是否可安全驱动 PowerPoint COM。

    BUG-FIX 2026-10-08（P2 容错）：此前 ``_extract_ppt`` 仅在 ``sys.platform ==
    'win32' and _late("PPT_COM_ENABLED")`` 时尝试 ``win32com.client.Dispatch(
    "PowerPoint.Application")``。在没有安装 Office 的服务器/CI 上，该 Dispatch 会
    触发**原生访问违规**（COM RPC 失败，非 Python 异常），在 daemon 线程里打印
    ``Windows fatal exception: code 0x800706be / 0x800706ba`` 并可能拖垮解析进程，
    而超时护栏无法兜住原生崩溃（只能兜住「挂死」）。

    现加一道**注册表可用探测**：Office 未安装时 ``HKEY_CLASSES_ROOT\\PowerPoint.Application``
    不存在，直接判定不可用、跳过 COM、返回降级提示，彻底避免原生崩溃。开关
    ``_late("PPT_COM_ENABLED")`` 与平台 ``win32`` 在每次调用时实时判断（不进缓存），只有
    昂贵的「pywin32 可导入 + 注册表存在 PowerPoint CLSID」探测结果进程内缓存，
    避免每次解析都重复探测、且不受开关切换影响。
    """
    import sys

    if not _late("PPT_COM_ENABLED") or sys.platform != "win32":
        return False
    global _PPT_OFFICE_PROBE
    if _PPT_OFFICE_PROBE is None:
        try:
            import win32com  # noqa: F401  (仅验证可导入，真正崩溃点在 Dispatch)
        except ImportError:
            _PPT_OFFICE_PROBE = False
        else:
            try:
                import winreg

                _key = winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "PowerPoint.Application")
                winreg.CloseKey(_key)
                _PPT_OFFICE_PROBE = True
            except OSError:
                _PPT_OFFICE_PROBE = False
    if not _PPT_OFFICE_PROBE:
        logger.info("[文件解析] 注册表无 PowerPoint.Application，跳过 COM 提取")
    return bool(_PPT_OFFICE_PROBE)


def _extract_ppt(file_path: str) -> str:
    """提取旧版 .ppt 文件文本（PowerPoint COM，带开关与超时护栏）

    遗留修复 2026-10-06（L1）：
      - ``_late("PPT_COM_ENABLED")=false`` → 直接降级，不再拉起 PowerPoint；
      - ``_late("PPT_COM_TIMEOUT")``       → COM 在 daemon 线程执行并限时等待，超时返回降级
        提示，绝不把上传请求挂死。
    BUG-FIX 2026-10-08（P2）：再加 ``_ppt_com_available`` 注册表探测 —— 未安装
    Office 时直接跳过 COM，避免 Dispatch 触发原生访问违规（见该 helper）。
    向后兼容：默认 ``_late("PPT_COM_ENABLED")=true`` + 120s 超时，非 Windows / 未装
    pywin32 / 未装 Office 时行为与修复前一致（返回同一句降级提示）。
    """
    if _late("_ppt_com_available")():
        text = _late("_ppt_via_powerpoint_com")(file_path)
        if text:
            return text

    return _PPT_FALLBACK_HINT


# PaddleOCR 单例缓存：模型加载开销极大（数秒~数十秒），每次新建实例会重复重载模型，
# 导致图片 OCR 响应极慢且内存持续增长。改为模块级单例复用。
_paddleocr_instance = None
_paddleocr_lock = threading.Lock()

# ENHANCE 2026-10-05（G1 配置未生效 BUG）：OCR_* 配置此前只有 config.py 里的定义，
# 全仓库无任何引用 —— OCR_ENGINE / OCR_MAX_FILE_SIZE_MB / OCR_MAX_CONCURRENT
# 对图片链路完全不生效（图片走硬编码 tesseract→PaddleOCR 顺序，无大小护栏、无并发
# 护栏、无引擎选择）。批量上传扫描件时会同时拉起多个 OCR 进程导致内存暴涨，且用户
# 把 OCR_ENGINE 配成 mineru / none 时也毫无反应。现统一接入：
#   1) OCR_ENGINE=none       → 直接跳过（空文本 + 提示，不中断上传）；
#   2) 超过 OCR_MAX_FILE_SIZE_MB → 跳过并告警（避免超大图拖垮解析进程）；
#   3) OCR_ENGINE=mineru     → 优先 MinerU（复用 MinerUParser 的并发信号量与缓存），
#      MinerU 不可用/无输出再回退 PaddleOCR → tesseract；
#   4) PaddleOCR / tesseract 调用受 _ocr_semaphore（OCR_MAX_CONCURRENT）限流。
# 向后兼容：OCR_ENABLED / OCR_ENGINE 取默认值（true / mineru）时，若本机未装任何
# OCR 引擎，行为与修复前完全一致 —— 优雅降级为空文本，上传仍返回 success。
_ocr_semaphore = threading.Semaphore(max(1, OCR_MAX_CONCURRENT))


def _ocr_image_size_exceeded(file_path: str) -> bool:
    """图片是否超过 OCR 单文件体积上限（_late("OCR_MAX_FILE_SIZE_MB")，默认 50MB）"""
    try:
        size_mb = Path(file_path).stat().st_size / (1024 * 1024)
    except OSError:
        return False
    if size_mb > _late("OCR_MAX_FILE_SIZE_MB"):
        logger.warning(
            "[图片OCR] 文件 %.1fMB 超过 OCR 上限 %dMB，跳过 OCR: %s",
            size_mb, _late("OCR_MAX_FILE_SIZE_MB"), Path(file_path).name,
        )
        return True
    return False


def _get_paddleocr():
    """获取 PaddleOCR 单例实例（线程安全，仅在首次调用时加载模型）"""
    global _paddleocr_instance
    if _paddleocr_instance is None:
        with _paddleocr_lock:
            if _paddleocr_instance is None:
                from paddleocr import PaddleOCR

                _paddleocr_instance = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
    return _paddleocr_instance


def _ocr_with_mineru(file_path: str) -> str:
    """用 MinerU 解析图片（_late("OCR_ENGINE")=mineru 时的首选引擎），不可用/无输出返回空串

    MinerU 侧自带并发信号量（_late("OCR_MAX_CONCURRENT")）与结果缓存（见 ocr_parser），
    因此这里不再额外限流；任何异常都吞掉并返回空串，由调用方继续回退下一引擎。
    """
    try:
        from ..ocr_parser import MinerUParser

        global _mineru_parser_instance
        with _mineru_lock:
            if _mineru_parser_instance is None:
                _mineru_parser_instance = MinerUParser()
        parser = _mineru_parser_instance
    except ImportError:
        logger.debug("[图片OCR] MinerU 模块未安装，跳过该引擎")
        return ""
    except Exception as e:
        logger.warning("[图片OCR] MinerU 初始化失败，跳过该引擎: %s", e)
        return ""

    if not parser.is_available():
        logger.info("[图片OCR] MinerU 不可用，回退下一引擎")
        return ""
    try:
        return (parser.parse_pdf(file_path) or "").strip()
    except Exception as e:
        logger.warning("[图片OCR] MinerU 解析异常，回退下一引擎: %s", e)
        return ""


def _ocr_with_paddle(file_path: str) -> str:
    """用 PaddleOCR 解析图片（复用模块级单例，避免重复重载模型）"""
    try:
        ocr = _late("_get_paddleocr")()
        result = ocr.ocr(file_path, cls=True)
    except ImportError:
        return ""
    except Exception as e:
        logger.warning("PaddleOCR失败: %s", e)
        return ""

    text_parts = []
    lines = result[0] if result and len(result) > 0 and result[0] else []
    for line in lines:
        if line and len(line) >= 2:
            text_parts.append(line[1][0])
    return "\n".join(text_parts)


def _ocr_with_tesseract(file_path: str) -> str:
    """用 tesseract 解析图片"""
    try:
        from PIL import Image

        import pytesseract
    except ImportError:
        return ""
    # BUG-FIX-72: 使用 with 上下文管理器确保 PIL Image 显式关闭，避免文件句柄泄漏
    try:
        with Image.open(file_path) as image:
            return (pytesseract.image_to_string(image, lang="chi_sim+eng") or "").strip()
    except Exception as e:
        logger.warning("tesseract OCR失败: %s", e)
        return ""


def _extract_image_ocr(file_path: str) -> str:
    """使用 OCR 提取图片文本（受 OCR_* 配置约束，逐级回退，绝不中断上传）

    引擎顺序由 _late("OCR_ENGINE") 决定（mineru 默认 / paddleocr / none）：
      ``mineru``   → MinerU → PaddleOCR → tesseract
      ``paddleocr``→ PaddleOCR → tesseract
      ``none``     → 直接跳过（空文本）

    所有引擎都不可用时优雅降级为空文本（与修复前一致），上传仍为 success。
    """
    _t0 = time.monotonic()
    name = Path(file_path).name
    engine = (_late("OCR_ENGINE") or "mineru").strip().lower()
    logger.info("[图片OCR] 开始: %s (engine=%s)", name, engine)

    if engine == "none":
        logger.info("[图片OCR] OCR_ENGINE=none，按配置跳过: %s", name)
        return ""
    if _late("_ocr_image_size_exceeded")(file_path):
        return ""

    text = ""
    try:
        if engine == "mineru":
            text = _late("_ocr_with_mineru")(file_path)
            if not text:
                logger.info("[图片OCR] MinerU 无输出，继续回退: %s", name)

        if not text:
            # 并发护栏：PaddleOCR / tesseract 单次推理内存占用高，限制同时在跑的
            # OCR 数量（_late("OCR_MAX_CONCURRENT")）。取不到信号量时记录告警后放行，
            # 避免极端情况下把上传请求永久挂住。
            # BUG-FIX（2026-10-28 跨模块深度探索 F4）：原实现 `acquired=False` 时
            # **照常执行** OCR（无限流），且与注释「记录告警后放行」相反——没有
            # 记录任何告警日志。后果：高峰期超出 OCR_MAX_CONCURRENT 的并发解析
            # 仍同时拉起 PaddleOCR/tesseract，单次推理高内存放大 OOM 风险。
            # 修复：取不到信号量时打 warning 并跳过本段 OCR（回退到「无 OCR 文本」
            # 的既有优雅降级路径，由上游 save_upload 判空文本提示，不中断上传）。
            acquired = _ocr_semaphore.acquire(timeout=_late("OCR_TIMEOUT") + 60)
            if not acquired:
                logger.warning(
                    "[图片OCR] OCR 并发信号量超时（上限 %d），跳过 OCR 以保护内存: %s | error_code=OCR_CONCURRENCY_SATURATED error_layer=service retryable=False",
                    _late("OCR_MAX_CONCURRENT"),
                    name,
                )
            else:
                try:
                    text = _late("_ocr_with_paddle")(file_path)
                    if not text:
                        text = _late("_ocr_with_tesseract")(file_path)
                finally:
                    _ocr_semaphore.release()
    finally:
        logger.info("[图片OCR] 完成: %s 耗时 %.2fs，提取 %d 字符", name, time.monotonic() - _t0, len(text))

    if not text:
        # 所有OCR库都不可用时，优雅降级：记录警告并返回空文本，
        # 避免因为没有 OCR 引擎而中断整个文件上传流程。
        logger.warning(
            "图片文字识别不可用（OCR_ENGINE=%s，未安装可用引擎或引擎无输出）。"
            "跳过 OCR，文件将以空文本入库。建议安装 OCR 引擎或将图片文字复制到文本文件后重新上传。"
            " | error_code=OCR_UNAVAILABLE error_layer=service retryable=False",
            engine,
        )
    return text


def _extract_html(file_path: str) -> str:
    """提取HTML网页文本内容"""
    import html as html_module
    from html.parser import HTMLParser

    class TextExtractor(HTMLParser):
        def __init__(self):
            super().__init__()
            self.text_parts = []
            self.skip_tags = {"script", "style"}
            self.skip_depth = 0  # 支持嵌套 skip 标签

        def handle_starttag(self, tag, attrs):
            if tag in self.skip_tags:
                self.skip_depth += 1

        def handle_endtag(self, tag):
            if tag in self.skip_tags and self.skip_depth > 0:
                self.skip_depth -= 1
            # BUG-FIX 2026-10-08：畸形 HTML（未闭合标签、标签不匹配）会导致
            # skip_depth 不同步，把后续内容误判为 skip 区域。每次 endtag 后
            # 做一次归零安全检查：若 skip_depth 已为 0 而仍收到 endtag，
            # 说明之前有未闭合的 skip 标签，强制归零恢复同步。
            elif tag in self.skip_tags and self.skip_depth == 0:
                # 不匹配的 endtag（无对应 starttag），忽略，不做任何操作
                pass

        @property
        def skip(self) -> bool:
            return self.skip_depth > 0

        def handle_data(self, data):
            if not self.skip and data.strip():
                # 解码 HTML 实体（如 &amp; -> &, &lt; -> < 等）
                self.text_parts.append(html_module.unescape(data.strip()))

    # BUG-FIX 2026-10-06（R4）：与此前 .txt 漏改同类问题——原实现只试
    # utf-8 → gbk 两档：UTF-16/BOM 存出的 HTML 会乱码或抛异常，gbk 解不出的
    # GB18030 生僻字文件同样失败。统一走 _read_text_auto（BOM 嗅探 →
    # utf-8 → gb18030 → 忽略坏字节兜底），与 .txt/.md 口径一致。
    html_content = _late("_read_text_auto")(file_path)

    extractor = TextExtractor()
    extractor.feed(html_content)

    return "\n".join(extractor.text_parts)


def _extract_wps(file_path: str) -> str:
    """提取WPS文档文本内容"""
    # WPS文档可能是旧版格式(.wps)或新版格式(.et/.dps)
    # 尝试使用olefile提取
    try:
        import olefile

        # BUG-FIX-75: 原代码 ole.open() 后无 finally 关闭，任何异常或正常 return
        # 都会导致 OleFileIO 文件句柄泄漏。参照 _extract_doc 的双层 try 模式修复。
        ole = olefile.OleFileIO(file_path)
        try:
            text_parts = []

            for stream_name in ole.listdir():
                name_lower = "/".join(stream_name).lower()

                # 尝试从各种流中提取文本
                if any(kw in name_lower for kw in ["document", "text", "content"]):
                    try:
                        data = ole.openstream(stream_name).read()
                        raw = data.decode("utf-16-le", errors="replace")
                        clean = "".join(c for c in raw if (c.isprintable() and c != '\ufffd') or c in "\n\r\t")
                        if clean.strip():
                            text_parts.append(clean)
                    except Exception as _e:
                        logger.debug("[silent-except] files.py: line 865 - %s", _e)

            if text_parts:
                return "\n".join(text_parts).strip()
        finally:
            ole.close()
    except ImportError:
        logger.warning("olefile未安装，无法提取WPS文档")
    except Exception:
        logger.warning("olefile提取WPS失败", exc_info=True)

    # 尝试作为DOCX处理（新版WPS可能保存为兼容格式）
    try:
        from docx import Document

        doc = Document(file_path)
        return "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    except Exception:
        logger.warning("docx提取WPS失败", exc_info=True)

    return EXTRACT_WPS_NOTICE


def _xls_cell_value(cell: "Any") -> str:
    """将 xlrd 单元格对象转换为字符串"""
    cell_type = cell.ctype
    value = cell.value

    if cell_type == 0:  # XL_CELL_EMPTY
        return ""
    elif cell_type == 1:  # XL_CELL_TEXT
        return str(value).strip()
    elif cell_type == 2:  # XL_CELL_NUMBER
        if value == int(value):
            return str(int(value))
        return str(value)
    elif cell_type == 3:  # XL_CELL_DATE
        try:
            import xlrd

            date_tuple = xlrd.xldate_as_tuple(value, 0)
            return f"{date_tuple[0]}-{date_tuple[1]:02d}-{date_tuple[2]:02d}"
        except Exception:
            return str(value)
    elif cell_type == 4:  # XL_CELL_BOOLEAN
        return "是" if value else "否"
    elif cell_type == 5:  # XL_CELL_ERROR
        return f"[错误码: {value}]"
    else:
        return str(value).strip()
