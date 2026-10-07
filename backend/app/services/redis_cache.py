"""Redis缓存服务

提供热点数据缓存功能，优化数据库查询性能。
支持自动过期、缓存失效、统计监控等功能。
降级策略：Redis 不可用时使用内存缓存兜底，不影响功能正常使用。
"""

from collections import OrderedDict
from functools import wraps
import json
import logging
import threading
import time
from typing import Any


logger = logging.getLogger(__name__)

# 全局Redis客户端实例
_redis_client = None
_redis_last_fail_time = 0.0  # 记录上次失败时间，用于退避重试
_redis_connect_attempts = 0  # 连续失败次数
_redis_connecting = False  # 标记是否有后台线程正在尝试连接


# ========== 内存缓存兜底（Redis不可用时使用） ==========
class _MemoryCache:
    """轻量级内存缓存，TTL过期，Redis不可用时作为兜底

    线程安全：所有读写均由 threading.RLock 保护，
    可被 FastAPI 线程池 / asyncio.to_thread worker 并发访问。
    """

    def __init__(self):
        self._lock = threading.RLock()
        self._store: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._max_size = 200  # 最多缓存200条，LRU淘汰
        self._hits = 0
        self._misses = 0

    def get(self, key: str, ttl: int = 300) -> Any | None:
        now = time.time()
        with self._lock:
            if key in self._store:
                value, expire_at = self._store[key]
                if now < expire_at:
                    # 移到末尾（LRU）
                    self._store.move_to_end(key)
                    self._hits += 1
                    return value
                else:
                    del self._store[key]
            self._misses += 1
            return None

    def set(self, key: str, value: Any, ttl: int = 300) -> None:
        now = time.time()
        with self._lock:
            if key in self._store:
                self._store[key] = (value, now + ttl)
                self._store.move_to_end(key)
            else:
                if len(self._store) >= self._max_size:
                    self._store.popitem(last=False)  # 淘汰最久未使用的
                self._store[key] = (value, now + ttl)

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> int:
        with self._lock:
            count = len(self._store)
            self._store.clear()
            return count

    @property
    def stats(self) -> dict:
        with self._lock:
            total = self._hits + self._misses
            return {
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / total * 100, 1) if total > 0 else 0.0,
                "size": len(self._store),
            }


# 内存缓存实例（模块级单例）
_memory_cache = _MemoryCache()


def _resolve_redis_url(url: str) -> str:
    """将 Redis URL 中的 'localhost' 替换为 '127.0.0.1'，避免 IPv6/IPv4 双栈延迟

    Windows 上 localhost 同时解析为 ::1 和 127.0.0.1，redis-py 会先尝试 IPv6
    连接，超时后再回退到 IPv4，导致每次连接尝试多出数秒延迟。
    """
    if "://localhost" in url:
        resolved = url.replace("://localhost", "://127.0.0.1", 1)
        logger.debug("Redis URL 已从 localhost 解析为 127.0.0.1")
        return resolved
    return url


# 修复：Redis 模块未安装（ImportError）是进程级永久问题，
# 每次冷却期过后 _try_connect_redis 被重新触发都会再次 WARNING。
# 加一次性标记保证整个进程生命周期只警告一次。
_redis_import_error_warned = False

def _try_connect_redis():
    """后台线程：尝试连接Redis（不阻塞请求）"""
    global _redis_client, _redis_last_fail_time, _redis_connect_attempts, _redis_connecting
    try:
        import redis

        from ..config import REDIS_URL

        client = redis.from_url(
            _resolve_redis_url(REDIS_URL),
            decode_responses=True,
            socket_timeout=1,
            socket_connect_timeout=0.5,
            retry_on_timeout=False,
            max_connections=20,
            protocol=2,
        )
        client.ping()
        _redis_client = client
        _redis_connect_attempts = 0
        logger.info("Redis连接成功")
    except Exception as e:
        _redis_connect_attempts += 1
        # BUG-CACHE-01 修复：Redis 模块未安装（No module named redis）是环境级永久问题，
        # 每次正文生成/目录生成调用 get_redis_client() 都会打 WARNING，让日志充满无意义噪音。
        # 降级规则：<3 次打 WARNING 帮助首次排查；>=3 次降到 DEBUG，
        # 仅 ModuleNotFoundError（Redis未装）触发此降级；真正连接失败继续 WARNING 保持可见。
        is_redis_module_missing = isinstance(e, ImportError) and "redis" in str(e)
        if is_redis_module_missing:
            # ImportError（模块未装）：环境级永久问题 → 整个进程只警告一次
            global _redis_import_error_warned
            if not _redis_import_error_warned:
                _redis_import_error_warned = True
                logger.warning(
                    "[Redis] 模块未安装，跳过连接; 可 pip install redis 启用缓存（仅首次警告）"
                )
            else:
                logger.debug(
                    "[Redis] 模块未安装，跳过连接(已%d次)", _redis_connect_attempts
                )
        elif _redis_connect_attempts <= 3:
            logger.warning("Redis后台连接失败(%d): %s", _redis_connect_attempts, e)
        else:
            logger.warning("Redis后台连接失败(%d): %s", _redis_connect_attempts, e)
        # 退避：无论降级与否都记录失败时间，避免连接失败时每次请求
        # （章节树查询/详情列表等高频接口）频繁尝试真 Redis 连接。
        _redis_last_fail_time = time.time()
    finally:
        # 状态机修复：无论成败都复位连接标记。
        # 若不复位，get_redis_client 里"退避 + 启动重连线程"的判断将永久失效：
        # 退避被绕过、且残留的（可能已失效的）客户端会让每次请求都执行 ping()，
        # 在 Redis 不可达时每次请求阻塞数秒（如 localhost IPv6/IPv4 双栈连接超时）。
        _redis_connecting = False


def mark_redis_failed():
    """标记 Redis 连接失效（供使用方捕获到操作异常后调用）

    将连接置空并记录失败时间，使 get_redis_client() 进入冷却期，
    避免每次请求都拿坏连接重试。
    """
    global _redis_client, _redis_last_fail_time
    _redis_client = None
    _redis_last_fail_time = time.time()


# Redis 失败后的冷却期（秒）：冷却期内 get_redis_client() 直接返回 None（O(1)），
# 不做 ping / 不建连；冷却结束后由后台线程重建连接。
_REDIS_COOLDOWN = 30


def get_redis_client():
    """获取Redis客户端实例（单例模式，支持自动重连）

    关键设计（修复"每次请求阻塞数秒"问题）：
    - 调用路径为 O(1)：**不主动 ping()**。坏连接由使用方 try/except 捕获后
      调用 mark_redis_failed() 标记失效（RedisCache 各方法均已包裹 try/except）。
    - 失败后进入冷却期（默认 30s），期内直接返回 None，不做任何 Redis 操作。
    - 冷却结束由后台线程重建连接，不阻塞请求线程。

    Returns:
        Redis客户端或None（如果连接失败或未连接）
    """
    global _redis_client, _redis_last_fail_time, _redis_connecting

    now = time.time()

    # 冷却期：失败后 30 秒内对调用方返回 None（不 ping、不建连、不阻塞）
    if _redis_last_fail_time > 0:
        if now - _redis_last_fail_time < _REDIS_COOLDOWN:
            return None
        # 冷却结束：丢弃可能失效的残留连接，走重连流程
        _redis_client = None
        _redis_last_fail_time = 0.0

    # 单例存在（且不在冷却期）：直接返回，不做健康探测
    if _redis_client is not None:
        return _redis_client

    # 没有可用客户端时，启动后台线程尝试连接（不阻塞当前请求）
    if not _redis_connecting:
        _redis_connecting = True
        threading.Thread(target=_try_connect_redis, daemon=True).start()

    return None


class CacheStats:
    """缓存统计信息"""

    def __init__(self):
        self.hits = 0
        self.misses = 0
        self.sets = 0
        self.deletes = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return (self.hits / total * 100) if total > 0 else 0.0

    def to_dict(self) -> dict:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "sets": self.sets,
            "deletes": self.deletes,
            "hit_rate": round(self.hit_rate, 2),
            "total_requests": self.hits + self.misses,
        }


# 全局缓存统计
cache_stats = CacheStats()


class RedisCache:
    """Redis缓存封装类

    提供常用的缓存操作：get、set、delete、exists等。
    支持自动序列化/反序列化、过期时间设置。
    """

    KEY_PREFIX = "tzb:"

    @staticmethod
    def _make_key(key: str) -> str:
        """生成缓存键

        Args:
            key: 原始键名

        Returns:
            带前缀的完整键名
        """
        return f"{RedisCache.KEY_PREFIX}{key}"

    @staticmethod
    def get(key: str) -> Any | None:
        """获取缓存值

        Args:
            key: 缓存键

        Returns:
            缓存值或None
        """
        client = get_redis_client()
        if not client:
            return None

        full_key = RedisCache._make_key(key)
        try:
            value = client.get(full_key)
            if value is not None:
                cache_stats.hits += 1
                return json.loads(value)
            else:
                cache_stats.misses += 1
                return None
        except Exception as e:
            logger.error("Redis GET失败: %s, 错误: %s", key, e)
            cache_stats.misses += 1
            mark_redis_failed()
            return None

    @staticmethod
    def set(key: str, value: Any, expire: int = 3600) -> bool:
        """设置缓存值

        Args:
            key: 缓存键
            value: 缓存值（会自动序列化）
            expire: 过期时间（秒），默认1小时

        Returns:
            是否成功
        """
        client = get_redis_client()
        if not client:
            return False

        full_key = RedisCache._make_key(key)
        try:
            serialized = json.dumps(value, ensure_ascii=False, default=str)
            # ENHANCE 2026-09-29：setex 在新版 redis-py 中已标记弃用（测试日志
            # 出现 85+ 处 DeprecationWarning），set(ex=...) 语义完全等价且为官方
            # 推荐写法，无行为变化。
            client.set(full_key, serialized, ex=expire)
            cache_stats.sets += 1
            return True
        except Exception as e:
            logger.error("Redis SET失败: %s, 错误: %s", key, e)
            mark_redis_failed()
            return False

    @staticmethod
    def delete(key: str) -> bool:
        """删除缓存

        Args:
            key: 缓存键

        Returns:
            是否成功
        """
        client = get_redis_client()
        if not client:
            return False

        full_key = RedisCache._make_key(key)
        try:
            client.delete(full_key)
            cache_stats.deletes += 1
            return True
        except Exception as e:
            logger.error("Redis DELETE失败: %s, 错误: %s", key, e)
            mark_redis_failed()
            return False

    @staticmethod
    def exists(key: str) -> bool:
        """检查缓存是否存在

        Args:
            key: 缓存键

        Returns:
            是否存在
        """
        client = get_redis_client()
        if not client:
            return False

        full_key = RedisCache._make_key(key)
        try:
            return client.exists(full_key) > 0
        except Exception as e:
            logger.error("Redis EXISTS失败: %s, 错误: %s", key, e)
            mark_redis_failed()
            return False

    @staticmethod
    def get_many(keys: list[str]) -> dict[str, Any]:
        """批量获取缓存

        Args:
            keys: 键列表

        Returns:
            键值对字典
        """
        client = get_redis_client()
        if not client:
            return {}

        result = {}
        full_keys = [RedisCache._make_key(k) for k in keys]

        try:
            values = client.mget(full_keys)
            for key, value in zip(keys, values, strict=False):
                if value is not None:
                    try:
                        result[key] = json.loads(value)
                        cache_stats.hits += 1
                    except json.JSONDecodeError:
                        cache_stats.misses += 1
                else:
                    cache_stats.misses += 1
        except Exception as e:
            logger.error("Redis MGET失败: %s", e)
            mark_redis_failed()

        return result

    @staticmethod
    def set_many(data: dict[str, Any], expire: int = 3600) -> bool:
        """批量设置缓存

        Args:
            data: 键值对字典
            expire: 过期时间（秒）

        Returns:
            是否成功
        """
        client = get_redis_client()
        if not client:
            return False

        try:
            pipe = client.pipeline()
            for key, value in data.items():
                full_key = RedisCache._make_key(key)
                serialized = json.dumps(value, ensure_ascii=False, default=str)
                pipe.set(full_key, serialized, ex=expire)  # ENHANCE 2026-09-29: setex 弃用，语义等价改写
            pipe.execute()
            cache_stats.sets += len(data)
            return True
        except Exception as e:
            logger.error("Redis MSET失败: %s", e)
            mark_redis_failed()
            return False

    @staticmethod
    def clear_pattern(pattern: str) -> int:
        """清除匹配模式的缓存

        Args:
            pattern: 键模式（如 "tzb:project:*"）

        Returns:
            清除的数量
        """
        client = get_redis_client()
        if not client:
            return 0

        try:
            count = 0
            for key in client.scan_iter(match=pattern):
                client.delete(key)
                count += 1
            cache_stats.deletes += count
            return count
        except Exception as e:
            logger.error("Redis CLEAR_PATTERN失败: %s, 错误: %s", pattern, e)
            mark_redis_failed()
            return 0


def cache_result(key_prefix: str, expire: int = 3600):
    """缓存结果装饰器

    自动缓存函数返回结果，下次直接从Redis读取。

    Args:
        key_prefix: 缓存键前缀
        expire: 过期时间（秒）

    Usage:
        @cache_result("project_outline", expire=1800)
        def get_project_outline(project_id: int):
            return db_query(...)
    """

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            # 生成缓存键
            key_parts = [key_prefix]
            for arg in args:
                if isinstance(arg, (int, str)):
                    key_parts.append(str(arg))
            for k, v in kwargs.items():
                key_parts.append(f"{k}={v}")

            cache_key = ":".join(key_parts)

            # 尝试从缓存读取
            cached = RedisCache.get(cache_key)
            if cached is not None:
                logger.debug("缓存命中: %s", cache_key)
                return cached

            # 执行函数并缓存结果
            result = func(*args, **kwargs)
            RedisCache.set(cache_key, result, expire=expire)
            logger.debug("缓存写入: %s", cache_key)
            return result

        return wrapper

    return decorator


def invalidate_cache(key_prefix: str):
    """使缓存失效装饰器

    在函数执行后自动清除匹配的缓存。

    Args:
        key_prefix: 要清除的缓存键前缀

    Usage:
        @invalidate_cache("project_outline")
        def update_outline(project_id: int, data: dict):
            db_update(...)
    """

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            result = func(*args, **kwargs)
            pattern = f"{RedisCache.KEY_PREFIX}{key_prefix}:*"
            RedisCache.clear_pattern(pattern)
            logger.debug("缓存失效: %s", pattern)
            return result

        return wrapper

    return decorator


class CacheService:
    """缓存服务

    提供常用业务场景的缓存操作。
    降级策略：Redis不可用时自动降级到内存缓存（LRU，最多200条）。
    """

    # 缓存键定义
    PROJECT_LIST = "project_list"
    PROJECT_DETAIL = "project_detail"
    OUTLINE_CONTENT = "outline_content"
    SECTION_CONTENT = "section_content"
    FILE_LIST = "file_list"
    API_KEYS_LIST = "api_keys_list"
    TEMPLATE_LIST = "template_list"
    KNOWLEDGE_BASE = "knowledge_base"

    # 过期时间配置（秒）
    SHORT_TTL = 300  # 5分钟
    MEDIUM_TTL = 1800  # 30分钟
    LONG_TTL = 7200  # 2小时
    VERY_LONG_TTL = 86400  # 24小时

    @classmethod
    def _fallback_get(cls, key: str, ttl: int = 300) -> Any | None:
        """内存缓存兜底：Redis不可用时使用"""
        return _memory_cache.get(key, ttl)

    @classmethod
    def _fallback_set(cls, key: str, value: Any, ttl: int = 300) -> None:
        """内存缓存兜底：Redis不可用时使用"""
        _memory_cache.set(key, value, ttl)

    @classmethod
    def _fallback_delete(cls, key: str) -> None:
        """内存缓存兜底：Redis不可用时使用"""
        _memory_cache.delete(key)

    @classmethod
    def get_project_list(cls) -> list | None:
        """获取项目列表（缓存）"""
        client = get_redis_client()
        if client:
            return RedisCache.get(cls.PROJECT_LIST)
        return cls._fallback_get(cls.PROJECT_LIST, ttl=cls.MEDIUM_TTL)

    @classmethod
    def set_project_list(cls, projects: list):
        """设置项目列表（缓存）"""
        client = get_redis_client()
        if client:
            RedisCache.set(cls.PROJECT_LIST, projects, expire=cls.MEDIUM_TTL)
        else:
            cls._fallback_set(cls.PROJECT_LIST, projects, ttl=cls.MEDIUM_TTL)

    @classmethod
    def invalidate_project_list(cls):
        """使项目列表缓存失效"""
        client = get_redis_client()
        if client:
            RedisCache.delete(cls.PROJECT_LIST)
        else:
            cls._fallback_delete(cls.PROJECT_LIST)

    @classmethod
    def get_project_detail(cls, project_id: int) -> dict | None:
        """获取项目详情（缓存）"""
        key = f"{cls.PROJECT_DETAIL}:{project_id}"
        client = get_redis_client()
        if client:
            return RedisCache.get(key)
        return cls._fallback_get(key, ttl=cls.SHORT_TTL)

    @classmethod
    def set_project_detail(cls, project_id: int, detail: dict):
        """设置项目详情（缓存）"""
        key = f"{cls.PROJECT_DETAIL}:{project_id}"
        client = get_redis_client()
        if client:
            RedisCache.set(key, detail, expire=cls.SHORT_TTL)
        else:
            cls._fallback_set(key, detail, ttl=cls.SHORT_TTL)

    @classmethod
    def invalidate_project_detail(cls, project_id: int):
        """使项目详情缓存失效"""
        try:
            RedisCache.delete(f"{cls.PROJECT_DETAIL}:{project_id}")
        except Exception as e:
            logger.warning("缓存失效失败（忽略）: %s", e)

    @classmethod
    def get_outline_content(cls, project_id: int) -> str | None:
        """获取大纲内容（缓存）"""
        return RedisCache.get(f"{cls.OUTLINE_CONTENT}:{project_id}")

    @classmethod
    def set_outline_content(cls, project_id: int, content: str):
        """设置大纲内容（缓存）"""
        RedisCache.set(f"{cls.OUTLINE_CONTENT}:{project_id}", content, expire=cls.LONG_TTL)

    @classmethod
    def invalidate_outline(cls, project_id: int):
        """使大纲缓存失效"""
        RedisCache.delete(f"{cls.OUTLINE_CONTENT}:{project_id}")

    @classmethod
    def get_section_content(cls, section_id: int) -> dict | None:
        """获取章节内容（缓存）"""
        return RedisCache.get(f"{cls.SECTION_CONTENT}:{section_id}")

    @classmethod
    def set_section_content(cls, section_id: int, content: dict):
        """设置章节内容（缓存）"""
        RedisCache.set(f"{cls.SECTION_CONTENT}:{section_id}", content, expire=cls.LONG_TTL)

    @classmethod
    def invalidate_section(cls, section_id: int):
        """使章节缓存失效"""
        try:
            RedisCache.delete(f"{cls.SECTION_CONTENT}:{section_id}")
        except Exception as e:
            logger.warning("缓存失效失败（忽略）: %s", e)

    @classmethod
    def invalidate_sections(cls, project_id: int):
        """使项目所有章节缓存失效

        同时删除：
        1. 单个章节内容缓存 SECTION_CONTENT:{section_id}
        2. 整包章节列表缓存 SECTION_CONTENT:project:{project_id} ← 关键修复：之前漏删这个键
        """
        try:
            from ..db import get_db

            # 1. 删整包章节列表缓存（_fetch_project_data_async 命中的就是这个）
            project_sections_key = f"{cls.SECTION_CONTENT}:project:{project_id}"
            client = get_redis_client()
            if client:
                RedisCache.delete(project_sections_key)
            else:
                cls._fallback_delete(project_sections_key)

            # 2. 删单个章节内容缓存（防止章节级别命中旧缓存）
            with get_db() as conn:
                section_ids = [
                    row[0]
                    for row in conn.execute(
                        "SELECT id FROM sections WHERE project_id = ?", (project_id,)
                    ).fetchall()
                ]
            for sid in section_ids:
                key = f"{cls.SECTION_CONTENT}:{sid}"
                if client:
                    RedisCache.delete(key)
                else:
                    cls._fallback_delete(key)
            logger.info("[缓存] invalidate_sections 完成 project=%d: 删除整包键+%d个章节键", project_id, len(section_ids))
        except Exception as e:
            logger.warning("缓存失效失败（忽略）: %s", e)

    @classmethod
    def invalidate_project(cls, project_id: int):
        """使项目相关缓存失效"""
        try:
            keys = [
                f"{cls.PROJECT_DETAIL}:{project_id}",
                f"{cls.OUTLINE_CONTENT}:{project_id}",
                f"{cls.FILE_LIST}:{project_id}",
            ]
            client = get_redis_client()
            if client:
                for key in keys:
                    RedisCache.delete(key)
            else:
                for key in keys:
                    cls._fallback_delete(key)
        except Exception as e:
            logger.warning("缓存失效失败（忽略）: %s", e)

    @classmethod
    def get_project_sections(cls, project_id: int) -> list | None:
        """获取项目章节列表（缓存）"""
        key = f"{cls.SECTION_CONTENT}:project:{project_id}"
        client = get_redis_client()
        if client:
            return RedisCache.get(key)
        return cls._fallback_get(key, ttl=cls.MEDIUM_TTL)

    @classmethod
    def set_project_sections(cls, project_id: int, sections: list):
        """设置项目章节列表（缓存）"""
        key = f"{cls.SECTION_CONTENT}:project:{project_id}"
        client = get_redis_client()
        if client:
            RedisCache.set(key, sections, expire=cls.MEDIUM_TTL)
        else:
            cls._fallback_set(key, sections, ttl=cls.MEDIUM_TTL)

    @classmethod
    def get_file_list(cls, project_id: int) -> list | None:
        """获取文件列表（缓存）"""
        key = f"{cls.FILE_LIST}:{project_id}"
        client = get_redis_client()
        if client:
            return RedisCache.get(key)
        return cls._fallback_get(key, ttl=cls.MEDIUM_TTL)

    @classmethod
    def set_file_list(cls, project_id: int, files: list):
        """设置文件列表（缓存）"""
        key = f"{cls.FILE_LIST}:{project_id}"
        client = get_redis_client()
        if client:
            RedisCache.set(key, files, expire=cls.MEDIUM_TTL)
        else:
            cls._fallback_set(key, files, ttl=cls.MEDIUM_TTL)

    @classmethod
    def invalidate_files(cls, project_id: int):
        """使文件列表缓存失效"""
        key = f"{cls.FILE_LIST}:{project_id}"
        client = get_redis_client()
        if client:
            RedisCache.delete(key)
        else:
            cls._fallback_delete(key)

    @classmethod
    def get_template_list(cls) -> list | None:
        """获取模板列表（缓存）"""
        client = get_redis_client()
        if client:
            return RedisCache.get(cls.TEMPLATE_LIST)
        return cls._fallback_get(cls.TEMPLATE_LIST, ttl=cls.VERY_LONG_TTL)

    @classmethod
    def set_template_list(cls, templates: list):
        """设置模板列表（缓存）"""
        client = get_redis_client()
        if client:
            RedisCache.set(cls.TEMPLATE_LIST, templates, expire=cls.VERY_LONG_TTL)
        else:
            cls._fallback_set(cls.TEMPLATE_LIST, templates, ttl=cls.VERY_LONG_TTL)

    @classmethod
    def invalidate_templates(cls):
        """使模板列表缓存失效"""
        client = get_redis_client()
        if client:
            RedisCache.delete(cls.TEMPLATE_LIST)
        else:
            cls._fallback_delete(cls.TEMPLATE_LIST)

    @classmethod
    def get_stats(cls) -> dict:
        """获取缓存统计信息"""
        return cache_stats.to_dict()

    @classmethod
    def clear_all(cls):
        """清除所有缓存（谨慎使用）"""
        pattern = f"{RedisCache.KEY_PREFIX}*"
        count = RedisCache.clear_pattern(pattern)
        logger.info("清除所有缓存: %d 条", count)
        return count


# 向后兼容的快捷函数
