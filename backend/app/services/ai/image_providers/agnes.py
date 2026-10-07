"""Agnes AI 图像生成适配器

实现 ImageProviderAdapter 接口，用于调用 Agnes AI 的图像生成 API。
默认平台：https://api.agnes-ai.cn/v1
模型：agnes-image-2.1-flash（当前免费）
"""

import logging
from typing import Any

import aiohttp

from ....utils.proxy_config import aiohttp_session_kwargs
from .base import ImageProviderAdapter, build_endpoint, normalize_size


logger = logging.getLogger(__name__)


class AgnesAdapter(ImageProviderAdapter):
    """Agnes AI 图像生成适配器（默认平台）"""

    provider_id = "agnes"
    default_model = "agnes-image-2.1-flash"
    supported_sizes = ["1K", "2K", "3K", "4K"]
    supported_ratios = ["1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9"]

    def __init__(self, config: dict[str, Any]):
        """初始化适配器

        Args:
            config: 包含 api_key, base_url, model, timeout 等配置项
        """
        self.base_url = config.get("base_url", "https://api.agnes-ai.cn/v1")
        self.api_key = config.get("api_key")
        self.model = config.get("model", self.default_model)
        self.default_size = config.get("default_size", "2K")
        self.default_ratio = config.get("default_ratio", "1:1")
        self.timeout = config.get("timeout", 60)
        self.price_per_image = config.get("price_per_image", 0.0)  # 当前免费

    async def generate(
        self,
        prompt: str,
        model: str | None = None,
        size: str | None = None,
        ratio: str | None = None,
        reference_images: list[str] | None = None,
        **kwargs,
    ) -> dict[str, Any]:
        """
        调用 Agnes Image 2.1 Flash API 生成图像

        Args:
            prompt: 图片描述提示词
            model: 模型名称（使用默认值）
            size: 输出尺寸（使用默认值）
            ratio: 宽高比（使用默认值）
            reference_images: 图生图的参考图片 URL 列表

        Returns:
            图像生成结果字典
        """
        use_model = model or self.model
        use_size = (
            normalize_size(size or self.default_size, self.supported_sizes) or self.default_size
        )
        use_ratio = ratio or self.default_ratio

        # 构造请求体
        payload: dict[str, Any] = {
            "model": use_model,
            "prompt": prompt,
            "size": use_size,
            "ratio": use_ratio,
            "extra_body": {"response_format": "url"},
        }

        # 图生图模式 - 如果有参考图片
        if reference_images and len(reference_images) > 0:
            payload["image"] = reference_images[0]  # 只使用第一个参考图

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
                        image_data = data.get("data", [{}])
                        image_url = image_data[0].get("url") if image_data else None

                        if image_url:
                            logger.info("Agnes AI 图像生成成功: %sx%s", use_size, use_ratio)
                            return {
                                "success": True,
                                "image_url": image_url,
                                "image_base64": None,
                                "cost": 0.0,  # Agnes 当前免费
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
                    elif response.status == 401:
                        error_msg = "认证失败：API Key 无效或过期"
                        logger.error(error_msg)
                        return {
                            "success": False,
                            "image_url": None,
                            "image_base64": None,
                            "cost": 0.0,
                            "provider": self.provider_id,
                            "model": use_model,
                            "raw_response": {"status": response.status},
                            "error": error_msg,
                        }
                    elif response.status == 429:
                        error_msg = "配额限制，请稍后重试"
                        logger.warning(error_msg)
                        return {
                            "success": False,
                            "image_url": None,
                            "image_base64": None,
                            "cost": 0.0,
                            "provider": self.provider_id,
                            "model": use_model,
                            "raw_response": {"status": response.status},
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
        except aiohttp.ClientConnectionError as e:
            error_msg = f"连接 Agnes API 失败: {e!s}"
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
        except aiohttp.ClientTimeout as e:
            error_msg = f"Agnes API 请求超时: {e!s}"
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
        except Exception as e:
            error_msg = f"Agnes AI 生成发生异常: {e!s}"
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
        """测试 Agnes API 连通性

        通过简单的 /models 端点验证 API 密钥和地址的有效性。
        """
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
            logger.debug("Agnes connection test failed: %s", e)
            return False

    async def list_models(self, api_key: str, base_url: str) -> list[str]:
        """获取 Agnes AI 支持的模型列表"""
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
            logger.debug("Failed to list models from Agnes: %s", e)
        return [self.default_model]  # 返回默认模型作为备用
