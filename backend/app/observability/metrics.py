"""指标看板（ENHANCE 2026-10-29）。

`MetricsRegistry` 提供：
- 各模块调用次数 / 成功率 / 失败率
- P50 / P95 / P99 耗时趋势
- 重试 / 降级次数
- 缓存命中率
- Token 消耗与成本趋势
- 错误码分布 TOP10
- 队列积压与等待时长

线程安全（threading.Lock），进程内滑动窗口（时间桶），供 `/observability/metrics`
读取。只依赖标准库。
"""
from __future__ import annotations

import math
import threading
import time
from collections import Counter, deque
from typing import Any


def percentile(values: list[float], pct: float) -> float:
    """计算百分位（pct 为 50/95/99）。"""
    if not values:
        return 0.0
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    idx = (pct / 100.0) * (len(s) - 1)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return float(s[lo])
    return float(s[lo] + (s[hi] - s[lo]) * (idx - lo))


class MetricsRegistry:
    """进程内指标注册表（滑动窗口）。"""

    def __init__(self, window_seconds: int = 3600) -> None:
        self._window = window_seconds
        self._lock = threading.Lock()
        # 名称 -> deque[(ts, value)]
        self._series: dict[str, deque[tuple[float, float]]] = {}
        # 错误码 -> count
        self._errors: Counter[str] = Counter()
        # 事件类型 -> count（success/fail/retry/degrade/cache_hit/cache_miss）
        self._events: Counter[str] = Counter()
        self._max_points = 10000

    def _bucket(self, name: str) -> deque[tuple[float, float]]:
        dq = self._series.get(name)
        if dq is None:
            dq = deque(maxlen=self._max_points)
            self._series[name] = dq
        return dq

    def record(self, name: str, value: float, tags: dict[str, str] | None = None) -> None:
        """记录一个指标点（name 形如 'ai.call.duration' 或 'module:content.call'）。"""
        now = time.time()
        with self._lock:
            self._bucket(name).append((now, float(value)))

    def record_event(self, event: str, delta: int = 1) -> None:
        with self._lock:
            self._events[event] += delta

    def record_error(self, error_code: str) -> None:
        with self._lock:
            self._errors[error_code or "UNKNOWN"] += 1

    def _prune(self, dq: deque[tuple[float, float]], now: float) -> None:
        cutoff = now - self._window
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def summary(self, name: str) -> dict[str, Any]:
        """指标摘要：count / avg / min / max / p50 / p95 / p99。"""
        with self._lock:
            dq = self._bucket(name)
            self._prune(dq, time.time())
            values = [v for _, v in dq]
        if not values:
            return {"count": 0, "avg": 0.0, "min": 0.0, "max": 0.0,
                    "p50": 0.0, "p95": 0.0, "p99": 0.0}
        return {
            "count": len(values),
            "avg": round(sum(values) / len(values), 3),
            "min": round(min(values), 3),
            "max": round(max(values), 3),
            "p50": round(percentile(values, 50), 3),
            "p95": round(percentile(values, 95), 3),
            "p99": round(percentile(values, 99), 3),
        }

    def snapshot(self) -> dict[str, Any]:
        """全量指标快照（供看板渲染）。"""
        with self._lock:
            now = time.time()
            cutoff = now - self._window
            series = {k: [p for p in v if p[0] >= cutoff] for k, v in self._series.items()}
            errors = dict(self._errors)
            events = dict(self._events)
        out: dict[str, Any] = {"metrics": {}, "errors": errors, "events": events}
        for name, pts in series.items():
            if not pts:
                continue
            values = [v for _, v in pts]
            out["metrics"][name] = {
                "count": len(values),
                "avg": round(sum(values) / len(values), 3),
                "min": round(min(values), 3),
                "max": round(max(values), 3),
                "p50": round(percentile(values, 50), 3),
                "p95": round(percentile(values, 95), 3),
                "p99": round(percentile(values, 99), 3),
            }
        # 错误码 TOP10
        top = sorted(errors.items(), key=lambda kv: kv[1], reverse=True)[:10]
        out["error_top10"] = [{"code": c, "count": n} for c, n in top]
        # 成功率（按事件统计）
        total_calls = events.get("call", 0) or 1
        fails = events.get("fail", 0)
        out["success_rate"] = round(1 - fails / total_calls, 4) if total_calls else 1.0
        cache_hit = events.get("cache_hit", 0)
        cache_total = cache_hit + events.get("cache_miss", 0)
        out["cache_hit_rate"] = round(cache_hit / cache_total, 4) if cache_total else 0.0
        return out


#: 全局指标注册表
_registry: MetricsRegistry | None = None
_registry_lock = threading.Lock()


def get_registry() -> MetricsRegistry:
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = MetricsRegistry()
    return _registry


def record_metric(name: str, value: float, tags: dict[str, str] | None = None) -> None:
    get_registry().record(name, value, tags)


def record_event(event: str, delta: int = 1) -> None:
    get_registry().record_event(event, delta)


def record_error(error_code: str) -> None:
    get_registry().record_error(error_code)


def metrics_snapshot() -> dict[str, Any]:
    return get_registry().snapshot()
