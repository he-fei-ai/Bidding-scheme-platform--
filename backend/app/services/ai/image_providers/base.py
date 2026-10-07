"""图像生成平台适配器抽象基类

实现 ImageProviderAdapter 抽象基类，为所有图像生成平台提供统一接口。
"""

from abc import ABC, abstractmethod
import re
from typing import Any


def build_endpoint(base_url: str, *suffixes: str) -> str:
    """拼接 API 端点并防御用户误填的完整路径后缀

    用户可能在配置里填  https://api.agnes-ai.cn/v1/images/generations 这种完整端点，
    而适配器内部会再次拼接 /images/generations，导致双路径（.../generations/generations）。
    此工具会自动剥离尾部 /images/generations（及 /dashscope/images/generations）后拼接。

    Args:
        base_url: 配置的 base_url
        *suffixes: 要追加的路径段，如 "images", "generations"

    Returns:
        拼接好的完整 URL
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    base = re.sub(r"/(?:dashscope/)?images/generations$", "", base)
    joined = "".join("/" + s.strip("/") for s in suffixes if s)
    return base + joined


# 尺寸档位对应的长边像素基准
_SIZE_RANK: dict[str, int] = {"1K": 1024, "2K": 2048, "3K": 3072, "4K": 4096}


def normalize_size(size: str | None, supported_sizes: list[str] | None) -> str | None:
    """把像素尺寸（1024x1024）归一化为平台支持的档位（1K/2K/3K/4K）

    前端配置面板使用像素值（如 1024x1024），而档位式平台（Agnes/智谱/阶跃/混元/百度等）
    只接受 1K/2K/3K/4K。此函数按长边就近映射到档位；已是档位或像素式平台则原样返回。

    Args:
        size: 待归一化的尺寸字符串
        supported_sizes: 平台支持的尺寸列表（如 ["1K","2K"]）；为空表示像素式平台

    Returns:
        归一化后的尺寸；无法识别时原样返回
    """
    if not size:
        return None
    s = str(size).strip().lower()
    if s in supported_sizes or s in _SIZE_RANK:
        return s

    # 像素格式 "1024x1024" / "1024*1024"
    m = re.match(r"^\s*(\d+)\s*[x*]\s*(\d+)\s*$", s)
    if not m:
        return size  # 无法识别，原样返回由平台决定

    long_edge = max(int(m.group(1)), int(m.group(2)))
    # 在平台支持的档位里选长边最接近的；平台无档位概念则返回原样
    available = [k for k in supported_sizes if k in _SIZE_RANK] if supported_sizes else []
    if not available:
        return size
    best = min(available, key=lambda k: abs(_SIZE_RANK[k] - long_edge))
    return best


class ImageProviderAdapter(ABC):
    """图像生成平台适配器抽象基类

    所有图像生成平台适配器必须继承此类并实现其抽象方法。
    适配器模式屏蔽不同厂商 API 的差异，提供统一调用接口。
    """

    @property
    @abstractmethod
    def provider_id(self) -> str:
        """平台唯一标识（如 'agnes', 'dashscope', 'volcengine'）"""
        pass

    @property
    @abstractmethod
    def default_model(self) -> str:
        """默认模型名称"""
        pass

    @property
    @abstractmethod
    def supported_sizes(self) -> list[str]:
        """支持的尺寸列表（如 ['1K', '2K', '3K', '4K']）"""
        pass

    @property
    @abstractmethod
    def supported_ratios(self) -> list[str]:
        """支持的宽高比列表（如 ['1:1', '16:9', '9:16']）"""
        pass

    @abstractmethod
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
        生成图像

        Args:
            prompt: 图片描述提示词
            model: 模型名称（使用默认值为 None）
            size: 输出尺寸（使用默认值为 None）
            ratio: 宽高比（使用默认值为 None）
            reference_images: 图生图的参考图片 URL 列表
            **kwargs: 其他参数

        Returns:
            {
                "success": bool,
                "image_url": str | None,
                "image_base64": str | None,
                "cost": float,
                "provider": str,
                "model": str,
                "raw_response": dict,
                "error": str | None  # 失败时存在
            }
        """
        pass

    @abstractmethod
    async def test_connection(self, api_key: str, base_url: str) -> bool:
        """测试 API 连通性

        Args:
            api_key: API 密钥
            base_url: API 基础地址

        Returns:
            True 如果连接成功，False 否则
        """
        pass

    @abstractmethod
    async def list_models(self, api_key: str, base_url: str) -> list[str]:
        """获取可用模型列表

        Args:
            api_key: API 密钥
            base_url: API 基础地址

        Returns:
            模型名称列表
        """
        pass
