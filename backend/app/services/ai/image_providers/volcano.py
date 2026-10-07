"""火山引擎（VolcEngine）图像生成适配器

实现 ImageProviderAdapter 接口，调用火山引擎 Seedream API。
参考价格：¥0.22/张
"""

import logging
import os
from typing import Any

import aiohttp

from ....utils.proxy_config import aiohttp_session_kwargs
from .base import ImageProviderAdapter, build_endpoint


logger = logging.getLogger(__name__)


class VolcanoAdapter(ImageProviderAdapter):
    """火山引擎图像生成适配器"""

    provider_id = "volcengine"
    default_model = "seedream-2.0"
    supported_sizes = ["1K", "2K", "3K", "4K"]
    supported_ratios = ["1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9"]

    def __init__(self, config: dict[str, Any]):
        """初始化适配器

        Args:
            config: 包含 ak, sk, base_url, model 等配置项
                ak = access_key, sk = secret_key
        """
        self.ak = config.get("ak") or os.getenv("VOLC_ACCESS_KEY", "")
        self.sk = config.get("sk") or os.getenv("VOLC_SECRET_KEY", "")
        self.base_url = config.get("base_url", "https://ark.cn-beijing.volces.com/api/v3")
        self.model = config.get("model", self.default_model)
        self.default_size = config.get("default_size", "1024x1024")
        self.default_ratio = config.get("default_ratio", "1:1")
        self.timeout = config.get("timeout", 60)
        self.price_per_image = config.get("price_per_image", 0.22)

    async def generate(
        self,
        prompt: str,
        model: str | None = None,
        size: str | None = None,
        ratio: str | None = None,
        reference_images: list[str] | None = None,
        **kwargs,
    ) -> dict[str, Any]:
        """调用火山引擎 Seedream API 生成图像

        火山引擎使用自有协议，不是 OpenAI 兼容格式。
        """
        use_model = model or self.model
        use_size = size or self.default_size

        # 构造火山引擎 API 请求体（自有协议格式）
        form_data = {
            "req_key": use_model,
            "prompt": prompt,
            "model_version": "general_v2.0",
            "width": self._parse_size_width(use_size),
            "height": self._parse_size_height(use_size),
        }

        if reference_images and len(reference_images) > 0:
            # 图生图模式
            form_data["image"] = reference_images[0]

        headers: dict[str, str] = {
            "Content-Type": "application/json",
        }

        # 火山引擎认证通过 query params或 header，这里使用环境变量方式

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout), **aiohttp_session_kwargs()
            ) as session:
                async with session.post(
                    build_endpoint(self.base_url, "cv", "process"),
                    json=form_data,
                    headers=headers,
                    params={"ak": self.ak},  # 简单认证方式，实际可能需要更复杂的签名
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        # 火山引擎响应格式：data.image_urls[0]
                        image_urls = data.get("data", {}).get("image_urls", [])
                        if image_urls:
                            image_url = image_urls[0]
                            logger.info("VolcEngine AI 图像生成成功")
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
                            error_msg = "API 返回中缺少图片 URL 列表"
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
            error_msg = f"VolcEngine 调用失败: {e!s}"
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

    @staticmethod
    def _parse_size_width(size: str) -> int:
        """从尺寸字符串解析宽度"""
        size_map = {
            "1K": 1024,
            "2K": 2048,
            "3K": 3072,
            "4K": 4096,
            "1024x1024": 1024,
            "512x512": 512,
        }
        return size_map.get(size, 1024)

    @staticmethod
    def _parse_size_height(size: str) -> int:
        """从尺寸字符串解析高度"""
        size_map = {
            "1K": 1024,
            "2K": 2048,
            "3K": 3072,
            "4K": 4096,
            "1024x1024": 1024,
            "512x512": 512,
        }
        return size_map.get(size, 1024)

    async def test_connection(self, api_key: str, base_url: str) -> bool:
        """测试火山引擎 API 连通性（简化版，实际需验证 ak/sk）"""
        # 火山引擎需要 ak 和 sk 两个凭证，此处只用 api_key 占位
        try:
            # 简单的 HTTP 请求测试端点可用性
            async with aiohttp.ClientSession(**aiohttp_session_kwargs()) as session:
                async with session.get(
                    base_url,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as response:
                    return response.status in [200, 401]  # 200 表示可达，401 表示认证失败但连接正常
        except Exception:
            return False

    async def list_models(self, api_key: str, base_url: str) -> list[str]:
        """获取火山引擎支持的模型列表（需要 ak/sk）"""
        # 由于火山引擎需要完整认证，这里返回默认模型作为备用
        return [self.default_model]
