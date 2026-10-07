"""ChromaDB 向量存储（增强 2.1）
轻量级嵌入式向量数据库，无需独立服务进程。
"""

import logging
from pathlib import Path

import chromadb


logger = logging.getLogger(__name__)


class VectorStore:
    def __init__(self, collection_name: str = "knowledge", persist_dir: str = "./data/chroma"):
        self.persist_dir = persist_dir
        Path(persist_dir).mkdir(parents=True, exist_ok=True)
        self.client = chromadb.PersistentClient(path=persist_dir)
        self.collection = self.client.get_or_create_collection(
            name=collection_name, metadata={"hnsw:space": "cosine"}
        )
        logger.info("[VectorStore] 初始化完成: %s @ %s", collection_name, persist_dir)

    def add_items(self, items: list[dict], embeddings: list[list[float]]):
        if not items:
            return
        self.collection.add(
            ids=[item["id"] for item in items],
            documents=[item["content"] for item in items],
            metadatas=[
                {
                    "title": item.get("title", ""),
                    "industry": item.get("industry", ""),
                    "summary": item.get("summary", "")[:200],
                }
                for item in items
            ],
            embeddings=embeddings,
        )
        logger.info("[VectorStore] 已添加 %s 条向量", len(items))

    def search(self, query_embedding: list[float], top_k: int = 5) -> dict:
        results = self.collection.query(query_embeddings=[query_embedding], n_results=top_k)
        return results

    def delete_items(self, ids: list[str]):
        if ids:
            self.collection.delete(ids=ids)

    def count(self) -> int:
        return self.collection.count()
