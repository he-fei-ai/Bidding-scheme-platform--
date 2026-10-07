"""腾讯云混元（Hunyuan）图像生成适配器

实现 ImageProviderAdapter 接口，调用腾讯混元图像生成 API。
OpenAI 兼容协议：https://api.hunyuan.cloud.tencent.com/v1
参考价格：¥0.20/张
"""

import logging
from typing import Any

import aiohttp

from ....utils.proxy_config import aiohttp_session_kwargs
from .base import ImageProviderAdapter, build_endpoint, normalize_size


logger = logging.getLogger(__name__)


class HunyuanAdapter(ImageProviderAdapter):
    """腾讯云混元图像生成适配器"""

    provider_id = "hunyuan"
    default_model = "hunyuan-image"
    supported_sizes = ["1K", "2K", "3K", "4K"]
    supported_ratios = ["1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9"]

    def __init__(self, config: dict[str, Any]):
        """初始化适配器

        Args:
            config: 包含 api_key, base_url, model 等配置项
        """
        self.base_url = config.get("base_url", "https://api.hunyuan.cloud.tencent.com/v1")
        self.api_key = config.get("api_key")
        self.model = config.get("model", self.default_model)
        self.default_size = config.get("default_size", "1024x1024")
        self.default_ratio = config.get("default_ratio", "16:9")
        self.timeout = config.get("timeout", 60)
        self.price_per_image = config.get("price_per_image", 0.20)

    async def generate(
        self,
        prompt: str,
        model: str | None = None,
        size: str | None = None,
        ratio: str | None = None,
        reference_images: list[str] | None = None,
        **kwargs,
    ) -> dict[str, Any]:
        """调用腾讯混元 API 生成图像（OpenAI 兼容格式）"""
        use_model = model or self.model
        use_size = (
            normalize_size(size or self.default_size, self.supported_sizes) or self.default_size
        )

        payload: dict[str, Any] = {
            "model": use_model,
            "prompt": prompt,
            "n": 1,
            "size": use_size,
            "quality": "standard",
        }

        if reference_images and len(reference_images) > 0:
            # 图生图 - 混元可能不支持直接通过 OpenAI 兼容接口，需要特殊处理
            pass  # 暂不支持图生图模式

        headers: dict[str, str] = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout), **aiohttp_session_kwargs()
            ) as session:
                async with session.post(
                    build_endpoint(self.base_url, "images", "generations"),
                    headers=headers,
                    json=payload,
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        image_urls = data.get("data", [])
                        if image_urls:
                            image_url = (
                                image_urls[0].get("url") if isinstance(image_urls, list) else None
                            )
                        else:
                            image_url = None

                        if image_url:
                            logger.info("Hunyuan AI 图像生成成功")
                            return {
                                "success": True,
                                "image_url": image_url,
                                "image_base64": None,
                                "cost": self.price_per_image,
                                "provider": self.provider_id,
                                "model": use_model,
                                "raw_response": data,
                                "error": None,
                            }
                        else:
                            error_msg = "API 返回中缺少图片 URL"
                            logger.error(error_msg)
                            return {
                                "success": False,
                                "image_url": None,
                                "image_base64": None,
                                "cost": 0.0,
                                "provider": self.provider_id,
                                "model": use_model,
                                "raw_response": data,
                                "error": error_msg,
                            }
                    else:
                        error_text = await response.text()[:200]
                        error_msg = f"HTTP {response.status}: {error_text}"
                        logger.error(error_msg)
                        return {
                            "success": False,
                            "image_url": None,
                            "image_base64": None,
                            "cost": 0.0,
                            "provider": self.provider_id,
                            "model": use_model,
                            "raw_response": {"status": response.status, "error": error_text},
                            "error": error_msg,
                        }
        except Exception as e:
            error_msg = f"Hunyuan 调用失败: {e!s}"
            logger.error(error_msg)
            return {
                "success": False,
                "image_url": None,
                "image_base64": None,
                "cost": 0.0,
                "provider": self.provider_id,
                "model": use_model,
                "raw_response": {},
                "error": error_msg,
            }

    async def test_connection(self, api_key: str, base_url: str) -> bool:
        """测试混元 API 连通性"""
        try:
            headers = {"Authorization": f"Bearer {api_key}"}
            async with aiohttp.ClientSession(**aiohttp_session_kwargs()) as session:
                async with session.get(
                    build_endpoint(base_url, "models"),
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as response:
                    return response.status == 200
        except Exception as e:
            logger.debug("Hunyuan connection test failed: %s", e)
            return False

    async def list_models(self, api_key: str, base_url: str) -> list[str]:
        """获取混元支持的模型列表"""
        try:
            headers = {"Authorization": f"Bearer {api_key}"}
            async with aiohttp.ClientSession(**aiohttp_session_kwargs()) as session:
                async with session.get(
                    build_endpoint(base_url, "models"),
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        models = data.get("data", [])
                        return [m.get("id") for m in models if m.get("id")]
        except Exception as e:
            logger.debug("Failed to list models from Hunyuan: %s", e)
        return [self.default_model]
