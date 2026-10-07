"""标书查重服务层

实现四维度检测：
1. 元数据：文件属性对比 + **文档同源指纹**（Word RSID / PDF 结构 / WPS 痕迹）
2. 目录：**覆盖率 + 顺序相似度综合评分**（含风险分级与近义标题识别）
3. 正文：倒排索引 + 相似度
4. 图片：SHA-256 哈希

特性：
- 签名缓存机制
- 招标文件白名单过滤
- 并发执行

ENHANCE 2026-09-14：维度 1、2 升级为易标 `duplicateCheckService.cjs` 的算法体系。
"""

import asyncio
from datetime import UTC, datetime
import hashlib
import json
import logging
from pathlib import Path
import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile

from ..config import DUPLICATE_REPORTS_DIR
from ..db import get_db
from . import check_report_excel as _excel_report

logger = logging.getLogger(__name__)


def _report_text(value, fallback: str = "") -> str:
    """报告写入前的统一兜底：None→空串、非 str→str、并清洗 XML 不安全字符。

    BUG-FIX（P2 整份报告 500）：原实现大量使用 `a['description']` / `g['sentence']`
    这类硬下标取值，历史结果 JSON 里任一字段缺失或为数字就会 KeyError/TypeError，
    整份 Word 报告直接 500；且写入 docx 前未做控制字符清洗，句子带 \\x0b 会让
    python-docx 抛 `All strings must be XML compatible`。与 Excel 侧的
    `cell_text()` 统一口径。
    """
    if value is None:
        return fallback
    if not isinstance(value, str):
        value = str(value)
    return _XML_UNSAFE_RE.sub("", value) or fallback


#: XML 1.0 非法字符（与 export.sanitize_for_docx 同口径）
_XML_UNSAFE_RE = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff\ufffe\uffff]"
)


def _report_join(values, sep: str = ", ") -> str:
    """把列表安全地拼成报告文本（元素可能不是字符串）。"""
    if not values:
        return ""
    if isinstance(values, str):
        return _report_text(values)
    parts = []
    for v in values:
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, (list, tuple, dict)):
            parts.append(json.dumps(v, ensure_ascii=False))
        else:
            parts.append(str(v))
    return _report_text(sep.join(parts))


logger = logging.getLogger(__name__)


# ========== 任务管理 ==========


def create_task(file_ids: list[int], tender_file_id: int | None = None, project_id: int | None = None) -> tuple[int, bool]:
    """创建查重任务，返回 (task_id, is_cached)

    BUG-FIX 2026-09-18（P1）：新增 project_id 归属——原表无项目列，任务全站
    混排，任意登录用户可浏览/猜 ID 读取他人查重结果。缓存命中也按项目隔离。
    """
    signature = _create_signature(file_ids, tender_file_id)

    # 检查缓存
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id, status FROM duplicate_checks WHERE signature = ? AND project_id IS ? AND status = 'completed' ORDER BY id DESC LIMIT 1",
            (signature, project_id),
        ).fetchone()
        if existing:
            return existing["id"], True

        cursor = conn.execute(
            """INSERT INTO duplicate_checks (project_id, file_ids, tender_file_id, signature, status, progress)
               VALUES (?, ?, ?, ?, 'pending', 0)""",
            (project_id, json.dumps(file_ids), tender_file_id, signature),
        )
        return cursor.lastrowid, False


def _create_signature(file_ids: list[int], tender_file_id: int | None) -> str:
    """生成任务签名"""
    sorted_ids = sorted(file_ids)
    parts = [str(i) for i in sorted_ids]
    if tender_file_id:
        parts.append(f"tender:{tender_file_id}")
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def update_task_progress(task_id: int, progress: int, message: str = ""):
    with get_db() as conn:
        conn.execute(
            "UPDATE duplicate_checks SET progress = ?, progress_message = ? WHERE id = ?",
            (progress, message, task_id),
        )


def update_task_status(task_id: int, status: str, results: dict | None = None, error: str = ""):
    with get_db() as conn:
        if results is not None:
            conn.execute(
                "UPDATE duplicate_checks SET status = ?, results = ?, error = ? WHERE id = ?",
                (status, json.dumps(results, ensure_ascii=False), error, task_id),
            )
        else:
            conn.execute(
                "UPDATE duplicate_checks SET status = ?, error = ? WHERE id = ?",
                (status, error, task_id),
            )


def get_task(task_id: int) -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM duplicate_checks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return None
        return _row_to_dict(row)


def list_tasks(limit: int = 20, project_id: int | None = None, *, readonly: bool = False) -> list[dict]:
    """列出查重任务。

    PERF（2026-10-06）：纯读场景新增 `readonly` 开关（默认 False = 保持既有写锁
    连接口径，**向后兼容**）。路由层为纯读端点，应传 readonly=True 并放入线程池，
    避免裸跑在事件循环里抢占全局写锁。
    """
    with get_db(readonly=readonly) as conn:
        # P1：按项目过滤（项目上下文里只看得到本项目的查重任务）
        if project_id is not None:
            rows = conn.execute(
                "SELECT * FROM duplicate_checks WHERE project_id = ? ORDER BY id DESC LIMIT ?",
                (project_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM duplicate_checks ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]


def _row_to_dict(row) -> dict:
    d = dict(row)
    for k in ("file_ids", "results"):
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except json.JSONDecodeError:
                d[k] = [] if k == "file_ids" else {}
        else:
            d[k] = [] if k == "file_ids" else {}
    return d


# ========== 文件信息加载 ==========


def _load_file_info(file_id: int) -> dict:
    """从 uploaded_files 表加载文件信息"""
    with get_db() as conn:
        row = conn.execute("SELECT * FROM uploaded_files WHERE id = ?", (file_id,)).fetchone()
        if not row:
            raise ValueError(f"文件 ID {file_id} 不存在")
        d = dict(row)
        text = ""
        if d.get("extracted_text_path"):
            p = Path(d["extracted_text_path"])
            if p.exists():
                text = p.read_text(encoding="utf-8")
        d["text"] = text
        d["file_id"] = d["id"]
        return d


# ========== 维度1：元数据提取 ==========


def extract_metadata(file_info: dict) -> dict:
    """提取文件元数据（作者、创建时间、修改时间等）"""
    file_path = file_info.get("path", "")
    file_type = file_info.get("file_type", "").lower()

    metadata = {
        "file_id": file_info["file_id"],
        "filename": file_info.get("filename", ""),
        "author": "",
        "created": "",
        "modified": "",
        "company": "",
        "last_saved_by": "",
        "title": "",
    }

    if not file_path or not Path(file_path).exists():
        return metadata

    try:
        if file_type == "docx" or file_path.endswith(".docx"):
            return _extract_docx_metadata(file_path, metadata)
        elif file_type == "pdf" or file_path.endswith(".pdf"):
            return _extract_pdf_metadata(file_path, metadata)
    except Exception:
        logger.warning("元数据提取失败(extract_metadata)", exc_info=True)

    return metadata


def _extract_docx_metadata(file_path: str, metadata: dict) -> dict:
    """从 docx 提取元数据"""
    try:
        from docx import Document

        doc = Document(file_path)
        cp = doc.core_properties
        metadata["author"] = cp.author or ""
        metadata["created"] = cp.created.strftime("%Y-%m-%d %H:%M:%S") if cp.created else ""
        metadata["modified"] = cp.modified.strftime("%Y-%m-%d %H:%M:%S") if cp.modified else ""
        metadata["company"] = getattr(cp, "category", "") or ""
        metadata["last_saved_by"] = cp.last_modified_by or ""
        metadata["title"] = cp.title or ""
    except Exception:
        logger.warning("docx元数据提取失败", exc_info=True)
    return metadata


def _extract_pdf_metadata(file_path: str, metadata: dict) -> dict:
    """从 PDF 提取元数据"""
    try:
        from pypdf import PdfReader

        reader = PdfReader(file_path)
        meta = reader.metadata or {}
        metadata["author"] = str(meta.get("/Author", "") or "")
        metadata["created"] = str(meta.get("/CreationDate", "") or "")
        metadata["modified"] = str(meta.get("/ModDate", "") or "")
        metadata["company"] = str(meta.get("/Creator", "") or "")
        metadata["last_saved_by"] = str(meta.get("/Producer", "") or "")
        metadata["title"] = str(meta.get("/Title", "") or "")
    except Exception:
        logger.warning("pdf元数据提取失败", exc_info=True)
    return metadata


def analyze_metadata(file_infos: list[dict]) -> dict:
    """分析元数据，识别异常项，并做「文档同源」指纹比对。

    ENHANCE 2026-09-14：移植自易标 `duplicateCheckService.cjs::extractMetadata`
    的指纹体系（word_rsid_* / ole_* / pdf_* / wps 信号）。

    原实现只看作者/公司是否相同，无法回答"两份标书是否出自同一台电脑/同一账号"。
    而围标串标的实务认定，往往正是靠这类**文件内部痕迹**：
      - Word 的 RSID（编辑会话 ID）：同一份底稿派生出的文档会共享 rsidRoot
      - PDF 增量保存次数 / Trailer ID / 签名数量
      - 文档里残留的 WPS/金山账号、邮箱等字符串
    这些证据在评标质疑时比"文字相似"更硬。
    """
    metadatas = [extract_metadata(f) for f in file_infos]

    # 异常项检测：相同作者、相同公司
    authors = [m["author"] for m in metadatas if m["author"]]
    companies = [m["company"] for m in metadatas if m["company"]]

    anomalies = []
    if len(authors) > 1 and len(set(authors)) < len(authors):
        anomalies.append(
            {
                "type": "author_duplicate",
                "description": "多个文件作者相同",
                "value": list(set(authors)),
            },
        )
    if len(companies) > 1 and len(set(companies)) < len(companies):
        anomalies.append(
            {
                "type": "company_duplicate",
                "description": "多个文件公司相同",
                "value": list(set(companies)),
            },
        )

    # ===== ENHANCE 2026-09-14：同源指纹比对 =====
    fingerprints: list[dict] = []
    for info, meta in zip(file_infos, metadatas, strict=False):
        try:
            fp = extract_file_fingerprints(info)
        except Exception:
            logger.warning("文件指纹提取失败: %s", info.get("filename"), exc_info=True)
            fp = {}
        meta["fingerprints"] = fp
        fingerprints.append(fp)

    same_source_pairs = compare_same_source(file_infos, fingerprints)
    for pair in same_source_pairs:
        anomalies.append(
            {
                "type": "same_source",
                "description": (
                    f"{pair['file_a']} 与 {pair['file_b']} 存在同源痕迹"
                    f"（{ '、'.join(pair['evidence']) }），建议人工核实是否共用底稿"
                ),
                "value": pair["evidence"],
                "severity": pair["severity"],
            },
        )

    return {
        "metadata": metadatas,
        "anomalies": anomalies,
        "fingerprints": fingerprints,
        "same_source_pairs": same_source_pairs,
    }


# ========== 文档同源指纹（ENHANCE 2026-09-14） ==========

# WPS/金山痕迹与账号类字符串（对应易标 rawSignalPattern）
_WPS_SIGNAL_RE = re.compile(
    r"(kingsoft|wps office|\bwps\b|\bkso\b|account|e-?mail|mail|userid|user id|user_id|uid|账号|金山)",
    re.IGNORECASE,
)

# Word RSID 属性：rsidRoot / rsidR / rsidRPr / rsidDel / rsidP ...
_RSID_RE = re.compile(rb"\b(?:[A-Za-z0-9_]+:)?(rsid[A-Za-z0-9]*)=\"([0-9A-Fa-f]{1,16})\"")


def _sha256_short(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _collect_wps_signals(*sources: str, limit: int = 12) -> list[str]:
    """从若干文本中抽取 WPS/账号类痕迹片段（去重、限量）"""
    snippets: list[str] = []
    seen: set[str] = set()
    for source in sources:
        text = str(source or "")
        if not text:
            continue
        for match in _WPS_SIGNAL_RE.finditer(text):
            start = max(0, match.start() - 20)
            end = min(len(text), match.end() + 20)
            snippet = text[start:end].strip()
            key = snippet.lower()
            if snippet and key not in seen:
                seen.add(key)
                snippets.append(snippet)
                if len(snippets) >= limit:
                    return snippets
    return snippets


def _extract_docx_fingerprints(file_path: str) -> dict:
    """DOCX 指纹：Word 编辑会话 RSID + docProps 里的应用/公司痕迹"""
    fp: dict = {}
    rsid_values: set[str] = set()
    rsid_root = ""
    app_texts: list[str] = []

    with zipfile.ZipFile(file_path) as zf:
        names = zf.namelist()
        # RSID 只出现在 word/*.xml（document.xml / styles.xml / settings.xml 等）
        for name in names:
            if not (name.startswith("word/") and name.endswith(".xml")):
                continue
            try:
                data = zf.read(name)
            except (KeyError, OSError):
                continue
            for match in _RSID_RE.finditer(data):
                attr = match.group(1).decode("ascii", errors="ignore").lower()
                value = match.group(2).decode("ascii", errors="ignore").upper()
                if not value:
                    continue
                rsid_values.add(value)
                if attr == "rsidroot" and not rsid_root:
                    rsid_root = value
        # docProps 作为 WPS/应用痕迹的来源
        for name in ("docProps/core.xml", "docProps/app.xml", "docProps/custom.xml"):
            if name in names:
                try:
                    app_texts.append(zf.read(name).decode("utf-8", errors="ignore"))
                except (KeyError, OSError):
                    continue

    if rsid_root:
        fp["word_rsid_root"] = rsid_root
    if rsid_values:
        ordered = sorted(rsid_values)
        fp["word_rsid_count"] = len(ordered)
        fp["word_rsid_fingerprint"] = _sha256_short("\n".join(ordered))
    signals = _collect_wps_signals(*app_texts)
    if signals:
        fp["wps_signals"] = signals
    return fp


def _extract_pdf_fingerprints(file_path: str) -> dict:
    """PDF 指纹：头版本 / 对象数 / 增量保存次数 / 线性化 / Trailer ID / 签名 / 附件"""
    fp: dict = {}
    try:
        raw = Path(file_path).read_bytes()
    except OSError:
        return fp

    # PDF 的交叉引用表是二进制安全的，但结构关键字都是 ASCII，用 latin-1 逐字节映射最稳
    text = raw.decode("latin-1", errors="ignore")

    header = re.search(r"%PDF-(\d\.\d)", text[:1024])
    if header:
        fp["pdf_header_version"] = header.group(1)
    object_count = len(re.findall(r"(?:^|[\r\n])\s*\d+\s+\d+\s+obj\b", text))
    startxref_count = len(re.findall(r"(?:^|[\r\n])startxref\b", text))
    if object_count:
        fp["pdf_object_count"] = object_count
    fp["pdf_incremental_update_count"] = max(0, startxref_count - 1)
    fp["pdf_linearized"] = bool(re.search(r"/Linearized\s+\d", text[:4096]))
    trailer_ids = re.findall(r"/ID\s*\[\s*<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>", text)
    if trailer_ids:
        fp["pdf_trailer_id"] = trailer_ids[0][0][:32]
    byte_range_count = len(re.findall(r"/ByteRange\s*\[", text))
    # 注意：`\S` 是合法正则类，写成 "/\Sig" 会被解析成「非空白字符 + ig」而静默失配；
    # 同理 `\E` 是非法转义会直接抛 PatternError。这里必须让 / 与关键字直接相邻。
    sig_field_count = len(re.findall(r"/FT\s*/Sig\b", text))
    sig_obj_count = len(re.findall(r"/Type\s*/Sig\b", text))
    fp["pdf_signature_count"] = max(byte_range_count, sig_field_count, sig_obj_count)
    fp["pdf_embedded_file_count"] = len(re.findall(r"/Type\s*/EmbeddedFile\b", text))
    return fp


def extract_file_fingerprints(file_info: dict) -> dict:
    """按文件类型分派指纹提取；失败时返回空 dict（不阻断查重主流程）"""
    file_path = file_info.get("path", "")
    file_type = str(file_info.get("file_type", "")).lower()
    if not file_path or not Path(file_path).exists():
        return {}
    try:
        if file_type == "docx" or file_path.lower().endswith(".docx"):
            return _extract_docx_fingerprints(file_path)
        if file_type == "pdf" or file_path.lower().endswith(".pdf"):
            return _extract_pdf_fingerprints(file_path)
    except Exception:
        logger.warning("文件指纹提取异常: %s", file_info.get("filename"), exc_info=True)
    return {}


# 同源判定规则：(指纹字段, 说明, 严重度)
_SAME_SOURCE_RULES: list[tuple[str, str, str]] = [
    ("word_rsid_root", "Word 编辑会话根 ID 相同", "high"),
    ("word_rsid_fingerprint", "Word 编辑会话指纹完全相同", "high"),
    ("pdf_trailer_id", "PDF Trailer ID 相同", "high"),
]


def compare_same_source(file_infos: list[dict], fingerprints: list[dict]) -> list[dict]:
    """两两比对各文件指纹，输出「同源」证据对。

    判定依据（命中任一即计入证据）：
      - Word RSID 根 ID / 整体指纹相同 → 同一份底稿派生（high）
      - PDF Trailer ID 相同 → 同一源文件另存（high）
      - WPS/账号痕迹交集非空 → 同一账号或同一办公环境处理过（medium）

    注意：指纹相同**不等于**一定串标（例如同一投标人不同标段本就同源），
    所以只作为"建议人工核实"的线索输出，不做自动定性。
    """
    pairs: list[dict] = []
    n = len(file_infos)
    for i in range(n):
        for j in range(i + 1, n):
            fp_a, fp_b = fingerprints[i] or {}, fingerprints[j] or {}
            if not fp_a or not fp_b:
                continue
            evidence: list[str] = []
            severity = "low"
            for field, label, level in _SAME_SOURCE_RULES:
                value_a, value_b = fp_a.get(field), fp_b.get(field)
                if value_a and value_a == value_b:
                    evidence.append(f"{label}（{str(value_a)[:16]}）")
                    if level == "high":
                        severity = "high"
                    elif severity != "high":
                        severity = "medium"
            shared_signals = set(fp_a.get("wps_signals") or []) & set(fp_b.get("wps_signals") or [])
            if shared_signals:
                evidence.append(f"共同的 WPS/账号痕迹 {len(shared_signals)} 处")
                if severity == "low":
                    severity = "medium"
            if evidence:
                pairs.append(
                    {
                        "file_a": file_infos[i].get("filename", ""),
                        "file_b": file_infos[j].get("filename", ""),
                        "evidence": evidence,
                        "severity": severity,
                    },
                )
    return pairs


# ========== 维度2：目录提取 ==========


def extract_toc(text: str) -> list[str]:
    """提取目录标题结构"""
    titles = []

    # 优先识别 "第X章" / "第X节" 模式
    chapter_pattern = re.compile(
        r"^第[一二三四五六七八九十百零\d]+[章节部分篇][\s、\.．]?\s*(.+)$",
        re.MULTILINE,
    )
    for m in chapter_pattern.finditer(text):
        titles.append(m.group(0).strip())

    # 识别 Markdown 标题
    md_pattern = re.compile(r"^#{1,6}\s+(.+)$", re.MULTILINE)
    for m in md_pattern.finditer(text):
        titles.append(m.group(0).strip())

    # 识别中文数字编号 "一、" "二、" 等
    cn_pattern = re.compile(r"^[一二三四五六七八九十百零]+[、．]\s*(.+)$", re.MULTILINE)
    for m in cn_pattern.finditer(text):
        titles.append(m.group(0).strip())

    # 识别纯数字编号 "1、" "2、" 等
    num_dun_pattern = re.compile(r"^(\d+)\s*[、．]\s*(.+)$", re.MULTILINE)
    for m in num_dun_pattern.finditer(text):
        titles.append(m.group(0).strip())

    # 识别数字编号标题 "1. xxx" "1.1 xxx"
    num_pattern = re.compile(r"^(\d+(\.\d+){1,3})[\s\.\、]\s*(.+)$", re.MULTILINE)
    for m in num_pattern.finditer(text):
        titles.append(m.group(0).strip())

    # 识别括号编号 "1)" "2)" 等
    bracket_pattern = re.compile(r"^(\d+)\s*[)）]\s*(.+)$", re.MULTILINE)
    for m in bracket_pattern.finditer(text):
        titles.append(m.group(0).strip())

    # 识别字母编号 "a、" "b、" 等
    letter_pattern = re.compile(r"^[a-z]\s*[、．.]\s*(.+)$", re.MULTILINE)
    for m in letter_pattern.finditer(text):
        titles.append(m.group(0).strip())

    return titles


def jaccard_similarity(set_a: set, set_b: set) -> float:
    """Jaccard 相似度"""
    if not set_a and not set_b:
        return 0
    union = set_a | set_b
    if not union:
        return 0
    return len(set_a & set_b) / len(union)


# ===== ENHANCE 2026-09-14：目录结构相似度算法（移植自易标 buildOutlineComparison）=====

# 标题编号/装饰前缀：第X章、一、、1.1、1)、（一）、a) 等
_TITLE_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"第[一二三四五六七八九十百零\d]+[章节部分篇条]"
    r"|[（(]?[一二三四五六七八九十百零]+[)）]?"
    r"|[（(]?[a-zA-Z][)）]?"
    r"|[（(]?\d+(?:\.\d+)*[)）]?"
    r")\s*[、\.．,:：]?\s*"
)


def normalize_outline_title(title: str) -> str:
    """目录标题归一化：去编号前缀、Markdown 记号、标点与空白，统一大小写。

    例："## 1.2 施工组织设计" → "施工组织设计"
    """
    text = str(title or "").strip()
    text = re.sub(r"^#{1,6}\s*", "", text)          # Markdown 井号
    text = _TITLE_PREFIX_RE.sub("", text)            # 编号前缀
    text = re.sub(r"[\s\W_]+", "", text)             # 标点/空白（\W 保留中日韩字符）
    return text.lower()


def bigram_dice_similarity(a: str, b: str) -> float:
    """字符二元组 Dice 系数（用于识别「近义/微调过」的标题）。

    相比编辑距离，二元组 Dice 对中文短标题更稳健：
      "施工组织设计" vs "施工组织设计方案" → 0.86（判定为近似）
      "施工组织设计" vs "安全管理体系"     → 0.0
    """
    left, right = str(a or ""), str(b or "")
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0

    def _bigrams(value: str) -> set[str]:
        chars = list(value)
        if len(chars) <= 1:
            return set(chars)
        return {f"{chars[i]}{chars[i + 1]}" for i in range(len(chars) - 1)}

    left_set, right_set = _bigrams(left), _bigrams(right)
    shared = len(left_set & right_set)
    denominator = len(left_set) + len(right_set)
    return (2 * shared) / (denominator or 1)


def lcs_similarity(left: list[str], right: list[str]) -> float:
    """最长公共子序列占比（衡量「目录顺序」是否一致）。

    两份标书目录标题可能完全相同，但顺序不同——顺序一致才说明是同一套模板。
    """
    if not left or not right:
        return 0.0
    n, m = len(left), len(right)
    # 滚动数组：目录最多几百项，仍做内存优化避免大文档 O(n*m) 内存
    prev = [0] * (m + 1)
    for i in range(1, n + 1):
        curr = [0] * (m + 1)
        left_item = left[i - 1]
        for j in range(1, m + 1):
            if left_item == right[j - 1]:
                curr[j] = prev[j - 1] + 1
            else:
                curr[j] = max(prev[j], curr[j - 1])
        prev = curr
    return prev[m] / max(n, m)


def outline_risk_from_score(score: float) -> str:
    """相似度 → 风险分级（阈值沿用易标：0.75/0.55/0.35）"""
    if score >= 0.75:
        return "high"
    if score >= 0.55:
        return "medium"
    if score >= 0.35:
        return "low"
    return "none"


# 近义标题判定阈值（易标同值）
_SIMILAR_TITLE_THRESHOLD = 0.86
# 增强判据：短标题下 Dice 天然偏低（如「施工组织设计」vs「施工组织设计方案」
# 只有 0.833），会漏掉大量"加两个字换个说法"的改写。因此补充「二元组包含度」判据：
# 一方的大多数二元组被另一方完整包含，且长度接近时，视为近义。
_SIMILAR_CONTAINMENT_THRESHOLD = 0.95
_SIMILAR_LENGTH_DELTA = 4


def _bigram_containment(a: str, b: str) -> float:
    """二元组包含度 = |交集| / min(|A|,|B|)，衡量"短标题是否被长标题覆盖"。"""
    left, right = str(a or ""), str(b or "")
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0

    def _bigrams(value: str) -> set[str]:
        chars = list(value)
        if len(chars) <= 1:
            return set(chars)
        return {f"{chars[i]}{chars[i + 1]}" for i in range(len(chars) - 1)}

    left_set, right_set = _bigrams(left), _bigrams(right)
    shared = len(left_set & right_set)
    return shared / max(min(len(left_set), len(right_set)), 1)


def is_near_synonym_title(a: str, b: str) -> tuple[bool, float, str]:
    """判定两个归一化标题是否为近义（改写）关系。

    Returns:
        (是否近义, 相似度, 命中判据) — 判据为 "dice" / "containment" / ""
    """
    if not a or not b:
        return False, 0.0, ""
    dice = bigram_dice_similarity(a, b)
    if dice >= _SIMILAR_TITLE_THRESHOLD:
        return True, round(dice, 3), "dice"
    containment = _bigram_containment(a, b)
    if (
        containment >= _SIMILAR_CONTAINMENT_THRESHOLD
        and abs(len(a) - len(b)) <= _SIMILAR_LENGTH_DELTA
        and min(len(a), len(b)) >= 4
    ):
        return True, round(dice, 3), "containment"
    return False, round(dice, 3), ""


def analyze_toc(file_infos: list[dict]) -> dict:
    """目录相似度分析。

    ENHANCE 2026-09-14：替换原「标题集合 Jaccard」算法。

    原算法的缺陷：
      1. 用 set 丢掉了**顺序**与**重复**信息 → 两份目录标题相同但顺序完全不同
         也会被判 1.0 相似；
      2. Jaccard 对规模敏感 → 100 个标题 vs 10 个标题共享 10 个时仅 0.09，
         而实际上小目录被完全覆盖，属于高危抄袭；
      3. 没有风险分级，无法指导人工复核。

    新算法（与易标一致）：
      路径/标题重叠率 = |交集| / min(|A|,|B|)（覆盖率，解决规模敏感问题）
      顺序相似度      = LCS / max(len)（解决顺序问题）
      综合分 = 路径重叠 × 0.45 + 标题重叠 × 0.35 + 顺序相似 × 0.2
    并保留原有返回字段（tocs / matrix / filenames / duplicate_groups）以兼容前端。
    """
    n = len(file_infos)
    raw_tocs = [extract_toc(f.get("text", "")) for f in file_infos]
    normalized = [[normalize_outline_title(t) for t in titles] for titles in raw_tocs]
    # 过滤空标题（归一化后为空说明只是编号行）
    normalized = [[t for t in titles if t] for titles in normalized]

    title_sets = [set(titles) for titles in normalized]

    # ===== 1) 完全重复的标题组 =====
    duplicate_groups: list[dict] = []
    for i in range(n):
        for j in range(i + 1, n):
            common = title_sets[i] & title_sets[j]
            if common:
                duplicate_groups.append(
                    {
                        "file_a": file_infos[i]["filename"],
                        "file_b": file_infos[j]["filename"],
                        "common_titles": sorted(common)[:20],
                        "common_count": len(common),
                    },
                )

    # ===== 2) 近义标题组（Dice ≥ 0.86 或二元组包含度 ≥ 0.95，跨文件）=====
    similar_groups: list[dict] = []
    for i in range(n):
        for j in range(i + 1, n):
            seen_pairs: set[tuple[str, str]] = set()
            for left in normalized[i]:
                for right in normalized[j]:
                    if left == right or (left, right) in seen_pairs:
                        continue
                    matched, score, criterion = is_near_synonym_title(left, right)
                    if not matched:
                        continue
                    seen_pairs.add((left, right))
                    similar_groups.append(
                        {
                            "file_a": file_infos[i]["filename"],
                            "file_b": file_infos[j]["filename"],
                            "title_a": left,
                            "title_b": right,
                            "similarity": score,
                            "criterion": criterion,
                        },
                    )
                    if len(similar_groups) >= 200:
                        break
                if len(similar_groups) >= 200:
                    break

    # ===== 3) 两两综合相似度（覆盖率 + 顺序）=====
    pairwise: list[dict] = []
    matrix = [[0.0] * n for _ in range(n)]
    for i in range(n):
        matrix[i][i] = 1.0
    for i in range(n):
        for j in range(i + 1, n):
            set_i, set_j = title_sets[i], title_sets[j]
            shared = len(set_i & set_j)
            title_overlap = shared / max(min(len(set_i), len(set_j)), 1)
            # 本平台从纯文本抽取标题，没有显式层级路径，用「父级标题+自身」近似路径
            path_overlap = title_overlap
            order_similarity = lcs_similarity(normalized[i], normalized[j])
            score = round(path_overlap * 0.45 + title_overlap * 0.35 + order_similarity * 0.20, 3)
            matrix[i][j] = score
            matrix[j][i] = score
            pairwise.append(
                {
                    "file_a": file_infos[i]["filename"],
                    "file_b": file_infos[j]["filename"],
                    "score": score,
                    "title_overlap": round(title_overlap, 3),
                    "path_overlap": round(path_overlap, 3),
                    "order_similarity": round(order_similarity, 3),
                    "shared_count": shared,
                    "risk": outline_risk_from_score(score),
                },
            )

    # 兼容旧字段：duplicate_groups 补回 similarity（取综合分），并按分排序
    score_lookup = {(p["file_a"], p["file_b"]): p["score"] for p in pairwise}
    for group in duplicate_groups:
        group["similarity"] = score_lookup.get((group["file_a"], group["file_b"]), 0.0)
    duplicate_groups.sort(key=lambda g: g["similarity"], reverse=True)

    return {
        "tocs": [
            {"filename": f["filename"], "titles": t, "normalized_count": len(nrm)}
            for f, t, nrm in zip(file_infos, raw_tocs, normalized, strict=False)
        ],
        "matrix": matrix,
        "filenames": [f["filename"] for f in file_infos],
        "duplicate_groups": duplicate_groups,
        # 新增字段（前端可选消费）
        "similar_groups": similar_groups,
        "pairwise": sorted(pairwise, key=lambda p: p["score"], reverse=True),
        "high_risk_pairs": [p for p in pairwise if p["risk"] in ("high", "medium")],
    }


# ========== 维度3：正文重复检测 ==========


def split_sentences(text: str) -> list[str]:
    """结构化分句"""
    if not text:
        return []
    # 移除图片占位符
    text = re.sub(r"!\[\]\([^)]*\)|<img[^>]*>", "", text)
    # 按句号、分号、换行分句
    sentences = re.split(r"[。；;\n！!？?]+", text)
    return [s.strip() for s in sentences if s.strip() and len(s.strip()) > 5]


def normalize_sentence(s: str) -> str:
    """规范化句子（去标点、空白、统一大小写）"""
    s = re.sub(r"[\s\W_]+", "", s)
    return s.lower()


# ===== ENHANCE 2026-09-15：招标原文白名单四级匹配（移植自易标 buildTenderSourceMatcher）=====

#: 骨架化占位符（把日期/金额/百分比/数量统一替换掉，只留句式骨架）。
#: 用「〇」而不是 ASCII 字母：中文归一化会删掉 \W（含字母以外的符号）而保留汉字，
#: 〇（U+3007）属汉字范围，能安全穿过 normalize_sentence。
_SKELETON_PLACEHOLDER = "〇"

_SKELETON_SUBS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\d{4}年\d{1,2}月\d{1,2}日"), _SKELETON_PLACEHOLDER),
    (re.compile(r"\d{1,2}月\d{1,2}日"), _SKELETON_PLACEHOLDER),
    (re.compile(r"\d+(?:\.\d+)?(?:万元|亿元|万|亿|元)"), _SKELETON_PLACEHOLDER),
    (re.compile(r"\d+(?:\.\d+)?(?:天|日|个月|月|年|人|台|套|个|项|次|座|米|平方米)"), _SKELETON_PLACEHOLDER),
    (re.compile(r"\d+"), _SKELETON_PLACEHOLDER),
)

#: 骨架剩余实体字数下限：低于该值说明句子本身太笼统（如「工期为〇天」），
#: 不据此豁免，否则会把"两个投标人各自套用同一句模板话"误判成引用招标原文。
_SKELETON_MIN_CORE_CHARS = 8

#: near 判定的 Dice 阈值（对齐易标：短句更严、长句更宽）
_NEAR_SHORT_THRESHOLD = 0.95
_NEAR_LONG_THRESHOLD = 0.90
#: 超过该长度按长句阈值判定
_NEAR_LONG_LENGTH = 30
#: 单句最多精算 Dice 的候选数（先用倒排索引收敛，再排序取前 N）
_NEAR_MAX_CANDIDATES = 40
#: 参与模糊匹配的最短句长（过短句子 Dice 天然虚高，误判成本大于收益）
_NEAR_MIN_LENGTH = 8


def _char_bigrams(text: str) -> set[str]:
    """字符二元组集合（用于倒排索引与 Dice 相似度）。"""
    if len(text) <= 1:
        return set(text)
    return {text[index : index + 2] for index in range(len(text) - 1)}


def skeleton_signature(text: str) -> str:
    """句子的「骨架」指纹：把日期/金额/百分比/数量替换为占位符。

    用途：投标文件正常引用招标条款时，常会把原文里的日期、金额、数量替换成
    自己的数据（"投标保证金人民币50万元" → "投标保证金人民币80万元"）。
    这类句子归一化后并不相等，但**句式骨架完全相同**，属于正常引用而非抄袭。
    返回空串表示该句不适合做骨架判定（无占位内容，或骨架过于笼统）。
    """
    normalized = normalize_sentence(text)
    if not normalized:
        return ""
    skeleton = normalized
    for pattern, placeholder in _SKELETON_SUBS:
        skeleton = pattern.sub(placeholder, skeleton)
    if _SKELETON_PLACEHOLDER not in skeleton:
        return ""
    skeleton = re.sub(f"{_SKELETON_PLACEHOLDER}+", _SKELETON_PLACEHOLDER, skeleton)
    core = skeleton.replace(_SKELETON_PLACEHOLDER, "")
    if len(core) < _SKELETON_MIN_CORE_CHARS:
        return ""
    return skeleton


class TenderWhitelist:
    """招标原文白名单：exact / strict / skeleton / near 四级匹配。

    为什么需要分级（易标 `buildTenderSourceMatcher` 的核心理由）：
    投标文件**理当**大量引用招标文件原文（响应性条款、承诺、技术规格）。
    若只做「归一化精确匹配」白名单，任何一处抄录时的措辞微调、数字替换都会
    漏出白名单，被当成"两家的正文重复"，从而产生大量噪声误报。
    """

    #: 命中级别（供报告与统计使用）
    LEVELS = ("exact", "strict", "skeleton", "near")

    def __init__(self, tender_text: str = "") -> None:
        self.exact: set[str] = set()
        self.strict: set[str] = set()
        self.skeleton: set[str] = set()
        self._bigram_index: dict[str, set[str]] = {}
        self._whitelist_sentences: list[str] = []

        for sentence in split_sentences(tender_text or ""):
            raw = sentence.strip()
            strict = normalize_sentence(raw)
            if not strict:
                continue
            self.exact.add(raw)
            self.strict.add(strict)
            skeleton = skeleton_signature(raw)
            if skeleton:
                self.skeleton.add(skeleton)
            if len(strict) >= _NEAR_MIN_LENGTH:
                self._whitelist_sentences.append(strict)
                for bigram in _char_bigrams(strict):
                    self._bigram_index.setdefault(bigram, set()).add(strict)

    def __len__(self) -> int:
        return len(self.strict)

    def match_level(self, sentence: str) -> str | None:
        """返回该句命中的白名单级别；未命中返回 None。"""
        raw = str(sentence or "").strip()
        strict = normalize_sentence(raw)
        if not strict:
            return None
        if raw in self.exact:
            return "exact"
        if strict in self.strict:
            return "strict"
        skeleton = skeleton_signature(raw)
        if skeleton and skeleton in self.skeleton:
            return "skeleton"
        if self._near_match(strict):
            return "near"
        return None

    def _near_match(self, strict: str) -> bool:
        """倒排索引收敛候选 + Dice 精算，判定是否为招标原文的近似改写。"""
        if not self._whitelist_sentences or len(strict) < _NEAR_MIN_LENGTH:
            return False
        hits: dict[str, int] = {}
        for bigram in _char_bigrams(strict):
            for candidate in self._bigram_index.get(bigram, ()):
                hits[candidate] = hits.get(candidate, 0) + 1
        if not hits:
            return False
        threshold = _NEAR_LONG_THRESHOLD if len(strict) >= _NEAR_LONG_LENGTH else _NEAR_SHORT_THRESHOLD
        ranked = sorted(hits.items(), key=lambda item: item[1], reverse=True)[:_NEAR_MAX_CANDIDATES]
        for candidate, _shared in ranked:
            # 长度差过大的候选不可能达到阈值，先剪枝省掉 Dice 计算
            if abs(len(candidate) - len(strict)) > max(6, int(len(strict) * 0.4)):
                continue
            if bigram_dice_similarity(strict, candidate) >= threshold:
                return True
        return False


def analyze_content(file_infos: list[dict], tender_text: str = "") -> dict:
    """正文重复检测"""
    # 白名单：招标文件的所有句子（四级匹配，避免把正常引用判成重复）
    whitelist = TenderWhitelist(tender_text)
    whitelist_stats: dict[str, int] = {}
    whitelist_cache: dict[str, str | None] = {}

    def _whitelist_level(sentence: str) -> str | None:
        """带缓存的匹配（同一句子在倒排与相似两轮中会各查一次）。

        统计只在该句子**首次**匹配时累加，避免同一句重复出现把计数放大。
        """
        if sentence not in whitelist_cache:
            level = whitelist.match_level(sentence)
            whitelist_cache[sentence] = level
            if level:
                whitelist_stats[level] = whitelist_stats.get(level, 0) + 1
        return whitelist_cache[sentence]

    # 每个文件的句子列表
    files_sentences = []
    for f in file_infos:
        sentences = split_sentences(f.get("text", ""))
        files_sentences.append(
            {
                "filename": f["filename"],
                "file_id": f["file_id"],
                "sentences": sentences,
            },
        )

    # 倒排索引：句子指纹 -> 文件 ID 列表
    inverted_index: dict[str, list[dict]] = {}
    for fs in files_sentences:
        for idx, s in enumerate(fs["sentences"]):
            norm = normalize_sentence(s)
            if not norm:
                continue
            if _whitelist_level(s):
                continue
            if norm not in inverted_index:
                inverted_index[norm] = []
            inverted_index[norm].append(
                {
                    "file_id": fs["file_id"],
                    "filename": fs["filename"],
                    "sentence_idx": idx,
                    "original": s,
                },
            )

    # 完全相同的重复句群
    duplicate_groups = []
    for _norm, occurrences in inverted_index.items():
        if len(occurrences) > 1:
            # 至少来自 2 个不同文件
            filenames = set(o["filename"] for o in occurrences)
            if len(filenames) > 1:
                duplicate_groups.append(
                    {
                        "type": "exact",
                        "sentence": occurrences[0]["original"],
                        "occurrences": occurrences,
                    },
                )

    # 高度相似检测（简单 Jaccard）
    similar_groups = []
    all_sentences = []
    for fs in files_sentences:
        for idx, s in enumerate(fs["sentences"]):
            norm = normalize_sentence(s)
            if norm and not _whitelist_level(s):
                all_sentences.append(
                    {
                        "norm_set": set(norm),
                        "filename": fs["filename"],
                        "file_id": fs["file_id"],
                        "sentence_idx": idx,
                        "original": s,
                    },
                )

    # 简单 O(n²) 比较（限制数量避免性能问题）
    # BUG-FIX 2026-09-18（中）：原实现在句子数 ≥ 5000 时**静默跳过**整个相似句检测，
    # 返回的 `similar_groups` 为空且没有任何标记 —— 用户看到「相似句 0 条」会误判为
    # 「两家标书没有雷同」，实际是根本没检测（长标书几万字轻易超过 5000 句）。
    # 性能护栏（O(n²)）保留不变，但必须如实上报「已跳过」这一事实。
    _SIMILAR_MAX_SENTENCES = 5000
    similar_skipped = len(all_sentences) >= _SIMILAR_MAX_SENTENCES
    if not similar_skipped:
        for i in range(len(all_sentences)):
            for j in range(i + 1, len(all_sentences)):
                if all_sentences[i]["filename"] == all_sentences[j]["filename"]:
                    continue
                sim = jaccard_similarity(all_sentences[i]["norm_set"], all_sentences[j]["norm_set"])
                if sim > 0.8 and sim < 1.0:
                    similar_groups.append(
                        {
                            "type": "similar",
                            "similarity": round(sim, 3),
                            "sentence_a": all_sentences[i]["original"],
                            "sentence_b": all_sentences[j]["original"],
                            "file_a": all_sentences[i]["filename"],
                            "file_b": all_sentences[j]["filename"],
                        },
                    )

    # BUG-FIX 2026-09-18（中）：`similar_count` 原为**截断后**的长度（先 `[:100]`
    # 再计数），与同结构的 `duplicate_count`（截断前取总数）口径不一致 ——
    # 用户看到「相似 100 条」，而实际可能有数千条。先取真实总数，再限制输出数量。
    similar_total = len(similar_groups)
    similar_groups = similar_groups[:100]

    return {
        "duplicate_groups": duplicate_groups[:100],
        "similar_groups": similar_groups,
        "stats": {
            "total_sentences": sum(len(fs["sentences"]) for fs in files_sentences),
            "duplicate_count": len(duplicate_groups),
            "similar_count": similar_total,
            "similar_returned": len(similar_groups),
            "similar_skipped": similar_skipped,
            # ENHANCE 2026-09-15：招标原文白名单分级命中统计，便于人工判断
            # 「重复变少」是正常引用被正确排除，还是白名单过度豁免
            "tender_whitelist": {
                level: whitelist_stats.get(level, 0) for level in TenderWhitelist.LEVELS
            },
            "tender_whitelist_count": len(whitelist),
        },
    }


# ========== 维度4：图片哈希 ==========


def extract_image_hashes(file_info: dict) -> list[dict]:
    """提取文件中所有图片的 SHA-256 哈希"""
    file_path = file_info.get("path", "")
    file_type = file_info.get("file_type", "").lower()
    images = []

    if not file_path or not Path(file_path).exists():
        return images

    try:
        if file_type == "docx" or file_path.endswith(".docx"):
            images = _extract_docx_images(file_path)
        elif file_type == "pdf" or file_path.endswith(".pdf"):
            images = _extract_pdf_images(file_path)
    except Exception:
        logger.warning("图片提取失败(extract_images)", exc_info=True)

    for img in images:
        img["file_id"] = file_info["file_id"]
        img["filename"] = file_info.get("filename", "")
    return images


# ----- ENHANCE 2026-09-15：图片上下文定位（移植自易标图片比对的上下文记录）-----
# 易标判定「重复图片」时会同时记录图片所在的标题路径、序号与前一句文本，
# 人工复核时才能判断"同一张图是否出现在同一章节语境"（同源配图 vs 通用示意图）。
# 我们原来只按 SHA-256 分组，复核者拿不到任何上下文。

_DOCX_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_DOCX_R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_DOCX_A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
#: 标题样式识别：Word 英文模板 Heading1 / 中文模板「标题 1」
_DOCX_HEADING_RE = re.compile(r"(?:heading|标题)\s*([1-9])", re.IGNORECASE)


def _docx_relationship_targets(rels_bytes: bytes) -> dict[str, str]:
    """解析 document.xml.rels：rId → 包内媒体路径（如 word/media/image1.png）。"""
    targets: dict[str, str] = {}
    try:
        root = ET.fromstring(rels_bytes)
    except ET.ParseError:
        return targets
    for rel in root:
        rid = rel.get("Id") or ""
        target = rel.get("Target") or ""
        if not rid or not target:
            continue
        if target.startswith("/"):
            targets[rid] = posixpath.normpath(target.lstrip("/"))
        else:
            targets[rid] = posixpath.normpath(posixpath.join("word", target))
    return targets


def _docx_image_contexts(xml_bytes: bytes) -> list[dict]:
    """按文档顺序扫描 document.xml，返回每张图片的关系 id 与上下文。"""
    contexts: list[dict] = []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return contexts

    heading_stack: list[tuple[int, str]] = []
    previous_text = ""
    image_index = 0

    for para in root.iter(f"{_DOCX_W_NS}p"):
        style = ""
        p_pr = para.find(f"{_DOCX_W_NS}pPr")
        if p_pr is not None:
            p_style = p_pr.find(f"{_DOCX_W_NS}pStyle")
            if p_style is not None:
                style = (p_style.get(f"{_DOCX_W_NS}val") or "").replace(" ", "").strip()

        text = "".join(node.text or "" for node in para.iter(f"{_DOCX_W_NS}t")).strip()

        level = 0
        match = _DOCX_HEADING_RE.search(style)
        if match:
            level = int(match.group(1))
        elif len(style) == 1 and style.isdigit():
            level = int(style)  # 少数生成器把标题级别直接写成 "1".."6"
        if level and text:
            heading_stack = [item for item in heading_stack if item[0] < level]
            heading_stack.append((level, text))
            previous_text = text
            continue

        embeds: list[str] = []
        for blip in para.iter(f"{_DOCX_A_NS}blip"):
            rid = blip.get(f"{_DOCX_R_NS}embed") or blip.get(f"{_DOCX_R_NS}link")
            if rid and rid not in embeds:
                embeds.append(rid)
        if embeds:
            directory = " / ".join(item[1] for item in heading_stack)
            for rid in embeds:
                image_index += 1
                contexts.append(
                    {
                        "rid": rid,
                        "image_index": image_index,
                        "directory": directory,
                        "previous_sentence": text or previous_text,
                    },
                )
        if text:
            previous_text = text
    return contexts


def _extract_docx_images(file_path: str) -> list[dict]:
    """从 docx 提取图片（含章节路径 / 序号 / 前一段文字）。"""
    images: list[dict] = []
    try:
        with zipfile.ZipFile(file_path) as z:
            media = {
                name: z.read(name) for name in z.namelist() if name.startswith("word/media/")
            }
            try:
                contexts = _docx_image_contexts(z.read("word/document.xml"))
            except KeyError:
                contexts = []
            try:
                targets = _docx_relationship_targets(z.read("word/_rels/document.xml.rels"))
            except KeyError:
                targets = {}
    except Exception:
        logger.warning("docx图片提取失败", exc_info=True)
        return images

    # 同一张媒体文件在正文中可能出现多次，取首次出现的上下文
    context_by_media: dict[str, dict] = {}
    for context in contexts:
        media_path = targets.get(context["rid"], "")
        if media_path and media_path not in context_by_media:
            context_by_media[media_path] = context

    for name in sorted(media):
        data = media[name]
        context = context_by_media.get(name) or {}
        images.append(
            {
                "hash": hashlib.sha256(data).hexdigest(),
                "path": name,
                "size": len(data),
                "image_index": context.get("image_index", 0),
                "directory": context.get("directory", ""),
                "previous_sentence": context.get("previous_sentence", ""),
            },
        )
    return images


def _extract_pdf_images(file_path: str) -> list[dict]:
    """从 PDF 提取图片（含页码与序号；PDF 无可靠图文顺序，不猜前一句）"""
    images = []
    try:
        from pypdf import PdfReader

        reader = PdfReader(file_path)
        image_index = 0
        for page_idx, page in enumerate(reader.pages):
            try:
                if hasattr(page, "images"):
                    for img in page.images:
                        data = img.data
                        image_index += 1
                        images.append(
                            {
                                "hash": hashlib.sha256(data).hexdigest(),
                                "path": f"page-{page_idx}-{img.name}",
                                "size": len(data),
                                "image_index": image_index,
                                "directory": f"第{page_idx + 1}页",
                                # PDF 的文本层与图像对象没有可靠顺序关系，
                                # 与其给一个可能错位的"前一句"，不如留空由人工核对
                                "previous_sentence": "",
                            },
                        )
            except Exception:
                logger.warning("pdf页面图片提取失败", exc_info=True)
                continue
    except Exception:
        logger.warning("pdf图片提取失败", exc_info=True)
    return images


def analyze_images(file_infos: list[dict]) -> dict:
    """图片哈希对比（含章节路径 / 序号 / 前一句上下文）"""
    all_images = []
    for f in file_infos:
        all_images.extend(extract_image_hashes(f))

    # 按哈希分组
    hash_groups: dict[str, list[dict]] = {}
    for img in all_images:
        h = img["hash"]
        if h not in hash_groups:
            hash_groups[h] = []
        hash_groups[h].append(img)

    # 仅保留重复的（出现在多个文件）
    duplicate_groups = []
    for h, imgs in hash_groups.items():
        filenames = set(i["filename"] for i in imgs)
        if len(filenames) > 1:
            duplicate_groups.append(
                {
                    "hash": h[:16] + "...",
                    "size": imgs[0]["size"],
                    "occurrences": imgs,
                    # 便于人工快速定位的「文件 · 章节 · 第N张」摘要
                    "locations": [
                        f"{i.get('filename', '')} · {i.get('directory') or '未识别章节'}"
                        f" · 第{i.get('image_index') or '?'}张"
                        for i in imgs
                    ],
                },
            )

    duplicate_groups.sort(key=lambda group: len(group["occurrences"]), reverse=True)

    return {
        "total_images": len(all_images),
        "duplicate_groups": duplicate_groups,
        "duplicate_image_count": sum(len(group["occurrences"]) for group in duplicate_groups),
    }


# ========== 主检查流程 ==========


async def run_duplicate_check(task_id: int):
    """执行查重任务"""
    task = get_task(task_id)
    if not task:
        return

    update_task_status(task_id, "running")
    update_task_progress(task_id, 5, "正在加载文件...")

    file_ids = task["file_ids"]
    tender_file_id = task.get("tender_file_id")

    # 加载文件信息
    file_infos = []
    for fid in file_ids:
        try:
            info = _load_file_info(fid)
            file_infos.append(info)
        except Exception as e:
            update_task_progress(task_id, 5, f"文件 {fid} 加载失败: {e}")

    if len(file_infos) < 2:
        update_task_status(task_id, "failed", error="至少需要 2 个有效文件")
        return

    tender_text = ""
    if tender_file_id:
        try:
            tender_info = _load_file_info(tender_file_id)
            tender_text = tender_info.get("text", "")
        except Exception:
            logger.warning("招标文件文本加载失败", exc_info=True)

    update_task_progress(task_id, 20, "正在分析元数据...")

    # 并发执行四维度分析
    async def _metadata():
        return await asyncio.to_thread(analyze_metadata, file_infos)

    async def _toc():
        return await asyncio.to_thread(analyze_toc, file_infos)

    async def _content():
        return await asyncio.to_thread(analyze_content, file_infos, tender_text)

    async def _images():
        return await asyncio.to_thread(analyze_images, file_infos)

    update_task_progress(task_id, 40, "正在执行四维度分析...")

    # BUG-FIX 2026-09-18（高）：`asyncio.gather` 默认**不**吞异常 —— 四维度中任一
    # 抛异常会立即传播，使 `run_duplicate_check` 直接崩溃：既写不了 `results`，
    # 也不会执行下面的 `update_task_status(task_id, "completed")`，任务状态
    # **永远停留在 running**（前端轮询到超时，用户只看到"进行中"，无从得知失败）。
    # 而四个 `analyze_*` 只保护了内部的小步骤，顶层的 `extract_metadata` /
    # `extract_toc` / `extract_image_hashes` 均**无 try**，一份损坏的 DOCX/PDF
    # 就足以让整个维度失败。
    # 改为 `return_exceptions=True` + 逐维度降级，并按失败情况如实上报终态
    # （与 `rejection.py` 的 `dim_failures` 处理保持一致）。
    _DIM_KEYS = ("metadata", "toc", "content", "images")
    gathered = await asyncio.gather(
        _metadata(),
        _toc(),
        _content(),
        _images(),
        return_exceptions=True,
    )

    update_task_progress(task_id, 90, "正在汇总结果...")

    results: dict = {}
    failed_dims: list[str] = []
    for _key, _val in zip(_DIM_KEYS, gathered, strict=False):
        if isinstance(_val, BaseException):
            failed_dims.append(_key)
            logger.error("查重维度 %s 分析失败: %s", _key, _val, exc_info=_val)
            # 导出报告全部用 `.get(key, [])` 取值，这里的降级结构与前端/报告兼容。
            results[_key] = {"error": str(_val)[:200], "available": False}
        else:
            results[_key] = _val

    if len(failed_dims) == len(_DIM_KEYS):
        update_task_status(
            task_id,
            "failed",
            results=results,
            error="查重四个维度全部失败，结论不可用，请检查文件是否损坏后重试",
        )
        return

    if failed_dims:
        # 部分维度失败仍给出可用结论，但必须让接口层如实告知不完整。
        update_task_status(
            task_id,
            "completed",
            results=results,
            error="部分维度分析失败，结论不完整: " + "、".join(failed_dims),
        )
        update_task_progress(task_id, 100, "查重完成（部分维度失败）")
        return

    update_task_status(task_id, "completed", results=results)
    update_task_progress(task_id, 100, "查重完成")


# ========== 报告导出 ==========


def export_report(task_id: int) -> str:
    """导出查重报告（DOCX）"""
    task = get_task(task_id)
    if not task:
        raise ValueError("任务不存在")

    DUPLICATE_REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    try:
        from docx import Document
        from docx.enum.text import WD_ALIGN_PARAGRAPH
    except ImportError:
        raise ImportError("需要安装 python-docx")

    doc = Document()
    title = doc.add_heading("标书查重报告", level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER

    doc.add_paragraph(f"生成时间：{datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')}")
    doc.add_paragraph(f"任务 ID：{task_id}")

    results = task.get("results", {})

    # 元数据
    doc.add_heading("一、元数据对比", level=1)
    metadata = results.get("metadata", {})
    metadatas = metadata.get("metadata", [])
    if metadatas:
        table = doc.add_table(rows=1, cols=4)
        table.style = "Table Grid"
        hdr = table.rows[0].cells
        hdr[0].text = "文件名"
        hdr[1].text = "作者"
        hdr[2].text = "创建时间"
        hdr[3].text = "公司"
        for m in metadatas:
            row = table.add_row().cells
            row[0].text = _report_text(m.get("filename"))
            row[1].text = _report_text(m.get("author"))
            row[2].text = _report_text(m.get("created"))
            row[3].text = _report_text(m.get("company"))

        anomalies = metadata.get("anomalies", [])
        if anomalies:
            doc.add_heading("异常项", level=2)
            for a in anomalies:
                doc.add_paragraph(
                    f"- {_report_text(a.get('description'))}：{_report_join(a.get('value'))}"
                )

    # 目录
    doc.add_heading("二、目录相似度", level=1)
    toc = results.get("toc", {})
    # ENHANCE 2026-09-14：优先展示带风险分级的综合相似度（覆盖率 + 顺序）
    pairwise = toc.get("pairwise", [])
    risk_label = {"high": "高", "medium": "中", "low": "低", "none": "无"}
    if pairwise:
        table = doc.add_table(rows=1, cols=5)
        table.style = "Table Grid"
        hdr = table.rows[0].cells
        hdr[0].text = "文件 A"
        hdr[1].text = "文件 B"
        hdr[2].text = "综合相似度"
        hdr[3].text = "顺序相似度"
        hdr[4].text = "风险"
        for p in pairwise[:20]:
            row = table.add_row().cells
            row[0].text = _report_text(p.get("file_a"))
            row[1].text = _report_text(p.get("file_b"))
            row[2].text = str(p.get("score", 0))
            row[3].text = str(p.get("order_similarity", 0))
            row[4].text = risk_label.get(p.get("risk", "none"), "无")

    duplicate_groups = toc.get("duplicate_groups", [])
    if duplicate_groups:
        doc.add_heading("目录标题重复明细", level=2)
        for g in duplicate_groups[:20]:
            doc.add_paragraph(
                f"- {_report_text(g.get('file_a'))} ↔ {_report_text(g.get('file_b'))}"
                f"（综合相似度: {g.get('similarity', 0)}，共同标题 {g.get('common_count', len(g.get('common_titles', []) or []))} 个）"
            )
    similar_groups = toc.get("similar_groups", [])
    if similar_groups:
        doc.add_heading("近义目录标题（措辞微调）", level=2)
        for g in similar_groups[:20]:
            doc.add_paragraph(
                f"- {_report_text(g.get('file_a'))}「{_report_text(g.get('title_a'))}」"
                f" ↔ {_report_text(g.get('file_b'))}「{_report_text(g.get('title_b'))}」"
                f"（相似度 {g.get('similarity', 0)}）"
            )
    if not duplicate_groups and not similar_groups:
        doc.add_paragraph("未发现目录重复")

    # 正文
    doc.add_heading("三、正文重复", level=1)
    content = results.get("content", {})
    stats = content.get("stats", {})
    doc.add_paragraph(f"总句子数：{stats.get('total_sentences', 0)}")
    doc.add_paragraph(f"完全相同重复：{stats.get('duplicate_count', 0)}")
    doc.add_paragraph(f"高度相似：{stats.get('similar_count', 0)}")

    duplicate_groups = content.get("duplicate_groups", [])
    if duplicate_groups:
        doc.add_heading("完全相同的重复句子", level=2)
        for g in duplicate_groups[:30]:
            doc.add_paragraph(f"- 「{_report_text(g.get('sentence'))}」")
            for o in g.get("occurrences", []) or []:
                doc.add_paragraph(f"   出现在：{_report_text(o.get('filename'))}")

    # 图片
    doc.add_heading("四、图片哈希重复", level=1)
    images = results.get("images", {})
    doc.add_paragraph(f"总图片数：{images.get('total_images', 0)}")
    img_groups = images.get("duplicate_groups", [])
    if img_groups:
        for g in img_groups[:20]:
            doc.add_paragraph(f"- 哈希 {_report_text(g.get('hash'))}（{_report_text(g.get('size'))} 字节）")
            for o in g.get("occurrences", []) or []:
                doc.add_paragraph(
                    f"   出现在：{_report_text(o.get('filename'))} - {_report_text(o.get('path'))}"
                )
    else:
        doc.add_paragraph("未发现重复图片")

    # ===== ENHANCE 2026-09-14：文档同源指纹（围标串标线索）=====
    doc.add_heading("五、文档同源指纹", level=1)
    fingerprints = metadata.get("fingerprints", [])
    if fingerprints:
        doc.add_paragraph("以下为各文件内部痕迹（Word 编辑会话 ID / PDF 结构 / WPS 账号痕迹），用于判断是否共用底稿：")
        for m, fp in zip(metadatas, fingerprints, strict=False):
            doc.add_paragraph(f"【{_report_text(m.get('filename'))}】")
            if not fp:
                doc.add_paragraph("  未能提取到指纹信息（文件格式不支持或文件不可读）")
                continue
            for key, value in fp.items():
                if isinstance(value, list):
                    value = "；".join(str(v) for v in value[:5])
                doc.add_paragraph(f"  {_report_text(key)}: {_report_text(value)}")

    same_source_pairs = metadata.get("same_source_pairs", [])
    if same_source_pairs:
        doc.add_heading("同源疑点（建议人工核实）", level=2)
        severity_label = {"high": "高", "medium": "中", "low": "低"}
        for pair in same_source_pairs:
            doc.add_paragraph(
                f"- {pair['file_a']} ↔ {pair['file_b']}"
                f"（严重度 {severity_label.get(pair.get('severity', 'low'), '低')}）："
                f"{'；'.join(pair.get('evidence', []))}"
            )
        doc.add_paragraph(
            "说明：同源痕迹仅表示文件由同一办公环境或同一底稿派生，"
            "不构成串标结论，请结合商务关系与投标授权情况人工核实。"
        )
    else:
        doc.add_paragraph("未发现同源痕迹")

    filename = f"duplicate_report_{task_id}_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.docx"
    file_path = DUPLICATE_REPORTS_DIR / filename
    doc.save(str(file_path))
    return str(file_path)


def export_report_excel(task_id: int) -> str:
    """导出查重报告（Excel 多 Sheet）。

    ENHANCE 2026-09-15：移植自易标 `checkResultExportService.cjs` 的
    「查重概览 / 元数据 / 目录重复 / 文件相似度 / 重复句子 / 重复图片」六 Sheet 结构。

    Word 报告适合阅读，Excel 报告适合复核：相似度列带百分比格式与自动筛选，
    「文件相似度」表可直接按风险等级排序定位重点文件对。
    """
    task = get_task(task_id)
    if not task:
        raise ValueError("任务不存在")

    sheets = _excel_report.build_duplicate_workbook(task)
    filename = f"duplicate_report_{task_id}_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.xlsx"
    return str(_excel_report.write_workbook(sheets, DUPLICATE_REPORTS_DIR / filename))
