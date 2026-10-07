"""DOCX → PDF 转换服务（ENHANCE 2026-10-04 T1：补齐 PDF 交付格式缺口）。

设计原则：
- **DOCX 是唯一事实源**，PDF 为按需转换件：GET /export/pdf/{export_id} 对既有
  .docx 产物调用 LibreOffice headless 转换；已存在同名 .pdf 且不早于源文件时直接复用。
  不改动 build_docx 构建链，零回归风险。
- **无伪造降级**：检测不到 LibreOffice 时抛 PdfUnavailable，路由回 501 中文说明，
  与图表模块「渲染失败绝不伪造」原则一致；不做任何 HTML→PDF 之类的低质量兜底。
- 配置项：可选环境变量 ``PDF_SOFFICE_PATH`` 显式指定 soffice 可执行文件路径。
  **默认行为（不设置时）**：按 PATH → Windows 常见安装路径 → Linux/macOS 常见路径
  顺序自动探测；探测不到仅影响 PDF 端点（501），DOCX 导出不受任何影响（向后兼容）。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import uuid

logger = logging.getLogger(__name__)

#: PDF 转换可用性探测的进程内缓存（soffice 路径不会在运行时消失）
_pdf_probe_lock = threading.Lock()
_cached_soffice: str | None = None
_probe_done = False

#: Windows/Linux/macOS 常见安装位置（which 探测失败后的兜底候选）
_SOFFICE_CANDIDATES: tuple[str, ...] = (
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    "/usr/bin/soffice",
    "/usr/bin/libreoffice",
    "/usr/local/bin/soffice",
    "/opt/libreoffice/program/soffice",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
)


class PdfUnavailable(RuntimeError):
    """PDF 转换能力不可用（未检测到 LibreOffice / PDF_SOFFICE_PATH 配置无效）。"""


def find_soffice(*, force_refresh: bool = False) -> str | None:
    """定位 LibreOffice 可执行文件；找不到返回 None（结果进程内缓存）。

    优先级：环境变量 PDF_SOFFICE_PATH > shutil.which > 常见安装路径。
    """
    global _cached_soffice, _probe_done
    if _probe_done and not force_refresh:
        return _cached_soffice
    with _pdf_probe_lock:
        if _probe_done and not force_refresh:
            return _cached_soffice
        result: str | None = None
        env_path = os.getenv("PDF_SOFFICE_PATH", "").strip()
        if env_path:
            if Path(env_path).exists():
                result = env_path
            else:
                logger.warning("PDF_SOFFICE_PATH 配置的路径不存在: %s（回退自动探测）", env_path)
        if result is None:
            result = shutil.which("soffice") or shutil.which("libreoffice")
        if result is None:
            for cand in _SOFFICE_CANDIDATES:
                if Path(cand).exists():
                    result = cand
                    break
        _cached_soffice = result
        _probe_done = True
        if result:
            logger.info("PDF 转换引擎已就绪: %s", result)
        else:
            logger.info("未检测到 LibreOffice，PDF 导出将以 501 提示（DOCX 导出不受影响）")
        return result


def convert_docx_to_pdf(docx_path: Path, *, timeout_seconds: int = 180) -> Path:
    """把 DOCX 转为 PDF，返回产物路径（与源文件同目录、同名 .pdf）。

    - 幂等复用：同名 .pdf 已存在且 mtime 不早于源 .docx → 直接返回（免重复转换）。
    - 并发安全：每次转换使用独立的 LibreOffice 用户配置目录
      （同一 profile 被多实例占用时 soffice 会静默失败/复用旧实例）。
    - 失败语义：LibreOffice 缺失抛 PdfUnavailable；子进程异常/超时/无产物
      抛 RuntimeError，由路由层转成 500 中文提示。
    """
    soffice = find_soffice()
    if not soffice:
        raise PdfUnavailable(
            "未检测到 LibreOffice（soffice）。请安装 LibreOffice 或设置环境变量 "
            "PDF_SOFFICE_PATH 指向 soffice 可执行文件后重试；DOCX 导出不受影响。"
        )
    docx_path = Path(docx_path)
    if not docx_path.exists():
        raise FileNotFoundError(f"待转换的 DOCX 不存在: {docx_path}")
    target = docx_path.with_suffix(".pdf")
    if target.exists() and target.stat().st_mtime >= docx_path.stat().st_mtime:
        logger.info("PDF 缓存命中（不早于源文件），直接复用: %s", target.name)
        return target

    profile_dir = Path(tempfile.gettempdir()) / f"lo_profile_{uuid.uuid4().hex[:8]}"
    profile_uri = profile_dir.as_uri()
    cmd = [
        soffice,
        f"-env:UserInstallation={profile_uri}",
        "--headless",
        "--norestore",
        "--convert-to",
        "pdf",
        "--outdir",
        str(docx_path.parent),
        str(docx_path),
    ]
    logger.info("开始 PDF 转换: %s -> %s", docx_path.name, target.name)
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=timeout_seconds, check=False)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"PDF 转换超时（>{timeout_seconds}s），请稍后重试") from e
    finally:
        # LibreOffice 退出后 profile 目录无保留价值；失败也清理，避免 temp 堆积
        shutil.rmtree(profile_dir, ignore_errors=True)
    if not target.exists():
        raise RuntimeError(
            "LibreOffice 未生成 PDF（exit=%d）: %s"
            % (proc.returncode, (proc.stderr or proc.stdout or b"").decode("utf-8", "replace")[:200])
        )
    logger.info("PDF 转换完成: %s (%d bytes)", target.name, target.stat().st_size)
    return target
