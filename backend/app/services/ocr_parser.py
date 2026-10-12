"""扫描件 PDF OCR 解析模块（增强 1.2）

使用 MinerU 解析扫描件 PDF，输出 Markdown 文本。
MinerU 是开源 VLM+OCR 双引擎 PDF 解析工具，支持 109 种语言。

集成方式：
  1. 安装 MinerU: pip install mineru
  2. 或使用独立 Docker 服务: docker run -d --name mineru mineru:latest

使用方式：
  from services.ocr_parser import MinerUParser
  parser = MinerUParser()
  text = parser.parse_pdf("path/to/scanned.pdf")

增强功能（v1.2）：
  - 文件缓存：基于文件路径+修改时间的内存缓存，避免重复解析
  - 并发控制：使用信号量限制同时解析的 PDF 数量
  - 临时文件清理：解析完成后自动清理临时输出目录
  - 异步支持：提供 async 版本的解析方法
"""
import asyncio
import threading
from collections import OrderedDict
import hashlib
import logging
from pathlib import Path
import subprocess
import tempfile
import time
try:
    from ..config import OCR_MAX_CONCURRENT, OCR_MINERU_PATH, OCR_TIMEOUT
except ImportError:
    from config import OCR_MAX_CONCURRENT, OCR_MINERU_PATH, OCR_TIMEOUT
logger = logging.getLogger(__name__)

class MinerUParser:
    """MinerU PDF 解析器（增强版）

    支持扫描件 PDF 和普通 PDF 的 OCR 解析，输出结构化 Markdown 文本。

    增强功能：
        - LRU 缓存：基于文件路径和修改时间的缓存，避免重复解析
        - 并发控制：通过 asyncio.Semaphore 限制并发数
        - 自动清理：解析完成后清理临时文件
        - 统计指标：记录解析次数、缓存命中数等
    """

    def __init__(self, mineru_path: str | None=None, timeout: int | None=None, max_concurrent: int | None=None, cache_max_size: int=50, cache_ttl: int=3600, auto_cleanup: bool=True):
        """初始化 MinerU 解析器

        Args:
            mineru_path: MinerU CLI 可执行文件路径，缺省从配置 OCR_MINERU_PATH 读取
            timeout: 解析超时时间（秒），缺省从配置 OCR_TIMEOUT 读取
            max_concurrent: 最大并发解析数，缺省从配置 OCR_MAX_CONCURRENT 读取
            cache_max_size: 缓存最大条目数，默认 50
            cache_ttl: 缓存有效期（秒），默认 3600 秒（1 小时）
            auto_cleanup: 是否自动清理临时文件，默认 True
        """
        self.mineru_path = mineru_path or OCR_MINERU_PATH
        self.timeout = timeout or OCR_TIMEOUT
        self.max_concurrent = max_concurrent or OCR_MAX_CONCURRENT
        self.cache_max_size = cache_max_size
        self.cache_ttl = cache_ttl
        self.auto_cleanup = auto_cleanup
        # 异步路径并发控制：配合 async_parse_pdf 使用
        self._semaphore = asyncio.Semaphore(self.max_concurrent)
        # 同步路径并发控制：实际解析在同步 subprocess 中执行（多在 worker 线程），
        # 必须用线程信号量，否则 asyncio.Semaphore 在同步方法里永远不会被 acquire，
        # 等于没有并发限制（多文件并行上传时会同时拉起大量 MinerU 进程）。
        self._thread_semaphore = threading.Semaphore(self.max_concurrent)
        # 缓存并发读写保护（同步解析现在真正多线程并发）
        self._cache_lock = threading.Lock()
        self._cache: dict[str, dict] = {}
        self._cache_order: OrderedDict[str, None] = OrderedDict()
        self._stats = {'total_parse': 0, 'cache_hits': 0, 'cache_misses': 0, 'errors': 0}

    @property
    def stats(self) -> dict:
        """获取统计指标"""
        return self._stats.copy()

    def _get_cache_key(self, pdf_path: str) -> str:
        """生成缓存 key（基于文件路径 + 修改时间 + 前 4KB 内容）。

        BUG-FIX 2026-10-11（P3·OCR 缓存键亚秒碰撞）：原实现用
        ``f'{pdf_path}:{mtime:.0f}'`` 把 mtime **截断到整秒**后求 MD5，存在与
        ``pdf_export`` 缓存复用同源的亚秒缺陷——同一 PDF 在同一秒内被替换/重解析
        时，新旧内容算出同一个 key，MinerU 缓存会把旧解析文本错配给新文件，用户
        换正文后拿到的仍是旧摘要。现对齐 ``files._get_file_hash``（CF-03）的加固
        口径，三步消除碰撞：
          1. mtime 用 ``st_mtime_ns``（纳秒，亚秒精度），不再截断到秒；
          2. 叠加文件大小，覆盖「纳秒刻度恰好相同的粗粒度文件系统」；
          3. 无条件叠加前 4KB 内容哈希，从根上消除任何粒度碰撞（同 key 后果仅
             缓存一次性失效→重解析一次，该哈希不落库、无兼容性影响）。
        TOCTOU race 保留：stat() 期间文件被删则回退返回原路径（与旧行为一致）。
        """
        path = Path(pdf_path)
        try:
            stat = path.stat()
        except (OSError, FileNotFoundError):
            # BUG-FIX 2026-10-08：TOCTOU race — exists() 检查与 stat() 之间
            # 文件可能被删除，导致 FileNotFoundError 未捕获
            return pdf_path
        digest = hashlib.md5(
            f'{pdf_path}:{stat.st_size}:{stat.st_mtime_ns}'.encode()
        ).hexdigest()
        try:
            with open(pdf_path, 'rb') as fh:
                head = fh.read(4096)
        except OSError:
            # stat() 成功但 open() 失败（竞态删除/权限）：退回纯 mtime+size 键，
            # 仍远优于原整秒截断键，且不抛异常打断解析链。
            return digest
        return hashlib.md5(digest.encode() + head).hexdigest()

    def _get_from_cache(self, cache_key: str) -> dict | None:
        """从缓存获取结果"""
        with self._cache_lock:
            entry = self._cache.get(cache_key)
            if entry is None:
                return None
            if time.time() - entry['timestamp'] > self.cache_ttl:
                self._cache.pop(cache_key, None)
                self._cache_order.pop(cache_key, None)
                return None
            self._cache_order.move_to_end(cache_key)
            self._stats['cache_hits'] += 1
            return entry

    def _set_to_cache(self, cache_key: str, text: str, char_count: int):
        """存入缓存"""
        with self._cache_lock:
            while len(self._cache) >= self.cache_max_size and self._cache_order:
                oldest_key, _ = self._cache_order.popitem(last=False)
                self._cache.pop(oldest_key, None)
            self._cache[cache_key] = {'text': text, 'timestamp': time.time(), 'char_count': char_count}
            self._cache_order[cache_key] = None

    def _clear_old_output(self, output_dir: str):
        """清理临时输出目录

        安全护栏：只允许删除**系统临时目录**下、以 `mineru_` 为前缀的目录。
        该方法会递归删除传入目录下的一切，若调用方误传业务目录（如 data/projects）
        就是数据灾难，必须在入口处挡死。
        """
        if not self.auto_cleanup:
            return
        try:
            output_path = Path(output_dir)
            resolved = output_path.resolve()
            tmp_root = Path(tempfile.gettempdir()).resolve()
            if not resolved.is_relative_to(tmp_root) or not resolved.name.startswith(
                "mineru_"
            ):
                logger.warning(
                    '[MinerU] 拒绝清理非 MinerU 临时目录（护栏拦截）: %s', output_dir
                )
                return
            if output_path.exists():
                for item in output_path.iterdir():
                    if item.is_file():
                        item.unlink()
                    elif item.is_dir():
                        import shutil
                        shutil.rmtree(item)
                output_path.rmdir()
                logger.debug('[MinerU] 临时文件已清理: %s', output_dir)
        except Exception as e:
            logger.warning('[MinerU] 清理临时文件失败: %s', e)

    def is_available(self) -> bool:
        """检查 MinerU 是否可用"""
        try:
            result = subprocess.run([self.mineru_path, '--version'], capture_output=True, text=True, timeout=10)
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired, PermissionError):
            return False

    def parse_pdf(self, pdf_path: str, output_dir: str | None=None, skip_cache: bool=False) -> str:
        """同步解析 PDF，返回 Markdown 文本

        Args:
            pdf_path: PDF 文件路径
            output_dir: 输出目录（可选）
            skip_cache: 是否跳过缓存强制重新解析

        Returns:
            解析后的 Markdown 文本
        """
        self._stats['total_parse'] += 1
        cache_key = self._get_cache_key(pdf_path)
        if not skip_cache:
            cached = self._get_from_cache(cache_key)
            if cached is not None:
                logger.debug('[MinerU] 缓存命中: %s', pdf_path)
                return cached['text']
        self._stats['cache_misses'] += 1
        pdf_file = Path(pdf_path)
        if not pdf_file.exists():
            raise FileNotFoundError(f'PDF 文件不存在: {pdf_path}')
        _auto_output_dir = False
        if not output_dir:
            output_dir = tempfile.mkdtemp(prefix=f'mineru_{pdf_file.stem}_')
            _auto_output_dir = True
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)
        logger.info('[MinerU] 开始解析 PDF: %s', pdf_path)
        _t0 = time.monotonic()
        # 并发护栏：限制同时运行的 MinerU 子进程数（同步路径用线程信号量；
        # 原实现只建了 asyncio.Semaphore 却从未 acquire，等于没有并发控制）。
        # 取超时 + 宽限，避免极端情况下 acquire 阻塞导致调用方永久挂起。
        acquired = self._thread_semaphore.acquire(timeout=self.timeout + 60)
        try:
            try:
                text = self._run_mineru(pdf_file, output_path)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
                self._stats['errors'] += 1
                if isinstance(e, subprocess.TimeoutExpired):
                    raise RuntimeError(
                        f'MinerU 解析超时（{self.timeout}秒）: {pdf_path}'
                    ) from e
                error_msg = getattr(e, 'stderr', '') or getattr(e, 'stdout', '') or '未知错误'
                logger.error('[MinerU] 解析失败: %s', error_msg[:200])
                raise RuntimeError(f'MinerU 解析失败: {error_msg[:200]}') from e
            if not text:
                self._stats['errors'] += 1
                raise RuntimeError(f'MinerU 解析完成但未找到输出 Markdown 文件: {output_dir}')
            elapsed = time.monotonic() - _t0
            logger.info('[MinerU] 解析完成: %s -> %d 字符, 耗时 %.1fs', pdf_file.name, len(text), elapsed)
            self._set_to_cache(cache_key, text, len(text))
            return text
        finally:
            if acquired:
                self._thread_semaphore.release()
            # BUG-FIX（P2 临时盘泄漏）：原实现只在成功路径末尾清理，CalledProcessError /
            # TimeoutExpired / 未找到 md 的 RuntimeError / read_text 的 UnicodeDecodeError
            # 全部泄漏临时目录。MinerU 输出含**逐页图片**，一份 200 页扫描件泄漏几百 MB，
            # 反复重试会占满系统临时盘。
            if _auto_output_dir:
                try:
                    self._clear_old_output(output_dir)
                except Exception as e:
                    logger.warning('[MinerU] 清理临时目录失败: %s -> %s', output_dir, e)

    def _run_mineru(self, pdf_file: Path, output_path: Path) -> str:
        """实际执行 MinerU 子进程并读取输出 Markdown（不含缓存与并发控制）

        MinerU 输出布局有两种：
          - 扁平：output_dir/<stem>.md
          - 嵌套（新版常见）：output_dir/<stem>/<stem>.md
        仅查一层会漏掉嵌套结果，这里做有限深度递归查找，优先匹配 <stem>.md。
        """
        result = subprocess.run(
            [self.mineru_path, '-p', str(pdf_file), '-o', str(output_path), '--method', 'ocr'],
            check=True, capture_output=True, text=True, timeout=self.timeout,
        )
        logger.debug('[MinerU] stdout: %s', result.stdout[:500])
        md_file = output_path / f'{pdf_file.stem}.md'
        if md_file.exists():
            # errors="replace"：扫描件转出的 md 可能含非法字节，直接抛
            # UnicodeDecodeError 会让已解析成功的文本整个丢掉
            return md_file.read_text(encoding='utf-8', errors='replace')
        for cand in self._find_output_markdown(output_path, pdf_file.stem):
            text = cand.read_text(encoding='utf-8', errors='replace')
            if text:
                logger.info('[MinerU] 解析完成（备用文件）: %s -> %d 字符', cand.name, len(text))
                return text
        return ''

    def _find_output_markdown(self, output_path: Path, stem: str) -> list:
        """查找 MinerU 产出的 Markdown 文件（最多向下 3 层），优先返回 <stem>.md"""
        exact, others = [], []
        for p in output_path.rglob('*.md'):
            if p.is_file():
                if p.stem == stem:
                    exact.append(p)
                else:
                    others.append(p)
        return exact + others

    async def async_parse_pdf(self, pdf_path: str, output_dir: str | None=None, skip_cache: bool=False) -> str:
        """异步解析 PDF：在后台线程执行同步解析，避免阻塞事件循环。

        并发控制由同步路径的线程信号量统一负责（asyncio 事件循环线程本身不执行
        CPU/IO 密集的 subprocess 阻塞），因此这里直接委托 parse_pdf 即可。
        """
        return await asyncio.to_thread(self.parse_pdf, pdf_path, output_dir, skip_cache)
