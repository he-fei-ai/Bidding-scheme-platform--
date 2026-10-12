"""R-2 2026-10-12：统一「取 provider → 调 collect_json_response」链路。

消除 routers/facts.py 与 routers/templates.py 各自内联 ``create_provider`` +
``collect_json_response`` 的重复（报告 §7.1 R-2）。

**关键约束 —— 必须保持晚绑定（调用时解析）**：

改造前各调用点都是在**函数体内**做
``from ..services.ai.provider_factory import create_provider, get_ai_config``
这类导入 —— 每次调用都会重新解析模块属性，因此测试对
``app.services.ai.provider_factory.create_provider`` /
``app.services.ai.workflows.collect_json_response`` 的 monkeypatch 才会生效。

本模块最初写成模块级 ``from .provider_factory import create_provider`` 再整体导出，
引用在 import 时即定死，补丁全部失效 —— 实测 facts 用例 14 红（真实
``create_provider`` 在无 API Key 环境下抛 400）。因此这里统一改为**调用时解析**：
``create_provider`` / ``get_ai_config`` 是薄透传函数，``collect_json_response``
在 ``run_json_extraction`` 内部按模块属性取用。行为与改造前逐字一致。

另外 provider **仍由调用点预构造后传入**（``provider=``），以保留各自的
错误处理语义：facts.py / templates.py 都把 provider 构造放在 try 之外，
配置故障要直接 500，而不是被下游的启发式兜底静默吞掉。
"""

from .providers.base import AIMessage

__all__ = ["run_json_extraction", "AIMessage", "create_provider", "get_ai_config"]


def get_ai_config():
    """透传 ``provider_factory.get_ai_config``（晚绑定，理由见模块 docstring）。"""
    from .provider_factory import get_ai_config as _f

    return _f()


def create_provider(config):
    """透传 ``provider_factory.create_provider``（晚绑定，理由见模块 docstring）。"""
    from .provider_factory import create_provider as _f

    return _f(config)


async def run_json_extraction(
    messages: list,
    *,
    provider=None,
    ai_config: dict | None = None,
    fallback_provider: bool = False,
    **kwargs,
) -> tuple[object, str | None]:
    """构造（或复用）provider 并调用 ``collect_json_response``。

    Args:
        messages: 传给模型的消息列表（由调用方用 ``AIMessage`` 构造）。
        provider: 调用方已构造好的 provider。传入时不再自行构造，以保留
            调用点原有的 provider 错误处理语义（配置故障直接 500）。
        ai_config: 显式传入的 AI 配置；为 ``None`` 时取 ``get_ai_config()``。
        fallback_provider: 仅当未传 ``provider`` 时生效 —— ``True`` 走
            ``get_working_provider``（带兜底提供商），``False`` 走 ``create_provider``。
        **kwargs: 透传给 ``collect_json_response``（如 temperature / max_retries）。

    Returns:
        ``(result, used_fallback)``；传入 provider 或非 fallback 模式下
        ``used_fallback`` 恒为 ``None``。
    """
    used_fb: str | None = None
    if provider is None:
        cfg = ai_config if ai_config is not None else get_ai_config()
        if fallback_provider:
            from .provider_factory import get_working_provider as _gw

            provider, used_fb = await _gw(cfg)
        else:
            provider = create_provider(cfg)
    # 晚绑定：调用时按模块属性解析，保证对 workflows.collect_json_response
    # 的 monkeypatch 生效（与改造前的函数内 import 语义一致）。
    from . import workflows as _workflows

    result = await _workflows.collect_json_response(provider, messages, **kwargs)
    return result, used_fb
