"""百度智能云（Baidu）图像生成适配器

实现 ImageProviderAdapter 接口，调用百度文心一格（ERNIE-ViLG）API。
参考价格：¥0.06~0.50/张
注意：百度使用自有协议，不是 OpenAI 兼容格式"""

import logging
from typing import Any

import aiohttp

from ....utils.proxy_config import aiohttp_session_kwargs
from .base import ImageProviderAdapter, build_endpoint


logger = logging.getLogger(__name__)


class BaiduAdapter(ImageProviderAdapter):
    """百度文心一格图像生成适配器"""

    provider_id = "baidu"
    default_model = "ernie-vilg"
    supported_sizes = ["1K", "2K"]
    supported_ratios = ["1:1", "3:4", "4:3", "16:9", "9:16"]

    def __init__(self, config: dict[str, Any]):
        """初始化适配器

        Args:
            config: 包含 api_key, base_url, model, secret_key 等配置项
                百度通常需要 api_key 和 secret_token
        """
        self.base_url = config.get("base_url", "https://aip.baidubce.com/rest/2.0/image-gen/v1")
        self.api_key = config.get("api_key")
        self.secret_token = config.get("secret_token") or config.get("secret_key")
        self.model = config.get("model", self.default_model)
        self.default_size = config.get("default_size", "1024x1024")
        self.default_ratio = config.get("default_ratio", "1:1")
        self.timeout = config.get("timeout", 60)
        self.price_per_image = config.get("price_per_image", 0.25)

    async def generate(
        self,
        prompt: str,
        model: str | None = None,
        size: str | None = None,
        ratio: str | None = None,
        reference_images: list[str] | None = None,
        **kwargs,
    ) -> dict[str, Any]:
        """调用百度文心一格 API 生成图像（自有协议）"""
        use_model = model or self.model
        use_size = size or self.default_size
        use_ratio = ratio or self.default_ratio

        # 百度需要先获取 access_token
        # 这里简化处理：假设通过配置直接传入可用的 token 或自动获取
        access_token = await self._get_access_token()

        if not access_token:
            error_msg = "无法获取百度 Access Token"
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

        # 构造百度 API 请求体（JSON 格式）
        payload: dict[str, Any] = {
            "prompt": prompt,
            "size": use_size,
            "ratio": use_ratio,
            "style": "engineering_diagram",  # 默认工程风格
        }

        headers: dict[str, str] = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
        }

        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout), **aiohttp_session_kwargs()
            ) as session:
                async with session.post(
                    build_endpoint(self.base_url, "generate"),
                    headers=headers,
                    json=payload,
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        image_url = (
                            data.get("result", [{}])[0].get("url") if "result" in data else None
                        )

                        if image_url:
                            logger.info("Baidu AI 图像生成成功")
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
            error_msg = f"Baidu 调用失败: {e!s}"
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

    async def _get_access_token(self) -> str:
        """获取百度 Access Token（需要 api_key 和 secret_token）

        百度文心一格使用自有 OAuth 认证协议，需要先通过 API Key + Secret Key
        获取 Access Token，然后使用 Token 调用图像生成接口。

        简化实现：支持三种方式获取 token ——
        1. 直接传入已获取的 Bearer token（以 'bearer ' 开头）
        2. 通过环境变量 BAIDU_ACCESS_TOKEN
        3. 通过 api_key + secret_token 向百度 OAuth 端点请求
        """
        import os

        # 方式 1：配置中直接传入 token
        key_lower = (self.api_key or "").lower()
        if key_lower.startswith("bearer "):
            return self.api_key[len("bearer ") :].strip()
        if key_lower.startswith("token "):
            return self.api_key[len("token ") :].strip()

        # 方式 2：环境变量
        token = os.getenv("BAIDU_ACCESS_TOKEN", "")
        if token:
            return token

        # 方式 3：通过 api_key + secret_token 请求 OAuth 端点
        if self.api_key and self.secret_token:
            try:
                oauth_url = "https://aip.baidubce.com/oauth/2.0/token"
                params = {
                    "grant_type": "client_credentials",
                    "client_id": self.api_key,
                    "client_secret": self.secret_token,
                }
                async with aiohttp.ClientSession(**aiohttp_session_kwargs()) as session:
                    async with session.post(
                        oauth_url,
                        params=params,
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            return data.get("access_token", "")
                        logger.debug("百度 OAuth 请求失败: HTTP %s", resp.status)
            except Exception as e:
                logger.debug("百度 OAuth 请求异常: %s", e)

        logger.warning("无法获取百度 Access Token，请检查 api_key 和 secret_token 配置")
        return ""

    async def test_connection(self, api_key: str, base_url: str) -> bool:
        """测试百度 API 连通性（简化版）"""
        try:
            async with aiohttp.ClientSession(**aiohttp_session_kwargs()) as session:
                async with session.get(
                    base_url,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as response:
                    return response.status in [200, 401]
        except Exception:
            return False

    async def list_models(self, api_key: str, base_url: str) -> list[str]:
        """获取百度支持的模型列表"""
        return [self.default_model]  # 百度模型较少，直接返回默认值
