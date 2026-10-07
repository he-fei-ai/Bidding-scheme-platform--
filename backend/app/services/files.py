import asyncio
import hashlib
import logging
import os
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

# BUG-FIX-71: MinerU 单例初始化锁，防止多线程并发创建多个实例
_mineru_lock = threading.Lock()

# BUG-FIX 2026-10-04（P2 并发竞态）：save_upload 经 asyncio.to_thread 走线程池，
# 同一项目并发上传同名文件时，"存在性检查→重命名→write_bytes" 非原子：两个线程
# 可能同时通过 exists() 判定、选中同一目标路径与同一文本文件名（f"{filename}.txt"），
# 后写者覆盖前者的原始文件与提取文本，DB 却生成两行 → 解析结果丢失/字段错位。
# 现按 project_id 维护写入互斥锁，仅锁住"命名+落盘"临界区（不含耗时的文本提取），
# 既消除竞态又不 serialize 解析本身。不同项目互不阻塞。
_upload_write_locks: dict[int, threading.Lock] = {}
_upload_write_locks_guard = threading.Lock()


def _get_upload_write_lock(project_id: int) -> threading.Lock:
    """获取（惰性创建）指定项目的上传写入互斥锁"""
    with _upload_write_locks_guard:
        lock = _upload_write_locks.get(project_id)
        if lock is None:
            lock = threading.Lock()
            _upload_write_locks[project_id] = lock
        return lock


def _get_file_hash(file_path: str) -> str:
    """计算文件的快速哈希（基于文件大小和修改时间）"""
    try:
        stat = os.stat(file_path)
        # 使用文件大小和修改时间作为快速标识
        raw = f"{stat.st_size}_{stat.st_mtime_ns}"
        return hashlib.md5(raw.encode()).hexdigest()
    except (OSError, FileNotFoundError):
        return ""


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
        logger.warning("创建文件解析任务失败（不影响核心功能）: %s", e)
        return

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
                conn.execute(
                    f"""UPDATE file_parse_tasks
                       SET status = ?, progress = ?, completed_files = 1,
                           completed_at = datetime('now', 'localtime'),
                           updated_at = datetime('now', 'localtime')
                       WHERE {where}""",
                    (status, progress, *tail),
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
EXTRACT_DOC_NOTICE = "[提示] 当前系统无法自动提取此 .doc 文件文本，建议转换为 .docx 后重新上传"
EXTRACT_PPT_NOTICE = "[提示] 当前系统无法自动提取此 .ppt 文件文本，建议转换为 .pptx 后重新上传"
EXTRACT_WPS_NOTICE = "[提示] 当前系统无法自动提取此 .wps 文件文本，建议使用WPS打开后另存为 .docx 格式再上传。"

EXTRACTION_NOTICES: frozenset[str] = frozenset(
    {EXTRACT_DOC_NOTICE, EXTRACT_PPT_NOTICE, EXTRACT_WPS_NOTICE}
)


def is_extraction_notice(text: str | None) -> bool:
    """判断提取结果是否为「无法提取」的降级提示串（而非真实正文）。

    精确匹配登记表中的三条已知提示串。供 save_upload、Celery 解析任务与
    latest_extracted_text 共用，保证「上传 / 异步重解析 / 合并文本」三条
    路径口径一致。
    """
    if not text:
        return False
    return text.strip() in EXTRACTION_NOTICES


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
        _create_parse_task(project_id, filename, ext)

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
            _update_parse_task(project_id, "completed", 100, filename=filename)
    except Exception as e:
        extracted_text = ""
        status = "failed"
        message = f"文本提取失败: {e}"
        logger.error("文件 %s 文本提取失败: %s", filename, e)
        _update_parse_task(project_id, "failed", 50, message, filename)

    # 保存提取文本（文件名含完整原名+扩展名，避免同名不同扩展名文件互相覆盖）
    text_filename = f"{Path(filename).name}.txt"
    text_path = texts_dir / text_filename
    try:
        text_path.write_text(extracted_text, encoding="utf-8")
    except Exception as e:
        logger.error("保存提取文本失败: %s", e)

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
                downgraded = conn.execute(
                    "SELECT id FROM uploaded_files "
                    "WHERE project_id = ? AND file_role = 'main'",
                    (project_id,),
                ).fetchall()
                demoted = [r["id"] for r in downgraded]
                if demoted:
                    conn.execute(
                        "UPDATE uploaded_files SET file_role = 'supporting' "
                        "WHERE project_id = ? AND file_role = 'main'",
                        (project_id,),
                    )
                    # REVIEW-FIX m2：main 换源且已有缓存分析产物 → 旧摘要/评分基于
                    # 旧主文件，透出 needs_reanalysis 供前端提示重新「提取项目」。
                    # 非破坏式：不静默清空用户已确认（confirmed_*）的数据。
                    needs_reanalysis = _analysis_has_cached(conn, project_id)
            cursor = conn.execute(
                """INSERT INTO uploaded_files
                   (project_id, filename, path, file_type, file_role,
                    status, extracted_text_path, message)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    project_id,
                    filename,
                    str(file_path),
                    file_type,
                    final_role,
                    status,
                    str(text_path),
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
    return ""


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
    """清除文本中的 BOM 残留与 NUL 控制字符（读取边界统一清洗）"""
    if not text:
        return text
    if text[0] == "\ufeff":
        text = text[1:]
    if "\x00" in text:
        text = text.replace("\x00", "")
    return text


def _read_text_auto(file_path: str) -> str:
    """读取文本文件并自动处理编码

    优先级：BOM 嗅探（utf-8-sig / utf-16 / utf-32） → utf-8 → gb18030 → 兜底忽略坏字节

    修复 BUG：此前 .txt/.md 硬编码 utf-8，GBK/GB2312 编码的中文文件会抛
    UnicodeDecodeError 导致整个上传解析失败。
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
    # gb18030 是 GBK/GB2312 的超集，覆盖常见中文编码
    try:
        return _strip_invisible(path.read_text(encoding="gb18030"))
    except UnicodeDecodeError:
        pass
    # 最终兜底：忽略无法解码的字节，保证不中断上传流程
    return _strip_invisible(path.read_text(encoding="utf-8", errors="ignore"))


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

    # 检查缓存（仅缓存支持的格式）
    # BUG-FIX-74/74c: 补全缓存格式列表并统一引用模块级常量
    if ext in _PARSE_CACHEABLE_EXTS:
        with _parse_cache_lock:
            cached = _parse_result_cache.get(file_path)
        if cached and cached[0] == file_hash:
            cache_time = cached[1]
            if time.time() - cache_time < _PARSE_CACHE_TTL:
                logger.info("[文件解析] 命中缓存: %s", Path(file_path).name)
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

    # 存储缓存（仅缓存支持的格式）
    if ext in _PARSE_CACHEABLE_EXTS:
        with _parse_cache_lock:
            if len(_parse_result_cache) >= _PARSE_CACHE_MAX:
                # 淘汰最旧的缓存
                sorted_items = sorted(_parse_result_cache.items(), key=lambda x: x[1][1])
                for key, _ in sorted_items[: len(sorted_items) // 2]:
                    _parse_result_cache.pop(key, None)
            _parse_result_cache[file_path] = (file_hash, time.time(), result)

    logger.info("[文件解析] %s 解析完成，提取 %d 字符, 耗时 %.2fs", ext, len(result), elapsed)
    return result


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
    if ext in _LEGACY_OLE_EXTS and _is_ole2_file(file_path) and ext in (
        declared_ext,
        _sniff_extension(file_path),
    ):
        return _extract_legacy_ole_chain(file_path, declared_ext)
    if ext == ".docx":
        return _extract_docx(file_path)
    elif ext == ".pdf":
        return _extract_pdf(file_path)
    elif ext == ".doc":
        return _extract_doc(file_path)
    elif ext == ".xlsx":
        return _extract_xlsx_openpyxl(file_path)
    elif ext == ".xls":
        return _extract_xls(file_path)
    elif ext == ".txt":
        return _read_text_auto(file_path)
    elif ext == ".pptx":
        return _extract_pptx(file_path)
    elif ext == ".ppt":
        return _extract_ppt(file_path)
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
        return _extract_wps(file_path)
    else:
        logger.warning("[文件解析] 不支持的文件格式: %s", ext)
        raise UnsupportedFileFormatError(
            f"不支持的文件格式: {ext}（支持的格式：{', '.join(sorted(SUPPORTED_EXTENSIONS))}）"
        )


def _extract_docx(file_path: str) -> str:
    """提取 Word 文档文本（python-docx 主引擎 + docx2python 回退，详见 docx_extractor 模块）"""
    return _extract_docx_docx(file_path)


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
        一次 PowerPoint COM（可能拉起 Office 进程、最长 PPT_COM_TIMEOUT 秒）；
      - 其余分支按 xls → ppt → wps 兜底。
    """
    _order = [".doc", ".xls", ".ppt", ".wps"]
    if declared_ext in _LEGACY_OLE_EXTS and declared_ext != ".doc":
        _order.remove(declared_ext)
        _order.insert(1, declared_ext)
    _handlers = {
        ".doc": _extract_doc,
        ".xls": _extract_xls,
        ".ppt": _extract_ppt,
        ".wps": _extract_wps,
    }
    last_notice = ""
    for label in _order:
        handler = _handlers[label]
        try:
            text = handler(file_path)
        except Exception as e:
            logger.debug("[OLE降级链] %s 分支失败（尝试下一分支）: %s", label, e)
            continue
        if text and not is_extraction_notice(text):
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
                    rows_txt = [_format_pdf_table_row(row) for row in table]
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
    """提取 PDF 文本（增强 1.1: 支持扫描件 OCR 自动降级）

    使用 pypdf 提取文本，若文本不足 100 字符则自动尝试 MinerU OCR 解析。

    ENHANCE 2026-10-05（P-2）：优先走 pdfplumber 表格感知提取——
    对齐易标规格一.1「PDF 文件提取：按页提取、添加页码标识、表格标记
    [表格 n]/[表格结束]」。旧实现只有 pypdf 纯文本流，**表格结构完全丢失**
    （pypdf 不区分表格与正文），下游 AI 分析无法还原表格数据。
    pdfplumber 不可用或整页提取失败时回退 pypdf（原有行为，向后兼容）。
    """
    # 优先：pdfplumber（表格感知 + 页码标识 + 表格标记）
    if PDF_TABLE_EXTRACT_ENABLED:
        try:
            plumber_text = _extract_pdf_pdfplumber(file_path)
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
    if pypdf_len < 100 and OCR_ENABLED:
        try:
            from .ocr_parser import MinerUParser

            # 模块级单例，避免每次创建新实例导致缓存失效
            # BUG-FIX-71: 加锁防止多线程并发创建多个 MinerU 实例
            with _mineru_lock:
                if not hasattr(_extract_pdf, "_mineru_parser"):
                    _extract_pdf._mineru_parser = MinerUParser()
            parser = _extract_pdf._mineru_parser
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
                    try:
                        raw = data.decode("utf-16-le", errors="ignore")
                        # 过滤掉控制字符，只保留可读文本
                        clean = "".join(c for c in raw if c.isprintable() or c in "\n\r\t")
                        if clean.strip():
                            text_parts.append(clean)
                    except Exception as _e:
                        logger.debug("[silent-except] files.py: line 452 - %s", _e)
                # 表格、文本流等
                elif "text" in name or "table" in name:
                    data = ole.openstream(stream_name).read()
                    try:
                        raw = data.decode("utf-16-le", errors="ignore")
                        clean = "".join(c for c in raw if c.isprintable() or c in "\n\r\t")
                        if clean.strip():
                            text_parts.append(clean)
                    except Exception as _e:
                        logger.debug("[silent-except] files.py: line 462 - %s", _e)
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
            if not _is_under_projects_dir(file_path):
                return ""
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
            # BUG-FIX 2026-09-18（中）：原为字符串前缀比较，可被
            # `projects_evil` 或 `projects/../..` 绕过，改为真实父子关系判定。
            if not _is_under_projects_dir(file_path):
                return ""
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
    return _extract_xlsx_raw_xml(file_path)


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
    except Exception as e:
        rows.append(f"[Excel 解析错误] {e}")

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
                    val = _xls_cell_value(cell)
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
        logger.error("PPTX提取失败: %s", e, exc_info=True)
        raise


# 遗留修复 2026-10-06（L1）：.ppt COM 自动化的降级提示（多处共用，文案保持一致）
_PPT_FALLBACK_HINT = (
    EXTRACT_PPT_NOTICE
)


def _ppt_com_extract_sync(file_path: str) -> str:
    """同步执行 PowerPoint COM 提取（**可能长时间不返回**，只应由
    :func:`_ppt_via_powerpoint_com` 在带超时的线程里调用）

    拆成独立函数的理由：COM 调用是唯一可能挂死的部分，把它与「超时包装」解耦后，
    超时逻辑才可以被单测直接覆盖（见 tests/test_import_enhancements_2026_10_05.py
    的 TestPptComGuardrails）。
    """
    import os

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


def _ppt_via_powerpoint_com(file_path: str) -> str:
    """把 PowerPoint COM 提取放进可超时的后台线程

    为什么必须放线程：COM 调用（Dispatch / Presentations.Open）在损坏文件、
    超大文件或 PowerPoint 弹出模态框（修复模式、密码框）时可能永不返回，
    而 `Thread.join(timeout)` 是唯一能为它加上界的手段（Python 线程无法被强杀）。
    超时的线程被标为 daemon 留在后台，进程退出即回收，不影响上传主流程。
    """
    box: list = []

    def _worker() -> None:
        box.append(_ppt_com_extract_sync(file_path))

    worker = threading.Thread(target=_worker, name="ppt-com-extract", daemon=True)
    worker.start()
    worker.join(timeout=PPT_COM_TIMEOUT)

    if worker.is_alive():
        logger.error(
            "PowerPoint COM 转换超时（>%ds），放弃等待并返回降级提示: %s",
            PPT_COM_TIMEOUT, Path(file_path).name,
        )
        return ""
    return (box[0] or "") if box else ""


def _extract_ppt(file_path: str) -> str:
    """提取旧版 .ppt 文件文本（PowerPoint COM，带开关与超时护栏）

    遗留修复 2026-10-06（L1）：
      - ``PPT_COM_ENABLED=false`` → 直接降级，不再拉起 PowerPoint；
      - ``PPT_COM_TIMEOUT``       → COM 在 daemon 线程执行并限时等待，超时返回降级
        提示，绝不把上传请求挂死。
    向后兼容：默认 ``PPT_COM_ENABLED=true`` + 120s 超时，非 Windows 或没装
    pywin32/Office 时行为与修复前完全一致（返回同一句降级提示）。
    """
    import sys

    if sys.platform == "win32" and PPT_COM_ENABLED:
        text = _ppt_via_powerpoint_com(file_path)
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
    """图片是否超过 OCR 单文件体积上限（OCR_MAX_FILE_SIZE_MB，默认 50MB）"""
    try:
        size_mb = Path(file_path).stat().st_size / (1024 * 1024)
    except OSError:
        return False
    if size_mb > OCR_MAX_FILE_SIZE_MB:
        logger.warning(
            "[图片OCR] 文件 %.1fMB 超过 OCR 上限 %dMB，跳过 OCR: %s",
            size_mb, OCR_MAX_FILE_SIZE_MB, Path(file_path).name,
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
    """用 MinerU 解析图片（OCR_ENGINE=mineru 时的首选引擎），不可用/无输出返回空串

    MinerU 侧自带并发信号量（OCR_MAX_CONCURRENT）与结果缓存（见 ocr_parser），
    因此这里不再额外限流；任何异常都吞掉并返回空串，由调用方继续回退下一引擎。
    """
    try:
        from .ocr_parser import MinerUParser

        with _mineru_lock:
            if not hasattr(_extract_pdf, "_mineru_parser"):
                _extract_pdf._mineru_parser = MinerUParser()
        parser = _extract_pdf._mineru_parser
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
        ocr = _get_paddleocr()
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

    引擎顺序由 OCR_ENGINE 决定（mineru 默认 / paddleocr / none）：
      ``mineru``   → MinerU → PaddleOCR → tesseract
      ``paddleocr``→ PaddleOCR → tesseract
      ``none``     → 直接跳过（空文本）

    所有引擎都不可用时优雅降级为空文本（与修复前一致），上传仍为 success。
    """
    _t0 = time.monotonic()
    name = Path(file_path).name
    engine = (OCR_ENGINE or "mineru").strip().lower()
    logger.info("[图片OCR] 开始: %s (engine=%s)", name, engine)

    if engine == "none":
        logger.info("[图片OCR] OCR_ENGINE=none，按配置跳过: %s", name)
        return ""
    if _ocr_image_size_exceeded(file_path):
        return ""

    text = ""
    try:
        if engine == "mineru":
            text = _ocr_with_mineru(file_path)
            if not text:
                logger.info("[图片OCR] MinerU 无输出，继续回退: %s", name)

        if not text:
            # 并发护栏：PaddleOCR / tesseract 单次推理内存占用高，限制同时在跑的
            # OCR 数量（OCR_MAX_CONCURRENT）。取不到信号量时记录告警后放行，
            # 避免极端情况下把上传请求永久挂住。
            acquired = _ocr_semaphore.acquire(timeout=OCR_TIMEOUT + 60)
            try:
                text = _ocr_with_paddle(file_path)
                if not text:
                    text = _ocr_with_tesseract(file_path)
            finally:
                if acquired:
                    _ocr_semaphore.release()
    finally:
        logger.info("[图片OCR] 完成: %s 耗时 %.2fs，提取 %d 字符", name, time.monotonic() - _t0, len(text))

    if not text:
        # 所有OCR库都不可用时，优雅降级：记录警告并返回空文本，
        # 避免因为没有 OCR 引擎而中断整个文件上传流程。
        logger.warning(
            "图片文字识别不可用（OCR_ENGINE=%s，未安装可用引擎或引擎无输出）。"
            "跳过 OCR，文件将以空文本入库。建议安装 OCR 引擎或将图片文字复制到文本文件后重新上传。",
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
    html_content = _read_text_auto(file_path)

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
                        raw = data.decode("utf-16-le", errors="ignore")
                        clean = "".join(c for c in raw if c.isprintable() or c in "\n\r\t")
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
    """
    main_files = [f for f in files if f.get("file_role") == "main"]
    ok_main = [f for f in main_files if f.get("status") == "success"]
    if ok_main:
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


def _text_cache_set(project_id: int, text: str) -> None:
    with _text_cache_lock:
        # 达到上限时淘汰最旧的一半
        if len(_text_cache) >= _TEXT_CACHE_MAX:
            sorted_items = sorted(_text_cache.items(), key=lambda x: x[1][1])
            for key, _ in sorted_items[: len(sorted_items) // 2]:
                _text_cache.pop(key, None)
        _text_cache[project_id] = (text, time.time())


def _text_cache_clear(project_id: int) -> None:
    """使项目文本缓存失效（上传/删除/重提取/清空后调用，防止 AI 使用旧文本）"""
    with _text_cache_lock:
        _text_cache.pop(project_id, None)


async def latest_extracted_text(project_id: int) -> str:
    """合并主文件和辅助文件，主文件优先（带项目级缓存）"""
    cached = _text_cache_get(project_id)
    if cached is not None:
        return cached

    # BUG-FIX 2026-09-16（高 事件循环阻塞）：原实现在 `with get_db()` **内部**执行
    # 多 MB 正文文件的读取与辅助文件截断。数据库连接看上去是在 asyncio 线程里拿的，
    # 但这把是**全局写锁**：一个「读取项目文本」请求（GET /files/text）会把全进程
    # 的写操作挡在锁外，而它本身主要时间在磁盘 IO 上。
    # 现在：DB 查询只用只读连接速取路径（不抢写锁），IO 全部在 `with` 之外做。
    with get_db(readonly=True) as conn:
        rows = conn.execute(
            "SELECT id, filename, file_type, file_role, status, extracted_text_path "
            "FROM uploaded_files WHERE project_id = ? ORDER BY id",
            (project_id,),
        ).fetchall()

    if not rows:
        return ""

    files = [dict(r) for r in rows]
    main = _pick_main_file(files)
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
    _text_cache_set(project_id, result)
    return result


def get_project_files(project_id: int) -> list[dict]:
    """获取项目的文件列表"""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT id, filename, file_type, file_role, status, message, created_at "
            "FROM uploaded_files WHERE project_id = ? ORDER BY id",
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_file_text(project_id: int, file_id: int) -> str:
    """获取指定文件的提取文本"""
    with get_db() as conn:
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
    with get_db() as conn:
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
                    "UPDATE uploaded_files SET file_role = 'supporting' "
                    "WHERE project_id = ? AND file_role = 'main' AND id != ?",
                    (project_id, file_id),
                )
        conn.execute(
            "UPDATE uploaded_files SET file_role = ? WHERE id = ? AND project_id = ?",
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
        "UPDATE uploaded_files SET file_role = 'main' WHERE id = ? AND project_id = ?",
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

    return {
        "status": "deleted",
        "file_id": file_id,
        # ENHANCE 2026-10-05（B5）：删除主文件后自动补位的新主文件 id（无剩余文件时为 null）
        "promoted_file_id": promoted_file_id,
    }


def clear_project_texts(project_id: int):
    """清理项目的提取文本（用于重新上传后重置）

    BUG-FIX 2026-10-05（B6 数据链断裂）：原实现只删 ``texts/`` 目录与合并文本缓存，
    完全不动 ``uploaded_files`` —— 记录仍显示 ``status='success'`` 且
    ``extracted_text_path`` 指向已被删除的文件，``GET /files/{id}/text`` 静默返回
    空串、合并文本也随之变空，界面上却看不出任何异常（仍显示"已提取"）。
    现在把 DB 侧一并重置为 ``status='pending'`` + 空路径 + 空 message，
    与实际文件状态保持一致（pending 在前端状态映射中已有"待提取"文案）。
    """
    texts_dir = PROJECTS_DATA_DIR / str(project_id) / "texts"
    if texts_dir.exists():
        shutil.rmtree(texts_dir)
        texts_dir.mkdir(exist_ok=True)

    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE uploaded_files SET status = 'pending', message = '', "
                "extracted_text_path = '' WHERE project_id = ?",
                (project_id,),
            )
    except Exception as e:
        logger.warning("清理项目文本后重置文件状态失败 project=%d: %s", project_id, e)

    # 文本已清空，使合并文本缓存失效
    _text_cache_clear(project_id)


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
    _create_parse_task(project_id, filename, Path(filename).suffix.lower())

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
            _update_parse_task(project_id, "completed", 100, filename=filename)
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
    try:
        text_path.write_text(extracted_text, encoding="utf-8")
    except Exception as e:
        logger.error("重新提取：写入提取文本失败 file_id=%s: %s", file_id, e)
        _update_parse_task(project_id, "failed", 0, f"写入提取文本失败: {e}", filename)
        return {
            "file_id": file_id,
            "filename": filename,
            "status": "failed",
            "message": f"写入提取文本失败: {e}",
            "text_length": 0,
        }

    with get_db() as conn:
        conn.execute(
            "UPDATE uploaded_files SET status = ?, message = ?, extracted_text_path = ? WHERE id = ?",
            (status, message, str(text_path), file_id),
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

