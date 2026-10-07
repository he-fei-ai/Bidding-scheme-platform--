"""系统监控服务体系"""

from collections import defaultdict
from datetime import datetime, timedelta
import logging
from typing import Any


logger = logging.getLogger(__name__)


class PerformanceMonitor:
    """性能监控器"""

    def __init__(self):
        self._metrics = defaultdict(list)
        self._alerts = []

    def record_metric(self, name: str, value: float, tags: dict[str, str] = None):
        """记录指标"""
        metric = {"name": name, "value": value, "timestamp": datetime.now(), "tags": tags or {}}
        self._metrics[name].append(metric)

        # 保留最近1000个数据点
        if len(self._metrics[name]) > 1000:
            self._metrics[name] = self._metrics[name][-1000:]

    def get_average(self, name: str, minutes: int = 5) -> float | None:
        """获取最近N分钟的平均值"""
        now = datetime.now()
        cutoff = now - timedelta(minutes=minutes)

        values = [m["value"] for m in self._metrics.get(name, []) if m["timestamp"] >= cutoff]

        return sum(values) / len(values) if values else None

    def get_max(self, name: str, minutes: int = 5) -> float | None:
        """获取最近N分钟的最大值"""
        now = datetime.now()
        cutoff = now - timedelta(minutes=minutes)

        values = [m["value"] for m in self._metrics.get(name, []) if m["timestamp"] >= cutoff]

        return max(values) if values else None

    def get_count(self, name: str, minutes: int = 5) -> int:
        """获取最近N分钟的调用次数"""
        now = datetime.now()
        cutoff = now - timedelta(minutes=minutes)

        return sum(1 for m in self._metrics.get(name, []) if m["timestamp"] >= cutoff)

    def add_alert(self, metric_name: str, condition: str, value: float, threshold: float):
        """添加告警"""
        alert = {
            "metric": metric_name,
            "condition": condition,
            "value": value,
            "threshold": threshold,
            "timestamp": datetime.now(),
        }
        self._alerts.append(alert)
        logger.warning("Alert: %s %s %s (threshold: %s)", metric_name, condition, value, threshold)

    def get_metrics_summary(self) -> dict[str, Any]:
        """获取指标摘要"""
        summary = {}
        for name, metrics in self._metrics.items():
            if metrics:
                values = [m["value"] for m in metrics]
                summary[name] = {
                    "count": len(values),
                    "avg": sum(values) / len(values),
                    "max": max(values),
                    "min": min(values),
                    "last_updated": metrics[-1]["timestamp"],
                }
        return summary


# 全局监控实例
_monitor = None


def get_monitor() -> PerformanceMonitor:
    """获取监控实例"""
    global _monitor
    if _monitor is None:
        _monitor = PerformanceMonitor()
    return _monitor
