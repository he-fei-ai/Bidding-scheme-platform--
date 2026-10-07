"""阿里云百炼（DashScope）图像生成适配器

实现 ImageProviderAdapter 接口，调用阿里云百炼 Qwen-Image API。
OpenAI 兼容协议：https://dashscope.aliyuncs.com/compatible-mode/v1
参考价格：¥0.50/张
"""

import logging
from typing import Any

import aiohttp

from ....utils.proxy_config import aiohttp_session_kwargs
from .base import ImageProviderAdapter, build_endpoint


logger = logging.getLogger(__name__)


class DashScopeAdapter(ImageProviderAdapter):
    """阿里云百炼图像生成适配器"""

    provider_id = "dashscope"
    default_model = "qwen-image-3.0-pro"  # 升级为 Qwen-Image-3.0，支持 4.5K token 长输入
    supported_sizes = ["1K", "2K", "3K", "4K"]
    supported_ratios = ["1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9"]

    def __init__(self, config: dict[str, Any]):
        """初始化适配器

        Args:
            config: 包含 api_key, base_url, model 等配置项
                阿里云 API Key 存储在 dashscope.API_KEY 环境变量中或通过配置传入
        """
        self.base_url = config.get("base_url", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.api_key = config.get("api_key") or self._get_api_key_from_env()
        self.model = config.get("model", self.default_model)
        self.default_size = config.get("default_size", "1024x1024")
        self.default_ratio = config.get("default_ratio", "16:9")
        self.timeout = config.get("timeout", 60)
        self.price_per_image = config.get("price_per_image", 0.50)

    def _get_api_key_from_env(self) -> str:
        """从环境变量获取 API Key（阿里云标准方式）"""
        import os

        return os.getenv("DASHSCOPE_API_KEY", "")

    async def generate(
        self,
        prompt: str,
        model: str | None = None,
        size: str | None = None,
        ratio: str | None = None,
        reference_images: list[str] | None = None,
        **kwargs,
    ) -> dict[str, Any]:
        """调用阿里云百炼 Qwen-Image API 生成图像

        支持 Qwen-Image-3.0 增强特性：
        - 4.5K token 超长输入（适用于复杂技术图解）
        - 12 国语言和 20+ 字体原生渲染
        - 蓝白灰专业色调风格约束
        """
        use_model = model or self.model

        # 构造请求体（阿里云百炼 OpenAI 兼容格式）
        payload: dict[str, Any] = {
            "model": use_model,
            "prompt": prompt,
            "n": 1,
            "size": size or self.default_size,
            "quality": "standard",
        }

        # Qwen-Image-3.0 增强：支持更长 prompt 和更丰富的风格参数
        if "qwen-image-3" in use_model:
            payload["parameters"] = {
                "style": "technical_diagram",
                "font": "SimHei",
                "dpi": 300,
            }
            # 如果 prompt 较短，自动补充技术文档风格约束
            if len(prompt) < 200:
                payload["prompt"] = f"技术方案插图，蓝白灰专业色调，图中标注使用黑体。\n\n{prompt}"

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
                        # 阿里云响应结构可能不同，需要根据实际 API 调整
                        # OpenAI 兼容模式通常返回类似结构
                        if "data" in data and isinstance(data["data"], list):
                            image_url = data["data"][0].get("url") if data["data"] else None
                        elif "output" in data:
                            image_url = data["output"].get("url")
                        else:
                            image_url = None

                        if image_url:
                            logger.info("DashScope AI 图像生成成功")
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
            error_msg = f"DashScope 调用失败: {e!s}"
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
        """测试 DashScope API 连通性"""
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
            logger.debug("DashScope connection test failed: %s", e)
            return False

    async def list_models(self, api_key: str, base_url: str) -> list[str]:
        """获取 DashScope 支持的模型列表"""
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
            logger.debug("Failed to list models from DashScope: %s", e)
        return [self.default_model]
