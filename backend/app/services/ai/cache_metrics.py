# -*- coding: utf-8 -*-
"""缓存命中率监控（ENHANCE 2026-10-05 / P-23；标书智能体（4）2026-10-23 扩展）。

对齐易标规格九「缓存命中监控」：分层提示词的价值只有靠命中率才能被验证。
此前平台的缓存**只有读路径、没有计量**——正文/图片/导出各处各自维护
``_hits`` / ``_misses`` 局部变量，AI 调用的 prompt cache（Anthropic
``cache_control``）用量更是完全没被采集。运营侧无法回答两个关键问题：

1. 分层提示词改造后，Anthropic prompt cache 到底命中了多少 token？
   （命中率低说明分层没生效，稳定前缀被每次重写）
2. 章节正文缓存复用率如何？（复用率低说明重生成频繁、prompt 频繁漂移）

标书智能体（4）——提示词顺序优化（2026-10-23 引入）新增「按任务类型」的
prompt cache 计量：解析提取/目录生成/全局事实/正文生成/正文编排/图表生成/
审核预检七类高频任务各自累计 prompt/cached token 并计算命中率，使
「哪类任务的稳定前缀被破坏、缓存有没有生效」可量化、可持续观测；
命中率低于阈值且样本足够时按任务类型节流告警（配置
AI_CACHE_HIT_RATE_ALERT_THRESHOLD，默认 0=关闭，行为与历史一致）。

本模块提供进程级计数器（线程安全），供各缓存路径埋点，
并暴露 `snapshot()` 供 `/api/v1/ai/cache-metrics` 查询。

计数器语义：
    - ``section_content``: 章节正文缓存命中 / 未命中（每次生成 1 次）
    - ``image``           : AI 生图结果缓存命中 / 未命中
    - ``export``          : 导出结果缓存命中 / 未命中
    - ``prompt_cache``    : Anthropic prompt cache 的 token 级统计
      （``cache_creation_input_tokens`` 首次写入 /
        ``cache_read_input_tokens`` 命中复用）
    - ``task_types``      : 按任务类型的 prompt cache 计量（标书智能体（4））

注意：这是**进程内**统计，进程重启归零。要长期留存请对接 ai_monitor
或落库（usage_stats.record_call 已支持 cached_tokens 字段），本模块有意保持
零依赖、零 IO。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any


logger = logging.getLogger(__name__)

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

#: 按任务类型的 prompt cache 计量（标书智能体（4）缓存可观测性）。
#: key = task_type（section_content / outline_l1 / outline_l2 / chart_analysis ...）
_TASK_STATS: dict[str, dict[str, int]] = {}

#: 命中率告警节流：task_type -> 上次告警时间戳（单调时钟）
_LAST_ALERT_TS: dict[str, float] = {}


def _cfg(name: str, default: Any) -> Any:
    """读取配置（动态获取，测试 monkeypatch app.config 后即时生效）。"""
    try:
        import app.config as app_config

        return getattr(app_config, name, default)
    except Exception:  # pragma: no cover - 防御性（配置不可用不影响计量）
        return default


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


def record_prompt_cache(
    usage: dict | None,
    *,
    task_type: str = "unknown",
    section_id: str = "",
    model: str = "",
) -> None:
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

    标书智能体（4）（2026-10-23）：新增按任务类型（``task_type``）的计量与
    单次调用日志、命中率告警——缓存优化是否生效，必须能按任务观察到
    （正文生成 50+ 次/项目，与目录生成 1+N 次的命中特征完全不同）。

    Args:
        usage: 响应里的 usage 字典，可为 None。
        task_type: 任务类型（section_content / outline_l1 / outline_l2 /
            orchestration / chart_analysis / chart_payload / quality_eval /
            outline_review / analysis_* ...）。
        section_id: 章节 ID（可选，用于日志定位）。
        model: 模型名（可选，用于日志）。
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

    # ── 按任务类型累计（统一口径：prompt=本次输入总量，cached=其中命中量）──
    task_prompt = 0
    task_cached = 0
    with _lock:
        if creation > 0 or read > 0:
            _PROMPT_CACHE["cache_creation_input_tokens"] += creation
            _PROMPT_CACHE["cache_read_input_tokens"] += read
            _PROMPT_CACHE["input_tokens"] += int(usage.get("input_tokens", 0) or 0)
            _PROMPT_CACHE["output_tokens"] += int(usage.get("output_tokens", 0) or 0)
            _PROMPT_CACHE["requests_with_cache"] += 1
            # Anthropic 口径：prompt_tokens 字段通常不存在，输入总量需自行合成
            task_prompt += (
                int(usage.get("input_tokens", 0) or 0) + creation + read
            )
            task_cached += read
        if prompt_tokens > 0 or cached_openai > 0:
            _PROMPT_CACHE["openai_prompt_tokens"] += prompt_tokens
            _PROMPT_CACHE["openai_cached_tokens"] += cached_openai
            _PROMPT_CACHE["openai_completion_tokens"] += completion_tokens
            _PROMPT_CACHE["openai_requests"] += 1
            task_prompt += prompt_tokens
            task_cached += cached_openai

        if not _cfg("AI_CACHE_METRICS_ENABLED", True):
            return
        st = _TASK_STATS.setdefault(
            task_type or "unknown",
            {
                "requests": 0,
                "prompt_tokens": 0,
                "cached_tokens": 0,
                "completion_tokens": 0,
                "cache_creation_tokens": 0,
            },
        )
        st["requests"] += 1
        st["prompt_tokens"] += task_prompt
        st["cached_tokens"] += task_cached
        st["completion_tokens"] += completion_tokens
        st["cache_creation_tokens"] += creation
        requests = st["requests"]
        hit_rate = (st["cached_tokens"] / st["prompt_tokens"] * 100) if st["prompt_tokens"] else 0.0

    # 单次调用日志（DEBUG：正文生成每项目 50+ 次，INFO 会淹没日志）
    if _cfg("AI_CACHE_LOG_ENABLED", True):
        _call_rate = (task_cached / task_prompt * 100) if task_prompt else 0.0
        logger.debug(
            "[缓存计量] task=%s section=%s model=%s prompt=%d cached=%d 本次命中=%.1f%% 累计命中=%.1f%%(%d次)",
            task_type, section_id or "-", model or "-",
            task_prompt, task_cached, _call_rate, hit_rate, requests,
        )

    _maybe_alert_hit_rate(task_type or "unknown")


def record_provider_cache_usage(
    provider: Any,
    *,
    task_type: str = "unknown",
    section_id: str = "",
) -> None:
    """从平台 provider 的 ``last_usage`` 采集 prompt cache 用量并记账。

    供 collect_json_response / call_ai_text / provider.chat 等不直接返回
    usage 的公共调用链使用（目录生成、编排、审核、图表、解析等）。
    OpenAI 兼容 provider 每次 chat 后回填 ``_last_usage``；Anthropic provider
    无该属性时静默跳过（fail-soft，绝不影响主链路）。
    """
    try:
        usage = getattr(provider, "last_usage", None)
        if not usage:
            return
        record_prompt_cache(
            usage,
            task_type=task_type,
            section_id=section_id,
            model=str(getattr(provider, "model", "") or ""),
        )
    except Exception as e:  # pragma: no cover - 防御性
        logger.debug("[silent-except] provider 缓存用量采集失败: %s", e)


def _maybe_alert_hit_rate(task_type: str) -> None:
    """命中率低于阈值且样本足够时按任务类型节流告警（可观测性闭环）。

    阈值 AI_CACHE_HIT_RATE_ALERT_THRESHOLD 默认 0=关闭（与历史行为一致）；
    开启后（如 0.3）某任务累计命中率持续低于阈值，说明稳定前缀被破坏
    （提示词顺序回退、共享上下文混入时间戳/随机数、sibling 列表按叶子漂移），
    需要人工介入排查。节流 AI_CACHE_ALERT_THROTTLE_SECONDS 默认 600s。
    """
    try:
        threshold = float(_cfg("AI_CACHE_HIT_RATE_ALERT_THRESHOLD", 0) or 0)
        if threshold <= 0:
            return
        min_samples = int(_cfg("AI_CACHE_HIT_RATE_MIN_SAMPLES", 20) or 0)
        throttle = float(_cfg("AI_CACHE_ALERT_THROTTLE_SECONDS", 600) or 600)
        now = time.monotonic()
        with _lock:
            st = _TASK_STATS.get(task_type)
            if not st or st["requests"] < max(1, min_samples):
                return
            rate = (st["cached_tokens"] / st["prompt_tokens"]) if st["prompt_tokens"] else 0.0
            last = _LAST_ALERT_TS.get(task_type, 0.0)
            if now - last < throttle:
                return
            _LAST_ALERT_TS[task_type] = now
            requests = st["requests"]
            prompt_tokens = st["prompt_tokens"]
            cached_tokens = st["cached_tokens"]
        logger.warning(
            "[缓存告警] task=%s 累计命中率 %.1f%% 低于阈值 %.0f%%"
            "（%d 次请求，prompt=%d cached=%d）——稳定前缀可能被破坏，"
            "请检查提示词顺序/共享上下文是否混入易变内容",
            task_type, rate * 100, threshold * 100,
            requests, prompt_tokens, cached_tokens,
        )
    except Exception as e:  # pragma: no cover - 防御性
        logger.debug("[silent-except] 缓存命中率告警失败: %s", e)


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

        # 标书智能体（4）：按任务类型的命中率拆解（哪类任务吃到了缓存）
        task_types: dict[str, Any] = {}
        for name, st in sorted(
            _TASK_STATS.items(), key=lambda kv: kv[1]["requests"], reverse=True
        ):
            p = st["prompt_tokens"]
            task_types[name] = {
                "requests": st["requests"],
                "prompt_tokens": p,
                "cached_tokens": st["cached_tokens"],
                "completion_tokens": st["completion_tokens"],
                "cache_creation_tokens": st["cache_creation_tokens"],
                "hit_rate": round(st["cached_tokens"] / p * 100, 1) if p else 0.0,
            }

    return {
        "stores": stores,
        "prompt_cache": prompt_cache,
        # ENHANCE 2026-10-09（E-3）：OpenAI 兼容链路命中率独立暴露
        "openai_prompt_cache": openai_prompt_cache,
        # 标书智能体（4）（2026-10-23）：按任务类型的命中率拆解
        "task_types": task_types,
    }


def reset() -> None:
    """清零全部计数器（测试用）。"""
    with _lock:
        _STORES.clear()
        for k in _PROMPT_CACHE:
            _PROMPT_CACHE[k] = 0
        _TASK_STATS.clear()
        _LAST_ALERT_TS.clear()
