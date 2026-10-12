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
    # ENHANCE 2026-10-09（E-3）：OpenAI 兼容链路（prompt_tokens_details.cached_tokens）
    "openai_prompt_tokens": 0,
    "openai_cached_tokens": 0,
    "openai_completion_tokens": 0,
    "openai_requests": 0,
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

    ENHANCE 2026-10-09（E-3 缓存度量闭环）：OpenAI 兼容链路的 usage 带
    ``prompt_tokens_details.cached_tokens``（DeepSeek / OpenRouter / 智谱 /
    火山方舟等中转普遍透出该字段，语义同易标 D5 文章中的 ``cached_tokens``）。
    此前只认 Anthropic 字段 → OpenAI 兼容供应商「缓存命中了多少」完全不可见。
    现两条链路分开累计、分别计算命中率（Anthropic 口径保持不变，避免破坏
    既有监控语义）。

    Args:
        usage: 响应里的 usage 字典，可为 None。
    """
    if not usage:
        return
    creation = int(usage.get("cache_creation_input_tokens", 0) or 0)
    read = int(usage.get("cache_read_input_tokens", 0) or 0)
    details = usage.get("prompt_tokens_details")
    cached_openai = 0
    if isinstance(details, dict):
        cached_openai = int(details.get("cached_tokens", 0) or 0)
    prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
    completion_tokens = int(usage.get("completion_tokens", 0) or 0)
    if creation <= 0 and read <= 0 and cached_openai <= 0 and prompt_tokens <= 0:
        # 无任何可用计量信息（如仅返回 content 的旧版响应）：不计数
        return
    with _lock:
        if creation > 0 or read > 0:
            _PROMPT_CACHE["cache_creation_input_tokens"] += creation
            _PROMPT_CACHE["cache_read_input_tokens"] += read
            _PROMPT_CACHE["input_tokens"] += int(usage.get("input_tokens", 0) or 0)
            _PROMPT_CACHE["output_tokens"] += int(usage.get("output_tokens", 0) or 0)
            _PROMPT_CACHE["requests_with_cache"] += 1
        if prompt_tokens > 0 or cached_openai > 0:
            _PROMPT_CACHE["openai_prompt_tokens"] += prompt_tokens
            _PROMPT_CACHE["openai_cached_tokens"] += cached_openai
            _PROMPT_CACHE["openai_completion_tokens"] += completion_tokens
            _PROMPT_CACHE["openai_requests"] += 1


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
        # ENHANCE 2026-10-09（E-3）：OpenAI 兼容链路的缓存命中率独立口径：
        # 命中率 = cached_tokens / prompt_tokens（与易标 D5 文章实测口径一致）
        oc_prompt = pc["openai_prompt_tokens"]
        oc_cached = pc["openai_cached_tokens"]
        openai_prompt_cache = {
            "requests": pc["openai_requests"],
            "prompt_tokens": oc_prompt,
            "cached_tokens": oc_cached,
            "completion_tokens": pc["openai_completion_tokens"],
            "hit_rate": round(oc_cached / oc_prompt * 100, 1) if oc_prompt else 0.0,
        }

    return {
        "stores": stores,
        "prompt_cache": prompt_cache,
        # ENHANCE 2026-10-09（E-3）：OpenAI 兼容链路命中率独立暴露
        "openai_prompt_cache": openai_prompt_cache,
    }


def reset() -> None:
    """清零全部计数器（测试用）。"""
    with _lock:
        _STORES.clear()
        for k in _PROMPT_CACHE:
            _PROMPT_CACHE[k] = 0
