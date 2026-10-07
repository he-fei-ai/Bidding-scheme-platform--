"""知识检索缓存（增强 2.2）
复用现有 Redis 基础设施，减少重复 Embedding 调用。
"""

import hashlib
import json
import logging


logger = logging.getLogger(__name__)


class RetrievalCache:
    def __init__(self, ttl: int = 3600):
        self.ttl = ttl
        self._redis = None

    def _get_redis(self):
        if self._redis is None:
            try:
                from ..redis_cache import get_redis_client

                self._redis = get_redis_client()
            except (ImportError, Exception):
                self._redis = None
        return self._redis

    def _get_cache_key(self, query: str, industry: str, top_k: int) -> str:
        raw = f"rag:retrieval:{hashlib.md5(f'{query}|{industry}|{top_k}'.encode()).hexdigest()}"
        return raw

    def get(self, query: str, industry: str, top_k: int) -> list | None:
        redis_client = self._get_redis()
        if redis_client is None:
            return None
        try:
            key = self._get_cache_key(query, industry, top_k)
            cached = redis_client.get(key)
            if cached:
                return json.loads(cached)
        except Exception as e:
            logger.debug("[RetrievalCache] 读取失败: %s", e)
        return None

    def set(self, query: str, industry: str, top_k: int, results: list):
        redis_client = self._get_redis()
        if redis_client is None:
            return
        try:
            key = self._get_cache_key(query, industry, top_k)
            # ENHANCE 2026-09-29: setex 弃用→ set(ex=) 语义等价
            redis_client.set(key, json.dumps(results, ensure_ascii=False), ex=self.ttl)
        except Exception as e:
            logger.debug("[RetrievalCache] 写入失败: %s", e)

    def clear(self, pattern: str = "rag:retrieval:*"):
        redis_client = self._get_redis()
        if redis_client is None:
            return
        try:
            keys = redis_client.keys(pattern)
            if keys:
                redis_client.delete(*keys)
                logger.info("[RetrievalCache] 已清除 %s 条缓存", len(keys))
        except Exception as e:
            logger.debug("[RetrievalCache] 清除失败: %s", e)
