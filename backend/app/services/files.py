import asyncio
import hashlib
import logging
import os
import re
from pathlib import Path
import shutil
import threading
import time
from typing import Any
from xml.etree import ElementTree
import zipfile

from ..config import (
    OCR_ENABLED,
    OCR_ENGINE,
    OCR_MAX_CONCURRENT,
    OCR_MAX_FILE_SIZE_MB,
    OCR_TIMEOUT,
    PARSE_TASK_RETENTION,
    PARSE_TASK_STALE_MINUTES,
    PDF_TABLE_EXTRACT_ENABLED,
    PPT_COM_ENABLED,
    PPT_COM_TIMEOUT,
    PROJECTS_DATA_DIR,
)
from ..db import get_db
from .docx_extractor import extract_docx_with_fallback as _extract_docx_docx


logger = logging.getLogger(__name__)

# ===== 文件解析结果缓存 =====
# 缓存文件路径 -> (文件哈希, 解析时间, 解析结果)
_parse_result_cache: dict[str, tuple[str, float, str]] = {}
_parse_cache_lock = threading.Lock()
_PARSE_CACHE_TTL = 300  # 秒（5分钟）
_PARSE_CACHE_MAX = 20  # 最多缓存20个文件的解析结果
# BUG-FIX-74c: 缓存支持的扩展名集中定义，避免读写两侧列表不一致
# （此前写入侧遗漏 .doc/.xls/.ppt/.html/.htm 导致这些格式永远不缓存）
_PARSE_CACHEABLE_EXTS: tuple[str, ...] = (
    ".docx", ".pdf", ".xlsx", ".txt", ".pptx", ".md",
    ".doc", ".xls", ".ppt", ".html", ".htm",
)

# ===== 解析能力「单真相」（ENHANCE 2026-10-05，技术债 TD3）=====
# 此前路由层 routers/files.ALLOWED_EXTENSIONS 与服务层 _do_extract_text 的分支是
# 两份手工维护的真相，历史上已经出现过「服务层早已支持 .wps 与图片 OCR、路由层
# 一直不放行」的功能入口封死（BUG-FIX 2026-09-29 靠测试才发现）。现由服务层导出
# 唯一真相，路由层直接引用，并由测试钉住「集合相等 + 每个放行扩展名都能落到真实
# 提取分支（不落到 raise 分支）」。
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset({
    ".docx", ".pdf", ".doc", ".xlsx", ".xls", ".txt", ".pptx", ".ppt",
    ".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".webp",
    ".html", ".htm", ".md", ".wps",
})

# 图片类扩展名：走 OCR 链路（同时供 guess_file_role 判定扫描件）
IMAGE_EXTENSIONS: frozenset[str] = frozenset({
    ".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".webp",
})


class UnsupportedFileFormatError(ValueError):
    """服务层无法处理的扩展名。

    路由层应在入口拦截并返回 400；此处是服务层被其它入口（Celery 任务、
    知识库管线、直接函数调用）绕过时的最后一道防线。
    """


def normalize_file_role(file_role: str, filename: str = "") -> str:
    """归一化外部传入的 file_role（BUG-FIX 2026-10-05 B2 字段错位）。

    POST /files 的 ``file_role`` 表单字段此前不做任何校验：任意字符串
    （``"MAIN"`` / ``"主文件"`` / ``"main "``）都会原样写进
    ``uploaded_files.file_role``，而 ``_pick_main_file`` 只认精确值 ``'main'``
    —— 项目从此没有任何主文件，静默回退「按 id 取首个 success 文件」，下游
    摘要/评分点/目录取错文本且接口仍返回 200，用户与前端均无从察觉。

    策略（向后兼容优先，不拒绝请求、不改接口形状）：
      - ``main`` / ``supporting``（大小写与首尾空白容错）→ 归一化后返回；
      - 空值 → 走 :func:`guess_file_role` 自动检测（与原行为一致）；
      - 其它非法值 → 记 warning 后同样回退自动检测，不抛 400。
    """
    raw = (file_role or "").strip().lower()
    if raw in ("main", "supporting"):
        return raw
    if raw:
        logger.warning(
            "非法的 file_role=%r（文件=%s），回退为自动角色检测", file_role, filename
        )
    return guess_file_role(filename) if filename else "main"



# R-1 2026-10-12：格式提取器已搬迁至 services/extractors/（纯搬迁，不改行为）。
# 以下回填 files 模块的**公共访问面** —— 调度器 _do_extract_text 与既有测试都以
# `files.<name>` / `from ...files import <name>` 访问这些名字，re-export 保证访问面不变。
# 注意：提取器内部互相调用会经 extractors._late() 回到本模块属性解析，
# 因此测试 monkeypatch `files.<name>` 依旧生效（与搬迁前同语义）。
from .extractors._notices import (
    EXTRACT_DOC_NOTICE,
    EXTRACT_ERROR_MAX_LEN,
    EXTRACT_ERROR_PREFIXES,
    EXTRACT_PPT_NOTICE,
    EXTRACT_WPS_NOTICE,
    EXTRACTION_NOTICES,
    _EXTRACT_ERROR_PREFIXES_TUPLED,
    is_extraction_notice,
)
# 标书智能体（一）引入：解析质量校验，非空文件却解析出空/乱码时显式告警，
# 填补此前"解析失败静默返回空"的缺口。
from .extractors.parse_quality import assess_extraction as _assess_extraction
from .extractors import (
    _LEGACY_OLE_EXTS,
    _OLE2_MAGIC,
    _PPT_FALLBACK_HINT,
    _PPT_OFFICE_PROBE,
    _extract_doc,
    _extract_docx,
    _extract_html,
    _extract_image_ocr,
    _extract_legacy_ole_chain,
    _extract_pdf,
    _extract_pdf_impl,
    _extract_pdf_pdfplumber,
    _extract_ppt,
    _extract_pptx,
    _extract_pptx_raw_xml,
    _extract_wps,
    _extract_xls,
    _extract_xlsx_openpyxl,
    _extract_xlsx_raw_xml,
    _format_pdf_table_row,
    _get_paddleocr,
    _is_ole2_file,
    _mineru_lock,
    _mineru_parser_instance,
    _ocr_image_size_exceeded,
    _ocr_semaphore,
    _ocr_with_mineru,
    _ocr_with_paddle,
    _ocr_with_tesseract,
    _paddleocr_instance,
    _paddleocr_lock,
    _ppt_com_available,
    _ppt_com_child_target,
    _ppt_com_extract_sync,
    _ppt_com_terminate,
    _ppt_via_powerpoint_com,
    _xls_cell_value,
)
# BUG-FIX 2026-10-04（P2 并发竞态）：save_upload 经 asyncio.to_thread 走线程池，
# 同一项目并发上传同名文件时，"存在性检查→重命名→write_bytes" 非原子：两个线程
# 可能同时通过 exists() 判定、选中同一目标路径与同一文本文件名（f"{filename}.txt"），
# 后写者覆盖前者的原始文件与提取文本，DB 却生成两行 → 解析结果丢失/字段错位。
# 现按 project_id 维护写入互斥锁，仅锁住"命名+落盘"临界区（不含耗时的文本提取），
# 既消除竞态又不 serialize 解析本身。不同项目互不阻塞。
#
# BUG-FIX 2026-10-11（Memory Leak）：此前 _upload_write_locks 字典永不清理 ——
# 长期运行的服务器处理完上万个项目后会累积上万把不再使用的 threading.Lock 对象。
# 现追踪每把锁的最后访问时间，超过上限（_UPLOAD_WRITE_LOCK_MAX）且闲置超时
# （_UPLOAD_WRITE_LOCK_IDLE_SECS）时惰性淘汰最旧的（下次创建新锁时触发清理）。
_upload_write_locks: dict[int, tuple[threading.Lock, float]] = {}
_upload_write_locks_guard = threading.Lock()
_UPLOAD_WRITE_LOCK_MAX = 500
_UPLOAD_WRITE_LOCK_IDLE_SECS = 3600  # 闲置超过 1 小时的锁可清理


def _get_upload_write_lock(project_id: int) -> threading.Lock:
    """获取（惰性创建）指定项目的上传写入互斥锁，带 LRU 清理。

    BUG-FIX 2026-10-11（Memory Leak）：每次调用更新最后访问时间戳；当锁数超过
    ``_UPLOAD_WRITE_LOCK_MAX`` 时清理闲置超时的最旧锁，使长期运行的服务器
    不会因累积废弃锁对象而缓慢泄漏内存。
    """
    now = time.monotonic()
    with _upload_write_locks_guard:
        entry = _upload_write_locks.get(project_id)
        if entry is None:
            # 创建新锁前，若字典已满先清理最旧的闲置锁
            if len(_upload_write_locks) >= _UPLOAD_WRITE_LOCK_MAX:
                _lru_cleanup_upload_write_locks_locked(now)
            lock = threading.Lock()
            _upload_write_locks[project_id] = (lock, now)
            return lock
        lock, _ts = entry
        # 更新最后访问时间戳（锁本身只含一个对象，赋值 tuple 成本可忽略）
        _upload_write_locks[project_id] = (lock, now)
        return lock


def _lru_cleanup_upload_write_locks_locked(now: float) -> None:
    """清理闲置超时的废弃写入锁（调用方需持有 _upload_write_locks_guard）"""
    if not _upload_write_locks:
        return
    to_delete = [
        pid
        for pid, (_lock, ts) in _upload_write_locks.items()
        if now - ts > _UPLOAD_WRITE_LOCK_IDLE_SECS
    ]
    for pid in to_delete:
        del _upload_write_locks[pid]
    logger.debug(
        "[upload_write_locks] LRU 清理：淘汰 %d 个闲置锁，剩余 %d 个",
        len(to_delete), len(_upload_write_locks),
    )


def _get_file_hash(file_path: str) -> str:
    """计算文件的快速哈希（基于文件大小、修改时间与前 4KB 内容）"""
    try:
        stat = os.stat(file_path)
        raw = f"{stat.st_size}_{stat.st_mtime_ns}"
        digest = hashlib.md5(raw.encode()).hexdigest()
        # BUG-FIX 2026-10-08（CF-03 哈希碰撞）：仅靠 size+mtime_ns 在「同大小 +
        # 同一 mtime 刻度」的两个不同文件上必然碰撞（全量回归实测复现：
        # 两个 9 字节临时文件连续创建命中同一刻度 → 哈希相同 → 解析缓存把
        # A 文件的解析结果错配给 B 文件）。旧兜底只在 st_mtime_ns==0 时追加
        # 内容哈希，覆盖不了 mtime 刻度粒度粗的文件系统。现无条件追加前 4KB
        # 内容哈希：4KB 读取开销可忽略；哈希值变化仅使解析缓存一次性失效，无
        # 持久化兼容性影响（该哈希不落库，仅作缓存键）。
        with open(file_path, "rb") as fh:
            head = fh.read(4096)
        digest = hashlib.md5(digest.encode() + head).hexdigest()
        return digest
    except (OSError, FileNotFoundError):
        return ""


def _find_duplicate_upload(project_id: int, content: bytes) -> dict | None:
    """按内容 SHA-256 查找本项目已存在的重复上传（G8 去重）。

    Args:
        project_id: 项目 ID
        content: 待上传文件字节

    Returns:
        命中则返回 {id, filename, path, status}（dict，sqlite3.Row 物化），
        未命中/异常返回 None。仅比较同项目且同文件大小的历史文件（先按
        os.stat 大小过滤再做全量哈希，避免每次上传读遍所有旧文件）。
    """
    if not content:
        return None
    try:
        digest = hashlib.sha256(content).hexdigest()
        target_size = len(content)
        with get_db(readonly=True) as conn:
            rows = conn.execute(
                "SELECT id, filename, path, status FROM uploaded_files "
                "WHERE project_id = ? ORDER BY id",
                (project_id,),
            ).fetchall()
        for row in rows:
            fp = str(row["path"] or "")
            if not fp:
                continue
            try:
                st = os.stat(fp)
                if st.st_size != target_size:
                    continue
                with open(fp, "rb") as fh:
                    if hashlib.sha256(fh.read()).hexdigest() != digest:
                        continue
            except (OSError, FileNotFoundError):
                continue
            # sqlite3.Row 惰性解引用：在 with 块外取值需物化
            return {
                "id": row["id"],
                "filename": row["filename"],
                "path": fp,
                "status": row["status"],
            }
        return None
    except Exception:
        return None


def _clear_parse_cache(file_path: str = "") -> None:
    """清除文件解析缓存"""
    with _parse_cache_lock:
        if file_path:
            _parse_result_cache.pop(file_path, None)
        else:
            _parse_result_cache.clear()


# ===== 文件解析任务持久化追踪 =====


def prune_parse_tasks(project_id: int | None = None, max_rows: int | None = None) -> int:
    """按「每项目保留最新 N 条」裁剪 file_parse_tasks（遗留修复 2026-10-06 L3）

    每次上传与每次「重新提取」都会新增一行，此前**没有任何清理策略**：任务表只增
    不减，长期项目会持续膨胀（表名叫 tasks 却从不被清理，beat 的
    ``cleanup_old_results`` 也不碰它）。

    裁剪规则：只淘汰**终态行**（completed / failed），在途的 processing 行一律保留，
    避免把正在解析的任务从状态端点上抹掉。

    Args:
        project_id: 只裁剪指定项目；None 表示裁剪所有项目（beat 定期任务用）
        max_rows: 保留上限；None 时取配置 ``PARSE_TASK_RETENTION``（0 = 不限制）

    Returns:
        实际删除的行数
    """
    keep = PARSE_TASK_RETENTION if max_rows is None else max_rows
    if keep <= 0:
        return 0

    deleted = 0
    try:
        from ..db import get_db

        with get_db() as conn:
            if project_id is None:
                project_ids = [
                    r[0] for r in conn.execute(
                        "SELECT DISTINCT project_id FROM file_parse_tasks"
                    ).fetchall()
                ]
            else:
                project_ids = [project_id]

            for pid in project_ids:
                cur = conn.execute(
                    """DELETE FROM file_parse_tasks
                       WHERE project_id = ?
                         AND status IN ('completed', 'failed')
                         AND id NOT IN (
                             SELECT id FROM file_parse_tasks
                             WHERE project_id = ?
                             ORDER BY id DESC LIMIT ?
                         )""",
                    (pid, pid, keep),
                )
                deleted += int(cur.rowcount or 0)
    except Exception as e:
        logger.warning("裁剪文件解析任务失败（不影响核心功能）: %s", e)
    return deleted


def _table_has_column(conn, table: str, column: str) -> bool:
    """探测表是否具备指定列（用于老库 schema 兼容分支）。

    比 ``try: SELECT col ... except sqlite3.OperationalError`` 更精确：不会把
    其他类型的 OperationalError（锁超时、磁盘 I/O）误判成「缺列」而走降级路径。
    探测失败一律按「无该列」处理（调用方走保守分支，不做可能误判的回收）。
    """
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    except Exception as _e:  # pragma: no cover - 防御性
        logger.debug("探测 %s 列结构失败（按无该列处理）: %s", table, _e)
        return False
    return any(str(r[1]) == column for r in rows)


def reclaim_stale_parse_state(*, stale_minutes: int | None = None) -> dict[str, int]:
    """回收长时间停留在 processing 的解析状态（beat 每小时调用一次）。

    GAP-FIX 2026-10-17（孤儿 processing 永不回收）：图片上传（R-3）走
    「broker 派发 → worker 收尾」，下列任一情形都会让行**永久停在 processing**：

      1. dispatch_task 把消息投给了 broker，但 worker 未运行 / 队列消息丢失；
      2. worker 进程被 SIGKILL（Celery time_limit 超时、容器重启），
         except 分支来不及执行，终态 UPDATE 不会发生；
      3. 任务重试耗尽前 worker 崩溃。

    此前没有任何回收路径：

      - ``prune_parse_tasks`` 明确「在途 processing 行一律保留」；
      - ``cleanup_old_results``（每小时 beat）只清 task_lock / task_snapshot /
        generation_tasks，再加一次 prune，不碰这两张表；
      - 本文件 M-4 注释还引用了一个**全仓并不存在**的 ``_recover_stale_tasks``，
        把「启动期会回收」当成了事实陈述。

    后果：前端 ``classifyUploadResult`` 判为「解析中」，进度条永久卡住，用户只能
    删除重传。本函数把超时的 processing 收敛为 failed 并写明原因，把「永久等待」
    变成「可自助重试」。

    时钟说明（F-H FIX 2026-10-26）：判陈旧一律用 ``updated_at``（最近一次状态
    变更时间），不再降级用 ``created_at``。老库缺该列时**跳过** uploaded_files
    回收 —— 因为「created_at 很老 + 刚被 async-reparse 改回 processing」正是
    误判场景本身；宁可少回收一轮，也不误杀合法在途任务。

    幂等与可自愈：重复调用只影响仍在 processing 且超时的行；迟到的 worker 完成后
    照常把终态覆盖为 success（``_persist_file_extraction`` 的终态 UPDATE 不带
    ``status='processing'`` 过滤），因此即便阈值设得过短导致误判，也会在 worker
    追上后自动纠正。

    Args:
        stale_minutes: 覆盖配置 ``PARSE_TASK_STALE_MINUTES``；<= 0 表示关闭回收。

    Returns:
        {"uploaded_files_failed": int, "parse_tasks_failed": int}
    """
    minutes = PARSE_TASK_STALE_MINUTES if stale_minutes is None else stale_minutes
    if minutes <= 0:
        return {"uploaded_files_failed": 0, "parse_tasks_failed": 0}

    stale_msg = (
        f"后台解析未在 {minutes} 分钟内完成（worker 未运行或任务已丢失），"
        "已自动标记失败，请重新解析"
    )[:500]
    result: dict[str, int] = {"uploaded_files_failed": 0, "parse_tasks_failed": 0}
    try:
        with get_db() as conn:
            # BUG-FIX 2026-10-17（GAP reclaim 误判）：uploaded_files 只有 created_at，
            # 此前用 created_at 判 stale 会让 async-reparse 路径立即被误判：
            # 文件上传后 created_at=T0，async-reparse 触发时 _persist_file_extraction
            # 把行改回 status='processing' 但 created_at 不变 → beat 下一轮立即判定
            # 「过期」并标记 failed，worker 正常完成后才覆盖回来。现改用 updated_at：
            #   - 有 updated_at 列（新 schema）→ 用 updated_at 判 stale（正确语义：
            #     最近一次状态变更时间，async-reparse 刚改回 processing 时 updated_at=现在）
            #
            # F-H FIX 2026-10-26（老库降级仍有误判风险）：此前缺列时**降级用
            # created_at 继续回收**，而这正是误判场景本身——老库上 async-reparse
            # 改回 processing 时 created_at 仍是上传时刻，一条合法在途任务会在
            # 下一轮 beat 被直接判死。现在缺列时**跳过 uploaded_files 回收**
            # （宁可不回收也不要误杀），只保留 file_parse_tasks 回收（该表 DDL
            # 自带 updated_at，不受老库影响），并给出可执行的迁移指引。
            if _table_has_column(conn, "uploaded_files", "updated_at"):
                cur = conn.execute(
                    """UPDATE uploaded_files
                       SET status = 'failed', message = ?
                       WHERE status = 'processing'
                         AND datetime(updated_at, ?) < datetime('now', 'localtime')""",
                    (stale_msg, f"{int(minutes)} minutes"),
                )
                result["uploaded_files_failed"] = int(cur.rowcount or 0)
            else:
                logger.warning(
                    "uploaded_files 缺 updated_at 列，本轮跳过孤儿 processing 回收"
                    "（避免用 created_at 误杀 async-reparse 的在途任务）—— "
                    "请执行 DB 迁移（init_db 会补列并回填 updated_at=created_at）。"
                )

            cur = conn.execute(
                """UPDATE file_parse_tasks
                   SET status = 'failed', progress = 50, error_message = ?,
                       completed_at = datetime('now', 'localtime'),
                       updated_at = datetime('now', 'localtime')
                   WHERE status = 'processing'
                     AND datetime(updated_at, ?) < datetime('now', 'localtime')""",
                (stale_msg, f"{int(minutes)} minutes"),
            )
            result["parse_tasks_failed"] = int(cur.rowcount or 0)
    except Exception as e:
        logger.warning("回收孤儿解析状态失败（不影响核心功能）: %s", e)
        return {"uploaded_files_failed": 0, "parse_tasks_failed": 0}

    if result["uploaded_files_failed"] or result["parse_tasks_failed"]:
        logger.info("回收孤儿解析状态: %s", result)
    return result


def _create_parse_task(project_id: int, filename: str, file_type: str = ""):
    """创建文件解析任务记录（持久化到数据库）"""
    try:
        from ..db import get_db

        with get_db() as conn:
            conn.execute(
                """INSERT INTO file_parse_tasks
                   (project_id, filename, file_type, status, progress, total_files,
                    completed_files, started_at, updated_at)
                   VALUES (?, ?, ?, 'processing', 0, 1, 0,
                           datetime('now', 'localtime'), datetime('now', 'localtime'))""",
                (project_id, filename, file_type),
            )
        logger.info("文件解析任务已创建: project=%d, file=%s", project_id, filename)
    except Exception as e:
        # 2026-10-11 加固（P-3）：旧实现仅 warning 后 return，导致任务行缺失、
        # 状态端点无解析痕迹、前端进度条永远卡在 processing。
        # 现改为 raise 让上游感知任务行缺失——若 DB 写入失败，整个上传流程应
        # 失败而非静默继续（后续 _update_parse_task 会 UPDATE 0 行，终态丢失）。
        logger.error("创建文件解析任务失败（将中断上传流程）: %s", e)
        raise

    # 遗留修复 2026-10-06（L3）：写入后即时裁剪，把表规模压在配置上限内。
    # 放在写入之后（而不是之前）是为了保证本次任务行一定存在。
    if PARSE_TASK_RETENTION > 0:
        prune_parse_tasks(project_id)


def _update_parse_task(
    project_id: int,
    status: str,
    progress: int = 0,
    error_message: str = "",
    filename: str = "",
):
    """更新文件解析任务状态

    BUG-FIX 2026-09-18（高）：原实现只按 `project_id + status='processing'` 批量更新，
    同一项目并发上传时 A 文件的完成/失败会串写到 B 文件的任务行（B 被误标为
    completed 或 failed，进度与错误信息全部错位）。现新增 `filename` 精确过滤，
    只更新本次上传对应的任务行；未传 filename 时保持旧的批量语义以兼容既有调用。
    """
    try:
        from ..db import get_db

        where = "project_id = ? AND status = 'processing'"
        tail: list = [project_id]
        if filename:
            where += " AND filename = ?"
            tail.append(filename)

        with get_db() as conn:
            if status == "completed":
                # BUG-FIX 2026-10-15（M-1 · 解析警告静默丢弃）：completed 分支的
                # SQL 从不写 error_message，而调用方（save_upload / re_extract /
                # _persist_file_extraction）在「解析成功但正文为空」时算出的
                # EMPTY_TEXT_MESSAGE 正是按第四参数传进来的 —— 于是这条警告
                # 只落到文件行，任务行永远显示「completed 且无信息」。
                # GET /tasks/files/status 的聚合口径（recent_completed 全部
                # 无提示）与前端状态条因此在同一缺陷上失明。
                # 说明：`error_message` 是本表唯一的消息通道，非致命警告也走它；
                # 无提示时写入空串，保持「行内无消息 == 一切正常」的读法一致。
                conn.execute(
                    f"""UPDATE file_parse_tasks
                       SET status = ?, progress = ?, completed_files = 1,
                           error_message = ?,
                           completed_at = datetime('now', 'localtime'),
                           updated_at = datetime('now', 'localtime')
                       WHERE {where}""",
                    (status, progress, error_message[:500], *tail),
                )
            elif status == "failed":
                conn.execute(
                    f"""UPDATE file_parse_tasks
                       SET status = ?, progress = ?, error_message = ?,
                           completed_at = datetime('now', 'localtime'),
                           updated_at = datetime('now', 'localtime')
                       WHERE {where}""",
                    (status, progress, error_message[:500], *tail),
                )
            else:
                conn.execute(
                    f"""UPDATE file_parse_tasks
                       SET status = ?, progress = ?,
                           updated_at = datetime('now', 'localtime')
                       WHERE {where}""",
                    (status, progress, *tail),
                )
    except Exception as e:
        logger.warning("更新文件解析任务失败: %s", e)


def _force_parse_task_failed(project_id: int, filename: str, error_message: str) -> None:
    """把指定文件最近的解析任务行**强制**改写为 failed（可覆盖已写终态的行）。

    BUG-FIX 2026-10-15（F-1/F-2 解析失败静默成功）：`_update_parse_task` 的
    WHERE 恒带 `status = 'processing'`，只能覆盖**在途**行。但
    「写提取文本失败」这条链路里，任务行往往在写盘**之前**就已经被写成
    `completed`（save_upload 在 extract 成功分支先写 completed，随后才落 .txt），
    此时再用 `_update_parse_task("failed")` 更新会 **UPDATE 0 行**，终态悄悄丢失，
    状态端点与前端进度条因此仍显示「已成功」。

    本函数不带 `status = 'processing'` 过滤，专供「先前误判成功、事后发现失败」
    这类需要覆盖终态的场景。仍按 `filename` 精确过滤（与 2026-09-18 修的并发
    串写同一不变式），只碰本次上传/提取对应的那一行，避免误伤同项目其它文件的
    历史任务行。异常只告警：任务行是本表的「可观测性通道」，写不进去不该让
    上传主流程本身失败。
    """
    try:
        from ..db import get_db

        with get_db() as conn:
            conn.execute(
                """UPDATE file_parse_tasks
                   SET status = 'failed', progress = 50, error_message = ?,
                       completed_at = datetime('now', 'localtime'),
                       updated_at = datetime('now', 'localtime')
                   WHERE project_id = ? AND filename = ?""",
                (error_message[:500], project_id, filename),
            )
    except Exception as e:
        logger.warning(
            "强制更新解析任务终态失败 project=%d file=%s: %s", project_id, filename, e
        )


# ============================================================================
# 异步文件 I/O 包装器（消除事件循环阻塞）
# ============================================================================


async def async_read_text(path: str | Path, encoding: str = "utf-8") -> str:
    """异步读取文本文件（不阻塞事件循环）"""
    return await asyncio.to_thread(Path(path).read_text, encoding=encoding)


# 解析成功但文本为空时的统一提示（BUG-FIX 2026-10-05 B4）：
# 此前只有 save_upload 写入该提示，re_extract_text 成功后无条件把 message 清空 ——
# 同一个文件「上传时空→重提取后仍空」，前端的黄色警告却在重提取后消失，
# 用户以为已修好。抽成常量供两条路径共用，保证口径一致。
EMPTY_TEXT_MESSAGE = "未提取到文本内容（可能为空文件、纯图片扫描件或 OCR 引擎未启用）"

# BUG-FIX 2026-10-06（H1）：老格式二进制文档（.doc/.ppt/.wps）在缺少
# olefile / antiword / catdoc / COM 自动化时，_extract_doc(:1018)、
# _extract_ppt(:1192)、_extract_wps(:1550) 会 return 一句**非空**的
# 「[提示] 当前系统无法自动提取此 .doc 文件文本，建议转换为 .docx 后重新上传」。
#
# 此前 save_upload(:377) 与 Celery parse_and_extract_file(tasks/__init__.py:54)
# 都只按「有没有抛异常」判 success —— 提示串不是异常，于是被写成
# status='success'。而 guess_file_role 对「技术要求.doc」这类命中主文件
# 关键词的文件判 main，_pick_main_file(:1648) 又只优先挑 status=='success'
# 的 main，于是这句提示被选为**项目主文本**，喂给全局事实提取 / 目录生成 /
# 正文生成三个模块的 AI —— 用户看到的不是空文档，而是「AI 按一句提示语
# 写出了标书」，且前端无任何失败提示（message 为空）。
#
# 判定用**精确登记表**而非「前缀 + 长度」启发式：真实标书正文完全可能以
# 「[提示] 投标人须知：…」起头且只有几十字（首版实现即因此误报，由
# tests/test_bugfix_2026_10_06_extraction_notice_poisoning.py 的自证用例
# 抓出）。三条提示串是本模块写死的常量，用 frozenset 登记可做到构造上的
# 零假阳性；三条提取分支改为直接 return 常量，登记表与代码不会再漂移，
# 另有测试扫描本文件全部 "[提示]" 字面量做防漂移守卫。


class MainFileRequiredError(ValueError):
    """REVIEW-FIX 2026-10-04（M2）：项目唯一 main 被降级为 supporting 时抛出（router 转 422）。

    0 main 态下 _pick_main_file 静默回退「按 id 取首个成功文件」，若图片扫描件
    （空文本）排前，真正招标正文会被 _truncate_supporting_file 截成 200 行
    「辅助摘要」，下游摘要/评分/目录全部基于残缺上下文且用户无感。
    因此项目必须保留至少一个主文件；换主文件的正确姿势是先把目标文件设为主。
    """


def _analysis_has_cached(conn, project_id: int) -> bool:
    """analysis 表是否已有实质分析产物（REVIEW-FIX m2：主文件换源后透出 needs_reanalysis 用）"""
    row = conn.execute(
        "SELECT 1 FROM analysis WHERE project_id = ? AND ("
        "COALESCE(summary_json, '') NOT IN ('', '{}')"
        " OR COALESCE(scoring_points_json, '[]') NOT IN ('', '[]', '{}')) LIMIT 1",
        (project_id,),
    ).fetchone()
    return row is not None


def save_upload(project_id: int, filename: str, content: bytes, file_role: str = "") -> dict:
    """保存上传文件并提取文本

    Args:
        project_id: 项目 ID
        filename: 原始文件名
        content: 文件二进制内容
        file_role: 文件角色（"main" 主文件 / "supporting" 辅助文件 / "" 自动检测）

    Returns:
        包含文件信息和提取文本的 dict
    """
    project_dir = PROJECTS_DATA_DIR / str(project_id)
    upload_dir = project_dir / "uploads"
    texts_dir = project_dir / "texts"
    upload_dir.mkdir(parents=True, exist_ok=True)
    texts_dir.mkdir(parents=True, exist_ok=True)

    # 防止路径穿越：仅保留文件名，丢弃客户端传入的任何目录成分（如 ../../etc/x）
    safe_filename = os.path.basename(filename)
    if not safe_filename:
        return {
            "status": "failed",
            "message": "非法文件名",
            "filename": filename,
        }
    if safe_filename != filename:
        logger.warning("上传文件名包含路径成分，已归一化为安全名称: %r", filename)
    filename = safe_filename

    # 保存原始文件
    # BUG-FIX 2026-10-04（P2 并发竞态）："重命名去重→write_bytes" 必须在项目级互斥
    # 锁内原子完成，否则并发同名上传会选中同一目标路径互相覆盖（见 _get_upload_write_lock）。
    # 锁只覆盖命名+落盘，文本提取（可能耗时数秒~OCR）在锁外执行，不阻塞同项目其它上传。
    with _get_upload_write_lock(project_id):
        # G8（2026-10-28 后续建议#4·文件去重）：按内容 SHA-256 查重，命中则
        # 不落盘/不建任务/不插行，直接返回 duplicate（前端据此提示「文件已存在」）。
        # 仅同项目同大小文件参与比较（_find_duplicate_upload 内按 os.stat 过滤），
        # 正常路径零额外行为。查重本身异常降级为「不去重」，不影响上传。
        _dup = _find_duplicate_upload(project_id, content)
        if _dup:
            # BUG-FIX 2026-10-28（P2 重传自愈）：若历史命中文件解析失败
            # （status='failed'），用户重传同一文件（最自然的修复动作）此前被判
            # duplicate，无法触发重新解析。现删除旧失败行 + 走正常上传路径，
            # 让重传真正触发重新解析（自愈）。仅 failed 触发；pending/processing
            # 保持 duplicate 拦截（任务进行中，无需重复）。
            if _dup.get("status") == "failed":
                logger.info(
                    "上传去重命中解析失败文件，删除旧行后重传自愈 project=%d old_id=%d file=%s",
                    project_id, _dup.get("id"), filename,
                )
                _dup_id = _dup.get("id")
                if _dup_id is not None:
                    try:
                        with get_db() as conn:
                            conn.execute(
                                "DELETE FROM uploaded_files WHERE id = ? AND project_id = ?",
                                (_dup_id, project_id),
                            )
                    except Exception as _dup_e:  # noqa: BLE001 —— 删除失败不阻塞上传
                        logger.warning("删除失败重复文件失败（继续上传）id=%s: %s", _dup_id, _dup_e)
                # 删除旧行后继续走正常落盘/建任务/插行路径（不 return）
            else:
                logger.info(
                    "上传去重命中 project=%d file=%s -> 已存在 id=%d status=%s",
                    project_id, filename, _dup.get("id"), _dup.get("status"),
                )
                return {
                    "status": "duplicate",
                    "message": f"文件内容已存在（{_dup.get('filename', filename)}），未重复上传",
                    "filename": filename,
                    "file_id": _dup.get("id"),
                    "duplicate_of": _dup.get("id"),
                }

        file_path = upload_dir / filename
        # BUG-FIX-65: 同名文件并发覆盖防护 — 若目标路径已存在，追加数字后缀避免覆盖
        if file_path.exists():
            stem = Path(filename).stem
            suffix = Path(filename).suffix
            counter = 1
            while file_path.exists():
                filename = f"{stem}_{counter}{suffix}"
                file_path = upload_dir / filename
                counter += 1
            logger.warning("同名文件已存在，重命名为: %s", filename)
        # 先创建文件解析任务行，再做落盘与提取。
        # BUG-FIX 2026-09-18（高）：原实现把 _create_parse_task 放在 write_bytes 之后，
        # 而失败分支在此之前就调用 _update_parse_task("failed")，此时不存在
        # status='processing' 的任务行 → UPDATE 命中 0 行，失败状态被丢弃，
        # 任务表停留在旧状态，前端无法感知保存失败。
        ext = Path(filename).suffix.lower()
        try:
            _create_parse_task(project_id, filename, ext)
        except Exception as _task_err:
            # P-3 加固：任务行创建失败说明 DB 不可用，继续写入文件会导致后续
            # _update_parse_task UPDATE 0 行（终态丢失）。直接返回失败让上游感知。
            logger.error("任务行创建失败，中止上传: %s", _task_err)
            return {
                "status": "failed",
                "message": f"任务行创建失败（数据库不可用）: {_task_err}",
                "filename": filename,
            }

        try:
            file_path.write_bytes(content)
        except Exception as e:
            logger.error("保存文件失败: %s, 错误: %s", filename, e)
            _update_parse_task(project_id, "failed", 0, f"保存文件失败: {e}", filename)
            return {
                "status": "failed",
                "message": f"保存文件失败: {e}",
                "filename": filename,
            }

    # R-3 2026-10-12：图片等 OCR 耗时格式在上传时改为后台异步解析，避免占满上传线程池
    # （asyncio.to_thread 内的同步 OCR 在批量图片上传时会让同进程其它阻塞型调用排队）。
    # 仅当 broker 可达、任务真正入队（dispatched_via='celery'）时才走异步；其余情况
    # （无 broker→进程内直调 / 入队失败→同步回退）均与旧同步行为一致，保证零回归。
    _img_role = normalize_file_role(file_role, filename)
    # 注意 IMAGE_EXTENSIONS 存的是带点的扩展名（".png"），ext 已是 Path.suffix（带点），
    # 直接 `ext in IMAGE_EXTENSIONS`，不要 lstrip('.')（否则永远匹配不到）。
    if ext in IMAGE_EXTENSIONS:
        _img = _save_upload_image_async(project_id, filename, str(file_path), _img_role)
        if _img is not None:
            _text_cache_clear(project_id)
            return _img

    # 提取文本
    try:
        extracted_text = extract_text_from_path(str(file_path))
        # BUG-FIX 2026-10-06（H1）：老格式降级提示串（「[提示] 当前系统无法自动提取
        # 此 .doc …」）不是正文。此前这里无条件 status='success'，提示串会被
        # _pick_main_file 当作「解析成功的主文件」选中去喂下游 AI。现在判为
        # failed，但把提示原文留在 message 里，用户仍能看到「请转换为 .docx」。
        if is_extraction_notice(extracted_text):
            status = "failed"
            message = extracted_text.strip()
            # ENHANCE 2026-10-06（R1）：任务行终态与文件状态同口径 —— 此前
            # 文件判 failed 但任务行仍写 completed，GET /tasks/files/status 的
            # recent_failed 恒为 0，批量上传的失败数被谎报为全成功
            _update_parse_task(project_id, "failed", 100, message, filename)
        else:
            status = "success"
            message = ""
            # BUG-FIX 2026-09-29：提取成功但结果为空（空文件/纯图片扫描件且 OCR 未启用）
            # 此前无任何提示，前端显示“解析成功”但下游 AI 链路拿到空文本且无从排查。
            # 现在如实写入提示信息（status 保持 success，不改变主文件选择/缓存语义）。
            if not extracted_text.strip():
                message = EMPTY_TEXT_MESSAGE
            else:
                # GAP-FIX 2026-10-26（R9）：解析质量 error（如几乎全为控制字符/
                # 大文件只出几十字符）不再静默成功，回填 warning 文案给前端展示。
                message = _parse_quality_error_message(str(file_path), extracted_text)
            # M-1 2026-10-15：把警告一并写进任务行（此前只写文件行）
            _update_parse_task(
                project_id, "completed", 100, error_message=message, filename=filename
            )
    except Exception as e:
        extracted_text = ""
        status = "failed"
        message = f"文本提取失败: {e}"
        logger.error("文件 %s 文本提取失败: %s", filename, e)
        _update_parse_task(project_id, "failed", 50, message, filename)

    # 保存提取文本（文件名含完整原名+扩展名，避免同名不同扩展名文件互相覆盖）
    text_filename = f"{Path(filename).name}.txt"
    text_path = texts_dir / text_filename
    # BUG-FIX 2026-10-08：提取失败时不写空 .txt 文件，避免下游误判文件存在
    text_written = False
    if extracted_text.strip():
        try:
            text_path.write_text(extracted_text, encoding="utf-8")
            text_written = True
        except Exception as e:
            # BUG-FIX 2026-10-15（F-1 假 success）：文本已提取成功但 .txt 落盘失败
            # （磁盘满 / 权限拒绝 / 目录被外部进程占住）。旧实现只 logger.error，
            # 而 status/message 早在 extract 分支被置为 'success'/''，随后 INSERT
            # 会把「status='success' + extracted_text_path='' + message=''」一起入库，
            # 任务行也已被写成 completed —— 用户看到「解析成功」却没有任何正文，
            # 下游 AI 拿到空文本且无从排查。re_extract_text(:1953) 早已对同一失败
            # 如实返回 failed，这里是同一缺陷在 save_upload 入口的漏网之鱼。
            # 现如实降级为 failed，并把任务行从 completed 覆盖回 failed。
            logger.error("保存提取文本失败: %s", e)
            status = "failed"
            message = f"写入提取文本失败: {e}"
            _force_parse_task_failed(project_id, filename, message)
    else:
        logger.info("跳过空文本文件写入: %s (status=%s)", filename, status)

    # BUG-FIX 2026-10-12（解析结果丢失 · 悬空路径）：2026-10-08 的修复只做了
    # 「不写空文件」，却仍然把 `str(text_path)` 无条件写进 uploaded_files。
    # 结果是：DB 声称该文件有一份提取文本，实际磁盘上并不存在该文件 ——
    # 任何只看 DB 字段判断「有没有正文」的代码/脚本/报表都会被误导，
    # 且与 clear_project_texts 之后 `extracted_text_path=''` 的干净状态自相矛盾
    # （同一个字段两处口径不一致，字段错位）。
    # 现统一口径：**只有真正落盘成功**才记录路径，否则置空串，
    # 下游 latest_extracted_text / _first_text_by_keywords / get_file_text
    # 的 `if path and Path(path).exists()` 判空语义因此在两条路径上完全一致。
    stored_text_path = str(text_path) if text_written else ""

    # 写入数据库
    file_type = ext.lstrip(".")
    # BUG-FIX 2026-10-05（B2）：外部传入的 file_role 必须归一化后再入库，
    # 否则非法值会让项目永远没有 'main'（_pick_main_file 静默回退）。
    final_role = normalize_file_role(file_role, filename)
    demoted: list[int] = []
    needs_reanalysis = False
    try:
        with get_db() as conn:
            # REVIEW-FIX 2026-10-04（M1）：main 互斥不变式必须同时覆盖上传写入路径。
            # guess_file_role 对未命中关键词的非图片文件默认判 main，此前连传两份
            # 「招标」文件会产生多 main 行，而 _pick_main_file 按 id 升序取首个 main，
            # 用户以「重新上传」换主文件后下游摘要/评分/目录仍读旧主文件（与 BUG-3
            # 同症状、不同入口）。降级与 INSERT 同处 get_db() 写锁事务内，与
            # set_file_role 的互斥口径一致。
            if final_role == "main":
                demoted, needs_reanalysis = _demote_existing_mains(conn, project_id)
            cursor = conn.execute(
                """INSERT INTO uploaded_files
                   (project_id, filename, path, file_type, file_role,
                    status, extracted_text_path, message, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now', 'localtime'))""",
                (
                    project_id,
                    filename,
                    str(file_path),
                    file_type,
                    final_role,
                    status,
                    stored_text_path,
                    message,
                ),
            )
    except Exception as e:
        logger.error("保存文件记录到数据库失败: %s", e)
        # BUG-FIX 2026-09-18（中）：此前 DB 写入失败直接 return，但文件已在
        # 上方落盘到 uploads/ 与 texts/，且无任何 DB 记录指向它们 → 永久孤儿
        # 文件，反复失败会持续占用磁盘且无法从界面清理。这里补一次尽力而为的
        # 回滚删除（删除失败只告警，不影响错误返回）。
        for _orphan in (file_path, text_path):
            try:
                if _orphan.exists():
                    _orphan.unlink()
            except Exception as _ce:
                logger.warning("清理孤儿文件失败 %s: %s", _orphan, _ce)
        _update_parse_task(project_id, "failed", 0, f"保存文件记录失败: {e}", filename)
        return {
            "status": "failed",
            "message": f"保存文件记录失败: {e}",
            "filename": filename,
        }

    # 项目文本已变化，使合并文本缓存失效
    _text_cache_clear(project_id)

    return {
        "id": cursor.lastrowid,
        "filename": filename,
        "file_type": file_type,
        "file_role": final_role,
        "status": status,
        "message": message,
        # REVIEW-FIX 2026-10-04（M1/m2）：上传换主文件时被自动降级的旧 main id；
        # 以及旧摘要/评分是否需基于新主文件重新生成（仅多 main 降级时可能为 true）
        "demoted_file_ids": demoted,
        "needs_reanalysis": needs_reanalysis,
    }


def _demote_existing_mains(conn, project_id: int) -> tuple[list[int], bool]:
    """在给定事务内降级已有 main 文件，返回 (被降级 id 列表, 是否需要重新分析)。

    供 save_upload / _save_upload_image_async 复用，确保「主文件互斥」不变式
    与 set_file_role 同口径，且降级与 INSERT 处于同一写事务。
    """
    downgraded = conn.execute(
        "SELECT id FROM uploaded_files WHERE project_id = ? AND file_role = 'main'",
        (project_id,),
    ).fetchall()
    demoted = [r["id"] for r in downgraded]
    needs_reanalysis = False
    if demoted:
        conn.execute(
            "UPDATE uploaded_files SET file_role = 'supporting' "
            "WHERE project_id = ? AND file_role = 'main'",
            (project_id,),
        )
        # REVIEW-FIX m2：main 换源且已有缓存分析产物 → 旧摘要/评分基于旧主文件，
        # 透出 needs_reanalysis 供前端提示重新「提取项目」（非破坏式，不静默清空
        # 用户已确认数据）。
        needs_reanalysis = _analysis_has_cached(conn, project_id)
    return demoted, needs_reanalysis


def _persist_file_extraction(
    project_id: int, file_id: int, file_path: str, filename: str, ext: str,
    create_task_row: bool = True,
) -> str:
    """提取文本并落盘 + 更新 uploaded_files 终态 + 解析任务行（同步/异步链路共用）。

    BUG-FIX 2026-10-12（BUG-5 异步链路悬空路径）：此前 Celery 任务
    ``tasks.file.parse_and_extract`` 在「文本为空 / OCR 未启用」时仍把
    ``extracted_text_path`` 写成磁盘上并不存在的 .txt 路径（与 save_upload /
    re_extract_text 已修复的悬空路径口径不一致，是 BUG-5 在异步链路的漏网之鱼）。
    现统一在此：**只有真正落盘成功（text_written）才记录路径，否则置空串**，
    与 save_upload(files.py:574) 完全同口径。

    R-3 2026-10-12：抽取为共用函数，save_upload 同步回退路径与 Celery 异步任务
    都调用它，杜绝两条链路再次出现口径漂移。
    """
    if create_task_row:
        _create_parse_task(project_id, filename, ext)
    with get_db() as conn:
        conn.execute(
            "UPDATE uploaded_files SET status = 'processing', "
            "updated_at = datetime('now', 'localtime') WHERE id = ? AND project_id = ?",
            (file_id, project_id),
        )
    try:
        _clear_parse_cache(str(file_path))
    except Exception as _cc_e:  # pragma: no cover - 缓存清理失败不影响主流程
        logger.debug("清理解析缓存失败（忽略）file=%s: %s", file_path, _cc_e)

    extracted_text = extract_text_from_path(str(file_path))

    texts_dir = PROJECTS_DATA_DIR / str(project_id) / "texts"
    texts_dir.mkdir(parents=True, exist_ok=True)
    text_filename = f"{Path(file_path).name}.txt"
    text_path = texts_dir / text_filename

    # 与 save_upload 同口径：只有真实非空文本才写 .txt；只有落盘成功才记录路径。
    text_written = False
    _write_error: str | None = None
    if extracted_text.strip():
        try:
            text_path.write_text(extracted_text, encoding="utf-8")
            text_written = True
        except Exception as _wt_e:
            # BUG-FIX 2026-10-15（F-2 假 success · Celery 主链路）：提取成功但 .txt
            # 落盘失败时，旧实现只 logger.warning，随后 _status 恒为 'success'、
            # _msg 恒为 ''，DB 行 / Celery 返回值 / 任务行三方全部报成功，
            # 而 extracted_text_path 已是空串 —— 用户点「重新提取」或直接看到
            # 「解析成功」，磁盘上却没有那份正文。这是**异步解析主链路**，
            # 影响面大于 save_upload 的同步路径。现如实判 failed。
            logger.error(
                "保存提取文本失败（Celery 解析链路）file=%s: %s", file_path, _wt_e
            )
            _write_error = f"写入提取文本失败: {_wt_e}"
    else:
        logger.info("跳过空文本文件写入: %s", filename)

    # 与 save_upload 同口径：老格式降级提示串判 failed；空文本 success + 空提示。
    if _write_error is not None:
        _status, _msg = "failed", _write_error
    elif is_extraction_notice(extracted_text):
        _status, _msg = "failed", extracted_text.strip()
    else:
        # GAP-FIX 2026-10-26（R9）：空文本沿用既有提示；非空但存在 error 级解析
        # 质量问题（控制字符/替换符过多、大文件近乎空）同样回填提示，不再静默成功。
        _msg = (
            EMPTY_TEXT_MESSAGE
            if not extracted_text.strip()
            else _parse_quality_error_message(file_path, extracted_text)
        )
        _status = "success"
    if _write_error is not None:
        # 任务行可能已被写为 completed（create_task_row=False 的派发路径），
        # 必须强制覆盖，否则会 UPDATE 0 行、状态端点继续谎报成功。
        _force_parse_task_failed(project_id, filename, _write_error)

    stored_text_path = str(text_path) if text_written else ""
    with get_db() as conn:
        conn.execute(
            "UPDATE uploaded_files SET status = ?, extracted_text_path = ?, message = ?, "
            "updated_at = datetime('now', 'localtime') "
            "WHERE id = ? AND project_id = ?",
            (_status, stored_text_path, _msg, file_id, project_id),
        )
    _text_cache_clear(project_id)
    _update_parse_task(
        project_id,
        "completed" if _status == "success" else "failed",
        100 if _status == "success" else 50,
        error_message=_msg,
        filename=filename,
    )
    return _status


def _read_uploaded_file_status(file_id: int) -> tuple[str, str]:
    """读取 uploaded_files 的 (status, message)，行不存在时返回 ('failed', '')。"""
    with get_db(readonly=True) as conn:
        row = conn.execute(
            "SELECT status, message FROM uploaded_files WHERE id = ?", (file_id,)
        ).fetchone()
    if row:
        return row["status"], row["message"]
    return "failed", ""


def _dispatch_async_parse(
    project_id: int, file_path: str, filename: str, file_id: int, ext: str
) -> dict:
    """派发后台解析任务（tasks.file.parse_and_extract）。

    返回 dispatch_task 的结果字典。注意 dispatch_task 在 broker 不可达时会**自动
    降级为进程内直调**（dispatched_via='inline'），不会抛异常；调用方据此判断
    是否真正异步（'celery'）还是已在当前进程同步跑完（'inline' / 'none'）。
    若派发本身抛异常（极少见，如任务未登记），这里捕获并返回 not_dispatched，
    交由 _save_upload_image_async 同步收尾，避免上传直接 500。
    """
    from ..celery_app import dispatch_task

    try:
        return dispatch_task(
            "tasks.file.parse_and_extract",
            (str(file_path), project_id, file_id),
            {"create_task_row": False},
        )
    except Exception as e:  # 派发异常兜底：绝不让图片上传因 broker 抖动而 500
        logger.warning("图片后台解析派发失败，回退同步解析: %s", e)
        return {"status": "not_dispatched", "dispatched_via": "none", "error": str(e)}


def _save_upload_image_async(
    project_id: int, filename: str, file_path: str, final_role: str
) -> dict:
    """图片上传的异步解析快路径（R-3）。

    返回最终要透出给前端的 dict。内部已处理三种派发结果：
      - dispatched_via='celery'：任务已入队，返回 status='processing'，由 worker 收尾；
      - dispatched_via='inline'：broker 不可达，任务已在本进程同步跑完，读终态返回；
      - dispatched_via='none'  / 入队失败：任务未执行，同步调用 _persist_file_extraction
        收尾后读终态返回 —— 与旧同步行为完全一致，保证无 broker 环境零回归。

    落库（降级旧 main + INSERT processing 行）与 save_upload 同步路径同口径，
    得到 file_id 后由任务或回退路径 UPDATE 终态。
    """
    ext = Path(filename).suffix.lower()
    try:
        with get_db() as conn:
            demoted, needs_reanalysis = ([], False)
            if final_role == "main":
                demoted, needs_reanalysis = _demote_existing_mains(conn, project_id)
            cursor = conn.execute(
                """INSERT INTO uploaded_files
                   (project_id, filename, path, file_type, file_role,
                    status, extracted_text_path, message, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'processing', '', '',
                           datetime('now', 'localtime'))""",
                (
                    project_id,
                    filename,
                    file_path,
                    ext.lstrip("."),
                    final_role,
                ),
            )
            file_id = cursor.lastrowid
    except Exception as e:
        logger.error("图片上传落库失败: %s", e)
        _update_parse_task(project_id, "failed", 0, f"保存文件记录失败: {e}", filename)
        return {
            "status": "failed",
            "message": f"保存文件记录失败: {e}",
            "filename": filename,
        }

    dispatched = _dispatch_async_parse(project_id, file_path, filename, file_id, ext)
    via = dispatched.get("dispatched_via")

    if via != "celery":
        # 非真正异步：inline（已在本进程跑完）或 none（未派发）→ 同步收尾确保终态落库
        if via != "inline":
            try:
                _persist_file_extraction(
                    project_id, file_id, file_path, filename, ext, create_task_row=False
                )
            except Exception as _pe:
                logger.error("图片同步回退解析失败: %s", _pe)
                # BUG-FIX 2026-10-15（M-4 · 假 processing 终态丢失）：此前这里只
                # logger.error，行仍以 status='processing' 落库且无人改写终态。
                # 下面 `_read_uploaded_file_status` 读回的就是 'processing'，
                # POST /files 于是返回 200 + status='processing'：
                #   · 前端 classifyUploadResult 判为「解析中」，进度条永久卡住；
                #   · 文件行的 message 为空，用户看不到失败原因；
                #   · 此前**没有任何**清洗逻辑回收该状态（本注释原引用的
                #     `_recover_stale_tasks` 在全仓并不存在），进程存活期间永不回收。
                #     现另有 reclaim_stale_parse_state()（beat 每小时）兜底：即便此处置顶
                #     失败失败，超过 PARSE_TASK_STALE_MINUTES 的 processing 也会被收敛为 failed。
                # 现与 save_upload 同步路径的 `文本提取失败: {e}` 口径对齐，
                # 同时把失败写进文件行与任务行。
                _fail_msg = f"文本提取失败: {_pe}"[:500]
                try:
                    with get_db() as conn:
                        conn.execute(
                            "UPDATE uploaded_files SET status = 'failed', message = ?, "
                            "updated_at = datetime('now', 'localtime') "
                            "WHERE id = ? AND project_id = ?",
                            (_fail_msg, file_id, project_id),
                        )
                except Exception as _fe:
                    logger.error("图片回退失败状态落库失败 file=%s: %s", file_id, _fe)
                _update_parse_task(project_id, "failed", 50, _fail_msg, filename)
        status, message = _read_uploaded_file_status(file_id)
        return {
            "id": file_id,
            "filename": filename,
            "file_type": ext.lstrip("."),
            "file_role": final_role,
            "status": status,
            "message": message,
            "demoted_file_ids": demoted,
            "needs_reanalysis": needs_reanalysis,
        }

    return {
        "id": file_id,
        "filename": filename,
        "file_type": ext.lstrip("."),
        "file_role": final_role,
        "status": "processing",
        "message": "",
        "demoted_file_ids": demoted,
        "needs_reanalysis": needs_reanalysis,
    }


# BOM 指纹 → 编码（ENHANCE 2026-10-05 B8 编码异常）：
# Word「另存为 Unicode 文本」、Windows 记事本 UTF-8 等都会写 BOM。原实现直接
# utf-8 硬解码有两个后果：
#   1) UTF-8 BOM 在正文开头留下不可见的 \ufeff，污染首行关键词匹配（guess_file_role、
#      章节锚点）与 AI 输入；
#   2) UTF-16LE 文件字节流含大量 NUL，utf-8 解码往往**不抛异常**，而是产出满屏
#      \x00 的乱码并被视为"解析成功"入库，下游 AI 拿到的是无法使用的文本。
# 因此先做 BOM 嗅探（顺序必须从长到短，\xff\xfe\x00\x00 要先于 \xff\xfe 匹配）。
_BOM_ENCODINGS: tuple[tuple[bytes, str], ...] = (
    (b"\xef\xbb\xbf", "utf-8-sig"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)


def _sniff_bom_encoding(head: bytes) -> str:
    """按 BOM 指纹判定编码，无 BOM 返回空串"""
    for bom, encoding in _BOM_ENCODINGS:
        if head.startswith(bom):
            return encoding
    return ""


def _sniff_zip_extension(file_path: str) -> str:
    """按 ZIP 内部结构判定真实 Office 格式（docx / xlsx / pptx 都是 ZIP 容器）"""
    try:
        with zipfile.ZipFile(file_path) as z:
            names = set(z.namelist())
    except Exception:
        return ""
    if "word/document.xml" in names:
        return ".docx"
    if "xl/workbook.xml" in names:
        return ".xlsx"
    if "ppt/presentation.xml" in names:
        return ".pptx"
    # BUG-FIX 2026-10-08：非 Office ZIP 返回 .zip 而非空串，
    # 让调用方获知真实容器类型（空串会使调用方误认为应按声明扩展名解析）
    return ".zip"


def _sniff_extension(file_path: str) -> str:
    """按魔数嗅探文件真实格式（ENHANCE 2026-10-05 格式误判）

    仅依据扩展名分发的风险：用户把 .docx 另存成 .doc、把 PDF 改后缀成 .docx
    （微信/邮件传输后改名非常常见），结果就是「解析失败」或提取到一堆乱码，
    而用户只会看到"提取失败"四个字，无从排查。

    这里只做**高置信**判定：
      - ``%PDF``        → .pdf
      - ``PK\\x03\\x04``    → .docx / .xlsx / .pptx（按 ZIP 内部目录名区分）
      - OLE2 复合文档    → 不判定（.doc/.xls/.ppt/.wps 无法从魔数区分，沿用扩展名）

    返回空串表示无法判定，调用方保持原扩展名（向后兼容）。
    """
    try:
        with open(file_path, "rb") as _f:
            head = _f.read(8)
    except OSError:
        return ""
    if head.startswith(b"%PDF"):
        return ".pdf"
    if head.startswith(b"PK\x03\x04"):
        return _sniff_zip_extension(file_path)
    # BUG-FIX 2026-10-06（R2 格式误判补齐）：OLE2 复合文档魔数
    # （D0 CF 11 E0 A1 B1 1A E1）—— 旧版 Office 二进制格式（.doc/.xls/.ppt）
    # 与 .wps 共用该容器。此前只嗅 %PDF 与 ZIP，用户把真正的 .doc 改名为
    # .docx（Word 2003 时代文件经邮件/网盘转存后改名极常见）时，docx 的
    # ZIP 解析直接抛「File is not a zip file」→ 上传被判失败，而按 .doc
    # 分支走 olefile 链路其实可以提取。此处判定后仍无法区分具体子格式
    # （共享同一容器），由 _do_extract_text 对 legacy 扩展名做降级链。
    if head.startswith(_OLE2_MAGIC):
        return ".doc"
    return ""


def _strip_invisible(text: str) -> str:
    """清除文本中的 BOM 残留、NUL 控制字符及其他不可见字符（读取边界统一清洗）

    2026-10-11 加固（I-11）：旧实现只剥首字符 ``\\ufeff`` 和全部 ``\\x00``，
    中后段出现的 BOM（多次追加拼接导致）、零宽字符（``\\u200b`` / ``\\ufeff``）
    和 C0/C1 控制字符均未被清除。现统一用正则一次遍历剥除全部不可见字符。
    """
    if not text:
        return text
    # 剥除：BOM（所有出现位置）、NUL、零宽字符、C0/C1 控制字符（保留 \n \r \t）
    # \x00-\x08, \x0b, \x0c, \x0e-\x1f, \x7f-\x9f
    _INVISIBLE_RE = re.compile(
        r"[\ufeff\x00"
        r"\u200b\u200c\u200d\ufe0e\ufe0f"
        r"\u2028\u2029"
        r"\x00-\x08\x0b\x0c\x0e-\x1f"
        r"\x7f-\x9f]"
    )
    return _INVISIBLE_RE.sub("", text)


def _read_text_auto(file_path: str) -> str:
    """读取文本文件并自动处理编码

    优先级：BOM 嗅探（utf-8-sig / utf-16 / utf-32） → utf-8 → gb18030 → 兜底忽略坏字节

    修复 BUG：此前 .txt/.md 硬编码 utf-8，GBK/GB2312 编码的中文文件会抛
    UnicodeDecodeError 导致整个上传解析失败。

    2026-10-11 加固（I-12）：无 BOM 的 UTF-16LE 此前走 utf-8 → gb18030 → 兜底
    替换，中文内容全部变成乱码。现增加 BOM 探测失败后的奇偶零字节启发式检测，
    识别 UTF-16LE/UTF-16BE 无 BOM 文件（部分系统导出的 xlsx 说明文本、
    Windows Notepad 另存为「Unicode」时的中间态）。
    """
    path = Path(file_path)
    try:
        with path.open("rb") as _f:
            head = _f.read(4)
    except OSError:
        head = b""

    bom_encoding = _sniff_bom_encoding(head)
    if bom_encoding:
        try:
            return _strip_invisible(path.read_text(encoding=bom_encoding))
        except (UnicodeDecodeError, UnicodeError) as e:
            logger.debug("[编码] BOM 判定 %s 解码失败，回退通用探测: %s", bom_encoding, e)

    try:
        return _strip_invisible(path.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        pass
    # 无 BOM 的 UTF-16LE/BE 探测（I-12）：
    # UTF-16LE 中文文本的特征是每个中文字符占 4 字节（UTF-16LE 编码为 4 字节：
    # 2 字节字符 + 2 字节 ASCII），奇数位全为 0x00。
    # 用启发式：前 512 字节中 0x00 字节占比 > 30% 时尝试 UTF-16。
    try:
        with path.open("rb") as _f:
            sample = _f.read(512)
        if len(sample) >= 4:
            zero_count = sample.count(b"\x00")
            if zero_count > len(sample) * 0.3:
                # 判断 LE 还是 BE：
                # UTF-16LE: ASCII 字符的 0x00 在奇数位（字节 1,3,5...）
                # UTF-16BE: ASCII 字符的 0x00 在偶数位（字节 0,2,4...）
                even_zeros = sum(1 for i in range(0, len(sample) - 1, 2) if sample[i] == 0)
                odd_zeros = sum(1 for i in range(1, len(sample) - 1, 2) if sample[i] == 0)
                encoding = "utf-16-le" if odd_zeros > even_zeros else "utf-16-be"
                try:
                    return _strip_invisible(path.read_text(encoding=encoding))
                except UnicodeDecodeError:
                    pass
    except OSError:
        pass
    # gb18030 是 GBK/GB2312 的超集，覆盖常见中文编码
    try:
        return _strip_invisible(path.read_text(encoding="gb18030"))
    except UnicodeDecodeError:
        pass
    # 最终兜底：用替换符保留无法解码的字节，保证不中断上传流程
    # BUG-FIX 2026-10-08：此前 errors="ignore" 会静默丢弃字节，用户无感知数据丢失
    _fallback_text = path.read_text(encoding="utf-8", errors="replace")
    _replace_count = _fallback_text.count("\ufffd")
    if _replace_count:
        logger.warning(
            "[编码] %s 无法以任何已知编码解码，使用 UTF-8 替换模式（%d 个字节被替换）",
            path.name, _replace_count,
        )
    return _strip_invisible(_fallback_text)


def _is_under_projects_dir(file_path: str | Path) -> bool:
    """判断路径是否确实位于项目数据目录之内（外部命令行工具调用前的护栏）。

    BUG-FIX 2026-09-18（中）：原实现是
    `str(file_path).startswith(str(PROJECTS_DATA_DIR))` —— 字符串前缀比较有两个漏洞：
      1. 同级目录只要共享前缀即可绕过（例如 `<data>/projects_evil/x.doc`
         会命中 `<data>/projects` 这个前缀）；
      2. 未做路径归一化，`<data>/projects/../../任意路径` 同样通过。
    改为 `Path.resolve()` 之后的真实父子关系判定（resolve 会消解 `..` 与符号链接）。
    """
    try:
        rp = Path(file_path).resolve()
        base = Path(PROJECTS_DATA_DIR).resolve()
        rp.relative_to(base)
        return True
    except (ValueError, OSError):
        return False


def extract_text_from_path(file_path: str) -> str:
    """根据文件后缀分发到对应提取函数（带缓存优化）

    优化：相同文件在短时间内重复解析时，直接返回缓存结果，
    避免重复进行耗时的文件读取和解析操作。
    """
    declared_ext = Path(file_path).suffix.lower()
    ext = declared_ext
    # ENHANCE 2026-10-05（格式误判）：扩展名与实际内容不一致时改按真实内容解析，
    # 解析再失败则回退扩展名（详见 _sniff_extension）。
    sniffed = _sniff_extension(file_path)
    if sniffed and sniffed != declared_ext:
        logger.warning(
            "[文件解析] %s 扩展名 %s 与实际内容 %s 不一致，按实际内容解析",
            Path(file_path).name, declared_ext, sniffed,
        )
        ext = sniffed

    file_hash = _get_file_hash(file_path)
    used_cache = False

    # 检查缓存（仅缓存支持的格式）
    # BUG-FIX-74/74c: 补全缓存格式列表并统一引用模块级常量
    # BUG-FIX 2026-10-08：file_hash 为空（_get_file_hash 失败）时跳过缓存，
    # 否则空哈希字符串会导致不同文件碰撞命中同一缓存条目
    if ext in _PARSE_CACHEABLE_EXTS and file_hash:
        with _parse_cache_lock:
            cached = _parse_result_cache.get(file_path)
        if cached and cached[0] == file_hash:
            cache_time = cached[1]
            if time.time() - cache_time < _PARSE_CACHE_TTL:
                logger.info("[文件解析] 命中缓存: %s", Path(file_path).name)
                used_cache = True
                return cached[2]
        # 清理过期缓存
        _clear_parse_cache(file_path)

    logger.info("[文件解析] 开始解析: %s (格式: %s)", Path(file_path).name, ext)
    _t0 = time.monotonic()
    try:
        result = _do_extract_text(file_path, ext, declared_ext)
    except Exception as _sniff_e:
        if ext == declared_ext:
            raise
        # 嗅探出的格式解析失败（如 ZIP 容器损坏）→ 回退按用户声明的扩展名再试一次
        logger.warning(
            "[文件解析] %s 按嗅探格式 %s 解析失败，回退扩展名 %s: %s",
            Path(file_path).name, ext, declared_ext, _sniff_e,
        )
        ext = declared_ext
        result = _do_extract_text(file_path, ext, declared_ext)
    elapsed = time.monotonic() - _t0

    # 标书智能体（一）引入：解析质量校验。
    # 仅对"非缓存命中"的真实解析结果做校验，非空文件却解析出空/乱码时显式告警，
    # 填补此前"解析失败静默返回空、下游误以为文件无内容"的缺口（不抛错以免中断上传流程）。
    if not used_cache:
        _report_parse_quality(file_path, result)

    # 存储缓存（仅缓存支持的格式）
    if ext in _PARSE_CACHEABLE_EXTS and file_hash:
        with _parse_cache_lock:
            if len(_parse_result_cache) >= _PARSE_CACHE_MAX:
                # BUG-FIX 2026-10-08：此前一次淘汰半数缓存（cache thrashing），
                # 现改为仅淘汰最旧的 1 条（LRU 单条淘汰）
                oldest = min(_parse_result_cache.items(), key=lambda x: x[1][1])
                _parse_result_cache.pop(oldest[0], None)
            _parse_result_cache[file_path] = (file_hash, time.time(), result)

    logger.info("[文件解析] %s 解析完成，提取 %d 字符, 耗时 %.2fs", ext, len(result), elapsed)
    return result


def _parse_quality_error_message(file_path: str, text: str) -> str:
    """把 error 级解析质量问题转成用户可读提示（无问题返回空串）。

    GAP-FIX 2026-10-26（R9，解析质量异常仍显示「成功」）：``_report_parse_quality``
    此前只写 ERROR 日志，文件行仍以 ``status='success'`` + ``message=''`` 落库，
    前端显示「解析成功」，用户完全看不到「文本几乎全是控制字符 / 大文件只解析出
    几十字符」这类异常。现在把 error 级结论回填到 ``uploaded_files.message``：
    - status 保持 success（不改变主文件选择与缓存语义，向后兼容）；
    - message 走前端既有的「成功但需注意」黄色提示位（FileUploadPage.tsx:715）。
    """
    try:
        issues = _assess_extraction(file_path, text)
    except Exception as _qe:  # pragma: no cover - 校验工具不应影响主流程
        logger.debug("[文件解析] 质量校验异常（已忽略）: %s", _qe)
        return ""
    errors = [i for i in issues if i.severity == "error"]
    if not errors:
        return ""
    return "解析质量异常：" + "；".join(i.message for i in errors)


def _report_parse_quality(file_path: str, text: str) -> None:
    """解析质量告警（标书智能体（一）引入）。

    对解析结果做质量校验，若存在 error 级问题则记录 ERROR 日志（不抛错，避免中断
    上传流程），使此前"静默返回空"的解析失败变为可观测。异常被吞掉以免校验本身
    影响主流程。
    """
    message = _parse_quality_error_message(file_path, text)
    if message:
        logger.error("[文件解析] %s -> %s", Path(file_path).name, message)


def _do_extract_text(file_path: str, ext: str, declared_ext: str = "") -> str:
    """根据文件后缀执行具体的提取逻辑

    ENHANCE 2026-10-05：扩展名集合统一引用模块级 :data:`SUPPORTED_EXTENSIONS` /
    :data:`IMAGE_EXTENSIONS`，路由层的放行白名单由同一真相派生（见 routers/files.py）。

    BUG-FIX 2026-10-06（R2）：新增 ``declared_ext``（用户声明的扩展名，可为空），
    OLE2 降级链据此把「声明格式」排到补试队列最前，避免声明 .xls 的文件
    白等一次 PowerPoint COM。
    """
    # BUG-FIX 2026-10-06（R2）：OLE2 容器（.doc/.xls/.ppt/.wps 共享同一魔数，
    # 魔数只能判出「是 OLE」判不出子格式）走降级链：任一子提取器产出真实文本
    # 即成功；全部失败时返回最后一个降级提示串（若有）让状态如实判 failed。
    # 护栏：仅当文件头确实是 OLE2 魔数、且 ext 来自「魔数嗅探」或「与声明
    # 扩展名一致」时才走链 —— 伪装扩展名的非 OLE 文件保持原单分支行为
    # （避免损坏/伪造文件被无谓拉起 antiword/catdoc/PowerPoint COM 试错）。
    # BUG-FIX 2026-10-09（格式误判 · 字段错位）：原条件
    #   `ext in (declared_ext, _sniff_extension(...) if not declared_ext else "")`
    # 里的 `if ... else ...` 只作用于元组**第二个元素**，当 declared_ext 非空时
    # 整个元组退化为 `(declared_ext, "")`。于是 .xls/.ppt/.wps 声明的文件被彻底
    # 排除在降级链之外：`.xls` 声明 → _sniff_extension 对 OLE2 一律返回 ".doc"
    # （容器格式无法区分子格式）→ ext 被改写成 ".doc" → `".doc" in (".xls", "")`
    # 为 False → 直接落到 `elif ext == ".doc"` 分支，用 Word 提取器（antiword/
    # catdoc/olefile Word 流）去解析 Excel 二进制。工程量清单/报价表的表格结构
    # 与数字全部错位成乱码，下游 get_boq_file_text / get_work_interface_file_text
    # 拿到的全是噪声，且 status 仍为 success（静默）。
    # 正确口径：文件头已确认是 OLE2，只要「声明扩展名本身是 4 个旧格式之一 /
    # 声明为空 / 声明与嗅探结果一致」就应进降级链（链内已把 declared_ext 排到
    # 优先位，不会白等 COM）。声明为 OOXML（.docx/.xlsx/.pptx）的伪装文件保持
    # 原单分支行为，避免为其多余拉起一次 PowerPoint COM（可能到 PPT_COM_TIMEOUT）。
    is_ole = _is_ole2_file(file_path)
    if ext in _LEGACY_OLE_EXTS and is_ole and (
        declared_ext in _LEGACY_OLE_EXTS or not declared_ext or declared_ext == ext
    ):
        return _extract_legacy_ole_chain(file_path, declared_ext)
    if ext == ".docx":
        return _extract_docx(file_path)
    elif ext == ".pdf":
        return _extract_pdf(file_path)
    elif ext == ".doc":
        result = _extract_doc(file_path)
        # Gap-6 2026-10-11：直连 _extract_doc 返回提示串/空串 且文件头是 OLE2 →
        # 回退降级链（伪装扩展名、antiword 损坏、OLE 容器类型不匹配都可能导致直连失败）
        if (is_extraction_notice(result) or not result) and is_ole:
            logger.warning(
                "[Gap-6] .doc 直连提取失败，回退 OLE2 降级链: %s (%s)", file_path, result[:40] if result else "empty"
            )
            return _extract_legacy_ole_chain(file_path, declared_ext or ".doc")
        return result
    elif ext == ".xlsx":
        return _extract_xlsx_openpyxl(file_path)
    elif ext == ".xls":
        result = _extract_xls(file_path)
        # Gap-6 2026-10-11：.xls 直连失败且文件头是 OLE2 → 回退降级链
        if (is_extraction_notice(result) or not result) and is_ole:
            logger.warning(
                "[Gap-6] .xls 直连提取失败，回退 OLE2 降级链: %s (%s)", file_path, result[:40] if result else "empty"
            )
            return _extract_legacy_ole_chain(file_path, declared_ext or ".xls")
        return result
    elif ext == ".txt":
        return _read_text_auto(file_path)
    elif ext == ".pptx":
        return _extract_pptx(file_path)
    elif ext == ".ppt":
        result = _extract_ppt(file_path)
        # Gap-6 2026-10-11：.ppt 直连失败且文件头是 OLE2 → 回退降级链
        if (is_extraction_notice(result) or not result) and is_ole:
            logger.warning(
                "[Gap-6] .ppt 直连提取失败，回退 OLE2 降级链: %s (%s)", file_path, result[:40] if result else "empty"
            )
            return _extract_legacy_ole_chain(file_path, declared_ext or ".ppt")
        return result
    elif ext in IMAGE_EXTENSIONS:
        if not OCR_ENABLED:
            logger.info("[文件解析] OCR 已禁用，跳过图片 OCR: %s", Path(file_path).name)
            return ""
        return _extract_image_ocr(file_path)
    elif ext in (".html", ".htm"):
        return _extract_html(file_path)
    elif ext == ".md":
        return _read_text_auto(file_path)
    elif ext == ".wps":
        result = _extract_wps(file_path)
        # Gap-6 2026-10-11：.wps 直连失败且文件头是 OLE2 → 回退降级链
        if (is_extraction_notice(result) or not result) and is_ole:
            logger.warning(
                "[Gap-6] .wps 直连提取失败，回退 OLE2 降级链: %s (%s)", file_path, result[:40] if result else "empty"
            )
            return _extract_legacy_ole_chain(file_path, declared_ext or ".wps")
        return result
    else:
        logger.warning("[文件解析] 不支持的文件格式: %s", ext)
        raise UnsupportedFileFormatError(
            f"不支持的文件格式: {ext}（支持的格式：{', '.join(sorted(SUPPORTED_EXTENSIONS))}）"
        )




def guess_file_role(filename: str) -> str:
    """根据文件名关键词猜测文件角色

    ENHANCE 2026-10-04（P3）：此前只要不命中辅助关键词就一律判 main，
    导致图片扫描件/杂件（如“照片.png”）也被当主招标文件，与真正的
    主文件（招标/采购/技术需求）竞争 _pick_main_file，一旦主文件解析失败
    会静默选中空内容的图片。现：
      1) 先命中辅助关键词（清单/工程量/报价等）→ supporting；
      2) 再命中主文件关键词（招标/采购/磋商/须知/评标等）→ main；
      3) 图片类扩展名且未命中任何关键词 → supporting（扫描件多为附件）；
      4) 其余默认 main（与旧行为一致，非图片杂件仍作主文件，向后兼容）。
    """
    name = Path(filename).stem.lower()
    supporting_keywords = [
        "清单",
        "工程量",
        "报价",
        "预算",
        "图纸",
        "附件",
        "supplement",
        "appendix",
        "list",
        "schedule",
    ]
    for kw in supporting_keywords:
        if kw in name:
            return "supporting"
    # 主文件正向关键词：命中则明确为 main（即便同时含其它字也优先归主）
    main_keywords = [
        "招标",
        "采购",
        "磋商",
        "谈判",
        "询价",
        "公告",
        "邀请",
        "投标人须知",
        "评标办法",
        "评标",
        "技术需求",
        "技术要求",
        "tender",
        "bid",
        "rfp",
        "itb",
    ]
    for kw in main_keywords:
        if kw in name:
            return "main"
    # 图片扫描件且无关键词：归为辅助，避免空内容图片顶替真正主文件
    ext = Path(filename).suffix.lower()
    if ext in (".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".webp"):
        return "supporting"
    return "main"


def _pick_main_file(files: list[dict]) -> dict | None:
    """根据 file_role 或文件名关键词判断主文件

    BUG-FIX 2026-09-18（高）：原实现先按 `file_role == 'main'` 直接返回，**不看 status**。
    主招标文件解析失败（status='failed'，提取文本为空）时仍会被选为「主文件」，
    导致下游摘要 / 评分点识别 / 目录生成全部基于空文本运行，且无任何告警。
    现在优先选「已成功解析的主文件」，仅在没有成功项时才回退到未成功的主文件。

    BUG-FIX 2026-10-15（F-3 主文件文本缺失被降级为摘要）：`_truncate_supporting_file`
    会校验 `extracted_text_path` 非空且文件存在（:1352-1354），但本函数只按 status
    选文件。一旦主文件行是 `status='success'` 而 .txt 缺失（历史脏行 / 磁盘被外部
    清理 / clear 与解析竞态），它仍会被选中，于是**真正的招标正文**被推到 supporting
    通道、被截断成 200 行「摘要」喂给 AI —— 全文分析质量静默劣化且无任何告警。
    现在在「已成功的主文件」内部再优先取「有可用提取文本」的那一个；没有任何
    主文件带有效文本时保持原有回退语义（既有测试锁定的回退行为不变）。
    """
    main_files = [f for f in files if f.get("file_role") == "main"]
    ok_main = [f for f in main_files if f.get("status") == "success"]
    if ok_main:
        # 优先「status=success 且提取文本确实可读」的主文件；判定口径与
        # _truncate_supporting_file 完全一致（非空路径 + exists），不重复造轮子。
        with_text = [
            f
            for f in ok_main
            if f.get("extracted_text_path")
            and Path(f["extracted_text_path"]).exists()
        ]
        if with_text:
            return with_text[0]
        return ok_main[0]
    if main_files:
        return main_files[0]

    # 无明确标记时选第一个 success 的文件
    success_files = [f for f in files if f.get("status") == "success"]
    return success_files[0] if success_files else (files[0] if files else None)


async def _truncate_supporting_file(files: list[dict]) -> str:
    """辅助文件智能截断，保留关键字行（异步 I/O）。

    策略：优先保留含关键字的行（项目/技术/报价/清单/工程量/规格等），
    不足时按比例补充普通行，至多保留 200 行，避免招标清单（报价、工程量）
    被整段丢弃导致 BOQ/清单分析质量下降。
    """
    keep_keywords = [
        "项目",
        "技术",
        "要求",
        "标准",
        "规范",
        "名称",
        "编号",
        "报价",
        "清单",
        "工程量",
        "单价",
        "合价",
        "规格",
        "型号",
        "数量",
        "单位",
        "金额",
        "标段",
        "材料",
        "设备",
    ]
    max_lines = 200
    parts = []
    for f in files:
        if f.get("status") != "success":
            continue
        text_path = f.get("extracted_text_path")
        if not text_path or not Path(text_path).exists():
            continue
        # BUG-FIX 2026-09-18（高）：此处硬编码 utf-8，GBK/GB2312 编码的清单、
        # 工程量等辅助 txt 会抛 UnicodeDecodeError，异常直接冒出
        # latest_extracted_text()，导致整个项目的招标上下文提取失败。
        # 主文本路径已改用 _read_text_auto 自动降级编码，辅助文本此前漏改。
        text = await asyncio.to_thread(_read_text_auto, text_path)
        if not text.strip():
            continue
        lines = text.splitlines()
        kept = [line for line in lines if any(kw in line for kw in keep_keywords)]
        if len(kept) < max_lines:
            # 关键字行不足时，按比例补充普通行（跳过空白行）
            needed = max_lines - len(kept)
            ordinary = [ln for ln in lines if ln.strip() and ln not in kept]
            kept = kept + ordinary[:needed]
        if kept:
            parts.append(f"\n--- 辅助文件: {f['filename']} (摘要) ---\n")
            parts.append("\n".join(kept[:max_lines]))
    return "\n".join(parts)


# 项目文本缓存：project_id -> (text, timestamp)
_text_cache: dict[int, tuple[str, float]] = {}
_text_cache_lock = threading.Lock()
_TEXT_CACHE_TTL = 600  # 秒（AI 连续多次分析时避免重复拼接大量文本）
_TEXT_CACHE_MAX = 50  # 最多缓存 50 个项目的合并文本（每个文本可能很大）

# 2026-10-11 加固（I-10）：项目版本号，防止并发上传/删除时读到中间态并写入陈旧缓存。
# 每次 _text_cache_clear（即文件上传/删除/重提取等变更）时递增版本号；
# _text_cache_set 在写入前检查版本是否已变更，若已变更则跳过写入（避免陈旧数据覆写）。
_project_versions: dict[int, int] = {}


def _get_project_version(project_id: int) -> int:
    """获取当前项目版本号（单调递增，初始为 0）。"""
    with _text_cache_lock:
        return _project_versions.get(project_id, 0)


def _text_cache_get(project_id: int) -> str | None:
    with _text_cache_lock:
        entry = _text_cache.get(project_id)
    if entry is None:
        return None
    text, ts = entry
    if time.time() - ts > _TEXT_CACHE_TTL:
        with _text_cache_lock:
            _text_cache.pop(project_id, None)
        return None
    return text


def _text_cache_set(project_id: int, text: str, since_version: int | None = None) -> None:
    with _text_cache_lock:
        # I-10：若版本号已变更（并发上传/删除），跳过写入，避免陈旧数据覆写。
        if since_version is not None and _project_versions.get(project_id, 0) != since_version:
            logger.debug(
                "跳过缓存写入 project=%d: 版本已从 %d 变更为 %d（并发变更）",
                project_id, since_version, _project_versions.get(project_id, 0),
            )
            return
        # BUG-FIX 2026-10-08：此前一次淘汰半数缓存（cache thrashing），
        # 现改为仅淘汰最旧的 1 条（LRU 单条淘汰）
        if len(_text_cache) >= _TEXT_CACHE_MAX:
            oldest = min(_text_cache.items(), key=lambda x: x[1][1])
            _text_cache.pop(oldest[0], None)
        _text_cache[project_id] = (text, time.time())


def _text_cache_clear(project_id: int) -> None:
    """使项目文本缓存失效（上传/删除/重提取/清空后调用，防止 AI 使用旧文本）

    BUG-FIX 2026-10-11（Race Condition）：此前 ``pop`` 与版本递增分在两个独立的
    ``with _text_cache_lock`` 块中 —— 两个锁之间的间隙可能被并发读
    （latest_extracted_text 的 start_version 记录）切入，在 pop 之后、版本
    递增之前写入陈旧缓存条目且带着**旧**版本号，从而绕过 I-10 版本守卫。
    现合并为单锁块，保证「缓存清除 + 版本递增」原子完成。
    """
    with _text_cache_lock:
        _text_cache.pop(project_id, None)
        _project_versions[project_id] = _project_versions.get(project_id, 0) + 1


def _latest_text_query_rows(project_id: int):
    """TD-13 2026-10-12（事件循环阻塞·读链一致性）：``latest_extracted_text`` 此前在
    ``async def`` 内**直连** `with get_db(readonly=True)` 做同步查询 —— 这条只读
    查询虽不抢写锁，但仍跑在事件循环线程上，与同模块 ``list_files`` /
    ``get_file_text`` 已由 router 用 ``asyncio.to_thread`` 包出的口径不一致；
    且合并文本路径读取（_read_text_auto）虽已在线程池，但「取路径列表」这一步
    仍是事件循环内的阻塞 DB 往返。现抽为同步函数并整体移入线程池，与模块既有的
    「DB/IO 一律 to_thread」约定完全对齐（行为、返回形状不变）。
    """
    with get_db(readonly=True) as conn:
        return conn.execute(
            "SELECT id, filename, file_type, file_role, status, extracted_text_path "
            "FROM uploaded_files WHERE project_id = ? ORDER BY id",
            (project_id,),
        ).fetchall()


async def latest_extracted_text(project_id: int) -> str:
    """合并主文件和辅助文件，主文件优先（带项目级缓存）"""
    cached = _text_cache_get(project_id)
    if cached is not None:
        return cached

    # I-10：记录读取起始版本号，若并发变更导致版本递增，写入时跳过
    start_version = _get_project_version(project_id)

    # BUG-FIX 2026-09-16（高 事件循环阻塞）：原实现在 `with get_db()` **内部**执行
    # 多 MB 正文文件的读取与辅助文件截断。数据库连接看上去是在 asyncio 线程里拿的，
    # 但这把是**全局写锁**：一个「读取项目文本」请求（GET /files/text）会把全进程
    # 的写操作挡在锁外，而它本身主要时间在磁盘 IO 上。
    # 现在：DB 查询只用只读连接速取路径（不抢写锁），IO 全部在 `with` 之外做。
    # TD-13 2026-10-12：即便只是只读连接的查询，也整体移入线程池，避免事件循环
    # 线程内的任何同步 DB 往返（与 list_files/get_file_text 的 to_thread 口径一致）。
    rows = await asyncio.to_thread(_latest_text_query_rows, project_id)

    if not rows:
        return ""

    files = [dict(r) for r in rows]
    main = _pick_main_file(files)
    # BUG-FIX 2026-10-28（P2 数据链断点）：主文件仍在异步解析（processing/pending，
    # extracted_text_path 为空）时，_pick_main_file 会回退到「首个未成功主文件」，
    # main_text 为空 → 下方辅助文件 200 行摘要被顶替为「正文」喂给 AI，全文事实
    # 提取质量静默劣化且无告警。此处显式检测：项目存在未完成解析的主文件且无任何
    # 成功主文件文本时，返回空（下游视为「提取未完成」，前端门禁同步拦截）。
    # 仅当确实没有任何 success 主文件文本时才拦截——有成功主文件文本则正常合并。
    _main_unfinished = [
        f for f in files
        if f.get("file_role") == "main"
        and f.get("status") in ("processing", "pending", "parsing")
    ]
    _any_main_text_ready = any(
        f.get("file_role") == "main"
        and f.get("status") == "success"
        and f.get("extracted_text_path")
        and Path(f["extracted_text_path"]).exists()
        for f in files
    )
    if _main_unfinished and not _any_main_text_ready:
        logger.warning(
            "主文件仍在解析（processing/pending）且无成功主文件文本，"
            "latest_extracted_text 返回空（避免辅助摘要顶替），project=%d unfinished=%d",
            project_id, len(_main_unfinished),
        )
        return ""
    # ENHANCE 2026-10-05（TD4 技术债）：原为 `f != main`（dict 全等比较）——语义
    # 依赖「两个 dict 恰好不完全相等」，一旦 SELECT 字段变化（如去掉 id）就会把
    # 主文件自己也当成辅助文件再拼一遍，造成正文重复。改为按 id 精确排除。
    main_id = (main or {}).get("id")
    supporting = [f for f in files if f.get("id") != main_id]

    main_text = ""
    if main and main.get("extracted_text_path"):
        p = Path(main["extracted_text_path"])
        if p.exists():
            # BUG-FIX-66: 使用 _read_text_auto 自动降级编码，避免 GBK 文件解码失败
            main_text = await asyncio.to_thread(_read_text_auto, str(p))
    # BUG-FIX 2026-10-06（H1）：_pick_main_file 在「没有任何 success 主文件」时
    # 会按设计回退到未成功的主文件（该回退语义被既有测试锁定，不改）。但当项目
    # 全部是老格式 .doc/.wps 且都提取失败时，回退选中的主文件文本正是那句
    # 「[提示] 无法自动提取…」，会原样拼进合并文本喂给 AI。这里补一道防御：
    # 提示串一律视为空文本（不改变 _pick_main_file 的选文件行为，只丢弃
    # 假正文），同时兼容本修复上线前遗留的 success 状态脏行。
    if is_extraction_notice(main_text):
        main_text = ""

    supporting_text = await _truncate_supporting_file(supporting)

    # 锁外再查一次缓存：并发请求可能已经算过同一份文本，避免重复读大文件
    raced = _text_cache_get(project_id)
    if raced is not None:
        return raced

    result = f"{main_text}\n{supporting_text}".strip()
    _text_cache_set(project_id, result, since_version=start_version)
    return result


def get_project_files(project_id: int) -> list[dict]:
    """获取项目的文件列表

    TD-12 2026-10-12（技术债·读锁）：本函数只做 SELECT，此前却走 `get_db()`
    抢全局写锁。批量上传高峰期 save_upload 频繁拿写锁，列表/文本读取若也抢写锁
    会互相排队（虽已被 router 用 asyncio.to_thread 移出事件循环，但锁本身仍串行）。
    db.py 明确约定「只读查询传 readonly=True 可跳过全局写锁」，这里对齐。
    """
    with get_db(readonly=True) as conn:
        rows = conn.execute(
            "SELECT id, filename, file_type, file_role, status, message, created_at "
            "FROM uploaded_files WHERE project_id = ? ORDER BY id",
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_file_text(project_id: int, file_id: int) -> str:
    """获取指定文件的提取文本

    TD-12 2026-10-12（技术债·读锁）：仅 SELECT，改走 readonly 连接跳过全局写锁
    （与 get_project_files 同口径，理由见其 docstring）。router 已用
    asyncio.to_thread 包裹本函数，但连接层面仍不应抢占写锁。
    """
    with get_db(readonly=True) as conn:
        row = conn.execute(
            "SELECT extracted_text_path FROM uploaded_files " "WHERE id = ? AND project_id = ?",
            (file_id, project_id),
        ).fetchone()
        if not row or not row["extracted_text_path"]:
            return ""
        p = Path(row["extracted_text_path"])
        # BUG-FIX-66: 使用 _read_text_auto 自动降级编码
        return _read_text_auto(str(p)) if p.exists() else ""


_BOQ_FILE_KEYWORDS = ["清单", "工程量", "boq", "quantity"]
_WORK_INTERFACE_FILE_KEYWORDS = ["界面", "工作界面", "interface"]
_CONSTRUCTION_METHOD_FILE_KEYWORDS = ["作法", "工程作法", "做法", "construction"]
_FACTS_FILE_KEYWORDS = ["事实", "global fact", "fact"]


def _first_text_by_keywords(project_id: int, keywords: list[str]) -> str:
    """按文件名关键词取首个「已解析成功且文本文件仍在」的提取文本

    ENHANCE 2026-10-05（TD2 技术债）：get_boq_file_text /
    get_work_interface_file_text / get_construction_method_file_text /
    get_facts_file_text 四个函数除关键词常量外**逐行同构**（SQL + 循环 + 读取
    共 15 行），任何一处编码/路径修复都要改四遍（BUG-FIX-66/76 就是这么漏的）。
    合并为单一实现，四个公开函数保留原签名与行为（零迁移）。
    """
    with get_db(readonly=True) as conn:
        rows = conn.execute(
            "SELECT id, filename, status, extracted_text_path "
            "FROM uploaded_files WHERE project_id = ? AND status = 'success' ORDER BY id",
            (project_id,),
        ).fetchall()
    for row in rows:
        filename = (row["filename"] or "").lower()
        if not any(kw in filename for kw in keywords):
            continue
        text_path = row["extracted_text_path"]
        if text_path and Path(text_path).exists():
            # 编码自动降级（BUG-FIX-66/76）
            return _read_text_auto(text_path)
    return ""


def get_boq_file_text(project_id: int) -> str:
    """获取项目中的清单文件（优先找含清单/工程量关键词的文件）的提取文本"""
    return _first_text_by_keywords(project_id, _BOQ_FILE_KEYWORDS)


def get_work_interface_file_text(project_id: int) -> str:
    """获取项目中的工作界面文件（优先找含界面/工作界面关键词的文件）的提取文本"""
    return _first_text_by_keywords(project_id, _WORK_INTERFACE_FILE_KEYWORDS)


def get_construction_method_file_text(project_id: int) -> str:
    """获取项目中的工程作法文件（优先找含作法/做法/construction关键词的文件）的提取文本"""
    return _first_text_by_keywords(project_id, _CONSTRUCTION_METHOD_FILE_KEYWORDS)


def get_facts_file_text(project_id: int) -> str:
    """获取项目中的事实变量文件（优先找含事实/global fact关键词的文件）的提取文本"""
    return _first_text_by_keywords(project_id, _FACTS_FILE_KEYWORDS)


def set_file_role(project_id: int, file_id: int, role: str) -> dict:
    """手动设置上传文件的主/辅助角色

    ENHANCE 2026-10-04（P4）：自动角色检测（guess_file_role）基于文件名关键词，
    命名不规范时会误判。提供手动纠正入口：_pick_main_file 依赖 file_role 选主
    文件，直接影响摘要/评分点/清单/目录生成等下游全链路，因此变更后必须
    使项目合并文本缓存失效。

    Args:
        role: "main"（主文件）/ "supporting"（辅助文件）

    Raises:
        MainFileRequiredError: 降级项目唯一 main（router 转 422，REVIEW-FIX M2）。
        ValueError: 非法角色值（router 转 400）或文件不存在（router 转 404）。
    """
    if role not in ("main", "supporting"):
        raise ValueError(f"非法的文件角色: {role}（仅支持 main / supporting）")
    demoted: list[int] = []
    needs_reanalysis = False
    with get_db() as conn:
        row = conn.execute(
            "SELECT filename, file_role FROM uploaded_files WHERE id = ? AND project_id = ?",
            (file_id, project_id),
        ).fetchone()
        if not row:
            raise ValueError("文件不存在")
        prev_role = row["file_role"]
        # REVIEW-FIX 2026-10-04（M2）：禁止把项目唯一 main 降为 supporting。
        # 0 main 时 _pick_main_file 静默回退按 id 取首个成功文件，可能选中
        # 空文本图片，招标正文被截成辅助摘要且接口返回 200 用户无感。
        if role == "supporting" and prev_role == "main":
            other_main = conn.execute(
                "SELECT 1 FROM uploaded_files "
                "WHERE project_id = ? AND file_role = 'main' AND id != ? LIMIT 1",
                (project_id, file_id),
            ).fetchone()
            if not other_main:
                raise MainFileRequiredError(
                    "项目必须保留一个主文件：请先将其他文件设为主文件，再降级本文件"
                )
        # BUG-FIX 2026-10-04（跨模块数据链）：main 角色必须全项目互斥。
        # _pick_main_file 按 id 顺序取首个 main，此前可同时存在多个 main，
        # 手工切换新主文件后下游摘要/评分仍读旧主文件，且无任何提示。
        # 现将其余 main 行降为 supporting，并随响应透出（前后端同步）。
        if role == "main":
            downgraded = conn.execute(
                "SELECT id FROM uploaded_files WHERE project_id = ? AND file_role = 'main' AND id != ?",
                (project_id, file_id),
            ).fetchall()
            demoted = [r["id"] for r in downgraded]
            if demoted:
                conn.execute(
                    "UPDATE uploaded_files SET file_role = 'supporting', "
                    "updated_at = datetime('now', 'localtime') "
                    "WHERE project_id = ? AND file_role = 'main' AND id != ?",
                    (project_id, file_id),
                )
        conn.execute(
            "UPDATE uploaded_files SET file_role = ?, updated_at = datetime('now', 'localtime') "
            "WHERE id = ? AND project_id = ?",
            (role, file_id, project_id),
        )
        # REVIEW-FIX 2026-10-04（m2）：主文件选择实际换源（新旧角色不同）且
        # analysis 已有缓存产物时，旧摘要/评分仍基于旧主文件，透出
        # needs_reanalysis 供前端提示重新生成（非破坏式，不清已确认数据）。
        if prev_role != role:
            needs_reanalysis = _analysis_has_cached(conn, project_id)
    # 主文件选择已变化，使合并文本缓存失效（否则下游 AI 仍用旧拼接结果）
    _text_cache_clear(project_id)
    return {
        "status": "success",
        "file_id": file_id,
        "filename": row["filename"],
        "file_role": role,
        "demoted_file_ids": demoted,
        "needs_reanalysis": needs_reanalysis,
    }


def _promote_next_main(conn, project_id: int) -> int | None:
    """在「项目已没有 main」时把剩余文件中最合适的一个提升为主文件。

    选取顺序与 :func:`_pick_main_file` 的静默回退完全一致（先首个
    ``status='success'``，其次 id 最小者），因此提升操作**不改变**下游实际取到的
    文本，只是把隐式回退显式化：文件列表能正确显示「主文件」标签，
    ``_pick_main_file`` 也不必再走静默分支。
    """
    remaining = conn.execute(
        "SELECT id, status FROM uploaded_files WHERE project_id = ? ORDER BY id",
        (project_id,),
    ).fetchall()
    if not remaining:
        return None
    target = next(
        (r["id"] for r in remaining if r["status"] == "success"), remaining[0]["id"]
    )
    conn.execute(
        "UPDATE uploaded_files SET file_role = 'main', updated_at = datetime('now', 'localtime') "
        "WHERE id = ? AND project_id = ?",
        (target, project_id),
    )
    return target


def delete_file(project_id: int, file_id: int) -> dict:
    """删除上传文件（删除数据库记录及物理文件）

    ENHANCE 2026-10-05（B5 不变式一致性）：``set_file_role`` 用 422 拒绝「把项目
    唯一 main 降级为 supporting」，理由是 0 main 态下 :func:`_pick_main_file` 会静默
    回退「按 id 取首个成功文件」，下游摘要/评分/目录取错文本且 UI 上没有「主文件」
    标签可核对。但**删除路径此前没有同一不变式** —— 删掉唯一主文件同样能进入
    0 main 态，两条写入路径口径不一致。现在删除后若项目已无 main 且仍有文件，
    自动提升最合适的一个（顺序与静默回退一致，下游取到的文本不变）。
    """
    promoted_file_id = None
    # BUG-FIX（2026-10-28 跨模块深度探索 F6）：delete_file 此前不取项目上传写锁，
    # 与并发 `save_upload`（已持 `_get_upload_write_lock`）存在竞态：DB 删除后、
    # 物理 unlink 前，save_upload 可在同一临界区把同名文件落盘到**同一路径**；
    # 随后 delete_file 的 `_p.unlink()` 会把新上传文件删除，而 DB 里新行指向
    # 已删除路径（悬空）。现整个「DB 删除 + 物理删除」临界区纳入项目写锁，
    # 与 save_upload 串行化（clear_project_texts 已同口径，delete_file 是漏网）。
    with _get_upload_write_lock(project_id):
        with get_db() as conn:
            row = conn.execute(
                "SELECT path, extracted_text_path FROM uploaded_files WHERE id = ? AND project_id = ?",
                (file_id, project_id),
            ).fetchone()
            if not row:
                raise ValueError("文件不存在")

            file_path = Path(row["path"])
            text_path = row["extracted_text_path"]

            conn.execute(
                "DELETE FROM uploaded_files WHERE id = ? AND project_id = ?",
                (file_id, project_id),
            )

            # 删除后维持「有文件则至少有一个显式 main」的不变式
            still_main = conn.execute(
                "SELECT 1 FROM uploaded_files WHERE project_id = ? AND file_role = 'main' LIMIT 1",
                (project_id,),
            ).fetchone()
            if not still_main:
                promoted_file_id = _promote_next_main(conn, project_id)

        # BUG-FIX 2026-09-16（低）：DB 记录已删后物理文件 unlink 失败（Windows 上
        # 文件正被解析进程占用会 PermissionError）原实现直接抛 500，且无兜底。
        # 记录删除是主操作，物理残留仅告警，不影响接口成功。
        # BUG-FIX（2026-10-28 F6）：unlink 保留在项目写锁临界区内，与 save_upload
        # 的落盘互斥，杜绝「unlink 删除新上传同名文件」竞态。
        for _p in [file_path, *( [Path(text_path)] if text_path else [] )]:
            try:
                if _p.exists():
                    _p.unlink()
            except OSError as _unlink_e:
                logger.warning(
                    "删除物理文件失败（记录已删除，文件将残留）path=%s: %s", _p, _unlink_e
                )

    # 项目文本已变化，使合并文本缓存失效
    _text_cache_clear(project_id)

    # BUG-FIX 2026-10-15（F-4 缓存未失效）：删除路径此前只清「合并文本缓存」，
    # 却不清「单文件解析结果缓存」。_clear_parse_cache 在另三条写入路径
    # （re_extract_text / _persist_file_extraction / clear_project_texts）都已调用，
    # 删除是漏网之鱼。后果：删除后**重新上传同名文件**时，若新文件的
    # (size, mtime) 哈希恰与旧文件相同（覆盖式导出、模板复用、mtime 精度粒度内
    # 重传都很常见），extract_text_from_path 会直接命中旧缓存、把**已删除文件**的
    # 正文当成新文件正文写回 —— 解析结果错配且无任何告警。
    try:
        _clear_parse_cache(str(file_path))
    except Exception as _dc_e:  # pragma: no cover - 缓存清理失败不影响删除主流程
        logger.debug("删除文件后清理解析缓存失败（忽略）file=%s: %s", file_path, _dc_e)

    return {
        "status": "deleted",
        "file_id": file_id,
        # ENHANCE 2026-10-05（B5）：删除主文件后自动补位的新主文件 id（无剩余文件时为 null）
        "promoted_file_id": promoted_file_id,
    }


def clear_project_texts(project_id: int) -> dict:
    """清理项目的提取文本（用于重新上传后重置）

    返回值（Gap-1 2026-10-11 新增）：
      {status: 'cleared', files_reset: N, tasks_cleared: M, warning: ''}

    M-5 2026-10-15：`warning` 非空表示「文本已删但 DB 状态重置失败」，
    前端必须显式告警，不能继续播报「已重置 0 个文件」这种误导性成功。

    BUG-FIX 2026-10-05（B6 数据链断裂）：原实现只删 ``texts/`` 目录与合并文本缓存，
    完全不动 ``uploaded_files`` —— 记录仍显示 ``status='success'`` 且
    ``extracted_text_path`` 指向已被删除的文件，``GET /files/{id}/text`` 静默返回
    空串、合并文本也随之变空，界面上却看不出任何异常（仍显示"已提取"）。
    现在把 DB 侧一并重置为 ``status='pending'`` + 空路径 + 空 message，
    与实际文件状态保持一致（pending 在前端状态映射中已有"待提取"文案）。

    Gap-1 2026-10-11 增强：
      1. 加上传写入锁保护 —— 防止与并发 save_upload 竞态（清空刚落盘的文件）。
      2. 清理 file_parse_tasks 终态行 —— 让 GET /tasks/files/status 不再显示
         已清空项目的旧解析记录。在途 processing 行一律保留。
      3. 返回结构化结果 —— 便于 DELETE /projects/{id}/texts 端点向前端透出
         实际重置的文件数与清理的任务行数（响应体不再是 silent void）。
    """
    write_lock = _get_upload_write_lock(project_id)
    with write_lock:
        texts_dir = PROJECTS_DATA_DIR / str(project_id) / "texts"
        if texts_dir.exists():
            shutil.rmtree(texts_dir)
            texts_dir.mkdir(exist_ok=True)

        # BUG-FIX 2026-10-09（缓存未失效 · 重置后重提取复活旧结果）：只删 texts/ 与
        # 合并文本缓存，解析结果缓存 _parse_result_cache（TTL 300s）仍以原上传文件路径
        # 为键保留着上一次的结果 —— 其中可能是降级提示串或空串。用户「清空后重新提取」
        # 走 re_extract_text 时会按单文件清一次，但走异步重解析入口、或清空后直接触发
        # 分析链（GET /files/text → latest_extracted_text 回落到重提取）时并不会清，
        # 于是清空操作在 TTL 内被缓存抵消：界面上显示已重置，拿到的仍是旧文本。
        # 这里在删目录的同时把该项目全部上传文件的解析缓存一并失效。
        try:
            with get_db(readonly=True) as _conn:
                _rows = _conn.execute(
                    "SELECT path FROM uploaded_files WHERE project_id = ?",
                    (project_id,),
                ).fetchall()
            for _r in _rows:
                _p = _r["path"] if hasattr(_r, "keys") else _r[0]
                if _p:
                    _clear_parse_cache(str(_p))
        except Exception as e:
            logger.warning("清空项目文本后失效解析缓存失败 project=%d: %s", project_id, e)

        files_reset = 0
        tasks_cleared = 0
        reset_warning = ""
        files_kept_processing = 0
        try:
            with get_db() as conn:
                # BUG-FIX 2026-10-26（清空与在途解析竞态 · 幽灵任务）：此前 WHERE
                # 无 status 过滤，把**正在后台解析**（status='processing'）的行也一并
                # 重置为 'pending'。后果有两层：
                #   1) 'pending' 在语义上是「已入库、从未解析过」，而该文件此刻正有
                #      Celery 任务在跑 —— 行状态与真实世界不一致；
                #   2) 前端 FileUploadPage 把 pending 当「在途」注册进 taskStore
                #      （inFlight = processing || queued || pending），于是给一个
                #      **永远不会再执行的解析**注册了 running 任务，顶栏永久显示
                #      「文件解析 0%」，只有离开页面才消失。
                # 现只对非在途行做重置；在途行的 texts/ 已被删除，但对应 worker 完成
                # 时会经 _persist_file_extraction 重新落盘并写终态，语义自洽。
                # files_reset 计数口径不变（仍只统计本次被重置的行）。
                kept = conn.execute(
                    "SELECT COUNT(*) AS c FROM uploaded_files "
                    "WHERE project_id = ? AND status = 'processing'",
                    (project_id,),
                ).fetchone()
                files_kept_processing = int(kept["c"]) if kept else 0
                cur = conn.execute(
                    "UPDATE uploaded_files SET status = 'pending', message = '', "
                    "extracted_text_path = '', updated_at = datetime('now', 'localtime') "
                    "WHERE project_id = ? AND status != 'processing'",
                    (project_id,),
                )
                files_reset = cur.rowcount or 0

                # Gap-1 2026-10-11：同时清理该项目的解析任务终态行（保留 processing 行）
                # —— 让解析状态端点 GET /tasks/files/status 不再展示已清空项目的历史记录
                cur2 = conn.execute(
                    "DELETE FROM file_parse_tasks WHERE project_id = ? AND status IN ('completed', 'failed')",
                    (project_id,),
                )
                tasks_cleared = cur2.rowcount or 0
        except Exception as e:
            # BUG-FIX 2026-10-15（M-5 · 破坏性操作假成功）：texts/ 目录此刻已经
            # 真删（不可回滚），但 DB 行仍停在 status='success' 且
            # extracted_text_path 指向刚被删掉的文件 —— 磁盘与库不一致。
            # 此前这里 logger.warning 后照常返回 {'status': 'cleared',
            # files_reset: 0}，前端弹「已重置 0 个文件状态为待提取」，用户以为
            # 清空完成，实际列表仍显示「已提取」且取文为空（悬空路径）。
            # 现把失败如实透出到响应体（新增 warning 字段），由前端显式告警。
            logger.error("清理项目文本后重置文件状态失败 project=%d: %s", project_id, e)
            reset_warning = (
                f"提取文本已删除，但数据库状态重置失败（{str(e)[:80]}），"
                "文件列表仍显示已提取；请对相关文件执行「重新提取」或重新上传。"
            )

    # 文本已清空，使合并文本缓存失效
    _text_cache_clear(project_id)

    logger.info(
        "clear_project_texts project=%d: 重置 %d 个文件状态为 pending，清理 %d 条解析任务终态行，"
        "保留 %d 个在途 processing 行",
        project_id, files_reset, tasks_cleared, files_kept_processing,
    )
    return {
        "status": "cleared",
        "files_reset": files_reset,
        "tasks_cleared": tasks_cleared,
        # BUG-FIX 2026-10-26：新增（可选字段，老前端忽略）—— 在途解析行未被重置的数量，
        # 前端据此提示「有 N 个文件正在解析中，未受本次清空影响」，避免用户误以为
        # 这批文件也被清空了（它们的 worker 稍后会重新落盘并写终态）。
        "files_kept_processing": files_kept_processing,
        # M-5 2026-10-15：仅在「文本已删但 DB 重置失败」时非空，前端据此告警
        "warning": reset_warning,
    }


def re_extract_text(project_id: int, file_id: int) -> dict:
    """重新提取指定文件的文本（依赖补装后的重试）"""
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, filename, path, extracted_text_path FROM uploaded_files WHERE id = ? AND project_id = ?",
            (file_id, project_id),
        ).fetchone()
        if not row:
            raise ValueError("文件不存在")

        file_path = Path(row["path"])
        if not file_path.exists():
            raise ValueError("原始文件已不存在，请重新上传")

        filename = row["filename"]
        old_text_path = row["extracted_text_path"]

    # BUG-FIX 2026-09-18（P2）：重新提取前必须清解析缓存——缓存键是
    # (size, mtime) 哈希，首次解析失败/降级时存入的可能是降级提示串
    # （如 "[提示] 当前系统无法自动提取…"），依赖补装后 mtime 未变会
    # 直接命中旧缓存，重提取永远拿回坏结果。
    _clear_parse_cache(str(file_path))

    # ENHANCE 2026-10-05（G3 任务表断链）：save_upload 会建 file_parse_tasks 行并把
    # 终态写回，而 re_extract 此前完全不碰任务表 —— GET /tasks/files/status 看到的
    # 永远是「上一次上传」的终态，重提取失败在该端点上不可感知（前端进度条一直显示
    # 上一次的 completed）。这里补上同口径的任务行，使两条写入路径状态一致。
    try:
        _create_parse_task(project_id, filename, Path(filename).suffix.lower())
    except Exception as _task_err:
        # P-3 加固：任务行创建失败说明 DB 不可用，继续重提取会导致后续
        # _update_parse_task UPDATE 0 行（终态丢失）。直接返回失败让上游感知。
        logger.error("重提取任务行创建失败，中止重提取: %s", _task_err)
        return {
            "status": "failed",
            "message": f"任务行创建失败（数据库不可用）: {_task_err}",
            "filename": filename,
        }

    try:
        extracted_text = extract_text_from_path(str(file_path))
        # BUG-FIX 2026-10-06（R1）：与 save_upload / Celery 解析任务同口径 ——
        # 老格式（.doc/.ppt/.wps）提取失败返回的「[提示] 无法自动提取…」降级串
        # 不是正文，不能写 status='success'，否则提示串文件会被 _pick_main_file
        # 选作项目主文本喂给下游 AI（H1 中毒链路在重提取入口的漏网之鱼）。
        if is_extraction_notice(extracted_text):
            status = "failed"
            message = extracted_text.strip()
            # BUG-FIX 2026-10-06（R1/G3）：任务行终态与文件状态同口径 ——
            # 此前文件判 failed 但任务行仍写 completed，GET /tasks/files/status
            # 的 recent_failed 恒为 0，重提取失败在任务端点不可感知。
            _update_parse_task(project_id, "failed", 100, message, filename)
        else:
            status = "success"
            message = ""
            # BUG-FIX 2026-10-05（B4）：与 save_upload 同口径 —— 重提取成功但文本仍为空时
            # 如实写提示，否则前端的「未提取到文本」黄色告警会在点「重新提取」后消失，
            # 用户误以为已修复（解析结果丢失无感知）。
            if not extracted_text.strip():
                message = EMPTY_TEXT_MESSAGE
            else:
                # GAP-FIX 2026-10-26（R9）：重提取同样是「非空但质量为 error」的场景，
                # 与 save_upload 同口径回填提示。
                message = _parse_quality_error_message(str(file_path), extracted_text)
            _update_parse_task(
                project_id, "completed", 100, error_message=message, filename=filename
            )
    except Exception as e:
        extracted_text = ""
        status = "failed"
        message = f"文本提取失败: {e}"
        logger.error("文件 %s 重新提取文本失败: %s", filename, e)
        _update_parse_task(project_id, "failed", 50, message, filename)

    project_dir = PROJECTS_DATA_DIR / str(project_id)
    texts_dir = project_dir / "texts"
    texts_dir.mkdir(parents=True, exist_ok=True)

    text_filename = f"{Path(filename).name}.txt"
    text_path = texts_dir / text_filename
    # BUG-FIX 2026-09-18（中）：原实现写文本无 try 保护，写失败会直接抛异常，
    # 使该 file_id 的状态永远停在旧值；且旧文本在 DB 更新之前就被 unlink，
    # 一旦 DB 更新失败则新旧文本皆失，文件彻底失联。现改为：写失败如实返回
    # failed 且保留旧文本；DB 更新成功后再清理旧文件，清理失败只告警。
    #
    # BUG-FIX 2026-10-12（悬空路径 · 字段错位）：此前**无条件** write_text，
    # 提取失败（extracted_text 为空串）时也会在磁盘上留下一个 0 字节 .txt，
    # 并把该路径写进 uploaded_files.extracted_text_path —— 与 save_upload 在
    # 2026-10-08 建立的口径（空文本不落盘）直接冲突，等于同一模块两套行为：
    #   * `ls texts/` 里凭空多出空文件，运维/清理脚本无法区分「解析出空正文」
    #     与「解析失败」；
    #   * DB 指向的路径存在但内容为空，下游 `latest_extracted_text` 与
    #     `_first_text_by_keywords` 的 `Path(path).exists()` 判空守卫被绕过
    #     （它们只判存在性，不判大小），于是把空串当成正文合并进上下文，
    #     AI 侧表现为「明明解析成功了却提不出任何要点」。
    # 现与 save_upload 对齐：仅在文本非空时落盘；未落盘时把 DB 字段置空串。
    text_written = False
    if extracted_text.strip():
        # F-G FIX 2026-10-26：先写临时文件再原子替换（同目录 os.replace）。
        # 原来直接 write_text 目标文件，'w' 模式会**先截断再写入**：一旦磁盘满/
        # 权限拒绝，旧文本已经被截断，所谓「保留旧结果」并不成立。改为临时文件
        # 后，失败时目标文件仍是上一次的完整内容，「保留旧结果」才可如实承诺。
        tmp_text_path = text_path.with_name(text_path.name + ".tmp")
        try:
            tmp_text_path.write_text(extracted_text, encoding="utf-8")
            os.replace(tmp_text_path, text_path)
            text_written = True
        except Exception as e:
            try:
                if tmp_text_path.exists():
                    tmp_text_path.unlink()
            except Exception:  # pragma: no cover - 清理失败不影响主流程
                pass
            logger.error("重新提取：写入提取文本失败 file_id=%s: %s", file_id, e)
            _update_parse_task(project_id, "failed", 0, f"写入提取文本失败: {e}", filename)
            # F-G FIX 2026-10-26（写盘失败口径不明确）：此前该分支直接 return，
            # DB 里 status/extracted_text_path 保持旧值——旧文本确实保住了，但
            # 前端只看到一次「失败」toast，刷新后又显示旧的「解析成功」，
            # 用户无法判断「现在用的到底是哪一版文本」。现明确两件事：
            #   1) 返回值新增 ``text_preserved``，如实说明旧结果是否仍然可用；
            #   2) 把提示回填 uploaded_files.message（**不改 status**，旧文本仍是
            #      唯一可用文本），刷新后前端黄色提示位能持续可见。
            preserved = bool(old_text_path and Path(old_text_path).exists())
            keep_msg = (
                f"写入提取文本失败，已保留上一次解析结果（未更新）: {e}"
                if preserved
                else f"写入提取文本失败，且无可用的历史解析结果: {e}"
            )[:500]
            try:
                with get_db() as conn:
                    conn.execute(
                        "UPDATE uploaded_files SET message = ?, "
                        "updated_at = datetime('now', 'localtime') "
                        "WHERE id = ? AND project_id = ?",
                        (keep_msg, file_id, project_id),
                    )
            except Exception as _db_e:
                # 回写提示失败只降级为日志：失败响应本身已能告知调用方
                logger.warning(
                    "重新提取：写盘失败后回写提示失败 file_id=%s: %s", file_id, _db_e
                )
            return {
                "file_id": file_id,
                "filename": filename,
                "status": "failed",
                "message": keep_msg,
                # BUG-FIX 2026-10-16（接口字段不实）：此前写盘失败分支硬编码
                # text_length=0，而 extracted_text 此时**可能已提取到内容**
                # （磁盘满/权限拒绝只发生在落盘环节）。字段含义是「本次提取
                # 到的文本长度」，与落盘成败无关：报 0 会让调用方误以为
                # 「什么都没提取到」，把一次写入故障误导成内容问题。
                # 空文本/提取失败路径的 extracted_text 本就是空串，取值不变。
                "text_length": len(extracted_text),
                "text_preserved": preserved,
            }
    else:
        logger.info("重提取未产出文本，不落盘: %s (status=%s)", filename, status)

    with get_db() as conn:
        conn.execute(
            "UPDATE uploaded_files SET status = ?, message = ?, extracted_text_path = ?, "
            "updated_at = datetime('now', 'localtime') "
            "WHERE id = ? AND project_id = ?",
            (status, message, str(text_path) if text_written else "", file_id, project_id),
        )

    # 清理旧文本文件（DB 已指向新路径后再删，避免中途失败导致新旧皆失）
    if old_text_path:
        try:
            old_p = Path(old_text_path)
            if old_p.exists() and old_p != text_path:
                old_p.unlink()
        except Exception as e:
            logger.warning("清理旧提取文本失败 %s: %s", old_text_path, e)

    # 项目文本已变化，使合并文本缓存失效
    _text_cache_clear(project_id)

    return {
        "file_id": file_id,
        "filename": filename,
        "status": status,
        "message": message,
        "text_length": len(extracted_text),
    }

