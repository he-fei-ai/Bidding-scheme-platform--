"""Word 文档提取工具模块

封装 python-docx 主引擎 + docx2python 回退引擎的双层提取逻辑，
供 files.py 及其他模块复用。

使用方式：
    from app.services.docx_extractor import extract_docx, extract_docx_with_fallback
"""

import gc
import logging


logger = logging.getLogger(__name__)


def is_docx2python_available() -> bool:
    """检查 docx2python 是否已安装"""
    try:
        import docx2python  # noqa: F401

        return True
    except ImportError:
        return False


def extract_docx_plain(file_path: str) -> str:
    """使用 python-docx 提取 Word 文本（主引擎）

    Args:
        file_path: Word 文件路径

    Returns:
        提取的文本，段落与表格混合输出

    Raises:
        ImportError: python-docx 未安装
    """
    try:
        from docx import Document
    except ImportError:
        raise ImportError("需要安装 python-docx: pip install python-docx")

    doc = Document(file_path)
    try:
        paragraphs = []
        table_count = 0
        for para in doc.paragraphs:
            text = para.text.strip()
            if text:
                paragraphs.append(text)

        for table in doc.tables:
            table_count += 1
            paragraphs.append("\n[表格内容]")
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells]
                paragraphs.append(" | ".join(cells))
            paragraphs.append("[表格结束]")

        result = "\n".join(paragraphs)
    except Exception:
        # 引入软件标准（标书智能体（一））：资源释放在 try 与 except 两处，避免异常时句柄泄漏。
        # 异常路径同样删除文档对象并立即 gc，防止 zip 文件句柄（Document 内部持有）堆积。
        del doc
        gc.collect()
        raise
    # 正常路径释放（与 except 分支口径一致；仅此两处覆盖全部路径，不依赖 finally）。
    # BUG-FIX 2026-10-08：此前用 doc.part._element.clear() 依赖 python-docx 内部私有 API，
    # 版本升级时可能失效，故改为直接删除文档对象让 GC 回收。
    del doc
    gc.collect()
    return result


def extract_docx_docx2python(file_path: str) -> str:
    """使用 docx2python 提取 Word 文本（增强表格结构解析，回退引擎）

    Args:
        file_path: Word 文件路径

    Returns:
        提取的文本，若无 docx2python 或解析失败则返回空字符串
    """
    try:
        from docx2python import docx2python
    except ImportError:
        logger.warning("[docx_extractor] docx2python 未安装，跳过回退提取")
        return ""

    # doc 必须先初始化为 None：若 docx2python(file_path) 抛异常，doc 未绑定，
    # 下方 except 分支的 `if doc is not None` 守卫可避免 NameError 掩盖原始异常
    # （与 ai provider 的 raw_content 同类问题）。
    doc = None
    try:
        doc = docx2python(file_path)
        parts = []

        # docx2python.body 结构: [sections][elements]
        # 每个 element 可能是段落(run列表)或表格(行列表,每行是单元格列表)
        for section in doc.body:
            for element in section:
                if not isinstance(element, list):
                    text = str(element).strip()
                    if text:
                        parts.append(text)
                    continue

                if not element:
                    continue

                first_item = element[0]
                is_table = isinstance(first_item, list)

                if is_table:
                    parts.append("[表格内容]")
                    for row in element:
                        cells = []
                        for cell in row if isinstance(row, list) else [row]:
                            if isinstance(cell, list):
                                cell_text = " ".join(
                                    str(r).strip() for r in cell if r and str(r).strip()
                                )
                            else:
                                cell_text = str(cell).strip()
                            cells.append(cell_text)
                        parts.append(" | ".join(cells))
                    parts.append("[表格结束]")
                else:
                    text = " ".join(str(run).strip() for run in element if run and str(run).strip())
                    if text:
                        parts.append(text)

        result = "\n".join(parts)
    except Exception as e:
        # 引入软件标准（标书智能体（一））：资源释放在 try 与 except 两处。
        # 异常路径同样释放 docx2python 持有的文档资源，避免句柄泄漏。
        if doc is not None:
            del doc
            gc.collect()
        logger.warning("[docx_extractor] docx2python 解析失败: %s", e)
        return ""
    # 正常路径释放（与 except 分支口径一致；仅此两处覆盖全部路径，不依赖 finally）。
    del doc
    gc.collect()
    return result


extract_docx = extract_docx_plain


def extract_docx_with_fallback(file_path: str) -> str:
    """提取 Word 文档文本（python-docx 主引擎 + docx2python 回退增强）

    逻辑：
    1. 先用 python-docx 提取
    2. 若提取结果不足 50 字符或无表格，尝试 docx2python 回退
    3. 仅当 docx2python 提取出更多字符时才替换结果

    Args:
        file_path: Word 文件路径

    Returns:
        提取的文本（优先使用 quality 更高的引擎结果）
    """
    file_path = str(file_path)

    try:
        result = extract_docx_plain(file_path)
    except Exception:
        # python-docx 失败，直接尝试 docx2python
        fallback = extract_docx_docx2python(file_path)
        if fallback:
            logger.info(
                "[docx_extractor] python-docx 失败，使用 docx2python 提取 %d 字符",
                len(fallback.strip()),
            )
            return fallback
        raise

    # python-docx 成功，判断是否需要回退
    # BUG 修复：原条件 `len < 50 or 无表格` 会对纯文本文档（无表格但字符多）
    # 也触发 docx2python 回退，而 docx2python 同样提取不到表格，白白多解析一次
    # 整份文档（大文档可达数秒）。回退应仅在"提取质量明显不足"时触发：
    # 字符极少（<50）即可判定质量不足；表格缺失但文本充足属正常纯文本文档。
    if len(result.strip()) < 50:
        fallback = extract_docx_docx2python(file_path)
        if fallback and len(fallback.strip()) > len(result.strip()):
            logger.info(
                "[docx_extractor] docx2python 回退生效: %d 字符（python-docx 仅 %d 字符）",
                len(fallback.strip()),
                len(result.strip()),
            )
            return fallback

    return result
