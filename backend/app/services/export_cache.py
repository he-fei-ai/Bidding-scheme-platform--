"""导出缓存与 single-flight 并发合并层（2026-10-12 从 export.py 拆出）

职责：
  1. 图表渲染 L1 内存缓存（`_chart_render_cache`）—— 避免重复 Mermaid 渲染
  2. Single-flight 并发合并（`_single_flight_render`）—— 缓存击穿修复
  3. 内联图表标记缓存（`_inline_marker_cache`）—— 避免重复字符串扫描
  4. 图号计数器（`_figure_counter_*`）—— 会话化 ContextVar + 进程级全局回退

从 export.py 拆出后，export.py 顶部加 `from .export_cache import *` re-export，
所有既有 `from app.services.export import _single_flight_render` 调用方无需改动。

本模块**完全独立**：仅依赖标准库 + config.PROJECTS_DATA_DIR + mermaid_renderer.ChartCache。
"""
from __future__ import annotations

import contextvars
import hashlib
import json
from io import BytesIO
import logging
import os
from pathlib import Path
import re
import threading
import time
import uuid

from ..config import PROJECTS_DATA_DIR
from .ai.mermaid_renderer import ChartCache


logger = logging.getLogger(__name__)


# ============================================================
# 图表渲染 L1 内存缓存（避免重复 render_mermaid_to_bytes）
# ============================================================

_chart_cache_dir = PROJECTS_DATA_DIR / "_exports" / "charts"
_chart_cache_dir.mkdir(parents=True, exist_ok=True)
_chart_cache = ChartCache(cache_dir=str(_chart_cache_dir))

# X-9（2026-10-16，N-6 新发现）：缓存条目存 `bytes` 而非 `BytesIO`。
# 背景：原实现在 `_store_chart_render` 里存 `BytesIO(data)`（可变流对象），
# 在 `_get_cached_chart_render` 命中时又用 `BytesIO(stream.getvalue())` 复制一份
# 返回。虽然 2026-10-13 的 F-3 修复通过副本解决了并发读光标互踩，但每次命中都
# 多做一次 `stream.getvalue()`（对 500KB-2MB 大图表尤其浪费）。
# 现改为直接存不可变 `bytes`：
#   1. `bytes` 是 CPython 只读对象，天然线程安全，命中时无需任何复制锁；
#   2. `BytesIO(data)` 只在返回时构造一次（省掉一次 getvalue 拷贝）；
#   3. 缓存条目本身不持有可变引用，杜绝任何调用方误写污染的可能。
_chart_render_cache: dict[str, tuple[bytes, int, float]] = {}  # key -> (raw_bytes, size, timestamp)
_chart_render_cache_lock = threading.Lock()
_chart_render_cache_max = 50  # 最多缓存 50 个不同 chart 渲染结果
_chart_render_cache_ttl = 300  # TTL: 5 分钟
_chart_render_cache_hits = 0
_chart_render_cache_misses = 0


def _get_cached_chart_render(payload_str: str, chart_type: str) -> tuple[BytesIO, int] | None:
    """查 L1 内存缓存，hit 则返回 (BytesIO, size)。

    X-9（2026-10-16）：返回的 BytesIO 每次都是基于缓存 `bytes` 现构造的独立对象，
    调用方读写不会污染缓存条目（缓存条目本身是 immutable bytes）。
    """
    global _chart_render_cache_hits, _chart_render_cache_misses
    cache_key = hashlib.sha256(f"{chart_type}|{payload_str}".encode("utf-8")).hexdigest()
    with _chart_render_cache_lock:
        entry = _chart_render_cache.get(cache_key)
    if entry:
        data, size, ts = entry
        if time.time() - ts < _chart_render_cache_ttl:
            _chart_render_cache_hits += 1
            # BUG-FIX 2026-10-13（P1·L1 缓存共享流竞态）：原实现直接把缓存条目里的
            # BytesIO 对象返回给调用方，多项目并发命中同一条目时两个调用方交替
            # seek(0)/read()，共享光标互相打断 → 一方读到空/半截字节 → 图片插入
            # 失败 → 正文留占位符且图号虚跳。改为返回独立副本，缓存条目保持只读。
            # X-9 增强：缓存条目直接存 bytes，命中路径只有一次 BytesIO 构造开销。
            return BytesIO(data), size
        # TTL 过期
        with _chart_render_cache_lock:
            _chart_render_cache.pop(cache_key, None)
    _chart_render_cache_misses += 1
    return None


def _store_chart_render(payload_str: str, stream: BytesIO, chart_type: str = "") -> None:
    """写入 L1 内存缓存（含 TTL + 容量上限淘汰）。

    X-9（2026-10-16）：缓存条目存 `bytes` 而非 `BytesIO`。
    """
    cache_key = hashlib.sha256(f"{chart_type}|{payload_str}".encode("utf-8")).hexdigest()
    stream.seek(0)
    data = stream.getvalue()
    size = len(data)
    with _chart_render_cache_lock:
        # TTL 淘汰
        now = time.time()
        expired = [k for k, (_, _, ts) in _chart_render_cache.items() if now - ts >= _chart_render_cache_ttl]
        for k in expired:
            _chart_render_cache.pop(k, None)
        # LRU 容量淘汰
        if len(_chart_render_cache) >= _chart_render_cache_max:
            oldest = min(_chart_render_cache.items(), key=lambda kv: kv[1][2])[0]
            _chart_render_cache.pop(oldest, None)
        _chart_render_cache[cache_key] = (data, size, now)


# ============================================================
# SINGLE-FLIGHT（2026-10-06：并发同 key 渲染合并）
# ============================================================

_inflight_renders: dict[str, threading.Event] = {}
_inflight_results: dict[str, tuple[float, bytes | None]] = {}
_inflight_lock = threading.Lock()
_INFLIGHT_WAIT_TIMEOUT = 90
_INFLIGHT_RESULT_TTL_SECONDS = 300
_INFLIGHT_RESULT_MAX_ENTRIES = 50


def _inflight_put(key: str, data: bytes | None) -> None:
    """写入 single-flight 产物并淘汰过期/超量条目（调用方不得持有 _inflight_lock）。"""
    now = time.time()
    with _inflight_lock:
        _inflight_results[key] = (now, data)
        for _k in [k for k, (ts, _d) in _inflight_results.items()
                   if now - ts > _INFLIGHT_RESULT_TTL_SECONDS]:
            _inflight_results.pop(_k, None)
        while len(_inflight_results) > _INFLIGHT_RESULT_MAX_ENTRIES:
            oldest = min(_inflight_results.items(), key=lambda kv: kv[1][0])[0]
            _inflight_results.pop(oldest, None)


def _single_flight_render(cache_key: str, do_render) -> BytesIO | None:
    """同 cache_key 的并发渲染合并为一次（single-flight）。

    Args:
        cache_key: 已剔除 fig_no 的缓存键。
        do_render: 无参可调用，真正执行渲染并返回 BytesIO | None。仅 owner 会调用。

    Returns:
        owner：自己渲染的产物（可能为 None）。
        等待方：与 owner 相同的产物（成功为 PNG BytesIO，失败为 None）。
    """
    with _inflight_lock:
        ev = _inflight_renders.get(cache_key)
        if ev is None:
            ev = threading.Event()
            _inflight_renders[cache_key] = ev
            _inflight_results.pop(cache_key, None)
            owner = True
        else:
            owner = False

    if not owner:
        ev.wait(timeout=_INFLIGHT_WAIT_TIMEOUT)
        with _inflight_lock:
            entry = _inflight_results.get(cache_key)
        data = entry[1] if entry else None
        return BytesIO(data) if data else None

    data: bytes | None = None
    out: BytesIO | None = None
    try:
        out = do_render()
        data = out.getvalue() if out else None
        return out
    finally:
        _inflight_put(cache_key, data)
        with _inflight_lock:
            ev.set()
            _inflight_renders.pop(cache_key, None)


# ============================================================
# 图号计数器（会话化 ContextVar + 进程级全局回退）
# ============================================================

_figure_counter_lock = threading.Lock()
_figure_counter_state: dict[str, int] = {}

_fig_counter_ctx_var: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "export_fig_counter_ctx", default=None
)

_FIG_NO_PREFIX_RE = re.compile(r"^图\d+-\d+\s*")


def _figure_counter_key(parent_section: str, project_id: int | None) -> str:
    """计数器分桶键：项目维度 + 父章节号（缺项目时归入 '-' 桶）"""
    return f"{project_id if project_id is not None else '-'}|{parent_section or 'default'}"


def _fig_chapter(parent_section: str) -> str:
    """把传入的章节标识归一为**展示章号**（图号「图{章号}-{序号}」的章号段）。

    BUG-FIX 2026-10-14（P1·同章图号重号）：图号语义是「按章连续编号」，章号取
    `parent_section` 第一个点号之前的段（如 "3.2" → "3"）。但旧实现分桶键用的是
    **完整** parent_section，而展示章号用的是**截断后**的章号——两者口径不一致。
    当调用方仅在未透传 chapter_number、回退用带点号的 parent_section_number 时
    （如 "3.1"、"3.2" 同属第 3 章的两个子节），两张图落入**不同桶**却都渲染成
    「图3-x」，各自从 1 计数 → 第 3 章出现两个「图3-1」（编号冲突）。
    现统一以截断后的章号同时作为**分桶键**与**展示章号**：同章恒共用一条计数器
    （序号连续且唯一），跨章互不影响。纯章号（生产主路径传入的 "3"）时截断为幂等，
    行为与原实现完全一致，零回归。
    空串 / 纯空白 / "default" 哨兵 → 返回空串（调用方据此不分配图号）。
    """
    s = str(parent_section or "").strip()
    if not s or s.lower() == "default":
        return ""
    return s.split(".")[0]


def _reset_figure_counter(project_id: int | None = None):
    """导出开始时重置图表计数器。

    Args:
        project_id: 给定时只清该项目的桶；不给定时清全部（向后兼容）。
    """
    with _figure_counter_lock:
        if project_id is None:
            _figure_counter_state.clear()
        else:
            prefix = f"{project_id}|"
            for k in [k for k in _figure_counter_state if k.startswith(prefix)]:
                _figure_counter_state.pop(k, None)


def _drop_fig_no(obj):
    """**递归**剔除任意层级的 `fig_no` 键（缓存键归一化）。"""
    if isinstance(obj, dict):
        return {k: _drop_fig_no(v) for k, v in obj.items() if k != "fig_no"}
    if isinstance(obj, list):
        return [_drop_fig_no(item) for item in obj]
    return obj


def _strip_fig_no_for_cache(payload) -> str:
    """生成图表缓存键：剔除 fig_no（会话内递增图号），并做 key 排序归一化。"""
    if isinstance(payload, dict):
        obj: object = _drop_fig_no(payload)
    elif isinstance(payload, str):
        try:
            parsed = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            return payload
        obj = _drop_fig_no(parsed) if isinstance(parsed, (dict, list)) else parsed
    else:
        obj = payload
    try:
        return json.dumps(obj, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(obj)


def _next_figure_number(parent_section: str, project_id: int | None = None) -> str:
    """获取下一个图表编号（线程安全）。

    Returns:
        格式化图号如 "图3-1"；章节号缺失时返回空串。

    BUG-FIX 2026-10-11（default桶安全兜底）：
      旧实现 `key = parent_section or "default"` 在 parent_section="" 时
      走 line 221-222 已经 return ""；但若某些路径显式传入 "default" 字符串
      作为 parent_section（如预渲染/兜底桶串位），会产出 "图default-1"。
      现增加对 "default" 字面值的防护，与空字符串同等处理。
    """
    if not parent_section or not str(parent_section).strip():
        return ""
    # 安全兜底：显式传入 "default"（桶名）不生成图号
    if str(parent_section).strip().lower() == "default":
        return ""
    # BUG-FIX 2026-10-14（P1·同章图号重号）：分桶键与展示章号统一取截断后的章号
    # （见 _fig_chapter），保证「同一章的图号连续且唯一」。纯章号输入时与原行为一致。
    chapter = _fig_chapter(parent_section)
    if not chapter:
        return ""
    bucket = _figure_counter_key(chapter, project_id)
    ctx = _fig_counter_ctx_var.get()
    if ctx is not None:
        ctx[bucket] = ctx.get(bucket, 0) + 1
        seq = ctx[bucket]
    else:
        with _figure_counter_lock:
            _figure_counter_state[bucket] = _figure_counter_state.get(bucket, 0) + 1
            seq = _figure_counter_state[bucket]
    return f"图{chapter}-{seq}"


def _rewind_figure_number(parent_section: str, project_id: int | None = None) -> None:
    """回滚刚分配的图号（渲染失败不得占号）。

    BUG-FIX 2026-10-14：分桶键必须与 _next_figure_number 严格一致（同取截断后的
    章号），否则回退找不到分配时写入的桶 → 图号不回退 → 后续图号整体前跳（虚跳）。
    """
    chapter = _fig_chapter(parent_section)
    if not chapter:
        return
    bucket = _figure_counter_key(chapter, project_id)
    ctx = _fig_counter_ctx_var.get()
    if ctx is not None:
        cur = ctx.get(bucket, 0)
        if cur <= 1:
            ctx.pop(bucket, None)
        else:
            ctx[bucket] = cur - 1
    else:
        with _figure_counter_lock:
            cur = _figure_counter_state.get(bucket, 0)
            if cur <= 1:
                _figure_counter_state.pop(bucket, None)
            else:
                _figure_counter_state[bucket] = cur - 1


def _snapshot_figure_counter() -> dict[str, int]:
    """快照当前图号计数器状态（供失败回滚后原地还原）。

    BUG-FIX 2026-10-13（P1·图号断档）：
      章节渲染失败时正文元素会被整体回滚（含已渲染出的图表与图题），但图号
      计数器只在**单张图表**渲染失败时通过 _rewind_figure_number 逐张回退。
      整节点失败走的是「删除已加入 body 的元素」路径，计数器不会回退 →
      重试时从新号继续 → 成品图号断档（图1-1 之后直接跳到 图1-3）。

    Returns:
        桶字典副本（bucket -> 已分配序号）。**不做深拷贝到 None**：调用方
        在快照后再分配号码，还原必须回到快照时的空桶状态，因此空桶也返回
        空 dict（而非 None）。
    """
    ctx = _fig_counter_ctx_var.get()
    if ctx is not None:
        return dict(ctx)
    with _figure_counter_lock:
        return dict(_figure_counter_state)


def _restore_figure_counter(snapshot: dict[str, int]) -> None:
    """把图号计数器原地还原到 _snapshot_figure_counter 的快照状态。

    Args:
        snapshot: 快照字典；原地 clear + update（不替换容器引用，避免
            调用方持有的旧引用指向过期状态）。
    """
    ctx = _fig_counter_ctx_var.get()
    if ctx is not None:
        ctx.clear()
        ctx.update(snapshot)
        return
    with _figure_counter_lock:
        _figure_counter_state.clear()
        _figure_counter_state.update(snapshot)


# ============================================================
# 内联图表标记缓存（按 section_detail id 缓存检测结果）
# ============================================================

_inline_marker_cache: dict[int, tuple[set[str], set[str], float]] = {}
_inline_marker_cache_lock = threading.Lock()
_inline_marker_cache_ttl = 300
_inline_marker_cache_max = 200
_inline_marker_cache_hits = 0
_inline_marker_cache_misses = 0


def _inline_marker_cache_cleanup_loop():
    """后台线程：每 60s 扫描一次，清理过期条目。"""
    while True:
        time.sleep(60)
        now = time.time()
        with _inline_marker_cache_lock:
            expired = [
                k for k, (_, _, ts) in _inline_marker_cache.items()
                if now - ts >= _inline_marker_cache_ttl
            ]
            for k in expired:
                del _inline_marker_cache[k]


_inline_marker_cache_cleanup_thread: threading.Thread | None = None


def _start_inline_marker_cache_cleanup():
    """启动后台清理线程（单例）。"""
    global _inline_marker_cache_cleanup_thread
    if (
        _inline_marker_cache_cleanup_thread is None
        or not _inline_marker_cache_cleanup_thread.is_alive()
    ):
        _inline_marker_cache_cleanup_thread = threading.Thread(
            target=_inline_marker_cache_cleanup_loop,
            daemon=True,
            name="inline_marker_cache_cleanup",
        )
        _inline_marker_cache_cleanup_thread.start()


# 启动清理线程（模块加载时）
_start_inline_marker_cache_cleanup()


__all__ = [
    # 全局变量
    "_chart_cache",
    "_chart_render_cache",
    "_chart_render_cache_lock",
    "_chart_render_cache_max",
    "_chart_render_cache_ttl",
    "_chart_render_cache_hits",
    "_chart_render_cache_misses",
    "_inflight_renders",
    "_inflight_results",
    "_inflight_lock",
    "_INFLIGHT_RESULT_MAX_ENTRIES",
    "_figure_counter_lock",
    "_figure_counter_state",
    "_fig_counter_ctx_var",
    "_FIG_NO_PREFIX_RE",
    "_inline_marker_cache",
    "_inline_marker_cache_lock",
    "_inline_marker_cache_ttl",
    "_inline_marker_cache_max",
    "_inline_marker_cache_hits",
    "_inline_marker_cache_misses",
    # 函数
    "_get_cached_chart_render",
    "_store_chart_render",
    "_inflight_put",
    "_single_flight_render",
    "_figure_counter_key",
    "_reset_figure_counter",
    "_drop_fig_no",
    "_strip_fig_no_for_cache",
    "_next_figure_number",
    "_rewind_figure_number",
    "_snapshot_figure_counter",
    "_restore_figure_counter",
    "_inline_marker_cache_cleanup_loop",
    "_start_inline_marker_cache_cleanup",
]
