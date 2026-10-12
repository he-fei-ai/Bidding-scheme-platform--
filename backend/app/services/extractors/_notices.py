# -*- coding: utf-8 -*-
"""R-1 2026-10-12：老格式降级提示串登记表（从 services/files.py 原样搬迁）。

独立成中立模块是为了**破除 files ↔ extractors 的循环导入**：files.py 顶部要
`from .extractors import ...` 回填 re-export，而提取器又需要这些常量；若留在
files.py，extractors 就得在模块级 import files，两模块加载期互相依赖必然成环。
下放后两边都只依赖本模块，依赖图变为单向。内容与搬迁前逐字一致。
"""

EXTRACT_DOC_NOTICE = "[提示] 当前系统无法自动提取此 .doc 文件文本，建议转换为 .docx 后重新上传"
EXTRACT_PPT_NOTICE = "[提示] 当前系统无法自动提取此 .ppt 文件文本，建议转换为 .pptx 后重新上传"
EXTRACT_WPS_NOTICE = "[提示] 当前系统无法自动提取此 .wps 文件文本，建议使用WPS打开后另存为 .docx 格式再上传。"

EXTRACTION_NOTICES: frozenset[str] = frozenset(
    {EXTRACT_DOC_NOTICE, EXTRACT_PPT_NOTICE, EXTRACT_WPS_NOTICE}
)

# BUG-FIX 2026-10-11（I-6）：Excel/PPT 分支的降级返回值是 **f-string 动态拼接**
# （`f"[Excel 解析错误] {e}"`），无法纳入上面的 frozenset 精确登记 —— 于是
# `is_extraction_notice` 对它返回 False，save_upload / Celery parse_and_extract_file
# / latest_extracted_text 全部按「提取成功」处理，损坏的 .xlsx/.pptx 的错误文本
# 被写成 status='success'、缓存下来，并喂给下游评分/目录/正文三段 AI。
#
# 处理策略：登记表仍只收「完整常量串」，动态串单独走**前缀匹配**：
#   - 前缀是本模块写死的固定子串（如 "[Excel 解析错误]"），真实标书正文不可能
#     以 `"[Excel 解析错误] "` 起头（连中括号 + 中文 + 方括号 + 空格都齐了），
#     故误判概率极低；
#   - 叠加**长度上界**（120 字符）作二重防御：真实的 Excel 表格导出 / 招标正文
#     不可能只有几十个字符，而错误串是「前缀 + 一个 str(e)」，天然很短。
# 前缀必须两两不互相前缀（如 "[Excel 文件不存在]" 不能被误判为 "[Excel 文件]"），
# 由 test_extraction_notice_prefixes_are_exact 守卫。
EXTRACT_ERROR_PREFIXES: frozenset[str] = frozenset({
    "[Excel 解析错误]",
    "[Excel 文件损坏]",
    "[Excel 文件不存在]",
    "[Excel 权限不足]",
    "[PPTX 解析错误]",
})
EXTRACT_ERROR_MAX_LEN = 120  # 错误串 = 前缀 + str(e)，正常不超过这个量级

# 精确登记表 + 前缀登记表，合并供 is_extraction_notice 判定。
_EXTRACT_ERROR_PREFIXES_TUPLED = tuple(EXTRACT_ERROR_PREFIXES)


def is_extraction_notice(text: str | None) -> bool:
    """判断提取结果是否为「无法提取 / 解析失败」的降级提示串（而非真实正文）。

    两路判定：
      1. 精确匹配 EXTRACTION_NOTICES（.doc/.ppt/.wps 三条写死的常量串）；
      2. 前缀匹配 EXTRACT_ERROR_PREFIXES 且总长度 ≤ EXTRACT_ERROR_MAX_LEN
         （.xlsx/.xls/.pptx 分支的 f-string 错误串）。

    供 save_upload、Celery 解析任务与 latest_extracted_text 共用，保证
    「上传 / 异步重解析 / 合并文本」三条路径口径一致。
    """
    if not text:
        return False
    stripped = text.strip()
    if stripped in EXTRACTION_NOTICES:
        return True
    if len(stripped) > EXTRACT_ERROR_MAX_LEN:
        return False
    # 前缀后必须紧跟空白或字符串结尾，避免 "[Excel 解析错误]" 误吃
    # "[Excel 解析错误xxx真实正文]" 这类伪造输入。
    return any(
        stripped.startswith(pref) and (
            len(stripped) == len(pref) or stripped[len(pref)] in (" ", "\t", "\n", ":")
        )
        for pref in _EXTRACT_ERROR_PREFIXES_TUPLED
    )
