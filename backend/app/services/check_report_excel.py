"""检查结果 Excel（xlsx）多 Sheet 报告。

ENHANCE 2026-09-15：移植自「易标投标工具箱」
`client/electron/services/checkResultExportService.cjs` 的报表体系。

背景：本平台的废标项检查与标书查重原先只能导出 Word 报告。Word 适合阅读归档，
但**不适合复核与流转**——评标复核时要按风险等级筛选、按文件排序、把问题清单贴进
Excel 台账，Word 表格做这些事非常低效。易标为此按「一类检查一份工作簿、一个维度
一个 Sheet」的方式导出 xlsx，并统一带上首行冻结 + 自动筛选 + 百分比列格式。

设计：
  - `build_rejection_workbook(task)` / `build_duplicate_workbook(task)` 是**纯函数**：
    只吃任务字典、吐 `SheetSpec` 列表，不碰数据库也不写盘 → 可以直接单测；
  - `write_workbook(sheets, target)` 集中处理 Excel 的硬约束：单元格 32767 字符上限
    截断、Sheet 名非法字符、以 `=` 开头的文本被误判为公式、百分比列格式、列宽自适应。

与参考实现的差异（有意为之）：
  - 参考实现用 SheetJS 直接拼数组；这里用 openpyxl（项目已有依赖），并把「表格数据」
    与「写盘细节」拆开，避免报表逻辑和 Excel 库耦合；
  - 概览 Sheet 的「问题数量」按**剔除人工误报后**的可见条数统计，并在说明列注明被剔除
    的条数，避免用户看到"报告里没有、概览里却有"的困惑。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
import logging
import math
from pathlib import Path
import re
from typing import Any


logger = logging.getLogger(__name__)


#: Excel 单元格文本上限为 32767，留出余量避免不同写入器的边界差异
MAX_CELL_CHARS = 32000
#: Excel Sheet 名上限 31 字符，且不允许 : \ / ? * [ ]
_SHEET_NAME_BAD_RE = re.compile(r"[:\\/?*\[\]]")
MAX_SHEET_NAME_CHARS = 31


@dataclass
class SheetSpec:
    """一个 Sheet 的纯数据描述（与 openpyxl 解耦，便于单测）。"""

    title: str
    headers: list[str]
    rows: list[list[Any]] = field(default_factory=list)
    #: 列宽（字符数），缺省时按内容自适应
    widths: list[int] = field(default_factory=list)
    #: 需要按百分比格式渲染的列（1 起始），单元格值应为 0~1 的浮点数
    percent_columns: tuple[int, ...] = ()
    #: 数据区下方的统计/说明行（每项一行，写在 A 列）
    notes: list[str] = field(default_factory=list)


# ==================== 通用取值/文案工具 ====================


def cell_text(value: Any) -> str:
    """把任意值转成受 Excel 长度限制保护的单元格文本。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        text = "是" if value else "否"
    elif isinstance(value, (list, tuple, set)):
        text = "；".join(str(item) for item in value)
    else:
        text = str(value)
    text = text.strip()
    if len(text) > MAX_CELL_CHARS:
        text = text[: MAX_CELL_CHARS - 3] + "..."
    return text


def severity_label(value: Any) -> str:
    """风险等级 → 中文（兼容 critical/warning/info 与 high/medium/low 两套写法）。"""
    return {
        "critical": "严重",
        "warning": "警告",
        "info": "提示",
        "high": "高",
        "medium": "中",
        "low": "低",
        "none": "无",
    }.get(str(value or "").strip().lower(), "警告")


def risk_label(value: Any) -> str:
    """目录/文件相似度风险 → 中文。"""
    return {"high": "高", "medium": "中", "low": "低", "none": "无"}.get(
        str(value or "").strip().lower(), "无",
    )


def as_ratio(value: Any) -> float | str:
    """把 0~1 的相似度转成 Excel 百分比单元格的浮点值；非法值原样返回文本。"""
    if isinstance(value, bool) or value is None:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return cell_text(value)
    # BUG-FIX：历史结果 JSON 可能含 Infinity / NaN（Python json.loads 默认接受），
    # 写进单元格会生成 `<v>nan</v>`，Excel 打开时提示「发现部分内容有问题」并触发修复。
    if not math.isfinite(number):
        return ""
    if number < 0:
        return ""
    return number


def now_text() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _first_present(issue: dict, *keys: str) -> str:
    """按优先级取第一个非空字段（容忍不同检查维度与历史字段命名）。"""
    for key in keys:
        value = issue.get(key)
        if value not in (None, "", [], {}):
            return cell_text(value)
    return ""


def _verification_text(issue: dict) -> str:
    """证据核验结论文案（本平台在参考实现之上补了原文定位核验）。"""
    if issue.get("validated") is True:
        hint = _first_present(issue, "line_hint")
        return f"已定位{hint and '：' + hint or ''}"
    if issue.get("validated") is False:
        return "未能核验（摘录与原文不一致，建议人工重点复核）"
    if issue.get("evidence_kind") == "structural":
        return "结构性线索（材料缺失类，无原文摘录）"
    return ""


def missing_state_text(issue: dict) -> str:
    """跨分块「疑似缺失」复核结论文案（ENHANCE 2026-09-15）。"""
    return {
        "resolved": "已排除（该材料在其它分块中存在线索）",
        "confirmed": "缺失成立（全文均无线索）",
        "pending": "待人工确认",
    }.get(str(issue.get("missing_state") or "").strip(), "")


# ==================== 废标项检查报表 ====================


#: 维度定义：(结果键, 中文名, 说明)
REJECTION_DIMENSIONS: tuple[tuple[str, str, str], ...] = (
    ("rejection", "废标项检查", "招标文件无效投标/废标条款的逐条响应性检查"),
    ("spelling", "错别字检查", "错别字、用词不当与标点问题检查"),
    ("logic", "逻辑谬误检查", "工期、人数、金额、技术指标等前后矛盾检查"),
)


def _visible_issues(results: dict, dimension: str, false_positives: set) -> list[dict]:
    """取某一维度的可见问题（剔除人工标记的误报）。"""
    items = results.get(dimension) or []
    return [item for item in items if isinstance(item, dict) and item.get("id") not in false_positives]


def build_rejection_overview_sheet(task: dict, results: dict, false_positives: set) -> SheetSpec:
    """Sheet 1：检查概览（各维度状态 + 问题数量 + 核验统计）。"""
    status = cell_text(task.get("status")) or "unknown"
    status_text = {
        "completed": "已完成",
        "running": "进行中",
        "failed": "失败",
        "pending": "待开始",
    }.get(status, status)

    documents = task.get("bid_documents") or []
    document_names = [
        cell_text(doc.get("name") if isinstance(doc, dict) else doc) for doc in documents
    ]
    documents_text = "、".join(name for name in document_names if name) or "-"

    rows: list[list[Any]] = []
    total_visible = 0
    for key, label, description in REJECTION_DIMENSIONS:
        raw_count = len(results.get(key) or [])
        visible = _visible_issues(results, key, false_positives)
        total_visible += len(visible)
        removed = raw_count - len(visible)
        note = description
        if removed:
            note = f"{description}；已剔除人工误报 {removed} 项"
        rows.append([label, status_text, len(visible), now_text(), documents_text, note])

    rejection_items = results.get("rejection") or []
    verified = sum(1 for i in rejection_items if isinstance(i, dict) and i.get("validated") is True)
    unverified = sum(1 for i in rejection_items if isinstance(i, dict) and i.get("validated") is False)
    structural = sum(
        1 for i in rejection_items if isinstance(i, dict) and i.get("validated") is None
    )

    notes = [
        f"生成时间：{now_text()}",
        f"任务 ID：{task.get('id', '')}",
        f"项目 ID：{task.get('project_id', '')}",
        f"有效问题合计（已剔除误报）：{total_visible} 项",
        f"已剔除人工误报：{len(false_positives)} 项",
        f"废标项证据核验：已在原文定位 {verified} 项；未能核验 {unverified} 项；"
        f"结构性线索（材料缺失类，无原文摘录）{structural} 项",
    ]

    # 跨分块缺失复核统计（ENHANCE 2026-09-15）
    def _missing_count(state: str) -> int:
        return sum(
            1 for item in rejection_items if isinstance(item, dict) and item.get("missing_state") == state
        )

    missing_resolved = _missing_count("resolved")
    missing_confirmed = _missing_count("confirmed")
    missing_pending = _missing_count("pending")
    if missing_resolved or missing_confirmed or missing_pending:
        notes.append(
            f"疑似缺失跨块复核：全文复核后排除 {missing_resolved} 项（该材料在其它分块中存在线索）；"
            f"确认缺失 {missing_confirmed} 项；待人工确认 {missing_pending} 项"
        )

    notes.append(f"检查对象：{documents_text}")
    return SheetSpec(
        title="检查概览",
        headers=["检查类型", "状态", "问题数量", "更新时间", "检查对象", "说明"],
        rows=rows,
        widths=[16, 10, 10, 20, 34, 46],
        notes=notes,
    )


def build_rejection_sheet(results: dict, false_positives: set) -> SheetSpec:
    """Sheet 2：废标项明细（含跨分块缺失复核列）。"""
    rows: list[list[Any]] = []
    for index, issue in enumerate(_visible_issues(results, "rejection", false_positives), 1):
        rows.append(
            [
                index,
                _first_present(issue, "bid_doc_id"),
                _first_present(issue, "bid_doc_name"),
                _first_present(issue, "check_item_id"),
                _first_present(issue, "category") or "废标",
                severity_label(issue.get("severity")),
                _first_present(issue, "description"),
                _first_present(issue, "quote", "bidEvidence", "evidence"),
                _first_present(issue, "location"),
                _verification_text(issue),
                missing_state_text(issue),
                _first_present(issue, "missing_state_reason"),
                _first_present(issue, "suggestion"),
            ],
        )
    return SheetSpec(
        title="废标项",
        headers=[
            "序号",
            "投标文件序号",
            "投标文件名",
            "检查项编号",
            "类型",
            "风险等级",
            "标题",
            "原文证据",
            "位置",
            "证据核验",
            "缺失复核",
            "缺失复核说明",
            "修改建议",
        ],
        rows=rows,
        widths=[6, 12, 26, 12, 10, 10, 30, 44, 24, 32, 26, 46, 40],
    )


def build_spelling_sheet(results: dict, false_positives: set) -> SheetSpec:
    """Sheet 3：错别字明细。"""
    rows: list[list[Any]] = []
    for index, issue in enumerate(_visible_issues(results, "spelling", false_positives), 1):
        rows.append(
            [
                index,
                _first_present(issue, "bid_doc_id"),
                _first_present(issue, "bid_doc_name"),
                _first_present(issue, "original", "wrongText"),
                _first_present(issue, "corrected", "correctText"),
                _first_present(issue, "location"),
                _first_present(issue, "quote", "originalExcerpt"),
                _verification_text(issue),
                _first_present(issue, "suggestion", "reason"),
            ],
        )
    return SheetSpec(
        title="错别字",
        headers=[
            "序号",
            "投标文件序号",
            "投标文件名",
            "错误文本",
            "建议改正",
            "位置",
            "原文摘录",
            "证据核验",
            "判断原因",
        ],
        rows=rows,
        widths=[6, 12, 26, 18, 18, 20, 40, 30, 34],
    )


def build_logic_sheet(results: dict, false_positives: set) -> SheetSpec:
    """Sheet 4：逻辑谬误明细。"""
    rows: list[list[Any]] = []
    for index, issue in enumerate(_visible_issues(results, "logic", false_positives), 1):
        rows.append(
            [
                index,
                _first_present(issue, "bid_doc_id"),
                _first_present(issue, "bid_doc_name"),
                _first_present(issue, "description"),
                _first_present(issue, "location_a"),
                _first_present(issue, "quote_a"),
                _first_present(issue, "location_b"),
                _first_present(issue, "quote_b"),
                _first_present(issue, "fallacyReason", "reason"),
                _first_present(issue, "suggestion"),
            ],
        )
    return SheetSpec(
        title="逻辑问题",
        headers=[
            "序号",
            "投标文件序号",
            "投标文件名",
            "标题",
            "位置A",
            "原文A",
            "位置B",
            "原文B",
            "问题原因",
            "修改建议",
        ],
        rows=rows,
        widths=[6, 12, 26, 28, 20, 40, 20, 40, 32, 34],
    )


def build_rejection_workbook(task: dict) -> list[SheetSpec]:
    """构建废标项检查工作簿（4 个 Sheet，对齐参考实现）。"""
    results = task.get("results") or {}
    if not isinstance(results, dict):
        results = {}
    false_positives = set(task.get("false_positives") or [])
    return [
        build_rejection_overview_sheet(task, results, false_positives),
        build_rejection_sheet(results, false_positives),
        build_spelling_sheet(results, false_positives),
        build_logic_sheet(results, false_positives),
    ]


# ==================== 标书查重报表 ====================


def build_duplicate_overview_sheet(task: dict, results: dict) -> SheetSpec:
    """Sheet 1：查重概览（四维度结果数量 + 全局统计）。"""
    status_text = {
        "completed": "已完成",
        "running": "进行中",
        "failed": "失败",
        "pending": "待开始",
    }.get(cell_text(task.get("status")), cell_text(task.get("status")) or "unknown")

    metadata = results.get("metadata") or {}
    toc = results.get("toc") or {}
    content = results.get("content") or {}
    images = results.get("images") or {}
    content_stats = content.get("stats") or {}

    tender_name = cell_text(task.get("tender_filename")) or "-"
    file_names = task.get("filenames") or task.get("file_names") or []
    if not file_names:
        file_names = [m.get("filename", "") for m in (metadata.get("metadata") or []) if isinstance(m, dict)]
    files_text = "、".join(cell_text(name) for name in file_names if name) or "-"

    rows: list[list[Any]] = [
        [
            "元数据对比",
            status_text,
            len(metadata.get("anomalies") or []),
            now_text(),
            tender_name,
            files_text,
            "作者/公司重复、以及 Word RSID / PDF 结构 / WPS 痕迹等文档同源线索",
        ],
        [
            "目录相似度",
            status_text,
            len(toc.get("duplicate_groups") or []) + len(toc.get("similar_groups") or []),
            now_text(),
            tender_name,
            files_text,
            "目录标题重复与近义改写（覆盖率 + 顺序综合评分）",
        ],
        [
            "正文重复",
            status_text,
            len(content.get("duplicate_groups") or []) + len(content.get("similar_groups") or []),
            now_text(),
            tender_name,
            files_text,
            "分句后完全相同 / 高度相似的句子（已按招标原文白名单四级匹配排除正常引用）",
        ],
        [
            "图片重复",
            status_text,
            len(images.get("duplicate_groups") or []),
            now_text(),
            tender_name,
            files_text,
            "跨文件 SHA-256 完全相同的图片",
        ],
    ]

    whitelist = content_stats.get("tender_whitelist") or {}
    notes = [
        f"生成时间：{now_text()}",
        f"任务 ID：{task.get('id', '')}",
        f"识别图片总数：{images.get('total_images', 0)}",
        f"正文句子总量：{content_stats.get('total_sentences', 0)}",
        f"招标原文白名单命中（不计入重复）：{cell_text(whitelist) or '0'}",
        f"招标文件：{tender_name}",
        f"投标文件：{files_text}",
    ]
    return SheetSpec(
        title="查重概览",
        headers=["分析维度", "状态", "结果数量", "更新时间", "招标文件", "投标文件", "说明"],
        rows=rows,
        widths=[14, 10, 12, 20, 26, 38, 52],
        notes=notes,
    )


def build_metadata_sheet(results: dict) -> SheetSpec:
    """Sheet 2：文件元数据与同源疑点。"""
    metadata = results.get("metadata") or {}
    metadatas = metadata.get("metadata") or []
    fingerprints = metadata.get("fingerprints") or []
    same_source_pairs = metadata.get("same_source_pairs") or []

    # 每份文件关联到的同源疑点描述
    hints: dict[str, list[str]] = {}

    def _same_source_text(other: Any, pair: dict) -> str:
        evidence = "；".join(cell_text(item) for item in (pair.get("evidence") or []))
        return f"与 {cell_text(other)} 同源（{risk_label(pair.get('severity'))}）：{evidence}"

    for pair in same_source_pairs:
        if not isinstance(pair, dict):
            continue
        hints.setdefault(cell_text(pair.get("file_a")), []).append(
            _same_source_text(pair.get("file_b"), pair),
        )
        hints.setdefault(cell_text(pair.get("file_b")), []).append(
            _same_source_text(pair.get("file_a"), pair),
        )

    rows: list[list[Any]] = []
    for index, meta in enumerate(metadatas):
        if not isinstance(meta, dict):
            continue
        name = cell_text(meta.get("filename"))
        fingerprint = fingerprints[index] if index < len(fingerprints) else {}
        fingerprint_text = ""
        if isinstance(fingerprint, dict) and fingerprint:
            fingerprint_text = "；".join(
                f"{key}={cell_text(value)}" for key, value in list(fingerprint.items())[:6]
            )
        rows.append(
            [
                name,
                cell_text(meta.get("author")),
                cell_text(meta.get("company")),
                cell_text(meta.get("title")),
                cell_text(meta.get("created")),
                cell_text(meta.get("modified")),
                cell_text(meta.get("last_saved_by")),
                "；".join(hints.get(name, [])) or fingerprint_text,
            ],
        )

    anomalies = metadata.get("anomalies") or []
    notes = ["【元数据异常项】"]
    if anomalies:
        for item in anomalies:
            if isinstance(item, dict):
                notes.append(
                    f"- {cell_text(item.get('description'))}：{cell_text(item.get('value'))}",
                )
    else:
        notes.append("- 未发现作者/公司重复等基础异常")

    # 同源疑点单独成段：这是围标串标线索里最需要人工确认的部分，
    # 放在表下方的说明区比塞进单元格更易读（易标只在单元格里给一句摘要）
    notes.append("【文档同源疑点】")
    if same_source_pairs:
        for pair in same_source_pairs:
            if not isinstance(pair, dict):
                continue
            evidence_parts = [cell_text(item) for item in (pair.get("evidence") or [])]
            nodes = "；".join(evidence_parts)
            notes.append(
                f"- {cell_text(pair.get('file_a'))} ↔ {cell_text(pair.get('file_b'))}"
                f"（{risk_label(pair.get('severity'))}）：{nodes}",
            )
    else:
        notes.append("- 未发现同源痕迹")
    notes.append("说明：同源痕迹仅表示文件由同一办公环境或同一底稿派生，不构成串标结论，请人工核实。")

    return SheetSpec(
        title="元数据",
        headers=[
            "文件名",
            "作者",
            "公司/分类",
            "标题",
            "创建时间",
            "修改时间",
            "最后保存者",
            "同源疑点 / 指纹",
        ],
        rows=rows,
        widths=[30, 16, 20, 26, 22, 22, 20, 60],
        notes=notes,
    )


def build_toc_sheet(results: dict) -> SheetSpec:
    """Sheet 3：目录重复明细（重复 + 近义改写）。"""
    toc = results.get("toc") or {}
    rows: list[list[Any]] = []
    index = 0

    for group in toc.get("duplicate_groups") or []:
        if not isinstance(group, dict):
            continue
        index += 1
        titles = group.get("common_titles") or []
        rows.append(
            [
                index,
                "目录重复",
                f"{len(titles) or group.get('common_count', 0)} 个共同标题",
                as_ratio(group.get("similarity")),
                f"{cell_text(group.get('file_a'))} ↔ {cell_text(group.get('file_b'))}",
                "、".join(cell_text(item) for item in titles[:20]),
            ],
        )

    for group in toc.get("similar_groups") or []:
        if not isinstance(group, dict):
            continue
        index += 1
        rows.append(
            [
                index,
                "近义改写",
                f"{cell_text(group.get('title_a'))} ↔ {cell_text(group.get('title_b'))}",
                as_ratio(group.get("similarity")),
                f"{cell_text(group.get('file_a'))} ↔ {cell_text(group.get('file_b'))}",
                cell_text(group.get("criterion")),
            ],
        )

    return SheetSpec(
        title="目录重复",
        headers=["序号", "类型", "标题", "相似度", "涉及文件", "明细"],
        rows=rows,
        widths=[6, 12, 46, 12, 40, 44],
        percent_columns=(4,),
        notes=["【目录覆盖率 + 顺序综合评分排名】"] + [
            f"{cell_text(pair.get('file_a'))} ↔ {cell_text(pair.get('file_b'))}："
            f"综合 {cell_text(pair.get('score'))}（标题 {cell_text(pair.get('title_overlap'))} / "
            f"路径 {cell_text(pair.get('path_overlap'))} / 顺序 {cell_text(pair.get('order_similarity'))}）"
            f" → 风险 {risk_label(pair.get('risk'))}"
            for pair in (toc.get("pairwise") or [])[:20]
            if isinstance(pair, dict)
        ],
    )


def build_pairwise_sheet(results: dict) -> SheetSpec:
    """Sheet 4：文件级相似度矩阵明细。"""
    toc = results.get("toc") or {}
    rows: list[list[Any]] = []
    for pair in toc.get("pairwise") or []:
        if not isinstance(pair, dict):
            continue
        rows.append(
            [
                cell_text(pair.get("file_a")),
                cell_text(pair.get("file_b")),
                as_ratio(pair.get("score")),
                as_ratio(pair.get("title_overlap")),
                as_ratio(pair.get("path_overlap")),
                as_ratio(pair.get("order_similarity")),
                pair.get("shared_count", 0),
                risk_label(pair.get("risk")),
            ],
        )
    return SheetSpec(
        title="文件相似度",
        headers=[
            "文件A",
            "文件B",
            "综合相似度",
            "标题重合度",
            "路径重合度",
            "顺序相似度",
            "共享目录数量",
            "风险等级",
        ],
        rows=rows,
        widths=[30, 30, 14, 14, 14, 14, 14, 12],
        percent_columns=(3, 4, 5, 6),
        notes=[
            "综合相似度 = 路径重合度 × 0.45 + 标题重合度 × 0.35 + 顺序相似度 × 0.20",
            "风险分级：≥75% 高 / ≥55% 中 / ≥35% 低",
        ],
    )


def build_content_sheet(results: dict) -> SheetSpec:
    """Sheet 5：正文重复句子（完全相同 + 高度相似）。"""
    content = results.get("content") or {}
    rows: list[list[Any]] = []
    index = 0

    for group in content.get("duplicate_groups") or []:
        if not isinstance(group, dict):
            continue
        index += 1
        occurrences = group.get("occurrences") or []
        names = []
        positions = []
        for item in occurrences:
            if not isinstance(item, dict):
                continue
            name = cell_text(item.get("filename"))
            if name and name not in names:
                names.append(name)
            positions.append(f"{name}#{item.get('sentence_idx', '')}")
        sentence = cell_text(group.get("sentence"))
        rows.append(
            [
                index,
                "完全相同",
                sentence,
                len(names),
                "、".join(names),
                "、".join(positions),
                len(sentence),
            ],
        )

    for group in content.get("similar_groups") or []:
        if not isinstance(group, dict):
            continue
        index += 1
        rows.append(
            [
                index,
                f"高度相似（{cell_text(group.get('similarity'))}）",
                f"{cell_text(group.get('sentence_a'))} ↔ {cell_text(group.get('sentence_b'))}",
                2,
                f"{cell_text(group.get('file_a'))}、{cell_text(group.get('file_b'))}",
                "",
                len(cell_text(group.get("sentence_a"))),
            ],
        )

    stats = content.get("stats") or {}
    notes = [
        f"总句子数：{stats.get('total_sentences', 0)}；完全相同重复：{stats.get('duplicate_count', 0)}；"
        f"高度相似：{stats.get('similar_count', 0)}",
        "说明：已按招标原文白名单（exact / strict / skeleton / near 四级）排除正常引用招标原文的句子。",
    ]
    return SheetSpec(
        title="重复句子",
        headers=["序号", "类型", "重复内容", "涉及文件数", "涉及文件", "各文件出现位置", "字数"],
        rows=rows,
        widths=[6, 18, 70, 12, 40, 40, 8],
        notes=notes,
    )


def build_image_sheet(results: dict) -> SheetSpec:
    """Sheet 6：重复图片（含章节路径 / 序号 / 前一句上下文）。"""
    images = results.get("images") or {}
    rows: list[list[Any]] = []
    for index, group in enumerate(images.get("duplicate_groups") or [], 1):
        if not isinstance(group, dict):
            continue
        occurrences = group.get("occurrences") or []
        names: list[str] = []
        counts: dict[str, int] = {}
        locations: list[str] = []
        contexts: list[str] = []
        for item in occurrences:
            if not isinstance(item, dict):
                continue
            name = cell_text(item.get("filename"))
            if name and name not in names:
                names.append(name)
            counts[name] = counts.get(name, 0) + 1
            directory = cell_text(item.get("directory"))
            positions = cell_text(item.get("image_index"))
            locations.append(f"{name}：{directory or '未识别章节'}·第{positions or '?'}张")
            previous = cell_text(item.get("previous_sentence"))
            if previous:
                contexts.append(previous)
        rows.append(
            [
                index,
                cell_text(group.get("hash")),
                "、".join(names),
                "；".join(f"{name}×{counts[name]}" for name in names),
                "；".join(locations),
                "｜".join(contexts[:4]),
            ],
        )

    notes = [
        f"识别图片总数：{images.get('total_images', 0)}；跨文件重复图片组："
        f"{len(images.get('duplicate_groups') or [])}",
        "说明：docx 的章节路径与前一句来自文档结构解析；PDF 只能定位到页码（文本层与图像对象无顺序关系）。",
    ]
    return SheetSpec(
        title="重复图片",
        headers=["序号", "图片Hash", "涉及文件", "各文件出现次数", "出现位置", "图片前文"],
        rows=rows,
        widths=[6, 22, 34, 26, 52, 60],
        notes=notes,
    )


def build_duplicate_workbook(task: dict) -> list[SheetSpec]:
    """构建标书查重工作簿（6 个 Sheet，对齐参考实现）。"""
    results = task.get("results") or {}
    if not isinstance(results, dict):
        results = {}
    return [
        build_duplicate_overview_sheet(task, results),
        build_metadata_sheet(results),
        build_toc_sheet(results),
        build_pairwise_sheet(results),
        build_content_sheet(results),
        build_image_sheet(results),
    ]


# ==================== 写盘 ====================


def _safe_sheet_name(title: str, used: set[str]) -> str:
    """生成合法且唯一的 Sheet 名（Excel 限 31 字符且禁用 : \\ / ? * [ ]）。"""
    name = _SHEET_NAME_BAD_RE.sub("_", str(title or "").strip()) or "Sheet"
    name = name[:MAX_SHEET_NAME_CHARS]
    if name not in used:
        used.add(name)
        return name
    for suffix in range(2, 100):
        candidate = f"{name[: MAX_SHEET_NAME_CHARS - 3]}({suffix})"
        if candidate not in used:
            used.add(candidate)
            return candidate
    fallback = f"Sheet{len(used) + 1}"
    used.add(fallback)
    return fallback


def _write_row(ws: Any, row_index: int, values: list[Any]) -> None:
    """写入一行：数字保持数值（供筛选/求和），文本走长度保护，`=` 开头强制按文本写。"""
    for column_index, value in enumerate(values, 1):
        cell = ws.cell(row=row_index, column=column_index)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            text = cell_text(value)
            cell.value = text
            if text.startswith(("=", "+", "-", "@")):
                # openpyxl 会把以 = 开头的字符串当公式；强制标记为字符串，避免 Excel 打开时报错
                cell.data_type = "s"
        else:
            cell.value = value


def _auto_widths(spec: SheetSpec) -> list[int]:
    """按表头与前 200 行内容估算列宽（中文按 2 个字符宽度折算）。"""
    if spec.widths:
        return spec.widths
    widths: list[int] = []
    for column_index, header in enumerate(spec.headers):
        longest = _display_width(cell_text(header))
        for row in spec.rows[:200]:
            if column_index < len(row):
                longest = max(longest, _display_width(cell_text(row[column_index])))
        widths.append(min(max(longest + 2, 8), 60))
    return widths


def _display_width(text: str) -> int:
    return sum(2 if ord(char) > 0x2E80 else 1 for char in text)


def write_workbook(sheets: list[SheetSpec], target: Path | str) -> Path:
    """把 SheetSpec 列表写成 xlsx 文件，返回落盘路径。"""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError as exc:  # pragma: no cover - 依赖缺失时给出明确指引
        raise ImportError("导出 Excel 需要安装 openpyxl") from exc

    if not sheets:
        raise ValueError("没有可导出的内容")

    workbook = Workbook()
    workbook.remove(workbook.active)
    used_names: set[str] = set()

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="4472C4")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    body_align = Alignment(vertical="top", wrap_text=True)

    for spec in sheets:
        ws = workbook.create_sheet(_safe_sheet_name(spec.title, used_names))
        _write_row(ws, 1, list(spec.headers))
        for cell in ws[1]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_align

        for offset, row in enumerate(spec.rows):
            _write_row(ws, offset + 2, list(row))
            for cell in ws[offset + 2]:
                cell.alignment = body_align

        last_data_row = 1 + len(spec.rows)
        for column_index in spec.percent_columns:
            for row_index in range(2, last_data_row + 1):
                cell = ws.cell(row=row_index, column=column_index)
                if isinstance(cell.value, (int, float)) and not isinstance(cell.value, bool):
                    cell.number_format = "0.00%"

        widths = _auto_widths(spec)
        for column_index, width in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(column_index)].width = width

        if spec.rows:
            ws.auto_filter.ref = f"A1:{get_column_letter(len(spec.headers))}{last_data_row}"
        ws.freeze_panes = "A2"

        for note in spec.notes:
            last_data_row += 1
            _write_row(ws, last_data_row, [f"※ {note}"])

    target_path = Path(target)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(str(target_path))
    return target_path
