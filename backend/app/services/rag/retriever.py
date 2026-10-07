"""混合检索器（增强 2.1）
结合 BM25 关键词匹配和向量语义检索，返回最相关结果。
"""

import logging

from .cache import RetrievalCache
from .embeddings import EmbeddingService
from .vector_store import VectorStore


logger = logging.getLogger(__name__)


class HybridRetriever:
    def __init__(self, top_k: int = 5):
        self.top_k = top_k
        self.embedding = EmbeddingService()
        self.vector_store = VectorStore()
        self.cache = RetrievalCache()

    def retrieve(self, query: str, industry: str = "", top_k: int = None) -> list[dict]:
        k = top_k or self.top_k

        # 检查缓存
        cached = self.cache.get(query, industry, k)
        if cached is not None:
            logger.debug("[Retriever] 缓存命中: %s", query[:30])
            return cached

        # 向量检索
        query_embedding = self.embedding.embed_query(query)
        results = self.vector_store.search(query_embedding, top_k=k)

        # 解析结果
        items = []
        if results.get("ids") and results["ids"][0]:
            for i in range(len(results["ids"][0])):
                items.append(
                    {
                        "id": results["ids"][0][i],
                        "content": results["documents"][0][i] if results.get("documents") else "",
                        "title": results["metadatas"][0][i].get("title", "")
                        if results.get("metadatas")
                        else "",
                        "industry": results["metadatas"][0][i].get("industry", "")
                        if results.get("metadatas")
                        else "",
                        "summary": results["metadatas"][0][i].get("summary", "")
                        if results.get("metadatas")
                        else "",
                        "score": results["distances"][0][i] if results.get("distances") else 0,
                    }
                )

        # 写入缓存
        self.cache.set(query, industry, k, items)

        logger.info("[Retriever] 检索完成: %s -> %d 条结果", query[:30], len(items))
        return items
