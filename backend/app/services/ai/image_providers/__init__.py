"""图像生成适配器模块

提供统一接口支持多个图像生成平台：
- Agnes AI（默认平台，新加坡）
- 阿里云百炼（DashScope）
- 火山引擎（VolcEngine）
- 腾讯混元（Hunyuan）
- 智谱 AI（Zhipu）
- 阶跃星辰（StepFun）
- 百度文心一格（Baidu）

核心组件：
- ImageProviderAdapter: 抽象基类
- ProviderRegistry: 注册中心
- AgnesAdapter, DashScopeAdapter, VolcanoAdapter, HunyuanAdapter, ZhipuAdapter, StepFunAdapter, BaiduAdapter: 具体适配器实现
"""

from .agnes import AgnesAdapter
from .baidu import BaiduAdapter
from .base import ImageProviderAdapter
from .dashscope import DashScopeAdapter
from .hunyuan import HunyuanAdapter
from .registry import ProviderRegistry
from .stepfun import StepFunAdapter
from .volcano import VolcanoAdapter
from .zhipu import ZhipuAdapter


# 注册所有适配器
def initialize_registry() -> None:
    """注册所有图像生成平台适配器到注册中心"""
    ProviderRegistry.register("agnes", AgnesAdapter)
    ProviderRegistry.register("dashscope", DashScopeAdapter)
    ProviderRegistry.register("volcengine", VolcanoAdapter)
    ProviderRegistry.register("hunyuan", HunyuanAdapter)
    ProviderRegistry.register("zhipu", ZhipuAdapter)
    ProviderRegistry.register("stepfun", StepFunAdapter)
    ProviderRegistry.register("baidu", BaiduAdapter)


# 在模块加载时自动初始化
initialize_registry()

__all__ = [
    "ImageProviderAdapter",
    "ProviderRegistry",
    "AgnesAdapter",
    "DashScopeAdapter",
    "VolcanoAdapter",
    "HunyuanAdapter",
    "ZhipuAdapter",
    "StepFunAdapter",
    "BaiduAdapter",
]
