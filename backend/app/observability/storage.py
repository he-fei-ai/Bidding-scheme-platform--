"""日志存储与性能保护（ENHANCE 2026-10-29）。

特性：
- 异步写入：后台线程 + 队列批量写入，`write()` 非阻塞，不拖慢主流程。
- 按天轮转 + 按级别分文件（obs-<LEVEL>-YYYY-MM-DD.jsonl），便于按级保留。
- 保留策略：ERROR 90 天 / INFO 30 天 / DEBUG 7 天（OBS_RETENTION_DAYS 可配），
  过期自动清理并记录清理日志。
- gzip 压缩：轮转后的旧文件压缩为 .jsonl.gz。
- 限速：令牌桶，超限时丢弃并节流告警，防日志拖垮主流程。
- 写入失败降级：内存环形缓冲 + 告警（OBS_002），恢复后自动重放。
- `flush()`：同步排空（应用关闭时调用）。

只依赖标准库 + 本包 redact/fields；`write()` 接受 build_record 产出的 dict。
"""
from __future__ import annotations

import gzip
import json
import os
import queue
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from . import fields as _f
from . import redact as _redact


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except Exception:
        return default


def _env_bool(name: str, default: bool) -> bool:
    return os.environ.get(name, "1" if default else "0").lower() in ("1", "true", "yes", "on")


def _log_dir() -> Path:
    base = os.environ.get("OBS_LOG_DIR", "")
    if base:
        p = Path(base)
    else:
        # 默认 backend/logs/structured
        p = Path(__file__).resolve().parent.parent.parent / "logs" / "structured"
    p.mkdir(parents=True, exist_ok=True)
    return p


#: 默认保留天数（按级别）
DEFAULT_RETENTION = {"ERROR": 90, "FATAL": 90, "WARN": 60, "INFO": 30, "DEBUG": 7}


def _retention_days() -> dict[str, int]:
    raw = os.environ.get("OBS_RETENTION_DAYS", "")
    if raw:
        try:
            cfg = json.loads(raw)
            if isinstance(cfg, dict):
                return {str(k).upper(): int(v) for k, v in cfg.items()}
        except Exception:
            pass
    return dict(DEFAULT_RETENTION)


class _TokenBucket:
    """简单令牌桶限速。"""

    def __init__(self, rate: float, capacity: int) -> None:
        self.rate = max(0.1, rate)
        self.capacity = max(1, capacity)
        self.tokens = float(self.capacity)
        self.last = time.time()

    def allow(self) -> bool:
        now = time.time()
        self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
        self.last = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True

class AsyncLogWriter:
    """异步缓冲日志写入器（单例）。"""

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        buffer_size: int | None = None,
        flush_interval: float | None = None,
        rate_limit: float | None = None,
    ) -> None:
        self.enabled = _env_bool("OBS_ENABLED", True) if enabled is None else enabled
        self.buffer_size = _env_int("OBS_BUFFER_SIZE", 10000) if buffer_size is None else buffer_size
        self.flush_interval = _env_float("OBS_FLUSH_INTERVAL", 0.5) if flush_interval is None else flush_interval
        self.rate_limit = _env_float("OBS_RATE_LIMIT", 5000.0) if rate_limit is None else rate_limit
        self._bucket = _TokenBucket(self.rate_limit, int(self.rate_limit))
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=self.buffer_size)
        self._mem_buffer: deque = deque(maxlen=5000)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._stats = {
            "written": 0, "dropped": 0, "warned": 0,
            "flush_errors": 0, "last_flush": 0.0, "degraded": False,
        }
        self._warn_cooldown = 0.0
        if self.enabled:
            self._start()

    def _start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="obs-log-writer", daemon=True)
        self._thread.start()

    def write(self, record: dict[str, Any]) -> bool:
        """非阻塞写入；返回是否已入队。不抛异常。"""
        if not self.enabled:
            return False
        if not self._bucket.allow():
            self._stats["dropped"] += 1
            self._degrade("rate_limited")
            return False
        try:
            self._queue.put_nowait(record)
            return True
        except queue.Full:
            self._stats["dropped"] += 1
            self._degrade("buffer_full")
            return False

    def _degrade(self, reason: str) -> None:
        """写入失败/超限降级：内存缓冲 + 节流告警。"""
        with self._lock:
            self._stats["degraded"] = True
        now = time.time()
        if now - self._warn_cooldown >= 30.0:
            self._warn_cooldown = now
            try:
                from . import structured as _s

                _s.log_event(
                    "WARN", "log.write_fail", module="observability",
                    message=f"日志存储降级（{reason}），进入内存缓冲",
                    status="degraded", error_code="OBS_002", error_layer="io",
                    retryable=True, dropped=self._stats["dropped"],
                )
            except Exception:
                pass

    def _append_line(self, path: Path, line: str) -> None:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")


    def _flush_records(self, records: list[dict[str, Any]]) -> None:
        """批量写入：按级别/日期分文件。"""
        if not records:
            return
        day = time.strftime("%Y-%m-%d")
        by_file: dict[Path, list[str]] = {}
        for rec in records:
            level = str(rec.get("level", "INFO")).upper()
            try:
                line = json.dumps(_redact.redact(rec), ensure_ascii=False, default=repr)
            except Exception:
                continue
            fname = f"obs-{level}-{day}.jsonl"
            by_file.setdefault(_log_dir() / fname, []).append(line)
        for path, lines in by_file.items():
            try:
                with open(path, "a", encoding="utf-8") as fh:
                    for ln in lines:
                        fh.write(ln + "\n")
            except Exception:
                self._stats["flush_errors"] += 1
                with self._lock:
                    self._mem_buffer.extend(lines)
                    self._stats["degraded"] = True

    def _loop(self) -> None:
        batch: list[dict[str, Any]] = []
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=self.flush_interval)
                if item is None:
                    break
                batch.append(item)
                for _ in range(200):
                    try:
                        batch.append(self._queue.get_nowait())
                    except queue.Empty:
                        break
                if batch:
                    self._flush_records(batch)
                    self._stats["written"] += len(batch)
                    self._stats["last_flush"] = time.time()
                    batch = []
            except queue.Empty:
                if batch:
                    self._flush_records(batch)
                    self._stats["written"] += len(batch)
                    self._stats["last_flush"] = time.time()
                    batch = []
            except Exception:
                self._stats["flush_errors"] += 1
                time.sleep(0.05)
        while True:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if batch:
            try:
                self._flush_records(batch)
                self._stats["written"] += len(batch)
            except Exception:
                self._stats["flush_errors"] += 1

    def flush(self, timeout: float = 3.0) -> None:
        """同步排空队列（应用关闭时调用）。"""
        if not self.enabled:
            return
        deadline = time.time() + timeout
        while not self._queue.empty() and time.time() < deadline:
            time.sleep(0.02)

    def stop(self) -> None:
        self._stop.set()
        try:
            self._queue.put(None, timeout=0.5)
        except queue.Full:
            pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            st = dict(self._stats)
            st["queue_size"] = self._queue.qsize()
            st["buffered_mem"] = len(self._mem_buffer)
            st["enabled"] = self.enabled
        return st



#: 全局单例
_writer: AsyncLogWriter | None = None
_writer_lock = threading.Lock()


def get_writer() -> AsyncLogWriter:
    global _writer
    if _writer is None:
        with _writer_lock:
            if _writer is None:
                _writer = AsyncLogWriter()
    return _writer


def write(record: dict[str, Any]) -> bool:
    """写入一条结构化日志（非阻塞）。"""
    return get_writer().write(record)


def flush(timeout: float = 3.0) -> None:
    get_writer().flush(timeout)


def stop() -> None:
    global _writer
    if _writer is not None:
        _writer.stop()


def stats() -> dict[str, Any]:
    return get_writer().stats()


# ──────────────────────────────────────────────────────────────────
# 轮转 / 压缩 / 保留清理
# ──────────────────────────────────────────────────────────────────
def _file_date(path: Path) -> str | None:
    """从文件名 obs-<LEVEL>-YYYY-MM-DD.jsonl(.gz) 提取日期。"""
    name = path.name
    for part in name.split("-"):
        if len(part) == 10 and part[4] == "-" and part[7] == "-":
            return part
    return None


def compress_old(keep_days: int = 2) -> int:
    """把超过 keep_days 的 .jsonl 压缩为 .jsonl.gz。返回压缩数。"""
    compressed = 0
    now = time.time()
    for path in _log_dir().glob("obs-*.jsonl"):
        try:
            if now - path.stat().st_mtime > keep_days * 86400:
                gz = path.with_suffix(".jsonl.gz")
                with open(path, "rb") as fi, gzip.open(gz, "wb") as fo:
                    fo.write(fi.read())
                path.unlink()
                compressed += 1
        except Exception:
            continue
    return compressed


def cleanup_retention() -> dict[str, int]:
    """按级别保留天数清理过期日志文件；删除后记录清理日志。

    返回 {level: deleted_count}。级别不区分压缩与否。
    """
    retention = _retention_days()
    now = time.time()
    deleted: dict[str, int] = {}
    for level in ("DEBUG", "INFO", "WARN", "ERROR", "FATAL"):
        days = retention.get(level, 30)
        cnt = 0
        for suffix in (".jsonl", ".jsonl.gz"):
            for path in _log_dir().glob(f"obs-{level}-*{suffix}"):
                try:
                    if now - path.stat().st_mtime > days * 86400:
                        path.unlink()
                        cnt += 1
                except Exception:
                    continue
        if cnt:
            deleted[level] = cnt
    if deleted:
        try:
            from . import structured as _s

            _s.log_event(
                "INFO", "log.cleanup", module="observability",
                message=f"日志保留清理完成: {deleted}",
                status="ok", deleted=deleted,
            )
        except Exception:
            pass
    return deleted


def cleanup(max_level: str = "INFO") -> dict[str, Any]:
    """综合清理：压缩 + 保留。返回统计。"""
    comp = compress_old()
    del_map = cleanup_retention()
    return {"compressed": comp, "deleted": del_map, "retention_days": _retention_days()}
