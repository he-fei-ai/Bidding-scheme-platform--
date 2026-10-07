"""AI 提供商服务层 — provider_factory 的面向对象门面（Facade）

职责：
  1. Provider 历史记录管理（record / query / clear）
  2. 降级链构建（转调 provider_factory.get_provider_priority_order）
  3. Provider 实例创建（转调 provider_factory 带健康检查与自动降级）
  4. AI 配置缓存管理（转调 provider_factory.get_ai_config）
  5. URL 规范化去重

外部调用方式：
  from app.services.ai.ai_provider_service import AIProviderService

  service = AIProviderService()
  service.record_usage("baidu", "image", "https://...", success=True)
  chain = service.get_priority_chain()
  provider, source = service.create_with_fallback()

重构说明（2026-10-04 遗留闭环 L-1）：
  本模块此前内联了一套**平行实现**：get_priority_chain 只做
  「history → 内置兜底」两层（缺 text_provider_configs 带 key 厂商与
  AI Profile 层）、create_with_fallback 只做「主配置 → agnes」两步、
  get_config 自带一份 5 分钟 TTL 缓存且加锁/解密/校验口径与
  provider_factory 不一致 —— 同一平台两套降级链语义，一旦有调用方
  误用本门面，就会拿到弱化版链路（历史技术债的根源形态）。
  现全部方法收敛为对 provider_factory 的薄委托：实现只有一份，
  本类仅保留面向对象入口与向后兼容的函数别名。
"""

import logging
from typing import Any

from .provider_factory import _FALLBACK_PROVIDERS as _FALLBACK_PROVIDERS  # 兼容旧测试引用
from .provider_factory import (
    _AI_CONFIG_DEFAULTS as _AI_CONFIG_DEFAULTS,  # 兼容旧测试引用
)
from .provider_factory import (
    _clear_ai_config_cache as _factory_clear_config_cache,
)
from .provider_factory import (
    _normalize_url as _normalize_url,
)
from .provider_factory import (
    clear_provider_history as _factory_clear_provider_history,
)
from .provider_factory import (
    create_provider as _factory_create_provider,
)
from .provider_factory import (
    create_provider_with_fallback as _factory_create_with_fallback,
)
from .provider_factory import (
    detect_provider_type,
)
from .provider_factory import (
    get_ai_config as _factory_get_ai_config,
)
from .provider_factory import (
    get_configured_providers as _factory_get_configured_providers,
)
from .provider_factory import (
    get_provider_history as _factory_get_provider_history,
)
from .provider_factory import (
    get_provider_priority_order as _factory_get_priority_order,
)
from .provider_factory import (
    record_provider_usage as _factory_record_usage,
)


logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────────
# AI Provider Service（主服务类 — provider_factory 门面无状态实现）
# ──────────────────────────────────────────────────────────────────────
class AIProviderService:
    """AI 提供商服务门面 — 所有逻辑委托 provider_factory（单一实现源）"""

    # ── 历史记录管理 ──
    def record_usage(
        self, provider_name: str, provider_type: str, base_url: str, success: bool = True
    ) -> None:
        _factory_record_usage(provider_name, provider_type, base_url, success)

    def clear_history(self) -> int:
        return _factory_clear_provider_history()

    def get_history(self) -> list[dict]:
        # 向后兼容：旧实现返回 provider_name/provider_type/... 字段集合，
        # provider_factory 统一版本包含 success_rate，两版消费方均可用。
        return _factory_get_provider_history()

    # ── 降级链管理 ──
    def get_priority_chain(self) -> list[dict]:
        """完整降级链：主配置 → text_provider_configs → Profile → history → 内置兜底"""
        return _factory_get_priority_order()

    def get_configured_providers(self) -> list[dict]:
        return _factory_get_configured_providers()

    def get_builtin_fallbacks(self) -> list[dict]:
        """返回内置兜底提供商列表"""
        return list(_FALLBACK_PROVIDERS)

    # ── Provider 创建（带降级） ──
    def create_with_fallback(self, config: dict | None = None) -> tuple[Any, str]:
        return _factory_create_with_fallback(config)

    def create_provider(self, config: dict | None = None) -> Any:
        return _factory_create_provider(config)

    # ── 配置缓存 ──
    def get_config(self) -> dict:
        return _factory_get_ai_config()

    def clear_config_cache(self) -> None:
        _factory_clear_config_cache()


# ──────────────────────────────────────────────────────────────────────
# 单例实例（模块级默认）
# ──────────────────────────────────────────────────────────────────────
_service = AIProviderService()


# ──────────────────────────────────────────────────────────────────────
# 便捷函数（向后兼容）
# ──────────────────────────────────────────────────────────────────────
def detect_provider_type_from_url(base_url: str) -> str:
    return detect_provider_type(base_url)


def normalize_provider_url(url: str) -> str:
    return _normalize_url(url)


# Public alias for tests
normalize_url = _normalize_url


def record_provider_usage(
    provider_name: str, provider_type: str, base_url: str, success: bool = True
) -> None:
    _service.record_usage(provider_name, provider_type, base_url, success)


def get_provider_priority_order() -> list[dict]:
    return _service.get_priority_chain()


def clear_provider_history() -> int:
    return _service.clear_history()


def get_provider_history() -> list[dict]:
    return _service.get_history()


def get_ai_config() -> dict:
    return _service.get_config()


def clear_ai_config_cache() -> None:
    _service.clear_config_cache()


def create_provider(config: dict | None = None) -> Any:
    return _service.create_provider(config)


def create_provider_with_fallback(config: dict | None = None) -> tuple[Any, str]:
    return _service.create_with_fallback(config)
