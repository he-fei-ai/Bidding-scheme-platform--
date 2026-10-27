#!/usr/bin/env python3
"""
基于 Redis 的速率限制服务模块 - 生产级实现

提供滑动窗口计数算法，支持多实例共享状态。可灵活配置每个标识符的配额。
无 Redis 时自动降级到内存限速，保证服务可用性。
"""

import asyncio
import logging
import threading
import time
from typing import Any

from ..services.redis_cache import get_redis_client

# 修复：只对"Redis 模块未装/不可用"这类环境级永久问题警告一次，
# 避免每次新建 RateLimiter 实例都重复打 WARNING 淹没真正重要的异常。
_rate_limiter_warned_no_redis = False
_rate_limiter_warn_lock = threading.Lock()


logger = logging.getLogger(__name__)


class InMemorySlidingWindowLimiter:
    """内存滑动窗口速率限制器（Redis 降级方案）

    使用线程安全的内存数据结构实现滑动窗口算法，适用于单实例部署场景。
    """

    def __init__(self, default_window: int = 60, default_limit: int = 100):
        self.default_window = default_window
        self.default_limit = default_limit
        self._locks: dict[str, threading.Lock] = {}
        self._data: dict[str, list[float]] = {}
        self._global_lock = threading.Lock()

    def _get_lock(self, identifier: str) -> threading.Lock:
        with self._global_lock:
            if identifier not in self._data:
                self._data[identifier] = []
            return self._locks.setdefault(identifier, threading.Lock())

    async def allow(
        self, identifier: str, limit: int | None = None, window: int | None = None
    ) -> tuple[bool, int]:
        """检查标识符是否被允许请求"""
        if limit is None:
            limit = self.default_limit
        if window is None:
            window = self.default_window

        lock = self._get_lock(identifier)
        with lock:
            now = time.time()
            entries = self._data.get(identifier, [])

            # 移除过期条目
            cutoff = now - window
            self._data[identifier] = [t for t in entries if t > cutoff]
            entries = self._data[identifier]

            if len(entries) >= limit:
                return False, 0

            entries.append(now)
            remaining = limit - len(entries)
            return True, remaining

    async def reset(self, identifier: str) -> None:
        """重置指定标识符的计数"""
        with self._global_lock:
            self._data.pop(identifier, None)
            self._locks.pop(identifier, None)

    async def get_info(self, identifier: str) -> dict[str, Any]:
        """获取标识符的速率限制信息"""
        lock = self._get_lock(identifier)
        with lock:
            now = time.time()
            entries = self._data.get(identifier, [])
            cutoff = now - self.default_window
            current_count = sum(1 for t in entries if t > cutoff)
            return {
                "identifier": identifier,
                "window": self.default_window,
                "limit": self.default_limit,
                "current_count": current_count,
                "remaining": max(0, self.default_limit - current_count),
                "redis_connected": False,
            }


class RedisSlidingWindowLimiter:
    """Redis 滑动窗口速率限制器

    算法说明：在每个时间窗口内维护请求时间戳列表，通过 Redis List 存储，
    使用 Lua 脚本保证原子性操作，自动清理过期条目。
    无 Redis 时自动降级到内存限速。
    """

    def __init__(
        self,
        redis_client=None,
        key_prefix: str = "rl:",
        default_window: int = 60,
        default_limit: int = 100,
    ):
        """
        Args:
            redis_client: Redis 客户端实例（None 则使用单例）
            key_prefix: Redis 键前缀
            default_window: 默认时间窗口秒数
            default_limit: 默认窗口内最大请求数
        """
        self.client = redis_client or get_redis_client()
        self.key_prefix = key_prefix
        self.default_window = default_window
        self.default_limit = default_limit
        self._use_in_memory = False

        if not self.client:
            global _rate_limiter_warned_no_redis
            if not _rate_limiter_warned_no_redis:
                with _rate_limiter_warn_lock:
                    if not _rate_limiter_warned_no_redis:
                        logger.warning("未连接 Redis，降级到内存限速模式（仅首次警告）")
                        _rate_limiter_warned_no_redis = True
            self._use_in_memory = True
            self._memory_limiter = InMemorySlidingWindowLimiter(
                default_window=default_window,
                default_limit=default_limit,
            )

    async def allow(
        self, identifier: str, limit: int | None = None, window: int | None = None
    ) -> tuple[bool, int]:
        """
        检查标识符是否被允许请求

        Args:
            identifier: 唯一标识符（如 user_id、ip、api_key_id）
            limit: 该标识符的最大请求数（使用默认值 if None）
            window: 时间窗口秒数（使用默认值 if None）

        Returns:
            (是否允许，剩余请求数)
        """
        if self._use_in_memory:
            return await self._memory_limiter.allow(identifier, limit, window)

        if limit is None:
            limit = self.default_limit
        if window is None:
            window = self.default_window

        now = time.time()

        # BUG-FIX 2026-10-26（Redis 模式限流完全失效）：此前调用
        # `eval(lua_script, 1, limit, window, now)` 漏传 key —— numkeys=1 时
        # 第一个参数会被当作 KEYS[1]，于是 KEY 变成了 limit 的字符串值
        # （所有不同标识符共用同一个桶），ARGV 整体左移一位：脚本里的
        # `now` 取到 nil → `table.insert(t, nil)` 长度仍为 0 →
        # 每次请求都返回「允许」且计数永不增长，限流形同虚设
        # （实测空桶返回 remaining=61 而非 99）。
        key = f"{self.key_prefix}{identifier}"

        # Lua 脚本：滑动窗口计数检查
        lua_script = """
        local key = KEYS[1]
        local limit = tonumber(ARGV[1])
        local window = tonumber(ARGV[2])
        local now = tonumber(ARGV[3])
        
        -- 获取当前列表
        local entries = redis.call('lrange', key, 0, -1)
        
        -- 移除过期的时间戳
        local new_entries = {}
        for i, entry in ipairs(entries) do
            if tonumber(entry) > now - window then
                table.insert(new_entries, entry)
            end
        end
        
        -- 检查是否超限
        if #new_entries >= limit then
            redis.log(redis.LOG_NOTICE, "Rate limit exceeded for key: "..key)
            return {false, 0}
        end
        
        -- 添加新时间戳
        table.insert(new_entries, now)
        
        -- 重写列表
        redis.call('del', key)
        if #new_entries > 0 then
            redis.call('rpush', key, unpack(new_entries))
            -- 设置稍后的过期时间
            redis.call('expire', key, window + 1)
        end
        
        -- 计算剩余配额（包含本次请求后）
        local remaining = limit - (#new_entries - 1)
        return {true, remaining}
        """

        try:
            # BUG-FIX 2026-09-16（高 事件循环阻塞）：本函数是 `async def`，
            # 而 redis-py 的 `eval` 是同步阻塞 IO。这个限流器挂在 export/images
            # 依赖链上，Redis 往返（网络抖动时可达数百毫秒）期间整个进程停摆。
            result = await asyncio.to_thread(
                self.client.eval, lua_script, 1, key, limit, window, now
            )
            allowed = bool(result[0])
            remaining = int(result[1]) if result[1] else 0
            return allowed, remaining
        except Exception as e:
            logger.error("Lua 执行失败，暂时允许请求但记录错误: %s", e)
            # 遇到错误时宽松放行（避免单点故障导致服务不可用），但应告警
            return True, 0  # 实际应调用监控告警

    async def reset(self, identifier: str) -> None:
        """重置指定标识符的计数（管理端使用）"""
        if self._use_in_memory:
            await self._memory_limiter.reset(identifier)
            return
        key = f"{self.key_prefix}{identifier}"
        try:
            await asyncio.to_thread(self.client.delete, key)
        except Exception as e:
            logger.error("重置速率限制失败: %s", e)

    async def get_info(self, identifier: str) -> dict[str, Any]:
        """获取标识符的速率限制信息（用于调试或监控）"""
        if self._use_in_memory:
            return await self._memory_limiter.get_info(identifier)

        info = {
            "identifier": identifier,
            "window": self.default_window,
            "limit": self.default_limit,
        }
        if not self.client:
            info["redis_connected"] = False
            return info

        key = f"{self.key_prefix}{identifier}"
        try:
            count = int(await asyncio.to_thread(self.client.llen, key))
            info["current_count"] = count
            info["remaining"] = max(0, self.default_limit - count)
            info["redis_connected"] = True
        except Exception as e:
            info["error"] = str(e)
            info["redis_connected"] = False
        return info


# 全局单例
_rate_limiter_instance: RedisSlidingWindowLimiter | None = None


def get_rate_limiter() -> RedisSlidingWindowLimiter:
    """获取全局速率限制器单例实例"""
    global _rate_limiter_instance
    if _rate_limiter_instance is None:
        _rate_limiter_instance = RedisSlidingWindowLimiter()
    return _rate_limiter_instance
