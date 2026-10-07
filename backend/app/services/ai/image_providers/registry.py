"""图像生成平台注册中心

管理所有已注册的图像生成平台适配器，提供统一的路由和查找机制。
"""

import logging
from typing import Any

from .base import ImageProviderAdapter


logger = logging.getLogger(__name__)


class ProviderRegistry:
    """图像生成平台注册中心

    采用单例模式维护适配器注册表，提供平台注册、获取和列表查询功能。
    """

    _adapters: dict[str, type[ImageProviderAdapter]] = {}

    @classmethod
    def register(cls, provider_id: str, adapter_class: type[ImageProviderAdapter]) -> None:
        """注册平台适配器

        Args:
            provider_id: 平台唯一标识（小写，如 'agnes'）
            adapter_class: 适配器类（必须继承 ImageProviderAdapter）
        """
        if provider_id in cls._adapters:
            logger.warning("Platform adapter already registered: %s", provider_id)
        cls._adapters[provider_id] = adapter_class
        logger.info("Registered image provider: %s (%s)", provider_id, adapter_class.__name__)

    @classmethod
    def get_adapter(cls, provider_id: str, config: dict[str, Any]) -> ImageProviderAdapter:
        """获取平台适配器实例

        Args:
            provider_id: 平台唯一标识
            config: 平台配置字典（包含 api_key, base_url, model 等）

        Returns:
            适配器实例

        Raises:
            ValueError: 如果未知平台
        """
        if provider_id not in cls._adapters:
            available = list(cls._adapters.keys())
            raise ValueError(f"Unknown provider: {provider_id}. Available: {available}")
        adapter_class = cls._adapters[provider_id]
        return adapter_class(config)

    @classmethod
    def list_providers(cls) -> list[str]:
        """列出所有已注册平台

        Returns:
            平台标识列表
        """
        return list(cls._adapters.keys())

    @classmethod
    def has_provider(cls, provider_id: str) -> bool:
        """检查平台是否已注册

        Args:
            provider_id: 平台标识

        Returns:
            True 如果已注册，False 否则
        """
        return provider_id in cls._adapters
