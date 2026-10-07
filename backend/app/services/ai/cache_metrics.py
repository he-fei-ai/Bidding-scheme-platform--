# -*- coding: utf-8 -*-
"""缓存命中率监控（ENHANCE 2026-10-05 / P-23）。

对齐易标规格九「缓存命中监控」：分层提示词的价值只有靠命中率才能被验证。
此前平台的缓存**只有读路径、没有计量**——正文/图片/导出各处各自维护
``_hits`` / ``_misses`` 局部变量，AI 调用的 prompt cache（Anthropic
``cache_control``）用量更是完全没被采集。运营侧无法回答两个关键问题：

1. 分层提示词改造后，Anthropic prompt cache 到底命中了多少 token？
   （命中率低说明分层没生效，稳定前缀被每次重写）
2. 章节正文缓存复用率如何？（复用率低说明重生成频繁、prompt 频繁漂移）

本模块提供进程级计数器（线程安全），供各缓存路径埋点，
并暴露 `snapshot()` 供 `/api/v1/ai/cache-metrics` 查询。

计数器语义：
    - ``section_content``: 章节正文缓存命中 / 未命中（每次生成 1 次）
    - ``image``           : AI 生图结果缓存命中 / 未命中
    - ``export``          : 导出结果缓存命中 / 未命中
    - ``prompt_cache``    : Anthropic prompt cache 的 token 级统计
      （``cache_creation_input_tokens`` 首次写入 /
        ``cache_read_input_tokens`` 命中复用）

注意：这是**进程内**统计，进程重启归零。要长期留存请对接 ai_monitor
或落库，本模块有意保持零依赖、零 IO。
"""

from __future__ import annotations

import threading
from typing import Any


_lock = threading.Lock()

#: 各缓存组件的命中/未命中计数
_STORES: dict[str, dict[str, int]] = {}

#: Anthropic prompt cache token 级统计
_PROMPT_CACHE: dict[str, int] = {
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "requests_with_cache": 0,
}


def _store(name: str) -> dict[str, int]:
    return _STORES.setdefault(name, {"hits": 0, "misses": 0, "total_bytes": 0})


def record_store(name: str, hit: bool, *, size: int = 0) -> None:
    """记录一次缓存组件的命中/未命中。

    Args:
        name: 组件名（section_content / image / export ...）
        hit: True = 命中（省一次 AI/IO 调用）
        size: 命中/写入内容的字节数（可选，用于估算节省量）
    """
    with _lock:
        s = _store(name)
        if hit:
            s["hits"] += 1
        else:
            s["misses"] += 1
        if size > 0:
            s["total_bytes"] += int(size)


def record_prompt_cache(usage: dict | None) -> None:
    """从一次 AI 响应的 ``usage`` 字段采集 prompt cache token 统计。

    Anthropic 官方端点在启用 ``cache_control`` 后，``usage`` 会带
    ``cache_creation_input_tokens``（本次新写入缓存的 token）与
    ``cache_read_input_tokens``（本次从缓存读取命中的 token）。
    其他 provider 不返回这些字段时，按 0 累加（不报错）。

    Args:
        usage: 响应里的 usage 字典，可为 None。
    """
    if not usage:
        return
    creation = int(usage.get("cache_creation_input_tokens", 0) or 0)
    read = int(usage.get("cache_read_input_tokens", 0) or 0)
    if creation <= 0 and read <= 0:
        # 非 Anthropic 端点或该次调用未走缓存路径：不计数，避免稀释命中率
        return
    with _lock:
        _PROMPT_CACHE["cache_creation_input_tokens"] += creation
        _PROMPT_CACHE["cache_read_input_tokens"] += read
        _PROMPT_CACHE["input_tokens"] += int(usage.get("input_tokens", 0) or 0)
        _PROMPT_CACHE["output_tokens"] += int(usage.get("output_tokens", 0) or 0)
        _PROMPT_CACHE["requests_with_cache"] += 1


def snapshot() -> dict[str, Any]:
    """返回当前缓存计量快照（供 API 查询）。"""
    with _lock:
        stores: dict[str, Any] = {}
        for name, s in sorted(_STORES.items()):
            total = s["hits"] + s["misses"]
            stores[name] = {
                "hits": s["hits"],
                "misses": s["misses"],
                "total": total,
                "hit_rate": round(s["hits"] / total * 100, 1) if total else 0.0,
                "total_bytes": s["total_bytes"],
            }

        pc = dict(_PROMPT_CACHE)
        cached = pc["cache_creation_input_tokens"] + pc["cache_read_input_tokens"]
        prompt_cache = {
            "requests_with_cache": pc["requests_with_cache"],
            "cache_creation_input_tokens": pc["cache_creation_input_tokens"],
            "cache_read_input_tokens": pc["cache_read_input_tokens"],
            "input_tokens": pc["input_tokens"],
            "output_tokens": pc["output_tokens"],
            # 命中率 = 命中的 cache_read token / (cache_read + cache_creation)
            # 即「本次输入有多少是从缓存里读出来的」
            "hit_rate": (
                round(pc["cache_read_input_tokens"] / cached * 100, 1)
                if cached else 0.0
            ),
            "total_cached_tokens": cached,
        }

    return {
        "stores": stores,
        "prompt_cache": prompt_cache,
    }


def reset() -> None:
    """清零全部计数器（测试用）。"""
    with _lock:
        _STORES.clear()
        for k in _PROMPT_CACHE:
            _PROMPT_CACHE[k] = 0
