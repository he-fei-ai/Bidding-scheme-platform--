"""服务健康检查核心模块"""

import asyncio
from dataclasses import dataclass, field
from enum import Enum
import logging
import os
from pathlib import Path
import socket
import time
from typing import Any

from .utils.proxy_config import aiohttp_session_kwargs


logger = logging.getLogger(__name__)


class ServiceStatus(Enum):
    """服务状态枚举"""

    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    DEGRADED = "degraded"
    UNKNOWN = "unknown"
    STOPPED = "stopped"


@dataclass
class HealthCheckResult:
    """健康检查结果"""

    service_name: str
    status: ServiceStatus
    message: str = ""
    response_time_ms: float = 0.0
    last_check_time: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "service": self.service_name,
            "status": self.status.value,
            "message": self.message,
            "response_time_ms": round(self.response_time_ms, 2),
            "last_check": self.last_check_time,
            "metadata": self.metadata,
        }


class DatabaseHealthChecker:
    """数据库健康检查器"""

    def __init__(self, db_path: str):
        self.db_path = Path(db_path)

    async def check(self) -> HealthCheckResult:
        """检查数据库状态"""
        start_time = time.perf_counter()

        try:
            # 检查数据库文件是否存在
            if not self.db_path.exists():
                return HealthCheckResult(
                    service_name="database",
                    status=ServiceStatus.STOPPED,
                    message=f"Database file not found: {self.db_path}",
                    response_time_ms=(time.perf_counter() - start_time) * 1000,
                )

            # 检查数据库文件权限
            if not os.access(self.db_path, os.R_OK | os.W_OK):
                return HealthCheckResult(
                    service_name="database",
                    status=ServiceStatus.UNHEALTHY,
                    message="Database file has no read/write permission",
                    response_time_ms=(time.perf_counter() - start_time) * 1000,
                )

            # 尝试连接数据库
            import sqlite3

            try:
                with sqlite3.connect(str(self.db_path)) as conn:
                    conn.execute("SELECT 1")

                return HealthCheckResult(
                    service_name="database",
                    status=ServiceStatus.HEALTHY,
                    message="Database is healthy",
                    response_time_ms=(time.perf_counter() - start_time) * 1000,
                    metadata={
                        "db_size_mb": round(self.db_path.stat().st_size / 1024 / 1024, 2),
                        "last_modified": self.db_path.stat().st_mtime,
                    },
                )
            except sqlite3.Error as e:
                return HealthCheckResult(
                    service_name="database",
                    status=ServiceStatus.UNHEALTHY,
                    message=f"Database connection error: {e!s}",
                    response_time_ms=(time.perf_counter() - start_time) * 1000,
                )

        except Exception as e:
            return HealthCheckResult(
                service_name="database",
                status=ServiceStatus.UNKNOWN,
                message=f"Database check failed: {e!s}",
                response_time_ms=(time.perf_counter() - start_time) * 1000,
            )


class RedisHealthChecker:
    """Redis健康检查器"""

    # Redis 不可达时（本机常见：防火墙静默丢弃而非拒绝），localhost 会先解析 IPv6
    # 再回退 IPv4，裸 socket/redis-py 可能阻塞数秒；统一显式 IPv4 + 短超时。
    _TCP_TIMEOUT = 1.0
    _PING_TIMEOUT = 2.0

    def __init__(self, host: str = "127.0.0.1", port: int = 6379, password: str = None):
        self.host = "127.0.0.1" if host in ("localhost",) else host
        self.port = port
        self.password = password
        self._client = None

    async def check(self) -> HealthCheckResult:
        """检查Redis状态"""
        start_time = time.perf_counter()

        try:
            # 尝试TCP连接（原生 socket，短超时、显式 IPv4，不依赖 redis-py 的连接重试）
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(self._TCP_TIMEOUT)
                s.connect((self.host, self.port))

            # 尝试PONG命令（如果redis-py可用）；用 wait_for 兜底，避免无界阻塞
            try:
                import redis

                if self._client is None:
                    self._client = redis.Redis(
                        host=self.host,
                        port=self.port,
                        decode_responses=True,
                        socket_connect_timeout=0.5,
                        socket_timeout=1,
                        retry_on_timeout=False,
                    )

                pong = await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(None, lambda: self._client.ping()),
                    timeout=self._PING_TIMEOUT,
                )

                if pong:
                    info = await asyncio.wait_for(
                        asyncio.get_running_loop().run_in_executor(
                            None, lambda: self._client.info("server")
                        ),
                        timeout=self._PING_TIMEOUT,
                    )

                    return HealthCheckResult(
                        service_name="redis",
                        status=ServiceStatus.HEALTHY,
                        message="Redis is healthy",
                        response_time_ms=(time.perf_counter() - start_time) * 1000,
                        metadata={
                            "redis_version": info.get("redis_version", "unknown"),
                            "uptime_seconds": info.get("uptime_in_seconds", 0),
                        },
                    )
            except ImportError:
                # redis-py不可用，仅做TCP检查
                return HealthCheckResult(
                    service_name="redis",
                    status=ServiceStatus.HEALTHY,
                    message="Redis TCP connection successful (redis-py not installed)",
                    response_time_ms=(time.perf_counter() - start_time) * 1000,
                )
            except (TimeoutError, Exception) as e:
                # ping/info 超时或失败：端口可达但响应异常
                if isinstance(e, asyncio.TimeoutError):
                    message = "Redis ping/info timeout"
                else:
                    message = f"Redis check failed: {e!s}"
                return HealthCheckResult(
                    service_name="redis",
                    status=ServiceStatus.UNHEALTHY,
                    message=message,
                    response_time_ms=(time.perf_counter() - start_time) * 1000,
                )

        except TimeoutError:
            return HealthCheckResult(
                service_name="redis",
                status=ServiceStatus.STOPPED,
                message="Redis connection timeout",
                response_time_ms=(time.perf_counter() - start_time) * 1000,
            )
        except ConnectionRefusedError:
            return HealthCheckResult(
                service_name="redis",
                status=ServiceStatus.STOPPED,
                message="Redis connection refused",
                response_time_ms=(time.perf_counter() - start_time) * 1000,
            )
        except Exception as e:
            return HealthCheckResult(
                service_name="redis",
                status=ServiceStatus.UNKNOWN,
                message=f"Redis check failed: {e!s}",
                response_time_ms=(time.perf_counter() - start_time) * 1000,
            )

        return HealthCheckResult(
            service_name="redis",
            status=ServiceStatus.UNKNOWN,
            message="Redis check ended without verdict",
            response_time_ms=(time.perf_counter() - start_time) * 1000,
        )


class CeleryHealthChecker:
    """Celery健康检查器"""

    # broker 不可达时（本机 Redis 未运行且连接被静默丢弃），inspect 调用可能阻塞数秒，
    # 统一用 wait_for 包一层超时兜底。
    _CHECK_TIMEOUT = 3.0

    def __init__(self, broker_url: str = "redis://localhost:6379/0"):
        self.broker_url = broker_url

    async def check(self) -> HealthCheckResult:
        """检查Celery状态"""
        start_time = time.perf_counter()

        try:
            from celery import Celery

            app = Celery("tasks", broker=self.broker_url)

            # 检查broker连接
            try:
                inspect = app.control.inspect()
                active = await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(None, inspect.active),
                    timeout=self._CHECK_TIMEOUT,
                )
                registered = await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(None, inspect.registered),
                    timeout=self._CHECK_TIMEOUT,
                )

                workers = active if active else {}
                tasks = registered if registered else {}

                if workers:
                    return HealthCheckResult(
                        service_name="celery",
                        status=ServiceStatus.HEALTHY,
                        message=f"Celery is healthy with {len(workers)} worker(s)",
                        response_time_ms=(time.perf_counter() - start_time) * 1000,
                        metadata={
                            "workers": len(workers),
                            "tasks": list(tasks.keys()) if tasks else [],
                        },
                    )
                else:
                    return HealthCheckResult(
                        service_name="celery",
                        status=ServiceStatus.DEGRADED,
                        message="Celery broker is reachable but no workers are active",
                        response_time_ms=(time.perf_counter() - start_time) * 1000,
                        metadata={"workers": 0},
                    )

            except Exception as e:
                return HealthCheckResult(
                    service_name="celery",
                    status=ServiceStatus.UNHEALTHY,
                    message=f"Celery check failed: {e!s}",
                    response_time_ms=(time.perf_counter() - start_time) * 1000,
                )

        except ImportError:
            return HealthCheckResult(
                service_name="celery",
                status=ServiceStatus.UNKNOWN,
                message="Celery not installed",
                response_time_ms=(time.perf_counter() - start_time) * 1000,
            )
        except Exception as e:
            return HealthCheckResult(
                service_name="celery",
                status=ServiceStatus.UNKNOWN,
                message=f"Celery check failed: {e!s}",
                response_time_ms=(time.perf_counter() - start_time) * 1000,
            )


class APIHealthChecker:
    """API服务健康检查器"""

    def __init__(self, base_url: str = "http://localhost:8000"):
        self.base_url = base_url

    async def check(self) -> HealthCheckResult:
        """检查API服务状态"""
        start_time = time.perf_counter()

        try:
            import aiohttp

            async with aiohttp.ClientSession(**aiohttp_session_kwargs()) as session:
                try:
                    async with session.get(
                        f"{self.base_url}/docs", timeout=aiohttp.ClientTimeout(total=5)
                    ) as resp:
                        if resp.status == 200:
                            return HealthCheckResult(
                                service_name="api",
                                status=ServiceStatus.HEALTHY,
                                message="API server is responding",
                                response_time_ms=(time.perf_counter() - start_time) * 1000,
                                metadata={"status_code": resp.status},
                            )
                        else:
                            return HealthCheckResult(
                                service_name="api",
                                status=ServiceStatus.UNHEALTHY,
                                message=f"API returned status {resp.status}",
                                response_time_ms=(time.perf_counter() - start_time) * 1000,
                                metadata={"status_code": resp.status},
                            )

                except aiohttp.ClientError as e:
                    return HealthCheckResult(
                        service_name="api",
                        status=ServiceStatus.STOPPED,
                        message=f"API server not reachable: {e!s}",
                        response_time_ms=(time.perf_counter() - start_time) * 1000,
                    )

        except ImportError:
            # aiohttp不可用，使用urllib
            import urllib.error
            import urllib.request

            try:
                req = urllib.request.Request(f"{self.base_url}/docs")
                with urllib.request.urlopen(req, timeout=5) as resp:
                    if resp.status == 200:
                        return HealthCheckResult(
                            service_name="api",
                            status=ServiceStatus.HEALTHY,
                            message="API server is responding",
                            response_time_ms=(time.perf_counter() - start_time) * 1000,
                        )
            except urllib.error.URLError as e:
                return HealthCheckResult(
                    service_name="api",
                    status=ServiceStatus.STOPPED,
                    message=f"API server not reachable: {e!s}",
                    response_time_ms=(time.perf_counter() - start_time) * 1000,
                )

        return HealthCheckResult(
            service_name="api",
            status=ServiceStatus.UNKNOWN,
            message="Unable to check API health",
            response_time_ms=(time.perf_counter() - start_time) * 1000,
        )


class SystemHealthChecker:
    """系统资源健康检查器"""

    def __init__(self):
        self.checkers = {
            "cpu": self.check_cpu,
            "memory": self.check_memory,
            "disk": self.check_disk,
        }

    async def check(self) -> HealthCheckResult:
        """检查所有系统资源"""
        results = await asyncio.gather(self.check_cpu(), self.check_memory(), self.check_disk())

        # 返回最严重的状态
        statuses = [r.status for r in results]
        if ServiceStatus.UNHEALTHY in statuses:
            worst = ServiceStatus.UNHEALTHY
        elif ServiceStatus.DEGRADED in statuses:
            worst = ServiceStatus.DEGRADED
        else:
            worst = ServiceStatus.HEALTHY

        # 合并结果
        combined_result = HealthCheckResult(
            service_name="system",
            status=worst,
            message="All system checks passed"
            if worst == ServiceStatus.HEALTHY
            else "Some system checks failed",
            response_time_ms=max(r.response_time_ms for r in results),
            metadata={
                "cpu": results[0].to_dict(),
                "memory": results[1].to_dict(),
                "disk": results[2].to_dict(),
            },
        )

        return combined_result

    async def check_cpu(self) -> HealthCheckResult:
        """检查CPU状态"""
        import psutil

        start_time = time.perf_counter()
        cpu_percent = psutil.cpu_percent(interval=1)

        if cpu_percent < 70:
            status = ServiceStatus.HEALTHY
            message = f"CPU usage normal: {cpu_percent}%"
        elif cpu_percent < 90:
            status = ServiceStatus.DEGRADED
            message = f"CPU usage high: {cpu_percent}%"
        else:
            status = ServiceStatus.UNHEALTHY
            message = f"CPU usage critical: {cpu_percent}%"

        return HealthCheckResult(
            service_name="cpu",
            status=status,
            message=message,
            response_time_ms=(time.perf_counter() - start_time) * 1000,
            metadata={"cpu_percent": cpu_percent},
        )

    async def check_memory(self) -> HealthCheckResult:
        """检查内存状态"""
        import psutil

        start_time = time.perf_counter()
        memory = psutil.virtual_memory()

        if memory.percent < 70:
            status = ServiceStatus.HEALTHY
            message = f"Memory usage normal: {memory.percent}%"
        elif memory.percent < 90:
            status = ServiceStatus.DEGRADED
            message = f"Memory usage high: {memory.percent}%"
        else:
            status = ServiceStatus.UNHEALTHY
            message = f"Memory usage critical: {memory.percent}%"

        return HealthCheckResult(
            service_name="memory",
            status=status,
            message=message,
            response_time_ms=(time.perf_counter() - start_time) * 1000,
            metadata={
                "percent": memory.percent,
                "available_mb": round(memory.available / 1024 / 1024, 2),
                "total_mb": round(memory.total / 1024 / 1024, 2),
            },
        )

    async def check_disk(self) -> HealthCheckResult:
        """检查磁盘状态"""
        import psutil

        start_time = time.perf_counter()
        disk = psutil.disk_usage("/")

        if disk.percent < 80:
            status = ServiceStatus.HEALTHY
            message = f"Disk usage normal: {disk.percent}%"
        elif disk.percent < 90:
            status = ServiceStatus.DEGRADED
            message = f"Disk usage high: {disk.percent}%"
        else:
            status = ServiceStatus.UNHEALTHY
            message = f"Disk usage critical: {disk.percent}%"

        return HealthCheckResult(
            service_name="disk",
            status=status,
            message=message,
            response_time_ms=(time.perf_counter() - start_time) * 1000,
            metadata={
                "percent": disk.percent,
                "free_gb": round(disk.free / 1024**3, 2),
                "total_gb": round(disk.total / 1024**3, 2),
            },
        )


class HealthCheckManager:
    """健康检查管理器"""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.checkers = {}
        self.results: dict[str, HealthCheckResult] = {}
        self._check_interval = config.get("check_interval", 30)
        self._is_running = False
        self._task = None

        self._initialize_checkers()

    def _initialize_checkers(self):
        """初始化健康检查器"""
        # 数据库
        db_path = self.config.get("database_path", "data/app.db")
        self.checkers["database"] = DatabaseHealthChecker(db_path)

        # Redis
        redis_config = self.config.get("redis", {})
        self.checkers["redis"] = RedisHealthChecker(
            host=redis_config.get("host", "localhost"), port=redis_config.get("port", 6379)
        )

        # Celery
        broker_url = self.config.get("celery_broker", "redis://localhost:6379/0")
        self.checkers["celery"] = CeleryHealthChecker(broker_url)

        # API
        api_base_url = self.config.get("api_base_url", "http://localhost:8000")
        self.checkers["api"] = APIHealthChecker(api_base_url)

        # 系统资源
        self.checkers["cpu"] = SystemHealthChecker()
        self.checkers["memory"] = SystemHealthChecker()
        self.checkers["disk"] = SystemHealthChecker()

    async def check_all(self) -> dict[str, HealthCheckResult]:
        """执行所有健康检查"""
        tasks = [checker.check() for checker in self.checkers.values()]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        self.results.clear()
        for checker, result in zip(self.checkers.values(), results, strict=False):
            if isinstance(result, Exception):
                self.results[checker.__class__.__name__] = HealthCheckResult(
                    service_name=checker.__class__.__name__,
                    status=ServiceStatus.UNKNOWN,
                    message=f"Check failed: {result!s}",
                )
            else:
                self.results[checker.__class__.__name__] = result

        return self.results

    async def get_overall_status(self) -> dict[str, Any]:
        """获取整体状态"""
        if not self.results:
            await self.check_all()

        statuses = [r.status for r in self.results.values()]

        if ServiceStatus.UNHEALTHY in statuses or ServiceStatus.STOPPED in statuses:
            overall = ServiceStatus.UNHEALTHY
        elif ServiceStatus.DEGRADED in statuses:
            overall = ServiceStatus.DEGRADED
        else:
            overall = ServiceStatus.HEALTHY

        return {
            "overall_status": overall.value,
            "services": {name: result.to_dict() for name, result in self.results.items()},
            "timestamp": time.time(),
        }

    async def start_monitoring(self):
        """启动监控"""
        if self._is_running:
            return

        self._is_running = True
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info("Health check monitoring started")

    async def stop_monitoring(self):
        """停止监控"""
        self._is_running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Health check monitoring stopped")

    async def _monitor_loop(self):
        """监控循环"""
        while self._is_running:
            try:
                await self.check_all()
                await asyncio.sleep(self._check_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Health check error: %s", e)
                await asyncio.sleep(self._check_interval)


# 全局健康检查管理器实例
_health_manager: HealthCheckManager | None = None


def get_health_manager() -> HealthCheckManager:
    """获取全局健康检查管理器"""
    global _health_manager
    if _health_manager is None:
        _health_manager = HealthCheckManager(
            {
                "check_interval": 30,
                "database_path": "data/app.db",
                "redis": {"host": "localhost", "port": 6379},
                "celery_broker": "redis://localhost:6379/0",
                "api_base_url": "http://localhost:8000",
            }
        )
    return _health_manager


def initialize_health_check(config: dict[str, Any] | None = None):
    """初始化健康检查"""
    global _health_manager
    _health_manager = HealthCheckManager(config or {})
    logger.info("Health check system initialized")
