"""告警规则与通知（ENHANCE 2026-10-29）。

`AlertEngine` 支持：
- 规则：错误率超阈值、P99 耗时超阈值、重试异常升高、成本异常升高、
  trace_id 断链率超阈值、日志写入失败、队列积压超阈值。
- 去重 + 冷却（静默期）防告警风暴。
- 通知：站内（内存/DB 记录）/ 邮件（可插拔，默认空）/ Webhook（HTTP POST）。
- 告警内容：类型 / 触发条件 / 当前值 / 阈值 / 关联 trace_id / 建议排查方向。

只依赖标准库 + 本包 fields/structured；Webhook 用 urllib（超时短、失败宽容）。
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from typing import Any

from . import fields as _f


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except Exception:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except Exception:
        return default


#: 建议排查方向（按告警类型）
SUGGESTIONS: dict[str, str] = {
    "error_rate": "检查错误码分布 TOP 项与对应模块日志；排查上游 AI/DB 可用性",
    "p99_latency": "定位 P99 耗时的 span/接口；检查慢查询与外部依赖超时",
    "retry_spike": "检查重试原因分布；确认是否为可重试瞬时错误被放大",
    "cost_spike": "检查 token 消耗与模型路由；确认是否降级/长上下文激增",
    "trace_break": "检查 span 父子关系；确认跨线程/进程 context 传播是否失效",
    "log_write_fail": "检查日志目录权限/磁盘空间/队列积压",
    "queue_backlog": "检查日志写入线程是否阻塞；确认磁盘 IO 与限速配置",
}


class AlertEngine:
    """告警引擎（去重 + 冷却 + 通知）。"""

    def __init__(
        self,
        *,
        cooldown_seconds: float | None = None,
        webhook_url: str | None = None,
    ) -> None:
        self.cooldown = _env_float("OBS_ALERT_COOLDOWN", 300.0) if cooldown_seconds is None else cooldown_seconds
        self.webhook_url = os.environ.get("OBS_ALERT_WEBHOOK", "") if webhook_url is None else webhook_url
        self._last_fire: dict[str, float] = {}
        self._lock = threading.Lock()
        self._history: list[dict[str, Any]] = []
        self._max_history = 500

    def _notify(self, alert: dict[str, Any]) -> None:
        """通知通道：日志 + Webhook（超时短、失败宽容）。"""
        # 站内：日志（统一结构化，含 trace_id）
        try:
            from . import structured as _s

            _s.log_event(
                "ERROR", "alert.fire", module="observability",
                message=alert["message"],
                status="fired", alert_type=alert["type"],
                alert_value=alert.get("value"), alert_threshold=alert.get("threshold"),
                trace_id=alert.get("trace_id", "-"),
                detail={"suggestion": alert.get("suggestion", "")},
            )
        except Exception:
            pass
        # Webhook
        if self.webhook_url:
            try:
                data = json.dumps(alert, ensure_ascii=False).encode("utf-8")
                req = urllib.request.Request(
                    self.webhook_url, data=data,
                    headers={"Content-Type": "application/json"},
                )
                urllib.request.urlopen(req, timeout=3)
            except Exception:
                pass

    def fire(
        self,
        alert_type: str,
        value: float,
        threshold: float,
        *,
        condition: str = ">",
        trace_id: str = "-",
        detail: str = "",
    ) -> bool:
        """触发告警（去重 + 冷却）。返回是否真正发出。"""
        now = time.time()
        with self._lock:
            last = self._last_fire.get(alert_type, 0)
            if now - last < self.cooldown:
                return False  # 静默期内去重
            self._last_fire[alert_type] = now
        alert: dict[str, Any] = {
            "type": alert_type,
            "condition": f"{condition} {threshold}",
            "value": value,
            "threshold": threshold,
            "trace_id": trace_id,
            "suggestion": SUGGESTIONS.get(alert_type, "查看对应模块日志定位根因"),
            "message": f"[{alert_type}] 当前 {value} {condition} 阈值 {threshold}"
                       + (f"（{detail}）" if detail else ""),
            "fired_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
        }
        with self._lock:
            self._history.append(alert)
            if len(self._history) > self._max_history:
                del self._history[: len(self._history) - self._max_history]
        self._notify(alert)
        return True

    def history(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._history[-limit:])


#: 全局单例
_engine: AlertEngine | None = None
_engine_lock = threading.Lock()


def get_engine() -> AlertEngine:
    global _engine
    if _engine is None:
        with _engine_lock:
            if _engine is None:
                _engine = AlertEngine()
    return _engine


def fire_alert(
    alert_type: str, value: float, threshold: float,
    *, condition: str = ">", trace_id: str = "-", detail: str = "",
) -> bool:
    return get_engine().fire(alert_type, value, threshold, condition=condition, trace_id=trace_id, detail=detail)


def alert_history(limit: int = 50) -> list[dict[str, Any]]:
    return get_engine().history(limit)
