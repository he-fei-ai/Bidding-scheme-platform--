"""AI 调用监控和告警模块

功能：
1. 跟踪 AI 调用成功率/失败率
2. 监控响应时间分布
3. 检测异常波动并触发告警
4. 提供实时监控面板数据
"""

from collections import deque
from dataclasses import dataclass, field
import logging
import threading
import time
from typing import Any


logger = logging.getLogger(__name__)


# ========== 数据结构 ==========


@dataclass
class AICallRecord:
    """单次 AI 调用记录"""

    timestamp: float
    provider: str
    model: str
    success: bool
    response_time: float  # 秒
    tokens_used: int = 0
    error: str = ""
    task_type: str = ""  # outline/content/chart


@dataclass
class AlertConfig:
    """告警配置"""

    # 失败率阈值（百分比）
    failure_rate_threshold: float = 30.0

    # 响应时间阈值（秒）
    response_time_threshold: float = 60.0

    # 监控窗口大小（次数）
    window_size: int = 50

    # 告警冷却时间（秒）- 避免频繁告警
    cooldown_seconds: float = 300.0

    # 是否启用告警
    enabled: bool = True


@dataclass
class MonitorStats:
    """监控统计信息"""

    # 总调用次数
    total_calls: int = 0

    # 成功次数
    success_calls: int = 0

    # 失败次数
    failed_calls: int = 0

    # 平均响应时间（秒）
    avg_response_time: float = 0.0

    # 最大响应时间（秒）
    max_response_time: float = 0.0

    # 最小响应时间（秒）
    min_response_time: float = float("inf")

    # 当前失败率（百分比）
    failure_rate: float = 0.0

    # 最近 N 次调用的响应时间列表
    recent_response_times: list[float] = field(default_factory=list)

    # 各 provider 的统计
    provider_stats: dict[str, dict] = field(default_factory=dict)


# ========== 监控器核心类 ==========


class AICallMonitor:
    """AI 调用监控器

    功能：
    1. 记录每次 AI 调用的详细信息
    2. 计算滑动窗口内的统计指标
    3. 检测异常并触发告警
    4. 提供实时监控数据
    """

    def __init__(self, alert_config: AlertConfig | None = None):
        self.alert_config = alert_config or AlertConfig()

        # 调用记录队列（滑动窗口）
        self._records: deque[AICallRecord] = deque(maxlen=self.alert_config.window_size)

        # 统计信息
        self._stats = MonitorStats()

        # 告警状态
        # FIX 2026-10-23（P1，日志实证 21:53:37 失败率 42% 告警）：
        # 原实现三种告警（high_failure_rate / high_latency / slow_request）
        # **共用同一个冷却时间戳**，于是任何一条先触发的告警都会把随后
        # `cooldown_seconds`（默认 300s）内的其它告警全部静默——一条 info 级
        # 「检测到慢请求」就能吞掉 5 分钟里的 critical 级失败率告警。
        # 改为按 alert type 各自冷却，互不遮挡。
        self._last_alert_time: float = 0  # 保留字段供既有测试/外部读取（全局最近一次告警时刻）
        self._last_alert_time_by_type: dict[str, float] = {}
        self._active_alerts: list[dict] = []

        # 线程锁
        self._lock = threading.Lock()

        # 告警回调函数
        self._alert_callbacks: list[Any] = []

        logger.info(
            "[AI监控] 监控器已初始化，窗口大小=%d, 失败率阈值=%.1f%%",
            self.alert_config.window_size,
            self.alert_config.failure_rate_threshold,
        )

    def record_call(
        self,
        provider: str,
        model: str,
        success: bool,
        response_time: float,
        tokens_used: int = 0,
        error: str = "",
        task_type: str = "",
    ) -> None:
        """记录一次 AI 调用

        Args:
            provider: AI 提供商名称
            model: 模型名称
            success: 是否成功
            response_time: 响应时间（秒）
            tokens_used: 使用的 token 数
            error: 错误信息（如果失败）
            task_type: 任务类型（outline/content/chart）
        """
        with self._lock:
            # 创建记录
            record = AICallRecord(
                timestamp=time.time(),
                provider=provider,
                model=model,
                success=success,
                response_time=response_time,
                tokens_used=tokens_used,
                error=error,
                task_type=task_type,
            )

            # 添加到队列
            self._records.append(record)

            # 更新统计
            self._update_stats()

            # 检查是否需要告警
            if not success:
                logger.warning("[AI监控] 调用失败 [%s/%s]: %s", provider, model, error)

            # 检查告警条件
            self._check_alerts(record)

    def _update_stats(self) -> None:
        """更新统计信息"""
        if not self._records:
            return

        self._stats.total_calls = len(self._records)
        self._stats.success_calls = sum(1 for r in self._records if r.success)
        self._stats.failed_calls = sum(1 for r in self._records if not r.success)

        # 计算响应时间统计
        response_times = [r.response_time for r in self._records]
        self._stats.recent_response_times = response_times
        self._stats.avg_response_time = sum(response_times) / len(response_times)
        self._stats.max_response_time = max(response_times)
        self._stats.min_response_time = min(response_times)

        # 计算失败率
        if self._stats.total_calls > 0:
            self._stats.failure_rate = (self._stats.failed_calls / self._stats.total_calls) * 100
        else:
            self._stats.failure_rate = 0.0

        # 按 provider 统计
        self._stats.provider_stats.clear()
        for record in self._records:
            if record.provider not in self._stats.provider_stats:
                self._stats.provider_stats[record.provider] = {
                    "total": 0,
                    "success": 0,
                    "failed": 0,
                    "response_times": [],
                }

            stats = self._stats.provider_stats[record.provider]
            stats["total"] += 1
            if record.success:
                stats["success"] += 1
            else:
                stats["failed"] += 1
            stats["response_times"].append(record.response_time)

        # 计算各 provider 的平均响应时间和失败率
        for provider, stats in self._stats.provider_stats.items():
            if stats["response_times"]:
                stats["avg_response_time"] = sum(stats["response_times"]) / len(
                    stats["response_times"]
                )
            if stats["total"] > 0:
                stats["failure_rate"] = (stats["failed"] / stats["total"]) * 100

    def _check_alerts(self, latest_record: AICallRecord) -> None:
        """检查是否需要触发告警"""
        if not self.alert_config.enabled:
            return

        now = time.time()

        # 检查冷却时间：按 alert type 各自冷却（FIX 2026-10-23，见 __init__ 注释）。
        # 先按类型过滤一遍，全部都在冷却期内才整体返回，避免"一条告警静默全部"。
        def _in_cooldown(alert_type: str) -> bool:
            last = self._last_alert_time_by_type.get(alert_type, 0.0)
            return now - last < self.alert_config.cooldown_seconds

        alerts_triggered = []

        # 检查失败率
        if (
            self._stats.failure_rate > self.alert_config.failure_rate_threshold
            and not _in_cooldown("high_failure_rate")
        ):
            alerts_triggered.append(
                {
                    "type": "high_failure_rate",
                    "message": f"AI 调用失败率过高: {self._stats.failure_rate:.1f}% (阈值: {self.alert_config.failure_rate_threshold}%)",
                    "severity": "critical" if self._stats.failure_rate > 50 else "warning",
                    "timestamp": now,
                }
            )

        # 检查响应时间
        if (
            self._stats.avg_response_time > self.alert_config.response_time_threshold
            and not _in_cooldown("high_latency")
        ):
            alerts_triggered.append(
                {
                    "type": "high_latency",
                    "message": f"AI 调用平均响应时间过长: {self._stats.avg_response_time:.1f}s (阈值: {self.alert_config.response_time_threshold}s)",
                    "severity": "warning",
                    "timestamp": now,
                }
            )

        # 检查单个慢请求
        if (
            latest_record.response_time > self.alert_config.response_time_threshold * 2
            and not _in_cooldown("slow_request")
        ):
            alerts_triggered.append(
                {
                    "type": "slow_request",
                    "message": f"检测到慢请求: {latest_record.response_time:.1f}s [{latest_record.provider}/{latest_record.model}]",
                    "severity": "info",
                    "timestamp": now,
                }
            )

        # 触发告警
        if alerts_triggered:
            self._last_alert_time = now
            for alert in alerts_triggered:
                self._last_alert_time_by_type[alert["type"]] = now
            self._active_alerts.extend(alerts_triggered)

            # 记录日志
            for alert in alerts_triggered:
                logger.warning("[AI监控] ⚠️ %s - %s", alert["severity"].upper(), alert["message"])

            # 调用告警回调
            self._trigger_alerts(alerts_triggered)

    def _trigger_alerts(self, alerts: list[dict]) -> None:
        """触发告警回调"""
        for callback in self._alert_callbacks:
            try:
                if callable(callback):
                    callback(alerts)
            except Exception as e:
                logger.error("[AI监控] 告警回调执行失败: %s", e)

    def register_alert_callback(self, callback: Any) -> None:
        """注册告警回调函数

        Args:
            callback: 接收 alerts 列表的函数
        """
        self._alert_callbacks.append(callback)
        logger.info("[AI监控] 已注册告警回调")

    def get_stats(self) -> dict:
        """获取当前监控统计信息

        Returns:
            dict: 包含所有统计数据的字典
        """
        with self._lock:
            return {
                "summary": {
                    "total_calls": self._stats.total_calls,
                    "success_calls": self._stats.success_calls,
                    "failed_calls": self._stats.failed_calls,
                    "failure_rate": round(self._stats.failure_rate, 2),
                    "avg_response_time": round(self._stats.avg_response_time, 2),
                    "max_response_time": round(self._stats.max_response_time, 2),
                    "min_response_time": round(self._stats.min_response_time, 2)
                    if self._stats.min_response_time != float("inf")
                    else 0,
                },
                "providers": {
                    name: {
                        "total_calls": stats.total_calls,
                        "success_calls": stats.success_calls,
                        "failed_calls": stats.failed_calls,
                        "failure_rate": round(stats.failure_rate, 2),
                        "avg_response_time": round(stats.avg_response_time, 2),
                        "max_response_time": round(stats.max_response_time, 2),
                        "min_response_time": round(stats.min_response_time, 2)
                        if stats.min_response_time != float("inf")
                        else 0,
                    }
                    for name, stats in self._stats.provider_stats.items()
                },
                "recent_alerts": self._active_alerts[-10:],  # 最近 10 条告警
            }

    def get_provider_stats(self, provider: str) -> dict:
        """获取指定 provider 的统计信息

        Args:
            provider: provider 名称

        Returns:
            dict: provider 统计信息
        """
        with self._lock:
            if provider in self._stats.provider_stats:
                stats = self._stats.provider_stats[provider]
                return {
                    "total": stats["total"],
                    "success": stats["success"],
                    "failed": stats["failed"],
                    "failure_rate": round(stats.get("failure_rate", 0), 2),
                    "avg_response_time": round(stats.get("avg_response_time", 0), 2),
                }
            return {}

    def clear_alerts(self) -> int:
        """清除所有活跃告警

        Returns:
            int: 清除的告警数量
        """
        with self._lock:
            count = len(self._active_alerts)
            self._active_alerts.clear()
            return count

    def reset_stats(self) -> None:
        """重置所有统计信息"""
        with self._lock:
            self._records.clear()
            self._stats = MonitorStats()
            self._active_alerts.clear()
            logger.info("[AI监控] 统计信息已重置")


# ========== 全局监控器实例 ==========

_ai_monitor = AICallMonitor()


def get_ai_monitor() -> AICallMonitor:
    """获取全局 AI 监控器实例"""
    return _ai_monitor


def record_ai_call(
    provider: str,
    model: str,
    success: bool,
    response_time: float,
    tokens_used: int = 0,
    error: str = "",
    task_type: str = "",
) -> None:
    """记录 AI 调用（便捷函数）

    Args:
        provider: AI 提供商名称
        model: 模型名称
        success: 是否成功
        response_time: 响应时间（秒）
        tokens_used: 使用的 token 数
        error: 错误信息
        task_type: 任务类型
    """
    _ai_monitor.record_call(provider, model, success, response_time, tokens_used, error, task_type)


def get_ai_monitor_stats() -> dict:
    """获取 AI 监控统计（便捷函数）"""
    return _ai_monitor.get_stats()


def configure_ai_monitor(alert_config: AlertConfig) -> None:
    """配置 AI 监控参数

    Args:
        alert_config: 告警配置
    """
    _ai_monitor.alert_config = alert_config
    logger.info(
        "[AI监控] 监控配置已更新: 失败率阈值=%.1f%%, 响应时间阈值=%.1fs",
        alert_config.failure_rate_threshold,
        alert_config.response_time_threshold,
    )
