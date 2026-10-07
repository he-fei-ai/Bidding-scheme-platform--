"""Embedding 服务（增强 2.1）
使用 BGE-M3 模型对文本进行向量化，支持多语言（1024维，8192 token 输入）。
"""

import logging

from sentence_transformers import SentenceTransformer


logger = logging.getLogger(__name__)


class EmbeddingService:
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._model = None
        return cls._instance

    def _load_model(self):
        if self._model is None:
            logger.info("[Embedding] 加载 BGE-M3 模型...")
            self._model = SentenceTransformer("BAAI/bge-m3")
            logger.info("[Embedding] BGE-M3 模型加载完成")

    def embed(self, texts: list[str]) -> list[list[float]]:
        self._load_model()
        return self._model.encode(texts, normalize_embeddings=True).tolist()

    def embed_query(self, text: str) -> list[float]:
        self._load_model()
        return self._model.encode(text, normalize_embeddings=True).tolist()

    @property
    def is_loaded(self) -> bool:
        return self._model is not None
