"""智谱 AI（Zhipu）图像生成适配器

实现 ImageProviderAdapter 接口，调用智谱 CogView/GLM-Image API。
OpenAI 兼容协议：https://open.bigmodel.cn/api/paas/v4
参考价格：¥0.15~0.30/张
"""

import logging
from typing import Any

import aiohttp

from ....utils.proxy_config import aiohttp_session_kwargs
from .base import ImageProviderAdapter, build_endpoint, normalize_size


logger = logging.getLogger(__name__)


class ZhipuAdapter(ImageProviderAdapter):
    """智谱 AI 图像生成适配器"""

    provider_id = "zhipu"
    default_model = "cogview-4"
    supported_sizes = ["1K", "2K", "3K", "4K"]
    supported_ratios = ["1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9"]

    def __init__(self, config: dict[str, Any]):
        """初始化适配器

        Args:
            config: 包含 api_key, base_url, model 等配置项
        """
        self.base_url = config.get("base_url", "https://open.bigmodel.cn/api/paas/v4")
        self.api_key = config.get("api_key")
        self.model = config.get("model", self.default_model)
        self.default_size = config.get("default_size", "1024x1024")
        self.default_ratio = config.get("default_ratio", "1:1")
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
        """调用智谱 API 生成图像（OpenAI 兼容格式）"""
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

        headers: dict[str, str] = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout), **aiohttp_session_kwargs()
            ) as session:
                # 智谱的 API endpoint 略有不同（base 下追加 dashscope/images/generations）
                async with session.post(
                    build_endpoint(self.base_url, "dashscope", "images", "generations"),
                    headers=headers,
                    json=payload,
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        # 智谱响应可能在不同字段中
                        image_url = None
                        if isinstance(data.get("data"), list):
                            image_url = data["data"][0].get("url")
                        elif isinstance(data.get("output", {}).get("images"), list):
                            image_url = data["output"]["images"][0].get("url")

                        if image_url:
                            logger.info("Zhipu AI 图像生成成功")
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
                        error_text = (await response.text())[:200]
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
            error_msg = f"Zhipu 调用失败: {e!s}"
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
        """测试智谱 API 连通性"""
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
            logger.debug("Zhipu connection test failed: %s", e)
            return False

    async def list_models(self, api_key: str, base_url: str) -> list[str]:
        """获取智谱支持的模型列表"""
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
            logger.debug("Failed to list models from Zhipu: %s", e)
        return [self.default_model]
